import unittest,tempfile,json
from pathlib import Path
from finance_gateway import FinanceGateway
from wealth_returns import calculate,value_minor,COMPONENTS,COMPLETENESS
from wealth_portfolio import state,convert
from financial_calendar import summation


def point(opening,closing,contribution=0,withdrawal=0):return {'accounting_basis':'total_equity_v2','opening':opening,'closing':closing,'owner_contribution_minor':contribution,'owner_withdrawal_minor':withdrawal,**{k:True for k in COMPLETENESS}}

class EquityTests(unittest.TestCase):
    def test_dividend_confirmation_and_settlement(self):
        a=state(100,1);b={**state(100,'.9'),'dividend_receivable_minor':1000};self.assertEqual(calculate(point(a,b))['daily_pnl_minor'],0)
        c={**state(100,'.9'),'investment_cash_minor':1000};self.assertEqual(calculate(point(b,c))['daily_pnl_minor'],0)
    def test_reinvestment_same_and_next_day(self):
        a=state(100,1);b=state('111.111111','.9');self.assertEqual(calculate(point(a,b))['daily_pnl_minor'],0)
        pending={**state(100,'.92'),'pending_subscription_minor':1000};self.assertEqual(calculate(point(a,pending))['daily_pnl_minor'],200)
    def test_redemption_receivable_and_owner_withdrawal(self):
        a=state(100,1);b={**state(0,None),'redemption_receivable_minor':10500};self.assertEqual(calculate(point(a,b))['daily_pnl_minor'],500)
        c=state(0,None);self.assertEqual(calculate(point(b,c,withdrawal=10500))['daily_pnl_minor'],0)
    def test_subscription_confirmation(self):
        a=state(100,1);b={**state(100,1),'pending_subscription_minor':1000};self.assertEqual(calculate(point(a,b,contribution=1000))['daily_pnl_minor'],0)
        c=state(110,1);self.assertEqual(calculate(point(b,c))['daily_pnl_minor'],0)
    def test_fee_and_external_owner_fee(self):
        self.assertEqual(calculate(point(state(100,1),state(99,1)))['daily_pnl_minor'],-100)
        before={**state(100,1),'payable_minor':100};after=state(100,1);self.assertEqual(calculate(point(before,after,contribution=100))['daily_pnl_minor'],0)
        self.assertEqual(calculate(point(state(100,1),before))['daily_pnl_minor'],-100)
    def test_explicit_basis_and_missing_not_zero(self):
        self.assertEqual(calculate({})['status'],'unavailable')
        for key in COMPLETENESS:self.assertIsNone(calculate({**point(state(1,1),state(1,1)),key:False})['daily_pnl_minor'])
        with self.assertRaises(ValueError):value_minor(1,0)
        self.assertEqual(value_minor(0,None),0)
    def test_rounding_asset_conservation(self):
        qty,cash=convert(10000,'3.6834');self.assertEqual(value_minor(qty,'3.6834')+cash,10000)
    def test_partial_result_and_cash_independent(self):
        base={'currency':'CNY','state':'recorded','source_type':'simulation'}
        rows=[{**base,'kind':'investment_pnl','returns':{'status':'available','daily_pnl_minor':200}},{**base,'kind':'investment_pnl','returns':{'status':'unavailable'}},{**base,'kind':'distribution','amount_minor':100}]
        r=summation(rows)[0];self.assertIsNone(r['investment_pnl_minor']);self.assertEqual(r['known_investment_pnl_minor'],200);self.assertEqual(r['settled_dividend_minor'],100)

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.g=FinanceGateway(Path(self.tmp.name)/'bank');self.w,_=self.g.create_session()
    def test_all_four_boards_and_complete_paths(self):
        cash=self.g.financial_graph.query(self.w,{'view':'cashflow'});self.assertTrue(any(n.get('metric_family')=='period_收入'for n in cash['nodes']));self.assertTrue(any(n.get('metric_family')=='period_支出'for n in cash['nodes']))
        wealth=self.g.financial_graph.query(self.w,{'view':'wealth'});self.assertTrue(any(n.get('role')=='liability'for n in wealth['nodes']));self.assertTrue(any(n['kind']=='account'for n in wealth['nodes']))
        for v in ['overview','cashflow','wealth','plans','preferences']:self.assertEqual(self.g.financial_graph.query(self.w,{'view':v})['view'],v)
    def test_real_nav_arithmetic_and_independent_history(self):
        r=self.g.wealth.detail(self.w,'ORIENT:006210','2026-09-30');self.assertEqual(r['returns']['daily_pnl_minor'],-400)
        self.assertEqual(self.g.wealth.detail(self.w,'ORIENT:006210','2026-09-29')['returns']['daily_pnl_minor'],300)
        q=self.g.calendar.query(self.w,{'from':'2026-09-01','to':'2026-09-30','portfolio':'historical'});self.assertFalse(any(d['summary_by_currency']and any(r['recorded_net_minor']is not None for r in d['summary_by_currency'])for d in q['days']))
        before=q['summary_by_currency'][0]['investment_pnl_minor'];self.g.wealth.provision(self.w);self.assertEqual(self.g.calendar.query(self.w,{'from':'2026-09-01','to':'2026-09-30','portfolio':'historical'})['summary_by_currency'][0]['investment_pnl_minor'],before)
    def test_current_cash_not_changed_by_provision_or_migration_audit(self):
        a=self.g.bank.holdings(self.w);cash=[x['balance_minor']for x in self.g.overview(self.w)['accounts']];self.g.wealth.provision(self.w);r=self.g.wealth.migrate(self.w);self.assertEqual(r['status'],'blocked');self.assertEqual(a,self.g.bank.holdings(self.w));self.assertEqual(cash,[x['balance_minor']for x in self.g.overview(self.w)['accounts']])
    def test_history_after_current_holdings_removed_and_scope(self):
        self.g.wealth.provision(self.w)
        with self.g.bank.transaction()as db:db.execute('DELETE FROM holdings WHERE workspace_id=?',(self.w,))
        self.assertEqual(self.g.wealth.detail(self.w,'ORIENT:006210','2026-09-30')['returns']['daily_pnl_minor'],-400)
        w2,_=self.g.create_session();v=self.g.wealth.versions(self.w)['versions'][0]['id'];self.g.wealth.provision(w2)
        with self.g.bank.transaction()as db:db.execute('DELETE FROM portfolio_versions WHERE workspace_id=?',(w2,))
        # Unregistered explicit version never leaks first workspace payload.
        with self.assertRaises(Exception):self.g.wealth.detail(w2,'ORIENT:006210','2026-09-30','history-does-not-exist')

