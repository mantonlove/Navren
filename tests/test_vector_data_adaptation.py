"""公共向量资料适配的风险回归：合成原件、确定性替身、临时 SQLite。

这些测试验证字段表示、来源身份和跨快照缓存边界，不验证模型语义效果。
不访问真实公共库、个人库、网络或云端模型。
"""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from local_knowledge import KnowledgeError, LocalKnowledgeProvider
from local_knowledge.importers import import_webank_disclosure
from local_knowledge.store import PARSER, PDF_PARSER, packed, sha
from local_knowledge import vector_documents
from local_knowledge.vector_documents import input_sha, representation
from local_knowledge.vectors import MAX_REUSE_RELEASES, VectorIndex


OLD_DATE = '2026-10-05'
NEW_DATE = '2026-10-06'


class DeterministicEncoder:
    """输入摘要产生单位向量，仅用于核验缓存；不是语义模型。"""
    dimension = 512
    profile_id = 'fixture-public-data-adaptation-not-real-model/512-v1'

    def __init__(self):
        self.documents = []

    def encode_documents(self, texts):
        self.documents.extend(texts)
        outputs = []
        for text in texts:
            raw = b''.join(hashlib.sha256(text.encode() + bytes([i])).digest()
                           for i in range(16))
            vector = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            vector /= np.linalg.norm(vector)
            outputs.append(vector.reshape(1, self.dimension))
        return outputs

    def encode_query(self, query):
        vector = np.zeros(self.dimension, dtype=np.float32)
        vector[0] = 1.0
        return vector


def disclosure_product(code='FIXTURE-001', **changes):
    product = {
        'code': code, 'name': '合成产品（不构成金融资料）',
        'saleStatus': '正常销售', 'type': '理财', 'placement': '公募',
        'saleType': '开放式', 'risk': 'R5高风险', 'publish': '合成发行机构',
        'investor': '合成投资者范围', 'charge': '详见产品页面',
        'chargeWay': '按产品页面披露', 'customerGroupType': '个人投资者',
    }
    product.update(changes)
    return product


def disclosure_body(products, day):
    return ('_jsonpproductpresentation(' + packed({
        'extractDate': day, 'productPresentation': products,
    }) + ');').encode()


def structured_row(field='risk', value='R5高风险', **changes):
    row = {
        'title': '合成披露', 'excerpt': packed(field) + ':' + packed(value),
        'parser_id': 'webank-record-json/1',
        'shape': 'webank-productpresentation-v1',
        'source_id': 'webank-direct-disclosure', 'channel_id': 'WEBANK',
        'product_id': 'WEBANK:FIXTURE-001', 'share_class_id': None,
        'paragraph': 'productPresentation:FIXTURE-001/' + field, 'page': None,
    }
    row.update(changes)
    return row


