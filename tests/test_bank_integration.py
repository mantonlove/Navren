"""真实HTTP接入测试：会话隔离、授权、防重放、流动性、状态与页面暴露边界。"""
import http.cookiejar
import hashlib
import io
import json
import tempfile
import threading
import time
from datetime import date
from contextlib import redirect_stderr
from concurrent.futures import ThreadPoolExecutor
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

import finance_agent
from server import create_server, ProxyHandler
from base_runtime.service_assembly import AssembledGateway
try:  # 兼容 `python -m unittest tests.xxx` 与 CI 把 tests/ 放进顶层路径两种入口。
    from tests import sms_fixture
except ImportError:  # pragma: no cover - 由 tests/ 作为顶层路径时走这里。
    import sms_fixture
from simulated_bank import BankError
from agent_runtime.cashflow_finance import deterministic_cashflow_agents


class LegacyFinanceRegression:
    """仅用于冻结旧行为对照；生产入口不再加载旧关键词路由。"""
    def __init__(self, models): self.models=models
    def process(self,gateway,workspace,text,**kwargs):
        return finance_agent.process(gateway,workspace,text,model_agents=self.models)


class Browser:
    def __init__(self,base):
        self.base=base
        self.cookies=http.cookiejar.CookieJar()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf=None
        self._conversation_scope=None

    def conversation_scope(self):
        if self._conversation_scope is None:
            status,data,_=self.call('/api/finance/dialogue/new-conversation','POST',{})
            assert status==200,(status,data)
            self._conversation_scope=data['conversation_scope']
        return self._conversation_scope

    def chat(self,body,**kwargs):
        body=dict(body);body.setdefault('conversation_scope',self.conversation_scope())
        result=self.call('/api/agent','POST',body,**kwargs)
        # 修改记忆/个人资料会推进 revision，使旧范围失效；界面此时点「新对话」，测试同样重取。
        if (result[0]==409 and isinstance(result[1],dict)
                and result[1].get('error',{}).get('code')=='CONVERSATION_SCOPE_STALE'):
            self._conversation_scope=None
            body['conversation_scope']=self.conversation_scope()
            result=self.call('/api/agent','POST',body,**kwargs)
        return result

    def call(self,path,method='GET',body=None,headers=None,raw=None,bind_personal_version=True):
        # 正常界面先读取当前个人资料版本；缺版本拒绝的反例由私库/基座专测覆盖。
        if bind_personal_version and method=='POST' and isinstance(body,dict) and 'expected_revision' not in body and path.startswith((
                '/api/finance/memories','/api/finance/memory-relations/','/api/finance/profile-observations/')):
            status,state,_=self.call('/api/finance/memories')
            if status!=200:raise AssertionError(('personal version bootstrap',status,state))
            body={**body,'expected_revision':state['personal_revision']}
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
        models=deterministic_cashflow_agents()
        cls.now=[time.time()];cls.clock=lambda:cls.now[0]
        cls.sms_lock=threading.Lock()
        cls.provider=sms_fixture.LocalSms(cls.clock)
        def factory(database,**options):
            gateway=AssembledGateway(database,sms_provider=cls.provider,
                                     allow_test_sms_provider=True,**options)
            # 短信发送限流按银行时钟判定；测试用可控时钟推进窗口，不改变生产判定逻辑。
            gateway.bank.clock=cls.clock
            return gateway
        cls.finance_factory=factory
        cls.server=create_server(0,Path(cls.tmp.name)/'bank.sqlite3',model_agents=models,
                                 conversation_agent=LegacyFinanceRegression(models),
                                 finance_factory=factory)
        cls.registered_user_id=uuid.uuid4().hex
        cls.server.finance.configure_dialogue_archive(
            lambda workspace,_db:{'registered_user_id':cls.registered_user_id,
                                  'workspace_id':workspace,'persona_id':'tycoon'})
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

    def prepare_payload(self,payload):
        status,view,_=self.b.call('/api/finance/operations/prepare','POST',payload)
        self.assertEqual(status,200,view)
        return view

    def confirm(self,view,pin=True,**extra):
        op=view['operation'];payload={'confirmed':True,'confirmation_digest':op['confirmation_digest'],**extra}
        if pin:
            path='/api/finance/operations/'+op['id']+'/sms'
            for _ in range(3):
                status,data,headers=self.b.call(path,'POST',{})
                if status!=403 or data.get('error',{}).get('code')!='SMS_SEND_RATE_LIMITED':break
                with self.sms_lock:self.now[0]+=61
            # 操作已失效/已取消时短信挑战也会被拒；调用方仍应看到 409。
            if status!=200:return status,data,headers
            payload['verification_code']=sms_fixture.CODE
            payload['challenge_id']=data['challenge_id']
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
        self.assertEqual(self.b.chat({'text':'余额'},headers={'X-QZ-CSRF':''})[0],403)

    def test_user_memory_is_opt_in_scoped_editable_and_only_used_by_cashflow(self):
        status,state,_=self.b.call('/api/finance/memories')
        self.assertEqual(status,200);self.assertFalse(state['enabled']);self.assertEqual(state['items'],[])
        status,state,_=self.b.call('/api/finance/memories','POST',{
            'category':'planned_commitment','value':'两个月后要预留学费 20,000 元'})
        self.assertEqual(status,200);self.assertEqual(len(state['items']),1)
        item=state['items'][0]
        self.assertEqual(item['origin'],'user_declared');self.assertTrue(item['user_confirmed'])

        _,without_memory,_=self.b.chat({'text':'查看未来30天资金时间轴'})
        self.assertEqual(without_memory['user_memory_context'],[])
        self.assertNotIn(item['value'],without_memory['reply'])

        status,enabled,_=self.b.call('/api/finance/memories/settings','POST',{'enabled':True})
        self.assertEqual(status,200);self.assertTrue(enabled['enabled'])
        _,with_memory,_=self.b.chat({'text':'查看未来30天资金时间轴'})
        used=with_memory['agent_workflow']['memory_context']
        self.assertEqual([row['node_id'] for row in used],[item['node_id']])
        self.assertTrue(any(row['evidence_id']==item['node_id'] for row in with_memory['agent_workflow']['evidence']))
        self.assertIn('不改变账户余额和现金流计算',with_memory['reply'])
        self.assertEqual(with_memory['bank_actions'],[])

        _,bill_answer,_=self.b.chat({'text':'分析9月账单和异常消费'})
        self.assertNotIn('user_memory_context',bill_answer)
        self.assertEqual(self.b.call('/api/finance/memories/settings','POST',{'enabled':False})[1]['enabled'],False)
        _,disabled_answer,_=self.b.chat({'text':'查看未来30天资金时间轴'})
        self.assertEqual(disabled_answer['user_memory_context'],[])

        status,corrected,_=self.b.call('/api/finance/memories/'+item['node_id']+'/correct','POST',
                                        {'value':'两个月后要预留学费 22,000 元'})
        self.assertEqual(status,200);self.assertEqual(corrected['items'][0]['value'],'两个月后要预留学费 22,000 元')
        status,removed,_=self.b.call('/api/finance/memories/'+item['node_id']+'/delete','POST',{})
        self.assertEqual(status,200);self.assertEqual(removed['items'],[])

        other=Browser(self.base);other.start()
        self.assertEqual(other.call('/api/finance/memories')[1]['items'],[])

    def test_expired_memory_is_visible_but_not_passed_to_cashflow_agent(self):
        status,state,_=self.b.call('/api/finance/memories','POST',{
            'category':'planned_commitment','value':'临时支出目标','expires_on':'2026-10-03'})
        self.assertEqual(status,200)
        item=state['items'][0]
        self.assertEqual(item['expires_on'],'2026-10-03')
        self.assertEqual(self.b.call('/api/finance/memories/settings','POST',{'enabled':True})[0],200)
        _,active,_=self.b.chat({'text':'查看未来30天资金时间轴'})
        self.assertEqual(len(active['user_memory_context']),1)

        self.g.bank.advance_days(self.w,1)

        state=self.b.call('/api/finance/memories')[1]
        self.assertTrue(state['items'][0]['expired'])
        self.assertEqual(state['items'][0]['node_id'],item['node_id'])
        _,expired,_=self.b.chat({'text':'查看未来30天资金时间轴'})
        self.assertEqual(expired['user_memory_context'],[])

    def test_read_monthly_annual_and_real_budget(self):
        self.assertEqual(self.initial['total_cash_minor'],2439000);self.assertEqual(self.initial['upcoming_due_minor'],261400)
        self.assertEqual(self.initial['investable_minor'],1177600)
        _,r,_=self.b.chat({'text':'分析9月账单和异常消费'})
        self.assertTrue(r['bank']);self.assertEqual(r['report']['month'],'2026-09');self.assertEqual(r['bank_actions'],[])
        _,annual,_=self.b.call('/api/finance/annual-analysis?year=2025');self.assertTrue(annual['complete_calendar_year']);self.assertEqual(len(annual['covered_months']),12)
        self.assertEqual(self.b.call('/api/finance/anomalies')[1]['duplicates'].__len__(),1)

    def test_balance_query_does_not_read_out_unsolicited_product_catalog(self):
        _,balance,_=self.b.chat({'text':'查看我的模拟账户余额'})
        self.assertEqual(balance['products'],[])
        self.assertIn('现金合计 24,390.00元',balance['reply'])
        self.assertIn('可投资 10,276.00元',balance['reply'])
        self.assertNotIn('products.catalog',balance['capabilities_used'])
        self.assertNotIn('SIM-BOND-30',balance['reply'])
        _,comparison,_=self.b.chat({'text':'对比模拟活钱和30天产品'})
        self.assertEqual({product['id'] for product in comparison['products']},{'SIM-CASH-01','SIM-BOND-30'})

    def test_csv_preview_commit_source_switch_and_agent_use_imported_dataset(self):
        csv_text=('流水号,日期,商户,金额,类型,分类,币种,备注\n'
                  'outside-1,2026-09-01,本地咖啡,12.34,支出,餐饮,CNY,\n'
                  'outside-2,2026-09-02,本地薪资,100.00,收入,工资,CNY,\n')
        initial_source=self.b.call('/api/finance/source')[1]
        self.assertEqual(initial_source['active_source'],'simulation')
        self.assertEqual(initial_source['imported_available'],False)
        self.assertEqual(self.b.call('/api/finance/source','POST',{'source':'imported'})[0],409)
        before=self.b.call('/api/finance/overview')[1]
        status,preview,_=self.b.call('/api/finance/statements/preview','POST',raw=csv_text.encode(),
                                     headers={'Content-Type':'text/csv; charset=utf-8'})
        self.assertEqual(status,200);self.assertTrue(preview['valid']);self.assertEqual(preview['new_count'],2)
        self.assertEqual(self.b.call('/api/finance/source')[1]['active_source'],'simulation')
        self.assertEqual(self.b.call('/api/finance/statements/commit','POST',
                         {'batch_id':preview['batch_id'],'confirmed':False})[0],400)
        status,committed,_=self.b.call('/api/finance/statements/commit','POST',
                         {'batch_id':preview['batch_id'],'confirmed':True})
        self.assertEqual(status,200);self.assertEqual(committed['inserted_count'],2)
        self.assertTrue(self.b.call('/api/finance/statements/commit','POST',
                         {'batch_id':preview['batch_id'],'confirmed':True})[1]['idempotent_replay'])
        self.assertEqual(self.b.call('/api/finance/source','POST',{'source':'imported'})[1]['active_source'],'imported')
        report=self.b.call('/api/finance/analysis?month=2026-09')[1]
        self.assertEqual(report['source'],'imported_local_csv')
        self.assertEqual(report['totals_minor']['expense'],1234)
        self.assertEqual(report['totals_minor']['income'],10000)
        self.assertEqual(self.b.call('/api/finance/transactions')[1][0]['id'],'outside-1')
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],before['total_cash_minor'])
        _,answer,_=self.b.chat({'text':'分析2026年9月账单消费'})
        self.assertIn('本地导入数据集',answer['reply'])
        self.assertEqual(answer['statement_source']['active_source'],'imported')
        self.assertEqual(answer['report']['totals_minor']['expense'],1234)
        self.assertIsNone(answer['budget'])
        self.assertTrue(any(step['tool']=='bill_analysis' and step['source']=='imported_local_csv'
                            for step in answer['agent_chain']))
        self.assertTrue(any(e['event']=='statement_import_committed' for e in self.b.call('/api/finance/audit')[1]))
        contract=self.b.call('/api/finance/data-contract')[1]
        self.assertTrue({'statement_datasets','imported_statement_transactions','statement_import_batches',
                         'finance_data_sources','imported_periodic_marks'} <= set(contract['database_tables']))
        capabilities=self.b.call('/api/finance/capabilities')[1]
        self.assertIn('statement_csv_import',capabilities['enabled'])

    def test_natural_language_never_executes_and_external_catalog_is_excluded(self):
        _,r,_=self.b.chat({'text':'保留一万元，购买模拟活钱产品3000元','profile':{'amount':999999,'risk_level':5}})
        self.assertEqual(len(r['bank_actions']),1);self.assertEqual(r['budget']['reserve_minor'],1000000)
        self.assertEqual(self.b.call('/api/finance/overview')[1]['orders'],[])
        self.assertNotIn('verification_code',json.dumps(r))
        _,r,_=self.b.chat({'text':'确认购买模拟活钱3000元'})
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
        _,result,_=self.b.chat({'text':'不要购买模拟活钱产品3000元'})
        self.assertEqual(result['bank_actions'],[])
        _,result,_=self.b.chat({'text':'购买模拟活钱产品100.01元'})
        self.assertEqual(result['bank_actions'][0]['draft']['amount_minor'],10001)
        self.assertEqual(finance_agent.parse_amount('3千元'),300000)
        self.assertIsNone(finance_agent.parse_amount('100.001元'))

    def test_verification_wrong_digest_and_code_do_not_change_money(self):
        v=self.prepare()
        self.assertEqual(self.confirm(v,pin=False)[0],403)
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
        _,r,_=self.b.chat({'text':'关闭视频会员的银行代扣'})
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

    def test_card_controls_require_confirmation_and_never_change_cash(self):
        state=self.b.call('/api/finance/overview')[1]
        card=next(c for c in state['cards'] if c['type']=='debit' and c['status']=='active')
        control=next(c for c in card['controls'] if c['control_type']=='overseas')
        payload={'action':'card_control','account_id':card['account_id'],'card_id':card['id'],
                 'control_type':'overseas','enabled':True,'idempotency_key':'card-overseas-on'}
        view=self.prepare_payload(payload)
        self.assertEqual(view['operation']['permission'],'yellow')
        self.assertFalse(next(c for c in view['card']['controls'] if c['control_type']=='overseas')['enabled'])
        status,result,_=self.confirm(view,pin=False)
        self.assertEqual(status,200);self.assertEqual(result['operation']['result']['enabled'],True)
        updated=next(c for c in result['overview']['cards'] if c['id']==card['id'])
        self.assertTrue(next(c for c in updated['controls'] if c['control_type']=='overseas')['enabled'])
        self.assertEqual(result['overview']['total_cash_minor'],state['total_cash_minor'])
        self.assertEqual(result['overview']['orders'],state['orders'])
        _,replay,_=self.confirm(view,pin=False)
        replay_card=next(c for c in replay['overview']['cards'] if c['id']==card['id'])
        self.assertTrue(next(c for c in replay_card['controls'] if c['control_type']=='overseas')['enabled'])

    def test_card_application_and_limit_request_do_not_issue_or_raise_credit(self):
        state=self.b.call('/api/finance/overview')[1]
        credit=next(c for c in state['cards'] if c['type']=='credit' and c['status']=='active')
        request=self.prepare_payload({'action':'request_card_limit','account_id':credit['account_id'],'card_id':credit['id'],
                                      'requested_limit_minor':1500000,'idempotency_key':'card-limit-request'})
        self.assertEqual(request['operation']['permission'],'red')
        status,result,_=self.confirm(request)
        self.assertEqual(status,200)
        self.assertEqual(result['operation']['result']['status'],'submitted')
        updated=next(c for c in result['overview']['cards'] if c['id']==credit['id'])
        self.assertEqual(updated['credit_limit_minor'],credit['credit_limit_minor'])
        self.assertTrue(any(r['requested_limit_minor']==1500000 and r['status']=='submitted' for r in updated['limit_requests']))

        application=self.prepare_payload({'action':'apply_card','account_id':credit['account_id'],
                                           'requested_type':'debit','requested_limit_minor':0,
                                           'idempotency_key':'new-card-application'})
        status,result,_=self.confirm(application)
        self.assertEqual(status,200);self.assertEqual(result['operation']['result']['status'],'submitted')
        self.assertEqual(len(result['overview']['cards']),len(state['cards']))
        self.assertTrue(any(a['requested_type']=='debit' and a['status']=='submitted'
                            and a['decision_reason']=='仅受理模拟申请；未生成卡片或授信结果。'
                            for a in result['overview']['card_applications']))
        self.assertIn('尚未审批',result['operation']['result']['notice'])

    def test_card_lost_report_turns_off_controls_and_cannot_be_undone_as_a_toggle(self):
        state=self.b.call('/api/finance/overview')[1]
        card=next(c for c in state['cards'] if c['type']=='debit' and c['status']=='active')
        view=self.prepare_payload({'action':'report_card_lost','account_id':card['account_id'],'card_id':card['id'],
                                   'idempotency_key':'report-card-lost'})
        self.assertEqual(view['operation']['permission'],'red')
        before=state['total_cash_minor']
        status,result,_=self.confirm(view)
        self.assertEqual(status,200);self.assertEqual(result['operation']['result']['status'],'lost')
        updated=next(c for c in result['overview']['cards'] if c['id']==card['id'])
        self.assertEqual(updated['status'],'lost')
        self.assertTrue(all(not control['enabled'] for control in updated['controls']))
        self.assertEqual(result['overview']['total_cash_minor'],before)
        self.assertTrue(any(event['event_type']=='lost' and event['reason']=='用户确认的模拟挂失'
                            for event in updated['events']))
        status,error,_=self.b.call('/api/finance/operations/prepare','POST',
            {'action':'card_control','account_id':card['account_id'],'card_id':card['id'],
             'control_type':'online','enabled':True,'idempotency_key':'lost-card-online-on'})
        self.assertEqual(status,409);self.assertEqual(error['error']['code'],'CARD_NOT_ACTIVE')

    def test_replacement_card_application_is_bound_to_lost_card(self):
        state=self.b.call('/api/finance/overview')[1]
        lost=next(c for c in state['cards'] if c['status']=='lost')
        wrong_type='credit' if lost['type']=='debit' else 'debit'
        status,error,_=self.b.call('/api/finance/operations/prepare','POST',{
            'action':'apply_card','account_id':lost['account_id'],'card_id':lost['id'],
            'requested_type':wrong_type,'requested_limit_minor':500000 if wrong_type=='credit' else 0,
            'idempotency_key':'replacement-wrong-type'})
        self.assertEqual(status,422);self.assertEqual(error['error']['code'],'INVALID_REPLACEMENT_CARD')

        requested_limit=lost['credit_limit_minor'] if lost['type']=='credit' else 0
        application=self.prepare_payload({'action':'apply_card','account_id':lost['account_id'],'card_id':lost['id'],
            'requested_type':lost['type'],'requested_limit_minor':requested_limit,
            'idempotency_key':'replacement-correct-type'})
        self.assertEqual(application['operation']['permission'],'red')
        status,result,_=self.confirm(application)
        self.assertEqual(status,200)
        linked=next(a for a in result['overview']['card_applications']
                    if a['id']==result['operation']['result']['application_id'])
        self.assertEqual(linked['card_id'],lost['id'])
        self.assertEqual(linked['status'],'submitted')
        self.assertIn('补卡申请',result['operation']['result']['notice'])

    def test_malformed_payload_and_unsupported_business(self):
        self.assertEqual(self.b.call('/api/finance/profile','POST',raw=b'{"confirmed":true,"confirmed":false}')[0],400)
        self.assertEqual(self.b.call('/api/finance/operations/prepare','POST',self.draft(amount=True))[0],400)
        self.assertEqual(self.b.call('/api/finance/operations/prepare','POST',self.draft(key={}))[0],400)
        self.assertEqual(self.b.call('/api/finance/products?workspace_id=evil')[0],400)
        _,r,_=self.b.chat({'text':'转账1000元'})
        self.assertEqual(r['bank_actions'],[]);self.assertIn('明确的模拟收款人',r['reply'])
        _,r,_=self.b.chat({'text':'转给妈妈1000元'})
        self.assertEqual(r['bank_actions'][0]['draft']['action'],'transfer')
        self.assertEqual(r['bank_actions'][0]['draft']['amount_minor'],100000)
        view=self.prepare_payload(r['bank_actions'][0]['draft'])
        self.assertEqual(view['operation']['state'],'pending_confirmation')
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],2439000)

    def test_agent_creates_subscription_and_cross_scene_confirmation_drafts(self):
        _,cancel,_=self.b.chat({'text':'取消视频会员订阅'})
        self.assertEqual(len(cancel['bank_actions']),1)
        self.assertEqual(cancel['bank_actions'][0]['draft']['action'],'cancel_subscription')
        _,task,_=self.b.chat({'text':'帮我安排妈妈生日礼物'})
        self.assertEqual(len(task['bank_actions']),1)
        self.assertEqual(task['bank_actions'][0]['draft']['action'],'reserve_funds')
        self.assertIn('不扣款',task['reply'])

    def test_confirmed_hold_reduces_budget_without_debiting_cash(self):
        quote_ids=[q['id'] for q in self.initial['service_quotes'] if q['status']=='illustrative']
        total=sum(q['price_minor'] for q in self.initial['service_quotes'] if q['id'] in quote_ids)
        task=next(t for t in self.initial['orchestration_tasks'] if t['status']=='draft')
        view=self.prepare_payload({'action':'reserve_funds','account_id':self.account,'task_id':task['id'],
            'amount_minor':total,'expires_on':'2026-10-17','idempotency_key':'integration-hold'})
        status,result,_=self.confirm(view)
        self.assertEqual(status,200)
        self.assertEqual(result['overview']['total_cash_minor'],2439000)
        self.assertEqual(result['overview']['held_funds_minor'],total)
        self.assertEqual(result['overview']['investable_minor'],self.initial['investable_minor']-total)

    def test_split_confirmation_view_includes_each_participant(self):
        split=next(s for s in self.initial['split_bills'] if s['status']=='draft')
        view=self.prepare_payload({'action':'request_split','account_id':self.account,'split_id':split['id'],
            'idempotency_key':'integration-split-view'})
        self.assertEqual(len(view['split']['participants']),3)

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
            status,data,_=self.b.chat({'text':'购买模拟活钱3000元'})
        self.assertEqual(status,500);self.assertNotIn('sensitive',json.dumps(data));self.assertIn('没有确认执行',data['reply'])
        self.assertEqual(self.b.call('/api/finance/overview')[1]['orders'],[])

    def test_commit_with_lost_response_survives_server_restart_and_replays_once(self):
        view=self.prepare(key='lost-response')
        # 直接让银行在提交成功之后抛出：提交已落盘，但客户端拿不到回执。
        original_confirm=self.g.bank.confirm
        def commit_then_lose_response(*args,**kwargs):
            original_confirm(*args,**kwargs)
            raise RuntimeError('simulated response loss after commit')
        with patch.object(self.g.bank,'confirm',side_effect=commit_then_lose_response):
            status,error,_=self.confirm(view)
        self.assertEqual(status,500)
        self.assertEqual(error['error']['code'],'BANK_UNAVAILABLE')
        self.assertNotIn('simulated response loss',json.dumps(error))

        # 用同一持久化数据库重建服务，验证进程重启后仍能查到回执。
        old_server=self.__class__.server
        old_thread=self.__class__.thread
        models=deterministic_cashflow_agents()
        replacement=create_server(0,Path(self.tmp.name)/'bank.sqlite3',model_agents=models,
                                  conversation_agent=LegacyFinanceRegression(models),
                                  finance_factory=self.__class__.finance_factory)
        replacement.finance.configure_dialogue_archive(
            lambda workspace,_db:{'registered_user_id':self.__class__.registered_user_id,
                                  'workspace_id':workspace,'persona_id':'tycoon'})
        replacement_thread=threading.Thread(target=replacement.serve_forever,daemon=True)
        replacement_thread.start()
        old_server.shutdown();old_server.server_close();old_thread.join(5)
        self.__class__.server=replacement
        self.__class__.thread=replacement_thread
        self.__class__.base=f'http://127.0.0.1:{replacement.server_port}'
        self.b.base=self.__class__.base

        # 客户端没有成功回执，但重启后的查询接口从持久化状态核实：资金已扣一次、订单已生成。
        operation_id=view['operation']['id']
        status,recovered,_=self.b.call('/api/finance/operations/'+operation_id)
        self.assertEqual(status,200)
        self.assertEqual(recovered['operation']['state'],'succeeded')
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],2139000)
        replay_status,replay,_=self.confirm(view,pin=False)
        self.assertEqual(replay_status,200)
        self.assertEqual(replay['operation']['id'],operation_id)
        self.assertEqual(len(replay['overview']['orders']),1)
        self.assertEqual([o['operation_id'] for o in replay['overview']['orders']],[operation_id])

    def test_http_timeout_while_backend_continues_recovers_receipt_without_duplicate(self):
        view=self.prepare(key='http-timeout')
        operation_id=view['operation']['id']
        challenge=self.b.call('/api/finance/operations/'+operation_id+'/sms','POST',{})[1]
        body={'confirmed':True,'confirmation_digest':view['operation']['confirmation_digest'],
              'verification_code':sms_fixture.CODE,'challenge_id':challenge['challenge_id']}
        cookie='; '.join(f'{item.name}={item.value}' for item in self.b.cookies)
        request=urllib.request.Request(self.b.base+'/api/finance/operations/'+operation_id+'/confirm',
            data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-QZ-CSRF':self.b.csrf,
                                                    'Cookie':cookie},
            method='POST')
        original=self.g.confirm
        started,finished=threading.Event(),threading.Event()
        def slow_backend(*args,**kwargs):
            started.set()
            time.sleep(.5)
            try:return original(*args,**kwargs)
            finally:finished.set()
        timed_out=False
        with patch.object(self.g,'confirm',side_effect=slow_backend):
            try:urllib.request.urlopen(request,timeout=.2).read()
            except TimeoutError:timed_out=True
            self.assertTrue(timed_out,'客户端应在后端完成前超时')
            self.assertTrue(started.wait(2),'请求未到达后端')
            self.assertTrue(finished.wait(5),'HTTP 客户端断开后后端没有完成操作')

        status,recovered,_=self.b.call('/api/finance/operations/'+operation_id)
        self.assertEqual(status,200)
        self.assertEqual(recovered['operation']['state'],'succeeded')
        self.assertEqual(recovered['operation']['result']['status'],'succeeded')
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],2139000)
        replay_status,replay,_=self.confirm(view,pin=False)
        self.assertEqual(replay_status,200)
        self.assertEqual(replay['operation']['id'],operation_id)
        orders=self.b.call('/api/finance/overview')[1]['orders']
        self.assertEqual([o['operation_id'] for o in orders],[operation_id])
        deadline=time.monotonic()+3
        events=[]
        disconnected=[]
        while time.monotonic()<deadline:
            events=[e for e in self.b.call('/api/finance/audit')[1] if e['operation_id']==operation_id]
            disconnected=[e for e in events if e['event']=='operation_response_disconnected']
            if disconnected:break
            time.sleep(.01)
        self.assertEqual(sum(e['event']=='operation_executed' for e in events),1)
        self.assertEqual(len(disconnected),1,f'断连审计未按时写入；events={events!r}')
        self.assertEqual(disconnected[0]['details']['state'],'succeeded')

    def test_backend_connection_error_is_not_logged_as_client_disconnect(self):
        view=self.prepare(key='backend-connection-error')
        operation_id=view['operation']['id']
        with patch.object(self.g,'confirm',side_effect=ConnectionError('internal operation failure')):
            status,error,_=self.confirm(view)
        self.assertEqual(status,500)
        self.assertEqual(error['error']['code'],'BANK_UNAVAILABLE')
        recovered=self.b.call('/api/finance/operations/'+operation_id)[1]['operation']
        self.assertEqual(recovered['state'],'pending_confirmation')
        events=[e for e in self.b.call('/api/finance/audit')[1] if e['operation_id']==operation_id]
        self.assertFalse(any(e['event']=='operation_response_disconnected' for e in events))

    def test_disconnect_reconciliation_failures_have_safe_correlation_logs(self):
        operation_id='a'*32
        path=f'/api/finance/operations/{operation_id}/confirm'
        workspace_ref=hashlib.sha256(self.w.encode('utf-8')).hexdigest()[:12]
        output=io.StringIO()
        with redirect_stderr(output):
            self.g._audit_disconnected_response(path,{'operation':{'state':''}},self.w)
            with patch.object(self.g.bank,'transaction',side_effect=RuntimeError('private database detail')):
                self.g._audit_disconnected_response(path,{'operation':{'state':'succeeded'}},self.w)
        log=output.getvalue()
        self.assertIn(f'operation={operation_id}',log)
        self.assertIn(f'workspace_ref={workspace_ref}',log)
        self.assertIn('reason=missing_operation_state',log)
        self.assertIn('error=RuntimeError',log)
        self.assertNotIn('private database detail',log)

    def test_cashflow_timeline_is_workspace_scoped_and_horizon_validated(self):
        overview_timeline=self.initial['cashflow_timeline']
        self.assertEqual(overview_timeline['workspace_id'],self.w)
        self.assertEqual(overview_timeline['horizon_days'],90)
        self.assertEqual(overview_timeline['currency'],'CNY')
        self.assertEqual(overview_timeline['source'],'simulated_bank_archive')

        status,timeline,_=self.b.call('/api/finance/cashflow-timeline?days=30')
        self.assertEqual(status,200)
        self.assertEqual(timeline['workspace_id'],self.w)
        self.assertEqual(timeline['horizon_days'],30)
        self.assertEqual((date.fromisoformat(timeline['horizon_end'])-
                          date.fromisoformat(timeline['as_of'])).days,30)

        other=Browser(self.base)
        other_session,_=other.start()
        other_timeline=other.call('/api/finance/cashflow-timeline?days=30')[1]
        self.assertEqual(other_timeline['workspace_id'],other_session['workspace_id'])
        self.assertNotEqual(other_timeline['workspace_id'],self.w)

        status,error,_=self.b.call('/api/finance/cashflow-timeline?days=366')
        self.assertEqual(status,400)
        self.assertEqual(error['error']['code'],'INVALID_HORIZON')
        status,error,_=self.b.call('/api/finance/cashflow-timeline?days=30&days=60')
        self.assertEqual(status,400)
        self.assertEqual(error['error']['code'],'INVALID_QUERY')

    def test_read_capability_registry_routes_agent_queries_with_a_read_only_catalog(self):
        catalog=self.b.call('/api/finance/capabilities')[1]['read_capabilities']
        capability_ids={item['id'] for item in catalog}
        self.assertIn('cashflow.timeline',capability_ids)
        self.assertIn('statements.monthly_analysis',capability_ids)
        self.assertTrue(all(item['read_only'] for item in catalog))
        self.assertTrue(all('execute' not in item['id'] and 'confirm' not in item['id']
                            for item in catalog))

        with self.assertRaises(BankError) as caught:
            self.g.query_read_capability(self.w,'cashflow.timeline',{'days':366})
        self.assertEqual(caught.exception.code,'INVALID_CAPABILITY_ARGUMENTS')
        with self.assertRaises(BankError) as caught:
            self.g.query_read_capability(self.w,'transaction.execute',{})
        self.assertEqual(caught.exception.code,'CAPABILITY_NOT_FOUND')

        status,response,_=self.b.chat({'text':'请展示未来30天资金时间轴'})
        self.assertEqual(status,200)
        self.assertEqual(response['cashflow_timeline']['horizon_days'],30)
        self.assertEqual(response['cashflow_timeline']['workspace_id'],self.w)
        self.assertIn('cashflow.timeline',response['capabilities_used'])
        self.assertIn('simulated_bank.account_snapshot',response['capabilities_used'])
        self.assertFalse(response['bank_actions'])
        self.assertIn('按日终汇总',response['reply'])
        self.assertNotIn('products.catalog',response['capabilities_used'])
        business_ids={item['id'] for item in self.b.call('/api/finance/capabilities')[1]['agent_capabilities']}
        self.assertIn('service_negotiation',business_ids)
        workflow=response['agent_workflow']
        self.assertEqual(workflow['status'],'approved')
        self.assertEqual([task['capability'] for task in workflow['task_graph']],
                         ['account_snapshot','cashflow_projection'])
        self.assertEqual(workflow['task_graph'][1]['depends_on'],['account-snapshot'])
        self.assertTrue(any(item['stage']=='reviewer.review' and item['outcome']=='approve'
                            for item in workflow['trace']))
        evidence_ids={item['evidence_id'] for item in workflow['evidence']}
        self.assertTrue(all(set(claim['evidence_ids'])<=evidence_ids for claim in workflow['claims']))
        self.assertTrue(any(item['evidence_id'].startswith('event:') for item in workflow['evidence']))

    def test_colloquial_balance_question_reads_account_data_without_risk_profile_or_product_recommendation(self):
        status,response,_=self.b.chat({'text':'我还有多少钱'})
        self.assertEqual(status,200)
        self.assertTrue(response['bank'])
        self.assertEqual(response['intent'],'bank_wealth')
        self.assertIn('wealth',response['business_capabilities'])
        self.assertIn('budgets.investable',response['capabilities_used'])
        self.assertNotIn('products.catalog',response['capabilities_used'])
        self.assertEqual(response['products'],[])
        self.assertFalse(response['bank_actions'])
        self.assertNotIn('风险画像',response['reply'])
        self.assertNotIn('风险测评',response['reply'])
        self.assertIn('模拟账户现金合计 24,390.00元',response['reply'])
        self.assertIn('日常账户',response['reply'])
        self.assertIn('备用账户',response['reply'])
        self.assertIn('可投资 10,276.00元',response['reply'])

        _,available_amount,_=self.b.chat({'text':'我有多少钱可以投资'})
        self.assertTrue(available_amount['bank'])
        self.assertEqual(available_amount['products'],[])
        self.assertNotIn('风险画像',available_amount['reply'])

    def test_conversation_memory_is_proposed_but_saved_only_after_explicit_confirmation(self):
        self.assertEqual(self.b.call('/api/finance/memories/settings','POST',{'enabled':True})[0],200)
        status,response,_=self.b.chat({
            'text':'我的长期目标是毕业前存下 30,000 元；请查看余额。'})
        self.assertEqual(status,200)
        self.assertEqual(len(response['memory_proposals']),1)
        self.assertEqual(self.b.call('/api/finance/memories')[1]['items'],[])
        proposal=response['memory_proposals'][0]
        saved_status,saved,_=self.b.call('/api/finance/memories','POST',{
            'source_ref':proposal['source_ref'],
            'proposal_id':proposal['proposal_id'],'confirmation_token':proposal['confirmation_token'],
            'expected_revision':proposal['expected_revision'],
        })
        self.assertEqual(saved_status,200)
        self.assertEqual(saved['items'][0]['source_type'],'conversation_user_statement')

    def test_service_benefit_negotiation_uses_synthetic_rules_and_two_step_confirmation(self):
        initial=self.b.call('/api/finance/overview')[1]['total_cash_minor']
        text='我愿意迁入工资账户，但不接受购买理财作为优惠条件；希望协商服务费减免。'
        _,chat,_=self.b.chat({'text':text})
        self.assertIn('service_negotiation_request',chat)
        status,preview,_=self.b.call('/api/finance/service-negotiations/preview','POST',
                                     {'text':text,'share_memory':False})
        self.assertEqual(status,200)
        self.assertEqual(len(preview['offers']),2)
        self.assertTrue(all(not offer['requires_investment_purchase'] for offer in preview['offers']))
        rejected=self.b.call('/api/finance/service-negotiations/'+preview['draft_id']+'/accept','POST',
                             {'offer_id':preview['offers'][0]['offer_id'],'confirmed':False})
        self.assertEqual(rejected[0],400)
        accepted=self.b.call('/api/finance/service-negotiations/'+preview['draft_id']+'/accept','POST',
                             {'offer_id':preview['offers'][0]['offer_id'],'confirmed':True})
        self.assertEqual(accepted[0],200)
        self.assertEqual(accepted[1]['status'],'submitted_simulation_pending_eligibility')
        self.assertEqual(self.b.call('/api/finance/overview')[1]['total_cash_minor'],initial)

    def test_unapproved_cashflow_hides_projection_numbers_from_api_response(self):
        blocked={
            'status':'blocked','answer':'这项处理暂时无法完成可靠核验。',
            'task_graph':[],'trace':[],'findings':[],'evidence':[],
            'evidence_sources':{},'claims':[],'attempts':0,'bank_actions':[],
        }
        with patch.object(finance_agent,'run_cashflow_workflow',return_value=blocked):
            status,response,_=self.b.chat({'text':'请展示未来30天资金时间轴'})

        self.assertEqual(status,200)
        self.assertEqual(response['agent_workflow']['status'],'blocked')
        self.assertEqual(response['cashflow_timeline']['status'],'withheld')
        self.assertNotIn('accounts',response['cashflow_timeline'])
        self.assertNotIn('¥',response['reply'])


if __name__=='__main__': unittest.main()
