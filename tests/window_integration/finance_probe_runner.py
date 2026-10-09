"""G2 原金融 S01→S02→S03→S10 顺序采集门；默认不运行任何原题。

只有明确cloud及手工核验SHA的同候选G1交接才能启动原adapter CLI。
原裁判留在旧runner本地，不生成模型工具/查询/答案；语义另行裁决。
"""
from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
import sys
import tempfile
import time

import public_probe_runner as public

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_BASE = public.OUTPUT_ROOT / 'finance-probe-runner'
VERSION = 'navren.integration.finance-probe-runner.v1'
CONTRACT_PATH = 'evaluation/window_integration/g2_probe_contract.json'
CONTRACT_SHA = 'f320e6f1abaf0ac3c0a0c0cb425c4594d880ea4f15437263c3becb82aa590d76'
DATASET_PATH = 'evaluation/navren_skill_semantic_cases.json'
DATASET_SHA = 'f92627f6dcd96a979d17d805dc5a219a47fcacc0a9e647c6f6e1a421f62ec0e6'
LEGACY_PATH = 'scripts/run_skill_acceptance.py'
ADAPTER_PATH = 'tests/window_integration/semantic_runner_adapter.py'
ORDER = ('S01_emergency_floor_once', 'S02_future_zero_not_missing_history',
         'S03_buffer_adequacy_unknown', 'S10_linked_reserve_dedup')
CASE_SHA = {
    ORDER[0]: '157b9533d967af36f3dd0c5dca508a550d0dc51dc4b35aca176d182cb668f7f0',
    ORDER[1]: '6659e156df87821f2b2e4d011927f84db43813a139aec26aa9706965e8127272',
    ORDER[2]: 'fdff372c2530830cd6715c60fdd583e572bcb29a68b712039315d94da5e53eec',
    ORDER[3]: '758dd94e9b74e3eb435cbc690dddc815664f97c158de709049023ae72cbb2cdf',
}
LIMITS = {'wall_seconds': 105, 'primary_reviewer_tokens': 100000, 'native_output_max': 4000,
          'model_rounds': 8, 'tool_attempts': 12}
STAGE_SECONDS, CHILD_SECONDS = 600, 140
FORBIDDEN_WRITES = {'prepare_operation', 'withdraw_draft', 'calendar_prepare_event',
                    'remember_profile', 'service_offer_preview'}


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))


def lock_cases(contract, dataset):
    if (contract.get('financial_probe_order') != list(ORDER) or contract.get('per_task_limits') != LIMITS or
            contract.get('serial_stage_limits') != {'public_seconds': 300, 'finance_seconds': 600, 'concurrency': 1}):
        raise ValueError('frozen_order_or_budget_changed')
    selected = []
    for ident in ORDER:
        rows = [row for row in dataset.get('cases', []) if row.get('id') == ident]
        probes = [row for row in contract.get('financial_probes', []) if row.get('id') == ident]
        if (len(rows) != 1 or len(probes) != 1 or public.canonical_sha(rows[0]) != CASE_SHA[ident] or
                probes[0]['original_case_canonical_sha256'] != CASE_SHA[ident] or
                rows[0]['prompt'] != probes[0]['prompt'] or rows[0]['fixture'] != 'default_temp' or
                rows[0]['expected']['business_writes_allowed'] is not False):
            raise ValueError('original_finance_case_changed:' + ident)
        selected.append(rows[0])
    return selected


def check_native_budget(candidate):
    # 不导入业务。固定原CLI实际调用的默认额度，不以报告自行声明放宽预算。
    tree = ast.parse(public.checked_file(candidate, 'agent_runtime/conversation.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ConversationAgent')
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    defaults = {arg.arg: ast.literal_eval(value) for arg, value in zip(init.args.kwonlyargs, init.args.kw_defaults)}
    if any(defaults.get(k) != v for k, v in {'max_rounds': 8, 'max_tools': 12, 'max_tokens': 100000, 'timeout_seconds': None}.items()):
        raise ValueError('native_agent_default_budget_changed')
    native = ast.parse(public.checked_file(candidate, 'agent_runtime/native_client.py').read_text())
    cls = next(n for n in native.body if isinstance(n, ast.ClassDef) and n.name == 'DeepSeekToolClient')
    outputs = [ast.literal_eval(n.value) for n in cls.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'output_token_limit' for t in n.targets)]
    payload = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'request_payload')
    token_values = [ast.literal_eval(value) for n in ast.walk(payload) if isinstance(n, ast.Dict)
                    for key, value in zip(n.keys, n.values) if isinstance(key, ast.Constant) and key.value == 'max_tokens']
    if outputs != [4000] or token_values != [4000]:
        raise ValueError('native_primary_output_limit_changed')


