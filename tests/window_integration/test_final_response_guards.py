"""冻结的四项晚到竞态：临时账本、真实本机 HTTP/WS、合成模型及语音。

原始账本、独立资金合同与正式指标版本为裁判。此组不是原十题正式
三轮，不验证真实模型语义或扬声器。禁止修改 owner 回归或业务实现。
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_cross_module as _cross
import test_market_voice_negotiation as _voice

from agent_runtime.conversation import ConversationAgent
from simulated_bank import BankError
from simulated_bank.service import digest
from voice_gateway import SpeechTask

ORACLE_PATH = _cross.ROOT / 'evaluation/window_integration/final_response_guard_oracles.json'
ORACLE_BYTES = ORACLE_PATH.read_bytes()
ORACLE = json.loads(ORACLE_BYTES)
ORACLE_HASH = hashlib.sha256(ORACLE_BYTES).hexdigest()
FROZEN_ORACLE_HASH = '7b9f23833feb841562c9f491fb898d10b31799f6c7bda01ba7681a9098d20011'
EXPECT = ORACLE['expectations']
CASE_IDS = {case['test']: case['id'] for case in ORACLE['cases']}


class BlockedAudioProvider(_voice.InMemoryAudioProvider):
    """先真实完成 SpeechTask 握手，再让首块 PCM 等待账户切换。"""
    def __init__(self):
        super().__init__(hold=True)
        self.blocked = threading.Event()
        self.release_audio = threading.Event()
        self.returned_audio = threading.Event()
        self.receives = 0

    def recv(self):
        self.receives += 1
        if self.receives == 1:
            return super().recv()
        if self.receives == 2:
            self.blocked.set()
            if not self.release_audio.wait(timeout=6):
                raise AssertionError('隔离上游未收到有界释放信号')
            self.returned_audio.set()
            return b'\x11\x00' * 64
        return super().recv()

    def close(self, timeout=1):
        self.release_audio.set()
        super().close(timeout)


class FinalResponseGuardIntegrationTests(unittest.TestCase):
    maxDiff = None
    raw_state = _cross.CrossModuleIntegrationTests.raw_state
    operation_count = _cross.CrossModuleIntegrationTests.operation_count
    action_payload = _cross.CrossModuleIntegrationTests.action_payload
    confirm_body = _cross.CrossModuleIntegrationTests.confirm_body
    assert_bank_error = _cross.CrossModuleIntegrationTests.assert_bank_error
    local_server = _voice.MarketVoiceNegotiationIntegrationTests.local_server
    http_json = _voice.MarketVoiceNegotiationIntegrationTests.http_json
    connect_ticket = _voice.MarketVoiceNegotiationIntegrationTests.connect_ticket
    need = _voice.MarketVoiceNegotiationIntegrationTests.need

    def setUp(self):
        self.assertEqual(ORACLE_HASH, FROZEN_ORACLE_HASH, '冻结预期不可按业务实现结果修改')
        self.assertEqual(len(CASE_IDS), 4)
        _cross.CrossModuleIntegrationTests.setUp(self)
        self.need(self.g.demo_sessions, 'select', 'request', 'validate')
        self.need(self.g.financial_metrics, 'snapshot', 'validate', 'bind_operation')

    def evidence(self, **values):
        test = self.id().split('.')[-1]
        print('WINDOW_FINAL_RESPONSE_GUARD_EVIDENCE ' + json.dumps({
            'case': CASE_IDS[test], 'test': test, 'oracle_sha256': ORACLE_HASH,
            'original_financial_contract_sha256': _cross.CONTRACT_HASH,
            'scope': 'temporary_synthetic_current_source',
            'formal_ten_questions_three_rounds': False, 'cloud_models_called': False,
            'workspace': self.w, **values}, ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def lookup_other_workspace(self, other):
        def lookup(user_id):
            self.assertEqual(user_id, 'grain_farmer', '不得回落真实四画像目录')
            self.assertNotEqual(other, self.w)
            return other
        return patch.object(self.g.demo_accounts, 'workspace_for', side_effect=lookup)

    def transaction_counts(self, operation):
        with closing(self.g.bank.connect()) as db:
            transactions = db.execute('SELECT count(*) FROM transactions WHERE workspace_id=? '
                                      'AND operation_id=?', (self.w, operation)).fetchone()[0]
            executed = db.execute("SELECT count(*) FROM audit WHERE workspace_id=? "
                                  "AND operation_id=? AND event='operation_executed'",
                                  (self.w, operation)).fetchone()[0]
        return {'transactions': transactions, 'executed_audits': executed}

    def revoked_token(self):
        self.assert_bank_error('SESSION_CHANGED', lambda: self.g.bank.session(self.token))
        with closing(self.g.bank.connect()) as db:
            row = db.execute('SELECT workspace_id,revoked FROM demo_sessions WHERE token_hash=?',
                             (digest(self.token),)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row['workspace_id'], row['revoked']), (self.w, 1))

    def test_authenticated_confirmation_after_account_switch_cannot_execute(self):
        other, _ = self.g.create_session()
        before, other_before = self.raw_state(), self.raw_state(other)
        original_confirm = self.g.confirm
        switches = []
        with self.local_server(), self.lookup_other_workspace(other):
            status, view = self.http_json('POST', '/api/finance/operations/prepare',
                self.action_payload(key='fg01-confirm-switch', amount_minor=EXPECT['transfer_minor']))
            self.assertEqual(status, 200, view)
            operation = view['operation']
            self.assertEqual(operation['payload']['amount_minor'], EXPECT['transfer_minor'])
            self.assertEqual(self.raw_state(), before, 'prepare 不得写资金或业务')
            body = self.confirm_body(view)

            def switch_after_authentication(owner, operation_id, payload):
                # 正式 dispatch 已验证 cookie/CSRF，当前请求上下文仍绑定 A。
                self.assertEqual((owner, operation_id), (self.w, operation['id']))
                switches.append(self.http_json('POST', '/api/finance/demo/select', {
                    'user_id': 'grain_farmer', 'expected_workspace_id': self.w}))
                self.assertEqual(switches[-1][0], 200, switches[-1])
                self.assertEqual(switches[-1][1]['workspace_id'], other)
                return original_confirm(owner, operation_id, payload)

            with patch.object(self.g, 'confirm', side_effect=switch_after_authentication):
                status, denied = self.http_json('POST',
                    '/api/finance/operations/' + operation['id'] + '/confirm', body)
            self.assertEqual(status, 409, denied)
            self.assertEqual(denied['error']['code'], 'SESSION_CHANGED')
        self.assertEqual(len(switches), 1, '必须实际经过已认证的 confirm 窗口')
        self.revoked_token()
        current = self.g.bank.operation(self.w, operation['id'])
        self.assertEqual(current['state'], 'pending_confirmation')
        self.assertFalse(current.get('result'), '拒绝不能留下成功回执')
        counts = self.transaction_counts(operation['id'])
        self.assertEqual(counts, {'transactions': 0, 'executed_audits': 0})
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.raw_state(other), other_before)
        self.assertEqual(self.operation_count(), 1)
        self.assertEqual(self.operation_count(other), 0)
        self.evidence(transport='actual_loopback_http', switch_status=switches[0][0],
            confirmation_status=status, failure=denied['error']['code'], operation=operation['id'],
            draft_state=current['state'], ledger=counts, old_token_revoked=True,
            two_workspaces_unchanged=True)

    def test_personal_revision_during_metric_binding_leaves_non_executable_draft(self):
        before = self.raw_state()
        original_bind = self.g.financial_metrics.bind_operation
        prior_revision = self.g.personal_data.revision(self.w)
        prior_snapshot = self.g.financial_metrics.snapshot(self.w, 60)
        observed = []
        payload = self.action_payload(key='fg02-unbound-draft', amount_minor=EXPECT['transfer_minor'])

        def changed_revision(owner, operation_id, snapshot):
            self.assertEqual(owner, self.w)
            self.assertEqual(snapshot['source_versions']['personal'], str(prior_revision))
            self.g.memories.create(owner, {'category': 'goal', 'value': '隔离目标修订，禁止沿用旧轮结论。',
                                          'expected_revision': prior_revision})
            observed.append(operation_id)
            return original_bind(owner, operation_id, snapshot)

        with self.local_server():
            with patch.object(self.g.financial_metrics, 'bind_operation', side_effect=changed_revision):
                status, denied = self.http_json('POST', '/api/finance/operations/prepare', payload)
            self.assertEqual(status, 409, denied)
            self.assertEqual(denied['error']['code'], 'METRIC_VERSION_CONFLICT')
            self.assertEqual(len(observed), 1)
            with closing(self.g.bank.connect()) as db:
                row = db.execute('SELECT id FROM operations WHERE workspace_id=? AND idempotency_key=?',
                                 (self.w, payload['idempotency_key'])).fetchone()
                self.assertIsNotNone(row)
                operation_id = row['id']
                required = db.execute('SELECT count(*) FROM base_operation_metric_requirements '
                                      'WHERE operation_id=? AND workspace_id=?', (operation_id, self.w)).fetchone()[0]
                bound = db.execute('SELECT count(*) FROM base_operation_metrics WHERE operation_id=?',
                                   (operation_id,)).fetchone()[0]
            self.assertEqual(operation_id, observed[0])
            self.assertEqual((required, bound), (1, 0))
            operation = self.g.bank.operation(self.w, operation_id)
            confirm_status, confirm_denied = self.http_json('POST',
                '/api/finance/operations/' + operation_id + '/confirm',
                self.confirm_body({'operation': operation}))
            self.assertEqual(confirm_status, 409, confirm_denied)
            self.assertEqual(confirm_denied['error']['code'], 'METRIC_BINDING_REQUIRED')
        revision = self.g.personal_data.revision(self.w)
        self.assertGreater(revision, prior_revision, '资料变更必须真实发生')
        after_snapshot = self.g.financial_metrics.snapshot(self.w, 60)
        self.assertNotEqual(prior_snapshot['source_versions'], after_snapshot['source_versions'])
        self.assertEqual(self.g.bank.operation(self.w, operation_id)['state'], 'pending_confirmation')
        counts = self.transaction_counts(operation_id)
        self.assertEqual(counts, {'transactions': 0, 'executed_audits': 0})
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.operation_count(), 1)
        self.evidence(transport='actual_loopback_http', prepare_status=status,
            prepare_failure=denied['error']['code'], confirm_status=confirm_status,
            confirm_failure=confirm_denied['error']['code'], personal_revision=[prior_revision, revision],
            operation=operation_id, required_rows=required, bound_rows=bound, ledger=counts,
            financial_state_unchanged=True)

    def test_actual_ws_withholds_old_audio_returned_after_account_switch(self):
        self.need(self.g, 'approve_speech', 'speech_guard')
        other, _ = self.g.create_session()
        before, other_before = self.raw_state(), self.raw_state(other)
        receipt = self.g.approve_speech(self.w, {
            'reply': '旧账户经过后端核验的隔离资金说明。', 'compliance_pass': True,
            'answer_contract': {'kind': 'offline_guard_fixture'},
            **self.g.session_data(self.w, self.token)}, self.token)
        self.assertIsInstance(receipt, str)
        provider = BlockedAudioProvider()

        def factory(kind, options, key, url):
            self.assertEqual(kind, 'tts')
            self.providers.append(provider)
            return SpeechTask(kind, options, key, url, connector=lambda *args, **kwargs: provider)

        frames, binary_bytes, slots = [], 0, 0
        with self.local_server() as server, self.lookup_other_workspace(other):
            server.voice.task_factory = factory
            status, session = self.http_json('POST', '/api/speech/session', {
                'kind': 'tts', 'speech_receipt': receipt})
            self.assertEqual(status, 200, session)
            ws = self.connect_ticket(session['path'])
            try:
                ws.send(json.dumps({'type': 'start', 'voice': 'female_sweet'}))
                ready = json.loads(ws.recv())
                self.assertEqual(ready['type'], 'ready')
                self.assertTrue(provider.blocked.wait(timeout=2), '上游必须先阻塞，不能验收空窗口')
                self.assertFalse(provider.returned_audio.is_set())
                switch_status, selected = self.http_json('POST', '/api/finance/demo/select', {
                    'user_id': 'grain_farmer', 'expected_workspace_id': self.w})
                self.assertEqual(switch_status, 200, selected)
                self.assertEqual(selected['workspace_id'], other)
                self.revoked_token()
                provider.release_audio.set()
                for _ in range(6):
                    frame = ws.recv()
                    if frame == '':
                        break
                    if isinstance(frame, bytes):
                        binary_bytes += len(frame)
                    else:
                        frames.append(json.loads(frame))
                self.assertEqual(frame, '', '拒绝旧音频后实际 WS 必须有界关闭')
                self.assertTrue(provider.returned_audio.wait(timeout=2), '旧 PCM 必须实际返回到 publish 守卫')
                self.assertEqual(binary_bytes, EXPECT['old_audio_bytes_published'])
                self.assertTrue(frames, '必须有明确拒绝事件，不能把无音频超时算通过')
                self.assertTrue(all(event.get('type') == 'error' and event.get('code') == 'SESSION_CHANGED'
                                    for event in frames), frames)
                self.assertTrue(provider.closed.wait(timeout=2), '旧语音上游必须关闭')
                for _ in range(3):
                    self.assertTrue(server.voice.slots.acquire(timeout=2))
                    slots += 1
                self.assertFalse(server.voice.slots.acquire(blocking=False))
            finally:
                provider.release_audio.set()
                ws.close()
                for _ in range(slots):
                    server.voice.slots.release()
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.raw_state(other), other_before)
        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.operation_count(other), 0)
        self.evidence(transport='actual_loopback_http_ws', upstream='blocked_synthetic_speech_task',
            initial_event=ready['type'], switch_status=switch_status, old_pcm_returned=True,
            published_audio_bytes=binary_bytes, events=frames, old_token_revoked=True,
            upstream_closed=provider.closed.is_set(), returned_slots=slots,
            two_workspaces_unchanged=True)

    def test_actual_agent_final_response_rejects_metric_change_during_review(self):
        before = self.raw_state()
        prior_profile = self.g.bank.profile(self.w)
        prior_metrics = self.g.financial_metrics.snapshot(self.w, 60)
        speech_before = set(self.g._speech_receipts)
        old_conclusion = '本轮旧约束支持这份安排，待审核批准后发布此隔离结论。'
        observed = []

        def answer_from_current_evidence(messages, tools):
            output = _cross.last_result(messages)
            return _cross.call('answer_reviewed', {'text': old_conclusion,
                'claims': [{'text': '本轮读取的账户约束依据。', 'evidence_ids': [output['evidence_id']]}],
                'draft_ids': [], 'followups': []})

        def change_metric_input_during_review():
            observed.append(self.g.financial_metrics.snapshot(self.w, 60))
            self.g.bank.profile(self.w, {'risk_level': prior_profile['risk_level'],
                'horizon_days': prior_profile['horizon_days'],
                'reserve_minor': prior_profile['reserve_minor'] + EXPECT['reserve_increment_minor'],
                'confirmed': True})

        primary = _cross.ScriptedPrimary([_cross.call('account_summary', {}), answer_from_current_evidence])
        reviewer = _cross.ScriptedReviewer(hook=change_metric_input_during_review)
        with self.local_server() as server:
            server.conversation_agent = ConversationAgent(primary, reviewer)
            status, denied = self.http_json('POST', '/api/agent', {
                'text': '依据本轮账户约束解释资金安排，先只分析，等待审核后展示。', 'mode': 'classic'})
        self.assertEqual(len(reviewer.packets), 1, '必须进入正式文字审核窗口')
        self.assertEqual(len(observed), 1)
        after_metrics = self.g.financial_metrics.snapshot(self.w, 60)
        # 与工具返回多少 metrics 无关，外部正式快照和独立底线合同作裁判。
        self.assertNotEqual(observed[0]['source_versions'], after_metrics['source_versions'])
        self.assertNotEqual(observed[0]['snapshot_id'], after_metrics['snapshot_id'])
        before_budget = prior_metrics['funding']['metrics']['current_budget']['value_minor']
        after_budget = after_metrics['funding']['metrics']['current_budget']['value_minor']
        self.assertEqual(before_budget - after_budget, EXPECT['reserve_increment_minor'])
        self.assertEqual(status, 409, denied)
        self.assertEqual(denied['error']['code'], 'METRIC_VERSION_CONFLICT')
        self.assertTrue(denied['isFallback'])
        self.assertNotIn(old_conclusion, json.dumps(denied, ensure_ascii=False))
        self.assertEqual(denied.get('bank_actions', []), [])
        self.assertNotIn('speech_receipt', denied)
        self.assertEqual(set(self.g._speech_receipts), speech_before)
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.operation_count(), 0)
        self.evidence(transport='actual_loopback_http_api_agent', review_calls=len(reviewer.packets),
            status=status, failure=denied['error']['code'], old_conclusion_published=False,
            speech_receipt_issued=False, operations=0, financial_state_unchanged=True,
            current_budget_minor=[before_budget, after_budget],
            reserve_increment_minor=EXPECT['reserve_increment_minor'],
            source_versions_before=observed[0]['source_versions'],
            source_versions_after=after_metrics['source_versions'],
            snapshot_ids=[observed[0]['snapshot_id'], after_metrics['snapshot_id']])


if __name__ == '__main__':
    unittest.main()
