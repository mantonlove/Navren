"""纯合成权威方法输出→指标消费合同，无用户资料、模型或业务操作。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.metric_claims import (
    MetricClaimError, MetricRegistry, amount_claim_schema, monetary_occurrences,
    merge_source_versions)
from financial_methods.contracts import MetricContext, money_metric


def synthetic_metric(*, subject="synthetic-account-main", currency="CNY",
                     start="2026-10-03", end="2026-11-02", as_of=None,
                     scope="synthetic_bank_recorded_items", name="arrangeable_now",
                     basis="floor_surplus", formula="funds.current_budget/1",
                     value=1027600, status="calculated", gaps=(), assumptions=(),
                     version="synthetic-version-1", excluded=("emergency_floor_already_deducted",)):
    context = MetricContext(subject, currency, scope, as_of=as_of,
                            period_start=None if as_of else start,
                            period_end=None if as_of else end,
                            input_refs=("synthetic-bank-evidence",),
                            source_versions=(("synthetic-bank", version),))
    return money_metric(context, name, basis, formula, value, status=status,
                        gaps=gaps, assumptions=assumptions, excluded=excluded)


class MetricClaimTest(unittest.TestCase):
    def setUp(self):
        self.registry = MetricRegistry()
        self.metric = synthetic_metric()
        self.registry.register(self.metric, kind="recorded_fact", evidence_id="evidence-1")
        self.current = {"synthetic-bank": "synthetic-version-1"}
        self.text = "日常账户当前可安排资金为10,276.00元。"
        self.claim = {**self.registry.binding(self.metric.metric_id), "text": self.text}

    def validate(self, text=None, claims=None, versions=None, **kwargs):
        return self.registry.validate_answer(
            self.text if text is None else text,
            [self.claim] if claims is None else claims,
            current_source_versions=self.current if versions is None else versions, **kwargs)

    def assert_error(self, code, callback, field=None):
        with self.assertRaises(MetricClaimError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        if field:
            self.assertEqual(caught.exception.field, field)

    def test_complete_method_snapshot_is_registered_and_reviewed(self):
        result = self.validate()
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["amount_claims"][0]["metric"], self.metric.to_dict())
        self.assertEqual(result["amount_occurrence_count"], 1)
        self.assertTrue(result["independent_review_required"])
        self.assertEqual(result["amount_claims"][0]["metric"]["excluded_items"],
                         ["emergency_floor_already_deducted"])
        json.dumps(result, allow_nan=False)

    def test_lookup_mutation_cannot_change_registered_authority(self):
        snapshot = self.registry.lookup(self.metric.metric_id)
        snapshot["metric"]["value_minor"] = 0
        snapshot["metric"]["source_versions"]["synthetic-bank"] = "forged"
        self.assertEqual(self.registry.lookup(self.metric.metric_id)["metric"], self.metric.to_dict())
        self.validate()

    def test_named_service_mapping_and_repeat_read_preserve_snapshot(self):
        ids = self.registry.register({"current_budget": self.metric.to_dict()},
                                     kind="recorded_fact", evidence_id="evidence-2")
        self.assertEqual(ids, [self.metric.metric_id])
        self.assertEqual(self.registry.lookup(ids[0])["evidence_ids"], ["evidence-1", "evidence-2"])

    def test_same_id_different_snapshot_is_rejected_atomically(self):
        other = synthetic_metric(subject="synthetic-account-backup")
        forged = self.metric.to_dict()
        forged["subject_id"] = other.subject_id
        self.assert_error("METRIC_SNAPSHOT_CONFLICT", lambda: self.registry.register(
            [other, forged], kind="recorded_fact", evidence_id="evidence-2"))
        self.assertEqual(self.registry.metric_ids, (self.metric.metric_id,))

    def test_legacy_tool_amount_cannot_become_formal_metric(self):
        self.assert_error("METRIC_CONTRACT_REQUIRED", lambda: self.registry.register(
            {"investable_minor": 1027600}, kind="recorded_fact", evidence_id="evidence-legacy"))
        self.assert_error("METRIC_AMOUNT_UNBOUND", lambda: MetricRegistry().validate_answer(
            self.text, [], current_source_versions=self.current))

    def test_same_number_wrong_subject_is_rejected(self):
        claim = {**self.claim, "subject_id": "synthetic-account-backup"}
        self.assert_error("METRIC_CLAIM_MISMATCH", lambda: self.validate(claims=[claim]), "subject_id")

    def test_same_number_wrong_metric_or_deduction_basis_is_rejected(self):
        for field, value in (("metric_name", "cash_balance"), ("basis", "ledger_cash_balance"),
                             ("formula_id", "funds.balance/1"), ("data_scope", "real_bank")):
            with self.subTest(field=field):
                self.assert_error("METRIC_CLAIM_MISMATCH",
                                  lambda: self.validate(claims=[{**self.claim, field: value}]), field)

    def test_same_number_wrong_period_or_as_of_is_rejected(self):
        for field, value in (("period_end", "2026-12-02"), ("period_start", "2026-09-03"),
                             ("as_of", "2026-10-03")):
            with self.subTest(field=field):
                self.assert_error("METRIC_CLAIM_MISMATCH",
                                  lambda: self.validate(claims=[{**self.claim, field: value}]), field)

    def test_cross_currency_and_unit_cannot_reuse_same_number(self):
        for field, value in (("currency", "USD"), ("unit", "public_minor_currency")):
            with self.subTest(field=field):
                self.assert_error("METRIC_CLAIM_MISMATCH",
                                  lambda: self.validate(claims=[{**self.claim, field: value}]), field)
        bad = "日常账户当前可安排资金为10,276.00美元。"
        self.assert_error("METRIC_LITERAL_MISMATCH",
                          lambda: self.validate(text=bad, claims=[{**self.claim, "text": bad}]))

    def test_stale_source_and_missing_source_are_rejected(self):
        for versions in ({"synthetic-bank": "synthetic-version-2"}, {}):
            with self.subTest(versions=versions):
                self.assert_error("STALE_METRIC_SOURCE", lambda: self.validate(versions=versions),
                                  "synthetic-bank")

    def test_claim_cannot_relabel_source_version(self):
        claim = {**self.claim, "source_versions": {"synthetic-bank": "synthetic-version-2"}}
        self.assert_error("METRIC_CLAIM_MISMATCH", lambda: self.validate(claims=[claim]), "source_versions")

    def test_unknown_is_not_zero_and_cannot_emit_any_amount(self):
        metric = synthetic_metric(value=None, status="unavailable", gaps=("income_missing",))
        self.registry.register(metric, kind="recorded_fact", evidence_id="unknown-evidence")
        claim = {**self.registry.binding(metric.metric_id), "text": "当前可安排资金未知。"}
        result = self.validate(text=claim["text"], claims=[claim])
        self.assertIsNone(result["amount_claims"][0]["metric"]["value_minor"])
        self.assert_error("METRIC_CLAIM_MISMATCH", lambda: self.validate(
            text="当前可安排资金为0元。", claims=[{**claim, "text": "当前可安排资金为0元。", "value_minor": 0}]))
        self.assert_error("METRIC_UNKNOWN_NOT_ZERO", lambda: self.validate(
            text="当前可安排资金为0元。", claims=[{**claim, "text": "当前可安排资金为0元。"}]))

    def test_known_recorded_zero_is_valid_and_distinct_from_unknown(self):
        metric = synthetic_metric(value=0, name="confirmed_future_inflow", basis="recorded_future_items",
                                  formula="synthetic.confirmed_inflow/1")
        self.registry.register(metric, kind="recorded_projection", evidence_id="zero-evidence")
        text = "区间内已确认未来入账为0元。"
        self.validate(text=text, claims=[{**self.registry.binding(metric.metric_id), "text": text}])

    def test_scenario_cannot_be_registered_or_claimed_as_fact(self):
        metric = synthetic_metric(name="planning_arrangeable_now", basis="planning_floor_surplus",
                                  formula="funds.planning_budget/1", value=977600,
                                  assumptions=({"reserve_id": "synthetic-reserve", "target_minor": 250000},))
        self.assert_error("METRIC_KIND_MISMATCH", lambda: self.registry.register(
            metric, kind="recorded_fact", evidence_id="scenario-evidence"))
        self.registry.register(metric, kind="planning_scenario", evidence_id="scenario-evidence")
        text = "规划情景可安排资金为9,776.00元。"
        claim = {**self.registry.binding(metric.metric_id), "text": text}
        self.validate(text=text, claims=[claim])
        self.assert_error("METRIC_KIND_MISMATCH", lambda: self.validate(
            text=text, claims=[{**claim, "claim_kind": "recorded_fact"}]))

    def test_candidate_branch_is_not_confirmed_branch(self):
        metric = synthetic_metric(name="minimum_if_candidates_occur", formula="funds.daily_minimum_candidates/1",
                                  value=966400)
        self.registry.register(metric, kind="candidate_scenario", evidence_id="candidate-evidence")
        text = "若候选发生，最低底线外余量为9,664.00元。"
        claim = {**self.registry.binding(metric.metric_id), "text": text}
        self.assert_error("METRIC_KIND_MISMATCH", lambda: self.validate(
            text=text, claims=[{**claim, "claim_kind": "recorded_projection"}]))

    def test_hypothesis_and_forecast_cannot_become_recorded_fact(self):
        metric = synthetic_metric(value=100000, assumptions=({"income_stable": True},))
        self.assert_error("METRIC_ASSUMPTION_AS_FACT", lambda: self.registry.register(
            metric, kind="recorded_fact", evidence_id="assumption-evidence"))
        self.registry.register(metric, kind="hypothesis", evidence_id="assumption-evidence")
        text = "假设条件下可安排1,000.00元。"
        self.assert_error("METRIC_KIND_MISMATCH", lambda: self.validate(
            text=text, claims=[{**self.registry.binding(metric.metric_id), "text": text,
                               "claim_kind": "forecast"}]))
        forecast = synthetic_metric(value=200000, scope="synthetic_forecast")
        self.registry.register(forecast, kind="forecast", evidence_id="forecast-evidence")
        text = "预测金额为2,000.00元。"
        self.assert_error("METRIC_KIND_MISMATCH", lambda: self.validate(
            text=text, claims=[{**self.registry.binding(forecast.metric_id), "text": text,
                               "claim_kind": "recorded_fact"}]))

    def test_base_recorded_forecast_marker_is_preserved_without_promoting_candidates(self):
        metric = synthetic_metric(name="minimum_after_confirmed", formula="funds.daily_minimum/1",
                                  assumptions=({"event_id": "synthetic-forecast", "type": "recorded_forecast_candidate",
                                                "notice": "已记录未付预测，仅候选分支"},))
        self.registry.register(metric, kind="recorded_projection", evidence_id="base-projection")
        text = "已确认分支最低底线外余量为10,276.00元。"
        claim = {**self.registry.binding(metric.metric_id), "text": text}
        result = self.validate(text=text, claims=[claim])
        self.assertEqual(result["amount_claims"][0]["metric"]["assumed_inputs"], metric.to_dict()["assumed_inputs"])
        self.assert_error("METRIC_KIND_MISMATCH", lambda: self.validate(
            text=text, claims=[{**claim, "claim_kind": "candidate_scenario"}]))

    def test_partial_amount_preserves_gaps_and_cannot_become_calculated(self):
        metric = synthetic_metric(status="partial", gaps=("unrecorded_expenses_not_covered",))
        self.registry.register(metric, kind="recorded_fact", evidence_id="partial-evidence")
        claim = {**self.registry.binding(metric.metric_id), "text": self.text}
        result = self.validate(claims=[claim])
        self.assertEqual(result["amount_claims"][0]["metric"]["coverage_gaps"],
                         ["unrecorded_expenses_not_covered"])
        self.assert_error("METRIC_CLAIM_MISMATCH", lambda: self.validate(
            claims=[{**claim, "calculation_status": "calculated"}]), "calculation_status")

    def test_same_amount_in_another_sentence_requires_another_binding(self):
        text = self.text + "备用账户余额也为10,276.00元。"
        self.assert_error("METRIC_AMOUNT_UNBOUND", lambda: self.validate(text=text))

    def test_repeated_fragment_requires_explicit_position(self):
        text = self.text + self.text
        self.assert_error("METRIC_CLAIM_TEXT_AMBIGUOUS", lambda: self.validate(text=text))
        self.validate(text=text, claims=[{**self.claim, "occurrence_start": 0},
                                        {**self.claim, "occurrence_start": len(self.text)}])

    def test_multiple_amounts_require_multiple_metric_claims(self):
        text = "已扣10,000.00元后可安排10,276.00元。"
        self.assert_error("METRIC_ONE_AMOUNT_PER_CLAIM", lambda: self.validate(
            text=text, claims=[{**self.claim, "text": text}]))

    def test_wrong_display_units_and_malformed_precision_are_rejected(self):
        for text, code in (("可安排1,027,600元。", "METRIC_LITERAL_MISMATCH"),
                           ("可安排10,276.001元。", "INVALID_MONEY_TEXT"),
                           ("可安排1.2.3元。", "INVALID_MONEY_TEXT"),
                           ("可安排1e4元。", "INVALID_MONEY_TEXT"),
                           ("可安排−10,276.00元。", "METRIC_LITERAL_MISMATCH")):
            with self.subTest(text=text):
                self.assert_error(code, lambda: self.validate(text=text, claims=[{**self.claim, "text": text}]))

    def test_elided_currency_and_chinese_money_cannot_bypass_empty_claims(self):
        attacks = ("你的账户余额100万。", "你的账户余额一百万元。", "账户余额壹佰萬元整。",
                   "账户余额人民币壹佰万元。", "账户余额100。", "当前可安排资金是100万。",
                   "你能花一百万。", "本月房租为一千八百。", "补助到账了5000。", "剩余100万。",
                   "100是你的余额。", "100现金。", "人民币100。", "$100。")
        for text in attacks:
            with self.subTest(text=text):
                self.assert_error("METRIC_MONEY_SYNTAX_REQUIRED", lambda: monetary_occurrences(text))
                self.assert_error("METRIC_MONEY_SYNTAX_REQUIRED", lambda: self.validate(text=text, claims=[]))

    def test_non_money_duration_rate_risk_grade_and_count_are_not_amounts(self):
        statements = ("期限100天。", "收益率2%。", "正式风险等级R4。", "房租100笔。",
                      "收益增长2%。", "收益翻了2倍。", "房租一百万笔记录。", "收入共100笔。",
                      "现金流覆盖100天，收益率2%，R4，房租100笔。", "R4资金配置仍需研究。")
        for text in statements:
            with self.subTest(text=text):
                self.assertEqual(monetary_occurrences(text), [])
                self.assertEqual(self.validate(text=text, claims=[])["amount_occurrence_count"], 0)

    def test_canonical_full_public_quote_still_passes_original_provenance_path(self):
        text = "公开原文：该政策最高贷款金额为100万元。个人资格未核实，不是你的账户余额。"
        occurrence = monetary_occurrences(text)[0]
        self.assertEqual(occurrence["value_minor"], 100000000)
        self.assertEqual(occurrence["currency"], "CNY")
        self.assert_error("METRIC_AMOUNT_UNBOUND", lambda: self.validate(text=text, claims=[]))
        result = self.validate(text=text, claims=[], public_quote_spans=[
            (occurrence["start"], occurrence["end"])])
        self.assertEqual(result["amount_occurrence_count"], 1)
        self.assertEqual(result["amount_claims"], [])

    def test_signed_profit_and_zero_are_distinct(self):
        metric = synthetic_metric(name="period_pnl", basis="total_equity_period_pnl",
                                  formula="returns.period_pnl.v1", value=-12000,
                                  scope="independent_historical_portfolio")
        self.registry.register(metric, kind="recorded_fact", evidence_id="historical-pnl")
        text = "独立历史组合期间损益为−120.00元。"
        self.validate(text=text, claims=[{**self.registry.binding(metric.metric_id), "text": text}])
        self.assert_error("METRIC_CLAIM_MISMATCH", lambda: self.validate(text=text, claims=[{
            **self.registry.binding(metric.metric_id), "text": text, "data_scope": "personal_cash_balance"}]), "data_scope")

    def test_trusted_metric_versions_merge_without_silent_override(self):
        metric = self.metric.to_dict()
        metric["source_versions"]["synthetic-bank-input-ref"] = "synthetic-version-1"
        merged = merge_source_versions([metric], base_versions=self.current)
        self.assertEqual(merged["synthetic-bank-input-ref"], "synthetic-version-1")
        self.assert_error("METRIC_SOURCE_VERSION_CONFLICT", lambda: merge_source_versions(
            [metric], base_versions={"synthetic-bank": "synthetic-version-2"}), "synthetic-bank")

    def test_public_quote_requires_external_provenance_and_is_not_registered(self):
        text = "公开政策原文补助5,000.00元，个人资格未核实。"
        self.assert_error("METRIC_AMOUNT_UNBOUND", lambda: self.validate(text=text, claims=[]))
        occurrence = monetary_occurrences(text)[0]
        result = self.validate(text=text, claims=[], public_quote_spans=[
            (occurrence["start"], occurrence["end"])])
        self.assertEqual(result["amount_claims"], [])
        self.assertEqual(self.registry.metric_ids, (self.metric.metric_id,))

    def test_invalid_types_and_unknown_id_are_structured_rejections(self):
        for value in (True, 1027600.0, "1027600"):
            metric = self.metric.to_dict()
            metric["value_minor"] = value
            self.assert_error("METRIC_UNKNOWN_NOT_ZERO", lambda: self.registry.register(
                metric, kind="recorded_fact", evidence_id="invalid"))
        for value in (None, [], "missing"):
            self.assert_error("METRIC_NOT_FOUND", lambda: self.registry.lookup(value))
        self.assert_error("METRIC_CLAIM_MISMATCH", lambda: self.validate(
            claims=[{**self.claim, "value_minor": 1027600.0}]), "value_minor")

    def test_as_of_and_period_contract_cannot_be_mixed(self):
        metric = self.metric.to_dict()
        metric["as_of"] = "2026-10-03"
        self.assert_error("METRIC_INVALID_CONTRACT", lambda: self.registry.register(
            metric, kind="recorded_fact", evidence_id="invalid-date"), "period")

    def test_schema_is_json_safe_and_dynamic_ids_are_only_registered_ids(self):
        schema = amount_claim_schema(self.registry.metric_ids)
        self.assertEqual(schema["properties"]["metric_id"]["enum"], [self.metric.metric_id])
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("basis", schema["required"])
        self.assertIn("period_end", schema["required"])
        json.dumps(schema, allow_nan=False)

    def test_actual_base_snapshot_consumes_exact_workspace_scope_and_versions(self):
        from cryptography.fernet import Fernet
        from finance_gateway import FinanceGateway
        with tempfile.TemporaryDirectory(prefix="navren-metric-consumer-test-") as tmp:
            with patch.dict(os.environ, {
                    "NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(),
                    "NAVREN_KNOWLEDGE_DB": str(Path(tmp) / "knowledge.sqlite3")}):
                g = FinanceGateway(Path(tmp) / "bank.sqlite3")
                w, _ = g.create_session()
                before = g.overview(w)
                snapshot = g.financial_metrics.snapshot(w, 60)
                g.financial_metrics.validate(w, snapshot)
                versions = merge_source_versions(snapshot["metrics"], base_versions=snapshot["source_versions"])
                self.assertIn("bank_snapshot/" + w, versions)
                kinds = {"cash_balance": "recorded_fact", "current_available": "recorded_fact",
                         "current_budget": "recorded_fact", "scenario_budget": "planning_scenario",
                         "confirmed_minimum": "recorded_projection", "candidate_minimum": "candidate_scenario",
                         "confirmed_closing_cash": "recorded_projection", "candidate_closing_cash": "candidate_scenario"}
                registry = MetricRegistry()
                for key, metric in snapshot["funding"]["metrics"].items():
                    registry.register(metric, kind=kinds[key], evidence_id=snapshot["snapshot_id"])
                baseline = snapshot["funding"]["metrics"]["current_budget"]
                self.assertEqual(baseline["subject_id"], w)
                value = baseline["value_minor"]
                self.assertIs(type(value), int)
                major, fraction = divmod(abs(value), 100)
                display = ("-" if value < 0 else "") + f"{major:,}.{fraction:02d}元"
                text = "该会话汇总范围的当前可安排金额为" + display + "。"
                claim = {**registry.binding(baseline["metric_id"]), "text": text}
                validated = registry.validate_answer(text, [claim], current_source_versions=versions)
                self.assertEqual(validated["amount_claims"][0]["metric"], baseline)
                wrong = {**claim, "subject_id": next(a["id"] for a in before["accounts"] if a["id"].endswith("-main"))}
                self.assert_error("METRIC_CLAIM_MISMATCH", lambda: registry.validate_answer(
                    text, [wrong], current_source_versions=versions), "subject_id")
                after = g.overview(w)
                for key in ("accounts", "orders", "holdings", "fund_holds", "profile"):
                    self.assertEqual(before[key], after[key])


if __name__ == "__main__":
    unittest.main()
