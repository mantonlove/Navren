"""Agent总预算与实际回执；供应商夹具只证明协议，另有真实语义验收。"""
import io
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agent_runtime.conversation import ConversationAgent, GLMConversationReviewer
from agent_runtime.model_adapters import OpenAICompatibleJsonClient, ModelAdapterError
from finance_gateway import FinanceGateway
from test_native_conversation import ScriptedPrimary, Reviewer, call
from agent_runtime.metric_claims import REF_FIELDS
from financial_methods.contracts import MetricContext, money_metric
from simulated_bank import BankError
from agent_runtime.bank_tools import BankTools
from test_rag_integration import publish_fixture, answer as public_answer


class SyntheticMetricService:
    """合成可信服务合同；不冒称正式基座指标接线已完成。"""
    def __init__(self): self.version='synthetic-1'
    def metrics_for_tool(self, workspace, name, arguments, data):
        if name!='account_summary': return None
        account=next(a for a in data['accounts'] if a['id'].endswith('-main'))
        context=MetricContext(account['id'],account['currency'],'synthetic_account',as_of=data['as_of'],
            input_refs=('synthetic-account-source',),source_versions=(('synthetic-bank',self.version),))
        metric=money_metric(context,'settled_cash','settled_account_cash','fixture.cash/1',account['balance_minor']).to_dict()
        return {'workspace_id':workspace,'source_versions':{'synthetic-bank':self.version},
            'metrics':[metric],'metric_kinds':{metric['metric_id']:'recorded_fact'}}
    def validate(self, workspace, batch):
        if batch['workspace_id']!=workspace or batch['source_versions']['synthetic-bank']!=self.version:
            raise BankError('METRIC_VERSION_CONFLICT','合成来源已更新。',409)


def metric_answer(messages, schemas, *, wrong_subject=False):
    output=next(json.loads(m['content']) for m in reversed(messages) if m['role']=='tool'
        and json.loads(m['content']).get('metrics'))
    record=output['metrics'][0]
    text='日常账户已结算现金为22,890.00元。'
    claim={**{key:record['metric'][key] for key in REF_FIELDS},'claim_kind':record['kind'],'text':text}
    if wrong_subject:claim['subject_id']='wrong-synthetic-account'
    return call('answer_reviewed',{'text':text,'claims':[{'text':text,'evidence_ids':[output['evidence_id']]}],
        'amount_claims':[claim],'draft_ids':[],'followups':[]})


def compact_metric_answer(messages, schemas, *, wrong_subject=False):
    output=next(json.loads(m['content']) for m in reversed(messages) if m['role']=='tool'
        and json.loads(m['content']).get('metrics'))
    record=output['metrics'][0];metric=record['metric']
    ref=metric['source_version_ref']
    assert 'source_versions' not in metric and ref in output['source_version_sets']
    value=metric['value_minor'];amount=f'{value//100:,}.{value%100:02d}'
    text=f'本次账户记录的现金为{amount}元。'
    claim={**{key:metric[key] for key in REF_FIELDS if key!='source_versions'},
        'source_version_ref':ref,'claim_kind':record['kind'],'text':text}
    if wrong_subject:claim['subject_id']='wrong-synthetic-subject'
    return call('answer_reviewed',{'text':text,'claims':[{'text':text,'evidence_ids':[output['evidence_id']]}],
        'amount_claims':[claim],'draft_ids':[],'followups':[]})


class MeteredReviewer(Reviewer):
    def review(self, packet, *args):
        return {**super().review(packet, *args), '_model_usage':{'prompt_tokens':5,'completion_tokens':2,'total_tokens':7}}


class PaidScriptedPrimary(ScriptedPrimary):
    """Zero-network local fixture exercising the production paid policy."""
    billing_mode='paid'
    def request_input_bound(self,messages,tools):
        return len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False).encode())


class PaidReviewer(MeteredReviewer):
    billing_mode='paid'
    def request_input_bound(self,packet):return len(json.dumps(packet,ensure_ascii=False).encode())
    def review(self,packet,*args,**kwargs):return super().review(packet,*args)


class AgentWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.g=FinanceGateway(Path(self.tmp.name)/'bank.sqlite3')
        self.w,_=self.g.create_session()
    def tearDown(self): self.tmp.cleanup()

    def public_wire_clients(self, *steps, first_usage=7, after_first=None):
        """Actual paid-policy serializers; urlopen is a zero-network local fixture."""
        from agent_runtime.native_client import DeepSeekToolClient
        sent=[]
        steps=list(steps)
        class Response(io.BytesIO):status=200
        def primary_open(request,**kwargs):
            body=json.loads(request.data)
            index=sum(row['stage']=='primary' for row in sent)
            sent.append({'stage':'primary','input_bytes':len(request.data),
                         'wire_sha256':hashlib.sha256(request.data).hexdigest(),'body':body})
            step=steps.pop(0)
            message=step(body['messages'],body['tools']) if callable(step) else step
            total=first_usage if index==0 else 7
            if index==0 and after_first:after_first()
            return Response(json.dumps({'choices':[{'finish_reason':'stop','message':message}],
                'usage':{'prompt_tokens':total-2,'completion_tokens':2,'total_tokens':total}},ensure_ascii=False).encode())
        def review_open(request,**kwargs):
            sent.append({'stage':'reviewer','input_bytes':len(request.data),
                         'wire_sha256':hashlib.sha256(request.data).hexdigest(),'body':json.loads(request.data)})
            return Response(json.dumps({'choices':[{'finish_reason':'stop','message':{
                'content':json.dumps({'verdict':'approve','reason':'离线协议核验','issues':[]})}}],
                'usage':{'prompt_tokens':11,'completion_tokens':2,'total_tokens':13}}).encode())
        primary=DeepSeekToolClient(urlopen=primary_open,key_loader=lambda:'offline-local-fixture',
            log_path=Path(self.tmp.name)/'public-native-wire.jsonl')
        reviewer=GLMConversationReviewer(OpenAICompatibleJsonClient(provider='ai_ping',
            model_id='GLM-5.3-Flash',url='https://offline.invalid',env_name='OFFLINE_FIXTURE',
            keychain_service='OFFLINE_FIXTURE',urlopen=review_open,key_loader=lambda:'offline-local-fixture',
            log_path=Path(self.tmp.name)/'public-review-wire.jsonl'))
        return primary,reviewer,sent

    @staticmethod
    def public_catalog_and_status():
        message=call('public_products',{'query':'TEST-P'})
        message['tool_calls']+=call('knowledge_status',{})['tool_calls']
        return message

    def test_public_view_actual_native_glm_three_step_complete_packet(self):
        from agent_runtime.conversation import PUBLIC_VIEW_TOOLS
        publish_fixture(self.g)
        primary,reviewer,sent=self.public_wire_clients(self.public_catalog_and_status(),
            call('knowledge_search',{'query':'风险','product_id':'TEST-P','facets':['risk']}),public_answer)
        before=self.g.overview(self.w)['total_cash_minor']
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'研究测试产品风险，核对公开证据，不执行。')
        self.assertFalse(result.get('isFallback'),(result.get('error'),result['agent_runtime']['budget_admissions']))
        self.assertEqual([r['stage'] for r in sent],['primary','primary','primary','reviewer'])
        admissions=result['agent_runtime']['budget_admissions']
        second=admissions[1]
        self.assertEqual(second['stage'],'explore')
        self.assertTrue(second['exploration_preflight']['terminal_reasons'])
        self.assertEqual(second['tool_view']['selected'],'public')
        self.assertTrue(second['tool_view']['public_admitted'])
        self.assertGreater(second['tool_view']['full_explore_input_bytes'],second['tool_view']['public_input_bytes'])
        self.assertEqual({t['function']['name'] for t in sent[1]['body']['tools']},PUBLIC_VIEW_TOOLS)
        self.assertEqual(len(sent[1]['body']['tools']),11)
        self.assertIn('load_skill',sent[1]['body']['messages'][-1]['content'])
        first_outputs=[m for m in sent[1]['body']['messages'] if m['role']=='tool']
        last_outputs=[m for m in sent[2]['body']['messages'] if m['role']=='tool']
        self.assertEqual(last_outputs[:len(first_outputs)],first_outputs)
        packet=json.loads(sent[-1]['body']['messages'][-1]['content'])
        self.assertEqual([e['tool'] for e in packet['evidence']],['public_products','knowledge_status','knowledge_search'])
        self.assertTrue(packet['evidence'][-1]['data']['hits'])
        self.assertEqual([a['input_bytes'] for a in admissions],[r['input_bytes'] for r in sent])
        self.assertEqual(sent[0]['body']['max_tokens'],4000)
        self.assertEqual(sent[-1]['body']['max_tokens'],1400)
        self.assertLessEqual(admissions[-1]['input_bytes'],admissions[-2]['review_input_cap'])
        self.assertTrue(all(a['accepted'] for a in admissions))
        self.assertLessEqual(result['agent_runtime']['budget_tokens'],100000)
        self.assertEqual(result['bank_actions'],[])
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)
        self.public_tool_view_evidence={'admissions':admissions,'review_packet':packet,
            'sent':[{'stage':r['stage'],'input_bytes':r['input_bytes'],'wire_sha256':r['wire_sha256'],
                     'tool_names':[t['function']['name'] for t in r['body'].get('tools',[])],
                     'output_limit':r['body']['max_tokens']} for r in sent],
            'runtime':result['agent_runtime'],'bank_actions':result['bank_actions'],
            'complete_tool_outputs_preserved':True,'financial_unchanged':True}

    def test_public_read_with_insufficient_full_budget_uses_public_view(self):
        publish_fixture(self.g)
        primary,reviewer,sent=self.public_wire_clients(call('knowledge_status',{}),
            call('answer_reviewed',{'text':'公开资料状态已核对，产品条款还需另行读取。',
                 'claims':[],'draft_ids':[],'followups':[]}),first_usage=7)
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查看公开资料接入状态')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        second=result['agent_runtime']['budget_admissions'][1]
        preflight=second['exploration_preflight']
        self.assertIn('full_review_reserve',preflight['terminal_reasons'])
        self.assertEqual(preflight['minimum_review_input']-preflight['review_input_cap'],134)
        self.assertEqual(second['tool_view']['selected'],'public')
        self.assertGreaterEqual(second['tool_view']['public_review_input_cap'],preflight['minimum_review_input'])
        self.assertIn('full_review_reserve',second['tool_view']['switch_reasons'])
        self.assertEqual(len(sent[1]['body']['tools']),second['tool_view']['public_tool_count'])

    def test_public_read_with_sufficient_full_budget_keeps_full_view(self):
        publish_fixture(self.g)
        primary,reviewer,sent=self.public_wire_clients(call('knowledge_status',{}),
            call('answer_reviewed',{'text':'公开资料状态已核对，产品条款还需另行读取。',
                 'claims':[],'draft_ids':[],'followups':[]}),first_usage=7)
        # Test-only headroom exercises the full-view side of the threshold;
        # the production/default 100,000 budget remains unchanged.
        result=ConversationAgent(primary,reviewer,max_tokens=100512).process(
            self.g,self.w,'查看公开资料接入状态')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        second=result['agent_runtime']['budget_admissions'][1]
        preflight=second['exploration_preflight']
        self.assertGreaterEqual(preflight['review_input_cap'],preflight['minimum_review_input'])
        self.assertEqual(preflight['terminal_reasons'],[])
        self.assertEqual(second['tool_view']['selected'],'full')
        self.assertIsNone(second['tool_view']['public_input_bytes'])
        self.assertEqual(len(sent[1]['body']['tools']),len(sent[0]['body']['tools']))
        self.assertIn('account_summary',{t['function']['name'] for t in sent[1]['body']['tools']})

    def test_mixed_personal_read_never_uses_public_view(self):
        publish_fixture(self.g)
        first=call('knowledge_status',{})
        first['tool_calls']+=call('account_summary',{})['tool_calls']
        primary,reviewer,sent=self.public_wire_clients(first,
            call('knowledge_search',{'query':'风险','product_id':'TEST-P'}))
        with patch.object(self.g.knowledge_provider,'search_public',wraps=self.g.knowledge_provider.search_public) as search:
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'结合当前账户研究产品风险，不执行。')
        second=result['agent_runtime']['budget_admissions'][1]
        self.assertFalse(second['tool_view']['public_eligible'])
        self.assertEqual(second['tool_view']['selected'],'full')
        self.assertIsNone(second['tool_view']['public_input_bytes'])
        self.assertEqual(result['error']['code'],'INVALID_FINAL_CONTRACT')
        self.assertEqual(search.call_count,0)
        self.assertEqual([r['stage'] for r in sent],['primary','primary'])
        self.assertEqual(result['bank_actions'],[])

    def test_public_view_nonpublic_skill_requests_full_expansion(self):
        publish_fixture(self.g)
        primary,reviewer,sent=self.public_wire_clients(self.public_catalog_and_status(),
            call('load_skill',{'name':'profile'}),call('answer_reviewed',{
                'text':'个人图谱分析仍需读取本人资料；本轮只核对了公开资料范围。',
                'claims':[],'draft_ids':[],'followups':[]}))
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'先查公开资料，然后分析本人图谱，不执行。')
        admissions=result['agent_runtime']['budget_admissions']
        self.assertEqual(admissions[1]['tool_view']['selected'],'public')
        third=admissions[2]
        self.assertEqual(third['tool_view']['expansion_skill'],'profile')
        self.assertFalse(third['tool_view']['public_eligible'])
        self.assertIsNone(third['tool_view']['public_input_bytes'])
        self.assertEqual(third['tool_view']['full_explore_tool_count'],len(sent[0]['body']['tools']))
        # Expanding descriptions cannot bypass the same token budget: the
        # restored full exploration may have to enter the original final phase.
        self.assertEqual(third['stage'],'final')
        self.assertEqual({t['function']['name'] for t in sent[2]['body']['tools']},{'answer_facts','answer_reviewed'})
        self.assertEqual(result['bank_actions'],[])

    def test_public_success_cannot_reopen_last_round_or_time_final_phase(self):
        import time
        publish_fixture(self.g)
        real_monotonic=time.monotonic
        for reason in ('last_round','time_reserve'):
            with self.subTest(reason=reason):
                offset={'value':0}
                def after_first():
                    if reason=='time_reserve':offset['value']=65
                primary,reviewer,sent=self.public_wire_clients(self.public_catalog_and_status(),
                    call('knowledge_search',{'query':'风险','product_id':'TEST-P'}),after_first=after_first)
                with patch('agent_runtime.conversation.time.monotonic',side_effect=lambda:real_monotonic()+offset['value']), \
                     patch.object(self.g.knowledge_provider,'search_public',wraps=self.g.knowledge_provider.search_public) as search:
                    result=ConversationAgent(primary,reviewer,max_rounds=2 if reason=='last_round' else 8).process(
                        self.g,self.w,'查公开产品风险，不执行。')
                second=result['agent_runtime']['budget_admissions'][1]
                self.assertIn(reason,second['exploration_preflight']['terminal_reasons'])
                self.assertIsNone(second['tool_view']['public_input_bytes'])
                self.assertEqual(second['stage'],'final')
                self.assertEqual(result['error']['code'],'INVALID_FINAL_CONTRACT')
                self.assertEqual(search.call_count,0)
                self.assertEqual([r['stage'] for r in sent],['primary','primary'])
                self.assertEqual(result['bank_actions'],[])

    def test_public_terminal_attempt_never_reopens_narrow_exploration(self):
        publish_fixture(self.g)
        primary,reviewer,sent=self.public_wire_clients(self.public_catalog_and_status(),
            call('answer_reviewed',{'text':'已准备操作草案，等待确认。',
                 'claims':[],'draft_ids':[],'followups':[]}),
            call('knowledge_search',{'query':'风险','product_id':'TEST-P'}))
        with patch.object(self.g.knowledge_provider,'search_public',wraps=self.g.knowledge_provider.search_public) as search:
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'只研究公开产品风险，不执行。')
        admissions=result['agent_runtime']['budget_admissions']
        self.assertEqual(admissions[1]['tool_view']['actual_view'],'public')
        self.assertEqual(admissions[2]['tool_view']['actual_view'],'final')
        self.assertFalse(admissions[2]['tool_view']['public_eligible'])
        self.assertIsNone(admissions[2]['tool_view']['public_input_bytes'])
        self.assertEqual(result['error']['code'],'INVALID_FINAL_CONTRACT')
        self.assertEqual(search.call_count,0)
        self.assertEqual([r['stage'] for r in sent],['primary','primary','primary'])
        self.assertEqual(result['bank_actions'],[])

    def test_public_candidate_serialization_rechecks_original_time_reserve(self):
        import time
        publish_fixture(self.g)
        primary,reviewer,sent=self.public_wire_clients(self.public_catalog_and_status(),
            call('knowledge_search',{'query':'风险','product_id':'TEST-P'}))
        real_monotonic=time.monotonic
        original_bound=primary.request_input_bound
        offset={'value':0}
        def bound(messages,schemas):
            if len(schemas)==11:offset['value']=65
            return original_bound(messages,schemas)
        with patch.object(primary,'request_input_bound',side_effect=bound), \
             patch('agent_runtime.conversation.time.monotonic',side_effect=lambda:real_monotonic()+offset['value']), \
             patch.object(self.g.knowledge_provider,'search_public',wraps=self.g.knowledge_provider.search_public) as search:
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'核对公开产品风险，不执行。')
        second=result['agent_runtime']['budget_admissions'][1]
        self.assertNotIn('time_reserve',second['exploration_preflight']['terminal_reasons'])
        self.assertIn('time_reserve',second['tool_view']['switch_reasons'])
        self.assertLessEqual(second['tool_view']['public_remaining_seconds'],42)
        self.assertFalse(second['tool_view']['public_admitted'])
        self.assertEqual(second['tool_view']['actual_view'],'final')
        self.assertEqual(result['error']['code'],'INVALID_FINAL_CONTRACT')
        self.assertEqual(search.call_count,0)
        self.assertEqual(len(sent),2)

    def test_compact_actual_base_amount_keeps_full_reviewable_versions(self):
        primary=ScriptedPrimary(call('account_summary',{}),compact_metric_answer)
        primary.supports_compact_metric_sources=True
        reviewer=MeteredReviewer()
        before=self.g.overview(self.w)['total_cash_minor']
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'解释本次账户现金，不执行。')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        self.assertEqual(len(result['answer_contract']['metric_ids']),1)
        self.assertEqual(result['bank_actions'],[])
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)
        packet=reviewer.packets[0];catalog=packet['source_version_sets']
        found=[]
        def walk(value):
            if isinstance(value,dict):
                if 'source_version_ref' in value:
                    self.assertIn(value['source_version_ref'],catalog)
                    self.assertNotIn('source_versions',value)
                    self.assertTrue(catalog[value['source_version_ref']])
                    found.append(value['source_version_ref'])
                for key,item in value.items():
                    if key!='source_version_sets':walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(packet)
        self.assertTrue(found)

    def test_compact_forged_signature_is_rejected_before_review(self):
        for field,value in (('subject_id','wrong-synthetic-subject'),('value_minor',1),
                            ('as_of','2026-10-04'),('basis','user_input_scenario'),
                            ('claim_kind','hypothesis')):
            with self.subTest(field=field):
                def forged(m,t):
                    message=compact_metric_answer(m,t)
                    arguments=json.loads(message['tool_calls'][0]['function']['arguments'])
                    arguments['amount_claims'][0][field]=value
                    message['tool_calls'][0]['function']['arguments']=json.dumps(arguments,ensure_ascii=False)
                    return message
                primary=ScriptedPrimary(call('account_summary',{}),forged)
                primary.supports_compact_metric_sources=True
                reviewer=MeteredReviewer()
                result=ConversationAgent(primary,reviewer).process(self.g,self.w,'解释账户现金，不执行。')
                self.assertTrue(result['isFallback'])
                self.assertEqual(result['error']['code'],'BOUNDARY_UNKNOWN_ALIAS')
                self.assertEqual(reviewer.packets,[])
                self.assertEqual(result['bank_actions'],[])

    def test_compact_source_change_after_review_still_rejects_original_amount(self):
        service=SyntheticMetricService();self.g.financial_metrics=service
        primary=ScriptedPrimary(call('account_summary',{}),compact_metric_answer)
        primary.supports_compact_metric_sources=True
        reviewer=MeteredReviewer(hook=lambda packet:setattr(service,'version','synthetic-2'))
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'解释账户现金，不执行。')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'METRIC_VERSION_CONFLICT')
        self.assertEqual(result['bank_actions'],[])
        self.assertNotIn('22,890',result['reply'])

    def test_rejected_private_query_can_recover_without_provider_disclosure(self):
        publish_fixture(self.g)
        rejected='我的账户余额100万元，测试产品风险'
        primary=ScriptedPrimary(call('knowledge_search',{'query':rejected}),
            call('knowledge_search',{'query':'风险','product_id':'TEST-P','facets':['risk']}),public_answer)
        reviewer=MeteredReviewer()
        provider=self.g.knowledge_provider
        with patch.object(provider,'search_public',wraps=provider.search_public) as search:
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查测试产品风险')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        self.assertEqual(search.call_count,1)
        self.assertEqual(search.call_args.args[0]['query'],'风险')
        self.assertNotIn(rejected,json.dumps(search.call_args.args,ensure_ascii=False))
        recovery=json.loads(next(m['content'] for m in primary.messages[1] if m['role']=='tool'))
        self.assertEqual(recovery['error']['code'],'BOUNDARY_PUBLIC_QUERY')
        self.assertNotIn(rejected,json.dumps(recovery,ensure_ascii=False))
        self.assertIn('TEST-P',recovery['public_retrieval']['public_identifiers'])
        self.assertEqual(len(reviewer.packets),1)
        self.assertEqual(result['bank_actions'],[])

    def test_public_query_recovery_is_finite_and_never_dispatches_rejected_args(self):
        publish_fixture(self.g)
        rejected=call('public_products',{'query':'我有100万元现金'})
        primary=ScriptedPrimary(rejected,rejected,rejected)
        reviewer=MeteredReviewer()
        with patch.object(self.g.public_products,'read',wraps=self.g.public_products.read) as read:
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查产品')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'BOUNDARY_PUBLIC_QUERY')
        self.assertEqual(len(primary.messages),3)
        self.assertEqual(read.call_count,0)
        self.assertEqual(reviewer.packets,[])

    def test_rejected_public_query_still_consumes_tool_budget(self):
        publish_fixture(self.g)
        primary=ScriptedPrimary(call('knowledge_search',{'query':'我的账户余额'}),
            call('knowledge_search',{'query':'风险','product_id':'TEST-P'}))
        with patch.object(self.g.knowledge_provider,'search_public',wraps=self.g.knowledge_provider.search_public) as search:
            result=ConversationAgent(primary,MeteredReviewer(),max_tools=1).process(self.g,self.w,'查风险')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(search.call_count,0)

    def test_service_privacy_arguments_do_not_gain_public_query_recovery(self):
        primary=ScriptedPrimary(call('service_offer_preview',{'share_memory':True}))
        reviewer=MeteredReviewer()
        with patch.object(self.g.service_negotiations,'preview',wraps=self.g.service_negotiations.preview) as preview:
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'协商减免费用')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'BOUNDARY_INVALID_INPUT')
        self.assertEqual(preview.call_count,0)
        self.assertEqual(reviewer.packets,[])

    def test_invalid_budget_cannot_create_an_unbounded_agent(self):
        for options in ({'timeout_seconds':float('nan')},{'timeout_seconds':float('inf')},
                        {'timeout_seconds':0},{'max_tokens':0},{'max_rounds':True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ConversationAgent(ScriptedPrimary(),Reviewer(),**options)

    def test_two_models_usage_is_counted_and_no_billing_is_invented(self):
        primary=ScriptedPrimary(call('answer_reviewed',{'text':'还需要核实你的资金使用期限。',
            'claims':[], 'draft_ids':[], 'followups':[]}))
        result=ConversationAgent(primary,MeteredReviewer()).process(self.g,self.w,'帮我规划')
        runtime=result['agent_runtime']
        self.assertFalse(result.get('isFallback'))
        self.assertEqual(runtime['total_tokens'],8)
        self.assertEqual(runtime['reviewer_tokens'],7)
        self.assertEqual(runtime['budget_tokens'],8)
        self.assertTrue(runtime['usage_complete'])
        self.assertIsNone(runtime['cost']['amount'])
        self.assertEqual([c['stage'] for c in runtime['model_calls']],['primary','reviewer'])

    def test_paid_reviewer_without_usage_stops_instead_of_releasing(self):
        class UnmeteredReviewer(PaidReviewer):
            def review(self,packet,*args,**kwargs):
                return Reviewer.review(self,packet,*args)
        primary=ScriptedPrimary(call('answer_reviewed',{'text':'需要核实期限。','claims':[],
            'draft_ids':[], 'followups':[]}))
        reviewer=UnmeteredReviewer()
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'规划')
        runtime=result['agent_runtime']
        self.assertIsNone(runtime['total_tokens'])
        self.assertIsNone(runtime['reviewer_tokens'])
        self.assertFalse(runtime['usage_complete'])
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'MODEL_USAGE_UNVERIFIED')
        self.assertEqual(runtime['budget_tokens'],100000)
        self.assertEqual(runtime['known_tokens_lower_bound'],1)
        self.assertEqual(runtime['model_calls'][1]['budget_source'],'unknown_usage_stop')
        self.assertEqual(len(reviewer.packets),1)

    def test_missing_primary_usage_is_unknown_instead_of_zero(self):
        class UnmeteredPrimary(PaidScriptedPrimary):
            def complete(self, *args, **kwargs):
                message, _ = super().complete(*args, **kwargs)
                return message, {}
        primary=UnmeteredPrimary({'role':'assistant','content':'需要核实期限。'})
        reviewer=MeteredReviewer()
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'规划')
        runtime=result['agent_runtime']
        self.assertTrue(result.get('isFallback'))
        self.assertEqual(result['error']['code'],'MODEL_USAGE_UNVERIFIED')
        self.assertIsNone(runtime['primary_tokens'])
        self.assertIsNone(runtime['total_tokens'])
        self.assertEqual(runtime['primary_tokens_known'],0)
        self.assertFalse(runtime['usage_complete'])
        self.assertEqual(runtime['budget_tokens'],100000)
        self.assertEqual(reviewer.packets,[])

    def test_failed_primary_provider_usage_is_counted_and_review_not_requested(self):
        class TruncatedPrimary(PaidScriptedPrimary):
            model_id='synthetic-truncated-primary'
            def complete(self,*args,**kwargs):
                error=ModelAdapterError('response_truncated',model_id=self.model_id,attempts=1,http_status=200)
                error.usage={'prompt_tokens':11,'completion_tokens':4000,'total_tokens':4011}
                raise error
        reviewer=MeteredReviewer()
        result=ConversationAgent(TruncatedPrimary(),reviewer).process(self.g,self.w,'规划，不执行。')
        runtime=result['agent_runtime']
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'response_truncated')
        self.assertEqual(runtime['primary_tokens'],4011)
        self.assertEqual(runtime['total_tokens'],4011)
        self.assertEqual(runtime['budget_tokens'],4011)
        self.assertTrue(runtime['usage_complete'])
        self.assertEqual(runtime['model_calls'][0]['budget_source'],'provider_usage')
        self.assertEqual(reviewer.packets,[])
        self.assertEqual(result['bank_actions'],[])

    def test_paid_success_inconsistent_or_partial_usage_stops_after_one_call(self):
        for supplied,lower in (({'prompt_tokens':11,'completion_tokens':2,'total_tokens':1},13),
                              ({'prompt_tokens':11},11),
                              ({'prompt_tokens':True,'completion_tokens':2,'total_tokens':3},2)):
            with self.subTest(supplied=supplied):
                class Primary(PaidScriptedPrimary):
                    def complete(self,*args,**kwargs):
                        message,_=super().complete(*args,**kwargs)
                        return message,{**supplied,'billing_mode':'unpaid_fixture'}
                primary=Primary(call('knowledge_status',{}),{'role':'assistant','content':'未核验答复'})
                reviewer=MeteredReviewer()
                result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查公开资料')
                runtime=result['agent_runtime']
                self.assertTrue(result['isFallback'])
                self.assertEqual(result['error']['code'],'MODEL_USAGE_UNVERIFIED')
                self.assertIsNone(runtime['total_tokens'])
                self.assertFalse(runtime['usage_complete'])
                self.assertEqual(runtime['known_tokens_lower_bound'],lower)
                self.assertEqual(runtime['budget_tokens'],100000)
                self.assertEqual(len(primary.messages),1)
                self.assertEqual(reviewer.packets,[])
                self.assertEqual(result['bank_actions'],[])

    def test_paid_failed_unknown_usage_stops_primary_and_reviewer(self):
        for stage in ('primary','reviewer'):
            with self.subTest(stage=stage):
                class Primary(PaidScriptedPrimary):
                    def complete(self,*args,**kwargs):
                        if stage=='primary':
                            self.messages.append(args[0])
                            error=ModelAdapterError('provider_unavailable',model_id=self.model_id,attempts=1)
                            error.usage={'prompt_tokens':11}
                            raise error
                        return super().complete(*args,**kwargs)
                class FailedReviewer(PaidReviewer):
                    def review(self,packet,*args,**kwargs):
                        self.packets.append(packet)
                        error=ModelAdapterError('invalid_model_contract',model_id=self.model_id,attempts=1)
                        error.usage={'prompt_tokens':11,'completion_tokens':2,'total_tokens':1}
                        raise error
                primary=Primary(call('answer_reviewed',{'text':'需要核实期限。','claims':[],
                    'draft_ids':[],'followups':[]}))
                reviewer=FailedReviewer()
                result=ConversationAgent(primary,reviewer).process(self.g,self.w,'规划，不执行。')
                runtime=result['agent_runtime']
                self.assertEqual(result['error']['code'],'MODEL_USAGE_UNVERIFIED')
                self.assertEqual(runtime['budget_tokens'],100000)
                self.assertIsNone(runtime['total_tokens'])
                self.assertEqual(runtime['known_tokens_lower_bound'],11 if stage=='primary' else 14)
                self.assertEqual(len(primary.messages),1)
                self.assertEqual(len(reviewer.packets),0 if stage=='primary' else 1)
                self.assertEqual(runtime['model_calls'][-1]['budget_source'],'unknown_usage_stop')
                self.assertEqual(result['bank_actions'],[])

    def test_paid_unknown_usage_after_known_call_keeps_prior_lower_bound_once(self):
        class Primary(PaidScriptedPrimary):
            def complete(self,*args,**kwargs):
                message,usage=super().complete(*args,**kwargs)
                return message,usage if len(self.messages)==1 else {'prompt_tokens':11}
        primary=Primary(call('knowledge_status',{}),{'role':'assistant','content':'未核验文本'})
        result=ConversationAgent(primary,MeteredReviewer()).process(self.g,self.w,'查公开资料')
        runtime=result['agent_runtime']
        self.assertEqual(result['error']['code'],'MODEL_USAGE_UNVERIFIED')
        self.assertEqual(runtime['budget_tokens'],100000)
        self.assertEqual(runtime['known_tokens_lower_bound'],12)
        self.assertEqual(runtime['primary_tokens_known'],1)
        self.assertEqual(runtime['model_calls'][-1]['budget_tokens'],99999)
        self.assertEqual(len(primary.messages),2)

    def test_unpaid_marker_is_trusted_local_configuration(self):
        class UnmeteredFixture(ScriptedPrimary):
            def complete(self,*args,**kwargs):
                message,_=super().complete(*args,**kwargs)
                return message,{}
        result=ConversationAgent(UnmeteredFixture({'role':'assistant','content':'核实期限。'}),
            MeteredReviewer()).process(self.g,self.w,'规划')
        self.assertFalse(result.get('isFallback'))
        self.assertIsNone(result['agent_runtime']['total_tokens'])
        self.assertEqual(result['agent_runtime']['model_calls'][0]['billing_mode'],'unpaid_fixture')
        self.assertEqual(result['agent_runtime']['model_calls'][0]['budget_source'],'unpaid_fixture_estimate')

    def test_paid_preflight_rejects_before_any_call_when_complete_wire_does_not_fit(self):
        primary=PaidScriptedPrimary({'role':'assistant','content':'未发送'})
        reviewer=PaidReviewer()
        result=ConversationAgent(primary,reviewer,max_tokens=100).process(self.g,self.w,'规划')
        self.assertEqual(result['error']['code'],'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(primary.messages,[])
        self.assertEqual(reviewer.packets,[])
        admission=result['agent_runtime']['budget_admissions'][0]
        self.assertFalse(admission['accepted'])
        self.assertEqual(admission['stage'],'final')
        self.assertEqual(admission['review_reserve'],1400)
        self.assertEqual(admission['review_input_cap'],0)
        self.assertGreater(admission['reserve_details']['shadow_input_bytes'],admission['review_input_cap'])
        self.assertFalse(admission['provider_bound_certified'])

    def test_glm_complete_wire_bound_and_cap_match_actual_adapter_request(self):
        class Response(io.BytesIO):status=200
        envelope={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
            {'verdict':'approve','reason':'证据齐全','issues':[]},ensure_ascii=False)}}],
            'usage':{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11}}
        sent=[]
        with tempfile.TemporaryDirectory() as directory:
            client=OpenAICompatibleJsonClient(provider='ai_ping',model_id='synthetic-review',
                url='https://example.test',env_name='TEST',keychain_service='TEST',key_loader=lambda:'synthetic',
                urlopen=lambda request,**kw:(sent.append(request.data) or Response(json.dumps(envelope).encode())),
                log_path=str(Path(directory)/'calls.jsonl'))
            reviewer=GLMConversationReviewer(client)
            packet={'question':'中文与"引号"及\\转义','evidence':[]}
            bound=reviewer.request_input_bound(packet)
            with self.assertRaises(ModelAdapterError) as error:
                reviewer.review(packet,'blocked',10,max_input_bytes=bound-1)
            self.assertEqual(error.exception.attempts,0)
            self.assertEqual(sent,[])
            reviewer.review(packet,'allowed',10,max_input_bytes=bound)
            self.assertEqual(sent,[reviewer.serialized_request(packet)])
            self.assertEqual(len(sent[0]),bound)
            self.assertEqual(json.loads(sent[0])['max_tokens'],1400)

    def test_paid_normal_chain_reserves_final_and_full_review_with_original_limits(self):
        primary=PaidScriptedPrimary(call('knowledge_status',{}),call('answer_reviewed',{
            'text':'公开资料的可用范围已核对。','claims':[],'draft_ids':[],'followups':[]}))
        reviewer=PaidReviewer()
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查询公开资料可用范围')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        runtime=result['agent_runtime'];admissions=runtime['budget_admissions']
        self.assertEqual(runtime['total_tokens'],9)
        self.assertEqual(runtime['budget_policy']['limit'],100000)
        self.assertEqual(admissions[0]['stage'],'explore')
        self.assertGreater(admissions[0]['terminal_reserve'],4000)
        self.assertEqual(admissions[0]['reserved_seconds'],42)
        self.assertEqual(admissions[-1]['stage'],'reviewer')
        self.assertEqual(admissions[-1]['review_reserve'],0)
        self.assertEqual(admissions[-1]['reserved_seconds'],2)
        self.assertTrue(all(a['accepted'] for a in admissions))

    def test_final_guard_rejection_does_not_save_approved_answer(self):
        before=self.g.personal_data.read_history(self.w)
        def reject(result,commit):
            self.assertFalse(result.get('isFallback'))
            raise BankError('STALE_KNOWLEDGE_EVIDENCE','公开资料已撤回。',409)
        with self.assertRaises(BankError) as error:
            ConversationAgent(ScriptedPrimary({'role':'assistant','content':'待发布的答复。'}),
                MeteredReviewer()).process(self.g,self.w,'规划',finalize_result=reject)
        self.assertEqual(error.exception.code,'STALE_KNOWLEDGE_EVIDENCE')
        self.assertEqual(self.g.personal_data.read_history(self.w),before)

    def test_history_finalizer_keeps_workspace_lock_and_saves_exactly_once(self):
        agent=ConversationAgent(ScriptedPrimary({'role':'assistant','content':'已核验答复。'}),MeteredReviewer())
        def publish(result,commit):
            with self.assertRaises(BankError) as busy:
                agent.process(self.g,self.w,'另一个请求')
            self.assertEqual(busy.exception.code,'AGENT_BUSY')
            self.assertTrue(commit())
            with self.assertRaises(BankError) as repeated:commit()
            self.assertEqual(repeated.exception.code,'HISTORY_ALREADY_FINALIZED')
            return result
        result=agent.process(self.g,self.w,'规划',finalize_result=publish)
        saved=self.g.personal_data.read_history(self.w)
        self.assertEqual(saved[-1],{'role':'assistant','content':result['reply']})
        self.assertEqual(len(saved),2)
        self.assertNotIn('commit_history',json.dumps(result,ensure_ascii=False))

    def test_guarded_history_writer_is_explicit_and_missing_commit_is_rejected(self):
        for outcome in ('missing','false','saved'):
            with self.subTest(outcome=outcome):
                before=self.g.personal_data.read_history(self.w)
                writes=[]
                def publish(result,commit):
                    if outcome=='missing':return result
                    def writer(owner,messages,response_id,widgets,**checks):
                        writes.append((owner,messages,response_id,widgets,checks))
                        return outcome=='saved'
                    commit(writer=writer)
                    return result
                agent=ConversationAgent(ScriptedPrimary({'role':'assistant','content':'需核实条件。'}),MeteredReviewer())
                if outcome=='saved':
                    result=agent.process(self.g,self.w,'规划',finalize_result=publish)
                    self.assertEqual(writes[0][0],self.w)
                    self.assertEqual(writes[0][1][-1]['content'],result['reply'])
                    self.assertEqual(set(writes[0][4]),{'expected_revision','expected_privacy'})
                else:
                    with self.assertRaises(BankError) as error:
                        agent.process(self.g,self.w,'规划',finalize_result=publish)
                    self.assertEqual(error.exception.code,'HISTORY_FINALIZATION_REQUIRED' if outcome=='missing' else 'STALE_EVIDENCE')
                # The injected test writer only verifies the server contract;
                # actual cross-database persistence is the Base owner's gate.
                self.assertEqual(self.g.personal_data.read_history(self.w),before)

    def test_publication_remaining_seconds_uses_original_deadline_and_rejects_expiry(self):
        for fallback in (False,True):
            with self.subTest(fallback=fallback):
                primary=ScriptedPrimary(ModelAdapterError('local_fixture_failure',model_id='scripted-native-test',attempts=0)
                    if fallback else {'role':'assistant','content':'已核验的条件说明。'})
                agent=ConversationAgent(primary,MeteredReviewer())
                def finalize(result,commit):
                    from agent_runtime import conversation
                    current=conversation.time.monotonic()
                    remaining=commit.remaining_seconds()
                    self.assertEqual(result.get('isFallback',False),fallback)
                    if fallback:self.assertIsNone(remaining)
                    else:
                        self.assertGreater(remaining,0)
                        self.assertLessEqual(remaining,105)
                    with patch('agent_runtime.conversation.time.monotonic',return_value=current+106):
                        if fallback:
                            self.assertIsNone(commit.remaining_seconds())
                            self.assertIsNone(commit.assert_budget())
                        else:
                            with self.assertRaises(BankError) as expired:commit.remaining_seconds()
                            self.assertEqual(expired.exception.code,'AGENT_BUDGET_EXCEEDED')
                    self.assertTrue(commit(writer=lambda *a,**k:True))
                    self.assertNotIn('remaining_seconds',json.dumps(result,ensure_ascii=False))
                    return result
                agent.process(self.g,self.w,'仅核对条件，不执行。',finalize_result=finalize)

    def test_catalog_provider_pin_is_checked_without_any_delivered_rag_hit(self):
        from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
        class Provider:
            def __init__(self):self.calls=0;self.stale=False
            def assert_fresh(self):
                self.calls+=1
                if self.stale:raise ValueError('withdrawn_catalog_identity')
        provider=Provider();consumer=KnowledgeRetrieval(provider)
        consumer.assert_fresh()
        self.assertEqual(provider.calls,1)
        provider.stale=True
        with self.assertRaises(ValueError) as error:consumer.assert_fresh()
        self.assertEqual(str(error.exception),'stale_knowledge_evidence')
        self.assertEqual(provider.calls,2)

    def test_native_sent_failure_cannot_claim_zero_attempts(self):
        from agent_runtime.native_client import DeepSeekToolClient
        sent=[]
        def transport(request,**kwargs):
            sent.append(request.data)
            error=ModelAdapterError('provider_unavailable',model_id='fixture-native',attempts=0)
            error.usage={'prompt_tokens':11,'completion_tokens':2,'total_tokens':13}
            raise error
        with tempfile.TemporaryDirectory() as path:
            primary=DeepSeekToolClient(urlopen=transport,key_loader=lambda:'SYNTHETIC_SECRET',
                log_path=str(Path(path)/'native.jsonl'))
            reviewer=MeteredReviewer()
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'规划，不执行。')
        self.assertEqual(result['error']['code'],'provider_unavailable')
        self.assertEqual(len(sent),1)
        self.assertEqual(result['agent_runtime']['total_tokens'],13)
        self.assertEqual(result['agent_runtime']['budget_tokens'],13)
        self.assertEqual(result['agent_runtime']['model_calls'][0]['attempts'],1)
        self.assertTrue(result['agent_runtime']['model_calls'][0]['request_sent'])
        self.assertEqual(reviewer.packets,[])

    def test_trusted_local_pre_request_rejection_keeps_stale_reason_and_zero_cost(self):
        class RejectedPrimary(PaidScriptedPrimary):
            def complete(self,*args,**kwargs):
                error=BankError('STALE_KNOWLEDGE_EVIDENCE','公开资料已撤回。',409)
                error.attempts=0;error.request_sent=False
                raise error
        reviewer=MeteredReviewer()
        result=ConversationAgent(RejectedPrimary(),reviewer).process(self.g,self.w,'查公开资料')
        self.assertEqual(result['error']['code'],'STALE_KNOWLEDGE_EVIDENCE')
        runtime=result['agent_runtime']
        self.assertEqual(runtime['budget_tokens'],0)
        self.assertEqual(runtime['total_tokens'],0)
        self.assertTrue(runtime['usage_complete'])
        self.assertEqual(runtime['primary_requests_sent'],0)
        self.assertEqual(reviewer.packets,[])

    def test_invalid_native_reasoning_type_is_controlled_and_keeps_actual_usage(self):
        from agent_runtime.native_client import DeepSeekToolClient
        class Response(io.BytesIO):status=200
        envelope={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':None,
            'reasoning_content':float('nan'),'tool_calls':call('knowledge_status',{})['tool_calls']}}],
            'usage':{'prompt_tokens':11,'completion_tokens':2,'total_tokens':13}}
        with tempfile.TemporaryDirectory() as path:
            primary=DeepSeekToolClient(urlopen=lambda *a,**kw:Response(json.dumps(envelope).encode()),
                key_loader=lambda:'SYNTHETIC_SECRET',log_path=str(Path(path)/'native.jsonl'))
            reviewer=MeteredReviewer()
            result=ConversationAgent(primary,reviewer).process(self.g,self.w,'查公开资料')
        self.assertEqual(result['error']['code'],'invalid_model_contract')
        self.assertEqual(result['agent_runtime']['total_tokens'],13)
        self.assertEqual(result['agent_runtime']['budget_tokens'],13)
        self.assertEqual(result['agent_runtime']['primary_requests_sent'],1)
        self.assertEqual(reviewer.packets,[])

    def test_paid_repeated_amount_contract_uses_complete_bounded_review_packet(self):
        self.g.financial_metrics=SyntheticMetricService()
        def repeated(messages,schemas):
            message=compact_metric_answer(messages,schemas)
            final=json.loads(message['tool_calls'][0]['function']['arguments'])
            original=final['amount_claims'][0]
            fragments=[f'第{i}处：{original["text"]}' for i in range(1,7)]
            final['text']='\n'.join(fragments)
            final['claims']=[{'text':final['text'],'evidence_ids':final['claims'][0]['evidence_ids']}]
            final['amount_claims']=[{**original,'text':fragment} for fragment in fragments]
            return call('answer_reviewed',final)
        primary=PaidScriptedPrimary(call('account_summary',{}),repeated)
        primary.supports_compact_metric_sources=True
        reviewer=PaidReviewer()
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'只核对本次账户现金，不执行。')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        packet=reviewer.packets[0]
        self.assertEqual(len(packet['draft']['amount_claims']),6)
        self.assertEqual(len(packet['amount_contract']['amount_claims']),6)
        self.assertEqual(packet['amount_contract']['amount_occurrence_count'],6)
        admissions=result['agent_runtime']['budget_admissions']
        pre=next(a for a in reversed(admissions) if a['stage']!='reviewer')
        self.assertLessEqual(admissions[-1]['input_bytes'],pre['review_input_cap'])
        self.assertEqual(pre['reserve_details']['claim_limit'],24)
        self.assertEqual(pre['reserve_details']['metric_count'],1)
        self.assertGreater(pre['reserve_details']['worst_case_input_bytes'],pre['reserve_details']['shadow_input_bytes'])
        self.assertEqual(result['bank_actions'],[])

    def test_server_final_phase_rejects_new_tool_reads(self):
        primary=PaidScriptedPrimary(call('knowledge_status',{}))
        reviewer=PaidReviewer()
        result=ConversationAgent(primary,reviewer,max_rounds=1).process(self.g,self.w,'查询公开资料')
        self.assertEqual(result['error']['code'],'INVALID_FINAL_CONTRACT')
        self.assertEqual(result['agent_runtime']['tool_calls'],0)
        self.assertEqual(result['agent_runtime']['budget_admissions'][0]['stage'],'final')
        self.assertEqual(reviewer.packets,[])

    def test_actual_native_and_glm_wires_allow_simple_read_then_review_in_paid_budget(self):
        from agent_runtime.native_client import DeepSeekToolClient
        requests=[]
        class Response(io.BytesIO):status=200
        def primary_open(request,**kwargs):
            body=json.loads(request.data)
            requests.append(('primary',len(request.data),body))
            message=(call('account_summary',{}) if len(requests)==1
                     else compact_metric_answer(body['messages'],body['tools']))
            return Response(json.dumps({'choices':[{'finish_reason':'stop','message':message}],
                'usage':{'prompt_tokens':5,'completion_tokens':2,'total_tokens':7}},ensure_ascii=False).encode())
        def review_open(request,**kwargs):
            requests.append(('reviewer',len(request.data),json.loads(request.data)))
            return Response(json.dumps({'choices':[{'finish_reason':'stop','message':{
                'content':json.dumps({'verdict':'approve','reason':'离线协议核验','issues':[]})}}],
                'usage':{'prompt_tokens':11,'completion_tokens':2,'total_tokens':13}}).encode())
        primary=DeepSeekToolClient(urlopen=primary_open,key_loader=lambda:'offline-local-fixture',
            log_path=Path(self.tmp.name)/'primary-wire.jsonl')
        reviewer=GLMConversationReviewer(OpenAICompatibleJsonClient(provider='ai_ping',
            model_id='GLM-5.3-Flash',url='https://offline.invalid',env_name='OFFLINE_FIXTURE',
            keychain_service='OFFLINE_FIXTURE',urlopen=review_open,key_loader=lambda:'offline-local-fixture',
            log_path=Path(self.tmp.name)/'review-wire.jsonl'))
        before=self.g.overview(self.w)['total_cash_minor']
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'只核对本次账户现金，不执行。')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        self.assertEqual([r[0] for r in requests],['primary','primary','reviewer'])
        self.assertIn('account_summary',[tool['function']['name'] for tool in requests[0][2]['tools']])
        admissions=result['agent_runtime']['budget_admissions']
        self.assertEqual(admissions[0]['stage'],'explore')
        self.assertEqual(admissions[0]['exploration_preflight']['terminal_reasons'],[])
        self.assertEqual([a['input_bytes'] for a in admissions],[r[1] for r in requests])
        self.assertEqual(result['agent_runtime']['total_tokens'],27)
        self.assertLessEqual(admissions[-1]['input_bytes'],admissions[-2]['review_input_cap'])
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)

    def test_full24_actual_native_glm_retains_complete_proof_once_with_original_caps(self):
        from agent_runtime.review_evidence import unpack_review_evidence
        self.g.financial_metrics=SyntheticMetricService()
        def repeated(messages,schemas):
            original=json.loads(metric_answer(messages,schemas)['tool_calls'][0]['function']['arguments'])
            claim=original['amount_claims'][0]
            fragments=[f'第{i+1}项：{claim["text"]}' for i in range(24)]
            original['text']='\n'.join(fragments)
            original['claims']=[{'text':original['text'],'evidence_ids':original['claims'][0]['evidence_ids']}]
            original['amount_claims']=[{**claim,'text':fragment} for fragment in fragments]
            return call('answer_reviewed',original)
        primary,reviewer,sent=self.public_wire_clients(call('account_summary',{}),repeated)
        primary.supports_compact_metric_sources=False
        packets=[]
        encode=reviewer.encoded_packet
        def capture(packet):
            packets.append(deepcopy(packet))
            return encode(packet)
        reviewer.encoded_packet=capture
        before=self.g.overview(self.w)['total_cash_minor']
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'只解释账户记录，不执行操作。')
        self.assertFalse(result.get('isFallback'),result.get('error'))
        self.assertEqual([row['stage'] for row in sent],['primary','primary','reviewer'])
        wire_packet=json.loads(sent[-1]['body']['messages'][-1]['content'])
        original=next(packet for packet in packets if len(packet.get('amount_contract',{}).get('amount_claims',[]))==24)
        self.assertEqual(unpack_review_evidence(wire_packet),original)
        self.assertEqual(len(wire_packet['review_metric_catalog']),1)
        self.assertEqual(len(wire_packet['draft']['amount_claims']),24)
        self.assertEqual(len(wire_packet['amount_contract']['amount_claims']),24)
        metric=next(iter(wire_packet['review_metric_catalog'].values()))
        self.assertEqual(len(metric),19)
        self.assertEqual(metric,original['amount_contract']['amount_claims'][0]['metric'])
        self.assertIn('source_versions',metric)
        self.assertLess(len(json.dumps(wire_packet,ensure_ascii=False).encode()),
                        len(json.dumps(original,ensure_ascii=False).encode()))
        admissions=result['agent_runtime']['budget_admissions']
        self.assertEqual([row['input_bytes'] for row in admissions],[row['input_bytes'] for row in sent])
        self.assertLessEqual(sent[-1]['input_bytes'],admissions[-2]['review_input_cap'])
        self.assertEqual(sent[0]['body']['max_tokens'],4000)
        self.assertEqual(sent[-1]['body']['max_tokens'],1400)
        self.assertEqual(result['agent_runtime']['total_tokens'],27)
        self.assertLessEqual(result['agent_runtime']['budget_tokens'],100000)
        self.assertEqual(result['bank_actions'],[])
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)

        self.full24_wire_evidence={'amount_claim_count':24,'catalog_metric_count':1,
            'complete_metric_field_count':len(metric),'lossless_roundtrip':True,
            'admissions':admissions,'sent':[{'stage':row['stage'],'input_bytes':row['input_bytes'],
                'wire_sha256':row['wire_sha256'],'output_limit':row['body']['max_tokens']} for row in sent],
            'runtime':result['agent_runtime'],'financial_unchanged':True,
            'paid_policy_with_offline_transport':True,'cloud_calls':0}

    def test_review_evidence_collision_and_exact_wire_cap_reject_before_key_or_network(self):
        requests=[];keys=[]
        client=OpenAICompatibleJsonClient(provider='ai_ping',model_id='offline-review',
            url='https://offline.invalid',env_name='OFFLINE_FIXTURE',keychain_service='OFFLINE_FIXTURE',
            key_loader=lambda:keys.append(True),urlopen=lambda *a,**k:requests.append(True),
            log_path=Path(self.tmp.name)/'unrequested-review.jsonl')
        reviewer=GLMConversationReviewer(client)
        packet={'question':'还需确认期限。','draft':{'text':'还需确认期限。'}}
        for candidate,cap,code in (({'review_metric_catalog':{}},None,'REVIEW_EVIDENCE_RESERVED_FIELD'),
                (packet,reviewer.request_input_bound(packet)-1,'request_budget_exceeded')):
            with self.subTest(code=code),self.assertRaises(ModelAdapterError) as caught:
                reviewer.review(candidate,'local-rejection',10,max_input_bytes=cap)
            self.assertEqual(caught.exception.code,code)
            self.assertIs(caught.exception.request_sent,False)
            self.assertEqual(caught.exception.attempts,0)
        self.assertEqual(keys,[])
        self.assertEqual(requests,[])

    def test_invalid_reviewer_content_or_verdict_keeps_actual_usage_and_blocks(self):
        for content,code in (('invalid PRIVATE_REVIEW','invalid_model_contract'),
                (json.dumps({'verdict':'INVALID_PRIVATE_VERDICT','reason':'无效','issues':[]}),
                 'INVALID_TOOL_ARGUMENTS')):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as path:
                envelope={'choices':[{'finish_reason':'stop','message':{'content':content}}],
                    'usage':{'prompt_tokens':11,'completion_tokens':2,'total_tokens':13}}
                class Response(io.BytesIO):status=200
                client=OpenAICompatibleJsonClient(provider='ai_ping',model_id='synthetic-review',
                    url='https://example.com',env_name='UNUSED_SYNTHETIC_KEY',keychain_service='UNUSED_SYNTHETIC_KEY',
                    urlopen=lambda *a,**kw:Response(json.dumps(envelope).encode()),key_loader=lambda:'SYNTHETIC_SECRET',
                    log_path=str(Path(path)/'calls.jsonl'),max_retries=0)
                reviewer=GLMConversationReviewer(client)
                primary=ScriptedPrimary(call('answer_reviewed',{'text':'还需核实期限。','claims':[],
                    'draft_ids':[],'followups':[]}))
                result=ConversationAgent(primary,reviewer).process(self.g,self.w,'规划，不执行。')
                self.assertTrue(result['isFallback'])
                self.assertEqual(result['error']['code'],code)
                runtime=result['agent_runtime']
                self.assertEqual(runtime['primary_tokens'],1)
                self.assertEqual(runtime['reviewer_tokens'],13)
                self.assertEqual(runtime['total_tokens'],14)
                self.assertEqual(runtime['budget_tokens'],14)
                self.assertTrue(runtime['usage_complete'])
                self.assertEqual(runtime['model_calls'][-1]['outcome'],'failed')
                self.assertEqual(runtime['model_calls'][-1]['attempts'],1)
                self.assertEqual(result['bank_actions'],[])
                for private in ('PRIVATE_REVIEW','INVALID_PRIVATE_VERDICT','SYNTHETIC_SECRET'):
                    self.assertNotIn(private,json.dumps(runtime,ensure_ascii=False))
                    self.assertNotIn(private,(Path(path)/'calls.jsonl').read_text())

    def test_total_budget_stops_before_unaffordable_review(self):
        reviewer=MeteredReviewer()
        primary=ScriptedPrimary(call('answer_reviewed',{'text':'尚未执行。','claims':[],
            'draft_ids':[], 'followups':[]}))
        result=ConversationAgent(primary,reviewer,max_tokens=2).process(self.g,self.w,'规划')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(reviewer.packets,[])
        self.assertEqual(result['bank_actions'],[])

    def test_approved_review_cannot_override_total_token_limit(self):
        class OverLimitReviewer(MeteredReviewer):
            def review(self,*args):
                return {**super().review(*args),'_model_usage':{'prompt_tokens':100000,'completion_tokens':1,'total_tokens':100001}}
        result=ConversationAgent(ScriptedPrimary({'role':'assistant','content':'需要核实期限。'}),
            OverLimitReviewer()).process(self.g,self.w,'规划')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(result['agent_runtime']['total_tokens'],100002)
        self.assertEqual(result['agent_review']['status'],'blocked')
        self.assertEqual(result['bank_actions'],[])

    def test_deadline_crossed_in_last_freshness_check_is_blocked_and_metered(self):
        clock={'now':0.,'reviewed':False,'checks':0}
        original=BankTools.fresh
        def late_fresh(tool):
            original(tool)
            if clock['reviewed']:
                clock['checks']+=1
                if clock['checks']==3:clock['now']=2.
        reviewer=MeteredReviewer(hook=lambda _:clock.update(reviewed=True))
        with patch('agent_runtime.conversation.time.monotonic',side_effect=lambda:clock['now']), \
                patch.object(BankTools,'fresh',late_fresh):
            result=ConversationAgent(ScriptedPrimary({'role':'assistant','content':'需要核实期限。'}),
                reviewer,timeout_seconds=1).process(self.g,self.w,'规划')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'AGENT_BUDGET_EXCEEDED')
        self.assertEqual(result['agent_runtime']['duration_ms'],2000)
        self.assertEqual(result['agent_review']['status'],'blocked')
        self.assertEqual(result['bank_actions'],[])
        self.assertNotIn('需要核实期限',result['_approved_tts_text'])

    def test_same_number_wrong_object_never_reaches_review(self):
        self.g.financial_metrics=SyntheticMetricService()
        primary=ScriptedPrimary(call('account_summary',{}),
            lambda m,t:metric_answer(m,t,wrong_subject=True),metric_answer)
        reviewer=MeteredReviewer()
        result=ConversationAgent(primary,reviewer).process(self.g,self.w,'解释日常账户现金')
        self.assertFalse(result.get('isFallback'))
        self.assertIn('METRIC_CLAIM_MISMATCH',result['agent_runtime']['contract_errors'])
        self.assertEqual(len(reviewer.packets),1)
        self.assertEqual(reviewer.packets[0]['amount_contract']['status'],'validated')
        self.assertEqual(len(result['answer_contract']['metric_ids']),1)

    def test_source_change_after_review_blocks_old_amount(self):
        service=SyntheticMetricService();self.g.financial_metrics=service
        reviewer=MeteredReviewer(hook=lambda packet:setattr(service,'version','synthetic-2'))
        result=ConversationAgent(ScriptedPrimary(call('account_summary',{}),metric_answer),
            reviewer).process(self.g,self.w,'解释日常账户现金')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'],'METRIC_VERSION_CONFLICT')
        self.assertNotIn('22,890',result['reply'])
        self.assertEqual(result['bank_actions'],[])

    def test_unknown_original_receipt_prevents_a_new_payment_draft(self):
        tool=BankTools(self.g,self.w,'unknown-receipt')
        overview=tool.invoke('account_summary',{})['data']
        payments=tool.invoke('business_records',{'kind':'payments'})['data']
        account=next(a['id'] for a in overview['accounts'] if a['id'].endswith('-main'))
        beneficiary=next(b['id'] for b in payments['beneficiaries'] if b['alias']=='妈妈')
        payload={'action':'transfer','account_id':account,'beneficiary_id':beneficiary,
            'amount_minor':100,'idempotency_key':'synthetic-unknown-receipt'}
        original=self.g.bank.prepare(self.w,payload)
        with self.g.bank.transaction() as db:
            db.execute("UPDATE operations SET state='unknown' WHERE id=?",(original['id'],))
        before=self.g.overview(self.w)['accounts']
        with self.assertRaises(BankError) as error:
            tool.invoke('prepare_operation',{key:value for key,value in payload.items() if key!='idempotency_key'})
        self.assertEqual(error.exception.code,'OPERATION_STATUS_REQUIRED')
        self.assertEqual(self.g.overview(self.w)['accounts'],before)
        with self.g.bank.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],1)

    def test_review_envelope_usage_is_preserved_without_response_body(self):
        class Response(io.BytesIO): status=200
        envelope={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
            {'verdict':'approve','reason':'证据完整','issues':[]},ensure_ascii=False)}}],
            'usage':{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11,'secret':'excluded'}}
        with tempfile.TemporaryDirectory() as directory:
            client=OpenAICompatibleJsonClient(provider='ai_ping',model_id='test',url='https://example.test',
                env_name='TEST',keychain_service='TEST',key_loader=lambda:'synthetic',
                urlopen=lambda *a,**k:Response(json.dumps(envelope).encode()),
                log_path=str(Path(directory)/'calls.jsonl'))
            result=GLMConversationReviewer(client).review({'question':'一般澄清'},'case',10)
        self.assertEqual(result['_model_usage'],{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11})
        self.assertEqual(set(result),{'verdict','reason','issues','_model_usage'})


if __name__=='__main__': unittest.main()
