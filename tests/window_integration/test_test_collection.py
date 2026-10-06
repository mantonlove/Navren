"""测试收集门独立反例：真实临时小测试子进程，绝无服务/云/现役库。

预期在编写后、首次执行前冻结：正常1项exit0；skip1项exit1；0项低于
minimum=1时exit1；缺文件非零；expectedFailure1项exit1。cloud无handoff
正常exit2，最终核验失败必须实际exit1且与run.json一致，均不得执行云任务。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/window_integration/run_unittest.py"
EXECUTOR = ROOT / "scripts/run_window_integration.py"
THIS_TEST = Path(__file__).resolve()
INPUTS = (RUNNER, EXECUTOR, THIS_TEST)


def manifest():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in INPUTS}


class TestCollectionContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = manifest()
        # 冻结副本不复制.venv；统一执行器传来的解释器仍应是项目的.venv。
        cls.python = Path(sys.executable).absolute()
        if cls.python.parent.parent.name != ".venv":
            raise RuntimeError("project_venv_python_required:" + str(cls.python))
        cls.observations = []

    @classmethod
    def tearDownClass(cls):
        after = manifest()
        print("WINDOW_COLLECTION_SUMMARY " + json.dumps({
            "source_sha256_before": cls.before, "source_sha256_after": after,
            "source_unchanged": cls.before == after, "cloud_calls": 0,
            "server_started": False, "observations": cls.observations,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        if after != cls.before:
            raise RuntimeError("collection_inputs_changed_during_test")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="navren-collection-check-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.test_directory = self.directory / "tests"
        self.test_directory.mkdir()
        self.environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONIOENCODING": "utf-8"}

    def child(self, arguments):
        return subprocess.run([str(self.python), "-B", *map(str, arguments)],
                              cwd=self.directory, env=self.environment,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=15, check=False)

    def record(self, case, process, counts=None, **values):
        observation = {"case": case, "actual_exit_code": process.returncode,
                       "structured_counts": counts, "stdout": process.stdout,
                       "stderr": process.stderr, **values}
        self.observations.append(observation)
        print("WINDOW_COLLECTION_EVIDENCE " + json.dumps(
            observation, ensure_ascii=False, sort_keys=True), flush=True)

    def run_fixture(self, case, source, minimum=1):
        pattern = "test_fixture.py"
        if source is not None:
            (self.test_directory / pattern).write_text(source, encoding="utf-8")
        result_path = self.directory / "test-result.json"
        process = self.child([RUNNER, "--start-dir", self.test_directory,
                              "--pattern", pattern, "--minimum-tests", minimum,
                              "--output", result_path])
        counts = json.loads(result_path.read_text()) if result_path.is_file() else None
        self.record(case, process, counts)
        return process, counts

    def counts(self, counts, *, tests, skipped=0, expected_failures=0, complete=False):
        self.assertIsInstance(counts, dict, "实际structured计数必须存在")
        expected = {"tests_run": tests, "failures": 0, "errors": 0,
                    "skipped": skipped, "expected_failures": expected_failures,
                    "unexpected_successes": 0, "minimum_tests": 1,
                    "complete": complete, "planned_exit_code": 0 if complete else 1}
        for key, value in expected.items():
            self.assertEqual(counts[key], value, key)

    def test_normal_file_completes_with_actual_exit_zero(self):
        process, counts = self.run_fixture("COL01-normal", """
import unittest
class Normal(unittest.TestCase):
    def test_one(self):
        self.assertEqual(2 + 3, 5)
""")
        self.assertEqual(process.returncode, 0)
        self.counts(counts, tests=1, complete=True)

    def test_skip_is_incomplete_even_when_unittest_succeeds(self):
        process, counts = self.run_fixture("COL02-skip", """
import unittest
class MissingInterface(unittest.TestCase):
    @unittest.skip("synthetic required interface unavailable")
    def test_one(self):
        self.fail("this test must genuinely skip")
