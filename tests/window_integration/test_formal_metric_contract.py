"""集成方正式指标独立合同验收：原始合成源、真实服务、无云。

复现：python3 -B tests/window_integration/test_formal_metric_contract.py
预期先冻结于 formal_metric_oracles.json；不使用生产计算结果生成黄金值。
压缩传输可以省略重复字段，但消费层必须回查完整本地签名。
"""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "evaluation/window_integration/formal_metric_oracles.json"
ORACLE_SHA256 = "f689bffc3a4442c413a2a1ff3136711cf064bcd3f188ec45a2366f8434c48c70"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(paths):
    return {path: sha(ROOT / path) for path in paths}


class FormalMetricContract(unittest.TestCase):
    """不导入其他窗口 TestCase，不用替身替换 FinancialMetrics。"""

    @classmethod
    def setUpClass(cls):
        if sha(ORACLE) != ORACLE_SHA256:
            raise RuntimeError("frozen_oracle_changed")
        cls.oracle = json.loads(ORACLE.read_text())
        cls.cases = {c["id"]: c for c in cls.oracle["cases"]}
        cls.before = manifest(cls.oracle["authoring_source_sha256"])
        for path, expected in cls.oracle["source_input_identity"].items():
            if cls.before[path] != expected:
                raise RuntimeError("original_synthetic_source_changed:" + path)
        if "financial_metrics" in sys.modules or "agent_runtime.bank_tools" in sys.modules:
            raise RuntimeError("fresh_process_required")
        sys.path.insert(0, str(ROOT))
        cls.addClassCleanup(lambda: sys.path.remove(str(ROOT)))
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("network_forbidden"))
            guard.start()
            cls.addClassCleanup(guard.stop)
        cls.gateway_module = importlib.import_module("finance_gateway")
        cls.metric_module = importlib.import_module("financial_metrics")
        cls.tools_module = importlib.import_module("agent_runtime.bank_tools")
        cls.claim_module = importlib.import_module("agent_runtime.metric_claims")
        cls.boundary_module = importlib.import_module("agent_runtime.data_boundary")
        cls.BankError = importlib.import_module("simulated_bank").BankError
        cls.privacy_revision = staticmethod(importlib.import_module("user_memory").privacy_revision)
        if manifest(cls.before) != cls.before:
            raise RuntimeError("candidate_changed_during_import")
        cls.observations = []

    @classmethod
    def tearDownClass(cls):
        cls.after = manifest(cls.before)
        print(json.dumps({"suite_id": cls.oracle["suite_id"], "oracle_sha256": ORACLE_SHA256,
                          "source_sha256_before": cls.before, "source_sha256_after": cls.after,
                          "sources_unchanged": cls.before == cls.after,
                          "authoring_snapshot_matches": cls.before == cls.oracle["authoring_source_sha256"],
                          "snapshot_verification_only": True, "cloud_model_calls": 0,
                          "independent_review_required": True, "observations": cls.observations},
                         ensure_ascii=False, sort_keys=True))
        if cls.before != cls.after or sha(ORACLE) != ORACLE_SHA256:
            raise RuntimeError("candidate_or_oracle_changed_during_test")

    def setUp(self):
        from cryptography.fernet import Fernet
        temporary = tempfile.TemporaryDirectory(prefix="navren-formal-metrics-")
        self.addCleanup(temporary.cleanup)
        self.temp_root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            "NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(),
            "NAVREN_KNOWLEDGE_DB": str(self.temp_root / "knowledge.sqlite3"),
            "NAVREN_MARKET_DB": str(self.temp_root / "market.sqlite3"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.g = self.gateway_module.FinanceGateway(self.temp_root / "bank.sqlite3")
        self.assertIs(type(self.g.financial_metrics), self.metric_module.FinancialMetrics)
        # 正式默认会话仍为原 demo fixture；没有初始化四画像或现役来源。
        self.w, self.token = self.g.create_session()
        self.assertFalse(self.g.demo_accounts.is_demo(self.w))
        self.t = self.tools_module.BankTools(self.g, self.w, "formal-integration-" + self.id().rsplit(".", 1)[-1])
        for path in (self.g.bank.database, self.g.personal_data.database, self.g.knowledge_provider.path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.temp_root), str(path))
        self.main = "acct-" + self.w[:12] + "-main"
        self.savings = "acct-" + self.w[:12] + "-savings"

    def error(self, expected, call, error_type=None):
        with self.assertRaises(error_type or self.BankError) as caught:
            call()
        self.assertEqual(caught.exception.code, expected)
        self.observations.append({"test": self.id().rsplit(".", 1)[-1], "rejected_code": caught.exception.code})

    def complete(self, output):
        self.assertTrue(output["metrics"])
        required = set(self.oracle["metric_fields"])
        for record in output["metrics"]:
            self.assertEqual(set(record["metric"]), required)
            self.assertEqual(record["metric_id"], record["metric"]["metric_id"])
            self.assertEqual(record, self.t.metrics.lookup(record["metric_id"]))
            self.assertTrue(record["metric"]["input_refs"])
            self.assertTrue(record["metric"]["source_versions"])
            self.assertIs(record["metric"]["value_minor"] is None or type(record["metric"]["value_minor"]) is int, True)
        self.g.financial_metrics.validate(self.w, self.t.metric_batches[-1])

    def record(self, output, name, subject=None):
        rows = [r for r in output["metrics"] if r["metric"]["metric_name"] == name
                and (subject is None or r["metric"]["subject_id"] == subject)]
        self.assertEqual(len(rows), 1, (name, subject))
        return rows[0]

    def hypothesis(self):
        output = self.t.invoke("money_calculation", self.cases["F02"]["inputs"])
        self.complete(output)
        return output, output["metrics"][0]

    def final(self, record, text="假设算式结果为130元。", **overrides):
        claim = {**self.t.metrics.binding(record["metric_id"]), "text": text, **overrides}
        return {"text": text, "claims": [], "amount_claims": [claim]}

    def cash(self):
        with closing(self.g.bank.connect()) as db:
            return [(r["id"], r["balance_minor"]) for r in db.execute(
                "SELECT id,balance_minor FROM accounts WHERE workspace_id=? ORDER BY id", (self.w,))]

    def operation_count(self):
        with closing(self.g.bank.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM operations WHERE workspace_id=?", (self.w,)).fetchone()[0]

    def change_source(self):
        # 只改变本题临时真实银行源：保留金额相同，来源时间/版本变更也必须失效。
        with self.g.bank.transaction() as db:
            db.execute("UPDATE workspaces SET as_of='2026-10-04',version=version+1 WHERE id=?", (self.w,))

    def prepare_args(self):
        self.t.invoke("account_summary", {})
        payments = self.t.invoke("business_records", {"kind": "payments"})
        beneficiary = next(r["id"] for r in payments["data"]["beneficiaries"]
                           if r["status"] == "active" and r["verified"])
        return {"action": "transfer", "account_id": self.main,
                "beneficiary_id": beneficiary, "amount_minor": 10000, "reference": "独立合成验收"}

    def guarded(self, batch):
        with closing(self.g.bank.connect()) as db:
            privacy = self.privacy_revision(db, self.w)
        return self.g.response_guard(self.w, privacy, self.g.personal_data.revision(self.w), metric_batches=[batch])

    def test_real_cash_batch_is_complete_and_account_specific(self):
        expected = self.cases["F01"]["expected"]
        output = self.t.invoke("account_summary", {})
        self.complete(output)
        for name, subject, value in (
                ("settled_cash_balance", self.main, expected["main_cash_minor"]),
                ("settled_cash_balance", self.savings, expected["savings_cash_minor"]),
                ("settled_cash_total", self.w, expected["cash_total_minor"]),
                ("holding_principal", self.w, expected["holding_principal_minor"])):
            record = self.record(output, name, subject)
            self.assertEqual(record["metric"]["value_minor"], value)
            self.assertEqual(record["metric"]["as_of"], expected["as_of"])
            self.assertEqual(record["kind"], expected["kind"])
        self.assertTrue(self.t.metric_batches[-1]["no_financial_side_effect"])

    def test_real_hypothesis_projection_preserves_complete_local_binding(self):
        output, record = self.hypothesis()
        expected = self.cases["F02"]["expected"]
        for key in ("basis", "formula_id", "assumed_inputs"):
            self.assertEqual(record["metric"][key], expected[key])
        self.assertEqual(record["metric"]["value_minor"], expected["result_minor"])
        self.assertEqual(record["kind"], expected["kind"])
        local = copy.deepcopy(self.t.metrics.lookup(record["metric_id"]))
        boundary = self.boundary_module.CloudDataBoundary(task="general", identities=(self.w, self.main))
        projected = boundary.tool_result("money_calculation", output)
        self.assertEqual(projected["metrics_coverage"]["status"], "complete")
        self.assertEqual(projected["metrics_coverage"]["projected_count"], 1)
        self.assertEqual(projected["metrics_coverage"]["withheld_count"], 0)
        # 完整投影目前可以逐字段往返；后续 compact 通道仍须保留本地原件并精确回查。
        restored = boundary.restore_output(projected["metrics"])
        self.assertEqual(restored, output["metrics"])
        self.assertEqual(self.t.metrics.lookup(record["metric_id"]), local)
        # 当前模型通道仅压缩 source map。私有来源集由同轮本地缓存发行，
        # 模型不能改写 catalog、对象、金额、日期或条件类型来借用来源引用。
        self.assertTrue(boundary.supports_compact_metric_sources)
        compact = boundary.model_tool_result("money_calculation", output, compact_metric_sources=True)
        compact_metric = compact["metrics"][0]["metric"]
        self.assertNotIn("source_versions", compact_metric)
        ref = compact_metric["source_version_ref"]
        self.assertIn(ref, compact["source_version_sets"])
        expanded = boundary._expand_metric_signature(compact_metric)
        self.assertEqual(boundary.restore_output(expanded), local["metric"])
        flat = {key: copy.deepcopy(compact_metric[key]) for key in self.claim_module.REF_FIELDS
                if key != "source_versions"}
        compact_claim = {**flat, "source_version_ref": ref, "claim_kind": "hypothesis", "text": "假设算式结果为130元。"}
        compact_final = {"text": compact_claim["text"], "claims": [], "amount_claims": [compact_claim]}
        expanded_final = boundary.restore_arguments("answer_reviewed", compact_final)
        self.assertEqual(expanded_final, self.final(record))
        self.assertEqual(self.t.amount_contract(expanded_final)["status"], "validated")
        for field, value in (("value_minor", 13001), ("as_of", "2026-10-04"),
                             ("source_version_ref", "vset_ffffffff"), ("claim_kind", "recorded_fact")):
            with self.subTest(compact_forgery=field):
                forged = copy.deepcopy(compact_final)
                forged["amount_claims"][0][field] = value
                self.error("BOUNDARY_UNKNOWN_ALIAS", lambda: boundary.restore_arguments("answer_reviewed", forged),
                           self.boundary_module.BoundaryError)
        catalog_injection = {**compact_final, "source_version_sets": copy.deepcopy(compact["source_version_sets"])}
        self.error("BOUNDARY_UNKNOWN_STRUCTURE", lambda: boundary.restore_arguments("answer_reviewed", catalog_injection),
                   self.boundary_module.BoundaryError)
        self.assertEqual(self.t.metrics.lookup(record["metric_id"]), local)
        result = self.t.amount_contract(self.final(record))
        self.assertEqual(result["status"], "validated")
        self.assertTrue(result["independent_review_required"])
        self.assertEqual(result["amount_claims"][0]["metric"], local["metric"])

    def test_service_rules_are_conditional_not_current_cash(self):
        expected = self.cases["F03"]["expected"]
        before = self.cash()
        output = self.t.invoke("service_offer_rules", {})
        self.complete(output)
        self.assertEqual(len(output["metrics"]), 6)
        for subject, terms in expected["offers"].items():
            for name in ("monthly_value", "max_cost", "minimum_monthly_salary"):
                record = self.record(output, name, subject)
                self.assertEqual(record["kind"], expected["kind"])
                for field in ("basis", "data_scope"):
                    self.assertEqual(record["metric"][field], expected[field])
                self.assertEqual(record["metric"]["value_minor"], terms[name])
                self.assertEqual(record["metric"]["period_start"], terms["period_start"])
                self.assertEqual(record["metric"]["period_end"], terms["period_end"])
                self.assertEqual(record["metric"]["assumed_inputs"], [{"type": "conditional_service_terms",
                    "status": "approved_rule_conditional", "duration_months": terms["duration_months"]}])
        self.assertEqual(self.cash(), before)

    def test_base_rejects_tool_amount_and_type_forgery(self):
        args = self.cases["F02"]["inputs"]
        for value in (13001, 13000.0, True):
            with self.subTest(value=value):
                self.error("METRIC_RESULT_MISMATCH", lambda: self.g.financial_metrics.metrics_for_tool(
                    self.w, "money_calculation", args, {"currency": "CNY", "result_minor": value,
                    "source": "user_input_scenario"}))

    def test_signed_real_batch_rejects_mutation_and_other_workspace(self):
        self.hypothesis()
        batch = self.t.metric_batches[-1]
        tampered = copy.deepcopy(batch)
        tampered["metrics"][0]["value_minor"] += 1
        self.error("METRIC_TAMPERED", lambda: self.g.financial_metrics.validate(self.w, tampered))
        other, _ = self.g.create_session()
        self.error("METRIC_SUBJECT_MISMATCH", lambda: self.g.financial_metrics.validate(other, batch))

    def test_agent_rejects_same_amount_wrong_object_period_currency_basis(self):
        _, record = self.hypothesis()
        changes = {"subject_id": self.main, "as_of": "2026-10-04", "period_start": "2026-10-01",
                   "period_end": "2026-10-31", "currency": "USD", "basis": "ledger_cash_balance",
                   "data_scope": "simulation_bank_ledger", "formula_id": "tools.account_balance/1"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.error("METRIC_CLAIM_MISMATCH", lambda: self.t.amount_contract(self.final(record, **{field: value})))

    def test_agent_rejects_wrong_amount_and_condition_relabel(self):
        _, record = self.hypothesis()
        self.error("METRIC_LITERAL_MISMATCH", lambda: self.t.amount_contract(self.final(record, "假设结果为131元。")))
        self.error("METRIC_KIND_MISMATCH", lambda: self.t.amount_contract(self.final(record, claim_kind="recorded_fact")))
        offer = self.t.invoke("service_offer_rules", {})
        fee = self.record(offer, "monthly_value", "SYNTH-E-STATEMENT-FEE-90D")
        self.error("METRIC_KIND_MISMATCH", lambda: self.t.amount_contract(self.final(fee, "条件服务费为5元。", claim_kind="recorded_fact")))

    def test_real_batches_cannot_merge_changed_source_versions(self):
        self.hypothesis()
        first = copy.deepcopy(self.t.metric_batches[-1])
        self.change_source()
        new_tools = self.tools_module.BankTools(self.g, self.w, "formal-source-changed")
        output = new_tools.invoke("money_calculation", self.cases["F02"]["inputs"])
        second = new_tools.metric_batches[-1]
        self.g.financial_metrics.validate(self.w, second)
        self.assertEqual(first["metrics"][0]["value_minor"], output["metrics"][0]["metric"]["value_minor"])
        self.error("METRIC_SOURCE_VERSION_CONFLICT", lambda: self.claim_module.merge_source_versions(
            first["metrics"] + second["metrics"]), self.claim_module.MetricClaimError)

    def test_source_change_before_prepare_rejects_without_new_operation(self):
        args = self.prepare_args()
        count, cash = self.operation_count(), self.cash()
        self.change_source()
        self.error("STALE_EVIDENCE", lambda: self.t.invoke("prepare_operation", args))
        self.assertEqual(self.operation_count(), count)
        self.assertEqual(self.cash(), cash)

    def test_source_change_before_final_answer_and_locked_release_rejects(self):
        _, record = self.hypothesis()
        batch = copy.deepcopy(self.t.metric_batches[-1])
        self.assertEqual(self.t.amount_contract(self.final(record))["status"], "validated")
        with self.guarded(batch) as db:
            self.assertIsNotNone(db.execute("SELECT 1").fetchone())
        self.change_source()
        self.error("STALE_EVIDENCE", lambda: self.t.amount_contract(self.final(record)))
        def publish():
            with self.guarded(batch):
                self.fail("changed formal source must not publish")
        self.error("METRIC_VERSION_CONFLICT", publish)

    def test_cashflow_prepare_keeps_existing_excluded_pending_source_valid(self):
        # 沿用正常HTTP请求的受信Observer，把本轮原批次交给实际prepare转换。
        from base_runtime.evidence import MetricEvidenceObserver
        service = self.g.financial_metrics
        observer = MetricEvidenceObserver(service)
        self.g.financial_metrics = observer
        args = self.prepare_args()
        cash = self.cash()
        cashflow = self.t.invoke("cashflow", {"days": self.cases["F11"]["inputs"]["days"]})
        cashflow_batch = copy.deepcopy(self.t.metric_batches[-1])
        self.complete(cashflow)
        self.assertTrue(any("pending_operations_not_counted" in r["metric"]["excluded_items"] for r in cashflow["metrics"]))
        output = self.t.invoke("prepare_operation", args)
        self.complete(output)
        self.assertEqual(output["data"]["operation"]["state"], "pending_confirmation")
        self.assertEqual(self.record(output, "pending_operation_amount")["kind"], "planning_scenario")
        self.t.fresh()
        self.assertEqual(self.cash(), cash)
        for batch in self.t.metric_batches:
            self.g.financial_metrics.validate(self.w, batch)
        self.assertEqual(observer.batches, self.t.metric_batches)
        transition = service._cashflow_prepare_transitions[(self.w, cashflow_batch["snapshot_id"])]
        self.assertEqual(transition["operation_id"], output["data"]["operation"]["id"])
        self.observations.append({"test": self.id().rsplit(".", 1)[-1],
            "request_observer": type(observer).__name__, "underlying_service": type(service).__name__,
            "original_cashflow_snapshot_id": cashflow_batch["snapshot_id"],
            "actual_prepare_transition": copy.deepcopy(transition),
            "observer_batch_count": len(observer.batches), "bank_tools_batch_count": len(self.t.metric_batches),
            "operation_state": output["data"]["operation"]["state"],
            "cash_before": cash, "cash_after": self.cash(), "cash_unchanged": self.cash() == cash})

    def test_locked_response_guard_accepts_real_batch_then_rejects_changed_pending_state(self):
        args = self.prepare_args()
        cash = self.cash()
        output = self.t.invoke("prepare_operation", args)
        self.complete(output)
        expected = self.cases["F12"]["expected"]
        pending = self.record(output, "pending_operation_amount")
        projected = self.record(output, "after_confirmation_balance", self.main)
        self.assertEqual(projected["metric"]["value_minor"], expected["after_confirmation_balance_minor"])
        self.assertEqual(projected["kind"], expected["pending_operation_kind"])
        self.assertEqual(projected["metric"]["assumed_inputs"], [{"type": "pending_user_confirmation",
            "operation_id": pending["metric"]["subject_id"]}])
        batch = copy.deepcopy(self.t.metric_batches[-1])
        with self.guarded(batch):
            pass
        with self.g.bank.transaction() as db:
            db.execute("UPDATE operations SET state='expired' WHERE workspace_id=? AND id=?",
                       (self.w, pending["metric"]["subject_id"]))
        self.error("STALE_OPERATION_METRICS", lambda: self.g.financial_metrics.validate(self.w, batch))
        def publish():
            with self.guarded(batch):
                self.fail("expired pending source must not publish")
        self.error("STALE_OPERATION_METRICS", publish)
        self.assertEqual(self.cash(), cash)


if __name__ == "__main__":
    unittest.main(verbosity=2)
