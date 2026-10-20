"""多视图接入的边界回归；不使用冻结金融问题或期待。"""
import os
import sqlite3
import unittest
from unittest.mock import patch

from local_knowledge.query_group_retrieval import merge_query_groups, additional_query_structures, rank_query_groups, with_context_candidates, _script_language


def group(gid='eg-a', **extra):
    return {'group_id':gid,'kind':'sentence_core','version_id':'v-a',
            'citation_ids':['c-a'],'start_char':0,'end_char':4,'complete':True,
            'score':-0.1,'child_rank':2,'recall_rank_positions':{'lexical.original':2}, **extra}


class QueryGroupIntegrationTests(unittest.TestCase):
    def test_disabled_path_never_splits_or_loads_model(self):
        original={'used':True,'evidence_groups':[group()]}
        with patch.dict(os.environ,{'NAVREN_RERANK_ENABLED':'0'}), \
                patch('local_knowledge.query_group_retrieval.build_query_segments',side_effect=AssertionError('must_not_run')):
            structure,local_queries,meta=additional_query_structures(None,None,'pub-a','private marker',{'c-a'},[],original)
        self.assertIs(structure,original)
        self.assertEqual(local_queries,[])
        self.assertNotIn('private marker',str(meta))

    def test_all_branch_ranks_survive_same_group_merge(self):
        first={'evidence_groups':[group()]}
        second={'evidence_groups':[group(child_rank=1,recall_rank_positions={'vector.primary.original':1})]}
        merged=merge_query_groups(first,[second])
        self.assertEqual(merged[0]['recall_rank_positions'],{'q0.lexical.original':2,'q1.vector.primary.original':1})
        self.assertEqual(merged[0]['child_rank'],1)
        self.assertEqual(first['evidence_groups'][0]['recall_rank_positions'],{'lexical.original':2})

    def test_same_group_id_cannot_rebind_version_span_or_citations(self):
        for changed in ({'version_id':'v-b'},{'start_char':1},{'citation_ids':['c-b']},{'complete':False},
                        {'boundary_key':'other'},{'canonical_anchor_id':'c-b'},{'window_policy':'changed'}):
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'identity_mismatch'):
                merge_query_groups({'evidence_groups':[group()]},[{'evidence_groups':[group(**changed)]}])

    def test_context_is_independent_atomic_candidate_and_deduplicated_by_span(self):
        option={'group_id':'cx-a','kind':'context_supplement','core':False,'complete':True,
                'version_id':'v-a','boundary_key':'boundary','start_char':0,'end_char':12,
                'citation_ids':['c-a','c-b'],'required_group_id':'eg-a'}
        original={'evidence_groups':[group(context_options=[option]),
                                    group('eg-b',context_options=[dict(option,required_group_id='eg-b')])]}
        expanded=with_context_candidates(original)
        contexts=[item for item in expanded['evidence_groups'] if item['kind']=='context_core']
        self.assertEqual(len(contexts),1)
        self.assertTrue(contexts[0]['core'])
        self.assertEqual(contexts[0]['citation_ids'],['c-a','c-b'])
        self.assertNotIn('required_group_id',contexts[0])
        self.assertEqual(len(original['evidence_groups']),2)

    def test_missing_reranker_cannot_create_subquestion_seed(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','digest')")
        rows=[{'version_id':'v-a','id':'c-a'}]
        diagnostics={'used':False,'reason':'reranker_model_missing'}
        with patch('local_knowledge.reranking.rerank_groups',return_value=([],diagnostics)):
            ids,seeds,meta=rank_query_groups(db,'本地查询',[group()],rows,[('子句',{'evidence_groups':[group()]})])
        db.close()
        self.assertEqual(ids,[]);self.assertEqual(seeds,[])
        self.assertEqual(meta['segment_used_count'],0)
        self.assertEqual(meta['segment_missing_core_count'],1)
        self.assertNotIn('本地查询',str(meta));self.assertNotIn('子句',str(meta))

    def test_only_current_eligible_versions_are_given_to_reranker(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.executemany('INSERT INTO document_versions VALUES(?,?,?)',[('v-a','公共原文','a'),('v-private','个人余额','b')])
        received=[]
        def ranking(query,groups,rows,texts,**kwargs):
            received.append(texts)
            return ['eg-a'],{'used':True,'reason':None}
        with patch('local_knowledge.reranking.rerank_groups',side_effect=ranking):
            ids,seeds,meta=rank_query_groups(db,'查询',[group()], [{'id':'c-a','version_id':'v-a'}],
                                           [('子句',{'evidence_groups':[group()]})])
        db.close()
        self.assertEqual(seeds,['eg-a'])
        self.assertTrue(all(set(item)=={'v-a'} for item in received))
        self.assertNotIn('个人余额',str(received));self.assertNotIn('公共原文',str(meta))
        self.assertEqual(meta['segment_missing_core_count'],0)

    def test_duplicate_seed_for_two_questions_does_not_claim_one_is_missing(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
        with patch('local_knowledge.reranking.rerank_groups',return_value=(['eg-a'],{'used':True,'reason':None})):
            _,seeds,meta=rank_query_groups(db,'查询',[group()],[{'id':'c-a','version_id':'v-a'}],
                [('子句甲',{'evidence_groups':[group()]}),('子句乙',{'evidence_groups':[group()]})])
        db.close()
        self.assertEqual(seeds,['eg-a'])
        self.assertEqual(meta['segment_count'],2)
        self.assertEqual(meta['segment_missing_core_count'],0)

    def test_diagnostic_order_and_counts_are_fused_views_not_first_view(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
        def ranking(query,*args,**kwargs):
            ordered=['eg-a','eg-b'] if query=='原问' else ['eg-b','eg-a','eg-c']
            return ordered,{'used':True,'reason':None,'scored_count':len(ordered),
                            'ranked_groups':[{'group_id':gid,'rank':index+1,'window_count':1}
                                             for index,gid in enumerate(ordered)]}
        with patch('local_knowledge.reranking.rerank_groups',side_effect=ranking):
            ids,seeds,meta=rank_query_groups(db,'原问',[group('eg-a'),group('eg-b',start_char=1),group('eg-c',start_char=2)],
                [{'id':'c-a','version_id':'v-a'}],[],translated_query='本地译文')
        db.close()
        self.assertEqual(ids,['eg-a','eg-b','eg-c'])
        self.assertEqual([item['group_id'] for item in meta['ranked_groups']],ids)
        self.assertEqual(meta['scored_count'],3)
        self.assertEqual(meta['scored_pair_count'],5)
        self.assertEqual(meta['query_view_count'],2)
        self.assertNotIn('本地译文',str(meta))

    def test_final_publication_after_deadline_clears_all_success(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
        # 初始deadline、视图前、融合前都未超时，最终发布跨过共同30秒。
        with patch('local_knowledge.reranking.rerank_groups',return_value=(['eg-a'],
                {'used':True,'reason':None,'scored_count':1,'ranked_groups':[]})), \
             patch('local_knowledge.query_group_retrieval.time.monotonic',side_effect=[0,1,2,31]):
            ids,seeds,meta=rank_query_groups(db,'查询',[group()],
                [{'id':'c-a','version_id':'v-a'}],[])
        db.close()
        self.assertEqual(ids,[]);self.assertEqual(seeds,[])
        self.assertFalse(meta['used']);self.assertEqual(meta['reason'],'rerank_timeout')
        self.assertEqual(meta['scored_count'],0);self.assertEqual(meta['ranked_groups'],[])
        self.assertEqual(meta['scored_pair_count'],0);self.assertEqual(meta['query_view_count'],0)
        self.assertEqual(meta['preferred_original_group_ids'],[])
        self.assertEqual(meta['original_canonical_preference_count'],0)

    def test_clear_han_and_latin_sources_use_matching_query_language(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.executemany('INSERT INTO document_versions VALUES(?,?,?)',
                       [('v-han','公开教育项目研究计划','a'),('v-latin','Public educational project study plan.','b')])
        groups=[group('eg-han',version_id='v-han',end_char=10),
                group('eg-latin',version_id='v-latin',end_char=38)]
        received=[]
        def ranking(query,groups,*args,**kwargs):
            ids=[item['group_id'] for item in groups];received.append((query,ids))
            return ids,{'used':True,'reason':None,'scored_count':len(ids)}
        with patch('local_knowledge.reranking.rerank_groups',side_effect=ranking):
            ids,seeds,meta=rank_query_groups(db,'教育项目',[*groups],
                [{'id':'c-han','version_id':'v-han'},{'id':'c-latin','version_id':'v-latin'}],[],
                translated_query='Educational project')
        db.close()
        self.assertEqual(received,[('教育项目',['eg-han']),('Educational project',['eg-latin'])])
        self.assertCountEqual(ids,['eg-han','eg-latin'])
        self.assertTrue(meta['script_alignment_used']);self.assertEqual(meta['query_view_count'],2)

    def test_unknown_script_uses_both_views_without_omitting_candidate(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','1234','a')")
        received=[]
        def ranking(query,groups,*args,**kwargs):
            received.append(query)
            return ['eg-a'],{'used':True,'reason':None,'scored_count':1}
        with patch('local_knowledge.reranking.rerank_groups',side_effect=ranking):
            ids,_,meta=rank_query_groups(db,'教育项目',[group()],
                [{'id':'c-a','version_id':'v-a'}],[],translated_query='Educational project')
        db.close()
        self.assertEqual(received,['教育项目','Educational project']);self.assertEqual(ids,['eg-a'])

    def test_other_unicode_letters_cannot_be_misclassified_by_one_han_or_latin(self):
        self.assertIsNone(_script_language('Ж'*50+'汉'))
        self.assertIsNone(_script_language('Ж'*50+'A'))
        self.assertIsNone(_script_language('公開計画'+'あいうえお'))
        self.assertEqual(_script_language('完整公开教育计划'),'han')
        self.assertEqual(_script_language('Public education plan.'),'latin')

    def test_unknown_lane_does_not_get_double_vote_over_each_first_place(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.executemany('INSERT INTO document_versions VALUES(?,?,?)',
            [('v-han','公开教育计划','a'),('v-latin','Public education plan','b'),('v-unknown','1234','c')])
        groups=[group('eg-han',version_id='v-han'),group('eg-latin',version_id='v-latin'),
                group('eg-unknown',version_id='v-unknown')]
        def ranking(query,*args,**kwargs):
            ids=['eg-han','eg-unknown'] if query=='教育计划' else ['eg-latin','eg-unknown']
            return ids,{'used':True,'reason':None,'scored_count':2}
        with patch('local_knowledge.reranking.rerank_groups',side_effect=ranking):
            ids,_,meta=rank_query_groups(db,'教育计划',groups,
                [{'id':'c-han','version_id':'v-han'},{'id':'c-latin','version_id':'v-latin'},
                 {'id':'c-unknown','version_id':'v-unknown'}],[],translated_query='Education plan')
        db.close()
        self.assertEqual(ids[-1],'eg-unknown')
        self.assertEqual(meta['scored_count'],3);self.assertEqual(meta['scored_pair_count'],4)

    def test_language_lanes_share_one_bounded_pool_before_scoring(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        groups=[];rows=[]
        for index in range(250):
            version='v-'+str(index)
            text='公开教育计划' if index%2 else 'Public education plan'
            db.execute('INSERT INTO document_versions VALUES(?,?,?)',(version,text,'digest'))
            groups.append(group('eg-'+str(index),version_id=version))
            rows.append({'id':'c-'+str(index),'version_id':version})
        received=[]
        def ranking(query,groups,*args,**kwargs):
            ids=[item['group_id'] for item in groups];received.extend(ids)
            return ids,{'used':True,'reason':None,'scored_count':len(ids)}
        with patch('local_knowledge.reranking.rerank_groups',side_effect=ranking):
            ids,_,meta=rank_query_groups(db,'教育计划',groups,rows,[],translated_query='Education plan')
        db.close()
        self.assertEqual(meta['query_candidate_pool_count'],192)
        self.assertEqual(len(set(received)),192)
        self.assertLessEqual(len(ids),192)
        self.assertEqual(set(ids),set(received))
        self.assertEqual(meta['pool_count'],192)
        self.assertEqual(meta['pool_selection'],'shared_query_candidate_pool')
        self.assertEqual(meta['pool_version_count'],192)

    def test_common_pool_strong_geometry_rank_survives_actual_lane_reranker(self):
        from test_local_reranking import fixture,FakeReranker
        groups,rows,texts=fixture(1)
        core=dict(groups[0],rank_positions={'canonical':80,'sentence':80},
                  child_rank=80,recall_rank_positions={'vector.secondary.original':1})
        window=dict(core,group_id='cw-public',kind='canonical_sentence_window',
                    canonical_anchor_id=rows[0]['id'],canonical_anchor_rank=80,
                    recall_rank_positions={})
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        record=texts[rows[0]['version_id']]
        db.execute('INSERT INTO document_versions VALUES(?,?,?)',
                   (rows[0]['version_id'],record['parsed_text'],record['parsed_sha256']))
        fake=FakeReranker()
        with patch.dict(os.environ,{'NAVREN_RERANK_ENABLED':'1'}), \
             patch('local_knowledge.reranking.get_reranker',return_value=fake):
            ids,seeds,meta=rank_query_groups(db,'公开资料',[window,core],rows,[])
        db.close()
        self.assertEqual(ids,['cw-public']);self.assertEqual(len(fake.calls),1)
        self.assertEqual(fake.calls[0][1],[record['parsed_text']])
        self.assertTrue(meta['used']);self.assertIsNone(meta['reason'])
        self.assertEqual(meta['pool_count'],1);self.assertEqual(meta['scored_count'],1)
        self.assertEqual(meta['pool_geometry_duplicate_count'],1)
        self.assertEqual(meta['pool_branch_counts']['vector.secondary.original'],1)

    def test_original_legal_rrf_view_is_retained_when_model_reorders_groups(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
        groups=[group('eg-a',fused_score=-1/61),
                group('eg-b',start_char=1,fused_score=-1/160)]
        with patch('local_knowledge.reranking.rerank_groups',return_value=(['eg-b','eg-a'],
                {'used':True,'reason':None,'scored_count':2})):
            ids,_,meta=rank_query_groups(db,'查询',groups,
                [{'id':'c-a','version_id':'v-a'}],[])
        db.close()
        self.assertEqual(ids,['eg-a','eg-b'])
        self.assertEqual(meta['query_view_policy'],
                         'original-exact-geometry-and-script-aligned-rerank-equal-rrf/4')
        self.assertEqual(meta['ranked_groups'][0]['query_view_rank_positions'],{'original':2})
        self.assertEqual(meta['ranked_groups'][0]['original_rrf_score'],-1/61)

    def test_invalid_original_rrf_score_cannot_add_a_vote(self):
        for value in (True,0,1,-1,float('nan'),float('inf'),'private-value'):
            with self.subTest(value=value):
                db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
                db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
                db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
                groups=[group('eg-a',fused_score=value),group('eg-b',start_char=1)]
                with patch('local_knowledge.reranking.rerank_groups',return_value=(['eg-b','eg-a'],
                        {'used':True,'reason':None,'scored_count':2})):
                    ids,_,meta=rank_query_groups(db,'查询',groups,
                        [{'id':'c-a','version_id':'v-a'}],[])
                db.close()
                self.assertEqual(ids,['eg-b','eg-a'])
                self.assertNotIn('private-value',str(meta))

    def test_original_first_complete_window_is_a_soft_single_query_preference(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
        first=group('cw-original',kind='canonical_sentence_window',
                    canonical_anchor_id='c-a',canonical_anchor_rank=1)
        model_first=group('eg-model',start_char=1)
        with patch('local_knowledge.reranking.rerank_groups',return_value=(['eg-model','cw-original'],
                {'used':True,'reason':None,'scored_count':2})):
            _,seeds,meta=rank_query_groups(db,'查询',[first,model_first],
                [{'id':'c-a','version_id':'v-a'}],[])
        db.close()
        self.assertEqual(seeds,[])
        self.assertEqual(meta['preferred_original_group_ids'],['cw-original'])
        self.assertEqual(meta['original_canonical_preference_count'],1)

    def test_unscored_original_window_cannot_become_required_seed(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE document_versions(id,parsed_text,parsed_sha256)')
        db.execute("INSERT INTO document_versions VALUES('v-a','公开原文','a')")
        first=group('cw-original',kind='canonical_sentence_window',
                    canonical_anchor_id='c-a',canonical_anchor_rank=1)
        with patch('local_knowledge.reranking.rerank_groups',return_value=(['eg-model'],
                {'used':True,'reason':'rerank_partial','scored_count':1})):
            _,seeds,meta=rank_query_groups(db,'查询',[first,group('eg-model',start_char=1)],
                [{'id':'c-a','version_id':'v-a'}],[])
        db.close()
        self.assertEqual(seeds,[])
        self.assertEqual(meta['original_canonical_preference_count'],0)


if __name__=='__main__':unittest.main()
