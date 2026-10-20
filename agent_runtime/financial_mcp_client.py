"""Navren 金融 MCP 的宿主侧消费器。

主体来自现有登录工作区登记；数据库路径只接受宿主配置。模型既看不到主体，
也不能传路径、SQL 或切换主体。消费器通过隔离 SDK bridge 做真实 MCP stdio 调用。
"""
from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Mapping

from financial_mcp.contract import SCHEMA_VERSION, TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]
SDK_PYTHON = ROOT / "outputs" / "financial-mcp" / "venv" / "bin" / "python"
MAX_BRIDGE_OUTPUT = 3 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class FinancialMCPError(RuntimeError):
    """稳定、可安全展示的消费端错误，不包含路径、凭据或原始金融数据。"""

    def __init__(self, code: str, message: str = "财务查询暂不可用。"):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) else "MCP_UNAVAILABLE"
        super().__init__(message)


_DESCRIPTIONS = {
    "financial_list_connections": "查看当前主体登记的银行、保险、证券及支付机构。全部为合成历史关系。",
    "financial_list_accounts": "查看当前主体独立财务库中的账户余额与机构关系；金额单位为人民币分。",
    "financial_list_cards": "查看当前主体银行卡及关联账户；不返回卡号。",
    "financial_get_account_overview": "读取一个已查询账户的账户、卡、授信、账单与合同概览。",
    "financial_list_transactions": "按账户和日期读取有界历史流水；最多200条，不可读取其他主体。",
    "financial_list_transfers": "读取当前主体合成历史的完整转账统计及分页明细；本人账户调拨按事件计一次，分币种和方向汇总。默认8条，最多12条，不含现役新操作。",
    "financial_list_positions": "查看历史持仓成本与合成估值；不代表真实净值。",
    "financial_list_insurance": "查看保险合同历史摘要；保额不是资产或可用资金。",
    "financial_get_policy": "读取已查询保单的历史缴费、理赔及适用的合成规则摘要。",
    "financial_list_assets": "查看主体登记的房产、车辆、农机及其他个人资产历史摘要。",
    "financial_list_obligations": "查看合同及服务的应付款历史安排；不等于已支付。",
    "financial_get_financial_summary": "读取指定历史日的合成资产负债摘要；不包含现役账户中的新操作。",
}


def _obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


_STR = {"type": "string", "minLength": 1, "maxLength": 192}
_DATE_SCHEMA = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_NULLABLE_ID = {"type": ["string", "null"], "minLength": 1, "maxLength": 192}

_PARAMETERS = {
    "financial_list_connections": _obj(),
    "financial_list_accounts": _obj(),
    "financial_list_cards": _obj({"account_id": dict(_NULLABLE_ID)}),
    "financial_get_account_overview": _obj({"account_id": dict(_STR)}, ("account_id",)),
    "financial_list_transactions": _obj({
        "account_id": dict(_NULLABLE_ID), "start_date": dict(_DATE_SCHEMA),
        "end_date": dict(_DATE_SCHEMA),
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
    }),
    "financial_list_positions": _obj({"account_id": dict(_NULLABLE_ID)}),
    "financial_list_transfers": _obj({
        "account_id": dict(_NULLABLE_ID), "start_date": dict(_DATE_SCHEMA),
        "end_date": dict(_DATE_SCHEMA),
        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
    }),
    "financial_list_insurance": _obj({"status": {
        "type": ["string", "null"], "enum": ["active", "expired", "cancelled", "pending", None],
    }}),
    "financial_get_policy": _obj({"policy_id": dict(_STR)}, ("policy_id",)),
    "financial_list_assets": _obj(),
    "financial_list_obligations": _obj({"start_date": dict(_DATE_SCHEMA), "end_date": dict(_DATE_SCHEMA)}),
    "financial_get_financial_summary": _obj({"as_of": dict(_DATE_SCHEMA)}),
}


