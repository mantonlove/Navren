"""公共机构覆盖选择器的独立合约测试。

仅使用本文件合成的公共候选，不读取原件缓存、验收题或个人数据。
这里检验明确机构解析、边界保护和已有候选的保序重排；不把来源覆盖
当作事实充分性，不改变向量分数、准入条件或应用层的结果数量。
"""
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch

from local_knowledge.public_institutions import (
    institution_policy_metadata,
    order_public_institution_coverage,
    resolve_public_institutions,
)
from local_knowledge.store import PARSER


FED = 'FEDERAL_RESERVE'
PBC = 'PEOPLE_BANK_OF_CHINA'
ECB = 'EUROPEAN_CENTRAL_BANK'
NBS = 'NATIONAL_BUREAU_OF_STATISTICS'
COMPARISON = '比较中国人民银行与美联储公布的政策框架'
OFFICIAL_ORIGINS = {
    'Federal Reserve': 'https://www.federalreserve.gov/',
    '中国人民银行': 'https://www.pbc.gov.cn/',
    'European Central Bank': 'https://www.ecb.europa.eu/',
    '国家统计局': 'https://www.stats.gov.cn/',
}


def public_row(cid, publisher, document=None, *, source=None, version=None, **changes):
    """与提供器 _rows 的准入字段一致，但没有真实金融资料。"""
    document = document or ('document-' + cid)
    row = {
        'id': cid,
        'title': '合成公共披露标题',
        'excerpt': 'Synthetic original public text, not financial advice.',
        'parser_id': PARSER,
        'shape': 'market-official-full-text-v1',
        'publisher': publisher,
        'source_id': source or ('source-' + publisher),
        'source_url': OFFICIAL_ORIGINS.get(publisher, 'https://synthetic.invalid/') + cid,
        'document_id': document,
        'version_id': version or ('version-' + document),
        'product_id': None,
        'share_class_id': None,
        'channel_id': None,
        'visibility': 'public',
        'data_origin': 'real',
        'status': 'verified',
        'enabled': 1,
        'read_allowed': 1,
        'outbound_allowed': 1,
    }
    row.update(changes)
    return row


def match(cid, score, facets=None):
    return (score, cid, facets if facets is not None else {'concept'})


