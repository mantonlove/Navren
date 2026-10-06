"""纯收益核算；不读取行情、账户、数据库，也不生成交易或投资建议。

金额以整数分输出，价格/份额使用 Decimal。历史重建仅支持单账户、单份额、
单币种与移动加权平均成本；基座负责提供核验后的对象、原始事件及覆盖声明。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
from typing import Mapping, Sequence

from .contracts import CONTRACT_VERSION, FinancialMetric, MetricContext, decimal_value, money_metric, require_minor


def _on(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("日期必须为 YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("日期必须为 YYYY-MM-DD")
    return parsed


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _minor_decimal(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def holding_value_minor(quantity: object, unit_price: object, *, currency: str = "CNY") -> int:
    """仅人民币：份额 × 每份人民币元价格 × 100，用 HALF_UP 舍入到分。"""
    if currency != "CNY":
        raise ValueError("unsupported_currency_scale")
    q = decimal_value(quantity, "quantity", nonnegative=True)
    if q == 0:
        return 0  # 已确认零份额不需要价格；未知份额不能走此分支。
    price = decimal_value(unit_price, "unit_price", nonnegative=True)
    if price <= 0:
        raise ValueError("非零份额必须有正价格")
    with localcontext() as ctx:
        ctx.prec = max(50, len(q.as_tuple().digits) + len(price.as_tuple().digits) + 10,
                       max(q.adjusted(), 0) + max(price.adjusted(), 0) + 10)
        return _minor_decimal(q * price * 100)


@dataclass(frozen=True)
class HoldingValue:
    instrument_id: str
    currency: str
    quantity: object | None
    unit_price: object | None


@dataclass(frozen=True)
class EquitySnapshot:
    subject_id: str
    currency: str
    on: str
    holdings: tuple[HoldingValue, ...] | None
    expected_instrument_ids: tuple[str, ...]
    cash_minor: int | None
    receivables_minor: int | None
    payables_minor: int | None
    coverage_complete: bool


def _snapshot_values(context: MetricContext, snapshot: EquitySnapshot,
                     expected_on: str | None) -> tuple[int | None, int | None, tuple[str, ...]]:
    gaps: list[str] = []
    if context.currency != "CNY":
        gaps.append("unsupported_currency_scale")
    _on(snapshot.on)
    if snapshot.subject_id != context.subject_id:
        gaps.append("snapshot_subject_mismatch")
    if snapshot.currency != context.currency:
        gaps.append("snapshot_currency_mismatch")
    if expected_on is None or snapshot.on != expected_on:
        gaps.append("snapshot_date_mismatch")
    if not context.coverage_complete or snapshot.coverage_complete is not True:
        gaps.append("snapshot_coverage_incomplete")
    expected = snapshot.expected_instrument_ids
    if any(not isinstance(i, str) or not i for i in expected) or len(set(expected)) != len(expected):
        raise ValueError("预期份额身份必须非空且不重复")
    if snapshot.holdings is None:
        gaps.append("holdings_missing")
        holdings_value = None
    else:
        identities = [h.instrument_id for h in snapshot.holdings]
        if len(identities) != len(set(identities)):
            gaps.append("duplicate_holding_identity")
        if set(identities) != set(expected):
            gaps.append("holding_identity_coverage_mismatch")
        holdings_value = 0
        for h in snapshot.holdings:
            if h.currency != context.currency:
                gaps.append("holding_currency_mismatch")
            if h.quantity is None:
                gaps.append("quantity_missing:" + h.instrument_id)
                continue
            q = decimal_value(h.quantity, "quantity", nonnegative=True)
            if q != 0 and h.unit_price is None:
                gaps.append("price_missing:" + h.instrument_id)
                continue
            holdings_value += holding_value_minor(q, h.unit_price)
    components = {}
    for name in ("cash_minor", "receivables_minor", "payables_minor"):
        value = getattr(snapshot, name)
        if value is None:
            gaps.append(name + "_missing")
        else:
            components[name] = require_minor(value, name, nonnegative=True)
    if gaps:
        return None, None, _unique(gaps)
    return (holdings_value, holdings_value + components["cash_minor"] +
            components["receivables_minor"] - components["payables_minor"], ())


def portfolio_equity(context: MetricContext, snapshot: EquitySnapshot) -> FinancialMetric:
    """仅人民币的持仓加现金/应收减应付权益；不支持币种不能绕过估值合同。"""
    _, value, gaps = _snapshot_values(context, snapshot, context.as_of)
    return money_metric(context, "portfolio_equity", "holdings_plus_cash_receivables_less_payables",
                        "returns.equity.v1", value, status="unavailable" if gaps else "calculated",
                        gaps=gaps, excluded=("未计入持仓成本；成本不是额外资产",))


def holdings_market_value(context: MetricContext, snapshot: EquitySnapshot) -> FinancialMetric:
    """仅持仓市值。现金/应收/应付不在此指标中；不替代总权益。"""
    # 不要求本指标排除的现金/债权字段已知，但仍要求持仓与对象覆盖完整。
    scoped = replace(snapshot, cash_minor=0, receivables_minor=0, payables_minor=0)
    value, _, gaps = _snapshot_values(context, scoped, context.as_of)
    return money_metric(context, "holdings_market_value", "holdings_market_value",
                        "returns.holdings_value.v1", value, status="unavailable" if gaps else "calculated",
                        gaps=gaps, excluded=("投资现金", "应收", "应付"))


def period_profit(context: MetricContext, opening: EquitySnapshot, closing: EquitySnapshot,
                  *, owner_contributions_minor: int | None,
                  owner_withdrawals_minor: int | None) -> FinancialMetric:
    """期间损益 = 期末权益 − 期初权益 − (所有者投入 − 所有者提取)。"""
    _, begin, a = _snapshot_values(context, opening, context.period_start)
    _, end, b = _snapshot_values(context, closing, context.period_end)
    gaps = list(a + b)
    if context.period_start is None or context.period_end is None or _on(context.period_start) >= _on(context.period_end):
        gaps.append("positive_period_required")
    flows = {}
    for name, value in (("owner_contributions_minor", owner_contributions_minor),
                        ("owner_withdrawals_minor", owner_withdrawals_minor)):
        if value is None:
            gaps.append(name + "_missing")
        else:
            flows[name] = require_minor(value, name, nonnegative=True)
    value = None if gaps else end - begin - flows["owner_contributions_minor"] + flows["owner_withdrawals_minor"]
    return money_metric(context, "period_pnl", "total_equity_period_pnl", "returns.period_pnl.v1",
                        value, status="unavailable" if gaps else "calculated", gaps=_unique(gaps),
                        excluded=("内部买卖、分红应收到账不是所有者投入或提取",))


@dataclass(frozen=True)
class RatioMetric:
    context: MetricContext
    metric_name: str
    basis: str
    formula_id: str
    value_decimal: Decimal | None
    coverage_gaps: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.context, MetricContext):
            raise ValueError("需要 MetricContext")
        if any(not isinstance(v, str) or not v for v in (self.metric_name, self.basis, self.formula_id)):
            raise ValueError("收益率名称、口径及公式必须明确")
        if self.value_decimal is not None:
            if not isinstance(self.value_decimal, Decimal) or not self.value_decimal.is_finite():
                raise ValueError("收益率必须为有限 Decimal")
            if not self.context.coverage_complete or self.coverage_gaps:
                raise ValueError("不完整收益率不能携带数值")
        elif not self.coverage_gaps:
            raise ValueError("缺收益率必须说明原因")

    def to_dict(self) -> dict:
        metadata = asdict(self.context)
        metadata.pop("coverage_complete")
        metadata["source_versions"] = dict(self.context.source_versions)
        metadata["input_refs"] = list(self.context.input_refs)
        payload = {**metadata, "contract_version": CONTRACT_VERSION,
                "metric_name": self.metric_name, "unit": "ratio", "basis": self.basis,
                "formula_id": self.formula_id,
                "value_decimal": str(self.value_decimal) if self.value_decimal is not None else None,
                "calculation_status": "calculated" if self.value_decimal is not None else "unavailable",
                "coverage_gaps": list(self.coverage_gaps), "excluded_items": [], "assumed_inputs": []}
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"metric_id": "ratio-" + hashlib.sha256(key.encode()).hexdigest()[:24], **payload}


def simple_return(context: MetricContext, *, opening_equity_minor: int | None,
                  closing_equity_minor: int | None, owner_contributions_minor: int | None,
                  owner_withdrawals_minor: int | None) -> RatioMetric:
    """仅适用于完整覆盖且期间没有任何所有者流入、流出的权益收益率。"""
    gaps = []
    if not context.coverage_complete:
        gaps.append("return_coverage_incomplete")
    if context.period_start is None or context.period_end is None:
        gaps.append("return_period_missing")
    elif _on(context.period_start) >= _on(context.period_end):
        gaps.append("positive_period_required")
    values = {}
    for name, value in (("opening_equity_minor", opening_equity_minor), ("closing_equity_minor", closing_equity_minor),
                        ("owner_contributions_minor", owner_contributions_minor), ("owner_withdrawals_minor", owner_withdrawals_minor)):
        if value is None:
            gaps.append(name + "_missing")
        else:
            values[name] = require_minor(value, name, nonnegative=True)
    if values.get("opening_equity_minor", 1) == 0:
        gaps.append("positive_opening_equity_required")
    if values.get("owner_contributions_minor", 0) or values.get("owner_withdrawals_minor", 0):
        gaps.append("cashflows_require_subperiod_valuations")
    with localcontext() as ctx:
        ctx.prec = 50
        ratio = None if gaps else Decimal(values["closing_equity_minor"] - values["opening_equity_minor"]) / values["opening_equity_minor"]
    return RatioMetric(context, "simple_return", "total_equity_no_owner_flows", "returns.simple.v1", ratio, _unique(gaps))


@dataclass(frozen=True)
class ReturnSubperiod:
    subject_id: str
    currency: str
    start: str
    end: str
    opening_equity_minor: int | None
    closing_equity_minor: int | None
    internal_contributions_minor: int | None
    internal_withdrawals_minor: int | None


@dataclass(frozen=True)
class ReturnBoundary:
    subject_id: str
    currency: str
    on: str
    contribution_minor: int | None
    withdrawal_minor: int | None


def time_weighted_return(context: MetricContext, subperiods: Sequence[ReturnSubperiod],
                         boundaries: Sequence[ReturnBoundary]) -> RatioMetric:
    """链接无内部现金流的子期；边界估值必须分别在所有者现金流前、后。

    检查前一期期末权益 + 边界净投入 = 后一期期初权益，不能仅用净流入修正
    全期间分母。有流入又流出，即使净额为零，也要求子期和边界完整覆盖。
    """
    gaps: list[str] = []
    if not context.coverage_complete:
        gaps.append("return_coverage_incomplete")
    if not subperiods or len(boundaries) != len(subperiods) - 1:
        gaps.append("subperiod_boundary_coverage_mismatch")
    factor = Decimal(1)
    with localcontext() as dec:
        dec.prec = 50
        for index, part in enumerate(subperiods):
            if part.subject_id != context.subject_id or part.currency != context.currency:
                gaps.append("subperiod_identity_mismatch")
            if _on(part.start) >= _on(part.end):
                gaps.append("subperiod_order_invalid")
            if index == 0 and part.start != context.period_start:
                gaps.append("subperiod_start_coverage_mismatch")
            if index == len(subperiods) - 1 and part.end != context.period_end:
                gaps.append("subperiod_end_coverage_mismatch")
            values = []
            for name in ("opening_equity_minor", "closing_equity_minor", "internal_contributions_minor", "internal_withdrawals_minor"):
                value = getattr(part, name)
                if value is None:
                    gaps.append("subperiod_" + name + "_missing")
                else:
                    require_minor(value, name, nonnegative=True)
                values.append(value)
            opening, closing, deposits, withdrawals = values
            if deposits or withdrawals:
                gaps.append("internal_cashflow_requires_finer_subperiods")
            if opening == 0:
                gaps.append("positive_opening_equity_required")
            if opening is not None and closing is not None and opening > 0:
                factor *= Decimal(closing) / opening
            if index > 0 and index - 1 < len(boundaries):
                prior = subperiods[index - 1]
                boundary = boundaries[index - 1]
                if boundary.subject_id != context.subject_id or boundary.currency != context.currency:
                    gaps.append("boundary_identity_mismatch")
                if boundary.on != prior.end or boundary.on != part.start:
                    gaps.append("boundary_date_mismatch")
                for name in ("contribution_minor", "withdrawal_minor"):
                    value = getattr(boundary, name)
                    if value is None:
                        gaps.append("boundary_" + name + "_missing")
                    else:
                        require_minor(value, name, nonnegative=True)
                if all(v is not None for v in (prior.closing_equity_minor, opening, boundary.contribution_minor, boundary.withdrawal_minor)):
                    if prior.closing_equity_minor + boundary.contribution_minor - boundary.withdrawal_minor != opening:
                        gaps.append("boundary_equity_does_not_reconcile")
        ratio = None if gaps else factor - 1
    return RatioMetric(context, "time_weighted_return", "linked_total_equity_subperiods", "returns.twr.v1", ratio, _unique(gaps))


@dataclass(frozen=True)
class DividendReceivable:
    dividend_id: str
    amount_minor: int


@dataclass(frozen=True)
class OpeningPosition:
    subject_id: str
    currency: str
    instrument_id: str
    on: str
    quantity: object | None
    remaining_cost_minor: int | None
    cash_minor: int | None
    payables_minor: int | None
    dividend_receivables: tuple[DividendReceivable, ...] | None
    unit_price: object | None


@dataclass(frozen=True)
class HoldingEvent:
    event_id: str
    sequence: int
    on: str
    subject_id: str
    currency: str
    instrument_id: str
    kind: str
    quantity: object | None = None
    gross_minor: int | None = None
    fee_minor: int | None = None
    fee_id: str | None = None
    fee_basis: str | None = None
    dividend_id: str | None = None
    split_factor: object | None = None


@dataclass(frozen=True)
class HistoryCoverage:
    period_start: str
    period_end: str
    expected_event_ids: tuple[str, ...] | None
    first_sequence: int
    transactions_complete: bool
    entitlements_complete: bool
    corporate_actions_complete: bool
    fees_complete: bool
    cost_method: str


@dataclass(frozen=True)
class HistoryResult:
    context: MetricContext
    instrument_id: str
    quantity_decimal: Decimal | None
    metrics: Mapping[str, FinancialMetric]
    event_trace: tuple[Mapping[str, object], ...]
    coverage_gaps: tuple[str, ...]

    def to_dict(self) -> dict:
        unavailable = [metric for metric in self.metrics.values() if metric.value_minor is None]
        summary_gaps = _unique(self.coverage_gaps + tuple(gap for metric in unavailable for gap in metric.coverage_gaps))
        return {"subject_id": self.context.subject_id, "currency": self.context.currency,
                "data_scope": self.context.data_scope, "instrument_id": self.instrument_id,
                "period_start": self.context.period_start, "period_end": self.context.period_end,
                "quantity_decimal": str(self.quantity_decimal) if self.quantity_decimal is not None else None,
                "calculation_status": "unavailable" if self.quantity_decimal is None else "partial" if unavailable else "calculated",
                "coverage_gaps": list(summary_gaps), "cost_method": "moving_average",
                "metrics": {name: metric.to_dict() for name, metric in self.metrics.items()},
                "event_trace": [dict(row) for row in self.event_trace]}


_HISTORY_METRICS = ("closing_holdings_value", "closing_equity", "remaining_cost", "closing_cash",
                    "closing_dividend_receivables", "owner_net_flow", "realized_sale_pnl",
                    "recognized_dividend_income", "external_fee_expense", "realized_pnl",
                    "unrealized_pnl", "period_pnl")
_CLOSING_STOCKS = {"closing_holdings_value", "closing_equity", "remaining_cost", "closing_cash",
                   "closing_dividend_receivables", "unrealized_pnl"}
_HISTORY_BASES = {
    "closing_holdings_value": "holdings_market_value",
    "closing_equity": "holdings_plus_cash_receivables_less_payables",
    "remaining_cost": "moving_average_capitalized_transaction_cost",
    "closing_cash": "settled_investment_cash",
    "closing_dividend_receivables": "recognized_unsettled_net_dividend_income",
    "owner_net_flow": "owner_contributions_less_withdrawals",
    "realized_sale_pnl": "net_sale_proceeds_less_allocated_average_cost",
    "recognized_dividend_income": "net_recognized_dividend_income",
    "external_fee_expense": "explicit_external_fees_not_nav_embedded",
    "realized_pnl": "sale_pnl_plus_net_dividend_less_external_expenses",
    "unrealized_pnl": "closing_holdings_value_less_remaining_cost",
    "period_pnl": "total_equity_period_pnl",
}


def reconstruct_history(context: MetricContext, opening: OpeningPosition,
                        events: Sequence[HoldingEvent], coverage: HistoryCoverage,
                        *, closing_unit_price: object | None) -> HistoryResult:
    """最小单份额历史重建。buy/sell 为同时确认并结算；不支持做空或延迟成交。

    买入交易费资本化到移动平均成本，卖出交易费减少卖出净收入；独立外部费用
    直接减少现金与已实现损益。分红确认计净收入及应收，到账只将应收转换为现金。
    split 仅改变份额；净值已内扣的费用事件记录为排除项，不能再扣现金。
    """
    if context.period_start is None or context.period_end is None:
        raise ValueError("historical_reconstruction_requires_period_context")
    gaps: list[str] = []
    excluded: list[str] = []
    if context.currency != "CNY":
        gaps.append("unsupported_currency_scale")
    if not context.coverage_complete:
        gaps.append("history_coverage_incomplete")
    if not opening.instrument_id:
        gaps.append("instrument_identity_missing")
    if opening.subject_id != context.subject_id or opening.currency != context.currency:
        gaps.append("opening_identity_mismatch")
    if opening.on != context.period_start or coverage.period_start != context.period_start or coverage.period_end != context.period_end:
        gaps.append("history_period_mismatch")
    _on(opening.on)
    if _on(coverage.period_start) >= _on(coverage.period_end):
        gaps.append("history_period_order_invalid")
    if coverage.cost_method != "moving_average":
        gaps.append("unsupported_cost_method")
    for name in ("transactions_complete", "entitlements_complete", "corporate_actions_complete", "fees_complete"):
        if getattr(coverage, name) is not True:
            gaps.append(name + "_required")
    if coverage.expected_event_ids is None:
        gaps.append("expected_event_identity_coverage_missing")
    elif tuple(e.event_id for e in events) != coverage.expected_event_ids:
        gaps.append("event_identity_coverage_mismatch")
    ids = [e.event_id for e in events]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        gaps.append("duplicate_or_missing_event_identity")
    if type(coverage.first_sequence) is not int or coverage.first_sequence < 1:
        raise ValueError("first_sequence 必须为正整数")
    for index, e in enumerate(events):
        if type(e.sequence) is not int or e.sequence != coverage.first_sequence + index:
            gaps.append("event_sequence_not_contiguous")
        if e.subject_id != context.subject_id or e.currency != context.currency or e.instrument_id != opening.instrument_id:
            gaps.append("event_identity_mismatch")
        when = _on(e.on)
        if not _on(coverage.period_start) < when <= _on(coverage.period_end):
            gaps.append("event_outside_covered_period")
        if index and e.on < events[index - 1].on:
            gaps.append("event_date_order_invalid")
    for name in ("quantity", "remaining_cost_minor", "cash_minor", "payables_minor", "dividend_receivables"):
        if getattr(opening, name) is None:
            gaps.append("opening_" + name + "_missing")

    def result(values: Mapping[str, int | None], quantity: Decimal | None,
               trace: Sequence[Mapping[str, object]], value_gaps: Mapping[str, tuple[str, ...]] | None = None) -> HistoryResult:
        metrics = {}
        for name in _HISTORY_METRICS:
            value = values.get(name)
            metric_gaps = tuple(gaps) if gaps else ((value_gaps or {}).get(name, ("valuation_missing",)) if value is None else ())
            metric_context = replace(context, as_of=context.period_end, period_start=None, period_end=None) if name in _CLOSING_STOCKS else context
            metrics[name] = money_metric(metric_context, name, _HISTORY_BASES[name], "returns.history." + name + ".v1",
                                         value, status="unavailable" if value is None else "calculated",
                                         gaps=_unique(metric_gaps), excluded=tuple(excluded))
        return HistoryResult(context, opening.instrument_id, quantity, metrics, tuple(trace), _unique(gaps))

    if gaps:
        return result({}, None, ())
    quantity = decimal_value(opening.quantity, "opening_quantity", nonnegative=True)
    cost = require_minor(opening.remaining_cost_minor, "remaining_cost_minor", nonnegative=True)
    cash = require_minor(opening.cash_minor, "cash_minor", nonnegative=True)
    payable = require_minor(opening.payables_minor, "payables_minor", nonnegative=True)
    receivables = {}
    for row in opening.dividend_receivables:
        if not row.dividend_id or row.dividend_id in receivables:
            gaps.append("duplicate_or_missing_opening_dividend_identity")
        receivables[row.dividend_id] = require_minor(row.amount_minor, "dividend_receivable", nonnegative=True)
    if quantity == 0 and cost != 0:
        gaps.append("zero_quantity_with_remaining_cost")
    if gaps:
        return result({}, None, ())
    opening_quantity = quantity
    opening_receivables = sum(receivables.values())
    used_dividends = set(receivables)
    fee_ids: set[str] = set()
    sales_pnl = dividends = expenses = contributions = withdrawals = 0
    trace: list[Mapping[str, object]] = []

    def amount(e: HoldingEvent, name: str) -> int:
        value = getattr(e, name)
        if value is None:
            raise ValueError(name + "_missing")
        return require_minor(value, name, nonnegative=True)

    def fee(e: HoldingEvent) -> int:
        value = amount(e, "fee_minor")
        if value:
            if not e.fee_id or e.fee_id in fee_ids:
                raise ValueError("duplicate_or_missing_fee_identity")
            if e.fee_basis != "external":
                raise ValueError("transaction_fee_basis_must_be_external")
            fee_ids.add(e.fee_id)
        return value

    try:
        with localcontext() as dec:
            dec.prec = 50
            for e in events:
                before = quantity
                allocated = 0
                allowed = {
                    "buy": {"quantity", "gross_minor", "fee_minor", "fee_id", "fee_basis"},
                    "sell": {"quantity", "gross_minor", "fee_minor", "fee_id", "fee_basis"},
                    "dividend_declared": {"gross_minor", "fee_minor", "fee_id", "fee_basis", "dividend_id"},
                    "dividend_settled": {"dividend_id", "fee_minor"},
                    "split": {"split_factor"},
                    "owner_contribution": {"gross_minor", "fee_minor"},
                    "owner_withdrawal": {"gross_minor", "fee_minor"},
                    "fee": {"gross_minor", "fee_id", "fee_basis"},
                }.get(e.kind)
                if allowed is None:
                    raise ValueError("unsupported_history_event")
                for field in ("quantity", "gross_minor", "fee_minor", "fee_id", "fee_basis", "dividend_id", "split_factor"):
                    if field not in allowed and getattr(e, field) is not None:
                        raise ValueError("unexpected_event_field:" + field)
                if e.kind in ("buy", "sell"):
                    q = decimal_value(e.quantity, "event_quantity", nonnegative=True)
                    if q <= 0:
                        raise ValueError("positive_trade_quantity_required")
                    gross, charge = amount(e, "gross_minor"), fee(e)
                    if gross <= 0 or e.kind == "sell" and charge > gross:
                        raise ValueError("invalid_trade_cash")
                    if e.kind == "buy":
                        cash -= gross + charge
                        cost += gross + charge
                        quantity += q
                    else:
                        if q > quantity:
                            raise ValueError("sale_exceeds_confirmed_quantity")
                        # 最后一笔卖出分配全部剩余成本，避免分币舍入残留。
                        allocated = cost if q == quantity else _minor_decimal(Decimal(cost) * q / quantity)
                        quantity -= q
                        cost -= allocated
                        cash += gross - charge
                        sales_pnl += gross - charge - allocated
                elif e.kind == "dividend_declared":
                    gross, charge = amount(e, "gross_minor"), fee(e)
                    if not e.dividend_id or e.dividend_id in used_dividends or charge > gross:
                        raise ValueError("dividend_identity_or_amount_invalid")
                    used_dividends.add(e.dividend_id)
                    receivables[e.dividend_id] = gross - charge
                    dividends += gross - charge
                elif e.kind == "dividend_settled":
                    if e.fee_minor is not None and require_minor(e.fee_minor, "fee_minor", nonnegative=True) != 0:
                        raise ValueError("dividend_settlement_must_not_charge_fee_twice")
                    if e.gross_minor is not None or e.dividend_id not in receivables:
                        raise ValueError("dividend_settlement_must_reference_unsettled_receivable")
                    cash += receivables.pop(e.dividend_id)
                elif e.kind == "split":
                    factor = decimal_value(e.split_factor, "split_factor", nonnegative=True)
                    if factor <= 0:
                        raise ValueError("positive_split_factor_required")
                    quantity *= factor
                elif e.kind in ("owner_contribution", "owner_withdrawal"):
                    value = amount(e, "gross_minor")
                    if e.fee_minor is not None and require_minor(e.fee_minor, "fee_minor", nonnegative=True) != 0:
                        raise ValueError("owner_flow_fee_requires_separate_event")
                    if e.kind == "owner_contribution":
                        cash += value
                        contributions += value
                    else:
                        cash -= value
                        withdrawals += value
                elif e.kind == "fee":
                    value = amount(e, "gross_minor")
                    if e.fee_minor is not None or not e.fee_id or e.fee_id in fee_ids:
                        raise ValueError("duplicate_or_missing_fee_identity")
                    fee_ids.add(e.fee_id)
                    if e.fee_basis == "nav_embedded":
                        excluded.append("净值已内扣费用：" + e.fee_id)
                    elif e.fee_basis == "external":
                        cash -= value
                        expenses += value
                    else:
                        raise ValueError("explicit_fee_basis_required")
                else:
                    raise ValueError("unsupported_history_event")
                if cash < 0:
                    raise ValueError("event_cash_exceeds_confirmed_cash")
                trace.append({"event_id": e.event_id, "sequence": e.sequence, "on": e.on,
                              "kind": e.kind, "quantity_before": str(before), "quantity_after": str(quantity),
                              "allocated_sale_cost_minor": allocated, "remaining_cost_minor": cost,
                              "cash_minor": cash, "dividend_receivables_minor": sum(receivables.values())})
    except ValueError as error:
        # 历史输入不完整/不满足合同，整体重建未知；不给部分事件伪装成全部历史。
        gaps.append(str(error))
        return result({}, None, ())
    closing_value = None if quantity != 0 and closing_unit_price is None else holding_value_minor(quantity, closing_unit_price)
    opening_value = None if opening_quantity != 0 and opening.unit_price is None else holding_value_minor(opening_quantity, opening.unit_price)
    closing_equity = None if closing_value is None else closing_value + cash + sum(receivables.values()) - payable
    opening_equity = None if opening_value is None else opening_value + opening.cash_minor + opening_receivables - payable
    pnl = None if closing_equity is None or opening_equity is None else closing_equity - opening_equity - contributions + withdrawals
    valuation_gaps = {}
    if closing_value is None:
        for name in ("closing_holdings_value", "closing_equity", "unrealized_pnl", "period_pnl"):
            valuation_gaps[name] = ("closing_price_missing",)
    if opening_value is None:
        valuation_gaps["period_pnl"] = (*valuation_gaps.get("period_pnl", ()), "opening_price_missing")
    return result({"closing_holdings_value": closing_value, "closing_equity": closing_equity,
                   "remaining_cost": cost, "closing_cash": cash, "closing_dividend_receivables": sum(receivables.values()),
                   "owner_net_flow": contributions - withdrawals, "realized_sale_pnl": sales_pnl,
                   "recognized_dividend_income": dividends, "external_fee_expense": expenses,
                   "realized_pnl": sales_pnl + dividends - expenses,
                   "unrealized_pnl": None if closing_value is None else closing_value - cost, "period_pnl": pnl},
                  quantity, trace, valuation_gaps)
