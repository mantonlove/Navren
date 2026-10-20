"""Versioned, side-effect-free research calculations.

The caller resolves facts from a trusted provider. A fact's ``verified`` label
and version binding are structural checks, NOT proof of provenance, permission,
currentness, insurability, or execution authority. No metric is issued here.
Money inputs are decimal major-unit strings; outputs are integer minor units.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import re

from financial_methods.contracts import decimal_value, iso_date

ALGORITHM_VERSION = "navren.research-methods/1"
MAX_MINOR = 9007199254740991
CENT = Decimal("0.01")
ZERO = Decimal(0)


class _InputError(ValueError):
    def __init__(self, path, reason, status="invalid"):
        self.path, self.reason, self.status = path, reason, status


def _need(obj, key, path=""):
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise _InputError(f"{path}.{key}".strip("."), "required_input_missing", "missing")
    return obj[key]


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise _InputError(path, "nonempty_string_required")
    return value


def _day(value, path):
    try:
        return iso_date(value, path)
    except (TypeError, ValueError):
        raise _InputError(path, "valid_ISO_calendar_date_required") from None


def _integer(value, path, maximum=100000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise _InputError(path, "nonnegative_bounded_integer_required")
    return value


def _decimal(value, path, signed=False):
    if (not isinstance(value, str) or len(value) > 50
            or not re.fullmatch(r"-?\d+(?:\.\d+)?", value)):
        raise _InputError(path, "plain_decimal_string_required")
    try:
        result = decimal_value(value, path, nonnegative=not signed)
    except ValueError:
        raise _InputError(path, "finite_decimal_out_of_range") from None
    return result


def _money(value, path, signed=False):
    result = _decimal(value, path, signed)
    if result != result.quantize(CENT) or abs(result * 100) > MAX_MINOR:
        raise _InputError(path, "money_requires_exact_minor_units_in_safe_range")
    return result


def _rate(value, path):
    result = _decimal(value, path)
    if result > 1:
        raise _InputError(path, "rate_requires_ratio_between_zero_and_one")
    return result


def _minor(value, path="calculation"):
    result = int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if abs(result) > MAX_MINOR:
        raise _InputError(path, "calculated_money_exceeds_safe_integer_range")
    return result


def _rounded(value):
    return Decimal(_minor(value)) / 100


def _list(value, path, maximum=1000):
    if not isinstance(value, list) or len(value) > maximum:
        raise _InputError(path, "bounded_array_required")
    return value


def _unique(rows, key, path):
    result = {}
    for i, row in enumerate(_list(rows, path)):
        ident = _text(_need(row, key, f"{path}[{i}]"), f"{path}[{i}].{key}")
        if ident in result:
            raise _InputError(path, "duplicate_identity")
        result[ident] = row
    return result


class _Context:
    def __init__(self, payload):
        self.payload = payload
        self.as_of = _day(_need(payload, "as_of"), "as_of")
        self.currency = _need(payload, "currency")
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise _InputError("currency", "explicit_ISO_currency_required_no_FX")
        self.versions = _need(payload, "evidence_versions")
        if (not isinstance(self.versions, dict) or not self.versions or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v
                for k, v in self.versions.items())):
            raise _InputError("evidence_versions", "explicit_nonempty_version_map_required")

    def fact(self, fact, path, **scope):
        state = _need(fact, "state", path)
        if state != "verified":
            status = state if state in {"missing", "conflict", "stale", "restricted", "superseded"} else "invalid"
            raise _InputError(path, "fact_not_verified", status)
        source = _text(_need(fact, "source_id", path), path + ".source_id")
        version = _text(_need(fact, "source_version", path), path + ".source_version")
        if self.versions.get(source) != version:
            raise _InputError(path, "evidence_version_mismatch", "conflict")
        for key, value in {"currency": self.currency, **scope}.items():
            if _need(fact, key, path) != value:
                raise _InputError(path + "." + key, "fact_scope_mismatch", "conflict")
        for key in ("effective_from", "effective_to"):
            if key not in fact:
                raise _InputError(path + "." + key, "explicit_date_or_null_required", "missing")
        start = _day(fact["effective_from"], path) if fact["effective_from"] is not None else None
        end = _day(fact["effective_to"], path) if fact["effective_to"] is not None else None
        if start and end and start > end:
            raise _InputError(path, "reversed_effective_interval")
        if start and self.as_of < start or end and self.as_of > end:
            raise _InputError(path, "fact_outside_effective_interval", "stale")
        return _need(fact, "value", path)


def _calculation(fn):
    def wrapped(payload: dict) -> dict:
        result = {"status": "complete", "rows": [], "exclusions": [], "missing": [],
                  "calculations": None, "evidence_versions": {},
                  "algorithm_version": ALGORITHM_VERSION, "execution_authority": False,
                  "scope": "conditional_research_not_execution_or_claim_approval"}
        try:
            with localcontext() as decimal_context:
                decimal_context.prec = 60
                if not isinstance(payload, dict):
                    raise _InputError("input", "JSON_object_required")
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
                result["input_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
                ctx = _Context(payload)
                result["evidence_versions"] = dict(ctx.versions)
                result["as_of"], result["currency"] = ctx.as_of.isoformat(), ctx.currency
                fn(payload, ctx, result)
        except _InputError as error:
            result.update(status=error.status, calculations=None, rows=[])
            result["missing"].append({"path": error.path, "reason": error.reason})
        except (InvalidOperation, OverflowError, TypeError, ValueError):
            result.update(status="invalid", calculations=None, rows=[])
            result["missing"].append({"path": "input", "reason": "invalid_numeric_or_JSON_input"})
        return result
    wrapped.__name__, wrapped.__doc__ = fn.__name__, fn.__doc__
    return wrapped


def _tier(rows, value, minimum, maximum, path, *, money_bounds=False):
    parsed = []
    for i, row in enumerate(_list(rows, path)):
        at = f"{path}[{i}]"
        low = _money(_need(row, minimum, at), at) if money_bounds else _integer(_need(row, minimum, at), at)
        upper = _need(row, maximum, at) if maximum not in row or row[maximum] is not None else None
        high = (_money(upper, at) if money_bounds else _integer(upper, at)) if upper is not None else None
        if high is not None and high <= low:
            raise _InputError(at, "empty_or_reversed_tier")
        parsed.append((low, high, row))
    parsed.sort(key=lambda item: item[0])
    for left, right in zip(parsed, parsed[1:]):
        if left[1] is None or right[0] < left[1]:
            raise _InputError(path, "overlapping_tiers")
    matches = [row for low, high, row in parsed if value >= low and (high is None or value < high)]
    if len(matches) != 1:
        raise _InputError(path, "applicable_fee_tier_missing", "missing")
    return matches[0]


@_calculation
def compare_share_costs(payload, ctx, out):
    """Compare 2–3 share/channel schedules; never predict future investment returns."""
    amount = _money(_need(payload, "amount"), "amount")
    if amount <= 0:
        raise _InputError("amount", "positive_investment_required")
    days = _integer(_need(payload, "holding_days"), "holding_days", 36500)
    channel = _text(_need(payload, "channel"), "channel")
    mode = _need(payload, "valuation_basis")
    if mode not in {"constant_gross_value", "observed_net_nav"}:
        raise _InputError("valuation_basis", "explicit_supported_valuation_basis_required")
    shares = _list(_need(payload, "shares"), "shares", 3)
    if len(shares) < 2:
        raise _InputError("shares", "two_or_three_distinct_shares_required")
    _unique(shares, "entity_id", "shares")
    for i, share in enumerate(shares):
        path = f"shares[{i}]"
        entity = share["entity_id"]
        try:
            fees = ctx.fact(_need(share, "fees", path), path + ".fees", entity_id=entity, channel=channel)
            subscription = _tier(_need(fees, "subscription", path), amount, "minimum", "maximum", path + ".subscription", money_bounds=True)
            method = _need(subscription, "method", path)
            value = _need(subscription, "value", path)
            if method == "external_rate":
                entry = _rounded(amount - amount / (1 + _rate(value, path)))
            elif method == "internal_rate":
                entry = _rounded(amount * _rate(value, path))
            elif method == "fixed":
                entry = _money(value, path)
            else:
                raise _InputError(path, "unsupported_subscription_method", "unsupported")
            if entry > amount:
                raise _InputError(path, "subscription_fee_exceeds_amount")
            net = amount - entry
            redemption = _tier(_need(fees, "redemption", path), days, "min_days", "max_days", path + ".redemption")
            exit_rate = _rate(_need(redemption, "rate", path), path)
            operating = _list(_need(fees, "ongoing", path), path + ".ongoing", 30)
            _unique(operating, "kind", path + ".ongoing")
            ongoing_rate = ZERO
            for fee in operating:
                ongoing_rate += _rate(_need(fee, "annual_rate", path), path)
                if type(_need(fee, "included_in_nav", path)) is not bool:
                    raise _InputError(path, "included_in_nav_requires_boolean")
            if mode == "observed_net_nav":
                if any(not fee["included_in_nav"] for fee in operating):
                    raise _InputError(path, "mixed_NAV_expense_treatment_not_supported", "unsupported")
                base = _money(ctx.fact(_need(share, "net_nav_value", path), path + ".net_nav_value", entity_id=entity, channel=channel), path)
                operating_cost = None
            else:
                if _need(fees, "expense_basis", path) != "net_subscription_constant":
                    raise _InputError(path, "explicit_constant_fee_base_required", "unsupported")
                basis = _need(fees, "day_basis", path)
                if type(basis) is not int or basis not in {365, 366}:
                    raise _InputError(path, "explicit_365_or_366_day_basis_required")
                operating_cost = _rounded(net * ongoing_rate * days / basis)
                base = net - operating_cost
                if base < 0:
                    raise _InputError(path, "fee_scenario_exhausts_principal", "unsupported")
            exit_fee = _rounded(base * exit_rate)
            out["rows"].append({"entity_id": entity, "status": "complete", "channel": channel, "currency": ctx.currency,
                "subscription_fee_minor": _minor(entry), "net_subscription_minor": _minor(net),
                "ongoing_fee_estimate_minor": _minor(operating_cost) if operating_cost is not None else None,
                "ongoing_treatment": "constant_base_simple_proration" if operating_cost is not None else "embedded_in_NAV_not_deducted_again",
                "redemption_fee_minor": _minor(exit_fee), "scenario_proceeds_minor": _minor(base - exit_fee),
                "explicit_transaction_cost_minor": _minor(entry + exit_fee),
                "valuation_basis": mode, "future_return_prediction": False})
        except _InputError as error:
            out["rows"].append({"entity_id": entity, "status": error.status, "scenario_proceeds_minor": None})
            out["missing"].append({"path": error.path, "reason": error.reason})
    failures = [row["status"] for row in out["rows"] if row["status"] != "complete"]
    out["status"] = (failures[0] if len(failures) == len(shares) and len(set(failures)) == 1
                     else "partial" if failures else "complete")
    out["calculations"] = {"amount_minor": _minor(amount), "holding_days": days,
        "assumptions": ["current_fee_schedule_unchanged_for_scenario", "not_a_future_return_forecast"],
        "ranking": None, "cross_source_or_currency_ranking_performed": False}


@_calculation
def reserve_obligations(payload, ctx, out):
    """Reserve confirmed rent/premiums and actual held funds exactly once."""
    if _need(payload, "cash_basis") != "ledger_cash_before_reservations":
        raise _InputError("cash_basis", "cash_before_held_reservations_required")
    until = _day(_need(payload, "until"), "until")
    if until < ctx.as_of:
        raise _InputError("until", "horizon_precedes_as_of")
    cash = _money(ctx.fact(_need(payload, "available_cash"), "available_cash"), "available_cash")
    obligations = ctx.fact(_need(payload, "obligations"), "obligations")
    if _need(obligations, "complete", "obligations") is not True:
        raise _InputError("obligations.complete", "obligation_inventory_incomplete", "missing")
    obligations = _unique(_need(obligations, "items", "obligations"), "obligation_id", "obligations.items")
    holds = ctx.fact(_need(payload, "reservations"), "reservations")
    if _need(holds, "complete", "reservations") is not True:
        raise _InputError("reservations.complete", "reservation_inventory_incomplete", "missing")
    holds = _unique(_need(holds, "items", "reservations"), "reservation_id", "reservations.items")
    linked, all_held = {}, ZERO
    for key, hold in holds.items():
        amount = _money(_need(hold, "amount", key), key)
        state = _need(hold, "status", key)
        if state not in {"held", "released"}:
            raise _InputError(key, "unknown_reservation_state", "missing")
        if "obligation_id" not in hold:
            raise _InputError(key + ".obligation_id", "explicit_identity_or_null_required", "missing")
        ident = hold["obligation_id"]
        if ident is not None and ident not in obligations:
            raise _InputError(key, "reservation_obligation_not_in_inventory", "conflict")
        if state == "held":
            all_held += amount
            if ident is not None:
                linked[ident] = linked.get(ident, ZERO) + amount
    incremental = ZERO
    for key, item in obligations.items():
        amount = _money(_need(item, "amount", key), key)
        due = _day(_need(item, "due_date", key), key)
        state = _need(item, "status", key)
        if state not in {"confirmed", "paid", "cancelled"}:
            raise _InputError(key, "unconfirmed_obligation_state", "missing")
        if state != "confirmed" or due > until:
            out["exclusions"].append({"obligation_id": key, "reason": "settled_cancelled_or_outside_horizon"})
            continue
        extra = max(ZERO, amount - linked.get(key, ZERO))
        incremental += extra
        out["rows"].append({"obligation_id": key, "due_date": due.isoformat(), "overdue": due < ctx.as_of,
            "amount_minor": _minor(amount), "already_held_minor": _minor(linked.get(key, ZERO)),
            "additional_reserve_minor": _minor(extra)})
    needed = all_held + incremental
    out["calculations"] = {"cash_minor": _minor(cash), "held_minor": _minor(all_held),
        "additional_reserve_minor": _minor(incremental), "required_reserve_minor": _minor(needed),
        "uncommitted_cash_minor": _minor(max(ZERO, cash - needed)),
        "shortfall_minor": _minor(max(ZERO, needed - cash)), "until_inclusive": until.isoformat(),
        "date_assumption": "all_unpaid_overdue_and_due_through_until; no_holiday_or_receipt_date_inference"}


def _limit(value, path):
    if value == "unlimited":
        return Decimal("Infinity")
    return _money(value, path)


def _progressive(value, tiers, path):
    previous, payout = ZERO, ZERO
    rows = _list(tiers, path, 100)
    if not rows:
        raise _InputError(path, "reimbursement_tiers_missing", "missing")
    for i, row in enumerate(rows):
        upper = _need(row, "up_to", path) if "up_to" not in row or row["up_to"] is not None else None
        high = _money(upper, path) if upper is not None else Decimal("Infinity")
        if high <= previous or upper is None and i != len(rows) - 1:
            raise _InputError(path, "invalid_progressive_tier_order")
        rate = _rate(_need(row, "rate", path), path)
        payout += max(ZERO, min(value, high) - previous) * rate
        previous = high
    if value > previous:
        raise _InputError(path, "reimbursement_tier_coverage_missing", "missing")
    return _rounded(payout)


@_calculation
def check_coverage_scenario(payload, ctx, out):
    """Conditional contractual arithmetic, never an underwriting/claims decision."""
    policy = _text(_need(payload, "policy_id"), "policy_id")
    terms = ctx.fact(_need(payload, "terms"), "terms", entity_id=policy)
    scenario = _need(payload, "scenario")
    event = _day(_need(scenario, "event_date", "scenario"), "scenario.event_date")
    start, end = (_day(_need(terms, key, "terms"), "terms." + key) for key in ("policy_start", "policy_end"))
    waiting_end = _day(_need(terms, "waiting_end", "terms"), "terms.waiting_end")
    if end < start or waiting_end < start or waiting_end > end:
        raise _InputError("terms", "inconsistent_policy_dates")
    usage_start = _day(_need(terms, "usage_period_start", "terms"), "usage_period_start")
    usage_end = _day(_need(terms, "usage_period_end", "terms"), "usage_period_end")
    if usage_start > usage_end:
        raise _InputError("terms", "reversed_annual_usage_period")
    if not usage_start <= event <= usage_end:
        raise _InputError("terms", "annual_usage_not_bound_to_event_period", "missing")
    inclusive = _need(terms, "waiting_end_inclusive", "terms")
    if type(inclusive) is not bool:
        raise _InputError("terms.waiting_end_inclusive", "boolean_required")
    if event < start or event > end or event < waiting_end or event == waiting_end and inclusive:
        out.update(status="ineligible", calculations={"expense_compensation_minor": 0, "fixed_benefit_minor": 0,
            "reason": "outside_policy_period_or_waiting_period"})
        return
    conditions = _need(scenario, "conditions", "scenario")
    for key in ("in_force", "covered_event", "exclusions_cleared"):
        value = _need(conditions, key, "scenario.conditions")
        if type(value) is not bool:
            raise _InputError("scenario.conditions." + key, "explicit_scenario_boolean_required", "missing")
        if value is False:
            out.update(status="ineligible", calculations={"expense_compensation_minor": 0,
                "fixed_benefit_minor": 0, "reason": "scenario_condition_not_met:" + key})
            return
    order = _need(terms, "deduction_order", "terms")
    if order not in (["other_reimbursements", "deductible", "ratio", "limits"],
                     ["deductible", "other_reimbursements", "ratio", "limits"]):
        raise _InputError("terms.deduction_order", "unsupported_explicit_deduction_order", "unsupported")
    expenses = _unique(_need(scenario, "costs", "scenario"), "cost_id", "scenario.costs")
    balances = {}
    for key, item in expenses.items():
        gross = _money(_need(item, "amount", key), key)
        paid = _money(_need(item, "other_reimbursements", key), key)
        if paid > gross:
            raise _InputError(key, "other_reimbursements_exceed_expense")
        balances[key] = [gross, paid]
    groups = _unique(_need(terms, "shared_limits", "terms"), "group_id", "shared_limits")
    remaining_groups = {}
    for key, group in groups.items():
        limit, used = _limit(_need(group, "limit", key), key), _money(_need(group, "used", key), key)
        if used > limit:
            raise _InputError(key, "used_exceeds_limit")
        remaining_groups[key] = limit - used
    deductibles = _unique(_need(terms, "deductibles", "terms"), "deductible_id", "deductibles")
    remaining_deductibles = {}
    for key, item in deductibles.items():
        amount, used = _money(_need(item, "amount", key), key), _money(_need(item, "used", key), key)
        if _need(item, "period", key) not in {"claim", "annual"} or used > amount:
            raise _InputError(key, "invalid_deductible_period_or_usage")
        remaining_deductibles[key] = amount - used
    policy_limit = _limit(_need(terms, "policy_annual_limit", "terms"), "policy_annual_limit")
    policy_used = _money(_need(terms, "policy_annual_used", "terms"), "policy_annual_used")
    if policy_used > policy_limit:
        raise _InputError("terms", "policy_usage_exceeds_limit")
    policy_remaining = policy_limit - policy_used
    responsibilities = _unique(_need(terms, "responsibilities", "terms"), "responsibility_id", "responsibilities")
    payment_order = _list(_need(terms, "payment_order", "terms"), "payment_order")
    if len(payment_order) != len(responsibilities) or set(payment_order) != set(responsibilities):
        raise _InputError("payment_order", "explicit_complete_unique_responsibility_order_required")
    totals = {"expense_compensation": ZERO, "fixed_benefit": ZERO}
    for ident in payment_order:
        item = responsibilities[ident]
        mechanism = _need(item, "mechanism", ident)
        group_id, deductible_id = _need(item, "shared_limit_id", ident), _need(item, "deductible_id", ident)
        if group_id != "none" and group_id not in groups or deductible_id != "none" and deductible_id not in deductibles:
            raise _InputError(ident, "unknown_limit_or_deductible_identity")
        annual = _limit(_need(item, "annual_limit", ident), ident)
        used = _money(_need(item, "annual_used", ident), ident)
        if used > annual:
            raise _InputError(ident, "responsibility_usage_exceeds_limit")
        per_claim = _limit(_need(item, "per_claim_limit", ident), ident)
        cost_ids = _list(_need(item, "cost_ids", ident), ident + ".cost_ids")
        if len(set(cost_ids)) != len(cost_ids) or any(key not in balances for key in cost_ids):
            raise _InputError(ident, "duplicate_or_unknown_cost_identity")
        if mechanism == "fixed_benefit":
            if cost_ids or deductible_id != "none":
                raise _InputError(ident, "fixed_benefit_must_not_use_expense_or_deductible")
            if ident not in _list(_need(scenario, "fixed_events", "scenario"), "scenario.fixed_events"):
                candidate = ZERO
            else:
                candidate = _money(_need(item, "benefit_amount", ident), ident)
        elif mechanism == "expense_compensation":
            provider = _need(conditions, "provider_eligible", "scenario.conditions")
            if type(provider) is not bool:
                raise _InputError("scenario.conditions.provider_eligible", "explicit_scenario_boolean_required", "missing")
            if not provider:
                out["rows"].append({"responsibility_id": ident, "mechanism": mechanism,
                                    "conditional_payment_minor": 0, "reason": "provider_not_eligible"})
                continue
            gross = sum((balances[key][0] for key in cost_ids), ZERO)
            prior_paid = sum((balances[key][1] for key in cost_ids), ZERO)
            deduction_base = max(ZERO, gross - prior_paid) if order[0] == "other_reimbursements" else gross
            deductible = min(deduction_base, remaining_deductibles.get(deductible_id, ZERO))
            if deductible_id != "none":
                remaining_deductibles[deductible_id] -= deductible
            eligible = max(ZERO, gross - prior_paid - deductible)
            candidate = min(eligible, _progressive(eligible, _need(item, "reimbursement_tiers", ident), ident))
        else:
            raise _InputError(ident, "unknown_payment_mechanism", "unsupported")
        payout = min(candidate, annual - used, per_claim, policy_remaining,
                     remaining_groups.get(group_id, Decimal("Infinity")))
        payout = _rounded(payout)
        policy_remaining -= payout
        if group_id != "none":
            remaining_groups[group_id] -= payout
        totals[mechanism] += payout
        if mechanism == "expense_compensation":
            left = payout
            for key in cost_ids:
                paid = min(left, balances[key][0] - balances[key][1])
                balances[key][1] += paid
                left -= paid
        out["rows"].append({"responsibility_id": ident, "mechanism": mechanism,
                            "conditional_payment_minor": _minor(payout)})
    out["calculations"] = {"expense_compensation_minor": _minor(totals["expense_compensation"]),
        "fixed_benefit_minor": _minor(totals["fixed_benefit"]), "claim_approved": False,
        "conditions_are_explicit_scenario_assumptions": True,
        "deduction_order": order, "cost_allocation_order": "explicit_cost_ids_order"}


def _irr(rows, basis):
    combined = {}
    for row in rows:
        on, amount = row["date"], Decimal(row["amount_minor"]) / 100
        combined[on] = combined.get(on, ZERO) + amount
    flows = [(day, amount) for day, amount in sorted(combined.items()) if amount]
    signs = [amount > 0 for _, amount in flows]
    if not flows or not any(signs) or all(signs):
        return {"status": "unavailable", "value": None, "reason": "both_cashflow_signs_required"}
    if signs[0] or sum(a != b for a, b in zip(signs, signs[1:])) != 1:
        return {"status": "ambiguous", "value": None, "reason": "nonconventional_cashflow_multiple_roots_not_selected"}
    origin = _day(flows[0][0], "cashflow.date")
    timed = [(Decimal((_day(day, "cashflow.date") - origin).days) / basis, amount) for day, amount in flows]

    def npv(rate):
        return sum((amount / ((1 + rate) ** years) for years, amount in timed), ZERO)

    low, high = Decimal("-0.9999999999"), Decimal(1)
    if npv(0) == 0:
        return {"status": "calculated", "value": "0.000000000000", "day_basis": basis}
    for _ in range(64):
        if npv(low) >= 0 and npv(high) <= 0:
            break
        high = high * 2 + 1
    else:
        return {"status": "unavailable", "value": None, "reason": "root_not_bracketed"}
    for _ in range(180):
        mid = (low + high) / 2
        if npv(mid) > 0:
            low = mid
        else:
            high = mid
    return {"status": "calculated", "value": format(((low + high) / 2).quantize(Decimal("0.000000000001")), "f"), "day_basis": basis}


@_calculation
def pension_cashflows(payload, ctx, out):
    """Keep guaranteed and illustrative benefits separate; dated IRR is conditional."""
    entity = _text(_need(payload, "entity_id"), "entity_id")
    schedule = ctx.fact(_need(payload, "cashflow_schedule"), "cashflow_schedule", entity_id=entity)
    complete = _need(schedule, "complete", "cashflow_schedule")
    if type(complete) is not bool:
        raise _InputError("cashflow_schedule.complete", "boolean_required")
    end = _day(_need(schedule, "projection_end", "cashflow_schedule"), "projection_end")
    basis = _need(payload, "day_basis")
    if type(basis) is not int or basis not in {365, 366}:
        raise _InputError("day_basis", "explicit_365_or_366_day_basis_required")
    flows = _unique(_need(schedule, "items", "cashflow_schedule"), "cashflow_id", "cashflow_schedule.items")
    guaranteed, illustrative = [], []
    for ident, item in flows.items():
        on = _day(_need(item, "date", ident), ident)
        amount = _money(_need(item, "amount", ident), ident, signed=True)
        kind = _need(item, "kind", ident)
        guarantee = _need(item, "guaranteed", ident)
        if type(guarantee) is not bool or kind not in {"premium", "benefit", "terminal_value"}:
            raise _InputError(ident, "explicit_cashflow_kind_and_guarantee_required")
        if on > end or kind == "premium" and (amount >= 0 or not guarantee) or kind != "premium" and amount < 0:
            raise _InputError(ident, "cashflow_date_sign_or_guarantee_inconsistent")
        row = {"cashflow_id": ident, "date": on.isoformat(), "amount_minor": _minor(amount), "kind": kind}
        (guaranteed if guarantee else illustrative).append(row)
        out["rows"].append({**row, "guaranteed": guarantee})
    terminal = [r for r in out["rows"] if r["kind"] == "terminal_value"]
    if terminal and (len(terminal) != 1 or terminal[0]["date"] != end.isoformat()):
        raise _InputError("cashflow_schedule", "terminal_value_must_be_unique_and_at_projection_end")
    if complete and (len(terminal) != 1 or terminal[0]["guaranteed"] is not True):
        raise _InputError("cashflow_schedule", "complete_schedule_requires_explicit_guaranteed_terminal_value", "missing")
    irr_missing = {"status": "missing", "value": None, "reason": "cashflow_schedule_incomplete"}
    out["status"] = "complete" if complete else "partial"
    if not complete:
        out["missing"].append({"path": "cashflow_schedule.complete", "reason": "incomplete_schedule_no_IRR"})
    out["calculations"] = {"guaranteed_cashflows": guaranteed, "non_guaranteed_cashflows": illustrative,
        "guaranteed_net_minor": sum(row["amount_minor"] for row in guaranteed) if complete else None,
        "illustrative_increment_minor": sum(row["amount_minor"] for row in illustrative) if complete else None,
        "known_guaranteed_net_minor": sum(row["amount_minor"] for row in guaranteed),
        "known_illustrative_increment_minor": sum(row["amount_minor"] for row in illustrative),
        "guaranteed_IRR": _irr(guaranteed, basis) if complete else dict(irr_missing),
        "illustrative_IRR": _irr(guaranteed + illustrative, basis) if complete else dict(irr_missing),
        "illustrative_is_guaranteed": False, "tax_benefit_assumed": False}
    for key in ("known_guaranteed_net_minor", "known_illustrative_increment_minor"):
        if abs(out["calculations"][key]) > MAX_MINOR:
            raise _InputError(key, "aggregate_money_exceeds_safe_integer_range")
