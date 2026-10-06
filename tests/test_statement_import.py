"""本机CSV账单：预览、来源隔离、精确统计、幂等和整批回滚。"""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from simulated_bank import BankError, SimulatedBank
from simulated_bank.statement_import import StatementImportService


def csv_doc(rows, *, headers='流水号,日期,商户,金额,类型,分类,币种,备注'):
    return headers+'\n'+'\n'.join(rows)+'\n'


class StatementImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1791018000.0]
        self.bank = SimulatedBank(Path(self.temp.name)/'bank.sqlite3', clock=lambda:self.now[0])
        self.credentials = self.bank.create_workspace()
        self.workspace = self.credentials['workspace_id']
        self.other_workspace = self.bank.create_workspace()['workspace_id']
        self.service = StatementImportService(self.bank, clock=lambda:self.now[0])
        self.rows = [
            'income-1,2026-09-01,工资,100.01,收入,,CNY,',
            'expense-1,2026-09-02,咖啡店,20.10,支出,餐饮,CNY,',
            'refund-1,2026-09-03,咖啡店,5.10,退款,,CNY,',
            'transfer-1,2026-09-04,内部转账,33.00,转账,,CNY,'
        ]

    def test_preview_commit_and_replay_preserve_simulator_ledger(self):
        before = self.bank.snapshot(self.workspace)
        mandates = self.bank.mandates(self.workspace)
        simulator_transactions = self.bank.transactions(self.workspace)
        preview = self.service.preview(self.workspace, csv_doc(self.rows))
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['input_count'], 4)
        self.assertEqual(preview['new_count'], 4)
        self.assertIsNone(self.service.source(self.workspace)['dataset'])
        result = self.service.commit(self.workspace, {'batch_id':preview['batch_id'],'confirmed':True})
        replay = self.service.commit(self.workspace, {'batch_id':preview['batch_id'],'confirmed':True})
        self.assertEqual(result['inserted_count'],4)
        self.assertFalse(result['idempotent_replay'])
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(self.bank.snapshot(self.workspace)['total_cash_minor'],before['total_cash_minor'])
        self.assertEqual(self.bank.snapshot(self.workspace)['version'],before['version'])
        self.assertEqual(self.bank.mandates(self.workspace),mandates)
        self.assertEqual(self.bank.transactions(self.workspace),simulator_transactions)
        rows = self.service.transactions(self.workspace, '2026-09')
        self.assertEqual(len(rows),4)
        report = self.service.analysis(self.workspace,'2026-09')
        self.assertEqual(report['totals_minor']['income'],10001)
        self.assertEqual(report['totals_minor']['expense'],2010)
        self.assertEqual(report['totals_minor']['refund'],510)
        self.assertEqual(report['totals_minor']['transfer'],3300)
        self.assertEqual(report['income_expense_surplus_minor'],8501)
        self.assertIsNone(report['cash_delta_minor'])
        self.assertEqual(report['category_expense_minor'],{'餐饮':2010})
        self.assertEqual(report['category_transaction_ids'],{'餐饮':['expense-1']})
        self.assertEqual(sum(report['category_expense_minor'].values()),report['totals_minor']['expense'])
        self.assertEqual(report['source'],'imported_local_csv')

    def test_identical_transaction_reimports_skip_and_conflicts_block(self):
        first = self.service.preview(self.workspace,csv_doc(self.rows[:1]))
        self.service.commit(self.workspace,{'batch_id':first['batch_id'],'confirmed':True})
        duplicate = self.service.preview(self.workspace,csv_doc(self.rows[:1]))
        self.assertEqual(duplicate['new_count'],0)
        self.assertEqual(duplicate['duplicate_count'],1)
        result = self.service.commit(self.workspace,{'batch_id':duplicate['batch_id'],'confirmed':True})
        self.assertEqual(result['inserted_count'],0)
        self.assertEqual(result['skipped_count'],1)
        conflict = self.service.preview(self.workspace,csv_doc(['income-1,2026-09-01,工资,999.00,收入,,CNY,']))
        self.assertFalse(conflict['valid'])
        self.assertIn('内容不同',conflict['errors'][0]['message'])
        self.assertEqual(len(self.service.transactions(self.workspace)),1)

    def test_parallel_commit_same_batch_returns_one_idempotent_receipt(self):
        preview=self.service.preview(self.workspace,csv_doc(self.rows[:1]))
        barrier=Barrier(2)
        original=self.bank.transaction

        @contextmanager
        def synchronized_transaction():
            barrier.wait(timeout=5)
            with original() as db:
                yield db

        payload={'batch_id':preview['batch_id'],'confirmed':True}
        with patch.object(self.bank,'transaction',side_effect=synchronized_transaction):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:self.service.commit(self.workspace,payload),range(2)))
        self.assertCountEqual([result['idempotent_replay'] for result in results],[False,True])
        self.assertEqual([result['inserted_count'] for result in results],[1,1])
        with self.bank.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM imported_statement_transactions WHERE workspace_id=?',
                                        (self.workspace,)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM statement_import_batches WHERE workspace_id=?',
                                        (self.workspace,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE workspace_id=? AND event='statement_import_committed'",
                                        (self.workspace,)).fetchone()[0],1)

    def test_identical_rows_inside_one_file_have_consistent_duplicate_counts(self):
        row='same-in-file,2026-09-05,同一笔收入,8.25,收入,工资,CNY,'
        preview=self.service.preview(self.workspace,csv_doc([row,row]))
        self.assertEqual(preview['input_count'],2)
        self.assertEqual(preview['new_count'],1)
        self.assertEqual(preview['duplicate_count'],1)
        result=self.service.commit(self.workspace,{'batch_id':preview['batch_id'],'confirmed':True})
        self.assertEqual(result['inserted_count'],1)
        self.assertEqual(result['skipped_count'],1)

    def test_preview_cache_evicts_oldest_per_workspace_only(self):
        foreign=self.service.preview(self.other_workspace,csv_doc(self.rows[:1]))
        previews=[self.service.preview(self.workspace,csv_doc(self.rows[:1])) for _ in range(5)]
        with self.assertRaises(BankError) as raised:
            self.service.commit(self.workspace,{'batch_id':previews[0]['batch_id'],'confirmed':True})
        self.assertEqual(raised.exception.code,'PREVIEW_EXPIRED')
        foreign_result=self.service.commit(self.other_workspace,{'batch_id':foreign['batch_id'],'confirmed':True})
        self.assertEqual(foreign_result['inserted_count'],1)
        retained=self.service.commit(self.workspace,{'batch_id':previews[-1]['batch_id'],'confirmed':True})
        self.assertEqual(retained['inserted_count'],1)

    def test_header_only_csv_cannot_create_empty_imported_dataset(self):
        preview=self.service.preview(self.workspace,'流水号,日期,商户,金额,类型,分类,币种,备注\n')
        self.assertFalse(preview['valid'])
        self.assertIn('至少需要一笔流水',preview['errors'][0]['message'])
        self.assertIsNone(self.service.source(self.workspace)['dataset'])
        with self.assertRaises(BankError) as raised:
            self.service.set_source(self.workspace,{'source':'imported'})
        self.assertEqual(raised.exception.code,'NO_IMPORTED_DATA')

    def test_invalid_rows_currency_precision_and_conflicting_duplicates_block_whole_batch(self):
        bad = csv_doc([
            'ok-1,2026-09-01,咖啡,1.00,支出,餐饮,CNY,',
            'bad-money,2026-09-02,咖啡,1.001,支出,餐饮,CNY,',
            'bad-fx,2026-09-03,店,2.00,支出,,USD,',
            'conflict,2026-09-04,店,3.00,支出,,CNY,',
            'conflict,2026-09-04,店,4.00,支出,,CNY,'
        ])
        preview = self.service.preview(self.workspace,bad)
        self.assertFalse(preview['valid'])
        self.assertGreaterEqual(len(preview['errors']),3)
        self.assertEqual(self.service.source(self.workspace)['dataset'],None)
        with self.assertRaises(BankError) as raised:
            self.service.transactions(self.workspace)
        self.assertEqual(raised.exception.code,'NO_IMPORTED_DATA')
        self.assertEqual(self.bank.snapshot(self.workspace)['total_cash_minor'],2439000)

    def test_reference_csv_reconciles_with_demo_report_without_cross_source_writes(self):
        fixture=(Path(__file__).parents[1]/'simulated_bank'/'fixtures'/'demo-statements.csv').read_text(encoding='utf-8-sig')
        before=self.bank.snapshot(self.workspace)
        preview=self.service.preview(self.workspace,fixture)
        self.assertTrue(preview['valid'],preview['errors'])
        self.assertEqual(preview['new_count'],101)
        result=self.service.commit(self.workspace,{'batch_id':preview['batch_id'],'confirmed':True})
        self.assertEqual(result['inserted_count'],101)
        imported=self.service.analysis(self.workspace,'2026-09')
        simulated=self.bank.analysis(self.workspace,'2026-09')
        self.assertEqual(imported['transaction_count'],simulated['transaction_count'])
        self.assertEqual(imported['totals_minor'],simulated['totals_minor'])
        self.assertEqual(imported['category_expense_minor'],simulated['category_expense_minor'])
        after=self.bank.snapshot(self.workspace)
        self.assertEqual(before['version'],after['version'])
        self.assertEqual(before['total_cash_minor'],after['total_cash_minor'])

    def test_cancel_expiry_and_cross_workspace_access_do_not_import(self):
        preview = self.service.preview(self.workspace,csv_doc(self.rows[:1]))
        self.assertEqual(self.service.cancel(self.workspace,{'batch_id':preview['batch_id']})['status'],'cancelled')
        with self.assertRaises(Exception) as raised:
            self.service.commit(self.workspace,{'batch_id':preview['batch_id'],'confirmed':True})
        self.assertEqual(raised.exception.code,'PREVIEW_EXPIRED')
        expiring = self.service.preview(self.workspace,csv_doc(self.rows[:1]))
        self.now[0] += 601
        with self.assertRaises(Exception) as raised:
            self.service.commit(self.workspace,{'batch_id':expiring['batch_id'],'confirmed':True})
        self.assertEqual(raised.exception.code,'PREVIEW_EXPIRED')
        foreign = self.service.preview(self.workspace,csv_doc(self.rows[:1]))
        with self.assertRaises(Exception) as raised:
            self.service.commit(self.other_workspace,{'batch_id':foreign['batch_id'],'confirmed':True})
        self.assertEqual(raised.exception.code,'PREVIEW_EXPIRED')
        with self.assertRaises(BankError) as raised:
            self.service.transactions(self.workspace)
        self.assertEqual(raised.exception.code,'NO_IMPORTED_DATA')

    def test_failed_row_write_rolls_back_dataset_batch_and_audit(self):
        preview = self.service.preview(self.workspace,csv_doc(self.rows))
        with self.bank.transaction() as db:
            db.execute('''CREATE TRIGGER reject_statement_row BEFORE INSERT ON imported_statement_transactions
                          BEGIN SELECT RAISE(ABORT,'injected failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.commit(self.workspace,{'batch_id':preview['batch_id'],'confirmed':True})
        self.assertEqual(self.service.source(self.workspace)['dataset'],None)
        with self.bank.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM statement_import_batches').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE event='statement_import_committed'").fetchone()[0],0)

    def test_imported_periodic_labels_are_local_and_persist(self):
        data = csv_doc([
            'm1,2026-07-01,音乐会员,9.99,支出,订阅,CNY,',
            'm2,2026-08-01,音乐会员,9.99,支出,订阅,CNY,',
            'm3,2026-09-01,音乐会员,9.99,支出,订阅,CNY,'
        ])
        preview=self.service.preview(self.workspace,data)
        self.service.commit(self.workspace,{'batch_id':preview['batch_id'],'confirmed':True})
        candidate=self.service.periodic_charges(self.workspace)['candidates'][0]
        before=self.bank.snapshot(self.workspace)
        result=self.service.mark_periodic_candidate(self.workspace,{'candidate_id':candidate['candidate_id'],'decision':'confirmed'})
        self.assertIn('不会修改模拟银行代扣',result['notice'])
        after=self.bank.snapshot(self.workspace)
        self.assertEqual(before['version'],after['version'])
        self.assertEqual(before['total_cash_minor'],after['total_cash_minor'])
        reopened=StatementImportService(SimulatedBank(self.bank.database,clock=lambda:self.now[0]),clock=lambda:self.now[0])
        persisted=next(c for c in reopened.periodic_charges(self.workspace)['candidates'] if c['candidate_id']==candidate['candidate_id'])
        self.assertEqual(persisted['user_decision'],'confirmed')
        self.assertIsNone(persisted['mandate_id'])
        changed=reopened.preview(self.workspace,csv_doc(['m4,2026-10-01,音乐会员,9.99,支出,订阅,CNY,']))
        reopened.commit(self.workspace,{'batch_id':changed['batch_id'],'confirmed':True})
        with self.assertRaises(BankError) as raised:
            reopened.mark_periodic_candidate(self.workspace,{'candidate_id':candidate['candidate_id'],'decision':'ignored'})
        self.assertEqual(raised.exception.code,'CANDIDATE_CHANGED')

    def test_duplicate_anomaly_requires_exact_times_and_ignores_date_only_rows(self):
        data=csv_doc([
            'd1,2026-09-01,同一商户,88.00,支出,购物,CNY,',
            'd2,2026-09-01,同一商户,88.00,支出,购物,CNY,',
            't1,2026-09-02T12:01:00+08:00,另一商户,66.00,支出,购物,CNY,',
            't2,2026-09-02T12:05:00+08:00,另一商户,66.00,支出,购物,CNY,'
        ])
        preview=self.service.preview(self.workspace,data)
        self.assertTrue(preview['valid'],preview['errors'])
        self.service.commit(self.workspace,{'batch_id':preview['batch_id'],'confirmed':True})
        anomalies=self.service.anomalies(self.workspace)
        self.assertEqual(len(anomalies['duplicates']),1)
        self.assertEqual(anomalies['duplicates'][0]['transaction_ids'],['t1','t2'])


if __name__ == '__main__':
    unittest.main()
