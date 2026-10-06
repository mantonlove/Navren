"""演示选择器的服务端身份。切换原子撤销旧令牌，不迁移账户授权。"""
from contextlib import closing
from contextlib import contextmanager
from contextvars import ContextVar
import secrets
import hmac
from simulated_bank import BankError
from simulated_bank.service import digest

SCHEMA = '''CREATE TABLE IF NOT EXISTS demo_sessions(
 token_hash TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 user_id TEXT NOT NULL, generation INTEGER NOT NULL, expires_at REAL NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
CREATE INDEX IF NOT EXISTS demo_sessions_workspace ON demo_sessions(workspace_id);'''


class DemoSessions:
    def __init__(self, gateway):
        self.g = gateway
        self.bank = gateway.bank
        self._request = ContextVar('navren_demo_request_identity', default=None)
        self.bank.request_identity_guard = self.guard_transaction
        self.bank.request_pin_verifier = self.pin_matches
        with closing(self.bank.connect()) as db:
            db.executescript(SCHEMA)

    def _create(self, db, user_id, workspace, generation=1):
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO demo_sessions VALUES(?,?,?,?,?,0)',
                   (digest(token), workspace, user_id, generation, self.bank.clock()+2592000))
        db.execute('UPDATE workspaces SET pin_hash=? WHERE id=?',
                   (digest(workspace+':'+self.g.demo_code(token)), workspace))
        self.bank._audit(db, workspace, 'demo_session_created', {'generation': generation})
        return workspace, token

    def create(self, user_id):
        workspace = self.g.demo_accounts.workspace_for(user_id)
        with self.bank.transaction() as db:
            return self._create(db, user_id, workspace)

    def select(self, token, user_id, expected_workspace_id, expected_privacy_revision=None):
        target = self.g.demo_accounts.workspace_for(user_id)
        with self.bank.transaction() as db:
            old = db.execute('SELECT * FROM demo_sessions WHERE token_hash=?', (digest(token),)).fetchone()
            if old:
                if old['revoked'] or old['expires_at'] <= self.bank.clock():
                    raise BankError('SESSION_CHANGED', '账户会话已切换，请刷新后再试。', 409)
                previous, generation = old['workspace_id'], old['generation']+1
            else:
                # 从原有随机演示账户迁入；永久撤销这条旧会话令牌。
                previous = db.execute('SELECT id FROM workspaces WHERE token_hash=?', (digest(token),)).fetchone()
                if not previous:
                    raise BankError('UNAUTHENTICATED', '演示会话已失效。', 401)
                previous, generation = previous['id'], 1
                db.execute('INSERT INTO demo_sessions VALUES(?,?,?,?,?,1)',
                           (digest(token), previous, 'legacy', 0, self.bank.clock()))
            if expected_workspace_id != previous:
                raise BankError('WORKSPACE_CHANGED', '当前账户已变化，请重新核对。', 409)
            if expected_privacy_revision is not None:
                from user_memory import privacy_revision
                if type(expected_privacy_revision) is not int or privacy_revision(db,previous)!=expected_privacy_revision:
                    raise BankError('PRIVACY_REVISION_CHANGED','账户权限已变化，请重新核对。',409)
            db.execute('UPDATE demo_sessions SET revoked=1 WHERE token_hash=?', (digest(token),))
            self.bank._audit(db, previous, 'demo_account_left', {'target_user_id': user_id})
            return self._create(db, user_id, target, generation)

    def describe(self, token):
        with closing(self.bank.connect()) as db:
            row = db.execute('SELECT user_id,generation,revoked,expires_at FROM demo_sessions WHERE token_hash=?',
                             (digest(token),)).fetchone()
        if not row:
            return {'mode': 'legacy_simulation'}
        if row['revoked'] or row['expires_at'] <= self.bank.clock():
            raise BankError('SESSION_CHANGED', '演示会话已失效。', 409)
        return {'mode': 'demo_account', 'user_id': row['user_id'], 'generation': row['generation'],
                'notice': '演示账户选择，不是实名登录。回原账户后草案仍须重新核对版本、有效期并本人确认。'}

    def validate(self, token, workspace):
        actual, role = self.bank.session(token)
        if actual != workspace or role != 'human':
            raise BankError('WORKSPACE_CHANGED', '账户已切换，旧请求已丢弃。', 409)

    @contextmanager
    def request(self, token, workspace):
        binding=self._request.set((token,workspace))
        try:yield
        finally:self._request.reset(binding)

    def guard_transaction(self, db):
        """已取得银行写锁之后回查身份；返回前校验不能撤销已提交扣款。"""
        request=self._request.get()
        if request is None:return  # 显式内部任务，不冒用HTTP身份。
        token,workspace=request
        session=db.execute('SELECT * FROM demo_sessions WHERE token_hash=?',(digest(token),)).fetchone()
        if session:
            if session['revoked'] or session['expires_at']<=self.bank.clock() or session['workspace_id']!=workspace:
                raise BankError('SESSION_CHANGED','账户会话已切换，旧请求未执行。',409)
        elif not db.execute('SELECT 1 FROM workspaces WHERE id=? AND token_hash=?',(workspace,digest(token))).fetchone():
            raise BankError('SESSION_CHANGED','账户会话已失效，旧请求未执行。',409)

    def pin_matches(self, db, workspace, pin):
        request=self._request.get()
        if request is None:return None  # 保留直接银行模拟接口。
        token,owner=request
        if owner!=workspace:raise BankError('WORKSPACE_CHANGED','验证码不属于当前账户。',409)
        self.guard_transaction(db)
        return hmac.compare_digest(self.g.demo_code(token),pin)
