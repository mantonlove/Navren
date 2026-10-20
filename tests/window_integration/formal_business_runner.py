"""冻结六业务题集的隔离 HTTP 承接器。默认仅预检，云执行须另交接放行。

离线原生替身只验证正式调用链，不执行冻结题目，不产生模型语义成绩。
所有裁判字段留在本地；本人 harness 不补模型草案或审核。
"""
from __future__ import annotations

import argparse
import ast
import base64
from contextlib import closing, contextmanager, ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import http.cookiejar
import importlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from unittest.mock import patch
if str(Path(__file__).resolve().parent) not in sys.path:sys.path.insert(0,str(Path(__file__).resolve().parent))
from registered_entry_context import (build_template,template_factory,attach_server,browser_bootstrap,browser_consume,request_context)
from registered_profile_templates import build_profile_template

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TASKS = ROOT / 'evaluation/window_integration/formal_business_tasks.json'
TASKS_SHA = '99eaeba3d2519f63c130b50c8a696809d60e68486e578546104e78a70a4e1d24'
SETUP = ROOT / 'evaluation/window_integration/formal_fixture_setup.json'
SETUP_SHA = '6571a0704e816dcc0bb446a63f28f57578407a2e322a6c4f678671e62dc7da6f'
TASK_STEPS = ROOT / 'evaluation/window_integration/formal_task_steps_supplement_v2.json'
TASK_STEPS_SHA = '4b036d1f37333023f70629113ec5211dbebfd38b831bccb684444b5517f65007'
PERMISSION_COMPATIBILITY = ROOT / 'evaluation/window_integration/formal_permission_compatibility.json'
PERMISSION_COMPATIBILITY_SHA = 'b3c172fa3fa656b1ba354a977c92818b0be8d688b113b41e318e5402e031b36e'
RUN_LABEL = 'original39_task_texts_with_explicit_task_steps_v2_supplement'
OUTPUT = ROOT / 'outputs/window-progress/integration/formal-business-runner'
SECRET_KEYS = {'csrf', 'verification_code', 'verification_token', 'verification_hash',
               'token_hash', 'agent_token_hash', 'pin_hash', 'session_token', 'agent_token',
               'human_verification_code', 'authorization', 'api_key'}
TERMINAL = {'succeeded', 'settled', 'pending_settlement'}
LIMITS = {'seconds': 105, 'rounds': 8, 'tools': 12, 'tokens': 100000}
CLEANUP_SECONDS = 5
_RETAINED_FIXTURES = []


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sanitize(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if k.lower() in SECRET_KEYS else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def save(path, value):
    Path(path).write_text(json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False) + '\n')


class Stop(Exception):
    def __init__(self, classification, reason, *, batch_stop=None, details=None):
        super().__init__(reason)
        self.classification, self.reason = classification, reason
        self.batch_stop = classification in ('technical_timeout_or_budget_exceeded', 'paid_accounting_failure') if batch_stop is None else batch_stop
        self.details = details


_PAID_PROTOCOL = None
def paid_protocol():
    """只导入独立stdlib审计代码，不加载业务/密钥/网络。依赖显式冻结。"""
    global _PAID_PROTOCOL
    if _PAID_PROTOCOL is None:
        path = Path(__file__).with_name('public_probe_runner.py')
        spec = importlib.util.spec_from_file_location('_formal_paid_protocol', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PAID_PROTOCOL = module
    return _PAID_PROTOCOL


def draft_payload_matches(actual, expected):
    """Quote membership is unordered; every other original field stays exact."""
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    if set(actual) != set(expected):
        return False
    if expected.get('action') != 'book_services' or 'quote_ids' not in expected:
        return canonical(actual) == canonical(expected)
    left, right = actual['quote_ids'], expected['quote_ids']
    if any(not isinstance(ids, list) or not 1 <= len(ids) <= 6
           or any(not isinstance(value, str) or not value for value in ids)
           or len(set(ids)) != len(ids) for ids in (left, right)):
        return False
    return (set(left) == set(right)
            and canonical({k: v for k, v in actual.items() if k != 'quote_ids'})
            == canonical({k: v for k, v in expected.items() if k != 'quote_ids'}))


def load_contract():
    if sha(TASKS) != TASKS_SHA:
        raise Stop('fixture_drift', 'formal_tasks_sha_changed', batch_stop=True)
    value = json.loads(TASKS.read_text())
    if value['minimum_design']['expanded_attempts_per_round'] != 13:
        raise Stop('runner_error', 'unexpected_frozen_task_shape')
    if sha(PERMISSION_COMPATIBILITY) != PERMISSION_COMPATIBILITY_SHA:
        raise Stop('fixture_drift', 'formal_permission_compatibility_sha_changed', batch_stop=True)
    compatibility = json.loads(PERMISSION_COMPATIBILITY.read_text())
    if compatibility['original_dataset_sha256'] != TASKS_SHA:
        raise Stop('fixture_drift', 'formal_permission_original_binding_changed', batch_stop=True)
    for update in compatibility['updates']:
        case = next(c for c in value['cases'] if c['id'] == update['case_id'])
        step = next(s for s in case['steps'] if s['id'] == update['step_id'])
        grading = step['grading_only']
        if 'before_action' in update:
            if step['action'] != update['before_action'] or grading['expected_error'] != update['before_expected_error']:
                raise Stop('fixture_drift', 'formal_permission_action_before_mismatch', batch_stop=True)
            step['action'] = update['after_action']
            grading['expected_error'] = deepcopy(update['after_expected_error'])
        elif 'before_verification_required' in update:
            if grading['verification_required'] is not update['before_verification_required']:
                raise Stop('fixture_drift', 'formal_permission_grade_before_mismatch', batch_stop=True)
            grading['verification_required'] = update['after_verification_required']
        elif 'before_semantic_criterion' in update:
            index = grading['semantic_criteria'].index(update['before_semantic_criterion'])
            grading['semantic_criteria'][index] = update['after_semantic_criterion']
        else:
            raise Stop('fixture_drift', 'formal_permission_unknown_update', batch_stop=True)
    return value



def check_schema_delta(current_path, archive_path):
    """只准入这两处已明确的枚举增量；其余AST必须与原archive全等。"""
    old = ast.parse(archive_path.read_text())
    new = ast.parse(current_path.read_text())
    def specs(tree):
        return next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'SPECS' for t in node.targets))
    old_node, new_node = specs(old), specs(new)
    old_specs, new_specs = ast.literal_eval(old_node.value), ast.literal_eval(new_node.value)
    changes = []
    if len(old_specs) != len(new_specs):
        raise ValueError('seed_schema_entity_count_changed')
    for previous, current in zip(old_specs, new_specs):
        if previous != current:
            if (previous[0] not in ('cards', 'card_applications') or previous[:3] != current[:3]
                    or current[3] != previous[3].replace('status(debit|credit)', 'status(debit|credit|virtual)')):
                raise ValueError('unsupported_seed_schema_delta')
            changes.append(previous[0])
    if set(changes) != {'cards', 'card_applications'}:
        raise ValueError('declared_schema_delta_not_exact')
    new_node.value = deepcopy(old_node.value)
    if ast.dump(new, include_attributes=False) != ast.dump(old, include_attributes=False):
        raise ValueError('other_seed_schema_logic_changed')
    return {'changed_entities': changes, 'other_AST_unchanged': True}

@contextmanager
def archived_generation(root, core):
    """仅临时默认闭包账户初始化，复用封存populate及其真实来源指纹。"""
    archive = root / core['generation_sources_archive']
    for name, digest in core['generation_source_sha256'].items():
        path = archive / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('archived_generation_source_changed:' + name)
    name = '_navren_sealed_generation_' + secrets.token_hex(8)
    package = types.ModuleType(name)
    package.__path__ = [str(archive / 'simulated_bank')]
    sys.modules[name] = package
    try:
        old = importlib.import_module(name + '.customer_seed')
        current = importlib.import_module('simulated_bank.customer_archive')
        with patch.object(current, 'populate', old.populate):
            yield
    finally:
        for imported in list(sys.modules):
            if imported == name or imported.startswith(name + '.'):
                sys.modules.pop(imported, None)

def frozen_inputs(contract):
    expected = {str(TASKS.relative_to(ROOT)): TASKS_SHA, str(SETUP.relative_to(ROOT)): SETUP_SHA,
                str(TASK_STEPS.relative_to(ROOT)): TASK_STEPS_SHA,
                str(PERMISSION_COMPATIBILITY.relative_to(ROOT)): PERMISSION_COMPATIBILITY_SHA}
    core = contract['fixture_contract']['new_closed_seed_v1']
    expected[core['core_path']] = core['core_sha256']
    archive = core['generation_sources_archive']
    for name, digest in core['generation_source_sha256'].items():
        expected[str(Path(archive) / name)] = digest
    profiles = contract['fixture_contract']['four_profiles_oracle_v1']
    expected[profiles['oracle_path']] = profiles['oracle_sha256']
    for name, digest in profiles['source_file_sha256'].items():
        expected[str(Path(profiles['source_directory']) / name)] = digest
    for name, digest in expected.items():
        target = ROOT / name
        if target.is_symlink() or not target.is_file() or sha(target) != digest:
            raise Stop('fixture_drift', 'frozen_input_changed:' + name, batch_stop=True)
    # 正常服务构造后仍使用封存的补充 seed。当前业务服务允许修复；闭包原件不可变。
    for name in ('simulated_bank/customer_seed.py',
                 'simulated_bank/customer_archive.py', 'simulated_bank/reference/fineract-client-examples.json'):
        if sha(ROOT / name) != core['generation_source_sha256'][name]:
            raise Stop('fixture_drift', 'active_seed_is_not_closed_seed:' + name, batch_stop=True)
    schema_name = 'simulated_bank/customer_schema.py'
    check_schema_delta(ROOT / schema_name, ROOT / archive / schema_name)
    expected[schema_name] = sha(ROOT / schema_name)
    return expected



def load_task_steps_supplement():
    if sha(TASK_STEPS) != TASK_STEPS_SHA:
        raise Stop('fixture_drift', 'task_steps_supplement_sha_changed', batch_stop=True)
    value = json.loads(TASK_STEPS.read_text())
    if value['original_dataset_sha256'] != TASKS_SHA or value['grade_contract']['new_run_label'] != RUN_LABEL:
        raise Stop('fixture_drift', 'task_steps_supplement_original_dataset_binding_changed', batch_stop=True)
    return value


class TaskStepOracle:
    """版本化独立四行合同；原始ID绑定、累计向量与本步转换分别核对。"""
    def __init__(self, raw, workspace, task, supplement):
        self.workspace, self.task, self.supplement = workspace, task, supplement
        rows = scoped(raw, 'task_steps', workspace)
        required = supplement['stable_row_binding']['required_initial_rows']
        if len(rows) != supplement['stable_row_binding']['expected_count']:
            raise Stop('fixture_error', 'frozen_task_step_initial_row_count_mismatch')
        self.initial, self.bindings = {}, {}
        for item in required:
            expected = {k: self.bindings.get(v[1:], 'MISSING_BINDING') if isinstance(v, str) and v.startswith('$') else v
                        for k, v in item.items() if k != 'binding'}
            row = one(rows, lambda r: r['task_id'] == task and all(r.get(k) == v for k, v in expected.items()), item['binding'])
            self.bindings[item['binding']] = row['id']
            self.initial[row['id']] = deepcopy(row)
        if len(self.initial) != len(rows):
            raise Stop('fixture_error', 'frozen_task_step_initial_identity_mismatch')

    def check(self, before, after, case_id, step_id):
        selected = [r for r in self.supplement['step_status_matrix'] if r['case'] == case_id and r['step'] == step_id]
        if case_id in self.supplement['grade_contract']['supplement_applies_only_to_cases']:
            if len(selected) != 1:
                raise Stop('not_run', 'task_step_matrix_interface_gap:' + case_id + '/' + step_id)
            matrix = selected[0]
        else:
            statuses = {r['action']: r['status'] for r in self.initial.values()}
            matrix = {'expected_statuses_before_step': statuses, 'expected_cumulative_statuses': statuses,
                      'allowed_status_transitions_this_step': []}
        observed = []
        for raw, expected in ((before, matrix['expected_statuses_before_step']),
                              (after, matrix['expected_cumulative_statuses'])):
            rows = scoped(raw, 'task_steps', self.workspace)
            if len(rows) != len(self.initial) or {r['id'] for r in rows} != set(self.initial):
                raise Stop('necessary_condition_failure', 'task_step_row_insertion_deletion_or_id_changed')
            current = {r['id']: r for r in rows}
            for identity, initial in self.initial.items():
                if canonical({k: v for k, v in current[identity].items() if k != 'status'}) != canonical(
                        {k: v for k, v in initial.items() if k != 'status'}):
                    raise Stop('necessary_condition_failure', 'task_step_nonstatus_field_changed:' + identity)
            statuses = {current[identity]['action']: current[identity]['status'] for identity in self.initial}
            if canonical(statuses) != canonical(expected):
                raise Stop('necessary_condition_failure', 'task_step_exact_cumulative_vector_mismatch',
                           details={'expected': expected, 'actual': statuses})
            observed.append(statuses)
        transitions = [{'action': action, 'from': observed[0][action], 'to': status}
                       for action, status in observed[1].items() if status != observed[0][action]]
        if canonical(sorted(transitions, key=canonical)) != canonical(sorted(matrix['allowed_status_transitions_this_step'], key=canonical)):
            raise Stop('necessary_condition_failure', 'task_step_this_step_transition_mismatch')
        return {'supplement_sha256': TASK_STEPS_SHA, 'initial_row_bindings': self.bindings,
                'before': observed[0], 'after': observed[1], 'actual_transitions': transitions,
                'status': 'exact_four_row_contract_satisfied'}


def source_manifest():
    paths = [p for p in ROOT.rglob('*.py') if not any(x in p.relative_to(ROOT).parts
             for x in ('.venv', '.git', 'outputs', 'tests', '__pycache__'))]
    paths += list((ROOT / 'skills').rglob('*.md'))
    paths += [Path(__file__), Path(__file__).with_name('test_formal_business_runner.py'),
              Path(__file__).with_name('public_probe_runner.py'),
              Path(__file__).with_name('semantic_runner_adapter.py'),
              Path(__file__).with_name('registered_entry_context.py'),
              Path(__file__).with_name('registered_fixture_template.py'),
              Path(__file__).with_name('registered_profile_templates.py'),
              Path(__file__).with_name('paid_entry_admission.py'),
              Path(__file__).with_name('paid_campaign.py'), ROOT / 'tests/test_sms_permissions.py']
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(paths) if p.is_file()}


def expand_cases(contract):
    return [(case, variant) for case in contract['cases']
            for variant in case.get('expand_variants', [None])]


def model_request(step):
    # 构造新对象，不复制、清洗后再发整个题目；这也是唯一正式模型请求投影。
    text = step.get('user_message')
    if step.get('actor') != 'model' or not isinstance(text, str) or not text.strip():
        raise Stop('runner_error', 'model_projection_requires_original_user_message')
    return {'text': text, 'mode': 'classic'}


