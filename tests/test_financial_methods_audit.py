"""独立金融审计：字面量手算与反例，不读取服务、数据库、密钥或模型。"""
from dataclasses import replace
from decimal import Decimal
import unittest

from financial_methods.contracts import MetricContext, money_metric
from financial_methods.funding import (
    CashSnapshot, FundingEvent, Hold, PlanningReserve, ScenarioShock,
    evaluate_funding, stress_funding,
)
from financial_methods.accounting import BalanceComponent, CashMovement, balance_sheet, cash_statement
from financial_methods.allocation import AllocationGoal, FormalRisk, ProductKey, ProductTerms, arrange_funds
from financial_methods.returns import (
    EquitySnapshot, HoldingValue, OpeningPosition, HoldingEvent, HistoryCoverage,
    period_profit, portfolio_equity, reconstruct_history, ReturnSubperiod, ReturnBoundary, time_weighted_return,
)


def context(point=False, **changes):
    values = dict(subject_id='A', currency='CNY', data_scope='independent_synthetic',
                  input_refs=('audit:literal-fixture',), source_versions=(('fixture', 'v1'),))
    values.update(dict(as_of='2026-10-06') if point else
                  dict(period_start='2026-10-06', period_end='2026-11-06'))
    values.update(changes)
    return MetricContext(**values)


def snapshot(**changes):
    values = dict(subject_id='A', currency='CNY', as_of='2026-10-06', balance_minor=2_000_000,
                  frozen_minor=0, emergency_floor_minor=1_000_000,
                  coverage_start='2026-10-06', coverage_end='2026-11-06',
                  evidence_ref='audit:cash', source_version='v1')
    values.update(changes)
    return CashSnapshot(**values)


def event(id='bill', on='2026-10-10', amount=300_000, direction='outflow', certainty='confirmed', **changes):
    return FundingEvent(event_id=id, subject_id=changes.get('subject_id', 'A'),
                        currency=changes.get('currency', 'CNY'), on=on, amount_minor=amount,
                        direction=direction, certainty=certainty, evidence_ref='audit:' + id, source_version='v1')


KEY = ProductKey('P', 'share', 'channel')


def goal(**changes):
    values = dict(subject_id='A', currency='CNY', data_scope='independent_synthetic',
                  goal_id='G', amount_minor=300_000, priority=1, needed_on='2026-11-06', confirmed=True,
                  input_refs=('audit:goal',), source_versions=(('goals', 'v1'),), product_key=KEY)
    values.update(changes)
    return AllocationGoal(**values)


def product(**changes):
    values = dict(subject_id='A', currency='CNY', data_scope='independent_synthetic', key=KEY,
                  identity_verified=True, current_sale_verified=True, terms_complete=True, risk_level=1,
                  minimum_minor=1, step_minor=1, entry_fixed_minor=0, entry_rate='0.01',
                  exit_fixed_minor=0, exit_rate='0', fee_rounding='half_up', fee_basis='allocated_principal',
                  exit_available_on='2026-11-01', evidence_as_of='2026-10-06', valid_through='2026-10-06',
                  input_refs=('audit:terms',), source_versions=(('terms', 'v1'),))
    values.update(changes)
    return ProductTerms(**values)


def risk(**changes):
    values = dict(subject_id='A', currency='CNY', data_scope='independent_synthetic', level=2,
                  confirmed=True, effective_on='2026-10-06', valid_through='2027-10-06',
                  input_refs=('audit:risk',), source_versions=(('risk', 'v1'),))
    values.update(changes)
    return FormalRisk(**values)


def budget(amount=400_000):
    return money_metric(context(point=True), 'current_budget', 'floor_surplus', 'funds.current_budget/1', amount)


def history_event(id, sequence, on, kind, **changes):
    return HoldingEvent(id, sequence, on, 'A', 'CNY', 'P/share', kind, **changes)