class CompletenessRegressionTests(IntegrationTests):
    def setUp(self):
        super().setUp();self.g.wealth.provision(self.w)
    def clone_account(self):
        import json,copy
        pid=self.g.wealth.versions(self.w)['versions'][0]['id'];fid='ORIENT:006210'
        with self.g.bank.transaction()as db:
            pos=json.loads(db.execute('SELECT payload FROM portfolio_positions WHERE workspace_id=? AND portfolio_version=? AND fund_id=?',(self.w,pid,fid)).fetchone()[0]);pos['quantity']='2000';pos['remaining_cost_minor']*=2;pos['initial_cost_minor']*=2
            db.execute('INSERT INTO portfolio_positions VALUES(?,?,?,?,?)',(self.w,pid,'second-account',fid,json.dumps(pos)))
            for row in db.execute('SELECT * FROM portfolio_valuations WHERE workspace_id=? AND portfolio_version=? AND fund_id=?',(self.w,pid,fid)).fetchall():
                p=json.loads(row['payload']);p['account_id']='second-account';p['quantity']='2000';p['opening']['quantity']=p['closing']['quantity']='2000';p['remaining_cost_minor']*=2;p['cumulative_pnl_minor']*=2
                db.execute('INSERT INTO portfolio_valuations VALUES(?,?,?,?,?,?)',(self.w,pid,'second-account',fid,row['valuation_on'],json.dumps(p)))
        return pid,fid
    def test_multi_account_detail_and_calendar(self):
        pid,fid=self.clone_account();r=self.g.wealth.detail(self.w,fid,'2026-09-30');self.assertEqual(r['point']['quantity'],'3000');self.assertEqual(r['returns']['daily_pnl_minor'],-1200);self.assertEqual(len(r['history'][-1]['accounts']),2)
        self.assertEqual(r['point']['remaining_cost_minor'],sum(p['remaining_cost_minor']for p in r['positions']))
        self.assertEqual(self.g.wealth.detail(self.w,fid,'2026-10-03')['as_of'],'2026-10-03')
        with self.g.bank.transaction()as db:db.execute('DELETE FROM portfolio_valuations WHERE workspace_id=? AND portfolio_version=? AND account_id=? AND valuation_on=?',(self.w,pid,'second-account','2026-09-30'))
        q=self.g.calendar.query(self.w,{'from':'2026-09-30','to':'2026-09-30','portfolio':'historical'});self.assertEqual(q['days'][0]['wealth_status'],'missing');self.assertIsNone(q['summary_by_currency'][0]['investment_pnl_minor']);self.assertIsNone(self.g.wealth.detail(self.w,fid,'2026-09-30')['returns']['daily_pnl_minor'])
        partial=self.g.wealth.detail(self.w,fid,'2026-09-30')['point'];self.assertIsNone(partial['remaining_cost_minor']);self.assertIsNone(partial['owner_contribution_minor'])
    def test_whole_fund_missing_and_first_nav_equity(self):
        first=self.g.financial_graph.query(self.w,{'portfolio':'historical','from':'2025-10-09','to':'2025-10-09','view':'wealth'});self.assertEqual(first['metrics'][0]['investment_equity_minor'],642940)
        with self.g.bank.transaction()as db:db.execute('DELETE FROM portfolio_valuations WHERE workspace_id=? AND fund_id=?',(self.w,'ORIENT:001198'))
        graph=self.g.financial_graph.query(self.w,{'portfolio':'historical','view':'wealth'});self.assertIsNone(graph['metrics'][0]['investment_equity_minor']);self.assertTrue(any('001198'in v for v in graph['coverage']['gaps']))
    def test_source_revision_and_reactivation(self):
        from unittest.mock import patch
        from fund_history import cached,FUNDS
        import copy
        original={c:cached(c)for c in FUNDS};before=self.g.wealth.versions(self.w)['versions'][0]['id'];changed=copy.deepcopy(original);changed['006210']['product']['risk_level']=5
        with patch('wealth_portfolio.cached',side_effect=lambda c:changed[c]):
            after=self.g.wealth.versions(self.w);self.assertNotEqual(before,next(v['id']for v in after['versions']if v['active']));self.assertEqual(self.g.wealth.detail(self.w,'ORIENT:006210','2026-09-30')['product']['risk_level'],5)
        with patch('wealth_portfolio.cached',side_effect=lambda c:original[c]):
            versions=self.g.wealth.versions(self.w);self.assertEqual(next(v['id']for v in versions['versions']if v['active']),before)
    def test_source_withdrawal_does_not_remove_component(self):
        from unittest.mock import patch
        from fund_history import cached,FUNDS
        import copy
        changed={c:copy.deepcopy(cached(c))for c in FUNDS};changed['001198']['coverage']['corporate_actions_complete']=False
        with patch('wealth_portfolio.cached',side_effect=lambda c:changed[c]):
            graph=self.g.financial_graph.query(self.w,{'portfolio':'historical','view':'wealth'});self.assertEqual(len([n for n in graph['nodes']if n.get('fund_id')]),3);self.assertIsNone(graph['metrics'][0]['investment_equity_minor']);q=self.g.calendar.query(self.w,{'from':'2026-09-30','to':'2026-09-30','portfolio':'historical'});self.assertIsNone(q['summary_by_currency'][0]['investment_pnl_minor'])
        with patch('wealth_portfolio.cached',return_value=None):
            q=self.g.calendar.query(self.w,{'from':'2026-09-30','to':'2026-09-30','portfolio':'historical'});self.assertEqual(q['days'][0]['wealth_status'],'missing')
    def test_basis_rejected_not_relabelled(self):
        for service in [self.g.calendar,self.g.financial_graph]:
            with self.assertRaises(Exception):service.query(self.w,{'portfolio':'historical','accounting_basis':'holdings_only_v1'})
    def test_decision_evidence_confirmation_and_feedback(self):
        from simulated_bank import BankError
        p={'choice':'先留应急金','alternatives':['先留应急金','购买长期产品'],'reason':'下个月有学费支出','evidence_refs':[],'confirmed':True,'feedback':'暂未执行'}
        d=self.g.financial_graph.decision(self.w,p);graph=self.g.financial_graph.query(self.w,{'view':'preferences'});self.assertTrue(any(n.get('decision',{}).get('id')==d['id']for n in graph['nodes']))
        for extra in [{'confirmed':False},{'expires_on':'not-a-date'},{'execution_ids':['unknown-operation']}]:
            with self.assertRaises(BankError):self.g.financial_graph.decision(self.w,{**p,**extra})
    def test_economic_surplus_excludes_financing(self):
        from financial_graph import classify
        rows=[{'kind':'income','amount_minor':600000},{'kind':'expense','amount_minor':200000},{'kind':'purchase','amount_minor':100000},{'kind':'transfer','amount_minor':300000,'internal_transfer':True},{'kind':'loan_principal','amount_minor':300000}]
        economic=sum(r['amount_minor']*(1 if classify(r)=='income'else -1 if classify(r)=='expense'else 0)for r in rows);self.assertEqual(economic,400000)

