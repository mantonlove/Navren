import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from finance_gateway import FinanceGateway
from public_products import PublicProductCatalog
from simulated_bank import BankError
from server import create_server
from test_bank_integration import Browser
from test_native_conversation import ScriptedPrimary, Reviewer, call, fact_reply
from agent_runtime.conversation import ConversationAgent


class PersonalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.g=FinanceGateway(Path(self.tmp.name)/'bank.sqlite')
        self.w,_=self.g.create_session()

    def tearDown(self):self.tmp.cleanup()

    def test_graph_uses_records_and_does_not_invent_preferences(self):
        graph=self.g.personal_graph.read(self.w)
        self.assertGreater(len(graph['nodes']),20)
        self.assertTrue(any(n['group']=='receipt' for n in graph['nodes']))
        self.assertTrue(any(n['group']=='payment' for n in graph['nodes']))
        self.assertFalse(any(n['group']=='preference' for n in graph['nodes']))
        text=json.dumps(graph)
        for field in ('masked_mobile','masked_number','password','token','details'):
            self.assertNotIn('"'+field+'"',text)
        ids={n['id'] for n in graph['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in graph['links']))

    def test_conversation_capture_consent_confirmation_and_workspace_isolation(self):
        args={'category':'goal','value':'准备留学','quote':'我计划明年去留学'}
        self.assertFalse(self.g.personal_graph.remember(self.w,args['quote'],'ref',args)['saved'])
        self.g.memories.set_enabled(self.w,{'enabled':True})
        with self.assertRaises(BankError):self.g.personal_graph.remember(self.w,'别人准备买房','ref',args)
        self.g.personal_graph.remember(self.w,args['quote'],'ref',args)
        graph=self.g.personal_graph.read(self.w)
        node=next(n for n in graph['nodes'] if n.get('observation_id'))
        self.assertEqual(node['status'],'candidate')
        self.assertFalse(any(n.get('observation_id') for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes']))
        other,_=self.g.create_session()
        confirmation={'decision':'confirm','confirmation_token':node['confirmation_token'],
                      'expected_revision':node['expected_revision']}
        with self.assertRaises(BankError):self.g.personal_graph.update(other,node['observation_id'],confirmation)
        self.g.personal_graph.update(self.w,node['observation_id'],confirmation)
        self.assertTrue(any(n.get('observation_id') for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes']))
        self.g.memories.set_enabled(self.w,{'enabled':False})
        self.assertFalse(any(n.get('observation_id') for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes']))
        self.g.personal_graph.update(self.w,node['observation_id'],{'decision':'delete',
            'expected_revision':self.g.memories.state(self.w)['personal_revision']})
        self.assertEqual(self.g.personal_graph.read(self.w)['observation_count'],0)

    def test_official_products_keep_stale_data_and_unknown_fields_explicit(self):
        raw={'extractDate':'2022-07-14','productPresentation':[{
            'name':'官方产品','code':'ABC','saleStatus':'在售','publish':'发行机构',
            'type':'开放式基金','placement':'公募','saleType':'代销','risk':'R2中低风险',
            'investor':'C2稳健型','charge':'详见产品页面','chargeWay':'详见产品页面',
            'customerGroupType':None}]}
        reader=lambda *a,**kw:io.BytesIO(('_jsonpproductpresentation('+json.dumps(raw)+')').encode())
        result=PublicProductCatalog(reader).read()
        self.assertTrue(result['stale']);self.assertEqual(result['status'],'historical')
        product=result['products'][0]
        self.assertIsNone(product['annual_return']);self.assertIsNone(product['current_sale_status'])
        self.assertFalse(product['executable'])
        self.assertFalse(PublicProductCatalog(lambda *a,**kw:(_ for _ in ()).throw(OSError())).read()['products'])

    def test_real_runtime_events_hide_model_reasoning_and_finish_with_result(self):
        primary=ScriptedPrimary(call('account_summary',{}),fact_reply)
        agent=ConversationAgent(primary=primary,reviewer=Reviewer())
        events=[]
        result=agent.process(self.g,self.w,'账户余额',on_event=events.append)
        self.assertFalse(result.get('isFallback'))
        self.assertEqual([e['name'] for e in events if e['kind']=='tool_start'],['account_summary','answer_facts'])
        self.assertFalse(any('reasoning' in str(e) or '余额' in str(e) for e in events))
        server=create_server(0,Path(self.tmp.name)/'http',conversation_agent=ConversationAgent(
            primary=ScriptedPrimary(call('account_summary',{}),fact_reply),reviewer=Reviewer()))
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            browser=Browser('http://127.0.0.1:'+str(server.server_port));browser.start()
            status,body,_=browser.call('/api/agent','POST',{'text':'查询余额'},headers={'Accept':'text/event-stream'})
            self.assertEqual(status,200)
            messages=[json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]
            self.assertEqual(messages[0]['type'],'RUN_STARTED');self.assertEqual(messages[-1]['type'],'RUN_FINISHED')
            self.assertTrue(any(m.get('name')=='navren.result' and m['value']['reply'] for m in messages))
            self.assertNotIn('toolCallArgs',body)
        finally:server.shutdown();server.server_close()
