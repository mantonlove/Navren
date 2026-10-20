"""字段语义登记的证据误绑定与覆盖边界，全部使用合成公开原件。"""
from copy import deepcopy
import json
import unittest

from local_knowledge.field_semantics import (
    FIELD_REGISTRY, SEMANTIC_POLICY, assess_semantic_coverage, field_evidence,
    query_semantics, registry_metadata,
)
from local_knowledge.store import packed, sha
from webank_catalog import URL


def disclosed(field='publish', value='合成基金管理有限公司', *, code='F-001',
              citation=None, version='version-A', **changes):
    excerpt = packed(field) + ':' + packed(value)
    row = {'id': citation or 'citation-' + code + '-' + field,
           'version_id': version, 'title': '合成产品-' + code,
           'excerpt': excerpt, 'excerpt_sha256': sha(excerpt),
           'parser_id': 'webank-record-json/1',
           'shape': 'webank-productpresentation-v1',
           'source_id': 'webank-direct-disclosure', 'source_url': URL,
           'channel_id': 'WEBANK', 'status': 'verified',
           'product_id': 'WEBANK:' + code, 'share_class_id': None,
           'paragraph': 'productPresentation:' + code + '/' + field,
           'start_char': 10, 'end_char': 10 + len(excerpt), 'page': None}
    row.update(changes)
    return row


class FieldRegistryTests(unittest.TestCase):
    def test_issuer_and_manager_have_distinct_meanings(self):
        issuer = FIELD_REGISTRY['publish']
        self.assertEqual(issuer.semantic_role, 'issuer')
        self.assertIn('manager_identity', issuer.prohibited_inference)
        self.assertIsNone(FIELD_REGISTRY['saleType'].semantic_role)
        self.assertIn('sales_institution_identity',
                      FIELD_REGISTRY['saleType'].prohibited_inference)

    def test_registry_is_immutable_and_json_safe(self):
        with self.assertRaises(TypeError):
            FIELD_REGISTRY['manager'] = FIELD_REGISTRY['publish']
        with self.assertRaises(AttributeError):
            FIELD_REGISTRY['publish'].semantic_role = 'manager'
        metadata = json.loads(json.dumps(registry_metadata(), ensure_ascii=False))
        self.assertEqual(metadata['policy'], SEMANTIC_POLICY)
        self.assertEqual(metadata['role_fields'], {'issuer': 'publish'})
        self.assertTrue(all(item['product_type_basis'] == 'same_version_original_type_field_only'
                            for item in metadata['fields'].values()))


class QuerySemanticTests(unittest.TestCase):
    def test_manager_request_does_not_become_publish_field(self):
        result = query_semantics('请说明登记的管理人全称')
        self.assertEqual(result['fields'], [])
        self.assertEqual(result['roles'], ['manager'])
        self.assertEqual(result['unsupported_roles'], ['manager'])

    def test_fee_wording_does_not_request_manager(self):
        result = query_semantics('管理费的费率是多少？')
        self.assertEqual(result['fields'], ['charge'])
        self.assertEqual(result['roles'], [])

    def test_source_compatible_category_and_placement_terms(self):
        result = query_semantics('说明产品类别以及募集属性')
        self.assertEqual(set(result['fields']), {'type', 'placement'})
        self.assertEqual(result['roles'], [])

    def test_issuer_manager_and_sales_institution_stay_separate(self):
        result = query_semantics('发行机构、管理人、销售机构分别是谁？')
        self.assertEqual(result['fields'], ['publish'])
        self.assertEqual(result['roles'], ['issuer', 'manager', 'sales_institution'])
        self.assertEqual(result['unsupported_roles'], ['manager', 'sales_institution'])

    def test_unsupported_custodian_is_reported(self):
        result = query_semantics('托管银行是哪家？')
        self.assertEqual(result['fields'], [])
        self.assertEqual(result['unsupported_roles'], ['custodian'])

    def test_old_exact_field_phrases_are_preserved(self):
        examples = {'risk': '风险几级', 'publish': '保险公司是哪家',
                    'charge': '收多少', 'chargeWay': '怎么交费用',
                    'investor': '核保', 'customerGroupType': '公司名义',
                    'saleType': '替别人销售', 'placement': '公众募集',
                    'saleStatus': '还能下单', 'type': '什么类型',
                    'code': '产品代码', 'name': '产品名称'}
        for field, query in examples.items():
            with self.subTest(field=field):
                self.assertIn(field, query_semantics(query)['fields'])

    def test_unknown_wording_has_no_fabricated_semantics(self):
        self.assertEqual(query_semantics('未来收益是否超过四个百分点'),
                         {'fields': [], 'roles': [], 'unsupported_roles': []})

    def test_english_role_requires_word_boundary(self):
        self.assertEqual(query_semantics('subissuer')['roles'], [])
        self.assertEqual(query_semantics('ISSUER and fund manager')['roles'],
                         ['issuer', 'manager'])

    def test_invalid_query_is_explicit(self):
        with self.assertRaisesRegex(ValueError, 'invalid_semantic_query'):
            query_semantics(None)


