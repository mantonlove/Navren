"""纯本地合成反例；不载入真实模型，也不证明金融检索质量。"""
import builtins
import copy
import hashlib
import inspect
import json
import os
import threading
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_knowledge import reranking
from local_knowledge.evidence_group_selection import SelectionError, plan_evidence_groups


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fixture(count=3, *, parts=None, separator='\n'):
    parts = parts if parts is not None else [f'合成资料第{i:04d}段只说明本段的边界。' for i in range(count)]
    count=len(parts)
    text = separator.join(parts)
    parsed_sha = digest(text)
    rows = []
    groups = []
    cursor = 0
    for i, part in enumerate(parts):
        excerpt = part + (separator if i < count - 1 else '')
        cid, gid = f'c-{i:04d}', f'g-{i:04d}'
        start, end = cursor, cursor + len(excerpt)
        rows.append({
            'id': cid, 'version_id': 'version-fixture',
            'start_char': start, 'end_char': end, 'excerpt': excerpt,
            'excerpt_sha256': digest(excerpt), 'parsed_sha256': parsed_sha,
            'raw_sha256': parsed_sha, 'paragraph': f'chars:{start}-{end}',
            'page': None, 'product_id': None, 'share_class_id': None,
            'channel_id': None, 'data_on': '2026-10-01',
            'facets': ['general'], 'conditions': [], 'parser_id': 'plain-v1',
            'shape': 'market-official-full-text-v1', 'source_id': 'source-fixture',
            'source_url': 'https://official.example/report',
            'endpoints': ['https://official.example/'],
            'visibility': 'public', 'data_origin': 'real', 'status': 'verified',
            'enabled': 1, 'read_allowed': 1, 'outbound_allowed': 1, 'paused': 0,
            'verified': 1,
        })
        groups.append({
            'group_id': gid, 'kind': 'sentence_core', 'core': True,
            'complete': True, 'citation_ids': [cid], 'version_id': 'version-fixture',
            'start_char': start, 'end_char': end, 'boundary_key': 'fixture-boundary',
            'score': -.01, 'fused_score': -.01, 'facets': ['general'],
            'rank_positions': {'canonical': i + 1, 'sentence': i + 1},
            'recall_rank_positions': {'lexical.original': i + 1}, 'rejection': None,
            'context_options': [],
        })
        cursor = end
    parsed = {'version-fixture': {'parsed_text': text, 'parsed_sha256': parsed_sha}}
    return groups, rows, parsed


def combined(groups):
    result = copy.deepcopy(groups[0])
    result.update(group_id='g-combined',
                  citation_ids=[cid for group in groups for cid in group['citation_ids']],
                  start_char=min(group['start_char'] for group in groups),
                  end_char=max(group['end_char'] for group in groups))
    return result


def pool_group(gid, *, kind='sentence_core', version='version', rank=1,
               branch='lexical.original', canonical=None, sentence=None,
               anchor=None, start=0, end=10, cid=None):
    """候选池阶段的合成合法 rank；原件/hash/权限另有实际消费链反例。"""
    value=dict(group_id=gid,kind=kind,version_id=version,start_char=start,end_char=end,
               citation_ids=[cid or 'cid-'+gid],core=True,complete=True,rejection=None,
               rank_positions={'canonical':canonical,'sentence':sentence},child_rank=sentence,
               recall_rank_positions={} if rank is None else {branch:rank})
    if kind=='canonical_sentence_window':
        value.update(canonical_anchor_id=value['citation_ids'][0],canonical_anchor_rank=anchor)
    return value


class FakeReranker:
    profile_id = 'fixture-reranker'

    def __init__(self, scores=None, results=None):
        self.calls = []
        self.scores = scores or {}
        self.results = results

    def score_documents(self, query, texts):
        self.calls.append((query, list(texts)))
        if callable(self.results):
            return self.results(texts)
        if self.results is not None:
            return self.results
        return [{'score': self.scores.get(text, float(-i)), 'reason': None,
                 'window_count': 1} for i, text in enumerate(texts)]


class CharacterTokenizer:
    """仅按字符生成精确 offset，验证完整覆盖合同，不模拟真实语义质量。"""
    def encode(self, first, second=None, add_special_tokens=False):
        if second is None:
            return SimpleNamespace(ids=list(range(len(first))),
                offsets=[(i, i + 1) for i in range(len(first))])
        count = len(first) + len(second) + (4 if add_special_tokens else 0)
        return SimpleNamespace(ids=list(range(count)), source_text=second)