def _provider_input_shape(schema):
    if not isinstance(schema, Mapping) or schema.get("type") != "object" or not isinstance(schema.get("properties"), Mapping):
        raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
    properties = {}
    for name, value in schema["properties"].items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
        raw_type = value.get("type")
        if isinstance(raw_type, str):
            types = {raw_type}
        elif isinstance(raw_type, list):
            types = set(raw_type)
        elif isinstance(value.get("anyOf"), list):
            types = {item.get("type") for item in value["anyOf"] if isinstance(item, Mapping)}
        else:
            raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
        properties[name] = types
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
        raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
    return properties, set(required)


_PROVIDER_INPUTS = {
    "financial_list_connections": ({}, set()),
    "financial_list_accounts": ({}, set()),
    "financial_list_cards": ({"account_id": {"string", "null"}}, set()),
    "financial_get_account_overview": ({"account_id": {"string"}}, {"account_id"}),
    "financial_list_transactions": ({"account_id": {"string", "null"}, "start_date": {"string", "null"},
        "end_date": {"string", "null"}, "limit": {"integer"}, "offset": {"integer"}}, set()),
    "financial_list_positions": ({"account_id": {"string", "null"}}, set()),
    "financial_list_transfers": ({"account_id": {"string", "null"}, "start_date": {"string", "null"},
        "end_date": {"string", "null"}, "limit": {"integer"}, "offset": {"integer"}}, set()),
    "financial_list_insurance": ({"status": {"string", "null"}}, set()),
    "financial_get_policy": ({"policy_id": {"string"}}, {"policy_id"}),
    "financial_list_assets": ({}, set()),
    "financial_list_obligations": ({"start_date": {"string", "null"}, "end_date": {"string", "null"}}, set()),
    "financial_get_financial_summary": ({"as_of": {"string", "null"}}, set()),
}


# Input columns are pinned to the provider schema; private/internal columns are
# accepted and discarded locally. Unknown columns fail closed for this response.
_ROW_COLUMNS = {
    "connections": {"subject_id", "connection_id", "institution_name", "institution_type", "status", "source"},
    "accounts": {"subject_id", "account_id", "connection_id", "name", "account_type", "currency", "scope",
        "source_pool_id", "opened_on", "opening_balance_minor", "balance_minor", "status", "details"},
    "cards": {"subject_id", "card_id", "account_id", "card_type", "masked_number", "status", "issued_on",
        "closed_on", "replaces_card_id", "facility_id", "role"},
    "credit_facilities": {"subject_id", "facility_id", "account_id", "limit_minor", "outstanding_minor"},
    "card_statements": {"subject_id", "statement_id", "account_id", "period_start", "period_end", "due_on",
        "statement_minor", "paid_minor", "status"},
    "contracts": {"subject_id", "contract_id", "contract_type", "account_id", "asset_id", "name", "start_date",
        "end_date", "status", "principal_minor", "details"},
    "positions": {"subject_id", "position_id", "account_id", "asset_id", "contract_id", "cost_basis_minor",
        "book_value_minor", "quantity", "valuation_date", "valuation_source"},
    "policies": {"subject_id", "policy_id", "contract_id", "insurer_connection_id", "policyholder_id", "insured_id",
        "beneficiary_id", "covered_asset_id", "coverage_key", "insurance_type", "terms_version", "start_date",
        "end_date", "status", "sum_insured_minor", "premium_minor", "farmer_paid_minor", "subsidy_minor",
        "deductible_minor", "cash_value_minor", "payment_account_id", "rule_id", "details"},
    "premium_funding": {"subject_id", "policy_id", "payer_type", "amount_minor", "personal_cash_effect", "details"},
    "claims": {"subject_id", "claim_id", "policy_id", "status", "occurred_on", "paid_minor", "transaction_id", "details"},
    "rules": {"rule_id", "kind", "source_url", "verified_on", "effective_from", "effective_to", "region", "provenance", "details"},
    "transactions": {"subject_id", "transaction_id", "account_id", "occurred_at", "booked_on", "status", "amount_minor",
        "signed_minor", "currency", "counterparty", "category", "kind", "scope", "card_id", "contract_id", "mandate_id",
        "original_transaction_id", "transfer_group_id", "source_event_id", "source_transaction_id", "economic_event_id",
        "expense_minor", "details"},
    "assets": {"subject_id", "asset_id", "name", "asset_type", "ownership_scope", "source_asset_id", "details"},
    "obligations": {"subject_id", "obligation_id", "account_id", "contract_id", "mandate_id", "category", "due_on",
        "amount_minor", "status", "details"},
}

