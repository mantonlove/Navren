import json
import hashlib
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from finance_gateway import FinanceGateway
from financial_graph import classify, liquidity_scenario, local_day
from simulated_bank import BankError


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=FinanceGateway(Path(self.tmp.name)/'bank');self.w,_=self.g.create_session()
    def query(self,**p):return self.g.financial_graph.query(self.w,p)
    def test_subgraphs_have_complete_lineage_and_stable_ids(self):
        r=self.query();s=self.query();self.assertEqual(r['version'],s['version'])
        self.assertEqual([n['id'] for n in r['nodes']],[n['id'] for n in s['nodes']]);self.assertEqual(r['view'],'overview');self.assertGreater(len(r['nodes']),12)
        ids={n['id'] for n in r['nodes']}
        for e in r['links']:
            self.assertIn(e['source'],ids);self.assertIn(e['target'],ids);self.assertTrue(e['evidence_refs']);self.assertIn('period',e)
        for c in r['calculations']:
            current=[r for r in c['input_refs'] if not r.get('comparison_period')]
            self.assertEqual(c['amount_minor'],sum(-r['amount_minor'] if r['kind']=='refund' else r['amount_minor'] for r in current))
    def test_previous_period_inputs_are_not_lost(self):
        r=self.query(view='funds',**{'from':'2026-08-01','to':'2026-08-31'})
        for c in r['calculations']:
            if c['change_minor'] is not None:
                refs=[r for r in c['input_refs'] if r.get('comparison_period')]
                self.assertTrue(refs)
                self.assertEqual(c['change_minor'],c['amount_minor']-c['previous_amount_minor'])
    def test_types_exclude_internal_transfer_and_principal(self):
        for kind in ('purchase','redeem','investment'):
            self.assertEqual(classify({'kind':kind}),'investment_principal')
        for kind in ('card_repayment','loan_principal','loan_repayment'):
            self.assertEqual(classify({'kind':kind}),'principal')
        self.assertEqual(classify({'kind':'income','internal_transfer':True}),'internal_transfer')
        self.assertEqual(classify({'kind':'income'}),'income')
        self.assertEqual(classify({'kind':'refund'}),'refund')
        self.assertEqual(classify({'kind':'unknown'}),'unknown')
    def test_timezone_and_period_are_explicit(self):
        self.assertEqual(str(local_day('2026-09-30T18:00:00Z')),'2026-10-01')
        r=self.query(**{'from':'2026-09-01','to':'2026-09-30'})
        self.assertEqual(r['period']['to_exclusive'],'2026-10-01')
        for p in ({'from':'oops'},{'from':'2020-01-01'},{'source':'both'},{'workspace_id':'another'}):
            with self.assertRaises(BankError):self.g.financial_graph.query(self.w,p)
    def test_imported_source_does_not_mix_bank_contracts_or_balances(self):
        preview=self.g.statements.preview(self.w,'流水号,日期,商户,金额,类型\nx,2026-09-20,商户,10.00,支出\n')
        self.g.statements.commit(self.w,{'batch_id':preview['batch_id'],'confirmed':True})
        r=self.query(source='imported',view='records')
        self.assertFalse(any(n['kind'] in {'account','contract','goal','event'} for n in r['nodes']))
        self.assertIsNone(r['timeline']);self.assertTrue(r['coverage']['gaps'])
    def test_risk_gaps_do_not_invent_exposures(self):
        r=self.query(view='risk');self.assertEqual(r['view'],'wealth');self.assertFalse(any(n.get('role')=='industry_exposure'for n in r['nodes']));self.assertTrue(r['coverage']['gaps'])
        self.assertIsNone(r['insights'][0]['amount_minor'])
    def test_annual_contract_cost_not_faked_as_actual(self):
        r=self.query(view='cost');self.assertTrue(r['insights'])
        for i in [x for x in r['insights']if 'annual_hypothesis_minor'in x]:
            self.assertIn('annual_hypothesis_minor',i)
            self.assertIn('年度化是假设',i['notice'])
    def test_saved_goal_does_not_create_allocation(self):
        r=self.query(view='goals')
        self.assertTrue(r['insights']);self.assertTrue(all(i['amount_minor']==0 for i in r['insights']if '资金约束' in i['title']))
        self.assertFalse(any(e['predicate']=='本人确认规划分配' for e in r['links']))
    def test_user_relations_and_deletion_invalidate_persisted_details(self):
        self.g.memories.set_enabled(self.w,{'enabled':True})
        a=self.g.memories.create(self.w,{'category':'goal','value':'准备留学学费'})['items'][0]['node_id']
        b=self.g.memories.create(self.w,{'category':'liquidity_preference','value':'应急资金先保留'})['items'][0]['node_id']
        self.g.memories.add_relation(self.w,a,{'target_node_id':b,'relation':'depends_on'})
        r=self.query(view='records');self.assertTrue(any(e['predicate']=='depends_on' for e in r['links']))
        self.g.memories.delete(self.w,a,{})
        with closing(self.g.bank.connect()) as db:
            for table in ('graph_entities','graph_assertions','graph_evidence_links','graph_calculations','graph_scenarios'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE workspace_id=?',(self.w,)).fetchone()[0],0)
        r=self.query(view='records');self.assertNotIn('准备留学学费',json.dumps(r,ensure_ascii=False))
    def test_disabled_agent_does_not_read_user_memories(self):
        self.g.memories.create(self.w,{'category':'goal','value':'准备留学学费'})
        r=self.g.financial_graph.query(self.w,{'view':'records'},for_agent=True)
        self.assertFalse(any(n['kind']=='memory' for n in r['nodes']))
    def test_question_retrieval_is_bounded_and_changes_with_question(self):
        self.g.memories.set_enabled(self.w,{'enabled':True})
        for i in range(14):self.g.memories.create(self.w,{'category':'goal','value':f'计划{i}号项目'})
        self.g.memories.create(self.w,{'category':'goal','value':'留学学费安排'})
        r=self.g.memories.read_relevant(self.w,'留学学费',workflow='cashflow')
        self.assertEqual([n.value for n in r.nodes],['留学学费安排'])
    def test_source_refs_are_workspace_scoped(self):
        r=self.query(view='records');ref=next(n for n in r['nodes'] if n['kind']=='account')['evidence_refs'][0]
        other,_=self.g.create_session()
        with self.assertRaises(BankError):self.g.financial_graph.evidence(other,{'source':ref['source_scope'],'table':ref['source_table'],'id':ref['source_id']})
        with self.assertRaises(BankError):self.g.financial_graph.evidence(self.w,{'source':'simulation','table':'workspaces','id':self.w})
        with self.assertRaises(BankError):self.g.financial_graph.query(other,{'node_id':next(n for n in r['nodes'] if n['kind']=='account')['id']})
    def test_planning_allocation_is_confirmed_scoped_and_bounded(self):
        archive=self.g.bank.customer_archive(self.w);account=next(a for a in archive['accounts'] if '日常' in a['name']);goal=archive['records']['wealth_goals'][0]
        with self.g.bank.transaction() as db:db.execute('UPDATE accounts SET balance_minor=5000000 WHERE id=?',(account['id'],))
        before=self.g.bank.customer_archive(self.w)['accounts'];r=self.query()
        p={'account_id':account['id'],'goal_id':goal['id'],'amount_minor':10000,'expected_version':r['version'],'confirmed':False}
        with self.assertRaises(BankError):self.g.financial_graph.allocate(self.w,p)
        p['confirmed']=True;r=self.g.financial_graph.allocate(self.w,p)
        self.assertEqual(before,self.g.bank.customer_archive(self.w)['accounts'])
        self.assertTrue(any(e['predicate']=='本人确认规划分配' for e in r['links']))
        with self.assertRaises(BankError):self.g.financial_graph.allocate(self.w,p)
        p['expected_version']=r['version'];p['amount_minor']=999999999
        with self.assertRaises(BankError):self.g.financial_graph.allocate(self.w,p)
    def test_scenario_uses_timeline_and_never_updates_accounts(self):
        archive=self.g.bank.customer_archive(self.w);r=self.query();a=archive['accounts'][0]
        s=self.g.financial_graph.scenario(self.w,{'account_id':a['id'],'expected_version':r['version']})
        self.assertEqual(s['baseline']['minimum_available_minor'],next(x for x in self.g.cashflow_timeline(self.w)['accounts'] if x['account_id']==a['id'])['minimum_available_after_confirmed_minor'])
        self.assertEqual(archive['accounts'],self.g.bank.customer_archive(self.w)['accounts'])
        with self.assertRaises(BankError):self.g.financial_graph.scenario(self.w,{'account_id':a['id'],'expected_version':'stale'})

    def test_relation_can_be_queried_and_is_workspace_scoped(self):
        data=self.query(view='cost');edge=data['links'][0]
        result=self.query(view='cost',edge_id=edge['id'])
        self.assertEqual(result['selected_relation']['id'],edge['id'])
        self.assertTrue(result['evidence_refs'])
        other,_=self.g.create_session()
        with self.assertRaises(BankError):self.g.financial_graph.query(other,{'edge_id':edge['id']})
    def test_deleted_profile_invalidates_inflight_context_and_history(self):
        from agent_runtime.bank_tools import BankTools
        from agent_runtime.conversation import ConversationAgent
        self.g.memories.set_enabled(self.w,{'enabled':True})
        candidate=self.g.personal_graph.remember(self.w,'我每月参加模拟炒股竞赛','r',{'category':'life','value':'每月参加模拟炒股竞赛','quote':'我每月参加模拟炒股竞赛'})
        oid=candidate['observation_id']
        self.g.personal_graph.update(self.w,oid,{'decision':'confirm',
            'confirmation_token':candidate['confirmation_token'],'expected_revision':candidate['expected_revision']})
        tools=BankTools(self.g,self.w,'r2')
        revision=self.g.memories.state(self.w)['personal_revision']
        self.assertTrue(ConversationAgent._save(self.g,self.w,[],'我的偏好',
            {'reply':'每月参加模拟炒股竞赛','bank_response_id':'saved-turn'},tools.privacy_revision,revision))
        self.g.personal_graph.update(self.w,oid,{'decision':'delete','expected_revision':revision})
        with self.assertRaises(BankError):tools.fresh()
        self.assertEqual(ConversationAgent._history(self.g,self.w),[])
        self.assertFalse(ConversationAgent._save(self.g,self.w,[],'旧轮次',
            {'reply':'旧偏好','bank_response_id':'stale-turn'},tools.privacy_revision,revision))
    def test_candidate_capture_does_not_invalidate_current_facts(self):
        from agent_runtime.bank_tools import BankTools
        self.g.memories.set_enabled(self.w,{'enabled':True});tools=BankTools(self.g,self.w,'r')
        self.g.personal_graph.remember(self.w,'我想准备学费','r',{'category':'goal','value':'准备学费','quote':'我想准备学费'})
        tools.fresh()
    def test_memory_original_words_and_held_principal_are_preserved(self):
        self.g.memories.create(self.w,{'category':'goal','value':'参加模拟炒股竞赛'})
        r=self.query(view='records');self.assertTrue(any(n['label']=='参加模拟炒股竞赛' for n in r['nodes']))
        holdings=[n for n in r['nodes'] if n['kind']=='holding'];self.assertTrue(holdings)
        for n in holdings:
            self.assertIn('未计入未来到账',n['notice']);ref=n['evidence_refs'][0]
            self.assertEqual(self.g.financial_graph.evidence(self.w,{'source':ref['source_scope'],'table':ref['source_table'],'id':ref['source_id']})['record']['remaining_minor'],n['amount_minor'])
    def test_calendar_relations_survive_without_deleted_memory(self):
        self.g.memories.set_enabled(self.w,{'enabled':True})
        mid=self.g.memories.create(self.w,{'category':'goal','value':'进修计划'})['items'][0]['node_id']
        draft=self.g.calendar.events.prepare(self.w,{'action':'create','event':{'title':'课程报名','kind':'life_event','start_on':'2026-10-03','linked_memory_ids':[mid]}})
        self.g.calendar.events.confirm(self.w,draft['draft_id'],{'confirmed':True})
        r=self.query(view='records');self.assertTrue(any(e['predicate']=='本人关联计划' for e in r['links']))
        self.g.memories.delete(self.w,mid,{})
        r=self.query(view='records');self.assertTrue(any(n['label']=='课程报名' for n in r['nodes']));self.assertFalse(any(e['predicate']=='本人关联计划' for e in r['links']))
    def test_scenario_range_does_not_omit_calendar_plans(self):
        a=self.g.overview(self.w)['accounts'][0];version=self.query()['version']
        with self.assertRaises(BankError):self.g.financial_graph.scenario(self.w,{'account_id':a['id'],'expected_version':version,'horizon_days':365})
    def test_future_edge_period_is_actual_date_not_historical_filter(self):
        r=self.query(view='liquidity',**{'from':'2026-09-01','to':'2026-09-30'})
        for e in r['links']:
            if e.get('occurred_at'):self.assertEqual(e['period']['from'],e['occurred_at'])
    def test_future_occurrences_retain_dates_certainty_and_signed_cash_effect(self):
        from cashflow_timeline import build_cashflow_timeline
        archive=self.g.bank.customer_archive(self.w)
        account=next(a for a in archive['accounts'] if a['id'].endswith('-main'))
        asof=date.fromisoformat(archive['as_of'])
        archive['records']['recorded_forecasts']=[{'obligation_id':'periodic-evidence','category':'个人日常支出',
            'scope':'personal','amount_minor':12000,'frequency':'monthly','next_due_on':archive['as_of'],
            'status':'forecast_not_paid'}]
        archive['records']['orders'].append({'id':'settlement-evidence','status':'pending_settlement',
            'account_id':account['id'],'amount_minor':25000,'settlement_date':(asof+timedelta(days=7)).isoformat()})
        archive['records']['fund_holds'].append({'id':'release-evidence','status':'held',
            'account_id':account['id'],'amount_minor':500,'expires_on':archive['as_of']})
        expected=build_cashflow_timeline(archive,90,self.w)
        pristine=deepcopy(expected)
        with patch.object(self.g,'cashflow_timeline',return_value=expected):
            result=self.query(view='plans',**{'from':'2026-09-01','to':'2026-09-30'})
        self.assertEqual(result['timeline'],pristine)
        events={e['id']:e for slot in expected['timeline'] for e in slot['events']}
        projected=[n for n in result['nodes'] if n['id'].startswith('event:') and n['id'][6:] in events]
        self.assertEqual(len(projected),len(events))
        forecasts=[n for n in projected if n['source_id']=='periodic-evidence']
        self.assertGreaterEqual(len(forecasts),3)
        self.assertEqual(len({n['id'] for n in forecasts}),len(forecasts))
        self.assertEqual(len({n['date'] for n in forecasts}),len(forecasts))
        self.assertEqual(len({n['label'] for n in forecasts}),1)
        for n in projected:
            original=events[n['id'][6:]]
            for field in ('account_id','date','certainty','cash_delta_minor','amount_minor','currency','source_table'):
                self.assertEqual(n[field],original[field])
            self.assertEqual(n['source_id'],original['evidence_id'])
            self.assertEqual(n['event_type'],original['kind'])
            self.assertEqual(n['derivation_type'],original['certainty'])
            self.assertEqual(n['status'],'candidate' if original['certainty']=='candidate' else 'active')
            self.assertEqual(n['evidence_refs'][0]['occurred_at'],original['date'])
        self.assertTrue(any(n['cash_delta_minor']>0 and n['certainty']=='confirmed' for n in projected))
        self.assertTrue(any(n['cash_delta_minor']==0 and n['certainty']=='confirmed' for n in projected))
        self.assertTrue(all(n['cash_delta_minor']==-12000 and n['certainty']=='candidate' for n in forecasts))
    def test_public_concepts_are_separate_from_personal_exposure(self):
        r=self.query(view='risk');self.assertTrue(r['related_knowledge']);self.assertFalse(any(n.get('role')=='industry_exposure'for n in r['nodes']))


class RecurringEvidenceTests(unittest.TestCase):
    """临时注册表和只读历史副本；不读取或修改现有演示数据库。"""
    def setUp(self):
        from demo_accounts import SOURCE
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=FinanceGateway(Path(self.tmp.name)/'bank')
        self.w,_=self.g.create_session();self.other,_=self.g.create_session()
        self.history=Path(self.tmp.name)/'history.sqlite3'
        self.forecast={'obligation_id':'own-periodic','user_id':'tycoon','category':'个人日常支出',
            'scope':'personal','amount_minor':12000,'frequency':'monthly','next_due_on':'2026-10-08',
            'status':'forecast_not_paid','source':SOURCE}
        self.other_forecast={**self.forecast,'obligation_id':'other-periodic','user_id':'grain_farmer'}
        with closing(sqlite3.connect(self.history)) as db:
            db.executescript('''CREATE TABLE recurring_obligations(obligation_id TEXT PRIMARY KEY,user_id TEXT,
                category TEXT,scope TEXT,amount_minor INTEGER,frequency TEXT,next_due_on TEXT,status TEXT,source TEXT);
                CREATE TABLE cash_transactions(transaction_id TEXT,user_id TEXT,occurred_at TEXT);''')
            for row in (self.forecast,self.other_forecast):
                db.execute('INSERT INTO recurring_obligations VALUES(?,?,?,?,?,?,?,?,?)',tuple(row.values()))
            db.commit()
        digest=hashlib.sha256(self.history.read_bytes()).hexdigest()
        with self.g.bank.transaction() as db:
            db.execute('''CREATE TABLE navren_demo_accounts(user_id TEXT PRIMARY KEY,workspace_id TEXT UNIQUE,
                display_name TEXT,as_of TEXT,source_version TEXT,history_path TEXT,history_sha256 TEXT,
                source_snapshot TEXT,account_mapping TEXT,status TEXT,import_version TEXT)''')
            for workspace,user_id,forecast in ((self.w,'tycoon',self.forecast),(self.other,'grain_farmer',self.other_forecast)):
                snapshot={'recurring_obligations':[forecast],'latest_balances':[],
                          'start_date':'2016-10-04','end_date':'2026-10-03'}
                db.execute('INSERT INTO navren_demo_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (user_id,workspace,user_id,'2026-10-08','fixture-v1',str(self.history),digest,
                     json.dumps(snapshot),'{}','ready','fixture-v1'))
    def evidence(self,workspace=None,source_id=None):
        return self.g.financial_graph.evidence(workspace or self.w,
            {'source':'simulation','table':'recurring_obligations','id':source_id or self.forecast['obligation_id']})
    def set_forecasts(self,forecasts):
        with self.g.bank.transaction() as db:
            snapshot=json.loads(db.execute('SELECT source_snapshot FROM navren_demo_accounts WHERE workspace_id=?',(self.w,)).fetchone()[0])
            snapshot['recurring_obligations']=forecasts
            db.execute('UPDATE navren_demo_accounts SET source_snapshot=? WHERE workspace_id=?',(json.dumps(snapshot),self.w))
    def test_current_workspace_forecast_evidence_is_readonly_and_bounded(self):
        before=self.history.read_bytes()
        record=self.evidence()['record']
        self.assertEqual(record,{'id':'own-periodic','category':'个人日常支出','amount_minor':12000,
            'frequency':'monthly','next_due_on':'2026-10-08','status':'forecast_not_paid'})
        self.assertEqual(self.history.read_bytes(),before)
        self.assertNotIn('user_id',record);self.assertNotIn('workspace_id',record);self.assertNotIn('scope',record)
        with self.assertRaises(BankError) as error:
            self.g.financial_graph.evidence(self.w,{'source':'simulation','table':'navren_demo_accounts','id':'tycoon'})
        self.assertEqual(error.exception.code,'GRAPH_EVIDENCE')
    def test_forecast_cannot_be_read_across_workspaces_even_with_copied_candidate(self):
        with self.assertRaises(BankError) as error:self.evidence(self.other)
        self.assertEqual(error.exception.code,'GRAPH_NOT_FOUND')
        self.set_forecasts([self.forecast,self.other_forecast])
        with self.assertRaises(BankError) as error:self.evidence(source_id='other-periodic')
        self.assertEqual(error.exception.code,'GRAPH_NOT_FOUND')
        self.assertEqual(self.evidence(self.other,'other-periodic')['record']['id'],'other-periodic')
    def test_removed_or_paid_forecast_does_not_reappear_from_history(self):
        for forecasts in ([],[{**self.forecast,'status':'paid'}]):
            with self.subTest(forecasts=forecasts):
                self.set_forecasts(forecasts)
                with self.assertRaises(BankError) as error:self.evidence()
                self.assertEqual(error.exception.code,'GRAPH_NOT_FOUND')
    def test_deleted_original_is_rejected_even_if_candidate_snapshot_remains(self):
        with closing(sqlite3.connect(self.history)) as db:
            db.execute('DELETE FROM recurring_obligations WHERE obligation_id=?',(self.forecast['obligation_id'],));db.commit()
        digest=hashlib.sha256(self.history.read_bytes()).hexdigest()
        with self.g.bank.transaction() as db:
            db.execute('UPDATE navren_demo_accounts SET history_sha256=? WHERE workspace_id=?',(digest,self.w))
        with self.assertRaises(BankError) as error:self.evidence()
        self.assertEqual(error.exception.code,'GRAPH_NOT_FOUND')
    def test_explicitly_empty_archive_does_not_fall_back_to_old_demo_forecast(self):
        with patch.object(self.g.bank,'customer_archive',return_value={'accounts':[],
                           'records':{'recurring_obligations':[]}}):
            with self.assertRaises(BankError) as error:self.evidence()
        self.assertEqual(error.exception.code,'GRAPH_NOT_FOUND')