@dataclass
class AttemptBudget:
    clock: object = time.monotonic
    started: float | None = None
    rounds: int = 0
    tools: int = 0
    tokens: int = 0
    provider_tokens: int = 0
    unknown_usage: bool = False
    outer_deadline: float | None = None
    billing_mode: str = 'paid'
    configured_models: dict | None = None
    known_tokens_lower_bound: int = 0
    known_requests_sent_lower_bound: int = 0
    request_count_unknown: bool = False
    batch_stop: bool = False
    admission_closed: bool = False
    stop_reason: str | None = None
    observations: list = field(default_factory=list)
    seen_turns: dict = field(default_factory=dict)
    transports: dict = field(default_factory=dict)
    log_offsets: dict = field(default_factory=dict)
    metadata_offsets: dict = field(default_factory=dict)
    review_offset: int = 0
    lower_by_turn: dict = field(default_factory=dict)
    failure_prefixes: list = field(default_factory=list)
    loaded_source_binding: dict | None = None
    accounting_lock: object = field(default_factory=threading.RLock, repr=False)

    def latch_paid(self, reason, *, unknown=False):
        with self.accounting_lock:
            self.batch_stop = True
            self.stop_reason = self.stop_reason or reason
            self.unknown_usage = self.unknown_usage or unknown

    def merge_lower_bound(self, turn_id, amount):
        if isinstance(turn_id, str) and type(amount) is int and amount >= 0:
            self.lower_by_turn[turn_id] = max(self.lower_by_turn.get(turn_id, 0), amount)
            self.known_tokens_lower_bound = sum(self.lower_by_turn.values())

    def preserve_unaccounted_failure(self, reason):
        """先止付；只读未入账真实日志，不推进正常offset，晚到回执按turn取max。"""
        if self.billing_mode != 'paid':
            return
        with self.accounting_lock:
            self.latch_paid(reason, unknown=True)
            self.request_count_unknown = True
            files, grouped, errors = [], {}, []
            paths = []
            for stage, client in self.transports.items():
                transport = client if stage == 'primary' else getattr(client, 'client', None)
                value = getattr(transport, 'log_path', None) or os.environ.get('NAVREN_MODEL_CALL_LOG')
                if value is not None:
                    paths.append(Path(value))
            for path in dict.fromkeys(paths):
                offset = self.log_offsets.get(str(path), 0)
                try:
                    raw = paid_protocol().read_model_log_prefix(path)
                    if not isinstance(raw, list) or len(raw) < offset:
                        raise ValueError('unaccounted_log_missing_or_removed')
                    rows = raw[offset:]
                    files.append({'path': str(path), 'sha256': sha(path), 'before_offset': offset,
                                  'after_offset': offset, 'observed_length': len(raw), 'new_raw_logs': deepcopy(rows)})
                    for row in rows:
                        if isinstance(row, dict) and isinstance(row.get('request_id'), str):
                            grouped.setdefault(row['request_id'], []).append(row)
                        else:
                            errors.append('unaccounted_log_prefix_incomplete')
                except (OSError, ValueError, TypeError, AttributeError):
                    files.append({'path': str(path), 'sha256': None, 'before_offset': offset,
                                  'after_offset': offset, 'new_raw_logs': None})
                    errors.append('unaccounted_log_missing_or_unparseable')
            audits = {}
            for turn_id, rows in grouped.items():
                audit = paid_protocol().audit_model_log_prefix(rows, turn_id=turn_id,
                                                              configured_models=self.configured_models)
                # 严格前缀可能在身份有效的partial usage处停止；只保留可信部分下界。
                lower = audit.get('known_tokens_lower_bound')
                if audit['errors'] == ['actual_model_log_prefix_usage_invalid']:
                    for index, row in enumerate(rows):
                        current = paid_protocol().audit_model_log_prefix(rows[:index + 1], turn_id=turn_id,
                                                                       configured_models=self.configured_models)
                        if not current['errors']:
                            continue
                        if current['errors'] == ['actual_model_log_prefix_usage_invalid']:
                            partial = paid_protocol().usage_lower_bound(row.get('usage'))
                            prior = paid_protocol().audit_model_log_prefix(rows[:index], turn_id=turn_id,
                                                                          configured_models=self.configured_models)
                            if type(partial) is int and (index == 0 or not prior['errors']):
                                lower = (prior.get('known_tokens_lower_bound') or 0) + partial
                                audit['partial_known_tokens_lower_bound'] = partial
                        break  # 身份/顺序/usage失效之后不收后续日志。
                audits[turn_id] = audit
                self.merge_lower_bound(turn_id, lower)
            evidence = {'reason': reason, 'log_files': files, 'prefix_audits': audits,
                        'capture_errors': errors, 'actual_total_tokens': None, 'actual_requests_sent': None}
            if evidence not in self.failure_prefixes:
                self.failure_prefixes.append(evidence)

    def account_result(self, result):
        with self.accounting_lock:
            runtime, turn_id = result.get('agent_runtime'), result.get('bank_response_id')
            logs, metadata, reviews, evidence = self.capture_turn(runtime, turn_id)
            self.consume(runtime, logs=logs, turn_id=turn_id, metadata=metadata, reviews=reviews, evidence=evidence)

    def stop_paid(self, reason, *, unknown=False):
        self.latch_paid(reason, unknown=unknown)
        raise Stop('technical_timeout_or_budget_exceeded', reason, batch_stop=True)

    def raise_if_stopped(self):
        if self.batch_stop:
            raise Stop('technical_timeout_or_budget_exceeded', self.stop_reason or 'paid_attempt_stopped', batch_stop=True)

    def configure_clients(self, primary, reviewer):
        # 只有两者明确声明unpaid_fixture才免费；缺标记或混合不得降级真实paid。
        self.billing_mode = 'unpaid_fixture' if all(getattr(client, 'billing_mode', None) == 'unpaid_fixture'
                                                   for client in (primary, reviewer)) else 'paid'
        self.transports = {'primary': primary, 'reviewer': reviewer}
        if self.billing_mode == 'paid':
            self.configured_models = paid_protocol().configured_model_contract(ROOT)
            # Every admitted transport starts with an empty observed log. A
            # valid facts-only turn may never call the reviewer; its zero calls
            # must remain distinguishable from a removed or corrupt log later.
            for stage, client in self.transports.items():
                transport = client if stage == 'primary' else getattr(client, 'client', None)
                value = getattr(transport, 'log_path', None)
                if value is not None:
                    path = Path(value)
                    if path.is_symlink():
                        raise Stop('runner_error', 'actual_transport_log_redirected', batch_stop=True)
                    with path.open('x'):
                        pass

    def capture_turn(self, runtime, turn_id):
        calls = runtime.get('model_calls', []) if isinstance(runtime, dict) else []
        calls = calls if isinstance(calls, list) else []
        paths, batches, offsets = {}, {}, {}
        for stage, client in self.transports.items():
            transport = client if stage == 'primary' else getattr(client, 'client', None)
            value = getattr(transport, 'log_path', None) or os.environ.get('NAVREN_MODEL_CALL_LOG')
            if value is not None:
                paths[stage] = Path(value)
        files = []
        errors = []
        for path in dict.fromkeys(paths.values()):
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError('actual_log_missing')
                raw = paid_protocol().read_model_log_prefix(path)
                if not isinstance(raw, list):
                    raise ValueError('actual_log_missing')
                offset = self.log_offsets.get(str(path), 0)
                if len(raw) < offset:
                    raise ValueError('actual_log_prefix_removed')
                new = raw[offset:]
                for row in new:
                    if not isinstance(row, dict) or row.get('request_id') != turn_id:
                        errors.append('actual_log_contains_other_turn')
                batches[str(path)] = new
                offsets[str(path)] = len(raw)
                files.append({'path': str(path), 'sha256': sha(path), 'before_offset': offset, 'after_offset': len(raw),
                              'new_raw_logs': deepcopy(new)})
            except (OSError, ValueError, TypeError, AttributeError):
                errors.append('actual_log_missing_or_unparseable')
                files.append({'path': str(path), 'sha256': sha(path) if path.is_file() else None,
                              'before_offset': self.log_offsets.get(str(path), 0), 'after_offset': None,
                              'new_raw_logs': None, 'parse_status': 'missing_or_invalid'})
        queues = {stage: [row for row in batches.get(str(path), [])
                  if isinstance(row, dict) and row.get('stage') == ('conversation.tools' if stage == 'primary' else 'reviewer.conversation')]
                  for stage, path in paths.items()}
        logs = []
        for call in calls:
            stage = call.get('stage') if isinstance(call, dict) else None
            if stage not in queues or not queues[stage]:
                errors.append('actual_turn_log_count_missing')
            else:
                logs.append(queues[stage].pop(0))
        if any(queues.values()):
            errors.append('actual_turn_log_count_extra')
        if not calls:
            # HTTP/runtime缺失仍保存真实本attempt新增raw日志前缀，不能静默记0。
            logs = [row for rows in batches.values() for row in rows]
        metadata = {}
        for stage, client in self.transports.items():
            rows = getattr(client, 'response_metadata', None)
            offset = self.metadata_offsets.get(stage, 0)
            if not isinstance(rows, list) or len(rows) < offset:
                metadata[stage] = []; errors.append('actual_response_metadata_missing')
            else:
                metadata[stage] = deepcopy(rows[offset:]); self.metadata_offsets[stage] = len(rows)
        reviews = getattr(self.transports.get('reviewer'), 'records', None)
        if not isinstance(reviews, list) or len(reviews) < self.review_offset:
            new_reviews = []; errors.append('actual_reviewer_records_missing')
        else:
            new_reviews = deepcopy(reviews[self.review_offset:]); self.review_offset = len(reviews)
        self.log_offsets.update(offsets)
        return logs, metadata, new_reviews, {'log_files': files, 'capture_errors': errors}

    def start(self):
        if self.started is None:
            self.started = self.clock()

    def remaining(self):
        self.raise_if_stopped()
        self.start()
        values = {'seconds': self.remaining_seconds(),
                  'rounds': LIMITS['rounds'] - self.rounds,
                  'tools': LIMITS['tools'] - self.tools,
                  'tokens': LIMITS['tokens'] - self.tokens}
        if self.unknown_usage or any(v <= 0 for v in values.values()):
            raise Stop('technical_timeout_or_budget_exceeded',
                       'unknown_usage_stops_subsequent_paid_calls' if self.unknown_usage else 'attempt_budget_exhausted')
        return values

    def consume(self, runtime, *, logs=None, turn_id=None, metadata=None, reviews=None, evidence=None):
        # runtime 是正式 Agent 的实测返回，不能以工具脚本或客户端估算替代。
        if self.billing_mode == 'paid':
            return self.consume_paid(runtime, logs, turn_id, metadata, reviews, evidence)
        if not isinstance(runtime, dict):
            self.unknown_usage = True
            raise Stop('technical_timeout_or_budget_exceeded', 'runtime_usage_missing')
        for key in ('rounds', 'tool_calls', 'budget_tokens'):
            if type(runtime.get(key)) is not int or runtime[key] < 0:
                self.unknown_usage = True
                raise Stop('technical_timeout_or_budget_exceeded', 'runtime_counter_invalid:' + key)
        self.rounds += runtime['rounds']
        self.tools += runtime['tool_calls']
        self.tokens += runtime['budget_tokens']
        total = runtime.get('total_tokens')
        if runtime.get('usage_complete') is not True or type(total) is not int or total < 0:
            self.unknown_usage = True
        else:
            self.provider_tokens += total
        if self.rounds > 8 or self.tools > 12 or self.tokens > 100000:
            raise Stop('technical_timeout_or_budget_exceeded', 'runtime_actual_budget_overrun')

    def consume_paid(self, runtime, logs, turn_id, metadata, reviews, evidence):
        observed = {'turn_id': turn_id, 'runtime': deepcopy(runtime), 'raw_logs': deepcopy(logs),
                    'response_metadata': deepcopy(metadata), 'review_records': deepcopy(reviews),
                    'transport_evidence': deepcopy(evidence), 'billing_mode': 'paid', 'provider_cost': None}
        try:
            digest = hashlib.sha256(canonical(observed).encode()).hexdigest()
        except (TypeError, ValueError):
            self.observations.append({'turn_id': turn_id, 'raw_accounting_unserializable': True,
                                      'transport_evidence': deepcopy(evidence), 'provider_cost': None})
            self.stop_paid('paid_accounting_payload_invalid', unknown=True)
        if turn_id in self.seen_turns:
            if self.seen_turns[turn_id] == digest:
                return  # 同一HTTP结果的finalize/return观察只计一次，包括已知失败。
            self.stop_paid('duplicate_turn_accounting_conflict', unknown=True)
        # 已发HTTP晚到finalize仍保存真实费用；新调用由transport闩拦截，末尾不能解闩。
        self.seen_turns[turn_id] = digest
        self.observations.append(observed)
        before_tokens = self.tokens
        admitted = LIMITS['tokens'] - before_tokens
        audit = paid_protocol().paid_usage_audit(runtime, logs, turn_id, budget_limit=admitted)
        identity = paid_protocol().audit_response_identity(runtime, logs, metadata, turn_id=turn_id,
                                                           configured_models=self.configured_models)
        errors = list(audit['errors']) + list(identity['errors'])
        prefix = paid_protocol().audit_model_log_prefix(logs, turn_id=turn_id, configured_models=self.configured_models)
        if evidence:
            errors += evidence.get('capture_errors', [])
        calls = runtime.get('model_calls', []) if isinstance(runtime, dict) else []
        calls = calls if isinstance(calls, list) else []
        review_calls = [call for call in calls if isinstance(call, dict) and call.get('stage') == 'reviewer'
                       and call.get('outcome') == 'completed']
        if not isinstance(reviews, list) or len(reviews) != len(review_calls) or any(
                not isinstance(review, dict) or not isinstance(review.get('decision'), dict) or
                review['decision'].get('_model_usage') != call.get('usage') for review, call in zip(reviews or [], review_calls)):
            errors.append('actual_review_usage_observation_mismatch')
        if isinstance(runtime, dict):
            for key, field_name in (('rounds', 'rounds'), ('tool_calls', 'tools'), ('budget_tokens', 'tokens')):
                value = runtime.get(key)
                if type(value) is int and value >= 0:
                    setattr(self, field_name, getattr(self, field_name) + value)
                else:
                    errors.append('runtime_counter_invalid:' + key)
        actual = audit['actual_total_tokens']
        # 已知失败也消耗真实回执；先计一次，再锁住后续全部paid。
        if type(actual) is int:
            self.provider_tokens += actual
        else:
            self.unknown_usage = True
        lower = audit['known_tokens_lower_bound']
        prefix_lower = prefix.get('known_tokens_lower_bound')
        known_lower = max(v for v in (lower, prefix_lower, 0) if type(v) is int and v >= 0)
        self.merge_lower_bound(turn_id, known_lower)
        requests = audit['actual_model_requests_sent']
        if type(requests) is int:
            self.known_requests_sent_lower_bound += requests
        else:
            self.request_count_unknown = True
        if any(code in errors for code in ('unknown_usage', 'actual_model_log_usage_mismatch',
                'actual_review_usage_observation_mismatch')) or identity['errors'] or evidence and evidence.get('capture_errors'):
            self.unknown_usage = True
        if self.rounds > 8 or self.tools > 12 or self.tokens > 100000:
            errors.append('runtime_actual_budget_overrun')
        if isinstance(runtime, dict) and (runtime.get('failure') or runtime.get('budget_limit')):
            errors.append('runtime_critical_failure_or_budget_limit')
        observed.update(audit=audit, response_identity_audit=identity, raw_log_prefix_audit=prefix,
                        errors=list(dict.fromkeys(errors)),
                        admitted_remaining_tokens=admitted)
        if errors:
            self.stop_paid('paid_accounting_or_model_failure:' + ','.join(dict.fromkeys(errors)))
        self.raise_if_stopped()
        self.check_time()

    def check_time(self):
        if self.started is not None and (self.clock() - self.started >= 105 or
                self.outer_deadline is not None and self.clock() >= self.outer_deadline):
            raise Stop('technical_timeout_or_budget_exceeded', 'attempt_deadline_exceeded')

    def remaining_seconds(self):
        self.start()
        self.check_time()
        own = 105 - (self.clock() - self.started)
        return min(own, self.outer_deadline - self.clock()) if self.outer_deadline is not None else own

    def report(self):
        return {'elapsed_seconds': 0 if self.started is None else self.clock() - self.started,
                'rounds': self.rounds, 'tools': self.tools, 'budget_tokens': self.tokens,
                'provider_total_tokens': None if self.unknown_usage else self.provider_tokens,
                'usage_complete': not self.unknown_usage, 'limits': LIMITS, 'billing_mode': self.billing_mode,
                'provider_usage_scope': 'actual_paid_calls_once' if self.billing_mode == 'paid' else 'unpaid_fixture_counters_only',
                'actual_provider_total_tokens': None if self.billing_mode != 'paid' or self.unknown_usage else self.provider_tokens,
                'known_tokens_lower_bound': self.known_tokens_lower_bound if self.billing_mode == 'paid' else None,
                'actual_requests_sent': None if self.billing_mode != 'paid' or self.request_count_unknown else self.known_requests_sent_lower_bound,
                'known_requests_sent_lower_bound': self.known_requests_sent_lower_bound if self.billing_mode == 'paid' else None,
                'batch_stop': self.batch_stop, 'stop_reason': self.stop_reason,
                'paid_turn_observations': self.observations, 'unaccounted_failure_prefixes': self.failure_prefixes,
                'actual_loaded_agent_source': self.loaded_source_binding, 'provider_cost': None}


def guarded_paid_transport(client, budget):
    class GuardedTransport:
        def __getattr__(self, name):
            return getattr(client, name)
        def invoke(self, method, *args, **kwargs):
            with budget.accounting_lock:
                budget.raise_if_stopped()
                try:
                    budget.check_time()
                except Stop as error:
                    budget.latch_paid(error.reason)
                    raise
                if budget.admission_closed:
                    raise Stop('technical_timeout_or_budget_exceeded', 'paid_admission_closed', batch_stop=True)
            # 不持锁等待HTTP；异常线程须能立即闩住下一次调用。
            return getattr(client, method)(*args, **kwargs)
        def complete(self, *args, **kwargs):
            return self.invoke('complete', *args, **kwargs)
        def review(self, *args, **kwargs):
            return self.invoke('review', *args, **kwargs)
    return GuardedTransport()


