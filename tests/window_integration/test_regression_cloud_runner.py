"""免费控制器协议检查；只执行受控stdlib子进程，不运行原22题或金融语义。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import regression_cloud_runner as runner

ROOT = Path(__file__).resolve().parents[2]
RECOVERY_OBSERVATIONS = []
WORKER = '''import json, pathlib, sys, time
mode, ident, out, touched = sys.argv[1:]
if mode == 'timeout':
    time.sleep(10)
if mode == 'mutation':
    pathlib.Path(touched).write_text('changed by controlled child')
pathlib.Path(out).write_text(json.dumps({'id': ident, 'billing_mode': 'unpaid_fixture',
    'protocol_result': 'unknown_usage' if mode == 'unknown' else 'complete',
    'actual_total_tokens': None, 'actual_model_requests_sent': None, 'semantic_judgment': None}))
'''

RECOVERY_WORKER = '''import json, pathlib, socket, sys, time
def no_network(*args, **kwargs):
    raise AssertionError('controlled_protocol_child_must_not_connect')
socket.socket.connect = no_network
payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
out = pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
log = out/'model-calls.jsonl'
lines = ''.join(json.dumps(row)+'\\n' for row in payload['logs'])
if payload.get('malformed_tail'):
    lines += '{"invalid_tail":'
if payload.get('redirect_log_path'):
    external = pathlib.Path(payload['redirect_log_path']); external.write_text(lines)
    log.symlink_to(external)
else:
    log.write_text(lines)
result = out/'semantic-results.json'
mode = payload['mode']
if mode in ('empty', 'timeout'):
    result.write_text(json.dumps({'records': [], 'scope': 'unpaid_protocol_only'}))
elif mode == 'bad':
    result.write_text('{')
elif mode == 'complete':
    result.write_text(json.dumps(payload['report']))
if mode == 'timeout':
    time.sleep(10)
'''


class RegressionControllerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='navren-g3-controller-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.worker = self.base/'protocol_worker.py'
        self.worker.write_text(WORKER)
        self.watched = self.base/'frozen-input.txt'
        self.watched.write_text('controlled frozen input')
        self.original_sha = runner.public.sha(self.watched)
        self.launched, self.executions = [], []
        spec = importlib.util.spec_from_file_location('_g3_controlled_process_contract', ROOT/'scripts/run_window_integration.py')
        self.executor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.executor)

    def protocol_child(self, family, case):
        ident = case['id']; self.launched.append((family, ident))
        out = self.base/ident; out.mkdir()
        raw_path = out/'protocol.json'
        command = [sys.executable, '-B', str(self.worker), case.get('mode', 'pass'), ident,
                   str(raw_path), str(self.watched)]
        actual = self.executor.execute(command, self.base, out/'process', case.get('timeout', 2), os.environ.copy())
        self.executions.append(actual)
        self.assertEqual(json.loads((out/'process/execution.json').read_text()), actual)
        record = {'id': ident, 'status': 'collected_pending_independent_judgment',
                  'stop_reasons': [], 'execution': actual, 'billing_mode': 'unpaid_fixture',
                  'actual_total_tokens': None, 'actual_model_requests_sent': None,
                  'known_tokens_lower_bound': None, 'semantic_judgment': None,
                  'protocol_only': True}
        if actual['status'] != 'exited' or actual['exit_code'] != 0:
            record['stop_reasons'].append('actual_child_not_exited_zero')
        else:
            raw = json.loads(raw_path.read_text())
            self.assertIsNone(raw['semantic_judgment'])
            self.assertEqual(raw['billing_mode'], 'unpaid_fixture')
            if raw['protocol_result'] == 'unknown_usage':
                record['stop_reasons'].append('unknown_or_inconsistent_actual_usage')
        if record['stop_reasons']:
            record['status'] = 'stopped'
        return record

    def collect(self, cases, *, started=None, stage_seconds=10, whole_seconds=20, caps=None):
        report = runner.initial_report(cases, paid=False)
        snapshots = []
        code = runner.collect_serial(list(cases.items()), report,
            started=time.monotonic() if started is None else started,
            execute=self.protocol_child, guard=lambda: runner.public.sha(self.watched) == self.original_sha,
            save=lambda: snapshots.append(json.loads(json.dumps(report))), paid=False,
            stage_seconds=stage_seconds, whole_seconds=whole_seconds,
            caps=caps or {family: 2 for family in cases})
        self.assertEqual(snapshots[-1], report)
        self.assertIsNone(report['semantic_judgment'])
        self.assertIsNone(report['actual_usage']['actual_total_tokens'])
        self.assertIsNone(report['actual_usage']['actual_model_requests_sent'])
        self.assertIsNone(report['actual_model_cost'])
        return code, report

    def native_fixture(self):
        """Synthetic transport values, never original task or provider evidence."""
        configured = runner.public.configured_model_contract(ROOT)
        turn = 'controlled-protocol-turn-no-model-request'
        usage = {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17}
        review_usage = {'prompt_tokens': 15, 'completion_tokens': 5, 'total_tokens': 20}
        calls = [{'stage': stage, 'usage': value, 'usage_complete': True,
                  'billing_mode': 'paid', 'model_id': configured[stage]['model_id'],
                  'request_sent': True, 'attempts': 1, 'outcome': 'completed', 'error_code': None}
                 for stage, value in (('primary', usage), ('reviewer', review_usage))]
        runtime = {'model_calls': calls, 'usage_complete': True, 'primary_tokens': 17,
                   'reviewer_tokens': 20, 'total_tokens': 37, 'budget_tokens': 37,
                   'budget_policy': {'limit': 100000}, 'rounds': 1, 'tool_calls': 0,
                   'duration_ms': 10, 'trace': [], 'failure': None, 'budget_limit': None}
        logs = [{'stage': 'conversation.tools' if stage == 'primary' else 'reviewer.conversation',
                 'request_id': turn, 'model_id': configured[stage]['model_id'],
                 'provider': configured[stage]['provider'], 'outcome': 'completed',
                 'error_code': '', 'http_status': 200, 'timestamp': 'controlled-' + stage}
                for stage in ('primary', 'reviewer')]
        logs[0]['usage'] = usage
        logs[1].update(attempts=1, finish_reason='stop')  # Real GLM logger omits usage.
        metadata = {stage: [{'id': 'controlled-' + stage + '-envelope',
                            'model': configured[stage]['model_id']}] for stage in configured}
        reviews = [{'packet': {}, 'decision': {'verdict': 'approve', '_model_usage': review_usage}}]
        return configured, turn, runtime, logs, metadata, reviews

    def recovery_child(self, *, mode='missing', logs=None, malformed_tail=False,
                       redirect_log=False, invalid_execution_receipt=False):
        configured, turn, runtime, all_logs, metadata, reviews = self.native_fixture()
        case = {'id': 'controlled-recovery-' + str(len(self.executions)),
                'prompt': 'unpaid collector protocol; no original case execution'}
        sources = {runner.finance.LEGACY_PATH: runner.public.sha(ROOT/runner.finance.LEGACY_PATH),
                   runner.finance.DATASET_PATH: runner.finance.DATASET_SHA}
        state = {'total_cash_minor': 0, 'profile': {}, 'orders': [], 'holdings': [],
                 'fund_holds': [], 'memories': [], 'observations': 0}
        row = {'id': case['id'], 'question': case['prompt'], 'http_status': 200, 'technical_completed': True,
               'result': {'reply': 'controlled protocol only', 'isFallback': False, 'bank_response_id': turn,
                          'agent_review': {'status': 'approved'}, 'agent_runtime': runtime},
               'response_metadata': metadata, 'independent_reviews': reviews,
               'before': state, 'after': deepcopy(state), 'no_financial_execution': True,
               'no_action_cards': True, 'profile_unchanged': True, 'memory_delta': 0, 'observation_delta': 0,
               'workspace_id': 'controlled-no-business-import',
               'oracle': [{'passed': True, 'scope': 'unpaid protocol shape; no original gold applied'}]}
        report = {'offline': False, 'tested_source_root': str(ROOT.resolve()), 'selected_case_ids': [case['id']],
                  'expected_case_count': 1, 'collection_complete': True, 'complete': True,
                  'run_status': 'collection_complete', 'records': [row], 'source_sha256': sources,
                  'final_source_sha256': sources, 'source_unchanged': True, 'dataset_unchanged': True,
                  'dataset_sha256': runner.finance.DATASET_SHA, 'final_dataset_sha256': runner.finance.DATASET_SHA,
                  'invalidated': False, 'scope': 'unpaid_protocol_only'}
        worker = self.base/'recovery_worker.py'; worker.write_text(RECOVERY_WORKER)
        payload = {'mode': mode, 'logs': deepcopy(logs if logs is not None else
                   all_logs if mode == 'complete' else all_logs[:1]), 'report': report,
                   'malformed_tail': malformed_tail}
        if redirect_log:
            payload['redirect_log_path'] = str(self.base/'outside-case-log.jsonl')
        requested = []
        def actual_child(command, cwd, process_out, cap, env):
            requested.append(command)
            # Execute only the controlled stdlib worker. Keep its real argv so
            # the original CLI gate must reject; never forge a CLI receipt.
            row['database_paths'] = {'bank': env['NAVREN_SIM_DB'],
                'personal': str(Path(env['NAVREN_SIM_DB']).parent/'private.sqlite3')}
            payload_path = self.base/(case['id'] + '-payload.json')
            payload_path.write_text(json.dumps(payload))
            result_out = Path(runner.flag(command, '--out-dir'))
            actual = self.executor.execute([sys.executable, '-B', str(worker), str(payload_path), str(result_out)],
                cwd, process_out, .15 if mode == 'timeout' else 2, env)
            self.executions.append(actual)
            if invalid_execution_receipt:
                (Path(process_out)/'execution.json').write_text(json.dumps({**actual, 'exit_code': 99}))
            return actual
        inputs = {'contract': SimpleNamespace(isolated_environment=self.executor.isolated_environment, execute=actual_child),
                  'sources': sources, 'datasets': {}, 'g3_cases': {'finance': [case]}}
        args = SimpleNamespace(candidate=ROOT)
        observed_connects = []
        def no_network(*args, **kwargs):
            observed_connects.append('blocked'); raise AssertionError('protocol_must_not_connect')
        with patch.object(socket.socket, 'connect', side_effect=no_network):
            actual = runner.execute_case(args, 'finance', case, inputs, self.base/case['id'])
        self.assertEqual(observed_connects, [])
        self.assertEqual(len(requested), 1)
        evidence = {'scope': 'unpaid stdlib subprocess synthetic transport values, zero model requests',
            'mode': mode, 'malformed_tail': malformed_tail, 'redirect_log': redirect_log,
            'invalid_execution_receipt': invalid_execution_receipt, 'actual_child': self.executions[-1],
            'requested_original_command_not_executed': requested[0], 'record': actual}
        RECOVERY_OBSERVATIONS.append(evidence)
        return actual

    def test_original_complete_corpora_and_cli_are_frozen(self):
        contract, cases = runner.load_corpus(ROOT)
        self.assertEqual((len(cases['public']), len(cases['finance'])), (12, 10))
        self.assertEqual(runner.finance.DATASET_PATH, 'evaluation/navren_skill_semantic_cases.json')
        self.assertEqual(runner.finance.DATASET_SHA, 'f92627f6dcd96a979d17d805dc5a219a47fcacc0a9e647c6f6e1a421f62ec0e6')
        self.assertEqual(contract['families']['public']['original_sha256'], runner.public.QUESTION_SHA)
        ident = cases['finance'][8]['id']
        with self.assertRaises(ValueError):
            runner.finance.case_command(ROOT, ident, self.base/'old')
        command = runner.financial_command(ROOT, ident, self.base/'new', cases['finance'])
        self.assertEqual(command[command.index('--dataset')+1], str(ROOT/'evaluation/navren_skill_semantic_cases.json'))
        self.assertEqual(command[-2:], ['--case-ids', 'S09_imported_surplus_not_cash_or_full_year'])
        self.assertEqual(runner.public.ORDER, ('P10', 'P02'))
        self.assertEqual(runner.finance.ORDER[-1], 'S10_linked_reserve_dedup')
        self.assertEqual(runner.public.agent_body(cases['public'][0]), {'text': cases['public'][0]['question']})

    def test_real_serial_children_preserve_two_denominators_and_null_usage(self):
        code, report = self.collect({'public': [{'id': 'p1'}, {'id': 'p2'}], 'finance': [{'id': 'f1'}]})
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [('public', 'p1'), ('public', 'p2'), ('finance', 'f1')])
        self.assertEqual(report['status'], 'unpaid_controller_protocol_completed')
        self.assertEqual([g['denominator'] for g in report['families'].values()], [2, 1])
        self.assertEqual([g['collected_count'] for g in report['families'].values()], [2, 1])
        self.assertTrue(all(g['semantic_pass_count'] is None for g in report['families'].values()))
        self.assertTrue(all(e['status'] == 'exited' and e['exit_code'] == 0 for e in self.executions))
        self.assertLessEqual(self.executions[0]['completed_at'], self.executions[1]['started_at'])

    def test_real_unknown_usage_stops_next_family_before_launch(self):
        code, report = self.collect({'public': [{'id': 'p1', 'mode': 'unknown'}, {'id': 'p2'}], 'finance': [{'id': 'f1'}]})
        self.assertEqual(code, 1)
        self.assertEqual(self.launched, [('public', 'p1')])
        self.assertEqual(self.executions[0]['status'], 'exited')
        self.assertEqual(self.executions[0]['exit_code'], 0)
        self.assertIn('unknown_or_inconsistent_actual_usage', report['stop_reasons'])
        self.assertEqual(report['families']['public']['attempted_count'], 1)
        self.assertEqual(report['families']['finance']['attempted_count'], 0)
        self.assertEqual(report['families']['finance']['records'][0]['status'], 'not_run')

    def test_real_timeout_is_failure_and_does_not_start_next_child(self):
        code, report = self.collect({'public': [{'id': 'p1', 'mode': 'timeout', 'timeout': .15}, {'id': 'p2'}]})
        self.assertEqual(code, 1)
        self.assertEqual(self.launched, [('public', 'p1')])
        self.assertEqual(self.executions[0]['status'], 'timed_out')
        self.assertIs(type(self.executions[0]['exit_code']), int)
        self.assertLess(self.executions[0]['duration_seconds'], 3)
        self.assertIn('actual_child_not_exited_zero', report['stop_reasons'])
        self.assertEqual(report['families']['public']['records'][1]['status'], 'not_run')

    def test_real_child_input_mutation_stops_all_subsequent_collection(self):
        code, report = self.collect({'public': [{'id': 'p1', 'mode': 'mutation'}, {'id': 'p2'}], 'finance': [{'id': 'f1'}]})
        self.assertEqual(code, 1)
        self.assertEqual(self.executions[0]['exit_code'], 0)
        self.assertNotEqual(runner.public.sha(self.watched), self.original_sha)
        self.assertEqual(self.launched, [('public', 'p1')])
        self.assertIn('same_candidate_or_evidence_drift_after_child', report['stop_reasons'])
        self.assertEqual(report['families']['finance']['collected_count'], 0)

    def test_gate_elapsed_consumes_admission_budget_without_shortening_child(self):
        started = time.monotonic()
        # 实际elapsed造成完整next cap不足；不mock时间或把hardcap缩成remaining。
        time.sleep(.03)
        code, report = self.collect({'public': [{'id': 'p1'}]}, started=started,
                                    stage_seconds=.2, whole_seconds=.2, caps={'public': .19})
        self.assertEqual(code, 1)
        self.assertEqual(self.launched, [])
        self.assertEqual(report['stop_reasons'], ['not_run_budget_dependency'])
        self.assertEqual(report['families']['public']['records'][0]['reason'], 'not_run_budget_dependency')
        self.assertEqual(report['families']['public']['attempted_count'], 0)
        self.assertGreaterEqual(report['duration_seconds'], .03)

    def test_raw_evidence_hash_and_actual_zero_exit_cannot_replace_exact_cli(self):
        path = self.base/'root-judgment.json'
        path.write_text('{"root_verified": false}')
        bound = runner.binding(path)
        self.assertEqual(runner.bound_file(bound, self.base), path)
        path.write_text('{"root_verified": true}')
        with self.assertRaisesRegex(ValueError, 'evidence_digest'):
            runner.bound_file(bound, self.base)
        actual = self.executor.execute([sys.executable, '-B', str(self.worker), 'pass', 'wrong',
            str(self.base/'wrong.json'), str(self.watched)], self.base, self.base/'wrong-process', 2, os.environ.copy())
        self.assertEqual(actual['exit_code'], 0)
        args = argparse.Namespace(candidate=ROOT, source_manifest=self.base/'source.json', data_manifest=self.base/'data.json')
        inputs = {'sources': {runner.G2_SCRIPT['public']: runner.public.sha(ROOT/runner.G2_SCRIPT['public'])}}
        with self.assertRaisesRegex(ValueError, 'g2_actual_cli_source_not_bound'):
            runner.validate_g2_execution(actual, 'public', self.base/'g2/results.json', args, inputs, {})
        with self.assertRaisesRegex(ValueError, 'missing_or_duplicate'):
            runner.flag(['python', '--out-dir', 'a', '--out-dir', 'b'], '--out-dir')

    def test_unpaid_protocol_cannot_become_native_cloud_usage_or_semantic_pass(self):
        rejected = runner.validate_native({'model_calls': []}, [], {}, [], 'approved',
                                          {'primary': 'DeepSeek-V4-Flash', 'reviewer': 'GLM-5.3-Flash'})
        self.assertTrue(rejected['stop_reasons'])
        self.assertIsNone(rejected['actual_total_tokens'])
        self.assertIsNone(rejected['actual_model_requests_sent'])
        record = {'status': 'collected_pending_independent_judgment', 'actual_total_tokens': 37,
                  'actual_model_requests_sent': 2, 'known_tokens_lower_bound': 37}
        usage = runner.account_usage([record, {**record, 'actual_total_tokens': None,
                                   'actual_model_requests_sent': None, 'known_tokens_lower_bound': 11}], paid=True)
        self.assertIsNone(usage['actual_total_tokens'])
        self.assertIsNone(usage['actual_model_requests_sent'])
        self.assertEqual(usage['known_tokens_lower_bound'], 48)
        # 合成数值只检查加总/unknown传播；不是实际paid用量或原题成绩。
        self.assertIsNone(runner.account_usage([record], paid=False)['actual_total_tokens'])

    def assert_recovery_stops_after_first(self, actual, lower_bound):
        self.assertEqual(actual['status'], 'stopped')
        self.assertIsNone(actual['actual_total_tokens'])
        self.assertIsNone(actual['actual_model_requests_sent'])
        self.assertEqual(actual['known_tokens_lower_bound'], lower_bound)
        self.assertIsNone(actual['actual_model_cost'])
        self.assertTrue(actual['temporary_state_removed'])
        cases = {'finance': [{'id': actual['id']}, {'id': 'never-next-finance'}],
                 'public': [{'id': 'never-next-public'}]}
        report = runner.initial_report(cases)
        launched = []
        def execute(family, case):
            launched.append((family, case['id']))
            return actual
        code = runner.collect_serial(list(cases.items()), report, started=time.monotonic(),
            execute=execute, guard=lambda: True, save=lambda: None, paid=True)
        self.assertEqual(code, 1)
        self.assertEqual(launched, [('finance', actual['id'])])
        self.assertEqual(report['families']['public']['attempted_count'], 0)
        self.assertEqual(report['families']['finance']['records'][1]['status'], 'not_run')
        self.assertEqual(report['actual_usage']['known_tokens_lower_bound'], lower_bound or 0)
        self.assertIsNone(report['actual_usage']['actual_total_tokens'])
        self.assertIsNone(report['actual_usage']['actual_model_requests_sent'])
        self.assertIsNone(report['semantic_judgment'])

    def test_real_partial_result_missing_empty_bad_keeps_known_prefix_and_stops(self):
        for mode in ('missing', 'empty', 'bad'):
            with self.subTest(mode=mode):
                actual = self.recovery_child(mode=mode)
                self.assertEqual(actual['execution']['exit_code'], 0)
                self.assertIn('actual_child_result_missing_or_invalid', actual['stop_reasons'])
                self.assert_recovery_stops_after_first(actual, 17)

    def test_real_child_killed_after_written_prefix_preserves_known_usage(self):
        actual = self.recovery_child(mode='timeout')
        self.assertEqual(actual['execution']['status'], 'timed_out')
        self.assertIs(type(actual['execution']['exit_code']), int)
        self.assertIn('actual_child_not_exited_zero', actual['stop_reasons'])
        self.assert_recovery_stops_after_first(actual, 17)

    def test_real_post_parse_cli_or_receipt_failure_retains_runtime_once(self):
        for invalid_receipt in (False, True):
            with self.subTest(invalid_execution_receipt=invalid_receipt):
                actual = self.recovery_child(mode='complete', invalid_execution_receipt=invalid_receipt)
                self.assertEqual(actual['execution']['exit_code'], 0)
                self.assertIn('actual_child_result_missing_or_invalid', actual['stop_reasons'])
                if invalid_receipt:
                    self.assertIn('actual_child_execution_receipt_missing_or_invalid', actual['stop_reasons'])
                self.assert_recovery_stops_after_first(actual, 37)  # 17 + GLM20, not 37 + log17.

    def test_real_malformed_tail_or_redirected_log_never_inflates_prefix(self):
        actual = self.recovery_child(mode='empty', malformed_tail=True)
        self.assertIn('actual_model_log_prefix_shape_invalid', actual['stop_reasons'])
        self.assert_recovery_stops_after_first(actual, 17)
        actual = self.recovery_child(mode='empty', redirect_log=True)
        self.assertTrue(any(code.startswith('actual_model_log_missing_or_invalid:') for code in actual['stop_reasons']))
        self.assert_recovery_stops_after_first(actual, None)

    def test_real_illegal_native_log_identity_or_usage_not_counted(self):
        for mutation in ('model', 'provider', 'turn', 'boolean_usage', 'contradictory_usage', 'first_reviewer'):
            configured, turn, runtime, logs, metadata, reviews = self.native_fixture()
            log = deepcopy(logs[0])
            if mutation == 'model': log['model_id'] = 'unconfigured-model'
            elif mutation == 'provider': log['provider'] = 'unconfigured-provider'
            elif mutation == 'turn': log['request_id'] = ''
            elif mutation == 'boolean_usage': log['usage']['total_tokens'] = True
            elif mutation == 'contradictory_usage': log['usage']['total_tokens'] = 999
            else: log = deepcopy(logs[1])
            with self.subTest(mutation=mutation):
                actual = self.recovery_child(logs=[log])
                self.assertEqual(actual['execution']['exit_code'], 0)
                self.assert_recovery_stops_after_first(actual, None)

    def test_finance_dispatch_uses_original_cli_knowledge_policy_without_mutating_parent(self):
        worker = self.base/'knowledge_dispatch_worker.py'
        worker.write_text('''import ast, json, os, pathlib, socket, sys, tempfile
from types import SimpleNamespace
def no_network(*args, **kwargs):
    raise AssertionError('controlled_dispatch_child_must_not_connect')
socket.socket.connect = no_network
source, output, fallback = map(pathlib.Path, sys.argv[1:])
keys = ('NAVREN_SIM_DB', 'QINGZHOU_SIM_DB', 'NAVREN_MARKET_DB', 'NAVREN_FUND_CACHE', 'TMPDIR')
observed = {key: os.environ[key] for key in keys}
assert 'NAVREN_KNOWLEDGE_DB' not in os.environ
tree = ast.parse(source.read_text())
guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
          and any(isinstance(part, ast.Constant) and part.value == 'knowledge_database_environment_override'
                  for part in ast.walk(node))]
assert len(guards) == 1
guard = compile(ast.fix_missing_locations(ast.Module(body=[guards[0]], type_ignores=[])), str(source), 'exec')
exec(guard, {'os': os})
os.environ['NAVREN_KNOWLEDGE_DB'] = str(fallback)
try:
    exec(guard, {'os': os})
    raise AssertionError('original_CLI_override_guard_was_not_preserved')
except ValueError as error:
    assert str(error) == 'knowledge_database_environment_override'
finally:
    del os.environ['NAVREN_KNOWLEDGE_DB']
check = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'assert_isolated_storage')
scope = {'Path': pathlib.Path}
exec(compile(ast.fix_missing_locations(ast.Module(body=[check], type_ignores=[])), str(source), 'exec'), scope)
with tempfile.TemporaryDirectory(prefix='controlled-original-cli-case-', dir=os.environ['TMPDIR']) as temp:
    bank = pathlib.Path(temp)/'bank.sqlite3'
    gateway = SimpleNamespace(bank=SimpleNamespace(database=bank),
        personal_data=SimpleNamespace(database=bank.with_name('bank.personal.sqlite3')),
        knowledge_provider=SimpleNamespace(path=bank.with_name('bank.knowledge.sqlite3')),
        market=SimpleNamespace(path=bank.with_name('bank.market.sqlite3')))
    paths = scope['assert_isolated_storage'](gateway, temp)
    gateway.knowledge_provider.path = fallback
    try:
        scope['assert_isolated_storage'](gateway, temp)
        raise AssertionError('original_CLI_storage_guard_was_not_preserved')
    except ValueError as error:
        assert str(error) == 'storage_outside_temporary_root'
output.write_text(json.dumps({'scope': 'exact_original_CLI_AST_guards_only_no_business_constructor',
    'billing_mode': 'unpaid_fixture', 'model_calls': 0, 'observed_dispatch_paths': observed,
    'knowledge_override_present_at_dispatch': False, 'original_override_negative_rejected': True,
    'original_storage_negative_rejected': True, 'synthetic_gateway_paths': paths,
    'case_temp_removed': not pathlib.Path(temp).exists()}))
''')
        case = {'id': 'controlled-knowledge-dispatch', 'prompt': 'unpaid environment protocol only'}
        out = self.base/'dispatch-case'
        captured, requested = {}, []
        def environment(candidate, state):
            env = self.executor.isolated_environment(candidate, state)
            captured['parent_env'] = env
            return env
        def actual_child(command, cwd, process_out, cap, env):
            requested.append({'argv': command, 'cap': cap})
            self.assertEqual(cap, 140)
            self.assertNotIn('NAVREN_KNOWLEDGE_DB', env)
            parent_env = captured['parent_env']
            self.assertIn('NAVREN_KNOWLEDGE_DB', parent_env)
            self.assertIsNot(env, parent_env)
            keys = ('NAVREN_SIM_DB', 'QINGZHOU_SIM_DB', 'NAVREN_MARKET_DB', 'NAVREN_FUND_CACHE', 'TMPDIR')
            self.assertEqual({key: env[key] for key in keys}, {key: parent_env[key] for key in keys})
            observation = out/'actual-dispatch-environment.json'
            execution = self.executor.execute([sys.executable, '-B', str(worker),
                str(ROOT/runner.finance.LEGACY_PATH), str(observation), parent_env['NAVREN_KNOWLEDGE_DB']],
                cwd, process_out, 2, env)
            self.executions.append(execution)
            return execution
        inputs = {'contract': SimpleNamespace(isolated_environment=environment, execute=actual_child),
                  'sources': {}, 'datasets': {}, 'g3_cases': {'finance': [case]}}
        before = os.environ.get('NAVREN_KNOWLEDGE_DB')
        with patch.dict(os.environ, {'NAVREN_KNOWLEDGE_DB': 'controlled-parent-path-must-not-dispatch'}), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('parent_must_not_connect')):
            actual = runner.execute_case(SimpleNamespace(candidate=ROOT), 'finance', case, inputs, out)
            self.assertEqual(os.environ['NAVREN_KNOWLEDGE_DB'], 'controlled-parent-path-must-not-dispatch')
        self.assertEqual(os.environ.get('NAVREN_KNOWLEDGE_DB'), before)
        observed = json.loads((out/'actual-dispatch-environment.json').read_text())
        self.assertEqual(len(requested), 1)
        self.assertEqual(actual['execution']['status'], 'exited')
        self.assertEqual(actual['execution']['exit_code'], 0)
        self.assertFalse(observed['knowledge_override_present_at_dispatch'])
        self.assertTrue(observed['original_override_negative_rejected'])
        self.assertTrue(observed['original_storage_negative_rejected'])
        self.assertTrue(observed['case_temp_removed'])
        self.assertEqual(observed['model_calls'], 0)
        isolation = json.loads((out/'controller-isolation.json').read_text())
        self.assertNotIn('NAVREN_KNOWLEDGE_DB', isolation['paths'])
        self.assertIn('NAVREN_KNOWLEDGE_DB', isolation['controller_fallback_paths'])
        self.assertEqual(isolation['knowledge_database_selection'], 'original_cli_case_bank_sibling')
        self.assertEqual(isolation['paths_scope'], 'actual_dispatch_environment_only')
        self.assertTrue(all(Path(value).is_relative_to(Path(isolation['paths']['TMPDIR']))
                            for value in observed['synthetic_gateway_paths'].values()))
        self.assertTrue(actual['temporary_state_removed'])
        self.assertEqual(actual['status'], 'stopped')  # This worker never supplies a paid/semantic result.
        self.assertIsNone(actual['actual_total_tokens'])
        self.assertIsNone(actual['actual_model_requests_sent'])
        RECOVERY_OBSERVATIONS.append({'scope': 'unpaid_dispatch_only_not_original_case_execution',
            'requested_original_command_not_executed': requested[0], 'actual_child': actual['execution'],
            'observed_child_protocol': observed, 'controller_isolation': isolation,
            'original_CLI_sha256': runner.public.sha(ROOT/runner.finance.LEGACY_PATH), 'models': 0})

    def test_native_configured_model_and_trace_envelope_identity_are_bound(self):
        configured, turn, runtime, logs, metadata, reviews = self.native_fixture()
        models = {stage: spec['model_id'] for stage, spec in configured.items()}
        checked = runner.validate_native(runtime, logs, metadata, reviews, 'approved', models,
                                         turn, configured)
        self.assertEqual(checked['stop_reasons'], [])
        self.assertEqual(checked['actual_total_tokens'], 37)
        self.assertEqual(checked['actual_model_requests_sent'], 2)
        self.assertEqual(checked['known_tokens_lower_bound'], 37)
        for mutation in ('primary_model', 'reviewer_model', 'missing_id', 'duplicate_stage_id', 'unconfigured_runtime_log'):
            changed_runtime, changed_logs, changed_metadata = deepcopy(runtime), deepcopy(logs), deepcopy(metadata)
            expected_lower = 17
            if mutation == 'primary_model': changed_metadata['primary'][0]['model'] = 'unproven-model-alias'
            elif mutation == 'reviewer_model': changed_metadata['reviewer'][0]['model'] = 'unproven-model-alias'
            elif mutation == 'missing_id': del changed_metadata['primary'][0]['id']
            elif mutation == 'duplicate_stage_id':
                changed_runtime['model_calls'].insert(1, deepcopy(changed_runtime['model_calls'][0]))
                changed_runtime.update(primary_tokens=34, total_tokens=54, budget_tokens=54)
                extra_log = deepcopy(changed_logs[0]); extra_log['timestamp'] = 'controlled-primary-second'
                changed_logs.insert(1, extra_log)
                changed_metadata['primary'].append(deepcopy(changed_metadata['primary'][0]))
                expected_lower = 34
            else:
                changed_runtime['model_calls'][0]['model_id'] = 'unconfigured-model'
                changed_logs[0]['model_id'] = 'unconfigured-model'
                expected_lower = None
            with self.subTest(mutation=mutation):
                rejected = runner.validate_native(changed_runtime, changed_logs, changed_metadata,
                    reviews, 'approved', models, turn, configured)
                self.assertTrue(rejected['stop_reasons'])
                self.assertIsNone(rejected['actual_total_tokens'])
                self.assertIsNone(rejected['actual_model_requests_sent'])
                self.assertEqual(rejected['known_tokens_lower_bound'], expected_lower)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    out = args.out_dir.resolve(); out.mkdir(parents=True, exist_ok=False)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RegressionControllerProtocolTests)
    actual = unittest.TextTestRunner(verbosity=2).run(suite)
    fields = {'tests_run': actual.testsRun, 'minimum_tests': 15, 'failures': len(actual.failures),
              'errors': len(actual.errors), 'skipped': len(actual.skipped), 'expected_failures': len(actual.expectedFailures),
              'unexpected_successes': len(actual.unexpectedSuccesses)}
    complete = actual.wasSuccessful() and actual.testsRun == 15 and all(fields[key] == 0 for key in fields if key not in ('tests_run', 'minimum_tests'))
    code = 0 if complete else 1
    runner.public.atomic_json(out/'test-result.json', {**fields, 'complete': complete, 'planned_exit_code': code,
        'billing_mode': 'unpaid_fixture', 'scope': 'controlled_controller_protocol_only', 'original_22_cases_executed': 0,
        'formal_18_cases_executed': 0, 'cloud_model_calls': 0, 'semantic_pass_count': None,
        'controlled_recovery_observations': RECOVERY_OBSERVATIONS})
    return code


if __name__ == '__main__':
    raise SystemExit(main())
