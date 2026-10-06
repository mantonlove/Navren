"""公共探针执行器的免费协议专项；不评价金融语义或云模型能力。"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name('public_probe_runner.py')
SPEC = importlib.util.spec_from_file_location('public_probe_runner_under_test', PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
ROOT = PATH.resolve().parents[2]


def good_record():
    primary={'prompt_tokens':6,'completion_tokens':2,'total_tokens':8}
    reviewer={'prompt_tokens':3,'completion_tokens':1,'total_tokens':4}
    calls=[{'stage':stage,'usage':usage,'usage_complete':True,'billing_mode':'paid',
            'request_sent':True,'attempts':1,'outcome':'completed','error_code':None,'model_id':runner.MODEL_IDENTITIES[stage]['model_id']}
           for stage,usage in (('primary',primary),('reviewer',reviewer))]
    return {'http_status': 200, 'status_http': 200, 'financial_state_unchanged': True,'personal_memory_state_unchanged':True,
            'speech_receipt_validated': True, 'speech_text_matches_reply': True,
            'fixed_release_preserved': True, 'elapsed_seconds': 1,
            'response': {'isFallback': False,'bank_response_id':'unit-turn', 'agent_review': {'status': 'approved'},
                         'knowledge_preparation': {'status': 'not_requested'},
                         'agent_runtime': {'usage_complete': True, 'total_tokens': 12,
                                           'primary_tokens': 8, 'reviewer_tokens': 4,
                                           'rounds':2,'tool_calls':2,'budget_tokens':12,'duration_ms':1000,
                                           'budget_policy':{'limit':100000},'trace':[],
                                           'model_calls':calls}},
            'configured_models':deepcopy(runner.MODEL_IDENTITIES),
            'response_metadata':{'primary':[{'model':runner.MODEL_IDENTITIES['primary']['model_id'],'id':'unit-primary-response'}],
                                 'reviewer':[{'model':runner.MODEL_IDENTITIES['reviewer']['model_id'],'id':'unit-review-response'}]},
            'model_call_logs':[{'stage':'conversation.tools','outcome':'completed','error_code':'','usage':primary,
                                'model_id':runner.MODEL_IDENTITIES['primary']['model_id'],'provider':'ai_ping','request_id':'unit-turn'},
                               {'stage':'reviewer.conversation','outcome':'completed','error_code':'','finish_reason':'stop',
                                'model_id':runner.MODEL_IDENTITIES['reviewer']['model_id'],'provider':'ai_ping','request_id':'unit-turn','attempts':1}],
            'reviewer_records':[{'decision':{'verdict':'approve','_model_usage':reviewer}}]}


class PublicProbeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='public-probe-unit-')
        self.base = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def write(self, relative, value):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def gate_fixture(self):
        counts = {'complete': True, 'tests_run': 1, 'skipped': 0, 'expected_failures': 0,
                  'unexpected_successes': 0, 'failures': 0, 'errors': 0}
        source_root = self.base / 'source'
        source_root.mkdir()
        driver_source = source_root / 'scripts/run_window_integration.py'
        driver_source.parent.mkdir()
        driver_source.write_text('# temporary unit fixture; no execution\n')
        source, data = {'source.py': 'source-sha', 'scripts/run_window_integration.py': runner.sha(driver_source)}, {'fixture.json': 'data-sha'}
        original_contract = runner.load_contract(ROOT)
        groups={'oracle':10,'process':10,'ui-modules':22,'financial-regression':15,
                'security-regression':26,'independent-oracle':10}
        groups.update({name.removeprefix('test_').removesuffix('.py'):minimum
                       for name,minimum in original_contract.LOCAL_TEST_GROUPS})
        support={str(i):'fixture-support-sha'for i in range(8)}
        g1 = {'status': 'engineering_stage_passed', 'stage':'local', 'source_sha256': source, 'data_sha256': data,
              'candidate_source_matches_frozen': True, 'candidate_data_matches_frozen': True,
              'live_business_digest_unchanged': True,
              'candidate_auxiliary_matches_frozen': True,
              'auxiliary_sha256': runner.AUXILIARY,
              'formal_support_sha256': support, 'candidate_formal_support_matches_frozen': True,
              'candidate_root': str(source_root),
              'executions': [{'name':name,'status': 'exited', 'exit_code': 0,
                              'test_counts': {**counts,'tests_run':minimum}}for name,minimum in groups.items()],
              'scope': 'temporary unit fixture; never an execution handoff'}
        path = self.write('g1/run.json', g1)
        execution_path = self.write('g1-driver/execution.json', {
            'status': 'exited', 'exit_code': 0,
            'argv': ['python', str(driver_source), '--source-root', str(source_root), '--out-dir', str(path.parent),'--stage','local'],
            'scope': 'temporary unit fixture; no process was started'})
        handoff = {'agent_handed_off': True, 'existing_semantic_run_finished': True,
                   'cloud_retest_authorized': True, 'isolated_fixture_preflight_passed': True,
                   'source_sha256': source, 'data_sha256': data,
                   'g1_run_path': str(path), 'g1_run_sha256': runner.sha(path),
                   'g1_execution_path': str(execution_path), 'g1_execution_sha256': runner.sha(execution_path),
                   'scope': 'temporary unit fixture; no model may be called'}
        return source, data, g1, handoff

    def gate(self, source, data, handoff):
        path = self.write('handoff.json', handoff)
        original_contract = runner.load_contract(ROOT)
        contract = SimpleNamespace(handoff_gate=original_contract.handoff_gate,
            source_manifest=lambda _root: source, data_manifest=lambda _root: data,
            auxiliary_manifest=lambda _root: runner.AUXILIARY,
            formal_support_manifest=lambda _root,required=False: {str(i):'fixture-support-sha'for i in range(8)},
            LOCAL_TEST_GROUPS=original_contract.LOCAL_TEST_GROUPS)
        with patch.object(runner, 'OUTPUT_ROOT', self.base):
            return runner.cloud_gate(path, runner.sha(path), source, data, contract)

    def test_handoff_uses_real_four_flags_and_complete_sha_maps(self):
        source, data, _g1, handoff = self.gate_fixture()
        self.assertTrue(self.gate(source, data, handoff)['ready'])
        for value in ({**handoff, 'agent_handed_off': False},
                      {**handoff, 'source_sha256': {'extra.py': 'different'}},
                      {**handoff, 'data_sha256': {}}):
            with self.assertRaisesRegex(ValueError, 'cloud_handoff_not_ready'):
                self.gate(source, data, value)

    def test_cloud_needs_exact_handoff_digest(self):
        source, data, _g1, handoff = self.gate_fixture()
        path = self.write('handoff.json', handoff)
        with self.assertRaisesRegex(ValueError, 'cloud_handoff_digest_missing_or_mismatch'):
            runner.cloud_gate(path, 'wrong', source, data, runner.load_contract(ROOT))

    def test_template_flags_do_not_replace_g1(self):
        source, data, _g1, handoff = self.gate_fixture()
        handoff.pop('g1_run_path')
        with self.assertRaisesRegex(ValueError, 'g1_receipt_missing_or_mismatch'):
            self.gate(source, data, handoff)

    def test_cloud_requires_g1_live_business_digest_explicitly_unchanged(self):
        source, data, original, handoff = self.gate_fixture()
        self.assertTrue(self.gate(source, data, handoff)['ready'])
        for mutation in ('false', 'null', 'missing'):
            g1 = deepcopy(original)
            if mutation == 'missing':
                del g1['live_business_digest_unchanged']
            else:
                g1['live_business_digest_unchanged'] = False if mutation == 'false' else None
            path = self.write('g1/run.json', g1)
            handoff['g1_run_sha256'] = runner.sha(path)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    ValueError, 'g1_live_business_digest_not_proven_unchanged'):
                self.gate(source, data, handoff)

    def test_g1_planned_success_cannot_replace_actual_driver_receipt(self):
        source, data, _g1, handoff = self.gate_fixture()
        handoff.pop('g1_execution_path')
        with self.assertRaisesRegex(ValueError, 'g1_driver_receipt_missing_or_mismatch'):
            self.gate(source, data, handoff)

    def test_g1_driver_requires_exact_out_and_source_script(self):
        source, data, _g1, handoff = self.gate_fixture()
        path = Path(handoff['g1_execution_path'])
        original = json.loads(path.read_text())
        for mutation in ('timed_out', 'different_out', 'script_changed'):
            driver = deepcopy(original)
            if mutation == 'timed_out':
                driver['status'] = 'timed_out'
            elif mutation == 'different_out':
                driver['argv'][-1] = str(self.base / 'wrong-out')
            else:
                driver['argv'][1] = str(self.base / 'nonexistent-driver.py')
            path.write_text(json.dumps(driver))
            handoff['g1_execution_sha256'] = runner.sha(path)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.gate(source, data, handoff)

    def test_g1_auxiliary_requires_all_three_exact_inputs(self):
        source, data, original, handoff = self.gate_fixture()
        for mutation in ('subset', 'extra', 'wrong_sha'):
            g1 = deepcopy(original)
            if mutation == 'subset':
                del g1['auxiliary_sha256']['outputs/window-progress/finance/implementation/adapter-contract.md']
            elif mutation == 'extra':
                g1['auxiliary_sha256']['unapproved.md'] = 'unknown'
            else:
                g1['auxiliary_sha256']['outputs/window-progress/finance/solution-review/solution-v2.md'] = 'wrong'
            path = self.write('g1/run.json', g1)
            handoff['g1_run_sha256'] = runner.sha(path)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'g1_original_question_not_frozen'):
                self.gate(source, data, handoff)

    def test_g1_rejects_timed_out_zero_exit_skips_and_unknown_counts(self):
        source, data, original, handoff = self.gate_fixture()
        for mutation in ('timed_out', 'skip', 'missing_counts', 'source_drift', 'incomplete',
                         'missing_formal_support', 'changed_formal_support', 'support_flag_unknown'):
            g1 = deepcopy(original)
            if mutation == 'timed_out':
                g1['executions'][0]['status'] = 'timed_out'
            elif mutation == 'skip':
                g1['executions'][0]['test_counts']['skipped'] = 1
            elif mutation == 'missing_counts':
                del g1['executions'][0]['test_counts']
            elif mutation == 'source_drift':
                g1['source_sha256'] = {}
            elif mutation == 'incomplete':
                g1['status'] = 'running'
            elif mutation == 'missing_formal_support':
                del g1['formal_support_sha256']
            elif mutation == 'changed_formal_support':
                g1['formal_support_sha256'] = {'unexpected': 'unknown'}
            else:
                g1['candidate_formal_support_matches_frozen'] = 'true'
            path = self.write('g1/run.json', g1)
            handoff['g1_run_sha256'] = runner.sha(path)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.gate(source, data, handoff)

    def test_model_body_contains_only_verbatim_question(self):
        case = {'question': ' 原题100万元？\n', 'expected_answer': '评分秘密',
                'request': {'query': '验收器预写正确检索', 'product_id': 'grading-only'}}
        self.assertEqual(runner.agent_body(case), {'text': ' 原题100万元？\n'})
        self.assertNotIn('评分秘密', json.dumps(runner.agent_body(case), ensure_ascii=False))

    def test_usage_unknown_stays_null_and_lower_bound_separate(self):
        result = runner.usage_record({'total_tokens': None, 'budget_tokens': 100000,
                                      'known_tokens_lower_bound': 713, 'usage_complete': False}, True)
        self.assertIsNone(result['actual_total_tokens'])
        self.assertEqual(result['known_tokens_lower_bound'], 713)
        self.assertEqual(result['budget_tokens'], 100000)
        self.assertEqual(result['status'], 'unknown_usage')
        offline = runner.usage_record({'total_tokens': 1, 'usage_complete': True}, False)
        self.assertIsNone(offline['actual_total_tokens'])
        self.assertIsNone(offline['usage_complete'])

    def test_usage_flags_cannot_replace_actual_counters_logs_or_paid_admission(self):
        for mutation in ('missing_counter','bool_counter','counter_sum','runtime_total','wrong_stage',
                         'not_sent','wrong_attempts','missing_logs','wrong_log_stage','native_log_usage',
                         'log_length','missing_budget_policy','malformed_call','wrong_turn','wrong_model','no_model',
                         'not_sent_with_attempt','memory_changed','missing_review_observation','changed_review_usage'):
            record=good_record();runtime=record['response']['agent_runtime'];call=runtime['model_calls'][0]
            if mutation=='missing_counter':del call['usage']['completion_tokens']
            elif mutation=='bool_counter':call['usage']['prompt_tokens']=True
            elif mutation=='counter_sum':call['usage']['total_tokens']=9
            elif mutation=='runtime_total':runtime['total_tokens']=13
            elif mutation=='wrong_stage':call['stage']='untrusted'
            elif mutation=='not_sent':call['request_sent']=False
            elif mutation=='wrong_attempts':call['attempts']=0
            elif mutation=='missing_logs':record.pop('model_call_logs')
            elif mutation=='wrong_log_stage':record['model_call_logs'][0]['stage']='reviewer.conversation'
            elif mutation=='native_log_usage':record['model_call_logs'][0]['usage']={'prompt_tokens':7,'completion_tokens':2,'total_tokens':9}
            elif mutation=='log_length':record['model_call_logs'][1]['finish_reason']='length'
            elif mutation=='missing_budget_policy':runtime.pop('budget_policy')
            elif mutation=='malformed_call':runtime['model_calls'][0]=None
            elif mutation=='wrong_turn':record['model_call_logs'][0]['request_id']='another-turn'
            elif mutation=='wrong_model':record['model_call_logs'][0]['model_id']='another-model'
            elif mutation=='no_model':call.pop('model_id')
            elif mutation=='not_sent_with_attempt':call.update(request_sent=False,attempts=1)
            elif mutation=='memory_changed':record['personal_memory_state_unchanged']=False
            elif mutation=='missing_review_observation':record.pop('reviewer_records')
            else:record['reviewer_records'][0]['decision']['_model_usage']={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}
            with self.subTest(mutation=mutation):
                self.assertTrue(runner.stop_reasons(record,True))

    def test_known_failed_usage_is_counted_once_and_partial_remains_unknown(self):
        record=good_record();runtime=record['response']['agent_runtime']
        runtime['model_calls'][0].update(outcome='failed',error_code='response_truncated')
        record['model_call_logs'][0].update(outcome='failed',error_code='response_truncated')
        audit=runner.paid_usage_audit(runtime,record['model_call_logs'],'unit-turn')
        self.assertEqual(audit['actual_total_tokens'],12)
        self.assertEqual(audit['known_tokens_lower_bound'],12)
        self.assertEqual(audit['actual_model_requests_sent'],2)
        self.assertIn('response_length_limit',audit['errors'])
        runtime['model_calls'][1]['usage']={'prompt_tokens':3};runtime['model_calls'][1]['usage_complete']=False
        runtime.update(usage_complete=False,reviewer_tokens=None,total_tokens=None,budget_tokens=100000)
        audit=runner.paid_usage_audit(runtime,record['model_call_logs'],'unit-turn')
        self.assertIsNone(audit['actual_total_tokens'])
        self.assertEqual(audit['known_tokens_lower_bound'],11)
        self.assertEqual(audit['actual_model_requests_sent'],2)
        self.assertIn('unknown_usage',audit['errors'])

    def test_cloud_gate_requires_complete_unique_actual_local_groups(self):
        source,data,original,handoff=self.gate_fixture()
        for mutation in ('single_phase','missing_group','duplicate','short_count','bool_count','empty_support'):
            g1=deepcopy(original)
            if mutation=='single_phase':g1['stage']='oracle'
            elif mutation=='missing_group':g1['executions'].pop()
            elif mutation=='duplicate':g1['executions'].append(deepcopy(g1['executions'][0]))
            elif mutation=='short_count':g1['executions'][0]['test_counts']['tests_run']=1
            elif mutation=='bool_count':g1['executions'][0]['test_counts']['tests_run']=True
            else:g1['formal_support_sha256']={}
            path=self.write('g1/run.json',g1);handoff['g1_run_sha256']=runner.sha(path)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.gate(source,data,handoff)

    def test_stop_on_fallback_budget_length_unknown_and_side_effect(self):
        self.assertEqual(runner.stop_reasons(good_record(), True), [])
        for mutation, expected in [('fallback', 'fallback_or_missing_flag'), ('budget', 'budget_limit'),
                                   ('length', 'response_length_limit'), ('unknown', 'unknown_usage'),
                                   ('finance', 'financial_side_effect'), ('prepare', 'real_prepare_round_receipt_missing')]:
            record = good_record()
            if mutation == 'fallback':
                record['response']['isFallback'] = True
            elif mutation == 'budget':
                record['response']['agent_runtime']['budget_limit'] = 'tokens'
            elif mutation == 'length':
                record['response']['agent_runtime']['model_calls'][0]['error_code'] = 'response_truncated'
            elif mutation == 'unknown':
                record['response']['agent_runtime']['usage_complete'] = False
            elif mutation == 'finance':
                record['financial_state_unchanged'] = False
            else:
                del record['response']['knowledge_preparation']
            with self.subTest(mutation=mutation):
                self.assertIn(expected, runner.stop_reasons(record, True))

    def test_round_tool_token_limits_cannot_be_relaxed_by_result(self):
        for key, value in (('rounds', 9), ('tool_calls', 13), ('budget_tokens', 100001)):
            record = good_record()
            record['response']['agent_runtime'][key] = value
            with self.subTest(key=key):
                self.assertIn('budget_contract_exceeded', runner.stop_reasons(record, True))

    def test_recovery_requires_real_rejection_and_later_public_read(self):
        runtime = {'trace': [{'tool': 'knowledge_search', 'status': 'rejected', 'code': 'BOUNDARY_PUBLIC_QUERY'},
                             {'tool': 'knowledge_search', 'status': 'completed'}]}
        self.assertEqual(runner.recovery_record(runtime, False, [{}])['status'], 'not_observed')
        self.assertEqual(runner.recovery_record(runtime, True, [{}])['status'], 'observed_boundary_retry')
        self.assertEqual(runner.recovery_record({}, True, [])['status'], 'not_observed')
        record = good_record()
        record['response']['agent_runtime']['trace'] = [runtime['trace'][0]] * 3
        self.assertIn('public_recovery_limit_exceeded', runner.stop_reasons(record, True))

    def test_fail_fast_does_not_start_p02(self):
        source_path = self.write('source.json', {'source.py': 'sha'})
        data_path = self.write('data.json', {'fixture.json': 'sha'})
        questions = self.write('questions.json', {})
        public = self.write('public.json', {})
        database = self.base / 'public.sqlite3'
        database.write_bytes(b'unit fixture')
        sources, data = {'source.py': 'sha'}, {'fixture.json': 'sha'}
        contract = SimpleNamespace(source_manifest=lambda _root: sources, data_manifest=lambda _root: data,
                                   auxiliary_manifest=lambda _root: runner.AUXILIARY)
        candidate = self.base / 'candidate'
        for relative, digest in runner.AUXILIARY.items():
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'unit auxiliary fixture')
        inputs = {'sources': sources, 'datasets': data, 'contract': contract, 'questions': questions,
                  'public_manifest': public, 'database': database,
                  'cases': [{'id': 'P10', 'question': '原题1'}, {'id': 'P02', 'question': '原题2'}]}
        first = {**good_record(), 'id': 'P10', 'stop_reasons': ['unknown_usage'], 'semantic_judgment': None,
                 'isolated_paths': {'NAVREN_SIM_DB': str(self.base/'closed-unit-fixture.sqlite3')}}
        args = SimpleNamespace(out_dir=self.base / 'run', candidate=self.base / 'candidate',
                               source_manifest=source_path, data_manifest=data_path, cloud=False)
        with patch.object(runner, 'OUTPUT_ROOT', self.base), patch.object(runner, 'inspect_inputs', return_value=inputs), \
                patch.object(runner, 'sha', side_effect=lambda path: runner.AUXILIARY[str(Path(path).relative_to(candidate))]
                             if Path(path).is_relative_to(candidate) else __import__('hashlib').sha256(Path(path).read_bytes()).hexdigest()), \
                patch.object(runner, 'execute_case', return_value=first) as execution:
            self.assertEqual(runner.run(args), 1)
        self.assertEqual(execution.call_count, 1)
        result = json.loads((args.out_dir / 'results.json').read_text())
        self.assertEqual(result['stop_reason'], ['unknown_usage'])
        self.assertIsNone(result['actual_child_exit_code'])
        self.assertIsNone(result['semantic_judgment'])

    def test_registered_network_requires_exact_paid_urls(self):
        direct = 'https://api.deepseek.com/chat/completions'
        aiping = 'https://aiping.cn/api/v1/chat/completions'
        self.assertFalse(runner.permitted_url(direct, False))
        self.assertFalse(runner.permitted_url(direct, True))
        self.assertFalse(runner.permitted_url(aiping, False))
        self.assertTrue(runner.permitted_url(aiping, True))
        self.assertFalse(runner.permitted_url(aiping + '?question=private', True))
        self.assertTrue(runner.permitted_url('http://127.0.0.1:123/api/agent', False))
        self.assertFalse(runner.permitted_url('https://www.webank.com/real-cold-reader', True))
        self.assertFalse(runner.permitted_url('http://name:password@127.0.0.1/api/agent', False))

    def test_path_escape_and_output_overwrite_refused(self):
        with self.assertRaisesRegex(ValueError, 'input_path_escape'):
            runner.checked_file(self.base, '../private.sqlite3')
        (self.base / 'existing').mkdir()
        with patch.object(runner, 'OUTPUT_ROOT', self.base):
            with self.assertRaises(FileExistsError):
                runner.run(SimpleNamespace(out_dir=self.base / 'existing', cloud=False))

    def test_original_public_inputs_are_candidate_local(self):
        for filename in ('integration-data-manifest.json', 'manual-question-set.json', 'reviewed-public.sqlite3'):
            target = self.base / runner.PUBLIC_ROOT / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / runner.PUBLIC_ROOT / filename, target)
        for relative in runner.AUXILIARY:
            target = self.base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        sources = {'source.py': 'sha', 'tests/window_integration/public_probe_runner.py': runner.sha(PATH)}
        source_path = self.write('source.json', sources)
        data_path = self.write('data.json', {'fixture.json': 'sha'})
        contract = SimpleNamespace(source_manifest=lambda _root: sources,
                                   data_manifest=lambda _root: {'fixture.json': 'sha'},
                                   auxiliary_manifest=lambda _root: runner.AUXILIARY)
        with patch.object(runner, 'load_contract', return_value=contract):
            inputs = runner.inspect_inputs(self.base, source_path, data_path)
        self.assertEqual(inputs['database'], self.base / runner.PUBLIC_ROOT / 'reviewed-public.sqlite3')
        self.assertEqual([case['id'] for case in inputs['cases']], ['P10', 'P02'])
        sources['tests/window_integration/public_probe_runner.py'] = 'old-adapter-version'
        source_path.write_text(json.dumps(sources))
        with patch.object(runner, 'load_contract', return_value=contract), self.assertRaisesRegex(ValueError, 'running_adapter_not_same_frozen_source'):
            runner.inspect_inputs(self.base, source_path, data_path)
        sources['tests/window_integration/public_probe_runner.py'] = runner.sha(PATH)
        source_path.write_text(json.dumps(sources))
        inputs['database'].write_bytes(b'changed')
        with patch.object(runner, 'load_contract', return_value=contract), self.assertRaisesRegex(ValueError, 'public_database_changed'):
            runner.inspect_inputs(self.base, source_path, data_path)

    def test_business_import_guard_is_before_environment(self):
        with patch.dict(sys.modules, {'agent_runtime.preloaded_test_fixture': SimpleNamespace()}):
            with self.assertRaisesRegex(ValueError, 'business_module_imported_before_isolation'):
                runner.assert_fresh_business_imports()

    def test_raw_log_prefix_keeps_only_legal_once_counted_usage_after_missing_terminal(self):
        logs=good_record()['model_call_logs']
        audit=runner.audit_model_log_prefix(logs)
        self.assertEqual(audit['known_tokens_lower_bound'],8)
        self.assertIsNone(audit['actual_total_tokens'])
        self.assertIsNone(audit['actual_model_requests_sent'])
        self.assertEqual(audit['errors'],[])
        for mutation in ('bad_tail','foreign_turn','foreign_model','foreign_provider','bad_sum','boolean',
                         'negative','duplicate','reviewer_first','native_usage_missing'):
            rows=deepcopy(logs)
            if mutation=='bad_tail':rows.append(None)
            elif mutation=='foreign_turn':rows[1]['request_id']='another-turn'
            elif mutation=='foreign_model':rows[1]['model_id']='invented-alias'
            elif mutation=='foreign_provider':rows[1]['provider']='another-provider'
            elif mutation=='bad_sum':rows[1]['usage']={'prompt_tokens':1,'completion_tokens':1,'total_tokens':3}
            elif mutation=='boolean':rows[1]['usage']={'prompt_tokens':True,'completion_tokens':1,'total_tokens':2}
            elif mutation=='negative':rows[1]['usage']={'prompt_tokens':-1,'completion_tokens':1,'total_tokens':0}
            elif mutation=='duplicate':rows.insert(1,deepcopy(rows[0]))
            elif mutation=='reviewer_first':rows.reverse()
            else:rows[0].pop('usage')
            actual=runner.audit_model_log_prefix(rows)
            with self.subTest(mutation=mutation):
                self.assertTrue(actual['errors'])
                self.assertEqual(actual['known_tokens_lower_bound'],None if mutation in ('reviewer_first','native_usage_missing') else 8)
        path=self.base/'actual-log.jsonl'
        path.write_text(json.dumps(logs[0])+'\n{malformed-final-line\n')
        actual=runner.audit_model_log_prefix(runner.read_model_log_prefix(path))
        self.assertEqual(actual['known_tokens_lower_bound'],8)
        self.assertTrue(actual['errors'])
        for usage in ({'prompt_tokens':6,'completion_tokens':2,'total_tokens':9},
                      {'prompt_tokens':6,'completion_tokens':2,'total_tokens':7},
                      {'prompt_tokens':True,'completion_tokens':2,'total_tokens':3},
                      {'prompt_tokens':-1,'completion_tokens':2,'total_tokens':1},
                      {'prompt_tokens':6,'total_tokens':5}):
            with self.subTest(inconsistent_usage=usage):
                self.assertIsNone(runner.usage_lower_bound(usage))
                record=good_record();record['response']['agent_runtime']['model_calls'][0]['usage']=usage
                audited=runner.paid_usage_audit(record['response']['agent_runtime'],record['model_call_logs'],'unit-turn')
                self.assertIsNone(audited['actual_total_tokens'])
                self.assertEqual(audited['known_tokens_lower_bound'],4)
        self.assertEqual(runner.usage_lower_bound({'prompt_tokens':6,'completion_tokens':2}),8)
        self.assertEqual(runner.usage_lower_bound({'prompt_tokens':6}),6)
        path.write_bytes((json.dumps(logs[0])+'\n').encode()+b'\xff\n')
        self.assertEqual(runner.audit_model_log_prefix(runner.read_model_log_prefix(path))['known_tokens_lower_bound'],8)

    def test_actual_response_metadata_binds_configured_model_and_stage_ordinal(self):
        record=good_record();runtime=record['response']['agent_runtime']
        self.assertEqual(runner.configured_model_contract(ROOT),runner.MODEL_IDENTITIES)
        self.assertEqual(runner.audit_response_identity(runtime,record['model_call_logs'],record['response_metadata'],
            turn_id='unit-turn')['errors'],[])
        for mutation in ('missing_metadata','missing_id','wrong_model','invented_alias','extra_primary',
                         'missing_review','wrong_provider','wrong_turn'):
            changed=deepcopy(record)
            if mutation=='missing_metadata':changed.pop('response_metadata')
            elif mutation=='missing_id':changed['response_metadata']['primary'][0].pop('id')
            elif mutation=='wrong_model':changed['response_metadata']['primary'][0]['model']='GLM-5.3-Flash'
            elif mutation=='invented_alias':changed['response_metadata']['reviewer'][0]['model']='GLM-5.3-Flash-latest'
            elif mutation=='extra_primary':changed['response_metadata']['primary'].append(deepcopy(changed['response_metadata']['primary'][0]))
            elif mutation=='missing_review':changed['response_metadata']['reviewer']=[]
            elif mutation=='wrong_provider':changed['model_call_logs'][0]['provider']='deepseek'
            else:changed['model_call_logs'][0]['request_id']='another-turn'
            with self.subTest(mutation=mutation):
                self.assertTrue(runner.stop_reasons(changed,True))
                self.assertTrue(runner.audit_response_identity(runtime,changed['model_call_logs'],changed.get('response_metadata'),
                    turn_id='unit-turn')['errors'])

    def test_dynamic_formal_remaining_budget_is_not_reset_to_full_task_limit(self):
        record=good_record();runtime=record['response']['agent_runtime']
        runtime['budget_policy']['limit']=12
        self.assertEqual(runner.paid_usage_audit(runtime,record['model_call_logs'],'unit-turn',budget_limit=12)['errors'],[])
        for remaining in (11,True,100001,-1):
            with self.subTest(remaining=remaining):
                self.assertIn('actual_paid_budget_counter_mismatch',runner.paid_usage_audit(runtime,
                    record['model_call_logs'],'unit-turn',budget_limit=remaining)['errors'])

    def test_whole_original_task_clock_includes_cleanup_and_stops_later_execution(self):
        sources, data = {'unit-source.py':'unit-sha'}, {'unit-data.json':'unit-data-sha'}
        source_path=self.write('cleanup-source.json',sources);data_path=self.write('cleanup-data.json',data)
        questions=self.write('cleanup-questions.json',{});manifest=self.write('cleanup-public.json',{})
        database=self.base/'cleanup-public.sqlite3';database.write_bytes(b'unit fixture; no business database')
        candidate=self.base/'cleanup-candidate'
        for relative in runner.AUXILIARY:
            target=candidate/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'unit auxiliary fixture')
        contract=SimpleNamespace(source_manifest=lambda _:sources,data_manifest=lambda _:data,
                                 auxiliary_manifest=lambda _:runner.AUXILIARY)
        inputs={'sources':sources,'datasets':data,'contract':contract,'questions':questions,
                'public_manifest':manifest,'database':database,
                'cases':[{'id':'P10','question':'unit first probe'}, {'id':'P02','question':'unit later probe must not execute'}]}
        for whole_duration in (100.0,106.0):
            clock={'value':0.0};args=SimpleNamespace(out_dir=self.base/('cleanup-'+str(int(whole_duration))),
                candidate=candidate,source_manifest=source_path,data_manifest=data_path,cloud=False)
            def actual_protocol_case(_candidate,case,_inputs,out,_cloud,seconds):
                self.assertEqual(case['id'],'P10')
                self.assertEqual(seconds,105)
                # 已有 unknown 停止保留；此新专项只隔离检查 cleanup 是否跨原105边界。
                result={**good_record(),'id':'P10','elapsed_seconds':1,'stop_reasons':['unknown_usage'],
                        'isolated_paths':{'NAVREN_SIM_DB':str(self.base/'closed-cleanup-unit.sqlite3')}}
                (out/'P10').mkdir()
                clock['value']+=whole_duration
                return result
            with self.subTest(whole_duration=whole_duration),patch.object(runner,'OUTPUT_ROOT',self.base), \
                    patch.object(runner,'inspect_inputs',return_value=inputs), \
                    patch.object(runner.time,'monotonic',side_effect=lambda:clock['value']), \
                    patch.object(runner,'sha',side_effect=lambda path:runner.AUXILIARY[str(Path(path).relative_to(candidate))]
                                 if Path(path).is_relative_to(candidate) else __import__('hashlib').sha256(Path(path).read_bytes()).hexdigest()), \
                    patch.object(runner,'execute_case',side_effect=actual_protocol_case) as execute:
                self.assertEqual(runner.run(args),1)
                execute.assert_called_once()
            result=json.loads((args.out_dir/'results.json').read_text());record=result['records'][0]
            self.assertEqual(record['elapsed_seconds'],whole_duration)
            self.assertIs(record['complete_prepare_http_cleanup'],True)
            self.assertIs(record['cleanup_completed'],True)
            self.assertEqual('task_deadline_exceeded' in record['stop_reasons'],whole_duration>105)
            self.assertEqual(record,json.loads((args.out_dir/'P10/record.json').read_text()))
            self.assertEqual(len(result['records']),1)

    def test_strict_authenticated_log_partial_usage_keeps_this_call_lower_bound_then_stops_prefix(self):
        primary=good_record()['model_call_logs'][0]
        partial=deepcopy(primary);partial.update(usage={'prompt_tokens':3},outcome='failed',error_code='unit_partial_failure')
        observed=runner.audit_model_log_prefix([partial,deepcopy(primary)])
        self.assertEqual(observed['known_tokens_lower_bound'],3)
        self.assertIn('actual_model_log_prefix_usage_incomplete',observed['errors'])
        self.assertIsNone(observed['actual_total_tokens'])
        self.assertIsNone(observed['actual_model_requests_sent'])
        partial['timestamp']='synthetic-distinct-second-log'
        observed=runner.audit_model_log_prefix([primary,partial,deepcopy(primary)])
        self.assertEqual(observed['known_tokens_lower_bound'],11)
        self.assertIn('actual_model_log_prefix_usage_incomplete',observed['errors'])
        for usage in ({'prompt_tokens':3,'completion_tokens':1,'total_tokens':5},
                      {'prompt_tokens':3,'completion_tokens':1,'total_tokens':3},
                      {'prompt_tokens':True}, {'prompt_tokens':-1},
                      {'prompt_tokens':3,'completion_tokens':False}, {'prompt_tokens':3,'total_tokens':2}):
            changed=deepcopy(partial);changed['usage']=usage
            actual=runner.audit_model_log_prefix([primary,changed])
            with self.subTest(usage=usage):
                self.assertEqual(actual['known_tokens_lower_bound'],8)
                self.assertIn('actual_model_log_prefix_usage_invalid',actual['errors'])
        for identity in ({'provider':'deepseek'}, {'model_id':'unconfigured-model'}, {'request_id':'another-turn'}):
            changed={**partial,**identity}
            actual=runner.audit_model_log_prefix([primary,changed])
            with self.subTest(identity=identity):
                self.assertEqual(actual['known_tokens_lower_bound'],8)
                self.assertIn('actual_model_log_prefix_identity_or_order_invalid',actual['errors'])


if __name__ == '__main__':
    unittest.main()
