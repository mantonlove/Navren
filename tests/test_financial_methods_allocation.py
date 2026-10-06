"""预期金额为手算常量，测试不读取数据库、模型或交易服务。"""
from dataclasses import replace
import unittest

from financial_methods.contracts import MetricContext, money_metric
from financial_methods.allocation import (
    AllocationGoal, FormalRisk, ProductKey, ProductTerms, arrange_funds,
)


KEY = ProductKey('product-a', 'share-a', 'channel-a')


def budget(value=100_000, **changes):
    context = MetricContext('person-a', 'CNY', 'verified-fixture', as_of='2026-10-06',
                            input_refs=('funds-evidence',), source_versions=(('funds', 'v1'),))
    metric = money_metric(context, 'arrangeable_funds', 'floor_surplus',
                          'funds.current_budget/1', value,
                          status='unavailable' if value is None else 'calculated',
                          gaps=('missing_balance',) if value is None else ())
    return replace(metric, **changes)


def goal(amount=60_000, **changes):
    defaults = dict(subject_id='person-a', currency='CNY', data_scope='verified-fixture',
                    goal_id='investment-goal', amount_minor=amount, priority=1,
                    needed_on='2026-11-06', confirmed=True,
                    input_refs=('goal-evidence',), source_versions=(('goals', 'v1'),),
                    product_key=KEY)
    defaults.update(changes)
    return AllocationGoal(**defaults)


def product(**changes):
    defaults = dict(subject_id='person-a', currency='CNY', data_scope='verified-fixture',
                    key=KEY, identity_verified=True, current_sale_verified=True,
                    terms_complete=True, risk_level=1, minimum_minor=10_000,
                    step_minor=10_000, entry_fixed_minor=100, entry_rate='0.01',
                    exit_fixed_minor=0, exit_rate='0.02', fee_rounding='half_up',
                    fee_basis='allocated_principal', exit_available_on='2026-11-01',
                    evidence_as_of='2026-10-06', valid_through='2026-10-06',
                    input_refs=('terms-evidence',), source_versions=(('terms', 'v1'),))
    defaults.update(changes)
    return ProductTerms(**defaults)


def formal_risk(**changes):
    defaults = dict(subject_id='person-a', currency='CNY', data_scope='verified-fixture',
                    level=2, confirmed=True, effective_on='2026-10-01',
                    valid_through='2027-10-01', input_refs=('risk-evidence',),
                    source_versions=(('risk', 'v1'),))
    defaults.update(changes)
    return FormalRisk(**defaults)


def uses(result):
    return result['plans'][1]


def values(plan):
    return {name: metric['value_minor'] for name, metric in plan['metrics'].items()}


