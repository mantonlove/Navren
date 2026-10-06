"""在隔离 SDK 环境执行一次实际 MCP 调用，供主应用无 SDK 环境消费。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .contract import TOOL_NAMES, FinancialMCPError
from .server import ServerConfig


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FinancialMCPError("INVALID_REQUEST", "请求不能包含重复字段。")
        result[key] = value
    return result


async def invoke(request: dict):
    if not isinstance(request, dict) or set(request) - {"action", "name", "arguments"}:
        raise FinancialMCPError("INVALID_REQUEST", "请求字段无效。")
    action = request.get("action", "call_tool")
    if action not in ("list_tools", "call_tool"):
        raise FinancialMCPError("INVALID_REQUEST", "未开放此 MCP 操作。")
    if action == "call_tool":
        if request.get("name") not in TOOL_NAMES:
            raise FinancialMCPError("TOOL_NOT_ALLOWED", "未开放此财务工具。")
        if not isinstance(request.get("arguments", {}), dict):
            raise FinancialMCPError("INVALID_REQUEST", "工具参数必须为对象。")
        if {"subject_id", "workspace", "database", "database_path", "sql"} & set(request.get("arguments", {})):
            raise FinancialMCPError("FORBIDDEN_ARGUMENT", "模型不能选择身份、路径或 SQL。")
    config = ServerConfig.from_environment()
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "financial_mcp.server"], cwd=root,
        env={"NAVREN_FINANCIAL_DB": str(config.database), "NAVREN_FINANCIAL_SUBJECT_ID": config.subject_id,
             "PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=30) as session:
            await session.initialize()
            if action == "list_tools":
                tools = await session.list_tools()
                return {"ok": True, "tools": [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in tools.tools]}
            result = await session.call_tool(request["name"], request.get("arguments", {}))
            if result.is_error:
                texts = [item.text for item in result.content if item.type == "text"]
                message = " ".join(texts)[:300]
                stable_code = re.search(r"(?:^|\s)([A-Z][A-Z0-9_]{1,63}):", message)
                return {"ok": False, "error": {"code": stable_code.group(1) if stable_code else "MCP_TOOL_FAILED",
                                               "message": message}}
            data = result.structured_content
            if data is None:
                texts = [item.text for item in result.content if item.type == "text"]
                if len(texts) != 1:
                    raise FinancialMCPError("INVALID_RESPONSE", "财务工具未返回结构化结果。")
                data = json.loads(texts[0])
            return {"ok": True, "data": data}


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if not raw or len(raw) > 65536:
            raise FinancialMCPError("INVALID_REQUEST", "请求大小无效。")
        request = json.loads(raw, object_pairs_hook=unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
        result = asyncio.run(asyncio.wait_for(invoke(request), timeout=40))
    except Exception as error:
        result = {"ok": False, "error": {"code": getattr(error, "code", "MCP_UNAVAILABLE"),
                                         "message": "财务 MCP 请求未完成。"}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
