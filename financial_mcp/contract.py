"""财务 MCP v1 的有界只读工具与参数约束。"""
from __future__ import annotations

from datetime import date
import re

SCHEMA_VERSION = "navren.financial.v1"
OPERATIONS = (
    "list_connections", "list_accounts", "list_cards", "get_account_overview",
    "list_transactions", "list_transfers", "list_positions", "list_insurance", "get_policy",
    "list_assets", "list_obligations", "get_financial_summary",
)
TOOL_NAMES = tuple("financial_" + name for name in OPERATIONS)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")


class FinancialMCPError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def __str__(self):
        return f"{self.code}: {super().__str__()}"


def identifier(value: str | None, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise FinancialMCPError("INVALID_IDENTIFIER", "记录编号无效。")
    return value


def calendar_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise FinancialMCPError("INVALID_DATE", "日期必须为有效的 YYYY-MM-DD。") from None
    return value


def bounded_integer(value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FinancialMCPError("INVALID_PAGINATION", "分页参数超出允许范围。")
    return value


def date_range(start_date: str | None, end_date: str | None) -> dict:
    first, last = calendar_date(start_date), calendar_date(end_date)
    if first and last and first > last:
        raise FinancialMCPError("INVALID_DATE_RANGE", "开始日期不能晚于结束日期。")
    return {key: value for key, value in (("start_date", first), ("end_date", last)) if value is not None}
