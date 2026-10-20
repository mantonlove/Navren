"""原ProxyHandler/发布守卫上的隔离装配；可信回调不得来自HTTP或模型。"""
from pathlib import Path
from http.cookies import SimpleCookie,CookieError
from urllib.parse import parse_qs,unquote,urlsplit
from base_runtime.design_quality_gateway import DesignQualityGateway
from base_runtime.approved_response_store import ApprovedResponseStore
from finance_gateway import FinanceGateway,request_json,COOKIE
from simulated_bank import BankError
from base_runtime.presentation import read_gateway
from contextlib import closing
from contextvars import ContextVar
import re
from user_memory import privacy_revision

class AssembledGateway(DesignQualityGateway):
    def __init__(self,database=None,*,agent_final_validator=None,**gateway_options):
        super().__init__(database,**gateway_options)
        self.approved_responses=ApprovedResponseStore(self,agent_final_validator)
        self._restore_send_check=ContextVar('navren_history_send_check',default=None)
        self.dialogue_archive=None
        provider=getattr(self,'experience_provider',None)
        if getattr(self,'registered_mode',False):
            if provider is None:
                raise BankError('REGISTERED_DIALOGUE_BINDING_REQUIRED','注册模式缺少当前实例身份来源。',503)
            def registered_binding(workspace,bank_db):
                provider.guard_current_request(bank_db)
                row=provider.instances.owner_for_workspace(workspace,db=bank_db)
                if row is None:
                    raise BankError('REGISTERED_DIALOGUE_BINDING_REQUIRED','当前实例尚未就绪。',403)
                return {k:row[k] for k in ('registered_user_id','workspace_id','persona_id')}
            self.configure_dialogue_archive(registered_binding)

    def configure_dialogue_archive(self,binding_reader):
        if self.dialogue_archive is not None:
            raise BankError('DIALOGUE_ARCHIVE_ALREADY_CONFIGURED','聊天归档已绑定，不能更换身份来源。',409)
        from base_runtime.dialogue_archive import DialogueArchiveReader
        self.dialogue_archive=DialogueArchiveReader(self,binding_reader=binding_reader)
        return self.dialogue_archive

    def discard_unpublished_answer(self,workspace,request_id,result,*,expected_privacy,expected_personal,
                                   history_saved=False,failure_stage='finalization',failure_code='PUBLICATION_FAILED'):
        outcome=super().discard_unpublished_answer(workspace,request_id,result,expected_privacy=expected_privacy,
            expected_personal=expected_personal,history_saved=history_saved,failure_stage=failure_stage,failure_code=failure_code)
        if (history_saved and failure_stage=='publication' and not result.get('isFallback')
                and (result.get('agent_review',{}).get('status')=='approved'
                    or (self.dialogue_archive is not None
                        and result.get('answer_contract',{}).get('kind')=='social_reply'
                        and result.get('answer_contract',{}).get('source')=='local_rule'))):
            restored=self.approved_responses.discard_unpublished(workspace,request_id,result,
                expected_revision=expected_personal,expected_privacy=expected_privacy)
            if restored is not None:
                outcome['history_status']='discarded' if restored else 'partial_or_stale'
                outcome['prepared_response_revoked']=True
        return outcome

    def budget(self,workspace,account_id=None,floor=0,exclude_hold_id=None):
        # 全户原预算没有单账户币种；只为明确选择的实际账户补充元数据。
        if account_id is None:
            return super().budget(workspace,account_id,floor,exclude_hold_id)
        if not isinstance(account_id,str) or not account_id:
            raise BankError('INVALID_ACCOUNT','预算需要明确账户。',422)
        with self.bank.transaction() as db,read_gateway(self,db) as scoped:
            state=scoped.overview(workspace)
            account=next((a for a in state['accounts'] if a['id']==account_id),None)
            if account is None:
                raise BankError('NOT_FOUND','当前客户没有该账户。',404)
            currencies=[a.get('currency') for a in state['accounts']]
            if any(not isinstance(c,str) or re.fullmatch(r'[A-Z]{3}',c) is None for c in currencies):
                raise BankError('BUDGET_CURRENCY_UNAVAILABLE','预算来源缺少明确币种。',409)
            # 原留款规则使用全户账户；没有汇率服务时不能给混币种总数贴单币种标签。
            if len(set(currencies))!=1:
                raise BankError('BUDGET_CURRENCY_MISMATCH','预算来源包含不同币种，无法按同一币种核对。',409)
            policy_currency=scoped.bank.capabilities().get('currency')
            if not isinstance(policy_currency,str) or re.fullmatch(r'[A-Z]{3}',policy_currency) is None:
                raise BankError('BUDGET_CURRENCY_UNAVAILABLE','预算资金政策缺少明确币种。',409)
            if set(currencies)!={policy_currency}:
                raise BankError('BUDGET_CURRENCY_MISMATCH','预算账户币种与原资金保留政策不一致。',409)
            scoped.overview=lambda _:state
            result=FinanceGateway.budget(scoped,workspace,account_id,floor,exclude_hold_id)
            return {**result,'currency':account['currency'],'account_id':account['id']}

    def assert_response_evidence(self,*args,**kwargs):
        super().assert_response_evidence(*args,**kwargs)
        check=self._restore_send_check.get()
        if check is not None:check(kwargs['db'])

    def identity(self,handler):
        w,token=super().identity(handler)
        # DQ扩展路由也沿原ProxyHandler的最终Cookie/隐私发送回查，不能只在读取时核验。
        handler._navren_workspace=w
        with closing(self.bank.connect()) as db:handler._navren_start_revision=privacy_revision(db,w)
        return w,token

    def dispatch(self,handler):
        if not urlsplit(handler.path).path.startswith('/api/finance'):
            return self._dispatch_assembled(handler)
        return self.security_events.audit_http(handler,self._dispatch_assembled)

    def _dispatch_assembled(self,handler):
        parsed=urlsplit(handler.path);path=parsed.path
        if self.registered_mode and path in {'/api/finance/account-profile','/api/finance/account-profile/display'}:
            original=handler._finance_json
            def send_profile(status,result,cookie=None):
                if status==200 and isinstance(result,dict) and isinstance(result.get('display'),dict):
                    w=result['workspace_binding'];revision=result['display']['record_revision']
                    handler._navren_finance_send_guard=lambda body:self.account_state.send_guard(w,revision)
                return original(status,result,cookie)
            handler._finance_json=send_profile
            try:return super().dispatch(handler)
            finally:handler._finance_json=original
        if path in {'/api/finance/session/logout','/api/finance/session/revocation'}:
            try:
                self.origin_check(handler)
                if parsed.query:raise BankError('INVALID_QUERY','退出不接受URL参数。',422)
                cookies=SimpleCookie()
                try:cookies.load(handler.headers.get('Cookie',''))
                except CookieError:raise BankError('UNAUTHENTICATED','演示会话无效。',401)
                token=cookies[COOKIE].value if COOKIE in cookies else ''
                if path.endswith('/revocation'):
                    if handler.command!='GET':raise BankError('METHOD_NOT_ALLOWED','退出凭据仅可GET。',405)
                    handler._finance_json(200,self.demo_sessions.revocation_context(token))
                    return True
                if handler.command!='POST':raise BankError('METHOD_NOT_ALLOWED','退出需要POST。',405)
                self.check_csrf(handler,token)
                if request_json(handler)!={}:raise BankError('INVALID_REQUEST','退出不接受身份参数。',422)
                result=self.demo_sessions.revoke_family(token)
                handler._finance_json(200,result,COOKIE+'=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            except BankError as e:handler._finance_json(e.status,{'error':e.as_dict()})
            return True
        if path=='/api/finance/dialogue/history':
            try:
                self.origin_check(handler);w,token=self.identity(handler)
                if handler.command!='GET':
                    raise BankError('METHOD_NOT_ALLOWED','聊天归档仅供只读查询。',405)
                query=parse_qs(parsed.query,keep_blank_values=True)
                if set(query)-{'cursor','limit'} or any(len(v)!=1 for v in query.values()):
                    raise BankError('INVALID_QUERY','聊天分页参数无效。',422)
                raw_limit=query.get('limit',['20'])[0]
                if re.fullmatch(r'[1-9][0-9]?',raw_limit) is None:
                    raise BankError('INVALID_QUERY','聊天分页条数无效。',422)
                archive=self.dialogue_archive
                if archive is None:
                    raise BankError('DIALOGUE_ARCHIVE_NOT_CONFIGURED','完整聊天尚未绑定当前注册实例。',503)
                with self.demo_sessions.request(token,w):
                    if getattr(handler,'finance_send_guard_contract',None)!='navren.finance-send-guard/1':
                        raise BankError('HISTORY_SEND_GUARD_REQUIRED','聊天归档缺少实际发送守卫。',503)
                    page,capture=archive.read_page(w,cursor=query.get('cursor',[None])[0],limit=int(raw_limit))
                    handler._navren_finance_send_guard=lambda body:archive.send_guard(w,capture,body)
                    handler._finance_json(200,page)
            except BankError as e:handler._finance_json(e.status,{'error':e.as_dict()})
            return True
        prefix=next((p for p in ('/api/finance/approved-responses/','/api/finance/presentation-history/') if path.startswith(p)),None)
        is_history=prefix is not None
        if not is_history:return super().dispatch(handler)
        try:
            self.origin_check(handler);w,token=self.identity(handler)
            if handler.command!='GET':raise BankError('METHOD_NOT_ALLOWED','审核原件仅供只读查询。',405)
            query=parse_qs(parsed.query,keep_blank_values=True)
            if set(query)-{'language'} or any(len(v)!=1 for v in query.values()):raise BankError('INVALID_QUERY','原件查询参数无效。',422)
            rid=unquote(path[len(prefix):])
            with self.demo_sessions.request(token,w):
                if getattr(handler,'finance_send_guard_contract',None)!='navren.finance-send-guard/1':
                    raise BankError('HISTORY_SEND_GUARD_REQUIRED','历史恢复缺少实际发送守卫。',503)
                capture={}
                result=self.approved_responses.read(w,rid,language=query.get('language',['zh'])[0],_send_capture=capture)
                handler._navren_finance_send_guard=lambda body:self.approved_responses.send_guard(w,capture,body)
                handler._finance_json(200,result)
        except BankError as e:handler._finance_json(e.status,{'error':e.as_dict()})
        return True

def create_assembled_server(database,*,port=0,conversation_agent=None,agent_final_validator=None,voice=None,evidence_runtime=None):
    path=Path(database).resolve()
    if 'Library/Application Support' in str(path):raise ValueError('live_database_forbidden')
    from server import create_server
    server=create_server(port=port,database=path,conversation_agent=conversation_agent,voice=voice,evidence_runtime=evidence_runtime,
        finance_factory=lambda p,**options:AssembledGateway(p,agent_final_validator=agent_final_validator,**options))
    if agent_final_validator is None:
        from agent_runtime.conversation import ConversationAgent
        agent=server.conversation_agent
        if isinstance(agent,ConversationAgent) and callable(getattr(agent,'validate_final_response',None)):
            server.finance.approved_responses.validator=agent.validate_final_response
    return server
