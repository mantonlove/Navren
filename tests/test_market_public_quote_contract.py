"""新闻公开引文的源绑定与真实适配器接线；模型输出为免费夹具。"""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

from cryptography.fernet import Fernet
from agent_runtime.bank_tools import BankTools, native_tools
from agent_runtime.conversation import ConversationAgent, GLMConversationReviewer
from agent_runtime.data_boundary import CloudDataBoundary, BoundaryError
from agent_runtime.model_adapters import OpenAICompatibleJsonClient
from finance_gateway import FinanceGateway
from simulated_bank import BankError
from test_agent_market_evidence import MarketFixture
from test_native_conversation import ScriptedPrimary, call, last_tool


class TypedPrimary(ScriptedPrimary):
    metric_selection_support = True


class MarketPublicQuoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode()})
        self.env.start(); self.addCleanup(self.env.stop)
        self.g = FinanceGateway(Path(self.tmp.name)/'bank.sqlite3')
        self.g.market = MarketFixture(); self.w, _ = self.g.create_session()
        # Published summary and original wording deliberately differ, as in the reported case.
        self.g.market.event_data['facts'] = [{
            'article_id': 'a_policy', 'text': '减税两年累计减收约10万亿日元。',
            'quote': '两年累计减收约10万亿日元（1元人民币约合23.6日元）。'}, {
            'article_id': 'a_policy', 'text': '预算申请总额达143万亿日元。',
            'quote': '2027财年预算申请总额却攀升至143万亿日元，增幅高达约17%。'}]
        self.g.market.article_data['evidence_excerpt'] = ''.join(f['quote'] for f in self.g.market.event_data['facts'])

    def read(self):
        t = BankTools(self.g, self.w, 'public-quote', metric_selections=True)
        return t, t.invoke('market_event', {'event_id': 'e_policy'})

    @staticmethod
    def answer(ids, evidence):
        return {'direct_answer': [{'text': '背景是财政扩张与货币收紧方向不一。报道记载：'},
                                 *[{'public_quote_fact_id': f} for f in ids],
                                 {'text': '减税意在减轻生活成本，但财政收入减少与支出扩大叠加；加息则提高融资成本，政策效果可能相互抵消。'}],
                'conditions': [], 'unknowns': [],
                'claims': [{'text': '财政与货币政策方向不一。', 'evidence_ids': [evidence]}],
                'amount_claims': [], 'draft_ids': [], 'followups': []}

    def test_canonical_originals_cross_boundary_and_typed_schema_then_amount_guard(self):
        t, out = self.read(); b = CloudDataBoundary()
        view = b.model_tool_result('market_event', out)
        self.assertEqual({f['text'] for f in out['facts']}, {f['quote'] for f in self.g.market.event_data['facts']})
        self.assertEqual(len(t.public_quote_fact_ids()), 2)
        schemas = b.native_tools(native_tools(facts=t.facts, evidence=t.evidence,
            metric_selections=True, public_quote_fact_ids=t.public_quote_fact_ids()), metric_selections=True)
        terminal = next(s['function']['parameters'] for s in schemas if s['function']['name']=='answer_reviewed')
        self.assertIn('direct_answer', terminal['required']); self.assertNotIn('text', terminal['properties'])
        model = self.answer([f['id'] for f in view['facts']], view['evidence_id'])
        restored = b.restore_arguments('answer_reviewed', model)
        rendered = t.invoke('answer_reviewed', restored)
        self.assertEqual(t.amount_contract(rendered)['status'], 'validated')
        for row in self.g.market.event_data['facts']:
            self.assertIn('“'+unicodedata.normalize('NFKC',row['quote'])+'”', rendered['text'])
        self.assertTrue(t.advice)  # No fact-only review bypass.
        self.assertTrue(all(f['reviewed_quote_claim']['evidence_ids']==[view['evidence_id']] for f in view['facts']))

    def test_wrong_article_text_or_edition_cannot_become_a_quote(self):
        for mutation in ('article','text','version','source','context'):
            with self.subTest(mutation=mutation):
                t, out = self.read(); fid = out['facts'][0]['id']; fact=t.facts[fid]
                if mutation=='article': fact['article_id']='a_other'
                elif mutation=='text': fact['text']='这是我编造的10万亿日元。'
                elif mutation=='version': t.evidence[out['evidence_id']]['data']['version']=999
                elif mutation=='source': t.evidence[out['evidence_id']]['source']='simulation'
                else: t.evidence[out['evidence_id']]['context_hash']='other-account-context'
                self.assertNotIn(fid, t.public_quote_fact_ids())

    def test_unquoted_or_personal_money_is_rejected_before_review(self):
        t, out=self.read()
        for text in ('你的可投资资金为10万亿日元。', out['facts'][0]['text'], '143万亿人民币。'):
            final={'text':text,'claims':[{'text':text,'evidence_ids':[out['evidence_id']]}],
                   'amount_claims':[],'draft_ids':[],'followups':[]}
            with self.assertRaises(BankError) as e:t.amount_contract(final)
            self.assertEqual(e.exception.code,'METRIC_AMOUNT_UNBOUND')

    def test_source_retracted_after_read_fails_before_render(self):
        t, out=self.read(); args=self.answer(t.public_quote_fact_ids(),out['evidence_id'])
        self.g.market.article_data['status']='withdrawn'
        self.assertEqual(t.public_quote_fact_ids(),[])
        with self.assertRaises(BankError) as e:t.fresh()
        self.assertEqual(e.exception.code,'STALE_MARKET_EVIDENCE')

    def test_metadata_unverified_or_unavailable_does_not_issue_quote(self):
        for key,value in [('read_scope','metadata_only'),('link_status','unverified'),('status','withdrawn')]:
            before=copy.deepcopy(self.g.market.article_data)
            self.g.market.article_data[key]=value
            t, _=self.read();self.assertEqual(t.public_quote_fact_ids(),[])
            self.g.market.article_data.clear();self.g.market.article_data.update(before)
        self.g.market.event_data['availability']='withdrawn'
        t, _=self.read(); self.assertEqual(t.public_quote_fact_ids(),[])

    def test_cross_turn_alias_and_private_read_do_not_authorize_public_only_mode(self):
        t,out=self.read(); b=CloudDataBoundary(); view=b.model_tool_result('market_event',out)
        other=CloudDataBoundary(); other.native_tools(native_tools(metric_selections=True),metric_selections=True)
        with self.assertRaises(BoundaryError):other.restore_arguments('answer_reviewed',self.answer([view['facts'][0]['id']],view['evidence_id']))
        t.invoke('account_summary',{});self.assertEqual(t.public_quote_fact_ids(),[])

    def conversation(self, *, withdraw=False, repair=False):
        wires=[]
        def open_fixture(request, **kwargs):
            packet=json.loads(request.data); wires.append(packet)
            if withdraw:self.g.market.article_data['status']='withdrawn'
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({
                'verdict':'approve','reason':'免费接线夹具，不代表真实模型语义验收。','issues':[]},ensure_ascii=False)}}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode())
        reviewer=GLMConversationReviewer(OpenAICompatibleJsonClient(provider='ai_ping',
            model_id=GLMConversationReviewer.model_id,url='https://fixture.invalid/review',
            env_name='FIXTURE_NO_KEY',keychain_service='fixture-not-a-secret',
            key_loader=lambda:'unpaid-fixture',urlopen=open_fixture,max_retries=0))
        reviewer.billing_mode='unpaid_fixture'
        def reply(messages, schemas):
            out=last_tool(messages)
            schema=next(s['function']['parameters'] for s in schemas if s['function']['name']=='answer_reviewed')
            self.assertIn('direct_answer',schema['required'])
            args=self.answer([f['id'] for f in out['facts']],out['evidence_id'])
            if repair:
                args['direct_answer']=[{'text':'财政扩张与货币收紧方向不同：减税累计减收10万亿日元。'},
                    {'text':'预算申请总额143万亿日元，进一步加重财政压力。'}]
                args['unknowns']=[[{'text':'法案尚待审议，不能据此推断个人收益。'}]]
            return call('answer_reviewed',args)
        def choose(messages,schemas):
            schema=next(s['function']['parameters'] for s in schemas if s['function']['name']=='answer_reviewed')
            self.assertNotIn('text',schema['properties'])
            self.assertIn('selections',schema['properties'])
            function=next(s['function'] for s in schemas if s['function']['name']=='answer_reviewed')
            self.assertIn('paragraphs',function['description'])
            self.assertIn('selections',function['description'])
            self.assertNotIn('replacements',function['description'])
            self.assertEqual(set(schema['properties']),{'base_sha256','selections','paragraphs'})
            return call('answer_reviewed',{'base_sha256':schema['properties']['base_sha256']['enum'][0],
                'selections':{key:row['enum'][0] for key,row in schema['properties']['selections']['properties'].items()},'paragraphs':{path:'财政扩张与货币收紧形成方向上的冲突。报道披露的事实如下：' for path in schema['properties']['paragraphs']['properties']}})
        primary=TypedPrimary(call('market_event',{'event_id':'e_policy'}),reply,*([choose]if repair else []))
        agent=ConversationAgent(primary,reviewer)
        result=agent.process(self.g,self.w,'给我详细介绍一下这个事件的背景')
        return agent,result,wires

    def test_full_conversation_uses_typed_answer_and_unchanged_real_review_adapter(self):
        agent,result,wires=self.conversation()
        self.assertFalse(result.get('isFallback'),result.get('error'))
        self.assertEqual(result['agent_review']['status'],'approved')
        self.assertEqual(result['agent_runtime']['contract_errors'],[])
        self.assertEqual(result['bank_actions'],[]);self.assertEqual(len(wires),1)
        self.assertTrue(agent._final_proofs._entries)
        self.assertTrue(all(e['response_hash'] for e in agent._final_proofs._entries.values()))

    def test_source_withdrawn_during_review_never_publishes_approved_text(self):
        agent,result,wires=self.conversation(withdraw=True)
        self.assertTrue(result.get('isFallback'))
        self.assertEqual(result['error']['code'],'STALE_MARKET_EVIDENCE')
        self.assertEqual(result['bank_actions'],[])
        self.assertEqual(len(wires),1)
        self.assertFalse(any(e['response_hash'] for e in agent._final_proofs._entries.values()))

    def test_bad_market_draft_only_selects_sources_then_full_review_and_proof(self):
        agent,result,wires=self.conversation(repair=True)
        self.assertFalse(result.get('isFallback'),result.get('error'))
        self.assertEqual(result['agent_runtime']['contract_errors'],['METRIC_AMOUNT_UNBOUND'])
        self.assertEqual(result['agent_runtime']['rounds'],3)
        self.assertEqual(len(wires),1)
        self.assertIn(unicodedata.normalize('NFKC','法案尚待审议，不能据此推断个人收益。'),result['reply'])
        self.assertIn('“两年累计减收约10万亿日元',result['reply'])
        self.assertIn('“2027财年预算申请总额',result['reply'])
        self.assertTrue(all(e['response_hash'] for e in agent._final_proofs._entries.values()))

    def test_market_selector_is_exact_amount_bound_and_rejects_changed_source(self):
        from agent_runtime.market_quote_recovery import build_market_repair,apply_market_repair
        answer=self.answer([], 'ev');answer['direct_answer']=[{'text':'减收10万亿日元。'},{'text':'预算143万亿日元。'}]
        originals={str(i):f['quote'] for i,f in enumerate(self.g.market.event_data['facts'])}
        paths=['direct_answer[0].text','direct_answer[1].text']
        context=build_market_repair(answer,paths,originals)
        selection={'base_sha256':context['base_sha256'],'selections':{s['slot_id']:s['source_choices'][0]for s in context['slots']},'paragraphs':{path:'财政政策与货币政策形成相反作用。' for path in paths}}
        restored=apply_market_repair(answer,context,originals,selection)
        self.assertEqual(restored['claims'],answer['claims'])
        self.assertNotIn('所引报道中的该项金额',json.dumps(restored,ensure_ascii=False))
        self.assertEqual(restored['conditions'],answer['conditions'])
        self.assertEqual(restored['unknowns'],answer['unknowns'])
        for invalid in ('预算143万亿日元。','所引报道中的该项金额',''):
            bad=copy.deepcopy(selection);bad['paragraphs'][paths[0]]=invalid
            with self.assertRaises(ValueError):apply_market_repair(answer,context,originals,bad)
        bad=copy.deepcopy(selection);bad['paragraphs']['unknowns[0][0].text']='改动未授权段。'
        with self.assertRaises(ValueError):apply_market_repair(answer,context,originals,bad)
        bad=copy.deepcopy(selection);bad['conditions']=[]
        with self.assertRaises(ValueError):apply_market_repair(answer,context,originals,bad)
        self.assertEqual(sum('public_quote_fact_id'in p for p in restored['direct_answer']),2)
        bad=copy.deepcopy(selection);bad['selections'][context['slots'][0]['slot_id']]='other-turn'
        with self.assertRaises(ValueError):apply_market_repair(answer,context,originals,bad)
        changed={**originals,'0':'改变后的10万亿日元原文。'}
        with self.assertRaises(ValueError):apply_market_repair(answer,context,changed,selection)

    def test_repaired_market_draft_is_also_rejected_on_source_withdrawal(self):
        agent,result,wires=self.conversation(repair=True,withdraw=True)
        self.assertTrue(result.get('isFallback'))
        self.assertEqual(result['error']['code'],'STALE_MARKET_EVIDENCE')
        self.assertEqual(len(wires),1)
        self.assertFalse(any(e['response_hash'] for e in agent._final_proofs._entries.values()))


if __name__=='__main__':unittest.main(verbosity=2)
