"""集成方独立手算合同；只测纯模块快照，不代表服务接线或金融建议通过。

复现：python3 -B -m unittest discover -s tests/window_integration -p test_financial_metrics.py -v
预期从事先冻结的 metric_oracles.json 读取；不导入金融窗口测试，不生成预期。
"""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
ORACLE_PATH = ROOT / "evaluation/window_integration/metric_oracles.json"
FROZEN_ORACLE_SHA256 = "556351bd8fdd4b2c1436861652eabdd312a6676b66746fa248bf2d21589488ee"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IndependentFinancialMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sha(ORACLE_PATH) != FROZEN_ORACLE_SHA256:
            raise RuntimeError("frozen_oracle_changed: 独立预期文件与冻结SHA不符")
        cls.oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cls.oracle["cases"]}
        if len(cls.cases) != 10 or not cls.oracle["expectations_frozen_before_execution"]:
            raise RuntimeError("independent_oracle_contract_invalid")
        cls.before = {path: sha(ROOT / path) for path in cls.oracle["source_baseline_sha256"]}
        # 正式复现要求新进程，避免把进程缓存的旧模块当成当前磁盘候选。
        if any(name == "financial_methods" or name.startswith("financial_methods.") for name in sys.modules):
            raise RuntimeError("fresh_process_required: 请单独运行本文件的复现命令")
        sys.path.insert(0, str(ROOT))
        cls.addClassCleanup(lambda: sys.path.remove(str(ROOT)))
        # 隔离纯计算：任何意外联网立即失败，不加载统一密钥或现役数据库。
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("network_forbidden_in_financial_oracle"))
            guard.start()
            cls.addClassCleanup(guard.stop)
        cls.c = importlib.import_module("financial_methods.contracts")
        cls.f = importlib.import_module("financial_methods.funding")
        cls.a = importlib.import_module("financial_methods.accounting")
        cls.r = importlib.import_module("financial_methods.returns")
        cls.p = importlib.import_module("financial_methods.allocation")
        cls.after_import = {path: sha(ROOT / path) for path in cls.before}
        if cls.after_import != cls.before:
            raise RuntimeError("candidate_changed_during_import: 不接收混合源码结果")

    @classmethod
    def tearDownClass(cls):
        after = {path: sha(ROOT / path) for path in cls.before}
        if after != cls.before or sha(ORACLE_PATH) != FROZEN_ORACLE_SHA256:
            raise RuntimeError("candidate_or_oracle_changed_during_test: 本轮结果无效")
        # 输出实际执行快照；旧报告和编写时版本不能冒充本轮。
        print(json.dumps({"suite": cls.oracle["suite_id"], "snapshot_verification_only": True,
                          "oracle_sha256": FROZEN_ORACLE_SHA256,
                          "candidate_sha256": cls.before,
                          "candidate_matches_authoring_baseline": cls.before == cls.oracle["source_baseline_sha256"],
                          "network_calls_allowed": False, "cloud_model_calls": 0},
                         ensure_ascii=False, sort_keys=True))

    def common_identity(self):
        common = self.oracle["common"]
        return {name: common[name] for name in ("subject_id", "currency")}

    def context(self, *, on=None, **changes):
        raw = {key: value for key, value in self.oracle["common"].items() if key != "evidence_policy"}
        raw["input_refs"] = tuple(raw["input_refs"])
        raw["source_versions"] = tuple(tuple(pair) for pair in raw["source_versions"])
        if on is not None:
            raw.pop("period_start")
            raw.pop("period_end")
            raw["as_of"] = on
        return self.c.MetricContext(**{**raw, **changes})

    @staticmethod
    def evidence(stable_id):
        return {"evidence_ref": "synthetic:" + stable_id, "source_version": "fixture-v1"}

    @staticmethod
    def evidence_sequence(stable_id):
        ref = "synthetic:" + stable_id
        return {"input_refs": (ref,), "source_versions": ((ref, "fixture-v1"),)}

    def funding_inputs(self, case):
        common = self.oracle["common"]
        snap = self.f.CashSnapshot(**{**self.common_identity(), "as_of": common["period_start"],
                                    "coverage_start": common["period_start"], "coverage_end": common["period_end"],
                                    **self.evidence("cash-snapshot"), **case["cash_snapshot"]})
        events = tuple(self.f.FundingEvent(**{**self.common_identity(), **self.evidence(e["event_id"]), **e})
                       for e in case.get("events", []))
        holds = tuple(self.f.Hold(**{**self.common_identity(), **self.evidence(h["hold_id"]), **h,
                                    "commitment_allocations": tuple(tuple(pair) for pair in h.get("commitment_allocations", []))})
                      for h in case.get("holds", []))
        reserves = tuple(self.f.PlanningReserve(**{**self.common_identity(), **self.evidence(r["reserve_id"]), **r,
                                                 "commitment_ids": tuple(r.get("commitment_ids", []))})
                         for r in case.get("planning_reserves", []))
        return snap, events, holds, reserves

    def equity_snapshot(self, raw):
        values = {**self.common_identity(), **raw}
        values["holdings"] = None if raw["holdings"] is None else tuple(
            self.r.HoldingValue(**{"currency": values["currency"], **holding}) for holding in raw["holdings"])
        values["expected_instrument_ids"] = tuple(raw["expected_instrument_ids"])
        return self.r.EquitySnapshot(**values)

    def money(self, metric, expected, status="calculated"):
        payload = metric if isinstance(metric, dict) else metric.to_dict()
        self.assertEqual(payload["value_minor"], expected)
        self.assertEqual(payload["calculation_status"], status)
        self.assertEqual(payload["unit"], "minor_currency")
        self.assertEqual(payload["subject_id"], self.oracle["common"]["subject_id"])
        self.assertEqual(payload["currency"], self.oracle["common"]["currency"])
        self.assertTrue(payload["input_refs"])
        self.assertTrue(payload["source_versions"])
        self.assertTrue(payload["formula_id"])
        if expected is None:
            self.assertTrue(payload["coverage_gaps"])
        else:
            self.assertIs(type(payload["value_minor"]), int)

    def funding_expected(self, result, case):
        self.assertEqual(result.status, case["expected"]["status"])
        for name, expected in case["expected"]["metrics"].items():
            self.money(result.metrics[name], expected)

    def cash_goal(self, amount, stable_id="cash", **changes):
        return self.p.AllocationGoal(**{**self.common_identity(), "data_scope": self.oracle["common"]["data_scope"],
                                       "goal_id": stable_id, "amount_minor": amount, "priority": 0,
                                       "needed_on": "2026-10-25", "confirmed": True,
                                       **self.evidence_sequence(stable_id), **changes})

    def allocation_inputs(self, case):
        funds = self.c.money_metric(self.context(), "arrangeable_now", "floor_surplus", "funds.current_budget/1", case["funds_minor"])
        key = self.p.ProductKey(**case["product_key"])
        goals = (self.cash_goal(case["cash_goal_minor"]),
                 self.cash_goal(case["investment_goal_minor"], "invest", priority=1, product_key=key))
        identity = {**self.common_identity(), "data_scope": self.oracle["common"]["data_scope"]}
        product = self.p.ProductTerms(**{**identity, "key": key, **case["product_terms"], **self.evidence_sequence("product-terms")})
        risk = self.p.FormalRisk(**{**identity, **case["formal_risk"], **self.evidence_sequence("formal-risk")})
        return funds, goals, product, risk

    @staticmethod
    def confirmed_plan(result):
        return next(plan for plan in result["plans"] if plan["plan_id"] == "confirmed_uses")

    def test_FM01_emergency_floor_once_future_inflow_not_current_budget(self):
        case = self.cases["FM01"]
        result = self.f.evaluate_funding(self.context(), *self.funding_inputs(case))
        self.funding_expected(result, case)
        metric = result.metrics["current_budget"]
        self.assertEqual(metric.basis, case["expected"]["budget_basis"])
        self.assertEqual(metric.formula_id, case["expected"]["formula_id"])
        self.assertEqual((metric.period_start, metric.period_end), ("2026-10-06", "2026-10-31"))
        self.assertEqual(result.metrics["cash_balance"].as_of, "2026-10-06")

    def test_FM02_linked_obligation_hold_and_planning_reserve_deduplicate(self):
        case = self.cases["FM02"]
        snap, events, holds, reserves = self.funding_inputs(case)
        result = self.f.evaluate_funding(self.context(), snap, events, holds, reserves)
        self.funding_expected(result, case)
        self.assertEqual(result.planning_reserves[0]["already_counted_commitment_minor"], case["expected"]["reserve"]["covered_by_commitments_minor"])
        self.assertEqual(result.planning_reserves[0]["additional_minor"], case["expected"]["reserve"]["additional_reserve_minor"])
        duplicate = self.f.evaluate_funding(self.context(), snap, events + (events[0],), holds + holds, reserves + reserves)
        self.money(duplicate.metrics["current_budget"], case["expected"]["same_id_duplicate_budget_minor"])
        self.money(duplicate.metrics["scenario_budget"], case["expected"]["same_id_duplicate_budget_minor"])
        unlinked = replace(reserves[0], reserve_id="unlinked-same-amount", commitment_ids=(), **self.evidence("unlinked-same-amount"))
        separate = self.f.evaluate_funding(self.context(), snap, events, holds, reserves + (unlinked,))
        self.money(separate.metrics["scenario_budget"], case["expected"]["unlinked_same_amount_scenario_minor"])

    def test_FM03_confirmed_candidate_branches_use_dates_and_coverage(self):
        case = self.cases["FM03"]
        result = self.f.evaluate_funding(self.context(), *self.funding_inputs(case))
        self.funding_expected(result, case)
        for key, expected in case["expected"]["floor_judgments"].items():
            self.assertEqual(result.floor_judgments[key], expected)
        self.assertEqual(set(result.excluded_items), set(case["expected"]["excluded_items"]))
        self.assertEqual([row["on"] for row in result.checkpoints], ["2026-10-10", "2026-10-15", "2026-10-20", "2026-10-22"])

    def test_FM04_planning_preserves_input_and_current_baseline(self):
        case = self.cases["FM04"]
        expected = case["expected"]
        snap, events, holds, reserves = self.funding_inputs(case)
        input_before = copy.deepcopy([asdict(snap), [asdict(e) for e in events], [asdict(r) for r in reserves]])
        baseline = self.f.evaluate_funding(self.context(), snap, events, holds)
        planned = self.f.evaluate_funding(self.context(), snap, events, holds, reserves)
        self.money(baseline.metrics["current_budget"], expected["current_budget_minor"])
        self.money(planned.metrics["current_budget"], expected["current_budget_minor"])
        self.money(planned.metrics["scenario_budget"], expected["scenario_budget_minor"])
        self.assertEqual(planned.planning_reserves[0]["additional_minor"], expected["additional_reserve_minor"])
        metric_before = baseline.metrics["current_budget"].to_dict()
        plan = self.confirmed_plan(self.p.arrange_funds(baseline.metrics["current_budget"], (self.cash_goal(100000),)))
        self.money(plan["metrics"]["assigned_cash"], expected["allocation_assigned_cash_minor"])
        self.money(plan["metrics"]["unassigned_cash"], expected["allocation_unassigned_cash_minor"])
        self.assertEqual(plan["execution_authority"], expected["execution_authority"])
        self.assertEqual(baseline.metrics["current_budget"].to_dict(), metric_before)
        self.assertEqual([asdict(snap), [asdict(e) for e in events], [asdict(r) for r in reserves]], input_before)

    def test_FM05_internal_transfer_and_principal_owner_flows_not_income(self):
        case = self.cases["FM05"]
        moves = tuple(self.a.CashMovement(**{**self.common_identity(), **self.evidence(m["movement_id"]), **m}) for m in case["movements"])
        result = self.a.cash_statement(self.context(), case["opening_cash_minor"], case["closing_cash_minor"], moves)
        for name, expected in case["expected"]["metrics"].items():
            self.money(result[name], expected)
        pair_only = self.a.cash_statement(self.context(), 500000, 500000, moves[:2])
        self.money(pair_only["total_cash_change"], case["expected"]["internal_pair_only_change_minor"])
        self.money(pair_only["cash_income"], case["expected"]["internal_pair_only_income_minor"])
        incomplete = self.a.cash_statement(self.context(), 500000, 400000, moves[:1])
        self.money(incomplete["total_cash_change"], None, "unavailable")
        self.assertIn(case["expected"]["incomplete_pair_gap"], incomplete["total_cash_change"].coverage_gaps)

    def test_FM06_equity_period_profit_excludes_owner_flows(self):
        case = self.cases["FM06"]
        expected = case["expected"]
        opening, closing = self.equity_snapshot(case["opening"]), self.equity_snapshot(case["closing"])
        self.money(self.r.portfolio_equity(self.context(on=opening.on), opening), expected["opening_equity_minor"])
        self.money(self.r.holdings_market_value(self.context(on=closing.on), closing), expected["closing_market_value_minor"])
        self.money(self.r.portfolio_equity(self.context(on=closing.on), closing), expected["closing_equity_minor"])
        pnl = self.r.period_profit(self.context(), opening, closing,
                                   owner_contributions_minor=case["owner_contributions_minor"], owner_withdrawals_minor=case["owner_withdrawals_minor"])
        self.money(pnl, expected["period_profit_minor"])
        recomposed = replace(closing, cash_minor=54000, receivables_minor=0, payables_minor=0)
        self.money(self.r.portfolio_equity(self.context(on=closing.on), recomposed), expected["recomposed_equity_minor"])
        simple = self.r.simple_return(self.context(), opening_equity_minor=50000, closing_equity_minor=69000,
                                      owner_contributions_minor=30000, owner_withdrawals_minor=10000).to_dict()
        self.assertEqual(simple["calculation_status"], expected["nonzero_owner_flows_simple_return_status"])
        self.assertIsNone(simple["value_decimal"])
        self.assertIn(expected["simple_return_gap"], simple["coverage_gaps"])

    def test_FM07_missing_nav_opening_and_owner_flow_are_unknown(self):
        case = self.cases["FM07"]
        expected = case["expected"]
        missing = self.equity_snapshot(case["snapshot"])
        metric = self.r.portfolio_equity(self.context(on=missing.on), missing)
        self.money(metric, expected["missing_price_value"], expected["missing_price_status"])
        self.assertIn(expected["missing_price_gap"], metric.coverage_gaps)
        zero = replace(missing, holdings=(replace(missing.holdings[0], quantity="0"),))
        self.money(self.r.portfolio_equity(self.context(on=zero.on), zero), expected["known_zero_equity_minor"])
        closing = replace(missing, holdings=(replace(missing.holdings[0], unit_price=case["known_price"]),))
        self.money(self.r.portfolio_equity(self.context(on=closing.on), closing), expected["known_closing_equity_minor"])
        opening = replace(missing, on="2026-10-06")
        pnl = self.r.period_profit(self.context(), opening, closing, owner_contributions_minor=0, owner_withdrawals_minor=0)
        self.money(pnl, None, expected["missing_opening_profit_status"])
        self.assertIn(expected["missing_price_gap"], pnl.coverage_gaps)
        known_opening = replace(closing, on="2026-10-06")
        missing_flow = self.r.period_profit(self.context(), known_opening, closing, owner_contributions_minor=None, owner_withdrawals_minor=0)
        self.money(missing_flow, None, "unavailable")
        self.assertIn(expected["missing_owner_flow_gap"], missing_flow.coverage_gaps)

    def test_FM08_time_weighted_return_independent_eight_percent(self):
        case = self.cases["FM08"]
        expected = case["expected"]
        parts = tuple(self.r.ReturnSubperiod(**{**self.common_identity(), **raw}) for raw in case["subperiods"])
        bounds = tuple(self.r.ReturnBoundary(**{**self.common_identity(), **raw}) for raw in case["boundaries"])
        ratio = self.r.time_weighted_return(self.context(), parts, bounds).to_dict()
        self.assertEqual(Decimal(ratio["value_decimal"]), Decimal(expected["ratio_decimal"]))
        self.assertEqual(ratio["unit"], expected["unit"])
        self.assertEqual(ratio["calculation_status"], expected["status"])
        self.assertNotIn("value_minor", ratio)
        bad = self.r.time_weighted_return(self.context(), parts, (replace(bounds[0], contribution_minor=30001),)).to_dict()
        self.assertIsNone(bad["value_decimal"])
        self.assertIn(expected["bad_boundary_gap"], bad["coverage_gaps"])
        unknown = self.r.time_weighted_return(self.context(), (parts[0], replace(parts[1], opening_equity_minor=None)), bounds).to_dict()
        self.assertIsNone(unknown["value_decimal"])
        self.assertIn(expected["missing_valuation_gap"], unknown["coverage_gaps"])
        simple = self.r.simple_return(self.context(), opening_equity_minor=100000, closing_equity_minor=135000,
                                      owner_contributions_minor=30000, owner_withdrawals_minor=0).to_dict()
        self.assertIsNone(simple["value_decimal"])
        self.assertIn(expected["simple_return_flow_gap"], simple["coverage_gaps"])

    def test_FM09_fees_rounding_and_cash_conservation(self):
        case = self.cases["FM09"]
        expected = case["expected"]
        funds, goals, product, risk = self.allocation_inputs(case)
        plan = self.confirmed_plan(self.p.arrange_funds(funds, goals, (product,), risk))
        for name, value in expected["metrics"].items():
            self.money(plan["metrics"][name], value)
        for key in ("feasibility", "conservation_difference_minor", "cash_partition_difference_minor", "execution_authority", "future_proceeds_minor"):
            self.assertEqual(plan[key], expected[key])
        self.assertEqual(plan["calculation_status"], expected["status"])
        variant = case["rounding_variant"]
        tiny_funds = self.c.money_metric(self.context(), "arrangeable_now", "floor_surplus", "funds.current_budget/1", variant["funds_minor"])
        tiny_goal = replace(goals[1], amount_minor=variant["principal_minor"])
        for rule, fee in variant["expected_fees"].items():
            with self.subTest(rounding=rule):
                terms = replace(product, minimum_minor=250, step_minor=1, entry_fixed_minor=0,
                                entry_rate=variant["entry_rate"], exit_rate="0", fee_rounding=rule)
                tiny = self.confirmed_plan(self.p.arrange_funds(tiny_funds, (tiny_goal,), (terms,), risk))
                self.money(tiny["metrics"]["entry_fees"], fee)
                self.money(tiny["metrics"]["unassigned_cash"], variant["expected_unassigned"][rule])

    def test_FM10_identity_period_currency_and_source_version_rejected(self):
        case = self.cases["FM10"]
        expected = case["expected"]
        snap, events, holds, reserves = self.funding_inputs(case)
        for rejection in case["funding_rejections"]:
            with self.subTest(mutation=rejection["mutation"]):
                result = self.f.evaluate_funding(self.context(), replace(snap, **rejection["mutation"]))
                self.assertEqual(result.status, expected["rejected_status"])
                for metric in result.metrics.values():
                    self.money(metric, expected["rejected_value"], expected["rejected_status"])
                self.assertIn(rejection["gap"], result.coverage_gaps)
        conflict = self.context(source_versions=((snap.evidence_ref, "other-version"),))
        result = self.f.evaluate_funding(conflict, snap)
        self.assertEqual(result.status, expected["rejected_status"])
        self.assertIn(expected["version_conflict_gap"], result.coverage_gaps)
        funds, goals, product, risk = self.allocation_inputs(self.cases["FM09"])
        mismatch = replace(goals[1], source_versions=((funds.input_refs[0], "other-version"),))
        plan = self.confirmed_plan(self.p.arrange_funds(funds, (goals[0], mismatch), (product,), risk))
        self.assertEqual(plan["calculation_status"], "unavailable")
        self.assertIn(expected["allocation_version_conflict_gap"], plan["coverage_gaps"])
        wrong_share = replace(goals[1], product_key=replace(product.key, share_class_id="synthetic-share-B"))
        share_plan = self.confirmed_plan(self.p.arrange_funds(funds, (wrong_share,), (product,), risk))
        self.assertIn(expected["allocation_share_mismatch_gap"], share_plan["coverage_gaps"])
        self.assertIsNone(share_plan["metrics"]["investment_principal"]["value_minor"])
        outside = self.confirmed_plan(self.p.arrange_funds(funds, (replace(goals[0], needed_on="2026-11-01"),)))
        self.assertEqual(outside["calculation_status"], "infeasible")
        self.assertIn(expected["allocation_period_gap"], outside["coverage_gaps"])
        with self.assertRaises(ValueError):
            replace(funds, contract_version="0.0.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
