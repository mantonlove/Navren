"""资产负债与实际现金收支分类；不将现金流等同权责发生制利润。"""
from dataclasses import dataclass
from .contracts import bind_inputs, iso_date, money_metric, require_minor


@dataclass(frozen=True)
class BalanceComponent:
    object_id: str
    subject_id: str
    currency: str
    as_of: str
    kind: str
    amount_minor: int | None
    basis: str
    evidence_ref: str
    source_version: str


def balance_sheet(context, components, *, expected_object_ids):
    """同一时点账面资产−未偿负债；财产账面价值不成为可用现金。"""
    try:
        if context.as_of is None:
            raise ValueError("balance_sheet_requires_as_of")
        # 证据绑定与金额分类使用同一份输入，不能先消费一次性迭代器。
        components = tuple(components)
        context = bind_inputs(context, components)
        allowed = {"cash": {"ledger_cash_balance"}, "financial_asset": {"market_value", "carrying_value"},
                   "nonfinancial_asset": {"carrying_value"}, "liability": {"outstanding_principal", "payable"}}
        ids = [c.object_id for c in components]
        if not expected_object_ids or len(set(expected_object_ids)) != len(expected_object_ids):
            raise ValueError("expected_object_scope_missing_or_duplicate")
        if len(set(ids)) != len(ids) or set(ids) != set(expected_object_ids):
            raise ValueError("missing_or_duplicate_balance_component")
        totals = {kind: 0 for kind in allowed}
        for c in components:
            if c.subject_id != context.subject_id or c.currency != context.currency:
                raise ValueError("balance_component_identity_mismatch")
            if c.as_of != context.as_of:
                raise ValueError("balance_component_date_mismatch")
            if c.kind not in allowed or c.basis not in allowed[c.kind]:
                raise ValueError("unsupported_valuation_basis")
            totals[c.kind] += require_minor(c.amount_minor, c.object_id, nonnegative=True)
        assets = totals["cash"] + totals["financial_asset"] + totals["nonfinancial_asset"]
        rows = {"total_assets": (assets, "book_assets"), "total_liabilities": (totals["liability"], "book_liabilities"),
                "net_assets": (assets - totals["liability"], "book_net_assets"),
                "cash_balance": (totals["cash"], "ledger_cash_balance")}
        return {key: money_metric(context, key, basis, "accounting.balance_sheet/1", value)
                for key, (value, basis) in rows.items()}
    except (ValueError, TypeError, AttributeError) as error:
        return {key: money_metric(context, key, basis, "accounting.balance_sheet/1", None,
                                  status="unavailable", gaps=(str(error),))
                for key, basis in [("total_assets", "book_assets"), ("total_liabilities", "book_liabilities"),
                                   ("net_assets", "book_net_assets"), ("cash_balance", "ledger_cash_balance")]}


@dataclass(frozen=True)
class CashMovement:
    movement_id: str
    subject_id: str
    account_id: str
    currency: str
    on: str
    kind: str
    delta_minor: int | None
    evidence_ref: str
    source_version: str
    related_id: str | None = None


def cash_statement(context, opening_cash_minor, closing_cash_minor, movements):
    """仅对完整的已发生现金流水分类；本金流量、退款与收入分别命名。"""
    basis = {
        "cash_income": "recorded_cash_income", "cash_expenses_gross": "recorded_cash_expenses",
        "cash_refunds": "recorded_cash_refunds", "net_cash_expenses": "recorded_cash_expenses_net_refunds",
        "operating_cash_surplus": "recorded_cash_income_minus_net_expenses",
        "total_cash_change": "recorded_cash_flow", "loan_principal_net": "loan_principal_cash_flow",
        "investment_transaction_cash_net": "investment_transaction_cash_flow_not_principal_pnl_split",
    }
    try:
        if context.as_of is not None or not context.coverage_complete:
            raise ValueError("cash_statement_requires_complete_period")
        # 净流量为零时，仅核对期初期末不能发现迭代器已经被消费。
        movements = tuple(movements)
        context = bind_inputs(context, movements)
        opening = require_minor(opening_cash_minor, "opening_cash", nonnegative=True)
        closing = require_minor(closing_cash_minor, "closing_cash", nonnegative=True)
        allowed = {"income", "expense", "refund", "loan_proceeds", "loan_principal_repayment",
                   "loan_interest", "investment_purchase", "investment_redemption", "internal_transfer",
                   "owner_contribution", "owner_withdrawal"}
        seen, totals, transfers = set(), {k: 0 for k in allowed}, {}
        for m in movements:
            if not m.movement_id or m.movement_id in seen:
                raise ValueError("duplicate_cash_movement")
            seen.add(m.movement_id)
            if m.subject_id != context.subject_id or m.currency != context.currency:
                raise ValueError("cash_movement_identity_mismatch")
            if not context.period_start <= iso_date(m.on).isoformat() <= context.period_end:
                raise ValueError("cash_movement_outside_period")
            if m.kind not in allowed:
                raise ValueError("unsupported_cash_kind_nonfinancial_depreciation_is_not_fund_loss")
            value = require_minor(m.delta_minor, m.movement_id)
            if m.kind in {"income", "refund", "loan_proceeds", "investment_redemption", "owner_contribution"} and value < 0:
                raise ValueError("cash_direction_conflict")
            if m.kind in {"expense", "loan_principal_repayment", "loan_interest", "investment_purchase", "owner_withdrawal"} and value > 0:
                raise ValueError("cash_direction_conflict")
            if m.kind == "refund" and not m.related_id:
                raise ValueError("refund_requires_original_expense_reference")
            if m.kind == "internal_transfer":
                if not m.related_id:
                    raise ValueError("internal_transfer_requires_pair_id")
                transfers.setdefault(m.related_id, []).append(m)
            totals[m.kind] += value
        for pair in transfers.values():
            if len(pair) != 2 or pair[0].account_id == pair[1].account_id or sum(m.delta_minor for m in pair) != 0:
                raise ValueError("incomplete_internal_transfer_pair")
        change = sum(totals.values())
        if opening + change != closing:
            raise ValueError("cash_reconciliation_failed")
        expense = -totals["expense"] - totals["loan_interest"]
        values = {"cash_income": totals["income"], "cash_expenses_gross": expense, "cash_refunds": totals["refund"],
                  "net_cash_expenses": expense - totals["refund"],
                  "operating_cash_surplus": totals["income"] - expense + totals["refund"],
                  "total_cash_change": change,
                  "loan_principal_net": totals["loan_proceeds"] + totals["loan_principal_repayment"],
                  "investment_transaction_cash_net": totals["investment_purchase"] + totals["investment_redemption"]}
        excluded = ("owner_flows_are_not_income", "borrowing_and_repayment_principal_are_not_income_or_expense",
                    "investment_redemption_requires_separate_cost_and_pnl_reconstruction",
                    "internal_transfers_are_not_consumption", "noncash_depreciation_is_not_investment_loss",
                    "cash_statement_is_not_accrual_profit")
        return {key: money_metric(context, key, basis[key], "accounting.cash_statement/1", value, excluded=excluded)
                for key, value in values.items()}
    except (ValueError, TypeError, AttributeError) as error:
        return {key: money_metric(context, key, value, "accounting.cash_statement/1", None,
                                  status="unavailable", gaps=(str(error),)) for key, value in basis.items()}
