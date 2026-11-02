"""可信宿主的脱敏安全事件接口；不是模型工具或HTTP上报入口。"""
from contextlib import closing,contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
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

# A parsed HTTP body is still unverified input. Never hash arbitrary body keys,
# credentials, challenge/proof values, headers or raw nested objects.
PREPARE_FIELDS=frozenset({'action','account_id','product_id','amount_minor','mandate_id',
 'card_id','control_type','enabled','limit_minor','requested_type','requested_limit_minor',
 'idempotency_key','liquidity_floor_minor','beneficiary_id','reference','frequency',
 'next_due_on','end_on','scheduled_payment_id','split_id','participant_id','subscription_id',
 'task_id','expires_on','hold_id','quote_ids','remittance','aa_bill','aa_share_id',
 'aa_bill_id','replacement_id','purpose'})
PREPARE_OBJECT_FIELDS=frozenset({'account_id','product_id','mandate_id','card_id',
 'beneficiary_id','scheduled_payment_id','split_id','participant_id','subscription_id',
 'task_id','hold_id','aa_share_id','aa_bill_id','replacement_id'})
PREPARE_NESTED_FIELDS={
 'remittance':frozenset({'card_id','sender_name','sender_phone','recipient_name',
   'recipient_account','recipient_bank','recipient_branch','purpose','message','mode'}),
 'aa_bill':frozenset({'title','total_minor','split_mode','participants','due_at',
   'source_transaction_id','receiver_card_id','source_consumption_id','attachment_ids',
   'consumption_snapshot','attachment_snapshots','consumption_fingerprint'}),
 'participants':frozenset({'user_id','share_minor'})}

