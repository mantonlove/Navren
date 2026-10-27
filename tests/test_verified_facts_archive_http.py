"""免费脚本模型驱动真实隔离 HTTP；核对只读事实发布和重启后的归档。"""
import json
import os
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from agent_runtime.conversation import ConversationAgent
from local_knowledge.runtime import EvidenceRuntime
from local_knowledge.store import LocalKnowledgeProvider
from server import create_server, ProxyHandler
from voice_gateway import VoiceGateway
from test_native_conversation import ScriptedPrimary, Reviewer, call, fact_reply
from simulated_bank import BankError
from user_memory import privacy_revision


class VerifiedFactsArchiveHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-facts-history-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(self.root / 'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB': '', 'NAVREN_PUBLIC_SYNC_ENABLED': 'false',
            'NAVREN_MESSAGE_WORKER_ENABLED': 'false', 'NAVREN_LIVING_WORKER_ENABLED': 'false'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.quiet = patch.object(ProxyHandler, 'log_message', lambda *_: None)
        self.quiet.start()
        self.addCleanup(self.quiet.stop)
        self.errors = []
        self.error_seen = threading.Event()
        def record_error(_, fmt, *args):
            self.errors.append(fmt % args)
            self.error_seen.set()
        self.error_log = patch.object(ProxyHandler, 'log_error', record_error)
        self.error_log.start()
        self.addCleanup(self.error_log.stop)
        self.registered_user_id = uuid.uuid4().hex
        self.primary = ScriptedPrimary(call('account_summary', {}), fact_reply)
        self.reviewer = Reviewer()
        self.agent = ConversationAgent(self.primary, self.reviewer)
        self.start_server()
        self.addCleanup(self.stop_server)

    def start_server(self):
        evidence = EvidenceRuntime(LocalKnowledgeProvider(self.root / 'knowledge.sqlite3'), background=False)
        self.server = create_server(0, self.root / 'bank.sqlite3',
            voice=VoiceGateway(key='', allow_cloud_asr=False), conversation_agent=self.agent,
            evidence_runtime=evidence, market_database=self.root / 'market.sqlite3',
            public_sync_enabled=False)
        self.gateway = self.server.finance
        if not hasattr(self, 'token'):
            self.workspace, self.token = self.gateway.create_session()
        self.gateway.configure_dialogue_archive(lambda workspace, _db: {
            'registered_user_id': self.registered_user_id, 'workspace_id': workspace,
            'persona_id': 'tycoon'})
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        if getattr(self, 'server', None) is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.server = None

    def read(self, path, body=None):
        headers = {'Origin': self.base, 'Cookie': 'qz_bank_user=' + self.token,
                   'X-QZ-CSRF': self.gateway.csrf(self.token)}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)
        except urllib.error.HTTPError as error:
            calls = [[item.get('function', {}).get('name') for item in message.get('tool_calls', [])]
                     for batch in self.primary.messages for message in batch if message['role'] == 'assistant']
            self.fail(f'HTTP {error.code}: {error.read().decode()} server_errors={self.errors} primary_calls={calls}')

    def test_host_verified_facts_are_readonly_after_restart_without_review(self):
        answer = self.read('/api/agent', {'text': '我还有多少钱'})
        self.assertFalse(answer['isFallback'])
        self.assertEqual(answer['answer_contract']['kind'], 'verified_facts')
        self.assertEqual(answer['agent_review']['status'], 'not_required')
        self.assertEqual(self.reviewer.packets, [])
        before = self.read('/api/finance/dialogue/history')['items'][0]['assistant']
        self.assertEqual(before['status'], 'published_verified_facts')
        self.assertEqual(before['content'], answer['reply'])
        self.assertEqual(before['original_review'], {
            'source': 'host_verified_facts', 'status': 'not_required',
            'reviewer_model': None, 'review_id': None})
        self.assertTrue(before['historical_readonly'])
        self.assertFalse(before['current_fact_eligible'])
        self.assertFalse(before['execution_authority'])
        self.assertEqual(before['publication']['state'], 'server_send_completed')
        self.stop_server()
        self.start_server()
        after = self.read('/api/finance/dialogue/history')['items'][0]['assistant']
        self.assertEqual(after['status'], 'published_verified_facts')
        self.assertEqual(after['content'], before['content'])
        self.assertEqual(self.reviewer.packets, [])

    def test_post_send_attestation_failure_never_withdraws_sent_reply(self):
        attestation_seen = threading.Event()
        def reject_attestation(*_args, **_kwargs):
            attestation_seen.set()
            raise RuntimeError('synthetic_attestation_failure')
        with (patch.object(self.gateway.dialogue_archive, 'attest_published',
                           side_effect=reject_attestation) as attestation,
              patch.object(self.gateway, 'discard_unpublished_answer') as withdraw):
            answer = self.read('/api/agent', {'text': '我还有多少钱'})
            self.assertTrue(attestation_seen.wait(3), self.errors)
            self.assertTrue(self.error_seen.wait(3), self.errors)
        self.assertFalse(answer['isFallback'])
        self.assertEqual(answer['answer_contract']['kind'], 'verified_facts')
        self.assertEqual(attestation.call_count, 1, self.errors)
        withdraw.assert_not_called()
        history = self.read('/api/finance/dialogue/history')['items'][0]['assistant']
        self.assertNotEqual(history.get('status'), 'published_verified_facts')
        self.assertIsNone(history.get('content'))

    def test_tampered_fact_reply_cannot_reuse_same_turn_witness(self):
        answer = self.read('/api/agent', {'text': '我还有多少钱'})
        self.assertEqual(self.read('/api/finance/dialogue/history')['items'][0]['assistant']['status'],
                         'published_verified_facts')
        entry = self.gateway.approved_responses._pending[(self.workspace, answer['bank_response_id'])]
        tampered = deepcopy(entry['packet']['original_response'])
        tampered['reply'] = '伪造答复'
        with self.assertRaises(BankError) as caught:
            self.gateway.approved_responses._verify_facts_witness(
                self.workspace, answer['bank_response_id'], 'zh', tampered,
                entry['packet']['facts_witness'])
        self.assertEqual(caught.exception.code, 'VERIFIED_FACTS_WITNESS_INVALID')

    def test_sent_local_reply_keeps_speech_receipt_when_attestation_fails(self):
        attestation_seen = threading.Event()
        def reject_attestation(*_args, **_kwargs):
            attestation_seen.set()
            raise RuntimeError('synthetic_local_attestation_failure')
        with (patch.object(self.gateway.dialogue_archive, 'attest_published',
                           side_effect=reject_attestation) as attestation,
              patch.object(self.gateway, 'discard_unpublished_answer') as withdraw):
            answer = self.read('/api/agent', {'text': '你好'})
            self.assertTrue(attestation_seen.wait(3), self.errors)
            self.assertTrue(self.error_seen.wait(3), self.errors)
        self.assertEqual(answer['answer_contract']['kind'], 'social_reply')
        receipt = answer['speech_receipt']
        self.assertIn(receipt, self.gateway._speech_receipts)
        self.assertEqual(attestation.call_count, 1)
        withdraw.assert_not_called()
        self.assertEqual(self.primary.messages, [])

    def test_missing_original_stays_neutral_after_restart(self):
        with closing(self.gateway.bank.connect()) as db:
            privacy = privacy_revision(db, self.workspace)
        personal = self.gateway.personal_data.revision(self.workspace)
        rid = uuid.uuid4().hex
        with self.gateway.demo_sessions.request(self.token, self.workspace):
            self.gateway.dialogue_archive.archive_user_turn(
                self.workspace, rid, '合成旧问题', references=[], media_ids=[], attachments=[],
                expected_personal_revision=personal, expected_privacy_revision=privacy)
        before = self.read('/api/finance/dialogue/history')['items'][0]['assistant']
        self.assertEqual(before['status'], 'archive_unavailable')
        self.assertIsNone(before['content'])
        self.assertFalse(before['current_fact_eligible'])
        self.assertFalse(before['execution_authority'])
        self.stop_server()
        self.start_server()
        after = self.read('/api/finance/dialogue/history')['items'][0]['assistant']
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
