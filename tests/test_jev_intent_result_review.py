"""零云旁路验收：完整原金标、原S10 oracle、篡改拒绝和全启动分母。"""
from copy import deepcopy
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


s = module('scripts/review_jev_intent_results.py', 'sidecar_test_module')
h = module('scripts/run_jev_intent_ab.py', 'sidecar_harness_test_module')


class ResultReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.freeze_temp = tempfile.TemporaryDirectory(prefix='navren-sidecar-frozen-')
        cls.frozen = Path(cls.freeze_temp.name) / 'experiment'
        s10 = next(case['id'] for case in h.catalog(ROOT) if case['id'].startswith('S10'))
        with s.offline_guard():
            result = h.freeze(cls.frozen, source_root=ROOT, case_ids=[s10, 'X01'], repetitions=2,
                              arms=['off', 'native', 'jev'], off_order='balanced')
        cls.protocol_sha = result['protocol_sha256']
        cls.protocol = h.read(cls.frozen / 'protocol.json')
        cls.cases = h.read(cls.frozen / 'cases.json')['cases']
        cls.oracle = h.oracle_module(cls.frozen)

    @classmethod
    def tearDownClass(cls):
        cls.freeze_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-sidecar-unit-')
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / 'experiment'
        self.out.mkdir()
        # 来源树共用只读原冻结；输出和轨迹每项独立。
        for name in ('protocol.json', 'cases.json', 'split.json', 'freeze-manifest.json', 'pricing.json'):
            if (self.frozen / name).exists():
                shutil.copy2(self.frozen / name, self.out / name)
        shutil.copytree(self.frozen / 'source', self.out / 'source')
        self.review_dir = Path(self.temp.name) / 'blind'
        self.result_path = Path(self.temp.name) / 'judged.json'
        self.summary_path = Path(self.temp.name) / 'summary.json'

    def real_s10_control(self, case):
        """真实本地合成夹具工具；固定答复仅单元控制，非模型表现。"""
        original_connect = sqlite3.connect
        with tempfile.TemporaryDirectory(prefix='navren-sidecar-s10-') as directory, s.offline_guard(), ExitStack() as connections:
            def tracked(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connections.callback(connection.close)
                return connection
            connections.enter_context(patch.object(sqlite3, 'connect', side_effect=tracked))
            gateway, workspace, _, original = h.fixture(self.oracle, case, directory)
            pre = self.oracle.preflight(gateway, workspace, original)
            before = h.financial_snapshot(self.oracle, gateway, workspace)
            runner = self.oracle._runner()
            tools = runner.BankTools(gateway, workspace, 'offline-sidecar-control')
            account = tools.invoke('account_summary', {})
            main = next(a['id'] for a in account['data']['accounts'] if a['id'].endswith('-main'))
            base = tools.invoke('budget', {'account_id': main})
            commitment = next(c['evidence_id'] for c in base['data']['commitments'] if c['kind'] == 'credit_card')
            variables = {'main_account_id': main, 'credit_commitment_id': commitment, 'workspace_id': workspace}
            calls, outputs, invocations = [], [], []
            for index, fact in enumerate(original['expected']['facts']):
                if 'tool' not in fact:
                    continue
                args = self.oracle._replace_variables(fact.get('arguments', {}), variables)
                call_id = 's10-local-control-' + str(index)
                output = tools.invoke(fact['tool'], args)
                calls.append({'id': call_id, 'name': fact['tool'], 'arguments': args})
                outputs.append({'call_id': call_id, 'output': output})
                invocations.append({'call_id': call_id, 'name': fact['tool'], 'arguments': args,
                                    'success': True, 'raw_output': output})
            after = h.financial_snapshot(self.oracle, gateway, workspace)
        self.assertEqual(pre['status'], 'passed')
        return {'id': case['id'], 'http_status': 200, 'technical_completed': True,
                'preflight': pre, 'before': before, 'after': after, 'model_tool_calls': calls,
                'tool_outputs': outputs, 'model_projected_tool_outputs': deepcopy(outputs), 'tool_invocations': invocations,
                'result': {'reply': '本机模拟主账户预算已计入同一稳定信用卡承诺2000元；目标2500元，仅追加500元。真实预算情景返回可安排9776元，只规划，没有冻结、预留或付款。', 'bank_actions': []}}

    def x01_control(self, case):
        state = {'cash': 100}
        output = {'data': {'result_minor': 12345, 'currency': 'CNY', 'source': 'user_input_scenario'},
                  'money_signature': {'fixture': 'must_preserve_whole_signature', 'model': 'business_model_field'}}
        return {'id': case['id'], 'http_status': 200, 'technical_completed': True,
                'before': state, 'after': deepcopy(state), 'model_tool_calls': [],
                'tool_invocations': [{'name': 'money_calculation', 'arguments': {'values_minor': [12345]},
                                      'success': True, 'raw_output': output}],
                'tool_outputs': [{'call_id': 'local', 'output': output}],
                'model_projected_tool_outputs': [{'call_id': 'local', 'output': deepcopy(output)}],
                'result': {'reply': '按你明确给出的假设金额计算，结果123.45元，来源为用户假设；未读取账户或画像，也未修改资金或记忆。', 'bank_actions': [],
                           'agent_runtime': {'primary_model': 'DeepSeek-V4-Flash', 'duration_ms': 42,
                                             'intent_route': {'selection': {'status': 'skills', 'skill_ids': ['goal-planning'],
                                                                           'provider': 'jev', 'reason': 'original_route_selection_reason',
                                                                           'workflow': {'id': 'actual-workflow', 'provider': 'native'},
                                                                           'parameters': {'query': '实际业务主体'}},
                                                              'calls': [{'provider': 'jev', 'model_actual': 'jev-1.13.0'}]}},
                           'agent_review': {'status': 'approved', 'model_id': 'GLM-5.3-Flash'}}}

    def records(self, include_s10=False, failed=False, unknown=False, missing=False):
        records = []; by_id = {case['id']: case for case in self.cases}
        control_s10 = None
        for index, planned in enumerate(self.protocol['plan']):
            case = by_id[planned['case_id']]
            if case['id'].startswith('S10'):
                if include_s10:
                    if control_s10 is None:
                        control_s10 = self.real_s10_control(case)
                    row = deepcopy(control_s10)
                else:
                    row = {'id': case['id'], 'technical_completed': False, 'http_status': 503,
                           'runner_failure': {'code': 'zero-cloud-test-unexecuted-original'},
                           'result': {'reply': '零云原题失败控制，未生成业务答复。'}}
            else:
                row = self.x01_control(case)
            row.update(planned)
            row.update(protocol_sha256=self.protocol_sha, status='FINISHED' if row['technical_completed'] else 'FAILED',
                       started_at='2026-10-10T00:00:00+00:00', http_elapsed_ms=100 + index, worker_wall_ms=200 + index)
            request_body = {'model': 'DeepSeek-V4-Flash', 'messages': [
                {'role': 'system', 'content': '完整初始上下文单元控制',
                 'server_context': {'customer_profile': {'customer_name': '合成测试客户'},
                                    'account': {'balance_minor': 12345}}},
                {'role': 'user', 'content': case['prompt']},
                {'role': 'assistant', 'content': '可见业务历史', 'reasoning_content': '隐藏推理测试值'},
                {'role': 'tool', 'content': '完整后续工具结果业务证据'}]}
            row['provider_calls'] = [{'index': 0, 'stage': 'primary', 'request_sent': True, 'identity_verified': True,
                                       'request_sha256': s.sha(json.dumps(request_body, ensure_ascii=False).encode()),
                                       'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}]
            if failed and planned['variant'] == 'jev':
                row.update(technical_completed=False, status='FAILED', http_status=503,
                           runner_failure={'code': 'actual_route_failure_must_be_preserved'})
            if unknown and planned['variant'] == 'jev':
                row['provider_calls'][0]['usage'] = None
            row['cost'] = h.account_calls(row['provider_calls'])
            row['exit'] = {'reaped': True, 'exit_code': 0, 'process_group_still_exists': False}
            trial = self.out / 'trials' / f'{index + 1:03}-{planned["variant"]}-{case["id"]}'
            h.write(trial / 'requests' / '000.json', request_body)
            row['provider_calls'][0]['stored_request_json_sha256'] = s.digest(trial / 'requests' / '000.json')
            h.write(trial / 'start.json', {**planned, 'index': index, 'protocol_sha256': self.protocol_sha})
            h.write(trial / 'provider-calls.json', row['provider_calls'])
            h.write(trial / 'exit.json', row['exit'])
            if not (missing and planned['variant'] == 'jev'):
                h.write(trial / 'record.json', row)
            records.append(row)
        h.write(self.out / 'results.json', {'protocol_sha256': self.protocol_sha, 'planned_starts': len(records),
                                           'records': records, 'complete': True, 'campaign_configuration': 1, 'configuration_attempt': 1})
        return records

    def export(self):
        return s.export(self.out, self.review_dir, self.protocol_sha)

    def opinion(self, name='independent-human-fixture', fail=False):
        review = h.read(self.review_dir / 'review-template.json')
        review.update(reviewer=name, reviewed_at='2026-10-10T01:00:00+00:00', source_kind='human',
                      independent_of_generation=True, performance_numbers_seen=False,
                      reviewer_source_reference='offline unit-control reviewer; synthetic attestation, not actual business acceptance')
        packet = h.read(self.review_dir / 'blind-packet.json')
        items = {item['blind_id']: item for item in packet['items']}
        for row in review['items']:
            row.update(verdict='fail' if fail else 'pass', rationale='零云合成控制逐项裁决，不是模型表现或正式验收。')
            item = items[row['blind_id']]; reply = item['record']['result']['reply']
            for criterion in row['criteria']:
                criterion['verdict'] = ('present' if fail else 'absent') if criterion['kind'] == 'forbidden' else ('not_met' if fail else 'met')
                criterion['evidence'] = '单元测试对已保存的完整答案/实际工具轨迹作绑定控制；真实审查不得照抄预填裁决。'
                criterion['citations'] = [{'pointer': ['record', 'result', 'reply'],
                                          'value_sha256': s.sha(s.packed(reply).encode()), 'excerpt': reply}]
        path = Path(self.temp.name) / (name + '.json'); h.write(path, review)
        return path

    def import_one(self, review):
        return s.import_reviews(self.out, self.review_dir, [review], self.result_path, self.protocol_sha)

    def test_one_complete_import_accepts_prior_correct_controls_without_second_run(self):
        records = self.records(include_s10=True); originals = [s.packed(row) for row in records]
        with s.offline_guard():
            self.export(); result = self.import_one(self.opinion())
            summary = s.summarize(self.out, self.review_dir, self.result_path, self.summary_path, self.protocol_sha)
        self.assertEqual(result['all_started'], 12); self.assertEqual(result['pass_count'], 12)
        self.assertEqual(summary['adoption_verdict'], 'NOT_ASSESSED')
        self.assertEqual(summary['real_customer_holdout'], 0)
        self.assertEqual([s.packed(row) for row in h.read(self.out / 'results.json')['records']], originals)
        self.assertEqual(summary['comparisons']['jev_vs_off']['paired_case_repetitions'], 4)
        self.assertIsNotNone(summary['comparisons']['jev_vs_native']['metrics']['pass']['family_cluster_bootstrap_95_percent'])

    def test_full_original_s10_words_values_and_all_criteria_preserved(self):
        self.records(); self.export()
        packet = h.read(self.review_dir / 'blind-packet.json')
        original = next(case for case in self.cases if case['id'].startswith('S10'))['gold']
        items = [item for item in packet['items'] if item['gold'] == original]
        self.assertEqual(len(items), 6)
        for item in items:
            for field in ('must_convey', 'forbidden_conclusions', 'minimum_evidence', 'facts'):
                original_values = original['expected'][field]
                present = [criterion for criterion in item['criteria'] if criterion['pointer'][:3] == ['gold', 'expected', field]]
                self.assertEqual(len(present), len(original_values))
            self.assertTrue(any('977600' in criterion['criterion'] for criterion in item['criteria']))
            self.assertEqual(len([c for c in item['criteria'] if c['pointer'][:2] == ['gold', 'human_judgment']]), len(original['human_judgment']))
        self.assertEqual(packet['shared_context'], h.read(self.out / 'source/evaluation/window_integration/semantic_cases.json'))

    def test_blind_export_masks_explicit_model_but_preserves_signature_projection_and_route_behavior(self):
        self.records(failed=True); self.export()
        item = next(item for item in h.read(self.review_dir / 'blind-packet.json')['items'] if item['gold'].get('must_convey'))
        self.assertNotIn('DeepSeek-V4-Flash', s.packed(item['record']['result']))
        self.assertNotIn('GLM-5.3-Flash', s.packed(item['record']['result']))
        self.assertNotIn('jev-1.13.0', s.packed(item['record']['result']))
        self.assertEqual(item['record']['tool_outputs'], item['record']['model_projected_tool_outputs'])
        self.assertEqual(item['record']['tool_outputs'][0]['output']['money_signature']['model'], 'business_model_field')
        self.assertEqual(item['record']['result']['agent_runtime']['intent_route']['selection']['skill_ids'], ['goal-planning'])
        selection = item['record']['result']['agent_runtime']['intent_route']['selection']
        self.assertEqual(selection['reason'], 'original_route_selection_reason')
        self.assertIn('遮蔽', selection['provider']); self.assertIn('遮蔽', selection['workflow']['provider'])
        self.assertEqual(selection['workflow']['id'], 'actual-workflow')
        self.assertEqual(selection['parameters']['query'], '实际业务主体')
        self.assertEqual((self.review_dir / 'private-mapping.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(any(item['record'].get('runner_failure', {}).get('code') == 'actual_route_failure_must_be_preserved'
                            for item in h.read(self.review_dir / 'blind-packet.json')['items']))

    def test_reject_original_record_tamper(self):
        self.records(); self.export(); review = self.opinion()
        path = next((self.out / 'trials').glob('*/record.json')); data = h.read(path)
        data['result']['reply'] = '篡改后的答复'; h.write(path, data)
        with self.assertRaisesRegex(ValueError, 'original_record_or_mapping_changed'):
            self.import_one(review)

    def test_actual_model_inputs_preserve_initial_private_context_and_later_business_evidence(self):
        self.records(); self.export()
        packet = h.read(self.review_dir / 'blind-packet.json')
        item = next(item for item in packet['items'] if item['gold'].get('must_convey'))
        model_input = item['model_inputs'][0]
        self.assertTrue(item['model_inputs_complete'])
        self.assertIn('遮蔽', model_input['body']['model'])
        initial = model_input['body']['messages'][0]['server_context']
        self.assertEqual(initial['customer_profile']['customer_name'], '合成测试客户')
        self.assertEqual(initial['account']['balance_minor'], 12345)
        self.assertEqual(model_input['body']['messages'][-1]['content'], '完整后续工具结果业务证据')
        self.assertNotIn('reasoning_content', model_input['body']['messages'][2])
        self.assertNotIn('隐藏推理测试值', s.packed(packet))
        self.assertEqual(model_input['hidden_reasoning_fields_omitted'], 1)
        private = h.read(self.review_dir / 'private-mapping.json')
        pin = private['mapping'][0]['request_body_pins'][0]
        self.assertEqual(pin['stored_json_sha256'], pin['native_stored_json_sha256'])
        self.assertNotEqual(pin['native_actual_wire_sha256'], pin['stored_json_sha256'])
        review = self.opinion(); data = h.read(review)
        target = next(row for row in data['items'] if row['blind_id'] == item['blind_id'])
        target['verdict'] = 'fail'
        criterion = target['criteria'][0]; criterion['verdict'] = 'present' if criterion['kind'] == 'forbidden' else 'not_met'
        pointer = ['model_inputs', 0, 'body', 'messages', 0, 'server_context']
        value = s.select_pointer(item, pointer)
        criterion['citations'] = [{'pointer': pointer, 'value_sha256': s.sha(s.packed(value).encode()),
                                  'excerpt': '合成测试客户'}]
        h.write(review, data); result = self.import_one(review)
        self.assertFalse(next(row for row in result['records'] if row['blind_id'] == item['blind_id'])['pass'])

    def test_missing_sent_request_body_remains_started_failure(self):
        rows = self.records()
        index = next(i for i, row in enumerate(rows) if row['case_id'] == 'X01' and row['variant'] == 'jev')
        row = rows[index]; trial = self.out / 'trials' / f'{index + 1:03}-{row["variant"]}-{row["case_id"]}'
        (trial / 'requests' / '000.json').unlink()
        self.export(); result = self.import_one(self.opinion())
        self.assertEqual(result['all_started'], 12)
        result_row = next(value for value in result['records'] if value['index'] == index)
        self.assertFalse(result_row['technical_complete']); self.assertFalse(result_row['pass'])

    def test_actual_request_body_tamper_and_header_payload_rejected(self):
        self.records(); self.export(); review = self.opinion()
        path = next((self.out / 'trials').glob('*/requests/000.json'))
        original = h.read(path); changed = deepcopy(original)
        changed['messages'][0]['server_context']['account']['balance_minor'] = 1
        h.write(path, changed)
        with self.assertRaisesRegex(ValueError, 'stored_request_body_differs_from_native_ledger'):
            self.import_one(review)
        # 认证字段即使有人同步伪造全部本地pins，也不能进入公开业务判卷包。
        changed = deepcopy(original); changed['Authorization'] = 'placeholder-not-a-secret'
        h.write(path, changed)
        trial = path.parent.parent; ledger = h.read(trial / 'provider-calls.json')
        ledger[0]['stored_request_json_sha256'] = s.digest(path); h.write(trial / 'provider-calls.json', ledger)
        report = h.read(self.out / 'results.json'); index = int(trial.name.split('-', 1)[0]) - 1
        report['records'][index]['provider_calls'] = ledger; h.write(self.out / 'results.json', report)
        with self.assertRaisesRegex(ValueError, 'contains_headers_or_credential_fields'):
            s.frozen_input(self.out, self.protocol_sha)

    def test_reject_gold_tamper_and_frozen_code_tamper(self):
        self.records(); self.export(); review = self.opinion()
        path = self.out / 'cases.json'; data = h.read(path); data['cases'][0]['gold']['prompt'] = '改金标'; h.write(path, data)
        with self.assertRaisesRegex(ValueError, 'frozen_input_changed'):
            self.import_one(review)

    def test_reject_missing_started_or_missing_criterion_review(self):
        self.records(); self.export(); review = self.opinion()
        original = h.read(review); changed = deepcopy(original); changed['items'].pop(); h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'all_started_review_missing'):
            self.import_one(review)
        changed = deepcopy(original); changed['items'][0]['criteria'].pop(); h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'original_criteria_review_missing'):
            self.import_one(review)

    def test_reject_wrong_protocol_wrong_criterion_text_and_fabricated_citation(self):
        self.records(); self.export(); review = self.opinion(); original = h.read(review)
        for error, mutate in [
            ('review_binding', lambda value: value.update(protocol_sha256='0' * 64)),
            ('original_criterion', lambda value: value['items'][0]['criteria'][0].update(criterion='静默改标准')),
            ('evidence_excerpt', lambda value: value['items'][0]['criteria'][0]['citations'][0].update(excerpt='不存在的摘录')),
        ]:
            with self.subTest(error=error):
                changed = deepcopy(original); mutate(changed); h.write(review, changed)
                with self.assertRaisesRegex(ValueError, error):
                    self.import_one(review)

    def test_business_glm_approval_alone_and_preflight_as_answer_truth_are_rejected(self):
        self.records(); self.export(); review = self.opinion(); original = h.read(review)
        changed = deepcopy(original); changed.update(reviewer='operational_glm')
        h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'operational_glm'):
            self.import_one(review)
        changed = deepcopy(original); changed['items'][0]['criteria'][0]['citations'][0]['pointer'] = ['gold']
        h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'not_actual_answer_evidence'):
            self.import_one(review)
        changed = deepcopy(original)
        target = next(row for row in changed['items'] if any(c['pointer'][:3] == ['gold', 'expected', 'must_convey'] for c in row['criteria']))
        criterion = next(c for c in target['criteria'] if c['pointer'][:3] == ['gold', 'expected', 'must_convey'])
        criterion['citations'][0]['pointer'] = ['record', 'preflight']
        h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'preflight_is_not_model_answer_evidence'):
            self.import_one(review)

    def test_failed_and_missing_record_starts_never_become_pass_or_leave_denominator(self):
        self.records(failed=True, missing=True); self.export(); result = self.import_one(self.opinion())
        self.assertEqual(result['all_started'], 12)
        self.assertTrue(all(not row['pass'] for row in result['records'] if row['variant'] == 'jev'))
        summary = s.summarize(self.out, self.review_dir, self.result_path, self.summary_path, self.protocol_sha)
        self.assertEqual(summary['arms']['jev']['all_started'], 4)
        self.assertEqual(summary['arms']['jev']['penalized_mean_api_latency_ms'], 105000)
        self.assertIsNone(summary['arms']['jev']['estimated_upper_cny'])

    def test_unknown_native_usage_never_becomes_zero_cost_or_known_comparison(self):
        self.records(unknown=True); self.export(); self.import_one(self.opinion())
        summary = s.summarize(self.out, self.review_dir, self.result_path, self.summary_path, self.protocol_sha)
        self.assertIsNone(summary['arms']['jev']['estimated_upper_cny'])
        self.assertEqual(summary['arms']['jev']['unknown_cost_starts'], 4)
        cost = summary['comparisons']['jev_vs_off']['metrics']['cost_upper_cny']
        self.assertIsNone(cost['delta_jev_minus_baseline']); self.assertIsNone(cost['family_cluster_bootstrap_95_percent'])
        self.assertEqual(cost['unknown_metric_pairs'], 4)

    def test_started_without_record_and_failed_supervisor_remain_all_started_failures(self):
        records = self.records()
        target = next(index for index, row in enumerate(records) if row['variant'] == 'jev' and row['case_id'] == 'X01')
        row = records[target]; row.update(status='STARTED', technical_completed=False)
        trial = self.out / 'trials' / f'{target + 1:03}-{row["variant"]}-{row["case_id"]}'
        (trial / 'record.json').unlink()
        failed = next(index for index, row in enumerate(records) if row['variant'] == 'off' and row['case_id'] == 'X01')
        row = records[failed]; row['exit']['exit_code'] = 1
        trial = self.out / 'trials' / f'{failed + 1:03}-{row["variant"]}-{row["case_id"]}'
        h.write(trial / 'record.json', row); h.write(trial / 'exit.json', row['exit'])
        h.write(self.out / 'results.json', {'protocol_sha256': self.protocol_sha, 'records': records, 'complete': False})
        self.export(); result = self.import_one(self.opinion())
        self.assertEqual(result['all_started'], 12)
        pending = next(value for value in result['records'] if value['index'] == target)
        self.assertFalse(pending['pass']); self.assertFalse(pending['technical_complete'])
        failed_result = next(value for value in result['records'] if value['index'] == failed)
        self.assertFalse(failed_result['pass']); self.assertFalse(failed_result['technical_complete'])

    def test_citations_cannot_use_prompt_or_glm_bubble_as_only_completion_proof(self):
        self.records(); self.export(); review = self.opinion(); original = h.read(review)
        packet = h.read(self.review_dir / 'blind-packet.json'); by_id = {value['blind_id']: value for value in packet['items']}
        changed = deepcopy(original); target = changed['items'][0]; prompt = by_id[target['blind_id']]['prompt']
        target['criteria'][0]['citations'] = [{'pointer': ['prompt'], 'value_sha256': s.sha(s.packed(prompt).encode()), 'excerpt': prompt}]
        h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'prompt_alone'):
            self.import_one(review)
        changed = deepcopy(original)
        target = next(value for value in changed['items'] if by_id[value['blind_id']]['gold'].get('must_convey'))
        target['criteria'][0]['citations'][0]['pointer'] = ['record', 'result', 'agent_review', 'status']
        h.write(review, changed)
        with self.assertRaisesRegex(ValueError, 'operational_glm_approval_is_not_answer_evidence'):
            self.import_one(review)

    def test_missing_start_receipt_keeps_denominator_but_cannot_pass_or_claim_known_cost(self):
        rows = self.records()
        index = next(i for i, row in enumerate(rows) if row['case_id'] == 'X01' and row['variant'] == 'jev')
        row = rows[index]; trial = self.out / 'trials' / f'{index + 1:03}-{row["variant"]}-{row["case_id"]}'
        (trial / 'start.json').unlink()
        self.export(); result = self.import_one(self.opinion())
        self.assertEqual(result['all_started'], 12)
        self.assertFalse(next(value for value in result['records'] if value['index'] == index)['pass'])
        summary = s.summarize(self.out, self.review_dir, self.result_path, self.summary_path, self.protocol_sha)
        self.assertIsNone(summary['arms']['jev']['estimated_upper_cny'])

    def test_original_explicit_unknown_cost_not_overruled_by_partial_known_provider_ledger(self):
        rows = self.records()
        index = next(i for i, row in enumerate(rows) if row['case_id'] == 'X01' and row['variant'] == 'jev')
        row = rows[index]; row['cost']['known'] = False
        trial = self.out / 'trials' / f'{index + 1:03}-{row["variant"]}-{row["case_id"]}'
        h.write(trial / 'record.json', row)
        h.write(self.out / 'results.json', {'protocol_sha256': self.protocol_sha, 'records': rows, 'complete': True})
        self.export(); result = self.import_one(self.opinion())
        self.assertFalse(next(value for value in result['records'] if value['index'] == index)['pass'])
        summary = s.summarize(self.out, self.review_dir, self.result_path, self.summary_path, self.protocol_sha)
        self.assertIsNone(summary['arms']['jev']['estimated_upper_cny'])

    def test_report_truncation_cannot_hide_a_physically_started_trial(self):
        self.records(); report = h.read(self.out / 'results.json'); report['records'].pop()
        h.write(self.out / 'results.json', report)
        with self.assertRaisesRegex(ValueError, 'actual_started_inventory_missing_from_results'):
            self.export()

    def test_campaign_only_start_after_lost_trial_artifacts_cannot_leave_denominator(self):
        rows = self.records(); report = h.read(self.out / 'results.json')
        starts = [{**row, 'index': index, 'configuration_attempt': 1} for index, row in enumerate(rows)]
        ledger = (Path(self.temp.name) / 'campaign.json').resolve(); h.write(ledger, {'starts': starts})
        report.update(campaign_path=str(ledger), configuration_attempt=1)
        last = report['records'].pop(); trial = self.out / 'trials' / f'{len(rows):03}-{last["variant"]}-{last["case_id"]}'
        shutil.rmtree(trial); h.write(self.out / 'results.json', report)
        with self.assertRaisesRegex(ValueError, 'campaign_started_inventory_missing_from_results'):
            self.export()

    def test_generic_new_gold_fields_are_not_silently_omitted(self):
        case = {'id': 'unseen-D02', 'oracle': 'extended_readonly',
                'gold': {'must_convey': ['原公开题标准'], 'must_not_convey': ['不能编造'],
                         'new_business_constraint': {'period': '2026-10-03', 'read_only': True},
                         'original': {'publish_fixture_strings': ['来源常量不冒充答复标准']}}}
        criteria = s.criteria_for(case, {})
        self.assertEqual({value['id'] for value in criteria}, {
            'gold/must_convey/0', 'gold/must_not_convey/0',
            'gold/new_business_constraint/period', 'gold/new_business_constraint/read_only'})

    def test_independent_disagreement_is_retained_and_not_averaged_to_pass(self):
        self.records(); self.export()
        result = s.import_reviews(self.out, self.review_dir, [self.opinion('reviewer-a'), self.opinion('reviewer-b', fail=True)], self.result_path, self.protocol_sha)
        self.assertEqual(result['pass_count'], 0)
        self.assertTrue(all(row['disagreement'] for row in result['records']))

    def test_tampered_import_result_rejected_by_recomputing_every_verdict(self):
        self.records(); self.export(); self.import_one(self.opinion())
        data = h.read(self.result_path); data['records'][0]['pass'] = not data['records'][0]['pass']; h.write(self.result_path, data)
        with self.assertRaisesRegex(ValueError, 'imported_judging_result_changed'):
            s.summarize(self.out, self.review_dir, self.result_path, self.summary_path, self.protocol_sha)

    def test_offline_guard_denies_network_subprocess_and_secret_loader(self):
        import subprocess
        import urllib.request
        with s.offline_guard():
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                urllib.request.urlopen('https://api.typesafe.ai')
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                subprocess.run(['/Users/manton/CC/services/cc-secrets/cc-with-secrets', 'true'])

    def test_families_are_clustered_repetitions_not_counted_as_new_tasks(self):
        self.assertIsNone(s.bootstrap({'same-family': [1, 1, 1]}))
        self.assertEqual(s.bootstrap({'a': [1, 1], 'b': [1, 1]}, samples=50), [1, 1])
        self.assertEqual(s.bootstrap({'a': [0, 1], 'b': [-1, 0]}, samples=100), s.bootstrap({'a': [0, 1], 'b': [-1, 0]}, samples=100))


if __name__ == '__main__':
    unittest.main()