class VectorDocumentRepresentationTests(unittest.TestCase):
    def assert_safe_fallback_or_rejection(self, row):
        original = deepcopy(row)
        try:
            result = representation(row)
        except KnowledgeError:
            self.assertEqual(row, original)
            return
        self.assertEqual(result['text'], row['title'][:120] + '\n' + row['excerpt'])
        self.assertIsNone(result['field'])
        self.assertEqual(row, original)

    def test_registered_jsonp_fields_get_only_their_registered_labels(self):
        labels = {'risk': '风险等级', 'charge': '收费标准', 'chargeWay': '收费方式',
                  'publish': '发行机构', 'investor': '合理投资范围',
                  'saleStatus': '销售状态'}
        for field, label in labels.items():
            with self.subTest(field=field):
                row = structured_row(field, '合成字段原值 0.123%')
                original = deepcopy(row)
                result = representation(row)
                self.assertEqual(result['field'], field)
                self.assertIn(label, result['text'])
                self.assertIn(row['excerpt'], result['text'])
                self.assertEqual(row, original)

    def test_reference_only_fee_is_preserved_without_inventing_rate(self):
        row = structured_row('charge', '详见产品页面')
        result = representation(row)
        self.assertEqual(result['detail_status'], 'reference_only')
        self.assertIn(row['excerpt'], result['text'])
        self.assertNotRegex(result['text'], r'\d+(?:\.\d+)?\s*%')
        self.assertNotIn('免费', result['text'])

    def test_escaped_json_value_remains_an_exact_quote(self):
        value = '合成说明：含"引号"与反斜杠\\；数值不得补造。'
        row = structured_row('chargeWay', value)
        result = representation(row)
        self.assertEqual(result['field'], 'chargeWay')
        self.assertIn(row['excerpt'], result['text'])
        self.assertEqual(row['excerpt'], packed('chargeWay') + ':' + packed(value))

    def test_unregistered_parser_source_shape_or_channel_cannot_attach_labels(self):
        mutations = [
            {'parser_id': PARSER}, {'parser_id': 'unknown-parser/1'},
            {'shape': 'unknown-shape'}, {'source_id': 'lookalike-official'},
            {'channel_id': 'OTHER-CHANNEL'},
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.assert_safe_fallback_or_rejection(structured_row(**change))

    def test_subject_code_and_locator_field_must_match_original_fragment(self):
        mutations = [
            {'product_id': 'WEBANK:OTHER'},
            {'paragraph': 'productPresentation:OTHER/risk'},
            {'paragraph': 'productPresentation:FIXTURE-001/charge'},
            {'paragraph': 'chars:0-10'},
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.assert_safe_fallback_or_rejection(structured_row(**change))

    def test_unknown_field_does_not_gain_an_inferred_financial_label(self):
        self.assert_safe_fallback_or_rejection(structured_row('annualReturn', '8%'))

    def test_incomplete_multiple_or_duplicate_fields_cannot_be_relabelled(self):
        values = [
            '"risk":"R5高风',
            '"risk":"R5高风险","charge":"0.10%"',
            '"risk":"R5高风险","risk":"R1低风险"',
            '"risk":',
        ]
        for excerpt in values:
            with self.subTest(excerpt=excerpt):
                self.assert_safe_fallback_or_rejection(structured_row(excerpt=excerpt))

    def test_full_text_and_pdf_keep_title_and_excerpt_without_factual_locator_text(self):
        for parser in (PARSER, PDF_PARSER):
            with self.subTest(parser=parser):
                row = structured_row(parser_id=parser, shape='synthetic-full-text',
                    source_id='synthetic-official', channel_id=None, product_id=None,
                    excerpt='原件明确写明：可能损失本金。', page=99,
                    paragraph='不会凭定位补充收益或条款')
                result = representation(row)
                self.assertEqual(result['text'], row['title'][:120] + '\n' + row['excerpt'])
                self.assertIsNone(result['field'])
                self.assertNotIn('99', result['text'])
                self.assertNotIn(row['paragraph'], result['text'])

    def test_digest_binds_input_policy_text_and_structured_subject(self):
        row = structured_row()
        before = input_sha(row)
        self.assertNotEqual(before, input_sha(structured_row(value='R4中高风险')))
        self.assertNotEqual(before, input_sha(structured_row(title='另一个检索标题')))
        another = structured_row(product_id='WEBANK:FIXTURE-002',
                                 paragraph='productPresentation:FIXTURE-002/risk')
        self.assertNotEqual(before, input_sha(another))
        self.assertNotEqual(before, input_sha(structured_row('chargeWay', 'R5高风险')))
        with patch.object(vector_documents, 'INPUT_POLICY', vector_documents.INPUT_POLICY + '/changed'):
            self.assertNotEqual(before, input_sha(row))


class VectorDataSnapshotAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='navren-vector-data-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'public.sqlite3'
        self.encoder = DeterministicEncoder()
        self.provider = LocalKnowledgeProvider(self.path, encoder=self.encoder)
        fixed_today = patch('local_knowledge.store.today', return_value=NEW_DATE)
        fixed_today.start()
        self.addCleanup(fixed_today.stop)

    def import_release(self, products, day):
        result = import_webank_disclosure(self.provider, disclosure_body(products, day), publish=True)
        return result['release_id']

    def rows(self, release):
        with self.provider.db() as db:
            return [dict(row) for row in self.provider._rows(db, release)]

    def index_rows(self, release):
        with self.provider.db() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM chunk_vectors WHERE release_id=? ORDER BY citation_id,window_no',
                (release,))]

    def status(self, release):
        with self.provider.db() as db:
            return VectorIndex(self.provider, self.encoder).status(db, release)

    def reseal_vector_manifest(self, release):
        with self.provider.db() as db:
            rows = db.execute('SELECT * FROM chunk_vectors WHERE release_id=? AND profile_id=? '
                              'ORDER BY citation_id,window_no',
                              (release, self.encoder.profile_id)).fetchall()
            manifest = sha(packed([(r['citation_id'], r['window_no'], r['text_sha256'],
                                    r['vector_sha256']) for r in rows]))
            db.execute('UPDATE vector_releases SET vector_manifest=? WHERE release_id=? AND profile_id=?',
                       (manifest, release, self.encoder.profile_id))

    def test_jsonp_revision_reuses_unchanged_fields_despite_new_citation_ids(self):
        product = disclosure_product()
        old = self.import_release([product], OLD_DATE)
        self.provider.build_vector_index(old)
        old_rows = self.rows(old)
        self.assertEqual(len(old_rows), len(product))
        self.encoder.documents.clear()
        product['risk'] = 'R4中高风险'
        new = self.import_release([product], NEW_DATE)
        # 当前发布移除已生效替代的旧成员，历史固定快照仍完整封存。
        all_new_rows = self.rows(new)
        new_rows = [row for row in all_new_rows if row['published_on'] == NEW_DATE]
        self.assertEqual(len(all_new_rows), len(product))
        self.assertEqual(len(new_rows), len(product))
        self.assertTrue({r['id'] for r in old_rows}.isdisjoint(r['id'] for r in new_rows))
        built = self.provider.build_vector_index(new)
        self.assertEqual(len(self.encoder.documents), 1)
        self.assertEqual(built['encoded_chunk_count'], 1)
        self.assertEqual(built['reused_chunk_count'], 11)
        self.assertIn('"risk":"R4中高风险"', self.encoder.documents[0])
        self.assertTrue(self.status(old)['ready'])
        self.assertTrue(self.status(new)['ready'])
        old_fields = {r['paragraph'].rsplit('/', 1)[1]: r for r in old_rows}
        new_fields = {r['paragraph'].rsplit('/', 1)[1]: r for r in new_rows}
        old_vectors = {r['citation_id']: r['vector'] for r in self.index_rows(old)}
        new_vectors = {r['citation_id']: r['vector'] for r in self.index_rows(new)}
        for field in product:
            before, after = old_fields[field], new_fields[field]
            if field != 'risk':
                self.assertEqual(input_sha(before), input_sha(after))
                self.assertEqual(old_vectors[before['id']], new_vectors[after['id']])
            hit = self.provider.resolve_public_evidence(after['id'], new, NEW_DATE)
            self.assertIsNotNone(hit)
            self.assertEqual(hit['excerpt'], after['excerpt'])
            self.assertEqual(hit['document_version_id'], after['version_id'])
        old_hit = self.provider.for_release(old).resolve_public_evidence(
            old_fields['risk']['id'], old, OLD_DATE)
        self.assertIsNotNone(old_hit)
        self.assertEqual(old_hit['excerpt'], '"risk":"R5高风险"')
        new_hit = self.provider.resolve_public_evidence(new_fields['risk']['id'], new, NEW_DATE)
        self.assertEqual(new_hit['excerpt'], '"risk":"R4中高风险"')
        self.assertIsNone(self.provider.resolve_public_evidence(old_fields['risk']['id'], new, NEW_DATE))
        self.assertIsNone(self.provider.resolve_public_evidence(old_fields['risk']['id'], old, NEW_DATE))

    def test_same_named_products_and_same_valued_fields_do_not_share_identity_digest(self):
        products = [disclosure_product('FIXTURE-001'), disclosure_product('FIXTURE-002')]
        products[0]['chargeWay'] = products[0]['risk']
        release = self.import_release(products, OLD_DATE)
        self.provider.build_vector_index(release)
        rows = self.rows(release)
        risk = [row for row in rows if row['paragraph'].endswith('/risk')]
        self.assertEqual(len(risk), 2)
        self.assertNotEqual(input_sha(risk[0]), input_sha(risk[1]))
        same_product = next(row for row in rows if row['product_id'] == 'WEBANK:FIXTURE-001'
                            and row['paragraph'].endswith('/chargeWay'))
        first_risk = next(row for row in risk if row['product_id'] == same_product['product_id'])
        self.assertNotEqual(input_sha(first_risk), input_sha(same_product))
        for row in rows:
            hit = self.provider.resolve_public_evidence(row['id'], release, OLD_DATE)
            self.assertEqual(hit['product_id'], row['product_id'])
            self.assertEqual(hit['excerpt'], row['excerpt'])

    def test_pre_policy_digest_index_degrades_and_rebuilds_without_old_cache(self):
        release = self.import_release([disclosure_product()], OLD_DATE)
        self.provider.build_vector_index(release)
        canonical = self.rows(release)
        with self.provider.db() as db:
            for row in canonical:
                legacy = sha(row['title'][:120] + '\n' + row['excerpt'])
                db.execute('UPDATE chunk_vectors SET text_sha256=? WHERE release_id=? AND citation_id=?',
                           (legacy, release, row['id']))
        self.reseal_vector_manifest(release)
        self.assertFalse(self.status(release)['ready'])
        result = self.provider.search_public({'scope': 'public', 'query': '高风险',
            'as_of': OLD_DATE, 'facets': [], 'limit': 8}, release)
        self.assertTrue(result['citation_ids'])
        self.assertEqual(result['retrieval']['mode'], 'fts5')
        self.encoder.documents.clear()
        self.provider.build_vector_index(release)
        self.assertEqual(len(self.encoder.documents), len(canonical))
        self.assertTrue(self.status(release)['ready'])

    def test_policy_change_invalidates_current_and_historical_reuse_with_same_model_profile(self):
        product = disclosure_product()
        old = self.import_release([product], OLD_DATE)
        self.provider.build_vector_index(old)
        new = self.import_release([product], NEW_DATE)
        self.encoder.documents.clear()
        with patch.object(vector_documents, 'INPUT_POLICY', vector_documents.INPUT_POLICY + '/changed'):
            self.assertFalse(self.status(old)['ready'])
            built = self.provider.build_vector_index(new)
            self.assertEqual(built['reused_chunk_count'], 0)
            self.assertEqual(len(self.encoder.documents), len(self.rows(new)))
            self.assertTrue(self.status(new)['ready'])
        self.assertFalse(self.status(new)['ready'])

    def test_corrupt_historical_vector_is_not_reused_even_with_matching_input(self):
        product = disclosure_product()
        old = self.import_release([product], OLD_DATE)
        self.provider.build_vector_index(old)
        with self.provider.db() as db:
            cid = db.execute('SELECT citation_id FROM chunk_vectors WHERE release_id=? LIMIT 1',
                             (old,)).fetchone()[0]
            db.execute('UPDATE chunk_vectors SET vector=? WHERE release_id=? AND citation_id=?',
                       (b'broken-local-cache', old, cid))
        self.assertFalse(self.status(old)['ready'])
        product['risk'] = 'R4中高风险'
        new = self.import_release([product], NEW_DATE)
        self.encoder.documents.clear()
        built = self.provider.build_vector_index(new)
        self.assertEqual(built['reused_chunk_count'], 0)
        self.assertEqual(len(self.encoder.documents), len(self.rows(new)))
        self.assertTrue(self.status(new)['ready'])

    def test_large_split_field_does_not_receive_a_complete_field_label(self):
        product = disclosure_product(charge='合成长费用说明' * 170 + '：末尾收费条件不得删去。')
        release = self.import_release([product], OLD_DATE)
        self.provider.build_vector_index(release)
        charge_rows = [row for row in self.rows(release)
                       if row['paragraph'].endswith('/charge')]
        self.assertGreater(len(charge_rows), 1)
        for row in charge_rows:
            result = representation(row)
            self.assertIsNone(result['field'])
            self.assertEqual(result['text'], row['title'][:120] + '\n' + row['excerpt'])
            hit = self.provider.resolve_public_evidence(row['id'], release, OLD_DATE)
            self.assertEqual(hit['excerpt'], row['excerpt'])
        restored = ''.join(row['excerpt'] for row in sorted(charge_rows, key=lambda r: r['start_char']))
        self.assertEqual(restored, packed('charge') + ':' + packed(product['charge']))

    def test_ten_disclosure_updates_keep_current_size_and_other_documents(self):
        self.provider.register_source('other-official-fixture', publisher='合成其他来源',
            endpoints=['https://other.example/'], shape='plain-text-v1',
            freshness={'general': None}, permission_basis='synthetic_fixture_only')
        other = self.provider.stage_document(source_id='other-official-fixture',
            source_url='https://other.example/preserved', title='需保留的合成官方全文',
            text='与产品披露更新无关的其他官方原文。',
            published_on='2026-09-01', retrieved_on='2026-09-01')
        self.provider.approve_version(other, verification_basis='synthetic_fixture_only')
        unrelated_release = self.provider.publish(version_ids=[other])
        unrelated_ids = {row['id'] for row in self.rows(unrelated_release)}
        self.provider.build_vector_index(unrelated_release)
        releases = []
        first_risk = None
        first_date = None
        product = disclosure_product()
        for i in range(10):
            day = (date(2026, 9, 20) + timedelta(days=i)).isoformat()
            product['risk'] = f'R5高风险（合成修订{i}）'
            self.encoder.documents.clear()
            with patch('local_knowledge.store.today', return_value=day):
                release = self.import_release([product], day)
                built = self.provider.build_vector_index(release)
            current_rows = self.rows(release)
            self.assertEqual(len(current_rows), len(product) + len(unrelated_ids))
            self.assertTrue(unrelated_ids.issubset(row['id'] for row in current_rows))
            product_rows = [row for row in current_rows if row['product_id'] == 'WEBANK:FIXTURE-001']
            self.assertEqual(len(product_rows), len(product))
            self.assertEqual({row['published_on'] for row in product_rows}, {day})
            if i:
                self.assertEqual(built['encoded_chunk_count'], 1)
                self.assertEqual(built['reused_chunk_count'], len(current_rows) - 1)
            else:
                first_risk = next(row for row in product_rows if row['paragraph'].endswith('/risk'))
                first_date = day
            releases.append(release)
        self.assertEqual(len(set(releases)), 10)
        for release in releases:
            self.assertTrue(self.status(release)['ready'])
            self.assertEqual(len(self.rows(release)), len(product) + len(unrelated_ids))
        fixed = self.provider.for_release(releases[0])
        original = fixed.resolve_public_evidence(first_risk['id'], releases[0], first_date)
        self.assertEqual(original['excerpt'], '"risk":"R5高风险（合成修订0）"')
        self.assertIsNone(fixed.resolve_public_evidence(first_risk['id'], releases[0], day))
        self.assertIsNone(self.provider.resolve_public_evidence(first_risk['id'], releases[-1], day))
        for cid in unrelated_ids:
            hit = self.provider.resolve_public_evidence(cid, releases[-1], day)
            self.assertEqual(hit['document_version_id'], other)
            self.assertEqual(hit['excerpt'], '与产品披露更新无关的其他官方原文。')

    def test_future_replacement_keeps_currently_valid_old_disclosure_members(self):
        product = disclosure_product()
        old = self.import_release([product], OLD_DATE)
        self.provider.build_vector_index(old)
        old_risk = next(row for row in self.rows(old) if row['paragraph'].endswith('/risk'))
        future_date = '2026-10-07'
        product['risk'] = 'R4中高风险'
        future = self.import_release([product], future_date)
        self.provider.build_vector_index(future)
        rows = self.rows(future)
        self.assertEqual(len(rows), 2 * len(product))
        self.assertIn(old_risk['id'], {row['id'] for row in rows})
        new_risk = next(row for row in rows if row['paragraph'].endswith('/risk')
                        and row['published_on'] == future_date)
        current_old = self.provider.resolve_public_evidence(old_risk['id'], future, NEW_DATE)
        self.assertEqual(current_old['excerpt'], '"risk":"R5高风险"')
        self.assertIsNone(self.provider.resolve_public_evidence(new_risk['id'], future, NEW_DATE))
        self.assertIsNone(self.provider.resolve_public_evidence(old_risk['id'], future, future_date))
        current_new = self.provider.resolve_public_evidence(new_risk['id'], future, future_date)
        self.assertEqual(current_new['excerpt'], '"risk":"R4中高风险"')
        self.assertTrue(self.status(old)['ready'])
        self.assertTrue(self.status(future)['ready'])

    def test_old_warm_import_does_not_restore_replaced_current_members(self):
        old_product = disclosure_product()
        old = self.import_release([old_product], OLD_DATE)
        self.provider.build_vector_index(old)
        current_product = disclosure_product(risk='R4中高风险')
        current = self.import_release([current_product], NEW_DATE)
        self.provider.build_vector_index(current)
        members = {row['id'] for row in self.rows(current)}
        self.encoder.documents.clear()
        restored = self.import_release([old_product], OLD_DATE)
        self.assertEqual(restored, current)
        self.assertEqual({row['id'] for row in self.rows(restored)}, members)
        self.assertEqual(self.provider.status()['public_release_id'], current)
        self.assertEqual(len(self.rows(restored)), len(current_product))
        self.assertEqual(self.encoder.documents, [])
        self.assertTrue(self.status(current)['ready'])
        self.assertTrue(self.status(old)['ready'])
        risk = next(row for row in self.rows(restored) if row['paragraph'].endswith('/risk'))
        hit = self.provider.resolve_public_evidence(risk['id'], restored, NEW_DATE)
        self.assertEqual(hit['excerpt'], '"risk":"R4中高风险"')

    def test_reuse_checks_at_most_eight_latest_indexes_and_never_unchecked_oldest(self):
        self.provider.register_source('bounded-fixture', publisher='合成来源',
            endpoints=['https://fixture.example/'], shape='plain-text-v1',
            freshness={'general': None}, permission_basis='synthetic_fixture_only')
        releases = []
        for i in range(9):
            version = self.provider.stage_document(source_id='bounded-fixture',
                source_url=f'https://fixture.example/{i}', title=f'合成全文 {i}',
                text=f'合成全文正文 {i}。', published_on=OLD_DATE, retrieved_on=OLD_DATE)
            self.provider.approve_version(version, verification_basis='synthetic_fixture_only')
            release = self.provider.publish(version_ids=[version])
            self.provider.build_vector_index(release)
            with self.provider.db() as db:
                db.execute('UPDATE vector_releases SET created_at=? WHERE release_id=?',
                           (f'2026-10-10T00:00:{i:02d}Z', release))
            releases.append(release)
        version = self.provider.stage_document(source_id='bounded-fixture',
            source_url='https://fixture.example/oldest-duplicate', title='合成全文 0',
            text='合成全文正文 0。', published_on=OLD_DATE, retrieved_on=OLD_DATE)
        self.provider.approve_version(version, verification_basis='synthetic_fixture_only')
        target = self.provider.publish(version_ids=[version])
        self.encoder.documents.clear()
        index = VectorIndex(self.provider, self.encoder)
        original_load = index._load
        checked = []
        def counted_load(db, release, encoder):
            checked.append(release)
            return original_load(db, release, encoder)
        with patch.object(index, '_load', side_effect=counted_load):
            built = index.build(target)
        self.assertEqual(MAX_REUSE_RELEASES, 8)
        self.assertEqual(checked[0], target)
        self.assertEqual(len(checked[1:]), 8)
        self.assertNotIn(releases[0], checked)
        self.assertEqual(built['reused_chunk_count'], 0)
        self.assertEqual(built['encoded_chunk_count'], 1)
        self.assertEqual(len(self.encoder.documents), 1)
        self.assertTrue(self.status(target)['ready'])
        checked.clear()
        with patch.object(index, '_load', side_effect=counted_load):
            ready = index.build(target)
        self.assertEqual(checked, [target])
        self.assertEqual(ready['encoded_chunk_count'], 0)


if __name__ == '__main__':
    unittest.main()