def budgeted_agent(primary, reviewer, budget):
    budget.configure_clients(primary, reviewer)
    # Parent evidence is read before this point. Never let its imports change
    # the actual business implementation under a current-source manifest.
    frozen_path = ROOT / 'outputs/window-progress/integration/baseline-mainflow-config/source-manifest-final.json'
    frozen = json.loads(frozen_path.read_text()) if frozen_path.is_file() else None
    if budget.billing_mode == 'paid' and frozen is None:
        raise Stop('not_run', 'paid_runtime_source_manifest_missing', batch_stop=True)
    loaded = {}
    for name in ('agent_runtime.conversation', 'agent_runtime.complex_agent',
                 'agent_runtime.bank_tools', 'agent_runtime.tool_routing',
                 'agent_runtime.data_boundary', 'agent_runtime.native_client',
                 'agent_runtime.model_adapters', 'agent_runtime.metric_claims',
                 'agent_runtime.review_evidence', 'agent_runtime.provider_review_carrier',
                 'agent_runtime.read_completion', 'agent_runtime.payment_completion', 'base_runtime.reply_presentation',
                 'base_runtime.tool_scope'):
        module = importlib.import_module(name)
        relative = name.replace('.', '/') + '.py'
        actual = Path(module.__file__).resolve()
        if (actual != ROOT / relative or (frozen is not None and sha(actual) != frozen.get(relative))):
            raise Stop('fixture_drift', 'actual_loaded_agent_source_mismatch:' + name, batch_stop=True)
        loaded[name] = {'path': str(actual), 'sha256': sha(actual)}
    budget.loaded_source_binding = {'source_root': str(ROOT), 'modules': loaded,
        'checked_before_model_request': True, 'provider_calls_before_check': 0}
    from agent_runtime.complex_agent import ComplexConversationAgent
    class SharedBudgetAgent(ComplexConversationAgent):
        def process(self, gateway, workspace, text, **options):
            remaining = budget.remaining()
            self.max_rounds, self.max_tools = remaining['rounds'], remaining['tools']
            self.max_tokens, self.timeout_seconds = remaining['tokens'], remaining['seconds']
            finalize = options.get('finalize_result')
            counted = False
            def account(result):
                nonlocal counted
                if not counted:
                    counted = True
                    if budget.billing_mode == 'paid':
                        budget.account_result(result)
                    else:
                        budget.consume(result.get('agent_runtime'))
            if finalize:
                def guarded_finalize(result, *args, **kwargs):
                    account(result)
                    budget.check_time()
                    return finalize(result, *args, **kwargs)
                options['finalize_result'] = guarded_finalize
            try:
                result = super().process(gateway, workspace, text, **options)
                account(result)
                return result
            except Exception as error:
                if budget.billing_mode == 'paid' and (not counted or not isinstance(error, Stop)):
                    budget.preserve_unaccounted_failure('agent_exception:' + type(error).__name__)
                elif not counted:
                    budget.unknown_usage = True
                raise
    agent = SharedBudgetAgent(primary=primary, reviewer=reviewer,
                              route_provider='off', tool_routing='progressive')
    if budget.billing_mode == 'paid':
        agent.primary, agent.reviewer = (guarded_paid_transport(client, budget)
                                        for client in (agent.primary, agent.reviewer))
    return agent


class Browser:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        self.workspace = None

    def call(self, path, method='GET', body=None, timeout=10):
        headers = {'Content-Type': 'application/json', 'Origin': self.base}
        if self.csrf:
            headers['X-QZ-CSRF'] = self.csrf
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                    data=None if body is None else canonical(body).encode())
        try:
            with self.opener.open(request, timeout=max(.05, timeout)) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def start(self, timeout=10):
        browser_bootstrap(self.opener,self.base)
        status, result = self.call('/api/finance/session', 'POST', {}, timeout=timeout)
        if status != 200:
            raise Stop('fixture_error', 'http_session_creation_failed', details=result)
        self.csrf, self.workspace = result['csrf'], result['workspace_id']
        browser_consume(self.opener,self.base,result)
        return result

    def select(self, variant):
        status,current=self.call('/api/finance/session','POST',{})
        if status!=200:raise Stop('fixture_error','registered_session_refresh_failed')
        status, result = self.call('/api/finance/demo/select', 'POST',
                          {'user_id': variant, 'expected_workspace_id': self.workspace,'expected_privacy_revision':current['privacy_revision']})
        if status != 200:
            raise Stop('fixture_error', 'http_profile_selection_failed', details=result)
        self.csrf, self.workspace = result['csrf'], result['workspace_id']
        browser_consume(self.opener,self.base,result)


@contextmanager
def local_network_only(allow_cloud=False):
    original_dns, original_connect = socket.getaddrinfo, socket.socket.connect
    allowed = {'127.0.0.1', 'localhost', '::1'}
    if allow_cloud:
        # 冷补充、基金抓取、语音等仍关闭；只准已交接的正式主模型和审核地址。
        from urllib.parse import urlsplit
        from agent_runtime.model_adapters import AIPING_URL, DEEPSEEK_URL
        allowed.update(urlsplit(url).hostname for url in (AIPING_URL, DEEPSEEK_URL))
    addresses = set()
    def dns(host, *args, **kwargs):
        if host not in allowed:
            raise Stop('runner_error', 'unapproved_network_destination')
        result = original_dns(host, *args, **kwargs)
        addresses.update(row[4][0] for row in result)
        return result
    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in allowed | addresses:
            raise Stop('runner_error', 'unapproved_network_connection')
        return original_connect(sock, address)
    with patch('socket.getaddrinfo', side_effect=dns), patch.object(socket.socket, 'connect', connect):
        yield


def raw_snapshot(bank):
    with closing(bank.connect()) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {table: sorted([dict(row) for row in db.execute('SELECT * FROM "' + table + '"')], key=canonical)
                for table in tables}
    personal = getattr(bank, 'personal_store', None)
    if personal is not None:
        with closing(personal.connect()) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for table in tables:
                rows = sorted([dict(row) for row in db.execute('SELECT * FROM "' + table + '"')], key=canonical)
                result['personal/' + table] = rows
                if table in ('user_memories', 'profile_observations'):
                    result[table] = rows
    return result


def scoped(raw, table, workspace):
    return [r for r in raw.get(table, []) if r.get('workspace_id', r.get('workspace', r.get('id') if table == 'workspaces' else None)) == workspace]


def one(rows, predicate, name):
    found = [r for r in rows if predicate(r)]
    if len(found) != 1:
        raise Stop('fixture_error', 'fixture_object_not_unique:' + name, details={'count': len(found)})
    return found[0]


class HandlerLifecycle:
    """仅登记本server实例实际接受的请求，不猜全局线程或监听线程退出。"""
    def __init__(self, server):
        self.server, self.records, self.by_request = server, [], {}
        self.condition = threading.Condition()
        self.admission_closed = False
        original_admit, original_handle = server.process_request, server.process_request_thread
        owner = self
        def admit(instance, request, address):
            with owner.condition:
                if owner.admission_closed:
                    instance.shutdown_request(request)
                    return
                record = {'marker': len(owner.records) + 1, 'thread': None, 'started': None,
                          'finished': None, 'done': False, 'admission_error': None}
                owner.records.append(record); owner.by_request[id(request)] = record
            try:
                return original_admit(request, address)
            except Exception as error:
                with owner.condition:
                    record.update(done=True, finished=time.monotonic(), admission_error=type(error).__name__)
                    owner.condition.notify_all()
                raise
        def handle(instance, request, address):
            with owner.condition:
                record = owner.by_request[id(request)]
                record.update(thread=threading.current_thread(), started=time.monotonic())
            try:
                return original_handle(request, address)
            finally:
                with owner.condition:
                    record.update(done=True, finished=time.monotonic())
                    owner.condition.notify_all()
        server.process_request = types.MethodType(admit, server)
        server.process_request_thread = types.MethodType(handle, server)

    def close_admission(self):
        with self.condition:
            self.admission_closed = True

    def wait(self, deadline):
        with self.condition:
            while any(not row['done'] for row in self.records):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            records = list(self.records)
        for row in records:
            thread = row['thread']
            if thread is not None:
                thread.join(max(0, deadline - time.monotonic()))
        return self.snapshot()

    def snapshot(self):
        with self.condition:
            rows = [{'marker': row['marker'], 'thread_ident': None if row['thread'] is None else row['thread'].ident,
                     'started': row['started'], 'finished': row['finished'], 'done': row['done'],
                     'alive': row['thread'] is not None and row['thread'].is_alive(),
                     'admission_error': row['admission_error']} for row in self.records]
        return {'admission_closed': self.admission_closed, 'admitted_count': len(rows), 'handlers': rows,
                'all_admitted_done': all(row['done'] and not row['alive'] for row in rows)}