def inspect_inputs(candidate, source_path, data_path):
    inputs = public.inspect_inputs(candidate, source_path, data_path)
    candidate = Path(candidate).resolve()
    if inputs['sources'].get('tests/window_integration/finance_probe_runner.py') != public.sha(__file__):
        raise ValueError('running_finance_adapter_not_same_frozen_source')
    paths = {name: public.checked_file(candidate, name) for name in (CONTRACT_PATH, DATASET_PATH, LEGACY_PATH, ADAPTER_PATH)}
    for name, expected in ((CONTRACT_PATH, CONTRACT_SHA), (DATASET_PATH, DATASET_SHA)):
        if public.sha(paths[name]) != expected:
            raise ValueError('frozen_finance_input_changed:' + name)
    for name in (LEGACY_PATH, ADAPTER_PATH):
        if inputs['sources'].get(name) != public.sha(paths[name]):
            raise ValueError('original_cli_not_bound_to_same_g1_source:' + name)
    contract, dataset = read_json(paths[CONTRACT_PATH]), read_json(paths[DATASET_PATH])
    cases = lock_cases(contract, dataset)
    check_native_budget(candidate)
    public.configured_model_contract(candidate)
    return {**inputs, 'finance_cases': cases, 'finance_contract': contract,
            'finance_paths': paths, 'finance_input_sha256': {name: public.sha(path) for name, path in paths.items()}}


def require_cloud(args, inputs):
    if args.cloud is not True:
        raise ValueError('explicit_cloud_required')
    return public.cloud_gate(args.handoff, args.handoff_sha256, inputs['sources'], inputs['datasets'], inputs['contract'])


def case_command(candidate, ident, out, interpreter=None):
    if ident not in ORDER:
        raise ValueError('unapproved_finance_case_id')
    return [str(interpreter or sys.executable), '-B', str(Path(candidate) / ADAPTER_PATH),
            '--dataset', str(Path(candidate) / DATASET_PATH), '--out-dir', str(Path(out)), '--case-ids', ident]


