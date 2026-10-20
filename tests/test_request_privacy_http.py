"""实际临时HTTP入口限制；脚本模型只证明边界，零付费调用。"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from agent_runtime.conversation import ConversationAgent
from server import create_server
from voice_gateway import VoiceGateway
from scripts.run_jev_ab import FrozenEvidenceRuntime
from scripts.run_jev_intent_ab import Recorder, digest, packed, sha
from tests.window_integration import semantic_oracle as oracle
from test_native_conversation import ScriptedPrimary, Reviewer, call, last_tool


def finish_hypothetical(messages, schemas):
    output=last_tool(messages)
    ids=[fact['id'] for fact in output['facts'] if fact.get('value')==12345]
    if not ids: raise AssertionError('actual hypothetical fact missing')
    return call('answer_facts',{'fact_ids':ids})


class RequestPrivacyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='navren-privacy-http-')
        self.addCleanup(self.temp.cleanup)
        self.env=oracle._isolated_environment(Path(self.temp.name))
        self.env.__enter__();self.addCleanup(self.env.__exit__,None,None,None)
        self.primary=ScriptedPrimary(call('money_calculation',{'operation':'sum','values_minor':[12345],'currency':'CNY'}),finish_hypothetical)
        catalog=oracle._NoNetworkCatalog();catalog.stop=lambda **kwargs:True
        self.server=create_server(port=0,database=str(Path(self.temp.name)/'bank.sqlite3'),
            market_database=str(Path(self.temp.name)/'market.sqlite3'),
            public_catalog=catalog,public_sync_enabled=False,voice=VoiceGateway(key='',allow_cloud_asr=False),
            conversation_agent=ConversationAgent(self.primary,Reviewer(),tool_routing='progressive'))
        self.addCleanup(self.server.server_close)
        self.g=self.server.finance
        self.server.evidence_runtime.close()
        self.server.evidence_runtime=FrozenEvidenceRuntime(self.g.knowledge_provider)
        self.w,self.token=self.g.create_session()
        self.csrf=self.g.csrf(self.token)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start();self.addCleanup(self.thread.join,2);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def request(self,body):
        request=urllib.request.Request(self.base+'/api/agent',data=json.dumps(body,ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json','Origin':self.base,
                     'Cookie':'qz_bank_user='+self.token,'X-QZ-CSRF':self.csrf})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try: response=opener.open(request,timeout=12)
        except urllib.error.HTTPError as error: response=error
        with response:return response.status,json.load(response)

    def test_actual_http_hypothetical_never_initializes_personal_financial_context(self):
        with ExitStack() as stack:
            for target,name in ((self.g.wealth,'provision'),(self.g.financial_metrics,'snapshot'),
                (self.g.financial_metrics,'validate'),(self.g.bank,'customer_archive'),
                (self.g.personal_data,'read_widgets'),(self.g.personal_data,'register_conversation_source'),
                (self.g.personal_data,'propose_candidates'),(self.g.memories,'state')):
                stack.enter_context(patch.object(target,name,side_effect=AssertionError('forbidden personal initialization:'+name)))
            history=stack.enter_context(patch.object(self.g.personal_data,'read_history',wraps=self.g.personal_data.read_history))
            status,result=self.request({'text':'只计算假设的123.45元，不读取个人账户。'})
        self.assertEqual(status,200,result)
        self.assertFalse(result.get('isFallback'),result)
        self.assertIn('123.45',result['reply'])
        self.assertIsNone(result['financial_metric_snapshot'])
        self.assertEqual(result['financial_metric_batches'],[])
        self.assertEqual(result['memory_proposals'],[])
        self.assertEqual(history.call_count,1)  # only final visible-history append
        self.assertEqual(len(self.primary.messages),2)
        initial=json.dumps(self.primary.messages[0],ensure_ascii=False)
        self.assertNotIn('financial_context',initial)
        self.assertNotIn('calendar_events',initial)
        self.assertNotIn('recent_operations',initial)
        self.assertNotIn('recent_calendar_widgets',initial)
        self.assertNotIn('financial_metric_snapshot',initial)

    def test_conflicting_graph_reference_is_rejected_before_private_lookup(self):
        with patch.object(self.g.financial_graph,'query',side_effect=AssertionError('private graph lookup')):
            status,result=self.request({'text':'不要使用我的账户资料，只计算假设。','graph_context':{'node_id':'private-unresolved'}})
        self.assertEqual(status,403,result)
        self.assertEqual(result['error']['code'],'REQUEST_PRIVATE_DATA_FORBIDDEN')
        self.assertEqual(self.primary.messages,[])

    def test_recorder_pins_exact_stored_json_separately_from_actual_wire(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self):return json.dumps({'model':'DeepSeek-V4-Flash','id':'offline-response',
                'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3},'choices':[]}).encode()
        calls=[]
        recorder=Recorder(Path(self.temp.name)/'recorder',transport=lambda request,**kwargs:(calls.append(request.data) or Response()))
        body={'model':'DeepSeek-V4-Flash','messages':[{'role':'user','content':'explicit hypothetical'}],'max_tokens':10}
        wire=json.dumps(body,separators=(',',':')).encode()
        request=urllib.request.Request('https://aiping.cn/api/v1/chat/completions',data=wire)
        with recorder.opener('primary')(request) as response:response.read()
        self.assertEqual(len(calls),1)
        row=recorder.calls[0]
        saved=Path(self.temp.name)/'recorder/requests/000.json'
        self.assertEqual(row['request_sha256'],sha(wire))
        self.assertEqual(row['stored_request_json_sha256'],digest(saved))
        self.assertNotEqual(row['request_sha256'],row['stored_request_json_sha256'])
        self.assertEqual(json.loads(saved.read_text()),body)
        self.assertEqual(saved.read_bytes(),(packed(body)+'\n').encode())


if __name__=='__main__':unittest.main(verbosity=2)
