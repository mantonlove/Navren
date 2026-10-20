"""模拟银行访问密码专用入口；只保存加盐哈希，草案只含不透明引用。

与 Navren WelcomeAuth 的应用登录密码是不同的明确凭据范围；不替换
应用身份服务。实际银行密码与真实身份恢复均未连接。
"""
from contextlib import closing
import hashlib
import hmac
import secrets
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS bank_password_drafts(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 salt BLOB NOT NULL, password_hash TEXT NOT NULL, fingerprint TEXT NOT NULL,
 expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS bank_access_credentials(
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), salt BLOB NOT NULL,
 password_hash TEXT NOT NULL, updated_at REAL NOT NULL);
'''

def password_hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32).hex()

def validate_password(password):
    from .service import BankError
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or len(password.encode('utf-8')) > 512:
        raise BankError('PASSWORD_FORMAT_INVALID', '模拟银行访问密码须为12至128个字符。')

def prepare(bank, workspace, payload):
    from .service import strict_fields
    strict_fields(payload, ['new_password'], ['new_password'])
    validate_password(payload['new_password'])
    salt = secrets.token_bytes(16)
    hashed = password_hash(payload['new_password'], salt)
    fingerprint = hashlib.sha256(salt + hashed.encode()).hexdigest()
    with bank.transaction() as db:
        bank._workspace(db, workspace)
        bank.permissions.write_allowed(db, workspace)
        now = bank.permissions.clock(db, workspace)
        ref = uuid.uuid4().hex
        db.execute('INSERT INTO bank_password_drafts VALUES(?,?,?,?,?,?,NULL)',
                   (ref, workspace, salt, hashed, fingerprint, now + 300))
        bank._audit(db, workspace, 'password_replacement_prepared', {'replacement_id': ref,
                         'credential_scope': 'simulated_bank_access'}, None)
    return {'replacement_id': ref, 'expires_at': now+300, 'credential_scope': 'simulated_bank_access',
            'secret_returned': False, 'execution_authority': False}

def details(bank, db, workspace, ref):
    from .service import BankError
    row = db.execute('SELECT * FROM bank_password_drafts WHERE id=? AND workspace_id=?', (ref, workspace)).fetchone()
    if not row or row['used_at'] is not None or bank.clock() >= row['expires_at']:
        raise BankError('PASSWORD_REPLACEMENT_INVALID', '密码替换引用已失效；请重新使用专用输入。', 409)
    return {'replacement_id': ref, 'replacement_fingerprint': row['fingerprint'],
            'credential_scope': 'simulated_bank_access', 'replacement_expires_at': row['expires_at'],
            'session_revocation': 'all bank human/agent sessions and pending drafts in this workspace'}

def execute(bank, db, workspace, operation, ref):
    from .service import digest
    details(bank, db, workspace, ref)
    row = db.execute('SELECT * FROM bank_password_drafts WHERE id=? AND workspace_id=?', (ref, workspace)).fetchone()
    db.execute('INSERT INTO bank_access_credentials VALUES(?,?,?,?) ON CONFLICT(workspace_id) '
               'DO UPDATE SET salt=excluded.salt,password_hash=excluded.password_hash,updated_at=excluded.updated_at',
               (workspace, row['salt'], row['password_hash'], bank.clock()))
    db.execute('UPDATE bank_password_drafts SET used_at=? WHERE id=?', (bank.clock(), ref))
    db.execute('UPDATE workspaces SET token_hash=?,agent_token_hash=? WHERE id=?',
               (digest(secrets.token_urlsafe(32)), digest(secrets.token_urlsafe(32)), workspace))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='demo_sessions'").fetchone():
        db.execute('UPDATE demo_sessions SET revoked=1 WHERE workspace_id=?', (workspace,))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='registered_instance_sessions'").fetchone():
        subjects=list(db.execute('''SELECT DISTINCT registered_user_id,auth_subject FROM registered_instance_sessions
            WHERE workspace_id=? AND revoked=0''',(workspace,)))
        for owner,subject in subjects:
            db.execute('UPDATE registered_auth_subjects SET revoked=1 WHERE registered_user_id=? AND auth_subject=?',(owner,subject))
            db.execute('''UPDATE demo_sessions SET revoked=1 WHERE token_hash IN
                (SELECT token_hash FROM registered_instance_sessions WHERE registered_user_id=? AND auth_subject=?)''',(owner,subject))
            db.execute('UPDATE registered_instance_sessions SET revoked=1 WHERE registered_user_id=? AND auth_subject=?',(owner,subject))
    db.execute("UPDATE operations SET state='invalidated',verification_hash=NULL WHERE workspace_id=? "
               "AND id<>? AND state='pending_confirmation'", (workspace, operation))
    bank._audit(db, workspace, 'bank_password_changed', {'credential_scope': 'simulated_bank_access',
                      'all_bank_sessions_revoked': True, 'old_pending_drafts_invalidated': True}, operation)
    return {'credential_scope': 'simulated_bank_access', 'status': 'changed', 'relogin_required': True,
            'sessions_revoked': True, 'notice': '模拟银行访问密码已更新；原银行会话和未执行草案已失效。'}

def restore(bank, payload):
    from .service import strict_fields, BankError, digest
    strict_fields(payload, ['workspace_id', 'password'], ['workspace_id', 'password'])
    validate_password(payload['password'])
    workspace = payload['workspace_id']
    if not isinstance(workspace, str) or len(workspace) != 32:
        raise BankError('BANK_LOGIN_FAILED', '模拟银行账户或密码不匹配。', 401)
    with closing(bank.connect()) as db:
        if (db.execute("SELECT 1 FROM sqlite_master WHERE name='registered_user_instances'").fetchone()
                and db.execute('SELECT 1 FROM registered_user_instances WHERE workspace_id=?',(workspace,)).fetchone()):
            raise BankError('REGISTERED_LOGIN_REQUIRED','注册实例请从原手机号账号重新登录。',401)
        row = db.execute('SELECT * FROM bank_access_credentials WHERE workspace_id=?', (workspace,)).fetchone()
    expected = row['password_hash'] if row else '0'*64
    salt = row['salt'] if row else b'\0'*16
    matches = hmac.compare_digest(password_hash(payload['password'], salt), expected)
    with bank.transaction() as db:
        current = db.execute('SELECT * FROM bank_access_credentials WHERE workspace_id=?', (workspace,)).fetchone()
        if not matches or not current or current['password_hash'] != expected:
            if current:
                bank.permissions.deny(db, workspace, None, 'BANK_LOGIN_FAILED', suspicious=True)
            raise BankError('BANK_LOGIN_FAILED', '模拟银行账户或密码不匹配。', 401)
        token = secrets.token_urlsafe(32)
        # 演示目录是服务端工作区→主体的原权威映射；不能由登录请求提供别名。
        alias=None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='navren_demo_accounts'").fetchone():
            identity=db.execute('SELECT user_id,status FROM navren_demo_accounts WHERE workspace_id=?',(workspace,)).fetchone()
            if identity is not None:
                from demo_accounts import DemoAccountService,USER_IDS
                if (identity['status']!='ready' or identity['user_id'] not in USER_IDS
                        or DemoAccountService.stable_workspace(identity['user_id'])!=workspace
                        or not db.execute("SELECT 1 FROM sqlite_master WHERE name='demo_sessions'").fetchone()):
                    raise BankError('BANK_RESTORE_IDENTITY_UNAVAILABLE','原银行工作区的主体绑定尚未就绪，不能另开账户替代。',409)
                alias=identity['user_id']
                generation=db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM demo_sessions WHERE workspace_id=?',(workspace,)).fetchone()[0]
                db.execute('INSERT INTO demo_sessions VALUES(?,?,?,?,?,0)',
                           (digest(token),workspace,alias,generation,bank.clock()+2592000))
        db.execute('UPDATE workspaces SET token_hash=? WHERE id=?', (digest(token), workspace))
        bank._audit(db, workspace, 'bank_password_session_restored', {'credential_scope': 'simulated_bank_access',
                            'does_not_unlock_financial_writes': True,'restored_account_id':alias}, None)
    return {'workspace_id': workspace, 'session_token': token, 'account_id':alias,'credential_scope': 'simulated_bank_access',
            'notice': '已恢复同一模拟银行工作区；没有解除金融安全锁。'}
