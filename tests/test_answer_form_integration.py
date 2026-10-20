"""原问形态软偏好的真实接入回归：合成原件、内存 SQLite、假重排器。

保留真实候选池、原文校验、视图融合和 planner 消费者；不加载模型、
访问正式 DB、网络或冻结金融题。本文件不测试事实支持或金融质量。
"""
import copy
import hashlib
import json
import os
import socket
import sqlite3
import unittest
from unittest.mock import patch

from local_knowledge import answer_form_preference, reranking
from local_knowledge.evidence_group_selection import fuse_group_candidates, plan_evidence_groups
from local_knowledge.query_group_retrieval import rank_query_groups


QUERY = '合成评审有多少票支持，多少票反对？'
TRANSLATION = 'How many votes support and how many votes oppose the sample review?'
PAIR_BODY = 'The sample receipt reports 7-2 votes. Both entries describe this synthetic review.'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class FakeReranker:
    """固定文本次序分数；只模拟调用结果格式，不模拟语义能力。"""
    profile_id = 'answer-form-integration-fixture'

    def __init__(self):
        self.calls = []
        self.rejected_texts = set()

    def score_documents(self, query, texts):
        self.calls.append((query, tuple(texts)))
        return [({'score': None, 'reason': 'rerank_candidate_too_long', 'window_count': 0}
                 if text in self.rejected_texts else
                 {'score': float(len(texts) - index), 'reason': None, 'window_count': 1})
                for index, text in enumerate(texts)]


def fixture(documents):
    """各示例为独立公共版本；parts 可让同一完整窗口跨多个原 CID。"""
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE document_versions(id TEXT, parsed_text TEXT, parsed_sha256 TEXT)')
    rows, groups, matches, bodies = [], [], [], {}
    for index, specification in enumerate(documents):
        if isinstance(specification, str):
            specification = {'parts': [specification]}
        parts = specification['parts']
        version = f'v-sample-{index:04d}'
        body = ''.join(parts)
        body_hash = digest(body)
        db.execute('INSERT INTO document_versions VALUES(?,?,?)', (version, body, body_hash))
        bodies[version] = body
        citations = []
        offset = 0
        facets = specification.get('facets', ['general'])
        for part_index, part in enumerate(parts):
            cid = f'c-sample-{index:04d}-{part_index}'
            start, end = offset, offset + len(part)
            row = {
                'id': cid, 'version_id': version, 'start_char': start, 'end_char': end,
                'excerpt': part, 'excerpt_sha256': digest(part), 'parsed_sha256': body_hash,
                'raw_sha256': body_hash, 'paragraph': f'chars:{start}-{end}',
                'page': None, 'product_id': None, 'share_class_id': None,
                'channel_id': None, 'data_on': '2026-10-01', 'facets': list(facets),
                'conditions': [], 'parser_id': 'plain-synthetic-v1',
                'shape': 'market-official-full-text-v1', 'source_id': f'source-sample-{index:04d}',
                'source_url': 'https://official.example/sample', 'publisher': 'Sample publisher',
                'visibility': 'public', 'data_origin': 'real', 'status': 'verified',
                'enabled': 1, 'read_allowed': 1, 'outbound_allowed': 1, 'paused': 0,
            }
            if 'title' in specification:
                row['title'] = specification['title']
            rows.append(row)
            matches.append((-1.0 / (len(matches) + 1), cid, set(facets)))
            citations.append(cid)
            offset = end
        groups.append({
            'group_id': f'cw-sample-{index:04d}', 'kind': 'canonical_sentence_window',
            'core': True, 'complete': True, 'rejection': None, 'version_id': version,
            'canonical_anchor_id': citations[0], 'citation_ids': citations,
            'start_char': 0, 'end_char': len(body), 'boundary_key': f'boundary-sample-{index:04d}',
            'score': 0.0, 'context_options': [], 'child_rank': None,
            # 相同真实 branch rank 在独立版本形成有界分支队列，能填满192池。
            'recall_rank_positions': {'lexical.original': 1},
        })
    db.commit()
    fused = fuse_group_candidates(matches, groups, eligible_ids={row['id'] for row in rows},
                                  canonical_rows=rows)
    return db, groups, fused, rows, matches, bodies


class AnswerFormIntegrationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'NAVREN_RERANK_ENABLED': '1',
                                             'NAVREN_ANSWER_FORM_ENABLED': '0'})
        environment.start()
        self.addCleanup(environment.stop)
        # 任一意外模型构造或 Python 网络创建立即失败。
        no_model = patch.object(reranking.LocalReranker, '__init__',
                                side_effect=AssertionError('real_model_forbidden'))
        no_socket = patch.object(socket, 'socket', side_effect=AssertionError('network_forbidden'))
        no_connect = patch.object(socket, 'create_connection',
                                  side_effect=AssertionError('network_forbidden'))
        for guard in (no_model, no_socket, no_connect):
            guard.start()
            self.addCleanup(guard.stop)

    def make_fixture(self, documents):
        value = fixture(documents)
        self.addCleanup(value[0].close)
        return value

    def rank(self, value, *, flag='1', query=QUERY, translation=TRANSLATION,
             segments=(), fake=None):
        db, _, groups, rows, _, _ = value
        fake = fake or FakeReranker()
        with patch.dict(os.environ, {'NAVREN_ANSWER_FORM_ENABLED': flag}), \
                patch.object(reranking, 'get_reranker', return_value=fake):
            result = rank_query_groups(db, query, copy.deepcopy(groups), rows, list(segments),
                                       translated_query=translation)
        return (*result, fake)

    def plan(self, value, ranking, meta, *, limit, facets=(), seed_ids=(), seed_group_ids=()):
        _, groups, _, rows, matches, _ = value
        return plan_evidence_groups(matches, groups, eligible_ids={row['id'] for row in rows},
                                    limit=limit, facets=facets, seed_ids=seed_ids,
                                    seed_group_ids=seed_group_ids, canonical_rows=rows,
                                    ranking_ids=ranking,
                                    preferred_group_ids=meta['preferred_evidence_group_ids'])

    def assert_timeout_clears_form(self, result):
        ids, seeds, meta = result
        self.assertEqual(ids, [])
        self.assertEqual(seeds, [])
        self.assertFalse(meta['used'])
        self.assertEqual(meta['reason'], 'rerank_timeout')
        self.assertEqual(meta['ranked_groups'], [])
        self.assertEqual(meta['scored_count'], 0)
        self.assertEqual(meta['scored_pair_count'], 0)
        self.assertEqual(meta['query_view_count'], 0)
        self.assertEqual(meta['preferred_evidence_group_ids'], [])
        self.assertEqual(meta['preferred_original_group_ids'], [])
        self.assertEqual(meta['original_canonical_preference_count'], 0)
        self.assertIsNone(meta['preference_origin'])
        form = meta['answer_form_preference']
        self.assertFalse(form['used'])
        self.assertEqual(form['reason'], 'rerank_timeout')
        self.assertEqual(form['matched_group_count'], 0)
        self.assertIsNone(form['preferred_group_id'])
        self.assertEqual(form['witnesses'], [])

    def test_only_exact_enabled_flag_calls_the_form_consumer(self):
        value = self.make_fixture(['The sample document describes the review.', PAIR_BODY])
        for disabled in ('0', '', 'true', 'yes', '2'):
            with self.subTest(flag=disabled), \
                    patch.object(answer_form_preference, 'preferred_answer_form',
                                 side_effect=AssertionError('disabled_form_called')):
                _, _, meta, _ = self.rank(value, flag=disabled)
            self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0000'])
            self.assertEqual(meta['preferred_original_group_ids'], ['cw-sample-0000'])
            self.assertEqual(meta['original_canonical_preference_count'], 1)
            self.assertEqual(meta['preference_origin'], 'original_canonical_first')
            self.assertEqual(meta['answer_form_preference'],
                             {'used': False, 'reason': 'answer_form_disabled'})
        _, _, enabled, _ = self.rank(value)
        self.assertTrue(enabled['answer_form_preference']['used'])
        self.assertEqual(enabled['preferred_evidence_group_ids'], ['cw-sample-0001'])

    def test_body_form_changes_soft_preference_without_changing_full_192_pool_or_ranking(self):
        documents = [f'The synthetic project register contains record {index:04d}.'
                     for index in range(200)]
        documents[1] = PAIR_BODY
        value = self.make_fixture(documents)
        before = copy.deepcopy(value[2])
        off_ids, off_seeds, off, off_fake = self.rank(value, flag='0')
        on_ids, on_seeds, on, on_fake = self.rank(value)
        self.assertEqual(on_ids, off_ids)
        self.assertEqual(on_seeds, off_seeds)
        self.assertEqual(len(on_ids), 192)
        self.assertEqual(on_fake.calls, off_fake.calls)
        self.assertEqual(len(on_fake.calls), 1)
        self.assertEqual(len(on_fake.calls[0][1]), 192)
        for field in ('ranked_groups', 'pool_count', 'pool_branch_counts',
                      'query_candidate_pool_count', 'query_candidate_pool_branch_counts',
                      'scored_count', 'scored_pair_count', 'query_view_count',
                      'query_lane_candidate_counts', 'source_coverage'):
            self.assertEqual(on[field], off[field], field)
        self.assertEqual(on['pool_count'], 192)
        self.assertEqual(off['preferred_original_group_ids'], ['cw-sample-0000'])
        self.assertEqual(on['preferred_evidence_group_ids'], ['cw-sample-0001'])
        self.assertEqual(on['preference_origin'], 'literal_answer_form')
        self.assertEqual(on['preferred_original_group_ids'], [])
        self.assertEqual(on['original_canonical_preference_count'], 0)
        self.assertEqual(value[2], before)
        form = on['answer_form_preference']
        self.assertEqual(form['authority'], 'retrieval_hint_only')
        self.assertEqual(form['matched_group_count'], 1)
        self.assertEqual(form['form'], 'paired_numeric')
        self.assertEqual(form['witnesses'], [{'shape': 'paired_numeric',
                         'start_char': PAIR_BODY.index('7-2 votes'),
                         'end_char': PAIR_BODY.index('7-2 votes') + len('7-2 votes')}])
        serialized = json.dumps(on, ensure_ascii=False)
        for literal in (QUERY, TRANSLATION, PAIR_BODY, '7-2 votes'):
            self.assertNotIn(literal, serialized)
        self.assertNotIn('supported', form)

    def test_title_and_candidate_supplied_text_cannot_supply_a_missing_body_form(self):
        value = self.make_fixture([
            'The project register describes one synthetic review.',
            {'parts': ['The body describes the procedure without a tally.'],
             'title': 'Synthetic review reports 7-2 votes'},
        ])
        value[2][0]['text'] = PAIR_BODY
        value[2][0]['shape'] = 'paired_numeric'
        _, _, meta, _ = self.rank(value)
        self.assertFalse(meta['answer_form_preference']['used'])
        self.assertEqual(meta['answer_form_preference']['reason'],
                         'scored_complete_window_form_missing')
        self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0000'])
        self.assertEqual(meta['preferred_original_group_ids'], ['cw-sample-0000'])
        self.assertEqual(meta['original_canonical_preference_count'], 1)

    def test_form_matching_unscored_core_cannot_become_window_preference(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        core = copy.deepcopy(value[2][1])
        core.update(group_id='sg-unscored-form', kind='sentence_core',
                    rank_positions={'canonical': 2, 'sentence': 1}, child_rank=1)
        # 这个版本仅有 core 表示；假评分器明确拒绝其实际评分，不借旧窗资格。
        value[2][1] = core
        fake = FakeReranker()
        fake.rejected_texts.add(PAIR_BODY)
        ids, _, meta, _ = self.rank(value, fake=fake)
        self.assertNotIn('sg-unscored-form', ids)
        self.assertNotIn('sg-unscored-form', meta['preferred_evidence_group_ids'])
        self.assertFalse(meta['answer_form_preference']['used'])
        self.assertEqual(meta['answer_form_preference']['matched_group_count'], 0)
        self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0000'])

    def test_even_independently_scored_core_cannot_replace_a_complete_window_preference(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        core = copy.deepcopy(value[2][1])
        core.update(group_id='sg-scored-form', kind='sentence_core',
                    rank_positions={'canonical': 2, 'sentence': 1}, child_rank=1)
        value[2][1] = core
        ids, _, meta, _ = self.rank(value)
        self.assertIn('sg-scored-form', ids)
        self.assertNotIn('sg-scored-form', meta['preferred_evidence_group_ids'])
        self.assertFalse(meta['answer_form_preference']['used'])
        self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0000'])

    def test_matching_window_with_failed_actual_score_cannot_become_preference(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        fake = FakeReranker()
        fake.rejected_texts.add(PAIR_BODY)
        ids, _, meta, _ = self.rank(value, fake=fake)
        self.assertEqual(meta['pool_count'], 2)
        self.assertNotIn('cw-sample-0001', ids)
        self.assertEqual(meta['candidate_rejections'], {'rerank_candidate_too_long': 1})
        self.assertFalse(meta['answer_form_preference']['used'])
        self.assertEqual(meta['answer_form_preference']['matched_group_count'], 0)
        self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0000'])

    def test_matching_eligible_body_outside_actual_192_pool_cannot_add_a_candidate(self):
        documents = [f'The synthetic project register contains record {index:04d}.'
                     for index in range(200)]
        documents[-1] = PAIR_BODY
        value = self.make_fixture(documents)
        ids, _, meta, fake = self.rank(value)
        self.assertEqual(len(ids), 192)
        self.assertEqual(meta['pool_count'], 192)
        self.assertNotIn('cw-sample-0199', ids)
        self.assertNotIn(PAIR_BODY, fake.calls[0][1])
        self.assertFalse(meta['answer_form_preference']['used'])
        self.assertEqual(meta['answer_form_preference']['matched_group_count'], 0)
        self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0000'])

    def test_negative_condition_enumeration_keeps_condition_and_all_window_cids(self):
        condition = 'If the record is not supplied, '
        listing = 'the options include a reminder, a second request and a later review.'
        body = condition + listing
        value = self.make_fixture([
            'The project register describes the review process.',
            {'parts': [condition, listing]},
        ])
        query = '如果记录没有及时补齐，可以用哪些方法完成检查？'
        translation = 'If the record is not supplied, what methods are available to complete the check?'
        ids, _, meta, _ = self.rank(value, query=query, translation=translation)
        form = meta['answer_form_preference']
        self.assertTrue(form['used'])
        self.assertEqual(form['form'], 'negative_condition_options')
        self.assertEqual(meta['preferred_evidence_group_ids'], ['cw-sample-0001'])
        self.assertEqual([w['shape'] for w in form['witnesses']],
                         ['negative_condition', 'explicit_enumeration'])
        for witness in form['witnesses']:
            self.assertTrue(0 <= witness['start_char'] < witness['end_char'] <= len(body))
        hard_cid = value[1][0]['citation_ids'][0]
        whole_cids = set(value[1][1]['citation_ids'])
        accepted, accepted_trace = self.plan(value, ids, meta, limit=3, seed_ids=[hard_cid])
        self.assertTrue(whole_cids <= set(accepted))
        self.assertEqual(accepted_trace['preferred_group_accepted_count'], 1)
        rejected, rejected_trace = self.plan(value, ids, meta, limit=2, seed_ids=[hard_cid])
        self.assertEqual(rejected, [hard_cid])
        self.assertFalse(set(rejected) & whole_cids)
        self.assertEqual(rejected_trace['preferred_group_accepted_count'], 0)
        self.assertEqual(rejected_trace['incomplete_accepted_groups'], 0)

    def test_enabled_form_never_loads_a_model_when_reranking_is_disabled(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        with patch.dict(os.environ, {'NAVREN_RERANK_ENABLED': '0', 'NAVREN_ANSWER_FORM_ENABLED': '1'}), \
                patch.object(reranking, 'get_reranker', side_effect=AssertionError('reranking_disabled')), \
                patch.object(answer_form_preference, 'preferred_answer_form',
                             side_effect=AssertionError('no_successful_score')):
            ids, seeds, meta = rank_query_groups(value[0], QUERY, value[2], value[3], [],
                                                 translated_query=TRANSLATION)
        self.assertEqual(ids, [])
        self.assertEqual(seeds, [])
        self.assertFalse(meta['used'])
        self.assertEqual(meta['preferred_evidence_group_ids'], [])
        self.assertFalse(meta['answer_form_preference']['used'])

    def test_multi_segment_request_keeps_segment_seeds_without_single_request_form(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        segment = (QUERY, {'evidence_groups': value[2]}, TRANSLATION)
        with patch.object(answer_form_preference, 'preferred_answer_form',
                          side_effect=AssertionError('single_form_must_not_replace_segments')):
            _, seeds, meta, _ = self.rank(value, segments=[segment])
        self.assertEqual(len(seeds), 1)
        self.assertEqual(meta['segment_missing_core_count'], 0)
        self.assertEqual(meta['preferred_evidence_group_ids'], [])
        self.assertEqual(meta['answer_form_preference']['reason'], 'answer_form_disabled')

    def test_final_deadline_after_real_form_success_clears_form_and_all_preferences(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        clock = {'now': 0.0}
        actual_form = answer_form_preference.preferred_answer_form
        observed = []

        def form_then_expire(*args, **kwargs):
            result = actual_form(*args, **kwargs)
            observed.append(copy.deepcopy(result))
            clock['now'] = reranking.MAX_CALL_SECONDS + 1
            return result

        with patch('local_knowledge.query_group_retrieval.time.monotonic',
                   side_effect=lambda: clock['now']), \
                patch.object(answer_form_preference, 'preferred_answer_form',
                             side_effect=form_then_expire):
            ids, seeds, meta, _ = self.rank(value)
        self.assertTrue(observed[0][1]['used'], '须先真实得到形态偏好再验证发布超时')
        self.assertEqual(observed[0][0], ['cw-sample-0001'])
        self.assert_timeout_clears_form((ids, seeds, meta))
        self.assertEqual(meta['attempted_scored_pair_count'], 2)

    def test_deadline_during_actual_body_scan_clears_form_witnesses(self):
        value = self.make_fixture(['The project register describes one review.', PAIR_BODY])
        clock = {'now': 0.0}
        actual_witnesses = answer_form_preference._witnesses

        def scan_then_expire(*args, **kwargs):
            result = actual_witnesses(*args, **kwargs)
            if result:
                clock['now'] = reranking.MAX_CALL_SECONDS + 1
            return result

        with patch('local_knowledge.query_group_retrieval.time.monotonic',
                   side_effect=lambda: clock['now']), \
                patch.object(answer_form_preference, '_witnesses', side_effect=scan_then_expire):
            ids, seeds, meta, _ = self.rank(value)
        self.assert_timeout_clears_form((ids, seeds, meta))

    def test_final_planner_pays_all_original_window_cids_and_keeps_hard_seed(self):
        value = self.make_fixture([
            'The project register describes one review.',
            {'parts': ['The sample receipt reports ',
                       '7-2 votes. Both entries describe this synthetic review.']},
        ])
        ids, _, meta, _ = self.rank(value)
        preferred = meta['preferred_evidence_group_ids'][0]
        preferred_cids = value[1][1]['citation_ids']
        hard_cid = value[1][0]['citation_ids'][0]
        selected, trace = self.plan(value, ids, meta, limit=3,
                                    facets=['general'], seed_ids=[hard_cid])
        self.assertEqual(set(selected), {hard_cid, *preferred_cids})
        accepted = {item['group_id']: item for item in trace['accepted_groups']}
        self.assertEqual(accepted[preferred]['reason'], 'preferred_coverage')
        self.assertEqual(accepted[preferred]['kind'], 'canonical_sentence_window')
        self.assertEqual(accepted[preferred]['citation_ids'], preferred_cids)
        self.assertEqual(trace['uncovered_seed_ids'], [])
        self.assertEqual(trace['uncovered_facets'], [])
        self.assertEqual(trace['incomplete_accepted_groups'], 0)
        self.assertEqual(trace['preferred_group_accepted_count'], 1)

    def test_insufficient_remaining_budget_rejects_whole_form_window_without_half_cid(self):
        value = self.make_fixture([
            'The project register describes one review.',
            {'parts': ['The sample receipt reports ', '7-2 votes in this synthetic review.']},
        ])
        ids, _, meta, _ = self.rank(value)
        hard_cid = value[1][0]['citation_ids'][0]
        preferred_cids = set(value[1][1]['citation_ids'])
        selected, trace = self.plan(value, ids, meta, limit=2, seed_ids=[hard_cid])
        self.assertEqual(selected, [hard_cid])
        self.assertFalse(set(selected) & preferred_cids)
        self.assertEqual(trace['uncovered_seed_ids'], [])
        self.assertEqual(trace['incomplete_accepted_groups'], 0)
        self.assertEqual(trace['preferred_group_accepted_count'], 0)
        rejection = next(item for item in trace['rejected_groups']
                         if item['group_id'] == meta['preferred_evidence_group_ids'][0])
        self.assertEqual(rejection['reason'], 'preferred_budget_exceeded')
        self.assertEqual(set(rejection['citation_ids']), preferred_cids)

    def test_form_soft_preference_cannot_drop_feasible_facet_and_seed_group_baseline(self):
        value = self.make_fixture([
            {'parts': ['A synthetic register describes a possible hazard.'], 'facets': ['risk']},
            {'parts': ['A synthetic register describes an access period.'], 'facets': ['liquidity']},
            {'parts': ['The sample receipt reports ', '7-2 votes in this synthetic review.']},
        ])
        off_ids, _, off, _ = self.rank(value, flag='0')
        on_ids, _, on, _ = self.rank(value)
        self.assertEqual(on_ids, off_ids)
        constraints = dict(limit=2, facets=['risk', 'liquidity'],
                           seed_ids=[value[1][0]['citation_ids'][0]],
                           seed_group_ids=['cw-sample-0001'])
        baseline, baseline_trace = self.plan(value, off_ids, off, **constraints)
        selected, trace = self.plan(value, on_ids, on, **constraints)
        self.assertEqual(set(selected), set(baseline))
        self.assertEqual(trace['uncovered_facets'], baseline_trace['uncovered_facets'])
        self.assertEqual(trace['uncovered_facets'], [])
        self.assertEqual(trace['uncovered_seed_ids'], [])
        self.assertEqual(trace['uncovered_seed_group_ids'], [])
        self.assertEqual(trace['incomplete_accepted_groups'], 0)
        self.assertEqual(trace['preferred_group_accepted_count'], 0)
        self.assertEqual(trace['preferred_group_rejected_count'], 1)


if __name__ == '__main__':
    unittest.main()
