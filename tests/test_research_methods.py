"""Independent arithmetic examples; synthetic RULES are not product facts."""
from copy import deepcopy
import json
import inspect
import math
import unittest

from research_methods import (check_coverage_scenario, compare_share_costs,
                              pension_cashflows, reserve_obligations)


def fact(value, **scope):
    return dict(value=value, state="verified", source_id="fixture-contract", source_version="v1",
                currency="CNY", effective_from=None, effective_to=None, **scope)


def common():
    return dict(as_of="2026-10-18", currency="CNY", evidence_versions={"fixture-contract": "v1"})


def comparison():
    fee = dict(subscription=[dict(minimum="0", maximum=None, method="external_rate", value="0.01")],
        redemption=[dict(min_days=0, max_days=7, rate="0.02"), dict(min_days=7, max_days=None, rate="0.01")],
        ongoing=[dict(kind="management", annual_rate="0.01", included_in_nav=True)],
        expense_basis="net_subscription_constant", day_basis=365)
    second = deepcopy(fee)
    second["subscription"][0]["value"] = "0"
    second["ongoing"][0]["annual_rate"] = "0.02"
    second["redemption"][1]["rate"] = "0"
    return dict(**common(), amount="10100.00", holding_days=365, channel="fixture-bank",
        valuation_basis="constant_gross_value", shares=[
            dict(entity_id="fund-A", fees=fact(fee, entity_id="fund-A", channel="fixture-bank")),
            dict(entity_id="fund-C", fees=fact(second, entity_id="fund-C", channel="fixture-bank"))])


def reserves():
    return dict(**common(), cash_basis="ledger_cash_before_reservations", until="2026-11-30",
        available_cash=fact("10000.00"), obligations=fact(dict(complete=True, items=[
            dict(obligation_id="rent", amount="3000", due_date="2026-11-01", status="confirmed"),
            dict(obligation_id="premium", amount="2000", due_date="2026-11-15", status="confirmed"),
            dict(obligation_id="overdue", amount="100", due_date="2026-10-01", status="confirmed")])),
        reservations=fact(dict(complete=True, items=[
            dict(reservation_id="hold-rent", obligation_id="rent", amount="1000", status="held"),
            dict(reservation_id="other", obligation_id=None, amount="500", status="held")])))


def coverage():
    medical = dict(responsibility_id="medical-A", mechanism="expense_compensation", cost_ids=["A"],
        shared_limit_id="medical", deductible_id="annual-deductible", annual_limit="20000", annual_used="0",
        per_claim_limit="20000", reimbursement_tiers=[dict(up_to=None, rate="0.8")])
    second = dict(deepcopy(medical), responsibility_id="medical-B", cost_ids=["B"])
    fixed = dict(responsibility_id="critical", mechanism="fixed_benefit", cost_ids=[],
        shared_limit_id="none", deductible_id="none", annual_limit="5000", annual_used="0",
        per_claim_limit="5000", benefit_amount="5000")
    terms = dict(policy_start="2026-01-01", policy_end="2026-12-31", waiting_end="2026-01-31",
        usage_period_start="2026-01-01", usage_period_end="2026-12-31",
        waiting_end_inclusive=True, deduction_order=["other_reimbursements", "deductible", "ratio", "limits"],
        policy_annual_limit="unlimited", policy_annual_used="0",
        shared_limits=[dict(group_id="medical", limit="6000", used="0")],
        deductibles=[dict(deductible_id="annual-deductible", amount="3000", used="1000", period="annual")],
        responsibilities=[medical, second, fixed], payment_order=["medical-A", "medical-B", "critical"])
    scenario = dict(event_date="2026-10-18", conditions=dict(in_force=True, covered_event=True,
        provider_eligible=True, exclusions_cleared=True), fixed_events=["critical"], costs=[
        dict(cost_id="A", amount="10000", other_reimbursements="1000"),
        dict(cost_id="B", amount="10000", other_reimbursements="0")])
    return dict(**common(), policy_id="test-policy", terms=fact(terms, entity_id="test-policy"), scenario=scenario)


def pension():
    return dict(**common(), entity_id="test-pension", day_basis=365,
        cashflow_schedule=fact(dict(complete=True, projection_end="2027-01-01", items=[
            dict(cashflow_id="premium", date="2026-01-01", amount="-1000", kind="premium", guaranteed=True),
            dict(cashflow_id="benefit", date="2027-01-01", amount="1100", kind="benefit", guaranteed=True),
            dict(cashflow_id="bonus", date="2027-01-01", amount="100", kind="benefit", guaranteed=False),
            dict(cashflow_id="terminal", date="2027-01-01", amount="0", kind="terminal_value", guaranteed=True)]),
            entity_id="test-pension"))


