"""Independent arithmetic, provenance guards and real-HTTP base verification."""
from copy import deepcopy
import unittest

from financial_metrics import fingerprint
from scripts.verify_base_runtime import temporary_runtime, recompute_metrics, verify_runtime
from simulated_bank import BankError


class BaseMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = temporary_runtime(initialize=False)
        cls.server, cls.base, _ = cls.runtime.__enter__()
        cls.gateway = cls.server.finance

    @classmethod
    def tearDownClass(cls):
        cls.runtime.__exit__(None, None, None)

    def setUp(self):
        self.workspace, _ = self.gateway.create_session()

    def test_snapshot_matches_independent_integer_cashflow_and_pure_methods(self):
        before = self.gateway.overview(self.workspace)
        snapshot = self.gateway.financial_metrics.snapshot(self.workspace, 60)
        recomputed = recompute_metrics(self.gateway, self.workspace)
        actual = {key: metric["value_minor"] for key, metric in snapshot["funding"]["metrics"].items()}
        self.assertEqual(actual, recomputed["manual"])
        self.assertEqual(actual, recomputed["financial_methods"])
        self.gateway.financial_metrics.validate(self.workspace, snapshot)
        self.assertEqual(snapshot["snapshot_id"], "base-metrics-" + fingerprint({k: v for k, v in snapshot.items() if k != "snapshot_id"}))
        for metric in snapshot["metrics"]:
            self.assertEqual(metric["subject_id"], self.workspace)
            self.assertTrue(metric["input_refs"])
            self.assertTrue(metric["source_versions"])
            self.assertTrue(metric["formula_id"])
            self.assertEqual(metric["currency"], "CNY")
        after = self.gateway.overview(self.workspace)
        for key in ("accounts", "holdings", "orders", "profile", "fund_holds"):
            self.assertEqual(before[key], after[key])

    def test_changed_amount_missing_identity_and_stale_personal_version_rejected(self):
        metrics = self.gateway.financial_metrics
        snapshot = metrics.snapshot(self.workspace, 60)
        tampered = deepcopy(snapshot)
        tampered["funding"]["metrics"]["cash_balance"]["value_minor"] += 1
        with self.assertRaises(BankError) as caught:
            metrics.validate(self.workspace, tampered)
        self.assertEqual(caught.exception.code, "METRIC_TAMPERED")
        other, _ = self.gateway.create_session()
        with self.assertRaises(BankError) as caught:
            metrics.validate(other, snapshot)
        self.assertEqual(caught.exception.code, "METRIC_SUBJECT_MISMATCH")
        self.gateway.memories.create(self.workspace, {"category": "goal", "value": "毕业前储蓄目标",
            "expected_revision": self.gateway.personal_data.revision(self.workspace)})
        with self.assertRaises(BankError) as caught:
            metrics.validate(self.workspace, snapshot)
        self.assertEqual(caught.exception.code, "METRIC_VERSION_CONFLICT")

    def test_invalid_horizon_never_becomes_a_zero_projection(self):
        for days in (0, 366, True, "60", None):
            with self.subTest(days=days), self.assertRaises(BankError) as caught:
                self.gateway.financial_metrics.snapshot(self.workspace, days)
            self.assertEqual(caught.exception.code, "INVALID_HORIZON")


class BaseRuntimeHTTPTests(unittest.TestCase):
    def test_four_account_runtime_identity_and_confirmation_boundaries(self):
        result = verify_runtime()
        failures = [row for row in result["checks"] if not row["passed"]]
        self.assertTrue(result["passed"], failures)
        self.assertGreaterEqual(len(result["checks"]), 11)
        self.assertTrue(result["temporary_server_closed"])


if __name__ == "__main__":
    unittest.main()
