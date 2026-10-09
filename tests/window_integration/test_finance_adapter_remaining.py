"""46项冻结金融oracle剩余34项：29项接线，5项明确未覆盖。

原12项只复用非TestCase的SQLite受信源读取类，不继承或收集旧TestCase。
预期先冻结；读取原始SQLite与独立目录后走真实Gateway/HMAC/Observer/
Registry/最终发布门。零网络、零模型；单次结果只绑定实际来源SHA。
"""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
import hashlib
import importlib
import importlib.util
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
OUTPUT_ROOT = ROOT / "outputs/window-progress/integration/finance-adapter-remaining"
ORACLE_PATH = ROOT / "evaluation/window_integration/finance_adapter_oracles.json"
HELPER_PATH = ROOT / "tests/window_integration/test_finance_adapter_handoff.py"
DESIGN_PATH = ROOT / "evaluation/window_integration/finance_adapter_remaining_design.json"
MATRIX_PATH = ROOT / "evaluation/window_integration/finance_adapter_remaining_coverage_matrix.json"
ORACLE_SHA256 = "91cfb1dc3f3cfbe99d39785535bd2d1861b10273e1a337b006357f96b2ebee6b"
DESIGN_SHA256 = "af0f18f6c21e4d47271807af5e87905e2396f6a793a4e4229fc16f00cf44f06c"
MATRIX_SHA256 = "f4143d3069010bdaa4bed5be5f2963f268dd316c27520ff1da917e7de3ea5b67"
# 已批准C01夹具补齐评估/本人确认出处；原设计和SqliteAuditedSource保持冻结。
HELPER_INPUT_SHA256 = "d102610d1166f41a26f667562806ad5bb171d4e26ebda3cfd2df494adf471075"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


ORACLE = json.loads(ORACLE_PATH.read_text())
CASES = {row["case_id"]: row for row in ORACLE["cases"]}
DESIGN = json.loads(DESIGN_PATH.read_text())
MATRIX = {row["case_id"]: row for row in json.loads(MATRIX_PATH.read_text())["rows"]}
SELECTED = tuple(cid for cid, row in MATRIX.items() if row["new_test"] is not None)
UNCOVERED = tuple(cid for cid, row in MATRIX.items() if row["new_test"] is None)


def _path_item(value, key):
    if not isinstance(value, list):
        return value[key]
    if key.isdigit():
        return value[int(key)]
    matches = [row for row in value if any(row.get(field) == key
        for field in ("object_id", "movement_id", "event_id", "goal_id"))]
    if len(matches) != 1:
        raise ValueError("mutation_identity_not_unique:" + key)
    return matches[0]


def resolved_input(case_id):
    """递归base_case；点路径支持索引和对象ID，绝不将点路径当顶层字段。"""
    case = CASES[case_id]
    value = resolved_input(case["base_case"]) if case.get("base_case") else copy.deepcopy(case["inputs"])
    for path, replacement in case.get("mutation", {}).items():
        pieces, target = path.split("."), value
        if pieces[0] == "context":
            value.setdefault("context", copy.deepcopy(ORACLE["contexts"][value["context_ref"]]))
        if pieces[0] == "refund":
            target = _path_item(value["movements"], "refund")
            pieces = pieces[1:]
        for piece in pieces[:-1]:
            target = _path_item(target, piece)
        if isinstance(target, list):
            target[int(pieces[-1])] = copy.deepcopy(replacement)
        else:
            target[pieces[-1]] = copy.deepcopy(replacement)
    if "remove_component_id" in value:
        value["components"] = [row for row in value["components"]
            if row["object_id"] != value["remove_component_id"]]
    if "remove_movement_id" in value:
        value["movements"] = [row for row in value["movements"]
            if row["movement_id"] != value["remove_movement_id"]]
    return value


def source_manifest():
    paths = set(ROOT.glob("*.py"))
    for folder in ("agent_runtime", "base_runtime", "simulated_bank", "financial_methods", "local_knowledge"):
        paths.update((ROOT / folder).rglob("*.py"))
    paths.update((Path(__file__).resolve(), HELPER_PATH, ORACLE_PATH,
        DESIGN_PATH, MATRIX_PATH))
    for relative in DESIGN["input_sha256"]:
        paths.add(ROOT / relative)
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths) if path.is_file()}


