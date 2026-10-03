"""真实HTTP接入测试：会话隔离、授权、防重放、流动性、状态与页面暴露边界。"""
import http.cookiejar
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import finance_agent
from server import create_server, ProxyHandler


class Browser:
    def __init__(self,base):
        self.base=base
        self.cookies=http.cookiejar.CookieJar()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf=None

    def call(self,path,method='GET',body=None,headers=None,raw=None):
        h={'Content-Type':'application/json'}
        if self.csrf:h['X-QZ-CSRF']=self.csrf
        h.update(headers or {})
        data=raw if raw is not None else json.dumps(body or {}).encode() if method=='POST' else None
        req=urllib.request.Request(self.base+path,data=data,headers=h,method=method)
        try: result=self.client.open(req,timeout=10)
        except urllib.error.HTTPError as e:result=e
        with result:
            text=result.read().decode()
            return result.status,json.loads(text) if result.headers.get_content_type()=='application/json' else text,result.headers

    def start(self):
        status,s,headers=self.call('/api/finance/session','POST',{})
        assert status==200
        self.csrf=s['csrf']
        return s,headers


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.quiet=patch.object(ProxyHandler,'log_message',lambda *args:None);cls.quiet.start()
        cls.server=create_server(0,Path(cls.tmp.name)/'bank.sqlite3')
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(5);cls.quiet.stop();cls.tmp.cleanup()

    def setUp(self):
        self.b=Browser(self.base);self.session,self.cookie_headers=self.b.start()
        self.g=self.server.finance;self.w=self.session['workspace_id']
        self.initial=self.b.call('/api/finance/overview')[1]
        self.account=next(a['id'] for a in self.initial['accounts'] if a['id'].endswith('-main'))

    def draft(self,action='purchase',product='SIM-CASH-01',amount=300000,key='test-draft',**extra):
        return dict(action=action,product_id=product,account_id=self.account,amount_minor=amount,idempotency_key=key,**extra)

    def prepare(self,**kwargs):
        status,view,_=self.b.call('/api/finance/operations/prepare','POST',self.draft(**kwargs));self.assertEqual(status,200);return view

    def confirm(self,view,pin=True,**extra):
        op=view['operation'];payload={'confirmed':True,'confirmation_digest':op['confirmation_digest'],**extra}
        if pin:payload['verification_code']=self.b.call('/api/finance/demo-code')[1]['code']
        return self.b.call('/api/finance/operations/'+op['id']+'/confirm','POST',payload)

    def test_session_cookie_persistence_and_isolation(self):
        cookie=self.cookie_headers.get('Set-Cookie');self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        self.assertNotIn('token',json.dumps(self.session));self.assertNotIn('code',json.dumps(self.session))
        s,_=self.b.start();self.assertEqual(s['workspace_id'],self.w)
        other=Browser(self.base);second,_=other.start();self.assertNotEqual(second['workspace_id'],self.w)
        v=self.prepare();self.confirm(v)
        self.assertEqual(other.call('/api/finance/overview')[1]['total_cash_minor'],2439000)
        self.assertEqual(other.call('/api/finance/operations/'+v['operation']['id'])[0],404)

    def test_auth_origin_and_csrf(self):
        empty=Browser(self.base);self.assertEqual(empty.call('/api/finance/overview')[0],401)
        self.assertEqual(self.b.call('/api/finance/operations/prepare','POST',self.draft(),headers={'X-QZ-CSRF':''})[0],403)
        self.assertEqual(self.b.call('/api/finance/session','POST',{},headers={'Origin':'https://attacker.invalid'})[0],403)
        self.assertEqual(self.b.call('/api/finance/overview',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.b.call('/api/agent','POST',{'text':'余额'},headers={'X-QZ-CSRF':''})[0],403)

    def test_read_monthly_annual_and_real_budget(self):
        self.assertEqual(self.initial['total_cash_minor'],2439000);self.assertEqual(self.initial['upcoming_due_minor'],261400)
        self.assertEqual(self.initial['investable_minor'],1177600)
        _,r,_=self.b.call('/api/agent','POST',{'text':'分析9月账单和异常消费'})
        self.assertTrue(r['bank']);self.assertEqual(r['report']['month'],'2026-09');self.assertEqual(r['bank_actions'],[])
        _,annual,_=self.b.call('/api/finance/annual-analysis?year=2025');self.assertTrue(annual['complete_calendar_year']);self.assertEqual(len(annual['covered_months']),12)
        self.assertEqual(self.b.call('/api/finance/anomalies')[1]['duplicates'].__len__(),1)

    def test_natural_language_never_executes_and_external_catalog_is_excluded(self):
        _,r,_=self.b.call('/api/agent','POST',{'text':'保留一万元，购买模拟活钱产品3000元','profile':{'amount':999999,'risk_level':5}})
        self.assertEqual(len(r['bank_actions']),1);self.assertEqual(r['budget']['reserve_minor'],1000000)
        self.assertEqual(self.b.call('/api/finance/overview')[1]['orders'],[])
        self.assertNotIn('verification_code',json.dumps(r))
        _,r,_=self.b.call('/api/agent','POST',{'text':'确认购买模拟活钱3000元'})
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],2439000)
        status,result,_=self.b.call('/api/finance/operations/prepare','POST',self.draft(product='000001'))
        self.assertEqual(status,404);self.assertEqual(result['error']['code'],'NOT_FOUND')

    def test_purchase_receipt_and_idempotency(self):
        v=self.prepare();self.assertEqual(v['operation']['state'],'pending_confirmation')
        status,result,_=self.confirm(v);self.assertEqual(status,200)
        self.assertEqual(result['overview']['total_cash_minor'],2139000);self.assertEqual(result['overview']['holding_principal_minor'],1100000)
        status,replay,_=self.confirm(v,pin=False);self.assertEqual(status,200);self.assertEqual(replay['operation'],result['operation'])
        self.assertEqual(len(replay['overview']['orders']),1)
        self.assertEqual(self.prepare()['operation']['id'],v['operation']['id'])
        self.assertTrue(result['overview']['integrity']['ok'])

    def test_parallel_confirmations_cannot_double_spend(self):
        first=self.prepare(amount=300000,key='parallel-first')
        second=self.prepare(amount=400000,key='parallel-second')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(self.confirm,[first,second]))
        self.assertEqual(sorted(r[0] for r in results),[200,409])
        state=self.b.call('/api/finance/overview')[1]
        self.assertEqual(len(state['orders']),1)
        self.assertIn(state['total_cash_minor'],[2139000,2039000]);self.assertTrue(state['integrity']['ok'])

    def test_negative_intent_and_amount_precision(self):
        _,result,_=self.b.call('/api/agent','POST',{'text':'不要购买模拟活钱产品3000元'})
        self.assertEqual(result['bank_actions'],[])
        _,result,_=self.b.call('/api/agent','POST',{'text':'购买模拟活钱产品100.01元'})
        self.assertEqual(result['bank_actions'][0]['draft']['amount_minor'],10001)
        self.assertEqual(finance_agent.parse_amount('3千元'),300000)
        self.assertIsNone(finance_agent.parse_amount('100.001元'))

    def test_verification_wrong_digest_and_code_do_not_change_money(self):
        v=self.prepare()
        self.assertEqual(self.confirm(v,pin=False)[0],400)
        self.assertEqual(self.confirm(v,confirmation_digest='wrong')[0],409)
        self.assertEqual(self.confirm(v,pin=False,verification_code='000000')[0],403)
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],2439000)

    def test_guard_requires_reserve_and_known_dues(self):
        status,error,_=self.b.call('/api/finance/operations/prepare','POST',self.draft(amount=1200000))
        self.assertEqual(status,422);self.assertEqual(error['error']['code'],'LIQUIDITY_GUARD')
        status,error,_=self.b.call('/api/finance/operations/prepare','POST',self.draft(liquidity_floor_minor=2200000))
        self.assertEqual(status,422)

    def test_stale_confirmation_and_cancellation(self):
        v=self.prepare(liquidity_floor_minor=1500000)
        profile={**{k:self.initial['profile'][k] for k in ('risk_level','horizon_days','reserve_minor')},'confirmed':True}
        self.assertEqual(self.b.call('/api/finance/profile','POST',profile)[0],200)
        self.assertEqual(self.confirm(v)[0],409)
        self.assertEqual(self.b.call('/api/finance/operations/'+v['operation']['id'])[1]['operation']['state'],'invalidated')
        self.assertEqual(self.b.call('/api/finance/operations/'+v['operation']['id'])[1]['liquidity_floor_minor'],1500000)
        v=self.prepare(key='second');self.assertEqual(self.b.call('/api/finance/operations/'+v['operation']['id']+'/cancel','POST',{})[0],200)
        self.assertEqual(self.confirm(v)[0],409)
        self.assertEqual(self.b.call('/api/finance/overview')[1]['orders'],[])

    def test_close_mandate_does_not_cancel_merchant_contract(self):
        _,r,_=self.b.call('/api/agent','POST',{'text':'关闭视频会员的银行代扣'})
        status,v,_=self.b.call('/api/finance/operations/prepare','POST',r['bank_actions'][0]['draft']);self.assertEqual(status,200)
        self.assertFalse(v['verification_required']);status,result,_=self.confirm(v,pin=False);self.assertEqual(status,200)
        self.assertEqual(result['operation']['result']['merchant_contract_status'],'unchanged')
        self.assertEqual(result['overview']['upcoming_due_minor'],261400);self.assertEqual(result['overview']['total_cash_minor'],2439000)
        self.assertTrue(any('商户合同仍有效' in c['name'] for c in result['overview']['commitments']))

    def test_redeem_and_pending_cash(self):
        v=self.prepare(action='redeem',amount=100000);status,result,_=self.confirm(v);self.assertEqual(status,200)
        self.assertEqual(result['overview']['total_cash_minor'],2539000);self.assertEqual(result['overview']['holding_principal_minor'],700000)
        status,error,_=self.b.call('/api/finance/operations/prepare','POST',self.draft(action='redeem',product='SIM-BOND-90',key='locked'))
        self.assertEqual(status,422);self.assertEqual(error['error']['code'],'HOLDING_LOCKED')
        # 在隔离工作区把业务时钟推进到锁定期结束，验证延迟到账的页面状态字段。
        self.g.bank.advance_days(self.w,73)
        v=self.prepare(action='redeem',product='SIM-BOND-90',amount=100000,key='pending')
        status,result,_=self.confirm(v);self.assertEqual(status,200)
        self.assertEqual(result['operation']['state'],'pending_settlement');self.assertEqual(result['overview']['pending_redemption_minor'],100000)
        self.assertEqual(result['overview']['total_cash_minor'],2539000)

    def test_malformed_payload_and_unsupported_business(self):
        self.assertEqual(self.b.call('/api/finance/profile','POST',raw=b'{"confirmed":true,"confirmed":false}')[0],400)
        self.assertEqual(self.b.call('/api/finance/operations/prepare','POST',self.draft(amount=True))[0],400)
        self.assertEqual(self.b.call('/api/finance/operations/prepare','POST',self.draft(key={}))[0],400)
        self.assertEqual(self.b.call('/api/finance/products?workspace_id=evil')[0],400)
        _,r,_=self.b.call('/api/agent','POST',{'text':'转账1000元'})
        self.assertEqual(r['bank_actions'],[]);self.assertIn('尚未接入',r['reply'])

    def test_only_public_static_files_are_served(self):
        for path in ['/server.py','/finance_gateway.py','/simulated_bank/fixtures/demo.json','/outputs/','/tests/test_bank_integration.py','/js/../server.py','/js/%2e%2e/server.py']:
            self.assertEqual(self.b.call(path)[0],404,path)
            self.assertEqual(self.b.call(path,'HEAD')[0],404,path)
        for path in ['/account.html','/chat.html','/recommend.html','/js/bank_ui.js','/css/bank.css','/assets/logo.png']:
            # 二进制logo不经过Browser的文本解析。
            if path.endswith('.png'):continue
            self.assertEqual(self.b.call(path)[0],200,path)

    def test_bank_failure_has_no_fake_success_or_private_error(self):
        with patch.object(self.g,'overview',side_effect=RuntimeError('sensitive-internal-detail')):
            status,data,_=self.b.call('/api/agent','POST',{'text':'购买模拟活钱3000元'})
        self.assertEqual(status,500);self.assertNotIn('sensitive',json.dumps(data));self.assertIn('没有确认执行',data['reply'])
        self.assertEqual(self.b.call('/api/finance/overview')[1]['orders'],[])


if __name__=='__main__': unittest.main()
