"""欢迎身份与 DQ Base 的真实隔离接线；不访问现役端口、库或模型。

Base 在独立 Python 进程从候选导入，避免测试发现时主分支同名模块
污染候选。两个 HTTP 服务均使用随机 loopback 端口与临时数据库。
延迟标记只用于让退出/切换准确发生在旧请求已经认证之后。
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
import json
import hashlib
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from welcome_auth import AuthError, COOKIE, EXPERIENCE_ACCOUNTS, WelcomeHandler, create_welcome_server


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / 'outputs/window-progress/base/object-reissue/design-quality/candidate'
WELCOME_SOURCE_AT_IMPORT = hashlib.sha256((ROOT / 'welcome_auth.py').read_bytes()).hexdigest()

# 这是故意延迟真实 Base 调用的测试夹具，不是备用业务实现。
CANDIDATE_WORKER = r'''
import hashlib, json, os, socket, subprocess, sys, threading, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
candidate_root = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2])
tracked = list(candidate_root.rglob('*.py')) + list((candidate_root / 'simulated_bank/fixtures').rglob('*.json'))
initial_inputs = {str(path.relative_to(candidate_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in tracked}
original_connect = socket.socket.connect
def local_only(sock, address):
    if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', 'localhost', '::1'}:
        raise AssertionError('external_network_forbidden')
    return original_connect(sock, address)
socket.socket.connect = local_only
for operation in ('run', 'Popen', 'check_output'):
    setattr(subprocess, operation, lambda *a, **k: (_ for _ in ()).throw(AssertionError('key_or_model_process_forbidden')))
from base_runtime.design_quality_gateway import create_isolated_server
from simulated_bank import BankError
server = create_isolated_server(root / 'bank.sqlite3')
g = server.gateway
g.demo_accounts.initialize_offline(allow_offline=True)

def pause(name):
    (root / (name + '.ready')).touch()
    deadline = time.monotonic() + 10
    while not (root / (name + '.release')).exists():
        if time.monotonic() > deadline:
            raise AssertionError('test_delay_not_released:' + name)
        time.sleep(.01)

original_display = g.account_profile.update_display
def delayed_display(w, payload):
    if payload.get('display_name') == '迟到退出保存':
        pause('profile-write')
    return original_display(w, payload)
g.account_profile.update_display = delayed_display

original_select = g.demo_sessions.select
selection_lock = threading.Lock()
def delayed_select(*args, **kwargs):
    if (root / 'pause-select').exists():
        pause('select')
    selected = original_select(*args, **kwargs)
    with selection_lock:
        path = root / 'selected-tokens.json'
        tokens = json.loads(path.read_text()) if path.exists() else []
        tokens.append(selected[1])
        path.write_text(json.dumps(tokens))
    if (root / 'pause-select-after-commit').exists():
        pause('select-after-commit')
    return selected
g.demo_sessions.select = delayed_select

def input_evidence():
    imported = set()
    for module in list(sys.modules.values()):
        location = getattr(module, '__file__', None)
        if not location: continue
        path = Path(location).resolve()
        if path.is_relative_to(candidate_root) and path.suffix == '.py':
            imported.add(str(path.relative_to(candidate_root)))
    fixtures = {name for name in initial_inputs if name.startswith('simulated_bank/fixtures/')}
    relevant = imported | fixtures
    final = {name: hashlib.sha256((candidate_root / name).read_bytes()).hexdigest()
             for name in sorted(relevant) if (candidate_root / name).exists()}
    changed = [name for name in sorted(relevant) if final.get(name) != initial_inputs.get(name)]
    return {'candidate_sources': {name: initial_inputs.get(name) for name in sorted(imported)},
            'fixture_sources': {name: initial_inputs[name] for name in sorted(fixtures)},
            'final_input_hashes': final, 'inputs_stable': not changed, 'changed_inputs': changed}

handler = server.RequestHandlerClass
original_get = handler.do_GET
def get(self):
    path = self.path.split('?', 1)[0]
    if path == '/api/test/source-evidence':
        self._finance_json(200, input_evidence())
        return
    if path not in {'/api/test/delayed-response', '/api/test/stream', '/api/test/dispatch-count'}:
        return original_get(self)
    try:
        g.origin_check(self)
        w, token = g.identity(self)
        with g.demo_sessions.request(token, w):
            if path == '/api/test/dispatch-count':
                (root / 'dispatch-count').touch()
                self._finance_json(200, {'dispatched': True})
            elif path == '/api/test/delayed-response':
                pause('response')
                self._finance_json(200, {'old_private_body': True},
                    'qz_bank_user=late-cookie; HttpOnly; SameSite=Strict; Path=/')
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'data: first\n\n'); self.wfile.flush()
                pause('stream')
                self.wfile.write(b'data: old-private-tail\n\n'); self.wfile.flush()
                self.close_connection = True
    except BankError as error:
        self._finance_json(error.status, {'error': error.as_dict()})
    except (BrokenPipeError, ConnectionResetError):
        self.close_connection = True
handler.do_GET = get
original_post = handler.do_POST
def post(self):
    failure = root / 'logout-failure'
    if self.path == '/api/finance/session/logout' and failure.exists():
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        mode = failure.read_text()
        if mode in {'503', '404'}:
            self._finance_json(int(mode), {'error': {'code': 'TEST_LOGOUT_UNAVAILABLE'}})
        else:
            result = {'logged_out': True, 'old_requests_revoked': True, 'public_session_logout_required': True}
            if mode == 'false-proof': result['old_requests_revoked'] = False
            cookie = None if mode == 'missing-cookie' else 'qz_bank_user=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
            self._finance_json(200, result, cookie)
        return
    return original_post(self)
handler.do_POST = post
print(json.dumps({'port': server.server_port, **input_evidence()}), flush=True)
server.serve_forever(poll_interval=.05)
'''


class Browser:
    """只管理本测试 HTTP Cookie；永远不读取浏览器或现役身份。"""
    def __init__(self, port):
        self.port, self.cookies, self.csrf = port, {}, None

    def request(self, method, path, payload=None, headers=None):
        request_headers = {'Cookie': '; '.join(k + '=' + v for k, v in self.cookies.items())}
        if self.csrf:
            request_headers['X-Welcome-CSRF'] = self.csrf
        body = None
        if payload is not None:
            request_headers['Content-Type'] = 'application/json'
            body = json.dumps(payload).encode()
        request_headers.update(headers or {})
        connection = HTTPConnection('127.0.0.1', self.port, timeout=12)
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            response_headers = response.getheaders()
            raw = response.read()
            for name, value in response_headers:
                if name.lower() == 'set-cookie':
                    parsed = SimpleCookie(); parsed.load(value)
                    for key, item in parsed.items():
                        if item['max-age'] == '0':
                            self.cookies.pop(key, None)
                        else:
                            self.cookies[key] = item.value
            data = json.loads(raw) if raw and response.getheader('Content-Type', '').startswith('application/json') else raw
            if path.startswith('/api/welcome/auth/') and isinstance(data, dict) and 'csrf' in data:
                self.csrf = data['csrf']
            return response.status, data, dict(response_headers)
        finally:
            connection.close()

    def copy(self):
        result = Browser(self.port)
        result.cookies, result.csrf = self.cookies.copy(), self.csrf
        return result


class WelcomeProfileSessionTests(unittest.TestCase):
    service_evidence = []

    @classmethod
    def setUpClass(cls):
        cls.service_evidence = []
        cls.temp = tempfile.TemporaryDirectory(prefix='navren-welcome-dq-isolated-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.fixture = Path(cls.temp.name)
        cls.worker_log = (cls.fixture / 'candidate.stderr').open('w+')
        cls.addClassCleanup(cls.worker_log.close)
        environment = os.environ.copy()
        environment['NAVREN_PERSONAL_DATA_KEY'] = Fernet.generate_key().decode()
        environment['NAVREN_KNOWLEDGE_DB'] = str(cls.fixture / 'knowledge.sqlite3')
        cls.worker = subprocess.Popen([sys.executable, '-u', '-c', CANDIDATE_WORKER,
            str(CANDIDATE), str(cls.fixture)], cwd=CANDIDATE, env=environment,
            stdout=subprocess.PIPE, stderr=cls.worker_log, text=True)
        cls.addClassCleanup(cls.stop_worker)
        ready, _, _ = select.select([cls.worker.stdout], [], [], 25)
        if not ready:
            raise RuntimeError('isolated Base did not become ready')
        line = cls.worker.stdout.readline()
        if not line:
            cls.worker_log.flush(); cls.worker_log.seek(0)
            raise RuntimeError('isolated Base failed: ' + cls.worker_log.read()[-4000:])
        cls.candidate = json.loads(line)
        cls.base_port = cls.candidate['port']
        if cls.base_port in {18767, 18768}:
            raise AssertionError('live_port_forbidden')

    @classmethod
    def stop_worker(cls):
        if cls.worker.poll() is None:
            cls.worker.terminate()
            try: cls.worker.wait(5)
            except subprocess.TimeoutExpired:
                cls.worker.kill(); cls.worker.wait(5)
        if cls.worker.stdout:
            cls.worker.stdout.close()

    @classmethod
    def tearDownClass(cls):
        connection = HTTPConnection('127.0.0.1', cls.base_port, timeout=5)
        try:
            connection.request('GET', '/api/test/source-evidence')
            response = connection.getresponse()
            if response.status != 200:
                raise AssertionError('candidate_input_evidence_unavailable')
            inputs = json.loads(response.read())
        finally:
            connection.close()
        welcome_end = hashlib.sha256((ROOT / 'welcome_auth.py').read_bytes()).hexdigest()
        stable = inputs['inputs_stable'] and welcome_end == WELCOME_SOURCE_AT_IMPORT
        print('WELCOME_PROFILE_SESSION_EVIDENCE=' + json.dumps({
            'scope': 'isolated_candidate_HTTP_not_live',
            **inputs, 'welcome_source_start': WELCOME_SOURCE_AT_IMPORT,
            'welcome_source_end': welcome_end, 'run_inputs_stable': stable,
            'base_random_port': cls.base_port, 'live_posts': 0, 'shared_restarts': 0,
            'paid_calls': 0, 'cases': cls.service_evidence}, ensure_ascii=False), flush=True)
        if not stable:
            raise AssertionError('run_invalidated_input_hash_changed: ' + repr(inputs['changed_inputs']))

    def setUp(self):
        for filename in ('response.ready', 'response.release', 'profile-write.ready', 'profile-write.release',
                         'stream.ready', 'stream.release', 'select.ready', 'select.release',
                         'pause-select', 'dispatch-count', 'logout-failure', 'selected-tokens.json',
                         'pause-select-after-commit', 'select-after-commit.ready', 'select-after-commit.release'):
            (self.fixture / filename).unlink(missing_ok=True)
        self.root = self.fixture / self._testMethodName
        self.root.mkdir()
        welcome = self.root / 'outputs/welcome-scene/index.html'
        welcome.parent.mkdir(parents=True); welcome.write_text('<!doctype html><title>隔离欢迎入口</title>')
        self.service = create_welcome_server(0, self.root / 'welcome.sqlite3', root=self.root,
            upstream_port=self.base_port, allow_unverified_registration=True,allow_legacy_experience=True)
        self.assertNotIn(self.service.server_port, {18767, 18768})
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.service.server_close)
        self.addCleanup(self.service.shutdown)
        self.browser = Browser(self.service.server_port)
        self.assertEqual(self.browser.request('GET', '/api/welcome/auth/session')[0], 200)

    def experience(self, account='tycoon', browser=None):
        browser = browser or self.browser
        status, data, _ = browser.request('POST', '/api/welcome/auth/experience', {'user_id': account})
        self.assertEqual(status, 200, data)
        self.assertTrue(data['authenticated'])
        return data

    def bank_cookie(self):
        session = self.service.auth.session(self.browser.cookies[COOKIE])
        return self.service.auth.upstream_cookie(session['user_id'])

    def base(self, method, path, body=None, cookie='', csrf=None):
        browser = Browser(self.base_port)
        headers = {'Cookie': cookie}
        if csrf: headers['X-QZ-CSRF'] = csrf
        return browser.request(method, path, body, headers)

    def count(self, table, *, welcome=False):
        database = self.service.auth.database if welcome else self.fixture / 'bank.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def active_base_sessions(self):
        with closing(sqlite3.connect(self.fixture / 'bank.sqlite3')) as db:
            return db.execute('SELECT COUNT(*) FROM demo_sessions WHERE revoked=0 AND expires_at>?',
                (time.time(),)).fetchone()[0]

    def selected_tokens(self):
        path = self.fixture / 'selected-tokens.json'
        return json.loads(path.read_text()) if path.exists() else []

    def wait_marker(self, name):
        deadline = time.monotonic() + 6
        while not (self.fixture / (name + '.ready')).exists():
            if time.monotonic() > deadline:
                self.fail('隔离请求没有抵达预定延迟点: ' + name)
            time.sleep(.01)

    def release(self, name):
        (self.fixture / (name + '.release')).touch()

    def logout(self, browser=None):
        status, data, headers = (browser or self.browser).request('POST', '/api/welcome/auth/logout', {})
        self.assertEqual(status, 200, data)
        self.assertFalse(data['authenticated'])
        self.assertEqual(data['redirect'], '/?view=experience')
        self.assertEqual(data['scope'], 'welcome_subject')
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        return data

    def revoked_base(self, cookie):
        status, data, _ = self.base('GET', '/api/finance/session', cookie=cookie)
        self.assertEqual((status, data['error']['code']), (409, 'SESSION_CHANGED'))

    def test_current_public_session_tracks_application_switch_without_new_workspace(self):
        original_count = self.count('workspaces')
        first = self.experience()
        first_cookie = self.bank_cookie()
        _, current, _ = self.browser.request('GET', '/api/finance/session')
        status, selected, _ = self.browser.request('POST', '/api/finance/demo/select', {
            'user_id': 'grain_farmer', 'expected_workspace_id': current['workspace_id'],
            'expected_privacy_revision': current['privacy_revision']}, {'X-QZ-CSRF': current['csrf']})
        self.assertEqual(status, 200, selected)
        status, public, _ = self.browser.request('GET', '/api/welcome/auth/session')
        self.assertEqual(status, 200)
        self.assertEqual(public['auth_subject'], first['auth_subject'])
        self.assertEqual(public['entry_experience_account']['user_id'], 'tycoon')
        self.assertEqual(public['experience_account']['user_id'], 'grain_farmer')
        self.assertEqual(public['current_business_identity']['account_id'], 'grain_farmer')
        self.assertEqual(public['current_business_identity']['workspace_id'], selected['workspace_id'])
        self.assertEqual(public['business_identity_status'], 'verified')
        self.assertNotIn('csrf', public['current_business_identity'])
        self.assertNotEqual(self.bank_cookie(), first_cookie)
        self.assertEqual(self.count('workspaces'), original_count)
        self.revoked_base(first_cookie)
        self.service_evidence.append({'case': 'application_switch_current_public_identity',
            'actual_selected_account': 'grain_farmer', 'stable_welcome_subject': True,
            'workspace_count_unchanged': True, 'old_base_cookie': '409 SESSION_CHANGED'})

    def test_existing_four_cards_reuse_subject_and_existing_workspace(self):
        original_count = self.count('workspaces')
        first = self.experience('working_landlady')
        old_cookie = self.bank_cookie()
        user_count = self.count('welcome_users', welcome=True)
        selected = self.experience('retired_entrepreneur')
        self.assertEqual(selected['auth_subject'], first['auth_subject'])
        self.assertEqual(selected['entry_experience_account']['user_id'], 'working_landlady')
        self.assertEqual(selected['experience_account']['user_id'], 'retired_entrepreneur')
        self.assertEqual(self.count('welcome_users', welcome=True), user_count)
        self.assertEqual(self.count('workspaces'), original_count)
        self.revoked_base(old_cookie)

    def test_session_identity_read_does_not_bootstrap_missing_business_cookie(self):
        status, local, _ = self.browser.request('POST', '/api/welcome/auth/register', {
            'phone': '13800138001', 'password': 'welcome-testing-passphrase'})
        self.assertEqual(status, 201, local)
        before = self.count('demo_sessions')
        for _ in range(2):
            status, current, _ = self.browser.request('GET', '/api/welcome/auth/session')
            self.assertEqual(status, 200)
            self.assertIsNone(current['current_business_identity'])
            self.assertEqual(current['business_identity_status'], 'not_connected')
            self.assertEqual(current['auth_subject'], local['auth_subject'])
        self.assertEqual(self.count('demo_sessions'), before)
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 0)

    def test_logout_revokes_business_token_and_all_shared_welcome_sessions(self):
        self.browser.request('POST', '/api/welcome/auth/register', {
            'phone': '13800138002', 'password': 'welcome-testing-passphrase'})
        _, current, _ = self.browser.request('GET', '/api/finance/session')
        cookie, csrf = self.bank_cookie(), current['csrf']
        old_welcome = self.browser.copy()
        identity = self.service.auth.session(self.browser.cookies[COOKIE])
        other_token, _ = self.service.auth.new_session(identity['user_id'])
        other = Browser(self.service.server_port); other.cookies[COOKIE] = other_token
        self.logout()
        self.revoked_base(cookie)
        self.assertEqual(old_welcome.request('GET', '/api/finance/account-profile')[0], 401)
        self.assertEqual(other.request('GET', '/api/finance/account-profile')[0], 401)
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 0)
        self.assertEqual(self.count('welcome_sessions', welcome=True), 0)
        self.assertEqual(self.count('welcome_users', welcome=True), 1)
        status, error, _ = self.base('POST', '/api/finance/account-profile/display', {
            'display_name': '退出后的旧请求', 'avatar': 'stone', 'confirmed': True,
            'expected_version': 'obsolete'}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, error['error']['code']), (409, 'SESSION_CHANGED'))
        self.service_evidence.append({'case': 'complete_logout_real_base_and_welcome',
            'base_old_cookie': '409 SESSION_CHANGED', 'welcome_old_cookies': '401',
            'subject_sessions_and_jar_deleted': True, 'local_credentials_preserved': True})

    def test_real_inflight_profile_save_is_rejected_after_logout(self):
        self.experience()
        _, current, _ = self.base('GET', '/api/finance/session', cookie=self.bank_cookie())
        cookie = self.bank_cookie()
        _, profile, _ = self.base('GET', '/api/finance/account-profile', cookie=cookie)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.base, 'POST', '/api/finance/account-profile/display', {
                'display_name': '迟到退出保存', 'avatar': 'stone', 'confirmed': True,
                'expected_version': profile['expected_version']}, cookie, current['csrf'])
            try:
                self.wait_marker('profile-write')
                self.logout()
            finally:
                self.release('profile-write')
            status, error, _ = future.result(timeout=8)
        self.assertEqual((status, error['error']['code']), (409, 'SESSION_CHANGED'))
        _, new_session, headers = self.base('POST', '/api/finance/session', {})
        _, unchanged, _ = self.base('GET', '/api/finance/account-profile',
            cookie=headers['Set-Cookie'].split(';', 1)[0])
        self.assertEqual(unchanged['display'], profile['display'])
        # expected_version 绑定会话，重新建立令牌后应变化；比较真正的资料版本。
        self.assertEqual(unchanged['bank_version'], profile['bank_version'])
        self.assertEqual(unchanged['authorization'], profile['authorization'])
        self.service_evidence.append({'case': 'already_authenticated_profile_write_after_real_logout',
            'result': '409 SESSION_CHANGED', 'display_and_data_revisions_unchanged': True})

    def test_late_response_cannot_restore_cookie_or_private_body_after_logout(self):
        self.experience(); old = self.browser.copy(); cookie = self.bank_cookie()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old.request, 'GET', '/api/test/delayed-response')
            try:
                self.wait_marker('response')
                self.logout()
            finally:
                self.release('response')
            status, data, _ = future.result(timeout=8)
        self.assertEqual(status, 401, data)
        self.assertNotIn('old_private_body', data)
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 0)
        self.assertEqual(self.count('welcome_users', welcome=True), 0)
        self.revoked_base(cookie)
        self.service_evidence.append({'case': 'late_upstream_cookie_and_body_after_logout',
            'reply': '401', 'old_body_suppressed': True, 'jar_not_resurrected': True})

    def test_late_response_after_account_switch_cannot_overwrite_new_cookie(self):
        self.experience(); old = self.browser.copy()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old.request, 'GET', '/api/test/delayed-response')
            try:
                self.wait_marker('response')
                self.experience('grain_farmer')
                new_cookie = self.bank_cookie()
            finally:
                self.release('response')
            status, data, _ = future.result(timeout=8)
        self.assertEqual(status, 409, data)
        self.assertNotIn('old_private_body', data)
        self.assertEqual(self.bank_cookie(), new_cookie)
        _, public, _ = self.browser.request('GET', '/api/welcome/auth/session')
        self.assertEqual(public['current_business_identity']['account_id'], 'grain_farmer')

    def test_request_guarded_before_logout_is_not_dispatched_after_logout(self):
        self.experience(); stale = self.browser.copy()
        entered, release = threading.Event(), threading.Event()
        original = WelcomeHandler._guard
        def delayed_guard(handler, path):
            value = original(handler, path)
            if path == '/api/test/dispatch-count' and value:
                entered.set()
                if not release.wait(8): raise AssertionError('guard delay not released')
            return value
        with patch.object(WelcomeHandler, '_guard', delayed_guard), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stale.request, 'GET', '/api/test/dispatch-count')
            try:
                self.assertTrue(entered.wait(6))
                self.logout()
            finally:
                release.set()
            status, data, _ = future.result(timeout=8)
        self.assertEqual(status, 401, data)
        self.assertFalse((self.fixture / 'dispatch-count').exists())

    def test_stream_stops_old_private_tail_after_logout(self):
        self.experience(); cookie = self.bank_cookie()
        connection = HTTPConnection('127.0.0.1', self.service.server_port, timeout=10)
        try:
            connection.request('GET', '/api/test/stream', headers={
                'Cookie': COOKIE + '=' + self.browser.cookies[COOKIE]})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            first = response.read(len(b'data: first\n\n'))
            self.assertEqual(first, b'data: first\n\n')
            self.wait_marker('stream')
            self.logout()
            self.release('stream')
            tail = response.read()
            self.assertNotIn(b'old-private-tail', tail)
            self.revoked_base(cookie)
        finally:
            self.release('stream'); connection.close()

    def test_pending_anonymous_experience_cannot_resurrect_logged_out_session(self):
        active_before = self.active_base_sessions()
        (self.fixture / 'pause-select').touch()
        stale = self.browser.copy()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stale.request, 'POST', '/api/welcome/auth/experience', {'user_id': 'tycoon'})
            try:
                self.wait_marker('select')
                self.logout()
            finally:
                self.release('select')
            status, data, _ = future.result(timeout=8)
        self.assertIn(status, (401, 403, 409), data)
        self.assertFalse(data.get('authenticated', False))
        self.assertEqual(self.count('welcome_users', welcome=True), 0)
        self.assertEqual(self.count('welcome_sessions', welcome=True), 0)
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 0)
        tokens = self.selected_tokens()
        self.assertEqual(len(tokens), 1)
        self.revoked_base('qz_bank_user=' + tokens[0])
        self.assertEqual(self.active_base_sessions(), active_before)
        self.service_evidence.append({'case': 'anonymous_selection_after_public_logout',
            'welcome_not_resurrected': True, 'unclaimed_base_token': '409 SESSION_CHANGED',
            'active_base_session_count_unchanged': True})

    def test_two_anonymous_experience_commits_create_only_one_welcome_subject(self):
        active_before = self.active_base_sessions()
        barrier = threading.Barrier(2)
        original = self.service.auth.experience_session
        def competing_commit(*args, **kwargs):
            barrier.wait(8)
            return original(*args, **kwargs)
        self.service.auth.experience_session = competing_commit
        first, second = self.browser.copy(), self.browser.copy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.request, 'POST', '/api/welcome/auth/experience', {'user_id': account})
                for client, account in ((first, 'tycoon'), (second, 'grain_farmer'))]
            replies = [future.result(timeout=12) for future in futures]
        self.assertEqual(sum(status == 200 and data.get('authenticated') is True for status, data, _ in replies), 1)
        self.assertEqual(self.count('welcome_users', welcome=True), 1)
        self.assertEqual(self.count('welcome_sessions', welcome=True), 1)
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 1)
        tokens = self.selected_tokens()
        self.assertEqual(len(tokens), 2)
        active = []
        for token in tokens:
            status, reply, _ = self.base('GET', '/api/finance/session', cookie='qz_bank_user=' + token)
            if status == 200:
                active.append(token)
            else:
                self.assertEqual((status, reply['error']['code']), (409, 'SESSION_CHANGED'))
        self.assertEqual(len(active), 1)
        winner = next(client for client, reply in zip((first, second), replies) if reply[0] == 200)
        welcome = self.service.auth.session(winner.cookies[COOKIE])
        self.assertEqual(self.service.auth.upstream_cookie(welcome['user_id']), 'qz_bank_user=' + active[0])
        self.assertEqual(self.active_base_sessions(), active_before + 1)
        self.service_evidence.append({'case': 'competing_anonymous_selection_CAS',
            'one_welcome_subject': True, 'one_claimed_base_token': True,
            'losing_unclaimed_token': '409 SESSION_CHANGED'})
        self.logout(winner)
        self.assertEqual(self.active_base_sessions(), active_before)

    def test_logout_waits_for_committed_proxy_switch_and_revokes_new_token(self):
        self.experience()
        old_cookie = self.bank_cookie()
        _, current, _ = self.base('GET', '/api/finance/session', cookie=old_cookie)
        (self.fixture / 'pause-select-after-commit').touch()
        logout_entered = threading.Event()
        original_auth = WelcomeHandler._auth
        def observe_logout(handler, path):
            if handler.server is self.service and path.endswith('/logout'):
                logout_entered.set()
            return original_auth(handler, path)
        select_client, logout_client = self.browser.copy(), self.browser.copy()
        with patch.object(WelcomeHandler, '_auth', observe_logout), ThreadPoolExecutor(max_workers=2) as pool:
            selected = pool.submit(select_client.request, 'POST', '/api/finance/demo/select', {
                'user_id': 'grain_farmer', 'expected_workspace_id': current['workspace_id'],
                'expected_privacy_revision': current['privacy_revision']}, {'X-QZ-CSRF': current['csrf']})
            try:
                self.wait_marker('select-after-commit')
                new_token = self.selected_tokens()[-1]
                new_cookie = 'qz_bank_user=' + new_token
                self.assertEqual(self.base('GET', '/api/finance/session', cookie=new_cookie)[0], 200)
                self.assertEqual(self.bank_cookie(), old_cookie)
                logout = pool.submit(self.logout, logout_client)
                self.assertTrue(logout_entered.wait(5))
                time.sleep(.1)
                self.assertFalse(logout.done(), '退出必须等待已提交切换的新 Cookie 入 jar')
            finally:
                self.release('select-after-commit')
            logout.result(timeout=8)
            # 切号响应可已送出，或被随后完成的退出阻断；两者不能保留新授权。
            try:
                status, reply, _ = selected.result(timeout=8)
                self.assertIn(status, (200, 401, 409), reply)
            except HTTPException:
                pass  # Content-Length 已发送后撤销，连接直接关闭也会阻断旧响应。
        self.revoked_base(old_cookie)
        self.revoked_base(new_cookie)
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 0)
        self.assertEqual(self.count('welcome_sessions', welcome=True), 0)
        self.assertEqual(self.count('welcome_users', welcome=True), 0)
        self.service_evidence.append({'case': 'logout_during_committed_proxy_switch_before_headers',
            'logout_waited_for_identity_exchange': True, 'new_base_token_before_logout': '200',
            'old_and_new_base_tokens_after_logout': '409 SESSION_CHANGED',
            'welcome_sessions_and_jar_cleared': True})

    def test_logout_transaction_rechecks_rotated_token_and_preserves_new_login(self):
        self.browser.request('POST', '/api/welcome/auth/register', {
            'phone': '13800138003', 'password': 'welcome-testing-passphrase'})
        old_token = self.browser.cookies[COOKIE]
        old_identity = self.service.auth.session(old_token)
        user_id = old_identity['user_id']
        self.service.auth.update_upstream_cookies(user_id, ['qz_bank_user=transaction-fixture'])
        new_token, new_identity = self.service.auth.new_session(user_id, old_token)
        with self.assertRaises(AuthError) as denied:
            self.service.auth.logout(old_token, expected_user_id=user_id)
        self.assertEqual((denied.exception.status, denied.exception.code), (401, 'WELCOME_SESSION_REVOKED'))
        self.assertIsNotNone(self.service.auth.session(new_token))
        self.assertEqual(self.service.auth.upstream_cookie(user_id), 'qz_bank_user=transaction-fixture')
        self.assertEqual(self.count('welcome_users', welcome=True), 1)
        self.assertEqual(self.count('welcome_sessions', welcome=True), 1)

    def test_already_revoked_business_cookie_can_finish_public_logout(self):
        self.experience()
        cookie = self.bank_cookie()
        _, current, _ = self.base('GET', '/api/finance/session', cookie=cookie)
        status, reply, _ = self.base('POST', '/api/finance/session/logout', {}, cookie, current['csrf'])
        self.assertEqual(status, 200, reply)
        self.revoked_base(cookie)
        _, public, _ = self.browser.request('GET', '/api/welcome/auth/session')
        self.assertIsNone(public['current_business_identity'])
        self.assertIsNone(public['experience_account'])
        self.assertEqual(public['business_identity_status'], 'revoked')
        self.logout()
        self.assertEqual(self.count('welcome_upstream_cookies', welcome=True), 0)
        self.assertEqual(self.count('welcome_users', welcome=True), 0)

    def assert_failed_logout_is_retryable(self, mode):
        self.experience()
        old_token, old_cookie = self.browser.cookies[COOKIE], self.bank_cookie()
        failure = self.fixture / 'logout-failure'; failure.write_text(mode)
        status, data, _ = self.browser.request('POST', '/api/welcome/auth/logout', {})
        self.assertGreaterEqual(status, 400, data)
        self.assertNotEqual(data.get('authenticated'), False)
        self.assertEqual(self.browser.cookies[COOKIE], old_token)
        self.assertIsNotNone(self.service.auth.session(old_token))
        self.assertEqual(self.bank_cookie(), old_cookie)
        self.assertEqual(self.base('GET', '/api/finance/session', cookie=old_cookie)[0], 200)
        failure.unlink()
        self.logout(); self.revoked_base(old_cookie)

    def test_business_logout_unavailable_preserves_welcome_and_jar_for_retry(self):
        self.assert_failed_logout_is_retryable('503')

    def test_missing_business_logout_route_does_not_claim_success(self):
        self.assert_failed_logout_is_retryable('404')

    def test_business_logout_missing_cookie_proof_does_not_claim_success(self):
        self.assert_failed_logout_is_retryable('missing-cookie')

    def test_business_logout_false_revocation_proof_does_not_claim_success(self):
        self.assert_failed_logout_is_retryable('false-proof')

    def test_business_logout_ack_with_still_valid_token_does_not_claim_success(self):
        self.assert_failed_logout_is_retryable('still-active')


if __name__ == '__main__':
    unittest.main(verbosity=2)
