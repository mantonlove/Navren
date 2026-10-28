"""同一纽约交易日三大股指收盘占一个事件，其他主题保持独立。"""
import copy
import unittest

from market_daily import distinct_event_count, event_aliases
from market_sources import cluster_key
from market_statistical_identity import source_release_aliases
from test_market_daily_unique import alias_event


XINHUA = (
    '新华社纽约10月27日电 纽约股市三大股指27日上涨。'
    '截至当天收盘，道琼斯工业平均指数上涨15.99点，收于52377.57点；'
    '标准普尔500种股票指数上涨45.83点，收于7722.60点；'
    '纳斯达克综合指数上涨308.43点，收于26477.81点。'
)
YICAI = (
    '人工智能推动的科技股上涨带动美国股市周二上涨。'
    '截至收盘，道琼斯工业平均指数上涨15.99点，报52377.57点，'
    '纳斯达克综合指数上涨1.18%，报26477.81点，'
    '标普500指数涨0.6%，报7722.60点。'
)
ALIAS = 'market_close:us_major_indices:2026-10-27'


def market_event(article_id, title, headline, evidence, published_at, publisher):
    event = alias_event(article_id, title, headline, publisher=publisher)
    article = event['articles'][0]
    article.update(source_id='xinhua' if publisher == '新华网' else 'yicai',
                   identity='media_report', evidence_text=evidence,
                   published_at=published_at, original_published_at=published_at,
                   first_publication_status='verified_original_page',
                   read_scope='full_text', evidence_truncated=False)
    return event


class USMarketCloseAliasTests(unittest.TestCase):
    def setUp(self):
        self.xinhua = market_event(
            'xinhua-ny', '纽约股市三大股指27日上涨',
            '纽约股市三大股指10月27日收涨，科技板块领涨', XINHUA,
            '2026-10-27T20:40:22+00:00', '新华网')
        self.yicai = market_event(
            'yicai-ny', 'AI交易发力美股两连阳，金银闪耀原油跳水',
            'AI交易带动美股两连阳，标普500重回7700点上方', YICAI,
            '2026-10-27T23:04:29+00:00', '第一财经')

    def test_original_day_and_three_closing_endpoints_merge_cross_source(self):
        self.assertEqual(event_aliases(self.xinhua) & event_aliases(self.yicai), {ALIAS})
        self.assertEqual(distinct_event_count([self.xinhua, self.yicai]), 1)

    def test_article_cluster_alone_does_not_inherit_us_close_identity(self):
        article = self.yicai['articles'][0]
        self.assertNotIn(ALIAS, source_release_aliases({'facts': []}, article))
        self.assertNotEqual(cluster_key(article), ALIAS)

    def test_market_study_oil_london_and_company_announcement_stay_separate(self):
        examples = (
            ('盘前必读丨美股存储、光通信板块延续强势',
             '高盛研究称半导体板块对资产价格分化的解释力超过油价'),
            ('美股收盘回顾：上一交易日三大股指表现', '美股收盘回顾'),
            ('国际油价27日下跌', '国际油价10月27日收盘下跌'),
            ('伦敦股市27日下跌', '伦敦股市27日收跌'),
            ('苹果公司发布季度业绩，美股个股上涨', '苹果公司发布季度业绩'),
        )
        for i, (title, headline) in enumerate(examples):
            with self.subTest(title=title):
                other = market_event(str(i), title, headline, YICAI,
                                     '2026-10-27T23:04:29+00:00', '另一来源')
                self.assertFalse(event_aliases(self.xinhua) & event_aliases(other))
        study = copy.deepcopy(self.yicai)
        study['headline'] = '高盛研究称半导体板块解释资产价格分化'
        self.assertNotIn(ALIAS, event_aliases(study))

    def test_weekday_must_match_verified_new_york_publication_day(self):
        other = copy.deepcopy(self.yicai)
        other['articles'][0]['original_published_at'] = '2026-10-28T23:04:29+00:00'
        other['articles'][0]['published_at'] = '2026-10-28T23:04:29+00:00'
        self.assertNotIn(ALIAS, event_aliases(other))
        other = copy.deepcopy(self.yicai)
        other['articles'][0]['evidence_text'] = YICAI.replace('周二', '周三')
        self.assertNotIn(ALIAS, event_aliases(other))

    def test_ambiguous_or_wrong_original_date_does_not_merge(self):
        other = copy.deepcopy(self.xinhua)
        other['articles'][0]['evidence_text'] = XINHUA.replace('10月27日电', '10月26日电')
        self.assertNotIn(ALIAS, event_aliases(other))
        other['articles'][0]['evidence_text'] = XINHUA.replace('10月27日电', '10月26日、10月27日电')
        self.assertNotIn(ALIAS, event_aliases(other))

    def test_missing_original_clock_or_one_index_close_does_not_merge(self):
        other = copy.deepcopy(self.yicai)
        other['articles'][0]['first_publication_status'] = 'unknown'
        self.assertNotIn(ALIAS, event_aliases(other))
        other = copy.deepcopy(self.yicai)
        other['articles'][0]['original_published_at'] = '2026-10-28T00:00:00+00:00'
        self.assertNotIn(ALIAS, event_aliases(other))
        other['articles'][0]['original_published_at'] = '2026-10-27'
        self.assertNotIn(ALIAS, event_aliases(other))
        other = copy.deepcopy(self.yicai)
        other['articles'][0]['evidence_text'] = YICAI.replace('标普500指数涨0.6%，报7722.60点。', '')
        self.assertNotIn(ALIAS, event_aliases(other))


if __name__ == '__main__':
    unittest.main()
