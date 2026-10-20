"""字段检索缺口的隔离风险回归。

新造公共披露原件和临时 SQLite；不借用验收产品代码，不运行真实模型，
不读取个人资料或当前服务。这里只验证检索、身份、原文和治理边界。
"""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from local_knowledge import KnowledgeError, LocalKnowledgeProvider
from local_knowledge.embeddings import VectorError
from local_knowledge.public_aliases import concept_coverage, query_projection
from local_knowledge.retrieval_encoder import EncoderBundle
from local_knowledge.retrieval_ranking import adjacent_context
from local_knowledge.store import PARSER
from local_knowledge.vector_documents import representation
from local_knowledge.vectors import VectorIndex, fuse
from tests.test_vector_data_adaptation import disclosure_body, disclosure_product


DISCLOSED_ON = '2026-10-05'
AS_OF = '2026-10-07'
SOURCE = 'webank-direct-disclosure'
FIRST = 'ISOLATED-SAFE-A'
SECOND = 'ISOLATED-SAFE-C'
FIRST_NAME = '启明合成理财A'
SECOND_NAME = '启明合成理财C'


class ConstantEncoder:
    """所有片段相同的单位向量，刻意不给字段选择提供语义帮助。"""
    dimension = 4
    profile_id = 'fixture-shortfall-no-semantic-information/4-v1'

    def encode_documents(self, texts):
        return [np.array([[1., 0., 0., 0.]], dtype=np.float32) for _ in texts]

    def encode_query(self, query):
        return np.array([1., 0., 0., 0.], dtype=np.float32)


class NineWindowEncoder(ConstantEncoder):
    """新配置的第九窗口才含目标方向，验证模型专属窗口上限及末尾召回。"""
    profile_id = 'fixture-nine-window-isolated-profile/4-v1'
    max_windows = 16

    def encode_documents(self, texts):
        outputs = []
        for text in texts:
            windows = [np.array([1., 0., 0., 0.], dtype=np.float32) for _ in range(8)]
            windows.append(np.array([0., 1., 0., 0.] if '"risk":' in text else [0., 0., 1., 0.],
                                    dtype=np.float32))
            outputs.append(np.stack(windows))
        return outputs

    def encode_query(self, query):
        return np.array([0., 1., 0., 0.], dtype=np.float32)


class OtherSpaceEncoder(ConstantEncoder):
    dimension = 3
    profile_id = 'fixture-independent-secondary-space/3-v1'

    def encode_documents(self, texts):
        return [np.array([[1., 0., 0.]], dtype=np.float32) for _ in texts]

    def encode_query(self, query):
        return np.array([1., 0., 0.], dtype=np.float32)


class StructuredRetrievalShortfallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='navren-shortfall-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.date = patch('local_knowledge.store.today', return_value=AS_OF)
        self.date.start()
        self.addCleanup(self.date.stop)
        self.provider = LocalKnowledgeProvider(Path(self.directory.name) / 'public.sqlite3', encoder=False)
        self.products = [
            disclosure_product(FIRST, name=FIRST_NAME, publish='合成发行甲有限公司',
                               investor='C2合成稳健型和C3合成平衡型',
                               customerGroupType='符合资质的机构投资者',
                               charge='以保险公司核定为准', chargeWay='详见产品页面'),
            disclosure_product(SECOND, name=SECOND_NAME, publish='合成发行乙有限公司',
                               investor='C4合成进取型', customerGroupType='个人投资者',
                               charge='详见产品页面', chargeWay='详见产品合同'),
        ]
        self.import_products(self.products)

    def import_products(self, products):
        imported = self.provider.import_webank_disclosure(
            disclosure_body(products, DISCLOSED_ON), publish=True)
        self.release = imported['release_id']
        with self.provider.db() as database:
            self.rows = {row['id']: dict(row) for row in self.provider._rows(database, self.release)}
        self.citations = {(row['product_id'][7:], row['paragraph'].rsplit('/', 1)[-1]): cid
                          for cid, row in self.rows.items()}
        return imported

    def search(self, query, *, code=FIRST, identity=True, limit=3, **changes):
        request = {'scope': 'public', 'query': query, 'as_of': AS_OF,
                   'facets': [], 'limit': limit}
        if identity:
            request.update(product_id='WEBANK:' + code, channel_id='WEBANK')
        request.update(changes)
        return self.provider.search_public(request, self.release)

    def assert_first_field(self, result, field, code=FIRST):
        expected = self.citations[(code, field)]
        self.assertTrue(result['citation_ids'])
        self.assertEqual(result['citation_ids'][0], expected)
        canonical = self.provider.resolve_public_evidence(expected, self.release, AS_OF)
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical['excerpt'], self.rows[expected]['excerpt'])
        self.assertEqual(canonical['product_id'], 'WEBANK:' + code)
        return expected

    def test_chinese_fields_work_with_vector_disabled_and_duplicate_titles(self):
        fields = {'publish': '发行机构是哪一家？', 'charge': '收费标准披露了什么？',
                  'chargeWay': '收费方式是什么？', 'risk': '风险等级是什么？',
                  'investor': '合理投资范围包含哪些投资者？',
                  'customerGroupType': '客群类别范围写的是个人还是机构？'}
        self.assertEqual(len({row['title'] for row in self.rows.values()}), 2)
        for field, question in fields.items():
            with self.subTest(field=field):
                result = self.search(FIRST_NAME + '，' + question, limit=1)
                self.assert_first_field(result, field)
                self.assertEqual(result['retrieval']['mode'], 'fts5')
                self.assertEqual(result['retrieval']['vector_reason'], 'vector_disabled')

    def test_public_full_name_binds_field_without_explicit_request_identity(self):
        for code, name in ((FIRST, FIRST_NAME), (SECOND, SECOND_NAME)):
            with self.subTest(code=code):
                result = self.search(name + '的发行机构是谁？', identity=False, limit=1)
                self.assert_first_field(result, 'publish', code)

    def test_public_channel_code_binds_field_without_fund_share_inference(self):
        result = self.search('渠道披露代码 ' + SECOND + ' 的收费标准是什么？',
                             identity=False, limit=1)
        self.assert_first_field(result, 'charge', SECOND)
        hit = self.provider.resolve_public_evidence(result['citation_ids'][0], self.release, AS_OF)
        self.assertIsNone(hit['share_class_id'])
        self.assertEqual(hit['channel_id'], 'WEBANK')

    def test_explicit_identity_and_other_full_name_require_refusal_or_clarification(self):
        result = self.search(SECOND_NAME + '发行机构是哪家？', code=FIRST, limit=1)
        self.assertEqual(result['citation_ids'], [])
        self.assertIn('identity_conflict', {gap['code'] for gap in result['coverage_gaps']})

    def test_duplicate_public_names_cannot_choose_one_channel_code_implicitly(self):
        name = '同名合成理财披露'
        self.import_products([disclosure_product('ISOLATED-SAME-1', name=name),
                              disclosure_product('ISOLATED-SAME-2', name=name)])
        result = self.search(name + '发行机构是哪家？', identity=False, limit=1)
        self.assertEqual(result['citation_ids'], [])
        self.assertTrue({'identity_conflict', 'identity_ambiguous'} &
                        {gap['code'] for gap in result['coverage_gaps']})

    def test_explicit_verified_identity_disambiguates_same_public_name(self):
        name = '同名合成理财披露'
        first, second = 'ISOLATED-SAME-1', 'ISOLATED-SAME-2'
        self.import_products([disclosure_product(first, name=name, publish='同名合成发行甲'),
                              disclosure_product(second, name=name, publish='同名合成发行乙')])
        result = self.search(name + '发行机构是哪家？', code=first, limit=1)
        self.assert_first_field(result, 'publish', first)
        self.assertNotIn(self.citations[(second, 'publish')], result['citation_ids'])

    def test_institution_role_and_investor_group_do_not_share_generic_word_priority(self):
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assert_first_field(result, 'publish')
        self.assertNotIn(self.citations[(FIRST, 'customerGroupType')], result['citation_ids'])
        group = self.search(FIRST_NAME + '客群类别范围是不是机构投资者？', limit=1)
        self.assert_first_field(group, 'customerGroupType')
        suitability = self.search(FIRST_NAME + '合理投资范围有哪些？', limit=1)
        self.assert_first_field(suitability, 'investor')

    def test_multi_field_question_keeps_independent_originals(self):
        result = self.search(FIRST_NAME + '的发行机构、风险等级和收费标准分别是什么？', limit=3)
        expected = {self.citations[(FIRST, field)] for field in ('publish', 'risk', 'charge')}
        self.assertEqual(set(result['citation_ids']), expected)
        for cid in expected:
            canonical = self.provider.resolve_public_evidence(cid, self.release, AS_OF)
            self.assertEqual(canonical['excerpt'], self.rows[cid]['excerpt'])

    def test_field_words_inside_complete_object_name_do_not_become_question_intent(self):
        code = 'ISOLATED-TITLE-CUES'
        name = '风险等级收费标准合成产品'
        self.import_products([disclosure_product(code, name=name)])
        only_name = self.search(name, code=code, limit=1)
        self.assertEqual(only_name['retrieval']['structured_intent'], [])
        self.assertEqual(only_name['retrieval']['field_candidate_count'], 0)
        explicit_question = self.search(name + '的产品名称是什么？', code=code, limit=1)
        self.assert_first_field(explicit_question, 'name', code)
        self.assertEqual(explicit_question['retrieval']['structured_intent'], ['name'])

    def test_field_question_without_identifiable_public_object_is_not_forced(self):
        result = self.search('发行机构是哪家？', identity=False, limit=1)
        self.assertEqual(result['retrieval']['structured_intent'], [])
        self.assertEqual(result['retrieval']['field_candidate_count'], 0)

    def test_code_prefix_is_not_an_exact_public_object_identifier(self):
        result = self.search('渠道披露代码 ' + FIRST + 'Z的发行机构是哪家？', identity=False, limit=1)
        self.assertEqual(result['retrieval']['structured_intent'], [])
        self.assertEqual(result['retrieval']['field_candidate_count'], 0)

    def test_extended_identifier_is_not_an_existing_channel_code(self):
        for extension in ('-EXTRA', '_EXTRA', '.EXTRA', ':EXTRA'):
            with self.subTest(extension=extension):
                result = self.search('渠道披露代码 ' + FIRST + extension + ' 的发行机构是哪家？',
                                     identity=False, limit=1)
                self.assertEqual(result['retrieval']['structured_intent'], [])
                self.assertEqual(result['retrieval']['field_candidate_count'], 0)

    def test_bare_code_and_underlying_fund_code_do_not_establish_channel_identity(self):
        for query in (FIRST + '的风险等级是什么？', '基金代码 ' + FIRST + '的风险等级是什么？'):
            with self.subTest(query=query):
                result = self.search(query, identity=False, limit=1)
                self.assertEqual(result['retrieval']['structured_intent'], [])
                self.assertEqual(result['retrieval']['field_candidate_count'], 0)

    def test_money_amount_equal_to_public_numeric_code_is_not_product_identity(self):
        self.import_products([disclosure_product('918273', name='合成数字代码理财')])
        result = self.search('我有918273元，收费标准是什么？', identity=False, limit=1)
        self.assertEqual(result['retrieval']['structured_intent'], [])
        self.assertEqual(result['retrieval']['field_candidate_count'], 0)

    def test_two_explicit_public_objects_keep_their_independent_issuer_originals(self):
        result = self.search(FIRST_NAME + '与' + SECOND_NAME + '的发行机构分别是谁？',
                             identity=False, limit=2)
        expected = {self.citations[(code, 'publish')] for code in (FIRST, SECOND)}
        self.assertEqual(set(result['citation_ids']), expected)

    def test_plain_original_cannot_impersonate_registered_json_field(self):
        self.provider.register_source('fixture-plain', publisher='合成官方文本源',
            endpoints=['https://fixture.example/'], parser_id=PARSER,
            freshness={'general': None}, shape='plain-text-v1',
            permission_basis='isolated_synthetic_fixture_only')
        self.provider.register_product('FIXTURE-PLAIN-PRODUCT', issuer='FIXTURE',
            scheme='isolated-fixture', code='PLAIN-ONLY', name='非结构化合成资料', verified=True)
        version = self.provider.stage_document(source_id='fixture-plain',
            source_url='https://fixture.example/disclosure', title='非结构化合成资料',
            text='"publish":"合成机构"', published_on=DISCLOSED_ON,
            retrieved_on=AS_OF, facets=['general'], product_id='FIXTURE-PLAIN-PRODUCT')
        self.provider.approve_version(version, verification_basis='isolated_original_review')
        self.release = self.provider.publish(version_ids=[version], activate=False, temporary=True)
        result = self.provider.search_public({'scope': 'public', 'query': '非结构化合成资料发行机构是谁？',
            'product_id': 'FIXTURE-PLAIN-PRODUCT', 'as_of': AS_OF, 'facets': [], 'limit': 1}, self.release)
        self.assertEqual(result['retrieval']['structured_intent'], [])
        self.assertEqual(result['retrieval']['field_candidate_count'], 0)

    def test_retrieved_fee_placeholder_remains_unknown_and_not_a_supported_rate(self):
        result = self.search(FIRST_NAME + '的收费标准是不是0.00%？', limit=1)
        cid = self.assert_first_field(result, 'charge')
        self.assertEqual(representation(self.rows[cid])['detail_status'], 'reference_only')
        canonical = self.provider.resolve_public_evidence(cid, self.release, AS_OF)
        self.assertEqual(canonical['excerpt'], '"charge":"以保险公司核定为准"')
        self.assertNotIn('0.00%', canonical['excerpt'])
        self.assertNotIn('answer_supported', result['retrieval'])
        self.assertNotIn('fact_verified', result['retrieval'])

    def test_field_lexical_path_survives_missing_model(self):
        with patch('local_knowledge.vectors.VectorIndex._encoder',
                   side_effect=KnowledgeError('vector_model_missing')):
            result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assert_first_field(result, 'publish')
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_model_missing')

    def test_field_priority_does_not_depend_on_semantic_model_discrimination(self):
        self.provider.vector_encoder = ConstantEncoder()
        self.provider.build_vector_index(self.release)
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assert_first_field(result, 'publish')
        self.assertTrue(result['retrieval']['vector_used'])

    def test_corrupt_vector_index_degrades_without_losing_field_or_canonical_quote(self):
        self.provider.vector_encoder = ConstantEncoder()
        self.provider.build_vector_index(self.release)
        with self.provider.db() as database:
            database.execute('UPDATE chunk_vectors SET vector=? WHERE release_id=?',
                             (b'corrupt-fixture-vector', self.release))
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assert_first_field(result, 'publish')
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_index_invalid')

    def test_zero_coverage_lexical_vote_cannot_create_or_remove_semantic_evidence(self):
        result = fuse({'title-only-public-candidate': -30., 'semantic-public-candidate': -20.},
                      {'semantic-public-candidate': .81},
                      {'title-only-public-candidate': 0., 'semantic-public-candidate': 0.})
        self.assertNotIn('title-only-public-candidate', result)
        self.assertIn('semantic-public-candidate', result)
        self.assertLess(result['semantic-public-candidate'], 0.)

    def test_invalid_encoder_selector_does_not_crash_provider_or_submit_background_job(self):
        with patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '1',
                                      'NAVREN_VECTOR_ENCODER': 'unregistered-encoder-fixture'}), \
                patch('local_knowledge.vectors._pool.submit',
                      side_effect=AssertionError('invalid_selection_must_not_start_background_job')) as submit:
            provider = LocalKnowledgeProvider(self.provider.path)
            result = provider.search_public({'scope': 'public', 'query': FIRST_NAME + '发行机构是哪家？',
                'product_id': 'WEBANK:' + FIRST, 'channel_id': 'WEBANK',
                'as_of': AS_OF, 'facets': [], 'limit': 1}, self.release)
            self.assert_first_field(result, 'publish')
            self.assertEqual(result['retrieval']['mode'], 'fts5')
            self.assertEqual(result['retrieval']['vector_reason'], 'vector_model_invalid')
            submit.assert_not_called()

    def test_ninth_window_profile_recalls_tail_without_mixing_existing_profile(self):
        old = ConstantEncoder()
        self.provider.vector_encoder = old
        self.provider.build_vector_index(self.release)
        new = NineWindowEncoder()
        self.provider.vector_encoder = new
        built = self.provider.build_vector_index(self.release)
        self.assertEqual(built['vector_count'], len(self.rows) * 9)
        result = self.search('unique_tail_window_synthetic_query', limit=1)
        self.assert_first_field(result, 'risk')
        self.assertEqual(result['retrieval']['vector_candidate_count'], 1)
        with self.provider.db() as database:
            metas = {row['profile_id']: dict(row) for row in database.execute(
                'SELECT * FROM vector_releases WHERE release_id=?', (self.release,))}
            self.assertEqual(set(metas), {old.profile_id, new.profile_id})
            self.assertEqual(metas[old.profile_id]['vector_count'], len(self.rows))
            self.assertEqual(metas[new.profile_id]['vector_count'], len(self.rows) * 9)
            self.assertTrue(VectorIndex(self.provider, old).status(database, self.release)['ready'])
            self.assertTrue(VectorIndex(self.provider, new).status(database, self.release)['ready'])
            for cid in self.rows:
                windows = [row[0] for row in database.execute(
                    'SELECT window_no FROM chunk_vectors WHERE release_id=? AND profile_id=? AND citation_id=? '
                    'ORDER BY window_no', (self.release, new.profile_id, cid))]
                self.assertEqual(windows, list(range(9)))

    def test_default_eight_window_profile_is_not_silently_relaxed_to_sixteen(self):
        encoder = NineWindowEncoder()
        encoder.max_windows = 8
        encoder.profile_id = 'fixture-nine-windows-under-default-eight-policy/4-v1'
        self.provider.vector_encoder = encoder
        with self.assertRaisesRegex(KnowledgeError, 'vector_index_invalid'):
            self.provider.build_vector_index(self.release)
        with self.provider.db() as database:
            self.assertEqual(database.execute('SELECT count(*) FROM vector_releases WHERE profile_id=?',
                                             (encoder.profile_id,)).fetchone()[0], 0)
            self.assertEqual(database.execute('SELECT count(*) FROM chunk_vectors WHERE profile_id=?',
                                             (encoder.profile_id,)).fetchone()[0], 0)

    def test_undeclared_window_policy_remains_eight(self):
        class UndeclaredNineEncoder(ConstantEncoder):
            profile_id = 'fixture-undeclared-nine-window-policy/4-v1'

            def encode_documents(self, texts):
                return [np.repeat(value, 9, axis=0) for value in super().encode_documents(texts)]

        self.provider.vector_encoder = UndeclaredNineEncoder()
        with self.assertRaisesRegex(KnowledgeError, 'vector_index_invalid'):
            self.provider.build_vector_index(self.release)

    def test_invalid_window_policy_is_rejected_without_partial_index_publication(self):
        for index, value in enumerate((0, 9, 17, True, 16.0, '16')):
            with self.subTest(value=value):
                encoder = ConstantEncoder()
                encoder.max_windows = value
                encoder.profile_id = 'fixture-invalid-window-policy-' + str(index)
                self.provider.vector_encoder = encoder
                with self.assertRaisesRegex(KnowledgeError, 'vector_model_invalid'):
                    self.provider.build_vector_index(self.release)
                with self.provider.db() as database:
                    self.assertEqual(database.execute('SELECT count(*) FROM vector_releases WHERE profile_id=?',
                                                     (encoder.profile_id,)).fetchone()[0], 0)
                    self.assertEqual(database.execute('SELECT count(*) FROM chunk_vectors WHERE profile_id=?',
                                                     (encoder.profile_id,)).fetchone()[0], 0)

    def test_read_side_cannot_use_nine_windows_under_eight_window_policy(self):
        encoder = NineWindowEncoder()
        self.provider.vector_encoder = encoder
        self.provider.build_vector_index(self.release)
        encoder.max_windows = 8
        result = self.search('unique_tail_window_synthetic_query', limit=1)
        self.assertEqual(result['citation_ids'], [])
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_index_invalid')
        self.assertEqual(result['retrieval']['mode'], 'fts5')

    def test_explicit_structured_public_object_never_enables_secondary_space(self):
        primary = ConstantEncoder()
        secondary = Mock(side_effect=AssertionError('structured_object_must_not_load_secondary_space'))
        VectorIndex(self.provider, primary).build(self.release)
        self.provider.vector_encoder = EncoderBundle(primary, secondary)
        result = self.search(FIRST_NAME + '，发行机构是谁？', limit=1)
        self.assert_first_field(result, 'publish')
        secondary.assert_not_called()
        self.assertFalse(result['retrieval']['cross_language_route'])
        self.assertEqual(len(result['retrieval']['vector_branches']), 1)

    def test_withdrawn_original_cannot_be_restored_by_field_priority(self):
        target = self.citations[(FIRST, 'publish')]
        self.provider.withdraw(self.rows[target]['version_id'])
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assertEqual(result['citation_ids'], [])
        self.assertIsNone(self.provider.resolve_public_evidence(target, self.release, AS_OF))

    def test_expired_field_cannot_be_restored_by_field_priority(self):
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1, as_of='2026-10-20')
        self.assertEqual(result['citation_ids'], [])
        self.assertIn('expired_evidence', {gap['code'] for gap in result['coverage_gaps']})

    def test_cancelled_outbound_permission_blocks_field_candidates(self):
        self.provider.set_source_state(SOURCE, outbound_allowed=False)
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assertEqual(result['citation_ids'], [])
        self.assertIn('source_permission_missing', {gap['code'] for gap in result['coverage_gaps']})

    def test_unverified_identity_blocks_field_candidates(self):
        self.provider.register_product('WEBANK:' + FIRST, issuer='WEBANK_DISCLOSURE',
                                       scheme='webank_channel_product_code', code=FIRST,
                                       name=FIRST_NAME, verified=False)
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assertEqual(result['citation_ids'], [])
        self.assertIn('identity_unverified', {gap['code'] for gap in result['coverage_gaps']})

    def test_withdrawn_graph_provenance_blocks_even_exact_field_match(self):
        target = self.citations[(FIRST, 'publish')]
        with self.provider.db() as database:
            edges = [row[0] for row in database.execute(
                "SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'", (target,))]
        self.assertTrue(edges)
        for edge in edges:
            self.provider.withdraw_relation(edge)
        result = self.search(FIRST_NAME + '发行机构是哪家？', limit=1)
        self.assertNotIn(target, result['citation_ids'])
        self.assertIn('not_enough_evidence', {gap['code'] for gap in result['coverage_gaps']})


