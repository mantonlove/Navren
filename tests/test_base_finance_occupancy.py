"""实际临时银行 held+本人目标确认的关联验收；helper夹具预算不是正式发行凭据。"""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import date,timedelta
from pathlib import Path
import json
import socket
import tempfile
import unittest
from unittest.mock import patch
import uuid

from financial_graph import SCHEMA as GRAPH_SCHEMA
from financial_methods import MetricContext
from financial_methods.funding import CashSnapshot,Hold,evaluate_funding
from goal_service import GoalService,version
from base_runtime.finance_occupancy import goal_occupancy_facts,verify_goal_occupancy
from simulated_bank import BankError,SimulatedBank


class FinanceOccupancyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory(prefix='navren-finance-occupancy-')
        cls.network=patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden'))
        cls.network.start();cls.bank=SimulatedBank(Path(cls.folder.name)/'bank.sqlite3')
        cls.goals=GoalService(cls.bank)
        with closing(cls.bank.connect()) as db:db.executescript(GRAPH_SCHEMA)
    @classmethod
    def tearDownClass(cls):cls.network.stop();cls.folder.cleanup()
    def setUp(self):
        self.credentials=self.bank.create_workspace();self.w=self.credentials['workspace_id']
        archive=self.bank.customer_archive(self.w);self.account=max(archive['accounts'],key=lambda a:a['balance_minor'])['id']
        self.other_account=next(a['id'] for a in archive['accounts'] if a['id']!=self.account)
        self.task=next(t for t in archive['records']['orchestration_tasks'] if t['status']=='draft')
        with self.bank.transaction() as db:db.execute('DELETE FROM wealth_goals WHERE workspace_id=?',(self.w,))
        prepared=self.bank.prepare(self.w,dict(action='reserve_funds',account_id=self.account,task_id=self.task['id'],
            amount_minor=30000,expires_on='2026-10-17',idempotency_key=uuid.uuid4().hex))
        body={'confirmed':True,'confirmation_digest':prepared['confirmation_digest']}
        if prepared['permission']=='red':body['verification_token']=self.bank.verify(self.w,prepared['id'],
            {'verification_code':self.credentials['human_verification_code']})['verification_token']
        self.hold_id=self.bank.confirm(self.w,prepared['id'],body)['result']['hold_id']

    def prepare_goal(self,**extra):
        payload=dict(action='create',name='本人明确的现金用途',target_minor=100000,
            due_on='2026-10-10',priority=0,funding_kind='cash',occupancy_hold_ids=[self.hold_id],
            occupancy_account_id=self.account,expected_version=self.goals.state(self.w)['version'],
            idempotency_key=uuid.uuid4().hex)
        payload.update(extra);return self.goals.prepare(self.w,payload)
    def confirm(self,draft):return self.goals.confirm(self.w,draft['id'],{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})
    def registered(self):
        draft=self.prepare_goal();receipt=self.confirm(draft)
        return draft,receipt,next(g for g in self.goals.constraints(self.w)['goals'] if g['goal_id']==receipt['goal']['id'])
    def funds(self,db,*,include_hold=True,begin=None):
        # 只为helper提供真实库读取的纯算术baseline，不调用/伪造Metrics发行器注册。
        ws=db.execute('SELECT * FROM workspaces WHERE id=?',(self.w,)).fetchone()
        accounts=[dict(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=?',(self.w,))]
        reserve=db.execute('SELECT reserve_minor FROM profiles WHERE workspace_id=?',(self.w,)).fetchone()[0]
        start=begin or ws['as_of'];end=(date.fromisoformat(start)+timedelta(days=14)).isoformat();v=str(ws['version'])
        ctx=MetricContext(self.w,'CNY','recorded_items_projection',period_start=start,period_end=end,
                          input_refs=tuple('accounts/'+a['id'] for a in accounts),source_versions=(('bank',v),))
        cash=CashSnapshot(self.w,'CNY',start,sum(a['balance_minor'] for a in accounts),0,reserve,start,end,'bank_snapshot/'+self.w,v)
        row=db.execute('SELECT * FROM fund_holds WHERE workspace_id=? AND id=?',(self.w,self.hold_id)).fetchone()
        holds=(Hold(row['id'],self.w,'CNY',row['amount_minor'],'fund_holds/'+row['id'],v,
                    release_on=(date.fromisoformat(row['expires_on'])+timedelta(days=1)).isoformat()),) if include_hold else ()
        return evaluate_funding(ctx,cash,holds=holds).metrics['current_budget']
    def verify(self,row,**fund_args):
        with self.bank.transaction() as db:
            return verify_goal_occupancy(db,self.w,self.funds(db,**fund_args),{**row,'data_scope':'recorded_items_projection'})
    def reject(self,code,callback):
        with self.assertRaises(BankError) as caught:callback()
        self.assertEqual(caught.exception.code,code)

    def test_original_goal_confirmation_binds_real_hold_account_task_and_digest(self):
        before=self.bank.customer_archive(self.w);draft=self.prepare_goal()
        self.assertEqual(draft['summary']['occupancy_hold_ids'],[self.hold_id])
        self.assertEqual(draft['occupancy_binding']['snapshots'][0]['hold']['id'],self.hold_id)
        self.assertEqual(draft['occupancy_binding']['snapshots'][0]['account']['id'],self.account)
        self.assertEqual(draft['occupancy_binding']['snapshots'][0]['task']['id'],self.task['id'])
        result=self.confirm(draft)
        self.assertEqual(self.confirm(draft),result)
        after=self.bank.customer_archive(self.w)
        self.assertEqual(after['accounts'],before['accounts']);self.assertEqual(after['records']['fund_holds'],before['records']['fund_holds'])
        self.assertEqual(self.bank.orders(self.w),[])
        row=next(g for g in self.goals.constraints(self.w)['goals'] if g['goal_id']==result['goal']['id'])
        self.assertEqual(row['already_occupied_minor'],30000);self.assertFalse(row['occupancy_verified_in_baseline'])
        projection,gaps=self.verify(row)
        self.assertEqual(gaps,[]);self.assertEqual(projection['already_occupied_minor'],30000)
        self.assertTrue(projection['occupancy_verified_in_baseline']);self.assertEqual(projection['occupancy_refs'],['fund_holds/'+self.hold_id])

    def test_saved_same_amount_or_client_verified_cannot_register_relationship(self):
        payload=dict(action='create',name='无关联现金目标',target_minor=100000,due_on='2026-10-10',priority=0,
            funding_kind='cash',expected_version=self.goals.state(self.w)['version'],idempotency_key=uuid.uuid4().hex)
        draft=self.goals.prepare(self.w,payload);receipt=self.confirm(draft)
        row=next(g for g in self.goals.constraints(self.w)['goals'] if g['goal_id']==receipt['goal']['id'])
        row.update(already_occupied_minor=30000,occupancy_refs=['fund_holds/'+self.hold_id],occupancy_verified_in_baseline=True)
        projection,gaps=self.verify(row)
        self.assertIsNone(projection['already_occupied_minor']);self.assertFalse(projection['occupancy_verified_in_baseline'])
        self.assertIn('goal_occupancy_unregistered',gaps)

    def test_correct_relationship_derives_amount_and_ignores_client_verified_fields(self):
        _,_,row=self.registered();row.update(already_occupied_minor=1,occupancy_verified_in_baseline=False)
        projection,gaps=self.verify(row);self.assertEqual(projection['already_occupied_minor'],30000)
        self.assertTrue(projection['occupancy_verified_in_baseline']);self.assertEqual(gaps,[])

    def test_duplicate_hold_cannot_credit_two_real_goals(self):
        self.registered()
        self.reject('GOAL_OCCUPANCY_REUSED',lambda:self.prepare_goal(name='另一目标'))
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM navren_goal_occupancy_links WHERE workspace_id=?',(self.w,)).fetchone()[0],1)

    def test_duplicate_claim_ref_is_preserved_for_global_c08_detection(self):
        _,_,row=self.registered();forged={**row,'goal_id':'not-the-associated-goal'}
        projection,gaps=self.verify(forged)
        self.assertEqual(projection['occupancy_refs'],row['occupancy_refs']);self.assertFalse(projection['occupancy_verified_in_baseline'])
        self.assertIsNone(projection['already_occupied_minor']);self.assertTrue(gaps)

    def test_invalid_fields_duplicate_ids_unknown_and_cross_account_rejected(self):
        for extra in ({'occupancy_hold_ids':[]},{'occupancy_hold_ids':[self.hold_id,self.hold_id]},
                      {'occupancy_hold_ids':['missing']},{'occupancy_account_id':self.other_account},
                      {'priority':None},{'funding_kind':'investment'},{'target_minor':10000}):
            with self.subTest(extra=extra):
                with self.assertRaises(BankError):self.prepare_goal(**extra)

    def test_cross_workspace_hold_is_not_accepted(self):
        other=self.bank.create_workspace()['workspace_id']
        payload=dict(action='create',name='错主体目标',target_minor=100000,due_on='2026-10-10',priority=0,funding_kind='cash',
            occupancy_hold_ids=[self.hold_id],occupancy_account_id=self.account,
            expected_version=self.goals.state(other)['version'],idempotency_key=uuid.uuid4().hex)
        self.reject('GOAL_OCCUPANCY_INVALID',lambda:self.goals.prepare(other,payload))

    def test_hold_changes_between_prepare_and_confirm_reject_without_link_or_cash_write(self):
        for field,value in (('amount_minor',31000),('expires_on','2026-10-16'),('account_id',self.other_account),
                            ('status','released'),('task_id','nonexistent')):
            with self.subTest(field=field):
                if field=='task_id':continue  # FK 本身拒绝不存在task，另一真实task见下一测试。
                draft=self.prepare_goal()
                with self.bank.transaction() as db:
                    old=db.execute('SELECT '+field+' FROM fund_holds WHERE workspace_id=? AND id=?',(self.w,self.hold_id)).fetchone()[0]
                    db.execute('UPDATE fund_holds SET '+field+'=? WHERE workspace_id=? AND id=?',(value,self.w,self.hold_id))
                self.reject('GOAL_OCCUPANCY_INVALID',lambda:self.confirm(draft))
                with self.bank.transaction() as db:db.execute('UPDATE fund_holds SET '+field+'=? WHERE workspace_id=? AND id=?',(old,self.w,self.hold_id))
                self.assertEqual(self.goals.constraints(self.w)['goals'],[])

    def test_task_snapshot_change_after_prepare_is_rejected(self):
        draft=self.prepare_goal()
        with self.bank.transaction() as db:db.execute('UPDATE orchestration_tasks SET title=? WHERE workspace_id=? AND id=?',('另一任务身份',self.w,self.task['id']))
        self.reject('GOAL_OCCUPANCY_INVALID',lambda:self.confirm(draft))

    def test_server_binding_tamper_cannot_reuse_original_confirmation_digest(self):
        draft=self.prepare_goal()
        with self.bank.transaction() as db:
            binding=json.loads(db.execute('SELECT occupancy_binding FROM navren_goal_drafts WHERE id=?',(draft['id'],)).fetchone()[0])
            binding['snapshots'][0]['hold']['amount_minor']=1
            db.execute('UPDATE navren_goal_drafts SET occupancy_binding=? WHERE id=?',(json.dumps(binding),draft['id']))
        self.reject('GOAL_CONFIRMATION_INVALID',lambda:self.confirm(draft))

    def test_link_hold_original_receipt_or_goal_version_tamper_is_unavailable(self):
        _,_,row=self.registered()
        for field,value in (('goal_version','bad'),('receipt_hash','bad'),('binding_hash','bad'),('hold_snapshot','{}')):
            with self.subTest(field=field):
                with self.bank.transaction() as db:
                    old=db.execute('SELECT '+field+' FROM navren_goal_occupancy_links WHERE workspace_id=?',(self.w,)).fetchone()[0]
                    db.execute('UPDATE navren_goal_occupancy_links SET '+field+'=? WHERE workspace_id=?',(value,self.w))
                projection,gaps=self.verify(row);self.assertIsNone(projection['already_occupied_minor']);self.assertTrue(gaps)
                with self.bank.transaction() as db:db.execute('UPDATE navren_goal_occupancy_links SET '+field+'=? WHERE workspace_id=?',(old,self.w))

    def test_historical_still_held_created_before_budget_opening_is_supported(self):
        with self.bank.transaction() as db:db.execute('UPDATE fund_holds SET created_on=? WHERE workspace_id=? AND id=?',('2026-01-01',self.w,self.hold_id))
        _,_,row=self.registered();projection,gaps=self.verify(row)
        self.assertEqual(gaps,[]);self.assertEqual(projection['already_occupied_minor'],30000)
        self.assertTrue(projection['occupancy_verified_in_baseline'])

    def test_expired_or_released_hold_cannot_obtain_baseline_credit(self):
        _,_,row=self.registered()
        with self.bank.transaction() as db:db.execute('UPDATE workspaces SET as_of=? WHERE id=?',('2026-10-18',self.w))
        projection,gaps=self.verify(row);self.assertIsNone(projection['already_occupied_minor']);self.assertTrue(gaps)

    def test_baseline_without_hold_or_wrong_opening_version_rejects_credit(self):
        _,_,row=self.registered();projection,gaps=self.verify(row,include_hold=False)
        self.assertFalse(projection['occupancy_verified_in_baseline']);self.assertTrue(gaps)
        projection,gaps=self.verify(row,begin='2026-01-01');self.assertFalse(projection['occupancy_verified_in_baseline']);self.assertTrue(gaps)
        with self.bank.transaction() as db:
            funds=self.funds(db);bad=replace(funds,source_versions=tuple((k,'wrong' if k=='fund_holds/'+self.hold_id else v) for k,v in funds.source_versions))
            projection,gaps=verify_goal_occupancy(db,self.w,bad,{**row,'data_scope':funds.data_scope})
        self.assertFalse(projection['occupancy_verified_in_baseline']);self.assertTrue(gaps)

    def test_wrong_goal_amount_priority_date_currency_scope_or_proof_cannot_credit(self):
        _,_,row=self.registered()
        for field,value in (('amount_minor',99999),('priority',1),('needed_on','2026-10-11'),('currency','USD'),
                            ('subject_id','foreign'),('input_refs',[]),('source_versions',{}),('priority',False),
                            ('source_versions',None),('input_refs',None)):
            with self.subTest(field=field):
                projection,gaps=self.verify({**row,field:value})
                self.assertIsNone(projection['already_occupied_minor']);self.assertFalse(projection['occupancy_verified_in_baseline']);self.assertTrue(gaps)

    def test_existing_confirmed_goal_can_bind_hold_by_original_update_confirmation(self):
        original=self.goals.prepare(self.w,dict(action='create',name='已确认现金目标',target_minor=100000,due_on='2026-10-10',
            priority=0,funding_kind='cash',expected_version=self.goals.state(self.w)['version'],idempotency_key=uuid.uuid4().hex))
        goal=self.confirm(original)['goal']
        update=self.goals.prepare(self.w,dict(action='update',goal_id=goal['id'],name=goal['name'],target_minor=goal['target_minor'],
            due_on=goal['due_on'],priority=goal['priority'],funding_kind='cash',occupancy_hold_ids=[self.hold_id],
            occupancy_account_id=self.account,expected_version=goal['object_version'],idempotency_key=uuid.uuid4().hex))
        self.confirm(update)
        row=next(g for g in self.goals.constraints(self.w)['goals'] if g['goal_id']==goal['id'])
        projection,gaps=self.verify(row);self.assertTrue(projection['occupancy_verified_in_baseline']);self.assertEqual(gaps,[])

    def test_two_prepared_same_hold_rounds_cannot_both_commit_credit(self):
        first=self.prepare_goal();second=self.prepare_goal(name='另一待确认目标')
        self.confirm(first);self.reject('GOAL_VERSION_CONFLICT',lambda:self.confirm(second))
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM navren_goal_occupancy_links WHERE workspace_id=?',(self.w,)).fetchone()[0],1)

    def test_released_hold_and_wrong_bank_or_scope_or_account_baseline_are_unavailable(self):
        _,_,row=self.registered()
        with self.bank.transaction() as db:
            funds=self.funds(db)
            for bad in (replace(funds,source_versions=tuple((k,'bad' if k=='bank' else v) for k,v in funds.source_versions)),
                        replace(funds,input_refs=tuple(r for r in funds.input_refs if r!='accounts/'+self.account))):
                fields,gaps=verify_goal_occupancy(db,self.w,bad,{**row,'data_scope':funds.data_scope})
                self.assertFalse(fields['occupancy_verified_in_baseline']);self.assertTrue(gaps)
            fields,gaps=verify_goal_occupancy(db,self.w,funds,{**row,'data_scope':'wrong-scope'})
            self.assertFalse(fields['occupancy_verified_in_baseline']);self.assertTrue(gaps)
            db.execute("UPDATE fund_holds SET status='released' WHERE workspace_id=? AND id=?",(self.w,self.hold_id))
        fields,gaps=self.verify(row);self.assertIsNone(fields['already_occupied_minor']);self.assertTrue(gaps)

    def test_new_goal_confirmation_changes_relation_without_touching_real_hold(self):
        _,receipt,row=self.registered();goal=receipt['goal']
        update=self.goals.prepare(self.w,dict(action='update',goal_id=goal['id'],name=goal['name'],target_minor=goal['target_minor'],
            due_on=goal['due_on'],priority=1,funding_kind='cash',expected_version=goal['object_version'],idempotency_key=uuid.uuid4().hex))
        self.confirm(update)
        projection,gaps=self.verify(row);self.assertIsNone(projection['already_occupied_minor']);self.assertTrue(gaps)
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT status FROM fund_holds WHERE workspace_id=? AND id=?',(self.w,self.hold_id)).fetchone()[0],'held')

    def test_same_db_read_verification_opens_no_connection_or_write_transaction(self):
        _,_,row=self.registered()
        with self.bank.transaction() as db:
            funds=self.funds(db);before=db.total_changes
            with (patch.object(self.bank,'connect',side_effect=AssertionError('extra_connection')),
                  patch.object(self.bank,'transaction',side_effect=AssertionError('extra_transaction'))):
                constraints=self.goals.constraints(self.w,db=db)
                projection,gaps=verify_goal_occupancy(db,self.w,funds,{**row,'data_scope':funds.data_scope})
            self.assertEqual(before,db.total_changes);self.assertTrue(projection['occupancy_verified_in_baseline']);self.assertEqual(gaps,[])
            self.assertEqual(constraints['goals'][0]['already_occupied_minor'],30000)


if __name__=='__main__':unittest.main()
