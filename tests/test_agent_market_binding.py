"""隔离公共市场库的历史身份、元信息与最终版本闸门；不调用云模型。"""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from agent_runtime.bank_tools import BankTools, native_tools
from finance_gateway import FinanceGateway
from market_content import MarketStore, CN
from market_daily import DailyMarket
from simulated_bank import BankError


class MarketBindingTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode()})
        self.env.start();self.addCleanup(self.env.stop)
        os.environ.pop('NAVREN_KNOWLEDGE_DB',None)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.g=FinanceGateway(base/'bank.sqlite3');self.w,self.token=self.g.create_session()
        self.store=MarketStore(base/'market.sqlite3');self.g.market=self.store
        self.now=datetime.now(CN);self.day=self.now.date().isoformat()
        published=(self.now-timedelta(hours=1)).isoformat()
        self.article={'id':'a_binding','source_id':'nbs','publisher':'国家统计局',
            'origin_publisher':'国家统计局','title':'隔离测试原文',
            'url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261006_999.html',
            'published_at':published,'published_precision':'minute','checked_at':self.now.isoformat(),
            'read_scope':'full_text','link_status':'verified','evidence_text':'隔离测试：政策金额5000元。',
            'identity':'official_release','fingerprint':'article-1'}
        self.store.save_article(self.article)
        self.event={'id':'e_binding','headline':'隔离测试政策事件','summary':'归档原摘要。',
            'analysis':'归档原分析。','uncertainty':'归档条件。',
            'facts':[{'text':'政策金额5000元。','quote':'政策金额5000元。','article_id':'a_binding'}],
            'article_ids':['a_binding'],'article_versions':{'a_binding':1},
            'primary_article_id':'a_binding','published_at':published,'category':'macro_policy',
            'review':{'status':'approved'},'score':1,'ranking_reason':{'importance':1,'new_fact':1}}
        self.store.publish('market_events','e_binding',self.event,'event-1')
        self.store.update_daily(now=self.now,run_id='isolated-agent',valid_check=True)
        self.parameters={'edition_date':self.day,'revision':1,'event_version':1}
        self.reference={'kind':'event','id':'e_binding','source_scope':'market','version':1,
                        'parameters':copy.deepcopy(self.parameters),'execution_authority':False}
        self.t=BankTools(self.g,self.w,'isolated-market-binding')

    def historical_args(self,**changes):
        return {'event_id':'e_binding',**self.parameters,**changes}

    def assert_stale(self,fn):
        with self.assertRaises(BankError) as error:fn()
        self.assertEqual(error.exception.code,'STALE_MARKET_EVIDENCE')
        self.assertEqual(error.exception.status,409)

    def publish_weekly(self,fingerprint='weekly-1',summary='周报原摘要。'):
        body={'period':self.day,'title':'隔离周报','summary':summary,'analysis':'周报分析。',
              'article_ids':['a_binding'],'citations':[{'event_id':'e_binding'}]}
        self.store.publish('market_weeklies',self.day,body,fingerprint)

    def new_edition(self):
        event=self.store.event('e_binding')
        short={'id':'e_binding','event_version':event['version'],
            'article_versions':event['article_versions'],'content_version':event['content_version'],
            'display_title':'新版精选标题','display_summary':'新版精选摘要','display_version':'display-2'}
        with patch.object(DailyMarket,'display_pack',return_value={'e_binding':short}):
            result=self.store.update_daily(now=self.now,run_id='changed-display',valid_check=True)
        self.assertEqual(result['revision'],2)

    def test_schema_and_all_or_none_history_arguments(self):
        schema=next(t['function']['parameters'] for t in native_tools()
                    if t['function']['name']=='market_event')
        self.assertTrue({'edition_date','revision','event_version'}<=schema['properties'].keys())
        for partial in ({'edition_date':self.day},{'revision':1},{'event_version':1},
                        {'edition_date':self.day,'revision':1}):
            with self.subTest(partial=partial),self.assertRaises(BankError) as error:
                self.t.invoke('market_event',{'event_id':'e_binding',**partial})
            self.assertEqual(error.exception.status,400)
        for changes in ({'revision':True},{'edition_date':'2026-02-30'}):
            with self.subTest(changes=changes),self.assertRaises(BankError) as error:
                self.t.invoke('market_event',self.historical_args(**changes))
            self.assertEqual(error.exception.status,400)
        self.assertFalse(self.t.evidence)

    def test_explicit_old_event_never_calls_latest_and_exposes_metadata_only(self):
        self.store.publish('market_events','e_binding',{**self.event,'summary':'最新秘密摘要。'},'event-2')
        with patch.object(self.store,'event',side_effect=AssertionError('历史事件不能读取 latest')):
            out=self.t.invoke('market_event',self.historical_args())
        self.assertEqual(out['data']['version'],1)
        self.assertEqual(out['data']['archive']['revision'],1)
        self.assertEqual(out['data']['availability'],'revised')
        self.assertEqual(out['data']['latest_pointer']['version'],2)
        self.assertFalse(out['facts']);self.assertEqual(out['data']['facts'],[])
        text=json.dumps(out,ensure_ascii=False)
        for stale in ('归档原摘要','归档原分析','最新秘密摘要','政策金额5000元'):
            self.assertNotIn(stale,text)
        self.assertTrue(all('evidence_excerpt' not in a for a in self.t.market_sources.values()))

    def test_bound_omission_uses_original_edition_not_current_edition(self):
        self.t.bind_market_context(self.reference)
        self.new_edition()
        self.assertEqual(self.store.hotspots()['revision'],2)
        out=self.t.invoke('market_event',{'event_id':'e_binding'})
        self.assertEqual(out['data']['archive']['revision'],1)
        self.assertEqual(out['data']['display_title'],'隔离测试政策事件')
        self.assertNotIn('新版精选',json.dumps(out,ensure_ascii=False))
        self.assertEqual(self.t.market_snapshot()['reads'][0]['arguments'],self.historical_args())
        self.t.fresh()  # 原归档不可变；无关的新版精选不使原版事实自动失效。

    def test_bound_identity_conflicts_and_rebinding_are_rejected(self):
        self.t.bind_market_context(self.reference)
        for args in (self.historical_args(revision=2),self.historical_args(event_version=2),
                     self.historical_args(edition_date=(self.now-timedelta(days=1)).date().isoformat()),
                     {'event_id':'another-event'}):
            with self.subTest(args=args),self.assertRaises(BankError) as error:
                self.t.invoke('market_event',args)
            self.assertEqual(error.exception.code,'MARKET_CONTEXT_CONFLICT')
            self.assertEqual(error.exception.status,409)
        self.new_edition()
        with self.assertRaises(BankError) as error:
            self.t.bind_market_context({**self.reference,'parameters':{**self.parameters,'revision':2}})
        self.assertEqual(error.exception.code,'MARKET_CONTEXT_CONFLICT')

    def test_binding_and_snapshot_do_not_alias_caller_dictionaries(self):
        ref=copy.deepcopy(self.reference);self.t.bind_market_context(ref)
        ref['parameters']['revision']=99
        snap=self.t.market_snapshot();snap['reads'][0]['arguments']['revision']=99
        self.assertEqual(self.t.market_context['parameters']['revision'],1)
        self.assertEqual(self.t.market_snapshot()['reads'][0]['arguments']['revision'],1)
        self.t.fresh()

    def test_legacy_reference_and_unbound_event_remain_compatible(self):
        self.t.bind_market_context({'kind':'event','id':'e_binding'})
        out=self.t.invoke('market_event',{'event_id':'e_binding'})
        self.assertEqual(out['data']['version'],1)
        self.assertNotIn('archive',out['data'])
        self.assertTrue(out['facts']);self.t.fresh()

    def test_history_errors_keep_market_status_and_do_not_fallback_latest(self):
        for args,status in ((self.historical_args(event_id='unknown'),404),
                            (self.historical_args(event_version=2),409)):
            with self.subTest(args=args),self.assertRaises(BankError) as error:
                self.t.invoke('market_event',args)
            self.assertEqual(error.exception.status,status)
        self.assertFalse(self.t.evidence)

    def test_withdrawn_archive_and_live_event_contain_no_old_body_or_facts(self):
        self.store.withdraw_article('a_binding')
        for args in (self.historical_args(),{'event_id':'e_binding'}):
            tool=BankTools(self.g,self.w,'withdrawn-'+str(len(args)))
            out=tool.invoke('market_event',args)
            self.assertFalse(out['facts']);self.assertEqual(out['data']['facts'],[])
            self.assertNotIn('归档原分析',json.dumps(out,ensure_ascii=False))
            self.assertNotIn('政策金额5000元',json.dumps(out,ensure_ascii=False))
            tool.fresh()  # 可以解释稳定的撤回元信息，不能复述旧事实。

    def test_final_fresh_rejects_withdrawal_after_historical_read(self):
        self.t.bind_market_context(self.reference)
        self.t.invoke('market_event',{'event_id':'e_binding'})
        self.store.withdraw_article('a_binding')
        self.assert_stale(self.t.fresh)
        self.assert_stale(lambda:BankTools.assert_market_snapshot(self.store,self.t.market_snapshot()))

    def test_final_fresh_rejects_source_revision_after_event_read(self):
        self.t.invoke('market_event',self.historical_args())
        self.store.save_article({**self.article,'fingerprint':'article-2','evidence_text':'更正后的原文。'})
        self.assert_stale(self.t.fresh)

    def test_final_fresh_rejects_event_revision_even_without_source_revision(self):
        self.t.invoke('market_event',{'event_id':'e_binding'})
        self.store.publish('market_events','e_binding',{**self.event,'summary':'修订摘要。'},'event-2')
        self.assert_stale(self.t.fresh)

    def test_final_fresh_rejects_article_revision(self):
        self.t.invoke('market_article',{'article_id':'a_binding'})
        self.store.save_article({**self.article,'fingerprint':'article-2','evidence_text':'更正后的原文。'})
        self.assert_stale(self.t.fresh)

    def test_final_fresh_rejects_weekly_revision_and_support_event_revision(self):
        self.publish_weekly();self.t.invoke('market_weekly',{'period':self.day})
        self.publish_weekly('weekly-2','周报新摘要。');self.assert_stale(self.t.fresh)
        tool=BankTools(self.g,self.w,'weekly-event-revision')
        tool.invoke('market_weekly',{'period':self.day})
        self.store.publish('market_events','e_binding',{**self.event,'summary':'事件修订。'},'event-2')
        self.assert_stale(tool.fresh)

    def test_invalidated_weekly_has_no_old_analysis_or_supporting_facts(self):
        self.publish_weekly();self.store.withdraw_article('a_binding')
        out=self.t.invoke('market_weekly',{'period':self.day})
        self.assertFalse(out['facts']);self.assertEqual(out['data']['supporting_events'],[])
        self.assertNotIn('周报原摘要',json.dumps(out,ensure_ascii=False))
        self.assertNotIn('周报分析',json.dumps(out,ensure_ascii=False))

    def test_final_fresh_rejects_new_hotspot_edition(self):
        self.t.invoke('market_hotspots',{});self.new_edition();self.assert_stale(self.t.fresh)

    def test_unchanged_source_recheck_does_not_invalidate_facts(self):
        self.t.invoke('market_article',{'article_id':'a_binding'})
        self.store.save_article({**self.article,'checked_at':(self.now+timedelta(seconds=1)).isoformat()})
        self.t.fresh()
        snap=json.loads(json.dumps(self.t.market_snapshot()))
        self.assertEqual(set(snap),{'schema_version','provider_id','context','reads'})
        self.assertEqual(set(snap['reads'][0]),{'tool','arguments','source_token','sha256'})
        self.assertNotIn(str(self.store.path),json.dumps(snap))
        BankTools.assert_market_snapshot(self.store,snap)

    def test_final_gate_service_failure_remains_503(self):
        self.t.invoke('market_article',{'article_id':'a_binding'})
        with patch.object(self.store,'article',side_effect=sqlite3.OperationalError('isolated failure')):
            with self.assertRaises(BankError) as error:self.t.fresh()
        self.assertEqual(error.exception.code,'MARKET_UNAVAILABLE');self.assertEqual(error.exception.status,503)

    def test_authoritative_page_ticket_cannot_be_consumed_in_other_workspace(self):
        ticket=self.g.page_contexts.create(self.w,self.token,{'question':'解释这条历史事件。',
            'reference':{k:v for k,v in self.reference.items() if k!='execution_authority'},
            'return_view':{'page':'account','view':'market'}})
        other,token=self.g.create_session()
        with self.assertRaises(BankError) as error:
            self.g.page_contexts.consume(other,token,ticket['context_id'],'解释这条历史事件。')
        self.assertEqual(error.exception.code,'PAGE_CONTEXT_NOT_FOUND')
        reference=self.g.page_contexts.consume(self.w,self.token,ticket['context_id'],'解释这条历史事件。')
        self.assertEqual(self.t.bind_market_context(reference)['parameters'],self.parameters)


if __name__=='__main__':unittest.main()
