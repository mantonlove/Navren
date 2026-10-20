"""正式退出路由的隔离 HTTP 验证；随机端口、临时库，不碰现役会话或模型。"""
from contextlib import closing
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from finance_gateway import COOKIE as BANK_COOKIE, FinanceGateway
from server import NavrenHTTPServer, ProxyHandler
from simulated_bank import BankError
from simulated_bank.service import digest
from welcome_auth import COOKIE as WELCOME_COOKIE, create_welcome_server


class QuietHandler(ProxyHandler):
    def log_message(self, *_):
        pass


class FixtureBrowser:
    """只管理当前临时服务发行的 Cookie，不读取系统浏览器或现役凭据。"""
    def __init__(self, port):
        self.port = port
        self.cookies = {}
        self.csrf = None

    def request(self, method, path, payload=None, *, headers=None):
        request_headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            request_headers['Content-Type'] = 'application/json'
        if self.csrf:
            request_headers['X-Welcome-CSRF'] = self.csrf
        request_headers.update(headers or {})
        connection = HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            response_headers = response.getheaders()
            data = json.loads(response.read())
            for name, value in response_headers:
                if name.lower() != 'set-cookie':
                    continue
                jar = SimpleCookie(); jar.load(value)
                for key, item in jar.items():
                    if item['max-age'] == '0':
                        self.cookies.pop(key, None)
                    else:
                        self.cookies[key] = item.value
            if 'csrf' in data and path.startswith('/api/welcome/auth/'):
                self.csrf = data['csrf']
            return response.status, data, dict(response_headers)
        finally:
            connection.close()


class SettingsLogoutRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='navren-settings-logout-isolated-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(self.root / 'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB': '',
        })
        environment.start(); self.addCleanup(environment.stop)
        original_connect = socket.socket.connect

        def local_connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', 'localhost', '::1'}:
                raise AssertionError('external_network_forbidden')
            return original_connect(sock, address)

        local_only = patch.object(socket.socket, 'connect', local_connect)
        local_only.start(); self.addCleanup(local_only.stop)
        # 若依赖误走钥匙串/外部命令，应使测试失败而不是读取本机凭据。
        for operation in ('run', 'Popen', 'check_output'):
            blocked = patch.object(subprocess, operation,
                side_effect=AssertionError('key_or_model_process_forbidden'))
            blocked.start(); self.addCleanup(blocked.stop)
        self.gateway = FinanceGateway(self.root / 'bank.sqlite3')
        self.gateway.demo_accounts.initialize_offline(allow_offline=True)
        self.base = NavrenHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.base.finance = self.gateway
        self.serve(self.base)
        welcome_html = self.root / 'outputs/welcome-scene/index.html'
        welcome_html.parent.mkdir(parents=True)
        welcome_html.write_text('<!doctype html><title>隔离登录</title>')
        self.welcome = create_welcome_server(0, self.root / 'welcome.sqlite3',
            root=self.root, upstream_port=self.base.server_port, allow_unverified_registration=True,allow_legacy_experience=True)
        self.serve(self.welcome)
        self.assertNotIn(self.base.server_port, {60291, 60262, 18767, 18768})
        self.assertNotIn(self.welcome.server_port, {60291, 60262, 18767, 18768})

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def bank_browser(self, user='tycoon'):
        workspace, token = self.gateway.demo_sessions.create(user)
        browser = FixtureBrowser(self.base.server_port)
        browser.cookies[BANK_COOKIE] = token
        return browser, workspace, token

    def bank_logout(self, browser, token, **kwargs):
        return browser.request('POST', '/api/finance/session/logout', {},
            headers={'X-QZ-CSRF': self.gateway.csrf(token), **kwargs})

    def state(self):
        with closing(self.gateway.bank.connect()) as db:
            return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('accounts', 'holdings', 'loans', 'wealth_goals', 'transactions')}

    def test_exact_demo_token_revoked_without_reset_or_other_session_revocation(self):
        browser, workspace, token = self.bank_browser()
        same_account, _, _ = self.bank_browser()
        other_account, _, _ = self.bank_browser('grain_farmer')
        before = self.state()
        status, data, headers = self.bank_logout(browser, token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'logged_out': True, 'old_requests_revoked': True,
            'public_session_logout_required': True})
        self.assertNotIn('X-Navren-Workspace', headers)
        jar = SimpleCookie(); jar.load(headers['Set-Cookie'])
        self.assertEqual(set(jar), {BANK_COOKIE})
        self.assertEqual(jar[BANK_COOKIE]['path'], '/')
        self.assertEqual(jar[BANK_COOKIE]['samesite'], 'Strict')
        self.assertTrue(jar[BANK_COOKIE]['httponly'])
        self.assertEqual(jar[BANK_COOKIE]['max-age'], '0')
        self.assertEqual(self.state(), before)
        stale = FixtureBrowser(self.base.server_port); stale.cookies[BANK_COOKIE] = token
        status, data, _ = stale.request('GET', '/api/finance/session')
        self.assertEqual((status, data['error']['code']), (409, 'SESSION_CHANGED'))
        self.assertEqual(same_account.request('GET', '/api/finance/session')[0], 200)
        self.assertEqual(other_account.request('GET', '/api/finance/session')[0], 200)
        with closing(self.gateway.bank.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE workspace_id=? AND event='account_logout'",
                (workspace,)).fetchone()[0], 1)

    def test_legacy_human_token_revoked_without_deleting_workspace(self):
        credentials = self.gateway.bank.create_workspace()
        token = credentials['session_token']; workspace = credentials['workspace_id']
        browser = FixtureBrowser(self.base.server_port); browser.cookies[BANK_COOKIE] = token
        self.assertEqual(self.bank_logout(browser, token)[0], 200)
        with closing(self.gateway.bank.connect()) as db:
            self.assertIsNotNone(db.execute('SELECT id FROM workspaces WHERE id=?', (workspace,)).fetchone())
            self.assertEqual(db.execute('SELECT revoked FROM demo_sessions WHERE token_hash=?',
                (digest(token),)).fetchone()[0], 1)
        with self.assertRaises(BankError) as failure:
            self.gateway.bank.session(token)
        self.assertEqual(failure.exception.code, 'SESSION_CHANGED')

    def test_method_csrf_origin_payload_and_query_fail_without_revocation(self):
        browser, _, token = self.bank_browser()
        for method, path, payload, headers, expected in [
            ('GET', '/api/finance/session/logout', None, {}, 405),
            ('POST', '/api/finance/session/logout', {}, {'X-QZ-CSRF': 'wrong-fixture-csrf'}, 403),
            ('POST', '/api/finance/session/logout', {}, {'Origin': 'https://forbidden.example'}, 403),
            ('POST', '/api/finance/session/logout', {'workspace_id': 'foreign-fixture'}, {}, 400),
            ('POST', '/api/finance/session/logout?account_id=grain_farmer', {}, {}, 400),
            ('POST', '/api/finance/session/logout?ignored', {}, {}, 400),
        ]:
            with self.subTest(method=method, path=path):
                status, _, returned = browser.request(method, path, payload,
                    headers={'X-QZ-CSRF': self.gateway.csrf(token), **headers})
                self.assertEqual(status, expected)
                self.assertNotIn('Set-Cookie', returned)
                self.assertEqual(browser.request('GET', '/api/finance/session')[0], 200)
        anonymous = FixtureBrowser(self.base.server_port)
        self.assertEqual(anonymous.request('POST', '/api/finance/session/logout', {})[0], 401)

    def test_audit_failure_rolls_back_and_preserves_retryable_current_token(self):
        browser, _, token = self.bank_browser()
        with patch.object(self.gateway.bank, '_audit', side_effect=RuntimeError('fixture-write-failure')):
            status, data, headers = self.bank_logout(browser, token)
        self.assertEqual((status, data['error']['code']), (500, 'BANK_UNAVAILABLE'))
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(browser.request('GET', '/api/finance/session')[0], 200)
        self.assertEqual(self.bank_logout(browser, token)[0], 200)

    def test_old_authenticated_request_cannot_write_after_logout(self):
        browser, workspace, token = self.bank_browser()
        with self.gateway.demo_sessions.request(token, workspace):
            self.assertEqual(self.bank_logout(browser, token)[0], 200)
            with self.assertRaises(BankError) as failure:
                with self.gateway.bank.transaction() as db:
                    db.execute('UPDATE workspaces SET version=version+1 WHERE id=?', (workspace,))
        self.assertEqual(failure.exception.code, 'SESSION_CHANGED')

    def test_complete_welcome_logout_uses_formal_business_route_and_keeps_other_user(self):
        browser = FixtureBrowser(self.welcome.server_port)
        self.assertEqual(browser.request('GET', '/api/welcome/auth/session')[0], 200)
        self.assertEqual(browser.request('POST', '/api/welcome/auth/experience', {'user_id': 'tycoon'})[0], 200)
        self.assertEqual(browser.request('GET', '/api/finance/session')[0], 200)
        other = FixtureBrowser(self.welcome.server_port)
        other.request('GET', '/api/welcome/auth/session')
        self.assertEqual(other.request('POST', '/api/welcome/auth/experience', {'user_id': 'grain_farmer'})[0], 200)
        old_welcome = browser.cookies[WELCOME_COOKIE]
        session = self.welcome.auth.session(old_welcome)
        # 仅从本测试新建的认证库取得夹具业务令牌，用于验证旧令牌拒绝。
        old_bank = self.welcome.auth.upstream_cookie(session['user_id'])
        jar = SimpleCookie(); jar.load(old_bank)
        old_bank_token = jar[BANK_COOKIE].value
        before = self.state()
        status, data, _ = browser.request('POST', '/api/welcome/auth/logout', {})
        self.assertEqual(status, 200)
        self.assertEqual(data['scope'], 'welcome_subject')
        self.assertTrue(data['business_requests_revoked'])
        self.assertFalse(data['authenticated'])
        self.assertNotIn(WELCOME_COOKIE, browser.cookies)
        self.assertIsNone(self.welcome.auth.session(old_welcome))
        self.assertEqual(self.welcome.auth.upstream_cookie(session['user_id']), '')
        stale = FixtureBrowser(self.base.server_port); stale.cookies[BANK_COOKIE] = old_bank_token
        self.assertEqual(stale.request('GET', '/api/finance/session')[0], 409)
        stale_welcome = FixtureBrowser(self.welcome.server_port)
        stale_welcome.cookies[WELCOME_COOKIE] = old_welcome
        self.assertEqual(stale_welcome.request('GET', '/api/finance/session')[0], 401)
        self.assertEqual(other.request('GET', '/api/finance/session')[0], 200)
        self.assertEqual(self.state(), before)

    def test_failed_business_logout_preserves_welcome_identity_for_explicit_retry(self):
        browser = FixtureBrowser(self.welcome.server_port)
        browser.request('GET', '/api/welcome/auth/session')
        browser.request('POST', '/api/welcome/auth/experience', {'user_id': 'tycoon'})
        old_welcome = browser.cookies[WELCOME_COOKIE]
        with patch.object(self.gateway.bank, '_audit', side_effect=RuntimeError('fixture-write-failure')):
            status, data, _ = browser.request('POST', '/api/welcome/auth/logout', {})
        self.assertEqual((status, data['code']), (502, 'BUSINESS_LOGOUT_UNAVAILABLE'))
        self.assertIsNotNone(self.welcome.auth.session(old_welcome))
        self.assertEqual(browser.request('GET', '/api/finance/session')[0], 200)
        self.assertEqual(browser.request('POST', '/api/welcome/auth/logout', {})[0], 200)


if __name__ == '__main__':
    unittest.main()
