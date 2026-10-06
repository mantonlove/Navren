"""本地 Navren 身份认证与同源业务代理；不承担银行实名认证。"""
from __future__ import annotations

from contextlib import contextmanager
from http import client, server
from http.cookies import SimpleCookie
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import select
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
COOKIE = 'navren_welcome_session'
SESSION_TTL = 12 * 60 * 60
ANONYMOUS_TTL = 30 * 60
MAX_BODY = 1024 * 1024
MAX_HTML = 2 * 1024 * 1024
MAX_CSV_BODY = 1_572_864
CSV_PREVIEW_PATHS = {'/api/finance/statements/preview', '/api/finance/calendar/import/preview'}
APP_PAGES = {'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html'}
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
    """密码只存加盐哈希，会话令牌只存摘要；业务 Cookie 按本地用户隔离。"""
    def __init__(self, database=None, clock=time.time):
        self.database = Path(database or Path.home() / 'Library/Application Support/Navren/welcome-auth.sqlite3')
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise ValueError('认证数据库不能使用符号链接')
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.database, 0o600)
        self.clock = clock
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
                CREATE TABLE IF NOT EXISTS welcome_login_failures(
                  username TEXT PRIMARY KEY, count INTEGER NOT NULL, until_at REAL NOT NULL);
            ''')
            # 旧用户名账户保持本地密码身份；体验身份不可走密码登录。
            columns = {row['name'] for row in db.execute('PRAGMA table_info(welcome_users)')}
            if 'identity_kind' not in columns:
                db.execute("ALTER TABLE welcome_users ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'local'")
            if 'experience_account' not in columns:
                db.execute('ALTER TABLE welcome_users ADD COLUMN experience_account TEXT')

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
            row = db.execute('''SELECT s.*,u.username,u.identity_kind,u.experience_account FROM welcome_sessions s
                LEFT JOIN welcome_users u ON u.id=s.user_id
                WHERE token_hash=? AND expires_at>?''', (_digest(token), self.clock())).fetchone()
        return dict(row) if row else None

    def new_session(self, user_id=None, old_token=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = SESSION_TTL if user_id is not None else ANONYMOUS_TTL
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM welcome_sessions WHERE expires_at<=?', (self.clock(),))
            if old_token:
                if not db.execute('SELECT 1 FROM welcome_sessions WHERE token_hash=?', (_digest(old_token),)).fetchone():
                    raise AuthError('登录页面已过期，请刷新后再试。', 401, 'WELCOME_SESSION_REVOKED')
                db.execute('DELETE FROM welcome_sessions WHERE token_hash=?', (_digest(old_token),))
            db.execute('INSERT INTO welcome_sessions VALUES(?,?,?,?)',
                       (_digest(token), user_id, csrf, self.clock() + ttl))
        return token, self.session(token)

    @staticmethod
    def public_session(session, current_identity=None, business_status='not_connected'):
        result = {'authenticated': bool(session and session['user_id'] is not None),
                  'session_contract': 'welcome-business-v1'}
        if session:
            result['csrf'] = session['csrf']
            result['auth_subject'] = 'welcome-' + session['token_hash']
        if result['authenticated']:
            result.update(user={'username': session['username']}, redirect='/chat.html',
                          mode='local', entry_experience_account=None,
                          experience_account=None, current_business_identity=current_identity,
                          business_identity_status=business_status)
            account = current_identity.get('account_id') if current_identity else None
            if account in EXPERIENCE_ACCOUNTS:
                result['experience_account'] = {'user_id': account, 'display_name': EXPERIENCE_ACCOUNTS[account]}
            if session.get('identity_kind') == 'experience':
                entry = session['experience_account']
                result.update(mode='experience', entry_experience_account={
                    'user_id': entry, 'display_name': EXPERIENCE_ACCOUNTS[entry]})
                result['user']['username'] = EXPERIENCE_ACCOUNTS.get(account, '测试体验')
        return result

    def register(self, username, password, old_token):
        username, password = self.validate_credentials(username, password)
        salt = secrets.token_bytes(16)
        with self._hash_slots:
            password_hash = _password_hash(password, salt)
        try:
            with self.connect() as db:
                row = db.execute('INSERT INTO welcome_users(username,salt,password_hash,created_at) VALUES(?,?,?,?)',
                                 (username, salt, password_hash, self.clock()))
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

    def logout(self, token, expected_user_id=...):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if expected_user_id is not ...:
                self.assert_current(token, expected_user_id, db=db)
            identity = db.execute('''SELECT u.id,u.identity_kind FROM welcome_sessions s
                JOIN welcome_users u ON u.id=s.user_id WHERE s.token_hash=?''', (_digest(token),)).fetchone()
            db.execute('DELETE FROM welcome_sessions WHERE token_hash=?', (_digest(token),))
            if identity:
                # 同一欢迎主体的会话共用业务 jar，退出须撤销全部相关会话。
                db.execute('DELETE FROM welcome_upstream_cookies WHERE user_id=?', (identity['id'],))
                db.execute('DELETE FROM welcome_sessions WHERE user_id=?', (identity['id'],))
                if identity['identity_kind'] == 'experience':
                    db.execute('DELETE FROM welcome_users WHERE id=?', (identity['id'],))

    def experience_session(self, account, bank_cookie, old_token):
        """业务选择已验证后原子建立一次体验身份，不赋予任何普通账户权限。"""
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT user_id FROM welcome_sessions WHERE token_hash=? AND expires_at>?',
                                  (_digest(old_token), self.clock())).fetchone() if old_token else None
            if not previous or previous['user_id'] is not None:
                raise AuthError('登录页面已过期，请刷新后再试。', 401, 'WELCOME_SESSION_REVOKED')
            row = db.execute('''INSERT INTO welcome_users
                (username,salt,password_hash,created_at,identity_kind,experience_account)
                VALUES(?,?,?,?, 'experience',?)''',
                ('~experience-' + secrets.token_urlsafe(24), secrets.token_bytes(16),
                 secrets.token_hex(64), self.clock(), account))
            user_id = row.lastrowid
            db.execute('INSERT INTO welcome_upstream_cookies VALUES(?,?,?)',
                       (user_id, 'qz_bank_user', bank_cookie))
            if old_token:
                db.execute('DELETE FROM welcome_sessions WHERE token_hash=?', (_digest(old_token),))
            db.execute('INSERT INTO welcome_sessions VALUES(?,?,?,?)',
                       (_digest(token), user_id, csrf, self.clock() + SESSION_TTL))
        return token, self.session(token)

    def user_lock(self, user_id):
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def upstream_cookie(self, user_id):
        with self.connect() as db:
            return self._db_cookie(db, user_id)

    @staticmethod
    def _db_cookie(db, user_id):
        rows = db.execute('SELECT name,value FROM welcome_upstream_cookies WHERE user_id=? ORDER BY name', (user_id,)).fetchall()
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
        if expected_cookie is not None and self._db_cookie(db, user_id) != expected_cookie:
            raise AuthError('当前业务账户已变化，请重新读取。', 409, 'WELCOME_BUSINESS_CHANGED')
        return dict(row)

    def update_upstream_cookies(self, user_id, values, *, session_token=None, expected_cookie=None):
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
                        db.execute('DELETE FROM welcome_upstream_cookies WHERE user_id=? AND name=?', (user_id, name))
                    else:
                        db.execute('INSERT OR REPLACE INTO welcome_upstream_cookies VALUES(?,?,?)',
                                   (user_id, name, morsel.value))
            return self._db_cookie(db, user_id)


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
            if not session:
                token, session = auth.new_session()
                cookie = self._cookie(token, session)
            identity, status = self._current_business(session, token)
            self._json(200, auth.public_session(session, identity, status), cookie)
            return
        if self.command != 'POST' or path not in {AUTH_PREFIX + x for x in ('register', 'login', 'logout', 'experience')}:
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
        if path.endswith('/logout'):
            if data:
                raise AuthError('退出请求格式不正确。')
            self._complete_logout(session, token)
            self._json(200, {'authenticated': False, 'redirect': '/?view=experience',
                            'scope': 'welcome_subject', 'business_requests_revoked': True},
                       f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            return
        if path.endswith('/experience'):
            if set(data) != {'user_id'} or not isinstance(data['user_id'], str) or data['user_id'] not in EXPERIENCE_ACCOUNTS:
                raise AuthError('请选择列表中的测试账户。')
            if session['user_id'] is not None:
                # 应用内回到既有四账户入口：复用当前欢迎主体和固定业务目录。
                with auth.user_lock(session['user_id']):
                    auth.assert_current(token, session['user_id'])
                    previous = auth.upstream_cookie(session['user_id'])
                    if not previous:
                        raise AuthError('当前业务账户尚未连接，请先完成登录。', 409)
                    bank_cookie, selected = self._select_experience(data['user_id'], previous)
                    auth.update_upstream_cookies(session['user_id'], ['qz_bank_user=' + bank_cookie],
                                                 session_token=token, expected_cookie=previous)
            else:
                bank_cookie, selected = self._select_experience(data['user_id'])
                try:
                    token, session = auth.experience_session(data['user_id'], bank_cookie, token)
                except AuthError:
                    # CAS失败没有发出欢迎身份，也不能遗留仍有效的临时业务令牌。
                    self._revoke_business_cookie(bank_cookie)
                    raise
            self._json(200, auth.public_session(session, self._project_identity(selected), 'verified'),
                       self._cookie(token, session))
            return
        using_phone = set(data) == {'phone', 'password'}
        if using_phone:
            username = auth.validate_phone(data['phone'])
        elif set(data) == {'username', 'password'}:
            username = data['username']
        else:
            raise AuthError('请输入手机号和密码。')
        action = auth.register if path.endswith('/register') else auth.login
        try:
            with auth.user_lock(session['user_id'] if session['user_id'] is not None else session['token_hash']):
                auth.assert_current(token, session['user_id'])
                token, session = action(username, data['password'], token)
        except AuthError as error:
            if using_phone and error.status == 401:
                raise AuthError('手机号或密码不正确。', 401) from None
            if using_phone and error.status == 409:
                raise AuthError('这个手机号已被使用，请登录。', 409) from None
            raise
        self._json(201 if path.endswith('/register') else 200, auth.public_session(session), self._cookie(token, session))

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
        previous = auth.upstream_cookie(session['user_id'])
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
        lock = auth.user_lock(session['user_id'] if session['user_id'] is not None else session['token_hash'])
        with lock:
            auth.assert_current(token, session['user_id'])
            previous = auth.upstream_cookie(session['user_id']) if session['user_id'] is not None else ''
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
        session = self.server.auth.session(self._token())
        if session and session['user_id'] is not None:
            return session
        if path.startswith('/api/'):
            raise AuthError('请先登录 Navren。', 401)
        self.send_response(303)
        self.send_header('Location', '/')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return None

    def _ensure_upstream_session(self, user_id, token):
        """本地认证通过后，独立连接已有银行演示身份；调用者持有用户锁。"""
        auth = self.server.auth
        if auth.upstream_cookie(user_id):
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
            if not auth.upstream_cookie(user_id):
                raise AuthError('Navren 账户连接未完成，请稍后重试。', 502)
        except (OSError, client.HTTPException):
            raise AuthError('Navren 账户服务暂不可用，请稍后重试。', 502) from None
        finally:
            bootstrap.close()

    def _proxy(self, session=None):
        path = urlsplit(self.path).path
        body_limit = MAX_CSV_BODY if path in CSV_PREVIEW_PATHS else MAX_BODY
        body = self._body(body_limit) if self.command in ('POST', 'PUT', 'PATCH', 'DELETE') else None
        headers = {key: value for key, value in self.headers.items() if key.lower() in FORWARDED_HEADERS}
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
        lock = auth.user_lock(session['user_id']) if session else threading.Lock()
        def check_current():
            if session:
                auth.assert_current(token, session['user_id'], expected_cookie)
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
                    expected_cookie = auth.upstream_cookie(session['user_id'])
                    if expected_cookie:
                        headers['Cookie'] = expected_cookie
                connection.request(self.command, self.path, body=body, headers=headers)
                if identity_mutation:
                    # Base旋转令牌至新Cookie入jar为一个交换，退出必须看到完整的新身份。
                    response = connection.getresponse()
                    response_headers = response.getheaders()
                    check_current()
                    expected_cookie = auth.update_upstream_cookies(session['user_id'],
                        [value for key, value in response_headers if key.lower() == 'set-cookie'],
                        session_token=token, expected_cookie=expected_cookie)
            if response is None:
                response = connection.getresponse()
                response_headers = response.getheaders()
                with lock:
                    check_current()
                    if session:
                        expected_cookie = auth.update_upstream_cookies(session['user_id'],
                            [value for key, value in response_headers if key.lower() == 'set-cookie'],
                            session_token=token, expected_cookie=expected_cookie)
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
                if unclaimed and self._bank_cookie_value(auth.upstream_cookie(session['user_id'])) != unclaimed:
                    # 已知新令牌但CAS未认领，回收它；完全丢失响应头需要Base恢复合同。
                    self._revoke_business_cookie(unclaimed)
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
        except AuthError as error:
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


def create_welcome_server(port=18768, database=None, *, root=ROOT, upstream_port=18767, clock=time.time):
    """生产启动固定代理 127.0.0.1:18767；端口注入只用于隔离测试。"""
    instance = server.ThreadingHTTPServer(('127.0.0.1', port), WelcomeHandler)
    instance.daemon_threads = True
    instance.root = Path(root).resolve()
    instance.upstream_port = upstream_port
    try:
        instance.auth = WelcomeAuth(database, clock)
    except BaseException:
        instance.server_close()
        raise
    return instance
