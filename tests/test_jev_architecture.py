"""验证显式Jev阶段、同轮deadline和不改变权限的路由合同；全为本地假响应。"""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.jev_agent import JevConversationAgent, SkillPrimedClient
from agent_runtime.jev_client import JevClient, JevError
from agent_runtime.native_client import DeepSeekToolClient
from finance_gateway import FinanceGateway
from simulated_bank import BankError


class CountingPrimary:
    model_id = 'offline-primary'
    billing_mode = 'unpaid_fixture'
    def __init__(self):
        self.sent = []
    def serialized_request(self, messages, tools):
        return json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False,
                          separators=(',', ':')).encode()
    def request_input_bound(self, messages, tools):
        return len(self.serialized_request(messages, tools))
    def complete(self, messages, tools, **kwargs):
        self.sent.append(self.serialized_request(messages, tools))
        return {'role': 'assistant', 'content': 'fixture'}, {}


class CountingJudge:
    def __init__(self, *, choice='cashflow', confidence=1, error=None):
        self.choice, self.confidence, self.error = choice, confidence, error
        self.calls = []
    def evaluate(self, state, questions, *, deadline):
        self.calls.append({'state': state, 'deadline': deadline})
        if self.error:
            raise self.error
        criteria = questions['first_skill']['criteria']
        return {'first_skill': {'type': 'choice', 'choice': self.choice,
            'confidence': self.confidence,
            'probabilities': {name: float(name == self.choice) for name in criteria}}}, {
            'request_sent': True, 'outcome': 'completed',
            'usage': {'input_tokens': 10, 'output_tokens': 1}}


def messages():
    return [{'role': 'system', 'content': 'trusted'}, {'role': 'user',
        'content': json.dumps({'current_request': '匿名化现金流规划问句',
                              'server_context': {'private': 'must-not-reach-judge'}})}]


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


QUESTIONS = {'x': {'type': 'choice', 'criteria': {'a': 'A', 'b': 'B'}}}
ANSWER = {'model': 'jev-1.13.0', 'usage': {'input_tokens': 5, 'output_tokens': 2},
          'answers': {'x': {'type': 'choice', 'choice': 'a', 'confidence': 1,
                            'probabilities': {'a': 1, 'b': 0}}}}


