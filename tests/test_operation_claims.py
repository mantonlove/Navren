"""S01 有限状态断言合同；独立临时银行行，不调用模型/确认/执行。"""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from agent_runtime.bank_tools import BankTools
from agent_runtime.operation_claims import validate_operation_wording
from finance_gateway import FinanceGateway
from simulated_bank import BankError


class OperationClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-operation-claims-')
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(path / 'public.sqlite3'),
            'NAVREN_FINANCIAL_DB': '', 'NAVREN_PUBLIC_SYNC_ENABLED': 'false',
            'NAVREN_MODEL_CALL_LOG': str(path / 'unused-model.jsonl')})
        environment.start()
        self.addCleanup(environment.stop)
        self.g = FinanceGateway(path / 'bank.sqlite3')
        self.w, _ = self.g.create_session()
        self.tools = BankTools(self.g, self.w, 'S01-test-current-turn')
        accounts = self.tools.invoke('account_summary', {})['data']['accounts']
        beneficiaries = self.tools.invoke('business_records', {'kind': 'payments'})['data']['beneficiaries']
        self.prepared = self.tools.invoke('prepare_operation', {
            'action': 'transfer', 'account_id': next(a['id'] for a in accounts if a['id'].endswith('-main')),
            'beneficiary_id': next(b['id'] for b in beneficiaries if b['alias'] == '妈妈'),
            'amount_minor': 10000})
        self.op = self.prepared['data']['operation']['id']

    def final(self, text, *, tools=None, draft=True):
        tools = tools or self.tools
        evidence_id = next(reversed(tools.evidence))
        return {'text': text, 'claims': [{'text': text, 'evidence_ids': [evidence_id]}],
                'draft_ids': [self.op] if draft else [], 'followups': []}

    def expect_denied(self, final, tools=None, bank_db=None):
        with self.assertRaises(BankError) as caught:
            validate_operation_wording(final, tools or self.tools, bank_db=bank_db)
        self.assertTrue(caught.exception.code.startswith('OPERATION_'))
        self.assertIn('不重新创建', caught.exception.operation_wording_instruction)
        return caught.exception

    def state_fixture(self, state, result=None):
        """Controlled synthetic state row; never executes a financial action."""
        with self.g.bank.transaction() as db:
            db.execute('UPDATE operations SET state=?,result=? WHERE id=?',
                       (state, json.dumps(result) if result is not None else None, self.op))
        tools = BankTools(self.g, self.w, 'S01-current-status-turn')
        tools.invoke('operation_status', {'operation_id': self.op})
        return tools

    def test_original_prepared_positive_and_success_negative_no_financial_effect(self):
        with closing(self.g.bank.connect()) as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
        result = validate_operation_wording(self.final(
            '已准备具体模拟转账草案，待你核对确认，尚未执行。'), self.tools)
        self.assertEqual(result['bindings'][0]['state'], 'pending_confirmation')
        self.expect_denied(self.final('已转账成功，资金已到账。'))
        self.expect_denied(self.final('已经转出资金。'))
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')])
            self.assertEqual(db.execute('SELECT state FROM operations WHERE id=?', (self.op,)).fetchone()[0],
                             'pending_confirmation')

    def test_success_requires_original_status_receipt_and_matching_action(self):
        tools = self.state_fixture('succeeded', {'status': 'succeeded', 'payment_id': 'synthetic-receipt'})
        result = validate_operation_wording(self.final('模拟转账已完成。', tools=tools, draft=False), tools)
        self.assertEqual(result['bindings'][0]['state'], 'succeeded')
        self.expect_denied(self.final('已买入成功。', tools=tools, draft=False), tools)
        self.expect_denied(self.final('资金已到账。', tools=tools, draft=False), tools)
        tools = self.state_fixture('succeeded', None)
        self.expect_denied(self.final('已转账成功。', tools=tools, draft=False), tools)

    def test_failed_pending_unknown_cannot_be_called_success(self):
        for state, wording, receipt in (
                ('failed', '转账已失败。', None),
                ('pending_settlement', '操作待结算。', {'status': 'pending_settlement'}),
                ('processing', '原操作结果未知。', None)):
            with self.subTest(state=state):
                tools = self.state_fixture(state, receipt)
                result = validate_operation_wording(self.final(wording, tools=tools, draft=False), tools)
                self.assertEqual(result['bindings'][0]['state'], state)
                self.expect_denied(self.final('已转账成功。', tools=tools, draft=False), tools)

    def test_settled_arrival_needs_same_completed_receipt(self):
        tools = self.state_fixture('settled', {'status': 'settled', 'receipt_id': 'synthetic-settlement'})
        result = validate_operation_wording(self.final('资金已到账。', tools=tools, draft=False), tools)
        self.assertEqual(result['bindings'][0]['state'], 'settled')
        self.expect_denied(self.final('尚未执行。', tools=tools, draft=False), tools)

    def test_withdrawn_draft_is_not_subscription_cancellation_success(self):
        # Withdrawal is a subsequent turn. Reusing old prepare metric batches
        # correctly fails their own freshness contract after cancellation.
        self.tools = BankTools(self.g, self.w, 'S01-current-withdraw-turn')
        self.tools.invoke('withdraw_draft', {'operation_id': self.op})
        result = validate_operation_wording(self.final('草案已撤回，尚未执行原操作。', draft=False), self.tools)
        self.assertEqual(result['bindings'][0]['state'], 'cancelled')
        self.expect_denied(self.final('订阅已取消成功。', draft=False))

    def test_foreign_subject_and_old_operation_do_not_support_current_draft(self):
        foreign, _ = self.g.create_session()
        forged = deepcopy(self.prepared)
        foreign_tools = BankTools(self.g, foreign, 'S01-foreign')
        foreign_tools.evidence['foreign-copy'] = {'tool': 'operation_status', 'source': 'simulation',
            'data': forged['data'], 'context_hash': foreign_tools.start_context}
        final = {'text': '已转账成功。', 'claims': [{'text': '已转账成功。', 'evidence_ids': ['foreign-copy']}],
                 'draft_ids': [], 'followups': []}
        self.expect_denied(final, foreign_tools)
        final = self.final('原操作' + 'f' * 32 + '尚未执行。')
        self.expect_denied(final)

    def test_explanation_hypothesis_question_and_general_refusal_are_not_killed(self):
        empty = BankTools(self.g, self.w, 'S01-read-only')
        for text in ('普通财务解释不涉及交易终态。', '如果本人确认，就会执行转账。',
                     '假设转账成功，仅用于教学示例。', '是否转账成功？',
                     '“转账成功”只是状态含义，不代表当前操作。',
                     '没有授权，本轮未执行任何金融操作。', '本轮只给方案，没有转账。',
                     '不能声称交易成功。'):
            with self.subTest(text=text), patch.object(self.g.bank, 'connect',
                    side_effect=AssertionError('non-operation prose must not read private DB')):
                final = {'text': text, 'claims': [], 'draft_ids': [], 'followups': []}
                self.assertEqual(validate_operation_wording(final, empty)['bindings'], [])

    def test_final_same_bank_read_rejects_late_state_change_without_new_connection(self):
        final = self.final('尚未执行，等待本人确认。')
        validate_operation_wording(final, self.tools)
        with self.g.bank.transaction() as db:
            db.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?",
                       (json.dumps({'status': 'succeeded', 'payment_id': 'synthetic-late'}), self.op))
            with patch.object(self.g.bank, 'connect', side_effect=AssertionError('must reuse held DB')):
                error = self.expect_denied(final, bank_db=db)
                self.assertEqual(error.code, 'OPERATION_STATUS_CHANGED')

    def test_unparseable_state_and_unbound_success_get_accurate_recovery(self):
        error = self.expect_denied(self.final('转账办妥了，资金到账了。'))
        self.assertIn('尚未执行', error.operation_wording_instruction)
        empty = BankTools(self.g, self.w, 'S01-no-receipt')
        self.expect_denied({'text': '已转账成功。', 'claims': [], 'draft_ids': []}, empty)
        final = self.final('尚未执行。')
        final['claims'] = [{'text': '已转账成功。', 'evidence_ids': [self.prepared['evidence_id']]}]
        self.expect_denied(final)

    def test_english_status_and_explicit_business_verbs_bind_to_real_original_action(self):
        self.expect_denied(self.final('Transfer completed.'))
        validate_operation_wording(self.final('Transfer draft prepared, not executed.'), self.tools)
        for state, wording, receipt in (
                ('succeeded', 'Payment completed.', {'status': 'succeeded'}),
                ('failed', 'Transfer failed.', None),
                ('pending_settlement', 'Payment pending settlement.', {'status': 'pending_settlement'}),
                ('processing', 'Transaction status unknown.', None)):
            tools = self.state_fixture(state, receipt)
            validate_operation_wording(self.final(wording, tools=tools, draft=False), tools)
        tools = self.state_fixture('succeeded', {'status': 'succeeded'})
        for text in ('虚拟卡已核发成功。', '已经解挂。', '已修改密码。', '资金已预留。',
                     '资金已释放。', '服务预约已完成。', 'Card issuance completed.', 'Password change completed.'):
            self.expect_denied(self.final(text, tools=tools, draft=False), tools)

    def test_quote_definition_cannot_erase_another_personal_terminal_assertion(self):
        for text in ('“交易成功”只是状态含义；“已转账成功”是本人刚刚操作。',
                     '“已转账成功”只是示例。',
                     '普通行情示例。“本人已转账成功”是实际结果。'):
            self.expect_denied(self.final(text))

    def test_verified_code_receipt_scope_never_proves_bank_transfer(self):
        from agent_runtime.generated_code import GeneratedCodeSession
        from agent_runtime.operation_claims import _digest
        tools = BankTools(self.g, self.w, 'S01-code-scope')
        session = GeneratedCodeSession.__new__(GeneratedCodeSession)
        session.tools, session.g, session.w = tools, self.g, self.w
        receipt = {'status': 'COMPLETED_CANDIDATE', 'worker_reaped': True,
                   'worker_group_absent': True, 'temporary_directory_removed': True}
        result = {'schema': 'navren.generated-code-result/1', 'receipt_sha256': _digest(receipt)}
        session._jobs = [{'receipt': receipt, 'result': result}]
        tools.generated_code_session = session
        tools.evidence['code'] = {'tool': 'run_generated_code', 'source': 'restricted_generated_code',
            'data': result, 'context_hash': tools.start_context}
        final = {'text': '代码已在沙箱执行完成。', 'claims': [], 'draft_ids': []}
        with patch.object(session, 'verify_fresh', return_value=True) as verify:
            self.assertEqual(validate_operation_wording(final, tools)['bindings'], [])
            verify.assert_called_once()
            self.expect_denied({'text': '代码已在沙箱执行，已转账成功。', 'claims': [], 'draft_ids': []}, tools)
        session._jobs = []
        self.expect_denied(final, tools)
        session._jobs = [{'receipt': {**receipt, 'status': 'REJECTED'}, 'result': result}]
        self.expect_denied(final, tools)


if __name__ == '__main__':
    unittest.main()