def prepare_parameter_binding(payload):
    """Allowlisted field fingerprints only; this does not validate arguments."""
    def field(name,value):
        kind={str:'string',int:'integer',float:'number',bool:'boolean',type(None):'null',
              list:'array',dict:'object'}.get(type(value),'unsupported')
        row={'type':kind,'validation_status':'unvalidated'}
        if type(value) in (str,int,float,bool,type(None)):
            row['sha256']=digest(['navren.prepare-field/1',name,value])
        elif type(value) is dict and name in PREPARE_NESTED_FIELDS:
            row['fields']={k:field(k,value[k]) for k in sorted(PREPARE_NESTED_FIELDS[name]&value.keys())}
        elif type(value) is list and name in ('quote_ids','attachment_ids') and all(type(x)is str for x in value):
            row['sha256']=digest(['navren.prepare-field/1',name,value])
        elif type(value) is list and name=='participants':
            row['items']=[field('participants',x) for x in value[:64]]
            row['complete']=len(value)<=64
        else:row['binding_status']='structure_only_value_not_bound'
        return row
    fields={k:field(k,payload[k]) for k in sorted(PREPARE_FIELDS&payload.keys())}
    return {'contract':'navren.prepare-parameters/1','fields':fields,
            'parameters_sha256':digest(fields),'binding_scope':'allowlisted_fields_not_raw_body',
            'input_validation_status':'unvalidated','ignored_field_count':len(payload.keys()-PREPARE_FIELDS)}

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
    before_states:dict=field(default_factory=dict)
    requested_operation_id:object=None
    prepare_request:object=None
    prepare_operation_id:object=None

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
        token=_scope.set(item)
        try:
            if operation_id and workspace:
                with closing(self.bank.connect()) as db:self.capture_before(db,workspace,operation_id)
            yield item
        finally:_scope.reset(token)

    def bind(self,workspace,*,capture_before=False):
        item=_scope.get()
        if item and item.bank is self.bank:
            if item.workspace is not None and item.workspace!=workspace:fail('SECURITY_EVENT_WORKSPACE_MISMATCH',403)
            item.workspace=workspace
            # Only the successful identity boundary opts in. The response hook
            # binds without this flag and cannot relabel an after-state as before.
            target=item.operation_id or item.requested_operation_id
            if capture_before and target:
                with closing(self.bank.connect()) as db:self.capture_before(db,workspace,target)

    def _prepare_audit_only(self,workspace,callback):
        try:callback()
        except Exception:
            # New observation failures never replace the original business
            # decision. No exception message, payload or credential is recorded.
            item=_scope.get()
            if item is not None and item.bank is self.bank and item.workspace==workspace:
                item.prepare_request={'contract':'navren.prepare-observation/1',
                    'audit_status':'observation_unavailable','parameters':None,
                    'input_parameters_remain_unverified':True,'checkpoints':[],
                    'resolved_targets':{},'before_state_status':'not_observed'}

    def capture_prepare_request(self,workspace,payload):
        self._prepare_audit_only(workspace,lambda:self._capture_prepare_request(workspace,payload))

    def prepare_checkpoint(self,workspace,stage,*,account_id=None,operation_id=None):
        self._prepare_audit_only(workspace,lambda:self._prepare_checkpoint(workspace,stage,
            account_id=account_id,operation_id=operation_id))

    def _capture_prepare_request(self,workspace,payload):
        """Trusted gateway after identity/CSRF/JSON only; no business validation."""
        item=_scope.get()
        if item is None or item.bank is not self.bank or item.phase!='http':return
        if item.workspace!=workspace:fail('SECURITY_EVENT_WORKSPACE_MISMATCH',403)
        if item.prepare_request is not None:fail('SECURITY_EVENT_CONFLICT',409)
        item.prepare_request={'contract':'navren.prepare-observation/1',
            'input_source':'authenticated_csrf_checked_json_object',
            'parameters':prepare_parameter_binding(payload),
            'checkpoints':[],'resolved_targets':{},
            'before_state_status':'operation_not_identified_at_input_capture'}

    def _prepare_checkpoint(self,workspace,stage,*,account_id=None,operation_id=None):
        """Record only the original gateway checkpoint which actually returned."""
        item=_scope.get()
        if item is None or item.bank is not self.bank or item.prepare_request is None:return
        if item.workspace!=workspace:fail('SECURITY_EVENT_WORKSPACE_MISMATCH',403)
        if stage not in ('basic_fields_validated','budget_account_observed','bank_prepare_returned'):fail()
        if stage not in item.prepare_request['checkpoints']:item.prepare_request['checkpoints'].append(stage)
        if stage=='budget_account_observed':
            item.prepare_request['resolved_targets']['account_id']={
                'ref':digest(['account_id',workspace,account_id]),
                'status':'membership_observed_by_original_budget_read',
                'authorization_status':'preparation_policy_not_observed'}
        if stage=='bank_prepare_returned':
            identifier(operation_id);item.prepare_operation_id=operation_id

    def _prepare_observation(self,db,item,workspace,operation,op):
        if item.prepare_request is None:return None
        value=json.loads(packed(item.prepare_request))
        value['operation_id']=operation if op else None
        value['after_state_status']='operation_record_observed'if op else'no_operation_record_bound'
        value['after_state']=op['state']if op else None
        value['authorization_status']='preparation_policy_not_observed'
        value['input_parameters_remain_unverified']=True
        if op:
            binding=db.execute('SELECT policy,grade,summary_hash FROM permission_bindings '
                'WHERE workspace_id=? AND operation_id=?',(workspace,operation)).fetchone()
            from simulated_bank.permission_policy import POLICY_VERSION
            valid=(binding is not None and binding['policy']==POLICY_VERSION
                and binding['grade']==op['permission']and binding['summary_hash']==op['confirmation_digest'])
            value['authorization_status']=('original_preparation_policy_binding_observed'
                if valid else'operation_without_matching_preparation_policy_binding')
            if valid:
                payload=json.loads(op['payload'])
                value['prepared_parameters']=prepare_parameter_binding(payload)
                value['prepared_parameters']['input_validation_status']='original_bank_prepare_record'
                value['resolved_targets']={k:{'ref':digest([k,workspace,payload[k]]),
                    'status':'recorded_in_original_validated_operation',
                    'authorization_status':'preparation_only_not_confirmation_or_execution'}
                    for k in sorted(PREPARE_OBJECT_FIELDS&payload.keys())if type(payload[k])is str}
        return value

    def capture_before(self,db,workspace,operation_id):
        """Trusted host only, before its restricted call; never a model/HTTP tool."""
        identifier(operation_id)
        item=_scope.get()
        if item is None or item.bank is not self.bank:return
        if item.workspace!=workspace:fail('SECURITY_EVENT_WORKSPACE_MISMATCH',403)
        if item.operation_id is not None and item.operation_id!=operation_id:fail('SECURITY_EVENT_OPERATION_MISMATCH',409)
        if operation_id in item.before_states:return
        if len(item.before_states)>=32:fail()
        self.bank._workspace(db,workspace)
        row=db.execute('SELECT state FROM operations WHERE workspace_id=? AND id=?',(workspace,operation_id)).fetchone()
        item.before_states[operation_id]={'value':row['state']if row else None,
            'status':'observed_before_call'if row else'not_observed_in_bound_workspace'}
        if item.operation_id==operation_id:item.before_state=row['state']if row else None

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
            op=db.execute('SELECT state,permission,confirmation_digest,payload FROM operations WHERE workspace_id=? AND id=?',(workspace,operation)).fetchone()
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
        before=item.before_states.get(operation)
        if before is None:
            before={'value':None,'status':('operation_identified_after_call_start'if op else'no_bound_operation')}
        recorded_at=self.bank.clock()
        value={'contract':CONTRACT,'event_id':eid,'request_id':item.request_id,'workspace_id':workspace,
            'operation_id':operation if op else None,'requested_object_ref':operation_ref if operation and not op else None,
            'phase':phase,'outcome':outcome,'reason_code':reason,'policy':POLICY_VERSION,
            'risk':op['permission'] if op else None,'evidence_sha256':list(evidence_sha256),
            'confirmation_ref':op['confirmation_digest'] if op else None,'authentication_ref':authentication_ref,
            'before_state':before['value'],'after_state':op['state'] if op else None,
            'recorded_at':recorded_at,
            'decision_summary':{'contract':'navren.security-decision-summary/1',
                'source':'trusted_host_policy_observation','policy_version':POLICY_VERSION,
                'reason_code':reason,'outcome':outcome,'subject_ref':digest(['workspace',workspace]),
                'object_ref':operation_ref,'risk_source':'operation_record'if op else'no_bound_operation',
                'before_state_status':before['status'],
                'after_state_status':'operation_record_observed'if op else'no_bound_operation',
                'evidence_status':'trusted_digest_supplied'if evidence_sha256 else'not_supplied_for_this_stage',
                'confirmation_status':'operation_summary_reference_only'if op else'no_bound_operation',
                'authentication_status':('proof_reference_observed_not_validated_by_audit'if authentication_ref else
                    'not_required_by_recorded_yellow_grade'if op and op['permission']=='yellow'else
                    'not_observed_for_recorded_red_grade'if op and op['permission']=='red'else'no_bound_operation'),
                'audit_is_not_execution_authority':True},
            'suspicious':bool(suspicious),'security_failure_recorded':counted,
            'failures':security['failures'] if security else 0,'financial_write_locked':bool(security and security['locked'])}
        try:preparation=self._prepare_observation(db,item,workspace,operation,op)
        except Exception:
            preparation={'contract':'navren.prepare-observation/1',
                'audit_status':'observation_unavailable','input_parameters_remain_unverified':True}
        if preparation is not None:value['prepare_observation']=preparation
        db.execute('INSERT INTO permission_security_events VALUES(?,?,?,?,?,?,?,?,?)',
            (eid,workspace,item.request_id,item.event_ref,phase,outcome,reason,packed(value),recorded_at))
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
                item=_scope.get()
                bound_operation=operation or (item.prepare_operation_id if item is not None else None)
                if status>=500:
                    self.observe(workspace,'RUNTIME_FAILURE',operation_id=bound_operation,outcome='fault')
                elif status>=400:
                    reason=data.get('error',{}).get('code') if isinstance(data,dict) else None
                    self.rejected(workspace,BankError(code(reason),'',status),operation_id=bound_operation)
                else:self.observe(workspace,'HTTP_ALLOWED',operation_id=bound_operation,outcome='allowed')
            return original(status,data,cookie)
        handler._finance_json=send
        try:
            with self.request(request_id=getattr(handler,'_navren_request_id',None),phase='http') as item:
                # A route target guides an observation; it must not turn this
                # multi-operation HTTP scope into an explicit single-op lock.
                item.requested_operation_id=operation
                return dispatch(handler)
        finally:handler._finance_json=original
