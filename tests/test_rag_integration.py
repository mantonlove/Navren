"""真实SQLite与本机HTTP接线验收；脚本模型仅验证协议，非真实模型语义评测。"""
import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent_runtime.conversation import ConversationAgent
from finance_gateway import FinanceGateway
from server import create_server, ProxyHandler
from simulated_bank import BankError
from voice_gateway import VoiceGateway
from test_bank_integration import Browser
from test_native_conversation import ScriptedPrimary, Reviewer, call, fact_reply, last_tool


def answer(messages, schemas):
    out=last_tool(messages)
    return call('answer_reviewed',{'text':'资料提示可能损失本金，当前在售状态仍需核实。',
        'claims':[{'text':'风险来源为公开条款。','evidence_ids':[out['evidence_id']]}],
        'draft_ids':[],'followups':[]})


def publish_fixture(g):
    p=g.knowledge_provider
    p.register_source('fixture',publisher='本地测试来源',endpoints=['https://official.example/'],
        freshness={'risk':None,'terms':None},permission_basis='isolated_test_only')
    p.register_product('TEST-P',issuer='TEST-ISSUER',scheme='test',code='TEST-P',name='测试产品',verified=True)
    v=p.stage_document(source_id='fixture',source_url='https://official.example/risk',title='测试风险条款',
        text='测试产品净值可能波动，投资者可能损失本金。',published_on='2026-01-01',
        retrieved_on=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
        effective_from='2026-01-01',effective_to='2099-01-01',product_id='TEST-P',facets=['risk'])
    p.approve_version(v,verification_basis='isolated_fixture_verified')
    p.publish()
    return v


class RagIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=FinanceGateway(Path(self.tmp.name)/'bank.sqlite3')
        self.w,self.token=self.g.create_session()

    def test_independent_files_and_ciphertext_history(self):
        primary=ScriptedPrimary(call('account_summary',{}),fact_reply)
        result=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'我叫张三，邮箱test.private@example.com，手机号13812345678，查余额')
        self.assertFalse(result.get('isFallback'))
        payload=json.dumps(primary.messages,ensure_ascii=False)
        for private in ('张三','test.private@example.com','13812345678',self.w):self.assertNotIn(private,payload)
        self.assertEqual(len(self.g.personal_data.read_history(self.w)),2)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM agent_dialogues').fetchone()[0],0)
        self.assertNotEqual(self.g.personal_data.database,self.g.knowledge_provider.path)
        raw=self.g.personal_data.database.read_bytes()
        for private in ('张三','test.private@example.com','查余额'):self.assertNotIn(private.encode(),raw)

    def test_public_graph_provider_consumed_and_review_projected(self):
        publish_fixture(self.g)
        primary=ScriptedPrimary(call('knowledge_search',{'query':'风险','product_id':'TEST-P','facets':['risk']}),answer)
        reviewer=Reviewer()
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'我叫张三，帮我查测试产品的风险，电话13812345678')
        self.assertFalse(result.get('isFallback'),result.get('agent_runtime'))
        self.assertTrue(result['knowledge_snapshot']['citations'])
        self.assertEqual(result['agent_review']['status'],'approved')
        payload=json.dumps(reviewer.packets,ensure_ascii=False)
        for private in ('张三','13812345678',self.w):self.assertNotIn(private,payload)
        self.assertIn('可能损失本金',payload)
        self.assertTrue(reviewer.packets[0]['draft']['claims'][0]['evidence_ids'])

    def test_history_ids_are_registered_before_first_cloud_message(self):
        with closing(self.g.bank.connect()) as db:
            account=db.execute('SELECT id FROM accounts WHERE workspace_id=?',(self.w,)).fetchone()[0]
        self.g.personal_data.save_history(self.w,[{'role':'assistant','content':'上次查询的账户编号是 '+account}])
        primary=ScriptedPrimary(call('account_summary',{}),fact_reply)
        result=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'查这个账户 '+account)
        self.assertFalse(result.get('isFallback'))
        self.assertNotIn(account,json.dumps(primary.messages,ensure_ascii=False))
        self.assertNotIn(self.w,json.dumps(primary.messages,ensure_ascii=False))

    def test_memory_revocation_blocks_next_cloud_request(self):
        self.g.memories.set_enabled(self.w,{'enabled':True})
        self.g.memories.create(self.w,{'category':'goal','value':'长期留学目标'})
        primary=ScriptedPrimary(call('user_memory',{}),call('answer_reviewed',
            {'text':'未核验分析','claims':[],'draft_ids':[],'followups':[]}))
        def hook(event):
            if event['kind']=='tool_end' and event.get('status')=='completed':
                self.g.memories.set_enabled(self.w,{'enabled':False})
        result=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'我的长期目标',on_event=hook)
        self.assertTrue(result['isFallback'])
        self.assertEqual(len(primary.messages),1)
        self.assertTrue(any(t['tool']=='user_memory' and t['status']=='completed' for t in result['agent_runtime']['trace']))

    def test_withdraw_during_review_discards_reply(self):
        version=publish_fixture(self.g)
        primary=ScriptedPrimary(call('knowledge_search',{'query':'风险','product_id':'TEST-P','facets':['risk']}),answer)
        reviewer=Reviewer(hook=lambda packet:self.g.knowledge_provider.withdraw(version))
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查测试产品的风险')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['agent_runtime']['failure'],'STALE_KNOWLEDGE_EVIDENCE')
        self.assertFalse(result.get('bank_actions'))

    def approved(self,text='已核验答复，可能损失本金。',snapshot=None):
        from simulated_bank.agent_guard import context_hash
        with closing(self.g.bank.connect()) as db:context=context_hash(db,self.w)
        result={'reply':text,'_approved_tts_text':text,'answer_contract':{'kind':'verified_facts'},
            'compliance_pass':True,**self.g.session_data(self.w,self.token),'knowledge_snapshot':snapshot}
        result['_answer_context']=context
        return self.g.approve_speech(self.w,result)

    def test_speech_receipt_owner_and_versions(self):
        receipt=self.approved()
        self.assertIn('可能损失本金',self.g.resolve_speech(self.w,receipt))
        other,_=self.g.create_session()
        with self.assertRaises(BankError):self.g.resolve_speech(other,receipt)
        with self.assertRaises(BankError):self.g.resolve_speech(self.w,'forged')
        self.g.memories.create(self.w,{'category':'goal','value':'长期学习目标'})
        with self.assertRaises(BankError) as error:self.g.resolve_speech(self.w,receipt)
        self.assertEqual(error.exception.code,'PERSONAL_VERSION_CONFLICT')

    def test_speech_rechecks_bank_revision(self):
        receipt=self.approved()
        self.g.bank.advance_days(self.w,1)
        with self.assertRaises(BankError) as error:self.g.resolve_speech(self.w,receipt)
        self.assertEqual(error.exception.code,'STALE_EVIDENCE')

    def test_speech_rechecks_withdrawn_public_evidence(self):
        version=publish_fixture(self.g)
        p=self.g.knowledge_provider;release=p.status()['public_release_id']
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        data=p.search_public({'query':'风险','scope':'public','as_of':day,'facets':['risk'],'limit':3},release)
        receipt=self.approved(snapshot={'release_id':release,'citations':[{'citation_id':data['citation_ids'][0],'as_of':day}]})
        p.withdraw(version)
        with self.assertRaises(BankError):self.g.resolve_speech(self.w,receipt)

    def test_unreviewed_output_has_no_speech_receipt(self):
        self.assertIsNone(self.g.approve_speech(self.w,{'reply':'未核验文字','compliance_pass':False}))


class RagHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        primary=ScriptedPrimary(call('answer_reviewed',{'text':'这个目标可以在你确认后保存。','claims':[],
            'draft_ids':[],'followups':[]}))
        self.server=create_server(0,Path(self.tmp.name)/'bank.sqlite3',voice=VoiceGateway(key='fixture-no-cloud'),
            conversation_agent=ConversationAgent(primary,Reviewer()))
        self.quiet=patch.object(ProxyHandler,'log_message',lambda *a:None);self.quiet.start();self.addCleanup(self.quiet.stop)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.b=Browser(self.base);self.state,_=self.b.start()

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5)

    def post(self,path,body):return self.b.call(path,'POST',body,headers={'Origin':self.base})

    def test_http_rejects_versionless_personal_mutation(self):
        status,body,_=self.b.call('/api/finance/memories/settings','POST',{'enabled':True},
            headers={'Origin':self.base},bind_personal_version=False)
        self.assertEqual(status,409);self.assertEqual(body['error']['code'],'PERSONAL_VERSION_REQUIRED')

    def test_chat_candidate_binding_and_speech_receipt(self):
        revision=self.state['personal_revision']
        status,state,_=self.post('/api/finance/memories/settings',{'enabled':True,'expected_revision':revision})
        self.assertEqual(status,200)
        status,result,_=self.post('/api/agent',{'text':'我的长期目标是毕业前存下学费'})
        self.assertEqual(status,200,result)
        self.assertTrue(result.get('speech_receipt'))
        proposal=result['memory_proposals'][0]
        self.assertTrue(proposal['confirmation_token'])
        self.assertEqual(self.b.call('/api/finance/memories')[1]['items'],[])
        payload={k:proposal[k] for k in ('proposal_id','source_ref','confirmation_token','expected_revision')}
        status,_,_=self.post('/api/finance/memories',{**payload,'value':'客户端改写'})
        self.assertEqual(status,400)
        status,saved,_=self.post('/api/finance/memories',payload)
        self.assertEqual(status,200);self.assertEqual(saved['items'][0]['value'],proposal['value'])
        status,_,_=self.post('/api/speech/session',{'kind':'tts','speech_receipt':result['speech_receipt']})
        self.assertEqual(status,409)

    def test_tts_cannot_submit_arbitrary_text(self):
        status,body,_=self.post('/api/speech/session',{'kind':'tts','text':'客户未经核验的正文'})
        self.assertEqual(status,400);self.assertEqual(body['error']['code'],'INVALID_SPEECH_REQUEST')

    def test_default_rejects_raw_cloud_audio(self):
        status,body,_=self.post('/api/speech/session',{'kind':'asr'})
        self.assertEqual(status,403);self.assertEqual(body['error']['code'],'LOCAL_ASR_REQUIRED')

    def test_fixed_voice_preview_without_arbitrary_text(self):
        status,result,_=self.post('/api/speech/session',{'kind':'tts','purpose':'voice_preview'})
        self.assertEqual(status,200,result)
        self.assertIn('/api/speech/ws',result['path'])


if __name__=='__main__':unittest.main()