def opening(**changes):
    values = dict(subject_id='A', currency='CNY', instrument_id='P/share', on='2026-10-06',
                  quantity='100', remaining_cost_minor=10_000, cash_minor=20_000,
                  payables_minor=0, dividend_receivables=(), unit_price='1.00')
    values.update(changes)
    return OpeningPosition(**values)


def coverage(events, **changes):
    values = dict(period_start='2026-10-06', period_end='2026-11-06',
                  expected_event_ids=tuple(e.event_id for e in events), first_sequence=1,
                  transactions_complete=True, entitlements_complete=True,
                  corporate_actions_complete=True, fees_complete=True, cost_method='moving_average')
    values.update(changes)
    return HistoryCoverage(**values)


class IndependentFinancialAudit(unittest.TestCase):
    def test_future_income_changes_projection_but_not_current_budget(self):
        events = (event(), event('income', '2026-10-09', 200_000, 'inflow'))
        result = evaluate_funding(context(), snapshot(), events)
        self.assertEqual(result.metrics['current_budget'].value_minor, 700_000)
        self.assertEqual(result.metrics['confirmed_minimum'].value_minor, 900_000)
        self.assertEqual(result.metrics['confirmed_closing_cash'].value_minor, 1_900_000)

    def test_historical_and_forecast_cash_never_enters_fact_branch(self):
        events = (event(), event('history', '2026-10-05', 500_000, 'inflow', 'historical'),
                  event('forecast', '2026-10-15', 600_000, 'inflow', 'forecast'))
        result = evaluate_funding(context(), snapshot(), events)
        self.assertEqual(result.metrics['confirmed_closing_cash'].value_minor, 1_700_000)
        self.assertEqual(result.metrics['candidate_closing_cash'].value_minor, 1_700_000)

    def test_minimum_is_already_floor_surplus_and_only_compared_to_zero(self):
        result = evaluate_funding(context(), snapshot(), (event(), event('renewal', amount=50_000, certainty='candidate')))
        self.assertEqual(result.metrics['confirmed_minimum'].value_minor, 700_000)
        self.assertEqual(result.metrics['candidate_minimum'].value_minor, 650_000)
        self.assertFalse(result.floor_judgments['candidate_floor_breached'])
        zero = evaluate_funding(context(), snapshot(balance_minor=1_300_000), (event(),))
        self.assertTrue(zero.floor_judgments['confirmed_floor_touched'])
        self.assertFalse(zero.floor_judgments['confirmed_floor_breached'])

    def test_plan_credit_is_consumed_once(self):
        reserves = (PlanningReserve('P1', 'A', 'CNY', 350_000, 'audit:p1', 'v1', ('bill',)),
                    PlanningReserve('P2', 'A', 'CNY', 350_000, 'audit:p2', 'v1', ('bill',)))
        result = evaluate_funding(context(), snapshot(), (event(),), planning_reserves=reserves)
        self.assertEqual(result.planning_reserves[0]['additional_minor'], 50_000)
        self.assertEqual(result.planning_reserves[1]['additional_minor'], 350_000)
        self.assertEqual(result.metrics['scenario_budget'].value_minor, 300_000)

    def test_hold_requires_explicit_payment_release(self):
        hold = Hold('H', 'A', 'CNY', 200_000, 'audit:hold', 'v1', (('bill', 200_000),), True)
        yes = evaluate_funding(context(), snapshot(), (event(),), holds=(hold,))
        no = evaluate_funding(context(), snapshot(), (event(),), holds=(replace(hold, release_on_payment=False),))
        self.assertEqual(yes.metrics['current_available'].value_minor, 800_000)
        self.assertEqual(yes.metrics['current_budget'].value_minor, 700_000)
        self.assertEqual(yes.metrics['confirmed_minimum'].value_minor, 700_000)
        self.assertEqual(no.metrics['confirmed_minimum'].value_minor, 500_000)

    def test_period_object_currency_and_missing_amount_block_computation(self):
        for bad in (snapshot(coverage_end='2026-11-05'), snapshot(subject_id='B'),
                    snapshot(currency='USD'), snapshot(balance_minor=None), snapshot(balance_minor=True)):
            with self.subTest(bad=bad):
                result = evaluate_funding(context(), bad, (event(),))
                self.assertEqual(result.status, 'unavailable')
                self.assertIsNone(result.metrics['current_budget'].value_minor)

    def test_truthy_snapshot_coverage_is_not_verified(self):
        for invalid in ('false', 1, [], None):
            with self.subTest(invalid=invalid):
                result = evaluate_funding(context(), snapshot(coverage_complete=invalid), (event(),))
                self.assertEqual(result.status, 'unavailable')

    def test_context_rejects_character_references_and_mapping_versions(self):
        for invalid in ({'input_refs': 'evidence'}, {'source_versions': {'aa': 'v1'}}):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    context(**invalid)

    def test_income_delay_and_fee_increase_have_literal_shortfall(self):
        snap = snapshot(balance_minor=800_000, emergency_floor_minor=500_000)
        events = (event('income', '2026-10-09', 200_000, 'inflow'), event('expense', '2026-10-11', 300_000))
        result = stress_funding(context(), snap, events,
                                shocks=(ScenarioShock('income', 3), ScenarioShock('expense', 0, 50_000)))
        self.assertEqual(result.metrics['confirmed_minimum'].value_minor, -50_000)
        self.assertEqual(result.floor_judgments['confirmed_shortfall_minor'], 50_000)
        self.assertEqual(result.mode, 'hypothetical_scenario')
        self.assertTrue(all(a['probability'] is None for a in result.metrics['confirmed_minimum'].assumed_inputs))

    def test_loan_principal_does_not_become_income_or_consumption(self):
        movements = (
            CashMovement('borrow', 'A', 'cash', 'CNY', '2026-10-07', 'loan_proceeds', 100_000, 'audit:borrow', 'v1'),
            CashMovement('repay', 'A', 'cash', 'CNY', '2026-10-08', 'loan_principal_repayment', -50_000, 'audit:repay', 'v1'),
            CashMovement('interest', 'A', 'cash', 'CNY', '2026-10-09', 'loan_interest', -1_000, 'audit:interest', 'v1'),
        )
        result = cash_statement(context(), 100_000, 149_000, movements)
        self.assertEqual(result['cash_income'].value_minor, 0)
        self.assertEqual(result['net_cash_expenses'].value_minor, 1_000)
        self.assertEqual(result['loan_principal_net'].value_minor, 50_000)

    def test_refund_reduces_expense_and_not_income(self):
        expense = CashMovement('expense', 'A', 'cash', 'CNY', '2026-10-07', 'expense', -10_000, 'audit:expense', 'v1')
        refund = CashMovement('refund', 'A', 'cash', 'CNY', '2026-10-08', 'refund', 2_000, 'audit:refund', 'v1', 'expense')
        result = cash_statement(context(), 20_000, 12_000, (expense, refund))
        self.assertEqual(result['net_cash_expenses'].value_minor, 8_000)
        self.assertEqual(result['cash_income'].value_minor, 0)

    def test_once_only_cash_input_keeps_income_and_expense_when_net_is_zero(self):
        # 期初期末均为1000分不能证明收入/费用为零；应分别保留100分。
        rows = (
            CashMovement('salary', 'A', 'cash', 'CNY', '2026-10-07', 'income', 100, 'audit:salary', 'v1'),
            CashMovement('bill', 'A', 'cash', 'CNY', '2026-10-08', 'expense', -100, 'audit:bill', 'v1'),
        )
        for shape, movements in (('tuple', rows), ('list', list(rows)), ('generator', (row for row in rows))):
            with self.subTest(shape=shape):
                result = cash_statement(context(), 1_000, 1_000, movements)
                expected = {'cash_income': 100, 'cash_expenses_gross': 100,
                            'net_cash_expenses': 100, 'total_cash_change': 0}
                for name, value in expected.items():
                    self.assertEqual(result[name].value_minor, value, name)
                    self.assertEqual(result[name].calculation_status, 'calculated')
                    self.assertEqual(result[name].coverage_gaps, ())
                self.assertIn('audit:salary', result['cash_income'].input_refs)
                self.assertIn('audit:bill', result['cash_expenses_gross'].input_refs)

    def test_redemption_proceeds_are_not_all_investment_principal(self):
        # 赎回13,000分可能含3,000分收益；此输入没有卖出成本，不能算“本金净流量”。
        buy = CashMovement('buy', 'A', 'cash', 'CNY', '2026-10-07', 'investment_purchase', -10_000, 'audit:buy', 'v1')
        sale = CashMovement('sale', 'A', 'cash', 'CNY', '2026-10-08', 'investment_redemption', 13_000, 'audit:sale', 'v1')
        result = cash_statement(context(), 20_000, 23_000, (buy, sale))
        self.assertEqual(result['total_cash_change'].value_minor, 3_000)
        if 'investment_principal_net' in result:
            self.assertIsNone(result['investment_principal_net'].value_minor)

    def test_nonfinancial_carrying_value_never_increases_cash(self):
        components = (
            BalanceComponent('cash', 'A', 'CNY', '2026-10-06', 'cash', 20_000, 'ledger_cash_balance', 'audit:cash', 'v1'),
            BalanceComponent('machine', 'A', 'CNY', '2026-10-06', 'nonfinancial_asset', 70_000, 'carrying_value', 'audit:machine', 'v1'),
            BalanceComponent('loan', 'A', 'CNY', '2026-10-06', 'liability', 30_000, 'outstanding_principal', 'audit:loan', 'v1'),
        )
        result = balance_sheet(context(point=True), components, expected_object_ids=('cash', 'machine', 'loan'))
        self.assertEqual(result['net_assets'].value_minor, 60_000)
        self.assertEqual(result['cash_balance'].value_minor, 20_000)

    def test_once_only_balance_components_keep_amounts_and_evidence(self):
        rows = (
            BalanceComponent('cash', 'A', 'CNY', '2026-10-06', 'cash', 1_000, 'ledger_cash_balance', 'audit:cash', 'v1'),
            BalanceComponent('machine', 'A', 'CNY', '2026-10-06', 'nonfinancial_asset', 4_000, 'carrying_value', 'audit:machine', 'v1'),
            BalanceComponent('loan', 'A', 'CNY', '2026-10-06', 'liability', 2_500, 'outstanding_principal', 'audit:loan', 'v1'),
        )
        # 字面量手算：资产1000+4000=5000；净资产5000-2500=2500，现金仅1000。
        for shape, components in (('tuple', rows), ('list', list(rows)), ('generator', (row for row in rows))):
            with self.subTest(shape=shape):
                result = balance_sheet(context(point=True), components, expected_object_ids=('cash', 'machine', 'loan'))
                expected = {'total_assets': 5_000, 'total_liabilities': 2_500,
                            'net_assets': 2_500, 'cash_balance': 1_000}
                for name, value in expected.items():
                    self.assertEqual(result[name].value_minor, value, name)
                    self.assertEqual(result[name].calculation_status, 'calculated')
                    self.assertEqual(result[name].coverage_gaps, ())
                self.assertTrue({'audit:cash', 'audit:machine', 'audit:loan'}.issubset(result['net_assets'].input_refs))

    def test_allocation_charges_fees_with_principal_without_risk_ratio(self):
        low = arrange_funds(budget(), (goal(),), (product(),), risk(level=1))['plans'][1]
        high = arrange_funds(budget(), (goal(),), (product(),), risk(level=5))['plans'][1]
        for result in (low, high):
            self.assertEqual(result['metrics']['investment_principal']['value_minor'], 300_000)
            self.assertEqual(result['metrics']['entry_fees']['value_minor'], 3_000)
            self.assertEqual(result['metrics']['unassigned_cash']['value_minor'], 97_000)
            self.assertEqual(result['conservation_difference_minor'], 0)

    def test_current_terms_exit_date_and_risk_dates_are_required(self):
        for bad in (product(exit_available_on='2026-11-07'), product(valid_through='2026-10-05'),
                    product(evidence_as_of='2026-10-07'), product(entry_rate=None),
                    product(key=replace(KEY, share_class_id='other-share'))):
            with self.subTest(bad=bad):
                result = arrange_funds(budget(), (goal(),), (bad,), risk())['plans'][1]
                self.assertEqual(result['allocations'], [])
                self.assertIn(result['calculation_status'], ('unavailable', 'infeasible'))
        result = arrange_funds(budget(), (goal(),), (product(),), risk(valid_through='2026-10-05'))['plans'][1]
        self.assertEqual(result['calculation_status'], 'unavailable')

    def test_json_product_key_and_rounding_objects_are_explicitly_rejected(self):
        for bad_goal, bad_product in ((goal(product_key={}), product()),
                                      (goal(), product(fee_rounding={}))):
            with self.subTest(bad_goal=bad_goal, bad_product=bad_product):
                result = arrange_funds(budget(), (bad_goal,), (bad_product,), risk())['plans'][1]
                self.assertEqual(result['allocations'], [])
                self.assertIn(result['calculation_status'], ('unavailable', 'infeasible'))

    def test_fee_capitalized_hand_calculation_and_dividend_settlement(self):
        events = (
            history_event('buy', 1, '2026-10-07', 'buy', quantity='100', gross_minor=12_000, fee_minor=100, fee_id='buy-fee', fee_basis='external'),
            history_event('sale', 2, '2026-10-08', 'sell', quantity='40', gross_minor=5_200, fee_minor=50, fee_id='sale-fee', fee_basis='external'),
            history_event('dividend', 3, '2026-10-09', 'dividend_declared', gross_minor=500, fee_minor=0, dividend_id='D'),
            history_event('settle', 4, '2026-10-10', 'dividend_settled', dividend_id='D'),
        )
        result = reconstruct_history(context(), opening(), events, coverage(events), closing_unit_price='1.40')
        self.assertEqual(result.quantity_decimal, Decimal('160'))
        expected = {'remaining_cost': 17_680, 'realized_sale_pnl': 730, 'unrealized_pnl': 4_720,
                    'recognized_dividend_income': 500, 'period_pnl': 5_950, 'closing_cash': 13_550}
        for name, value in expected.items():
            self.assertEqual(result.metrics[name].value_minor, value, name)

    def test_ex_dividend_rights_preserve_total_equity(self):
        events = (history_event('dividend', 1, '2026-10-07', 'dividend_declared', gross_minor=1_000, fee_minor=0, dividend_id='D'),)
        result = reconstruct_history(context(), opening(unit_price='1.10', remaining_cost_minor=11_000),
                                     events, coverage(events), closing_unit_price='1.00')
        self.assertEqual(result.metrics['period_pnl'].value_minor, 0)
        self.assertEqual(result.metrics['closing_dividend_receivables'].value_minor, 1_000)

    def test_split_changes_quantity_not_cost_or_profit(self):
        events = (history_event('split', 1, '2026-10-07', 'split', split_factor='2'),)
        result = reconstruct_history(context(), opening(unit_price='2', remaining_cost_minor=20_000),
                                     events, coverage(events), closing_unit_price='1')
        self.assertEqual(result.quantity_decimal, Decimal('200'))
        self.assertEqual(result.metrics['remaining_cost'].value_minor, 20_000)
        self.assertEqual(result.metrics['period_pnl'].value_minor, 0)

    def test_existing_opening_unrealized_gain_is_not_period_gain(self):
        # 期初已有未实现2000分；期末未实现3000分，期间新增收益只为1000分。
        result = reconstruct_history(context(), opening(unit_price='1.20', cash_minor=0),
                                     (), coverage(()), closing_unit_price='1.30')
        self.assertEqual(result.metrics['unrealized_pnl'].value_minor, 3_000)
        self.assertEqual(result.metrics['period_pnl'].value_minor, 1_000)

    def test_nav_embedded_fee_does_not_debit_cash_again(self):
        events = (history_event('embedded', 1, '2026-10-07', 'fee', gross_minor=100,
                                fee_id='management', fee_basis='nav_embedded'),)
        result = reconstruct_history(context(), opening(), events, coverage(events), closing_unit_price='0.99')
        self.assertEqual(result.metrics['closing_cash'].value_minor, 20_000)
        self.assertEqual(result.metrics['external_fee_expense'].value_minor, 0)
        self.assertEqual(result.metrics['period_pnl'].value_minor, -100)

    def test_fee_identity_and_missing_corporate_actions_block_history(self):
        events = (history_event('buy', 1, '2026-10-07', 'buy', quantity='10', gross_minor=1_000, fee_minor=10, fee_id='same', fee_basis='external'),
                  history_event('sale', 2, '2026-10-08', 'sell', quantity='10', gross_minor=1_100, fee_minor=10, fee_id='same', fee_basis='external'))
        for inputs in (coverage(events), coverage(events, corporate_actions_complete=False)):
            result = reconstruct_history(context(), opening(), events, inputs, closing_unit_price='1')
            self.assertIsNone(result.metrics['period_pnl'].value_minor)
        no_price = reconstruct_history(context(), opening(), (), coverage(()), closing_unit_price=None)
        self.assertIsNone(no_price.metrics['unrealized_pnl'].value_minor)

    def test_internal_receivable_and_payable_settlement_not_new_profit(self):
        old = EquitySnapshot('A', 'CNY', '2026-10-06', (HoldingValue('P', 'CNY', '100', '1'),), ('P',), 2_000, 6_000, 200, True)
        new = EquitySnapshot('A', 'CNY', '2026-11-06', (HoldingValue('P', 'CNY', '100', '1.02'),), ('P',), 7_800, 0, 0, True)
        result = period_profit(context(), old, new, owner_contributions_minor=0, owner_withdrawals_minor=0)
        self.assertEqual(result.value_minor, 200)

    def test_non_cent_currency_is_supported_with_correct_scale_or_rejected(self):
        # SIX/ISO4217当前原件：JPY最小单位0位，KWD为3位。不能都乘100。
        for currency, price, expected in (('JPY', '100', 100), ('KWD', '1', 1_000)):
            with self.subTest(currency=currency):
                try:
                    ctx = context(point=True, currency=currency)
                    snap = EquitySnapshot('A', currency, '2026-10-06',
                                          (HoldingValue('P', currency, '1', price),), ('P',), 0, 0, 0, True)
                    result = portfolio_equity(ctx, snap)
                except ValueError:
                    continue  # 首版显式拒绝未支持币种是有效合同，不要求全ISO覆盖。
                if result.value_minor is None:
                    self.assertEqual(result.calculation_status, 'unavailable')
                    self.assertTrue(result.coverage_gaps)
                else:
                    self.assertEqual(result.value_minor, expected)

    def test_twr_links_cashflow_boundary_and_rejects_false_reconciliation(self):
        periods = (ReturnSubperiod('A', 'CNY', '2026-10-06', '2026-10-20', 10_000, 10_500, 0, 0),
                   ReturnSubperiod('A', 'CNY', '2026-10-20', '2026-11-06', 20_500, 19_475, 0, 0))
        boundaries = (ReturnBoundary('A', 'CNY', '2026-10-20', 10_000, 0),)
        self.assertEqual(time_weighted_return(context(), periods, boundaries).value_decimal, Decimal('-0.0025'))
        wrong = (replace(boundaries[0], contribution_minor=9_999),)
        self.assertIsNone(time_weighted_return(context(), periods, wrong).value_decimal)


if __name__ == '__main__':
    unittest.main()
