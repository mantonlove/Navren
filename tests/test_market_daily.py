import copy, json, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from market_content import MarketStore,CN
from market_daily import DailyMarket,MarketDailyError,classify

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MarketStore(Path(self.tmp.name)/'public.sqlite3')
        self.daily=DailyMarket(self.store);self.now=datetime(2026,10,6,12,30,tzinfo=CN)
    def add(self,i,published=None,category='macro_policy',importance=1,extra=None):
        when=(published or self.now-timedelta(hours=i)).isoformat();id='a_'+str(i)
        article={'id':id,'source_id':'nbs','publisher':'国家统计局','origin_publisher':'国家统计局',
            'title':'真实统计发布 '+str(i),'url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261006_'+str(100+i)+'.html',
            'published_at':when,'published_precision':'minute','checked_at':self.now.isoformat(),
            'read_scope':'full_text','link_status':'verified','evidence_text':'真实统计资料，所有事实均来自已审核的原文。',
            'identity':'official_release','fingerprint':'f'+str(i),'version':1}
        self.store.save_article(article)
        event={'id':'e_'+str(i),'headline':'已审核统计事件 '+str(i),'summary':'已审核摘要。','analysis':'经审核的传导条件。','uncertainty':'延续影响仍取决于执行条件。',
            'facts':[{'text':'真实统计资料','article_id':id,'quote':'真实统计资料'}],
            'article_ids':[id],'article_versions':{id:1},'primary_article_id':id,'published_at':when,
            'category':category,'review':{'status':'approved'},'score':importance,
            'ranking_reason':{'importance':importance,'new_fact':.8}}
        event.update(extra or {})
        self.store.publish('market_events',event['id'],event,'e'+str(i));return article,event
    def update(self,**kw):return self.store.update_daily(run_id='check',valid_check=True,now=self.now,**kw)
    def day(self,category='all'):return self.store.history_day(self.now.date().isoformat(),category,now=self.now)
    def test_get_is_read_only_until_valid_check(self):
        self.add(1);self.assertEqual(self.store.hotspots(now=self.now)['status'],'missing_archive')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM market_daily_editions').fetchone()[0],0)
    def test_nine_candidates_max_six_distinct_and_deterministic(self):
        for i in range(9):self.add(i)
        a=self.update();self.assertEqual(a['count'],6)
        first=self.day();ids=[e['id'] for e in first['events']]
        self.assertEqual(len(set(ids)),6);self.assertFalse(self.update()['changed'])
        self.assertEqual(ids,[e['id'] for e in self.store.hotspots(now=self.now)['events']])
    def test_actual_counts_five_two_one_zero(self):
        for count in (0,1,2,5):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as tmp:
                old=self.store;self.store=MarketStore(Path(tmp)/'market.db')
                for i in range(count):self.add(i)
                self.update();self.assertEqual(self.day()['total_count'],count)
                self.assertEqual(len(self.day()['events']),count);self.store=old
    def test_next_day_same_fingerprint_creates_actual_day_and_seals_previous(self):
        self.add(1);self.update();self.now+=timedelta(days=1)
        result=self.update();self.assertTrue(result['changed']);self.assertEqual(result['revision'],1)
        self.assertEqual(self.store.history_day('2026-10-06',now=self.now)['period_state'],'sealed')
    def test_failed_or_partial_zero_check_does_not_invent_archive(self):
        self.store.update_daily(run_id='failed',valid_check=False,now=self.now)
        self.update(coverage={'limited':True})
        self.assertEqual(self.day()['archive_state'],'missing')
        self.update(coverage={'limited':False});self.assertEqual(self.day()['archive_state'],'no_qualified')
    def test_partial_verified_candidates_can_publish_coverage_limits(self):
        self.add(1);self.update(coverage={'limited':True,'sources':[{'status':'failed'}]})
        self.assertEqual(self.day()['total_count'],1);self.assertTrue(self.day()['selection_coverage']['limited'])
    def test_fail_after_success_keeps_previous_edition(self):
        self.add(1);self.update();self.now+=timedelta(hours=2)
        self.store.update_daily(run_id='fail',valid_check=False,now=self.now)
        self.assertEqual(self.day()['revision'],1);self.assertEqual(self.day()['total_count'],1)
    def test_idempotent_selection_updates_check_time_not_selected_time(self):
        self.add(1);self.update();old=self.day()['selected_at'];self.now+=timedelta(hours=1)
        self.update();data=self.day();self.assertEqual(data['selected_at'],old)
        self.assertEqual(data['last_success_at'],self.now.isoformat())
    def test_distinct_event_rows_with_same_primary_group_do_not_duplicate(self):
        _,event=self.add(1)
        self.store.publish('market_events','another',dict(event,id='another'),'another')
        self.update();self.assertEqual(self.day()['total_count'],1)
    def test_announcement_and_republication_with_reviewed_canonical_identity_share_slot(self):
        quote='2026年9月制造业采购经理指数为50.1%，较上月上升0.5个百分点。'
        self.add(1,extra={'canonical_event_id':'audited-pmi-2026-09','facts':[{'quote':quote,'text':quote,'article_id':'a_1'}]})
        self.add(2,extra={'canonical_event_id':'audited-pmi-2026-09','facts':[{'quote':quote,'text':quote,'article_id':'a_2'}]})
        self.update();row=self.day()['events'][0]
        self.assertEqual(self.day()['total_count'],1);self.assertEqual(len(row['related_event_refs']),1)
        self.assertTrue(row['selection_reason']['group_evidence'][0].startswith('canonical:'))
    def test_shared_effective_date_boilerplate_does_not_merge_different_policies(self):
        quote='新规定自2026年10月1日起施行，持续执行至另行公告为止。'
        self.add(1,extra={'facts':[{'quote':quote,'text':quote,'article_id':'a_1'}]})
        self.add(2,extra={'facts':[{'quote':quote,'text':quote,'article_id':'a_2'}]})
        self.update();self.assertEqual(self.day()['total_count'],2)
    def test_identity_alias_bridge_uses_transitive_groups(self):
        _,a=self.add(1,extra={'canonical_event_id':'one'})
        self.add(2,extra={'event_group_id':'two'})
        self.add(3,extra={'canonical_event_id':'one','event_group_id':'two'})
        self.update();self.assertEqual(self.day()['total_count'],1)
    def test_same_publisher_different_substantive_events_still_have_independent_slots(self):
        self.add(1);self.add(2);self.update();self.assertEqual(self.day()['total_count'],2)
    def test_same_origin_title_period_announcement_and_repost_share_slot(self):
        for i in (1,2):
            article,_=self.add(i)
            article['title']='居民养老金融权益更新'
            if i==2:article.update(source_id='cctv',publisher='央视网',origin_publisher='国家统计局',identity='media_report',access_status='free_full_text')
            with self.store.db() as db:db.execute('UPDATE market_articles SET body=? WHERE id=?',(json.dumps(article),'a_'+str(i)))
        self.update();self.assertEqual(self.day()['total_count'],1)
    def test_seventy_two_hour_and_seven_day_eligibility(self):
        self.add(1,self.now-timedelta(hours=71),importance=.5)
        self.add(2,self.now-timedelta(hours=80),importance=.5)
        self.add(3,self.now-timedelta(hours=80),importance=1)
        self.add(4,self.now-timedelta(days=8),importance=1)
        self.update();self.assertEqual({e['id'] for e in self.day()['events']},{'e_1','e_3'})
    def test_unreviewed_or_unbound_material_never_enters_selection(self):
        self.add(1,extra={'review':{'status':'rejected'}})
        self.add(2,extra={'article_versions':{'a_2':99}})
        self.update();self.assertEqual(self.day()['total_count'],0)
    def test_thirty_days_and_timezone_not_bank_clock(self):
        self.add(1);self.update();index=self.store.history_index(now=self.now)
        self.assertEqual(len(index['dates']),30);self.assertEqual(index['window_start'],'2026-09-07')
        self.assertEqual(index['server_today'],'2026-10-06')
        utc=self.now.astimezone(__import__('datetime').timezone.utc)
        self.assertEqual(self.store.history_index(now=utc)['server_today'],'2026-10-06')
    def test_date_and_type_intersection_retains_empty_matching_day(self):
        self.add(1,category='household');self.update()
        day=self.day('investment');self.assertEqual(day['total_count'],1);self.assertEqual(day['matched_count'],0)
        self.assertEqual(day['status'],'no_matching_category')
        row=self.store.history_index(category='investment',now=self.now)['dates'][-1]
        self.assertEqual(row['total_count'],1);self.assertEqual(row['matched_count'],0);self.assertEqual(row['archive_state'],'archived')
    def test_strict_query_dates_categories_revision(self):
        for path,query in [('/api/market/hotspots',{'bogus':['x']}),('/api/market/hotspots/history',{'category':['all','general']}),
            ('/api/market/hotspots/history',{'category':['fake']}),('/api/market/hotspots/history/2026-02-30',{}),
            ('/api/market/hotspots/history/2026-10-6',{}),
            ('/api/market/hotspots/history/2026-10-07',{}),('/api/market/hotspots/history/2026-09-06',{}),
            ('/api/market/hotspots/history/2026-10-06/events/e_1',{'revision':['0']}),
            ('/api/market/hotspots/history',{'from':['2026-10-06'],'to':['2026-10-05']})]:
            with self.subTest(path=path,query=query),self.assertRaises(MarketDailyError) as ctx:self.store.route(path,query,now=self.now)
            self.assertEqual(ctx.exception.status,400)
    def test_unknown_details_404_and_version_conflict(self):
        self.add(1);self.update()
        with self.assertRaises(MarketDailyError) as e:self.store.history_event('2026-10-06','unknown',1,now=self.now)
        self.assertEqual(e.exception.status,404)
        with self.assertRaises(MarketDailyError) as e:self.store.history_event('2026-10-06','e_1',1,now=self.now,expected_version=2)
        self.assertEqual(e.exception.status,409)
    def test_source_revision_removes_all_stale_body_and_retains_position(self):
        article,event=self.add(1);self.update()
        self.store.save_article({**article,'fingerprint':'new','evidence_text':'更正后的事实。'})
        old=self.store.history_event('2026-10-06','e_1',1,now=self.now)
        self.assertEqual(old['availability'],'revised');self.assertEqual(old['summary'],'');self.assertEqual(old['facts'],[])
        self.assertEqual(old['archive']['position'],1);self.assertEqual(old['articles'][0]['version'],1)
        self.assertNotIn('evidence_excerpt',old['articles'][0]);self.assertEqual(self.day()['readable_count'],0)
    def test_unavailable_english_short_summary_conditions_are_cleared_without_changing_snapshot(self):
        self.add(1)
        short={'id':'e_1','event_version':1,'article_versions':{'a_1':1},'content_version':'e1',
            'display_title':'审核标题','display_title_en':'Reviewed title','display_summary':'审核摘要',
            'display_summary_en':'Reviewed English summary','display_conditions_en':'Reviewed English condition','display_version':'short-en'}
        with patch.object(DailyMarket,'display_pack',return_value={'e_1':short}):self.update()
        self.store.withdraw_article('a_1')
        row=self.store.history_event('2026-10-06','e_1',1,now=self.now)
        self.assertEqual(row['display_summary_en'],'');self.assertEqual(row['display_conditions_en'],'')
        self.assertEqual(row['display_title_en'],'Reviewed title')
        with self.store.db() as db:
            snapshot=json.loads(db.execute('SELECT snapshot FROM market_daily_items').fetchone()[0])
        self.assertEqual(snapshot['display_summary_en'],'Reviewed English summary')
    def test_english_condition_change_revises_day_preserves_old_version_and_masks_both_after_withdrawal(self):
        self.add(1)
        short={'id':'e_1','event_version':1,'article_versions':{'a_1':1},'content_version':'e1',
            'display_title':'审核标题','display_summary':'审核摘要','display_conditions_en':'Condition A','display_version':'same-display-version'}
        with patch.object(DailyMarket,'display_pack',return_value={'e_1':short}):first=self.update()
        with patch.object(DailyMarket,'display_pack',return_value={'e_1':{**short,'display_conditions_en':'Condition B'}}):second=self.update()
        self.assertEqual(first['revision'],1);self.assertTrue(second['changed']);self.assertEqual(second['revision'],2)
        self.assertEqual(self.store.history_event('2026-10-06','e_1',1,now=self.now)['display_conditions_en'],'Condition A')
        self.assertEqual(self.store.history_event('2026-10-06','e_1',2,now=self.now)['display_conditions_en'],'Condition B')
        self.store.withdraw_article('a_1')
        for revision in (1,2):
            archived=self.store.history_event('2026-10-06','e_1',revision,now=self.now)
            self.assertEqual(archived['availability'],'withdrawn');self.assertEqual(archived['display_conditions_en'],'')
        with self.store.db() as db:
            stored=[json.loads(row[0])['display_conditions_en'] for row in db.execute('SELECT snapshot FROM market_daily_items ORDER BY revision')]
        self.assertEqual(stored,['Condition A','Condition B'])
    def test_default_date_chooses_last_readable_day_not_latest_withdrawn_records(self):
        self.add(1,self.now-timedelta(days=8));self.store.reconstruct_daily(now=self.now)
        self.add(2);self.update();self.store.withdraw_article('a_2')
        index=self.store.history_index(now=self.now)
        self.assertEqual(index['dates'][-1]['total_count'],1);self.assertEqual(index['dates'][-1]['readable_count'],0)
        self.assertEqual(index['default_date'],'2026-09-28')
    def test_event_new_version_pointer_without_implicit_latest(self):
        _,e=self.add(1);self.update();self.store.publish('market_events','e_1',{**e,'summary':'新版。'},'new')
        old=self.store.history_event('2026-10-06','e_1',1,now=self.now)
        self.assertEqual(old['version'],1);self.assertEqual(old['latest_pointer']['version'],2);self.assertEqual(old['summary'],'')
        self.update();current=self.day();self.assertEqual(current['revision'],2);self.assertEqual(current['events'][0]['version'],2)
        self.assertEqual(self.store.history_event('2026-10-06','e_1',1,now=self.now)['archive']['revision'],1)
    def test_withdrawal_never_replaces_or_reorders_archive(self):
        self.add(1);self.add(2);self.update();self.store.withdraw_article('a_1')
        data=self.day();self.assertEqual(data['original_count'],2);self.assertEqual(data['readable_count'],1)
        self.assertEqual(next(e for e in data['events'] if e['id']=='e_1')['availability'],'withdrawn')
    def test_temporary_access_failure_is_not_withdrawal(self):
        self.add(1);self.update()
        with self.store.db() as db:db.execute("UPDATE market_articles SET status='temporarily_unavailable' WHERE id='a_1'")
        self.assertEqual(self.day()['events'][0]['availability'],'temporarily_unavailable')
    def test_display_only_exact_reviewed_version_and_snapshot_immutable(self):
        self.add(1)
        short={'id':'e_1','event_version':1,'article_versions':{'a_1':1},'content_version':'e1','display_title':'审核短标题','display_summary':'审核短摘要','display_version':'short1','category':'fake','theme':'fake','version':999}
        with patch.object(DailyMarket,'display_pack',return_value={'e_1':short}):self.update()
        with patch.object(DailyMarket,'display_pack',return_value={}):self.assertEqual(self.day()['events'][0]['display_title'],'审核短标题')
        self.assertEqual(self.day()['events'][0]['version'],1);self.assertEqual(self.day()['events'][0]['category'],'macro_policy')
    def test_reconstruction_uses_primary_date_not_earliest_source(self):
        main,event=self.add(1,self.now-timedelta(days=2))
        early,_=self.add(2,self.now-timedelta(days=3))
        event.update(article_ids=['a_1','a_2'],article_versions={'a_1':1,'a_2':1},published_at=early['published_at'])
        self.store.publish('market_events','e_1',event,'with-earlier')
        result=self.store.reconstruct_daily(now=self.now);self.assertTrue(result['days'])
        data=self.store.history_day('2026-10-04',now=self.now)
        self.assertEqual(data['origin'],'reconstructed');self.assertIn('e_1',[e['id'] for e in data['events']])
        self.assertTrue(next(e for e in data['events'] if e['id']=='e_1')['after_revision_material'])
    def test_first_local_version_with_late_source_revision_is_post_revision_material(self):
        article,event=self.add(1,self.now-timedelta(days=2))
        # It is the first local capture, but the publisher revised after the archive date.
        article['content_updated_at']=self.now.isoformat()
        with self.store.db() as db:db.execute('UPDATE market_articles SET body=? WHERE id=?',(json.dumps(article),'a_1'))
        self.store.reconstruct_daily(now=self.now)
        row=self.store.history_event('2026-10-04','e_1',1,now=self.now)
        self.assertTrue(row['after_revision_material']);self.assertEqual(row['revision_confirmed_at'],self.now.isoformat())
    def test_late_supporting_material_not_reconstructed_as_known_earlier(self):
        main,event=self.add(1,self.now-timedelta(days=2));self.add(2,self.now-timedelta(days=1))
        event.update(article_ids=['a_1','a_2'],article_versions={'a_1':1,'a_2':1})
        self.store.publish('market_events','e_1',event,'late-source')
        self.store.reconstruct_daily(now=self.now)
        self.assertEqual(self.store.history_day('2026-10-04',now=self.now)['archive_state'],'missing')
    def test_reconstruction_does_not_overwrite_observed_or_fill_missing_days(self):
        self.add(1);self.update();self.store.reconstruct_daily(now=self.now)
        self.assertEqual(self.day()['origin'],'observed')
        days=self.store.history_index(now=self.now)['dates'];self.assertEqual(sum(x['archive_state']=='missing' for x in days),29)
    def test_binding_constraint_failure_rolls_back_header_and_items(self):
        self.add(1)
        original=self.daily.write_edition
        def broken(db,day,items,*args):
            items[0]['article_versions']={'a_1':77};return original(db,day,items,*args)
        with patch.object(self.daily,'write_edition',side_effect=broken),self.assertRaises(MarketDailyError):self.daily.update(now=self.now,run_id='broken',valid_check=True)
        self.assertEqual(self.day()['archive_state'],'missing')
    def test_classification_object_priority_no_publisher_inference(self):
        self.assertEqual(classify({'headline':'住房贷款贴息政策'}),'household')
        self.assertEqual(classify({'headline':'央行下调政策利率'}),'macro_policy')
        self.assertEqual(classify({'headline':'跨境汇率波动'}),'international')
        self.assertEqual(classify({'headline':'信息公布'},[{'title':'一般消息','publisher':'美联储'}]),'general')
        self.assertEqual(classify({'headline':'美联储发布一般消息'}),'general')
    def test_pipeline_updates_same_events_when_publication_fingerprint_unchanged(self):
        from market_sources import REGISTRY
        from market_pipeline import refresh
        from market_daily import DailyMarket
        self.add(1,published=datetime.now(CN)-timedelta(hours=1))
        class Editor:
            def events(*args):return []
            def weekly(*args):raise ValueError('no_report')
        def discovery(source,*args):return [],{'source_id':source.id,'status':'ok','checked_at':datetime.now(CN).isoformat()}
        with patch('market_pipeline.discover',side_effect=discovery),patch('market_pipeline.read_article',return_value=None):
            result=refresh(self.store,editor=Editor(),sources=[REGISTRY['nbs']],total_seconds=10)
        self.assertIn('daily_edition',result);self.assertEqual(self.store.hotspots()['total_count'],1)
    def test_hotspots_only_refresh_never_runs_weekly_editor(self):
        from market_sources import REGISTRY
        from market_pipeline import refresh
        from unittest.mock import Mock
        editor=Mock();editor.events.return_value=[]
        with patch('market_pipeline.discover',return_value=([],{'source_id':'nbs','status':'ok','checked_at':self.now.isoformat()})):
            result=refresh(self.store,editor=editor,sources=[REGISTRY['nbs']],total_seconds=10,hotspots_only=True)
        editor.weekly.assert_not_called();self.assertEqual(set(result['streams']),{'hotspots'})
    def test_access_denied_preserves_material_as_unavailable_not_withdrawn(self):
        import urllib.error
        from market_sources import REGISTRY
        from market_pipeline import refresh
        article,_=self.add(1)
        with patch('market_pipeline.discover',return_value=([],{'source_id':'nbs','status':'ok','checked_at':self.now.isoformat()})),patch('market_pipeline.read_article',side_effect=urllib.error.HTTPError(article['url'],403,'Denied',None,None)):
            refresh(self.store,editor=object(),sources=[REGISTRY['nbs']],total_seconds=2,ingest_only=True,force_check=True)
        self.assertEqual(self.store.article('a_1')['status'],'temporarily_unavailable')
        with self.store.db() as db:self.assertIn('真实统计资料',json.loads(db.execute('SELECT body FROM market_articles WHERE id=?',('a_1',)).fetchone()[0])['evidence_text'])

if __name__=='__main__':unittest.main()
