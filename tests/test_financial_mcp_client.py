"""离线消费端合同测试；不读取密钥、不访问模型或修改财务数据。"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from financial_mcp.contract import TOOL_NAMES
from agent_runtime.bank_tools import FINANCIAL_MCP_TOOL_NAMES, native_tools
from agent_runtime.data_boundary import CloudDataBoundary
from agent_runtime.financial_mcp_client import (
    FinancialMCPClient, FinancialMCPError, _PROVIDER_INPUTS, tool_specs,
)


def tool_list_response():
    tools = []
    for name in TOOL_NAMES:
        properties, required = _PROVIDER_INPUTS[name]
        schema_properties = {}
        for field, types in properties.items():
            ordered = sorted(types)
            schema_properties[field] = ({"type": ordered[0]} if len(ordered) == 1 else
                {"anyOf": [{"type": value} for value in ordered]})
        tools.append({"name": name, "description": "local test contract",
            "inputSchema": {"type": "object", "properties": schema_properties, "required": sorted(required)},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}})
    return {"ok": True, "tools": tools}


def envelope(subject="tycoon", data=None):
    return {"schema_version": "navren.financial.v1", "subject_id": subject,
        "source": "navren_synthetic", "dataset_version": "navren-financial-test",
        "as_of": "2026-10-03", "data": data or {"items": [], "total": 0, "has_more": False},
        "evidence": {"source_database_sha256": "a" * 64, "coverage_start": "2016-10-04",
            "coverage_end": "2026-10-03", "bank_integration": False, "synthetic_contracts": True,
            "valuation": "synthetic_not_real_NAV", "dataset_state": "historical_read_only"}}


class FinancialMCPClientTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "financial.sqlite3"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE subjects(subject_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL)")
            db.execute("INSERT INTO subjects VALUES(?,?)", ("tycoon", "trusted-workspace"))
            db.commit()
        python = self.root / "outputs/financial-mcp/venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        python.chmod(0o755)
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, response_for_call=None):
        def run(_command, **kwargs):
            request = json.loads(kwargs["input"])
            self.requests.append((request, kwargs["env"]))
            if request["action"] == "list_tools":
                response = tool_list_response()
            else:
                response = response_for_call(request) if response_for_call else {"ok": True, "data": envelope()}
            return SimpleNamespace(returncode=0 if response.get("ok") else 1,
                                    stdout=json.dumps(response), stderr="")
        return run

    def client(self, runner=None):
        return FinancialMCPClient(self.database, "tycoon", "trusted-workspace", root=self.root,
                                  runner=runner or self.runner())

    def test_discovery_registers_only_read_only_contract_and_passes_fixed_subject(self):
        client = self.client()
        definitions = client.tool_definitions()
        self.assertEqual(set(client.tool_names), set(TOOL_NAMES))
        self.assertEqual({row["function"]["name"] for row in definitions}, set(TOOL_NAMES))
        self.assertTrue(all("subject_id" not in row["function"]["parameters"]["properties"] for row in definitions))
        client.call("financial_list_accounts", {})
        request, environment = self.requests[-1]
        self.assertNotIn("subject_id", request["arguments"])
        self.assertEqual(environment["NAVREN_FINANCIAL_SUBJECT_ID"], "tycoon")
        self.assertEqual(environment["NAVREN_FINANCIAL_DB"], str(self.database.resolve()))

    def test_agent_hides_tools_while_disabled_and_boundary_projects_them_when_enabled(self):
        disabled = {row["function"]["name"] for row in native_tools(financial_mcp_tools=())}
        enabled = {row["function"]["name"] for row in native_tools(financial_mcp_tools=TOOL_NAMES)}
        self.assertFalse(disabled & FINANCIAL_MCP_TOOL_NAMES)
        self.assertEqual(enabled & FINANCIAL_MCP_TOOL_NAMES, FINANCIAL_MCP_TOOL_NAMES)
        projected_schemas = CloudDataBoundary(task="account").native_tools(
            native_tools(financial_mcp_tools=TOOL_NAMES))
        self.assertEqual({row["function"]["name"] for row in projected_schemas} & FINANCIAL_MCP_TOOL_NAMES,
                         FINANCIAL_MCP_TOOL_NAMES)

    def test_missing_explicit_database_configuration_stays_disabled(self):
        with self.assertRaises(FinancialMCPError) as disabled:
            FinancialMCPClient.from_workspace(SimpleNamespace(), "trusted-workspace", environ={})
        self.assertEqual(disabled.exception.code, "CONFIGURATION_REQUIRED")

    def test_subject_mismatch_and_model_supplied_control_fields_fail_closed(self):
        with self.assertRaises(FinancialMCPError) as mismatch:
            FinancialMCPClient(self.database, "tycoon", "other-workspace", root=self.root, runner=self.runner())
        self.assertEqual(mismatch.exception.code, "SUBJECT_MAPPING_MISMATCH")
        client = self.client()
        with self.assertRaises(FinancialMCPError):
            client.call("financial_list_accounts", {"subject_id": "grain_farmer"})

    def test_provider_error_is_an_error_not_an_empty_account_list(self):
        def response(request):
            return {"ok": False, "error": {"code": "ENTITY_NOT_ACCESSIBLE", "message": "denied"}}
        client = self.client(self.runner(response))
        with self.assertRaises(FinancialMCPError) as failed:
            client.call("financial_get_account_overview", {"account_id": "foreign::account:1"})
        self.assertEqual(failed.exception.code, "ENTITY_NOT_ACCESSIBLE")
        self.assertNotIn("data", self.requests[-1][0])

    def test_outbound_envelope_removes_subject_and_database_hash(self):
        response_data = {"items": [{"subject_id": "tycoon", "account_id": "tycoon::account:1",
            "connection_id": "tycoon::connection:1", "name": "synthetic", "account_type": "bank_settlement",
            "currency": "CNY", "scope": "personal", "source_pool_id": "internal", "opened_on": "2016-10-04",
            "opening_balance_minor": 100, "balance_minor": 50, "status": "active", "details": {"private": "drop"}}],
            "total": 1, "has_more": False}
        client = self.client(self.runner(lambda _request: {"ok": True, "data": envelope(data=response_data)}))
        result = client.call("financial_list_accounts", {})
        serialized = json.dumps(result)
        self.assertNotIn("subject_id", serialized)
        self.assertNotIn("source_database_sha256", serialized)
        self.assertNotIn("source_pool_id", serialized)
        self.assertNotIn("private", serialized)
        self.assertEqual(result["data"]["items"][0]["balance_minor"], 50)
        outbound = CloudDataBoundary(task="account").tool_result(
            "financial_list_accounts", {"evidence_id": "evidence-1", "data": result, "facts": []})
        outbound_text = json.dumps(outbound, ensure_ascii=False)
        self.assertNotIn("subject_id", outbound_text)
        self.assertNotIn("source_database_sha256", outbound_text)
        self.assertNotIn("tycoon::account:1", outbound_text)

    def test_outbound_transaction_counterparty_is_aliased(self):
        row = {"subject_id": "tycoon", "transaction_id": "tx_1", "account_id": "acc_1",
            "occurred_at": "2026-10-01T08:00:00", "booked_on": "2026-10-01", "status": "posted",
            "signed_minor": -100, "currency": "CNY", "counterparty": "私人收款人甲", "category": "transfer",
            "kind": "transfer", "scope": "personal", "card_id": None, "contract_id": None,
            "mandate_id": None, "original_transaction_id": None, "transfer_group_id": None,
            "economic_event_id": "event_1", "expense_minor": 0}
        data = {"items": [row], "total": 1, "limit": 1, "offset": 0,
                "has_more": False, "coverage_partial": False}
        client = self.client(self.runner(lambda _request: {"ok": True, "data": envelope(data=data)}))
        result = client.call("financial_list_transactions", {"limit": 1})
        outbound = CloudDataBoundary(task="account").tool_result(
            "financial_list_transactions", {"data": result, "facts": []})
        serialized = json.dumps(outbound, ensure_ascii=False)
        self.assertNotIn("私人收款人甲", serialized)
        self.assertNotIn("tycoon", serialized)


if __name__ == "__main__":
    unittest.main()