class JevArchitectureTest(unittest.TestCase):
    def test_estimation_serialization_and_complete_never_prepare_implicitly(self):
        primary, judge, debits = CountingPrimary(), CountingJudge(), []
        wrapper = SkillPrimedClient(primary, judge, mode='active', debit=debits.append)
        original = messages()
        expected = primary.serialized_request(original, [])
        for _ in range(2):
            self.assertEqual(wrapper.request_input_bound(original, []), len(expected))
            self.assertEqual(wrapper.serialized_request(original, []), expected)
        wrapper.complete(original, [])
        self.assertEqual(primary.sent, [expected])
        self.assertEqual(judge.calls, [])
        self.assertEqual(debits, [])
        self.assertFalse(wrapper.prepared)

    def test_explicit_prepare_once_and_frozen_augmentation_matches_sending(self):
        primary, judge, debits, events = CountingPrimary(), CountingJudge(), [], []
        wrapper = SkillPrimedClient(primary, judge, mode='active', debit=debits.append)
        original, deadline = messages(), time.monotonic() + 10
        wrapper.prepare_turn(original, deadline=deadline, on_event=events.append)
        wrapper.prepare_turn(original, deadline=deadline, on_event=events.append)
        wire = wrapper.serialized_request(original, [])
        self.assertEqual(wrapper.request_input_bound(original, []), len(wire))
        wrapper.complete(original, [])
        self.assertEqual(primary.sent, [wire])
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(judge.calls[0], {'state': {'current_request': '匿名化现金流规划问句'}, 'deadline': deadline})
        self.assertEqual(debits, [11])
        self.assertEqual(wrapper.routing_skill, 'cashflow')
        self.assertEqual(original[0]['content'], 'trusted')
        self.assertEqual([e['status'] for e in events], ['started', 'completed'])
        self.assertNotIn('must-not-reach-judge', json.dumps(events))

    def test_shadow_and_low_confidence_consume_usage_without_changing_route_or_wire(self):
        for mode, confidence, reason in [('shadow', 1, 'shadow_only'),
                                         ('active', 0.2, 'none_or_uncertain')]:
            with self.subTest(mode=mode, confidence=confidence):
                primary, judge, debits = CountingPrimary(), CountingJudge(confidence=confidence), []
                wrapper = SkillPrimedClient(primary, judge, mode=mode, debit=debits.append)
                original = messages()
                wrapper.prepare_turn(original, deadline=time.monotonic() + 10)
                self.assertEqual(wrapper.serialized_request(original, []), primary.serialized_request(original, []))
                self.assertIsNone(wrapper.routing_skill)
                self.assertEqual(wrapper.selection['reason'], reason)
                self.assertEqual(debits, [11])

    def test_existing_trusted_scope_skips_judge_and_preserves_wire(self):
        primary, judge, debits = CountingPrimary(), CountingJudge(), []
        primary.allowed_tools = frozenset({'monthly_statement', 'answer_facts'})
        wrapper = SkillPrimedClient(primary, judge, mode='active', debit=debits.append)
        original = messages()
        wrapper.prepare_turn(original, deadline=time.monotonic() + 10)
        self.assertEqual(judge.calls, [])
        self.assertEqual(debits, [])
        self.assertIsNone(wrapper.routing_skill)
        self.assertEqual(wrapper.selection['reason'], 'trusted_tool_scope_already_selected')
        self.assertEqual(wrapper.serialized_request(original, []), primary.serialized_request(original, []))

    def test_wrapper_preserves_underlying_scope_and_protocol_capabilities(self):
        primary = CountingPrimary()
        primary.allowed_tools = frozenset({'monthly_statement', 'answer_facts'})
        primary.provider = 'ai_ping'
        primary.supports_financial_metric_catalog = True
        primary.supports_compact_metric_sources = True
        primary.output_token_limit = 2800
        wrapper = SkillPrimedClient(primary, CountingJudge(), mode='active', debit=lambda _: None)
        self.assertIs(wrapper.allowed_tools, primary.allowed_tools)
        self.assertEqual(wrapper.provider, 'ai_ping')
        self.assertTrue(wrapper.supports_financial_metric_catalog)
        self.assertTrue(wrapper.supports_compact_metric_sources)
        self.assertEqual(wrapper.output_token_limit, 2800)

    def test_missing_projected_request_never_reads_other_context_or_calls_judge(self):
        judge = CountingJudge()
        wrapper = SkillPrimedClient(CountingPrimary(), judge, mode='active', debit=lambda _: None)
        wrapper.prepare_turn([{'role': 'system', 'content': 'trusted'},
                              {'role': 'user', 'content': 'raw unprojected text'}],
                             deadline=time.monotonic() + 10)
        self.assertEqual(judge.calls, [])
        self.assertEqual(wrapper.selection['reason'], 'no_boundary_projected_current_request')

    def test_unknown_paid_usage_stops_prepare_without_recording_zero(self):
        primary, debits = CountingPrimary(), []
        judge = CountingJudge(error=JevError('timeout', {'request_sent': True, 'usage': None}))
        wrapper = SkillPrimedClient(primary, judge, mode='active', debit=debits.append)
        with self.assertRaises(BankError) as raised:
            wrapper.prepare_turn(messages(), deadline=time.monotonic() + 10)
        self.assertEqual(raised.exception.code, 'MODEL_USAGE_UNVERIFIED')
        self.assertIsNone(wrapper.receipts[0]['usage'])
        self.assertEqual(debits, [])
        self.assertEqual(primary.sent, [])
        self.assertIsNone(wrapper.routing_skill)

    def test_known_failed_call_is_debited_but_does_not_apply_skill(self):
        debits = []
        judge = CountingJudge(error=JevError('jev_model_version_mismatch', {
            'request_sent': True, 'usage': {'input_tokens': 10, 'output_tokens': 1}}))
        wrapper = SkillPrimedClient(CountingPrimary(), judge, mode='active', debit=debits.append)
        wrapper.prepare_turn(messages(), deadline=time.monotonic() + 10)
        self.assertEqual(debits, [11])
        self.assertIsNone(wrapper.routing_skill)

    def test_expired_prepare_does_not_call_judge(self):
        judge = CountingJudge()
        wrapper = SkillPrimedClient(CountingPrimary(), judge, mode='active', debit=lambda _: None)
        with self.assertRaises(BankError) as raised:
            wrapper.prepare_turn(messages(), deadline=time.monotonic() - 1)
        self.assertEqual(raised.exception.code, 'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(judge.calls, [])

    def test_transport_expired_before_key_load_has_no_key_or_network_access(self):
        touched = []
        client = JevClient(key_loader=lambda: touched.append('key'),
                           urlopen=lambda *a, **k: touched.append('network'))
        with patch('agent_runtime.jev_client.time.monotonic', return_value=100):
            with self.assertRaises(JevError) as raised:
                client.evaluate('state', QUESTIONS, deadline=99)
        self.assertEqual(raised.exception.code, 'jev_deadline_exceeded')
        self.assertFalse(raised.exception.receipt['request_sent'])
        self.assertEqual(touched, [])

    def test_transport_rechecks_deadline_after_key_load(self):
        touched = []
        client = JevClient(key_loader=lambda: 'offline-key',
                           urlopen=lambda *a, **k: touched.append('network'))
        with patch('agent_runtime.jev_client.time.monotonic', side_effect=[100, 102]):
            with self.assertRaises(JevError) as raised:
                client.evaluate('state', QUESTIONS, deadline=101)
        self.assertFalse(raised.exception.receipt['request_sent'])
        self.assertEqual(touched, [])

    def test_transport_timeout_uses_remaining_turn_time_and_never_exposes_key(self):
        timeouts = []
        def opener(request, *, timeout):
            timeouts.append(timeout)
            return Response(json.dumps(ANSWER).encode())
        client = JevClient(key_loader=lambda: 'offline-secret', urlopen=opener, timeout=5)
        with patch('agent_runtime.jev_client.time.monotonic', side_effect=[100, 101, 101.1, 102]):
            _, receipt = client.evaluate('state', QUESTIONS, deadline=103)
        self.assertAlmostEqual(timeouts[0], 1.9)
        self.assertNotIn('offline-secret', json.dumps(receipt))

    def test_transport_deadline_can_expire_between_key_load_and_send(self):
        touched = []
        client = JevClient(key_loader=lambda: 'offline-key',
                           urlopen=lambda *a, **k: touched.append('network'))
        with patch('agent_runtime.jev_client.time.monotonic', side_effect=[100, 100.9, 101.1]):
            with self.assertRaises(JevError) as raised:
                client.evaluate('state', QUESTIONS, deadline=101)
        self.assertFalse(raised.exception.receipt['request_sent'])
        self.assertEqual(touched, [])

    def test_agent_unknown_usage_stops_primary_and_preserves_progressive_configuration(self):
        primary = CountingPrimary()
        judge = CountingJudge(error=JevError('timeout', {'request_sent': True, 'usage': None}))
        with tempfile.TemporaryDirectory() as directory:
            gateway = FinanceGateway(Path(directory) / 'bank.sqlite3')
            workspace, _ = gateway.create_session()
            agent = JevConversationAgent(primary=primary, judge=judge, jev_mode='active',
                                         tool_routing='progressive')
            result = agent.process(gateway, workspace, '查询余额')
        self.assertEqual(agent.tool_routing, 'progressive')
        self.assertEqual(result['agent_runtime']['failure'], 'MODEL_USAGE_UNVERIFIED')
        self.assertFalse(result['agent_runtime']['usage_complete'])
        self.assertIsNone(result['agent_runtime']['total_tokens'])
        self.assertEqual(primary.sent, [])
        self.assertEqual(len(judge.calls), 1)


if __name__ == '__main__':
    unittest.main()
