"""新技能进入真实主循环的协议验证；脚本模型不证明金融语义质量。"""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from agent_runtime.conversation import ConversationAgent
from agent_runtime.bank_tools import BankTools
from agent_runtime.skills import describe
from finance_gateway import FinanceGateway
from simulated_bank import BankError
from test_native_conversation import call, last_tool, ScriptedPrimary, Reviewer


def unavailable_answer(messages, tools):
    output = last_tool(messages)
    return call('answer_reviewed', {
        'text':'公共知识检索尚未接入。现在可以梳理研究条件，当前费用和在售状态需查证。',
        'claims':[{'text':'本轮公共知识检索不可用。','evidence_ids':[output['evidence_id']]}],
        'draft_ids':[],'followups':[]})


class PublicFixture:
    """合成公共条款，仅验证消费合同；不表示任何真实补助或金融产品。"""
    def __init__(self):
        self.withdrawn = False
        excerpt = '符合原文条件的申请人可领取补助5000元。'
        self.hit = {
            'schema_version':1,'citation_id':'citation-1','release_id':'release-1',
            'document_id':'document-1','document_version_id':'version-1',
            'parse_id':'parse-1','span_id':'span-1','title':'合成政策夹具',
            'publisher':'合成来源','source_url':'https://example.test/policy',
            'excerpt':excerpt,'locator':{'page':1,'paragraph':None,'start_char':0,'end_char':len(excerpt)},
            'product_id':None,'share_class_id':None,'channel_id':None,
            'published_on':'2026-01-01','retrieved_on':'2026-10-05',
            'effective_from':'2026-01-01','effective_to':'2027-01-01',
            'conditions':['需符合原文条件，未核验个人资格'],'facets':['general'],
            'visibility':'public','outbound_allowed':True,'withdrawn':False,
            'content_status':'full_text'}
    def status(self):
        return {'schema_version':1,'ready':True,'provider_id':'fixture','public_release_id':'release-1'}
    def public_catalog(self):
        # 只声明这份合成公开政策的标题、金额概念与原文稳定标识。
        # 不将待测查询中的任意词添加为准入词，查询边界仍由运行时核验。
        return {'terms':[self.hit['title'], '政策', '补助', '金额'],
                'identifiers':[self.hit[key] for key in (
                    'citation_id', 'release_id', 'document_id',
                    'document_version_id', 'parse_id', 'span_id')]}
    def search_public(self, request, release_id):
        return {'schema_version':1,'release_id':release_id,'citation_ids':['citation-1'],'coverage_gaps':[]}
    def resolve_public_evidence(self, citation_id, release_id, as_of):
        return {**deepcopy(self.hit),'withdrawn':self.withdrawn}


def public_answer(messages, tools):
    output = next(last_tool([message]) for message in reversed(messages)
                  if message['role']=='tool' and 'hits' in last_tool([message]).get('data',{}))
    return call('answer_reviewed', {
        'text':'政策原文：“符合原文条件的申请人可领取补助5000元。”个人是否符合领取条件还需要核验。',
        'claims':[{'text':'“符合原文条件的申请人可领取补助5000元。”','evidence_ids':[output['evidence_id']]}],
        'draft_ids':[],'followups':[]})


class RevisingPublicFixture(PublicFixture):
    def __init__(self):
        super().__init__()
        self.search_count = 0
    def search_public(self, request, release_id):
        self.search_count += 1
        if self.search_count == 2:
            self.hit.update(document_version_id='version-2',parse_id='parse-2',span_id='span-2',
                            excerpt='符合原文条件的申请人可领取补助10000元。')
            self.hit['locator']['end_char'] = len(self.hit['excerpt'])
        return super().search_public(request, release_id)


class SkillConversationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = FinanceGateway(Path(self.temp.name)/'bank.sqlite3')
        self.workspace, _ = self.gateway.create_session()
    def tearDown(self): self.temp.cleanup()

    def run_agent(self, *steps, reviewer=None):
        self.primary = ScriptedPrimary(*steps)
        self.reviewer = reviewer or Reviewer()
        agent = ConversationAgent(self.primary, self.reviewer)
        return agent.process(self.gateway, self.workspace, '请研究产品条件并说明证据缺口。')

    def assert_no_operation(self, result):
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(self.gateway.overview(self.workspace)['total_cash_minor'], 2439000)

    def test_new_research_skill_loads_in_the_main_loop_and_is_independently_reviewed(self):
        result = self.run_agent(call('load_skill', {'name':'product-research'}),
                                call('knowledge_status', {}), unavailable_answer)
        first_system = self.primary.messages[0][0]['content']
        self.assertIn('product-research', first_system)
        self.assertIn('asset-allocation', first_system)
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assertEqual(len(self.reviewer.packets), 1)
        trace = result['agent_runtime']['trace']
        self.assertEqual([row['tool'] for row in trace if row['status']=='completed'],
                         ['load_skill','knowledge_status'])
        self.assert_no_operation(result)

    def test_branch_reference_is_read_only_when_requested(self):
        reference = describe('asset-allocation')['references'][0]
        result = self.run_agent(call('load_skill', {'name':'asset-allocation'}),
                                call('load_skill', {'name':'asset-allocation','reference':reference}),
                                call('knowledge_status', {}), unavailable_answer)
        outputs = [last_tool(messages) for messages in self.primary.messages[1:]]
        self.assertIsNone(outputs[0]['data']['reference'])
        self.assertEqual(outputs[1]['data']['reference'], reference)
        self.assert_no_operation(result)

    def test_main_model_cannot_load_reviewer_even_with_a_fabricated_call(self):
        result = self.run_agent(call('load_skill', {'name':'reviewer'}),
                                call('load_skill', {'name':'financial-diagnosis'}),
                                call('knowledge_status', {}), unavailable_answer)
        self.assertIn('SKILL_FORBIDDEN', [row.get('code') for row in result['agent_runtime']['trace']])
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assert_no_operation(result)

    def test_unavailable_search_finishes_with_evidence_and_cannot_create_a_trade(self):
        result = self.run_agent(call('load_skill', {'name':'knowledge-retrieval'}),
                                call('knowledge_search', {'query':'产品的赎回条件','facets':['liquidity']}),
                                unavailable_answer)
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assertIn('尚未接入', result['reply'])
        self.assert_no_operation(result)

    def test_independent_reviewer_can_block_the_new_skill_answer(self):
        reviewer = Reviewer('reject')
        result = self.run_agent(call('load_skill', {'name':'product-research'}),
                                call('knowledge_status', {}), unavailable_answer, reviewer=reviewer)
        self.assertEqual(result['agent_review']['status'], 'blocked')
        self.assert_no_operation(result)

    def test_public_knowledge_money_has_public_units_and_requires_review(self):
        self.gateway.knowledge_provider = PublicFixture()
        result = self.run_agent(call('knowledge_search', {'query':'政策补助金额','as_of':'2026-10-05'}),
                                public_answer)
        self.assertEqual(result['agent_review']['status'], 'approved')
        money_evidence = self.reviewer.packets[0]['monetary_evidence']
        self.assertEqual([(item['value'], item['unit']) for item in money_evidence],
                         [(500000, 'public_minor_currency')])
        self.assertEqual(self.reviewer.packets[0]['evidence'][0]['tool'], 'knowledge_search')
        self.assert_no_operation(result)

    def test_public_knowledge_cannot_be_used_for_the_fact_only_fast_path(self):
        self.gateway.knowledge_provider = PublicFixture()
        def public_fact(messages, tools):
            return call('answer_facts', {'fact_ids':[last_tool(messages)['facts'][0]['id']]})
        result = self.run_agent(call('knowledge_search', {'query':'补助金额','as_of':'2026-10-05'}),
                                public_fact, public_answer)
        self.assertIn('REVIEW_REQUIRED', [row.get('code') for row in result['agent_runtime']['trace']])
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assert_no_operation(result)

    def test_withdrawal_during_review_prevents_displaying_the_old_quote(self):
        provider = PublicFixture(); self.gateway.knowledge_provider = provider
        reviewer = Reviewer(hook=lambda packet: setattr(provider, 'withdrawn', True))
        result = self.run_agent(call('knowledge_search', {'query':'补助金额','as_of':'2026-10-05'}),
                                public_answer, reviewer=reviewer)
        self.assertEqual(result['agent_review']['status'], 'blocked')
        self.assertNotIn('5,000.00元', result['reply'])
        self.assert_no_operation(result)

    def test_bank_tools_reject_revision_inside_search_without_registering_new_facts(self):
        self.gateway.knowledge_provider = RevisingPublicFixture()
        tools = BankTools(self.gateway, self.workspace, 'revision-test')
        args = {'query':'补助金额','as_of':'2026-10-05'}
        first = tools.invoke('knowledge_search', args)
        with self.assertRaises(BankError) as caught:
            tools.invoke('knowledge_search', args)
        self.assertEqual(caught.exception.code, 'STALE_KNOWLEDGE_EVIDENCE')
        self.assertEqual(list(tools.evidence), [first['evidence_id']])
        self.assertEqual([item['value'] for item in tools.facts.values()], [500000])
        with self.assertRaises(BankError) as caught:
            tools.fresh()
        self.assertEqual(caught.exception.code, 'STALE_KNOWLEDGE_EVIDENCE')
        self.assertFalse(tools.changed)
        self.assertEqual(tools.drafts, {})

    def test_bank_tools_reject_revision_during_multiple_resolves_before_registering_evidence(self):
        provider = PublicFixture()
        self.gateway.knowledge_provider = provider
        original_resolve = provider.resolve_public_evidence
        def resolve(citation_id, release_id, as_of):
            if citation_id == 'citation-2': provider.hit['document_version_id'] = 'version-2'
            row = original_resolve(citation_id, release_id, as_of)
            row['citation_id'] = citation_id
            return row
        provider.resolve_public_evidence = resolve
        provider.search_public = lambda request, release_id: {
            'schema_version':1,'release_id':release_id,
            'citation_ids':['citation-1','citation-2'],'coverage_gaps':[]}
        tools = BankTools(self.gateway, self.workspace, 'resolve-test')
        with self.assertRaises(BankError) as caught:
            tools.invoke('knowledge_search', {'query':'补助金额','as_of':'2026-10-05'})
        self.assertEqual(caught.exception.code, 'STALE_KNOWLEDGE_EVIDENCE')
        self.assertEqual(tools.evidence, {})
        self.assertEqual(tools.facts, {})
        self.assertFalse(tools.changed)

    def test_revision_inside_repeated_search_blocks_old_answer_even_if_reviewer_would_approve(self):
        self.gateway.knowledge_provider = RevisingPublicFixture()
        result = self.run_agent(call('knowledge_search', {'query':'补助金额','as_of':'2026-10-05'}),
                                call('knowledge_search', {'query':'复查补助','as_of':'2026-10-05'}),
                                public_answer, {'content':'这段原文仍可作为政策依据。'})
        self.assertEqual(result['agent_review']['status'], 'blocked')
        self.assertEqual(result['error']['code'], 'STALE_KNOWLEDGE_EVIDENCE')
        self.assertNotIn('5,000.00元', result['reply'])
        self.assertEqual(self.reviewer.packets, [])
        self.assert_no_operation(result)


if __name__ == '__main__': unittest.main()