class Fixture:
    """当前正常服务 + 可信导入；封存闭包另生成独立初始原始行用于比对。"""
    def __init__(self, out, contract, variant=None, primary=None, reviewer=None, history_prelude=False):
        self.construction_started, self.precheck_finished, self.closed = time.monotonic(), None, False
        self.out, self.contract, self.variant = Path(out), contract, variant
        self.case_id, self.task_steps = None, None
        self.stack = ExitStack()
        self.server = self.thread = None
        self.budget = AttemptBudget(billing_mode='unpaid_fixture' if primary is None else 'paid')
        self.bound = {}
        self.receipts = {}
        self.original_operations = {}
        self.tool_records = []
        self.review_records = []
        self.harness_workspace_baselines = {}
        self.history_prelude = history_prelude
        self.out.mkdir(parents=True, exist_ok=False)
        self.temp_directory = tempfile.TemporaryDirectory(prefix='navren-formal-')
        self.tmp = self.stack.enter_context(self.temp_directory)
        self.handlers, self.cleanup_report = None, None
        self.temp = Path(self.tmp)
        private_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.stack.enter_context(patch.dict(os.environ, {
            'NAVREN_SIM_DB': str(self.temp / 'bank.sqlite3'),
            'QINGZHOU_SIM_DB': str(self.temp / 'bank.sqlite3'),
            'NAVREN_KNOWLEDGE_DB': str(self.temp / 'knowledge.sqlite3'),
            'NAVREN_FUND_CACHE': str(self.temp / 'funds'),
            'NAVREN_PERSONAL_DATA_KEY': private_key,
            'NAVREN_MODEL_CALL_LOG': str(self.out / 'model-calls.jsonl')}))
        try:
            self._build(primary, reviewer)
            self.precheck_finished = time.monotonic()
        except Exception as error:
            save(self.out / 'fixture-error.json', {'classification': getattr(error, 'classification', 'runner_error'),
                'reason': getattr(error, 'reason', type(error).__name__), 'details': getattr(error, 'details', None),
                'message': str(error)})
            if hasattr(self, 'g'):
                save(self.out / 'failed-fixture-raw.json', raw_snapshot(self.g.bank))
            self.close()
            raise

    def _build(self, primary, reviewer):
        from server import create_server, ProxyHandler
        from voice_gateway import VoiceGateway
        from local_knowledge.runtime import PreparedEvidence, RoundProvider
        from agent_runtime.agent_factory import create_agent
        from agent_runtime.bank_tools import BankTools
        import fund_history
        owner = self
        class ObservedTools(BankTools):
            def invoke(self, name, args):
                record = {'request_id': self.request_id, 'tool': name, 'arguments': deepcopy(args)}
                owner.tool_records.append(record)
                try:
                    result = super().invoke(name, args)
                    record.update(status='completed', output=deepcopy(result))
                    return result
                except Exception as error:
                    record.update(status='failed', error_code=getattr(error, 'code', type(error).__name__),
                                  error_http_status=getattr(error, 'status', None))
                    raise
        self.stack.enter_context(patch('agent_runtime.conversation.BankTools', ObservedTools))
        if reviewer:
            class ObservedReviewer:
                def __getattr__(self, name):
                    return getattr(reviewer, name)
                def review(self, packet, *args, **kwargs):
                    record = {'packet': deepcopy(packet)}
                    owner.review_records.append(record)
                    result = reviewer.review(packet, *args, **kwargs)
                    decision = deepcopy(result)
                    wire = decision.get('_agent_review_wire')
                    if isinstance(wire, bytes):
                        decision['_agent_review_wire'] = {'request_bytes': len(wire),
                            'request_sha256': hashlib.sha256(wire).hexdigest(),
                            'representation': 'wire_metadata_only'}
                    record['decision'] = decision
                    return result
            actual_reviewer = ObservedReviewer()
        else:
            actual_reviewer = None
        self.stack.enter_context(patch.object(fund_history, 'CACHE', self.temp / 'funds'))
        # cached 的默认参数在 import 时已绑定，实例替身仅固定空私有cache，绝不读现役cache。
        self.stack.enter_context(patch('wealth_portfolio.cached', lambda code: fund_history.cached(code, self.temp / 'funds')))
        self.stack.enter_context(patch.object(ProxyHandler, 'log_message', lambda *args: None))
        class NoCloud:
            model_id, billing_mode = 'formal-preflight-cloud-disabled', 'unpaid_fixture'
            def complete(self, *args, **kwargs):
                raise Stop('runner_error', 'preflight_must_not_call_model')
        agent = budgeted_agent(primary, actual_reviewer, self.budget) if primary else create_agent(primary=NoCloud(), route_provider='off')
        # 仅隔离 formal Fixture 显式注入现有本地短信替身；生产默认渠道保持未配置。
        from base_runtime.service_assembly import AssembledGateway
        spec = importlib.util.spec_from_file_location('_formal_local_sms', ROOT / 'tests/test_sms_permissions.py')
        local_sms_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local_sms_module)
        self.local_sms = local_sms_module.LocalSms(lambda: self.g.bank.clock())
        self.local_sms_code = local_sms_module.CODE
        self.sms_clock_offset = 0.0
        self.registered_template = (build_profile_template(ROOT,self.temp,self.variant,self.contract) if self.variant else
            build_template(ROOT,self.temp,self.contract['fixture_contract']['new_closed_seed_v1']))
        with template_factory(self.registered_template) as registered_factory:
            def fixture_finance(database=None, **options):
                return registered_factory(database, sms_provider=self.local_sms,
                                        allow_test_sms_provider=True, **options)
            self.server = create_server(port=0, database=str(self.temp / 'bank.sqlite3'),
                              voice=VoiceGateway(key='', allow_cloud_asr=False), conversation_agent=agent,
                              finance_factory=fixture_finance)
        self.registered_host=attach_server(self.server,self.registered_template,self.variant or 'tycoon')
        self.handlers = HandlerLifecycle(self.server)  # 启动监听前登记admission和真实请求线程。
        self.server.evidence_runtime.close()
        provider = self.server.finance.knowledge_provider
        class ClosedRuntime:
            def prepare_round(self, text):
                return PreparedEvidence(RoundProvider(provider), {'mode': 'formal_no_public_catalog',
                        'cold_fetch_enabled': False, 'semantic_validation': False})
            def close(self):
                pass
        self.server.evidence_runtime = ClosedRuntime()
        self.g = self.server.finance
        initial_clock = time.monotonic()
        epoch = self.contract['fixture_contract']['new_closed_seed_v1']['clock_epoch']
        self.g.bank.clock = lambda: epoch + (time.monotonic() - initial_clock) + self.sms_clock_offset
        if not self.variant:
            core = self.contract['fixture_contract']['new_closed_seed_v1']
            data = json.loads((ROOT / core['core_path']).read_text())
            self.g.bank.fixture = data
            self.g.bank.seed_hash = hashlib.sha256(canonical(data).encode()).hexdigest()
            with self.g.bank.transaction() as db:
                if db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0] or self.g.demo_accounts.catalog()['initialized_count']:
                    raise Stop('fixture_error', 'new_core_requires_empty_database')
                db.execute('DELETE FROM products')
                for product in data['products']:
                    db.execute('INSERT INTO products(id,data) VALUES(?,?)', (product['id'], canonical(product)))
                db.execute("UPDATE metadata SET value=? WHERE key='seed_hash'", (self.g.bank.seed_hash,))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.browser = Browser('http://127.0.0.1:' + str(self.server.server_port))
        if self.variant:
            self.browser.start()
        else:
            with archived_generation(ROOT, self.contract['fixture_contract']['new_closed_seed_v1']):
                self.browser.start()
        if self.variant:
            self.browser.select(self.variant)
        self.workspace = self.browser.workspace
        self.stack.enter_context(request_context(self.g,self.workspace))
        self.initial = raw_snapshot(self.g.bank)
        self.initial_version = one(scoped(self.initial, 'workspaces', self.workspace), lambda r: True, 'workspace')['version']
        if not self.variant:
            self._core_bindings()
            self.task_steps = TaskStepOracle(self.initial, self.workspace, self.bound['birthday_task'], load_task_steps_supplement())
            self._verify_seed_closure()
        self.observe_initial()
        save(self.out / 'fixture-baseline.json', self.initial)
        if self.history_prelude:
            self._history_setup()
        save(self.out / 'fixture-attestation.json', {'variant': self.variant, 'isolation': 'system TemporaryDirectory',
            'source': 'controlled_synthetic_fixture', 'live_customer_data': False, 'public_catalog_provided': False,
            'all_database_and_cache_paths_private': True, 'seed_closed_rows_checked': not bool(self.variant),
            'normal_http_session': True, 'cloud_calls': 0})

    def _history_setup(self):
        if sha(SETUP) != SETUP_SHA:
            raise Stop('fixture_drift', 'history_setup_sha_changed', batch_stop=True)
        contract = json.loads(SETUP.read_text())
        before = raw_snapshot(self.g.bank)
        save(self.out / 'history-setup-raw-before.json', before)
        for table, rows in before.items():
            empty_only = (table.split('/')[-1] in ('user_memories', 'profile_observations', 'user_memory_relations',
                'memory_candidates', 'conversation_sources', 'agent_dialogues', 'agent_widget_history', 'guarded_turn_saves')
                or table.startswith('graph_') or 'decisions' in table)
            if empty_only and rows:
                raise Stop('fixture_error', 'history_prelude_requires_empty_history_candidates_graph_decisions:' + table)
        started = time.monotonic()
        revision = self.g.personal_data.revision(self.workspace)
        payload = {**contract['request']['payload'], 'expected_revision': revision}
        status, response = self.browser.call(contract['request']['path'], contract['request']['method'], payload)
        after = raw_snapshot(self.g.bank)
        save(self.out / 'history-setup-raw-after.json', after)
        changed = {table: {'before': before.get(table, []), 'after': after.get(table, [])}
                   for table in set(before) | set(after) if before.get(table, []) != after.get(table, [])}
        save(self.out / 'history-setup-diff.json', changed)
        allowed = set(contract['allowed_setup_effects']['bank_tables']) | {
                   'personal/' + table for table in contract['allowed_setup_effects']['private_tables']}
        if status != 200 or response.get('enabled') is not True or self.browser.workspace != self.workspace:
            raise Stop('fixture_error', 'normal_history_settings_http_failed', details=response)
        if set(changed) - allowed:
            raise Stop('fixture_error', 'history_prelude_undeclared_raw_table_change', details=sorted(set(changed) - allowed))
        fields = {'personal/user_memory_settings': {'workspace_id', 'enabled', 'updated_at'},
                  'personal/personal_versions': {'workspace_id', 'revision', 'pending_invalidation'},
                  'personal/personal_audit': {'id', 'workspace_id', 'event', 'object_id', 'revision', 'created_at'},
                  'personal_data_versions': {'workspace_id', 'revision'},
                  'conversation_privacy_versions': {'workspace_id', 'revision'}}
        for table in changed:
            for row in after.get(table, []):
                if set(row) != fields[table] or row['workspace_id'] != self.workspace:
                    raise Stop('fixture_error', 'history_prelude_undeclared_field_or_workspace_change:' + table)
        setting = one(scoped(after, 'personal/user_memory_settings', self.workspace), lambda r: True, 'history_setting')
        private_version = one(scoped(after, 'personal/personal_versions', self.workspace), lambda r: True, 'personal_version')
        if setting['enabled'] != 1 or private_version['revision'] != revision + 1 or private_version['pending_invalidation'] != 0:
            raise Stop('fixture_error', 'history_prelude_permission_version_mismatch')
        if self.g.personal_data.read_history(self.workspace) or self.g.memories.state(self.workspace)['items']:
            raise Stop('fixture_error', 'history_prelude_must_not_create_history_or_memories')
        self.initial = after
        save(self.out / 'post-history-setup-baseline.json', after)
        self.observe_initial()
        save(self.out / 'history-setup-result.json', {'status': 'verified', 'actor': contract['actor'],
            'setup_sha256': SETUP_SHA, 'normal_http_settings': True, 'elapsed_seconds': time.monotonic() - started,
            'model_or_tool_credit': 0, 'results_label': contract['results_label'],
            'candidate_memory_confirmed_by_harness': False, 'original_seed_prechecked': True})

    def _core_bindings(self):
        raw, workspace = self.initial, self.workspace
        rules = {'main': ('accounts', lambda r: r['id'].endswith('-main') and r['name'] == '正式验收模拟日常账户'),
                 'savings': ('accounts', lambda r: r['id'].endswith('-savings')),
                 'friend': ('beneficiaries', lambda r: r['alias'] == '同学小张' and r['verified'] == 1 and r['status'] == 'active'),
                 'debit1001': ('cards', lambda r: r['type'] == 'debit' and r['last_four'] == '1001' and r['status'] == 'active'),
                 'video_subscription': ('subscriptions', lambda r: r['plan_name'] == '模拟视频会员' and r['amount_minor'] == 2700),
                 'birthday_task': ('orchestration_tasks', lambda r: r['title'] == '妈妈生日礼物' and r['budget_minor'] == 50000 and r['due_on'] == '2026-10-18'),
                 'flowers_quote': ('service_quotes', lambda r: r['price_minor'] == 19900 and r['currency'] == 'CNY' and r['status'] == 'illustrative'),
                 'cake_quote': ('service_quotes', lambda r: r['price_minor'] == 25900 and r['currency'] == 'CNY' and r['status'] == 'illustrative')}
        self.objects = {}
        for key, (table, predicate) in rules.items():
            row = one(scoped(raw, table, workspace), predicate, key)
            self.bound[key], self.objects[key] = row['id'], row
        subscription = self.objects['video_subscription']
        mandate = one(scoped(raw, 'mandates', workspace), lambda r: r['id'] == subscription['mandate_id']
                       and r['merchant'] == '模拟视频会员' and r['amount_minor'] == 2700, 'video_mandate')
        self.bound['video_mandate'], self.objects['video_mandate'] = mandate['id'], mandate
        for kind in ('flowers', 'cake'):
            quote = self.objects[kind + '_quote']
            one(scoped(raw, 'service_providers', workspace), lambda r: r['id'] == quote['provider_id']
                and r['service_type'] == kind, kind + '_provider')

    def _verify_seed_closure(self):
        core = self.contract['fixture_contract']['new_closed_seed_v1']
        package_name = '_navren_formal_closed_seed_' + secrets.token_hex(8)
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT / core['generation_sources_archive'] / 'simulated_bank')]
        sys.modules[package_name] = package
        try:
            module = importlib.import_module(package_name + '.service')
            reference = module.SimulatedBank(str(self.temp / 'closed-reference.sqlite3'),
                  fixture=ROOT / core['core_path'], clock=lambda: core['clock_epoch'])
            reference_workspace = reference.create_workspace()['workspace_id']
            expected = raw_snapshot(reference)
            def normalize(rows, workspace, table):
                normalized = []
                for row in rows:
                    constructor_times = {'schema_migrations': {'applied_at'}, 'workspace_datasets': {'populated_at'}}
                    row = {k: v for k, v in row.items() if k not in SECRET_KEYS
                           and k not in {'created_at'} | constructor_times.get(table, set())}
                    normalized.append(json.loads(canonical(row).replace(workspace, '$workspace').replace(workspace[:12], '$prefix')))
                return sorted(normalized, key=canonical)
            for table, rows in expected.items():
                if table in ('metadata', 'audit'):
                    continue
                target = self.initial.get(table, [])
                # 原closure仍核同一参考generator；注册额外租户不混进本题。
                if table=='workspaces' or any('workspace_id' in row or 'workspace' in row for row in rows):
                    target=scoped(self.initial,table,self.workspace)
                if normalize(rows, reference_workspace, table) != normalize(target, self.workspace, table):
                    raise Stop('fixture_error', 'closed_seed_raw_rows_mismatch:' + table,
                      details={'expected': normalize(rows, reference_workspace, table), 'actual': normalize(target, self.workspace, table)})
            save(self.out / 'closed-seed-baseline.json', expected)
        finally:
            for name in list(sys.modules):
                if name == package_name or name.startswith(package_name + '.'):
                    sys.modules.pop(name, None)

    def observe_initial(self):
        failures = []
        if self.g.memories.state(self.workspace)['enabled'] is not False or self.g.personal_data.read_history(self.workspace):
            failures.append('original_seed_requires_memory_off_and_empty_normal_history')
        for table in ('user_memories', 'user_memory_relations', 'profile_observations', 'memory_candidates',
                      'conversation_sources', 'agent_dialogues', 'agent_widget_history', 'guarded_turn_saves'):
            if scoped(self.initial, 'personal/' + table, self.workspace):
                failures.append('original_seed_requires_empty_private_history_or_memory:' + table)
        if self.variant:
            case = next(c for c in self.contract['cases'] if c['id'] == 'FB10')
            expected = case['frozen_profile_expectations'][self.variant]
            failures += self.profile_checks(self.initial, expected)
        else:
            expected = self.contract['fixture_contract']['new_closed_seed_v1']['initial_state']
            failures += self.state_checks(self.initial, expected)
            transactions = scoped(self.initial, 'transactions', self.workspace)
            if len(transactions) != 7 or self.initial_version != 1:
                failures.append('baseline_version_or_seven_ledger_rows')
            if sum(r['amount_minor'] for r in transactions if r['kind'] == 'income') != 750000 or sum(r['amount_minor'] for r in transactions if r['kind'] == 'expense') != 266600:
                failures.append('independent_september_bill_totals')
            frozen_bill = next(c for c in self.contract['cases'] if c['id'] == 'FB02')['steps'][0]['grading_only']['frozen_facts']
            bill = {'transaction_count': len(transactions),
                'income_minor': sum(r['amount_minor'] for r in transactions if r['kind'] == 'income'),
                'expense_minor': sum(r['amount_minor'] for r in transactions if r['kind'] == 'expense'),
                'income_expense_surplus_minor': sum(r['delta_minor'] for r in transactions),
                'cash_delta_minor': sum(r['delta_minor'] for r in transactions),
                'category_expense_minor': {category: sum(r['amount_minor'] for r in transactions
                    if r['kind'] == 'expense' and r['category'] == category)
                    for category in {r['category'] for r in transactions if r['kind'] == 'expense'}}}
            if canonical(bill) != canonical(frozen_bill) or any(not r['occurred_at'].startswith('2026-09-') for r in transactions):
                failures.append('independent_september_bill_objects_period_categories')
        if failures:
            raise Stop('fixture_error', 'frozen_baseline_mismatch', details=failures)

    def resolve(self, value):
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, str) and value.startswith('$'):
            name, *field = value[1:].split('.')
            if field:
                return self.objects[name][field[0]]
            if name not in self.bound:
                raise Stop('necessary_condition_failure', 'required_real_binding_missing:' + name)
            return self.bound[name]
        return value

    def close(self, final_state_path=None):
        if self.closed:
            return
        self.closed = True
        started = time.monotonic()
        deadline = started + CLEANUP_SECONDS
        with self.budget.accounting_lock:
            self.budget.admission_closed = True
        try:
            self.budget.check_time()
        except Stop as error:
            self.budget.latch_paid(error.reason)
        if self.handlers:
            self.handlers.close_admission()
        if self.server and self.thread:
            self.server.shutdown()
            self.thread.join(max(0, deadline - time.monotonic()))
        handler_state = self.handlers.wait(deadline) if self.handlers else None
        done = (handler_state is None or handler_state['all_admitted_done']) and not bool(self.thread and self.thread.is_alive())
        within_cap = time.monotonic() <= deadline
        self.cleanup_report = {'cleanup_cap_seconds': CLEANUP_SECONDS,
            'cleanup_seconds': time.monotonic() - started, 'handler_state': handler_state,
            'listening_thread_exited': not bool(self.thread and self.thread.is_alive()),
            'cleanup_completed': done and within_cap, 'retained_state': not (done and within_cap),
            'private_temp_path': self.tmp, 'private_temp_exists': Path(self.tmp).exists()}
        if not done or not within_cap:
            self.budget.preserve_unaccounted_failure('fixture_cleanup_cap_or_handler_not_exited')
            # 活动handler仍引用本fixture，不拆库/恢复patch/移除temp；外层硬上限负责终结。
            self.temp_directory._finalizer.detach()
            _RETAINED_FIXTURES.append(self)
            save(self.out / 'fixture-cleanup.json', self.cleanup_report)
            raise Stop('technical_timeout_or_budget_exceeded', 'fixture_cleanup_cap_or_handler_not_exited', batch_stop=True)
        try:
            if final_state_path is not None:
                save(final_state_path, raw_snapshot(self.g.bank))
        finally:
            try:
                if self.server:
                    self.server.server_close()
                    self.server = None
            finally:
                self.stack.close()
                finished = time.monotonic()
                self.cleanup_report.update(private_temp_exists=Path(self.tmp).exists(),
                    cleanup_seconds=finished - started, cleanup_completed=finished <= deadline,
                    retained_state=False, all_handler_state_before_teardown=handler_state)
                if finished > deadline:
                    self.budget.latch_paid('fixture_complete_cleanup_cap_exceeded')
                try:
                    self.budget.check_time()  # 原105/outer包含请求等待、finalraw及完整teardown。
                except Stop as error:
                    self.budget.latch_paid(error.reason)
        save(self.out / 'fixture-cleanup.json', self.cleanup_report)
        save(self.out / 'fixture-timing.json', {'variant': self.variant,
            'status': 'verified' if self.precheck_finished is not None else 'failed',
            'construction_precheck_seconds': None if self.precheck_finished is None else self.precheck_finished - self.construction_started,
            'teardown_seconds': time.monotonic() - started, 'history_prelude': self.history_prelude,
            'excludes_model_and_owner_harness_elapsed': True})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def state_checks(self, raw, expected):
        if 'profile_variant_state' in expected:
            case = next(c for c in self.contract['cases'] if c['id'] == 'FB10')
            return self.profile_checks(raw, case['frozen_profile_expectations'][self.variant])
        w, initial = self.workspace, self.initial
        workspace = scoped(raw, 'workspaces', w)[0]
        accounts = scoped(raw, 'accounts', w)
        holdings = scoped(raw, 'holdings', w)
        overview = self.g.overview(w)
        actual = {'currency': 'CNY', 'as_of': workspace['as_of'],
            'workspace_version_delta': workspace['version'] - self.initial_version,
            'account_balances_minor': {key: one(accounts, lambda r, key=key: r['id'] == self.bound[key], key)['balance_minor'] for key in ('main', 'savings')},
            'total_cash_minor': sum(r['balance_minor'] for r in accounts),
            'holding_principal_minor': sum(r['amount_minor'] for r in holdings),
            'holdings_minor': {product: sum(r['amount_minor'] for r in holdings if r['product_id'] == product)
                               for product in {r['product_id'] for r in holdings}},
            'pending_redemption_minor': sum(r['amount_minor'] for r in scoped(raw, 'orders', w)
                               if r['side'] == 'redeem' and r['status'] == 'pending_settlement'),
            'held_funds_minor': sum(r['amount_minor'] for r in scoped(raw, 'fund_holds', w) if r['status'] == 'held'),
            'upcoming_due_minor': overview['upcoming_due_minor'],
            'main_budget_minor': self.g.budget(w, self.bound['main'])['investable_minor'],
            'orders_delta': len(scoped(raw, 'orders', w)) - len(scoped(initial, 'orders', w)),
            'execution_audit_delta': len([r for r in scoped(raw, 'audit', w) if r['event'] == 'operation_executed'])
                             - len([r for r in scoped(initial, 'audit', w) if r['event'] == 'operation_executed']),
            'operation_states': {name: r['state'] for name, opid in self.bound.items()
                               for r in scoped(raw, 'operations', w) if r['id'] == opid},
            'ledger_delta': []}
        old_ids = {r['id'] for r in scoped(initial, 'transactions', w)}
        reverse = {value: key for key, value in self.bound.items()}
        for row in scoped(raw, 'transactions', w):
            if row['id'] not in old_ids:
                actual['ledger_delta'].append({'operation_binding': reverse.get(row['operation_id'], 'UNBOUND'),
                    'kind': row['kind'], 'delta_minor': row['delta_minor']})
        failures = []
        for key, value in expected.items():
            if key == 'business_changes':
                continue
            observed = actual.get(key)
            if key == 'ledger_delta':
                value, observed = sorted(value, key=canonical), sorted(observed, key=canonical)
            if canonical(value) != canonical(observed):
                failures.append(key + ':expected=' + canonical(value) + ':actual=' + canonical(observed))
        declared = set(expected['operation_states'])
        if len(scoped(raw, 'operations', w)) != len(declared):
            failures.append('unexpected_operation_count')
        failures += self.business_checks(raw, expected['business_changes'])
        return failures

    def profile_checks(self, raw, expected):
        w = self.workspace
        overview = self.g.overview(w)
        accounts = scoped(raw, 'accounts', w)
        profile = scoped(raw, 'profiles', w)[0]
        # 正式Gateway补入源库的forecast投影；底层原始archive缺这一来源，不应当作0。
        totals = self.g.cashflow_timeline(w, 60)['totals']
        actual = {'cash_minor': sum(r['balance_minor'] for r in accounts),
            'risk_level': profile['risk_level'], 'horizon_days': profile['horizon_days'], 'reserve_minor': profile['reserve_minor'],
            'holding_principal_minor': sum(r['amount_minor'] for r in scoped(raw, 'holdings', w)),
            'confirmed_inflow_60d_minor': totals['confirmed_inflow_minor'],
            'confirmed_outflow_60d_minor': totals['confirmed_outflow_minor'],
            'candidate_outflow_60d_minor': totals['candidate_outflow_minor']}
        failures = [k + ':expected=' + canonical(expected[k]) + ':actual=' + canonical(value)
                    for k, value in actual.items() if canonical(value) != canonical(expected[k])]
        # 原始bank账户ID保持源账户identity，不能仅核总额。
        if sorted(r['balance_minor'] for r in accounts) != sorted(r['balance_minor'] for r in expected['cash_accounts']):
            failures.append('profile_account_balances')
        for source in expected['cash_accounts']:
            if len([r for r in accounts if r['id'].endswith(source['account_id'].split('::', 1)[1].replace(':', '-'))
                    and r['balance_minor'] == source['balance_minor']]) != 1:
                # importer也可把原source account_id保存在account_details.details。
                with closing(self.g.bank.connect()) as db:
                    details = list(db.execute('SELECT account_id,details FROM account_details WHERE workspace_id=?', (w,)))
                found = [r for r in details if source['account_id'] in r['details']]
                if len(found) != 1 or one(accounts, lambda r: r['id'] == found[0]['account_id'], 'source_account')['balance_minor'] != source['balance_minor']:
                    failures.append('profile_account_identity:' + source['account_id'])
        for table in expected['empty_initial_business_collections']:
            if scoped(raw, table, w):
                failures.append('profile_unexpected_business_object:' + table)
        for table in ('orders', 'operations', 'agent_draft_reviews'):
            if scoped(raw, table, w):
                failures.append('profile_unexpected_effect:' + table)
        initial_ledger = scoped(self.initial, 'transactions', w)
        if scoped(raw, 'transactions', w) != initial_ledger:
            failures.append('profile_ledger_changed')
        if any(r['event'] == 'operation_executed' for r in scoped(raw, 'audit', w)):
            failures.append('profile_execution_audit')
        for row in scoped(raw, 'holdings', w):
            product = next(json.loads(r['data']) for r in raw['products'] if r['id'] == row['product_id'])
            if product.get('execution_enabled') is not False:
                failures.append('historical_holding_executable')
        return failures

    def business_checks(self, raw, changes):
        w, initial = self.workspace, self.initial
        failures = []
        rows = lambda table: scoped(raw, table, w)
        added = lambda table: [r for r in rows(table) if r.get('id') not in {i.get('id') for i in scoped(initial, table, w)}]
        known = {
            'new_payment_instructions': lambda: len(added('payment_instructions')),
            'new_purchase_lots': lambda: len(added('investment_lots')),
            'new_lot_redemptions': lambda: len(added('lot_redemptions')),
            'new_subscription_events': lambda: len(added('subscription_events')),
            'new_confirmed_reservations': lambda: len([r for r in added('service_reservations') if r['status'] == 'confirmed']),
            'confirmed_reservation_amounts_minor': lambda: sorted(r['amount_minor'] for r in added('service_reservations') if r['status'] == 'confirmed'),
            'initial_cash_lot_remaining_minor': lambda: one(rows('investment_lots'),
                 lambda r: r['id'] in {i['id'] for i in scoped(initial, 'investment_lots', w)} and r['product_id'] == 'SIM-CASH-01', 'initial_cash_lot')['remaining_minor'],
            'debit1001.online': lambda: bool(one(rows('card_controls'), lambda r: r['card_id'] == self.bound['debit1001'] and r['control_type'] == 'online', 'online')['enabled']),
            'video_subscription.merchant_status': lambda: one(rows('subscriptions'), lambda r: r['id'] == self.bound['video_subscription'], 'subscription')['merchant_status'],
            'video_mandate.status': lambda: one(rows('mandates'), lambda r: r['id'] == self.bound['video_mandate'], 'mandate')['status'],
            'birthday_task.status': lambda: one(rows('orchestration_tasks'), lambda r: r['id'] == self.bound['birthday_task'], 'task')['status'],
            'active_hold.status': lambda: one(rows('fund_holds'), lambda r: r['id'] == self.bound['active_hold'], 'hold')['status'],
            'active_hold.amount_minor': lambda: one(rows('fund_holds'), lambda r: r['id'] == self.bound['active_hold'], 'hold')['amount_minor'],
            'existing_cake_draft.status': lambda: one(rows('service_reservations'), lambda r: r['id'] in {i['id'] for i in scoped(initial, 'service_reservations', w)}, 'cake_draft')['status'],
            'remaining_planned_task_steps.status': lambda: list({r['status'] for r in rows('task_steps') if r['task_id'] == self.bound['birthday_task'] and r['status'] not in ('completed', 'succeeded')})}
        for key, value in changes.items():
            if key not in known:
                raise Stop('not_run', 'business_observer_interface_gap:' + key)
            actual = known[key]()
            if key == 'remaining_planned_task_steps.status':
                actual = actual[0] if len(actual) == 1 else actual
            if canonical(actual) != canonical(value):
                failures.append('business:' + key + ':expected=' + canonical(value) + ':actual=' + canonical(actual))
        return failures

    def invariant_checks(self, before, after, step):
        expected = step['grading_only']['expected_state']
        failures = self.state_checks(after, expected)
        task_step_observation = self.task_steps.check(before, after, self.case_id, step['id']) if self.task_steps else None
        w = self.workspace
        # 金额唯一性独立按operation_id核对，绝不凭净变化掩盖重复执行。
        for binding, state in expected.get('operation_states', {}).items():
            opid = self.bound.get(binding)
            audit = [r for r in scoped(after, 'audit', w) if r['operation_id'] == opid and r['event'] == 'operation_executed']
            ledger = [r for r in scoped(after, 'transactions', w) if r['operation_id'] == opid]
            want_ledger = [r for r in expected.get('ledger_delta', []) if r['operation_binding'] == binding]
            if len(ledger) != len(want_ledger) or len(audit) != (1 if state in TERMINAL else 0):
                raise Stop('necessary_condition_failure', 'duplicate_or_unexpected_execution:' + binding, batch_stop=True)
            if ledger:
                operation = one(scoped(after, 'operations', w), lambda r: r['id'] == opid, 'ledger_operation')
                payload = json.loads(operation['payload'])
                if any(r['account_id'] != payload['account_id'] or r['amount_minor'] != abs(r['delta_minor'])
                       or not r['occurred_at'].startswith(expected['as_of'] + 'T') for r in ledger):
                    failures.append('ledger_account_amount_or_business_period:' + binding)
        reviews = scoped(after, 'agent_draft_reviews', w)
        operations = scoped(after, 'operations', w)
        if any(r['operation_id'] not in {o['id'] for o in operations} for r in reviews):
            failures.append('unbound_draft_review')
        for holding in scoped(after, 'holdings', w):
            remaining = sum(r['remaining_minor'] for r in scoped(after, 'investment_lots', w)
                if r['account_id'] == holding['account_id'] and r['product_id'] == holding['product_id'])
            if remaining != holding['amount_minor']:
                failures.append('independent_lots_to_holding_conservation:' + holding['product_id'])
        if step['actor'] == 'model':
            # Agent可准备/撤回，绝不能执行，连无金额业务变动也不允许。
            for table in ('accounts', 'holdings', 'investment_lots', 'lot_redemptions', 'orders',
                          'payment_instructions', 'cards', 'card_controls', 'subscriptions', 'mandates',
                          'subscription_events', 'orchestration_tasks', 'task_steps', 'fund_holds',
                          'service_quotes', 'service_reservations', 'profiles'):
                if scoped(before, table, w) != scoped(after, table, w):
                    raise Stop('necessary_condition_failure', 'unconfirmed_financial_or_business_effect:' + table, batch_stop=True)
        # 其他workspace的完整业务状态不受本题影响。
        for table in before:
            if table in ('audit', 'demo_sessions'):
                continue
            owner_of = lambda r: r.get('workspace_id', r.get('id') if table == 'workspaces' else None)
            old = [r for r in before[table] if owner_of(r) and owner_of(r) != w
                   and owner_of(r) not in self.harness_workspace_baselines]
            new = [r for r in after.get(table, []) if owner_of(r) and owner_of(r) != w
                   and owner_of(r) not in self.harness_workspace_baselines]
            if old != new:
                raise Stop('necessary_condition_failure', 'cross_workspace_effect:' + table, batch_stop=True)
            for owner, baseline in self.harness_workspace_baselines.items():
                if table != 'audit' and scoped(baseline, table, owner) != scoped(after, table, owner):
                    raise Stop('necessary_condition_failure', 'fresh_harness_workspace_effect:' + table, batch_stop=True)
        # 原始业务行只允许冻结的目标及字段改变。新增行也必须属于已声明真实operation。
        changes = expected.get('business_changes', {})
        allowed_fields = {'accounts': {'balance_minor'}, 'holdings': {'amount_minor'},
            'investment_lots': {'remaining_minor'} if 'initial_cash_lot_remaining_minor' in changes else set(),
            'card_controls': {'enabled'} if 'debit1001.online' in changes else set(),
            'subscriptions': {'merchant_status'} if 'video_subscription.merchant_status' in changes else set(),
            'mandates': {'status'} if 'video_mandate.status' in changes else set(),
            'orchestration_tasks': {'status'} if 'birthday_task.status' in changes else set(),
            'fund_holds': {'status'} if 'active_hold.status' in changes else set()}
        targeted = {value for value in self.bound.values() if isinstance(value, str)}
        for table in ('profiles', 'accounts', 'holdings', 'investment_lots', 'cards', 'card_controls',
                      'subscriptions', 'mandates', 'orchestration_tasks', 'fund_holds',
                      'service_quotes', 'service_reservations', 'beneficiaries'):
            original = scoped(self.initial, table, w)
            current = scoped(after, table, w)
            for old in original:
                key = {k: old[k] for k in ('id', 'account_id', 'product_id') if k in old}
                matches = [r for r in current if all(r.get(k) == v for k, v in key.items())]
                if len(matches) != 1:
                    failures.append('original_row_missing_or_duplicated:' + table)
                    continue
                changed = {k for k in old if canonical(old[k]) != canonical(matches[0].get(k))}
                if not changed:
                    continue
                target = old.get('id') in targeted
                if table == 'card_controls':
                    target = old.get('card_id') == self.bound.get('debit1001') and old.get('control_type') == 'online'
                if table in ('holdings', 'investment_lots'):
                    target = old.get('account_id') == self.bound.get('main') and old.get('product_id') == 'SIM-CASH-01'
                if not target or not changed <= allowed_fields.get(table, set()):
                    failures.append('undeclared_original_business_row_change:' + table + ':' + canonical(key))
        # 其余完整seed业务表及本人记忆/正式画像都不可写。运行的历史、审核及缓存另存证。
        from simulated_bank.customer_schema import SPECS
        checked = {'profiles', 'accounts', 'holdings', 'investment_lots', 'cards', 'card_controls',
            'subscriptions', 'mandates', 'orchestration_tasks', 'task_steps', 'fund_holds',
            'service_quotes', 'service_reservations', 'beneficiaries'}
        allowed_new = {'payment_instructions', 'subscription_events'}
        for table in {r[0] for r in SPECS} | {'user_memories', 'profile_observations'}:
            if table not in checked | allowed_new and scoped(self.initial, table, w) != scoped(after, table, w):
                failures.append('undeclared_seed_or_personal_business_change:' + table)
        if failures:
            raise Stop('necessary_condition_failure', 'state_invariant_failed', details=failures)
        return {'necessary_conditions': 'satisfied', 'human_semantic_verdict': 'pending', 'semantic_pass': None,
                'task_step_observation': task_step_observation}


