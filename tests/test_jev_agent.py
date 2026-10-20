import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_runtime.jev_agent import JevConversationAgent, SkillPrimedClient
from agent_runtime.jev_client import JevClient, JevError
from agent_runtime.native_client import DeepSeekToolClient
from finance_gateway import FinanceGateway


def tool(name, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-' + name,
        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class Judge:
    def __init__(self, selection='banking', confidence=1.0, error=None):
        self.selection, self.confidence, self.error = selection, confidence, error
        self.states = []

    def evaluate(self, state, questions, *, deadline=None):
        self.states.append(state)
        if self.error:
            raise self.error
        probabilities = {k: float(k == self.selection) for k in questions['first_skill']['criteria']}
        return {'first_skill': {'type': 'choice', 'choice': self.selection,
            'confidence': self.confidence, 'probabilities': probabilities}}, {
            'request_sent': True, 'outcome': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 1}}


class Primary(DeepSeekToolClient):
    billing_mode = 'unpaid_fixture'
    def __init__(self):
        super().__init__()
        self.messages = []
        self.bound_calls = []

    def complete(self, messages, tools, **kwargs):
        self.messages.append(messages)
        self.bound_calls.append(len(self.serialized_request(messages, tools)))
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        if not outputs:
            result = tool('account_summary', {})
        else:
            ids = [f['id'] for f in outputs[-1].get('facts', []) if '现金合计' in f['text']]
            result = tool('answer_facts', {'fact_ids': ids})
        return result, {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}


class NeverReviewer:
    billing_mode = 'unpaid_fixture'
    model_id = 'unused-reviewer'
    def review(self, *args, **kwargs):
        raise AssertionError('fact-only response must not add review')


