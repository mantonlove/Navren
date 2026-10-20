"""Zero-paid accounting checks for the Jev experiment collector."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from agent_runtime.model_adapters import AIPING_URL
from agent_runtime.native_client import AIPingToolClient
from scripts.run_jev_ab import (Recorder, cost, enforce_provider_routes, execution_observation,
    freeze, instantiate_trial_agent, paired_plan, selected_cases, shared_agent_options)


class FrozenProtocolV2Test(unittest.TestCase):
    def setUp(self):
        self.data = {'cases': [{'id': f'S{i:02}_original_case', 'prompt': f'原题{i}',
            'golden_integer': i * 100} for i in range(1, 11)]}

    def test_subset_freeze_preserves_original_dataset_and_two_reversed_pairs(self):
        with tempfile.TemporaryDirectory(prefix='jev-ab-freeze-test-') as tmp:
            root = Path(tmp) / 'project'
            dataset = root / 'evaluation/window_integration/semantic_cases.json'
            dataset.parent.mkdir(parents=True)
            dataset.write_text(json.dumps(self.data, ensure_ascii=False))
            original_bytes = dataset.read_bytes()
            out = Path(tmp) / 'new-experiment'
            with patch('scripts.run_jev_ab.ROOT', root), patch('builtins.print'):
                freeze(out, repetitions=2, case_ids=['S01,S04', 'S06', 'S08'])
            protocol = json.loads((out / 'protocol.json').read_text())
            self.assertEqual(protocol['version'], 2)
            self.assertEqual(protocol['case_ids'], [f'S{i:02}_original_case' for i in (1,4,6,8)])
            self.assertEqual(protocol['original_case_count'], 10)
            self.assertEqual(protocol['repetitions'], 2)
            self.assertEqual(len(protocol['plan']), 16)
            self.assertEqual((out / 'source/evaluation/window_integration/semantic_cases.json').read_bytes(), original_bytes)
            self.assertEqual(dataset.read_bytes(), original_bytes)
            for case_id in protocol['case_ids']:
                first = [p['variant'] for p in protocol['plan'] if p['case_id'] == case_id and p['repetition'] == 1]
                second = [p['variant'] for p in protocol['plan'] if p['case_id'] == case_id and p['repetition'] == 2]
                self.assertEqual(set(first), {'baseline', 'jev'})
                self.assertEqual(second, list(reversed(first)))
            self.assertEqual(protocol['architecture']['tool_routing'], 'progressive')
            self.assertTrue(all(profile['tool_routing'] == 'progressive' for profile in protocol['profiles'].values()))
            enforce_provider_routes(protocol)

    def test_original_freeze_signature_keeps_all_cases_and_default_two_repetitions(self):
        with tempfile.TemporaryDirectory(prefix='jev-ab-default-freeze-') as tmp:
            root = Path(tmp) / 'project'
            dataset = root / 'evaluation/window_integration/semantic_cases.json'
            dataset.parent.mkdir(parents=True)
            dataset.write_text(json.dumps(self.data))
            out = Path(tmp) / 'experiment'
            with patch('scripts.run_jev_ab.ROOT', root), patch('builtins.print'):
                freeze(out)
            protocol = json.loads((out / 'protocol.json').read_text())
            self.assertEqual(len(protocol['plan']), 40)
            self.assertEqual(len(protocol['case_ids']), 10)

    def test_invalid_subset_and_repetitions_are_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory(prefix='jev-ab-invalid-freeze-') as tmp:
            root = Path(tmp) / 'project'
            dataset = root / 'evaluation/window_integration/semantic_cases.json'
            dataset.parent.mkdir(parents=True)
            dataset.write_text(json.dumps(self.data))
            for index, (kwargs, error) in enumerate([
                ({'case_ids': ['S99']}, 'unknown_case_id'),
                ({'case_ids': ['S01', 'S01_original_case']}, 'duplicate_case_id'),
                ({'case_ids': []}, 'case_subset_empty'),
                ({'repetitions': 0}, 'repetitions_must_be_positive_integer'),
                ({'repetitions': True}, 'repetitions_must_be_positive_integer'),
            ]):
                out = Path(tmp) / f'invalid-{index}'
                with patch('scripts.run_jev_ab.ROOT', root), self.assertRaisesRegex(ValueError, error):
                    freeze(out, **kwargs)
                self.assertFalse(out.exists())

    def test_plan_is_deterministic_and_reverses_each_adjacent_repetition(self):
        cases = selected_cases(self.data, 'S01,S04,S06,S08')
        first = paired_plan(cases, repetitions=4)
        self.assertEqual(first, paired_plan(cases, repetitions=4))
        self.assertEqual(len(first), 32)
        for case in cases:
            orders = [[p['variant'] for p in first if p['case_id'] == case['id'] and p['repetition'] == rep] for rep in range(1,5)]
            self.assertEqual(orders[1], list(reversed(orders[0])))
            self.assertEqual(orders[2], orders[0])
            self.assertEqual(orders[3], orders[1])

    def test_both_trial_constructors_receive_identical_main_agent_controls(self):
        protocol = {'version': 2, 'limits': {'seconds_per_trial': 105,
            'tokens_per_trial_including_jev': 100000, 'rounds': 8, 'tools': 12},
            'architecture': {'tool_routing': 'progressive'},
            'profiles': {v: {'tool_routing': 'progressive'} for v in ('baseline','jev')}}
        primary, reviewer, judge = object(), object(), object()
        baseline, jev = Mock(), Mock()
        instantiate_trial_agent(primary, reviewer, protocol, 'baseline', judge=None,
            baseline_class=baseline, jev_class=jev)
        instantiate_trial_agent(primary, reviewer, protocol, 'jev', judge=judge,
            baseline_class=baseline, jev_class=jev)
        b, j = baseline.call_args.kwargs, dict(jev.call_args.kwargs)
        self.assertIs(j.pop('judge'), judge)
        self.assertEqual(j.pop('jev_mode'), 'active')
        self.assertEqual(b, j)
        self.assertEqual(b['tool_routing'], 'progressive')
        self.assertEqual(b['max_tokens'], 100000)
        mismatched = {**protocol, 'profiles': {'baseline': {'tool_routing': 'full'},
            'jev': {'tool_routing': 'progressive'}}}
        with self.assertRaisesRegex(ValueError, 'v2_requires_identical_progressive_tool_routing'):
            shared_agent_options(mismatched)

    def test_historical_v1_constructor_omits_new_argument(self):
        self.assertNotIn('tool_routing', shared_agent_options({'version':1, 'limits':{
            'seconds_per_trial':105, 'tokens_per_trial_including_jev':100000, 'rounds':8, 'tools':12}}))

    def test_observation_preserves_actual_tool_view_and_unknown_usage(self):
        row = {'technical_completed':False, 'provider_calls':[
            {'stage':'jev','started_at':'2026-10-07T08:00:00+00:00','duration_ms':450,
             'usage':{'input_tokens':100,'output_tokens':2}},
            {'stage':'primary','started_at':'2026-10-07T08:00:00.500000+00:00',
             'duration_ms':35000,'usage':None}], 'result':{'agent_runtime':{
            'budget_admissions':[{'stage':'explore','tool_view':{'actual_view':'progressive_initial','actual_tool_count':3}}],
            'jev':{'preparation':{'phase':'before_tool_view_and_budget_admission'},'selection':{'applied':True}},
            'failure':'MODEL_USAGE_UNVERIFIED'}}}
        view = {'tool_count':3,'tool_names':['load_skill','answer_reviewed','ask_user']}
        observation = execution_observation(row, [view])
        self.assertEqual(observation['first_primary_admission_stage'], 'explore')
        self.assertEqual(observation['first_primary_tool_view']['actual_tool_count'], 3)
        self.assertEqual(observation['first_primary_requested_tools'], view)
        self.assertTrue(observation['jev_preparation']['provider_called_before_first_primary'])
        self.assertEqual(observation['stages']['primary']['requests_sent'], 1)
        self.assertEqual(observation['stages']['primary']['usage_unknown_calls'], 1)
        self.assertEqual(observation['stages']['reviewer']['requests_sent'], 0)
        self.assertFalse(observation['technical_completed'])


class RecorderInterruptionTest(unittest.TestCase):
    def test_route_guard_requires_aiping_for_llms_and_official_typesafe_for_jev(self):
        with self.assertRaisesRegex(ValueError, 'experiment_requires_aiping_primary_and_reviewer'):
            enforce_provider_routes({'models': {'primary_provider': 'deepseek',
                'reviewer_provider': 'ai_ping'}, 'plan': []})
        with self.assertRaisesRegex(ValueError, 'experiment_requires_aiping_primary_and_reviewer'):
            enforce_provider_routes({'models': {'primary_provider': 'ai_ping',
                'reviewer_provider': 'glm'}, 'plan': []})
        wrong_provider = {'models': {'primary_provider': 'ai_ping', 'reviewer_provider': 'ai_ping',
            'judge_provider': 'ai_ping', 'judge_endpoint': 'https://aiping.cn/api/v1/chat/completions'},
            'plan': [{'variant': 'jev'}]}
        with self.assertRaisesRegex(ValueError, 'experiment_requires_official_typesafe_jev'):
            enforce_provider_routes(wrong_provider)
        protocol = {'models': {'primary_provider': 'ai_ping', 'reviewer_provider': 'ai_ping',
            'judge_provider': 'typesafe_official',
            'judge_endpoint': 'https://api.typesafe.ai/v1/systemone'},
            'plan': [{'variant': 'jev'}]}
        enforce_provider_routes(protocol)

    def test_aiping_price_is_used_for_primary_tokens(self):
        report = cost([{'stage': 'primary', 'usage': {'prompt_tokens': 10,
            'completion_tokens': 3}}], {'models': {'judge_provider': 'unavailable_via_aiping'},
            'cost': {'aiping_primary_cny_per_million': {'input': [1, 1], 'output': [2, 2]},
                     'glm_cny_per_million': {'input': [0.8, 0.8], 'output': [1.4, 2.8]}}})
        self.assertTrue(report['known'])
        self.assertEqual(report['main_cny_range'], [0.000016, 0.000016])

    def test_official_jev_usage_is_included_in_comparison_cost(self):
        report = cost([{'stage': 'jev', 'usage': {'input_tokens': 1000,
            'output_tokens': 20}}], {'models': {'judge_provider': 'typesafe_official'},
            'cost': {'jev_usd_per_million': {'input': 0.042, 'output': 0},
                     'comparison_fx_cny_per_usd': 7.2}})
        self.assertTrue(report['known'])
        self.assertEqual(report['tokens'], 1020)
        self.assertAlmostEqual(report['comparison_total_cny_range'][0], 0.0003024)

    def test_dispatch_is_persisted_before_wait_and_usage_remains_unknown(self):
        with tempfile.TemporaryDirectory(prefix="jev-ab-recorder-") as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            request = Request(
                "https://provider.invalid/v1/chat/completions",
                data=json.dumps({"model": "fixture-model"}).encode("utf-8"),
            )

            with patch("urllib.request.urlopen", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    recorder.opener("primary")(request, timeout=1)

            ledger = json.loads((root / "provider-calls.json").read_text())
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0]["stage"], "primary")
            self.assertTrue(ledger[0]["request_sent"])
            self.assertEqual(ledger[0]["outcome"], "pending")
            self.assertIsNone(ledger[0]["usage"])
            self.assertTrue((root / "requests" / "000.json").exists())

            report = cost(ledger, {"cost": {
                "jev_usd_per_million": {"input": 0.042},
                "comparison_fx_cny_per_usd": 7.2,
                "deepseek_cny_per_million": {
                    "cached_input": [0.02, 0.04],
                    "uncached_input": [1, 2],
                    "output": [4, 8],
                },
                "glm_cny_per_million": {"input": [0.8, 0.8], "output": [1.4, 2.8]},
            }})
            self.assertFalse(report["known"])
            self.assertIsNone(report["comparison_total_cny_range"])

    def test_primary_tool_client_uses_aiping_endpoint_and_key_name(self):
        with tempfile.TemporaryDirectory(prefix="jev-ab-aiping-client-") as temporary:
            observed = {}

            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, limit):
                    return json.dumps({
                        "model": "DeepSeek-V4-Flash",
                        "choices": [{"finish_reason": "stop", "message": {
                            "role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 8, "completion_tokens": 2,
                                  "total_tokens": 10},
                    }).encode()

            def open_request(request, **kwargs):
                observed["url"] = request.full_url
                observed["authorization"] = request.get_header("Authorization")
                observed["payload"] = json.loads(request.data)
                return Response()

            client = AIPingToolClient(urlopen=open_request,
                key_loader=lambda: "AIPING_TEST_KEY", log_path=str(Path(temporary) / "calls.jsonl"))
            message, usage = client.complete([{"role": "user", "content": "test"}], [],
                request_id="test", remaining_seconds=10)

            self.assertEqual(observed["url"], AIPING_URL)
            self.assertEqual(observed["authorization"], "Bearer AIPING_TEST_KEY")
            self.assertEqual(observed["payload"]["model"], "DeepSeek-V4-Flash")
            self.assertEqual(message["content"], "ok")
            self.assertEqual(usage["prompt_tokens"], 8)
            self.assertIn('"provider": "ai_ping"', Path(temporary, "calls.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
