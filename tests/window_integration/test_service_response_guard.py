"""真实JSON/SSE/WS服务条件末端守卫；只用合成临时库和脚本provider。"""
from __future__ import annotations

import copy
from contextlib import closing, contextmanager
import hashlib
import http.client
import inspect
import json
from pathlib import Path
import sys
import time
import unicodedata
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_cross_module as _cross
import test_market_voice_negotiation as _voice
import test_negotiation_consent as _consent
import test_final_response_guards as _final

from agent_runtime.conversation import ConversationAgent
from local_knowledge.runtime import EvidenceRuntime
from service_negotiation import ServiceNegotiationService
from finance_gateway import COOKIE
from voice_gateway import SpeechTask

ROOT = _cross.ROOT
ORACLE_PATH = ROOT / "evaluation/window_integration/service_response_guard_oracles.json"
FROZEN_ORACLE_SHA = "c0de2b0ba224fed1b58c6a21fab89457c749d9addee2c8f65339b4706b86b375"
ORACLE = json.loads(ORACLE_PATH.read_text())
FIXTURE, EXPECT = ORACLE["fixture"], ORACLE["expectations"]
CASE_IDS = {case["test"]: case["id"] for case in ORACLE["cases"]}


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def source_manifest():
    paths = {path for path in ROOT.glob("*.py") if path.is_file()}
    for folder in ("agent_runtime", "base_runtime", "simulated_bank", "financial_methods", "local_knowledge"):
        paths.update((ROOT / folder).rglob("*.py"))
    paths.update(HERE / name for name in (
        "test_cross_module.py", "test_market_voice_negotiation.py", "test_negotiation_consent.py",
        "test_final_response_guards.py", "test_service_response_guard.py"))
    paths.add(ORACLE_PATH)
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


