"""金额用途与留款读取顺序的确定性回归；不代替云模型语义验收。"""
import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.bank_tools import BankTools, native_tools
from agent_runtime.conversation import ConversationAgent
from finance_gateway import FinanceGateway
from simulated_bank import BankError
from test_native_conversation import call, last_tool, ScriptedPrimary, Reviewer


class FinancialContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.g = FinanceGateway(Path(self.temp.name) / 'bank.sqlite3')
        self.w, _ = self.g.create_session()
        self.t = BankTools(self.g, self.w, 'contract-test')
        self.aid = next(a['id'] for a in self.g.overview(self.w)['accounts'] if a['id'].endswith('-main'))

    def tearDown(self):
        self.temp.cleanup()

    def test_reserve_requires_observed_budget_and_does_not_emit_wrong_scenario(self):
        before = self.g.overview(self.w)
        with self.assertRaises(BankError) as error:
            self.t.invoke('budget', {'account_id': self.aid, 'reserves': [
                {'label': '信用卡本期款', 'amount_minor': 250000, 'commitment_ids': []}]})
        self.assertEqual(error.exception.code, 'BUDGET_BASELINE_REQUIRED')
        self.assertIsNone(self.t.current_budget)
        self.assertFalse(self.t.facts)
        after = self.g.overview(self.w)
        for key in ('accounts', 'orders', 'holdings', 'fund_holds', 'profile'):
            self.assertEqual(before[key], after[key])

    def test_linked_credit_target_only_adds_difference_and_keeps_current_basis(self):
        original_holds = self.g.overview(self.w)['fund_holds']
        baseline = self.t.invoke('budget', {'account_id': self.aid})['data']
        credit = next(c for c in baseline['commitments'] if c['kind'] == 'credit_card')
        planned = self.t.invoke('budget', {'account_id': self.aid, 'reserves': [
            {'label': '信用卡本期款', 'amount_minor': 250000, 'commitment_ids': [credit['evidence_id']]}]})
        self.assertEqual(baseline['investable_minor'], 1027600)
        self.assertEqual(planned['data']['baseline_investable_minor'], 1027600)
        self.assertEqual(planned['data']['additional_reserve_minor'], 50000)
        self.assertEqual(planned['data']['investable_minor'], 977600)
        self.assertEqual(planned['data']['budget_scope'], 'planning_scenario')
        detail = planned['data']['planning_reserves'][0]
        self.assertEqual(detail['already_budgeted_minor'], 200000)
        facts = [fact['text'] for fact in planned['facts']]
        self.assertTrue(any('情景可安排资金：9,776.00元' in text for text in facts))
        self.assertFalse(any('当前可安排资金：9,776.00元' in text for text in facts))
        self.assertEqual(self.g.overview(self.w)['fund_holds'], original_holds)

    def test_observed_links_are_account_scoped_and_listed_in_native_schema(self):
        baseline = self.t.invoke('budget', {'account_id': self.aid})['data']
        credit = next(c for c in baseline['commitments'] if c['kind'] == 'credit_card')
        schema = next(t['function']['parameters'] for t in native_tools(identifiers=self.t.identifiers)
                      if t['function']['name'] == 'budget')
        ids = schema['properties']['reserves']['items']['properties']['commitment_ids']['items']['enum']
        self.assertIn(credit['evidence_id'], ids)
        other = next(a['id'] for a in self.g.overview(self.w)['accounts'] if a['id'] != self.aid)
        self.t.invoke('budget', {'account_id': other})
        with self.assertRaises(BankError) as error:
            self.t.invoke('budget', {'account_id': other, 'reserves': [
                {'label': '错误跨账户关联', 'amount_minor': 250000, 'commitment_ids': [credit['evidence_id']]}]})
        self.assertEqual(error.exception.code, 'INVALID_RESERVE_LINK')

    def test_future_available_is_not_a_current_budget_fact(self):
        output = self.t.invoke('cashflow', {'days': 60})
        main = next(a for a in output['data']['accounts'] if a['account_id'] == self.aid)
        self.assertEqual(main['current_available_minor'], 1289000)
        self.assertEqual(main['minimum_available_if_candidates_occur_minor'], 966400)
        self.assertFalse(main['candidate_floor_breached'])
        texts = [fact['text'] for fact in output['facts']]
        self.assertTrue(any('尚未扣未来事项的底线外余量' in text for text in texts))
        self.assertFalse(any('当前可安排资金' in text for text in texts))

    def test_unreviewed_planning_value_is_excluded_from_fallback(self):
        def baseline(messages, tools):
            accounts = last_tool(messages)['data']['accounts']
            aid = next(a['id'] for a in accounts if a['id'].endswith('-main'))
            return call('budget', {'account_id': aid})
        def new_reserve(messages, tools):
            return call('budget', {'account_id': last_tool(messages)['data']['account_id'], 'reserves': [
                {'label': '全新独立留款', 'amount_minor': 250000, 'commitment_ids': []}]})
        agent = ConversationAgent(ScriptedPrimary(call('account_summary', {}), baseline, new_reserve),
                                  Reviewer(), max_rounds=3)
        result = agent.process(self.g, self.w, '先做独立留款情景，不执行。')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['agent_runtime']['failure'], 'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(result['budget']['investable_minor'], 777600)
        self.assertNotIn('7,776', result['reply'])
        self.assertIn('当前可安排资金：10,276.00元', result['reply'])
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'], 2439000)


if __name__ == '__main__':
    unittest.main()
