"""The public daily card omits originals; identity still uses exact approved sources."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from market_content import CN, MarketStore, event_input_hash
from market_daily import (DailyMarket, MarketDailyError, distinct_event_count, event_aliases,
                          selected_substory_unverified, _publication_clock_scope, _clock_execution_owner)
from market_editor import MarketEditor
from test_market_same_day import FixedClient
from test_market_us_close_dedup import ALIAS, XINHUA, YICAI


class PublicProjectionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarketStore(Path(self.tmp.name) / 'market.sqlite3')
        self.daily = DailyMarket(self.store)
        self.now = datetime.fromisoformat('2026-10-28T09:15:00+08:00')

    def add(self, kind, *, headline=None, facts=None, evidence=None, article_id_override=None):
        article_id = article_id_override or 'article-' + kind
        event_id = 'event-' + kind
        if kind == 'xinhua':
            title = '纽约股市三大股指27日上涨'
            headline = headline or '纽约股市三大股指10月27日收涨，科技板块领涨'
            evidence = evidence or XINHUA
            published = '2026-10-27T20:40:22+00:00'
            source = 'xinhua'
            publisher = '新华网'
            url = 'https://www.news.cn/fortune/20261027/' + 'a' * 32 + '/c.html'
            attribution = {'contract': 'xinhua-visible-source-v1', 'status': 'self_published'}
            facts = facts or [
                {'text': '据新华社纽约10月27日电，纽约股市三大股指在27日上涨。',
                 'quote': '新华社纽约10月27日电 纽约股市三大股指27日上涨。'},
                {'text': '道琼斯工业平均指数收于52377.57点。',
                 'quote': '道琼斯工业平均指数上涨15.99点，收于52377.57点'},
                {'text': '标准普尔500种股票指数收于7722.60点。',
                 'quote': '标准普尔500种股票指数上涨45.83点，收于7722.60点'},
                {'text': '纳斯达克综合指数收于26477.81点。',
                 'quote': '纳斯达克综合指数上涨308.43点，收于26477.81点'},
            ]
        elif kind in ('yicai', 'financing', 'study'):
            title = ('AI交易发力美股两连阳，金银闪耀原油跳水' if kind == 'yicai'
                     else '美国银行等宣布融资协议' if kind == 'financing'
                     else '高盛研究称半导体板块解释资产价格分化')
            headline = headline or ('AI交易带动美股两连阳，三大股指收涨' if kind == 'yicai'
                                    else title)
            evidence = evidence or (YICAI if kind == 'yicai'
                                    else '美国银行等宣布提供630亿美元融资。' if kind == 'financing'
                                    else '高盛研究称半导体板块解释资产价格分化。')
            published = '2026-10-27T23:04:29+00:00'
            source = 'yicai'
            publisher = '第一财经'
            url = 'https://www.yicai.com/news/' + {'yicai':'12345678','financing':'12345679','study':'12345680'}[kind] + '.html'
            attribution = {'contract':'yicai-visible-source-v1','status':'self_published',
                           'visible_source_credits':['第一财经'],'visible_date_matches_published_at':True}
            facts = facts or ([{'text':'周二美股三大股指收涨：道指收于52377.57点、纳指收于26477.81点、标普500收于7722.60点。',
                               'quote':'截至收盘，道琼斯工业平均指数上涨15.99点，报52377.57点，纳斯达克综合指数上涨1.18%，报26477.81点，标普500指数涨0.6%，报7722.60点。'}]
                              if kind == 'yicai' else [{'text':evidence,'quote':evidence}])
        else:
            title = '隔离独立金融资料 ' + kind
            headline = headline or title
            evidence = evidence or ('隔离独立金融资料 ' + kind + '，具体事项经核实。')
            published = (self.now - timedelta(minutes=int(kind[1:]) + 1)).isoformat()
            source = 'nbs'
            publisher = '国家统计局'
            url = 'https://www.stats.gov.cn/sj/zxfb/202610/t20261028_19654' + kind[1:] + '.html'
            attribution = None
            facts = facts or [{'text':evidence,'quote':evidence}]
        article = {'id':article_id,'source_id':source,'publisher':publisher,
            'origin_publisher':publisher,'title':title,'url':url,'published_at':published,
            'original_published_at':published,'first_publication_status':'verified_original_page',
            'published_precision':'second','checked_at':self.now.isoformat(),
            'read_scope':'full_text','link_status':'verified','access_status':'free_full_text',
            'evidence_text':evidence,'evidence_truncated':False,'identity':
            'media_report' if source in ('xinhua','yicai') else 'official_release',
            'fingerprint':hashlib.sha256(evidence.encode()).hexdigest(),'version':1}
        if attribution:
            article.update(source_attribution=attribution, publication_platform=publisher,
                           reporting_body=publisher, byline_text='测试记者')
        self.store.save_article(article)
        saved = next(a for a in self.store.articles() if a['id'] == article_id)
        self.assertEqual(saved['status'],'active')
        draft = {'id':event_id,'headline':headline,'summary':facts[0]['text'],
                 'analysis':'市场变化仍须结合资金与个人风险承受能力。',
                 'uncertainty':'后续表现仍待观察。',
                 'facts':[{**fact,'article_id':article_id} for fact in facts]}
        captured = self.store.capture_event_generation([{'id':event_id,'articles':[saved]}])
        editor = MarketEditor(FixedClient({'events':[draft]}),
                              FixedClient({'verdict':'approve','reason':'隔离原文与草稿绑定通过。','issues':[]}))
        event = editor.events(captured['groups'],'public-projection-test',
                              generations=captured['generations'])[0]
        event['input_hash'] = event_input_hash(captured['groups'][0])
        self.store._record_event_review(event)
        self.store.publish('market_events',event_id,event,event['input_hash'])
        with self.store.db() as db:
            row = self.daily.event_row(db,event_id,1)
            public = self.daily.approved_snapshot(db,row,{})
        self.assertIsNotNone(public)
        self.assertNotIn('evidence_text',public['articles'][0])
        return public

    def test_public_projection_collapses_two_new_york_close_sources(self):
        first,second = self.add('xinhua'),self.add('yicai')
        self.assertEqual(distinct_event_count([first,second]),2)
        self.assertNotIn(ALIAS,event_aliases(first))
        self.assertNotIn(ALIAS,event_aliases(second))
        with self.store.db() as db:
            aliases = [self.daily.source_bound_aliases(db,e) for e in (first,second)]
            self.assertEqual(distinct_event_count([first,second],
                lambda event:self.daily.source_bound_aliases(db,event)),1)
            selected = self.daily.candidates(db,self.now)
        self.assertEqual(set(aliases[0]) & set(aliases[1]),{ALIAS})
        self.assertEqual(len(selected),1)
        self.assertEqual({e['id'] for e in selected[0]['related_event_refs']} |
                         {selected[0]['id']},{first['id'],second['id']})
        self.assertNotIn('evidence_text',json.dumps(selected,ensure_ascii=False))

    def test_financing_and_study_remain_separate(self):
        first,second = self.add('xinhua'),self.add('yicai')
        financing,study = self.add('financing'),self.add('study')
        with self.store.db() as db:
            self.assertEqual(distinct_event_count([first,second,financing,study],
                lambda event:self.daily.source_bound_aliases(db,event)),3)
            selected = self.daily.candidates(db,self.now)
        self.assertEqual(len(selected),3)

    def test_archived_public_six_reports_five_and_read_does_not_write(self):
        first,second = self.add('xinhua'),self.add('yicai')
        others = [self.add('n'+str(i)) for i in range(4)]
        snapshots = [first,second,*others]
        self.assertEqual(distinct_event_count(snapshots),6)
        for event in snapshots:
            event['selection_reason'] = {'rule_version':'legacy-test','score':1}
        with self.store.db() as db:
            self.daily.write_edition(db,'2026-10-28',snapshots,self.now,'observed','legacy-six',{})
        with self.store.db() as db:
            before=db.total_changes
            day=self.daily._read_day(db,'2026-10-28',now=self.now)
            index=self.daily.index('2026-10-28','2026-10-28',now=self.now)
            self.assertEqual(before,db.total_changes)
        self.assertEqual(day['total_count'],6)
        self.assertEqual(day['readable_count'],6)
        self.assertEqual(day['distinct_event_count'],5)
        self.assertEqual(index['dates'][0]['distinct_event_count'],5)
        self.assertIsNone(index['dates'][0]['complete_revision'])
        self.assertIsNone(index['default_date'])
        self.assertEqual([event['id'] for event in day['events']],
                         [event['id'] for event in snapshots])
        self.assertTrue(all('evidence_text' not in json.dumps(event) for event in day['events']))

    def test_live_publication_final_gate_rejects_duplicate_sources(self):
        first,second=self.add('xinhua'),self.add('yicai')
        for event in (first,second):
            event['selection_reason']={'rule_version':'test','score':1}
        with self.store.db() as db:
            token=_publication_clock_scope.set((self.daily,_clock_execution_owner()))
            try:
                with self.assertRaises(MarketDailyError) as error:
                    self.daily.write_edition(db,'2026-10-28',[first,second],self.now,
                                             'observed','duplicate-blocked',{})
            finally:
                _publication_clock_scope.reset(token)
            self.assertEqual(error.exception.code,'daily_selection_identity_unverified')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_daily_editions').fetchone()[0],0)

    def test_missing_exact_raw_article_fails_closed(self):
        first = self.add('xinhua')
        with self.store.db() as db:
            self.assertIn(ALIAS,self.daily.source_bound_aliases(db,first))
            forged = copy.deepcopy(first)
            forged['facts'] = [{'article_id':first['primary_article_id'],
                                'text':'伪造事实','quote':'伪造引文'}]
            self.assertIsNone(self.daily.source_bound_aliases(db,forged))
            db.execute('DELETE FROM market_articles WHERE id=? AND version=?',
                       (first['primary_article_id'],first['article_versions'][first['primary_article_id']]))
            self.assertIsNone(self.daily.source_bound_aliases(db,first))
            self.assertEqual(self.daily.candidates(db,self.now),[])

    def test_unverified_selected_substory_is_isolated_without_changing_source(self):
        quote=('高盛研究显示，过去三周内，半导体板块的行情差异可以解释各类资产约54%'
               '的表现分化，而油价仅能解释2%。')
        unmarked='投资者情绪与科技股的关联已经变得十分紧密，近年来这种关联还在不断加强。'
        evidence='►►高盛表示，研究市场变化。 '+quote+' '+unmarked+' ►►甲骨文宣布其他事项。'
        selected=self.add('study',evidence=evidence,facts=[{'text':quote,'quote':quote}],
                          article_id_override='a_8469149abcc5d21088b43522')
        self.assertTrue(selected_substory_unverified(selected))
        unrelated=copy.deepcopy(selected)
        unrelated['facts'][0]['quote']='另一项独立研究，不能借用已标记的选稿事实。'
        self.assertFalse(selected_substory_unverified(unrelated))
        regenerated=copy.deepcopy(selected)
        regenerated['id']='another-generated-event'
        regenerated['primary_article_id']='another-id-for-same-page'
        regenerated['article_versions']={'another-id-for-same-page':2}
        regenerated['articles'][0]['id']='another-id-for-same-page'
        regenerated['articles'][0]['url']='https://www.yicai.com/news/103412209.html'
        regenerated['facts'][0]['article_id']='another-id-for-same-page'
        self.assertTrue(selected_substory_unverified(regenerated))
        regenerated['facts'][0]['quote']='高盛研究显示，半导体板块解释资产价格分化。'
        self.assertTrue(selected_substory_unverified(regenerated))
        shortened=copy.deepcopy(selected)
        shortened['facts'][0]['quote']=unmarked
        with self.store.db() as db:
            self.assertTrue(self.daily.selected_substory_unverified(db,shortened))
        regenerated['facts'][0]['quote']='美国银行等宣布融资协议。'
        self.assertFalse(selected_substory_unverified(regenerated))
        with self.store.db() as db:
            row=self.daily.article_row(db,selected['primary_article_id'],1)
            self.assertEqual(row['status'],'active')
            self.assertEqual(self.daily.candidates(db,self.now),[])
            selected['selection_reason']={'rule_version':'legacy-test','score':1}
            self.daily.write_edition(db,'2026-10-28',[selected],self.now,'observed','legacy-selected',{})
            day=self.daily._read_day(db,'2026-10-28',now=self.now)
        self.assertEqual(day['readable_count'],0)
        self.assertEqual(day['distinct_event_count'],1)
        self.assertEqual(day['events'][0]['availability'],'temporarily_unavailable')
        self.assertEqual(day['events'][0]['availability_reason'],'first_of_selected_substory_unverified')

    def test_archive_revision_and_home_use_latest_complete_valid_six(self):
        self.now = datetime.fromisoformat('2026-10-27T12:30:00+08:00')
        stable = [self.add('n'+str(i)) for i in range(6)]
        extra = self.add('n6')
        def write(items, label):
            copies = copy.deepcopy(items)
            for item in copies:
                item['selection_reason'] = {'rule_version':'legacy-test','score':1}
                item['display_summary'] = label
            with self.store.db() as db:
                return self.daily.write_edition(db,self.now.date().isoformat(),copies,
                    self.now,'observed','test-'+label,{})
        for label in ('one','two','three'):
            self.assertTrue(write(stable,label)['changed'])
        self.assertEqual(write([*stable[:5],extra],'four')['revision'],4)
        self.store.unavailable_article(extra['primary_article_id'])
        self.now = datetime.fromisoformat('2026-10-28T09:15:00+08:00')
        newer = [self.add('xinhua'),self.add('yicai'),
                 *(self.add('n'+str(i)) for i in range(20,24))]
        self.assertEqual(write(newer,'duplicate')['revision'],1)
        before = self._archive_rows()
        old = self.store.history_day('2026-10-27',now=self.now)
        actual = self.daily.day('2026-10-27',now=self.now,revision=3)
        routed = self.daily.route('/api/market/hotspots/history/2026-10-27',
                                  {'revision':['3']},now=self.now)
        with self.assertRaises(MarketDailyError):
            self.daily.route('/api/market/hotspots/history/2026-10-27',
                             {'revision':['0']},now=self.now)
        index = self.store.history_index(now=self.now)
        home = self.store.hotspots(now=self.now)
        self.assertEqual(before,self._archive_rows())
        self.assertEqual(old['revision'],4)
        self.assertEqual(old['readable_count'],5)
        self.assertEqual(actual['revision'],3)
        self.assertEqual(routed['revision'],3)
        self.assertEqual((actual['total_count'],actual['readable_count'],
                          actual['distinct_event_count']),(6,6,6))
        self.assertEqual(index['dates'][-1]['distinct_event_count'],5)
        self.assertEqual(index['dates'][-2]['complete_revision'],3)
        self.assertEqual((index['default_date'],index['default_revision']),('2026-10-27',3))
        self.assertEqual((home['edition_date'],home['revision']),('2026-10-27',3))

    def _archive_rows(self):
        with self.store.db() as db:
            return (
                [tuple(row) for row in db.execute('SELECT * FROM market_daily_editions ORDER BY edition_date,revision')],
                [tuple(row) for row in db.execute('SELECT * FROM market_daily_items ORDER BY edition_date,revision,position')],
                [tuple(row) for row in db.execute('SELECT * FROM market_daily_checks ORDER BY run_id')],
            )


if __name__ == '__main__':
    unittest.main()
