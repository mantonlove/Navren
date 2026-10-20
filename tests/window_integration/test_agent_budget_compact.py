"""独立增量验收：真实 Agent/原生及 GLM 适配/边界/登记/末端守卫，无云。

预期来自 solution-v2 2.1--2.3，先冻结于 agent_budget_compact_oracles.json。
离线替身仅占提供器传输层；不复制计量、压缩、还原或金额校验算法。
运行：.venv/bin/python -B tests/window_integration/test_agent_budget_compact.py
"""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
ORACLE_PATH = ROOT / "evaluation/window_integration/agent_budget_compact_oracles.json"
ORACLE_SHA256 = "8449f0b95a9149b93863397269b7d230437e0fdaa2301ed9d978032a09af5f32"
OUTPUT_ROOT = ROOT / "outputs/window-progress/integration/agent-budget-compact"
ORACLE = json.loads(ORACLE_PATH.read_text())
FIELDS = tuple(ORACLE["ref_fields_excluding_sources"])
CASES = {case["test"]: case for case in ORACLE["cases"]}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    paths = {path for path in ROOT.glob("*.py") if path.is_file()}
    for folder in ("agent_runtime", "base_runtime", "simulated_bank", "financial_methods", "local_knowledge"):
        paths.update((ROOT / folder).rglob("*.py"))
    paths.update((ORACLE_PATH, Path(__file__).resolve(), ROOT / ORACLE["basis"]["path"]))
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}