""")
        self.assertEqual(process.returncode, 1)
        self.assertIn("OK (skipped=1)", process.stderr)
        self.counts(counts, tests=1, skipped=1)
        self.assertEqual(len(counts["skipped_cases"]), 1)
        self.assertEqual(counts["skipped_cases"][0]["reason"],
                         "synthetic required interface unavailable")

    def test_zero_cases_cannot_satisfy_minimum(self):
        process, counts = self.run_fixture("COL03-zero", "VALUE = 1\n")
        self.assertEqual(process.returncode, 1)
        self.assertIn("Ran 0 tests", process.stderr)
        self.counts(counts, tests=0)

    def test_missing_file_is_a_real_failure_without_fabricated_counts(self):
        process, counts = self.run_fixture("COL04-missing", None)
        self.assertNotEqual(process.returncode, 0)
        self.assertIsNone(counts, "启动拒绝没有计数文件，不得伪造已运行0项")
        self.assertIn("必须指定存在的测试文件", process.stderr)

    def test_expected_failure_cannot_be_a_complete_gate(self):
        process, counts = self.run_fixture("COL05-expected-failure", """
import unittest
class KnownDefect(unittest.TestCase):
    @unittest.expectedFailure
    def test_one(self):
        self.assertEqual(2 + 3, 6)
""")
        self.assertEqual(process.returncode, 1)
        self.assertIn("OK (expected failures=1)", process.stderr)
        self.counts(counts, tests=1, expected_failures=1)

    def test_no_handoff_final_check_controls_actual_process_exit(self):
        code = r'''
import importlib.util,json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
script,source,output,fault=map(Path,sys.argv[1:])
spec=importlib.util.spec_from_file_location("collection_executor_probe",script)
executor=importlib.util.module_from_spec(spec);spec.loader.exec_module(executor)
calls=[]
def baseline(*args):
    calls.append("synthetic")
    if len(calls)==2 and str(fault)=="postflight":
        raise RuntimeError("synthetic_postflight_failure")
    return {"bank_state":{"synthetic":True}}
args=[str(script),"--stage","cloud","--source-root",str(source),
      "--out-dir",str(output/"run"),"--budget-seconds","10",
      "--stage-timeout-seconds","2","--rounds","1"]
with patch.object(executor,"ROOT",source), patch.object(executor,"OUTPUT_ROOT",output), \
     patch.object(executor,"baseline",side_effect=baseline), \
     patch.object(executor,"isolated_environment",return_value={"PATH":os.defpath}), \
     patch.object(executor,"execute",side_effect=AssertionError("cloud_execute_forbidden")) as launch, \
     patch.object(socket,"create_connection",side_effect=AssertionError("network_forbidden")), \
     patch.object(sys,"argv",args):
    result=executor.main()
    launch.assert_not_called()
report=json.loads((output/"run/run.json").read_text())
print(json.dumps({"returned_code":result,"baseline_calls":len(calls),
                  "status":report["status"],"planned_exit_code":report["planned_exit_code"],
                  "executions":report["executions"],"rounds_executed":report["rounds_executed"],
                  "cloud_models_called":report["cloud_models_called"],
                  "handoff_gate":report["handoff_gate"],
                  "candidate_source_matches_frozen":report["candidate_source_matches_frozen"],
                  "candidate_data_matches_frozen":report["candidate_data_matches_frozen"],
                  "postflight_error_type":report.get("postflight_error_type")},sort_keys=True))
sys.exit(result)
'''
        for fault, expected_exit, expected_status in (
                ("none", 2, "waiting_for_module_handoff"),
                ("postflight", 1, "postflight_verification_failed")):
            with self.subTest(fault=fault):
                case = self.directory / fault
                source, output = case / "source", case / "output"
                source.mkdir(parents=True)
                for name, text in {
                        "business.py": "VALUE = 1\n",
                        "evaluation/window_integration/semantic_cases.json": '{"cases":[]}\n',
                        "tests/window_integration/semantic_runner_adapter.py":
                            'raise AssertionError("cloud_runner_must_not_execute")\n'}.items():
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                process = self.child(["-c", code, EXECUTOR, source, output, fault])
                observation = json.loads(process.stdout)
                self.record("COL06-no-handoff-" + fault, process,
                            final_execution=observation)
                self.assertEqual(process.returncode, expected_exit)
                self.assertEqual(observation["returned_code"], process.returncode)
                self.assertEqual(observation["planned_exit_code"], process.returncode)
                self.assertEqual(observation["status"], expected_status)
                self.assertEqual(observation["baseline_calls"], 2)
                self.assertEqual(observation["executions"], [])
                self.assertEqual(observation["rounds_executed"], 0)
                self.assertFalse(observation["cloud_models_called"])
                self.assertFalse(observation["handoff_gate"]["ready"])
                self.assertTrue(observation["candidate_source_matches_frozen"])
                self.assertTrue(observation["candidate_data_matches_frozen"])
                self.assertEqual(observation["postflight_error_type"],
                                 "RuntimeError" if fault == "postflight" else None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
