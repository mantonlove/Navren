"""独立审计回归：一次生成基准，逐份复制注入错误，不复用生成器的汇总算法。"""
from __future__ import annotations

import hashlib
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = load_script("contest_finances_independent_audit", ROOT / "scripts/audit_contest_finances.py")
GENERATOR = load_script("contest_finances_fixture_generator", ROOT / "scripts/generate_contest_finances.py")


class ContestFinancesAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="navren-finances-audit-")
        cls.baseline = Path(cls.directory.name) / "baseline.sqlite3"
        GENERATOR.build_dataset(cls.baseline)
        cls.baseline_hash = hashlib.sha256(cls.baseline.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def mutated_report(self, mutate):
        path = Path(self.directory.name) / (self.id().split(".")[-1] + ".sqlite3")
        shutil.copy2(self.baseline, path)
        with closing(sqlite3.connect(path)) as conn, conn:
            # 禁用写入端外键，确保审计器而非 SQLite 拒绝归属/孤儿故障。
            conn.execute("PRAGMA foreign_keys=OFF")
            mutate(conn)
        return AUDIT.audit_database(path)

    def assert_failure(self, report, category, text=None):
        self.assertFalse(report["passed"])
        checks = [c for c in report["checks"] if c["category"] == category and not c["passed"]]
        self.assertTrue(checks, json.dumps(report, ensure_ascii=False))
        if text:
            self.assertTrue(any(text in s["message"] for c in checks for s in c["samples"]), checks)

    def test_full_ten_year_dataset_and_read_only_cli(self):
        report_file = Path(self.directory.name) / "cli-report.json"
        run = subprocess.run([sys.executable, str(ROOT / "scripts/audit_contest_finances.py"),
                              "--db", str(self.baseline), "--output", str(report_file)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(report_file.read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertEqual(report["uncovered_categories"], ["external_total"])
        self.assertEqual(report["row_counts"]["users"], 4)
        self.assertEqual(report["row_counts"]["daily_balances"], 14608)
        self.assertEqual(report["row_counts"]["monthly_summaries"], 484)
        self.assertEqual(report["row_counts"]["annual_summaries"], 44)
        self.assertEqual(hashlib.sha256(self.baseline.read_bytes()).hexdigest(), self.baseline_hash)

    def test_balanced_definition_cannot_hide_wrong_daily_snapshot(self):
        # 同时改现金、总资产、净资产使局部定义式仍成立；独立分录回放必须拒绝。
        def mutate(conn):
            conn.execute("UPDATE daily_balances SET cash_minor=cash_minor+100, "
                         "total_assets_minor=total_assets_minor+100, net_worth_minor=net_worth_minor+100 "
                         "WHERE rowid=(SELECT rowid FROM daily_balances LIMIT 1)")
        self.assert_failure(self.mutated_report(mutate), "identity", "独立分录回放")

    def test_unbalanced_journal_event(self):
        self.assert_failure(self.mutated_report(lambda c: c.execute(
            "UPDATE journal_lines SET debit_minor=debit_minor+1 WHERE line_id="
            "(SELECT line_id FROM journal_lines WHERE debit_minor>0 LIMIT 1)")),
            "identity", "事件借贷不平")

    def test_cash_leg_missing_or_duplicated(self):
        for fault in ("missing", "duplicated"):
            with self.subTest(fault=fault):
                def mutate(conn):
                    if fault == "missing":
                        conn.execute("DELETE FROM cash_transactions WHERE transaction_id="
                                     "(SELECT transaction_id FROM cash_transactions LIMIT 1)")
                    else:
                        columns = [r[1] for r in conn.execute("PRAGMA table_info(cash_transactions)")]
                        conn.execute("INSERT INTO cash_transactions SELECT 'audit_duplicate', " +
                                     ", ".join(columns[1:]) + " FROM cash_transactions LIMIT 1")
                self.assert_failure(self.mutated_report(mutate), "cross_total", "现金")

    def test_self_consistent_period_definition_cannot_hide_wrong_totals(self):
        # 同时改收入和结余，income-expense 恒等式仍成立，科目加总仍应捕获。
        self.assert_failure(self.mutated_report(lambda c: c.execute(
            "UPDATE monthly_summaries SET income_minor=income_minor+123, "
            "net_saving_minor=net_saving_minor+123 WHERE rowid="
            "(SELECT rowid FROM monthly_summaries LIMIT 1)")), "cross_total", "独立科目/现金加总")

    def test_negative_cash_between_events_cannot_hide_behind_day_end(self):
        def mutate(conn):
            account, user, opening, scope = conn.execute(
                "SELECT account_id,user_id,opening_balance_minor,scope FROM ledger_accounts "
                "WHERE account_class='cash' AND opening_balance_minor>0 LIMIT 1").fetchone()
            expense = conn.execute("SELECT account_id FROM ledger_accounts WHERE user_id=? "
                                   "AND account_class='expense' LIMIT 1", (user,)).fetchone()[0]
            amount = opening + 1
            for suffix, outgoing in (("withdraw", True), ("restore", False)):
                event = "audit_" + suffix
                occurred = "2016-10-04T00:00:0" + ("1" if outgoing else "2") + "+08:00"
                conn.execute("INSERT INTO journal_events VALUES(?,?,?,?,?,?,?,?,?)",
                             (event, user, occurred, "expense" if outgoing else "refund",
                              "audit_fault", scope, "审计故障", AUDIT.SOURCE, None))
                conn.executemany("INSERT INTO journal_lines VALUES(?,?,?,?,?,?)", [
                    (event + "_cash", event, user, account, 0 if outgoing else amount,
                     amount if outgoing else 0),
                    (event + "_expense", event, user, expense, amount if outgoing else 0,
                     0 if outgoing else amount)])
                conn.execute("INSERT INTO cash_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (event, user, occurred, account, "审计故障", amount,
                              -amount if outgoing else amount, "expense" if outgoing else "refund",
                              "audit_fault", scope, event, None, AUDIT.SOURCE))
            # 毛现金流增加，净支出和日末余额完全不变；退款同步增加。
            for table in ("monthly_summaries", "annual_summaries"):
                conn.execute(f"UPDATE {table} SET cash_inflow_minor=cash_inflow_minor+?, "
                             "cash_outflow_minor=cash_outflow_minor+?, refund_minor=refund_minor+? "
                             "WHERE user_id=? AND period_start='2016-10-04'",
                             (amount, amount, amount, user))
        report = self.mutated_report(mutate)
        self.assert_failure(report, "ranges", "事件后现金/资产科目为负")
        self.assertTrue(all(c["passed"] for c in report["checks"] if c["category"] in {"identity", "cross_total"}))

    def test_user_ownership_and_orphan_account(self):
        for fault in ("other_user", "orphan"):
            with self.subTest(fault=fault):
                def mutate(conn):
                    line, owner = conn.execute("SELECT line_id,user_id FROM journal_lines LIMIT 1").fetchone()
                    if fault == "other_user":
                        other = conn.execute("SELECT user_id FROM users WHERE user_id!=? LIMIT 1", (owner,)).fetchone()[0]
                        conn.execute("UPDATE journal_lines SET user_id=? WHERE line_id=?", (other, line))
                    else:
                        conn.execute("UPDATE journal_lines SET account_id='audit_unknown' WHERE line_id=?", (line,))
                self.assert_failure(self.mutated_report(mutate), "entity")

    def test_calendar_time_and_clipped_period_boundaries(self):
        mutations = [
            lambda c: c.execute("DELETE FROM daily_balances WHERE rowid=(SELECT rowid FROM daily_balances LIMIT 1)"),
            lambda c: c.execute("UPDATE journal_events SET occurred_at=replace(occurred_at,'+08:00','+00:00') "
                                "WHERE rowid=(SELECT rowid FROM journal_events LIMIT 1)"),
            lambda c: c.execute("UPDATE journal_events SET occurred_at='2026-10-04T12:00:00+08:00' "
                                "WHERE rowid=(SELECT rowid FROM journal_events LIMIT 1)"),
            lambda c: c.execute("UPDATE monthly_summaries SET period_start='2016-10-01' "
                                "WHERE rowid=(SELECT rowid FROM monthly_summaries WHERE period_start='2016-10-04' LIMIT 1)"),
            lambda c: c.execute("UPDATE annual_summaries SET partial_period=0 WHERE period_start='2016-10-04'")]
        for number, mutate in enumerate(mutations):
            with self.subTest(fault=number):
                self.assert_failure(self.mutated_report(mutate), "time_semantics")

    def test_required_values_integer_minor_units_and_sources(self):
        mutations = [
            ("value_coverage", lambda c: c.execute("UPDATE annual_summaries SET income_minor=NULL WHERE rowid="
                                                  "(SELECT rowid FROM annual_summaries LIMIT 1)")),
            ("value_coverage", lambda c: c.execute("UPDATE daily_balances SET cash_minor=cash_minor+0.5 WHERE rowid="
                                                  "(SELECT rowid FROM daily_balances LIMIT 1)")),
            ("spec", lambda c: c.execute("UPDATE cash_transactions SET source='real_bank' WHERE rowid="
                                         "(SELECT rowid FROM cash_transactions LIMIT 1)")),
            ("spec", lambda c: c.execute("UPDATE metadata SET value='true' WHERE key='is_real_product_return'"))]
        for category, mutate in mutations:
            with self.subTest(category=category):
                self.assert_failure(self.mutated_report(mutate), category)

    def test_duplicate_period_key_even_without_database_constraint(self):
        def mutate(conn):
            conn.execute("DROP INDEX annual_summary_key")
            conn.execute("INSERT INTO annual_summaries SELECT * FROM annual_summaries LIMIT 1")
        self.assert_failure(self.mutated_report(mutate), "duplicates", "重复主键")

    def test_asset_cost_identity_and_zero_position_history(self):
        def missing_zero_position(conn):
            user = conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO ledger_accounts VALUES(?,?,?,?,?,?,?,?)",
                         ("audit_zero_asset", user, "investment:audit_zero", "审计零持仓", "investment",
                          "personal", 0, AUDIT.SOURCE))
            conn.execute("INSERT INTO asset_values SELECT user_id,date,'audit_zero_asset',0,0,0 "
                         "FROM daily_balances WHERE user_id=?", (user,))
            conn.execute("DELETE FROM asset_values WHERE asset_id='audit_zero_asset' AND date='2016-10-04'")
        mutations = [
            lambda c: c.execute("UPDATE asset_values SET cost_basis_minor=cost_basis_minor+1 "
                                "WHERE rowid=(SELECT rowid FROM asset_values LIMIT 1)"),
            missing_zero_position,
            lambda c: c.execute("DELETE FROM loan_balances WHERE rowid=(SELECT rowid FROM loan_balances LIMIT 1)")]
        categories = ("identity", "cross_total", "cross_total")
        for number, (category, mutate) in enumerate(zip(categories, mutations)):
            with self.subTest(fault=number):
                self.assert_failure(self.mutated_report(mutate), category)

    def test_joint_cost_and_unrealized_tampering_cannot_hide_behind_identity(self):
        # 账面值保持不变，cost+pnl 也仍相等，成本来源回放应独立拒绝。
        self.assert_failure(self.mutated_report(lambda c: c.execute(
            "UPDATE asset_values SET cost_basis_minor=cost_basis_minor+100, "
            "unrealized_pnl_minor=unrealized_pnl_minor-100 WHERE rowid="
            "(SELECT rowid FROM asset_values LIMIT 1)")), "identity", "资产成本与独立事件回放")

    def test_missing_database_is_configuration_error_with_nonzero_exit(self):
        run = subprocess.run([sys.executable, str(ROOT / "scripts/audit_contest_finances.py"),
                              "--db", str(Path(self.directory.name) / "missing.sqlite3")],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertTrue(json.loads(run.stdout)["config_error"])
        self.assertFalse((Path(self.directory.name) / "missing.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