def tool_call(name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": "call-" + name,
        "type": "function", "function": {"name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False, allow_nan=False)}}]}


def envelope(message, usage, finish_reason="stop"):
    result = {"id": "offline-envelope", "model": "offline-transport-fixture",
              "choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        result["usage"] = copy.deepcopy(usage)
    return result


class ByteResponse:
    status = 200

    def __init__(self, value):
        self.raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.raw if size < 0 else self.raw[:size]


class OfflineTransport:
    """可信 runner 的离线传输；从正式客户端的序列化 request 读取模型视图。"""
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, request, **kwargs):
        payload = json.loads(request.data)
        self.requests.append(copy.deepcopy(payload))
        if not self.steps:
            raise AssertionError("unexpected_additional_supplier_call")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        value = step(payload) if callable(step) else step
        return ByteResponse(value)


class AgentBudgetCompactContracts(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if sha(ORACLE_PATH) != ORACLE_SHA256:
            raise RuntimeError("frozen_agent_budget_compact_oracle_changed")
        if Path(sys.executable).absolute().parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required")
        destination = os.environ.get("NAVREN_AGENT_COMPACT_OUTPUT")
        cls.output = (Path(destination).resolve() if destination else
                      OUTPUT_ROOT / ("run-" + str(time.time_ns()) + "-" + uuid.uuid4().hex[:8]))
        if not cls.output.is_relative_to(OUTPUT_ROOT.resolve()):
            raise RuntimeError("isolated_output_scope_required")
        cls.output.mkdir(parents=True, exist_ok=True)
        if (cls.output / "suite-evidence.json").exists():
            raise RuntimeError("new_run_directory_required")
        cls.before, cls.observations = source_manifest(), []
        (cls.output / "source-before.json").write_text(json.dumps(cls.before, indent=2) + "\n")
        sys.path.insert(0, str(ROOT))
        cls.addClassCleanup(lambda: sys.path.remove(str(ROOT)))
        cls.conversation = importlib.import_module("agent_runtime.conversation")
        cls.native = importlib.import_module("agent_runtime.native_client")
        cls.adapters = importlib.import_module("agent_runtime.model_adapters")
        cls.request_budget = importlib.import_module("agent_runtime.request_budget")
        cls.boundaries = importlib.import_module("agent_runtime.data_boundary")
        cls.bank_tools = importlib.import_module("agent_runtime.bank_tools")
        cls.claims = importlib.import_module("agent_runtime.metric_claims")
        cls.gateway = importlib.import_module("finance_gateway")
        cls.BankError = importlib.import_module("simulated_bank").BankError
        cls.privacy_revision = staticmethod(importlib.import_module("user_memory").privacy_revision)
        profile_client = cls.native.AIPingToolClient(key_loader=lambda: "offline-fixture-placeholder")
        profile_wire = profile_client.serialized_request([
            {"role": "system", "content": "离线预算配置核验"},
            {"role": "user", "content": "仅核验模型 profile，不调用网络"}], [])
        profile = cls.request_budget.estimate_request(profile_wire)
        if (profile.get("model_id") != cls.native.AIPING_PRIMARY_MODEL
                or profile.get("bound_method") != "official_dsv4_two_profiles_two_modes_max_20pct_plus1024"
                or {row.get("profile") for row in profile.get("profiles", [])} != {"base", "0731"}
                or profile.get("provider_bound_certified") is not False):
            raise RuntimeError("canonical_aiping_v4_budget_profile_unavailable")
        cls.primary_budget_profile = profile
        if source_manifest() != cls.before:
            raise RuntimeError("candidate_changed_during_import")

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        evidence = {"suite_id": ORACLE["suite_id"], "oracle_sha256": ORACLE_SHA256,
            "source_sha256_before": cls.before, "source_sha256_after": after,
            "sources_unchanged": cls.before == after,
            "authoring_source_sha256": ORACLE["authoring_source_sha256"],
            "authoring_snapshot_matches": all(cls.before.get(p) == v
                for p, v in ORACLE["authoring_source_sha256"].items()),
            "cloud_model_calls": 0, "real_supplier_budget_gate_verified": False,
            "offline_fixture": {"primary_provider": "ai_ping",
                "primary_model_id": cls.native.AIPING_PRIMARY_MODEL,
                "primary_bound_method": cls.primary_budget_profile["bound_method"],
                "primary_profiles": sorted({row["profile"] for row in cls.primary_budget_profile["profiles"]}),
                "provider_bound_certified": False,
                "reviewer_provider": "ai_ping", "reviewer_model_id": cls.conversation.GLM_REVIEW_MODEL},
            "formal_financial_four_questions_verified": False,
            "observations": cls.observations}
        (cls.output / "suite-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print("WINDOW_AGENT_BUDGET_COMPACT_SUMMARY " + json.dumps({
            "oracle_sha256": ORACLE_SHA256, "sources_unchanged": cls.before == after,
            "authoring_snapshot_matches": evidence["authoring_snapshot_matches"],
            "observations": len(cls.observations), "output": str(cls.output),
            "cloud_model_calls": 0}, ensure_ascii=False), flush=True)
        if cls.before != after or sha(ORACLE_PATH) != ORACLE_SHA256:
            raise RuntimeError("candidate_or_oracle_changed_during_test")

    def setUp(self):
        from cryptography.fernet import Fernet
        temporary = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.output)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        env = patch.dict(os.environ, {"NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(),
            "NAVREN_SIM_DB": str(self.directory / "bank.sqlite3"),
            "NAVREN_KNOWLEDGE_DB": str(self.directory / "knowledge.sqlite3"),
            "NAVREN_MARKET_DB": str(self.directory / "market.sqlite3"),
            "NAVREN_FUND_CACHE": str(self.directory / "funds"),
            "NAVREN_MODEL_CALL_LOG": str(self.directory / "default-provider.jsonl"),
            "NAVREN_ALLOW_CLOUD_ASR": "0"})
        env.start()
        self.addCleanup(env.stop)
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("network_forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.g = self.gateway.FinanceGateway(self.directory / "bank.sqlite3")
        self.g.service_negotiations.rules_path = self.directory / "service-rules.json"
        self.g.service_negotiations.rules_path.write_bytes((ROOT / "simulated_bank/service_benefit_rules.json").read_bytes())
        self.w, self.token = self.g.create_session()
        self.assertFalse(self.g.demo_accounts.is_demo(self.w))
        for path in (self.g.bank.database, self.g.personal_data.database, self.g.knowledge_provider.path,
                     self.g.service_negotiations.rules_path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.directory), str(path))
        self.tool_instances, self.boundary_instances, self.restored_finals = [], [], []
        self.review_packets, self.primary_views = [], []

    def evidence(self, **values):
        row = {"case": CASES[self._testMethodName]["id"], "test": self._testMethodName,
               "oracle_sha256": ORACLE_SHA256, "cloud_model_calls": 0, **values}
        self.observations.append(row)
        print("WINDOW_AGENT_BUDGET_COMPACT_EVIDENCE " + json.dumps(row,
            ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def reviewer_client(self, transport):
        client = self.adapters.OpenAICompatibleJsonClient(provider="ai_ping", model_id=self.conversation.GLM_REVIEW_MODEL,
            url="https://offline.invalid/chat/completions", env_name="NAVREN_OFFLINE_KEY_UNUSED",
            keychain_service="NAVREN_OFFLINE_KEY_UNUSED", key_loader=lambda: "offline-fixture-placeholder",
            urlopen=transport, max_retries=0, log_path=str(self.directory / "review-provider.jsonl"))
        return self.conversation.GLMConversationReviewer(client)

    def drive(self, primary_transport, reviewer_transport, *, native_capability=True,
              boundary_capability=True, missing_key=False):
        primary = self.native.AIPingToolClient(urlopen=primary_transport,
            key_loader=(lambda: "") if missing_key else (lambda: "offline-fixture-placeholder"),
            log_path=str(self.directory / "native-provider.jsonl"))
        reviewer = self.reviewer_client(reviewer_transport)
        self.assertEqual(primary.provider, "ai_ping")
        self.assertEqual(primary.model_id, self.native.AIPING_PRIMARY_MODEL)
        self.assertEqual(primary.billing_mode, "paid")
        self.assertIs(type(reviewer), self.conversation.GLMConversationReviewer)
        self.assertEqual(reviewer.client.provider, "ai_ping")
        self.assertEqual(reviewer.client.model_id, self.conversation.GLM_REVIEW_MODEL)
        self.assertTrue(reviewer.supports_financial_metric_catalog)
        self.assertEqual(reviewer.billing_mode, "paid")
        # The frozen suite predates the separate financial-metric-catalog capability.
        # Keep its two compact-source booleans as the only negotiated matrix.
        primary.supports_financial_metric_catalog = False
        self.assertIs(primary.supports_financial_metric_catalog, False)
        primary.supports_compact_metric_sources = native_capability
        agent = self.conversation.ConversationAgent(primary, reviewer, timeout_seconds=105, max_tokens=100000)
        original_tools, original_boundary = self.bank_tools.BankTools, self.boundaries.CloudDataBoundary
        owner = self

        class WitnessedTools(original_tools):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                owner.tool_instances.append(self)

        class WitnessedBoundary(original_boundary):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.supports_compact_metric_sources = boundary_capability
                owner.boundary_instances.append(self)

            def restore_arguments(self, name, arguments):
                value = super().restore_arguments(name, arguments)
                if name == "answer_reviewed" and self is owner.boundary_instances[0]:
                    owner.restored_finals.append(copy.deepcopy(value))
                return value

            def review_packet(self, value, **options):
                projected = super().review_packet(value, **options)
                # 预算预留在 boundary 的 deepcopy 上投影，不能改变正式实例。
                if self is owner.boundary_instances[0]:
                    owner.review_packets.append(copy.deepcopy(projected))
                return projected

        with patch.object(self.conversation, "BankTools", WitnessedTools), \
             patch.object(self.conversation, "CloudDataBoundary", WitnessedBoundary):
            result = agent.process(self.g, self.w, "请解释这组假设算式，不执行操作。",
                                   request_id="independent-" + self._testMethodName)
        self.assertEqual(len(self.tool_instances), 1)
        self.assertEqual(len(self.boundary_instances), 1)
        with closing(self.g.bank.connect()) as db:
            rows = db.execute("SELECT details FROM audit WHERE workspace_id=? AND event='agent_turn_completed'",
                              (self.w,)).fetchall()
        self.assertEqual(len(rows), 1, "终结审计必须恰计一次")
        audit = json.loads(rows[0]["details"])
        for key in ("total_tokens", "usage_complete", "budget_tokens", "model_calls"):
            self.assertEqual(audit[key], result["agent_runtime"][key])
        self.last_audit = audit
        self.last_result = result
        self.primary_transport, self.review_transport = primary_transport, reviewer_transport
        for payload in primary_transport.requests:
            self.assertEqual(payload["model"], self.native.AIPING_PRIMARY_MODEL)
            names = {item.get("function", {}).get("name") for item in payload.get("tools", [])}
            if names and names <= {"answer_facts", "answer_reviewed"} and "answer_reviewed" in names:
                self.assertEqual(payload["tool_choice"], {
                    "type": "function", "function": {"name": "answer_reviewed"}})
        for payload in reviewer_transport.requests:
            self.assertEqual(payload["model"], self.conversation.GLM_REVIEW_MODEL)
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
        return result

    def approve_envelope(self, usage=None):
        usage = ORACLE["literal_valid_usage"] if usage is None else usage
        message = {"role": "assistant", "content": json.dumps({"verdict": "approve",
            "reason": "离线可信传输用于协议验收", "issues": []}, ensure_ascii=False)}
        return envelope(message, usage)

    def reviewed_terminal_envelope(self, original_text, usage, finish_reason="stop"):
        original_bytes = original_text.encode("utf-8")
        arguments = {"text": original_bytes.decode("utf-8"),
                     "claims": [], "draft_ids": [],
                     "followups": []}
        self.assertEqual(arguments["text"].encode("utf-8"), original_bytes)
        return envelope(tool_call("answer_reviewed", arguments), usage, finish_reason)

    def final_from_view(self, payload, *, count=1, mutate=None):
        view = json.loads(next(message["content"] for message in reversed(payload["messages"])
            if message["role"] == "tool" and json.loads(message["content"]).get("metrics")))
        self.primary_views.append(copy.deepcopy(view))
        record = view["metrics"][0]
        metric = record["metric"]
        expected = ORACLE["literal_hypothesis"]["expected"]
        for key, value in expected.items():
            if key == "kind":
                self.assertEqual(record[key], value)
            else:
                observed = metric[key]
                # The actual boundary exports signed formula identifiers using
                # its per-request reversible alias. Resolve only this display
                # precondition, using the witnessed boundary's existing map;
                # leave the model packet and every later mutation untouched.
                # Monetary values, kind and the frozen oracle stay literal.
                if key == "formula_id" and isinstance(observed, str):
                    observed = self.boundary_instances[0]._reverse.get(observed, observed)
                self.assertEqual(observed, value)
                self.assertIs(type(observed), type(value))
        fragment = ORACLE["literal_hypothesis"]["fragment"]
        claims = []
        text = "\n".join(fragment for _ in range(count))
        for i in range(count):
            claim = {key: copy.deepcopy(metric[key]) for key in FIELDS}
            source_key = "source_version_ref" if "source_version_ref" in metric else "source_versions"
            claim[source_key] = copy.deepcopy(metric[source_key])
            claim.update(text=fragment, claim_kind=record["kind"])
            if count > 1:
                claim["occurrence_start"] = i * (len(fragment) + 1)
            claims.append(claim)
        arguments = {"text": text, "claims": [{"text": fragment, "evidence_ids": [view["evidence_id"]]}],
                     "amount_claims": claims, "draft_ids": [], "followups": []}
        if mutate:
            mutate(arguments, view)
        return envelope(tool_call("answer_reviewed", arguments), ORACLE["literal_valid_usage"])

    def hypothesis_transports(self, *, count=1, mutate=None):
        p = OfflineTransport(envelope(tool_call("money_calculation", ORACLE["literal_hypothesis"]["inputs"]),
            ORACLE["literal_valid_usage"]), lambda payload: self.final_from_view(payload, count=count, mutate=mutate))
        r = OfflineTransport(self.approve_envelope())
        return p, r

    def publish(self, result):
        with closing(self.g.bank.connect()) as db:
            privacy = self.privacy_revision(db, self.w)
        personal = self.g.personal_data.revision(self.w)
        batches = self.tool_instances[0].metric_batches
        with self.g.response_guard(self.w, privacy, personal, result.get("knowledge_snapshot"),
                result.get("_answer_context"), market_snapshot=result.get("market_snapshot"),
                service_snapshot=result.get("service_snapshot"), metric_batches=batches) as db:
            self.g.operation_evidence.bind_answer(self.w, result, db)
            self.assertIs(self.g.assert_publication_current(), True)
        return len(batches)

    def assert_known(self, result, total):
        runtime = result["agent_runtime"]
        self.assertEqual(runtime["total_tokens"], total)
        self.assertIs(runtime["usage_complete"], True)
        self.assertEqual(runtime["budget_tokens"], total)
        self.assertEqual(sum(row["budget_tokens"] for row in runtime["model_calls"]), total)

    def known_lower_bound(self, runtime):
        # 只适配正式观测字段名称，不复制 usage 解析逻辑或用预计输入代替下界。
        if "known_tokens_lower_bound" in runtime:
            return runtime["known_tokens_lower_bound"]
        if "total_tokens_known" in runtime:
            return runtime["total_tokens_known"]
        if "known_token_lower_bound" in runtime:
            return runtime["known_token_lower_bound"]
        if "reviewer_tokens_known" in runtime:
            return runtime["primary_tokens_known"] + runtime["reviewer_tokens_known"]
        if all(row["stage"] == "primary" for row in runtime["model_calls"]):
            return runtime["primary_tokens_known"]
        self.fail("审核合法分项的真实下界没有正式可观测字段")

    def unknown_case(self, stage, outcome, name):
        variant = ORACLE["unknown_usage_variants"][name]
        usage = variant["usage"]
        message = {"role": "assistant", "content": "请说明需要比较的范围。"}
        if stage == "primary":
            response = envelope(message, usage, "length" if outcome == "failure" else "stop")
            p, r = OfflineTransport(response), OfflineTransport(self.approve_envelope())
            prefix, wanted_primary, wanted_review = 0, 1, 0
        else:
            p = OfflineTransport(self.reviewed_terminal_envelope(message["content"],
                ORACLE["literal_valid_usage"]))
            content = {"role": "assistant", "content": json.dumps({"verdict": "approve",
                "reason": "可信协议夹具", "issues": []}, ensure_ascii=False)}
            response = envelope(content, usage, "length" if outcome == "failure" else "stop")
            r = OfflineTransport(response)
            prefix, wanted_primary, wanted_review = 7, 1, 1
        result = self.drive(p, r)
        runtime = result["agent_runtime"]
        expected = ORACLE["expectations"]["unknown_stop"]
        self.assertTrue(result.get("isFallback"), "成功 envelope 的未知usage同样必须停止")
        self.assertIs(runtime["total_tokens"], expected["total_tokens"])
        self.assertIs(runtime["usage_complete"], expected["usage_complete"])
        self.assertEqual(runtime["budget_tokens"], expected["budget_tokens"])
        self.assertEqual(len(p.requests), wanted_primary)
        self.assertEqual(len(r.requests), wanted_review)
        self.assertEqual(len(runtime["model_calls"]), wanted_primary + wanted_review)
        lower = self.known_lower_bound(runtime)
        if "known_component_lower_bound" in variant:
            self.assertEqual(lower, prefix + variant["known_component_lower_bound"])
        else:
            self.assertGreaterEqual(lower, prefix + variant["known_component_lower_bound_minimum"])
        self.evidence(stage=stage, outcome=outcome, usage_variant=name, known_lower_bound=lower,
            budget_tokens=runtime["budget_tokens"], total_tokens=runtime["total_tokens"],
            primary_calls=len(p.requests), reviewer_calls=len(r.requests), runtime_failure=runtime["failure"])

    def known_failure_case(self, stage, failure):
        valid, failed = ORACLE["literal_valid_usage"], ORACLE["literal_failed_usage"]
        if stage == "primary":
            first = envelope(tool_call("account_summary", {}), valid)
            message = {"role": "assistant", "content": "应被拒绝的部分答复。"}
            if failure == "invalid_contract":
                message["role"] = "user"
            p = OfflineTransport(first, envelope(message, failed, "length" if failure == "length" else "stop"))
            r = OfflineTransport(self.approve_envelope())
            expected_calls = (2, 0)
        else:
            p = OfflineTransport(self.reviewed_terminal_envelope("请说明需要比较的范围。", valid))
            message = {"role": "assistant", "content": "not-json" if failure == "invalid_contract" else
                       json.dumps({"verdict": "approve", "reason": "部分答复", "issues": []}, ensure_ascii=False)}
            r = OfflineTransport(envelope(message, failed, "length" if failure == "length" else "stop"))
            expected_calls = (1, 1)
        result = self.drive(p, r)
        self.assertTrue(result.get("isFallback"))
        self.assert_known(result, 18)
        runtime = result["agent_runtime"]
        failed_rows = [row for row in runtime["model_calls"] if row["outcome"] == "failed"]
        self.assertEqual(len(failed_rows), 1)
        self.assertEqual(failed_rows[0]["usage"], failed)
        self.assertEqual(failed_rows[0]["budget_tokens"], 11)
        self.assertEqual((len(p.requests), len(r.requests)), expected_calls)
        self.evidence(stage=stage, failure=failure, total_tokens=18, failed_usage=failed_rows[0]["usage"],
                      primary_calls=len(p.requests), reviewer_calls=len(r.requests))

    def overrun_case(self, stage, outcome):
        message = {"role": "assistant", "content": "请说明需要比较的范围。"}
        if stage == "primary":
            usage = {"prompt_tokens": 100000, "completion_tokens": 1000, "total_tokens": 101000}
            p = OfflineTransport(envelope(message, usage, "length" if outcome == "failure" else "stop"))
            r = OfflineTransport(self.approve_envelope())
            expected_calls = (1, 0)
        else:
            p = OfflineTransport(self.reviewed_terminal_envelope("请说明需要比较的范围。",
                ORACLE["literal_valid_usage"]))
            usage = {"prompt_tokens": 100000, "completion_tokens": 993, "total_tokens": 100993}
            approval = {"role": "assistant", "content": json.dumps({"verdict": "approve",
                "reason": "可信夹具", "issues": []}, ensure_ascii=False)}
            r = OfflineTransport(envelope(approval, usage, "length" if outcome == "failure" else "stop"))
            expected_calls = (1, 1)
        result = self.drive(p, r)
        self.assertTrue(result.get("isFallback"))
        self.assert_known(result, ORACLE["expectations"]["known_overrun_preserved"]["total_tokens"])
        self.assertEqual((len(p.requests), len(r.requests)), expected_calls)
        self.evidence(stage=stage, outcome=outcome, total_tokens=101000, budget_tokens=101000,
                      primary_calls=len(p.requests), reviewer_calls=len(r.requests))

    def test_native_missing_key_is_genuinely_unsent_zero(self):
        p, r = OfflineTransport(), OfflineTransport()
        result = self.drive(p, r, missing_key=True)
        expected = ORACLE["expectations"]["zero_only_unsent"]
        self.assertEqual(len(p.requests), expected["transport_calls"])
        self.assertEqual(len(r.requests), 0)
        self.assertTrue(result.get("isFallback"))
        self.assert_known(result, expected["total_tokens"])
        row = result["agent_runtime"]["model_calls"][0]
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["budget_tokens"], 0)
        self.evidence(transport_calls=0, budget_tokens=0, total_tokens=0, attempts=row["attempts"])

    def test_sent_native_failure_cannot_self_assert_attempts_zero(self):
        error = self.adapters.ModelAdapterError("provider_unavailable",
            model_id="offline-native-transport", attempts=0)
        error.usage = copy.deepcopy(ORACLE["literal_valid_usage"])
        p, r = OfflineTransport(error), OfflineTransport()
        result = self.drive(p, r)
        expected = ORACLE["expectations"]["sent_known_count_once"]
        self.assertEqual(len(p.requests), expected["transport_calls"])
        self.assertTrue(result.get("isFallback"))
        self.assert_known(result, expected["total_tokens"])
        self.evidence(transport_calls=1, budget_tokens=result["agent_runtime"]["budget_tokens"],
                      attempts=result["agent_runtime"]["model_calls"][0].get("attempts"))

    def capability_case(self, primary_cap, boundary_cap):
        p, r = self.hypothesis_transports()
        result = self.drive(p, r, native_capability=primary_cap, boundary_capability=boundary_cap)
        self.assertFalse(result.get("isFallback"), result.get("error"))
        self.assert_known(result, 21)
        compact = primary_cap and boundary_cap
        native_schema = next(item["function"]["parameters"]["properties"]["amount_claims"]
                             for item in p.requests[0]["tools"] if item["function"]["name"] == "answer_reviewed")
        self.assertEqual(native_schema["maxItems"], 24)
        self.assertEqual("source_version_ref" in native_schema["items"]["properties"], compact)
        view = self.primary_views[0]
        self.assertEqual("source_version_sets" in view, compact)
        self.assertEqual("source_version_ref" in view["metrics"][0]["metric"], compact)
        self.assertEqual(len(r.requests), 1)
        glm_packet = json.loads(r.requests[0]["messages"][1]["content"])
        self.assertEqual(glm_packet, self.review_packets[0])
        self.assertEqual("source_version_sets" in glm_packet, compact)
        local_packet = self.boundary_instances[0].restore_output(glm_packet)
        contract = local_packet["amount_contract"]
        self.assertEqual(contract["status"], "validated")
        self.assertEqual(contract["amount_occurrence_count"], 1)
        restored = self.restored_finals[0]["amount_claims"][0]
        row = contract["amount_claims"][0]
        original = self.tool_instances[0].metrics.lookup(restored["metric_id"])
        self.assertEqual(row["metric"], original["metric"],
                         "完整缺项、假设和已排除事项必须到达真实GLM包")
        for field in FIELDS + ("source_versions",):
            self.assertIs(type(restored[field]), type(original["metric"][field]))
            self.assertEqual(restored[field], original["metric"][field])
            self.assertEqual(row["metric"][field], original["metric"][field])
            self.assertEqual(row["claim"][field], original["metric"][field])
        self.assertEqual(row["kind"], "hypothesis")
        self.assertEqual(restored["claim_kind"], "hypothesis")
        expected_review_claim = copy.deepcopy(restored)
        # GLM text uses the existing boundary's NFKC punctuation normalizer.
        # Compare the full claim after this display-only adaptation; every
        # typed metric/provenance field above remains exact and unchanged.
        expected_review_claim["text"] = unicodedata.normalize("NFKC", expected_review_claim["text"])
        self.assertEqual(local_packet["draft"]["amount_claims"][0], expected_review_claim)
        self.assertEqual(result["answer_contract"]["metric_ids"], [original["metric_id"]])
        batches = self.publish(result)
        self.evidence(primary_capability=primary_cap, boundary_capability=boundary_cap,
            compact=compact, original_registry_preserved=True, restored_fields=list(FIELDS),
            final_registry_metric_count=len(result["answer_contract"]["metric_ids"]),
            publication_guard_completed=True, validated_metric_batches=batches)

    def rejected_compact(self, mutate, code):
        p, r = self.hypothesis_transports(mutate=mutate)
        result = self.drive(p, r)
        self.assertTrue(result.get("isFallback"))
        self.assertEqual(result["error"]["code"], code)
        self.assertEqual(len(p.requests), 2)
        self.assertEqual(len(r.requests), 0, "拒绝后不得发出GLM付费请求")
        self.assertEqual(result.get("bank_actions"), [])
        self.assertIsNotNone(self.tool_instances[0].metrics.lookup(
            next(iter(self.tool_instances[0].metrics.metric_ids))))
        self.evidence(rejected_code=code, primary_calls=2, reviewer_calls=0)

    def field_case(self, field, mutation):
        def mutate(arguments, view):
            claim = arguments["amount_claims"][0]
            if mutation == "missing":
                del claim[field]
            elif mutation == "type":
                claim[field] = [claim[field]]
            elif field == "value_minor":
                claim[field] = 13001
            elif claim[field] is None:
                claim[field] = "2026-10-01"
            else:
                claim[field] = "independent-forged-value"
        code = ORACLE["expectations"]["structure_rejected" if mutation == "missing" else "alias_rejected"]
        self.rejected_compact(mutate, code)

    def extra_ref_case(self, name):
        def mutate(arguments, view):
            claim = arguments["amount_claims"][0]
            if name == "kind_missing":
                del claim["claim_kind"]
            elif name == "kind_type":
                claim["claim_kind"] = ["hypothesis"]
            elif name == "kind_value":
                claim["claim_kind"] = "recorded_fact"
            elif name == "unknown_ref":
                claim["source_version_ref"] = "vset_ffffffff"
            elif name == "mixed_sources":
                claim["source_versions"] = copy.deepcopy(view["source_version_sets"][claim["source_version_ref"]])
            elif name == "catalog_injection":
                arguments["source_version_sets"] = copy.deepcopy(view["source_version_sets"])
            elif name == "old_boundary":
                fresh = self.boundaries.CloudDataBoundary(identities=(self.w,))
                with self.assertRaises(self.boundaries.BoundaryError) as caught:
                    fresh.restore_arguments("answer_reviewed", copy.deepcopy(arguments))
                self.assertEqual(caught.exception.code, "BOUNDARY_UNKNOWN_ALIAS")
                claim["metric_id"] = "obj_ffffffff"
        expected = CASES[self._testMethodName]["expectation"]
        self.rejected_compact(mutate, ORACLE["expectations"][expected])

    def amount_limit_case(self, compact, count):
        p, r = self.hypothesis_transports(count=count)
        result = self.drive(p, r, native_capability=compact, boundary_capability=True)
        schema = next(item["function"]["parameters"]["properties"]["amount_claims"]
                      for item in p.requests[0]["tools"] if item["function"]["name"] == "answer_reviewed")
        self.assertEqual(schema["maxItems"], 24)
        submitted = next(json.loads(message["content"]) for message in reversed(p.requests[-1]["messages"])
                         if message["role"] == "tool" and json.loads(message["content"]).get("metrics"))
        self.assertTrue(submitted["metrics"])
        if count == 25:
            self.assertTrue(result.get("isFallback"))
            self.assertEqual(result["error"]["code"], "BOUNDARY_UNKNOWN_STRUCTURE")
            self.assertEqual(len(r.requests), 0)
            self.assertEqual(self.restored_finals, [])
            self.evidence(claims_submitted=25, rejected_code=result["error"]["code"],
                          reviewer_calls=0, silently_truncated=False, compact=compact)
            return
        self.assertFalse(result.get("isFallback"), result.get("error"))
        self.assertEqual(len(self.restored_finals[0]["amount_claims"]), 24)
        glm_packet = json.loads(r.requests[0]["messages"][1]["content"])
        expanded = self.boundary_instances[0].restore_output(glm_packet)
        contract = expanded["amount_contract"]
        self.assertEqual(contract["amount_occurrence_count"], 24)
        self.assertEqual(len(contract["amount_claims"]), 24)
        self.assertEqual(len(expanded["draft"]["amount_claims"]), 24)
        self.assertEqual(result["reply"].count("130元"), 24)
        self.assertEqual(len(r.requests), 1)
        self.publish(result)
        self.evidence(claims_submitted=24, claims_validated=24, amount_occurrence_count=24,
                      reviewer_calls=1, publication_guard_completed=True, compact=compact)


def add_case(name, helper, *arguments):
    def test(self):
        return getattr(self, helper)(*arguments)
    test.__name__ = name
    setattr(AgentBudgetCompactContracts, name, test)


for stage in ("primary", "reviewer"):
    for outcome in ("success", "failure"):
        for variant in ORACLE["unknown_usage_variants"]:
            add_case(f"test_unknown_{stage}_{outcome}_{variant}", "unknown_case", stage, outcome, variant)
    for failure in ("length", "invalid_contract"):
        add_case(f"test_known_{stage}_{failure}", "known_failure_case", stage, failure)
    for outcome in ("success", "failure"):
        add_case(f"test_overrun_{stage}_{outcome}", "overrun_case", stage, outcome)
for primary_cap in (False, True):
    for boundary_cap in (False, True):
        add_case(f"test_capabilities_{int(primary_cap)}{int(boundary_cap)}_whole_chain",
                 "capability_case", primary_cap, boundary_cap)
for field in FIELDS:
    for mutation in ("missing", "type", "value"):
        add_case(f"test_compact_{field}_{mutation}", "field_case", field, mutation)
for name in ("kind_missing", "kind_type", "kind_value", "unknown_ref", "mixed_sources",
             "catalog_injection", "old_boundary"):
    add_case("test_compact_" + name, "extra_ref_case", name)
for compact in (False, True):
    for count in (24, 25):
        add_case(f'test_amount_limit_{"compact" if compact else "full"}_{count}',
                 "amount_limit_case", compact, count)

if set(unittest.defaultTestLoader.getTestCaseNames(AgentBudgetCompactContracts)) != set(CASES):
    raise RuntimeError("frozen_case_collection_mismatch")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AgentBudgetCompactContracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    complete = (result.wasSuccessful() and result.testsRun == ORACLE["expected_test_count"]
                and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses)
    record = {"tests_run": result.testsRun, "expected_tests": ORACLE["expected_test_count"],
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
              "unexpected_successes": len(result.unexpectedSuccesses), "complete": complete,
              "planned_exit_code": 0 if complete else 1, "oracle_sha256": ORACLE_SHA256}
    if hasattr(AgentBudgetCompactContracts, "output"):
        (AgentBudgetCompactContracts.output / "actual-counts.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print("WINDOW_AGENT_BUDGET_COMPACT_COUNTS " + json.dumps(record), flush=True)
    sys.exit(record["planned_exit_code"])
