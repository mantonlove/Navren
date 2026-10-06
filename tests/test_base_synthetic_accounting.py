"""原始四账户历史→独立日记账覆盖→正式会计发行；临时账本，零模型。"""
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from finance_gateway import FinanceGateway
from financial_metrics import fingerprint
from base_runtime.finance_sources import synthetic_cash_snapshot
from simulated_bank import BankError


class SyntheticAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory(prefix='navren-native-accounting-')
        cls.env=patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB':str(Path(cls.folder.name)/'knowledge.sqlite3')})
        cls.env.start();cls.network=patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden'));cls.network.start()
        cls.g=FinanceGateway(Path(cls.folder.name)/'bank.sqlite3')
        cls.g.demo_accounts.initialize_offline(allow_offline=True)
        cls.evidence=[]

    @classmethod
    def tearDownClass(cls):cls.network.stop();cls.env.stop();cls.folder.cleanup()

    def state(self,w):
        with closing(self.g.bank.connect()) as db:
            return fingerprint({t:[dict(x) for x in db.execute('SELECT * FROM '+t+' WHERE workspace_id=? ORDER BY rowid',(w,))]
                for t in ('accounts','holdings','orders','operations','transactions','audit')})

    def report(self,w,args):
        before=self.state(w);r=self.g.financial_metrics.method_report(w,'accounting_report',args)
        self.g.financial_metrics.validate(w,r['metric_batch'])
        self.assertEqual(before,self.state(w))
        self.evidence.append({'test':self.id(),'arguments':args,'result':r,'before':before,'after':self.state(w)})
        return r

    def accounts(self):return self.g.demo_accounts.catalog()['accounts']

    def period(self,w,start='2026-09-01',end='2026-09-30'):
        return dict(scope=w,source='navren_synthetic',period_start=start,period_end=end,expected_versions={})

    def test_four_sealed_balance_sheets_match_original_daily_totals(self):
        for entry in self.accounts():
            w=entry['workspace_id'];record=self.g.demo_accounts._record(w)
            with closing(self.g.demo_accounts._history_connection(record)) as db:
                row=db.execute('SELECT * FROM daily_balances WHERE user_id=? AND date=?',(entry['user_id'],'2026-10-03')).fetchone()
            r=self.report(w,dict(scope=w,source='navren_synthetic',as_of='2026-10-03',expected_versions={}))
            m={v['metric_name']:v['value_minor'] for v in r['metrics']}
            self.assertEqual(m,dict(total_assets=row['total_assets_minor'],total_liabilities=row['liabilities_minor'],
                net_assets=row['net_worth_minor'],cash_balance=row['cash_minor']))
            self.assertTrue(r['historical_source_not_current_wealth'])
            self.assertEqual(r['calculation_status'],'calculated')

    def test_four_complete_months_cash_reconcile_independent_source_summaries(self):
        for entry in self.accounts():
            w=entry['workspace_id'];record=self.g.demo_accounts._record(w)
            with closing(self.g.demo_accounts._history_connection(record)) as db:
                summary=db.execute('SELECT * FROM monthly_summaries WHERE user_id=? AND period_start=?',(entry['user_id'],'2026-09-01')).fetchone()
                income=db.execute("SELECT COALESCE(SUM(signed_minor),0) FROM cash_transactions WHERE user_id=? AND kind='income' AND substr(occurred_at,1,7)='2026-09'",(entry['user_id'],)).fetchone()[0]
                refunds=db.execute("SELECT COALESCE(SUM(signed_minor),0) FROM cash_transactions WHERE user_id=? AND kind='refund' AND substr(occurred_at,1,7)='2026-09'",(entry['user_id'],)).fetchone()[0]
                expense=-db.execute("SELECT COALESCE(SUM(signed_minor),0) FROM cash_transactions WHERE user_id=? AND kind='expense' AND substr(occurred_at,1,7)='2026-09'",(entry['user_id'],)).fetchone()[0]
                interest=db.execute("""SELECT COALESCE(SUM(l.debit_minor-l.credit_minor),0) FROM journal_lines l
                    JOIN ledger_accounts a ON a.user_id=l.user_id AND a.account_id=l.account_id
                    JOIN journal_events e ON e.user_id=l.user_id AND e.event_id=l.event_id
                    WHERE l.user_id=? AND a.code LIKE 'expense:贷款利息:%' AND substr(e.occurred_at,1,7)='2026-09'""",(entry['user_id'],)).fetchone()[0]
            r=self.report(w,self.period(w));m={v['metric_name']:v['value_minor'] for v in r['metrics']}
            self.assertEqual(r['calculation_status'],'calculated',r['reasons'])
            self.assertEqual(m['total_cash_change'],summary['closing_cash_minor']-summary['opening_cash_minor'])
            self.assertEqual(m['cash_income'],income);self.assertEqual(m['cash_refunds'],refunds)
            self.assertEqual(m['cash_expenses_gross'],expense+interest)

    def test_opening_journal_is_balance_not_income(self):
        entry=self.accounts()[0];w=entry['workspace_id'];record=self.g.demo_accounts._record(w)
        with closing(self.g.demo_accounts._history_connection(record)) as db:
            opening=db.execute("SELECT SUM(opening_balance_minor) FROM ledger_accounts WHERE user_id=? AND account_class='cash'",(entry['user_id'],)).fetchone()[0]
            closing_cash=db.execute('SELECT cash_minor FROM daily_balances WHERE user_id=? AND date=?',(entry['user_id'],'2016-10-04')).fetchone()[0]
        args=self.period(w,'2016-10-04','2016-10-04');source=synthetic_cash_snapshot(self.g.demo_accounts,w,args)
        self.assertEqual(source['records']['opening_cash_minor'],opening)
        r=self.report(w,args);m={v['metric_name']:v['value_minor'] for v in r['metrics']}
        self.assertEqual(m['total_cash_change'],closing_cash-opening)
        self.assertNotEqual(m['cash_income'],opening)

    def test_source_period_outside_archive_never_mixed_with_current_cash(self):
        w=self.accounts()[0]['workspace_id'];r=self.report(w,self.period(w,'2026-10-02','2026-10-04'))
        self.assertEqual(r['calculation_status'],'unavailable')
        self.assertTrue(all(m['value_minor'] is None for m in r['metrics']))

    def test_native_catalog_is_independent_cash_journal_legs(self):
        entry=self.accounts()[2];w=entry['workspace_id'];s=synthetic_cash_snapshot(self.g.demo_accounts,w,self.period(w))
        self.assertTrue(s['coverage_complete'])
        self.assertTrue(all('-l' in i for i in s['catalog']['expected_movement_ids']))
        self.assertTrue(any(i.endswith('/principal') for i in s['catalog']['expected_movement_ids']))
        self.assertEqual(set(s['catalog']['expected_movement_ids']),{r['movement_id'] for r in s['records']['movements']})

    def test_archived_original_refunds_and_transfer_pairs_are_bound(self):
        entry=self.accounts()[0];w=entry['workspace_id'];s=synthetic_cash_snapshot(self.g.demo_accounts,w,self.period(w,'2016-10-04','2016-11-30'))
        refunds=[r for r in s['records']['movements'] if r['kind']=='refund']
        self.assertTrue(refunds)
        originals={r['movement_id']:r for r in s['records']['refund_originals']}
        for refund in refunds:
            original=originals[refund['related_id']]
            self.assertLessEqual(original['on'],refund['on'])
            self.assertEqual(original['source_version'],s['source_versions'][original['evidence_ref']])
        self.assertEqual(self.report(w,self.period(w,'2016-10-04','2016-11-30'))['calculation_status'],'calculated')

    def test_historical_research_and_current_quantity_are_not_sealed_book_value(self):
        entry=self.accounts()[0];w=entry['workspace_id']
        r=self.g.financial_metrics.returns_report(w,scope=w,portfolio_ref='personal/current',accounting_basis='equity',
            as_of='2026-10-03',expected_versions={})
        self.assertEqual(r['calculation_status'],'unavailable')
        self.assertTrue(all(m['value_minor'] is None for m in r['metrics']))


if __name__=='__main__':unittest.main()
