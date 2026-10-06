import json
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from finance_gateway import FinanceGateway
from calendar_events import occurrences, normalize
from financial_calendar import summation
from wealth_returns import calculate, value_minor
from simulated_bank import BankError
from server import create_server
from test_bank_integration import Browser
from test_native_conversation import ScriptedPrimary,Reviewer,call,last_tool
from agent_runtime.conversation import ConversationAgent
from agent_runtime.bank_tools import BankTools
from test_native_conversation import transfer_step,reviewed

CSV='外部编号,标题,类型,日期,重复周期,月份日期策略,预算金额,币种,提前提醒天数,备注\nBIRTH-MOM,妈妈生日,生日,2026-11-05,每年,跳过该期,500.00,CNY,7,礼物预算\nRENT,房租,固定开支,2026-11-10,每月,当月最后一天,2500.00,CNY,3,手工计划\n'


def final_widget(messages,tools):
    result=last_tool(messages)
    return call('answer_reviewed',{'text':'已准备日历内容，请核对来源和日期；没有执行资金操作。',
        'claims':[],'draft_ids':[],'followups':[],'widget_ids':[result['data']['widget_id']]})


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.g=FinanceGateway(self.path)
        self.w,self.token=self.g.create_session();self.other,_=self.g.create_session();self.c=self.g.calendar
    def tearDown(self):self.tmp.cleanup()
    def create(self,**overrides):
        event={'title':'妈妈生日','kind':'birthday','start_on':'2026-11-05','recurrence':'yearly','remind_days':7,'budget_minor':50000,**overrides}
        p=self.c.events.prepare(self.w,{'action':'create','event':event});return self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True})['series_id']
    def error(self,code,fn):
        with self.assertRaises(BankError) as result:fn()
        self.assertEqual(result.exception.code,code)

    def test_queries_match_original_recorded_amounts_and_do_not_change_balances(self):
        before=self.g.overview(self.w)
        result=self.c.query(self.w,{'from':'2026-09-01','to':'2026-09-30','view':'month'})
        tx=self.g.bank.transactions(self.w,'2026-09');total=result['summary_by_currency'][0]
        self.assertEqual(total['recorded_inflow_minor'],sum(max(r['delta_minor'],0) for r in tx))
        self.assertEqual(total['recorded_outflow_minor'],sum(max(-r['delta_minor'],0) for r in tx))
        self.assertEqual(total['recorded_net_minor'],sum(r['delta_minor'] for r in tx))
        self.assertEqual(total['refund_minor'],sum(r['amount_minor'] for r in tx if r['kind']=='refund'))
        self.assertEqual(before['accounts'],self.g.overview(self.w)['accounts'])
        self.assertEqual(before['orders'],self.g.overview(self.w)['orders'])
        self.assertEqual(result['as_of'],'2026-10-03')
        empty=self.c.query(self.w,{'from':'2030-01-01','to':'2030-01-01'})
        self.assertEqual(empty['days'][0]['coverage'],'unknown');self.assertEqual(empty['summary_by_currency'],[])
    def test_range_periods_scope_and_pagination(self):
        q=self.c.query(self.w,{'period':'last_week','view':'week'})
        self.assertEqual(q['range'],{'from':'2026-09-21','to':'2026-09-27'})
        self.error('CALENDAR_RANGE',lambda:self.c.query(self.w,{'from':'2026-01-01','to':'2026-12-31'}))
        self.error('CALENDAR_DATE',lambda:self.c.query(self.w,{'from':'2026-02-30'}))
        self.error('CALENDAR_NOT_FOUND',lambda:self.c.detail(self.other,q['view_id'],'2026-09-21'))
        self.error('CALENDAR_PAGE',lambda:self.c.detail(self.w,q['view_id'],'2026-09-21',0,101))
        self.error('CALENDAR_RANGE',lambda:self.c.detail(self.w,q['view_id'],'2026-10-01'))
        for i in range(103):self.create(title='计划'+str(i),recurrence='once')
        view=self.c.query(self.w,{'from':'2026-11-05','to':'2026-11-05'})
        d=self.c.detail(self.w,view['view_id'],'2026-11-05');self.assertEqual(len(d['items']),100);self.assertTrue(d['has_more'])
        page=self.c.detail(self.w,view['view_id'],'2026-11-05',100);self.assertEqual(len(page['items']),d['total']-100)
    def test_recurrence_month_end_and_leap_year(self):
        p=normalize({'title':'月底','kind':'bill','start_on':'2026-01-31','recurrence':'monthly'})
        self.assertEqual([d.isoformat() for d in occurrences(p,date(2026,2,1),date(2026,4,30))],['2026-03-31'])
        p['month_policy']='last_day'
        self.assertEqual([d.isoformat() for d in occurrences(p,date(2026,2,1),date(2026,4,30))],['2026-02-28','2026-03-31','2026-04-30'])
        p.update(start_on='2024-02-29',recurrence='yearly',month_policy='skip')
        self.assertEqual([d.isoformat() for d in occurrences(p,date(2025,1,1),date(2028,3,1))],['2028-02-29'])
        p['month_policy']='last_day'
        self.assertEqual([d.isoformat() for d in occurrences(p,date(2025,1,1),date(2025,3,1))],['2025-02-28'])
    def test_event_draft_boundaries_idempotence_and_old_view_latest_details(self):
        initial=self.g.overview(self.w)['accounts'];sid=self.create()
        old=self.c.query(self.w,{'from':'2026-11-01','to':'2026-11-30'})
        p=self.c.events.prepare(self.w,{'action':'edit','series_id':sid,'scope':'series','event':{'budget_minor':30000}})
        self.error('CALENDAR_NOT_FOUND',lambda:self.c.events.confirm(self.other,p['draft_id'],{'confirmed':True}))
        self.error('CONFIRMATION_REQUIRED',lambda:self.c.events.confirm(self.w,p['draft_id'],{'confirmed':False}))
        result=self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True})
        self.assertEqual(result,self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True}))
        fresh=self.c.detail(self.w,old['view_id'],'2026-11-05');self.assertTrue(fresh['changed_since_snapshot'])
        item=next(i for i in fresh['items'] if i.get('series_id')==sid)
        self.assertEqual(item['budget_minor'],30000)
        original=next(i for d in old['days'] for i in d['preview'] if i.get('series_id')==sid)
        self.assertEqual(original['budget_minor'],50000)
        self.error('CALENDAR_NOT_FOUND',lambda:self.c.item(self.other,item['id']))
        self.assertEqual(self.c.item(self.w,item['id'])['budget_minor'],30000)
        self.assertEqual(initial,self.g.overview(self.w)['accounts'])
    def test_target_revision_expiry_pending_review(self):
        sid=self.create();p=self.c.events.prepare(self.w,{'action':'delete','series_id':sid})
        edit=self.c.events.prepare(self.w,{'action':'edit','series_id':sid,'event':{'title':'新的标题'}})
        self.c.events.confirm(self.w,edit['draft_id'],{'confirmed':True})
        self.error('CALENDAR_STALE',lambda:self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True}))
        p=self.c.events.prepare(self.w,{'action':'delete','series_id':sid},agent=True)
        self.error('CALENDAR_REVIEW_REQUIRED',lambda:self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True}))
        self.c.events.release(self.w,[p['draft_id']],True)
        with self.g.bank.transaction() as db:db.execute('UPDATE calendar_event_drafts SET expires_at=0 WHERE id=?',(p['draft_id'],))
        self.error('CALENDAR_EXPIRED',lambda:self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True}))
    def test_edit_occurrence_future_and_delete(self):
        sid=self.create(recurrence='monthly')
        p=self.c.events.prepare(self.w,{'action':'edit','series_id':sid,'scope':'occurrence','original_on':'2026-12-05','event':{'budget_minor':10000}})
        self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True})
        rows=self.c.events.list(self.w,date(2026,11,1),date(2027,1,31))
        self.assertEqual([r['budget_minor'] for r in rows],[50000,10000,50000])
        again=self.c.events.prepare(self.w,{'action':'edit','series_id':sid,'scope':'occurrence','original_on':'2026-12-05','event':{'note':'仅改备注'}})
        self.c.events.confirm(self.w,again['draft_id'],{'confirmed':True})
        self.assertEqual(next(r for r in self.c.events.list(self.w,date(2026,12,1),date(2026,12,31)) if r['series_id']==sid)['budget_minor'],10000)
        p=self.c.events.prepare(self.w,{'action':'delete','series_id':sid,'scope':'occurrence','original_on':'2026-12-05'})
        self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True})
        rows=self.c.events.list(self.w,date(2026,11,1),date(2027,1,31));self.assertEqual(rows[1]['state'],'cancelled');self.assertEqual(rows[2]['state'],'planned')
        p=self.c.events.prepare(self.w,{'action':'edit','series_id':sid,'scope':'future','original_on':'2027-01-05','event':{'budget_minor':30000}})
        self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True})
        rows=sorted(self.c.events.list(self.w,date(2026,11,1),date(2027,2,28)),key=lambda i:i['date'])
        self.assertEqual([r['budget_minor'] for r in rows if r['state']=='planned'],[50000,30000,30000])
    def test_budget_can_be_cleared_and_filtered_view_retains_scope(self):
        sid=self.create();tools=BankTools(self.g,self.w,'清空生日预算')
        draft=tools.invoke('calendar_prepare_event',{'action':'edit','series_id':sid,'event':{'budget_minor':None}})
        self.c.events.release(self.w,[draft['data']['draft_id']],True)
        self.c.events.confirm(self.w,draft['data']['draft_id'],{'confirmed':True})
        result=self.c.query(self.w,{'from':'2026-11-01','to':'2026-11-30','kinds':['birthday']})
        self.assertEqual(result['source_scope']['kinds'],['birthday'])
        self.assertIsNone(next(i for d in result['days'] for i in d['preview'] if i['series_id']==sid)['budget_minor'])
    def test_import_atomic_conflicts_duplicates_changes_and_isolation(self):
        e=self.c.events;p=e.preview(self.w,CSV);self.assertTrue(p['can_commit'])
        self.error('CALENDAR_NOT_FOUND',lambda:e.commit(self.other,{'batch_id':p['batch_id'],'confirmed':True}))
        result=e.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True});self.assertEqual(result['counts']['create'],2)
        self.assertEqual(result,e.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True}))
        p=e.preview(self.w,CSV);result=e.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True});self.assertEqual(result['counts']['duplicate'],2)
        changed=CSV.replace('500.00','300.00');p=e.preview(self.w,changed);self.assertEqual(p['rows'][0]['action'],'update');self.assertEqual(p['rows'][0]['before']['budget_minor'],50000)
        e.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True})
        broken=CSV+'NEW,标题,生日,2026-02-30,单次,跳过该期,,CNY,0,测试\n';p=e.preview(self.w,broken)
        self.assertFalse(p['can_commit']);self.error('CSV_ERRORS',lambda:e.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True}))
        self.assertEqual(len(e.list(self.w,date(2026,11,1),date(2026,11,30))),2)
        p=e.preview(self.w,CSV);sid=p['rows'][0]['target_id'];edit=e.prepare(self.w,{'action':'edit','series_id':sid,'event':{'title':'变化'}});e.confirm(self.w,edit['draft_id'],{'confirmed':True})
        self.error('CALENDAR_STALE',lambda:e.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True}))
    def test_imported_statements_isolated_unknown_transfer_and_life_overlay(self):
        raw='流水号,日期,商户,金额,类型\nA,2026-10-01,收入,100.00,收入\nB,2026-10-02,转账,20.00,转账\nC,2026-10-02,商店,5.00,支出\n'
        p=self.g.statements.preview(self.w,raw)
        self.g.statements.commit(self.w,{'batch_id':p['batch_id'],'confirmed':True})
        self.create(start_on='2026-10-02');q=self.c.query(self.w,{'from':'2026-10-01','to':'2026-10-03','source':'imported','simulation_overlay':True})
        t=q['summary_by_currency'][0];self.assertEqual(t['recorded_net_minor'],9500);self.assertEqual(t['unknown_direction_count'],1);self.assertEqual(t['user_budget_minor'],50000)
        self.assertEqual(t['confirmed_due_minor'],0)
        self.assertEqual(q['clock_mode'],'actual_records')
        self.assertTrue(q['simulation_overlay_summary'])
    def test_reminders_memory_off_and_no_banking_effect(self):
        self.create(start_on='2026-10-10');initial=self.g.overview(self.w)
        r=self.c.events.reminders(self.w,'2026-10-03');self.assertEqual(len(r['items']),1)
        self.assertEqual(r,self.c.events.reminders(self.w,'2026-10-03'))
        self.error('CALENDAR_NOT_FOUND',lambda:self.c.events.read_reminder(self.other,r['items'][0]['id']))
        self.c.events.read_reminder(self.w,r['items'][0]['id']);self.assertEqual(self.c.events.reminders(self.w,'2026-10-03')['items'],[])
        q=self.c.query(self.w,{'from':'2026-10-01','to':'2026-10-31'})
        self.assertFalse(any(i['source_type']=='bank_profile' for d in q['days'] for i in d['preview']))
        self.assertEqual(initial['accounts'],self.g.overview(self.w)['accounts']);self.assertEqual(initial['orders'],self.g.overview(self.w)['orders'])
    def test_profile_link_consent_expiry_and_calendar_survives_deleted_memory(self):
        m=self.g.memories.create(self.w,{'category':'goal','value':'给家人准备生日礼物'})['items'][0]['node_id']
        foreign=self.g.memories.create(self.other,{'category':'goal','value':'其他账户目标'})['items'][0]['node_id']
        self.error('MEMORY_DISABLED',lambda:self.create(linked_memory_ids=[m]))
        self.g.memories.set_enabled(self.w,{'enabled':True})
        self.error('CALENDAR_LINK',lambda:self.create(linked_memory_ids=[foreign]))
        expired=self.g.memories.create(self.w,{'category':'goal','value':'过期礼物目标','expires_on':'2026-10-03'})['items'][0]['node_id']
        self.g.bank.advance_days(self.w,1)
        self.error('CALENDAR_LINK',lambda:self.create(linked_memory_ids=[expired]))
        sid=self.create(linked_memory_ids=[m]);graph=self.g.personal_graph.read(self.w,for_agent=True)
        self.assertTrue(any(e['source']=='calendar:'+sid and e['target']=='memory:'+m for e in graph['links']))
        self.g.memories.set_enabled(self.w,{'enabled':False})
        graph=self.g.personal_graph.read(self.w,for_agent=True)
        self.assertFalse(any(n['group']=='preference' for n in graph['nodes']))
        self.assertTrue(any(n['id']=='calendar:'+sid for n in graph['nodes']))
        self.g.memories.delete(self.w,m,{})
        edit=self.c.events.prepare(self.w,{'action':'edit','series_id':sid,'event':{'budget_minor':30000}})
        self.c.events.confirm(self.w,edit['draft_id'],{'confirmed':True})
        self.assertEqual(next(e for e in self.c.events.list(self.w,date(2026,11,1),date(2026,11,30)) if e['series_id']==sid)['budget_minor'],30000)
    def test_money_categories_currencies_internal_transfers_and_returns(self):
        def tx(c,k,amount,delta,internal=False):return {'kind':'transaction','state':'recorded','currency':c,'transaction_kind':k,'amount_minor':amount,'cash_delta_minor':delta,'source_type':'simulation','internal_transfer':internal}
        rows=summation([tx('CNY','income',10000,10000),tx('CNY','transfer',5000,-5000,True),tx('CNY','transfer',5000,5000,True),tx('USD','refund',200,200),tx('USD','purchase',1000,-1000)])
        self.assertEqual(rows[0]['recorded_net_minor'],10000);self.assertEqual(rows[0]['transfer_out_minor'],0)
        self.assertEqual(rows[1]['recorded_net_minor'],-800);self.assertEqual(rows[1]['expense_minor'],0);self.assertEqual(rows[1]['investment_principal_out_minor'],1000)
    def test_wealth_independent_samples_exact_daily_and_missing_cost(self):
        # 手算：100*10 -> (100+20-10)*10.1，买20000，卖10100，分红300，费用50：日损益1450分。
        p={'accounting_basis':'holdings_only_v1','opening_quantity':'100','opening_nav':'10','closing_quantity':'110','closing_nav':'10.1',
            'buy_minor':20000,'sale_minor':10100,'dividend_minor':300,'fee_minor':50,'movements_complete':True,
            'allocated_sale_cost_minor':10000,'remaining_cost_minor':110000,'cumulative_realized_minor':100,
            'cumulative_dividend_minor':300,'cumulative_fee_minor':50,'settled_dividend_minor':300,'cost_rule':'移动平均成本'}
        r=calculate(p);self.assertEqual(r['daily_pnl_minor'],1450);self.assertEqual(r['cumulative_pnl_minor'],1450);self.assertEqual(r['realized_gross_pnl_minor'],100)
        # 第二份：买入+部分赎回+现金分红+费用，负损益350分。
        p2={**p,'closing_nav':'10','dividend_minor':0,'sale_minor':9900,'fee_minor':250};self.assertEqual(calculate(p2)['daily_pnl_minor'],-350)
        self.assertEqual(value_minor('0.1','0.15'),2)
        missing={**p,'opening_nav':None};self.assertIsNone(calculate(missing)['daily_pnl_minor'])
        no_cost={k:v for k,v in p.items() if k not in ('allocated_sale_cost_minor','remaining_cost_minor')};self.assertIsNone(calculate(no_cost)['cumulative_pnl_minor']);self.assertIsNone(calculate(no_cost)['realized_pnl_minor'])
        holdings=self.g.bank.customer_archive(self.w)['records']['holdings']
        account=holdings[0]['account_id']
        with self.g.bank.transaction() as db:
            for dataset,product,point in [('isolated-one',holdings[0]['product_id'],p),('isolated-two',holdings[1]['product_id'],p2),('isolated-missing',holdings[0]['product_id'],missing)]:
                db.execute('INSERT INTO wealth_valuation_points VALUES(?,?,?,?,?,?,?,?)',(self.w,dataset,product,'2026-10-02','CNY','simulation_fixture',1,json.dumps({**point,'account_id':account,'settled_dividend_on':'2026-10-03'})))
        q=self.c.query(self.w,{'from':'2026-10-02','to':'2026-10-02'});self.assertIsNone(q['summary_by_currency'][0]['investment_pnl_minor']);self.assertEqual(q['summary_by_currency'][0]['known_investment_pnl_minor'],-350)
        detail=self.c.detail(self.w,q['view_id'],'2026-10-02');missing_item=next(i for i in detail['items'] if i['kind']=='investment_pnl' and i['state']=='unknown');self.assertIsNone(missing_item['amount_minor'])
        self.assertFalse(any(i['kind']=='distribution' for i in detail['items']))
        following=self.c.query(self.w,{'from':'2026-10-03','to':'2026-10-03'})
        self.assertEqual(following['summary_by_currency'][0]['settled_dividend_minor'],600)
        Path(self.tmp.name,'samples.json').write_text(json.dumps([p,p2,missing]))
    def test_native_widgets_review_history_and_rejection(self):
        primary=ScriptedPrimary(call('calendar_query',{'period':'next_month','view':'month'}),final_widget)
        reviewer=Reviewer();r=ConversationAgent(primary,reviewer).process(self.g,self.w,'下个月财务日历')
        self.assertFalse(r.get('isFallback'));self.assertEqual(r['widgets'][0]['range'],{'from':'2026-11-01','to':'2026-11-30'});self.assertTrue(reviewer.packets[0]['calendar_widgets'])
        self.assertEqual(self.g.personal_data.read_widgets(self.w)[0]['widgets'][0]['view_id'],r['widgets'][0]['view_id'])
        with closing(self.g.bank.connect()) as db:self.assertEqual(db.execute('SELECT count(*) FROM agent_widget_history WHERE workspace_id=?',(self.w,)).fetchone()[0],0)
        primary=ScriptedPrimary(call('calendar_prepare_event',{'action':'create','event':{'title':'妈妈生日','kind':'birthday','start_on':'2026-11-05'}}),final_widget)
        rejected=ConversationAgent(primary,Reviewer('reject')).process(self.g,self.w,'记录生日')
        self.assertTrue(rejected['isFallback']);self.assertEqual(rejected['widgets'],[])
        with closing(self.g.bank.connect()) as db:row=db.execute("SELECT * FROM calendar_event_drafts WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",(self.w,)).fetchone()
        self.error('CALENDAR_REVIEW_REQUIRED',lambda:self.c.events.confirm(self.w,row['id'],{'confirmed':True}))
    def test_native_approved_event_draft_and_fact_widget(self):
        primary=ScriptedPrimary(call('calendar_prepare_event',{'action':'create','event':{'title':'妈妈生日','kind':'birthday','start_on':'2026-11-05','budget_minor':50000}}),final_widget)
        r=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'记录生日')
        self.assertFalse(r.get('isFallback'));did=r['widgets'][0]['draft']['draft_id'];self.c.events.confirm(self.w,did,{'confirmed':True})
        def facts(m,t):
            o=last_tool(m);return call('answer_facts',{'fact_ids':[o['facts'][0]['id']],'widget_ids':[o['data']['widget_id']]})
        r=ConversationAgent(ScriptedPrimary(call('calendar_query',{'period':'next_month'}),facts),Reviewer()).process(self.g,self.w,'查询日历')
        self.assertEqual(len(r['widgets']),1);self.assertEqual(r['agent_review']['status'],'not_required')
    def test_missing_widget_reference_repairs_without_new_draft(self):
        primary=ScriptedPrimary(call('calendar_prepare_event',{'action':'create','event':{'title':'生日','kind':'birthday','start_on':'2026-11-05'}}),
            {'role':'assistant','content':'已经准备日历草案，待核对。'},final_widget)
        r=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'记录生日')
        self.assertFalse(r.get('isFallback'));self.assertEqual(len(r['widgets']),1)
        self.assertEqual(r['agent_runtime']['rounds'],3)
        with closing(self.g.bank.connect()) as db:self.assertEqual(db.execute('SELECT count(*) FROM calendar_event_drafts WHERE workspace_id=?',(self.w,)).fetchone()[0],1)
    def test_real_confirmation_state_enters_model_context_and_edit_cannot_become_create(self):
        sid=self.create();primary=ScriptedPrimary(call('calendar_prepare_event',{'action':'edit','series_id':sid,'event':{'budget_minor':30000}}),final_widget)
        r=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'改为三百元')
        self.assertFalse(r.get('isFallback'));self.assertEqual(r['widgets'][0]['draft']['action'],'edit')
        initial_payloads=[json.loads(message['content']) for message in primary.messages[0]
                          if message['role']=='user' and isinstance(message.get('content'),str)
                          and message['content'].startswith('{')]
        context_payloads=[payload for payload in initial_payloads if 'server_context' in payload]
        self.assertEqual(len(context_payloads),1)
        model_context=context_payloads[0]['server_context']
        event=model_context['calendar_events'][0]
        self.assertNotEqual(event['series_id'],sid)
        self.assertTrue(event['series_id'].startswith('obj_'))
        self.assertEqual(event['event']['budget_minor'],50000)
        self.assertEqual(model_context['calendar_draft_receipts'][0]['result']['series_id'],event['series_id'])
        tools=BankTools(self.g,self.w,'test')
        self.error('CALENDAR_NOT_FOUND',lambda:tools.invoke('calendar_prepare_event',{'action':'edit','series_id':'not-a-series','event':{'budget_minor':30000}}))
        self.error('CALENDAR_TARGET_REQUIRED',lambda:tools.invoke('calendar_prepare_event',{'action':'create','event':{'title':'生日','kind':'birthday','start_on':'2026-11-05'}}))
    def test_calendar_change_invalidates_prepared_financial_review(self):
        agent=ConversationAgent(ScriptedPrimary(call('account_summary',{}),call('business_records',{'kind':'payments'}),transfer_step,reviewed),Reviewer())
        r=agent.process(self.g,self.w,'给妈妈转1000元');self.assertFalse(r.get('isFallback'));oid=r['bank_actions'][0]['operation_id']
        self.create()
        op=self.g.operation_view(self.w,oid)['operation'];initial=self.g.overview(self.w)['accounts']
        self.error('STALE_AGENT_REVIEW',lambda:self.g.confirm(self.w,oid,{'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':self.g.demo_code(self.token)}))
        self.assertEqual(initial,self.g.overview(self.w)['accounts'])
    def test_timezone_and_in_place_migration(self):
        account=self.g.overview(self.w)['accounts'][0]['id']
        with self.g.bank.transaction() as db:
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('tz-test',self.w,account,'2026-09-30T20:00:00Z','UTC测试',100,-100,'expense','测试','',None,None))
        q=self.c.query(self.w,{'from':'2026-10-01','to':'2026-10-01'})
        d=self.c.detail(self.w,q['view_id'],'2026-10-01');item=next(i for i in d['items'] if i['source_ref']=='tz-test')
        self.assertEqual(item['occurred_at'],'2026-10-01T04:00:00+08:00')
        before=self.g.overview(self.w)['accounts'];new=FinanceGateway(self.path);self.assertEqual(before,new.overview(self.w)['accounts'])
    def test_user_budget_scenario_deduplicates_existing_reservation_and_keeps_currency(self):
        archive=self.g.bank.customer_archive(self.w);hold=archive['records']['fund_holds'][0]
        with self.g.bank.transaction() as db:db.execute("UPDATE fund_holds SET status='held',amount_minor=50000 WHERE id=? AND workspace_id=?",(hold['id'],self.w))
        self.create(title='预算一',recurrence='once',budget_minor=30000,linked_commitment_ids=[hold['id']])
        self.create(title='预算二',recurrence='once',budget_minor=30000,linked_commitment_ids=[hold['id']])
        self.create(title='外币预算',recurrence='once',currency='USD',budget_minor=10000)
        initial=self.g.overview(self.w)['accounts'];r=self.c.budget_scenario(self.w,hold['account_id'],'2026-11-01','2026-11-30')
        self.assertEqual(r['additional_reserve_minor'],10000)
        self.assertEqual(sum(i['already_reserved_minor'] for i in r['items']),50000)
        self.assertEqual(len(r['excluded_currency_item_ids']),1);self.assertEqual(initial,self.g.overview(self.w)['accounts'])
        self.error('CALENDAR_LINK',lambda:self.create(linked_commitment_ids=['foreign-id']))
    def test_agent_new_calendar_draft_supersedes_previous_and_complete_coverage_is_explicit(self):
        p=self.c.events.prepare(self.w,{'action':'create','event':{'title':'一','kind':'life_event','start_on':'2026-11-01'}},agent=True)
        self.c.events.release(self.w,[p['draft_id']],True)
        self.c.events.prepare(self.w,{'action':'create','event':{'title':'二','kind':'life_event','start_on':'2026-11-01'}},agent=True)
        self.error('CALENDAR_REVIEW_REQUIRED',lambda:self.c.events.confirm(self.w,p['draft_id'],{'confirmed':True}))
        q=self.c.query(self.w,{'from':'2025-01-01','to':'2025-01-31'})
        self.assertEqual(q['coverage'],'complete')
        self.assertTrue(all(d['coverage']=='complete' for d in q['days']))
    def test_http_json_sse_and_csrf_isolation(self):
        server=create_server(0,Path(self.tmp.name)/'http',conversation_agent=ConversationAgent(
            ScriptedPrimary(call('calendar_query',{'period':'next_month'}),final_widget,call('calendar_query',{'period':'this_week','view':'week'}),final_widget),Reviewer()))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            a=Browser('http://127.0.0.1:'+str(server.server_port));b=Browser(a.base);a.start();b.start()
            status,q,_=a.call('/api/finance/calendar?period=next_month');self.assertEqual(status,200)
            status,_,_=b.call('/api/finance/calendar/views/'+q['view_id']+'/days/2026-11-05');self.assertEqual(status,404)
            status,_,_=a.call('/api/finance/calendar/events/prepare','POST',{'action':'create','event':{'title':'例','kind':'life_event','start_on':'2026-11-05'}},headers={'X-QZ-CSRF':''});self.assertEqual(status,403)
            status,r,_=a.call('/api/agent','POST',{'text':'下个月日历'});self.assertEqual(status,200);self.assertEqual(len(r['widgets']),1)
            status,r,_=a.call('/api/agent','POST',{'text':'本周日历'},headers={'Accept':'text/event-stream'});self.assertEqual(status,200)
            events=[json.loads(l[6:]) for l in r.splitlines() if l.startswith('data: ')];final=next(e['value'] for e in events if e.get('name')=='navren.result');self.assertEqual(len(final['widgets']),1)
            status,r,_=a.call('/api/finance/calendar/history');self.assertEqual(status,200);self.assertEqual(len(r['entries']),2)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