class SourceFreshnessTests(CompletenessRegressionTests):
    def test_inflight_source_revision_blocks_old_agent_context(self):
        from unittest.mock import patch
        from fund_history import cached,FUNDS
        from agent_runtime.bank_tools import BankTools
        import copy
        t=BankTools(self.g,self.w,'test-source-revision');changed={c:copy.deepcopy(cached(c))for c in FUNDS};changed['006210']['product']['risk_level']=5
        with patch('wealth_portfolio.cached',side_effect=lambda c:changed[c]):
            with self.assertRaises(Exception):t.fresh()
    def test_partial_detail_does_not_label_known_part_as_total(self):
        pid,fid=self.clone_account()
        with self.g.bank.transaction()as db:db.execute('DELETE FROM portfolio_valuations WHERE workspace_id=? AND portfolio_version=? AND account_id=? AND valuation_on=?',(self.w,pid,'second-account','2026-09-30'))
        r=self.g.wealth.detail(self.w,fid,'2026-09-30');self.assertIsNone(r['point']['quantity']);self.assertIsNone(r['point']['closing']['investment_cash_minor']);self.assertEqual(r['point']['known_quantity'],'1000');self.assertEqual(r['point']['missing_accounts'],['second-account'])

class HistoricalRowCompletenessTests(IntegrationTests):
    def test_past_partial_day_not_masked_by_complete_latest_day(self):
        helper=CompletenessRegressionTests();helper.g=self.g;helper.w=self.w;self.g.wealth.provision(self.w);pid,fid=helper.clone_account()
        with self.g.bank.transaction()as db:db.execute('DELETE FROM portfolio_valuations WHERE workspace_id=? AND portfolio_version=? AND account_id=? AND valuation_on=?',(self.w,pid,'second-account','2026-09-29'))
        r=self.g.wealth.detail(self.w,fid,'2026-09-30');self.assertEqual(r['point']['quantity'],'3000');past=next(p for p in r['history']if p['date']=='2026-09-29');self.assertIsNone(past['quantity']);self.assertEqual(past['known_quantity'],'1000');self.assertEqual(past['missing_accounts'],['second-account'])

