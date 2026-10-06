"""46项原oracle的12项独立接线subset：真实Gateway/HMAC/Observer/Registry。

受控审计夹具先将原始记录与独立目录存入临时银行库；替身只提供受信
source读取接口，不替换算法、发行、版本回查或预算解析。无云/端口。
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
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
ORACLE_PATH = ROOT / "evaluation/window_integration/finance_adapter_oracles.json"
ORACLE_SHA256 = "04dde7576f4dec85ddf3ac2e0ecbc0940de16a3a52ef32c7bd79625ab513bb14"
CONTRACT_PATH = ROOT / "outputs/window-progress/finance/implementation/adapter-contract.md"
RISK_SOURCE_CONTRACT_PATH = ROOT / "evaluation/window_integration/finance_adapter_capability_design.json"
RISK_SOURCE_CONTRACT_SHA256 = "1855e9b811b8bdbb0499cb737def56763f8f92ba5e4db084db3791606ce05915"
OUTPUT_ROOT = ROOT / "outputs/window-progress/integration/finance-adapter-handoff"
ORACLE = json.loads(ORACLE_PATH.read_text())
CASES = {case["case_id"]: case for case in ORACLE["cases"]}
SELECTED = ("A01_book_balance", "A02_cash_classification", "A11_previous_period_refund",
    "B01_equity_scope", "B03_capitalized_buy_fee", "B08_twr_external_flow",
    "C01_cash_investment_fee", "C02_no_confirmed_goals", "C03_cash_only_no_risk",
    "C04_mixed_missing_investment_dependencies", "G02_source_version_drift", "G04_independent_scope_inventory")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_manifest():
    paths = {path for path in ROOT.glob("*.py") if path.is_file()}
    for folder in ("agent_runtime", "base_runtime", "simulated_bank", "financial_methods", "local_knowledge"):
        paths.update((ROOT / folder).rglob("*.py"))
    paths.update((ORACLE_PATH, CONTRACT_PATH, RISK_SOURCE_CONTRACT_PATH, Path(__file__).resolve()))
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}


def resolved_input(case_id):
    case = CASES[case_id]
    value = resolved_input(case["base_case"]) if "base_case" in case else copy.deepcopy(case["inputs"])
    value.update(copy.deepcopy(case.get("mutation", {})))
    return value


class SqliteAuditedSource:
    """维护方显式注入的受控审计source；事实与完整目录分表保存。"""
    def __init__(self, bank, workspace):
        self.bank, self.workspace, self.reads = bank, workspace, []
        with bank.transaction() as db:
            for table in ("integration_method_metadata", "integration_method_raw_records", "integration_method_catalog"):
                db.execute("CREATE TABLE " + table + "(workspace_id TEXT NOT NULL,name TEXT NOT NULL,"
                           "payload TEXT NOT NULL,PRIMARY KEY(workspace_id,name))")

    def install(self, name, metadata, records, catalog):
        with self.bank.transaction() as db:
            for table, value in (("integration_method_metadata", metadata),
                                 ("integration_method_raw_records", records),
                                 ("integration_method_catalog", catalog)):
                db.execute("INSERT INTO " + table + " VALUES(?,?,?)", (self.workspace, name,
                    json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def snapshot(self, workspace, name, arguments, *, db):
        if workspace != self.workspace or db is None:
            raise AssertionError("audited_source_requires_current_workspace_and_real_db")
        values = []
        for table in ("integration_method_metadata", "integration_method_raw_records", "integration_method_catalog"):
            row = db.execute("SELECT payload FROM " + table + " WHERE workspace_id=? AND name=?",
                             (workspace, name)).fetchone()
            if row is None:
                raise AssertionError("audited_source_fixture_not_installed")
            values.append(json.loads(row["payload"]))
        metadata, records, catalog = values
        self.reads.append({"name": name, "in_transaction": db.in_transaction,
            "records_sha256": digest(records), "catalog_sha256": digest(catalog),
            "metadata_sha256": digest(metadata)})
        return {**metadata, "records": records, "catalog": catalog}

    def change_fixture_version(self, name, version):
        with self.bank.transaction() as db:
            row = db.execute("SELECT payload FROM integration_method_metadata WHERE workspace_id=? AND name=?",
                             (self.workspace, name)).fetchone()
            value = json.loads(row["payload"])
            value["source_versions"]["fixture"] = version
            db.execute("UPDATE integration_method_metadata SET payload=? WHERE workspace_id=? AND name=?",
                (json.dumps(value, ensure_ascii=False), self.workspace, name))


class FinanceAdapterHandoffContracts(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if sha(ORACLE_PATH) != ORACLE_SHA256:
            raise RuntimeError("received_finance_adapter_oracle_changed")
        if sha(RISK_SOURCE_CONTRACT_PATH) != RISK_SOURCE_CONTRACT_SHA256:
            raise RuntimeError("frozen_synthetic_risk_source_contract_changed")
        if Path(sys.executable).absolute().parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required")
        destination = os.environ.get("NAVREN_FINANCE_HANDOFF_OUTPUT")
        cls.output = Path(destination).resolve() if destination else OUTPUT_ROOT / (
            "run-" + str(time.time_ns()) + "-" + uuid.uuid4().hex[:8])
        if not cls.output.is_relative_to(OUTPUT_ROOT.resolve()):
            raise RuntimeError("isolated_output_scope_required")
        cls.output.mkdir(parents=True, exist_ok=True)
        if (cls.output / "suite-evidence.json").exists():
            raise RuntimeError("new_run_directory_required")
        cls.before, cls.observations = source_manifest(), []
        (cls.output / "source-before.json").write_text(json.dumps(cls.before, indent=2) + "\n")
        sys.path.insert(0, str(ROOT))
        cls.addClassCleanup(lambda: sys.path.remove(str(ROOT)))
        cls.gateway = importlib.import_module("finance_gateway")
        cls.metrics_module = importlib.import_module("financial_metrics")
        cls.evidence_module = importlib.import_module("base_runtime.evidence")
        cls.claims_module = importlib.import_module("agent_runtime.metric_claims")
        cls.BankError = importlib.import_module("simulated_bank").BankError
        cls.privacy_revision = staticmethod(importlib.import_module("user_memory").privacy_revision)
        if source_manifest() != cls.before:
            raise RuntimeError("candidate_changed_during_import")

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        report = {"suite_id": "navren.integration.finance-adapter-handoff/1", "oracle_sha256": ORACLE_SHA256,
            "scope": "controlled_audited_sqlite_source_fixture_real_base_adapter_chain",
            "sources_unchanged": cls.before == after, "source_before": cls.before, "source_after": after,
            "selected_cases": list(SELECTED), "case_disposition": [
                {"case": case, "status": "selected" if case in SELECTED else "not_covered",
                 "reason": "正式接线subset" if case in SELECTED else "此专项没有执行该原oracle，不能由近似案例升级"}
                for case in CASES], "cloud_model_calls": 0,
            "not_verified": ["真实客户确认", "正式真实用户风险资格", "当前市场/产品在售原件",
                             "四画像全部A/B/C接线", "Agent真实模型与GLM语义", "46项完整通过"],
            "observations": cls.observations}
        (cls.output / "suite-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("WINDOW_FINANCE_ADAPTER_HANDOFF_SUMMARY " + json.dumps({"oracle_sha256": ORACLE_SHA256,
            "sources_unchanged": cls.before == after, "observations": len(cls.observations),
            "output": str(cls.output), "cloud_model_calls": 0}, ensure_ascii=False), flush=True)
        if cls.before != after or sha(ORACLE_PATH) != ORACLE_SHA256:
            raise RuntimeError("candidate_or_received_oracle_changed_during_test")

    def setUp(self):
        from cryptography.fernet import Fernet
        temporary = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.output)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        env = patch.dict(os.environ, {"NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(),
            "NAVREN_SIM_DB": str(self.directory / "bank.sqlite3"),
            "NAVREN_KNOWLEDGE_DB": str(self.directory / "knowledge.sqlite3"),
            "NAVREN_MARKET_DB": str(self.directory / "market.sqlite3"),
            "NAVREN_FUND_CACHE": str(self.directory / "funds"),
            "NAVREN_MODEL_CALL_LOG": str(self.directory / "provider.jsonl"),
            "NAVREN_ALLOW_CLOUD_ASR": "0"})
        env.start()
        self.addCleanup(env.stop)
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("network_forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.g = self.gateway.FinanceGateway(self.directory / "bank.sqlite3")
        self.g.service_negotiations.rules_path = self.directory / "service-rules.json"
        self.g.service_negotiations.rules_path.write_bytes((ROOT / "simulated_bank/service_benefit_rules.json").read_bytes())
        self.w, self.token = self.g.create_session()
        self.assertFalse(self.g.demo_accounts.is_demo(self.w))
        self.service = self.g.financial_metrics
        self.assertIs(type(self.service), self.metrics_module.FinancialMetrics)
        self.observer = self.evidence_module.MetricEvidenceObserver(self.service)
        self.g.financial_metrics = self.observer
        self.provider = SqliteAuditedSource(self.g.bank, self.w)
        self.g.finance_method_sources = self.provider
        self.registry = self.claims_module.MetricRegistry()
        self.source_packages = {}

    def mapped(self, value, scope="independent_synthetic"):
        if isinstance(value, dict):
            return {key: (self.w if key == "subject_id" and item == "fixture-A" else
                scope if key == "data_scope" and item == "independent_synthetic" else self.mapped(item, scope))
                for key, item in value.items()}
        if isinstance(value, list):
            return [self.mapped(item, scope) for item in value]
        return value

    def context(self, case_id, scope="independent_synthetic"):
        data = resolved_input(case_id)
        context = self.mapped(copy.deepcopy(ORACLE["contexts"][data["context_ref"]]), scope)
        context["source_versions"] = dict(context["source_versions"])
        return data, context

    def install(self, name, context, records, catalog, provenance):
        context = copy.deepcopy(context)
        context["provenance"] = provenance
        refs = list(dict.fromkeys([*context["input_refs"], *(ref for values in provenance.values() for ref in values)]))
        context["input_refs"] = refs
        for ref in refs:
            context["source_versions"].setdefault(ref, "v1")
        package = {**context, "records": records, "catalog": catalog}
        self.source_packages[name] = copy.deepcopy(package)
        self.provider.install(name, context, records, catalog)
        (self.directory / (name + "-raw-source.json")).write_text(
            json.dumps(package, ensure_ascii=False, indent=2) + "\n")

    def arguments(self, name, context):
        result = {"scope": self.w, "expected_versions": {"fixture": "v1"}}
        if name == "accounting_report":
            result["source"] = "simulation"
        if name == "returns_report":
            result.update(portfolio_ref="audit-portfolio/current", accounting_basis="equity")
        result.update({key: context[key] for key in ("as_of", "period_start", "period_end")})
        return result

    def bank_state(self):
        with closing(self.g.bank.connect()) as db:
            tables = ("workspaces", "accounts", "profiles", "holdings", "orders", "operations", "transactions",
                      "loan_installments", "credit_statements", "subscriptions", "mandates", "scheduled_payments", "fund_holds")
            return {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE " +
                ("id" if table == "workspaces" else "workspace_id") + "=? ORDER BY rowid", (self.w,))] for table in tables}

    def issue(self, case_id, name, arguments):
        before = self.bank_state()
        report = self.g.financial_method_report(self.w, name, arguments)
        self.assertEqual(self.bank_state(), before, "只读方法不得改变现金、持仓、账单或操作")
        self.assertIs(report["no_financial_side_effect"], True)
        self.assertIs(report["execution_authority"], False)
        batch = report["metric_batch"]
        self.assertEqual(batch["tool_name"], name)
        self.assertEqual(batch["workspace_id"], self.w)
        self.assertEqual(len(batch["service_signature"]), 64)
        self.assertEqual(batch, self.observer.batches[-1])
        self.assertEqual(len(self.observer.batches), 1)
        self.service.validate(self.w, batch)
        self.assertEqual(report["metrics"], batch["metrics"])
        for metric in batch["metrics"]:
            self.assertEqual(set(metric), self.claims_module.METRIC_FIELDS)
            self.assertEqual(metric["unit"], "minor_currency")
            self.registry.register(metric, kind=batch["metric_kinds"][metric["metric_id"]],
                                   evidence_id="audit-evidence/" + case_id)
            self.assertEqual(self.registry.lookup(metric["metric_id"])["metric"], metric)
        self.assertEqual(len(self.registry.metric_ids), len(batch["metrics"]))
        self.assertTrue(self.provider.reads)
        self.assertTrue(any(row["in_transaction"] for row in self.provider.reads))
        forged = copy.deepcopy(batch)
        forged["metrics"][0]["currency"] = "USD"
        with self.assertRaises(self.BankError) as caught:
            self.service.validate(self.w, forged)
        self.assertEqual(caught.exception.code, "METRIC_TAMPERED")
        with closing(self.g.bank.connect()) as db:
            privacy = self.privacy_revision(db, self.w)
        with self.g.response_guard(self.w, privacy, self.g.personal_data.revision(self.w),
                metric_batches=self.observer.batches):
            self.assertIs(self.g.assert_publication_current(), True)
        self.last_report, self.last_arguments = report, arguments
        return report

    def evidence(self, case_id, **values):
        report = self.last_report
        name = report["metric_batch"]["tool_name"]
        row = {"case": case_id, "scope": "controlled_audited_sqlite_source_fixture",
            "oracle_sha256": ORACLE_SHA256, "source_records_sha256": digest(self.source_packages[name]["records"]),
            "independent_catalog_sha256": digest(self.source_packages[name]["catalog"]),
            "gateway_entry": "FinanceGateway.financial_method_report", "hmac_issued": True,
            "observer_batches": len(self.observer.batches), "registry_count": len(self.registry.metric_ids),
            "no_financial_side_effect": True, "cloud_model_calls": 0, **values}
        self.observations.append(row)
        artifact = {**row, "request": self.last_arguments, "report": report,
                    "source_package": self.source_packages[name], "source_reads": self.provider.reads}
        (self.output / (case_id + "-evidence.json")).write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        print("WINDOW_FINANCE_ADAPTER_HANDOFF_EVIDENCE " + json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)

    def assert_values(self, report, expected):
        metrics = {metric["metric_name"]: metric for metric in report["metrics"]}
        for name, value in expected.items():
            self.assertIn(name, metrics)
            self.assertEqual(metrics[name]["value_minor"], value)
            self.assertIs(type(metrics[name]["value_minor"]), type(value))
        return metrics

    def accounting_fixture(self, case_id):
        data, context = self.context(case_id)
        records = {}
        catalog = {"complete": True, "coverage_ref": "audit:independent-catalog"}
        provenance = {"coverage": ["audit:independent-catalog"]}
        if context["as_of"] is not None:
            records["components"] = self.mapped(data["components"])
            catalog["expected_object_ids"] = copy.deepcopy(data["expected_object_ids"])
            provenance["components"] = [row["evidence_ref"] for row in records["components"]]
        else:
            records = {key: self.mapped(data[key]) for key in ("opening_cash_minor", "closing_cash_minor", "movements")}
            catalog.update(expected_movement_ids=(["borrow", "repay", "interest", "bill", "refund", "buy", "out", "in"]
                if case_id == "A02_cash_classification" else ["refund"]),
                expected_account_ids=["bank", "wallet"] if case_id == "A02_cash_classification" else ["bank"],
                expected_refund_ids=["refund"], refunds_complete=True)
            # 清单在安装时分表固定。生产读取缺行不能重建这里的预期清单。
            provenance.update(opening_cash=["audit:opening-cash"], closing_cash=["audit:closing-cash"],
                              movements=[row["evidence_ref"] for row in records["movements"]],
                              refund_originals=["audit:refund-original"], refund_cumulative=["audit:refund-cumulative"])
            if case_id == "A02_cash_classification":
                original = copy.deepcopy(next(row for row in records["movements"] if row["movement_id"] == "bill"))
                original["evidence_ref"] = "audit:refund-original"
            else:
                raw = data["refund_original"]
                original = {"movement_id": raw["id"], "subject_id": self.w, "account_id": "bank",
                    "currency": raw["currency"], "on": raw["on"], "kind": "expense",
                    "delta_minor": -raw["expense_minor"], "evidence_ref": "audit:refund-original", "source_version": "v1"}
            records.update(refund_originals=[original], refund_cumulative_minor={original["movement_id"]: 2000})
        self.install("accounting_report", context, records, catalog, provenance)
        return context

    def accounting_case(self, case_id):
        context = self.accounting_fixture(case_id)
        report = self.issue(case_id, "accounting_report", self.arguments("accounting_report", context))
        expected = CASES[case_id]["expected"]
        self.assertEqual(report["calculation_status"], expected["status"])
        self.assert_values(report, expected["metrics"])
        self.evidence(case_id, calculation_status=report["calculation_status"], expected_metrics=expected["metrics"])

    def returns_fixture(self, case_id):
        data, context = self.context(case_id)
        records = {"accounting_basis": "history_moving_average" if case_id == "B03_capitalized_buy_fee" else "equity",
                   "owner_contributions_minor": 0, "owner_withdrawals_minor": 0}
        instrument = {"instrument_id": "P/share", "share_class_id": "share", "channel_id": "audit-channel",
                      "currency": "CNY", "data_scope": "independent_synthetic"}
        catalog = {"expected_instrument_ids": ["P/share"], "instruments": [instrument],
                   "portfolio_ref": "audit-portfolio/current"}
        if case_id == "B01_equity_scope":
            records["snapshot"] = self.mapped(data["snapshot"])
            records["snapshot"]["holdings"][0].update(share_class_id="share", channel_id="audit-channel")
            records["prices"] = [{**instrument, "on": "2026-10-06", "unit": "major_currency_per_share", "unit_price": "1.00"}]
        elif case_id == "B03_capitalized_buy_fee":
            for key in ("opening", "events", "coverage"):
                records[key] = self.mapped(data[key])
            records["opening"].update(share_class_id="share", channel_id="audit-channel")
            catalog["expected_event_ids"] = copy.deepcopy(data["coverage"]["expected_event_ids"])
            records["prices"] = [{**instrument, "on": "2026-10-06", "unit": "major_currency_per_share", "unit_price": "1.00"},
                                 {**instrument, "on": "2026-11-06", "unit": "major_currency_per_share", "unit_price": "1.40"}]
        else:
            # B08只给收益边界。以下是受控审计源显式补充原件，不是实际客户资产。
            catalog.update(expected_instrument_ids=[], instruments=[], expected_boundary_dates=["2026-10-20"])
            for key, on, cash in (("opening", "2026-10-06", 10000), ("closing", "2026-11-06", 19475)):
                records[key] = {"subject_id": self.w, "currency": "CNY", "on": on, "holdings": [],
                    "expected_instrument_ids": [], "cash_minor": cash, "receivables_minor": 0,
                    "payables_minor": 0, "coverage_complete": True}
            records.update(subperiods=self.mapped(data["subperiods"]), boundaries=self.mapped(data["boundaries"]),
                           owner_contributions_minor=10000, owner_withdrawals_minor=0, prices=[])
        categories = ["snapshot"] if context["as_of"] is not None else ["opening", "closing"]
        categories += ["holdings", "prices", "cash", "receivables", "payables", "owner_flows", "coverage"]
        if case_id == "B03_capitalized_buy_fee":
            categories += ["costs", "events", "fees", "dividends", "splits"]
        if case_id == "B08_twr_external_flow":
            categories += ["boundaries"]
        provenance = {category: ["audit:returns:" + category] for category in categories}
        self.install("returns_report", context, records, catalog, provenance)
        arguments = self.arguments("returns_report", context)
        arguments["accounting_basis"] = records["accounting_basis"]
        return arguments

    def returns_case(self, case_id):
        report = self.issue(case_id, "returns_report", self.returns_fixture(case_id))
        expected = CASES[case_id]["expected"]
        if case_id == "B01_equity_scope":
            self.assert_values(report, {"portfolio_equity": expected["portfolio_equity"],
                                       "holdings_market_value": expected["holdings_market_value"]})
            self.assertEqual(report["calculation_status"], expected["calculation_status"])
        elif case_id == "B03_capitalized_buy_fee":
            self.assert_values(report, expected["metrics"])
            self.assertEqual(report["quantity_decimal"], expected["quantity_decimal"])
            self.assertEqual(report["calculation_status"], expected["calculation_status"])
            self.assertTrue(any(row.get("allocated_sale_cost_minor") == expected["sale_cost_minor"] for row in report["event_trace"]))
            closing = next(m for m in report["metrics"] if m["metric_name"] == "closing_equity")
            period = next(m for m in report["metrics"] if m["metric_name"] == "period_pnl")
            self.assertEqual(closing["as_of"], "2026-11-06")
            self.assertIsNone(period["as_of"])
            self.assertEqual((period["period_start"], period["period_end"]), ("2026-10-06", "2026-11-06"))
        else:
            ratios = [ratio for ratio in report["ratio_reports"] if ratio["value_decimal"] == expected["value_decimal"]]
            self.assertEqual(len(ratios), 1)
            self.assertEqual(ratios[0]["unit"], expected["unit"])
            self.assertEqual(ratios[0]["calculation_status"], expected["calculation_status"])
            self.assertNotIn("value_minor", ratios[0])
            self.assertTrue(all(self.registry.lookup(mid)["metric"]["unit"] == "minor_currency" for mid in self.registry.metric_ids))
            with self.assertRaises(self.claims_module.MetricClaimError):
                self.registry.register(ratios[0], kind="recorded_fact", evidence_id="audit-ratio-forbidden")
        self.evidence(case_id, calculation_status=report["calculation_status"],
                      ratio_reports=report["ratio_reports"], expected=expected)

    def issued_allocation_budget(self):
        upstream = resolved_input("C01_cash_investment_fee")["funds_upstream_literal"]
        with self.g.bank.transaction() as db:
            main = db.execute("SELECT id FROM accounts WHERE workspace_id=? AND id LIKE '%-main'", (self.w,)).fetchone()[0]
            db.execute("UPDATE accounts SET balance_minor=0,opening_balance_minor=0 WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE accounts SET balance_minor=?,opening_balance_minor=? WHERE workspace_id=? AND id=?",
                       (upstream["balance_minor"], upstream["balance_minor"], self.w, main))
            db.execute("UPDATE profiles SET reserve_minor=? WHERE workspace_id=?", (upstream["emergency_floor_minor"], self.w))
            db.execute("UPDATE loan_installments SET status='paid',paid_minor=principal_minor+interest_minor+fees_minor WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE credit_statements SET status='paid',paid_minor=total_due_minor WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE subscriptions SET merchant_status='cancelled' WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE mandates SET status='closed' WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE orders SET status='settled' WHERE workspace_id=? AND status='pending_settlement'", (self.w,))
            db.execute("DELETE FROM fund_holds WHERE workspace_id=?", (self.w,))
            template = db.execute("SELECT * FROM scheduled_payments WHERE workspace_id=? LIMIT 1", (self.w,)).fetchone()
            self.assertIsNotNone(template, "需要真实种子付款结构作原始确定义务")
            row = dict(template)
            db.execute("UPDATE scheduled_payments SET status='cancelled' WHERE workspace_id=?", (self.w,))
            row.update(id="audit-issued-budget-obligation", account_id=main, amount_minor=upstream["confirmed_outflow_minor"],
                       frequency="once", next_due_on="2026-10-20", status="active", end_on=None)
            columns = list(row)
            db.execute("INSERT INTO scheduled_payments(" + ",".join(columns) + ") VALUES(" +
                       ",".join("?" for _ in columns) + ")", [row[key] for key in columns])
            db.execute("UPDATE workspaces SET as_of='2026-10-06',version=version+1 WHERE id=?", (self.w,))
        issued = self.service.snapshot(self.w, 31)
        metric = issued["funding"]["metrics"]["current_budget"]
        self.assertEqual(metric["value_minor"], 500000)
        self.assertEqual(metric["basis"], "floor_surplus")
        self.assertEqual(metric["formula_id"], "funds.current_budget/1")
        self.assertEqual(self.service.resolve_budget_metric(self.w, metric["metric_id"]).to_dict(), metric)
        self.issued_budget, self.funding_source = metric, issued
        return metric

    def allocation_case(self, case_id):
        funds = self.issued_allocation_budget()
        data, context = self.context(case_id, funds["data_scope"])
        context.update({key: funds[key] for key in ("data_scope", "as_of", "period_start", "period_end")})
        records = {key: self.mapped(data[key], funds["data_scope"]) for key in ("goals", "products", "risk")}
        # 本受控SQLite source的目录/metadata已显式冻结每个adapter原件为v1。
        # 补精确原件版本，不改原oracle的金额/目标/资格/缺项预期；生产缺值不填。
        audited_versions = {"adapter:goal:cash": "v1", "adapter:goal:invest": "v1",
                            "adapter:terms": "v1", "adapter:formal-risk": "v1"}
        rows = [*records["goals"], *records["products"]]
        if records["risk"] is not None:
            rows.append(records["risk"])
        for row in rows:
            versions = dict(row["source_versions"])
            for ref in row["input_refs"]:
                self.assertIn(ref, audited_versions, "未声明原件不得补版本")
                versions[ref] = audited_versions[ref]
            row["source_versions"] = list(versions.items())
        provenance = {"coverage": ["audit:allocation-catalog"], "goals": ["adapter:goal:cash", "adapter:goal:invest"],
                      "products": ["adapter:terms"], "risk": ["adapter:formal-risk"]}
        for key in ("goals", "terms", "risk"):
            context["source_versions"][key] = "v1"
        catalog = {"constraints_ref": "audit:confirmed-goals", "product_evidence_refs": ["adapter:terms"]}
        if case_id == "C01_cash_investment_fee":
            # 独立合成评估/本人确认原件；不读取或升级种子profile的风险等级。
            design = json.loads(RISK_SOURCE_CONTRACT_PATH.read_text())
            source = copy.deepcopy(design["fixture_contract"]["risk_source_formal_fixture"])
            source.update(subject_id=self.w, data_scope=funds["data_scope"], confirmed_by_subject_id=self.w)
            risk = records["risk"]
            for key in ("subject_id", "currency", "data_scope", "level", "effective_on", "valid_through"):
                self.assertEqual(source[key], risk[key], "合成出处必须逐项绑定原C01风险原件")
            self.assertIs(risk["confirmed"], True)
            self.assertIn(source["assessment_ref"], risk["input_refs"])
            confirmation_ref = source["confirmation_ref"]
            risk["input_refs"] = list(dict.fromkeys([*risk["input_refs"], confirmation_ref]))
            exact_versions = {source["assessment_ref"]: source["source_version"],
                              confirmation_ref: source["confirmation_source_version"]}
            self.assertEqual(set(risk["input_refs"]), set(exact_versions))
            risk["source_versions"] = [[ref, exact_versions[ref]] for ref in risk["input_refs"]]
            # risk:v1不是一个实际资格原件，不能作为评估或确认引用保留。
            context["source_versions"].pop("risk", None)
            context["source_versions"].update(exact_versions)
            provenance["risk"] = list(risk["input_refs"])
            records["risk_source"] = source
            records["risk_assessment"] = {key: source[key] for key in (
                "assessment_ref", "source_version", "assessment_type", "assessment_status",
                "subject_id", "currency", "data_scope", "effective_on", "valid_through", "level")}
            records["risk_subject_confirmation"] = {key: source[key] for key in (
                "confirmation_ref", "confirmation_source_version", "confirmation_source",
                "confirmed_by_subject_id", "assessment_ref", "subject_id", "currency", "data_scope")}
            catalog.update(risk_source_refs=list(risk["input_refs"]),
                           risk_source_contract_sha256=RISK_SOURCE_CONTRACT_SHA256)
        self.install("allocation_preview", context, records,
                     catalog, provenance)
        arguments = {"scope": self.w, "budget_metric_id": funds["metric_id"], "constraints_ref": "audit:confirmed-goals",
                     "product_evidence_refs": ["adapter:terms"] if records["products"] else [],
                     "expected_versions": {"fixture": "v1"}}
        financial_state_before = self.bank_state() if case_id == "C01_cash_investment_fee" else None
        report = self.issue(case_id, "allocation_preview", arguments)
        self.assertEqual(report["funds"], funds)
        plans = {plan["plan_id"]: plan for plan in report["plans"]}
        expected = CASES[case_id]["expected"]
        for wanted in expected["plans"]:
            actual = plans[wanted["plan_id"]]
            for field in ("calculation_status", "feasibility", "execution_authority", "future_proceeds_minor",
                          "conservation_difference_minor", "cash_partition_difference_minor"):
                if field in wanted:
                    self.assertEqual(actual[field], wanted[field])
            for name, value in wanted["metrics"].items():
                self.assertEqual(actual["metrics"][name]["value_minor"], value)
                self.assertIs(type(actual["metrics"][name]["value_minor"]), type(value))
            for gap in wanted.get("coverage_gaps_contains", []):
                self.assertIn(gap, actual["coverage_gaps"])
        if "confirmed_uses_allocations" in expected:
            self.assertEqual(plans["confirmed_uses"]["allocations"], expected["confirmed_uses_allocations"])
        self.assertEqual(set(report["metric_batch"]["metric_kinds"].values()), {"planning_scenario"})
        source_evidence = {}
        if case_id == "C01_cash_investment_fee":
            self.assertEqual(report["risk_qualification"], {"classification": "formal_assessment",
                "reasons": [], "formal_qualification_verified": True})
            self.assertNotIn("risk", dict(records["risk"]["source_versions"]))
            source_evidence = {"risk_source_contract_sha256": RISK_SOURCE_CONTRACT_SHA256,
                "risk_source_scope": "separate_controlled_synthetic_assessment_and_subject_confirmation",
                "financial_state_before": financial_state_before, "financial_state_after": self.bank_state()}
        self.evidence(case_id, actual_issued_budget_metric_id=funds["metric_id"],
            budget_value_minor=funds["value_minor"], budget_formula=funds["formula_id"],
            plan_statuses={key: plan["calculation_status"] for key, plan in plans.items()},
            budget_created_from_literal_upstream_rows=True, **source_evidence)

    def test_G02_source_version_drift(self):
        context = self.accounting_fixture("A01_book_balance")
        arguments = self.arguments("accounting_report", context)
        report = self.issue("G02_source_version_drift", "accounting_report", arguments)
        self.provider.change_fixture_version("accounting_report", "v2")
        for call in (lambda: self.g.financial_method_report(self.w, "accounting_report", arguments),
                     lambda: self.service.validate(self.w, report["metric_batch"])):
            with self.assertRaises(self.BankError) as caught:
                call()
            self.assertEqual(caught.exception.code, CASES["G02_source_version_drift"]["expected"]["error_code"])
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.observer.batches), 1, "来源漂移不得发行新的旧partial批次")
        self.evidence("G02_source_version_drift", rejected_code="METRIC_VERSION_CONFLICT",
                      rejected_http_status=409, no_old_partial_published=True)

    def test_G04_independent_scope_inventory(self):
        case_id = "G04_independent_scope_inventory"
        data, context = self.context("A01_book_balance")
        controls = CASES[case_id]["inputs"]
        rows = self.mapped([row for row in data["components"] if row["object_id"] in controls["returned_components"]])
        catalog = {"complete": True, "coverage_ref": "audit:canonical-catalog",
                   "expected_object_ids": controls["canonical_inventory"]}
        self.install("accounting_report", context, {"components": rows}, catalog,
                     {"coverage": ["audit:canonical-catalog"], "components": [row["evidence_ref"] for row in rows]})
        report = self.issue(case_id, "accounting_report", self.arguments("accounting_report", context))
        self.assertEqual(report["calculation_status"], "unavailable")
        self.assertTrue(all(metric["value_minor"] is None for metric in report["metrics"]))
        self.assertTrue(report["reasons"])
        self.assertEqual(self.source_packages["accounting_report"]["catalog"]["expected_object_ids"], ["cash", "fund", "loan"])
        self.evidence(case_id, independent_inventory=["cash", "fund", "loan"], returned_objects=["cash", "fund"],
                      calculation_status="unavailable", coverage_upgrade_rejected=True, reasons=report["reasons"])


def add_case(case_id, helper):
    def test(self):
        return getattr(self, helper)(case_id)
    test.__name__ = "test_" + case_id
    setattr(FinanceAdapterHandoffContracts, test.__name__, test)


for case_id in SELECTED:
    if case_id.startswith("A"):
        add_case(case_id, "accounting_case")
    elif case_id.startswith("B"):
        add_case(case_id, "returns_case")
    elif case_id.startswith("C"):
        add_case(case_id, "allocation_case")
if set(unittest.defaultTestLoader.getTestCaseNames(FinanceAdapterHandoffContracts)) != {"test_" + c for c in SELECTED}:
    raise RuntimeError("finance_adapter_subset_collection_mismatch")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FinanceAdapterHandoffContracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    complete = (result.wasSuccessful() and result.testsRun == len(SELECTED) and not result.skipped
                and not result.expectedFailures and not result.unexpectedSuccesses)
    record = {"tests_run": result.testsRun, "expected_tests": len(SELECTED), "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses), "complete": complete,
        "planned_exit_code": 0 if complete else 1, "oracle_sha256": ORACLE_SHA256}
    if hasattr(FinanceAdapterHandoffContracts, "output"):
        (FinanceAdapterHandoffContracts.output / "actual-counts.json").write_text(json.dumps(record, indent=2) + "\n")
    print("WINDOW_FINANCE_ADAPTER_HANDOFF_COUNTS " + json.dumps(record), flush=True)
    sys.exit(record["planned_exit_code"])
