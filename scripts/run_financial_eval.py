#!/usr/bin/env python3
"""Run Navren's fixed synthetic financial regression set and write a JSON report."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
DEFAULT_SET = ROOT / "evaluation" / "financial_eval_set.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "latest.json"


def resolve_test(reference: str) -> unittest.TestCase:
    module_name, class_name, method_name = reference.split(".", 2)
    module = importlib.import_module(module_name)
    case_class = getattr(module, class_name)
    case = case_class(method_name)
    if not isinstance(case, unittest.TestCase):
        raise TypeError(f"Not a unittest case: {reference}")
    return case


def run(dataset_path: Path, output_path: Path) -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(TESTS))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    references = list(dict.fromkeys(reference for case in cases for reference in case["tests"]))
    test_cases = {reference: resolve_test(reference) for reference in references}
    suite = unittest.TestSuite(test_cases.values())
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)

    failed = {
        test.id()
        for test, _ in [*result.failures, *result.errors]
    }
    outcomes = []
    for case in cases:
        case_failures = [reference for reference in case["tests"] if reference in failed]
        outcomes.append({
            "id": case["id"],
            "scenario": case["scenario"],
            "status": "failed" if case_failures else "passed",
            "expected_invariant": case["expected_invariant"],
            "tests": case["tests"],
            "failed_tests": case_failures,
        })

    report = {
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": dataset["environment"],
        "limitations": dataset["limitations"],
        "summary": {
            "case_count": len(outcomes),
            "passed_cases": sum(row["status"] == "passed" for row in outcomes),
            "failed_cases": sum(row["status"] == "failed" for row in outcomes),
            "test_count": result.testsRun,
            "failed_test_count": len(failed),
        },
        "cases": outcomes,
        "runner_output": stream.getvalue().strip(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"固定评测：{report['summary']['passed_cases']}/{report['summary']['case_count']} 项场景通过；"
        f"{result.testsRun} 个唯一回归测试，失败 {len(failed)} 项。\n结果：{output_path}"
    )
    if failed:
        print(stream.getvalue())
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_SET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return run(args.dataset.resolve(), args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
