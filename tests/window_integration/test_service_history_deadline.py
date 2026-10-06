"""先冻结、后在稳定候选运行：真实HTTP历史原件/CAS及慢TCP原轮截止。

只借用旧fixture的辅助方法，不继承旧TestCase或导入其测试类到本模块。
真实守卫和历史写入/撤回不mock；只替换免费模型、原生传输故障与时钟。
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_cross_module as _cross
import test_service_transport_commit as _transport
import agent_runtime.conversation as _conversation
from finance_gateway import COOKIE
from server import PublicationSocket

ROOT = _cross.ROOT
ORACLE_PATH = ROOT / 'evaluation/window_integration/service_history_deadline_oracles.json'
FROZEN_ORACLE_SHA = '07495f0a34bb8abcc1c24e8e482dcc4e159a40aaa42d109327c08e9245ce7aa9'
OUTPUT_ROOT = ROOT / 'outputs/window-progress/integration/service-history-deadline'
ORACLE = json.loads(ORACLE_PATH.read_text())
FIXTURE, EXPECT = ORACLE['fixture'], ORACLE['expectations']
CASES = {case['test']: case['id'] for case in ORACLE['cases']}
WAIT = FIXTURE['thread_timeout_seconds']


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha_file(path):
    return sha_bytes(path.read_bytes())


def source_manifest():
    paths = {path for path in ROOT.glob('*.py') if path.is_file()}
    for folder in ('agent_runtime', 'base_runtime', 'simulated_bank', 'financial_methods', 'local_knowledge'):
        paths.update((ROOT / folder).rglob('*.py'))
    paths.update(HERE / name for name in ('test_cross_module.py', 'test_market_voice_negotiation.py',
        'test_negotiation_consent.py', 'test_service_response_guard.py', 'test_service_transport_commit.py',
        'test_service_history_deadline.py'))
    paths.update((ORACLE_PATH, ROOT / 'evaluation/window_integration/service_transport_commit_oracles.json',
        ROOT / 'evaluation/window_integration/service_response_guard_oracles.json',
        ROOT / 'simulated_bank/fixtures/demo.json', ROOT / 'simulated_bank/service_benefit_rules.json',
        ROOT / 'outputs/window-progress/finance/solution-review/solution-v2.md'))
    return {str(path.relative_to(ROOT)): sha_file(path) for path in sorted(paths) if path.is_file()}


class RawSocketLayer:
    """只在真实socket.sendall接缝观察/注入，不替代PublicationSocket的检查。"""
    def __init__(self, raw, transmit):
        self.raw, self.transmit = raw, transmit

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def sendall(self, body, *args, **kwargs):
        return self.transmit(self.raw, bytes(body), *args, **kwargs)


class MonotonicPosition:
    """模块time代理，其他真实计时/等待不变；仅移动同一Agent单调截止的位置。"""
    def __init__(self):
        self.now = float(FIXTURE['synthetic_monotonic_start'])
        self.reads = []

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        self.reads.append(self.now)
        return self.now


class ServiceHistoryDeadlineContracts(unittest.TestCase):
    maxDiff = None
    raw_state = _transport.ServiceTransportCommitContracts.raw_state
    operation_count = _transport.ServiceTransportCommitContracts.operation_count
    public_fixture = _transport.ServiceTransportCommitContracts.public_fixture
    http_json = _transport.ServiceTransportCommitContracts.http_json
    actual_server = _transport.ServiceTransportCommitContracts.actual_server
    counts = _transport.ServiceTransportCommitContracts.counts
    no_side_effects = _transport.ServiceTransportCommitContracts.no_side_effects
    confirm_body = _transport.ServiceTransportCommitContracts.confirm_body

    @classmethod
    def setUpClass(cls):
        if sha_file(ORACLE_PATH) != FROZEN_ORACLE_SHA:
            raise RuntimeError('frozen_service_history_deadline_oracle_changed')
        if Path(sys.executable).absolute().parent.parent.name != '.venv':
            raise RuntimeError('project_venv_python_required')
        destination = os.environ.get('NAVREN_SERVICE_HISTORY_OUTPUT')
        cls.output = Path(destination).resolve() if destination else OUTPUT_ROOT / ('run-' + str(time.time_ns()))
        if not cls.output.is_relative_to(OUTPUT_ROOT.resolve()):
            raise RuntimeError('isolated_output_scope_required')
        cls.output.mkdir(parents=True, exist_ok=True)
        if (cls.output / 'suite-evidence.json').exists():
            raise RuntimeError('new_run_directory_required')
        cls.before_sources, cls.observations = source_manifest(), []
        (cls.output / 'source-before.json').write_text(json.dumps(cls.before_sources, indent=2) + '\n')

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        record = {'suite_id': ORACLE['suite_id'], 'oracle_sha256': FROZEN_ORACLE_SHA,
            'source_before': cls.before_sources, 'source_after': after,
            'source_unchanged': after == cls.before_sources, 'observations': cls.observations,
            'actual_cloud_calls': 0, 'real_model_semantics_verified': False,
            'real_tts_or_microphone_verified': False}
        (cls.output / 'suite-evidence.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        print('WINDOW_SERVICE_HISTORY_DEADLINE_SUMMARY ' + json.dumps({
            'oracle_sha256': FROZEN_ORACLE_SHA, 'source_unchanged': record['source_unchanged'],
            'observations': len(cls.observations), 'output': str(cls.output), 'actual_cloud_calls': 0},
            ensure_ascii=False), flush=True)
        if after != cls.before_sources:
            raise RuntimeError('service_history_deadline_source_changed_during_execution')

    def require(self, owner, *names):
        missing = [name for name in names if not callable(getattr(owner, name, None))]
        if missing:
            self.skipTest('BLOCKED:真实接口缺少 ' + type(owner).__name__ + '.' + ','.join(missing))

    def setUp(self):
        _transport.ServiceTransportCommitContracts.setUp(self)
        self.require(self.g, 'discard_unpublished_answer', 'publication_guard', 'publication_send_timeout')
        self.require(self.g.personal_data, 'save_turn_guarded', 'discard_turn_guarded')
        self.assertIs(self.g.memories.state(self.w)['enabled'], EXPECT['memory_enabled'])
        self.rounds, self.cleanups = [], []
        original = self.g.approve_speech

        def observe_round(workspace, result, session_token=None):
            receipt = original(workspace, result, session_token)
            self.rounds.append({'reply': result.get('reply'), 'response_id': result.get('bank_response_id'),
                'handler_thread_ident': threading.get_ident(),
                'receipt': receipt, 'operation_ids': [item['operation_id'] for item in result.get('bank_actions', [])
                    if item.get('operation_id')], 'widget_count': len(result.get('widgets', []))})
            return receipt
        recorder = patch.object(self.g, 'approve_speech', side_effect=observe_round)
        recorder.start()
        self.addCleanup(recorder.stop)

    def evidence(self, **values):
        record = {'case': CASES[self._testMethodName], 'test': self._testMethodName,
            'oracle_sha256': FROZEN_ORACLE_SHA, 'actual_cloud_calls': 0, **values}
        self.observations.append(record)
        print('WINDOW_SERVICE_HISTORY_DEADLINE_EVIDENCE ' + json.dumps(record,
            ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def round_for(self, reply):
        found = [record for record in self.rounds if record['reply'] == reply]
        self.assertEqual(len(found), 1, '必须到达真实本轮approve_speech，不能以前期回退代替')
        return found[0]

    def local_agent(self, server, reply, *, draft=False, reviewer_hook=None):
        def prepare(messages, schemas):
            outputs = [json.loads(message['content']) for message in messages if message['role'] == 'tool']
            accounts = next(output['data']['accounts'] for output in outputs if 'accounts' in output.get('data', {}))
            payment = next(output['data'] for output in outputs if 'beneficiaries' in output.get('data', {}))
            return _cross.call('prepare_operation', {'action': 'transfer',
                'account_id': next(row['id'] for row in accounts if row['id'].endswith('-main')),
                'beneficiary_id': next(row['id'] for row in payment['beneficiaries'] if row['alias'] == '妈妈'),
                'amount_minor': FIXTURE['transfer_minor']})

        def answer(messages, schemas):
            outputs = [json.loads(message['content']) for message in messages if message['role'] == 'tool']
            evidence = [output['evidence_id'] for output in outputs if 'evidence_id' in output]
            operations = [output['data']['operation']['id'] for output in outputs if 'operation' in output.get('data', {})]
            widgets = [output['data']['widget_id'] for output in outputs if 'widget_id' in output.get('data', {})]
            return _cross.call('answer_reviewed', {'text': reply,
                'claims': [{'text': '公开测试条款说明本金风险，服务预览须另行接受，日历为只读。',
                    'evidence_ids': evidence}], 'draft_ids': operations, 'widget_ids': widgets, 'followups': []})

        steps = [_cross.call('service_offer_preview', {}), _cross.call('knowledge_search', {
            'query': '风险', 'product_id': FIXTURE['public_product'], 'as_of': FIXTURE['public_as_of']}),
            _cross.call('calendar_query', {'source': 'simulation', 'from': FIXTURE['calendar_from'], 'to': FIXTURE['calendar_to']})]
        if draft:
            steps.extend([_cross.call('account_summary', {}), _cross.call('business_records', {'kind': 'payments'}), prepare])
        self.primary = _transport.LocalPrimary(steps + [answer])
        self.reviewer = _transport.LocalReviewer(hook=reviewer_hook)
        server.conversation_agent = _conversation.ConversationAgent(self.primary, self.reviewer,
            timeout_seconds=FIXTURE['original_turn_timeout_seconds'])
        self.assertEqual(server.conversation_agent.timeout_seconds, 105)
        return server.conversation_agent

    def request_wire(self, text):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port,
            timeout=FIXTURE['request_timeout_seconds'])
        try:
            connection.request('POST', '/api/agent', json.dumps({'text': text, 'mode': 'classic'}, ensure_ascii=False).encode('utf-8'),
                {'Content-Type': 'application/json', 'Accept': 'application/json', 'Origin': self.base,
                 'Cookie': COOKIE + '=' + self.token, 'X-QZ-CSRF': self.g.csrf(self.token)})
            response = connection.getresponse()
            partial = False
            try:
                body = response.read()
            except http.client.IncompleteRead as error:
                body, partial = error.partial, True
            return {'status': response.status, 'body': body, 'partial': partial}
        finally:
            connection.close()

    @staticmethod
    def parsed_body(wire):
        try:
            return json.loads(wire['body'])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def private_snapshot(self):
        # 原密文与原时点保留在内存；只对其hash留证，不日志正文/密文/receipt。
        with closing(self.g.personal_data.connect()) as db:
            dialogue = db.execute('SELECT messages AS messages_cipher,updated_at FROM agent_dialogues WHERE workspace_id=?',
                (self.w,)).fetchone()
            widgets = db.execute('SELECT response_id,widgets AS widgets_cipher,created_at FROM agent_widget_history '
                'WHERE workspace_id=? ORDER BY response_id', (self.w,)).fetchall()
        return {'dialogue': dict(dialogue) if dialogue else None, 'widgets': [dict(row) for row in widgets]}

    @staticmethod
    def snapshot_digest(snapshot):
        return sha_bytes(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode())

    def round_state(self, record):
        with closing(self.g.personal_data.connect()) as db:
            saved = db.execute('SELECT status FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',
                (self.w, record['response_id'])).fetchone()
            widgets = db.execute('SELECT count(*) FROM agent_widget_history WHERE workspace_id=? AND response_id=?',
                (self.w, record['response_id'])).fetchone()[0]
        with closing(self.g.bank.connect()) as db:
            operations = [{**dict(db.execute('SELECT id,state,result FROM operations WHERE workspace_id=? AND id=?',
                (self.w, operation)).fetchone())} for operation in record['operation_ids'] + record.get('unrelated_operation_ids', [])]
            audits = [json.loads(row['details']) for row in db.execute(
                "SELECT details FROM audit WHERE workspace_id=? AND event='agent_unpublished_answer_withdrawal' ORDER BY id",
                (self.w,)).fetchall()]
        with self.g._speech_lock:
            retained = record['receipt'] in self.g._speech_receipts
        return {'response_id': record['response_id'], 'guarded_save_status': saved[0] if saved else None,
            'widget_rows': widgets, 'receipt_issued': bool(record['receipt']), 'receipt_retained': retained,
            'operations': [{'id': row['id'], 'state': row['state'], 'has_result': bool(row['result'])} for row in operations],
            'withdrawal_audits': [row for row in audits if row.get('request_id') == record['response_id']]}

    def successful_prior(self, server):
        # 原生Agent准备会合法替代旧Agent草案；用真实工作台API的无关草案
        # 作为保留裁判，避免把合法替代误判成失败清理越权。
        protected_status, protected = self.http_json('POST', '/api/finance/operations/prepare', {
            'action': 'transfer', 'account_id': self.main, 'idempotency_key': 'shd-unrelated-prior',
            'beneficiary_id': next(row['id'] for row in self.initial['beneficiaries'] if row['alias'] == '妈妈'),
            'amount_minor': FIXTURE['transfer_minor']})
        self.assertEqual(protected_status, 200, protected)
        self.assertEqual(protected['operation']['state'], EXPECT['prior_financial_draft_state'])
        self.local_agent(server, FIXTURE['prior_reply'], draft=False)
        wire = self.request_wire(FIXTURE['prior_request'])
        result = self.parsed_body(wire)
        self.assertEqual(wire['status'], 200, result)
        self.assertEqual(result.get('reply'), FIXTURE['prior_reply'], result.get('error'))
        self.assertFalse(result.get('isFallback'))
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assertGreaterEqual(len(result['widgets']), EXPECT['prior_widgets_minimum'])
        self.assertEqual(len(self.g.personal_data.read_history(self.w)), EXPECT['prior_history_entries'])
        self.assertEqual(self.g.personal_data.read_history(self.w)[-1]['content'], FIXTURE['prior_reply'])
        record = self.round_for(FIXTURE['prior_reply'])
        self.assertTrue(record['receipt'])
        self.assertEqual(record['operation_ids'], [])
        record['unrelated_operation_ids'] = [protected['operation']['id']]
        self.assertEqual(self.round_state(record)['guarded_save_status'], 'saved')
        return record, self.private_snapshot(), wire

    @contextmanager
    def native_socket_layer(self, transmit):
        original = PublicationSocket.__init__
        def initialize(instance, *args, **kwargs):
            original(instance, *args, **kwargs)
            instance.sock = RawSocketLayer(instance.sock, transmit)
        with patch.object(PublicationSocket, '__init__', new=initialize):
            yield

    def delivery_failure_layer(self, reply, windows):
        marker = reply.encode('utf-8')
        def transmit(raw, body, *args, **kwargs):
            if marker in body:
                record = self.round_for(reply)
                state = self.round_state(record)
                windows.append({'window': 'real_raw_socket_sendall_after_guarded_history_commit',
                    'frame_bytes': len(body), 'frame_sha256': sha_bytes(body), 'round_state_before_failure': state,
                    'private_state_before_failure_sha256': self.snapshot_digest(self.private_snapshot())})
                self.assertEqual(state['guarded_save_status'], 'saved')
                self.assertGreater(state['widget_rows'], 0)
                raise BrokenPipeError('isolated_real_http_delivery_failure')
            return raw.sendall(body, *args, **kwargs)
        return transmit

    @contextmanager
    def observe_cleanup(self, *, pause_reply=None):
        original = self.g.discard_unpublished_answer
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def discard(workspace, request_id, result, **options):
            try:
                if pause_reply and result.get('reply') == pause_reply:
                    entered.set()
                    if not release.wait(WAIT):
                        raise AssertionError('delayed_cleanup_not_released')
                outcome = original(workspace, request_id, result, **options)
                self.cleanups.append({'response_id': request_id, 'history_saved_flag': options.get('history_saved'),
                    'outcome': copy.deepcopy(outcome)})
                return outcome
            finally:
                done.set()
        with patch.object(self.g, 'discard_unpublished_answer', side_effect=discard):
            yield entered, release, done

    def assert_own_failure_restricted(self, record, *, history_status):
        state = self.round_state(record)
        self.assertTrue(state['receipt_issued'])
        self.assertIs(state['receipt_retained'], EXPECT['failed_round_receipt_retained'])
        self.assertEqual(state['widget_rows'], EXPECT['failed_round_own_widget_rows'])
        self.assertEqual(len(state['withdrawal_audits']), 1)
        if history_status is not None:
            self.assertEqual(state['withdrawal_audits'][0]['history_status'], history_status)
        for row in state['operations']:
            self.assertEqual(row['state'], EXPECT['failed_round_financial_draft_state'])
            self.assertFalse(row['has_result'])
            view = self.g.operation_view(self.w, row['id'])
            status, error = self.http_json('POST', '/api/finance/operations/' + row['id'] + '/confirm', self.confirm_body(view))
            self.assertGreaterEqual(status, 400, error)
        status, replay = self.http_json('POST', '/api/speech/session', {'kind': 'tts', 'speech_receipt': record['receipt']})
        self.assertGreaterEqual(status, 400, replay)
        self.assertEqual(replay.get('error', {}).get('code'), 'SPEECH_RECEIPT_INVALID')
        return state

    def assert_prior_retained(self, record):
        state = self.round_state(record)
        self.assertEqual(state['guarded_save_status'], 'saved')
        self.assertGreaterEqual(state['widget_rows'], EXPECT['prior_widgets_minimum'])
        self.assertTrue(state['receipt_retained'], '失败清理不能全局废弃先前receipt')
        self.assertEqual([row['state'] for row in state['operations']], [EXPECT['prior_financial_draft_state']])
        return state

    def assert_no_success_or_second_result(self, wire, reply):
        parsed = self.parsed_body(wire)
        success = bool(parsed and parsed.get('reply') == reply and not parsed.get('isFallback'))
        self.assertEqual(int(success), EXPECT['failed_request_final_json_frames'])
        self.assertNotIn(reply.encode(), wire['body'])
        self.assertNotIn(b'speech_receipt', wire['body'])
        self.assertNotIn(b'HTTP/1.', wire['body'], '已发HTTP后不能追加另一份响应')

    def test_delivery_failure_restores_prior_actual_http_history_and_widgets(self):
        before, windows = self.raw_state(), []
        with self.actual_server() as server:
            prior, original_private, prior_wire = self.successful_prior(server)
            self.local_agent(server, FIXTURE['failed_reply'], draft=True)
            with self.observe_cleanup() as (_, _, done), self.native_socket_layer(
                    self.delivery_failure_layer(FIXTURE['failed_reply'], windows)):
                wire = self.request_wire(FIXTURE['failed_request'])
                self.assertTrue(done.wait(WAIT))
            failed = self.round_for(FIXTURE['failed_reply'])
            failed_state = self.assert_own_failure_restricted(failed, history_status=EXPECT['first_case_cleanup_history_status'])
            prior_state = self.assert_prior_retained(prior)
            restored = self.private_snapshot()
            self.evidence(prior_actual_http_status=prior_wire['status'], prior_reply_saved_by_http=True,
                memory_enabled=self.g.memories.state(self.w)['enabled'], real_send_windows=windows,
                http_status=wire['status'], received_bytes=len(wire['body']), incomplete_read=wire['partial'],
                exact_prior_ciphertext_and_timestamps_restored=restored == original_private,
                prior_snapshot_sha256=self.snapshot_digest(original_private), current_snapshot_sha256=self.snapshot_digest(restored),
                prior_state=prior_state, failed_state=failed_state, cleanup=self.cleanups,
                funds_unchanged=self.raw_state() == before)
            self.assertEqual(len(windows), 1)
            self.assertEqual(restored, original_private)
            self.assertNotIn(FIXTURE['failed_reply'], [row['content'] for row in self.g.personal_data.read_history(self.w)])
            self.assertEqual(failed_state['guarded_save_status'], EXPECT['first_case_guarded_save_status'])
            self.assert_no_success_or_second_result(wire, FIXTURE['failed_reply'])
            self.no_side_effects(before)

    def test_late_cleanup_preserves_later_actual_http_history_and_reports_partial(self):
        before, windows, results, errors = self.raw_state(), [], {}, []
        with self.actual_server() as server:
            prior, _, _ = self.successful_prior(server)
            self.local_agent(server, FIXTURE['failed_reply'], draft=True)
            with self.observe_cleanup(pause_reply=FIXTURE['failed_reply']) as (entered, release, done), \
                 self.native_socket_layer(self.delivery_failure_layer(FIXTURE['failed_reply'], windows)):
                def failed_request():
                    try:
                        results['failed'] = self.request_wire(FIXTURE['failed_request'])
                    except Exception as error:
                        errors.append({'type': type(error).__name__, 'message': str(error)})
                request = threading.Thread(target=failed_request, daemon=True)
                request.start()
                try:
                    self.assertTrue(entered.wait(WAIT), '必须到达真实失败清理入口，发布锁已释放')
                    # 模拟另一受信worker的独立Agent；银行/私库/守卫仍为同一真实对象。
                    self.local_agent(server, FIXTURE['later_reply'], draft=False)
                    later_wire = self.request_wire(FIXTURE['later_request'])
                    later_result = self.parsed_body(later_wire)
                    self.assertEqual(later_wire['status'], 200, later_result)
                    self.assertEqual(later_result.get('reply'), FIXTURE['later_reply'], later_result.get('error'))
                    self.assertFalse(later_result.get('isFallback'))
                    self.assertTrue(later_result.get('widgets'))
                    later = self.round_for(FIXTURE['later_reply'])
                    later_private = self.private_snapshot()
                finally:
                    release.set()
                    # 即使入口等待断言失败，也在撤销接缝补丁前收尾并留现场。
                    request.join(WAIT)
                    cleanup_done = done.wait(WAIT)
                    diagnostic = {'phase': 'SHD02_finally_before_patch_scope_exit',
                        'cleanup_entered': entered.is_set(), 'cleanup_done': cleanup_done,
                        'client_thread_alive': request.is_alive(), 'client_errors': copy.deepcopy(errors),
                        'real_send_windows': copy.deepcopy(windows), 'cleanup': copy.deepcopy(self.cleanups)}
                    wire = results.get('failed')
                    parsed = self.parsed_body(wire) if wire else None
                    diagnostic['client_result'] = None if wire is None else {
                        'status': wire['status'], 'body_bytes': len(wire['body']),
                        'body_sha256': sha_bytes(wire['body']), 'partial': wire['partial'],
                        'reply': parsed.get('reply') if parsed else None,
                        'isFallback': parsed.get('isFallback') if parsed else None,
                        'error': parsed.get('error') if parsed else None,
                        'runtime': parsed.get('agent_runtime') if parsed else None}
                    try:
                        agent = server.conversation_agent
                        diagnostic['server_agent_workspace_lock_locked'] = agent._locks[
                            int(hashlib.sha256(self.w.encode()).hexdigest(), 16) % len(agent._locks)].locked()
                        diagnostic['primary_calls'] = len(self.primary.sent)
                        diagnostic['primary_pending_steps'] = len(self.primary.steps)
                        diagnostic['review_calls'] = len(self.reviewer.packets)
                        diagnostic['round_states'] = [self.round_state(record) for record in self.rounds]
                        diagnostic['private_snapshot_sha256'] = self.snapshot_digest(self.private_snapshot())
                        diagnostic['funds_unchanged'] = self.raw_state() == before
                    except Exception as error:
                        diagnostic['state_observation_error'] = {'type': type(error).__name__, 'message': str(error)}
                    self.evidence(**diagnostic)
                self.assertTrue(done.wait(WAIT))
                request.join(WAIT)
                self.assertFalse(request.is_alive())
            self.assertEqual(errors, [])
            failed = self.round_for(FIXTURE['failed_reply'])
            failed_state = self.assert_own_failure_restricted(failed, history_status=EXPECT['second_case_cleanup_history_status'])
            prior_state, later_state = self.assert_prior_retained(prior), self.round_state(later)
            current = self.private_snapshot()
            # 失败轮匹配组件要撤回，但后来对话原件与后来/先前组件不得覆盖。
            later_filtered = {'dialogue': later_private['dialogue'], 'widgets': [row for row in later_private['widgets']
                if row['response_id'] != failed['response_id']]}
            self.evidence(prior_and_later_history_both_generated_by_actual_http=True,
                later_actual_http_status=later_wire['status'], independent_worker_fixture=True, real_send_windows=windows,
                exact_later_dialogue_preserved=current['dialogue'] == later_private['dialogue'],
                only_failed_widget_removed=current == later_filtered, later_state=later_state,
                failed_state=failed_state, prior_state=prior_state, cleanup=self.cleanups,
                client_failed_body_bytes=len(results['failed']['body']),
                later_history_includes_precleanup_failed_turn=any(row['content'] == FIXTURE['failed_reply']
                    for row in self.g.personal_data.read_history(self.w)), funds_unchanged=self.raw_state() == before)
            self.assertEqual(current, later_filtered)
            self.assertEqual(failed_state['guarded_save_status'], EXPECT['second_case_guarded_save_status'])
            self.assertEqual(later_state['guarded_save_status'], 'saved')
            self.assertTrue(later_state['receipt_retained'])
            self.assertGreater(later_state['widget_rows'], 0)
            self.assert_no_success_or_second_result(results['failed'], FIXTURE['failed_reply'])
            self.no_side_effects(before)

    def test_slow_actual_socket_uses_original_deadline_remaining_and_stops(self):
        before, clock, calls = self.raw_state(), MonotonicPosition(), []
        entered, returned = threading.Event(), threading.Event()
        target = {'thread': None, 'active': True, 'timeout': None, 'duration': None, 'native_outcome': None}
        deadline = FIXTURE['synthetic_monotonic_start'] + FIXTURE['original_turn_timeout_seconds']
        reply = FIXTURE['deadline_reply_prefix'] + FIXTURE['deadline_reply_padding'] * FIXTURE['deadline_reply_padding_repeats']

        def approved():
            target['thread'] = threading.get_ident()
            clock.now = deadline - FIXTURE['remaining_at_review_approval_seconds']

        def transmit(raw, body, *args, **kwargs):
            is_target = target['active'] and threading.get_ident() == target['thread']
            if is_target:
                calls.append({'started_clock': clock.now, 'bytes': len(body), 'sha256': sha_bytes(body),
                    'is_reply_frame': FIXTURE['deadline_reply_prefix'].encode() in body})
            if is_target and FIXTURE['deadline_reply_prefix'].encode() in body:
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, FIXTURE['tcp_buffer_bytes'])
                target['timeout'] = raw.gettimeout()
                started = time.monotonic()
                entered.set()
                try:
                    result = raw.sendall(body, *args, **kwargs)
                    target['native_outcome'] = 'returned'
                    return result
                except TimeoutError:
                    target['native_outcome'] = 'native_socket_timeout'
                    raise
                finally:
                    target['duration'] = time.monotonic() - started
                    returned.set()
            return raw.sendall(body, *args, **kwargs)

        with self.actual_server() as server:
            prior, original_private, _ = self.successful_prior(server)
            agent = self.local_agent(server, reply, reviewer_hook=approved)
            with self.observe_cleanup() as (_, _, cleanup_done), patch.object(_conversation, 'time', new=clock), \
                    self.native_socket_layer(transmit):
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, FIXTURE['tcp_buffer_bytes'])
                    client.settimeout(WAIT)
                    client.connect(('127.0.0.1', self.server.server_port))
                    body = json.dumps({'text': FIXTURE['deadline_request'], 'mode': 'classic'}, ensure_ascii=False).encode()
                    headers = ('POST /api/agent HTTP/1.1\r\nHost: 127.0.0.1:' + str(self.server.server_port) + '\r\nContent-Type: application/json\r\n'
                        'Accept: application/json\r\nConnection: close\r\nOrigin: ' + self.base + '\r\nCookie: ' + COOKIE + '=' + self.token +
                        '\r\nX-QZ-CSRF: ' + self.g.csrf(self.token) + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode()
                    client.sendall(headers + body)
                    received_headers = bytearray()
                    while not received_headers.endswith(b'\r\n\r\n'):
                        piece = client.recv(1)
                        self.assertTrue(piece, '真实HTTP未到响应头，不能把前期回退当慢发送证据')
                        received_headers.extend(piece)
                        self.assertLess(len(received_headers), 16384)
                    self.assertTrue(entered.wait(WAIT), 'BLOCKED:未到真正原生sendall')
                    self.assertFalse(returned.wait(FIXTURE['backpressure_observation_seconds']),
                        '真实TCP未发生反压，不能用立即返回伪通过')
                    clock.now = deadline + FIXTURE['deadline_excess_seconds']
                    self.assertTrue(returned.wait(WAIT))
                    self.assertTrue(cleanup_done.wait(WAIT))
                    target['active'] = False
                    received_body = bytearray()
                    while True:
                        try:
                            piece = client.recv(65536)
                        except ConnectionResetError:
                            # 真正socket.shutdown后的RST也是有界关闭；已有前缀保留，
                            # 后面的完整性/截止断言仍须成立，不把RST本身算通过。
                            break
                        if not piece:
                            break
                        received_body.extend(piece)
                finally:
                    target['active'] = False
                    client.close()
            failed = self.round_for(reply)
            failed_state = self.assert_own_failure_restricted(failed, history_status=EXPECT['first_case_cleanup_history_status'])
            self.assert_prior_retained(prior)
            declared = next(int(line.split(b':', 1)[1].strip()) for line in bytes(received_headers).split(b'\r\n')
                if line.lower().startswith(b'content-length:'))
            wire = {'status': 200, 'body': bytes(received_body), 'partial': len(received_body) != declared}
            parsed = self.parsed_body(wire)
            late_calls = [row for row in calls if row['started_clock'] >= deadline]
            self.evidence(original_timeout_seconds=agent.timeout_seconds, original_monotonic_start=clock.reads[0],
                original_deadline=deadline, clock_after=clock.now, real_native_socket=target,
                raw_serialized_frame_calls=calls, calls_started_after_deadline=len(late_calls),
                response_header_bytes=len(received_headers), response_body_received_bytes=len(received_body),
                declared_body_bytes=declared, complete_success_json_frames=int(bool(parsed and not parsed.get('isFallback'))),
                failed_state=failed_state, cleanup=self.cleanups,
                exact_prior_restored=self.private_snapshot() == original_private, funds_unchanged=self.raw_state() == before,
                interpretation='截止前有效TCP前缀允许，原生sendall真实超时；不把后来读取缓冲前缀解释成新发布')
            self.assertEqual(clock.reads[0], FIXTURE['synthetic_monotonic_start'])
            self.assertEqual(target['native_outcome'], 'native_socket_timeout')
            self.assertGreater(target['timeout'], 0)
            self.assertLessEqual(target['timeout'], FIXTURE['remaining_at_review_approval_seconds'] + 1e-9)
            self.assertLessEqual(target['duration'], FIXTURE['remaining_at_review_approval_seconds'] + FIXTURE['maximum_transport_wait_overhead_seconds'])
            self.assertEqual(len(late_calls), EXPECT['native_socket_send_calls_started_after_deadline'])
            self.assertLess(len(received_body), declared)
            self.assertFalse(parsed and not parsed.get('isFallback'), '客户端不能已有完整成功答复')
            self.assertNotIn(b'HTTP/1.', received_body)
            self.assertEqual(self.private_snapshot(), original_private)
            self.no_side_effects(before)

    def test_private_commit_then_permissions_error_restores_prior_actual_http_turn(self):
        self.require(self.g.personal_data, '_permissions')
        before, windows = self.raw_state(), []
        original = self.g.personal_data._permissions
        fired = False

        def permissions():
            nonlocal fired
            found = [record for record in self.rounds if record['reply'] == FIXTURE['failed_reply']]
            if found and not fired:
                record = found[-1]
                with closing(self.g.personal_data.connect()) as db:
                    committed = db.execute('SELECT status FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',
                        (self.w, record['response_id'])).fetchone()
                    widgets = db.execute('SELECT count(*) FROM agent_widget_history WHERE workspace_id=? AND response_id=?',
                        (self.w, record['response_id'])).fetchone()[0]
                if committed and committed[0] == 'saved':
                    fired = True
                    windows.append({'window': 'real_private_commit_visible_to_second_connection_before_permissions_exception',
                        'response_id': record['response_id'], 'committed_guarded_save_status': committed[0],
                        'committed_widget_rows': widgets, 'private_snapshot_sha256': self.snapshot_digest(self.private_snapshot())})
                    raise OSError('isolated_permissions_failure_after_actual_private_commit')
            return original()

        with self.actual_server() as server:
            prior, original_private, _ = self.successful_prior(server)
            self.local_agent(server, FIXTURE['failed_reply'], draft=True)
            with self.observe_cleanup() as (_, _, done), patch.object(self.g.personal_data, '_permissions', side_effect=permissions):
                wire = self.request_wire(FIXTURE['failed_request'])
                self.assertTrue(done.wait(WAIT))
            failed = self.round_for(FIXTURE['failed_reply'])
            failed_state = self.assert_own_failure_restricted(failed, history_status=EXPECT['first_case_cleanup_history_status'])
            prior_state = self.assert_prior_retained(prior)
            restored = self.private_snapshot()
            self.evidence(prior_generated_by_actual_success_http=True, private_commit_fault_windows=windows,
                permissions_error_once=fired, http_status=wire['status'], received_bytes=len(wire['body']),
                failed_state=failed_state, prior_state=prior_state, cleanup=self.cleanups,
                exact_prior_ciphertext_and_timestamps_restored=restored == original_private,
                funds_unchanged=self.raw_state() == before)
            self.assertTrue(fired, '未到真实私库commit后异常窗口，不得伪通过')
            self.assertEqual(len(windows), 1)
            self.assertGreater(windows[0]['committed_widget_rows'], 0)
            self.assertEqual(restored, original_private)
            self.assertEqual(failed_state['guarded_save_status'], EXPECT['first_case_guarded_save_status'])
            self.assert_no_success_or_second_result(wire, FIXTURE['failed_reply'])
            self.no_side_effects(before)


            # 同一成功前轮之后，再独立注入真实私库commit之前失败。
            # 不是改黄金适应结果：oracle已冻结“未commit安全noop”，源码门仍真实。
            uncommitted_reply = FIXTURE['uncommitted_reply']
            self.local_agent(server, uncommitted_reply, draft=True)
            connect = self.g.personal_data.connect
            uncommitted_windows, no_commit_phases = [], []
            no_commit_wire, no_commit_error = None, None
            owner = self

            class CommitFaultConnection:
                def __init__(self, raw):
                    self.raw, self.target = raw, False

                def __getattr__(self, name):
                    return getattr(self.raw, name)

                def commit(self):
                    found = [record for record in owner.rounds if record['reply'] == uncommitted_reply]
                    if found and not uncommitted_windows and self.raw.in_transaction:
                        record = found[-1]
                        no_commit_phases.append({'phase': 'commit_candidate_enter', 'clock': time.monotonic(),
                            'response_id': record['response_id']})
                        inside = self.raw.execute('SELECT status FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',
                            (owner.w, record['response_id'])).fetchone()
                        if inside and inside[0] == 'saved':
                            self.target = True
                            no_commit_phases.append({'phase': 'target_before_independent_reader_connect', 'clock': time.monotonic()})
                            with closing(connect()) as reader:
                                no_commit_phases.append({'phase': 'target_independent_reader_connected', 'clock': time.monotonic()})
                                outside = reader.execute('SELECT status FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',
                                    (owner.w, record['response_id'])).fetchone()
                            no_commit_phases.append({'phase': 'target_independent_reader_completed', 'clock': time.monotonic()})
                            uncommitted_windows.append({'window': 'real_private_commit_before_native_commit_failure',
                                'response_id': record['response_id'], 'inside_transaction_status': inside[0],
                                'separate_connection_has_committed_row': outside is not None})
                            no_commit_phases.append({'phase': 'target_before_native_commit_fault', 'clock': time.monotonic()})
                            raise sqlite3.OperationalError('isolated_private_commit_failure_before_commit')
                    return self.raw.commit()

                def rollback(self):
                    if self.target:
                        no_commit_phases.append({'phase': 'target_native_rollback_enter', 'clock': time.monotonic()})
                    result = self.raw.rollback()
                    if self.target:
                        no_commit_phases.append({'phase': 'target_native_rollback_completed', 'clock': time.monotonic(),
                            'in_transaction': self.raw.in_transaction})
                    return result

            with self.observe_cleanup() as (_, _, no_commit_done), patch.object(self.g.personal_data, 'connect',
                    side_effect=lambda: CommitFaultConnection(connect())):
                try:
                    no_commit_wire = self.request_wire(FIXTURE['failed_request'])
                    self.assertTrue(no_commit_done.wait(WAIT))
                except Exception as error:
                    no_commit_error = {'type': type(error).__name__, 'message': str(error)}
                    raise
                finally:
                    handlers_at_request_end = server.fixture_handler_snapshot()
                    if no_commit_error is not None:
                        # 异常后只诊断/收口；不改变原7秒客户端和5秒cleanup门。
                        server.fixture_wait_handlers(WAIT)
                    handlers_before_exit = server.fixture_handler_snapshot()
                    state_observations, snapshot_observed, state_error = [], None, None
                    if not any(record['alive'] for record in handlers_before_exit):
                        try:
                            state_observations = [self.round_state(record) for record in self.rounds]
                            snapshot_observed = self.snapshot_digest(self.private_snapshot())
                        except Exception as error:
                            state_error = {'type': type(error).__name__, 'message': str(error)}
                    self.evidence(phase='SHD04_no_commit_finally_before_patch_scope_exit',
                        client_exception=no_commit_error, http_status=no_commit_wire.get('status') if no_commit_wire else None,
                        received_bytes=len(no_commit_wire['body']) if no_commit_wire else None,
                        private_commit_fault_windows=copy.deepcopy(uncommitted_windows),
                        commit_phase_events=copy.deepcopy(no_commit_phases), cleanup_done=no_commit_done.is_set(),
                        cleanup=copy.deepcopy(self.cleanups), primary_calls=len(self.primary.sent),
                        primary_pending_steps=len(self.primary.steps), review_calls=len(self.reviewer.packets),
                        rounds=[{'response_id': record['response_id'], 'handler_thread_ident': record['handler_thread_ident'],
                                 'reply_kind': 'uncommitted' if record['reply'] == uncommitted_reply
                                 else 'prior_or_postcommit', 'operation_ids': record['operation_ids']} for record in self.rounds],
                        round_states=state_observations, private_snapshot_sha256=snapshot_observed,
                        state_observation_error=state_error, handlers_at_original_request_end=handlers_at_request_end,
                        handlers_before_patch_exit=handlers_before_exit)
            uncommitted = self.round_for(uncommitted_reply)
            no_commit_state = self.assert_own_failure_restricted(uncommitted, history_status=None)
            self.evidence(subscenario='native_private_commit_failure_safe_noop',
                private_commit_fault_windows=uncommitted_windows, failed_state=no_commit_state,
                actual_http_status=no_commit_wire['status'], received_bytes=len(no_commit_wire['body']),
                prior_actual_http_original_unchanged=self.private_snapshot() == original_private,
                cleanup=self.cleanups, funds_unchanged=self.raw_state() == before)
            self.assertEqual(len(uncommitted_windows), 1)
            self.assertFalse(uncommitted_windows[0]['separate_connection_has_committed_row'])
            self.assertIsNone(no_commit_state['guarded_save_status'])
            self.assertNotIn(no_commit_state['withdrawal_audits'][0]['history_status'], ('discarded', 'fully_reverted'))
            self.assertEqual(self.private_snapshot(), original_private)
            self.assert_prior_retained(prior)
            self.assert_no_success_or_second_result(no_commit_wire, uncommitted_reply)
            self.no_side_effects(before)


if set(unittest.defaultTestLoader.getTestCaseNames(ServiceHistoryDeadlineContracts)) != set(CASES):
    raise RuntimeError('frozen_service_history_deadline_case_collection_mismatch')


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ServiceHistoryDeadlineContracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    complete = (result.wasSuccessful() and result.testsRun == EXPECT['test_method_count']
        and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses)
    counts = {'tests_run': result.testsRun, 'expected_tests': EXPECT['test_method_count'],
        'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'expected_failures': len(result.expectedFailures), 'unexpected_successes': len(result.unexpectedSuccesses),
        'complete': complete, 'planned_exit_code': 0 if complete else 1, 'oracle_sha256': FROZEN_ORACLE_SHA}
    if hasattr(ServiceHistoryDeadlineContracts, 'output'):
        (ServiceHistoryDeadlineContracts.output / 'actual-counts.json').write_text(json.dumps(counts, indent=2) + '\n')
    sys.exit(0 if complete else 1)