_ROW_VISIBLE = {
    "connections": {"connection_id", "institution_name", "institution_type", "status", "source"},
    "accounts": {"account_id", "connection_id", "name", "account_type", "currency", "scope", "opened_on",
        "opening_balance_minor", "balance_minor", "status"},
    "cards": {"card_id", "account_id", "card_type", "status", "issued_on", "closed_on", "replaces_card_id", "facility_id", "role"},
    "credit_facilities": {"facility_id", "account_id", "limit_minor", "outstanding_minor"},
    "card_statements": {"statement_id", "account_id", "period_start", "period_end", "due_on", "statement_minor", "paid_minor", "status"},
    "contracts": {"contract_id", "contract_type", "account_id", "asset_id", "name", "start_date", "end_date", "status", "principal_minor"},
    "positions": {"position_id", "account_id", "asset_id", "contract_id", "cost_basis_minor", "book_value_minor", "quantity", "valuation_date", "valuation_source"},
    "policies": {"policy_id", "contract_id", "insurer_connection_id", "covered_asset_id", "coverage_key", "insurance_type",
        "terms_version", "start_date", "end_date", "status", "sum_insured_minor", "premium_minor", "farmer_paid_minor",
        "subsidy_minor", "deductible_minor", "cash_value_minor", "payment_account_id", "rule_id"},
    "premium_funding": {"payer_type", "amount_minor", "personal_cash_effect"},
    "claims": {"claim_id", "policy_id", "status", "occurred_on", "paid_minor", "transaction_id"},
    "rules": {"rule_id", "kind", "verified_on", "effective_from", "effective_to", "region", "provenance"},
    "transactions": {"transaction_id", "account_id", "occurred_at", "booked_on", "status", "signed_minor", "currency",
        "counterparty", "category", "kind", "scope", "card_id", "contract_id", "mandate_id", "original_transaction_id",
        "transfer_group_id", "economic_event_id", "expense_minor"},
    "assets": {"asset_id", "name", "asset_type", "ownership_scope"},
    "obligations": {"obligation_id", "account_id", "contract_id", "mandate_id", "category", "due_on", "amount_minor", "status"},
}

_LIST_TO_TABLE = {
    "financial_list_connections": "connections", "financial_list_accounts": "accounts", "financial_list_cards": "cards",
    "financial_list_positions": "positions", "financial_list_insurance": "policies", "financial_list_assets": "assets",
    "financial_list_obligations": "obligations",
}
_SUMMARY_FIELDS = {"cash_minor", "investment_minor", "real_assets_minor", "loan_principal_minor", "credit_card_outstanding_minor",
    "liabilities_minor", "total_assets_minor", "net_worth_minor", "insurance_cash_value_minor", "insurance_sum_excluded",
    "credit_limits_excluded", "runtime_operations_included", "notice"}
