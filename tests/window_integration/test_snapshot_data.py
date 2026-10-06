"""数据源包冻结复核：只用临时合成 manifest，baseline/执行均 mock。"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/run_window_integration.py'
SPEC = importlib.util.spec_from_file_location('snapshot_data_executor_under_test', SCRIPT)
EXECUTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXECUTOR)


class SnapshotDataTests(unittest.TestCase):
    def install_original_formal_support(self, source):
        original = SCRIPT.parents[1]
        manifest_name = 'evaluation/window_integration/formal_support_manifest.json'
        manifest = json.loads((original / manifest_name).read_text())
        names = [manifest_name, 'evaluation/window_integration/formal_business_tasks.json']
        names += [row['path'] for row in manifest['files']]
        for name in names:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original / name, target)

    def test_original_formal_support_survives_both_copy_layers_and_rejects_replacement(self):
        source = self.root / 'formal-source'
        source.mkdir()
        self.install_original_formal_support(source)
        expected = EXECUTOR.formal_support_manifest(source, required=True)
        self.assertEqual(len(expected), 8)
        prepared, candidate = self.root / 'formal-prepared', self.root / 'formal-candidate'
        for origin, destination in ((source, prepared), (prepared, candidate)):
            EXECUTOR.copy_snapshot(origin, destination)
            self.assertEqual(EXECUTOR.copy_formal_support_snapshot(origin, destination, required=True), expected)
        self.assertEqual(EXECUTOR.formal_support_manifest(candidate, required=True), expected)
        self.assertEqual(EXECUTOR.data_manifest(candidate), {})
        self.assertEqual(EXECUTOR.auxiliary_manifest(candidate), {})
        archive = next(name for name in expected if name.endswith('simulated_bank/service.py'))
        (candidate / archive).write_text('# replacement active source is forbidden\n')
        with self.assertRaisesRegex(ValueError, 'formal_support_original_missing_or_changed'):
            EXECUTOR.formal_support_manifest(candidate, required=True)
        (candidate / archive).unlink()
        (candidate / archive).symlink_to(prepared / archive)
        with self.assertRaisesRegex(ValueError, 'formal_support_path_invalid'):
            EXECUTOR.formal_support_manifest(candidate, required=True)
        path = source / 'evaluation/window_integration/formal_support_manifest.json'
        raw = json.loads(path.read_text());raw['files'].pop();raw['file_count'] = 7
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, 'formal_support_entries_differ_from_original_authority'):
            EXECUTOR.formal_support_manifest(source, required=True)

    def test_required_auxiliary_sources_are_separately_frozen_and_reject_escape(self):
        source = self.root / 'question-source'
        source.mkdir()
        relative = Path('outputs/window-progress/rag/manual-question-set.json')
        question = source / relative
        question.parent.mkdir(parents=True)
        question.write_text('{"cases":[{"id":"P10","question":"固定原题"}]}\n')
        contract_relative = Path('outputs/window-progress/finance/implementation/adapter-contract.md')
        contract = source / contract_relative
        contract.parent.mkdir(parents=True)
        contract.write_text('固定金融接口合同\n')
        expected = {str(relative): self.digest(question), str(contract_relative): self.digest(contract)}
        target = self.root / 'question-candidate'
        self.assertEqual(EXECUTOR.copy_auxiliary_snapshot(source, target), expected)
        self.assertEqual(EXECUTOR.auxiliary_manifest(target), expected)
        self.assertEqual(EXECUTOR.data_manifest(source), {})
        self.assertEqual((target / contract_relative).read_bytes(), contract.read_bytes())
        (target / relative).write_text('{"cases":[]}\n')
        self.assertNotEqual(EXECUTOR.auxiliary_manifest(target), expected)
        question.unlink()
        outside = self.root / 'outside-question.json'
        outside.write_text('{"cases":[]}\n')
        question.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'invalid_auxiliary_source_path'):
            EXECUTOR.auxiliary_manifest(source)
        question.unlink()
        question.parent.rmdir()
        outside_dir = self.root / 'outside-question-dir'
        outside_dir.mkdir()
        (outside_dir / question.name).write_text('{"cases":[]}\n')
        question.parent.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'invalid_auxiliary_source_path'):
            EXECUTOR.copy_auxiliary_snapshot(source, self.root / 'escape-candidate')
        question.parent.unlink()
        contract.unlink()
        contract.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'invalid_auxiliary_source_path'):
            EXECUTOR.auxiliary_manifest(source)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-snapshot-data-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def source_fixture(self, name):
        source = self.root / name
        package = source / 'outputs/contest-finances'
        package.mkdir(parents=True)
        (source / 'business.py').write_text('VALUE = "synthetic-only"\n')
        base = source / 'base_runtime'
        base.mkdir()
        (base / '__init__.py').write_text('')
        (base / 'identity.py').write_text('IDENTITY = "isolated-fixture"\n')
        dataset = source / 'evaluation/window_integration/semantic_cases.json'
        dataset.parent.mkdir(parents=True)
        dataset.write_text('{"cases": []}\n')
        adapter = source / 'tests/window_integration/semantic_runner_adapter.py'
        adapter.parent.mkdir(parents=True)
        # 本套execute已被假进程替换；仅满足真实入口存在性的夹具条件。
        adapter.write_text('# isolated fake-execute entry; never imports or calls a model\n')
        # 只迁移toy目录到强制收集合同；下方fake_execute不会运行这些文件。
        (adapter.parent / 'run_unittest.py').write_text('# never executed fixture\n')
        for filename, _ in EXECUTOR.LOCAL_TEST_GROUPS:
            (adapter.parent / filename).write_text('# never executed fixture\n')
        ui_runner = source / 'tests/ui_window/run_module_checks.py'
        ui_runner.parent.mkdir(parents=True)
        ui_runner.write_text('# mandatory UI entry; fake_execute prevents any execution\n')
        payload = package / 'profiles/test-person/current.json'
        payload.parent.mkdir(parents=True)
        payload.write_text('{"environment":"synthetic","balance_minor":12345}\n')
        self.write_manifest(package, ['profiles/test-person/current.json'])
        return source, package, payload

    def write_manifest(self, package, names):
        manifest = {'environment': 'synthetic', 'files': [
            {'path': name, 'sha256': self.digest(package / name),
             'bytes': (package / name).stat().st_size} for name in names]}
        (package / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return manifest

    def public_fixture(self, source, *, public_only=True):
        package = source / 'outputs/window-progress/rag'
        package.mkdir(parents=True)
        for name in ('public-knowledge.sqlite3', 'public-market.sqlite3'):
            with contextlib.closing(sqlite3.connect(package / name)) as db:
                db.execute('CREATE TABLE public_fixture(id TEXT PRIMARY KEY, body TEXT)')
                db.execute('INSERT INTO public_fixture VALUES(?,?)', ('synthetic', '合成公开事实'))
                db.commit()
        nav = package / 'sources/nav/delivery.json'
        nav.parent.mkdir(parents=True)
        nav.write_text('{"environment":"synthetic","version":1,"nav":[]}\n')
        self.write_public_manifest(package, public_only=public_only)
        return package, nav

    def write_public_manifest(self, package, *, public_only=True):
        delivery = {'public_only': public_only}
        for field, relative in (
                ('knowledge_database', 'public-knowledge.sqlite3'),
                ('market_database', 'public-market.sqlite3'),
                ('nav_delivery', 'sources/nav/delivery.json')):
            path = package / relative
            # 声明的外部绝对路径不存在；只能使用已交接目录内同名文件。
            delivery[field] = {'path': str(self.root / 'untrusted-external' / path.name),
                               'sha256': self.digest(path)}
        (package / 'integration-data-manifest.json').write_text(json.dumps(delivery), encoding='utf-8')
        return delivery

    def evidence(self, **values):
        print('WINDOW_SNAPSHOT_DATA_EVIDENCE ' + json.dumps({
            'test': self.id().split('.')[-1], 'scope': 'temporary_synthetic_only',
            'baseline': 'mocked_no_live_database_or_network', **values}, sort_keys=True,
            ensure_ascii=False), flush=True)

    def test_copy_matches_manifest_and_freezes_base_runtime_and_handoff_data(self):
        source, package, payload = self.source_fixture('source-copy')
        self.public_fixture(source)
        destination = self.root / 'candidate-copy'
        source_hashes = EXECUTOR.copy_snapshot(source, destination)
        self.assertIn('base_runtime/identity.py', source_hashes)
        self.assertEqual(source_hashes, EXECUTOR.source_manifest(destination))
        expected_data = EXECUTOR.data_manifest(source)
        copied_data = EXECUTOR.copy_data_snapshot(source, destination)
        self.assertEqual(copied_data, expected_data)
        self.assertEqual(EXECUTOR.data_manifest(destination), expected_data)
        self.assertEqual(len(expected_data), 6)
        self.assertIn('outputs/window-progress/rag/sources/nav/delivery.json', expected_data)
        self.assertFalse(any('untrusted-external' in name for name in expected_data))
        for name, expected in expected_data.items():
            self.assertEqual((source / name).read_bytes(), (destination / name).read_bytes())
            self.assertEqual(self.digest(destination / name), expected)
        # 输出数据不混入源码摘要，但必须由 handoff 的独立 data_sha256 绑定。
        self.assertEqual(source_hashes, EXECUTOR.source_manifest(destination))
        handoff = self.root / 'handoff-copy.json'
        fields = {name: True for name in ('agent_handed_off', 'existing_semantic_run_finished',
                  'cloud_retest_authorized', 'isolated_fixture_preflight_passed')}
        fields['source_sha256'] = source_hashes
        for declared_data in (None, {'wrong': '0' * 64}, copied_data):
            if declared_data is None:
                fields.pop('data_sha256', None)
            else:
                fields['data_sha256'] = declared_data
            handoff.write_text(json.dumps(fields))
            gate = EXECUTOR.handoff_gate(handoff, source_hashes, copied_data)
            matches = declared_data == copied_data
            self.assertEqual(gate['checks']['data_matches_handoff'], matches)
            self.assertEqual(gate['ready'], matches)
        self.evidence(data_files=len(expected_data), data_hashes=copied_data,
                      base_runtime_frozen=True, public_rag_copy_matches=True,
                      external_absolute_declarations_not_followed=True,
                      absent_and_mismatched_handoff_rejected=True)

    def test_missing_file_changed_hash_and_declared_size_are_rejected(self):
        rejected = []
        for fault in ('missing_file', 'same_size_changed_hash', 'wrong_declared_size',
                      'public_only_false', 'public_only_truthy_string'):
            with self.subTest(fault=fault):
                source, package, payload = self.source_fixture('source-' + fault)
                if fault == 'missing_file':
                    payload.unlink()
                elif fault == 'same_size_changed_hash':
                    original = payload.read_bytes()
                    payload.write_bytes(b'X' * len(original))
                    self.assertEqual(payload.stat().st_size, len(original))
                elif fault == 'wrong_declared_size':
                    manifest = json.loads((package / 'manifest.json').read_text())
                    manifest['files'][0]['bytes'] += 1
                    (package / 'manifest.json').write_text(json.dumps(manifest))
                else:
                    self.public_fixture(source, public_only=False if fault == 'public_only_false' else 'true')
                expected = FileNotFoundError if fault == 'missing_file' else ValueError
                with self.assertRaises(expected):
                    EXECUTOR.data_manifest(source)
                destination = self.root / ('candidate-' + fault)
                with self.assertRaises(expected):
                    EXECUTOR.copy_data_snapshot(source, destination)
                self.assertFalse(destination.exists(), '源包核验失败后不应开始复制')
                rejected.append(fault)
        self.evidence(rejected=rejected, copied_partial_packages=0)

    def test_traversal_absolute_and_symlink_manifest_paths_are_rejected(self):
        rejected = []
        for fault in ('parent_traversal', 'absolute_escape', 'symlink_escape', 'symlink_inside_package'):
            with self.subTest(fault=fault):
                source, package, payload = self.source_fixture('source-' + fault)
                outside = source / 'outside.json'
                outside.write_text('{"synthetic":"outside-package"}\n')
                if fault == 'parent_traversal':
                    declared = '../../outside.json'
                    target = outside
                elif fault == 'absolute_escape':
                    declared = str(outside)
                    target = outside
                else:
                    declared = 'declared-link.json'
                    target = outside if fault == 'symlink_escape' else payload
                    (package / declared).symlink_to(target)
                (package / 'manifest.json').write_text(json.dumps({'files': [{
                    'path': declared, 'sha256': self.digest(target), 'bytes': target.stat().st_size}]}))
                with self.assertRaisesRegex(ValueError, 'invalid_synthetic_package_path'):
                    EXECUTOR.data_manifest(source)
                destination = self.root / ('candidate-' + fault)
                with self.assertRaisesRegex(ValueError, 'invalid_synthetic_package_path'):
                    EXECUTOR.copy_data_snapshot(source, destination)
                self.assertFalse(destination.exists())
                rejected.append(fault)
        # 固定公共交接目录的中间目录也必须保持在包内，不能只查最终文件 symlink。
        with self.subTest(fault='public_parent_symlink_escape'):
            source, _, _ = self.source_fixture('source-public-parent-symlink')
            public, nav = self.public_fixture(source)
            outside_nav = self.root / 'outside-public-nav'
            outside_nav.mkdir()
            (outside_nav / nav.name).write_text('{"environment":"synthetic","outside_package":true}\n')
            nav.unlink()
            nav.parent.rmdir()
            nav.parent.symlink_to(outside_nav, target_is_directory=True)
            self.write_public_manifest(public)
            destination = self.root / 'candidate-public-parent-symlink'
            manifest_accepted = copy_accepted = False
            try:
                EXECUTOR.data_manifest(source)
                manifest_accepted = True
            except ValueError:
                pass
            try:
                EXECUTOR.copy_data_snapshot(source, destination)
                copy_accepted = True
            except ValueError:
                pass
            self.evidence(public_parent_symlink_manifest_accepted=manifest_accepted,
                          public_parent_symlink_copy_accepted=copy_accepted,
                          outside_fixture_copied=(destination / 'outputs/window-progress/rag/sources/nav/delivery.json').exists())
            self.assertFalse(manifest_accepted, '公共包中间目录 symlink 不能让包外文件进入冻结清单')
            self.assertFalse(copy_accepted, '公共包中间目录 symlink 不能让包外文件进入候选副本')
            rejected.append('public_parent_symlink_escape')
        self.evidence(rejected_paths=rejected, escaped_or_symlink_files_copied=0)

    def test_post_execution_data_drift_cannot_be_reported_as_success(self):
        observed = []
        for stage, kind, recalculate_manifest in (
                ('local', 'contest', True), ('browser', 'contest', False),
                ('local', 'public_rag', True), ('browser', 'public_rag', False)):
            with self.subTest(stage=stage, data_kind=kind, recalculate_manifest=recalculate_manifest):
                suffix = stage + '-' + kind
                source, _, _ = self.source_fixture('source-drift-' + suffix)
                if stage == 'local':
                    self.install_original_formal_support(source)
                if kind == 'public_rag':
                    self.public_fixture(source)
                output_root = self.root / ('artifacts-' + suffix)
                output = output_root / 'run'
                calls = []

                def completed_then_mutated(candidate):
                    candidate = Path(candidate)
                    if kind == 'contest':
                        package = candidate / 'outputs/contest-finances'
                        payload = package / 'profiles/test-person/current.json'
                        payload.write_text('{"environment":"synthetic","balance_minor":98765}\n')
                        if recalculate_manifest:
                            self.write_manifest(package, ['profiles/test-person/current.json'])
                    else:
                        package = candidate / 'outputs/window-progress/rag'
                        (package / 'sources/nav/delivery.json').write_text(
                            '{"environment":"synthetic","version":2,"nav":[]}\n')
                        if recalculate_manifest:
                            self.write_public_manifest(package)
                    return {'status': 'exited', 'exit_code': 0}

                def fake_execute(argv, cwd, directory, timeout, environment):
                    calls.append('execute')
                    return completed_then_mutated(cwd)

                def fake_browser(candidate, directory, hold_seconds, environment):
                    calls.append('browser_worker')
                    return completed_then_mutated(candidate)

                args = ['run_window_integration.py', '--stage', stage, '--source-root', str(source),
                        '--out-dir', str(output), '--budget-seconds', '60',
                        '--stage-timeout-seconds', '5', '--browser-hold-seconds', '1']
                with patch.object(EXECUTOR, 'ROOT', source), \
                        patch.object(EXECUTOR, 'OUTPUT_ROOT', output_root), \
                        patch.object(EXECUTOR, 'baseline', return_value={'bank_state': {'synthetic': True}}), \
                        patch.object(EXECUTOR, 'isolated_environment', return_value={}), \
                        patch.object(EXECUTOR, 'execute', side_effect=fake_execute), \
                        patch.object(EXECUTOR, 'browser_worker', side_effect=fake_browser), \
                        patch.object(EXECUTOR.subprocess, 'Popen', side_effect=AssertionError('禁止实际子进程')), \
                        patch.object(EXECUTOR, 'get_public', side_effect=AssertionError('禁止网络或现役基线')), \
                        patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
                    result = EXECUTOR.main()
                report = json.loads((output / 'run.json').read_text())
                record = {'stage': stage, 'data_kind': kind, 'exit_code': result, 'status': report['status'],
                          'planned_exit_code': report['planned_exit_code'],
                          'postflight_error_type': report.get('postflight_error_type'),
                          'candidate_data_matches_frozen': report.get('candidate_data_matches_frozen'),
                          'current_data_matches_frozen': report.get('current_data_matches_frozen')}
                # 即使断言失败也保留独立实测结果，不能用 wrapper 状态覆盖 run.json。
                self.evidence(drift_observation=record)
                self.assertEqual(len(calls), 1)
                self.assertEqual(report['executions'][0]['exit_code'], 0)
                self.assertNotEqual(result, 0, '子任务退出0，但候选源包已改动，主运行不能退出成功')
                self.assertNotEqual(report['planned_exit_code'], 0)
                self.assertNotIn(report['status'], ('engineering_stage_passed',
                    'collected_pending_independent_judgment', 'browser_technical_session_finished'))
                self.assertFalse(report.get('candidate_data_matches_frozen', False))
                self.assertFalse(report['cloud_models_called'])
                observed.append(record)
        self.evidence(subprocess_exit_zero_not_sufficient=True, outcomes=observed)


if __name__ == '__main__':
    unittest.main()
