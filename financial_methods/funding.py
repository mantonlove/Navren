"""对基座已展开、已核验的资金输入做纯算术；不生成账单/周期事项。"""
from dataclasses import dataclass, replace
from datetime import timedelta
from .contracts import CONTRACT_VERSION, bind_inputs, iso_date, money_metric, require_minor


@dataclass(frozen=True)
class CashSnapshot:
    subject_id: str
    currency: str
    as_of: str
    balance_minor: int | None
    frozen_minor: int | None
    emergency_floor_minor: int | None
    coverage_start: str
    coverage_end: str
    evidence_ref: str
    source_version: str
    basis: str = "ledger_cash_balance"
    coverage_complete: bool = True


@dataclass(frozen=True)
class FundingEvent:
    event_id: str
    subject_id: str
    currency: str
    on: str
    amount_minor: int | None
    direction: str
    certainty: str
    evidence_ref: str
    source_version: str
    kind: str = "obligation"


@dataclass(frozen=True)
class Hold:
    hold_id: str
    subject_id: str
    currency: str
    amount_minor: int | None
    evidence_ref: str
    source_version: str
    # 每个金额是可用于结算该确切义务的预留份额；不能凭同金额关联。
    commitment_allocations: tuple[tuple[str, int], ...] = ()
    release_on_payment: bool = False
    release_on: str | None = None


@dataclass(frozen=True)
class PlanningReserve:
    reserve_id: str
    subject_id: str
    currency: str
    amount_minor: int | None
    evidence_ref: str
    source_version: str
    commitment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioShock:
    event_id: str
    delay_days: int = 0
    amount_increase_minor: int = 0


@dataclass(frozen=True)
class FundingResult:
    status: str
    metrics: dict
    checkpoints: tuple
    planning_reserves: tuple
    floor_judgments: dict
    excluded_items: tuple
    coverage_gaps: tuple
    mode: str = "recorded_items_projection"

    def to_dict(self):
        return {"contract_version": CONTRACT_VERSION, "status": self.status, "mode": self.mode,
                "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
                "checkpoints": list(self.checkpoints), "planning_reserves": list(self.planning_reserves),
                "floor_judgments": self.floor_judgments, "excluded_items": list(self.excluded_items),
                "coverage_gaps": list(self.coverage_gaps)}


def _unique(records, id_field):
    seen = {}
    for record in records:
        key = getattr(record, id_field)
        if not isinstance(key, str) or not key:
            raise ValueError("输入必须有稳定ID")
        if key in seen and seen[key] != record:
            raise ValueError(f"conflicting_duplicate:{key}")
        seen[key] = record
    return tuple(seen[key] for key in sorted(seen))


def _identity(context, record):
    if record.subject_id != context.subject_id:
        raise ValueError("subject_mismatch")
    if record.currency != context.currency:
        raise ValueError("currency_mismatch")


_NAMES = {
    "cash_balance": ("cash_balance", "ledger_cash_balance", "funds.balance/1"),
    "current_available": ("current_available", "floor_surplus", "funds.available/1"),
    "current_budget": ("arrangeable_now", "floor_surplus", "funds.current_budget/1"),
    "scenario_budget": ("planning_arrangeable_now", "planning_floor_surplus", "funds.planning_budget/1"),
    "confirmed_minimum": ("minimum_after_confirmed", "floor_surplus", "funds.daily_minimum/1"),
    "candidate_minimum": ("minimum_if_candidates_occur", "floor_surplus", "funds.daily_minimum_candidates/1"),
    "confirmed_closing_cash": ("projected_cash_after_confirmed", "projected_cash_balance", "funds.closing_cash/1"),
    "candidate_closing_cash": ("projected_cash_if_candidates_occur", "projected_cash_balance", "funds.closing_cash_candidates/1"),
}


def _failed(context, reason, assumptions=()):
    metrics = {key: money_metric(context, name, basis, formula, None, status="unavailable",
                                gaps=(reason,), assumptions=assumptions)
               for key, (name, basis, formula) in _NAMES.items()}
    return FundingResult("unavailable", metrics, (), (), {}, (), (reason,))


