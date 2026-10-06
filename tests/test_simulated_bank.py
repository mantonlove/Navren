"""验证模拟账本不变量、授权边界、回滚、重放和真实HTTP接口。"""
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from simulated_bank import BankError, SimulatedBank
from simulated_bank.http_api import create_server
from simulated_bank.tools import BankTools
from simulated_bank.customer_schema import SPECS


class BankTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1791018000.0]
        self.path = Path(self.temp.name)/'bank.sqlite3'
        self.bank = SimulatedBank(self.path,clock=lambda:self.now[0])
        self.credentials = self.bank.create_workspace()
        self.ws = self.credentials['workspace_id']
        self.account = next(r['id'] for r in self.bank.snapshot(self.ws)['accounts'] if r['name']=='模拟日常账户')
        self.serial = 0

    def prepare(self, action='purchase', product='SIM-CASH-01', amount=500000, **other):
        self.serial += 1
        body = dict(action=action,account_id=self.account,idempotency_key=f'test-{self.serial}')
        if action=='close_mandate':
            body['mandate_id']=self.bank.mandates(self.ws)[0]['id']
        else:
            body.update(product_id=product,amount_minor=amount)
        body.update(other)
        return self.bank.prepare(self.ws,body)

    def confirm_body(self, operation, strong=True):
        data = {'confirmed':True,'confirmation_digest':operation['confirmation_digest']}
        if strong and operation['permission']=='red':
            data['verification_token']=self.bank.verify(self.ws,operation['id'],{'verification_code':self.credentials['human_verification_code']})['verification_token']
        return data

    def execute(self, operation):
        return self.bank.confirm(self.ws,operation['id'],self.confirm_body(operation))

    def assert_code(self, code, call):
        with self.assertRaises(BankError) as raised:
            call()
        self.assertEqual(raised.exception.code,code)

    def change_profile(self, risk=3, horizon=90, reserve=1000000):
        return self.bank.profile(self.ws,dict(risk_level=risk,horizon_days=horizon,reserve_minor=reserve,confirmed=True))

    def test_seed_balance_reconciles_to_ledger_and_is_persistent(self):
        snapshot = self.bank.snapshot(self.ws)
        self.assertEqual(snapshot['total_cash_minor'],2439000)
        self.assertEqual(snapshot['holding_principal_minor'],800000)
        self.assertEqual(len(self.bank.transactions(self.ws)),101)
        for account in snapshot['accounts']:
            movements = sum(r['delta_minor'] for r in self.bank.transactions(self.ws) if r['account_id']==account['id'])
            self.assertEqual(account['balance_minor'],account['opening_balance_minor']+movements)
        reopened = SimulatedBank(self.path)
        self.assertEqual(reopened.snapshot(self.ws)['total_cash_minor'],2439000)
        self.assertEqual(reopened.authenticate(self.credentials['agent_token']),self.ws)

    def test_monthly_categories_and_investment_are_separate(self):
        report = self.bank.analysis(self.ws,'2026-09')
        self.assertEqual(report['transaction_count'],29)
        self.assertEqual(report['totals_minor']['income'],650000)
        self.assertEqual(report['totals_minor']['refund'],4000)
        self.assertEqual(sum(report['category_expense_minor'].values()),report['totals_minor']['expense'])
        self.execute(self.prepare())
        october = self.bank.analysis(self.ws,'2026-10')
        self.assertEqual(october['totals_minor']['investment_purchase'],500000)
        self.assertEqual(october['totals_minor']['expense'],0)
        self.assertEqual(october['cash_delta_minor'],-500000)

    def test_anomalies_are_grounded_in_prior_records(self):
        findings = self.bank.anomalies(self.ws)
        self.assertEqual(len(findings['duplicates']),1)
        self.assertEqual(len(findings['high_amounts']),1)
        self.assertEqual(findings['high_amounts'][0]['baseline_count'],8)
        ids = {r['id'] for r in self.bank.transactions(self.ws)}
        self.assertTrue(all(set(f['transaction_ids']) <= ids for f in findings['duplicates']+findings['high_amounts']))

    def test_periodic_charges_exclude_frequent_restaurant(self):
        candidates = self.bank.periodic_charges(self.ws)['candidates']
        self.assertEqual(len([r for r in candidates if r['mandate_id']]),3)
        self.assertFalse(any(r['merchant']=='模拟日常餐饮' for r in candidates))
        video = next(r for r in candidates if r['merchant']=='模拟视频会员')
        self.assertEqual(video['expected_next_date'],'2026-10-05')
        self.assertEqual(len(video['transaction_ids']),4)
        self.assertTrue(any(r['classification']=='固定生活支出' for r in candidates))

    def test_periodic_candidate_mark_is_local_persistent_and_audited(self):
        candidate=next(c for c in self.bank.periodic_charges(self.ws)['candidates'] if c['merchant']=='模拟视频会员')
        before=self.bank.snapshot(self.ws)
        mandates_before=self.bank.mandates(self.ws)
        marked=self.bank.mark_periodic_candidate(self.ws,{'candidate_id':candidate['candidate_id'],'decision':'confirmed'})
        self.assertIn('仅本地标记',marked['status'])
        self.assertIn('不会修改银行代扣',marked['notice'])
        after=self.bank.snapshot(self.ws)
        self.assertEqual(before['total_cash_minor'],after['total_cash_minor'])
        self.assertEqual(before['version'],after['version'])
        self.assertEqual(mandates_before,self.bank.mandates(self.ws))
        reopened=SimulatedBank(self.path)
        persisted=next(c for c in reopened.periodic_charges(self.ws)['candidates'] if c['candidate_id']==candidate['candidate_id'])
        self.assertEqual(persisted['user_decision'],'confirmed')
        self.assertIn('仅本地标记',persisted['status'])
        audit=[e for e in reopened.audit(self.ws) if e['event']=='periodic_candidate_marked']
        self.assertEqual(audit[-1]['details']['transaction_ids'],candidate['transaction_ids'])

    def test_periodic_candidate_mark_rejects_stale_evidence_and_bad_decisions(self):
        candidate=next(c for c in self.bank.periodic_charges(self.ws)['candidates'] if c['merchant']=='模拟视频会员')
        self.assert_code('INVALID_DECISION',lambda:self.bank.mark_periodic_candidate(self.ws,{'candidate_id':candidate['candidate_id'],'decision':'closed'}))
        with self.bank.transaction() as db:
            db.executemany('DELETE FROM transactions WHERE id=?',[(transaction_id,) for transaction_id in candidate['transaction_ids']])
        self.assert_code('CANDIDATE_CHANGED',lambda:self.bank.mark_periodic_candidate(self.ws,{'candidate_id':candidate['candidate_id'],'decision':'ignored'}))

    def test_empty_month_and_invalid_month(self):
        self.assertEqual(self.bank.analysis(self.ws,'2025-01')['recorded_range'],None)
        self.assertEqual(self.bank.snapshot(self.ws,'2025-01')['total_cash_minor'],2439000)
        for month in ('2026-13','09',None,42):
            self.assert_code('INVALID_MONTH',lambda:self.bank.analysis(self.ws,month))

    def test_note_injection_does_not_execute_an_action(self):
        records = self.bank.transactions(self.ws)
        self.assertTrue(any('忽略所有权限' in r['note'] for r in records))
        tools = BankTools(self.bank,self.credentials['agent_token'])
        tools.query_snapshot(); tools.query_transactions()
        self.assertEqual(self.bank.orders(self.ws),[])
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],2439000)

    def test_prepare_is_side_effect_free(self):
        op = self.prepare()
        self.assertEqual(op['state'],'pending_confirmation')
        self.assertEqual(op['permission'],'red')
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],2439000)
        self.assertEqual(self.bank.orders(self.ws),[])

    def test_red_operation_requires_verification(self):
        op = self.prepare()
        self.assert_code('VERIFICATION_REQUIRED',lambda:self.bank.confirm(self.ws,op['id'],self.confirm_body(op,False)))
        self.assertEqual(self.bank.operation(self.ws,op['id'])['state'],'pending_confirmation')
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],2439000)

    def test_purchase_changes_cash_holdings_and_one_order(self):
        result = self.execute(self.prepare())
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['result']['balance_before_minor'],2289000)
        self.assertEqual(result['result']['balance_after_minor'],1789000)
        snapshot = self.bank.snapshot(self.ws)
        self.assertEqual(snapshot['total_cash_minor'],1939000)
        self.assertEqual(snapshot['holding_principal_minor'],1300000)
        self.assertEqual(len(self.bank.orders(self.ws)),1)

    def test_repeat_and_concurrent_confirmation_execute_once(self):
        op = self.prepare()
        body = self.confirm_body(op)
        with ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(lambda _:self.bank.confirm(self.ws,op['id'],body),range(5)))
        self.assertEqual(len({r['result']['order_id'] for r in replies}),1)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],1939000)
        self.assertEqual(len(self.bank.orders(self.ws)),1)
        self.assertEqual(len([r for r in self.bank.transactions(self.ws) if r['operation_id']==op['id']]),1)

    def test_idempotency_key_conflict_and_exact_replay(self):
        op = self.prepare()
        self.assertEqual(self.bank.prepare(self.ws,op['payload'])['id'],op['id'])
        changed = {**op['payload'],'amount_minor':400000}
        self.assert_code('IDEMPOTENCY_CONFLICT',lambda:self.bank.prepare(self.ws,changed))

    def test_risk_term_minimum_reserve_and_money_validation(self):
        self.assert_code('UNSUITABLE_PRODUCT',lambda:self.prepare(product='SIM-EQUITY-04'))
        self.change_profile(risk=2,horizon=10)
        self.assert_code('UNSUITABLE_PRODUCT',lambda:self.prepare(product='SIM-BOND-90'))
        self.assert_code('BELOW_MINIMUM',lambda:self.prepare(amount=99))
        self.assert_code('INSUFFICIENT_AVAILABLE',lambda:self.prepare(amount=2000000))
        for amount in (True,100.0,'100',0,-100,100000001):
            self.assert_code('INVALID_INTEGER',lambda:self.prepare(amount=amount))

    def test_unsupported_action_and_client_permission_override(self):
        self.assert_code('ACTION_UNAVAILABLE',lambda:self.prepare(action='real_bank_transfer'))
        self.assert_code('INVALID_FIELDS',lambda:self.prepare(permission='green'))

    def test_changed_profile_invalidates_old_confirmation(self):
        op = self.prepare()
        body = self.confirm_body(op)
        self.change_profile()
        self.assert_code('STALE_CONFIRMATION',lambda:self.bank.confirm(self.ws,op['id'],body))
        self.assertEqual(self.bank.operation(self.ws,op['id'])['state'],'invalidated')
        self.assertEqual(self.bank.orders(self.ws),[])

    def test_changed_balance_invalidates_second_operation(self):
        op1,op2 = self.prepare(),self.prepare(amount=300000)
        body2 = self.confirm_body(op2)
        self.execute(op1)
        self.assert_code('STALE_CONFIRMATION',lambda:self.bank.confirm(self.ws,op2['id'],body2))
        self.assertEqual(len(self.bank.orders(self.ws)),1)

    def test_confirmation_digest_tampering_and_extra_amount(self):
        op = self.prepare()
        self.assert_code('CONFIRMATION_MISMATCH',lambda:self.bank.confirm(self.ws,op['id'],dict(confirmed=True,confirmation_digest='changed')))
        body = self.confirm_body(op)
        self.assert_code('INVALID_FIELDS',lambda:self.bank.confirm(self.ws,op['id'],{**body,'amount_minor':1}))
        self.assertEqual(self.bank.orders(self.ws),[])

    def test_operation_and_verification_expiry(self):
        op = self.prepare()
        body = self.confirm_body(op)
        self.now[0]+=121
        self.assert_code('VERIFICATION_REQUIRED',lambda:self.bank.confirm(self.ws,op['id'],body))
        self.now[0]+=180
        self.assert_code('EXPIRED',lambda:self.bank.confirm(self.ws,op['id'],body))
        self.assertEqual(self.bank.operation(self.ws,op['id'])['state'],'expired')

    def test_verification_token_is_bound_to_operation(self):
        a,b = self.prepare(),self.prepare(amount=300000)
        first = self.confirm_body(a)
        forged = dict(confirmed=True,confirmation_digest=b['confirmation_digest'],verification_token=first['verification_token'])
        self.assert_code('VERIFICATION_REQUIRED',lambda:self.bank.confirm(self.ws,b['id'],forged))

    def test_failed_verification_locks_then_recovers(self):
        op = self.prepare()
        pin = self.credentials['human_verification_code']
        wrong = f'{(int(pin)+1)%1000000:06d}'
        for _ in range(3):
            self.assert_code('VERIFICATION_FAILED',lambda:self.bank.verify(self.ws,op['id'],{'verification_code':wrong}))
        self.assert_code('VERIFICATION_LOCKED',lambda:self.bank.verify(self.ws,op['id'],{'verification_code':pin}))
        self.now[0]+=61
        self.assertEqual(self.execute(op)['state'],'succeeded')

    def test_cross_workspace_isolation(self):
        second = self.bank.create_workspace()['workspace_id']
        op = self.prepare()
        self.assert_code('NOT_FOUND',lambda:self.bank.operation(second,op['id']))
        self.assert_code('NOT_FOUND',lambda:self.bank.prepare(second,op['payload']))
        self.execute(op)
        self.assertEqual(self.bank.snapshot(second)['total_cash_minor'],2439000)

    def test_close_mandate_does_not_cancel_merchant_contract(self):
        op = self.prepare(action='close_mandate')
        self.assertEqual(op['permission'],'yellow')
        result = self.bank.confirm(self.ws,op['id'],self.confirm_body(op,False))
        self.assertEqual(result['result']['status'],'closed')
        self.assertEqual(result['result']['merchant_contract_status'],'unchanged')
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],2439000)
        self.assertEqual(len(self.bank.transactions(self.ws)),101)

    def test_cancel_does_not_execute_and_is_repeatable(self):
        op = self.prepare()
        self.assertEqual(self.bank.cancel(self.ws,op['id'])['state'],'cancelled')
        self.assertEqual(self.bank.cancel(self.ws,op['id'])['state'],'cancelled')
        self.assert_code('INVALID_STATE',lambda:self.bank.confirm(self.ws,op['id'],self.confirm_body(op,False)))
        self.assertEqual(self.bank.orders(self.ws),[])

    def test_locked_holding_cannot_be_redeemed(self):
        self.assert_code('HOLDING_LOCKED',lambda:self.prepare(action='redeem',product='SIM-BOND-90',amount=100000))
        self.assert_code('INSUFFICIENT_HOLDING',lambda:self.prepare(action='redeem',amount=600000))

    def test_instant_redemption_changes_cash(self):
        result = self.execute(self.prepare(action='redeem',amount=100000))
        self.assertEqual(result['state'],'succeeded')
        snapshot = self.bank.snapshot(self.ws)
        self.assertEqual(snapshot['total_cash_minor'],2539000)
        self.assertEqual(snapshot['holding_principal_minor'],700000)

    def test_delayed_redemption_preserves_wealth_and_settles_once(self):
        self.change_profile()
        self.execute(self.prepare(product='SIM-FLEX-03',amount=100000))
        before = self.bank.snapshot(self.ws)
        result = self.execute(self.prepare(action='redeem',product='SIM-FLEX-03',amount=100000))
        self.assertEqual(result['state'],'pending_settlement')
        pending = self.bank.snapshot(self.ws)
        self.assertEqual(pending['total_cash_minor'],before['total_cash_minor'])
        self.assertEqual(pending['pending_redemption_minor'],100000)
        self.assertEqual(pending['total_cash_minor']+pending['holding_principal_minor']+pending['pending_redemption_minor'],3239000)
        self.assertEqual(self.bank.advance_days(self.ws,1)['settled_orders'],0)
        self.assertEqual(self.bank.advance_days(self.ws,1)['settled_orders'],1)
        self.assertEqual(self.bank.advance_days(self.ws,1)['settled_orders'],0)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],2439000)
        self.assertEqual(self.bank.operation(self.ws,result['id'])['state'],'settled')

    def test_sql_failure_rolls_back_every_business_change(self):
        op = self.prepare()
        body = self.confirm_body(op)
        with self.bank.transaction() as db:
            db.execute("CREATE TRIGGER fail_order BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bank.confirm(self.ws,op['id'],body)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],2439000)
        self.assertEqual(self.bank.snapshot(self.ws)['holding_principal_minor'],800000)
        self.assertEqual(self.bank.operation(self.ws,op['id'])['state'],'pending_confirmation')
        self.assertEqual(len(self.bank.transactions(self.ws)),101)
        self.assertFalse(any(r['event']=='operation_executed' for r in self.bank.audit(self.ws)))
        with self.bank.transaction() as db:
            db.execute('DROP TRIGGER fail_order')
        self.assertEqual(self.bank.confirm(self.ws,op['id'],body)['state'],'succeeded')

    def test_audit_does_not_contain_credentials(self):
        self.execute(self.prepare())
        text = json.dumps(self.bank.audit(self.ws))
        for secret in (self.credentials['session_token'],self.credentials['agent_token'],self.credentials['human_verification_code']):
            self.assertNotIn('"'+secret+'"',text)

    def test_agent_tools_require_scoped_credentials(self):
        self.assert_code('AGENT_TOKEN_REQUIRED',lambda:BankTools(self.bank,self.credentials['session_token']))
        tools = BankTools(self.bank,self.credentials['agent_token'])
        self.assertFalse(hasattr(tools,'verify'))
        self.assertFalse(hasattr(tools,'confirm'))
        self.assertEqual(len(tools.query_holdings()),2)

    def test_customer_archive_covers_all_scenes_and_separates_sources(self):
        archive = self.bank.customer_archive(self.ws)
        self.assertTrue(archive['integrity']['ok'])
        self.assertEqual(len(archive['coverage']['scenes']),6)
        self.assertEqual(len(archive['coverage']['counts']),len(SPECS))
        for scene,tables in archive['coverage']['scenes'].items():
            self.assertTrue(all(archive['records'][t] for t in tables),scene)
        self.assertEqual(archive['records']['collateral'],[])
        self.assertEqual(archive['financial_summary']['liabilities_minor'],800000)
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reference_examples').fetchone()[0],7)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_archive_contains_no_authentication_material(self):
        data = self.bank.customer_archive(self.ws)
        text = json.dumps(data)
        for secret in (self.credentials['session_token'],self.credentials['agent_token'],self.credentials['human_verification_code']):
            self.assertNotIn('"'+secret+'"',text)
        for forbidden in ['pin_hash','token_hash','agent_token_hash','verification_hash','cvv','private_key']:
            self.assertNotIn('"'+forbidden+'"',text.lower())

    def test_annual_report_has_full_2025_and_partial_2026(self):
        report = self.bank.annual_analysis(self.ws,2025)
        self.assertTrue(report['complete_calendar_year'])
        self.assertEqual(report['archived_count'],96)
        self.assertEqual(report['totals_minor']['income'],7800000)
        self.assertEqual(report['cash_delta_minor'],837600)
        current = self.bank.annual_analysis(self.ws,2026)
        self.assertFalse(current['complete_calendar_year'])
        self.assertEqual(len(current['covered_months']),9)
        self.assertEqual(current['live_count'],101)
        self.assert_code('INVALID_YEAR',lambda:self.bank.annual_analysis(self.ws,True))
        empty = self.bank.annual_analysis(self.ws,2028)
        self.assertEqual(empty['covered_months'],[])
        self.assertFalse(empty['complete_calendar_year'])

    def test_archive_rows_and_foreign_keys_are_workspace_scoped(self):
        other = self.bank.create_workspace()['workspace_id']
        a,b = self.bank.customer_archive(self.ws),self.bank.customer_archive(other)
        self.assertNotEqual(a['records']['customers'][0]['id'],b['records']['customers'][0]['id'])
        for rows in a['records'].values():
            for r in rows:
                if 'workspace_id' in r: self.assertEqual(r['workspace_id'],self.ws)
        with self.assertRaises(sqlite3.IntegrityError),self.bank.transaction() as db:
            db.execute('UPDATE cards SET account_id=? WHERE workspace_id=?',
                       (b['accounts'][0]['id'],self.ws))

    def test_archive_reconciliation_detects_corrupted_statement(self):
        with self.bank.transaction() as db:
            db.execute('UPDATE statement_periods SET closing_minor=closing_minor+1 WHERE workspace_id=?',(self.ws,))
        self.assertFalse(self.bank.customer_archive(self.ws)['integrity']['ok'])

    def test_new_purchase_does_not_lock_older_matured_lot(self):
        self.change_profile(horizon=180)
        self.execute(self.prepare(product='SIM-BOND-30',amount=100000))
        self.bank.advance_days(self.ws,31)
        self.execute(self.prepare(product='SIM-BOND-30',amount=100000))
        op = self.prepare(action='redeem',product='SIM-BOND-30',amount=100000)
        self.execute(op)
        self.assert_code('HOLDING_LOCKED',lambda:self.prepare(action='redeem',product='SIM-BOND-30',amount=100000))
        self.assertTrue(self.bank.customer_archive(self.ws)['integrity']['ok'])
        with closing(self.bank.connect()) as db:
            rows = db.execute("SELECT acquired_on,remaining_minor FROM investment_lots WHERE workspace_id=? AND product_id='SIM-BOND-30' ORDER BY acquired_on",(self.ws,)).fetchall()
            self.assertEqual([r['remaining_minor'] for r in rows],[0,100000])
            self.assertEqual(db.execute('SELECT sum(amount_minor) FROM lot_redemptions WHERE workspace_id=?',(self.ws,)).fetchone()[0],100000)

    def test_fifo_partial_redemption_and_lot_rollbacks(self):
        self.execute(self.prepare(amount=100000))
        self.execute(self.prepare(action='redeem',amount=550000))
        with closing(self.bank.connect()) as db:
            original = db.execute("SELECT remaining_minor FROM investment_lots WHERE workspace_id=? AND origin='fixture' AND product_id='SIM-CASH-01'",(self.ws,)).fetchone()[0]
            self.assertEqual(original,0)
        self.assertTrue(self.bank.customer_archive(self.ws)['integrity']['ok'])
        op = self.prepare(action='redeem',amount=10000)
        body = self.confirm_body(op)
        before = self.bank.customer_archive(self.ws)['records']['investment_lots']
        with self.bank.transaction() as db:
            db.execute("CREATE TRIGGER fail_lot_order BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.bank.confirm(self.ws,op['id'],body)
        self.assertEqual(self.bank.customer_archive(self.ws)['records']['investment_lots'],before)

    def test_updated_profile_has_one_current_assessment(self):
        self.change_profile(risk=3,horizon=180)
        rows = self.bank.customer_archive(self.ws)['records']['risk_assessments']
        current = [r for r in rows if r['status']=='current']
        self.assertEqual(len(current),1)
        self.assertEqual(current[0]['risk_level'],3)

    def test_archive_reads_do_not_execute_and_unknown_card_action_stays_unavailable(self):
        before = self.bank.snapshot(self.ws)['total_cash_minor']
        self.bank.customer_archive(self.ws)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before)
        for action in ['issue_card','book_cake']:
            self.assert_code('ACTION_UNAVAILABLE',lambda:self.prepare(action=action))
        self.assert_code('INVALID_FIELDS',lambda:self.prepare(action='transfer'))

    def test_confirmed_transfer_updates_ledger_once_and_enforces_daily_limit(self):
        archive=self.bank.customer_archive(self.ws)['records']
        beneficiary=next(b for b in archive['beneficiaries'] if b['alias']=='妈妈')
        before=self.bank.snapshot(self.ws)['total_cash_minor']
        op=self.bank.prepare(self.ws,{'action':'transfer','account_id':self.account,'beneficiary_id':beneficiary['id'],
            'amount_minor':50000,'reference':'','idempotency_key':'transfer-once'})
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before)
        body=self.confirm_body(op)
        receipt=self.bank.confirm(self.ws,op['id'],body)
        replay=self.bank.confirm(self.ws,op['id'],body)
        self.assertEqual(receipt['result']['payment_id'],replay['result']['payment_id'])
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before-50000)
        rows=[r for r in self.bank.transactions(self.ws) if r['operation_id']==op['id']]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['kind'],'transfer')
        payment=next(p for p in self.bank.customer_archive(self.ws)['records']['payment_instructions']
                     if p['id']==receipt['result']['payment_id'])
        self.assertEqual(payment['status'],'succeeded')

    def test_scheduled_transfer_executes_on_simulation_date_once(self):
        archive=self.bank.customer_archive(self.ws)['records']
        beneficiary=next(b for b in archive['beneficiaries'] if b['alias']=='妈妈')
        due='2026-10-04'
        op=self.bank.prepare(self.ws,{'action':'schedule_transfer','account_id':self.account,
            'beneficiary_id':beneficiary['id'],'amount_minor':25000,'frequency':'once','next_due_on':due,
            'idempotency_key':'schedule-once'})
        confirmed=self.execute(op); schedule_id=confirmed['result']['scheduled_payment_id']
        before=self.bank.snapshot(self.ws)['total_cash_minor']
        result=self.bank.advance_days(self.ws,1)
        self.assertEqual(result['scheduled_transfers'],1);self.assertEqual(result['failed_transfers'],0)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before-25000)
        schedule=next(x for x in self.bank.customer_archive(self.ws)['records']['scheduled_payments'] if x['id']==schedule_id)
        self.assertEqual(schedule['status'],'completed')
        self.assertEqual(self.bank.advance_days(self.ws,1)['scheduled_transfers'],0)

    def test_split_request_and_receipt_are_local_and_reconciled(self):
        records=self.bank.customer_archive(self.ws)['records']
        split=next(s for s in records['split_bills'] if s['status']=='draft')
        request=self.bank.prepare(self.ws,{'action':'request_split','account_id':self.account,
            'split_id':split['id'],'idempotency_key':'split-request'})
        self.execute(request)
        people=[p for p in self.bank.customer_archive(self.ws)['records']['split_participants'] if p['split_id']==split['id']]
        self.assertTrue(all(p['status']=='requested' for p in people))
        before=self.bank.snapshot(self.ws)['total_cash_minor']
        collect=self.bank.prepare(self.ws,{'action':'collect_split_payment','account_id':self.account,
            'split_id':split['id'],'participant_id':people[0]['id'],'idempotency_key':'split-receipt'})
        receipt=self.execute(collect)
        self.assertEqual(receipt['result']['amount_minor'],15000)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before+15000)
        self.assertEqual(self.bank.analysis(self.ws,'2026-10')['totals_minor']['transfer'],15000)

    def test_subscription_cancellation_does_not_close_bank_mandate(self):
        records=self.bank.customer_archive(self.ws)['records']
        subscription=next(s for s in records['subscriptions'] if s['merchant_status']=='active')
        op=self.bank.prepare(self.ws,{'action':'cancel_subscription','account_id':subscription['account_id'],
            'subscription_id':subscription['id'],'idempotency_key':'cancel-subscription'})
        receipt=self.execute(op)
        self.assertEqual(receipt['result']['mandate_status'],'unchanged')
        after=self.bank.customer_archive(self.ws)['records']
        self.assertEqual(next(s for s in after['subscriptions'] if s['id']==subscription['id'])['merchant_status'],'cancelled')
        self.assertEqual(next(m for m in after['mandates'] if m['id']==subscription['mandate_id'])['status'],'active')

    def test_fund_hold_then_service_booking_debits_once_and_completes_task(self):
        records=self.bank.customer_archive(self.ws)['records']
        task=next(t for t in records['orchestration_tasks'] if t['status']=='draft')
        quotes=[q for q in records['service_quotes'] if q['status']=='illustrative']
        total=sum(q['price_minor'] for q in quotes)
        before=self.bank.snapshot(self.ws)['total_cash_minor']
        hold=self.bank.prepare(self.ws,{'action':'reserve_funds','account_id':self.account,'task_id':task['id'],
            'amount_minor':total,'expires_on':'2026-10-17','idempotency_key':'reserve-birthday'})
        hold_receipt=self.execute(hold); hold_id=hold_receipt['result']['hold_id']
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before)
        book=self.bank.prepare(self.ws,{'action':'book_services','account_id':self.account,'task_id':task['id'],
            'hold_id':hold_id,'quote_ids':[q['id'] for q in quotes],'idempotency_key':'book-birthday'})
        booked=self.execute(book)
        self.assertEqual(booked['result']['amount_minor'],total)
        self.assertEqual(self.bank.snapshot(self.ws)['total_cash_minor'],before-total)
        after=self.bank.customer_archive(self.ws)['records']
        self.assertEqual(next(t for t in after['orchestration_tasks'] if t['id']==task['id'])['status'],'completed')
        self.assertEqual(len([r for r in after['service_reservations'] if r['task_id']==task['id'] and r['status']=='confirmed']),len(quotes))

    def test_card_limit_and_application_reviews_run_only_after_simulation_date_moves(self):
        records=self.bank.customer_archive(self.ws)['records']
        credit=next(c for c in records['cards'] if c['type']=='credit' and c['status']=='active')
        limit=self.bank.prepare(self.ws,{'action':'request_card_limit','account_id':credit['account_id'],
            'card_id':credit['id'],'requested_limit_minor':1500000,'idempotency_key':'limit-review'})
        limit_receipt=self.execute(limit)
        self.assertEqual(next(c for c in self.bank.customer_archive(self.ws)['records']['cards'] if c['id']==credit['id'])['credit_limit_minor'],1000000)
        review=self.bank.advance_days(self.ws,1)
        self.assertEqual(review['reviewed_card_limits'],1)
        updated=next(c for c in self.bank.customer_archive(self.ws)['records']['cards'] if c['id']==credit['id'])
        self.assertEqual(updated['credit_limit_minor'],1500000)
        self.assertEqual(next(r for r in self.bank.customer_archive(self.ws)['records']['card_limit_requests']
                              if r['id']==limit_receipt['result']['request_id'])['status'],'approved')
        application=self.bank.prepare(self.ws,{'action':'apply_card','account_id':self.account,
            'requested_type':'debit','requested_limit_minor':0,'idempotency_key':'debit-card-review'})
        receipt=self.execute(application)
        self.assertEqual(receipt['result']['status'],'submitted')
        self.assertEqual(self.bank.advance_days(self.ws,1)['reviewed_card_applications'],1)
        records=self.bank.customer_archive(self.ws)['records']
        issued=next(a for a in records['card_applications'] if a['id']==receipt['result']['application_id'])
        self.assertEqual(issued['status'],'issued')
        self.assertTrue(any(c['status']=='active' and c['type']=='debit' and c['id']!=credit['id'] for c in records['cards']))

    def test_typed_money_and_status_reject_bad_storage_values(self):
        for sql in ['UPDATE cards SET outstanding_minor=1.5','UPDATE cards SET outstanding_minor=-1',"UPDATE cards SET status='unknown'"]:
            with self.assertRaises(sqlite3.IntegrityError),self.bank.transaction() as db:
                db.execute(sql+' WHERE workspace_id=?',(self.ws,))

    def legacy_copy(self):
        original = {'metadata','workspaces','profiles','accounts','products','transactions','mandates','holdings','operations','orders','audit'}
        with closing(self.bank.connect()) as db:
            data = {t:[tuple(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in original-{'metadata'}}
            db.execute('PRAGMA foreign_keys=OFF')
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for name in names:
                if name not in original and not name.startswith('sqlite_'): db.execute('DROP TABLE '+name)
            for name in ['order_enum_insert','order_enum_update']: db.execute('DROP TRIGGER IF EXISTS '+name)
            db.execute("DELETE FROM metadata WHERE key='schema_version'")
            db.commit()
        return data

    def test_inplace_v1_migration_preserves_all_old_business_rows(self):
        self.execute(self.prepare(amount=100000))
        self.execute(self.prepare(action='close_mandate'))
        before = self.legacy_copy()
        migrated = SimulatedBank(self.path)
        with closing(migrated.connect()) as db:
            after = {t:[tuple(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in before}
            self.assertEqual(before,after)
        self.assertTrue(migrated.customer_archive(self.ws)['integrity']['ok'])
        self.assertTrue(list(self.path.parent.glob('*.before-v5-*.sqlite3')))
        SimulatedBank(self.path)
        self.assertEqual(len(list(self.path.parent.glob('*.before-v5-*.sqlite3'))),1)

    def test_failed_migration_rolls_back_and_can_retry(self):
        before = self.legacy_copy()
        with patch('simulated_bank.customer_archive.populate',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError): SimulatedBank(self.path)
        with closing(self.bank.connect()) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='customers'").fetchone())
            for t,rows in before.items(): self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY rowid')],rows)
        retried = SimulatedBank(self.path)
        self.assertTrue(retried.customer_archive(self.ws)['integrity']['ok'])

    def test_v2_migration_disables_lost_card_controls_without_rewriting_lots(self):
        with closing(self.bank.connect()) as db:
            lots_before=db.execute('SELECT count(*) FROM investment_lots WHERE workspace_id=?',(self.ws,)).fetchone()[0]
        with self.bank.transaction() as db:
            db.execute('UPDATE schema_migrations SET version=2')
            db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
            db.execute("UPDATE card_controls SET enabled=1 WHERE card_id=?",(self.ws[:12]+'-lost-card-online',))
        migrated=SimulatedBank(self.path)
        archive=migrated.customer_archive(self.ws)
        self.assertTrue(archive['integrity']['ok'])
        with closing(migrated.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM investment_lots WHERE workspace_id=?',(self.ws,)).fetchone()[0],lots_before)
            self.assertEqual(db.execute('SELECT count(*) FROM card_controls WHERE workspace_id=? AND card_id=? AND enabled<>0',
                                        (self.ws,self.ws[:12]+'-lost-card-online')).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT dataset_version FROM workspace_datasets WHERE workspace_id=?',(self.ws,)).fetchone()[0],
                             'customer-bank-2026-10-06-v5')

    def test_changed_new_workspace_fixture_does_not_require_database_reset(self):
        fixture = json.loads((Path(__file__).parents[1]/'simulated_bank/fixtures/demo.json').read_text())
        fixture['description']='新版样本说明，仅影响新工作区'
        path = self.path.parent/'new-fixture.json'; path.write_text(json.dumps(fixture))
        upgraded = SimulatedBank(self.path,fixture=path)
        self.assertEqual(upgraded.snapshot(self.ws)['total_cash_minor'],2439000)
        new = upgraded.create_workspace()['workspace_id']
        with closing(upgraded.connect()) as db:
            old_hash = db.execute('SELECT core_seed_hash FROM workspace_datasets WHERE workspace_id=?',(self.ws,)).fetchone()[0]
            new_hash = db.execute('SELECT core_seed_hash FROM workspace_datasets WHERE workspace_id=?',(new,)).fetchone()[0]
            self.assertNotEqual(old_hash,new_hash)

    def test_unsupported_future_schema_cannot_be_downgraded(self):
        with self.bank.transaction() as db: db.execute("INSERT INTO schema_migrations VALUES(99,0,'future')")
        with self.assertRaises(RuntimeError): SimulatedBank(self.path)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.bank = SimulatedBank(Path(cls.temp.name)/'http.sqlite3')
        cls.server = create_server(cls.bank,0)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2); cls.temp.cleanup()

    def request(self,path,body=None,token=None,headers=None,raw=None):
        h = {'Content-Type':'application/json',**(headers or {})}
        if token:
            h['Authorization']='Bearer '+token
        payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base+path,data=payload,headers=h)
        try:
            response = urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status,json.loads(response.read()),dict(response.headers)

    def create(self):
        status,data,headers = self.request('/api/sim-bank/workspaces',{})
        self.assertEqual(status,201)
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        return data

    def test_health_contract_and_no_cache(self):
        self.assertEqual(self.request('/health')[1]['status'],'ok')
        status,contract,headers = self.request('/openapi.json')
        self.assertEqual(status,200)
        self.assertEqual(sum(len(v) for v in contract['paths'].values()),26)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',headers)

    def test_archive_and_annual_http_contract_and_invalid_query(self):
        c = self.create()
        for path in ['customer-archive','data-contract','annual-analysis?year=2025']:
            status,data,_ = self.request('/api/sim-bank/'+path,token=c['agent_token'])
            self.assertEqual(status,200)
        status,error,_ = self.request('/api/sim-bank/annual-analysis?year=bad',token=c['agent_token'])
        self.assertEqual((status,error['error']['code']),(400,'INVALID_YEAR'))
        status,_,_ = self.request('/api/sim-bank/customer-archive?workspace_id=other',token=c['agent_token'])
        self.assertEqual(status,400)

    def test_agent_can_prepare_but_cannot_self_confirm_or_verify(self):
        c = self.create()
        snap = self.request('/api/sim-bank/snapshot',token=c['agent_token'])[1]
        account = next(r['id'] for r in snap['accounts'] if r['name']=='模拟日常账户')
        status,op,_ = self.request('/api/sim-bank/operations/prepare',dict(action='purchase',account_id=account,product_id='SIM-CASH-01',amount_minor=500000,idempotency_key='http-test'),c['agent_token'])
        self.assertEqual(status,200)
        for action,body in [('confirm',dict(confirmed=True,confirmation_digest=op['confirmation_digest'])),('verify',dict(verification_code=c['human_verification_code']))]:
            status,error,_ = self.request(f'/api/sim-bank/operations/{op["id"]}/{action}',body,c['agent_token'])
            self.assertEqual((status,error['error']['code']),(403,'HUMAN_REQUIRED'))
        _,grant,_ = self.request(f'/api/sim-bank/operations/{op["id"]}/verify',dict(verification_code=c['human_verification_code']),c['session_token'])
        body = dict(confirmed=True,confirmation_digest=op['confirmation_digest'],verification_token=grant['verification_token'])
        self.assertEqual(self.request(f'/api/sim-bank/operations/{op["id"]}/confirm',body,c['session_token'])[1]['state'],'succeeded')
        self.assertEqual(self.request(f'/api/sim-bank/operations/{op["id"]}/confirm',body,c['session_token'])[1]['state'],'succeeded')
        self.assertEqual(len(self.request('/api/sim-bank/orders',token=c['agent_token'])[1]),1)

    def test_periodic_mark_requires_human_and_preserves_bank_state(self):
        c=self.create()
        candidate=next(x for x in self.request('/api/sim-bank/periodic-charges',token=c['agent_token'])[1]['candidates']
                       if x['merchant']=='模拟视频会员')
        path='/api/sim-bank/periodic-charges/'+candidate['candidate_id']+'/mark'
        before=self.request('/api/sim-bank/snapshot',token=c['agent_token'])[1]
        status,error,_=self.request(path,{'decision':'confirmed'},c['agent_token'])
        self.assertEqual((status,error['error']['code']),(403,'HUMAN_REQUIRED'))
        status,marked,_=self.request(path,{'decision':'confirmed'},c['session_token'])
        self.assertEqual(status,200)
        self.assertIn('仅本地标记',marked['status'])
        current=self.request('/api/sim-bank/periodic-charges',token=c['agent_token'])[1]
        viewed=next(x for x in current['candidates'] if x['candidate_id']==candidate['candidate_id'])
        self.assertEqual(viewed['user_decision'],'confirmed')
        after=self.request('/api/sim-bank/snapshot',token=c['agent_token'])[1]
        self.assertEqual((after['total_cash_minor'],after['version']),
                         (before['total_cash_minor'],before['version']))

    def test_auth_csrf_host_and_non_json_fail_closed(self):
        self.assertEqual(self.request('/api/sim-bank/snapshot')[0],401)
        self.assertEqual(self.request('/api/sim-bank/workspaces',{},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/health',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/sim-bank/workspaces',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('/api/sim-bank/workspaces',raw=b'{"a":1,"a":2}')[0],400)
        self.assertEqual(self.request('/api/sim-bank/workspaces',raw=b'{"a":NaN}')[0],400)

    def test_scoped_token_profile_and_operation_denial(self):
        c = self.create()
        status,error,_ = self.request('/api/sim-bank/profile',dict(risk_level=5,horizon_days=180,reserve_minor=0,confirmed=True),c['agent_token'])
        self.assertEqual((status,error['error']['code']),(403,'HUMAN_REQUIRED'))
        self.assertEqual(self.request('/api/sim-bank/operations/'+'0'*32,token=c['session_token'])[0],404)


if __name__=='__main__':
    unittest.main()
