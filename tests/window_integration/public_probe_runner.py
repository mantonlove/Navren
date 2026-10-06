"""G2 公共 P10→P02 驱动器；离线仅检查协议，云语义交给独立裁决。

业务导入必须晚于每题隔离环境。保留真实 HTTP、EvidenceRuntime、Agent 与审核链。
本进程不填写外层实际子进程退出状态，不读取现役数据库，也不生成金融授权。
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http import cookiejar
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
_parts = ROOT.parts
_outer = next((i for i in range(len(_parts)-2)
               if _parts[i:i+3] == ('outputs', 'window-progress', 'integration')), None)
OUTPUT_ROOT = Path(*_parts[:_outer+3]) if _outer is not None else ROOT / 'outputs/window-progress/integration'
VERSION = 'navren.integration.public-probe-runner.v1'
ORDER = ('P10', 'P02')
PUBLIC_ROOT = Path('outputs/window-progress/rag')
QUESTION_SHA = '71190ef37aa8f9f69f46cbe84cff56b7305cddcfd3b2a4f532e2428e3584a3cb'
PUBLIC_MANIFEST_SHA = '71bbc2512c91a8ad3d3e6e4d05deb7447dbeb41a6be411d2d7a546a2fd90cc60'
DATABASE_SHA = '230e16cbce15cedbe1f54ee2222a54466f442b7402e141e8721d5d26ed80fcf0'
RELEASE = 'pub-7a4f1c5fb3ab99d78eb2aae5846d5d3e17eb8330'
AUXILIARY = {str(PUBLIC_ROOT / 'manual-question-set.json'): QUESTION_SHA,
             'outputs/window-progress/finance/implementation/adapter-contract.md':
             '4cce2b7a8f03db6ef8c9139419ba9ab9c82d847cc286f59b22c83e1eedc9da30',
             'outputs/window-progress/finance/solution-review/solution-v2.md':
             '7e3ffe6996588cdd55733926ae294fff72a6e6b23454134ee9fa0708d2f100e8'}
CASE_SHA = {'P10': 'f864839df978240b699c5ba7780a558f5c2805e8529d13065572f1b7c8c916d8',
            'P02': 'eefd29d6fff6759cbda79a085fce8a5bec3c7d073b78a02e651be2a7a0bc03a9'}
BUDGET = {'parallelism': 1, 'whole_seconds': 300, 'task_seconds': 105,
          'max_rounds': 8, 'max_tools': 12, 'task_primary_reviewer_tokens': 100000,
          'native_primary_output_tokens': 4000, 'planned_total_tokens_upper_bound': 200000}
MODEL_URLS = {'https://aiping.cn/api/v1/chat/completions'}
MODEL_IDENTITIES = {'primary': {'provider': 'ai_ping', 'model_id': 'DeepSeek-V4-Flash'},
                    'reviewer': {'provider': 'ai_ping', 'model_id': 'GLM-5.3-Flash'}}
BUSINESS_MODULES = ('server', 'finance_gateway', 'fund_history', 'market_content',
                    'voice_gateway', 'agent_runtime', 'base_runtime', 'simulated_bank', 'local_knowledge')
FINANCIAL_TABLES = ('accounts', 'profiles', 'holdings', 'orders', 'transactions',
                    'fund_holds', 'operations', 'cards', 'mandates', 'subscriptions',
                    'account_details', 'account_limits', 'account_restrictions', 'deposit_contracts',
                    'beneficiaries', 'payment_instructions', 'scheduled_payments', 'split_bills',
                    'split_participants', 'subscription_events', 'card_controls', 'card_transactions',
                    'card_statements', 'loans', 'loan_repayments', 'risk_assessments', 'wealth_goals',
                    'budgets', 'orchestration_tasks', 'task_steps', 'service_reservations',
                    'investment_lots', 'lot_redemptions', 'goal_allocations', 'navren_goal_drafts',
                    'navren_research_plans', 'portfolio_versions', 'portfolio_positions', 'portfolio_valuations',
                    'goals', 'budget_holds', 'calendar_events', 'calendar_series',
                    'calendar_occurrence_overrides', 'calendar_event_drafts', 'calendar_import_batches',
                    'service_negotiation_drafts', 'service_negotiation_applications')


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.public-probe-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checked_file(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    path = root / relative
    if relative.is_absolute() or '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('input_path_escape:' + str(relative))
    if not path.is_file():
        raise ValueError('input_file_missing:' + str(relative))
    return path


def load_contract(candidate):
    path = checked_file(candidate, 'scripts/run_window_integration.py')
    spec = importlib.util.spec_from_file_location('_public_probe_integration_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inspect_inputs(candidate, source_path, data_path):
    """复用外层完整枚举；不跟随原公共 manifest 的绝对库路径。"""
    candidate = Path(candidate).resolve()
    sources = json.loads(Path(source_path).read_text())
    datasets = json.loads(Path(data_path).read_text())
    contract = load_contract(candidate)
    if not sources or sources != contract.source_manifest(candidate):
        raise ValueError('candidate_source_manifest_mismatch')
    if sources.get('tests/window_integration/public_probe_runner.py') != sha(__file__):
        raise ValueError('running_adapter_not_same_frozen_source')
    if not datasets or datasets != contract.data_manifest(candidate):
        raise ValueError('candidate_data_manifest_mismatch')
    if contract.auxiliary_manifest(candidate) != AUXILIARY:
        raise ValueError('candidate_auxiliary_manifest_mismatch')
    auxiliary_paths = [checked_file(candidate, relative) for relative in AUXILIARY]
    if any(sha(path) != AUXILIARY[str(path.relative_to(candidate))] for path in auxiliary_paths):
        raise ValueError('candidate_auxiliary_input_changed')
    public_manifest = checked_file(candidate, PUBLIC_ROOT / 'integration-data-manifest.json')
    questions = checked_file(candidate, PUBLIC_ROOT / 'manual-question-set.json')
    database = checked_file(candidate, PUBLIC_ROOT / 'reviewed-public.sqlite3')
    for path, digest, code in ((public_manifest, PUBLIC_MANIFEST_SHA, 'public_manifest_changed'),
                               (questions, QUESTION_SHA, 'original_questions_changed'),
                               (database, DATABASE_SHA, 'public_database_changed')):
        if sha(path) != digest:
            raise ValueError(code)
    delivery = json.loads(public_manifest.read_text())
    if (delivery.get('public_only') is not True or delivery.get('source_read_only') is not True
            or delivery['knowledge_database']['sha256'] != DATABASE_SHA
            or delivery['knowledge_database']['release_id'] != RELEASE):
        raise ValueError('public_delivery_contract_mismatch')
    rows = json.loads(questions.read_text())['cases']
    cases = []
    for ident in ORDER:
        selected = [row for row in rows if row.get('id') == ident]
        if len(selected) != 1 or canonical_sha(selected[0]) != CASE_SHA[ident]:
            raise ValueError('original_case_changed:' + ident)
        cases.append(selected[0])
    return {'contract': contract, 'sources': sources, 'datasets': datasets, 'cases': cases,
            'database': database, 'questions': questions, 'public_manifest': public_manifest}


def cloud_gate(handoff_path, handoff_sha, sources, datasets, contract):
    """模板旗标不够：要求同一冻结候选的实际 G1 正常退出及零跳过计数。"""
    if not handoff_path or not handoff_sha or sha(handoff_path) != handoff_sha:
        raise ValueError('cloud_handoff_digest_missing_or_mismatch')
    gate = contract.handoff_gate(handoff_path, sources, datasets)
    if gate.get('ready') is not True:
        raise ValueError('cloud_handoff_not_ready')
    handoff = json.loads(Path(handoff_path).read_text())
    g1_path = Path(handoff.get('g1_run_path', '')).resolve()
    if (not g1_path.is_relative_to(OUTPUT_ROOT.resolve()) or not g1_path.is_file()
            or sha(g1_path) != handoff.get('g1_run_sha256')):
        raise ValueError('g1_receipt_missing_or_mismatch')
    g1 = json.loads(g1_path.read_text())
    if (g1.get('status') != 'engineering_stage_passed' or g1.get('stage') != 'local'
            or g1.get('source_sha256') != sources or g1.get('data_sha256') != datasets
            or g1.get('candidate_source_matches_frozen') is not True
            or g1.get('candidate_data_matches_frozen') is not True):
        raise ValueError('g1_candidate_not_passed')
    if g1.get('live_business_digest_unchanged') is not True:
        raise ValueError('g1_live_business_digest_not_proven_unchanged')
    auxiliary = AUXILIARY
    if g1.get('auxiliary_sha256') != auxiliary or g1.get('candidate_auxiliary_matches_frozen') is not True:
        raise ValueError('g1_original_question_not_frozen')
    execution_path = Path(handoff.get('g1_execution_path', '')).resolve()
    if (not execution_path.is_relative_to(OUTPUT_ROOT.resolve()) or not execution_path.is_file()
            or sha(execution_path) != handoff.get('g1_execution_sha256')):
        raise ValueError('g1_driver_receipt_missing_or_mismatch')
    driver = json.loads(execution_path.read_text())
    if driver.get('status') != 'exited' or type(driver.get('exit_code')) is not int or driver['exit_code'] != 0:
        raise ValueError('g1_driver_not_exited_zero')
    argv = driver.get('argv')
    def flag(name):
        if not isinstance(argv, list) or argv.count(name) != 1:
            raise ValueError('g1_driver_argv_binding_missing')
        index = argv.index(name)
        if index+1 >= len(argv) or not isinstance(argv[index+1], str) or argv[index+1].startswith('--'):
            raise ValueError('g1_driver_argv_binding_missing')
        return argv[index+1]
    source_root = Path(flag('--source-root')).resolve()
    if flag('--stage') != 'local':
        raise ValueError('g1_driver_stage_not_full_local')
    if Path(flag('--out-dir')).resolve() != g1_path.parent:
        raise ValueError('g1_driver_output_mismatch')
    if (not source_root.is_relative_to(OUTPUT_ROOT.resolve())
            or contract.source_manifest(source_root) != sources
            or contract.data_manifest(source_root) != datasets
            or contract.auxiliary_manifest(source_root) != auxiliary):
        raise ValueError('g1_driver_source_mismatch')
    if (len(argv) < 2 or not isinstance(argv[1], str) or not Path(argv[1]).is_file()
            or sha(argv[1]) != sources.get('scripts/run_window_integration.py')):
        raise ValueError('g1_driver_source_script_mismatch')
    g1_candidate = Path(g1.get('candidate_root', '')).resolve()
    if (not g1_candidate.is_relative_to(OUTPUT_ROOT.resolve())
            or contract.source_manifest(g1_candidate) != sources
            or contract.data_manifest(g1_candidate) != datasets
            or contract.auxiliary_manifest(g1_candidate) != auxiliary):
        raise ValueError('g1_frozen_candidate_drift')
    support = g1.get('formal_support_sha256')
    if (not isinstance(support, dict) or len(support) != 8 or g1.get('candidate_formal_support_matches_frozen') is not True
            or contract.formal_support_manifest(source_root, required=True) != support
            or contract.formal_support_manifest(g1_candidate, required=True) != support):
        raise ValueError('g1_formal_support_not_frozen_or_drifted')
    executions = g1.get('executions')
    if not isinstance(executions, list) or not executions:
        raise ValueError('g1_execution_missing')
    minimums = {'oracle':10, 'process':10, 'ui-modules':22, 'financial-regression':15,
                'security-regression':26, 'independent-oracle':10}
    minimums.update({name.removeprefix('test_').removesuffix('.py'): minimum
                    for name, minimum in contract.LOCAL_TEST_GROUPS})
    names = [execution.get('name') for execution in executions if isinstance(execution, dict)]
    if len(names) != len(executions) or len(set(names)) != len(names) or set(names) != set(minimums):
        raise ValueError('g1_full_local_group_collection_missing_or_duplicate')
    for execution in executions:
        if execution.get('status') != 'exited' or type(execution.get('exit_code')) is not int or execution['exit_code'] != 0:
            raise ValueError('g1_execution_not_exited_zero')
        counts = execution.get('test_counts')
        if (not isinstance(counts, dict) or counts.get('complete') is not True
                or type(counts.get('tests_run')) is not int or counts['tests_run'] < minimums[execution['name']]
                or any(type(counts.get(key)) is not int or counts[key] != 0 for key in
                       ('skipped', 'expected_failures', 'unexpected_successes', 'failures', 'errors'))):
            raise ValueError('g1_zero_skip_not_proven')
    return {**gate, 'g1_run_path': str(g1_path), 'g1_run_sha256': sha(g1_path),
            'g1_execution_path': str(execution_path), 'g1_execution_sha256': sha(execution_path),
            'g1_frozen_source_root': str(source_root), 'g1_candidate_root': str(g1_candidate)}


def agent_body(case):
    # 云输入只允许原题。request/expected/身份答案绝不进入这里。
    return {'text': case['question']}


def usage_record(runtime, cloud):
    def count(value):
        return value if type(value) is int and value >= 0 else None
    return {'usage_complete': runtime.get('usage_complete') if cloud else None,
            'actual_total_tokens': count(runtime.get('total_tokens')) if cloud else None,
            'actual_primary_tokens': count(runtime.get('primary_tokens')) if cloud else None,
            'actual_reviewer_tokens': count(runtime.get('reviewer_tokens')) if cloud else None,
            'known_tokens_lower_bound': count(runtime.get('known_tokens_lower_bound')) if cloud else None,
            'budget_tokens': count(runtime.get('budget_tokens')),
            'status': 'provider_reported' if cloud and runtime.get('usage_complete') is True
                      else 'unknown_usage' if cloud else 'offline_fixture_counters_only',
            'fixture_counters': deepcopy(runtime.get('model_calls', [])) if not cloud else None}


def configured_model_contract(candidate):
    """只读绑定实际原生配置；配置没有别名表时不接受推测别名。"""
    tree = ast.parse(checked_file(candidate, 'agent_runtime/model_adapters.py').read_text())
    names = ('AIPING_PRIMARY_MODEL', 'GLM_REVIEW_MODEL', 'AIPING_URL')
    values = {target.id: ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) for target in node.targets
              if isinstance(target, ast.Name) and target.id in names}
    expected = {'AIPING_PRIMARY_MODEL': MODEL_IDENTITIES['primary']['model_id'],
                'GLM_REVIEW_MODEL': MODEL_IDENTITIES['reviewer']['model_id'],
                'AIPING_URL': 'https://aiping.cn/api/v1/chat/completions'}
    if values != expected:
        raise ValueError('configured_native_model_or_url_changed')
    conversation = ast.parse(checked_file(candidate, 'agent_runtime/conversation.py').read_text())
    reviewer = next(node for node in conversation.body if isinstance(node, ast.ClassDef)
                    and node.name == 'GLMConversationReviewer')
    reviewer_ids = [node.value.id for node in reviewer.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'model_id' for target in node.targets)
                    and isinstance(node.value, ast.Name)]
    constructor = next(node for node in reviewer.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    clients = [node for node in ast.walk(constructor) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == 'OpenAICompatibleJsonClient']
    keywords = {keyword.arg: keyword.value for keyword in clients[0].keywords} if len(clients) == 1 else {}
    if (reviewer_ids != ['GLM_REVIEW_MODEL']
            or not isinstance(keywords.get('provider'), ast.Constant) or keywords['provider'].value != 'ai_ping'
            or not isinstance(keywords.get('model_id'), ast.Attribute) or keywords['model_id'].attr != 'model_id'
            or not isinstance(keywords['model_id'].value, ast.Name) or keywords['model_id'].value.id != 'self'
            or not isinstance(keywords.get('url'), ast.Name) or keywords['url'].id != 'AIPING_URL'):
        raise ValueError('configured_reviewer_provider_changed')
    native = ast.parse(checked_file(candidate, 'agent_runtime/native_client.py').read_text())
    cls = next(node for node in native.body if isinstance(node, ast.ClassDef) and node.name == 'AIPingToolClient')
    ids = [node.value.id for node in cls.body if isinstance(node, ast.Assign)
           and any(isinstance(target, ast.Name) and target.id == 'model_id' for target in node.targets)
           and isinstance(node.value, ast.Name)]
    native_providers = [keyword.value.value for node in ast.walk(cls) if isinstance(node, ast.Call)
                        for keyword in node.keywords
                        if keyword.arg == 'provider' and isinstance(keyword.value, ast.Constant)]
    if ids != ['AIPING_PRIMARY_MODEL'] or native_providers != ['ai_ping']:
        raise ValueError('configured_primary_provider_changed')
    agent_cls = next(node for node in conversation.body if isinstance(node, ast.ClassDef)
                     and node.name == 'ConversationAgent')
    agent_constructor = next(node for node in agent_cls.body
                             if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    defaults = [node.value for node in ast.walk(agent_constructor) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Attribute) and target.attr == 'primary'
                        for target in node.targets)]
    if not any(isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
               and len(node.values) == 2 and isinstance(node.values[0], ast.Name)
               and node.values[0].id == 'primary' and isinstance(node.values[1], ast.Call)
               and isinstance(node.values[1].func, ast.Name) and node.values[1].func.id == 'AIPingToolClient'
               for node in defaults):
        raise ValueError('configured_primary_default_changed')
    return deepcopy(MODEL_IDENTITIES)


def read_model_log_prefix(path):
    """保留解析失败之前的真实 JSONL 前缀；None 尾标使完整核验失败。"""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    rows = []
    try:
        with path.open('rb') as stream:
            for raw_line in stream:
                try:
                    line = raw_line.decode('utf-8')
                except UnicodeError:
                    rows.append(None)
                    break
                if not line.strip():
                    continue
                try:
                    def pairs(items):
                        value = {}
                        for key, item in items:
                            if key in value:
                                raise ValueError('duplicate_model_log_key')
                            value[key] = item
                        return value
                    rows.append(json.loads(line, object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_model_log'))))
                except (ValueError, TypeError):
                    rows.append(None)
                    break
    except (OSError, UnicodeError):
        rows.append(None)
    return rows


def audit_model_log_prefix(logs, *, turn_id=None, configured_models=None):
    """日志独立下界，不与 runtime 相加，也不声称不完整终态的请求数。"""
    errors, known, accepted, seen = [], 0, 0, set()
    models = configured_models or MODEL_IDENTITIES
    observed_turn = turn_id
    if not isinstance(logs, list):
        return {'known_tokens_lower_bound': None, 'actual_total_tokens': None,
                'actual_model_requests_sent': None, 'errors': ['actual_model_log_missing']}
    for log in logs:
        if not isinstance(log, dict):
            errors.append('actual_model_log_prefix_shape_invalid'); break
        stage = {'conversation.tools': 'primary', 'reviewer.conversation': 'reviewer'}.get(log.get('stage'))
        identity = models.get(stage) if isinstance(models, dict) else None
        request_id = log.get('request_id')
        if (stage is None or (accepted == 0 and stage != 'primary') or not isinstance(identity, dict)
                or log.get('model_id') != identity.get('model_id')
                or log.get('provider') != identity.get('provider')
                or not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
                or observed_turn is not None and request_id != observed_turn
                or log.get('outcome') not in ('completed', 'failed')
                or not isinstance(log.get('error_code'), str)
                or 'attempts' in log and (type(log['attempts']) is not int or log['attempts'] not in (0, 1))):
            errors.append('actual_model_log_prefix_identity_or_order_invalid'); break
        observed_turn = request_id
        try:
            fingerprint = canonical_sha(log)
        except (ValueError, TypeError):
            errors.append('actual_model_log_prefix_shape_invalid'); break
        if fingerprint in seen:
            errors.append('actual_model_log_prefix_duplicate_row'); break
        seen.add(fingerprint)
        # 一次重试及后续新请求均不能冒充本题合法前缀。原主模型日志没有 attempts 字段。
        if (log.get('outcome') == 'completed' and ('attempts' in log and log['attempts'] != 1)):
            errors.append('actual_model_log_prefix_attempts_invalid'); break
        usage = log.get('usage')
        if 'usage' in log:
            lower = usage_lower_bound(usage)
            if lower is None:
                errors.append('actual_model_log_prefix_usage_invalid'); break
            known += lower
            if not all(key in usage for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
                accepted += 1
                errors.append('actual_model_log_prefix_usage_incomplete')
                break
        elif stage == 'primary':
            errors.append('actual_model_log_prefix_usage_missing'); break
        accepted += 1
    return {'known_tokens_lower_bound': known if accepted else None,
            'actual_total_tokens': None, 'actual_model_requests_sent': None,
            'errors': list(dict.fromkeys(errors))}


def audit_response_identity(runtime, logs, metadata, *, turn_id=None, configured_models=None):
    """Trace 在真实读取时记录的 model/id，按阶段内顺序绑定实际调用。"""
    errors = []
    models = configured_models or MODEL_IDENTITIES
    calls = runtime.get('model_calls') if isinstance(runtime, dict) else None
    if not isinstance(calls, list) or not isinstance(logs, list) or len(calls) != len(logs):
        return {'errors': ['actual_response_identity_call_log_missing']}
    grouped = {'primary': [], 'reviewer': []}
    for call, log in zip(calls, logs):
        if not isinstance(call, dict) or not isinstance(log, dict) or call.get('stage') not in grouped:
            errors.append('actual_response_identity_call_shape_invalid'); continue
        stage = call['stage']; identity = models.get(stage) if isinstance(models, dict) else None
        if (not isinstance(identity, dict) or call.get('model_id') != identity.get('model_id')
                or log.get('model_id') != identity.get('model_id') or log.get('provider') != identity.get('provider')
                or log.get('stage') != ('conversation.tools' if stage == 'primary' else 'reviewer.conversation')
                or not isinstance(turn_id, str) or log.get('request_id') != turn_id):
            errors.append('actual_response_identity_configuration_or_turn_mismatch')
        if call.get('request_sent') is True:
            grouped[stage].append(call)
    if not isinstance(metadata, dict):
        return {'errors': list(dict.fromkeys(errors + ['actual_response_identity_metadata_missing']))}
    for stage, sent in grouped.items():
        rows = metadata.get(stage)
        if not isinstance(rows, list) or len(rows) != len(sent):
            errors.append('actual_response_identity_metadata_count_mismatch'); continue
        seen = set()
        for call, row in zip(sent, rows):
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not 1 <= len(row['id']) <= 256
                    or not isinstance(row.get('model'), str) or not 1 <= len(row['model']) <= 256
                    or row.get('model') != call.get('model_id') or row['id'] in seen):
                errors.append('actual_response_identity_model_or_id_invalid'); continue
            seen.add(row['id'])
    return {'errors': list(dict.fromkeys(errors))}


def compatible_trace_reviewer(original_class, safe_data):
    """精确加载同目录适配器；工厂调用前不导入业务或读取密钥。"""
    path = Path(__file__).with_name('semantic_runner_adapter.py')
    spec = importlib.util.spec_from_file_location('_paid_native_trace_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compatible_trace_reviewer(original_class, safe_data)


def paid_usage_audit(runtime, logs, turn_id=None, *, budget_limit=100000):
    """核对实际回执；已知失败用量只计一次，缺字段保留未知。"""
    errors = []
    calls = runtime.get('model_calls') if isinstance(runtime, dict) else None
    calls = calls if isinstance(calls, list) else []
    totals = {'primary':0, 'reviewer':0}
    all_known = bool(calls)
    known_lower, lower_observed = 0, False
    if not isinstance(turn_id,str) or not 1<=len(turn_id)<=256:
        errors.append('actual_model_turn_identity_missing')
    def counters(value):
        if not isinstance(value, dict):return None
        result = {k:value.get(k) for k in ('prompt_tokens','completion_tokens','total_tokens')}
        if any(type(v) is not int or v < 0 for v in result.values()):return None
        return result if result['prompt_tokens']+result['completion_tokens']==result['total_tokens'] else None
    for call in calls:
        if not isinstance(call, dict) or call.get('stage') not in totals:
            all_known=False; errors.append('paid_call_stage_invalid'); continue
        supplied = call.get('usage')
        lower = usage_lower_bound(supplied)
        if lower is not None:
            known_lower += lower; lower_observed = True
        usage = counters(supplied)
        if usage is None or call.get('usage_complete') is not True:
            all_known=False
        else:totals[call['stage']]+=usage['total_tokens']
        if (call.get('billing_mode')!='paid' or type(call.get('request_sent'))is not bool
                or type(call.get('attempts'))is not int or call['attempts']not in (0,1)
                or (call.get('request_sent')is True and call['attempts']!=1)
                or (call.get('request_sent')is False and call['attempts']!=0)
                or (call.get('outcome')=='completed' and call.get('request_sent')is not True)
                or (call.get('attempts')==0 and (call.get('request_sent')is not False or usage is None or usage['total_tokens']!=0))):
            errors.append('paid_sent_admission_invalid')
        if not isinstance(call.get('model_id'),str) or not call['model_id'] or len(call['model_id'])>256:
            errors.append('actual_model_identity_missing')
        if call.get('outcome')!='completed' or call.get('error_code'):
            errors.append('critical_model_call_failure')
        if call.get('error_code')=='response_truncated':errors.append('response_length_limit')
    total = sum(totals.values()) if all_known else None
    if (not isinstance(runtime,dict) or runtime.get('usage_complete')is not True or not all_known
            or any(type(runtime.get(k))is not int or runtime[k]!=totals[stage]
                   for k,stage in (('primary_tokens','primary'),('reviewer_tokens','reviewer')))
            or type(runtime.get('total_tokens'))is not int or runtime['total_tokens']!=total):
        errors.append('unknown_usage')
    if isinstance(runtime,dict):
        if (type(budget_limit)is not int or not 0<=budget_limit<=100000
                or not isinstance(runtime.get('budget_policy'),dict) or runtime['budget_policy'].get('limit')!=budget_limit
                or any(type(runtime.get(key))is not int or not 0<=runtime[key]<=limit for key,limit in
                       (('rounds',8),('tool_calls',12),('budget_tokens',budget_limit),('duration_ms',105000)))
                or (total is not None and runtime.get('budget_tokens')!=total)):
            errors.append('actual_paid_budget_counter_mismatch')
    if not isinstance(logs,list) or len(logs)!=len(calls):
        errors.append('actual_model_log_missing_or_count_mismatch')
    else:
        for call,log in zip(calls,logs):
            if not isinstance(call,dict) or call.get('stage')not in totals or not isinstance(log,dict):
                errors.append('actual_model_log_shape_invalid'); continue
            stage='conversation.tools' if call['stage']=='primary' else 'reviewer.conversation'
            if (log.get('stage')!=stage or log.get('outcome')!=call.get('outcome')
                    or (log.get('error_code')or '')!=(call.get('error_code')or '')):
                errors.append('actual_model_log_stage_or_outcome_mismatch')
            if log.get('request_id')!=turn_id or log.get('model_id')!=call.get('model_id'):
                errors.append('actual_model_log_turn_or_model_mismatch')
            if 'attempts'in log and (type(log['attempts'])is not int or log['attempts']!=call.get('attempts')):
                errors.append('actual_model_log_attempts_mismatch')
            # native 日志有usage；当前 GLM logger不输出usage，保留此证据边界。
            if 'usage'in log and counters(log['usage'])!=counters(call.get('usage')):
                errors.append('actual_model_log_usage_mismatch')
            if log.get('finish_reason')=='length' or log.get('error_code')=='response_truncated':
                errors.append('response_length_limit')
    return {'errors':list(dict.fromkeys(errors)),
            'actual_total_tokens':total if not any(k in errors for k in ('unknown_usage','actual_model_log_usage_mismatch')) else None,
            'primary_tokens':totals['primary'] if all_known else None,
            'reviewer_tokens':totals['reviewer'] if all_known else None,
            'known_tokens_lower_bound':known_lower if lower_observed else None,
            'actual_model_requests_sent':sum(call['request_sent']is True for call in calls)
             if calls and all(isinstance(call,dict) and type(call.get('request_sent'))is bool for call in calls) else None,
            'provider_cost':None}


def usage_lower_bound(usage):
    """可信 partial 字段可作下界；负数、bool 和相互矛盾的计数整条不计。"""
    if not isinstance(usage, dict):
        return None
    values = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens') if key in usage}
    if not values or any(type(value) is not int or value < 0 for value in values.values()):
        return None
    subtotal = sum(values.get(key, 0) for key in ('prompt_tokens', 'completion_tokens'))
    if 'total_tokens' in values:
        if (subtotal > values['total_tokens'] or len(values) == 3 and subtotal != values['total_tokens']):
            return None
    return subtotal if any(key in values for key in ('prompt_tokens', 'completion_tokens')) else values['total_tokens']


def recovery_record(runtime, cloud, provider_calls):
    trace = runtime.get('trace', [])
    rejected = [i for i, row in enumerate(trace) if row.get('status') == 'rejected'
                and row.get('code') == 'BOUNDARY_PUBLIC_QUERY']
    completed_after = bool(rejected and any(row.get('status') == 'completed'
        and row.get('tool') in ('knowledge_search', 'public_products') for row in trace[rejected[-1]+1:]))
    return {'status': 'not_observed' if not cloud or not rejected else
                     'observed_boundary_retry' if completed_after else 'rejected_without_successful_retry',
            'boundary_rejection_count': len(rejected), 'public_retrieval_completed_after_rejection': completed_after,
            'observed_provider_search_calls': len(provider_calls),
            'scope': '真实云模型可见 trace 的技术顺序；离线替身不算真实恢复，语义仍待独立裁决'}


def stop_reasons(record, cloud):
    result = record.get('response') if isinstance(record.get('response'),dict) else {}
    runtime = result.get('agent_runtime') if isinstance(result.get('agent_runtime'),dict) else {}
    calls = runtime.get('model_calls')
    calls = calls if isinstance(calls,list) else []
    reasons = []
    if record.get('http_status') != 200:
        reasons.append('http_failure')
    if result.get('isFallback') is not False:
        reasons.append('fallback_or_missing_flag')
    if runtime.get('failure') or result.get('error'):
        reasons.append('critical_technical_failure')
    if runtime.get('budget_limit'):
        reasons.append('budget_limit')
    if result.get('budgetLimit') or record.get('budgetLimit'):
        reasons.append('budget_limit')
    if any(type(runtime.get(key)) is int and runtime[key] > maximum for key, maximum in
           (('budget_tokens', 100000), ('rounds', 8), ('tool_calls', 12))):
        reasons.append('budget_contract_exceeded')
    if runtime.get('contract_errors'):
        reasons.append('answer_contract_error')
    if result.get('agent_review', {}).get('status') != 'approved':
        reasons.append('review_not_approved')
    if cloud and (runtime.get('usage_complete') is not True
                  or usage_record(runtime, True)['actual_total_tokens'] is None
                  or any(not isinstance(call,dict) or call.get('usage_complete') is not True for call in calls)):
        reasons.append('unknown_usage')
    if not runtime.get('model_calls'):
        reasons.append('model_trace_missing')
    if cloud:
        reasons.extend(paid_usage_audit(runtime, record.get('model_call_logs'),result.get('bank_response_id'))['errors'])
        if record.get('configured_models') != MODEL_IDENTITIES:
            reasons.append('actual_cloud_configuration_receipt_missing_or_mismatch')
        reasons.extend(audit_response_identity(runtime, record.get('model_call_logs'),record.get('response_metadata'),
            turn_id=result.get('bank_response_id'),configured_models=MODEL_IDENTITIES)['errors'])
        review_calls=[call for call in calls if isinstance(call,dict) and call.get('stage')=='reviewer']
        reviews=record.get('reviewer_records')
        if not isinstance(reviews,list) or len(reviews)!=len(review_calls) or not review_calls:
            reasons.append('actual_review_usage_observation_missing')
        else:
            for call,review in zip(review_calls,reviews):
                decision=review.get('decision') if isinstance(review,dict) else None
                if not isinstance(decision,dict) or decision.get('_model_usage')!=call.get('usage'):
                    reasons.append('actual_review_usage_observation_mismatch')
    if any(isinstance(call,dict) and call.get('error_code') == 'response_truncated' for call in calls):
        reasons.append('response_length_limit')
    if not record.get('financial_state_unchanged'):
        reasons.append('financial_side_effect')
    if record.get('personal_memory_state_unchanged')is not True:
        reasons.append('personal_memory_side_effect_or_missing_observation')
    if not record.get('speech_receipt_validated') or not record.get('speech_text_matches_reply'):
        reasons.append('speech_guard_failure')
    if record.get('status_http') != 200 or not record.get('fixed_release_preserved'):
        reasons.append('public_release_or_status_failure')
    if not isinstance(result.get('knowledge_preparation'), dict):
        reasons.append('real_prepare_round_receipt_missing')
    if result.get('bank_actions') or result.get('draft_ids'):
        reasons.append('unexpected_financial_draft')
    trace=runtime.get('trace')
    if not isinstance(trace,list) or any(not isinstance(row,dict) for row in trace):
        reasons.append('actual_tool_trace_missing')
        trace=[]
    if sum(row.get('code') == 'BOUNDARY_PUBLIC_QUERY' for row in trace) > 2:
        reasons.append('public_recovery_limit_exceeded')
    elapsed = record.get('elapsed_seconds')
    if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0<=elapsed<=BUDGET['task_seconds']:
        reasons.append('task_deadline_exceeded')
    return list(dict.fromkeys(reasons))


def permitted_url(request, cloud):
    url = request.full_url if hasattr(request, 'full_url') else str(request)
    parts = urlsplit(url)
    return ((parts.scheme == 'http' and parts.hostname == '127.0.0.1'
             and parts.username is None and parts.password is None)
            or cloud and url in MODEL_URLS)


def financial_state(bank, workspace):
    """完整行用于差分；仅本次合成 workspace。audit/对话历史是允许的读取日志。"""
    with closing(bank.connect()) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for table in FINANCIAL_TABLES:
            if table not in tables:
                result[table] = {'present': False, 'rows': []}
                continue
            columns = {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')}
            if 'workspace_id' not in columns:
                raise ValueError('financial_table_owner_column_missing:' + table)
            rows = [dict(row) for row in db.execute('SELECT * FROM "' + table + '" WHERE workspace_id=? ORDER BY rowid', (workspace,))]
            result[table] = {'present': True, 'rows': rows}
    return result


def personal_memory_state(gateway,workspace):
    """精确观察长期记忆/画像存储，保留哈希，不保存敏感正文。"""
    tables=('user_memories','user_memory_relations','user_memory_settings','profile_observations',
            'memory_candidates','conversation_sources')
    result={}
    with closing(gateway.personal_data.connect()) as db:
        for table in tables:
            columns=[row[1]for row in db.execute('PRAGMA table_info("'+table+'")')]
            if not columns or 'workspace_id'not in columns:raise ValueError('personal_memory_table_missing:'+table)
            projection=','.join('"'+col+'" AS "'+col+'_raw"'for col in columns)
            rows=[dict(row)for row in db.execute('SELECT '+projection+' FROM "'+table+'" WHERE workspace_id=? ORDER BY rowid',(workspace,))]
            result[table]={'row_count':len(rows),'raw_rows_sha256':canonical_sha(rows)}
    return result


def purge_candidate_modules(candidate):
    """每题服务关闭后卸载候选模块，避免下一题导入时复用上题 DB/cache 默认值。"""
    candidate = Path(candidate).resolve()
    for name, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if filename and Path(filename).resolve().is_relative_to(candidate):
            del sys.modules[name]


def assert_fresh_business_imports():
    if any(name == prefix or name.startswith(prefix + '.') for name in sys.modules for prefix in BUSINESS_MODULES):
        raise ValueError('business_module_imported_before_isolation')


def execute_case(candidate, case, inputs, out, cloud, seconds):
    started = time.monotonic()
    model_contract = configured_model_contract(candidate) if cloud else None
    assert_fresh_business_imports()
    from cryptography.fernet import Fernet
    with tempfile.TemporaryDirectory(prefix='navren-public-' + case['id'] + '-') as directory:
        state = Path(directory).resolve()
        case_out = out / case['id']
        case_out.mkdir()
        bank = state / 'bank.sqlite3'
        knowledge = state / 'knowledge.sqlite3'
        # create_server 正常接线从显式 bank 路径派生该市场库。
        market = state / 'bank.market.sqlite3'
        environment = {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
                       'NAVREN_SIM_DB': str(bank), 'QINGZHOU_SIM_DB': str(bank),
                       'NAVREN_KNOWLEDGE_DB': str(knowledge), 'NAVREN_MARKET_DB': str(market),
                       'NAVREN_FUND_CACHE': str(state / 'funds'),
                       'NAVREN_MODEL_CALL_LOG': str(case_out / 'model-calls.jsonl'),
                       'NAVREN_ALLOW_CLOUD_ASR': '0', 'NAVREN_AGENT_TIMEOUT': str(seconds),
                       'PYTHONDONTWRITEBYTECODE': '1'}
        if not cloud:
            environment.update(AIPING_API_KEY='offline-no-provider')
        paths = {key: value for key, value in environment.items() if key.endswith(('_DB', '_CACHE', '_LOG'))}
        atomic_json(case_out / 'isolation.json', {'paths': paths, 'workspace_id': None,
                    'private_key_storage': 'temporary_process_environment_only', 'secret_values_recorded': False,
                    'environment_set_before_business_import': True, 'cold_network': False,
                    'cloud_audio': False, 'cleanup_policy': 'close_server_then_TemporaryDirectory_context'})
        shutil.copy2(inputs['database'], knowledge)
        if sha(knowledge) != DATABASE_SHA:
            raise ValueError('isolated_public_copy_changed')
        real_open = urllib.request.urlopen
        real_opener_open = urllib.request.OpenerDirector.open
        def guarded_open(request, *args, **kwargs):
            if not permitted_url(request, cloud):
                raise ValueError('unregistered_network_request')
            return real_open(request, *args, **kwargs)
        def guarded_opener(opener, request, *args, **kwargs):
            if not permitted_url(request, cloud):
                raise ValueError('unregistered_network_request')
            return real_opener_open(opener, request, *args, **kwargs)
        previous_path = list(sys.path)
        sys.path.insert(0, str(candidate))
        server = thread = runtime = None
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, environment))
                stack.enter_context(patch('urllib.request.urlopen', guarded_open))
                stack.enter_context(patch.object(urllib.request.OpenerDirector, 'open', guarded_opener))
                from agent_runtime.conversation import ConversationAgent
                from local_knowledge import LocalKnowledgeProvider
                from local_knowledge.runtime import EvidenceRuntime
                from local_knowledge.store import KnowledgeError
                from server import create_server, ProxyHandler
                from voice_gateway import VoiceGateway
                from scripts.run_skill_acceptance import TracePrimary, TraceReviewer, safe_data
                import fund_history
                if fund_history.CACHE.resolve() != (state / 'funds'):
                    raise ValueError('fund_cache_not_isolated')
                imported = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
                            if getattr(module, '__file__', None) and
                            any(name == prefix or name.startswith(prefix + '.') for prefix in BUSINESS_MODULES)}
                if any(not Path(path).is_relative_to(candidate) for path in imported.values()):
                    raise ValueError('business_module_outside_candidate')
                provider = LocalKnowledgeProvider(knowledge, release_id=RELEASE)
                provider_calls = []
                original_search = LocalKnowledgeProvider.search_public
                def observed_search(provider_instance, request, release_id):
                    call = {'sequence': len(provider_calls), 'request': deepcopy(request), 'release_id': release_id,
                            'provider_path': str(provider_instance.path.resolve()), 'status': 'started'}
                    provider_calls.append(call)
                    try:
                        result = original_search(provider_instance, request, release_id)
                        call.update(status='completed', citation_ids=deepcopy(result.get('citation_ids', [])))
                        return result
                    except Exception as error:
                        call.update(status='failed', error_code=getattr(error, 'code', type(error).__name__))
                        raise
                stack.enter_context(patch.object(LocalKnowledgeProvider, 'search_public', observed_search))
                cold_calls = []
                def cold_network_disabled(_provider, source_id, url, **kwargs):
                    cold_calls.append({'source_id': source_id, 'url': url, 'outcome': 'cold_network_disabled'})
                    raise KnowledgeError('cold_network_disabled')
                # 实际运行类及 prepare_round 保留；固定reader只关闭本专项未授权的冷外网。
                runtime = EvidenceRuntime(provider, reader=cold_network_disabled, background=False)
                preparation_spy = stack.enter_context(patch.object(runtime, 'prepare_round', wraps=runtime.prepare_round))
                if cloud:
                    primary = TracePrimary(case_out / 'model-calls.jsonl')
                    reviewer = compatible_trace_reviewer(TraceReviewer, safe_data)(case_out / 'model-calls.jsonl')
                    if (primary.model_id != model_contract['primary']['model_id']
                            or reviewer.model_id != model_contract['reviewer']['model_id']
                            or reviewer.client.provider != model_contract['reviewer']['provider']):
                        raise ValueError('actual_cloud_client_configuration_mismatch')
                    if primary.output_token_limit != BUDGET['native_primary_output_tokens']:
                        raise ValueError('native_output_contract_changed')
                    if primary.billing_mode != 'paid' or reviewer.billing_mode != 'paid':
                        raise ValueError('cloud_clients_not_original_paid_chain')
                else:
                    from scripts.run_rag_acceptance import ProtocolPrimary, ProtocolReviewer
                    class OfflinePrimary(ProtocolPrimary):
                        billing_mode = 'unpaid_fixture'
                    class OfflineReviewer(ProtocolReviewer):
                        billing_mode = 'unpaid_fixture'
                    primary, reviewer = OfflinePrimary(deepcopy(case)), OfflineReviewer()
                agent = ConversationAgent(primary, reviewer, max_rounds=8, max_tools=12,
                                          timeout_seconds=max(.01, seconds-(time.monotonic()-started)), max_tokens=100000)
                stack.enter_context(patch.object(ProxyHandler, 'log_message', lambda *args: None))
                server = create_server(0, bank, voice=VoiceGateway(key='isolated-no-cloud-audio'),
                                       conversation_agent=agent, evidence_runtime=runtime)
                def close_server_before_restoring_guards():
                    nonlocal server, thread, runtime
                    if server is not None:
                        if thread is not None:
                            server.shutdown()
                        server.server_close()
                    if thread is not None:
                        thread.join(timeout=3)
                    server = thread = runtime = None
                # LIFO：先等所有请求结束，再恢复环境/网络守门；超时请求也不能漏出隔离。
                stack.callback(close_server_before_restoring_guards)
                server.finance.knowledge_provider = provider
                if server.finance.bank.database != bank or server.market.path.resolve() != market:
                    raise ValueError('database_path_not_isolated')
                paths['personal_database'] = str(server.finance.personal_data.database.resolve())
                if not server.finance.personal_data.database.resolve().is_relative_to(state):
                    raise ValueError('personal_database_not_isolated')
                status = provider.status()
                if status.get('public_release_id') != RELEASE or status.get('published_chunk_count') != 22:
                    raise ValueError('unexpected_public_release')
                cookies = cookiejar.CookieJar()
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                base = 'http://127.0.0.1:' + str(server.server_port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def request(path, body=None, csrf=None):
                    remaining = seconds-(time.monotonic()-started)
                    if remaining <= 0:
                        raise TimeoutError('task_deadline_exceeded')
                    headers = {'Content-Type': 'application/json', 'Origin': base}
                    if csrf:
                        headers['X-QZ-CSRF'] = csrf
                    req = urllib.request.Request(base+path, headers=headers,
                        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
                    try:
                        response = client.open(req, timeout=remaining)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        return response.status, json.loads(response.read()), dict(response.headers)
                code, session, _headers = request('/api/finance/session', {})
                if code != 200:
                    raise ValueError('session_setup_failed')
                workspace = session['workspace_id']
                server.finance.wealth.provision(workspace)
                server.finance.memories.state(workspace)
                atomic_json(case_out / 'isolation.json', {'paths': paths, 'workspace_id': workspace,
                    'private_key_storage': 'temporary_process_environment_only', 'secret_values_recorded': False,
                    'environment_set_before_business_import': True, 'cold_network': False,
                    'cloud_audio': False, 'cleanup_policy': 'close_server_then_TemporaryDirectory_context'})
                before = financial_state(server.finance.bank, workspace)
                personal_before=personal_memory_state(server.finance,workspace)
                atomic_json(case_out / 'financial-before.json', before)
                status_http, status_body, _headers = request('/api/finance/knowledge/status')
                code, result, headers = request('/api/agent', agent_body(case), session['csrf'])
                after = financial_state(server.finance.bank, workspace)
                personal_after=personal_memory_state(server.finance,workspace)
                atomic_json(case_out / 'financial-after.json', after)
                speech_text, speech_valid, speech_error = None, False, None
                if result.get('speech_receipt'):
                    try:
                        token = next(cookie.value for cookie in cookies if cookie.name == 'qz_bank_user')
                        speech_text = server.finance.resolve_speech(workspace, result['speech_receipt'], token)
                        speech_valid = bool(isinstance(speech_text, str) and speech_text.strip())
                    except Exception as error:
                        speech_error = getattr(error, 'code', type(error).__name__)
                snapshot = result.get('knowledge_snapshot') or {}
                record = {'id': case['id'], 'question': case['question'], 'question_sha256': hashlib.sha256(case['question'].encode()).hexdigest(),
                    'original_case_canonical_sha256': canonical_sha(case), 'mode': 'cloud' if cloud else 'offline_protocol_precheck',
                    'http_status': code, 'release_header': headers.get('X-Navren-Release'),
                    'status_http': status_http, 'provider_status': status_body, 'response': result,
                    'model_tool_calls': primary.calls, 'tool_outputs': primary.tool_outputs,
                    'provider_search_calls': provider_calls,
                    'reviewer_records': reviewer.records, 'response_metadata': {
                        'primary': getattr(primary, 'response_metadata', []), 'reviewer': getattr(reviewer, 'response_metadata', [])},
                    'configured_models': model_contract,
                    'workspace_id': workspace, 'isolated_paths': paths, 'business_module_paths': imported,
                    'runtime_configuration': {'class': EvidenceRuntime.__module__+'.'+EvidenceRuntime.__name__,
                        'background': False, 'reader': 'cold_network_disabled', 'real_prepare_round_path_preserved': True,
                        'prepare_round_invocations': preparation_spy.call_count,
                        'cold_reader_calls': cold_calls, 'audio_cloud_requested': False},
                    'financial_before_path': str(case_out / 'financial-before.json'),
                    'financial_after_path': str(case_out / 'financial-after.json'),
                    'financial_state_unchanged': before == after, 'financial_before_sha256': canonical_sha(before),
                    'financial_after_sha256': canonical_sha(after), 'speech_receipt_validated': speech_valid,
                    'personal_memory_before':personal_before,'personal_memory_after':personal_after,
                    'personal_memory_state_unchanged':personal_before==personal_after,
                    'speech_text': speech_text, 'speech_error_code': speech_error,
                    'speech_text_matches_reply': speech_text == unicodedata.normalize('NFKC', result.get('reply', '')).strip(),
                    'fixed_release_preserved': snapshot.get('release_id') == RELEASE,
                    'elapsed_seconds': round(time.monotonic()-started, 3),
                    'usage': usage_record(result.get('agent_runtime', {}), cloud),
                    'semantic_judgment': None, 'semantic_status': 'PENDING_INDEPENDENT_JUDGMENT' if cloud else 'NOT_EVALUATED_OFFLINE',
                    'recovery': recovery_record(result.get('agent_runtime', {}), cloud, provider_calls),
                    'actual_child_status': None, 'actual_child_exit_code': None}
                log_path = case_out / 'model-calls.jsonl'
                logs = read_model_log_prefix(log_path)
                record.update(model_call_logs=logs, model_call_log_path=str(log_path),
                              model_call_log_sha256=sha(log_path) if log_path.is_file() else None)
                if cloud:
                    record['paid_usage_audit']=paid_usage_audit(result.get('agent_runtime',{}),logs,result.get('bank_response_id'))
                    audited=record['paid_usage_audit']
                    record['usage'].update(actual_total_tokens=audited['actual_total_tokens'],
                        actual_primary_tokens=audited['primary_tokens'],actual_reviewer_tokens=audited['reviewer_tokens'],
                        known_tokens_lower_bound=audited['known_tokens_lower_bound'],
                        status='unknown_usage' if audited['actual_total_tokens']is None else 'provider_reported',
                        actual_requests_sent=audited['actual_model_requests_sent'])
                record['stop_reasons'] = stop_reasons(record, cloud)
                atomic_json(case_out / 'record.json', record)
                return record
        finally:
            if server is not None:
                if thread is not None:
                    server.shutdown()
                server.server_close()
            elif runtime is not None:
                runtime.close()
            if thread is not None:
                thread.join(timeout=3)
            sys.path[:] = previous_path
            purge_candidate_modules(candidate)


def run(args):
    out = Path(args.out_dir).resolve()
    if out == OUTPUT_ROOT.resolve() or not out.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError('output_must_be_under_integration')
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'schema_version': VERSION, 'started_at': now(), 'status': 'preflight',
              'mode': 'cloud' if args.cloud else 'offline_protocol_precheck', 'budget': BUDGET,
              'probe_order': list(ORDER), 'records': [], 'cloud_model_calls': 0 if not args.cloud else None,
              'semantic_judgment': None, 'actual_child_status': None, 'actual_child_exit_code': None,
              'total_cost': None, 'no_live_database_access': True, 'source_frozen_by': 'root_outer_executor',
              'limitations': ['实际退出、进程组超时/清理由外层记录', '离线替身不能证明金融语义、真实模型恢复或生产稳定',
                              '仅消费原固定公共发布；关闭冷网络和后台晋升；不请求云音频']}
    exit_code = 1
    try:
        sys.dont_write_bytecode = True
        candidate = Path(args.candidate).resolve()
        input_paths = [candidate, Path(args.source_manifest).resolve(), Path(args.data_manifest).resolve()]
        if args.cloud and args.handoff:
            input_paths.append(Path(args.handoff).resolve())
        if any(not path.is_relative_to(OUTPUT_ROOT.resolve()) for path in input_paths):
            raise ValueError('inputs_must_be_under_integration')
        inputs = inspect_inputs(candidate, args.source_manifest, args.data_manifest)
        report.update(candidate_root=str(candidate), source_sha256=inputs['sources'], data_sha256=inputs['datasets'],
            auxiliary_sha256=AUXILIARY,
            running_adapter_path=str(Path(__file__).resolve()), running_adapter_sha256=sha(__file__),
            source_manifest_path=str(Path(args.source_manifest).resolve()), source_manifest_sha256=sha(args.source_manifest),
            data_manifest_path=str(Path(args.data_manifest).resolve()), data_manifest_sha256=sha(args.data_manifest),
            original_question_set_path=str(inputs['questions']), original_question_set_sha256=QUESTION_SHA,
            public_manifest_path=str(inputs['public_manifest']), public_manifest_sha256=PUBLIC_MANIFEST_SHA,
            public_database_path=str(inputs['database']), public_database_sha256=DATABASE_SHA, release_id=RELEASE)
        tracked = {str(path): sha(path) for path in (Path(args.source_manifest), Path(args.data_manifest),
                                                   inputs['questions'], inputs['public_manifest'], inputs['database'])}
        tracked.update({str(candidate / relative): digest for relative, digest in AUXILIARY.items()})
        if args.cloud:
            report['configured_models'] = configured_model_contract(candidate)
            report['cloud_handoff_gate'] = cloud_gate(args.handoff, args.handoff_sha256,
                                                    inputs['sources'], inputs['datasets'], inputs['contract'])
            tracked[str(Path(args.handoff))] = sha(args.handoff)
            tracked[report['cloud_handoff_gate']['g1_run_path']] = report['cloud_handoff_gate']['g1_run_sha256']
            tracked[report['cloud_handoff_gate']['g1_execution_path']] = report['cloud_handoff_gate']['g1_execution_sha256']
        report['status'] = 'running'
        atomic_json(out / 'results.json', report)
        for case in inputs['cases']:
            if time.monotonic()-started >= BUDGET['whole_seconds']-BUDGET['task_seconds']:
                report['stop_reason'] = 'whole_deadline_insufficient_for_next_task'
                break
            if (inputs['contract'].source_manifest(candidate) != inputs['sources']
                    or inputs['contract'].data_manifest(candidate) != inputs['datasets']
                    or inputs['contract'].auxiliary_manifest(candidate) != AUXILIARY
                    or any(sha(path) != digest for path, digest in tracked.items())):
                raise ValueError('frozen_input_drift')
            if args.cloud:
                cloud_gate(args.handoff, args.handoff_sha256, inputs['sources'], inputs['datasets'], inputs['contract'])
            case_started = time.monotonic()
            record = execute_case(candidate, case, inputs, out, args.cloud,
                                  min(BUDGET['task_seconds'], BUDGET['whole_seconds']-(case_started-started)))
            # execute_case 的 return 经过 finally、服务关闭和临时目录退出；以此结束同一原任务。
            record['elapsed_seconds'] = time.monotonic()-case_started
            record['complete_prepare_http_cleanup'] = True
            report['records'].append(record)
            cleanup_paths = [Path(path) for key, path in record.get('isolated_paths', {}).items()
                             if key.endswith(('_DB', '_CACHE')) or key == 'personal_database']
            record['temporary_directory_removed'] = bool(cleanup_paths) and all(not path.exists() for path in cleanup_paths)
            record['cleanup_completed'] = record['temporary_directory_removed']
            record['stop_reasons'] = list(dict.fromkeys(record.get('stop_reasons', []) + stop_reasons(record, args.cloud)
                + ([] if record['cleanup_completed'] else ['isolated_cleanup_not_completed'])))
            if (out / case['id']).is_dir():
                atomic_json(out / case['id'] / 'record.json', record)
            atomic_json(out / 'results.json', report)
            if record['stop_reasons']:
                report['stop_reason'] = record['stop_reasons']
                break
        report['candidate_source_unchanged'] = inputs['contract'].source_manifest(candidate) == inputs['sources']
        report['candidate_data_unchanged'] = inputs['contract'].data_manifest(candidate) == inputs['datasets']
        report['candidate_auxiliary_unchanged'] = inputs['contract'].auxiliary_manifest(candidate) == AUXILIARY
        report['tracked_inputs_unchanged'] = all(sha(path) == digest for path, digest in tracked.items())
        if not all(report[key] for key in ('candidate_source_unchanged', 'candidate_data_unchanged',
                                         'candidate_auxiliary_unchanged', 'tracked_inputs_unchanged')):
            raise ValueError('frozen_input_drift')
        if len(report['records']) == len(ORDER) and not report.get('stop_reason'):
            report['status'] = 'COLLECTED_PENDING_INDEPENDENT_JUDGMENT' if args.cloud else 'OFFLINE_PROTOCOL_PRECHECK_COMPLETED'
            exit_code = 0
        else:
            report['status'] = 'STOPPED_TECHNICAL_FAILURE'
    except Exception as error:
        report['status'] = 'FIXTURE_OR_DEPENDENCY_ERROR'
        report['error'] = {'type': type(error).__name__, 'code': str(error)[:240]}
    finally:
        report['completed_at'] = now()
        report['duration_seconds'] = round(time.monotonic()-started, 3)
        if report['duration_seconds'] > BUDGET['whole_seconds']:
            report['status'] = 'STOPPED_TECHNICAL_FAILURE'
            report['stop_reason'] = 'whole_deadline_exceeded'
            exit_code = 1
        report['returned_exit_code'] = exit_code
        atomic_json(out / 'results.json', report)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--data-manifest', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--handoff', type=Path)
    parser.add_argument('--handoff-sha256')
    args = parser.parse_args()
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
