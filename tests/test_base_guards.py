"""临时 HTTP/语音运行时中的账户与指标竞态回归；不访问模型或现役库。"""
from contextlib import closing
from http.cookiejar import Cookie
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from scripts.verify_base_runtime import Browser, temporary_runtime
from voice_gateway import VoiceGateway


class BaseGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = temporary_runtime()
        cls.server, cls.base, _ = cls.runtime.__enter__()
        cls.gateway = cls.server.finance

    @classmethod
    def tearDownClass(cls):
        cls.runtime.__exit__(None, None, None)

    def setUp(self):
        self.browser = Browser(self.base)
        if self._testMethodName != 'test_second_browser_does_not_invalidate_first_browser_current_verification_code':
            # 竞态用独立模拟工作区，避免另一项的账本/个人资料状态互相影响。
            credentials = self.gateway.bank.create_workspace()
            self.browser.jar.set_cookie(Cookie(version=0, name='qz_bank_user', value=credentials['session_token'],
                port=None, port_specified=False, domain='127.0.0.1', domain_specified=False,
                domain_initial_dot=False, path='/', path_specified=True, secure=False, expires=None,
                discard=True, comment=None, comment_url=None, rest={}, rfc2109=False))
        self.session, _ = self.browser.start()
        self.workspace = self.session['workspace_id']
        self.token = next(cookie.value for cookie in self.browser.jar)
        overview = self.gateway.overview(self.workspace)
        self.account = next(row for row in overview['accounts'] if row['id'].endswith('-main'))
        self.beneficiary = next(row for row in overview['beneficiaries'] if row['verified'] and row['status'] == 'active')

    def prepare(self):
        payload = {'action': 'transfer', 'account_id': self.account['id'],
                   'beneficiary_id': self.beneficiary['id'], 'amount_minor': 10000,
                   'idempotency_key': 'base-guard-' + uuid.uuid4().hex}
        status, result, _ = self.browser.request('/api/finance/operations/prepare', 'POST', payload)
        return status, result, payload

    def confirm(self, operation):
        return self.browser.request('/api/finance/operations/' + operation['id'] + '/confirm', 'POST',
                                    {'confirmed': True, 'confirmation_digest': operation['confirmation_digest'],
                                     'verification_code': self.gateway.demo_code(self.token)})

    def recorded_change(self, operation_id):
        with closing(self.gateway.bank.connect()) as db:
            balance = db.execute('SELECT balance_minor FROM accounts WHERE id=?', (self.account['id'],)).fetchone()[0]
            rows = db.execute('SELECT count(*) FROM transactions WHERE operation_id=?', (operation_id,)).fetchone()[0]
        return balance - self.account['balance_minor'], rows

    def test_account_switch_after_request_identity_cannot_execute_old_confirm(self):
        status, view, _ = self.prepare()
        self.assertEqual(status, 200, view)
        operation = view['operation']
        original = self.gateway.confirm

        def switched_confirm(workspace, operation_id, payload):
            # 确认请求已通过初始 cookie/CSRF；模拟此时另一请求完成账户切换。
            self.gateway.demo_sessions.select(self.token, 'grain_farmer', workspace)
            return original(workspace, operation_id, payload)

        with patch.object(self.gateway, 'confirm', side_effect=switched_confirm):
            status, result, _ = self.confirm(operation)
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'SESSION_CHANGED')
        self.assertEqual(self.recorded_change(operation['id']), (0, 0))
        self.assertEqual(self.gateway.bank.operation(self.workspace, operation['id'])['state'], 'pending_confirmation')

    def test_personal_change_during_metric_binding_cannot_leave_executable_unbound_draft(self):
        original = self.gateway.financial_metrics.bind_operation

        def changed_personal_context(workspace, operation_id, snapshot):
            # 正常个人资料写入增加 private revision，银行余额/version保持不变。
            self.gateway.memories.create(workspace, {'category': 'goal',
                'value': '测试资金目标 ' + uuid.uuid4().hex,
                'expected_revision': self.gateway.personal_data.revision(workspace)})
            return original(workspace, operation_id, snapshot)

        with patch.object(self.gateway.financial_metrics, 'bind_operation', side_effect=changed_personal_context):
            status, result, payload = self.prepare()
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'METRIC_VERSION_CONFLICT')
        with closing(self.gateway.bank.connect()) as db:
            row = db.execute('SELECT id FROM operations WHERE workspace_id=? AND idempotency_key=?',
                             (self.workspace, payload['idempotency_key'])).fetchone()
        self.assertIsNotNone(row)
        operation = self.gateway.bank.operation(self.workspace, row['id'])
        status, result, _ = self.confirm(operation)
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'METRIC_BINDING_REQUIRED')
        self.assertEqual(self.recorded_change(operation['id']), (0, 0))

    def test_second_browser_does_not_invalidate_first_browser_current_verification_code(self):
        status, view, _ = self.prepare()
        self.assertEqual(status, 200, view)
        second = Browser(self.base)
        second_session, _ = second.start()
        self.assertEqual(second_session['workspace_id'], self.workspace)
        status, result, _ = self.confirm(view['operation'])
        self.assertEqual(status, 200, result)
        self.assertEqual(result['operation']['state'], 'succeeded')
        self.assertEqual(self.recorded_change(view['operation']['id']), (-10000, 1))
        status, again, _ = self.confirm(view['operation'])
        self.assertEqual(status, 200, again)
        self.assertEqual(self.recorded_change(view['operation']['id']), (-10000, 1))

    def test_source_calendar_redemption_keeps_cost_recovery_separate_from_return(self):
        workspace = self.gateway.demo_accounts.workspace_for('tycoon')
        before = self.gateway.demo_accounts.current(workspace)['bank_state']['cash_minor']
        rows = self.gateway.bank.transactions(workspace, '2017-09')
        redemption = next(row for row in rows if row.get('event_id') == 'tycoon-e0001434')
        self.assertEqual(redemption['amount_minor'], 409784417)
        self.assertEqual(redemption['investment_principal_minor'], 381944444)
        self.assertEqual(redemption['realized_return_minor'], 27839973)
        self.assertEqual(redemption['principal_basis'], 'source_journal_cost_recovery')
        self.assertEqual(redemption['amount_minor'], redemption['investment_principal_minor'] + redemption['realized_return_minor'])
        calendar = self.gateway.calendar.query(workspace, {'from': '2017-09-03', 'to': '2017-09-03',
            'source': 'simulation', 'include_life_events': False})
        summary = next(row for row in calendar['summary_by_currency'] if row['currency'] == 'CNY')
        self.assertEqual(summary['investment_redemption_cash_in_minor'], 409784417)
        self.assertEqual(summary['investment_principal_in_minor'], 381944444)
        self.assertEqual(summary['realized_return_minor'], 27839973)
        self.assertEqual(self.gateway.demo_accounts.current(workspace)['bank_state']['cash_minor'], before)

    def test_switch_while_voice_receive_blocks_never_publishes_old_account_audio(self):
        gateway = self.gateway
        receipt = gateway.approve_speech(self.workspace, {'reply': '旧账户的合成资金说明',
            'compliance_pass': True, 'answer_contract': {'kind': 'offline_guard_fixture'},
            **gateway.session_data(self.workspace, self.token)})
        self.assertIsNotNone(receipt)
        messages, finished = [], threading.Event()
        token, workspace = self.token, self.workspace

        class FakeBrowser:
            def __init__(self, handler):
                pass

            def receive(self):
                yield {'type': 'start', 'voice': 'female_sweet'}
                finished.wait(3)
                yield {'type': 'cancel'}

            def send(self, message):
                messages.append(message)
                if isinstance(message, bytes) or (isinstance(message, dict) and message.get('type') == 'error'):
                    finished.set()

            def close(self):
                pass

        class FakeTask:
            id = 'offline-voice-guard-fixture'

            def __init__(self, *args):
                pass

            def receive(self):
                # 云任务返回前切换；随后这块数据必须在 publish 守卫中丢弃。
                gateway.demo_sessions.select(token, 'grain_farmer', workspace)
                return b'old-account-audio'

            def close(self):
                pass

        voice = VoiceGateway(key='offline-guard-fixture', allow_cloud_asr=False, task_factory=FakeTask)
        ticket = 'offline-guard-' + uuid.uuid4().hex
        voice.tickets[ticket] = (token, 'tts', time.monotonic() + 30, workspace, receipt)
        handler = SimpleNamespace(server=self.server, client_address=('127.0.0.1', 9876),
            path='/api/speech/ws?ticket=' + ticket,
            headers={'Host': self.base.split('://')[1], 'Origin': self.base, 'Cookie': self.browser.cookie()})
        with patch('voice_gateway.BrowserSocket', FakeBrowser):
            voice.socket(handler)
        self.assertTrue(finished.is_set(), messages)
        self.assertFalse(any(isinstance(item, bytes) for item in messages), messages)
        self.assertTrue(any(isinstance(item, dict) and item.get('code') == 'SESSION_CHANGED' for item in messages), messages)


if __name__ == '__main__':
    unittest.main()
