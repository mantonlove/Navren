"""消息组件在主循环的本地接线，不新增模型请求或审核后数值。"""
import tempfile
import unittest
from pathlib import Path

from agent_runtime.conversation import ConversationAgent
from finance_gateway import FinanceGateway
from simulated_bank import BankError
from test_native_conversation import ScriptedPrimary, Reviewer, call, fact_reply, last_tool


class ReplyPresentationAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.g=FinanceGateway(Path(self.tmp.name)/'bank.sqlite3')
        self.w,_=self.g.create_session()
    def tearDown(self):
        self.tmp.cleanup()

    def test_selected_verified_facts_bind_the_same_local_view_and_text(self):
        primary=ScriptedPrimary(call('account_summary',{}),fact_reply)
        result=ConversationAgent(primary,Reviewer()).process(self.g,self.w,'查询账户余额')
        view=result['reply_presentation']
        self.assertEqual(view['schema_version'],'navren.reply-presentation.v1')
        self.assertEqual(view['fact_ids'],result['answer_contract']['fact_ids'])
        self.assertEqual(view['workspace_id'],self.w)
        self.assertEqual(view['response_id'],result['bank_response_id'])
        shown={v['fact_id'] for b in view['blocks'] for k in ('rows','metrics') for v in b[k]}
        self.assertEqual(shown,set(result['answer_contract']['fact_ids']))
        self.assertEqual(result['reply'],view['text'])
        self.assertEqual(result['agent_runtime']['review_calls'],0)
        self.assertEqual(len(primary.messages),2)
        self.assertEqual(result['bank_actions'],[])

    def test_reviewed_text_is_unchanged_and_has_no_new_amount_component(self):
        text='账户资料已经读取。本轮未执行操作。'
        def answer(messages,schemas):
            e=last_tool(messages)['evidence_id']
            return call('answer_reviewed',{'text':text,'claims':[{'text':'账户资料来自本轮查询。','evidence_ids':[e]}],
                'draft_ids':[],'followups':[]})
        result=ConversationAgent(ScriptedPrimary(call('account_summary',{}),answer),Reviewer()).process(
            self.g,self.w,'解释账户资料的来源')
        self.assertEqual(result['reply'],text)
        self.assertIsNone(result['reply_presentation'])

    def test_failure_response_clears_presentation(self):
        result=ConversationAgent(ScriptedPrimary(BankError('AGENT_BUDGET_EXCEEDED','fixture')),Reviewer()).process(
            self.g,self.w,'查询账户余额')
        self.assertTrue(result['isFallback'])
        self.assertIsNone(result['reply_presentation'])


if __name__=='__main__':unittest.main()
