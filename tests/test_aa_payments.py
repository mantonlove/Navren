"""AA双边账本的定向边界检查，只用新建合成库及其隔离副本。"""
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
from simulated_bank import BankError
from simulated_bank.aa_payments import bills

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()


def build_isolated_fixture(directory,*,clock=None):
    """总控可复用：新建四用户连接账本，不复制现役账本或个人库。"""
    from test_remittance_simulation import build_isolated_fixture as remittance_fixture
    g,_,_=remittance_fixture(directory,connected=True,clock=clock or (lambda:NOW))
    with closing(g.bank.connect()) as db:
        users=[dict(r) for r in db.execute('SELECT user_id,workspace_id,display_name FROM navren_demo_accounts ORDER BY user_id')]
    return g,users


class AAPaymentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.fernet import Fernet
        cls.base=tempfile.TemporaryDirectory();cls.key=Fernet.generate_key().decode()
        with patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':cls.key}):
            cls.base_g,cls.users=build_isolated_fixture(cls.base.name)

    @classmethod
    def tearDownClass(cls):cls.base.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'bank.sqlite3';self.now=[NOW]
        with sqlite3.connect(self.base_g.bank.database.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(self.path) as target:source.backup(target)
        self.env=patch.dict(os.environ,{'NAVREN_KNOWLEDGE_DB':str(Path(self.temp.name)/'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB':'','NAVREN_PERSONAL_DATA_KEY':self.key})
        self.env.start();self.addCleanup(self.env.stop)
        self.g=FinanceGateway(self.path,financial_database=ROOT/'outputs/financial-data/navren-financial-v3.sqlite3')
        self.g.bank.clock=lambda:self.now[0]
        self.a,self.b,self.c,self.d=[dict(u) for u in self.users]
        self.tokens={u['user_id']:self.g.demo_sessions.create(u['user_id'])[1] for u in self.users}
        self.cards={}
        for u in self.users:
            self.cards[u['user_id']]=next(c for c in self.g.overview(u['workspace_id'])['cards'] if c['type']=='debit' and c['status']=='active')

    def card(self,u):return self.cards[u['user_id']]
    def error(self,code,call):
        with self.assertRaises(BankError) as e:call()
        if code is not None:self.assertEqual(e.exception.code,code)
        return e.exception

    def balance(self,u):
        with closing(self.g.bank.connect()) as db:return db.execute('SELECT balance_minor FROM accounts WHERE workspace_id=? AND id=?',(u['workspace_id'],self.card(u)['account_id'])).fetchone()[0]

    def counts(self):
        with closing(self.g.bank.connect()) as db:return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('transactions','navren_aa_bills','navren_aa_shares','navren_aa_receipts')}

    def prepare_create(self,**changes):
        payload={'account_id':self.card(self.a)['account_id'],'title':'合成聚餐','total_minor':2001,
            'split_mode':'equal','participants':[{'user_id':self.b['user_id']},{'user_id':self.a['user_id']}],
            'idempotency_key':'create',**changes}
        with self.g.demo_sessions.request(self.tokens[self.a['user_id']],self.a['workspace_id']):
            return self.g.prepare_aa(self.a['workspace_id'],payload)

    def confirm(self,u,op,**changes):
        token=self.tokens[u['user_id']]
        body={'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':self.g.demo_code(token),**changes}
        with self.g.demo_sessions.request(token,u['workspace_id']):return self.g.confirm(u['workspace_id'],op['id'],body)['operation']

    def create(self,**changes):
        op=self.prepare_create(**changes)['operation'];done=self.confirm(self.a,op)
        return self.g.aa_bills(self.a['workspace_id'],done['result']['bill_id'])

    def prepare_pay(self,bill,u=None,key='pay',**changes):
        u=u or self.b;card=self.card(u);share=self.g.aa_bills(u['workspace_id'],bill['id'])['my_share']
        body={'account_id':card['account_id'],'card_id':card['id'],'idempotency_key':key,**changes}
        with self.g.demo_sessions.request(self.tokens[u['user_id']],u['workspace_id']):
            return self.g.prepare_aa(u['workspace_id'],body,kind='pay',object_id=share['id'])['operation']

    def test_create_prepare_equal_remainder_own_share_and_refusal_have_no_money_effect(self):
        before=self.counts();balance=self.balance(self.a);view=self.prepare_create()
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(self.a),balance)
        self.assertEqual(view['aa']['bill']['own_share_minor'],1001);self.assertEqual(view['aa']['bill']['receivable_minor'],1000)
        self.g.bank.cancel(self.a['workspace_id'],view['operation']['id']);self.assertEqual(self.counts(),before)
        bill=self.create(idempotency_key='accepted');self.assertEqual(self.balance(self.a),balance)
        self.assertEqual(bill['my_share']['status'],'own');self.assertFalse(bill['my_share']['payable'])
        self.assertFalse(self.g.aa_bills(self.a['workspace_id'])['pending'])

    def test_custom_integer_totals_duplicates_unknown_members_and_bad_fields(self):
        self.error('AA_TOTAL_MISMATCH',lambda:self.prepare_create(split_mode='custom',participants=[{'user_id':self.a['user_id'],'share_minor':500},{'user_id':self.b['user_id'],'share_minor':500}]))
        for amount in (True,2001.0,'2001',-1):self.error('INVALID_INTEGER',lambda:self.prepare_create(total_minor=amount))
        for people in ([{'user_id':self.a['user_id']},{'user_id':self.a['user_id']}], [{'user_id':self.b['user_id']},{'user_id':self.c['user_id']}]):
            self.error('AA_PARTICIPANTS_INVALID',lambda:self.prepare_create(participants=people))
        self.error('AA_IDENTITY_REQUIRED',lambda:self.prepare_create(participants=[{'user_id':self.a['user_id']},{'user_id':'unknown'}]))
        self.error('INVALID_FIELDS',lambda:self.prepare_create(participants=[{'user_id':self.a['user_id'],'share_minor':1000},{'user_id':self.b['user_id']}]))
        bill=self.create(split_mode='custom',participants=[{'user_id':self.a['user_id'],'share_minor':0},{'user_id':self.b['user_id'],'share_minor':2001}])
        self.assertEqual(bill['own_share_minor'],0);self.assertEqual(bill['receivable_minor'],2001)

    def test_atomic_payment_both_balances_versions_ledgers_receipts_and_current_timestamp(self):
        bill=self.create();aa=self.balance(self.a);bb=self.balance(self.b);op=self.prepare_pay(bill)
        with closing(self.g.bank.connect()) as db:
            versions={u['workspace_id']:db.execute('SELECT version FROM workspaces WHERE id=?',(u['workspace_id'],)).fetchone()[0] for u in (self.a,self.b)}
            snapshots=list(db.execute('SELECT snapshot FROM connected_asset_migrations ORDER BY workspace_id'))
        before=self.counts();self.assertEqual(before['navren_aa_receipts'],0)
        result=self.confirm(self.b,op)['result'];self.assertEqual(result['amount_minor'],1000)
        self.assertEqual(self.balance(self.a),aa+1000);self.assertEqual(self.balance(self.b),bb-1000)
        with closing(self.g.bank.connect()) as db:
            rows=list(db.execute("SELECT * FROM transactions WHERE category IN ('AA分摊付款','AA垫付回收')"))
            self.assertEqual(len(rows),2);self.assertEqual(sum(r['delta_minor'] for r in rows),0)
            self.assertEqual({r['occurred_at'] for r in rows},{'2026-10-10T00:00:00+08:00'})
            self.assertTrue(all(r['kind']=='transfer' for r in rows))
            for w,v in versions.items():self.assertEqual(db.execute('SELECT version FROM workspaces WHERE id=?',(w,)).fetchone()[0],v+1)
            self.assertEqual([tuple(r) for r in db.execute('SELECT snapshot FROM connected_asset_migrations ORDER BY workspace_id')],[tuple(r) for r in snapshots])
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
        self.assertEqual(self.g.aa_bills(self.a['workspace_id'],bill['id'])['status'],'paid')
        current=self.g.connected_assets.overview(self.b['workspace_id'])['data']
        account=next(a for a in current['accounts'] if a['account_id']==self.card(self.b)['account_id'] or a.get('runtime_account_id')==self.card(self.b)['account_id'])
        self.assertEqual(account['balance_minor'],bb-1000)

    def test_replay_distinct_idempotency_keys_and_concurrent_confirm_settle_share_once(self):
        bill=self.create();op=self.prepare_pay(bill);other=self.prepare_pay(bill,key='other');before=self.balance(self.b)
        self.assertEqual(self.prepare_pay(bill)['id'],op['id'])
        token=self.tokens[self.b['user_id']]
        def pay(which):
            try:return self.confirm(self.b,which)['state']
            except BankError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(pay,[op,other]))
        self.assertEqual(outcomes.count('succeeded'),1);self.assertEqual(self.balance(self.b),before-1000)
        winner=op if self.g.bank.operation(self.b['workspace_id'],op['id'])['state']=='succeeded' else other
        self.assertEqual(self.confirm(self.b,winner)['result'],self.confirm(self.b,winner)['result'])
        self.error('AA_NOT_PAYABLE',lambda:self.prepare_pay(bill,key='third'))
        self.assertEqual(self.counts()['navren_aa_receipts'],1)

    def test_privacy_third_party_other_participant_receipts_and_operation(self):
        bill=self.create(total_minor=3000,participants=[{'user_id':u['user_id']} for u in (self.a,self.b,self.c)])
        pay=self.prepare_pay(bill);self.confirm(self.b,pay)
        b=self.g.aa_bills(self.b['workspace_id'],bill['id']);c=self.g.aa_bills(self.c['workspace_id'],bill['id'])
        self.assertNotIn('participants',b);self.assertNotIn('account_id',b);self.assertEqual(c['receipts'],[])
        self.assertEqual(len(b['receipts']),1);self.assertEqual(b['receipts'][0]['side'],'payer')
        creator=self.g.aa_bills(self.a['workspace_id'],bill['id']);receipt=creator['receipts'][0]
        text=json.dumps(receipt);self.assertNotIn(self.card(self.b)['id'],text);self.assertNotIn(self.card(self.b)['account_id'],text)
        self.assertNotIn(pay['id'],text);self.assertEqual(receipt['side'],'recipient')
        self.error('AA_NOT_FOUND',lambda:self.g.aa_bills(self.d['workspace_id'],bill['id']))
        self.error('NOT_FOUND',lambda:self.g.operation_view(self.c['workspace_id'],pay['id']))
        share=b['my_share']['id']
        self.error('AA_NOT_FOUND',lambda:self.g.prepare_aa(self.c['workspace_id'],{'idempotency_key':'steal'},kind='decline',object_id=share))

    def test_decline_is_self_only_and_cancel_only_unpaid_preserves_paid_money(self):
        bill=self.create(total_minor=3000,participants=[{'user_id':u['user_id']} for u in (self.a,self.b,self.c)])
        self.confirm(self.b,self.prepare_pay(bill));paid_bal=self.balance(self.b);received=self.balance(self.a)
        cancellation=self.g.prepare_aa(self.a['workspace_id'],{'idempotency_key':'cancel'},kind='cancel',object_id=bill['id'])
        self.assertFalse(cancellation['verification_required']);self.confirm(self.a,cancellation['operation'])
        updated=self.g.aa_bills(self.a['workspace_id'],bill['id']);self.assertEqual(updated['status'],'cancelled')
        self.assertEqual(self.balance(self.b),paid_bal);self.assertEqual(self.balance(self.a),received)
        self.assertEqual(len(updated['receipts']),1);self.assertEqual(self.g.aa_bills(self.c['workspace_id'],bill['id'])['my_share']['status'],'cancelled')
        self.error('AA_NOT_PAYABLE',lambda:self.prepare_pay(bill,u=self.c))
        second=self.create(idempotency_key='second');share=self.g.aa_bills(self.b['workspace_id'],second['id'])['my_share']
        op=self.g.prepare_aa(self.b['workspace_id'],{'idempotency_key':'decline'},kind='decline',object_id=share['id'])['operation']
        before=self.counts();self.confirm(self.b,op);self.assertEqual(self.counts(),before)
        self.assertEqual(self.g.aa_bills(self.a['workspace_id'],second['id'])['status'],'closed')
        self.error('AA_NOT_PAYABLE',lambda:self.prepare_pay(second,key='second-pay'))

    def test_pay_prepared_before_cancel_and_decline_is_rechecked_under_same_lock(self):
        for mode in ('cancel','decline'):
            bill=self.create(idempotency_key=mode);pay=self.prepare_pay(bill,key='pay-'+mode);before=self.balance(self.b)
            if mode=='cancel':u=self.a;obj=bill['id']
            else:u=self.b;obj=self.g.aa_bills(self.b['workspace_id'],bill['id'])['my_share']['id']
            op=self.g.prepare_aa(u['workspace_id'],{'idempotency_key':'resolve-'+mode},kind=mode,object_id=obj)['operation']
            self.confirm(u,op);self.error(None,lambda:self.confirm(self.b,pay));self.assertEqual(self.balance(self.b),before)
        self.assertEqual(self.counts()['navren_aa_receipts'],0)

    def test_balance_actual_holds_and_pending_remittance_freeze_block_overcommit(self):
        from test_remittance_simulation import sample_form
        card=self.card(self.b);account=card['account_id']
        with self.g.bank.transaction() as db:db.execute('UPDATE accounts SET balance_minor=2000 WHERE id=?',(account,))
        rem=self.g.prepare_remittance(self.b['workspace_id'],sample_form(card,mode='ordinary',amount=1500))['operation']
        self.confirm(self.b,rem)
        bill=self.create(total_minor=2000)
        self.error('INSUFFICIENT_AVAILABLE',lambda:self.prepare_pay(bill))
        self.assertEqual(self.balance(self.b),2000);self.assertEqual(self.counts()['navren_aa_receipts'],0)

    def test_payment_card_receiver_account_state_ownership_and_expiry_rechecked(self):
        cases=[('cards','status','lost',self.b,'AA_PERMISSION_REQUIRED'),
               ('cards','type','credit',self.b,'AA_PERMISSION_REQUIRED'),
               ('cards','expires_on','2020-01-01',self.b,'AA_PERMISSION_REQUIRED'),
               ('account_details','status','frozen',self.a,'AA_ACCOUNT_UNAVAILABLE'),
               ('customers','status','closed',self.b,'AA_PERMISSION_REQUIRED')]
        for table,field,value,u,code in cases:
            with self.subTest(table=table,field=field):
                bill=self.create(idempotency_key=table+field);op=self.prepare_pay(bill,key=table+field);before=self.balance(self.b)
                w=u['workspace_id']
                with self.g.bank.transaction() as db:
                    rows=list(db.execute('SELECT id,'+field+' FROM '+table+' WHERE workspace_id=?',(w,)))
                    db.execute('UPDATE '+table+' SET '+field+'=? WHERE workspace_id=?',(value,w))
                try:
                    from simulated_bank.aa_payments import validate
                    with self.g.bank.transaction() as db:self.error(code,lambda:validate(self.g.bank,db,self.b['workspace_id'],op['payload']))
                    e=self.error(None,lambda:self.confirm(self.b,op))
                    self.assertIn(e.code,(code,'METRIC_VERSION_CONFLICT'));self.assertEqual(self.balance(self.b),before)
                finally:
                    with self.g.bank.transaction() as db:
                        for row in rows:db.execute('UPDATE '+table+' SET '+field+'=? WHERE workspace_id=? AND id=?',(row[1],w,row[0]))
        self.error('NOT_FOUND',lambda:self.prepare_pay(self.create(idempotency_key='inject'),card_id=self.card(self.a)['id']))

    def test_account_type_limits_and_connected_owner_explicit_revoke(self):
        bill=self.create();card=self.card(self.b);w=self.b['workspace_id']
        with self.g.bank.transaction() as db:
            db.execute("UPDATE connected_account_links SET account_type='payment_wallet' WHERE workspace_id=? AND account_id=?",(w,card['account_id']))
        self.error('AA_ACCOUNT_UNAVAILABLE',lambda:self.prepare_pay(bill))
        with self.g.bank.transaction() as db:
            db.execute("UPDATE connected_account_links SET account_type='bank_settlement' WHERE workspace_id=? AND account_id=?",(w,card['account_id']))
            db.execute("UPDATE account_limits SET limit_minor=500 WHERE workspace_id=? AND account_id=? AND kind='transfer_single'",(w,card['account_id']))
        self.error(None,lambda:self.prepare_pay(bill))
        with self.g.bank.transaction() as db:
            db.execute("UPDATE account_limits SET limit_minor=2000000 WHERE workspace_id=? AND account_id=? AND kind='transfer_single'",(w,card['account_id']))
            changed=db.execute("UPDATE signatories SET status='revoked' WHERE workspace_id=? AND account_id=?",(w,card['account_id'])).rowcount
            if not changed:self.g.bank._insert_archive_row(db,'signatories',w,{'account_id':card['account_id'],'customer_id':card['customer_id'],'role':'owner','status':'revoked'})
        self.error('AA_PERMISSION_REQUIRED',lambda:self.prepare_pay(bill))

    def test_transaction_failure_after_both_balance_updates_rolls_everything_back(self):
        bill=self.create();op=self.prepare_pay(bill);before=self.counts();aa=self.balance(self.a);bb=self.balance(self.b)
        with patch.object(self.g.bank,'_ledger',side_effect=RuntimeError('injected-write-failure')):
            with self.assertRaises(RuntimeError):self.confirm(self.b,op)
        self.assertEqual(self.balance(self.a),aa);self.assertEqual(self.balance(self.b),bb);self.assertEqual(self.counts(),before)
        self.assertEqual(self.g.bank.operation(self.b['workspace_id'],op['id'])['state'],'pending_confirmation')
        self.assertEqual(self.confirm(self.b,op)['state'],'succeeded')

    def test_confirmation_digest_verification_expiry_and_false_confirmation_do_not_pay(self):
        bill=self.create();op=self.prepare_pay(bill);bb=self.balance(self.b)
        self.error('CONFIRMATION_REQUIRED',lambda:self.confirm(self.b,op,confirmed=False))
        self.error('CONFIRMATION_MISMATCH',lambda:self.confirm(self.b,op,confirmation_digest='changed'))
        self.error('INVALID_VERIFICATION',lambda:self.confirm(self.b,op,verification_code='wrong'))
        self.now[0]+=301;self.error('EXPIRED',lambda:self.confirm(self.b,op))
        self.assertEqual(self.balance(self.b),bb);self.assertEqual(self.counts()['navren_aa_receipts'],0)

    def test_switch_session_under_write_lock_prevents_confirmation(self):
        bill=self.create();op=self.prepare_pay(bill);before=self.balance(self.b);token=self.tokens[self.b['user_id']]
        self.g.demo_sessions.select(token,self.c['user_id'],self.b['workspace_id'])
        self.error('SESSION_CHANGED',lambda:self.confirm(self.b,op))
        self.assertEqual(self.balance(self.b),before);self.assertEqual(self.counts()['navren_aa_receipts'],0)

    def test_deadline_and_no_self_payment(self):
        self.error('INVALID_DATE',lambda:self.prepare_create(due_at='2026-10-10T00:00:00'))
        self.error('INVALID_DATE',lambda:self.prepare_create(due_at='2026-10-09T00:00:00+08:00'))
        bill=self.create(due_at='2026-10-10T00:00:02+08:00');op=self.prepare_pay(bill);self.now[0]+=3
        self.error('AA_NOT_PAYABLE',lambda:self.confirm(self.b,op));self.assertFalse(self.g.aa_bills(self.b['workspace_id'],bill['id'])['my_share']['payable'])
        self.error('AA_NOT_PAYABLE',lambda:self.prepare_pay(bill,u=self.a))

    def test_contact_alias_crud_privacy_and_deletion_does_not_change_bound_bill(self):
        w=self.a['workspace_id'];before=self.counts()
        result=self.g.payment_contacts(w);self.assertEqual(len(result['available_users']),3)
        self.assertNotIn('workspace',json.dumps(result));self.assertEqual(result['current_user_id'],self.a['user_id'])
        body={'user_id':self.b['user_id'],'alias':'聚餐伙伴'}
        c=self.g.payment_contacts(w,body,action='create')['contact']
        self.assertEqual(self.g.payment_contacts(w,body,action='create')['contact']['id'],c['id'])
        self.g.payment_contacts(w,{'alias':'同学'},c['id'],'update')
        self.error('AA_NOT_FOUND',lambda:self.g.payment_contacts(self.c['workspace_id'],{},c['id'],'delete'))
        self.assertEqual(self.counts(),before)
        bill=self.create();self.g.payment_contacts(w,{},c['id'],'delete')
        self.assertEqual(self.g.aa_bills(self.b['workspace_id'],bill['id'])['my_share']['user_id'],self.b['user_id'])
        self.confirm(self.b,self.prepare_pay(bill));self.assertEqual(self.g.aa_bills(w,bill['id'])['status'],'paid')

    def test_legacy_collection_cannot_credit_new_bill_and_source_consumption_is_owned(self):
        bill=self.create();share=self.g.aa_bills(self.b['workspace_id'],bill['id'])['my_share']
        self.error('AA_LEGACY_COLLECTION_FORBIDDEN',lambda:self.g.prepare(self.a['workspace_id'],{
            'action':'collect_split_payment','account_id':self.card(self.a)['account_id'],'split_id':bill['id'],
            'participant_id':share['id'],'idempotency_key':'legacy'}))
        self.error('AA_SOURCE_INVALID',lambda:self.prepare_create(idempotency_key='missing-source',source_transaction_id='missing'))
        with self.g.bank.transaction() as db:
            tx=self.g.bank._ledger(db,self.a['workspace_id'],self.card(self.a)['account_id'],datetime.fromtimestamp(NOW).date(),
                '合成消费',5000,-5000,'expense',None,category='餐饮',note='仅测试原消费关联')
        linked=self.create(idempotency_key='linked',source_transaction_id='runtime:'+tx)
        self.assertEqual(linked['source_transaction_id'],tx)
        self.assertIsNone(self.g.aa_bills(self.b['workspace_id'],linked['id'])['source_transaction_id'])

    def test_reopen_refresh_preserves_receipt_and_construction_does_not_replay(self):
        bill=self.create();pay=self.prepare_pay(bill);receipt=self.confirm(self.b,pay)['result'];counts=self.counts();a=self.balance(self.a)
        reopened=FinanceGateway(self.path,financial_database=ROOT/'outputs/financial-data/navren-financial-v3.sqlite3');reopened.bank.clock=lambda:self.now[0]
        self.assertEqual(reopened.aa_bills(self.b['workspace_id'],bill['id'])['receipts'][0],receipt)
        self.assertEqual(self.counts(),counts);self.assertEqual(self.balance(self.a),a)
        self.assertEqual(reopened.bank.operation(self.b['workspace_id'],pay['id'])['result'],receipt)

    def test_completed_create_replay_preserves_saved_card_even_after_loss_or_freeze(self):
        view=self.prepare_create();op=view['operation'];done=self.confirm(self.a,op);before=self.counts()
        with self.g.bank.transaction() as db:
            db.execute("UPDATE cards SET status='lost' WHERE workspace_id=? AND id=?",(self.a['workspace_id'],op['payload']['aa_bill']['receiver_card_id']))
            db.execute("UPDATE account_details SET status='frozen' WHERE workspace_id=? AND account_id=?",(self.a['workspace_id'],op['payload']['account_id']))
        replay=self.prepare_create();self.assertEqual(replay['operation']['id'],op['id'])
        self.assertEqual(self.confirm(self.a,op)['result'],done['result']);self.assertEqual(self.counts(),before)
        self.error('IDEMPOTENCY_CONFLICT',lambda:self.prepare_create(title='更改金额语义'))
        self.error('AA_ACCOUNT_UNAVAILABLE',lambda:self.prepare_pay(self.g.aa_bills(self.a['workspace_id'],done['result']['bill_id'])))

    def test_read_snapshot_never_combines_old_share_and_new_receipt_after_commit(self):
        import simulated_bank.aa_payments as aa
        bill=self.create();op=self.prepare_pay(bill);original=aa.bill_row;fired=[False]
        def read_and_commit(db,bid):
            row=original(db,bid)
            if not fired[0]:
                fired[0]=True;self.confirm(self.b,op)
            return row
        with patch.object(aa,'bill_row',side_effect=read_and_commit):
            observed=self.g.aa_bills(self.b['workspace_id'],bill['id'])
        self.assertEqual(observed['status'],'open');self.assertEqual(observed['my_share']['status'],'pending');self.assertEqual(observed['receipts'],[])
        self.assertEqual(self.g.aa_bills(self.b['workspace_id'],bill['id'])['my_share']['status'],'paid')

    def test_online_disabled_and_history_source_fail_closed(self):
        bill=self.create();card=self.card(self.b)
        with self.g.bank.transaction() as db:db.execute("UPDATE card_controls SET enabled=0 WHERE workspace_id=? AND card_id=? AND control_type='online'",(self.b['workspace_id'],card['id']))
        self.error('AA_CARD_UNAVAILABLE',lambda:self.prepare_pay(bill))
        self.error('AA_SOURCE_INVALID',lambda:self.prepare_create(idempotency_key='historical',source_transaction_id='history:opaque'))

    def test_future_business_day_cannot_bypass_actual_day_aa_transfer_limit(self):
        w=self.b['workspace_id'];account=self.card(self.b)['account_id'];before=self.balance(self.b)
        with self.g.bank.transaction() as db:
            db.execute("UPDATE workspaces SET as_of='2026-10-11' WHERE id=?",(w,))
            db.execute("UPDATE account_limits SET limit_minor=1500 WHERE workspace_id=? AND account_id=? AND kind='transfer_daily'",(w,account))
        first=self.create(total_minor=2000,idempotency_key='day-limit-first')
        self.confirm(self.b,self.prepare_pay(first,key='day-limit-pay-first'))
        second=self.create(total_minor=2000,idempotency_key='day-limit-second')
        self.error('TRANSFER_DAILY_LIMIT',lambda:self.prepare_pay(second,key='day-limit-pay-second'))
        self.assertEqual(self.balance(self.b),before-1000)
        self.assertEqual(self.counts()['navren_aa_receipts'],1)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT as_of FROM workspaces WHERE id=?',(w,)).fetchone()[0],'2026-10-11')
            rows=list(db.execute("SELECT occurred_at,delta_minor FROM transactions WHERE workspace_id=? AND category='AA分摊付款'",(w,)))
            self.assertEqual([(r['occurred_at'],r['delta_minor']) for r in rows],[('2026-10-10T00:00:00+08:00',-1000)])

    def test_operation_read_keeps_pending_state_and_share_in_one_snapshot(self):
        import simulated_bank.aa_payments as aa
        bill=self.create();op=self.prepare_pay(bill);original=aa.bill_row;fired=[False]
        def read_and_commit(db,bid):
            row=original(db,bid)
            if not fired[0]:fired[0]=True;self.confirm(self.b,op)
            return row
        with patch.object(aa,'bill_row',side_effect=read_and_commit):
            observed=aa.operation_view(self.g.bank,self.b['workspace_id'],op['id'],include_operation=True)
        self.assertEqual(observed['operation']['state'],'pending_confirmation')
        self.assertEqual(observed['aa']['share']['status'],'pending');self.assertEqual(observed['aa']['bill']['receipts'],[])
        refreshed=self.g.operation_view(self.b['workspace_id'],op['id'])
        self.assertEqual(refreshed['operation']['state'],'succeeded');self.assertEqual(refreshed['aa']['share']['status'],'paid')

    def test_real_http_contacts_create_pay_refresh_and_scope_csrf_guards(self):
        from server import create_server,ProxyHandler
        from webank_catalog import PublicProductCatalog
        from test_bank_integration import Browser
        from finance_gateway import COOKIE
        source=ROOT/'outputs/financial-data/navren-financial-v3.sqlite3'
        with patch.dict(os.environ,{'NAVREN_FINANCIAL_DB':str(source)}),patch.object(ProxyHandler,'log_message',lambda *args:None):
            server=create_server(0,self.path,public_catalog=PublicProductCatalog(lambda *a,**k:io.BytesIO()),public_sync_enabled=False)
            server.finance.bank.clock=lambda:self.now[0]
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                def call(u,path,method='GET',body=None,csrf=True):
                    token=self.tokens[u['user_id']];browser=Browser(base)
                    browser.csrf=self.g.csrf(token) if csrf else None
                    return browser.call(path,method,body,headers={'Cookie':COOKIE+'='+token})[:2]
                status,directory=call(self.a,'/api/finance/payment-contacts');self.assertEqual(status,200)
                self.assertEqual(len(directory['available_users']),3)
                contact=call(self.a,'/api/finance/payment-contacts','POST',{'user_id':self.b['user_id'],'alias':'同学'})
                self.assertEqual(contact[0],200)
                body={'account_id':self.card(self.a)['account_id'],'title':'HTTP聚餐','total_minor':2642,
                    'split_mode':'equal','participants':[{'user_id':self.a['user_id']},{'user_id':self.b['user_id']}],'idempotency_key':'http-create'}
                self.assertEqual(call(self.a,'/api/finance/aa/bills/prepare','POST',body,csrf=False)[0],403)
                status,view=call(self.a,'/api/finance/aa/bills/prepare','POST',body);self.assertEqual(status,200)
                op=view['operation'];request={'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':self.g.demo_code(self.tokens[self.a['user_id']])}
                status,done=call(self.a,'/api/finance/operations/'+op['id']+'/confirm','POST',request);self.assertEqual(status,200)
                bill_id=done['operation']['result']['bill_id'];path='/api/finance/aa/bills/'+bill_id
                bill=call(self.b,path)[1];share=bill['my_share']['id'];self.assertEqual(call(self.c,path)[0],404)
                card=self.card(self.b);paypath='/api/finance/aa/shares/'+share+'/pay/prepare'
                status,view=call(self.b,paypath,'POST',{'account_id':card['account_id'],'card_id':card['id'],'idempotency_key':'http-pay'});self.assertEqual(status,200)
                op=view['operation'];request={'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':self.g.demo_code(self.tokens[self.b['user_id']])}
                status,done=call(self.b,'/api/finance/operations/'+op['id']+'/confirm','POST',request);self.assertEqual(status,200)
                receipt=done['operation']['result'];self.assertEqual(receipt['paid_at'],'2026-10-10T00:00:00+08:00')
                self.assertEqual(receipt['amount_minor'],1321)
                refreshed=call(self.b,path)[1];self.assertEqual(refreshed['receipts'][0],receipt)
                self.assertEqual(call(self.b,'/api/finance/operations/'+op['id'])[1]['operation']['result'],receipt)
                self.assertEqual(call(self.c,'/api/finance/operations/'+op['id'])[0],404)
                self.assertEqual(call(self.b,'/api/finance/aa/bills?workspace=other')[0],400)
                self.assertEqual(call(self.a,'/api/finance/payment-contacts/'+contact[1]['contact']['id']+'/delete','POST',{})[0],200)
                self.assertEqual(len(call(self.a,path)[1]['receipts']),1)
            finally:server.shutdown();server.server_close();thread.join(2)

    def test_committed_confirm_disconnected_response_can_be_queried_without_second_payment(self):
        from finance_gateway import COOKIE
        bill=self.create();op=self.prepare_pay(bill);token=self.tokens[self.b['user_id']]
        body={'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':self.g.demo_code(token)}
        raw=json.dumps(body).encode();responses=[]
        class Handler:
            path='/api/finance/operations/'+op['id']+'/confirm';command='POST'
            from types import SimpleNamespace
            server=SimpleNamespace(server_port=12345);client_address=('127.0.0.1',1)
            headers={'Host':'127.0.0.1:12345','Content-Type':'application/json','Content-Length':str(len(raw)),
                'Cookie':COOKIE+'='+token,'X-QZ-CSRF':self.g.csrf(token)}
            rfile=io.BytesIO(raw)
            def _finance_json(self,*args):responses.append(args[0]);raise ConnectionError('injected-client-disconnect')
        before=self.balance(self.b);self.assertTrue(self.g.dispatch(Handler()))
        self.assertEqual(responses,[200])
        refreshed=self.g.operation_view(self.b['workspace_id'],op['id'])['operation']
        self.assertEqual(refreshed['state'],'succeeded');self.assertEqual(self.balance(self.b),before-1000)
        self.confirm(self.b,op);self.assertEqual(self.counts()['navren_aa_receipts'],1);self.assertEqual(self.balance(self.b),before-1000)


if __name__=='__main__':unittest.main()