def evaluate_funding(context, snapshot, events=(), holds=(), planning_reserves=(), *, assumptions=()):
    """期间包括期初快照及截至日；日终净额，不声称日内付款可行。

    current_budget不提前花未来已确认流入。minimum则按各日期加入已确认
    流入，并独立显示候选分支。预测、历史和用户假设不进入事实分支。
    """
    if context.as_of is not None:
        return _failed(context, "funding_requires_period", assumptions)
    try:
        events, holds = _unique(events, "event_id"), _unique(holds, "hold_id")
        reserves = _unique(planning_reserves, "reserve_id")
        context = bind_inputs(context, (snapshot, *events, *holds, *reserves))
        for record in (snapshot, *events, *holds, *reserves):
            _identity(context, record)
        if snapshot.basis != "ledger_cash_balance":
            raise ValueError("requires_gross_balance_not_already_deducted_surplus")
        start, end = iso_date(context.period_start), iso_date(context.period_end)
        if iso_date(snapshot.as_of) != start:
            raise ValueError("opening_snapshot_date_mismatch")
        if type(snapshot.coverage_complete) is not bool or not snapshot.coverage_complete or not context.coverage_complete:
            raise ValueError("input_coverage_incomplete")
        if iso_date(snapshot.coverage_start) > start or iso_date(snapshot.coverage_end) < end:
            raise ValueError("requested_period_outside_input_coverage")
        balance = require_minor(snapshot.balance_minor, "balance", nonnegative=True)
        frozen = require_minor(snapshot.frozen_minor, "frozen", nonnegative=True)
        floor = require_minor(snapshot.emergency_floor_minor, "emergency_floor", nonnegative=True)
        if frozen > balance:
            raise ValueError("frozen_exceeds_cash_balance")
        active, excluded = [], []
        by_id = {e.event_id: e for e in events}
        for event in events:
            day = iso_date(event.on)
            if event.direction not in {"inflow", "outflow"}:
                raise ValueError("invalid_event_direction")
            if event.certainty not in {"confirmed", "candidate", "forecast", "assumption", "historical"}:
                raise ValueError("invalid_event_certainty")
            if day < start or day > end or event.certainty in {"forecast", "assumption", "historical"}:
                excluded.append(f"{event.event_id}:{event.certainty}:not_counted")
                continue
            require_minor(event.amount_minor, event.event_id, nonnegative=True)
            active.append(event)
        active_confirmed = {e.event_id: e for e in active if e.certainty == "confirmed" and e.direction == "outflow"}
        hold_state, linked_total = {}, {}
        for hold in holds:
            amount = require_minor(hold.amount_minor, hold.hold_id, nonnegative=True)
            if type(hold.release_on_payment) is not bool:
                raise ValueError("hold_release_rule_missing")
            release = iso_date(hold.release_on) if hold.release_on is not None else None
            allocations = dict(hold.commitment_allocations)
            if len(allocations) != len(hold.commitment_allocations):
                raise ValueError("duplicate_link_within_hold")
            for event_id, amount_linked in allocations.items():
                require_minor(amount_linked, "hold_link", nonnegative=True)
                event = by_id.get(event_id)
                if event is None or event.direction != "outflow" or event.certainty != "confirmed":
                    raise ValueError("unknown_or_unconfirmed_hold_link")
                require_minor(event.amount_minor, "linked_obligation", nonnegative=True)
                if hold.release_on_payment:
                    linked_total[event_id] = linked_total.get(event_id, 0) + amount_linked
                    if linked_total[event_id] > event.amount_minor:
                        raise ValueError("linked_holds_exceed_obligation")
            if sum(allocations.values()) > amount:
                raise ValueError("hold_allocations_exceed_hold")
            if release is not None and release <= start:
                excluded.append(f"{hold.hold_id}:released_before_opening")
                continue
            hold_state[hold.hold_id] = {"hold": hold, "remaining": amount, "release": release,
                                        "allocations": allocations}
        held = sum(s["remaining"] for s in hold_state.values())
        if held + frozen > balance:
            raise ValueError("holds_and_frozen_exceed_cash_balance")
        due = sum(e.amount_minor for e in active_confirmed.values())
        linked_credit = 0
        for s in hold_state.values():
            if not s["hold"].release_on_payment:
                continue
            for event_id, amount in s["allocations"].items():
                event = active_confirmed.get(event_id)
                if event is not None and (s["release"] is None or s["release"] >= iso_date(event.on)):
                    linked_credit += amount
        initial = balance - frozen - held - floor
        budget = initial - due + linked_credit
        # 一个义务的抵扣信用最多消费一次，按稳定ID分配；不是按金额相同猜测。
        credits = {key: e.amount_minor for key, e in active_confirmed.items()}
        reserve_rows, extra = [], 0
        for reserve in reserves:
            amount = require_minor(reserve.amount_minor, reserve.reserve_id, nonnegative=True)
            if len(set(reserve.commitment_ids)) != len(reserve.commitment_ids):
                raise ValueError("duplicate_commitment_within_reserve")
            if any(key not in credits for key in reserve.commitment_ids):
                raise ValueError("unknown_or_out_of_period_reserve_link")
            covered = 0
            for key in sorted(reserve.commitment_ids):
                take = min(credits[key], amount - covered)
                credits[key] -= take
                covered += take
            additional = amount - covered
            extra += additional
            reserve_rows.append({"reserve_id": reserve.reserve_id, "target_minor": amount,
                                 "already_counted_commitment_minor": covered, "additional_minor": additional,
                                 "kind": "planning_only_no_bank_hold"})
        cash_c = cash_e = balance
        min_c = min_e = initial
        min_c_date = min_e_date = start.isoformat()
        checkpoints = []
        dates = {iso_date(e.on) for e in active}
        dates |= {s["release"] for s in hold_state.values() if s["release"] is not None and s["release"] <= end}
        for day in sorted(dates):
            today = [e for e in active if iso_date(e.on) == day]
            for e in today:
                delta = e.amount_minor if e.direction == "inflow" else -e.amount_minor
                if e.certainty == "confirmed":
                    cash_c += delta
                cash_e += delta
                if e.event_id in active_confirmed:
                    for s in hold_state.values():
                        if s["hold"].release_on_payment and s["remaining"]:
                            used = min(s["remaining"], s["allocations"].get(e.event_id, 0))
                            s["remaining"] -= used
            for s in hold_state.values():
                if s["release"] is not None and s["release"] <= day:
                    s["remaining"] = 0
            remaining_held = sum(s["remaining"] for s in hold_state.values())
            available_c, available_e = cash_c - frozen - remaining_held - floor, cash_e - frozen - remaining_held - floor
            if available_c < min_c:
                min_c, min_c_date = available_c, day.isoformat()
            if available_e < min_e:
                min_e, min_e_date = available_e, day.isoformat()
            checkpoints.append({"on": day.isoformat(), "confirmed_cash_minor": cash_c,
                                "cash_if_candidates_minor": cash_e, "remaining_hold_minor": remaining_held,
                                "confirmed_floor_surplus_minor": available_c,
                                "candidate_floor_surplus_minor": available_e})
        point_context = replace(context, as_of=snapshot.as_of, period_start=None, period_end=None)
        reserve_assumptions = tuple({"reserve_id": r["reserve_id"], "target_minor": r["target_minor"],
                                     "type": "user_planning_allocation"} for r in reserve_rows)
        values = {"cash_balance": balance, "current_available": initial, "current_budget": budget,
                  "scenario_budget": budget - extra, "confirmed_minimum": min_c, "candidate_minimum": min_e,
                  "confirmed_closing_cash": cash_c, "candidate_closing_cash": cash_e}
        metrics = {}
        for key, (name, basis, formula) in _NAMES.items():
            ctx = point_context if key in {"cash_balance", "current_available"} else context
            assumptions_here = (*assumptions, *reserve_assumptions) if key == "scenario_budget" else assumptions
            metrics[key] = money_metric(ctx, name, basis, formula, values[key], excluded=tuple(excluded),
                                        assumptions=assumptions_here)
        judgments = {"confirmed_floor_breached": min_c < 0, "confirmed_floor_touched": min_c == 0,
                     "candidate_floor_breached": min_e < 0, "candidate_floor_touched": min_e == 0,
                     "confirmed_shortfall_minor": max(0, -min_c), "candidate_shortfall_minor": max(0, -min_e),
                     "confirmed_minimum_on": min_c_date, "candidate_minimum_on": min_e_date,
                     "adequacy": "unknown_without_living_costs_income_stability_and_goals",
                     "intraday_solvency": "not_assessed_daily_net_only"}
        return FundingResult("calculated", metrics, tuple(checkpoints), tuple(reserve_rows), judgments,
                             tuple(excluded), (), "hypothetical_scenario" if assumptions else "recorded_items_projection")
    except (ValueError, TypeError, AttributeError) as error:
        return _failed(context, str(error), assumptions)