_EVIDENCE_FIELDS = {"source_database_sha256", "coverage_start", "coverage_end", "bank_integration", "synthetic_contracts",
    "valuation", "dataset_state"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FinancialMCPError("INVALID_RESPONSE")
        result[key] = value
    return result


def _valid_date(value):
    try:
        return isinstance(value, str) and bool(_DATE.fullmatch(value)) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _validate_args(name, arguments):
    if name not in _PARAMETERS or not isinstance(arguments, Mapping):
        raise FinancialMCPError("INVALID_ARGUMENTS")
    schema = _PARAMETERS[name]
    props, required = schema["properties"], set(schema.get("required", ()))
    if set(arguments) - set(props) or required - set(arguments):
        raise FinancialMCPError("INVALID_ARGUMENTS")
    for key, value in arguments.items():
        if value is None and isinstance(props[key]["type"], list) and "null" in props[key]["type"]:
            continue
        if key in ("account_id", "policy_id") and (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)):
            raise FinancialMCPError("INVALID_ARGUMENTS")
        if key in ("start_date", "end_date", "as_of") and not _valid_date(value):
            raise FinancialMCPError("INVALID_ARGUMENTS")
        if key == "status" and value not in ("active", "expired", "cancelled", "pending"):
            raise FinancialMCPError("INVALID_ARGUMENTS")
        if key == "limit" and (type(value) is not int or not 1 <= value <= props[key]["maximum"]):
            raise FinancialMCPError("INVALID_ARGUMENTS")
        if key == "offset" and (type(value) is not int or not 0 <= value <= 100000):
            raise FinancialMCPError("INVALID_ARGUMENTS")
    return dict(arguments)