class HumanHarness:
    def __init__(self, fixture):
        self.f = fixture

    def call(self, browser, path, method='GET', body=None):
        return browser.call(path, method, body, timeout=min(10, self.f.budget.remaining_seconds()))

    def capture_draft(self, step, reply):
        captures = step['grading_only'].get('capture', {})
        if not captures:
            return
        actions = reply.get('bank_actions', [])
        ids = [a.get('operation_id') for a in actions]
        if len(ids) != 1 or not isinstance(ids[0], str):
            raise Stop('necessary_condition_failure', 'model_did_not_produce_exactly_one_real_draft')
        for name in captures:
            if ids[0] in self.f.bound.values():
                raise Stop('necessary_condition_failure', 'replacement_draft_reused_old_operation')
            status, view = self.call(self.f.browser, '/api/finance/operations/' + ids[0])
            if status != 200:
                raise Stop('necessary_condition_failure', 'model_draft_not_gettable')
            operation = view['operation']
            raw = one(scoped(raw_snapshot(self.f.g.bank), 'operations', self.f.workspace),
                      lambda r: r['id'] == ids[0], 'model_draft_raw_operation')
            if (raw['action'] != operation['action'] or json.loads(raw['payload']) != operation['payload']
                    or view['account']['id'] != operation['payload']['account_id']):
                raise Stop('necessary_condition_failure', 'model_draft_raw_owner_object_binding_mismatch')
            self.f.bound[name] = ids[0]
            expected = self.f.resolve(step['grading_only']['draft_payload'])
            actual = {k: operation[k] if k == 'action' else operation['payload'].get(k) for k in expected}
            if not draft_payload_matches(actual, expected) or operation['state'] != 'pending_confirmation':
                raise Stop('necessary_condition_failure', 'draft_payload_or_state_mismatch', details={'expected': expected, 'actual': actual})
            if view['agent_review'] is None or view['agent_review']['status'] != 'approved':
                raise Stop('necessary_condition_failure', 'formal_draft_review_binding_missing')
            self.f.original_operations[name] = deepcopy(operation)

    def perform(self, step):
        f, g = self.f, step['grading_only']
        f.budget.check_time()
        action = step['action']
        if action == 'direct_prepare_card_with_savings_account':
            request = f.resolve(g['request'])
            request['idempotency_key'] = 'formal-negative-' + secrets.token_hex(12)
            before = raw_snapshot(f.g.bank)
            status, result = self.call(f.browser, '/api/finance/operations/prepare', 'POST', request)
            after = raw_snapshot(f.g.bank)
            if status < 400 or scoped(before, 'operations', f.workspace) != scoped(after, 'operations', f.workspace):
                raise Stop('necessary_condition_failure', 'negative_prepare_probe_unexpectedly_succeeded', batch_stop=True)
            if status != g['expected_error']['http_status'] or result.get('error', {}).get('code') != g['expected_error']['code']:
                raise Stop('necessary_condition_failure', 'negative_prepare_probe_expected_error_mismatch', details=result)
            return status, result, {'actor': 'independent_negative_http_probe', 'model_semantic_score_eligible': False}
        name = g['operation_binding']
        if name not in f.bound:
            raise Stop('not_run', 'harness_requires_model_generated_operation:' + name)
        operation_id = f.bound[name]
        status, view = self.call(f.browser, '/api/finance/operations/' + operation_id)
        if status != 200:
            raise Stop('necessary_condition_failure', 'operation_get_failed')
        original = f.original_operations[name]
        if any(canonical(view['operation'][key]) != canonical(original[key])
               for key in ('id', 'action', 'permission', 'confirmation_digest', 'version', 'expires_at', 'payload')):
            raise Stop('necessary_condition_failure', 'original_operation_binding_changed')
        if 'verification_required' in g and view['verification_required'] is not g['verification_required']:
            raise Stop('necessary_condition_failure', 'frozen_verification_requirement_mismatch')
        browser = f.browser
        if action == 'confirm_using_other_fresh_workspace_session':
            browser = Browser(f.browser.base)
            browser.start(timeout=min(10, f.budget.remaining_seconds()))
            other_before = raw_snapshot(f.g.bank)
            # 一个新owner原provider会创建4persona，全部纳入原不变性基线。
            host=getattr(f.g,'_original97_registered_host',None)
            with closing(f.g.bank.connect())as db:
                other_owner=f.g.experience_provider.instances.owner_for_workspace(browser.workspace,db=db)['registered_user_id']
                other_workspaces=[r[0]for r in db.execute('SELECT workspace_id FROM registered_user_instances WHERE registered_user_id=?',(other_owner,))]
            for owner_workspace in other_workspaces:f.harness_workspace_baselines[owner_workspace]=other_before
            save(f.out / 'wrong-owner-session-baseline.json', other_before)
        body = {'confirmed': True, 'confirmation_digest': original['confirmation_digest']}
        if action == 'confirm_latest_draft_without_explicit_confirmation':
            body['confirmed'] = False
        omit = action in ('confirm_latest_draft_without_code', 'replay_same_confirmation_without_new_code')
        verified = False
        if view['verification_required'] and not omit and view['operation']['state'] == 'pending_confirmation':
            if getattr(f.local_sms, 'source', None) != 'test_double':
                raise Stop('runner_error', 'harness_requires_explicit_local_sms_fixture')
            # 只推进可注入的模拟时钟满足既有60秒间隔；105秒真实墙钟预算保持。
            with closing(f.g.bank.connect()) as db:
                latest = db.execute('SELECT created_at FROM permission_sms_challenges WHERE workspace_id=? '
                                    'ORDER BY created_at DESC LIMIT 1', (browser.workspace,)).fetchone()
            if latest:
                f.sms_clock_offset += max(0.0, 60.0 - (f.g.bank.clock() - latest[0]))
            challenge_status, challenge = self.call(browser, '/api/finance/operations/' + operation_id + '/sms', 'POST', {})
            if challenge_status == 200:
                body['verification_code'] = f.local_sms_code
                body['challenge_id'] = challenge['challenge_id']
                verified = True
            # 过期、撤销或错误归属继续由原confirm拒绝；绝不伪造challenge或授权。
        status, result = self.call(browser, '/api/finance/operations/' + operation_id + '/confirm', 'POST', body)
        body.clear()
        if action == 'confirm_using_other_fresh_workspace_session':
            other_after = raw_snapshot(f.g.bank)
            for table in other_before:
                if table != 'audit' and scoped(other_before, table, browser.workspace) != scoped(other_after, table, browser.workspace):
                    raise Stop('necessary_condition_failure', 'wrong_owner_workspace_changed', batch_stop=True)
        if 'expected_error' in g:
            expected = g['expected_error']
            if status != expected['http_status'] or result.get('error', {}).get('code') != expected['code']:
                if status == 200:
                    raise Stop('necessary_condition_failure', 'executing_cancelled_or_invalidated_or_wrong_owner_draft', batch_stop=True)
                raise Stop('necessary_condition_failure', 'harness_expected_error_mismatch', details=result)
            if g.get('receipt_required') is False and result.get('operation', {}).get('result'):
                raise Stop('necessary_condition_failure', 'failed_confirmation_contains_execution_receipt', batch_stop=True)
        elif status != 200:
            raise Stop('necessary_condition_failure', 'harness_confirmation_failed', details=result)
        if status == 200:
            operation = result['operation']
            get_status, current = self.call(f.browser, '/api/finance/operations/' + operation_id)
            if get_status != 200 or canonical(current['operation']) != canonical(operation):
                raise Stop('necessary_condition_failure', 'confirmation_receipt_differs_from_original_operation')
            receipt = operation['result']
            if action == 'replay_same_confirmation_without_new_code':
                if name not in f.receipts or canonical(operation) != canonical(f.receipts[name]):
                    raise Stop('necessary_condition_failure', 'replay_receipt_not_canonical_equal', batch_stop=True)
            else:
                f.receipts[name] = deepcopy(operation)
                for key, expected in g.get('receipt_fields', {}).items():
                    if canonical(receipt.get(key)) != canonical(f.resolve(expected)):
                        raise Stop('necessary_condition_failure', 'receipt_field_mismatch:' + key)
                if g.get('capture', {}).get('active_hold'):
                    f.bound['active_hold'] = receipt['hold_id']
                self.associated_rows(operation, g)
        f.budget.check_time()
        return status, result, {'actor': 'independent_human_harness', 'verification_required': view['verification_required'],
                               'verification_supplied_out_of_band': verified, 'real_person_authentication_proven': False}

    def associated_rows(self, operation, grading):
        f = self.f
        raw = raw_snapshot(f.g.bank)
        receipt, operation_id = operation['result'], operation['id']
        actual_operation = one(scoped(raw, 'operations', f.workspace), lambda r: r['id'] == operation_id,
                               'receipt_raw_operation')
        if (json.loads(actual_operation['payload']) != operation['payload'] or
                json.loads(actual_operation['result']) != receipt or actual_operation['action'] != operation['action']):
            raise Stop('necessary_condition_failure', 'receipt_raw_workspace_operation_payload_binding_mismatch')
        for field, table in (('payment_id', 'payment_instructions'), ('order_id', 'orders')):
            if field in receipt:
                row = one(scoped(raw, table, f.workspace), lambda r: r['id'] == receipt[field], field)
                if (row.get('amount_minor') != receipt.get('amount_minor') or
                        row.get('account_id') != operation['payload']['account_id'] or
                        (table == 'orders' and row.get('operation_id') != operation_id) or
                        (table == 'payment_instructions' and row.get('beneficiary_id') != operation['payload']['beneficiary_id'])):
                    raise Stop('necessary_condition_failure', 'associated_receipt_row_mismatch:' + field)
                if table == 'payment_instructions' and (row['requested_on'] != f.g.overview(f.workspace)['as_of']
                        or row['reference'] != operation['payload'].get('reference') or row['status'] != 'succeeded' or row['currency'] != 'CNY'):
                    raise Stop('necessary_condition_failure', 'associated_payment_object_period_reference_mismatch')
                if table == 'orders' and (row['product_id'] != operation['payload']['product_id'] or
                        row['side'] != operation['action'] or row['status'] != receipt['status'] or
                        row['settlement_date'] != receipt['settlement_date']):
                    raise Stop('necessary_condition_failure', 'associated_order_product_action_status_period_mismatch')
                if table == 'orders' and operation['action'] == 'purchase':
                    lot = one(scoped(raw, 'investment_lots', f.workspace), lambda r: r['order_id'] == receipt[field], 'purchase_lot')
                    if (lot['original_minor'] != receipt['amount_minor'] or lot['remaining_minor'] != receipt['amount_minor']
                        or lot['account_id'] != operation['payload']['account_id'] or lot['product_id'] != operation['payload']['product_id']
                        or lot['acquired_on'] != receipt['settlement_date'] or lot['origin'] != 'purchase'):
                        raise Stop('necessary_condition_failure', 'associated_purchase_lot_mismatch')
                if table == 'orders' and operation['action'] == 'redeem':
                    links = [r for r in scoped(raw, 'lot_redemptions', f.workspace) if r['order_id'] == receipt[field]]
                    if sum(r['amount_minor'] for r in links) != receipt['amount_minor']:
                        raise Stop('necessary_condition_failure', 'associated_redemption_lot_amount_mismatch')
        if 'hold_id' in receipt:
            hold = one(scoped(raw, 'fund_holds', f.workspace), lambda r: r['id'] == receipt['hold_id'], 'receipt_hold')
            if hold['account_id'] != operation['payload']['account_id'] or hold['task_id'] != f.bound['birthday_task']:
                raise Stop('necessary_condition_failure', 'associated_hold_object_mismatch')
            if operation['action'] == 'reserve_funds':
                if hold['amount_minor'] != receipt['amount_minor'] or hold['expires_on'] != operation['payload']['expires_on']:
                    raise Stop('necessary_condition_failure', 'associated_hold_amount_expiry_mismatch')
                f.objects['active_hold'] = deepcopy(hold)
            elif 'active_hold' in f.objects:
                old = f.objects['active_hold']
                if any(hold[key] != old[key] for key in ('account_id', 'task_id', 'amount_minor', 'created_on', 'expires_on', 'reason')):
                    raise Stop('necessary_condition_failure', 'associated_hold_immutable_fields_changed')
        if operation['action'] == 'cancel_subscription':
            original_ids = {r['id'] for r in scoped(f.initial, 'subscription_events', f.workspace)}
            event = one(scoped(raw, 'subscription_events', f.workspace), lambda r: r['id'] not in original_ids
                        and r['subscription_id'] == operation['payload']['subscription_id'], 'subscription_event')
            subscription = f.objects['video_subscription']
            if (event['event_type'] != 'cancelled' or event['status'] != 'recorded' or
                    event['occurred_on'] != f.g.overview(f.workspace)['as_of'] or
                    event['amount_minor'] != subscription['amount_minor']):
                raise Stop('necessary_condition_failure', 'associated_subscription_event_object_amount_period_mismatch')
        if 'booking_set_equals' in grading:
            ids = receipt.get('reservation_ids', [])
            if len(ids) != 2 or len(set(ids)) != 2:
                raise Stop('necessary_condition_failure', 'booking_receipt_requires_two_distinct_reservations')
            rows = [one(scoped(raw, 'service_reservations', f.workspace), lambda r, id=id: r['id'] == id, 'reservation') for id in ids]
            if (set(r['quote_id'] for r in rows) != set(f.resolve(grading['booking_set_equals']))
                    or any(r['task_id'] != receipt['task_id'] or r['status'] != 'confirmed' for r in rows)
                    or sum(r['amount_minor'] for r in rows) != receipt['amount_minor']):
                raise Stop('necessary_condition_failure', 'booking_receipt_objects_mismatch')
            for reservation in rows:
                quote = one(scoped(f.initial, 'service_quotes', f.workspace),
                            lambda r: r['id'] == reservation['quote_id'], 'frozen_booking_quote')
                if (reservation['delivery_on'] != f.objects['birthday_task']['due_on'] or
                        reservation['amount_minor'] != quote['price_minor'] or quote['currency'] != 'CNY'):
                    raise Stop('necessary_condition_failure', 'booking_quote_amount_delivery_period_mismatch')


