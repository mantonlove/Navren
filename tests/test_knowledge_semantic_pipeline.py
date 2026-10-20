"""实际 SQLite 原件→消费器→云边界的离线合同回归。"""
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.data_boundary import CloudDataBoundary
from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
from local_knowledge import LocalKnowledgeProvider
from local_knowledge.store import packed


class SemanticPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='navren-semantic-pipeline-')
        self.addCleanup(temporary.cleanup)
        self.provider = LocalKnowledgeProvider(Path(temporary.name) / 'public.sqlite3', encoder=False)
        records = [dict(code=code, name=name, saleStatus='在售', type='基金', placement='公募',
                        saleType='代销', risk='中风险', publish='合成基金管理有限公司',
                        investor='个人', charge='详见产品页面', chargeWay='费率',
                        customerGroupType='个人')
                   for code, name in (('F001', '合成甲产品'), ('F002', '合成乙产品'))]
        body = ('_jsonpproductpresentation(' + packed({'extractDate': '2026-10-01',
                                                      'productPresentation': records}) + ');').encode()
        self.provider.import_webank_disclosure(body, publish=True)

    def pipeline(self, query, provider=None, **options):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')):
            consumed = KnowledgeRetrieval(provider or self.provider).search(query, as_of='2026-10-05',
                                                                           limit=8, **options)
            boundary = CloudDataBoundary(task='comparison')
            safe = boundary.tool_result('knowledge_search', {'data': consumed})['data']
        return consumed, safe

    def test_two_objects_manager_gaps_remain_distinct_after_cloud_projection(self):
        consumed, safe = self.pipeline('合成甲产品和合成乙产品的发行机构、管理人、费用分别是什么')
        for stage in (consumed, safe):
            gaps = [gap for gap in stage['coverage_gaps'] if gap['code'] == 'unsupported_semantic_role']
            self.assertEqual({gap['product_id'] for gap in gaps}, {'WEBANK:F001', 'WEBANK:F002'})
            self.assertTrue(all(gap['role'] == 'manager' for gap in gaps))
            self.assertEqual(len(gaps), 2)
            supported = stage['semantic_coverage']['supported_roles']
            self.assertTrue(supported)
            self.assertEqual({item['role'] for item in supported}, {'issuer'})
            self.assertTrue(all(set(item['citation_ids']) <= {hit['citation_id'] for hit in stage['hits']}
                                for item in supported))

    def test_registered_field_reference_is_not_promoted_to_fee_value(self):
        consumed, safe = self.pipeline('合成甲产品的管理费是多少', product_id='WEBANK:F001')
        for stage in (consumed, safe):
            self.assertIn(('semantic_field_reference_only', 'charge'),
                          {(gap['code'], gap.get('field')) for gap in stage['coverage_gaps']})
            self.assertEqual(stage['semantic_coverage']['requested_roles'], [])
            self.assertEqual(stage['semantic_coverage']['field_support'][0]['status'], 'reference_only')

    def test_consumer_rejection_removes_role_support(self):
        provider = self.provider
        class WithdrawnDuringResolve:
            def status(self): return provider.status()
            def search_public(self, request, release): return provider.search_public(request, release)
            def resolve_public_evidence(self, *args): return None
        consumed, safe = self.pipeline('合成甲产品的发行机构是谁', provider=WithdrawnDuringResolve())
        for stage in (consumed, safe):
            self.assertEqual(stage['hits'], [])
            coverage = stage.get('semantic_coverage')
            self.assertFalse(coverage and coverage.get('supported_roles'))
            self.assertNotIn('WEBANK:F001', json.dumps(stage, ensure_ascii=False))

    def test_output_boundary_rechecks_visible_citation_and_version(self):
        consumed, _ = self.pipeline('合成甲产品的发行机构是谁', product_id='WEBANK:F001')
        for mutation in ('remove', 'wrong_version'):
            data = deepcopy(consumed)
            if mutation == 'remove': data['hits'] = []
            else:
                for hit in data['hits']: hit['document_version_id'] = 'unrelated-version'
            safe = CloudDataBoundary(task='comparison').tool_result('knowledge_search', {'data': data})['data']
            self.assertFalse(safe.get('semantic_coverage', {}).get('supported_roles'))

    def test_context_gap_uses_fixed_message_and_strips_private_context(self):
        consumed, _ = self.pipeline('合成甲产品的发行机构是谁', product_id='WEBANK:F001')
        consumed['coverage_gaps'].append({'code': 'context_incomplete', 'message': '私密账户值',
                                         'role': '私密姓名', 'path': '/private/secret'})
        safe = CloudDataBoundary(task='comparison').tool_result('knowledge_search', {'data': consumed})['data']
        gaps = [gap for gap in safe['coverage_gaps'] if gap['code'] == 'context_incomplete']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(set(gaps[0]), {'code', 'message'})
        text = json.dumps(safe, ensure_ascii=False)
        for value in ('私密账户值', '私密姓名', '/private/secret'):
            self.assertNotIn(value, text)

    def test_other_public_tool_does_not_acquire_semantic_allowlist(self):
        output = {'data': {'semantic_coverage': {'objects': ['private-object'], 'role': 'manager'},
                           'role': 'manager', 'field': 'publish'}}
        safe = CloudDataBoundary(task='comparison').tool_result('market_event', output)
        self.assertNotIn('semantic_coverage', json.dumps(safe))

    def test_many_ordinary_gaps_do_not_truncate_later_role_gap(self):
        provider = self.provider
        class ManyGaps:
            def status(self): return provider.status()
            def search_public(self, request, release):
                result = provider.search_public(request, release)
                ordinary = ['provider_missing', 'provider_not_ready', 'provider_error',
                    'invalid_provider_status', 'invalid_request', 'invalid_search_response',
                    'provider_limit_exceeded', 'invalid_citation', 'duplicate_hit',
                    'evidence_unavailable', 'resolver_error', 'invalid_evidence',
                    'release_mismatch', 'citation_mismatch', 'private_evidence',
                    'outbound_forbidden', 'withdrawn_evidence', 'metadata_only',
                    'subject_mismatch', 'not_yet_effective', 'expired_evidence',
                    'published_after_as_of', 'effective_period_unknown',
                    'publication_date_unknown', 'facet_uncovered', 'source_unavailable',
                    'source_permission_missing', 'identity_unverified', 'conflicting_sources',
                    'sale_status_unknown', 'not_enough_evidence']
                result['coverage_gaps'] = [{'code': code} for code in ordinary] + [
                    {'code': 'unsupported_semantic_role', 'product_id': 'WEBANK:F001', 'role': 'manager'}]
                return result
            def resolve_public_evidence(self, *args): return provider.resolve_public_evidence(*args)
        consumed, safe = self.pipeline('合成甲产品的发行机构、管理人是谁', provider=ManyGaps())
        for stage in (consumed, safe):
            self.assertTrue(any(gap['code'] == 'unsupported_semantic_role'
                                and gap.get('product_id') == 'WEBANK:F001' for gap in stage['coverage_gaps']))
            self.assertGreater(len(stage['coverage_gaps']), 32)

    def test_private_identity_is_not_restored_as_public_hash(self):
        consumed, _ = self.pipeline('合成甲产品的发行机构是谁', product_id='WEBANK:F001')
        private = consumed['public_release_id']
        boundary = CloudDataBoundary(task='comparison', identities=[private])
        safe = boundary.tool_result('knowledge_search', {'data': consumed})['data']
        self.assertNotIn(private, json.dumps(safe))
        self.assertFalse(safe.get('semantic_coverage', {}).get('supported_roles'))

    def test_private_citation_is_removed_without_crashing_gap_projection(self):
        consumed, _ = self.pipeline('合成甲产品的发行机构是谁', product_id='WEBANK:F001')
        private = consumed['semantic_coverage']['supported_roles'][0]['citation_ids'][0]
        boundary = CloudDataBoundary(task='comparison')
        boundary.register_private_identifiers([private])
        safe = boundary.tool_result('knowledge_search', {'data': consumed})['data']
        self.assertNotIn(private, json.dumps(safe))
        self.assertFalse(safe.get('semantic_coverage', {}).get('supported_roles'))

    def test_unknown_shape_and_ordinary_messages_cannot_register_no_hit_object(self):
        private = 'UNVERIFIED_NO_HIT_OBJECT'
        data = {'hits': [], 'product_id': private,
                'coverage_gaps': [{'code': ['invalid'], 'facet': ['invalid'], 'message': '私密资料'},
                                  {'code': 'source_unavailable', 'product_id': private, 'message': '私密资料'}]}
        boundary = CloudDataBoundary(task='comparison')
        safe = boundary.tool_result('knowledge_search', {'data': data})['data']
        self.assertNotIn(private, json.dumps(safe))
        self.assertNotIn('私密资料', json.dumps(safe, ensure_ascii=False))
        self.assertNotIn(private, boundary._public_ids)

    def test_invalid_hit_list_is_withheld(self):
        for hits in (True, 1, None, 'private-path', [{}] * 9):
            safe = CloudDataBoundary(task='comparison').tool_result('knowledge_search',
                {'data': {'hits': hits, 'coverage_gaps': []}})['data']
            self.assertEqual(safe['boundary']['status'], 'withheld')


if __name__ == '__main__':
    unittest.main()