class AllocationBaselineTests(unittest.TestCase):
    def test_two_plans_and_hand_calculated_fee_cash_conservation(self):
        cash_goal = goal(20_000, goal_id='near-term-cash', priority=0, product_key=None)
        result = arrange_funds(budget(), (cash_goal, goal()), (product(),), formal_risk())
        cash, plan = result['plans']
        self.assertEqual(values(cash), {
            'investment_principal': 0, 'assigned_cash': 0, 'entry_fees': 0,
            'exit_fee_reserve': 0, 'remaining_cash': 100_000, 'unassigned_cash': 100_000})
        # 60,000本金；申购费100 + 60,000×1%=700；退出费预留1,200。
        # 剩余现金100,000−60,000−700=39,300；未分配39,300−20,000−1,200=18,100。
        self.assertEqual(values(plan), {
            'investment_principal': 60_000, 'assigned_cash': 20_000, 'entry_fees': 700,
            'exit_fee_reserve': 1_200, 'remaining_cash': 39_300, 'unassigned_cash': 18_100})
        self.assertEqual(plan['feasibility'], 'feasible')
        self.assertEqual(plan['conservation_difference_minor'], 0)
        self.assertEqual(plan['cash_partition_difference_minor'], 0)
        self.assertIsNone(plan['future_proceeds_minor'])
        self.assertFalse(plan['execution_authority'])
        m = plan['metrics']['investment_principal']
        self.assertEqual(m['source_versions'], {'funds': 'v1', 'goals': 'v1', 'risk': 'v1', 'terms': 'v1'})
        self.assertEqual(m['subject_id'], 'person-a')

    def test_true_zero_is_calculated_not_missing(self):
        result = arrange_funds(budget(0), (goal(0, product_key=None),))
        self.assertEqual(uses(result)['calculation_status'], 'calculated')
        self.assertEqual(values(uses(result))['unassigned_cash'], 0)
        missing = arrange_funds(budget(None), (goal(0, product_key=None),))
        self.assertEqual(uses(missing)['calculation_status'], 'unavailable')
        self.assertIsNone(values(uses(missing))['unassigned_cash'])

    def test_missing_goal_amount_priority_or_date_is_not_inferred(self):
        for field in ('amount_minor', 'priority', 'needed_on'):
            with self.subTest(field=field):
                plan = uses(arrange_funds(budget(), (replace(goal(), **{field: None}),),
                                         (product(),), formal_risk()))
                self.assertEqual(plan['calculation_status'], 'unavailable')
                self.assertEqual(plan['allocations'], [])
        self.assertEqual(uses(arrange_funds(budget(), ()))['feasibility'], 'pending_dependencies')

    def test_context_mismatch_refuses_cross_subject_currency_scope(self):
        for changes in ({'subject_id': 'person-b'}, {'currency': 'USD'}, {'data_scope': 'other-fixture'}):
            with self.subTest(changes=changes):
                self.assertEqual(uses(arrange_funds(budget(), (goal(**changes),),
                                                   (product(),), formal_risk()))['feasibility'], 'infeasible')
                self.assertEqual(uses(arrange_funds(budget(), (goal(),),
                                                   (product(**changes),), formal_risk()))['feasibility'], 'infeasible')
        self.assertEqual(uses(arrange_funds(budget(), (goal(),), (product(),),
                                           formal_risk(subject_id='person-b')))['feasibility'], 'infeasible')

    def test_duplicate_goal_and_already_deducted_occupancy_are_not_double_counted(self):
        target = goal(100_000, product_key=None, already_occupied_minor=30_000,
                      occupancy_refs=('active-hold-1',), occupancy_verified_in_baseline=True)
        # floor_surplus=70,000 已扣占用30,000；目标100,000只再安排70,000。
        plan = uses(arrange_funds(budget(70_000), (target, target)))
        self.assertEqual(len(plan['allocations']), 1)
        self.assertEqual(values(plan)['assigned_cash'], 70_000)
        self.assertEqual(values(plan)['unassigned_cash'], 0)
        self.assertEqual(plan['allocations'][0]['occupancy_status'], 'coverage_only')
        self.assertEqual(plan['allocations'][0]['already_occupied_minor'], 30_000)

    def test_duplicate_goal_conflict_or_reused_occupancy_is_explicit(self):
        target = goal(50_000, product_key=None, already_occupied_minor=20_000,
                      occupancy_refs=('hold-1',), occupancy_verified_in_baseline=True)
        conflict = uses(arrange_funds(budget(), (target, replace(target, amount_minor=60_000))))
        self.assertIn('conflicting_duplicate_goal:investment-goal', conflict['coverage_gaps'])
        reused = uses(arrange_funds(budget(), (target, replace(target, goal_id='another-goal'))))
        self.assertIn('occupancy_reused:hold-1', reused['coverage_gaps'])
        unknown = uses(arrange_funds(budget(), (replace(target, occupancy_verified_in_baseline=False),)))
        self.assertEqual(unknown['calculation_status'], 'unavailable')

    def test_fully_covered_goal_does_not_require_a_new_purchase(self):
        target = goal(30_000, already_occupied_minor=30_000,
                      occupancy_refs=('hold-1',), occupancy_verified_in_baseline=True)
        plan = uses(arrange_funds(budget(70_000), (target,)))
        self.assertEqual(plan['calculation_status'], 'calculated')
        self.assertEqual(plan['goal_states'][0]['status'], 'already_covered')
        self.assertEqual(values(plan)['unassigned_cash'], 70_000)
        self.assertIn('hold-1', plan['metrics']['unassigned_cash']['input_refs'])

    def test_missing_current_identity_terms_sale_and_fee_are_dependencies(self):
        for changes in ({'identity_verified': False}, {'current_sale_verified': False},
                        {'terms_complete': False}, {'entry_rate': None},
                        {'fee_rounding': None}, {'fee_basis': 'future_redemption_value'}):
            with self.subTest(changes=changes):
                plan = uses(arrange_funds(budget(), (goal(),), (product(**changes),), formal_risk()))
                self.assertEqual(plan['calculation_status'], 'unavailable')
                self.assertEqual(plan['allocations'], [])
        other_share = ProductKey('product-a', 'share-b', 'channel-a')
        self.assertIn('product_terms_missing:investment-goal',
                      uses(arrange_funds(budget(), (goal(product_key=other_share),),
                                         (product(),), formal_risk()))['coverage_gaps'])

    def test_fee_rounding_and_exact_budget_boundary(self):
        for rounding, expected_fee in (('half_up', 1), ('ceiling', 1), ('floor', 0)):
            with self.subTest(rounding=rounding):
                terms = product(minimum_minor=1, step_minor=1, entry_fixed_minor=0,
                                entry_rate='0.005', exit_rate='0', fee_rounding=rounding)
                # 105×0.005=0.525分；按三种已确认舍入规则分别为1、1、0分。
                plan = uses(arrange_funds(budget(106), (goal(105),), (terms,), formal_risk()))
                self.assertEqual(values(plan)['entry_fees'], expected_fee)
                self.assertEqual(values(plan)['unassigned_cash'], 1 - expected_fee)
        insufficient = uses(arrange_funds(budget(105), (goal(105),),
                                           (replace(terms, fee_rounding='half_up'),),
                                           formal_risk()))
        self.assertEqual(insufficient['feasibility'], 'infeasible')
        self.assertEqual(insufficient['allocations'], [])

    def test_minimum_and_increment_do_not_silently_round_target(self):
        for amount in (5_000, 25_000):
            plan = uses(arrange_funds(budget(), (goal(amount),), (product(),), formal_risk()))
            self.assertEqual(plan['feasibility'], 'infeasible')
            self.assertEqual(plan['allocations'], [])
            self.assertIn('minimum_or_step_conflict:investment-goal', plan['coverage_gaps'])

    def test_formal_risk_only_checks_eligibility_not_portfolio_ratio(self):
        r1 = uses(arrange_funds(budget(), (goal(),), (product(),), formal_risk(level=1)))
        r5 = uses(arrange_funds(budget(), (goal(),), (product(),), formal_risk(level=5)))
        self.assertEqual(values(r1), values(r5))
        self.assertEqual(values(r1)['investment_principal'], 60_000)
        refused = uses(arrange_funds(budget(), (goal(),), (product(risk_level=3),), formal_risk(level=2)))
        self.assertIn('product_risk_ineligible:investment-goal', refused['coverage_gaps'])
        self.assertEqual(uses(arrange_funds(budget(), (goal(),), (product(),)))['calculation_status'], 'unavailable')

    def test_exit_date_and_evidence_dates_conflict(self):
        late = uses(arrange_funds(budget(), (goal(),),
                                  (product(exit_available_on='2026-11-07'),), formal_risk()))
        self.assertIn('exit_after_use_date:investment-goal', late['coverage_gaps'])
        self.assertEqual(late['feasibility'], 'infeasible')
        for changes in ({'valid_through': '2026-10-05'}, {'evidence_as_of': '2026-10-07'},
                        {'exit_available_on': None}):
            with self.subTest(changes=changes):
                self.assertEqual(uses(arrange_funds(budget(), (goal(),),
                                                   (product(**changes),), formal_risk()))['calculation_status'], 'unavailable')
        expired_risk = uses(arrange_funds(budget(), (goal(),), (product(),),
                                          formal_risk(valid_through='2026-10-05')))
        self.assertIn('formal_risk_not_current:investment-goal', expired_risk['coverage_gaps'])

    def test_period_scope_does_not_extrapolate_beyond_input_coverage(self):
        funds = budget(as_of=None, period_start='2026-10-06', period_end='2026-10-31')
        outside = uses(arrange_funds(funds, (goal(),), (product(),), formal_risk()))
        self.assertIn('goal_outside_funds_period:investment-goal', outside['coverage_gaps'])

    def test_negative_funds_and_wrong_basis_never_become_allocations(self):
        for funds in (budget(-1), budget(basis='account_balance'), budget(formula_id='other/1')):
            with self.subTest(funds=funds.basis):
                result = arrange_funds(funds, (goal(),), (product(),), formal_risk())
                for plan in result['plans']:
                    self.assertEqual(plan['calculation_status'], 'infeasible')
                    self.assertIsNone(values(plan)['remaining_cash'])

    def test_equal_priority_shortfall_does_not_choose_winner_by_order(self):
        a, b = goal(60_000, product_key=None, goal_id='a'), goal(60_000, product_key=None, goal_id='b')
        for ordered in ((a, b), (b, a)):
            plan = uses(arrange_funds(budget(), ordered))
            self.assertEqual(plan['allocations'], [])
            self.assertEqual(values(plan)['unassigned_cash'], 100_000)
            self.assertEqual(plan['calculation_status'], 'partial')

    def test_priority_shortfall_keeps_known_plan_and_unassigned_cash(self):
        a = goal(60_000, product_key=None, goal_id='a', priority=0)
        b = goal(50_000, product_key=None, goal_id='b', priority=1)
        c = goal(1_000, product_key=None, goal_id='c', priority=2)
        plan = uses(arrange_funds(budget(), (c, b, a)))
        self.assertEqual([x['goal_id'] for x in plan['allocations']], ['a'])
        self.assertEqual(values(plan)['assigned_cash'], 60_000)
        self.assertEqual(values(plan)['unassigned_cash'], 40_000)
        self.assertEqual(plan['feasibility'], 'infeasible')
        self.assertEqual(plan['conservation_difference_minor'], 0)
        self.assertEqual(plan['goal_states'][-1]['status'], 'blocked_by_higher_priority')

    def test_source_version_conflict_is_not_silently_overwritten(self):
        conflicting = goal(source_versions=(('funds', 'v2'),))
        plan = uses(arrange_funds(budget(), (conflicting,), (product(),), formal_risk()))
        self.assertIn('source_version_conflict_or_invalid', plan['coverage_gaps'])
        self.assertEqual(plan['calculation_status'], 'unavailable')

    def test_boolean_money_and_truthy_confirmation_are_not_confirmed_inputs(self):
        for changed in (goal(True), goal(confirmed='yes')):
            plan = uses(arrange_funds(budget(), (changed,), (product(),), formal_risk()))
            self.assertEqual(plan['calculation_status'], 'unavailable')
        for changed in (product(current_sale_verified='yes'), product(identity_verified=1)):
            plan = uses(arrange_funds(budget(), (goal(),), (changed,), formal_risk()))
            self.assertEqual(plan['calculation_status'], 'unavailable')

    def test_json_container_types_fail_closed_without_raw_type_error(self):
        for malformed in ({}, [], {'product_id': 'product-a'},
                          ProductKey({}, 'share-a', 'channel-a'),
                          ProductKey('product-a', [], 'channel-a'),
                          ProductKey('product-a', '', 'channel-a')):
            with self.subTest(product_key=malformed):
                plan = uses(arrange_funds(budget(), (goal(product_key=malformed),),
                                         (product(),), formal_risk()))
                self.assertEqual(plan['calculation_status'], 'infeasible')
                self.assertIn('product_key_invalid:investment-goal', plan['coverage_gaps'])
                bad_terms = uses(arrange_funds(budget(), (goal(),),
                                              (product(key=malformed),), formal_risk()))
                self.assertEqual(bad_terms['calculation_status'], 'infeasible')
        for malformed in ({}, [], None, 1):
            with self.subTest(fee_rounding=malformed):
                plan = uses(arrange_funds(budget(), (goal(),),
                                         (product(fee_rounding=malformed),), formal_risk()))
                self.assertEqual(plan['calculation_status'], 'unavailable')
                self.assertEqual(plan['allocations'], [])
        for changes in ({'input_refs': 'evidence'}, {'input_refs': {'evidence': 'v1'}},
                        {'source_versions': {'goals': 'v1'}},
                        {'source_versions': ('ab',)}, {'source_versions': ({'a': 'b', 'c': 'd'},)},
                        {'source_versions': (1,)}, {'source_versions': (({}, 'v1'),)},
                        {'occupancy_refs': 'hold-1'}):
            with self.subTest(evidence=changes):
                plan = uses(arrange_funds(budget(), (goal(**changes),),
                                         (product(),), formal_risk()))
                self.assertEqual(plan['calculation_status'], 'unavailable')
                self.assertEqual(plan['allocations'], [])
        for kwargs in ({'goals': {}}, {'goals': ({},)}, {'products': {}},
                       {'products': ({},)}, {'risk': {}}):
            args = dict(goals=(goal(),), products=(product(),), risk=formal_risk())
            args.update(kwargs)
            self.assertEqual(uses(arrange_funds(budget(), **args))['calculation_status'], 'infeasible')


if __name__ == '__main__':
    unittest.main()
