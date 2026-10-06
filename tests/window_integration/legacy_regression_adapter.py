#!/usr/bin/env python3
"""Measure the original fixed regression runner's actual unittest result."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_RELATIVE_PATH = "scripts/run_financial_eval.py"
ORIGINAL_SHA256 = "6f1ec0592d5e0d90270f1e0af14ec8f3a91a1edf0e2a3dbe4e44b2b8a9b3663a"
FROZEN_DATASETS = {
    "evaluation/financial_eval_set.json": {
        "sha256": "e2b9ae5b4b6e70b73425b786e373f76235e049f6d80bd205c6734403c3157dd7",
        "minimum_tests": 15,
    },
    "evaluation/security_eval_set.json": {
        "sha256": "1c671f311125d94b4fe2fee454c9c669c26d58fe5125d6b5e263d3f44a10ce7a",
        "minimum_tests": 26,
    },
}
COUNT_FIELDS = (
    "tests_run", "failures", "errors", "skipped", "expected_failures",
    "unexpected_successes",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def references_from_dataset(path: Path) -> tuple[str, ...]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset_cases_missing")
    references = []
    for case in cases:
        tests = case["tests"]
        if not isinstance(tests, list) or not tests:
            raise ValueError("dataset_case_references_missing")
        for reference in tests:
            if not isinstance(reference, str) or len(reference.split(".", 2)) != 3:
                raise ValueError("invalid_test_reference")
            if not all(reference.split(".", 2)):
                raise ValueError("invalid_test_reference")
            references.append(reference)
    return tuple(dict.fromkeys(references))


def load_original_runner(root: Path = ROOT):
    source = root / ORIGINAL_RELATIVE_PATH
    if sha256(source) != ORIGINAL_SHA256:
        raise ValueError("original_runner_sha_mismatch")
    spec = importlib.util.spec_from_file_location("_navren_original_fixed_regression", source)
    if spec is None or spec.loader is None:
        raise ValueError("original_runner_import_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_adapter(legacy, dataset: Path, out_dir: Path, *,
                frozen_dataset_sha: str, frozen_references: tuple[str, ...]) -> dict:
    """Invoke legacy.run once, retaining its report and measuring real results.

    The CLI supplies SHA-verified original dataset references. The narrow harness
    supplies its own frozen synthetic references and patches only resolve_test.
    """
    dataset = dataset.resolve()
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise ValueError("out_dir_exists_refuse_overwrite")
    source = Path(legacy.__file__).resolve()
    source_before = sha256(source)
    dataset_before = sha256(dataset)
    if source_before != ORIGINAL_SHA256:
        raise ValueError("original_runner_sha_mismatch")
    if dataset_before != frozen_dataset_sha:
        raise ValueError("dataset_sha_mismatch")
    references = references_from_dataset(dataset)
    if not frozen_references or references != frozen_references:
        raise ValueError("frozen_references_mismatch")
    out_dir.mkdir(parents=True, exist_ok=False)
    legacy_output = out_dir / "legacy-scene-results.json"
    captures = []
    run_calls = 0
    runner_class = legacy.unittest.TextTestRunner

    class CapturingTextTestRunner(runner_class):
        def run(self, suite):
            nonlocal run_calls
            run_calls += 1
            suite_count = suite.countTestCases()
            result = super().run(suite)
            captures.append((result, suite_count))
            return result

    legacy_return_code = None
    legacy_exception = None
    original_sys_path = list(sys.path)
    try:
        with patch.object(legacy.unittest, "TextTestRunner", CapturingTextTestRunner):
            legacy_return_code = legacy.run(dataset, legacy_output)
    except Exception as exc:
        legacy_exception = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        sys.path[:] = original_sys_path

    counts = dict.fromkeys(COUNT_FIELDS)
    issues = []
    suite_count = None
    was_successful = None
    if run_calls != 1 or len(captures) != 1:
        issues.append("actual_result_capture_count_not_one")
    else:
        result, suite_count = captures[0]
        # All counts come from the returned standard TestResult, never scenes.
        counts.update(
            tests_run=result.testsRun,
            failures=len(result.failures),
            errors=len(result.errors),
            skipped=len(result.skipped),
            expected_failures=len(result.expectedFailures),
            unexpected_successes=len(result.unexpectedSuccesses),
        )
        was_successful = result.wasSuccessful()
        if not all(type(value) is int and value >= 0 for value in counts.values()):
            issues.append("actual_standard_counts_invalid")
        else:
            if counts["tests_run"] < len(frozen_references):
                issues.append("actual_test_count_below_frozen_minimum")
            if any(counts[field] != 0 for field in COUNT_FIELDS[1:]):
                issues.append("actual_failure_skip_or_expected_counter_nonzero")
        if was_successful is not True:
            issues.append("actual_result_not_successful")
    if type(legacy_return_code) is not int or legacy_return_code != 0:
        issues.append("original_run_return_not_zero")
    if legacy_exception is not None:
        issues.append("original_run_raised")
    source_after = sha256(source) if source.is_file() else None
    dataset_after = sha256(dataset) if dataset.is_file() else None
    if source_after != source_before or dataset_after != dataset_before:
        issues.append("frozen_inputs_changed")
    if not legacy_output.is_file():
        issues.append("original_scene_report_missing")
    complete = not issues
    report = {
        "schema": "navren.legacy-regression-test-result/1",
        "measurement_source": "actual_unittest_TestResult",
        "dataset_path": str(dataset),
        "dataset_sha256_before": dataset_before,
        "dataset_sha256_after": dataset_after,
        "original_runner_path": str(source),
        "original_runner_sha256_before": source_before,
        "original_runner_sha256_after": source_after,
        "frozen_unique_references": list(frozen_references),
        "minimum_tests": len(frozen_references),
        "suite_count_before_run": suite_count,
        "original_run_call_count": 1,
        "text_runner_run_call_count": run_calls,
        "actual_result_capture_count": len(captures),
        **counts,
        "was_successful": was_successful,
        "legacy_return_code": legacy_return_code,
        "legacy_exception": legacy_exception,
        "legacy_scene_report_path": str(legacy_output),
        "legacy_scene_report_sha256": sha256(legacy_output) if legacy_output.is_file() else None,
        "contract_issues": issues,
        "complete": complete,
        "planned_exit_code": 0 if complete else 1,
    }
    (out_dir / "test-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def integration_output_root() -> Path:
    parts = ROOT.parts
    for index in range(len(parts) - 2):
        if parts[index:index + 3] == ("outputs", "window-progress", "integration"):
            return Path(*parts[:index + 3])
    return ROOT / "outputs/window-progress/integration"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        dataset = args.dataset.resolve()
        relative = dataset.relative_to(ROOT.resolve()).as_posix()
        frozen = FROZEN_DATASETS.get(relative)
        if frozen is None:
            raise ValueError("dataset_not_in_frozen_allowlist")
        if sha256(dataset) != frozen["sha256"]:
            raise ValueError("dataset_sha_mismatch")
        references = references_from_dataset(dataset)
        if len(references) != frozen["minimum_tests"]:
            raise ValueError("frozen_unique_reference_count_mismatch")
        out_dir = args.out_dir.resolve()
        if not out_dir.is_relative_to(integration_output_root().resolve()):
            raise ValueError("out_dir_outside_integration_scope")
        if out_dir.exists():
            raise ValueError("out_dir_exists_refuse_overwrite")
        legacy = load_original_runner()
        report = run_adapter(legacy, dataset, out_dir,
                             frozen_dataset_sha=frozen["sha256"],
                             frozen_references=references)
        print(json.dumps({"counts_path": str(out_dir / "test-result.json"),
                          "complete": report["complete"],
                          "planned_exit_code": report["planned_exit_code"]}, ensure_ascii=False))
        return report["planned_exit_code"]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"回归计数适配器拒绝：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
