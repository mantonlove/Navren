"""自填账户资料的独立加密持久化、版本、隔离与同源 HTTP 边界。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import http.server
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from cryptography.fernet import Fernet
from PIL import Image

from finance_gateway import FinanceGateway, COOKIE
from personal_data import PersonalDataStore
from personal_profile import PersonalProfile, FIELDS
from server import ProxyHandler
from simulated_bank import BankError, SimulatedBank
from user_memory import privacy_revision


NOW = datetime.fromisoformat('2026-10-14T12:00:00+08:00').timestamp()


class PersonalProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-profile-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = Fernet.generate_key()
        self.env = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': self.key.decode(),
            'NAVREN_KNOWLEDGE_DB': str(self.root / 'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bank = SimulatedBank(self.root / 'bank.sqlite3', clock=lambda: NOW)
        self.store = PersonalDataStore(self.bank, encryption_key=self.key)
        self.service = PersonalProfile(SimpleNamespace(bank=self.bank, personal_data=self.store))
        self.a = self.bank.create_workspace()
        self.b = self.bank.create_workspace()
        self.w = self.a['workspace_id']
        self.other = self.b['workspace_id']

    def payload(self, revision=0, **changes):
        return {'expected_revision': revision,
                'profile': {**{field: '' for field in FIELDS}, 'nickname': '隔离测试昵称', **changes}}

    def error(self, code, operation):
        with self.assertRaises(BankError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def bank_snapshot(self):
        with closing(self.bank.connect()) as db:
            return '\n'.join(db.iterdump())

    @staticmethod
    def avatar(fmt='PNG'):
        stream = io.BytesIO()
        Image.new('RGB', (4, 4), (120, 140, 160)).save(stream, format=fmt)
        mime = {'PNG': 'png', 'JPEG': 'jpeg', 'WEBP': 'webp'}[fmt]
        return 'data:image/' + mime + ';base64,' + base64.b64encode(stream.getvalue()).decode()

    def test_default_is_unpersisted_and_uses_only_trusted_optional_identity_name(self):
        result = self.service.read(self.w)
        self.assertEqual(result['workspace_binding'], self.w)
        self.assertEqual(result['profile'], {
            **{field: '' for field in FIELDS}, 'nickname': '我的账户',
            'revision': 0, 'updated_at': None})
        named = self.service.read(self.w, identity={'display_name': '受控身份昵称'})
        self.assertEqual(named['profile']['nickname'], '受控身份昵称')
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM personal_profiles').fetchone()[0], 0)

    def test_encrypted_roundtrip_and_restart_persistence(self):
        body = self.payload(full_name='隔离测试姓名', phone='+86 138 0000 0000',
                            email='profile.test@example.com', city='隔离测试城市',
                            occupation='隔离职业', company='隔离单位', birthday='2000-02-29',
                            gender='undisclosed', bio='第一行\n第二行', avatar=self.avatar())
        result = self.service.save(self.w, body)['profile']
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['updated_at'], '2026-10-14T12:00:00+08:00')
        self.assertEqual({key: result[key] for key in FIELDS}, body['profile'])
        with sqlite3.connect(self.store.database) as db:
            raw = db.execute('SELECT value FROM personal_profiles WHERE workspace_id=?', (self.w,)).fetchone()[0]
        self.assertTrue(raw.startswith('pd1:'))
        self.assertNotIn(body['profile']['full_name'], raw)
        self.assertNotIn(body['profile']['full_name'].encode(), self.store.database.read_bytes())
        reopened = PersonalDataStore(self.bank, encryption_key=self.key)
        service = PersonalProfile(SimpleNamespace(bank=self.bank, personal_data=reopened))
        self.assertEqual(service.read(self.w)['profile'], result)

    def test_workspace_isolation_and_clearing_optional_fields(self):
        self.service.save(self.w, self.payload(email='profile.test@example.com', avatar=self.avatar()))
        self.assertEqual(self.service.read(self.other)['profile']['revision'], 0)
        second = self.service.save(self.other, self.payload(nickname='另一工作区'))
        self.assertEqual(second['profile']['revision'], 1)
        cleared = self.service.save(self.w, self.payload(1))['profile']
        self.assertEqual(cleared['revision'], 2)
        self.assertEqual(cleared['avatar'], '')
        self.assertEqual(cleared['email'], '')
        self.assertEqual(self.service.read(self.other)['profile']['nickname'], '另一工作区')

    def test_stale_and_concurrent_saves_do_not_overwrite(self):
        self.service.save(self.w, self.payload())
        self.error('PERSONAL_PROFILE_VERSION_CONFLICT', lambda: self.service.save(self.w, self.payload()))
        gate = threading.Barrier(2)
        def save(nickname):
            gate.wait()
            try:
                self.service.save(self.w, self.payload(1, nickname=nickname))
                return 'saved'
            except BankError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ('并发昵称甲', '并发昵称乙')))
        self.assertEqual(sorted(results), ['PERSONAL_PROFILE_VERSION_CONFLICT', 'saved'])
        self.assertEqual(self.service.read(self.w)['profile']['revision'], 2)

    def test_profile_revision_has_no_bank_kyc_risk_memory_or_authorization_side_effect(self):
        before = self.bank_snapshot()
        personal_revision = self.store.revision(self.w)
        with closing(self.bank.connect()) as db:
            privacy = privacy_revision(db, self.w)
        self.service.save(self.w, self.payload(full_name='自填姓名', phone='13800000000', gender='female'))
        self.assertEqual(self.bank_snapshot(), before)
        self.assertEqual(self.store.revision(self.w), personal_revision)
        with closing(self.bank.connect()) as db:
            self.assertEqual(privacy_revision(db, self.w), privacy)
        for table in ('user_memory_settings', 'user_memories', 'profile_observations'):
            with closing(self.store.connect()) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_strict_profile_payload_and_version(self):
        for body in ({**self.payload(), 'workspace_id': self.other},
                     {**self.payload(), 'account_id': self.other},
                     {**self.payload(), 'profile': {**self.payload()['profile'], 'risk_level': 5}},
                     {**self.payload(), 'profile': {'nickname': '缺少字段'}}):
            self.error('INVALID_FIELDS', lambda body=body: self.service.save(self.w, body))
        for revision in (None, True, -1, '0', 2**63):
            self.error('PERSONAL_PROFILE_VERSION_REQUIRED',
                       lambda revision=revision: self.service.save(self.w, self.payload(revision)))
        self.error('NOT_FOUND', lambda: self.service.save('unknown-workspace', self.payload()))

    def test_invalid_text_length_date_phone_email_gender_leave_nothing_saved(self):
        invalid = [
            {'nickname': ''}, {'nickname': 'x' * 41}, {'full_name': 'x' * 81},
            {'bio': 'x' * 501}, {'city': 12}, {'company': '含\x00字符'},
            {'occupation': '不能\n换行'}, {'phone': 'not-a-phone'}, {'phone': '123'},
            {'phone': '1' * 21}, {'email': 'missing-at'}, {'email': 'x@bad'},
            {'email': 'x..y@example.com'}, {'email': 'x@bad-.com'},
            {'birthday': '2001-02-29'}, {'birthday': '2026-10-15'},
            {'birthday': '2026-1-2'}, {'gender': 'other'},
        ]
        for changes in invalid:
            with self.subTest(field=next(iter(changes))):
                self.error('PERSONAL_PROFILE_INVALID', lambda: self.service.save(self.w, self.payload(**changes)))
        self.assertEqual(self.service.read(self.w)['profile']['revision'], 0)

    def test_avatar_accepts_real_raster_and_rejects_svg_disguises_damage_and_size(self):
        revision = 0
        for fmt in ('PNG', 'JPEG', 'WEBP'):
            revision = self.service.save(self.w, self.payload(revision, avatar=self.avatar(fmt)))['profile']['revision']
        invalid = [
            'data:image/svg+xml;base64,' + base64.b64encode(b'<svg/>').decode(),
            'data:image/png;base64,' + base64.b64encode(b'<svg/>').decode(),
            self.avatar().replace('image/png', 'image/jpeg'),
            'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n').decode(),
            'data:image/png;base64,' + base64.b64encode(b'x' * (400 * 1024 + 1)).decode(),
            'data:image/png;base64,not-valid',
        ]
        for avatar in invalid:
            self.error('PERSONAL_PROFILE_INVALID', lambda: self.service.save(self.w, self.payload(revision, avatar=avatar)))
        self.assertEqual(self.service.read(self.w)['profile']['revision'], revision)

    def test_actual_http_routes_bind_session_csrf_envelope_and_reject_identity_override(self):
        gateway = FinanceGateway(self.root / 'http-bank.sqlite3',
                                 public_catalog=SimpleNamespace(sync_status=lambda: {}))
        first = gateway.bank.create_workspace()
        second = gateway.bank.create_workspace()
        class QuietHandler(ProxyHandler):
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        server.finance = gateway
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(credentials=first, body=None, suffix='', csrf=True, cookie=True):
            headers = {'Origin': base, 'Content-Type': 'application/json'}
            if cookie:
                headers['Cookie'] = COOKIE + '=' + credentials['session_token']
            if csrf:
                headers['X-QZ-CSRF'] = gateway.csrf(credentials['session_token'])
            req = urllib.request.Request(base + '/api/finance/personal-profile' + suffix,
                data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                response = opener.open(req, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), response.headers
        status, result, headers = request()
        self.assertEqual(status, 200)
        self.assertEqual(result['workspace_id'], first['workspace_id'])
        self.assertEqual(headers['X-Navren-Workspace'], first['workspace_id'])
        self.assertEqual(str(result['privacy_revision']), headers['X-Navren-Privacy-Revision'])
        status, result, _ = request(body=self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(result['profile']['revision'], 1)
        status, result, _ = request()
        self.assertEqual(status, 200)
        self.assertEqual(result['profile']['nickname'], '隔离测试昵称')
        status, result, _ = request(second)
        self.assertEqual(status, 200)
        self.assertEqual(result['profile']['revision'], 0)
        for kwargs, code, expected_status in (
            ({'body': self.payload(1), 'csrf': False}, 'CSRF_DENIED', 403),
            ({'body': {**self.payload(1), 'workspace_id': second['workspace_id']}}, 'INVALID_FIELDS', 400),
            ({'suffix': '?workspace_id=' + second['workspace_id']}, 'INVALID_QUERY', 400),
            ({'body': self.payload(1), 'suffix': '?account_id=' + second['workspace_id']}, 'INVALID_QUERY', 400),
            ({'cookie': False}, 'UNAUTHENTICATED', 401),
            ({'body': self.payload(0)}, 'PERSONAL_PROFILE_VERSION_CONFLICT', 409),
        ):
            status, result, _ = request(**kwargs)
            self.assertEqual(status, expected_status)
            self.assertEqual(result['error']['code'], code)


if __name__ == '__main__':
    unittest.main()
