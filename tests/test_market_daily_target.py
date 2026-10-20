"""固定六条：当日候选、审核失败备选、完整日版门槛。全部隔离且无外部调用。"""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_market_same_day as fixtures
from market_content import CN
from market_daily import MarketDailyError
from market_editor import MarketEditor
from market_pipeline import daily_hotspot_candidates, refresh
from market_sources import REGISTRY, discover


class TargetTests(unittest.TestCase):
    setUp = fixtures.SameDayTests.setUp
    add = fixtures.SameDayTests.add

    def fill(self, count):
        for i in range(count):
            self.add('target' + str(i), self.now - timedelta(minutes=i))

    def update_target(self, run='target'):
        return self.store.update_daily(now=self.now, run_id=run, valid_check=True,
                                       coverage={'required_count': 6})

    def test_incomplete_day_is_not_a_new_edition(self):
        self.fill(5)
        result = self.update_target()
        self.assertEqual(result['skipped'], 'daily_target_not_met')
        self.assertEqual(result['count'], 5)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_daily_editions').fetchone()[0], 0)

    def test_six_publishes_and_idempotent_read_still_reports_six(self):
        self.fill(6)
        self.assertEqual(self.update_target()['count'], 6)
        repeated = self.update_target('second')
        self.assertFalse(repeated['changed'])
        self.assertEqual(repeated['count'], 6)
        self.assertEqual(len(self.store.hotspots(now=self.now)['events']), 6)

    def test_failed_new_day_keeps_previous_complete_edition_and_dates(self):
        self.fill(6)
        self.update_target()
        old = self.store.history_day('2026-10-18', now=self.now)
        self.now += timedelta(days=1)
        self.add('new-today', self.now)
        self.assertEqual(self.update_target('next-day')['skipped'], 'daily_target_not_met')
        self.assertEqual(self.store.history_day('2026-10-19', now=self.now)['status'], 'missing_archive')
        previous = self.store.history_day('2026-10-18', now=self.now)
        self.assertEqual(previous['events'], old['events'])

    def test_withdrawn_source_can_be_replaced_by_approved_seventh(self):
        self.fill(7)
        self.update_target()
        before = self.store.hotspots(now=self.now)['events']
        removed = before[0]['primary_article_id']
        self.store.withdraw_article(removed, 'isolated_target_test')
        after = self.update_target('replacement')
        self.assertEqual(after['count'], 6)
        selected = self.store.hotspots(now=self.now)['events']
        self.assertEqual(len(selected), 6)
        self.assertNotIn(removed, [e['primary_article_id'] for e in selected])

    def test_incomplete_check_cannot_replace_existing_edition_with_five(self):
        self.fill(6)
        self.update_target()
        removed = self.store.hotspots(now=self.now)['events'][0]['primary_article_id']
        self.store.withdraw_article(removed, 'isolated_target_test')
        self.assertEqual(self.update_target('lost-source')['skipped'], 'daily_target_not_met')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT MAX(revision) FROM market_daily_editions').fetchone()[0], 1)

    def test_target_cannot_be_lowered_or_loosely_typed(self):
        for value in (True, 1, 5, '6', 6.0):
            with self.subTest(value=value), self.assertRaises(MarketDailyError):
                self.store.update_daily(now=self.now, run_id=str(value), valid_check=True,
                                        coverage={'required_count': value})

    def run_pipeline(self, editor):
        current = self.now

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return current.astimezone(tz) if tz else current.replace(tzinfo=None)

        with patch('market_pipeline.datetime', Clock), patch('market_daily.datetime', Clock), \
                patch('market_pipeline.discover', return_value=([], {'source_id': 'nbs', 'status': 'ok', 'checked_at': current.isoformat()})):
            return refresh(self.store, editor=editor, hotspots_only=True, sources=[REGISTRY['nbs']])

    def test_existing_six_stops_unnecessary_model_generation(self):
        self.fill(6)

        class NeverGenerate:
            def events(self, *args, **kwargs):
                raise AssertionError('generation_after_complete')

        result = self.run_pipeline(NeverGenerate())
        self.assertEqual(result['streams']['hotspots']['status'], 'ok')
        self.assertEqual(result['daily_edition']['count'], 6)
        self.assertFalse(any(r.get('stage') == 'event_review' for r in result['rejected']))

    def test_rejected_candidate_is_replaced_without_loosening_review(self):
        self.fill(7)
        with self.store.db() as db:
            db.execute('DELETE FROM market_events')

        class BoundEditor:
            calls = 0

            def events(self, groups, request_id, *, generations):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError('independent_review_rejected')
                g = groups[0]
                draft = {'id': g['id'], 'headline': g['articles'][0]['title'],
                         'summary': '隔离验收资料。', 'analysis': '执行仍取决于具体条件。',
                         'uncertainty': '这不是实际市场发布。',
                         'facts': [{'text': '隔离验收资料', 'quote': '隔离验收资料',
                                    'article_id': g['articles'][0]['id']}]}
                primary = fixtures.FixedClient({'events': [draft]})
                reviewer = fixtures.FixedClient({'verdict': 'approve', 'reason': '隔离原文与草稿绑定通过。', 'issues': []})
                return MarketEditor(primary, reviewer).events(groups, request_id, generations=generations)

        editor = BoundEditor()
        result = self.run_pipeline(editor)
        self.assertEqual(editor.calls, 7)
        self.assertEqual(result['streams']['hotspots']['status'], 'ok')
        self.assertEqual(result['daily_edition']['count'], 6)
        self.assertEqual(len(self.store.hotspots(now=self.now)['events']), 6)
        self.assertEqual(sum(r.get('stage') == 'event_review' for r in result['rejected']), 1)

    def test_five_verified_items_do_not_complete_the_slot(self):
        self.fill(5)

        class Reject:
            def events(self, *args, **kwargs):
                raise ValueError('independent_review_rejected')

        result = self.run_pipeline(Reject())
        self.assertEqual(result['streams']['hotspots']['status'], 'partial')
        self.assertFalse(result['streams']['hotspots']['publication_complete'])
        self.assertEqual(result['daily_edition']['skipped'], 'daily_target_not_met')


