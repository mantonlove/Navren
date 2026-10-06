"""新闻金额不是账户金额；公开取证、免审边界和重复校验失败的回归。"""
import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_runtime.bank_tools import BankTools, market_agent_view, monetary_literals
from agent_runtime.conversation import ConversationAgent
from finance_gateway import FinanceGateway
from test_native_conversation import ScriptedPrimary, Reviewer, call, last_tool


class MarketFixture:
    def __init__(self):
        self.article_data={'id':'a_policy','url':'https://www.pbc.gov.cn/policy.html',
            'title':'贷款上限100万元','publisher':'中国人民银行','published_at':'2026-10-01T00:00:00+00:00',
            'status':'active','read_scope':'full_text','link_status':'verified',
            'evidence_excerpt':'单户贴息贷款上限100万元。再贷款额度增至1.4万亿元。'}
        self.event_data={'id':'e_policy','headline':'贷款贴息政策','status':'published',
            'published_at':self.article_data['published_at'],'version':1,'articles':[self.article_data],
            'facts':[{'article_id':'a_policy','text':'单户贴息贷款上限100万元。',
                      'quote':'单户贴息贷款上限100万元。'},
                     {'article_id':'a_policy','text':'再贷款额度增至1.4万亿元。',
                      'quote':'再贷款额度增至1.4万亿元。'}],
            'analysis':'假设用户余额为900万元。','review':{'reason':'审核费用10万元。'},
            'ranking_reason':{'score':1},'uncertainty':'须符合条件。'}
    def hotspots(self):
        return copy.deepcopy({'as_of':self.article_data['published_at'],'status':'published',
            'events':[self.event_data],'reading':[],
            'coverage':{'state':'ready','limitations':['非全市场覆盖。'],'sources':[{'id':'nbs','status':'ok'}]}})
    def article(self,id):return copy.deepcopy(self.article_data)
    def event(self,id):return copy.deepcopy(self.event_data)
    def weekly(self,period):
        return {'status':'published','report':{'status':'published','period':period,
            'citations':[{'event_id':'e_policy'}]},'coverage':{'state':'ready'}}


def final(text, evidence_id):
    return call('answer_reviewed',{'text':text,'claims':[{'text':text,'evidence_ids':[evidence_id]}],
                                 'draft_ids':[],'followups':[]})


def market_output(messages):
    return next(json.loads(message['content']) for message in reversed(messages)
                if message['role'] == 'tool' and any(fact.get('unit') == 'public_minor_currency'
                    for fact in json.loads(message['content']).get('facts', [])))


def public_quote_reply(messages, tools, *, values=None):
    """合法公开金额必须全文显式引用本轮 canonical fact.text。"""
    output = market_output(messages)
    quotes = list(dict.fromkeys('“' + fact['text'] + '”' for fact in output['facts']
        if fact['unit'] == 'public_minor_currency' and (values is None or fact['value'] in values)))
    return call('answer_reviewed', {'text': '政策原文：' + '；'.join(quotes) + '须符合原文条件。',
        'claims': [{'text': quote, 'evidence_ids': [output['evidence_id']]} for quote in quotes],
        'amount_claims': [], 'draft_ids': [], 'followups': []})


class MarketEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=FinanceGateway(Path(self.tmp.name)/'bank.sqlite3')
        self.g.market=MarketFixture();self.w,_=self.g.create_session()

    def run_agent(self,*steps,reviewer=None,**kwargs):
        self.primary=ScriptedPrimary(*steps);self.reviewer=reviewer or Reviewer()
        return ConversationAgent(self.primary,self.reviewer,**kwargs).process(self.g,self.w,'今天有什么热点新闻')

    def rejected_money(self,text):
        answer=lambda m,t:final(text,next(json.loads(x['content'])['evidence_id'] for x in m
            if x['role']=='tool' and 'evidence_id' in json.loads(x['content'])))
        return self.run_agent(call('market_hotspots',{}),answer,answer,answer)

    def test_policy_amount_and_large_currency_units_reach_independent_review(self):
        r=self.run_agent(call('market_hotspots',{}),public_quote_reply)
        self.assertFalse(r.get('isFallback'));self.assertEqual(r['agent_review']['status'],'approved')
        self.assertEqual(r['agent_runtime']['rounds'],2);self.assertEqual(r['agent_runtime']['contract_errors'],[])
        self.assertEqual(r['agent_runtime']['trace'][0]['tool'],'market_hotspots')
        amounts=self.reviewer.packets[0]['monetary_evidence']
        self.assertEqual({v['unit'] for v in amounts},{'public_minor_currency'})
        self.assertTrue(all(v['scope']=='public_statement_not_account_balance' for v in amounts))
        self.assertEqual({v['value'] for v in amounts}, {100000000, 140000000000000})

    def test_scale_conversion_covers_news_units_without_float_rounding(self):
        self.assertEqual(list(monetary_literals('2000亿元、1.4万亿元、150万元、1.2万美元')),
            [(20000000000000,'CNY'),(140000000000000,'CNY'),(150000000,'CNY'),(1200000,'USD')])

    def test_citation_summary_need_not_duplicate_every_policy_amount(self):
        def answer(m,t):
            output = market_output(m)
            quote = '“' + output['facts'][0]['text'] + '”'
            return call('answer_reviewed',{'text':'该发布说明了贷款贴息的适用条件，须核对本人资格。',
                'claims':[{'text':'该发布说明了贷款贴息的适用条件。','evidence_ids':[output['evidence_id']]},
                          {'text':quote,'evidence_ids':[output['evidence_id']]}],
                'draft_ids':[],'followups':[]})
        r=self.run_agent(call('market_hotspots',{}),answer)
        self.assertFalse(r.get('isFallback'));self.assertEqual(r['agent_runtime']['contract_errors'],[])
        self.assertEqual(len(self.reviewer.packets),1)
        self.assertEqual(self.reviewer.packets[0]['draft']['claims'][0]['text'], '该发布说明了贷款贴息的适用条件。')
        self.assertEqual(self.reviewer.packets[0]['amount_contract']['amount_occurrence_count'], 0)

    def test_projection_preserves_raw_dates_quotes_and_limitations_without_mutating_store(self):
        before=self.g.market.hotspots();view=market_agent_view('market_hotspots',before)
        self.assertEqual(before,self.g.market.hotspots())
        self.assertEqual(view['as_of'],before['as_of']);self.assertIn('queried_at',view)
        self.assertEqual(view['window_days'],7);self.assertEqual(view['events'][0]['facts'],before['events'][0]['facts'])
        self.assertNotIn('review',view['events'][0]);self.assertNotIn('sources',view['coverage'])
        self.assertEqual(view['coverage']['limitations'],before['coverage']['limitations'])
        self.assertEqual(view['events'][0]['articles'][0]['url'],before['events'][0]['articles'][0]['url'])

    def test_fabricated_policy_amount_stops_loop_and_audits_actual_reason(self):
        r=self.rejected_money('政策贷款上限200万元。')
        self.assertEqual(r['error']['code'],'AGENT_OUTPUT_INVALID');self.assertEqual(r['market_sources'],[])
        self.assertEqual(r['agent_runtime']['contract_errors'],['METRIC_AMOUNT_UNBOUND']*3)
        self.assertFalse(self.reviewer.packets);self.assertNotIn('200万元',r['reply'])
        with closing(self.g.bank.connect()) as db:
            details=json.loads(db.execute("SELECT details FROM audit WHERE event='agent_turn_completed'").fetchone()[0])
        self.assertEqual(details['contract_errors'],['METRIC_AMOUNT_UNBOUND']*3)

    def test_public_analysis_review_reason_and_metadata_do_not_prove_amounts(self):
        t=BankTools(self.g,self.w,'test');o=t.invoke('market_hotspots',{})
        values={f['value'] for f in o['facts']}
        self.assertNotIn(900000000,values);self.assertNotIn(10000000,values)
        self.g.market.article_data['read_scope']='metadata_only'
        self.assertFalse(BankTools(self.g,self.w,'metadata').invoke('market_hotspots',{})['facts'])
        self.assertFalse(BankTools(self.g,self.w,'metadata-article').invoke('market_article',{'article_id':'a_policy'})['facts'])

    def test_withdrawn_or_unverified_article_does_not_prove_amounts(self):
        for field,value in [('status','withdrawn'),('link_status','unverified')]:
            self.g.market.article_data[field]=value
            self.assertFalse(BankTools(self.g,self.w,field).invoke('market_hotspots',{})['facts'])
            self.g.market.article_data[field]='active' if field=='status' else 'verified'
        self.g.market.event_data['status']='source_revised'
        self.assertFalse(BankTools(self.g,self.w,'revised').invoke('market_hotspots',{})['facts'])

    def test_public_amount_cannot_bypass_review_via_fact_answer(self):
        r=self.run_agent(call('market_hotspots',{}),lambda m,t:call('answer_facts',{'fact_ids':[last_tool(m)['facts'][0]['id']]}),
            lambda m,t:public_quote_reply(m,t,values={100000000}))
        self.assertFalse(r.get('isFallback'));self.assertEqual(r['agent_review']['status'],'approved')
        self.assertIn('REVIEW_REQUIRED',[x.get('code') for x in r['agent_runtime']['trace']])

    def test_public_money_misrepresented_as_balance_is_locally_rejected_before_review(self):
        r=self.rejected_money('你账户余额100万元。')
        self.assertEqual(r['error']['code'],'AGENT_OUTPUT_INVALID')
        self.assertEqual(r['bank_actions'],[]);self.assertNotIn('余额100万元',r['reply'])
        self.assertEqual(r['agent_runtime']['contract_errors'], ['METRIC_AMOUNT_UNBOUND'] * 3)
        self.assertFalse(self.reviewer.packets)

    def test_currency_swap_and_uncited_public_amount_are_rejected(self):
        r=self.rejected_money('贷款上限100万美元。');self.assertEqual(r['error']['code'],'AGENT_OUTPUT_INVALID')
        t=BankTools(self.g,self.w,'citations');t.invoke('market_hotspots',{});s=t.invoke('load_skill',{'name':'market'})
        self.assertEqual(t.public_money_for_claims([{'text':'贷款上限100万元。','evidence_ids':[s['evidence_id']]}]),set())

    def test_weekly_has_current_source_event_quotes_for_public_amount_review(self):
        t=BankTools(self.g,self.w,'weekly');o=t.invoke('market_weekly',{})
        self.assertIn('supporting_events',o['data'])
        self.assertEqual({f['value'] for f in o['facts']},{100000000,140000000000000})


if __name__=='__main__':unittest.main()
