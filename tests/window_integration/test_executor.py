"""执行器合同测试：仅临时源目录与本地无网络子进程。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/run_window_integration.py'
SPEC = importlib.util.spec_from_file_location('window_integration_executor_under_test', SCRIPT)
EXECUTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXECUTOR)


class ExecutorContractsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-executor-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'artifacts'
        self.environment = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}

    def write(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def fixture_runner(self, *, fail_offline=False, mutate_candidate=False, response_metadata=None,
                       sigterm_exit=False):
        self.write('business.py', 'VALUE = 1\n')
        self.write('evaluation/window_integration/semantic_cases.json', '{"cases": []}\n')
        # 只供执行器合同的进程替身：新入口仍执行同一个旧fixture_runner，
        # 非真实语义适配；源码漂移、非零退出及元数据断言保持不变。
        self.write('tests/window_integration/semantic_runner_adapter.py',
                   "import runpy\nfrom pathlib import Path\n"
                   "runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/run_skill_acceptance.py'), run_name='__main__')\n")
        self.write('scripts/run_skill_acceptance.py', f'''
import json, sys
from pathlib import Path
args=sys.argv[1:]
if {sigterm_exit!r}:
    import signal, time
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print('ready-for-timeout', flush=True)
    time.sleep(60)
out=Path(args[args.index('--out-dir')+1]);out.mkdir(parents=True,exist_ok=False)
print('fixture stdout retained',flush=True)
print('fixture stderr retained',file=sys.stderr,flush=True)
if {mutate_candidate!r}:
    (Path(__file__).resolve().parents[1]/'business.py').write_text('VALUE = 2\\n')
(out/'semantic-results.json').write_text(json.dumps({{
    'records':{[{'response_metadata': response_metadata}] if response_metadata is not None else []!r},
    'run_status':'collection_complete', 'collection_complete':True,
    'source_unchanged':True, 'dataset_unchanged':True,
    'summary':{{'no_financial_execution':True}}
}}))
sys.exit(17 if {fail_offline!r} and '--offline' in args else 0)
''')

    def invoke_main(self, *, stage='preflight', run_name='run', handoff=None,
                    preflight_manifest=None, timeout=5):
        args = ['run_window_integration.py', '--stage', stage, '--source-root', str(self.source),
                '--out-dir', str(self.output / run_name), '--budget-seconds', '20',
                '--stage-timeout-seconds', str(timeout), '--rounds', '1']
        if handoff is not None:
            args += ['--handoff', str(handoff)]
        if preflight_manifest is not None:
            args += ['--paid-preflight-manifest', str(preflight_manifest)]
        # 禁止默认现役源目录、数据库/网络检查与密钥读取进入这些测试。
        with patch.object(EXECUTOR, 'ROOT', self.source), \
                patch.object(EXECUTOR, 'OUTPUT_ROOT', self.output), \
                patch.object(EXECUTOR, 'baseline', return_value={'bank_state': {'synthetic': True}}), \
                patch.object(EXECUTOR, 'isolated_environment', return_value=self.environment), \
                patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
            return EXECUTOR.main()

    def paid_preflight_inputs(self, result, *, returncode=0, stale_source=False):
        from hashlib import sha256

        preflight_dir = self.source / EXECUTOR.PAID_PREFLIGHT_RELATIVE
        script = self.write(str(EXECUTOR.PAID_PREFLIGHT_RELATIVE / 'preflight.py'),
            'import json,sys,hashlib\nfrom pathlib import Path\n'
            f'result={result!r}\n'
            "manifest=Path(sys.argv[sys.argv.index('--manifest')+1])\n"
            "result['manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()\n"
            "out=Path(sys.argv[sys.argv.index('--out')+1]);out.parent.mkdir(parents=True,exist_ok=True)\n"
            "out.write_text(json.dumps(result),encoding='utf-8')\n"
            f'sys.exit({returncode})\n')
        delivery = {'schema': 'navren.baseline-preflight-delivery.v1',
            'status': 'component_verified_pending_integration_adoption',
            'files': {'preflight.py': sha256(script.read_bytes()).hexdigest()},
            'models_called': 0, 'external_reviews': 0, 'resource_preparation_only': True}
        (preflight_dir / 'delivery.json').write_text(json.dumps(delivery), encoding='utf-8')

        source_manifest = EXECUTOR.source_manifest(self.source)
        if stale_source:
            source_manifest['business.py'] = '0' * 64
        source_manifest_path = preflight_dir / 'source-manifest.json'
        source_manifest_path.write_text(json.dumps(source_manifest, sort_keys=True), encoding='utf-8')
        manifest_path = preflight_dir / 'paid-manifest.json'
        manifest_path.write_text(json.dumps({
            'schema': 'navren.baseline-preflight.v1', 'stage': 'paid',
            'source_root': str(self.source.resolve()),
            'source_manifest': {'path': str(source_manifest_path),
                                'sha256': sha256(source_manifest_path.read_bytes()).hexdigest()},
            'entrypoint': {'path': 'business.py', 'sha256': EXECUTOR.sha(self.source / 'business.py')},
        }), encoding='utf-8')
        return manifest_path, sha256(script.read_bytes()).hexdigest()

    def handoff(self, name='handoff.json'):
        path = self.root / name
        payload = {key: True for key in ('agent_handed_off', 'existing_semantic_run_finished',
            'cloud_retest_authorized', 'isolated_fixture_preflight_passed')}
        payload['source_sha256'] = EXECUTOR.source_manifest(self.source)
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    @staticmethod
    def process_alive(pid):
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                capture_output=True, text=True, check=False)
        status = result.stdout.strip()
        return bool(status) and not status.startswith('Z')

    def test_timeout_kills_descendant_that_ignores_sigterm(self):
        """主进程已退出不能作为整个进程组退出的证据。"""
        child_pid_file = self.root / 'child.pid'
        child_code = (
            "import os,signal,sys,time;from pathlib import Path;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
        )
        parent_code = (
            "import subprocess,sys,time;from pathlib import Path;"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
            "p=Path(sys.argv[2]);deadline=time.monotonic()+5\n"
            "while not p.exists() and time.monotonic()<deadline:time.sleep(.01)\n"
            "print('child-ready',flush=True);time.sleep(60)"
        )
        directory = self.root / 'timeout-execution'
        started = time.monotonic()
        try:
            record = EXECUTOR.execute(
                [sys.executable, '-c', parent_code, child_code, str(child_pid_file)],
                self.source, directory, .6, self.environment)
            self.assertEqual(record['status'], 'timed_out')
            self.assertNotEqual(record['exit_code'], 0)
            self.assertLess(time.monotonic() - started, 9)
            self.assertTrue(child_pid_file.exists(), '必须实际启动子进程，不能仅杀父进程算通过')
            child_pid = int(child_pid_file.read_text())
            deadline = time.monotonic() + .5
            while self.process_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertFalse(self.process_alive(child_pid), '超时返回后仍有忽略SIGTERM的子进程存活')
        finally:
            # 无论断言是否失败，都清理本测试创建的整个组。
            execution = directory / 'execution.json'
            if execution.exists():
                pid = json.loads(execution.read_text()).get('pid')
                if pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)

    def test_nonzero_exit_and_both_logs_are_preserved(self):
        directory = self.root / 'nonzero-execution'
        record = EXECUTOR.execute(
            [sys.executable, '-c', "import sys;print('out');print('err',file=sys.stderr);sys.exit(23)"],
            self.source, directory, 3, self.environment)
        self.assertEqual(record['exit_code'], 23)
        self.assertEqual(json.loads((directory / 'execution.json').read_text())['exit_code'], 23)
        self.assertEqual((directory / 'stdout.log').read_text().strip(), 'out')
        self.assertEqual((directory / 'stderr.log').read_text().strip(), 'err')

    def test_timeout_with_clean_sigterm_exit_cannot_pass_stage(self):
        """实际进程因超时收到SIGTERM并exit0，仍是未完成。"""
        self.fixture_runner(sigterm_exit=True)
        self.assertEqual(self.invoke_main(timeout=1), 1)
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['executions']), 1)
        execution = report['executions'][0]
        self.assertEqual(execution['status'], 'timed_out')
        self.assertEqual(execution['exit_code'], 0, '实际退出码应原样保留')
        self.assertIn('ready-for-timeout', (self.output / 'run/oracle/stdout.log').read_text())

    def test_launch_error_leaves_terminal_execution_record(self):
        directory = self.root / 'launch-error'
        # 实现可以抛出OSError或返回失败记录；合同只要求证据终态且不能报成功。
        try:
            EXECUTOR.execute([str(self.root / 'missing-program')], self.source,
                             directory, 1, self.environment)
        except OSError:
            pass
        record = json.loads((directory / 'execution.json').read_text())
        self.assertNotEqual(record['status'], 'running', '未启动的进程不能遗留running状态')
        self.assertIn('completed_at', record)
        self.assertNotEqual(record['exit_code'], 0)

    def test_baseline_never_records_private_process_arguments(self):
        sentinel = 'SENTINEL_PRIVATE_ARGUMENT'
        fake_ps = subprocess.CompletedProcess([], 0, stdout=(
            ' PID PPID STARTED COMMAND\n'
            f'123 1 Tue Oct 6 00:00:00 2026 /fake/Navren/server.py --token {sentinel}\n'
            f'124 1 Tue Oct 6 00:00:00 2026 run_skill_acceptance.py --prompt {sentinel}\n'
        ))
        with patch.object(EXECUTOR.subprocess, 'run', return_value=fake_ps), \
                patch.object(EXECUTOR, 'get_public', return_value={'status': None}), \
                patch.object(EXECUTOR, 'app_release', return_value='navren-fixture'), \
                patch.object(EXECUTOR, 'database_digest', return_value={'synthetic': True}), \
                patch.dict(EXECUTOR.os.environ, {}, clear=True):
            record = EXECUTOR.baseline(self.source, 'http://network-disabled.invalid')
        serialized = json.dumps(record)
        self.assertNotIn(sentinel, serialized, '服务进程参数不能原样写入普通验收日志')
        self.assertNotIn('--token', serialized)

    def test_main_retains_nonzero_stage_even_when_next_stage_passes(self):
        self.fixture_runner(fail_offline=True)
        self.assertNotEqual(self.invoke_main(), 0)
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual([r['exit_code'] for r in report['executions']], [17, 0])
        self.assertEqual(report['planned_exit_code'], 1)
        self.assertIn('fixture stderr retained', (self.output / 'run/oracle/stderr.log').read_text())

    def test_execute_refuses_existing_artifacts_before_launch(self):
        directory = self.root / 'existing'
        directory.mkdir()
        marker = directory / 'execution.json'
        marker.write_text('{"status":"running","marker":"keep"}')
        original = marker.read_bytes()
        with patch.object(EXECUTOR.subprocess, 'Popen') as launch:
            with self.assertRaises(FileExistsError):
                EXECUTOR.execute([sys.executable, '-c', 'pass'], self.source,
                                 directory, 1, self.environment)
        launch.assert_not_called()
        self.assertEqual(marker.read_bytes(), original)

    def test_main_refuses_existing_run_before_baseline_or_execution(self):
        target = self.output / 'run'
        target.mkdir(parents=True)
        marker = target / 'run.json'
        marker.write_text('{"status":"running"}')
        with patch.object(EXECUTOR, 'execute') as execute:
            with self.assertRaises(FileExistsError):
                self.invoke_main()
        execute.assert_not_called()
        self.assertEqual(marker.read_text(), '{"status":"running"}')

    def test_snapshot_hashes_match_and_exclude_outputs(self):
        self.write('business.py', 'VALUE = 1\n')
        self.write('agent_runtime/tool.py', 'ANSWER = 2\n')
        self.write('financial_mcp/contract.py', 'SCHEMA_VERSION = 1\n')
        self.write('financial_data_service/__init__.py', 'from .service import FinancialDataService\n')
        self.write('financial_data_service/service.py', 'class FinancialDataService: pass\n')
        self.write('dependency-resources/deepseek-v4/tokenizer.json', '{"version": 1}\n')
        self.write('dependency-resources/deepseek-v4/provenance.json', '{"note": "not a runtime dependency"}\n')
        self.write('outputs/old-results.json', '{"old": true}\n')
        destination = self.root / 'candidate'
        expected = EXECUTOR.copy_snapshot(self.source, destination)
        self.assertEqual(expected, EXECUTOR.source_manifest(destination))
        self.assertEqual(expected, EXECUTOR.source_manifest(self.source))
        self.assertEqual(expected['financial_mcp/contract.py'], EXECUTOR.sha(destination / 'financial_mcp/contract.py'))
        self.assertEqual(expected['financial_data_service/service.py'], EXECUTOR.sha(destination / 'financial_data_service/service.py'))
        self.assertEqual(expected['dependency-resources/deepseek-v4/tokenizer.json'],
                         EXECUTOR.sha(destination / 'dependency-resources/deepseek-v4/tokenizer.json'))
        self.assertNotIn('dependency-resources/deepseek-v4/provenance.json', expected)
        self.assertNotIn('outputs/old-results.json', expected)
        with self.assertRaises(FileExistsError):
            EXECUTOR.copy_snapshot(self.source, destination)

    def test_snapshot_rejects_source_change_during_copy(self):
        original = self.write('business.py', 'VALUE = 1\n')
        real_copy = EXECUTOR.shutil.copy2
        def copy_then_mutate(source, target):
            copied = real_copy(source, target)
            original.write_text('VALUE = 2\n')
            return copied
        with patch.object(EXECUTOR.shutil, 'copy2', side_effect=copy_then_mutate):
            with self.assertRaisesRegex(ValueError, 'source_changed_during_snapshot'):
                EXECUTOR.copy_snapshot(self.source, self.root / 'candidate')

    def test_cloud_without_handoff_never_launches_execute(self):
        self.fixture_runner()
        with patch.object(EXECUTOR, 'execute') as execute:
            self.assertEqual(self.invoke_main(stage='cloud'), 2)
        execute.assert_not_called()
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertEqual(report['status'], 'waiting_for_module_handoff')
        self.assertFalse(report['cloud_models_called'])
        self.assertEqual(report['rounds_executed'], 0)
        self.assertEqual(report['cloud_runner_launch_attempts'], 0)

    def test_cloud_paid_preflight_negative_controls_never_launch_provider_runner(self):
        self.fixture_runner()
        success = {'schema': 'navren.baseline-preflight.v1', 'status': 'PRECHECK_PENDING',
            'stage': 'paid', 'paid_start_allowed': True, 'models_called': 0,
            'external_reviews': 0, 'overall_accepted': False, 'release_allowed': False, 'blockers': []}
        controls = [
            ('nonzero_returncode', success, 2, False),
            ('wrong_stage', {**success, 'stage': 'free'}, 0, False),
            ('paid_start_false', {**success, 'paid_start_allowed': False,
                                  'status': 'PRECHECK_BLOCKED'}, 0, False),
            ('source_drift', success, 0, True),
        ]
        for name, result, returncode, stale_source in controls:
            with self.subTest(control=name):
                manifest, script_sha = self.paid_preflight_inputs(
                    result, returncode=returncode, stale_source=stale_source)
                handoff = self.handoff(name + '-handoff.json')
                with patch.object(EXECUTOR, 'PAID_PREFLIGHT_SCRIPT_SHA256', script_sha), \
                        patch.object(EXECUTOR, 'execute') as execute:
                    exit_code = self.invoke_main(stage='cloud', run_name=name, handoff=handoff,
                                                 preflight_manifest=manifest)
                self.assertEqual(exit_code, 2)
                execute.assert_not_called()
                report = json.loads((self.output / name / 'run.json').read_text())
                self.assertEqual(report['status'], 'paid_preflight_blocked')
                self.assertEqual(report['cloud_runner_launch_attempts'], 0)
                self.assertEqual(report['rounds_executed'], 0)
                self.assertFalse(report['cloud_models_called'])
                self.assertFalse(report['paid_preflight_gate']['ready'])

    def test_cloud_paid_preflight_missing_manifest_fails_closed(self):
        self.fixture_runner()
        handoff = self.handoff()
        with patch.object(EXECUTOR, 'execute') as execute:
            self.assertEqual(self.invoke_main(stage='cloud', handoff=handoff), 2)
        execute.assert_not_called()
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertEqual(report['status'], 'paid_preflight_blocked')
        self.assertEqual(report['paid_preflight_gate']['reason'], 'paid_preflight_manifest_required')
        self.assertEqual(report['cloud_runner_launch_attempts'], 0)
        self.assertEqual(report['rounds_executed'], 0)
        self.assertFalse(report['cloud_models_called'])

    def test_handoff_requires_exact_source_and_explicit_booleans(self):
        manifest = {'business.py': 'expected-hash'}
        handoff = self.root / 'handoff.json'
        payload = {k: True for k in ('agent_handed_off', 'existing_semantic_run_finished',
                                     'cloud_retest_authorized', 'isolated_fixture_preflight_passed')}
        payload['source_sha256'] = {'business.py': 'different-hash'}
        handoff.write_text(json.dumps(payload))
        self.assertFalse(EXECUTOR.handoff_gate(handoff, manifest)['ready'])
        payload['source_sha256'] = manifest
        payload['cloud_retest_authorized'] = 'true'
        handoff.write_text(json.dumps(payload))
        self.assertFalse(EXECUTOR.handoff_gate(handoff, manifest)['ready'])
        payload['cloud_retest_authorized'] = True
        handoff.write_text(json.dumps(payload))
        self.assertTrue(EXECUTOR.handoff_gate(handoff, manifest)['ready'])

    def test_cloud_call_flag_uses_actual_response_metadata_schema(self):
        # 既有semantic-results逐题原记录字段；本地假执行器不调用模型。
        examples = [({'primary': [], 'reviewer': []}, False),
                    ({'primary': [{'response_id': 'fixture-local-only'}], 'reviewer': []}, True)]
        ready = {'schema': 'navren.baseline-preflight.v1', 'status': 'PRECHECK_PENDING',
            'stage': 'paid', 'paid_start_allowed': True, 'models_called': 0,
            'external_reviews': 0, 'overall_accepted': False, 'release_allowed': False, 'blockers': []}
        for number, (metadata, expected) in enumerate(examples):
            with self.subTest(metadata=metadata):
                self.fixture_runner(response_metadata=metadata)
                manifest, script_sha = self.paid_preflight_inputs(ready)
                handoff = self.handoff(f'handoff-{number}.json')
                run_name = f'cloud-schema-{number}'
                with patch.object(EXECUTOR, 'PAID_PREFLIGHT_SCRIPT_SHA256', script_sha):
                    self.assertEqual(self.invoke_main(stage='cloud', handoff=handoff, run_name=run_name,
                                                      preflight_manifest=manifest), 0)
                report = json.loads((self.output / run_name / 'run.json').read_text())
                self.assertEqual(report['cloud_models_called'], expected)
                self.assertEqual(report['cloud_runner_launch_attempts'], 1)
                self.assertTrue(report['paid_preflight_gate']['ready'])

    def test_unknown_model_cost_stays_none(self):
        result = self.root / 'semantic-results.json'
        result.write_text(json.dumps({'records': [{'duration_seconds': 1}, {'duration_seconds': 3}],
                                      'collection_complete': True, 'summary': {}}))
        summary = EXECUTOR.semantic_summary(result)
        self.assertIsNone(summary['total_model_cost'])
        self.assertEqual(summary['semantic_grading'], 'pending_independent_review')
        self.fixture_runner()
        self.assertEqual(self.invoke_main(stage='cloud'), 2)
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertIsNone(report['total_model_cost'])

    def test_candidate_drift_cannot_be_reported_as_stage_passed(self):
        """子进程修改冻结副本时，原源目录不变也不能维持验收成绩。"""
        self.fixture_runner(mutate_candidate=True)
        code = self.invoke_main()
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertEqual((self.source / 'business.py').read_text(), 'VALUE = 1\n')
        self.assertNotEqual(code, 0, '冻结副本执行后漂移却返回成功')
        self.assertNotEqual(report['status'], 'engineering_stage_passed')

    def test_ui_empty_or_partial_receipts_cannot_pass_with_exit_zero(self):
        # 这里只造执行器收据，不执行或生成产品/模型成绩。
        names = ['tests/ui_window/test_fixture_' + str(n) + '.js' for n in range(13)]
        names += list(EXECUTOR.UI_REGRESSIONS)
        for name in names:
            self.write(name, '// trusted executor receipt fixture only\n')
        report_path = self.root / 'ui-results.json'
        rows = [{'test': name, 'exit_code': 0, 'timeout': False,
                 'test_sha256': EXECUTOR.sha(self.source / name)} for name in names]
        valid = {'results': rows, 'total': 22, 'passed': 22,
                 'frontend_stable_during_checks': True}
        report_path.write_text(json.dumps(valid))
        self.assertTrue(EXECUTOR.ui_test_summary(self.source, report_path)['complete'])
        for mutation in ('empty', 'missing', 'duplicate', 'timeout', 'changed_source'):
            value = json.loads(json.dumps(valid))
            if mutation == 'empty':
                value.update(results=[], total=0, passed=0)
            elif mutation == 'missing':
                value['results'].pop()
            elif mutation == 'duplicate':
                value['results'][-1] = value['results'][0]
            elif mutation == 'timeout':
                value['results'][0]['timeout'] = True
            else:
                value['results'][0]['test_sha256'] = 'old-source'
            report_path.write_text(json.dumps(value))
            complete = EXECUTOR.ui_test_summary(self.source, report_path)['complete']
            with self.subTest(mutation=mutation):
                self.assertFalse(complete)
                self.assertFalse(EXECUTOR.execution_passed({'status': 'exited', 'exit_code': 0,
                    'result_contract_passed': complete}))

    def test_zero_exit_cannot_replace_real_complete_structured_counts(self):
        path = self.root / 'structured-result.json'
        self.assertFalse(EXECUTOR.structured_test_summary(path, 18)['complete'])
        valid = {'tests_run': 18, 'failures': 0, 'errors': 0, 'skipped': 0,
                 'expected_failures': 0, 'unexpected_successes': 0,
                 'complete': True, 'planned_exit_code': 0}
        path.write_text(json.dumps(valid))
        self.assertTrue(EXECUTOR.structured_test_summary(path, 18)['complete'])
        variants = [{**valid, 'tests_run': 17}, {**valid, 'tests_run': True},
                    {**valid, 'skipped': 1}, {**valid, 'expected_failures': 1},
                    {**valid, 'unexpected_successes': 1}, {**valid, 'failures': 1},
                    {**valid, 'errors': 1}, {**valid, 'complete': False},
                    {k: v for k, v in valid.items() if k != 'tests_run'}]
        for record in variants:
            with self.subTest(record=record):
                path.write_text(json.dumps(record))
                qualified = EXECUTOR.structured_test_summary(path, 18)['complete']
                self.assertFalse(qualified)
                self.assertFalse(EXECUTOR.execution_passed({'status': 'exited', 'exit_code': 0,
                    'result_contract_passed': qualified}))

    def case_receipt_fixture(self, phase):
        """仅执行器收据：不运行业务，不产出原题或云成绩。"""
        cases = [{'id': name, 'expected': {'facts': [{'label': 'receipt', 'tool': 'budget',
                 'path': 'data.amount_minor', 'expected': 7}]}} for name in
                 ('S01_emergency_floor_once', 'S08_conditional_not_saved_profile')]
        cases[1]['expected']['facts'] += [
            {'table': 'profile_observations', 'metric': 'count_delta', 'expected': 0},
            {'method': 'g.memories.state(w)', 'path': 'items', 'metric': 'count_delta', 'expected': 0}]
        dataset = self.root / 'receipt-cases.json'
        dataset.write_text(json.dumps({'cases': cases}))
        ids = [case['id'] for case in cases]
        if phase == 'independent-oracle':
            raw = {'scope': 'offline_fixture_self_test', 'model_calls': 0, 'semantic_pass_count': None,
                   'records': [{'id': name, 'preflight_status': 'passed',
                       'check_count': EXECUTOR.SELF_TEST_CHECK_COUNTS[name.split('_', 1)[0]],
                       'failed_checks': [], 'snapshot_stable': True,
                       'structural_control_status': 'needs_human_review',
                       'negative_controls_passed': True} for name in ids]}
        else:
            state = {'total_cash_minor': 7, 'profile': {}, 'orders': [], 'holdings': [], 'fund_holds': [],
                     'observations': 0, 'memories': []}
            raw = {'run_status': 'collection_complete', 'collection_complete': True, 'complete': True,
                   'invalidated': False, 'source_unchanged': True, 'dataset_unchanged': True,
                   'planned_exit_code': 0, 'selected_case_ids': ids, 'expected_case_count': 2,
                   'dataset_sha256': EXECUTOR.sha(dataset), 'offline': phase == 'oracle',
                   'process_preflight': phase == 'process', 'records': []}
            for case in cases:
                raw['records'].append({'id': case['id'],
                    'oracle': [{**case['expected']['facts'][0], 'actual': 7, 'passed': True}],
                    'before': state, 'after': state, 'no_financial_execution': True,
                    'no_action_cards': True, 'profile_unchanged': True, 'observation_delta': 0, 'memory_delta': 0,
                    'http_status': 200, 'technical_completed': True, 'process_preflight_completed': True,
                    'integration_failures': [], 'result': {'agent_runtime': {'trace': [
                        {'tool': name, 'status': 'completed'} for name in
                        ('load_skill', 'account_summary', 'personal_graph', 'knowledge_status', 'budget', 'cashflow')]}}})
        path = self.root / (phase + '-receipts.json')
        path.write_text(json.dumps(raw))
        return path, dataset, raw

    def test_local_case_receipts_count_actual_cases_and_reject_false_completion(self):
        for phase in ('oracle', 'process', 'independent-oracle'):
            path, dataset, valid = self.case_receipt_fixture(phase)
            result = EXECUTOR.local_case_summary(path, dataset, phase)
            self.assertTrue(result['complete'])
            self.assertEqual(result['tests_run'], 2)
            self.assertEqual(result['semantic_grading'], 'not_evaluated')
            for mutation in ('short', 'duplicate', 'missing_check', 'failed_check', 'short_preflight'):
                raw = json.loads(json.dumps(valid))
                if mutation == 'short':
                    raw['records'].pop()
                elif mutation == 'duplicate':
                    raw['records'][-1] = raw['records'][0]
                elif phase == 'independent-oracle':
                    if mutation == 'missing_check':
                        del raw['records'][0]['negative_controls_passed']
                    elif mutation == 'short_preflight':
                        raw['records'][0]['check_count'] = 1
                    else:
                        raw['records'][0]['negative_controls_passed'] = False
                elif mutation == 'missing_check':
                    raw['records'][0]['oracle'].clear()
                else:
                    raw['records'][0]['oracle'][0]['actual'] = 8
                path.write_text(json.dumps(raw))
                with self.subTest(phase=phase, mutation=mutation):
                    counts = EXECUTOR.local_case_summary(path, dataset, phase)
                    self.assertFalse(counts['complete'])
                    self.assertFalse(EXECUTOR.execution_passed({'status': 'exited', 'exit_code': 0,
                        'result_contract_passed': counts['complete']}))

    def test_process_flags_cannot_replace_real_trace_and_typed_facts(self):
        path, dataset, valid = self.case_receipt_fixture('process')
        for mutation in ('trace', 'http', 'failure', 'bool_amount', 'missing_snapshot', 'changed_profile',
                         'memory_changed', 'memory_replaced', 'observation_changed', 'missing_delta'):
            raw = json.loads(json.dumps(valid))
            row = raw['records'][0]
            if mutation == 'trace':
                row['result']['agent_runtime']['trace'].pop()
            elif mutation == 'http':
                row['http_status'] = 500
            elif mutation == 'failure':
                row['integration_failures'] = [{'code': 'fixture-failure'}]
            elif mutation == 'bool_amount':
                row['oracle'][0]['actual'] = True
            elif mutation == 'missing_snapshot':
                del row['before']['profile']
            elif mutation == 'changed_profile':
                row['after']['profile'] = {'changed': True}
            elif mutation == 'missing_delta':
                del row['observation_delta']
            elif mutation == 'memory_changed':
                row = raw['records'][1]
                row['after']['memories'] = [{'id': 'unconfirmed-fixture'}]
                row['memory_delta'] = 1
            elif mutation == 'memory_replaced':
                row = raw['records'][1]
                row['before']['memories'] = [{'id': 'old-confirmed-fixture'}]
                row['after']['memories'] = [{'id': 'unconfirmed-fixture'}]
            else:
                row = raw['records'][1]
                row['after']['observations'] = 1
                row['observation_delta'] = 1
            path.write_text(json.dumps(raw))
            with self.subTest(mutation=mutation):
                self.assertFalse(EXECUTOR.local_case_summary(path, dataset, 'process')['complete'])
        path.write_text('non-json output before the final receipt')
        self.assertIsNone(EXECUTOR.local_case_summary(path, dataset, 'process')['tests_run'])

    def test_original_cli_dispatch_preserves_isolation_without_knowledge_override(self):
        self.fixture_runner()
        cli = self.source / 'scripts/run_skill_acceptance.py'
        cli.write_text("import os\nassert 'NAVREN_KNOWLEDGE_DB' not in os.environ\n" + cli.read_text())
        state = self.root / 'unit-state'
        self.environment.update({
            'NAVREN_KNOWLEDGE_DB': str(state / 'knowledge.sqlite3'),
            'NAVREN_SIM_DB': str(state / 'bank.sqlite3'),
            'QINGZHOU_SIM_DB': str(state / 'bank.sqlite3'),
            'NAVREN_MARKET_DB': str(state / 'market.sqlite3'),
            'NAVREN_FUND_CACHE': str(state / 'funds'),
            'NAVREN_TASK_TOTAL_TIMEOUT_SECONDS': '105',
        })
        original_environment = self.environment.copy()
        actual_execute = EXECUTOR.execute
        dispatched = []
        def observe_dispatch(argv, source, output, timeout, environment):
            dispatched.append((argv, environment.copy()))
            return actual_execute(argv, source, output, timeout, environment)
        with patch.object(EXECUTOR, 'execute', side_effect=observe_dispatch):
            self.invoke_main()
        self.assertEqual(len(dispatched), 2)
        self.assertEqual(self.environment, original_environment)
        expected = {key: value for key, value in original_environment.items()
                    if key != 'NAVREN_KNOWLEDGE_DB'}
        for argv, environment in dispatched:
            self.assertTrue(argv[1].endswith('/semantic_runner_adapter.py'))
            self.assertEqual(environment, expected)
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertEqual([row['exit_code'] for row in report['executions']], [0, 0])
        self.assertTrue(all(row['status'] == 'exited' for row in report['executions']))

    def test_default_gateway_uses_isolated_bank_and_preserves_actual_baseline_binding(self):
        from finance_gateway import FinanceGateway
        from contextlib import closing
        live_marker = (self.root / 'actual-live-path-not-opened.sqlite3').resolve()
        state = (self.root / 'isolated-state').resolve()
        state.mkdir()
        with patch.dict(os.environ, {'PATH': os.defpath, 'NAVREN_SIM_DB': str(live_marker)}, clear=True):
            environment = EXECUTOR.isolated_environment(self.source, state)
            self.assertEqual(os.environ['NAVREN_SIM_DB'], str(live_marker))
        self.assertEqual(environment['NAVREN_INTEGRATION_LIVE_BANK_DB'], str(live_marker))
        for name, relative in (('NAVREN_SIM_DB', 'bank.sqlite3'), ('QINGZHOU_SIM_DB', 'bank.sqlite3'),
                               ('NAVREN_KNOWLEDGE_DB', 'knowledge.sqlite3'), ('NAVREN_MARKET_DB', 'market.sqlite3')):
            self.assertEqual(Path(environment[name]), state / relative)
        with patch.dict(os.environ, environment, clear=True):
            gateway = FinanceGateway()
            self.assertEqual(gateway.bank.database, state / 'bank.sqlite3')
            gateway.create_session()
            with closing(gateway.bank.connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0], 1)
            self.assertEqual(EXECUTOR.baseline_bank_path(), live_marker)
            nested = EXECUTOR.isolated_environment(self.source, self.root / 'nested-state')
            self.assertEqual(nested['NAVREN_INTEGRATION_LIVE_BANK_DB'], str(live_marker))
            self.assertNotEqual(nested['NAVREN_SIM_DB'], environment['NAVREN_SIM_DB'])
        self.assertFalse(live_marker.exists())

    def test_actual_main_exits_nonzero_when_postflight_live_digest_changes(self):
        self.fixture_runner()
        real_copy = EXECUTOR.copy_snapshot
        def observe_later_live_change(source, target):
            # invoke_main 的免费 baseline 替身先已返回原始摘要，此后返回不同摘要。
            EXECUTOR.baseline.return_value = {'bank_state': {'synthetic': False}}
            return real_copy(source, target)
        with patch.object(EXECUTOR, 'copy_snapshot', side_effect=observe_later_live_change):
            code = self.invoke_main()
        self.assertEqual(code, 1)
        report = json.loads((self.output / 'run/run.json').read_text())
        self.assertFalse(report['live_business_digest_unchanged'])
        self.assertEqual(report['status'], 'live_business_drift')
        self.assertEqual(report['planned_exit_code'], 1)
        self.assertEqual(report['before_live']['bank_state'], {'synthetic': True})
        self.assertEqual(report['after_live']['bank_state'], {'synthetic': False})


if __name__ == '__main__':
    unittest.main()
