"""合成纯计算数据的独立手算预期；不接现役服务、数据库、模型或交易。"""
from dataclasses import replace
from decimal import Decimal
import unittest

from financial_methods.contracts import MetricContext
from financial_methods.returns import (
    DividendReceivable, EquitySnapshot, HistoryCoverage, HoldingEvent, HoldingValue,
    OpeningPosition, ReturnBoundary, ReturnSubperiod, holding_value_minor,
    holdings_market_value, period_profit, portfolio_equity, reconstruct_history,
    simple_return, time_weighted_return,
)


def context(*, period=False, complete=True):
    return MetricContext("portfolio-a", "CNY", "synthetic_test", **(
        {"period_start": "2026-01-01", "period_end": "2026-01-31"} if period else {"as_of": "2026-01-31"}),
        input_refs=("manual-case-1",), source_versions=(("manual-case-1", "1"),), coverage_complete=complete)


def snapshot(on="2026-01-31", quantity="100", price="1", cash=0, receivables=0, payables=0):
    return EquitySnapshot("portfolio-a", "CNY", on, (HoldingValue("fund-a", "CNY", quantity, price),),
                          ("fund-a",), cash, receivables, payables, True)


def opening(**changes):
    base = OpeningPosition("portfolio-a", "CNY", "fund-a", "2026-01-01", "100", 10000,
                           20000, 0, (), "1")
    return replace(base, **changes)


def event(number, kind, **fields):
    return HoldingEvent("event-" + str(number), number, "2026-01-" + str(number + 1).zfill(2),
                        "portfolio-a", "CNY", "fund-a", kind, **fields)


def coverage(events, **changes):
    base = HistoryCoverage("2026-01-01", "2026-01-31", tuple(e.event_id for e in events), 1,
                           True, True, True, True, "moving_average")
    return replace(base, **changes)


def values(result):
    return {name: metric.value_minor for name, metric in result.metrics.items()}


class ValuationTests(unittest.TestCase):
    def test_decimal_prices_and_half_up_minor_rounding(self):
        self.assertEqual(holding_value_minor("1", "0.005"), 1)
        self.assertEqual(holding_value_minor("2.5", "1.003"), 251)
        self.assertEqual(holding_value_minor("0", None), 0)
        self.assertEqual(holding_value_minor("1", "1E+30"), 10 ** 32)

    def test_invalid_or_binary_float_inputs_raise(self):
        for q, price in ((True, "1"), ("-1", "1"), ("1", 1.0), ("1", "0"), ("NaN", "1"), ("1", "Infinity")):
            with self.subTest(q=q, price=price), self.assertRaises(ValueError):
                holding_value_minor(q, price)

    def test_holdings_and_total_equity_are_different(self):
        s = snapshot(price="1.005", cash=2000, receivables=500, payables=300)
        self.assertEqual(holdings_market_value(context(), s).value_minor, 10050)
        self.assertEqual(portfolio_equity(context(), s).value_minor, 12250)
        payload = portfolio_equity(context(), s).to_dict()
        self.assertEqual(payload["input_refs"], ["manual-case-1"])
        self.assertEqual(payload["source_versions"], {"manual-case-1": "1"})

    def test_unknown_cash_does_not_become_zero_or_hide_holdings_value(self):
        s = snapshot(cash=None)
        self.assertIsNone(portfolio_equity(context(), s).value_minor)
        self.assertIn("cash_minor_missing", portfolio_equity(context(), s).coverage_gaps)
        self.assertEqual(holdings_market_value(context(), s).value_minor, 10000)

    def test_missing_quantity_or_price_not_zero(self):
        for s in (snapshot(quantity=None), snapshot(price=None)):
            self.assertIsNone(portfolio_equity(context(), s).value_minor)
        self.assertEqual(portfolio_equity(context(), snapshot(quantity="0", price=None)).value_minor, 0)

    def test_expected_identity_coverage_and_duplicates(self):
        s = snapshot()
        cases = (replace(s, expected_instrument_ids=("fund-a", "fund-b")),
                 replace(s, holdings=s.holdings + s.holdings), replace(s, holdings=None))
        for case in cases:
            self.assertIsNone(portfolio_equity(context(), case).value_minor)

    def test_wrong_subject_currency_date_or_coverage_returns_unknown(self):
        s = snapshot()
        cases = (replace(s, subject_id="other"), replace(s, currency="USD"),
                 replace(s, on="2026-01-30"), replace(s, coverage_complete=False),
                 replace(s, holdings=(replace(s.holdings[0], currency="USD"),)))
        for case in cases:
            self.assertIsNone(portfolio_equity(context(), case).value_minor)
        self.assertIsNone(portfolio_equity(context(complete=False), s).value_minor)

    def test_cash_only_and_negative_equity_are_explicit(self):
        s = replace(snapshot(), holdings=(), expected_instrument_ids=(), cash_minor=1000, payables_minor=1500)
        self.assertEqual(portfolio_equity(context(), s).value_minor, -500)

    def test_unsupported_currency_scale_even_when_holdings_are_zero_or_empty(self):
        for currency in ("JPY", "KWD", "USD"):
            ctx = replace(context(), currency=currency)
            s = replace(snapshot(quantity="0", price=None), currency=currency,
                        holdings=(HoldingValue("fund-a", currency, "0", None),))
            for fn in (holdings_market_value, portfolio_equity):
                result = fn(ctx, s)
                self.assertIsNone(result.value_minor)
                self.assertIn("unsupported_currency_scale", result.coverage_gaps)
            cash_only = replace(s, holdings=(), expected_instrument_ids=(), cash_minor=100)
            self.assertIsNone(portfolio_equity(ctx, cash_only).value_minor)
            with self.assertRaisesRegex(ValueError, "unsupported_currency_scale"):
                holding_value_minor("0", None, currency=currency)

    def test_period_profit_removes_owner_net_investment(self):
        # 期初 11000，期末 12600，投入1000、提取400，损益1000分。
        metric = period_profit(context(period=True), snapshot("2026-01-01", cash=1000),
                               snapshot(price="1.1", cash=1600),
                               owner_contributions_minor=1000, owner_withdrawals_minor=400)
        self.assertEqual(metric.value_minor, 1000)

    def test_period_profit_missing_owner_flow_or_stale_snapshot(self):
        a, b = snapshot("2026-01-01"), snapshot()
        self.assertIsNone(period_profit(context(period=True), a, b,
                                       owner_contributions_minor=None, owner_withdrawals_minor=0).value_minor)
        self.assertIsNone(period_profit(context(period=True), replace(a, on="2025-12-31"), b,
                                       owner_contributions_minor=0, owner_withdrawals_minor=0).value_minor)

    def test_dividend_receivable_settlement_is_not_second_profit(self):
        c = replace(context(period=True), period_start="2026-01-30")
        a = snapshot("2026-01-30", price="0.9", receivables=1000)
        b = snapshot(price="0.9", cash=1000)
        self.assertEqual(period_profit(c, a, b, owner_contributions_minor=0, owner_withdrawals_minor=0).value_minor, 0)


