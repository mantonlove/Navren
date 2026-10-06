"""金融纯计算合同；验证来源/授权和业务接线由基座负责。"""
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping

CONTRACT_VERSION = "1.0.0"
STATUSES = {"calculated", "partial", "unavailable", "infeasible"}


def require_minor(value, name, nonnegative=False):
    if type(value) is not int or (nonnegative and value < 0):
        raise ValueError(f"{name}必须是{'非负' if nonnegative else ''}整数分")
    return value


def decimal_value(value, name, nonnegative=False):
    # 不接受二进制浮点价格，调用方应使用原始十进制字符串。
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name}必须是十进制字符串、整数或Decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name}不是有效十进制数") from None
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"{name}超出允许范围")
    return result


def iso_date(value, name="日期"):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}必须是ISO公历日期") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{name}必须是ISO公历日期")
    return parsed


@dataclass(frozen=True)
class MetricContext:
    subject_id: str
    currency: str
    data_scope: str
    as_of: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    input_refs: tuple[str, ...] = ()
    source_versions: tuple[tuple[str, str], ...] = ()
    coverage_complete: bool = True

    def __post_init__(self):
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ValueError("必须有明确对象subject_id")
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("币种必须是三位大写代码，不做汇率换算")
        if not isinstance(self.data_scope, str) or not self.data_scope.strip():
            raise ValueError("必须说明实际数据范围data_scope")
        if self.as_of is not None:
            iso_date(self.as_of)
            if self.period_start is not None or self.period_end is not None:
                raise ValueError("时点与期间不能混用")
        else:
            if self.period_start is None or self.period_end is None:
                raise ValueError("必须提供时点或完整期间")
            if iso_date(self.period_start) > iso_date(self.period_end):
                raise ValueError("期间倒置")
        if type(self.coverage_complete) is not bool:
            raise ValueError("覆盖状态必须显式为布尔值")
        if not isinstance(self.input_refs, (tuple, list)) or not self.input_refs or any(not isinstance(r, str) or not r.strip() for r in self.input_refs):
            raise ValueError("必须提供输入证据引用")
        if not isinstance(self.source_versions, (tuple, list)) or not self.source_versions:
            raise ValueError("必须提供来源版本")
        seen = {}
        for pair in self.source_versions:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(not isinstance(v, str) or not v.strip() for v in pair):
                raise ValueError("来源版本必须是非空键值对")
            key, value = pair
            if key in seen and seen[key] != value:
                raise ValueError("同一来源不能混用不同版本")
            seen[key] = value
        object.__setattr__(self, "input_refs", tuple(sorted(set(self.input_refs))))
        object.__setattr__(self, "source_versions", tuple(sorted(seen.items())))


def bind_inputs(context, records):
    """附加实际消费的证据；不替代基座的事实与版本真实性核验。"""
    versions = dict(context.source_versions)
    refs = set(context.input_refs)
    for record in records:
        ref, version = record.evidence_ref, record.source_version
        if not isinstance(ref, str) or not ref or not isinstance(version, str) or not version:
            raise ValueError("每项输入必须有证据引用与版本")
        if ref in versions and versions[ref] != version:
            raise ValueError("同一输入证据存在版本冲突")
        refs.add(ref)
        versions[ref] = version
    return replace(context, input_refs=tuple(refs), source_versions=tuple(versions.items()))