class PublicAliasAndViewSafetyTests(unittest.TestCase):
    def test_public_aliases_never_carry_dates_money_or_rates_from_query(self):
        projection = query_projection('欧洲央行2026-10-21利率从5.50%降到3.25%，我有100000元。')
        self.assertIsNotNone(projection)
        self.assertIn('ECB', projection)
        self.assertIn('interest rates', projection)
        self.assertNotRegex(projection, r'[0-9%￥¥]')
        self.assertNotIn('2026', projection)
        self.assertNotIn('100000', projection)

    def test_undefined_topics_are_not_translated_or_completed(self):
        self.assertIsNone(query_projection('九尾狐草药抗毒性与铯波量子套利如何关联？'))
        baseline = query_projection('欧洲央行利率')
        with_unknown = query_projection('欧洲央行利率以及九尾狐草药铯波量子套利')
        self.assertEqual(with_unknown, baseline)
        self.assertIsNone(query_projection('风险'))
        self.assertIsNone(query_projection('ECB interest rate 5.50%'))

    def test_semantic_views_use_independent_ranks_instead_of_maximum_cosine(self):
        original = {'original-first': .90, 'projection-first': .89}
        projection = {'original-first': .51, 'projection-first': .99}
        merged = {cid: max(original[cid], projection[cid]) for cid in original}
        result = fuse({}, merged, semantic_views={'original': original, 'public_concepts': projection})
        self.assertAlmostEqual(result['original-first'], result['projection-first'])
        # Changing one view's scale without changing its order cannot change fusion.
        rescaled = {'original-first': .71, 'projection-first': .999}
        changed = fuse({}, {'original-first': .90, 'projection-first': .999},
                       semantic_views={'original': original, 'public_concepts': rescaled})
        self.assertEqual(result, changed)

    def test_adding_three_semantic_views_does_not_triple_total_semantic_vote(self):
        only = fuse({}, {'shared-original': .95}, semantic_views={'primary:original': {'shared-original': .95}})
        three = fuse({}, {'shared-original': 0.0}, semantic_views={
            'primary:original': {'shared-original': .95},
            'primary:public_concepts': {'shared-original': .82},
            'secondary:original': {'shared-original': .43},
        })
        self.assertAlmostEqual(only['shared-original'], three['shared-original'])

    def test_public_concept_coverage_ignores_punctuation_and_latin_context(self):
        baseline = concept_coverage('欧洲央行利率风险')
        self.assertEqual(concept_coverage('ECB 2026 欧洲央行，利率！风险；USD 99.25%'), baseline)
        self.assertEqual(concept_coverage('  欧洲央行 利率 风险  '), baseline)

    def test_overlapping_or_repeated_public_terms_cannot_exceed_full_coverage(self):
        value = concept_coverage('证券融资融资抵押品抵押')
        self.assertGreater(value, 0.)
        self.assertLessEqual(value, 1.)
        self.assertEqual(value, 1.)

    def test_unmapped_or_english_query_coverage_does_not_imply_financial_support(self):
        self.assertEqual(concept_coverage('九尾狐草药铯波量子梦桥蜃景'), 0.)
        self.assertEqual(concept_coverage('ECB artificial intelligence risk'), 0.)