class ShareCosts(unittest.TestCase):
    def test_independent_external_rate_and_annual_cost(self):
        # 10100/1.01=10000; entry100 + ongoing100 + exit99, proceeds9801.
        r = compare_share_costs(comparison())
        self.assertEqual(r["status"], "complete")
        a, c = r["rows"]
        self.assertEqual([a[k] for k in ("subscription_fee_minor", "ongoing_fee_estimate_minor",
            "redemption_fee_minor", "scenario_proceeds_minor")], [10000, 10000, 9900, 980100])
        # C: no entry; 10100*2%=202; 9898 left, no redemption fee.
        self.assertEqual(c["scenario_proceeds_minor"], 989800)

    def test_actual_net_NAV_expenses_not_deducted_twice(self):
        p = comparison();p["valuation_basis"] = "observed_net_nav"
        for row in p["shares"]:
            row["net_nav_value"] = fact("11000", entity_id=row["entity_id"], channel=p["channel"])
        r = compare_share_costs(p)
        self.assertEqual(r["rows"][0]["scenario_proceeds_minor"], 1089000)
        self.assertIsNone(r["rows"][0]["ongoing_fee_estimate_minor"])

    def test_redemption_boundary_is_half_open(self):
        p = comparison();p["holding_days"] = 7
        self.assertEqual(compare_share_costs(p)["rows"][0]["redemption_fee_minor"], 9998)

    def test_fixed_and_internal_subscription_tiers(self):
        p = comparison();p["holding_days"] = 0
        p["shares"][0]["fees"]["value"]["subscription"][0].update(method="fixed", value="100")
        p["shares"][1]["fees"]["value"]["subscription"][0].update(method="internal_rate", value="0.01")
        r = compare_share_costs(p)
        self.assertEqual([row["subscription_fee_minor"] for row in r["rows"]], [10000, 10100])

    def test_missing_channel_fee_not_zero(self):
        p = comparison();p["shares"][0].pop("fees")
        r = compare_share_costs(p)
        self.assertEqual(r["status"], "partial")
        self.assertIsNone(r["rows"][0]["scenario_proceeds_minor"])

    def test_overlapping_tiers_and_wrong_scope(self):
        p = comparison();p["shares"][0]["fees"]["value"]["redemption"].append(dict(min_days=5,max_days=10,rate="0.01"))
        self.assertEqual(compare_share_costs(p)["rows"][0]["status"], "invalid")
        p = comparison();p["shares"][0]["fees"]["channel"] = "other"
        self.assertEqual(compare_share_costs(p)["rows"][0]["status"], "conflict")

    def test_amount_tier_boundary_and_half_up(self):
        p = comparison();p["holding_days"] = 0
        p["shares"][0]["fees"]["value"]["subscription"] = [
            dict(minimum="0",maximum="10100",method="fixed",value="1"),
            dict(minimum="10100",maximum=None,method="internal_rate",value="0.00015")]
        self.assertEqual(compare_share_costs(p)["rows"][0]["subscription_fee_minor"], 152)


class CashReserves(unittest.TestCase):
    def test_hand_calculated_no_double_reservation(self):
        # Required: rent3000 + premium2000 + overdue100 + unrelated hold500 =5600.
        r = reserve_obligations(reserves())
        self.assertEqual(r["status"], "complete")
        c = r["calculations"]
        self.assertEqual([c[k] for k in ("held_minor", "additional_reserve_minor",
            "required_reserve_minor", "uncommitted_cash_minor")], [150000,410000,560000,440000])

    def test_overfunded_hold_not_released_by_calculation(self):
        p = reserves();p["reservations"]["value"]["items"][0]["amount"] = "4000"
        self.assertEqual(reserve_obligations(p)["calculations"]["required_reserve_minor"], 660000)

    def test_paid_and_future_obligations_and_shortfall(self):
        p = reserves();p["available_cash"]["value"] = "1000"
        p["obligations"]["value"]["items"][1]["status"] = "paid"
        p["obligations"]["value"]["items"][2]["due_date"] = "2026-12-01"
        c = reserve_obligations(p)["calculations"]
        self.assertEqual(c["required_reserve_minor"], 350000)
        self.assertEqual(c["uncommitted_cash_minor"], 0)
        self.assertEqual(c["shortfall_minor"], 250000)

    def test_incomplete_or_missing_money_keeps_unknown(self):
        for field in ("obligations", "reservations"):
            p = reserves();p[field]["value"]["complete"] = False
            r = reserve_obligations(p)
            self.assertEqual(r["status"], "missing");self.assertIsNone(r["calculations"])
        p = reserves();p["obligations"]["value"]["items"][0]["amount"] = None
        self.assertIsNone(reserve_obligations(p)["calculations"])

    def test_duplicate_identity_wrong_cash_basis_and_date(self):
        p = reserves();p["reservations"]["value"]["items"] *= 2
        self.assertEqual(reserve_obligations(p)["status"], "invalid")
        p = reserves();p["cash_basis"] = "available_after_holds"
        self.assertEqual(reserve_obligations(p)["status"], "invalid")
        p = reserves();p["obligations"]["value"]["items"][0]["due_date"] = "2026-02-30"
        self.assertEqual(reserve_obligations(p)["status"], "invalid")


