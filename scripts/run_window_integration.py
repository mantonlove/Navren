#!/usr/bin/env python3
"""Navren 独立集成验收：冻结副本、限时执行、逐案证据和发布核对。

不修改业务实现、既有题意或现场数据。云调用须显式 --stage cloud、模块交接清单和付费预检清单。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / 'outputs/window-progress/integration'
PAID_PREFLIGHT_RELATIVE = Path('outputs/window-progress/agent/baseline-remediation-20261014/preflight')
PAID_PREFLIGHT_SCRIPT_SHA256 = 'efcec9a222f38019090057c5b2306b289f426f68e69314728015cd98b4ef6e17'
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
            '.playwright-cli', 'output', 'outputs', 'archive', 'archives', 'backups',
            'upstream', '设计文档', 'logs'}
SOURCE_DIRS = ('agent_runtime', 'base_runtime', 'simulated_bank', 'local_knowledge', 'financial_methods',
               'financial_mcp', 'financial_data_service',
               'knowledge', 'skills', 'js', 'css', 'assets', 'tests', 'evaluation', 'scripts')
EXACT_SOURCE_FILES = ('dependency-resources/deepseek-v4/tokenizer.json',)
LOCAL_TEST_GROUPS = (
    ('test_cross_module.py', 24), ('test_market_voice_negotiation.py', 12),
    ('test_financial_metrics.py', 10), ('test_four_profiles.py', 6),
    ('test_public_handoff.py', 5), ('test_snapshot_data.py', 6),
    ('test_final_response_guards.py', 4), ('test_semantic_runner_adapter.py', 9),
    ('test_negotiation_consent.py', 2), ('test_negotiation_conditions.py', 5),
    ('test_formal_metric_contract.py', 12), ('test_executor.py', 22),
    ('test_test_collection.py', 6), ('test_service_response_guard.py', 4),
    ('test_service_transport_commit.py', 4), ('test_agent_budget_compact.py', 96),
    ('test_finance_adapter_handoff.py', 12), ('test_finance_adapter_remaining.py', 29),
    ('test_public_probe_runner.py', 26), ('test_formal_business_runner.py', 24),
    ('test_service_history_deadline.py', 4), ('test_finance_probe_runner.py', 11),
    ('test_finance_adapter_capabilities.py', 6), ('test_legacy_regression_adapter.py', 4),
    ('test_regression_cloud_runner.py', 15))
UI_REGRESSIONS = ('tests/test_api_response.js', 'tests/test_voice_frontend.js',
                  'tests/test_i18n.js', 'tests/test_account_workspace.js',
                  'tests/test_account_workspace_calendar.js', 'tests/test_market_deck.js',
                  'tests/test_market_history.js', 'tests/test_discover_workspace.js',
                  'tests/test_market_weekly_ui.js')
# semantic_oracle.py 原十题73项事前预检；按case计数，不拆合并负控。
SELF_TEST_CHECK_COUNTS = dict(zip(('S01', 'S02', 'S03', 'S04', 'S05', 'S06', 'S07', 'S08', 'S09', 'S10'),
                                  (12, 4, 10, 8, 7, 5, 4, 2, 12, 9)))


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_json(path):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key:' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          ValueError('invalid_json_constant:' + value)))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    fd, name = tempfile.mkstemp(prefix='.integration-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def source_files(root):
    root = Path(root).resolve()
    paths = {p for p in root.iterdir() if p.is_file() and p.suffix in ('.py', '.html', '.json', '.txt')}
    for name in SOURCE_DIRS:
        directory = root / name
        if not directory.exists():
            continue
        for current, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in EXCLUDED]
            paths.update(Path(current) / f for f in files if not f.startswith('.') and
                         Path(f).suffix.lower() not in ('.pyc', '.sqlite3', '.sqlite', '.db', '.log'))
    for name in EXACT_SOURCE_FILES:
        path = root / name
        if not path.exists() and not path.is_symlink():
            if (root / 'agent_runtime/request_budget.py').is_file():
                raise ValueError('required_source_file_missing:' + name)
            continue
        if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
            raise ValueError('source_file_redirected:' + name)
        paths.add(path)
    return sorted(p for p in paths if p.name not in ('voice.local.json', 'config.local.json') and not p.is_symlink())


def source_manifest(root):
    root = Path(root).resolve()
    return {str(p.relative_to(root)): sha(p) for p in source_files(root)}


def app_release(root):
    root = Path(root)
    files = [root / name for name in sorted(('index.html', 'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html'))]
    files += sorted((root / 'css').glob('*.css')) + sorted((root / 'js').glob('*.js'))
    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return 'navren-' + h.hexdigest()[:12]


def get_public(url):
    try:
        with urlopen(url, timeout=5) as response:
            return {'status': response.status, 'data': json.load(response)}
    except (OSError, ValueError, URLError) as error:
        return {'status': None, 'error_type': type(error).__name__}


def database_digest(path):
    """只保存配置、数量和摘要，不把现役私人字段写入普通验收产物。"""
    path = Path(path)
    if not path.exists():
        return {'path': str(path), 'exists': False}
    result = {'path': str(path), 'exists': True, 'mode': 'read_only', 'tables': {}}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            db.execute('BEGIN')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('workspaces', 'accounts', 'profiles', 'holdings', 'orders', 'operations', 'fund_holds'):
                if table not in tables:
                    continue
                rows = db.execute('SELECT * FROM "' + table + '"').fetchall()
                h = hashlib.sha256()
                for row in sorted(json.dumps(list(r), ensure_ascii=False, default=str) for r in rows):
                    h.update(row.encode())
                    h.update(b'\n')
                result['tables'][table] = {'rows': len(rows), 'sha256': h.hexdigest()}
            result['schema_version'] = db.execute('PRAGMA schema_version').fetchone()[0]
            db.rollback()
    except sqlite3.Error as error:
        result['error_type'] = type(error).__name__
    return result


def baseline_bank_path():
    default = Path.home() / 'Library/Application Support/Qingzhou/simulated-bank.sqlite3'
    return Path(os.environ.get('NAVREN_INTEGRATION_LIVE_BANK_DB') or os.environ.get('NAVREN_SIM_DB')
                or os.environ.get('QINGZHOU_SIM_DB') or default).resolve()


def baseline(root, service_url):
    dbpath = baseline_bank_path()
    config = {'database_path': str(dbpath), 'configured_overrides': {
        key: bool(os.environ.get(key)) for key in ('NAVREN_SIM_DB', 'NAVREN_KNOWLEDGE_DB',
        'NAVREN_MARKET_DB', 'NAVREN_FUND_CACHE', 'NAVREN_PERSONAL_DATA_KEY')},
        'secret_values_recorded': False,
        'database_path_basis': 'explicit_read_only_baseline_binding' if os.environ.get('NAVREN_INTEGRATION_LIVE_BANK_DB')
                               else 'environment_or_source_default_not_process_environment'}
    processes = subprocess.run(['ps', '-axo', 'pid,ppid,lstart,command'], capture_output=True, text=True, check=True)
    relevant = []
    for line in processes.stdout.splitlines():
        if 'server.py' in line and '/Navren/' in line and 'ps -axo' not in line:
            relevant.append(' '.join(line.split()[:7]) + ' server.py')
        elif 'run_skill_acceptance.py' in line and 'ps -axo' not in line and 'rg ' not in line:
            # 不记录可能包含提示词/密钥的完整命令。
            relevant.append(' '.join(line.split()[:7]) + ' run_skill_acceptance.py')
    return {'observed_at': now(), 'service_url': service_url,
            'app': get_public(service_url + '/api/app-version'),
            'voice': get_public(service_url + '/api/speech/status'),
            'market': get_public(service_url + '/api/market/status'),
            'expected_static_release': app_release(root), 'processes': relevant,
            'config': config, 'bank_state': database_digest(dbpath),
            'models': {'primary': 'AiPing / DeepSeek-V4-Flash', 'reviewer': 'AiPing / GLM-5.3-Flash',
                       'configuration_basis': 'production_source_defaults', 'total_cost': None}}


def copy_snapshot(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    before = source_manifest(root)
    destination.mkdir(parents=True)
    for name, expected in before.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
        if sha(target) != expected:
            raise ValueError('source_changed_during_snapshot:' + name)
    after = source_manifest(root)
    if before != after:
        raise ValueError('source_changed_during_snapshot')
    return before


def data_manifest(root):
    root = Path(root).resolve()
    package = Path(root) / 'outputs/contest-finances'
    manifest_path = package / 'manifest.json'
    result = {}
    if manifest_path.exists():
        declared = json.loads(manifest_path.read_text())['files']
        result['outputs/contest-finances/manifest.json'] = sha(manifest_path)
        for row in declared:
            declared_path = package / row['path']
            path = declared_path.resolve()
            if not path.is_relative_to(package.resolve()) or declared_path.is_symlink():
                raise ValueError('invalid_synthetic_package_path')
            actual = sha(path)
            if actual != row['sha256'] or path.stat().st_size != row['bytes']:
                raise ValueError('synthetic_source_manifest_mismatch:' + row['path'])
            result[str(path.relative_to(root))] = actual
    public_package = root / 'outputs/window-progress/rag'
    public_manifest = public_package / 'integration-data-manifest.json'
    if public_manifest.exists():
        if (not public_package.resolve().is_relative_to(root) or public_manifest.is_symlink()
                or not public_manifest.resolve().is_relative_to(public_package.resolve())):
            raise ValueError('invalid_public_package_path')
        delivery = json.loads(public_manifest.read_text())
        if delivery.get('public_only') is not True:
            raise ValueError('public_package_attestation_missing')
        result[str(public_manifest.relative_to(root))] = sha(public_manifest)
        for name in ('knowledge_database', 'market_database', 'nav_delivery'):
            row = delivery[name]
            # 只复制交接目录中的固定文件名，绝不依manifest绝对路径读取其它库。
            relative = Path('sources/nav') if name == 'nav_delivery' else Path()
            path = public_package / relative / Path(row['path']).name
            if (path.is_symlink() or not path.resolve().is_relative_to(public_package.resolve())
                    or sha(path) != row['sha256']):
                raise ValueError('public_handoff_manifest_mismatch:' + name)
            result[str(path.relative_to(root))] = row['sha256']
    return result


def copy_data_snapshot(root, destination):
    before = data_manifest(root)
    for name, expected in before.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / name, target)
        if sha(target) != expected:
            raise ValueError('synthetic_source_changed_during_snapshot')
    if before != data_manifest(root) or before != data_manifest(destination):
        raise ValueError('synthetic_source_changed_during_snapshot')
    return before


def auxiliary_manifest(root):
    """固定题目、接口合同及批准方案另冻，保留43项数据包的精确合同。"""
    root = Path(root).resolve()
    result = {}
    for name in ('outputs/window-progress/rag/manual-question-set.json',
                 'outputs/window-progress/finance/implementation/adapter-contract.md',
                 'outputs/window-progress/finance/solution-review/solution-v2.md'):
        relative = Path(name)
        path = root / relative
        if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(root)):
            raise ValueError('invalid_auxiliary_source_path:' + name)
        if path.is_file():
            result[name] = sha(path)
    return result


def copy_auxiliary_snapshot(root, destination):
    before = auxiliary_manifest(root)
    for name, expected in before.items():
        target = Path(destination) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / name, target)
        if sha(target) != expected:
            raise ValueError('auxiliary_source_copy_changed:' + name)
    if auxiliary_manifest(root) != before:
        raise ValueError('auxiliary_source_changed_during_copy')
    return before


def formal_support_manifest(root, *, required=False):
    """正式fixture原件另冻8项，保留既有43数据/3辅助清单。"""
    root = Path(root).resolve()
    contract_path = root / 'evaluation/window_integration/formal_support_manifest.json'
    if not contract_path.exists():
        if required:
            raise ValueError('formal_support_contract_missing')
        return {}
    if contract_path.is_symlink() or not contract_path.resolve().is_relative_to(root):
        raise ValueError('formal_support_contract_path_invalid')
    contract = json.loads(contract_path.read_text())
    basis = root / 'evaluation/window_integration/formal_business_tasks.json'
    if (not basis.is_file() or basis.is_symlink() or not basis.resolve().is_relative_to(root)
            or sha(basis) != '99eaeba3d2519f63c130b50c8a696809d60e68486e578546104e78a70a4e1d24'):
        raise ValueError('formal_support_original_authority_missing_or_changed')
    seed = json.loads(basis.read_text())['fixture_contract']['new_closed_seed_v1']
    expected = {seed['core_path']: seed['core_sha256'], **{
        str(Path(seed['generation_sources_archive']) / name): digest
        for name, digest in seed['generation_source_sha256'].items()}}
    rows = contract.get('files')
    if (contract.get('contract') != 'navren.formal_support_inputs.v1'
            or type(contract.get('file_count')) is not int or contract['file_count'] != 8
            or not isinstance(rows, list) or len(rows) != 8
            or any(not isinstance(row, dict) or not isinstance(row.get('path'), str)
                   or not isinstance(row.get('sha256'), str) for row in rows)
            or len({row['path'] for row in rows}) != 8
            or {row['path']: row['sha256'] for row in rows} != expected):
        raise ValueError('formal_support_entries_differ_from_original_authority')
    for name, digest in expected.items():
        path = root / name
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root))):
            raise ValueError('formal_support_path_invalid:' + name)
        if not path.is_file() or sha(path) != digest:
            raise ValueError('formal_support_original_missing_or_changed:' + name)
    return expected


def copy_formal_support_snapshot(root, destination, *, required=False):
    before = formal_support_manifest(root, required=required)
    for name, digest in before.items():
        target = Path(destination) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / name, target)
        if sha(target) != digest:
            raise ValueError('formal_support_copy_changed:' + name)
    if (formal_support_manifest(root, required=required) != before
            or formal_support_manifest(destination, required=required) != before):
        raise ValueError('formal_support_changed_during_copy')
    return before


def terminate_group(process):
    group = process.pid
    def group_alive():
        process.poll()  # 先回收组长，macOS对只剩僵尸的组可能返回EPERM。
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 只读确认本次创建的组是否还有活成员，不以组长退出代替子进程退出。
            members = subprocess.run(['ps', '-axo', 'pid,pgid,stat'],
                                     capture_output=True, text=True, check=True)
            return any(len(fields := line.split()) >= 3 and fields[1] == str(group)
                       and not fields[2].startswith('Z') for line in members.stdout.splitlines())
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3
    while group_alive() and time.monotonic() < deadline:
        process.poll()  # 回收已退出的组长，但不将其退出当作整个进程组消失。
        time.sleep(.05)
    if group_alive():
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def execute(argv, cwd, directory, timeout, environment):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    record = {'argv': [str(v) for v in argv], 'cwd': str(cwd), 'started_at': now(),
              'timeout_seconds': timeout, 'status': 'running', 'exit_code': None}
    atomic_json(directory / 'execution.json', record)
    started = time.monotonic()
    try:
        with (directory / 'stdout.log').open('wb') as stdout, (directory / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=stdout, stderr=stderr, start_new_session=True)
            record['pid'] = process.pid
            atomic_json(directory / 'execution.json', record)
            try:
                record['exit_code'] = process.wait(timeout=timeout)
                record['status'] = 'exited'
            except subprocess.TimeoutExpired:
                record['status'] = 'timed_out'
                try:
                    terminate_group(process)
                except OSError as error:
                    record['cleanup_error_type'] = type(error).__name__
                record['exit_code'] = process.poll()
    except OSError as error:
        record.update(status='launch_failed', error_type=type(error).__name__)
    finally:
        record.update(completed_at=now(), duration_seconds=round(time.monotonic() - started, 3))
        atomic_json(directory / 'execution.json', record)
    return record


def execution_passed(record):
    """超时清理的实际退出码可为0；只有自行正常结束才完成关卡。"""
    return (record.get('status') == 'exited' and record.get('exit_code') == 0
            and record.get('result_contract_passed') is not False)


def structured_test_summary(path, minimum):
    """实际计数和收集完整性均是必需门；退出0不能替代缺失结果。"""
    if not path.is_file():
        return {'tests_run': None, 'minimum_tests': minimum, 'complete': False,
                'contract_errors': ['result_missing']}
    raw = json.loads(path.read_text())
    errors = []
    if not isinstance(raw, dict):
        return {'tests_run': None, 'minimum_tests': minimum, 'complete': False,
                'contract_errors': ['result_not_object']}
    fields = ('tests_run', 'failures', 'errors', 'skipped', 'expected_failures',
              'unexpected_successes')
    if any(type(raw.get(key)) is not int or raw[key] < 0 for key in fields):
        errors.append('actual_counts_missing_or_invalid')
    elif raw['tests_run'] < minimum or any(raw[key] != 0 for key in fields[1:]):
        errors.append('collection_incomplete_or_unsuccessful')
    if raw.get('complete') is not True or type(raw.get('planned_exit_code')) is not int or raw['planned_exit_code'] != 0:
        errors.append('result_not_complete')
    return {**raw, 'minimum_tests': minimum, 'raw_complete': raw.get('complete'),
            'complete': not errors, 'contract_errors': errors,
            'raw_report': str(path), 'raw_sha256': sha(path)}


def ui_test_summary(candidate, path):
    """逐个核对真实Node脚本退出与冻结文件，禁止空汇总获得完整G1。"""
    expected = sorted(str(p.relative_to(candidate)) for p in
                      (candidate / 'tests/ui_window').glob('test_*.js')) + list(UI_REGRESSIONS)
    if not path.is_file():
        return {'tests_run': 0, 'minimum_tests': 22, 'failures': 0, 'errors': 1,
                'skipped': 0, 'expected_failures': 0, 'unexpected_successes': 0,
                'complete': False, 'scope': 'node_scripts', 'reason': 'result_missing'}
    raw = json.loads(path.read_text())
    rows = raw.get('results', [])
    complete = (len(expected) >= 22 and len(rows) == len(expected)
                and sorted(row.get('test', '') for row in rows) == sorted(expected)
                and raw.get('frontend_stable_during_checks') is True
                and raw.get('total') == len(rows) and raw.get('passed') == len(rows)
                and all(row.get('exit_code') == 0 and row.get('timeout') is False
                        and (candidate / row['test']).is_file()
                        and row.get('test_sha256') == sha(candidate / row['test']) for row in rows))
    return {'tests_run': len(rows), 'minimum_tests': 22, 'scope': 'node_scripts',
            'failures': sum(row.get('exit_code') != 0 for row in rows), 'errors': 0,
            'skipped': 0, 'expected_failures': 0, 'unexpected_successes': 0,
            'count_basis': 'one_actual_node_process_per_frozen_script; protocol_has_no_skip_or_expected_failure_mode',
            'complete': complete, 'raw_report': str(path), 'raw_sha256': sha(path)}


def local_case_summary(path, dataset, phase):
    """封闭本地协议按实际原题记录计数，不把结构替身计为金融语义成绩。"""
    base = {'scope': 'local_fixture_cases', 'semantic_grading': 'not_evaluated',
            'count_basis': 'unique_actual_case_records; closed_protocol_has_no_skip_or_expected_failure_branch'}
    try:
        cases = json.loads(dataset.read_text())['cases']
        ids = [case['id'] for case in cases]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('dataset_ids_invalid')
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get('records'), list):
            raise ValueError('case_result_schema_invalid')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {**base, 'tests_run': None, 'complete': False,
                'contract_errors': ['missing_or_invalid_case_artifact:' + type(error).__name__]}
    rows = raw['records']
    seen = [row.get('id') if isinstance(row, dict) else None for row in rows]
    contracts = []
    if seen != ids:
        contracts.append('original_case_collection_incomplete_or_reordered')
    if phase == 'independent-oracle':
        if (raw.get('scope') != 'offline_fixture_self_test' or type(raw.get('model_calls')) is not int
                or raw['model_calls'] != 0 or raw.get('semantic_pass_count', 'missing') is not None):
            contracts.append('self_test_protocol_invalid')
    else:
        if (raw.get('run_status') != 'collection_complete' or raw.get('collection_complete') is not True
                or raw.get('complete') is not True or raw.get('invalidated') is not False
                or raw.get('source_unchanged') is not True or raw.get('dataset_unchanged') is not True
                or type(raw.get('planned_exit_code')) is not int or raw['planned_exit_code'] != 0
                or raw.get('selected_case_ids') != ids or type(raw.get('expected_case_count')) is not int
                or raw['expected_case_count'] != len(ids) or raw.get('dataset_sha256') != sha(dataset)):
            contracts.append('runner_collection_contract_invalid')
        if phase == 'oracle' and raw.get('offline') is not True:
            contracts.append('offline_fixture_protocol_invalid')
        if phase == 'process' and (raw.get('offline') is not False or raw.get('process_preflight') is not True):
            contracts.append('real_http_scripted_protocol_invalid')
    outcomes = []
    required = {'load_skill', 'account_summary', 'personal_graph', 'knowledge_status', 'budget', 'cashflow'}
    by_id = {case['id']: case for case in cases}
    for row in rows:
        reasons, failures = [], []
        if not isinstance(row, dict) or row.get('id') not in by_id:
            outcomes.append({'id': row.get('id') if isinstance(row, dict) else None,
                             'status': 'error', 'reasons': ['unknown_case_or_invalid_row']})
            continue
        if phase == 'independent-oracle':
            for key, wanted in (('preflight_status', 'passed'), ('snapshot_stable', True),
                                ('structural_control_status', 'needs_human_review'),
                                ('negative_controls_passed', True)):
                if key not in row or type(row[key]) is not type(wanted):
                    reasons.append('missing_or_invalid:' + key)
                elif row[key] != wanted:
                    failures.append(key)
            required_checks = SELF_TEST_CHECK_COUNTS.get(row['id'].split('_', 1)[0])
            if (type(row.get('check_count')) is not int or required_checks is None
                    or row['check_count'] != required_checks or not isinstance(row.get('failed_checks'), list)):
                reasons.append('preflight_checks_invalid')
            elif row['failed_checks']:
                failures.append('failed_checks')
            if row.get('error'):
                reasons.append('self_test_error')
        else:
            if row.get('runner_failure') or row.get('cleanup_failure'):
                reasons.append('actual_runner_or_cleanup_error')
            facts = [fact for fact in by_id[row['id']]['expected']['facts'] if 'tool' in fact]
            actual = row.get('oracle')
            if not isinstance(actual, list) or len(actual) != len(facts) or not facts:
                reasons.append('factual_checks_missing_or_short')
            else:
                for observed, frozen in zip(actual, facts):
                    if not isinstance(observed, dict) or any(
                            key not in observed or type(observed[key]) is not type(frozen[key])
                            or observed[key] != frozen[key] for key in ('label', 'tool', 'path', 'expected')):
                        reasons.append('frozen_fact_binding_invalid')
                    elif 'actual' not in observed or type(observed.get('passed')) is not bool:
                        reasons.append('actual_factual_check_missing')
                    elif (type(observed['actual']) is not type(frozen['expected'])
                            or observed['actual'] != frozen['expected'] or observed['passed'] is not True):
                        failures.append('frozen_fact_failed')
            before, after = row.get('before'), row.get('after')
            snapshot_types = {'total_cash_minor': int, 'profile': dict, 'orders': list, 'holdings': list,
                              'fund_holds': list, 'observations': int, 'memories': list}
            if not all(isinstance(value, dict) and all(type(value.get(key)) is kind
                       for key, kind in snapshot_types.items()) for value in (before, after)):
                reasons.append('state_snapshots_missing_or_invalid')
            else:
                if any(before[key] != after[key] for key in ('total_cash_minor', 'profile', 'orders', 'holdings', 'fund_holds')):
                    failures.append('actual_financial_state_or_profile_changed')
                deltas = {'observation_delta': after['observations'] - before['observations'],
                          'memory_delta': len(after['memories']) - len(before['memories'])}
                for key, value in deltas.items():
                    if type(row.get(key)) is not int:
                        reasons.append('actual_delta_missing_or_invalid:' + key)
                    elif row[key] != value:
                        failures.append('actual_delta_does_not_match_snapshots:' + key)
                for fact in by_id[row['id']]['expected']['facts']:
                    if 'tool' in fact:
                        continue
                    key = ('observation_delta' if fact.get('table') == 'profile_observations'
                           else 'memory_delta' if fact.get('method') == 'g.memories.state(w)' and fact.get('path') == 'items' else None)
                    if key is None or fact.get('metric') != 'count_delta' or type(fact.get('expected')) is not int:
                        reasons.append('non_tool_frozen_fact_unhandled')
                    elif type(row.get(key)) is not int or row[key] != fact['expected']:
                        failures.append('non_tool_frozen_fact_failed:' + key)
                    elif key == 'memory_delta' and fact['expected'] == 0 and before['memories'] != after['memories']:
                        failures.append('non_tool_memory_contents_changed')
            for key in ('no_financial_execution', 'no_action_cards', 'profile_unchanged'):
                if type(row.get(key)) is not bool:
                    reasons.append('missing_or_invalid:' + key)
                elif row[key] is not True:
                    failures.append(key)
            if phase == 'process':
                if type(row.get('http_status')) is not int:
                    reasons.append('actual_http_status_missing')
                elif row['http_status'] != 200:
                    failures.append('actual_http_status')
                for key in ('technical_completed', 'process_preflight_completed'):
                    if type(row.get(key)) is not bool:
                        reasons.append('missing_or_invalid:' + key)
                    elif row[key] is not True:
                        failures.append(key)
                result = row.get('result')
                trace = result.get('agent_runtime', {}).get('trace') if isinstance(result, dict) else None
                if not isinstance(trace, list) or any(not isinstance(item, dict) for item in trace):
                    reasons.append('actual_process_trace_missing')
                elif not required <= {item.get('tool') for item in trace if item.get('status') == 'completed'}:
                    failures.append('required_tools_not_actually_completed')
                if row.get('integration_failures') != []:
                    reasons.append('process_integration_errors_or_missing')
        outcomes.append({'id': row['id'], 'status': 'error' if reasons else ('failed' if failures else 'passed'),
                         'reasons': reasons + failures})
    counters = {'tests_run': len({item for item in seen if isinstance(item, str)}),
                'failures': sum(row['status'] == 'failed' for row in outcomes),
                'errors': sum(row['status'] == 'error' for row in outcomes), 'skipped': 0,
                'expected_failures': 0, 'unexpected_successes': 0}
    complete = not contracts and all(row['status'] == 'passed' for row in outcomes) and seen == ids
    return {**base, **counters, 'minimum_tests': len(ids), 'complete': complete,
            'planned_exit_code': 0 if complete else 1, 'contract_errors': contracts,
            'case_outcomes': outcomes, 'raw_report': str(path), 'raw_sha256': sha(path),
            'dataset_sha256': sha(dataset)}


def runtime_summary():
    packages = {}
    for name in ('cryptography', 'websocket-client'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {'executable': sys.executable, 'version': sys.version, 'packages': packages}


def isolated_environment(candidate, state):
    from cryptography.fernet import Fernet
    env = os.environ.copy()
    # 真正现役库的观测路径只供只读基线，不冒用下面的临时默认库。
    live_bank = baseline_bank_path()
    state = Path(state).resolve()
    for name in ('NAVREN_SIM_DB', 'QINGZHOU_SIM_DB', 'NAVREN_KNOWLEDGE_DB', 'NAVREN_MARKET_DB',
                 'NAVREN_FUND_CACHE', 'PYTHONPATH', 'NAVREN_PERSONAL_DATA_KEY', 'NAVREN_ALLOW_CLOUD_ASR'):
        env.pop(name, None)
    # 此临时密钥仅存在进程环境，不能进入manifest或日志。
    env['NAVREN_PERSONAL_DATA_KEY'] = Fernet.generate_key().decode()
    env['NAVREN_INTEGRATION_LIVE_BANK_DB'] = str(live_bank)
    env['NAVREN_SIM_DB'] = env['QINGZHOU_SIM_DB'] = str(state / 'bank.sqlite3')
    env['NAVREN_KNOWLEDGE_DB'] = str(state / 'knowledge.sqlite3')
    env['NAVREN_MARKET_DB'] = str(state / 'market.sqlite3')
    env['NAVREN_FUND_CACHE'] = str(state / 'funds')
    env['PYTHONPATH'] = os.pathsep.join((str(candidate), str(candidate / 'tests')))
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['NAVREN_AGENT_TIMEOUT'] = '105'
    return env


def imported_results(path, out):
    """接收已有窗口结果；保留来源版本，绝不升级为当前冻结成绩。"""
    path = Path(path).resolve()
    d = json.loads(path.read_text())
    target = out / 'received-semantic-results.json'
    atomic_json(target, d)
    return {'source_path': str(path), 'source_sha256': sha(path), 'received_at': now(),
            'collection_complete': d.get('collection_complete'), 'run_status': d.get('run_status'),
            'source_sha256': d.get('source_sha256', {}), 'scope': 'received_prior_version_not_current_retest',
            'case_count': len(d.get('records', [])), 'summary': d.get('summary'),
            'semantic_grading': d.get('semantic_grading'), 'raw_copy': str(target)}


def handoff_gate(path, manifest, dataset_manifest=None):
    if not path:
        return {'ready': False, 'reason': 'no_module_handoff_manifest'}
    h = json.loads(Path(path).read_text())
    checks = {key: h.get(key) is True for key in ('agent_handed_off', 'existing_semantic_run_finished',
                                               'cloud_retest_authorized', 'isolated_fixture_preflight_passed')}
    checks['source_matches_handoff'] = h.get('source_sha256') == manifest
    if dataset_manifest:
        checks['data_matches_handoff'] = h.get('data_sha256') == dataset_manifest
    return {'ready': all(checks.values()), 'checks': checks, 'handoff_sha256': sha(path)}


def paid_preflight_gate(root, manifest_path, frozen_source_manifest, output_dir, timeout_seconds=30):
    """Run the delivered read-only gate and fail closed before any cloud runner starts."""
    root = Path(root).resolve()
    output_dir = Path(output_dir).resolve()
    script = root / PAID_PREFLIGHT_RELATIVE / 'preflight.py'
    delivery = root / PAID_PREFLIGHT_RELATIVE / 'delivery.json'
    result_path = output_dir / 'paid-preflight-result.json'
    script_snapshot = output_dir / 'paid-preflight-program.py'
    execution_path = output_dir / 'paid-preflight-execution.json'
    record = {'required': True, 'ready': False, 'stage': 'paid',
              'provider_start_attempts_before_gate': 0, 'returncode': None,
              'manifest_path': str(manifest_path) if manifest_path else None}

    def finish(reason):
        record['reason'] = reason
        record['status'] = 'blocked'
        atomic_json(execution_path, record)
        return record

    try:
        if manifest_path is None:
            return finish('paid_preflight_manifest_required')
        manifest_path = Path(manifest_path)
        if (manifest_path.is_symlink() or not manifest_path.is_file()
                or not manifest_path.resolve().is_relative_to(root)):
            return finish('paid_preflight_manifest_missing_or_outside_source_root')
        manifest_path = manifest_path.resolve()
        if script.is_symlink() or not script.is_file() or not script.resolve().is_relative_to(root):
            return finish('paid_preflight_component_hash_mismatch')
        script_bytes = script.read_bytes()
        script_sha256 = hashlib.sha256(script_bytes).hexdigest()
        if script_sha256 != PAID_PREFLIGHT_SCRIPT_SHA256:
            return finish('paid_preflight_component_hash_mismatch')
        if (delivery.is_symlink() or not delivery.is_file() or not delivery.resolve().is_relative_to(root)):
            return finish('paid_preflight_delivery_receipt_missing')
        delivery_record = strict_json(delivery)
        if (delivery_record.get('schema') != 'navren.baseline-preflight-delivery.v1'
                or delivery_record.get('status') != 'component_verified_pending_integration_adoption'
                or delivery_record.get('files', {}).get('preflight.py') != PAID_PREFLIGHT_SCRIPT_SHA256
                or delivery_record.get('models_called') != 0
                or delivery_record.get('external_reviews') != 0
                or delivery_record.get('resource_preparation_only') is not True):
            return finish('paid_preflight_delivery_contract_invalid')
        if result_path.exists() or result_path.is_symlink():
            return finish('paid_preflight_output_already_exists')

        delivery_sha256 = sha(delivery)
        manifest_sha256 = sha(manifest_path)
        if script_snapshot.exists() or script_snapshot.is_symlink():
            return finish('paid_preflight_program_snapshot_already_exists')
        with script_snapshot.open('xb') as stream:
            stream.write(script_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if sha(script_snapshot) != script_sha256:
            return finish('paid_preflight_program_snapshot_hash_mismatch')
        record.update(status='running', script_sha256=script_sha256,
                      delivery_sha256=delivery_sha256, manifest_sha256=manifest_sha256,
                      script_snapshot=str(script_snapshot), result_path=str(result_path))
        atomic_json(execution_path, record)
        command = [sys.executable, '-B', str(script_snapshot), '--root', str(root),
                   '--manifest', str(manifest_path), '--out', str(result_path)]
        started = time.monotonic()
        try:
            completed = subprocess.run(command, cwd=root,
                env={'PATH': os.defpath, 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=max(1, int(timeout_seconds)), check=False)
        except subprocess.TimeoutExpired as error:
            def output_bytes(value):
                if isinstance(value, str):
                    return value.encode()
                return value or b''
            record.update(returncode=None, status='timed_out',
                          duration_seconds=round(time.monotonic() - started, 3),
                          stdout_sha256=hashlib.sha256(output_bytes(error.stdout)).hexdigest(),
                          stderr_sha256=hashlib.sha256(output_bytes(error.stderr)).hexdigest())
            return finish('paid_preflight_timed_out')
        record.update(returncode=completed.returncode,
                      duration_seconds=round(time.monotonic() - started, 3),
                      stdout_sha256=hashlib.sha256(completed.stdout.encode()).hexdigest(),
                      stderr_sha256=hashlib.sha256(completed.stderr.encode()).hexdigest())
        if sha(delivery) != delivery_sha256:
            return finish('paid_preflight_component_changed_during_execution')
        if sha(manifest_path) != manifest_sha256:
            return finish('paid_preflight_manifest_changed_during_execution')
        if (result_path.is_symlink() or not result_path.is_file()
                or not result_path.resolve().is_relative_to(output_dir)):
            return finish('paid_preflight_result_missing_or_invalid_path')
        result = strict_json(result_path)
        record.update(result_sha256=sha(result_path), result_schema=result.get('schema'),
                      result_status=result.get('status'), result_stage=result.get('stage'),
                      paid_start_allowed=result.get('paid_start_allowed'),
                      result_manifest_sha256=result.get('manifest_sha256'),
                      blockers=result.get('blockers', []),
                      models_called=result.get('models_called'),
                      external_reviews=result.get('external_reviews'))

        if completed.returncode != 0:
            return finish('paid_preflight_nonzero_exit')
        if (result.get('schema') != 'navren.baseline-preflight.v1'
                or result.get('stage') != 'paid'
                or result.get('paid_start_allowed') is not True
                or result.get('manifest_sha256') != manifest_sha256
                or result.get('status') != 'PRECHECK_PENDING'
                or result.get('models_called') != 0
                or result.get('external_reviews') != 0
                or result.get('overall_accepted') is not False
                or result.get('release_allowed') is not False):
            return finish('paid_preflight_result_did_not_authorize_start')

        paid_manifest = strict_json(manifest_path)
        if paid_manifest.get('source_root') != str(root):
            return finish('paid_preflight_source_root_mismatch')
        source_ref = paid_manifest['source_manifest']
        frozen_path = Path(source_ref['path'])
        if not frozen_path.is_absolute():
            frozen_path = root / frozen_path
        if (frozen_path.is_symlink() or not frozen_path.is_file()
                or not frozen_path.resolve().is_relative_to(root)
                or sha(frozen_path) != source_ref['sha256']):
            return finish('paid_preflight_source_manifest_missing_or_changed')
        frozen_manifest = strict_json(frozen_path)
        if frozen_manifest != frozen_source_manifest:
            return finish('paid_preflight_source_differs_from_executor_snapshot')
        entrypoint = paid_manifest['entrypoint']
        if frozen_manifest.get(entrypoint['path']) != entrypoint['sha256']:
            return finish('paid_preflight_entrypoint_differs_from_executor_snapshot')

        record.update(status='ready', ready=True, source_manifest_sha256=source_ref['sha256'],
                      reason=None)
        atomic_json(execution_path, record)
        return record
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return finish(type(error).__name__ + ':' + str(error)[:240])


def semantic_summary(path):
    if not path.exists():
        return {'status': 'artifact_missing'}
    d = json.loads(path.read_text())
    records = d.get('records', [])
    durations = sorted(r['duration_seconds'] for r in records if isinstance(r.get('duration_seconds'), (int, float)))
    def quantile(q):
        if not durations:
            return None
        index = int((len(durations)-1)*q)
        return durations[index] + (durations[min(index+1, len(durations)-1)]-durations[index])*(((len(durations)-1)*q)-index)
    return {'status': d.get('run_status'), 'complete': d.get('collection_complete'),
            'cases': len(records), 'technical_completed': sum(r.get('technical_completed') is True for r in records),
            'no_financial_execution': d.get('summary', {}).get('no_financial_execution'),
            'source_unchanged': d.get('source_unchanged'), 'dataset_unchanged': d.get('dataset_unchanged'),
            'semantic_grading': 'pending_independent_review', 'duration_p50_seconds': quantile(.5),
            'duration_p95_seconds': quantile(.95), 'duration_n': len(durations), 'total_model_cost': None,
            'cost_basis': 'no_verified_price_or_complete_two_model_usage_basis'}


def browser_worker(candidate, directory, hold_seconds, environment):
    """独立四画像和冻结市场实例，云模型关闭；服务关闭不等于实屏通过。"""
    helper = candidate / 'tests/window_integration/browser_fixture.py'
    if not helper.is_file():
        raise ValueError('browser_fixture_helper_missing')
    return execute([sys.executable, '-B', str(helper), '--out-dir', str(directory),
                    '--hold-seconds', str(hold_seconds)], candidate,
                   directory.parent / 'browser-execution', hold_seconds + 30, environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('baseline', 'preflight', 'local', 'cloud', 'browser', 'oracle-self-test'), default='baseline')
    parser.add_argument('--run-id', default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--service-url', default='http://127.0.0.1:18767')
    parser.add_argument('--receive-results', type=Path)
    parser.add_argument('--handoff', type=Path)
    parser.add_argument('--paid-preflight-manifest', type=Path,
                        help='付费 cloud 阶段必需；传入 Agent 已交付的付费事前清单')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--case-ids', default='')
    parser.add_argument('--budget-seconds', type=int)
    parser.add_argument('--stage-timeout-seconds', type=int)
    parser.add_argument('--browser-hold-seconds', type=int, default=600)
    args = parser.parse_args()
    if args.budget_seconds is None:
        args.budget_seconds = {'local': 900, 'cloud': 3000}.get(args.stage, 1800)
    if args.stage_timeout_seconds is None:
        args.stage_timeout_seconds = 1500 if args.stage == 'cloud' else 360
    if not 1 <= args.rounds <= 3 or not 1 <= args.budget_seconds <= 7200 or not 1 <= args.stage_timeout_seconds <= 1800 or not 1 <= args.browser_hold_seconds <= 1800:
        parser.error('轮次/预算超出合同')
    root = args.source_root.resolve()
    out = (args.out_dir or OUTPUT_ROOT / args.run_id).resolve()
    if not out.is_relative_to(OUTPUT_ROOT.resolve()):
        parser.error('输出必须位于集成owner范围')
    out.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'run_id': args.run_id, 'started_at': now(), 'stage': args.stage,
              'status': 'running', 'budget_seconds': args.budget_seconds, 'concurrency': 1,
              'rounds_planned': args.rounds if args.stage == 'cloud' else 0, 'rounds_executed': 0,
              'executions': [], 'collection_summaries': [], 'cloud_models_called': False,
              'cloud_runner_launch_attempts': 0,
              'semantic_final_judgment': 'not_completed', 'total_model_cost': None,
              'runtime': runtime_summary(),
              'limitations': ['固定合成条件的工程结果不代表真实客户表现或比赛评分。',
                              '脚本化模型仅核验请求链；独立语义裁决另行保存。',
                              '未完成的接口、实屏、对照和设备验收不得计为通过。']}
    atomic_json(out / 'run.json', report)
    started = time.monotonic()
    exit_code = 1
    try:
        report['before_live'] = baseline(root, args.service_url)
        atomic_json(out / 'baseline.json', report['before_live'])
        candidate = out / 'candidate'
        manifest = copy_snapshot(root, candidate)
        report['data_sha256'] = copy_data_snapshot(root, candidate)
        atomic_json(out / 'data-manifest.json', report['data_sha256'])
        report['auxiliary_sha256'] = copy_auxiliary_snapshot(root, candidate)
        atomic_json(out / 'auxiliary-manifest.json', report['auxiliary_sha256'])
        report['formal_support_sha256'] = copy_formal_support_snapshot(root, candidate, required=args.stage == 'local')
        atomic_json(out / 'formal-support-manifest.json', report['formal_support_sha256'])
        report['source_sha256'] = manifest
        report['source_manifest_sha256'] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        report['candidate_root'] = str(candidate)
        atomic_json(out / 'source-manifest.json', manifest)
        if args.receive_results:
            report['received_results'] = imported_results(args.receive_results, out)
        dataset = candidate / 'evaluation/window_integration/semantic_cases.json'
        if args.stage not in ('baseline', 'browser') and not dataset.exists():
            raise ValueError('independent_frozen_dataset_not_ready')
        if dataset.exists():
            report['dataset_sha256'] = sha(dataset)
        with tempfile.TemporaryDirectory(prefix='navren-window-integration-state-') as temp:
            environment = isolated_environment(candidate, Path(temp))
            semantic_runner = candidate / 'tests/window_integration/semantic_runner_adapter.py'
            if args.stage in ('preflight', 'local', 'cloud') and not semantic_runner.is_file():
                raise ValueError('semantic_fixture_adapter_not_ready')
            commands = []
            if args.stage == 'oracle-self-test':
                commands.append(('independent-oracle', [sys.executable, '-B',
                    str(candidate / 'tests/window_integration/semantic_oracle.py'), '--self-test']))
            if args.stage == 'browser':
                remaining = args.budget_seconds - (time.monotonic() - started)
                if remaining <= 31:
                    raise ValueError('insufficient_browser_budget')
                hold = min(args.browser_hold_seconds, int(remaining - 30))
                report['executions'].append(browser_worker(candidate, out / 'browser-state', hold, environment))
                report['status'] = 'browser_technical_session_finished'
                exit_code = 0 if execution_passed(report['executions'][-1]) else 1
                if exit_code:
                    report['status'] = 'failed'
            if args.stage in ('preflight', 'local'):
                commands.append(('oracle', [sys.executable, str(semantic_runner),
                    '--dataset', str(dataset), '--out-dir', str(out / 'oracle-results'), '--offline']))
                commands.append(('process', [sys.executable, str(semantic_runner),
                    '--dataset', str(dataset), '--out-dir', str(out / 'process-results'), '--process-preflight']))
            if args.stage == 'local':
                ui_runner = candidate / 'tests/ui_window/run_module_checks.py'
                if not ui_runner.is_file():
                    raise ValueError('required_ui_module_runner_missing')
                ui_output = candidate / 'outputs/window-progress/integration/ui-module-checks'
                commands.append(('ui-modules', [sys.executable, '-B', str(ui_runner),
                    '--output', str(ui_output)]))
                for family in ('financial', 'security'):
                    commands.append((family + '-regression', [sys.executable, '-B',
                        str(candidate / 'tests/window_integration/legacy_regression_adapter.py'), '--dataset',
                        str(candidate / ('evaluation/' + family + '_eval_set.json')),
                        '--out-dir', str(out / (family + '-regression-results'))]))
                commands.append(('independent-oracle', [sys.executable, '-B',
                    str(candidate / 'tests/window_integration/semantic_oracle.py'), '--self-test']))
                test_directory = candidate / 'tests/window_integration'
                formal_output = candidate / 'outputs/window-progress/integration/formal-business-runner/g1-offline-tests'
                unittest_runner = test_directory / 'run_unittest.py'
                if not unittest_runner.is_file():
                    raise ValueError('local_structured_test_runner_missing')
                for filename, minimum in LOCAL_TEST_GROUPS:
                    if not (test_directory / filename).is_file():
                        raise ValueError('required_local_test_group_missing:' + filename)
                    name = filename.removeprefix('test_').removesuffix('.py')
                    if filename == 'test_formal_business_runner.py':
                        commands.append((name, [sys.executable, '-B', str(test_directory / filename),
                            '--out-dir', str(formal_output)]))
                        continue
                    commands.append((name, [sys.executable, '-B', str(unittest_runner),
                        '--start-dir', str(test_directory), '--pattern', filename,
                        '--minimum-tests', str(minimum), '--output', str(out / name / 'test-result.json')]))
            if args.stage == 'cloud':
                report['handoff_gate'] = handoff_gate(args.handoff, manifest, report['data_sha256'])
                if not report['handoff_gate']['ready']:
                    report['status'] = 'waiting_for_module_handoff'
                    exit_code = 2
                else:
                    remaining = args.budget_seconds - (time.monotonic() - started)
                    report['paid_preflight_gate'] = paid_preflight_gate(
                        root, args.paid_preflight_manifest, manifest, out,
                        timeout_seconds=max(1, min(30, int(remaining))))
                    atomic_json(out / 'run.json', report)
                    if not report['paid_preflight_gate']['ready']:
                        report['status'] = 'paid_preflight_blocked'
                        exit_code = 2
                    else:
                        for number in range(1, args.rounds + 1):
                            commands.append((f'cloud-round-{number}', [sys.executable, str(semantic_runner),
                                '--dataset', str(dataset), '--out-dir', str(out / f'cloud-results-{number}'),
                                '--paid-campaign-manifest', str(args.paid_preflight_manifest),
                                '--paid-campaign-round', str(number)]))
            for name, argv in commands:
                remaining = args.budget_seconds - (time.monotonic() - started)
                if remaining <= 1:
                    report['status'] = 'budget_exhausted'
                    break
                if args.case_ids and (name in ('oracle', 'process') or name.startswith('cloud-round-')):
                    argv += ['--case-ids', args.case_ids]
                dispatch_environment = environment
                if len(argv) > 1 and argv[1] == str(semantic_runner):
                    # 原 CLI 禁止知识库环境覆盖，并自行派生逐题临时库及检查实际路径。
                    # 只对这个入口移除兜底变量；其他进程仍使用完整隔离环境。
                    dispatch_environment = environment.copy()
                    dispatch_environment.pop('NAVREN_KNOWLEDGE_DB', None)
                if name.startswith('cloud-round-'):
                    report['cloud_runner_launch_attempts'] += 1
                    atomic_json(out / 'run.json', report)
                execution = execute(argv, candidate, out / name, min(remaining, args.stage_timeout_seconds), dispatch_environment)
                execution['name'] = name
                if name in {p.removeprefix('test_').removesuffix('.py') for p, _ in LOCAL_TEST_GROUPS}:
                    counts = formal_output / 'test-result.json' if name == 'formal_business_runner' else out / name / 'test-result.json'
                    minimum = next(n for p, n in LOCAL_TEST_GROUPS
                                   if p.removeprefix('test_').removesuffix('.py') == name)
                    execution['test_counts'] = structured_test_summary(counts, minimum)
                    execution['result_contract_passed'] = execution['test_counts']['complete']
                if name == 'ui-modules':
                    execution['test_counts'] = ui_test_summary(candidate, ui_output / 'results.json')
                    execution['result_contract_passed'] = execution['test_counts']['complete']
                if name in ('financial-regression', 'security-regression'):
                    counts = out / (name + '-results') / 'test-result.json'
                    execution['test_counts'] = structured_test_summary(counts, 15 if name == 'financial-regression' else 26)
                    execution['result_contract_passed'] = execution['test_counts']['complete']
                if name in ('oracle', 'process', 'independent-oracle'):
                    raw = (out / name / 'stdout.log' if name == 'independent-oracle'
                           else out / (name + '-results') / 'semantic-results.json')
                    execution['test_counts'] = local_case_summary(raw, dataset, name)
                    counts_path = out / name / 'case-counts.json'
                    atomic_json(counts_path, execution['test_counts'])
                    execution['case_counts_report'] = str(counts_path)
                    execution['case_counts_sha256'] = sha(counts_path)
                    execution['result_contract_passed'] = execution['test_counts']['complete']
                report['executions'].append(execution)
                if name.startswith('cloud-round-'):
                    report['rounds_executed'] += 1
                    results_path = out / ('cloud-results-' + name.rsplit('-', 1)[1]) / 'semantic-results.json'
                elif name in ('oracle', 'process'):
                    results_path = out / (name + '-results') / 'semantic-results.json'
                else:
                    results_path = None
                if results_path:
                    report['collection_summaries'].append({'name': name, **semantic_summary(results_path)})
                    if name.startswith('cloud-round-') and results_path.exists():
                        records = json.loads(results_path.read_text()).get('records', [])
                        report['cloud_models_called'] = report['cloud_models_called'] or any(
                            any(r.get('response_metadata', {}).get(kind) for kind in ('primary', 'reviewer'))
                            for r in records)
                if (source_manifest(candidate) != manifest or data_manifest(candidate) != report['data_sha256']
                        or auxiliary_manifest(candidate) != report['auxiliary_sha256']
                        or formal_support_manifest(candidate) != report['formal_support_sha256']):
                    report['status'] = 'invalidated_candidate_drift'
                    raise ValueError('candidate_source_changed_during_execution')
                atomic_json(out / 'run.json', report)
                print(json.dumps({'stage': name, 'status': execution['status'], 'exit_code': execution['exit_code']}, ensure_ascii=False), flush=True)
                if not execution_passed(execution):
                    report['status'] = 'failed'
                    # 本地独立测试仍可继续，云试验首次失败即熔断，不重复消耗。
                    if name.startswith('cloud-') or execution['status'] == 'timed_out':
                        break
            if args.stage == 'baseline':
                report['status'] = 'baseline_recorded'
                exit_code = 0
            elif args.stage == 'browser':
                pass
            elif report['status'] in ('waiting_for_module_handoff', 'paid_preflight_blocked'):
                pass
            elif len(report['executions']) == len(commands) and all(execution_passed(e) for e in report['executions']):
                report['status'] = 'engineering_stage_passed' if args.stage != 'cloud' else 'collected_pending_independent_judgment'
                exit_code = 0
            elif report['status'] == 'running':
                report['status'] = 'incomplete'
    except Exception as error:
        if report['status'] != 'invalidated_candidate_drift':
            report['status'] = 'fixture_or_dependency_error'
        report['error'] = {'type': type(error).__name__, 'message': str(error)[:300]}
    finally:
        report['completed_at'] = now()
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        report['planned_exit_code'] = report.get('planned_exit_code', exit_code)
        try:
            report['candidate_source_matches_frozen'] = 'candidate' in locals() and source_manifest(candidate) == report.get('source_sha256')
            report['candidate_data_matches_frozen'] = 'candidate' in locals() and data_manifest(candidate) == report.get('data_sha256')
            report['candidate_auxiliary_matches_frozen'] = 'candidate' in locals() and auxiliary_manifest(candidate) == report.get('auxiliary_sha256')
            report['candidate_formal_support_matches_frozen'] = 'candidate' in locals() and formal_support_manifest(candidate) == report.get('formal_support_sha256')
            if report.get('source_sha256') and (not report['candidate_source_matches_frozen'] or not report['candidate_data_matches_frozen']
                                                or not report['candidate_auxiliary_matches_frozen']
                                                or not report['candidate_formal_support_matches_frozen']):
                report['status'] = 'invalidated_candidate_drift'
                report['planned_exit_code'] = exit_code = 1
            report['current_source_matches_frozen'] = source_manifest(root) == report.get('source_sha256')
            report['current_data_matches_frozen'] = data_manifest(root) == report.get('data_sha256')
            report['current_auxiliary_matches_frozen'] = auxiliary_manifest(root) == report.get('auxiliary_sha256')
            report['current_formal_support_matches_frozen'] = formal_support_manifest(root) == report.get('formal_support_sha256')
            report['after_live'] = baseline(root, args.service_url)
            report['live_business_digest_unchanged'] = report.get('before_live', {}).get('bank_state') == report['after_live']['bank_state']
            if args.stage in ('preflight', 'local', 'cloud', 'browser') and not report['live_business_digest_unchanged']:
                report['status_before_live_postflight'] = report['status']
                report['status'] = 'live_business_drift'
                report['live_drift_actor'] = 'unknown; observed digest difference does not identify the writer'
                report['planned_exit_code'] = exit_code = 1
        except Exception as error:
            report['postflight_error_type'] = type(error).__name__
            report['status'] = 'postflight_verification_failed'
            report['planned_exit_code'] = exit_code = 1
        atomic_json(out / 'run.json', report)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
