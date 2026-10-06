"""受限原生协商接线；合成 SQLite/实际服务，脚本模型仅验证协议，零云。"""
import copy
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from agent_runtime.bank_tools import BankTools, native_tools
from agent_runtime.conversation import ConversationAgent
from finance_gateway import FinanceGateway
from simulated_bank import BankError
from test_native_conversation import ScriptedPrimary, Reviewer, call, last_tool


class AgentServiceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.g = FinanceGateway(Path(self.temp.name) / 'bank.sqlite')
        self.w, self.token = self.g.create_session()
        self.service = self.g.service_negotiations
        # 每项只改独占临时规则文件，绝不触碰正式规则。
        self.rules = copy.deepcopy(self.service.rules())
        self.rule_path = Path(self.temp.name) / 'rules.json'
        self.write_rules(self.rules)
        self.service.rules_path = self.rule_path
        self.request = '我愿意迁入工资账户，但不接受购买理财作为优惠条件，希望协商服务费减免。'

    def write_rules(self, rules):
        self.rule_path.write_text(json.dumps(rules, ensure_ascii=False), encoding='utf-8')

    def tools(self, text=None, request_id='isolated-service'):
        tool = BankTools(self.g, self.w, request_id)
        tool.user_text = self.request if text is None else text
        return tool

    def financial_state(self):
        overview = self.g.overview(self.w)
        with closing(self.g.bank.connect()) as db:
            operations = db.execute('SELECT COUNT(*) FROM operations WHERE workspace_id=?', (self.w,)).fetchone()[0]
        return {key: overview[key] for key in ('accounts', 'total_cash_minor', 'holdings', 'orders',
                'fund_holds', 'service_reservations')} | {
            'operations': operations,
            'applications': self.service.applications(self.w)['applications'],
        }

    def assert_stale(self, fn):
        with self.assertRaises(BankError) as caught:
            fn()
        self.assertEqual(caught.exception.code, 'STALE_SERVICE_EVIDENCE')
        self.assertEqual(caught.exception.status, 409)

    def seed_application(self, workspace=None, offer='SYNTH-E-STATEMENT-FEE-90D'):
        workspace = workspace or self.w
        preview = self.service.preview(workspace, {'text': self.request, 'share_memory': False})
        return self.service.accept(workspace, preview['draft_id'], {'offer_id': offer, 'confirmed': True})

    @staticmethod
    def finish(messages, tools):
        output = last_tool(messages)
        return call('answer_reviewed', {
            'text': '服务条件和申请状态以本次查询为准。预览不代表已提交申请、资格获批或优惠兑现；由你在工作台另行确认。',
            'claims': [{'text': '服务依据来自本轮查询。', 'evidence_ids': [output['evidence_id']]}],
            'draft_ids': [], 'followups': [],
        })

    def test_schema_has_only_empty_preview_status_and_no_accept_or_execute(self):
        schemas = {row['function']['name']: row['function']['parameters'] for row in native_tools()}
        for name in ('service_offer_preview', 'service_application_status'):
            self.assertIn(name, schemas)
            self.assertEqual(schemas[name]['properties'], {})
            self.assertFalse(schemas[name]['additionalProperties'])
        tool = self.tools()
        before = self.financial_state()
        for name in ('service_accept', 'service_offer_accept', 'submit_simulated_service_application',
                     'confirm_operation', 'execute_operation'):
            with self.subTest(name=name), self.assertRaises(BankError) as caught:
                tool.invoke(name, {})
            self.assertEqual(caught.exception.code, 'TOOL_FORBIDDEN')
        for payload in ({'text': '模型伪造愿意买理财'}, {'share_memory': True}, {'confirmed': True}):
            with self.subTest(payload=payload), self.assertRaises(BankError) as caught:
                tool.invoke('service_offer_preview', payload)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.financial_state(), before)

    def test_preview_uses_original_conditions_and_only_finite_current_rule_offers(self):
        tool = self.tools()
        before = self.financial_state()
        data = tool.invoke('service_offer_preview', {})['data']
        self.assertEqual(data['status'], 'previewed')
        self.assertTrue(data['conditions']['salary_deposit_intent'])
        self.assertTrue(data['conditions']['investment_purchase_refused'])
        allowed = {row['offer_id']: row for row in self.rules['offers']}
        self.assertEqual({row['offer_id'] for row in data['offers']}, set(allowed))
        for row in data['offers']:
            self.assertFalse(row['requires_investment_purchase'])
            self.assertEqual(row['review_status'], 'rules_checked')
            self.assertEqual(row['eligibility_status'], 'conditional_application')
            for key in ('conditions', 'currency', 'monthly_value_minor', 'duration_months',
                        'max_cost_minor', 'valid_from', 'valid_through'):
                self.assertEqual(row[key], allowed[row['offer_id']][key])
            self.assertEqual(row['monthly_value_minor'] * row['duration_months'], row['max_cost_minor'])
        self.assertEqual(self.financial_state(), before)
        self.assertFalse(tool.drafts)

    def test_refusal_to_migrate_salary_does_not_qualify_for_salary_offer(self):
        # 这是当前服务解析的安全语义反例，不能以换措辞或放宽断言绕过。
        text = '我不愿意迁入工资账户，不接受购买理财作为优惠条件，希望协商服务费减免。'
        data = self.tools(text).invoke('service_offer_preview', {})['data']
        self.assertFalse(data['conditions']['salary_deposit_intent'])
        self.assertTrue(data['conditions']['investment_purchase_refused'])
        self.assertEqual({row['offer_id'] for row in data['offers']}, {'SYNTH-E-STATEMENT-FEE-90D'})

    def test_preview_never_discloses_enabled_confirmed_personal_memory(self):
        self.g.memories.create(self.w, {'category': 'goal', 'value': '私人进修目标，不能向银行披露'})
        self.g.memories.set_enabled(self.w, {'enabled': True})
        tool = self.tools()
        before = self.financial_state()
        with patch.object(self.g.memories, 'read_relevant', side_effect=AssertionError('原生预览不可披露个人记忆')):
            data = tool.invoke('service_offer_preview', {})['data']
        self.assertEqual(data['shared_memory'], [])
        with closing(self.g.bank.connect()) as db:
            row = db.execute('SELECT share_memory,memory_revision,memory_node_ids_json FROM service_negotiation_drafts WHERE id=?',
                             (data['draft_id'],)).fetchone()
        self.assertEqual(row['share_memory'], 0)
        self.assertIsNone(row['memory_revision'])
        self.assertEqual(json.loads(row['memory_node_ids_json']), [])
        self.assertNotIn('私人进修', json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.financial_state(), before)

    def test_same_turn_caches_one_preview_and_next_turn_supersedes_previous(self):
        first_tool = self.tools(request_id='first-turn')
        first = first_tool.invoke('service_offer_preview', {})['data']
        again = first_tool.invoke('service_offer_preview', {})['data']
        self.assertEqual(first, again)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM service_negotiation_drafts WHERE workspace_id=?',
                                        (self.w,)).fetchone()[0], 1)
        next_tool = self.tools('只协商服务费减免，不愿迁入工资。', 'second-turn')
        second = next_tool.invoke('service_offer_preview', {})['data']
        self.assertNotEqual(first['draft_id'], second['draft_id'])
        self.assert_stale(first_tool.fresh)
        next_tool.fresh()
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT status FROM service_negotiation_drafts WHERE id=?',
                                        (first['draft_id'],)).fetchone()[0], 'superseded')

    def test_clarification_and_no_offer_are_cached_without_financial_effects(self):
        for status, text in (('clarification', '你好'), ('no_offer', '希望协商服务费减免')):
            with self.subTest(status=status):
                if status == 'no_offer':
                    expired = copy.deepcopy(self.rules)
                    for row in expired['offers']:
                        row['valid_from'] = '2020-01-01'
                        row['valid_through'] = '2020-01-01'
                    self.write_rules(expired)
                tool = self.tools(text, 'cached-' + status)
                before = self.financial_state()
                with patch.object(self.service, 'preview', wraps=self.service.preview) as preview:
                    first = tool.invoke('service_offer_preview', {})['data']
                    second = tool.invoke('service_offer_preview', {})['data']
                    self.assertEqual(preview.call_count, 1)
                self.assertEqual(first['status'], status)
                self.assertEqual(second, first)
                self.assertIsNone(first['draft_id'])
                self.assertFalse(first['offers'])
                self.assertEqual(self.financial_state(), before)

    def test_status_reads_actual_workspace_receipts_without_preparing_preview(self):
        own = self.seed_application()
        other_workspace, _ = self.g.create_session()
        other = self.seed_application(other_workspace)
        tool = self.tools('查刚才申请到了哪一步')
        before = self.financial_state()
        with patch.object(self.service, 'preview', side_effect=AssertionError('状态查询不能生成新预览')):
            data = tool.invoke('service_application_status', {})['data']
        self.assertEqual([row['application_id'] for row in data['applications']], [own['application_id']])
        self.assertNotIn(other['application_id'], json.dumps(data))
        self.assertEqual(data['applications'][0]['status'], 'submitted_simulation_pending_eligibility')
        self.assertEqual(self.financial_state(), before)
        self.assertIsNone(tool.service_preview)

    def test_rule_content_change_with_same_schema_and_ids_is_stale(self):
        for name in ('service_offer_rules', 'service_offer_preview', 'service_application_status'):
            with self.subTest(name=name):
                self.write_rules(self.rules)
                tool = self.tools(request_id=name)
                tool.invoke(name, {})
                changed = copy.deepcopy(self.rules)
                changed['offers'][0]['conditions'].append('同版本条款已改变')
                self.write_rules(changed)
                self.assert_stale(tool.fresh)
                self.assert_stale(lambda: BankTools.assert_service_snapshot(self.g, self.w, tool.service_snapshot()))

    def test_observed_application_receipt_change_is_stale(self):
        self.seed_application()
        tool = self.tools()
        tool.invoke('service_application_status', {})
        self.seed_application(offer='SYNTH-PAYROLL-FEE-180D')
        self.assert_stale(tool.fresh)

    def test_snapshot_cannot_be_mutated_reused_cross_workspace_or_used_after_expiry(self):
        tool = self.tools()
        data = tool.invoke('service_offer_preview', {})['data']
        snapshot = tool.service_snapshot()
        snapshot['rules_sha256'] = 'forged'
        self.assert_stale(lambda: BankTools.assert_service_snapshot(self.g, self.w, snapshot))
        tool.fresh()
        other, _ = self.g.create_session()
        self.assert_stale(lambda: BankTools.assert_service_snapshot(self.g, other, tool.service_snapshot()))
        with closing(self.g.bank.connect()) as db:
            expires = db.execute('SELECT expires_at FROM service_negotiation_drafts WHERE id=?', (data['draft_id'],)).fetchone()[0]
        with patch.object(self.g.bank, 'clock', return_value=expires + 1):
            self.assert_stale(lambda: BankTools.assert_service_snapshot(self.g, self.w, tool.service_snapshot()))

    def test_rules_unavailable_or_invalid_are_errors_not_fake_offers(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.rule_path.unlink(missing_ok=True)
                else:
                    self.write_rules({'schema_version': 'invalid', 'environment': 'synthetic', 'offers': []})
                with self.assertRaises(BankError) as caught:
                    self.tools().invoke('service_offer_preview', {})
                self.assertEqual(caught.exception.code, 'SERVICE_RULES_UNAVAILABLE' if missing else 'SERVICE_RULES_INVALID')
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(self.service.applications(self.w)['applications'], [])

    def test_malformed_rule_date_interval_is_technical_error_not_no_offer(self):
        malformed = copy.deepcopy(self.rules)
        malformed['offers'][0]['valid_from'] = '2026-10-01'
        malformed['offers'][0]['valid_through'] = '2020-01-01'
        self.write_rules(malformed)
        before = self.financial_state()
        with self.assertRaises(BankError) as caught:
            self.tools().invoke('service_offer_preview', {})
        self.assertEqual(caught.exception.code, 'SERVICE_RULES_INVALID')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.financial_state(), before)

    def test_conversation_primary_and_reviewer_receive_preserved_preview_conditions(self):
        def finish(messages, schemas):
            data = last_tool(messages)['data']
            self.assertEqual(data['status'], 'previewed')
            self.assertTrue(data['conditions']['salary_deposit_intent'])
            self.assertTrue(data['conditions']['investment_purchase_refused'])
            self.assertEqual(len(data['offers']), 2)
            self.assertTrue(all(row['requires_investment_purchase'] is False for row in data['offers']))
            self.assertTrue(all(row['conditions'] and row['valid_through'] for row in data['offers']))
            return self.finish(messages, schemas)
        primary = ScriptedPrimary(call('service_offer_preview', {}), finish)
        reviewer = Reviewer()
        before = self.financial_state()
        result = ConversationAgent(primary, reviewer).process(self.g, self.w, self.request)
        self.assertFalse(result.get('isFallback'))
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(result['service_negotiation_request']['text'], self.request)
        self.assertIsNotNone(result['service_snapshot']['preview'])
        evidence = next(row for row in reviewer.packets[0]['evidence'] if row['tool'] == 'service_offer_preview')
        self.assertTrue(evidence['data']['conditions']['investment_purchase_refused'])
        self.assertEqual(len(evidence['data']['offers']), 2)
        self.assertEqual(self.financial_state(), before)

    def test_conversation_status_only_reports_actual_receipt_without_ui_preview_request(self):
        application = self.seed_application()
        def finish(messages, schemas):
            data = last_tool(messages)['data']
            self.assertEqual(len(data['applications']), 1)
            self.assertEqual(data['applications'][0]['status'], application['status'])
            self.assertTrue(data['applications'][0]['terms']['conditions'])
            return self.finish(messages, schemas)
        reviewer = Reviewer()
        before = self.financial_state()
        result = ConversationAgent(ScriptedPrimary(call('service_application_status', {}), finish), reviewer).process(
            self.g, self.w, '查已提交的服务申请，不要再准备新方案')
        self.assertFalse(result.get('isFallback'))
        self.assertNotIn('service_negotiation_request', result)
        self.assertIsNone(result['service_snapshot']['preview'])
        self.assertIsNotNone(result['service_snapshot']['applications_sha256'])
        evidence = next(row for row in reviewer.packets[0]['evidence'] if row['tool'] == 'service_application_status')
        self.assertEqual(evidence['data']['applications'][0]['status'], application['status'])
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(self.financial_state(), before)

    def test_rule_change_during_independent_review_blocks_even_approved_model(self):
        def change(_):
            rules = copy.deepcopy(self.rules)
            rules['offers'][0]['conditions'].append('审核期间已改变')
            self.write_rules(rules)
        result = ConversationAgent(ScriptedPrimary(call('service_offer_preview', {}), self.finish),
                                   Reviewer(hook=change)).process(self.g, self.w, self.request)
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'], 'STALE_SERVICE_EVIDENCE')
        self.assertEqual(result['agent_review']['status'], 'blocked')
        self.assertIsNone(result.get('service_snapshot'))
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(self.service.applications(self.w)['applications'], [])

    def test_receipt_change_during_independent_review_blocks_stale_status(self):
        self.seed_application()
        reviewer = Reviewer(hook=lambda _: self.seed_application(offer='SYNTH-PAYROLL-FEE-180D'))
        result = ConversationAgent(ScriptedPrimary(call('service_application_status', {}), self.finish), reviewer).process(
            self.g, self.w, '查询服务申请当前状态')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'], 'STALE_SERVICE_EVIDENCE')
        self.assertEqual(result['agent_review']['status'], 'blocked')
        self.assertEqual(result['bank_actions'], [])


if __name__ == '__main__':
    unittest.main()