class RateTests(unittest.TestCase):
    def test_no_owner_flow_simple_return_uses_ratio_not_minor(self):
        r = simple_return(context(period=True), opening_equity_minor=10000, closing_equity_minor=11000,
                          owner_contributions_minor=0, owner_withdrawals_minor=0)
        self.assertEqual(r.value_decimal, Decimal("0.1"))
        self.assertEqual(r.to_dict()["unit"], "ratio")
        self.assertEqual(r.to_dict()["value_decimal"], "0.1")
        self.assertNotIn("value_minor", r.to_dict())
        self.assertEqual(r.to_dict()["source_versions"], {"manual-case-1": "1"})

    def test_unitless_return_keeps_same_currency_minor_ratio(self):
        for currency in ("JPY", "KWD"):
            ctx = replace(context(period=True), currency=currency)
            result = simple_return(ctx, opening_equity_minor=10000, closing_equity_minor=11000,
                                   owner_contributions_minor=0, owner_withdrawals_minor=0)
            self.assertEqual(result.value_decimal, Decimal("0.1"))

    def test_net_zero_flows_still_block_simple_return(self):
        r = simple_return(context(period=True), opening_equity_minor=10000, closing_equity_minor=11000,
                          owner_contributions_minor=500, owner_withdrawals_minor=500)
        self.assertIsNone(r.value_decimal)
        self.assertIn("cashflows_require_subperiod_valuations", r.coverage_gaps)

    def test_simple_missing_or_zero_base_and_incomplete_period(self):
        for begin, end, ctx in ((0, 0, context(period=True)), (None, 11000, context(period=True)),
                                (10000, 11000, context(complete=False))):
            self.assertIsNone(simple_return(ctx, opening_equity_minor=begin, closing_equity_minor=end,
                                           owner_contributions_minor=0, owner_withdrawals_minor=0).value_decimal)

    def parts(self):
        return (ReturnSubperiod("portfolio-a", "CNY", "2026-01-01", "2026-01-15", 10000, 11000, 0, 0),
                ReturnSubperiod("portfolio-a", "CNY", "2026-01-15", "2026-01-31", 16000, 17600, 0, 0))

    def boundary(self):
        return ReturnBoundary("portfolio-a", "CNY", "2026-01-15", 5000, 0)

    def test_time_weighted_return_links_before_after_cashflow_valuations(self):
        # 第一子期10%，投入5000后第二子期10%，(1.1×1.1)-1=21%。
        r = time_weighted_return(context(period=True), self.parts(), (self.boundary(),))
        self.assertEqual(r.value_decimal, Decimal("0.21"))
        self.assertEqual(r.to_dict()["calculation_status"], "calculated")

    def test_withdrawal_boundary_links_identically(self):
        parts = (self.parts()[0], replace(self.parts()[1], opening_equity_minor=6000, closing_equity_minor=6600))
        boundary = replace(self.boundary(), contribution_minor=0, withdrawal_minor=5000)
        self.assertEqual(time_weighted_return(context(period=True), parts, (boundary,)).value_decimal, Decimal("0.21"))

    def test_missing_reconciliation_and_internal_cashflow_block_twr(self):
        cases = ((self.parts(), ()),
                 ((self.parts()[0], replace(self.parts()[1], opening_equity_minor=None)), (self.boundary(),)),
                 (self.parts(), (replace(self.boundary(), contribution_minor=4000),)),
                 ((replace(self.parts()[0], internal_contributions_minor=1), self.parts()[1]), (self.boundary(),)),
                 (self.parts(), (replace(self.boundary(), withdrawal_minor=None),)))
        for parts, boundaries in cases:
            self.assertIsNone(time_weighted_return(context(period=True), parts, boundaries).value_decimal)

    def test_twr_wrong_subject_currency_period_or_zero_denominator(self):
        cases = ((replace(self.parts()[0], currency="USD"), self.parts()[1]),
                 (self.parts()[0], replace(self.parts()[1], subject_id="other")),
                 (replace(self.parts()[0], start="2026-01-02"), self.parts()[1]),
                 (replace(self.parts()[0], opening_equity_minor=0), self.parts()[1]),
                 (self.parts()[0], replace(self.parts()[1], start="2026-01-16")))
        for parts in cases:
            self.assertIsNone(time_weighted_return(context(period=True), parts, (self.boundary(),)).value_decimal)


