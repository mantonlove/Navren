"""冻结的传输/提交专项：真实loopback、真实guard/SQLite，只有合成阻塞点。"""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import http.client
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_cross_module as _cross
import test_market_voice_negotiation as _voice
import test_service_response_guard as _service

from agent_runtime.conversation import ConversationAgent
from agent_runtime.model_adapters import OpenAICompatibleJsonClient
from finance_gateway import COOKIE
from server import PublicationSocket
from simulated_bank import BankError
from voice_gateway import SpeechTask

ROOT = _cross.ROOT
_OFFLINE_WIRE_COMPLETE_JSON = OpenAICompatibleJsonClient.complete_json
ORACLE_PATH = ROOT / "evaluation/window_integration/service_transport_commit_oracles.json"
FROZEN_ORACLE_SHA = "a4d52f3d9cab6da7890fb548179524e14ccae232653f40e9974ce0f17d8da772"
ORACLE = json.loads(ORACLE_PATH.read_text())
FIXTURE, EXPECT = ORACLE["fixture"], ORACLE["expectations"]
CASE_IDS = {case["test"]: case["id"] for case in ORACLE["cases"]}
WAIT = FIXTURE["thread_timeout_seconds"]


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    paths = {path for path in ROOT.glob("*.py") if path.is_file()}
    for folder in ("agent_runtime", "base_runtime", "simulated_bank", "financial_methods", "local_knowledge"):
        paths.update((ROOT / folder).rglob("*.py"))
    paths.update(HERE / name for name in (
        "test_cross_module.py", "test_market_voice_negotiation.py", "test_negotiation_consent.py",
        "test_final_response_guards.py", "test_service_response_guard.py", "test_service_transport_commit.py"))
    paths.update((ORACLE_PATH, ROOT / "evaluation/window_integration/service_response_guard_oracles.json",
        ROOT / "simulated_bank/fixtures/demo.json", ROOT / "simulated_bank/service_benefit_rules.json"))
    return {str(path.relative_to(ROOT)): file_sha(path) for path in sorted(paths) if path.is_file()}


