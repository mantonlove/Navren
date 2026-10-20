import copy, importlib.util, json, tempfile, time, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from market_content import MarketStore, CN, event_input_hash
from market_schedule import plan, hot_slot
from market_pipeline import refresh
from market_editor import MarketEditor

class FixedJsonClient:
    model_id='test-schedule-no-network'
    def __init__(self,result):self.result=result
    def complete_json(self,**kwargs):return copy.deepcopy(self.result)

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
        # 复用真实capture/editor/review/publish流程；固定客户端不调用外部模型。
        start=datetime.fromisoformat(period).replace(tzinfo=CN);end=start+timedelta(days=7)
        article={'id':'schedule-article-'+period,'source_id':'nbs','publisher':'国家统计局',
            'title':'隔离测试统计发布','url':'https://www.stats.gov.cn/sj/zxfb/202609/t20260930_1965449.html',
            'published_at':(start+timedelta(days=1)).isoformat(),'published_precision':'minute',
            'checked_at':self.now.isoformat(),'read_scope':'full_text','link_status':'verified',
            'evidence_text':'隔离测试统计资料，不能解释为真实发布。','evidence_truncated':False,
            'identity':'official_release','fingerprint':'schedule-article-'+period,'version':1}
        self.store.save_article(article)
        article=next(a for a in self.store.articles() if a['id']==article['id'])
        draft={'id':'schedule-event-'+period,'headline':'隔离测试统计事件','summary':'隔离合成统计资料。',
            'analysis':'影响仍取决于实际条件。','uncertainty':'这不是实际市场发布。',
            'facts':[{'text':'隔离测试统计资料','quote':'隔离测试统计资料','article_id':article['id']}]}
        approval={'verdict':'approve','reason':'隔离测试原文及草稿绑定核对通过。','issues':[]}
        editor=MarketEditor(FixedJsonClient({'events':[draft]}),FixedJsonClient(approval))
        captured=self.store.capture_event_generation([{'id':draft['id'],'articles':[article]}])
        event=editor.events(captured['groups'],'schedule-event',generations=captured['generations'])[0]
        event['input_hash']=event_input_hash(captured['groups'][0])
        self.store._record_event_review(event)
        self.store.publish('market_events',event['id'],event,event['input_hash'])
        captured=self.store.capture_weekly_generation([self.store.event(event['id'])],start,end,'schedule-week')
        weekly_draft={'title':'已审核周报','summary':'隔离测试周报摘要。',
            'changes':[{'event_id':event['id'],'heading':'隔离资料变化','text':'只依据本次隔离统计资料说明变化。','conditions':'影响仍取决于条件。'}],
            'transmission':'这是隔离测试资料，不能用于投资判断。'}
        editor=MarketEditor(FixedJsonClient(weekly_draft),FixedJsonClient(approval))
        weekly=editor.weekly(captured['events'],start,end,'schedule-week',generation=captured['generation'])
        weekly['input_hash']=captured['generation']['input_hash'];weekly['reading']=captured['reading']
        self.store._record_weekly_review(weekly)
        self.store.publish('market_weeklies',period,weekly,weekly['input_hash'])
        return self.store.weekly_publications()[0]
    def legacy_week(self,period='2026-09-28'):
        body={'period':period,'version':1,'title':'缺少生成凭据的旧周报','article_ids':[],
            'period_start':period+'T00:00:00+08:00',
            'period_end_exclusive':(datetime.fromisoformat(period)+timedelta(days=7)).isoformat()+'+08:00'}
        with self.store.db() as db:
            db.execute('INSERT INTO market_weeklies VALUES(?,?,?,?,?,?)',
                (period,1,'legacy-unverified',json.dumps(body),'published',self.now.isoformat()))
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
    def test_legacy_published_without_generation_is_rebuild_not_success(self):
        self.legacy_week();self.record(self.now,'ok','weekly','legacy-ok')
        p=plan(self.store,self.now+timedelta(minutes=15))
        self.assertIn('weekly',p['jobs']);self.assertTrue(p['streams']['weekly']['rebuild_required'])
        self.assertEqual(p['streams']['weekly']['publication_status'],'generation_binding_unavailable')
        self.assertIsNone(p['streams']['weekly']['last_success_at'])
        self.assertIsNone(self.store.weekly()['report'])
    def test_legacy_success_attempts_are_bounded_and_retry_next_day(self):
        self.legacy_week()
        for i,m in enumerate((0,15,45)):self.record(self.now+timedelta(minutes=m),'ok','weekly','legacy-'+str(i))
        self.assertFalse(plan(self.store,self.now+timedelta(hours=2))['streams']['weekly']['due'])
        self.assertTrue(plan(self.store,self.now+timedelta(days=1))['streams']['weekly']['due'])
    def test_older_valid_report_does_not_complete_missing_target(self):
        self.publish_week('2026-09-21');self.legacy_week()
        self.assertEqual(self.store.weekly()['report']['period'],'2026-09-21')
        self.assertTrue(plan(self.store,self.now)['streams']['weekly']['due'])
    def test_permission_revoke_restore_cannot_restore_old_weekly_qualification(self):
        self.publish_week()
        self.store.set_source_permission('nbs',enabled=False,expected_revision=1)
        self.store.set_source_permission('nbs',enabled=True,expected_revision=2)
        with self.store.db() as db:db.execute("UPDATE market_weeklies SET status='published'")
        self.assertTrue(plan(self.store,self.now)['streams']['weekly']['due'])
        self.assertNotEqual(self.store.weekly_publications()[0]['status'],'published')
    def test_supporting_event_tamper_reopens_raw_published_weekly(self):
        self.publish_week()
        with self.store.db() as db:
            row=db.execute('SELECT body FROM market_events').fetchone();event=json.loads(row[0]);event.pop('generation')
            db.execute('UPDATE market_events SET body=?',(json.dumps(event),))
        self.assertTrue(plan(self.store,self.now)['streams']['weekly']['due'])
    def test_publication_helper_does_not_read_coverage_or_recurse_into_schedule(self):
        self.publish_week()
        with patch.object(self.store,'status',side_effect=AssertionError('status recursion')):
            self.assertEqual(self.store.weekly_publications()[0]['status'],'published')
            self.assertFalse(plan(self.store,self.now)['streams']['weekly']['due'])
    def test_pipeline_does_not_count_legacy_raw_publication_as_success(self):
        self.legacy_week()
        with patch('market_pipeline.plan',return_value={'jobs':['weekly'],'targets':{'weekly':'2026-09-28'}}):
            result=refresh(self.store,scheduled=True,sources=[],total_seconds=0)
        self.assertEqual(result['streams']['weekly']['status'],'failed')
        self.assertEqual(result['streams']['weekly']['publication_status'],'generation_binding_unavailable')
        self.assertEqual(result['published_weeklies'],0)
    def test_pipeline_accepts_genuinely_published_target_without_rewriting(self):
        self.publish_week()
        with patch('market_pipeline.plan',return_value={'jobs':['weekly'],'targets':{'weekly':'2026-09-28'}}):
            result=refresh(self.store,scheduled=True,sources=[],total_seconds=0)
        self.assertEqual(result['streams']['weekly']['status'],'ok')
        self.assertEqual(result['streams']['weekly']['version'],1)
        self.assertEqual(result['published_weeklies'],0)