class CoverageScenarios(unittest.TestCase):
    def test_shared_annual_deductible_and_mechanism_separation(self):
        # A:(10000-1000-(3000-1000))*.8=5600; B shared remaining400.
        # Independent fixed benefit5000 does not consume medical expense.
        r = check_coverage_scenario(coverage())
        self.assertEqual(r["status"], "complete")
        self.assertEqual([row["conditional_payment_minor"] for row in r["rows"]], [560000,40000,500000])
        self.assertEqual(r["calculations"]["expense_compensation_minor"], 600000)
        self.assertEqual(r["calculations"]["fixed_benefit_minor"], 500000)
        self.assertFalse(r["calculations"]["claim_approved"])

    def test_progressive_reimbursement(self):
        p = coverage();t = p["terms"]["value"];t["responsibilities"] = t["responsibilities"][:1]
        t["payment_order"] = ["medical-A"];t["responsibilities"][0].update(deductible_id="none",shared_limit_id="none",
            reimbursement_tiers=[dict(up_to="10000",rate="0.8"),dict(up_to=None,rate="1")])
        p["scenario"]["costs"][0].update(amount="15000",other_reimbursements="0")
        self.assertEqual(check_coverage_scenario(p)["rows"][0]["conditional_payment_minor"], 1300000)

    def test_policy_annual_and_per_claim_caps(self):
        p = coverage();t = p["terms"]["value"];t["policy_annual_limit"] = "10000";t["policy_annual_used"] = "8000"
        t["responsibilities"][0]["per_claim_limit"] = "1500"
        r = check_coverage_scenario(p)
        self.assertEqual([row["conditional_payment_minor"] for row in r["rows"]], [150000,50000,0])

    def test_same_expense_cannot_be_reimbursed_twice(self):
        p = coverage();t=p["terms"]["value"];t["shared_limits"][0]["limit"] = "50000"
        t["deductibles"][0]["amount"] = "0";t["deductibles"][0]["used"] = "0"
        for row in t["responsibilities"][:2]:
            row.update(cost_ids=["A"],reimbursement_tiers=[dict(up_to=None,rate="1")])
        self.assertEqual(check_coverage_scenario(p)["calculations"]["expense_compensation_minor"],900000)

    def test_missing_conditions_no_assumed_eligibility(self):
        p=coverage();p["scenario"]["conditions"]["exclusions_cleared"] = None
        r=check_coverage_scenario(p);self.assertEqual(r["status"],"missing");self.assertIsNone(r["calculations"])

    def test_waiting_end_inclusive_and_invalid_rule_order(self):
        p=coverage();p["scenario"]["event_date"]="2026-01-31"
        self.assertEqual(check_coverage_scenario(p)["status"],"ineligible")
        p["terms"]["value"]["waiting_end_inclusive"]=False
        self.assertEqual(check_coverage_scenario(p)["status"],"complete")
        p["terms"]["value"]["deduction_order"]=["ratio","limits"]
        self.assertEqual(check_coverage_scenario(p)["status"],"unsupported")

    def test_fixed_benefit_does_not_require_medical_provider(self):
        p=coverage();p["terms"]["value"]["responsibilities"]=p["terms"]["value"]["responsibilities"][2:]
        p["terms"]["value"]["payment_order"]=["critical"];p["scenario"]["conditions"].pop("provider_eligible")
        self.assertEqual(check_coverage_scenario(p)["calculations"]["fixed_benefit_minor"],500000)

    def test_annual_usage_cannot_cross_contract_year(self):
        p=coverage();p["terms"]["value"].update(policy_end="2027-12-31")
        p["scenario"]["event_date"]="2027-01-01"
        r=check_coverage_scenario(p)
        self.assertEqual(r["status"],"missing");self.assertIsNone(r["calculations"])

    def test_late_invalid_responsibility_does_not_leave_payable_rows(self):
        p=coverage();p["terms"]["value"]["responsibilities"][-1]["annual_used"]="5001"
        r=check_coverage_scenario(p)
        self.assertEqual(r["status"],"invalid");self.assertEqual(r["rows"],[])