def counters(usage):
    if not isinstance(usage, dict):
        return None
    values = {key: usage.get(key) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
    if any(type(v) is not int or v < 0 for v in values.values()):
        return None
    return values if values['prompt_tokens'] + values['completion_tokens'] == values['total_tokens'] else None


def lower_bound(usage):
    value = public.usage_lower_bound(usage)
    return value if value is not None else 0


def validate_child(execution, report, logs, ident, prompt, sources, datasets, candidate):
    """实测usage计一次；旧collector成功或普通语义错误均不替代技术判据。"""
    reasons = []
    if execution.get('status') != 'exited' or type(execution.get('exit_code')) is not int or execution['exit_code'] != 0:
        reasons.append('child_did_not_exit_zero')
    elapsed = execution.get('duration_seconds')
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= CHILD_SECONDS:
        reasons.append('child_elapsed_missing_or_over_hard_limit')
    prefix = public.audit_model_log_prefix(logs, configured_models=public.MODEL_IDENTITIES)
    def incomplete_record(code):
        return {'stop_reasons': list(dict.fromkeys(reasons + [code] + prefix['errors'])),
                'actual_total_tokens': None, 'known_tokens_lower_bound': prefix['known_tokens_lower_bound'],
                'actual_model_requests_sent': None, 'semantic_judgment': None,
                'model_log_prefix_audit': prefix}
    if not isinstance(report, dict):
        return incomplete_record('semantic_results_missing_or_invalid')
    if (report.get('offline') is not False or report.get('process_preflight') or
            report.get('tested_source_root') != str(Path(candidate).resolve()) or
            report.get('selected_case_ids') != [ident] or type(report.get('expected_case_count')) is not int or report.get('expected_case_count') != 1 or
            report.get('collection_complete') is not True or report.get('complete') is not True or
            report.get('run_status') != 'collection_complete' or report.get('runner_failure') or report.get('stop_reason')):
        reasons.append('original_collection_contract_failed')
    child_sources = report.get('source_sha256')
    if (not isinstance(child_sources, dict) or not child_sources or
            report.get('final_source_sha256') != child_sources or report.get('source_unchanged') is not True or
            report.get('dataset_unchanged') is not True or report.get('invalidated') is not False or
            report.get('dataset_sha256') != DATASET_SHA or report.get('final_dataset_sha256') != DATASET_SHA or
            any(digest != sources.get(name, datasets.get(name)) for name, digest in child_sources.items()) or
            not sources.get(LEGACY_PATH) or child_sources.get(LEGACY_PATH) != sources[LEGACY_PATH] or
            child_sources.get(DATASET_PATH) != DATASET_SHA):
        reasons.append('child_input_drift_or_manifest_missing')
    rows = report.get('records')
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get('id') != ident or rows[0].get('question') != prompt):
        return incomplete_record('exact_case_record_missing')
    row = rows[0]
    result = row.get('result') if isinstance(row.get('result'), dict) else {}
    runtime = result.get('agent_runtime') if isinstance(result.get('agent_runtime'), dict) else {}
    calls = runtime.get('model_calls')
    calls = calls if isinstance(calls, list) else []
    if (row.get('http_status') != 200 or row.get('technical_completed') is not True or
            result.get('isFallback') is not False or row.get('runner_failure') or row.get('cleanup_failure') or
            runtime.get('failure') or result.get('error') or result.get('compliance_pass') is False):
        reasons.append('http_fallback_publisher_or_critical_failure')
    if runtime.get('budget_limit') or result.get('budgetLimit') or row.get('budgetLimit'):
        reasons.append('budget_limit')
    for key, limit in (('rounds', 8), ('tool_calls', 12), ('budget_tokens', 100000), ('duration_ms', 105000)):
        if type(runtime.get(key)) is not int or not 0 <= runtime[key] <= limit:
            reasons.append('budget_or_runtime_counter_invalid:' + key)
    if not isinstance(runtime.get('budget_policy'), dict) or runtime['budget_policy'].get('limit') != 100000:
        reasons.append('agent_budget_policy_mismatch')
    if not calls:
        reasons.append('model_calls_missing')
    stage_totals = {'primary': 0, 'reviewer': 0}
    all_known = bool(calls)
    for call in calls:
        if not isinstance(call, dict) or call.get('stage') not in stage_totals:
            reasons.append('model_call_shape_or_stage_invalid')
            all_known = False
            continue
        usage = counters(call.get('usage'))
        if usage is None or call.get('usage_complete') is not True:
            all_known = False
        else:
            stage_totals[call['stage']] += usage['total_tokens']
        if (call.get('billing_mode') != 'paid' or type(call.get('request_sent')) is not bool or
                type(call.get('attempts')) is not int or call['attempts'] not in (0, 1) or
                (call.get('request_sent') is True and call['attempts'] != 1) or
                (call.get('outcome') == 'completed' and call.get('request_sent') is not True) or
                (call.get('attempts') == 0 and (call.get('request_sent') is not False or usage is None or usage['total_tokens'] != 0))):
            reasons.append('model_sent_admission_or_billing_invalid')
        if call.get('error_code') == 'response_truncated':
            reasons.append('response_length_limit')
        if call.get('outcome') != 'completed' or call.get('error_code'):
            reasons.append('critical_model_call_failure')
    total = sum(stage_totals.values()) if all_known else None
    if (runtime.get('usage_complete') is not True or not all_known or
            any(type(runtime.get(key)) is not int or runtime[key] != stage_totals[stage]
                for key, stage in (('primary_tokens', 'primary'), ('reviewer_tokens', 'reviewer'))) or
            type(runtime.get('total_tokens')) is not int or runtime['total_tokens'] != total):
        reasons.append('unknown_or_inconsistent_actual_usage')
    if total is not None and (runtime.get('budget_tokens') != total or total > 100000):
        reasons.append('actual_usage_budget_mismatch_or_exceeded')
    if not isinstance(logs, list) or len(logs) != len(calls):
        reasons.append('actual_model_log_missing_or_count_mismatch')
    else:
        for log, call in zip(logs, calls):
            if not isinstance(log, dict) or not isinstance(call, dict) or call.get('stage') not in stage_totals:
                reasons.append('actual_model_log_shape_invalid')
                continue
            expected_stage = 'conversation.tools' if call['stage'] == 'primary' else 'reviewer.conversation'
            if (log.get('stage') != expected_stage or log.get('outcome') != call.get('outcome') or
                    log.get('error_code', '') != (call.get('error_code') or '')):
                reasons.append('actual_model_log_stage_or_outcome_mismatch')
            if 'usage' in log and counters(log['usage']) != counters(call.get('usage')):
                reasons.append('actual_model_log_usage_mismatch')
            if log.get('finish_reason') == 'length' or log.get('error_code') == 'response_truncated':
                reasons.append('response_length_limit')
    oracle = row.get('oracle')
    if not isinstance(oracle, list) or not oracle or any(not isinstance(fact, dict) or fact.get('passed') is not True for fact in oracle):
        reasons.append('original_literal_fixture_precheck_failed')
    before, after = row.get('before'), row.get('after')
    immutable = ('total_cash_minor', 'profile', 'orders', 'holdings', 'fund_holds', 'memories', 'observations')
    if (not isinstance(before, dict) or not isinstance(after, dict) or
            any(key not in before or key not in after or public.canonical_sha(before[key]) != public.canonical_sha(after[key]) for key in immutable) or
            any(row.get(key) is not True for key in ('no_financial_execution', 'no_action_cards', 'profile_unchanged')) or
            any(type(row.get(key)) is not int or row[key] != 0 for key in ('memory_delta', 'observation_delta')) or
            result.get('bank_actions') or result.get('draft_ids')):
        reasons.append('financial_profile_or_memory_side_effect')
    trace = runtime.get('trace')
    if not isinstance(trace, list) or any(not isinstance(item, dict) for item in trace):
        reasons.append('actual_tool_trace_missing')
    elif any(item.get('status') == 'completed' and item.get('tool') in FORBIDDEN_WRITES for item in trace):
        reasons.append('unexpected_completed_business_write_tool')
    review = result.get('agent_review')
    review = review.get('status') if isinstance(review, dict) else None
    answer_contract = result.get('answer_contract')
    fact_kind = answer_contract.get('kind') if isinstance(answer_contract, dict) else None
    if review != 'approved' and not (review == 'not_required' and fact_kind == 'verified_facts'):
        reasons.append('final_review_or_fact_contract_missing')
    # Shared paid accounting supplements the original financial/trace contract.
    # Runtime and transport logs observe the same calls; never add them twice.
    paid_audit = public.paid_usage_audit(runtime, logs, result.get('bank_response_id'))
    reasons.extend(paid_audit['errors'])
    identity_audit = public.audit_response_identity(runtime, logs, row.get('response_metadata'),
        turn_id=result.get('bank_response_id'),configured_models=public.MODEL_IDENTITIES)
    reasons.extend(identity_audit['errors'])
    review_calls = [call for call in calls if isinstance(call, dict) and call.get('stage') == 'reviewer']
    independent_reviews = row.get('independent_reviews')
    if not isinstance(independent_reviews, list) or len(independent_reviews) != len(review_calls):
        reasons.append('actual_review_usage_observation_missing')
    else:
        for call, observation in zip(review_calls, independent_reviews):
            decision = observation.get('decision') if isinstance(observation, dict) else None
            observed_usage = counters(decision.get('_model_usage')) if isinstance(decision, dict) else None
            if observed_usage is None or observed_usage != counters(call.get('usage')):
                reasons.append('actual_review_usage_observation_mismatch')
    actual = None if any(code in reasons for code in (
        'unknown_or_inconsistent_actual_usage', 'unknown_usage', 'actual_model_log_usage_mismatch',
        'actual_review_usage_observation_mismatch')) or identity_audit['errors'] else total
    return {'stop_reasons': list(dict.fromkeys(reasons)), 'actual_total_tokens': actual,
            'known_tokens_lower_bound': paid_audit['known_tokens_lower_bound'],
            'primary_tokens': stage_totals['primary'] if all_known else None,
            'reviewer_tokens': stage_totals['reviewer'] if all_known else None,
            'actual_model_requests_sent': sum(call['request_sent'] is True for call in calls)
                if calls and all(isinstance(call, dict) and type(call.get('request_sent')) is bool for call in calls) else None,
            'budget_tokens': runtime.get('budget_tokens'), 'semantic_judgment': None,
            'financial_observer_scope': list(immutable), 'provider_cost': None}


