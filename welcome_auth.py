"""本地 Navren 身份认证与同源业务代理；不承担银行实名认证。"""
from __future__ import annotations

from contextlib import contextmanager
from http import client, server
from http.cookies import SimpleCookie
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import sqlite3
import threading
import time
import unicodedata
import uuid
from urllib.parse import unquote, urlsplit

from sms_auth import SmsChallenges
from sms_aliyun import SmsFailure
from simulated_bank import BankError


ROOT = Path(__file__).resolve().parent
COOKIE = 'navren_welcome_session'
SESSION_TTL = 12 * 60 * 60
ANONYMOUS_TTL = 30 * 60
MAX_BODY = 1024 * 1024
MAX_HTML = 2 * 1024 * 1024
MAX_CSV_BODY = 1_572_864
CSV_PREVIEW_PATHS = {'/api/finance/statements/preview', '/api/finance/calendar/import/preview'}
# MEDIA-20261016冻结协议：仅此无query的POST原件入口扩大传输上限；MIME分限/真类型由基座核验。
CHAT_MEDIA_UPLOAD_PATH = '/api/finance/chat-media'
MAX_CHAT_MEDIA_BODY = 25 * 1024 * 1024
CHAT_MEDIA_UPLOAD_HEADERS = frozenset({'x-navren-expected-workspace','x-navren-privacy-revision',
    'x-navren-archive-epoch','x-navren-upload-key'})
CHAT_MEDIA_BIND_PATH = '/api/finance/chat-media/bind'
CHAT_MEDIA_BIND_HEADERS = CHAT_MEDIA_UPLOAD_HEADERS - {'x-navren-upload-key'}
ACCOUNT_STATE_WRITE_PATH = '/api/finance/account-state'
ACCOUNT_STATE_WRITE_HEADERS = frozenset({'x-navren-workspace','x-navren-privacy-revision'})
RESEARCH_UPLOAD_PATH = '/api/finance/research/attachments'
MAX_RESEARCH_UPLOAD_BODY = 10 * 1024 * 1024
RESEARCH_UPLOAD_HEADERS = frozenset({'x-navren-expected-revision','x-navren-upload-key',
    'x-navren-confirmed','x-navren-health-consent'})
RESEARCH_PRIVATE_WRITE = re.compile(r'/api/finance/research/(?:workspace|compare|calculate|policies|tasks|prepare|records/[A-Za-z0-9_.:-]{1,160}/(?:delete|operations)|attachments/[A-Za-z0-9_.:-]{1,160}/delete)')
APP_PAGES = {'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html', 'messages.html'}
AUTH_PREFIX = '/api/welcome/auth/'
EXPERIENCE_ACCOUNTS = {
    'tycoon': 'A市首富',
    'grain_farmer': '种粮大户',
    'working_landlady': '坚持上班的包租婆',
    'retired_entrepreneur': '退休后重新创业的阿姨',
}
EXPERIENCE_UNAVAILABLE = '测试账户尚未接通，请稍后再试。'
PUBLIC_ROUTES = {
    '/': 'outputs/welcome-scene/index.html',
    '/index.html': 'outputs/welcome-scene/index.html',
    '/css/welcome_scene.css': 'css/welcome_scene.css',
    '/js/welcome_scene.js': 'js/welcome_scene.js',
    '/js/welcome_water.js': 'js/welcome_water.js',
    '/js/welcome_voyage.js': 'js/welcome_voyage.js',
    '/js/welcome_rock.js': 'js/welcome_rock.js',
    '/js/welcome_login.js': 'js/welcome_login.js',
    '/js/welcome_session.js': 'js/welcome_session.js',
    '/assets/brand/welcome-scene.png': 'assets/brand/welcome-scene.png',
    '/assets/brand/navren-icon.png': 'assets/brand/navren-icon.png',
    '/assets/brand/navren-wordmark.png': 'assets/brand/navren-wordmark.png',
    '/assets/brand/rock-face-diff-2k.jpg': 'assets/brand/rock-face-diff-2k.jpg',
    '/assets/brand/rock-face-disp-2k.jpg': 'assets/brand/rock-face-disp-2k.jpg',
    '/assets/brand/rock-face-arm-2k.jpg': 'assets/brand/rock-face-arm-2k.jpg',
}
PUBLIC_EXTENSIONS = {'.js', '.css', '.json', '.png', '.jpg', '.jpeg', '.webp',
                     '.svg', '.ico', '.woff', '.woff2', '.ttf'}
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}
FORWARDED_HEADERS = {'accept', 'accept-language', 'content-type', 'x-qz-csrf',
                     'range', 'sec-websocket-key', 'sec-websocket-version',
                     'sec-websocket-protocol'}


class AuthError(Exception):
    def __init__(self, message, status=400, code=None):
        self.message, self.status, self.code = message, status, code


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _password_hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1,
                          maxmem=64 * 1024 * 1024).hex()


class _UnbufferedHTTPResponse(client.HTTPResponse):
    """升级握手不预读后续 WebSocket 帧，交给透明双向隧道。"""
    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp.close()
        self.fp = sock.makefile('rb', buffering=0)


