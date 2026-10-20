"""每日六条不能靠跨来源转载或同一场开幕综述重复占位。"""
import copy
import hashlib
import unittest
from datetime import timedelta
from market_content import event_input_hash
from market_daily import event_aliases
from market_editor import MarketEditor
from market_sources import cluster_key
import test_market_same_day as fixtures


def alias_event(id, title, headline=None, publisher='来源甲'):
    article={'id':id,'title':title,'publisher':publisher,'source_id':'cctv',
             'identity':'media_report','published_at':'2026-10-19T09:00:00+08:00'}
    return {'id':'event-'+id,'articles':[article],'primary_article_id':id,
            'headline':headline or title}


class AliasTests(unittest.TestCase):
    def test_same_headline_in_different_sources_does_not_get_two_slots(self):
        a=alias_event('one','原始刊发标题甲','银行间债券发行监管规则升级')
        b=alias_event('two','异地转载标题乙','银行间债券发行监管规则升级','来源乙')
        self.assertTrue(event_aliases(a)&event_aliases(b))

    def test_same_original_title_with_rewritten_summary_title_is_republication(self):
        a=alias_event('one','银行间债券发行监管规则升级','债券发行监管加力')
        b=alias_event('two','银行间债券发行监管规则升级','银行债券发行迎新规','来源乙')
        self.assertTrue(event_aliases(a)&event_aliases(b))

    def test_current_two_forum_opening_reports_share_one_slot(self):
        a=alias_event('one','风云激荡十五载：金融街见证开放',
                      '第十五届金融街论坛年会开幕，聚焦开放合作与全球金融新生态')
        b=alias_event('two','向强而行：中国的金融街，世界的金融街',
                      '2026金融街论坛年会开幕：聚焦全球金融发展新生态，多项政策与成果将发布','来源乙')
        common=event_aliases(a)&event_aliases(b)
        self.assertEqual(common,{'same_day_meeting_opening:2026-10-19:金融街论坛年会'})

    def test_separate_decisions_at_same_forum_keep_independent_slots(self):
        a=alias_event('one','论坛开幕报道','2026金融街论坛年会开幕')
        b=alias_event('two','央行政策决定','金融街论坛年会开幕，央行宣布下调政策利率')
        self.assertFalse(event_aliases(a)&event_aliases(b))

    def test_different_meetings_and_different_numbers_do_not_merge(self):
        for left,right in [('金融街论坛年会开幕','全球金融科技峰会开幕'),
                           ('央行下调政策利率10个基点','央行下调政策利率20个基点')]:
            with self.subTest(left=left,right=right):
                self.assertFalse(event_aliases(alias_event('a',left))&event_aliases(alias_event('b',right)))


class BoundDailyTests(unittest.TestCase):
    setUp=fixtures.SameDayTests.setUp

    def report(self,id,title,headline=None):
        article={'id':'article-'+id,'source_id':'nbs','publisher':'国家统计局','title':title,
                 'url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261018_1965449.html',
                 'published_at':self.now.isoformat(),'published_precision':'minute','checked_at':self.now.isoformat(),
                 'read_scope':'full_text','link_status':'verified','evidence_text':'隔离验收资料，不是真实新闻。',
                 'evidence_truncated':False,'identity':'official_release','fingerprint':id,'version':1}
        self.store.save_article(article)
        article=next(a for a in self.store.articles() if a['id']==article['id'])
        eid='e_'+hashlib.sha256(cluster_key(article).encode()).hexdigest()[:24]
        snapshot=self.store.capture_event_generation([{'id':eid,'articles':[article]}])
        draft={'id':eid,'headline':headline or title,'summary':'隔离验收资料。','analysis':'执行仍取决于具体条件。',
               'uncertainty':'这不是实际市场发布。',
               'facts':[{'text':'隔离验收资料','quote':'隔离验收资料','article_id':article['id']}]}
        editor=MarketEditor(fixtures.FixedClient({'events':[draft]}),
                            fixtures.FixedClient({'verdict':'approve','reason':'隔离原文及草稿绑定通过。','issues':[]}))
        event=editor.events(snapshot['groups'],'unique-reports',generations=snapshot['generations'])[0]
        event['input_hash']=event_input_hash(snapshot['groups'][0]);self.store._record_event_review(event)
        self.store.publish('market_events',eid,event,event['input_hash'])
        return eid

    def test_duplicate_opening_cannot_complete_six_and_seventh_can_replace(self):
        for i in range(4):self.report(str(i),'隔离独立经济资料 '+str(i))
        self.report('forum-a','金融街专题原稿甲','第十五届金融街论坛年会开幕')
        self.report('forum-b','金融街专题原稿乙','2026金融街论坛年会开幕：聚焦开放合作')
        result=self.store.update_daily(now=self.now,run_id='incomplete',valid_check=True,coverage={'required_count':6})
        self.assertEqual(result['count'],5)
        self.assertEqual(result['skipped'],'daily_target_not_met')
        self.report('seventh','新的隔离独立产业资料')
        result=self.store.update_daily(now=self.now,run_id='complete',valid_check=True,coverage={'required_count':6})
        self.assertEqual(result['count'],6)
        events=self.store.hotspots(now=self.now)['events']
        self.assertEqual(len(events),6)
        self.assertEqual(sum('论坛年会开幕' in e['headline'] for e in events),1)

    def test_new_daily_six_are_selected_automatically_on_three_consecutive_days(self):
        snapshots=[]
        for day in range(3):
            ids={self.report(str(day)+'-'+str(i),'隔离独立经济资料 '+str(day)+'-'+str(i)) for i in range(6)}
            result=self.store.update_daily(now=self.now,run_id='day-'+str(day),valid_check=True,coverage={'required_count':6})
            events=self.store.hotspots(now=self.now)['events']
            self.assertEqual(result['count'],6)
            self.assertEqual({e['id'] for e in events},ids)
            snapshots.append((self.now.date().isoformat(),copy.deepcopy(self.store.history_day(self.now.date().isoformat(),now=self.now))))
            self.now+=timedelta(days=1)
        for day,old in snapshots:
            self.assertEqual(self.store.history_day(day,now=self.now)['events'],old['events'])
        self.assertEqual(len({e['id'] for _,snapshot in snapshots for e in snapshot['events']}),18)


if __name__=='__main__':unittest.main()