class RerankerWindowBoundaryTests(unittest.TestCase):
    def adapter(self):
        adapter = reranking.LocalReranker.__new__(reranking.LocalReranker)
        adapter._tokenizer = CharacterTokenizer()
        return adapter

    def test_long_candidate_covers_final_source_characters_without_truncation(self):
        text = '甲' * 3000 + '完整尾部不可遗漏'
        windows = self.adapter()._windows('原始问题', text)
        self.assertGreater(len(windows), 1)
        self.assertTrue(windows[0].source_text.startswith('甲'))
        self.assertTrue(windows[-1].source_text.endswith('完整尾部不可遗漏'))
        # Each bounded pair is formed from an exact contiguous original slice.
        self.assertTrue(all(window.source_text in text for window in windows))
        self.assertTrue(all(len(window.ids) <= 512 for window in windows))

    def test_excess_windows_reject_whole_candidate_instead_of_returning_prefix(self):
        with self.assertRaises(reranking.RerankError) as raised:
            self.adapter()._windows('原始问题', '甲' * 5000 + '禁止丢弃的尾部')
        self.assertEqual(raised.exception.code, 'rerank_candidate_too_long')

    def test_length_sorted_batches_preserve_every_window_owner_and_output_order(self):
        adapter=self.adapter();adapter._lock=threading.Lock()
        def pair(name,length,score):
            return SimpleNamespace(ids=list(range(length)),name=name,score=score)
        documents={'first':[pair('first-long',90,9),pair('first-short',5,4)],
                   'second':[pair('second',20,3)],
                   'third':[pair('third-short',5,1),pair('third-long',110,7)]}
        adapter._windows=lambda query,text,**kwargs:documents[text]
        seen=[];batches=[]
        def run(values,deadline):
            batches.append(len(values));seen.extend((value.name,len(value.ids)) for value in values)
            return [value.score for value in values]
        adapter._run=run
        results=adapter.score_documents('合成原问',['first','second','third'])
        self.assertEqual([result['score'] for result in results],[9,3,7])
        self.assertEqual([result['window_count'] for result in results],[2,1,2])
        self.assertEqual(batches,[4,1])
        self.assertEqual(seen,[('first-short',5),('third-short',5),('second',20),
                               ('first-long',90),('third-long',110)])

    def test_tokenizer_deadline_expiry_takes_precedence_over_length_error(self):
        clock = {'now': 0.0}

        class SlowTokenizer(CharacterTokenizer):
            def encode(self, first, second=None, add_special_tokens=False):
                clock['now'] = 2.0
                # A short string can produce many tokens. Timeout already
                # occurred, so this must not be relabelled as a length error.
                return SimpleNamespace(ids=list(range(1000)), offsets=[])

        adapter = self.adapter()
        adapter._tokenizer = SlowTokenizer()
        adapter._lock = threading.Lock()
        with patch.object(reranking.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(adapter, '_run', side_effect=AssertionError('expired inference')):
            with self.assertRaises(reranking.RerankError) as raised:
                adapter.score_documents('原始短问题', ['完整原文'], deadline=1.0)
        self.assertEqual(raised.exception.code, 'rerank_timeout')

    def test_external_artifact_map_cannot_bypass_pinned_validation(self):
        with patch.object(reranking, '_load_verified_artifacts',
                          side_effect=reranking.RerankError('rerank_model_invalid')) as verify:
            try:
                reranking.LocalReranker('/nonexistent-public-model', artifacts={})
            except TypeError:
                # Removing the bypass parameter is the strictest valid API.
                self.assertNotIn('artifacts', inspect.signature(reranking.LocalReranker).parameters)
                return
            except reranking.RerankError:
                self.assertTrue(verify.called, 'artifacts bypass skipped verification')
            else:
                self.fail('constructor accepted unverified artifact map')


class LocalRerankingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'NAVREN_RERANK_ENABLED': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def rank(self, fake, values=None, query='赎回条件如何确定', **kwargs):
        groups, rows, parsed = values or fixture()
        return reranking.rerank_groups(query, groups, rows, parsed, reranker=fake, **kwargs)

    def assert_safe_diagnostics(self, diagnostics, query, rows):
        serialized = json.dumps(diagnostics, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(query, serialized)
        for row in rows:
            self.assertNotIn(row['excerpt'].rstrip(), serialized)
        self.assertNotIn('parsed_text', serialized)

    def test_default_off_does_not_load_model_or_call_injected_fake(self):
        fake = FakeReranker()
        with patch.dict(os.environ, {}, clear=True), patch.object(
                reranking, '_load_verified_artifacts', side_effect=AssertionError('unexpected model load')):
            ranking, diagnostics = self.rank(fake)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(fake.calls, [])

    def test_fake_receives_original_question_and_exact_source_spans(self):
        values = fixture()
        groups, rows, parsed = values
        query = '秘密标记987：中文原问题如何赎回？'
        fake = FakeReranker(scores={rows[-1]['excerpt']: 20.0})
        ranking, diagnostics = self.rank(fake, values, query)
        self.assertTrue(diagnostics['used'])
        self.assertEqual(ranking[0], groups[-1]['group_id'])
        self.assertEqual(fake.calls[0][0], query)
        self.assertEqual(set(fake.calls[0][1]), {row['excerpt'] for row in rows})
        self.assert_safe_diagnostics(diagnostics, query, rows)

    def test_oversized_question_returns_fixed_failure_before_fake_call(self):
        fake = FakeReranker()
        query = '甲' * 501
        ranking, diagnostics = self.rank(fake, query=query)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertIsInstance(diagnostics['reason'], str)
        self.assertTrue(diagnostics['reason'])
        self.assertNotIn(query, json.dumps(diagnostics, ensure_ascii=False))
        self.assertEqual(fake.calls, [])

    def test_deadline_includes_loader_and_prevents_post_load_scoring(self):
        clock = {'now': 0.0}
        fake = FakeReranker()
        loader_deadlines = []

        def slow_loader(*, deadline=None):
            loader_deadlines.append(deadline)
            clock['now'] = reranking.MAX_CALL_SECONDS + 1.0
            return fake

        groups, rows, parsed = fixture(1)
        with patch.object(reranking.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(reranking, 'get_reranker', side_effect=slow_loader):
            ranking, diagnostics = reranking.rerank_groups('原始问题', groups, rows, parsed)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')
        self.assertEqual(loader_deadlines, [reranking.MAX_CALL_SECONDS])
        self.assertEqual(fake.calls, [])

    def test_caller_expired_deadline_prevents_model_load(self):
        groups, rows, parsed = fixture(1)
        with patch.object(reranking.time, 'monotonic', return_value=10.0), \
                patch.object(reranking, 'get_reranker', side_effect=AssertionError('expired loader')):
            ranking, diagnostics = reranking.rerank_groups(
                '原始问题', groups, rows, parsed, deadline=9.0)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')

    def test_scoring_return_after_shared_deadline_is_not_published(self):
        clock = {'now': 0.0}

        def late_scores(texts):
            clock['now'] = 2.0
            return [{'score': 1.0, 'reason': None, 'window_count': 1} for _ in texts]

        fake = FakeReranker(results=late_scores)
        groups, rows, parsed = fixture(1)
        with patch.object(reranking.time, 'monotonic', side_effect=lambda: clock['now']):
            ranking, diagnostics = reranking.rerank_groups(
                '原始问题', groups, rows, parsed, reranker=fake, deadline=1.0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')

    def test_final_sort_expiry_cannot_publish_order_or_success_metadata(self):
        clock = {'now': 0.0}
        original_sorted = builtins.sorted

        def delayed_final_sort(values, *args, **kwargs):
            result = original_sorted(values, *args, **kwargs)
            # Pool/span sorts use branch sets or row lists. Only the final
            # score map has group IDs as dictionary keys.
            if isinstance(values, dict) and values and all(
                    isinstance(gid, str) and gid.startswith('g-') for gid in values):
                clock['now'] = 2.0
            return result

        groups, rows, parsed = fixture(1)
        fake = FakeReranker()
        with patch.object(reranking.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(reranking, 'sorted', delayed_final_sort, create=True):
            ranking, diagnostics = reranking.rerank_groups(
                '原始问题', groups, rows, parsed, reranker=fake, deadline=1.0)
        self.assertEqual(clock['now'], 2.0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['scored_count'], 0)
        self.assertEqual(diagnostics['ranked_groups'], [])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')

    def test_final_metadata_expiry_clears_prepared_success_state(self):
        clock = {'now': 0.0}
        original_enumerate = builtins.enumerate

        def delayed_rank_metadata(values, *args, **kwargs):
            # 池内平局也会枚举 ID；仅在实际评分结束后的诊断组装触发超时。
            # fake model 枚举的是精确原文，不能把准备候选时的 enumerate
            # 误当成最终发布，提前阻止此反例本来要核验的 model 调用。
            if fake.calls and isinstance(values, list) and values and all(
                    isinstance(gid, str) and gid.startswith('g-') for gid in values):
                clock['now'] = 2.0
            return original_enumerate(values, *args, **kwargs)

        groups, rows, parsed = fixture(1)
        fake = FakeReranker()
        with patch.object(reranking.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(reranking, 'enumerate', delayed_rank_metadata, create=True):
            ranking, diagnostics = reranking.rerank_groups(
                '原始问题', groups, rows, parsed, reranker=fake, deadline=1.0)
        self.assertEqual(clock['now'], 2.0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['scored_count'], 0)
        self.assertEqual(diagnostics['ranked_groups'], [])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')

    def test_nonfinite_or_malformed_scores_fail_as_one_batch(self):
        malformed = [
            [{'score': float('nan'), 'reason': None, 'window_count': 1}],
            [{'score': float('inf'), 'reason': None, 'window_count': 1}],
            [{'score': True, 'reason': None, 'window_count': 1}],
            [{'score': '9.0', 'reason': None, 'window_count': 1}],
            [{'score': 1.0, 'reason': None, 'window_count': 0}],
            [{'score': 1.0, 'reason': None, 'window_count': 9}],
            [{'score': 1.0, 'reason': None, 'window_count': True}],
            [{'score': 1.0, 'reason': '秘密标记错误原文', 'window_count': 1}],
            [{'reason': None, 'window_count': 1}],
            [1.0], [], None,
        ]
        for result in malformed:
            with self.subTest(result=repr(result)):
                fake = FakeReranker(results=lambda texts, result=result: result)
                ranking, diagnostics = self.rank(fake, fixture(1))
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertIsInstance(diagnostics['reason'], str)
                self.assertTrue(diagnostics['reason'])
                self.assertNotIn('秘密标记错误原文', json.dumps(diagnostics, ensure_ascii=False))

    def test_success_and_candidate_too_long_are_reported_separately(self):
        values = fixture(2)
        groups, rows, parsed = values
        fake = FakeReranker(results=lambda texts: [
            {'score': None, 'reason': 'rerank_candidate_too_long', 'window_count': 8},
            {'score': 2.0, 'reason': None, 'window_count': 1},
        ])
        ranking, diagnostics = self.rank(fake, values)
        self.assertTrue(diagnostics['used'])
        self.assertEqual(ranking, [groups[1]['group_id']])
        self.assertIn('rerank_candidate_too_long', json.dumps(diagnostics))
        self.assert_safe_diagnostics(diagnostics, '赎回条件如何确定', rows)

    def test_incomplete_or_rejected_group_is_not_scored(self):
        for key, value in (('complete', False), ('core', False),
                           ('rejection', 'not_eligible')):
            with self.subTest(key=key):
                values = fixture(1)
                values[0][0][key] = value
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, values)
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(fake.calls, [])

    def test_normal_admission_filters_do_not_report_a_partial_model_failure(self):
        cases = (
            ('complete', False, 'core_completion_limit'),
            ('rejection', 'core_completion_limit', 'core_completion_limit'),
            ('citation_ids', ['c-not-admitted'], 'not_eligible'),
        )
        for key, value, reason in cases:
            with self.subTest(key=key):
                values = fixture(2)
                groups, rows, parsed = values
                groups[1][key] = value
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, values)
                self.assertEqual(ranking, [groups[0]['group_id']])
                self.assertTrue(diagnostics['used'])
                self.assertIsNone(diagnostics['reason'])
                self.assertEqual(diagnostics['candidate_rejections'], {})
                self.assertEqual(diagnostics['admission_filtered_groups'], {reason: 1})
                self.assertEqual(fake.calls[0][1], [rows[0]['excerpt']])

    def test_actual_missing_span_mapping_remains_an_evidence_failure(self):
        groups, rows, parsed = fixture(2)
        malformed = combined(groups)
        malformed['citation_ids'] = groups[0]['citation_ids']
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, ([malformed, groups[1]], rows, parsed))
        self.assertEqual(ranking, [groups[1]['group_id']])
        self.assertEqual(diagnostics['reason'], 'rerank_partial')
        self.assertEqual(diagnostics['candidate_rejections'], {'rerank_evidence_invalid': 1})
        self.assertEqual(diagnostics['admission_filtered_groups'], {})
        self.assertEqual(fake.calls[0][1], [rows[1]['excerpt']])

    def test_missing_citation_cannot_be_silently_scored_as_a_prefix(self):
        groups, rows, parsed = fixture(2)
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, ([combined(groups)], rows[:1], parsed))
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(fake.calls, [])

    def test_complete_multicitation_span_is_scored_without_summary_or_prefix(self):
        groups, rows, parsed = fixture(2)
        whole = combined(groups)
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, ([whole], rows, parsed))
        self.assertTrue(diagnostics['used'])
        self.assertEqual(ranking, [whole['group_id']])
        self.assertEqual(fake.calls[0][1], [parsed['version-fixture']['parsed_text']])

    def test_late_bad_score_does_not_publish_an_earlier_valid_partial_order(self):
        fake = FakeReranker(results=[
            {'score': 9.0, 'reason': None, 'window_count': 1},
            {'score': float('nan'), 'reason': None, 'window_count': 1},
        ])
        ranking, diagnostics = self.rank(fake, fixture(2))
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['ranked_groups'], [])

    def test_duplicate_group_identifier_rejects_entire_input_before_scoring(self):
        groups, rows, parsed = fixture(2)
        groups[1]['group_id'] = groups[0]['group_id']
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, (groups, rows, parsed))
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(fake.calls, [])

    def test_mixed_metadata_cannot_merge_individually_valid_citations(self):
        mutations = {
            'version_id': 'version-other', 'product_id': 'product-other',
            'share_class_id': 'share-other', 'channel_id': 'channel-other',
            'page': 2, 'data_on': '2026-10-02', 'facets': ['risk'],
            'conditions': ['仅特定渠道'], 'paragraph': 'explicit-other-paragraph',
            'source_id': 'source-other', 'shape': 'shape-other', 'parser_id': 'parser-other',
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                groups, rows, parsed = fixture(2)
                rows[1][key] = value
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, ([combined(groups)], rows, parsed))
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(fake.calls, [])

    def test_hash_permissions_and_version_changes_reject_the_whole_group(self):
        mutations = {
            'excerpt_sha256': '0' * 64, 'parsed_sha256': '0' * 64,
            'version_id': 'version-other', 'enabled': 0, 'read_allowed': 0,
            'outbound_allowed': 0, 'paused': 1, 'status': 'staged',
            'visibility': 'personal', 'data_origin': 'synthetic',
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                values = fixture(1)
                values[1][0][key] = value
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, values)
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(fake.calls, [])

    def test_changed_parsed_bytes_and_quote_mismatch_do_not_reach_model(self):
        for change in ('parsed_bytes', 'quote', 'offset'):
            with self.subTest(change=change):
                values = fixture(1)
                if change == 'parsed_bytes':
                    values[2]['version-fixture']['parsed_text'] += '未验证变化'
                elif change == 'quote':
                    values[1][0]['excerpt'] = '改写后的摘要'
                    values[1][0]['excerpt_sha256'] = digest(values[1][0]['excerpt'])
                else:
                    values[0][0]['end_char'] += 1
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, values)
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(fake.calls, [])

    def test_strong_single_branch_candidate_survives_low_global_rank(self):
        groups, rows, parsed = fixture(320)
        target = groups[-1]
        target['recall_rank_positions'] = {'vector.secondary.original': 1}
        fake = FakeReranker(scores={rows[-1]['excerpt']: 100.0})
        ranking, diagnostics = self.rank(fake, (groups, rows, parsed))
        self.assertTrue(diagnostics['used'])
        self.assertEqual(ranking[0], target['group_id'])
        self.assertIn(rows[-1]['excerpt'], fake.calls[0][1])
        self.assertLessEqual(len(fake.calls[0][1]), 192)

    def test_fixed_segment_slots_receive_round_robin_pool_coverage(self):
        branches = ('lexical.original', 'lexical.public_concepts', 'lexical.translated',
                    'vector.primary.original', 'vector.primary.public_concepts',
                    'vector.primary.translated', 'vector.secondary.original')
        keys = [f'q{slot}.{branch}' for slot in range(4) for branch in branches]
        groups, rows, parsed = fixture(len(keys) * 17)
        first_ids = []
        first_texts = []
        for branch_index, key in enumerate(keys):
            for rank in range(1, 18):
                index = branch_index * 17 + rank - 1
                groups[index]['recall_rank_positions'] = {key: rank}
                if rank == 1:
                    first_ids.append(groups[index]['group_id'])
                    first_texts.append(rows[index]['excerpt'])
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, (groups, rows, parsed))
        self.assertTrue(diagnostics['used'])
        self.assertTrue(set(first_ids) <= set(ranking))
        self.assertTrue(set(first_texts) <= set(fake.calls[0][1]))
        self.assertLessEqual(len(fake.calls[0][1]), 192)

    def test_complete_canonical_and_source_scope_windows_are_reserved_before_alternatives(self):
        parts=['甲。'*499]+[f'第{i:04d}份合成资料仅描述样本边界。' for i in range(64)]
        groups,rows,parsed=fixture(parts=parts)
        windows=[]
        for index,group in enumerate(groups):
            window=copy.deepcopy(group)
            window.update(group_id=f'window-{index:04d}',kind='canonical_sentence_window',
                          canonical_anchor_id=rows[index]['id'],canonical_anchor_rank=index+1,
                          recall_rank_positions={},child_rank=None)
            window['rank_positions']['sentence']=None
            if index>=32:window['source_scope_rank']=index-31
            windows.append(window)
        alternatives=[]
        for index in range(260):
            alternative=copy.deepcopy(groups[0])
            alternative.update(group_id=f'cg-{index:04d}',start_char=index*2,end_char=index*2+2,
                               child_rank=index+1,kind='context_core')
            alternative['recall_rank_positions']={'q0.lexical.original':1}
            alternatives.append(alternative)
        fake=FakeReranker()
        ranking,diagnostic=self.rank(fake,(alternatives+windows,rows,parsed))
        self.assertTrue(diagnostic['used'])
        self.assertTrue({window['group_id'] for window in windows[:64]}<=set(ranking))
        self.assertEqual(diagnostic['pool_reserved_window_count'],32)
        self.assertEqual(diagnostic['pool_reserved_scope_window_count'],32)
        self.assertLessEqual(diagnostic['pool_count'],192)
        self.assertNotIn(windows[64]['group_id'],ranking)

    def test_exact_geometry_duplicates_keep_window_and_recall_branch_without_merging_other_facts(self):
        groups,rows,parsed=fixture(3)
        window=copy.deepcopy(groups[0])
        window.update(group_id='window-source',kind='canonical_sentence_window',
                      canonical_anchor_id=rows[0]['id'],canonical_anchor_rank=80,
                      recall_rank_positions={},rank_positions={'canonical':80,'sentence':None})
        other_version=copy.deepcopy(groups[2]);other_version['version_id']='another-version'
        rows[2]['version_id']='another-version'
        parsed['another-version']=copy.deepcopy(parsed['version-fixture'])
        groups[0]['rank_positions']={'canonical':80,'sentence':None}
        groups[0]['recall_rank_positions']={'vector.secondary.original':1}
        groups[1]['recall_rank_positions']={'vector.secondary.original':2}
        other_version['recall_rank_positions']={'vector.secondary.original':3}
        fake=FakeReranker()
        ranking,diagnostic=self.rank(fake,([window,groups[0],groups[1],other_version],rows,parsed))
        self.assertTrue(diagnostic['used'])
        self.assertIn(window['group_id'],ranking)
        self.assertNotIn(groups[0]['group_id'],ranking)
        self.assertIn(groups[1]['group_id'],ranking)
        self.assertIn(other_version['group_id'],ranking)
        self.assertEqual(diagnostic['pool_geometry_duplicate_count'],1)
        self.assertEqual(diagnostic['pool_version_count'],2)
        self.assertEqual(len(fake.calls[0][1]),3)

    def test_inherited_common_pool_scores_weak_window_representing_strong_full_core(self):
        groups, rows, parsed = fixture(2)
        core = combined(groups)
        core.update(rank_positions={'canonical': 80, 'sentence': None},
                    recall_rank_positions={'vector.secondary.original': 1}, child_rank=None)
        window = copy.deepcopy(core)
        window.update(group_id='window-inherited', kind='canonical_sentence_window',
                      canonical_anchor_id=rows[0]['id'], canonical_anchor_rank=80,
                      recall_rank_positions={})
        common_ids, counts = reranking._candidate_pool([window, core])
        self.assertEqual(common_ids, [window['group_id']])
        self.assertEqual(counts['_geometry_duplicates'], 1)

        discarded = FakeReranker()
        ranking, diagnostics = self.rank(discarded, ([window], rows, parsed))
        self.assertEqual(ranking, [])
        self.assertEqual(diagnostics['reason'], 'rerank_empty_candidates')
        self.assertEqual(discarded.calls, [])

        fake = FakeReranker()
        with patch.object(reranking, '_candidate_pool', side_effect=AssertionError('common pool repicked')):
            ranking, diagnostics = self.rank(fake, ([window], rows, parsed),
                                             preselected_ids=common_ids)
        self.assertEqual(ranking, [window['group_id']])
        self.assertTrue(diagnostics['used'])
        self.assertEqual(fake.calls[0][1], [parsed['version-fixture']['parsed_text']])
        self.assertEqual(diagnostics['pool_selection'], 'inherited_common_pool')
        self.assertEqual(diagnostics['pool_count'], 1)
        self.assertEqual(diagnostics['pool_version_count'], 1)
        self.assertEqual(diagnostics['pool_branch_counts'], {})
        self.assertEqual(diagnostics['pool_reserved_window_count'], 0)
        self.assertEqual(diagnostics['pool_reserved_scope_window_count'], 0)
        self.assertEqual(diagnostics['pool_geometry_duplicate_count'], 0)
        self.assert_safe_diagnostics(diagnostics, '赎回条件如何确定', rows)

    def test_preselected_pool_scores_only_requested_ids_in_requested_order(self):
        groups, rows, parsed = fixture(3)
        groups[2]['version_id'] = rows[2]['version_id'] = 'other-version'
        parsed['other-version'] = copy.deepcopy(parsed['version-fixture'])
        selected = (groups[2]['group_id'], groups[0]['group_id'])
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, (groups, rows, parsed), preselected_ids=selected)
        self.assertEqual(set(ranking), set(selected))
        self.assertEqual(fake.calls[0][1], [rows[2]['excerpt'], rows[0]['excerpt']])
        self.assertEqual(diagnostics['pool_count'], 2)
        self.assertEqual(diagnostics['pool_version_count'], 2)

    def test_explicit_empty_preselected_pool_never_falls_back_to_automatic_pool(self):
        for selected in ([], ()):
            with self.subTest(selected=selected):
                fake = FakeReranker()
                with patch.object(reranking, '_candidate_pool', side_effect=AssertionError('empty pool repicked')), \
                        patch.object(reranking, 'get_reranker', side_effect=AssertionError('empty model load')):
                    ranking, diagnostics = self.rank(fake, preselected_ids=selected)
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(diagnostics['reason'], 'rerank_empty_candidates')
                self.assertEqual(diagnostics['pool_count'], 0)
                self.assertEqual(fake.calls, [])

    def test_preselected_pool_rejects_invalid_duplicate_unknown_and_oversized_ids(self):
        values = fixture(193)
        gid = values[0][0]['group_id']
        invalid = (
            gid, {gid}, {gid: 1}, [True], [3], [''], ['私密查询标记7654321'],
            [gid, gid], [gid, 'g-unknown'],
            [group['group_id'] for group in values[0]],
        )
        for selected in invalid:
            with self.subTest(selected=repr(selected)):
                fake = FakeReranker()
                with patch.object(reranking, '_candidate_pool', side_effect=AssertionError('invalid pool repicked')):
                    ranking, diagnostics = self.rank(fake, values, preselected_ids=selected)
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(diagnostics['reason'], 'rerank_input_invalid')
                self.assertEqual(fake.calls, [])
                self.assertNotIn('私密查询标记7654321', json.dumps(diagnostics, ensure_ascii=False))

    def test_preselected_pool_accepts_all_192_ids_without_truncation(self):
        groups, rows, parsed = fixture(192)
        selected = [group['group_id'] for group in reversed(groups)]
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, (groups, rows, parsed), preselected_ids=selected)
        self.assertEqual(set(ranking), set(selected))
        self.assertEqual(fake.calls[0][1], [row['excerpt'] for row in reversed(rows)])
        self.assertEqual(diagnostics['pool_count'], 192)
        self.assertEqual(diagnostics['scored_count'], 192)

    def test_preselected_ids_must_reference_admitted_groups_with_complete_mapping(self):
        for change in ('incomplete', 'prior_rejection', 'missing_citation', 'missing_mapping',
                       'data_on', 'product_id', 'channel_id'):
            with self.subTest(change=change):
                groups, rows, parsed = fixture(2)
                whole = combined(groups)
                if change == 'incomplete':
                    whole['complete'] = False
                elif change == 'prior_rejection':
                    whole['rejection'] = 'not_eligible'
                elif change == 'missing_citation':
                    whole['citation_ids'] = ['c-not-admitted']
                elif change == 'missing_mapping':
                    whole['citation_ids'] = groups[0]['citation_ids']
                else:
                    rows[1][change] = '2026-10-02' if change == 'data_on' else 'other-object'
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, ([groups[0], whole], rows, parsed),
                                                 preselected_ids=[groups[0]['group_id'], whole['group_id']])
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(diagnostics['reason'], 'rerank_input_invalid')
                self.assertEqual(fake.calls, [])

    def test_preselected_pool_does_not_skip_rank_field_validation(self):
        for field, value in (
                ('recall_rank_positions', {'private-branch': 1}),
                ('rank_positions', {'canonical': True}), ('child_rank', 0)):
            with self.subTest(field=field):
                values = fixture(1)
                values[0][0][field] = value
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, values,
                                                 preselected_ids=[values[0][0]['group_id']])
                self.assertEqual(ranking, [])
                self.assertEqual(diagnostics['reason'], 'rerank_input_invalid')
                self.assertEqual(fake.calls, [])

    def test_preselected_pool_still_validates_original_hash_and_public_permissions(self):
        mutations = {
            'excerpt_sha256': '0' * 64, 'parsed_sha256': '0' * 64,
            'enabled': 0, 'read_allowed': 0, 'outbound_allowed': 0, 'paused': 1,
            'status': 'staged', 'visibility': 'personal', 'data_origin': 'synthetic',
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                values = fixture(1)
                values[1][0][key] = value
                fake = FakeReranker()
                ranking, diagnostics = self.rank(fake, values,
                                                 preselected_ids=[values[0][0]['group_id']])
                self.assertEqual(ranking, [])
                self.assertFalse(diagnostics['used'])
                self.assertEqual(diagnostics['candidate_rejections'], {'rerank_evidence_invalid': 1})
                self.assertEqual(fake.calls, [])

    def test_preselected_pool_respects_shared_deadline_before_and_after_scoring(self):
        values = fixture(1)
        selected = [values[0][0]['group_id']]
        with patch.object(reranking.time, 'monotonic', return_value=2.0), \
                patch.object(reranking, 'get_reranker', side_effect=AssertionError('expired model load')):
            ranking, diagnostics = reranking.rerank_groups(
                '原始问题', *values, deadline=1.0, preselected_ids=selected)
        self.assertEqual(ranking, [])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')

        clock = {'now': 0.0}
        def late_scores(texts):
            clock['now'] = 2.0
            return [{'score': 1.0, 'reason': None, 'window_count': 1} for _ in texts]
        fake = FakeReranker(results=late_scores)
        with patch.object(reranking.time, 'monotonic', side_effect=lambda: clock['now']):
            ranking, diagnostics = self.rank(fake, values, deadline=1.0, preselected_ids=selected)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(diagnostics['scored_count'], 0)
        self.assertEqual(diagnostics['ranked_groups'], [])
        self.assertEqual(diagnostics['reason'], 'rerank_timeout')

    def test_real_branch_pool_gives_qualified_other_version_a_turn_before_more_same_version(self):
        groups,rows,parsed=fixture(33)
        for group in groups:
            group['rank_positions']={'canonical':None,'sentence':None}
        target=groups[15];target['version_id']='other-version'
        rows[15]['version_id']='other-version'
        parsed['other-version']=copy.deepcopy(parsed['version-fixture'])
        fake=FakeReranker()
        ranking,diagnostic=self.rank(fake,(groups,rows,parsed))
        self.assertTrue(diagnostic['used'])
        self.assertEqual(fake.calls[0][1][:2],[rows[0]['excerpt'],rows[15]['excerpt']])
        self.assertNotIn(groups[16]['group_id'],ranking)
        self.assertEqual(diagnostic['pool_version_count'],2)

    def test_source_scope_rank_is_only_a_bounded_internal_rank(self):
        for rank in (True,0,-1,20001,float('nan'),'查询派生文本'):
            with self.subTest(rank=rank):
                groups,rows,parsed=fixture(1)
                groups[0].update(kind='canonical_sentence_window',canonical_anchor_id=rows[0]['id'],
                                 canonical_anchor_rank=1,source_scope_rank=rank)
                fake=FakeReranker()
                ranking,diagnostic=self.rank(fake,(groups,rows,parsed))
                self.assertEqual(ranking,[])
                self.assertEqual(fake.calls,[])
                self.assertNotIn('查询派生文本',json.dumps(diagnostic,ensure_ascii=False))

    def test_one_full_sentence_window_covers_multiple_anchors_without_duplicate_scores(self):
        for count in (2,3):
            with self.subTest(count=count):
                parts=['甲'*25 for _ in range(count-1)]+['乙'*25+'。']
                groups,rows,parsed=fixture(parts=parts,separator='')
                windows=[]
                for index,row in enumerate(rows):
                    window=combined(groups)
                    window.update(group_id=f'window-{index}',kind='canonical_sentence_window',
                                  canonical_anchor_id=row['id'],canonical_anchor_rank=index+1,
                                  rank_positions={'canonical':index+1,'sentence':None},recall_rank_positions={})
                    windows.append(window)
                fake=FakeReranker()
                ranking,diagnostic=self.rank(fake,(windows,rows,parsed))
                self.assertEqual(ranking,['window-0'])
                self.assertEqual(diagnostic['pool_reserved_window_count'],1)
                self.assertEqual(diagnostic['pool_geometry_duplicate_count'],count-1)
                self.assertEqual(fake.calls[0][1],[parsed['version-fixture']['parsed_text']])

    def test_canonical_alternatives_do_not_crowd_out_other_true_top_ranks(self):
        groups, rows, parsed = fixture(33)
        for group in groups:
            group['rank_positions']['sentence'] = None
            group['recall_rank_positions'] = {}
        alternatives = []
        for index in range(48):
            alternative = copy.deepcopy(groups[0])
            alternative['group_id'] = f'g-canonical-copy-{index:03d}'
            alternatives.append(alternative)
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, (alternatives + groups, rows, parsed))
        self.assertTrue(diagnostics['used'])
        self.assertTrue({group['group_id'] for group in groups[1:32]} <= set(ranking))
        self.assertNotIn(groups[32]['group_id'], ranking)
        self.assertLessEqual(diagnostics['pool_count'], 192)

    def test_sentence_pool_uses_true_rank_limit_across_many_rank_one_alternatives(self):
        groups, rows, parsed = fixture(33)
        for group in groups:
            group['rank_positions']['canonical'] = None
            group['recall_rank_positions'] = {}
        alternatives = []
        for index in range(48):
            alternative = copy.deepcopy(groups[0])
            alternative['group_id'] = f'g-sentence-copy-{index:03d}'
            alternatives.append(alternative)
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, (alternatives + groups, rows, parsed))
        self.assertTrue(diagnostics['used'])
        self.assertTrue({group['group_id'] for group in groups[1:32]} <= set(ranking))
        self.assertNotIn(groups[32]['group_id'], ranking)
        self.assertLessEqual(diagnostics['pool_count'], 192)

    def test_real_producer_single_canonical_contexts_cannot_crowd_out_67_complete_natural_cores(self):
        from local_knowledge.evidence_group_selection import fuse_group_candidates
        from local_knowledge.query_group_retrieval import with_context_candidates
        from local_knowledge.retrieval_units import build_units
        from local_knowledge.structure_index import (
            _core_records,_context_options,_canonical_sentence_windows)
        text=' '.join(f'Unit {index:02d} applies.' for index in range(67))
        _,rows,parsed=fixture(parts=[text],separator='')
        cid=rows[0]['id'];version=rows[0]['version_id']
        self.assertEqual(len(text),1138)
        cores,_=_core_records(text,build_units(text,rows,version_id=version))
        core_by_id={group['group_id']:group for group in cores}
        self.assertEqual(len(cores),67)
        self.assertTrue(all(group['complete'] for group in cores))
        for index,group in enumerate(cores,1):
            group.update(score=-1/index,child_rank=index,recall_rank_positions={},
                         context_options=_context_options(group,core_by_id,{cid}))
        windows=_canonical_sentence_windows(text,rows,cores,{cid})
        structure=with_context_candidates({'evidence_groups':cores+windows})
        self.assertEqual(structure['context_candidate_count'],131)
        self.assertEqual(max(len(group['context_options']) for group in cores),3)
        groups=fuse_group_candidates([(-1,cid,{'general'})],structure['evidence_groups'],
                                     eligible_ids={cid},canonical_rows=rows)
        self.assertEqual(len(groups),199)
        self.assertTrue(all(group['rejection'] is None for group in groups))
        saved=copy.deepcopy(groups)
        fake=FakeReranker()
        ranking,diagnostic=self.rank(fake,(groups,rows,parsed))
        self.assertTrue(diagnostic['used'])
        self.assertEqual(diagnostic['pool_count'],192)
        self.assertEqual(diagnostic['pool_reserved_window_count'],1)
        self.assertEqual(len(fake.calls),1)
        self.assertTrue({group['group_id'] for group in cores}<=set(ranking))
        self.assertTrue({text[group['start_char']:group['end_char']] for group in cores}
                        <=set(fake.calls[0][1]))
        self.assertEqual(groups,saved)

    def test_core_tie_does_not_change_legacy_cross_version_order_at_cap(self):
        groups=[pool_group(f'sg-{index:03d}',version=f'v-{index:03d}') for index in range(192)]
        groups.extend([pool_group('sg-zzz-target',version='target-version'),
                       pool_group('cg-000-target',kind='context_core',version='target-version',end=20)])
        saved=copy.deepcopy(groups)
        ids,counts=reranking._candidate_pool(groups)
        self.assertEqual(len(ids),192)
        self.assertEqual(ids[0],'sg-zzz-target')
        self.assertIn('sg-zzz-target',ids)
        self.assertNotIn('cg-000-target',ids)
        self.assertNotIn('sg-191',ids)
        self.assertEqual(counts['_reserved_windows'],0)
        self.assertEqual(groups,saved)
        self.assertEqual(ids,reranking._candidate_pool(list(reversed(groups)))[0])

    def test_core_tie_moves_only_derived_slots_and_keeps_other_kinds_in_original_positions(self):
        groups=[pool_group('cg-first',kind='context_core'),
                pool_group('cw-next',kind='canonical_sentence_window',anchor=80),
                pool_group('eg-required',kind='required_context'),
                pool_group('mg-single',kind='canonical_singleton'),pool_group('sg-last')]
        ids,_=reranking._candidate_pool(groups)
        self.assertEqual(ids,['sg-last','cg-first','eg-required','mg-single','cw-next'])
        self.assertEqual(ids.index('eg-required'),2)
        self.assertEqual(ids.index('mg-single'),3)

    def test_core_tie_never_admits_an_over_rank_core_using_a_different_window_or_context(self):
        unqualified=pool_group('sg-unqualified',rank=17,canonical=33,sentence=33,
                               start=0,end=10,cid='public-cid')
        qualified=pool_group('cw-qualified',kind='canonical_sentence_window',rank=1,
                             anchor=1,canonical=1,start=0,end=20,cid='public-cid')
        context=pool_group('cg-qualified',kind='context_core',rank=1,
                           start=0,end=30,cid='public-cid')
        ids,counts=reranking._candidate_pool([unqualified,qualified,context])
        self.assertEqual(set(ids),{'cw-qualified','cg-qualified'})
        self.assertNotIn('sg-unqualified',ids)
        self.assertEqual(counts['_reserved_windows'],1)
        self.assertEqual(counts['_geometry_duplicates'],0)

    def test_core_tie_preserves_exact_geometry_window_representation_and_reservation(self):
        qualified=pool_group('cw-qualified',kind='canonical_sentence_window',rank=1,
                             anchor=1,canonical=1,start=0,end=20,cid='public-cid')
        exact=pool_group('sg-same-fact',rank=None,canonical=33,sentence=33,
                         start=0,end=20,cid='public-cid')
        ids,counts=reranking._candidate_pool([exact,qualified])
        self.assertEqual(ids,['cw-qualified'])
        self.assertEqual(counts['_reserved_windows'],1)
        self.assertEqual(counts['_geometry_duplicates'],1)

    def test_core_tie_preserves_rank_qualification_and_global_cap_without_promising_all_cores(self):
        cases=[([pool_group(f'sg-{index:03d}',version=f'v-{index:03d}') for index in range(200)],
                 ['sg-199']),
               ([pool_group(f'sg-{version:03d}-{rank:02d}',version=f'v-{version:03d}',rank=rank)
                 for version in range(100) for rank in (1,2,16)],['sg-000-16'])]
        keys=sorted(reranking.RECALL_BRANCH_NAMES)
        cases.append(([pool_group(f'sg-b{branch:02d}-{rank:02d}',
                                  version=f'v-{branch:02d}-{rank:02d}',rank=rank,branch=key)
                       for branch,key in enumerate(keys) for rank in range(1,18)],
                      ['sg-b00-16',f'sg-b{len(keys)-1:02d}-16','sg-b00-17']))
        for groups,not_guaranteed in cases:
            with self.subTest(targets=not_guaranteed):
                ids,_=reranking._candidate_pool(groups)
                self.assertEqual(len(ids),192)
                self.assertFalse(set(not_guaranteed)&set(ids))

    def test_core_tie_profile_describes_only_local_ties_and_preserves_model_assets(self):
        profile=reranking.rerank_profile_metadata()
        self.assertEqual(profile['policy'],
                         'local-cross-encoder-q8-complete-canonical-windows-scope-core-tie-batch-v5')
        self.assertEqual(profile['max_candidates'],192)
        self.assertEqual(profile['max_windows'],8)
        self.assertEqual(profile['max_pair_tokens'],512)
        self.assertIn('other kinds keep positions',profile['pool_tie_policy'])
        self.assertIn('legacy minimum-rank minimum-group-ID version order preserved',
                      profile['pool_tie_policy'])
        self.assertEqual(profile['revision'],'1427fd652930e4ba29e8149678df786c240d8825')
        self.assertEqual(profile['artifacts']['onnx/model_qint8_arm64.onnx']['sha256'],
                         '1825907d6c1a9001ff78124780bbde20a614a8c3df3b63409cf3c72c6fe5c8b4')

    def test_only_consistent_verified_canonical_title_can_prefix_exact_span(self):
        groups, rows, parsed = fixture(2)
        title = '已验证资料的正式标题'
        for row in rows:
            row['title'] = title
        whole = combined(groups)
        whole['title'] = '由问题推导的任意标题不可采用'
        whole['text'] = '外来改写文本不可采用'
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, ([whole], rows, parsed))
        self.assertEqual(ranking, [whole['group_id']])
        scored_text = fake.calls[0][1][0]
        self.assertTrue(scored_text.startswith(title))
        self.assertTrue(scored_text.endswith(parsed['version-fixture']['parsed_text']))
        self.assertNotIn(whole['title'], scored_text)
        self.assertNotIn(whole['text'], scored_text)
        self.assertNotIn(title, json.dumps(diagnostics, ensure_ascii=False))
        self.assert_safe_diagnostics(diagnostics, '赎回条件如何确定', rows)

    def test_inconsistent_titles_for_same_version_reject_multicitation_span(self):
        groups, rows, parsed = fixture(2)
        rows[0]['title'] = '第一标题'
        rows[1]['title'] = '第二标题'
        fake = FakeReranker()
        ranking, diagnostics = self.rank(fake, ([combined(groups)], rows, parsed))
        self.assertEqual(ranking, [])
        self.assertFalse(diagnostics['used'])
        self.assertEqual(fake.calls, [])
        self.assertEqual(diagnostics['candidate_rejections'], {'rerank_evidence_invalid': 1})

    def test_unknown_branch_names_cannot_echo_query_or_gain_pool_admission(self):
        groups, rows, parsed = fixture(300)
        query = '不可落盘的私人问题标记7654321'
        groups[-1]['recall_rank_positions'] = {query: 1, 'q8.lexical.original': 1}
        fake = FakeReranker(scores={rows[-1]['excerpt']: 100.0})
        ranking, diagnostics = self.rank(fake, (groups, rows, parsed), query)
        self.assertNotIn(groups[-1]['group_id'], ranking)
        self.assertTrue(all(rows[-1]['excerpt'] not in texts for _, texts in fake.calls))
        self.assert_safe_diagnostics(diagnostics, query, rows)


