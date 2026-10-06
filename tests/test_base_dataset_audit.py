"""Corruption controls for the independent base audit (offline source copies)."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs/contest-finances"
SPEC = importlib.util.spec_from_file_location("audit_base_dataset", ROOT / "scripts/audit_base_dataset.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class BaseDatasetAuditTests(unittest.TestCase):
    """Change actual snapshot facts and neutralize its manifest to isolate checks.

    Never update the source tree. This is deliberately not a fixture generated
    by the same accounting formulas as the auditor.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="navren-independent-audit-")
        self.addCleanup(self.temp.cleanup)
        self.copy = Path(self.temp.name) / "source"
        self.copy.mkdir()
        for name in ("financial_data.sqlite3", "summary.json", "manifest.json", "data_contract.json"):
            shutil.copyfile(SOURCE / name, self.copy / name)
        for uid in AUDIT.USERS:
            (self.copy / uid).mkdir()
            shutil.copyfile(SOURCE / uid / "current_finances.json", self.copy / uid / "current_finances.json")

    def mutate_sql(self, statement, parameters=()):
        with closing(sqlite3.connect(self.copy / "financial_data.sqlite3")) as db:
            db.execute(statement, parameters)
            db.commit()
        self.rehash_manifest()

    def rehash_manifest(self):
        path = self.copy / "manifest.json"
        manifest = json.loads(path.read_text())
        for record in manifest["files"]:
            source = self.copy / record["path"]
            if source.is_file():
                record["sha256"] = digest(source)
                record["bytes"] = source.stat().st_size
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def assert_detected(self, check):
        report = AUDIT.audit_dataset(self.copy)
        self.assertFalse(report["passed"])
        records = {r["id"]: r for r in report["checks"]}
        self.assertIn(check, records)
        self.assertFalse(records[check]["passed"], records.get("audit_execution"))
        self.assertGreater(records[check]["failure_count"], 0)
        # Arithmetic controls must fail even after replacement digests match.
        self.assertTrue(records["manifest_hashes"]["passed"])
        return report

    def test_original_snapshot_passes_and_source_is_unchanged(self):
        before = {name: digest(self.copy / name) for name in (
            "financial_data.sqlite3", "tycoon/current_finances.json", "summary.json")}
        report = AUDIT.audit_dataset(self.copy)
        self.assertTrue(report["passed"], [c for c in report["checks"] if not c["passed"]])
        self.assertEqual(report["counts"]["cash_transactions"], 22977)
        self.assertEqual(set(report["accounts"]), set(AUDIT.USERS))
        self.assertEqual(report["accounts"]["tycoon"]["closing_balances_minor"]["net_worth_minor"], 262520039660)
        self.assertEqual(len(report["accounts"]["grain_farmer"]["partial_periods"]["annual_summaries"]), 2)
        self.assertIn("fund_units_and_real_nav_linkage", {r["id"] for r in report["uncovered"]})
        self.assertEqual(before, {name: digest(self.copy / name) for name in before})

    def test_balanced_but_wrong_cash_leg_is_detected(self):
        self.mutate_sql("UPDATE cash_transactions SET amount_minor=amount_minor+100,signed_minor=signed_minor-100 WHERE transaction_id=(SELECT transaction_id FROM cash_transactions WHERE kind='expense' LIMIT 1)")
        self.assert_detected("cash_legs")

    def test_unbalanced_journal_is_detected(self):
        self.mutate_sql("UPDATE journal_lines SET debit_minor=debit_minor+1 WHERE line_id=(SELECT line_id FROM journal_lines WHERE debit_minor>0 LIMIT 1)")
        self.assert_detected("balanced_events")

    def test_wrong_closing_snapshot_is_detected(self):
        self.mutate_sql("UPDATE daily_balances SET cash_minor=cash_minor+1,total_assets_minor=total_assets_minor+1,net_worth_minor=net_worth_minor+1 WHERE user_id='tycoon' AND date='2026-10-03'")
        self.assert_detected("daily_balance_replay")

    def test_missing_day_is_detected(self):
        self.mutate_sql("DELETE FROM daily_balances WHERE user_id='working_landlady' AND date='2020-02-29'")
        self.assert_detected("calendar_coverage")

    def test_partial_year_cannot_be_relabeled_full_year(self):
        self.mutate_sql("UPDATE annual_summaries SET partial_period=0 WHERE user_id='tycoon' AND period_start='2016-10-04'")
        self.assert_detected("period_calendar_semantics")

    def test_investment_principal_cannot_be_added_to_income(self):
        self.mutate_sql("UPDATE monthly_summaries SET income_minor=income_minor+10000,net_saving_minor=net_saving_minor+10000 WHERE user_id='tycoon' AND period_start='2016-10-04'")
        self.assert_detected("independent_period_summaries")

    def test_cost_and_pnl_consistent_but_wrong_cost_is_detected(self):
        self.mutate_sql("UPDATE asset_values SET cost_basis_minor=cost_basis_minor+100,unrealized_pnl_minor=unrealized_pnl_minor-100 WHERE user_id='grain_farmer' AND date='2026-10-03' AND asset_id='grain_farmer::asset:GF-FB'")
        self.assert_detected("asset_value_and_cost_replay")

    def test_refund_cannot_reference_another_income_event(self):
        self.mutate_sql("UPDATE journal_events SET reference_id=(SELECT event_id FROM journal_events WHERE user_id='tycoon' AND event_type='income' LIMIT 1) WHERE event_id=(SELECT event_id FROM journal_events WHERE user_id='tycoon' AND event_type='refund' LIMIT 1)")
        self.assert_detected("refund_reference_and_netting")

    def test_transfer_pair_identity_is_required(self):
        self.mutate_sql("UPDATE cash_transactions SET transfer_group_id='invented-pair' WHERE transaction_id=(SELECT transaction_id FROM cash_transactions WHERE kind='internal_transfer' LIMIT 1)")
        self.assert_detected("transfer_pairing")

    def test_cross_user_link_is_detected(self):
        self.mutate_sql("UPDATE cash_transactions SET user_id='grain_farmer' WHERE transaction_id=(SELECT transaction_id FROM cash_transactions WHERE user_id='tycoon' LIMIT 1)")
        self.assert_detected("user_ownership")

    def test_current_json_cost_and_source_changes_are_detected(self):
        path = self.copy / "retired_entrepreneur/current_finances.json"
        current = json.loads(path.read_text())
        current["assets"][0]["cost_basis_minor"] += 1
        current["assets"][0]["source"] = "real_bank"
        path.write_text(json.dumps(current), encoding="utf-8")
        self.rehash_manifest()
        self.assert_detected("current_holdings_and_provenance")

    def test_cli_reports_failure_and_cannot_overwrite_sources(self):
        self.mutate_sql("UPDATE recurring_obligations SET status='paid' WHERE obligation_id=(SELECT obligation_id FROM recurring_obligations LIMIT 1)")
        target = Path(self.temp.name) / "audit.json"
        process = subprocess.run([sys.executable, str(ROOT / "scripts/audit_base_dataset.py"),
            "--source-dir", str(self.copy), "--output", str(target)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertFalse(json.loads(target.read_text())["passed"])
        protected = subprocess.run([sys.executable, str(ROOT / "scripts/audit_base_dataset.py"),
            "--source-dir", str(self.copy), "--output", str(self.copy / "summary.json")], capture_output=True, text=True)
        self.assertEqual(protected.returncode, 2)
        self.assertIn("outside the read-only source directory", protected.stderr)


if __name__ == "__main__":
    unittest.main()
