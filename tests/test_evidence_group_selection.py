"""纯合成原子组反例；不访问模型、网络或正式题集。"""
import copy
import json
import unittest

from local_knowledge.evidence_group_selection import (
    POLICY, PREFERENCE_POLICY, ORIGINAL_GEOMETRY_POLICY, SelectionError,
    fuse_group_candidates, plan_evidence_groups)


def core(gid, ids, *, score=-1.0, facets=(), **changes):
    value = dict(group_id=gid, kind='sentence_core', core=True, complete=True,
                 citation_ids=list(ids), score=score, facets=list(facets),
                 version_id='v1', boundary_key='public-boundary',
                 start_char=0, end_char=100, child_ids=['child-'+gid], parent_ids=['parent-'+gid])
    value.update(changes)
    return value


def row(cid, **changes):
    value = dict(id=cid, version_id='v1', start_char=0, end_char=100,
                 paragraph='chars:0-100', product_id=None, share_class_id=None,
                 channel_id=None, page=None, data_on=None, facets=['general'],
                 conditions=[], source_id='source1', parser_id='parser1', shape='market')
    value.update(changes)
    return value


def matches(ids, facets=()):
    return [(-1.0, cid, set(facets)) for cid in ids]


def original_geometry_fixture():
    """实际 producer 生成的两种等价表示；只有合成原文与 metadata。"""
    from local_knowledge.retrieval_units import build_units
    from local_knowledge.structure_index import _core_records, _canonical_sentence_windows
    text = 'Records must be supplied together before assessment.'
    rows = [row('a', end_char=20, paragraph='chars:0-20', excerpt=text[:20]),
            row('b', start_char=20, end_char=len(text),
                paragraph=f'chars:20-{len(text)}', excerpt=text[20:])]
    built = build_units(text, rows, version_id='v1')
    cores, _ = _core_records(text, built)
    sentence = dict(cores[0], score=-1.0)
    windows = _canonical_sentence_windows(text, rows, [sentence], {'a', 'b'})
    window = next(value for value in windows if value['canonical_anchor_id'] == 'b')
    return rows, sentence, window


