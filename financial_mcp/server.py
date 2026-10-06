"""官方 SDK stdio 财务 MCP；身份来自宿主启动配置，模型不能指定用户。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import wraps
import logging
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .contract import (
    FinancialMCPError, OPERATIONS, SCHEMA_VERSION, bounded_integer,
    calendar_date, date_range, identifier,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerConfig:
    database: Path
    subject_id: str

    @classmethod
    def from_environment(cls):
        database = os.environ.get("NAVREN_FINANCIAL_DB")
        subject = os.environ.get("NAVREN_FINANCIAL_SUBJECT_ID")
        if not database or not subject:
            raise FinancialMCPError("CONFIGURATION_REQUIRED", "宿主须配置财务库路径与授权主体。")
        path = Path(database).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FinancialMCPError("DATABASE_UNAVAILABLE", "财务数据库文件不可用。")
        return cls(path, identifier(subject, required=True))


def build_server(config: ServerConfig | None = None):
    config = config or ServerConfig.from_environment()
    # 此模块只依赖服务接口，不拼接 SQL、不通过模型参数选择文件或主体。
    from financial_data_service.service import FinancialDataService
    service = FinancialDataService(str(config.database))
    server = MCPServer(
        "navren-financial-data", version="1.0.0", log_level="WARNING",
        instructions="查询已授权演示用户的财务事实。金额单位为人民币分；个人数据均为合成。"
                     "保额与授信额度不属于可用现金；此服务只读，不能执行转账或申赎。",
    )
    annotation = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def read_tool(function):
        @wraps(function)
        async def handled(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except FinancialMCPError as error:
                raise ToolError(str(error)) from None
        return server.tool(annotations=annotation)(handled)

    async def query(operation: str, arguments: dict):
        if operation not in OPERATIONS:
            raise FinancialMCPError("TOOL_NOT_ALLOWED", "未开放此财务工具。")
        try:
            result = await asyncio.to_thread(service.query, config.subject_id, operation, arguments)
        except Exception as error:
            # 不把 SQL、本机路径、原始私有数据或凭据写进协议错误。
            code = getattr(error, "code", "SERVICE_QUERY_FAILED")
            if not isinstance(code, str) or not code.replace("_", "").isalnum():
                code = "SERVICE_QUERY_FAILED"
            log.warning("financial query failed operation=%s code=%s", operation, code)
            raise FinancialMCPError(code, "财务查询未完成，请检查所选记录及数据服务。") from None
        if not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION:
            raise FinancialMCPError("INVALID_RESPONSE", "财务服务返回的版本无效。")
        if result.get("subject_id") != config.subject_id:
            raise FinancialMCPError("SUBJECT_MISMATCH", "财务记录不属于当前授权主体。")
        if result.get("source") != "navren_synthetic" or "data" not in result or "dataset_version" not in result:
            raise FinancialMCPError("INVALID_RESPONSE", "财务服务返回的数据来源或结构无效。")
        if result.get("error"):
            error = result["error"]
            code = error.get("code", "SERVICE_QUERY_FAILED") if isinstance(error, dict) else "SERVICE_QUERY_FAILED"
            if not isinstance(code, str) or not code.replace("_", "").isalnum():
                code = "SERVICE_QUERY_FAILED"
            raise FinancialMCPError(code, "财务查询被拒绝或数据暂不可用。")
        return result

    @read_tool
    async def financial_list_connections() -> dict:
        """查看当前用户接入的银行、保险、证券及支付机构。"""
        return await query("list_connections", {})

    @read_tool
    async def financial_list_accounts() -> dict:
        """查看当前用户的独立金融账户、机构、状态和余额。"""
        return await query("list_accounts", {})

    @read_tool
    async def financial_list_cards(account_id: str | None = None) -> dict:
        """查看银行卡及其关联账户；共享账户的卡片不会复制资产。"""
        account = identifier(account_id)
        return await query("list_cards", {"account_id": account} if account else {})

    @read_tool
    async def financial_get_account_overview(account_id: str) -> dict:
        """查询一个账户的现金、存款、投资、贷款与已开通服务。"""
        return await query("get_account_overview", {"account_id": identifier(account_id, required=True)})

    @read_tool
    async def financial_list_transactions(account_id: str | None = None, start_date: str | None = None,
                                          end_date: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """按账户和日期查看有界交易明细，每次最多200条；不能请求其他用户。"""
        arguments = date_range(start_date, end_date)
        account = identifier(account_id)
        if account:
            arguments["account_id"] = account
        arguments.update(limit=bounded_integer(limit, 1, 200), offset=bounded_integer(offset, 0, 100000))
        return await query("list_transactions", arguments)

    @read_tool
    async def financial_list_positions(account_id: str | None = None) -> dict:
        """查看投资持仓、成本、模拟估值和关联结算账户。"""
        account = identifier(account_id)
        return await query("list_positions", {"account_id": account} if account else {})

    @read_tool
    async def financial_list_insurance(status: str | None = None) -> dict:
        """查看保险合同及保障对象；保额不能计入财富或可用资金。"""
        if status is not None and (not isinstance(status, str) or not 1 <= len(status) <= 32
                                   or not status.replace("_", "").isascii()
                                   or not status.replace("_", "").isalnum()):
            raise FinancialMCPError("INVALID_STATUS", "保单状态无效。")
        return await query("list_insurance", {"status": status} if status else {})

    @read_tool
    async def financial_get_policy(policy_id: str) -> dict:
        """查看一份保单的期间、条款、保费、免赔、缴费与理赔关联。"""
        return await query("get_policy", {"policy_id": identifier(policy_id, required=True)})

    @read_tool
    async def financial_list_assets() -> dict:
        """查看当前用户的房产、车辆、农机、个人股权和相关合同。"""
        return await query("list_assets", {})

    @read_tool
    async def financial_list_obligations(start_date: str | None = None, end_date: str | None = None) -> dict:
        """查看还款、保费、续费和其他未来应付款，区分预测与已发生交易。"""
        return await query("list_obligations", date_range(start_date, end_date))

    @read_tool
    async def financial_get_financial_summary(as_of: str | None = None) -> dict:
        """查看指定历史日的资金、资产、负债和净资产；数据范围为演示历史。"""
        target = calendar_date(as_of)
        return await query("get_financial_summary", {"as_of": target} if target else {})

    return server


def main():
    logging.basicConfig(level=logging.WARNING)  # 默认 stderr，stdout 仅用于 MCP 协议。
    try:
        server = build_server()
    except Exception as error:
        log.error("financial MCP startup failed type=%s", type(error).__name__)
        return 2
    # 本地 OS 进程是授权边界。HTTP 部署需单独实现身份验证后才能开放。
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
