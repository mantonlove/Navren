"""正式承接器的离线必要条件验收；不执行39项模型语义题。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import ExitStack
import http.server
import tempfile
import threading
import urllib.request
from unittest.mock import patch
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
import formal_business_runner as runner


class FormalBusinessRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = runner.load_contract()
        cls.inputs = runner.frozen_inputs(cls.contract)
        cls.source_before = runner.source_manifest()

    def setUp(self):
        self.out = TEST_OUTPUT / self._testMethodName
        self.out.mkdir(parents=True, exist_ok=False)

    def test_frozen_dataset_expands_13_without_changing_39_denominator(self):
        expanded = runner.expand_cases(self.contract)
        self.assertEqual(len(expanded), 13)
        self.assertEqual(sum(c['id'] == 'FB10' for c, _ in expanded), 4)
        self.assertEqual(self.contract['minimum_design']['planned_attempts'], 39)

    def test_only_original_user_message_enters_normal_http_projection(self):
        step = deepcopy(self.contract['cases'][0]['steps'][0])
        step['grading_only']['secret_oracle'] = {'code': 'MUST_NOT_ENTER_MODEL', 'expected_state': 1234567}
        step['frozen_profile_expectations'] = 'GOLD_SENTINEL'
        request = runner.model_request(step)
        self.assertEqual(request, {'text': step['user_message'], 'mode': 'classic'})
        self.assertNotIn('MUST_NOT_ENTER_MODEL', runner.canonical(request))
        self.assertNotIn('GOLD_SENTINEL', runner.canonical(request))
        with self.assertRaises(runner.Stop):
            runner.model_request({'actor': 'independent_human_harness', 'user_message': '不能送入模型'})

    def test_cross_message_actual_counters_reduce_remaining_agent_quota(self):
        now = [0.0]
        budget = runner.AttemptBudget(clock=lambda: now[0], billing_mode='unpaid_fixture')
        first = budget.remaining()
        budget.consume({'rounds': 3, 'tool_calls': 4, 'budget_tokens': 7000,
                        'usage_complete': True, 'total_tokens': 7000})
        now[0] = 20
        second = budget.remaining()
        self.assertEqual(first, {'seconds': 105, 'rounds': 8, 'tools': 12, 'tokens': 100000})
        self.assertEqual(second, {'seconds': 85, 'rounds': 5, 'tools': 8, 'tokens': 93000})
        budget.consume({'rounds': 2, 'tool_calls': 3, 'budget_tokens': 11000,
                        'usage_complete': True, 'total_tokens': 11000})
        self.assertEqual(budget.remaining()['rounds'], 3)
        self.assertEqual(budget.report()['provider_total_tokens'], 18000)

    def test_unknown_runtime_usage_prevents_next_paid_turn(self):
        budget = runner.AttemptBudget(clock=lambda: 0, billing_mode='unpaid_fixture')
        budget.consume({'rounds': 1, 'tool_calls': 0, 'budget_tokens': 100000,
                        'usage_complete': False, 'total_tokens': None})
        with self.assertRaisesRegex(runner.Stop, 'unknown_usage_stops_subsequent_paid_calls'):
            budget.remaining()
        self.assertIsNone(budget.report()['provider_total_tokens'])

    def test_invalid_runtime_counter_cannot_get_free_remaining_quota(self):
        budget = runner.AttemptBudget(clock=lambda: 0, billing_mode='unpaid_fixture')
        with self.assertRaises(runner.Stop):
            budget.consume({'rounds': True, 'tool_calls': 0, 'budget_tokens': 0,
                            'usage_complete': True, 'total_tokens': 0})
        with self.assertRaises(runner.Stop):
            budget.remaining()

    def test_harness_elapsed_time_shares_attempt_105_seconds(self):
        now = [7.0]
        budget = runner.AttemptBudget(clock=lambda: now[0], billing_mode='unpaid_fixture')
        budget.start()
        now[0] = 111.9
        self.assertLess(budget.remaining()['seconds'], .11)
        now[0] = 112
        with self.assertRaisesRegex(runner.Stop, 'attempt_deadline_exceeded'):
            budget.check_time()

    def test_exact_round_tool_and_token_limits_are_not_reset(self):
        for counters in ({'rounds': 8, 'tool_calls': 0, 'budget_tokens': 2},
                         {'rounds': 1, 'tool_calls': 12, 'budget_tokens': 2},
                         {'rounds': 1, 'tool_calls': 1, 'budget_tokens': 100000}):
            with self.subTest(counters=counters):
                budget = runner.AttemptBudget(clock=lambda: 0, billing_mode='unpaid_fixture')
                budget.consume({**counters, 'usage_complete': True, 'total_tokens': counters['budget_tokens']})
                with self.assertRaises(runner.Stop):
                    budget.remaining()

    def test_cloud_gate_rejects_unverified_or_missing_outer_exit(self):
        with self.assertRaisesRegex(runner.Stop, 'explicit_root_verified'):
            runner.verify_handoff(None, None, self.source_before, self.inputs)
        record = {'attested_by': '/root', 'root_verified': True, 'source_sha256': self.source_before,
                  'data_sha256': self.inputs, 'gates': {'G1': {'passed': True}}}
        path = self.out / 'unverified-handoff.json'
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(runner.Stop, 'root_gate_real_exit_count_or_evidence_missing:G1'):
            runner.verify_handoff(path, runner.sha(path), self.source_before, self.inputs)

    def test_secret_sanitization_retains_financial_amount_and_digest(self):
        value = {'csrf': 'opaque', 'verification_code': '123456', 'operation': {
            'amount_minor': 62500, 'confirmation_digest': 'actual-operation-digest', 'state': 'succeeded'},
            'nested': [{'session_token': 'private'}]}
        result = runner.sanitize(value)
        self.assertNotIn('123456', runner.canonical(result))
        self.assertNotIn('opaque', runner.canonical(result))
        self.assertEqual(result['operation'], value['operation'])

    def test_current_http_core_fixture_matches_closed_raw_seed_and_literal_amounts(self):
        with runner.local_network_only(), runner.Fixture(self.out / 'core', self.contract) as fixture:
            self.assertEqual(fixture.initial_version, 1)
            self.assertEqual(fixture.g.budget(fixture.workspace, fixture.bound['main'])['investable_minor'], 1724400)
            self.assertEqual(fixture.g.overview(fixture.workspace)['total_cash_minor'], 2583400)
            self.assertEqual(fixture.g.overview(fixture.workspace)['holding_principal_minor'], 330000)
            self.assertEqual(len(runner.scoped(fixture.initial, 'transactions', fixture.workspace)), 7)
            self.assertTrue((fixture.out / 'closed-seed-baseline.json').is_file())
            self.assertEqual(len(fixture.task_steps.bindings), 4)
            status, reply = fixture.browser.call('/api/finance/operations/' + '0' * 32)
            self.assertEqual((status, reply['error']['code']), (404, 'NOT_FOUND'))

    def test_all_four_profiles_import_private_source_and_verify_scope(self):
        for variant in self.contract['fixture_contract']['four_profiles_oracle_v1']['profile_variants']:
            with self.subTest(variant=variant), runner.local_network_only(), runner.Fixture(self.out / variant, self.contract, variant) as fixture:
                self.assertEqual(fixture.g.demo_accounts.catalog()['initialized_count'], 4)
                self.assertEqual(fixture.g.demo_accounts.source_dir, fixture.temp / 'profile-source')
                for table in ('cards', 'subscriptions', 'mandates', 'orchestration_tasks', 'service_quotes'):
                    self.assertEqual(runner.scoped(fixture.initial, table, fixture.workspace), [])

    def test_real_http_agent_review_owner_confirm_replay_without_financial_ledger(self):
        with runner.local_network_only():
            result = runner.offline_harness_probe(self.out / 'probe', self.contract)
        self.assertEqual(result['status'], 'offline_protocol_verified')
        self.assertEqual(result['cloud_calls'], 0)
        self.assertEqual(result['formal_task_attempts'], 0)
        self.assertEqual(result['unique_execution_audit'], 1)
        self.assertEqual(result['new_financial_ledger'], 0)
        self.assertEqual(result['server_owned_history_turns'], 2)
        self.assertFalse(result['long_term_memory_enabled'])
        self.assertTrue(result['normal_http_history_persistence_verified'])
        self.assertEqual(result['budget']['rounds'], 4)
        self.assertEqual(result['budget']['provider_total_tokens'], 12)
        self.assertIsNone(result['semantic_pass'])

    def test_frozen_negative_prepare_probe_never_supplies_model_draft(self):
        case = next(c for c in self.contract['cases'] if c['id'] == 'FB05')
        step = next(s for s in case['steps'] if s['id'] == 'wrong-account')
        with runner.local_network_only(), runner.Fixture(self.out / 'negative', self.contract) as fixture:
            fixture.budget.start()
            status, reply, actor = runner.HumanHarness(fixture).perform(step)
            self.assertEqual((status, reply['error']['code']), (404, 'NOT_FOUND'))
            self.assertEqual(actor['actor'], 'independent_negative_http_probe')
            self.assertFalse(actor['model_semantic_score_eligible'])
            self.assertEqual(runner.scoped(runner.raw_snapshot(fixture.g.bank), 'operations', fixture.workspace), [])
            self.assertEqual(fixture.tool_records, [])

    def test_formal_multi_turn_stops_before_model_without_current_history_preflight_proof(self):
        class NeverCall:
            model_id, billing_mode = 'must-never-be-called', 'paid'
            def complete(self, *args, **kwargs):
                raise AssertionError('disabled history must stop before a paid model request')
        with runner.local_network_only():
            result = runner.run_attempt(self.out / 'not-run', self.contract, self.contract['cases'][0], None,
                 NeverCall(), runner.OfflineReviewer(), runner.source_manifest(), self.inputs)
        self.assertEqual(result['status'], 'not_run')
        self.assertIn('normal_history_preflight_evidence_missing', result['reason'])
        self.assertEqual(result['budget']['provider_total_tokens'], 0)
        self.assertEqual(len(result['steps']), len(self.contract['cases'][0]['steps']))
        self.assertTrue(all(s['status'] == 'not_run' for s in result['steps']))

    @staticmethod
    def task_step_rows():
        # 受控原始行夹具；四个独立字面量保留45800计划金额，不从执行器逻辑制造预期。
        rows = [
            {'id': 'budget-row', 'workspace_id': 'owner', 'task_id': 'birthday', 'sequence': 1,
             'action': 'check_budget', 'amount_minor': 0, 'dependency_id': None, 'status': 'planned'},
            {'id': 'hold-row', 'workspace_id': 'owner', 'task_id': 'birthday', 'sequence': 2,
             'action': 'reserve_funds', 'amount_minor': 45800, 'dependency_id': 'budget-row', 'status': 'planned'},
            {'id': 'flowers-row', 'workspace_id': 'owner', 'task_id': 'birthday', 'sequence': 3,
             'action': 'book_flowers', 'amount_minor': 19900, 'dependency_id': 'hold-row', 'status': 'planned'},
            {'id': 'cake-row', 'workspace_id': 'owner', 'task_id': 'birthday', 'sequence': 4,
             'action': 'book_cake', 'amount_minor': 25900, 'dependency_id': 'hold-row', 'status': 'planned'},
        ]
        return {'task_steps': rows}

    def test_v2_exact_four_row_vectors_and_per_step_transitions(self):
        supplement = runner.load_task_steps_supplement()
        raw = self.task_step_rows()
        oracle = runner.TaskStepOracle(raw, 'owner', 'birthday', supplement)
        for matrix in supplement['step_status_matrix']:
            with self.subTest(case=matrix['case'], step=matrix['step']):
                before, after = deepcopy(raw), deepcopy(raw)
                for row in before['task_steps']:
                    row['status'] = matrix['expected_statuses_before_step'][row['action']]
                for row in after['task_steps']:
                    row['status'] = matrix['expected_cumulative_statuses'][row['action']]
                result = oracle.check(before, after, matrix['case'], matrix['step'])
                self.assertEqual(result['status'], 'exact_four_row_contract_satisfied')
                self.assertEqual(result['initial_row_bindings']['birthday_step_hold'], 'hold-row')

    def test_v2_hold_confirm_cannot_authorize_same_task_other_status_writes(self):
        raw = self.task_step_rows()
        oracle = runner.TaskStepOracle(raw, 'owner', 'birthday', runner.load_task_steps_supplement())
        after = deepcopy(raw)
        after['task_steps'][1]['status'] = 'succeeded'
        after['task_steps'][3]['status'] = 'succeeded'
        with self.assertRaisesRegex(runner.Stop, 'exact_cumulative_vector_mismatch'):
            oracle.check(raw, after, 'FB07', 'reserve-confirm')

    def test_v2_original_identity_dependency_and_plan_amount_are_immutable(self):
        raw = self.task_step_rows()
        oracle = runner.TaskStepOracle(raw, 'owner', 'birthday', runner.load_task_steps_supplement())
        changes = {'id': 'new-hold-row', 'workspace_id': 'foreign-owner', 'task_id': 'same-title-other-task',
                   'sequence': 9, 'action': 'check_budget', 'amount_minor': 30000, 'dependency_id': 'cake-row'}
        for key, value in changes.items():
            with self.subTest(field=key):
                after = deepcopy(raw)
                after['task_steps'][1].update(status='succeeded', **{key: value})
                with self.assertRaises(runner.Stop):
                    oracle.check(raw, after, 'FB08', 'reserve-confirm')

    def test_v2_replay_and_release_cannot_reset_completed_hold_or_insert_delete_rows(self):
        raw = self.task_step_rows()
        oracle = runner.TaskStepOracle(raw, 'owner', 'birthday', runner.load_task_steps_supplement())
        before = deepcopy(raw)
        before['task_steps'][1]['status'] = 'succeeded'
        after = deepcopy(before)
        for index in (0, 2, 3):
            after['task_steps'][index]['status'] = 'cancelled'
        oracle.check(before, after, 'FB08', 'release-confirm')
        changed_hold = deepcopy(after)
        changed_hold['task_steps'][1]['status'] = 'cancelled'
        with self.assertRaisesRegex(runner.Stop, 'exact_cumulative_vector_mismatch'):
            oracle.check(after, changed_hold, 'FB08', 'replay')
        for changed in (after['task_steps'][:-1], after['task_steps'] + [{**after['task_steps'][-1], 'id': 'extra-row'}]):
            with self.subTest(row_count=len(changed)), self.assertRaisesRegex(runner.Stop, 'row_insertion_deletion'):
                oracle.check(after, {'task_steps': changed}, 'FB08', 'replay')


class FormalPaidAccountingProtocolTests(unittest.TestCase):
    """控制合成paid协议形状，不实例化业务/供应商；整个测试执行免费。"""
    MODELS = {'primary': {'provider': 'ai_ping', 'model_id': 'DeepSeek-V4-Flash'},
              'reviewer': {'provider': 'ai_ping', 'model_id': 'GLM-5.3-Flash'}}

    def setUp(self):
        self.out = TEST_OUTPUT/self._testMethodName
        self.out.mkdir(parents=True, exist_ok=False)

    def budget(self):
        return runner.AttemptBudget(clock=lambda: 0, billing_mode='paid', configured_models=deepcopy(self.MODELS))

    def turn(self, ident='controlled-http-1', *, limit=100000, failed=None, review=True):
        primary = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        reviewer = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
        stages = [('primary', primary)] + ([('reviewer', reviewer)] if review else [])
        calls, logs, metadata, reviews = [], [], {'primary': [], 'reviewer': []}, []
        for stage, usage in stages:
            failure = failed if stage == 'primary' else None
            calls.append({'stage': stage, 'model_id': self.MODELS[stage]['model_id'], 'billing_mode': 'paid',
                'usage': deepcopy(usage), 'usage_complete': True, 'request_sent': True, 'attempts': 1,
                'outcome': 'failed' if failure else 'completed', 'error_code': failure})
            log = {'stage': 'conversation.tools' if stage == 'primary' else 'reviewer.conversation',
                'provider': self.MODELS[stage]['provider'], 'model_id': self.MODELS[stage]['model_id'],
                'request_id': ident, 'outcome': calls[-1]['outcome'], 'error_code': failure or '', 'http_status': 200}
            if stage == 'primary':
                log['usage'] = deepcopy(usage)
            else:
                log.update(attempts=1, finish_reason='stop')
                reviews.append({'packet': {'scope': 'controlled_unpaid_protocol_shape'},
                    'decision': {'verdict': 'approve', '_model_usage': deepcopy(usage)}})
            logs.append(log)
            metadata[stage].append({'model': self.MODELS[stage]['model_id'], 'id': 'controlled-response-' + stage + '-' + ident})
        total = 7 if review else 5
        runtime = {'rounds': 1, 'tool_calls': 1, 'budget_tokens': total, 'total_tokens': total,
            'primary_tokens': 5, 'reviewer_tokens': 2 if review else 0, 'usage_complete': True,
            'duration_ms': 1, 'budget_policy': {'limit': limit}, 'model_calls': calls,
            'failure': failed, 'budget_limit': False}
        return runtime, {'logs': logs, 'turn_id': ident, 'metadata': metadata, 'reviews': reviews}

    def stopped_schedule(self, work):
        launched = []
        observed_budget = None
        def run_one(target, case, variant, deadline):
            nonlocal observed_budget
            launched.append(case['id'])
            observed_budget = self.budget()
            try:
                work(observed_budget)
            except runner.Stop as error:
                return {'case_id': case['id'], 'variant': variant, 'status': error.classification,
                    'reason': error.reason, 'batch_stop': error.batch_stop, 'budget': observed_budget.report(), 'semantic_pass': None}
            self.fail('controlled fatal input must stop before next attempt')
        result = runner.execute_formal_schedule(self.out/'schedule', [({'id': 'controlled-A'}, None),
            ({'id': 'controlled-B'}, None)], {'core_fixture_guard_seconds': 0, 'profile_fixture_guard_seconds': 0}, run_one)
        self.assertEqual(launched, ['controlled-A'])
        self.assertEqual(len(result['attempts']), 6)
        self.assertTrue(result['batch_stopped'])
        self.assertEqual(result['status'], 'stopped_paid_batch')
        self.assertTrue(all(row['status'] == 'not_run' for row in result['attempts'][1:]))
        self.assertIsNone(result['scores'])
        runner.save(self.out/'controlled-protocol-result.json', {'scope': 'unpaid_protocol_shape_only',
                    'cloud_calls': 0, 'formal39_attempts': 0, 'actual_provider_usage': None,
                    'synthetic_schedule': result})
        return observed_budget, result

    def test_two_http_offsets_remaining_policy_and_duplicate_accounting(self):
        budget = self.budget()
        first, args = self.turn()
        p, r = self.out/'primary.jsonl', self.out/'reviewer.jsonl'
        p.write_text(runner.canonical(args['logs'][0])+'\n')
        r.write_text(runner.canonical(args['logs'][1])+'\n')
        primary = SimpleNamespace(log_path=p, response_metadata=deepcopy(args['metadata']['primary']))
        reviewer = SimpleNamespace(client=SimpleNamespace(log_path=r),
                    response_metadata=deepcopy(args['metadata']['reviewer']), records=deepcopy(args['reviews']))
        budget.transports = {'primary': primary, 'reviewer': reviewer}
        logs, metadata, reviews, evidence = budget.capture_turn(first, args['turn_id'])
        budget.consume(first, logs=logs, turn_id=args['turn_id'], metadata=metadata, reviews=reviews, evidence=evidence)
        self.assertEqual(budget.remaining()['tokens'], 99993)
        second, other = self.turn('controlled-http-2', limit=99993)
        with p.open('a') as stream: stream.write(runner.canonical(other['logs'][0])+'\n')
        with r.open('a') as stream: stream.write(runner.canonical(other['logs'][1])+'\n')
        primary.response_metadata += other['metadata']['primary']
        reviewer.response_metadata += other['metadata']['reviewer']; reviewer.records += other['reviews']
        logs2, metadata2, reviews2, evidence2 = budget.capture_turn(second, other['turn_id'])
        self.assertEqual(len(logs2), 2)
        self.assertEqual(metadata2, other['metadata'])
        self.assertTrue(all(row['before_offset'] == 1 and row['after_offset'] == 2 for row in evidence2['log_files']))
        budget.consume(second, logs=logs2, turn_id=other['turn_id'], metadata=metadata2, reviews=reviews2, evidence=evidence2)
        budget.consume(second, logs=logs2, turn_id=other['turn_id'], metadata=metadata2, reviews=reviews2, evidence=evidence2)
        self.assertEqual(budget.provider_tokens, 14)
        self.assertEqual(budget.report()['actual_provider_total_tokens'], 14)
        self.assertEqual(budget.remaining()['tokens'], 99986)
        self.assertEqual(len(budget.observations), 2)

    def test_complete_flag_cannot_hide_invalid_native_math_and_integer_types(self):
        for value in (True, 3.0, -3, 99):
            with self.subTest(value=value):
                def invalid(budget):
                    runtime, args = self.turn(review=False)
                    runtime['model_calls'][0]['usage']['prompt_tokens'] = value
                    args['logs'][0]['usage']['prompt_tokens'] = value
                    budget.consume(runtime, **args)
                budget, _ = self.stopped_schedule(invalid)
                self.assertIsNone(budget.report()['actual_provider_total_tokens'])
                # 对矛盾原生计数不伪造实际0或重新修正total。
                self.assertTrue(budget.unknown_usage)

    def test_known_length_failure_is_counted_once_and_stops_all_later_batches(self):
        def length(budget):
            first, args = self.turn('controlled-good')
            budget.consume(first, **args)
            failed, failed_args = self.turn('controlled-truncated', limit=99993, failed='response_truncated', review=False)
            try:
                budget.consume(failed, **failed_args)
            except runner.Stop:
                budget.consume(failed, **failed_args)  # 重复观察同一失败回执不能重复计费。
                raise
        budget, _ = self.stopped_schedule(length)
        self.assertEqual(budget.report()['actual_provider_total_tokens'], 12)
        self.assertEqual(budget.report()['known_tokens_lower_bound'], 12)
        self.assertEqual(len(budget.observations), 2)
        with self.assertRaises(runner.Stop): budget.remaining()

    def test_partial_unknown_after_known_http_keeps_lower_bound_and_blocks_next_attempt(self):
        def unknown(budget):
            first, args = self.turn('controlled-good')
            budget.consume(first, **args)
            runtime, args = self.turn('controlled-unknown', limit=99993, review=False)
            runtime.update(usage_complete=False, primary_tokens=None, total_tokens=None)
            runtime['model_calls'][0].update(usage_complete=False, usage={'prompt_tokens': 3})
            args['logs'][0]['usage'] = {'prompt_tokens': 3}
            budget.consume(runtime, **args)
        budget, _ = self.stopped_schedule(unknown)
        self.assertIsNone(budget.report()['actual_provider_total_tokens'])
        self.assertEqual(budget.report()['known_tokens_lower_bound'], 10)
        self.assertEqual(budget.provider_tokens, 7)

    def test_actual_log_and_trace_identity_errors_or_missing_runtime_stop_schedule(self):
        for mode in ('wrong_turn', 'wrong_model', 'missing_response_id', 'truncated_log_no_runtime'):
            with self.subTest(mode=mode):
                def wrong(budget):
                    runtime, args = self.turn(review=False)
                    if mode == 'wrong_turn': args['logs'][0]['request_id'] = 'different-http'
                    elif mode == 'wrong_model': args['logs'][0]['model_id'] = 'different-model'
                    elif mode == 'missing_response_id': args['metadata']['primary'][0]['id'] = ''
                    else:
                        path = self.out/'malformed.jsonl'
                        path.write_text(runner.canonical(args['logs'][0])+'\n{bad-json\n')
                        primary = SimpleNamespace(log_path=path, response_metadata=args['metadata']['primary'])
                        reviewer = SimpleNamespace(client=SimpleNamespace(log_path=path), response_metadata=[], records=[])
                        budget.transports={'primary':primary,'reviewer':reviewer}
                        logs, metadata, reviews, evidence = budget.capture_turn(None, args['turn_id'])
                        self.assertEqual(len(logs), 2)
                        self.assertIsNone(logs[-1])
                        budget.consume(None, logs=logs, turn_id=args['turn_id'], metadata=metadata, reviews=reviews, evidence=evidence)
                    budget.consume(runtime, **args)
                budget, _ = self.stopped_schedule(wrong)
                self.assertTrue(budget.batch_stop)
                self.assertIsNone(budget.report()['actual_provider_total_tokens'])
                if mode == 'truncated_log_no_runtime':
                    self.assertEqual(budget.report()['known_tokens_lower_bound'], 5)
                    self.assertIsNone(budget.report()['actual_requests_sent'])
                    self.assertTrue(budget.observations[0]['transport_evidence']['log_files'])

        # 泛异常先闩，再只读未入账前缀；晚到结果不能重复下界或放开下一paid调用。
        budget = self.budget()
        first, args = self.turn('controlled-before-timeout')
        budget.consume(first, **args)
        late, late_args = self.turn('controlled-late-http', limit=99993, review=False)
        p, r = self.out/'late-primary.jsonl', self.out/'late-reviewer.jsonl'
        partial_log = deepcopy(late_args['logs'][0]); partial_log['usage'] = {'prompt_tokens': 3}
        p.write_text(runner.canonical(args['logs'][0])+'\n'+runner.canonical(partial_log)+'\n')
        r.write_text(runner.canonical(args['logs'][1])+'\n')
        invoked = []
        primary = SimpleNamespace(log_path=p, response_metadata=late_args['metadata']['primary'],
                                  complete=lambda *a, **kw: invoked.append('paid-primary'))
        reviewer = SimpleNamespace(client=SimpleNamespace(log_path=r), response_metadata=[], records=[],
                                   review=lambda *a, **kw: invoked.append('paid-reviewer'))
        budget.transports = {'primary': primary, 'reviewer': reviewer}
        budget.log_offsets = {str(p): 1, str(r): 1}
        before_offsets = deepcopy(budget.log_offsets)
        budget.preserve_unaccounted_failure('controlled_http_timeout_before_finalize')
        budget.preserve_unaccounted_failure('controlled_http_timeout_before_finalize')
        self.assertEqual(budget.log_offsets, before_offsets)
        self.assertEqual(budget.report()['known_tokens_lower_bound'], 10)
        self.assertEqual(len(budget.failure_prefixes), 1)
        self.assertEqual(budget.failure_prefixes[0]['log_files'][0]['new_raw_logs'], [partial_log])
        self.assertIsNone(budget.report()['actual_provider_total_tokens'])
        self.assertIsNone(budget.report()['actual_requests_sent'])
        p.write_text(runner.canonical(args['logs'][0])+'\n'+runner.canonical(late_args['logs'][0])+'\n')
        with self.assertRaises(runner.Stop):
            budget.account_result({'agent_runtime': late, 'bank_response_id': late_args['turn_id']})
        self.assertEqual(budget.provider_tokens, 12)
        self.assertEqual(budget.report()['known_tokens_lower_bound'], 12)
        self.assertTrue(budget.batch_stop)
        for client, method in ((primary, 'complete'), (reviewer, 'review')):
            with self.assertRaises(runner.Stop):
                getattr(runner.guarded_paid_transport(client, budget), method)()
        self.assertEqual(invoked, [])
        with self.assertRaises(runner.Stop): budget.remaining()
        expired = self.budget(); expired.started = 0; expired.clock = lambda: 106
        with self.assertRaises(runner.Stop):
            runner.guarded_paid_transport(primary, expired).complete()
        self.assertTrue(expired.batch_stop)
        self.assertEqual(invoked, [])
        runner.save(self.out/'generic-exception-prefix-result.json', {'scope': 'unpaid_protocol_shape_only',
                    'cloud_calls': 0, 'original18_executed': 0, 'original39_executed': 0,
                    'synthetic_budget': budget.report()})

        # 真loopback请求线程仍占用临时状态时，真实Fixture.close必须等待或保留。
        for mode in ('normal', 'cleanup_cap', 'cleanup_crosses105'):
            with self.subTest(loopback=mode):
                entered, release = threading.Event(), threading.Event()
                request_errors, close_errors, stack_observations = [], [], []
                class Handler(http.server.BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def do_GET(self):
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError('controlled_request_release_missing')
                        self.send_response(200); self.end_headers(); self.wfile.write(b'controlled-no-model')
                fixture = runner.Fixture.__new__(runner.Fixture)
                fixture.out = self.out/('loopback-'+mode); fixture.out.mkdir()
                fixture.temp_directory = tempfile.TemporaryDirectory(prefix='private-', dir=fixture.out)
                fixture.stack = ExitStack(); fixture.tmp = fixture.stack.enter_context(fixture.temp_directory)
                fixture.stack.callback(lambda: stack_observations.append(release.is_set()))
                env_marker = 'NAVREN_FORMAL_CONTROLLED_LIFECYCLE'
                env_before = os.environ.get(env_marker)
                fixture.stack.enter_context(patch.dict(os.environ, {env_marker: fixture.tmp}))
                fixture.budget = self.budget(); fixture.closed = False; fixture.cleanup_report = None
                fixture.precheck_finished = fixture.construction_started = None
                fixture.variant, fixture.history_prelude = None, False
                if mode == 'cleanup_crosses105':
                    logical_now = [104.5]
                    fixture.budget.started = 0
                    fixture.budget.clock = lambda: logical_now[0]
                fixture.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                fixture.handlers = runner.HandlerLifecycle(fixture.server)
                fixture.thread = threading.Thread(target=lambda: fixture.server.serve_forever(poll_interval=.005), daemon=True)
                fixture.thread.start()
                def request():
                    try:
                        with urllib.request.urlopen('http://127.0.0.1:'+str(fixture.server.server_port), timeout=2) as response:
                            response.read()
                    except Exception as error:
                        request_errors.append(type(error).__name__)
                client_thread = threading.Thread(target=request, daemon=True); client_thread.start()
                self.assertTrue(entered.wait(1))
                def close():
                    try: fixture.close()
                    except Exception as error: close_errors.append(error)
                try:
                    if mode == 'cleanup_cap':
                        with patch.object(runner, 'CLEANUP_SECONDS', .05): close()
                        self.assertEqual(len(close_errors), 1)
                        self.assertIsInstance(close_errors[0], runner.Stop)
                        self.assertTrue(fixture.budget.batch_stop)
                        self.assertTrue(fixture.budget.unknown_usage)
                        self.assertFalse(fixture.cleanup_report['cleanup_completed'])
                        self.assertTrue(fixture.cleanup_report['retained_state'])
                        self.assertTrue(fixture.cleanup_report['private_temp_exists'])
                        self.assertTrue(Path(fixture.tmp).exists())
                        self.assertEqual(stack_observations, [])
                        self.assertEqual(os.environ.get(env_marker), fixture.tmp)
                        self.assertFalse(fixture.cleanup_report['handler_state']['all_admitted_done'])
                    else:
                        closer = threading.Thread(target=close, daemon=True); closer.start()
                        closer.join(.03)
                        self.assertTrue(closer.is_alive())
                        self.assertTrue(Path(fixture.tmp).exists())
                        self.assertEqual(stack_observations, [])
                        if mode == 'cleanup_crosses105':
                            self.assertLess(fixture.budget.clock(), 105)
                            self.assertFalse(fixture.budget.batch_stop)
                            logical_now[0] = 105.1
                        release.set(); closer.join(1)
                        self.assertFalse(closer.is_alive())
                        self.assertEqual(close_errors, [])
                        self.assertTrue(fixture.cleanup_report['cleanup_completed'])
                        self.assertTrue(fixture.cleanup_report['handler_state']['all_admitted_done'])
                        self.assertEqual(fixture.cleanup_report['handler_state']['admitted_count'], 1)
                        self.assertTrue(all(row['done'] and not row['alive'] for row in fixture.cleanup_report['handler_state']['handlers']))
                        self.assertEqual(stack_observations, [True])
                        self.assertEqual(os.environ.get(env_marker), env_before)
                        self.assertFalse(Path(fixture.tmp).exists())
                        self.assertEqual(fixture.budget.batch_stop, mode == 'cleanup_crosses105')
                    runner.save(fixture.out/'observed-cleanup-result.json', {'scope': 'actual_loopback_no_model',
                        'cloud_calls': 0, 'original18_executed': 0, 'original39_executed': 0,
                        'cleanup': fixture.cleanup_report, 'budget': fixture.budget.report(),
                        'controlled_env_before': env_before, 'controlled_env_observed': os.environ.get(env_marker),
                        'env_scope': 'dedicated_non_secret_lifecycle_marker_only'})
                finally:
                    release.set(); client_thread.join(1)
                    if mode == 'cleanup_cap':
                        terminal = fixture.handlers.wait(runner.time.monotonic()+1)
                        self.assertTrue(terminal['all_admitted_done'])
                        fixture.server.server_close(); fixture.stack.close(); fixture.temp_directory.cleanup()
                        runner._RETAINED_FIXTURES.remove(fixture)
                        runner.save(fixture.out/'controlled_post_receipt_cleanup.json', {'actual_handler_terminal': terminal,
                            'private_temp_exists': Path(fixture.tmp).exists(), 'test_cleanup_after_observed_retention': True})
                self.assertEqual(os.environ.get(env_marker), env_before)
                self.assertFalse(client_thread.is_alive())
                self.assertEqual(request_errors, [])

    def test_critical_model_failure_overrun_or_budget_reset_stops_before_next_paid_factory(self):
        for mode in ('critical', 'overrun', 'reset_remaining_policy'):
            with self.subTest(mode=mode):
                def bad(budget):
                    first, args = self.turn('controlled-first')
                    budget.consume(first, **args)
                    runtime, args = self.turn('controlled-second', limit=100000 if mode == 'reset_remaining_policy' else 99993,
                                              failed='provider_unavailable' if mode == 'critical' else None, review=False)
                    if mode == 'overrun': runtime['rounds'] = 8
                    budget.consume(runtime, **args)
                budget, _ = self.stopped_schedule(bad)
                self.assertTrue(budget.batch_stop)
                self.assertEqual(budget.provider_tokens, 12)


def paid_accounting_protocol_main(out):
    global TEST_OUTPUT
    TEST_OUTPUT = Path(out).resolve()
    permitted = runner.paid_protocol().OUTPUT_ROOT/'stage-continuation-preflight/formal-paid-accounting'
    if not TEST_OUTPUT.is_relative_to(permitted.resolve()) or TEST_OUTPUT == permitted.resolve():
        raise SystemExit('paid_protocol_test_output_outside_authorized_scope')
    TEST_OUTPUT.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    own_paths = [Path(__file__), Path(runner.__file__), Path(runner.__file__).with_name('public_probe_runner.py')]
    before = {str(path): runner.sha(path) for path in own_paths}
    with (TEST_OUTPUT/'unittest.log').open('w') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(FormalPaidAccountingProtocolTests))
    after = {str(path): runner.sha(path) for path in own_paths}
    fields = {'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures), 'unexpected_successes': len(result.unexpectedSuccesses)}
    complete = result.wasSuccessful() and result.testsRun == 6 and all(fields[key] == 0 for key in fields if key != 'tests_run') and before == after
    code = 0 if complete else 1
    runner.save(TEST_OUTPUT/'test-result.json', {**fields, 'minimum_tests': 6, 'complete': complete, 'planned_exit_code': code,
        'source_before': before, 'source_after': after, 'scope': 'unpaid_controlled_paid_protocol_shape_only',
        'billing_mode': 'unpaid_fixture', 'cloud_calls': 0, 'original18_executed': 0, 'original39_executed': 0,
        'actual_model_usage': None, 'actual_provider_cost': None, 'semantic_pass': None, 'elapsed_seconds': time.monotonic()-started})
    print(runner.canonical({'exit_code': code, 'test_count': result.testsRun, 'scope': 'new6_only', 'cloud_calls': 0}))
    return code


def main():
    global TEST_OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--paid-accounting-protocol', action='store_true', help='只收新增6个免费协议负控，不运行原18')
    args = parser.parse_args()
    if args.paid_accounting_protocol:
        return paid_accounting_protocol_main(args.out_dir)
    TEST_OUTPUT = args.out_dir.resolve()
    if runner.OUTPUT.resolve() not in TEST_OUTPUT.parents:
        raise SystemExit('test_output_outside_authorized_scope')
    TEST_OUTPUT.mkdir(parents=True, exist_ok=False)
    before = runner.source_manifest()
    started = time.monotonic()
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(FormalBusinessRunnerTests),
                              unittest.defaultTestLoader.loadTestsFromTestCase(FormalPaidAccountingProtocolTests)])
    with (TEST_OUTPUT / 'unittest.log').open('w') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    after = runner.source_manifest()
    complete = (result.wasSuccessful() and result.testsRun == 24 and not result.skipped
                and not result.expectedFailures and not result.unexpectedSuccesses and before == after)
    code = 0 if complete else 1
    counts_report = {'outer_exit_code': code, 'test_count': result.testsRun,
         'failures': len(result.failures), 'errors': len(result.errors), 'skip_count': len(result.skipped),
         'tests_run': result.testsRun, 'minimum_tests': 24, 'complete': complete,
         'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures),
         'unexpected_successes': len(result.unexpectedSuccesses),
         'skipped_cases': [{'id': test.id(), 'reason': reason} for test, reason in result.skipped],
         'planned_exit_code': code,
         'source_before': before, 'source_after': after, 'source_drift': before != after,
         'elapsed_seconds': time.monotonic() - started, 'cloud_calls': 0, 'formal_task_attempts': 0,
         'semantic_scores': None}
    # Save the actual TestResult before touching setup-derived fixture evidence.
    # A setUpClass error has real errors/short collection but no initialized inputs.
    runner.save(TEST_OUTPUT / 'test-result.json', counts_report)
    try:
        inputs = getattr(FormalBusinessRunnerTests, 'inputs', None)
        contract = getattr(FormalBusinessRunnerTests, 'contract', None)
        fixture_timings = [json.loads(p.read_text()) for p in TEST_OUTPUT.rglob('fixture-timing.json')]
        measured = [r for r in fixture_timings if r['status'] == 'verified']
        core_max = max((r['construction_precheck_seconds'] + r['teardown_seconds'] for r in measured if r['variant'] is None), default=None)
        profile_max = max((r['construction_precheck_seconds'] + r['teardown_seconds'] for r in measured if r['variant'] is not None), default=None)
        probe_path = TEST_OUTPUT / 'test_real_http_agent_review_owner_confirm_replay_without_financial_ledger/probe/probe-result.json'
        probe = json.loads(probe_path.read_text()) if probe_path.is_file() else None
        all_variants = set(contract['fixture_contract']['four_profiles_oracle_v1']['profile_variants']) if contract is not None else None
        validated = (code == 0 and isinstance(inputs, dict) and bool(inputs)
                     and core_max is not None and core_max > 0 and profile_max is not None
                     and all_variants is not None and {r['variant'] for r in measured if r['variant']} == all_variants)
        guarded_round = 2 * core_max * 9 + 2 * profile_max * 4 if core_max is not None and profile_max is not None else None
        runner.save(TEST_OUTPUT / 'budget-precheck.json', {
            'run_label': runner.RUN_LABEL, 'history_prelude': False,
            'source_sha256': before, 'data_sha256': inputs,
            'fixture_precondition_satisfied': validated,
            'normal_http_history_persistence_verified': validated and isinstance(probe, dict) and probe.get('normal_http_history_persistence_verified') is True,
            'history_probe_evidence_path': str(probe_path.relative_to(ROOT)),
            'history_probe_evidence_sha256': runner.sha(probe_path) if probe_path.is_file() else None,
            'fixture_timings': fixture_timings,
            'core_fixture_guard_seconds': 2 * core_max if core_max is not None else None,
            'profile_fixture_guard_seconds': 2 * profile_max if profile_max is not None else None,
            'guarded_round_fixture_overhead_seconds': guarded_round,
            'overhead_method': 'twice maximum observed isolated core/profile construction, full literal precheck and cleanup; same factories expanded 9+4',
            'per_stage_remaining_seconds': 435, 'whole_remaining_seconds': 3105,
            'fits_observed_budget': validated and guarded_round is not None and guarded_round <= 435 and guarded_round * 3 <= 3105,
            'precheck_status': 'validated' if validated else 'not_validated_from_actual_test_run',
            'provider_usage_and_cost': None, 'formal_attempts': 0, 'cloud_calls': 0,
        })
    except Exception as error:
        code = 1
        counts_report.update(outer_exit_code=code, planned_exit_code=code, complete=False,
                             precheck_receipt_error={'type': type(error).__name__, 'detail': str(error)})
        runner.save(TEST_OUTPUT / 'test-result.json', counts_report)
        runner.save(TEST_OUTPUT / 'budget-precheck.json', {
            'run_label': runner.RUN_LABEL, 'history_prelude': False,
            'source_sha256': before, 'data_sha256': None,
            'fixture_precondition_satisfied': False, 'fits_observed_budget': False,
            'normal_http_history_persistence_verified': False,
            'core_fixture_guard_seconds': None, 'profile_fixture_guard_seconds': None,
            'guarded_round_fixture_overhead_seconds': None,
            'precheck_status': 'receipt_generation_failed',
            'receipt_error': counts_report['precheck_receipt_error'],
            'provider_usage_and_cost': None, 'formal_attempts': 0, 'cloud_calls': 0,
        })
    print(runner.canonical({'exit_code': code, 'count': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skip_count': len(result.skipped), 'source_drift': before != after,
        'out_dir': str(TEST_OUTPUT)}), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