class EvidenceGroupSelectionTests(unittest.TestCase):
    def plan(self, groups, ids=None, *, limit=8, **changes):
        ids = list(dict.fromkeys(cid for g in groups for cid in g['citation_ids'])) if ids is None else ids
        return plan_evidence_groups(matches(ids), groups, eligible_ids=set(ids), limit=limit, **changes)

    def test_minimal_sentence_core_is_atomic_and_has_no_singleton_escape(self):
        group = core('cross', ['a','b'])
        selected, diagnostic = self.plan([group], limit=1)
        self.assertEqual(selected, [])
        self.assertEqual(diagnostic['candidate_count'], 1)
        self.assertEqual(diagnostic['budget_rejected_groups'], 1)
        self.assertEqual(diagnostic['incomplete_accepted_groups'], 0)
        self.assertEqual(self.plan([group], limit=2)[0], ['a','b'])

    def test_unused_overlapping_candidate_is_not_accepted_context_gap(self):
        groups = [core('a-single', ['a'], score=-2), core('ab-cross', ['a','b'], score=-1)]
        selected, diagnostic = self.plan(groups, limit=1)
        self.assertEqual(selected, ['a'])
        self.assertEqual(diagnostic['accepted_group_count'], 1)
        self.assertEqual(diagnostic['incomplete_accepted_groups'], 0)
        self.assertEqual(diagnostic['unaccepted_partial_groups'], 1)
        self.assertEqual(diagnostic['accepted_groups'][0]['group_id'], 'a-single')

    def test_minimal_child_mapping_does_not_expand_to_whole_parent(self):
        group = core('middle', ['b'], parent_ids=['parent-containing-a-b-c'])
        # Canonical a is strongest in this fixture. The child receives no forced
        # full-parent expansion; with its public seed b, exactly b is feasible.
        selected, diagnostic = self.plan([group], ids=['a','b','c'], limit=1, seed_ids=['b'])
        self.assertEqual(selected, ['b'])
        self.assertEqual(diagnostic['incomplete_accepted_groups'], 0)

    def test_facet_seed_cannot_insert_only_one_crossing_citation(self):
        selected, diagnostic = plan_evidence_groups(matches(['a','b'], ['risk']),
            [core('risk-cross', ['a','b'])], eligible_ids={'a','b'}, limit=1, facets=['risk'])
        self.assertEqual(selected, [])
        self.assertEqual(diagnostic['uncovered_facets'], ['risk'])
        self.assertEqual(diagnostic['incomplete_accepted_groups'], 0)

    def test_facet_plan_preserves_feasible_alternative_instead_of_greedy_seed(self):
        groups = [core('expensive-risk', ['a','b'], score=-3),
                  core('cheap-risk', ['c'], score=-2), core('fees', ['c','d'], score=-1)]
        ranked = [(-3,'a',{'risk'}),(-2,'b',{'risk'}),(-1,'c',{'risk','fees'}),(0,'d',{'fees'})]
        selected, diagnostic = plan_evidence_groups(ranked, groups,
            eligible_ids={'a','b','c','d'}, limit=2, facets=['risk','fees'])
        self.assertEqual(set(selected), {'c','d'})
        self.assertEqual(diagnostic['uncovered_facets'], [])

    def test_institution_seeds_are_planned_as_whole_groups(self):
        groups = [core('bank-one', ['a','b']), core('bank-two', ['c'], score=-0.5)]
        selected, diagnostic = self.plan(groups, limit=3, seed_ids=['a','c'])
        self.assertEqual(set(selected), {'a','b','c'})
        self.assertEqual(diagnostic['uncovered_seed_ids'], [])
        selected, diagnostic = self.plan(groups, limit=1, seed_ids=['a','c'])
        self.assertEqual(selected, ['c'])
        self.assertEqual(diagnostic['uncovered_seed_ids'], ['a'])

    def test_required_enumeration_context_consumes_budget_atomically(self):
        group = core('enumerated', ['a','b','c'], kind='required_context', score=-200)
        selected, diagnostic = plan_evidence_groups(matches(['a','b','c']), [],
            context_groups=[group], eligible_ids={'a','b','c'}, limit=2, seed_ids=['a','b','c'])
        self.assertEqual(selected, [])
        self.assertEqual(diagnostic['uncovered_seed_ids'], ['a','b','c'])
        self.assertEqual(diagnostic['rejected_groups'][0]['rank_positions']['sentence'], None)
        self.assertEqual(plan_evidence_groups(matches(['a','b','c']), [], context_groups=[group],
            eligible_ids={'a','b','c'}, limit=3, seed_ids=['a','b','c'])[0], ['a','b','c'])

    def test_all_canonical_conditions_are_strict_group_boundaries(self):
        changes = dict(version_id='v2', product_id='product2', share_class_id='share2',
                       channel_id='channel2', page=2, data_on='2026-01-02', facets=['risk'],
                       conditions=['仅新客户'], paragraph='explicit-section', parser_id='other',
                       source_id='other-source', shape='other-shape')
        for key, value in changes.items():
            with self.subTest(key=key):
                selected, diagnostic = self.plan([core('mixed', ['a','b'])],
                    canonical_rows=[row('a'),row('b', **{key:value})])
                self.assertEqual(selected, [])
                self.assertEqual(diagnostic['invalid_boundary_groups'], 1)

    def test_auto_char_locators_can_cross_without_weakening_explicit_locators(self):
        rows = [row('a'), row('b',start_char=100,end_char=200,paragraph='chars:100-200')]
        self.assertEqual(self.plan([core('cross',['a','b'],end_char=200)], canonical_rows=rows)[0], ['a','b'])
        rows[1]['paragraph']='chars:101-200'
        self.assertEqual(self.plan([core('cross',['a','b'])], canonical_rows=rows)[0], [])

    def test_declared_wrong_version_cannot_relabel_canonical_members(self):
        selected, diagnostic = self.plan([core('wrong',['a'],version_id='v2')], canonical_rows=[row('a')])
        self.assertEqual(selected, [])
        self.assertEqual(diagnostic['invalid_boundary_groups'], 1)

    def test_partial_eligibility_rejects_whole_core_and_no_singleton_bypass(self):
        selected, diagnostic = self.plan([core('cross',['a','b'])], ids=['a'])
        self.assertEqual(selected, [])
        self.assertEqual(diagnostic['ineligible_rejected_groups'], 1)

    def test_incomplete_long_sentence_is_refused_with_explicit_reason(self):
        selected, diagnostic = self.plan([core('long',['a'],complete=False)])
        self.assertEqual(selected, [])
        self.assertEqual(diagnostic['core_completion_rejected_groups'], 1)

    def test_complete_core_with_optional_neighbor_is_selected_whole(self):
        option = core('context-ab',['a','b'],kind='context_supplement',core=False,
                      required_group_id='core-a',end_char=200)
        group = core('core-a',['a'],context_options=[option])
        selected, diagnostic = self.plan([group], ids=['a','b'], limit=2, seed_ids=['a'])
        self.assertEqual(selected, ['a','b'])
        self.assertEqual(diagnostic['optional_context_citations'], 1)
        self.assertEqual(diagnostic['incomplete_accepted_groups'], 0)

    def test_neighbor_cannot_cross_boundary_or_exceed_bounded_span(self):
        for changes, reason in (({'boundary_key':'other'},'mixed_boundary'),
                                ({'version_id':'v2'},'mixed_boundary'),
                                ({'end_char':2000},'invalid_context'),
                                ({'required_group_id':'other'},'invalid_context'),
                                ({'start_char':1},'invalid_context')):
            with self.subTest(changes=changes):
                values=dict(kind='context_supplement',core=False,required_group_id='core-a',end_char=200)
                values.update(changes)
                option = core('ctx',['a','b'],**values)
                selected, diagnostic = self.plan([core('core-a',['a'],context_options=[option])],
                    ids=['a','b'], limit=2, seed_ids=['a'], canonical_rows=[row('a'),
                        row('b',page=2,start_char=100,end_char=200,paragraph='chars:100-200')])
                self.assertIn('a', selected)
                self.assertEqual(diagnostic['optional_context_citations'], 0)
                self.assertIn(reason, [trace['reason'] for trace in diagnostic['rejected_groups']])

    def test_optional_citation_allowance_is_global_and_cannot_grow_full_document(self):
        groups=[]
        for number in range(3):
            cid, neighbor = 'a'+str(number), 'b'+str(number)
            gid='core'+str(number)
            option=core('ctx'+str(number),[cid,neighbor],kind='context_supplement',core=False,
                        required_group_id=gid,end_char=200)
            groups.append(core(gid,[cid],context_options=[option]))
        selected, diagnostic = self.plan(groups, ids=['a0','a1','a2','b0','b1','b2'],
                                        seed_ids=['a0','a1','a2'])
        self.assertEqual(diagnostic['optional_context_citations'], 2)
        self.assertEqual(diagnostic['incomplete_accepted_groups'], 0)

    def test_low_rank_neighbor_can_complete_core_before_unrelated_core_without_evicting_seed(self):
        option=core('context-ab',['a','b'],kind='context_supplement',core=False,
                    required_group_id='anchor',end_char=200)
        groups=[core('anchor',['a'],score=-3,context_options=[option]),
                core('unrelated',['u'],score=-2),core('low-neighbor',['b'],score=-0.1),
                core('other-institution',['z'],score=-1)]
        selected, diagnostic=self.plan(groups,ids=['a','u','z','b'],limit=3,seed_ids=['a','z'])
        self.assertEqual(set(selected),{'a','b','z'})
        self.assertEqual(diagnostic['uncovered_seed_ids'],[])
        self.assertEqual(diagnostic['optional_context_citations'],1)
        self.assertNotIn('u',selected)

    def test_trace_is_bounded_and_contains_no_source_or_question(self):
        groups=[core('g'+str(i),['c'+str(i)], text='隐私原文',query='私密问题',
                     boundary_key='原文条件',start_char=i*100,end_char=i*100+100) for i in range(10)]
        for bound in (0,3):
            selected, diagnostic = self.plan(groups, limit=8, max_trace=bound)
            traces=diagnostic['accepted_groups']+diagnostic['rejected_groups']
            self.assertLessEqual(len(traces), bound)
            self.assertNotIn('隐私原文',json.dumps(diagnostic,ensure_ascii=False))
            self.assertNotIn('私密问题',json.dumps(diagnostic,ensure_ascii=False))
            self.assertNotIn('原文条件',json.dumps(diagnostic,ensure_ascii=False))
            self.assertGreater(diagnostic['trace_truncated_count'],0)
            self.assertEqual(len(selected),8)

    def test_duplicate_canonical_vote_keeps_real_rank_not_group_id_position(self):
        groups=[core('g'+str(i),['a'],score=-1) for i in range(50)]
        ranked=fuse_group_candidates(matches(['a','b']),groups,eligible_ids={'a','b'})
        copies=[g for g in ranked if g['kind']=='sentence_core']
        self.assertTrue(all(g['rank_positions']=={'canonical':1,'sentence':1} for g in copies))
        self.assertEqual(len({g['fused_score'] for g in copies}),1)

    def test_two_views_do_not_make_weak_market_outrank_strong_plain(self):
        ranked=fuse_group_candidates(matches(['plain','market']),[core('market-group',['market'])],
                                     eligible_ids={'plain','market'})
        self.assertEqual(ranked[0]['citation_ids'],('plain',))
        ranked=fuse_group_candidates(matches(['market','plain']),[core('market-group',['market'])],
                                     eligible_ids={'plain','market'})
        self.assertEqual(ranked[0]['citation_ids'],('market',))

    def test_required_context_never_uses_its_bm25_as_sentence_score(self):
        group=core('ctx',['a'],kind='required_context',score=-999999)
        ranked=fuse_group_candidates(matches(['a','b']),[],eligible_ids={'a','b'},context_groups=[group])
        self.assertEqual(ranked[0]['rank_positions'],{'canonical':1,'sentence':None})

    def test_original_geometry_keeps_same_fact_prior_across_real_producer_formats(self):
        from local_knowledge.vectors import RRF_K
        rows, sentence, window = original_geometry_fixture()
        original = copy.deepcopy([sentence, window])
        self.assertNotEqual(sentence['boundary_key'], window['boundary_key'])
        ranked = fuse_group_candidates(matches(['a', 'b']), [sentence, window],
            eligible_ids={'a', 'b'}, canonical_rows=rows)
        by_id = {group['group_id']: group for group in ranked}
        actual_window = by_id[window['group_id']]
        actual_sentence = by_id[sentence['group_id']]
        self.assertEqual(actual_window['rank_positions'], {'canonical':2, 'sentence':None})
        self.assertEqual(actual_window['canonical_anchor_rank'], 2)
        self.assertEqual(actual_sentence['rank_positions'], {'canonical':1, 'sentence':1})
        for group in ranked:
            self.assertEqual(group['original_geometry_policy'], ORIGINAL_GEOMETRY_POLICY)
            self.assertEqual(group['original_geometry_rank_positions'], {'canonical':1, 'sentence':1})
            self.assertAlmostEqual(group['fused_score'], -1 / (RRF_K + 1))
        self.assertEqual([sentence, window], original)

    def test_original_geometry_duplicate_variants_add_no_branch_vote_to_their_fact(self):
        rows, sentence, window = original_geometry_fixture()
        once = fuse_group_candidates(matches(['a', 'b']), [sentence, window],
            eligible_ids={'a', 'b'}, canonical_rows=rows)
        copies = [dict(sentence, group_id=f'copy-{index}') for index in range(50)]
        repeated = fuse_group_candidates(matches(['a', 'b']), [sentence, window, *copies],
            eligible_ids={'a', 'b'}, canonical_rows=rows)
        expected = next(group['fused_score'] for group in once if group['group_id'] == window['group_id'])
        self.assertEqual(len(repeated), 52)
        for group in repeated:
            self.assertEqual(group['fused_score'], expected)
            self.assertEqual(group['original_geometry_rank_positions'], {'canonical':1, 'sentence':1})

    def test_original_geometry_different_span_cannot_lend_its_strong_sentence_vote(self):
        rows, sentence, window = original_geometry_fixture()
        smaller = dict(sentence, group_id='smaller', citation_ids=['a'],
                       end_char=20, score=-2.0)
        ranked = fuse_group_candidates(matches(['a', 'b']), [sentence, window, smaller],
            eligible_ids={'a', 'b'}, canonical_rows=rows)
        by_id = {group['group_id']: group for group in ranked}
        self.assertEqual(by_id[window['group_id']]['original_geometry_rank_positions'],
                         {'canonical':1, 'sentence':2})
        self.assertEqual(by_id['smaller']['original_geometry_rank_positions'],
                         {'canonical':1, 'sentence':1})
        self.assertNotEqual(by_id[window['group_id']]['fused_score'], by_id['smaller']['fused_score'])

    def test_original_geometry_different_map_or_version_cannot_lend_votes(self):
        rows, sentence, window = original_geometry_fixture()
        other_rows = [dict(value, id='other-' + value['id'], version_id='v2') for value in rows]
        from local_knowledge.retrieval_units import build_units
        from local_knowledge.structure_index import _core_records
        text = ''.join(value['excerpt'] for value in other_rows)
        other_cores, _ = _core_records(text, build_units(text, other_rows, version_id='v2'))
        other = dict(other_cores[0], score=-2.0)
        ranked = fuse_group_candidates(matches(['other-a', 'other-b', 'a', 'b']),
            [sentence, window, other], eligible_ids={'a', 'b', 'other-a', 'other-b'},
            canonical_rows=rows + other_rows)
        by_id = {group['group_id']: group for group in ranked}
        self.assertEqual(by_id[window['group_id']]['original_geometry_rank_positions'],
                         {'canonical':3, 'sentence':2})
        self.assertEqual(by_id[other['group_id']]['original_geometry_rank_positions'],
                         {'canonical':1, 'sentence':1})

    def test_original_geometry_unknown_boundary_keeps_own_vote_without_being_removed(self):
        from local_knowledge.vectors import RRF_K
        rows, sentence, window = original_geometry_fixture()
        unknown = dict(sentence, boundary_key='unknown-boundary')
        ranked = fuse_group_candidates(matches(['a', 'b']), [unknown, window],
            eligible_ids={'a', 'b'}, canonical_rows=rows)
        by_id = {group['group_id']: group for group in ranked}
        self.assertIsNone(by_id[unknown['group_id']]['rejection'])
        self.assertNotIn('original_geometry_rank_positions', by_id[unknown['group_id']])
        self.assertAlmostEqual(by_id[unknown['group_id']]['fused_score'], -1 / (RRF_K + 1))
        self.assertAlmostEqual(by_id[window['group_id']]['fused_score'], -1 / (RRF_K + 2))
        self.assertEqual(by_id[window['group_id']]['original_geometry_rank_positions'],
                         {'canonical':2, 'sentence':None})

    def test_original_geometry_missing_metadata_or_bounds_keeps_own_vote(self):
        from local_knowledge.vectors import RRF_K
        rows, sentence, window = original_geometry_fixture()
        for candidate, canonical_rows in (
                (sentence, []),
                (dict(sentence, start_char=None, end_char=None), rows),
                (dict(sentence, boundary_key=None), rows),
                (dict(sentence, version_id=None), rows),
                (sentence, [{key:value for key,value in member.items() if key != 'source_id'} for member in rows])):
            with self.subTest(candidate=candidate, rows=canonical_rows):
                ranked = fuse_group_candidates(matches(['a', 'b']), [candidate, window],
                    eligible_ids={'a', 'b'}, canonical_rows=canonical_rows)
                by_id = {group['group_id']: group for group in ranked}
                self.assertIsNone(by_id[candidate['group_id']]['rejection'])
                self.assertNotIn('original_geometry_rank_positions', by_id[candidate['group_id']])
                self.assertAlmostEqual(by_id[window['group_id']]['fused_score'], -1 / (RRF_K + 2))

    def test_original_geometry_rejected_or_incomplete_map_never_lends_votes(self):
        from local_knowledge.vectors import RRF_K
        rows, sentence, window = original_geometry_fixture()
        for changes, reason in (({'rejection':'not_eligible'}, 'not_eligible'),
                                ({'complete':False}, 'core_completion_limit'),
                                ({'citation_ids':['a']}, 'invalid_span'),
                                ({'version_id':'other-version'}, 'mixed_boundary')):
            with self.subTest(changes=changes):
                refused = dict(sentence, **changes)
                ranked = fuse_group_candidates(matches(['a', 'b']), [refused, window],
                    eligible_ids={'a', 'b'}, canonical_rows=rows)
                by_id = {group['group_id']: group for group in ranked}
                self.assertEqual(by_id[refused['group_id']]['rejection'], reason)
                self.assertEqual(by_id[refused['group_id']]['fused_score'], 0.0)
                self.assertNotIn('original_geometry_rank_positions', by_id[refused['group_id']])
                self.assertAlmostEqual(by_id[window['group_id']]['fused_score'], -1 / (RRF_K + 2))

    def test_original_geometry_same_map_with_tampered_boundary_cannot_lend_votes(self):
        from local_knowledge.vectors import RRF_K
        rows, sentence, window = original_geometry_fixture()
        for boundary in (sentence['boundary_key'].replace('source1', 'other-source'),
                         window['boundary_key'][:-1] + ('0' if window['boundary_key'][-1] != '0' else '1'),
                         {'unknown':'boundary'}):
            with self.subTest(boundary=boundary):
                changed = dict(sentence, boundary_key=boundary)
                ranked = fuse_group_candidates(matches(['a', 'b']), [changed, window],
                    eligible_ids={'a', 'b'}, canonical_rows=rows)
                by_id = {group['group_id']: group for group in ranked}
                self.assertNotIn('original_geometry_rank_positions', by_id[changed['group_id']])
                self.assertAlmostEqual(by_id[window['group_id']]['fused_score'], -1 / (RRF_K + 2))

    def test_original_geometry_without_equivalent_keeps_original_scores_and_order(self):
        from local_knowledge.vectors import RRF_K
        rows, sentence, window = original_geometry_fixture()
        groups = [dict(sentence, group_id='a-core', citation_ids=['a'], end_char=20, score=-2.0),
                  dict(sentence, group_id='b-core', citation_ids=['b'], start_char=20, score=-1.0)]
        ranked = fuse_group_candidates(matches(['b', 'a']), groups,
            eligible_ids={'a', 'b'}, canonical_rows=rows)
        self.assertEqual([group['group_id'] for group in ranked], ['a-core', 'b-core'])
        expected = -((1 / (RRF_K + 1)) + (1 / (RRF_K + 2))) / 2
        for group in ranked:
            self.assertEqual(group['original_geometry_rank_positions'], group['rank_positions'])
            self.assertEqual(group['fused_score'], expected)

    def test_original_geometry_trace_is_bounded_and_scrubs_incoming_support(self):
        rows, sentence, window = original_geometry_fixture()
        poisoned = dict(sentence, original_geometry_policy=ORIGINAL_GEOMETRY_POLICY,
                        original_geometry_rank_positions={'private text':'私密原文','canonical':True},
                        query='私密问题')
        _, diagnostic = self.plan([poisoned, window], ids=['a', 'b'],
            limit=2, canonical_rows=rows, max_trace=1)
        self.assertEqual(diagnostic['original_geometry_policy'], ORIGINAL_GEOMETRY_POLICY)
        self.assertEqual(len(diagnostic['accepted_groups']), 1)
        trace = diagnostic['accepted_groups'][0]
        self.assertEqual(trace['original_geometry_rank_positions'], {'canonical':1, 'sentence':1})
        self.assertNotIn('私密', json.dumps(diagnostic, ensure_ascii=False))
        unknown = dict(poisoned, boundary_key='unknown-boundary')
        _, diagnostic = self.plan([unknown], ids=['a', 'b'], limit=2, canonical_rows=rows)
        self.assertTrue(all('original_geometry_rank_positions' not in trace
                            for trace in diagnostic['accepted_groups']))

    def test_group_order_is_deterministic_and_input_not_mutated(self):
        groups=[core('b',['b']),core('a',['a'])]
        original=copy.deepcopy(groups)
        left=self.plan(groups)[0]
        right=self.plan(list(reversed(groups)),ids=['b','a'])[0]
        self.assertEqual(left,right)
        self.assertEqual(groups,original)

    def test_span_missing_overlapping_canonical_id_is_rejected_before_fallback(self):
        rows=[row('a'),row('b',start_char=100,end_char=200,paragraph='chars:100-200')]
        wrong=core('wrong-map',['a'],end_char=200)
        selected,diagnostic=self.plan([wrong],ids=['a','b'],canonical_rows=rows,
                                     limit=2,seed_group_ids=['wrong-map'])
        self.assertNotIn('a',selected)
        self.assertEqual(diagnostic['uncovered_seed_group_ids'],['wrong-map'])
        self.assertEqual(diagnostic['invalid_span_groups'],1)
        self.assertEqual(diagnostic['incomplete_accepted_groups'],0)
        self.assertEqual(diagnostic['rejected_groups'][0]['reason'],'invalid_span')

    def test_span_gap_and_outside_coverage_cannot_be_accepted_as_complete(self):
        for changes in ({'start_char':120,'end_char':200,'paragraph':'chars:120-200'},
                        {'start_char':100,'end_char':180,'paragraph':'chars:100-180'}):
            with self.subTest(changes=changes):
                rows=[row('a'),row('b',**changes)]
                selected,diagnostic=self.plan([core('gap',['a','b'],end_char=200)],
                                             canonical_rows=rows,limit=2)
                self.assertEqual(selected,[])
                self.assertEqual(diagnostic['invalid_span_groups'],1)

    def test_context_core_seed_reserves_its_whole_span_before_other_core(self):
        rows=[row('a'),row('b',start_char=100,end_char=200,paragraph='chars:100-200'),
              row('other',version_id='v2')]
        groups=[core('context-ab',['a','b'],kind='context_core',end_char=200),
                core('other',['other'],version_id='v2')]
        selected,diagnostic=self.plan(groups,ids=['other','a','b'],canonical_rows=rows,
            limit=2,ranking_ids=['other','context-ab'],seed_group_ids=['context-ab'])
        self.assertEqual(selected,['a','b'])
        self.assertEqual(diagnostic['uncovered_seed_group_ids'],[])
        self.assertEqual(diagnostic['incomplete_accepted_groups'],0)

    def test_context_core_exceeds_span_limit_is_refused_without_singleton_escape(self):
        rows=[row('a',end_char=900,paragraph='chars:0-900'),
              row('b',start_char=900,end_char=1800,paragraph='chars:900-1800')]
        selected,diagnostic=self.plan([core('context-ab',['a','b'],kind='context_core',end_char=1800)],
                                      canonical_rows=rows,limit=2)
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['core_completion_rejected_groups'],1)

    def test_fixed_prior_rejection_survives_renormalization(self):
        selected,diagnostic=self.plan([core('refused',['a'],rejection='not_eligible')])
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['ineligible_rejected_groups'],1)
        with self.assertRaises(SelectionError):
            self.plan([core('invalid',['a'],rejection=['任意文本'])])

    def test_canonical_window_uses_true_anchor_rank_and_pays_whole_mapping(self):
        rows=[row('a'),row('b',start_char=100,end_char=200,paragraph='chars:100-200')]
        window=core('window',['a','b'],kind='canonical_sentence_window',canonical_anchor_id='b',end_char=200)
        ranked=fuse_group_candidates(matches(['a','b']),[window],eligible_ids={'a','b'},canonical_rows=rows)
        self.assertEqual(ranked[0]['rank_positions'],{'canonical':2,'sentence':None})
        self.assertEqual(ranked[0]['canonical_anchor_rank'],2)
        self.assertEqual(self.plan([window],ids=['a','b'],canonical_rows=rows,limit=1)[0],[])
        selected,diagnostic=self.plan([window],ids=['a','b'],canonical_rows=rows,limit=2,
                                      seed_group_ids=['window'])
        self.assertEqual(selected,['a','b'])
        self.assertEqual(diagnostic['uncovered_seed_group_ids'],[])
        self.assertEqual(diagnostic['incomplete_accepted_groups'],0)
        self.assertEqual(diagnostic['accepted_groups'][0]['canonical_anchor_id'],'b')

    def test_canonical_window_cannot_claim_unmapped_anchor_or_oversized_complete_core(self):
        with self.assertRaises(SelectionError):
            self.plan([core('wrong-anchor',['a'],kind='canonical_sentence_window',canonical_anchor_id='b')])
        for ids,end in ((['a'],6401),([f'a{i}' for i in range(9)],900)):
            with self.subTest(ids=ids,end=end):
                selected,diagnostic=self.plan([core('large',ids,kind='canonical_sentence_window',
                    canonical_anchor_id=ids[0],end_char=end)])
                self.assertEqual(selected,[])
                self.assertEqual(diagnostic['core_completion_rejected_groups'],1)

    def test_source_scope_cannot_override_window_full_eligibility(self):
        window=core('scoped',['a','b'],kind='canonical_sentence_window',canonical_anchor_id='a',
                     end_char=200,source_scope_rank=1)
        selected,diagnostic=self.plan([window],ids=['a'],limit=8)
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['ineligible_rejected_groups'],1)

    def test_window_invalid_span_reason_cannot_be_hidden_by_incomplete_flag(self):
        window=core('broken',['a'],kind='canonical_sentence_window',canonical_anchor_id='a',
                     complete=False,rejection='invalid_span')
        selected,diagnostic=self.plan([window])
        self.assertEqual(selected,[])
        self.assertEqual(diagnostic['invalid_span_groups'],1)
        self.assertEqual(diagnostic['core_completion_rejected_groups'],0)

    def test_soft_preference_cannot_displace_original_facets_seeds_or_required_context(self):
        ranked=[(-1,'a',{'fees'}),(-1,'b',{'fees'}),
                (-1,'c',{'fees'}),(-1,'d',{'liquidity'})]
        rows=[row('a',facets=['fees']),row('b',start_char=100,end_char=200,
                  paragraph='chars:100-200',facets=['fees']),
              row('c',version_id='v2',facets=['fees']),
              row('d',version_id='v3',facets=['liquidity'])]
        groups=[core('gnew',['a','b'],kind='canonical_sentence_window',
                     canonical_anchor_id='a',end_char=200),
                core('gold',['c'],kind='required_context',version_id='v2'),
                core('ginst',['d'],kind='required_context',version_id='v3')]
        for requirements in ({'facets':['fees','liquidity']},
                             {'seed_ids':['c','d']},
                             {'seed_group_ids':['gold','ginst']}):
            with self.subTest(requirements=requirements):
                options=dict(eligible_ids={'a','b','c','d'},limit=2,
                             ranking_ids=['gnew','gold','ginst'],canonical_rows=rows)
                baseline,old=plan_evidence_groups(ranked,groups,**options,**requirements)
                selected,diagnostic=plan_evidence_groups(ranked,groups,**options,
                    preferred_group_ids=['gnew'],**requirements)
                self.assertEqual(baseline,['c','d'])
                self.assertEqual(selected,baseline)
                for field in ('uncovered_facets','uncovered_seed_ids','uncovered_seed_group_ids',
                              'budget_rejected_groups','incomplete_accepted_groups'):
                    self.assertEqual(diagnostic[field],old[field])
                self.assertEqual(diagnostic['preferred_group_attempt_count'],1)
                self.assertEqual(diagnostic['preferred_group_accepted_count'],0)
                self.assertEqual(diagnostic['preferred_group_rejected_count'],1)
                trace=next(g for g in diagnostic['rejected_groups'] if g['group_id']=='gnew')
                self.assertEqual(trace['reason'],'preferred_budget_exceeded')
                self.assertEqual(trace['budget_before'],0)

    def test_preference_breaks_hard_coverage_tie_before_ranking_cost(self):
        window=core('window',['a','b'],kind='canonical_sentence_window',
                    canonical_anchor_id='a',end_char=200)
        groups=[core('model',['m'],version_id='v2'),window]
        ranked=matches(['m','a','b'],['general'])
        options=dict(eligible_ids={'m','a','b'},limit=2,facets=['general'],
                     ranking_ids=['model','window'])
        self.assertEqual(plan_evidence_groups(ranked,groups,**options)[0],['m'])
        selected,diagnostic=plan_evidence_groups(ranked,groups,**options,
                                                preferred_group_ids=['window'])
        self.assertEqual(selected,['a','b'])
        self.assertEqual(diagnostic['uncovered_facets'],[])
        self.assertEqual(diagnostic['preferred_group_accepted_count'],1)
        self.assertEqual(diagnostic['preferred_group_rejected_count'],0)
        self.assertEqual(diagnostic['accepted_group_count'],1)
        self.assertEqual(diagnostic['accepted_groups'][0]['reason'],'required_coverage')

    def test_soft_beam_pruning_cannot_lose_baseline_facets_seeds_or_groups(self):
        # 67合法组/68原引文；中间128-state剪枝曾留下昂贵偏好，丢掉最后硬要求。
        ranked=[(-100.,'a',{'fees'}),(-99.,'b',{'fees'}),(-98.,'c',{'fees'})]
        groups=[core('gnew',['a','b'],kind='canonical_sentence_window',
                     canonical_anchor_id='a',end_char=200),
                core('gold',['c'],version_id='v-old')]
        rows=[row('a',facets=['fees']),row('b',start_char=100,end_char=200,
                  paragraph='chars:100-200',facets=['fees']),
              row('c',version_id='v-old',facets=['fees'])]
        for facet,prefix in [('risk','r'),('liquidity','l')]:
            for index in range(32):
                cid=f'{prefix}-{index:02}'
                ranked.append((-95.+len(ranked),cid,{facet}))
                groups.append(core('g-'+cid,[cid],version_id='v-'+cid))
                rows.append(row(cid,version_id='v-'+cid,facets=[facet]))
        ranked.append((1.,'s',{'strategy'}))
        groups.append(core('g-strategy',['s'],version_id='v-strategy'))
        rows.append(row('s',version_id='v-strategy',facets=['strategy']))
        options=dict(eligible_ids={value[1] for value in ranked},limit=4,
                     ranking_ids=[group['group_id'] for group in groups],canonical_rows=rows)
        fused=fuse_group_candidates(ranked,groups,eligible_ids=options['eligible_ids'],canonical_rows=rows)
        self.assertEqual(len(fused),67)
        self.assertTrue(all(group['rejection'] is None for group in fused))
        for requirements in ({'facets':['fees','risk','liquidity','strategy']},
                             {'facets':['fees','risk','liquidity'],'seed_ids':['s']},
                             {'facets':['fees','risk','liquidity'],'seed_group_ids':['g-strategy']}):
            with self.subTest(requirements=requirements):
                baseline,old=plan_evidence_groups(ranked,groups,**options,**requirements)
                selected,new=plan_evidence_groups(ranked,groups,**options,**requirements,
                                                  preferred_group_ids=['gnew'])
                self.assertEqual(baseline,['c','r-00','l-00','s'])
                self.assertEqual(selected,baseline)
                for field in ('uncovered_facets','uncovered_seed_ids','uncovered_seed_group_ids'):
                    self.assertEqual(new[field],old[field])
                self.assertTrue(old['beam_pruned'])
                self.assertTrue(new['beam_pruned'])
                self.assertTrue(new['preference_plan_fallback'])
                self.assertEqual(new['preferred_group_accepted_count'],0)
                self.assertEqual(new['incomplete_accepted_groups'],0)
                trace=next(group for group in new['rejected_groups'] if group['group_id']=='gnew')
                self.assertEqual(trace['reason'],'preferred_budget_exceeded')

    def test_preference_guard_preserves_each_hard_token_not_only_count(self):
        from unittest.mock import patch
        # 有界搜索返回相同数量、不同硬项，不能把丢失原机构要求视为平局。
        groups=[core('old',['a'],facets=['fees']),core('replacement',['b'],facets=['fees']),
                core('window',['c'],kind='canonical_sentence_window',canonical_anchor_id='c')]
        options=dict(eligible_ids={'a','b','c'},limit=1,facets=['fees'],
                     seed_ids=['a','b'],ranking_ids=['old','replacement','window'])
        with patch('local_knowledge.evidence_group_selection._beam_required_plan',
                   side_effect=[([0],True),([1],True)]):
            selected,diagnostic=plan_evidence_groups(matches(['a','b','c'],['fees']),
                groups,**options,preferred_group_ids=['window'])
        self.assertEqual(selected,['a'])
        self.assertEqual(diagnostic['uncovered_seed_ids'],['b'])
        self.assertTrue(diagnostic['preference_plan_fallback'])

    def test_feasible_soft_only_window_is_accepted_before_optional_context(self):
        option=core('option-ab',['a','b'],kind='context_supplement',core=False,
                    required_group_id='anchor',end_char=200)
        groups=[core('anchor',['a'],context_options=[option]),
                core('window',['c','d'],kind='canonical_sentence_window',
                     canonical_anchor_id='c',version_id='v2',end_char=200)]
        rows=[row('a'),row('b',start_char=100,end_char=200,paragraph='chars:100-200'),
              row('c',version_id='v2'),row('d',version_id='v2',start_char=100,
                  end_char=200,paragraph='chars:100-200')]
        selected,diagnostic=self.plan(groups,ids=['a','b','c','d'],limit=3,
            seed_ids=['a'],canonical_rows=rows,ranking_ids=['anchor','window'],
            preferred_group_ids=['window'])
        self.assertEqual(selected,['a','c','d'])
        self.assertEqual(diagnostic['optional_context_citations'],0)
        self.assertEqual(diagnostic['uncovered_seed_ids'],[])
        self.assertEqual(diagnostic['preferred_group_accepted_count'],1)
        self.assertEqual([g['reason'] for g in diagnostic['accepted_groups']],
                         ['required_coverage','preferred_coverage'])

    def test_preferred_window_is_rejected_whole_with_one_remaining_slot(self):
        groups=[core('anchor',['a']),core('window',['c','d'],
                kind='canonical_sentence_window',canonical_anchor_id='c',
                version_id='v2',end_char=200)]
        selected,diagnostic=self.plan(groups,limit=2,seed_ids=['a'],
            ranking_ids=['window','anchor'],preferred_group_ids=['window'])
        self.assertEqual(selected,['a'])
        self.assertEqual(diagnostic['remaining_budget'],1)
        self.assertEqual(diagnostic['preferred_group_rejected_count'],1)
        self.assertEqual(diagnostic['uncovered_seed_ids'],[])
        self.assertEqual(diagnostic['uncovered_seed_group_ids'],[])
        self.assertEqual(diagnostic['incomplete_accepted_groups'],0)
        trace=next(g for g in diagnostic['rejected_groups'] if g['group_id']=='window')
        self.assertEqual(trace['reason'],'preferred_budget_exceeded')
        self.assertEqual(trace['citation_ids'],['c','d'])
        self.assertEqual(trace['budget_before'],1)

    def test_preference_policy_and_counters_exist_without_changing_original_policy(self):
        selected,diagnostic=self.plan([core('plain',['a'])])
        self.assertEqual(selected,['a'])
        self.assertEqual(diagnostic['policy'],POLICY)
        self.assertEqual(diagnostic['preference_policy'],PREFERENCE_POLICY)
        for field in ('preferred_group_attempt_count','preferred_group_accepted_count',
                      'preferred_group_rejected_count'):
            self.assertEqual(diagnostic[field],0)

    def test_preference_requires_one_known_scored_valid_canonical_window(self):
        window=core('window',['a','b'],kind='canonical_sentence_window',
                    canonical_anchor_id='a',end_char=200)
        for preferred in (None,'window',{'window'},['window','window'],
                          ['window','other'],[3],['bad id'],['unknown']):
            with self.subTest(preferred=preferred),self.assertRaises(SelectionError):
                self.plan([window],ranking_ids=['window'],preferred_group_ids=preferred)
        for ranking in ([],['other']):
            with self.subTest(ranking=ranking),self.assertRaises(SelectionError):
                self.plan([window,core('other',['c'])],ranking_ids=ranking,
                          preferred_group_ids=['window'])
        for kind in ('sentence_core','canonical_singleton','required_context',
                     'context_core','context_supplement'):
            with self.subTest(kind=kind),self.assertRaises(SelectionError):
                other=core('other',['a','b'],kind=kind,
                           core=kind!='context_supplement',end_char=200)
                self.plan([other],ranking_ids=['other'],preferred_group_ids=['other'])
        for changes in ({'complete':False},{'rejection':'not_eligible'},
                        {'rejection':'invalid_span'},{'end_char':6401}):
            with self.subTest(changes=changes),self.assertRaises(SelectionError):
                refused=dict(window,**changes)
                self.plan([refused],ranking_ids=['window'],preferred_group_ids=['window'])
        with self.assertRaises(SelectionError):
            self.plan([window],ids=['a'],ranking_ids=['window'],preferred_group_ids=['window'])
        with self.assertRaises(SelectionError):
            self.plan([dict(window,citation_ids=['a'])],ids=['a','b'],
                canonical_rows=[row('a'),row('b',start_char=100,end_char=200,
                    paragraph='chars:100-200')],ranking_ids=['window'],preferred_group_ids=['window'])

    def test_preference_trace_remains_bounded_and_contains_only_public_metadata(self):
        window=core('window',['a','b'],kind='canonical_sentence_window',
                    canonical_anchor_id='a',end_char=200,text='私密原文',query='私密问题')
        for bound in (0,1):
            with self.subTest(bound=bound):
                selected,diagnostic=self.plan([window],limit=1,max_trace=bound,
                    ranking_ids=['window'],preferred_group_ids=['window'])
                self.assertEqual(selected,[])
                self.assertEqual(diagnostic['preferred_group_rejected_count'],1)
                self.assertLessEqual(len(diagnostic['accepted_groups'])+
                                     len(diagnostic['rejected_groups']),bound)
                self.assertNotIn('私密',json.dumps(diagnostic,ensure_ascii=False))
                self.assertEqual(diagnostic['trace_truncated_count'],1-bound)

    def test_empty_sentence_branch_preserves_canonical_order(self):
        selected, diagnostic = self.plan([],ids=['c','a','b'])
        self.assertEqual(selected,['c','a','b'])
        self.assertEqual(diagnostic['incomplete_accepted_groups'],0)

    def test_invalid_shapes_fail_closed_with_fixed_error(self):
        invalid=({'limit':True},{'limit':0},{'max_trace':True},{'facets':[{}]},
                 {'seed_ids':['not-eligible']},{'eligible_ids':['a']})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(SelectionError):
                values=dict(eligible_ids={'a'},limit=1)
                values.update(changes)
                plan_evidence_groups(matches(['a']),[core('valid',['a'])],**values)
        for group in (core('bad id',['a']),core('nan',['a'],score=float('nan')),
                      core('bool',['a'],complete=1),core('range',['a'],end_char=0),
                      core('duplicate',['a','a'])):
            with self.subTest(group=group['group_id']), self.assertRaises(SelectionError):
                self.plan([group],ids=['a'])

    def test_option_group_identity_cannot_alias_another_candidate(self):
        option=core('second',['a','b'],kind='context_supplement',core=False,
                    required_group_id='first',end_char=200)
        with self.assertRaises(SelectionError):
            self.plan([core('first',['a'],context_options=[option]),core('second',['b'])])


if __name__ == '__main__':
    unittest.main()
