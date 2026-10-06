"""仅检查本次四主体的 MCP 握手、读取和拒绝边界，无模型调用。"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from .contract import SCHEMA_VERSION, TOOL_NAMES


async def check(database: Path):
    root = Path(__file__).resolve().parents[1]
    rows, sample_ids = [], {}
    tool_schema_checked, calls = False, 0
    for subject in ("tycoon", "grain_farmer", "working_landlady", "retired_entrepreneur"):
        params = StdioServerParameters(command=sys.executable, args=["-m", "financial_mcp.server"], cwd=root,
            env={"NAVREN_FINANCIAL_DB": str(database), "NAVREN_FINANCIAL_SUBJECT_ID": subject,
                 "PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as session:
                info = await session.initialize()
                if not tool_schema_checked:
                    listing = await session.list_tools()
                    assert {t.name for t in listing.tools} == set(TOOL_NAMES), "tool registry mismatch"
                    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint
                               for t in listing.tools), "read-only annotations absent"
                    assert all(not {"subject_id", "workspace", "database", "database_path", "sql"} &
                               set(t.input_schema.get("properties", {})) for t in listing.tools), "identity exposed to model"
                    tool_schema_checked = True
                record = {"subject_id": subject, "server_version": info.server_info.version, "transport": "stdio"}
                for operation in ("list_accounts", "list_cards", "list_insurance", "get_financial_summary"):
                    response = await session.call_tool("financial_" + operation, {})
                    assert not response.is_error, (subject, operation, "read failed")
                    data = response.structured_content
                    if data is None:
                        data = json.loads(response.content[0].text)
                    assert (data["schema_version"] == SCHEMA_VERSION and data["subject_id"] == subject
                            and data["source"] == "navren_synthetic" and not data.get("error")), "invalid envelope"
                    calls += 1
                    if operation != "get_financial_summary":
                        items = data["data"]["items"]
                        record[operation.removeprefix("list_")] = len(items)
                        assert items and all(i["subject_id"] == subject for i in items), "ownership mismatch"
                        if operation == "list_accounts":
                            sample_ids[subject] = items[0]["account_id"]
                    else:
                        summary = data["data"]
                        assert summary["net_worth_minor"] == summary["total_assets_minor"] - summary["liabilities_minor"]
                        assert summary["insurance_sum_excluded"] and summary["credit_limits_excluded"]
                        record.update(summary_identity_valid=True, dataset_version=data["dataset_version"], as_of=data["as_of"])
                if subject == "grain_farmer":
                    denied = await session.call_tool("financial_get_account_overview", {"account_id": sample_ids["tycoon"]})
                    assert denied.is_error, "cross-subject access succeeded"
                    invalid = await session.call_tool("financial_list_transactions", {"limit": 201})
                    assert invalid.is_error, "pagination exceeded"
                    calls += 2
                    record.update(cross_subject_denied=True, pagination_limit_enforced=True)
                rows.append(record)
    return {"mcp_initialize_list_call": True, "read_only_tools": len(TOOL_NAMES),
            "subject_not_in_model_schema": True, "tool_calls": calls, "model_calls": 0, "subjects": rows,
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest()}


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m financial_mcp.check_connection DATABASE REPORT_JSON")
    database, output = Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2])
    started = time.monotonic()
    result = asyncio.run(asyncio.wait_for(check(database), timeout=90))
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
