import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from agent_runtime.contracts import AdviceDraft, AdviceRequest, Claim, EvidenceItem
from agent_runtime.model_adapters import (
    AIPING_PRIMARY_MODEL,
    AIPingCashflowSupervisor,
    AIPingCashflowReviewer,
    DeepSeekCashflowSupervisor,
    ModelAdapterError,
    OpenAICompatibleJsonClient,
)


class Response:
    status = 200

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return json.dumps({"choices": [{"message": {"content": json.dumps(self.value)}}]}).encode()


def client_for(urlopen, log_path, key_loader=lambda: "test-key", retries=1):
    return OpenAICompatibleJsonClient(
        provider="test", model_id="test-model", url="https://example.invalid/v1/chat/completions",
        env_name="TEST_KEY", keychain_service="CC/TEST_KEY", timeout=0.01,
        max_retries=retries, urlopen=urlopen, key_loader=key_loader, log_path=str(log_path),
    )


class ModelAdapterTests(unittest.TestCase):
    def test_cashflow_live_supervisor_uses_aiping_key_and_route(self):
        supervisor = AIPingCashflowSupervisor()
        self.assertEqual(supervisor.client.provider, 'ai_ping')
        self.assertEqual(supervisor.client.model_id, AIPING_PRIMARY_MODEL)
        self.assertEqual(supervisor.client.url, 'https://aiping.cn/api/v1/chat/completions')
        self.assertEqual(supervisor.client.env_name, 'AIPING_API_KEY')

    def test_retries_transient_http_error_and_logs_metadata_without_prompt_or_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.jsonl"
            calls = []

            def urlopen(request, timeout):
                calls.append((request, timeout))
                if len(calls) == 1:
                    raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, io.BytesIO(b"private"))
                return Response({"ok": True})

            result = client_for(urlopen, path).complete_json(
                system="private system", user="private customer data", stage="reviewer.review", request_id="r1",
            )
            self.assertEqual(result, {"ok": True})
            self.assertEqual(len(calls), 2)
            record = json.loads(path.read_text().strip())
            self.assertEqual(record["outcome"], "completed")
            self.assertEqual(record["attempts"], 2)
            self.assertNotIn("private", path.read_text())
            self.assertNotIn("test-key", path.read_text())

    def test_missing_key_is_audited_without_http_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.jsonl"
            called = []
            with self.assertRaises(ModelAdapterError) as raised:
                client_for(lambda *args, **kwargs: called.append(True), path,
                           key_loader=lambda: "").complete_json(
                    system="s", user="u", stage="supervisor.route", request_id="r2",
                )
            self.assertEqual(raised.exception.code, "missing_api_key")
            self.assertEqual(called, [])
            record = json.loads(path.read_text().strip())
            self.assertEqual(record["error_code"], "missing_api_key")
            self.assertEqual(record["attempts"], 0)

    def test_invalid_provider_json_fails_closed_and_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.jsonl"

            class InvalidResponse(Response):
                def read(self, _limit):
                    return b"not json"

            with self.assertRaises(ModelAdapterError) as raised:
                client_for(lambda *args, **kwargs: InvalidResponse({}), path, retries=0).complete_json(
                    system="s", user="u", stage="supervisor.synthesize", request_id="r3",
                )
            self.assertEqual(raised.exception.code, "invalid_model_contract")
            record = json.loads(path.read_text().strip())
            self.assertEqual(record["outcome"], "failed")
            self.assertEqual(record["error_code"], "invalid_model_contract")

    def test_invalid_provider_envelope_is_classified_as_contract_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.jsonl"

            class InvalidEnvelope(Response):
                def read(self, _limit):
                    return json.dumps({"choices": ["unexpected"]}).encode()

            with self.assertRaises(ModelAdapterError) as raised:
                client_for(lambda *args, **kwargs: InvalidEnvelope({}), path, retries=0).complete_json(
                    system="s", user="u", stage="supervisor.route", request_id="r5",
                )
            self.assertEqual(raised.exception.code, "invalid_model_contract")
            record = json.loads(path.read_text().strip())
            self.assertEqual(record["error_code"], "invalid_model_contract")
            self.assertIsNone(record["exception_type"])

    def test_unexpected_provider_error_logs_only_exception_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.jsonl"

            def urlopen(*_args, **_kwargs):
                raise RuntimeError("private response detail")

            with self.assertRaises(ModelAdapterError) as raised:
                client_for(urlopen, path, retries=0).complete_json(
                    system="s", user="u", stage="supervisor.route", request_id="r6",
                )
            self.assertEqual(raised.exception.code, "provider_error")
            log = path.read_text()
            record = json.loads(log.strip())
            self.assertEqual(record["exception_type"], "RuntimeError")
            self.assertNotIn("private response detail", log)

    def test_glm_reviewer_uses_single_bounded_request(self):
        reviewer = AIPingCashflowReviewer()
        self.assertEqual(reviewer.client.timeout, 40)
        self.assertEqual(reviewer.client.max_retries, 0)

    def test_deepseek_requests_json_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.jsonl"
            seen = []
            client = OpenAICompatibleJsonClient(
                provider="deepseek", model_id="deepseek-flash",
                url="https://example.invalid/v1/chat/completions",
                env_name="TEST_KEY", keychain_service="CC/TEST_KEY",
                urlopen=lambda request, timeout: (seen.append(json.loads(request.data)) or Response({"ok": True})),
                key_loader=lambda: "test-key", log_path=str(path), max_retries=0,
            )
            client.complete_json(system="return json", user="synthetic", stage="test", request_id="r4")
            self.assertEqual(seen[0]["response_format"], {"type": "json_object"})
            self.assertEqual(seen[0]["thinking"], {"type": "disabled"})
            self.assertEqual(seen[0]["max_tokens"], 1800)

    def test_supervisor_supports_one_targeted_synthesis_revision(self):
        class StubClient:
            def complete_json(self, **kwargs):
                self.call = kwargs
                return {"text": "修订后的只读答复", "claims": [
                    {"text": "仅包含已有证据的结论。", "evidence_ids": ["e-1"]}],
                    "proposed_actions": []}

        client = StubClient()
        supervisor = DeepSeekCashflowSupervisor(client=client)
        request = AdviceRequest(
            request_id="revise-1", question="展示模拟现金流",
            constraints=("只读分析",),
            evidence=(EvidenceItem("e-1", "simulated_bank", "仅供模拟。"),),
        )
        revised = supervisor.revise_synthesis(
            request, (), AdviceDraft("旧答复", claims=(Claim("旧事实", ("e-1",)),)),
            ("明确证据范围",),
        )
        self.assertEqual(client.call["stage"], "supervisor.synthesis.revise")
        self.assertEqual(revised.text, "修订后的只读答复")
        self.assertIn("明确证据范围", client.call["user"])


if __name__ == "__main__":
    unittest.main()
