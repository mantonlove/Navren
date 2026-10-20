"""查询特征边界回归：只用合成公共原件、临时库和模型替身。

验证召回辅助失败降级与原始权限/身份/时点不变，不评价翻译质量或回答质量。
不打开真实公共库、个人库、联网、下载或加载真实模型。
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from local_knowledge import LocalKnowledgeProvider
from local_knowledge.embeddings import VectorError
from local_knowledge.query_features import translated_feature
from local_knowledge import query_translation as qt
from local_knowledge.query_translation import QueryTranslator, TranslationError
from local_knowledge.retrieval_encoder import EncoderBundle
from local_knowledge.store import PARSER
from local_knowledge.vectors import VectorIndex, fuse


AS_OF = '2026-10-06'
PRIVATE_QUERY = '我的秘密查询：未知金融主题哪方面？'
PRIVATE_FEATURE = 'Confidential translated query risk governance'


class QueryFeatureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict('os.environ', {'NAVREN_QUERY_TRANSLATION_ENABLED': '1'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_disabled_and_nonapplicable_never_acquire_or_call_translator(self):
        for enabled, applicable, expected_reason in (
                ('0', True, 'query_translation_disabled'),
                ('1', False, None), ('0', False, None)):
            with self.subTest(enabled=enabled, applicable=applicable), \
                    patch.dict('os.environ', {'NAVREN_QUERY_TRANSLATION_ENABLED': enabled}), \
                    patch('local_knowledge.query_translation.get_query_translator') as loader:
                feature, diagnostic = translated_feature(PRIVATE_QUERY, applicable=applicable)
                self.assertIsNone(feature)
                self.assertFalse(diagnostic['used'])
                self.assertEqual(diagnostic['reason'], expected_reason)
                loader.assert_not_called()

    def test_all_published_failure_codes_survive_without_text_in_diagnostics(self):
        for code in sorted(qt.ERROR_CODES):
            with self.subTest(code=code), \
                    patch('local_knowledge.query_translation.get_query_translator') as loader:
                loader.return_value.translate.side_effect = TranslationError(code)
                feature, diagnostic = translated_feature(PRIVATE_QUERY, applicable=True)
                self.assertIsNone(feature)
                self.assertEqual(diagnostic['reason'], code)
                serialized = json.dumps(diagnostic, ensure_ascii=False)
                self.assertNotIn(PRIVATE_QUERY, serialized)
                self.assertNotIn(PRIVATE_FEATURE, serialized)
                self.assertFalse(diagnostic['used'])
                self.assertIsNone(diagnostic['profile_id'])

    def test_unknown_exception_text_cannot_become_a_reason(self):
        with patch('local_knowledge.query_translation.get_query_translator') as loader:
            loader.return_value.translate.side_effect = RuntimeError(PRIVATE_QUERY + PRIVATE_FEATURE)
            feature, diagnostic = translated_feature(PRIVATE_QUERY, applicable=True)
        self.assertIsNone(feature)
        self.assertEqual(diagnostic['reason'], 'query_translation_inference_failed')
        self.assertNotIn('秘密', json.dumps(diagnostic, ensure_ascii=False))
        self.assertNotIn('Confidential', json.dumps(diagnostic))

    def test_success_metadata_contains_profile_but_no_original_or_translated_text(self):
        translator = SimpleNamespace(profile_id='fixture-query-feature-profile',
                                     translate=Mock(return_value=PRIVATE_FEATURE))
        with patch('local_knowledge.query_translation.get_query_translator', return_value=translator):
            feature, diagnostic = translated_feature(PRIVATE_QUERY, applicable=True)
        self.assertEqual(feature, PRIVATE_FEATURE)
        self.assertTrue(diagnostic['used'])
        self.assertEqual(diagnostic['profile_id'], translator.profile_id)
        self.assertIsNone(diagnostic['reason'])
        serialized = json.dumps(diagnostic, ensure_ascii=False)
        self.assertNotIn(PRIVATE_QUERY, serialized)
        self.assertNotIn(PRIVATE_FEATURE, serialized)

    def test_invalid_or_mixed_language_outputs_do_not_become_features(self):
        for output in ('', '只有中文', 'English 夹杂中文', 123, 'x' * 2049):
            with self.subTest(output=type(output).__name__), \
                    patch('local_knowledge.query_translation.get_query_translator') as loader:
                loader.return_value.translate.return_value = output
                feature, diagnostic = translated_feature(PRIVATE_QUERY, applicable=True)
                self.assertIsNone(feature)
                self.assertFalse(diagnostic['used'])
                self.assertEqual(diagnostic['reason'], 'query_translation_inference_failed')


class QueryTranslatorContractTests(unittest.TestCase):
    def translator(self, *, encoded_count=1, output='Governance risk', emitted=(2, 0)):
        """Exercise real bounded decoding with deterministic tensors, no sessions/model load."""
        translator = object.__new__(QueryTranslator)
        translator._np = np
        translator._lock = threading.Lock()
        translator._tokenizer = SimpleNamespace(encode=Mock(return_value=SimpleNamespace(
            ids=[2] * encoded_count, tokens=['fixture-piece'] * encoded_count)))
        translator._joint = {'fixture-piece': 2}
        translator._pieces = ['</s>', '<unk>', 'fixture-piece']
        translator._decoder = SimpleNamespace(decode=Mock(return_value=output))
        translator._encoder = object()
        translator._encoder_output = 'hidden'
        translator._merged = object()
        translator._cache_names = {f'past_key_values.{layer}.{side}.{kind}'
            for layer in range(qt.LAYERS) for side in ('encoder', 'decoder') for kind in ('key', 'value')}
        translator._decoder_outputs = ['logits'] + [
            name.replace('past_key_values.', 'present.') for name in sorted(translator._cache_names)]
        tokens = iter(emitted)
        decoder_calls = []

        def run(session, names, feeds, deadline):
            if session is translator._encoder:
                return [np.zeros((1, feeds['input_ids'].shape[1], qt.DIMENSION), dtype=np.float32)]
            decoder_calls.append((int(feeds['input_ids'][0, 0]), bool(feeds['use_cache_branch'][0])))
            token = next(tokens, 2)
            logits = np.zeros((1, 1, qt.VOCABULARY_SIZE), dtype=np.float32)
            logits[0, 0, token] = 1.
            outputs = {'logits': logits}
            for name in translator._cache_names:
                length = feeds['encoder_hidden_states'].shape[1] if '.encoder.' in name else len(decoder_calls)
                outputs[name.replace('past_key_values.', 'present.')] = np.zeros(
                    (1, qt.ATTENTION_HEADS, length, qt.DIMENSION // qt.ATTENTION_HEADS), dtype=np.float32)
            return [outputs[name] for name in names]

        translator._run = Mock(side_effect=run)
        return translator, decoder_calls

    def assert_translation_error(self, translator, query, code):
        with self.assertRaises(TranslationError) as result:
            translator.translate_query(query)
        self.assertEqual(result.exception.code, code)

    def test_source_token_limit_reserves_eos_without_silent_truncation(self):
        accepted, _ = self.translator(encoded_count=qt.MAX_INPUT_TOKENS - 1)
        self.assertEqual(len(accepted._input_ids('合成风险问题')), qt.MAX_INPUT_TOKENS)
        rejected, _ = self.translator(encoded_count=qt.MAX_INPUT_TOKENS)
        self.assert_translation_error(rejected, '合成风险问题', 'query_translation_input_too_long')
        rejected._run.assert_not_called()

    def test_character_and_special_token_boundaries_do_not_run_inference(self):
        for query, code in (('中' * (qt.MAX_INPUT_CHARACTERS + 1), 'query_translation_input_too_long'),
                            ('中文<pad>问题', 'query_translation_input_unsupported'),
                            ('English only', 'query_translation_not_applicable'),
                            ('', 'query_translation_input_invalid')):
            with self.subTest(code=code):
                translator, _ = self.translator()
                self.assert_translation_error(translator, query, code)
                translator._run.assert_not_called()

    def test_unmapped_source_piece_rejects_before_model_inference(self):
        translator, _ = self.translator()
        translator._tokenizer.encode.return_value.tokens = ['absent-piece']
        self.assert_translation_error(translator, '中文未知词', 'query_translation_input_unsupported')
        translator._run.assert_not_called()

    def test_eos_terminated_feature_keeps_numeric_literals_and_releases_lock(self):
        translator, calls = self.translator(output='Risk 0.25% in 2026 for 500 units')
        self.assertEqual(translator.translate('2026年500元费率0.25%？'),
                         'Risk 0.25% in 2026 for 500 units')
        self.assertEqual(calls, [(qt.DECODER_START_TOKEN_ID, False), (2, True)])
        self.assertFalse(translator._lock.locked())

    def test_changed_introduced_and_duplicated_numbers_reject_the_entire_feature(self):
        for query, output in (('风险是0.25%吗？', 'Risk is 0.35%'),
                              ('这个风险如何？', 'Risk 500'),
                              ('500元风险如何？', 'Risk 500 and 500'),
                              ('风险是0.25%吗？', 'Risk is 25%')):
            with self.subTest(output=output):
                translator, _ = self.translator(output=output)
                self.assert_translation_error(translator, query, 'query_translation_numeric_mismatch')
                self.assertFalse(translator._lock.locked())

    def test_generation_budget_without_eos_returns_no_partial_translation(self):
        translator, calls = self.translator(emitted=())
        self.assert_translation_error(translator, '中文风险问题', 'query_translation_generation_limit')
        self.assertEqual(len(calls), qt.MAX_GENERATED_TOKENS)
        translator._decoder.decode.assert_not_called()
        self.assertFalse(translator._lock.locked())

    def test_unknown_or_empty_eos_outputs_are_rejected(self):
        for emitted in ((0,), (1,)):
            with self.subTest(emitted=emitted):
                translator, _ = self.translator(emitted=emitted)
                self.assert_translation_error(translator, '中文风险问题', 'query_translation_output_invalid')

    def test_output_character_budget_rejects_instead_of_clipping(self):
        translator, _ = self.translator(output='x' * (qt.MAX_OUTPUT_CHARACTERS + 1))
        self.assert_translation_error(translator, '中文风险问题', 'query_translation_output_invalid')

    def test_invalid_encoder_tensor_rejects_before_decoder_runs(self):
        translator, calls = self.translator()
        translator._run.side_effect = lambda *args: [np.full((1, 2, qt.DIMENSION), np.nan, dtype=np.float32)]
        self.assert_translation_error(translator, '中文风险问题', 'query_translation_inference_failed')
        self.assertEqual(calls, [])
        self.assertFalse(translator._lock.locked())

    def test_lock_contention_has_controlled_timeout_and_does_not_run(self):
        translator, _ = self.translator()
        translator._lock = Mock()
        translator._lock.acquire.return_value = False
        self.assert_translation_error(translator, '中文风险问题', 'query_translation_timeout')
        translator._run.assert_not_called()
        translator._lock.release.assert_not_called()

    def test_ort_termination_or_deadline_rejects_completed_return_value(self):
        for by_timer in (False, True):
            with self.subTest(by_timer=by_timer):
                translator = object.__new__(QueryTranslator)
                options = SimpleNamespace(terminate=False)
                translator._ort = SimpleNamespace(RunOptions=lambda: options)
                session = Mock()
                session.run.return_value = ['discard']
                timestamps = [0., 0.] if by_timer else [0., 10.]
                with patch('local_knowledge.query_translation.threading.Timer') as timer, \
                        patch('local_knowledge.query_translation.time.monotonic', side_effect=timestamps):
                    if by_timer:
                        timer.return_value.start.side_effect = lambda: timer.call_args.args[1]()
                    with self.assertRaises(TranslationError) as result:
                        translator._run(session, ['output'], {}, 1.)
                self.assertEqual(result.exception.code, 'query_translation_timeout')
                timer.return_value.cancel.assert_called_once()

    def test_non_timeout_ort_error_is_controlled_without_exception_text(self):
        translator = object.__new__(QueryTranslator)
        translator._ort = SimpleNamespace(RunOptions=lambda: SimpleNamespace(terminate=False))
        session = Mock()
        session.run.side_effect = RuntimeError(PRIVATE_QUERY)
        with patch('local_knowledge.query_translation.threading.Timer') as timer:
            with self.assertRaises(TranslationError) as result:
                translator._run(session, [], {}, time.monotonic() + 5)
        self.assertEqual(result.exception.code, 'query_translation_inference_failed')
        self.assertNotIn(PRIVATE_QUERY, str(result.exception))
        timer.return_value.cancel.assert_called_once()

    def test_expired_ort_deadline_does_not_start_run_or_timer(self):
        translator = object.__new__(QueryTranslator)
        session = Mock()
        with patch('local_knowledge.query_translation.threading.Timer') as timer:
            with self.assertRaises(TranslationError) as result:
                translator._run(session, [], {}, time.monotonic() - 1)
        self.assertEqual(result.exception.code, 'query_translation_timeout')
        timer.assert_not_called()
        session.run.assert_not_called()


class QueryViewFailureTests(unittest.TestCase):
    def encoder(self, failures):
        encoder = SimpleNamespace(dimension=3, profile_id='fixture-query-view/3', encode_query=Mock())
        vectors = {'original': np.array([1., 0., 0.], dtype=np.float32),
                   'projection': np.array([0., 1., 0.], dtype=np.float32),
                   'translation': np.array([0., 0., 1.], dtype=np.float32)}

        def encode(query):
            if query in failures:
                failure = failures[query]
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return vectors[query]

        encoder.encode_query.side_effect = encode
        return encoder

    def search(self, failures):
        encoder = self.encoder(failures)
        index = VectorIndex(None, encoder)
        with patch.object(index, '_load', return_value=([], ['A', 'B', 'C'], np.eye(3, dtype=np.float32))), \
                patch.object(index, 'build', side_effect=AssertionError('read path must not build')):
            result = index.search(None, 'fixture-release', 'original', {'A', 'B', 'C'},
                projected_query='projection', translated_query='translation')
        return result, encoder

    def test_failed_translation_encoding_keeps_original_and_projection_candidates(self):
        result, _ = self.search({'translation': VectorError('vector_inference_failed')})
        self.assertEqual(result['mode'], 'vector')
        self.assertEqual(result['reason'], 'vector_partial')
        self.assertEqual(set(result['scores']), {'A', 'B'})
        self.assertEqual(set(result['views']), {'original', 'public_concepts'})
        self.assertNotIn(PRIVATE_FEATURE, json.dumps(result))

    def test_failed_original_encoding_does_not_discard_two_healthy_extra_views(self):
        result, _ = self.search({'original': RuntimeError(PRIVATE_QUERY)})
        self.assertEqual(result['mode'], 'vector')
        self.assertEqual(result['reason'], 'vector_partial')
        self.assertEqual(set(result['scores']), {'B', 'C'})
        self.assertEqual(set(result['views']), {'public_concepts', 'translated'})
        self.assertNotIn(PRIVATE_QUERY, json.dumps(result, ensure_ascii=False))

    def test_invalid_extra_vectors_have_independent_degradation(self):
        for invalid in (np.array([np.nan, 0., 0.]), np.zeros(3), np.ones(4)):
            with self.subTest(shape=invalid.shape):
                result, _ = self.search({'projection': invalid})
                self.assertEqual(result['mode'], 'vector')
                self.assertEqual(result['reason'], 'vector_partial')
                self.assertEqual(set(result['scores']), {'A', 'C'})

    def test_all_view_failures_return_controlled_fts_degradation(self):
        result, _ = self.search({name: RuntimeError(PRIVATE_QUERY + PRIVATE_FEATURE)
                                for name in ('original', 'projection', 'translation')})
        self.assertEqual(result['mode'], 'fts5')
        self.assertEqual(result['scores'], {})
        self.assertEqual(result['reason'], 'vector_inference_failed')
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(PRIVATE_QUERY, serialized)
        self.assertNotIn(PRIVATE_FEATURE, serialized)

    def test_bundle_reports_partial_view_failure_without_discarding_other_space(self):
        primary = self.encoder({'translation': VectorError('vector_inference_failed')})
        secondary = self.encoder({})
        secondary.profile_id = 'fixture-other-space/3'
        bundle = EncoderBundle(primary, lambda: secondary)
        with patch.object(VectorIndex, '_load', return_value=([], ['A', 'B', 'C'], np.eye(3, dtype=np.float32))):
            result = VectorIndex(None, bundle).search(None, 'fixture-release', 'original', {'A', 'B', 'C'},
                projected_query='projection', translated_query='translation', cross_language=True)
        self.assertEqual(result['mode'], 'vector')
        self.assertEqual(result['reason'], 'vector_partial')
        self.assertEqual(set(result['views']), {'0:original', '0:public_concepts', '1:original'})
        self.assertEqual(set(result['scores'].values()), {0.})
        self.assertEqual(result['branches'][0]['reason'], 'vector_partial')

    def test_feature_views_do_not_multiply_total_semantic_vote(self):
        single = fuse({}, {'A': .9}, semantic_views={'original': {'A': .9}})
        triple = fuse({}, {'A': .9}, semantic_views={
            'original': {'A': .9}, 'public_concepts': {'A': .8}, 'translated': {'A': .7}})
        self.assertAlmostEqual(single['A'], triple['A'])


class QueryFeatureOriginalGateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='navren-query-feature-fixture-')
        self.addCleanup(directory.cleanup)
        self.environment = patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '0',
            'NAVREN_VECTOR_ENCODER': 'dual', 'NAVREN_QUERY_TRANSLATION_ENABLED': '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.today = patch('local_knowledge.store.today', return_value='2026-10-10')
        self.today.start()
        self.addCleanup(self.today.stop)
        self.provider = LocalKnowledgeProvider(Path(directory.name) / 'public.sqlite3', encoder=False)
        self.provider.register_source('feature-original', publisher='Synthetic original publisher',
            endpoints=['https://feature-original.example/'], parser_id=PARSER,
            freshness={'general': None}, shape='plain-text-v1', permission_basis='isolated_public_fixture')
        self.versions = {}
        for product in ('FIXTURE-A', 'FIXTURE-B'):
            self.provider.register_product(product, issuer='FIXTURE', scheme='isolated-feature',
                code=product, name=product + ' synthetic original', verified=True)
            self.versions[product] = self.document(product, published_on='2026-10-04')
        self.release = self.provider.publish(version_ids=list(self.versions.values()))
        with self.provider.db() as db:
            self.rows = [dict(row) for row in self.provider._rows(db, self.release)]
        self.ids = {row['product_id']: row['id'] for row in self.rows}
        self.bundle = EncoderBundle(SimpleNamespace(profile_id='fixture-primary'),
                                    lambda: SimpleNamespace(profile_id='fixture-secondary'))
        self.provider.vector_encoder = self.bundle

    def document(self, product, *, published_on, **metadata):
        version = self.provider.stage_document(source_id='feature-original',
            source_url='https://feature-original.example/' + product + '/' + published_on,
            title=product + ' synthetic original',
            text='Governance risk original evidence about accountability and independent financial controls for ' + product,
            published_on=published_on, retrieved_on='2026-10-10', facets=['general'], product_id=product,
            **metadata)
        self.provider.approve_version(version, verification_basis='isolated_original_review')
        return version

    def request(self, **changes):
        request = {'scope': 'public', 'query': PRIVATE_QUERY, 'as_of': AS_OF,
                   'facets': ['general'], 'limit': 8}
        request.update(changes)
        return request

    def search(self, request, *, feature=PRIVATE_FEATURE, enabled=True):
        translator = SimpleNamespace(profile_id='fixture-query-feature-profile', translate=Mock(return_value=feature))
        # A deliberately overbroad candidate emitter must still not release gated IDs.
        mocked_vectors = {'scores': {row['id']: 0.9 for row in self.rows},
            'views': {'original': {row['id']: 0.9 for row in self.rows}}, 'mode': 'vector', 'reason': None}
        with patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '1' if enabled else '0'}), \
                patch('local_knowledge.query_translation.get_query_translator', return_value=translator) as loader, \
                patch.object(VectorIndex, 'search', return_value=mocked_vectors) as vectors:
            result = self.provider.search_public(request, self.release)
        return result, translator, loader, vectors

    def test_feature_cannot_change_explicit_product_identity_or_mutate_request(self):
        request = self.request(product_id='FIXTURE-A')
        before = deepcopy(request)
        result, translator, _, vectors = self.search(request, feature='FIXTURE-B Governance risk 2030')
        self.assertEqual(result['citation_ids'], [self.ids['FIXTURE-A']])
        self.assertEqual(vectors.call_args.args[3], {self.ids['FIXTURE-A']})
        self.assertEqual(request, before)
        translator.translate.assert_called_once_with(PRIVATE_QUERY)

    def test_feature_date_cannot_make_future_evidence_available_at_original_asof(self):
        future = self.document('FIXTURE-B', published_on='2026-10-07')
        self.release = self.provider.publish(version_ids=[self.versions['FIXTURE-A'], future])
        with self.provider.db() as db:
            self.rows = [dict(row) for row in self.provider._rows(db, self.release)]
        future_id = next(row['id'] for row in self.rows if row['version_id'] == future)
        request = self.request()
        result, _, _, vectors = self.search(request, feature='Governance risk effective 2030 future FIXTURE-B')
        self.assertNotIn(future_id, result['citation_ids'])
        self.assertNotIn(future_id, vectors.call_args.args[3])
        self.assertEqual(request['as_of'], AS_OF)
        self.assertIn('published_after_as_of', {gap['code'] for gap in result['coverage_gaps']})

    def test_translated_recall_cannot_revive_withdrawn_original(self):
        blocked = self.ids['FIXTURE-B']
        self.provider.withdraw(self.versions['FIXTURE-B'])
        result, _, _, vectors = self.search(self.request(), feature='FIXTURE-B Governance risk')
        self.assertNotIn(blocked, result['citation_ids'])
        self.assertNotIn(blocked, vectors.call_args.args[3])
        self.assertIn(self.ids['FIXTURE-A'], result['citation_ids'])

    def test_effective_to_boundary_stays_expired_despite_feature_date(self):
        expired = self.document('FIXTURE-B', published_on='2026-10-04', effective_to=AS_OF)
        self.release = self.provider.publish(version_ids=[self.versions['FIXTURE-A'], expired])
        with self.provider.db() as db:
            self.rows = [dict(row) for row in self.provider._rows(db, self.release)]
        expired_id = next(row['id'] for row in self.rows if row['version_id'] == expired)
        result, _, _, vectors = self.search(self.request(), feature='Governance risk available 2026-10-05')
        self.assertNotIn(expired_id, result['citation_ids'])
        self.assertNotIn(expired_id, vectors.call_args.args[3])
        self.assertIn('expired_evidence', {gap['code'] for gap in result['coverage_gaps']})

    def test_identity_verification_withdrawal_blocks_translated_recall(self):
        blocked = self.ids['FIXTURE-B']
        self.provider.register_product('FIXTURE-B', issuer='FIXTURE', scheme='isolated-feature',
            code='FIXTURE-B', name='FIXTURE-B synthetic original', verified=False)
        result, _, _, vectors = self.search(self.request(), feature='FIXTURE-B Governance risk')
        self.assertNotIn(blocked, result['citation_ids'])
        self.assertNotIn(blocked, vectors.call_args.args[3])
        self.assertIn('identity_unverified', {gap['code'] for gap in result['coverage_gaps']})

    def test_graph_withdrawal_filters_feature_candidates_before_vector_query(self):
        blocked = self.ids['FIXTURE-B']
        with self.provider.db() as db:
            edges = [row[0] for row in db.execute(
                "SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'", (blocked,))]
        self.assertTrue(edges)
        for edge in edges:
            self.provider.withdraw_relation(edge)
        result, _, _, vectors = self.search(self.request(), feature='FIXTURE-B Governance risk')
        self.assertNotIn(blocked, result['citation_ids'])
        self.assertNotIn(blocked, vectors.call_args.args[3])

    def test_outbound_gate_with_no_eligible_rows_does_not_load_translator(self):
        self.provider.set_source_state('feature-original', outbound_allowed=False)
        result, translator, loader, vectors = self.search(self.request())
        self.assertEqual(result['citation_ids'], [])
        loader.assert_not_called()
        translator.translate.assert_not_called()
        self.assertEqual(vectors.call_args.args[3], set())

    def test_vector_disabled_provider_does_not_translate_or_load(self):
        result, translator, loader, _ = self.search(self.request(), enabled=False)
        loader.assert_not_called()
        translator.translate.assert_not_called()
        self.assertFalse(result['retrieval']['query_translation']['used'])

    def test_english_query_is_nonapplicable_even_with_enabled_dual_selector(self):
        _, translator, loader, _ = self.search(self.request(query='Governance risk'))
        loader.assert_not_called()
        translator.translate.assert_not_called()

    def test_invalid_encoder_selector_degrades_without_translator_or_provider_exception(self):
        self.provider.vector_encoder = None
        with patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '1', 'NAVREN_VECTOR_ENCODER': 'invalid-fixture'}), \
                patch('local_knowledge.query_translation.get_query_translator') as loader:
            result = self.provider.search_public(self.request(query=PRIVATE_QUERY + ' Governance risk'), self.release)
        self.assertTrue(result['citation_ids'])
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_model_invalid')
        loader.assert_not_called()

    def test_query_and_feature_are_absent_from_diagnostics_and_public_storage(self):
        with self.provider.db() as db:
            before = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                      for table in ('document_versions', 'chunks', 'graph_relations', 'documents')}
        result, _, _, _ = self.search(self.request())
        serialized = json.dumps(result['retrieval'], ensure_ascii=False)
        self.assertNotIn(PRIVATE_QUERY, serialized)
        self.assertNotIn(PRIVATE_FEATURE, serialized)
        with self.provider.db() as db:
            after = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                     for table in before}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