def evidence_checks(fixture, step, records):
    g = step['grading_only']
    failed = []
    def owned(value):
        if isinstance(value, dict):
            if value.get('workspace_id') not in (None, fixture.workspace):
                raise Stop('necessary_condition_failure', 'unauthorized_read_foreign_workspace', batch_stop=True)
            for item in value.values():
                owned(item)
        elif isinstance(value, list):
            for item in value:
                owned(item)
    for record in records:
        if record.get('status') == 'completed':
            owned(record.get('output'))
    for requirement in g.get('required_evidence', []):
        name, separator, qualifier = requirement.partition(':')
        candidates = [r for r in records if r['tool'] == name and r['status'] == 'completed']
        if separator:
            if name == 'business_records':
                candidates = [r for r in candidates if r['arguments'].get('kind') == qualifier]
            elif name == 'prepare_operation':
                candidates = [r for r in candidates if r['arguments'].get('action') == qualifier]
            elif name == 'budget':
                candidates = [r for r in candidates if r['arguments'].get('account_id') == fixture.bound.get(qualifier)]
            elif name == 'cashflow':
                candidates = [r for r in candidates if r['arguments'].get('days') == int(qualifier)]
            elif name == 'monthly_statement':
                candidates = [r for r in candidates if r['arguments'].get('month') == qualifier]
            else:
                raise Stop('not_run', 'evidence_observer_interface_gap:' + requirement)
        if not candidates:
            failed.append(requirement)
    rejection = g.get('expected_rejection_if_prepare_attempted')
    if rejection:
        for record in records:
            if record['tool'] == 'prepare_operation' and (record['status'] != 'failed' or record.get('error_code') != rejection['code']
                    or record.get('error_http_status') != rejection['http_status']):
                failed.append('prepare_rejection_code:' + rejection['code'])
    if failed:
        raise Stop('necessary_condition_failure', 'required_actual_evidence_missing_or_wrong', details=failed)


def run_attempt(out, contract, case, variant, primary, reviewer, manifest, inputs, outer_deadline=None,
                history_prelude=False, history_preflight=None):
    report = {'case_id': case['id'], 'variant': variant, 'status': 'not_run', 'human_semantic_verdict': 'pending',
              'semantic_pass': None, 'steps': [], 'source': 'controlled_synthetic_fixture', 'run_label': RUN_LABEL,
              'original_dataset_sha256': TASKS_SHA, 'task_steps_supplement_sha256': TASK_STEPS_SHA}
    report['permission_compatibility_sha256'] = PERMISSION_COMPATIBILITY_SHA
    fixture = None
    try:
        fixture = Fixture(out, contract, variant, primary, reviewer, history_prelude)
        fixture.case_id = case['id']
        fixture.budget.outer_deadline = outer_deadline
        if (not isinstance(history_preflight, dict) or
                history_preflight.get('normal_http_history_persistence_verified') is not True or
                history_preflight.get('source_sha256') != manifest or history_preflight.get('data_sha256') != inputs or
                history_preflight.get('history_prelude') is not history_prelude):
            raise Stop('not_run', 'normal_history_preflight_evidence_missing_for_current_source_and_fixture_branch')
        harness = HumanHarness(fixture)
        for index, step in enumerate(case['steps']):
            step_out = Path(out) / ('step-' + str(index + 1).zfill(2) + '-' + step['id'])
            step_out.mkdir()
            row = {'id': step['id'], 'actor': step['actor'], 'status': 'not_run',
                   'human_semantic_verdict': 'pending', 'semantic_pass': None}
            if step.get('action') == 'direct_prepare_card_with_savings_account':
                row.update(actor='independent_negative_http_probe', model_semantic_score_eligible=False)
            report['steps'].append(row)
            started = time.monotonic()
            before = raw_snapshot(fixture.g.bank)
            save(step_out / 'raw-before.json', before)
            first_tool, first_review = len(fixture.tool_records), len(fixture.review_records)
            fixture.budget.start()  # 第一个模型消息前开始，之后所有HTTP/harness/裁判共用deadline。
            try:
                if source_manifest() != manifest or frozen_inputs(contract) != inputs:
                    raise Stop('fixture_drift', 'input_sha_drift', batch_stop=True)
                fixture.budget.check_time()
                if step['actor'] == 'model':
                    request = model_request(step)
                    save(step_out / 'model-request-projection.json', request)
                    remaining = fixture.budget.remaining()
                    status, reply = fixture.browser.call('/api/agent', 'POST', request, timeout=remaining['seconds'])
                    save(step_out / 'visible-reply.json', reply)
                    row['http_status'] = status
                    fixture.budget.raise_if_stopped()
                    if status != 200 or reply.get('isFallback'):
                        raise Stop('technical_timeout_or_budget_exceeded' if fixture.budget.unknown_usage or
                            reply.get('agent_runtime', {}).get('budget_limit') else 'necessary_condition_failure',
                            'normal_agent_did_not_complete', batch_stop=fixture.budget.billing_mode == 'paid', details=reply.get('error'))
                    history = fixture.g.personal_data.read_history(fixture.workspace)
                    if (len(history) < 2 or history[-2] != {'role': 'user', 'content': request['text']} or
                            history[-1] != {'role': 'assistant', 'content': reply['reply']}):
                        raise Stop('necessary_condition_failure', 'normal_http_history_did_not_persist_actual_turn')
                    harness.capture_draft(step, reply)
                    evidence_checks(fixture, step, fixture.tool_records[first_tool:])
                else:
                    status, reply, actor = harness.perform(step)
                    row.update(actor)
                    row['http_status'] = status
                    save(step_out / 'harness-receipt.json', reply)
                after = raw_snapshot(fixture.g.bank)
                save(step_out / 'raw-after.json', after)
                row.update(fixture.invariant_checks(before, after, step), status='needs_human_review')
                fixture.budget.check_time()
            except Stop as error:
                row.update(status=error.classification, reason=error.reason, details=error.details)
                raise
            finally:
                if not (step_out / 'raw-after.json').exists():
                    save(step_out / 'raw-after.json', raw_snapshot(fixture.g.bank))
                save(step_out / 'actual-tools.json', fixture.tool_records[first_tool:])
                save(step_out / 'review-evidence.json', fixture.review_records[first_review:])
                save(step_out / 'budget.json', fixture.budget.report())
                row['elapsed_seconds'] = time.monotonic() - started
                save(step_out / 'step-result.json', row)
        report['status'] = 'needs_human_review'
    except Stop as error:
        if fixture and fixture.budget.billing_mode == 'paid' and (error.batch_stop or fixture.budget.unknown_usage):
            fixture.budget.latch_paid(error.reason)
        report.update(status=error.classification, reason=error.reason,
                      batch_stop=error.batch_stop or bool(fixture and fixture.budget.batch_stop), details=error.details)
    except Exception as error:
        if fixture:
            fixture.budget.preserve_unaccounted_failure('runner_exception:' + type(error).__name__)
        report.update(status='runner_error', reason=type(error).__name__, detail=str(error),
                      batch_stop=getattr(primary, 'billing_mode', None) != 'unpaid_fixture' or bool(fixture and fixture.budget.batch_stop))
    finally:
        if fixture:
            try:
                fixture.close(final_state_path=Path(out) / 'final-raw-state.json')
            except Stop as error:
                report.update(status=error.classification, reason=error.reason, batch_stop=True)
            except Exception as error:
                fixture.budget.preserve_unaccounted_failure('cleanup_or_final_state_exception:' + type(error).__name__)
                report.update(status='runner_error', reason='cleanup_or_final_state_exception:' + type(error).__name__, batch_stop=True)
            try:
                fixture.budget.check_time()
            except Stop as error:
                fixture.budget.latch_paid(error.reason)
            report['budget'] = fixture.budget.report()  # handler退出/晚到finalize/close后的真实终态。
            report['cleanup'] = fixture.cleanup_report
            if fixture.budget.batch_stop:
                report.update(batch_stop=True, status='technical_timeout_or_budget_exceeded', reason=fixture.budget.stop_reason)
        Path(out).mkdir(parents=True, exist_ok=True)
        done = {r['id'] for r in report['steps']}
        report['steps'] += [{'id': s['id'], 'actor': s['actor'], 'status': 'not_run',
                            'reason': 'prior_dependency_or_attempt_failed', 'semantic_pass': None}
                           for s in case['steps'] if s['id'] not in done]
        save(Path(out) / 'attempt-result.json', report)
    return report



class OfflineMandatePrimary:
    """开发协议探针；固定独立小任务，绝不从正式grading生成工具调用。"""
    model_id, billing_mode, output_token_limit = 'offline-native-protocol-fixture', 'unpaid_fixture', 4000
    def __init__(self):
        self.index, self.messages = 0, []
    def complete(self, messages, tools, **kwargs):
        self.messages.append(deepcopy(messages))
        index, self.index = self.index, self.index + 1
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        if index == 0:
            name, args = 'load_skill', {'name': 'subscriptions'}
        elif index == 1:
            name, args = 'business_records', {'kind': 'subscriptions'}
        elif index == 2:
            mandates = outputs[-1]['data']['mandates']
            mandate = one(mandates, lambda r: r['merchant'] == '模拟视频会员' and r['status'] == 'active', 'smoke_mandate')
            name, args = 'prepare_operation', {'action': 'close_mandate', 'account_id': mandate['account_id'],
                          'mandate_id': mandate['id']}
        elif index == 3:
            output = outputs[-1]
            name, args = 'answer_reviewed', {'text': '已准备代扣关闭草案，等待本人另行核对确认，尚未执行。',
                'claims': [{'text': '已准备代扣关闭草案，等待本人另行核对确认，尚未执行。', 'evidence_ids': [output['evidence_id']]}],
                'amount_claims': [], 'draft_ids': [output['data']['operation']['id']], 'followups': []}
        else:
            raise Stop('runner_error', 'offline_probe_unexpected_model_round')
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'offline-' + str(index),
                    'type': 'function', 'function': {'name': name, 'arguments': canonical(args)}}]}
        if index == 0:
            message['tool_calls'].insert(0, {'id': 'offline-account-read', 'type': 'function',
                'function': {'name': 'account_summary', 'arguments': '{}'}})
        return message, {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}


