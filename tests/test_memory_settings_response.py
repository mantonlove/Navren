"""正式临时 HTTP 入口的设置提交回执及相关拒绝；不调用模型或现役库。"""
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import create_server, ProxyHandler
from window_integration import semantic_oracle as oracle
from user_memory import invalidate_graph, privacy_revision
from voice_gateway import VoiceGateway


class NoModel:
    def process(self, *args, **kwargs):
        raise AssertionError('memory_settings_must_not_call_model')


class MemorySettingsResponseTests(unittest.TestCase):
    observations = []

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-memory-settings-')
        self.addCleanup(self.temp.cleanup)
        env = oracle._isolated_environment(Path(self.temp.name))
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        self.worker = patch.dict(os.environ, {'NAVREN_MESSAGE_WORKER_ENABLED': 'false'})
        self.worker.start()
        self.addCleanup(self.worker.stop)
        quiet = patch.object(ProxyHandler, 'log_message', lambda *args: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        catalog = oracle._NoNetworkCatalog()
        catalog.stop = lambda **kwargs: True
        self.server = create_server(0, Path(self.temp.name) / 'bank.sqlite3',
            market_database=Path(self.temp.name) / 'market.sqlite3',
            conversation_agent=NoModel(), public_catalog=catalog, public_sync_enabled=False,
            voice=VoiceGateway(key='', allow_cloud_asr=False))
        self.g = self.server.finance
        self.w, self.token = self.g.create_session()
        self.csrf = self.g.csrf(self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def state(self):
        memory = self.g.memories.state(self.w)
        with closing(self.g.bank.connect()) as db:
            privacy = privacy_revision(db, self.w)
        return {'enabled': memory['enabled'], 'personal_revision': memory['personal_revision'],
                'privacy_revision': privacy}

    def request(self, payload=None):
        # 原诊断正文及其预期不变。
        payload = {'enabled': True, 'expected_revision': 0} if payload is None else payload
        body = json.dumps(payload).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST', '/api/finance/memories/settings', body, {
                'Content-Type': 'application/json', 'Origin': self.base,
                'Cookie': 'qz_bank_user=' + self.token, 'X-QZ-CSRF': self.csrf})
            response = conn.getresponse()
            result = json.loads(response.read())
            self.observations.append({'case': self._testMethodName,
                'request_body': body.decode(), 'request_sha256': hashlib.sha256(body).hexdigest(),
                'status': response.status, 'response': result})
            return response.status, result
        finally:
            conn.close()

    def change_privacy(self):
        with self.g.bank.transaction() as db:
            invalidate_graph(db, self.w)

    def revoke_session(self):
        self.g.demo_sessions.revoke_family(self.token)

    def test_original_success_returns_own_committed_versions(self):
        self.assertEqual(self.state(), {'enabled': False, 'personal_revision': 0, 'privacy_revision': 0})
        status, body = self.request()
        self.assertEqual(status, 200, body)
        self.assertTrue(body['enabled'])
        self.assertEqual(body['workspace_id'], self.w)
        self.assertEqual((body['personal_revision'], body['privacy_revision']), (1, 1))
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 1})
        self.assertNotIn('commit_receipts', body)

    def test_stale_personal_CAS_rejects_without_another_commit(self):
        self.assertEqual(self.request()[0], 200)
        status, body = self.request({'enabled': False, 'expected_revision': 0})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'PERSONAL_VERSION_CONFLICT')
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 1})

    def test_external_privacy_change_before_transaction_rejects_without_setting_write(self):
        original = self.g.memories.set_enabled
        def changed(*args, **kwargs):
            self.change_privacy()
            return original(*args, **kwargs)
        with patch.object(self.g.memories, 'set_enabled', side_effect=changed):
            status, body = self.request()
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'PERSONAL_PRIVACY_CONFLICT')
        self.assertEqual(self.state(), {'enabled': False, 'personal_revision': 0, 'privacy_revision': 1})

    def test_external_privacy_change_after_commit_still_blocks_final_response(self):
        original = self.g.memories.set_enabled
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.change_privacy()
            return result
        with patch.object(self.g.memories, 'set_enabled', side_effect=changed):
            status, body = self.request()
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'PRIVACY_REVISION_CHANGED')
        # 本次设置确实提交；409 只拒绝已经过时的响应。
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 2})

    def test_post_commit_state_read_cannot_adopt_another_personal_transaction(self):
        original = self.g.memories.state
        def changed(workspace):
            with self.g.memories.store.transaction(workspace, expected_revision=1) as db:
                db.execute('UPDATE user_memory_settings SET enabled=0 WHERE workspace_id=?', (workspace,))
            return original(workspace)
        with patch.object(self.g.memories, 'state', side_effect=changed):
            status, body = self.request()
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'PRIVACY_REVISION_CHANGED')
        self.assertEqual(self.state(), {'enabled': False, 'personal_revision': 2, 'privacy_revision': 2})

    def test_revoked_session_after_commit_still_blocks_final_response(self):
        original = self.g.memories.set_enabled
        def revoked(*args, **kwargs):
            result = original(*args, **kwargs)
            self.revoke_session()
            return result
        with patch.object(self.g.memories, 'set_enabled', side_effect=revoked):
            status, body = self.request()
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'SESSION_CHANGED')
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 1})

    def test_revoked_session_before_transaction_blocks_setting_write(self):
        original = self.g.memories.set_enabled
        def revoked(*args, **kwargs):
            self.revoke_session()
            return original(*args, **kwargs)
        with patch.object(self.g.memories, 'set_enabled', side_effect=revoked):
            status, body = self.request()
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'SESSION_CHANGED')
        self.assertEqual(self.state(), {'enabled': False, 'personal_revision': 0, 'privacy_revision': 0})

    def test_client_cannot_submit_an_internal_commit_receipt(self):
        status, body = self.request({'enabled': True, 'expected_revision': 0,
                                    'commit_receipts': [{'privacy_after': 1}]})
        self.assertEqual(status, 400, body)
        self.assertEqual(body['error']['code'], 'INVALID_FIELDS')
        self.assertEqual(self.state(), {'enabled': False, 'personal_revision': 0, 'privacy_revision': 0})


if __name__ == '__main__':
    unittest.main(verbosity=2)