class FieldEvidenceTests(unittest.TestCase):
    def test_company_name_is_preserved_as_issuer_only(self):
        row = disclosed(value='合成基金管理有限公司')
        before = deepcopy(row)
        evidence = field_evidence(row)
        self.assertEqual(evidence['value'], '合成基金管理有限公司')
        self.assertEqual(evidence['semantic_role'], 'issuer')
        self.assertEqual(evidence['original_field'], 'publish')
        self.assertEqual(evidence['citation_id'], row['id'])
        self.assertEqual(evidence['document_version_id'], row['version_id'])
        self.assertEqual(evidence['locator']['start_char'], row['start_char'])
        self.assertEqual(row, before)

    def test_unknown_original_manager_field_is_not_registered_by_name(self):
        self.assertIsNone(field_evidence(disclosed('manager', '合成管理人')))

    def test_sales_type_does_not_prove_sales_institution(self):
        evidence = field_evidence(disclosed('saleType', '代销'))
        self.assertIsNone(evidence['semantic_role'])
        self.assertEqual(evidence['original_field'], 'saleType')

    def test_type_is_only_the_original_catalogue_value(self):
        evidence = field_evidence(disclosed('type', '合成原件类型'))
        self.assertEqual(evidence['value'], '合成原件类型')
        self.assertEqual(evidence['product_type_basis'],
                         'same_version_original_type_field_only')

    def test_wrong_source_parser_shape_and_channel_cannot_support_roles(self):
        mutations = [{'source_id': 'other-source'}, {'parser_id': 'exact-text/1'},
                     {'shape': 'other-shape'}, {'channel_id': 'OTHER'},
                     {'product_id': 'OTHER:F-001'}, {'share_class_id': 'share-A'},
                     {'source_url': 'https://example.invalid/other-catalogue'},
                     {'page': 3}]
        for change in mutations:
            with self.subTest(change=change):
                self.assertIsNone(field_evidence(disclosed(**change)))

    def test_candidate_or_withdrawn_status_cannot_describe_supported_role(self):
        for status in ('candidate', 'rejected', 'withdrawn'):
            with self.subTest(status=status):
                self.assertIsNone(field_evidence(disclosed(status=status)))

    def test_wrong_original_key_locator_is_rejected(self):
        row = disclosed(paragraph='productPresentation:F-001/manager')
        self.assertIsNone(field_evidence(row))

    def test_span_and_hash_tampering_is_rejected(self):
        mutations = [{'start_char': -1}, {'start_char': True}, {'end_char': 200},
                     {'end_char': None}, {'excerpt_sha256': '0' * 64}]
        for change in mutations:
            with self.subTest(change=change):
                self.assertIsNone(field_evidence(disclosed(**change)))

    def test_missing_citation_or_version_is_rejected(self):
        for key in ('id', 'version_id'):
            row = disclosed()
            del row[key]
            self.assertIsNone(field_evidence(row))

    def test_empty_partial_joined_or_noncanonical_fields_are_rejected(self):
        rows = [disclosed(value=''), disclosed(value='   '),
                disclosed(excerpt='"publish":"合成公司'),
                disclosed(excerpt='"publish":"合成公司","type":"合成类型"'),
                disclosed(excerpt='"publish": "合成公司"'),
                disclosed(value=123)]
        for row in rows:
            with self.subTest(excerpt=row['excerpt']):
                self.assertIsNone(field_evidence(row))

    def test_reference_is_kept_as_reference_not_value(self):
        evidence = field_evidence(disclosed('charge', '详见产品页面'))
        self.assertEqual(evidence['detail_status'], 'reference_only')

    def test_no_product_family_is_inferred_from_title(self):
        row = disclosed(title='合成保险基金银行万能产品')
        evidence = field_evidence(row)
        self.assertNotIn('product_type', evidence)
        self.assertEqual(evidence['applicable_product_scope'],
                         'webank_dated_channel_catalog_product')


