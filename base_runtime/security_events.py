"""可信宿主的脱敏安全事件接口；不是模型工具或HTTP上报入口。"""
from contextlib import closing,contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib,json,re,uuid

CONTRACT='navren.security-event/1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS permission_security_event_counts(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),event_ref TEXT NOT NULL,
 PRIMARY KEY(workspace_id,event_ref));
CREATE TABLE IF NOT EXISTS permission_security_events(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 request_id TEXT,event_ref TEXT NOT NULL,phase TEXT NOT NULL,outcome TEXT NOT NULL,
 reason_code TEXT NOT NULL,details TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS security_events_request ON permission_security_events(workspace_id,request_id);
'''
# 只接原认证/验证守卫的明确失败；普通模型选错工具、参数/数据缺项及运行故障不在此表。
SUSPICIOUS=frozenset({'AUTHENTICATION_FORMAT_INVALID','CROSS_SUBJECT_ACCESS','FORGED_AUTHORIZATION',
 'VERIFICATION_TOKEN_TAMPERED','CONFIRMATION_MISMATCH','INVALID_CONFIRMATION','PERMISSION_BINDING_INVALID',
 'SMS_CODE_INVALID','SMS_CHALLENGE_INVALID','SMS_VERIFICATION_FAILED','SMS_PROOF_REQUIRED',
 'SMS_PHONE_BINDING_CHANGED','SMS_PROOF_REPLAY'})
PHASES=frozenset({'http','tool','native','prepare','verification','confirm','recovery','runtime'})
NORMAL_CODES=frozenset({'TOOL_ALLOWED','HTTP_ALLOWED','TOOL_NOT_ALLOWED','TOOL_ARGUMENTS_INVALID',
 'USER_AUTHORIZATION_DECLINED','DATA_UNAVAILABLE','SUPPLIER_UNAVAILABLE','AUTHORIZATION_DENIED',
 'RUNTIME_FAILURE','SECURITY_RECOVERY_NOT_CONFIGURED','PERMISSION_PREPARED'})
_scope=ContextVar('navren_security_event_scope',default=None)

def packed(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(v):return hashlib.sha256(packed(v).encode()).hexdigest()
def fail(code='SECURITY_EVENT_INVALID',status=422):
    from simulated_bank import BankError
    raise BankError(code,'安全事件输入或受信请求关联无法核对。',status)
def identifier(value,nullable=False):
    if nullable and value is None:return value
    if type(value) is not str or re.fullmatch('[a-f0-9]{32}',value) is None:fail()
    return value
def code(value):
    return value if type(value) is str and re.fullmatch('[A-Z][A-Z_]{1,63}',value) else 'AUTHORIZATION_DENIED'

@dataclass
class Scope:
    bank:object
    workspace:object
    request_id:object
    event_ref:str
    phase:str
    operation_id:object=None
    before_state:object=None

class SecurityEvents:
    def __init__(self,bank):self.bank=bank

    @property
    def recovery(self):
        from .security_recovery import FinancialLockRecovery
        return FinancialLockRecovery(self.bank)

    @contextmanager
    def request(self,workspace=None,*,request_id=None,event_id=None,phase='tool',operation_id=None):
        parent=_scope.get()
        if parent and parent.bank is self.bank and request_id is None:request_id=parent.request_id
        identifier(request_id,True)
        if event_id is not None and (type(event_id) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,160}',event_id)):fail()
        if phase not in PHASES:fail()
        if operation_id is not None:identifier(operation_id)
        # event_ref是事件幂等引用，绝不替代或伪造原request_id；缺原ID明确保留null。
        ref=parent.event_ref if parent and parent.bank is self.bank and event_id is None else digest([request_id,event_id if event_id is not None else uuid.uuid4().hex])
        item=Scope(self.bank,workspace,request_id,ref,phase,operation_id)
        if operation_id and workspace:
            with closing(self.bank.connect()) as db:
                row=db.execute('SELECT state FROM operations WHERE workspace_id=? AND id=?',(workspace,operation_id)).fetchone()
                item.before_state=row['state'] if row else None
        token=_scope.set(item)
        try:yield item
        finally:_scope.reset(token)

    def bind(self,workspace):
        item=_scope.get()
        if item and item.bank is self.bank:
            if item.workspace is not None and item.workspace!=workspace:fail('SECURITY_EVENT_WORKSPACE_MISMATCH',403)
            item.workspace=workspace

    def _context(self,workspace):
        item=_scope.get()
        if item and item.bank is self.bank:
            if item.workspace is not None and item.workspace!=workspace:fail('SECURITY_EVENT_WORKSPACE_MISMATCH',403)
            return item
        return Scope(self.bank,workspace,None,digest(uuid.uuid4().hex),'native')

    def in_db(self,db,workspace,operation,reason,*,outcome='denied',suspicious=False,phase=None,evidence_sha256=()):
        from simulated_bank.permission_policy import LOCK_THRESHOLD,POLICY_VERSION
        self.bank._workspace(db,workspace);item=self._context(workspace);phase=phase or item.phase
        if item.operation_id is not None and item.operation_id!=operation:fail('SECURITY_EVENT_OPERATION_MISMATCH',409)
        if phase not in PHASES or outcome not in ('allowed','denied','fault'):fail()
        reason=code(reason)
        if not isinstance(evidence_sha256,(tuple,list)) or len(evidence_sha256)>32 or any(type(x) is not str or not re.fullmatch('[a-f0-9]{64}',x) for x in evidence_sha256):fail()
        op=None
        operation_ref=digest(operation) if type(operation) is str and len(operation)<=512 else None
        if type(operation) is str and re.fullmatch('[a-f0-9]{32}',operation):
            op=db.execute('SELECT state,permission,confirmation_digest FROM operations WHERE workspace_id=? AND id=?',(workspace,operation)).fetchone()
        eid=digest([workspace,item.event_ref,phase,outcome,reason,operation_ref])
        old=db.execute('SELECT details FROM permission_security_events WHERE id=? AND workspace_id=?',(eid,workspace)).fetchone()
        if old:
            value=json.loads(old['details'])
            if (value['operation_id']!=(operation if op else None) or value['requested_object_ref']!=(operation_ref if operation and not op else None)
                    or value['evidence_sha256']!=list(evidence_sha256)):fail('SECURITY_EVENT_CONFLICT',409)
            return value
        counted=False
        if suspicious:
            inserted=db.execute('INSERT OR IGNORE INTO permission_security_event_counts VALUES(?,?)',(workspace,item.event_ref)).rowcount
            if inserted:
                db.execute('INSERT OR IGNORE INTO permission_security(workspace_id) VALUES(?)',(workspace,))
                db.execute('UPDATE permission_security SET failures=failures+1,locked=CASE WHEN failures+1>=? THEN 1 ELSE locked END WHERE workspace_id=?',(LOCK_THRESHOLD,workspace))
                counted=True
        authentication_ref=None
        if op:
            proof=db.execute('SELECT provider_reference_hash FROM permission_sms_proofs WHERE workspace_id=? AND operation_id=? ORDER BY issued_at DESC LIMIT 1',(workspace,operation)).fetchone()
            authentication_ref=proof[0] if proof else None
        security=db.execute('SELECT failures,locked FROM permission_security WHERE workspace_id=?',(workspace,)).fetchone()
        value={'contract':CONTRACT,'event_id':eid,'request_id':item.request_id,'workspace_id':workspace,
            'operation_id':operation if op else None,'requested_object_ref':operation_ref if operation and not op else None,
            'phase':phase,'outcome':outcome,'reason_code':reason,'policy':POLICY_VERSION,
            'risk':op['permission'] if op else None,'evidence_sha256':list(evidence_sha256),
            'confirmation_ref':op['confirmation_digest'] if op else None,'authentication_ref':authentication_ref,
            'before_state':item.before_state,'after_state':op['state'] if op else None,
            'suspicious':bool(suspicious),'security_failure_recorded':counted,
            'failures':security['failures'] if security else 0,'financial_write_locked':bool(security and security['locked'])}
        db.execute('INSERT INTO permission_security_events VALUES(?,?,?,?,?,?,?,?,?)',
            (eid,workspace,item.request_id,item.event_ref,phase,outcome,reason,packed(value),self.bank.clock()))
        self.bank._audit(db,workspace,'security_event',value,operation if op else None)
        return value

    def allowed(self,workspace,*,operation_id=None,evidence_sha256=()):
        with self.bank.transaction() as db:
            return self.in_db(db,workspace,operation_id,'TOOL_ALLOWED',outcome='allowed',evidence_sha256=evidence_sha256)

    def rejected(self,workspace,error,*,operation_id=None,evidence_sha256=()):
        from simulated_bank import BankError
        reason=code(error.code) if isinstance(error,BankError) else 'RUNTIME_FAILURE'
        with self.bank.transaction() as db:
            return self.in_db(db,workspace,operation_id,reason,outcome='denied' if isinstance(error,BankError) else 'fault',
                suspicious=isinstance(error,BankError) and reason in SUSPICIOUS,evidence_sha256=evidence_sha256)

    def observe(self,workspace,reason,*,operation_id=None,outcome='denied'):
        """非攻击事件只记录、不增加金融安全计数；仅接受固定原因码。"""
        if reason not in NORMAL_CODES:fail()
        with self.bank.transaction() as db:
            return self.in_db(db,workspace,operation_id,reason,outcome=outcome,suspicious=False)

    def status(self,workspace):
        from simulated_bank.permission_policy import LOCK_THRESHOLD
        with self.bank.transaction() as db:
            self.bank._workspace(db,workspace);self._context(workspace)
            row=db.execute('SELECT failures,locked FROM permission_security WHERE workspace_id=?',(workspace,)).fetchone()
            return {'contract':CONTRACT,'workspace_id':workspace,'failures':row['failures'] if row else 0,
                'financial_write_locked':bool(row and row['locked']),'threshold':LOCK_THRESHOLD,
                'reads_and_original_receipt_recovery_allowed':True,'recovery_configured':self.recovery.configured(workspace),
                'login_or_wait_unlocks':False}

    def read(self,workspace,*,request_id=None):
        identifier(request_id,True)
        with self.bank.transaction() as db:
            self.bank._workspace(db,workspace);self._context(workspace)
            query='SELECT details FROM permission_security_events WHERE workspace_id=?';args=[workspace]
            if request_id is not None:query+=' AND request_id=?';args.append(request_id)
            return [json.loads(r[0]) for r in db.execute(query+' ORDER BY created_at,id LIMIT 100',args)]

    def recover(self,workspace,payload=None):
        """只消费当前原认证会话的一次性恢复用途证明；不是模型工具。"""
        return self.recovery.recover(workspace,payload)

    def audit_http(self,handler,dispatch):
        """由已存在HTTP分派调用；仅记录服务端验证过的workspace，不信任头内主体/请求ID。"""
        from simulated_bank import BankError
        from urllib.parse import urlsplit
        original=handler._finance_json
        # 同一handler复用也不能沿用前一请求主体。
        handler._navren_workspace=None
        path=urlsplit(handler.path).path
        match=re.fullmatch(r'/api/finance/(?:research/)?operations/([a-f0-9]{32})(?:/(?:sms|verify|confirm|cancel))?',path)
        operation=match[1] if match else None
        def send(status,data,cookie=None):
            workspace=getattr(handler,'_navren_workspace',None)
            if workspace:
                self.bind(workspace)
                if status>=500:
                    self.observe(workspace,'RUNTIME_FAILURE',operation_id=operation,outcome='fault')
                elif status>=400:
                    reason=data.get('error',{}).get('code') if isinstance(data,dict) else None
                    self.rejected(workspace,BankError(code(reason),'',status),operation_id=operation)
                else:self.observe(workspace,'HTTP_ALLOWED',operation_id=operation,outcome='allowed')
            return original(status,data,cookie)
        handler._finance_json=send
        try:
            with self.request(request_id=getattr(handler,'_navren_request_id',None),phase='http'):
                return dispatch(handler)
        finally:handler._finance_json=original
