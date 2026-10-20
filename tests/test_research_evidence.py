"""临时公开资料 fixture 的本地版本/完整性合同；不代表真实金融内容验收。"""
from copy import deepcopy
from datetime import date
import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_evidence import (ResearchEvidenceStore, ResearchEvidenceError,
                               EvidenceChanged, DependencyUnavailable)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def _revision_worker(root, ready, done, result):
    try:
        store = ResearchEvidenceStore(root, clock=lambda: date(2026, 10, 18))
        ready.set()
        result.put(store.revision())
    except Exception as exc:
        result.put(type(exc).__name__)
    finally:
        done.set()


def _publish_worker(root, body, ready, done, result):
    try:
        store = ResearchEvidenceStore(root, clock=lambda: date(2026, 10, 18))
        source = json.loads((Path(root) / 'manifest.json').read_text())['catalog']['url']
        ready.set()
        result.put(store.publish_catalog_snapshot(body, source_url=source,
            fetched_at='2026-10-19T01:00:00+08:00', expected_sha256=hashlib.sha256(body).hexdigest()))
    except Exception as exc:
        result.put(type(exc).__name__)
    finally:
        done.set()


class ResearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rows = [dict(code=code, name=name, type=kind, publish='管理人甲', risk='R4',
                     placement='公募', saleType='代销', investor='C4', charge='详见产品页面', chargeWay='详见产品页面',
                     saleStatus='在售', customerGroupType=audience)
                for code, name, kind, audience in [
                    ('100001', '甲基金A', '开放式基金', '个人客户'),
                    ('100002', '甲基金C', '开放式基金', '个人客户'),
                    ('100003', '隔离基金', '开放式基金', ''),
                    ('INS1', '个人保险目录', '保险', '个人投资者'),
                    ('INS2', '机构保险目录', '保险', '机构投资者')]]
        raw = '_jsonpproductpresentation(' + json.dumps({'extractDate': '2026-10-14', 'productPresentation': rows}) + ');'
        catalog = self.doc('catalog:WEBANK', 'catalog/source.jsonp', raw.encode(), 'channel_catalog')
        self.manifest = dict(schema_version=1, data_scope='public', catalog=catalog,
                             quarantines={'100003': {'state': 'conflict', 'reason': 'identity_mismatch'}})
        _write(self.root / 'manifest.json', self.manifest)
        self.a = self.doc('manager:a:summary', 'funds/raw/a.txt', b'A summary official')
        self.b = self.doc('manager:a:prospectus', 'funds/raw/b.txt', b'B prospectus official')
        self.c = self.doc('manager:c:summary', 'funds/raw/c.txt', b'C summary official')
        a = self.entity('100001', self.a, [self.a, self.b])
        c = self.entity('100002', self.c, [self.c])
        underlying = self.fact(a['entity_id'], 'fees.annual.sales_service', '0', self.b)
        a['facts'].append(underlying)
        fees = self.fact(a['entity_id'], 'fees', {'fixed': '0'}, self.a)
        fees['channel'] = 'manager_standard'
        fees['locator'] = {'kind': 'composite', 'bindings': [
            {'field': underlying['field'], 'document_id': self.b['document_id'], 'locator': underlying['locator']} ]}
        a['facts'].append(fees)
        self.bundle = {'schema_version': 1, 'entities': [a, c], 'documents': [self.a, self.b, self.c]}
        self.save()
        self.store = ResearchEvidenceStore(self.root, clock=lambda: date(2026, 10, 18))

    def doc(self, did, path, payload, kind='product_summary'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        return dict(document_id=did, source_id=did, path=path, url='https://official.example/' + path,
                    sha256=sha, source_version='sha256:' + sha, media_type='text/plain',
                    document_type=kind, source_scope='manager_disclosure', published_at=None,
                    effective_from=None, effective_to=None, fetched_at='2026-10-18T00:00:00Z', access='public')

    def fact(self, eid, field, value, doc):
        return dict(entity_id=eid, field=field, value=value, unit=None, state='verified',
                    source_scope=doc['source_scope'], source_version=doc['source_version'],
                    published_at=None, effective_from=None, effective_to=None,
                    locator={'line': 1, 'quote': 'official'}, missing_reason=None,
                    document_id=doc['document_id'], source_url=doc['url'], source_sha256=doc['sha256'])

    def entity(self, code, doc, documents):
        eid = 'fund:CN:' + code
        return dict(entity_id=eid, kind='fund_share', name=code, family_id='family:test',
                    customer_type='individual', document_ids=[d['document_id'] for d in documents],
                    facts=[self.fact(eid, 'identity.code', code, doc),
                           self.fact(eid, 'identity.manager', '管理人甲', doc)], relations=[], limitations=[])

    def save(self):
        _write(self.root / 'funds/bundle.json', self.bundle)

    def update_doc(self, did, payload):
        doc = next(d for d in self.bundle['documents'] if d['document_id'] == did)
        (self.root / doc['path']).write_bytes(payload)
        doc['sha256'] = hashlib.sha256(payload).hexdigest()
        doc['source_version'] = 'sha256:' + doc['sha256']
        for entity in self.bundle['entities']:
            for fact in entity['facts']:
                if fact.get('document_id') == did:
                    fact.update(source_version=doc['source_version'], source_sha256=doc['sha256'])
        self.save()

    def test_catalog_pagination_and_audience_separation(self):
        first = self.store.catalog({'limit': '2'})
        self.assertEqual(first['total'], 5)
        self.assertTrue(first['has_more'])
        self.assertEqual(len(self.store.catalog({'limit': 2, 'offset': 4})['items']), 1)
        personal = self.store.catalog({'kind': 'insurance_offer', 'customer_group': 'individual'})
        institutional = self.store.catalog({'kind': 'insurance_offer', 'customer_group': 'institution'})
        self.assertEqual([r['entity_id'] for r in personal['items']], ['offer:WEBANK:INS1'])
        self.assertEqual([r['entity_id'] for r in institutional['items']], ['offer:WEBANK:INS2'])

    def test_document_source_map_and_graph_have_exact_bindings(self):
        d = self.store.detail('fund:CN:100001')
        self.assertEqual(d['evidence_version'], d['entity_version'])
        for fact in d['facts']:
            if fact['state'] == 'verified':
                self.assertEqual(d['source_versions'][fact['source_id']], fact['source_version'])
        self.assertTrue(any(e['predicate'] == 'share_of' for e in d['graph']))
        self.assertFalse(d['execution_authority'])
        self.assertNotIn('path', self.store.documents(d['entity_id'])['items'][0])

    def test_unknown_customer_is_missing_and_quarantine_cannot_upgrade(self):
        d = self.store.detail('fund:CN:100003')
        self.assertEqual(d['customer_group'], 'unknown')
        self.assertEqual(next(f for f in d['facts'] if f['field'] == 'channel_customer_group')['state'], 'missing')
        self.assertEqual(d['capabilities'], ['catalog', 'identity_conflict'])
        bad = deepcopy(self.bundle['entities'][0]);bad['entity_id'] = 'fund:CN:100003'
        self.bundle['entities'].append(bad);self.save()
        with self.assertRaises(ResearchEvidenceError):
            self.store.revision()

    def test_damaged_secondary_source_downgrades_composite_fees(self):
        self.store.revision()
        (self.root / self.b['path']).write_bytes(b'tampered')
        d = self.store.detail('fund:CN:100001')
        fees = next(f for f in d['facts'] if f['field'] == 'fees')
        self.assertEqual(fees['state'], 'conflict')
        self.assertNotIn('manager_standard_cost_scenario', d['capabilities'])
        self.assertEqual(len(fees['dependency_versions']), 2)

    def test_missing_source_downgrades_and_old_original_survives(self):
        old = self.store.revision()
        (self.root / self.b['path']).unlink()
        d = self.store.detail('fund:CN:100001')
        self.assertNotEqual(old, d['provider_revision'])
        self.assertEqual(next(f for f in d['facts'] if f['field'] == 'fees')['state'], 'missing')
        self.assertEqual(self.store.document_bytes(self.b['document_id'], revision=old), b'B prospectus official')

    def test_archive_tamper_is_detected_by_cached_detail_and_guard(self):
        revision = self.store.revision()
        (self.root / '.objects' / self.a['sha256']).write_bytes(b'bad archive')
        with self.assertRaises(DependencyUnavailable):
            self.store.detail('fund:CN:100001')
        with self.assertRaises(DependencyUnavailable):
            with self.store.guard(revision):
                pass

    def test_update_keeps_stable_source_id_and_historical_bytes(self):
        old = self.store.revision()
        old_entity = self.store.detail('fund:CN:100001')['evidence_version']
        self.update_doc(self.a['document_id'], b'new disclosed content')
        current = self.store.detail('fund:CN:100001')
        self.assertNotEqual(old_entity, current['evidence_version'])
        self.assertIn(self.a['document_id'], current['source_versions'])
        self.assertEqual(self.store.document_bytes(self.a['document_id'], revision=old), b'A summary official')
        self.assertEqual(self.store.document_bytes(self.a['document_id']), b'new disclosed content')
        with self.assertRaises(EvidenceChanged):
            with self.store.guard(old):
                pass

    def test_unrelated_entity_and_refetch_do_not_change_entity_version(self):
        before = self.store.detail('fund:CN:100001')
        self.update_doc(self.c['document_id'], b'C newer version')
        after = self.store.detail('fund:CN:100001')
        self.assertEqual(before['evidence_version'], after['evidence_version'])
        self.assertNotEqual(before['provider_revision'], after['provider_revision'])
        self.bundle['documents'][0]['fetched_at'] = '2026-10-18T23:00:00Z'
        self.save()
        self.assertEqual(after['provider_revision'], self.store.revision())

    def test_restart_reads_same_published_content(self):
        old = self.store.detail('fund:CN:100001')
        other = ResearchEvidenceStore(self.root, clock=lambda: date(2026, 10, 18))
        self.assertEqual(old, other.detail('fund:CN:100001'))

    def test_cross_process_publication_waits_for_guard(self):
        revision = self.store.revision()
        ctx = multiprocessing.get_context('spawn')
        ready, done, result = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_revision_worker, args=(str(self.root), ready, done, result))
        try:
            with self.store.guard(revision):
                process.start()
                self.assertTrue(ready.wait(5), 'worker must reach publication lock')
                self.assertFalse(done.wait(.1), 'publication must block while parent holds guard')
                self.assertEqual(self.store.detail('fund:CN:100001')['provider_revision'], revision)
            self.assertTrue(done.wait(5))
            self.assertEqual(result.get(timeout=1), revision)
            process.join(3)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate();process.join(3)
            result.close();result.join_thread()

    def test_source_collision_wrong_entity_and_fake_binding_are_rejected(self):
        original = deepcopy(self.bundle)
        self.bundle['documents'][1]['source_id'] = self.a['source_id'];self.save()
        with self.assertRaises(ResearchEvidenceError):self.store.revision()
        self.bundle = deepcopy(original)
        self.bundle['entities'][0]['facts'][0]['value'] = '999999';self.save()
        with self.assertRaises(ResearchEvidenceError):self.store.revision()
        self.bundle = deepcopy(original)
        self.bundle['entities'][0]['facts'][-1]['locator']['bindings'][0]['document_id'] = self.c['document_id'];self.save()
        with self.assertRaises(ResearchEvidenceError):self.store.revision()

    def test_private_inputs_and_path_traversal_are_rejected(self):
        self.bundle['entities'][0]['workspace_id'] = 'private-workspace';self.save()
        with self.assertRaises(ResearchEvidenceError):self.store.revision()
        del self.bundle['entities'][0]['workspace_id']
        self.bundle['documents'][0]['path'] = '../private.pdf';self.save()
        with self.assertRaises(ResearchEvidenceError):self.store.revision()

    def test_expired_fact_is_stale_and_bad_catalog_never_falls_back(self):
        self.bundle['entities'][0]['facts'][-1]['effective_to'] = '2026-10-17';self.save()
        d = self.store.detail('fund:CN:100001')
        self.assertEqual(next(f for f in d['facts'] if f['field'] == 'fees')['state'], 'stale')
        (self.root / self.manifest['catalog']['path']).write_bytes(b'bad')
        with self.assertRaises(DependencyUnavailable):self.store.catalog()

    def test_query_and_nested_guard_are_bounded(self):
        for query in ({'owner': 'x'}, {'limit': True}, {'limit': 201}, {'q': 'x'*201}):
            with self.assertRaises(ResearchEvidenceError):self.store.catalog(query)
        revision = self.store.revision()
        with self.store.guard(revision):
            with self.store.guard(revision):self.assertEqual(self.store.revision(), revision)
            with self.assertRaises(EvidenceChanged):
                with self.store.guard('0'*64):pass

    def test_publication_timestamp_is_preserved_and_naive_time_rejected(self):
        self.bundle['documents'][0]['published_at'] = '2025-08-19T15:46:00+08:00'
        self.save()
        docs = self.store.documents('fund:CN:100001')['items']
        self.assertEqual(next(d for d in docs if d['document_id'] == self.a['document_id'])['published_at'],
                         '2025-08-19T15:46:00+08:00')
        self.bundle['documents'][0]['published_at'] = '2025-08-19T15:46:00'
        self.save()
        with self.assertRaises(ResearchEvidenceError):self.store.revision()

    def test_frozen_bundle_manifest_ignores_unpublished_staging(self):
        payload = (self.root / 'funds/bundle.json').read_bytes()
        sha = hashlib.sha256(payload).hexdigest()
        release = self.root / '.bundles' / (sha + '.json')
        release.parent.mkdir();release.write_bytes(payload)
        self.manifest['bundles'] = [{'path': '.bundles/' + release.name, 'sha256': sha}]
        _write(self.root / 'manifest.json', self.manifest)
        revision = self.store.revision()
        (self.root / 'funds/bundle.json').write_text('{ partial new staging')
        self.assertEqual(self.store.revision(), revision)
        release.write_bytes(b'bad published bundle')
        with self.assertRaises(DependencyUnavailable):self.store.revision()

    def catalog_body(self, day='2026-10-18'):
        raw = (self.root / 'catalog/source.jsonp').read_text()
        return raw.replace('2026-10-14', day).replace('R4', 'R3').encode()

    def publish(self, body, **kwargs):
        return self.store.publish_catalog_snapshot(body,
            source_url=kwargs.pop('source_url', self.manifest['catalog']['url']),
            fetched_at='2026-10-19T01:00:00+08:00',
            expected_sha256=kwargs.pop('expected_sha256', hashlib.sha256(body).hexdigest()), **kwargs)

    def test_catalog_sync_publishes_original_date_and_invalidates_old_version(self):
        old = self.store.detail('fund:CN:100001')
        original = self.store.document_bytes('catalog:WEBANK')
        receipt = self.publish(self.catalog_body(), expected_revision=old['provider_revision'])
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual(receipt['source_date'], '2026-10-18')
        current = self.store.detail('fund:CN:100001')
        self.assertNotEqual(current['evidence_version'], old['evidence_version'])
        risk = next(f for f in current['facts'] if f['field'] == 'channel_risk_rating')
        self.assertEqual((risk['value'], risk['source_date']), ('R3', '2026-10-18'))
        self.assertIsNone(risk['published_at'])
        self.assertEqual(self.store.document_bytes('catalog:WEBANK', revision=old['provider_revision']), original)
        self.assertEqual(self.store.document_bytes('catalog:WEBANK'), self.catalog_body())
        new = ResearchEvidenceStore(self.root, clock=lambda: date(2026, 10, 18))
        self.assertEqual(new.revision(), receipt['provider_revision'])
        with self.assertRaises(EvidenceChanged):
            with self.store.guard(old['provider_revision']):pass

    def test_catalog_sync_unchanged_bytes_do_not_republish(self):
        revision = self.store.revision()
        body = (self.root / self.manifest['catalog']['path']).read_bytes()
        before = (self.root / 'manifest.json').read_bytes()
        receipt = self.publish(body)
        self.assertEqual(receipt['status'], 'unchanged')
        self.assertEqual(receipt['provider_revision'], revision)
        self.assertEqual(receipt['source_date'], '2026-10-14')
        self.assertEqual((self.root / 'manifest.json').read_bytes(), before)

    def test_catalog_sync_rejects_invalid_hash_source_schema_and_date(self):
        revision = self.store.revision()
        before = (self.root / 'manifest.json').read_bytes()
        cases = [(self.catalog_body(), {'expected_sha256': '0'*64}),
                 (self.catalog_body(), {'source_url': 'https://wrong.example/catalog'}),
                 (b'not JSONP', {}), (self.catalog_body('2026-10-13'), {}),
                 (self.catalog_body('2099-01-01'), {})]
        for body, kwargs in cases:
            with self.subTest(kwargs=kwargs, body=body[:20]):
                with self.assertRaises(ResearchEvidenceError):self.publish(body, **kwargs)
                self.assertEqual(self.store.revision(), revision)
                self.assertEqual((self.root / 'manifest.json').read_bytes(), before)

    def test_catalog_sync_failed_commit_restores_last_valid_pointer(self):
        revision = self.store.revision()
        before = (self.root / 'manifest.json').read_bytes()
        original = self.store._refresh_locked
        count = [0]
        def fail_candidate(**kwargs):
            count[0] += 1
            if count[0] == 2:raise DependencyUnavailable('injected candidate commit failure')
            return original(**kwargs)
        with patch.object(self.store, '_refresh_locked', side_effect=fail_candidate):
            with self.assertRaises(DependencyUnavailable):self.publish(self.catalog_body())
        self.assertEqual((self.root / 'manifest.json').read_bytes(), before)
        self.assertEqual(self.store.revision(), revision)

    def test_catalog_sync_blocks_while_another_process_holds_guard(self):
        revision = self.store.revision()
        ctx = multiprocessing.get_context('spawn')
        ready, done, result = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_publish_worker, args=(str(self.root), self.catalog_body(), ready, done, result))
        try:
            with self.store.guard(revision):
                process.start()
                self.assertTrue(ready.wait(5))
                self.assertFalse(done.wait(.1))
                self.assertEqual(self.store.revision(), revision)
            self.assertTrue(done.wait(5))
            receipt = result.get(timeout=1)
            self.assertEqual(receipt['status'], 'published')
            self.assertNotEqual(self.store.revision(), revision)
            process.join(3)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():process.terminate();process.join(3)
            result.close();result.join_thread()

    def test_catalog_sync_cannot_publish_inside_pinned_guard(self):
        revision = self.store.revision()
        with self.store.guard(revision):
            with self.assertRaises(EvidenceChanged):self.publish(self.catalog_body())

    def test_catalog_sync_null_audience_stays_unknown(self):
        body = self.catalog_body().replace(b'"customerGroupType": ""', b'"customerGroupType": null')
        self.publish(body)
        d = self.store.detail('fund:CN:100003')
        self.assertEqual(d['customer_group'], 'unknown')
        self.assertEqual(next(f for f in d['facts'] if f['field'] == 'channel_customer_group')['state'], 'missing')


if __name__ == '__main__':
    unittest.main()
