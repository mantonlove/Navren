"""本地认证和固定上游代理的真实 HTTP 回归；不触及现役账号或业务库。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest

from welcome_auth import (COOKIE, EXPERIENCE_ACCOUNTS, EXPERIENCE_UNAVAILABLE,
                          MAX_BODY, MAX_CSV_BODY, WelcomeAuth, create_welcome_server)


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def json_response(self, status, data, bank_cookie=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        if bank_cookie:
            self.send_header('Set-Cookie', f'qz_bank_user={bank_cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/finance/session':
            cookie = SimpleCookie(); cookie.load(self.headers.get('Cookie', ''))
            token = cookie['qz_bank_user'].value if 'qz_bank_user' in cookie else ''
            if token in self.server.revoked_tokens:
                self.json_response(409, {'error': {'code': 'SESSION_CHANGED'}})
                return
            selected = token.startswith('selected-')
            account = next((a for a in EXPERIENCE_ACCOUNTS if token.endswith('-' + a)), 'tycoon')
            self.json_response(200, {'environment': 'simulation', 'workspace_id': self.server.demo_workspaces[account] if selected else 'a' * 32,
                'account_id': account, 'privacy_revision': 9 if selected else 7,
                'csrf': ('selected-csrf-' if selected else 'bootstrap-csrf-') + token,
                'demo_identity': {'mode': 'demo_account', 'user_id': account}})
            return
        if self.path == '/api/finance/demo/accounts':
            self.server.experience_requests.append({'path': self.path, 'cookie': self.headers.get('Cookie', '')})
            self.json_response(self.server.demo_catalog_status, self.server.demo_catalog)
            return
        if self.path == '/api/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(b'data: first\n\n')
            self.wfile.flush()
            self.server.continue_stream.wait(5)
            self.server.stream_finished.set()
            self.wfile.write(b'data: last\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == '/api/speech/ws':
            self.send_response(101)
            self.send_header('Connection', 'Upgrade')
            self.send_header('Upgrade', 'websocket')
            self.end_headers()
            self.wfile.write(b'hello')
            self.wfile.flush()
            self.connection.sendall(self.connection.recv(4))
            self.close_connection = True
            return
        if self.path == '/chat.html':
            self.send_response(307)
            self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/chat.html?ui=test')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path.startswith(('/chat.html?', '/mine.html?')):
            body = b'<!doctype html><html><body>Navren home</body></html>'
            self.send_response(403 if 'error=1' in self.path else 200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({'cookie': self.headers.get('Cookie', ''),
                           'host': self.headers.get('Host'), 'origin': self.headers.get('Origin'),
                           'fetch_site': self.headers.get('Sec-Fetch-Site'),
                           'received_bytes': getattr(self, 'received_bytes', 0)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.received_bytes = len(raw)
        if self.path == '/api/finance/session':
            with self.server.test_lock:
                self.server.session_requests += 1
                unavailable = self.server.session_unavailable
                cookie = SimpleCookie()
                cookie.load(self.headers.get('Cookie', ''))
                existing = cookie['qz_bank_user'].value if 'qz_bank_user' in cookie else None
                if not existing and not unavailable:
                    self.server.counter += 1
                value = existing or f'bank-user-{self.server.counter}'
            self.server.experience_requests.append({'path': self.path, 'cookie': self.headers.get('Cookie', '')})
            body = json.dumps({'cookie': value, 'environment': 'simulation', 'workspace_id': 'a' * 32,
                               'privacy_revision': 7, 'csrf': 'bootstrap-csrf-' + value}).encode()
            self.send_response(503 if unavailable else 200)
            if not unavailable and not existing:
                self.send_header('Set-Cookie', f'qz_bank_user={value}; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/finance/session/logout':
            cookie = SimpleCookie(); cookie.load(self.headers.get('Cookie', ''))
            token = cookie['qz_bank_user'].value if 'qz_bank_user' in cookie else ''
            self.server.revoked_tokens.add(token)
            body = json.dumps({'logged_out': True, 'old_requests_revoked': True, 'public_session_logout_required': True}).encode()
            self.send_response(200)
            self.send_header('Set-Cookie', 'qz_bank_user=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif self.path == '/api/finance/demo/select':
            payload = json.loads(raw)
            cookie = self.headers.get('Cookie', '')
            csrf = self.headers.get('X-QZ-CSRF', '')
            self.server.experience_requests.append({'path': self.path, 'cookie': cookie,
                                                   'csrf': csrf, 'payload': payload})
            current_cookie = SimpleCookie()
            current_cookie.load(cookie)
            bank_cookie = current_cookie['qz_bank_user'].value if 'qz_bank_user' in current_cookie else ''
            selected_before = bank_cookie.startswith('selected-')
            before_account = next((a for a in EXPERIENCE_ACCOUNTS if bank_cookie.endswith('-' + a)), 'tycoon')
            valid = (bank_cookie.startswith(('bank-user-', 'selected-'))
                     and csrf == ('selected-csrf-' if selected_before else 'bootstrap-csrf-') + bank_cookie
                     and payload.get('expected_workspace_id') == (self.server.demo_workspaces[before_account] if selected_before else 'a' * 32)
                     and payload.get('expected_privacy_revision') == (9 if selected_before else 7))
            status = self.server.demo_select_status if valid else 403
            account = self.server.demo_selected_account or payload['user_id']
            workspace = self.server.demo_selected_workspace or self.server.demo_workspaces.get(account)
            selected_cookie = ('selected-' + bank_cookie + '-' + account
                               if self.server.demo_rotate_cookie else bank_cookie)
            data = {'environment': 'simulation', 'workspace_id': workspace, 'account_id': account,
                    'privacy_revision': 9, 'csrf': 'selected-csrf-' + selected_cookie,
                    'demo_identity': {'mode': 'demo_account', 'user_id': account}}
            self.json_response(status, data, selected_cookie if self.server.demo_send_cookie else None)
        else:
            self.do_GET()


class Browser:
    def __init__(self, port):
        self.port = port
        self.cookies = {}
        self.csrf = None

    def request(self, method, path, payload=None, extra_headers=None, raw_body=None):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Cookie': '; '.join(k + '=' + v for k, v in self.cookies.items())}
        body = raw_body
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload).encode()
        if self.csrf:
            headers['X-Welcome-CSRF'] = self.csrf
        headers.update(extra_headers or {})
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        response_headers = response.getheaders()
        for name, value in response_headers:
            if name.lower() != 'set-cookie':
                continue
            cookie = SimpleCookie()
            cookie.load(value)
            for key, item in cookie.items():
                if item['max-age'] == '0':
                    self.cookies.pop(key, None)
                else:
                    self.cookies[key] = item.value
        raw = response.read()
        status = response.status
        connection.close()
        data = json.loads(raw) if raw and dict(response_headers).get('Content-Type', '').startswith('application/json') else raw
        if isinstance(data, dict) and 'csrf' in data:
            self.csrf = data['csrf']
        return status, data, dict(response_headers)

    def bootstrap(self):
        return self.request('GET', '/api/welcome/auth/session')

    def register(self, username, password='test-passphrase-42'):
        self.bootstrap()
        return self.request('POST', '/api/welcome/auth/register', {'username': username, 'password': password})


class WelcomeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        html = self.root / 'outputs/welcome-scene/index.html'
        html.parent.mkdir(parents=True)
        html.write_text('<!doctype html><title>欢迎</title>')
        public_js = self.root / 'js/welcome_session.js'
        public_js.parent.mkdir()
        public_js.write_text('window.testSessionEntry = true;')
        self.now = [1000.0]
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.counter = 0
        self.upstream.revoked_tokens = set()
        self.upstream.session_requests = 0
        self.upstream.session_unavailable = False
        self.upstream.demo_workspaces = {account: str(index) * 32
            for index, account in enumerate(EXPERIENCE_ACCOUNTS, 1)}
        self.upstream.demo_catalog = {'environment': 'simulation', 'source': 'navren_synthetic',
            'is_real_bank_data': False, 'initialized_count': 4, 'accounts': [
                {'user_id': account, 'display_name': name, 'source': 'navren_synthetic',
                 'workspace_id': self.upstream.demo_workspaces[account]}
                for account, name in EXPERIENCE_ACCOUNTS.items()]}
        self.upstream.demo_catalog_status = 200
        self.upstream.demo_select_status = 200
        self.upstream.demo_selected_account = None
        self.upstream.demo_selected_workspace = None
        self.upstream.demo_send_cookie = True
        self.upstream.demo_rotate_cookie = True
        self.upstream.experience_requests = []
        self.upstream.test_lock = threading.Lock()
        self.upstream.continue_stream = threading.Event()
        self.upstream.stream_finished = threading.Event()
        self.service = create_welcome_server(0, self.root / 'auth.sqlite3', root=self.root,
            upstream_port=self.upstream.server_port, clock=lambda: self.now[0])
        self.threads = []
        for instance in (self.upstream, self.service):
            thread = threading.Thread(target=instance.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.browser = Browser(self.service.server_port)

    def tearDown(self):
        for instance in (self.service, self.upstream):
            instance.shutdown()
            instance.server_close()
        for thread in self.threads:
            thread.join(2)
        self.temp.cleanup()

    def test_anonymous_has_public_welcome_but_no_business_html_or_api(self):
        self.assertEqual(self.browser.request('GET', '/')[0], 200)
        status, _, headers = self.browser.request('GET', '/chat.html')
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/')
        self.assertEqual(self.browser.request('GET', '/api/finance/session')[0], 401)
        self.assertEqual(self.browser.request('POST', '/api/agent', {'text': 'hello'})[0], 401)
        self.assertEqual(self.browser.request('GET', '/%2e%2e/auth.sqlite3')[0], 404)

    def test_register_uses_salted_hash_rotates_cookie_and_persists(self):
        self.browser.bootstrap()
        old_token = self.browser.cookies[COOKIE]
        status, data, headers = self.browser.request('POST', '/api/welcome/auth/register',
            {'username': ' Ｔｅｓｔ用户 ', 'password': 'test-passphrase-42'})
        self.assertEqual(status, 201)
        self.assertTrue(data['authenticated'])
        self.assertEqual(data['user']['username'], 'test用户')
        self.assertEqual(data['redirect'], '/chat.html')
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotEqual(old_token, self.browser.cookies[COOKIE])
        self.assertIsNone(self.service.auth.session(old_token))
        self.assertEqual(self.service.auth.database.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            row = db.execute('SELECT salt,password_hash FROM welcome_users').fetchone()
            self.assertEqual(len(row[0]), 16)
            self.assertNotIn('test-passphrase-42', row[1])
        fresh_auth = WelcomeAuth(self.service.auth.database, clock=lambda: self.now[0])
        self.assertEqual(fresh_auth.session(self.browser.cookies[COOKIE])['username'], 'test用户')

    def test_wrong_password_cannot_login_and_logout_revokes_access(self):
        self.browser.register('alice')
        token = self.browser.cookies[COOKIE]
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/logout', {})[0], 200)
        self.assertNotIn(COOKIE, self.browser.cookies)
        self.assertIsNone(self.service.auth.session(token))
        self.browser.bootstrap()
        status, data, _ = self.browser.request('POST', '/api/welcome/auth/login',
            {'username': 'alice', 'password': 'wrong-passphrase'})
        self.assertEqual(status, 401)
        self.assertEqual(data['error'], '用户名或密码不正确。')
        self.assertFalse(self.browser.bootstrap()[1]['authenticated'])
        self.assertEqual(self.browser.request('GET', '/api/finance/session')[0], 401)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/login',
            {'username': 'alice', 'password': 'test-passphrase-42'})[0], 200)

    def test_origin_and_csrf_are_required_for_login_and_proxy(self):
        self.browser.bootstrap()
        body = {'username': 'alice', 'password': 'test-passphrase-42'}
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register', body,
            {'X-Welcome-CSRF': 'wrong'})[0], 403)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register', body,
            {'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register', body,
            {'Sec-Fetch-Site': 'same-site'})[0], 403)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register', body)[0], 201)
        self.assertEqual(self.browser.request('POST', '/api/finance/session', {},
            {'Origin': 'http://localhost:1'})[0], 403)

    def test_proxy_cookie_isolation_and_redirect_headers(self):
        self.browser.register('alice')
        other = Browser(self.service.server_port)
        other.register('bob')
        # 两浏览器带来的遗留/伪造银行 Cookie 不能穿透服务端代理。
        self.browser.cookies['qz_bank_user'] = 'browser-forged-alice'
        other.cookies['qz_bank_user'] = 'browser-forged-bob'
        self.assertEqual(self.browser.request('GET', '/api/check')[1]['cookie'], 'qz_bank_user=bank-user-1')
        self.browser.request('POST', '/api/finance/session', {})
        other.request('POST', '/api/finance/session', {})
        a = self.browser.request('GET', '/api/check')[1]
        b = other.request('GET', '/api/check')[1]
        self.assertEqual(a['cookie'], 'qz_bank_user=bank-user-1')
        self.assertEqual(b['cookie'], 'qz_bank_user=bank-user-2')
        self.assertEqual(a['host'], f'127.0.0.1:{self.upstream.server_port}')
        self.assertEqual(a['origin'], 'http://' + a['host'])
        self.assertEqual(a['fetch_site'], 'same-origin')
        self.assertEqual(self.browser.cookies['qz_bank_user'], 'browser-forged-alice')
        status, _, headers = self.browser.request('GET', '/chat.html')
        self.assertEqual(status, 307)
        self.assertEqual(headers['Location'], '/chat.html?ui=test')
        # 同一个浏览器注销后登录另一用户，也只取得另一用户的上游 Cookie。
        self.browser.request('POST', '/api/welcome/auth/logout', {})
        self.browser.bootstrap()
        self.browser.request('POST', '/api/welcome/auth/login', {'username': 'bob', 'password': 'test-passphrase-42'})
        self.assertEqual(self.browser.request('GET', '/api/check')[1]['cookie'], b['cookie'])

    def test_first_business_request_connects_once_per_local_user(self):
        self.browser.register('alice')
        self.assertEqual(self.upstream.session_requests, 0)
        first = self.browser.request('GET', '/api/check')[1]
        self.assertEqual(first['cookie'], 'qz_bank_user=bank-user-1')
        self.assertEqual(self.upstream.session_requests, 1)
        self.assertEqual(self.browser.request('GET', '/api/check')[1]['cookie'], first['cookie'])
        self.assertEqual(self.upstream.session_requests, 1)
        other = Browser(self.service.server_port)
        other.register('bob')
        self.assertEqual(other.request('GET', '/api/check')[1]['cookie'], 'qz_bank_user=bank-user-2')
        self.assertEqual(self.upstream.session_requests, 2)
        self.assertNotIn('qz_bank_user', self.browser.cookies)
        self.assertNotIn('qz_bank_user', other.cookies)

    def test_failed_upstream_connection_keeps_local_login_and_can_retry(self):
        self.browser.register('alice')
        self.upstream.session_unavailable = True
        self.assertEqual(self.browser.request('GET', '/chat.html?ui=test')[0], 502)
        self.assertTrue(self.browser.bootstrap()[1]['authenticated'])
        self.assertEqual(self.upstream.session_requests, 1)
        self.upstream.session_unavailable = False
        self.assertEqual(self.browser.request('GET', '/chat.html?ui=test')[0], 200)
        self.assertEqual(self.upstream.session_requests, 2)
        self.assertTrue(self.browser.bootstrap()[1]['authenticated'])

    def test_expired_session_cannot_enter_business(self):
        self.browser.register('alice')
        self.now[0] += 43201
        self.assertEqual(self.browser.request('GET', '/api/check')[0], 401)
        self.assertFalse(self.browser.bootstrap()[1]['authenticated'])

    def test_duplicate_registration_race_creates_one_user(self):
        def register_once(_):
            return Browser(self.service.server_port).register('same-user')[0]
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = list(executor.map(register_once, range(2)))
        self.assertEqual(sorted(result), [201, 409])
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_users').fetchone()[0], 1)

    def test_password_username_and_duplicate_fields_validation(self):
        self.browser.bootstrap()
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register',
            {'username': 'alice', 'password': 'short'})[0], 400)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register',
            {'username': '../escape', 'password': 'test-passphrase-42'})[0], 400)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register',
            {'username': 'alice', 'password': 'invalid-\ud800-text'})[0], 400)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/login',
            extra_headers={'Content-Type': 'application/json'},
            raw_body=b'{"username":"a","username":"b","password":"passphrase"}')[0], 400)

    def test_phone_password_registration_and_login_preserve_legacy_username_api(self):
        self.browser.bootstrap()
        credentials = {'phone': '13800000001', 'password': 'test-passphrase-42'}
        status, data, _ = self.browser.request('POST', '/api/welcome/auth/register', credentials)
        self.assertEqual(status, 201)
        self.assertEqual(data['user']['username'], credentials['phone'])
        self.assertTrue(data['authenticated'])
        self.browser.request('POST', '/api/welcome/auth/logout', {})
        self.browser.bootstrap()
        status, data, _ = self.browser.request('POST', '/api/welcome/auth/login',
            {**credentials, 'password': 'wrong-passphrase'})
        self.assertEqual((status, data['error']), (401, '手机号或密码不正确。'))
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/login', credentials)[0], 200)
        legacy = Browser(self.service.server_port)
        legacy.bootstrap()
        # 相同本地账户仍可通过已存在的用户名合同恢复，不创建第二账户。
        self.assertEqual(legacy.request('POST', '/api/welcome/auth/login',
            {'username': credentials['phone'], 'password': credentials['password']})[0], 200)
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_users').fetchone()[0], 1)

    def test_invalid_phone_never_creates_user_or_authenticated_session(self):
        self.browser.bootstrap()
        for phone in ('12345678901', '1380000000', '138000000012', '+8613800000001',
                      ' 13800000001', '１３８０００００００１', 13800000001, None):
            with self.subTest(phone=phone):
                for route in ('register', 'login'):
                    status, data, _ = self.browser.request('POST', '/api/welcome/auth/' + route,
                        {'phone': phone, 'password': 'test-passphrase-42'})
                    self.assertEqual((status, data['error']), (400, '请输入正确的 11 位手机号。'))
        self.assertFalse(self.browser.bootstrap()[1]['authenticated'])
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/register',
            {'phone': '13800000001', 'username': 'alice', 'password': 'test-passphrase-42'})[0], 400)
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_users').fetchone()[0], 0)

    def test_old_auth_database_adds_experience_fields_without_changing_existing_account(self):
        database = self.root / 'legacy-auth.sqlite3'
        old_auth = WelcomeAuth(database, clock=lambda: self.now[0])
        token, session = old_auth.register('legacy-user', 'test-passphrase-42', None)
        with closing(sqlite3.connect(database)) as db:
            original = db.execute('SELECT id,username,salt,password_hash,created_at FROM welcome_users').fetchone()
            db.execute('ALTER TABLE welcome_users DROP COLUMN experience_account')
            db.execute('ALTER TABLE welcome_users DROP COLUMN identity_kind')
            db.commit()
        migrated = WelcomeAuth(database, clock=lambda: self.now[0])
        self.assertEqual(migrated.session(token)['identity_kind'], 'local')
        self.assertEqual(migrated.session(token)['user_id'], session['user_id'])
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute('SELECT id,username,salt,password_hash,created_at FROM welcome_users').fetchone(), original)
        _, logged_in = migrated.login('legacy-user', 'test-passphrase-42', token)
        self.assertTrue(migrated.public_session(logged_in)['authenticated'])

    def test_experience_requires_same_origin_csrf_and_fixed_account_id(self):
        self.browser.bootstrap()
        payload = {'user_id': 'tycoon'}
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/experience', payload,
            {'X-Welcome-CSRF': 'wrong'})[0], 403)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/experience', payload,
            {'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/experience', payload,
            {'Sec-Fetch-Site': 'cross-site'})[0], 403)
        for invalid in ({'user_id': 'unknown'}, {'user_id': None},
                        {'user_id': 'tycoon', 'workspace_id': 'forged'}, {}):
            self.assertEqual(self.browser.request('POST', '/api/welcome/auth/experience', invalid)[0], 400)
        self.assertEqual(self.upstream.experience_requests, [])
        self.assertFalse(self.browser.bootstrap()[1]['authenticated'])

    def test_four_experiences_select_server_identity_and_independent_cookies(self):
        identities = []
        selected_cookies = []
        for account in EXPERIENCE_ACCOUNTS:
            browser = Browser(self.service.server_port)
            browser.bootstrap()
            anonymous = browser.cookies[COOKIE]
            browser.cookies['qz_bank_user'] = 'browser-forged'
            status, data, headers = browser.request('POST', '/api/welcome/auth/experience', {'user_id': account})
            self.assertEqual(status, 200)
            self.assertTrue(data['authenticated'])
            self.assertEqual(data['mode'], 'experience')
            self.assertEqual(data['experience_account']['user_id'], account)
            self.assertEqual(data['redirect'], '/chat.html')
            self.assertIn('HttpOnly', headers['Set-Cookie'])
            self.assertNotIn('qz_bank_user', headers['Set-Cookie'])
            self.assertIsNone(self.service.auth.session(anonymous))
            session = self.service.auth.session(browser.cookies[COOKIE])
            identities.append(session['user_id'])
            self.assertEqual(session['identity_kind'], 'experience')
            selected_cookies.append(browser.request('GET', '/api/check')[1]['cookie'])
            calls = self.upstream.experience_requests[-3:]
            self.assertEqual([call['path'] for call in calls],
                ['/api/finance/demo/accounts', '/api/finance/session', '/api/finance/demo/select'])
            self.assertEqual(calls[0]['cookie'], '')
            self.assertEqual(calls[1]['cookie'], '')
            self.assertNotIn('browser-forged', calls[2]['cookie'])
            self.assertEqual(calls[2]['payload'], {'user_id': account,
                'expected_workspace_id': 'a' * 32, 'expected_privacy_revision': 7})
        self.assertEqual(len(set(identities)), 4)
        self.assertEqual(len(set(selected_cookies)), 4)
        self.assertTrue(all(cookie.startswith('qz_bank_user=selected-bank-user-') for cookie in selected_cookies))
        self.assertEqual(self.upstream.session_requests, 4)

    def test_unavailable_or_unready_experience_catalog_never_grants_access(self):
        self.browser.bootstrap()
        original = self.browser.cookies[COOKIE]
        valid_catalog = self.upstream.demo_catalog
        cases = [
            (401, valid_catalog),
            (200, {**valid_catalog, 'accounts': []}),
            (200, {**valid_catalog, 'source': 'real_bank'}),
            (200, {**valid_catalog, 'is_real_bank_data': True}),
            (200, {**valid_catalog, 'accounts': [valid_catalog['accounts'][0]] * 2}),
            (200, {**valid_catalog, 'accounts': [{**valid_catalog['accounts'][0], 'status': 'pending'}]}),
        ]
        for status, catalog in cases:
            with self.subTest(status=status, catalog_kind=catalog.get('source')):
                self.upstream.demo_catalog_status, self.upstream.demo_catalog = status, catalog
                result, data, _ = self.browser.request('POST', '/api/welcome/auth/experience', {'user_id': 'tycoon'})
                self.assertEqual((result, data['error']), (502, EXPERIENCE_UNAVAILABLE))
                self.assertNotIn('authenticated', data)
                self.assertEqual(self.browser.cookies[COOKIE], original)
                self.assertEqual(self.browser.request('GET', '/api/check')[0], 401)
        self.assertEqual(self.upstream.session_requests, 0)
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_users').fetchone()[0], 0)

    def test_failed_or_mismatched_experience_selection_never_creates_welcome_identity(self):
        self.browser.bootstrap()
        original = self.browser.cookies[COOKIE]
        cases = [
            {'demo_select_status': 503},
            {'demo_selected_account': 'grain_farmer'},
            {'demo_selected_workspace': 'f' * 32},
            {'demo_send_cookie': False},
            {'demo_rotate_cookie': False},
            {'session_unavailable': True},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                defaults = {'demo_select_status': 200, 'demo_selected_account': None,
                            'demo_selected_workspace': None, 'demo_send_cookie': True,
                            'demo_rotate_cookie': True, 'session_unavailable': False}
                for key, value in {**defaults, **overrides}.items():
                    setattr(self.upstream, key, value)
                result, data, _ = self.browser.request('POST', '/api/welcome/auth/experience', {'user_id': 'tycoon'})
                self.assertEqual((result, data['error']), (502, EXPERIENCE_UNAVAILABLE))
                self.assertEqual(self.browser.cookies[COOKIE], original)
                self.assertFalse(self.browser.bootstrap()[1]['authenticated'])
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_users').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM welcome_upstream_cookies').fetchone()[0], 0)

    def test_existing_welcome_subject_selects_fixed_experience_without_new_identity(self):
        self.browser.register('alice')
        original_session = self.service.auth.session(self.browser.cookies[COOKIE])
        normal_cookie = self.browser.request('GET', '/api/check')[1]['cookie']
        status, data, _ = self.browser.request('POST', '/api/welcome/auth/experience', {'user_id': 'grain_farmer'})
        self.assertEqual(status, 200)
        self.assertEqual(data['mode'], 'local')
        self.assertEqual(self.service.auth.session(self.browser.cookies[COOKIE])['user_id'], original_session['user_id'])
        self.assertEqual(data['auth_subject'], 'welcome-' + original_session['token_hash'])
        self.assertEqual(data['experience_account']['user_id'], 'grain_farmer')
        self.assertNotEqual(self.service.auth.upstream_cookie(original_session['user_id']), normal_cookie)
        self.assertEqual(self.upstream.experience_requests[-1]['cookie'], normal_cookie)
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/logout', {})[0], 200)
        with closing(sqlite3.connect(self.service.auth.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM welcome_users WHERE identity_kind='experience'").fetchone()[0], 0)
        self.browser.bootstrap()
        self.browser.request('POST', '/api/welcome/auth/login', {'username': 'alice', 'password': 'test-passphrase-42'})
        self.assertEqual(self.service.auth.upstream_cookie(original_session['user_id']), '')
        self.assertNotEqual(self.browser.request('GET', '/api/check')[1]['cookie'], normal_cookie)

    def test_failed_experience_preserves_existing_normal_session_and_cookie(self):
        self.browser.register('alice')
        original_token = self.browser.cookies[COOKIE]
        normal_cookie = self.browser.request('GET', '/api/check')[1]['cookie']
        self.upstream.demo_select_status = 503
        self.assertEqual(self.browser.request('POST', '/api/welcome/auth/experience', {'user_id': 'tycoon'})[0], 502)
        self.assertEqual(self.browser.cookies[COOKIE], original_token)
        self.assertTrue(self.browser.bootstrap()[1]['authenticated'])
        self.assertEqual(self.browser.request('GET', '/api/check')[1]['cookie'], normal_cookie)

    def test_websocket_upgrade_preserves_early_bytes_and_bidirectional_stream(self):
        self.browser.register('alice')
        sock = socket.create_connection(('127.0.0.1', self.service.server_port), timeout=5)
        try:
            request = (f'GET /api/speech/ws HTTP/1.1\r\nHost: 127.0.0.1:{self.service.server_port}\r\n'
                       f'Cookie: {COOKIE}={self.browser.cookies[COOKIE]}\r\n'
                       'Connection: Upgrade\r\nUpgrade: websocket\r\n'
                       'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n')
            sock.sendall(request.encode())
            result = b''
            while b'\r\n\r\n' not in result:
                result += sock.recv(4096)
            head, pending = result.split(b'\r\n\r\n', 1)
            self.assertIn(b'101 Switching Protocols', head)
            while len(pending) < 5:
                pending += sock.recv(4096)
            self.assertEqual(pending, b'hello')
            sock.sendall(b'ping')
            echoed = b''
            while len(echoed) < 4:
                echoed += sock.recv(4096)
            self.assertEqual(echoed, b'ping')
        finally:
            sock.close()

    def test_authenticated_html_contains_session_script_with_correct_length(self):
        self.assertEqual(self.browser.request('GET', '/js/welcome_session.js')[0], 200)
        self.assertEqual(self.browser.request('GET', '/chat.html?ui=test')[0], 303)
        self.browser.register('alice')
        status, body, headers = self.browser.request('GET', '/chat.html?ui=test')
        self.assertEqual(status, 200)
        self.assertIn(b'<script src="/js/welcome_session.js?contract=welcome-business-v1" defer></script></body>', body)
        self.assertEqual(int(headers['Content-Length']), len(body))
        status, body, headers = self.browser.request('GET', '/mine.html?error=1')
        self.assertEqual(status, 403)
        self.assertNotIn(b'welcome_session.js', body)
        self.assertEqual(int(headers['Content-Length']), len(body))

    def test_sse_first_event_arrives_before_upstream_finishes_without_injection(self):
        self.browser.register('alice')
        connection = HTTPConnection('127.0.0.1', self.service.server_port, timeout=2)
        try:
            connection.request('GET', '/api/stream', headers={'Cookie': f'{COOKIE}={self.browser.cookies[COOKIE]}'})
            response = connection.getresponse()
            first = response.readline()
            self.assertEqual(first, b'data: first\n')
            self.assertFalse(self.upstream.stream_finished.is_set())
            self.upstream.continue_stream.set()
            rest = response.read()
            self.assertIn(b'data: last\n', rest)
            self.assertNotIn(b'welcome_session.js', first + rest)
        finally:
            self.upstream.continue_stream.set()
            connection.close()

    def test_csv_previews_retain_one_and_half_megabyte_budget(self):
        self.browser.register('alice')
        body = b'a' * (MAX_BODY + 1)
        for path in ('/api/finance/statements/preview', '/api/finance/calendar/import/preview'):
            status, data, _ = self.browser.request('POST', path,
                extra_headers={'Content-Type': 'text/csv'}, raw_body=body)
            self.assertEqual(status, 200)
            self.assertEqual(data['received_bytes'], len(body))
        # 只发送超限长度声明，避免被拒绝后还传输无效大 body。
        self.assertEqual(self.browser.request('POST', '/api/agent',
            extra_headers={'Content-Type': 'application/json', 'Content-Length': str(MAX_BODY + 1)}, raw_body=b'')[0], 413)
        self.assertEqual(self.browser.request('POST', '/api/finance/statements/preview',
            extra_headers={'Content-Type': 'text/csv', 'Content-Length': str(MAX_CSV_BODY + 1)}, raw_body=b'')[0], 413)

    def test_unavailable_upstream_returns_actionable_error(self):
        self.browser.register('alice')
        self.upstream.shutdown()
        self.upstream.server_close()
        status, data, _ = self.browser.request('GET', '/api/check')
        self.assertEqual(status, 502)
        self.assertIn('服务暂不可用', data['error'])


if __name__ == '__main__':
    unittest.main()
