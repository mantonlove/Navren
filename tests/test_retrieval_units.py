"""Synthetic offset/provenance tests; no encoder, network or acceptance corpus."""
import dataclasses
import hashlib
import json
import sqlite3
import unittest

from local_knowledge.retrieval_units import (POLICY_ID, UnitBuildError, build_units,
                                           eligible_citations)


def canonical(text, ranges=None, **changes):
    ranges = ranges or [(start, min(start + 1000, len(text)))
                        for start in range(0, len(text), 1000)]
    rows = []
    for i, (start, end) in enumerate(ranges):
        excerpt = text[start:end]
        row = {'id': 'c' + str(i), 'version_id': 'v-public',
               'start_char': start, 'end_char': end, 'excerpt': excerpt,
               'excerpt_sha256': hashlib.sha256(excerpt.encode()).hexdigest(),
               'paragraph': f'chars:{start}-{end}', 'page': None,
               'product_id': None, 'share_class_id': None, 'channel_id': None,
               'facets': '["general"]', 'conditions': '[]', 'data_on': None,
               'parser_id': 'exact-text/1', 'shape': 'market-official-full-text-v1',
               'source_id': 'official-public'}
        row.update(changes)
        rows.append(row)
    return rows


class RetrievalUnitTests(unittest.TestCase):
    def assert_provenance(self, text, rows, result):
        by_id = {row['id']: row for row in rows}
        parents = {parent.id: parent for parent in result.parents}
        for unit in result.parents + result.children:
            self.assertEqual(unit.text, text[unit.start_char:unit.end_char])
            self.assertTrue(unit.text.strip())
            self.assertTrue(unit.citation_ids)
            for cid in unit.citation_ids:
                row = by_id[cid]
                self.assertLess(row['start_char'], unit.end_char)
                self.assertGreater(row['end_char'], unit.start_char)
            covered = set()
            for cid in unit.citation_ids:
                row = by_id[cid]
                covered.update(range(max(unit.start_char, row['start_char']),
                                     min(unit.end_char, row['end_char'])))
            self.assertEqual(covered, set(range(unit.start_char, unit.end_char)))
            if unit.kind == 'child':
                parent = parents[unit.parent_id]
                self.assertLessEqual(parent.start_char, unit.start_char)
                self.assertGreaterEqual(parent.end_char, unit.end_char)
                self.assertEqual(parent.boundary_key, unit.boundary_key)
                self.assertTrue(set(unit.citation_ids).issubset(parent.citation_ids))

    def test_chinese_sentences_and_paragraphs(self):
        text = '第一句讲风险。第二句谈费用！第三句回答问题？\n\n另一段说明限制。'
        rows = canonical(text)
        result = build_units(text, rows)
        self.assertEqual([unit.text for unit in result.children],
                         ['第一句讲风险。', '第二句谈费用！', '第三句回答问题？', '另一段说明限制。'])
        self.assertEqual([unit.text for unit in result.parents],
                         ['第一句讲风险。第二句谈费用！第三句回答问题？', '另一段说明限制。'])
        self.assertEqual(result.policy_id, POLICY_ID)
        self.assert_provenance(text, rows, result)

    def test_python_offsets_keep_emoji_and_combining_marks(self):
        text = '  👨‍👩‍👧 金额 2.5%。  e\u0301 is unchanged.\n\n最后一段。  '
        rows = canonical(text)
        result = build_units(text, rows)
        self.assertEqual(result.children[0].start_char, 2)
        self.assertEqual(result.children[0].text, '👨‍👩‍👧 金额 2.5%。')
        self.assertEqual(result.children[1].text, 'e\u0301 is unchanged.')
        self.assert_provenance(text, rows, result)

    def test_decimals_abbreviations_acronyms_and_initials(self):
        text = 'Dr. A. Smith cites the U.S. rate of 2.5%. It is unchanged!'
        result = build_units(text, canonical(text))
        self.assertEqual([unit.text for unit in result.children],
                         ['Dr. A. Smith cites the U.S. rate of 2.5%.', 'It is unchanged!'])

    def test_dotted_examples_and_domains_not_broken(self):
        text = 'Use e.g. example.org and a.b@example.org here. A second sentence.'
        result = build_units(text, canonical(text))
        self.assertEqual([unit.text for unit in result.children],
                         ['Use e.g. example.org and a.b@example.org here.', 'A second sentence.'])

    def test_closing_quotes_attached_to_original_sentence(self):
        text = 'He said “Keep cash.” Next sentence!\n她说：“仍然保留现金。”随后说明。'
        result = build_units(text, canonical(text))
        self.assertEqual([unit.text for unit in result.children],
                         ['He said “Keep cash.”', 'Next sentence!', '她说：“仍然保留现金。”', '随后说明。'])

    def test_list_lines_remain_independent_children(self):
        text = '三个方面如下：\n- 稳定价格\n- 维护金融稳定\n- 完善支付体系'
        rows = canonical(text)
        result = build_units(text, rows)
        self.assertEqual(len(result.parents), 1)
        self.assertEqual([unit.text for unit in result.children],
                         ['三个方面如下：', '- 稳定价格', '- 维护金融稳定', '- 完善支付体系'])
        self.assert_provenance(text, rows, result)

    def test_cross_canonical_chunk_sentence_maps_every_overlap(self):
        text = '前。' * 486 + '这句话在旧的一千字边界左右连续完整存在，而且不可丢失上下文。' * 3
        rows = canonical(text)
        result = build_units(text, rows)
        crossing = [unit for unit in result.children
                    if unit.start_char < 1000 < unit.end_char]
        self.assertEqual(len(crossing), 1)
        self.assertEqual(crossing[0].citation_ids, ('c0', 'c1'))
        self.assert_provenance(text, rows, result)

    def test_parent_context_crosses_both_previous_and_next_chunk(self):
        text = '起。' * 475 + 'first answer. ' + '完整上下文很重要，' * 90 + 'This is the end.'
        rows = canonical(text)
        result = build_units(text, rows, max_parent_chars=1600)
        parent = next(parent for parent in result.parents if parent.start_char <= 1000 < parent.end_char)
        self.assertTrue({'c0', 'c1'}.issubset(parent.citation_ids))
        self.assert_provenance(text, rows, result)

    def test_long_sentence_fragments_explicit_budgets(self):
        text = ('连续文字' * 140) + '。'
        rows = canonical(text)
        result = build_units(text, rows, max_child_chars=80, max_parent_chars=240)
        self.assertTrue(all(len(unit.text) <= 80 for unit in result.children))
        self.assertTrue(all(len(unit.text) <= 240 for unit in result.parents))
        self.assertTrue(all(unit.continuation for unit in result.children))
        self.assertTrue(all(unit.continuation for unit in result.parents))
        self.assertIn('child_budget_split', {issue.code for issue in result.issues})
        self.assertIn('parent_budget_split', {issue.code for issue in result.issues})
        self.assertEqual(''.join(unit.text for unit in result.children), text)
        self.assert_provenance(text, rows, result)

    def test_whitespace_long_sentence_split_exact_provenance(self):
        text = ('one two three four five six ' * 20) + 'end.'
        rows = canonical(text)
        result = build_units(text, rows, max_child_chars=64, max_parent_chars=192)
        self.assertTrue(all(len(unit.text) <= 64 for unit in result.children))
        self.assertEqual(''.join(unit.text for unit in result.children).replace(' ', ''),
                         text.replace(' ', ''))
        self.assert_provenance(text, rows, result)

    def test_each_annotation_boundary_is_separate(self):
        text = '第一个对象谈风险第二个对象谈风险'
        end = len(text) // 2
        changes = {'product_id': 'p-other', 'share_class_id': 'share-other',
                   'channel_id': 'channel-other', 'facets': '["risk"]',
                   'conditions': '["仅限持有者"]', 'page': 2,
                   'data_on': '2026-10-01', 'source_id': 'other-official',
                   'parser_id': 'pdfplumber-text/1', 'shape': 'pdfplumber-pages/1'}
        for field, value in changes.items():
            with self.subTest(field=field):
                rows = canonical(text, [(0, end), (end, len(text))])
                rows[1][field] = value
                result = build_units(text, rows)
                self.assertEqual(len(result.parents), 2)
                self.assertEqual(len(result.children), 2)
                self.assertNotEqual(result.parents[0].boundary_key, result.parents[1].boundary_key)
                self.assert_provenance(text, rows, result)

    def test_explicit_locator_does_not_merge(self):
        text = '第一字段是发行机构第二字段是管理人'
        rows = canonical(text, [(0, 9), (9, len(text))])
        rows[0]['paragraph'] = 'reviewed:issuer'
        rows[1]['paragraph'] = 'reviewed:manager'
        result = build_units(text, rows)
        self.assertEqual(len(result.parents), 2)
        self.assertEqual([unit.citation_ids for unit in result.parents], [('c0',), ('c1',)])

    def test_auto_locator_must_match_actual_offsets(self):
        text = '第一个第二个'
        rows = canonical(text, [(0, 3), (3, len(text))])
        rows[1]['paragraph'] = 'chars:0-3'
        result = build_units(text, rows)
        self.assertEqual(len(result.parents), 2)

    def test_same_annotation_order_is_stable(self):
        text = '风险以及费用内容'
        rows = canonical(text, [(0, 4), (4, len(text))])
        rows[0].update(facets=['general', 'risk'], conditions=['A', 'B'])
        rows[1].update(facets='["risk","general"]', conditions='["B","A"]')
        result = build_units(text, rows)
        self.assertEqual(len(result.parents), 1)
        self.assertEqual(result.parents[0].citation_ids, ('c0', 'c1'))

    def test_gap_is_never_filled_from_unmapped_parsed_text(self):
        text = '公开左段秘密未映射部分公开右段'
        rows = canonical(text, [(0, 4), (11, len(text))])
        result = build_units(text, rows)
        self.assertEqual(len(result.parents), 2)
        self.assertTrue(all('秘密' not in unit.text for unit in result.parents + result.children))
        self.assertIn('canonical_coverage_gap', {issue.code for issue in result.issues})
        self.assert_provenance(text, rows, result)

    def test_mixed_annotation_overlap_discarded_fail_closed(self):
        text = 'abcdefghij'
        rows = canonical(text, [(0, 7), (3, 10)])
        rows[1]['product_id'] = 'different-object'
        result = build_units(text, rows)
        self.assertEqual(result.parents, ())
        self.assertEqual(result.children, ())
        self.assertIn('ambiguous_canonical_overlap', {issue.code for issue in result.issues})
        self.assertIn('ambiguous_canonical_chunk', {issue.code for issue in result.issues})
        self.assert_provenance(text, rows, result)

    def test_unambiguous_chunks_survive_ambiguous_neighbor_exclusion(self):
        text = 'abcdefghij safe final sentence.'
        rows = canonical(text, [(0, 7), (3, 10), (11, len(text))])
        rows[1]['product_id'] = 'different-object'
        result = build_units(text, rows)
        self.assertEqual([parent.text for parent in result.parents], ['safe final sentence.'])
        self.assertEqual(result.parents[0].citation_ids, ('c2',))
        self.assert_provenance(text, rows, result)

    def test_matching_annotation_overlap_maps_all_original_citations(self):
        text = 'abcdefghij'
        rows = canonical(text, [(0, 7), (3, 10)])
        result = build_units(text, rows)
        self.assertEqual(result.parents[0].text, text)
        self.assertEqual(result.parents[0].citation_ids, ('c0', 'c1'))
        self.assert_provenance(text, rows, result)

    def test_order_independent_deterministic_ids_and_payload(self):
        text = '甲乙丙。' * 350
        rows = canonical(text)
        forward = build_units(text, rows)
        backward = build_units(text, list(reversed(rows)))
        self.assertEqual(forward, backward)
        json.dumps(dataclasses.asdict(forward), ensure_ascii=False)

    def test_same_text_different_version_has_different_unit_ids(self):
        text = '同一段文字。'
        first = build_units(text, canonical(text))
        second = build_units(text, canonical(text, version_id='v-next'))
        self.assertNotEqual(first.parents[0].id, second.parents[0].id)
        self.assertNotEqual(first.children[0].id, second.children[0].id)

    def test_citation_mapping_change_invalidates_unit_id(self):
        text = '同一段文字。'
        rows = canonical(text)
        first = build_units(text, rows)
        rows[0]['id'] = 'new-canonical-id'
        second = build_units(text, rows)
        self.assertNotEqual(first.parents[0].id, second.parents[0].id)

    def test_eligible_mapping_is_all_or_nothing(self):
        text = '同一段连续文字'
        rows = canonical(text, [(0, 4), (4, len(text))])
        unit = build_units(text, rows).parents[0]
        self.assertEqual(eligible_citations(unit, {'c0', 'c1'}), ('c0', 'c1'))
        self.assertEqual(eligible_citations(unit, {'c0'}), ())
        self.assertEqual(eligible_citations(unit, set()), ())
        self.assertEqual(eligible_citations(unit, 'c0c1'), ())
        self.assertEqual(eligible_citations({'citation_ids': ['c0']}, {'c0'}), ())

    def test_sqlite_row_supported(self):
        text = 'SQLite 原件。'
        row = canonical(text)[0]
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        # Explicit integer columns represent the actual chunks schema.
        declaration = ','.join(key + (' INTEGER' if key in ('start_char', 'end_char', 'page') else ' TEXT') for key in row)
        db.execute('CREATE TABLE canonical (' + declaration + ')')
        db.execute('INSERT INTO canonical VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
        result = build_units(text, db.execute('SELECT * FROM canonical').fetchall())
        self.assertEqual(result.children[0].text, text)

    def test_invalid_rows_cannot_publish_partial_derivative(self):
        text = '可靠原件。'
        for field, value, code in (
                ('excerpt', '伪造', 'canonical_quote_mismatch'),
                ('excerpt_sha256', '0' * 64, 'canonical_quote_hash_mismatch'),
                ('start_char', True, 'invalid_canonical_range'),
                ('page', False, 'invalid_canonical_locator'),
                ('facets', '["general",2]', 'invalid_canonical_annotations')):
            with self.subTest(field=field):
                rows = canonical(text)
                rows[0][field] = value
                with self.assertRaises(UnitBuildError) as caught:
                    build_units(text, rows)
                self.assertEqual(caught.exception.code, code)

    def test_missing_annotations_rejected(self):
        text = '可靠原件。'
        rows = canonical(text)
        del rows[0]['conditions']
        with self.assertRaises(UnitBuildError) as caught:
            build_units(text, rows)
        self.assertEqual(caught.exception.code, 'canonical_metadata_missing')

    def test_mixed_or_wrong_version_rejected(self):
        text = '版本不能合并'
        rows = canonical(text, [(0, 3), (3, len(text))])
        rows[1]['version_id'] = 'v-other'
        with self.assertRaises(UnitBuildError) as caught:
            build_units(text, rows)
        self.assertEqual(caught.exception.code, 'mixed_document_versions')
        with self.assertRaises(UnitBuildError) as caught:
            build_units(text, canonical(text), version_id='v-other')
        self.assertEqual(caught.exception.code, 'document_version_mismatch')

    def test_malformed_version_type_is_typed_failure(self):
        text = '版本字段要合法'
        for value in (None, '', ['v-public'], {'id': 'v-public'}):
            rows = canonical(text)
            rows[0]['version_id'] = value
            with self.subTest(value=value), self.assertRaises(UnitBuildError) as caught:
                build_units(text, rows)
            self.assertEqual(caught.exception.code, 'mixed_document_versions')

    def test_chunk_limit_rejects_generator_without_consuming_rest(self):
        text = '一个原件。'
        template = canonical(text)[0]
        consumed = []
        def stream():
            for i in range(6000):
                consumed.append(i)
                yield {**template, 'id': 'c' + str(i)}
        with self.assertRaises(UnitBuildError) as caught:
            build_units(text, stream())
        self.assertEqual(caught.exception.code, 'canonical_chunk_limit')
        self.assertEqual(len(consumed), 5001)

    def test_duplicate_citation_rejected(self):
        text = '重复引用不可用'
        rows = canonical(text, [(0, 3), (3, len(text))])
        rows[1]['id'] = rows[0]['id']
        with self.assertRaises(UnitBuildError):
            build_units(text, rows)

    def test_total_unit_limit_rejects_instead_of_silent_truncation(self):
        text = '一个句子。另一个句子。'
        with self.assertRaises(UnitBuildError) as caught:
            build_units(text, canonical(text), max_units=2)
        self.assertEqual(caught.exception.code, 'unit_limit_exceeded')

    def test_budget_configuration_bounded(self):
        text = '可用文本。'
        for kwargs in ({'max_child_chars': 0}, {'max_child_chars': True},
                       {'max_parent_chars': 20}, {'max_parent_chars': 100000},
                       {'max_units': 0}, {'max_units': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(UnitBuildError):
                build_units(text, canonical(text), **kwargs)


if __name__ == '__main__':
    unittest.main()
