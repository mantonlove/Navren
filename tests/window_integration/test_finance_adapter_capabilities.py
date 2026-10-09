"""冻结的资金缺项、真实目标占用、资格来源和预算入口能力测试。

只在 Base 正式交接后的独立候选中执行。六方法不收集原12/29，C07/C08
桥的输入差异使其仅为独立服务能力试验，C16拒绝也不是原oracle规划通过。
设计时不导入业务或运行服务；真实运行由外层记录超时、counts及进程退出。
"""
from __future__ import annotations

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
OUTPUT_ROOT = ROOT / "outputs/window-progress/integration/finance-adapter-capabilities"
ORACLE_PATH = ROOT / "evaluation/window_integration/finance_adapter_oracles.json"
DESIGN_PATH = ROOT / "evaluation/window_integration/finance_adapter_capability_design.json"
HELPER_PATH = ROOT / "tests/window_integration/test_finance_adapter_handoff.py"
ORACLE_SHA256 = "91cfb1dc3f3cfbe99d39785535bd2d1861b10273e1a337b006357f96b2ebee6b"
DESIGN_SHA256 = "e3fd3e1fcb0778abe76647871d17660e38f8c8bf6396d441780af6c337d43d42"
# 仅迁移已批准C01夹具出处接线的helper SHA，其读取类和原设计均未变。
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
METHODS = {row["method"]: row for row in DESIGN["methods"]}


def resolved_input(case_id):
    """原预期递归继承；点路径按确切索引/对象ID应用。"""
    case = CASES[case_id]
    value = resolved_input(case["base_case"]) if case.get("base_case") else copy.deepcopy(case["inputs"])
    for path, replacement in case.get("mutation", {}).items():
        target, keys = value, path.split(".")
        for key in keys[:-1]:
            if isinstance(target, list):
                if key.isdigit():
                    target = target[int(key)]
                else:
                    rows = [row for row in target if any(row.get(field) == key
                        for field in ("goal_id", "object_id", "event_id", "movement_id"))]
                    if len(rows) != 1:
                        raise ValueError("mutation_identity_not_unique:" + key)
                    target = rows[0]
            else:
                target = target[key]
        if isinstance(target, list):
            target[int(keys[-1])] = copy.deepcopy(replacement)
        else:
            target[keys[-1]] = copy.deepcopy(replacement)
    return value


def source_manifest():
    paths = set(ROOT.glob("*.py"))
    for folder in ("agent_runtime", "base_runtime", "simulated_bank", "financial_methods", "local_knowledge"):
        paths.update((ROOT / folder).rglob("*.py"))
    paths.update((Path(__file__).resolve(), ORACLE_PATH, DESIGN_PATH, HELPER_PATH))
    for relative in set(DESIGN["input_sha256"]) | set(DESIGN["wip_interface_observations_sha256"]):
        paths.add(ROOT / relative)
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths) if path.is_file()}


