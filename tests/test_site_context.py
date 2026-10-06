"""全站最小合同的真实HTTP验收；独立临时银行，无真实模型或用户账户写入。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from server import create_server,ProxyHandler
from test_bank_integration import Browser

class StubAgent:
    def __init__(self):self.calls=[]
    def process(self,g,w,text,**kwargs):
        self.calls.append((w,text,kwargs));return {'reply':'已读取对象引用。','bank':True,'isFallback':False}

class SiteContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.agent=StubAgent()
        cls.quiet=patch.object(ProxyHandler,'log_message',lambda *args:None);cls.quiet.start()
        cls.server=create_server(0,Path(cls.tmp.name)/'site.sqlite3',conversation_agent=cls.agent)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.quiet.stop();cls.tmp.cleanup()
    def setUp(self):self.b=Browser(self.base);self.session,_=self.b.start();self.w=self.session['workspace_id']
    def get(self,p):
        code,data,_=self.b.call('/api/finance'+p);self.assertEqual(code,200,data);return data
    def post(self,p,b,code=200):
        status,data,_=self.b.call('/api/finance'+p,'POST',b);self.assertEqual(status,code,data);return data
    def create_goal(self):
        goals=self.get('/goals');p={'action':'create','name':'新目标','target_minor':100000,'due_on':'2026-11-01','expected_version':goals['version'],'idempotency_key':'test_create_goal'}
        draft=self.post('/goals/prepare',p)
        reply=self.post('/goals/drafts/'+draft['id']+'/confirm',{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})
        return draft,reply
    def test_private_response_binding(self):
        for route in ['/overview','/memories','/financial-records','/source','/goals']:
            response=self.get(route);self.assertEqual(response['workspace_id'],self.w);self.assertEqual(response['privacy_revision'],0)
        status,products,headers=self.b.call('/api/finance/products');self.assertEqual(status,200);self.assertIsInstance(products,list)
        self.assertEqual(headers['X-Navren-Workspace'],self.w);self.assertEqual(headers['X-Navren-Privacy-Revision'],'0')
    def test_report_group_does_not_use_active_source(self):
        source=self.get('/source');v=source['sources']['simulation']['source_version']
        result=self.get('/reports/snapshot?source=simulation&source_version='+v+'&month=2026-09&year=2026')
        self.assertEqual(result['source_binding'],{'workspace_id':self.w,'source_scope':'simulation','source_version':v})
        self.assertTrue(result['transactions']);self.assertIn('candidates',result['periodic_charges'])
        self.assertEqual(self.b.call('/api/finance/reports/snapshot?source=imported&source_version=1')[0],409)
        self.assertEqual(self.b.call('/api/finance/reports/snapshot?source=simulation&source_version=wrong')[0],409)
    def test_goal_confirm_is_same_receipt_and_deadline_projection(self):
        draft,result=self.create_goal();gid=result['goal']['id']
        replay=self.post('/goals/drafts/'+draft['id']+'/confirm',{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})
        self.assertEqual(replay['receipt'],result['receipt']);self.assertEqual(len([g for g in self.get('/goals')['items'] if g['id']==gid]),1)
        cal=self.get('/calendar?from=2026-11-01&to=2026-11-01&source=simulation&portfolio=current&view=day')
        items=[i for d in cal['days'] for i in d['preview'] if i.get('goal_id')==gid]
        # 完整详情不受日格preview截断限制。
        full=self.get('/calendar/views/'+cal['view_id']+'/days/2026-11-01')
        item=next(i for i in full['items'] if i.get('goal_id')==gid)
        self.assertEqual(item['linked_role'],'goal_deadline');self.assertIsNone(item['cash_delta_minor']);self.assertIsNone(item['budget_minor'])
    def test_goal_old_version_cannot_overwrite(self):
        _,r=self.create_goal();goal=r['goal'];p={'action':'update','goal_id':goal['id'],'name':'改期限','target_minor':goal['target_minor'],'due_on':'2026-12-01','expected_version':goal['object_version'],'idempotency_key':'update_goal_key'}
        d=self.post('/goals/prepare',p)
        p2={**p,'name':'另页内容','idempotency_key':'other_goal_key'};other=self.post('/goals/prepare',p2)
        self.post('/goals/drafts/'+other['id']+'/confirm',{'confirmed':True,'confirmation_digest':other['confirmation_digest']})
        self.post('/goals/drafts/'+d['id']+'/confirm',{'confirmed':True,'confirmation_digest':d['confirmation_digest']},409)
        self.assertEqual(self.get('/goals/'+goal['id'])['goal']['name'],'另页内容')
    def test_goal_cancel_does_not_change_business_object(self):
        gs=self.get('/goals');g=gs['items'][0]
        d=self.post('/goals/prepare',{'action':'delete','goal_id':g['id'],'expected_version':g['object_version'],'idempotency_key':'cancel_goal_key'})
        self.assertTrue(any(x['id']==d['id'] for x in self.get('/goals')['drafts']))
        self.assertEqual(self.post('/goals/drafts/'+d['id']+'/cancel',{})['receipt']['state'],'cancelled')
        self.post('/goals/drafts/'+d['id']+'/confirm',{'confirmed':True,'confirmation_digest':d['confirmation_digest']},409)
        self.assertEqual(self.get('/goals/'+g['id'])['goal']['object_version'],g['object_version'])
    def make_context(self):
        state=self.get('/overview');ref={'kind':'account','id':state['accounts'][0]['id'],'source_scope':'simulation','version':state['version'],'label':'日常账户'}
        return self.post('/page-contexts',{'question':'这笔资金在哪里？','reference':ref,'return_view':{'page':'account','view':'account-detail','object_id':ref['id'],'date':'2026-10-03'}})['context_id']
    def test_cross_page_claim_once_agent_once(self):
        cid=self.make_context();self.assertTrue(self.post('/page-contexts/'+cid+'/claim',{})['send_once'])
        self.assertFalse(self.post('/page-contexts/'+cid+'/claim',{})['send_once'])
        count=len(self.agent.calls);status,reply,_=self.b.call('/api/agent','POST',{'text':'这笔资金在哪里？','page_context_id':cid})
        self.assertEqual(status,200,reply);self.assertEqual(len(self.agent.calls),count+1)
        self.assertEqual(self.agent.calls[-1][2]['page_context']['execution_authority'],False)
        self.assertEqual(self.b.call('/api/agent','POST',{'text':'这笔资金在哪里？','page_context_id':cid})[0],409)
        self.assertEqual(len(self.agent.calls),count+1)
    def test_context_workspace_and_session_binding(self):
        cid=self.make_context();other=Browser(self.base);other.start()
        self.assertEqual(other.call('/api/finance/page-contexts/'+cid+'/claim','POST',{})[0],404)
    def test_object_chat_draft_waits_for_final_question(self):
        state=self.get('/overview');ref={'kind':'account','id':state['accounts'][0]['id'],'version':state['version']}
        payload={'question':'','draft_only':True,'reference':ref,'return_view':{'page':'account','view':'account-detail','object_id':ref['id'],'scrollposition':120}}
        count=len(self.agent.calls);cid=self.post('/page-contexts',payload)['context_id']
        draft=self.post('/page-contexts/'+cid+'/claim',{})
        self.assertTrue(draft['open_once']);self.assertFalse(draft['send_once']);self.assertEqual(draft['question'],'')
        self.assertEqual(draft['return_view']['scrollposition'],120)
        self.assertFalse(self.post('/page-contexts/'+cid+'/claim',{})['open_once'])
        self.assertEqual(self.b.call('/api/agent','POST',{'text':'解释账户','page_context_id':cid})[0],409)
        self.assertEqual(len(self.agent.calls),count)
        final=self.post('/page-contexts',{'question':'只查询可用余额','reference':ref,'return_view':payload['return_view']})['context_id']
        status,result,_=self.b.call('/api/agent','POST',{'text':'只查询可用余额','page_context_id':final})
        self.assertEqual(status,200,result);self.assertEqual(len(self.agent.calls),count+1)
        self.assertEqual(self.agent.calls[-1][1],'只查询可用余额')
    def test_draft_still_has_workspace_and_privacy_boundary(self):
        cid=self.post('/page-contexts',{'question':'可编辑的问题','draft_only':True,'return_view':{'page':'mine','view':'home'}})['context_id']
        other=Browser(self.base);other.start();self.assertEqual(other.call('/api/finance/page-contexts/'+cid+'/claim','POST',{})[0],404)
        self.post('/memories/settings',{'enabled':True});self.post('/page-contexts/'+cid+'/claim',{},409)
        self.post('/page-contexts',{'question':'','draft_only':True,'return_view':{'page':'mine','view':'home'}},400)
    def test_context_privacy_revision_invalidates(self):
        cid=self.make_context();self.post('/memories/settings',{'enabled':True})
        self.post('/page-contexts/'+cid+'/claim',{},409)
    def test_reject_private_return_url_and_unknown_reference(self):
        self.post('/page-contexts',{'question':'看看','return_view':{'page':'account','view':'home','return_url':'https://evil.example'}},400)
        self.post('/page-contexts',{'question':'看看','return_view':{'page':'account','view':'home'},'reference':{'kind':'account','id':'not-current-account'}},404)
    def test_reset_session_is_real_separate_workspace(self):
        other=self.post('/session/reset',{'confirmed':True});self.assertNotEqual(other['workspace_id'],self.w)
        self.b.csrf=other['csrf'];self.assertEqual(self.get('/overview')['workspace_id'],other['workspace_id'])
    def test_research_plan_typed_product_refs(self):
        products=self.get('/products')[:2]
        refs=[{'kind':'product','source_scope':'bank_product','id':p['id'],'version':p['version'],'label':p['name']} for p in products]
        plan=self.post('/research-plans',{'name':'准备研究','products':refs,'assumptions':'先保留应急资金','confirmed':True})['plan']
        self.assertEqual(plan['status'],'candidate');self.assertEqual(plan['execution']['covered'],0)
        read=self.get('/research-plans');self.assertEqual(read['items'][0]['id'],plan['id'])
        from agent_runtime.bank_tools import BankTools
        tool=BankTools(self.server.finance,self.w,'site-plan-read')
        evidence=tool.invoke('research_plans',{'plan_id':plan['id']})
        self.assertEqual(evidence['data']['items'][0]['id'],plan['id']);self.assertTrue(tool.advice)
        with self.assertRaises(Exception):tool.invoke('research_plans',{'plan_id':'missing'})
        self.post('/page-contexts',{'question':'解释这个方案','reference':{'kind':'research_plan','id':plan['id'],'version':plan['object_version']},'return_view':{'page':'recommend','view':'plan','object_id':plan['id']}})

    def test_reference_holding_and_authoritative_source(self):
        overview=self.get('/overview');holding=overview['holdings'][0]
        payload={'question':'看看持仓','return_view':{'page':'account','view':'holding-detail'},'reference':{'kind':'holding','id':holding['account_id']+'|'+holding['product_id'],'source_scope':'public_nav','version':overview['version']}}
        cid=self.post('/page-contexts',payload)['context_id']
        self.assertEqual(self.post('/page-contexts/'+cid+'/claim',{})['reference']['source_scope'],'simulation')

    def test_goal_date_requires_canonical_format(self):
        goals=self.get('/goals');self.post('/goals/prepare',{'action':'create','name':'日期校验','target_minor':1000,'due_on':'20261101','expected_version':goals['version'],'idempotency_key':'date_goal_key'},400)

    def test_research_missing_version_is_not_saved(self):
        products=self.get('/products')[:2]
        refs=[{'kind':'product','source_scope':'bank_product','id':p['id']} for p in products]
        self.post('/research-plans',{'name':'不能确认版本','products':refs,'assumptions':'测试','confirmed':True},400)
        self.assertEqual(self.get('/research-plans')['items'],[])

    def test_public_product_unverifiable_version_is_rejected(self):
        g=self.server.finance;catalog={'products':[],'verified_nav_products':[{'product':{'fund_id':'public-no-version'}}]}
        with patch.object(g.public_products,'read',return_value=catalog):
            self.post('/page-contexts',{'question':'查资料','return_view':{'page':'recommend','view':'product'},'reference':{'kind':'product','id':'public-no-version','source_scope':'public_nav','version':'old'}},409)

    def test_imported_calendar_cannot_claim_historical_portfolio(self):
        status,data,_=self.b.call('/api/finance/calendar?source=imported&portfolio=historical&from=2026-10-01&to=2026-10-05')
        self.assertEqual(status,409,data);self.assertEqual(data['error']['code'],'CALENDAR_SOURCE_PORTFOLIO')

if __name__=='__main__':unittest.main()