class FinanceAdapterRemainingContracts(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        for path, expected in ((ORACLE_PATH, ORACLE_SHA256),
                (DESIGN_PATH, DESIGN_SHA256),
                (MATRIX_PATH, MATRIX_SHA256),
                (HELPER_PATH, HELPER_INPUT_SHA256)):
            if sha(path) != expected:
                raise RuntimeError("frozen_input_changed:" + str(path))
        if Path(sys.executable).absolute().parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required")
        if len(SELECTED) != 29 or len(UNCOVERED) != 5 or len(MATRIX) != 34:
            raise RuntimeError("remaining_case_count_changed")
        for cid, row in MATRIX.items():
            if (digest(CASES[cid]) != row["oracle_case_sha256"] or
                    resolved_input(cid) != row["resolved_input"] or
                    digest(resolved_input(cid)) != row["resolved_input_sha256"] or
                    CASES[cid]["expected"] != row["expected_literal"]):
                raise RuntimeError("frozen_literal_or_dotted_mutation_changed:" + cid)
        destination = os.environ.get("NAVREN_FINANCE_REMAINING_OUTPUT")
        cls.output = (Path(destination).resolve() if destination else OUTPUT_ROOT /
            ("run-" + str(time.time_ns()) + "-" + uuid.uuid4().hex[:8]))
        if not cls.output.is_relative_to(OUTPUT_ROOT.resolve()) or cls.output == OUTPUT_ROOT.resolve():
            raise RuntimeError("isolated_output_scope_required")
        if cls.output.exists():
            raise RuntimeError("new_run_directory_required")
        cls.output.mkdir(parents=True)
        cls.before, cls.observations = source_manifest(), []
        cls.mode = os.environ.get("NAVREN_FINANCE_REMAINING_MODE", "source_bound_local")
        write_json(cls.output / "source-before.json", cls.before)
        # 模块只有stdlib定义；不实例化、不暴露、不继承旧TestCase。
        spec = importlib.util.spec_from_file_location("_readonly_remaining_sqlite_helper", HELPER_PATH)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        cls.sqlite_source = helper.SqliteAuditedSource

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        observations = {row["case_id"]: row for row in cls.observations}
        report = {"schema": "navren.finance-adapter-remaining.run/1", "mode": cls.mode,
            "oracle_sha256": ORACLE_SHA256, "design_sha256": DESIGN_SHA256,
            "coverage_matrix_sha256": MATRIX_SHA256, "source_before": cls.before, "source_after": after,
            "sources_unchanged": after == cls.before, "expected_new_test_count": 29,
            "remaining_case_count": 34, "actual_observation_count": len(cls.observations),
            "case_disposition": [{"case_id": cid,
                "status": ("covered" if observations[cid]["assertions_passed"] and after == cls.before
                    else "uncovered") if cid in observations else row["coverage_status"],
                "execution_status": "observed" if cid in observations else "NOT_RUN",
                "reason": row["gap"], "source_bound_only": cid in observations}
                for cid, row in MATRIX.items()], "observations": cls.observations,
            "cloud_model_calls": 0, "formal_46_passed": False,
            "suite_execution_exit_code": None,
            "exit_code_contract": "Actual parent process exit must be recorded by external executor; this suite does not manufacture it",
            "limits": DESIGN["limitations"]}
        write_json(cls.output / "suite-evidence.json", report)
        print("WINDOW_FINANCE_REMAINING_SUMMARY " + json.dumps({
            "output": str(cls.output), "observations": len(cls.observations),
            "sources_unchanged": cls.before == after, "cloud_model_calls": 0,
            "formal_46_passed": False}, ensure_ascii=False), flush=True)
        if cls.before != after:
            raise RuntimeError("candidate_changed_during_test")

    def setUp(self):
        from cryptography.fernet import Fernet
        self.directory = Path(tempfile.mkdtemp(prefix="isolated-", dir=self.output)).resolve()
        paths = {"NAVREN_SIM_DB": self.directory / "bank.sqlite3",
            "NAVREN_KNOWLEDGE_DB": self.directory / "knowledge.sqlite3",
            "NAVREN_MARKET_DB": self.directory / "market.sqlite3",
            "NAVREN_FUND_CACHE": self.directory / "funds",
            "NAVREN_MODEL_CALL_LOG": self.directory / "provider.jsonl"}
        env = patch.dict(os.environ, {**{key: str(value) for key, value in paths.items()},
            "NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(), "NAVREN_ALLOW_CLOUD_ASR": "0"})
        env.start()
        self.addCleanup(env.stop)
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("network_forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        # 临时数据库/缓存/密钥和断网约束先于业务import。
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))
        self.gateway_module = importlib.import_module("finance_gateway")
        self.metrics_module = importlib.import_module("financial_metrics")
        self.evidence_module = importlib.import_module("base_runtime.evidence")
        self.claims_module = importlib.import_module("agent_runtime.metric_claims")
        self.BankError = importlib.import_module("simulated_bank").BankError
        self.privacy_revision = importlib.import_module("user_memory").privacy_revision
        self.addCleanup(self.purge_business_modules)
        self.g = self.gateway_module.FinanceGateway(self.directory / "bank.sqlite3")
        self.g.service_negotiations.rules_path = self.directory / "service-rules.json"
        self.g.service_negotiations.rules_path.write_bytes((ROOT / "simulated_bank/service_benefit_rules.json").read_bytes())
        self.w, _token = self.g.create_session()
        self.assertFalse(self.g.demo_accounts.is_demo(self.w))
        self.service = self.g.financial_metrics
        self.assertIs(type(self.service), self.metrics_module.FinancialMetrics)
        self.observer = self.evidence_module.MetricEvidenceObserver(self.service)
        self.g.financial_metrics = self.observer
        self.provider = self.sqlite_source(self.g.bank, self.w)
        self.g.finance_method_sources = self.provider
        self.registry = self.claims_module.MetricRegistry()
        self.paths = {key: str(value) for key, value in paths.items()}
        self.packages, self.stages, self.actual_reports, self.requests = {}, {}, [], []
        self.financial_checks = []
        self.funding = None
        self.private_key_origin = "temporary_generated_Fernet_key_not_logged"

    @staticmethod
    def purge_business_modules():
        for name, module in list(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if filename and Path(filename).resolve().is_relative_to(ROOT) and not name.startswith("tests"):
                relative = Path(filename).resolve().relative_to(ROOT)
                if relative.parts[0] != ".venv" and Path(filename).resolve() != Path(__file__).resolve():
                    sys.modules.pop(name, None)

    def bank_state(self):
        with closing(self.g.bank.connect()) as db:
            tables = ("workspaces", "accounts", "profiles", "holdings", "orders", "operations", "transactions",
                "loan_installments", "credit_statements", "subscriptions", "mandates", "scheduled_payments",
                "fund_holds", "wealth_goals", "navren_goal_drafts", "audit")
            return {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE " +
                ("id" if table == "workspaces" else "workspace_id") + "=? ORDER BY rowid", (self.w,))]
                for table in tables}

    def mapped(self, value, scope="independent_synthetic"):
        if isinstance(value, dict):
            return {key: (self.w if key == "subject_id" and item == "fixture-A" else
                scope if key == "data_scope" and item == "independent_synthetic" else self.mapped(item, scope))
                for key, item in value.items()}
        if isinstance(value, list):
            return [self.mapped(item, scope) for item in value]
        return value

    def context(self, cid, scope="independent_synthetic"):
        data = resolved_input(cid)
        context = self.mapped(copy.deepcopy(data.get("context", ORACLE["contexts"][data["context_ref"]])), scope)
        context["source_versions"] = dict(context["source_versions"])
        return data, context

    def install(self, name, context, records, catalog, provenance):
        context = copy.deepcopy(context)
        context["provenance"] = provenance
        refs = list(dict.fromkeys([*context["input_refs"],
            *(ref for values in provenance.values() for ref in values)]))
        context["input_refs"] = refs
        for ref in refs:
            context["source_versions"].setdefault(ref, "v1")
        self.provider.install(name, context, records, catalog)
        self.packages[name] = {**context, "records": records, "catalog": catalog}

    def arguments(self, name, context):
        result = {"scope": self.w, "expected_versions": {"fixture": "v1"},
            **{key: context[key] for key in ("as_of", "period_start", "period_end")}}
        if name == "accounting_report":
            result["source"] = "simulation"
        else:
            result.update(portfolio_ref="audit-portfolio/current", accounting_basis="equity")
        return result

    def final_guard(self):
        with closing(self.g.bank.connect()) as db:
            privacy = self.privacy_revision(db, self.w)
        with self.g.response_guard(self.w, privacy, self.g.personal_data.revision(self.w),
                metric_batches=self.observer.batches):
            self.assertIs(self.g.assert_publication_current(), True)
        self.stages["response_guard"] = True

    def issue(self, cid, name, arguments):
        before, previous = self.bank_state(), len(self.observer.batches)
        self.requests.append({"name": name, "arguments": copy.deepcopy(arguments)})
        report = self.g.financial_method_report(self.w, name, arguments)
        self.actual_reports.append(report)
        self.stages["gateway"] = True
        after = self.bank_state()
        self.financial_checks.append({"phase": "method_report", "before": before, "after": after})
        self.assertEqual(after, before)
        self.assertIs(report["no_financial_side_effect"], True)
        self.assertIs(report["execution_authority"], False)
        batch = report["metric_batch"]
        self.assertEqual((batch["workspace_id"], batch["tool_name"]), (self.w, name))
        self.assertEqual(len(batch["service_signature"]), 64)
        self.service.validate(self.w, batch)
        self.stages["hmac_validate"] = True
        self.assertEqual(len(self.observer.batches), previous + 1)
        self.assertEqual(self.observer.batches[-1], batch)
        self.stages["observer"] = True
        self.assertEqual(report["metrics"], batch["metrics"])
        metadata = self.packages.get(name)
        for metric in batch["metrics"]:
            self.assertEqual(set(metric), self.claims_module.METRIC_FIELDS)
            self.assertEqual(metric["subject_id"], self.w)
            self.assertEqual(metric["currency"], "CNY")
            self.assertEqual(metric["unit"], "minor_currency")
            self.assertIn(type(metric["value_minor"]), (int, type(None)))
            self.assertTrue(metric["basis"])
            self.assertTrue(metric["formula_id"])
            self.assertEqual(bool(metric["as_of"]), metric["period_start"] is None)
            if metadata:
                self.assertEqual(metric["data_scope"], metadata["data_scope"])
                self.assertTrue(set(metadata["input_refs"]) <= set(metric["input_refs"]))
                for ref, version in metadata["source_versions"].items():
                    self.assertEqual(metric["source_versions"][ref], version)
            kind = batch["metric_kinds"][metric["metric_id"]]
            self.assertEqual(kind, "planning_scenario" if name == "allocation_preview" else "recorded_fact")
            self.registry.register(metric, kind=kind, evidence_id="remaining/" + cid)
            registered = self.registry.lookup(metric["metric_id"])
            self.assertEqual(registered["metric"], metric)
            self.assertEqual(registered["kind"], kind)
        self.stages["registry"] = True
        if metadata:
            self.assertTrue(self.provider.reads)
            # 发行/最终门在真实事务内；独立validate还会进行事务外只读版本回查。
            # 保存全部读取，不把合法的额外回查误判为没有发行事务。
            self.assertTrue(any(row["in_transaction"] for row in self.provider.reads))
            self.assertTrue(all(row["records_sha256"] == digest(metadata["records"])
                and row["catalog_sha256"] == digest(metadata["catalog"]) for row in self.provider.reads))
        forged = copy.deepcopy(batch)
        forged["metrics"][0]["currency"] = "USD"
        with self.assertRaises(self.BankError) as caught:
            self.service.validate(self.w, forged)
        self.assertEqual(caught.exception.code, "METRIC_TAMPERED")
        self.stages["tampered_hmac_refused"] = True
        self.final_guard()
        after = self.bank_state()
        self.financial_checks.append({"phase": "after_registry_and_final_guard", "before": before, "after": after})
        self.assertEqual(after, before)
        self.stages["zero_financial_side_effect"] = True
        return report

    def values(self, report, wanted):
        metrics = {metric["metric_name"]: metric for metric in report["metrics"]}
        for name, value in wanted.items():
            self.assertIn(name, metrics)
            self.assertEqual(metrics[name]["value_minor"], value, name)
            self.assertIs(type(metrics[name]["value_minor"]), type(value), name)
        return metrics

    def require_gaps(self, values, expected):
        for gap in expected.get("coverage_gaps_contains", []):
            self.assertIn(gap, values)

    def accounting_fixture(self, cid):
        data, context = self.context(cid)
        catalog = {"complete": True, "coverage_ref": "audit:independent-catalog"}
        provenance = {"coverage": ["audit:independent-catalog"]}
        if context["as_of"] is not None:
            records = {"components": self.mapped(data["components"])}
            # 目录来自独立声明；缺失loan仍必须留在此目录。
            catalog["expected_object_ids"] = copy.deepcopy(data["expected_object_ids"])
            provenance["components"] = [row["evidence_ref"] for row in records["components"]]
        else:
            records = {key: self.mapped(data[key]) for key in ("opening_cash_minor", "closing_cash_minor", "movements")}
            if cid == "A03_one_shot_offset":
                ids, accounts, refunds = ["salary", "bill"], ["bank"], []
            elif cid == "A04_complete_true_zero":
                ids, accounts, refunds = [], ["bank"], []
            else:
                ids = ["borrow", "repay", "interest", "bill", "refund", "buy", "out", "in"]
                accounts, refunds = ["bank", "wallet"], ["refund"]
            catalog.update(expected_movement_ids=ids, expected_account_ids=accounts,
                expected_refund_ids=refunds, refunds_complete=True)
            provenance.update(opening_cash=["audit:opening-cash"], closing_cash=["audit:closing-cash"],
                movements=["audit:movements", *[row["evidence_ref"] for row in records["movements"]]])
            if refunds:
                original = self.mapped(copy.deepcopy(_path_item(CASES["A02_cash_classification"]["inputs"]["movements"], "bill")))
                original["evidence_ref"] = "audit:refund-original"
                records.update(refund_originals=[original], refund_cumulative_minor={"bill": 2000})
                provenance.update(refund_originals=["audit:refund-original"], refund_cumulative=["audit:refund-cumulative"])
            else:
                records.update(refund_originals=[], refund_cumulative_minor={})
        self.install("accounting_report", context, records, catalog, provenance)
        return self.arguments("accounting_report", context)

    def accounting_case(self, cid):
        report = self.issue(cid, "accounting_report", self.accounting_fixture(cid))
        expected = CASES[cid]["expected"]
        self.assertEqual(report["calculation_status"], expected.get("status", expected.get("calculation_status")))
        if "metrics" in expected:
            self.values(report, expected["metrics"])
        if expected.get("all_metrics_null") or cid == "A10_refund_unverified_original":
            self.assertTrue(all(row["value_minor"] is None for row in report["metrics"]))
        self.require_gaps(report["reasons"], expected)
        if cid == "A10_refund_unverified_original":
            self.assertIn("refund_original_identity_mismatch", report["reasons"])

    def returns_fixture(self, cid):
        data, context = self.context(cid)
        records = self.mapped(copy.deepcopy(data))
        records.pop("context_ref", None)
        history = "instrument_id" in records.get("opening", {})
        records.update(accounting_basis="history_moving_average" if history else "equity")
        records.setdefault("owner_contributions_minor", 0)
        records.setdefault("owner_withdrawals_minor", 0)
        if "opening_equity_minor" in records:
            for key, on in (("opening", context["period_start"]), ("closing", context["period_end"])):
                records[key] = {"subject_id": self.w, "currency": "CNY", "on": on, "holdings": [],
                    "expected_instrument_ids": [], "cash_minor": records.pop(key + "_equity_minor"),
                    "receivables_minor": 0, "payables_minor": 0, "coverage_complete": True}
        if "subperiods" in records:
            # B08共同literal的独立补充，不由修改后的边界/实际输出反推。
            for key, on, cash in (("opening", "2026-10-06", 10000), ("closing", "2026-11-06", 19475)):
                records[key] = {"subject_id": self.w, "currency": "CNY", "on": on, "holdings": [],
                    "expected_instrument_ids": [], "cash_minor": cash, "receivables_minor": 0,
                    "payables_minor": 0, "coverage_complete": True}
            records.update(owner_contributions_minor=10000, owner_withdrawals_minor=0)
        has_instrument = history or any(records.get(key, {}).get("holdings") for key in ("snapshot", "opening", "closing"))
        instrument = {"instrument_id": "P/share", "share_class_id": "share", "channel_id": "audit-channel",
            "currency": "CNY", "data_scope": "independent_synthetic"}
        catalog = {"portfolio_ref": "audit-portfolio/current", "expected_instrument_ids": ["P/share"] if has_instrument else [],
            "instruments": [instrument] if has_instrument else []}
        if history:
            catalog["expected_event_ids"] = copy.deepcopy(data["coverage"]["expected_event_ids"])
        if "subperiods" in records:
            catalog["expected_boundary_dates"] = ["2026-10-20"]
        prices = []
        if history:
            records["opening"].update(share_class_id="share", channel_id="audit-channel")
            price_points = [(context["period_start"], records["opening"].pop("unit_price")),
                (context["period_end"], records.pop("closing_unit_price"))]
            for on, value in price_points:
                prices.append({**instrument, "on": on, "unit": "major_currency_per_share", "unit_price": value})
        else:
            for key in ("snapshot", "opening", "closing"):
                snapshot = records.get(key)
                if snapshot is None:
                    continue
                for holding in snapshot["holdings"]:
                    holding.update(share_class_id="share", channel_id="audit-channel")
                    prices.append({**instrument, "on": snapshot["on"], "unit": "major_currency_per_share",
                        "unit_price": holding.pop("unit_price")})
        records["prices"] = prices
        categories = ["snapshot"] if context["as_of"] else ["opening", "closing"]
        categories += ["holdings", "prices", "cash", "receivables", "payables", "owner_flows", "coverage"]
        if history:
            categories += ["costs", "events", "fees", "dividends", "splits"]
        if "subperiods" in records:
            categories += ["boundaries"]
        provenance = {key: ["audit:returns:" + key] for key in categories}
        self.install("returns_report", context, records, catalog, provenance)
        arguments = self.arguments("returns_report", context)
        arguments["accounting_basis"] = records["accounting_basis"]
        return arguments

    def returns_case(self, cid):
        report = self.issue(cid, "returns_report", self.returns_fixture(cid))
        expected = CASES[cid]["expected"]
        if cid in ("B13_simple_return_with_flow", "B14_twr_boundary_mismatch", "G03_ratio_not_amount_claim"):
            metric_name = "simple_return" if cid.startswith("B13") else "time_weighted_return"
            ratio = next(row for row in report["ratio_reports"] if row["metric_name"] == metric_name)
            self.assertEqual(ratio["unit"], "ratio")
            self.assertNotIn("value_minor", ratio)
            if cid == "G03_ratio_not_amount_claim":
                self.assertEqual(ratio["value_decimal"], "-0.0025")
                self.assertEqual(ratio["calculation_status"], "calculated")
            else:
                self.assertIsNone(ratio["value_decimal"])
                self.assertEqual(ratio["calculation_status"], expected["calculation_status"])
                self.require_gaps(ratio["coverage_gaps"], expected)
            with self.assertRaises(self.claims_module.MetricClaimError):
                self.registry.register(ratio, kind="recorded_fact", evidence_id="ratio-must-not-be-money")
            self.stages["ratio_amount_registration_refused"] = True
            return
        wanted = {key: value for key, value in expected.items() if key in (
            "period_pnl", "closing_dividend_receivables", "recognized_dividend_income", "remaining_cost",
            "unrealized_pnl", "closing_cash", "external_fee_expense", "portfolio_equity", "holdings_market_value")}
        wanted.update(expected.get("known_metrics", {}))
        metrics = self.values(report, wanted)
        status = expected.get("report_calculation_status", expected.get("calculation_status"))
        if status:
            self.assertEqual(report["calculation_status"], status)
        if "quantity_decimal" in expected:
            self.assertEqual(report["quantity_decimal"], expected["quantity_decimal"])
        if expected.get("all_history_metrics_null"):
            self.assertTrue(all(row["value_minor"] is None for row in report["metrics"]))
        for name in expected.get("null_metrics", []):
            self.assertIsNone(metrics[name]["value_minor"])
            self.assertEqual(metrics[name]["calculation_status"], expected["null_metric_status"])
        self.require_gaps(report["reasons"], expected)
        if "excluded_contains_fee_id" in expected:
            self.assertTrue(any(expected["excluded_contains_fee_id"] in value
                for value in metrics["period_pnl"]["excluded_items"]))
        if cid == "B02_internal_settlement":
            # Opening and closing are named roles of the same portfolio_equity
            # metric. Keep their frozen hand values and dates separate rather
            # than indexing a dict that overwrites equal metric names.
            context = ORACLE["contexts"][resolved_input(cid)["context_ref"]]
            identities = []
            for role, amount, date in (
                    ("opening_portfolio_equity", 17800, context["period_start"]),
                    ("closing_portfolio_equity", 18000, context["period_end"])):
                metric = report["named_metrics"][role]
                self.assertEqual(metric["metric_name"], "portfolio_equity")
                self.assertEqual(metric["value_minor"], amount)
                self.assertIs(type(metric["value_minor"]), int)
                self.assertEqual(metric["as_of"], date)
                self.assertIn(metric["metric_id"], {row["metric_id"] for row in report["metrics"]})
                identities.append(metric["metric_id"])
            self.assertNotEqual(*identities)

    def issued_budget(self, cid):
        upstream = DESIGN["budget_bridge"][cid]
        self.assertEqual(upstream["balance_minor"] - upstream["emergency_floor_minor"] -
            upstream["confirmed_outflow_minor"], upstream["expected_budget_minor"])
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
            row = dict(db.execute("SELECT * FROM scheduled_payments WHERE workspace_id=? LIMIT 1", (self.w,)).fetchone())
            db.execute("UPDATE scheduled_payments SET status='cancelled' WHERE workspace_id=?", (self.w,))
            row.update(id="remaining-fixed-obligation", account_id=main,
                amount_minor=upstream["confirmed_outflow_minor"], frequency="once", next_due_on="2026-10-20",
                status="active", end_on=None)
            columns = list(row)
            db.execute("INSERT INTO scheduled_payments(" + ",".join(columns) + ") VALUES(" +
                ",".join("?" for _ in columns) + ")", [row[key] for key in columns])
            db.execute("UPDATE workspaces SET as_of='2026-10-06',version=version+1 WHERE id=?", (self.w,))
        before = self.bank_state()
        self.funding = self.service.snapshot(self.w, 31)
        after = self.bank_state()
        self.financial_checks.append({"phase": "actual_budget_snapshot", "before": before, "after": after})
        self.assertEqual(after, before)
        metric = self.funding["funding"]["metrics"]["current_budget"]
        self.assertEqual(metric["value_minor"], upstream["expected_budget_minor"])
        self.assertIs(type(metric["value_minor"]), int)
        self.assertEqual((metric["basis"], metric["formula_id"]), ("floor_surplus", "funds.current_budget/1"))
        self.assertEqual((metric["period_start"], metric["period_end"]), ("2026-10-06", "2026-11-06"))
        self.assertEqual(self.service.resolve_budget_metric(self.w, metric["metric_id"]).to_dict(), metric)
        self.stages["budget_actual_snapshot_issued"] = True
        return metric

    def allocation_fixture(self, cid, funds):
        data, context = self.context(cid, funds["data_scope"])
        context.update({key: funds[key] for key in ("data_scope", "as_of", "period_start", "period_end")})
        records = {key: self.mapped(data[key], funds["data_scope"]) for key in ("goals", "products", "risk")}
        provenance = {"coverage": ["audit:allocation-catalog"], "goals": [], "products": [], "risk": []}
        for key in ("goals", "products", "risk"):
            rows = [records[key]] if key == "risk" and records[key] is not None else records[key] or []
            for row in rows:
                versions = dict(row["source_versions"])
                for ref in row["input_refs"]:
                    versions[ref] = "v1"  # 明确冻结的fixture来源，不是生产版本猜测。
                row["source_versions"] = list(versions.items())
                provenance[key] += row["input_refs"]
                context["source_versions"].update(versions)
        for key in provenance:
            provenance[key] = list(dict.fromkeys(provenance[key]))
        self.install("allocation_preview", context, records,
            {"constraints_ref": "audit:confirmed-goals", "expected_goal_ids": [row["goal_id"] for row in records["goals"]],
             "product_evidence_refs": provenance["products"]}, provenance)
        return {"scope": self.w, "budget_metric_id": funds["metric_id"], "constraints_ref": "audit:confirmed-goals",
            "product_evidence_refs": provenance["products"], "expected_versions": {"fixture": "v1"}}

    def assert_plans(self, report, expected):
        plans = {plan["plan_id"]: plan for plan in report["plans"]}
        wanted = list(expected.get("plans", []))
        wanted += [dict(value, plan_id=key) for key, value in expected.items()
            if key in ("retain_cash", "confirmed_uses")]
        for item in wanted:
            actual = plans[item["plan_id"]]
            for field in ("calculation_status", "feasibility", "execution_authority", "future_proceeds_minor",
                    "conservation_difference_minor", "cash_partition_difference_minor", "allocations"):
                if field in item:
                    self.assertEqual(actual[field], item[field], field)
            self.values({"metrics": list(actual["metrics"].values())}, item["metrics"])
            self.require_gaps(actual["coverage_gaps"], item)
            if "allocation_goal_ids" in item:
                self.assertEqual([row["goal_id"] for row in actual["allocations"]], item["allocation_goal_ids"])
        if "goal_c_status" in expected:
            self.assertEqual(next(row for row in plans["confirmed_uses"]["goal_states"] if row["goal_id"] == "c")["status"],
                expected["goal_c_status"])

    def allocation_case(self, cid):
        if cid == "C17_unconfirmed_priority":
            self.native_unconfirmed_priority(cid)
            return
        funds = self.issued_budget(cid)
        arguments = self.allocation_fixture(cid, funds)
        report = self.issue(cid, "allocation_preview", arguments)
        self.assertEqual(report["funds"], funds)
        self.assert_plans(report, CASES[cid]["expected"])
        if cid == "C10_equal_priority_shortfall":
            # 同等优先级原件两种冻结顺序；每次仍经完整服务重新发行/最终门。
            records = copy.deepcopy(self.packages["allocation_preview"]["records"])
            records["goals"] = list(reversed(records["goals"]))
            with self.g.bank.transaction() as db:
                db.execute("UPDATE integration_method_raw_records SET payload=? WHERE workspace_id=? AND name='allocation_preview'",
                    (json.dumps(records, ensure_ascii=False), self.w))
            self.packages["allocation_preview"]["records"] = records
            # 旧批次必须过期，不能继续放入最后发布门；重新创建本轮Observer/Registry。
            with self.assertRaises(self.BankError) as caught:
                self.service.validate(self.w, report["metric_batch"])
            self.assertEqual(caught.exception.code, "METRIC_VERSION_CONFLICT")
            self.observer = self.evidence_module.MetricEvidenceObserver(self.service)
            self.g.financial_metrics = self.observer
            self.registry = self.claims_module.MetricRegistry()
            self.provider.reads.clear()
            report2 = self.issue(cid, "allocation_preview", arguments)
            self.assert_plans(report2, CASES[cid]["expected"])

    def native_unconfirmed_priority(self, cid):
        # 使用真实GoalService抽取原始default=3且没有确认回执的目标。
        with self.g.bank.transaction() as db:
            customer = db.execute("SELECT customer_id FROM wealth_goals WHERE workspace_id=? LIMIT 1", (self.w,)).fetchone()[0]
            db.execute("DELETE FROM navren_goal_drafts WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE wealth_goals SET status='completed' WHERE workspace_id=?", (self.w,))
            db.execute("INSERT INTO wealth_goals(id,workspace_id,source_id,data_origin,details,customer_id,name,"
                "target_minor,saved_minor,due_on,priority,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("cash", self.w, "qingzhou", "qingzhou_synthetic", "{}", customer, "冻结现金用途",
                 100000, 0, "2026-11-06", 3, "active"))
        # 目标属于预算依赖版本；写原始fixture后由实际snapshot重新发行。
        funds = self.issued_budget(cid)
        del self.g.finance_method_sources
        before = self.bank_state()
        constraints = self.g.goals.constraints(self.w)
        self.assertEqual(len(constraints["goals"]), 1)
        goal = constraints["goals"][0]
        self.assertIsNone(goal["priority"])
        self.assertFalse(goal["confirmed"])
        self.assertIn("goal_priority_unconfirmed:cash", constraints["reasons"])
        self.assertEqual(next(row for row in before["wealth_goals"] if row["id"] == "cash")["priority"], 3)
        self.assertEqual(before["navren_goal_drafts"], [])
        self.native_source = {"raw_wealth_goals": before["wealth_goals"], "raw_confirmation_receipts": [],
            "real_constraints": constraints, "catalog_source": "GoalService.goal_set version"}
        arguments = {"scope": self.w, "budget_metric_id": funds["metric_id"], "constraints_ref": "goals/current",
            "product_evidence_refs": [], "expected_versions": {}}
        report = self.issue(cid, "allocation_preview", arguments)
        self.assertEqual(report["funds"], funds)
        self.assert_plans(report, CASES[cid]["expected"])
        self.assertTrue(report["plans"][1]["coverage_gaps"])
        # 具体本人确认缺项由真实GoalService原因证明，规划层可能统称缺确认用途。
        self.assertIn("goal_confirmation_missing:cash", constraints["reasons"])
        for metric in report["metrics"]:
            self.assertTrue(set(constraints["input_refs"]) <= set(metric["input_refs"]))

    def rejection_case(self, cid):
        expected = CASES[cid]["expected"]
        if cid == "G01_boolean_money":
            name, arguments = "accounting_report", self.accounting_fixture(cid)
            attempts = [(arguments, expected["error_code"], expected["http_status"])]
        else:
            funds = self.issued_budget(cid)
            name, arguments = "allocation_preview", self.allocation_fixture(cid, funds)
            arguments["budget_metric_id"] = "not-issued"
            client_args = {**arguments, "client_value_minor": 500000, "client_formula_id": "funds.current_budget/1"}
            attempts = [(arguments, "METRIC_BUDGET_NOT_ISSUED", 409), (client_args, "METRIC_ARGUMENTS_INVALID", 422)]
        before = self.bank_state()
        for arguments, code, status in attempts:
            self.requests.append({"name": name, "arguments": copy.deepcopy(arguments)})
            with self.assertRaises(self.BankError) as caught:
                self.g.financial_method_report(self.w, name, arguments)
            self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
            self.actual_reports.append({"error_code": code, "http_status": status})
            self.assertEqual(self.observer.batches, [])
            self.assertEqual(tuple(self.registry.metric_ids), ())
            after = self.bank_state()
            self.financial_checks.append({"phase": "rejected_gateway_call", "before": before, "after": after})
            self.assertEqual(after, before)
        self.stages.update(gateway=True, no_signed_result=True, no_amount_registered=True,
            zero_financial_side_effect=True)
        self.final_guard()
        after = self.bank_state()
        self.financial_checks.append({"phase": "rejection_final_guard", "before": before, "after": after})
        self.assertEqual(after, before)

    def run_case(self, cid):
        passed, error = False, None
        try:
            if cid.startswith("A"):
                self.accounting_case(cid)
            elif cid.startswith("B") or cid == "G03_ratio_not_amount_claim":
                self.returns_case(cid)
            elif cid in ("C15_unissued_or_forged_budget", "G01_boolean_money"):
                self.rejection_case(cid)
            else:
                self.allocation_case(cid)
            passed = True
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            source = source_manifest()
            row = {"case_id": cid, "assertions_passed": passed, "error": error,
                "source_bound_only": True, "sources_unchanged": source == self.before,
                "oracle_case_sha256": MATRIX[cid]["oracle_case_sha256"],
                "expected_sha256": MATRIX[cid]["expected_sha256"], "stages": self.stages,
                "paths": self.paths, "workspace_id": self.w, "private_key_origin": self.private_key_origin,
                "observer_batches": len(self.observer.batches), "registry_count": len(self.registry.metric_ids),
                "cloud_model_calls": 0}
            self.observations.append(row)
            write_json(self.output / (cid + "-evidence.json"), {**row,
                "source_packages": self.packages, "source_reads": self.provider.reads,
                "native_source": getattr(self, "native_source", None), "budget_snapshot": self.funding,
                "requests": self.requests, "actual_reports": self.actual_reports,
                "financial_state_checks": self.financial_checks,
                "final_financial_state": self.bank_state()})
            if source != self.before:
                raise RuntimeError("candidate_changed_during_case:" + cid)


def _add_case(case_id):
    def test(self):
        self.run_case(case_id)
    test.__name__ = "test_" + case_id
    setattr(FinanceAdapterRemainingContracts, test.__name__, test)


for _case_id in SELECTED:
    _add_case(_case_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--case-ids", nargs="+", required=True,
        help="explicit new cases only; no original12 or uncovered fake cases")
    args = parser.parse_args()
    if len(set(args.case_ids)) != len(args.case_ids) or set(args.case_ids) - set(SELECTED):
        parser.error("case selection must be unique and implemented in remaining29")
    os.environ["NAVREN_FINANCE_REMAINING_OUTPUT"] = args.out_dir
    os.environ["NAVREN_FINANCE_REMAINING_MODE"] = "diagnostic_source_bound"
    suite = unittest.TestSuite(FinanceAdapterRemainingContracts("test_" + cid) for cid in args.case_ids)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
