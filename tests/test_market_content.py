import copy,json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from market_sources import REGISTRY,safe_url,parse_date,discover,read_article,cluster_key
from market_content import MarketStore,week_periods,observation_change,CN
from market_editor import MarketEditor,numbers
from market_pipeline import refresh,due

class Client:
    model_id='test-independent'
    def __init__(self,result):self.result=result
    def complete_json(self,**kwargs):return copy.deepcopy(self.result)

class MarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=MarketStore(Path(self.tmp.name)/'market.sqlite3')
        self.a={'id':'a_test','source_id':'nbs','publisher':'国家统计局','title':'2026年9月制造业PMI','title_cn':'2026年9月制造业PMI','url':'https://www.stats.gov.cn/sj/zxfb/202609/t20260930_1965449.html',
            'published_at':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),'original_timezone':'Asia/Shanghai','content_updated_at':None,
            'fetched_at':datetime.now(timezone.utc).isoformat(),'checked_at':datetime.now(timezone.utc).isoformat(),'read_scope':'full_text','link_status':'verified',
            'fingerprint':'f1','evidence_text':'制造业PMI为50.1%，比上月上升0.5个百分点。','evidence_truncated':False,'identity':'official_release','version':1}
        self.e={'id':'e_test','headline':'制造业PMI为50.1%','summary':'制造业PMI为50.1%，比上月上升0.5个百分点。','analysis':'景气改善可能影响融资需求，仍取决于订单持续性。','uncertainty':'单月指标不能证明持续复苏。','facts':[{'text':'制造业PMI为50.1%。','article_id':'a_test','quote':'制造业PMI为50.1%'}]}
    def test_live_market_editor_defaults_use_aiping_for_both_models(self):
        editor=MarketEditor()
        self.assertEqual(editor.primary.provider,'ai_ping')
        self.assertEqual(editor.primary.model_id,'DeepSeek-V4-Flash')
        self.assertEqual(editor.primary.env_name,'AIPING_API_KEY')
        self.assertEqual(editor.reviewer.provider,'ai_ping')
        self.assertEqual(editor.reviewer.env_name,'AIPING_API_KEY')
    def editor(self,e=None,verdict='approve'):return MarketEditor(Client({'events':[e or self.e]}),Client({'verdict':verdict,'reason':'证据核对通过。','issues':[]}))
    def valid_event(self):return self.editor().events([{'id':'e_test','articles':[self.a]}],'test')[0]
    def test_url_script_rejected(self):
        for u in ['javascript:alert(1)','https://evil.test/test.html','https://www.stats.gov.cn/','https://www.stats.gov.cn@evil.test/test.html','https://www.stats.gov.cn:1234/test.html']:
            with self.assertRaises(ValueError):safe_url(REGISTRY['nbs'],u)
    def test_unregistered_feed_rejected(self):
        with self.assertRaises(ValueError):safe_url(REGISTRY['nbs'],'https://www.stats.gov.cn/other.xml',False)
    def test_redirect_guard(self):
        from market_sources import GuardedRedirect
        with self.assertRaises(ValueError):GuardedRedirect(REGISTRY['nbs'],True).redirect_request(None,None,302,'',{},'https://evil.test/a.html')
    def test_chinese_slash_date_and_timezone(self):self.assertEqual(parse_date('2026/09/30 09:30','Asia/Shanghai'),'2026-09-30T01:30:00+00:00')
    def test_rfc_timezone(self):self.assertEqual(parse_date('Wed, 30 Sep 2026 15:00:00 +0200'),'2026-09-30T13:00:00+00:00')
    def test_future_date_rejected(self):
        with self.assertRaises(ValueError):parse_date((datetime.now(timezone.utc)+timedelta(days=2)).isoformat())
    def test_truncated_rss_rejected(self):
        with patch('market_sources.fetch',return_value=(b'<rss><channel><item>',REGISTRY['nbs'].url,'application/xml')):
            with self.assertRaises(ValueError):discover(REGISTRY['nbs'])
    def test_200_login_rejected(self):
        with patch('market_sources.fetch',return_value=(b'<html><title>Login</title><form>sign in</form></html>',self.a['url'],'text/html')):
            with self.assertRaises(ValueError):read_article(REGISTRY['nbs'],{'url':self.a['url'],'title':'制造业PMI','published_at':self.a['published_at']})
    def test_original_date_not_fetch_date(self):
        self.s.save_article(self.a);b={**self.a,'checked_at':datetime.now(timezone.utc).isoformat()};self.assertFalse(self.s.save_article(b))
        self.assertEqual(self.s.article('a_test')['published_at'],self.a['published_at']);self.assertEqual(self.s.article('a_test')['version'],1)
    def test_event_dedup_by_period(self):
        b={**self.a,'source_id':'nbs_analysis','title':'统计局解读采购经理指数'};self.assertEqual(cluster_key(self.a),cluster_key(b))
    def test_public_body_not_exposed(self):
        self.s.save_article(self.a);a=self.s.article('a_test');self.assertNotIn('evidence_text',a);self.assertNotIn('fingerprint',a);self.assertTrue(a['evidence_excerpt'])
    def test_official_claim_approved(self):self.assertEqual(self.valid_event()['review']['status'],'approved')
    def test_forged_quote_rejected(self):
        e=copy.deepcopy(self.e);e['facts'][0]['quote']='不存在的证明'
        with self.assertRaises(ValueError):self.editor(e).events([{'id':'e_test','articles':[self.a]}],'test')
    def test_english_quote_case_restored_from_actual_source(self):
        e=copy.deepcopy(self.e);e['facts'][0]['quote']='manufacturing PMI was 50.1%'
        a={**self.a,'evidence_text':'Manufacturing PMI was 50.1%, up 0.5 percentage points.'}
        result=self.editor(e).events([{'id':'e_test','articles':[a]}],'test')[0]
        self.assertEqual(result['facts'][0]['quote'],'Manufacturing PMI was 50.1%')
        self.assertIn(result['facts'][0]['quote'],a['evidence_text'])
    def test_quote_content_and_punctuation_not_repaired(self):
        e=copy.deepcopy(self.e);e['facts'][0]['quote']='manufacturing PMI was 50,1%'
        a={**self.a,'evidence_text':'Manufacturing PMI was 50.1%, up 0.5 percentage points.'}
        with self.assertRaises(ValueError):self.editor(e).events([{'id':'e_test','articles':[a]}],'test')
    def test_wrong_number_rejected(self):
        e=copy.deepcopy(self.e);e['facts'][0]['text']='制造业PMI为90.9%。'
        with self.assertRaises(ValueError):self.editor(e).events([{'id':'e_test','articles':[self.a]}],'test')
    def test_english_month_normalization(self):self.assertTrue({'7','11'}<=numbers('23 July and 30 November 2026'))
    def test_magnitude_conversion_by_code(self):self.assertIn('996',numbers('9.96 million people'))
    def test_metadata_not_full_text(self):
        with self.assertRaises(ValueError):self.editor().events([{'id':'e_test','articles':[{**self.a,'read_scope':'metadata_only'}]}],'test')
    def test_independent_rejection(self):
        with self.assertRaises(ValueError):self.editor(verdict='reject').events([{'id':'e_test','articles':[self.a]}],'test')
    def test_unknown_evidence_rejected(self):
        e=copy.deepcopy(self.e);e['facts'][0]['article_id']='fake'
        with self.assertRaises(ValueError):self.editor(e).events([{'id':'e_test','articles':[self.a]}],'test')
    def test_source_revision_invalidates_content(self):
        self.s.save_article(self.a);e=self.valid_event();self.s.publish('market_events','e_test',e,'h1')
        self.s.update_daily(run_id='valid-before-revision',valid_check=True)
        self.s.save_article({**self.a,'fingerprint':'f2','evidence_text':'已更正的发布内容。'})
        self.assertEqual(self.s.event('e_test')['status'],'source_revised');archived=self.s.hotspots()['events']
        self.assertEqual(len(archived),1);self.assertEqual(archived[0]['availability'],'revised');self.assertEqual(archived[0]['summary'],'');self.assertEqual(self.s.article('a_test')['version'],2)
    def test_withdrawn_bookmark_retains_status(self):
        self.s.save_article(self.a);self.s.withdraw_article('a_test');self.assertEqual(self.s.article('a_test')['status'],'withdrawn')
    def test_old_article_not_hotspot(self):
        self.a['published_at']=(datetime.now(timezone.utc)-timedelta(days=20)).isoformat()
        self.s.save_article(self.a);e=self.valid_event();self.s.publish('market_events','e_test',e,'h1')
        self.s.update_daily(run_id='verify-old-only',valid_check=True)
        self.assertEqual(self.s.hotspots()['events'],[])
    def test_failure_retains_last_good(self):
        self.s.save_article(self.a);self.s.publish('market_events','e_test',self.valid_event(),'h1')
        self.s.update_daily(run_id='valid-before-failure',valid_check=True)
        with patch('market_pipeline.discover',side_effect=TimeoutError):r=refresh(self.s,ingest_only=True,total_seconds=2)
        self.assertEqual(r['status'],'failed');self.assertEqual(len(self.s.hotspots()['events']),1)
    def test_week_cutoff(self):
        a,b=week_periods(1,datetime(2026,10,5,8,30,tzinfo=CN))[0];self.assertEqual((a.date().isoformat(),b.date().isoformat()),('2026-09-28','2026-10-05'))
    def test_current_period_not_generated_as_complete(self):
        a,b=week_periods(1)[0]
        with self.assertRaises(ValueError):self.editor().weekly([self.valid_event()],b,b+timedelta(days=7),'test')
    def test_wrong_rate_unit(self):
        with self.assertRaises(ValueError):observation_change('2','1.75','rate','CNY','policy_rate')
    def test_basis_points(self):self.assertEqual(observation_change('2','1.75','rate','percent','policy_rate')['value'],'-25.00')
    def test_price_change(self):self.assertEqual(observation_change('100','102','price','CNY','same_series')['value'],'2.00')
    def test_missing_metric_not_zero(self):
        with self.assertRaises(ValueError):observation_change(None,102,'price','CNY','same_series')
    def test_republish_same_evidence_no_new_version(self):
        e=self.valid_event();self.assertTrue(self.s.publish('market_events','e_test',e,'h1'));self.assertFalse(self.s.publish('market_events','e_test',e,'h1'))
    def test_no_good_report_not_old_static(self):self.assertEqual(self.s.weekly()['status'],'no_verified_weekly')
    def test_schedule_due_boundary(self):
        from market_schedule import plan
        self.assertEqual(plan(self.s,datetime(2026,10,5,8,29,tzinfo=CN))['targets']['weekly'],'2026-09-21')
        self.assertEqual(plan(self.s,datetime(2026,10,5,8,30,tzinfo=CN))['targets']['weekly'],'2026-09-28')

    def test_old_published_dependency_is_rechecked_after_feed_exit(self):
        import urllib.error
        self.s.save_article(self.a);self.s.publish('market_events','e_test',self.valid_event(),'h1')
        self.s.update_daily(run_id='valid-before-withdrawal',valid_check=True)
        def discovery(source,*args):return [],{'source_id':source.id,'status':'ok','checked_at':self.a['checked_at']}
        with patch('market_pipeline.discover',side_effect=discovery),patch('market_pipeline.read_article',side_effect=urllib.error.HTTPError(self.a['url'],404,'Gone',None,None)) as read:
            refresh(self.s,ingest_only=True,force_check=True,total_seconds=5)
        self.assertEqual(read.call_count,1);self.assertEqual(self.s.article('a_test')['status'],'withdrawn')
        self.assertEqual(self.s.hotspots()['events'][0]['availability'],'withdrawn');self.assertEqual(self.s.hotspots()['original_count'],1)
    def test_weekly_source_excerpt_survives_public_projection(self):
        self.s.save_article(self.a)
        self.s.publish('market_weeklies','2026-09-28',{'period':'2026-09-28','title':'测试','article_ids':['a_test']},'h1')
        self.assertEqual(self.s.weekly()['report']['source_articles'][0]['evidence_excerpt'],self.a['evidence_text'])

    def test_cache_recheck_keeps_date_precision_and_fingerprint(self):
        body='<html><head><meta name="PubDate" content="2026-10-04"></head><body><h1>原始统计发布标题</h1><div class="TRS_Editor">'+('真实原始统计资料。'*30)+'</div></body></html>'
        with patch('market_sources.fetch',return_value=(body.encode(),self.a['url'],'text/html')):
            first=read_article(REGISTRY['nbs'],{'url':self.a['url'],'title':'标题'})
            again=read_article(REGISTRY['nbs'],first)
        self.assertEqual(first['published_precision'],'date');self.assertEqual(again['published_precision'],'date')
        self.assertEqual(first['fingerprint'],again['fingerprint'])

if __name__=='__main__':unittest.main()
