"""既有候选确认的正式临时 HTTP 及篡改/并发控制；不重跑原设置八项。"""
from contextlib import closing
import http.client
import json
import unittest
from unittest.mock import patch

import test_memory_settings_response as fixtures


class PersonalMutationResponseTests(unittest.TestCase):
    observations = []
    setUp = fixtures.MemorySettingsResponseTests.setUp
    state = fixtures.MemorySettingsResponseTests.state
    change_privacy = fixtures.MemorySettingsResponseTests.change_privacy

    def request(self, path, payload):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST', path, json.dumps(payload).encode(), {
                'Content-Type': 'application/json', 'Origin': self.base,
                'Cookie': 'qz_bank_user=' + self.token, 'X-QZ-CSRF': self.csrf})
            response = conn.getresponse()
            result = json.loads(response.read())
            self.observations.append({'case': self._testMethodName, 'path': path,
                                      'status': response.status, 'response': result})
            return response.status, result
        finally:
            conn.close()

    def candidate(self):
        status, body = self.request('/api/finance/memories/settings',
                                    {'enabled': True, 'expected_revision': 0})
        self.assertEqual(status, 200, body)
        proposals = self.g.personal_data.propose_candidates(self.w,
            '我的长期目标是毕业前存下学费', 'isolated-candidate-source', expected_revision=1)
        self.assertEqual(len(proposals), 1)
        return {k: proposals[0][k] for k in
                ('proposal_id', 'source_ref', 'confirmation_token', 'expected_revision')}

    def test_valid_candidate_confirmation_returns_its_committed_response(self):
        payload = self.candidate()
        status, body = self.request('/api/finance/memories', payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(body['workspace_id'], self.w)
        self.assertEqual((body['personal_revision'], body['privacy_revision']), (2, 2))
        self.assertEqual(body['items'][0]['value'], '我的长期目标是毕业前存下学费')
        self.assertEqual(body['items'][0]['source_ref'], payload['source_ref'])
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 2, 'privacy_revision': 2})

    def test_tampered_candidate_fields_and_token_never_commit(self):
        payload = self.candidate()
        for altered, code, expected_status in (
                ({**payload, 'value': '客户端改写'}, 'INVALID_MEMORY_CONFIRMATION', 400),
                ({**payload, 'source_ref': 'wrong-source'}, 'INVALID_MEMORY_CONFIRMATION', 409),
                ({**payload, 'confirmation_token': '0' * 32}, 'INVALID_MEMORY_CONFIRMATION', 409)):
            with self.subTest(code=code, fields=tuple(altered)):
                status, body = self.request('/api/finance/memories', altered)
                self.assertEqual(status, expected_status, body)
                self.assertEqual(body['error']['code'], code)
                self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 1})
                self.assertEqual(self.g.memories.state(self.w)['items'], [])

    def test_expired_candidate_and_unrelated_source_remain_rejected(self):
        payload = self.candidate()
        with closing(self.g.personal_data.connect()) as db, db:
            db.execute('UPDATE memory_candidates SET expires_at=? WHERE workspace_id=?',
                       (self.g.bank.clock() - 1, self.w))
        status, body = self.request('/api/finance/memories', payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'INVALID_MEMORY_CONFIRMATION')
        with closing(self.g.personal_data.connect()) as db, db:
            db.execute('UPDATE memory_candidates SET expires_at=? WHERE workspace_id=?',
                       (self.g.bank.clock() + 86400, self.w))
            db.execute('UPDATE conversation_sources SET text=? WHERE workspace_id=?',
                       (self.g.personal_data.protect('这是一条无关来源。'), self.w))
        status, body = self.request('/api/finance/memories', payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'INVALID_MEMORY_SOURCE')
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 1})

    def test_external_privacy_change_before_candidate_commit_denies_old_request(self):
        payload = self.candidate()
        original = self.g.memories.create
        def changed(*args, **kwargs):
            self.change_privacy()
            return original(*args, **kwargs)
        with patch.object(self.g.memories, 'create', side_effect=changed):
            status, body = self.request('/api/finance/memories', payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'PERSONAL_PRIVACY_CONFLICT')
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 1, 'privacy_revision': 2})
        self.assertEqual(self.g.memories.state(self.w)['items'], [])

    def test_external_privacy_change_after_candidate_commit_denies_old_response(self):
        payload = self.candidate()
        original = self.g.memories.create
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.change_privacy()
            return result
        with patch.object(self.g.memories, 'create', side_effect=changed):
            status, body = self.request('/api/finance/memories', payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'PRIVACY_REVISION_CHANGED')
        self.assertEqual(self.state(), {'enabled': True, 'personal_revision': 2, 'privacy_revision': 3})
        self.assertEqual(len(self.g.memories.state(self.w)['items']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
