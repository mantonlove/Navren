"""Navren同源银行接入：HttpOnly会话、限定Agent工具、用户确认与资金约束。"""
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sys
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from simulated_bank import BankError, SimulatedBank
from simulated_bank.service import digest, integer, strict_fields
from simulated_bank.statement_import import MAX_BYTES, StatementImportService
from cashflow_timeline import build_cashflow_timeline, validate_horizon_days
from finance_capabilities import finance_read_capabilities, workflow_capability_catalog
from user_memory import UserMemoryStore
from personal_profile import PersonalProfile
from service_negotiation import ServiceNegotiationService
from public_products import PublicProductCatalog
from personal_graph import PersonalGraph
from financial_graph import FinancialGraph
from financial_calendar import FinancialCalendar
from wealth_portfolio import WealthPortfolio
from goal_service import GoalService
from view_context import PageContexts
from research_plans import ResearchPlans
from local_knowledge import LocalKnowledgeProvider

COOKIE = 'qz_bank_user'
TERMINAL = {'succeeded','pending_settlement','settled'}
_publication_checkpoint = ContextVar('navren_publication_checkpoint', default=None)
_publication_remaining = ContextVar('navren_publication_remaining', default=None)


def json_unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise ValueError('重复字段')
        result[key] = value
    return result


def request_json(handler):
    if handler.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
        raise BankError('JSON_REQUIRED','请求体必须为JSON。',415)
    raw = handler.headers.get('Content-Length','')
    if not re.fullmatch(r'\d+',raw) or not 1<=int(raw)<=1048576 or handler.headers.get('Transfer-Encoding'):
        raise BankError('INVALID_LENGTH','请求体大小无效。',413)
    try:
        data = json.loads(handler.rfile.read(int(raw)),object_pairs_hook=json_unique,
                          parse_constant=lambda _:(_ for _ in ()).throw(ValueError('无效数值')))
    except (ValueError,UnicodeError): raise BankError('INVALID_JSON','请求体无法解析。')
    if not isinstance(data,dict): raise BankError('INVALID_JSON','请求体必须是对象。')
    return data


def request_text(handler):
    if handler.headers.get('Content-Type','').split(';')[0].strip().lower() != 'text/csv':
        raise BankError('CSV_REQUIRED','请求体必须为text/csv格式。',415)
    raw = handler.headers.get('Content-Length','')
    if not re.fullmatch(r'\d+',raw) or not 1<=int(raw)<=MAX_BYTES or handler.headers.get('Transfer-Encoding'):
        raise BankError('CSV_SIZE','CSV不能为空，且不能超过1.5MB。',413)
    try:
        return handler.rfile.read(int(raw)).decode('utf-8')
    except UnicodeError:
        raise BankError('CSV_ENCODING','CSV必须使用UTF-8编码。')


