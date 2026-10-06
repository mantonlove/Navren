import unittest
from dataclasses import replace
from financial_methods.contracts import MetricContext
from financial_methods.funding import CashSnapshot, FundingEvent, Hold, PlanningReserve, ScenarioShock, evaluate_funding, stress_funding


def fixture():
    c = MetricContext("A", "CNY", "navren_synthetic", period_start="2026-10-03", period_end="2026-12-02",
                      input_refs=("coverage:A",), source_versions=(("bank", "v1"),))
    s = CashSnapshot("A", "CNY", "2026-10-03", 2289000, 0, 1000000,
                     "2026-10-03", "2026-12-02", "snapshot:A", "v1")
    events = (FundingEvent("card", "A", "CNY", "2026-10-10", 200000, "outflow", "confirmed", "card:1", "v1"),
              FundingEvent("other", "A", "CNY", "2026-11-01", 104600, "outflow", "confirmed", "bill:2", "v1"),
              FundingEvent("renewal", "A", "CNY", "2026-11-10", 18000, "outflow", "candidate", "sub:3", "v1"))
    return c, s, events


class FundingTests(unittest.TestCase):
    def test_known_semantic_case_independent_arithmetic(self):
        c, s, events = fixture()
        r = evaluate_funding(c, s, events)
        self.assertEqual(r.metrics["cash_balance"].value_minor, 2289000)
        self.assertEqual(r.metrics["current_available"].value_minor, 1289000)
        self.assertEqual(r.metrics["current_budget"].value_minor, 984400)
        self.assertEqual(r.metrics["confirmed_minimum"].value_minor, 984400)
        self.assertEqual(r.metrics["candidate_minimum"].value_minor, 966400)
        self.assertFalse(r.floor_judgments["confirmed_floor_breached"])
        self.assertIn("unknown", r.floor_judgments["adequacy"])

    def test_linked_reserve_only_increment_and_credit_used_once(self):
        c, s, e = fixture()
        rs = (PlanningReserve("p1", "A", "CNY", 250000, "plan:1", "v1", ("card",)),)
        r = evaluate_funding(c, s, e, planning_reserves=rs)
        self.assertEqual(r.planning_reserves[0]["additional_minor"], 50000)
        self.assertEqual(r.metrics["scenario_budget"].value_minor, 934400)
        self.assertEqual(r.metrics["current_budget"].value_minor, 984400)
        r2 = evaluate_funding(c, s, e, planning_reserves=(*rs, replace(rs[0], reserve_id="p2", evidence_ref="plan:2")))
        self.assertEqual(r2.metrics["scenario_budget"].value_minor, 684400)
        self.assertEqual(r2.planning_reserves[1]["additional_minor"], 250000)

    def test_empty_link_is_new_reserve_not_same_amount_dedup(self):
        c, s, e = fixture()
        p = PlanningReserve("p", "A", "CNY", 250000, "plan:p", "v1")
        r = evaluate_funding(c, s, e, planning_reserves=(p,))
        self.assertEqual(r.metrics["scenario_budget"].value_minor, 734400)
        bad = evaluate_funding(c, s, e, planning_reserves=(replace(p, commitment_ids=("unknown",)),))
        self.assertEqual(bad.status, "unavailable")

    def test_confirmed_income_not_current_budget_historical_forecast_excluded(self):
        c, s, e = fixture()
        additions = (FundingEvent("salary", "A", "CNY", "2026-10-05", 300000, "inflow", "confirmed", "income:1", "v1"),
                     FundingEvent("history", "A", "CNY", "2026-09-30", 900000, "inflow", "historical", "past:1", "v1"),
                     FundingEvent("forecast", "A", "CNY", "2026-10-06", 900000, "inflow", "forecast", "future:1", "v1"))
        r = evaluate_funding(c, s, (*e, *additions))
        self.assertEqual(r.metrics["current_budget"].value_minor, 984400)
        self.assertEqual(r.metrics["confirmed_minimum"].value_minor, 1284400)
        self.assertEqual(r.metrics["confirmed_closing_cash"].value_minor, 2284400)
        self.assertEqual(len(r.excluded_items), 2)

    def test_hold_payment_link_requires_explicit_consumption(self):
        c, s, e = fixture()
        h = Hold("h", "A", "CNY", 200000, "hold:1", "v1", (("card", 200000),), True)
        r = evaluate_funding(c, s, e, (h,))
        self.assertEqual(r.metrics["current_available"].value_minor, 1089000)
        self.assertEqual(r.metrics["current_budget"].value_minor, 984400)
        self.assertEqual(r.metrics["confirmed_minimum"].value_minor, 984400)
        without = evaluate_funding(c, s, e, (replace(h, release_on_payment=False),))
        self.assertEqual(without.metrics["current_budget"].value_minor, 784400)
        self.assertEqual(without.metrics["confirmed_minimum"].value_minor, 784400)

    def test_stable_ids_dedup_but_not_same_amount(self):
        c, s, e = fixture()
        self.assertEqual(evaluate_funding(c, s, (*e, e[0])).metrics["current_budget"].value_minor, 984400)
        another = replace(e[0], event_id="another", evidence_ref="card:another")
        self.assertEqual(evaluate_funding(c, s, (*e, another)).metrics["current_budget"].value_minor, 784400)
        self.assertEqual(evaluate_funding(c, s, (*e, replace(e[0], amount_minor=1))).status, "unavailable")

    def test_incomplete_mismatched_missing_and_already_deducted(self):
        c, s, e = fixture()
        for bad in (replace(s, balance_minor=None), replace(s, currency="USD"), replace(s, subject_id="B"),
                    replace(s, coverage_end="2026-11-01"), replace(s, basis="floor_surplus"), replace(s, coverage_complete=False)):
            r = evaluate_funding(c, bad, e)
            self.assertEqual(r.status, "unavailable")
            self.assertIsNone(r.metrics["confirmed_minimum"].value_minor)

    def test_zero_is_touch_negative_is_breach(self):
        c, s, _ = fixture()
        r = evaluate_funding(c, replace(s, balance_minor=1000000))
        self.assertTrue(r.floor_judgments["confirmed_floor_touched"])
        self.assertFalse(r.floor_judgments["confirmed_floor_breached"])
        r = evaluate_funding(c, replace(s, balance_minor=999999))
        self.assertEqual(r.floor_judgments["confirmed_shortfall_minor"], 1)

    def test_explicit_stress_delay_then_fee(self):
        c, s, _ = fixture()
        s = replace(s, balance_minor=800000, emergency_floor_minor=500000)
        e = (FundingEvent("income", "A", "CNY", "2026-10-06", 200000, "inflow", "confirmed", "i:1", "v1"),
             FundingEvent("fee", "A", "CNY", "2026-10-08", 300000, "outflow", "confirmed", "e:1", "v1"))
        self.assertEqual(evaluate_funding(c, s, e).metrics["confirmed_minimum"].value_minor, 200000)
        r = stress_funding(c, s, e, shocks=(ScenarioShock("income", 3), ScenarioShock("fee", 0, 50000)))
        self.assertEqual(r.metrics["confirmed_minimum"].value_minor, -50000)
        self.assertEqual(r.floor_judgments["confirmed_shortfall_minor"], 50000)
        self.assertEqual(r.mode, "hypothetical_scenario")
        self.assertEqual(len(r.metrics["confirmed_minimum"].assumed_inputs), 2)
        self.assertTrue(all(a["probability"] is None for a in r.metrics["confirmed_minimum"].assumed_inputs))


if __name__ == "__main__":
    unittest.main()
