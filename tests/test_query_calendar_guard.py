"""Synthetic calendar/numeric boundary tests; no model or acceptance data."""
import json
import unittest
from unittest.mock import patch

from local_knowledge import query_translation as qt


class CalendarLiteralGuardTests(unittest.TestCase):
    def test_each_full_month_name(self):
        for month, name in enumerate(qt.CALENDAR_MONTH_NAMES, 1):
            with self.subTest(month=month):
                self.assertTrue(qt._numeric_literals_equal(f'央行{month}月声明', f'The statement in {name}.'))

    def test_month_and_year_amount_percentage_unchanged(self):
        self.assertTrue(qt._numeric_literals_equal('2027年9月金额20元，费率2.5%',
                                                 'In September 2027 the amount is20 yuan and the fee is2.5%.'))

    def test_amount_cannot_become_month(self):
        for source in ('贷款9元', '费率9%', '产品代码9', '期限9个月', '9月期产品', '9月龄'):
            with self.subTest(source=source):
                self.assertFalse(qt._numeric_literals_equal(source, 'In September.'))

    def test_changed_or_extra_month_refused(self):
        for output in ('In October.', 'In September and October.', 'In September and September.', 'In month9.'):
            with self.subTest(output=output):
                self.assertFalse(qt._numeric_literals_equal('9月声明', output))

    def test_month_multiplicity_preserved(self):
        self.assertTrue(qt._numeric_literals_equal('9月和10月，另一个9月', 'In September, October, and September.'))
        self.assertFalse(qt._numeric_literals_equal('9月和9月', 'In September.'))

    def test_non_calendar_numbers_remain_exact(self):
        source = '9月2027年金额20元费率2.5%'
        for output in ('In September2028 amount20 rate2.5%',
                       'In September2027 amount21 rate2.5%',
                       'In September2027 amount20 rate2.6%',
                       'In September2027 amount20 rate2.5% and5',
                       'In September2027 amount20.0 rate2.5%'):
            with self.subTest(output=output):
                self.assertFalse(qt._numeric_literals_equal(source, output))

    def test_without_source_month_no_month_number_inference(self):
        self.assertFalse(qt._numeric_literals_equal('金额9元，2027年', 'September2027.'))
        self.assertTrue(qt._numeric_literals_equal('政策2027年', 'Policy in September2027.'))

    def test_chinese_number_words_quarters_and_invalid_month_not_inferred(self):
        for source in ('九月9元', '三季度9元', '13月', '19月', '1.9月'):
            with self.subTest(source=source):
                self.assertFalse(qt._numeric_literals_equal(source, 'In September.'))

    def test_exact_month_case_and_word_boundaries(self):
        for output in ('In september.', 'In Sept.', 'In xSeptember.', 'In Septemberish.'):
            with self.subTest(output=output):
                self.assertFalse(qt._numeric_literals_equal('9月', output))

    def test_ambiguous_may_march_conservative(self):
        self.assertFalse(qt._numeric_literals_equal('5月', 'May the rate fall.'))
        self.assertFalse(qt._numeric_literals_equal('3月', 'March forward.'))
        self.assertTrue(qt._numeric_literals_equal('5月2027年', 'May2027.'))
        self.assertTrue(qt._numeric_literals_equal('3月9日', '9 March.'))

    def test_date_day_stays_a_separate_exact_number(self):
        self.assertTrue(qt._numeric_literals_equal('2027年9月22日', 'September22,2027.'))
        self.assertFalse(qt._numeric_literals_equal('2027年9月22日', 'September23,2027.'))

    def test_profile_is_pure_and_contains_guard(self):
        with patch.object(qt, 'get_query_translator', side_effect=AssertionError('must remain lazy')):
            metadata = qt.translation_profile_metadata()
        self.assertEqual(metadata['calendar_literal_guard']['version'], 'strict-calendar-month-literals/1')
        self.assertIn('calendar-literals-v2', metadata['algorithm_policy'])
        self.assertFalse(metadata['authority'])
        json.dumps(metadata)


if __name__ == '__main__':
    unittest.main()
