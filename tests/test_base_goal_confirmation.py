"""目标本人确认到现金用途约束的隔离服务回归；无模型、live 或现役库。"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import uuid

from financial_methods import MetricContext, money_metric
from financial_methods.allocation import AllocationGoal, arrange_funds
from financial_graph import SCHEMA as GRAPH_SCHEMA
from goal_service import GoalService, version
from simulated_bank import BankError, SimulatedBank


class GoalConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory(prefix='navren-goal-confirmation-')
        cls.network=patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden'))
        cls.network.start()
        cls.bank=SimulatedBank(Path(cls.folder.name)/'bank.sqlite3');cls.goals=GoalService(cls.bank)
        with closing(cls.bank.connect()) as db:db.executescript(GRAPH_SCHEMA)

    @classmethod
    def tearDownClass(cls):cls.network.stop();cls.folder.cleanup()

    def setUp(self):
        self.w=self.bank.create_workspace()['workspace_id']
        with self.bank.transaction() as db:db.execute('DELETE FROM wealth_goals WHERE workspace_id=?',(self.w,))

    def reject(self,code,callback):
        with self.assertRaises(BankError) as caught:callback()
        self.assertEqual(caught.exception.code,code)

    def prepare(self, **extra):
        payload={'action':'create','name':'  学费现金用途  ','target_minor':100000,'due_on':'2026-11-01',
                 'expected_version':self.goals.state(self.w)['version'],'idempotency_key':uuid.uuid4().hex,
                 'priority':0,'funding_kind':'cash',**extra}
        for key in tuple(payload):
            if payload[key]=='__missing__':payload.pop(key)
        return self.goals.prepare(self.w,payload)

    def confirm(self,draft,**extra):
        return self.goals.confirm(self.w,draft['id'],{'confirmed':True,
                                 'confirmation_digest':draft['confirmation_digest'],**extra})

    def create(self,**extra):
        draft=self.prepare(**extra);return draft,self.confirm(draft)

    def update(self,goal,**extra):
        return self.goals.prepare(self.w,{'action':'update','goal_id':goal['id'],'name':goal['name'],
            'target_minor':goal['target_minor'],'due_on':goal['due_on'],'priority':goal['priority'],
            'funding_kind':'cash','expected_version':goal['object_version'],'idempotency_key':uuid.uuid4().hex,**extra})

    def item(self,gid):return self.goals.state(self.w,gid)['goal']

    def mutate(self,draft,fn):
        with self.bank.transaction() as db:
            row=dict(db.execute('SELECT * FROM navren_goal_drafts WHERE id=?',(draft['id'],)).fetchone())
            fn(row)
            db.execute('UPDATE navren_goal_drafts SET payload=?,digest=?,result=? WHERE id=?',
                       (row['payload'],row['digest'],row['result'],draft['id']))

    def test_explicit_cash_priority_zero_is_bound_and_projected(self):
        draft,result=self.create()
        goal=self.item(result['goal']['id'])
        self.assertTrue(goal['confirmed']);self.assertTrue(goal['priority_confirmed'])
        self.assertEqual(goal['priority'],0);self.assertEqual(goal['funding_kind'],'cash')
        self.assertEqual(goal['confirmed_fields'],['amount_minor','needed_on','priority','funding_kind'])
        self.assertEqual(goal['reasons'],[])
        self.assertIn('goal_confirmation/'+self.w+'/'+draft['id'],goal['input_refs'])
        self.assertEqual(result['goal'],goal)
        row=self.goals.constraints(self.w)['goals'][0]
        self.assertTrue(row['confirmed']);self.assertEqual(row['subject_id'],self.w)
        self.assertEqual(row['amount_minor'],100000);self.assertEqual(row['needed_on'],'2026-11-01')
        self.assertIn('product_key',row);self.assertIsNone(row['product_key']);self.assertIsNone(row['data_scope'])

    def test_seed_details_and_priority_do_not_prove_confirmation(self):
        seeded=self.bank.create_workspace()['workspace_id']
        with self.bank.transaction() as db:
            db.execute('UPDATE wealth_goals SET details=? WHERE workspace_id=?',('{"user_confirmed":true}',seeded))
        for item in self.goals.state(seeded)['items']:
            self.assertFalse(item['confirmed']);self.assertFalse(item['priority_confirmed'])
        for item in self.goals.constraints(seeded)['goals']:
            self.assertFalse(item['confirmed']);self.assertIsNone(item['priority'])
            self.assertNotIn('product_key',item)

    def test_default_three_is_not_explicit_priority(self):
        _,result=self.create(priority='__missing__')
        goal=self.item(result['goal']['id'])
        self.assertEqual(goal['priority'],3);self.assertFalse(goal['confirmed'])
        self.assertFalse(goal['priority_confirmed']);self.assertIn('goal_priority_unconfirmed',goal['reasons'])
        constraint=self.goals.constraints(self.w)['goals'][0]
        self.assertIsNone(constraint['priority']);self.assertNotIn('product_key',constraint)

    def test_missing_cash_use_cannot_supply_default_product_key(self):
        _,result=self.create(funding_kind='__missing__')
        goal=self.item(result['goal']['id']);self.assertFalse(goal['confirmed'])
        self.assertTrue(goal['priority_confirmed']);self.assertIn('goal_cash_use_unconfirmed',goal['reasons'])
        self.assertNotIn('product_key',self.goals.constraints(self.w)['goals'][0])

    def test_invalid_priorities_and_unknown_funding_kind_do_not_prepare(self):
        for priority in (True,False,-1,None,1.5,'1',2**63):
            with self.subTest(priority=priority):self.reject('GOAL_INVALID',lambda:self.prepare(priority=priority))
        for kind in ('investment',None,True):
            with self.subTest(kind=kind):self.reject('GOAL_INVALID',lambda:self.prepare(funding_kind=kind))

    def test_maximum_storage_priority_is_valid_without_business_bucket_limit(self):
        _,result=self.create(priority=2**63-1)
        self.assertEqual(result['goal']['priority'],2**63-1)
        self.assertTrue(self.item(result['goal']['id'])['confirmed'])

    def test_confirmation_cannot_add_or_change_unbound_fields(self):
        draft=self.prepare()
        self.reject('CONFIRMATION_REQUIRED',lambda:self.confirm(draft,priority=1))
        self.reject('CONFIRMATION_REQUIRED',lambda:self.confirm(draft,funding_kind='investment'))
        self.reject('GOAL_INVALID',lambda:self.confirm(draft,priority=True))
        result=self.confirm(draft,priority=0,funding_kind='cash');self.assertTrue(result['goal']['confirmed'])
        missing=self.prepare(priority='__missing__',funding_kind='__missing__')
        self.reject('CONFIRMATION_REQUIRED',lambda:self.confirm(missing,priority=3))
        self.reject('CONFIRMATION_REQUIRED',lambda:self.confirm(missing,funding_kind='cash'))

    def test_priority_is_in_object_version_and_stale_prepare_cannot_overwrite(self):
        _,result=self.create();old=result['goal'];draft=self.update(old,priority=2)
        with self.bank.transaction() as db:db.execute('UPDATE wealth_goals SET priority=1 WHERE id=?',(old['id'],))
        fresh=self.item(old['id']);self.assertNotEqual(old['object_version'],fresh['object_version'])
        self.assertFalse(fresh['confirmed'])
        self.reject('GOAL_VERSION_CONFLICT',lambda:self.confirm(draft))

    def test_new_priority_confirmation_changes_actual_allocation_order(self):
        _,a=self.create(name='先安排甲',priority=2)
        _,b=self.create(name='先安排乙',priority=1)
        # 另一目标的提交不能撤销此前本人确认；当前读取 revision 另行绑定。
        self.assertTrue(self.item(a['goal']['id'])['confirmed'])
        ctx=MetricContext(self.w,'CNY','isolated_cash_uses',period_start='2026-10-03',period_end='2026-12-02',
                          input_refs=('isolated_funds',),source_versions=(('isolated_funds','1'),))
        funds=money_metric(ctx,'budget','floor_surplus','funds.current_budget/1',150000)
        constraints=self.goals.constraints(self.w)
        goals=tuple(AllocationGoal(**{**g,'data_scope':ctx.data_scope,'source_versions':tuple(g['source_versions'].items()),
            'input_refs':tuple(g['input_refs']),'occupancy_refs':tuple(g['occupancy_refs'])}) for g in constraints['goals'])
        arranged=arrange_funds(funds,goals)
        plan=next(p for p in arranged['plans'] if p['plan_id']=='confirmed_uses')
        self.assertEqual([line['goal_id'] for line in plan['allocations']],[b['goal']['id']])
        self.assertEqual(plan['cash_partition_difference_minor'],0)
        update=self.update(a['goal'],priority=0);new=self.confirm(update)
        self.assertEqual(new['goal']['priority'],0);self.assertTrue(self.item(new['goal']['id'])['confirmed'])
        updated=self.goals.constraints(self.w)
        goals=tuple(AllocationGoal(**{**g,'data_scope':ctx.data_scope,'source_versions':tuple(g['source_versions'].items()),
            'input_refs':tuple(g['input_refs']),'occupancy_refs':tuple(g['occupancy_refs'])}) for g in updated['goals'])
        plan=next(p for p in arrange_funds(funds,goals)['plans'] if p['plan_id']=='confirmed_uses')
        self.assertEqual([line['goal_id'] for line in plan['allocations']],[a['goal']['id']])

    def test_update_missing_explicit_priority_and_use_does_not_reuse_old_proof(self):
        _,result=self.create();goal=result['goal']
        draft=self.goals.prepare(self.w,{'action':'update','goal_id':goal['id'],'name':'更新后的现金用途',
            'target_minor':goal['target_minor'],'due_on':goal['due_on'],'expected_version':goal['object_version'],
            'idempotency_key':uuid.uuid4().hex})
        self.confirm(draft);current=self.item(goal['id'])
        self.assertEqual(current['priority'],0);self.assertFalse(current['confirmed'])
        self.assertFalse(current['priority_confirmed']);self.assertIsNone(current['funding_kind'])

    def test_legacy_receipt_without_priority_keeps_idempotency_but_is_incomplete(self):
        draft,result=self.create(priority='__missing__',funding_kind='__missing__')
        legacy=deepcopy(result);goal=legacy['goal']
        for key in ('priority','confirmed','confirmed_fields','funding_kind','priority_confirmed','input_refs','source_versions','reasons'):
            goal.pop(key,None)
        core={k:goal[k] for k in ('id','name','target_minor','saved_minor','due_on','status')}
        goal['object_version']=version(core)
        self.mutate(draft,lambda row:row.update(result=json.dumps(legacy)))
        self.assertEqual(self.confirm(draft),legacy)
        state=self.item(goal['id']);self.assertFalse(state['confirmed'])
        self.assertIn('goal_priority_unconfirmed',state['reasons']);self.assertIn('goal_cash_use_unconfirmed',state['reasons'])

    def test_committed_payload_digest_result_and_receipt_tamper_fail_closed(self):
        for location in ('payload','payload_json','result_json','digest','receipt_id','receipt_state',
                         'receipt_action','receipt_goal','workspace','goal_version','goal_amount',
                         'source_scope','funding_state','saved_minor_basis'):
            with self.subTest(location=location):
                draft,result=self.create()
                def alter(row):
                    payload=json.loads(row['payload']);stored=json.loads(row['result'])
                    if location=='payload':payload['priority']=7;row['payload']=json.dumps(payload)
                    elif location=='digest':row['digest']='bad-digest'
                    elif location=='workspace':stored['workspace_id']='foreign-workspace'
                    elif location=='goal_version':stored['goal']['object_version']='bad-version'
                    elif location=='goal_amount':stored['goal']['target_minor']+=1
                    elif location=='payload_json':row['payload']='malformed-json'
                    elif location in ('result_json','source_scope','funding_state','saved_minor_basis'):
                        if location!='result_json':stored['goal'][location]='real-bank-executed-frozen'
                    else:stored['receipt']['goal_id' if location=='receipt_goal' else location.removeprefix('receipt_')]='bad-value'
                    row['result']='malformed-json' if location=='result_json' else json.dumps(stored)
                self.mutate(draft,alter)
                self.assertFalse(self.item(result['goal']['id'])['confirmed'])
                self.assertFalse(next(g for g in self.goals.constraints(self.w)['goals'] if g['goal_id']==result['goal']['id'])['confirmed'])
                shown=self.goals.draft(self.w,draft['id'])
                self.assertIsNone(shown['result']);self.assertEqual(shown['result_error'],'GOAL_CONFIRMATION_INVALID')

    def test_historical_projection_metadata_cannot_be_forged_on_replay(self):
        for field,value in (('confirmed',True),('priority_confirmed',True),('funding_kind','cash'),
                            ('confirmed_fields',['amount_minor','needed_on','priority','funding_kind']),
                            ('input_refs',['forged']),('source_versions',{'forged':'1'}),('reasons',[])):
            with self.subTest(field=field):
                draft,result=self.create(priority='__missing__',funding_kind='__missing__')
                def alter(row):
                    receipt=json.loads(row['result']);receipt['goal'][field]=value;row['result']=json.dumps(receipt)
                self.mutate(draft,alter)
                self.assertFalse(self.item(result['goal']['id'])['confirmed'])
                self.reject('GOAL_CONFIRMATION_INVALID',lambda:self.confirm(draft))
                shown=self.goals.draft(self.w,draft['id'])
                self.assertIsNone(shown['result']);self.assertEqual(shown['result_error'],'GOAL_CONFIRMATION_INVALID')
                shown=next(d for d in self.goals.state(self.w)['drafts'] if d['id']==draft['id'])
                self.assertIsNone(shown['result']);self.assertEqual(shown['result_error'],'GOAL_CONFIRMATION_INVALID')

    def test_prepare_payload_change_without_digest_cannot_commit(self):
        draft=self.prepare()
        def alter(row):
            payload=json.loads(row['payload']);payload['target_minor']+=1;row['payload']=json.dumps(payload)
        self.mutate(draft,alter)
        self.reject('GOAL_CONFIRMATION_INVALID',lambda:self.confirm(draft))
        self.assertEqual(self.goals.state(self.w)['items'],[])

    def test_current_amount_date_or_priority_change_invalidates_confirmation(self):
        for field,value in (('target_minor',120000),('due_on','2026-11-02'),('priority',4),('saved_minor',1)):
            with self.subTest(field=field):
                _,result=self.create()
                with self.bank.transaction() as db:db.execute('UPDATE wealth_goals SET '+field+'=? WHERE id=?',(value,result['goal']['id']))
                current=self.item(result['goal']['id']);self.assertFalse(current['confirmed'])
                row=next(g for g in self.goals.constraints(self.w)['goals'] if g['goal_id']==result['goal']['id'])
                self.assertIsNone(row['amount_minor']);self.assertIsNone(row['priority'])

    def test_prepared_cancelled_expired_cross_workspace_and_authorization_guards(self):
        draft=self.prepare();self.assertEqual(self.goals.constraints(self.w)['goals'],[])
        other=self.bank.create_workspace()['workspace_id']
        self.reject('GOAL_DRAFT_NOT_FOUND',lambda:self.goals.confirm(other,draft['id'],
                    {'confirmed':True,'confirmation_digest':draft['confirmation_digest']}))
        self.goals.cancel(self.w,draft['id'],{})
        self.reject('GOAL_DRAFT_CANCELLED',lambda:self.confirm(draft))
        expired=self.prepare()
        with patch.object(self.bank,'clock',return_value=expired['expires_at']):
            self.reject('GOAL_DRAFT_EXPIRED',lambda:self.confirm(expired))
        changed=self.prepare()
        with self.bank.transaction() as db:
            db.execute('INSERT INTO conversation_privacy_versions VALUES(?,1) ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1',(self.w,))
        self.reject('GOAL_VERSION_CONFLICT',lambda:self.confirm(changed))

    def test_old_committed_receipt_replay_after_update_stays_original(self):
        first,old=self.create();second=self.update(old['goal'],name='新版目标',priority=2)
        self.confirm(second);self.assertEqual(self.confirm(first),old)
        self.assertEqual(self.item(old['goal']['id'])['priority'],2)
        self.reject('GOAL_ALREADY_COMMITTED',lambda:self.goals.cancel(self.w,first['id'],{}))

    def test_delete_replay_and_completed_goals_are_not_new_uses(self):
        _,result=self.create(status='completed')
        self.assertFalse(self.item(result['goal']['id'])['confirmed'])
        self.assertEqual(self.goals.constraints(self.w)['goals'],[])
        goal=self.item(result['goal']['id'])
        draft=self.goals.prepare(self.w,{'action':'delete','goal_id':goal['id'],'expected_version':goal['object_version'],
                                      'idempotency_key':uuid.uuid4().hex})
        deleted=self.confirm(draft);self.assertIsNone(deleted['goal'])
        self.assertEqual(self.confirm(draft),deleted)

    def test_confirmation_lookup_is_not_limited_to_twenty_display_drafts(self):
        _,result=self.create();gid=result['goal']['id']
        for i in range(21):self.prepare(name='未确认草案'+str(i))
        state=self.goals.state(self.w);self.assertEqual(len(state['drafts']),20)
        self.assertTrue(self.item(gid)['confirmed'])

    def test_readonly_constraints_in_existing_transaction_open_no_connection_or_write(self):
        _,result=self.create()
        with self.bank.transaction() as db:
            before=db.total_changes
            with (patch.object(self.bank,'connect',side_effect=AssertionError('extra_connection_forbidden')),
                  patch.object(self.bank,'transaction',side_effect=AssertionError('nested_write_forbidden'))):
                constraints=self.goals.constraints(self.w,db)
            self.assertEqual(db.total_changes,before)
        self.assertTrue(constraints['goals'][0]['confirmed'])
        self.assertEqual(constraints['goals'][0]['already_occupied_minor'],0)
        self.assertEqual(constraints['goals'][0]['occupancy_refs'],[])
        self.assertFalse(constraints['goals'][0]['occupancy_verified_in_baseline'])

    def test_prepare_idempotency_and_digest_confirmation_remain_strict(self):
        draft=self.prepare(priority=1);payload=draft['summary']
        self.assertEqual(self.goals.prepare(self.w,payload),draft)
        self.reject('IDEMPOTENCY_CONFLICT',lambda:self.goals.prepare(self.w,{**payload,'priority':2}))
        for priority in (True,1.0):
            self.reject('GOAL_INVALID',lambda:self.goals.prepare(self.w,{**payload,'priority':priority}))
        self.reject('CONFIRMATION_REQUIRED',lambda:self.goals.confirm(self.w,draft['id'],
                    {'confirmed':False,'confirmation_digest':draft['confirmation_digest']}))
        self.reject('CONFIRMATION_REQUIRED',lambda:self.goals.confirm(self.w,draft['id'],
                    {'confirmed':True,'confirmation_digest':'wrong'}))


if __name__=='__main__':unittest.main()
