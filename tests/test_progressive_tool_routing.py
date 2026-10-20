"""本地主循环验收：领域扩展、子技能扩展和未展示调用拒绝。"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from agent_runtime.bank_tools import native_tools
from agent_runtime.conversation import ConversationAgent
from agent_runtime.jev_agent import JevConversationAgent
from agent_runtime.tool_routing import TurnToolScope
from agent_runtime.skills import load_advisor
from agent_runtime.prompt_layers import method_body
from finance_gateway import FinanceGateway
from test_native_conversation import ScriptedPrimary, Reviewer, call, fact_reply
from test_skill_conversation import unavailable_answer
from test_jev_agent import Judge


class ObservedPrimary(ScriptedPrimary):
    def __init__(self, *steps):
        super().__init__(*steps)
        self.views = []
    def complete(self, messages, tools, **kwargs):
        self.views.append(deepcopy(tools))
        return super().complete(messages, tools, **kwargs)
    def serialized_request(self, messages, tools):
        return json.dumps({'messages':messages,'tools':tools}, ensure_ascii=False).encode()
    def request_input_bound(self, messages, tools):
        return len(self.serialized_request(messages, tools))


def names(view):
    return {schema['function']['name'] for schema in view}


class ProgressiveToolRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = FinanceGateway(Path(self.temp.name)/'bank.sqlite3')
        self.workspace, _ = self.gateway.create_session()
    def tearDown(self):
        self.temp.cleanup()

    def test_cross_domain_subskill_opens_public_evidence_in_next_round(self):
        primary = ObservedPrimary(call('load_skill', {'name':'asset-allocation'}),
            call('load_skill', {'name':'product-research'}), call('knowledge_status', {}), unavailable_answer)
        result = ConversationAgent(primary, Reviewer(), tool_routing='progressive').process(
            self.gateway, self.workspace, '请核验配置和产品资料的证据缺口')
        self.assertNotIn('products', names(primary.views[0]))
        self.assertIn('products', names(primary.views[1]))
        self.assertNotIn('knowledge_search', names(primary.views[1]))
        self.assertIn('knowledge_search', names(primary.views[2]))
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assertEqual(result['agent_runtime']['tool_routing']['active_skills'],
                         ['asset-allocation','product-research'])
        self.assertEqual(result['bank_actions'], [])

    def test_same_response_cannot_execute_a_hidden_tool_after_load(self):
        batch = call('load_skill', {'name':'payments'})
        batch['tool_calls'] += call('prepare_operation', {'action':'transfer'})['tool_calls']
        primary = ObservedPrimary(batch, call('account_summary', {}), fact_reply)
        before = self.gateway.overview(self.workspace)['total_cash_minor']
        result = ConversationAgent(primary, Reviewer(), tool_routing='progressive').process(
            self.gateway, self.workspace, '查询余额并了解支付功能')
        self.assertNotIn('prepare_operation', names(primary.views[0]))
        self.assertIn('prepare_operation', names(primary.views[1]))
        rejection = next(row for row in result['agent_runtime']['trace'] if row.get('code')=='TOOL_SCOPE_REQUIRED')
        self.assertEqual(rejection['tool'], 'prepare_operation')
        self.assertEqual(self.gateway.overview(self.workspace)['total_cash_minor'], before)
        self.assertEqual(result['bank_actions'], [])

    def test_jev_high_confidence_opens_domain_and_low_confidence_keeps_discovery(self):
        for confidence in (1.0, 0.2):
            with self.subTest(confidence=confidence):
                primary = ObservedPrimary(call('account_summary', {}), fact_reply)
                result = JevConversationAgent(primary, Reviewer(), tool_routing='progressive',
                    judge=Judge(selection='asset-allocation', confidence=confidence), jev_mode='active').process(
                        self.gateway, self.workspace, '查询当前余额和配置条件')
                self.assertEqual('products' in names(primary.views[0]), confidence==1.0)
                self.assertEqual(result['agent_runtime']['jev']['selection']['applied'], confidence==1.0)
                self.assertEqual(result['agent_runtime']['total_tokens'], 13)

    def test_explicit_host_scope_remains_cap_even_after_loading_other_skill(self):
        scope = TurnToolScope(allowed_tools={'monthly_statement','statement_details','answer_facts'})
        scope.add_skill('payments')
        schemas = scope.project(native_tools(financial_mcp_tools=()))
        self.assertEqual(names(schemas), {'monthly_statement','statement_details','answer_facts'})

    def test_composite_history_query_and_saved_research_have_read_tools(self):
        scope = TurnToolScope()
        scope.add_skill('cashflow')
        self.assertIn('statement_details', names(scope.project(native_tools(financial_mcp_tools=()))))
        scope.add_skill('product-research')
        self.assertIn('research_plans', names(scope.project(native_tools(financial_mcp_tools=()))))

    def test_financial_tools_only_when_host_enabled_and_domain_loaded(self):
        scope = TurnToolScope(financial_tools={'financial_list_accounts'})
        schemas = native_tools(financial_mcp_tools={'financial_list_accounts'})
        self.assertNotIn('financial_list_accounts', names(scope.project(schemas)))
        scope.add_skill('financial-diagnosis')
        self.assertIn('financial_list_accounts', names(scope.project(schemas)))
        self.assertNotIn('financial_list_transactions', names(scope.project(schemas)))

    def test_repeated_method_load_has_one_body_and_reviewer_gets_receipts(self):
        primary=ObservedPrimary(call('load_skill',{'name':'product-research'}),
            call('load_skill',{'name':'product-research'}),call('knowledge_status',{}),unavailable_answer)
        reviewer=Reviewer()
        result=ConversationAgent(primary,reviewer,tool_routing='progressive').process(
            self.gateway,self.workspace,'研究产品条款资料是否可用')
        body=method_body(load_advisor('product-research')['instructions'])
        for turn in primary.messages[1:]:
            self.assertEqual(sum((message.get('content') or '').count(body) for message in turn),1)
        methods=[e for e in reviewer.packets[0]['evidence'] if e['source']=='maintainer_skill']
        self.assertTrue(methods)
        self.assertTrue(all('instructions' not in e['data'] and 'instructions_sha256' in e['data'] for e in methods))
        data=next(e for e in reviewer.packets[0]['evidence'] if e['tool']=='knowledge_status')['data']
        self.assertIn('ready',data)
        self.assertEqual(result['agent_review']['status'],'approved')


if __name__ == '__main__':
    unittest.main()
