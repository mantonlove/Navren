import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
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
    def test_public_concepts_are_separate_from_personal_exposure(self):
        r=self.query(view='risk');self.assertTrue(r['related_knowledge']);self.assertFalse(any(n.get('role')=='industry_exposure'for n in r['nodes']))


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