@dataclass(frozen=True)
class FinancialMetric:
    metric_id: str
    subject_id: str
    metric_name: str
    currency: str
    unit: str
    value_minor: int | None
    as_of: str | None
    period_start: str | None
    period_end: str | None
    data_scope: str
    calculation_status: str
    basis: str
    formula_id: str
    input_refs: tuple[str, ...]
    source_versions: tuple[tuple[str, str], ...]
    excluded_items: tuple[str, ...]
    coverage_gaps: tuple[str, ...]
    assumed_inputs: tuple[Mapping[str, object], ...]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        ctx = MetricContext(self.subject_id, self.currency, self.data_scope, self.as_of,
                            self.period_start, self.period_end, self.input_refs, self.source_versions)
        if self.contract_version != CONTRACT_VERSION or self.unit != "minor_currency":
            raise ValueError("指标合同版本或单位不匹配")
        if self.calculation_status not in STATUSES:
            raise ValueError("指标状态无效")
        if not all(isinstance(v, str) and v.strip() for v in (self.metric_id, self.metric_name, self.basis, self.formula_id)):
            raise ValueError("指标身份与公式不能为空")
        if self.value_minor is None:
            if self.calculation_status in {"calculated", "partial"} or not self.coverage_gaps:
                raise ValueError("缺失指标必须给出不可计算原因")
        else:
            require_minor(self.value_minor, self.metric_name)
            if self.calculation_status in {"unavailable", "infeasible"}:
                raise ValueError("不可计算/不可行不能携带金额")
        if not isinstance(self.assumed_inputs, (tuple, list)):
            raise ValueError("假设必须是对象序列")
        for values in (self.excluded_items, self.coverage_gaps):
            if not isinstance(values, (tuple, list)) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError("排除项和缺项必须是非空字符串序列")
        frozen = []
        for assumption in self.assumed_inputs:
            if not isinstance(assumption, Mapping):
                raise ValueError("假设必须是有字段名的JSON对象")
            # 不接受可变嵌套结构或浮点数，避免生成后变更假设却保持同metric_id。
            if any(not isinstance(k, str) or not k or (v is not None and type(v) not in (str, int, bool))
                   for k, v in assumption.items()):
                raise ValueError("假设字段必须是不可变JSON标量")
            frozen.append(MappingProxyType(dict(assumption)))
        object.__setattr__(self, "input_refs", ctx.input_refs)
        object.__setattr__(self, "source_versions", ctx.source_versions)
        object.__setattr__(self, "assumed_inputs", tuple(frozen))
        object.__setattr__(self, "excluded_items", tuple(self.excluded_items))
        object.__setattr__(self, "coverage_gaps", tuple(self.coverage_gaps))

    def to_dict(self):
        return {
            **{k: getattr(self, k) for k in (
                "contract_version", "metric_id", "subject_id", "metric_name", "currency", "unit",
                "value_minor", "as_of", "period_start", "period_end", "data_scope",
                "calculation_status", "basis", "formula_id")},
            "input_refs": list(self.input_refs), "source_versions": dict(self.source_versions),
            "excluded_items": list(self.excluded_items), "coverage_gaps": list(self.coverage_gaps),
            "assumed_inputs": [dict(a) for a in self.assumed_inputs],
        }


def money_metric(context, name, basis, formula_id, value_minor, *, status="calculated",
                 gaps=(), excluded=(), assumptions=()):
    if not isinstance(context, MetricContext):
        raise ValueError("需要MetricContext")
    if status not in STATUSES or not all(isinstance(v, str) and v for v in (name, basis, formula_id)):
        raise ValueError("指标状态、名称、口径或公式无效")
    if not isinstance(gaps, (tuple, list)) or not isinstance(excluded, (tuple, list)) or not isinstance(assumptions, (tuple, list)):
        raise ValueError("缺项、排除项和假设必须是序列")
    if not context.coverage_complete:
        gaps = (*gaps, "input_coverage_incomplete")
        if status == "calculated":
            value_minor, status = None, "unavailable"
    if value_minor is not None:
        require_minor(value_minor, name)
        if status in {"unavailable", "infeasible"}:
            raise ValueError("不可计算/不可行指标不能携带金额")
    elif status in {"calculated", "partial"}:
        raise ValueError("已计算指标必须有整数金额，缺项不能补零")
    if status in {"unavailable", "infeasible"} and not gaps:
        raise ValueError("不可计算/不可行必须给出原因")
    for values in (gaps, excluded):
        if any(not isinstance(v, str) or not v for v in values):
            raise ValueError("缺项及排除项必须有稳定说明")
    payload = {
        "contract_version": CONTRACT_VERSION, "subject_id": context.subject_id,
        "metric_name": name, "currency": context.currency, "unit": "minor_currency",
        "value_minor": value_minor, "as_of": context.as_of, "period_start": context.period_start,
        "period_end": context.period_end, "data_scope": context.data_scope,
        "calculation_status": status, "basis": basis, "formula_id": formula_id,
        "input_refs": context.input_refs, "source_versions": context.source_versions,
        "excluded_items": tuple(sorted(set(excluded))), "coverage_gaps": tuple(sorted(set(gaps))),
        "assumed_inputs": tuple(dict(a) for a in assumptions),
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       allow_nan=False).encode()).hexdigest()[:24]
    return FinancialMetric(metric_id="fm-" + digest, **payload)