class FinancialMCPClient:
    """一个工作区一份主体绑定；调用均经官方 SDK bridge 的 MCP ClientSession。"""

    def __init__(self, database: Path, subject_id: str, workspace_id: str, *, root: Path = ROOT,
                 runner=subprocess.run):
        self.database = database.resolve(strict=True)
        self.subject_id = subject_id
        self.workspace_id = workspace_id
        self.root = root.resolve()
        self.python = self.root / "outputs" / "financial-mcp" / "venv" / "bin" / "python"
        self.runner = runner
        self.dataset_version = None
        self._subject_resolver = None
        self._signature = self._database_signature()
        self._verify_local_subject_binding()
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise FinancialMCPError("MCP_SDK_UNAVAILABLE")
        self._tool_names = self._discover()

    @classmethod
    def from_workspace(cls, gateway, workspace, *, environ=None, runner=subprocess.run):
        env = os.environ if environ is None else environ
        raw_database = env.get("NAVREN_FINANCIAL_DB")
        if not raw_database:
            raise FinancialMCPError("CONFIGURATION_REQUIRED")
        demo_accounts = getattr(gateway, "demo_accounts", None)
        subject_for = getattr(demo_accounts, "subject_for", None)
        if not callable(subject_for):
            raise FinancialMCPError("WORKSPACE_MAPPING_UNAVAILABLE")
        try:
            subject = subject_for(workspace)
        except Exception:
            raise FinancialMCPError("WORKSPACE_MAPPING_UNAVAILABLE") from None
        if not isinstance(subject, str) or not _IDENTIFIER.fullmatch(subject):
            raise FinancialMCPError("WORKSPACE_MAPPING_INVALID")
        try:
            database = Path(raw_database).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise FinancialMCPError("DATABASE_UNAVAILABLE") from None
        if not database.is_file():
            raise FinancialMCPError("DATABASE_UNAVAILABLE")
        client = cls(database, subject, workspace, root=ROOT, runner=runner)
        client._subject_resolver = subject_for
        return client

    @property
    def tool_names(self):
        return self._tool_names

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def _database_signature(self):
        stat = self.database.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _verify_local_subject_binding(self):
        try:
            uri = self.database.as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                row = connection.execute("SELECT workspace_id FROM subjects WHERE subject_id=?", (self.subject_id,)).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            raise FinancialMCPError("SUBJECT_MAPPING_UNAVAILABLE") from None
        if row is None or row[0] != self.workspace_id:
            raise FinancialMCPError("SUBJECT_MAPPING_MISMATCH")

    def _call_bridge(self, request):
        self._verify_workspace_binding()
        if self._database_signature() != self._signature:
            raise FinancialMCPError("DATASET_CHANGED")
        payload = self._json(request)
        if len(payload.encode("utf-8")) > 65536:
            raise FinancialMCPError("INVALID_REQUEST")
        env = {
            "NAVREN_FINANCIAL_DB": str(self.database),
            "NAVREN_FINANCIAL_SUBJECT_ID": self.subject_id,
            "PYTHONPATH": str(self.root),
            "PYTHONNOUSERSITE": "1",
        }
        try:
            completed = self.runner([str(self.python), "-m", "financial_mcp.bridge"], cwd=str(self.root), env=env,
                input=payload, capture_output=True, text=True, timeout=45, check=False)
        except subprocess.TimeoutExpired:
            raise FinancialMCPError("MCP_TIMEOUT") from None
        except OSError:
            raise FinancialMCPError("MCP_UNAVAILABLE") from None
        raw = completed.stdout
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BRIDGE_OUTPUT:
            raise FinancialMCPError("INVALID_RESPONSE")
        try:
            response = json.loads(raw, object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(FinancialMCPError("INVALID_RESPONSE")))
        except (ValueError, TypeError, UnicodeError):
            raise FinancialMCPError("INVALID_RESPONSE") from None
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise FinancialMCPError("INVALID_RESPONSE")
        if response["ok"] is not True:
            error = response.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            raise FinancialMCPError(code or "MCP_TOOL_FAILED")
        if self._database_signature() != self._signature:
            raise FinancialMCPError("DATASET_CHANGED")
        self._verify_workspace_binding()
        return response

    def _discover(self):
        response = self._call_bridge({"action": "list_tools"})
        if set(response) != {"ok", "tools"} or not isinstance(response["tools"], list):
            raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
        found = {}
        for item in response["tools"]:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or item["name"] in found:
                raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
            found[item["name"]] = item
        if set(found) != set(TOOL_NAMES):
            raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
        for name, item in found.items():
            annotations = item.get("annotations")
            if (not isinstance(annotations, Mapping) or annotations.get("readOnlyHint") is not True
                    or annotations.get("destructiveHint") is not False or annotations.get("openWorldHint") is not False):
                raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
            actual_properties, actual_required = _provider_input_shape(item.get("inputSchema"))
            expected_properties, expected_required = _PROVIDER_INPUTS[name]
            if actual_properties != expected_properties or actual_required != expected_required:
                raise FinancialMCPError("MCP_TOOL_CONTRACT_INVALID")
        return tuple(TOOL_NAMES)

    def tool_definitions(self):
        return [{"type": "function", "function": {"name": name, "description": _DESCRIPTIONS[name],
            "parameters": json.loads(self._json(_PARAMETERS[name]))}} for name in self._tool_names]

    def assert_fresh(self):
        if self._database_signature() != self._signature:
            raise FinancialMCPError("DATASET_CHANGED")
        self._verify_workspace_binding()

    def _verify_workspace_binding(self):
        if self._subject_resolver is None:
            return
        try:
            current = self._subject_resolver(self.workspace_id)
        except Exception:
            raise FinancialMCPError("WORKSPACE_MAPPING_UNAVAILABLE") from None
        if current != self.subject_id:
            raise FinancialMCPError("SUBJECT_MAPPING_MISMATCH")

    def call(self, name, arguments):
        if name not in self._tool_names:
            raise FinancialMCPError("TOOL_NOT_ALLOWED")
        args = _validate_args(name, arguments)
        response = self._call_bridge({"action": "call_tool", "name": name, "arguments": args})
        if set(response) != {"ok", "data"} or not isinstance(response["data"], Mapping):
            raise FinancialMCPError("INVALID_RESPONSE")
        envelope = response["data"]
        required = {"schema_version", "subject_id", "source", "dataset_version", "as_of", "data", "evidence"}
        if set(envelope) != required:
            # 服务端错误必须传为 MCP 错误，不能当作空列表/零余额。
            if "error" in envelope:
                error = envelope.get("error")
                code = error.get("code") if isinstance(error, Mapping) else None
                raise FinancialMCPError(code or "FINANCIAL_QUERY_FAILED")
            raise FinancialMCPError("INVALID_RESPONSE")
        if (envelope["schema_version"] != SCHEMA_VERSION or envelope["subject_id"] != self.subject_id
                or envelope["source"] != "navren_synthetic" or not isinstance(envelope["dataset_version"], str)
                or not _valid_date(envelope["as_of"])):
            raise FinancialMCPError("SUBJECT_OR_SCHEMA_MISMATCH")
        version = envelope["dataset_version"]
        if self.dataset_version is not None and version != self.dataset_version:
            raise FinancialMCPError("DATASET_CHANGED")
        self.dataset_version = version
        evidence = envelope["evidence"]
        if (not isinstance(evidence, Mapping) or set(evidence) != _EVIDENCE_FIELDS
                or not isinstance(evidence.get("source_database_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence["source_database_sha256"])
                or evidence.get("coverage_start") != "2016-10-04" or evidence.get("coverage_end") != "2026-10-03"
                or evidence.get("bank_integration") is not False or evidence.get("synthetic_contracts") is not True
                or evidence.get("valuation") != "synthetic_not_real_NAV"
                or evidence.get("dataset_state") != "historical_read_only"):
            raise FinancialMCPError("INVALID_RESPONSE")
        data = self._project_operation(name, envelope["data"])
        return {"schema_version": SCHEMA_VERSION, "source": envelope["source"], "dataset_version": version,
            "as_of": envelope["as_of"], "data": data,
            "evidence": {key: value for key, value in evidence.items() if key != "source_database_sha256"},
            "notice": "该独立财务库只含合成历史，不是实时银行数据；保险保额不计入资产，现役账户操作不在此摘要内。"}

    def _row(self, value, table):
        if not isinstance(value, Mapping):
            raise FinancialMCPError("SUBJECT_MISMATCH")
        # Rules are global synthetic rule records joined through the already
        # ownership-checked policy; all user-owned records carry subject_id.
        if table == "rules":
            if "subject_id" in value:
                raise FinancialMCPError("INVALID_RESPONSE")
        elif value.get("subject_id") != self.subject_id:
            raise FinancialMCPError("SUBJECT_MISMATCH")
        if set(value) - _ROW_COLUMNS[table]:
            raise FinancialMCPError("INVALID_RESPONSE")
        return {key: value[key] for key in _ROW_VISIBLE[table] if key in value}

    def _rows(self, value, table, *, limit=200):
        if not isinstance(value, list) or len(value) > limit:
            raise FinancialMCPError("RESULT_TOO_LARGE")
        return [self._row(row, table) for row in value]

    def _project_operation(self, name, data):
        if not isinstance(data, Mapping):
            raise FinancialMCPError("INVALID_RESPONSE")
        if name in _LIST_TO_TABLE:
            if set(data) != {"items", "total", "has_more"}:
                raise FinancialMCPError("INVALID_RESPONSE")
            if type(data["total"]) is not int or data["total"] < 0 or type(data["has_more"]) is not bool:
                raise FinancialMCPError("INVALID_RESPONSE")
            return {"items": self._rows(data["items"], _LIST_TO_TABLE[name]), "total": data["total"], "has_more": data["has_more"]}
        if name == "financial_list_transactions":
            fields = {"items", "total", "limit", "offset", "has_more", "coverage_partial"}
            if set(data) != fields or any(type(data[key]) is not int for key in ("total", "limit", "offset")):
                raise FinancialMCPError("INVALID_RESPONSE")
            if type(data["has_more"]) is not bool or type(data["coverage_partial"]) is not bool:
                raise FinancialMCPError("INVALID_RESPONSE")
            return {**{key: data[key] for key in fields if key != "items"},
                    "items": self._rows(data["items"], "transactions")}
        if name == "financial_list_transfers":
            return self._transfer_data(data)
        if name == "financial_get_financial_summary":
            if set(data) != _SUMMARY_FIELDS:
                raise FinancialMCPError("INVALID_RESPONSE")
            for key, value in data.items():
                if key.endswith("_minor") and (value is not None and type(value) is not int):
                    raise FinancialMCPError("INVALID_RESPONSE")
                if key.endswith("_excluded") or key == "runtime_operations_included":
                    if type(value) is not bool:
                        raise FinancialMCPError("INVALID_RESPONSE")
                if key == "notice" and not isinstance(value, str):
                    raise FinancialMCPError("INVALID_RESPONSE")
            return dict(data)
        if name == "financial_get_account_overview":
            fields = {"account", "cards", "credit_facilities", "recent_card_statements", "contracts"}
            if set(data) != fields:
                raise FinancialMCPError("INVALID_RESPONSE")
            return {"account": self._row(data["account"], "accounts"),
                "cards": self._rows(data["cards"], "cards"),
                "credit_facilities": self._rows(data["credit_facilities"], "credit_facilities"),
                "recent_card_statements": self._rows(data["recent_card_statements"], "card_statements"),
                "contracts": self._rows(data["contracts"], "contracts")}
        if name == "financial_get_policy":
            fields = {"policy", "premium_funding", "claims", "rule"}
            if set(data) != fields:
                raise FinancialMCPError("INVALID_RESPONSE")
            return {"policy": self._row(data["policy"], "policies"),
                "premium_funding": self._rows(data["premium_funding"], "premium_funding"),
                "claims": self._rows(data["claims"], "claims"), "rule": self._row(data["rule"], "rules")}
        raise FinancialMCPError("TOOL_NOT_ALLOWED")

    def _transfer_data(self, data):
        fields = {"items", "transfer_count", "ledger_row_count", "totals_by_currency_and_direction",
            "actual_coverage_start", "actual_coverage_end", "offset", "limit", "has_more", "query_complete",
            "item_page_complete", "missing_fields", "coverage_partial", "runtime_ledger_included"}
        item_fields = {"subject_id", "transfer_id", "transaction_ids", "booked_on", "occurred_at", "kind",
            "currency", "amount_minor", "signed_minor", "direction", "source_account_id", "target_account_id",
            "counterparty", "pair_status"}
        summary_fields = {"currency", "direction", "transfer_count", "amount_minor"}
        def reject():
            raise FinancialMCPError("INVALID_RESPONSE")
        def identifier(value, nullable=False):
            if value is None and nullable:
                return
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                reject()
        def amount(value, signed=False):
            if value is not None and (type(value) is not int or (not signed and value < 0)):
                reject()
        def currency(value, nullable=False):
            if value is None and nullable:
                return
            if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
                reject()
        if set(data) != fields:
            reject()
        for key in ("transfer_count", "ledger_row_count", "offset", "limit"):
            if type(data[key]) is not int or data[key] < 0:
                reject()
        if not 1 <= data["limit"] <= 12 or data["offset"] > 100000:
            reject()
        for key in ("has_more", "query_complete", "item_page_complete", "coverage_partial", "runtime_ledger_included"):
            if type(data[key]) is not bool:
                reject()
        if data["query_complete"] is not True or data["runtime_ledger_included"] is not False:
            reject()
        rows = data["items"]
        if not isinstance(rows, list) or len(rows) != min(data["limit"], max(0, data["transfer_count"]-data["offset"])):
            reject()
        if data["has_more"] != (data["offset"]+len(rows) < data["transfer_count"]):
            reject()
        if data["item_page_complete"] != (data["offset"] == 0 and not data["has_more"]):
            reject()
        if data["ledger_row_count"] < data["transfer_count"]:
            reject()
        for key in ("actual_coverage_start", "actual_coverage_end"):
            if data[key] is not None and not _valid_date(data[key]):
                reject()
        first, last = data["actual_coverage_start"], data["actual_coverage_end"]
        if (first is None) != (last is None) or (first is not None and first > last):
            reject()
        if (data["transfer_count"] == 0) != (first is None):
            reject()
        missing = data["missing_fields"]
        allowed_missing = item_fields-{"subject_id", "transfer_id", "transaction_ids", "booked_on", "occurred_at", "kind", "pair_status"} | {"transfer_pair", "transfer_group_id"}
        if (not isinstance(missing, list) or any(not isinstance(v, str) or v not in allowed_missing for v in missing)
                or len(missing) != len(set(missing))):
            reject()
        projected_rows = []
        seen = set()
        for row in rows:
            if not isinstance(row, Mapping) or row.get("subject_id") != self.subject_id:
                raise FinancialMCPError("SUBJECT_MISMATCH")
            if set(row) != item_fields:
                reject()
            identifier(row["transfer_id"])
            if row["transfer_id"] in seen:
                reject()
            seen.add(row["transfer_id"])
            ids = row["transaction_ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
                reject()
            for ident in ids:
                identifier(ident)
            if len(set(ids)) != len(ids):
                reject()
            for key in ("source_account_id", "target_account_id"):
                identifier(row[key], nullable=True)
            if not _valid_date(row["booked_on"]):
                reject()
            if (not isinstance(row["occurred_at"], str) or len(row["occurred_at"]) > 64
                    or not row["occurred_at"].startswith(row["booked_on"]+"T")):
                reject()
            try:
                datetime.fromisoformat(row["occurred_at"])
            except ValueError:
                reject()
            if row["kind"] not in ("internal_transfer", "transfer", "external_transfer"):
                reject()
            if row["direction"] not in ("internal", "incoming", "outgoing", None):
                reject()
            if row["pair_status"] not in ("paired", "unpaired", "single_entry", "inconsistent"):
                reject()
            currency(row["currency"], nullable=True)
            amount(row["amount_minor"]); amount(row["signed_minor"], signed=True)
            if row["currency"] is None and "currency" not in missing:
                reject()
            if row["amount_minor"] is None and "amount_minor" not in missing:
                reject()
            if row["pair_status"] == "paired":
                value = row["amount_minor"]
                if (row["kind"] != "internal_transfer" or value is None or value <= 0
                        or row["currency"] is None or len(ids) != 2
                        or row["source_account_id"] is None or row["target_account_id"] is None
                        or row["source_account_id"] == row["target_account_id"]
                        or row["direction"] is None):
                    reject()
                expected = {"internal": 0, "incoming": value, "outgoing": -value}[row["direction"]]
                if row["signed_minor"] != expected:
                    reject()
            elif row["kind"] == "internal_transfer":
                if row["pair_status"] not in ("unpaired", "inconsistent") or row["amount_minor"] is not None or row["signed_minor"] is not None:
                    reject()
            elif row["pair_status"] != "single_entry":
                reject()
            else:
                if len(ids) != 1 or row["direction"] == "internal":
                    reject()
                value = row["amount_minor"]
                if value is not None:
                    if value <= 0 or row["direction"] not in ("incoming", "outgoing"):
                        reject()
                    if row["signed_minor"] != (value if row["direction"] == "incoming" else -value):
                        reject()
                elif row["signed_minor"] is not None:
                    reject()
            if row["counterparty"] is not None and (not isinstance(row["counterparty"], str) or len(row["counterparty"]) > 240):
                reject()
            projected_rows.append({k: v for k, v in row.items() if k != "subject_id"})
        summaries = data["totals_by_currency_and_direction"]
        if not isinstance(summaries, list) or len(summaries) > 96:
            reject()
        seen_buckets = set(); bucket_count = 0
        for row in summaries:
            if not isinstance(row, Mapping) or set(row) != summary_fields:
                reject()
            currency(row["currency"]); amount(row["amount_minor"])
            if row["amount_minor"] is None and "amount_minor" not in missing:
                reject()
            if row["direction"] not in ("internal", "incoming", "outgoing"):
                reject()
            if type(row["transfer_count"]) is not int or row["transfer_count"] < 1:
                reject()
            bucket = (row["currency"], row["direction"])
            if bucket in seen_buckets:
                reject()
            seen_buckets.add(bucket); bucket_count += row["transfer_count"]
        if bucket_count > data["transfer_count"] or (not {"currency", "direction"}.intersection(missing) and bucket_count != data["transfer_count"]):
            reject()
        return {**dict(data), "items": projected_rows}


def tool_specs():
    """本地固定工具合同；运行时只有已发现并核验的工具会被暴露。"""
    return {name: (_DESCRIPTIONS[name], json.loads(json.dumps(_PARAMETERS[name]))) for name in TOOL_NAMES}
