"""四画像导入的金额对账、来源隔离、显式许可与失败恢复回归。"""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from demo_accounts import DemoAccountService, SOURCE, USER_IDS, REGISTRY
from personal_data import PersonalDataStore
from simulated_bank.service import SimulatedBank, BankError
from simulated_bank.customer_archive import verify_invariants


SOURCE_DIR = Path(__file__).resolve().parents[1] / 'outputs/contest-finances'
EXPECTED = {
    'tycoon': (9466265796, 24196640384, 0, 262520039660),
    'grain_farmer': (1216248131, 192383009, 0, 1998346366),
    'working_landlady': (13508826, 31754812, 64000000, 710212760),
    'retired_entrepreneur': (73633324, 53388934, 4000000, 332985415),
}


class DemoAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dir = tempfile.TemporaryDirectory(prefix='navren-four-profile-base-')
        cls.key = Fernet.generate_key()
        cls.base_bank = SimulatedBank(Path(cls.base_dir.name) / 'bank.sqlite3')
        cls.base_store = PersonalDataStore(cls.base_bank, encryption_key=cls.key)
        cls.base_service = DemoAccountService(SimpleNamespace(bank=cls.base_bank, memories=SimpleNamespace(store=cls.base_store)))
        cls.first_result = cls.base_service.initialize_offline(allow_offline=True)

    @classmethod
    def tearDownClass(cls):
        cls.base_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='navren-four-profile-test-')
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'bank.sqlite3'
        with closing(self.base_bank.connect()) as src, closing(sqlite3.connect(path)) as dest:
            src.backup(dest)
        self.bank = SimulatedBank(path)
        private = path.with_name(path.stem + '.personal.sqlite3')
        with closing(self.base_store.connect()) as src, closing(sqlite3.connect(private)) as dest:
            src.backup(dest)
        self.store = PersonalDataStore(self.bank, encryption_key=self.key)
        self.gateway = SimpleNamespace(bank=self.bank, memories=SimpleNamespace(store=self.store))
        self.service = DemoAccountService(self.gateway)

    def fresh(self):
        bank = SimulatedBank(Path(self.tmp.name) / 'fresh.sqlite3')
        store = PersonalDataStore(bank, encryption_key=self.key)
        gateway = SimpleNamespace(bank=bank, memories=SimpleNamespace(store=store))
        return DemoAccountService(gateway), bank, store

    def source_copy(self):
        target = Path(self.tmp.name) / 'source'
        target.mkdir()
        # 原始历史只读复用；故障只改当前测试的 JSON 文件。
        (target / 'financial_data.sqlite3').symlink_to(SOURCE_DIR / 'financial_data.sqlite3')
        shutil.copyfile(SOURCE_DIR / 'contest_profiles.json', target / 'contest_profiles.json')
        for name in ('summary.json', 'data_contract.json', 'manifest.json'):
            shutil.copyfile(SOURCE_DIR / name, target / name)
        for uid in USER_IDS:
            (target / uid).mkdir()
            shutil.copyfile(SOURCE_DIR / uid / 'current_finances.json', target / uid / 'current_finances.json')
        return target

    def test_constructor_and_permission_do_not_import(self):
        service, bank, _ = self.fresh()
        self.assertEqual(service.catalog()['initialized_count'], 0)
        with self.assertRaises(BankError) as error:
            service.initialize_offline()
        self.assertEqual(error.exception.code, 'DEMO_OFFLINE_PERMISSION_REQUIRED')
        with closing(bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspaces').fetchone()[0], 0)
            self.assertFalse(service._has_registry(db))

    def test_exact_totals_stable_ids_and_no_history_replay(self):
        catalog = self.service.catalog()
        self.assertEqual([r['user_id'] for r in catalog['accounts']], list(USER_IDS))
        self.assertEqual(len({r['workspace_id'] for r in catalog['accounts']}), 4)
        for uid, expected in EXPECTED.items():
            with self.subTest(uid=uid):
                workspace = self.service.workspace_for(uid)
                self.assertEqual(workspace, DemoAccountService.stable_workspace(uid))
                current = self.service.current(workspace)
                state = current['bank_state']
                self.assertEqual((state['cash_minor'], state['holding_principal_minor'], state['liabilities_minor'],
                                  current['latest_balances']['net_worth_minor']), expected)
                self.assertEqual(current['source'], SOURCE)
                self.assertEqual(current['as_of'], '2026-10-03')
                self.assertEqual(sum(a['book_value_minor'] for a in current['assets'] if a['account_class'] == 'investment'),
                                 current['latest_balances']['investment_minor'])
                self.assertTrue(all(h['quantity'] is None and h['execution_enabled'] is False and h['locked_until'] is None
                                    for h in state['holdings']))
                with closing(self.bank.connect()) as db:
                    for table in ('transactions', 'operations', 'orders', 'archived_transactions', 'loan_installments', 'identifiers', 'employment', 'kyc_reviews'):
                        self.assertEqual(db.execute('SELECT count(*) FROM ' + table + ' WHERE workspace_id=?', (workspace,)).fetchone()[0], 0)
                    self.assertTrue(verify_invariants(db, workspace)['ok'])
                    customer = db.execute('SELECT * FROM customers WHERE workspace_id=?', (workspace,)).fetchone()
                    self.assertIsNone(customer['birth_date'])
                    self.assertEqual(customer['display_name'], current['display_name'])
                    self.assertEqual(customer['nationality'], '未提供')
                    self.assertTrue(all(r['balance_minor'] == r['opening_balance_minor'] for r in db.execute('SELECT * FROM accounts WHERE workspace_id=?', (workspace,))))
                main = self.service.map_account_id(workspace, uid + '::cash:daily')
                self.assertTrue(main.endswith('-main'))
                self.assertEqual(next(a['source_account_id'] for a in state['accounts'] if a['id'] == main), uid + '::cash:daily')

    def test_original_history_is_isolated_paginated_and_partial_period_preserved(self):
        for uid in USER_IDS:
            workspace = self.service.workspace_for(uid)
            page = self.service.history(workspace, 'daily_balances', limit=2)
            self.assertEqual(page['total'], 3652)
            self.assertEqual([r['date'] for r in page['rows']], ['2016-10-04', '2016-10-05'])
            self.assertTrue(all(r['user_id'] == uid for r in page['rows']))
            next_page = self.service.history(workspace, 'daily_balances', limit=2, offset=2)
            self.assertEqual(next_page['rows'][0]['date'], '2016-10-06')
            annual = self.service.history(workspace, 'annual_summaries', period='2016')
            self.assertEqual(annual['rows'][0]['period_start'], '2016-10-04')
            self.assertEqual(annual['rows'][0]['partial_period'], 1)
            self.assertTrue(annual['coverage_partial_period'])
            monthly = self.service.history(workspace, 'monthly_summaries', period='2026-10')
            self.assertEqual(monthly['rows'][0]['period_end'], '2026-10-03')
            self.assertEqual(monthly['rows'][0]['partial_period'], 1)
            lines = self.service.history(workspace, 'journal_lines', period='2017-01', limit=500)
            self.assertTrue(all(r['user_id'] == uid and r['event_id'].startswith(uid + '-') for r in lines['rows']))
        workspace = self.service.workspace_for('tycoon')
        for table, period in [('workspaces', None), ('cash_transactions;DROP TABLE users', None), ('cash_transactions', "2026-09' OR 1=1")]:
            with self.assertRaises(BankError):
                self.service.history(workspace, table, period)
        with self.assertRaises(BankError):
            self.service.history('other-workspace', 'users')
        with self.assertRaises(BankError):
            self.service.history(workspace, 'users', limit=0)

    def test_history_forecast_not_paid_and_unknown_contract_fields(self):
        workspace = self.service.workspace_for('working_landlady')
        forecasts = self.service.history(workspace, 'recurring_obligations')
        self.assertTrue(forecasts['forecast_not_paid'])
        self.assertTrue(forecasts['rows'])
        self.assertTrue(all(r['status'] == 'forecast_not_paid' for r in forecasts['rows']))
        current = self.service.current(workspace)
        self.assertEqual(current['forecasts'], current['recurring_obligations'])
        self.assertTrue(all(r['next_due_on'] > current['end_date'] for r in current['forecasts']))
        with closing(self.bank.connect()) as db:
            loan = db.execute('SELECT * FROM loans WHERE workspace_id=?', (workspace,)).fetchone()
            for key in ('original_principal_minor', 'started_on', 'matures_on'):
                self.assertIsNone(loan[key])
            self.assertEqual(json.loads(loan['details'])['repayment_schedule_status'], 'unknown')
            self.assertTrue(all(r['due_on'] is None for r in db.execute('SELECT * FROM wealth_goals WHERE workspace_id=?', (workspace,))))

    def test_rerun_preserves_running_balances_tokens_and_personal_preferences(self):
        workspace = self.service.workspace_for('grain_farmer')
        main = self.service.map_account_id(workspace, 'grain_farmer::cash:daily')
        with self.bank.transaction() as db:
            db.execute('UPDATE accounts SET balance_minor=balance_minor+100 WHERE id=?', (main,))
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('test-runtime-income', workspace, main, '2026-10-03T18:00:00+08:00', '合成收入测试', 100, 100,
                        'income', '测试', '运行事件；不属于来源历史', None, None))
            db.execute('UPDATE workspaces SET version=2 WHERE id=?', (workspace,))
            before = dict(db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone())
        with self.store.transaction(workspace, change=True) as db:
            db.execute('UPDATE user_memory_settings SET enabled=1 WHERE workspace_id=?', (workspace,))
        revision_before = None
        with closing(self.store.connect()) as db:
            revision_before = db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?', (workspace,)).fetchone()[0]
        result = self.service.initialize_offline(allow_offline=True)
        self.assertTrue(result['already_initialized'])
        current = self.service.current(workspace)
        self.assertEqual(current['bank_state']['cash_minor'], EXPECTED['grain_farmer'][0] + 100)
        self.assertEqual(current['latest_balances']['cash_minor'], EXPECTED['grain_farmer'][0])
        with closing(self.bank.connect()) as db:
            after = dict(db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone())
            self.assertEqual(after, before)
            self.assertTrue(verify_invariants(db, workspace)['ok'])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?', (workspace,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?', (workspace,)).fetchone()[0], revision_before)

    def test_default_personal_seed_disabled_no_inferred_memories(self):
        with closing(self.store.connect()) as db:
            for uid in USER_IDS:
                workspace = self.service.workspace_for(uid)
                self.assertEqual(db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?', (workspace,)).fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?', (workspace,)).fetchone()[0], 0)
                for table in ('user_memories', 'profile_observations', 'memory_candidates'):
                    self.assertEqual(db.execute('SELECT count(*) FROM ' + table + ' WHERE workspace_id=?', (workspace,)).fetchone()[0], 0)

    def test_new_source_version_rejected_without_replacing_state(self):
        copied = self.source_copy()
        current = copied / 'tycoon/current_finances.json'
        data = json.loads(current.read_text())
        data['latest_balances']['cash_minor'] += 1
        current.write_text(json.dumps(data, ensure_ascii=False))
        service = DemoAccountService(self.gateway, copied)
        with self.assertRaises(BankError) as error:
            service.initialize_offline(allow_offline=True)
        self.assertEqual(error.exception.code, 'DEMO_SOURCE_VERSION_CONFLICT')
        self.assertEqual(service.current(service.workspace_for('tycoon'))['bank_state']['cash_minor'], EXPECTED['tycoon'][0])

    def test_unregistered_stable_workspace_is_never_overwritten(self):
        service, bank, _ = self.fresh()
        workspace = service.stable_workspace('tycoon')
        with bank.transaction() as db:
            db.execute('INSERT INTO workspaces(id,token_hash,agent_token_hash,pin_hash,as_of,version,created_at) VALUES(?,?,?,?,?,?,?)',
                       (workspace, 'existing-token', 'existing-agent', 'existing-pin', '2026-09-01', 42, 1))
        with self.assertRaises(BankError) as error:
            service.initialize_offline(allow_offline=True)
        self.assertEqual(error.exception.code, 'DEMO_WORKSPACE_CONFLICT')
        with closing(bank.connect()) as db:
            self.assertEqual(db.execute('SELECT version FROM workspaces WHERE id=?', (workspace,)).fetchone()[0], 42)

    def test_mid_import_rolls_back_all_profiles_and_copied_history(self):
        service, bank, _ = self.fresh()
        original = service._seed_workspace
        def fail_second(db, uid, *args):
            if uid == 'grain_farmer':
                raise RuntimeError('测试中途失败')
            return original(db, uid, *args)
        with patch.object(service, '_seed_workspace', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, '测试中途失败'):
                service.initialize_offline(allow_offline=True)
        self.assertEqual(service.catalog()['initialized_count'], 0)
        with closing(bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspaces').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM products WHERE id LIKE 'DEMO-%'").fetchone()[0], 0)
        self.assertFalse(list(bank.database.parent.glob('fresh.demo-source-*')))

    def test_pending_private_failure_is_not_exposed_and_retry_does_not_reset(self):
        service, bank, store = self.fresh()
        with patch.object(store, 'transaction', side_effect=RuntimeError('测试私库失败')):
            with self.assertRaisesRegex(RuntimeError, '测试私库失败'):
                service.initialize_offline(allow_offline=True)
        self.assertEqual(service.catalog()['initialized_count'], 0)
        self.assertFalse(service.is_demo(service.stable_workspace('tycoon')))
        with closing(bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspaces').fetchone()[0], 4)
            self.assertEqual({r[0] for r in db.execute('SELECT status FROM ' + REGISTRY)}, {'pending'})
        result = service.initialize_offline(allow_offline=True)
        self.assertTrue(result['already_initialized'])
        self.assertEqual(result['initialized_count'], 4)
        self.assertEqual(service.current(service.workspace_for('tycoon'))['bank_state']['cash_minor'], EXPECTED['tycoon'][0])

    def test_source_json_disagrees_with_ledger_rejected(self):
        service, bank, _ = self.fresh()
        copied = self.source_copy()
        data_file = copied / 'tycoon/current_finances.json'
        data = json.loads(data_file.read_text())
        data['accounts'][0]['balance_minor'] += 1
        data_file.write_text(json.dumps(data, ensure_ascii=False))
        service = DemoAccountService(service.gateway, copied)
        with self.assertRaises(BankError) as error:
            service.initialize_offline(allow_offline=True)
        self.assertEqual(error.exception.code, 'DEMO_SOURCE_AUDIT_FAILED')
        with closing(bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspaces').fetchone()[0], 0)

    def test_history_snapshot_has_readonly_copy_with_original_links(self):
        workspace = self.service.workspace_for('tycoon')
        record = self.service._record(workspace)
        self.assertNotEqual(Path(record['history_path']), SOURCE_DIR / 'financial_data.sqlite3')
        with closing(self.service._history_connection(record)) as db:
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM cash_transactions WHERE user_id='tycoon'")
        page = self.service.history(workspace, 'cash_transactions', limit=1)
        with closing(sqlite3.connect(record['history_path'])) as db:
            event = db.execute('SELECT user_id FROM journal_events WHERE event_id=?', (page['rows'][0]['event_id'],)).fetchone()
            self.assertEqual(event[0], 'tycoon')


if __name__ == '__main__':
    unittest.main()