class LocalPrimary(_cross.ScriptedPrimary):
    billing_mode = "unpaid_fixture"

    def complete(self, *args, **kwargs):
        response, _ = super().complete(*args, **kwargs)
        # 没有模型推理/请求：脚本fixture实际token为0，不代表付费provider用量。
        return response, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class _LocalJsonResponse:
    def __init__(self, value):
        self.status = 200
        self.body = json.dumps(value, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body if limit is None or limit < 0 else self.body[:limit]


class LocalReviewer(_cross.ScriptedReviewer):
    """Keep the frozen decision but build/capture the real GLM review wire locally."""
    billing_mode = "unpaid_fixture"

    def __init__(self, verdict="approve", hook=None):
        super().__init__(verdict=verdict, hook=hook)
        from agent_runtime import conversation as conversation_module
        from agent_runtime.conversation import GLMConversationReviewer
        from agent_runtime.model_adapters import GLM_REVIEW_MODEL

        # CrossModuleIntegrationTests blocks the shared client method globally.
        # Save its original implementation before per-test guards are installed;
        # this subclass restores it only for the one client whose urlopen is the
        # in-process fixture below.
        class OfflineWireClient(OpenAICompatibleJsonClient):
            complete_json = _OFFLINE_WIRE_COMPLETE_JSON

        self._conversation_module = conversation_module
        self._offline_wire_client_class = OfflineWireClient

        self.model_id = GLM_REVIEW_MODEL
        self._offline_decision = threading.local()
        self._wire_lock = threading.Lock()
        self.wire_requests = []
        local_url = "https://offline-final-review.invalid/v1/chat/completions"

        def local_open(request, *, timeout=None, **kwargs):
            if (getattr(request, "full_url", None) != local_url
                    or request.get_method() != "POST"
                    or not isinstance(getattr(request, "data", None), bytes)):
                raise ValueError("offline_final_review_request_contract_invalid")
            payload = json.loads(request.data.decode("utf-8"))
            if payload.get("model") != GLM_REVIEW_MODEL:
                raise ValueError("offline_final_review_model_binding_invalid")
            decision = getattr(self._offline_decision, "value", None)
            if type(decision) is not dict:
                raise ValueError("offline_final_review_decision_unbound")
            with self._wire_lock:
                self.wire_requests.append({"method": request.get_method(), "url": local_url,
                    "request_bytes": len(request.data), "request_sha256": hashlib.sha256(request.data).hexdigest(),
                    "transport": "in_process_fixture_no_network", "billing_mode": "unpaid_fixture"})
            return _LocalJsonResponse({"id": "offline-final-review", "model": GLM_REVIEW_MODEL,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        client = OpenAICompatibleJsonClient(provider="ai_ping", model_id=GLM_REVIEW_MODEL,
            url=local_url, env_name="NAVREN_OFFLINE_FINAL_REVIEW_KEY",
            keychain_service="CC/NAVREN_OFFLINE_FINAL_REVIEW_KEY", timeout=35, max_retries=0,
            urlopen=local_open, key_loader=lambda: "offline-fixture-only", log_path=os.devnull)
        self._wire_reviewer = GLMConversationReviewer(client=client)
        self._wire_reviewer.billing_mode = "unpaid_fixture"

    def review(self, packet, *args, **kwargs):
        scripted = super().review(packet, *args)
        expected = {key: scripted[key] for key in ("verdict", "reason", "issues")}
        self._offline_decision.value = expected
        try:
            # The inner production reviewer reconstructs its client here. Route
            # that constructor to the subclass that still uses only local_open.
            with patch.object(self._conversation_module, "OpenAICompatibleJsonClient",
                              self._offline_wire_client_class):
                response = self._wire_reviewer.review(packet, *args, **kwargs)
        finally:
            self._offline_decision.value = None
        if any(response.get(key) != value for key, value in expected.items()):
            raise ValueError("offline_final_review_decision_changed")
        if not isinstance(response.get("_agent_review_wire"), bytes) or not response["_agent_review_wire"]:
            raise ValueError("offline_final_review_wire_missing")
        return response


class TwoPieceProvider(_voice.InMemoryAudioProvider):
    """真实SpeechTask握手后给首片，下一片有界等待；第三片是必须废弃的旧队列。"""
    def __init__(self):
        super().__init__(hold=True)
        self.second_waiting, self.release_second, self.second_returned = (
            threading.Event(), threading.Event(), threading.Event())
        self.receives, self.third_returned = 0, False
        self.pieces = [bytes.fromhex(FIXTURE[key]) * FIXTURE["pcm_repeats"]
            for key in ("first_pcm_hex", "second_pcm_hex", "third_pcm_hex")]

    def recv(self):
        self.receives += 1
        if self.receives == 1:
            return super().recv()
        if self.receives == 2:
            return self.pieces[0]
        if self.receives == 3:
            self.second_waiting.set()
            if not self.release_second.wait(WAIT):
                raise AssertionError("second_pcm_not_released_within_bound")
            self.second_returned.set()
            return self.pieces[1]
        if self.receives == 4:
            self.third_returned = True
            return self.pieces[2]
        return super().recv()

    def close(self, timeout=1):
        self.release_second.set()
        super().close(timeout)


class CommitConnection:
    """所有SQL/commit/rollback都委托真实SQLite，只在目标commit前阻塞或注入故障。"""
    def __init__(self, raw, probe):
        self.raw, self.probe, self.target = raw, probe, False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        return self.raw.__exit__(*args)

    def commit(self):
        if not self.target:
            return self.raw.commit()
        self.probe["phase_events"].append({'phase': 'target_native_commit_enter', 'clock': time.monotonic()})
        self.probe["entered"].set()
        if not self.probe["release"].wait(WAIT):
            raise AssertionError("bank_commit_not_released_within_bound")
        if self.probe["fail"]:
            raise sqlite3.OperationalError("isolated_synthetic_commit_failure")
        self.probe["phase_events"].append({'phase': 'target_before_native_commit', 'clock': time.monotonic()})
        result = self.raw.commit()
        self.probe["phase_events"].append({'phase': 'target_native_commit_completed', 'clock': time.monotonic()})
        self.probe["committed"].set()
        self.probe["bank_exit"].set()
        return result

    def rollback(self):
        result = self.raw.rollback()
        if self.target:
            self.probe["phase_events"].append({'phase': 'target_native_rollback_completed', 'clock': time.monotonic()})
            self.probe["rolled_back"].set()
            self.probe["bank_exit"].set()
        return result


class ServiceTransportCommitContracts(unittest.TestCase):
    raw_state = _cross.CrossModuleIntegrationTests.raw_state
    operation_count = _cross.CrossModuleIntegrationTests.operation_count
    public_fixture = _cross.CrossModuleIntegrationTests.public_fixture
    http_json = _voice.MarketVoiceNegotiationIntegrationTests.http_json
    connect_ticket = _voice.MarketVoiceNegotiationIntegrationTests.connect_ticket
    require = _service.ServiceResponseGuardContracts.require
    actual_server = _service.ServiceResponseGuardContracts.actual_server
    no_side_effects = _service.ServiceResponseGuardContracts.no_side_effects
    confirm_body = _cross.CrossModuleIntegrationTests.confirm_body

    @classmethod
    def setUpClass(cls):
        if file_sha(ORACLE_PATH) != FROZEN_ORACLE_SHA:
            raise RuntimeError("frozen_transport_commit_oracle_changed")
        if Path(sys.executable).absolute().parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required")
        cls.before_sources, cls.observations = source_manifest(), []

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        print("WINDOW_TRANSPORT_COMMIT_SUMMARY " + json.dumps({
            "oracle_sha256": FROZEN_ORACLE_SHA, "source_sha256_before": cls.before_sources,
            "source_sha256_after": after, "source_unchanged": after == cls.before_sources,
            "observations": cls.observations, "cloud_calls": 0, "scope": ORACLE["scope"],
        }, ensure_ascii=False, sort_keys=True), flush=True)
        if after != cls.before_sources:
            raise RuntimeError("transport_commit_inputs_changed_during_test")

    def setUp(self):
        # 只借用既有隔离辅助，不继承旧TestCase，不改变旧测试/oracle。
        _service.ServiceResponseGuardContracts.setUp(self)
        self.require(self.g.operation_evidence, "bind_answer", "public_guard")
        self.require(self.g, "assert_publication_current")
        self.round_receipts = []
        original_approval = self.g.approve_speech
        def remember_round(workspace, result, session_token=None):
            receipt = original_approval(workspace, result, session_token)
            # 凭据仅在测试内存中用于本请求观察，绝不写日志或发送给模型。
            self.round_receipts.append({"receipt": receipt, "response_id": result.get("bank_response_id"),
                "handler_thread_ident": threading.get_ident(),
                "operation_ids": [item["operation_id"] for item in result.get("bank_actions", []) if item.get("operation_id")]})
            return receipt
        recorder = patch.object(self.g, "approve_speech", side_effect=remember_round)
        recorder.start()
        self.addCleanup(recorder.stop)

    def evidence(self, **values):
        row = {"case": CASE_IDS[self._testMethodName], "test": self._testMethodName,
            "oracle_sha256": FROZEN_ORACLE_SHA, "cloud_calls": 0,
            "scope": ORACLE["scope"], **values}
        self.observations.append(row)
        print("WINDOW_TRANSPORT_COMMIT_EVIDENCE " + json.dumps(
            row, ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def counts(self, owner):
        result = _service.ServiceResponseGuardContracts.counts(self, owner)
        # 本组明确允许prepare草案；资金操作以真实已执行状态计数，另留总草案数。
        result["financial_drafts"] = result["financial_operations"]
        with closing(self.g.bank.connect()) as db:
            result["financial_operations"] = db.execute(
                "SELECT count(*) FROM operations WHERE workspace_id=? AND state='succeeded'", (owner,)).fetchone()[0]
        return result

    def observed_round_state(self, operation=None):
        records = [row for row in self.round_receipts if operation is None or operation in row["operation_ids"]]
        record = records[-1] if records else None
        ids = [operation] if operation else (record["operation_ids"] if record else [])
        with closing(self.g.bank.connect()) as db:
            states = []
            for operation_id in ids:
                row = db.execute("SELECT id,state,result FROM operations WHERE id=? AND workspace_id=?",
                    (operation_id, self.w)).fetchone()
                bindings = db.execute("SELECT count(*) FROM base_operation_evidence WHERE operation_id=? AND workspace_id=?",
                    (operation_id, self.w)).fetchone()[0]
                states.append({"id": operation_id, "state": row["state"] if row else None,
                    "has_result": bool(row and row["result"]), "evidence_binding_rows": bindings})
        messages = self.g.personal_data.read_history(self.w)
        with closing(self.g.personal_data.connect()) as db:
            widgets = db.execute("SELECT count(*) FROM agent_widget_history WHERE workspace_id=? AND response_id=?",
                (self.w, record["response_id"])).fetchone()[0] if record and record["response_id"] else None
        with self.g._speech_lock:
            retained = bool(record and record["receipt"] in self.g._speech_receipts)
        return {"only_this_request_operation_states": states,
            "response_id": record["response_id"] if record else None,
            "speech_receipt_issued": bool(record and record["receipt"]), "speech_receipt_retained": retained,
            "history_entries": len(messages),
            "old_reply_in_history": any(message.get("role") == "assistant" and
                message.get("content") == FIXTURE["approved_reply"] for message in messages),
            "this_response_widget_rows": widgets,
            "history_observation_limit": "workspace历史是否包含本轮合成正文；未撤销或改写任何轮次"}

    def local_agent(self, server, *, draft=False):
        def prepare(messages, tools):
            outputs = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            accounts = next(output["data"]["accounts"] for output in outputs if "accounts" in output.get("data", {}))
            payment = next(output["data"] for output in outputs if "beneficiaries" in output.get("data", {}))
            return _cross.call("prepare_operation", {"action": "transfer",
                "account_id": next(row["id"] for row in accounts if row["id"].endswith("-main")),
                "beneficiary_id": next(row["id"] for row in payment["beneficiaries"] if row["alias"] == "妈妈"),
                "amount_minor": FIXTURE["transfer_minor"]})

        def answer(messages, tools):
            outputs = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            ids = [output["evidence_id"] for output in outputs if "evidence_id" in output]
            operations = [output["data"]["operation"]["id"] for output in outputs
                if "operation" in output.get("data", {})]
            return _cross.call("answer_reviewed", {"text": FIXTURE["approved_reply"],
                "claims": [{"text": "预览条件须另行接受，公开条款说明本金风险；草案尚未执行。", "evidence_ids": ids}],
                "draft_ids": operations, "followups": []})

        steps = [_cross.call("service_offer_preview", {}), _cross.call("knowledge_search", {
            "query": "风险", "product_id": FIXTURE["public_product"], "as_of": FIXTURE["public_as_of"]})]
        if draft:
            steps.extend([_cross.call("account_summary", {}), _cross.call("business_records", {"kind": "payments"}), prepare])
        self.primary, self.reviewer = LocalPrimary(steps + [answer]), LocalReviewer()
        # 只给本地无付费fixture明确有界UTF8预算，本组不验收生产token准入成绩。
        server.conversation_agent = ConversationAgent(self.primary, self.reviewer,
            max_tokens=FIXTURE["local_fixture_byte_budget"])

    def wire_agent(self, transport="json", *, draft=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=WAIT + 2)
        try:
            connection.request("POST", "/api/agent", body=json.dumps({
                "text": FIXTURE["commit_request"] if draft else FIXTURE["request"], "mode": "classic"}), headers={
                "Content-Type": "application/json", "Accept": "text/event-stream" if transport == "sse" else "application/json",
                "Cookie": COOKIE + "=" + self.token, "X-QZ-CSRF": self.g.csrf(self.token), "Origin": self.base})
            response = connection.getresponse()
            truncated = False
            try:
                body = response.read()
            except http.client.IncompleteRead as error:
                body, truncated = error.partial, True
            return {"status": response.status, "body": body, "truncated": truncated}
        finally:
            connection.close()

    def expire_preview(self):
        self.assertEqual(len(self.reviewer.packets), 1)
        self.assertTrue(self.approvals and self.approvals[-1]["receipt_issued"])
        preview = self.approvals[-1]["service_snapshot"]["preview"]
        with closing(self.g.bank.connect()) as db:
            row = db.execute("SELECT expires_at,created_at FROM service_negotiation_drafts WHERE id=? AND workspace_id=?",
                (preview["draft_id"], self.w)).fetchone()
        self.assertEqual(row["expires_at"] - row["created_at"], FIXTURE["preview_seconds"])
        self.assertLess(self.clock_now, row["expires_at"])
        self.clock_now = row["expires_at"] + FIXTURE["expiry_extra_seconds"]
        return {"preview_id": preview["draft_id"], "expires_at": row["expires_at"], "clock_after": self.clock_now}

    def test_expiry_after_json_and_sse_serialization_before_real_socket_send(self):
        self.require(self.g.personal_data, "save_turn_guarded")
        before = self.raw_state()
        original_send = PublicationSocket.sendall
        with self.actual_server() as server:
            for transport in ("json", "sse"):
                with self.subTest(transport=transport):
                    self.clock_now = time.time()
                    self.local_agent(server)
                    windows = []
                    marker = FIXTURE["approved_reply"].encode()
                    escaped = json.dumps(FIXTURE["approved_reply"])[1:-1].encode()

                    def send(sock, data, *args, **kwargs):
                        immutable = bytes(data)
                        if not windows and (marker in immutable or escaped in immutable):
                            windows.append({"serialized_frame_sha256": hashlib.sha256(immutable).hexdigest(),
                                "serialized_bytes": len(immutable), "window": "PublicationSocket_before_original_sendall",
                                **self.expire_preview()})
                        return original_send(sock, immutable, *args, **kwargs)

                    with patch.object(PublicationSocket, "sendall", new=send):
                        wire = self.wire_agent(transport)
                    body = wire["body"]
                    events = []
                    for line in body.decode(errors="replace").splitlines():
                        if line.startswith("data:"):
                            events.append(json.loads(line[5:].strip()))
                    final = [event for event in events if event.get("name") == "navren.result"]
                    old_final = [event for event in final if event.get("value", {}).get("reply") == FIXTURE["approved_reply"]
                        or event.get("value", {}).get("speech_receipt")]
                    self.evidence(transport="actual_" + transport, http_status=wire["status"],
                        received_body_bytes=len(body), truncated=wire["truncated"], late_windows=windows,
                        final_sse_events=len(final), old_final_sse_events=len(old_final),
                        event_types=[event.get("type") for event in events],
                        round_state=self.observed_round_state(),
                        financial_state_unchanged=self.raw_state() == before, state=self.counts(self.w))
                    self.assertEqual(len(windows), 1, "必须实际观察完整序列化帧后窗口")
                    self.assertNotIn(marker, body)
                    self.assertNotIn(escaped, body)
                    self.assertNotIn(b"speech_receipt", body)
                    if transport == "json":
                        self.assertEqual(len(body), EXPECT["old_json_body_bytes"])
                    else:
                        self.assertTrue(any(event.get("type") == "RUN_STARTED" for event in events))
                        self.assertEqual(len(old_final), EXPECT["old_final_sse_events"])
                    self.no_side_effects(before)

    def test_first_pcm_valid_second_serialized_frame_expires_and_receipt_cannot_replay(self):
        self.require(self.g.personal_data, "save_turn_guarded")
        from websocket import WebSocketConnectionClosedException
        before, provider = self.raw_state(), TwoPieceProvider()
        original_send, windows, received, frames, slots = PublicationSocket.sendall, [], [], [], 0
        def factory(kind, options, key, url):
            self.assertEqual(kind, "tts")
            self.providers.append(provider)
            return SpeechTask(kind, options, key, url, connector=lambda *args, **kwargs: provider)
        def send(sock, data, *args, **kwargs):
            immutable = bytes(data)
            if not windows and provider.pieces[1] in immutable:
                self.assertEqual(immutable[0] & 15, 2, "必须是真实已编码WS二进制帧")
                windows.append({"serialized_frame_sha256": hashlib.sha256(immutable).hexdigest(),
                    "serialized_bytes": len(immutable), "window": "serialized_second_pcm_before_actual_socket_send",
                    **self.expire_preview()})
            return original_send(sock, immutable, *args, **kwargs)
        with self.actual_server() as server:
            self.local_agent(server)
            wire = self.wire_agent()
            result = json.loads(wire["body"])
            if result.get("reply") != FIXTURE["approved_reply"]:
                self.evidence(phase="BLOCKED_BEFORE_WS", http_status=wire["status"],
                    error=result.get("error"), review_status=result.get("agent_review", {}).get("status"))
            self.assertEqual(result.get("reply"), FIXTURE["approved_reply"], result.get("error"))
            self.assertEqual(result.get("agent_review", {}).get("status"), "approved", result.get("error"))
            receipt = result["speech_receipt"]
            server.voice.task_factory = factory
            status, ticket = self.http_json("POST", "/api/speech/session", {"kind": "tts", "speech_receipt": receipt})
            self.assertEqual(status, 200, ticket)
            with patch.object(PublicationSocket, "sendall", new=send):
                ws = self.connect_ticket(ticket["path"])
                try:
                    ws.send(json.dumps({"type": "start", "voice": "female_sweet"}))
                    self.assertEqual(json.loads(ws.recv())["type"], "ready")
                    first = ws.recv()
                    self.assertEqual(first, provider.pieces[0], "客户端必须实际收到有效首片")
                    received.append(first)
                    self.assertTrue(provider.second_waiting.wait(WAIT))
                    provider.release_second.set()
                    closed = False
                    for _ in range(6):
                        try:
                            frame = ws.recv()
                        except WebSocketConnectionClosedException:
                            closed = True
                            break
                        if frame == "":
                            closed = True
                            break
                        if isinstance(frame, bytes):
                            received.append(frame)
                        else:
                            frames.append(json.loads(frame))
                    self.assertTrue(closed, "失效后连接必须有界关闭")
                finally:
                    provider.release_second.set()
                    ws.close()
            self.assertTrue(provider.closed.wait(WAIT))
            for _ in range(EXPECT["voice_slots_restored"]):
                self.assertTrue(server.voice.slots.acquire(timeout=WAIT))
                slots += 1
            for _ in range(slots):
                server.voice.slots.release()
            with self.g._speech_lock:
                discarded = receipt not in self.g._speech_receipts
            replay_status, replay = self.http_json("POST", "/api/speech/session", {"kind": "tts", "speech_receipt": receipt})
        self.evidence(transport="actual_ws", late_windows=windows, first_pcm_bytes=len(received[0]),
            later_pcm_bytes=sum(map(len, received[1:])), frames=frames,
            second_upstream_returned=provider.second_returned.is_set(), third_upstream_returned=provider.third_returned,
            upstream_closed=provider.closed.is_set(), slots_restored=slots, receipt_discarded=discarded,
            replay_status=replay_status, replay_error=replay.get("error", {}).get("code"),
            round_state=self.observed_round_state(),
            financial_state_unchanged=self.raw_state() == before, state=self.counts(self.w))
        self.assertEqual(len(windows), 1)
        self.assertTrue(provider.second_returned.is_set())
        self.assertEqual(sum(map(len, received[1:])), EXPECT["later_pcm_bytes"])
        self.assertFalse(provider.third_returned, "失效后旧队列不得继续取片")
        self.assertEqual(discarded, EXPECT["receipt_discarded_after_invalid_frame"])
        self.assertGreaterEqual(replay_status, 400)
        self.no_side_effects(before)

    def commit_case(self, *, fail):
        before, prior_revision = self.raw_state(), self.g.personal_data.revision(self.w)
        probe = {name: threading.Event() for name in ("entered", "release", "committed", "rolled_back", "bank_exit")}
        probe["fail"] = fail
        probe["phase_events"] = []
        self.addCleanup(probe["release"].set)
        rules = self.g.service_negotiations.rules()
        expected_hash = self.g.service_negotiations._rules_digest(rules)
        changed_rules = copy.deepcopy(rules)
        row = next(row for row in changed_rules["offers"] if row["offer_id"] == FIXTURE["electronic_offer_id"])
        row["conditions"] = ["隔离commit后由受信维护者更新，须重新确认"]
        results, writers, errors, operations, threads = {}, {}, [], [], []
        original_connect, original_bind = self.g.bank.connect, self.g.operation_evidence.bind_answer
        def connect():
            return CommitConnection(original_connect(), probe)
        def bind(workspace, result, db):
            probe["phase_events"].append({'phase': 'bind_answer_enter', 'clock': time.monotonic()})
            original_bind(workspace, result, db)
            probe["phase_events"].append({'phase': 'bind_answer_completed', 'clock': time.monotonic()})
            ids = [action["operation_id"] for action in result.get("bank_actions", []) if action.get("operation_id")]
            if ids:
                self.assertEqual(len(ids), 1)
                self.assertIsInstance(db, CommitConnection)
                self.assertEqual(db.execute("SELECT count(*) FROM base_operation_evidence WHERE operation_id=?", (ids[0],)).fetchone()[0], 1)
                operations.extend(ids)
                db.target = True
                probe["phase_events"].append({'phase': 'target_commit_armed', 'clock': time.monotonic()})
        def run(name, action):
            writers[name] = {"entered": threading.Event(), "done": threading.Event()}
            def target():
                writers[name]["entered"].set()
                try:
                    results[name] = action()
                    writers[name]["before_bank_exit"] = not probe["bank_exit"].is_set()
                except Exception as error:
                    errors.append({"writer": name, "type": type(error).__name__, "message": str(error)})
                finally:
                    writers[name]["done"].set()
            thread = threading.Thread(target=target, daemon=True, name="stc-" + name)
            threads.append(thread)
            thread.start()
            return thread
        with self.actual_server() as server:
            self.local_agent(server, draft=True)
            with patch.object(self.g.bank, "connect", new=connect), patch.object(self.g.operation_evidence, "bind_answer", new=bind):
                request = run("http", lambda: self.wire_agent(draft=True))
                try:
                    entered = probe["entered"].wait(WAIT)
                    if not entered:
                        http = results.get("http", {})
                        payload = json.loads(http["body"]) if http.get("body") else {}
                        self.evidence(phase="BLOCKED_BEFORE_COMMIT", commit_failure=fail,
                            http_status=http.get("status"), error=payload.get("error"),
                            review_status=payload.get("agent_review", {}).get("status"),
                            bank_actions=len(payload.get("bank_actions", [])), thread_errors=errors)
                    self.assertTrue(entered, "真实HTTP的bind_answer后commit窗口未到达")
                    cas = run("cas", lambda: self.g.service_negotiations.publish_rules(changed_rules, expected_sha256=expected_hash))
                    personal = run("personal", lambda: self.g.memories.create(self.w, {
                        "category": "goal", "value": "隔离个人资料作者等待bank提交后写入。", "expected_revision": prior_revision}))
                    self.assertTrue(writers["cas"]["entered"].wait(WAIT))
                    self.assertTrue(writers["personal"]["entered"].wait(WAIT))
                    cas_early = writers["cas"]["done"].wait(FIXTURE["waiting_observation_seconds"])
                    personal_early = writers["personal"]["done"].wait(FIXTURE["waiting_observation_seconds"])
                    with closing(sqlite3.connect(self.g.knowledge_provider.path, timeout=0.05)) as public_db:
                        try:
                            public_db.execute("BEGIN IMMEDIATE")
                            public_lock_available = True
                            public_db.rollback()
                        except sqlite3.OperationalError as error:
                            if "locked" not in str(error).lower():
                                raise
                            public_lock_available = False
                finally:
                    probe["release"].set()
                    failing = sys.exc_info()[0] is not None
                    handlers_at_assertion = server.fixture_handler_snapshot()
                    if failing:
                        # 只收口失败路径；原5秒entry与7秒HTTP门不延长。
                        cleanup_deadline = time.monotonic() + WAIT
                        for worker in threads:
                            worker.join(max(0, cleanup_deadline - time.monotonic()))
                        server.fixture_wait_handlers(WAIT)
                    http_observed = results.get("http", {})
                    try:
                        payload_observed = json.loads(http_observed["body"]) if http_observed.get("body") else {}
                    except (ValueError, UnicodeDecodeError):
                        payload_observed = {}
                    self.evidence(phase="STC_finally_before_patch_scope_exit", commit_failure=fail,
                        exceptional_exit=failing, entered_at_original_success_gate=entered,
                        commit_entered=probe["entered"].is_set(), committed=probe["committed"].is_set(),
                        rolled_back=probe["rolled_back"].is_set(), bank_exit=probe["bank_exit"].is_set(),
                        phase_events=copy.deepcopy(probe["phase_events"]),
                        primary_calls=len(self.primary.sent), primary_pending_steps=len(self.primary.steps),
                        review_calls=len(self.reviewer.packets), receipt_rounds=len(self.round_receipts),
                        round_requests=[{"response_id": record["response_id"],
                            "handler_thread_ident": record["handler_thread_ident"], "operation_ids": record["operation_ids"]}
                            for record in self.round_receipts],
                        http_status=http_observed.get("status"), client_error=payload_observed.get("error"),
                        client_body_bytes=len(http_observed.get("body", b"")),
                        client_body_sha256=hashlib.sha256(http_observed.get("body", b"")).hexdigest(),
                        thread_errors=copy.deepcopy(errors),
                        client_threads=[{"name": worker.name, "alive": worker.is_alive()} for worker in threads],
                        handlers_at_original_gate=handlers_at_assertion,
                        handlers_before_patch_exit=server.fixture_handler_snapshot())
                for thread in (request, cas, personal):
                    thread.join(WAIT)
                    self.assertFalse(thread.is_alive(), "个人/规则作者或请求未有界完成")
                self.assertTrue(probe["bank_exit"].wait(WAIT))
        self.assertEqual(len(operations), 1)
        operation = operations[0]
        with closing(original_connect()) as db:
            bindings = db.execute("SELECT count(*) FROM base_operation_evidence WHERE operation_id=?", (operation,)).fetchone()[0]
            audits = db.execute("SELECT count(*) FROM audit WHERE operation_id=? AND event='operation_public_evidence_bound'", (operation,)).fetchone()[0]
            execution_audits = db.execute("SELECT count(*) FROM audit WHERE workspace_id=? AND event='operation_executed'", (self.w,)).fetchone()[0]
        http = results.get("http", {})
        body = http.get("body", b"")
        http_payload = json.loads(body) if body else {}
        successful_frames = int(http.get("status") == 200 and not http_payload.get("isFallback")
            and http_payload.get("agent_review", {}).get("status") == "approved")
        self.evidence(transport="actual_http_plus_real_sqlite_commit", commit_failure=fail,
            reached_real_bind_commit=True, operation_id=operation, cas_finished_while_commit_blocked=cas_early,
            personal_finished_while_commit_blocked=personal_early, public_write_lock_available_while_commit_blocked=public_lock_available,
            committed=probe["committed"].is_set(), rolled_back=probe["rolled_back"].is_set(),
            writers_done={name: value["done"].is_set() for name, value in writers.items()},
            writer_completed_before_bank_exit={name: value.get("before_bank_exit") for name, value in writers.items() if name != "http"},
            errors=errors, http_status=http.get("status"), received_body_bytes=len(body),
            received_body_sha256=hashlib.sha256(body).hexdigest(), successful_financial_frames=successful_frames,
            client_reply=http_payload.get("reply"), client_has_speech_receipt=bool(http_payload.get("speech_receipt")),
            binding_rows=bindings, binding_audits=audits, execution_audits=execution_audits,
            round_state=self.observed_round_state(operation),
            personal_revision_before=prior_revision, personal_revision_after=self.g.personal_data.revision(self.w),
            state=self.counts(self.w), financial_state_unchanged=self.raw_state() == before)
        self.assertEqual(errors, [])
        self.assertEqual(cas_early, EXPECT["cas_writer_finished_before_bank_exit"])
        self.assertEqual(personal_early, EXPECT["personal_writer_finished_before_bank_exit"])
        self.assertEqual(public_lock_available, EXPECT["public_writer_lock_available_before_bank_exit"])
        for name in ("cas", "personal"):
            self.assertFalse(writers[name]["before_bank_exit"])
        self.assertEqual(self.g.personal_data.revision(self.w), prior_revision + 1)
        self.assertEqual(execution_audits, 0)
        if fail:
            self.assertEqual(successful_frames, EXPECT["failed_commit_success_financial_frames"],
                "SQLite提交失败时客户端不得已收到成功金融帧")
            self.assertEqual(bindings, EXPECT["failed_binding_rows_after_rollback"])
            self.assertEqual(audits, EXPECT["failed_binding_audits_after_rollback"])
            self.assertEqual(probe["rolled_back"].is_set(), EXPECT["bank_commit_failure_rollback_observed"])
            self.assertFalse(probe["committed"].is_set())
        else:
            self.assertEqual(bindings, EXPECT["normal_binding_rows_after_commit"])
            self.assertEqual(audits, 1)
            self.assertTrue(probe["committed"].is_set())
            # commit之后受信作者可先完成；二阶段须拒绝新版本下的旧结论。
            self.assertIn(http.get("status"), (200, 409))
            if http.get("status") == 200:
                self.assertEqual(http_payload.get("reply"), FIXTURE["approved_reply"])
            else:
                self.assertIn(http_payload.get("error", {}).get("code"), (
                    "STALE_SERVICE_EVIDENCE", "PERSONAL_VERSION_CONFLICT", "METRIC_VERSION_CONFLICT",
                    "STALE_EVIDENCE", "PRIVACY_REVISION_CHANGED"))
                self.assertEqual(successful_frames, 0)
                self.assertFalse(http_payload.get("speech_receipt"))
        self.no_side_effects(before)

    def test_actual_bind_commit_holds_public_and_rules_locks_with_personal_writer(self):
        self.commit_case(fail=False)

    def test_actual_bind_commit_failure_rolls_back_before_rules_and_public_unlock(self):
        self.commit_case(fail=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
