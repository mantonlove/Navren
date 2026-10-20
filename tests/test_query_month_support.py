"""Independent synthetic month-support boundaries; no model/acceptance-data reads."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from local_knowledge import query_translation as qt


def deterministic_translator(output):
    """Run the real translation exit with deterministic valid decoder tensors."""
    translator = object.__new__(qt.QueryTranslator)
    translator._np = np
    translator._lock = threading.Lock()
    translator._tokenizer = SimpleNamespace(encode=Mock(return_value=SimpleNamespace(ids=[2], tokens=['piece'])))
    translator._joint = {'piece': 2}
    translator._pieces = ['</s>', '<unk>', 'piece']
    translator._decoder = SimpleNamespace(decode=Mock(return_value=output))
    translator._encoder = object()
    translator._encoder_output = 'hidden'
    translator._merged = object()
    translator._cache_names = {f'past_key_values.{layer}.{side}.{kind}'
        for layer in range(qt.LAYERS) for side in ('encoder', 'decoder') for kind in ('key', 'value')}
    translator._decoder_outputs = ['logits'] + [
        name.replace('past_key_values.', 'present.') for name in sorted(translator._cache_names)]
    steps = iter((2, 0))

    def run(session, names, feeds, deadline):
        if session is translator._encoder:
            return [np.zeros((1, feeds['input_ids'].shape[1], qt.DIMENSION), dtype=np.float32)]
        token = next(steps)
        logits = np.zeros((1, 1, qt.VOCABULARY_SIZE), dtype=np.float32)
        logits[0, 0, token] = 1.
        outputs = {'logits': logits}
        for name in translator._cache_names:
            length = feeds['encoder_hidden_states'].shape[1] if '.encoder.' in name else token == 2
            outputs[name.replace('past_key_values.', 'present.')] = np.zeros(
                (1, qt.ATTENTION_HEADS, int(length), qt.DIMENSION // qt.ATTENTION_HEADS), dtype=np.float32)
        return [outputs[name] for name in names]

    translator._run = Mock(side_effect=run)
    return translator


class MonthSupportTests(unittest.TestCase):
    def test_all_explicit_numeric_months_supported(self):
        for number, name in enumerate(qt.CALENDAR_MONTH_NAMES, 1):
            with self.subTest(month=number):
                self.assertTrue(qt._calendar_months_supported(f'央行{number}月声明', f'Statement in {name}.'))

    def test_all_finite_chinese_word_months_and_month_suffix_supported(self):
        words = ('一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二')
        for word, name in zip(words, qt.CALENDAR_MONTH_NAMES):
            for suffix in ('月', '月份'):
                with self.subTest(month=word, suffix=suffix):
                    self.assertTrue(qt._calendar_months_supported(f'央行{word}{suffix}声明', f'Statement in {name}.'))

    def test_introduced_month_rejected_even_when_old_numeric_guard_passes(self):
        original, output = '政策2027年', 'Policy in September2027.'
        self.assertTrue(qt._numeric_literals_equal(original, output))
        self.assertFalse(qt._calendar_months_supported(original, output))

    def test_quarters_relative_and_invalid_months_do_not_supply_anchors(self):
        for original in ('第三季度声明', '上月声明', '下月声明', '本月声明', '13月声明',
                         '19月声明', '1.9月声明', '-9月声明', '十九月声明', '二十九月声明'):
            with self.subTest(original=original):
                self.assertFalse(qt._calendar_months_supported(original, 'Statement in September.'))

    def test_explicit_duration_prefixes_do_not_supply_calendar_anchors(self):
        for original in ('期限为9月', '时长约九月', '存期是十月', '持有期至少9月',
                         '锁定期：九月份', '封闭期9月', '存续期为九月', '历时九月',
                         '长达九月', '持续9月', '9月期产品', '九月期产品', '9个月期限'):
            name = 'October' if '十月' in original else 'September'
            with self.subTest(original=original):
                self.assertFalse(qt._calendar_months_supported(original, f'In {name}.'))

    def test_explicit_date_after_duration_word_is_a_calendar_anchor(self):
        for original in ('期限截至9月', '期限为2027年9月', '9月声明，存期9个月'):
            with self.subTest(original=original):
                self.assertTrue(qt._calendar_months_supported(original, 'In September.'))

    def test_calendar_and_duration_occurrences_are_counted_separately(self):
        original = '九月声明，另有期限为九月'
        self.assertTrue(qt._calendar_months_supported(original, 'Statement in September.'))
        self.assertFalse(qt._calendar_months_supported(original, 'In September and September.'))

    def test_exact_multiplicity_for_mixed_explicit_anchor_forms(self):
        original = '9月与十月，还有九月份'
        self.assertTrue(qt._calendar_months_supported(original, 'In September, October, and September.'))
        for output in ('In September and October.', 'In September, October, and October.',
                       'In September, October, September, and November.'):
            with self.subTest(output=output):
                self.assertFalse(qt._calendar_months_supported(original, output))

    def test_nonambiguous_lowercase_full_month_is_still_checked(self):
        self.assertFalse(qt._calendar_months_supported('政策2027年', 'Policy in september2027.'))
        self.assertTrue(qt._calendar_months_supported('九月政策', 'Policy in september.'))

    def test_may_and_march_are_conservative_without_confusing_lowercase_verbs(self):
        for original, output in (('五月声明', 'In May.'), ('三月声明', 'In march.'),
                                 ('五月2027年', 'May2027.'), ('三月9日', '9 March.')):
            with self.subTest(output=output):
                self.assertTrue(qt._calendar_months_supported(original, output))
        for original, output in (('央行声明', 'In May.'), ('央行声明', 'in march.'),
                                 ('五月声明', 'May the rate fall.'), ('三月声明', 'March forward.')):
            with self.subTest(output=output):
                self.assertFalse(qt._calendar_months_supported(original, output))
        self.assertTrue(qt._calendar_months_supported('政策变化', 'The policy may change.'))
        self.assertTrue(qt._calendar_months_supported('前进', 'Please march forward.'))

    def test_full_word_boundaries_do_not_claim_substring_months(self):
        for output in ('xSeptember', 'Septemberish', 'Sept.', 'Januaryish'):
            with self.subTest(output=output):
                self.assertTrue(qt._calendar_months_supported('没有日历信息', output))

    def test_without_output_month_this_layer_leaves_numeric_contract_separate(self):
        self.assertTrue(qt._calendar_months_supported('9月20元', 'A value of21.'))
        self.assertFalse(qt._numeric_literals_equal('9月20元', 'A value of21.'))
        self.assertTrue(qt._numeric_literals_equal('政策2027年', 'Policy in September2027.'))

    def test_real_translation_exit_rejects_introduced_month_and_releases_lock(self):
        translator = deterministic_translator('Policy in September2027.')
        original = '政策2027年'
        with self.assertRaises(qt.TranslationError) as caught:
            translator.translate(original)
        self.assertEqual(caught.exception.code, 'query_translation_numeric_mismatch')
        self.assertNotIn(original, str(caught.exception))
        self.assertFalse(translator._lock.locked())
        translator._tokenizer.encode.assert_called_once_with(original, add_special_tokens=False)

    def test_real_translation_exit_accepts_explicit_chinese_word_month(self):
        translator = deterministic_translator('The statement in September2027.')
        self.assertEqual(translator.translate('2027年九月份声明'), 'The statement in September2027.')

    def test_real_translation_exit_refuses_duration_reinterpretation(self):
        translator = deterministic_translator('In September.')
        self.assertTrue(qt._numeric_literals_equal('期限为9月', 'In September.'))
        with self.assertRaises(qt.TranslationError) as caught:
            translator.translate('期限为9月')
        self.assertEqual(caught.exception.code, 'query_translation_numeric_mismatch')

    def test_real_translation_exit_still_refuses_changed_other_numbers(self):
        translator = deterministic_translator('In September2028 amount20 rate2.5%.')
        with self.assertRaises(qt.TranslationError) as caught:
            translator.translate('2027年9月金额20元费率2.5%')
        self.assertEqual(caught.exception.code, 'query_translation_numeric_mismatch')

    def test_month_support_does_not_claim_year_relationship_fidelity(self):
        original, output = '2027年九月与2028年十月', 'In September2028 and October2027.'
        self.assertTrue(qt._calendar_months_supported(original, output))
        self.assertTrue(qt._numeric_literals_equal(original, output))

    def test_new_support_policy_is_pure_profile_bound_and_keeps_old_subpolicy(self):
        with patch.object(qt, 'get_query_translator', side_effect=AssertionError('must remain lazy')):
            metadata = qt.translation_profile_metadata()
            with patch.dict(qt.CALENDAR_SUPPORT_POLICY, {'version': 'fixture-changed-policy'}):
                changed = qt.translation_profile_metadata()
        self.assertNotEqual(metadata['profile_id'], changed['profile_id'])
        self.assertEqual(metadata['calendar_support_guard']['version'], 'explicit-source-month-support/1')
        self.assertEqual(metadata['calendar_literal_guard']['version'], 'strict-calendar-month-literals/1')
        self.assertIn('calendar-literals-v2', metadata['algorithm_policy'])
        self.assertEqual(len(metadata['artifacts']), 8)
        self.assertFalse(metadata['authority'])
        json.dumps(metadata)


if __name__ == '__main__':
    unittest.main()
