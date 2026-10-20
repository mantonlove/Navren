"""公开CLI注册链：真实隔离子进程和HTTP，禁止外部网络、密钥进程及收费调用。"""
from contextlib import closing
from functools import partial
from http.client import HTTPConnection
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FINANCIAL = ROOT / 'outputs/realistic-history/navren-financial.sqlite3'
FORBIDDEN_PORTS = {60291, 60262, 18767, 18768, 65363, 65463}
OBSERVED = []


def free_port():
    while True:
        with closing(socket.socket()) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port


def request(port, method, path, body=None, extra=None):
    authority = f'127.0.0.1:{port}'
    headers = {'Host': authority, 'Origin': 'http://' + authority, 'Content-Type': 'application/json'}
    headers.update(extra or {})
    with closing(HTTPConnection('127.0.0.1', port, timeout=3)) as client:
        client.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = client.getresponse()
        data = response.read()
        return response.status, data, dict(response.getheaders())


class RegisteredCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='navren-registered-cli-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.state = cls.directory / 'state'
        cls.state.mkdir()
        # 只保留系统运行变量；真实凭据不传给本测试或子进程。
        cls.environment = {key: value for key, value in os.environ.items()
                           if key in {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR', 'SHELL'}}
        cls.environment.update(
            AIPING_API_KEY='isolated-fixture-never-call-model',
            DASHSCOPE_API_KEY='isolated-fixture-never-call-cloud-voice',
            DASHSCOPE_API_HOST='fixture.cn-beijing.maas.aliyuncs.com',
            NAVREN_PERSONAL_DATA_KEY=Fernet.generate_key().decode(),
            NAVREN_SMS_CONFIG=str(cls.directory / 'no-real-sms-config.json'),
            NAVREN_KNOWLEDGE_DB=str(cls.state / 'ordinary-knowledge.sqlite3'),
            NAVREN_PUBLIC_DATA_DIR=str(cls.directory / 'public'),
            NAVREN_PUBLIC_SYNC_ENABLED='false', NAVREN_MESSAGE_WORKER_ENABLED='false',
            NAVREN_LIVING_WORKER_ENABLED='false', NAVREN_ALLOW_CLOUD_ASR='0',
            PYTHONDONTWRITEBYTECODE='1',
        )
        guard = patch.dict(os.environ, cls.environment, clear=True)
        guard.start()
        cls.addClassCleanup(guard.stop)
        original = socket.socket.connect

        def only_local(sock, address):
            if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', 'localhost', '::1'}:
                raise AssertionError('external_network_forbidden')
            return original(sock, address)

        guard = patch.object(socket.socket, 'connect', only_local)
        guard.start()
        cls.addClassCleanup(guard.stop)
        from finance_gateway import FinanceGateway
        gateway = FinanceGateway(cls.state / 'ordinary-bank.sqlite3',
                                 financial_database=FINANCIAL, registered_mode=True)
        gateway.stop_public_sync()
        for name in ('ordinary-auth.sqlite3', 'market.sqlite3'):
            with closing(sqlite3.connect(cls.state / name)):
                pass
        cls.financial_sha = hashlib.sha256(FINANCIAL.read_bytes()).hexdigest()
        guard_dir = cls.directory / 'process-guards'
        guard_dir.mkdir()
        # 仅隔离子进程加载；不替换应用HTTP、认证或注册provider。
        (guard_dir / 'sitecustomize.py').write_text('''import socket, subprocess
_connect = socket.socket.connect
_dns = socket.getaddrinfo
def local_connect(sock, address):
    if not isinstance(address, tuple) or address[0] not in {'127.0.0.1','localhost','::1'}:
        raise AssertionError('external_network_forbidden')
    return _connect(sock, address)
def local_dns(host, *args, **kwargs):
    if host not in {'127.0.0.1','localhost','::1'}:
        raise AssertionError('external_dns_forbidden')
    return _dns(host, *args, **kwargs)
def no_process(*args, **kwargs):
    raise AssertionError('secret_or_external_process_forbidden')
socket.socket.connect = local_connect
socket.getaddrinfo = local_dns
subprocess.Popen = no_process
subprocess.run = no_process
subprocess.check_output = no_process
''')
        cls.environment['PYTHONPATH'] = str(guard_dir) + os.pathsep + str(ROOT)

    @classmethod
    def tearDownClass(cls):
        if hashlib.sha256(FINANCIAL.read_bytes()).hexdigest() != cls.financial_sha:
            raise AssertionError('read_only_synthetic_source_changed')

    def cli_arguments(self, directory):
        port, upstream = free_port(), free_port()
        while upstream == port:
            upstream = free_port()
        return port, upstream, [
            '--state-dir', str(self.state), '--financial-database', str(FINANCIAL),
            '--market-database', str(self.state / 'market.sqlite3'),
            '--runtime-dir', str(directory), '--port', str(port), '--upstream-port', str(upstream),
        ]

    def test_public_cli_help_exposes_same_registered_launcher_options(self):
        for entry in ('server.py', 'scripts/serve_financial_frontend.py'):
            with self.subTest(entry=entry):
                result = subprocess.run([sys.executable, '-B', str(ROOT / entry), '--help'],
                                        env=self.environment, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])
                for option in ('--state-dir', '--upstream-port', '--runtime-dir', '--prepare'):
                    self.assertIn(option, result.stdout)
                self.assertNotIn('--allow-unverified', result.stdout)
                self.assertNotIn('--legacy', result.stdout)

    def test_public_cli_missing_state_fails_before_listening(self):
        runtime = self.directory / 'missing-state-runtime'
        port, upstream, args = self.cli_arguments(runtime)
        args[1] = str(self.directory / 'missing-state')
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'server.py'), *args],
                                env=self.environment, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((runtime / 'runtime.json').exists())
        for selected in (port, upstream):
            with closing(socket.socket()) as sock:
                self.assertNotEqual(sock.connect_ex(('127.0.0.1', selected)), 0)

    def check_registered_entry(self, entry):
        name = Path(entry).stem
        runtime = self.directory / (name + '-runtime')
        port, upstream, args = self.cli_arguments(runtime)
        log_path = self.directory / (name + '-process.log')
        with log_path.open('wb') as log:
            process = subprocess.Popen([sys.executable, '-B', str(ROOT / entry), *args],
                                       env=self.environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 35
            while not (runtime / 'runtime.json').exists():
                if process.poll() is not None:
                    self.fail('cli_exited:' + log_path.read_text()[-2500:])
                if time.monotonic() >= deadline:
                    self.fail('cli_not_ready_before_deadline')
                time.sleep(.05)
            receipt = json.loads((runtime / 'runtime.json').read_text())
            self.assertTrue(receipt['registered_experience_mode'])
            self.assertTrue(receipt['runtime_enabled'])
            self.assertEqual(receipt['model_checks'], 0)
            self.assertFalse(receipt['sms_configured'])
            self.assertEqual(Path(receipt['reused_databases']['bank']), (self.state / 'ordinary-bank.sqlite3').resolve())
            with closing(sqlite3.connect(self.state / 'ordinary-bank.sqlite3')) as db:
                count_before = db.execute('SELECT COUNT(*) FROM registered_user_instances').fetchone()[0]
            status, html, _ = request(port, 'GET', '/?view=register')
            self.assertEqual(status, 200)
            self.assertIn(b'welcomePasswordConfirmation', html)
            self.assertEqual(request(port, 'GET', '/chat.html')[0], 303)
            for selected in (port, upstream):
                for extra in ({}, {'X-Registered-User-Id': 'f' * 32, 'X-Workspace-Id': 'f' * 32}):
                    status, _, headers = request(selected, 'POST', '/api/finance/session', {}, extra)
                    self.assertEqual(status, 401)
                    self.assertNotIn('qz_bank_user=', headers.get('Set-Cookie', ''))
                self.assertEqual(request(selected, 'GET', '/api/finance/overview')[0], 401)
            status, data, headers = request(port, 'GET', '/api/welcome/auth/session')
            self.assertEqual(status, 200)
            state = json.loads(data)
            self.assertFalse(state['authenticated'])
            self.assertTrue(state['sms']['registration_required'])
            self.assertFalse(state['sms']['phone_verified'])
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            # 没有验证码来源和验证证明时不得建立已注册身份；不向短信发送接口发请求。
            status, _, _ = request(port, 'POST', '/api/welcome/auth/register',
                {'phone': '13800000001', 'password': 'isolated-fixture-password',
                 'password_confirmation': 'isolated-fixture-password'},
                {'Cookie': cookie, 'X-Welcome-CSRF': state['csrf']})
            self.assertIn(status, (400, 422, 503))
            with closing(sqlite3.connect(self.state / 'ordinary-bank.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM registered_user_instances').fetchone()[0], count_before)
            with closing(sqlite3.connect(self.state / 'ordinary-auth.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM welcome_users').fetchone()[0], 0)
            self.assertFalse((runtime / 'model-calls.jsonl').exists())
            OBSERVED.append({'entry': entry, 'registered_mode': True, 'welcome_and_upstream_anonymous_status': 401,
                             'verified_registration_required': True, 'private_instances_created': 0,
                             'actual_cli_subprocess': True, 'runtime_output_isolated': True})
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, log_path.read_text()[-2500:])

    def test_registered_launcher_denies_anonymous_private_entry(self):
        self.check_registered_entry('scripts/serve_financial_frontend.py')

    def test_public_server_cli_denies_anonymous_private_entry(self):
        self.check_registered_entry('server.py')

    def test_explicit_internal_legacy_factory_remains_available(self):
        from base_runtime.service_assembly import AssembledGateway
        from server import create_server
        from voice_gateway import VoiceGateway
        database = self.directory / 'isolated-legacy.sqlite3'
        instance = create_server(port=0, database=database, voice=VoiceGateway(key='', url=''),
                                 conversation_agent=object(),
                                 finance_factory=partial(AssembledGateway, registered_mode=False))
        worker = threading.Thread(target=instance.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        try:
            self.assertFalse(instance.finance.registered_mode)
            status, _, headers = request(instance.server_port, 'POST', '/api/finance/session', {})
            self.assertEqual(status, 200)
            self.assertIn('qz_bank_user=', headers['Set-Cookie'])
        finally:
            instance.shutdown()
            instance.server_close()
            worker.join(3)


if __name__ == '__main__':
    result = unittest.main(exit=False).result
    target = os.environ.get('NAVREN_CLI_TEST_RECEIPT')
    if target:
        Path(target).write_text(json.dumps({'status': 'passed' if result.wasSuccessful() else 'failed',
            'test_count': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'observed': OBSERVED, 'real_sms': 0, 'paid_models': 0, 'cloud_voice': 0,
            'live_service_restarted': False, 'source_fixture_only': str(FINANCIAL)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