class DecisionGraphTests(IntegrationTests):
    def test_choice_alternatives_and_confirmed_goal_connect(self):
        goal=self.g.bank.customer_archive(self.w)['records']['wealth_goals'][0]
        self.g.financial_graph.decision(self.w,{'choice':'保留学费','alternatives':['保留学费','购买长期产品'],'reason':'学费日期较近','evidence_refs':[{'source_scope':'simulation','source_table':'wealth_goals','source_id':goal['id']}],'confirmed':True})
        graph=self.g.financial_graph.query(self.w,{'view':'preferences'});self.assertTrue(any(n.get('role')=='alternative'for n in graph['nodes']));self.assertTrue(any(n['id']=='goal:'+goal['id']for n in graph['nodes']))
    def test_deleted_memory_does_not_resurrect_decision(self):
        m=self.g.memories.create(self.w,{'category':'goal','value':'明年支付学费'})['items'][0];p={'choice':'先留学费','alternatives':['先留学费','购买长期产品'],'reason':'希望保留灵活资金','evidence_refs':[{'source_scope':'user_confirmed','source_table':'user_memories','source_id':m['node_id']}],'confirmed':True};d=self.g.financial_graph.decision(self.w,p)
        self.g.memories.delete(self.w,m['node_id'],{});graph=self.g.financial_graph.query(self.w,{'view':'preferences'});self.assertFalse(any(n.get('decision',{}).get('id')==d['id']for n in graph['nodes']))