class ServiceResponseGuardContracts(unittest.TestCase):
    raw_state = _cross.CrossModuleIntegrationTests.raw_state
    operation_count = _cross.CrossModuleIntegrationTests.operation_count
    public_fixture = _cross.CrossModuleIntegrationTests.public_fixture
    http_json = _voice.MarketVoiceNegotiationIntegrationTests.http_json
    connect_ticket = _voice.MarketVoiceNegotiationIntegrationTests.connect_ticket
    counts = _consent.NegotiationConsentIntegrationTests.counts

    @classmethod
    def setUpClass(cls):
        if hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest() != FROZEN_ORACLE_SHA:
            raise RuntimeError("frozen_service_response_oracle_changed")
        if Path(sys.executable).absolute().parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required")
        cls.before_sources, cls.observations = source_manifest(), []

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        print("WINDOW_SERVICE_GUARD_SUMMARY " + json.dumps({
            "oracle_sha256": FROZEN_ORACLE_SHA,
            "source_sha256_before": cls.before_sources, "source_sha256_after": after,
            "source_unchanged": cls.before_sources == after, "cloud_calls": 0,
            "scope": ORACLE["scope"], "observations": cls.observations,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        if after != cls.before_sources:
            raise RuntimeError("service_guard_inputs_changed_during_test")

    def setUp(self):
        _cross.CrossModuleIntegrationTests.setUp(self)
        self.require(self.g, "approve_speech", "speech_guard", "response_guard")
        self.require(self.g.service_negotiations, "rules", "preview", "publish_rules", "evidence_guard")
        # 默认规则只读复制，所有publish.lock也必须在本题临时目录。
        rules = json.loads(Path(self.g.service_negotiations.rules_path).read_text())
        self.rules_path = self.directory / "service-response-rules.json"
        self.rules_path.write_text(json.dumps(rules, ensure_ascii=False))
        self.g.service_negotiations = ServiceNegotiationService(self.g, self.rules_path)
        self.original_rules = copy.deepcopy(rules)
        self.public_fixture(text=FIXTURE["public_text"], suffix="service-response")
        self.clock_now = time.time()
        self.clock_patch = patch.object(self.g.bank, "clock", side_effect=lambda: self.clock_now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.approvals, self.windows = [], []
        approve = self.g.approve_speech

        def witnessed_approval(workspace, result, session_token=None):
            receipt = approve(workspace, result, session_token)
            self.approvals.append({"service_snapshot": copy.deepcopy(result.get("service_snapshot")),
                "review_status": result.get("agent_review", {}).get("status"),
                "reply": result.get("reply"), "receipt_issued": receipt is not None})
            return receipt

        probe = patch.object(self.g, "approve_speech", side_effect=witnessed_approval)
        probe.start()
        self.addCleanup(probe.stop)

    def require(self, owner, *names):
        missing = [name for name in names if not callable(getattr(owner, name, None))]
        if missing:
            self.skipTest("BLOCKED:真实服务接口缺少 " + type(owner).__name__ + "." + ",".join(missing))

    def evidence(self, **values):
        row = {"case": CASE_IDS[self._testMethodName], "test": self._testMethodName,
               "oracle_sha256": FROZEN_ORACLE_SHA, "cloud_calls": 0,
               "scope": ORACLE["scope"], **values}
        self.observations.append(row)
        print("WINDOW_SERVICE_GUARD_EVIDENCE " + json.dumps(
            row, ensure_ascii=False, sort_keys=True, default=str), flush=True)

    @contextmanager
    def actual_server(self):
        # 当前ProxyHandler要求的真实准备服务；只使用本题公共库，没有冷补充请求。
        with _voice.MarketVoiceNegotiationIntegrationTests.local_server(self) as server:
            def forbidden_reader(*args, **kwargs):
                raise AssertionError("public_network_reader_forbidden")
            runtime = EvidenceRuntime(self.g.knowledge_provider, background=False, reader=forbidden_reader)
            server.evidence_runtime = runtime
            try:
                yield server
            finally:
                runtime.close()

    def local_agent(self, server):
        def answer(messages, tools):
            outputs = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            ids = [output["evidence_id"] for output in outputs if "evidence_id" in output]
            return _cross.call("answer_reviewed", {"text": FIXTURE["approved_reply"],
                "claims": [{"text": "服务条件仅是预览，须本人另行接受；公共条款说明本金风险。",
                            "evidence_ids": ids}], "draft_ids": [], "followups": []})
        self.primary = _cross.ScriptedPrimary([
            _cross.call("service_offer_preview", {}),
            _cross.call("knowledge_search", {"query": "风险", "product_id": FIXTURE["public_product"],
                "as_of": FIXTURE["public_as_of"]}), answer])
        self.reviewer = _cross.ScriptedReviewer()
        server.conversation_agent = ConversationAgent(self.primary, self.reviewer)

    def request_agent(self, transport="json"):
        if transport == "json":
            status, result = self.http_json("POST", "/api/agent", {
                "text": FIXTURE["request"], "mode": "classic"})
            return status, result, json.dumps(result, ensure_ascii=False), []
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=6)
        try:
            connection.request("POST", "/api/agent", body=json.dumps({
                "text": FIXTURE["request"], "mode": "classic"}), headers={
                "Content-Type": "application/json", "Accept": "text/event-stream",
                "Cookie": COOKIE + "=" + self.token, "X-QZ-CSRF": self.g.csrf(self.token),
                "Origin": self.base})
            response = connection.getresponse()
            raw = response.read().decode()
            events = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
            finals = [event["value"] for event in events if event.get("name") == "navren.result"]
            self.assertEqual(len(finals), 1, "实际SSE必须仅有一个最终结果")
            self.assertTrue(any(event.get("type") == "RUN_FINISHED" for event in events))
            return response.status, finals[0], raw, events
        finally:
            connection.close()

    def result_view(self, result):
        return {"reply": result.get("reply"), "isFallback": result.get("isFallback"),
                "error": result.get("error"), "bank_actions": result.get("bank_actions", []),
                "has_speech_receipt": bool(result.get("speech_receipt")),
                "service_snapshot": result.get("service_snapshot"),
                "agent_review": result.get("agent_review"),
                "metric_batches": len(result.get("financial_metric_batches", []))}

    def no_side_effects(self, before):
        count = self.counts(self.w)
        self.assertEqual(count["applications"], EXPECT["applications"])
        self.assertEqual(count["submission_audits"], EXPECT["submission_audits"])
        self.assertEqual(count["financial_operations"], EXPECT["financial_operations"])
        self.assertEqual(self.raw_state(), before)

    def allow(self, status, result, transport, before):
        self.assertEqual(status, EXPECT["normal_" + transport + "_http"], result)
        self.assertFalse(result.get("isFallback"), result)
        self.assertEqual(result["reply"], FIXTURE["approved_reply"])
        self.assertEqual(result["agent_review"]["status"], "approved")
        self.assertIsInstance(result.get("service_snapshot"), dict)
        self.assertIsInstance(result.get("knowledge_snapshot"), dict)
        self.assertIsInstance(result.get("speech_receipt"), str)
        # 语音出站对全角标点作Unicode规范化；独立标准库复算，不放宽正文内容。
        self.assertEqual(self.g.resolve_speech(self.w, result["speech_receipt"], self.token),
                         unicodedata.normalize("NFKC", result["reply"]))
        self.assertEqual(len(self.reviewer.packets), 1)
        self.no_side_effects(before)

    def publish_changed_rules(self, kind):
        service = self.g.service_negotiations
        current = service.rules()
        before = service._rules_digest(current)
        if kind == "terms":
            row = next(item for item in current["offers"] if item["offer_id"] == FIXTURE["electronic_offer_id"])
            row["monthly_value_minor"] += 100
            row["max_cost_minor"] = row["monthly_value_minor"] * row["duration_months"]
            row["conditions"] = ["修订后的隔离合成电子账单条件，须重新确认"]
        else:
            row = next(item for item in current["offers"] if item["offer_id"] == FIXTURE["salary_offer_id"])
            row["minimum_monthly_salary_minor"] += 300000
            row["salary_months_required"] += 2
            row["conditions"] = ["修订后的工资资格须重新核验，旧说明不再适用"]
        published = service.publish_rules(current, expected_sha256=before)
        after = service._rules_digest(service.rules())
        self.assertNotEqual(before, after, "必须实际变更规则而非空注入")
        self.assertEqual(published["rules_sha256"], after)
        return {"kind": kind, "rules_sha256_before": before, "rules_sha256_after": after,
                "same_schema_version": current["schema_version"] == self.original_rules["schema_version"],
                "changed_offer": row["offer_id"]}

    @contextmanager
    def final_public_db_fault(self, change):
        provider = self.g.knowledge_provider
        original_db = provider.db
        fired = False

        @contextmanager
        def database():
            nonlocal fired
            # 包装真实提供器连接，不替换任何待测发布/服务/公共guard。
            with original_db() as db:
                in_public_guard = any(frame.function == "public_guard" and
                    Path(frame.filename).resolve() == ROOT / "base_runtime/evidence.py"
                    for frame in inspect.stack(context=0))
                if in_public_guard and not fired:
                    fired = True
                    self.assertEqual(len(self.reviewer.packets), 1, "变化须在本地审核后")
                    self.assertTrue(self.approvals and self.approvals[-1]["receipt_issued"],
                                    "必须进入实际最终发布窗口，不能将前期回退算守卫通过")
                    self.windows.append({"window": "actual_provider_db_in_public_guard", **change()})
                yield db
        with patch.object(provider, "db", new=database):
            yield
        self.assertTrue(fired, "BLOCKED:真实最终公共锁入口未到达，不得伪通过")

    def withheld(self, status, result, raw, transport, before):
        self.assertEqual(status, EXPECT["changed_json_http"] if transport == "json" else EXPECT["expired_sse_http"])
        self.assertTrue(result.get("isFallback"), result)
        self.assertIn(result.get("error", {}).get("code"), EXPECT["safe_service_errors"], result)
        self.assertNotIn(FIXTURE["approved_reply"], raw)
        self.assertNotIn("speech_receipt", result)
        self.assertEqual(result.get("bank_actions", []), [])
        self.no_side_effects(before)

    def test_same_rules_allow_actual_json_and_sse_local_control(self):
        before = self.raw_state()
        with self.actual_server() as server:
            for transport in ("json", "sse"):
                with self.subTest(transport=transport):
                    self.local_agent(server)
                    status, result, raw, events = self.request_agent(transport)
                    self.evidence(transport=transport, status=status, result=self.result_view(result),
                        review_calls=len(self.reviewer.packets), approvals=self.approvals,
                        state=self.counts(self.w), financial_state_unchanged=self.raw_state() == before,
                        event_types=[event.get("type") for event in events])
                    self.allow(status, result, transport, before)

    def test_late_rule_content_or_eligibility_change_withholds_json(self):
        before = self.raw_state()
        with self.actual_server() as server:
            for fault in ("terms", "eligibility"):
                with self.subTest(fault=fault):
                    self.local_agent(server)
                    window_before = len(self.windows)
                    with self.final_public_db_fault(lambda: self.publish_changed_rules(fault)):
                        status, result, raw, events = self.request_agent("json")
                    self.evidence(fault=fault, transport="actual_json", status=status,
                        result=self.result_view(result), late_windows=self.windows[window_before:],
                        approvals=self.approvals[-1:], review_calls=len(self.reviewer.packets),
                        state=self.counts(self.w), financial_state_unchanged=self.raw_state() == before)
                    self.withheld(status, result, raw, "json", before)

    def test_preview_expires_in_final_public_lock_window_withholds_sse(self):
        before = self.raw_state()
        def expire():
            snapshot = self.approvals[-1]["service_snapshot"]
            self.assertIsNotNone(snapshot["preview"])
            with closing(self.g.bank.connect()) as db:
                row = db.execute("SELECT expires_at,created_at FROM service_negotiation_drafts WHERE id=? AND workspace_id=?",
                                 (snapshot["preview"]["draft_id"], self.w)).fetchone()
            prior = self.clock_now
            self.assertEqual(row["expires_at"] - row["created_at"], FIXTURE["preview_seconds"])
            self.assertLess(prior, row["expires_at"])
            self.clock_now = row["expires_at"] + FIXTURE["expired_extra_seconds"]
            return {"clock_before": prior, "clock_after": self.clock_now,
                    "expires_at": row["expires_at"], "preview_id": snapshot["preview"]["draft_id"]}
        with self.actual_server() as server:
            self.local_agent(server)
            with self.final_public_db_fault(expire):
                status, result, raw, events = self.request_agent("sse")
        self.evidence(transport="actual_sse", status=status, result=self.result_view(result),
            late_windows=self.windows, approvals=self.approvals,
            event_types=[event.get("type") for event in events],
            state=self.counts(self.w), financial_state_unchanged=self.raw_state() == before)
        self.withheld(status, result, raw, "sse", before)

    def test_late_tts_pcm_after_service_rule_change_is_not_published(self):
        before = self.raw_state()
        provider = _final.BlockedAudioProvider()
        def factory(kind, options, key, url):
            self.assertEqual(kind, "tts")
            self.providers.append(provider)
            return SpeechTask(kind, options, key, url, connector=lambda *args, **kwargs: provider)
        frames, binary_bytes, slots = [], 0, 0
        with self.actual_server() as server:
            self.local_agent(server)
            status, result, raw, events = self.request_agent("json")
            self.allow(status, result, "json", before)
            server.voice.task_factory = factory
            ticket_status, ticket = self.http_json("POST", "/api/speech/session", {
                "kind": "tts", "speech_receipt": result["speech_receipt"]})
            self.assertEqual(ticket_status, 200, ticket)
            ws = self.connect_ticket(ticket["path"])
            try:
                ws.send(json.dumps({"type": "start", "voice": "female_sweet"}))
                self.assertEqual(json.loads(ws.recv())["type"], "ready")
                self.assertTrue(provider.blocked.wait(timeout=2), "必须实际阻塞在上游PCM之前")
                change = self.publish_changed_rules("terms")
                provider.release_audio.set()
                for _ in range(6):
                    frame = ws.recv()
                    if frame == "":
                        break
                    if isinstance(frame, bytes):
                        binary_bytes += len(frame)
                    else:
                        frames.append(json.loads(frame))
                self.assertEqual(frame, "", "拒绝后WS须有界关闭")
                self.assertTrue(provider.returned_audio.wait(timeout=2), "旧PCM必须实际返回，不能以没有上游输出算通过")
                self.assertTrue(provider.closed.wait(timeout=2))
                for _ in range(EXPECT["voice_slots"]):
                    self.assertTrue(server.voice.slots.acquire(timeout=2))
                    slots += 1
                self.assertFalse(server.voice.slots.acquire(blocking=False))
            finally:
                provider.release_audio.set()
                ws.close()
                for _ in range(slots):
                    server.voice.slots.release()
        self.evidence(transport="actual_ws_receipt_from_actual_api_agent", upstream_pcm_returned=provider.returned_audio.is_set(),
            binary_bytes=binary_bytes, frames=frames, rules_change=change,
            upstream_closed=provider.closed.is_set(), slots_restored=slots,
            state=self.counts(self.w), financial_state_unchanged=self.raw_state() == before)
        self.assertEqual(binary_bytes, EXPECT["old_audio_bytes"])
        self.assertTrue(frames, "必须明确拒绝，不能以空音频/超时算通过")
        self.assertTrue(all(frame.get("type") == "error" and frame.get("code") in EXPECT["safe_service_errors"]
                            for frame in frames), frames)
        self.no_side_effects(before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
