"""合成资料只验证派生索引的证据边界，不证明真实金融问答质量。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from local_knowledge import LocalKnowledgeProvider
from local_knowledge.structure_index import (POLICY, StructureIndex, select_complete_context,
    _canonical_sentence_windows)
from local_knowledge.evidence_group_selection import plan_evidence_groups
from test_local_vector_retrieval import FixtureEncoder, AS_OF


class StructureIndexTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='navren-structure-fixture-')
        self.addCleanup(directory.cleanup)
        self.encoder=FixtureEncoder()
        self.provider=LocalKnowledgeProvider(Path(directory.name)/'public.sqlite3',encoder=self.encoder)
        self.provider.register_source('market',publisher='合成官方源',endpoints=['https://official.example/'],
            shape='market-official-full-text-v1',freshness={'general':None},permission_basis='synthetic_only')
        # 同一完整句跨原 1000 字符边界；父段必须保留两份原始引文。
        text='Background statement. '*44+'\n赎回申请后两个工作日到账，具体条件需要完整阅读。\n'
        self.version=self.provider.stage_document(source_id='market',source_url='https://official.example/a',
            title='合成报告',text=text,published_on='2026-10-01',retrieved_on=AS_OF,
            effective_from='2026-10-01',shape='market-official-full-text-v1',facets=['general'])
        self.provider.approve_version(self.version,verification_basis='synthetic_test_verified')
        self.release=self.provider.publish(version_ids=[self.version])
        self.index=StructureIndex(self.provider,self.encoder)

    def rows(self):
        with self.provider.db() as db:return self.provider._rows(db,self.release)

    def query(self,ids=None):
        with self.provider.db() as db:
            return self.index.search(db,self.release,'急用钱想拿出来要等多久',
                                     set(ids) if ids is not None else {r['id'] for r in self.rows()})

    def activate_text(self,text):
        version=self.provider.stage_document(source_id='market',source_url='https://official.example/core',
            title='合成句组测试',text=text,published_on='2026-10-01',retrieved_on=AS_OF,
            effective_from='2026-10-01',shape='market-official-full-text-v1',facets=['general'])
        self.provider.approve_version(version,verification_basis='synthetic_test_verified')
        release=self.provider.publish(version_ids=[version])
        self.index.build(release)
        with self.provider.db() as db:
            rows=self.provider._rows(db,release)
            result=self.index.search(db,release,'赎回申请',{r['id'] for r in rows})
        return release,rows,result

    def test_missing_index_is_read_only_fallback(self):
        self.assertEqual(self.query()['reason'],'structure_index_missing')
        with self.provider.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM structure_releases').fetchone()[0],0)
        self.assertEqual(self.encoder.documents,[])

    def test_build_reuse_and_complete_canonical_parent(self):
        built=self.index.build(self.release)
        count=len(self.encoder.documents)
        self.assertTrue(built['ready'])
        self.assertTrue(self.index.build(self.release)['profiles'][0]['reused'])
        self.assertEqual(len(self.encoder.documents),count)
        result=self.query()
        self.assertTrue(result['used'])
        citations={r['id'] for r in self.rows()}
        self.assertTrue(all(set(g['citation_ids']) <= citations for g in result['parent_groups']))
        all_ids=[cid for g in result['parent_groups'] for cid in g['citation_ids']]
        proof=self.provider.resolve_public_evidence_batch(all_ids,self.release,AS_OF)
        self.assertTrue(all(proof.values()))

    def test_real_branch_ranks_are_preserved_before_fusion(self):
        self.index.build(self.release)
        with self.provider.db() as db:
            result=self.index.search(db,self.release,'赎回申请',{r['id'] for r in self.rows()},
                                     projected_query='Background statement')
        ranks=[g['recall_rank_positions'] for g in result['evidence_groups']]
        self.assertTrue(any(g.get('lexical.original')==1 for g in ranks))
        self.assertTrue(any(g.get('vector.primary.original')==1 for g in ranks))
        self.assertTrue(any(g.get('lexical.public_concepts')==1 for g in ranks))
        from local_knowledge.reranking import RECALL_BRANCH_NAMES
        self.assertTrue(all(set(g)<=RECALL_BRANCH_NAMES for g in ranks))

    def test_partial_eligibility_rejects_entire_parent(self):
        self.index.build(self.release)
        result=self.query(ids=[])
        self.assertTrue(result['ready'])
        self.assertFalse(result['used'])
        self.assertEqual(result['parent_groups'],[])

    def test_long_sentence_restores_minimal_core_across_parent_budgets(self):
        text='甲'*1850+'赎回申请后两个工作日到账。'
        release,rows,result=self.activate_text(text)
        original_cores=[group for group in result['evidence_groups'] if group['kind']=='sentence_core']
        self.assertEqual(len(original_cores),1)
        group=original_cores[0]
        self.assertTrue(group['complete'])
        self.assertTrue(group['continuation'])
        self.assertGreater(len(group['parent_ids']),1)
        self.assertEqual((group['start_char'],group['end_char']),(0,len(text)))
        self.assertEqual(set(group['citation_ids']),{r['id'] for r in rows})
        ranked=[(-1,r['id'],{'general'}) for r in rows]
        eligible={r['id'] for r in rows}
        self.assertEqual(plan_evidence_groups(ranked,[group],eligible_ids=eligible,
            canonical_rows=rows,limit=1)[0],[])
        ids,diagnostic=plan_evidence_groups(ranked,[group],eligible_ids=eligible,
                                          canonical_rows=rows,limit=2)
        self.assertEqual(set(ids),eligible)
        self.assertEqual(diagnostic['incomplete_accepted_groups'],0)
        proof=self.provider.resolve_public_evidence_batch(ids,release,AS_OF)
        self.assertTrue(all(proof.values()))

    def test_canonical_windows_expand_all_intersecting_sentences_and_preserve_units(self):
        first='甲'*600+'。';second='乙'*600+'。';third='赎回申请'+'丙'*300+'。'
        text=first+second+third
        release,rows,result=self.activate_text(text)
        windows=[group for group in result['evidence_groups'] if group['kind']=='canonical_sentence_window']
        self.assertEqual(len(windows),len(rows))
        by_anchor={group['canonical_anchor_id']:group for group in windows}
        first_row=min(rows,key=lambda row:row['start_char'])
        window=by_anchor[first_row['id']]
        self.assertEqual((window['start_char'],window['end_char']),(0,len(first)+len(second)))
        for row in rows:
            group=by_anchor[row['id']]
            self.assertTrue(group['complete'])
            self.assertLessEqual(group['start_char'],row['start_char'])
            self.assertGreaterEqual(group['end_char'],row['end_char'])
            self.assertEqual(set(group['citation_ids']),{member['id'] for member in rows
                if member['start_char']<group['end_char'] and member['end_char']>group['start_char']})
        with self.provider.db() as db:
            _,before,digest=self.index._load_units(db,release)
            self.assertNotIn('windows',before)
            repeated=self.index.search(db,release,'另一个完全合成问题',{row['id'] for row in rows})
            _,after,after_digest=self.index._load_units(db,release)
        self.assertEqual(before,after)
        self.assertEqual(digest,after_digest)
        repeated_ids={group['group_id'] for group in repeated['evidence_groups']
                      if group['kind']=='canonical_sentence_window'}
        self.assertEqual(repeated_ids,{group['group_id'] for group in windows})

    def test_canonical_window_rejects_missing_visible_mapping_atomically(self):
        text='甲'*600+'。'+'赎回申请'+'乙'*600+'。'
        release,rows,_=self.activate_text(text)
        first_row=min(rows,key=lambda row:row['start_char'])
        with self.provider.db() as db:
            result=self.index.search(db,release,'完全合成问题',{first_row['id']})
        windows=[group for group in result['evidence_groups'] if group['kind']=='canonical_sentence_window']
        self.assertEqual(len(windows),1)
        window=windows[0]
        self.assertFalse(window['complete'])
        self.assertEqual(window['completion_reason'],'not_eligible')
        self.assertEqual(set(window['citation_ids']),{row['id'] for row in rows})
        selected,diagnostic=plan_evidence_groups([(-1,first_row['id'],{'general'})],windows,
            eligible_ids={first_row['id']},canonical_rows=[first_row],limit=8)
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['ineligible_rejected_groups'],1)

    def test_canonical_window_rejects_a_strict_boundary_change_inside_whole_sentence(self):
        text='甲'*600+'。'+'赎回申请'+'乙'*600+'。'
        release,rows,_=self.activate_text(text)
        with self.provider.db() as db:
            _,payload,_=self.index._load_units(db,release)
        changed=[dict(row) for row in rows]
        changed[1]['conditions']='["合成条件变更"]'
        windows=_canonical_sentence_windows(text,changed,payload['cores'],{row['id'] for row in changed})
        self.assertTrue(windows)
        self.assertTrue(all(not window['complete'] for window in windows))
        self.assertTrue(all(window['completion_reason']=='mixed_boundary' for window in windows))
        self.assertEqual(plan_evidence_groups([(-1,row['id'],{'general'}) for row in changed],windows,
            eligible_ids={row['id'] for row in changed},canonical_rows=changed,limit=8)[0],[])

    def test_uncompleted_sentence_rejects_every_intersecting_canonical_window(self):
        _,rows,result=self.activate_text('甲'*6500+'赎回申请。')
        windows=[group for group in result['evidence_groups'] if group['kind']=='canonical_sentence_window']
        self.assertEqual(len(windows),len(rows))
        self.assertTrue(all(not window['complete'] for window in windows))
        self.assertTrue(all(window['end_char']-window['start_char']>6400 for window in windows))
        selected,diagnostic=plan_evidence_groups([(-1,row['id'],{'general'}) for row in rows],windows,
            eligible_ids={row['id'] for row in rows},canonical_rows=rows,limit=8)
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['core_completion_rejected_groups'],len(windows))

    def test_intact_minimal_child_survives_unavailable_other_parent_citation(self):
        first='赎回申请'+('甲'*900)+'。'
        text=first+('乙'*450)+'。'
        release,rows,_=self.activate_text(text)
        self.assertEqual(len(rows),2)
        first_id=min(rows,key=lambda r:r['start_char'])['id']
        with self.provider.db() as db:
            result=self.index.search(db,release,'赎回申请',{first_id})
        self.assertTrue(result['used'])
        self.assertEqual(result['parent_groups'],[])
        group=result['evidence_groups'][0]
        self.assertTrue(group['complete'])
        self.assertEqual(group['citation_ids'],[first_id])
        self.assertEqual(group['end_char'],len(first))
        self.assertEqual(group['context_options'],[])

    def test_partial_sentence_visibility_cannot_escape_as_canonical_singleton(self):
        release,rows,_=self.activate_text('赎回申请'+'甲'*1850+'。')
        first_id=min(rows,key=lambda r:r['start_char'])['id']
        with self.provider.db() as db:
            result=self.index.search(db,release,'赎回申请',{first_id})
        self.assertTrue(result['used'])
        self.assertGreater(result['blocked_core_count'],0)
        selected,diagnostic=plan_evidence_groups([(-1,first_id,{'general'})],result['evidence_groups'],
            eligible_ids={first_id},canonical_rows=[r for r in rows if r['id']==first_id],limit=8)
        self.assertEqual(selected,[])
        self.assertGreater(diagnostic['ineligible_rejected_groups'],0)

    def test_neighbor_context_crosses_parent_budget_but_not_natural_paragraph(self):
        before='甲'*600+'。'+'乙'*600+'。'
        target='赎回申请'+'丙'*500+'。'
        after='丁'*100+'。'
        _,_,result=self.activate_text(before+target+after)
        core=next(g for g in result['evidence_groups'] if g['start_char']==len(before))
        self.assertTrue(any(option['start_char']<core['start_char']
                            and len(option['parent_ids'])>1 for option in core['context_options']))
        _,_,result=self.activate_text(before+'\n\n'+target+after)
        core=next(g for g in result['evidence_groups'] if g['start_char']==len(before)+2)
        self.assertIsNone(core['previous_core_id'])
        self.assertTrue(all(option['start_char']>=core['start_char'] for option in core['context_options']))

    def test_sentence_over_completion_bound_is_visible_but_never_selected_as_complete(self):
        _,rows,result=self.activate_text('甲'*6500+'赎回申请。')
        self.assertEqual(result['complete_core_count'],0)
        self.assertEqual(result['unresolved_core_count'],1)
        group=result['evidence_groups'][0]
        self.assertFalse(group['complete'])
        selected,diagnostic=plan_evidence_groups([(-1,r['id'],{'general'}) for r in rows],
            [group],eligible_ids={r['id'] for r in rows},canonical_rows=rows,limit=8)
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['core_completion_rejected_groups'],1)

    def test_previous_policy_is_not_rebuilt_on_query(self):
        self.index.build(self.release)
        with self.provider.db() as db:
            db.execute("UPDATE structure_releases SET policy_id='public-sentence-parent-index/1'")
        documents=len(self.encoder.documents)
        self.assertEqual(self.query()['reason'],'structure_index_missing')
        self.assertEqual(len(self.encoder.documents),documents)
        with self.provider.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM structure_releases WHERE policy_id=?',(POLICY,)).fetchone()[0],0)

    def test_corrupt_unit_payload_rejected_without_rebuild(self):
        self.index.build(self.release)
        with self.provider.db() as db:db.execute("UPDATE structure_releases SET payload='{}'")
        before=len(self.encoder.documents)
        self.assertEqual(self.query()['reason'],'structure_index_invalid')
        self.assertEqual(len(self.encoder.documents),before)

    def test_corrupt_vectors_do_not_disable_independent_lexical(self):
        self.index.build(self.release)
        with self.provider.db() as db:db.execute("UPDATE structure_vectors SET vector_sha256='bad'")
        with self.provider.db() as db:
            result=self.index.search(db,self.release,'赎回申请', {r['id'] for r in self.rows()})
        self.assertTrue(result['used'])
        self.assertFalse(result['vector_used'])
        self.assertEqual(result['branches'][0]['reason'],'structure_index_invalid')

    def test_per_unit_window_cap_survives_recomputed_manifest(self):
        self.index.build(self.release)
        with self.provider.db() as db:
            row=db.execute('SELECT * FROM structure_vectors LIMIT 1').fetchone()
            for window in range(1,9):
                db.execute('INSERT INTO structure_vectors VALUES(?,?,?,?,?,?,?,?)',
                    (row['release_id'],row['policy_id'],row['profile_id'],row['unit_id'],window,
                     row['text_sha256'],row['vector'],row['vector_sha256']))
            rows=db.execute('SELECT * FROM structure_vectors ORDER BY unit_id,window_no').fetchall()
            db.execute('UPDATE structure_vector_releases SET vector_count=?,vector_manifest=?',
                       (len(rows),self.index._vector_manifest(rows)))
        self.assertEqual(self.query()['branches'][0]['reason'],'structure_index_invalid')

    def test_provider_gate_rechecks_revocation(self):
        with patch.dict(os.environ,{'NAVREN_STRUCTURE_ENABLED':'1'}):
            self.assertTrue(self.provider.build_vector_index(self.release)['structure_index']['ready'])
            request={'scope':'public','query':'赎回申请','as_of':AS_OF,'limit':8,'facets':[]}
            self.assertTrue(self.provider.search_public(request,self.release)['citation_ids'])
            self.provider.withdraw(self.version)
            self.assertEqual(self.provider.search_public(request,self.release)['citation_ids'],[])

    def test_complete_group_budget_and_incomplete_facet_seed(self):
        groups=[{'citation_ids':['a','b'],'score':-1.0}]
        matches=[(-1.0,'a',set()),(-1.0,'b',set()),(0.0,'c',set())]
        selected,budget=select_complete_context([],matches,groups,1)
        self.assertEqual(selected,['c'])
        self.assertGreater(budget['budget_skipped_groups'],0)
        self.assertEqual(select_complete_context([],matches,groups,2)[0],['a','b'])
        self.assertEqual(select_complete_context(['a'],matches,groups,1)[1]['incomplete_selected_groups'],1)

    def test_overlapping_parent_groups_report_partial_context(self):
        groups=[{'citation_ids':['a'],'score':-2.0},{'citation_ids':['a','b'],'score':-1.0}]
        selected,budget=select_complete_context([], [(-2.0,'a',set()),(-1.0,'b',set())],groups,1)
        self.assertEqual(selected,['a'])
        self.assertEqual(budget['incomplete_selected_groups'],1)

    def test_market_plain_ranking_uses_same_outer_scale(self):
        """市场原件在原召回和父子视图均最优，不能因资料类型降一层分数。"""
        class MixedKindEncoder:
            # 固定模拟向量，仅检验排序合同，不能作为金融/真实模型质量证据。
            dimension=512
            max_windows=8
            profile_id='fixture-mixed-kind-not-real/512-v1'

            def encode_query(self,query):
                value=np.zeros(self.dimension,dtype=np.float32)
                value[0]=1.0
                return value

            def encode_documents(self,texts):
                result=[]
                for text in texts:
                    cosine=0.9 if 'SYNTHETIC_MARKET' in text else 0.85
                    value=np.zeros(self.dimension,dtype=np.float32)
                    value[0]=cosine
                    value[1]=np.sqrt(1.0-cosine*cosine)
                    result.append(value[None,:])
                return result

        provider=LocalKnowledgeProvider(self.provider.path.with_name('mixed-kind-public.sqlite3'),
                                        encoder=MixedKindEncoder())
        provider.register_source('mixed-kind',publisher='合成审计源',
            endpoints=['https://official.example/'],freshness={'general':None},
            permission_basis='synthetic_only')
        versions=[]
        fixtures=(
            ('market','market-official-full-text-v1','SYNTHETIC_MARKET 赎回申请。'),
            ('plain','plain-text-v1','SYNTHETIC_PLAIN 赎回申请。 '+
             ' '.join('word'+str(i) for i in range(80))),
        )
        for label,shape,text in fixtures:
            version=provider.stage_document(source_id='mixed-kind',
                source_url='https://official.example/'+label,title='SYNTHETIC_'+label.upper(),
                text=text,shape=shape,facets=['general'],published_on='2026-10-01',
                retrieved_on=AS_OF)
            provider.approve_version(version,verification_basis='synthetic_only')
            versions.append(version)
        release=provider.publish(version_ids=versions)
        with patch.dict(os.environ,{'NAVREN_STRUCTURE_ENABLED':'1'}):
            built=provider.build_vector_index(release)
        self.assertTrue(built['structure_index']['ready'])
        with provider.db() as db:
            titles={r['id']:r['title'] for r in provider._rows(db,release)}
        request={'scope':'public','query':'赎回申请','as_of':AS_OF,'limit':8,'facets':[]}
        with patch.dict(os.environ,{'NAVREN_STRUCTURE_ENABLED':'0'}):
            baseline=provider.search_public(request,release)
        self.assertEqual([titles[cid] for cid in baseline['citation_ids']],
                         ['SYNTHETIC_MARKET','SYNTHETIC_PLAIN'])
        with patch.dict(os.environ,{'NAVREN_STRUCTURE_ENABLED':'1'}):
            structured=provider.search_public(request,release)
        self.assertEqual([titles[cid] for cid in structured['citation_ids']],
                         ['SYNTHETIC_MARKET','SYNTHETIC_PLAIN'])
        self.assertEqual(set(structured['citation_ids']),set(baseline['citation_ids']))
        meta=structured['retrieval']['structure_index']
        self.assertTrue(meta['ready'])
        self.assertTrue(meta['used'])
        self.assertTrue(meta['vector_used'])
        self.assertIsNone(meta['reason'])
        self.assertEqual(meta['rank_fusion'],'canonical_evidence_group_exact_geometry_rrf/2')
        self.assertEqual(meta['original_geometry_policy'],'canonical-sentence-exact-geometry-original-rrf/1')

    def test_plain_strongest_candidate_is_not_suppressed_by_extra_market_view(self):
        class PlainStrongEncoder(FixtureEncoder):
            profile_id='fixture-plain-strong-not-real/512-v1'
            max_windows=8

            def encode_documents(self,texts):
                self.documents.extend(texts)
                result=[]
                for text in texts:
                    cosine=0.85 if 'SYNTHETIC_MARKET' in text else 0.90
                    value=np.zeros(self.dimension,dtype=np.float32)
                    value[0]=cosine
                    value[1]=np.sqrt(1.0-cosine*cosine)
                    result.append(value[None,:])
                return result

        provider=LocalKnowledgeProvider(self.provider.path.with_name('plain-strong.sqlite3'),
                                       encoder=PlainStrongEncoder())
        provider.register_source('mixed-kind',publisher='合成审计源',
            endpoints=['https://official.example/'],freshness={'general':None},permission_basis='synthetic_only')
        versions=[]
        for label,shape,text in (
            ('market','market-official-full-text-v1','SYNTHETIC_MARKET 赎回申请。 '+
             ' '.join('word'+str(i) for i in range(80))),
            ('plain','plain-text-v1','SYNTHETIC_PLAIN 赎回申请。')):
            version=provider.stage_document(source_id='mixed-kind',source_url='https://official.example/'+label,
                title='SYNTHETIC_'+label.upper(),text=text,shape=shape,facets=['general'],
                published_on='2026-10-01',retrieved_on=AS_OF)
            provider.approve_version(version,verification_basis='synthetic_only')
            versions.append(version)
        release=provider.publish(version_ids=versions)
        with patch.dict(os.environ,{'NAVREN_STRUCTURE_ENABLED':'1'}):
            self.assertTrue(provider.build_vector_index(release)['structure_index']['ready'])
        with provider.db() as db:
            titles={r['id']:r['title'] for r in provider._rows(db,release)}
        request={'scope':'public','query':'赎回申请','as_of':AS_OF,'limit':8,'facets':[]}
        for enabled in ('0','1'):
            with self.subTest(enabled=enabled),patch.dict(os.environ,{'NAVREN_STRUCTURE_ENABLED':enabled}):
                result=provider.search_public(request,release)
                self.assertEqual([titles[cid] for cid in result['citation_ids']],
                                 ['SYNTHETIC_PLAIN','SYNTHETIC_MARKET'])
                if enabled=='1':
                    budget=result['retrieval']['structure_index']['context_budget']
                    self.assertEqual(budget['accepted_groups'][0]['kind'],'canonical_singleton')


if __name__=='__main__':unittest.main()
