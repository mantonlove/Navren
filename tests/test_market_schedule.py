import tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from market_content import MarketStore, CN
from market_schedule import plan, hot_slot
from market_pipeline import refresh

class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MarketStore(Path(self.tmp.name)/'market.sqlite3')
        self.now=datetime(2026,10,5,8,30,tzinfo=CN)
    def record(self,now,status='ok',kind='hotspots',id='run'):
        target=plan(self.store,now)['targets'].get(kind,now.isoformat())
        self.store.record_run(id,{'started_at':now.isoformat(),'ended_at':now.isoformat(),
            'status':'complete' if status=='ok' else 'failed','targets':{kind:target},'streams':{kind:{'status':status}}})
    def publish_week(self,period='2026-09-28'):
        self.store.publish('market_weeklies',period,{'period':period,'title':'已审核周报','article_ids':[]},period)
    def test_complete_hot_slot_is_not_repeated(self):
        self.record(self.now);self.publish_week()
        self.assertEqual(plan(self.store,self.now+timedelta(minutes=1))['jobs'],[])
        self.assertIn('hotspots',plan(self.store,self.now.replace(hour=12,minute=30))['jobs'])
    def test_failed_slot_retries_after_fifteen_minutes(self):
        self.record(self.now,'failed');self.publish_week()
        self.assertEqual(plan(self.store,self.now+timedelta(minutes=14))['jobs'],[])
        self.assertEqual(plan(self.store,self.now+timedelta(minutes=15))['jobs'],['hotspots'])
    def test_partial_slot_also_retries(self):
        self.record(self.now,'partial');self.publish_week()
        self.assertIn('hotspots',plan(self.store,self.now+timedelta(minutes=15))['jobs'])
    def test_retry_is_bounded_and_new_slot_resets(self):
        for i,m in enumerate((0,15,45)):self.record(self.now+timedelta(minutes=m),'failed',id=str(i))
        self.publish_week()
        self.assertEqual(plan(self.store,self.now+timedelta(hours=2))['jobs'],[])
        self.assertIn('hotspots',plan(self.store,self.now.replace(hour=12,minute=30))['jobs'])
    def test_weekly_failure_independent_from_hot_success(self):
        self.record(self.now);self.record(self.now,'failed','weekly','weekly')
        self.assertEqual(plan(self.store,self.now+timedelta(minutes=15))['jobs'],['weekly'])
    def test_weekly_retries_next_day_after_three_failures(self):
        for i,m in enumerate((0,15,45)):self.record(self.now+timedelta(minutes=m),'failed','weekly',str(i))
        p=plan(self.store,self.now+timedelta(hours=2));self.assertFalse(p['streams']['weekly']['due'])
        self.assertFalse(plan(self.store,self.now+timedelta(days=1,minutes=-1))['streams']['weekly']['due'])
        self.assertTrue(plan(self.store,self.now+timedelta(days=1))['streams']['weekly']['due'])
    def test_sleep_catchup_before_first_daily_slot(self):
        now=self.now.replace(hour=7)
        self.assertEqual(hot_slot(now).date().isoformat(),'2026-10-04')
        self.assertIn('hotspots',plan(self.store,now)['jobs'])
    def test_ingest_only_does_not_consume_publication_slot(self):
        self.record(self.now,'ok','ingest');self.publish_week()
        self.assertIn('hotspots',plan(self.store,self.now+timedelta(minutes=1))['jobs'])
    def test_source_revision_reopens_weekly_job(self):
        self.publish_week();self.assertFalse(plan(self.store,self.now)['streams']['weekly']['due'])
        with self.store.db() as db:db.execute("UPDATE market_weeklies SET status='source_revised'")
        self.assertTrue(plan(self.store,self.now)['streams']['weekly']['due'])
    def test_scheduled_refresh_rechecks_plan_under_lock(self):
        with patch('market_pipeline.plan',return_value={'jobs':[],'targets':{}}),patch('market_pipeline.discover') as discover:
            self.assertEqual(refresh(self.store,scheduled=True)['status'],'not_due');discover.assert_not_called()
    def test_all_network_failures_are_not_success(self):
        with patch('market_pipeline.discover',side_effect=ConnectionRefusedError),patch('market_pipeline.MarketEditor'):
            r=refresh(self.store,ingest_only=True,total_seconds=1)
        self.assertEqual(r['status'],'failed');self.assertEqual(r['streams']['ingest']['status'],'failed')
        self.assertTrue(all(s['error_type']=='ConnectionRefusedError' for s in r['sources']))

if __name__=='__main__':unittest.main()