class JevTest(unittest.TestCase):
    def test_active_mode_requires_and_accepts_official_typesafe_jev_client(self):
        with self.assertRaisesRegex(ValueError, 'jev_provider_required'):
            JevConversationAgent(jev_mode='active')

        agent = JevConversationAgent(jev_mode='active', judge=JevClient())
        self.assertEqual(agent.judge.url, 'https://api.typesafe.ai/v1/systemone')
        self.assertEqual(agent.judge.model_id, 'jev-1.13.0')

    def test_transport_protocol_and_secret_not_in_receipt(self):
        captured = []
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def opener(request, **kwargs):
            captured.append(request)
            return Response(json.dumps({'model': 'jev-1.13.0', 'usage': {'input_tokens': 5, 'output_tokens': 2},
                'answers': {'x': {'type': 'choice', 'choice': 'a', 'confidence': 1,
                                  'probabilities': {'a': 1, 'b': 0}}}}).encode())
        answers, receipt = JevClient(urlopen=opener, key_loader=lambda: 'test-secret').evaluate(
            'data', {'x': {'type': 'choice', 'instructions': 'Choose', 'criteria': {'a': 'A', 'b': 'B'}}})
        self.assertEqual(answers['x']['choice'], 'a')
        self.assertNotIn('test-secret', json.dumps(receipt))
        self.assertNotIn('test-secret', captured[0].data.decode())
        self.assertEqual(captured[0].full_url, 'https://api.typesafe.ai/v1/systemone')

    def test_invalid_probability_is_not_applied(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        payload = {'model': 'jev-1.13.0', 'usage': {'input_tokens': 5, 'output_tokens': 2},
                   'answers': {'x': {'type': 'choice', 'choice': 'a', 'confidence': 1,
                                     'probabilities': {'a': 2, 'b': -1}}}}
        with self.assertRaises(JevError):
            JevClient(urlopen=lambda *a, **k: Response(json.dumps(payload).encode()),
                      key_loader=lambda: 'test').evaluate('x', {'x': {'type': 'choice',
                         'instructions': 'Choose', 'criteria': {'a': 'A', 'b': 'B'}}})

    def test_malformed_top_level_retains_unknown_paid_receipt(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        for payload in [[], None, 'unexpected']:
            with self.subTest(payload=payload), self.assertRaises(JevError) as raised:
                JevClient(urlopen=lambda *a, **k: Response(json.dumps(payload).encode()),
                    key_loader=lambda: 'test').evaluate('x', {'x': {'type': 'choice', 'criteria': {'a': 'A'}}})
            self.assertTrue(raised.exception.receipt['request_sent'])
            self.assertIsNone(raised.exception.receipt['usage'])

    def test_invalid_cost_counts_do_not_create_negative_cost(self):
        from scripts.run_jev_ab import cost
        policy = {'cost': {'deepseek_cny_per_million': {'cached_input': [.02, .04],
            'uncached_input': [1, 2], 'output': [4, 8]}, 'comparison_fx_cny_per_usd': 7.2}}
        bad_cache = cost([{'stage': 'primary', 'usage': {'prompt_tokens': 10,
            'completion_tokens': 1, 'prompt_cache_hit_tokens': 10000}}], policy)
        self.assertTrue(bad_cache['known'])
        self.assertGreaterEqual(bad_cache['comparison_total_cny_range'][0], 0)
        bad_usage = cost([{'stage': 'primary', 'usage': {'prompt_tokens': -10,
            'completion_tokens': 1}}], policy)
        self.assertFalse(bad_usage['known'])
        self.assertIsNone(bad_usage['comparison_total_cny_range'])

    def test_frozen_public_fixture_adapts_catalog_without_fetch(self):
        from scripts.run_jev_ab import FrozenEvidenceRuntime
        class LegacyFixture:
            def status(self): return {'ready': False, 'public_release_id': None}
            def public_catalog(self): return {'terms': ['fixture'], 'identifiers': ['id']}
        runtime = FrozenEvidenceRuntime(LegacyFixture())
        prepared = runtime.prepare_round('合成问题')
        self.assertEqual(prepared.provider.public_catalog(limit=1)['terms'], ['fixture'])
        self.assertFalse(prepared.report['cold_fetch_enabled'])

    def test_estimator_and_sender_use_identical_augmented_wire_once(self):
        primary, judge, debit = Primary(), Judge(), []
        client = SkillPrimedClient(primary, judge, mode='active', debit=debit.append)
        messages = [{'role': 'system', 'content': 'trusted'}, {'role': 'user',
                    'content': json.dumps({'current_request': '已匿名化问题', 'server_context': {'private': 'excluded'}})}]
        self.assertEqual(client.request_input_bound(messages, []), primary.request_input_bound(messages, []))
        self.assertEqual(judge.states, [])
        client.prepare_turn(messages, deadline=time.monotonic()+10)
        bound = client.request_input_bound(messages, [])
        client.complete(messages, [], request_id='x', remaining_seconds=10)
        client.request_input_bound(messages, [])
        self.assertEqual(primary.bound_calls, [bound])
        self.assertEqual(len(judge.states), 1)
        self.assertEqual(judge.states, [{'current_request': '已匿名化问题'}])
        self.assertEqual(debit, [11])
        self.assertEqual(messages[0]['content'], 'trusted')

    def test_unknown_paid_usage_stops_main_model(self):
        primary = Primary()
        judge = Judge(error=JevError('timeout', {'request_sent': True, 'usage': None}))
        with tempfile.TemporaryDirectory() as root:
            g = FinanceGateway(Path(root) / 'bank.sqlite3')
            w, _ = g.create_session()
            result = JevConversationAgent(primary=primary, reviewer=NeverReviewer(),
                judge=judge, jev_mode='active').process(g, w, '查询余额')
            with g.bank.connect() as db:
                audit = json.loads(db.execute("SELECT details FROM audit WHERE event='agent_jev_turn_completed'").fetchone()[0])
            self.assertFalse(audit['usage_complete'])
            self.assertIsNone(audit['total_tokens'])
        self.assertEqual(len(primary.messages), 0)
        self.assertFalse(result['agent_runtime']['usage_complete'])
        self.assertEqual(result['agent_runtime']['failure'], 'MODEL_USAGE_UNVERIFIED')

    def test_finalizer_receives_total_usage_once(self):
        with tempfile.TemporaryDirectory() as root:
            g = FinanceGateway(Path(root) / 'bank.sqlite3')
            w, _ = g.create_session()
            published = []
            def finalize(result, commit_history):
                published.append(result['agent_runtime']['total_tokens'])
                commit_history()
                return result
            result = JevConversationAgent(primary=Primary(), reviewer=NeverReviewer(),
                judge=Judge(), jev_mode='active').process(g,w,'查询余额',finalize_result=finalize)
            with g.bank.connect() as db:
                rows = db.execute("SELECT details FROM audit WHERE event='agent_jev_turn_completed'").fetchall()
            self.assertEqual(published, [31])
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0][0])['total_tokens'], 31)
            self.assertEqual(result['agent_runtime']['known_tokens_lower_bound'], 31)

    def test_low_confidence_preserves_original_context(self):
        primary, judge = Primary(), Judge(confidence=0.2)
        client = SkillPrimedClient(primary, judge, mode='active', debit=lambda x: None)
        messages = [{'role': 'system', 'content': 'trusted'}, {'role': 'user',
                    'content': json.dumps({'current_request': '问题'})}]
        client.prepare_turn(messages, deadline=time.monotonic()+10)
        self.assertEqual(client.serialized_request(messages, []), primary.serialized_request(messages, []))
        self.assertFalse(client.selection['applied'])

    def test_fact_path_privacy_budget_and_off_switch(self):
        for mode in ['off', 'shadow', 'active']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                primary, judge = Primary(), Judge()
                g = FinanceGateway(Path(root) / 'bank.sqlite3')
                w, _ = g.create_session()
                before = g.overview(w)
                result = JevConversationAgent(primary=primary, reviewer=NeverReviewer(),
                    judge=judge, jev_mode=mode).process(g, w, '查询余额')
                after = g.overview(w)
                self.assertEqual(before['total_cash_minor'], after['total_cash_minor'])
                self.assertEqual(result['answer_contract']['kind'], 'verified_facts')
                self.assertEqual(result['agent_runtime']['review_calls'], 0)
                self.assertEqual(result['agent_runtime']['total_tokens'], 20 if mode == 'off' else 31)
                self.assertEqual(len(judge.states), 0 if mode == 'off' else 1)
                self.assertEqual(len(primary.messages), 2)

if __name__ == '__main__': unittest.main()
