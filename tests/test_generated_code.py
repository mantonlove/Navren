"""正式 WAT worker 与真实临时银行签发/身份的最小接线验证；零云调用。"""
from contextlib import closing
from copy import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from base_runtime.evidence import MetricEvidenceObserver
from finance_gateway import FinanceGateway
from personal_data import PersonalDataStore
from agent_runtime.bank_tools import BankTools
from agent_runtime.generated_code import GeneratedCodeSession, TOOL_SPEC


IDENTITY_WAT = '''(module (memory (export "memory") 1)
(func (export "run") (param $p i32) (param $n i32) (result i64)
i32.const 0 local.get $p i64.load i64.store i64.const 34359738368))'''


class GeneratedCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-formal-generated-code-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        key = Fernet.generate_key()
        with patch('user_memory.personal_store', side_effect=lambda bank:
                   PersonalDataStore(bank, encryption_key=key)):
            original = FinanceGateway(self.path/'bank.sqlite3', public_catalog=object())
        self.g = copy(original)
        self.g.financial_metrics = MetricEvidenceObserver(original.financial_metrics)
        self.workspace, self.token = self.g.create_session()
        self.request = self.g.demo_sessions.request(self.token, self.workspace)
        self.request.__enter__()
        self.addCleanup(self.request.__exit__, None, None, None)
        self.tools = BankTools(self.g, self.workspace, 'generated-fixture-request')
        self.tools.invoke('account_summary', {})
        self.metric = next(m for b in self.tools.metric_batches for m in b['metrics']
            if m['metric_name'] == 'settled_cash_balance'
            and m['calculation_status'] == 'calculated' and not m['coverage_gaps'])
        self.cancel = threading.Event()
        self.session = GeneratedCodeSession(self.tools, audit_dir=self.path/'audit',
            deadline=time.monotonic()+105, cancel=self.cancel)
        self.addCleanup(self.session.close)

    def execute(self, wat=IDENTITY_WAT, **extra):
        return self.session.execute({'metric_id': self.metric['metric_id'], 'wat': wat, **extra})

    def receipts(self):
        return [json.loads(p.read_text()) for p in (self.path/'audit/worker-receipts').glob('*.json')]

    def deny(self, call):
        with self.assertRaises(Exception) as caught:
            call()
        self.assertNotIsInstance(caught.exception, AssertionError)
        return getattr(caught.exception, 'code', str(caught.exception))

    def test_true_formal_worker_reproduces_without_new_fact_or_financial_write(self):
        with closing(self.g.bank.connect()) as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
        ids = self.tools.metrics.metric_ids
        result = self.execute()
        self.assertNotIn('value_minor', result)
        self.assertFalse(result['financial_execution_authorized'])
        self.assertFalse(result['new_financial_metric_registered'])
        self.assertTrue(result['independent_review_required'])
        self.assertEqual(self.tools.metrics.metric_ids, ids)
        self.assertTrue(self.session.verify_fresh())
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')])
            db.execute('BEGIN IMMEDIATE')
            self.assertTrue(self.session.verify_for_publication(db))
        receipt, = self.receipts()
        self.assertEqual(receipt['runtime']['version'], '49.0.0')
        self.assertIn('/agent_runtime/sandbox_runtime/', str(Path(__file__).resolve().parents[1]/
            'agent_runtime/sandbox_runtime/worker.py'))
        self.assertGreater(receipt['worker_pid'], 0)
        self.assertEqual(receipt['status'], 'COMPLETED_CANDIDATE')
        self.assertTrue(receipt['worker_reaped'])
        self.assertTrue(receipt['worker_group_absent'])
        self.assertTrue(receipt['temporary_directory_removed'])
        self.assertEqual(receipt['worker_report']['imports_count'], 0)
        self.assertFalse(receipt['worker_report']['wasi_configured'])

    def test_every_guest_host_capability_import_is_denied_by_real_worker(self):
        for capability in ('filesystem', 'network', 'process', 'environment', 'bank_write'):
            with self.subTest(capability=capability):
                wat = '(module (import "env" "'+capability+'" (func)) '+IDENTITY_WAT[len('(module '):]
                self.assertEqual(self.deny(lambda: self.execute(wat)), 'GENERATED_CODE_WORKER_REJECTED')
                self.assertEqual(self.receipts()[-1]['reason'], 'all_imports_forbidden')
        wat = '(module (import "wasi_snapshot_preview1" "fd_read" (func)) '+IDENTITY_WAT[len('(module '):]
        self.assertEqual(self.deny(lambda: self.execute(wat)), 'GENERATED_CODE_WORKER_REJECTED')
        self.assertTrue(all(r['worker_report']['imports_count'] == 1 for r in self.receipts()))
        self.assertTrue(all(r['worker_reaped'] and r['worker_group_absent'] for r in self.receipts()))

    def test_wrong_result_is_not_consumed_as_registered_amount(self):
        wat = '(module (memory (export "memory") 1) (func (export "run") (param i32 i32) (result i64) i64.const 34359738368))'
        self.assertEqual(self.deny(lambda: self.execute(wat)), 'GENERATED_CODE_RESULT_MISMATCH')
        self.assertEqual(self.session.receipt_refs(), [])
        self.assertEqual(self.receipts()[0]['status'], 'COMPLETED_CANDIDATE')

    def test_infinite_loop_exhausts_fuel_and_worker_is_reaped(self):
        wat = '(module (memory (export "memory") 1) (func (export "run") (param i32 i32) (result i64) (loop $forever (br $forever)) i64.const 0))'
        self.assertEqual(self.deny(lambda: self.execute(wat)), 'GENERATED_CODE_WORKER_REJECTED')
        receipt, = self.receipts()
        self.assertEqual(receipt['reason'], 'fuel_exhausted')
        self.assertTrue(receipt['worker_reaped'] and receipt['worker_group_absent'])
        self.assertTrue(receipt['temporary_directory_removed'])

    def test_unknown_or_forged_metric_and_extra_host_arguments_never_spawn(self):
        self.deny(lambda: self.session.execute({'metric_id': 'not-issued', 'wat': IDENTITY_WAT}))
        self.assertEqual(self.deny(lambda: self.execute(workspace='other')),
                         'GENERATED_CODE_ARGUMENTS_INVALID')
        self.tools.metrics._records[self.metric['metric_id']]['metric']['value_minor'] += 1
        self.assertEqual(self.deny(self.execute), 'GENERATED_CODE_ISSUANCE_MISMATCH')
        self.assertEqual(self.receipts(), [])

    def test_privacy_disabled_or_unregistered_observer_never_spawn(self):
        self.g.financial_metrics.batches.clear()
        self.assertEqual(self.deny(self.execute), 'GENERATED_CODE_ORIGINAL_OBSERVER_REQUIRED')
        self.assertEqual(self.receipts(), [])

    def test_original_source_changed_after_execution_blocks_final_same_db_hook(self):
        self.execute()
        with self.g.bank.transaction() as db:
            db.execute('UPDATE accounts SET balance_minor=balance_minor+1 WHERE workspace_id=?',
                       (self.workspace,))
        with closing(self.g.bank.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            self.deny(lambda: self.session.verify_for_publication(db))

    def test_other_host_session_and_cancel_cannot_reuse_request_scope(self):
        other_workspace, other_token = self.g.create_session()
        with self.g.demo_sessions.request(other_token, other_workspace):
            self.deny(self.execute)
        self.cancel.set()
        self.assertEqual(self.deny(self.execute), 'GENERATED_CODE_CANCELLED')
        self.assertEqual(self.receipts(), [])

    def test_worker_deadline_cannot_extend_original_request_deadline(self):
        self.session.deadline = time.monotonic()-1
        self.assertEqual(self.deny(self.execute), 'GENERATED_CODE_REQUEST_EXPIRED')
        self.assertEqual(self.receipts(), [])

    def test_source_validation_returning_after_deadline_cannot_publish(self):
        self.execute()
        original = self.g.financial_metrics.validate
        def late(*args, **kwargs):
            checked = original(*args, **kwargs)
            self.session.deadline = time.monotonic()-1
            return checked
        with patch.object(self.g.financial_metrics, 'validate', side_effect=late):
            with closing(self.g.bank.connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                self.assertEqual(self.deny(lambda: self.session.verify_for_publication(db)),
                                 'GENERATED_CODE_REQUEST_EXPIRED')

    def test_schema_has_only_wat_and_original_metric_id(self):
        schema = TOOL_SPEC[1]
        self.assertEqual(set(schema['properties']), {'wat', 'metric_id'})
        self.assertEqual(set(schema['required']), {'wat', 'metric_id'})
        self.assertFalse(schema['additionalProperties'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
