"""获批协商三态及申请分支合同；已知反例回归，全部临时、实际HTTP/SQL。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_cross_module as _cross
import test_market_voice_negotiation as _voice
import test_negotiation_consent as _consent

ORACLE_PATH = _cross.ROOT / 'evaluation/window_integration/negotiation_condition_oracles.json'
ORACLE_BYTES = ORACLE_PATH.read_bytes()
ORACLE = json.loads(ORACLE_BYTES)
ORACLE_HASH = hashlib.sha256(ORACLE_BYTES).hexdigest()
FROZEN_ORACLE_HASH = '8c0810a9ba16c2d25e8bbb7e5cc26bbb4e2a691cb982cda75c1479b02d34d2aa'
CASES = {case['test']: case for case in ORACLE['cases']}
EXPECT = ORACLE['expectations']
NC_PATH = _cross.ROOT / 'evaluation/window_integration/negotiation_consent_oracles.json'
NC = json.loads(NC_PATH.read_text(encoding='utf-8'))
NC_HASH = '8dd528f6f3ee0e176e9d2c7c4086b6e0d465e675156314aedcacb5dd32cf267d'
U12_PATH = _cross.ROOT / 'evaluation/window_integration/unseen_task_cases.json'
U12_HASH = '67f3517e13778c4962c365e9c8a4575dd1de9e9084c94fcca44a10e184399472'


def state_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


class NegotiationConditionIntegrationTests(unittest.TestCase):
    maxDiff = None
    raw_state = _cross.CrossModuleIntegrationTests.raw_state
    operation_count = _cross.CrossModuleIntegrationTests.operation_count
    local_server = _voice.MarketVoiceNegotiationIntegrationTests.local_server
    http_json = _voice.MarketVoiceNegotiationIntegrationTests.http_json
    need = _voice.MarketVoiceNegotiationIntegrationTests.need
    rules_copy = _consent.NegotiationConsentIntegrationTests.rules_copy
    counts = _consent.NegotiationConsentIntegrationTests.counts

    def setUp(self):
        self.assertEqual(ORACLE_HASH, FROZEN_ORACLE_HASH, '新增合同冻结后不可按实现改预期')
        self.assertEqual(hashlib.sha256(NC_PATH.read_bytes()).hexdigest(), NC_HASH, '原NC黄金不得改变')
        self.assertEqual(hashlib.sha256(U12_PATH.read_bytes()).hexdigest(), U12_HASH, '原U12黄金不得改变')
        _cross.CrossModuleIntegrationTests.setUp(self)
        self.need(self.g.service_negotiations, 'rules', 'preview', 'accept', 'applications')
        self.baseline_rules = copy.deepcopy(self.g.service_negotiations.rules())
        self.rules_copy(self._testMethodName)

    def evidence(self, **values):
        print('WINDOW_NEGOTIATION_CONDITIONS_EVIDENCE ' + json.dumps({
            'case': CASES[self._testMethodName]['id'], 'test': self._testMethodName,
            'oracle_sha256': ORACLE_HASH, 'original_nc_sha256': NC_HASH,
            'original_u12_sha256': U12_HASH, 'financial_contract_sha256': _cross.CONTRACT_HASH,
            'scope': 'temporary_synthetic_current_source', 'known_regression_or_contract_extension': True,
            'formal_three_rounds': False, 'cloud_calls': 0, 'transport': 'actual_loopback_http_sql',
            **values}, ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def preview(self, token, text):
        status, result = self.http_json('POST', '/api/finance/service-negotiations/preview', {
            'text': text, 'share_memory': False}, token=token)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['status'], 'previewed', result)
        self.assertIsInstance(result['draft_id'], str)
        return result

    def observed(self, owner, preview, before):
        current = self.raw_state(owner)
        return {'workspace': owner, 'conditions': preview.get('conditions'),
            'salary_offers': [r['offer_id'] for r in preview['offers'] if r.get('requires_salary_deposit')],
            'offer_ids': [r['offer_id'] for r in preview['offers']], 'message': preview.get('message'),
            'state': self.counts(owner), 'financial_state_hashes': [state_sha(before), state_sha(current)],
            'financial_state_unchanged': current == before}

    def assert_no_application(self, observation):
        self.assertEqual(observation['state']['applications'], 0)
        self.assertEqual(observation['state']['submission_audits'], 0)
        self.assertEqual(observation['state']['financial_operations'], 0)
        self.assertTrue(observation['financial_state_unchanged'])

    def assert_state(self, observation, state):
        conditions = observation['conditions']
        self.assertIsInstance(conditions, dict)
        for key, expected in ORACLE['states'][state].items():
            self.assertIs(conditions.get(key), expected, key + ' 必须符合明确的人类三态预期')
        self.assertIn(EXPECT['electronic_offer_id'], observation['offer_ids'], '独立电子账单不能随工资条件一起阻断')
        expected_salary = [EXPECT['salary_offer_id']] if state == 'consented' else []
        self.assertEqual(observation['salary_offers'], expected_salary)

    def test_explicit_consent_and_refusal_have_distinct_states(self):
        with self.local_server():
            for sample in CASES[self._testMethodName]['samples']:
                with self.subTest(state=sample['state']):
                    owner, token = self.g.create_session()
                    before = self.raw_state(owner)
                    preview = self.preview(token, sample['text'])
                    observation = self.observed(owner, preview, before)
                    self.evidence(text=sample['text'], expected_state=sample['state'], **observation)
                    self.assert_no_application(observation)
                    self.assert_state(observation, sample['state'])

    def test_unknown_salary_keeps_electronic_offer_and_requests_clarification(self):
        with self.local_server():
            for index, sample in enumerate(CASES[self._testMethodName]['samples'], 1):
                with self.subTest(sample=index, origin=sample['origin']):
                    owner, token = self.g.create_session()
                    before = self.raw_state(owner)
                    preview = self.preview(token, sample['text'])
                    observation = self.observed(owner, preview, before)
                    self.evidence(sample=index, text=sample['text'], origin=sample['origin'],
                                  expected_state='unknown', **observation)
                    self.assert_no_application(observation)
                    self.assert_state(observation, 'unknown')
                    message = preview.get('message', '')
                    self.assertIsInstance(message, str)
                    self.assertRegex(message,
                        r'(?:工资|薪资).{0,60}(?:明确|确认|决定|澄清)|(?:请|需要|尚未|未明确).{0,45}(?:工资|薪资)',
                        '未知工资条件须通过已有message明确请求澄清，不能只隐藏方案')

    def test_electronic_acceptance_is_independent_and_replay_submits_once(self):
        before = self.raw_state()
        with self.local_server():
            preview = self.preview(self.token, CASES[self._testMethodName]['text'])
            observation = self.observed(self.w, preview, before)
            self.assert_state(observation, 'refused')
            self.assert_no_application(observation)
            path = '/api/finance/service-negotiations/' + preview['draft_id'] + '/accept'
            body = {'offer_id': EXPECT['electronic_offer_id'], 'confirmed': True}
            status, accepted = self.http_json('POST', path, body)
            replay_status, replay = self.http_json('POST', path, body)
            listed_status, listed = self.http_json('GET', '/api/finance/service-negotiations')
            counts = self.counts(self.w)
            after = self.raw_state()
            self.evidence(workspace=self.w, status=status, replay_status=replay_status,
                response=accepted, replay=replay, state=counts,
                financial_state_hashes=[state_sha(before), state_sha(after)], financial_state_unchanged=after == before)
            self.assertEqual((status, replay_status, listed_status), (200, 200, 200))
            self.assertEqual(accepted['application_id'], replay['application_id'])
            self.assertEqual(accepted['terms'], replay['terms'])
            self.assertFalse(accepted['idempotent_replay'])
            self.assertTrue(replay['idempotent_replay'])
            self.assertEqual(accepted['status'], EXPECT['accepted_status'])
            self.assertEqual(replay['status'], EXPECT['accepted_status'])
            self.assertFalse(accepted['terms']['requires_salary_deposit'])
            self.assertFalse(accepted['terms']['requires_investment_purchase'])
            self.assertEqual([r['application_id'] for r in listed['applications']], [accepted['application_id']])
            self.assertEqual((counts['applications'], counts['submission_audits'], counts['financial_operations']), (1, 1, 0))
            self.assertEqual(after, before)

    def test_known_salary_refusals_cannot_accept_excluded_payroll_offer(self):
        original = next(case for case in NC['cases'] if case['id'] == 'NC01')['requests']
        samples = original + CASES[self._testMethodName]['additional_requests']
        with self.local_server():
            for index, text in enumerate(samples, 1):
                with self.subTest(request=index):
                    owner, token = self.g.create_session()
                    before = self.raw_state(owner)
                    preview = self.preview(token, text)
                    status, result = self.http_json('POST', '/api/finance/service-negotiations/'
                        + preview['draft_id'] + '/accept', {'offer_id': EXPECT['salary_offer_id'],
                                                         'confirmed': True}, token=token)
                    observation = self.observed(owner, preview, before)
                    self.evidence(request=index, text=text, source='原NC01' if index <= len(original) else '已知普通否定句',
                        expected_state='refused', accept_status=status, response=result, **observation)
                    self.assert_state(observation, 'refused')
                    self.assertEqual(status, 409, result)
                    self.assertTrue(result.get('error', {}).get('code'))
                    self.assert_no_application(observation)

    def test_trusted_terms_or_eligibility_change_rejects_old_confirmation(self):
        original = next(case for case in NC['cases'] if case['id'] == 'NC02')
        with self.local_server():
            for fault in original['faults']:
                with self.subTest(fault=fault['id']):
                    owner, token = self.g.create_session()
                    before = self.raw_state(owner)
                    _, path, rules = self.rules_copy(fault['id'])
                    preview = self.preview(token, original['request'])
                    salary = fault['id'] == 'salary_eligibility_changed'
                    offer = next(row for row in preview['offers'] if bool(row['requires_salary_deposit']) == salary)
                    current = next(row for row in rules['offers'] if row['offer_id'] == offer['offer_id'])
                    if salary:
                        current['minimum_monthly_salary_minor'] += 300000
                        current['salary_months_required'] += 2
                        current['conditions'] = ['修订后需连续5个月每月工资入账不少于人民币9,000元',
                                                 '资格须经模拟规则核验后生效']
                    else:
                        current.update(monthly_value_minor=250, duration_months=2, max_cost_minor=500,
                            title='隔离修订电子账单优惠（2个月）', conditions=['修订额度和期限'])
                    rules_before = hashlib.sha256(path.read_bytes()).hexdigest()
                    path.write_text(json.dumps(rules, ensure_ascii=False), encoding='utf-8')
                    rules_after = hashlib.sha256(path.read_bytes()).hexdigest()
                    self.assertNotEqual(rules_before, rules_after)
                    self.assertEqual(rules['schema_version'], preview['rule_version'])
                    status, result = self.http_json('POST', '/api/finance/service-negotiations/'
                        + preview['draft_id'] + '/accept', {'offer_id': offer['offer_id'],
                                                          'confirmed': True}, token=token)
                    observation = self.observed(owner, preview, before)
                    self.evidence(fault=fault['id'], original_offer=offer, current_offer=current,
                        rules_hashes=[rules_before, rules_after], accept_status=status, response=result, **observation)
                    self.assertEqual(status, 409, result)
                    self.assertTrue(result.get('error', {}).get('code'))
                    self.assertEqual([row['id'] for row in observation['state']['drafts']], [preview['draft_id']],
                                     '旧确认不得隐形重建替代草案')
                    self.assert_no_application(observation)


if __name__ == '__main__':
    unittest.main()