class OnceRecoveryTests(unittest.TestCase):
    setUp=ScheduleTests.setUp
    def executor(self):
        path=Path(__file__).resolve().parents[1]/'outputs/daily-market/market-recovery-20261017/recover_weekly_once.py'
        spec=importlib.util.spec_from_file_location('market_recovery_once_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def fixed_editor(self,module,budget,*,approve=True,length_revisions=0):
        class Primary:
            model_id='test-recovery-primary'
            weekly_calls=0
            def complete_json(self,**kwargs):
                packet=json.loads(kwargs['user'])
                if kwargs['stage']=='market.events':
                    material=packet['materials'][0];article=material['articles'][0]
                    return {'events':[{'id':material['event_id'],'headline':'隔离测试统计事件','summary':'隔离测试统计资料。',
                        'analysis':'影响仍取决于实际条件。','uncertainty':'不是实际市场发布。',
                        'facts':[{'text':'隔离测试统计资料','quote':'隔离测试统计资料','article_id':article['id']}]}]}
                self.weekly_calls+=1
                return {'title':'隔离测试周报','summary':'隔离。'*600 if self.weekly_calls<=length_revisions else '只依据两份隔离原文。',
                    'changes':[{'event_id':e['id'],'heading':'隔离资料变化','text':'仅反映这份资料提供的信息。',
                        'conditions':'影响仍取决于条件。'} for e in packet['materials']],
                    'transmission':'来源覆盖有限，不构成投资建议。'}
        reviewer=FixedJsonClient({'verdict':'approve' if approve else 'reject','reason':'隔离原文与草稿核对。','issues':[]})
        return MarketEditor(module.BoundedClient(Primary(),budget),module.BoundedClient(reviewer,budget))
    def articles(self,module):
        for aid,version,_,_ in module.SPEC:
            article={'id':aid,'source_id':'nbs','publisher':'国家统计局','title':'隔离统计资料',
                'url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261008_1965465.html',
                'published_at':'2026-10-09T09:30:00+08:00','checked_at':self.now.isoformat(),
                'read_scope':'full_text','link_status':'verified','evidence_text':'隔离测试统计资料，不能解释为真实发布。',
                'evidence_truncated':False,'identity':'official_release','fingerprint':aid}
            for revision in range(version):self.store.save_article({**article,'fingerprint':aid+str(revision)})
    def test_one_shot_real_generation_path_six_fake_calls_no_daily_rewrite(self):
        module=self.executor();self.articles(module)
        legacy={}
        with self.store.db() as db:
            for index,(aid,version,eid,_) in enumerate(module.SPEC):
                body=json.dumps({'id':eid,'version':1,'article_ids':[aid],'article_versions':{aid:1}})
                legacy[eid]=body
                db.execute('INSERT INTO market_events VALUES(?,?,?,?,?,?)',
                    (eid,1,'untrusted-old',body,'published' if index==0 else 'withdrawn',self.now.isoformat()))
            weekly=json.dumps({'period':module.PERIOD,'version':1,'title':'不合格旧周报',
                'period_start':'2026-10-05T00:00:00+08:00','period_end_exclusive':'2026-10-12T00:00:00+08:00','article_ids':[]})
            db.execute('INSERT INTO market_weeklies VALUES(?,?,?,?,?,?)',
                (module.PERIOD,1,'untrusted-old',weekly,'published',self.now.isoformat()))
        budget=module.Budget(time.monotonic()+420,lambda:None)
        with patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            result=module.rebuild(self.store,self.fixed_editor(module,budget),'unpaid-once')
        self.assertEqual(len(budget.calls),6);self.assertEqual(result['version'],2)
        self.assertEqual(len(result['events']),2)
        self.assertEqual({e['version'] for e in result['events']},{2})
        self.assertEqual(self.store.weekly_publications()[0]['status'],'published')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_event_review_bindings').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_weekly_review_bindings').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_daily_editions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT body FROM market_weeklies WHERE version=1').fetchone()[0],weekly)
            for eid,body in legacy.items():self.assertEqual(db.execute('SELECT body FROM market_events WHERE id=? AND version=1',(eid,)).fetchone()[0],body)
    def test_one_shot_rejected_review_never_publishes_event_or_weekly(self):
        module=self.executor();self.articles(module);budget=module.Budget(time.monotonic()+420,lambda:None)
        with self.assertRaisesRegex(ValueError,'independent_review_rejected'):
            module.rebuild(self.store,self.fixed_editor(module,budget,approve=False),'unpaid-reject')
        self.assertEqual(len(budget.calls),2)
        self.assertEqual(self.store.versions('market_events'),[]);self.assertEqual(self.store.weekly_publications(),[])
    def test_one_shot_two_existing_length_revisions_still_stop_at_eight_calls(self):
        module=self.executor();self.articles(module);budget=module.Budget(time.monotonic()+420,lambda:None)
        result=module.rebuild(self.store,self.fixed_editor(module,budget,length_revisions=2),'unpaid-lengths')
        self.assertEqual(result['version'],1);self.assertEqual(len(budget.calls),8)
        self.assertEqual(budget.output_tokens,39200)
    def test_one_shot_reserves_glm_call_input_and_deadline_before_primary(self):
        module=self.executor();client=FixedJsonClient({});args={'stage':'market.events','system':'原审核','user':'原文','request_id':'unpaid-limit'}
        for kind in ('calls','input','deadline'):
            budget=module.Budget(time.monotonic()+420,lambda:None)
            if kind=='calls':budget.calls=[{}]*7
            if kind=='input':budget.input_bytes=250001
            if kind=='deadline':budget.deadline=time.monotonic()+90
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'recovery_budget_or_deadline_exhausted'):
                budget.request(client,args)
    def test_one_shot_source_change_stops_before_client(self):
        module=self.executor()
        def changed():raise ValueError('recovery_inputs_or_sources_changed')
        budget=module.Budget(time.monotonic()+420,changed)
        with self.assertRaisesRegex(ValueError,'recovery_inputs_or_sources_changed'):
            budget.request(FixedJsonClient({}),{'stage':'market.events','system':'原审核','user':'原文','request_id':'unpaid-change'})
        self.assertEqual(budget.calls,[])
    def test_one_shot_input_budget_counts_exact_model_adapter_payload(self):
        module=self.executor()
        from agent_runtime.model_adapters import OpenAICompatibleJsonClient
        observed=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,limit):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}).encode()
        def transport(request,timeout):observed.append(request.data);return Response()
        client=OpenAICompatibleJsonClient(provider='ai_ping',model_id='test-byte-count',url='https://offline.test',
            env_name='UNUSED',keychain_service='UNUSED',timeout=45,max_retries=0,
            key_loader=lambda:'isolated-no-secret',urlopen=transport,log_path=str(Path(self.tmp.name)/'offline-call.jsonl'))
        budget=module.Budget(time.monotonic()+420,lambda:None)
        budget.request(client,{'stage':'market.events','system':'原审核','user':'原文','request_id':'unpaid-exact-payload'})
        self.assertEqual(budget.input_bytes,len(observed[0]))
        self.assertEqual(json.loads(observed[0])['reasoning_effort'],'low')
    def test_resume_reuses_approved_event_and_needs_four_or_five_calls(self):
        module=self.executor();self.articles(module)
        initial=module.Budget(time.monotonic()+420,lambda:None)
        first=module.rebuild(self.store,self.fixed_editor(module,initial),'unpaid-first')
        approved=self.store.event(module.SPEC[0][2])
        reuse={'event_id':approved['id'],'version':approved['version'],'publication_sha256':approved['publication_sha256']}
        for corrections,expected in ((0,4),(1,5)):
            limits={'max_calls':5,'max_input_utf8_bytes':246985,'max_output_tokens':23800}
            budget=module.Budget(time.monotonic()+369,lambda:None,limits)
            result=module.rebuild(self.store,self.fixed_editor(module,budget,length_revisions=corrections),
                'unpaid-resume',reuse_event=reuse)
            self.assertEqual(len(budget.calls),expected)
            self.assertEqual(self.store.event(approved['id'])['version'],approved['version'])
            self.assertEqual(result['events'][0]['version'],first['events'][0]['version'])
    def test_resume_does_not_admit_second_length_revision_without_review_reserve(self):
        module=self.executor();self.articles(module);initial=module.Budget(time.monotonic()+420,lambda:None)
        module.rebuild(self.store,self.fixed_editor(module,initial),'unpaid-first')
        approved=self.store.event(module.SPEC[0][2])
        reuse={'event_id':approved['id'],'version':approved['version'],'publication_sha256':approved['publication_sha256']}
        budget=module.Budget(time.monotonic()+369,lambda:None,{'max_calls':5,'max_input_utf8_bytes':246985,'max_output_tokens':23800})
        with self.assertRaisesRegex(ValueError,'recovery_budget_or_deadline_exhausted'):
            module.rebuild(self.store,self.fixed_editor(module,budget,length_revisions=2),'unpaid-limit',reuse_event=reuse)
        self.assertEqual(len(budget.calls),4)
        self.assertEqual(self.store.weekly_publications()[0]['version'],1)
    def test_controlled_editor_error_and_cause_are_retained_without_dynamic_text(self):
        module=self.executor()
        self.assertEqual(module.controlled_error_code(ValueError('quote_not_in_source')),'quote_not_in_source')
        self.assertEqual(module.controlled_error_code(ValueError('unsupported_fact_number:DO_NOT_LOG')),'unsupported_fact_number')
        self.assertIsNone(module.controlled_error_code(ValueError('service_error_DO_NOT_LOG')))
        try:
            try:raise ValueError('quote_not_in_source')
            except ValueError as cause:raise RuntimeError('DO_NOT_LOG') from cause
        except RuntimeError as error:self.assertEqual(module.controlled_error_code(error),'quote_not_in_source')
    def final_executor(self):
        path=Path(__file__).resolve().parents[1]/'outputs/daily-market/market-recovery-20261017/finish_weekly_once.py'
        spec=importlib.util.spec_from_file_location('market_final_weekly_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def test_final_weekly_reuses_both_fixed_events_with_two_calls(self):
        module=self.executor();self.articles(module);initial=module.Budget(time.monotonic()+420,lambda:None)
        module.rebuild(self.store,self.fixed_editor(module,initial),'unpaid-initial')
        expected=[self.store.event(row[2]) for row in module.SPEC]
        with self.store.db() as db:before=[tuple(r) for r in db.execute('SELECT * FROM market_events ORDER BY id,version')]
        final=self.final_executor();budget=final.FinalBudget(time.monotonic()+200,lambda:None)
        with patch.object(self.store,'capture_event_generation',side_effect=AssertionError('events must be reused')):
            result=final.finish(self.store,self.fixed_editor(module,budget),expected,'unpaid-final')
        self.assertEqual(result['version'],2);self.assertEqual(len(budget.calls),2)
        self.assertEqual([c['timeout_seconds'] for c in budget.calls],[90,45]);self.assertEqual(budget.output_tokens,8400)
        with self.store.db() as db:after=[tuple(r) for r in db.execute('SELECT * FROM market_events ORDER BY id,version')]
        self.assertEqual(before,after)
    def test_final_weekly_length_revision_is_rejected_before_extra_http(self):
        module=self.executor();self.articles(module);initial=module.Budget(time.monotonic()+420,lambda:None)
        module.rebuild(self.store,self.fixed_editor(module,initial),'unpaid-initial')
        expected=[self.store.event(row[2]) for row in module.SPEC]
        final=self.final_executor();budget=final.FinalBudget(time.monotonic()+200,lambda:None)
        with self.assertRaisesRegex(ValueError,'recovery_weekly_length_revision_forbidden'):
            final.finish(self.store,self.fixed_editor(module,budget,length_revisions=1),expected,'unpaid-final-limit')
        self.assertEqual(len(budget.calls),1);self.assertEqual(self.store.weekly_publications()[0]['version'],1)
    def test_final_weekly_wrong_timeout_or_event_stage_stops_before_http(self):
        final=self.final_executor();client=FixedJsonClient({});client.timeout=45
        kwargs={'stage':'market.weekly','system':'原审核','user':'原文','request_id':'unpaid-final-guard'}
        budget=final.FinalBudget(time.monotonic()+200,lambda:None)
        with self.assertRaisesRegex(ValueError,'recovery_final_client_limits_changed'):budget.request(client,kwargs)
        kwargs['stage']='market.events'
        with self.assertRaisesRegex(ValueError,'recovery_final_stage_forbidden'):budget.request(client,kwargs)
        self.assertEqual(budget.calls,[])

if __name__=='__main__':unittest.main()
