"""同一次统计发布不能凭跨标题、跨来源解读占据两个每日名额。"""
import copy
import unittest
from market_daily import event_aliases
from test_market_daily_unique import alias_event


def gdp_event(id, title, summary=None, growth='4.6', period='前三季度', year='2026'):
    e=alias_event(id,title,publisher='来源'+id)
    e['articles'][0]['published_at']='2026-10-20T08:23:34+08:00'
    quote=f'{year}年{period}国内生产总值同比增长{growth}%。'
    e['summary']=summary or '国家统计局发布'+quote
    e['facts']=[{'article_id':id,'quote':quote,'text':quote}]
    return e


class StatisticalReleaseTests(unittest.TestCase):
    def test_two_current_different_headlines_share_one_release(self):
        a=gdp_event('a','透视“十五五”开局之年中国经济三季报：前三季度GDP同比增长4.6%')
        b=gdp_event('b','超107万亿元，前三季度经济运行看点多',
                    '国家统计局10月19日发布前三季度经济数据：国内生产总值1071071亿元，同比增长4.6%。')
        self.assertEqual(event_aliases(a)&event_aliases(b),{'cn_gdp_release:2026:q1-q3:4.6'})

    def test_different_sources_and_decimal_spelling_share_release(self):
        a=gdp_event('a','中国前三季度GDP同比增长4.6%')
        b=gdp_event('b','我国经济三季报：前三季度国内生产总值增长4.60%',growth='4.60')
        self.assertTrue(event_aliases(a)&event_aliases(b))

    def test_other_year_period_and_growth_are_not_the_same_release(self):
        a=gdp_event('a','2026年前三季度中国GDP同比增长4.6%')
        for e in [gdp_event('b','2025年前三季度中国GDP同比增长4.6%',year='2025'),
                  gdp_event('c','2026年三季度中国GDP同比增长4.6%',period='三季度'),
                  gdp_event('d','2026年前三季度中国GDP同比增长4.7%',growth='4.7')]:
            with self.subTest(title=e['headline']):self.assertFalse(event_aliases(a)&event_aliases(e))

    def test_foreign_gdp_does_not_merge_with_chinese_release(self):
        a=gdp_event('a','中国前三季度GDP同比增长4.6%')
        b=gdp_event('b','美国前三季度GDP同比增长4.6%',summary='美国发布前三季度GDP数据，同比增长4.6%。')
        self.assertFalse(event_aliases(a)&event_aliases(b))

    def test_forecasts_targets_and_separate_policy_decisions_stay_independent(self):
        a=gdp_event('a','中国前三季度GDP同比增长4.6%')
        for title in ['预计中国前三季度GDP同比增长4.6%','中国GDP增长目标4.6%',
                      '央行宣布降息，支持前三季度GDP增长4.6%']:
            b=gdp_event('b',title)
            with self.subTest(title=title):self.assertFalse(event_aliases(a)&event_aliases(b))

    def test_unrelated_theme_with_gdp_background_does_not_merge(self):
        a=gdp_event('a','中国前三季度GDP同比增长4.6%')
        b=gdp_event('b','银行制造业贷款投放取得新进展')
        self.assertFalse(event_aliases(a)&event_aliases(b))

    def test_core_growth_must_be_supported_by_primary_original_quote(self):
        a=gdp_event('a','中国前三季度GDP同比增长4.6%')
        b=gdp_event('b','超107万亿元，前三季度经济运行看点多')
        b['facts'][0]['quote']='居民消费价格同比上涨4.6%。'
        self.assertFalse(event_aliases(a)&event_aliases(b))
        b['facts'][0]['article_id']='unbound-other-article'
        self.assertFalse(event_aliases(a)&event_aliases(b))

    def test_year_is_not_guessed_for_previous_year_report_in_january(self):
        a=gdp_event('a','2026年前三季度中国GDP同比增长4.6%')
        b=gdp_event('b','前三季度中国GDP同比增长4.6%',summary='国家统计局公布前三季度国内生产总值同比增长4.6%。')
        b['articles'][0]['published_at']='2027-01-20T08:23:34+08:00'
        self.assertFalse(event_aliases(a)&event_aliases(b))


if __name__=='__main__':unittest.main()
