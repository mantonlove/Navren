"""正式基座采用窄检查：临时HTTP/原生序列化，显式免费来源，无真实模型语义声明。"""
from contextlib import closing
from copy import deepcopy
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from base_runtime.evidence import EvidenceGuardedClient
from base_runtime.money_range import MAX_SAFE_MINOR
from base_runtime.service_assembly import AssembledGateway
from base_runtime.tool_scope import ScopedEvidenceClient,STATEMENT_TOOLS
from financial_metrics import fingerprint
from simulated_bank import BankError
from server import create_server,ProxyHandler


class NoModel:
    def process(self,*args,**kwargs):raise AssertionError('no_model_in_base_http_probe')


class FreshObserver:
    def assert_fresh(self):pass


class BaseFormalAdoptionTests(unittest.TestCase):
    observations=[]
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='navren-formal-base-')
        cls.addClassCleanup(cls.temp.cleanup)
        env=patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB':str(Path(cls.temp.name)/'knowledge.sqlite3'),
            'NAVREN_PUBLIC_SYNC_ENABLED':'false','NAVREN_MESSAGE_WORKER_ENABLED':'false'})
        env.start();cls.addClassCleanup(env.stop)
        quiet=patch.object(ProxyHandler,'log_message',lambda *a:None);quiet.start();cls.addClassCleanup(quiet.stop)
        # 采用正式默认factory，不能用候选create_assembled_server或自定义子类代替。
        cls.server=create_server(0,Path(cls.temp.name)/'bank.sqlite3',conversation_agent=NoModel(),public_sync_enabled=False)
        cls.g=cls.server.finance
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.addClassCleanup(cls.thread.join,3);cls.addClassCleanup(cls.server.server_close);cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        self.cookie=self.csrf=None
        status,session=self.request('POST','/api/finance/session',{})
        self.assertEqual(status,200,session);self.w=session['workspace_id'];self.csrf=session['csrf']

    def request(self,method,path,body=None,*,cookie=None,csrf=True):
        headers={'Content-Type':'application/json'}
        if self.cookie is not None:headers['Cookie']=self.cookie if cookie is None else cookie
        if csrf and self.csrf:headers['X-QZ-CSRF']=self.csrf
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15)
        try:
            c.request(method,path,None if body is None else json.dumps(body),headers)
            response=c.getresponse();raw=response.read();data=json.loads(raw)
            if response.getheader('Set-Cookie'):self.cookie=response.getheader('Set-Cookie').split(';',1)[0]
            return response.status,data
        finally:c.close()

    def test_default_formal_factory_and_original_finance_routes(self):
        self.assertIs(type(self.g),AssembledGateway)
        self.assertIsNotNone(self.g.approved_responses)
        status,overview=self.request('GET','/api/finance/overview')
        self.assertEqual(status,200,overview);self.assertEqual(overview['workspace_id'],self.w)
        status,metrics=self.request('GET','/api/finance/metrics')
        self.assertEqual(status,200,metrics);self.assertIn('money_range_policy_code',metrics['source_versions'])
        self.assertEqual(metrics['workspace_id'],self.w)
        self.observations.append({'case':'formal_default_factory_original_routes','factory':type(self.g).__module__+'.'+type(self.g).__name__,
                                  'snapshot_id':metrics['snapshot_id'],'status':status,'model_calls':0})

    def test_actual_http_safe_month_year_and_out_of_range_cancelling_maps(self):
        month=self.g.bank.fixture['as_of'][:7];year=int(month[:4])
        for method,path,args in [('analysis','/api/finance/analysis?month='+month,month),('annual_analysis','/api/finance/annual-analysis?year='+str(year),year)]:
            original=getattr(self.g.bank,method)(self.w,args)
            self.assertEqual(self.request('GET',path)[0],200)
            raw=deepcopy(original);raw['totals_minor']={**raw['totals_minor'],'income':MAX_SAFE_MINOR+2,'expense':MAX_SAFE_MINOR+2,'refund':0}
            raw['category_expense_minor']={};raw['income_expense_surplus_minor']=raw['cash_delta_minor']=0
            for field in ('journal_net_expense_minor','cash_expense_minor'):
                if field in raw:raw[field]=0
            with closing(self.g.bank.connect()) as db:before=fingerprint(list(db.iterdump()))
            with patch.object(self.g.bank,method,return_value=raw):status,denied=self.request('GET',path)
            self.assertEqual((status,denied['error']['code']),(503,'METRIC_NUMERIC_RANGE_EXCEEDED'))
            with closing(self.g.bank.connect()) as db:self.assertEqual(fingerprint(list(db.iterdump())),before)
            self.assertNotIn('totals_minor',denied)
            self.observations.append({'case':method+'_formal_HTTP_safe_and_unsafe_source','status':status,'error':denied['error']['code'],'source_fixture':'explicit local raw-map override','model_calls':0})

    def test_unissued_history_cannot_approve_and_query_identity_csrf(self):
        status,data=self.request('GET','/api/finance/presentation-history/unissued')
        self.assertEqual(status,404,data);self.assertNotIn('original_agent_final_proof',data)
        self.assertEqual(self.request('GET','/api/finance/presentation-history/unissued?workspace_id=other')[0],422)
        self.assertEqual(self.request('POST','/api/finance/presentations/query',{'parameters':{}},csrf=False)[0],403)
        self.observations.append({'case':'formal_history_unissued_no_approval','HTTP':status,'model_calls':0})

    def test_original_profile_update_and_new_versioned_protocol_both_preserved(self):
        with closing(self.g.bank.connect()) as db:original=self.g.bank._profile(db,self.w)
        legacy={k:original[k] for k in ('risk_level','horizon_days','reserve_minor')}
        legacy.update(reserve_minor=original['reserve_minor']+1,confirmed=True)
        status,changed=self.request('POST','/api/finance/profile',legacy)
        self.assertEqual(status,200,changed)
        self.assertEqual(self.g.bank.snapshot(self.w)['profile']['reserve_minor'],original['reserve_minor']+1)
        self.assertEqual(self.request('POST','/api/finance/profile',{**legacy,'confirmed':False})[0],400)
        status,current=self.request('GET','/api/finance/account-profile');self.assertEqual(status,200,current)
        conditions=current['conditions']
        payload={k:conditions[k] for k in ('risk_level','horizon_days','reserve_minor')}
        payload.update(reserve_minor=conditions['reserve_minor']+1,confirmed=True,expected_version=current['expected_version'])
        self.assertEqual(self.request('POST','/api/finance/profile',payload)[0],200)
        self.assertEqual(self.g.bank.snapshot(self.w)['profile']['reserve_minor'],payload['reserve_minor'])
        self.observations.append({'case':'old_confirmed_profile_and_new_CAS_both_preserved','model_calls':0})

    def test_native_estimate_bytes_and_payload_include_same_trusted_scope(self):
        from agent_runtime.native_client import DeepSeekToolClient
        def forbidden(*a,**kw):raise AssertionError('probe_never_calls_network_or_keychain')
        client=DeepSeekToolClient(urlopen=forbidden,key_loader=forbidden,log_path=Path(self.temp.name)/'native.jsonl')
        guarded=ScopedEvidenceClient(client,FreshObserver(),{'status':'prepared','coverage_gaps':[]},allowed_tools=STATEMENT_TOOLS)
        guarded.append_notice('\n受信本机账单说明。')
        messages=[{'role':'user','content':'只读9月账单'}]
        schemas=[{'type':'function','function':{'name':name,'parameters':{'type':'object','properties':{}}}} for name in ('monthly_statement','answer_facts','prepare_operation')]
        estimated=guarded.request_input_token_estimate(messages,schemas)
        actual=guarded.serialized_request(messages,schemas);packet=json.loads(actual)
        self.assertEqual(estimated,client.request_input_token_estimate(packet['messages'],packet['tools']))
        self.assertEqual(guarded.request_input_bound(messages,schemas),len(actual))
        self.assertEqual(guarded.request_payload(messages,schemas),packet)
        self.assertIn('受信本机账单说明',packet['messages'][-1]['content'])
        self.assertEqual({t['function']['name'] for t in packet['tools']},{'monthly_statement','answer_facts'})
        with self.assertRaises(BankError):guarded.append_notice('估算后的修改不得放行')
        guarded.notice='伪造范围'
        with self.assertRaises(BankError):guarded.request_input_token_estimate(messages,schemas)
        self.observations.append({'case':'native_same_scope_estimate_payload_and_bytes','wire_bytes':len(actual),'wire_sha256':hashlib.sha256(actual).hexdigest(),
            'normal_native_estimate':estimated,'GLM_limit_changed':False,'no_model_response_or_semantics':True,'paid_calls':0})

    def test_public_review_source_captured_and_private_disabled_empty_snapshot(self):
        class Client:
            def request_payload(self,packet):return packet
        preparation={'status':'prepared','coverage_gaps':[]}
        guarded=EvidenceGuardedClient(Client(),FreshObserver(),preparation)
        packet={'evidence':[],'content':'只用公开资料'}
        prepared=guarded.prepare_review_packet(packet,publication_sources={'knowledge_preparation':preparation,'financial_metric_snapshot':None})
        self.assertIn('public_evidence_limits',prepared)
        self.assertEqual(guarded.request_payload(prepared),prepared)
        with self.assertRaises(BankError):guarded.prepare_review_packet(packet,publication_sources={'knowledge_preparation':{'status':'gap'},'financial_metric_snapshot':None})
        self.observations.append({'case':'public_original_source_and_explicit_private_disabled','trusted_empty_financial_snapshot':True,'model_calls':0})


if __name__=='__main__':unittest.main()
