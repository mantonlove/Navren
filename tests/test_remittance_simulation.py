"""完整模拟汇款定向测试；只使用临时或明确指定的隔离合成账本。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from finance_gateway import FinanceGateway
from simulated_bank import BankError, SimulatedBank

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime.fromisoformat('2026-10-09T22:00:00+08:00').timestamp()


def sample_form(card,*,mode='realtime',key='sample-remittance',amount=10000):
    return {'account_id':card['account_id'],'card_id':card['id'],
            'recipient_name':'模拟收款人','recipient_account':'06222021000000000123',
            'recipient_bank':'中国工商银行','recipient_branch':'杭州模拟支行','amount_minor':amount,
            'purpose':'生活费','message':'10月模拟生活费','mode':mode,'idempotency_key':key}


def build_isolated_fixture(directory,*,connected=False,clock=None):
    """供总控复用；创建新库，绝不复制现役银行/个人/认证库。"""
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    bank_path=directory/'bank.sqlite3'
    if bank_path.exists():raise ValueError('隔离构造仅接受没有bank.sqlite3的新目录')
    env={'NAVREN_KNOWLEDGE_DB':str(directory/'knowledge.sqlite3'),'NAVREN_FINANCIAL_DB':''}
    with patch.dict(os.environ,env):
        if connected:
            from simulated_bank.connected_assets import migrate_connected_assets
            source=ROOT/'outputs/financial-data/navren-financial-v3.sqlite3'
            gateway=FinanceGateway(bank_path,financial_database=source)
            if clock:gateway.bank.clock=clock
            gateway.demo_accounts.initialize_offline(allow_offline=True)
            migrate_connected_assets(bank_path,source,apply=True,clock=clock or gateway.bank.clock)
            workspace=gateway.demo_accounts.stable_workspace('retired_entrepreneur')
            _,token=gateway.demo_sessions.create('retired_entrepreneur')
            session={'workspace_id':workspace,'session_token':token,'demo_user_id':'retired_entrepreneur'}
        else:
            gateway=FinanceGateway(bank_path)
            if clock:gateway.bank.clock=clock
            session=gateway.bank.create_workspace();workspace=session['workspace_id']
    card=next(c for c in gateway.overview(workspace)['cards'] if c['type']=='debit' and c['status']=='active')
    return gateway,session,sample_form(card)


class RemittanceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'bank.sqlite3';self.now=[NOW]
        self.env=patch.dict(os.environ,{'NAVREN_KNOWLEDGE_DB':str(Path(self.directory.name)/'knowledge.sqlite3'),
                                      'NAVREN_FINANCIAL_DB':''});self.env.start();self.addCleanup(self.env.stop)
        self.g,self.session,self.form=build_isolated_fixture(self.directory.name,clock=lambda:self.now[0])
        self.w=self.session['workspace_id'];self.account=self.form['account_id']

    def counts(self):
        with closing(self.g.bank.connect()) as db:
            return {t:db.execute('SELECT count(*) FROM '+t+' WHERE workspace_id=?',(self.w,)).fetchone()[0]
                    for t in ('transactions','payment_instructions','navren_remittances','scheduled_payments','beneficiaries')}

    def balance(self):return next(a['balance_minor'] for a in self.g.bank.snapshot(self.w)['accounts'] if a['id']==self.account)

    def prepare(self,**changes):return self.g.prepare_remittance(self.w,{**self.form,**changes})['operation']

    def confirm(self,operation,**changes):
        body={'confirmed':True,'confirmation_digest':operation['confirmation_digest'],
              'verification_code':self.session['human_verification_code'],**changes}
        return self.g.confirm(self.w,operation['id'],body)['operation']

    def error(self,code,call):
        with self.assertRaises(BankError) as caught:call()
        self.assertEqual(caught.exception.code,code)

    def test_prepare_preview_is_bound_and_has_no_financial_side_effects(self):
        before=self.counts();cash=self.balance();op=self.prepare()
        self.assertEqual(op['state'],'pending_confirmation');self.assertEqual(self.counts(),before)
        self.assertEqual(self.balance(),cash)
        view=self.g.bank.remittances(self.w,op['id'])
        self.assertEqual(view['recipient_account'],self.form['recipient_account']);self.assertEqual(view['held_minor'],0)
        self.assertEqual(op['payload']['remittance']['message'],self.form['message'])
        self.assertEqual(self.g.bank.remittances(self.w)['remittances'][0]['operation_id'],op['id'])

    def test_legacy_sender_values_are_ignored_and_idempotency_uses_server_identity(self):
        op=self.prepare(sender_name='客户端伪造姓名',sender_phone={'untrusted':'13800000000'})
        form=op['payload']['remittance']
        self.assertEqual(form['sender_name'],'林舟（虚构）')
        self.assertEqual(form['sender_phone'],'SIM-PHONE-****-1001')
        again=self.prepare(sender_name=[],sender_phone='另一条伪造联系电话')
        self.assertEqual(again['id'],op['id']);self.assertEqual(again['confirmation_digest'],op['confirmation_digest'])
        self.assertNotIn('客户端伪造姓名',json.dumps(op,ensure_ascii=False))
        self.assertNotIn('13800000000',json.dumps(op,ensure_ascii=False))
        with closing(self.g.bank.connect()) as db:
            row=db.execute('SELECT payload,payload_hash FROM operations WHERE id=?',(op['id'],)).fetchone()
            from simulated_bank.service import digest,encoded
            self.assertEqual(row['payload_hash'],digest(encoded(op['payload'])))
            self.assertEqual(json.loads(row['payload'])['remittance'],form)

    def test_missing_mobile_is_empty_and_remains_empty_in_persisted_receipt(self):
        with self.g.bank.transaction() as db:
            db.execute("DELETE FROM contacts WHERE workspace_id=? AND kind='mobile'",(self.w,))
        op=self.prepare();self.assertEqual(op['payload']['remittance']['sender_phone'],'')
        self.assertEqual(op['payload']['remittance']['sender_name'],'林舟（虚构）')
        self.confirm(op)
        reopened=FinanceGateway(self.path);reopened.bank.clock=lambda:self.now[0]
        receipt=reopened.bank.remittances(self.w,op['id'])
        self.assertEqual(receipt['sender_name'],'林舟（虚构）');self.assertEqual(receipt['sender_phone'],'')

    def test_completed_prepare_replay_retains_frozen_sender_after_profile_changes(self):
        op=self.prepare();completed=self.confirm(op);before=self.counts();cash=self.balance()
        receipt_before=self.g.bank.remittances(self.w,op['id'])
        with self.g.bank.transaction() as db:
            card=self.g.bank._card(db,self.w,self.form['card_id'])
            db.execute("UPDATE customers SET display_name='新的系统姓名（虚构）' WHERE workspace_id=?",(self.w,))
            db.execute("UPDATE contacts SET masked_value='139****9999' WHERE workspace_id=? AND kind='mobile'",(self.w,))
            self.g.bank._insert_archive_row(db,'contacts',self.w,{'customer_id':card['customer_id'],
                'kind':'mobile','masked_value':'137****2222','verified':1,'preferred':1})
        replay=self.prepare(sender_name='客户端伪造',sender_phone='135****0000')
        self.assertEqual(replay,completed)
        self.assertEqual(self.g.bank.prepare(self.w,op['payload']),completed)
        self.assertEqual(self.g.bank.remittances(self.w,op['id']),receipt_before)
        for changes in ({'amount_minor':self.form['amount_minor']+1},{'recipient_name':'另一收款人'},
                        {'purpose':'另一用途'},{'card_id':'另一付款卡'},{'mode':'ordinary'}):
            with self.subTest(changes=changes):
                self.error('IDEMPOTENCY_CONFLICT',lambda:self.prepare(**changes))
        self.error('REMITTANCE_SENDER_UNAVAILABLE',lambda:self.prepare(idempotency_key='new-after-profile-change'))
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)

    def test_scheduled_prepare_replay_retains_frozen_sender_and_pending_replay_rechecks_profile(self):
        op=self.prepare(mode='ordinary');accepted=self.confirm(op);before=self.counts();cash=self.balance()
        with self.g.bank.transaction() as db:
            db.execute("UPDATE customers SET display_name='新的系统姓名（虚构）' WHERE workspace_id=?",(self.w,))
        self.assertEqual(self.prepare(mode='ordinary'),accepted)
        self.assertEqual(self.g.bank.prepare(self.w,op['payload']),accepted)
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)
        pending=self.prepare(idempotency_key='new-pending-after-profile-change')
        self.assertEqual(pending['payload']['remittance']['sender_name'],'新的系统姓名（虚构）')
        with self.g.bank.transaction() as db:
            db.execute("UPDATE customers SET display_name='再次更新姓名（虚构）' WHERE workspace_id=?",(self.w,))
        self.error('IDEMPOTENCY_CONFLICT',lambda:self.prepare(idempotency_key='new-pending-after-profile-change'))
        self.error('REMITTANCE_SENDER_MISMATCH',lambda:self.confirm(pending))
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)

    def test_verified_preferred_masked_mobile_is_preserved_without_reconstruction(self):
        with self.g.bank.transaction() as db:
            card=self.g.bank._card(db,self.w,self.form['card_id'])
            db.execute("UPDATE contacts SET masked_value='138****4321' WHERE workspace_id=? AND kind='mobile'",
                       (self.w,))
            self.g.bank._insert_archive_row(db,'contacts',self.w,{'customer_id':card['customer_id'],
                'kind':'mobile','masked_value':'139****8888','verified':0,'preferred':1})
            self.g.bank._insert_archive_row(db,'contacts',self.w,{'customer_id':card['customer_id'],
                'kind':'mobile','masked_value':'137****7777','verified':1,'preferred':0})
        op=self.prepare(sender_phone='136****9999')
        self.assertEqual(op['payload']['remittance']['sender_phone'],'138****4321')
        self.confirm(op);receipt=self.g.bank.remittances(self.w,op['id'])
        self.assertEqual(receipt['sender_phone'],'138****4321')
        self.assertNotIn('136****9999',json.dumps(receipt,ensure_ascii=False))

    def test_unverified_and_nonpreferred_mobiles_are_not_used(self):
        with self.g.bank.transaction() as db:
            card=self.g.bank._card(db,self.w,self.form['card_id'])
            db.execute("UPDATE contacts SET verified=0 WHERE workspace_id=? AND kind='mobile'",(self.w,))
            self.g.bank._insert_archive_row(db,'contacts',self.w,{'customer_id':card['customer_id'],
                'kind':'mobile','masked_value':'139****8888','verified':1,'preferred':0})
        self.assertEqual(self.prepare()['payload']['remittance']['sender_phone'],'')

    def test_direct_bank_and_generic_gateway_prepare_cannot_accept_forged_sender(self):
        form={k:v for k,v in self.form.items() if k not in ('account_id','amount_minor','idempotency_key')}
        form.update(sender_name='绕过网关的姓名',sender_phone='客户端掩码****0000')
        core={'action':'transfer','account_id':self.account,'amount_minor':self.form['amount_minor'],
              'idempotency_key':'direct-bank-profile','remittance':form}
        before=self.counts();cash=self.balance()
        op=self.g.bank.prepare(self.w,core)
        self.assertEqual(op['payload']['remittance']['sender_name'],'林舟（虚构）')
        self.assertEqual(op['payload']['remittance']['sender_phone'],'SIM-PHONE-****-1001')
        self.assertEqual(core['remittance']['sender_name'],'绕过网关的姓名')
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)
        core['idempotency_key']='generic-gateway-profile';core['remittance'].pop('sender_phone')
        result=self.g.prepare(self.w,core)['operation']
        self.assertEqual(result['payload']['remittance']['sender_phone'],'SIM-PHONE-****-1001')

    def test_sender_follows_selected_card_customer_in_multi_customer_workspace(self):
        with self.g.bank.transaction() as db:
            original=dict(db.execute('SELECT * FROM customers WHERE workspace_id=?',(self.w,)).fetchone())
            keys=('branch_id','birth_date','nationality','status','opened_on','segment')
            customer=self.g.bank._insert_archive_row(db,'customers',self.w,
                {**{k:original[k] for k in keys},'display_name':'另一位持卡人（虚构）'})
            db.execute('UPDATE cards SET customer_id=? WHERE workspace_id=? AND id=?',
                       (customer,self.w,self.form['card_id']))
            db.execute('UPDATE account_details SET customer_id=? WHERE workspace_id=? AND account_id=?',
                       (customer,self.w,self.account))
            db.execute('UPDATE signatories SET customer_id=? WHERE workspace_id=? AND account_id=?',
                       (customer,self.w,self.account))
            self.g.bank._insert_archive_row(db,'contacts',self.w,{'customer_id':customer,
                'kind':'mobile','masked_value':'139****2222','verified':1,'preferred':1})
        form=self.prepare()['payload']['remittance']
        self.assertEqual(form['sender_name'],'另一位持卡人（虚构）');self.assertEqual(form['sender_phone'],'139****2222')

    def test_mobile_from_other_workspace_is_not_a_fallback(self):
        self.g.bank.create_workspace()
        with self.g.bank.transaction() as db:
            db.execute("DELETE FROM contacts WHERE workspace_id=? AND kind='mobile'",(self.w,))
        self.assertEqual(self.prepare()['payload']['remittance']['sender_phone'],'')

    def test_profile_changes_before_confirmation_cannot_use_old_sender_snapshot(self):
        for field in ('sender_name','sender_phone'):
            with self.subTest(field=field):
                op=self.prepare(idempotency_key='profile-change-'+field);before=self.counts();cash=self.balance()
                with self.g.bank.transaction() as db:
                    if field=='sender_name':
                        db.execute("UPDATE customers SET display_name='更新的系统姓名（虚构）' WHERE workspace_id=?",(self.w,))
                    else:
                        db.execute("UPDATE contacts SET masked_value='139****9999' WHERE workspace_id=? AND kind='mobile'",(self.w,))
                self.error('REMITTANCE_SENDER_MISMATCH',lambda:self.confirm(op))
                self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)
                self.assertEqual(self.g.bank.operation(self.w,op['id'])['state'],'pending_confirmation')

    def test_validation_does_not_allow_arbitrary_masked_sender_or_changed_card_binding(self):
        op=self.prepare();core=json.loads(json.dumps(op['payload']));core['remittance']['sender_phone']='任意掩码****4321'
        with self.g.bank.transaction() as db:
            self.error('REMITTANCE_SENDER_MISMATCH',lambda:self.g.bank._validate_action(db,self.w,core))
        before=self.counts();cash=self.balance()
        with self.g.bank.transaction() as db:
            other=db.execute('SELECT id FROM accounts WHERE workspace_id=? AND id<>? LIMIT 1',(self.w,self.account)).fetchone()['id']
            db.execute('UPDATE cards SET account_id=? WHERE workspace_id=? AND id=?',(other,self.w,self.form['card_id']))
        with self.g.bank.transaction() as db:
            self.error('REMITTANCE_CARD_MISMATCH',lambda:self.g.bank._validate_action(db,self.w,op['payload']))
        self.error('METRIC_VERSION_CONFLICT',lambda:self.confirm(op))
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)

    def test_multiple_preferred_verified_mobiles_require_archive_review(self):
        with self.g.bank.transaction() as db:
            card=self.g.bank._card(db,self.w,self.form['card_id'])
            self.g.bank._insert_archive_row(db,'contacts',self.w,{'customer_id':card['customer_id'],
                'kind':'mobile','masked_value':'139****2222','verified':1,'preferred':1})
        before=self.counts();cash=self.balance()
        self.error('REMITTANCE_SENDER_UNAVAILABLE',lambda:self.prepare())
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(),cash)

    def test_realtime_receipt_balance_payment_ledger_and_refresh_reconcile(self):
        before=self.balance();op=self.prepare();receipt=self.confirm(op);result=receipt['result']
        self.assertEqual(receipt['state'],'succeeded');self.assertEqual(self.balance(),before-self.form['amount_minor'])
        with closing(self.g.bank.connect()) as db:
            ledger=dict(db.execute('SELECT * FROM transactions WHERE id=?',(result['transaction_id'],)).fetchone())
            payment=dict(db.execute('SELECT * FROM payment_instructions WHERE workspace_id=? AND id=?',(self.w,result['payment_id'])).fetchone())
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
        self.assertEqual(ledger['operation_id'],op['id']);self.assertEqual(ledger['account_id'],self.account)
        self.assertEqual(ledger['delta_minor'],-self.form['amount_minor']);self.assertIn(self.form['purpose'],ledger['note'])
        self.assertEqual(payment['amount_minor'],self.form['amount_minor']);self.assertEqual(payment['status'],'succeeded')
        self.assertEqual(result['balance_before_minor']-result['balance_after_minor'],self.form['amount_minor'])
        reopened=FinanceGateway(self.path);reopened.bank.clock=lambda:self.now[0]
        self.assertEqual(reopened.bank.remittances(self.w,op['id'])['transaction_id'],ledger['id'])
        self.assertEqual(next(a for a in reopened.overview(self.w)['accounts'] if a['id']==self.account)['balance_minor'],before-self.form['amount_minor'])

    def test_reject_and_false_confirmation_never_debit_or_create_payment(self):
        before=self.counts();cash=self.balance();op=self.prepare()
        self.error('CONFIRMATION_REQUIRED',lambda:self.confirm(op,confirmed=False))
        self.g.bank.cancel(self.w,op['id']);self.g.bank.cancel(self.w,op['id'])
        self.assertEqual(self.balance(),cash);self.assertEqual(self.counts(),before)
        self.assertEqual(self.g.bank.remittances(self.w,op['id'])['status'],'cancelled')
        self.error('INVALID_STATE',lambda:self.confirm(op))

    def test_amount_and_full_account_validation(self):
        for change in ({'amount_minor':True},{'amount_minor':1.5},{'amount_minor':'100'},
            {'amount_minor':0},{'amount_minor':-1},{'amount_minor':100000001},
            {'recipient_account':6222021000000000000},{'recipient_account':'****1001'},
            {'recipient_account':'0'*19},{'recipient_branch':''},{'card_id':[]},{'card_id':''},
            {'purpose':'\n'},{'recipient_bank':' 工行'},{'message':'x'*241},
            {'idempotency_key':[],'mode':'ordinary'},{'mode':'instant'}):
            with self.subTest(change=change),self.assertRaises(BankError):self.prepare(**change)
        self.assertEqual(self.counts()['navren_remittances'],0)

    def test_card_account_and_owner_permissions_are_rechecked(self):
        archive=self.g.bank.customer_archive(self.w)['records']
        credit=next(c for c in archive['cards'] if c['type']=='credit')
        self.error('REMITTANCE_CARD_UNAVAILABLE',lambda:self.prepare(card_id=credit['id']))
        other_account=next(a['id'] for a in self.g.overview(self.w)['accounts'] if a['id']!=self.account)
        self.error('REMITTANCE_CARD_MISMATCH',lambda:self.prepare(account_id=other_account))
        with self.g.bank.transaction() as db:db.execute("UPDATE account_details SET status='frozen' WHERE workspace_id=? AND account_id=?",(self.w,self.account))
        self.error('REMITTANCE_ACCOUNT_UNAVAILABLE',lambda:self.prepare())
        with self.g.bank.transaction() as db:
            db.execute("UPDATE account_details SET status='active' WHERE workspace_id=? AND account_id=?",(self.w,self.account))
            db.execute("UPDATE signatories SET status='revoked' WHERE workspace_id=? AND account_id=?",(self.w,self.account))
        self.error('REMITTANCE_PERMISSION_REQUIRED',lambda:self.prepare())

    def test_cross_workspace_read_confirm_and_card_injection_are_blocked(self):
        op=self.prepare();other=self.g.bank.create_workspace()['workspace_id']
        self.error('NOT_FOUND',lambda:self.g.bank.remittances(other,op['id']))
        self.error('NOT_FOUND',lambda:self.g.confirm(other,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest']}))
        foreign=next(c for c in self.g.overview(other)['cards'] if c['type']=='debit' and c['status']=='active')
        self.error('NOT_FOUND',lambda:self.prepare(card_id=foreign['id'],idempotency_key='foreign-card'))

    def test_expired_digest_modified_and_missing_verification_do_not_pay(self):
        op=self.prepare();before=self.counts();cash=self.balance()
        self.error('CONFIRMATION_MISMATCH',lambda:self.confirm(op,confirmation_digest='tampered'))
        self.error('INVALID_VERIFICATION',lambda:self.g.confirm(self.w,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest']}))
        self.now[0]+=301;self.error('EXPIRED',lambda:self.confirm(op))
        self.assertEqual(self.balance(),cash);self.assertEqual(self.counts(),before)

    def test_idempotency_duplicate_confirmation_and_changed_fields(self):
        op=self.prepare();self.assertEqual(self.prepare()['id'],op['id'])
        self.error('IDEMPOTENCY_CONFLICT',lambda:self.prepare(message='不同附言'))
        before=self.balance();first=self.confirm(op);second=self.confirm(op,verification_code='')
        self.assertEqual(first['result'],second['result']);self.assertEqual(self.balance(),before-self.form['amount_minor'])
        self.assertEqual(len([r for r in self.g.bank.transactions(self.w) if r['operation_id']==op['id']]),1)

    def test_transfer_uses_disposable_balance_without_relaxing_investment(self):
        with self.g.bank.transaction() as db:db.execute('UPDATE profiles SET reserve_minor=100000000 WHERE workspace_id=?',(self.w,))
        self.error('LIQUIDITY_GUARD',lambda:self.g.prepare(self.w,{'action':'purchase','account_id':self.account,
            'product_id':'SIM-CASH-01','amount_minor':10000,'idempotency_key':'investment-still-protected'}))
        self.assertEqual(self.confirm(self.prepare())['state'],'succeeded')

    def test_actual_frozen_funds_reduce_available_and_cannot_be_spent(self):
        balance=self.balance()
        with self.g.bank.transaction() as db:
            db.execute("UPDATE fund_holds SET status='held',amount_minor=? WHERE workspace_id=?",(balance-5000,self.w))
        self.error('INSUFFICIENT_AVAILABLE',lambda:self.prepare(amount_minor=10000))
        self.assertEqual(self.counts()['navren_remittances'],0)

    def test_concurrent_same_confirmation_debits_once(self):
        op=self.prepare();before=self.balance()
        def call(_):
            try:return self.confirm(op)
            except BankError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(call,range(2)))
        self.assertTrue(any(isinstance(r,dict) and r['state']=='succeeded' for r in results))
        self.assertEqual(self.balance(),before-self.form['amount_minor'])
        self.assertEqual(len([r for r in self.g.bank.transactions(self.w) if r['operation_id']==op['id']]),1)

    def test_concurrent_distinct_drafts_keep_balance_nonnegative(self):
        first=self.prepare(idempotency_key='first');second=self.prepare(idempotency_key='second')
        before=self.balance()
        def call(op):
            try:return self.confirm(op)
            except BankError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(call,[first,second]))
        succeeded=sum(isinstance(r,dict) and r['state']=='succeeded' for r in results)
        self.assertEqual(succeeded,1);self.assertEqual(self.balance(),before-self.form['amount_minor'])
        self.assertGreaterEqual(self.balance(),0)

    def test_failure_after_ledger_write_rolls_back_money_and_business_records(self):
        op=self.prepare();before=self.counts();cash=self.balance()
        with patch.object(self.g.bank,'_record_payment',side_effect=RuntimeError('controlled_failure')):
            with self.assertRaises(RuntimeError):self.confirm(op)
        self.assertEqual(self.balance(),cash);self.assertEqual(self.counts(),before)
        self.assertEqual(self.g.bank.operation(self.w,op['id'])['state'],'pending_confirmation')

    def test_ordinary_holds_once_then_overview_processes_due_without_other_business(self):
        before=self.balance();op=self.prepare(mode='ordinary');accepted=self.confirm(op)
        self.assertEqual(accepted['state'],'scheduled');self.assertEqual(self.balance(),before)
        self.assertEqual(accepted['result']['held_minor'],self.form['amount_minor'])
        state=self.g.overview(self.w)
        self.assertEqual(state['held_funds_by_account'][self.account],self.form['amount_minor'])
        self.assertFalse(any(c['evidence_id']==accepted['result']['scheduled_payment_id'] for c in state['commitments']))
        self.assertEqual(self.confirm(op)['result'],accepted['result'])
        archive=self.g.bank.customer_archive(self.w);as_of=archive['as_of'];loans=archive['records']['loans'];orders=archive['records']['orders']
        self.now[0]+=7201;state=self.g.overview(self.w);finished=self.g.bank.remittances(self.w,op['id'])
        self.assertEqual(finished['status'],'succeeded');self.assertEqual(self.balance(),before-self.form['amount_minor'])
        self.assertEqual(state['held_funds_by_account'][self.account],0)
        self.assertEqual(state['as_of'],as_of);archive_after=self.g.bank.customer_archive(self.w)
        self.assertEqual(archive_after['records']['loans'],loans);self.assertEqual(archive_after['records']['orders'],orders)
        self.assertEqual(self.confirm(op)['state'],'succeeded')

    def test_next_day_waits_until_shanghai_nine_and_is_persistent(self):
        before=self.balance();op=self.prepare(mode='next_day');accepted=self.confirm(op)
        target=datetime.fromisoformat('2026-10-10T09:00:00+08:00').timestamp()
        self.assertEqual(accepted['result']['execute_at'],target)
        self.now[0]=target-1;self.assertEqual(self.g.bank.remittances(self.w,op['id'])['status'],'scheduled')
        reopened=FinanceGateway(self.path);reopened.bank.clock=lambda:self.now[0]
        self.assertEqual(reopened.bank.remittances(self.w,op['id'])['held_minor'],self.form['amount_minor'])
        self.now[0]=target;self.assertEqual(reopened.bank.remittances(self.w,op['id'])['status'],'succeeded')
        self.assertEqual(self.balance(),before-self.form['amount_minor'])

    def test_due_failure_releases_hold_and_records_failed_payment_without_debit(self):
        before=self.balance();op=self.prepare(mode='ordinary');self.confirm(op)
        with self.g.bank.transaction() as db:db.execute("UPDATE cards SET status='lost' WHERE workspace_id=? AND id=?",(self.w,self.form['card_id']))
        self.now[0]+=7201;record=self.g.bank.remittances(self.w,op['id'])
        self.assertEqual(record['status'],'failed');self.assertEqual(record['held_minor'],0)
        self.assertTrue(record['failure_reason']);self.assertIsNone(record['transaction_id'])
        self.assertEqual(self.balance(),before);self.assertEqual(self.g.overview(self.w)['held_funds_by_account'][self.account],0)
        self.assertEqual(self.confirm(op)['state'],'failed')

    def test_confirmed_schedule_cancellation_releases_freeze(self):
        before=self.balance();op=self.prepare(mode='ordinary');accepted=self.confirm(op)
        cancel=self.g.prepare(self.w,{'action':'cancel_scheduled_payment','account_id':self.account,
            'scheduled_payment_id':accepted['result']['scheduled_payment_id'],'idempotency_key':'cancel-scheduled'})['operation']
        self.confirm(cancel);self.now[0]+=7201
        record=self.g.bank.remittances(self.w,op['id']);self.assertEqual(record['status'],'cancelled')
        self.assertEqual(record['held_minor'],0);self.assertEqual(self.balance(),before)

    def test_parallel_due_reads_execute_once_and_exclude_own_freeze(self):
        before=self.balance();op=self.prepare(mode='ordinary');self.confirm(op);self.now[0]+=7201
        with ThreadPoolExecutor(max_workers=3) as pool:records=list(pool.map(lambda _:self.g.bank.remittances(self.w,op['id']),range(3)))
        self.assertTrue(all(r['status']=='succeeded' for r in records))
        self.assertEqual(len({r['transaction_id'] for r in records}),1)
        self.assertEqual(self.balance(),before-self.form['amount_minor'])

    def test_simulation_advance_also_executes_reserved_remittance_once(self):
        op=self.prepare(mode='next_day');self.confirm(op);before=self.balance()
        result=self.g.bank.advance_days(self.w,1)
        self.assertEqual(result['scheduled_transfers'],1);self.assertEqual(self.balance(),before-self.form['amount_minor'])
        self.assertEqual(self.g.bank.remittances(self.w,op['id'])['status'],'succeeded')
        self.assertEqual(self.g.bank.advance_days(self.w,1)['scheduled_transfers'],0)

    def test_empty_invalid_construction_schema_is_rebuilt_without_bank_data_loss(self):
        with closing(self.g.bank.connect()) as db,db:
            before=db.execute('SELECT count(*) FROM transactions').fetchone()[0]
            db.execute('DROP TABLE navren_remittances')
            db.execute('CREATE TABLE navren_remittances(id TEXT PRIMARY KEY,card_id TEXT REFERENCES cards(id))')
        rebuilt=SimulatedBank(self.path)
        with closing(rebuilt.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM transactions').fetchone()[0],before)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
        self.assertEqual(self.confirm(self.prepare())['state'],'succeeded')


class ConnectedRemittanceTests(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.now=[NOW]
        self.env=patch.dict(os.environ,{'NAVREN_KNOWLEDGE_DB':str(Path(self.directory.name)/'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB':'','NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode()})
        self.env.start();self.addCleanup(self.env.stop)
        self.g,self.session,self.form=build_isolated_fixture(self.directory.name,connected=True,clock=lambda:self.now[0])
        self.w=self.session['workspace_id'];self.token=self.session['session_token']

    def confirm(self,mode):
        self.form['mode']=mode
        with self.g.demo_sessions.request(self.token,self.w):
            op=self.g.prepare_remittance(self.w,self.form)['operation']
            self.g.confirm(self.w,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
                                         'verification_code':self.g.demo_code(self.token)})
        return op

    def read(self,operation):
        with self.g.demo_sessions.request(self.token,self.w):
            data=self.g.connected_assets.overview(self.w)['data']
            record=self.g.bank.remittances(self.w,operation['id'])
        return data,record

    def test_all_four_connected_demo_profiles_supply_name_and_truthful_missing_mobile(self):
        profiles=self.g.demo_accounts.catalog()['accounts']
        self.assertEqual(len(profiles),4)
        for profile in profiles:
            with self.subTest(user_id=profile['user_id']):
                workspace=profile['workspace_id'];_,token=self.g.demo_sessions.create(profile['user_id'])
                with self.g.demo_sessions.request(token,workspace):
                    state=self.g.overview(workspace)
                    funded={a['id'] for a in state['accounts'] if a['balance_minor']>0}
                    card=next(c for c in state['cards'] if c['type']=='debit' and c['status']=='active' and c['account_id'] in funded)
                    op=self.g.prepare_remittance(workspace,sample_form(card,key='profile-autofill',amount=1))['operation']
                    sender=op['payload']['remittance']
                    self.assertEqual(sender['sender_name'],profile['display_name']);self.assertEqual(sender['sender_phone'],'')
                    self.assertEqual(self.g.bank.remittances(workspace,op['id'])['sender_phone'],'')

    def test_next_day_connected_midnight_stays_frozen_until_nine(self):
        op=self.confirm('next_day');data,record=self.read(op)
        account=next(a for a in data['accounts'] if a['account_id']==self.form['account_id']);before=account['balance_minor']
        self.assertEqual(account['held_minor'],self.form['amount_minor'])
        self.assertEqual(account['available_minor'],before-self.form['amount_minor'])
        self.assertEqual(account['available_balance_minor'],account['available_minor'])
        self.now[0]=datetime.fromisoformat('2026-10-10T00:01:00+08:00').timestamp()
        data,record=self.read(op);self.assertEqual(record['status'],'scheduled')
        self.now[0]=datetime.fromisoformat('2026-10-10T09:00:00+08:00').timestamp()
        data,record=self.read(op);self.assertEqual(record['status'],'succeeded')
        account=next(a for a in data['accounts'] if a['account_id']==self.form['account_id'])
        self.assertEqual(account['balance_minor'],before-self.form['amount_minor'])
        self.assertEqual(account['held_minor'],0);self.assertEqual(account['remittance_held_minor'],0)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
            self.assertEqual(db.execute('SELECT count(*) FROM transactions WHERE operation_id=?',(op['id'],)).fetchone()[0],1)

    def test_ordinary_connected_cross_midnight_waits_full_two_hours(self):
        self.now[0]=datetime.fromisoformat('2026-10-09T23:30:00+08:00').timestamp()
        op=self.confirm('ordinary')
        self.now[0]=datetime.fromisoformat('2026-10-10T00:01:00+08:00').timestamp()
        _,record=self.read(op);self.assertEqual(record['status'],'scheduled')
        self.now[0]=datetime.fromisoformat('2026-10-10T01:30:00+08:00').timestamp()
        _,record=self.read(op);self.assertEqual(record['status'],'succeeded')

    def test_connected_explicit_revocation_cannot_fall_back_to_opening_owner(self):
        with self.g.bank.transaction() as db:
            card=self.g.bank._card(db,self.w,self.form['card_id'])
            db.execute('DELETE FROM signatories WHERE workspace_id=? AND account_id=?',(self.w,self.form['account_id']))
            self.g.bank._insert_archive_row(db,'signatories',self.w,{'account_id':self.form['account_id'],
                 'customer_id':card['customer_id'],'role':'owner','status':'revoked'})
        with self.g.demo_sessions.request(self.token,self.w),self.assertRaises(BankError) as caught:
            self.g.prepare_remittance(self.w,self.form)
        self.assertEqual(caught.exception.code,'REMITTANCE_PERMISSION_REQUIRED')

    def test_connected_closed_customer_has_no_opening_owner_fallback(self):
        with self.g.bank.transaction() as db:
            card=self.g.bank._card(db,self.w,self.form['card_id'])
            db.execute("UPDATE customers SET status='closed' WHERE workspace_id=? AND id=?",(self.w,card['customer_id']))
        with self.g.demo_sessions.request(self.token,self.w),self.assertRaises(BankError) as caught:
            self.g.prepare_remittance(self.w,self.form)
        self.assertEqual(caught.exception.code,'REMITTANCE_PERMISSION_REQUIRED')


class RemittanceHTTPTests(unittest.TestCase):
    def test_authenticated_prepare_confirm_read_cancel_and_refresh_contract(self):
        from server import create_server,ProxyHandler
        from webank_catalog import PublicProductCatalog
        from test_bank_integration import Browser
        with (tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{
            'NAVREN_KNOWLEDGE_DB':str(Path(directory)/'knowledge.sqlite3'),'NAVREN_FINANCIAL_DB':''}),
            patch.object(ProxyHandler,'log_message',lambda *args:None)):
            server=create_server(0,Path(directory)/'bank.sqlite3',public_catalog=PublicProductCatalog(lambda *a,**k:io.BytesIO()),public_sync_enabled=False)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                browser=Browser('http://127.0.0.1:'+str(server.server_port));browser.start()
                status,state,_=browser.call('/api/finance/overview');self.assertEqual(status,200)
                card=next(c for c in state['cards'] if c['type']=='debit' and c['status']=='active')
                form=sample_form(card);before=next(a['balance_minor'] for a in state['accounts'] if a['id']==card['account_id'])
                status,draft,_=browser.call('/api/finance/remittances/prepare','POST',form)
                self.assertEqual(status,200);self.assertEqual(draft['remittance']['recipient_account'],form['recipient_account'])
                op=draft['operation'];code=browser.call('/api/finance/demo-code')[1]['code']
                status,receipt,_=browser.call('/api/finance/operations/'+op['id']+'/confirm','POST',
                    {'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':code})
                self.assertEqual(status,200);self.assertEqual(receipt['operation']['state'],'succeeded')
                status,record,_=browser.call('/api/finance/remittances/'+op['id']);self.assertEqual(status,200)
                self.assertEqual(record['payment_id'],receipt['operation']['result']['payment_id'])
                self.assertTrue(record['transaction_id']);self.assertEqual(record['balance_after_minor'],before-form['amount_minor'])
                status,listing,_=browser.call('/api/finance/remittances');self.assertEqual(status,200)
                self.assertEqual(len(listing['remittances']),1)
                status,overview,_=browser.call('/api/finance/overview');self.assertEqual(status,200)
                self.assertIn(record['payment_id'],{r['id'] for r in overview['payment_instructions']})
                self.assertEqual(next(a['balance_minor'] for a in overview['accounts'] if a['id']==card['account_id']),before-form['amount_minor'])
                other=Browser(browser.base);other.start();self.assertEqual(other.call('/api/finance/remittances/'+op['id'])[0],404)
                self.assertEqual(browser.call('/api/finance/remittances?workspace=other')[0],400)
                form=sample_form(card,key='refusal')
                refused=browser.call('/api/finance/remittances/prepare','POST',form)[1]['operation']
                self.assertEqual(browser.call('/api/finance/operations/'+refused['id']+'/cancel','POST',{})[0],200)
                self.assertEqual(browser.call('/api/finance/remittances/'+refused['id'])[1]['status'],'cancelled')
            finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
