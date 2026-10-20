"""纯合成形态软线索反例；不读金融题、不运行模型/API/数据库。

fixture 的 public/real 字段只用于验证既有准入合同，不是实际数据真实性
证明。原件验证在此检验 parsed/excerpt hash；raw 文件验证仍属上游消费链。
"""
import builtins
import copy
import json
import socket
import time
import unittest
from unittest.mock import patch

from local_knowledge import answer_form_preference as preference
from local_knowledge import reranking
from local_knowledge.evidence_group_selection import plan_evidence_groups
from test_local_reranking import combined, fixture


NEGATIVE_QUERY = '如果材料没有补齐，可采取哪些措施？'
NEGATIVE_BODY = 'If records are not supplied, options include a reminder and a review.'
PAIRED_QUERY = 'How many volunteers enrolled and how many volunteers declined?'
PAIRED_BODY = 'The count is 19191-17171 volunteers.'


def form_fixture(parts=None, *, separator=' '):
    groups, rows, parsed = fixture(parts=parts or [NEGATIVE_BODY], separator=separator)
    window = combined(groups)
    window.update(group_id='cw-local-form', kind='canonical_sentence_window',
                  canonical_anchor_id=rows[0]['id'], canonical_anchor_rank=1)
    records = [{'group_id':window['group_id'], 'rank':1,
                'query_view_rank_positions':{'original':1}}]
    return [window], rows, parsed, records


class AnswerFormPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.model_guard = patch.object(reranking, 'get_reranker', side_effect=AssertionError('model forbidden'))
        self.network_mock = self.network_guard.start()
        self.model_mock = self.model_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.addCleanup(self.model_guard.stop)

    def tearDown(self):
        self.assertEqual(self.network_mock.call_count, 0)
        self.assertEqual(self.model_mock.call_count, 0)

    def call(self, data=None, *, query=NEGATIVE_QUERY, translated=None, deadline=None):
        groups, rows, parsed, records = data or form_fixture()
        return preference.preferred_answer_form(query, translated, groups, rows, parsed,
                                                records, deadline=time.monotonic()+30 if deadline is None else deadline)

    def assert_no_preference(self, result, *, reason=None):
        ids, diagnostic = result
        self.assertEqual(ids, [])
        self.assertFalse(diagnostic['used'])
        self.assertEqual(diagnostic['authority'], 'retrieval_hint_only')
        self.assertIsNone(diagnostic['preferred_group_id'])
        self.assertEqual(diagnostic['witnesses'], [])
        self.assertEqual(diagnostic['matched_group_count'], 0)
        if reason is not None:
            self.assertEqual(diagnostic['reason'], reason)

    def test_negative_condition_and_list_prefers_whole_window_without_mutating_original_shape(self):
        data = form_fixture(['If records are not supplied,', 'options include a reminder and a review.'])
        before = copy.deepcopy(data)
        for query in (NEGATIVE_QUERY, 'If records are not supplied, which measures are available?'):
            with self.subTest(query=query):
                ids, diagnostic = self.call(data, query=query)
                self.assertEqual(ids, ['cw-local-form'])
                self.assertTrue(diagnostic['used'])
                self.assertEqual(diagnostic['form'], 'negative_condition_options')
                self.assertEqual(diagnostic['authority'], 'retrieval_hint_only')
                self.assertEqual({item['shape'] for item in diagnostic['witnesses']},
                                 {'negative_condition','explicit_enumeration'})
                self.assertEqual(data, before)
                self.assertEqual(data[0][0]['citation_ids'], [row['id'] for row in data[1]])
                self.assertTrue(all(row['shape']=='market-official-full-text-v1' for row in data[1]))

    def test_paired_numbers_are_literal_hint_with_no_answer_values_or_units_in_metadata(self):
        ids, diagnostic = self.call(form_fixture([PAIRED_BODY]), query=PAIRED_QUERY)
        self.assertEqual(ids, ['cw-local-form'])
        self.assertEqual(diagnostic['form'], 'paired_numeric')
        self.assertEqual(diagnostic['authority'], 'retrieval_hint_only')
        self.assertEqual(len(diagnostic['witnesses']), 1)
        encoded = json.dumps(diagnostic, ensure_ascii=False)
        for forbidden in ('19191','17171','volunteers',PAIRED_BODY,PAIRED_QUERY,
                          'graph_assertion','semantic_support','answer_value'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(set(diagnostic), {'policy','authority','used','form','matched_group_count',
                                         'preferred_group_id','witnesses','reason'})
        self.assertEqual(set(diagnostic['witnesses'][0]), {'shape','start_char','end_char'})

    def test_two_original_chinese_slots_use_only_literal_unit_in_existing_translation(self):
        ids, diagnostic = self.call(form_fixture([PAIRED_BODY]),
            query='有多少名志愿者报名，多少名志愿者退出？', translated=PAIRED_QUERY)
        self.assertEqual(ids, ['cw-local-form'])
        self.assertEqual(diagnostic['form'], 'paired_numeric')
        self.assert_no_preference(self.call(form_fixture([PAIRED_BODY]),
            query='有多少名志愿者报名，多少名志愿者退出？'), reason='request_form_unknown')

    def test_unknown_original_form_cannot_be_created_by_translation_or_injected_group_shape(self):
        data = form_fixture([PAIRED_BODY])
        data[0][0]['shape'] = 'paired_numeric'
        self.assert_no_preference(self.call(data, query='Explain the public summary.', translated=PAIRED_QUERY),
                                 reason='request_form_unknown')
        for query in ('How many volunteers enrolled?', 'List available measures.',
                      '如果材料已经补齐，可采取哪些措施？', '', None, '查'*501):
            with self.subTest(query=query):
                self.assert_no_preference(self.call(data, query=query), reason='request_form_unknown')

    def test_different_count_units_are_ambiguous_and_no_unit_is_guessed(self):
        queries = ('How many volunteers enrolled and how many teams participated?',
                   'How many active volunteers enrolled and how many volunteers declined?')
        for query in queries:
            with self.subTest(query=query):
                self.assert_no_preference(self.call(form_fixture([PAIRED_BODY]), query=query),
                                         reason='request_form_unknown')
        self.assert_no_preference(self.call(form_fixture([PAIRED_BODY]), query=PAIRED_QUERY,
            translated='How many teams participated and how many teams declined?'), reason='request_form_unknown')

    def test_explicit_count_prohibition_does_not_turn_a_date_request_into_a_pair_preference(self):
        english = [
            'What date was the meeting? Do not tell how many volunteers enrolled or how many volunteers declined.',
            'What date was the meeting? Do not say how many volunteers enrolled or how many volunteers declined.',
            'What date was the meeting? Do not report how many volunteers enrolled or how many volunteers declined.',
            'What date was the meeting? Do not answer how many volunteers enrolled or how many volunteers declined.',
            "What date was the meeting? Don't mention how many volunteers enrolled or how many volunteers declined.",
            'What date was the meeting? Never include how many volunteers enrolled or how many volunteers declined.',
            'What date was the meeting? Avoid reporting how many volunteers enrolled or how many volunteers declined.',
        ]
        chinese = [f'请只给日期，不要{verb}多少名志愿者报名或多少名志愿者退出。'
                   for verb in ('告诉','说','报告','回答','列出','给出','提及')]
        for query in english + chinese:
            with self.subTest(query=query):
                # 中文也提供已有字面计数单位；若无禁令守卫，实际会匹配pair，
                # 避免仅因缺译文单位而通过的空反例。
                self.assert_no_preference(self.call(form_fixture([PAIRED_BODY]),
                    query=query,translated=PAIRED_QUERY), reason='request_form_unknown')
        for query in (NEGATIVE_QUERY, 'If records are not supplied, what options are available?'):
            with self.subTest(positive_condition=query):
                ids,diagnostic=self.call(form_fixture(),query=query)
                self.assertEqual(ids,['cw-local-form'])
                self.assertEqual(diagnostic['form'],'negative_condition_options')
                self.assertEqual(diagnostic['authority'],'retrieval_hint_only')

    def test_unit_case_and_limited_plural_match_without_converting_or_interpreting_numbers(self):
        for body in ('The count is 7-2 VOLUNTEERS.', 'The count is 7–2 volunteer.',
                     'The count is 7—2 volunteers.', 'The count is 7:2 volunteers.'):
            with self.subTest(body=body):
                ids, diagnostic = self.call(form_fixture([body]), query=PAIRED_QUERY)
                self.assertEqual(ids, ['cw-local-form'])
                self.assertEqual(diagnostic['authority'], 'retrieval_hint_only')
                self.assertNotIn('7', json.dumps({key:value for key,value in diagnostic.items()
                                                if key!='witnesses'}))

    def test_pair_requires_literal_same_unit_and_explicit_pair_shape(self):
        for body in ('The count is 7-2 teams.', 'The count is 7-2.',
                     'There were 7 volunteers and 2 volunteers.', 'The count is 7/2 volunteers.'):
            with self.subTest(body=body):
                self.assert_no_preference(self.call(form_fixture([body]), query=PAIRED_QUERY),
                                         reason='scored_complete_window_form_missing')

    def test_negative_list_requires_condition_before_explicit_enumeration_without_cross_paragraph(self):
        bodies = ('If records are supplied, options include a reminder and a review.',
                  'If records are not supplied, a review follows.',
                  'Options include a reminder and a review. If records are not supplied, wait.',
                  'If records are not supplied,\noptions include a reminder and a review.',
                  'If records are not supplied, first wait. Then pause. Next stop. Options include a reminder and a review.',
                  'If records are not supplied, '+('x'*650)+' options include a reminder and a review.')
        for body in bodies:
            with self.subTest(body=body):
                self.assert_no_preference(self.call(form_fixture([body])),
                                         reason='scored_complete_window_form_missing')

    def test_no_scoring_record_or_wrong_group_record_never_grants_preference(self):
        for records in ([], [{'group_id':'cw-other', 'rank':1,
                             'query_view_rank_positions':{'original':1}}]):
            with self.subTest(records=records):
                data = form_fixture()
                data = (*data[:3], records)
                self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_only_successfully_scored_complete_canonical_window_can_be_preferred(self):
        for changes in ({'kind':'sentence_core'}, {'kind':'context_core'}, {'complete':False},
                        {'rejection':'not_eligible'}, {'core':False}, {'complete':1}):
            with self.subTest(changes=changes):
                data = form_fixture()
                data[0][0].update(changes)
                self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_invalid_rank_records_and_unknown_view_names_do_not_leak_or_gain_qualification(self):
        invalid = [None, [], '私密记录', {'group_id':'cw-local-form'},
                   {'group_id':'cw-local-form','rank':1,'query_view_rank_positions':{}},
                   {'group_id':'cw-local-form','rank':1,'query_view_rank_positions':{'私密问题':1}}]
        for rank in (True, 0, -1, 193, float('nan'), float('inf'), '1', {}):
            invalid.append({'group_id':'cw-local-form','rank':rank,
                            'query_view_rank_positions':{'original':1}})
            invalid.append({'group_id':'cw-local-form','rank':1,
                            'query_view_rank_positions':{'original':rank}})
        for record in invalid:
            with self.subTest(record=record):
                data = form_fixture()
                result = self.call((*data[:3], [record]))
                self.assert_no_preference(result, reason='scored_complete_window_form_missing')
                self.assertNotIn('私密', json.dumps(result[1],ensure_ascii=False))

    def test_parsed_body_and_each_full_window_excerpt_hash_are_rechecked(self):
        for change in ('parsed_hash','parsed_body','first_excerpt_hash','last_excerpt_hash',
                       'last_excerpt','row_parsed_hash'):
            with self.subTest(change=change):
                data = form_fixture(['If records are not supplied,', 'options include a reminder and a review.'])
                rows, parsed = data[1], data[2]
                record = parsed[rows[0]['version_id']]
                if change=='parsed_hash': record['parsed_sha256']='0'*64
                elif change=='parsed_body': record['parsed_text'] += 'Changed.'
                elif change=='first_excerpt_hash': rows[0]['excerpt_sha256']='0'*64
                elif change=='last_excerpt_hash': rows[-1]['excerpt_sha256']='0'*64
                elif change=='last_excerpt': rows[-1]['excerpt']='Changed excerpt.'
                else: rows[-1]['parsed_sha256']='0'*64
                self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_actual_public_original_permissions_are_rechecked(self):
        for key,value in (('visibility','private'),('data_origin','synthetic'),('status','candidate'),
                          ('enabled',0),('read_allowed',0),('outbound_allowed',0),('paused',1)):
            with self.subTest(key=key):
                data = form_fixture()
                data[1][0][key] = value
                self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_injected_candidate_text_or_verified_title_is_never_a_body_witness(self):
        data = form_fixture(['The original only describes the timetable.'])
        data[0][0].update(text=NEGATIVE_BODY,title=NEGATIVE_BODY,shape='negative_condition_options')
        data[1][0]['title'] = NEGATIVE_BODY
        data[2][data[1][0]['version_id']]['title'] = NEGATIVE_BODY
        self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_incomplete_map_cross_version_boundary_or_gap_is_not_repaired_by_shape(self):
        for change in ('missing_cid','version','boundary','gap'):
            with self.subTest(change=change):
                data = form_fixture(['If records are not supplied,', 'options include a reminder and a review.'])
                group, rows = data[0][0], data[1]
                if change=='missing_cid': group['citation_ids'] = [rows[0]['id']]
                elif change=='version': rows[-1]['version_id']='other-version'
                elif change=='boundary': rows[-1]['page']=2
                else: rows[-1]['start_char'] += 1
                self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_whole_window_limit_or_invalid_bounds_never_returns_a_partial_prefix_hint(self):
        for changes in ({'start_char':-1},{'start_char':True},{'end_char':0},
                        {'start_char':None,'end_char':None},{'end_char':6401}):
            with self.subTest(changes=changes):
                data = form_fixture()
                data[0][0].update(changes)
                self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')
        data = form_fixture(['If records are not supplied,']+['a segment']*7+
                            ['options include a reminder and a review.'])
        self.assertEqual(len(data[0][0]['citation_ids']),9)
        self.assert_no_preference(self.call(data), reason='scored_complete_window_form_missing')

    def test_duplicates_or_malformed_container_fail_closed(self):
        for change in ('duplicate_group','duplicate_cid','group_generator','row_generator',
                       'groups_none','rows_none','parsed_list'):
            with self.subTest(change=change):
                groups, rows, parsed, records = form_fixture()
                if change=='duplicate_group': groups += copy.deepcopy(groups)
                elif change=='duplicate_cid': rows += copy.deepcopy(rows)
                elif change=='group_generator': groups=iter(groups)
                elif change=='row_generator': rows=iter(rows)
                elif change=='groups_none': groups=None
                elif change=='rows_none': rows=None
                else: parsed=[]
                self.assert_no_preference(self.call((groups,rows,parsed,records)),
                                         reason='answer_form_input_invalid')

    def test_invalid_record_container_or_more_than_192_records_fails_closed(self):
        for records in (None, True, {}, '私密记录', set(), [{}]*193):
            with self.subTest(records=records):
                data = form_fixture()
                self.assert_no_preference(self.call((*data[:3],records)), reason='answer_form_input_invalid')

    def test_tuple_inputs_remain_accepted_without_mutation(self):
        data = form_fixture()
        data = (tuple(data[0]),tuple(data[1]),data[2],tuple(data[3]))
        before = copy.deepcopy(data)
        self.assertEqual(self.call(data)[0], ['cw-local-form'])
        self.assertEqual(data,before)

    def test_invalid_or_overflowing_deadline_fails_closed(self):
        data = form_fixture()
        for deadline in (True, False, 'soon', {}, float('nan'), float('inf'), float('-inf'), 10**1000):
            with self.subTest(deadline=deadline):
                self.assert_no_preference(self.call(data,deadline=deadline), reason='answer_form_input_invalid')
        # None is the call helper's default, so exercise the public contract directly.
        self.assert_no_preference(preference.preferred_answer_form(
            NEGATIVE_QUERY,None,*data,deadline=None), reason='answer_form_input_invalid')

    def test_expired_deadline_never_publishes_hint(self):
        self.assert_no_preference(self.call(deadline=-1), reason='rerank_timeout')

    def test_deadline_expiring_during_verified_span_check_clears_witness_and_count(self):
        clock={'now':0.0}
        original=reranking._exact_span
        def late_span(*args,**kwargs):
            value=original(*args,**kwargs)
            clock['now']=2.0
            return value
        with patch.object(preference.time,'monotonic',side_effect=lambda:clock['now']), \
             patch.object(reranking,'_exact_span',side_effect=late_span):
            self.assert_no_preference(self.call(deadline=1),reason='rerank_timeout')

    def test_deadline_expiring_during_final_selection_cannot_publish_prepared_success(self):
        clock={'now':0.0}
        original_min=builtins.min
        def late_selection(values,*args,**kwargs):
            result=original_min(values,*args,**kwargs)
            if isinstance(values,list) and values and isinstance(values[0],tuple):
                clock['now']=2.0
            return result
        with patch.object(preference.time,'monotonic',side_effect=lambda:clock['now']), \
             patch.object(preference,'min',side_effect=late_selection,create=True):
            self.assert_no_preference(self.call(deadline=1),reason='rerank_timeout')
        self.assertEqual(clock['now'],2.0)

    def test_witness_offsets_are_global_original_locations_with_no_raw_quote(self):
        raw_groups,rows,parsed=fixture(parts=['Ordinary preface.',NEGATIVE_BODY],separator='\n')
        group=dict(raw_groups[1],kind='canonical_sentence_window',canonical_anchor_id=rows[1]['id'],
                   canonical_anchor_rank=2)
        records=[{'group_id':group['group_id'],'rank':1,
                  'query_view_rank_positions':{'translated':1}}]
        ids,diagnostic=self.call(([group],rows,parsed,records))
        self.assertEqual(ids,[group['group_id']])
        for witness in diagnostic['witnesses']:
            self.assertGreaterEqual(witness['start_char'],group['start_char'])
            self.assertLessEqual(witness['end_char'],group['end_char'])
            self.assertGreater(witness['end_char'],witness['start_char'])
            self.assertEqual(set(witness),{'shape','start_char','end_char'})
        self.assertNotIn(NEGATIVE_BODY,json.dumps(diagnostic))

    def test_one_preference_uses_actual_view_and_record_rank_without_promoting_other_group_scores(self):
        raw_groups,rows,parsed=fixture(parts=[NEGATIVE_BODY,NEGATIVE_BODY],separator='\n')
        groups=[dict(group,kind='canonical_sentence_window',canonical_anchor_id=rows[index]['id'],
                     canonical_anchor_rank=index+1,score=(-9999 if index==0 else 0))
                for index,group in enumerate(raw_groups)]
        records=[{'group_id':groups[0]['group_id'],'rank':1,'query_view_rank_positions':{'original':2}},
                 {'group_id':groups[1]['group_id'],'rank':2,'query_view_rank_positions':{'translated':1}}]
        saved=copy.deepcopy((groups,rows,parsed,records))
        ids,diagnostic=self.call((groups,rows,parsed,records))
        self.assertEqual(ids,[groups[1]['group_id']])
        self.assertEqual(diagnostic['matched_group_count'],2)
        self.assertEqual((groups,rows,parsed,records),saved)

    def test_soft_form_preference_pays_whole_citation_budget_and_does_not_create_hard_coverage(self):
        data=form_fixture(['If records are not supplied,','options include a reminder and a review.'])
        ids,diagnostic=self.call(data)
        groups,rows,_,_=data
        selected,plan=plan_evidence_groups([(-1,row['id'],{'general'}) for row in rows],groups,
            eligible_ids={row['id'] for row in rows},limit=1,canonical_rows=rows,
            ranking_ids=ids,preferred_group_ids=ids)
        self.assertEqual(diagnostic['authority'],'retrieval_hint_only')
        self.assertEqual(selected,[])
        self.assertEqual(plan['preferred_group_rejected_count'],1)
        self.assertEqual(plan['incomplete_accepted_groups'],0)
        self.assertEqual(plan['uncovered_seed_group_ids'],[])
        self.assertTrue(all(item['reason']=='preferred_budget_exceeded' for item in plan['rejected_groups']))


if __name__=='__main__':
    unittest.main()
