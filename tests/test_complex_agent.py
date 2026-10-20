import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.complex_agent import AIPingIntentJudge, ComplexConversationAgent, ComplexRoutingClient
from agent_runtime.conversation import validate_local_workflow_step, terminal_messages
from agent_runtime.jev_client import JevError
from agent_runtime.jev_client import JevClient
from agent_runtime.native_client import AIPingToolClient
from agent_runtime.model_adapters import ModelAdapterError, OpenAICompatibleJsonClient
from agent_runtime.conversation import GLMConversationReviewer
from finance_gateway import FinanceGateway
from simulated_bank import BankError


def tool(name, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-' + name,
        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class Judge:
    def __init__(self, error=None, malformed=False):
        self.states, self.error, self.malformed = [], error, malformed

    def evaluate(self, state, questions, *, deadline=None):
        self.states.append(state)
        if self.error: raise self.error
        answers = {key: {'value': 'no'} for key in questions}
        answers['cap_subscriptions'] = {'value': 'yes'}
        answers['fit_subscription_candidates_read'] = {'value': 'yes'}
        if self.malformed: answers.pop(next(iter(answers)))
        return answers, {'request_sent': True, 'outcome': 'completed',
                         'usage': {'input_tokens': 10, 'output_tokens': 1}}


class Primary(AIPingToolClient):
    billing_mode = 'unpaid_fixture'
    def __init__(self):
        super().__init__()
        self.messages = []

    def complete(self, messages, tools, **kwargs):
        self.messages.append(messages)
        outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        facts = [f['id'] for o in outputs for f in o.get('facts', [])]
        return (tool('answer_facts', {'fact_ids': facts}) if outputs else tool('account_summary', {})), {
            'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}


class NeverReviewer:
    billing_mode = 'unpaid_fixture'
    model_id = 'unused-reviewer'
    def review(self, *args, **kwargs): raise AssertionError('fact path must not add review')


class ComplexAgentTest(unittest.TestCase):
    def test_terminal_view_removes_only_registered_discovery_catalog(self):
        from agent_runtime.skills import catalog
        from simulated_bank.service import encoded
        from copy import deepcopy
        directory = [{k:item[k] for k in ('name','description')} for item in catalog()]
        method = '\n已加载方法：风险和退出条件须引用本轮证据。'
        messages = [{'role':'system','content':'可信规则\n专业技能目录：'+encoded(directory)+method},
            {'role':'user','content':'完整原题'},
            {'role':'tool','tool_call_id':'read-1','content':'完整金融证据与金额签名'}]
        before = deepcopy(messages)
        terminal = terminal_messages(messages)
        self.assertEqual(messages,before)
        self.assertNotIn('专业技能目录：',terminal[0]['content'])
        self.assertIn(method,terminal[0]['content'])
        self.assertEqual(terminal[1:-1],messages[1:])
        altered = [{'role':'system','content':'专业技能目录：'+encoded(directory)+'新增资料'}]
        # Arbitrary financial text stays intact; only the exact maintained list
        # is a discovery-only region, never a prefix-based policy rewrite.
        self.assertIn('新增资料',terminal_messages(altered)[0]['content'])

    def test_local_greeting_preserves_exact_contract_and_finalizer(self):
        from agent_runtime.social_reply import validate_social_reply_result
        class NoJudge:
            def evaluate(self, *args, **kwargs):
                raise AssertionError('local greeting must not run the router')
        with tempfile.TemporaryDirectory() as root:
            gateway = FinanceGateway(Path(root)/'bank.sqlite3')
            workspace, _ = gateway.create_session()
            for provider in ('off', 'native', 'jev'):
                finalized = []
                primary = Primary()
                def finalize(result, commit_history):
                    self.assertTrue(validate_social_reply_result(result))
                    finalized.append(result['reply'])
                    commit_history()
                    return result
                result = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(),
                    route_provider=provider, judge=NoJudge()).process(gateway, workspace, '你好',
                        finalize_result=finalize)
                self.assertTrue(validate_social_reply_result(result))
                self.assertEqual(finalized, [result['reply']])
                self.assertEqual(primary.messages, [])
            with gateway.bank.connect() as db:
                count = db.execute("SELECT count(*) FROM audit WHERE event='agent_intent_route_completed'").fetchone()[0]
            self.assertEqual(count, 0)

    def test_paid_channels_are_enforced_before_any_key_or_request(self):
        # Constructors contain no credential or network operations; default channels
        # must remain AiPing for DeepSeek/GLM and official TypeSafe for Jev.
        for provider in ('off', 'native', 'jev'):
            with self.subTest(provider=provider):
                agent = ComplexConversationAgent(route_provider=provider)
                self.assertEqual(agent.primary.endpoint, 'https://aiping.cn/api/v1/chat/completions')
                self.assertEqual(agent.reviewer.client.url, agent.primary.endpoint)
        primary = AIPingToolClient()
        primary.endpoint = 'https://api.deepseek.com/chat/completions'
        with self.assertRaisesRegex(ValueError, 'aiping_primary_route_required'):
            ComplexConversationAgent(primary=primary)
        judge = AIPingIntentJudge()
        judge.url = 'https://api.deepseek.com/chat/completions'
        with self.assertRaisesRegex(ValueError, 'aiping_intent_route_required'):
            ComplexConversationAgent(route_provider='native', judge=judge)
        jev = JevClient()
        jev.url = 'https://example.invalid/jev'
        with self.assertRaisesRegex(ValueError, 'official_jev_route_required'):
            ComplexConversationAgent(route_provider='jev', judge=jev)
        reviewer = GLMConversationReviewer()
        reviewer.client.model_id = 'other-model'
        with self.assertRaisesRegex(ValueError, 'aiping_reviewer_route_required'):
            ComplexConversationAgent(reviewer=reviewer)

    def test_paid_response_identity_rejection_keeps_native_usage(self):
        payload = {'model':'other-model', 'usage':{'prompt_tokens':8,'completion_tokens':2,'total_tokens':10},
            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{}'}}]}
        fake_open = lambda *args, **kwargs: Response(json.dumps(payload).encode())
        primary = AIPingToolClient(urlopen=fake_open, key_loader=lambda:'test-only')
        agent = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer())
        with self.assertRaises(ModelAdapterError) as caught:
            agent.primary.complete([], [], request_id='identity-test', remaining_seconds=10)
        self.assertEqual(caught.exception.code, 'provider_model_identity_mismatch')
        self.assertEqual(caught.exception.usage, payload['usage'])
        self.assertTrue(caught.exception.request_sent)
        client = OpenAICompatibleJsonClient(provider='ai_ping',model_id='GLM-5.3-Flash',
            url='https://aiping.cn/api/v1/chat/completions',env_name='AIPING_API_KEY',
            keychain_service='CC/AIPING_GLM_API_KEY',max_retries=0,
            urlopen=fake_open,key_loader=lambda:'test-only')
        reviewer = ComplexConversationAgent(primary=Primary(),reviewer=GLMConversationReviewer(client)).reviewer
        with self.assertRaises(ModelAdapterError) as caught:
            reviewer.client.complete_json(system='schema test',user='schema test',
                stage='reviewer.conversation',request_id='identity-test')
        self.assertEqual(caught.exception.code, 'provider_model_identity_mismatch')
        self.assertEqual(caught.exception.usage, payload['usage'])

    def test_native_discrete_protocol_and_true_usage(self):
        seen = []
        def opener(request, **kwargs):
            seen.append(request)
            return Response(json.dumps({'model': 'DeepSeek-V4-Flash', 'id': 'fixture-only',
                'usage': {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10},
                'choices': [{'finish_reason': 'tool_calls', 'message':
                    tool('submit_intent_route', {'cap': 'yes', 'gate': 'unknown'})}]}).encode())
        answers, receipt = AIPingIntentJudge(urlopen=opener, key_loader=lambda: 'test-only').evaluate(
            {'current_request': '查询'}, {'cap': {'type': 'noul'}, 'gate': {'type': 'noul'}},
            deadline=time.monotonic()+10)
        self.assertEqual(answers['gate'], {'value': 'unknown'})
        self.assertEqual(receipt['usage'], {'input_tokens': 8, 'output_tokens': 2})
        self.assertEqual(seen[0].full_url, 'https://aiping.cn/api/v1/chat/completions')
        self.assertNotIn('test-only', json.dumps(receipt))
        self.assertEqual(json.loads(seen[0].data)['thinking'], {'type': 'disabled'})

    def test_native_identity_and_unknown_usage_fail_closed(self):
        for data in ({'model': 'other', 'usage': {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}},
                     {'model': 'DeepSeek-V4-Flash', 'usage': None}):
            with self.subTest(data=data), self.assertRaises(JevError) as caught:
                AIPingIntentJudge(urlopen=lambda *a, **k: Response(json.dumps(data).encode()),
                    key_loader=lambda: 'test-only').evaluate({}, {'cap': {'type': 'noul'}},
                    deadline=time.monotonic()+10)
            self.assertTrue(caught.exception.receipt['request_sent'])
            self.assertEqual(caught.exception.receipt['usage'] is None, data['usage'] is None)

    def test_native_oversize_does_not_load_key_or_send(self):
        forbidden = lambda *a, **k: self.fail('preflight must precede key/network')
        with self.assertRaises(JevError) as caught:
            AIPingIntentJudge(urlopen=forbidden, key_loader=forbidden).evaluate('a'*16000,
                {'cap': {'type': 'noul'}}, deadline=time.monotonic()+10)
        self.assertFalse(caught.exception.receipt['request_sent'])

    def test_guard_after_key_load_can_revoke_before_http(self):
        denied = lambda: (_ for _ in ()).throw(BankError('AUTH_REVOKED', '已撤销'))
        forbidden = lambda *a, **k: self.fail('revoked request must never reach opener')
        for judge in (AIPingIntentJudge(urlopen=forbidden, key_loader=lambda: 'test-only'),
                      JevClient(urlopen=forbidden, key_loader=lambda: 'test-only')):
            with self.subTest(judge=type(judge).__name__), self.assertRaises(BankError):
                judge.evaluate({}, {'x': {'type': 'noul', 'instructions': '检查'}},
                    deadline=time.monotonic()+10,before_request=denied)

    def test_native_expiry_during_guard_is_not_sent_or_unknown_billing(self):
        forbidden=lambda *a, **k:self.fail('expired request must not reach network')
        with patch('agent_runtime.complex_agent.time.monotonic',side_effect=[0,0,11]):
            with self.assertRaises(JevError) as caught:
                AIPingIntentJudge(urlopen=forbidden,key_loader=lambda:'test-only').evaluate(
                    {},{'x':{'type':'noul'}},deadline=10,before_request=lambda:None)
        self.assertFalse(caught.exception.receipt['request_sent'])

    def test_paid_route_budget_rejects_before_key_or_http(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=FinanceGateway(Path(root)/'bank.sqlite3');workspace,_=gateway.create_session()
            primary=Primary();primary.billing_mode='paid'
            forbidden=lambda *a, **k:self.fail('budget admission must precede key/network')
            result=ComplexConversationAgent(primary=primary,reviewer=NeverReviewer(),route_provider='native',
                judge=AIPingIntentJudge(urlopen=forbidden,key_loader=forbidden),max_tokens=100).process(
                    gateway,workspace,'查询周期扣费候选')
            self.assertEqual(result['agent_runtime']['failure'],'AGENT_BUDGET_EXCEEDED')
            self.assertEqual(primary.messages,[])
            self.assertEqual(result['agent_runtime']['intent_route']['calls'],[])

    def test_deterministic_fact_query_does_not_pay_for_semantic_route(self):
        class NoJudge:
            def evaluate(self,*args,**kwargs):raise AssertionError('deterministic query must bypass router')
        with tempfile.TemporaryDirectory() as root:
            gateway=FinanceGateway(Path(root)/'bank.sqlite3');workspace,_=gateway.create_session()
            result=ComplexConversationAgent(primary=Primary(),reviewer=NeverReviewer(),route_provider='native',
                judge=NoJudge()).process(gateway,workspace,'查询余额')
            runtime=result['agent_runtime']
            self.assertIsNone(runtime['failure'])
            self.assertEqual(runtime['intent_route']['selection']['reason'],'deterministic_facts_only_query')
            self.assertEqual(runtime['intent_route']['calls'],[])

    def test_known_native_route_charge_blocks_later_paid_primary_and_reviewer(self):
        # The provider counters are synthetic boundary inputs, not a model test.
        # Real adapters, wire admission, temporary bank and both stop gates run.
        for spent_route, spent_primary, expected_primary_calls in (
                (90000, 0, 0), (100000, 0, 0), (100001, 0, 0),
                (11000, 88000, 1), (11000, 89000, 1)):
            with self.subTest(route=spent_route, primary=spent_primary), tempfile.TemporaryDirectory() as root:
                gateway=FinanceGateway(Path(root)/'bank.sqlite3')
                workspace,_=gateway.create_session()
                routing_requests=[]
                def route_response(request, **kwargs):
                    body=json.loads(request.data)
                    keys=body['tools'][0]['function']['parameters']['properties']
                    routing_requests.append(body)
                    return Response(json.dumps({'model':'DeepSeek-V4-Flash',
                        'usage':{'prompt_tokens':spent_route,'completion_tokens':0,'total_tokens':spent_route},
                        'choices':[{'finish_reason':'tool_calls','message':tool('submit_intent_route',
                            {key:'no' for key in keys})}]}).encode())
                class PaidPrimary(Primary):
                    billing_mode='paid'
                    def complete(self, messages, tools, **kwargs):
                        self.messages.append(messages)
                        return tool('answer_reviewed',{'text':'当前资料不足，无法给出结论。',
                            'claims':[],'draft_ids':[],'followups':[]}), {
                            'prompt_tokens':spent_primary,'completion_tokens':0,'total_tokens':spent_primary}
                forbidden=lambda *args, **kwargs:self.fail('GLM key or request must be rejected by the budget')
                review_client=OpenAICompatibleJsonClient(provider='ai_ping',model_id='GLM-5.3-Flash',
                    url='https://aiping.cn/api/v1/chat/completions',env_name='AIPING_API_KEY',
                    keychain_service='CC/AIPING_GLM_API_KEY',max_retries=0,
                    urlopen=forbidden,key_loader=forbidden,log_path=str(Path(root)/'metadata.jsonl'))
                primary=PaidPrimary()
                # Primary.__init__ assigns no instance billing_mode; the paid
                # subclass therefore exercises the real paid admission path.
                agent=ComplexConversationAgent(primary=primary,
                    reviewer=GLMConversationReviewer(client=review_client),route_provider='native',
                    judge=AIPingIntentJudge(urlopen=route_response,key_loader=lambda:'test-only'),
                    max_tokens=100000)
                result=agent.process(gateway,workspace,'请分析当前情况。')
                runtime=result['agent_runtime']
                self.assertEqual(len(routing_requests),1)
                self.assertEqual(len(primary.messages),expected_primary_calls)
                self.assertEqual(runtime['failure'],'AGENT_BUDGET_EXCEEDED')
                self.assertEqual(runtime['budget_tokens'],spent_route+spent_primary)
                self.assertEqual(runtime['total_tokens'],spent_route+spent_primary)
                self.assertEqual(runtime['budget_policy']['limit'],100000)
                self.assertEqual(agent.max_tokens,100000)
                self.assertFalse(any(call['stage']=='reviewer' for call in runtime['model_calls']))
                self.assertTrue(runtime['usage_complete'])

    def test_known_charge_is_accounted_once_for_native_and_jev_invalid_fallback(self):
        for provider in ('native','jev'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                gateway=FinanceGateway(Path(root)/'bank.sqlite3');workspace,_=gateway.create_session()
                class InvalidKnownJudge:
                    def evaluate(self, state, questions, *, deadline=None):
                        # Both providers can return paid invalid labels. They
                        # must still debit the charge before original fallback.
                        return {},{'request_sent':True,'outcome':'completed',
                            'usage':{'input_tokens':2000,'output_tokens':0}}
                primary=Primary()
                agent=ComplexConversationAgent(primary=primary,reviewer=NeverReviewer(),
                    route_provider=provider,judge=InvalidKnownJudge(),max_tokens=100000)
                result=agent.process(gateway,workspace,'请分析当前情况。')
                runtime=result['agent_runtime']
                self.assertEqual(runtime['intent_route']['selection']['reason'],'route_answers_invalid')
                self.assertIsNone(runtime['failure'])
                self.assertEqual(len(primary.messages),2)
                self.assertEqual(runtime['conversation_total_tokens'],20)
                self.assertEqual(runtime['total_tokens'],2020)
                self.assertEqual(runtime['budget_tokens'],2020)
                self.assertEqual(runtime['budget_policy']['limit'],100000)
                self.assertEqual(agent.max_tokens,100000)

    def test_two_round_budget_does_not_start_two_stage_reads(self):
        class BudgetJudge(Judge):
            def evaluate(self,state,questions,**kwargs):
                labels={k:{'value':'no'} for k in questions}
                for k in ('cap_financial_diagnosis','cap_goal_planning','fit_budget_and_cashflow_read'):
                    labels[k]={'value':'yes'}
                return labels,{'request_sent':True,'outcome':'completed',
                    'usage':{'input_tokens':10,'output_tokens':1}}
        with tempfile.TemporaryDirectory() as root:
            gateway=FinanceGateway(Path(root)/'bank.sqlite3');workspace,_=gateway.create_session()
            account=gateway.overview(workspace)['accounts'][0]['id']
            with patch('agent_runtime.workflow_registry.WorkflowRunner.local_workflow_step',
                       side_effect=AssertionError('two-stage workflow must reserve final round')):
                result=ComplexConversationAgent(primary=Primary(),reviewer=NeverReviewer(),route_provider='native',
                    judge=BudgetJudge(),max_rounds=2).process(gateway,workspace,
                    '查'+account+'可安排资金与未来30天现金流',page_context={'kind':'account','id':account})
            runtime=result['agent_runtime']
            self.assertEqual(runtime['intent_route']['selection']['status'],'workflow')
            self.assertEqual(runtime['intent_route']['local_workflow_steps'],[])

    def test_local_hook_cannot_introduce_write_or_hidden_tools(self):
        schemas = [{'function': {'name': 'prepare_operation'}}, {'function': {'name': 'account_summary'}}]
        for message in (tool('prepare_operation', {}), tool('budget', {}),
                        {**tool('account_summary', {}), 'content': '绕过审核'}):
            with self.subTest(message=message), self.assertRaises(BankError):
                validate_local_workflow_step((message, {}), schemas, 12)
        self.assertEqual(validate_local_workflow_step((tool('account_summary', {}), {}), schemas, 12),
                         tool('account_summary', {}))

    def test_invalid_paid_answers_keep_charge_and_original_planner(self):
        primary, judge, debits = Primary(), Judge(malformed=True), []
        client = ComplexRoutingClient(primary, judge, provider='native', request_id='request', debit=debits.append)
        client.bind_turn_guards(before_request=lambda: None,admit_route=lambda *args: None)
        messages = [{'role': 'user', 'content': json.dumps({'current_request': '查询周期扣费候选',
                      'server_context': {'unrelated_private': 'DO_NOT_FORWARD'}})}]
        client.prepare_turn(messages, deadline=time.monotonic()+10)
        self.assertEqual(debits, [11])
        self.assertEqual(client.selection['reason'], 'route_answers_invalid')
        self.assertIsNone(client.runner)
        self.assertNotIn('DO_NOT_FORWARD', json.dumps(judge.states))
        self.assertEqual(json.loads(messages[0]['content'])['server_context']['unrelated_private'], 'DO_NOT_FORWARD')

    def test_freshness_guard_blocks_route_and_local_execution(self):
        primary, judge = Primary(), Judge()
        def denied(): raise BankError('AUTH_REVOKED', '已撤销')
        primary._before_request = denied
        client = ComplexRoutingClient(primary, judge, provider='native', request_id='x', debit=lambda _: None)
        client.bind_turn_guards(before_request=lambda: None,admit_route=lambda *args: None)
        with self.assertRaises(BankError):
            client.prepare_turn([{'role': 'user', 'content': json.dumps({'current_request': '查询周期扣费候选'})}],
                                deadline=time.monotonic()+10)
        self.assertEqual(judge.states, [])
        client.runner = object()
        with self.assertRaises(BankError): client.local_workflow_step([], [], 1, 12)

    def test_unknown_paid_route_stops_primary_and_audit_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = FinanceGateway(Path(root)/'bank.sqlite3')
            workspace, _ = gateway.create_session()
            primary = Primary()
            result = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(), route_provider='native',
                judge=Judge(error=JevError('timeout', {'request_sent': True, 'usage': None}))).process(
                    gateway, workspace, '查询周期扣费候选')
            self.assertEqual(primary.messages, [])
            self.assertEqual(result['agent_runtime']['failure'], 'MODEL_USAGE_UNVERIFIED')
            self.assertIsNone(result['agent_runtime']['total_tokens'])
            with gateway.bank.connect() as db:
                audit = json.loads(db.execute("SELECT details FROM audit WHERE event='agent_intent_route_completed'").fetchone()[0])
            self.assertFalse(audit['usage_complete'])

    def test_registered_reads_use_real_bank_boundary_and_skip_primary_reads(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = FinanceGateway(Path(root)/'bank.sqlite3')
            workspace, _ = gateway.create_session()
            primary, judge, published = Primary(), Judge(), []
            before = gateway.overview(workspace)
            def finalize(result, commit_history):
                published.append(result['agent_runtime']['total_tokens'])
                commit_history()
                return result
            result = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(), route_provider='native',
                judge=judge).process(gateway, workspace, '查询周期扣费候选', finalize_result=finalize)
            runtime = result['agent_runtime']
            self.assertIsNone(runtime['failure'])
            self.assertEqual(len(primary.messages), 1)
            self.assertEqual(runtime['total_tokens'], 21)
            self.assertEqual(published, [21])
            self.assertEqual(runtime['intent_route']['selection']['status'], 'workflow')
            self.assertTrue(runtime['intent_route']['local_workflow_steps'])
            self.assertEqual([item['tool'] for item in runtime['trace']],
                             ['statement_source', 'periodic_charges'])
            self.assertEqual(runtime['tool_calls'],3)
            with gateway.bank.connect() as db:
                audits=db.execute("SELECT event,details FROM audit WHERE event IN ('agent_turn_completed','agent_intent_route_completed')").fetchall()
            self.assertEqual(len(audits),2)
            self.assertTrue(all(json.loads(row['details'])['total_tokens']==21 for row in audits))
            self.assertEqual(gateway.overview(workspace)['accounts'], before['accounts'])
            self.assertNotIn(workspace, json.dumps(judge.states))


if __name__ == '__main__': unittest.main()
