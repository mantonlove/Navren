"""Same-day policy through genuine generation binding, archive and public reads. No external calls."""
import copy,json,sqlite3,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from market_content import MarketStore,CN,event_input_hash
from market_daily import DailyMarket,MarketDailyError
from market_editor import MarketEditor

class FixedClient:
    model_id='test-same-day-no-network'
    def __init__(self,value):self.value=value
    def complete_json(self,**kwargs):return copy.deepcopy(self.value)

class SameDayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MarketStore(Path(self.tmp.name)/'market.sqlite3');self.daily=DailyMarket(self.store)
        self.now=datetime(2026,10,18,12,30,tzinfo=CN)
    def add(self,id,published):
        text='隔离验收资料，不是真实新闻。具体执行条件仍然持续有效。'
        article={'id':'article-'+id,'source_id':'nbs','publisher':'国家统计局','title':'隔离验收资料 '+id,
            'url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261018_1965449.html',
            'published_at':published.isoformat(),'published_precision':'minute','checked_at':self.now.isoformat(),
            'read_scope':'full_text','link_status':'verified','evidence_text':text,'evidence_truncated':False,
            'identity':'official_release','fingerprint':id,'version':1}
        self.store.save_article(article);article=next(a for a in self.store.articles() if a['id']==article['id'])
        draft={'id':id,'headline':'隔离验收资料 '+id,'summary':'隔离验收资料。','analysis':'执行仍取决于具体条件。',
            'uncertainty':'这不是实际市场发布。','facts':[{'text':'隔离验收资料','quote':'隔离验收资料','article_id':article['id']}]}
        captured=self.store.capture_event_generation([{'id':id,'articles':[article]}])
        editor=MarketEditor(FixedClient({'events':[draft]}),FixedClient({'verdict':'approve','reason':'隔离原文与草稿绑定通过。','issues':[]}))
        event=editor.events(captured['groups'],'same-day',generations=captured['generations'])[0]
        event['input_hash']=event_input_hash(captured['groups'][0]);self.store._record_event_review(event)
        self.store.publish('market_events',id,event,event['input_hash']);return event
    def update(self):return self.store.update_daily(run_id='same-day',valid_check=True,now=self.now)
    def test_today_only_not_rolling_hours_and_capped(self):
        for i in range(8):self.add('today'+chr(97+i),self.now-timedelta(minutes=i))
        self.add('yesterday',self.now-timedelta(days=1));self.add('old',self.now-timedelta(days=3))
        self.assertEqual(self.update()['count'],6)
        result=self.store.hotspots(now=self.now)
        self.assertEqual(len(result['events']),6)
        self.assertTrue(all(e['selection_reason']['same_day_publication'] for e in result['events']))
        self.assertFalse(self.update()['changed'])
    def test_future_primary_does_not_qualify(self):
        event=self.add('future',self.now+timedelta(minutes=1))
        with self.store.db() as db:self.assertIsNone(self.daily.selection_eligibility(db,event,self.now))
        self.assertEqual(self.update()['count'],0)
    def test_midnight_uses_shanghai_not_utc(self):
        self.now=datetime(2026,10,18,0,1,tzinfo=CN)
        self.add('today',datetime(2026,10,17,16,0,tzinfo=timezone.utc))
        self.add('yesterday',datetime(2026,10,17,15,59,tzinfo=timezone.utc))
        self.update();self.assertEqual([e['id'] for e in self.store.hotspots(now=self.now)['events']],['today'])
    def test_clock_rollover_keeps_history_and_never_falls_back(self):
        self.add('today',self.now);self.update();before=self.store.history_day('2026-10-18',now=self.now)
        self.now+=timedelta(days=1);result=self.store.hotspots(now=self.now)
        self.assertEqual(result['edition_date'],'2026-10-19');self.assertEqual(result['events'],[])
        self.update();self.assertEqual(self.store.hotspots(now=self.now)['events'],[])
        after=self.store.history_day('2026-10-18',now=self.now)
        self.assertEqual(after['events'],before['events']);self.assertEqual(after['period_state'],'sealed')
    def test_updated_ingested_or_selected_today_cannot_upgrade_old_publication(self):
        event=self.add('old',self.now-timedelta(days=1))
        event.update(generated_at=self.now.isoformat(),published_at=self.now.isoformat(),reported_at=self.now.isoformat(),selection_attention={'important':True,'expires_at':(self.now+timedelta(days=2)).isoformat()})
        with self.store.db() as db:self.assertIsNone(self.daily.selection_eligibility(db,event,self.now))
    def test_unknown_and_update_only_dates_rejected(self):
        event=self.add('today',self.now)
        with self.store.db() as db:
            row=db.execute('SELECT body FROM market_articles').fetchone();article=json.loads(row[0])
            for change in ({'published_at':None},{'published_at':'2026-10-18T10:30:00'},{'reported_time_kind':'source_page_updated_at'}):
                db.execute('UPDATE market_articles SET body=?',(json.dumps({**article,**change}),))
                self.assertIsNone(self.daily.selection_eligibility(db,event,self.now))
    def test_already_stored_old_policy_day_is_filtered_without_mutating_history(self):
        self.add('old',self.now-timedelta(days=1));self.add('today',self.now)
        with self.store.db() as db:
            items=self.daily.candidates(db,self.now,reconstructed_date=self.now.date()-timedelta(days=1))
            # Reproduce a legacy observed snapshot without changing publication dates.
            db.execute('DROP TRIGGER market_daily_same_day_publication')
            old_guard=self.daily.selection_eligibility
            try:
                self.daily.selection_eligibility=lambda *args:{'freshness_layer':0}
                self.daily.write_edition(db,'2026-10-18',items,self.now,'observed','legacy',{})
            finally:self.daily.selection_eligibility=old_guard
        with self.store.db() as db:DailyMarket.schema(db)
        archived=self.store.history_day('2026-10-18',now=self.now)
        self.assertEqual([e['id'] for e in archived['events']],['old'])
        self.assertEqual(self.store.hotspots(now=self.now)['events'],[])
        self.assertEqual(self.store.history_day('2026-10-18',now=self.now)['events'],archived['events'])
    def test_storage_guard_rejects_stale_writer_and_allows_historical_reconstruction(self):
        self.add('old',self.now-timedelta(days=1))
        with self.store.db() as db:items=self.daily.candidates(db,self.now,reconstructed_date=self.now.date()-timedelta(days=1))
        original=self.daily.selection_eligibility
        try:
            self.daily.selection_eligibility=lambda *args:{'freshness_layer':0}
            with self.assertRaisesRegex(sqlite3.IntegrityError,'same_day_publication'),self.store.db() as db:
                self.daily.write_edition(db,'2026-10-18',items,self.now,'observed','stale',{})
        finally:self.daily.selection_eligibility=original
        result=self.store.reconstruct_daily(now=self.now)
        self.assertTrue(result['days']);self.assertEqual(len(self.store.history_day('2026-10-17',now=self.now)['events']),1)

if __name__=='__main__':unittest.main()
