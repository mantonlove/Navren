"""正式ProxyHandler的真实HTTP、注册Cookie、CSRF、版本冲突及发送前拒绝。"""
from contextlib import closing, contextmanager
from copy import deepcopy
import base64
import http.server
import json
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

import test_account_state as account_fixture
import test_chat_media as media_fixture
from base_runtime.account_state import DEFAULTS
from server import ProxyHandler

CONNECT = socket.create_connection


class AccountStateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.f = account_fixture.AccountStateTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.g, self.a = self.f.g, self.f.a
        class Quiet(ProxyHandler):
            def log_message(self, *args): pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.server.finance = self.g
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.thread.join, 2); self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:'+str(self.server.server_port)

    def request(self, body=None, lease=None, token=True, headers=None, suffix='', path='/api/finance/account-state'):
        lease = lease or self.a
        h = {'Origin': self.base, 'Content-Type': 'application/json'}
        if token:
            t = self.f.f.token(lease)
            h.update({'Cookie': 'qz_bank_user='+t, 'X-QZ-CSRF': self.g.csrf(t),
                'X-Navren-Workspace': lease['workspace_id'], 'X-Navren-Privacy-Revision': str(lease['privacy_revision'])})
        h.update(headers or {})
        req = urllib.request.Request(self.base+path+suffix, data=json.dumps(body).encode() if body is not None else None, headers=h)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.object(socket, 'create_connection', CONNECT):
            try: response = opener.open(req, timeout=10)
            except urllib.error.HTTPError as e: response = e
            with response: return response.status, json.load(response), response.headers

    def test_actual_get_all_sections_cas_boundaries_compat_and_late_send(self):
        status, state, headers = self.request(); self.assertEqual(status, 200)
        self.assertEqual(state['registered_user_id'], self.f.f.owner)
        self.assertEqual(state['workspace_id'], self.a['workspace_id'])
        self.assertEqual(headers['X-Navren-Workspace'], self.a['workspace_id'])
        values = deepcopy(DEFAULTS)
        values['profile']['nickname'] = 'HTTP本人'; values['settings']['theme'] = 'dark'
        values['preferences']['weeklyReport'] = False; values['insurance']['topics'] = ['health', 'pension']
        values['favorites'] = [{'category': 'market_news', 'object_id': 'event-verified', 'object_kind': 'event', 'version': 2, 'text': '权威新闻'}]
        values['reading'] = [{'kind': 'weekly', 'id': '2026-W40', 'version': 3, 'source_scope': 'public', 'label': '历史周报'}]
        values['markers']['onboarded'] = True
        for section, value in values.items():
            status, state, _ = self.request({'expected_revision': state['revision'], 'section': section, 'value': value})
            self.assertEqual(status, 200, state)
            self.assertEqual(state['sections'][section], value)
        revision = state['revision']
        for kwargs, expected in [
            ({'body': {'section': 'settings', 'value': values['settings'], 'expected_revision': 0}}, 409),
            ({'body': {'section': 'settings', 'value': values['settings'], 'expected_revision': revision, 'owner': self.f.f.owner}}, 422),
            ({'body': {'section': 'settings', 'value': values['settings'], 'expected_revision': revision}, 'headers': {'X-Navren-Workspace': uuid.uuid4().hex}}, 409),
            ({'body': {'section': 'settings', 'value': values['settings'], 'expected_revision': revision}, 'headers': {'X-QZ-CSRF': 'wrong'}}, 403),
            ({'token': False}, 401), ({'suffix': '?owner='+self.f.f.owner}, 400),
        ]:
            status, body, _ = self.request(**kwargs); self.assertEqual(status, expected, body)
        status, old, _ = self.request(path='/api/finance/personal-profile'); self.assertEqual(status, 200)
        self.assertEqual(old['profile']['nickname'], 'HTTP本人')
        self.assertIsInstance(old['profile']['updated_at'], str)
        oldprofile = {k: old['profile'][k] for k in account_fixture.FIELDS}; oldprofile['nickname'] = '兼容入口本人'
        oldprofile['avatar'] = 'data:image/png;base64,'+base64.b64encode(media_fixture.image_bytes()).decode()
        status, body, _ = self.request({'expected_revision': old['profile']['revision'], 'profile': oldprofile}, path='/api/finance/personal-profile')
        self.assertEqual(status, 200, body)
        self.assertEqual(self.request()[1]['sections']['profile']['nickname'], '兼容入口本人')
        status, display, _ = self.request(path='/api/finance/account-profile'); self.assertEqual(status, 200, display)
        same_preset = {'expected_version': display['expected_version'], 'display_name': '旧界面仅改昵称',
                       'avatar': display['display']['avatar'], 'confirmed': True}
        status, changed_display, _ = self.request(same_preset, path='/api/finance/account-profile/display')
        self.assertEqual(status, 200, changed_display)
        kept = self.request()[1]['sections']['profile']
        self.assertEqual(kept['nickname'], '旧界面仅改昵称'); self.assertEqual(kept['avatar'], oldprofile['avatar'])
        status, rejected, _ = self.request({**same_preset, 'display_name': '迟到改名'}, path='/api/finance/account-profile/display')
        self.assertEqual(status, 409, rejected)
        self.assertEqual(self.request()[1]['sections']['profile'], kept)
        status, chosen, _ = self.request({'expected_version': changed_display['expected_version'], 'display_name': kept['nickname'],
            'avatar': 'wave', 'confirmed': True}, path='/api/finance/account-profile/display')
        self.assertEqual(status, 200, chosen)
        chosen_profile = self.request()[1]['sections']['profile']
        self.assertEqual(chosen_profile['avatar'], ''); self.assertEqual(chosen_profile['avatar_preset'], 'wave')
        original = self.g.account_state.send_guard
        changed = [False]
        @contextmanager
        def delayed(w, expected):
            if not changed[0]:
                changed[0] = True
                current = self.g.account_state.read(w)
                self.g.account_state.save(w, {'expected_revision': current['revision'], 'section': 'settings', 'value': values['settings']})
            with original(w, expected) as db: yield db
        with patch.object(self.g.account_state, 'send_guard', delayed):
            status, body, _ = self.request(); self.assertEqual(status, 409, body)
        self.g.experience_provider.revoke_subject(self.f.f.owner, self.f.f.subject)
        self.assertIn(self.request()[0], (401, 409))

    def test_registered_catalog_real_http_preserves_pinned_version_through_production_adapter(self):
        status, catalog, _ = self.request(path='/api/finance/demo/accounts')
        self.assertEqual(status, 200, catalog)
        with closing(self.g.bank.connect()) as bank:
            versions = {r['persona_id']: r['template_version'] for r in bank.execute(
                'SELECT persona_id,template_version FROM registered_user_instances WHERE registered_user_id=?', (self.f.f.owner,))}
        self.assertEqual(len(catalog['accounts']), 4)
        self.assertEqual({r['account_id']: r['source_version'] for r in catalog['accounts']}, versions)
        identity = self.g.session_data(self.a['workspace_id'], self.f.f.token(self.a))
        root = Path(__file__).resolve().parents[1]
        probe = root/'outputs/window-progress/base/account-data-completion-20261017/verify_catalog_adapter.js'
        result = subprocess.run(['node', str(probe)], input=json.dumps({'catalog': catalog, 'identity': identity}),
            text=True, capture_output=True, cwd=root, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['actual_catalog_accepted'])
        self.assertTrue(report['selection_dialog_opens'])
        self.assertTrue(report['missing_version_still_rejected'])