class FinanceGateway:
    def __init__(self, database=None, *, financial_database=None, public_catalog=None,
                 sms_provider=None, allow_test_sms_provider=False, registered_mode=False):
        if type(registered_mode) is not bool:raise TypeError('registered_mode_must_be_bool')
        self.registered_mode=registered_mode
        self.experience_provider=None
        path = database or os.environ.get('NAVREN_SIM_DB') or os.environ.get('QINGZHOU_SIM_DB') or str(Path.home()/'Library/Application Support/Qingzhou/simulated-bank.sqlite3')
        self.bank = SimulatedBank(path, sms_provider=sms_provider,
                                  allow_test_sms_provider=allow_test_sms_provider)
        self.statements = StatementImportService(self.bank)
        self.memories = UserMemoryStore(self.bank)
        self.personal_data = self.memories.store
        from base_runtime.account_state import AccountState
        self.account_state = AccountState(self)
        self.personal_profile = PersonalProfile(self)
        knowledge_path = os.environ.get('NAVREN_KNOWLEDGE_DB') or str(Path(path).with_name(Path(path).stem+'.knowledge.sqlite3'))
        self.knowledge_provider = LocalKnowledgeProvider(knowledge_path)
        from local_knowledge.catalog_information_host import CatalogInformationHost
        self.catalog_information_host=CatalogInformationHost()
        self._speech_receipts = {}
        self._speech_lock = threading.Lock()
        self.personal_graph = PersonalGraph(self)
        self.public_products = public_catalog if public_catalog is not None else PublicProductCatalog(
            data_dir=os.environ.get('NAVREN_PUBLIC_DATA_DIR') or self.bank.database.parent/'public-data',
            refresh_interval_seconds=os.environ.get('NAVREN_PUBLIC_SYNC_INTERVAL_SECONDS','300'))
        self.product_performance = None
        self.public_money_products = None
        self.public_product_research = None
        self._product_performance_lock = threading.Lock()
        self.service_negotiations = ServiceNegotiationService(self)
        self.wealth = WealthPortfolio(self)
        self.calendar = FinancialCalendar(self)
        self.financial_graph = FinancialGraph(self)
        self.goals = GoalService(self.bank)
        self.page_contexts = PageContexts(self)
        self.research_plans = ResearchPlans(self)
        from research_workspace import ResearchWorkspace
        self.research_workspace = ResearchWorkspace(self)
        from demo_accounts import DemoAccountService
        from base_runtime.identity import DemoSessions
        from financial_metrics import FinancialMetrics
        self.demo_accounts = DemoAccountService(self)
        self.bank.demo_accounts = self.demo_accounts
        self.demo_sessions = DemoSessions(self)
        self._runtime_bindings = {}
        self._runtime_binding_lock = threading.Lock()
        self.financial_metrics = FinancialMetrics(self)
        from base_runtime.evidence import OperationEvidence
        self.operation_evidence = OperationEvidence(self)
        self.read_capabilities = finance_read_capabilities(self)
        self.financial_data = None
        financial_path = financial_database or os.environ.get('NAVREN_FINANCIAL_DB')
        if financial_path:
            from financial_data_service import FinancialDataService
            self.financial_data = FinancialDataService(financial_path)
        from simulated_bank.connected_assets import ConnectedAssets
        self.connected_assets = ConnectedAssets(self)
        from simulated_bank.account_connections import AccountConnections
        self.account_connections = AccountConnections(self)
        from simulated_bank.card_bindings import CardBindings
        self.card_bindings = CardBindings(self)
        from message_center import MessageCenter
        self.messages = MessageCenter(self)
        if registered_mode:
            from registered_instances import attach_registered_experiences
            attach_registered_experiences(self)
        self.research_workspace.install()

    def start_public_sync(self, enabled=None):
        if enabled is None:
            value=os.environ.get('NAVREN_PUBLIC_SYNC_ENABLED','true').strip().lower()
            if value not in ('true','false','1','0','yes','no','on','off'):
                raise ValueError('invalid_public_sync_enabled')
            enabled=value in ('true','1','yes','on')
        value=os.environ.get('NAVREN_MESSAGE_WORKER_ENABLED','true').strip().lower()
        if value not in ('true','false','1','0','yes','no','on','off'):
            raise ValueError('invalid_message_worker_enabled')
        living=False
        if self.registered_mode:
            raw=os.environ.get('NAVREN_LIVING_WORKER_ENABLED','true').strip().lower()
            if raw not in ('true','false','1','0','yes','no','on','off'):
                raise ValueError('invalid_living_worker_enabled')
            living=raw in ('true','1','yes','on')
        started=self.public_products.start() if enabled else False
        message_started=False
        try:
            if value in ('true','1','yes','on'):message_started=self.messages.start()
            if living:self.living_scheduler.start()
        except Exception:
            if message_started:self.messages.stop(timeout=2)
            if started:self.public_products.stop(timeout=2)
            raise
        return started

    def stop_public_sync(self, timeout=2):
        try:
            if self.registered_mode and not self.living_scheduler.stop(timeout=timeout):
                raise RuntimeError('living_worker_not_stopped')
        finally:
            try:
                if not self.messages.stop(timeout=timeout):raise RuntimeError('message_worker_not_stopped')
            finally:result=self.public_products.stop(timeout=timeout)
        return result

    def financial_query(self, workspace, operation, arguments):
        """同源浏览器与MCP读取同一事实服务；金融写操作仍用原确认流程。"""
        if self.financial_data is None:
            raise BankError('FINANCIAL_DATA_NOT_CONFIGURED', '独立财务库尚未显式接入此服务。', 503)
        subject = self.demo_accounts.subject_for(workspace)
        service=self.demo_accounts.active_history(workspace) or self.financial_data
        if operation == 'presentation':
            from financial_data_service.presentation import FinancialPresentationService
            result = FinancialPresentationService(service).query(subject, arguments)
        else:
            result = service.query(subject, operation, arguments)
        if result.get('error'):
            error = result['error']
            code = error['code']
            status = 404 if code in ('ENTITY_NOT_ACCESSIBLE', 'SUBJECT_NOT_FOUND') else 503 if code in ('DATASET_CHANGED', 'DATABASE_READ_FAILED') else 400
            raise BankError(code, error['message'], status)
        return {**result, 'workspace_binding': workspace, 'read_only': True,
                'runtime_ledger_included': False}

    def query_read_capability(self, workspace, capability_id, parameters=None):
        return self.read_capabilities.invoke(workspace,capability_id,parameters)

    def statement_details(self, workspace, **parameters):
        from base_runtime.statement_details import query
        return query(self, workspace, parameters)

    def financial_method_report(self,workspace,name,arguments):
        """只读A/B/C服务入口；权威输入、预算及指标表由基座适配器读取。"""
        if name not in ('accounting_report','returns_report','allocation_preview') or not isinstance(arguments,dict):
            raise BankError('METRIC_ARGUMENTS_INVALID','金融方法入口或参数结构不合法。',422)
        return self.financial_metrics.method_report(workspace,name,arguments)

    def capture_request_binding(self,workspace):
        from base_runtime.runtime_guards import capture_request_binding
        return capture_request_binding(self,workspace)

    def current_registered_binding(self,workspace_id,bank_db):
        provider=self.experience_provider
        request=self.demo_sessions._request.get()
        if provider is None or request is None or request[1]!=workspace_id:
            raise BankError('REGISTERED_CONTEXT_REQUIRED','请从当前注册体验会话读取本人记录。',403)
        record=provider._validate(bank_db,request[0],workspace=workspace_id)
        return {'registered_user_id':record['registered_user_id'],'workspace_id':workspace_id,
                'persona_id':record['persona_id']}

    def revalidate_request_binding(self,binding,*,bank_db):
        from base_runtime.runtime_guards import revalidate_request_binding
        return revalidate_request_binding(self,binding,bank_db=bank_db)

    def accounting_report(self,workspace,**arguments):
        return self.financial_method_report(workspace,'accounting_report',arguments)

    def returns_report(self,workspace,**arguments):
        return self.financial_method_report(workspace,'returns_report',arguments)

    def allocation_preview(self,workspace,**arguments):
        return self.financial_method_report(workspace,'allocation_preview',arguments)

    def bound_report_source(self,workspace,source,expected_version):
        # 仅本次 Agent 读取使用选定来源；共享服务和 active_source 不做任何写入。
        from copy import copy
        scoped=copy(self)
        scoped._request_report_source=(workspace,source,str(expected_version))
        scoped.read_capabilities=finance_read_capabilities(scoped)
        scoped.source(workspace)
        scoped.financial_metrics=self.financial_metrics.bound_gateway(scoped)
        return scoped

    def source(self, workspace):
        result=self.statements.source(workspace)
        with closing(self.bank.connect()) as db:
            row=self.bank._workspace(db,workspace)
        result['sources']={'simulation':{'source_version':str(row['version'])},
            'imported':{'source_version':str(result['dataset']['version']) if result['dataset'] else None}}
        if self.demo_accounts.is_demo(workspace):
            demo=self.demo_accounts.current(workspace)
            result.update(data_scope='navren_synthetic',source_dataset_version=demo['source_version'],
                          history_coverage={'from':demo['start_date'],'to':demo['end_date']})
            result['sources']['simulation']['dataset_version']=demo['source_version']
        binding=getattr(self,'_request_report_source',None)
        if binding:
            owner,chosen,expected=binding
            actual=result['sources'].get(chosen,{}).get('source_version')
            if owner!=workspace or actual is None or str(actual)!=expected:raise BankError('SOURCE_VERSION_CONFLICT','报告引用来源或版本已变化，请回到报告重新读取。',409)
            result={**result,'workspace_active_source':result['active_source'],'active_source':chosen,'read_scope':'this_report_reference'}
        return result

    def reports_snapshot(self,workspace,p):
        source=p.get('source','simulation')
        if source not in ('simulation','imported'):raise BankError('INVALID_SOURCE','请选择实际账单来源。')
        # 写锁固定整组读取边界；各现有只读服务独立连接，在 WAL 下读取同一提交版本。
        with self.bank.transaction() as db:
            state=self.source(workspace)
            v=state['sources'][source]['source_version']
            if v is None:raise BankError('NO_IMPORTED_DATA','当前账户没有已导入账单。',409)
            if p.get('source_version')!=v:raise BankError('SOURCE_VERSION_CONFLICT','账单版本已变化，请重新核对来源。',409)
            service=self.bank if source=='simulation' else self.statements
            month=p.get('month','2026-09');year=p.get('year',month[:4])
            if not re.fullmatch(r'\d{4}',year):raise BankError('INVALID_YEAR','年度格式无效。')
            result={'source_binding':{'source_scope':source,'workspace_id':workspace,'source_version':v},
                'analysis':service.analysis(workspace,month),'annual_analysis':service.annual_analysis(workspace,int(year)),
                'transactions':service.transactions(workspace),'anomalies':service.anomalies(workspace),
                'periodic_charges':service.periodic_charges(workspace)}
        return result

    def financial_records(self, workspace):
        archive=self.bank.customer_archive(workspace)
        fields={'id','name','loan_type','status','original_principal_minor','outstanding_minor',
                'annual_rate_bps','matures_on','principal_minor','started_on','due_on','interest_bps',
                'target_minor','saved_minor','topic','resolution','expires_on','scope','permission'}
        result = {'as_of':archive['as_of'],'version':archive['version'],'environment':'simulation',
                'records':{key:[{k:v for k,v in row.items() if k in fields} for row in archive['records'].get(key,[])]
                           for key in ('loans','deposit_contracts','wealth_goals','support_cases','permission_grants')},
                'insurance':{'status':'unavailable','notice':'当前账户未接入保障合同数据，没有将缺失数据视为没有保障。'}}
        if self.financial_data is not None and self.demo_accounts.is_demo(workspace):
            policies = self.financial_query(workspace, 'list_insurance', {'status':'active'})
            result['insurance'] = {'status':'available', 'source':'navren_synthetic',
                'dataset_version':policies['dataset_version'], 'as_of':policies['as_of'],
                'policies':policies['data']['items'], 'sum_insured_is_not_asset':True,
                'notice':'保障合同资料；现金价值与未提供的理赔保持未知。'}
            result['independent_financial_data'] = self.demo_accounts.independent_reading_status()
        return result

    def set_source(self, workspace, payload):
        return self.statements.set_source(workspace, payload)

    def analysis(self, workspace, month):
        return self.statements.analysis(workspace, month) if self.source(workspace)['active_source']=='imported' else self.bank.analysis(workspace, month)

    def transactions(self, workspace, month=None):
        return self.statements.transactions(workspace, month) if self.source(workspace)['active_source']=='imported' else self.bank.transactions(workspace, month)

    def anomalies(self, workspace):
        return self.statements.anomalies(workspace) if self.source(workspace)['active_source']=='imported' else self.bank.anomalies(workspace)

    def periodic_charges(self, workspace):
        return self.statements.periodic_charges(workspace) if self.source(workspace)['active_source']=='imported' else self.bank.periodic_charges(workspace)

    def mark_periodic_candidate(self, workspace, payload):
        return self.statements.mark_periodic_candidate(workspace, payload) if self.source(workspace)['active_source']=='imported' else self.bank.mark_periodic_candidate(workspace, payload)

    def annual_analysis(self, workspace, year):
        return self.statements.annual_analysis(workspace, year) if self.source(workspace)['active_source']=='imported' else self.bank.annual_analysis(workspace, year)

    def capabilities(self):
        return {**self.bank.capabilities(),
                'conversation_runtime': {'architecture':'native_tool_agent',
                    'primary':'DeepSeek-V4-Flash','primary_provider':'ai_ping',
                    'reviewer':'GLM-5.3-Flash','reviewer_provider':'ai_ping',
                    'user_confirmation_required':True,'semantic_keyword_router':False},
                'enabled': self.bank.capabilities()['enabled'] + ['cashflow_timeline','statement_csv_preview','statement_csv_import','statement_source_switch',
                    'simulated_transfer','scheduled_transfer','split_bill_requests','split_bill_receipts',
                    'merchant_subscription_cancellation','fund_reservations','service_booking','simulation_clock'],
                'unavailable': self.bank.capabilities()['unavailable'] + ['real_bank_statement_sync'],
                'read_capabilities': self.read_capabilities.catalog(),
                'agent_capabilities': workflow_capability_catalog(),
                'service_benefit_negotiation': {'enabled': True, 'environment': 'synthetic',
                    'rules_version': 'navren.synthetic-service-benefits.v1',
                    'execution_requires_user_confirmation': True}}

    def origin_check(self, handler):
        authority = {f'127.0.0.1:{handler.server.server_port}',f'localhost:{handler.server.server_port}'}
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback or handler.headers.get('Host') not in authority:
            raise BankError('LOCAL_ONLY','银行演示仅供本机同源访问。',403)
        origin = handler.headers.get('Origin')
        if origin and origin not in {'http://'+x for x in authority}:
            raise BankError('CROSS_ORIGIN_DENIED','拒绝跨站银行请求。',403)
        if handler.headers.get('Sec-Fetch-Site') in ('cross-site','same-site'):
            raise BankError('CROSS_ORIGIN_DENIED','拒绝跨站银行请求。',403)

    def identity(self, handler):
        cookies = SimpleCookie()
        try: cookies.load(handler.headers.get('Cookie',''))
        except Exception: raise BankError('UNAUTHENTICATED','银行会话无效。',401)
        token = cookies[COOKIE].value if COOKIE in cookies else ''
        workspace,role = self.bank.session(token)
        if role!='human': raise BankError('HUMAN_REQUIRED','当前会话不能确认操作。',403)
        if self.experience_provider is not None:self.experience_provider.validate(token,workspace)
        handler._navren_session_token = token
        return workspace,token

    @staticmethod
    def csrf(token):
        return digest(token+':qingzhou-csrf-v1')

    @staticmethod
    def demo_code(token):
        # 仅本机模拟第二因子；独立用户界面读取，绝不送入Agent上下文。
        return f'{int(hashlib.sha256((token+":demo-code").encode()).hexdigest()[:12],16)%1000000:06d}'

    def check_csrf(self,handler,token):
        value = handler.headers.get('X-QZ-CSRF','')
        if not hmac.compare_digest(value,self.csrf(token)):
            raise BankError('CSRF_DENIED','页面会话已变化，请刷新后重新查看操作。',403)

    def create_session(self):
        if self.registered_mode:
            raise BankError('REGISTERED_LOGIN_REQUIRED','请先完成手机号注册并选择本人画像。',401)
        catalog = self.demo_accounts.catalog()
        if catalog['accounts']:
            return self.demo_sessions.create(catalog['accounts'][0]['user_id'])
        credentials = self.bank.create_workspace()
        token,workspace = credentials['session_token'],credentials['workspace_id']
        with self.bank.transaction() as db:
            db.execute('UPDATE workspaces SET pin_hash=? WHERE id=?',(digest(workspace+':'+self.demo_code(token)),workspace))
            self.bank._audit(db,workspace,'application_session_created',{'environment':'simulation'})
        return workspace,token

    def _committed_personal_result(self, handler, workspace, mutation, *args, **kwargs):
        from user_memory import privacy_revision
        start = getattr(handler, '_navren_start_revision', None)
        if start is None:
            with closing(self.bank.connect()) as db:
                start = privacy_revision(db, workspace)
        result, receipt = self.personal_data.committed_response(
            workspace, start, mutation, workspace, *args, **kwargs)
        # 本请求真实提交的版本替代起始版本；原发送 guard 仍拒绝随后外部变化。
        handler._navren_start_revision = receipt.privacy_after
        return result

    def session_data(self,workspace,token):
        from user_memory import privacy_revision
        with closing(self.bank.connect()) as db:revision=privacy_revision(db,workspace)
        identity=self.demo_sessions.describe(token)
        result={'environment':'simulation','workspace_id':workspace,'csrf':self.csrf(token),'privacy_revision':revision,
                'personal_revision':self.personal_data.revision(workspace),
                'demo_identity':identity,
                'account_id':identity.get('user_id'),
                'safe_switch':True}
        if self.experience_provider is not None:
            binding=self.experience_provider.validate(token,workspace)
            result.update(registered_user_id=binding['registered_user_id'],persona_id=binding['persona_id'],
                          auth_subject=binding['auth_subject'],
                          session_generation=binding['generation'],auth_expires_at=binding['auth_expires_at'],
                          lease_expires_at=binding['expires_at'])
        return result

    def approve_speech(self, workspace, result, session_token=None):
        """只有后端已校验的本轮答复可以获取短期语音凭据。正文不放入凭据。"""
        if result.get('isFallback') or not result.get('compliance_pass') or not result.get('answer_contract'):
            return None
        from agent_runtime.data_boundary import CloudDataBoundary
        from user_memory import privacy_revision
        text=result.get('_approved_tts_text') or CloudDataBoundary().tts_text(result.get('reply',''))
        if not text.strip():return None
        with self.bank.transaction() as db:
            revision=privacy_revision(db,workspace)
            personal=self.personal_data.revision(workspace)
            if result.get('personal_revision')!=personal or result.get('privacy_revision')!=revision:
                return None
            from local_knowledge.catalog_information_host import response_snapshot
            catalog_information=response_snapshot(result)
            if catalog_information is not None:
                self.catalog_information_host.lookup(catalog_information,workspace=workspace,
                    privacy_revision=revision,personal_revision=personal,request_id=result.get('bank_response_id'))
            receipt=secrets.token_urlsafe(32)
            with self._speech_lock:
                now=time.monotonic()
                self._speech_receipts={k:v for k,v in self._speech_receipts.items() if v['expires']>now}
                # 有界内存；只保存最近已核验语音，不持久化原文。
                if len(self._speech_receipts)>=256:self._speech_receipts.pop(next(iter(self._speech_receipts)))
                self._speech_receipts[receipt]=deepcopy({'owner':workspace,'expires':now+600,'privacy':revision,
                    'personal':personal,'knowledge':result.get('knowledge_snapshot'),
                    'context':result.get('_answer_context'),'text':text,'market':result.get('market_snapshot'),
                    'service':result.get('service_snapshot'),
                    'metrics':result.get('financial_metric_snapshot'),
                    'catalog_information':catalog_information,'request_id':result.get('bank_response_id'),
                    'metric_batches':result.get('financial_metric_batches',[]),
                    'session_hash':digest(session_token) if session_token else None})
        return receipt

    def resolve_speech(self, workspace, receipt, session_token=None):
        with self.speech_guard(workspace,receipt,session_token) as text:return text

    def assert_speech_receipt(self,workspace,receipt,session_token=None):
        """每次实际播报前重查服务端凭据和时限；不申请或延长凭据。"""
        with self._speech_lock:
            value=self._speech_receipts.get(receipt) if isinstance(receipt,str) else None
            value=deepcopy(value) if value else None
        if not value or value['owner']!=workspace or value['expires']<=time.monotonic():
            raise BankError('SPEECH_RECEIPT_INVALID','这段答复没有有效的语音凭据，请重新查询。',403)
        if value.get('session_hash'):
            if not session_token or digest(session_token)!=value['session_hash']:
                raise BankError('SPEECH_RECEIPT_INVALID','账户会话已变化，请重新查询这段答复。',403)
            self.demo_sessions.validate(session_token,workspace)
        if value['expires']<=time.monotonic():
            raise BankError('SPEECH_RECEIPT_INVALID','这段答复没有有效的语音凭据，请重新查询。',403)
        if value.get('catalog_information') is not None:
            self.catalog_information_host.lookup(value['catalog_information'],workspace=workspace,
                privacy_revision=value['privacy'],personal_revision=value['personal'],request_id=value['request_id'])
        return value

    def assert_speech_evidence(self,workspace,receipt,session_token=None, *, db):
        """在已取得全部发布锁的发送区，以同一银行连接重查凭据及证据。"""
        value=self.assert_speech_receipt(workspace,receipt,session_token)
        self.assert_response_evidence(workspace,value['privacy'],value['personal'],value['knowledge'],value['context'],
            db=db,market_snapshot=value.get('market'),metric_snapshot=value.get('metrics'),
            service_snapshot=value.get('service'),metric_batches=value.get('metric_batches'),
            catalog_information=value.get('catalog_information'))
        # Original/metric validation can take time; TTL must still hold when
        # control returns to the caller that will actually send the text/audio.
        return self.assert_speech_receipt(workspace,receipt,session_token)['text']

    def assert_publication_current(self):
        """实际发送入口调用当前线程的只读检查；不存储待发送正文。"""
        checkpoint=_publication_checkpoint.get()
        if checkpoint is None:return False
        checkpoint()
        return True

    def publication_send_timeout(self):
        """受信原轮时钟限定实际写入等待；普通流由socket原超时决定。"""
        remaining=_publication_remaining.get()
        if remaining is None:return 2.0
        seconds=remaining()
        if seconds is None:return 2.0
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds):
            raise BankError('PUBLICATION_CLOCK_INVALID','发布时钟不可用，答复已停止。',503)
        if seconds<=0:raise BankError('AGENT_BUDGET_EXCEEDED','本轮处理已超过时限，答复已停止。',409)
        return min(2.0,seconds)

    @contextmanager
    def speech_guard(self,workspace,receipt,session_token=None):
        try:
            value=self.assert_speech_receipt(workspace,receipt,session_token)
            with self.publication_guard(workspace,value['privacy'],value['personal'],value['knowledge'],value['context'],
                                     market_snapshot=value.get('market'),metric_snapshot=value.get('metrics'),
                                     service_snapshot=value.get('service'),metric_batches=value.get('metric_batches'),
                                     catalog_information=value.get('catalog_information')) as db:
                checkpoint=lambda:self.assert_speech_evidence(workspace,receipt,session_token,db=db)
                token=_publication_checkpoint.set(checkpoint)
                try:yield checkpoint()
                finally:_publication_checkpoint.reset(token)
        except Exception:
            with self._speech_lock:
                current=self._speech_receipts.get(receipt) if isinstance(receipt,str) else None
                if current and current['owner']==workspace:self._speech_receipts.pop(receipt,None)
            raise

    def assert_response_evidence(self,workspace,expected_privacy,expected_personal,knowledge_snapshot=None,expected_context=None,
                                *,db,market_snapshot=None,metric_snapshot=None,service_snapshot=None,metric_batches=None,catalog_information=None):
        """只读末端复查；调用方须持有银行、公开库及适用服务规则锁。"""
        from user_memory import privacy_revision
        from simulated_bank.agent_guard import context_hash
        if privacy_revision(db,workspace)!=expected_privacy or self.personal_data.revision(workspace)!=expected_personal:
            raise BankError('PERSONAL_VERSION_CONFLICT','个人资料已变化，旧答复已丢弃，请重新查询。',409)
        if catalog_information is not None:
            self.catalog_information_host.lookup(catalog_information,workspace=workspace,
                privacy_revision=expected_privacy,personal_revision=expected_personal)
        if expected_context and context_hash(db,workspace)!=expected_context:
            raise BankError('STALE_EVIDENCE','账户或资料已变化，旧答复已丢弃，请重新查询。',409)
        if metric_snapshot:self.financial_metrics.validate(workspace,metric_snapshot,db)
        for batch in metric_batches or []:self.financial_metrics.validate(workspace,batch,db)
        self.operation_evidence.assert_public_evidence({'market':market_snapshot,'knowledge':knowledge_snapshot})
        if service_snapshot:
            self.service_negotiations.assert_evidence(workspace,service_snapshot,db=db)

    @contextmanager
    def response_guard(self,workspace,expected_privacy,expected_personal,knowledge_snapshot=None,expected_context=None,
                       *,market_snapshot=None,metric_snapshot=None,service_snapshot=None,metric_batches=None,catalog_information=None):
        """提交前验证和绑定；退出并提交成功后才能进入只读发布区。"""
        with ExitStack() as locks, self.bank.transaction() as db:
            locks.enter_context(self.operation_evidence.public_guard({'market':market_snapshot,'knowledge':knowledge_snapshot}))
            locks.enter_context(self.catalog_information_host.guard(catalog_information,workspace=workspace,
                privacy_revision=expected_privacy,personal_revision=expected_personal))
            if service_snapshot:
                locks.enter_context(self.service_negotiations.evidence_guard(workspace,service_snapshot,db=db))
            checkpoint=lambda:self.assert_response_evidence(workspace,expected_privacy,expected_personal,knowledge_snapshot,expected_context,
                db=db,market_snapshot=market_snapshot,metric_snapshot=metric_snapshot,
                service_snapshot=service_snapshot,metric_batches=metric_batches,catalog_information=catalog_information)
            checkpoint()
            token=_publication_checkpoint.set(checkpoint)
            try:
                yield db
            finally:
                _publication_checkpoint.reset(token)

    @contextmanager
    def publication_guard(self,workspace,expected_privacy,expected_personal,knowledge_snapshot=None,expected_context=None,
                          *,market_snapshot=None,metric_snapshot=None,service_snapshot=None,metric_batches=None,
                          before_send=None,budget_check=None,budget_remaining=None,catalog_information=None):
        """提交后重新按原锁序校验；发布区银行连接只读、释放时回滚空事务。"""
        if before_send is not None and not callable(before_send):raise TypeError('发布前处理器必须由本地服务注入。')
        if budget_check is not None and not callable(budget_check):raise TypeError('发布预算检查必须由本地服务注入。')
        if budget_remaining is not None and not callable(budget_remaining):raise TypeError('剩余时限必须由本地服务注入。')
        with ExitStack() as locks, closing(self.bank.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            token=None;remaining_token=None
            try:
                identity_guard=getattr(self.bank,'request_identity_guard',None)
                if identity_guard:identity_guard(db)
                locks.enter_context(self.operation_evidence.public_guard({'market':market_snapshot,'knowledge':knowledge_snapshot}))
                locks.enter_context(self.catalog_information_host.guard(catalog_information,workspace=workspace,
                    privacy_revision=expected_privacy,personal_revision=expected_personal))
                if service_snapshot:
                    locks.enter_context(self.service_negotiations.evidence_guard(workspace,service_snapshot,db=db))
                def validate():
                    if budget_check is not None:budget_check()
                    self.assert_response_evidence(workspace,expected_privacy,expected_personal,knowledge_snapshot,expected_context,
                        db=db,market_snapshot=market_snapshot,metric_snapshot=metric_snapshot,
                        service_snapshot=service_snapshot,metric_batches=metric_batches,catalog_information=catalog_information)
                    if budget_check is not None:budget_check()
                validate()
                # 禁止发布区继续写入银行状态；此处无需也不能依赖后置commit。
                forbidden={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                           sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE}
                db.set_authorizer(lambda action,*args:sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK)
                prepared=False
                def checkpoint():
                    nonlocal prepared
                    validate()
                    if before_send is not None and not prepared:
                        before_send(db)
                        prepared=True
                        validate()
                token=_publication_checkpoint.set(checkpoint)
                remaining_token=_publication_remaining.set(budget_remaining)
                yield db
            finally:
                if remaining_token is not None:_publication_remaining.reset(remaining_token)
                if token is not None:_publication_checkpoint.reset(token)
                # 银行业务已在前一事务提交。空读锁事务无后置写提交可失败。
                db.rollback()

    def discard_unpublished_answer(self,workspace,request_id,result,*,expected_privacy,expected_personal,history_saved=False,
                                   failure_stage='finalization',failure_code='PUBLICATION_FAILED'):
        """受信本地撤回：只降低本请求权限，不接受HTTP或模型调用。"""
        if failure_stage not in ('finalization','binding','publication'):failure_stage='finalization'
        if not isinstance(failure_code,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',failure_code):
            failure_code='PUBLICATION_FAILED'
        outcome={'speech_receipt_revoked':False,'drafts_withdrawn_count':0,
                 'history_status':'not_requested','failure_stage':failure_stage,'failure_code':failure_code}
        receipt=result.get('speech_receipt')
        with self._speech_lock:
            current=self._speech_receipts.get(receipt) if isinstance(receipt,str) else None
            if current and current['owner']==workspace:
                self._speech_receipts.pop(receipt,None);outcome['speech_receipt_revoked']=True
        if not isinstance(request_id,str) or not re.fullmatch(r'[0-9a-f]{32}',request_id):
            raise BankError('INVALID_REQUEST_ID','未发布答复缺少受信请求身份。',503)
        # 撤回可以发生在原会话已切换之后。此连接仅取消同轮草案；没有任何
        # 金额/账本执行路径，不借撤回扩大旧会话的金融授权。
        with closing(self.bank.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            history_error=None
            try:
                self.bank._workspace(db,workspace)
                prefix='agent-'+request_id+'-'
                rows=db.execute("SELECT o.id,o.idempotency_key FROM operations o JOIN agent_draft_reviews a ON a.operation_id=o.id WHERE o.workspace_id=? AND a.workspace_id=? AND o.state='pending_confirmation'",
                                (workspace,workspace)).fetchall()
                for row in rows:
                    if not row['idempotency_key'].startswith(prefix):continue
                    db.execute("UPDATE operations SET state='cancelled',verification_hash=NULL WHERE id=? AND state='pending_confirmation'",(row['id'],))
                    db.execute("UPDATE agent_draft_reviews SET status='superseded' WHERE operation_id=?",(row['id'],))
                    self.bank._audit(db,workspace,'agent_unpublished_draft_withdrawn',{},row['id'])
                    outcome['drafts_withdrawn_count']+=1
                if history_saved:
                    try:
                        discard=getattr(self.personal_data,'discard_turn_guarded',None)
                        if not callable(discard):
                            outcome['history_status']='unavailable'
                            raise BankError('HISTORY_CLEANUP_UNAVAILABLE','未发布对话的撤回接口尚未接入。',503)
                        restored=discard(workspace,request_id,bank_db=db,expected_revision=expected_personal,
                                         expected_privacy=expected_privacy)
                        outcome['history_status']='discarded' if restored is True else 'partial_or_stale'
                    except Exception as error:
                        history_error=error
                        if outcome['history_status']!='unavailable':outcome['history_status']='error'
                # 纯分析也留下撤回结果；不记录正文、私库内容、receipt或异常原文。
                self.bank._audit(db,workspace,'agent_unpublished_answer_withdrawal',{'request_id':request_id,**outcome})
                db.commit()
            except Exception:
                db.rollback()
                raise
        # 私库清理不可用时仍保留已提交的草案撤权；不能回滚成可确认状态。
        if history_error is not None:raise history_error
        return outcome

    def cashflow_timeline(self,workspace,horizon_days=90):
        try:
            validate_horizon_days(horizon_days)
        except ValueError as error:
            raise BankError('INVALID_HORIZON',str(error))
        archive=self.bank.customer_archive(workspace)
        if self.demo_accounts.is_demo(workspace):
            data=self.demo_accounts.current(workspace)
            archive['data_scope']='navren_synthetic'
            archive['records']['recorded_forecasts']=data.get('recurring_obligations',data.get('forecasts',[]))
            archive['source_version']=data['source_version']
        return build_cashflow_timeline(archive,horizon_days,workspace_id=workspace)

    def overview(self,workspace):
        self.bank.process_due_remittances(workspace)
        archive = self.bank.customer_archive(workspace)
        accounts,profile,records = archive['accounts'],archive['profile'],archive['records']
        today = date.fromisoformat(archive['as_of'])
        end = (today+timedelta(days=30)).isoformat()
        commitments = []
        loans = {r['id']:r for r in records['loans']}
        for r in records['loan_installments']:
            if r['status'] in ('scheduled','overdue') and r['due_on']<=end:
                due = max(0,r['principal_minor']+r['interest_minor']+r['fees_minor']-r['paid_minor'])
                commitments.append({'kind':'loan','name':loans[r['loan_id']]['loan_type'],'account_id':loans[r['loan_id']]['repayment_account_id'],
                                    'amount_minor':due,'due_on':r['due_on'],'evidence_id':r['id']})
        cards = {r['id']:r for r in records['cards']}
        for r in records['credit_statements']:
            if r['status']=='open' and r['due_on']<=end:
                commitments.append({'kind':'credit_card','name':'信用卡本期应还（全额口径）','account_id':cards[r['card_id']]['account_id'],
                                    'amount_minor':max(0,r['total_due_minor']-r['paid_minor']),'due_on':r['due_on'],'evidence_id':r['id']})
        mandates = {r['id']:r for r in records['mandates']}
        linked = set()
        for r in records['subscriptions']:
            linked.add(r['mandate_id'])
            if r['merchant_status']=='active' and r['next_renewal_on']<=end:
                mandate=mandates.get(r['mandate_id'])
                name=r['plan_name']+('（商户合同仍有效）' if mandate and mandate['status']=='closed' else '')
                commitments.append({'kind':'subscription','name':name,'account_id':r['account_id'],
                                    'amount_minor':r['amount_minor'],'due_on':r['next_renewal_on'],'evidence_id':r['id']})
        for r in records['mandates']:
            if r['id'] not in linked and r['status']=='active' and r['next_charge_date']<=end:
                commitments.append({'kind':'mandate','name':r['merchant'],'account_id':r['account_id'],
                                    'amount_minor':r['amount_minor'],'due_on':r['next_charge_date'],'evidence_id':r['id']})
        reserved_schedules={r['scheduled_payment_id'] for r in records['remittances'] if r['status']=='scheduled'}
        for r in records['scheduled_payments']:
            if r['id'] in reserved_schedules:continue
            if r['status']=='active' and r['next_due_on']<=end:
                beneficiary=next((b for b in records['beneficiaries'] if b['id']==r['beneficiary_id']),None)
                commitments.append({'kind':'scheduled_transfer','name':'定时转账 · '+(beneficiary['display_name'] if beneficiary else '模拟收款人'),
                                    'account_id':r['account_id'],'amount_minor':r['amount_minor'],
                                    'due_on':r['next_due_on'],'evidence_id':r['id']})
        total_due = sum(r['amount_minor'] for r in commitments)
        cash = sum(r['balance_minor'] for r in accounts)
        assets = archive['financial_summary']
        held_by_account={a['id']:sum(h['amount_minor'] for h in records['fund_holds']
                                    if h['account_id']==a['id'] and h['status']=='held')+
                                sum(r['amount_minor'] for r in records['remittances']
                                    if r['account_id']==a['id'] and r['status']=='scheduled') for a in accounts}
        accounts=[{**a,'held_minor':held_by_account[a['id']],
                   'available_minor':max(0,a['balance_minor']-held_by_account[a['id']])} for a in accounts]
        connection_states=self.account_connections.overview_states(workspace)
        account_connections={a['account_id']:b for b in connection_states for a in b['accounts']}
        accounts=[{**a,'binding_id':account_connections[a['id']]['id'] if a['id'] in account_connections else None,
                   'connection_status':account_connections[a['id']]['status'] if a['id'] in account_connections else 'connected',
                   'connection_active':account_connections[a['id']]['active'] if a['id'] in account_connections else True} for a in accounts]
        card_views=[]
        for card in records['cards']:
            card_views.append({**card,
                'controls':[c for c in records['card_controls'] if c['card_id']==card['id']],
                'statements':[s for s in records['credit_statements'] if s['card_id']==card['id']],
                'limit_requests':[r for r in records['card_limit_requests'] if r['card_id']==card['id']],
                'events':[e for e in records['card_events'] if e['card_id']==card['id']]})
        return {'environment':'simulation','workspace_id':workspace,'as_of':archive['as_of'],'version':archive['version'],
                'accounts':accounts,'account_connections':connection_states,'profile':profile,'holdings':records['holdings'],'lots':records['investment_lots'],
                'mandates':records['mandates'],'orders':records['orders'],'operations':records['operations'],
                'cards':card_views,'card_applications':records['card_applications'],
                'beneficiaries':records['beneficiaries'],'scheduled_payments':records['scheduled_payments'],
                'payment_instructions':records['payment_instructions'],'remittances':records['remittances'],
                'split_bills':records['split_bills'],'split_participants':records['split_participants'],
                'subscriptions':records['subscriptions'],'subscription_events':records['subscription_events'],
                'orchestration_tasks':records['orchestration_tasks'],'task_steps':records['task_steps'],
                'fund_holds':records['fund_holds'],'service_quotes':records['service_quotes'],
                'service_reservations':records['service_reservations'],'held_funds_by_account':held_by_account,
                'commitments':commitments,'commitment_window_end':end,'upcoming_due_minor':total_due,
                'held_funds_minor':sum(held_by_account.values()),
                'total_cash_minor':cash,'holding_principal_minor':assets['holding_principal_minor'],
                'pending_redemption_minor':assets['pending_redemption_minor'],'liabilities_minor':assets['liabilities_minor'],
                'net_assets_principal_minor':assets['net_assets_principal_minor'],
                'total_assets_principal_minor':cash+assets['holding_principal_minor']+assets['pending_redemption_minor'],
                'investable_minor':max(0,cash-profile['reserve_minor']-total_due-sum(held_by_account.values())),
                'cashflow_timeline':build_cashflow_timeline(archive,90,workspace_id=workspace),
                'integrity':archive['integrity'],'notice':'本金口径；仅计已结算现金。30日内到期款和生效的任务预留均从可投资金额中扣除；银行代扣与商户合同分开。不预支未来工资，未预测其他生活支出。'}

    def budget(self,workspace,account_id=None,floor=0,exclude_hold_id=None):
        state = self.overview(workspace)
        account = next((a for a in state['accounts'] if a['id']==account_id),None)
        if account_id and not account: raise BankError('NOT_FOUND','当前客户没有该账户。',404)
        reserve = max(integer(floor,'保留资金'),state['profile']['reserve_minor'])
        commitments=state['commitments'];due=state['upcoming_due_minor']
        if self.demo_accounts.is_demo(workspace) and account:
            from cashflow_timeline import demo_reserve_floors
            reserve=max(integer(floor,'保留资金'),demo_reserve_floors(state['accounts'],state['profile']['reserve_minor'])[account['id']])
            commitments=[c for c in commitments if c['account_id']==account['id']]
            due=sum(c['amount_minor'] for c in commitments)
        # 演示画像按账户分配同一全户底线；旧工作区保持保守的付款账户底线。
        account_cash = account['balance_minor'] if account else state['total_cash_minor']
        held=state['held_funds_minor'] if not account else state['held_funds_by_account'].get(account['id'],0)
        if exclude_hold_id:
            excluded=next((h for h in state['fund_holds'] if h['id']==exclude_hold_id and h['status']=='held'),None)
            if excluded and (not account or excluded['account_id']==account['id']): held=max(0,held-excluded['amount_minor'])
        return {'cash_minor':account_cash,'reserve_minor':reserve,'upcoming_due_minor':due,'held_minor':held,
                'investable_minor':max(0,account_cash-reserve-due-held),
                'commitments':commitments,'as_of':state['as_of'],'version':state['version'],
                'reserve_basis':'single_household_allocation' if self.demo_accounts.is_demo(workspace) else 'conservative_payment_account_floor'}

    def prepare(self,workspace,payload, *, agent_origin=False):
        strict_fields(payload,['action','account_id','product_id','amount_minor','mandate_id','card_id','control_type','enabled',
                               'limit_minor','requested_type','requested_limit_minor','idempotency_key','liquidity_floor_minor',
                               'beneficiary_id','reference','frequency','next_due_on','end_on','scheduled_payment_id',
                               'split_id','participant_id','subscription_id','task_id','expires_on','hold_id','quote_ids','remittance',
                               'aa_bill','aa_share_id','aa_bill_id','replacement_id','purpose'],
                      ['action','account_id','idempotency_key'])
        core = {k:v for k,v in payload.items() if k!='liquidity_floor_minor'}
        for key in ('action','account_id','idempotency_key'):
            if not isinstance(core[key],str) or not 1<=len(core[key])<=128:
                raise BankError('INVALID_FIELDS','操作、账户和幂等键必须为字符串。')
        floor = integer(payload.get('liquidity_floor_minor',0),'保留资金')
        with closing(self.bank.connect()) as db:
            if core['action']=='aa_create':
                from simulated_bank.aa_payments import canonical
                core=canonical(self.bank,db,workspace,core)
            if core['action'] in ('transfer','schedule_transfer') and 'remittance' in core:
                from simulated_bank.remittance import canonical_payload
                core=canonical_payload(self.bank,db,workspace,core)
            existing = db.execute('SELECT id,payload_hash,state FROM operations WHERE workspace_id=? AND idempotency_key=?',(workspace,core['idempotency_key'])).fetchone()
        if not existing or existing['state'] not in TERMINAL:
            if core['action']=='purchase':
                budget = self.budget(workspace,core['account_id'],floor)
                if integer(core.get('amount_minor'),'申购金额',1)>budget['investable_minor']:
                    raise BankError('LIQUIDITY_GUARD','申购金额超过扣除保留资金及近期到期款后的可投资额度。',422)
            elif core['action']=='reserve_funds':
                budget=self.budget(workspace,core['account_id'])
                if integer(core.get('amount_minor'),'操作金额',1)>budget['investable_minor']:
                    raise BankError('LIQUIDITY_GUARD','金额超过扣除应急金、到期款和已预留资金后的可用额度。',422)
            elif core['action']=='book_services':
                budget=self.budget(workspace,core['account_id'],exclude_hold_id=core.get('hold_id'))
                archive=self.bank.customer_archive(workspace)['records']
                quote_ids=core.get('quote_ids')
                quote_total=sum(q['price_minor'] for q in archive['service_quotes'] if quote_ids and q['id'] in quote_ids)
                if quote_total>budget['investable_minor']:
                    raise BankError('LIQUIDITY_GUARD','预约金额超过扣除其他保留款和到期款后的可用额度。',422)
        metrics = self.financial_metrics.snapshot(workspace,60)
        op = self.financial_metrics.prepare_bank_operation(workspace,core,agent_origin=agent_origin,metric_required=True)
        with self.bank.transaction() as db:
            policies = db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? AND event='liquidity_policy' ORDER BY id LIMIT 1",(workspace,op['id'])).fetchone()
            if policies:
                if json.loads(policies['details'])['floor_minor']!=floor:
                    raise BankError('IDEMPOTENCY_CONFLICT','同一操作不能更改保留资金约束。',409)
            else: self.bank._audit(db,workspace,'liquidity_policy',{'floor_minor':floor},op['id'])
        self.financial_metrics.bind_operation(workspace,op['id'],metrics)
        return self.operation_view(workspace,op['id'])

    def operation_view(self,workspace,operation_id):
        from simulated_bank.aa_payments import operation_view as aa_operation_view
        state = self.overview(workspace)
        op = self.bank.operation(workspace,operation_id)
        aa=None
        if op['action'].startswith('aa_'):
            aa_snapshot=aa_operation_view(self.bank,workspace,operation_id,include_operation=True)
            op=aa_snapshot['operation'];aa=aa_snapshot['aa']
        payload = op['payload']
        product = next((p for p in self.bank.products(workspace) if p['id']==payload.get('product_id')),None)
        mandate = next((m for m in state['mandates'] if m['id']==payload.get('mandate_id')),None)
        card = next((c for c in state['cards'] if c['id']==payload.get('card_id')),None)
        beneficiary=next((b for b in state['beneficiaries'] if b['id']==payload.get('beneficiary_id')),None)
        schedule=next((s for s in state['scheduled_payments'] if s['id']==payload.get('scheduled_payment_id')),None)
        split=next((s for s in state['split_bills'] if s['id']==payload.get('split_id')),None)
        if split:
            split={**split,'participants':[p for p in state['split_participants'] if p['split_id']==split['id']]}
        participant=next((p for p in state['split_participants'] if p['id']==payload.get('participant_id')),None)
        subscription=next((s for s in state['subscriptions'] if s['id']==payload.get('subscription_id')),None)
        task=next((t for t in state['orchestration_tasks'] if t['id']==payload.get('task_id')),None)
        hold=next((h for h in state['fund_holds'] if h['id']==payload.get('hold_id')),None)
        quotes=[q for q in state['service_quotes'] if payload.get('quote_ids') and q['id'] in payload['quote_ids']]
        with closing(self.bank.connect()) as db:
            policy = db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? AND event='liquidity_policy' ORDER BY id LIMIT 1",(workspace,operation_id)).fetchone()
            agent_review = db.execute('SELECT status,policy_version,expires_at FROM agent_draft_reviews WHERE workspace_id=? AND operation_id=?',
                                      (workspace,operation_id)).fetchone()
            strong_verification = self.bank.permissions.status(db,workspace,operation_id)
            from simulated_bank.operation_mfa_evidence import operation_mfa_evidence
            mfa_evidence = operation_mfa_evidence(db, workspace, operation_id, now=self.bank.clock(),
                provider=self.bank.permissions.provider, status=strong_verification)
            transfer_terms=self.bank.ordinary_transfer_terms(db,workspace,operation_id) if op['action']=='transfer' and 'remittance' not in payload else None
        floor = json.loads(policy['details'])['floor_minor'] if policy else 0
        return {'operation':op,'agent_review':dict(agent_review) if agent_review else None,'product':product,'mandate':mandate,'card':card,
                'aa':aa,
                **({'transfer_terms':transfer_terms} if op['action']=='transfer' and 'remittance' not in payload else {}),
                'remittance':self.bank.remittances(workspace,operation_id,process_due=False) if 'remittance' in payload else None,
                'beneficiary':beneficiary,'schedule':schedule,'split':split,'participant':participant,
                'subscription':subscription,'task':task,'hold':hold,'quotes':quotes,
                'card_application':{'requested_type':payload.get('requested_type'),
                                    'requested_limit_minor':payload.get('requested_limit_minor'),
                                    'virtual_terms':{'purpose':payload.get('purpose','online_payment'),'fee_minor':0,
                                        'validity_days':365,'credit_limit_minor':0,'real_payment_capability':False,
                                        'single_transaction_limit_minor':payload.get('requested_limit_minor'),
                                        'terms_version':'navren.virtual-card.demo/1'} if payload.get('requested_type')=='virtual' else None} if op['action']=='apply_card' else None,
                'account':next(a for a in state['accounts'] if a['id']==payload['account_id']),
                'budget':self.budget(workspace,payload['account_id'],floor,
                                     payload.get('hold_id') if op['action']=='book_services' else None),
                'liquidity_floor_minor':floor,
                'environment':'simulation','verification_required':op['permission']=='red',
                'strong_verification':strong_verification, 'operation_mfa_evidence':mfa_evidence}

    def confirm(self,workspace,operation_id,payload):
        try:
            strict_fields(payload,['confirmed','confirmation_digest','verification_code','challenge_id'],['confirmed','confirmation_digest'])
        except BankError:
            self.bank.permissions.failed_attempt(workspace,operation_id,'INVALID_FIELDS',suspicious=True)
            raise
        self.bank.process_due_remittances(workspace)
        op = self.bank.operation(workspace,operation_id)
        if op['state'] in TERMINAL or ('remittance' in op['payload'] and op['state'] in ('scheduled','failed')):
            result = self.bank.confirm(workspace,operation_id,{k:v for k,v in payload.items() if k not in ('verification_code','challenge_id')})
            return {'operation':result,'overview':self.overview(workspace)}
        if op['action'] in ('purchase','reserve_funds'):
            view = self.operation_view(workspace,operation_id)
            if op['payload']['amount_minor']>view['budget']['investable_minor']:
                raise BankError('LIQUIDITY_GUARD','当前资金不足以满足已确认的应急保留、到期款和资金预留约束，请重新准备。',422)
        elif op['action']=='book_services':
            view=self.operation_view(workspace,operation_id)
            total=sum(q['price_minor'] for q in view['quotes'])
            if total>view['budget']['investable_minor']:
                raise BankError('LIQUIDITY_GUARD','当前可用资金不足以完成预约，请先核对预留和到期款。',422)
        body = {k:v for k,v in payload.items() if k not in ('verification_code','challenge_id')}
        if body['confirmed'] is not True: raise BankError('CONFIRMATION_REQUIRED','请明确确认此操作。')
        if not isinstance(body['confirmation_digest'],str) or body['confirmation_digest']!=op['confirmation_digest']:
            self.bank.permissions.failed_attempt(workspace,operation_id,'CONFIRMATION_MISMATCH',suspicious=True)
            raise BankError('CONFIRMATION_MISMATCH','确认摘要不一致。',409)
        if op['permission']=='red':
            grant = self.bank.verify(workspace,operation_id,{'verification_code':payload.get('verification_code'),
                                                          'challenge_id':payload.get('challenge_id')})
            body['verification_token'] = grant['verification_token']
        result = self.bank.confirm(workspace,operation_id,body)
        if op['action']=='password_change':
            return {'operation':result,'relogin_required':True,'sessions_revoked':True,
                    'credential_scope':'simulated_bank_access'}
        return {'operation':result,'overview':self.overview(workspace)}

    def prepare_remittance(self,workspace,payload):
        fields={'account_id','card_id','sender_name','sender_phone','recipient_name','recipient_account',
                'recipient_bank','recipient_branch','amount_minor','purpose','message','mode','idempotency_key'}
        strict_fields(payload,fields,fields-{'message','sender_name','sender_phone'})
        if any(not isinstance(payload[k],str) or not 1<=len(payload[k])<=128 for k in ('account_id','idempotency_key')):
            raise BankError('INVALID_FIELDS','账户和幂等键必须为不超过128字符的字符串。')
        integer(payload['amount_minor'],'汇款金额',1)
        if payload['mode'] not in ('realtime','ordinary','next_day'):
            raise BankError('INVALID_REMITTANCE_MODE','汇款方式无效。')
        form={k:payload.get(k,'') for k in fields-{'account_id','amount_minor','idempotency_key','sender_name','sender_phone'}}
        core={'action':'transfer' if payload['mode']=='realtime' else 'schedule_transfer',
              'account_id':payload['account_id'],'amount_minor':payload['amount_minor'],
              'idempotency_key':payload['idempotency_key'],'remittance':form}
        if core['action']=='schedule_transfer':
            with closing(self.bank.connect()) as db:
                ws=self.bank._workspace(db,workspace)
                existing=db.execute('SELECT payload FROM operations WHERE workspace_id=? AND idempotency_key=?',
                                    (workspace,payload['idempotency_key'])).fetchone()
                old=json.loads(existing['payload']) if existing else {}
                due=old.get('next_due_on') or (date.fromisoformat(ws['as_of'])+timedelta(days=1)).isoformat()
            core.update(frequency='once',next_due_on=due)
        return self.prepare(workspace,core)

    def prepare_aa(self,workspace,payload,*,kind='create',object_id=None,agent_origin=False):
        from simulated_bank.aa_payments import identity,share_row,bill_row,text
        if kind=='create':
            fields={'account_id','title','total_minor','split_mode','participants','due_at','source_transaction_id','source_consumption_id','attachment_ids','idempotency_key'}
            strict_fields(payload,fields,fields-{'due_at','source_transaction_id','source_consumption_id','attachment_ids'})
            core={'action':'aa_create','account_id':payload['account_id'],'idempotency_key':payload['idempotency_key'],
                  'aa_bill':{k:v for k,v in payload.items() if k not in ('account_id','idempotency_key')}}
        elif kind=='pay':
            strict_fields(payload,{'account_id','card_id','idempotency_key'},{'account_id','card_id','idempotency_key'})
            core={**payload,'action':'aa_pay','aa_share_id':text(object_id,'share_id')}
        else:
            strict_fields(payload,{'idempotency_key'},{'idempotency_key'})
            with closing(self.bank.connect()) as db:
                identity(db,workspace=workspace)
                if kind=='cancel':
                    bill=bill_row(db,object_id)
                    if bill['creator_workspace']!=workspace:raise BankError('AA_NOT_FOUND','AA账单不存在或无权访问。',404)
                    account_id=bill['account_id'];field='aa_bill_id'
                elif kind=='decline':
                    share_row(db,workspace,object_id)
                    account_id=self.bank._accounts(db,workspace)[0]['id'];field='aa_share_id'
                else:raise BankError('INVALID_FIELDS','AA操作类型无效。')
            core={'action':'aa_'+kind,'account_id':account_id,field:object_id,'idempotency_key':payload['idempotency_key']}
        return self.prepare(workspace,core,agent_origin=agent_origin)

    def aa_bills(self,workspace,bill_id=None):
        from simulated_bank.aa_payments import bills
        return bills(self.bank,workspace,bill_id)

    def aa_consumptions(self,workspace,query):
        from simulated_bank.aa_evidence import consumptions
        if set(query)-{'q','from','to','limit','offset'} or any(len(v)!=1 for v in query.values()):
            raise BankError('INVALID_QUERY','消费查询参数无效。',400)
        return consumptions(self.bank,workspace,q=query.get('q',[''])[0],
            from_day=query.get('from',[None])[0],to_day=query.get('to',[None])[0],
            limit=self.page_number(query,'limit',20,1,50),offset=self.page_number(query,'offset',0,0,10000))

    def aa_attachment(self,workspace,attachment_id=None,payload=None,*,delete=False):
        from simulated_bank.aa_evidence import upload,read_attachment,delete_attachment
        if delete:
            strict_fields(payload,[])
            return delete_attachment(self.bank,workspace,attachment_id)
        if payload is not None:return upload(self.bank,workspace,payload)
        return read_attachment(self.bank,workspace,attachment_id)

    def payment_contacts(self,workspace,payload=None,contact_id=None,action=None):
        from simulated_bank.aa_payments import contacts
        return contacts(self.bank,workspace,payload,contact_id,action)

    def remittance_contacts(self,workspace,payload=None,contact_id=None,action=None):
        from simulated_bank.remittance_contacts import contacts
        return contacts(self.bank,workspace,payload,contact_id,action)

    def _audit_disconnected_response(self,path,result,workspace):
        match = re.fullmatch(r'/api/finance/operations/([a-f0-9]{32})/confirm',path)
        if not match:
            return
        workspace_ref = hashlib.sha256(workspace.encode('utf-8')).hexdigest()[:12]
        operation = result.get('operation') if isinstance(result,dict) else None
        state = operation.get('state') if isinstance(operation,dict) else None
        if not isinstance(state,str) or not state:
            print(f'Finance disconnect reconciliation skipped; operation={match[1]} '
                  f'workspace_ref={workspace_ref} reason=missing_operation_state',file=sys.stderr)
            return
        try:
            with self.bank.transaction() as db:
                self.bank._audit(db,workspace,'operation_response_disconnected',{'state':state},match[1])
        except Exception as error:
            # The client is already gone; retain a server-side signal if reconciliation audit fails.
            print(f'Finance audit failed after response disconnect; operation={match[1]} '
                  f'workspace_ref={workspace_ref} '
                  f'error={type(error).__name__}',file=sys.stderr)

    @property
    def security_events(self):
        """可信HTTP/Agent宿主接口，不注册为模型工具；计数复用原permission_security。"""
        return self.bank.permissions.events

    def dispatch(self,handler):
        if not urlsplit(handler.path).path.startswith('/api/finance'):return False
        return self.security_events.audit_http(handler,self._dispatch_finance)

    def _dispatch_finance(self,handler):
        parsed = urlsplit(handler.path)
        if not parsed.path.startswith('/api/finance'): return False
        if parsed.path.startswith('/api/finance/research/'):
            return self.research_workspace.dispatch(handler)
        cookie = None
        request_guard = None
        try:
            self.origin_check(handler)
            path,query = parsed.path,parse_qs(parsed.query,keep_blank_values=(
                parsed.path=='/api/finance/messages' or parsed.path.startswith('/api/finance/messages/') or
                parsed.path.startswith('/api/finance/data/') or
                parsed.path=='/api/finance/account-connections' or parsed.path.startswith('/api/finance/account-connections/') or
                parsed.path=='/api/finance/card-bindings' or
                parsed.path in ('/api/finance/public-product-performance','/api/finance/public-money-products','/api/finance/public-research-products','/api/finance/connected-assets/transactions',
                                '/api/finance/demo/accounts','/api/finance/dialogue-archive','/api/finance/simulation/snapshots')))
            if any(len(v)!=1 for v in query.values()): raise BankError('INVALID_QUERY','查询参数重复。')
            if path=='/api/finance/demo/accounts' and handler.command=='GET' and not self.registered_mode:
                if query:raise BankError('INVALID_QUERY','账户目录不接受身份参数。')
                result={**self.demo_accounts.catalog(),'schema_version':1,'data_scope':'navren_synthetic'}
                try:
                    owner,session_token=self.identity(handler)
                    identity=self.session_data(owner,session_token)
                    result.update(workspace_binding=owner,current_account_id=identity['account_id'],privacy_revision=identity['privacy_revision'])
                    handler._navren_workspace=owner
                except BankError as error:
                    if error.code!='UNAUTHENTICATED':raise
                handler._finance_json(200,result);return True
            if handler.command=='POST' and path=='/api/finance/session':
                strict_fields(request_json(handler),[])
                try: workspace,token = self.identity(handler)
                except BankError as error:
                    if error.code!='UNAUTHENTICATED': raise
                    workspace,token = self.create_session()
                    cookie = f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000'
                handler._finance_json(200,self.session_data(workspace,token),cookie)
                return True
            if path=='/api/finance/security/password-session' and handler.command=='POST':
                if self.registered_mode:
                    raise BankError('REGISTERED_LOGIN_REQUIRED','注册体验请通过原手机号账号重新登录，不能另发匿名银行会话。',401)
                from simulated_bank.security_credentials import restore
                result=restore(self.bank,request_json(handler))
                token=result.pop('session_token')
                result.update(csrf=self.csrf(token))
                handler._finance_json(200,result,f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000')
                return True
            workspace,token = self.identity(handler)
            handler._navren_workspace=workspace
            request_guard=self.demo_sessions.request(token,workspace)
            request_guard.__enter__()
            if path.startswith('/api/finance/security/recovery'):
                if parsed.query:
                    raise BankError('INVALID_QUERY','金融锁恢复不接受查询参数。')
                if handler.command=='GET' and path=='/api/finance/security/recovery':
                    result=self.security_events.status(workspace)
                elif handler.command=='POST':
                    self.check_csrf(handler,token)
                    result=self.security_events.recovery.dispatch(workspace,path,request_json(handler))
                else:
                    raise BankError('METHOD_NOT_ALLOWED','金融锁恢复接口方法无效。',405)
                handler._finance_json(200,result)
                return True
            if path=='/api/finance/account-state':
                if parsed.query:raise BankError('INVALID_QUERY','账号状态接口不接受查询参数。',400)
                if getattr(handler,'finance_send_guard_contract',None)!='navren.finance-send-guard/1':
                    raise BankError('ACCOUNT_STATE_SEND_GUARD_REQUIRED','账号状态缺少实际发送守卫。',503)
                if handler.command not in ('GET','POST'):
                    raise BankError('METHOD_NOT_ALLOWED','账号状态仅支持读取和保存。',405)
                if handler.command=='POST':
                    self.check_csrf(handler,token)
                    payload=request_json(handler)
                    with self.bank.transaction() as db:
                        from user_memory import privacy_revision
                        revision=privacy_revision(db,workspace)
                        if (handler.headers.get('X-Navren-Workspace')!=workspace or
                                handler.headers.get('X-Navren-Privacy-Revision')!=str(revision)):
                            raise BankError('PRIVACY_REVISION_CHANGED','保存请求的工作区或资料权限已变化。',409)
                        result=self.account_state.save(workspace,payload,bank_db=db)
                        handler._navren_start_revision=revision
                else:
                    with self.bank.transaction() as db:
                        from user_memory import privacy_revision
                        handler._navren_start_revision=privacy_revision(db,workspace)
                        result=self.account_state.read(workspace,bank_db=db)
                handler._navren_finance_send_guard=lambda data:self.account_state.send_guard(workspace,result['revision'])
                handler._finance_json(200,result)
                return True
            if path.startswith('/api/finance/messages/') and parsed.query:
                raise BankError('INVALID_QUERY','消息详情与写入接口不接受查询参数。')
            if path=='/api/finance/messages' and handler.command!='GET' and parsed.query:
                raise BankError('INVALID_QUERY','消息写入接口不接受查询参数。')
            if (path=='/api/finance/remittance-contacts' or path.startswith('/api/finance/remittance-contacts/')) and parsed.query:
                raise BankError('INVALID_QUERY','收款联系人接口不接受查询参数。')
            if path=='/api/finance/personal-profile' and parsed.query:
                raise BankError('INVALID_QUERY','账户资料接口不接受查询参数。')
            if path=='/api/finance/session/logout':
                if handler.command!='POST':
                    raise BankError('METHOD_NOT_ALLOWED','退出接口仅接受POST。',405)
                if parsed.query:
                    raise BankError('INVALID_QUERY','退出接口不接受查询参数。',400)
                self.check_csrf(handler,token)
                strict_fields(request_json(handler),[])
                # 银行写锁内重新校验精确请求令牌；不重置工作区，也不撤销其他会话。
                with self.bank.transaction() as db:
                    hashed=digest(token)
                    current=db.execute('SELECT token_hash FROM demo_sessions WHERE token_hash=?',(hashed,)).fetchone()
                    if current:
                        db.execute('UPDATE demo_sessions SET revoked=1 WHERE token_hash=?',(hashed,))
                    else:
                        db.execute('INSERT INTO demo_sessions VALUES(?,?,?,?,?,1)',
                            (hashed,workspace,'legacy',0,self.bank.clock()))
                    self.bank._audit(db,workspace,'account_logout',{})
                # 此回执仅证明认证终止；旧令牌已撤销，不能再作为私有资料发布绑定。
                handler._navren_workspace=None
                handler._navren_session_token=None
                handler._finance_json(200,{'logged_out':True,'old_requests_revoked':True,
                    'public_session_logout_required':True},
                    f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                return True
            if handler.command=='GET' or path.startswith('/api/finance/messages') or path=='/api/finance/personal-profile':
                from user_memory import privacy_revision
                with closing(self.bank.connect()) as db:handler._navren_start_revision=privacy_revision(db,workspace)
            if handler.command=='GET':
                allowed = {'from','to','view','source','include_life_events','simulation_overlay','kinds','period','portfolio','accounting_basis'} if path=='/api/finance/calendar' else {'offset','limit'} if re.fullmatch(r'/api/finance/calendar/views/[^/]+/days/[^/]+',path) else {'month'} if path in ('/api/finance/analysis','/api/finance/transactions') else {'year'} if path=='/api/finance/annual-analysis' else {'days'} if path=='/api/finance/cashflow-timeline' else set()
                if path=='/api/finance/personal-graph':allowed={'view','source','from','to','question','node_id','insight_id','edge_id','portfolio','accounting_basis','expected_version'}
                elif path=='/api/finance/wealth/product':allowed={'fund_id','on','portfolio'}
                elif path=='/api/finance/graph/evidence':allowed={'source','table','id'}
                elif path=='/api/finance/reports/snapshot':allowed={'source','source_version','month','year'}
                elif path=='/api/finance/public-products':allowed={'q','offset','limit'}
                elif path=='/api/finance/public-products/sync-status':allowed=set()
                elif path=='/api/finance/public-product-performance':allowed={'code','catalog_version'}
                elif path=='/api/finance/public-research-products':allowed={'code'}
                elif path=='/api/finance/budget':allowed={'account_id'}
                elif path=='/api/finance/metrics':allowed={'days'}
                elif path=='/api/finance/aa/consumptions':allowed={'q','from','to','limit','offset'}
                elif path=='/api/finance/account-connections':allowed={'kind','institution'}
                elif path=='/api/finance/messages':allowed={'view','category','unread','offset','limit'}
                elif path=='/api/finance/demo/history':allowed={'table','period','limit','offset'}
                elif path=='/api/finance/dialogue-archive':allowed={'cursor','limit'}
                elif path=='/api/finance/simulation/snapshots':allowed={'from','to'}
                elif path=='/api/finance/connected-assets/transactions':allowed={'month','account_id','limit','offset','kind'}
                elif path.startswith('/api/finance/data/'):
                    from financial_data_service.service import OPERATIONS
                    operation = path[len('/api/finance/data/'):]
                    if operation == 'presentation':
                        from financial_data_service.presentation import PRESENTATION_ARGUMENTS
                        allowed = PRESENTATION_ARGUMENTS
                    elif operation not in OPERATIONS:
                        raise BankError('OPERATION_NOT_ALLOWED','独立财务读取操作未开放。',400)
                    else:
                        allowed = OPERATIONS[operation]
                if set(query)-allowed: raise BankError('INVALID_QUERY','查询参数无效。')
                if path=='/api/finance/dialogue-archive':
                    if any(not values[0] for values in query.values()):raise BankError('INVALID_QUERY','历史分页参数不能为空。',400)
                    if getattr(handler,'finance_send_guard_contract',None)!='navren.finance-send-guard/1':
                        raise BankError('FINANCE_SEND_GUARD_REQUIRED','当前入口尚未接入受控历史发送。',503)
                    reader=getattr(self,'dialogue_archive',None)
                    if reader is None:raise BankError('DIALOGUE_ARCHIVE_UNAVAILABLE','完整聊天归档尚未装配。',503)
                    raw_limit=query.get('limit',['20'])[0]
                    if not re.fullmatch(r'[0-9]{1,2}',raw_limit) or not 1<=int(raw_limit)<=50:
                        raise BankError('INVALID_QUERY','历史分页数量须为1至50。',400)
                    page,capture=reader.read_page(workspace,cursor=query.get('cursor',[None])[0],limit=int(raw_limit))
                    handler._navren_finance_send_guard=lambda body:reader.send_guard(workspace,capture,body)
                    handler._finance_json(200,page);return True
                if path=='/api/finance/simulation/snapshots':
                    if any(not values[0] for values in query.values()):raise BankError('INVALID_QUERY','快照日期不能为空。',400)
                    if not self.registered_mode:raise BankError('REGISTERED_LOGIN_REQUIRED','仅用于本人注册实例。',401)
                    if getattr(handler,'finance_send_guard_contract',None)!='navren.finance-send-guard/1':
                        raise BankError('FINANCE_SEND_GUARD_REQUIRED','当前入口尚未接入受控快照发送。',503)
                    from simulated_bank.living_simulation import LivingError
                    try:
                        result=self.living_simulation.read_snapshots(workspace,start_on=query.get('from',[None])[0],end_on=query.get('to',[None])[0])
                    except LivingError:
                        raise BankError('LIVING_SNAPSHOT_UNAVAILABLE','运行快照待重新核验，未返回旧版本。',409) from None
                    version=result['ledger_version']
                    handler._navren_finance_send_guard=lambda body:self.living_simulation.snapshot_send_guard(workspace,version)
                    handler._finance_json(200,result);return True
                if path=='/api/finance/demo/accounts':
                    identity=self.session_data(workspace,token)
                    result={**self.demo_accounts.catalog(),'schema_version':1,'data_scope':'navren_synthetic',
                            'workspace_binding':workspace,'current_account_id':identity['account_id'],
                            'privacy_revision':identity['privacy_revision']}
                    handler._finance_json(200,result);return True
                if path.startswith('/api/finance/data/'):
                    if any(len(v)!=1 for v in query.values()):
                        raise BankError('INVALID_QUERY','独立财务读取参数不得重复。',400)
                    arguments = {k:v[0] for k,v in query.items()}
                    for key in ('limit','offset'):
                        if key in arguments:
                            if not re.fullmatch(r'\d{1,7}',arguments[key]):
                                raise BankError('INVALID_PAGINATION','分页参数必须为有界整数。',400)
                            arguments[key]=int(arguments[key])
                    handler._finance_json(200,self.financial_query(workspace,operation,arguments))
                    return True
                if path=='/api/finance/wealth/versions':
                    handler._finance_json(200,self.wealth.versions(workspace));return True
                if path=='/api/finance/wealth/product':
                    p={k:v[0] for k,v in query.items()}
                    handler._finance_json(200,self.wealth.detail(workspace,p.get('fund_id',''),p.get('on',self.calendar.clock(workspace)[0]),p.get('portfolio','historical')));return True
                if path.startswith('/api/finance/calendar'):
                    p={k:v[0] for k,v in query.items()}
                    for key in ('include_life_events','simulation_overlay'):
                        if key in p:
                            if p[key] not in ('true','false'):raise BankError('INVALID_QUERY','覆盖层开关无效。')
                            p[key]=p[key]=='true'
                    if 'kinds' in p:p['kinds']=p['kinds'].split(',') if p['kinds'] else []
                    detail=re.fullmatch(r'/api/finance/calendar/views/([a-f0-9]{32})/days/(\d{4}-\d{2}-\d{2})',path)
                    item=re.fullmatch(r'/api/finance/calendar/items/(.+)',path)
                    event_draft=re.fullmatch(r'/api/finance/calendar/event-drafts/([a-f0-9]{32})',path)
                    if path=='/api/finance/calendar':result=self.calendar.query(workspace,p)
                    elif detail:
                        for key in ('offset','limit'):
                            if key in p and not re.fullmatch(r'\d{1,8}',p[key]):raise BankError('CALENDAR_PAGE','分页无效。')
                        result=self.calendar.detail(workspace,detail[1],detail[2],int(p.get('offset',0)),int(p.get('limit',100)))
                    elif item:
                        from urllib.parse import unquote
                        result=self.calendar.item(workspace,unquote(item[1]))
                    elif event_draft:
                        with closing(self.bank.connect()) as db:
                            row=db.execute('SELECT status,result,expires_at FROM calendar_event_drafts WHERE workspace_id=? AND id=?',(workspace,event_draft[1])).fetchone()
                        if not row:raise BankError('CALENDAR_NOT_FOUND','草案不属于当前账户。',404)
                        result={'status':'expired' if row['status']!='committed' and self.bank.clock()>=row['expires_at'] else row['status'],
                                'result':json.loads(row['result']) if row['result'] else None}
                    elif path=='/api/finance/calendar/reminders':result=self.calendar.events.reminders(workspace,self.calendar.clock(workspace)[0])
                    elif path=='/api/finance/calendar/history':
                        result={'workspace_binding':workspace,'entries':self.personal_data.read_widgets(workspace,16)}
                    else:raise BankError('NOT_FOUND','日历接口不存在。',404)
                    handler._finance_json(200,result);return True
                if path=='/api/finance/cashflow-timeline':
                    raw_days=query.get('days',['90'])[0]
                    if not re.fullmatch(r'[1-9]\d{0,2}',raw_days):
                        raise BankError('INVALID_HORIZON','时间范围必须为1至365天的整数。')
                    result=self.cashflow_timeline(workspace,int(raw_days))
                    handler._finance_json(200,result)
                    return True
                routes = {
                    '/api/finance/session':lambda:self.session_data(workspace,token),
                    '/api/finance/connected-assets':lambda:self.connected_assets.overview(workspace),
                    '/api/finance/account-connections':lambda:self.account_connections.read(workspace,query),
                    '/api/finance/connected-assets/transactions':lambda:self.connected_assets.transactions(
                        workspace,month=query.get('month',[datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')])[0],
                        account_id=query.get('account_id',[None])[0],
                        kind=query.get('kind',[None])[0],
                        limit=self.page_number(query,'limit',8,1,100),offset=self.page_number(query,'offset',0,0,10000)),
                    '/api/finance/demo/current':lambda:self.demo_accounts.current(workspace),
                    '/api/finance/demo/history':lambda:self.demo_accounts.history(workspace,query.get('table',['cash_transactions'])[0],
                        period=query.get('period',[None])[0],limit=self.page_number(query,'limit',100,1,500),
                        offset=self.page_number(query,'offset',0,0,1000000)),
                    '/api/finance/metrics':lambda:self.financial_metrics.snapshot(workspace,self.page_number(query,'days',60,1,365)),
                    '/api/finance/overview':lambda:self.overview(workspace),
                    '/api/finance/capabilities':lambda:self.capabilities(),
                    '/api/finance/knowledge/status':lambda:self.knowledge_provider.status(),
                    '/api/finance/data-contract':lambda:self.bank.data_contract(),
                    '/api/finance/source':lambda:self.source(workspace),
                    '/api/finance/reports/snapshot':lambda:self.reports_snapshot(workspace,{k:v[0] for k,v in query.items()}),
                    '/api/finance/budget':lambda:self.budget(workspace,query.get('account_id',[None])[0]),
                    '/api/finance/goals':lambda:self.goals.state(workspace),
                    '/api/finance/research-plans':lambda:self.research_plans.read(workspace),
                    '/api/finance/products':lambda:self.bank.products(workspace),
                    '/api/finance/analysis':lambda:self.analysis(workspace,query.get('month',['2026-09'])[0]),
                    '/api/finance/transactions':lambda:self.transactions(workspace,query.get('month',[None])[0]),
                    '/api/finance/anomalies':lambda:self.anomalies(workspace),
                    '/api/finance/periodic-charges':lambda:self.periodic_charges(workspace),
                    '/api/finance/audit':lambda:self.bank.audit(workspace),
                    '/api/finance/memories':lambda:self.memories.state(workspace),
                    '/api/finance/personal-profile':lambda:self.personal_profile.read(workspace,identity=self.demo_sessions.describe(token)),
                    '/api/finance/personal-graph':lambda:self.financial_graph.query(workspace,{k:v[0] for k,v in query.items()}),
                    '/api/finance/graph/evidence':lambda:self.financial_graph.evidence(workspace,{k:v[0] for k,v in query.items()}),
                    '/api/finance/public-products':lambda:self.public_products.read(query.get('q',[''])[0],self.page_number(query,'limit',24,1,500),self.page_number(query,'offset',0,0,100000)),
                    '/api/finance/public-products/sync-status':self.public_products.sync_status,
                    '/api/finance/remittances':lambda:self.bank.remittances(workspace),
                    '/api/finance/payment-contacts':lambda:self.payment_contacts(workspace),
                    '/api/finance/remittance-contacts':lambda:self.remittance_contacts(workspace),
                    '/api/finance/aa/bills':lambda:self.aa_bills(workspace),
                    '/api/finance/aa/consumptions':lambda:self.aa_consumptions(workspace,query),
                    '/api/finance/public-product-performance':lambda:self.public_product_performance(query),
                    '/api/finance/public-money-products':lambda:self.public_money_products_read(),
                    '/api/finance/public-research-products':lambda:self.public_research_products_read(query),
                    '/api/finance/financial-records':lambda:self.financial_records(workspace),
                    '/api/finance/service-negotiations':lambda:self.service_negotiations.applications(workspace),
                    '/api/finance/demo-code':lambda:{'code':self.demo_code(token),'notice':'仅用于模拟额外验证；不要发送到对话。'},
                }
                match = re.fullmatch(r'/api/finance/operations/([a-f0-9]{32})',path)
                if path in routes: result = routes[path]()
                elif path=='/api/finance/messages':result=self.messages.read(workspace,query,expected_privacy=handler._navren_start_revision)
                elif path=='/api/finance/messages/summary':result=self.messages.read(workspace,summary=True,expected_privacy=handler._navren_start_revision)
                elif re.fullmatch(r'/api/finance/messages/[a-f0-9]{32}',path):
                    result=self.messages.read(workspace,message_id=path.rsplit('/',1)[1],expected_privacy=handler._navren_start_revision)
                elif re.fullmatch(r'/api/finance/aa/bills/[a-f0-9]{32}',path):
                    result=self.aa_bills(workspace,path.split('/')[-1])
                elif re.fullmatch(r'/api/finance/aa/attachments/[a-f0-9]{32}',path):
                    if query:raise BankError('INVALID_QUERY','图片凭证不接受额外查询参数。',400)
                    result=self.aa_attachment(workspace,path.split('/')[-1])
                elif re.fullmatch(r'/api/finance/remittances/[a-f0-9]{32}',path):
                    result=self.bank.remittances(workspace,path.rsplit('/',1)[1])
                elif path.startswith('/api/finance/goals/drafts/'):
                    result=self.goals.draft(workspace,path.rsplit('/',1)[1])
                elif path.startswith('/api/finance/goals/'):
                    result=self.goals.state(workspace,path.rsplit('/',1)[1])
                elif path=='/api/finance/annual-analysis':
                    year = query.get('year',['2025'])[0]
                    if not re.fullmatch(r'\d{4}',year): raise BankError('INVALID_YEAR','年度格式无效。')
                    result = self.annual_analysis(workspace,int(year))
                elif match: result = self.operation_view(workspace,match[1])
                else: raise BankError('NOT_FOUND','银行接口不存在。',404)
            elif handler.command=='POST':
                self.check_csrf(handler,token)
                match = re.fullmatch(r'/api/finance/operations/([a-f0-9]{32})/(confirm|cancel|sms)',path)
                if path=='/api/finance/calendar/import/preview':
                    result=self.calendar.events.preview(workspace,request_text(handler))
                elif path=='/api/finance/statements/preview':
                    result = self.statements.preview(workspace,request_text(handler))
                else:
                    payload = request_json(handler)
                    if path.startswith('/api/finance/account-connections') and parsed.query:
                        raise BankError('INVALID_QUERY','平台关联写入不接受查询参数。',400)
                    if path=='/api/finance/card-bindings' and parsed.query:
                        raise BankError('INVALID_QUERY','银行卡登记接口不接受查询参数。',400)
                    if path.startswith(('/api/finance/memories','/api/finance/memory-relations/','/api/finance/profile-observations/')):
                        if type(payload.get('expected_revision')) is not int:
                            raise BankError('PERSONAL_VERSION_REQUIRED','请先读取当前个人资料，再提交版本绑定的修改。',409)
                    memory_match = re.fullmatch(r'/api/finance/memories/([a-f0-9]{32})/(correct|delete)',path)
                    memory_relation_match = re.fullmatch(r'/api/finance/memories/([a-f0-9]{32})/relations',path)
                    memory_relation_delete = re.fullmatch(r'/api/finance/memory-relations/([a-f0-9]{32})/delete',path)
                    service_accept = re.fullmatch(r'/api/finance/service-negotiations/([a-f0-9]{32})/accept',path)
                    observation_match = re.fullmatch(r'/api/finance/profile-observations/([a-f0-9]{32})',path)
                    calendar_confirm=re.fullmatch(r'/api/finance/calendar/events/([a-f0-9]{32})/confirm',path)
                    calendar_read=re.fullmatch(r'/api/finance/calendar/reminders/([a-f0-9]{64})/read',path)
                    if path=='/api/finance/demo/select':
                        strict_fields(payload,['user_id','expected_workspace_id','expected_privacy_revision'],['user_id','expected_workspace_id'])
                        if 'expected_privacy_revision' in payload and type(payload['expected_privacy_revision']) is not int:
                            raise BankError('PRIVACY_REVISION_CHANGED','账户权限版本无效。',409)
                        workspace,token=self.demo_sessions.select(token,payload['user_id'],payload['expected_workspace_id'],
                            payload.get('expected_privacy_revision'))
                        handler._navren_workspace=workspace;handler._navren_session_token=token
                        from user_memory import privacy_revision
                        with closing(self.bank.connect()) as db:handler._navren_start_revision=privacy_revision(db,workspace)
                        cookie=f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000'
                        result=self.session_data(workspace,token)
                    elif path=='/api/finance/research-plans':result=self.research_plans.create(workspace,payload)
                    elif re.fullmatch(r'/api/finance/research-plans/[a-f0-9]{32}/execution',path):result=self.research_plans.associate(workspace,path.split('/')[-2],payload)
                    elif path=='/api/finance/page-contexts':result=self.page_contexts.create(workspace,token,payload)
                    elif re.fullmatch(r'/api/finance/page-contexts/[a-f0-9]{32}/claim',path):
                        strict_fields(payload,[]);result=self.page_contexts.claim(workspace,token,path.split('/')[-2])
                    elif path=='/api/finance/session/reset':
                        strict_fields(payload,['confirmed'],['confirmed'])
                        if payload['confirmed'] is not True:raise BankError('CONFIRMATION_REQUIRED','创建独立演示账户需要明确确认。')
                        identity=self.demo_sessions.describe(token)
                        if identity.get('mode')=='demo_account':
                            workspace,token=self.demo_sessions.select(token,identity['user_id'],workspace)
                        else:
                            old_token=token
                            workspace,token=self.create_session()
                            with self.bank.transaction() as db:
                                db.execute('INSERT OR IGNORE INTO demo_sessions VALUES(?,?,?,?,?,1)',
                                    (digest(old_token),handler._navren_workspace,'legacy',0,self.bank.clock()))
                        handler._navren_workspace=workspace;handler._navren_session_token=token
                        cookie=f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000'
                        result=self.session_data(workspace,token)
                    elif path=='/api/finance/goals/prepare':result=self.goals.prepare(workspace,payload)
                    elif re.fullmatch(r'/api/finance/goals/drafts/[a-f0-9]{32}/confirm',path):
                        result=self.goals.confirm(workspace,path.split('/')[-2],payload)
                    elif re.fullmatch(r'/api/finance/goals/drafts/[a-f0-9]{32}/cancel',path):
                        result=self.goals.cancel(workspace,path.split('/')[-2],payload)
                    elif path=='/api/finance/graph/allocations':result=self.financial_graph.allocate(workspace,payload)
                    elif path=='/api/finance/graph/allocations/delete':result=self.financial_graph.delete_allocation(workspace,payload)
                    elif path=='/api/finance/graph/scenarios':result=self.financial_graph.scenario(workspace,payload)
                    elif path=='/api/finance/graph/decisions':result=self.financial_graph.decision(workspace,payload)
                    elif path=='/api/finance/calendar/events/prepare':result=self.calendar.events.prepare(workspace,payload)
                    elif calendar_confirm:result=self.calendar.events.confirm(workspace,calendar_confirm[1],payload)
                    elif path=='/api/finance/calendar/import/commit':result=self.calendar.events.commit(workspace,payload)
                    elif calendar_read:
                        strict_fields(payload,[]);result=self.calendar.events.read_reminder(workspace,calendar_read[1])
                    elif path=='/api/finance/statements/commit': result = self.statements.commit(workspace,payload)
                    elif observation_match:
                        result=self._committed_personal_result(handler,workspace,
                            self.personal_graph.update,observation_match[1],payload)
                    elif path=='/api/finance/statements/cancel': result = self.statements.cancel(workspace,payload)
                    elif path=='/api/finance/service-negotiations/preview': result = self.service_negotiations.preview(workspace,payload)
                    elif service_accept: result = self.service_negotiations.accept(workspace,service_accept[1],payload)
                    elif path=='/api/finance/source': result = self.set_source(workspace,payload)
                    elif path=='/api/finance/memories':
                        result=self._committed_personal_result(handler,workspace,self.memories.create,payload)
                    elif path=='/api/finance/personal-profile':result=self.personal_profile.save(workspace,payload,
                        identity=self.demo_sessions.describe(token),expected_privacy=handler._navren_start_revision)
                    elif path=='/api/finance/memories/settings':
                        result=self._committed_personal_result(handler,workspace,self.memories.set_enabled,payload)
                    elif memory_match and memory_match[2]=='correct':
                        result=self._committed_personal_result(handler,workspace,
                            self.memories.correct,memory_match[1],payload)
                    elif memory_match and memory_match[2]=='delete':
                        result=self._committed_personal_result(handler,workspace,
                            self.memories.delete,memory_match[1],payload)
                    elif memory_relation_match:
                        result=self._committed_personal_result(handler,workspace,
                            self.memories.add_relation,memory_relation_match[1],payload)
                    elif memory_relation_delete:
                        strict_fields(payload,['expected_revision'],['expected_revision'])
                        result=self._committed_personal_result(handler,workspace,
                            self.memories.delete_relation,memory_relation_delete[1],payload['expected_revision'])
                    elif path=='/api/finance/simulation/advance':
                        if self.registered_mode:
                            raise BankError('LIVING_CLOCK_MANAGED','注册实例随服务器时间持续模拟，不能手动生成未来交易。',409)
                        strict_fields(payload,['days','confirmed'],['days','confirmed'])
                        if payload['confirmed'] is not True:
                            raise BankError('CONFIRMATION_REQUIRED','推进模拟日期需要明确确认。')
                        result=self.bank.advance_days(workspace,payload['days'])
                    elif path=='/api/finance/operations/prepare': result = self.prepare(workspace,payload)
                    elif path=='/api/finance/security/password-drafts':
                        from simulated_bank.security_credentials import prepare as prepare_password
                        result=prepare_password(self.bank,workspace,payload)
                    elif path=='/api/finance/account-connections':result=self.account_connections.change(workspace,payload)
                    elif path=='/api/finance/card-bindings':result=self.card_bindings.register(workspace,payload)
                    elif path=='/api/finance/messages/read-all':result=self.messages.mark_read(workspace,payload,expected_privacy=handler._navren_start_revision)
                    elif re.fullmatch(r'/api/finance/messages/[a-f0-9]{32}/read',path):
                        result=self.messages.mark_read(workspace,payload,message_id=path.split('/')[-2],expected_privacy=handler._navren_start_revision)
                    elif re.fullmatch(r'/api/finance/account-connections/[a-f0-9]{32}/disconnect',path):
                        result=self.account_connections.change(workspace,payload,path.split('/')[-2])
                    elif path=='/api/finance/remittances/prepare':result=self.prepare_remittance(workspace,payload)
                    elif path=='/api/finance/aa/bills/prepare':result=self.prepare_aa(workspace,payload)
                    elif path=='/api/finance/aa/attachments':result=self.aa_attachment(workspace,payload=payload)
                    elif re.fullmatch(r'/api/finance/aa/attachments/[a-f0-9]{32}/delete',path):
                        result=self.aa_attachment(workspace,path.split('/')[-2],payload,delete=True)
                    elif re.fullmatch(r'/api/finance/aa/shares/[a-f0-9]{32}/(pay|decline)/prepare',path):
                        result=self.prepare_aa(workspace,payload,kind=path.split('/')[-2],object_id=path.split('/')[-3])
                    elif re.fullmatch(r'/api/finance/aa/bills/[a-f0-9]{32}/cancel/prepare',path):
                        result=self.prepare_aa(workspace,payload,kind='cancel',object_id=path.split('/')[-3])
                    elif path=='/api/finance/payment-contacts':result=self.payment_contacts(workspace,payload,action='create')
                    elif path=='/api/finance/remittance-contacts':result=self.remittance_contacts(workspace,payload,action='create')
                    elif re.fullmatch(r'/api/finance/remittance-contacts/[a-f0-9]{32}/(update|delete)',path):
                        result=self.remittance_contacts(workspace,payload,path.split('/')[-2],path.split('/')[-1])
                    elif re.fullmatch(r'/api/finance/payment-contacts/[a-f0-9]{32}/(update|delete)',path):
                        result=self.payment_contacts(workspace,payload,path.split('/')[-2],path.split('/')[-1])
                    elif path=='/api/finance/profile': result = self.bank.profile(workspace,payload)
                    elif re.fullmatch(r'/api/finance/periodic-charges/[a-f0-9]{64}/mark',path):
                        candidate_match = re.fullmatch(r'/api/finance/periodic-charges/([a-f0-9]{64})/mark',path)
                        strict_fields(payload,['decision','source'],['decision'])
                        source=payload.get('source',self.source(workspace)['active_source'])
                        if source not in ('simulation','imported'):raise BankError('INVALID_SOURCE','扣费候选来源无效。')
                        service=self.bank if source=='simulation' else self.statements
                        result=service.mark_periodic_candidate(workspace,{'candidate_id':candidate_match[1],'decision':payload['decision']})
                    elif match and match[2]=='confirm': result = self.confirm(workspace,match[1],payload)
                    elif match and match[2]=='sms':
                        strict_fields(payload,[])
                        result = self.bank.start_sms_verification(workspace,match[1])
                    elif match:
                        strict_fields(payload,[])
                        result = self.bank.cancel(workspace,match[1])
                    else: raise BankError('NOT_FOUND','银行接口不存在。',404)
            else: raise BankError('METHOD_NOT_ALLOWED','此接口不支持该方法。',405)
            try:
                if self.registered_mode and path=='/api/finance/personal-profile' and isinstance(result,dict) and 'account_revision' in result:
                    handler._navren_finance_send_guard=lambda data:self.account_state.send_guard(workspace,result['account_revision'])
                if (isinstance(result,dict) and result.get('sessions_revoked') is True
                        and result.get('operation',{}).get('action')=='password_change'
                        and result['operation'].get('state')=='succeeded'):
                    handler._navren_workspace=None
                    handler._navren_session_token=None
                    cookie=f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
                handler._finance_json(200,result,cookie)
            except ConnectionError:
                # Only classify failures from writing a completed HTTP response as client disconnects.
                self._audit_disconnected_response(path,result,workspace)
        except BankError as error:
            try: handler._finance_json(error.status,{'error':error.as_dict()})
            except ConnectionError: pass
        except Exception:
            try: handler._finance_json(500,{'error':{'code':'BANK_UNAVAILABLE','message':'银行服务暂不可用；请查询订单核实状态，勿重复提交。'}})
            except ConnectionError: pass
        finally:
            if request_guard:request_guard.__exit__(None,None,None)
        return True

    def public_money_products_read(self):
        if self.public_money_products is None:
            with self._product_performance_lock:
                if self.public_money_products is None:
                    from product_performance import PublicMoneyProducts
                    self.public_money_products = PublicMoneyProducts()
        return self.public_money_products.read()

    def public_research_products_read(self, query):
        if (set(query) - {'code'} or any(len(v) != 1 for v in query.values())
                or ('code' in query and not re.fullmatch(r'[0-9]{6}', query['code'][0]))):
            raise BankError('INVALID_QUERY', '产品研究查询参数无效。')
        if self.public_product_research is None:
            with self._product_performance_lock:
                if self.public_product_research is None:
                    from product_research import PublicProductResearch
                    self.public_product_research = PublicProductResearch(self.public_products)
        code = query.get('code', [None])[0]
        result = self.public_product_research.read(code)
        if code is not None and result.get('ranking_complete') and not result['products']:
            raise BankError('OBJECT_NOT_FOUND', '公开目录中未找到该份额。', 404)
        return result

    def public_product_performance(self, query):
        """业绩资料单独读取；销售目录身份与行情来源不得互相替代。"""
        if set(query)-{'code','catalog_version'} or any(len(v)!=1 for v in query.values()):
            raise BankError('INVALID_QUERY','产品业绩查询参数无效。')
        code = query.get('code',[''])[0]
        expected = query.get('catalog_version',[None])[0]
        if not isinstance(code,str) or not re.fullmatch(r'[0-9]{6}',code):
            raise BankError('INVALID_PRODUCT_CODE','基金份额代码须为六位数字。')
        if expected is not None and not re.fullmatch(r'[a-f0-9]{64}',expected):
            raise BankError('INVALID_PRODUCT_VERSION','产品目录版本无效。')
        catalog = self.public_products.read(query=code,limit=500)
        version = catalog.get('catalog_version')
        if not version or catalog.get('status')=='unavailable':
            raise BankError('PRODUCT_CATALOG_UNAVAILABLE','产品目录暂时无法读取，请稍后重试。',503)
        if expected is not None and expected != version:
            raise BankError('OBJECT_VERSION_CHANGED','产品目录已更新，请重新读取产品。',409)
        product = next((p for p in catalog.get('products',[]) if p.get('code')==code),None)
        if product is None:
            raise BankError('OBJECT_NOT_FOUND','微众公开目录中未找到该产品。',404)
        if self.product_performance is None:
            with self._product_performance_lock:
                if self.product_performance is None:
                    from product_performance import ProductPerformance
                    self.product_performance = ProductPerformance()
        result = self.product_performance.read(product)
        return {**result,'product_code':code,'product_name':product['name'],
                'catalog_version':version,'catalog_source_date':catalog.get('source_date'),
                'catalog_source_url':product.get('source_url'),
                'catalog_fetch_status':catalog.get('fetch_status'),
                'catalog_stale':catalog.get('stale',True),'executable':False}

    @staticmethod
    def page_number(query,key,default,minimum,maximum):
        raw=query.get(key,[str(default)])[0]
        if not re.fullmatch(r'\d{1,6}',raw) or not minimum<=int(raw)<=maximum:
            raise BankError('INVALID_QUERY','分页参数无效。')
        return int(raw)
