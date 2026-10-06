"""G3 原公共12/金融10串行采集；默认拒绝云调用，语义留待 Root 独立裁决。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import time

import public_probe_runner as public
import finance_probe_runner as finance

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_BASE = public.OUTPUT_ROOT / 'regression-cloud-runner'
CONTRACT_PATH = 'evaluation/window_integration/g3_regression_contract.json'
CONTRACT_SHA = '4015465fec56d0949bfa2f2da5fb24d6e3257ce84b5547154c50351c35c65a5e'
RUNNER_PATH = 'tests/window_integration/regression_cloud_runner.py'
VERSION = 'navren.g3.original-regression-controller.v1'
G2_SCRIPT = {'public': 'tests/window_integration/public_probe_runner.py',
             'finance': 'tests/window_integration/finance_probe_runner.py'}
G2_ORDER = {'public': ('P10', 'P02'), 'finance': finance.ORDER}
CAPS = {'public': 125, 'finance': 140}
LIMITS = {'wall_seconds': 105, 'model_rounds': 8, 'tool_attempts': 12,
          'primary_reviewer_tokens': 100000, 'native_output_max': 4000}


def finite(value, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def question_sha(question):
    return hashlib.sha256(question.encode()).hexdigest()


def binding(path):
    return {'path': str(Path(path).resolve()), 'sha256': public.sha(path)}


def bound_file(value, scope=None):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
        raise ValueError('evidence_binding_shape_invalid')
    raw = Path(value['path'])
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError('evidence_path_not_absolute_regular')
    path = raw.resolve()
    if scope is not None and not path.is_relative_to(Path(scope).resolve()):
        raise ValueError('evidence_outside_required_scope')
    if not path.is_file() or public.sha(path) != value['sha256']:
        raise ValueError('evidence_digest_missing_or_mismatch')
    return path


def flag(argv, name):
    if not isinstance(argv, list) or argv.count(name) != 1:
        raise ValueError('actual_argv_flag_missing_or_duplicate:' + name)
    index = argv.index(name)
    if index + 1 == len(argv) or not isinstance(argv[index+1], str) or argv[index+1].startswith('--'):
        raise ValueError('actual_argv_value_missing:' + name)
    return argv[index+1]


def load_corpus(candidate):
    candidate = Path(candidate).resolve()
    path = public.checked_file(candidate, CONTRACT_PATH)
    if public.sha(path) != CONTRACT_SHA:
        raise ValueError('g3_frozen_contract_changed')
    contract = finance.read_json(path)
    if (contract.get('schema') != 'navren.g3.original-regression.v1' or
            contract.get('concurrency') != 1 or contract.get('whole_hard_seconds') != 3000 or
            contract.get('serial_order') != ['public', 'finance'] or contract.get('task_limits') != LIMITS):
        raise ValueError('g3_frozen_limits_changed')
    selected = {}
    for family, count in (('public', 12), ('finance', 10)):
        spec = contract['families'][family]
        path = public.checked_file(candidate, spec['original_path'])
        required_sha = public.QUESTION_SHA if family == 'public' else finance.DATASET_SHA
        if (spec['original_sha256'] != required_sha or public.sha(path) != required_sha or
                spec['denominator'] != count or spec['child_hard_seconds'] != CAPS[family] or
                spec['stage_hard_seconds'] != 1500 or spec['task_wall_seconds'] != 105):
            raise ValueError('original_corpus_or_budget_changed:' + family)
        if family == 'finance' and spec['original_path'] != finance.DATASET_PATH:
            raise ValueError('derived_finance_input_is_not_original')
        rows = finance.read_json(path)['cases']
        if len(rows) != count or [row['id'] for row in rows] != spec['order'] or len(set(spec['order'])) != count:
            raise ValueError('original_corpus_order_count_changed:' + family)
        for row, expected in zip(rows, spec['case_identities']):
            question = row['question' if family == 'public' else 'prompt']
            identity = {'id': row['id'], 'question': question, 'question_sha256': question_sha(question),
                        'original_case_canonical_sha256': public.canonical_sha(row)}
            if identity != expected:
                raise ValueError('original_case_literal_changed:' + row['id'])
            if family == 'finance' and row['expected']['business_writes_allowed'] is not False:
                raise ValueError('original_finance_write_policy_changed')
        selected[family] = rows
    return contract, selected


def inspect_inputs(candidate, source_path, data_path):
    candidate = Path(candidate).resolve()
    inputs = finance.inspect_inputs(candidate, source_path, data_path)
    if inputs['sources'].get(RUNNER_PATH) != public.sha(__file__):
        raise ValueError('running_g3_controller_not_same_candidate_source')
    contract, cases = load_corpus(candidate)
    if inputs['sources'].get(CONTRACT_PATH) != CONTRACT_SHA:
        raise ValueError('g3_contract_not_in_full_source_manifest')
    support = inputs['contract'].formal_support_manifest(candidate, required=True)
    return {**inputs, 'g3_contract': contract, 'g3_cases': cases, 'support': support}


def same_maps(document, inputs):
    return all(document.get(key) == expected for key, expected in
               (('source_sha256', inputs['sources']), ('data_sha256', inputs['datasets']),
                ('auxiliary_sha256', public.AUXILIARY), ('formal_support_sha256', inputs['support'])))


def read_logs(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('actual_model_log_missing')
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def native_models(candidate):
    return {stage: spec['model_id'] for stage, spec in public.configured_model_contract(candidate).items()}


def validate_native(runtime, logs, metadata, reviews, review_status, models, turn_id=None, configured_models=None):
    """计数唯一来源为 runtime.model_calls；实际日志和原生响应回执仅用于核验。"""
    reasons, totals = [], {'primary': 0, 'reviewer': 0}
    runtime = runtime if isinstance(runtime, dict) else {}
    configured_models = public.MODEL_IDENTITIES if configured_models is None else configured_models
    prefix = public.audit_model_log_prefix(logs, turn_id=turn_id, configured_models=configured_models)
    identity = public.audit_response_identity(runtime, logs, metadata, turn_id=turn_id,
                                              configured_models=configured_models)
    audited = public.paid_usage_audit(runtime, logs, turn_id)
    reasons.extend(audited['errors'] + prefix['errors'] + identity['errors'])
    calls = runtime.get('model_calls')
    if not isinstance(calls, list) or not calls or not isinstance(logs, list) or len(logs) != len(calls):
        return {'stop_reasons': reasons + ['actual_native_call_log_missing'], 'actual_total_tokens': None,
                'actual_model_requests_sent': None, 'known_tokens_lower_bound': prefix['known_tokens_lower_bound']}
    known, sent = True, {'primary': 0, 'reviewer': 0}
    for call, log in zip(calls, logs):
        if not isinstance(call, dict) or call.get('stage') not in totals or not isinstance(log, dict):
            reasons.append('actual_native_call_shape_invalid'); known = False; continue
        stage = call['stage']
        usage = finance.counters(call.get('usage'))
        if usage is None or call.get('usage_complete') is not True:
            known = False
        else:
            totals[stage] += usage['total_tokens']
        if (call.get('billing_mode') != 'paid' or call.get('request_sent') is not True or
                type(call.get('attempts')) is not int or call['attempts'] != 1 or
                call.get('outcome') != 'completed' or call.get('error_code') or call.get('model_id') != models[stage] or
                log.get('model_id') != models[stage] or log.get('http_status') != 200 or
                log.get('outcome') != 'completed' or log.get('error_code') or
                log.get('stage') != ('conversation.tools' if stage == 'primary' else 'reviewer.conversation')):
            reasons.append('native_paid_chain_or_actual_log_invalid')
        # 原GLM日志不含usage；其实际usage来自review返回后runtime记录。
        if (stage == 'primary' or 'usage' in log) and finance.counters(log.get('usage')) != usage:
            reasons.append('actual_native_log_usage_mismatch'); known = False
        if log.get('finish_reason') == 'length' or call.get('error_code') == 'response_truncated' or log.get('error_code') == 'response_truncated':
            reasons.append('response_length_limit')
        sent[stage] += call.get('request_sent') is True
    total = sum(totals.values()) if known else None
    if (not known or runtime.get('usage_complete') is not True or
            audited.get('actual_total_tokens') != total or
            any(type(runtime.get(key)) is not int or runtime[key] != value for key, value in
                (('primary_tokens', totals['primary']), ('reviewer_tokens', totals['reviewer']), ('total_tokens', total)))):
        reasons.append('unknown_or_inconsistent_actual_usage'); total = None
    if total is not None and (runtime.get('budget_tokens') != total or total > 100000):
        reasons.append('actual_usage_budget_mismatch_or_exceeded')
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(stage), list) or
            len(metadata[stage]) != sent[stage] or any(not isinstance(row, dict) or not row.get('id') or
            not row.get('model') or 'offline' in str(row.get('model')).lower() for row in metadata[stage]) for stage in sent):
        reasons.append('original_native_response_identity_missing')
    if review_status == 'approved' and (sent['reviewer'] == 0 or not isinstance(reviews, list) or
            len(reviews) != sent['reviewer'] or any(not isinstance(row, dict) or
                row.get('decision', {}).get('verdict') not in ('approve', 'revise', 'reject', 'insufficient_evidence')
                or not isinstance(row.get('packet'), dict) for row in reviews) or reviews[-1]['decision']['verdict'] != 'approve'):
        reasons.append('actual_trace_reviewer_record_missing')
    review_calls = [call for call in calls if isinstance(call, dict) and call.get('stage') == 'reviewer']
    if (not isinstance(reviews, list) or len(reviews) != len(review_calls) or any(
            not isinstance(row, dict) or not isinstance(row.get('decision'), dict) or
            finance.counters(row['decision'].get('_model_usage')) != finance.counters(call.get('usage'))
            for row, call in zip(reviews, review_calls))):
        reasons.append('actual_trace_reviewer_usage_not_bound')
    if prefix['errors'] or identity['errors'] or any(reason in reasons for reason in ('unknown_usage', 'actual_model_log_usage_mismatch',
            'unknown_or_inconsistent_actual_usage', 'actual_native_log_usage_mismatch',
            'actual_trace_reviewer_usage_not_bound', 'native_paid_chain_or_actual_log_invalid',
            'original_native_response_identity_missing')):
        total = None
    # The log and runtime describe the same calls. Keep an authenticated prefix
    # when identity/log validation fails; never add the two observations.
    invalid_usage_binding = prefix['errors'] or identity['errors'] or any(reason in reasons for reason in
        ('actual_model_log_usage_mismatch', 'actual_native_log_usage_mismatch',
         'actual_trace_reviewer_usage_not_bound', 'native_paid_chain_or_actual_log_invalid',
         'actual_native_call_shape_invalid', 'paid_call_stage_invalid', 'paid_sent_admission_invalid',
         'actual_model_log_stage_or_outcome_mismatch', 'actual_model_log_attempts_mismatch'))
    known_lower = prefix['known_tokens_lower_bound'] if invalid_usage_binding else audited['known_tokens_lower_bound']
    return {'stop_reasons': list(dict.fromkeys(reasons)), 'actual_total_tokens': total,
            'primary_tokens': totals['primary'] if total is not None else None,
            'reviewer_tokens': totals['reviewer'] if total is not None else None,
            'actual_model_requests_sent': sum(sent.values()) if not invalid_usage_binding and all(isinstance(c, dict) and type(c.get('request_sent')) is bool for c in calls) else None,
            'known_tokens_lower_bound': known_lower,
            'provider_cost': None}


def validate_public(record, logs, case, candidate):
    reasons = public.stop_reasons(record, True)
    if (record.get('id') != case['id'] or record.get('question') != case['question'] or
            record.get('question_sha256') != question_sha(case['question']) or
            record.get('original_case_canonical_sha256') != public.canonical_sha(case) or record.get('mode') != 'cloud'):
        reasons.append('exact_original_public_case_missing')
    response = record.get('response') if isinstance(record.get('response'), dict) else {}
    runtime = response.get('agent_runtime') if isinstance(response.get('agent_runtime'), dict) else {}
    checked = validate_native(runtime, logs, record.get('response_metadata'), record.get('reviewer_records'),
                              response.get('agent_review', {}).get('status'), native_models(candidate), response.get('bank_response_id'))
    reasons.extend(checked['stop_reasons'])
    for key, limit in (('rounds', 8), ('tool_calls', 12), ('budget_tokens', 100000), ('duration_ms', 105000)):
        if type(runtime.get(key)) is not int or not 0 <= runtime[key] <= limit:
            reasons.append('invalid_or_exceeded_public_runtime:' + key)
    config = record.get('runtime_configuration', {})
    if (config.get('class') != 'local_knowledge.runtime.EvidenceRuntime' or config.get('background') is not False
            or config.get('reader') != 'cold_network_disabled' or config.get('real_prepare_round_path_preserved') is not True
            or type(config.get('prepare_round_invocations')) is not int or config['prepare_round_invocations'] <= 0):
        reasons.append('actual_evidence_runtime_prepare_path_missing')
    if (record.get('model_call_logs') != logs or record.get('model_call_log_path') is None or
            public.sha(record['model_call_log_path']) != record.get('model_call_log_sha256')):
        reasons.append('actual_public_raw_model_log_not_bound')
    for key in ('financial_before_path', 'financial_after_path'):
        path = Path(record.get(key, ''))
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            reasons.append('actual_financial_observer_file_missing')
        elif public.canonical_sha(finance.read_json(path)) != record.get(key.replace('_path', '_sha256')):
            reasons.append('actual_financial_observer_digest_mismatch')
    if record.get('financial_before_sha256') != record.get('financial_after_sha256'):
        reasons.append('actual_financial_observer_changed')
    trace = runtime.get('trace')
    if not isinstance(trace, list) or any(not isinstance(item, dict) or
            (item.get('status') == 'completed' and item.get('tool') in finance.FORBIDDEN_WRITES) for item in trace):
        reasons.append('unexpected_or_missing_business_write_trace')
    return {**checked, 'stop_reasons': list(dict.fromkeys(reasons)), 'semantic_judgment': None}


def validate_g2_execution(receipt, family, run_path, args, inputs, gate):
    if receipt.get('status') != 'exited' or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0:
        raise ValueError('g2_actual_outer_exit_not_zero:' + family)
    argv = receipt.get('argv')
    script = public.checked_file(args.candidate, G2_SCRIPT[family])
    # 本轮协议只有此正式CLI；重复/额外参数不能覆盖先前绑定。
    if (not isinstance(argv, list) or len(argv) != 16 or argv[1] != '-B' or
            Path(argv[2]).resolve() != script.resolve() or argv.count('--cloud') != 1 or
            public.sha(script) != inputs['sources'].get(G2_SCRIPT[family])):
        raise ValueError('g2_actual_cli_source_not_bound:' + family)
    for name, expected in (('--candidate', args.candidate), ('--source-manifest', args.source_manifest),
                           ('--data-manifest', args.data_manifest), ('--out-dir', run_path.parent)):
        actual = Path(flag(argv, name)).resolve()
        if actual != Path(expected).resolve():
            # 不同manifest文件可以同内容，但不能只比较一个不完整map。
            if name not in ('--source-manifest', '--data-manifest') or not actual.is_file() or public.sha(actual) != public.sha(expected):
                raise ValueError('g2_actual_cli_input_or_out_mismatch:' + name)
    archived_handoff = Path(flag(argv, '--handoff')).resolve()
    prior = public.cloud_gate(archived_handoff, flag(argv, '--handoff-sha256'),
                              inputs['sources'], inputs['datasets'], inputs['contract'])
    for key in ('g1_run_path', 'g1_run_sha256', 'g1_execution_path', 'g1_execution_sha256'):
        if prior[key] != gate[key]:
            raise ValueError('g2_different_g1_execution:' + family)
    return archived_handoff


def validate_finance_execution(execution, candidate, case, out, cases):
    expected = financial_command(candidate, case['id'], out, cases)
    if (not isinstance(execution.get('argv'), list) or execution['argv'][1:] != expected[1:]
            or Path(execution.get('cwd', '')).resolve() != Path(candidate).resolve()
            or execution.get('timeout_seconds') != 140):
        raise ValueError('original_finance_child_cli_not_exactly_bound')


def validate_g2(args, inputs, gate, tracked):
    handoff = finance.read_json(args.handoff)
    evidence = handoff.get('g2_evidence')
    if not isinstance(evidence, dict):
        raise ValueError('g2_independent_raw_evidence_not_provided')
    raw_bindings, rows = {}, {}
    for family in ('public', 'finance'):
        paths = {}
        for kind in ('run', 'execution'):
            key = family + '_' + kind
            value = {'path': evidence.get(key + '_path'), 'sha256': evidence.get(key + '_sha256')}
            path = bound_file(value, public.OUTPUT_ROOT)
            paths[kind] = path; raw_bindings[key] = binding(path); tracked[path] = public.sha(path)
        run, receipt = finance.read_json(paths['run']), finance.read_json(paths['execution'])
        archived = validate_g2_execution(receipt, family, paths['run'], args, inputs, gate)
        tracked[archived] = public.sha(archived)
        if run.get('source_sha256') != inputs['sources'] or run.get('data_sha256') != inputs['datasets']:
            raise ValueError('g2_source_or_data_not_same_candidate:' + family)
        status = 'COLLECTED_PENDING_INDEPENDENT_JUDGMENT' if family == 'public' else 'collected_pending_independent_judgment'
        if run.get('status') != status or type(run.get('returned_exit_code')) is not int or run['returned_exit_code'] != 0 or run.get('stop_reason'):
            raise ValueError('g2_collection_not_complete:' + family)
        records = run.get('records')
        if not isinstance(records, list) or [r.get('id') for r in records] != list(G2_ORDER[family]):
            raise ValueError('g2_exact_probe_denominator_missing:' + family)
        for record in records:
            case = next(c for c in inputs['g3_cases'][family] if c['id'] == record['id'])
            ident = family + ':' + case['id']
            if family == 'public':
                log_path = paths['run'].parent / case['id'] / 'model-calls.jsonl'
                checked = validate_public(record, read_logs(log_path), case, args.candidate)
                raw_record = paths['run'].parent / case['id'] / 'record.json'
                if finance.read_json(raw_record) != record:
                    raise ValueError('g2_public_record_file_not_same_as_collection')
                dependencies = [log_path, raw_record, Path(record['financial_before_path']), Path(record['financial_after_path'])]
            else:
                path = bound_file({'path': record.get('semantic_results_path'), 'sha256': record.get('semantic_results_sha256')}, paths['run'].parent)
                log_path = path.parent / 'model-calls.jsonl'
                if public.sha(log_path) != record.get('model_log_sha256'):
                    raise ValueError('g2_finance_raw_log_digest_mismatch')
                actual = path.parent.parent / 'process/execution.json'
                execution = finance.read_json(actual)
                if execution != record.get('execution'):
                    raise ValueError('g2_finance_actual_child_receipt_not_bound')
                validate_finance_execution(execution, args.candidate, case, path.parent, inputs['g3_cases']['finance'])
                raw, logs = finance.read_json(path), read_logs(log_path)
                checked = finance.validate_child(execution, raw, logs, case['id'], case['prompt'], inputs['sources'], inputs['datasets'], args.candidate)
                row = raw['records'][0]; result = row.get('result', {})
                native = validate_native(result.get('agent_runtime', {}), logs, row.get('response_metadata'), row.get('independent_reviews'),
                    result.get('agent_review', {}).get('status'), native_models(args.candidate), result.get('bank_response_id'))
                checked['stop_reasons'].extend(native['stop_reasons'])
                dependencies = [path, log_path, actual]
            if checked['stop_reasons'] or record.get('stop_reasons'):
                raise ValueError('g2_actual_native_or_technical_contract_failed:' + ident + ':' + ','.join(checked['stop_reasons']))
            rows[ident] = {'case': case, 'evidence_scope': paths['run'].parent,
                           'required_evidence': [binding(path) for path in dependencies]}
            for path in dependencies:
                tracked[path] = public.sha(path)
    judgment_path = bound_file({'path': evidence.get('independent_judgment_path'),
                               'sha256': evidence.get('independent_judgment_sha256')}, public.OUTPUT_ROOT)
    tracked[judgment_path] = public.sha(judgment_path)
    judgment = finance.read_json(judgment_path)
    if (judgment.get('schema') != 'navren.root.g2.independent-judgment.v1' or judgment.get('attested_by') != 'root'
            or judgment.get('root_verified') is not True or not same_maps(judgment, inputs)
            or judgment.get('raw_evidence') != raw_bindings):
        raise ValueError('root_g2_judgment_not_bound_to_exact_raw_artifacts')
    entries = judgment.get('cases')
    if not isinstance(entries, list) or len(entries) != len(rows) or len({(r.get('family'), r.get('id')) for r in entries}) != len(rows):
        raise ValueError('root_g2_judgment_missing_exact_six_cases')
    for entry in entries:
        key = str(entry.get('family')) + ':' + str(entry.get('id'))
        if key not in rows:
            raise ValueError('root_g2_judgment_wrong_case')
        row = rows[key]
        if (entry.get('semantic_verdict') != 'pass' or not isinstance(entry.get('rationale'), str) or not entry['rationale'].strip()
                or entry.get('original_case_canonical_sha256') != public.canonical_sha(row['case'])
                or entry.get('question_sha256') != question_sha(row['case'].get('question', row['case'].get('prompt')))):
            raise ValueError('root_g2_case_not_independently_passed:' + key)
        refs = entry.get('evidence')
        if not isinstance(refs, list) or not refs or any(ref not in refs for ref in row['required_evidence']):
            raise ValueError('root_g2_case_actual_logs_or_observer_not_bound:' + key)
        for ref in refs:
            path = bound_file(ref, row['evidence_scope']); tracked[path] = public.sha(path)
    return {'raw_evidence': raw_bindings, 'independent_judgment': binding(judgment_path),
            'independently_passed_case_ids': list(rows)}


def budget_gate(handoff, inputs, tracked):
    path = bound_file({'path': handoff.get('g3_budget_precheck_path'),
                       'sha256': handoff.get('g3_budget_precheck_sha256')}, public.OUTPUT_ROOT)
    tracked[path] = public.sha(path)
    data = finance.read_json(path)
    if (data.get('schema') != 'navren.root.g3.budget-admission.v1' or data.get('attested_by') != 'root'
            or data.get('root_verified') is not True or not same_maps(data, inputs)
            or data.get('task_limits') != LIMITS or data.get('child_hard_seconds') != CAPS
            or data.get('family_hard_seconds') != {'public': 1500, 'finance': 1500}
            or data.get('whole_hard_seconds') != 3000 or data.get('completion_guaranteed') is not False):
        raise ValueError('g3_budget_precheck_not_same_source_or_limits')
    ref = data.get('public_fresh_process_observation')
    observed_path = bound_file(ref, public.OUTPUT_ROOT)
    tracked[observed_path] = public.sha(observed_path)
    observed = finance.read_json(observed_path)
    # 真实source-bound免费准备观测可供Root计入上界；固定105从进入execute_case开始不重置。
    if (not same_maps(observed, inputs) or observed.get('billing_mode') != 'unpaid_fixture'
            or observed.get('measurement_scope') != 'public_fresh_process_startup_prepare_cleanup'
            or observed.get('actual_execution', {}).get('status') != 'exited'
            or type(observed.get('actual_execution', {}).get('exit_code')) is not int
            or observed['actual_execution']['exit_code'] != 0
            or observed.get('real_evidence_runtime_prepare_path') is not True
            or observed.get('environment_set_before_business_import') is not True
            or observed.get('cleanup_completed') is not True
            or not finite(observed.get('non_task_overhead_seconds')) or observed['non_task_overhead_seconds'] > 20
            or data.get('public_non_task_overhead_seconds') != observed['non_task_overhead_seconds']):
        raise ValueError('public_125_cap_dependency_not_observed')
    actual = bound_file(observed.get('actual_execution_receipt'), public.OUTPUT_ROOT)
    if finance.read_json(actual) != observed['actual_execution']:
        raise ValueError('public_budget_actual_execution_not_bound')
    tracked[actual] = public.sha(actual)
    return {'precheck': binding(path), 'public_fresh_process_observation': binding(observed_path),
            'arithmetic': {'public_12x125': 1500, 'finance_10x140': 1400, 'whole_child_sum': 2900},
            'completion_guaranteed': False, 'admission_only': True}


def cloud_gate(args, inputs, tracked):
    if args.cloud is not True:
        raise ValueError('explicit_cloud_required')
    gate = public.cloud_gate(args.handoff, args.handoff_sha256, inputs['sources'], inputs['datasets'], inputs['contract'])
    # 同内容桥接不要求两个物理candidate路径相等，所有完整map必须实际吻合。
    g1 = finance.read_json(gate['g1_run_path'])
    if g1.get('formal_support_sha256') != inputs['support']:
        raise ValueError('g1_g3_support_not_same_candidate')
    for key in ('g1_run_path', 'g1_execution_path'):
        tracked[Path(gate[key])] = public.sha(gate[key])
    tracked[Path(args.handoff)] = args.handoff_sha256
    g2 = validate_g2(args, inputs, gate, tracked)
    budget = budget_gate(finance.read_json(args.handoff), inputs, tracked)
    return {'g1': gate, 'g2': g2, 'budget': budget}


def unchanged(inputs, candidate, tracked):
    try:
        return (inputs['contract'].source_manifest(candidate) == inputs['sources']
                and inputs['contract'].data_manifest(candidate) == inputs['datasets']
                and inputs['contract'].auxiliary_manifest(candidate) == public.AUXILIARY
                and inputs['contract'].formal_support_manifest(candidate, required=True) == inputs['support']
                and all(not Path(path).is_symlink() and public.sha(path) == digest for path, digest in tracked.items()))
    except (OSError, ValueError):
        return False


def financial_command(candidate, ident, out, cases):
    if ident not in [case['id'] for case in cases]:
        raise ValueError('not_original_frozen_finance_case')
    return [sys.executable, '-B', str(Path(candidate) / finance.ADAPTER_PATH), '--dataset',
            str(Path(candidate) / finance.DATASET_PATH), '--out-dir', str(out), '--case-ids', ident]


def public_command(args, ident, out):
    return [sys.executable, '-B', str(Path(args.candidate) / RUNNER_PATH),
            '--candidate', str(args.candidate), '--source-manifest', str(args.source_manifest),
            '--data-manifest', str(args.data_manifest), '--out-dir', str(out), '--cloud',
            '--handoff', str(args.handoff), '--handoff-sha256', args.handoff_sha256,
            '--public-child', ident]


def collected_file(path, scope):
    """Bind an actual child-output file without following a redirected path."""
    path, scope = Path(path), Path(scope).resolve()
    if (not path.is_absolute() or not path.resolve().is_relative_to(scope)
            or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(scope))
            or not path.is_file()):
        raise ValueError('actual_child_file_outside_scope_symlink_or_missing')
    return path


def collected_logs(path, scope):
    """Read an actual child-output prefix independently of its terminal result."""
    path = collected_file(path, scope)
    logs = public.read_model_log_prefix(path)
    if logs is None:
        raise ValueError('actual_model_log_missing')
    return logs, binding(path)


def result_turn(raw, family):
    if not isinstance(raw, dict):
        return None
    if family == 'public':
        response = raw.get('response')
    else:
        rows = raw.get('records')
        response = rows[0].get('result') if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    turn = response.get('bank_response_id') if isinstance(response, dict) else None
    return turn if isinstance(turn, str) and 1 <= len(turn) <= 256 else None


def execute_case(args, family, case, inputs, out):
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    configured_models = public.configured_model_contract(args.candidate)
    with tempfile.TemporaryDirectory(prefix='navren-g3-' + family + '-state-') as temp:
        state = Path(temp).resolve()
        env = inputs['contract'].isolated_environment(Path(args.candidate), state)
        env.update(NAVREN_SIM_DB=str(state/'bank.sqlite3'), QINGZHOU_SIM_DB=str(state/'bank.sqlite3'),
                   NAVREN_KNOWLEDGE_DB=str(state/'knowledge.sqlite3'), NAVREN_MARKET_DB=str(state/'market.sqlite3'),
                   NAVREN_ALLOW_CLOUD_ASR='0', NAVREN_AGENT_TIMEOUT='105', TMPDIR=str(state))
        path_keys = ('NAVREN_SIM_DB', 'QINGZHOU_SIM_DB', 'NAVREN_KNOWLEDGE_DB',
                     'NAVREN_MARKET_DB', 'NAVREN_FUND_CACHE', 'TMPDIR')
        dispatch_env = env.copy()
        if family == 'finance':
            # 原 CLI 拒绝知识库覆盖，并由每题显式临时 bank 派生知识库。
            dispatch_env.pop('NAVREN_KNOWLEDGE_DB', None)
        isolation = {'paths': {key: dispatch_env[key] for key in path_keys if key in dispatch_env},
                     'paths_scope': 'actual_dispatch_environment_only',
                     'controller_fallback_paths': {key: env[key] for key in path_keys},
                     'knowledge_database_selection': ('original_cli_case_bank_sibling' if family == 'finance'
                                                       else 'dispatch_environment_override'),
                     'actual_database_paths_evidence': 'business_isolation.paths_from_child_record', 'workspace_id': None,
                     'private_key_storage': 'temporary_process_environment_only', 'secret_values_recorded': False,
                     'environment_set_before_business_import': True, 'cloud_audio': False}
        public.atomic_json(out/'controller-isolation.json', isolation)
        if family == 'public':
            result_out = out / 'public'
            command = public_command(args, case['id'], result_out)
        else:
            result_out = out / 'semantic'
            dispatch_env['NAVREN_MODEL_CALL_LOG'] = str(result_out/'model-calls.jsonl')
            command = financial_command(args.candidate, case['id'], result_out, inputs['g3_cases']['finance'])
        execution = inputs['contract'].execute(command, args.candidate, out/'process', CAPS[family], dispatch_env)
    result_path = result_out/case['id']/'record.json' if family == 'public' else result_out/'semantic-results.json'
    log_path = result_out/case['id']/'model-calls.jsonl' if family == 'public' else result_out/'model-calls.jsonl'
    logs, log_binding, log_errors = None, None, []
    try:
        logs, log_binding = collected_logs(log_path, out)
    except (OSError, ValueError, TypeError) as error:
        log_errors.append('actual_model_log_missing_or_invalid:' + type(error).__name__)
    prefix = public.audit_model_log_prefix(logs, configured_models=configured_models)
    checked = {'stop_reasons': [], 'actual_total_tokens': None,
               'actual_model_requests_sent': None, 'known_tokens_lower_bound': prefix['known_tokens_lower_bound']}
    try:
        raw = finance.read_json(collected_file(result_path, out))
        prefix = public.audit_model_log_prefix(logs, turn_id=result_turn(raw, family), configured_models=configured_models)
        checked['known_tokens_lower_bound'] = prefix['known_tokens_lower_bound']
        if family == 'public':
            checked = validate_public(raw, logs, case, args.candidate)
            checked['business_isolation'] = {'workspace_id': raw.get('workspace_id'),
                                             'paths': raw.get('isolated_paths')}
            task_receipt = finance.read_json(result_out/'task-receipt.json')
            if (task_receipt.get('complete_prepare_http_cleanup') is not True or
                    not finite(task_receipt.get('duration_seconds')) or task_receipt['duration_seconds'] > 105):
                checked['stop_reasons'].append('public_full_task_prepare_http_cleanup_exceeded_105')
        else:
            checked = finance.validate_child(execution, raw, logs, case['id'], case['prompt'], inputs['sources'], inputs['datasets'], args.candidate)
            # Until the complete native chain is bound, only its legal log prefix
            # is evidence for the lower bound, including on an empty-row result.
            checked['known_tokens_lower_bound'] = prefix['known_tokens_lower_bound']
            row = raw['records'][0]; result = row.get('result', {})
            checked['business_isolation'] = {'workspace_id': row.get('workspace_id'), 'paths': row.get('database_paths')}
            native = validate_native(result.get('agent_runtime', {}), logs, row.get('response_metadata'), row.get('independent_reviews'),
                result.get('agent_review', {}).get('status'), native_models(args.candidate), result.get('bank_response_id'), configured_models)
            checked['stop_reasons'].extend(native['stop_reasons'])
            checked['known_tokens_lower_bound'] = native['known_tokens_lower_bound']
            if native['actual_total_tokens'] is None:
                for key in ('actual_total_tokens', 'primary_tokens', 'reviewer_tokens'):
                    checked[key] = None
            if native['actual_model_requests_sent'] is None:
                checked['actual_model_requests_sent'] = None
            validate_finance_execution(execution, args.candidate, case, result_out, inputs['g3_cases']['finance'])
        checked.update(raw_result=binding(result_path))
        actual_isolation = checked.get('business_isolation', {})
        actual_paths = actual_isolation.get('paths')
        if (not isinstance(actual_isolation.get('workspace_id'), str) or not actual_isolation['workspace_id'] or
                not isinstance(actual_paths, dict) or not actual_paths or any(not Path(path).resolve().is_relative_to(state)
                or Path(path).exists() for key, path in actual_paths.items()
                if key.endswith(('_DB', '_CACHE')) or key in ('personal_database', 'bank', 'personal', 'market', 'knowledge'))):
            checked['stop_reasons'].append('actual_business_storage_not_isolated_or_cleaned')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        checked.setdefault('stop_reasons', []).append('actual_child_result_missing_or_invalid')
        checked.update(error_type=type(error).__name__, actual_total_tokens=None, actual_model_requests_sent=None)
    # Runtime and the raw log observe the same calls: max, never their sum.
    lower_bounds = [v for v in (checked.get('known_tokens_lower_bound'), prefix['known_tokens_lower_bound'])
                    if type(v) is int and v >= 0]
    checked['known_tokens_lower_bound'] = max(lower_bounds) if lower_bounds else None
    checked['model_log_prefix_audit'] = prefix
    if log_binding is not None:
        checked['model_log'] = log_binding
    checked['stop_reasons'].extend(log_errors + prefix['errors'])
    if execution.get('status') != 'exited' or type(execution.get('exit_code')) is not int or execution['exit_code'] != 0:
        checked['stop_reasons'].append('actual_child_not_exited_zero')
    if not finite(execution.get('duration_seconds')) or execution['duration_seconds'] > CAPS[family]:
        checked['stop_reasons'].append('actual_child_hardcap_exceeded')
    receipt_binding = None
    try:
        receipt_path = collected_file(out/'process/execution.json', out)
        if finance.read_json(receipt_path) != execution:
            raise ValueError('actual_child_execution_receipt_not_bound')
        receipt_binding = binding(receipt_path)
    except (OSError, ValueError, TypeError) as error:
        checked['stop_reasons'].append('actual_child_execution_receipt_missing_or_invalid')
        checked.update(error_type=type(error).__name__, actual_total_tokens=None, actual_model_requests_sent=None)
    checked.update(id=case['id'], family=family, execution=execution,
        execution_receipt=receipt_binding, original_case_canonical_sha256=public.canonical_sha(case),
        question_sha256=question_sha(case.get('question', case.get('prompt'))), task_wall_seconds=105,
        child_hard_seconds=CAPS[family], isolation= isolation, temporary_state_removed=not state.exists(),
        oracle_sent_to_model=False, semantic_judgment=None, actual_model_cost=None)
    checked['stop_reasons'] = list(dict.fromkeys(checked['stop_reasons']))
    checked['status'] = 'stopped' if checked['stop_reasons'] else 'collected_pending_independent_judgment'
    public.atomic_json(out/'record.json', checked)
    return checked


def account_usage(records, *, paid):
    attempted = [r for r in records if r['status'] != 'not_run']
    def measure(key):
        values = [r.get(key) for r in attempted]
        return sum(values) if paid and attempted and all(type(v) is int and v >= 0 for v in values) else None
    return {'actual_total_tokens': measure('actual_total_tokens'), 'actual_model_requests_sent': measure('actual_model_requests_sent'),
            'known_tokens_lower_bound': sum(r['known_tokens_lower_bound'] for r in attempted
                if type(r.get('known_tokens_lower_bound')) is int), 'actual_model_cost': None,
            'usage_scope': 'each_actual_runtime_call_once' if paid else 'unpaid_protocol_harness_no_provider_usage'}


def collect_serial(plan, report, *, started, execute, guard, save, paid=True,
                   stage_seconds=1500, whole_seconds=3000, caps=None):
    """通用串行控制层；免费协议测试使用真实进程，不能成为云语义成绩。"""
    caps = CAPS if caps is None else caps
    stop = None
    for phase, (family, cases) in enumerate(plan):
        stage_started = started if phase == 0 else time.monotonic()
        group = report['families'][family]
        for index, case in enumerate(cases):
            if not guard():
                stop = ['same_candidate_or_evidence_drift_before_next_child']; break
            # guard/取门耗时已计入真实剩余；cap不得按剩余缩短，也不得重置105。
            if min(stage_seconds-(time.monotonic()-stage_started), whole_seconds-(time.monotonic()-started)) < caps[family]:
                stop = ['not_run_budget_dependency']; break
            group['records'][index].update(status='running', actual_total_tokens=None, actual_model_requests_sent=None)
            save()
            try:
                record = execute(family, case)
            except Exception as error:
                record = {'id': case['id'], 'status': 'stopped', 'stop_reasons': ['child_launch_or_collection_failure'],
                          'error_type': type(error).__name__, 'actual_total_tokens': None,
                          'actual_model_requests_sent': None, 'semantic_judgment': None}
            group['records'][index] = record
            if paid and any(type(record.get(key)) is not int or record[key] < 0
                            for key in ('actual_total_tokens', 'actual_model_requests_sent')):
                record.setdefault('stop_reasons', []).append('unknown_actual_provider_usage')
            if not guard():
                record.setdefault('stop_reasons', []).append('same_candidate_or_evidence_drift_after_child')
            if time.monotonic()-stage_started > stage_seconds or time.monotonic()-started > whole_seconds:
                record.setdefault('stop_reasons', []).append('actual_stage_or_whole_deadline_exceeded')
            if record.get('stop_reasons') or record.get('status') != 'collected_pending_independent_judgment':
                record['status'] = 'stopped'; stop = record.get('stop_reasons') or ['actual_collection_incomplete']; save(); break
            save()
        group['duration_seconds'] = time.monotonic()-stage_started
        if stop:
            break
    for group in report['families'].values():
        for record in group['records']:
            if record['status'] == 'not_run':
                record['reason'] = 'not_run_budget_dependency' if stop == ['not_run_budget_dependency'] else 'prior_stop_or_gate_dependency'
        group['attempted_count'] = sum(r['status'] != 'not_run' for r in group['records'])
        group['collected_count'] = sum(r['status'] == 'collected_pending_independent_judgment' for r in group['records'])
        group['semantic_pass_count'] = None
        group['usage'] = account_usage(group['records'], paid=paid)
    all_records = [r for g in report['families'].values() for r in g['records']]
    report.update(stop_reasons=stop, actual_usage=account_usage(all_records, paid=paid),
                  duration_seconds=time.monotonic()-started,
                  status='stopped' if stop else 'collected_pending_independent_judgment' if paid else 'unpaid_controller_protocol_completed',
                  semantic_judgment=None, returned_exit_code=1 if stop else 0)
    save()
    return report['returned_exit_code']


def initial_report(cases, *, paid=True):
    return {'schema': VERSION, 'status': 'not_run', 'billing_mode': 'paid' if paid else 'unpaid_fixture',
            'source_semantic_claim': 'NONE', 'families': {family: {'denominator': len(rows), 'order': [c['id'] for c in rows],
            'records': [{'id': c['id'], 'status': 'not_run', 'reason': 'gate_not_verified', 'semantic_judgment': None,
                         'actual_total_tokens': None, 'actual_model_requests_sent': None} for c in rows]} for family, rows in cases.items()},
            'concurrency': 1, 'task_wall_seconds': 105, 'stage_hard_seconds': 1500,
            'whole_hard_seconds': 3000, 'child_hard_seconds': CAPS, 'semantic_judgment': None,
            'actual_model_cost': None, 'returned_exit_code': 2}


def public_child(args):
    # 每题新OS进程，先验完整cloud门；没有隐式免费转付费或绕过ORDER。
    out = Path(args.out_dir).resolve()
    if not out.is_relative_to(OUTPUT_BASE.resolve()) or out == OUTPUT_BASE.resolve():
        raise ValueError('g3_child_output_outside_authorized_scope')
    if any(not Path(path).resolve().is_relative_to(public.OUTPUT_ROOT.resolve()) for path in
           (args.candidate, args.source_manifest, args.data_manifest, args.handoff)):
        raise ValueError('g3_child_inputs_must_be_frozen_under_integration')
    out.mkdir(parents=True, exist_ok=False)
    inputs = inspect_inputs(args.candidate, args.source_manifest, args.data_manifest)
    tracked = {Path(args.source_manifest): public.sha(args.source_manifest), Path(args.data_manifest): public.sha(args.data_manifest)}
    cloud_gate(args, inputs, tracked)
    case = next((case for case in inputs['g3_cases']['public'] if case['id'] == args.public_child), None)
    if case is None or not unchanged(inputs, args.candidate, tracked):
        raise ValueError('g3_public_child_case_or_source_dependency')
    started = time.monotonic()
    record = public.execute_case(Path(args.candidate).resolve(), case, inputs, out, True, 105)
    elapsed = time.monotonic()-started  # execute_case finally 已完成真实服务/reader/线程/临时库清理。
    receipt = {'id': case['id'], 'duration_seconds': elapsed, 'task_wall_seconds': 105,
               'complete_prepare_http_cleanup': elapsed <= 105,
               'source_data_aux_support_evidence_unchanged': unchanged(inputs, args.candidate, tracked)}
    public.atomic_json(out/'task-receipt.json', receipt)
    return 0 if elapsed <= 105 and receipt['source_data_aux_support_evidence_unchanged'] and not record['stop_reasons'] else 1


def run(args):
    started = time.monotonic()  # 包含mkdir、读取输入、取门与全部子进程，不由preflight后重置。
    out = Path(args.out_dir).resolve()
    if not out.is_relative_to(OUTPUT_BASE.resolve()) or out == OUTPUT_BASE.resolve():
        raise ValueError('g3_output_outside_authorized_scope')
    out.mkdir(parents=True, exist_ok=False)
    report = {'schema': VERSION, 'status': 'not_run_gate_dependency', 'families': {},
              'actual_usage': None, 'actual_model_cost': None, 'semantic_judgment': None, 'returned_exit_code': 2}
    try:
        if args.cloud is not True:
            raise ValueError('explicit_cloud_required')
        candidate = Path(args.candidate).resolve()
        if any(not Path(path).resolve().is_relative_to(public.OUTPUT_ROOT.resolve()) for path in
               (candidate, args.source_manifest, args.data_manifest, args.handoff)):
            raise ValueError('candidate_must_be_frozen_under_integration')
        inputs = inspect_inputs(candidate, args.source_manifest, args.data_manifest)
        report = initial_report(inputs['g3_cases'])
        tracked = {Path(args.source_manifest): public.sha(args.source_manifest), Path(args.data_manifest): public.sha(args.data_manifest)}
        gate = cloud_gate(args, inputs, tracked)
        report.update(candidate_root=str(candidate), source_sha256=inputs['sources'], data_sha256=inputs['datasets'],
                      auxiliary_sha256=public.AUXILIARY, formal_support_sha256=inputs['support'],
                      source_manifest=binding(args.source_manifest), data_manifest=binding(args.data_manifest),
                      original_corpora={family: binding(candidate/spec['original_path']) for family, spec in inputs['g3_contract']['families'].items()},
                      frozen_contract=binding(candidate/CONTRACT_PATH), gate=gate, status='running')
        def guard():
            try:
                public.cloud_gate(args.handoff, args.handoff_sha256, inputs['sources'], inputs['datasets'], inputs['contract'])
                return unchanged(inputs, candidate, tracked)
            except (OSError, ValueError, TypeError, KeyError):
                return False
        def save():
            for family, group in report['families'].items():
                for record in group['records']:
                    path = out/family/record['id']/'record.json'
                    if path.is_file() and record.get('execution'):
                        public.atomic_json(path, record)
            public.atomic_json(out/'results.json', report)
        collect_serial([(family, inputs['g3_cases'][family]) for family in ('public', 'finance')], report, started=started,
            execute=lambda family, case: execute_case(args, family, case, inputs, out/family/case['id']), guard=guard,
            save=save)
    except Exception as error:
        report.update(status='not_run_gate_dependency' if not any(r['status'] != 'not_run' for g in report['families'].values()
            for r in g['records']) else 'stopped', error={'type': type(error).__name__, 'code': str(error)[:240]}, returned_exit_code=2)
    finally:
        report['duration_seconds'] = time.monotonic()-started
        if report['duration_seconds'] > 3000:
            report.update(status='stopped', stop_reasons=['whole_deadline_exceeded'], returned_exit_code=1)
        public.atomic_json(out/'results.json', report)
    return report['returned_exit_code']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--data-manifest', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--handoff', type=Path)
    parser.add_argument('--handoff-sha256')
    parser.add_argument('--public-child')
    args = parser.parse_args()
    return public_child(args) if args.public_child else run(args)


if __name__ == '__main__':
    raise SystemExit(main())
