"""真实序列化/预算/业务读取，提供商传输替身；零云模型质量结论。"""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.conversation import ConversationAgent, GLMConversationReviewer
from agent_runtime.jev_agent import JevConversationAgent
from agent_runtime.model_adapters import OpenAICompatibleJsonClient, AIPING_URL, GLM_REVIEW_MODEL
from agent_runtime.native_client import AIPingToolClient
from test_jev_agent import Judge
from test_native_conversation import call


class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class OfflinePrimary:
    def __init__(self):self.requests=[]
    def __call__(self,request,**kwargs):
        payload=json.loads(request.data);self.requests.append(payload)
        turn=len(self.requests)
        if turn==1:
            message=call('load_skill',{'name':'product-research'})
            extra=call('load_skill',{'name':'knowledge-retrieval'})['tool_calls']
            extra[0]['id']='call-knowledge-retrieval'
            message['tool_calls']+=extra
        elif turn==2:
            visible={t['function']['name'] for t in payload['tools']}
            assert {'public_products','knowledge_status'} <= visible, 'business_reads_lost_to_final_view'
            message=call('public_products',{})
            message['tool_calls']+=call('knowledge_status',{})['tool_calls']
        elif turn==3:
            outputs=[json.loads(m['content']) for m in payload['messages'] if m['role']=='tool']
            e=outputs[-1]['evidence_id']
            message=call('answer_reviewed',{'text':'资料检索尚未接入。历史目录不能确认当前费用和在售渠道。下周用钱的赎回与到账条件仍需核验。',
                'claims':[{'text':'公共资料检索尚未接入。','evidence_ids':[e]}], 'draft_ids':[],'followups':[]})
        else:raise AssertionError('unexpected_primary_retry')
        # 原pilot第一轮实际spent，更保守地验证压缩后仍可继续；不是新provider计量。
        usage={'prompt_tokens':6853 if turn==1 else 100,'completion_tokens':94 if turn==1 else 10,
               'total_tokens':6947 if turn==1 else 110}
        return Response(json.dumps({'model':'DeepSeek-V4-Flash','choices':[{'message':message}], 'usage':usage},ensure_ascii=False).encode())


class OfflineReviewer:
    def __init__(self):self.requests=[]
    def __call__(self,request,**kwargs):
        payload=json.loads(request.data);self.requests.append(payload)
        result={'verdict':'approve','reason':'离线协议替身','issues':[]}
        return Response(json.dumps({'model':GLM_REVIEW_MODEL,'choices':[{'message':{'role':'assistant','content':json.dumps(result)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}).encode())


class ProgressivePaidWireTest(unittest.TestCase):
    def test_both_arms_reach_business_reads_after_two_skill_loads(self):
        path=Path(__file__).resolve().parent/'window_integration/semantic_oracle.py'
        spec=importlib.util.spec_from_file_location('progressive_wire_oracle',path)
        oracle=importlib.util.module_from_spec(spec);spec.loader.exec_module(oracle)
        case=next(c for c in oracle.load_dataset()['cases'] if c['id'].startswith('S06_'))
        for variant in ('baseline','jev'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as tmp,patch(
                    'socket.create_connection',side_effect=AssertionError('network_forbidden')):
                g,w,_=oracle.build_fixture(case,tmp)
                primary_transport,review_transport=OfflinePrimary(),OfflineReviewer()
                primary=AIPingToolClient(urlopen=primary_transport,key_loader=lambda:'offline-fixture',log_path=str(Path(tmp)/'primary.jsonl'))
                reviewer=GLMConversationReviewer(OpenAICompatibleJsonClient(provider='ai_ping',model_id=GLM_REVIEW_MODEL,
                    url=AIPING_URL,env_name='AIPING_API_KEY',keychain_service='unused-fixture',key_loader=lambda:'offline-fixture',
                    urlopen=review_transport,max_retries=0,log_path=str(Path(tmp)/'reviewer.jsonl')))
                kwargs={'tool_routing':'progressive','max_tokens':100000}
                if variant=='jev':
                    agent=JevConversationAgent(primary,reviewer,judge=Judge(selection='product-research'),jev_mode='active',**kwargs)
                else:agent=ConversationAgent(primary,reviewer,**kwargs)
                result=agent.process(g,w,case['prompt'])
                self.assertFalse(result.get('isFallback',False),result.get('error'))
                admissions=[a for a in result['agent_runtime']['budget_admissions'] if a['stage']!='reviewer']
                self.assertEqual([a['stage'] for a in admissions[:2]],['explore','explore'])
                self.assertTrue(all(a['accepted'] for a in admissions))
                self.assertGreaterEqual(admissions[1]['review_input_cap'],admissions[1]['exploration_preflight']['minimum_review_input'])
                tools={t['tool'] for t in result['agent_runtime']['trace'] if t['status']=='completed'}
                self.assertTrue({'public_products','knowledge_status'}<=tools)
                self.assertEqual(len(primary_transport.requests),3)
                self.assertEqual(len(review_transport.requests),1)
                self.assertEqual(result['bank_actions'],[])


if __name__=='__main__':unittest.main()
