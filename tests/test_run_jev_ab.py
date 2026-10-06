"""Zero-paid accounting checks for the Jev experiment collector."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from agent_runtime.model_adapters import AIPING_URL
from agent_runtime.native_client import AIPingToolClient
from scripts.run_jev_ab import Recorder, cost, enforce_aiping_only


class RecorderInterruptionTest(unittest.TestCase):
    def test_aiping_only_guard_rejects_official_route_and_unavailable_jev(self):
        with self.assertRaisesRegex(ValueError, 'experiment_requires_aiping_primary_and_reviewer'):
            enforce_aiping_only({'models': {'primary_provider': 'deepseek',
                'reviewer_provider': 'ai_ping'}, 'plan': []})
        protocol = {'models': {'primary_provider': 'ai_ping', 'reviewer_provider': 'ai_ping',
            'judge_available_via_aiping': False}, 'plan': [{'variant': 'jev'}]}
        with self.assertRaisesRegex(ValueError, 'jev_model_unavailable_via_aiping'):
            enforce_aiping_only(protocol)

    def test_aiping_price_is_used_for_primary_tokens(self):
        report = cost([{'stage': 'primary', 'usage': {'prompt_tokens': 10,
            'completion_tokens': 3}}], {'models': {'judge_provider': 'unavailable_via_aiping'},
            'cost': {'aiping_primary_cny_per_million': {'input': [1, 1], 'output': [2, 2]},
                     'glm_cny_per_million': {'input': [0.8, 0.8], 'output': [1.4, 2.8]}}})
        self.assertTrue(report['known'])
        self.assertEqual(report['main_cny_range'], [0.000016, 0.000016])

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
