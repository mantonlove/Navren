"""受信运行时在原银行事务内回查身份和来源；不创建嵌套事务。"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import hmac
import secrets
import threading
from contextlib import closing

from simulated_bank import BankError


@dataclass(frozen=True)
class RequestBinding:
    nonce: str


def capture_request_binding(gateway, workspace):
    request = gateway.demo_sessions._request.get()
    if request is None or request[1] != workspace:
        raise BankError('RUNTIME_REQUEST_BINDING_REQUIRED', '运行时没有当前受信 HTTP 身份。', 409)
    token_hash = hashlib.sha256(request[0].encode()).hexdigest()
    from user_memory import privacy_revision
    with closing(gateway.bank.connect()) as db:
        row = db.execute('SELECT * FROM demo_sessions WHERE token_hash=?', (token_hash,)).fetchone()
        if row:
            if row['workspace_id'] != workspace or row['revoked'] or row['expires_at'] <= gateway.bank.clock():
                raise BankError('SESSION_CHANGED', '运行时身份已失效。', 409)
            generation, expires = row['generation'], row['expires_at']
        else:
            owner = db.execute('SELECT token_hash FROM workspaces WHERE id=?', (workspace,)).fetchone()
            if not owner or owner['token_hash'] != token_hash:
                raise BankError('SESSION_CHANGED', '运行时身份已失效。', 409)
            generation, expires = 0, gateway.bank.clock() + 120
        revision = privacy_revision(db, workspace)
    binding = RequestBinding(secrets.token_urlsafe(32))
    now = gateway.bank.clock()
    with gateway._runtime_binding_lock:
        for key in [k for k,v in gateway._runtime_bindings.items() if now >= v['expires']]:
            del gateway._runtime_bindings[key]
        if len(gateway._runtime_bindings) >= 1024:
            raise BankError('RUNTIME_REQUEST_BINDING_LIMIT', '运行时身份绑定数量已达上限。', 429)
        gateway._runtime_bindings[binding.nonce] = {'workspace':workspace, 'token_hash':token_hash,
            'generation':generation, 'expires':min(expires, now+120), 'created':now,
            'privacy':revision, 'thread':threading.get_ident()}
    return binding


def revalidate_request_binding(gateway, binding, *, bank_db):
    if not isinstance(binding, RequestBinding):
        raise BankError('RUNTIME_REQUEST_BINDING_INVALID', '运行时身份绑定不是宿主发行对象。', 409)
    with gateway._runtime_binding_lock:
        record = gateway._runtime_bindings.get(binding.nonce)
    request = gateway.demo_sessions._request.get()
    now = gateway.bank.clock()
    if (not record or not request or record['thread'] != threading.get_ident()
            or not record['created'] <= now < record['expires'] or request[1] != record['workspace']
            or hashlib.sha256(request[0].encode()).hexdigest() != record['token_hash']):
        raise BankError('RUNTIME_REQUEST_BINDING_INVALID', '运行时请求身份已变化或过期。', 409)
    if not bank_db.in_transaction or not any(r[1] == 'main' and Path(r[2]).resolve() == gateway.bank.database
                                            for r in bank_db.execute('PRAGMA database_list')):
        raise BankError('RUNTIME_BANK_BINDING_INVALID', '运行时必须复用当前银行事务。', 409)
    row = bank_db.execute('SELECT * FROM demo_sessions WHERE token_hash=?', (record['token_hash'],)).fetchone()
    if row:
        valid = (not row['revoked'] and row['workspace_id'] == record['workspace']
                 and row['generation'] == record['generation'] and now < row['expires_at'])
    else:
        owner = bank_db.execute('SELECT token_hash FROM workspaces WHERE id=?', (record['workspace'],)).fetchone()
        valid = record['generation'] == 0 and owner and owner['token_hash'] == record['token_hash']
    from user_memory import privacy_revision
    if not valid or privacy_revision(bank_db, record['workspace']) != record['privacy']:
        raise BankError('SESSION_CHANGED', '运行时授权或身份已失效。', 409)
    return {'workspace_id':record['workspace'], 'generation':record['generation'],
            'privacy_revision':record['privacy'], 'same_bank_transaction':True,
            'financial_execution_authority':False}


@dataclass(frozen=True)
class RuntimeCheckpoint:
    gateway: object
    workspace: str
    session_token: str
    expected_privacy: int
    expected_personal: object
    metric_batches: tuple = ()
    metric_snapshot: object = None
    knowledge_snapshot: object = None
    market_snapshot: object = None
    service_snapshot: object = None

    def validate(self, bank_db, *, external_sources_held=False):
        """调用者持原 response_guard/publication_guard 的所有来源锁。

        仅无外部来源的私人指标复现可省略 external_sources_held；包含公开
        来源时必须在受信宿主持原锁的生命周期内显式声明。此对象/声明仅
        服务端可构造，不能从模型工具或 HTTP JSON 反序列化。
        """
        if not bank_db.in_transaction:
            raise BankError('RUNTIME_TRANSACTION_REQUIRED', '运行时检查必须使用当前银行事务。', 409)
        files = {r[1]: r[2] for r in bank_db.execute('PRAGMA database_list')}
        if not files.get('main') or Path(files['main']).resolve() != self.gateway.bank.database:
            raise BankError('RUNTIME_BANK_BINDING_INVALID', '运行时连接不属于当前银行库。', 409)
        token_hash = hashlib.sha256(self.session_token.encode()).hexdigest()
        session = bank_db.execute('SELECT * FROM demo_sessions WHERE token_hash=?', (token_hash,)).fetchone()
        if session:
            if session['workspace_id'] != self.workspace or session['revoked'] or session['expires_at'] <= self.gateway.bank.clock():
                raise BankError('SESSION_CHANGED', '运行时身份已失效。', 409)
        else:
            owner = bank_db.execute('SELECT token_hash FROM workspaces WHERE id=?', (self.workspace,)).fetchone()
            if not owner or not hmac.compare_digest(owner['token_hash'], token_hash):
                raise BankError('SESSION_CHANGED', '运行时身份已失效。', 409)
        if (self.knowledge_snapshot is not None or self.market_snapshot is not None) and external_sources_held is not True:
            raise BankError('RUNTIME_EXTERNAL_SOURCE_NOT_HELD', '公开来源未持续锁定，运行时输出不能被消费。', 409)
        self.gateway.assert_response_evidence(self.workspace, self.expected_privacy, self.expected_personal,
            self.knowledge_snapshot, db=bank_db, market_snapshot=self.market_snapshot,
            metric_snapshot=self.metric_snapshot, service_snapshot=self.service_snapshot,
            metric_batches=list(self.metric_batches))
        return {'identity_verified': True, 'same_bank_transaction': True, 'sources_verified': True,
                'financial_execution_authority': False}
