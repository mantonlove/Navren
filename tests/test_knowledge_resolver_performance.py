"""Resolver safety and controlled synthetic performance; temporary databases only.

Run correctness with unittest. Run the same-security locator benchmark separately:
  .venv/bin/python tests/test_knowledge_resolver_performance.py --benchmark
No network, financial-data quality claim or live database access is involved.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_knowledge import LocalKnowledgeProvider, KnowledgeError
from local_knowledge.store import WEBANK_PARSER, packed, sha


AS_OF = '2026-10-06'
URL = 'https://resolver-fixture.invalid/terms'


def build_fixture(path, count=4):
    provider = LocalKnowledgeProvider(path)
    provider.register_source('fixture', publisher='合成性能测试来源', endpoints=[URL],
        freshness={'fees': None, 'risk': None, 'general': None},
        shape='resolver-fixture/1', permission_basis='synthetic_test_only_no_official_fact_claim')
    for pid in ('PRODUCT-A', 'PRODUCT-B'):
        provider.register_product(pid, issuer='SYNTHETIC', scheme='fixture', code=pid,
                                  name='合成产品'+pid, verified=True)
    provider.register_share('SHARE-A', product_id='PRODUCT-A', scheme='fixture', code='A', currency='CNY')
    provider.register_channel('CHANNEL-A', name='合成渠道')
    lines = [f'合成条款{i:05d}。费用为0.10%，可能损失本金，条件以原文为准。' for i in range(count)]
    text = '\n'.join(lines)
    spans = []
    offset = 0
    for i, line in enumerate(lines):
        spans.append({'start_char': offset, 'end_char': offset+len(line),
                      'paragraph': 'synthetic:'+str(i), 'facets': ['fees', 'risk']})
        offset += len(line)+1
    version = provider.stage_document(source_id='fixture', source_url=URL, title='合成回查性能夹具',
        text=text, spans=spans, product_id='PRODUCT-A', share_class_id='SHARE-A', channel_id='CHANNEL-A',
        published_on='2026-09-01', retrieved_on=AS_OF, effective_from='2026-09-01',
        effective_to='2026-11-01', facets=['fees', 'risk'], shape='resolver-fixture/1')
    provider.approve_version(version, verification_basis='synthetic_fixture_only')
    release = provider.publish(version_ids=[version])
    with provider.db() as db:
        ids = [r[0] for r in db.execute('SELECT id FROM chunks WHERE version_id=? ORDER BY id', (version,))]
    return provider, version, release, ids


class FullScanLocatorProvider(LocalKnowledgeProvider):
    """Control: identical new gates/transaction, old full-body row locator only."""
    def _rows(self, db, release_id, *, citation_ids=None):
        rows = super()._rows(db, release_id)
        wanted = None if citation_ids is None else set(citation_ids)
        return rows if wanted is None else [r for r in rows if r['id'] in wanted]


class ResolverSafetyTests(unittest.TestCase):
    def fixture(self, count=4):
        directory = tempfile.TemporaryDirectory(prefix='navren-resolver-fixture-', ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        return build_fixture(Path(directory.name)/'public.sqlite3', count)

    @staticmethod
    def tamper(provider, sql, args=(), *, drop=()):
        # Deliberately bypass write-side protection to test the read-side gates.
        with sqlite3.connect(provider.path) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for name in drop:
                db.execute('DROP TRIGGER '+name)
            db.execute(sql, args)

    def assert_unavailable(self, provider, release, cid, as_of=AS_OF):
        self.assertIsNone(provider.resolve_public_evidence(cid, release, as_of))
        self.assertIsNone(provider.resolve_public_evidence_batch([cid], release, as_of)[cid])

    def test_indexed_locator_only_fetches_requested_members(self):
        p, _, release, ids = self.fixture(30)
        statements = []
        original_db = p.db
        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(p, 'db', traced_db), patch.object(p, '_rows', wraps=p._rows) as locate:
            result = p.resolve_public_evidence_batch(ids[:3], release, AS_OF)
        self.assertTrue(all(result.values()))
        self.assertEqual(locate.call_count, 1)
        self.assertEqual(locate.call_args.kwargs['citation_ids'], ids[:3])
        locator = next(s for s in statements if s.startswith('SELECT c.*,v.document_id'))
        self.assertIn('rc.chunk_id IN (', locator)
        with p.db() as db:
            plan = [r['detail'] for r in db.execute('EXPLAIN QUERY PLAN '+locator)]
        self.assertTrue(any('SEARCH rc' in s and 'release_id=? AND chunk_id=?' in s for s in plan), plan)
        self.assertFalse(any('SCAN rc' in s for s in plan), plan)
        self.assertIn('BEGIN', statements)

    def test_batch_preserves_single_contract_duplicates_and_missing_id(self):
        p, _, release, ids = self.fixture()
        requested = [ids[1], ids[0], ids[1], 'unknown-citation']
        batch = p.resolve_public_evidence_batch(requested, release, AS_OF)
        self.assertEqual(list(batch), [ids[1], ids[0], 'unknown-citation'])
        for cid in batch:
            self.assertEqual(batch[cid], p.resolve_public_evidence(cid, release, AS_OF))
        self.assertIsNone(batch['unknown-citation'])
        self.assertEqual(p.resolve_public_evidence_batch([], release, AS_OF), {})
        for bad in ('a-citation', [ids[0]]*65, [None], {'citation_id': ids[0]}):
            with self.subTest(bad_type=type(bad).__name__), self.assertRaises(KnowledgeError):
                p.resolve_public_evidence_batch(bad, release, AS_OF)

    def test_batch_hash_reuse_is_only_inside_one_transaction(self):
        p, version, release, ids = self.fixture()
        with p.db() as db:
            text = db.execute('SELECT parsed_text FROM document_versions WHERE id=?', (version,)).fetchone()[0]
            raw = bytes(db.execute('SELECT payload FROM raw_artifacts').fetchone()[0])
        with patch('local_knowledge.store.sha', wraps=sha) as hashed:
            self.assertTrue(all(p.resolve_public_evidence_batch(ids, release, AS_OF).values()))
        self.assertEqual(sum(call.args[0] == raw for call in hashed.call_args_list), 1)
        self.assertEqual(sum(call.args[0] == text for call in hashed.call_args_list), 1)
        self.tamper(p, 'UPDATE raw_artifacts SET payload=?', (b'corrupt-after-first-batch',), drop=('immutable_raw',))
        self.assert_unavailable(p, release, ids[0])

    def test_parse_excerpt_and_out_of_bounds_span_tampering_fail(self):
        mutations = [
            ('UPDATE document_versions SET parsed_text=? WHERE id=?', ('different text',), 'immutable_version', 'version'),
            ('UPDATE chunks SET excerpt=? WHERE id=?', ('different quote',), 'immutable_chunk', 'chunk'),
            ('UPDATE chunks SET end_char=end_char+100000 WHERE id=?', (), 'immutable_chunk', 'chunk_end'),
            ('UPDATE chunks SET excerpt_sha256=? WHERE id=?', ('0'*64,), 'immutable_chunk', 'chunk')]
        for sql, args, trigger, subject in mutations:
            with self.subTest(sql=sql):
                p, version, release, ids = self.fixture()
                cid = ids[0]
                if subject == 'chunk_end':
                    # Python slicing truncates beyond EOF: use the last original
                    # span so quote equality alone would miss the invalid locator.
                    with p.db() as db:
                        cid = db.execute('SELECT id FROM chunks ORDER BY end_char DESC LIMIT 1').fetchone()[0]
                self.tamper(p, sql, (*args, version if subject == 'version' else cid), drop=(trigger,))
                self.assert_unavailable(p, release, cid)

    def test_complete_manifest_rejects_unrelated_member_removal_and_orphan(self):
        for mode in ('removed', 'orphan'):
            with self.subTest(mode=mode):
                p, _, release, ids = self.fixture()
                if mode == 'removed':
                    self.tamper(p, 'DELETE FROM release_chunks WHERE release_id=? AND chunk_id=?',
                                (release, ids[-1]), drop=('sealed_member_delete',))
                else:
                    self.tamper(p, 'DELETE FROM chunks WHERE id=?', (ids[-1],))
                # Requested member is untouched, but its complete release is corrupt.
                self.assert_unavailable(p, release, ids[0])

    def test_unsealed_manifest_and_temporary_flag_tampering_fail(self):
        for field, value in (('sealed', 0), ('manifest_sha256', '0'*64), ('temporary', 1)):
            with self.subTest(field=field):
                p, _, release, ids = self.fixture()
                self.tamper(p, 'UPDATE releases SET '+field+'=? WHERE id=?',
                            (value, release), drop=('immutable_release',))
                self.assert_unavailable(p, release, ids[0])

    def test_recomputed_manifest_cannot_rebind_existing_release_identity(self):
        p, _, release, ids = self.fixture()
        self.tamper(p, 'DELETE FROM release_chunks WHERE release_id=? AND chunk_id=?',
                    (release, ids[-1]), drop=('sealed_member_delete',))
        with p.db() as db:
            members = db.execute('''SELECT c.id,c.excerpt_sha256 FROM release_chunks rc
                        JOIN chunks c ON c.id=rc.chunk_id WHERE rc.release_id=? ORDER BY c.id''', (release,)).fetchall()
        replacement = sha(packed([(r['id'], r['excerpt_sha256']) for r in members]))
        self.tamper(p, 'UPDATE releases SET manifest_sha256=? WHERE id=?',
                    (replacement, release), drop=('immutable_release',))
        self.assert_unavailable(p, release, ids[0])

    def test_added_member_cannot_enter_existing_manifest(self):
        p, version, release, ids = self.fixture()
        temporary = p.publish(version_ids=[version], activate=False, temporary=True)
        other = p.stage_document(source_id='fixture', source_url=URL, document_key='other',
            title='另一合成候选', text='额外的合成风险条款。', facets=['risk'], shape='resolver-fixture/1')
        p.approve_version(other, verification_basis='synthetic_fixture_only')
        with p.db() as db:
            orphan = db.execute('SELECT id FROM chunks WHERE version_id=?', (other,)).fetchone()[0]
        self.assertIsNotNone(p.resolve_public_evidence(ids[0], temporary, AS_OF))
        self.assertIsNone(p.resolve_public_evidence(orphan, release, AS_OF))
        self.tamper(p, 'INSERT INTO release_chunks VALUES(?,?)', (release, orphan), drop=('sealed_member_insert',))
        self.assert_unavailable(p, release, ids[0])
        self.assert_unavailable(p, release, orphan)

    def test_dynamic_permissions_and_product_verification_are_rechecked(self):
        mutations = [
            ('UPDATE sources SET enabled=0', ()),
            ('UPDATE sources SET read_allowed=0', ()),
            ('UPDATE sources SET outbound_allowed=0', ()),
            ('UPDATE sources SET endpoints=?', (packed(['https://another.invalid/']),)),
            ('UPDATE products SET verified=0 WHERE id=?', ('PRODUCT-A',))]
        for sql, args in mutations:
            with self.subTest(sql=sql):
                p, _, release, ids = self.fixture()
                self.assertTrue(p.resolve_public_evidence_batch(ids, release, AS_OF)[ids[0]])
                self.tamper(p, sql, args)
                self.assert_unavailable(p, release, ids[0])

    def test_share_and_channel_identity_are_rechecked_after_fk_bypass(self):
        for sql in ('UPDATE share_classes SET product_id=\'PRODUCT-B\' WHERE id=\'SHARE-A\'',
                    'DELETE FROM channels WHERE id=\'CHANNEL-A\'',
                    'DELETE FROM share_classes WHERE id=\'SHARE-A\''):
            with self.subTest(sql=sql):
                p, _, release, ids = self.fixture()
                self.tamper(p, sql)
                self.assert_unavailable(p, release, ids[0])

    def test_withdrawal_conflict_expiry_and_confirmed_revision_are_dynamic(self):
        for change in ('withdrawal', 'conflict', 'expiry', 'revision'):
            with self.subTest(change=change):
                p, version, release, ids = self.fixture()
                self.assertTrue(p.resolve_public_evidence_batch(ids, release, AS_OF)[ids[0]])
                if change == 'withdrawal': p.withdraw(version)
                elif change == 'conflict':
                    p.block_conflict(product_id='PRODUCT-A', facets=['fees'], from_on='2026-10-01', reason='synthetic_conflict')
                elif change == 'revision':
                    new = p.stage_document(source_id='fixture', source_url=URL, title='合成修订版',
                        text='费用修订为0.20%。', facets=['fees'], shape='resolver-fixture/1',
                        published_on='2026-10-01', effective_from='2026-10-01')
                    p.approve_version(new, verification_basis='synthetic_revision_only')
                    self.assertIsNotNone(p.resolve_public_evidence(ids[0], release, '2026-09-20'))
                self.assert_unavailable(p, release, ids[0], '2026-11-01' if change == 'expiry' else AS_OF)

    def test_field_freshness_and_future_data_date_are_rechecked(self):
        for source_rule, data_on in (({'fees': 7, 'risk': None}, '2026-09-01'),
                                    ({'fees': None, 'risk': None}, '2026-10-07')):
            with self.subTest(data_on=data_on):
                p, _, release, ids = self.fixture()
                self.tamper(p, 'UPDATE sources SET freshness=?', (packed(source_rule),))
                self.tamper(p, 'UPDATE chunks SET data_on=? WHERE id=?', (data_on, ids[0]), drop=('immutable_chunk',))
                self.assert_unavailable(p, release, ids[0])

    def test_paused_preserves_verified_original_contract(self):
        p, version, release, ids = self.fixture()
        p.set_source_state('fixture', paused=True)
        self.assertIsNotNone(p.resolve_public_evidence(ids[0], release, AS_OF))
        with self.assertRaises(KnowledgeError): p.approve_version(version, verification_basis='must_not_reapprove_paused')
        with self.assertRaises(KnowledgeError): p.publish(version_ids=[version])

    def test_graph_withdrawal_and_provenance_tampering_fail(self):
        for change in ('withdrawal', 'provenance'):
            with self.subTest(change=change):
                p, _, release, ids = self.fixture()
                with p.db() as db:
                    edge = db.execute("SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'", (ids[0],)).fetchone()[0]
                if change == 'withdrawal': p.withdraw_relation(edge)
                else:
                    self.tamper(p, 'UPDATE graph_provenance SET quote=? WHERE relation_id=?',
                                ('modified provenance', edge), drop=('immutable_graph_provenance',))
                self.assert_unavailable(p, release, ids[0])

    def test_real_batch_read_snapshot_and_next_batch_sees_commit(self):
        p, _, release, ids = self.fixture()
        with p.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            edge = db.execute("SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'", (ids[1],)).fetchone()[0]
        original = p._available
        writes = []
        def concurrent_revision(db, row, as_of, checked=None):
            if not writes:
                with sqlite3.connect(p.path) as writer:
                    writer.execute("UPDATE graph_relations SET status='withdrawn' WHERE id=?", (edge,))
                writes.append(True)
            return original(db, row, as_of, checked)
        with patch.object(p, '_available', concurrent_revision):
            first = p.resolve_public_evidence_batch(ids[:2], release, AS_OF)
        self.assertTrue(all(first.values()))
        second = p.resolve_public_evidence_batch(ids[:2], release, AS_OF)
        self.assertIsNotNone(second[ids[0]])
        self.assertIsNone(second[ids[1]])


class WebankDisclosureOrderTests(unittest.TestCase):
    """Actual registered JSONP parser, synthetic originals, temporary SQLite."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='navren-webank-order-', ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.p = LocalKnowledgeProvider(Path(directory.name)/'public.sqlite3')
        from webank_catalog import URL
        self.url = URL
        self.p.register_source('webank-direct-disclosure', publisher='微众银行', endpoints=[URL],
            parser_id=WEBANK_PARSER, shape='webank-productpresentation-v1',
            freshness={'risk': 7}, permission_basis='synthetic_parser_regression_only')
        self.p.register_product('WEBANK:000001', issuer='WEBANK_DISCLOSURE',
            scheme='webank_channel_product_code', code='000001', name='合成目录测试产品', verified=True)
        self.p.register_channel('WEBANK', name='微众银行')

    def stage(self, source_date, risk='R2'):
        from webank_catalog import FIELD_MAP
        row = {key: '' for key in FIELD_MAP}
        row.update(code='000001', name='合成目录测试产品', risk=risk)
        raw = ('_jsonpproductpresentation('+packed({'extractDate': source_date,
                'productPresentation': [row]})+');').encode()
        text = packed(row)
        fragment = packed('risk')+':'+packed(risk)
        start = text.index(fragment)
        return self.p.stage_document(source_id='webank-direct-disclosure', source_url=self.url,
            document_key='WEBANK:000001', title=row['name'], text=text, raw_bytes=raw,
            parser_id=WEBANK_PARSER, shape='webank-productpresentation-v1',
            product_id='WEBANK:000001', channel_id='WEBANK', published_on=source_date,
            retrieved_on=AS_OF, data_on=source_date, facets=['risk'],
            spans=[{'start_char': start, 'end_char': start+len(fragment),
                    'paragraph': 'productPresentation:000001/risk', 'facets': ['risk']}])

    def approve(self, version, **options):
        return self.p.approve_version(version, verification_basis='synthetic_jsonp_fixture_review', **options)

    def version(self, vid):
        with self.p.db() as db:
            return dict(db.execute('SELECT * FROM document_versions WHERE id=?', (vid,)).fetchone())

    def citation(self, vid):
        with self.p.db() as db:
            return db.execute('SELECT id FROM chunks WHERE version_id=?', (vid,)).fetchone()[0]

    def test_late_older_original_is_rejected_without_revoking_current(self):
        new = self.stage('2026-10-05', 'R3'); self.approve(new)
        release = self.p.publish(version_ids=[new])
        old = self.stage('2026-10-01')
        for supersedes in (True, False):
            with self.subTest(supersedes=supersedes), self.assertRaisesRegex(KnowledgeError, '^source_date_regressed$'):
                self.approve(old, supersedes=supersedes)
        self.assertEqual(self.version(old)['status'], 'candidate')
        self.assertIsNone(self.version(new)['revoked_from'])
        self.assertEqual(self.p.status()['public_release_id'], release)
        self.assertIsNotNone(self.p.resolve_public_evidence(self.citation(new), release, AS_OF))

    def test_calendar_order_overrides_inverse_staging_order(self):
        new = self.stage('2026-10-05', 'R3')
        old = self.stage('2026-10-01'); self.approve(old); self.approve(new)
        self.assertLess(self.version(new)['version_no'], self.version(old)['version_no'])
        self.assertIsNone(self.version(new)['revoked_from'])
        self.assertEqual(self.version(old)['revoked_from'], '2026-10-05')
        release = self.p.publish(version_ids=[old, new])
        self.assertIsNotNone(self.p.resolve_public_evidence(self.citation(new), release, AS_OF))
        self.assertIsNone(self.p.resolve_public_evidence(self.citation(old), release, AS_OF))
        self.assertIsNotNone(self.p.resolve_public_evidence(self.citation(old), release, '2026-10-03'))

    def test_same_day_different_raw_is_ambiguous_even_after_registry_revision(self):
        first = self.stage('2026-10-05'); self.approve(first)
        release = self.p.publish(version_ids=[first])
        second = self.stage('2026-10-05', 'R3')
        # Registry revision has no immutable per-original revision binding.
        with self.p.db() as db:
            db.execute("UPDATE sources SET revision=revision+1 WHERE id='webank-direct-disclosure'")
        for supersedes in (True, False):
            with self.subTest(supersedes=supersedes), self.assertRaisesRegex(KnowledgeError, '^source_version_ambiguous$'):
                self.approve(second, supersedes=supersedes)
        self.assertEqual(self.version(second)['status'], 'candidate')
        self.assertIsNone(self.version(first)['revoked_from'])
        self.assertIsNotNone(self.p.resolve_public_evidence(self.citation(first), release, AS_OF))

    def test_verified_same_version_is_idempotent_and_keeps_revocation(self):
        old = self.stage('2026-10-01'); self.approve(old)
        new = self.stage('2026-10-05', 'R3'); self.approve(new)
        release = self.p.publish(version_ids=[old, new])
        self.assertEqual(self.stage('2026-10-01'), old)
        old_snapshot = self.version(old)
        self.p.approve_version(old, verification_basis='a cache reread cannot replace the original approval')
        self.assertEqual(self.version(old), old_snapshot)
        self.assertEqual(self.approve(old), old)
        self.assertEqual(self.approve(new), new)
        self.assertEqual(self.version(old)['revoked_from'], '2026-10-05')
        self.assertIsNone(self.version(new)['revoked_from'])
        self.assertIsNone(self.p.resolve_public_evidence(self.citation(old), release, AS_OF))
        self.assertIsNotNone(self.p.resolve_public_evidence(self.citation(old), release, '2026-10-03'))
        self.p.withdraw(old)
        with self.assertRaisesRegex(KnowledgeError, '^withdrawn_evidence$'):
            self.approve(old)

    def test_withdrawn_approved_original_still_prevents_date_regression(self):
        new = self.stage('2026-10-05'); self.approve(new); self.p.withdraw(new)
        old = self.stage('2026-10-01')
        with self.assertRaisesRegex(KnowledgeError, '^source_date_regressed$'):
            self.approve(old)
        ambiguous = self.stage('2026-10-05', 'R3')
        with self.assertRaisesRegex(KnowledgeError, '^source_version_ambiguous$'):
            self.approve(ambiguous)
        newer = self.stage('2026-10-06', 'R3'); self.approve(newer)
        self.assertIsNone(self.version(newer)['revoked_from'])

    def test_withdrawn_unapproved_candidate_does_not_establish_source_date(self):
        candidate = self.stage('2026-10-05'); self.p.withdraw(candidate)
        old = self.stage('2026-10-01'); self.approve(old)
        self.assertEqual(self.version(old)['status'], 'verified')
        self.assertIsNone(self.version(old)['revoked_from'])