class ExplicitVectorBuilderSelectionTests(unittest.TestCase):
    def test_builder_default_uses_the_same_actual_selector_as_query_runtime(self):
        from scripts import build_local_vectors
        from local_knowledge.retrieval_encoder import get_retrieval_encoder
        choices = {'bge': object(), 'e5': object(), 'aligned': object()}
        for selected in choices:
            with self.subTest(selected=selected), \
                    patch.dict('os.environ', {'NAVREN_VECTOR_ENCODER': selected}), \
                    patch('local_knowledge.embeddings.get_encoder', return_value=choices['bge']), \
                    patch('local_knowledge.multilingual_embeddings.get_multilingual_encoder', return_value=choices['e5']), \
                    patch('local_knowledge.aligned_embeddings.get_aligned_encoder', return_value=choices['aligned']), \
                    patch('scripts.build_local_vectors.LocalKnowledgeProvider') as provider_factory, \
                    patch('sys.argv', ['build_local_vectors.py', '--database', 'unopened-fixture.sqlite3']), \
                    redirect_stdout(StringIO()):
                provider_factory.return_value.build_vector_index.return_value = {'ready': True}
                provider_factory.return_value.status.return_value = {'ready': True}
                expected = get_retrieval_encoder()
                self.assertIs(expected, choices[selected])
                self.assertEqual(build_local_vectors.main(), 0)
                provider_factory.assert_called_once_with(Path('unopened-fixture.sqlite3'),
                    release_id=None, encoder=expected)
                provider_factory.return_value.build_vector_index.assert_called_once_with(None)

    def test_builder_explicit_aligned_selection_overrides_environment_without_loading_bge(self):
        from scripts import build_local_vectors
        aligned = object()
        with patch.dict('os.environ', {'NAVREN_VECTOR_ENCODER': 'bge'}), \
                patch('local_knowledge.embeddings.get_encoder') as bge, \
                patch('local_knowledge.aligned_embeddings.get_aligned_encoder', return_value=aligned), \
                patch('scripts.build_local_vectors.LocalKnowledgeProvider') as provider_factory, \
                patch('sys.argv', ['build_local_vectors.py', '--database', 'unopened-fixture.sqlite3',
                                   '--encoder', 'aligned']), redirect_stdout(StringIO()):
            provider_factory.return_value.build_vector_index.return_value = {'ready': True}
            provider_factory.return_value.status.return_value = {'ready': True}
            self.assertEqual(build_local_vectors.main(), 0)
            bge.assert_not_called()
            provider_factory.assert_called_once_with(Path('unopened-fixture.sqlite3'),
                release_id=None, encoder=aligned)

    def test_explicit_dual_builder_supplies_bundle_without_eagerly_loading_secondary(self):
        from scripts import build_local_vectors
        primary = ConstantEncoder()
        with patch.dict('os.environ', {'NAVREN_VECTOR_ENCODER': 'bge'}), \
                patch('local_knowledge.multilingual_embeddings.get_multilingual_encoder', return_value=primary), \
                patch('local_knowledge.aligned_embeddings.get_aligned_encoder') as secondary_loader, \
                patch('scripts.build_local_vectors.LocalKnowledgeProvider') as provider_factory, \
                patch('sys.argv', ['build_local_vectors.py', '--database', 'unopened-fixture.sqlite3',
                                   '--encoder', 'dual']), redirect_stdout(StringIO()):
            provider_factory.return_value.build_vector_index.return_value = {'ready': True}
            provider_factory.return_value.status.return_value = {'ready': True}
            self.assertEqual(build_local_vectors.main(), 0)
            supplied = provider_factory.call_args.kwargs['encoder']
            self.assertIsInstance(supplied, EncoderBundle)
            self.assertIs(supplied.primary, primary)
            secondary_loader.assert_not_called()


class MarketContextBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='navren-context-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.provider = LocalKnowledgeProvider(Path(self.directory.name) / 'public.sqlite3', encoder=False)
        self.provider.register_source('fixture-market', publisher='合成官方资料源',
            endpoints=['https://market-fixture.example/'], parser_id=PARSER,
            freshness={'general': None}, shape='market-official-full-text-v1',
            permission_basis='isolated_synthetic_market_originals_only')
        self.version = self.document('合成甲机构报告', 'first')
        self.other_version = self.document('合成乙机构报告', 'second')
        self.release = self.provider.publish(version_ids=[self.version, self.other_version])
        self.refresh_rows()

    def document(self, title, key, *, source='fixture-market', shape='market-official-full-text-v1',
                 identities=None):
        parts = ['ECB artificial intelligence risk: synthetic list begins.',
                 'First synthetic area: governance and review.',
                 'Second synthetic area: accountable operations.',
                 'Third synthetic area: original source provenance.',
                 'Closing synthetic paragraph, no financial advice.']
        text = ''.join(parts)
        spans = []; offset = 0
        for index, part in enumerate(parts):
            span = {'start_char': offset, 'end_char': offset + len(part),
                    'paragraph': 'synthetic-list:' + str(index), 'facets': ['general']}
            if identities:
                span.update(identities[index])
            spans.append(span); offset += len(part)
        version = self.provider.stage_document(source_id=source,
            source_url='https://market-fixture.example/' + key, title=title, text=text,
            published_on=DISCLOSED_ON, retrieved_on=AS_OF, shape=shape, spans=spans,
            document_key=key, facets=['general'])
        self.provider.approve_version(version, verification_basis='isolated_market_original_review')
        return version

    def refresh_rows(self):
        with self.provider.db() as database:
            self.rows = [dict(row) for row in self.provider._rows(database, self.release)]
        self.first = sorted((row for row in self.rows if row['version_id'] == self.version),
                            key=lambda row: row['start_char'])
        self.second = sorted((row for row in self.rows if row['version_id'] == self.other_version),
                             key=lambda row: row['start_char'])

    def test_enumeration_adds_at_most_two_contiguous_same_version_neighbors(self):
        seed_ids = [self.first[0]['id'], self.first[1]['id']]
        actual = adjacent_context('合成甲机构报告列出哪些方面？', seed_ids, self.rows)
        self.assertEqual(actual, [row['id'] for row in self.first[:3]])
        self.assertLessEqual(len(actual), 3)

    def test_context_requires_first_two_hits_to_be_from_same_version(self):
        actual = adjacent_context('涉及哪些方面？', [self.first[0]['id'], self.second[0]['id']], self.rows)
        self.assertEqual(actual, [])
        self.assertEqual(adjacent_context('涉及哪些方面？', [self.first[0]['id']], self.rows), [])

    def test_comparison_and_two_named_reports_do_not_append_one_sided_context(self):
        ids = [self.first[0]['id'], self.first[1]['id']]
        for query in ('比较甲乙机构涉及哪些方面？', '甲乙机构各自涉及哪些方面？',
                      '合成甲机构报告和合成乙机构报告涉及哪些方面？'):
            with self.subTest(query=query):
                self.assertEqual(adjacent_context(query, ids, self.rows), [])

    def test_missing_eligible_neighbor_does_not_skip_across_a_gap(self):
        eligible = [row for row in self.rows if row['id'] != self.first[1]['id']]
        actual = adjacent_context('涉及哪些方面？', [self.first[0]['id'], self.first[2]['id']], eligible)
        self.assertEqual(actual, [])

    def test_plain_text_shape_cannot_gain_market_enumeration_context(self):
        self.provider.register_source('fixture-plain-context', publisher='合成普通文本源',
            endpoints=['https://market-fixture.example/'], parser_id=PARSER,
            freshness={'general': None}, shape='plain-text-v1',
            permission_basis='isolated_plain_original_only')
        version = self.document('合成普通原件', 'plain', source='fixture-plain-context', shape='plain-text-v1')
        release = self.provider.publish(version_ids=[version], activate=False, temporary=True)
        with self.provider.db() as database:
            rows = sorted((dict(row) for row in self.provider._rows(database, release)),
                          key=lambda row: row['start_char'])
        self.assertEqual(adjacent_context('列出哪些方面？', [rows[0]['id'], rows[1]['id']], rows), [])

    def test_different_product_share_or_channel_is_never_a_context_neighbor(self):
        for code in ('CONTEXT-A', 'CONTEXT-B'):
            self.provider.register_product(code, issuer='FIXTURE', scheme='isolated-context',
                                           code=code, name='合成对象' + code, verified=True)
            self.provider.register_share('SHARE-' + code, product_id=code,
                scheme='isolated-share', code='S-' + code, currency='CNY')
            self.provider.register_channel('CHANNEL-' + code, name='合成渠道' + code)
        first = {'product_id': 'CONTEXT-A', 'share_class_id': 'SHARE-CONTEXT-A', 'channel_id': 'CHANNEL-CONTEXT-A'}
        boundaries = [
            {'product_id': 'CONTEXT-B', 'share_class_id': 'SHARE-CONTEXT-B', 'channel_id': 'CHANNEL-CONTEXT-A'},
            {'product_id': 'CONTEXT-A', 'share_class_id': None, 'channel_id': 'CHANNEL-CONTEXT-A'},
            {'product_id': 'CONTEXT-A', 'share_class_id': 'SHARE-CONTEXT-A', 'channel_id': 'CHANNEL-CONTEXT-B'},
        ]
        for index, other in enumerate(boundaries):
            with self.subTest(boundary=index):
                version = self.document('合成边界报告' + str(index), 'boundary-' + str(index),
                                        identities=[first, first, other, first, first])
                release = self.provider.publish(version_ids=[version], activate=False, temporary=True)
                with self.provider.db() as database:
                    rows = sorted((dict(row) for row in self.provider._rows(database, release)),
                                  key=lambda row: row['start_char'])
                self.assertEqual(adjacent_context('列出哪些方面？', [rows[0]['id'], rows[1]['id']], rows),
                                 [rows[0]['id'], rows[1]['id']])

    def test_withdrawn_graph_neighbor_is_not_restored_by_context(self):
        removed = self.first[1]['id']
        with self.provider.db() as database:
            edges = [row[0] for row in database.execute(
                "SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'", (removed,))]
        for edge in edges:
            self.provider.withdraw_relation(edge)
        result = self.provider.search_public({'scope': 'public', 'query': '合成甲机构报告列出哪些方面？',
            'as_of': AS_OF, 'facets': ['general'], 'limit': 3}, self.release)
        self.assertNotIn(removed, result['citation_ids'])
        self.assertIsNone(self.provider.resolve_public_evidence(removed, self.release, AS_OF))
        from local_knowledge.graph import paths
        with self.provider.db() as database:
            eligible = []
            for row in self.provider._rows(database, self.release):
                facets, _ = self.provider._available(database, row, AS_OF)
                if facets and paths(database, row, AS_OF, facets=facets):
                    eligible.append(dict(row))
        self.assertEqual(adjacent_context('列出哪些方面？',
            [self.first[0]['id'], self.first[2]['id']], eligible), [])

    def test_both_query_views_resolve_only_unchanged_canonical_originals(self):
        before = {row['id']: (row['excerpt'], row['excerpt_sha256'], row['raw_sha256'], row['parsed_sha256'])
                  for row in self.rows}
        self.provider.vector_encoder = ConstantEncoder()
        self.provider.build_vector_index(self.release)
        result = self.provider.search_public({'scope': 'public',
            'query': '欧洲央行人工智能对金融机构风险有何影响？',
            'as_of': AS_OF, 'facets': ['general'], 'limit': 3}, self.release)
        self.assertTrue(result['retrieval']['query_projection_used'])
        self.assertEqual(result['retrieval']['semantic_view_count'], 2)
        self.assertTrue(result['citation_ids'])
        for cid in result['citation_ids']:
            canonical = self.provider.resolve_public_evidence(cid, self.release, AS_OF)
            self.assertEqual(canonical['excerpt'], before[cid][0])
            self.assertNotIn('query_projection', canonical)
        self.refresh_rows()
        after = {row['id']: (row['excerpt'], row['excerpt_sha256'], row['raw_sha256'], row['parsed_sha256'])
                 for row in self.rows}
        self.assertEqual(after, before)

    def bundle(self, *, build=True):
        primary, secondary = ConstantEncoder(), OtherSpaceEncoder()
        factory = Mock(return_value=secondary)
        bundle = EncoderBundle(primary, factory)
        self.provider.vector_encoder = bundle
        if build:
            self.provider.build_vector_index(self.release)
        factory.reset_mock()
        return bundle, primary, secondary, factory

    def bundle_search(self, query='欧洲央行 synthetic 九尾狐草药铯波量子套利梦桥蜃景怎样影响风险？'):
        return self.provider.search_public({'scope': 'public', 'query': query,
            'as_of': AS_OF, 'facets': ['general'], 'limit': 3}, self.release)

    def corrupt_profile(self, encoder):
        with self.provider.db() as database:
            database.execute('UPDATE chunk_vectors SET vector=? WHERE release_id=? AND profile_id=?',
                             (b'corrupt-single-space-fixture', self.release, encoder.profile_id))

    def test_dual_build_and_status_keep_two_dimension_and_profile_manifests_separate(self):
        bundle, primary, secondary, _ = self.bundle()
        with self.provider.db() as database:
            profiles = {row['profile_id']: dict(row) for row in database.execute(
                'SELECT * FROM vector_releases WHERE release_id=?', (self.release,))}
            self.assertEqual(set(profiles), {primary.profile_id, secondary.profile_id})
            self.assertEqual(profiles[primary.profile_id]['dimension'], 4)
            self.assertEqual(profiles[secondary.profile_id]['dimension'], 3)
            for encoder in (primary, secondary):
                self.assertEqual(profiles[encoder.profile_id]['chunk_count'], len(self.rows))
                self.assertTrue(VectorIndex(self.provider, encoder).status(database, self.release)['ready'])
            status = VectorIndex(self.provider, bundle).status(database, self.release)
            self.assertTrue(status['ready'])
            self.assertEqual({p['profile_id'] for p in status['profiles']}, set(profiles))

    def test_bundle_scores_are_only_membership_and_views_keep_each_math_space(self):
        bundle, primary, secondary, _ = self.bundle()
        with self.provider.db() as database:
            result = VectorIndex(self.provider, bundle).search(database, self.release,
                '合成跨语问题', set(row['id'] for row in self.rows),
                projected_query='synthetic public concepts', cross_language=True)
        self.assertEqual(result['mode'], 'vector')
        self.assertEqual(set(result['views']), {'0:original', '0:public_concepts', '1:original'})
        self.assertEqual(set(result['scores'].values()), {0.0})
        self.assertEqual({branch['profile_id'] for branch in result['branches']},
                         {primary.profile_id, secondary.profile_id})

    def test_primary_index_corruption_keeps_secondary_with_explicit_partial_reason(self):
        _, primary, secondary, _ = self.bundle()
        self.corrupt_profile(primary)
        result = self.bundle_search()
        self.assertEqual(result['retrieval']['mode'], 'hybrid')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_partial')
        self.assertTrue(result['retrieval']['vector_used'])
        branches = {branch['profile_id']: branch for branch in result['retrieval']['vector_branches']}
        self.assertEqual(branches[primary.profile_id]['reason'], 'vector_index_invalid')
        self.assertEqual(branches[secondary.profile_id]['mode'], 'vector')
        self.assertEqual(result['retrieval']['semantic_view_count'], 1)
        for cid in result['citation_ids']:
            self.assertIsNotNone(self.provider.resolve_public_evidence(cid, self.release, AS_OF))

    def test_secondary_index_corruption_keeps_primary_with_explicit_partial_reason(self):
        _, primary, secondary, _ = self.bundle()
        self.corrupt_profile(secondary)
        result = self.bundle_search()
        self.assertEqual(result['retrieval']['mode'], 'hybrid')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_partial')
        self.assertTrue(result['retrieval']['vector_used'])
        branches = {branch['profile_id']: branch for branch in result['retrieval']['vector_branches']}
        self.assertEqual(branches[primary.profile_id]['mode'], 'vector')
        self.assertEqual(branches[secondary.profile_id]['reason'], 'vector_index_invalid')
        self.assertEqual(result['retrieval']['semantic_view_count'], 2)

    def test_secondary_model_missing_keeps_primary_and_reports_failure_without_exception_text(self):
        _, primary, _, _ = self.bundle()
        failure = Mock(side_effect=VectorError('vector_model_missing'))
        self.provider.vector_encoder = EncoderBundle(primary, failure)
        result = self.bundle_search()
        self.assertEqual(result['retrieval']['mode'], 'hybrid')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_partial')
        self.assertTrue(result['retrieval']['vector_used'])
        self.assertEqual(result['retrieval']['vector_branches'][1]['reason'], 'vector_model_missing')
        self.assertIsNone(result['retrieval']['vector_branches'][1]['profile_id'])

    def test_explicit_dual_primary_model_missing_still_uses_healthy_secondary(self):
        secondary = OtherSpaceEncoder()
        VectorIndex(self.provider, secondary).build(self.release)
        self.provider.vector_encoder = None
        with patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '1', 'NAVREN_VECTOR_ENCODER': 'dual'}), \
                patch('local_knowledge.multilingual_embeddings.get_multilingual_encoder',
                      side_effect=VectorError('vector_model_missing')), \
                patch('local_knowledge.aligned_embeddings.get_aligned_encoder', return_value=secondary):
            result = self.bundle_search()
        self.assertEqual(result['retrieval']['mode'], 'hybrid')
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_partial')
        self.assertTrue(result['retrieval']['vector_used'])
        self.assertTrue(any(branch['profile_id'] == secondary.profile_id and branch['mode'] == 'vector'
                            for branch in result['retrieval']['vector_branches']))

    def test_both_spaces_invalid_degrade_to_the_same_local_fts_results(self):
        bundle, primary, secondary, _ = self.bundle()
        self.provider.vector_encoder = False
        baseline = self.bundle_search()
        self.provider.vector_encoder = bundle
        self.corrupt_profile(primary); self.corrupt_profile(secondary)
        result = self.bundle_search()
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertFalse(result['retrieval']['vector_used'])
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_index_invalid')
        self.assertEqual(result['citation_ids'], baseline['citation_ids'])
        self.assertTrue(all(branch['mode'] == 'fts5' for branch in result['retrieval']['vector_branches']))

    def test_non_cross_language_query_never_loads_secondary_or_builds_on_read(self):
        _, _, _, factory = self.bundle()
        with patch.object(VectorIndex, 'build', side_effect=AssertionError('read_side_must_not_build')):
            result = self.bundle_search('ECB artificial intelligence risk')
        factory.assert_not_called()
        self.assertFalse(result['retrieval']['cross_language_route'])
        self.assertEqual(len(result['retrieval']['vector_branches']), 1)
        self.assertEqual(result['retrieval']['mode'], 'hybrid')

    def test_missing_secondary_index_is_not_built_during_cross_language_search(self):
        bundle, primary, secondary, factory = self.bundle(build=False)
        VectorIndex(self.provider, primary).build(self.release)
        with patch.object(VectorIndex, 'build', side_effect=AssertionError('read_side_must_not_build')):
            result = self.bundle_search()
        factory.assert_called_once()
        self.assertEqual(result['retrieval']['vector_reason'], 'vector_partial')
        self.assertEqual(result['retrieval']['vector_branches'][1]['reason'], 'vector_index_missing')
        with self.provider.db() as database:
            self.assertEqual(database.execute('SELECT count(*) FROM vector_releases WHERE profile_id=?',
                                             (secondary.profile_id,)).fetchone()[0], 0)

    def test_fully_covered_public_terms_keep_primary_views_without_loading_secondary(self):
        _, _, _, factory = self.bundle()
        result = self.bundle_search('欧洲央行人工智能金融机构风险')
        factory.assert_not_called()
        self.assertFalse(result['retrieval']['cross_language_route'])
        self.assertEqual(result['retrieval']['public_concept_coverage'], 1.)
        self.assertEqual(result['retrieval']['semantic_view_count'], 2)

    def test_english_question_with_chinese_evidence_can_enable_secondary_space(self):
        self.provider.register_source('fixture-chinese-market', publisher='合成中文资料源',
            endpoints=['https://market-fixture.example/'], parser_id=PARSER,
            freshness={'general': None}, shape='market-official-full-text-v1',
            permission_basis='isolated_chinese_original_only')
        version = self.provider.stage_document(source_id='fixture-chinese-market',
            source_url='https://market-fixture.example/chinese', title='合成中文公开资料',
            text='这是一段经过核对的合成中文原件，只用于验证语言路由边界，不能推断收益、费率或购买资格。',
            published_on=DISCLOSED_ON, retrieved_on=AS_OF, facets=['general'],
            shape='market-official-full-text-v1')
        self.provider.approve_version(version, verification_basis='isolated_chinese_original_review')
        self.release = self.provider.publish(version_ids=[self.version, self.other_version, version])
        _, _, _, factory = self.bundle()
        result = self.bundle_search('What patterns does the public research discuss?')
        factory.assert_called_once()
        self.assertTrue(result['retrieval']['cross_language_route'])
        self.assertEqual(result['retrieval']['public_concept_coverage'], 0.)
        self.assertEqual(len(result['retrieval']['vector_branches']), 2)


if __name__ == '__main__':
    unittest.main()