class PensionFlows(unittest.TestCase):
    def test_guaranteed_10_percent_and_illustrative_20_percent(self):
        r=pension_cashflows(pension());c=r["calculations"]
        self.assertEqual(r["status"],"complete")
        self.assertEqual(c["guaranteed_IRR"]["value"],"0.100000000000")
        self.assertEqual(c["illustrative_IRR"]["value"],"0.200000000000")
        self.assertEqual(c["guaranteed_net_minor"],10000)
        self.assertFalse(c["illustrative_is_guaranteed"])

    def test_incomplete_schedule_has_no_IRR_or_complete_total(self):
        p=pension();p["cashflow_schedule"]["value"]["complete"]=False
        r=pension_cashflows(p);self.assertEqual(r["status"],"partial")
        self.assertIsNone(r["calculations"]["guaranteed_IRR"]["value"])
        self.assertIsNone(r["calculations"]["guaranteed_net_minor"])

    def test_nonconventional_cashflow_does_not_select_arbitrary_root(self):
        p=pension();p["cashflow_schedule"]["value"]["items"][1]["date"]="2026-06-01"
        p["cashflow_schedule"]["value"]["items"].append(dict(cashflow_id="later-premium", date="2026-10-01", amount="-500",kind="premium",guaranteed=True))
        self.assertEqual(pension_cashflows(p)["calculations"]["guaranteed_IRR"]["status"],"ambiguous")

    def test_terminal_value_and_cashflow_sign_required(self):
        p=pension();p["cashflow_schedule"]["value"]["items"].pop()
        self.assertEqual(pension_cashflows(p)["status"],"missing")
        p=pension();p["cashflow_schedule"]["value"]["items"][0]["amount"]="1000"
        self.assertEqual(pension_cashflows(p)["status"],"invalid")

    def test_leap_year_actual_day_basis(self):
        p=pension();s=p["cashflow_schedule"]["value"];s["projection_end"]="2025-01-01"
        for row in s["items"]:row["date"]="2024-01-01" if row["kind"]=="premium" else "2025-01-01"
        r=pension_cashflows(p)
        self.assertAlmostEqual(float(r["calculations"]["guaranteed_IRR"]["value"]), math.pow(1.1,365/366)-1,places=10)

    def test_terminal_value_not_duplicated_as_intermediate_surrender(self):
        p=pension();p["cashflow_schedule"]["value"]["items"].append(dict(cashflow_id="duplicate-terminal",
            date="2026-11-01", amount="1000", kind="terminal_value", guaranteed=True))
        self.assertEqual(pension_cashflows(p)["status"],"invalid")


class CommonContracts(unittest.TestCase):
    def test_public_call_signature_is_one_JSON_payload(self):
        for fn in (compare_share_costs,reserve_obligations,check_coverage_scenario,pension_cashflows):
            self.assertEqual(list(inspect.signature(fn).parameters),["payload"])

    def test_version_state_and_currency_rejection(self):
        for value,status in [("conflict","conflict"),("stale","stale"),("restricted","restricted")]:
            p=reserves();p["available_cash"]["state"]=value
            self.assertEqual(reserve_obligations(p)["status"],status)
        p=reserves();p["available_cash"]["source_version"]="other"
        self.assertEqual(reserve_obligations(p)["status"],"conflict")
        p=reserves();p["available_cash"]["currency"]="USD"
        self.assertEqual(reserve_obligations(p)["status"],"conflict")

    def test_floats_boolean_nan_and_unsafe_minor_range(self):
        for amount in (True, 1.5, "NaN", "1e100", "90071992547409.92", "1.001"):
            p=reserves();p["available_cash"]["value"]=amount
            self.assertEqual(reserve_obligations(p)["status"],"invalid")

    def test_functions_do_not_mutate_inputs_and_are_JSON_serializable(self):
        for fn, factory in [(compare_share_costs,comparison),(reserve_obligations,reserves),
                            (check_coverage_scenario,coverage),(pension_cashflows,pension)]:
            p=factory();before=deepcopy(p);r=fn(p)
            self.assertEqual(p,before);self.assertFalse(r["execution_authority"])
            json.dumps(r,allow_nan=False)


if __name__ == "__main__":
    unittest.main()