class HistoryTests(unittest.TestCase):
    def run_case(self, events=(), start=None, price="1", cov=None):
        return reconstruct_history(context(period=True), start or opening(), events, cov or coverage(events), closing_unit_price=price)

    def test_moving_average_buy_fee_capitalization_sell_fee_and_dividend(self):
        events = (event(1, "buy", quantity="100", gross_minor=12000, fee_minor=100,
                        fee_id="buy-fee", fee_basis="external"),
                  event(2, "sell", quantity="40", gross_minor=5200, fee_minor=50,
                        fee_id="sell-fee", fee_basis="external"),
                  event(3, "dividend_declared", gross_minor=500, fee_minor=0, dividend_id="div-1"),
                  event(4, "dividend_settled", dividend_id="div-1"))
        r = self.run_case(events, price="1.4")
        # 22100成本/200份=110.5分/份；卖出成本4420，净售5150，已实现730。
        # 剩余成本17680；市值22400，未实现4720；730+500+4720=5950。
        expected = {"closing_holdings_value": 22400, "remaining_cost": 17680, "closing_cash": 13550,
                    "closing_dividend_receivables": 0, "closing_equity": 35950,
                    "owner_net_flow": 0, "realized_sale_pnl": 730, "recognized_dividend_income": 500,
                    "external_fee_expense": 0, "realized_pnl": 1230, "unrealized_pnl": 4720, "period_pnl": 5950}
        self.assertEqual(values(r), expected)
        self.assertEqual(r.quantity_decimal, Decimal("160"))
        self.assertEqual(r.event_trace[1]["allocated_sale_cost_minor"], 4420)
        self.assertEqual(r.metrics["closing_equity"].as_of, "2026-01-31")
        self.assertIsNone(r.metrics["closing_equity"].period_start)
        self.assertEqual(r.metrics["period_pnl"].period_start, "2026-01-01")
        self.assertIsNone(r.metrics["period_pnl"].as_of)

    def test_dividend_confirmation_and_settlement_do_not_repeat_income(self):
        declared = event(1, "dividend_declared", gross_minor=1000, fee_minor=0, dividend_id="div-1")
        pending = self.run_case((declared,), start=opening(cash_minor=0), price="0.9")
        settled = self.run_case((declared, event(2, "dividend_settled", dividend_id="div-1")),
                                start=opening(cash_minor=0), price="0.9")
        self.assertEqual(values(pending)["closing_equity"], 10000)
        self.assertEqual(values(settled)["closing_equity"], 10000)
        self.assertEqual(values(settled)["recognized_dividend_income"], 1000)
        self.assertEqual(values(settled)["period_pnl"], 0)

    def test_opening_receivable_settlement_not_current_period_income(self):
        events = (event(1, "dividend_settled", dividend_id="previous-period"),)
        r = self.run_case(events, start=opening(cash_minor=0,
            dividend_receivables=(DividendReceivable("previous-period", 1000),)))
        self.assertEqual(values(r)["period_pnl"], 0)
        self.assertEqual(values(r)["recognized_dividend_income"], 0)
        self.assertEqual(values(r)["closing_cash"], 1000)

    def test_dividend_tax_recognized_net_once(self):
        events = (event(1, "dividend_declared", gross_minor=1000, fee_minor=100,
                        fee_id="tax", fee_basis="external", dividend_id="div"),)
        r = self.run_case(events)
        self.assertEqual(values(r)["recognized_dividend_income"], 900)
        self.assertEqual(values(r)["closing_dividend_receivables"], 900)
        self.assertEqual(values(r)["period_pnl"], 900)

    def test_split_preserves_cost_and_equity(self):
        r = self.run_case((event(1, "split", split_factor="2.5"),), start=opening(cash_minor=0), price="0.4")
        self.assertEqual(r.quantity_decimal, Decimal("250"))
        self.assertEqual(values(r)["remaining_cost"], 10000)
        self.assertEqual(values(r)["closing_equity"], 10000)
        self.assertEqual(values(r)["period_pnl"], 0)

    def test_final_sale_allocates_rounding_residual_cost(self):
        events = (event(1, "sell", quantity="1", gross_minor=4000, fee_minor=0),
                  event(2, "sell", quantity="2", gross_minor=8000, fee_minor=0))
        r = self.run_case(events, start=opening(quantity="3", remaining_cost_minor=10000,
                                              cash_minor=0, unit_price="33.3333333333333333"), price=None)
        self.assertEqual([t["allocated_sale_cost_minor"] for t in r.event_trace], [3333, 6667])
        self.assertEqual(values(r)["remaining_cost"], 0)
        self.assertEqual(values(r)["realized_sale_pnl"], 2000)
        self.assertEqual(values(r)["period_pnl"], 2000)

    def test_external_fee_and_nav_embedded_fee_are_separate(self):
        events = (event(1, "fee", gross_minor=100, fee_id="external", fee_basis="external"),
                  event(2, "fee", gross_minor=200, fee_id="internal", fee_basis="nav_embedded"))
        r = self.run_case(events)
        self.assertEqual(values(r)["period_pnl"], -100)
        self.assertEqual(values(r)["realized_pnl"], -100)
        self.assertEqual(values(r)["closing_cash"], 19900)
        self.assertIn("净值已内扣费用：internal", r.metrics["period_pnl"].excluded_items)

    def test_owner_flows_remove_capital_from_profit(self):
        events = (event(1, "owner_contribution", gross_minor=5000),
                  event(2, "buy", gross_minor=5000, fee_minor=0, quantity="50"),
                  event(3, "owner_withdrawal", gross_minor=3000))
        r = self.run_case(events)
        self.assertEqual(values(r)["owner_net_flow"], 2000)
        self.assertEqual(values(r)["period_pnl"], 0)
        self.assertEqual(values(r)["closing_equity"], 32000)

    def test_missing_price_preserves_inventory_but_not_equity_or_profit(self):
        r = self.run_case(price=None)
        self.assertEqual(r.quantity_decimal, Decimal("100"))
        self.assertEqual(values(r)["remaining_cost"], 10000)
        self.assertIsNone(values(r)["closing_equity"])
        self.assertIsNone(values(r)["period_pnl"])
        self.assertEqual(r.to_dict()["calculation_status"], "partial")
        self.assertIn("closing_price_missing", r.metrics["period_pnl"].coverage_gaps)

    def test_missing_opening_price_does_not_erase_closing_equity(self):
        r = self.run_case(start=opening(unit_price=None))
        self.assertEqual(values(r)["closing_equity"], 30000)
        self.assertIsNone(values(r)["period_pnl"])
        self.assertIn("opening_price_missing", r.metrics["period_pnl"].coverage_gaps)

    def test_explicit_coverage_all_required(self):
        for flag in ("transactions_complete", "fees_complete", "entitlements_complete", "corporate_actions_complete"):
            r = self.run_case(cov=coverage((), **{flag: False}))
            self.assertIsNone(r.quantity_decimal)
            self.assertTrue(all(value is None for value in values(r).values()))
        self.assertIsNone(self.run_case(cov=coverage((), cost_method="fifo")).quantity_decimal)

    def test_missing_opening_fields_are_unknown(self):
        for field in ("quantity", "remaining_cost_minor", "cash_minor", "payables_minor", "dividend_receivables"):
            self.assertIsNone(self.run_case(start=opening(**{field: None})).quantity_decimal)

    def test_zero_length_rate_and_history_period_are_not_fabricated(self):
        same_day = replace(context(period=True), period_end="2026-01-01")
        rate = simple_return(same_day, opening_equity_minor=10000, closing_equity_minor=11000,
                             owner_contributions_minor=0, owner_withdrawals_minor=0)
        self.assertIsNone(rate.value_decimal)
        r = reconstruct_history(same_day, opening(), (),
            replace(coverage(()), period_end="2026-01-01"), closing_unit_price="1")
        self.assertIsNone(r.quantity_decimal)
        self.assertIn("history_period_order_invalid", r.coverage_gaps)

    def test_history_requires_period_not_point_context(self):
        with self.assertRaisesRegex(ValueError, "historical_reconstruction_requires_period_context"):
            reconstruct_history(context(), opening(), (), coverage(()), closing_unit_price="1")

    def test_history_estimation_explicitly_rejects_other_currency_scales(self):
        for currency in ("JPY", "KWD"):
            ctx = replace(context(period=True), currency=currency)
            start = opening(currency=currency, quantity="0", remaining_cost_minor=0, unit_price=None)
            result = reconstruct_history(ctx, start, (), coverage(()), closing_unit_price=None)
            self.assertIsNone(result.quantity_decimal)
            self.assertTrue(all(value is None for value in values(result).values()))
            self.assertIn("unsupported_currency_scale", result.coverage_gaps)

    def test_sequence_expected_ids_identity_date_and_duplicates_checked(self):
        e = event(1, "split", split_factor="2")
        cases = ((replace(e, sequence=2),), (replace(e, currency="USD"),),
                 (replace(e, subject_id="other"),), (replace(e, instrument_id="other"),),
                 (replace(e, on="2026-02-01"),), (replace(e, on="2026-01-01"),), (e, replace(e, sequence=2)))
        for events in cases:
            self.assertIsNone(self.run_case(events).quantity_decimal)
        self.assertIsNone(self.run_case((e,), cov=coverage((e,), expected_event_ids=())).quantity_decimal)
        self.assertIsNone(self.run_case(cov=coverage((), expected_event_ids=None)).quantity_decimal)

    def test_duplicate_fee_is_not_double_charged(self):
        events = (event(1, "buy", quantity="1", gross_minor=100, fee_minor=10,
                        fee_id="same-fee", fee_basis="external"),
                  event(2, "fee", gross_minor=10, fee_id="same-fee", fee_basis="external"))
        r = self.run_case(events)
        self.assertIsNone(r.quantity_decimal)
        self.assertIn("duplicate_or_missing_fee_identity", r.coverage_gaps)

    def test_boolean_fee_does_not_pass_as_zero(self):
        events = (event(1, "owner_contribution", gross_minor=100, fee_minor=False),)
        self.assertIsNone(self.run_case(events).quantity_decimal)
        declared = event(1, "dividend_declared", gross_minor=100, fee_minor=0, dividend_id="div")
        settled = event(2, "dividend_settled", dividend_id="div", fee_minor=False)
        self.assertIsNone(self.run_case((declared, settled)).quantity_decimal)

    def test_unsettled_reference_required_and_duplicate_declaration_rejected(self):
        d = event(1, "dividend_declared", gross_minor=100, fee_minor=0, dividend_id="div")
        s = event(2, "dividend_settled", dividend_id="div")
        for events in ((event(1, "dividend_settled", dividend_id="missing"),),
                       (d, replace(d, event_id="event-2", sequence=2, on="2026-01-03")),
                       (d, s, event(3, "dividend_settled", dividend_id="div"))):
            self.assertIsNone(self.run_case(events).quantity_decimal)

    def test_oversale_insufficient_cash_unsupported_events_and_ignored_fields_rejected(self):
        cases = ((event(1, "sell", quantity="101", gross_minor=10100, fee_minor=0),),
                 (event(1, "buy", quantity="300", gross_minor=30000, fee_minor=0),),
                 (event(1, "redemption_receivable", gross_minor=100),),
                 (event(1, "split", split_factor="2", fee_minor=100),))
        for events in cases:
            self.assertIsNone(self.run_case(events).quantity_decimal)


if __name__ == "__main__":
    unittest.main()
