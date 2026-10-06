"""两项独立协商验收：拒绝迁工资、旧方案接受前条款/资格变化。

先冻结人类预期再实现测试；真实 HTTP 与临时 SQLite 为证据。不得按
当前业务结果调整预期；同意电子账单不能扩张为同意迁工资或新条款。
"""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import test_cross_module as _cross
import test_market_voice_negotiation as _voice

from service_negotiation import ServiceNegotiationService

ORACLE_PATH = _cross.ROOT / 'evaluation/window_integration/negotiation_consent_oracles.json'
ORACLE_BYTES = ORACLE_PATH.read_bytes()
ORACLE = json.loads(ORACLE_BYTES)
ORACLE_HASH = hashlib.sha256(ORACLE_BYTES).hexdigest()
FROZEN_ORACLE_HASH = '8dd528f6f3ee0e176e9d2c7c4086b6e0d465e675156314aedcacb5dd32cf267d'
CASES = {case['test']: case for case in ORACLE['cases']}


class NegotiationConsentIntegrationTests(unittest.TestCase):
    maxDiff = None
    raw_state = _cross.CrossModuleIntegrationTests.raw_state
    operation_count = _cross.CrossModuleIntegrationTests.operation_count
    local_server = _voice.MarketVoiceNegotiationIntegrationTests.local_server
    http_json = _voice.MarketVoiceNegotiationIntegrationTests.http_json
    need = _voice.MarketVoiceNegotiationIntegrationTests.need

    def setUp(self):
        self.assertEqual(ORACLE_HASH, FROZEN_ORACLE_HASH, '不得按当前实现改冻结预期')
        self.assertEqual(len(CASES), 2)
        _cross.CrossModuleIntegrationTests.setUp(self)
        self.need(self.g.service_negotiations, 'rules', 'preview', 'accept', 'applications')
        self.baseline_rules = copy.deepcopy(self.g.service_negotiations.rules())

    def evidence(self, **values):
        name = self.id().split('.')[-1]
        print('WINDOW_NEGOTIATION_CONSENT_EVIDENCE ' + json.dumps({
            'case': CASES[name]['id'], 'test': name, 'oracle_sha256': ORACLE_HASH,
            'original_financial_contract_sha256': _cross.CONTRACT_HASH,
            'scope': 'temporary_synthetic_current_source', 'transport': 'actual_loopback_http',
            'cloud_models_called': False, 'formal_ten_questions_three_rounds': False,
            'responsible_owner': ORACLE['responsibility']['business_owner'], **values},
            ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def rules_copy(self, suffix):
        rules = copy.deepcopy(self.baseline_rules)
        path = self.directory / ('consent-rules-' + suffix + '.json')
        path.write_text(json.dumps(rules, ensure_ascii=False), encoding='utf-8')
        self.assertTrue(path.resolve().is_relative_to(self.directory))
        service = ServiceNegotiationService(self.g, path)
        self.g.service_negotiations = service
        return service, path, rules

    def counts(self, owner):
        with closing(self.g.bank.connect()) as db:
            drafts = [dict(row) for row in db.execute(
                'SELECT id,status FROM service_negotiation_drafts WHERE workspace_id=? ORDER BY id', (owner,))]
            applications = db.execute('SELECT count(*) FROM service_negotiation_applications '
                                      'WHERE workspace_id=?', (owner,)).fetchone()[0]
            submitted = db.execute("SELECT count(*) FROM audit WHERE workspace_id=? "
                                   "AND event='service_negotiation_application_submitted'", (owner,)).fetchone()[0]
        return {'drafts': drafts, 'applications': applications, 'submission_audits': submitted,
                'financial_operations': self.operation_count(owner)}

    def test_explicit_salary_refusals_never_become_salary_consent(self):
        before = self.raw_state()
        self.rules_copy('refusals')
        electronic_ids = {rule['offer_id'] for rule in self.baseline_rules['offers']
                          if not rule.get('requires_salary_deposit') and not rule.get('requires_investment_purchase')}
        self.assertTrue(electronic_ids, 'fixture 必须提供不需迁工资的电子账单方案')
        with self.local_server():
            for index, text in enumerate(CASES[self._testMethodName]['requests'], 1):
                with self.subTest(request=index):
                    status, preview = self.http_json('POST', '/api/finance/service-negotiations/preview', {
                        'text': text, 'share_memory': False})
                    self.assertEqual(status, 200, preview)
                    self.assertEqual(preview['status'], 'previewed', preview)
                    salary_intent = preview['conditions']['salary_deposit_intent']
                    salary_offers = [offer['offer_id'] for offer in preview['offers']
                                     if offer.get('requires_salary_deposit')]
                    non_salary_offers = {offer['offer_id'] for offer in preview['offers']
                                        if not offer.get('requires_salary_deposit')}
                    counts = self.counts(self.w)
                    unchanged = self.raw_state() == before
                    matched = (salary_intent is False and not salary_offers
                               and bool(electronic_ids & non_salary_offers)
                               and counts['applications'] == counts['submission_audits']
                               == counts['financial_operations'] == 0 and unchanged)
                    # 失败样本也保存实际返回与数据库证据，不只输出成功结论。
                    self.evidence(request=index, text=text, http_status=status, salary_intent=salary_intent,
                        salary_offers=salary_offers, non_salary_offers=sorted(non_salary_offers),
                        state=counts, financial_state_unchanged=unchanged, oracle_matched=matched)
                    self.assertEqual(counts['applications'], 0)
                    self.assertEqual(counts['submission_audits'], 0)
                    self.assertEqual(counts['financial_operations'], 0)
                    self.assertTrue(unchanged)
                    self.assertFalse(salary_intent, '明确拒绝迁工资不能被解析为同意')
                    self.assertEqual(salary_offers, [], '不得展示以迁工资为资格的方案')
                    self.assertTrue(electronic_ids & non_salary_offers, '同意的电子账单方案应可保留')

    def test_changed_trusted_terms_or_eligibility_reject_original_draft(self):
        case = CASES[self._testMethodName]
        with self.local_server():
            for fault in case['faults']:
                with self.subTest(fault=fault['id']):
                    # 每个变更样本独立工作区，前个失败写出的申请不能污染后个裁判。
                    owner, token = self.g.create_session()
                    before = self.raw_state(owner)
                    service, path, rules = self.rules_copy(fault['id'])
                    status, preview = self.http_json('POST', '/api/finance/service-negotiations/preview', {
                        'text': case['request'], 'share_memory': False}, token=token)
                    self.assertEqual(status, 200, preview)
                    self.assertEqual(preview['status'], 'previewed')
                    select_salary = fault['id'] == 'salary_eligibility_changed'
                    original_offer = next(offer for offer in preview['offers']
                                          if bool(offer.get('requires_salary_deposit')) == select_salary)
                    changed_offer = next(offer for offer in rules['offers']
                                         if offer['offer_id'] == original_offer['offer_id'])
                    original_terms = copy.deepcopy(changed_offer)
                    if fault['id'] == 'benefit_terms_changed':
                        changed_offer.update(monthly_value_minor=250, duration_months=2, max_cost_minor=500,
                            title='隔离修订后的电子账单服务费减免（2个月）',
                            conditions=['修订后须启用电子账单通知，额度和期限已变化'])
                    elif fault['id'] == 'salary_eligibility_changed':
                        changed_offer['minimum_monthly_salary_minor'] += 300000
                        changed_offer['salary_months_required'] += 2
                        changed_offer['conditions'] = ['修订后需连续5个月每月工资入账不少于人民币9,000元',
                                                       '资格须经模拟规则核验后生效']
                    else:
                        self.fail('未冻结的额外异常不得加入当前验收')
                    self.assertNotEqual(original_terms, changed_offer)
                    # 与实际受信路径相同，换正文而不换 schema_version 或 offer_id。
                    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    path.write_text(json.dumps(rules, ensure_ascii=False), encoding='utf-8')
                    after_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    self.assertNotEqual(before_hash, after_hash)
                    reread = service.rules()
                    self.assertEqual(reread['schema_version'], preview['rule_version'])
                    self.assertIn(original_offer['offer_id'], [offer['offer_id'] for offer in reread['offers']])
                    accept_status, result = self.http_json('POST', '/api/finance/service-negotiations/'
                        + preview['draft_id'] + '/accept', {'offer_id': original_offer['offer_id'],
                                                          'confirmed': True}, token=token)
                    counts = self.counts(owner)
                    unchanged = self.raw_state(owner) == before
                    error = result.get('error', {})
                    old_draft_only = [draft['id'] for draft in counts['drafts']] == [preview['draft_id']]
                    matched = (accept_status == 409 and bool(error.get('code'))
                               and counts['applications'] == counts['submission_audits']
                               == counts['financial_operations'] == 0 and unchanged and old_draft_only)
                    self.evidence(fault=fault['id'], workspace=owner, preview_draft=preview['draft_id'],
                        offer_id=original_offer['offer_id'], rules_hashes=[before_hash, after_hash],
                        original_terms=original_terms, current_terms=changed_offer,
                        accept_status=accept_status, response=result, state=counts,
                        financial_state_unchanged=unchanged, old_draft_only=old_draft_only,
                        oracle_matched=matched)
                    self.assertTrue(unchanged)
                    self.assertEqual(counts['financial_operations'], 0)
                    self.assertTrue(old_draft_only, '旧确认不得隐形创建替代草案')
                    self.assertEqual(accept_status, 409, '本人未确认新条款，旧方案必须拒绝')
                    self.assertTrue(error.get('code'), '拒绝须有明确错误码')
                    self.assertEqual(counts['applications'], 0, '条款变化不能提交申请')
                    self.assertEqual(counts['submission_audits'], 0)


if __name__ == '__main__':
    unittest.main()