class SqliteFundingSource:
    """受信来源接口；只从真实事务db读取事先独立冻结的三张SQLite表。

    不替换发行器/算法，也不返回指标。目录是固定JSON，不从raw记录生成。
    """
    TABLES = ("capability_funding_metadata", "capability_funding_raw", "capability_funding_catalog")

    def __init__(self, bank, workspace, envelope):
        self.workspace, self.reads = workspace, []
        values = ({k: v for k, v in envelope.items() if k not in ("records", "catalog")},
            envelope["records"], envelope["catalog"])
        with bank.transaction() as db:
            for table, value in zip(self.TABLES, values):
                db.execute("CREATE TABLE " + table + "(workspace_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                db.execute("INSERT INTO " + table + " VALUES(?,?)",
                    (workspace, json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def snapshot(self, workspace, days, *, db):
        if workspace != self.workspace or days != 31 or db is None:
            raise AssertionError("trusted_funding_requires_actual_workspace_days_and_db")
        values = [json.loads(db.execute("SELECT payload FROM " + table + " WHERE workspace_id=?",
            (workspace,)).fetchone()["payload"]) for table in self.TABLES]
        metadata, raw, catalog = values
        self.reads.append({"in_transaction": db.in_transaction, "metadata_sha256": digest(metadata),
            "records_sha256": digest(raw), "catalog_sha256": digest(catalog)})
        return {**metadata, "records": raw, "catalog": catalog}


class FinanceAdapterCapabilityContracts(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        for path, expected in ((ORACLE_PATH, ORACLE_SHA256), (DESIGN_PATH, DESIGN_SHA256)):
            if sha(path) != expected:
                raise RuntimeError("frozen_input_changed:" + str(path))
        for relative, expected in DESIGN["input_sha256"].items():
            if relative == str(HELPER_PATH.relative_to(ROOT)):
                expected = HELPER_INPUT_SHA256
            if sha(ROOT / relative) != expected:
                raise RuntimeError("frozen_dependency_changed:" + relative)
        if Path(sys.executable).absolute().parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required")
        if len(METHODS) != 6 or len(DESIGN["original_cases"]) != 5:
            raise RuntimeError("frozen_case_count_changed")
        for row in DESIGN["original_cases"]:
            cid = row["case_id"]
            if (digest(CASES[cid]) != row["oracle_case_sha256"] or
                    resolved_input(cid) != row["resolved_input"] or
                    digest(resolved_input(cid)) != row["resolved_input_sha256"] or
                    CASES[cid]["expected"] != row["expected_literal"] or
                    digest(CASES[cid]["expected"]) != row["expected_sha256"]):
                raise RuntimeError("original_literal_or_mutation_changed:" + cid)
        destination = os.environ.get("NAVREN_FINANCE_CAPABILITY_OUTPUT")
        cls.output = (Path(destination).resolve() if destination else OUTPUT_ROOT /
            ("run-" + str(time.time_ns()) + "-" + uuid.uuid4().hex[:8]))
        if not cls.output.is_relative_to(OUTPUT_ROOT.resolve()) or cls.output == OUTPUT_ROOT.resolve():
            raise RuntimeError("isolated_output_scope_required")
        if cls.output.exists():
            raise RuntimeError("new_run_directory_required")
        cls.output.mkdir(parents=True)
        cls.before, cls.observations = source_manifest(), []
        write_json(cls.output / "source-before.json", cls.before)
        # 只载入非TestCase的维护方读取helper；不会收集原12方法。
        spec = importlib.util.spec_from_file_location("_readonly_capability_sqlite_helper", HELPER_PATH)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        cls.sqlite_method_source = helper.SqliteAuditedSource

    @classmethod
    def tearDownClass(cls):
        after = source_manifest()
        observations = {row["method"]: row for row in cls.observations}
        report = {"schema": "navren.finance-adapter-capability-run/1", "source_bound_only": True,
            "oracle_sha256": ORACLE_SHA256, "design_sha256": DESIGN_SHA256,
            "source_before": cls.before, "source_after": after, "sources_unchanged": cls.before == after,
            "expected_test_method_count": 6, "actual_observation_count": len(cls.observations),
            "method_disposition": [{"method": method, "kind": row["kind"],
                "status": ("source_bound_observed" if observations[method]["assertions_passed"]
                    and cls.before == after else "failed") if method in observations else "NOT_RUN"}
                for method, row in METHODS.items()],
            "original_case_disposition": [{"case_id": row["case_id"],
                "original_oracle_file_adapter_execution_status": row["original_adapter_execution_status"],
                "disposition": row["disposition"],
                "original_literal_path_observed": row["case_id"] in ("C06_unknown_budget", "C14_simulated_risk_not_formal")
                    and any(item["case_id"] == row["case_id"] and item["assertions_passed"]
                        for item in cls.observations) and cls.before == after}
                for row in DESIGN["original_cases"]],
            "observations": cls.observations, "cloud_model_calls": 0,
            "original_C07_C08_same_input_adapter_passed": False,
            "original_C16_adapter_passed": False, "formal_46_passed": False,
            "suite_execution_exit_code": None,
            "exit_receipt_contract": "Actual process exit, standard unittest counts and hard timeout belong to root external executor",
            "limitations": DESIGN["limitations"]}
        write_json(cls.output / "suite-evidence.json", report)
        print("WINDOW_FINANCE_CAPABILITIES_SUMMARY " + json.dumps({"output": str(cls.output),
            "observations": len(cls.observations), "sources_unchanged": cls.before == after,
            "cloud_model_calls": 0, "formal_46_passed": False}, ensure_ascii=False), flush=True)
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
        self.network_attempts = []
        def deny_network(*args, **kwargs):
            self.network_attempts.append("blocked_network_attempt")
            raise AssertionError("network_forbidden")
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, side_effect=deny_network)
            guard.start()
            self.addCleanup(guard.stop)
        # 所有临时路径、私钥和断网约束早于业务import。
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
        self.reset_observer()
        self.provider = self.sqlite_method_source(self.g.bank, self.w)
        self.paths = {key: str(value) for key, value in paths.items()}
        self.paths.update(fixture_directory=str(self.directory), service_rules=str(self.g.service_negotiations.rules_path))
        self.packages, self.stages, self.reports, self.requests = {}, {}, [], []
        self.source_variants = []
        self.state_checks, self.goal_setup, self.funding = [], [], None
        self.funding_provider = None

    @staticmethod
    def purge_business_modules():
        for name, module in list(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if filename and Path(filename).resolve().is_relative_to(ROOT) and not name.startswith("tests"):
                relative = Path(filename).resolve().relative_to(ROOT)
                if relative.parts[0] != ".venv" and Path(filename).resolve() != Path(__file__).resolve():
                    sys.modules.pop(name, None)

    def reset_observer(self):
        self.observer = self.evidence_module.MetricEvidenceObserver(self.service)
        self.g.financial_metrics = self.observer
        self.registry = self.claims_module.MetricRegistry()

    def bank_state(self):
        with closing(self.g.bank.connect()) as db:
            result = {}
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                table = row[0]
                if not table.replace("_", "").isalnum():
                    continue
                columns = {column[1] for column in db.execute("PRAGMA table_info(" + table + ")")}
                key = "workspace_id" if "workspace_id" in columns else "id" if table == "workspaces" else None
                if key:
                    result[table] = [dict(item) for item in db.execute("SELECT * FROM " + table +
                        " WHERE " + key + "=? ORDER BY rowid", (self.w,))]
            return result

    def money_state(self):
        state = self.bank_state()
        return {table: state[table] for table in ("accounts", "profiles", "holdings", "orders", "operations",
            "transactions", "loan_installments", "credit_statements", "subscriptions", "mandates",
            "scheduled_payments", "fund_holds", "orchestration_tasks")}

    def mapped(self, value, scope):
        if isinstance(value, dict):
            return {key: (self.w if key == "subject_id" and item == "fixture-A" else scope
                if key == "data_scope" and item == "independent_synthetic" else self.mapped(item, scope))
                for key, item in value.items()}
        if isinstance(value, list):
            return [self.mapped(item, scope) for item in value]
        return value

    def normalize_bank(self, balance, floor=200000, outflow=0, hold=False, goals=()):
        """显式冻结夹具安装，不是被测只读请求；金额绝不来自服务返回。"""
        with self.g.bank.transaction() as db:
            self.main = db.execute("SELECT id FROM accounts WHERE workspace_id=? AND id LIKE '%-main'",
                (self.w,)).fetchone()[0]
            customer = db.execute("SELECT id FROM customers WHERE workspace_id=? ORDER BY id LIMIT 1",
                (self.w,)).fetchone()[0]
            db.execute("UPDATE accounts SET balance_minor=0,opening_balance_minor=0 WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE accounts SET balance_minor=?,opening_balance_minor=? WHERE workspace_id=? AND id=?",
                (balance, balance, self.w, self.main))
            db.execute("UPDATE profiles SET reserve_minor=? WHERE workspace_id=?", (floor, self.w))
            db.execute("UPDATE loan_installments SET status='paid',paid_minor=principal_minor+interest_minor+fees_minor WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE credit_statements SET status='paid',paid_minor=total_due_minor WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE subscriptions SET merchant_status='cancelled' WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE mandates SET status='closed' WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE orders SET status='settled' WHERE workspace_id=? AND status='pending_settlement'", (self.w,))
            db.execute("UPDATE scheduled_payments SET status='cancelled' WHERE workspace_id=?", (self.w,))
            db.execute("DELETE FROM fund_holds WHERE workspace_id=?", (self.w,))
            db.execute("UPDATE wealth_goals SET status='completed' WHERE workspace_id=?", (self.w,))
            if outflow:
                template = dict(db.execute("SELECT * FROM scheduled_payments WHERE workspace_id=? LIMIT 1", (self.w,)).fetchone())
                template.update(id="capability-fixed-obligation", account_id=self.main, amount_minor=outflow,
                    frequency="once", next_due_on="2026-10-20", status="active", end_on=None)
                columns = list(template)
                db.execute("INSERT INTO scheduled_payments(" + ",".join(columns) + ") VALUES(" +
                    ",".join("?" for _ in columns) + ")", [template[key] for key in columns])
            if hold:
                db.execute("INSERT INTO orchestration_tasks(id,workspace_id,source_id,data_origin,details,customer_id,"
                    "title,budget_minor,due_on,status,approval_policy) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("capability-task", self.w, "qingzhou", "qingzhou_synthetic", "{}", customer,
                     "冻结既有占用任务", 30000, "2026-11-06", "in_progress", "explicit_fixture_approval"))
                db.execute("INSERT INTO fund_holds(id,workspace_id,source_id,data_origin,details,account_id,task_id,"
                    "amount_minor,status,created_on,expires_on,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("hold-1", self.w, "qingzhou", "qingzhou_synthetic", "{}", self.main,
                     "capability-task", 30000, "held", "2026-10-06", "2026-11-06", "独立冻结30000分既有占用"))
            for row in goals:
                db.execute("INSERT INTO wealth_goals(id,workspace_id,source_id,data_origin,details,customer_id,name,"
                    "target_minor,saved_minor,due_on,priority,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["goal_id"], self.w, "qingzhou", "qingzhou_synthetic",
                     '{"record_origin":"controlled_fixture","user_confirmed":false}', customer,
                     "冻结现金用途" + row["goal_id"], row["amount_minor"], 0,
                     row["needed_on"], row["priority"], "active"))
            db.execute("UPDATE workspaces SET as_of='2026-10-06',version=version+1 WHERE id=?", (self.w,))
        self.stages["independent_literal_fixture_installed"] = True

    def confirm_goal(self, row, bind_hold=False):
        gid = row["goal_id"]
        state = self.g.goals.state(self.w, gid)
        goal = state["goal"]
        self.assertEqual(goal["id"], gid)
        self.assertIs(goal["confirmed"], False)
        payload = {"action": "update", "goal_id": gid, "name": goal["name"],
            "target_minor": row["amount_minor"], "due_on": row["needed_on"],
            "priority": row["priority"], "funding_kind": "cash", "status": "active",
            "expected_version": goal["object_version"], "idempotency_key": "capability-confirm-" + gid}
        if bind_hold:
            payload.update(occupancy_hold_ids=["hold-1"], occupancy_account_id=self.main)
        money_before = self.money_state()
        draft = self.g.goals.prepare(self.w, payload)
        self.assertEqual((draft["goal_id"], draft["status"]), (gid, "prepared"))
        full_before = self.bank_state()
        with self.assertRaises(self.BankError) as caught:
            self.g.goals.confirm(self.w, draft["id"], {"confirmed": True, "confirmation_digest": "0" * 64})
        self.assertEqual(caught.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual(self.bank_state(), full_before)
        receipt = self.g.goals.confirm(self.w, draft["id"], {"confirmed": True,
            "confirmation_digest": draft["confirmation_digest"], "priority": row["priority"], "funding_kind": "cash"})
        self.assertEqual(receipt["receipt"], {"id": draft["id"], "state": "succeeded",
            "action": "update", "goal_id": gid})
        self.assertEqual((receipt["goal"]["id"], receipt["goal"]["target_minor"], receipt["goal"]["priority"],
            receipt["goal"]["due_on"]), (gid, row["amount_minor"], row["priority"], row["needed_on"]))
        self.assertIs(receipt["goal"]["confirmed"], True)
        self.assertEqual(receipt["goal"]["funding_kind"], "cash")
        self.assertEqual(self.money_state(), money_before)
        with closing(self.g.bank.connect()) as db:
            raw_draft = dict(db.execute("SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND id=?",
                (self.w, draft["id"])).fetchone())
            links = [dict(item) for item in db.execute("SELECT * FROM navren_goal_occupancy_links WHERE workspace_id=? AND goal_id=?",
                (self.w, gid))]
        self.assertEqual(raw_draft["status"], "committed")
        self.assertEqual(json.loads(raw_draft["result"]), receipt)
        if bind_hold:
            self.assertEqual(len(links), 1)
            self.assertEqual((links[0]["goal_id"], links[0]["hold_id"], links[0]["draft_id"], links[0]["goal_version"]),
                (gid, "hold-1", draft["id"], receipt["goal"]["object_version"]))
            self.assertEqual(links[0]["receipt_hash"], digest({"digest": draft["confirmation_digest"], "result": receipt}))
            self.assertEqual(links[0]["binding_hash"], digest(draft["occupancy_binding"]))
            self.assertEqual(json.loads(links[0]["hold_snapshot"]), draft["occupancy_binding"]["snapshots"][0])
        else:
            self.assertEqual(links, [])
        self.goal_setup.append({"original_goal_id": gid, "payload": payload, "actual_draft": draft,
            "actual_committed_draft": raw_draft, "actual_receipt": receipt, "actual_links": links,
            "money_before": money_before, "money_after": self.money_state(),
            "confirmation_setup_metadata_effects_allowed": True})
        self.stages["real_goal_subject_confirmation_and_versions"] = True

    def funding_envelope(self, balance=None, basis="ledger_cash_balance"):
        refs = ["capability:funding:cash", "capability:funding:obligation", "capability:funding:catalog"]
        cash = {"subject_id": self.w, "currency": "CNY", "as_of": "2026-10-06",
            "balance_minor": balance, "frozen_minor": 0, "emergency_floor_minor": 200000,
            "coverage_start": "2026-10-06", "coverage_end": "2026-11-06",
            "evidence_ref": refs[0], "source_version": "v1", "basis": basis, "coverage_complete": True}
        event = {"event_id": "capability-obligation", "subject_id": self.w, "currency": "CNY",
            "on": "2026-10-20", "amount_minor": 300000, "direction": "outflow", "certainty": "confirmed",
            "evidence_ref": refs[1], "source_version": "v1", "kind": "obligation"}
        return {"subject_id": self.w, "currency": "CNY", "data_scope": "independent_synthetic",
            "as_of": None, "period_start": "2026-10-06", "period_end": "2026-11-06", "coverage_complete": True,
            "input_refs": refs, "source_versions": dict.fromkeys(refs, "v1"),
            "records": {"cash": cash, "events": [event], "holds": []},
            "catalog": {"complete": True, "coverage_ref": refs[2],
                "expected_event_ids": ["capability-obligation"], "expected_hold_ids": []},
            "provenance": {"cash": [refs[0]], "events": [refs[1]], "holds": [], "coverage": [refs[2]]}}

    def issue_budget(self, expected_value, status="calculated", gap=None, kind="bank_ledger"):
        before = self.bank_state()
        self.funding = self.service.snapshot(self.w, 31)
        self.assertEqual(self.bank_state(), before)
        self.state_checks.append({"phase": "real_issuer_snapshot", "before": before, "after": self.bank_state()})
        self.assertEqual(self.funding["funding_baseline_kind"], kind)
        self.assertIs(self.funding["no_financial_side_effect"], True)
        funds = self.funding["funding"]["metrics"]["current_budget"]
        self.assertEqual((funds["value_minor"], funds["calculation_status"], funds["basis"], funds["formula_id"]),
            (expected_value, status, "floor_surplus", "funds.current_budget/1"))
        self.assertIs(type(funds["value_minor"]), type(expected_value))
        self.assertEqual((funds["as_of"], funds["period_start"], funds["period_end"]),
            (None, "2026-10-06", "2026-11-06"))
        self.assertEqual((funds["subject_id"], funds["currency"], funds["unit"]), (self.w, "CNY", "minor_currency"))
        if gap:
            self.assertIn(gap, funds["coverage_gaps"])
        self.assertEqual(self.service.resolve_budget_metric(self.w, funds["metric_id"]).to_dict(), funds)
        if self.funding_provider:
            self.assertTrue(self.funding_provider.reads)
            self.assertTrue(any(row["in_transaction"] for row in self.funding_provider.reads))
            envelope = self.funding_envelope()
            self.assertTrue(all(row["records_sha256"] == digest(envelope["records"])
                and row["catalog_sha256"] == digest(envelope["catalog"]) for row in self.funding_provider.reads))
        self.stages["actual_budget_snapshot_issued_and_resolved"] = True
        return funds

    def assert_real_hold_baseline(self, funds, goal_ids):
        with closing(self.g.bank.connect()) as db:
            baseline = self.service.resolve_budget_baseline(self.w, funds["metric_id"], db=db)
            version = str(db.execute("SELECT version FROM workspaces WHERE id=?", (self.w,)).fetchone()[0])
            links = [dict(row) for row in db.execute("SELECT * FROM navren_goal_occupancy_links WHERE workspace_id=?", (self.w,))]
        self.assertEqual(baseline["baseline_kind"], "bank_ledger")
        self.assertEqual([row["id"] for row in baseline["holds"]], ["hold-1"])
        self.assertEqual(baseline["holds"][0]["amount_minor"], 30000)
        self.assertIn("accounts/" + self.main, funds["input_refs"])
        self.assertIn("fund_holds/hold-1", funds["input_refs"])
        self.assertEqual(funds["source_versions"]["fund_holds/hold-1"], version)
        self.assertEqual([(row["goal_id"], row["hold_id"]) for row in links], [(goal_ids[0], "hold-1")])
        constraints = self.g.goals.constraints(self.w)
        self.assertEqual([row["goal_id"] for row in constraints["goals"]], goal_ids)
        first = constraints["goals"][0]
        self.assertEqual((first["already_occupied_minor"], first["occupancy_refs"]), (30000, ["fund_holds/hold-1"]))
        self.assertIs(first["occupancy_verified_in_baseline"], False)
        self.assertTrue(all(row["confirmed"] for row in constraints["goals"]))
        self.stages["real_hold_deducted_in_actual_budget_baseline"] = True
        self.native_constraints = constraints
        self.real_baseline = baseline
        return constraints

    def install_allocation(self, cid, funds, records=None, catalog_goal_ids=None):
        original = resolved_input(cid)
        context = self.mapped(copy.deepcopy(ORACLE["contexts"][original["context_ref"]]), funds["data_scope"])
        context.update({key: funds[key] for key in ("data_scope", "as_of", "period_start", "period_end")})
        context["source_versions"] = dict(context["source_versions"])
        context["source_versions"]["adapter:fixture"] = "v1"
        records = (self.mapped({key: copy.deepcopy(original[key]) for key in ("goals", "products", "risk")},
            funds["data_scope"]) if records is None else copy.deepcopy(records))
        if "risk_source" in original and "risk_source" not in records:
            records["risk_source"] = copy.deepcopy(original["risk_source"])
        provenance = {"coverage": ["capability:allocation:catalog"], "goals": [], "products": [], "risk": []}
        for category in ("goals", "products", "risk"):
            rows = ([records[category]] if category == "risk" and records[category] is not None
                else records[category] or [])
            for row in rows:
                versions = dict(row["source_versions"])
                for ref in row["input_refs"]:
                    if ref not in versions:
                        versions[ref] = "v1"  # 确切受控fixture版本；真实GoalService版本已在原件中。
                row["source_versions"] = list(versions.items())
                provenance[category].extend(row["input_refs"])
                context["source_versions"].update(versions)
        if hasattr(self, "native_constraints"):
            # 目录自身来自真实GoalService集合，不能只保留逐goal记录后丢掉集合版本。
            provenance["goals"].extend(self.native_constraints["input_refs"])
            context["source_versions"].update(self.native_constraints["source_versions"])
        for category in provenance:
            provenance[category] = list(dict.fromkeys(provenance[category]))
        context["input_refs"] = list(dict.fromkeys([*context["input_refs"],
            *(ref for values in provenance.values() for ref in values)]))
        for ref in context["input_refs"]:
            context["source_versions"].setdefault(ref, "v1")
        context["provenance"] = provenance
        catalog = {"complete": True, "coverage_ref": "capability:allocation:catalog",
            "constraints_ref": "capability:confirmed-goals", "expected_goal_ids": (catalog_goal_ids
                if catalog_goal_ids is not None else [row["goal_id"] for row in original["goals"]]),
            "product_evidence_refs": provenance["products"]}
        self.provider.install("allocation_preview", context, records, catalog)
        self.g.finance_method_sources = self.provider
        self.packages["allocation_preview"] = {**context, "records": records, "catalog": catalog}
        self.source_variants.append({"label": cid, "package": copy.deepcopy(self.packages["allocation_preview"])})
        return {"scope": self.w, "budget_metric_id": funds["metric_id"],
            "constraints_ref": catalog["constraints_ref"], "product_evidence_refs": provenance["products"],
            "expected_versions": {"fixture": "v1"}}

    def replace_allocation_source(self, package, label):
        """只安装事先声明的受控来源变体；版本与内容由真实发行服务重新读取。"""
        metadata = {key: value for key, value in package.items() if key not in ("records", "catalog")}
        with self.g.bank.transaction() as db:
            for table, value in (("integration_method_metadata", metadata),
                    ("integration_method_raw_records", package["records"]),
                    ("integration_method_catalog", package["catalog"])):
                db.execute("UPDATE " + table + " SET payload=? WHERE workspace_id=? AND name='allocation_preview'",
                    (json.dumps(value, ensure_ascii=False, allow_nan=False), self.w))
        self.packages["allocation_preview"] = copy.deepcopy(package)
        self.source_variants.append({"label": label, "package": copy.deepcopy(package)})

    def reject_dispatch(self, arguments, code, status, label):
        """资格拒绝不产生新签名/登记；保留当下已有预算，不伪称零发行。"""
        before = self.bank_state()
        batches, ids = copy.deepcopy(self.observer.batches), tuple(self.registry.metric_ids)
        budget_count = len(self.service._funding_issuances)
        self.requests.append({"name": "allocation_preview", "arguments": copy.deepcopy(arguments), "label": label})
        with self.assertRaises(self.BankError) as caught:
            self.g.financial_method_report(self.w, "allocation_preview", arguments)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.reports.append({"label": label, "actual_error_code": caught.exception.code,
            "actual_http_status": caught.exception.status})
        self.assertEqual(self.observer.batches, batches)
        self.assertEqual(tuple(self.registry.metric_ids), ids)
        self.assertEqual(len(self.service._funding_issuances), budget_count)
        self.final_guard()
        self.assertEqual(self.bank_state(), before)
        self.state_checks.append({"phase": label, "before": before, "after": self.bank_state()})

    def final_guard(self):
        with closing(self.g.bank.connect()) as db:
            privacy = self.privacy_revision(db, self.w)
        with self.g.response_guard(self.w, privacy, self.g.personal_data.revision(self.w),
                metric_batches=self.observer.batches):
            self.assertIs(self.g.assert_publication_current(), True)
        self.stages["response_guard_and_final_publication_current"] = True

    def issue(self, arguments, funds):
        before, previous = self.bank_state(), len(self.observer.batches)
        self.requests.append({"name": "allocation_preview", "arguments": copy.deepcopy(arguments)})
        report = self.g.financial_method_report(self.w, "allocation_preview", arguments)
        self.reports.append(report)
        self.assertEqual(self.bank_state(), before)
        self.assertIs(report["execution_authority"], False)
        self.assertIs(report["no_financial_side_effect"], True)
        self.assertEqual(report["funds"], funds)
        batch = report["metric_batch"]
        self.assertEqual((batch["workspace_id"], batch["tool_name"]), (self.w, "allocation_preview"))
        self.assertEqual(len(batch["service_signature"]), 64)
        self.service.validate(self.w, batch)
        self.assertEqual(len(self.observer.batches), previous + 1)
        self.assertEqual(self.observer.batches[-1], batch)
        self.assertEqual(report["metrics"], batch["metrics"])
        self.assertEqual(len(report["metrics"]), 12)
        package = self.packages.get("allocation_preview")
        for metric in batch["metrics"]:
            self.assertEqual(set(metric), self.claims_module.METRIC_FIELDS)
            self.assertEqual((metric["subject_id"], metric["currency"], metric["unit"], metric["data_scope"]),
                (self.w, "CNY", "minor_currency", funds["data_scope"]))
            self.assertIn(type(metric["value_minor"]), (int, type(None)))
            self.assertTrue(metric["basis"].startswith("planned_"))
            self.assertTrue(metric["formula_id"].startswith("allocation."))
            self.assertEqual((metric["as_of"], metric["period_start"], metric["period_end"]),
                (None, "2026-10-06", "2026-11-06"))
            for ref, version in funds["source_versions"].items():
                self.assertEqual(metric["source_versions"][ref], version)
            self.assertTrue(set(funds["input_refs"]) <= set(metric["input_refs"]))
            if package:
                self.assertTrue(set(package["input_refs"]) <= set(metric["input_refs"]))
                for ref, version in package["source_versions"].items():
                    self.assertEqual(metric["source_versions"][ref], version)
            if hasattr(self, "native_constraints"):
                self.assertTrue(set(self.native_constraints["input_refs"]) <= set(metric["input_refs"]))
                for ref, version in self.native_constraints["source_versions"].items():
                    self.assertEqual(metric["source_versions"][ref], version)
            kind = batch["metric_kinds"][metric["metric_id"]]
            self.assertEqual(kind, "planning_scenario")
            self.registry.register(metric, kind=kind, evidence_id="capability/" + self._testMethodName)
            registered = self.registry.lookup(metric["metric_id"])
            self.assertEqual((registered["metric"], registered["kind"]), (metric, kind))
        if package:
            self.assertTrue(any(row["in_transaction"] for row in self.provider.reads))
            self.assertTrue(all(row["records_sha256"] == digest(package["records"])
                and row["catalog_sha256"] == digest(package["catalog"]) for row in self.provider.reads))
        tampered = copy.deepcopy(batch)
        tampered["metrics"][0]["currency"] = "USD"
        with self.assertRaises(self.BankError) as caught:
            self.service.validate(self.w, tampered)
        self.assertEqual(caught.exception.code, "METRIC_TAMPERED")
        self.final_guard()
        after = self.bank_state()
        self.assertEqual(after, before)
        self.state_checks.append({"phase": "Gateway_HMAC_Observer_Registry_final", "before": before, "after": after})
        self.stages.update(gateway=True, hmac_validate=True, observer=True, registry=True,
            tampered_hmac_refused=True, zero_read_side_effect=True)
        self.assertEqual(self.network_attempts, [])
        return report

    def assert_plans(self, report, expected, occupancy_ref_mapping=False):
        plans = {row["plan_id"]: row for row in report["plans"]}
        wanted = list(expected.get("plans", [])) + [dict(row, plan_id=key) for key, row in expected.items()
            if key in ("retain_cash", "confirmed_uses")]
        for item in wanted:
            actual = plans[item["plan_id"]]
            for key in ("calculation_status", "feasibility", "execution_authority", "future_proceeds_minor",
                    "conservation_difference_minor", "cash_partition_difference_minor"):
                if key in item:
                    self.assertEqual(actual[key], item[key], key)
            for name, value in item["metrics"].items():
                metric = actual["metrics"][name]
                self.assertEqual(metric["metric_name"], name)
                self.assertEqual(metric["value_minor"], value, name)
                self.assertIs(type(metric["value_minor"]), type(value), name)
            for gap in item.get("coverage_gaps_contains", []):
                service_gap = gap
                if occupancy_ref_mapping:
                    mapping = DESIGN["fixture_contract"]["reference_mapping"]
                    self.assertEqual(gap, mapping["oracle_reuse_diagnostic"])
                    service_gap = mapping["service_reuse_diagnostic"]
                self.assertIn(service_gap, actual["coverage_gaps"])
        for plan in report["plans"]:
            self.assertIs(plan["execution_authority"], False)
            self.assertIsNone(plan["future_proceeds_minor"])

    def execute(self, action):
        method, passed, error = self._testMethodName, False, None
        try:
            action()
            self.assertEqual(self.network_attempts, [])
            passed = True
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            after = source_manifest()
            definition = METHODS[method]
            row = {"method": method, "case_id": definition["case_id"], "kind": definition["kind"],
                "assertions_passed": passed, "error": error, "source_bound_only": True,
                "sources_unchanged": after == self.before, "stages": self.stages,
                "paths": self.paths, "workspace_id": self.w,
                "private_key_origin": "temporary_generated_Fernet_key_not_logged",
                "observer_batches": len(self.observer.batches), "registry_count": len(self.registry.metric_ids),
                "expected_literal_sha256": digest(definition["expected_literal"]),
                "network_attempts": self.network_attempts, "cloud_model_calls": 0,
                "original_C07_C08_same_input_adapter_passed": False, "original_C16_adapter_passed": False,
                "formal_46_passed": False}
            self.observations.append(row)
            write_json(self.output / (method + "-evidence.json"), {**row,
                "source_packages": self.packages, "source_variants": self.source_variants,
                "source_reads": self.provider.reads,
                "funding_source_reads": self.funding_provider.reads if self.funding_provider else [],
                "real_goal_setup": self.goal_setup, "native_constraints": getattr(self, "native_constraints", None),
                "real_budget_baseline": getattr(self, "real_baseline", None), "budget_snapshot": self.funding,
                "requests": self.requests, "actual_reports": self.reports,
                "financial_state_checks": self.state_checks, "final_financial_state": self.bank_state(),
                "input_difference": DESIGN["fixture_contract"]["inherited_upstream_limit"]
                    if definition["case_id"].startswith(("C07", "C08")) else None,
                "original_and_service_diagnostic_mapping": DESIGN["fixture_contract"]["reference_mapping"]
                    if definition["case_id"].startswith("C08") else None})
            if after != self.before:
                raise RuntimeError("candidate_changed_during_case:" + method)

    def test_C06_unknown_budget(self):
        def action():
            self.normalize_bank(1000000)
            envelope = self.funding_envelope()
            self.assertIsNone(envelope["records"]["cash"]["balance_minor"])
            self.funding_provider = SqliteFundingSource(self.g.bank, self.w, envelope)
            self.g.finance_funding_sources = self.funding_provider
            funds = self.issue_budget(None, "unavailable", "missing_balance", "trusted_source")
            self.assertEqual(sum(row["balance_minor"] for row in self.bank_state()["accounts"]), 1000000)
            arguments = self.install_allocation("C06_unknown_budget", funds)
            self.assertIs(self.packages["allocation_preview"]["records"]["risk"]["confirmed"], True)
            report = self.issue(arguments, funds)
            self.assert_plans(report, CASES["C06_unknown_budget"]["expected"])
        self.execute(action)

    def test_C07_occupied_once(self):
        def action():
            goals = resolved_input("C07_occupied_once")["goals"]
            self.normalize_bank(300000, hold=True, goals=goals)
            self.confirm_goal(goals[0], bind_hold=True)
            funds = self.issue_budget(70000)
            self.assert_real_hold_baseline(funds, ["G"])
            report = self.issue({"scope": self.w, "budget_metric_id": funds["metric_id"],
                "constraints_ref": "goals/G", "product_evidence_refs": [], "expected_versions": {}}, funds)
            expected = CASES["C07_occupied_once"]["expected"]
            self.assert_plans(report, expected)
            allocation = next(row for row in report["plans"] if row["plan_id"] == "confirmed_uses")["allocations"]
            self.assertEqual(len(allocation), 1)
            self.assertEqual((allocation[0]["goal_id"], allocation[0]["amount_minor"], allocation[0]["already_occupied_minor"],
                allocation[0]["occupancy_status"]), ("G", expected["allocation_amount_minor"],
                expected["already_occupied_minor"], expected["occupancy_status"]))
        self.execute(action)

    def test_C08_reused_occupancy(self):
        def action():
            goals = resolved_input("C08_reused_occupancy")["goals"]
            self.normalize_bank(300000, hold=True, goals=goals)
            self.confirm_goal(goals[0], bind_hold=True)
            second = self.g.goals.state(self.w, "G2")["goal"]
            before = self.bank_state()
            with self.assertRaises(self.BankError) as caught:
                self.g.goals.prepare(self.w, {"action": "update", "goal_id": "G2", "name": second["name"],
                    "target_minor": 50000, "due_on": "2026-11-06", "priority": 1, "funding_kind": "cash",
                    "expected_version": second["object_version"], "idempotency_key": "capability-duplicate-G2",
                    "occupancy_hold_ids": ["hold-1"], "occupancy_account_id": self.main})
            self.assertEqual((caught.exception.code, caught.exception.status), ("GOAL_OCCUPANCY_REUSED", 409))
            self.assertEqual(self.bank_state(), before)
            self.confirm_goal(goals[1], bind_hold=False)
            funds = self.issue_budget(70000)
            constraints = self.assert_real_hold_baseline(funds, ["G1", "G2"])
            records = {"goals": copy.deepcopy(constraints["goals"]), "products": [], "risk": None}
            for row in records["goals"]:
                row["data_scope"] = funds["data_scope"]
            # 第二条是独立冻结的重复声明；真实无link、verified=false，不制造确认凭证。
            claim = records["goals"][1]
            claim.update(already_occupied_minor=30000, occupancy_refs=["fund_holds/hold-1"],
                occupancy_verified_in_baseline=False)
            claim["input_refs"] = [*claim["input_refs"], "fund_holds/hold-1"]
            claim["source_versions"]["fund_holds/hold-1"] = funds["source_versions"]["fund_holds/hold-1"]
            arguments = self.install_allocation("C08_reused_occupancy", funds, records=records,
                catalog_goal_ids=["G1", "G2"])
            report = self.issue(arguments, funds)
            self.assert_plans(report, CASES["C08_reused_occupancy"]["expected"], occupancy_ref_mapping=True)
            self.assertIn("goal_occupancy_unregistered:G2", report["reasons"])
            self.stages["original_hold_id_bound_to_exact_canonical_diagnostic"] = True
            self.stages["duplicate_real_link_refused_without_forging_two_links"] = True
        self.execute(action)

    def test_C14_simulated_risk_not_formal(self):
        def action():
            self.normalize_bank(1000000, outflow=300000)
            funds = self.issue_budget(500000)
            arguments = self.install_allocation("C14_simulated_risk_not_formal", funds)
            package = self.packages["allocation_preview"]
            self.assertEqual(package["records"]["risk_source"],
                {"configuration_source": "navren_synthetic", "not_real_risk_assessment": True})
            self.assertIs(package["records"]["risk"]["confirmed"], True)
            negative = self.issue(arguments, funds)
            self.assert_plans(negative, CASES["C14_simulated_risk_not_formal"]["expected"])
            self.assertEqual(negative["risk_qualification"]["classification"], "personal_simulation")
            self.assertIs(negative["risk_qualification"]["formal_qualification_verified"], False)
            self.assertIn("risk_source_not_formal", negative["risk_qualification"]["reasons"])
            self.assertIn("risk_source_not_formal", negative["reasons"])
            self.stages["simulation_risk_declined_without_forcing_risk_confirmed"] = True
            for field, value in (("risk_source", "formal"), ("risk_confirmed", True)):
                self.reject_dispatch({**arguments, field: value}, "METRIC_ARGUMENTS_INVALID", 422,
                    "client_self_claim_" + field + "_cannot_create_qualification")
            # 新正式来源对照明确是受控fixture；不声称来自真实银行风险服务。
            positive_package = copy.deepcopy(package)
            risk = positive_package["records"]["risk"]
            self.assertIs(risk["confirmed"], True)
            confirmation_ref = "capability:risk-subject-confirmation"
            risk["input_refs"] = [*risk["input_refs"], confirmation_ref]
            risk["source_versions"] = [[ref, "v1"] for ref in risk["input_refs"]]
            formal_source = copy.deepcopy(DESIGN["fixture_contract"]["risk_source_formal_fixture"])
            formal_source.update(subject_id=self.w, data_scope=funds["data_scope"], confirmed_by_subject_id=self.w)
            positive_package["records"]["risk_source"] = formal_source
            positive_package["input_refs"].append(confirmation_ref)
            positive_package["source_versions"][confirmation_ref] = "v1"
            positive_package["provenance"]["risk"].append(confirmation_ref)
            incomplete = copy.deepcopy(positive_package)
            incomplete["records"]["risk_source"] = {"configuration_source": "formal_assessment"}
            self.replace_allocation_source(incomplete, "formal_classification_without_original_contract")
            with self.assertRaises(self.BankError) as caught:
                self.service.validate(self.w, negative["metric_batch"])
            self.assertEqual(caught.exception.code, "METRIC_VERSION_CONFLICT")
            self.reset_observer()
            self.reject_dispatch(arguments, "METRIC_ARGUMENTS_INVALID", 422,
                "formal_classification_requires_assessment_and_confirmation_originals")
            mismatched = copy.deepcopy(positive_package)
            mismatched["records"]["risk_source"]["source_version"] = "v2"
            self.replace_allocation_source(mismatched, "formal_assessment_source_version_mismatch")
            self.reject_dispatch(arguments, "METRIC_VERSION_CONFLICT", 409,
                "formal_assessment_requires_actual_reference_version")
            self.replace_allocation_source(positive_package, "separate_explicit_synthetic_formal_source_control")
            self.provider.reads.clear()
            positive = self.issue(arguments, funds)
            self.assert_plans(positive, DESIGN["positive_control"]["expected_literal"])
            self.assertEqual(positive["risk_qualification"], {"classification": "formal_assessment",
                "reasons": [], "formal_qualification_verified": True})
            self.stages["separate_synthetic_formal_source_control"] = True
            self.stages["old_source_batch_cannot_publish_after_source_change"] = True
            self.stages["client_claim_and_unbound_formal_classification_cannot_create_qualification"] = True
        self.execute(action)

    def rejected(self, action, code, status):
        before = self.bank_state()
        with self.assertRaises(self.BankError) as caught:
            action()
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.reports.append({"actual_error_code": caught.exception.code, "actual_http_status": caught.exception.status})
        self.assertEqual(self.observer.batches, [])
        self.assertEqual(tuple(self.registry.metric_ids), ())
        self.assertEqual(len(self.service._funding_issuances), 0)  # 只读检查真实issuer，绝不写入。
        self.final_guard()
        after = self.bank_state()
        self.assertEqual(after, before)
        self.state_checks.append({"phase": "service_rejection_empty_registry_final", "before": before, "after": after})
        self.stages.update(real_service_entry_rejected=True, no_issued_budget=True,
            no_signed_result=True, no_amount_registered=True, zero_read_side_effect=True)

    def test_C16_source_basis_rejected_422(self):
        def action():
            self.normalize_bank(1000000)
            envelope = self.funding_envelope(balance=1000000, basis="account_balance")
            self.funding_provider = SqliteFundingSource(self.g.bank, self.w, envelope)
            self.g.finance_funding_sources = self.funding_provider
            self.requests.append({"name": "FinancialMetrics.snapshot", "days": 31,
                "source_cash_basis": "account_balance", "not_original_C16_planning_test": True})
            self.rejected(lambda: self.service.snapshot(self.w, 31), "METRIC_ARGUMENTS_INVALID", 422)
        self.execute(action)

    def test_C16_unissued_budget_rejected_409(self):
        def action():
            self.normalize_bank(1000000)
            arguments = {"scope": self.w, "budget_metric_id": "capability-unissued-budget",
                "constraints_ref": "goals/current", "product_evidence_refs": [], "expected_versions": {}}
            self.requests.append({"name": "allocation_preview", "arguments": arguments,
                "not_original_C16_planning_test": True})
            self.rejected(lambda: self.g.financial_method_report(self.w, "allocation_preview", arguments),
                "METRIC_BUDGET_NOT_ISSUED", 409)
        self.execute(action)
