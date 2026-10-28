"""No live provider calls: old public action versus genuinely new selected news."""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from market_daily import DailyMarket
from market_pipeline import held_old_announcement_groups
from market_semantic_checks import old_announcement_headline_issues, validate_market_semantics


def article(version=1):
    return {'id': 'a_demo', 'version': version,
            'title': '新闻分析丨这项协议为何受到关注',
            'published_at': '2026-10-28T07:11:16+00:00',
            'first_publication_status': 'verified_original_page',
            'evidence_text': ('新华社10月28日电。甲公司22日宣布，与乙公司签署长期协议，'
                              '采购规模由协议约定。分析人士解释合作背景。')}


def selected():
    return {'id': 'e_demo', 'headline': '甲公司与乙公司签署长期协议',
            'summary': '甲公司22日宣布与乙公司签署长期协议，采购规模由协议约定。',
            'facts': [{'article_id': 'a_demo', 'quote': '甲公司22日宣布，与乙公司签署长期协议',
                       'text': '甲公司22日宣布与乙公司签署长期协议。'}],
            'article_ids': ['a_demo'], 'article_versions': {'a_demo': 1}}


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()


class OldAnnouncementGuardTests(unittest.TestCase):
    def test_selected_old_action_is_rejected_before_review(self):
        event, source = selected(), article()
        self.assertEqual(old_announcement_headline_issues(event, [source])[0]['code'],
                         'selected_old_announcement_without_new_fact')
        with self.assertRaisesRegex(ValueError, 'selected_old_announcement_without_new_fact'):
            validate_market_semantics(event, [source])

    def test_yesterday_market_close_is_not_an_old_announcement(self):
        event = selected()
        event.update(headline='27日美股收盘', summary='27日美股收盘，28日发表市场回顾。',
                     facts=[{'article_id': 'a_demo', 'quote': '27日美股收盘', 'text': '27日美股收盘。'}])
        source = article()
        source['evidence_text'] = '新华社10月28日电。27日美股收盘，主要指数上涨。'
        self.assertFalse(old_announcement_headline_issues(event, [source]))

    def test_old_background_with_new_progress_or_interview_is_not_rejected(self):
        event, source = selected(), article()
        event['headline'] = '甲公司披露协议新增条款'
        event['summary'] = '甲公司22日宣布签署协议，28日披露新增条款。'
        event['facts'].append({'article_id': 'a_demo', 'quote': '28日披露新增条款',
                               'text': '28日披露新增条款。'})
        source['evidence_text'] += ' 甲公司28日披露新增条款。'
        self.assertFalse(old_announcement_headline_issues(event, [source]))
        event['headline'] = '记者专访甲公司负责人谈协议影响'
        self.assertFalse(old_announcement_headline_issues(event, [source]))

    def test_undated_new_main_fact_with_old_background_is_not_rejected(self):
        event, source = selected(), article()
        source['evidence_text'] += ' 甲公司与丙公司签署追加协议。'
        event['headline'] = '甲公司与丙公司签署追加协议'
        event['summary'] = ('甲公司与丙公司签署追加协议。此前甲公司22日宣布与乙公司签署长期协议。')
        event['facts'].insert(0, {'article_id': 'a_demo',
                                  'quote': '甲公司与丙公司签署追加协议',
                                  'text': '甲公司与丙公司签署追加协议。'})
        self.assertFalse(old_announcement_headline_issues(event, [source]))

    def test_page_dateline_or_today_label_is_not_new_selected_evidence(self):
        event, source = selected(), article()
        event['headline'] = '今日甲公司与乙公司签署长期协议'
        event['facts'].append({'article_id': 'a_demo',
                               'quote': '新华社10月28日电甲公司22日宣布',
                               'text': '报道发于10月28日。'})
        source['evidence_text'] = source['evidence_text'].replace(
            '新华社10月28日电。甲公司22日宣布',
            '新华社10月28日电甲公司22日宣布')
        self.assertTrue(old_announcement_headline_issues(event, [source]))
        event['headline'] = '最新进展：甲公司与乙公司签署长期协议'
        self.assertTrue(old_announcement_headline_issues(event, [source]))

    def test_ambiguous_cross_month_and_unverified_page_fail_open(self):
        event, source = selected(), article()
        source['published_at'] = '2026-11-01T07:11:16+00:00'
        event['summary'] = '甲公司30日宣布与乙公司签署协议。'
        event['facts'][0]['quote'] = '甲公司30日宣布，与乙公司签署长期协议'
        source['evidence_text'] = source['evidence_text'].replace('22日', '30日')
        self.assertFalse(old_announcement_headline_issues(event, [source]))
        source = article()
        source['first_publication_status'] = 'unknown'
        self.assertFalse(old_announcement_headline_issues(selected(), [source]))
        source = article()
        source['title'] = None
        self.assertFalse(old_announcement_headline_issues(selected(), [source]))

    def test_exact_quarantine_hold_resumes_after_source_change(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(str(Path(folder) / 'market.sqlite3'))
            source = article()
            raw_article = json.dumps(source, ensure_ascii=False, sort_keys=True)
            article_digest = sha256(raw_article.encode()).hexdigest()
            event = selected()
            event['generation'] = {'article_bindings': [
                {'article_id': source['id'], 'version': source['version'],
                 'article_body_sha256': article_digest}]}
            raw_event = json.dumps(event, ensure_ascii=False, sort_keys=True)
            event_digest = sha256(raw_event.encode()).hexdigest()
            with store.db() as db:
                db.executescript('''
                    CREATE TABLE market_articles(id TEXT,version INTEGER,body TEXT,status TEXT);
                    CREATE TABLE market_events(id TEXT,version INTEGER,body TEXT,status TEXT);
                    CREATE TABLE market_editorial_quarantines(
                        event_id TEXT,event_version INTEGER,body_sha256 TEXT,reason TEXT);
                ''')
                db.execute('INSERT INTO market_articles VALUES(?,?,?,?)',
                           ('a_demo', 1, raw_article, 'active'))
                db.execute('INSERT INTO market_events VALUES(?,?,?,?)',
                           ('e_demo', 1, raw_event, 'editorial_review_required'))
                db.execute('INSERT INTO market_editorial_quarantines VALUES(?,?,?,?)',
                           ('e_demo', 1, event_digest, '旧公告待复核'))
                self.assertTrue(DailyMarket(None).selected_substory_unverified(db, event))
            group = {'id': 'e_demo', 'articles': [source]}
            self.assertEqual(held_old_announcement_groups(store, [group]), {'e_demo'})
            scope_only = dict(event)
            scope_only['headline'] = '甲公司介绍协议后续影响'
            scope_only['summary'] = '甲公司介绍协议的后续影响。'
            raw_scope = json.dumps(scope_only, ensure_ascii=False, sort_keys=True)
            with store.db() as db:
                db.execute('UPDATE market_events SET body=? WHERE id=?', (raw_scope, 'e_demo'))
                db.execute('UPDATE market_editorial_quarantines SET body_sha256=? WHERE event_id=?',
                           (sha256(raw_scope.encode()).hexdigest(), 'e_demo'))
            self.assertEqual(held_old_announcement_groups(store, [group]), set())
            with store.db() as db:
                db.execute('UPDATE market_events SET body=? WHERE id=?', (raw_event, 'e_demo'))
                db.execute('UPDATE market_editorial_quarantines SET body_sha256=? WHERE event_id=?',
                           (event_digest, 'e_demo'))
            updated = article(version=2)
            updated['evidence_text'] += ' 甲公司28日披露新增条款。'
            with store.db() as db:
                db.execute('INSERT INTO market_articles VALUES(?,?,?,?)',
                           ('a_demo', 2, json.dumps(updated, ensure_ascii=False, sort_keys=True), 'active'))
            self.assertEqual(held_old_announcement_groups(store, [{'id': 'e_demo', 'articles': [updated]}]), set())


if __name__ == '__main__':
    unittest.main()
