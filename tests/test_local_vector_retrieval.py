"""模拟 encoder 的真实 SQLite 向量索引风险回归。

仅验证持久化、混合召回与证据边界，不能证明真实模型的语义质量。
所有资料和索引均在临时目录，不访问生产库、网络或付费模型。
"""
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
from local_knowledge import LocalKnowledgeProvider, KnowledgeError
from local_knowledge.store import packed, sha
from local_knowledge.vectors import VectorIndex, fuse, schedule


AS_OF = '2026-10-06'
URL = 'https://official.example/terms'
ORAL_QUERY = '急用钱想拿出来要等多久'
QUOTE = '赎回申请后两个工作日到账。渠道条件以原文为准。'


class FixtureEncoder:
    """明确为模拟向量；固定方向便于构造反例，不进行模型推理。"""
    dimension = 512
    profile_id = 'fixture-only-not-a-real-model/512-v1'

    def __init__(self, *, windows=1):
        self.windows = windows
        self.documents = []
        self.queries = []

    @staticmethod
    def direction(index):
        value = np.zeros(512, dtype=np.float32)
        value[index] = 1.0
        return value

    def encode_query(self, query):
        self.queries.append(query)
        return self.direction(0 if '急用钱' in query or '赎回' in query else 7)

    def encode_documents(self, texts):
        self.documents.extend(texts)
        result = []
        for text in texts:
            index = 0 if '赎回申请' in text else 3 if '费用' in text else 2
            windows = [self.direction(index)]
            if self.windows > 1:
                windows.insert(0, self.direction(4))
            result.append(np.stack(windows))
        return result


class LocalVectorRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='navren-vector-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'public.sqlite3'
        self.encoder = FixtureEncoder()
        self.provider = LocalKnowledgeProvider(self.path, encoder=self.encoder)
        self.provider.register_source('official', publisher='合成官方源',
            endpoints=['https://official.example/'], shape='plain-text-v1',
            freshness={'fees': None, 'liquidity': None, 'terms': None,
                       'risk': None, 'general': 30, 'returns': 7},
            permission_basis='synthetic_fixture_only')
        for product in ('PRODUCT-A', 'PRODUCT-B'):
            self.provider.register_product(product, issuer='SYNTHETIC', scheme='fixture',
                code=product, name='测试产品'+product, verified=True)
        self.provider.register_share('SHARE-A', product_id='PRODUCT-A',
            scheme='fixture-share', code='001A', currency='CNY')
        self.provider.register_share('SHARE-C', product_id='PRODUCT-A',
            scheme='fixture-share', code='001C', currency='CNY')
        self.provider.register_channel('CHANNEL-A', name='测试渠道甲')
        self.provider.register_channel('CHANNEL-B', name='测试渠道乙')

    def stage(self, **changes):
        values = dict(source_id='official', source_url=URL, title='合成流动性条款',
            text=QUOTE, published_on='2026-09-01', retrieved_on=AS_OF,
            effective_from='2026-09-01', effective_to='2026-11-01',
            product_id='PRODUCT-A', share_class_id='SHARE-A', channel_id='CHANNEL-A',
            facets=['liquidity', 'terms'])
        values.update(changes)
        return self.provider.stage_document(**values)

    def active(self, **changes):
        version = self.stage(**changes)
        self.provider.approve_version(version, verification_basis='synthetic_test_verified')
        release = self.provider.publish(version_ids=[version])
        return version, release

    def search(self, release, query=ORAL_QUERY, provider=None, **changes):
        # 不给 product+facets，避免既有维度补召回伪装成向量改善。
        request = dict(scope='public', query=query, as_of=AS_OF, facets=[], limit=8)
        request.update(changes)
        return (provider or self.provider).search_public(request, release)

    def index_status(self, release, provider=None, encoder=None):
        selected = provider or self.provider
        with selected.db() as db:
            return VectorIndex(selected, encoder=encoder or self.encoder).status(db, release)

    def indexed_ids(self, release):
        with self.provider.db() as db:
            return {row[0] for row in db.execute(
                'SELECT citation_id FROM chunk_vectors WHERE release_id=?', (release,))}

    def member_ids(self, release):
        with self.provider.db() as db:
            return {row[0] for row in db.execute(
                'SELECT chunk_id FROM release_chunks WHERE release_id=?', (release,))}

    def assert_no_hits(self, release, **request):
        self.assertEqual(self.search(release, **request)['citation_ids'], [])

    def assert_index_degraded(self, release):
        self.assertFalse(self.index_status(release)['ready'])
        result = self.search(release, query='赎回申请')
        self.assertTrue(result['citation_ids'])  # 独立 FTS 证据仍有效。
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertTrue(result['retrieval']['vector_reason'])
        self.assertEqual(result['retrieval']['vector_candidate_count'], 0)

    def reseal_vector_manifest_for_corruption_test(self, release):
        # 有意重写派生清单，用于证明仅有 hash 一致仍不能放行 NaN/非单位向量。
        with self.provider.db() as db:
            rows = db.execute('SELECT * FROM chunk_vectors WHERE release_id=? AND profile_id=? '
                'ORDER BY citation_id,window_no', (release, self.encoder.profile_id)).fetchall()
            manifest = sha(packed([(row['citation_id'], row['window_no'], row['text_sha256'],
                                   row['vector_sha256']) for row in rows]))
            db.execute('UPDATE vector_releases SET vector_manifest=? WHERE release_id=? AND profile_id=?',
                       (manifest, release, self.encoder.profile_id))

    def test_simulated_semantic_candidate_improves_zero_fts_baseline(self):
        _, release = self.active()
        before = self.search(release)
        self.assertEqual(before['citation_ids'], [])
        self.assertEqual(before['retrieval']['mode'], 'fts5')
        self.provider.build_vector_index(release)
        after = self.search(release)
        self.assertEqual(set(after['citation_ids']), self.member_ids(release))
        self.assertEqual(after['retrieval']['mode'], 'hybrid')
        self.assertIsNone(after['retrieval']['vector_reason'])
        hit = self.provider.resolve_public_evidence(after['citation_ids'][0], release, AS_OF)
        self.assertEqual(hit['excerpt'], QUOTE)
        self.assertEqual(hit['product_id'], 'PRODUCT-A')

    def test_missing_index_uses_fts_and_reports_degradation(self):
        _, release = self.active()
        result = self.search(release, query='赎回申请')
        self.assertTrue(result['citation_ids'])
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertTrue(result['retrieval']['vector_reason'])
        self.assertFalse(self.index_status(release)['ready'])

    def test_local_index_persists_without_reencoding_documents(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        first = self.indexed_ids(release)
        another_encoder = FixtureEncoder()
        reopened = LocalKnowledgeProvider(self.path, encoder=another_encoder)
        status = self.index_status(release, provider=reopened, encoder=another_encoder)
        self.assertTrue(status['ready'])
        result = self.search(release, provider=reopened)
        self.assertEqual(set(result['citation_ids']), first)
        self.assertEqual(another_encoder.documents, [])

    def test_build_indexes_only_frozen_release_members(self):
        _, release = self.active()
        unpublished = self.stage(source_url='https://official.example/unpublished',
            title='已审核未发布', text='赎回申请未发布特殊条款。')
        self.provider.approve_version(unpublished, verification_basis='fixture')
        candidate = self.stage(source_url='https://official.example/candidate',
            title='未审核候选', text='赎回申请候选特殊条款。')
        self.provider.build_vector_index(release)
        self.assertEqual(self.indexed_ids(release), self.member_ids(release))
        self.assertNotIn('未发布特殊条款', '\n'.join(self.encoder.documents))
        self.assertNotIn('候选特殊条款', '\n'.join(self.encoder.documents))
        with self.provider.db() as db:
            excluded = {row[0] for row in db.execute(
                'SELECT id FROM chunks WHERE version_id IN (?,?)', (unpublished, candidate))}
        self.assertTrue(self.indexed_ids(release).isdisjoint(excluded))

    def test_build_default_release_is_idempotent(self):
        _, release = self.active()
        self.provider.build_vector_index()
        self.assertTrue(self.index_status(release)['ready'])
        with self.provider.db() as db:
            before = db.execute('SELECT count(*) FROM chunk_vectors').fetchone()[0]
        self.provider.build_vector_index(release)
        with self.provider.db() as db:
            after = db.execute('SELECT count(*) FROM chunk_vectors').fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(self.indexed_ids(release), self.member_ids(release))

    def test_product_share_and_channel_constraints_survive_high_similarity(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        for constraint in ({'product_id': 'PRODUCT-B'}, {'share_class_id': 'SHARE-C'},
                           {'channel_id': 'CHANNEL-B'}):
            with self.subTest(constraint=constraint):
                self.assert_no_hits(release, **constraint)
        valid = self.search(release, product_id='PRODUCT-A', share_class_id='SHARE-A',
                            channel_id='CHANNEL-A')
        self.assertTrue(valid['citation_ids'])

    def test_wrong_product_candidates_do_not_starve_requested_product(self):
        versions = []
        for i in range(10):
            version = self.stage(source_url=f'https://official.example/wrong-{i}',
                text=f'赎回申请错误对象条款{i}。', product_id='PRODUCT-B',
                share_class_id=None, channel_id=None)
            self.provider.approve_version(version, verification_basis='fixture')
            versions.append(version)
        correct = self.stage(source_url='https://official.example/correct')
        self.provider.approve_version(correct, verification_basis='fixture')
        release = self.provider.publish(version_ids=[*versions, correct])
        self.provider.build_vector_index(release)
        result = self.search(release, product_id='PRODUCT-A', limit=1)
        self.assertEqual(len(result['citation_ids']), 1)
        hit = self.provider.resolve_public_evidence(result['citation_ids'][0], release, AS_OF)
        self.assertEqual(hit['product_id'], 'PRODUCT-A')

    def test_time_bounds_and_freshness_survive_high_similarity(self):
        cases = [({'published_on': '2026-10-07'}, {}),
                 ({'effective_from': '2026-10-07'}, {}),
                 ({'effective_to': AS_OF}, {}),
                 ({'data_on': '2026-10-07'}, {}),
                 ({'facets': ['returns'], 'data_on': '2026-09-01'}, {})]
        for i, (document, request) in enumerate(cases):
            with self.subTest(document=document):
                version = self.stage(source_url=f'https://official.example/time-{i}', **document)
                self.provider.approve_version(version, verification_basis='fixture')
                release = self.provider.publish(version_ids=[version])
                self.provider.build_vector_index(release)
                self.assert_no_hits(release, **request)

    def test_dynamic_withdrawal_invalidates_residual_index_and_consumer(self):
        version, release = self.active()
        self.provider.build_vector_index(release)
        consumer = KnowledgeRetrieval(self.provider)
        delivered = consumer.search(ORAL_QUERY, as_of=AS_OF)
        self.assertTrue(delivered['hits'])
        self.provider.withdraw(version)
        self.assertTrue(self.indexed_ids(release))
        self.assert_no_hits(release)
        with self.assertRaisesRegex(ValueError, 'stale_knowledge_evidence'):
            consumer.assert_fresh()

    def test_revoked_permission_and_allowlist_deny_residual_vector(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        self.provider.set_source_state('official', outbound_allowed=False)
        self.assert_no_hits(release)
        self.provider.set_source_state('official', outbound_allowed=True)
        self.assertTrue(self.search(release)['citation_ids'])
        self.provider.register_source('official', publisher='合成官方源',
            endpoints=['https://official.example/other/'], shape='plain-text-v1',
            freshness={'liquidity': None, 'terms': None}, permission_basis='fixture_revised')
        self.assert_no_hits(release)

    def test_missing_graph_source_path_does_not_bypass_with_vectors(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        cid = self.search(release)['citation_ids'][0]
        with self.provider.db() as db:
            relation = db.execute("SELECT id FROM graph_relations WHERE citation_id=? "
                                  "AND predicate='quoted_in'", (cid,)).fetchone()[0]
        self.provider.withdraw_relation(relation)
        result = self.search(release)
        self.assertEqual(result['citation_ids'], [])
        self.assertIn('not_enough_evidence', {gap['code'] for gap in result['coverage_gaps']})
        self.assertIsNone(self.provider.resolve_public_evidence(cid, release, AS_OF))

    def test_confirmed_revision_does_not_resurrect_old_index(self):
        old, release = self.active()
        self.provider.build_vector_index(release)
        new = self.stage(text='赎回申请后五个工作日到账。',
            published_on='2026-10-01', effective_from='2026-10-01')
        self.provider.approve_version(new, verification_basis='fixture_confirmed_revision')
        self.assert_no_hits(release)
        past = self.search(release, as_of='2026-09-20')
        self.assertTrue(past['citation_ids'])
        hit = self.provider.resolve_public_evidence(past['citation_ids'][0], release, '2026-09-20')
        self.assertEqual(hit['document_version_id'], old)

    def test_pinned_and_temporary_releases_do_not_cross_index_membership(self):
        version, release = self.active()
        self.provider.build_vector_index(release)
        temporary = self.provider.publish(version_ids=[version], activate=False, temporary=True)
        self.provider.build_vector_index(temporary)
        extra = self.stage(source_url='https://official.example/new', text='赎回申请新增说明。')
        self.provider.approve_version(extra, verification_basis='fixture')
        latest = self.provider.publish(version_ids=[version, extra])
        self.provider.build_vector_index(latest)
        fixed = self.provider.for_release(release)
        result = self.search(release, provider=fixed)
        self.assertEqual(set(result['citation_ids']), self.member_ids(release))
        result = self.search(temporary)
        self.assertEqual(set(result['citation_ids']), self.member_ids(temporary))
        self.assertGreater(len(self.member_ids(latest)), len(result['citation_ids']))

    def test_unrelated_query_does_not_return_nearest_neighbor(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        self.assert_no_hits(release, query='明天是否下雨')

    def test_multiple_windows_and_hybrid_duplicates_return_one_citation(self):
        self.encoder.windows = 2
        _, release = self.active()
        self.provider.build_vector_index(release)
        result = self.search(release, query='赎回申请')
        self.assertEqual(len(result['citation_ids']), 1)
        with self.provider.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chunk_vectors WHERE release_id=?',
                                        (release,)).fetchone()[0], 2)
        self.assertEqual(result['retrieval']['mode'], 'hybrid')

    def test_rank_fusion_is_scale_independent_and_rewards_both_retrievers(self):
        lexical = {'both': -1.0, 'lexical-only': -1_000_000.0}
        semantic = {'both': 0.80, 'vector-only': 0.90}
        first = fuse(lexical, semantic)
        rescaled = fuse({cid: score*1000 for cid, score in lexical.items()},
                        {cid: score/1000 for cid, score in semantic.items()})
        self.assertEqual(first, rescaled)
        self.assertLess(first['both'], first['lexical-only'])
        self.assertLess(first['both'], first['vector-only'])

    def test_high_similarity_mixed_conflicted_span_is_wholly_rejected(self):
        mixed = self.stage(text='赎回申请后两个工作日到账。费用0.10%。',
                           facets=['fees', 'liquidity'])
        risk = self.stage(source_url='https://official.example/independent-risk',
                          text='风险披露：可能损失本金。', facets=['risk'])
        for version in (mixed, risk):
            self.provider.approve_version(version, verification_basis='fixture')
        release = self.provider.publish(version_ids=[mixed, risk])
        self.provider.build_vector_index(release)
        self.provider.block_conflict(product_id='PRODUCT-A', share_class_id='SHARE-A',
            channel_id='CHANNEL-A', facets=['fees'], from_on='2026-10-01', reason='fixture_conflict')
        self.assert_no_hits(release)
        result = self.search(release, query='风险', facets=['fees', 'risk'])
        self.assertEqual(len(result['citation_ids']), 1)
        hit = self.provider.resolve_public_evidence(result['citation_ids'][0], release, AS_OF)
        self.assertEqual(hit['facets'], ['risk'])
        self.assertNotIn('0.10%', hit['excerpt'])
        self.assertIn('fees', {gap.get('facet') for gap in result['coverage_gaps']
                              if gap['code'] == 'facet_uncovered'})

    def test_query_and_private_markers_do_not_persist_or_leave_local_encoder(self):
        _, release = self.active()
        question = '私密姓名测试甲13812345678存款8754321元，急用钱想拿出来要等多久'
        with patch('urllib.request.urlopen', side_effect=AssertionError('network_forbidden')), \
             patch('urllib.request.build_opener', side_effect=AssertionError('network_forbidden')):
            self.provider.build_vector_index(release)
            result = self.search(release, query=question)
        self.assertTrue(result['citation_ids'])
        raw = self.path.read_bytes()
        for marker in (question, '私密姓名测试甲', '13812345678', '8754321'):
            self.assertNotIn(marker.encode(), raw)
            self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(question, self.encoder.documents)

    def test_personal_database_cannot_be_opened_for_public_vectors(self):
        path = Path(self.directory.name)/'personal.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE workspaces(id TEXT PRIMARY KEY)')
        with self.assertRaisesRegex(KnowledgeError, 'personal_database_forbidden'):
            LocalKnowledgeProvider(path, encoder=self.encoder)

    def test_metadata_manifest_dimension_and_cardinality_corruption_degrade(self):
        mutations = [('release_manifest', '0'*64), ('vector_manifest', '0'*64),
                     ('dimension', 511), ('chunk_count', 2), ('vector_count', 2)]
        for field, value in mutations:
            with self.subTest(field=field):
                _, release = self.active(source_url='https://official.example/meta-'+field)
                self.provider.build_vector_index(release)
                with self.provider.db() as db:
                    db.execute('UPDATE vector_releases SET '+field+'=? WHERE release_id=?',
                               (value, release))
                self.assert_index_degraded(release)

    def test_vector_blob_hash_text_and_window_corruption_degrade(self):
        mutations = [('vector', b'truncated-vector'), ('vector_sha256', '0'*64),
                     ('text_sha256', '0'*64), ('window_no', 3)]
        for field, value in mutations:
            with self.subTest(field=field):
                _, release = self.active(source_url='https://official.example/vector-'+field)
                self.provider.build_vector_index(release)
                with self.provider.db() as db:
                    db.execute('UPDATE chunk_vectors SET '+field+'=? WHERE release_id=?',
                               (value, release))
                self.assert_index_degraded(release)

    def test_deleted_vector_member_degrades_instead_of_partial_ready(self):
        self.encoder.windows = 2
        _, release = self.active()
        self.provider.build_vector_index(release)
        with self.provider.db() as db:
            db.execute('DELETE FROM chunk_vectors WHERE release_id=? AND window_no=1', (release,))
        self.assert_index_degraded(release)

    def test_nonfinite_or_nonunit_vector_rejected_even_with_recomputed_hashes(self):
        for label, value in (('nan', float('nan')), ('inf', float('inf')), ('nonunit', 2.0)):
            with self.subTest(label=label):
                _, release = self.active(source_url='https://official.example/numeric-'+label)
                self.provider.build_vector_index(release)
                vector = self.encoder.direction(0)
                vector[0] = value
                blob = vector.astype('<f4').tobytes()
                with self.provider.db() as db:
                    db.execute('UPDATE chunk_vectors SET vector=?,vector_sha256=? WHERE release_id=?',
                               (blob, sha(blob), release))
                self.reseal_vector_manifest_for_corruption_test(release)
                self.assert_index_degraded(release)

    def test_model_profile_change_cannot_silently_use_old_vectors(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        changed = FixtureEncoder()
        changed.profile_id = 'fixture-only-not-a-real-model/512-v2'
        reopened = LocalKnowledgeProvider(self.path, encoder=changed)
        self.assertFalse(self.index_status(release, provider=reopened, encoder=changed)['ready'])
        result = self.search(release, query='赎回申请', provider=reopened)
        self.assertTrue(result['citation_ids'])
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertTrue(result['retrieval']['vector_reason'])
        self.assertEqual(changed.documents, [])

    def test_vector_search_only_scores_provided_eligible_release_members(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        other = self.stage(source_url='https://official.example/other-release',
                           text='赎回申请另一版本的说明。')
        self.provider.approve_version(other, verification_basis='fixture')
        another_release = self.provider.publish(version_ids=[other])
        self.provider.build_vector_index(another_release)
        foreign_ids = self.member_ids(another_release)
        with self.provider.db() as db:
            result = VectorIndex(self.provider, self.encoder).search(db, release, ORAL_QUERY, foreign_ids)
        self.assertEqual(result['scores'], {})
        self.assertEqual(result['mode'], 'vector')
        with self.provider.db() as db:
            result = VectorIndex(self.provider, self.encoder).search(db, release, ORAL_QUERY, set())
        self.assertEqual(result['scores'], {})

    def test_query_encoder_failure_and_bad_vectors_fail_without_error_text_leak(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        values = [np.zeros(512, dtype=np.float32), np.ones(511, dtype=np.float32),
                  np.full(512, float('nan'), dtype=np.float32)]
        for value in values:
            with self.subTest(shape=value.shape), patch.object(self.encoder, 'encode_query', return_value=value):
                result = self.search(release, query='赎回申请')
                self.assertTrue(result['citation_ids'])
                self.assertEqual(result['retrieval']['mode'], 'fts5')
                self.assertTrue(result['retrieval']['vector_reason'])
        with patch.object(self.encoder, 'encode_query',
                          side_effect=RuntimeError('秘密查询/Users/private/token')):
            result = self.search(release, query='赎回申请')
        self.assertTrue(result['citation_ids'])
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.assertNotIn('秘密', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('/Users/private', json.dumps(result, ensure_ascii=False))

    def test_build_failure_is_atomic_and_does_not_replace_previous_profile(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        changed = FixtureEncoder()
        changed.profile_id = 'fixture-only-failed-build/512-v2'
        bad = [np.zeros((1, 512), dtype=np.float32)]
        with patch.object(changed, 'encode_documents', return_value=bad), \
             self.assertRaises(KnowledgeError):
            VectorIndex(self.provider, changed).build(release)
        self.assertTrue(self.index_status(release)['ready'])
        with self.provider.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM vector_releases WHERE profile_id=?',
                                        (changed.profile_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM chunk_vectors WHERE profile_id=?',
                                        (changed.profile_id,)).fetchone()[0], 0)

    def test_vector_read_does_not_treat_corrupt_raw_original_as_evidence(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER immutable_raw')
            db.execute('UPDATE raw_artifacts SET payload=?', (b'corrupt-original',))
        self.assert_no_hits(release)
        consumer = KnowledgeRetrieval(self.provider)
        self.assertEqual(consumer.search(ORAL_QUERY, as_of=AS_OF)['hits'], [])

    def test_corrupt_release_seal_rejects_vector_build_and_canonical_delivery(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER immutable_release')
            db.execute('UPDATE releases SET manifest_sha256=? WHERE id=?', ('0'*64, release))
        self.assertFalse(self.index_status(release)['ready'])
        with self.assertRaisesRegex(KnowledgeError, 'release_manifest_mismatch'):
            self.provider.build_vector_index(release)
        # 原文 resolver 是最后权威门禁，即使 FTS 仍提供候选 ID 也不得交付正文。
        consumer = KnowledgeRetrieval(self.provider)
        self.assertEqual(consumer.search('赎回申请', as_of=AS_OF)['hits'], [])

    def test_revocation_during_encoding_prevents_index_commit(self):
        for case in ('withdrawal', 'outbound_permission'):
            with self.subTest(case=case):
                version, release = self.active(source_url='https://official.example/during-'+case)
                original = self.encoder.encode_documents
                def revoke_then_encode(texts):
                    if case == 'withdrawal':
                        self.provider.withdraw(version)
                    else:
                        self.provider.set_source_state('official', outbound_allowed=False)
                    return original(texts)
                with patch.object(self.encoder, 'encode_documents', side_effect=revoke_then_encode), \
                     self.assertRaises(KnowledgeError):
                    self.provider.build_vector_index(release)
                self.assertEqual(self.indexed_ids(release), set())
                self.assertFalse(self.index_status(release)['ready'])
                self.provider.set_source_state('official', outbound_allowed=True)

    def test_last_encoding_batch_past_deadline_does_not_commit_index(self):
        _, release = self.active()
        elapsed = [0.0]
        original = self.encoder.encode_documents
        def over_deadline(texts):
            result = original(texts)
            elapsed[0] = 301.0
            return result
        with patch('local_knowledge.vectors.time.monotonic', side_effect=lambda: elapsed[0]), \
             patch.object(self.encoder, 'encode_documents', side_effect=over_deadline), \
             self.assertRaisesRegex(KnowledgeError, 'vector_build_timeout'):
            VectorIndex(self.provider, self.encoder).build(release, max_seconds=300)
        self.assertEqual(self.indexed_ids(release), set())

    def test_two_instances_building_same_release_produce_complete_index(self):
        _, release = self.active()
        barrier = threading.Barrier(2)
        encoders = [FixtureEncoder(), FixtureEncoder()]
        providers = [LocalKnowledgeProvider(self.path, encoder=encoder) for encoder in encoders]
        originals = [encoder.encode_documents for encoder in encoders]
        def synchronize(original):
            def encoded(texts):
                barrier.wait(timeout=5)
                return original(texts)
            return encoded
        with patch.object(encoders[0], 'encode_documents', side_effect=synchronize(originals[0])), \
             patch.object(encoders[1], 'encode_documents', side_effect=synchronize(originals[1])), \
             ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(provider.build_vector_index, release) for provider in providers]
            results = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(result['ready'] for result in results))
        self.assertTrue(self.index_status(release)['ready'])
        self.assertEqual(self.indexed_ids(release), self.member_ids(release))
        with self.provider.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM vector_releases WHERE release_id=?',
                                        (release,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM chunk_vectors WHERE release_id=?',
                                        (release,)).fetchone()[0], 1)

    def test_failed_metadata_commit_rolls_back_same_profile_vector_replacement(self):
        _, release = self.active()
        self.provider.build_vector_index(release)
        with self.provider.db() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM chunk_vectors ORDER BY citation_id')]
            before_meta = tuple(db.execute('SELECT * FROM vector_releases WHERE release_id=?',
                                           (release,)).fetchone())
        original_db = self.provider.db
        class FailedMetadataCommit:
            def __init__(self, db):
                self.db = db
            def __getattr__(self, key):
                return getattr(self.db, key)
            def execute(self, sql, args=()):
                if sql.startswith('INSERT OR REPLACE INTO vector_releases'):
                    raise sqlite3.IntegrityError('simulated_metadata_commit_failure')
                return self.db.execute(sql, args)
        @contextmanager
        def database_with_commit_fault():
            with original_db() as db:
                yield FailedMetadataCommit(db)
        index = VectorIndex(self.provider, self.encoder)
        original_load = index._load
        count = [0]
        def rebuild_once(db, selected_release, encoder):
            count[0] += 1
            if count[0] == 1:
                # 强制进入重建路径，仅注入单次缓存失效；旧索引实际完整。
                raise KnowledgeError('vector_index_missing')
            return original_load(db, selected_release, encoder)
        with patch.object(self.provider, 'db', database_with_commit_fault), \
             patch.object(index, '_load', side_effect=rebuild_once), \
             self.assertRaisesRegex(sqlite3.IntegrityError, 'simulated_metadata_commit_failure'):
            index.build(release)
        self.assertTrue(self.index_status(release)['ready'])
        with self.provider.db() as db:
            after = [tuple(row) for row in db.execute('SELECT * FROM chunk_vectors ORDER BY citation_id')]
            after_meta = tuple(db.execute('SELECT * FROM vector_releases WHERE release_id=?',
                                          (release,)).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(before_meta, after_meta)

    def test_background_queue_deduplicates_instances_and_bounds_pending_jobs(self):
        directory = Path(self.directory.name)/'queue-model'
        directory.mkdir()
        (directory/'manifest.json').write_text('{}')
        pending = []
        class DeferredPool:
            def submit(self, callback):
                future = Future()
                pending.append((callback, future))
                return future
        provider = SimpleNamespace(path=self.path, vector_encoder=None)
        other_instance = SimpleNamespace(path=self.path, vector_encoder=None)
        with patch('local_knowledge.vectors._pool', DeferredPool()), \
             patch('local_knowledge.vectors._jobs', {}), \
             patch('local_knowledge.embeddings.default_model_directory', return_value=directory), \
             patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '1'}):
            schedule(provider, 'pub-fixture-0')
            schedule(other_instance, 'pub-fixture-0')
            self.assertEqual(len(pending), 1)
            for i in range(1, 9):
                schedule(provider, f'pub-fixture-{i}')
            self.assertEqual(len(pending), 8)
            pending[0][1].set_result(None)
            schedule(other_instance, 'pub-fixture-8')
            self.assertEqual(len(pending), 9)

    def test_background_queue_respects_disabled_and_injected_encoder(self):
        pool = SimpleNamespace(submit=lambda callback: self.fail('background_submission_forbidden'))
        with patch('local_knowledge.vectors._pool', pool), \
             patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '0'}):
            schedule(SimpleNamespace(path=self.path, vector_encoder=None), 'pub-fixture')
        with patch('local_knowledge.vectors._pool', pool), \
             patch.dict('os.environ', {'NAVREN_VECTOR_ENABLED': '1'}):
            schedule(self.provider, 'pub-fixture')
            schedule(SimpleNamespace(path=self.path, vector_encoder=None), None)


if __name__ == '__main__':
    unittest.main()
