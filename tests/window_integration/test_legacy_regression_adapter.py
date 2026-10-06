"""Four controlled cases for real legacy-runner TestResult measurement only."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("_legacy_regression_adapter", HERE / "legacy_regression_adapter.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class LegacyRegressionAdapterTest(unittest.TestCase):
    def measure(self, references, resolver, *, scenes=None):
        # The actual original run is invoked; only test resolution is synthetic.
        legacy = adapter.load_original_runner()
        rows = scenes if scenes is not None else [references]
        dataset = {"dataset_id": "controlled-stdlib-unittest", "version": 1,
                   "environment": "synthetic_harness", "limitations": ["No business test references executed"],
                   "cases": [{"id": f"S{index}", "scenario": "controlled result measurement",
                              "expected_invariant": "actual TestResult counts",
                              "tests": row} for index, row in enumerate(rows)]}
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            path = base / "dataset.json"
            path.write_text(json.dumps(dataset) + "\n", encoding="utf-8")
            frozen_sha = adapter.sha256(path)
            unique = tuple(dict.fromkeys(reference for row in rows for reference in row))
            with patch.object(legacy, "resolve_test", side_effect=resolver) as resolved, \
                    contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(legacy, "run", wraps=legacy.run) as original_run:
                report = adapter.run_adapter(legacy, path, base / "results",
                                             frozen_dataset_sha=frozen_sha,
                                             frozen_references=unique)
            raw_path = base / "results/legacy-scene-results.json"
            raw_bytes = raw_path.read_bytes()
            raw = json.loads(raw_bytes)
            saved = json.loads((base / "results/test-result.json").read_text())
            self.assertEqual(saved, report)
            self.assertEqual(adapter.sha256(raw_path), report["legacy_scene_report_sha256"])
            self.assertEqual(adapter.sha256(path), frozen_sha)
            self.assertEqual(report["original_runner_sha256_before"], adapter.ORIGINAL_SHA256)
            self.assertEqual(report["original_runner_sha256_after"], adapter.ORIGINAL_SHA256)
            original_run.assert_called_once_with(path, raw_path)
            self.assertEqual(report["text_runner_run_call_count"], 1)
            self.assertEqual(report["actual_result_capture_count"], 1)
            self.assertIn(f"Ran {report['tests_run']} test", raw["runner_output"])
            self.assertEqual(raw["summary"]["test_count"], report["tests_run"])
            for field in adapter.COUNT_FIELDS:
                self.assertIs(type(report[field]), int)
            return report, raw, resolved.call_count

    def test_actual_pass_uses_real_result(self):
        class Passing(unittest.TestCase):
            def test_case(self):
                self.assertEqual(7 * 6, 42)

        report, raw, calls = self.measure(["fixture.Passing.test_case"], lambda ref: Passing("test_case"))
        self.assertEqual(calls, 1)
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(report["minimum_tests"], 1)
        self.assertEqual([report[field] for field in adapter.COUNT_FIELDS[1:]], [0] * 5)
        self.assertEqual(report["legacy_return_code"], 0)
        self.assertEqual(report["planned_exit_code"], 0)
        self.assertTrue(report["complete"])
        self.assertEqual(raw["cases"][0]["status"], "passed")

    def test_real_skip_rejects_legacy_zero_exit(self):
        class Skipping(unittest.TestCase):
            @unittest.skip("controlled real skip")
            def test_case(self):
                self.fail("skip must prevent execution")

        report, raw, calls = self.measure(["fixture.Skipping.test_case"], lambda ref: Skipping("test_case"))
        self.assertEqual(calls, 1)
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["legacy_return_code"], 0)
        self.assertTrue(report["was_successful"])
        self.assertEqual(raw["cases"][0]["status"], "passed")
        self.assertEqual(raw["summary"]["failed_test_count"], 0)
        self.assertIn("actual_failure_skip_or_expected_counter_nonzero", report["contract_issues"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["planned_exit_code"], 1)

    def test_set_up_class_error_and_missing_actual_count(self):
        class BrokenSetup(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("controlled setup error before two tests")

            def test_one(self):
                self.fail("setUpClass should prevent execution")

            def test_two(self):
                self.fail("setUpClass should prevent execution")

        refs = ["fixture.BrokenSetup.test_one", "fixture.BrokenSetup.test_two"]
        report, raw, calls = self.measure(refs, lambda ref: BrokenSetup(ref.rsplit(".", 1)[1]))
        self.assertEqual(calls, 2)
        self.assertEqual(report["suite_count_before_run"], 2)
        self.assertEqual(report["minimum_tests"], 2)
        self.assertEqual(report["tests_run"], 0)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["legacy_return_code"], 1)
        self.assertEqual(raw["cases"][0]["status"], "passed")
        self.assertIn("actual_test_count_below_frozen_minimum", report["contract_issues"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["planned_exit_code"], 1)

    def test_duplicate_references_count_once(self):
        executed = []

        class Once(unittest.TestCase):
            def test_case(self):
                executed.append("actual_execution")

        reference = "fixture.Once.test_case"
        report, raw, calls = self.measure([reference], lambda ref: Once("test_case"),
                                          scenes=[[reference, reference], [reference]])
        self.assertEqual(calls, 1)
        self.assertEqual(executed, ["actual_execution"])
        self.assertEqual(report["frozen_unique_references"], [reference])
        self.assertEqual(report["minimum_tests"], 1)
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(raw["summary"]["case_count"], 2)
        self.assertEqual(raw["summary"]["test_count"], 1)
        self.assertEqual(report["legacy_return_code"], 0)
        self.assertEqual(report["planned_exit_code"], 0)
        self.assertTrue(report["complete"])


if __name__ == "__main__":
    unittest.main()
