"""G2四题执行器必要硬门/熔断契约；不启动原题、不调用模型或网络。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import finance_probe_runner as runner


PAID_FAILURE_CASES = ('not_sent_one_attempt', 'missing_model', 'wrong_log_turn',
                      'wrong_log_model', 'invalid_log_usage', 'mismatched_log_usage',
                      'review_attempt_mismatch', 'partial_failed_usage', 'missing_turn',
                      'missing_review_observation', 'mismatched_review_usage', 'boolean_review_usage')


class FinanceProbeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-finance-gate-unit-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.net = patch.object(socket.socket, 'connect', side_effect=AssertionError('unit_must_not_connect'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.contract = runner.read_json(runner.ROOT / runner.CONTRACT_PATH)
        self.dataset = runner.read_json(runner.ROOT / runner.DATASET_PATH)

    def write(self, name, value):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def good(self):
        ident, prompt = runner.ORDER[0], 'unit protocol prompt; not a financial task execution'
        sources = {runner.LEGACY_PATH: runner.public.sha(runner.ROOT/runner.LEGACY_PATH), runner.DATASET_PATH: runner.DATASET_SHA}
        usage = {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}
        review = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
        turn_id = 'synthetic-unit-turn-not-an-actual-model-request'
        models = {stage:identity['model_id'] for stage,identity in runner.public.MODEL_IDENTITIES.items()}
        calls = [{'stage': stage, 'usage': counters, 'usage_complete': True, 'billing_mode': 'paid',
                  'model_id': models[stage], 'request_sent': True, 'attempts': 1,
                  'outcome': 'completed', 'error_code': None}
                 for stage, counters in (('primary', usage), ('reviewer', review))]
        runtime = {'rounds': 1, 'tool_calls': 1, 'budget_tokens': 6, 'duration_ms': 1000,
                   'primary_tokens': 4, 'reviewer_tokens': 2, 'total_tokens': 6, 'usage_complete': True,
                   'budget_policy': {'limit': 100000}, 'model_calls': calls,
                   'trace': [{'tool': 'budget', 'status': 'completed'}], 'failure': None, 'budget_limit': None}
        state = {'total_cash_minor': 100, 'profile': {'risk_level': 2}, 'orders': [], 'holdings': [],
                 'fund_holds': [], 'memories': [], 'observations': 0}
        row = {'id': ident, 'question': prompt, 'http_status': 200, 'technical_completed': True,
               'result': {'reply': 'unit fixture', 'bank_response_id': turn_id, 'isFallback': False,
                          'agent_review': {'status': 'approved'}, 'agent_runtime': runtime},
               'before': state, 'after': deepcopy(state), 'no_financial_execution': True, 'no_action_cards': True,
               'profile_unchanged': True, 'observation_delta': 0, 'memory_delta': 0,
               'independent_reviews': [{'scope': 'synthetic unit observation; no actual model call',
                                        'decision': {'verdict': 'approve', '_model_usage': review}}],
               'response_metadata': {stage:[{'model':models[stage],'id':'synthetic-response-'+stage}]
                                     for stage in ('primary','reviewer')},
               'oracle': [{'passed': True, 'scope': 'unit fixture; no original oracle or model task run'}]}
        report = {'offline': False, 'tested_source_root': str(self.base.resolve()), 'selected_case_ids': [ident], 'expected_case_count': 1,
                  'collection_complete': True, 'complete': True, 'run_status': 'collection_complete', 'records': [row],
                  'source_sha256': sources, 'final_source_sha256': sources, 'source_unchanged': True,
                  'dataset_sha256': runner.DATASET_SHA, 'final_dataset_sha256': runner.DATASET_SHA,
                  'dataset_unchanged': True, 'invalidated': False}
        execution = {'status': 'exited', 'exit_code': 0, 'duration_seconds': 2,
                     'scope': 'synthetic unit receipt; no process was started'}
        logs = [{'stage': 'conversation.tools', 'request_id': turn_id, 'model_id': models['primary'],
                 'provider':'deepseek','outcome': 'completed', 'error_code': '', 'usage': usage},
                {'stage': 'reviewer.conversation', 'request_id': turn_id, 'model_id': models['reviewer'],
                 'provider':'ai_ping','outcome': 'completed', 'error_code': '', 'attempts': 1, 'finish_reason': 'stop'}]
        return execution, report, logs, ident, prompt, sources, {}, self.base

    def paid_failure(self, mutation):
        data = list(self.good())
        runtime = data[1]['records'][0]['result']['agent_runtime']
        call = runtime['model_calls'][0]
        if mutation == 'not_sent_one_attempt':
            zero = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
            call.update(request_sent=False, attempts=1, outcome='failed',
                        error_code='synthetic_pre_send_rejection', usage=zero)
            runtime.update(primary_tokens=0, total_tokens=2, budget_tokens=2)
            data[2][0].update(outcome='failed', error_code=call['error_code'], usage=zero)
            data[1]['records'][0]['response_metadata']['primary'] = []
        elif mutation == 'missing_model':
            del call['model_id']
        elif mutation == 'wrong_log_turn':
            data[2][0]['request_id'] = 'different-synthetic-turn'
        elif mutation == 'wrong_log_model':
            data[2][0]['model_id'] = 'different-synthetic-model'
        elif mutation == 'invalid_log_usage':
            data[2][0]['usage'] = {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': True}
        elif mutation == 'mismatched_log_usage':
            data[2][0]['usage'] = {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}
        elif mutation == 'review_attempt_mismatch':
            data[2][1]['attempts'] = 0
        elif mutation == 'partial_failed_usage':
            partial = {'prompt_tokens': 3, 'completion_tokens': 1}
            call.update(outcome='failed', error_code='synthetic_partial_usage_failure',
                        usage=partial, usage_complete=False)
            runtime.update(primary_tokens=None, total_tokens=None, usage_complete=False)
            data[2][0].update(outcome='failed', error_code=call['error_code'], usage=partial)
        elif mutation == 'missing_turn':
            del data[1]['records'][0]['result']['bank_response_id']
        elif mutation == 'missing_review_observation':
            del data[1]['records'][0]['independent_reviews']
        elif mutation in ('mismatched_review_usage', 'boolean_review_usage'):
            data[1]['records'][0]['independent_reviews'][0]['decision']['_model_usage'] = (
                {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
                if mutation == 'mismatched_review_usage'
                else {'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2})
        else:
            raise AssertionError('unknown_paid_receipt_mutation')
        return data

    def test_original_four_case_canonical_order_and_budgets_are_literal_locked(self):
        cases = runner.lock_cases(self.contract, self.dataset)
        self.assertEqual([c['id'] for c in cases], list(runner.ORDER))
        self.assertEqual(runner.public.sha(runner.ROOT / runner.CONTRACT_PATH), runner.CONTRACT_SHA)
        for mutation in ('order', 'budget', 'case_prompt', 'case_expected'):
            contract, dataset = deepcopy(self.contract), deepcopy(self.dataset)
            if mutation == 'order': contract['financial_probe_order'].reverse()
            elif mutation == 'budget': contract['per_task_limits']['wall_seconds'] = 106
            else:
                case = next(c for c in dataset['cases'] if c['id'] == runner.ORDER[0])
                if mutation == 'case_prompt': case['prompt'] += '\n'
                else: case['expected']['facts'][0]['expected'] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runner.lock_cases(contract, dataset)

    def test_default_not_run_and_wrong_handoff_sha_never_launch_original_adapter(self):
        args = SimpleNamespace(out_dir=self.base/'default', cloud=False)
        with patch.object(runner, 'OUTPUT_BASE', self.base), patch.object(runner, 'execute_case') as child:
            self.assertEqual(runner.run(args), 2)
        child.assert_not_called()
        record = json.loads((args.out_dir/'results.json').read_text())
        self.assertTrue(all(r['status'] == 'not_run' for r in record['records']))
        path = self.write('not-a-handoff.json', {'scope': 'unit fake; never authorization'})
        args = SimpleNamespace(cloud=True, handoff=path, handoff_sha256='0'*64)
        inputs = {'sources': {}, 'datasets': {}, 'contract': SimpleNamespace()}
        with self.assertRaisesRegex(ValueError, 'cloud_handoff_digest_missing_or_mismatch'):
            runner.require_cloud(args, inputs)

    def test_same_candidate_public_inspection_and_native_limits_are_reused(self):
        candidate = self.base/'candidate'
        for name in (runner.CONTRACT_PATH, runner.DATASET_PATH, runner.LEGACY_PATH,
                     runner.ADAPTER_PATH, 'agent_runtime/conversation.py', 'agent_runtime/native_client.py',
                     'agent_runtime/model_adapters.py'):
            path = candidate/name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(runner.ROOT/name, path)
        public_inputs = {'sources': {'tests/window_integration/finance_probe_runner.py': runner.public.sha(runner.__file__),
                         **{name:runner.public.sha(candidate/name) for name in (runner.LEGACY_PATH,runner.ADAPTER_PATH)}},
                         'datasets': {}, 'contract': SimpleNamespace(), 'scope': 'unit inspection only'}
        with patch.object(runner.public, 'inspect_inputs', return_value=public_inputs) as inspect:
            inputs = runner.inspect_inputs(candidate, self.base/'source.json', self.base/'data.json')
            self.assertEqual([c['id'] for c in inputs['finance_cases']], list(runner.ORDER))
            inspect.assert_called_once_with(candidate, self.base/'source.json', self.base/'data.json')
            public_inputs['sources']['tests/window_integration/finance_probe_runner.py'] = 'old-source'
            with self.assertRaisesRegex(ValueError, 'running_finance_adapter_not_same_frozen_source'):
                runner.inspect_inputs(candidate, None, None)
        native = candidate/'agent_runtime/native_client.py'
        native.write_text(native.read_text().replace('output_token_limit = 4000', 'output_token_limit = 4001'))
        with self.assertRaisesRegex(ValueError, 'native_primary_output_limit_changed'):
            runner.check_native_budget(candidate)

    def test_child_command_is_original_single_id_cli_without_gold_tool_scripting(self):
        for ident in runner.ORDER:
            command = runner.case_command(self.base, ident, self.base/'child', interpreter='/test/python')
            self.assertEqual(command, ['/test/python', '-B', str(self.base/runner.ADAPTER_PATH), '--dataset',
                str(self.base/runner.DATASET_PATH), '--out-dir', str(self.base/'child'), '--case-ids', ident])
            self.assertNotIn('--offline', command)
            self.assertNotIn('--process-preflight', command)
            self.assertEqual(command.count('--case-ids'), 1)
        with self.assertRaises(ValueError): runner.case_command(self.base, 'S04', self.base/'child')

    def test_real_finance_dispatch_removes_only_legacy_knowledge_override(self):
        contract = runner.public.load_contract(runner.ROOT)
        real_isolated_environment = contract.isolated_environment
        generated, dispatched = [], []
        def isolate(candidate, state):
            environment = real_isolated_environment(candidate, state)
            generated.append(environment)
            return environment
        def dispatch(argv, candidate, process_out, timeout, environment):
            dispatched.append((argv, candidate, process_out, timeout, dict(environment)))
            return {'status': 'exited', 'exit_code': 1, 'duration_seconds': 0,
                    'scope': 'unit dispatch spy; original CLI and model were not started'}
        case = next(row for row in self.dataset['cases'] if row['id'] == runner.ORDER[0])
        out = self.base / 'dispatch'
        inputs = {'contract': contract, 'sources': {}, 'datasets': {}}
        with patch.dict(contract.os.environ, {'NAVREN_KNOWLEDGE_DB': 'unit-inherited-override',
                                             'UNIT_ISOLATION_PRESERVED': 'unit-value'}, clear=True), \
             patch.object(contract, 'baseline_bank_path', return_value=self.base / 'unit-live-observer-only.sqlite3'), \
             patch.object(contract, 'isolated_environment', side_effect=isolate) as isolated, \
             patch.object(contract, 'execute', side_effect=dispatch) as execute:
            record = runner.execute_case(runner.ROOT, case, inputs, out)
        self.assertEqual(isolated.call_count, 1)
        self.assertEqual(execute.call_count, 1)
        argv, candidate, process_out, timeout, environment = dispatched[0]
        original = generated[0]
        self.assertIn('NAVREN_KNOWLEDGE_DB', original)
        self.assertNotIn('NAVREN_KNOWLEDGE_DB', environment)
        expected = {key: value for key, value in original.items() if key != 'NAVREN_KNOWLEDGE_DB'}
        expected.update(NAVREN_AGENT_TIMEOUT='105', NAVREN_ALLOW_CLOUD_ASR='0',
                        NAVREN_MODEL_CALL_LOG=str(out / 'semantic/model-calls.jsonl'))
        self.assertEqual(set(environment), set(expected))
        self.assertTrue(all(environment[key] == value for key, value in expected.items()))
        self.assertEqual(environment['NAVREN_SIM_DB'], environment['QINGZHOU_SIM_DB'])
        state = Path(environment['NAVREN_SIM_DB']).parent
        for name in ('NAVREN_SIM_DB', 'NAVREN_MARKET_DB', 'NAVREN_FUND_CACHE'):
            self.assertTrue(Path(environment[name]).is_relative_to(state))
        self.assertTrue(environment['NAVREN_PERSONAL_DATA_KEY'] == original['NAVREN_PERSONAL_DATA_KEY'])
        self.assertEqual(environment['PYTHONPATH'], original['PYTHONPATH'])
        self.assertEqual(environment['UNIT_ISOLATION_PRESERVED'], 'unit-value')
        self.assertEqual(argv, [str(runner.sys.executable), '-B', str(runner.ROOT / runner.ADAPTER_PATH),
            '--dataset', str(runner.ROOT / runner.DATASET_PATH), '--out-dir', str(out / 'semantic'),
            '--case-ids', runner.ORDER[0]])
        self.assertEqual(candidate, runner.ROOT)
        self.assertEqual(process_out, out / 'process')
        self.assertEqual(timeout, 140)
        self.assertEqual(record['task_budget_seconds'], 105)
        self.assertEqual(record['child_hard_limit_seconds'], 140)
        self.assertIn('semantic_results_missing_or_invalid', record['stop_reasons'])
        self.assertFalse(state.exists())

    def test_collector_zero_does_not_override_real_exit_manifest_or_publisher_failure(self):
        self.assertEqual(runner.validate_child(*self.good())['stop_reasons'], [])
        facts = list(self.good())
        fact_row = facts[1]['records'][0]
        fact_runtime = fact_row['result']['agent_runtime']
        fact_runtime['model_calls'] = fact_runtime['model_calls'][:1]
        fact_runtime.update(reviewer_tokens=0, total_tokens=4, budget_tokens=4)
        fact_row['result'].update(agent_review={'status': 'not_required'}, answer_contract={'kind': 'verified_facts'})
        fact_row['independent_reviews'] = []
        fact_row['response_metadata']['reviewer'] = []
        facts[2] = facts[2][:1]
        self.assertEqual(runner.validate_child(*facts)['stop_reasons'], [])
        for mutation in ('actual_exit', 'timeout_exit_zero', 'selected_id', 'dataset', 'source', 'publisher', 'missing_report'):
            data = list(self.good())
            execution, report = data[:2]
            if mutation == 'actual_exit': execution['exit_code'] = 1
            elif mutation == 'timeout_exit_zero': execution['status'] = 'timed_out'
            elif mutation == 'selected_id': report['selected_case_ids'] = [runner.ORDER[1]]
            elif mutation == 'dataset': report['final_dataset_sha256'] = 'different'
            elif mutation == 'source': report['source_unchanged'] = False
            elif mutation == 'publisher': report['records'][0]['result']['error'] = {'code': 'PUBLICATION_FINALIZATION_FAILED'}
            else: data[1] = None
            with self.subTest(mutation=mutation): self.assertTrue(runner.validate_child(*data)['stop_reasons'])

    def test_actual_usage_unknown_invalid_or_truncated_is_not_free_or_double_counted(self):
        for mutation in ('missing', 'sum', 'boolean', 'sent_zero_attempts', 'completed_not_sent', 'zero_attempt_nonzero_usage', 'retry', 'log_count', 'truncated'):
            data = list(self.good())
            runtime = data[1]['records'][0]['result']['agent_runtime']
            if mutation == 'missing': runtime['model_calls'][0]['usage'] = None
            elif mutation == 'sum': runtime['model_calls'][0]['usage']['total_tokens'] = 9
            elif mutation == 'boolean': runtime['total_tokens'] = True
            elif mutation == 'sent_zero_attempts': runtime['model_calls'][0]['attempts'] = 0
            elif mutation == 'completed_not_sent': runtime['model_calls'][0]['request_sent'] = False
            elif mutation == 'zero_attempt_nonzero_usage': runtime['model_calls'][0].update(attempts=0,request_sent=False,outcome='failed')
            elif mutation == 'retry': runtime['model_calls'][0]['attempts'] = 2
            elif mutation == 'log_count': data[2].pop()
            else:
                runtime['model_calls'][0].update(outcome='failed', error_code='response_truncated')
                data[2][0].update(outcome='failed', error_code='response_truncated')
            result = runner.validate_child(*data)
            with self.subTest(mutation=mutation):
                self.assertTrue(result['stop_reasons'])
                if mutation in ('missing','sum','boolean'): self.assertIsNone(result['actual_total_tokens'])
                if mutation == 'truncated':
                    self.assertEqual(result['actual_total_tokens'], 6)
                    self.assertEqual(result['known_tokens_lower_bound'], 6)
                    self.assertIn('response_length_limit', result['stop_reasons'])
        for mutation in PAID_FAILURE_CASES:
            with self.subTest(paid_receipt=mutation):
                data = self.paid_failure(mutation)
                result = runner.validate_child(*data)
                self.assertTrue(result['stop_reasons'])
                if mutation not in ('missing_review_observation', 'mismatched_review_usage', 'boolean_review_usage'):
                    self.assertTrue(runner.public.paid_usage_audit(
                        data[1]['records'][0]['result']['agent_runtime'], data[2],
                        data[1]['records'][0]['result'].get('bank_response_id'))['errors'])
                if mutation == 'not_sent_one_attempt':
                    self.assertIn('paid_sent_admission_invalid', result['stop_reasons'])
                if mutation in ('invalid_log_usage', 'mismatched_log_usage', 'partial_failed_usage',
                                'mismatched_review_usage', 'boolean_review_usage'):
                    self.assertIsNone(result['actual_total_tokens'])
                if mutation == 'partial_failed_usage':
                    self.assertEqual(result['known_tokens_lower_bound'], 6)
        not_sent = self.paid_failure('not_sent_one_attempt')
        not_sent[1]['records'][0]['result']['agent_runtime']['model_calls'][0]['attempts'] = 0
        admission = runner.validate_child(*not_sent)
        self.assertNotIn('paid_sent_admission_invalid', admission['stop_reasons'])
        self.assertEqual(admission['actual_total_tokens'], 2)
        self.assertEqual(admission['known_tokens_lower_bound'], 2)
        self.assertEqual(admission['actual_model_requests_sent'], 1)
        self.assertIn('critical_model_call_failure', admission['stop_reasons'])

    def test_budget_fallback_length_financial_and_business_side_effects_stop(self):
        for mutation in ('rounds','tools','tokens','wall','limit','fallback','length','cash','profile','write'):
            data = list(self.good())
            row = data[1]['records'][0]
            runtime = row['result']['agent_runtime']
            if mutation == 'rounds': runtime['rounds'] = 9
            elif mutation == 'tools': runtime['tool_calls'] = 13
            elif mutation == 'tokens': runtime['budget_tokens'] = 100001
            elif mutation == 'wall': runtime['duration_ms'] = 105001
            elif mutation == 'limit': runtime['budget_limit'] = 'tokens'
            elif mutation == 'fallback': row['result']['isFallback'] = True
            elif mutation == 'length': data[2][-1]['finish_reason'] = 'length'
            elif mutation == 'cash': row['after']['total_cash_minor'] -= 1
            elif mutation == 'profile': row['after']['profile']['risk_level'] = 3
            else: runtime['trace'].append({'tool':'prepare_operation','status':'completed'})
            with self.subTest(mutation=mutation): self.assertTrue(runner.validate_child(*data)['stop_reasons'])

    def test_sequence_stops_before_later_paid_case_but_semantic_wrong_is_pending(self):
        good = self.good()
        good[1]['records'][0]['result']['reply'] = '故意语义错误的单位测试文本；不作实际金融答复'
        self.assertEqual(runner.validate_child(*good)['stop_reasons'], [])
        sources, datasets = {'unit.py':'source'}, {'unit.json':'data'}
        path = self.write('source.json', sources)
        data_path = self.write('data.json', datasets)
        handoff = self.write('handoff.json', {'scope':'unit mock; not authorization'})
        contract = SimpleNamespace(source_manifest=lambda _: sources, data_manifest=lambda _: datasets,
                                   auxiliary_manifest=lambda _: runner.public.AUXILIARY)
        inputs = {'sources':sources,'datasets':datasets,'contract':contract,'finance_paths':{},'finance_input_sha256':{},
                  'finance_cases':[{'id':ident,'prompt':'unit mock; not original execution'} for ident in runner.ORDER]}
        args = SimpleNamespace(out_dir=self.base/'sequence',cloud=True,candidate=self.base,source_manifest=path,
                               data_manifest=data_path,handoff=handoff,handoff_sha256=runner.public.sha(handoff))
        first = {'id':runner.ORDER[0],'status':'stopped','stop_reasons':['unknown_or_inconsistent_actual_usage'],
                 'actual_total_tokens':None,'actual_model_requests_sent':1,'semantic_judgment':None}
        with patch.object(runner,'OUTPUT_BASE',self.base), patch.object(runner,'inspect_inputs',return_value=inputs), \
             patch.object(runner,'require_cloud',return_value={'ready':True,'scope':'unit mock only'}), \
             patch.object(runner,'execute_case',return_value=first) as execute:
            self.assertEqual(runner.run(args),1)
        execute.assert_called_once()
        results = json.loads((args.out_dir/'results.json').read_text())
        self.assertEqual([r['status'] for r in results['records']],['stopped','not_run','not_run','not_run'])
        self.assertIsNone(results['semantic_judgment'])
        for mutation in PAID_FAILURE_CASES:
            with self.subTest(paid_receipt_sequence=mutation):
                failed = runner.validate_child(*self.paid_failure(mutation))
                failed.update(id=runner.ORDER[0], status='stopped')
                args.out_dir = self.base / ('sequence-' + mutation)
                with patch.object(runner,'OUTPUT_BASE',self.base), patch.object(runner,'inspect_inputs',return_value=inputs), \
                     patch.object(runner,'require_cloud',return_value={'ready':True,'scope':'unit mock only'}), \
                     patch.object(runner,'execute_case',return_value=failed) as execute:
                    self.assertEqual(runner.run(args), 1)
                execute.assert_called_once()
                collected = json.loads((args.out_dir / 'results.json').read_text())
                self.assertEqual([r['status'] for r in collected['records']], ['stopped','not_run','not_run','not_run'])
        args.out_dir = self.base/'publisher-exception'
        with patch.object(runner,'OUTPUT_BASE',self.base), patch.object(runner,'inspect_inputs',return_value=inputs), \
             patch.object(runner,'require_cloud',return_value={'ready':True,'scope':'unit mock only'}), \
             patch.object(runner,'execute_case',side_effect=OSError('unit receipt failure after possibly sent request')) as execute:
            self.assertEqual(runner.run(args),1)
        execute.assert_called_once()
        results = json.loads((args.out_dir/'results.json').read_text())
        self.assertEqual([r['status'] for r in results['records']],['stopped','not_run','not_run','not_run'])
        self.assertIsNone(results['actual_requests_sent'])

    def test_missing_report_or_exact_row_preserves_only_verified_raw_log_prefix(self):
        for mutation in ('missing_report','missing_row','invalid_row','malformed_tail','foreign_tail',
                         'invalid_first_usage','wrong_first_model'):
            data=list(self.good())
            if mutation=='missing_report':data[1]=None
            elif mutation=='missing_row':data[1]['records']=[]
            elif mutation=='invalid_row':data[1]['records']=[None]
            else:
                data[1]=None
                if mutation=='malformed_tail':data[2].append(None)
                elif mutation=='foreign_tail':data[2][1]['request_id']='another-turn'
                elif mutation=='invalid_first_usage':data[2][0]['usage']['total_tokens']=5
                else:data[2][0]['model_id']='unconfigured-model'
            actual=runner.validate_child(*data)
            with self.subTest(mutation=mutation):
                self.assertTrue(actual['stop_reasons'])
                self.assertIsNone(actual['actual_total_tokens'])
                self.assertIsNone(actual['actual_model_requests_sent'])
                self.assertEqual(actual['known_tokens_lower_bound'],
                    None if mutation in ('invalid_first_usage','wrong_first_model') else 4)
                self.assertIsNone(actual['semantic_judgment'])

    def test_response_identity_gap_stops_paid_gate_and_keeps_known_spend(self):
        for mutation in ('missing_metadata','wrong_returned_model','missing_response_id','extra_response',
                         'wrong_provider','invented_alias'):
            data=list(self.good());row=data[1]['records'][0]
            if mutation=='missing_metadata':row.pop('response_metadata')
            elif mutation=='wrong_returned_model':row['response_metadata']['primary'][0]['model']='GLM-5.3-Flash'
            elif mutation=='missing_response_id':row['response_metadata']['reviewer'][0].pop('id')
            elif mutation=='extra_response':row['response_metadata']['primary'].append(deepcopy(row['response_metadata']['primary'][0]))
            elif mutation=='wrong_provider':data[2][0]['provider']='ai_ping'
            else:row['response_metadata']['reviewer'][0]['model']='GLM-5.3-Flash-unconfigured-alias'
            actual=runner.validate_child(*data)
            with self.subTest(mutation=mutation):
                self.assertTrue(actual['stop_reasons'])
                self.assertIsNone(actual['actual_total_tokens'])
                self.assertEqual(actual['known_tokens_lower_bound'],6)


if __name__ == '__main__':
    unittest.main()