class SemanticCoverageTests(unittest.TestCase):
    def test_manager_gap_survives_publish_company_match(self):
        row = disclosed()
        result = assess_semantic_coverage('管理人是谁？', [row], {row['product_id']}, [row['id']])
        self.assertEqual(result['supported_roles'], [])
        self.assertEqual(result['field_support'], [])
        self.assertEqual(result['coverage_gaps'], [
            {'code': 'unsupported_semantic_role', 'product_id': row['product_id'],
             'role': 'manager', 'reason': 'no_registered_role_source'}])

    def test_one_product_cannot_fill_another_product_role_gap(self):
        issuer_a = disclosed(code='F-001')
        other_b = disclosed('type', '合成类型', code='F-002')
        result = assess_semantic_coverage('发行机构是谁', [issuer_a, other_b],
                                          {issuer_a['product_id'], other_b['product_id']})
        self.assertEqual([item['product_id'] for item in result['supported_roles']],
                         ['WEBANK:F-001'])
        self.assertIn({'code': 'semantic_role_uncovered', 'product_id': 'WEBANK:F-002',
                       'role': 'issuer', 'field': 'publish'}, result['coverage_gaps'])

    def test_unsupported_role_gap_is_retained_for_each_product(self):
        rows = [disclosed(code='F-001'), disclosed(code='F-002')]
        result = assess_semantic_coverage('管理人是谁', rows,
                                          {row['product_id'] for row in rows})
        self.assertEqual(len(result['coverage_gaps']), 2)
        self.assertEqual({gap['product_id'] for gap in result['coverage_gaps']},
                         {'WEBANK:F-001', 'WEBANK:F-002'})

    def test_unselected_candidate_cannot_claim_role_coverage(self):
        row = disclosed()
        result = assess_semantic_coverage('发行机构是谁', [row], {row['product_id']}, [])
        self.assertEqual(result['supported_roles'], [])
        self.assertTrue(any(gap['code'] == 'semantic_role_uncovered'
                            for gap in result['coverage_gaps']))

    def test_other_product_selected_id_cannot_claim_coverage(self):
        rows = [disclosed(code='F-001'), disclosed(code='F-002')]
        result = assess_semantic_coverage('发行机构是谁', rows, {'WEBANK:F-001'},
                                          [rows[1]['id']])
        self.assertEqual(result['supported_roles'], [])

    def test_supported_category_fields_only_report_original_citation(self):
        rows = [disclosed('type', '合成类型'), disclosed('placement', '合成募集属性')]
        result = assess_semantic_coverage('募集方式及产品分类', rows, {'WEBANK:F-001'})
        self.assertEqual(result['coverage_gaps'], [])
        self.assertEqual({item['field'] for item in result['field_support']}, {'type', 'placement'})
        self.assertEqual({cid for item in result['field_support'] for cid in item['citation_ids']},
                         {row['id'] for row in rows})
        self.assertNotIn('value', json.dumps(result))

    def test_reference_has_gap_and_cannot_claim_issuer(self):
        row = disclosed('publish', '详见产品页面')
        result = assess_semantic_coverage('发行机构是谁', [row], {row['product_id']})
        self.assertEqual(result['supported_roles'], [])
        self.assertEqual(result['field_support'][0]['status'], 'reference_only')
        self.assertEqual(result['field_support'][0]['citation_ids'], [])
        self.assertEqual(result['field_support'][0]['reference_citation_ids'], [row['id']])

    def test_exact_product_name_is_not_a_role_question(self):
        row = disclosed('type', '合成类型', title='合成基金管理公司一号')
        result = assess_semantic_coverage('合成基金管理公司一号的产品类型',
                                          [row], {row['product_id']})
        self.assertEqual(result['requested_roles'], [])
        self.assertEqual(result['coverage_gaps'], [])

    def test_results_do_not_keep_query_or_mutate_input(self):
        row = disclosed()
        rows, objects, selected = [row], {row['product_id']}, [row['id']]
        before = deepcopy((rows, objects, selected))
        query = '查询标记-QA123-发行机构'
        result = assess_semantic_coverage(query, rows, objects, selected)
        self.assertNotIn('QA123', json.dumps(result, ensure_ascii=False))
        self.assertEqual((rows, objects, selected), before)
        json.dumps(result, allow_nan=False)

    def test_unbound_objects_have_no_asserted_coverage(self):
        result = assess_semantic_coverage('管理人是谁', [disclosed()], set())
        self.assertEqual(result['objects'], [])
        self.assertEqual(result['supported_roles'], [])
        self.assertEqual(result['coverage_gaps'], [])

    def test_invalid_coverage_query_or_object_is_explicit(self):
        with self.assertRaisesRegex(ValueError, 'invalid_semantic_query'):
            assess_semantic_coverage(None, [disclosed()], {'WEBANK:F-001'})
        with self.assertRaisesRegex(ValueError, 'invalid_semantic_objects'):
            assess_semantic_coverage('发行机构', [disclosed()], {'WEBANK:F-001', 12})


if __name__ == '__main__':
    unittest.main()
