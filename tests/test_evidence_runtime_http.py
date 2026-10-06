"""真实HTTP/SQLite/Agent协议联调；脚本主模型与审核器不证明金融语义。"""
from pathlib import Path
import json
import time
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_runtime.conversation import ConversationAgent
from local_knowledge.runtime import EvidenceRuntime
from local_knowledge.runtime import RoundProvider
from server import create_server,ProxyHandler
from simulated_bank import BankError
from test_bank_integration import Browser
from test_native_conversation import ScriptedPrimary,Reviewer,call,last_tool
from test_evidence_runtime import catalog_body
from agent_runtime.events import RuntimeEvents


def answer(messages,schemas):
    out=last_tool(messages)
    return call('answer_reviewed',{'text':'目录披露风险等级为R2；即时可买和完整费率仍需核实。',
        'claims':[{'text':'风险信息来自本轮官方目录披露。','evidence_ids':[out['evidence_id']]}],
        'draft_ids':[],'followups':[]})


class EvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.quiet=patch.object(ProxyHandler,'log_message',lambda *a:None);self.quiet.start();self.addCleanup(self.quiet.stop)
        self.server=create_server(0,Path(self.temp.name)/'bank.sqlite3')
        # 测试生命周期等待真实handler退出，再删除SQLite临时目录；不改生产daemon策略。
        self.server.daemon_threads=False
        self.server.evidence_runtime.close()
        self.fetches=[]
        def reader(*args,**kwargs):self.fetches.append((args[1:],kwargs));return catalog_body()
        self.runtime=EvidenceRuntime(self.server.finance.knowledge_provider,reader=reader,background=False)
        self.server.evidence_runtime=self.runtime
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.browser=Browser(self.base);self.session,_=self.browser.start()

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5)

    def agent(self,tool='knowledge_search',reviewer=None):
        args={'query':'风险','product_id':'WEBANK:000001','facets':['risk']} if tool=='knowledge_search' else {'query':'微众测试甲产品'}
        self.primary=ScriptedPrimary(call(tool,args),answer)
        self.server.conversation_agent=ConversationAgent(self.primary,reviewer or Reviewer())

    def ask(self):
        return self.browser.call('/api/agent','POST',{'text':'微众测试甲产品风险是什么？'})

    def test_cold_empty_library_fixed_temporary_rag_reaches_final_http_guard(self):
        self.agent();status,result,_=self.ask()
        self.assertEqual(status,200,result);self.assertFalse(result.get('isFallback'),result.get('agent_runtime'))
        self.assertTrue(result['knowledge_snapshot']['release_id'].startswith('tmp-'))
        self.assertTrue(result['knowledge_snapshot']['canonical_digests'])
        self.assertEqual(result['knowledge_preparation']['status'],'prepared')
        self.assertIsNone(self.server.finance.knowledge_provider.status()['public_release_id'])
        self.assertEqual(len(self.fetches),1)

    def test_product_catalog_tool_cannot_bypass_final_evidence_snapshot(self):
        self.agent('public_products');status,result,_=self.ask()
        self.assertEqual(status,200,result);self.assertFalse(result.get('isFallback'),result.get('agent_runtime'))
        self.assertTrue(result['knowledge_snapshot']['canonical_digests'])
        self.assertIn('public_products',result['capabilities_used'])

    def test_revocation_blocks_old_text_and_speech_receipt(self):
        self.agent();status,result,_=self.ask();self.assertEqual(status,200,result)
        g=self.server.finance;receipt=result.get('speech_receipt')
        self.assertTrue(receipt,result)
        g.knowledge_provider.set_source_state('webank-direct-disclosure',outbound_allowed=False)
        # 语音回执还绑定账户会话；从cookie取得原token，不伪造新授权。
        token=next(c.value for c in self.browser.cookies if c.name=='qz_bank_user')
        with self.assertRaises(BankError):g.resolve_speech(self.session['workspace_id'],receipt,token)

    def test_background_publication_keeps_round_version(self):
        class PromoteReviewer(Reviewer):
            def review(inner,packet,*args):
                self.runtime.drain_promotions();return super(PromoteReviewer,inner).review(packet,*args)
        self.agent(reviewer=PromoteReviewer());status,result,_=self.ask()
        self.assertEqual(status,200,result);self.assertFalse(result.get('isFallback'),result.get('agent_runtime'))
        self.assertTrue(result['knowledge_snapshot']['release_id'].startswith('tmp-'))
        self.assertTrue(self.server.finance.knowledge_provider.status()['public_release_id'].startswith('pub-'))

    def test_cross_user_receipt_rejected_and_no_user_data_shared(self):
        self.agent();status,result,_=self.ask();self.assertEqual(status,200,result)
        other=Browser(self.base);state,_=other.start()
        token=next(c.value for c in other.cookies if c.name=='qz_bank_user')
        with self.assertRaises(BankError):
            self.server.finance.resolve_speech(state['workspace_id'],result['speech_receipt'],token)
        raw=self.server.finance.knowledge_provider.path.read_bytes()
        self.assertNotIn(self.session['workspace_id'].encode(),raw)
        self.assertNotIn(state['workspace_id'].encode(),raw)

    def test_auth_failure_cannot_trigger_public_fetch(self):
        self.agent();status,_,_=self.browser.call('/api/agent','POST',{'text':'微众测试甲产品风险'},headers={'X-QZ-CSRF':''})
        self.assertEqual(status,403);self.assertEqual(self.fetches,[])

    def test_cashflow_then_prepare_uses_real_request_observer_and_final_guard(self):
        from test_native_conversation import reviewed
        def prepare(messages,schemas):
            data=[json.loads(m['content']).get('data') for m in reversed(messages) if m['role']=='tool']
            cashflow=next(v for v in data if isinstance(v,dict) and v.get('accounts'))
            account=next(a['account_id'] for a in cashflow['accounts'] if a['account_id'].endswith('-main'))
            return call('prepare_operation',{'action':'purchase','account_id':account,
                        'product_id':'SIM-CASH-01','amount_minor':10000})
        self.primary=ScriptedPrimary(call('cashflow',{'days':60}),call('products',{}),prepare,reviewed)
        reviewer=Reviewer()
        self.server.conversation_agent=ConversationAgent(self.primary,reviewer)
        g=self.server.finance;workspace=self.session['workspace_id']
        balances={a['id']:a['balance_minor'] for a in g.overview(workspace)['accounts']}
        status,result,_=self.browser.call('/api/agent','POST',{'text':'查看未来现金流后准备申购100元现金类模拟产品，等待我本人确认。'})
        self.assertEqual(status,200,result)
        self.assertFalse(result.get('isFallback'),result.get('agent_runtime'))
        self.assertEqual(len(reviewer.packets),1)
        self.assertEqual(len(result['bank_actions']),1,result)
        operation_id=result['bank_actions'][0]['operation_id']
        batches=result['financial_metric_batches']
        self.assertTrue(batches)
        for batch in batches:g.financial_metrics.validate(workspace,batch)
        from contextlib import closing
        with closing(g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT state FROM operations WHERE id=?',(operation_id,)).fetchone()[0],
                             'pending_confirmation')
            self.assertEqual(db.execute('SELECT count(*) FROM base_operation_evidence WHERE operation_id=?',
                                        (operation_id,)).fetchone()[0],1)
        self.assertEqual({a['id']:a['balance_minor'] for a in g.overview(workspace)['accounts']},balances)
        self.assertEqual(self.fetches,[])

    def test_revocation_after_identity_catalog_blocks_first_cloud_call(self):
        original=RoundProvider.public_catalog
        def revoke(provider,**kwargs):
            data=original(provider,**kwargs)
            self.server.finance.knowledge_provider.set_source_state('webank-direct-disclosure',outbound_allowed=False)
            return data
        self.agent()
        with patch.object(RoundProvider,'public_catalog',revoke):status,result,_=self.ask()
        self.assertTrue(result['isFallback'],result)
        self.assertEqual(self.primary.messages,[])
        self.assertNotIn('speech_receipt',result)

    def test_revocation_during_review_blocks_final_json_and_speech(self):
        def revoke(packet):
            self.server.finance.knowledge_provider.set_source_state('webank-direct-disclosure',outbound_allowed=False)
        self.agent(reviewer=Reviewer(hook=revoke))
        status,result,_=self.ask()
        self.assertEqual(status,409,result);self.assertTrue(result['isFallback'])
        self.assertNotIn('speech_receipt',result)
        self.assertNotIn('目录披露风险等级为R2',result['reply'])
        self.assertEqual(self.server.finance.personal_data.read_history(self.session['workspace_id']),[])

    def test_revocation_during_review_blocks_streamed_final_answer(self):
        def revoke(packet):
            self.server.finance.knowledge_provider.set_source_state('webank-direct-disclosure',outbound_allowed=False)
        self.agent(reviewer=Reviewer(hook=revoke))
        status,stream,_=self.browser.call('/api/agent','POST',{'text':'微众测试甲产品风险是什么？'},
                                        headers={'Accept':'text/event-stream'})
        self.assertEqual(status,200)
        self.assertNotIn('目录披露风险等级为R2',stream)
        events=[json.loads(line[6:]) for line in stream.splitlines() if line.startswith('data: ')]
        final=[e for e in events if e.get('name')=='navren.result']
        self.assertEqual(len(final),1,events)
        self.assertTrue(final[0]['value']['isFallback'])
        self.assertNotIn('speech_receipt',final[0]['value'])
        self.assertEqual(self.server.finance.personal_data.read_history(self.session['workspace_id']),[])

    def commit_failure(self,stream=False):
        import sqlite3
        self.agent();g=self.server.finance;w=self.session['workspace_id'];targets=[]
        original_connect=g.bank.connect;original_bind=g.operation_evidence.bind_answer
        class Connection:
            def __init__(self,db):self.db=db
            def __getattr__(self,name):return getattr(self.db,name)
            def commit(self):
                if self in targets:raise sqlite3.OperationalError('isolated_commit_failure')
                return self.db.commit()
        def bind(workspace,result,db):
            original_bind(workspace,result,db)
            g.bank._audit(db,workspace,'isolated_publication_commit_marker',{})
            targets.append(db)
        with patch.object(g.bank,'connect',side_effect=lambda:Connection(original_connect())),\
                patch.object(g.operation_evidence,'bind_answer',side_effect=bind):
            status,body,_=self.browser.call('/api/agent','POST',{'text':'微众测试甲产品风险是什么？'},
                headers={'Accept':'text/event-stream'} if stream else {})
        self.assertTrue(targets)
        self.assertEqual(g.personal_data.read_history(w),[])
        from contextlib import closing
        with closing(g.bank.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE workspace_id=? AND event='isolated_publication_commit_marker'",(w,)).fetchone()[0],0)
        if stream:
            self.assertEqual(status,200)
            self.assertNotIn('目录披露风险等级为R2',body)
            final=[json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ') and 'navren.result' in line]
            self.assertEqual(len(final),1,body);self.assertTrue(final[0]['value']['isFallback'])
            self.assertNotIn('speech_receipt',final[0]['value'])
        else:
            self.assertEqual(status,500,body);self.assertTrue(body['isFallback'])
            self.assertNotIn('目录披露风险等级为R2',body['reply']);self.assertNotIn('speech_receipt',body)

    def test_bank_commit_failure_cannot_publish_successful_json(self):self.commit_failure()

    def test_bank_commit_failure_cannot_publish_successful_sse(self):self.commit_failure(stream=True)

    def test_final_encoded_service_rejection_revokes_own_draft_and_receipt(self):
        from test_native_conversation import reviewed
        g=self.server.finance;w=self.session['workspace_id']
        account=next(a['id'] for a in g.overview(w)['accounts'] if a['id'].endswith('-main'))
        old=g.prepare(w,{'action':'purchase','account_id':account,'product_id':'SIM-CASH-01',
                         'amount_minor':10000,'idempotency_key':'manual-other-round'})['operation']['id']
        self.service_agent()  # 独立规则文件，保留真实服务预览和实际时钟。
        def prepare(messages,schemas):
            views=[json.loads(m['content']).get('data') for m in reversed(messages) if m['role']=='tool']
            cashflow=next(v for v in views if isinstance(v,dict) and v.get('accounts'))
            alias=next(a['account_id'] for a in cashflow['accounts'] if a['account_id'].endswith('-main'))
            return call('prepare_operation',{'action':'purchase','account_id':alias,
                        'product_id':'SIM-CASH-01','amount_minor':10000})
        self.primary=ScriptedPrimary(call('cashflow',{'days':60}),call('products',{}),
                                    call('service_offer_preview',{}),prepare,reviewed)
        self.server.conversation_agent=ConversationAgent(self.primary,Reviewer())
        ticks=[g.bank.clock()];original=json.dumps
        def encode(value,*args,**kwargs):
            body=original(value,*args,**kwargs)
            if isinstance(value,dict) and value.get('bank_actions') and value.get('service_snapshot'):
                ticks[0]+=3600
            return body
        with patch.object(g.bank,'clock',side_effect=lambda:ticks[0]),patch('server.json.dumps',encode):
            status,result,_=self.browser.call('/api/agent','POST',{'text':'我愿意迁入工资账户，希望服务费减免。查看现金流后准备申购100元现金类模拟产品，等待本人确认。'})
        self.assertEqual(status,409,result);self.assertTrue(result['isFallback'])
        self.assertEqual(g.personal_data.read_history(w),[])
        self.assertEqual(g._speech_receipts,{})
        from contextlib import closing
        with closing(g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT state FROM operations WHERE id=?',(old,)).fetchone()[0],'pending_confirmation')
            own=db.execute("SELECT o.state,a.status FROM operations o JOIN agent_draft_reviews a ON a.operation_id=o.id WHERE o.workspace_id=?",(w,)).fetchall()
            self.assertEqual([(r['state'],r['status']) for r in own],[('cancelled','superseded')])

    def test_speech_receipt_snapshots_have_no_mutable_alias_to_result_or_reader(self):
        self.agent();g=self.server.finance;captured=[];original=g.approve_speech
        def approve(workspace,result,token):
            receipt=original(workspace,result,token);captured.append((result,receipt));return receipt
        with patch.object(g,'approve_speech',side_effect=approve):status,result,_=self.ask()
        self.assertEqual(status,200,result)
        server_result,receipt=captured[0];token=next(c.value for c in self.browser.cookies if c.name=='qz_bank_user')
        before=g.resolve_speech(self.session['workspace_id'],receipt,token)
        server_result['knowledge_snapshot']['canonical_digests'][0]['sha256']='changed-local-result-alias'
        reader=g.assert_speech_receipt(self.session['workspace_id'],receipt,token)
        reader['knowledge']['canonical_digests'][0]['sha256']='changed-read-copy'
        self.assertEqual(g.resolve_speech(self.session['workspace_id'],receipt,token),before)

    def enable_history(self):
        g=self.server.finance;w=self.session['workspace_id']
        g.memories.set_enabled(w,{'enabled':True,'expected_revision':g.personal_data.revision(w)})

    def test_real_guarded_history_saves_only_final_published_answer(self):
        self.enable_history();self.agent();status,result,_=self.ask()
        self.assertEqual(status,200,result);self.assertFalse(result.get('isFallback'),result)
        history=self.server.finance.personal_data.read_history(self.session['workspace_id'])
        self.assertEqual(history[-1],{'role':'assistant','content':result['reply']})
        self.assertNotIn('knowledge_snapshot',json.dumps(history))

    def test_private_commit_then_permissions_failure_compensates_actual_http_turn(self):
        from contextlib import closing
        self.enable_history();g=self.server.finance;w=self.session['workspace_id']
        previous=[{'role':'user','content':'已成功的上一轮问题'},{'role':'assistant','content':'已成功的上一轮回答'}]
        previous_widgets=[{'type':'cashflow','data':{'cash_minor':12300}}]
        self.assertTrue(g.personal_data.save_turn(w,previous,'prior-success',previous_widgets,
            expected_revision=g.personal_data.revision(w)))
        original_widgets=g.personal_data.read_widgets(w)
        self.agent();store=g.personal_data;save=store.save_turn_guarded;committed=[]
        def fail_after_commit(*args,**kwargs):
            with patch.object(store,'_permissions',side_effect=OSError('fixture_postcommit_permissions_failure')):
                try:return save(*args,**kwargs)
                except OSError:
                    # 实际私库commit已生效，异常并非模拟一个未写入的writer。
                    committed.append(store.read_history(w))
                    raise
        with patch.object(store,'save_turn_guarded',side_effect=fail_after_commit):status,result,_=self.ask()
        self.assertEqual(status,500,result);self.assertTrue(result['isFallback'])
        self.assertEqual(len(committed),1)
        self.assertEqual(committed[0][-1]['content'],'目录披露风险等级为R2；即时可买和完整费率仍需核实。')
        self.assertNotIn('目录披露风险等级为R2',result['reply']);self.assertNotIn('speech_receipt',result)
        self.assertEqual(store.read_history(w),previous)
        self.assertEqual(store.read_widgets(w),original_widgets)
        self.assertEqual(g._speech_receipts,{})
        with closing(store.connect()) as db:
            rows=db.execute("SELECT status FROM guarded_turn_saves WHERE workspace_id=?",(w,)).fetchall()
        self.assertEqual([row['status'] for row in rows],['discarded'])
        with closing(g.bank.connect()) as db:
            row=db.execute("SELECT details FROM audit WHERE workspace_id=? AND event='agent_unpublished_answer_withdrawal'",(w,)).fetchone()
        self.assertEqual(json.loads(row['details'])['history_status'],'discarded')

    def test_original_agent_deadline_after_json_encoding_blocks_publication(self):
        self.enable_history();self.agent();g=self.server.finance
        original_time=time.monotonic;offset=[0.];original_encode=json.dumps
        def encode(value,*args,**kwargs):
            body=original_encode(value,*args,**kwargs)
            if isinstance(value,dict) and value.get('reply','').startswith('目录披露风险等级为R2') and value.get('agent_runtime'):
                offset[0]+=106
            return body
        with patch('agent_runtime.conversation.time.monotonic',side_effect=lambda:original_time()+offset[0]),\
                patch('server.json.dumps',encode):status,result,_=self.ask()
        self.assertTrue(result['isFallback'],result)
        self.assertEqual(result['error']['code'],'AGENT_BUDGET_EXCEEDED')
        self.assertNotIn('speech_receipt',result)
        self.assertEqual(g._speech_receipts,{})
        self.assertEqual(g.personal_data.read_history(self.session['workspace_id']),[])

    def test_memory_disabled_during_review_does_not_restore_old_history(self):
        self.enable_history();g=self.server.finance;w=self.session['workspace_id']
        def disable(packet):g.memories.set_enabled(w,{'enabled':False,'expected_revision':g.personal_data.revision(w)})
        self.agent(reviewer=Reviewer(hook=disable));status,result,_=self.ask()
        self.assertTrue(result['isFallback'],result)
        self.assertEqual(g.personal_data.read_history(w),[]);self.assertEqual(g._speech_receipts,{})

    def test_partial_history_cleanup_preserves_later_turn_and_records_local_outcome(self):
        from contextlib import closing
        from user_memory import privacy_revision
        self.enable_history();g=self.server.finance;w=self.session['workspace_id'];request_id='a'*32
        revision=g.personal_data.revision(w)
        with g.bank.transaction() as db:
            privacy=privacy_revision(db,w)
            self.assertTrue(g.personal_data.save_turn_guarded(w,[{'role':'assistant','content':'本轮旧答复'}],
                request_id,[],bank_db=db,expected_revision=revision,expected_privacy=privacy))
        later=[{'role':'assistant','content':'用户后续已保存答复'}]
        self.assertTrue(g.personal_data.save_turn(w,later,'later-round',[],expected_revision=revision))
        outcome=g.discard_unpublished_answer(w,request_id,{},expected_privacy=privacy,
            expected_personal=revision,history_saved=True,failure_stage='publication')
        self.assertEqual(outcome['history_status'],'partial_or_stale')
        self.assertEqual(outcome['drafts_withdrawn_count'],0)
        self.assertEqual(g.personal_data.read_history(w),later)
        with closing(g.bank.connect()) as db:
            row=db.execute("SELECT details FROM audit WHERE workspace_id=? AND event='agent_unpublished_answer_withdrawal'",(w,)).fetchone()
        audit=json.loads(row['details']);self.assertEqual(audit['history_status'],'partial_or_stale')
        self.assertEqual(audit['request_id'],request_id)
        self.assertNotIn('本轮旧答复',row['details']);self.assertNotIn('用户后续已保存答复',row['details'])

    def partial_sse_failure(self,*,cleanup_error=False,deadline=False):
        from server import PublicationSocket
        self.enable_history();g=self.server.finance;w=self.session['workspace_id']
        previous=[{'role':'user','content':'历史问题'},{'role':'assistant','content':'历史回答'}]
        self.assertTrue(g.personal_data.save_turn(w,previous,'prior-success',[],expected_revision=g.personal_data.revision(w)))
        self.service_agent();ticks=[g.bank.clock()];segments=[];send_timeouts=[];final_attempts=[]
        offset=[0.];original_time=time.monotonic;remaining_clock=[None]
        original_guard=g.publication_guard
        def guard(*args,**kwargs):
            remaining_clock[0]=kwargs.get('budget_remaining')
            return original_guard(*args,**kwargs)
        cleaned=threading.Event();original_discard=g.discard_unpublished_answer
        def discard(*args,**kwargs):
            try:
                outcome=original_discard(*args,**kwargs)
                if cleanup_error:raise BankError('HISTORY_CLEANUP_UNAVAILABLE','测试注入清理异常。',503)
                return outcome
            finally:cleaned.set()
        original_finish=RuntimeEvents.finish;original_send=PublicationSocket.sendall
        def finish(events,result):
            final_attempts.append(bool(result.get('isFallback')))
            if not result.get('isFallback'):
                result['publication_test_padding']='X'*200000
                if deadline:
                    self.assertTrue(callable(remaining_clock[0]),'original_agent_clock_not_wired')
                    offset[0]+=remaining_clock[0]()-0.5
            return original_finish(events,result)
        class ExpiringSocket:
            def __init__(self,sock):self.sock=sock
            def __getattr__(self,name):return getattr(self.sock,name)
            def sendall(self,body,*args,**kwargs):
                send_timeouts.append(self.sock.gettimeout())
                self.sock.sendall(body,*args,**kwargs)
                segments.append(len(body))
                if deadline:offset[0]+=1
                else:ticks[0]+=3600
        def send(proxy,body,*args,**kwargs):
            if bytes(body).startswith(b'data: ') and b'navren.result' in bytes(body)[:1024]:
                proxy.sock=ExpiringSocket(proxy.sock)
            return original_send(proxy,body,*args,**kwargs)
        with patch.object(g.bank,'clock',side_effect=lambda:ticks[0]),patch.object(RuntimeEvents,'finish',finish),\
                patch.object(PublicationSocket,'sendall',send),patch.object(g,'discard_unpublished_answer',side_effect=discard),\
                patch.object(g,'publication_guard',side_effect=guard),\
                patch('agent_runtime.conversation.time.monotonic',side_effect=lambda:original_time()+offset[0]):
            status,body,_=self.browser.call('/api/agent','POST',{'text':'我愿意迁入工资账户，希望协商服务费减免。'},
                                          headers={'Accept':'text/event-stream'})
            # socket关闭可先于私库补偿结束；Agent同workspace锁在补偿完成前不释放。
            self.assertTrue(cleaned.wait(5),'server_cleanup_not_completed')
        self.assertEqual(status,200)
        self.assertEqual(segments,[65536])
        self.assertEqual(final_attempts,[False])
        if deadline:
            self.assertTrue(all(0<t<=0.5 for t in send_timeouts),send_timeouts)
        self.assertNotIn('RUN_FINISHED',body)
        final_lines=[line[6:] for line in body.splitlines() if line.startswith('data: ') and 'navren.result' in line]
        self.assertEqual(len(final_lines),1)
        with self.assertRaises(json.JSONDecodeError):json.loads(final_lines[0])
        self.assertEqual(g.personal_data.read_history(w),previous)
        self.assertEqual(g._speech_receipts,{})

    def test_actual_sse_partial_expiry_stops_remaining_frames_and_cas_restores_history(self):
        self.partial_sse_failure()

    def test_cleanup_error_after_partial_sse_cannot_publish_second_result(self):
        with self.assertLogs('server',level='ERROR') as logs:self.partial_sse_failure(cleanup_error=True)
        self.assertIn('类型=BankError',logs.output[0])
        self.assertNotIn('测试注入清理异常',logs.output[0])

    def test_original_deadline_limits_actual_send_timeout_and_stops_remaining_sse(self):
        self.partial_sse_failure(deadline=True)

    def service_agent(self):
        rules=self.server.finance.service_negotiations.rules()
        path=Path(self.temp.name)/'rules.json';path.write_text(json.dumps(rules))
        self.server.finance.service_negotiations.rules_path=path
        def service_answer(messages,schemas):
            evidence=last_tool(messages)
            return call('answer_reviewed',{'text':'服务方案仅为预览，需要另行本人确认。',
                'claims':[{'text':'服务条款来自本轮预览。','evidence_ids':[evidence['evidence_id']]}],
                'draft_ids':[],'followups':[]})
        self.primary=ScriptedPrimary(call('service_offer_preview',{}),service_answer)
        self.server.conversation_agent=ConversationAgent(self.primary,Reviewer())

    def test_service_expiry_after_json_encoding_blocks_actual_socket_write(self):
        self.service_agent();g=self.server.finance;ticks=[g.bank.clock()]
        original=json.dumps
        def encode(value,*a,**k):
            result=original(value,*a,**k)
            if isinstance(value,dict) and value.get('reply')=='服务方案仅为预览，需要另行本人确认。' and value.get('service_snapshot'):
                ticks[0]+=3600
            return result
        with patch.object(g.bank,'clock',side_effect=lambda:ticks[0]),patch('server.json.dumps',encode):
            status,result,_=self.browser.call('/api/agent','POST',{'text':'我愿意迁入工资账户，希望协商服务费减免。'})
        self.assertEqual(status,409,result);self.assertTrue(result['isFallback'])
        self.assertNotIn('speech_receipt',result);self.assertNotIn('服务方案仅为预览',result['reply'])

    def test_service_expiry_before_custom_sse_frame_blocks_actual_write(self):
        self.service_agent();g=self.server.finance;ticks=[g.bank.clock()]
        original=RuntimeEvents.emit
        def emit(events,event):
            value=getattr(event,'value',None)
            if isinstance(value,dict) and value.get('reply')=='服务方案仅为预览，需要另行本人确认。':ticks[0]+=3600
            return original(events,event)
        with patch.object(g.bank,'clock',side_effect=lambda:ticks[0]),patch.object(RuntimeEvents,'emit',emit):
            status,stream,_=self.browser.call('/api/agent','POST',{'text':'我愿意迁入工资账户，希望协商服务费减免。'},
                                            headers={'Accept':'text/event-stream'})
        self.assertEqual(status,200);self.assertNotIn('服务方案仅为预览',stream)
        events=[json.loads(line[6:]) for line in stream.splitlines() if line.startswith('data: ')]
        final=[e for e in events if e.get('name')=='navren.result']
        self.assertEqual(len(final),1,events);self.assertTrue(final[0]['value']['isFallback'])

    def test_tts_receipt_expiry_after_frame_guard_blocks_actual_pcm(self):
        from voice_gateway import VoiceGateway,BrowserSocket
        from test_voice_gateway import factory
        from websocket import create_connection
        self.agent();status,result,_=self.ask();self.assertEqual(status,200,result)
        receipt=result['speech_receipt'];g=self.server.finance
        self.server.voice=VoiceGateway(key='isolated-protocol-only',task_factory=factory)
        status,session,_=self.browser.call('/api/speech/session','POST',{'kind':'tts','speech_receipt':receipt})
        self.assertEqual(status,200,session)
        original=BrowserSocket.send;attempts=[]
        def send(browser,value):
            if isinstance(value,bytes):
                attempts.append(len(value))
                with g._speech_lock:g._speech_receipts[receipt]['expires']=time.monotonic()-1
            return original(browser,value)
        cookie='; '.join(f'{c.name}={c.value}' for c in self.browser.cookies)
        with patch.object(BrowserSocket,'send',send):
            ws=create_connection(self.base.replace('http:','ws:')+session['path'],origin=self.base,cookie=cookie,timeout=5)
            try:
                ws.send(json.dumps({'type':'start','voice':'male_warm'}))
                self.assertEqual(json.loads(ws.recv())['type'],'ready')
                received=ws.recv();self.assertNotIsInstance(received,bytes)
                self.assertEqual(json.loads(received)['code'],'SPEECH_RECEIPT_INVALID')
                self.assertTrue(attempts)
            finally:ws.close()


if __name__=='__main__':unittest.main()