class Counterexamples(unittest.TestCase):
    def timeline(self):
        return {'as_of':'2026-10-01','horizon_end':'2026-10-31','accounts':[{'account_id':'a','currency':'CNY','opening_balance_minor':1200000,'reserve_floor_minor':500000,'held_minor':0}],
                'timeline':[{'date':'2026-10-15','accounts':[{'account_id':'a','balance_after_confirmed_minor':900000,'balance_if_candidates_occur_minor':900000,'held_minor':0}]},{'date':'2026-10-20','accounts':[{'account_id':'a','balance_after_confirmed_minor':300000,'balance_if_candidates_occur_minor':300000,'held_minor':0}]}]}
    def test_floor_shortfall_is_not_negative_bank_balance(self):
        r=liquidity_scenario(self.timeline(),'a',[])
        self.assertEqual(r['planning_floor_shortfall_minor'],200000);self.assertEqual(r['rows'][-1]['cash_after_plans_minor'],300000)
    def test_salary_and_unlock_cannot_silently_raise_cash(self):
        r=liquidity_scenario(self.timeline(),'a',[]);self.assertEqual(r['rows'][-1]['cash_after_confirmed_minor'],300000)
    def test_unknown_currency_and_dates_are_rejected(self):
        for p in ({'date':'2026-10-20','currency':'USD','amount_minor':10},{'date':'2026-11-20','currency':'CNY','amount_minor':10},{'date':'invalid','currency':'CNY','amount_minor':10}):
            with self.assertRaises(BankError):liquidity_scenario(self.timeline(),'a',[p])
    def test_candidate_branch_is_separate(self):
        t=self.timeline();t['timeline'][-1]['accounts'][0]['balance_if_candidates_occur_minor']=250000
        r=liquidity_scenario(t,'a',[])
        self.assertEqual(r['rows'][-1]['available_if_candidates_occur_minor'],-250000)
        self.assertEqual(r['minimum_available_minor'],-200000)

if __name__=='__main__':unittest.main()