def execute_case(candidate, case, inputs, out):
    out = Path(out)
    out.mkdir()
    semantic = out / 'semantic'
    with tempfile.TemporaryDirectory(prefix='navren-g2-finance-state-') as state:
        environment = dict(inputs['contract'].isolated_environment(candidate, Path(state)))
        # 原 CLI 禁止知识库环境覆盖；逐题公共库由显式临时 bank 派生并核验隔离路径。
        environment.pop('NAVREN_KNOWLEDGE_DB', None)
        environment.update(NAVREN_AGENT_TIMEOUT='105', NAVREN_ALLOW_CLOUD_ASR='0',
                           NAVREN_MODEL_CALL_LOG=str(semantic / 'model-calls.jsonl'))
        execution = inputs['contract'].execute(case_command(candidate, case['id'], semantic),
                    candidate, out / 'process', CHILD_SECONDS, environment)
    path = semantic / 'semantic-results.json'
    log = semantic / 'model-calls.jsonl'
    logs = public.read_model_log_prefix(log)
    try:
        report = read_json(path) if path.is_file() and not path.is_symlink() else None
        record = validate_child(execution, report, logs, case['id'], case['prompt'], inputs['sources'], inputs['datasets'], candidate)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        record = {'stop_reasons': ['actual_result_parse_or_contract_failure'], 'error_type': type(error).__name__,
                  'actual_total_tokens': None, 'actual_model_requests_sent': None, 'semantic_judgment': None,
                  'known_tokens_lower_bound': public.audit_model_log_prefix(logs,
                      configured_models=public.MODEL_IDENTITIES)['known_tokens_lower_bound']}
    record.update(id=case['id'], execution=execution, status='stopped' if record['stop_reasons'] else 'collected_pending_independent_judgment',
                  semantic_results_path=str(path), semantic_results_sha256=public.sha(path) if path.is_file() else None,
                  model_log_sha256=public.sha(log) if log.is_file() else None, task_budget_seconds=105,
                  child_hard_limit_seconds=CHILD_SECONDS, oracle_sent_to_model=False)
    public.atomic_json(out / 'record.json', record)
    return record