class RankedEvidenceGroupPlanningTests(unittest.TestCase):
    def setUp(self):
        self.groups, self.rows, self.parsed = fixture(3)
        self.matches = [(-3.0 + i, row['id'], {'general'}) for i, row in enumerate(self.rows)]
        self.eligible = {row['id'] for row in self.rows}

    def plan(self, groups=None, limit=1, **kwargs):
        return plan_evidence_groups(self.matches, self.groups if groups is None else groups,
                                    eligible_ids=self.eligible, canonical_rows=self.rows,
                                    limit=limit, **kwargs)

    def test_valid_reranked_group_is_used_as_an_atomic_priority(self):
        selected, diagnostics = self.plan(ranking_ids=[self.groups[-1]['group_id']])
        self.assertEqual(selected, self.groups[-1]['citation_ids'])
        self.assertEqual(diagnostics['incomplete_accepted_groups'], 0)

    def test_unknown_or_duplicate_ranking_and_seed_group_ids_are_rejected(self):
        for kwargs in ({'ranking_ids': ['g-unknown']},
                       {'ranking_ids': ['g-0000', 'g-0000']},
                       {'seed_group_ids': ['g-unknown']},
                       {'seed_group_ids': ['g-0000', 'g-0000']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SelectionError):
                self.plan(**kwargs)

    def test_seed_group_cannot_be_replaced_by_a_group_sharing_one_citation(self):
        core = combined(self.groups[:2])
        selected, diagnostics = self.plan([self.groups[0], core, self.groups[2]],
                                         seed_group_ids=[core['group_id']])
        self.assertEqual(diagnostics['uncovered_seed_group_ids'], [core['group_id']])
        self.assertNotIn(core['group_id'], {group['group_id']
                                           for group in diagnostics['accepted_groups']})
        self.assertLessEqual(len(selected), 1)
        self.assertEqual(diagnostics['incomplete_accepted_groups'], 0)

    def test_seed_group_reserves_all_citations_before_other_ranked_groups(self):
        core = combined(self.groups[:2])
        selected, diagnostics = self.plan([core, self.groups[2]], limit=2,
            ranking_ids=[self.groups[2]['group_id'], core['group_id']],
            seed_group_ids=[core['group_id']])
        self.assertEqual(set(selected), set(core['citation_ids']))
        self.assertEqual(diagnostics['uncovered_seed_group_ids'], [])
        self.assertEqual(diagnostics['incomplete_accepted_groups'], 0)

    def test_reranking_does_not_bypass_required_facet_coverage(self):
        self.rows[0]['facets'] = ['risk']
        self.matches[0] = (-3.0, self.rows[0]['id'], {'risk'})
        selected, diagnostics = self.plan(facets=['risk'],
                                          ranking_ids=[self.groups[-1]['group_id']])
        self.assertEqual(selected, self.groups[0]['citation_ids'])
        self.assertEqual(diagnostics['uncovered_facets'], [])


if __name__ == '__main__':
    unittest.main()