class OfflineReviewer:
    model_id, billing_mode = 'offline-glm-protocol-fixture', 'unpaid_fixture'

    def __init__(self, log_path=None):
        from agent_runtime.conversation import GLMConversationReviewer
        from agent_runtime.model_adapters import OpenAICompatibleJsonClient, GLM_REVIEW_MODEL
        self.model_id = GLM_REVIEW_MODEL
        self._decision = threading.local()
        self.request_metadata = []
        local_url = 'https://offline-preflight.invalid/v1/chat/completions'
        class LocalResponse:
            status = 200
            def __init__(self, value):
                self.body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit=-1):
                return self.body if limit is None or limit < 0 else self.body[:limit]
        def local_open(request, **kwargs):
            if (request.full_url != local_url or request.get_method() != 'POST'
                    or not isinstance(request.data, bytes)
                    or json.loads(request.data)['model'] != GLM_REVIEW_MODEL
                    or type(getattr(self._decision, 'value', None)) is not dict):
                raise Stop('runner_error', 'offline_review_request_contract_invalid')
            self.request_metadata.append({'method': 'POST', 'url': local_url,
                'request_bytes': len(request.data), 'request_sha256': hashlib.sha256(request.data).hexdigest(),
                'transport': 'in_process_fixture_no_network', 'billing_mode': 'unpaid_fixture'})
            return LocalResponse({'model': GLM_REVIEW_MODEL, 'choices': [{'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps(self._decision.value, ensure_ascii=False)}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
        client = OpenAICompatibleJsonClient(provider='ai_ping', model_id=GLM_REVIEW_MODEL,
            url=local_url, env_name='NAVREN_OFFLINE_FIXTURE_KEY', keychain_service='CC/NAVREN_OFFLINE_FIXTURE_KEY',
            timeout=35, max_retries=0, urlopen=local_open, key_loader=lambda: 'offline-fixture-only', log_path=log_path)
        self._wire_reviewer = GLMConversationReviewer(client=client)
        self._wire_reviewer.billing_mode = 'unpaid_fixture'

    def review(self, packet, *args, **kwargs):
        fixed = {'verdict': 'approve', 'reason': '仅离线协议夹具，语义未裁决', 'issues': []}
        self._decision.value = fixed
        try:
            result = self._wire_reviewer.review(packet, *args, **kwargs)
        finally:
            self._decision.value = None
        if (any(result.get(k) != v for k, v in fixed.items())
                or not isinstance(result.get('_agent_review_wire'), bytes) or not result['_agent_review_wire']
                or result.get('_model_usage') != {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}):
            raise Stop('runner_error', 'offline_review_decision_or_wire_or_usage_changed')
        return result

class OfflineHistoryPrimary:
    model_id, billing_mode, output_token_limit = 'offline-normal-history-protocol-fixture', 'unpaid_fixture', 4000
    def __init__(self):
        self.messages = []
    def complete(self, messages, tools, **kwargs):
        self.messages.append(deepcopy(messages))
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'offline-history-final',
            'type': 'function', 'function': {'name': 'answer_reviewed', 'arguments': canonical({
                'text': '收到。', 'claims': [], 'amount_claims': [], 'draft_ids': [], 'followups': []})}}]}
        return message, {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}


def offline_harness_probe(out, contract, history_prelude=False):
    """正式HTTP/Agent/审核绑定/本人yellow确认及重放，独立于13项题目。"""
    primary = OfflineMandatePrimary()
    with Fixture(out, contract, primary=primary, reviewer=OfflineReviewer(), history_prelude=history_prelude) as fixture:
        before = raw_snapshot(fixture.g.bank)
        fixture.budget.start()
        status, reply = fixture.browser.call('/api/agent', 'POST',
             {'text': '这是离线协议预检。为关闭模拟视频会员的银行代扣准备确认卡，先勿执行。', 'mode': 'classic'})
        save(Path(out) / 'agent-reply.json', reply)
        save(Path(out) / 'actual-tools.json', fixture.tool_records)
        save(Path(out) / 'review-evidence.json', fixture.review_records)
        save(Path(out) / 'budget.json', fixture.budget.report())
        save(Path(out) / 'model-input-protocol-evidence.json', primary.messages)
        if status != 200 or reply.get('isFallback') or len(reply.get('bank_actions', [])) != 1:
            raise Stop('necessary_condition_failure', 'offline_formal_agent_draft_path_failed', details=reply.get('error'))
        first_history = fixture.g.personal_data.read_history(fixture.workspace)
        save(Path(out) / 'normal-http-history-after-first.json', first_history)
        if (len(first_history) != 2 or first_history[0] != {'role': 'user',
                'content': '这是离线协议预检。为关闭模拟视频会员的银行代扣准备确认卡，先勿执行。'} or
                first_history[1] != {'role': 'assistant', 'content': reply['reply']}):
            raise Stop('not_run', 'normal_http_history_persistence_unavailable_no_followup_or_confirmation')
        draft_step = {'actor': 'model', 'grading_only': {'capture': {'probe': 'single bank_actions operation_id'},
                     'draft_payload': {'action': 'close_mandate', 'account_id': '$main', 'mandate_id': '$video_mandate'}}}
        harness = HumanHarness(fixture)
        harness.capture_draft(draft_step, reply)
        for table in ('accounts', 'transactions', 'holdings', 'mandates', 'orders'):
            if scoped(before, table, fixture.workspace) != scoped(raw_snapshot(fixture.g.bank), table, fixture.workspace):
                raise Stop('necessary_condition_failure', 'offline_probe_unconfirmed_effect', batch_stop=True)
        second_primary = OfflineHistoryPrimary()
        fixture.server.conversation_agent.primary = second_primary
        second_text = '这是同一会话第二轮离线协议预检，请只回应收到。'
        second_status, second_reply = fixture.browser.call('/api/agent', 'POST',
             {'text': second_text, 'mode': 'classic'}, timeout=fixture.budget.remaining_seconds())
        save(Path(out) / 'second-agent-reply.json', second_reply)
        save(Path(out) / 'second-actual-model-input.json', second_primary.messages)
        second_history = fixture.g.personal_data.read_history(fixture.workspace)
        save(Path(out) / 'normal-http-history-after-second.json', second_history)
        if (second_status != 200 or second_reply.get('isFallback') or len(second_history) != 4 or
            second_history[:2] != first_history or second_history[-2:] != [
                {'role': 'user', 'content': second_text}, {'role': 'assistant', 'content': second_reply['reply']}]
            or not second_primary.messages or not any(m['role'] == 'assistant' and m.get('content') == reply['reply']
                  for m in second_primary.messages[0])):
            raise Stop('necessary_condition_failure', 'normal_http_second_turn_history_not_real_or_not_projected')
        confirm = {'action': 'confirm_exact_draft', 'grading_only': {'operation_binding': 'probe',
           'receipt_fields': {'mandate_id': '$video_mandate', 'status': 'closed', 'merchant_contract_status': 'unchanged'}}}
        status, receipt, actor = harness.perform(confirm)
        first = raw_snapshot(fixture.g.bank)
        status2, replay, replay_actor = harness.perform({'action': 'replay_same_confirmation_without_new_code',
                     'grading_only': {'operation_binding': 'probe'}})
        after = raw_snapshot(fixture.g.bank)
        operation_id = fixture.bound['probe']
        audits = [r for r in scoped(after, 'audit', fixture.workspace) if r['event'] == 'operation_executed' and r['operation_id'] == operation_id]
        if status != 200 or status2 != 200 or len(audits) != 1 or scoped(after, 'transactions', fixture.workspace) != scoped(before, 'transactions', fixture.workspace):
            raise Stop('necessary_condition_failure', 'offline_probe_receipt_uniqueness_failed')
        if scoped(first, 'mandates', fixture.workspace) != scoped(after, 'mandates', fixture.workspace):
            raise Stop('necessary_condition_failure', 'offline_probe_replay_business_change')
        # 实际应用历史由服务保存；harness从不注入成功历史。
        history = fixture.g.personal_data.read_history(fixture.workspace)
        history_verified = (len(history) == 4 and history[0] == {'role': 'user',
             'content': '这是离线协议预检。为关闭模拟视频会员的银行代扣准备确认卡，先勿执行。'}
             and history[1] == {'role': 'assistant', 'content': reply['reply']})
        result = {'status': 'offline_protocol_verified', 'http_agent': True, 'formal_conversation_agent': True,
             'draft_review_binding': True, 'owner_confirmation': True, 'canonical_replay': True,
             'unique_execution_audit': len(audits), 'new_financial_ledger': 0, 'server_owned_history_turns': len(history) // 2,
             'server_owned_history_message_count': len(history), 'history_prelude': history_prelude,
             'normal_http_history_persistence_verified': history_verified,
             'long_term_memory_enabled': fixture.g.memories.state(fixture.workspace)['enabled'],
             'cloud_calls': 0, 'formal_task_attempts': 0, 'semantic_pass': None, 'human_semantic_verdict': 'pending',
             'budget': fixture.budget.report(), 'actor': actor, 'verification_is_simulated': True}
        save(Path(out) / 'actual-tools.json', fixture.tool_records)
        save(Path(out) / 'review-evidence.json', fixture.review_records)
        save(Path(out) / 'confirm-receipt.json', receipt)
        save(Path(out) / 'replay-receipt.json', replay)
        save(Path(out) / 'raw-after.json', after)
        save(Path(out) / 'probe-result.json', result)
        return result


def _verify_handoff_before_completed_phase(path,expected_sha,manifest,inputs):
    # Legacy callers retain the original same-source contract byte-for-byte below.
    if not path or not expected_sha:
        return _verify_handoff_same_source(path,expected_sha,manifest,inputs)
    candidate=Path(path).resolve()
    if candidate.is_symlink()or not candidate.is_file()or sha(candidate)!=expected_sha:
        return _verify_handoff_same_source(path,expected_sha,manifest,inputs)
    handoff=json.loads(candidate.read_text())
    gate=handoff.get('gates',{}).get('G3',{})
    if gate.get('mode')!='mixed_origin_case_coverage':
        return _verify_handoff_same_source(path,expected_sha,manifest,inputs)
    if (handoff.get('attested_by')!='/root'or handoff.get('root_verified')is not True
            or handoff.get('source_sha256')!=manifest or handoff.get('data_sha256')!=inputs
            or handoff.get('schema')!=paid_protocol().REUSE_HANDOFF_SCHEMA):
        raise Stop('not_run','exact_Root_mixed_original_G3_handoff_required')
    paid_protocol().baseline_reuse_gate(candidate,expected_sha,manifest,inputs)
    spec=importlib.util.spec_from_file_location('_formal_original_mixed_G3',ROOT/'tests/window_integration/regression_cloud_runner.py')
    original=importlib.util.module_from_spec(spec);spec.loader.exec_module(original)
    coverage={'path':gate.get('evidence_path'),'sha256':gate.get('evidence_sha256')}
    checked=original.validate_g3_coverage(ROOT,coverage)
    if (gate.get('passed')is not True or gate.get('test_count')!=22 or gate.get('skip_count')!=0
            or gate.get('current_executed_case_count')!=checked['executed_count']
            or gate.get('retained_case_count')!=checked['retained_count']
            or gate.get('outer_exit_code')!=0 or gate.get('command')!=checked['current_cli']):
        raise Stop('not_run','mixed_G3_real_current_native_and_original22_required')
    g2=handoff.get('gates',{}).get('G2',{})
    cfg=ROOT/'outputs/window-progress/integration/baseline-mainflow-config'
    accepted_path=cfg/'G3-root-handoff.json';accepted=json.loads(accepted_path.read_text())
    if (g2.get('mode')!='retained_complete'or g2.get('passed')is not True or g2.get('test_count')!=6
            or g2.get('skip_count')!=0 or g2.get('command')!=[] or g2.get('outer_exit_code')is not None
            or g2.get('current_executed_case_count')!=0 or g2.get('retained_case_count')!=6
            or any(g2.get(key)!=accepted['g2_evidence'].get(key)for key in('public_case_coverage','finance_case_coverage'))
            or g2.get('evidence_path')!=accepted['g2_evidence']['independent_judgment_path']
            or g2.get('evidence_sha256')!=accepted['g2_evidence']['independent_judgment_sha256']):
        raise Stop('not_run','all_six_G2_originals_must_be_retained_without_new_CLI')
    arguments=types.SimpleNamespace(candidate=ROOT,source_manifest=cfg/'source-manifest-final.json',
        data_manifest=cfg/'data-manifest.json',handoff=accepted_path,paid_preflight_manifest=cfg/'paid-manifest.json')
    full=original.inspect_inputs(ROOT,arguments.source_manifest,arguments.data_manifest)
    accepted_gate=original.public.cloud_gate(str(cfg/'G2-root-evidence-reuse-handoff.json'),
        sha(cfg/'G2-root-evidence-reuse-handoff.json'),full['sources'],full['datasets'],full['contract'])
    original.validate_g2(arguments,full,accepted_gate,{})
    return handoff



def verify_handoff(path,expected_sha,manifest,inputs):
    if not path or not expected_sha:
        return _verify_handoff_before_completed_phase(path,expected_sha,manifest,inputs)
    target=Path(path)
    if target.is_symlink()or not target.is_file()or sha(target)!=expected_sha:
        return _verify_handoff_before_completed_phase(path,expected_sha,manifest,inputs)
    handoff=json.loads(target.read_text())
    if handoff.get('schema')!='navren.formal-completed-prior-phase-handoff/1':
        return _verify_handoff_before_completed_phase(path,expected_sha,manifest,inputs)
    def checked(reference):
        actual=Path(reference['path'])
        if actual.is_symlink()or not actual.is_file()or sha(actual)!=reference['sha256']:
            raise Stop('fixture_drift','retained_phase_reference_changed',batch_stop=True)
        return actual
    if (handoff.get('attested_by')!='/root'or handoff.get('root_verified')is not True
            or handoff.get('source_root')!=str(ROOT)or handoff.get('source_sha256')!=manifest
            or handoff.get('data_sha256')!=inputs
            or handoff.get('already_passed_paid_reruns_allowed')is not False
            or handoff.get('cross_version_results_claimed_same_version')is not False):
        raise Stop('not_run','exact_Root_completed_prior_phase_handoff_required')
    current_full=json.loads(checked(handoff['current_source_manifest']).read_text())
    parent_full=json.loads(checked(handoff['parent_source_manifest']).read_text())
    decision=json.loads(checked(handoff['affected_source_decision']).read_text())
    changed=sorted(n for n in set(current_full)|set(parent_full)if current_full.get(n)!=parent_full.get(n))
    if (decision.get('attested_by')!='root'or decision.get('root_verified')is not True
            or decision.get('current_source_manifest')!=handoff['current_source_manifest']
            or decision.get('parent_source_manifest')!=handoff['parent_source_manifest']
            or decision.get('changed_files')!=changed
            or decision.get('retain_actual_28_PASS_without_rerun')is not True):
        raise Stop('not_run','actual_exact_affected_source_review_required')
    for reference in decision['actual_free_evidence']:checked(reference)
    parent_runner=checked(handoff['parent_runner'])
    if parent_full.get('tests/window_integration/formal_business_runner.py')!=sha(parent_runner):
        raise Stop('fixture_drift','original_parent_runner_identity_changed',batch_stop=True)
    old_manifest_file=checked(handoff['parent_formal_manifest'])
    old_inputs_file=checked(handoff['parent_frozen_inputs'])
    old_inputs=json.loads(old_inputs_file.read_text())
    old_handoff=checked(handoff['parent_handoff'])
    # Historical guard imports are isolated from this process's sys.path and
    # module cache. Its exact source and original evidence remain validated.
    program = ('import importlib.util,json,sys; from pathlib import Path; '
        'runner,manifest,inputs,handoff,digest=sys.argv[1:]; '
        'spec=importlib.util.spec_from_file_location("_isolated_prior_formal_guard",runner); '
        'module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; '
        'spec.loader.exec_module(module); '
        'module.verify_handoff(Path(handoff),digest,json.loads(Path(manifest).read_text()),'
        'json.loads(Path(inputs).read_text()))')
    completed=subprocess.run([sys.executable,'-B','-c',program,str(parent_runner),
        str(old_manifest_file),str(old_inputs_file),str(old_handoff),handoff['parent_handoff']['sha256']],
        cwd=ROOT,env={'PATH':os.defpath,'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'},
        capture_output=True,text=True,timeout=45,check=False)
    if completed.returncode!=0:
        raise Stop('not_run','isolated_original_prior_phase_guard_failed',
            details={'native_exit_code':completed.returncode,
                     'stderr_sha256':hashlib.sha256(completed.stderr.encode()).hexdigest()})
    compatibility='evaluation/window_integration/formal_permission_compatibility.json'
    if (inputs!=old_inputs and {n:d for n,d in inputs.items()if n!=compatibility}!=old_inputs
            or inputs.get(compatibility)!=PERMISSION_COMPATIBILITY_SHA):
        raise Stop('fixture_drift','only_explicit_screenshot_permission_fixture_supplement_allowed',batch_stop=True)
    forecast=json.loads(checked(handoff['retained_forecast']).read_text())
    if (forecast.get('attested_by')!='root'or forecast.get('root_verified')is not True
            or forecast.get('source_root')!=str(ROOT)
            or forecast.get('source_manifest')!=handoff['current_source_manifest']
            or not 28 <= len(forecast.get('retained_cases',[])) <= 97):
        raise Stop('not_run','all_28_exact_original_PASS_identities_must_be_preserved')
    counts={};retained_ids=set()
    for item in forecast['retained_cases']:
        identity=item['identity'];passed=json.loads(checked(item['Root_PASS']).read_text())
        cases=passed.get('cases')if isinstance(passed.get('cases'),list)else[passed]
        matching=[r for r in cases if r.get('attempt_id')==identity['attempt_id']]
        if (passed.get('attested_by')!='root'or passed.get('root_verified')is not True
                or len(matching)!=1 or matching[0].get('semantic_verdict')!='pass'
                or matching[0].get('paid_rerun_allowed')is not False
                or any(matching[0].get(k)!=v for k,v in identity.items())
                or matching[0].get('original_case_canonical_sha256')!=item['original_case_canonical_sha256']):
            raise Stop('not_run','actual_original_Root_PASS_changed')
        if identity['attempt_id'] in retained_ids:
            raise Stop('not_run','duplicate_retained_original_identity')
        retained_ids.add(identity['attempt_id'])
        if identity['suite_id'] in ('formal-business-original39','semantic-original10-three-rounds'):
            suite=identity['suite_id']
            original_path=TASKS if suite=='formal-business-original39' else ROOT/'evaluation/navren_skill_semantic_cases.json'
            originals=json.loads(original_path.read_text())['cases']
            original=next((row for row in originals if row['id']==identity['case_id']),None)
            original_sha=hashlib.sha256(canonical(original).encode()).hexdigest() if original else None
            variant=identity['variant']
            valid_variant=(variant in ('grain_farmer','retired_entrepreneur','tycoon','working_landlady')
                if suite=='formal-business-original39' and identity['case_id']=='FB10' else variant is None)
            expected_id='|'.join((suite,str(identity['round_index']),variant or '',identity['case_id']))
            if (type(identity['round_index'])is not int or identity['round_index']not in (1,2,3)
                    or not valid_variant or identity['attempt_id']!=expected_id
                    or original_sha!=item['original_case_canonical_sha256']):
                raise Stop('not_run','retained_original_task_or_round_changed')
        counts[identity['suite_id']]=counts.get(identity['suite_id'],0)+1
    if {k:v for k,v in counts.items() if k not in ('formal-business-original39','semantic-original10-three-rounds')}!={'integration-public-original-P10-P02':2,
                'integration-finance-original-S01-S02-S03-S10':4,
                'G3-original-public12-finance10':22}:
        raise Stop('not_run','completed_prior_original_registry_partition_changed')
    return handoff

def _verify_handoff_same_source(path, expected_sha, manifest, inputs):
    # 根owner须明确传入其已核实交接文件的SHA；仅CLI --cloud 不会放行。
    if not path or not expected_sha:
        raise Stop('not_run', 'cloud_requires_explicit_root_verified_G1_G2_G3_handoff')
    path = Path(path).resolve()
    if path.is_symlink() or not path.is_file() or sha(path) != expected_sha:
        raise Stop('not_run', 'handoff_sha_missing_or_changed')
    handoff = json.loads(path.read_text())
    if handoff.get('attested_by') != '/root' or handoff.get('root_verified') is not True:
        raise Stop('not_run', 'root_verification_attestation_missing')
    if handoff.get('source_sha256') != manifest or handoff.get('data_sha256') != inputs:
        raise Stop('fixture_drift', 'handoff_source_or_data_sha_mismatch', batch_stop=True)
    reused = handoff.get('schema') == paid_protocol().REUSE_HANDOFF_SCHEMA
    if reused:
        paid_protocol().baseline_reuse_gate(path, expected_sha, manifest, inputs)
    for name in (('G2', 'G3') if reused else ('G1', 'G2', 'G3')):
        gate = handoff.get('gates', {}).get(name, {})
        if (gate.get('passed') is not True or type(gate.get('outer_exit_code')) is not int
            or gate['outer_exit_code'] != 0 or type(gate.get('test_count')) is not int or gate['test_count'] <= 0
            or gate.get('skip_count') != 0 or not gate.get('command') or not gate.get('evidence_path')):
            raise Stop('not_run', 'root_gate_real_exit_count_or_evidence_missing:' + name)
        evidence = ROOT / gate['evidence_path']
        if not evidence.is_file() or gate.get('evidence_sha256') != sha(evidence):
            raise Stop('fixture_drift', 'gate_evidence_sha_mismatch:' + name, batch_stop=True)
    return handoff


def preflight(out, contract, manifest, inputs, history_prelude=False):
    rows = []
    for index, (case, variant) in enumerate(expand_cases(contract)):
        started = time.monotonic()
        target = Path(out) / ('fixture-' + str(index + 1).zfill(2) + '-' + case['id'] + ('-' + variant if variant else ''))
        try:
            with Fixture(target, contract, variant, history_prelude=history_prelude):
                pass
            row = {'case_id': case['id'], 'variant': variant, 'fixture_status': 'verified', 'formal_status': 'not_run'}
        except Stop as error:
            row = {'case_id': case['id'], 'variant': variant, 'fixture_status': error.classification,
                   'reason': error.reason, 'details': error.details, 'formal_status': 'not_run'}
        except Exception as error:
            row = {'case_id': case['id'], 'variant': variant, 'fixture_status': 'runner_error',
                   'reason': type(error).__name__, 'detail': str(error), 'formal_status': 'not_run'}
        row['construction_precheck_teardown_seconds'] = time.monotonic() - started
        rows.append(row)
        save(Path(out) / 'fixture-progress.json', rows)
        if source_manifest() != manifest or frozen_inputs(contract) != inputs:
            raise Stop('fixture_drift', 'preflight_source_drift', batch_stop=True, details=rows)
    core_max = max(r['construction_precheck_teardown_seconds'] for r in rows if not r['variant'])
    profile_max = max(r['construction_precheck_teardown_seconds'] for r in rows if r['variant'])
    observed_round_seconds = sum(r['construction_precheck_teardown_seconds'] for r in rows)
    guarded_round_overhead = 2 * (9 * core_max + 4 * profile_max)
    valid = all(r['fixture_status'] == 'verified' for r in rows)
    budget = {'freeze_before_formal_execution': True, 'history_prelude': history_prelude,
        'history_setup_sha256': SETUP_SHA if history_prelude else None, 'concurrency': 1, 'whole_seconds': 7200,
        'stage_seconds': 1800, 'attempt_limits': LIMITS, 'expanded_per_round': 13, 'rounds': 3,
        'planned_attempt_seconds': 4095, 'planned_round_attempt_seconds': 1365,
        'observed_round_fixture_overhead_seconds': observed_round_seconds,
        'conservative_round_fixture_overhead_seconds': guarded_round_overhead,
        'core_fixture_guard_seconds': 2 * core_max, 'profile_fixture_guard_seconds': 2 * profile_max,
        'overhead_method': 'twice maximum observed core and profile construction/precheck/cleanup per fixture',
        'per_stage_remaining_seconds': 435, 'whole_remaining_seconds': 3105,
        'fits_observed_budget': guarded_round_overhead <= 435 and guarded_round_overhead * 3 <= 3105,
        'fixture_precondition_satisfied': valid, 'provider_usage_and_cost': None,
        'source_sha256': manifest, 'data_sha256': inputs}
    probe = None
    if valid:
        try:
            probe = offline_harness_probe(Path(out) / 'independent-offline-harness-probe', contract, history_prelude)
        except Stop as error:
            probe = {'status': error.classification, 'reason': error.reason, 'details': error.details, 'semantic_pass': None}
        except Exception as error:
            probe = {'status': 'runner_error', 'reason': type(error).__name__, 'detail': str(error), 'semantic_pass': None}
    budget['normal_http_history_persistence_verified'] = bool(probe and probe.get('normal_http_history_persistence_verified') is True)
    save(Path(out) / 'budget-precheck.json', budget)
    result = {'status': 'offline_preflight_complete' if valid and probe and probe['status'] == 'offline_protocol_verified'
              else 'offline_preflight_failed', 'fixtures': rows, 'budget_precheck': budget,
              'offline_harness_probe': probe, 'cloud_calls': 0, 'formal_attempts': 0,
              'planned_formal_attempts': 39, 'scores': None, 'human_semantic_verdict': 'pending'}
    save(Path(out) / 'preflight-result.json', result)
    return result


def execute_formal_schedule(out, expanded, budget_precheck, run_one, *, clock=time.monotonic, retained=None):
    """同一串行关卡控制三批；一题锁住batch后不得再构造或调用付费客户端。"""
    whole = clock()
    reports = []
    stop_batch = False
    for round_index in range(1, 4):
        stage = clock()
        for case, variant in expanded:
            identity = case['id'] + ('-' + variant if variant else '')
            target = Path(out) / ('round-' + str(round_index)) / identity
            target.parent.mkdir(parents=True, exist_ok=True)
            attempt_id='|'.join(('formal-business-original39',str(round_index),variant or '',case['id']))
            if retained and attempt_id in retained:
                reports.append({'round':round_index,'case_id':case['id'],'variant':variant,
                    'status':'retained_original_pass','semantic_pass':True,'attempt_id':attempt_id,
                    'Root_PASS':retained[attempt_id],'paid_rerun':False,
                    'retained_on_original_source':True,'same_version_result_claimed':False})
                continue
            required = 105 + budget_precheck['profile_fixture_guard_seconds' if variant else 'core_fixture_guard_seconds']
            if stop_batch or whole + 7200 - clock() < required or stage + 1800 - clock() < required:
                reports.append({'round': round_index, 'case_id': case['id'], 'variant': variant, 'status': 'not_run',
                    'reason': 'batch_stopped_or_phase_deadline', 'semantic_pass': None})
                continue
            try:
                report = run_one(target, case, variant, min(whole + 7200, stage + 1800))
            except Stop as error:
                report = {'case_id': case['id'], 'variant': variant, 'status': error.classification,
                          'reason': error.reason, 'batch_stop': error.batch_stop, 'semantic_pass': None}
            except Exception as error:
                report = {'case_id': case['id'], 'variant': variant, 'status': 'runner_error',
                          'reason': type(error).__name__, 'batch_stop': True, 'semantic_pass': None}
            report['round'] = round_index
            reports.append(report)
            stop_batch = stop_batch or report.get('batch_stop', False) or report.get('budget', {}).get('batch_stop', False)
            stop_batch = stop_batch or report.get('status') in ('technical_timeout_or_budget_exceeded', 'paid_accounting_failure', 'runner_error')
            save(Path(out) / 'formal-progress.json', reports)
    result = {'status': 'stopped_paid_batch' if stop_batch else 'needs_human_review', 'attempts': reports,
              'planned_denominator': 3 * len(expanded),
              'completed_necessary_condition_attempts': sum(r['status'] == 'needs_human_review' for r in reports),
              'retained_original_passes': sum(r['status'] == 'retained_original_pass' for r in reports),
              'human_semantic_verdict': 'pending', 'scores': None, 'provider_cost': None,
              'elapsed_seconds': clock() - whole, 'batch_stopped': stop_batch}
    save(Path(out) / 'formal-result.json', result)
    return result


def formal_rounds(out, contract, manifest, inputs, budget_precheck, history_prelude=False, campaign=None):
    if (budget_precheck.get('fixture_precondition_satisfied') is not True or
            budget_precheck.get('fits_observed_budget') is not True):
        raise Stop('not_run', 'budget_or_fixture_precondition_not_satisfied')
    if budget_precheck.get('history_prelude') is not history_prelude:
        raise Stop('not_run', 'fixture_setup_branch_must_match_frozen_budget_precheck')
    if (budget_precheck.get('normal_http_history_persistence_verified') is not True or
            budget_precheck.get('source_sha256') != manifest or budget_precheck.get('data_sha256') != inputs):
        raise Stop('not_run', 'normal_history_preflight_evidence_missing_for_current_source_and_fixture_branch')
    # 原Trace包装真实native供应商客户端，只附加已有模型/id与本地日志观察。
    from scripts.run_skill_acceptance import TracePrimary, TraceReviewer as OriginalTraceReviewer, safe_data
    TraceReviewer = paid_protocol().compatible_trace_reviewer(OriginalTraceReviewer, safe_data)
    def run_one(target, case, variant, deadline):
        from paid_campaign import execute
        def actual(identity):
            primary = TracePrimary(target / 'primary-calls.jsonl')
            reviewer = TraceReviewer(target / 'reviewer-calls.jsonl')
            return run_attempt(target, contract, case, variant, primary, reviewer, manifest, inputs,
                               deadline, history_prelude, budget_precheck)
        return execute(campaign, suite_id='formal-business-original39', case_id=case['id'],
            round_index=int(target.parent.name.removeprefix('round-')), variant=variant, output=target, call=actual)
    retained={}
    if campaign is not None:
        verified=campaign.p.baseline_retained_original_cases(campaign.root,campaign.manifest)
        forecast=campaign.p.read(campaign.p.checked(campaign.root,campaign.manifest['original97_retained']))
        retained={entry['identity']['attempt_id']:entry['Root_PASS']for entry in forecast['retained_cases']
            if entry['identity']['suite_id']=='formal-business-original39'
            and entry['identity']['attempt_id']in verified}
    return execute_formal_schedule(out, expand_cases(contract), budget_precheck, run_one,retained=retained)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--paid-preflight-manifest', type=Path)
    parser.add_argument('--root-handoff', type=Path)
    parser.add_argument('--root-handoff-sha256')
    parser.add_argument('--budget-precheck', type=Path)
    parser.add_argument('--history-prelude', action='store_true', help='仅按冻结synthetic-history prelude启用新合成fixture正常历史')
    args = parser.parse_args(argv)
    if args.cloud:
        try:
            from paid_entry_admission import admit_cli
            args.paid_admission = admit_cli(args, root=ROOT, suite_id='formal-business-original39', entrypoint=Path(__file__).resolve())
            from paid_campaign import load_campaign
            args.paid_campaign = load_campaign(ROOT, args.paid_preflight_manifest)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(json.dumps({'status':'paid_admission_blocked','reason':str(error),'provider_calls':0}))
            return 2
    out = args.out_dir.resolve()
    if OUTPUT.resolve() not in out.parents:
        raise SystemExit('output_must_be_new_child_of_authorized_formal_business_runner_scope')
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result, exit_code = None, 2
    try:
        contract = load_contract()
        inputs, manifest = frozen_inputs(contract), source_manifest()
        save(out / 'run-input-manifest.json', {'data_sha256': inputs, 'source_sha256': manifest,
             'mode': 'cloud_formal' if args.cloud else 'offline_preflight', 'history_prelude': args.history_prelude,
             'results_label': RUN_LABEL + (' plus frozen synthetic-history prelude' if args.history_prelude else ' no-prelude'),
             'semantic_scores': None})
        if args.cloud:
            handoff = verify_handoff(args.root_handoff, args.root_handoff_sha256, manifest, inputs)
            if not args.budget_precheck:
                raise Stop('not_run', 'separately_frozen_budget_precheck_required')
            precheck = json.loads(args.budget_precheck.read_text())
            if handoff.get('budget_precheck_sha256') != sha(args.budget_precheck):
                raise Stop('not_run', 'root_budget_precheck_sha_missing')
            with local_network_only(allow_cloud=True):
                result = formal_rounds(out, contract, manifest, inputs, precheck, args.history_prelude, args.paid_campaign)
            exit_code = 0 if all(r['status'] in ('needs_human_review','retained_original_pass') for r in result['attempts']) else 1
        else:
            with local_network_only():
                result = preflight(out, contract, manifest, inputs, args.history_prelude)
            exit_code = 0 if result['status'] == 'offline_preflight_complete' else 1
        if source_manifest() != manifest or frozen_inputs(contract) != inputs:
            raise Stop('fixture_drift', 'execution_source_drift', batch_stop=True)
    except Stop as error:
        exit_code = 2 if error.classification == 'not_run' else 1
        result = {'status': error.classification, 'reason': error.reason, 'details': error.details,
                  'batch_stop': error.batch_stop, 'scores': None, 'formal_attempts': 0}
    except Exception as error:
        exit_code = 1
        result = {'status': 'runner_error', 'reason': type(error).__name__, 'detail': str(error), 'scores': None}
    finally:
        save(out / 'source-after.json', source_manifest())
        save(out / 'execution-result.json', {'exit_code': exit_code, 'elapsed_seconds': time.monotonic() - started,
            'result': result, 'private_credentials_persisted': False})
    print(canonical({'exit_code': exit_code, 'out_dir': str(out), 'status': result['status'], 'scores': None}), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
