#!/usr/bin/env python3
"""Bounded offline regressions: independent subprocesses, artifacts and true timeouts."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    'tests.test_simulated_bank', 'tests.test_bank_integration',
    'tests.test_cashflow_timeline', 'tests.test_calendar',
    'tests.test_service_negotiation', 'tests.test_personal_data',
    'tests.test_user_memory', 'tests.test_financial_graph',
    'tests.test_finance_capabilities', 'tests.test_rag_integration',
    'tests.test_voice_gateway',
)
REVIEWS = {
    'test_simulated_bank': 'TemporaryDirectory SQLite；本机 HTTP；无模型调用。',
    'test_bank_integration': '临时银行/派生库；LegacyFinanceRegression 使用 deterministic_cashflow_agents。',
    'test_cashflow_timeline': '纯函数、内存归档样本；无数据库或外部请求。',
    'test_calendar': '临时 FinanceGateway 与 HTTP；ScriptedPrimary/Reviewer 替身。',
    'test_service_negotiation': '临时 FinanceGateway；本地预批准规则，不调用机构或模型。',
    'test_personal_data': '临时加密库；测试自行生成 Fernet key，不读取真实客户库。',
    'test_user_memory': '临时 SimulatedBank；测试个人资料使用运行器生成的临时 Fernet key。',
    'test_financial_graph': '临时 FinanceGateway；历史保存/图谱均为本地合成样本。',
    'test_finance_capabilities': '纯路由和能力目录断言；无数据库或模型请求。',
    'test_rag_integration': '临时知识/银行/个人库；ScriptedPrimary/Reviewer；语音只申请凭据不连接真实上游。',
    'test_voice_gateway': '临时库和本机 HTTP/WS；SpeechTask connector=Provider，语音上游完全替身。',
}
PURE = {'test_cashflow_timeline', 'test_finance_capabilities'}
APPLICATION_DIRS = ('agent_runtime', 'base_runtime', 'financial_methods', 'local_knowledge', 'simulated_bank')


def now():
    return datetime.now(timezone.utc).isoformat()


def application_digests():
    files = set(ROOT.glob('*.py'))
    for name in APPLICATION_DIRS:
        files.update((ROOT / name).rglob('*.py'))
    files.discard(Path(__file__).resolve())
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)}


def test_dependency_digests():
    # Some reviewed modules import shared fixtures from other test modules.
    # Freeze those helper sources too, so changed fixtures cannot escape the run guard.
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / 'tests').rglob('*.py'))}


def atomic_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False, indent=2); out.write('\n')
        out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def source_review(module):
    name = module.rsplit('.', 1)[-1]; source = ROOT / 'tests' / (name + '.py')
    if name not in REVIEWS or not source.is_file():
        return {'module': module, 'safe': False, 'reason': '不在已核对的限定模块清单或测试文件缺失。'}
    body = source.read_text(encoding='utf-8')
    if name not in PURE and 'TemporaryDirectory' not in body:
        return {'module': module, 'safe': False, 'reason': '未找到独立临时目录；需重新人工核对，已排除。'}
    return {'module': module, 'safe': True, 'reason': REVIEWS[name],
            'source': str(source), 'sha256': hashlib.sha256(body.encode()).hexdigest()}


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.executed = []; self.successes = []
    def startTest(self, test):
        self.executed.append(test.id()); super().startTest(test)
    def addSuccess(self, test):
        self.successes.append(test.id()); super().addSuccess(test)


def child(module, report_path):
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
    blocked = []
    original_connect, original_getaddrinfo = socket.socket.connect, socket.getaddrinfo
    def local(host):
        if host in ('localhost', None): return True
        try: return ipaddress.ip_address(str(host)).is_loopback
        except ValueError: return False
    def resolve(host, *args, **kwargs):
        if not local(host):
            blocked.append({'kind': 'dns', 'host': str(host)})
            raise OSError('Regression permits only loopback network')
        return original_getaddrinfo(host, *args, **kwargs)
    def connect(sock, address):
        if not isinstance(address, tuple) or not local(address[0]):
            blocked.append({'kind': 'connect', 'host': str(address[0]) if isinstance(address, tuple) else 'non-IP'})
            raise OSError('Regression permits only loopback network')
        return original_connect(sock, address)
    # Import under the network guard, then disable actual Keychain provider loaders.
    with patch.object(socket, 'getaddrinfo', resolve), patch.object(socket.socket, 'connect', connect):
        import agent_runtime.model_adapters as adapters
        import voice_gateway
        import local_knowledge.store as knowledge
        with patch.object(adapters, '_key_from_sources', return_value=''), \
                patch.object(voice_gateway, 'load_key', return_value='isolated-no-cloud-fixture'), \
                patch.object(knowledge, 'default_path', return_value=Path.cwd() / 'fallback-knowledge.sqlite3'):
            suite = unittest.defaultTestLoader.loadTestsFromName(module)
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    record = {'module': module, 'tests_run': result.testsRun, 'successful_tests': result.successes,
              'executed_tests': result.executed, 'failures': [{'test': t.id(), 'traceback': error} for t, error in result.failures],
              'errors': [{'test': t.id(), 'traceback': error} for t, error in result.errors],
              'skipped': [{'test': t.id(), 'reason': reason} for t, reason in result.skipped],
              'expected_failures': [t.id() for t, _ in result.expectedFailures],
              'unexpected_successes': [t.id() for t in result.unexpectedSuccesses],
              'blocked_external_network': blocked, 'finished_at': now(),
              'passed': result.wasSuccessful() and result.testsRun > 0 and not blocked}
    atomic_json(report_path, record)
    return 0 if record['passed'] else 1


def classify_failure(item):
    text = item.get('traceback', '')
    if any(code in text for code in ('PERSONAL_VERSION_REQUIRED', 'SESSION_CHANGED', 'STALE_AGENT_REVIEW',
                                    'AGENT_REVIEW_REQUIRED', 'PUBLIC_EVIDENCE_CHANGED', 'STALE_PUBLIC_EVIDENCE')):
        return {**item, 'classification': '新增安全规则与旧测试预期冲突候选',
                'review_status': '需结合原始断言核对；不视为测试通过'}
    if any(value in text for value in ('409 != 200', '403 != 200', '401 != 200')):
        return {**item, 'classification': 'HTTP 拒绝语义变化候选',
                'review_status': '需辨别新增安全约束或功能回归；保留失败'}
    return {**item, 'classification': '未核定的功能/旧断言回归', 'review_status': '待人工核对原始日志'}


def run_module(module, logs, timeout, active, lock, stopped=None):
    name = module.rsplit('.', 1)[-1]; started = time.monotonic()
    if stopped is not None and stopped.is_set():
        return {'module': module, 'passed': False, 'exit_code': 130, 'runner_error': '总任务已中断，未启动模块。'}
    out_path, err_path, child_path = (logs / (name + suffix) for suffix in ('.stdout.log', '.stderr.log', '.json'))
    timed_out = False; terminated = False
    with tempfile.TemporaryDirectory(prefix='navren-regression-' + name + '-') as folder:
        temporary = Path(folder)
        from cryptography.fernet import Fernet
        # Keep platform variables intact, remove inherited credentials/proxies, and bind all fallback stores to temp.
        env = {key: value for key, value in os.environ.items()
               if not key.endswith(('_API_KEY', '_TOKEN', '_PROXY')) and key not in ('NAVREN_PERSONAL_DATA_KEY', 'NAVREN_KNOWLEDGE_DB')}
        env.update(NAVREN_PERSONAL_DATA_KEY=Fernet.generate_key().decode(),
                   NAVREN_SIM_DB=str(temporary / 'fallback-bank.sqlite3'),
                   QINGZHOU_SIM_DB=str(temporary / 'fallback-bank.sqlite3'),
                   NAVREN_FUND_CACHE=str(temporary / 'funds'),
                   NAVREN_MODEL_CALL_LOG=str(temporary / 'model-calls.jsonl'),
                   NAVREN_ALLOW_CLOUD_ASR='0', NO_PROXY='127.0.0.1,localhost,::1', PYTHONUNBUFFERED='1',
                   PYTHONDONTWRITEBYTECODE='1')
        command = [sys.executable, '-u', str(Path(__file__).resolve()), '--child-module', module,
                   '--child-report', str(child_path)]
        with out_path.open('wb') as stdout, err_path.open('wb') as stderr:
            process = subprocess.Popen(command, cwd=temporary, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            with lock:
                active.add(process)
                if stopped is not None and stopped.is_set(): os.killpg(process.pid, signal.SIGKILL)
            try:
                try: code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
                    code = 124; terminated = True
            finally:
                with lock: active.discard(process)
    result = {'module': module, 'passed': False, 'exit_code': code, 'timed_out': timed_out,
              'process_group_terminated': terminated, 'timeout_seconds': timeout,
              'duration_seconds': round(time.monotonic() - started, 3),
              'stdout_log': str(out_path), 'stderr_log': str(err_path), 'result_file': str(child_path)}
    if child_path.is_file():
        try:
            summary = json.loads(child_path.read_text(encoding='utf-8'))
            result['details'] = summary
            result['passed'] = code == 0 and summary.get('passed') is True and summary.get('tests_run', 0) > 0
            result['failure_reviews'] = [classify_failure(i) for i in summary.get('failures', []) + summary.get('errors', [])]
        except (OSError, ValueError) as error:
            result['artifact_error'] = type(error).__name__
    else: result['artifact_error'] = '未生成本模块结果摘要'
    return result


def run(output, timeout=60, workers=2, modules=MODULES, baseline=None, changed_sources=()):
    started = time.monotonic(); run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    output = Path(output).resolve(); logs = output.parent / 'regression-logs' / run_id; logs.mkdir(parents=True)
    pass_marker = output.with_name(output.stem + '-passed.json')
    pass_marker.unlink(missing_ok=True)
    reused = []
    original_modules = tuple(modules)
    baseline_record = None
    source_changes = []
    if baseline is not None:
        baseline = Path(baseline).resolve()
        baseline_record = json.loads(baseline.read_text(encoding='utf-8'))
        if baseline_record.get('status') != 'finished': raise ValueError('baseline regression must be finished')
        modules = tuple(baseline_record['requested_modules'])
        if any(module not in MODULES for module in modules) or not set(original_modules) <= set(modules):
            raise ValueError('baseline contains unreviewed or incompatible modules')
        current_sources = application_digests()
        previous_sources = baseline_record.get('application_sources_end', {})
        if not previous_sources: raise ValueError('baseline lacks application source SHA evidence')
        source_changes = sorted(name for name, digest in previous_sources.items()
                                if current_sources.get(name) != digest)
        unacknowledged = set(source_changes) - set(changed_sources)
        if unacknowledged:
            raise ValueError('cannot reuse a changed business source without an impact review: ' + ','.join(sorted(unacknowledged)))
        if 'service_negotiation.py' in source_changes and not {
                'tests.test_service_negotiation', 'tests.test_bank_integration'} <= set(original_modules):
            raise ValueError('service_negotiation changes require service and HTTP protocol regressions')
        old_reviews = {r['module']: r for r in baseline_record['safety_reviews']}
        for item in baseline_record['modules']:
            if item['module'] in original_modules: continue
            if (not item['passed'] or source_review(item['module']).get('sha256') != old_reviews[item['module']].get('sha256')):
                raise ValueError('cannot reuse failed or changed test module: ' + item['module'])
            reused.append({**item, 'verification_origin': '复用已通过基线模块',
                           'baseline_report': str(baseline), 'baseline_run_id': baseline_record['run_id']})
    reviews = [source_review(module) for module in modules]
    excluded = [review for review in reviews if not review['safe']]
    reused_count = len(reused)
    report = {'schema_version': 1, 'run_id': run_id, 'status': 'running', 'passed': False,
              'started_at': now(), 'requested_modules': list(modules), 'safety_reviews': reviews,
              'excluded': excluded, 'module_timeout_seconds': timeout, 'workers': workers,
              'full_current_revision_rerun': baseline_record is None,
              'isolation': {'temporary_directory_per_module': True, 'temporary_personal_key': True,
                            'provider_keychain_loaders_disabled': True, 'network': 'loopback_only',
                            'background_market_refresh': False, 'real_models': False},
              'modules': reused, 'rerun_modules': list(original_modules),
              'application_sources_start': application_digests(),
              'test_dependency_sources_start': test_dependency_digests(),
              'uncovered': ['未测试真实模型语义、外部机构在线可用性、真实客户或生产部署。']}
    if baseline_record is not None:
        report['baseline_report'] = str(baseline)
        report['baseline_changed_sources'] = source_changes
        report['source_impact_review'] = {'declared_changed_sources': list(changed_sources),
                                         'rerun_modules': list(original_modules)}
        report['full_current_revision_rerun'] = False
        report['uncovered'].append('仅重新运行选定的失败或受影响模块；其他模块复用首轮结果，未在修复后重新执行全部 201 项。')
    atomic_json(output, report)
    active, lock, stopped = set(), threading.Lock(), threading.Event()
    def terminate(signum, frame):
        stopped.set()
        with lock: processes = list(active)
        for process in processes:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        raise SystemExit(128 + signum)
    old_handlers = {s: signal.signal(s, terminate) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_module, r['module'], logs, timeout, active, lock, stopped): r['module']
                       for r in reviews if r['safe'] and r['module'] in original_modules}
            for future in as_completed(futures):
                try: item = future.result()
                except Exception as error:
                    item = {'module': futures[future], 'passed': False, 'exit_code': 1,
                            'runner_error': type(error).__name__ + ': ' + str(error)}
                item['verification_origin'] = '本次重新执行' if baseline_record is not None else '本次执行'
                report['modules'].append(item); report['modules'].sort(key=lambda row: modules.index(row['module']))
                atomic_json(output, report)
                print(json.dumps({'module': item['module'], 'passed': item['passed'],
                                  'exit_code': item['exit_code'], 'duration_seconds': item.get('duration_seconds')}, ensure_ascii=False), flush=True)
    finally:
        for s, handler in old_handlers.items(): signal.signal(s, handler)
    report.update(status='finished', finished_at=now(), duration_seconds=round(time.monotonic() - started, 3))
    report['application_sources_end'] = application_digests()
    report['application_changed_during_run'] = report['application_sources_start'] != report['application_sources_end']
    report['test_sources_end'] = {r['module']: source_review(r['module']).get('sha256') for r in reviews}
    report['test_dependency_sources_end'] = test_dependency_digests()
    report['test_source_changed_during_run'] = (any(report['test_sources_end'][r['module']] != r.get('sha256') for r in reviews)
        or report['test_dependency_sources_start'] != report['test_dependency_sources_end'])
    report['counts'] = {'modules_requested': len(modules), 'modules_run': len(report['modules']),
                        'modules_passed': sum(item['passed'] for item in report['modules']),
                        'modules_failed': sum(not item['passed'] for item in report['modules']),
                        'modules_excluded': len(excluded),
                        'modules_reused': reused_count, 'modules_executed_this_run': len(original_modules),
                        'tests_run': sum(item.get('details', {}).get('tests_run', 0) for item in report['modules'])}
    report['passed'] = (not excluded and not report['application_changed_during_run'] and not report['test_source_changed_during_run']
                        and len(report['modules']) == len(modules) and all(item['passed'] for item in report['modules']))
    report['aggregate_exit_code'] = 0 if report['passed'] else 1
    atomic_json(output, report)
    if report['passed']:
        atomic_json(pass_marker, {'passed': True, 'run_id': run_id, 'finished_at': report['finished_at'],
                                 'tests_run': report['counts']['tests_run']})
    print(json.dumps({'passed': report['passed'], 'output': str(output), 'counts': report['counts']}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/window-progress/base/regression.json')
    parser.add_argument('--module-timeout', type=float, default=60)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--modules', nargs='+', choices=MODULES)
    parser.add_argument('--baseline', type=Path, help='Reuse only unchanged test modules that passed in this finished report')
    parser.add_argument('--changed-source', action='append', default=[],
                        help='Explicitly reviewed changed source covered by the selected dependent modules; never silently reuse changed SHA')
    parser.add_argument('--child-module', choices=MODULES, help=argparse.SUPPRESS)
    parser.add_argument('--child-report', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_module:
        if not args.child_report: parser.error('--child-report required')
        return child(args.child_module, args.child_report)
    if not 0 < args.module_timeout <= 60: parser.error('module timeout must be in (0,60] seconds')
    return run(args.output, args.module_timeout, args.workers, tuple(args.modules or MODULES), args.baseline, args.changed_source)


if __name__ == '__main__':
    raise SystemExit(main())
