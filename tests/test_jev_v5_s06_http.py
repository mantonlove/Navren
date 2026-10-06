"""S06-v2 no-RAG zero-paid check through the real local /api/agent handler."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_runtime.conversation import ConversationAgent
from agent_runtime.jev_client import JevError
from local_knowledge.runtime import EvidenceRuntime
from local_knowledge.store import KnowledgeError
from server import ProxyHandler, create_server
from test_bank_integration import Browser
from test_native_conversation import Reviewer, ScriptedPrimary, call


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "outputs/jev-ab/experiment-v5/s06-v2-no-rag-contract.json"
RESULT = ROOT / "outputs/jev-ab/experiment-v5/s06-v2-no-rag-http-result.json"
JEV_RESULT = ROOT / "outputs/jev-ab/experiment-v5/s06-v2-no-rag-jev-http-result.json"


class S06NoRagHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="navren-jev-v5-http-")
        self.addCleanup(self.temporary.cleanup)
        self.quiet = patch.object(ProxyHandler, "log_message", lambda *args: None)
        self.quiet.start()
        self.addCleanup(self.quiet.stop)
        self.server = create_server(0, Path(self.temporary.name) / "bank.sqlite3")
        self.server.daemon_threads = False
        self.server.evidence_runtime.close()

        self.reader_calls = []

        def offline_reader(provider, source_id, url, **kwargs):
            self.reader_calls.append({"source_id": source_id, "url": url})
            raise KnowledgeError("source_unavailable")

        class ContractRuntime(EvidenceRuntime):
            def prepare_round(inner, text):
                try:
                    prepared = super(ContractRuntime, inner).prepare_round(text)
                    prepared.report.update(fixture_id=inner.fixture_id,
                                           fixture_sha256=inner.fixture_sha256)
                    return prepared
                except Exception as error:
                    inner.prepare_error = {"type": type(error).__name__, "message": str(error)}
                    raise

        contract_bytes = CONTRACT.read_bytes()
        self.contract = json.loads(contract_bytes)
        self.contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
        self.runtime = ContractRuntime(self.server.finance.knowledge_provider,
            reader=offline_reader, background=False, timeout=0.25)
        self.runtime.fixture_id = self.contract["fixture_id"]
        self.runtime.fixture_sha256 = self.contract_sha256
        self.server.evidence_runtime = self.runtime

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.browser = Browser(f"http://127.0.0.1:{self.server.server_port}")
        self.session, _ = self.browser.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_same_no_rag_contract_reaches_model_and_review_through_http(self):
        public_reads = call("public_products", {"query": "风险"})
        public_reads["tool_calls"] += call("knowledge_status", {})["tool_calls"]
        class CapturingAgent(ConversationAgent):
            errors = []

            def process(inner, *args, **kwargs):
                try:
                    return super(CapturingAgent, inner).process(*args, **kwargs)
                except Exception as error:
                    CapturingAgent.errors.append({"type": type(error).__name__, "message": str(error)})
                    raise

        CapturingAgent.errors.clear()
        primary = ScriptedPrimary(
            public_reads,
            call("knowledge_search", {"query": "风险", "facets": ["risk"]}),
            call("answer_reviewed", {
                "text": "当前公共 RAG 未接入；本轮没有可核验的公开产品证据，不能据此判断当前在售或建议购买。",
                "claims": [], "draft_ids": [], "followups": []}),
        )
        reviewer = Reviewer()
        self.server.conversation_agent = CapturingAgent(primary, reviewer)
        before_cash = self.server.finance.overview(self.session["workspace_id"])["total_cash_minor"]

        status, result, _ = self.browser.call("/api/agent", "POST", {
            "text": "检查公开知识库、微众理财产品和风险证据是否可用；不可用就说明，不要建议购买。"})
        self.assertEqual(status, 200, {"result": result, "agent_errors": CapturingAgent.errors,
                                      "prepare_error": getattr(self.runtime, "prepare_error", None)})
        self.assertFalse(result.get("isFallback"), result.get("agent_runtime"))
        preparation = result["knowledge_preparation"]
        self.assertEqual(preparation["fixture_id"], self.contract["fixture_id"])
        self.assertEqual(preparation["fixture_sha256"], self.contract_sha256)
        self.assertEqual(preparation["status"], "gap")
        self.assertEqual(len(self.reader_calls), 1)

        packet_evidence = reviewer.packets[0]["evidence"]
        by_tool = {item["tool"]: item["data"] for item in packet_evidence}
        for tool_name in ("knowledge_status", "public_products", "knowledge_search"):
            self.assertIn(tool_name, by_tool)
        expected_status = self.contract["expected"]["agent_visible_knowledge_status"]
        observed_status = {key: by_tool["knowledge_status"].get(key) for key in expected_status}
        self.assertEqual(observed_status, expected_status)
        expected_products = self.contract["expected"]["agent_visible_public_products"]
        observed_products = {key: by_tool["public_products"].get(key) for key in expected_products}
        self.assertEqual(observed_products, expected_products)
        self.assertFalse(by_tool["knowledge_search"]["ready"])
        self.assertEqual(by_tool["knowledge_search"]["status"], "unavailable")
        self.assertEqual(by_tool["knowledge_search"]["hits"], [])
        self.assertEqual(result["bank_actions"], [])
        self.assertEqual(self.server.finance.overview(self.session["workspace_id"])["total_cash_minor"], before_cash)

        report = {
            "fixture_id": self.contract["fixture_id"],
            "fixture_sha256": self.contract_sha256,
            "status": "passed",
            "test_scope": "real local /api/agent HTTP handler with scripted primary and reviewer",
            "external_network_requests": 0,
            "local_http_status": status,
            "knowledge_preparation_status": preparation["status"],
            "reader_calls": len(self.reader_calls),
            "observed": {"knowledge_status": observed_status,
                         "public_products": observed_products,
                         "knowledge_search": {"ready": by_tool["knowledge_search"]["ready"],
                                              "status": by_tool["knowledge_search"]["status"],
                                              "hits": by_tool["knowledge_search"]["hits"]}},
            "fallback": bool(result.get("isFallback")),
            "bank_actions": result["bank_actions"],
            "paid_model_calls": 0,
        }
        RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def test_active_jev_and_no_rag_contract_reach_same_http_handler_without_paid_calls(self):
        from agent_runtime.jev_agent import JevConversationAgent

        class MeasuredPrimary(ScriptedPrimary):
            def request_input_bound(inner, messages, tools):
                body = {"messages": messages, "tools": tools}
                return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))

        class ScriptedJev:
            def __init__(inner):
                inner.states = []

            def evaluate(inner, state, questions):
                inner.states.append(state)
                skill = "product-research"
                probabilities = {name: float(name == skill)
                                 for name in questions["first_skill"]["criteria"]}
                return {"first_skill": {"type": "choice", "choice": skill,
                        "confidence": 0.99, "probabilities": probabilities}}, {
                    "request_sent": False, "outcome": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                    "billing_mode": "unpaid_fixture"}

        public_reads = call("public_products", {"query": "风险"})
        public_reads["tool_calls"] += call("knowledge_status", {})["tool_calls"]
        primary = MeasuredPrimary(
            public_reads,
            call("knowledge_search", {"query": "风险", "facets": ["risk"]}),
            call("answer_reviewed", {
                "text": "当前公共 RAG 未接入；本轮没有可核验的公开产品证据，不能据此判断当前在售或建议购买。",
                "claims": [], "draft_ids": [], "followups": []}),
        )
        judge = ScriptedJev()
        reviewer = Reviewer()
        self.server.conversation_agent = JevConversationAgent(primary, reviewer,
            jev_mode="active", judge=judge)
        before_cash = self.server.finance.overview(self.session["workspace_id"])["total_cash_minor"]

        status, result, _ = self.browser.call("/api/agent", "POST", {
            "text": "检查公开知识库、微众理财产品和风险证据是否可用；不可用就说明，不要建议购买。"})
        self.assertEqual(status, 200, result)
        self.assertFalse(result.get("isFallback"), result.get("agent_runtime"))
        preparation = result["knowledge_preparation"]
        self.assertEqual(preparation["fixture_id"], self.contract["fixture_id"])
        self.assertEqual(preparation["fixture_sha256"], self.contract_sha256)
        self.assertEqual(result["agent_runtime"]["jev"]["mode"], "active")
        selection = result["agent_runtime"]["jev"]["selection"]
        self.assertTrue(selection["applied"], selection)
        self.assertEqual(selection["skill"], "product-research")
        self.assertEqual(judge.states, [{"current_request":
            "检查公开知识库、微众理财产品和风险证据是否可用;不可用就说明,不要建议购买。"}])
        self.assertTrue(any("产品研究" in message.get("content", "")
                            for message in primary.messages[0] if message.get("role") == "system"))
        self.assertEqual(result["bank_actions"], [])
        self.assertEqual(self.server.finance.overview(self.session["workspace_id"])["total_cash_minor"], before_cash)
        self.assertEqual(len(self.reader_calls), 1)

        report = {
            "fixture_id": self.contract["fixture_id"],
            "fixture_sha256": self.contract_sha256,
            "status": "passed",
            "test_scope": "real local /api/agent HTTP handler with active Jev and scripted primary/reviewer",
            "external_network_requests": 0,
            "local_http_status": status,
            "knowledge_preparation_status": preparation["status"],
            "jev_selection": selection,
            "jev_fixture_usage": result["agent_runtime"].get("jev_tokens_known"),
            "reader_calls": len(self.reader_calls),
            "fallback": bool(result.get("isFallback")),
            "bank_actions": result["bank_actions"],
            "paid_model_calls": 0,
        }
        JEV_RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def test_active_jev_unknown_usage_stops_before_primary_on_http_path(self):
        from agent_runtime.jev_agent import JevConversationAgent

        class MeasuredPrimary(ScriptedPrimary):
            def request_input_bound(inner, messages, tools):
                return len(json.dumps({"messages": messages, "tools": tools},
                                      ensure_ascii=False).encode("utf-8"))

        class TimedOutJev:
            def evaluate(inner, state, questions):
                raise JevError("timeout", {"request_sent": True, "usage": None})

        primary = MeasuredPrimary(call("account_summary", {}))
        self.server.conversation_agent = JevConversationAgent(primary, Reviewer(),
            jev_mode="active", judge=TimedOutJev())
        before_cash = self.server.finance.overview(self.session["workspace_id"])["total_cash_minor"]
        status, result, _ = self.browser.call("/api/agent", "POST", {"text": "查询我的现金余额"})

        self.assertEqual(status, 200, result)
        self.assertTrue(result.get("isFallback"))
        self.assertEqual(result["error"]["code"], "MODEL_USAGE_UNVERIFIED")
        self.assertEqual(primary.messages, [])
        self.assertEqual(self.server.finance.overview(self.session["workspace_id"])["total_cash_minor"], before_cash)

    def test_http_final_round_rejects_a_new_read_tool(self):
        class ProtocolPaidPrimary(ScriptedPrimary):
            billing_mode = "paid"

            def request_input_bound(inner, messages, tools):
                return len(json.dumps({"messages": messages, "tools": tools},
                                      ensure_ascii=False).encode("utf-8"))

        primary = ProtocolPaidPrimary(call("knowledge_status", {}))
        reviewer = Reviewer()
        self.server.conversation_agent = ConversationAgent(primary, reviewer, max_rounds=1)
        status, result, _ = self.browser.call("/api/agent", "POST", {"text": "查询公共资料状态"})

        self.assertEqual(status, 200, result)
        self.assertTrue(result.get("isFallback"))
        self.assertEqual(result["error"]["code"], "INVALID_FINAL_CONTRACT")
        self.assertEqual(len(primary.messages), 1)
        self.assertEqual(reviewer.packets, [])


if __name__ == "__main__":
    unittest.main()