def stress_funding(context, snapshot, events=(), holds=(), planning_reserves=(), *, shocks=()):
    """仅改变明确事项的日期/金额；幅度是用户假设，不产生预测概率或写操作。"""
    try:
        event_map = {e.event_id: e for e in _unique(events, "event_id")}
        changes = []
        seen = set()
        for shock in shocks:
            if shock.event_id in seen or shock.event_id not in event_map:
                raise ValueError("unknown_or_duplicate_shock_event")
            seen.add(shock.event_id)
            if type(shock.delay_days) is not int or shock.delay_days < 0:
                raise ValueError("delay_days_requires_nonnegative_integer")
            require_minor(shock.amount_increase_minor, "amount_increase", nonnegative=True)
            event = event_map[shock.event_id]
            if event.certainty != "confirmed":
                raise ValueError("stress_requires_confirmed_event_not_forecast")
            require_minor(event.amount_minor, "event_amount", nonnegative=True)
            adjusted = replace(event, on=(iso_date(event.on) + timedelta(days=shock.delay_days)).isoformat(),
                               amount_minor=event.amount_minor + shock.amount_increase_minor)
            event_map[event.event_id] = adjusted
            changes.append({"event_id": event.event_id, "type": "user_stress_assumption",
                            "original_on": event.on, "assumed_on": adjusted.on,
                            "original_amount_minor": event.amount_minor, "assumed_amount_minor": adjusted.amount_minor,
                            "probability": None})
        if not changes:
            raise ValueError("stress_requires_explicit_assumptions")
        return evaluate_funding(context, snapshot, tuple(event_map.values()), holds, planning_reserves,
                                assumptions=tuple(changes))
    except (ValueError, TypeError) as error:
        return _failed(context, str(error))