def unchanged(inputs, candidate, tracked):
    return (inputs['contract'].source_manifest(candidate) == inputs['sources'] and
            inputs['contract'].data_manifest(candidate) == inputs['datasets'] and
            inputs['contract'].auxiliary_manifest(candidate) == public.AUXILIARY and
            all(public.sha(path) == digest for path, digest in tracked.items()))


def run(args):
    out = Path(args.out_dir).resolve()
    if not out.is_relative_to(OUTPUT_BASE.resolve()) or out == OUTPUT_BASE.resolve():
        raise ValueError('finance_output_outside_authorized_scope')
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'version': VERSION, 'status': 'not_run', 'case_order': list(ORDER),
              'records': [{'id': ident, 'status': 'not_run', 'reason': 'cloud_not_explicitly_authorized', 'semantic_judgment': None} for ident in ORDER],
              'stage_seconds': 600, 'parallelism': 1, 'task_limits': LIMITS,
              'child_hard_limit_seconds': CHILD_SECONDS, 'actual_model_cost': None, 'semantic_judgment': None,
              'full_original_finance_G3_run': False, 'returned_exit_code': 2}
    try:
        if args.cloud is not True:
            return 2
        candidate = Path(args.candidate).resolve()
        inputs = inspect_inputs(candidate, args.source_manifest, args.data_manifest)
        gate = require_cloud(args, inputs)
        tracked = {Path(args.source_manifest): public.sha(args.source_manifest), Path(args.data_manifest): public.sha(args.data_manifest),
                   Path(args.handoff): args.handoff_sha256, **{path: digest for name, path in inputs['finance_paths'].items()
                   for digest in [inputs['finance_input_sha256'][name]]}}
        report.update(status='running', source_sha256=inputs['sources'], data_sha256=inputs['datasets'],
                      cloud_gate=gate, finance_input_sha256=inputs['finance_input_sha256'], returned_exit_code=1)
        for index, case in enumerate(inputs['finance_cases']):
            if STAGE_SECONDS - (time.monotonic() - started) < CHILD_SECONDS:
                report['stop_reason'] = ['finance_stage_insufficient_for_complete_next_child']
                break
            if not unchanged(inputs, candidate, tracked):
                report['stop_reason'] = ['same_candidate_input_drift_before_next_paid_case']
                break
            require_cloud(args, inputs)
            # Child launch/save exceptions may occur after a paid request. Unknown
            # progress must never be reported as an unattempted zero-cost case.
            report['records'][index] = {'id': case['id'], 'status': 'running',
                'actual_total_tokens': None, 'actual_model_requests_sent': None, 'semantic_judgment': None}
            public.atomic_json(out / 'results.json', report)
            try:
                record = execute_case(candidate, case, inputs, out / case['id'])
            except Exception as error:
                record = {'id': case['id'], 'status': 'stopped',
                    'stop_reasons': ['child_launch_collection_or_receipt_failure'],
                    'error_type': type(error).__name__, 'actual_total_tokens': None,
                    'actual_model_requests_sent': None, 'semantic_judgment': None}
            report['records'][index] = record
            if not unchanged(inputs, candidate, tracked):
                record['stop_reasons'].append('same_candidate_input_drift_after_child')
                record['status'] = 'stopped'
                if (out / case['id']).is_dir():
                    public.atomic_json(out / case['id'] / 'record.json', record)
            public.atomic_json(out / 'results.json', report)
            if record['stop_reasons']:
                report['stop_reason'] = record['stop_reasons']
                break
        for record in report['records']:
            if record['status'] == 'not_run':
                record['reason'] = 'prior_stop_or_insufficient_stage_budget'
        complete = all(r['status'] == 'collected_pending_independent_judgment' and not r['stop_reasons'] for r in report['records'])
        report.update(status='collected_pending_independent_judgment' if complete else 'stopped_technical_failure',
                      returned_exit_code=0 if complete else 1)
    except Exception as error:
        report.update(status='not_run_gate_or_dependency_failure' if all(r['status'] == 'not_run' for r in report['records']) else 'stopped_technical_failure',
                      error={'type': type(error).__name__, 'code': str(error)[:200]}, returned_exit_code=2 if all(r['status'] == 'not_run' for r in report['records']) else 1)
    finally:
        report['duration_seconds'] = time.monotonic() - started
        if report['duration_seconds'] > 600:
            report.update(status='stopped_technical_failure', stop_reason=['finance_stage_deadline_exceeded'], returned_exit_code=1)
        attempted = [r for r in report['records'] if r['status'] != 'not_run']
        report['known_requests_sent_lower_bound'] = sum(r['actual_model_requests_sent'] for r in attempted
            if type(r.get('actual_model_requests_sent')) is int)
        report['actual_requests_sent'] = report['known_requests_sent_lower_bound'] if all(
            type(r.get('actual_model_requests_sent')) is int for r in attempted) else None
        public.atomic_json(out / 'results.json', report)
    return report['returned_exit_code']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, default=ROOT)
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--data-manifest', type=Path)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--handoff', type=Path)
    parser.add_argument('--handoff-sha256')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