class PublicInstitutionScopeTests(unittest.TestCase):
    def assert_scope(self, query, expected, *, ready=True, compound=None):
        scope = resolve_public_institutions(query)
        self.assertIsInstance(scope, dict)
        self.assertIs(scope['ready'], ready)
        self.assertEqual(set(scope['institution_ids']), set(expected))
        self.assertEqual(len(scope['institution_ids']), len(set(scope['institution_ids'])))
        self.assertIsInstance(scope['comparison_intent'], bool)
        self.assertIsInstance(scope['compound_used'], bool)
        self.assertIsInstance(scope['policy'], str)
        if compound is not None:
            self.assertIs(scope['compound_used'], compound)
        return scope

    def test_two_explicit_chinese_institutions_and_comparison_are_ready(self):
        self.assert_scope(COMPARISON, {PBC, FED}, compound=False)

    def test_english_full_names_are_supported_without_translating_query(self):
        self.assert_scope("Compare the Federal Reserve and People's Bank of China policies",
                          {FED, PBC}, compound=False)

    def test_english_matching_is_case_insensitive(self):
        self.assert_scope("COMPARE FEDERAL RESERVE versus PEOPLE'S BANK OF CHINA",
                          {FED, PBC}, compound=False)

    def test_full_ascii_word_boundaries_prevent_false_institution_matches(self):
        for disguised in ('PreFederal Reserve', 'Federal ReserveSuffix',
                          'xFEDx', 'ECBextra', 'PreEuropean Central Bank'):
            with self.subTest(disguised=disguised):
                scope = resolve_public_institutions('比较中国人民银行与 ' + disguised)
                self.assertFalse(scope['ready'])
                self.assertNotIn(FED, scope['institution_ids'])
                self.assertNotIn(ECB, scope['institution_ids'])

    def test_single_institution_with_each_committee_member_does_not_enable_coverage(self):
        scope = self.assert_scope('美国央行每位委员分别如何解释各自的投票？',
                                  {FED}, ready=False)
        self.assertTrue(scope['comparison_intent'])

    def test_several_names_of_one_institution_count_only_once(self):
        self.assert_scope('比较美联储与美国央行 Federal Reserve 的不同表述',
                          {FED}, ready=False)

    def test_two_institutions_without_comparison_intent_do_not_enable_coverage(self):
        scope = self.assert_scope('中国人民银行和美联储发布了公告',
                                  {PBC, FED}, ready=False)
        self.assertFalse(scope['comparison_intent'])

    def test_domestic_and_country_only_words_do_not_bind_an_institution(self):
        for vague in ('国内', '国内委员会', '美国', '美国议息', 'FOMC'):
            with self.subTest(vague=vague):
                self.assert_scope('比较中国人民银行与' + vague + '的政策',
                                  {PBC}, ready=False)

    def test_domestic_central_bank_is_not_assumed_to_mean_china(self):
        scope = resolve_public_institutions('比较国内央行与美联储的政策')
        self.assertFalse(scope['ready'])
        self.assertNotIn(PBC, scope['institution_ids'])
        self.assertFalse(scope['compound_used'])

    def test_registered_country_pair_can_resolve_in_both_orders(self):
        for term in ('中美央行', '美中央行'):
            with self.subTest(term=term):
                self.assert_scope(term + '的政策框架有哪些区别', {PBC, FED}, compound=True)

    def test_named_and_compound_references_are_deduplicated(self):
        self.assert_scope('比较中美央行与中国人民银行和美联储的政策框架',
                          {PBC, FED}, compound=True)

    def test_registered_countries_with_no_unique_registered_central_bank_fail_closed(self):
        for term in ('中英央行', '日美央行', '中德央行', '中法央行', '中俄央行'):
            with self.subTest(term=term):
                scope = resolve_public_institutions(term + '的政策有何区别')
                self.assertFalse(scope['ready'])
                self.assertFalse(scope['compound_used'])

    def test_unknown_or_ambiguous_country_symbol_fails_closed(self):
        for term in ('中韩央行', '中欧央行', '中越央行', '中美韩央行'):
            with self.subTest(term=term):
                scope = resolve_public_institutions(term + '的政策有何区别')
                self.assertFalse(scope['ready'])
                self.assertFalse(scope['compound_used'])

    def test_duplicate_country_in_compound_fails_closed(self):
        for term in ('中美美央行', '中中美央行', '中中央行'):
            with self.subTest(term=term):
                scope = resolve_public_institutions(term + '的政策有何区别')
                self.assertFalse(scope['ready'])
                self.assertFalse(scope['compound_used'])

    def test_single_country_symbol_is_not_a_compound(self):
        scope = resolve_public_institutions('比较美央行与中国人民银行')
        self.assertFalse(scope['ready'])
        self.assertFalse(scope['compound_used'])

    def test_invalid_compound_disables_otherwise_ready_named_institutions(self):
        for term in ('中欧央行', '中美美央行', '中韩央行'):
            with self.subTest(term=term):
                scope = resolve_public_institutions(
                    '比较' + term + '与中国人民银行及美联储的政策')
                self.assertFalse(scope['ready'])

    def test_canonical_national_statistics_bureau_has_no_central_bank_role(self):
        self.assert_scope('比较国家统计局与中国人民银行公布的材料', {NBS, PBC})

    def test_euro_area_central_bank_is_explicit_institution_not_country_symbol(self):
        self.assert_scope('比较欧洲央行与中国人民银行的政策框架', {ECB, PBC})

    def test_english_versus_comparison_is_supported(self):
        self.assert_scope('European Central Bank versus Federal Reserve', {ECB, FED})


class PublicInstitutionOrderingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            public_row('fed-a-1', 'Federal Reserve', 'fed-a', source='fed-one'),
            public_row('fed-a-2', 'Federal Reserve', 'fed-a', source='fed-one'),
            public_row('fed-b-1', 'Federal Reserve', 'fed-b', source='fed-two'),
            public_row('fed-b-2', 'Federal Reserve', 'fed-b', source='fed-two'),
            public_row('pbc-a-1', '中国人民银行', 'pbc-a', source='pbc-one'),
            public_row('pbc-a-2', '中国人民银行', 'pbc-a', source='pbc-one'),
            public_row('pbc-b-1', '中国人民银行', 'pbc-b', source='pbc-two'),
            public_row('ecb-a-1', 'European Central Bank', 'ecb-a'),
            public_row('unregistered-a-1', '合成未知发布机构', 'unknown-a'),
        ]
        self.matches = [
            match('fed-a-1', -.90),
            match('fed-a-2', -.88),
            match('ecb-a-1', -.86),
            match('fed-b-1', -.85),
            match('fed-b-2', -.84),
            match('unregistered-a-1', -.80),
            match('pbc-a-1', -.50),
            match('pbc-a-2', -.49),
            match('pbc-b-1', -.48),
        ]

    def reorder(self, *, query=COMPARISON, matches=None, rows=None):
        matches = self.matches if matches is None else matches
        rows = self.rows if rows is None else rows
        before_matches = deepcopy(matches)
        before_rows = deepcopy(rows) if all(isinstance(row, dict) for row in rows) else None
        ordered, diagnostics = order_public_institution_coverage(query, matches, rows)
        self.assertEqual(matches, before_matches, '重排不能修改传入的排名、分数或 facets')
        if before_rows is not None:
            self.assertEqual(rows, before_rows, '重排不能修改原件身份或准入字段')
        self.assertEqual(len(ordered), len(matches), '纯重排不能增加或删除候选')
        self.assertCountEqual([id(item) for item in ordered], [id(item) for item in matches],
                              '必须保留原 score/citation/facets tuple 对象')
        self.assertIsInstance(diagnostics, dict)
        self.assertIsInstance(diagnostics['used'], bool)
        self.assertIsInstance(diagnostics['prefix_candidate_count'], int)
        self.assertIsInstance(diagnostics['policy'], str)
        return ordered, diagnostics

    @staticmethod
    def ids(matches):
        return [item[1] for item in matches]

    def assert_disabled(self, *, query=COMPARISON, matches=None, rows=None):
        original = self.matches if matches is None else matches
        ordered, diag = self.reorder(query=query, matches=matches, rows=rows)
        self.assertFalse(diag['used'])
        self.assertEqual(self.ids(ordered), self.ids(original))
        self.assertEqual(diag['prefix_candidate_count'], 0)
        return diag

    def test_first_two_results_cover_two_explicit_institutions(self):
        ordered, diag = self.reorder()
        self.assertTrue(diag['used'])
        self.assertEqual(set(self.ids(ordered[:2])), {'fed-a-1', 'pbc-a-1'})
        self.assertEqual(set(diag['participating_institution_ids']), {FED, PBC})
        self.assertEqual(set(diag['institution_ids']), {FED, PBC})

    def test_second_document_from_each_institution_precedes_repeated_chunks(self):
        ordered, diag = self.reorder()
        self.assertEqual(set(self.ids(ordered[:4])),
                         {'fed-a-1', 'pbc-a-1', 'fed-b-1', 'pbc-b-1'})
        self.assertEqual(diag['prefix_candidate_count'], 4)

    def test_only_explicit_institutions_participate_in_prefix(self):
        ordered, diag = self.reorder()
        prefix = self.ids(ordered[:diag['prefix_candidate_count']])
        self.assertNotIn('ecb-a-1', prefix)
        self.assertNotIn('unregistered-a-1', prefix)
        expected_tail = [item for item in self.matches if item[1] not in set(prefix)]
        self.assertEqual(self.ids(ordered[diag['prefix_candidate_count']:]),
                         self.ids(expected_tail))

    def test_institution_order_comes_from_existing_candidate_order_not_query_order(self):
        first, _ = self.reorder(query='比较美联储与中国人民银行的政策框架')
        second, _ = self.reorder(query='比较中国人民银行与美联储的政策框架')
        self.assertEqual(self.ids(first), self.ids(second))

    def test_many_sources_for_single_institution_are_not_counted_as_multiple_institutions(self):
        rows = [row for row in self.rows if row['publisher'] == 'Federal Reserve']
        ids = {row['id'] for row in rows}
        matches = [item for item in self.matches if item[1] in ids]
        diag = self.assert_disabled(matches=matches, rows=rows)
        self.assertNotIn(PBC, diag['participating_institution_ids'])
        self.assertIn(PBC, diag['missing_institution_ids'])

    def test_single_institution_member_question_keeps_rankings_even_with_other_sources(self):
        self.assert_disabled(query='美国央行每位委员分别如何解释各自的投票？')

    def test_two_explicit_institutions_without_comparison_keep_rankings(self):
        self.assert_disabled(query='中国人民银行和美联储发布了公告')

    def test_multiple_aliases_of_one_institution_keep_rankings(self):
        self.assert_disabled(query='比较美国央行与美联储的政策表述')

    def test_known_country_compound_enables_same_coverage_as_named_institutions(self):
        expected, _ = self.reorder()
        for query in ('比较中美央行的政策框架', '美中央行的政策框架有何区别'):
            with self.subTest(query=query):
                ordered, diag = self.reorder(query=query)
                self.assertTrue(diag['used'])
                self.assertTrue(diag['compound_used'])
                self.assertEqual(self.ids(ordered), self.ids(expected))

    def test_incomplete_unknown_and_duplicate_compounds_keep_original_order(self):
        for term in ('中英央行', '日美央行', '中韩央行', '中欧央行', '中美美央行'):
            with self.subTest(term=term):
                self.assert_disabled(query='比较' + term + '的政策框架')

    def test_source_id_title_and_excerpt_cannot_create_institution_identity(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == '中国人民银行':
                row.update(publisher='合成不明机构', source_id='people-bank-china',
                           title='中国人民银行 Federal Reserve',
                           excerpt='中国人民银行与美联储的政策')
        self.assert_disabled(rows=rows)

    def test_unknown_publisher_stays_in_tail_without_invalidating_known_two_institutions(self):
        ordered, diag = self.reorder()
        self.assertTrue(diag['used'])
        self.assertNotIn('unregistered-a-1', self.ids(ordered[:diag['prefix_candidate_count']]))
        self.assertIn('unregistered-a-1', self.ids(ordered[diag['prefix_candidate_count']:]))

    def test_publisher_requires_exact_registered_alias_not_substring(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == '中国人民银行':
                row['publisher'] = '中国人民银行合成研究小组'
        self.assert_disabled(rows=rows)

    def test_publisher_aliases_of_one_institution_merge_sources(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['source_id'] == 'fed-two':
                row['publisher'] = '美联储'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        self.assertEqual(set(self.ids(ordered[:4])),
                         {'fed-a-1', 'pbc-a-1', 'fed-b-1', 'pbc-b-1'})
        self.assertEqual(set(diag['participating_institution_ids']), {FED, PBC})

    def test_fake_federal_reserve_publisher_cannot_bind_an_unrelated_domain(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = 'https://evil.example.org/claimed-fed-original'
        self.assert_disabled(rows=rows)

    def test_official_name_as_hostname_prefix_is_not_an_official_domain(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = 'https://federalreserve.gov.evil.example.org/article'
        self.assert_disabled(rows=rows)

    def test_hostname_suffix_requires_a_dot_boundary(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = 'https://fakefederalreserve.gov/article'
        self.assert_disabled(rows=rows)

    def test_url_userinfo_disables_origin_mapping_even_with_official_host(self):
        for url in ('https://user@www.federalreserve.gov/article',
                    'https://user:password@www.federalreserve.gov/article',
                    'https://@www.federalreserve.gov/article',
                    'https://:@www.federalreserve.gov/article',
                    'https://federalreserve.gov@evil.example.org/article'):
            with self.subTest(url=url):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = url
                self.assert_disabled(rows=rows)

    def test_non_https_origin_does_not_bind_an_institution(self):
        for scheme in ('http', 'ftp'):
            with self.subTest(scheme=scheme):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = scheme + '://www.federalreserve.gov/article'
                self.assert_disabled(rows=rows)

    def test_nonstandard_or_invalid_port_does_not_bind_an_institution(self):
        for port in ('80', '8443', '65536', 'not-a-port'):
            with self.subTest(port=port):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = 'https://www.federalreserve.gov:' + port + '/article'
                self.assert_disabled(rows=rows)

    def test_explicit_https_port_443_is_allowed(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = 'https://www.federalreserve.gov:443/article'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        self.assertEqual(set(self.ids(ordered[:2])), {'fed-a-1', 'pbc-a-1'})

    def test_root_and_dot_boundary_subdomains_are_allowed_and_merge_sources(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = (
                    'https://federalreserve.gov/article' if row['source_id'] == 'fed-one'
                    else 'https://research.federalreserve.gov/article')
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        self.assertEqual(set(self.ids(ordered[:4])),
                         {'fed-a-1', 'pbc-a-1', 'fed-b-1', 'pbc-b-1'})
        self.assertEqual(set(diag['participating_institution_ids']), {FED, PBC})

    def test_origin_allowlist_matches_publisher_institution_not_any_official_domain(self):
        for url in ('https://www.npc.gov.cn/synthetic-article',
                    'https://www.federalreserve.gov/synthetic-article',
                    'https://www.stats.gov.cn/synthetic-article'):
            with self.subTest(url=url):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == '中国人民银行':
                        row['source_url'] = url
                self.assert_disabled(rows=rows)

    def test_one_unmatched_origin_moves_only_that_candidate_to_unchanged_tail(self):
        rows = deepcopy(self.rows)
        rows[0]['source_url'] = 'https://evil.example.org/forged-source'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        prefix = self.ids(ordered[:diag['prefix_candidate_count']])
        self.assertNotIn('fed-a-1', prefix)
        self.assertIn('fed-a-2', prefix)
        expected_tail = [item for item in self.matches if item[1] not in set(prefix)]
        self.assertEqual(self.ids(ordered[diag['prefix_candidate_count']:]),
                         self.ids(expected_tail))

    def test_missing_or_malformed_source_url_is_unmapped_and_falls_back_when_required(self):
        for value in (None, 17, '', 'www.federalreserve.gov/article',
                      'https:///article', 'https://[invalid-host/article'):
            with self.subTest(value=value):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = value
                self.assert_disabled(rows=rows)
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                del row['source_url']
        self.assert_disabled(rows=rows)

    def test_raw_whitespace_is_not_silently_normalized_into_an_official_origin(self):
        for url in (' https://www.federalreserve.gov/article',
                    'https://www.federalreserve.gov/article ',
                    'https://www.federal\nreserve.gov/article',
                    'https://www.federalreserve.gov/has a space',
                    'https://www.federalreserve.gov/has\tcontrol'):
            with self.subTest(url=url):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = url
                self.assert_disabled(rows=rows)

    def test_invalid_ascii_dns_labels_cannot_bind_by_official_hostname_suffix(self):
        hosts = (
            'evil.com\\.federalreserve.gov',
            '.federalreserve.gov',
            'foo..federalreserve.gov',
            '-foo.federalreserve.gov',
            'foo-.federalreserve.gov',
            'foo_bar.federalreserve.gov',
            'foo%5c.federalreserve.gov',
            'föö.federalreserve.gov',
            ('a' * 64) + '.federalreserve.gov',
        )
        for hostname in hosts:
            with self.subTest(hostname=hostname):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = 'https://' + hostname + '/article'
                self.assert_disabled(rows=rows)

    def test_explicit_empty_port_does_not_count_as_an_absent_default_port(self):
        for hostname in ('federalreserve.gov', 'www.federalreserve.gov'):
            with self.subTest(hostname=hostname):
                rows = deepcopy(self.rows)
                for row in rows:
                    if row['publisher'] == 'Federal Reserve':
                        row['source_url'] = 'https://' + hostname + ':/article'
                self.assert_disabled(rows=rows)

    def test_total_hostname_length_is_bounded_even_when_each_label_is_valid(self):
        hostname = '.'.join(['a' * 63] * 4 + ['federalreserve', 'gov'])
        self.assertGreater(len(hostname), 253)
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = 'https://' + hostname + '/article'
        self.assert_disabled(rows=rows)

    def test_valid_dns_label_with_internal_hyphen_is_allowed(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row['publisher'] == 'Federal Reserve':
                row['source_url'] = 'https://policy-lab1.federalreserve.gov/article'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        self.assertEqual(set(self.ids(ordered[:2])), {'fed-a-1', 'pbc-a-1'})

    def test_ambiguous_known_publisher_combination_fails_closed(self):
        rows = deepcopy(self.rows)
        rows[0]['publisher'] = '中国人民银行 / Federal Reserve'
        self.assert_disabled(rows=rows)

    def test_document_and_version_identity_both_split_groups(self):
        rows = deepcopy(self.rows)
        next(row for row in rows if row['id'] == 'fed-a-2')['version_id'] = 'fed-a-new-version'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        prefix = self.ids(ordered[:diag['prefix_candidate_count']])
        self.assertIn('fed-a-1', prefix)
        self.assertIn('fed-a-2', prefix)
        self.assertLess(prefix.index('fed-a-2'), prefix.index('fed-b-1'))
        self.assertEqual(diag['prefix_candidate_count'], 5)

    def test_reused_version_id_in_another_document_does_not_merge_documents(self):
        rows = deepcopy(self.rows)
        next(row for row in rows if row['id'] == 'fed-b-1')['version_id'] = 'version-fed-a'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        self.assertIn('fed-b-1', self.ids(ordered[:diag['prefix_candidate_count']]))

    def test_product_share_and_channel_identity_individually_split_groups(self):
        for field in ('product_id', 'share_class_id', 'channel_id'):
            with self.subTest(field=field):
                rows = deepcopy(self.rows)
                next(row for row in rows if row['id'] == 'fed-a-2')[field] = 'identity-other'
                ordered, diag = self.reorder(rows=rows)
                self.assertTrue(diag['used'])
                prefix = self.ids(ordered[:diag['prefix_candidate_count']])
                self.assertIn('fed-a-1', prefix)
                self.assertIn('fed-a-2', prefix)
                self.assertEqual(diag['prefix_candidate_count'], 5)

    def test_distinct_sources_for_same_institution_document_and_version_share_one_group(self):
        rows = deepcopy(self.rows)
        next(row for row in rows if row['id'] == 'fed-a-2')['source_id'] = 'additional-official-source'
        ordered, diag = self.reorder(rows=rows)
        self.assertTrue(diag['used'])
        self.assertNotIn('fed-a-2', self.ids(ordered[:diag['prefix_candidate_count']]))
        self.assertEqual(diag['prefix_candidate_count'], 4)

    def test_unknown_candidate_id_disables_entire_reordering(self):
        matches = [*self.matches, match('not-in-eligible', -.20)]
        self.assert_disabled(matches=matches)

    def test_duplicate_match_id_disables_entire_reordering(self):
        matches = [*self.matches, match('fed-a-1', -.10)]
        self.assert_disabled(matches=matches)

    def test_duplicate_selected_row_id_disables_entire_reordering(self):
        rows = [*self.rows, deepcopy(self.rows[0])]
        self.assert_disabled(rows=rows)

    def test_missing_selected_canonical_identity_field_disables_entire_reordering(self):
        for field in ('id', 'publisher', 'document_id', 'version_id', 'visibility',
                      'data_origin', 'status', 'enabled', 'read_allowed', 'outbound_allowed',
                      'parser_id', 'shape', 'product_id', 'share_class_id', 'channel_id'):
            with self.subTest(field=field):
                rows = deepcopy(self.rows)
                del rows[0][field]
                self.assert_disabled(rows=rows)

    def test_rejected_selected_row_disables_entire_reordering(self):
        rejected = (
            {'visibility': 'private'}, {'visibility': 'Public'},
            {'data_origin': 'synthetic'}, {'data_origin': 'demo'},
            {'status': 'pending'}, {'status': 'Verified'},
            {'enabled': 0}, {'read_allowed': 0}, {'outbound_allowed': 0},
            {'parser_id': 'unknown-parser'}, {'shape': 'unknown-shape'},
        )
        for changes in rejected:
            with self.subTest(changes=changes):
                rows = deepcopy(self.rows)
                rows[0].update(changes)
                self.assert_disabled(rows=rows)

    def test_non_market_selected_row_keeps_original_order(self):
        rows = deepcopy(self.rows)
        rows[0]['shape'] = 'webank-productpresentation-v1'
        rows[0]['parser_id'] = 'webank-public-disclosure/1'
        self.assert_disabled(rows=rows)

    def test_invalid_score_disables_entire_reordering(self):
        for score in ('-.9', None, True, float('nan'), float('inf'), -float('inf')):
            with self.subTest(score=score):
                matches = [match('fed-a-1', score), *self.matches[1:]]
                ordered, diag = order_public_institution_coverage(COMPARISON, matches, self.rows)
                self.assertFalse(diag['used'])
                self.assertEqual([id(item) for item in ordered], [id(item) for item in matches])

    def test_invalid_facets_disables_entire_reordering(self):
        for facets in (['concept'], ('concept',), 'concept', None, {'concept': True}, {1}):
            with self.subTest(facets=facets):
                matches = [(-.9, 'fed-a-1', facets), *self.matches[1:]]
                self.assert_disabled(matches=matches)

    def test_invalid_tuple_structure_disables_entire_reordering(self):
        for invalid in ((-.9, 'fed-a-1'), (-.9, 'fed-a-1', {'concept'}, 'extra'),
                        [-.9, 'fed-a-1', {'concept'}]):
            with self.subTest(invalid=invalid):
                matches = [invalid, *self.matches[1:]]
                ordered, diag = order_public_institution_coverage(COMPARISON, matches, self.rows)
                self.assertFalse(diag['used'])
                self.assertEqual([id(item) for item in ordered], [id(item) for item in matches])

    def test_unselected_malformed_private_or_unknown_rows_do_not_affect_valid_candidates(self):
        baseline, baseline_diag = self.reorder()
        junk = [
            {'id': 'unselected-private', 'visibility': 'private'},
            {'id': 'unselected-malformed'},
            {'title': '没有候选身份'},
            public_row('unselected-bad-source', '中国人民银行 / Federal Reserve'),
        ]
        ordered, diag = self.reorder(rows=[*self.rows, *junk])
        self.assertTrue(diag['used'])
        self.assertEqual(self.ids(ordered), self.ids(baseline))
        self.assertEqual(diag, baseline_diag)

    def test_no_candidates_does_not_enable_coverage_or_read_irrelevant_rows(self):
        ordered, diag = self.reorder(matches=[], rows=[{'id': 'junk'}])
        self.assertEqual(ordered, [])
        self.assertFalse(diag['used'])

    def test_eligible_rows_from_sqlite_are_supported(self):
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        sqlite_rows = []
        for row in self.rows:
            columns = ', '.join('?' + ' AS "' + key + '"' for key in row)
            sqlite_rows.append(database.execute('SELECT ' + columns, list(row.values())).fetchone())
        ordered, diag = self.reorder(rows=sqlite_rows)
        self.assertTrue(diag['used'])
        self.assertEqual(set(self.ids(ordered[:2])), {'fed-a-1', 'pbc-a-1'})

    def test_unique_scores_and_facet_set_objects_are_never_recomputed_or_copied(self):
        ordered, _ = self.reorder()
        original = {item[1]: item for item in self.matches}
        for item in ordered:
            self.assertIs(item, original[item[1]])
            self.assertIs(item[2], original[item[1]][2])
            self.assertEqual(item[0], original[item[1]][0])

    def test_ties_use_supplied_order_without_sorting_chunk_ids(self):
        matches = [match(item[1], -.5, item[2]) for item in self.matches]
        ordered, diag = self.reorder(matches=matches)
        self.assertTrue(diag['used'])
        self.assertEqual(ordered[0][1], 'fed-a-1')
        self.assertEqual(ordered[1][1], 'pbc-a-1')

    def test_diagnostics_do_not_echo_query_or_evidence_text(self):
        query = COMPARISON + '，个人备注：合成隐私标记 XYZ-78329'
        _, diag = self.reorder(query=query)
        encoded = json.dumps(diag, ensure_ascii=False)
        self.assertNotIn(query, encoded)
        self.assertNotIn('XYZ-78329', encoded)
        self.assertNotIn('合成隐私标记', encoded)
        self.assertNotIn('Synthetic original public text', encoded)
        self.assertNotIn('合成公共披露标题', encoded)


class PublicInstitutionMetadataTests(unittest.TestCase):
    def test_static_metadata_is_available_without_network(self):
        with patch('socket.socket', side_effect=AssertionError('静态元数据不得访问网络')):
            metadata = institution_policy_metadata()
            self.assertIsInstance(metadata, dict)
            self.assertIsInstance(metadata['policy'], str)
            serialized = json.dumps(metadata, ensure_ascii=False)
        self.assertIn(FED, serialized)
        self.assertIn(PBC, serialized)
        self.assertIn(ECB, serialized)
        self.assertIn(NBS, serialized)
        self.assertIn('central_bank', serialized)
        self.assertIn('https://', serialized)
        self.assertIn('federalreserve.gov', serialized)
        self.assertIn('pbc.gov.cn', serialized)
        self.assertIn('ecb.europa.eu', serialized)
        by_id = {item['institution_id']: item for item in metadata['institutions']}
        self.assertEqual(by_id[FED]['country_code'], 'US')
        self.assertEqual(by_id[PBC]['country_code'], 'CN')
        self.assertEqual(by_id[ECB]['country_code'], 'euro_area')
        self.assertIsNone(by_id[NBS]['role'])
        for item in (by_id[FED], by_id[PBC], by_id[ECB]):
            self.assertEqual(item['role'], 'central_bank')
            self.assertTrue(item['evidence_urls'])
            self.assertTrue(all(url.startswith('https://') for url in item['evidence_urls']))
        self.assertNotIn('欧', metadata['country_short_names'])
        self.assertEqual(set(by_id[FED]['allowed_source_domains']), {'federalreserve.gov'})
        self.assertEqual(set(by_id[PBC]['allowed_source_domains']), {'pbc.gov.cn'})
        self.assertEqual(set(by_id[ECB]['allowed_source_domains']), {'ecb.europa.eu'})
        self.assertEqual(set(by_id[NBS]['allowed_source_domains']), {'stats.gov.cn'})

    def test_metadata_call_returns_equivalent_values_without_global_mutation(self):
        first = institution_policy_metadata()
        baseline = deepcopy(first)
        first['policy'] = 'test-only-mutated-copy'
        first['country_short_names']['中'] = 'test-only-country'
        first['institutions'][0]['publisher_aliases'].append('test-only-alias')
        first['institutions'][0]['evidence_urls'].clear()
        first['institutions'][0]['allowed_source_domains'].append('evil.example.org')
        self.assertEqual(institution_policy_metadata(), baseline)


if __name__ == '__main__':
    unittest.main()