def benchmark(repeats=3):
    records = []
    for size in (100, 1000, 5000):
        with tempfile.TemporaryDirectory(prefix='navren-resolver-benchmark-', ignore_cleanup_errors=True) as directory:
            p, _, release, ids = build_fixture(Path(directory)/'synthetic.sqlite3', size)
            control = FullScanLocatorProvider(p.path)
            for count in (1, 8, 32):
                requested = ids[:count]
                expected = p.resolve_public_evidence_batch(requested, release, AS_OF)
                operations = {
                    'full_scan_locator_batch': lambda: control.resolve_public_evidence_batch(requested, release, AS_OF),
                    'indexed_batch': lambda: p.resolve_public_evidence_batch(requested, release, AS_OF),
                    'full_scan_locator_individual': lambda: {cid: control.resolve_public_evidence(cid, release, AS_OF) for cid in requested},
                    'indexed_individual': lambda: {cid: p.resolve_public_evidence(cid, release, AS_OF) for cid in requested}}
                timings = {}
                for name, operation in operations.items():
                    samples = []
                    for _ in range(repeats):
                        started = time.perf_counter()
                        actual = operation()
                        samples.append((time.perf_counter()-started)*1000)
                        if actual != expected or not all(actual.values()):
                            raise AssertionError('benchmark_result_mismatch')
                    timings[name] = round(statistics.median(samples), 3)
                row = {'published_members': size, 'references': count, 'median_ms': timings}
                records.append(row)
                print(json.dumps(row), flush=True)
    return {'scope': 'synthetic_single_process_warm_cache_only', 'repeats': repeats,
            'same_security_control': True, 'manifest_checked_each_batch': True,
            'cross_connection_hash_cache': False, 'records': records}


if __name__ == '__main__':
    if '--benchmark' in sys.argv:
        print(json.dumps(benchmark(), ensure_ascii=False), flush=True)
    else:
        unittest.main()
