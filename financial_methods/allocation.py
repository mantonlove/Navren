"""已确认用途的资金安排基线；纯计算，不读账户、不生成订单。

目标金额是当前拟安排预算，不是承诺的未来赎回金额。floor_surplus 已扣除
应急与生效占用；已占用目标金额只计覆盖，不能再次从该余量扣除。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from itertools import groupby
from typing import Optional

from .contracts import FinancialMetric, MetricContext, decimal_value, money_metric, require_minor


@dataclass(frozen=True)
class ProductKey:
    product_id: str
    share_class_id: str
    channel_id: str


@dataclass(frozen=True)
class AllocationGoal:
    subject_id: str
    currency: str
    data_scope: str
    goal_id: str
    amount_minor: Optional[int]
    priority: Optional[int]
    needed_on: Optional[str]
    confirmed: bool
    input_refs: tuple[str, ...]
    source_versions: tuple[tuple[str, str], ...]
    product_key: Optional[ProductKey] = None  # None 表示明确保留现金用于该目标。
    already_occupied_minor: int = 0
    occupancy_refs: tuple[str, ...] = ()
    occupancy_verified_in_baseline: bool = False


@dataclass(frozen=True)
class FormalRisk:
    subject_id: str
    currency: str
    data_scope: str
    level: Optional[int]
    confirmed: bool
    effective_on: str
    valid_through: str
    input_refs: tuple[str, ...]
    source_versions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProductTerms:
    # 主体和范围由基座绑定，指本次资格核验范围；公共来源仍单独保留。
    subject_id: str
    currency: str
    data_scope: str
    key: ProductKey
    identity_verified: bool
    current_sale_verified: bool
    terms_complete: bool
    risk_level: Optional[int]
    minimum_minor: Optional[int]
    step_minor: Optional[int]
    entry_fixed_minor: Optional[int]
    entry_rate: Optional[str]
    exit_fixed_minor: Optional[int]
    exit_rate: Optional[str]
    fee_rounding: Optional[str]
    fee_basis: Optional[str]
    exit_available_on: Optional[str]
    evidence_as_of: str
    valid_through: str
    input_refs: tuple[str, ...]
    source_versions: tuple[tuple[str, str], ...]


ROUNDING = {'half_up': ROUND_HALF_UP, 'ceiling': ROUND_CEILING, 'floor': ROUND_FLOOR}


def _day(value):
    if not isinstance(value, str):
        raise ValueError('date_required')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('date_must_be_iso_day')
    return parsed


def _evidence(obj):
    try:
        return (isinstance(obj.input_refs, (tuple, list)) and bool(obj.input_refs)
                and all(isinstance(r, str) and r.strip() for r in obj.input_refs)
                and isinstance(obj.source_versions, (tuple, list)) and bool(obj.source_versions)
                and all(isinstance(pair, (tuple, list)) and len(pair) == 2
                        and all(isinstance(v, str) and v.strip() for v in pair)
                        for pair in obj.source_versions))
    except (TypeError, ValueError):
        return False


def _context(funds, sources=()):
    refs = list(funds.input_refs)
    versions = dict(funds.source_versions)
    for obj in sources:
        if not _evidence(obj):
            raise ValueError('invalid_evidence_container')
        refs.extend(obj.input_refs)
        occupancy_refs = getattr(obj, 'occupancy_refs', ())
        if not isinstance(occupancy_refs, (tuple, list)):
            raise ValueError('invalid_occupancy_ref_container')
        refs.extend(occupancy_refs)
        for source, version in obj.source_versions:
            if source in versions and versions[source] != version:
                raise ValueError('source_version_conflict:' + source)
            versions[source] = version
    return MetricContext(subject_id=funds.subject_id, currency=funds.currency,
                         data_scope=funds.data_scope, as_of=funds.as_of,
                         period_start=funds.period_start, period_end=funds.period_end,
                         input_refs=tuple(dict.fromkeys(refs)),
                         source_versions=tuple(versions.items()),
                         coverage_complete=not bool(funds.coverage_gaps))


def _bound(obj, funds):
    return (obj.subject_id == funds.subject_id and obj.currency == funds.currency
            and obj.data_scope == funds.data_scope)


def _valid_key(key):
    return (isinstance(key, ProductKey)
            and all(isinstance(v, str) and v.strip()
                    for v in (key.product_id, key.share_class_id, key.channel_id)))


def _fee(principal, fixed, rate, rounding):
    if not isinstance(rounding, str) or rounding not in ROUNDING:
        raise ValueError('fee_rounding_invalid')
    fixed = require_minor(fixed, 'fee_fixed_minor', nonnegative=True)
    rate = decimal_value(rate, 'fee_rate', nonnegative=True)
    if rate > 1:
        raise ValueError('fee_rate_exceeds_one')
    return fixed + int((Decimal(principal) * rate).quantize(Decimal('1'), rounding=ROUNDING[rounding]))


def _plan(context, plan_id, funds_minor, lines=(), *, status='calculated',
          feasibility='feasible', gaps=(), goal_states=()):
    known = funds_minor is not None
    investment = sum(x['amount_minor'] for x in lines if x['product_key'] is not None) if known else None
    cash = sum(x['amount_minor'] for x in lines if x['product_key'] is None) if known else None
    fees = sum(x['entry_fee_minor'] for x in lines) if known else None
    reserve = sum(x['exit_fee_reserve_minor'] for x in lines) if known else None
    remaining = funds_minor - investment - fees if known else None
    unassigned = remaining - cash - reserve if known else None
    values = {'investment_principal': investment, 'assigned_cash': cash,
              'entry_fees': fees, 'exit_fee_reserve': reserve,
              'remaining_cash': remaining, 'unassigned_cash': unassigned}
    metrics = {name: money_metric(context, name, 'planned_' + name,
                                 'allocation.' + plan_id + '/1', value, status=status,
                                 gaps=tuple(gaps),
                                 assumptions=({'kind': 'method_limit',
                                               'future_proceeds': 'not_projected',
                                               'execution': 'planning_only'},)).to_dict()
               for name, value in values.items()}
    return {'plan_id': plan_id,
            'method_id': 'retain_cash/1' if plan_id == 'retain_cash' else 'confirmed_priority_full_amounts/1',
            'calculation_status': status, 'feasibility': feasibility,
            'allocations': list(lines), 'goal_states': list(goal_states),
            'metrics': metrics, 'coverage_gaps': list(gaps),
            'conservation_difference_minor': (funds_minor - investment - fees - remaining) if known else None,
            'cash_partition_difference_minor': (remaining - cash - reserve - unassigned) if known else None,
            'future_proceeds_minor': None, 'execution_authority': False}


def arrange_funds(funds: FinancialMetric, goals: tuple[AllocationGoal, ...],
                  products: tuple[ProductTerms, ...] = (),
                  risk: Optional[FormalRisk] = None) -> dict:
    """返回保留现金和按明确优先级安排两方案。

    不推断目标、权重、收益、风险等级或产品。按优先级全额安排；同优先级
    的组整体可行才安排，资金不足不通过对象 ID 选择赢家或改成模板比例。
    费率仅支持按安排本金计算；赎回费预留在现金中，不假装已经支付。
    """
    context = _context(funds)
    errors, pending = [], []
    if funds.basis != 'floor_surplus' or funds.formula_id != 'funds.current_budget/1':
        errors.append('funds_basis_mismatch')
    if funds.calculation_status != 'calculated' or funds.value_minor is None:
        pending.append('funds_not_calculated')
    if funds.coverage_gaps or not _evidence(funds):
        pending.append('funds_coverage_or_evidence_missing')
    if not isinstance(goals, (tuple, list)) or not all(isinstance(g, AllocationGoal) for g in goals):
        errors.append('goal_container_invalid')
    if not isinstance(products, (tuple, list)) or not all(isinstance(p, ProductTerms) for p in products):
        errors.append('product_container_invalid')
    if risk is not None and not isinstance(risk, FormalRisk):
        errors.append('risk_input_type_invalid')
    amount = None
    if funds.value_minor is not None:
        try:
            amount = require_minor(funds.value_minor, 'funds.value_minor')
            if amount < 0:
                errors.append('negative_floor_surplus')
                amount = None
        except ValueError:
            errors.append('invalid_funds_amount')
    if errors or pending:
        status = 'infeasible' if errors else 'unavailable'
        gaps = tuple(errors + pending)
        return {'schema_version': 1, 'funds': funds.to_dict(), 'plans': [
            _plan(context, p, None, status=status, feasibility='infeasible' if errors else 'pending_dependencies', gaps=gaps)
            for p in ('retain_cash', 'confirmed_uses')]}

    retained = _plan(context, 'retain_cash', amount)
    unique, occupancy_owner = {}, {}
    for goal in goals:
        if not isinstance(goal.goal_id, str) or not goal.goal_id.strip():
            errors.append('goal_id_invalid')
            continue
        if goal.goal_id in unique:
            if unique[goal.goal_id] != goal:
                errors.append('conflicting_duplicate_goal:' + goal.goal_id)
            continue
        unique[goal.goal_id] = goal
        if goal.product_key is not None and not _valid_key(goal.product_key):
            errors.append('product_key_invalid:' + goal.goal_id)
        if not _bound(goal, funds):
            errors.append('goal_context_mismatch:' + goal.goal_id)
        if not isinstance(goal.goal_id, str) or not goal.goal_id.strip() or goal.confirmed is not True:
            pending.append('goal_not_confirmed:' + goal.goal_id)
        if not _evidence(goal):
            pending.append('goal_evidence_missing:' + goal.goal_id)
        try:
            require_minor(goal.amount_minor, 'goal.amount_minor', nonnegative=True)
            occupied = require_minor(goal.already_occupied_minor, 'already_occupied_minor', nonnegative=True)
            if occupied > goal.amount_minor:
                errors.append('occupied_exceeds_goal:' + goal.goal_id)
            if occupied and (goal.occupancy_verified_in_baseline is not True or not goal.occupancy_refs):
                pending.append('baseline_occupancy_unverified:' + goal.goal_id)
            if not isinstance(goal.occupancy_refs, (tuple, list)):
                raise ValueError('invalid_occupancy_ref_container')
            if any(not isinstance(ref, str) or not ref.strip() for ref in goal.occupancy_refs):
                raise ValueError('invalid_occupancy_ref')
            if len(set(goal.occupancy_refs)) != len(goal.occupancy_refs):
                errors.append('duplicate_occupancy_ref:' + goal.goal_id)
            for ref in goal.occupancy_refs:
                if not isinstance(ref, str) or not ref:
                    errors.append('invalid_occupancy_ref:' + goal.goal_id)
                if ref in occupancy_owner:
                    errors.append('occupancy_reused:' + ref)
                occupancy_owner[ref] = goal.goal_id
        except (ValueError, TypeError):
            pending.append('goal_amount_missing_or_invalid:' + goal.goal_id)
        if type(goal.priority) is not int or goal.priority < 0:
            pending.append('goal_priority_missing_or_invalid:' + goal.goal_id)
        try:
            needed = _day(goal.needed_on)
            start = _day(funds.as_of or funds.period_start)
            if needed < start or (funds.period_end and needed > _day(funds.period_end)):
                errors.append('goal_outside_funds_period:' + goal.goal_id)
        except (ValueError, TypeError):
            pending.append('goal_date_missing_or_invalid:' + goal.goal_id)

    terms = {}
    for index, product in enumerate(products):
        if not _valid_key(product.key):
            errors.append('product_key_invalid:terms_' + str(index))
            continue
        if product.key in terms and terms[product.key] != product:
            errors.append('conflicting_product_terms:' + product.key.product_id)
        terms[product.key] = product
    used = []
    if not unique:
        pending.append('confirmed_goals_missing')
    for goal in unique.values():
        if goal.product_key is None:
            continue
        if not _valid_key(goal.product_key):
            continue
        # 已从基线扣除且足额覆盖的目标没有新增投资需求，不重新虚构申购资格。
        if type(goal.amount_minor) is int and goal.amount_minor == goal.already_occupied_minor:
            continue
        product = terms.get(goal.product_key)
        if product is None:
            pending.append('product_terms_missing:' + goal.goal_id)
            continue
        used.append(product)
        if not _bound(product, funds):
            errors.append('product_context_mismatch:' + goal.goal_id)
        if (product.identity_verified is not True
                or not all(isinstance(v, str) and v.strip() for v in
                           (product.key.product_id, product.key.share_class_id, product.key.channel_id))):
            pending.append('product_identity_unverified:' + goal.goal_id)
        if product.current_sale_verified is not True:
            pending.append('current_sale_unverified:' + goal.goal_id)
        if product.terms_complete is not True or not _evidence(product):
            pending.append('product_terms_or_evidence_missing:' + goal.goal_id)
        if (product.fee_basis != 'allocated_principal' or not isinstance(product.fee_rounding, str)
                or product.fee_rounding not in ROUNDING):
            pending.append('fee_basis_or_rounding_unsupported:' + goal.goal_id)
        try:
            minimum = require_minor(product.minimum_minor, 'minimum_minor', nonnegative=True)
            step = require_minor(product.step_minor, 'step_minor', nonnegative=True)
            if minimum == 0 or step == 0:
                raise ValueError('minimum_or_step_zero')
            _fee(0, product.entry_fixed_minor, product.entry_rate, product.fee_rounding)
            _fee(0, product.exit_fixed_minor, product.exit_rate, product.fee_rounding)
        except (ValueError, KeyError, TypeError):
            pending.append('product_amount_or_fees_missing_or_invalid:' + goal.goal_id)
        try:
            decision_on = _day(funds.as_of or funds.period_start)
            disclosed, through = _day(product.evidence_as_of), _day(product.valid_through)
            exit_on = _day(product.exit_available_on)
            if disclosed > decision_on or through < decision_on or through < disclosed:
                pending.append('product_evidence_not_current:' + goal.goal_id)
            if exit_on < decision_on:
                errors.append('exit_date_before_allocation:' + goal.goal_id)
            if exit_on > _day(goal.needed_on):
                errors.append('exit_after_use_date:' + goal.goal_id)
        except (ValueError, TypeError):
            pending.append('product_dates_missing_or_invalid:' + goal.goal_id)
        if risk is None or risk.confirmed is not True or not _evidence(risk):
            pending.append('formal_risk_missing:' + goal.goal_id)
        else:
            if not _bound(risk, funds):
                errors.append('risk_context_mismatch:' + goal.goal_id)
            if type(risk.level) is not int or not 1 <= risk.level <= 5 or type(product.risk_level) is not int or not 1 <= product.risk_level <= 5:
                pending.append('risk_level_missing_or_invalid:' + goal.goal_id)
            elif product.risk_level > risk.level:
                errors.append('product_risk_ineligible:' + goal.goal_id)
            try:
                if not _day(risk.effective_on) <= decision_on <= _day(risk.valid_through):
                    pending.append('formal_risk_not_current:' + goal.goal_id)
            except (ValueError, TypeError):
                pending.append('formal_risk_dates_invalid:' + goal.goal_id)

    try:
        sources = tuple(unique.values()) + tuple(used) + ((risk,) if used and risk is not None else ())
        context = _context(funds, sources)
    except (ValueError, TypeError):
        pending.append('source_version_conflict_or_invalid')
    if errors or pending:
        gaps = tuple(dict.fromkeys(errors + pending))
        plan = _plan(context, 'confirmed_uses', None,
                     status='infeasible' if errors else 'unavailable',
                     feasibility='infeasible' if errors else 'pending_dependencies', gaps=gaps)
        return {'schema_version': 1, 'funds': funds.to_dict(), 'plans': [retained, plan]}

    lines, states, shortfalls, available = [], [], [], amount
    for priority, group in groupby(sorted(unique.values(), key=lambda g: g.priority), key=lambda g: g.priority):
        candidates, group_cost, group_gaps = [], 0, []
        for goal in group:
            principal = goal.amount_minor - goal.already_occupied_minor
            state = {'goal_id': goal.goal_id, 'already_occupied_minor': goal.already_occupied_minor,
                     'occupancy_status': 'coverage_only' if goal.already_occupied_minor else 'none',
                     'new_required_minor': principal, 'priority': priority}
            if not principal:
                states.append({**state, 'status': 'already_covered'})
                continue
            product = terms.get(goal.product_key)
            if product and (principal < product.minimum_minor or (principal - product.minimum_minor) % product.step_minor):
                group_gaps.append('minimum_or_step_conflict:' + goal.goal_id)
            entry = _fee(principal, product.entry_fixed_minor, product.entry_rate, product.fee_rounding) if product else 0
            exit_fee = _fee(principal, product.exit_fixed_minor, product.exit_rate, product.fee_rounding) if product else 0
            candidates.append({'goal_id': goal.goal_id, 'amount_minor': principal,
                               'product_key': vars(goal.product_key).copy() if goal.product_key else None,
                               'entry_fee_minor': entry, 'exit_fee_reserve_minor': exit_fee,
                               'needed_on': goal.needed_on, 'priority': priority,
                               'already_occupied_minor': goal.already_occupied_minor,
                               'occupancy_status': state['occupancy_status']})
            group_cost += principal + entry + exit_fee
        if group_cost > available:
            group_gaps.append('priority_group_budget_shortfall:' + str(priority))
        if group_gaps:
            shortfalls.extend(group_gaps)
            states.extend({'goal_id': x['goal_id'], 'status': 'not_funded', 'priority': priority} for x in candidates)
            # 优先事项未满足时，不越过它将资金交给较低优先事项。
            break
        lines.extend(candidates)
        states.extend({'goal_id': x['goal_id'], 'status': 'planned', 'priority': priority} for x in candidates)
        available -= group_cost
    funded = {x['goal_id'] for x in states}
    states.extend({'goal_id': g.goal_id, 'status': 'blocked_by_higher_priority', 'priority': g.priority}
                  for g in unique.values() if g.goal_id not in funded)
    plan = _plan(context, 'confirmed_uses', amount, lines,
                 status='partial' if shortfalls else 'calculated',
                 feasibility='infeasible' if shortfalls else 'feasible',
                 gaps=tuple(shortfalls), goal_states=states)
    return {'schema_version': 1, 'funds': funds.to_dict(), 'plans': [retained, plan]}