class CandidateTests(unittest.TestCase):
    now = datetime(2026, 10, 19, 12, 30, tzinfo=CN)

    def group(self, id, when, **extra):
        return {'id': id, 'articles': [{'published_at': when.isoformat(),
                                      'source_id': 'nbs', 'identity': 'official_release', **extra}]}

    def test_old_high_ranked_groups_do_not_starve_today_backups(self):
        old = [self.group('old' + str(i), self.now - timedelta(days=1)) for i in range(6)]
        today = [self.group('new' + str(i), self.now - timedelta(minutes=i)) for i in range(8)]
        selected = daily_hotspot_candidates(old + today, self.now)
        self.assertEqual([g['id'] for g in selected], [g['id'] for g in today])

    def test_future_unknown_and_update_only_are_not_candidates(self):
        bad = [self.group('future', self.now + timedelta(seconds=1)),
               self.group('updated', self.now, reported_time_kind='source_page_updated_at'),
               {'id': 'unknown', 'articles': [{}]}]
        self.assertEqual(daily_hotspot_candidates(bad, self.now), [])

    def test_shanghai_day_and_first_primary_are_preserved(self):
        utc_today = self.group('today', datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc))
        repost = self.group('repost', self.now - timedelta(days=1))
        repost['articles'].append(utc_today['articles'][0])
        self.assertEqual([g['id'] for g in daily_hotspot_candidates([repost, utc_today], self.now)], ['today'])

    def test_candidate_pool_is_bounded(self):
        groups = [self.group(str(i), self.now - timedelta(seconds=i)) for i in range(40)]
        self.assertEqual(len(daily_hotspot_candidates(groups, self.now)), 18)

    def test_financial_coverage_precedes_generic_and_promotional_copy(self):
        generic=self.group('generic',self.now,title='制造业人物访谈',identity='media_report')
        deposit=self.group('deposit',self.now,title='大额存单与存款变化',identity='media_report')
        promo=self.group('promo',self.now,title='财富发布会圆满收官',identity='media_report')
        selected=daily_hotspot_candidates([generic,promo,deposit],self.now)
        self.assertEqual([g['id'] for g in selected],['deposit','generic'])

    def test_media_list_discovers_new_date_before_old_high_relevance(self):
        source = REGISTRY['cctv']
        page = b'<a href="https://finance.cctv.com/2026/10/18/ARTIoldnews.shtml">old banking policy</a><a href="https://finance.cctv.com/2026/10/19/ARTItodaynews.shtml">today new release</a>'
        with patch('market_sources.fetch', return_value=(page, source.url, 'text/html')):
            entries, _ = discover(source, limit=1)
        self.assertIn('/2026/10/19/', entries[0]['url'])


if __name__ == '__main__':
    unittest.main()