class WelcomeAuth:
    """密码只存加盐哈希；注册模式业务Cookie按当前认证会话隔离。"""
    def __init__(self, database=None, clock=time.time, *, sms_transport=None, allow_test_sms=False,
                 allow_unverified_registration=False, allow_legacy_experience=False):
        self.database = Path(database or Path.home() / 'Library/Application Support/Navren/welcome-auth.sqlite3')
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise ValueError('认证数据库不能使用符号链接')
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.database, 0o600)
        self.clock = clock
        # 旧注册仅供显式启用的隔离夹具；正式入口的认证要求不随短信渠道健康而降级。
        self.allow_unverified_registration = allow_unverified_registration is True
        self.allow_legacy_experience = allow_legacy_experience is True
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._hash_slots = threading.BoundedSemaphore(4)
        self._dummy_salt = secrets.token_bytes(16)
        self._dummy_hash = _password_hash(secrets.token_urlsafe(24), self._dummy_salt)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS welcome_users(
                  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                  salt BLOB NOT NULL, password_hash TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS welcome_sessions(
                  token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES welcome_users(id),
                  csrf TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS welcome_upstream_cookies(
                  user_id INTEGER NOT NULL REFERENCES welcome_users(id), name TEXT NOT NULL,
                  value TEXT NOT NULL, PRIMARY KEY(user_id,name));
                CREATE TABLE IF NOT EXISTS welcome_session_upstream_cookies(
                  token_hash TEXT NOT NULL REFERENCES welcome_sessions(token_hash) ON DELETE CASCADE,
                  name TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(token_hash,name));
                CREATE TABLE IF NOT EXISTS welcome_session_business_state(
                  token_hash TEXT PRIMARY KEY REFERENCES welcome_sessions(token_hash) ON DELETE CASCADE,
                  revoked INTEGER NOT NULL CHECK(revoked IN (0,1)));
                CREATE TABLE IF NOT EXISTS welcome_login_failures(
                  username TEXT PRIMARY KEY, count INTEGER NOT NULL, until_at REAL NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            # 旧用户名账户保持本地密码身份；体验身份不可走密码登录。
            columns = {row['name'] for row in db.execute('PRAGMA table_info(welcome_users)')}
            if 'identity_kind' not in columns:
                db.execute("ALTER TABLE welcome_users ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'local'")
            if 'experience_account' not in columns:
                db.execute('ALTER TABLE welcome_users ADD COLUMN experience_account TEXT')
            if 'registered_user_id' not in columns:
                db.execute("ALTER TABLE welcome_users ADD COLUMN registered_user_id TEXT "
                    "CHECK(registered_user_id IS NULL OR (length(registered_user_id)=32 AND registered_user_id NOT GLOB '*[^0-9a-f]*'))")
            for row in db.execute("SELECT id FROM welcome_users WHERE identity_kind='local' AND registered_user_id IS NULL").fetchall():
                db.execute('UPDATE welcome_users SET registered_user_id=? WHERE id=? AND registered_user_id IS NULL',
                           (uuid.uuid4().hex,row['id']))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS welcome_registered_owner '
                       'ON welcome_users(registered_user_id) WHERE registered_user_id IS NOT NULL')
        self.sms = SmsChallenges(self, sms_transport, allow_test=allow_test_sms)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def validate_credentials(username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            raise AuthError('请输入用户名和密码。')
        username = unicodedata.normalize('NFKC', username).strip().lower()
        if not re.fullmatch(r'[a-z0-9_\-\u3400-\u9fff]{2,32}', username):
            raise AuthError('用户名需为 2–32 位中文、英文、数字、下划线或短横线。')
        try:
            password_bytes = password.encode('utf-8')
        except UnicodeError:
            raise AuthError('密码包含无法读取的字符。') from None
        if not 8 <= len(password) <= 128 or len(password_bytes) > 512:
            raise AuthError('密码需为 8–128 个字符。')
        return username, password

    @staticmethod
    def validate_phone(phone):
        # 仅把号码作为本地账户标识；这里没有短信或号码归属验证。
        if not isinstance(phone, str) or not re.fullmatch(r'1[3-9][0-9]{9}', phone):
            raise AuthError('请输入正确的 11 位手机号。')
        return phone

    def session(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        with self.connect() as db:
            row = db.execute('''SELECT s.*,u.username,u.identity_kind,u.experience_account,u.registered_user_id FROM welcome_sessions s
                LEFT JOIN welcome_users u ON u.id=s.user_id
                WHERE token_hash=? AND expires_at>?''', (_digest(token), self.clock())).fetchone()
        if not row:
            return None
        result = dict(row)
        binding = self.sms.binding_for_user(result['user_id']) if result['user_id'] is not None else None
        result['sms_configured'] = self.sms.configured
        result['sms_registration_required'] = not self.allow_unverified_registration
        result['legacy_experience_allowed'] = self.allow_legacy_experience
        result['phone_verified'] = binding is not None
        result['masked_phone'] = binding['phone'][:3] + '****' + binding['phone'][-4:] if binding else None
        return result

    def _new_session_in(self, db, user_id, old_token):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = SESSION_TTL if user_id is not None else ANONYMOUS_TTL
        db.execute('DELETE FROM welcome_sessions WHERE expires_at<=?', (self.clock(),))
        if old_token:
            if not db.execute('SELECT 1 FROM welcome_sessions WHERE token_hash=?', (_digest(old_token),)).fetchone():
                raise AuthError('登录页面已过期，请刷新后再试。', 401, 'WELCOME_SESSION_REVOKED')
            db.execute('DELETE FROM welcome_sessions WHERE token_hash=?', (_digest(old_token),))
        db.execute('INSERT INTO welcome_sessions VALUES(?,?,?,?)',
                   (_digest(token), user_id, csrf, self.clock() + ttl))
        return token

    def new_session(self, user_id=None, old_token=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            token = self._new_session_in(db, user_id, old_token)
        return token, self.session(token)

    @staticmethod
    def public_session(session, current_identity=None, business_status='not_connected'):
        legacy = bool(session and session.get('legacy_experience_allowed'))
        result = {'authenticated': bool(session and session['user_id'] is not None and
                  (session.get('identity_kind')=='local' or legacy)),
                  'session_contract': 'welcome-business-v1',
                  'sms': {'configured': bool(session and session.get('sms_configured')),
                          'registration_required': bool(session and session.get('sms_registration_required')),
                          'phone_verified': bool(session and session.get('phone_verified')),
                          'masked_phone': session.get('masked_phone') if session else None}}
        if session:
            result['csrf'] = session['csrf']
            result['auth_subject'] = 'welcome-' + session['token_hash']
        if result['authenticated']:
            result.update(user={'username': 'Navren用户', 'display_name': 'Navren用户'},
                          registered_user_id=session['registered_user_id'],
                          redirect='/chat.html' if current_identity or legacy else
                            '/?view=experience' if session.get('phone_verified') else '/?view=bind',
                          experience_selection_required=not bool(current_identity),
                          selection_required=not bool(current_identity),
                          account_id=current_identity.get('account_id') if current_identity else None,
                          workspace_id=current_identity.get('workspace_id') if current_identity else None,
                          mode='local', entry_experience_account=None,
                          experience_account=None, current_business_identity=current_identity,
                          business_identity_status=business_status)
            account = current_identity.get('account_id') if current_identity else None
            if account in EXPERIENCE_ACCOUNTS:
                result['experience_account'] = {'user_id': account, 'display_name': EXPERIENCE_ACCOUNTS[account]}
            if legacy and session.get('identity_kind') == 'local':
                result['user']['username']=session['username']
            if legacy and session.get('identity_kind') == 'experience':
                result.update(mode='experience', entry_experience_account={
                    'user_id':session['experience_account'],
                    'display_name':EXPERIENCE_ACCOUNTS[session['experience_account']]})
                result['user']['username'] = EXPERIENCE_ACCOUNTS.get(account, '测试体验')
        return result

    @staticmethod
    def auth_subject(session):
        return 'welcome-' + session['token_hash']

    def business_lock_key(self, session):
        return session['user_id'] if self.allow_legacy_experience and session['user_id'] is not None else session['token_hash']

    def registered_owner(self, token):
        """仅内部调用：从当前认证会话解析稳定主体，不接受前端自报owner。"""
        session = self.session(token)
        if not session or session['user_id'] is None or session.get('identity_kind') != 'local':
            raise AuthError('请先使用手机号注册并登录。',401,'REGISTRATION_REQUIRED')
        if not session.get('phone_verified'):
            raise AuthError('请先完成手机号验证。',403,'PHONE_VERIFICATION_REQUIRED')
        owner = session.get('registered_user_id')
        if not isinstance(owner,str) or re.fullmatch(r'[0-9a-f]{32}',owner) is None:
            raise AuthError('账户归属尚未就绪，请稍后再试。',503,'REGISTERED_OWNER_UNAVAILABLE')
        return owner

    def register(self, username, password, old_token):
        if not self.allow_unverified_registration:
            raise AuthError('创建账户需要验证手机号。', 403, 'SMS_REQUIRED')
        username, password = self.validate_credentials(username, password)
        salt = secrets.token_bytes(16)
        with self._hash_slots:
            password_hash = _password_hash(password, salt)
        try:
            with self.connect() as db:
                row = db.execute('INSERT INTO welcome_users(username,salt,password_hash,created_at,registered_user_id) VALUES(?,?,?,?,?)',
                                 (username, salt, password_hash, self.clock(),uuid.uuid4().hex))
                user_id = row.lastrowid
        except sqlite3.IntegrityError:
            raise AuthError('这个用户名已被使用，请登录或换一个用户名。', 409) from None
        return self.new_session(user_id, old_token)

    def login(self, username, password, old_token):
        username, password = self.validate_credentials(username, password)
        now = self.clock()
        with self.connect() as db:
            failure = db.execute('SELECT * FROM welcome_login_failures WHERE username=?', (username,)).fetchone()
            if failure and failure['count'] >= 8 and failure['until_at'] > now:
                raise AuthError('尝试次数较多，请一分钟后再试。', 429)
            row = db.execute("SELECT * FROM welcome_users WHERE username=? AND identity_kind='local'", (username,)).fetchone()
        salt = row['salt'] if row else self._dummy_salt
        expected = row['password_hash'] if row else self._dummy_hash
        with self._hash_slots:
            matches = hmac.compare_digest(_password_hash(password, salt), expected)
        if not row or not matches:
            with self.connect() as db:
                db.execute('''INSERT INTO welcome_login_failures VALUES(?,1,?)
                    ON CONFLICT(username) DO UPDATE SET
                    count=CASE WHEN until_at<=? THEN 1 ELSE count+1 END, until_at=?''',
                           (username, now + 60, now, now + 60))
            raise AuthError('用户名或密码不正确。', 401)
        with self.connect() as db:
            db.execute('DELETE FROM welcome_login_failures WHERE username=?', (username,))
        return self.new_session(row['id'], old_token)

    @staticmethod
    def _sms_context(session, purpose):
        return {'flow': purpose, 'user_id': session['user_id'], 'version': 1}

    def _sms_session(self, token, *, bind=False):
        session = self.session(token)
        if not session or (bind and (session['user_id'] is None or session['identity_kind'] != 'local')):
            raise AuthError('请先使用原密码登录，再验证手机号。', 401)
        if not bind and session['user_id'] is not None:
            raise AuthError('请先退出当前账户，再进行注册或短信登录。', 409)
        return session

    def _verify_current_password(self, session, password):
        with self.connect() as db:
            row = db.execute("SELECT * FROM welcome_users WHERE id=? AND identity_kind='local'",
                             (session['user_id'],)).fetchone()
        if not row:
            raise AuthError('请先登录普通账户。', 401)
        # 复用原登录频控和密码验证；不在二次确认中签发新会话。
        username, password = self.validate_credentials(row['username'], password)
        now = self.clock()
        with self.connect() as db:
            failure = db.execute('SELECT * FROM welcome_login_failures WHERE username=?', (username,)).fetchone()
            if failure and failure['count'] >= 8 and failure['until_at'] > now:
                raise AuthError('尝试次数较多，请一分钟后再试。', 429)
        with self._hash_slots:
            correct = hmac.compare_digest(_password_hash(password, row['salt']), row['password_hash'])
        if not correct:
            with self.connect() as db:
                db.execute('''INSERT INTO welcome_login_failures VALUES(?,1,?)
                    ON CONFLICT(username) DO UPDATE SET
                    count=CASE WHEN until_at<=? THEN 1 ELSE count+1 END,until_at=?''',
                    (username, now+60, now, now+60))
            raise AuthError('原密码不正确。', 401)

    def send_sms(self, token, purpose, phone, *, password=None, client_ip='127.0.0.1'):
        phone = self.validate_phone(phone)
        if purpose not in ('register', 'login', 'bind'):
            raise AuthError('短信用途无效。')
        session = self._sms_session(token, bind=purpose == 'bind')
        if purpose == 'bind':
            self._verify_current_password(session, password)
            binding = self.sms.binding_for_user(session['user_id'])
            if binding:
                raise AuthError('当前账户已验证手机号；更换手机号尚未开放。', 409)
        return self.sms.send(self.sms.principal(session), purpose, phone, self._sms_context(session, purpose),
                             client_ip=client_ip)

    def sms_register(self, phone, password, challenge, code, old_token, *, password_confirmation=None):
        phone = self.validate_phone(phone)
        _, password = self.validate_credentials(phone, password)
        if not isinstance(password_confirmation,str):
            raise AuthError('请再次输入密码。',400,'PASSWORD_CONFIRMATION_REQUIRED')
        try:
            matches = hmac.compare_digest(password.encode('utf-8'),password_confirmation.encode('utf-8'))
        except UnicodeError:
            matches = False
        if not matches:
            raise AuthError('两次输入的密码不一致。',400,'PASSWORD_CONFIRMATION_MISMATCH')
        session = self._sms_session(old_token)
        proof = self.sms.verify(challenge, self.sms.principal(session), 'register', phone, code,
                                self._sms_context(session, 'register'))
        salt = secrets.token_bytes(16)
        with self._hash_slots:
            password_hash = _password_hash(password, salt)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assert_current(old_token, None, db=db)
            if db.execute('SELECT 1 FROM welcome_users WHERE username=?', (phone,)).fetchone():
                raise AuthError('这个手机号已有账户，请先使用原密码登录，再验证手机号。', 409)
            self.sms.consume(db, proof)
            row = db.execute('INSERT INTO welcome_users(username,salt,password_hash,created_at,registered_user_id) VALUES(?,?,?,?,?)',
                             (phone, salt, password_hash, self.clock(),uuid.uuid4().hex))
            self.sms.bind_phone(db, row.lastrowid, proof)
            token = self._new_session_in(db, row.lastrowid, old_token)
        return token, self.session(token)

    def sms_login(self, phone, challenge, code, old_token):
        phone = self.validate_phone(phone)
        session = self._sms_session(old_token)
        proof = self.sms.verify(challenge, self.sms.principal(session), 'login', phone, code,
                                self._sms_context(session, 'login'))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assert_current(old_token, None, db=db)
            binding = db.execute('SELECT * FROM sms_phone_bindings WHERE phone=?', (phone,)).fetchone()
            if not binding:
                raise AuthError('此手机号尚未注册或验证，请先创建账户；已有账户请使用原密码登录。', 409)
            self.sms.consume(db, proof)
            token = self._new_session_in(db, binding['user_id'], old_token)
        return token, self.session(token)

    def sms_bind(self, phone, password, challenge, code, old_token, *, before_rotate=None):
        phone = self.validate_phone(phone)
        session = self._sms_session(old_token, bind=True)
        self._verify_current_password(session, password)
        proof = self.sms.verify(challenge, self.sms.principal(session), 'bind', phone, code,
                                self._sms_context(session, 'bind'))
        if before_rotate is not None:
            before_rotate()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assert_current(old_token, session['user_id'], db=db)
            self.sms.consume(db, proof)
            self.sms.bind_phone(db, session['user_id'], proof)
            token = self._new_session_in(db, session['user_id'], old_token)
            if self.allow_legacy_experience:
                db.execute('DELETE FROM welcome_sessions WHERE user_id=? AND token_hash!=?',
                           (session['user_id'], _digest(token)))
        return token, self.session(token)

    def logout(self, token, expected_user_id=...):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if expected_user_id is not ...:
                self.assert_current(token, expected_user_id, db=db)
            identity = db.execute('''SELECT u.id,u.identity_kind FROM welcome_sessions s
                JOIN welcome_users u ON u.id=s.user_id WHERE s.token_hash=?''', (_digest(token),)).fetchone()
            db.execute('DELETE FROM welcome_sessions WHERE token_hash=?', (_digest(token),))
            if identity and self.allow_legacy_experience:
                # 同一欢迎主体的会话共用业务 jar，退出须撤销全部相关会话。
                db.execute('DELETE FROM welcome_upstream_cookies WHERE user_id=?', (identity['id'],))
                db.execute('DELETE FROM welcome_sessions WHERE user_id=?', (identity['id'],))
                if identity['identity_kind'] == 'experience':
                    db.execute('DELETE FROM welcome_users WHERE id=?', (identity['id'],))

    def experience_session(self, account, bank_cookie, old_token):
        """仅显式旧隔离模式可建立公共体验身份；正式注册入口不使用此路径。"""
        if not self.allow_legacy_experience:
            raise AuthError('请先使用手机号注册并登录。',401,'REGISTRATION_REQUIRED')
        if account not in EXPERIENCE_ACCOUNTS:
            raise AuthError('请选择列表中的测试账户。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.assert_current(old_token,None,db=db)
            row = db.execute('''INSERT INTO welcome_users
                (username,salt,password_hash,created_at,identity_kind,experience_account)
                VALUES(?,?,?,?, 'experience',?)''',
                ('~experience-'+secrets.token_urlsafe(24),secrets.token_bytes(16),secrets.token_hex(64),self.clock(),account))
            user_id = row.lastrowid
            db.execute('INSERT INTO welcome_upstream_cookies VALUES(?,?,?)',(user_id,'qz_bank_user',bank_cookie))
            token = self._new_session_in(db,user_id,old_token)
        return token,self.session(token)

    def retire_legacy_experience(self, token):
        """撤销旧匿名体验会话，保留旧身份记录及业务历史，不认领任何旧工作区。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT s.user_id FROM welcome_sessions s JOIN welcome_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND u.identity_kind='experience' ''',(_digest(token),)).fetchone()
            if not row:
                return
            db.execute('DELETE FROM welcome_sessions WHERE user_id=?',(row['user_id'],))
            db.execute('DELETE FROM welcome_upstream_cookies WHERE user_id=?',(row['user_id'],))

    def user_lock(self, user_id):
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.RLock())

    def upstream_cookie(self, user_id, *, session_token=None):
        with self.connect() as db:
            return self._db_cookie(db,user_id,session_token)

    def _db_cookie(self, db, user_id, session_token=None):
        if self.allow_legacy_experience:
            rows = db.execute('SELECT name,value FROM welcome_upstream_cookies WHERE user_id=? ORDER BY name',(user_id,)).fetchall()
        else:
            if not session_token:
                raise AuthError('业务凭证需要当前登录会话。',401,'WELCOME_SESSION_REQUIRED')
            current = db.execute('SELECT 1 FROM welcome_sessions WHERE token_hash=? AND user_id=? AND expires_at>?',
                                 (_digest(session_token),user_id,self.clock())).fetchone()
            if not current:
                raise AuthError('Navren 登录已失效。',401,'WELCOME_SESSION_REVOKED')
            rows = db.execute('SELECT name,value FROM welcome_session_upstream_cookies WHERE token_hash=? ORDER BY name',
                              (_digest(session_token),)).fetchall()
        cookie = SimpleCookie()
        for row in rows:
            cookie[row['name']] = row['value']
        return '; '.join(m.OutputString() for m in cookie.values())

    def assert_current(self, token, user_id, expected_cookie=None, db=None):
        if db is None:
            with self.connect() as connection:
                return self.assert_current(token, user_id, expected_cookie, connection)
        row = db.execute('SELECT * FROM welcome_sessions WHERE token_hash=? AND expires_at>?',
                         (_digest(token), self.clock())).fetchone()
        if not row or row['user_id'] != user_id:
            raise AuthError('Navren 登录已失效。', 401, 'WELCOME_SESSION_REVOKED')
        if expected_cookie is not None and (self._db_cookie(db,user_id,token) != expected_cookie or self.business_revoked(token,db=db)):
            raise AuthError('当前业务账户已变化，请重新读取。', 409, 'WELCOME_BUSINESS_CHANGED')
        return dict(row)

    def business_revoked(self, token, *, db=None):
        if db is None:
            with self.connect() as connection:
                return self.business_revoked(token,db=connection)
        row = db.execute('SELECT revoked FROM welcome_session_business_state WHERE token_hash=?',(_digest(token),)).fetchone()
        return bool(row and row['revoked'])

    def mark_business_revoked(self, token, user_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assert_current(token,user_id,db=db)
            db.execute('INSERT OR REPLACE INTO welcome_session_business_state VALUES(?,1)',(_digest(token),))

    def update_upstream_cookies(self, user_id, values, *, session_token=None, expected_cookie=None):
        if not self.allow_legacy_experience and session_token is None:
            raise AuthError('业务凭证需要当前登录会话。',401,'WELCOME_SESSION_REQUIRED')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if session_token is not None:
                self.assert_current(session_token, user_id, expected_cookie, db)
            for value in values:
                cookie = SimpleCookie()
                try:
                    cookie.load(value)
                except Exception:
                    continue
                # 此固定上游只需要银行身份 Cookie；不接收任意域或其他应用身份。
                for name, morsel in cookie.items():
                    if name != 'qz_bank_user' or morsel['domain'] not in ('', '127.0.0.1', 'localhost'):
                        continue
                    if not morsel.value or morsel['max-age'] == '0':
                        table,key,identifier = ('welcome_upstream_cookies','user_id',user_id) if self.allow_legacy_experience else (
                            'welcome_session_upstream_cookies','token_hash',_digest(session_token))
                        db.execute(f'DELETE FROM {table} WHERE {key}=? AND name=?',(identifier,name))
                    else:
                        if not re.fullmatch('[A-Za-z0-9_-]{20,200}',morsel.value) and not self.allow_legacy_experience:
                            raise AuthError('业务凭证格式无法核对。',502,'PRIVATE_EXPERIENCE_INVALID')
                        table,identifier = ('welcome_upstream_cookies',user_id) if self.allow_legacy_experience else (
                            'welcome_session_upstream_cookies',_digest(session_token))
                        db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?)',(identifier,name,morsel.value))
            return self._db_cookie(db,user_id,session_token)


class RegisteredExperienceAdapter:
    """同进程边界：仅从当前已验证welcome会话解析owner，永不公开业务Cookie。"""
    def __init__(self, auth, provider):
        self.auth, self.provider = auth, provider
        if provider is not None:
            bank_clock = getattr(getattr(provider,'bank',None),'clock',auth.clock)
            if bank_clock is not auth.clock:
                raise ValueError('welcome_and_experience_clock_must_match')
            for method in ('catalog','resume','select_registered','revoke_subject','owner_for_workspace'):
                if not callable(getattr(provider,method,None)):
                    raise TypeError('trusted_experience_provider_required')

    def _call(self, method, *args, **kwargs):
        if self.provider is None:
            raise AuthError('个人体验空间尚未接通，请稍后再试。',503,'PRIVATE_EXPERIENCE_NOT_CONFIGURED')
        try:
            return getattr(self.provider,method)(*args,**kwargs)
        except BankError as error:
            raise AuthError('个人体验空间暂时无法核对，请重新读取或登录。',error.status,error.code) from None
        except Exception:
            raise AuthError('个人体验空间暂不可用，请稍后再试。',503,'PRIVATE_EXPERIENCE_UNAVAILABLE') from None

    def _session(self, token, *, verified=True):
        session = self.auth.session(token)
        if verified:
            self.auth.registered_owner(token)
        elif not session or session['user_id'] is None or session.get('identity_kind') != 'local':
            raise AuthError('请先登录。',401,'REGISTRATION_REQUIRED')
        self.auth.assert_current(token,session['user_id'])
        return session

    @staticmethod
    def _invalid():
        return AuthError('个人体验空间返回资料无法核对。',502,'PRIVATE_EXPERIENCE_INVALID')

    def _project(self, session, lease):
        if not isinstance(lease,dict):
            raise self._invalid()
        owner, subject = session['registered_user_id'], self.auth.auth_subject(session)
        expiry = lease.get('auth_expires_at')
        if (lease.get('registered_user_id') != owner or lease.get('auth_subject') != subject
                or type(expiry) not in (int,float) or not math.isfinite(expiry)
                or expiry != session['expires_at'] or expiry <= self.auth.clock()):
            raise self._invalid()
        if lease.get('selection_required') is True:
            if lease.get('account_id') is not None or lease.get('workspace_id') is not None or lease.get('upstream_cookies') != {}:
                raise self._invalid()
            entries = lease.get('accounts')
            if (not isinstance(entries,list) or len(entries)!=len(EXPERIENCE_ACCOUNTS)
                    or {e.get('account_id') for e in entries if isinstance(e,dict)} != set(EXPERIENCE_ACCOUNTS)):
                raise self._invalid()
            return None
        cookie = lease.get('upstream_cookies')
        expiry = lease.get('lease_expires_at')
        if (lease.get('selection_required') is not False or lease.get('account_id') not in EXPERIENCE_ACCOUNTS
                or not isinstance(lease.get('workspace_id'),str) or re.fullmatch('[0-9a-f]{32}',lease['workspace_id']) is None
                or type(expiry) not in (int,float) or not math.isfinite(expiry)
                or not self.auth.clock() < expiry <= session['expires_at']
                or any(type(lease.get(k)) is not int or lease[k]<0 for k in ('session_generation','privacy_revision','personal_revision'))
                or not isinstance(cookie,dict) or set(cookie)!={'qz_bank_user'}
                or not isinstance(cookie['qz_bank_user'],str) or re.fullmatch('[A-Za-z0-9_-]{20,200}',cookie['qz_bank_user']) is None):
            raise self._invalid()
        if self._call('owner_for_workspace',lease['workspace_id']) != owner:
            raise self._invalid()
        return {'environment':'simulation','registered_user_id':owner,'auth_subject':subject,
            'account_id':lease['account_id'],'workspace_id':lease['workspace_id'],
            'display_name':EXPERIENCE_ACCOUNTS[lease['account_id']],
            'privacy_revision':lease['privacy_revision'],'personal_revision':lease['personal_revision'],
            'generation':lease['session_generation'],'session_generation':lease['session_generation'],
            'auth_expires_at':lease['auth_expires_at'],'lease_expires_at':lease['lease_expires_at']}

    def _revoke(self, session):
        result = self._call('revoke_subject',session['registered_user_id'],self.auth.auth_subject(session))
        if (not isinstance(result,dict) or type(result.get('revoked_session_count')) is not int
                or result['revoked_session_count']<0 or result.get('persistent_instances_retained') is not True):
            raise AuthError('业务退出尚未核实，请重试。',502,'BUSINESS_LOGOUT_UNVERIFIED')
        return result

    def _store(self, token, session, lease, previous, *, may_issue=False):
        try:
            identity = self._project(session,lease)
            self.auth.assert_current(token,session['user_id'],previous)
            if identity:
                if WelcomeHandler._bank_cookie_value(previous) != lease['upstream_cookies']['qz_bank_user']:
                    self.auth.update_upstream_cookies(session['user_id'],
                        ['qz_bank_user='+lease['upstream_cookies']['qz_bank_user']],
                        session_token=token,expected_cookie=previous)
            elif previous:
                raise self._invalid()
            return identity
        except AuthError:
            # 已签出的链不能因welcome CAS或返回合同失败成为未登记的存活令牌。
            if may_issue:
                self._revoke(session)
                try:
                    self.auth.update_upstream_cookies(session['user_id'],['qz_bank_user=; Max-Age=0'],
                        session_token=token,expected_cookie=previous)
                    self.auth.mark_business_revoked(token,session['user_id'])
                except AuthError:
                    pass
            raise

    def resume(self, token):
        session = self._session(token)
        with self.auth.user_lock(self.auth.business_lock_key(session)):
            session = self._session(token)
            if self.auth.business_revoked(token):
                raise AuthError('当前业务会话已退出，请重新登录。',409,'WELCOME_BUSINESS_REVOKED')
            previous = self.auth.upstream_cookie(session['user_id'],session_token=token)
            bank_token = WelcomeHandler._bank_cookie_value(previous) if previous else None
            recovered = False
            try:
                lease = self._call('resume',session['registered_user_id'],self.auth.auth_subject(session),
                    auth_expires_at=session['expires_at'],bank_session_token=bank_token)
            except AuthError as error:
                if bank_token is None or error.code != 'SESSION_CHANGED':
                    raise
                lease = self._call('recover_committed_selection',session['registered_user_id'],self.auth.auth_subject(session),
                    auth_expires_at=session['expires_at'],bank_session_token=bank_token)
                recovered = True
            return self._store(token,session,lease,previous,may_issue=bank_token is None or recovered)

    def select(self, token, persona):
        session = self._session(token)
        with self.auth.user_lock(self.auth.business_lock_key(session)):
            session = self._session(token)
            if self.auth.business_revoked(token):
                raise AuthError('当前业务会话已退出，请重新登录。',409,'WELCOME_BUSINESS_REVOKED')
            previous = self.auth.upstream_cookie(session['user_id'],session_token=token)
            current = self.resume(token) if previous else None
            previous = self.auth.upstream_cookie(session['user_id'],session_token=token)
            lease = self._call('select_registered',session['registered_user_id'],persona,self.auth.auth_subject(session),
                auth_expires_at=session['expires_at'],expected_workspace_id=current['workspace_id'] if current else None)
            return self._store(token,session,lease,previous,may_issue=True)

    def revoke(self, token):
        session = self._session(token,verified=False)
        with self.auth.user_lock(self.auth.business_lock_key(session)):
            session = self._session(token,verified=False)
            previous = self.auth.upstream_cookie(session['user_id'],session_token=token)
            if self.provider is not None:
                result = self._revoke(session)
            elif previous:
                raise AuthError('业务退出暂不可用，请稍后重试。',503,'PRIVATE_EXPERIENCE_NOT_CONFIGURED')
            else:
                result = {'revoked_session_count':0,'persistent_instances_retained':True}
            self.auth.mark_business_revoked(token,session['user_id'])
            return result

    def adopt_headers(self, token, values, previous):
        """银行切号回复先由同进程provider验证归属、会话和TTL，再写本会话jar。"""
        session = self._session(token)
        parsed = []
        for value in values:
            jar = SimpleCookie()
            try: jar.load(value)
            except Exception: continue
            if 'qz_bank_user' not in jar: continue
            morsel = jar['qz_bank_user']
            if morsel['domain'] not in ('','127.0.0.1','localhost'): continue
            parsed.append('' if morsel['max-age']=='0' else morsel.value)
        if not parsed:
            self.auth.assert_current(token,session['user_id'],previous)
            return previous
        if len(set(parsed))!=1:
            raise self._invalid()
        if not parsed[0]:
            # 直接业务logout同样撤完整subject；不得在下一次session轮询中自动复活。
            self._revoke(session)
            result = self.auth.update_upstream_cookies(session['user_id'],['qz_bank_user=; Max-Age=0'],
                session_token=token,expected_cookie=previous)
            self.auth.mark_business_revoked(token,session['user_id'])
            return result
        lease = self._call('resume',session['registered_user_id'],self.auth.auth_subject(session),
            auth_expires_at=session['expires_at'],bank_session_token=parsed[0])
        self._store(token,session,lease,previous,may_issue=True)
        return self.auth.upstream_cookie(session['user_id'],session_token=token)


class WelcomeHandler(server.SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(args[2].root), **kwargs)

    def log_message(self, *_):
        # 不记录查询、账号、密码、令牌或响应内容。
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        super().end_headers()

    def _json(self, status, data, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _origin_check(self):
        authorities = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.client_address[0] not in ('127.0.0.1', '::1') or self.headers.get('Host') not in authorities:
            raise AuthError('仅支持本机访问。', 403)
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + authority for authority in authorities}:
            raise AuthError('请从当前 Navren 页面发起操作。', 403)
        if self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
            raise AuthError('请从当前 Navren 页面发起操作。', 403)

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return ''
        return cookie[COOKIE].value if COOKIE in cookie else ''

    @staticmethod
    def _cookie(token, session):
        ttl = SESSION_TTL if session['user_id'] is not None else ANONYMOUS_TTL
        return f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={ttl}'

    def _body(self, limit=MAX_BODY):
        if self.headers.get('Transfer-Encoding'):
            raise AuthError('请求格式不支持。', 400)
        size = self.headers.get('Content-Length', '0')
        if not re.fullmatch(r'\d+', size) or int(size) > limit:
            raise AuthError('请求内容过大。', 413)
        body = self.rfile.read(int(size))
        if len(body) != int(size):
            raise AuthError('请求内容不完整。', 400)
        return body

    def _auth(self, path):
        auth = self.server.auth
        token = self._token()
        session = auth.session(token)
        if path == AUTH_PREFIX + 'session' and self.command == 'GET':
            cookie = None
            if session and session.get('identity_kind') == 'experience' and not auth.allow_legacy_experience:
                auth.retire_legacy_experience(token)
                session = None
            if not session:
                token, session = auth.new_session()
                cookie = self._cookie(token, session)
            identity, status = self._current_business(session,token)
            self._json(200, auth.public_session(session, identity, status), cookie)
            return
        if self.command != 'POST' or path not in {AUTH_PREFIX + x for x in
                ('register', 'login', 'logout', 'experience', 'sms/send', 'sms/login', 'sms/bind')}:
            raise AuthError('这个登录接口不存在。', 404)
        if not session or not hmac.compare_digest(self.headers.get('X-Welcome-CSRF', ''), session['csrf']):
            raise AuthError('登录页面已过期，请刷新后再试。', 403)
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise AuthError('请使用正确的登录请求格式。', 415)
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            data = json.loads(self._body(4096), object_pairs_hook=unique)
        except (ValueError, UnicodeError):
            raise AuthError('登录请求无法读取。') from None
        if not isinstance(data, dict):
            raise AuthError('登录请求格式不正确。')
        if path.endswith('/sms/send'):
            expected = {'phone', 'purpose', 'password'} if data.get('purpose') == 'bind' else {'phone', 'purpose'}
            if set(data) != expected:
                raise AuthError('短信发送请求格式不正确。')
            with auth.user_lock(session['token_hash']):
                auth.assert_current(token, session['user_id'])
                result = auth.send_sms(token, data['purpose'], data['phone'], password=data.get('password'),
                                       client_ip=self.client_address[0])
                auth.assert_current(token, session['user_id'])
            self._json(200, result)
            return
        sms_action = ('register' if path.endswith('/register') and not auth.allow_unverified_registration else
                      'login' if path.endswith('/sms/login') else 'bind' if path.endswith('/sms/bind') else None)
        if sms_action:
            auth.sms.require_configured()
            expected = {'phone', 'challenge_id', 'verification_code'}
            if sms_action in ('register', 'bind'):
                expected.add('password')
            if sms_action == 'register':
                expected.add('password_confirmation')
            if set(data) != expected:
                raise AuthError('请填写手机号和短信验证码。', 400, 'SMS_REQUIRED')
            with auth.user_lock(session['token_hash']):
                auth.assert_current(token, session['user_id'])
                if sms_action == 'register':
                    token, session = auth.sms_register(data['phone'], data['password'], data['challenge_id'], data['verification_code'], token,
                        password_confirmation=data['password_confirmation'])
                elif sms_action == 'login':
                    token, session = auth.sms_login(data['phone'], data['challenge_id'], data['verification_code'], token)
                else:
                    token, session = auth.sms_bind(data['phone'], data['password'], data['challenge_id'], data['verification_code'], token,
                        before_rotate=None if auth.allow_legacy_experience else lambda:self.server.experiences.revoke(token))
            identity, status = self._current_business(session,token)
            self._json(201 if sms_action == 'register' else 200, auth.public_session(session,identity,status), self._cookie(token, session))
            return
        if path.endswith('/logout'):
            if data:
                raise AuthError('退出请求格式不正确。')
            self._complete_logout(session, token)
            self._json(200, {'authenticated': False, 'redirect': '/?view=experience' if auth.allow_legacy_experience else '/?view=login',
                            'scope': 'welcome_subject', 'business_requests_revoked': True},
                       f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            return
        if path.endswith('/experience'):
            if set(data) != {'user_id'} or not isinstance(data['user_id'], str) or data['user_id'] not in EXPERIENCE_ACCOUNTS:
                raise AuthError('请选择列表中的测试账户。')
            if not auth.allow_legacy_experience:
                identity = self.server.experiences.select(token,data['user_id'])
                self._json(200,auth.public_session(session,identity,'verified'))
                return
            if session['user_id'] is not None:
                with auth.user_lock(auth.business_lock_key(session)):
                    auth.assert_current(token,session['user_id'])
                    previous = auth.upstream_cookie(session['user_id'],session_token=token)
                    if not previous:
                        raise AuthError('当前业务账户尚未连接，请先完成登录。',409)
                    bank_cookie,selected = self._select_experience(data['user_id'],previous)
                    auth.update_upstream_cookies(session['user_id'],['qz_bank_user='+bank_cookie],
                        session_token=token,expected_cookie=previous)
            else:
                bank_cookie,selected = self._select_experience(data['user_id'])
                try:
                    token,session = auth.experience_session(data['user_id'],bank_cookie,token)
                except AuthError:
                    self._revoke_business_cookie(bank_cookie)
                    raise
            self._json(200,auth.public_session(session,self._project_identity(selected),'verified'),self._cookie(token,session))
            return
        if session['user_id'] is not None and not auth.allow_legacy_experience:
            raise AuthError('请先退出当前账户，再登录其他账户。',409,'WELCOME_LOGOUT_REQUIRED')
        using_phone = set(data) == {'phone', 'password'}
        if using_phone:
            username = auth.validate_phone(data['phone'])
        elif set(data) == {'username', 'password'}:
            username = data['username']
        else:
            raise AuthError('请输入手机号和密码。')
        action = auth.register if path.endswith('/register') else auth.login
        try:
            with auth.user_lock(auth.business_lock_key(session)):
                auth.assert_current(token, session['user_id'])
                token, session = action(username, data['password'], token)
        except AuthError as error:
            if using_phone and error.status == 401:
                raise AuthError('手机号或密码不正确。', 401) from None
            if using_phone and error.status == 409:
                raise AuthError('这个手机号已被使用，请登录。', 409) from None
            raise
        identity, status = self._current_business(session,token)
        self._json(201 if path.endswith('/register') else 200, auth.public_session(session,identity,status), self._cookie(token, session))

    def _business_request(self, method, path, payload=None, *, cookie=None, csrf=None, timeout=5):
        """固定同源上游、有界 JSON，不在只读身份核验中建立业务账户。"""
        authority = f'127.0.0.1:{self.server.upstream_port}'
        headers = {'Host': authority, 'Origin': 'http://' + authority,
                   'Sec-Fetch-Site': 'same-origin', 'Accept': 'application/json'}
        if cookie:
            headers['Cookie'] = 'qz_bank_user=' + cookie
        if csrf:
            headers['X-QZ-CSRF'] = csrf
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        connection = client.HTTPConnection('127.0.0.1', self.server.upstream_port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_headers = response.getheaders()
            raw = response.read(65537)
            if (len(raw) > 65536
                    or response.getheader('Content-Type', '').split(';')[0].lower() != 'application/json'):
                raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
            return response.status, result, response_headers
        except (OSError, client.HTTPException, ValueError, UnicodeError):
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502) from None
        finally:
            connection.close()

    def _experience_request(self, method, path, payload=None, *, cookie=None, csrf=None):
        status, result, headers = self._business_request(method, path, payload, cookie=cookie, csrf=csrf)
        if status != 200:
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        return result, headers

    @staticmethod
    def _bank_cookie_value(cookie):
        jar = SimpleCookie()
        jar.load(cookie)
        return jar['qz_bank_user'].value if 'qz_bank_user' in jar else None

    @staticmethod
    def _revoked_business(status, data):
        error = data.get('error')
        code = error.get('code') if isinstance(error, dict) else None
        return ((status == 409 and code == 'SESSION_CHANGED')
                or (status == 401 and code == 'UNAUTHENTICATED'))

    @staticmethod
    def _project_identity(data):
        identity = data.get('demo_identity')
        account = data.get('account_id')
        if (not WelcomeHandler._experience_identity(data) or account not in EXPERIENCE_ACCOUNTS
                or not isinstance(identity, dict) or identity.get('user_id') != account
                or identity.get('mode') != 'demo_account'):
            return None
        result = {key: data[key] for key in ('environment', 'account_id', 'workspace_id', 'privacy_revision')}
        result['display_name'] = EXPERIENCE_ACCOUNTS[account]
        for key in ('personal_revision',):
            if type(data.get(key)) is int and data[key] >= 0:
                result[key] = data[key]
        if type(identity.get('generation')) is int and identity['generation'] >= 0:
            result['generation'] = identity['generation']
        return result

    def _current_business(self, session, token):
        if session['user_id'] is None:
            return None, 'not_connected'
        auth = self.server.auth
        if not auth.allow_legacy_experience:
            if not session.get('phone_verified'):
                return None,'not_connected'
            if self.server.experiences.provider is None:
                return None,'not_connected'
            try:
                identity = self.server.experiences.resume(token)
                return identity,'verified' if identity else 'not_connected'
            except AuthError as error:
                auth.assert_current(token,session['user_id'])
                revoked = error.code in ('SESSION_CHANGED','WORKSPACE_CHANGED','WELCOME_BUSINESS_REVOKED','WELCOME_SESSION_REVOKED',
                    'WELCOME_SESSION_EXPIRED','UNAUTHENTICATED')
                return None,'revoked' if revoked else 'unavailable'
        previous = auth.upstream_cookie(session['user_id'],session_token=token)
        if not previous:
            return None, 'not_connected'
        try:
            status, data, _ = self._business_request('GET', '/api/finance/session',
                                                   cookie=self._bank_cookie_value(previous), timeout=3)
        except AuthError:
            auth.assert_current(token, session['user_id'], previous)
            return None, 'unavailable'
        auth.assert_current(token, session['user_id'], previous)
        identity = self._project_identity(data) if status == 200 else None
        return identity, ('verified' if identity else 'revoked' if self._revoked_business(status, data) else 'unavailable')

    def _revoke_business_cookie(self, bank_cookie):
        def request(method, path, payload=None, **headers):
            try:
                return self._business_request(method, path, payload, **headers)
            except AuthError:
                raise AuthError('业务退出未完成，请稍后重试。', 502, 'BUSINESS_LOGOUT_UNAVAILABLE') from None
        status, identity, _ = request('GET', '/api/finance/session', cookie=bank_cookie)
        # 精确旧令牌已撤销允许恢复；网络异常/任意409均不能作为成功证明。
        if self._revoked_business(status, identity):
            return
        if status != 200 or not self._experience_identity(identity):
            raise AuthError('业务退出未完成，请稍后重试。', 502, 'BUSINESS_LOGOUT_UNAVAILABLE')
        status, result, headers = request('POST', '/api/finance/session/logout', {},
                                          cookie=bank_cookie, csrf=identity['csrf'])
        deleted = False
        for key, value in headers:
            if key.lower() == 'set-cookie':
                jar = SimpleCookie(); jar.load(value)
                if ('qz_bank_user' in jar and not jar['qz_bank_user'].value
                        and jar['qz_bank_user']['max-age'] == '0'
                        and jar['qz_bank_user']['domain'] in ('', '127.0.0.1', 'localhost')
                        and jar['qz_bank_user']['path'] == '/'):
                    deleted = True
        if (status != 200 or not deleted or any(result.get(key) is not True for key in
                ('logged_out', 'old_requests_revoked', 'public_session_logout_required'))):
            raise AuthError('业务退出未完成，请稍后重试。', 502, 'BUSINESS_LOGOUT_UNAVAILABLE')
        status, result, _ = request('GET', '/api/finance/session', cookie=bank_cookie)
        if not self._revoked_business(status, result):
            raise AuthError('旧业务请求尚未撤销，请重试。', 502, 'BUSINESS_LOGOUT_UNVERIFIED')

    def _complete_logout(self, session, token):
        auth = self.server.auth
        # 与业务切号/代理发送互斥，等待响应的普通业务请求不占此锁。
        lock = auth.user_lock(auth.business_lock_key(session))
        with lock:
            auth.assert_current(token, session['user_id'])
            if not auth.allow_legacy_experience:
                if session['user_id'] is not None:
                    self.server.experiences.revoke(token)
                auth.logout(token,session['user_id'])
                return
            previous = auth.upstream_cookie(session['user_id'],session_token=token) if session['user_id'] is not None else ''
            if previous:
                self._revoke_business_cookie(self._bank_cookie_value(previous))
                auth.assert_current(token, session['user_id'], previous)
            # token、预期主体与删除在同一事务重验，不能在并发登录旋转后假称成功。
            auth.logout(token, session['user_id'])

    @staticmethod
    def _experience_bank_cookie(headers):
        values = []
        for key, value in headers:
            if key.lower() != 'set-cookie':
                continue
            cookie = SimpleCookie()
            try:
                cookie.load(value)
            except Exception:
                continue
            if 'qz_bank_user' not in cookie:
                continue
            item = cookie['qz_bank_user']
            if (item['domain'] in ('', '127.0.0.1', 'localhost') and item['max-age'] != '0'
                    and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', item.value)):
                values.append(item.value)
        if len(set(values)) != 1:
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        return values[0]

    @staticmethod
    def _experience_identity(data):
        return (data.get('environment') == 'simulation'
                and isinstance(data.get('workspace_id'), str)
                and re.fullmatch(r'[a-f0-9]{32}', data['workspace_id'])
                and isinstance(data.get('csrf'), str) and 16 <= len(data['csrf']) <= 256
                and type(data.get('privacy_revision')) is int and data['privacy_revision'] >= 0)

    def _select_experience(self, account, existing_cookie=None):
        catalog, _ = self._experience_request('GET', '/api/finance/demo/accounts')
        if (catalog.get('environment') != 'simulation' or catalog.get('source') != 'navren_synthetic'
                or catalog.get('is_real_bank_data') is not False or not isinstance(catalog.get('accounts'), list)):
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        entries = [entry for entry in catalog['accounts']
                   if isinstance(entry, dict) and entry.get('user_id') == account]
        if (len(entries) != 1 or entries[0].get('source') != 'navren_synthetic'
                or not isinstance(entries[0].get('workspace_id'), str)
                or not re.fullmatch(r'[a-f0-9]{32}', entries[0]['workspace_id'])
                or entries[0].get('status', 'ready') != 'ready'):
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        if existing_cookie:
            temporary_cookie = self._bank_cookie_value(existing_cookie)
            initial, _ = self._experience_request('GET', '/api/finance/session', cookie=temporary_cookie)
        else:
            initial, headers = self._experience_request('POST', '/api/finance/session', {})
            temporary_cookie = self._experience_bank_cookie(headers)
        if not self._experience_identity(initial):
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        selected, headers = self._experience_request('POST', '/api/finance/demo/select', {
            'user_id': account, 'expected_workspace_id': initial['workspace_id'],
            'expected_privacy_revision': initial['privacy_revision'],
        }, cookie=temporary_cookie, csrf=initial['csrf'])
        identity = selected.get('demo_identity')
        if (not self._experience_identity(selected) or selected.get('account_id') != account
                or selected['workspace_id'] != entries[0]['workspace_id']
                or not isinstance(identity, dict) or identity.get('mode') != 'demo_account'
                or identity.get('user_id') != account):
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        selected_cookie = self._experience_bank_cookie(headers)
        if selected_cookie == temporary_cookie or selected['csrf'] == initial['csrf']:
            raise AuthError(EXPERIENCE_UNAVAILABLE, 502)
        return selected_cookie, selected

    def _guard(self, path):
        auth = self.server.auth
        token = self._token()
        session = auth.session(token)
        if auth.allow_legacy_experience and session and session['user_id'] is not None:
            return session
        try:
            auth.registered_owner(token)
            error = AuthError('请先选择本人的体验画像。',409,'EXPERIENCE_SELECTION_REQUIRED')
            redirect = '/?view=experience'
            if self.server.experiences.provider is not None and self.server.experiences.resume(token) is not None:
                return session
        except AuthError as denied:
            error = denied
            redirect = '/?view=bind' if denied.code=='PHONE_VERIFICATION_REQUIRED' else (
                '/?view=experience' if session and session.get('phone_verified') else '/?view=login')
        if auth.allow_legacy_experience:
            redirect='/'
        if path.startswith('/api/'):
            raise error
        self.send_response(303)
        self.send_header('Location', redirect)
        self.send_header('Content-Length', '0')
        self.end_headers()
        return None

    def _ensure_upstream_session(self, user_id, token):
        """本地认证通过后，独立连接已有银行演示身份；调用者持有用户锁。"""
        auth = self.server.auth
        if not auth.allow_legacy_experience:
            if self.server.experiences.resume(token) is None:
                raise AuthError('请先选择本人的体验画像。',409,'EXPERIENCE_SELECTION_REQUIRED')
            return
        if auth.upstream_cookie(user_id,session_token=token):
            return
        upstream = f'127.0.0.1:{self.server.upstream_port}'
        bootstrap = client.HTTPConnection('127.0.0.1', self.server.upstream_port, timeout=15)
        try:
            bootstrap.request('POST', '/api/finance/session', body=b'{}', headers={
                'Host': upstream, 'Origin': 'http://' + upstream,
                'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json',
                'Accept': 'application/json',
            })
            response = bootstrap.getresponse()
            # 有限读取，身份 Cookie 只存在本地用户对应的服务端 cookiejar。
            content = response.read(65537)
            if response.status != 200 or len(content) > 65536:
                raise AuthError('Navren 账户服务暂不可用，请稍后重试。', 502)
            auth.update_upstream_cookies(user_id,
                [value for key, value in response.getheaders() if key.lower() == 'set-cookie'],
                session_token=token, expected_cookie='')
            if not auth.upstream_cookie(user_id,session_token=token):
                raise AuthError('Navren 账户连接未完成，请稍后重试。', 502)
        except (OSError, client.HTTPException):
            raise AuthError('Navren 账户服务暂不可用，请稍后重试。', 502) from None
        finally:
            bootstrap.close()

    def _proxy(self, session=None):
        path = urlsplit(self.path).path
        if (session and not self.server.auth.allow_legacy_experience and self.command=='POST'
                and path=='/api/finance/session'):
            raise AuthError('请从当前注册账户恢复或选择体验画像。',409,'REGISTERED_SESSION_REQUIRED')
        body_limit = MAX_CSV_BODY if path in CSV_PREVIEW_PATHS else MAX_BODY
        media_upload = self.command=='POST' and self.path==CHAT_MEDIA_UPLOAD_PATH
        research_upload = self.command=='POST' and self.path==RESEARCH_UPLOAD_PATH
        if media_upload:
            body_limit=MAX_CHAT_MEDIA_BODY
        elif research_upload:
            body_limit=MAX_RESEARCH_UPLOAD_BODY
        body = self._body(body_limit) if self.command in ('POST', 'PUT', 'PATCH', 'DELETE') else None
        allowed_headers = FORWARDED_HEADERS | CHAT_MEDIA_UPLOAD_HEADERS if media_upload else FORWARDED_HEADERS
        if self.command=='POST' and self.path==CHAT_MEDIA_BIND_PATH:
            allowed_headers = FORWARDED_HEADERS | CHAT_MEDIA_BIND_HEADERS
        elif self.command=='POST' and self.path==ACCOUNT_STATE_WRITE_PATH:
            allowed_headers = FORWARDED_HEADERS | ACCOUNT_STATE_WRITE_HEADERS
        elif self.command=='POST' and (research_upload or RESEARCH_PRIVATE_WRITE.fullmatch(self.path)):
            allowed_headers = FORWARDED_HEADERS | ACCOUNT_STATE_WRITE_HEADERS
            if research_upload:allowed_headers |= RESEARCH_UPLOAD_HEADERS
        # 这些客户端字段只作上游前置条件；身份仍由当前welcome session的服务端银行Cookie确定。
        headers = {key: value for key, value in self.headers.items() if key.lower() in allowed_headers}
        upstream = f'127.0.0.1:{self.server.upstream_port}'
        headers.update(Host=upstream, Origin='http://' + upstream, **{'Sec-Fetch-Site': 'same-origin'})
        websocket = self.headers.get('Upgrade', '').lower() == 'websocket' and urlsplit(self.path).path == '/api/speech/ws'
        if websocket:
            headers.update(Connection='Upgrade', Upgrade='websocket')
        identity_mutation = session is not None and self.command == 'POST' and path in {
            '/api/finance/session', '/api/finance/session/reset',
            '/api/finance/demo/select', '/api/finance/session/logout'}
        connection = client.HTTPConnection('127.0.0.1', self.server.upstream_port, timeout=15 if identity_mutation else 45)
        if websocket:
            connection.response_class = _UnbufferedHTTPResponse
        response_started = False
        auth = self.server.auth
        token = self._token()
        expected_cookie = None
        response_headers = []
        business_logout_response = False
        lock = auth.user_lock(auth.business_lock_key(session)) if session else threading.Lock()
        def check_current():
            if session:
                auth.assert_current(token,session['user_id'],None if business_logout_response else expected_cookie)
        def adopt_headers():
            values = [value for key,value in response_headers if key.lower()=='set-cookie']
            if not auth.allow_legacy_experience:
                return self.server.experiences.adopt_headers(token,values,expected_cookie)
            return auth.update_upstream_cookies(session['user_id'],values,
                session_token=token,expected_cookie=expected_cookie)
        try:
            # 下游停止读取不能无限占住主体锁、阻塞退出。
            self.connection.settimeout(5)
            response = None
            # 只把派发与身份变更串行；上游等待不挡住真实退出。
            with lock:
                if session:
                    auth.assert_current(token, session['user_id'])
                    if not (self.command == 'POST' and path == '/api/finance/session'):
                        self._ensure_upstream_session(session['user_id'], token)
                    expected_cookie = auth.upstream_cookie(session['user_id'],session_token=token)
                    if expected_cookie:
                        headers['Cookie'] = expected_cookie
                connection.request(self.command, self.path, body=body, headers=headers)
                if identity_mutation:
                    # Base旋转令牌至新Cookie入jar为一个交换，退出必须看到完整的新身份。
                    response = connection.getresponse()
                    response_headers = response.getheaders()
                    check_current()
                    expected_cookie = adopt_headers()
                    business_logout_response = (not auth.allow_legacy_experience and response.status==200
                        and path=='/api/finance/session/logout' and auth.business_revoked(token))
            if response is None:
                response = connection.getresponse()
                response_headers = response.getheaders()
                with lock:
                    check_current()
                    if session:
                        expected_cookie = adopt_headers()
                        business_logout_response = (not auth.allow_legacy_experience and response.status==200
                            and path=='/api/finance/session/logout' and auth.business_revoked(token))
            html = None
            inject_session = (session is not None and self.command == 'GET' and response.status == 200
                and path.lstrip('/') in APP_PAGES
                and response.getheader('Content-Type', '').split(';')[0].strip().lower() == 'text/html'
                and response.getheader('Content-Encoding', 'identity').lower() == 'identity')
            if inject_session:
                # 只缓冲有限大小的业务 HTML；对话 SSE 与语音继续逐块转发。
                html = response.read(MAX_HTML + 1)
                if len(html) > MAX_HTML:
                    raise AuthError('主页内容过大，请稍后重试。', 502)
                script = b'<script src="/js/welcome_session.js?contract=welcome-business-v1" defer></script>'
                if re.search(rb'</body\s*>', html, re.IGNORECASE):
                    html = re.sub(rb'</body\s*>', lambda match: script + match[0], html,
                                  count=1, flags=re.IGNORECASE)
                else:
                    html += script
            with lock:
                check_current()
            self.send_response(response.status)
            response_started = True
            for key, value in response_headers:
                lower = key.lower()
                if lower in HOP_HEADERS | {'set-cookie', 'server', 'date', 'cache-control', 'x-content-type-options'}:
                    continue
                if html is not None and lower == 'content-length':
                    continue
                if lower == 'location':
                    parsed = urlsplit(value)
                    if parsed.netloc:
                        if parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.port != self.server.upstream_port:
                            continue
                        value = parsed.path + ('?' + parsed.query if parsed.query else '') + ('#' + parsed.fragment if parsed.fragment else '')
                self.send_header(key, value)
            if html is not None:
                self.send_header('Content-Length', str(len(html)))
            if websocket and response.status == 101:
                self.send_header('Connection', 'Upgrade')
                self.send_header('Upgrade', 'websocket')
                self.end_headers()
                self.wfile.flush()
                self._websocket_tunnel(connection.sock, check_current, lock)
                return
            if html is None and response.getheader('Content-Length') is None:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            if html is not None:
                with lock:
                    check_current()
                    self.wfile.write(html)
                    self.wfile.flush()
            elif self.command != 'HEAD':
                while chunk := response.read1(65536):
                    with lock:
                        check_current()
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except AuthError:
            if identity_mutation and response_headers and not response_started:
                try:
                    unclaimed = self._experience_bank_cookie(response_headers)
                except AuthError:
                    unclaimed = None
                with lock:
                    try:
                        current = auth.upstream_cookie(session['user_id'],session_token=token)
                    except AuthError:
                        current = None
                    if (unclaimed and current is not None and current==expected_cookie
                            and self._bank_cookie_value(current)!=unclaimed and not auth.business_revoked(token)):
                        # 只回收本次失败交换；后来成功选择的新jar不能被迟到清理撤销。
                        if auth.allow_legacy_experience:
                            self._revoke_business_cookie(unclaimed)
                        else:
                            self.server.experiences.revoke(token)
            if response_started:
                self.close_connection = True
            else:
                raise
        except (OSError, client.HTTPException):
            # 返回头之后断连时直接结束；不会把错误 JSON 混进流式内容。
            if response_started:
                self.close_connection = True
            else:
                raise AuthError('Navren 主页服务暂不可用，请稍后重试。', 502) from None
        finally:
            connection.close()

    def _websocket_tunnel(self, upstream_socket, check_current, lock):
        self.close_connection = True
        if upstream_socket is None:
            return
        peers = (self.connection, upstream_socket)
        while True:
            with lock:
                check_current()
            ready, _, _ = select.select(peers, [], [], 1)
            if not ready:
                continue
            for source in ready:
                chunk = source.recv(65536)
                if not chunk:
                    return
                target = upstream_socket if source is self.connection else self.connection
                with lock:
                    check_current()
                    target.sendall(chunk)

    def _route(self):
        try:
            self._origin_check()
            path = unquote(urlsplit(self.path).path)
            if '\\' in path or '\x00' in path or '..' in path.split('/'):
                raise AuthError('页面地址无效。', 404)
            if path.startswith(AUTH_PREFIX):
                self._auth(path)
                return
            if path in PUBLIC_ROUTES or path.startswith('/assets/vendor/welcome-three/'):
                if self.command not in ('GET', 'HEAD'):
                    raise AuthError('页面不接受此操作。', 405)
                target = PUBLIC_ROUTES.get(path, path.lstrip('/'))
                file = (self.server.root / target).resolve()
                if not file.is_relative_to(self.server.root) or not file.is_file() or file.suffix not in PUBLIC_EXTENSIONS | {'.html'}:
                    raise AuthError('页面不存在。', 404)
                self.path = '/' + target
                super().do_HEAD() if self.command == 'HEAD' else super().do_GET()
                return
            if path.lstrip('/') in APP_PAGES or path.startswith('/api/'):
                session = self._guard(path)
                if session:
                    self._proxy(session)
                return
            if path.startswith(('/js/', '/css/', '/assets/')) and Path(path).suffix.lower() in PUBLIC_EXTENSIONS:
                if self.command not in ('GET', 'HEAD'):
                    raise AuthError('静态资源不接受此操作。', 405)
                self._proxy()
                return
            raise AuthError('页面不存在。', 404)
        except (AuthError, SmsFailure) as error:
            # 错误请求可能含尚未读取的 body；关闭连接防止残留内容变成下一条请求。
            self.close_connection = True
            data = {'error': error.message}
            if error.code:
                data['code'] = error.code
            if urlsplit(self.path).path.startswith('/api/finance/'):
                data = {'error': {'message': error.message,
                    'code': 'SESSION_CHANGED' if error.code == 'WELCOME_BUSINESS_CHANGED' else error.code or 'WELCOME_GATEWAY_ERROR'}}
                if error.code:
                    data['error']['welcome_code'] = error.code
            self._json(error.status, data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = _route
    do_HEAD = _route
    do_POST = _route
    do_PUT = _route
    do_PATCH = _route
    do_DELETE = _route

    def do_OPTIONS(self):
        self.close_connection = True
        self._json(403, {'error': 'Navren 不开放跨站登录或业务请求。'})


def create_welcome_server(port=18768, database=None, *, root=ROOT, upstream_port=18767, clock=time.time,
                          sms_transport=None, allow_test_sms=False, allow_unverified_registration=False,
                          experience_provider=None, allow_legacy_experience=False):
    """生产启动固定代理 127.0.0.1:18767；端口注入只用于隔离测试。"""
    instance = server.ThreadingHTTPServer(('127.0.0.1', port), WelcomeHandler)
    instance.daemon_threads = True
    instance.root = Path(root).resolve()
    instance.upstream_port = upstream_port
    try:
        instance.auth = WelcomeAuth(database, clock, sms_transport=sms_transport, allow_test_sms=allow_test_sms,
            allow_unverified_registration=allow_unverified_registration,allow_legacy_experience=allow_legacy_experience)
        if allow_legacy_experience and experience_provider is not None:
            raise ValueError('legacy_and_registered_experience_are_exclusive')
        instance.experiences = RegisteredExperienceAdapter(instance.auth,experience_provider)
    except BaseException:
        instance.server_close()
        raise
    return instance
