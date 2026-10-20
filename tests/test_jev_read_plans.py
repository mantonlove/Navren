"""有限计划合同的本地验证；所有 Jev/主模型响应均为假对象，无网络。"""
import json
import time
import unittest

from agent_runtime.jev_client import JevClient, JevError
from agent_runtime.jev_plans import ReadPlanClient, deterministic_read_kind
from simulated_bank import BankError


PUBLIC_REQUEST = '2026-09-24华夏纯债债券A的渠道风险披露是什么？'
SERVICE_REQUEST = '我愿意迁入工资账户，但不接受购买理财作为优惠条件；希望协商服务费减免。'


def projected(text=PUBLIC_REQUEST):
    return [{'role': 'system', 'content': '维护者策略'},
            {'role': 'user', 'content': json.dumps({'current_request': text,
                 'server_context': {'private_account': '不可发送账户'},
                 'history': ['不可发送历史']}, ensure_ascii=False)}]


def schemas(*names):
    return [{'type': 'function', 'function': {'name': name,
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
            for name in names]


class Primary:
    model_id = 'offline-primary'
    billing_mode = 'unpaid_fixture'
    provider = 'offline'
    supports_compact_metric_sources = True
    supports_financial_metric_catalog = True
    output_token_limit = 3210
    def __init__(self):
        self.allowed_tools, self.sent = None, []
    def serialized_request(self, messages, tools):
        return json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False).encode()
    def request_input_bound(self, messages, tools):
        return len(self.serialized_request(messages, tools))
    def complete(self, messages, tools, **kwargs):
        self.sent.append((messages, tools, kwargs))
        return {'role': 'assistant', 'content': '本地协议对象'}, {'fixture': True}


class Judge:
    def __init__(self, choice='public_evidence_status', confidence=1.0,
                 probability=1.0, error=None, bad=None, usage=None):
        self.choice, self.confidence, self.probability = choice, confidence, probability
        self.error, self.bad, self.calls = error, bad, []
        self.usage = {'input_tokens': 10, 'output_tokens': 1} if usage is None else usage
    def evaluate(self, state, questions, *, deadline):
        self.calls.append({'state': state, 'questions': questions, 'deadline': deadline})
        if self.error:
            raise self.error
        alternatives = [key for key in questions['read_plan']['criteria'] if key != self.choice]
        probabilities = {key: (self.probability if key == self.choice else
                               (1 - self.probability) / len(alternatives))
                         for key in questions['read_plan']['criteria']}
        answer = {'type': 'choice', 'choice': self.choice, 'confidence': self.confidence,
                  'probabilities': probabilities}
        if self.bad == 'type':
            answer['type'] = 'score'
        elif self.bad == 'choice':
            answer['choice'] = 'execute_purchase'
        elif self.bad == 'probability':
            answer['confidence'] = float('nan')
        elif self.bad == 'keys':
            return {'wrong_question': answer}, self.receipt()
        return {'read_plan': answer}, self.receipt()
    def receipt(self):
        return {'request_sent': True, 'outcome': 'completed', 'usage': self.usage}


class ReadPlanTests(unittest.TestCase):
    def client(self, judge=None, mode='active'):
        primary, debit = Primary(), []
        client = ReadPlanClient(primary, judge or Judge(), mode=mode, debit=debit.append)
        return client, primary, debit

    def prepare(self, client, text=PUBLIC_REQUEST):
        deadline = time.monotonic() + 10
        client.prepare_turn(projected(text), deadline=deadline)
        return deadline

    def test_preparation_once_only_sees_projected_current_request(self):
        judge = Judge()
        client, primary, debit = self.client(judge)
        deadline = self.prepare(client)
        client.prepare_turn(projected(SERVICE_REQUEST), deadline=deadline)
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(judge.calls[0]['state'], {'current_request': PUBLIC_REQUEST})
        self.assertEqual(judge.calls[0]['deadline'], deadline)
        self.assertEqual(debit, [11])
        self.assertEqual(primary.sent, [])
        self.assertEqual(client.routing_skills, ('knowledge-retrieval', 'product-research'))
        self.assertFalse(client.selection['applied'])

    def test_host_public_retrieval_trailer_is_not_the_current_user_request(self):
        client, primary, _ = self.client()
        messages = projected() + [{'role': 'user', 'content': json.dumps({
            'public_retrieval': {'registered_public_identities': ['not-a-user-question']}})}]
        client.prepare_turn(messages, deadline=time.monotonic() + 10)
        self.assertEqual(client.judge.calls[0]['state'], {'current_request': PUBLIC_REQUEST})
        self.assertTrue(client.selection['eligible'])
        self.assertEqual(primary.sent, [])

    def test_active_mode_pins_official_client_route_and_validates_thresholds(self):
        class WrongRoute(JevClient):
            url = 'https://invalid.example/not-typesafe'
        class WrongModel(JevClient):
            model_id = 'unregistered-model'
        for judge in [WrongRoute(), WrongModel()]:
            with self.subTest(judge=judge), self.assertRaisesRegex(
                    ValueError, 'jev_provider_must_use_pinned_official_typesafe_route'):
                ReadPlanClient(Primary(), judge, debit=lambda value: None)
        with self.assertRaisesRegex(ValueError, 'jev_provider_required'):
            ReadPlanClient(Primary(), None, debit=lambda value: None)
        for threshold in [True, float('nan'), -0.1, 1.1]:
            with self.subTest(threshold=threshold), self.assertRaisesRegex(
                    ValueError, 'invalid_jev_threshold'):
                ReadPlanClient(Primary(), Judge(), probability=threshold, debit=lambda value: None)

    def test_wire_and_primary_passthrough_do_not_prepare_or_invent_usage(self):
        judge = Judge()
        client, primary, debit = self.client(judge)
        messages, tools = projected(), schemas('knowledge_status')
        self.assertEqual(client.serialized_request(messages, tools), primary.serialized_request(messages, tools))
        self.assertEqual(client.request_input_bound(messages, tools), primary.request_input_bound(messages, tools))
        self.assertEqual(client.complete(messages, tools, request_id='fixture')[1], {'fixture': True})
        self.assertEqual(len(primary.sent), 1)
        self.assertFalse(client.prepared)
        self.assertEqual(judge.calls, [])
        self.assertEqual(debit, [])
        self.assertEqual(client.provider, 'offline')
        self.assertTrue(client.supports_financial_metric_catalog)
        self.assertEqual(client.output_token_limit, 3210)

    def test_consumes_fixed_empty_argument_read_once_without_model_or_tool_execution(self):
        for choice, expected in [('public_evidence_status', 'knowledge_status'),
                                 ('bank_service_rules', 'service_offer_rules')]:
            with self.subTest(choice=choice):
                client, primary, _ = self.client(Judge(choice=choice))
                self.prepare(client, SERVICE_REQUEST if choice == 'bank_service_rules' else PUBLIC_REQUEST)
                message, receipt = client.read_plan(schemas(expected), 0)
                call = message['tool_calls'][0]
                self.assertEqual(call['function'], {'name': expected, 'arguments': '{}'})
                self.assertEqual(receipt['outcome'], 'consumed_not_executed')
                self.assertFalse(receipt['request_sent'])
                self.assertEqual(receipt['model_calls'], 0)
                self.assertNotIn('usage', receipt)
                self.assertEqual(primary.sent, [])
                self.assertTrue(client.selection['applied'])
                self.assertIsNone(client.read_plan(schemas(expected), 0))
                self.assertEqual(len(client.local_receipts), 1)

    def test_none_low_confidence_and_low_probability_keep_original_path(self):
        for judge, expected in [(Judge(choice='none'), 'none'),
                                (Judge(confidence=0.59), 'low_confidence'),
                                (Judge(probability=0.74), 'low_confidence')]:
            with self.subTest(expected=expected, confidence=judge.confidence):
                client, _, debit = self.client(judge)
                self.prepare(client)
                self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))
                self.assertEqual(client.routing_skills, ())
                self.assertEqual(client.selection['reason'], expected)
                self.assertEqual(debit, [11])

    def test_bad_answer_type_keys_choice_and_probability_are_metered_but_not_applied(self):
        for bad, expected in [('type', 'jev_answer_type_mismatch'),
                              ('keys', 'jev_answer_keys_mismatch'),
                              ('choice', 'jev_invalid_choice'),
                              ('probability', 'jev_invalid_probability')]:
            with self.subTest(bad=bad):
                client, _, debit = self.client(Judge(bad=bad))
                self.prepare(client)
                self.assertEqual(client.selection['reason'], expected)
                self.assertEqual(debit, [11])
                self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))

    def test_missing_schema_or_final_only_falls_back_and_cannot_retry_plan(self):
        for tools in [schemas('answer_facts', 'answer_reviewed'), schemas('account_summary'), []]:
            with self.subTest(tools=tools):
                client, _, _ = self.client()
                self.prepare(client)
                self.assertIsNone(client.read_plan(tools, 0))
                self.assertEqual(client.selection['reason'], 'required_read_schema_unavailable')
                self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))

    def test_preselected_hard_scope_skips_judge_and_later_restriction_blocks_plan(self):
        for allowed in [frozenset(), frozenset({'account_summary'})]:
            client, primary, _ = self.client()
            primary.allowed_tools = allowed
            self.prepare(client)
            self.assertEqual(client.judge.calls, [])
            self.assertEqual(client.selection['reason'], 'trusted_tool_scope_already_selected')
        client, primary, _ = self.client()
        self.prepare(client)
        primary.allowed_tools = frozenset({'account_summary'})
        self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))
        self.assertEqual(client.selection['reason'], 'trusted_tool_scope_conflict')

    def test_off_and_shadow_modes_do_not_return_executable_plan(self):
        for mode in ['off', 'shadow']:
            client, _, debit = self.client(mode=mode)
            self.prepare(client)
            self.assertEqual(client.routing_skills, ())
            self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))
            self.assertEqual(len(client.judge.calls), 0 if mode == 'off' else 1)
            self.assertEqual(debit, [] if mode == 'off' else [11])

    def test_raw_or_invalid_last_request_does_not_read_prior_history(self):
        for last in [{'role': 'user', 'content': '原始用户文本'},
                     {'role': 'user', 'content': json.dumps({'server_context': {'private': '私有'}})}]:
            client, _, _ = self.client()
            client.prepare_turn(projected() + [last], deadline=time.monotonic() + 10)
            self.assertEqual(client.judge.calls, [])
            self.assertEqual(client.selection['reason'], 'no_boundary_projected_current_request')

    def test_sent_unknown_usage_stops_and_not_sent_failure_can_fall_back(self):
        unknown = JevError('timeout', {'request_sent': True, 'usage': None})
        client, primary, debit = self.client(Judge(error=unknown))
        with self.assertRaises(BankError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, 'MODEL_USAGE_UNVERIFIED')
        self.assertEqual(client.selection['reason'], 'MODEL_USAGE_UNVERIFIED')
        self.assertEqual(primary.sent, [])
        self.assertEqual(debit, [])
        self.assertIsNone(client.receipts[0]['usage'])
        client, primary, _ = self.client(Judge(error=JevError('jev_key_unavailable',
                                              {'request_sent': False, 'usage': None})))
        self.prepare(client)
        self.assertEqual(client.selection['reason'], 'jev_key_unavailable')
        self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))
        client.complete(projected(), [])
        self.assertEqual(len(primary.sent), 1)

    def test_invalid_paid_usage_counts_do_not_enable_plan(self):
        for usage in [{'input_tokens': -1, 'output_tokens': 0},
                      {'input_tokens': True, 'output_tokens': 0}, {'input_tokens': 1}]:
            with self.subTest(usage=usage):
                client, _, _ = self.client(Judge(usage=usage))
                with self.assertRaises(BankError) as raised:
                    self.prepare(client)
                self.assertEqual(raised.exception.code, 'MODEL_USAGE_UNVERIFIED')
                self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))

    def test_deadline_and_first_round_limits_prevent_late_plan(self):
        client, _, _ = self.client()
        with self.assertRaises(BankError) as raised:
            client.prepare_turn(projected(), deadline=time.monotonic() - 1)
        self.assertEqual(raised.exception.code, 'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(client.judge.calls, [])
        client, _, _ = self.client()
        self.prepare(client)
        self.assertIsNone(client.read_plan(schemas('knowledge_status'), 1))
        self.assertEqual(client.selection['reason'], 'first_round_only')
        client, _, _ = self.client()
        self.prepare(client)
        client.deadline = time.monotonic() - 1
        self.assertIsNone(client.read_plan(schemas('knowledge_status'), 0))
        self.assertEqual(client.selection['reason'], 'plan_deadline_exceeded')

    def test_general_cash_and_relative_calendar_skip_is_shared_not_exact_question_matching(self):
        for question, expected in [('查询账户余额', 'balance'), ('看看银行卡里的余额', 'balance'),
                                   ('下个月财务日历', 'calendar:next_month'),
                                   ('查看明天的日历', 'calendar:tomorrow')]:
            with self.subTest(question=question):
                self.assertEqual(deterministic_read_kind(question), expected)
                client, _, _ = self.client()
                self.prepare(client, question)
                self.assertEqual(client.judge.calls, [])
                self.assertEqual(client.selection['reason'], 'deterministic_read_skip')
        for question in ['修改下个月日历', '查询余额，再推荐理财', '本月和下月日历', '日历']:
            with self.subTest(question=question):
                self.assertIsNone(deterministic_read_kind(question))


if __name__ == '__main__':
    unittest.main()
