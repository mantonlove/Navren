"""服务端审核证据整理：纯 JSON、无银行/模型/网络调用。"""
from copy import deepcopy
import hashlib
import json
import unittest

from agent_runtime.review_evidence import (
    ReviewEvidenceError, pack_review_evidence, unpack_review_evidence)


def metric_fixture(metric_id='obj_metric_1'):
    return {'contract_version': '1.0.0', 'metric_id': metric_id,
        'subject_id': 'obj_subject_1', 'metric_name': 'hypothetical_sum_result',
        'currency': 'CNY', 'unit': 'minor_currency', 'value_minor': 13000,
        'as_of': '2026-10-03', 'period_start': None, 'period_end': None,
        'data_scope': 'user_input_scenario', 'calculation_status': 'calculated',
        'basis': 'user_input_arithmetic_not_account_data', 'formula_id': 'obj_formula_1',
        'input_refs': ['obj_input_1'],
        'source_versions': {'obj_source_' + str(i): 'obj_version_' + str(i) for i in range(9)},
        'excluded_items': ['未记录的生活费不视为零'], 'coverage_gaps': [],
        'assumed_inputs': [{'type': 'user_hypothesis', 'input_index': 0,
                           'value_minor': 13000, 'confirmed': False}]}


def packet_fixture(count=24, *, unique=False):
    rows, claims = [], []
    for i in range(count):
        metric = metric_fixture('obj_metric_' + str(i) if unique else 'obj_metric_1')
        claim = {key: deepcopy(metric[key]) for key in ('metric_id', 'subject_id',
            'metric_name', 'currency', 'unit', 'value_minor', 'as_of', 'period_start',
            'period_end', 'data_scope', 'calculation_status', 'basis', 'formula_id',
            'source_versions')}
        claim.update(text='仅作假设算式:130元。', claim_kind='hypothesis', occurrence_start=i * 13)
        rows.append({'metric_id': metric['metric_id'], 'metric': metric,
                     'kind': 'hypothesis', 'evidence_ids': ['obj_evidence_1'], 'claim': claim})
        claims.append(deepcopy(claim))
    return {'question': '合成验收', 'history': [], 'draft': {
        'text': '仅作假设算式:130元。' * count, 'amount_claims': claims},
        'amount_contract': {'status': 'validated', 'amount_claims': rows,
            'amount_occurrence_count': count, 'independent_review_required': bool(count)},
        'monetary_evidence': [{'value': 13000, 'currency': 'CNY', 'unit': 'minor_currency',
                               'evidence_id': 'obj_evidence_1'}]}


def ref_for(metric):
    wire = json.dumps(metric, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode()
    return 'rmetric_' + hashlib.sha256(wire).hexdigest()


class ReviewEvidenceTest(unittest.TestCase):
    def assert_error(self, call, code=None):
        with self.assertRaises(ReviewEvidenceError) as caught:
            call()
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_full24_roundtrip_retains_every_draft_claim_and_complete_original(self):
        packet = packet_fixture()
        before = deepcopy(packet)
        packed = pack_review_evidence(packet)
        self.assertIn('review_metric_catalog', packed)
        self.assertEqual(len(packed['review_metric_catalog']), 1)
        self.assertEqual(len(packed['amount_contract']['amount_claims']), 24)
        self.assertEqual(packed['draft'], packet['draft'])
        self.assertEqual(packed['monetary_evidence'], packet['monetary_evidence'])
        self.assertEqual(unpack_review_evidence(packed), packet)
        self.assertEqual(packet, before)
        ref = next(iter(packed['review_metric_catalog']))
        self.assertEqual(ref, ref_for(packet['amount_contract']['amount_claims'][0]['metric']))
        self.assertEqual(packed['review_metric_catalog'][ref], before['amount_contract']['amount_claims'][0]['metric'])

    def test_short_and_no_amount_packets_are_unchanged_independent_copies(self):
        for packet in ({'draft': {'text': '你好'}}, packet_fixture(0), packet_fixture(1)):
            with self.subTest(packet=packet.keys()):
                packed = pack_review_evidence(packet)
                self.assertEqual(packed, packet)
                self.assertIsNot(packed, packet)
                self.assertEqual(unpack_review_evidence(packed), packet)

    def test_unique24_is_not_forced_into_larger_catalog(self):
        packet = packet_fixture(unique=True)
        self.assertEqual(pack_review_evidence(packet), packet)

    def test_none_types_and_nested_assumptions_remain_exact(self):
        packet = packet_fixture()
        for row in packet['amount_contract']['amount_claims']:
            row['metric']['value_minor'] = None
            row['metric']['calculation_status'] = 'unavailable'
            row['metric']['coverage_gaps'] = ['尚未确认']
            row['metric']['assumed_inputs'].append({'optional': None, 'ratio': '0.20',
                'flags': [False, True], 'nested': {'count': 0, 'decimal_number': 1.25}})
            row['claim']['value_minor'] = None
            row['claim']['calculation_status'] = 'unavailable'
        packed = pack_review_evidence(packet)
        restored = unpack_review_evidence(packed)
        self.assertEqual(restored, packet)
        metric = restored['amount_contract']['amount_claims'][0]['metric']
        self.assertIsNone(metric['value_minor'])
        self.assertIs(type(metric['assumed_inputs'][0]['confirmed']), bool)
        self.assertEqual(metric['assumed_inputs'][1]['ratio'], '0.20')

    def test_existing_compact_source_protocol_roundtrips_without_registration(self):
        packet = packet_fixture()
        versions = deepcopy(packet['amount_contract']['amount_claims'][0]['metric']['source_versions'])
        packet['source_version_sets'] = {'vset_1234abcd': versions}
        packet['metric_source_policy'] = 'unchanged-producer-policy'
        for row in packet['amount_contract']['amount_claims']:
            for item in (row['metric'], row['claim']):
                del item['source_versions']
                item['source_version_ref'] = 'vset_1234abcd'
        for item in packet['draft']['amount_claims']:
            del item['source_versions']
            item['source_version_ref'] = 'vset_1234abcd'
        before = deepcopy(packet)
        packed = pack_review_evidence(packet)
        self.assertEqual(unpack_review_evidence(packed), before)
        self.assertEqual(packed['source_version_sets'], before['source_version_sets'])
        self.assertEqual(packet, before)
        modified = deepcopy(packed)
        modified['source_version_sets']['vset_1234abcd']['obj_source_0'] = 'tampered'
        self.assert_error(lambda: unpack_review_evidence(modified), 'REVIEW_EVIDENCE_CATALOG_MISMATCH')

    def test_unknown_source_ref_is_not_filled_or_registered(self):
        packet = packet_fixture()
        row = packet['amount_contract']['amount_claims'][0]
        del row['metric']['source_versions']
        row['metric']['source_version_ref'] = 'vset_unknown'
        self.assert_error(lambda: pack_review_evidence(packet), 'REVIEW_EVIDENCE_UNKNOWN_SOURCE_REF')

    def test_same_metric_id_with_different_complete_original_is_rejected(self):
        packet = packet_fixture()
        row = packet['amount_contract']['amount_claims'][-1]
        row['metric']['excluded_items'].append('新增排除项')
        self.assert_error(lambda: pack_review_evidence(packet), 'REVIEW_EVIDENCE_METRIC_CONFLICT')

    def test_full_legacy_decode_also_rejects_same_id_conflicting_original(self):
        packet = packet_fixture()
        packet['amount_contract']['amount_claims'][-1]['metric']['coverage_gaps'].append('冲突原件')
        self.assert_error(lambda: unpack_review_evidence(packet), 'REVIEW_EVIDENCE_METRIC_CONFLICT')

    def test_same_metric_id_conflicting_catalog_entries_is_rejected(self):
        packed = pack_review_evidence(packet_fixture())
        metric = deepcopy(next(iter(packed['review_metric_catalog'].values())))
        metric['coverage_gaps'].append('另一原件')
        packed['review_metric_catalog'][ref_for(metric)] = metric
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_METRIC_CONFLICT')

    def test_unknown_reference_fails_without_mutating_packet(self):
        packed = pack_review_evidence(packet_fixture())
        packed['amount_contract']['amount_claims'][0]['metric_ref'] = 'rmetric_' + '0' * 64
        before = deepcopy(packed)
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_UNKNOWN_REF')
        self.assertEqual(packed, before)

    def test_missing_catalog_and_unused_entries_are_rejected(self):
        packed = pack_review_evidence(packet_fixture())
        del packed['review_metric_catalog']
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_UNKNOWN_REF')
        packed = pack_review_evidence(packet_fixture())
        metric = metric_fixture('other_metric')
        packed['review_metric_catalog'][ref_for(metric)] = metric
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_UNUSED_REF')

    def test_dual_forms_nested_tables_and_producer_reserved_fields_are_rejected(self):
        packed = pack_review_evidence(packet_fixture())
        row = packed['amount_contract']['amount_claims'][0]
        row['metric'] = deepcopy(next(iter(packed['review_metric_catalog'].values())))
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_RESERVED_FIELD')
        for location, key in (('root', 'review_metric_catalog'), ('row', 'metric_ref'),
                              ('draft', 'review_metric_catalog'), ('draft', 'metric_ref')):
            packet = packet_fixture()
            target = packet if location == 'root' else packet['draft'] if location == 'draft' else packet['amount_contract']['amount_claims'][0]
            target[key] = {}
            self.assert_error(lambda: pack_review_evidence(packet), 'REVIEW_EVIDENCE_RESERVED_FIELD')
        packed = pack_review_evidence(packet_fixture())
        packed['amount_contract']['review_metric_catalog'] = {}
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_RESERVED_FIELD')

    def test_all19_catalog_fields_are_hash_bound(self):
        packed = pack_review_evidence(packet_fixture())
        for field in next(iter(packed['review_metric_catalog'].values())):
            changed = deepcopy(packed)
            metric = next(iter(changed['review_metric_catalog'].values()))
            old = metric[field]
            metric[field] = ({'changed': 'value'} if type(old) is dict else
                             old + ['changed'] if type(old) is list else
                             old + 1 if type(old) is int else 'changed')
            with self.subTest(field=field):
                self.assert_error(lambda: unpack_review_evidence(changed))

    def test_swap_valid_ref_across_different_metrics_is_rejected(self):
        packet = packet_fixture()
        second = metric_fixture('other_metric')
        packet2 = packet_fixture()
        for row in packet2['amount_contract']['amount_claims']:
            row['metric'] = deepcopy(second)
            row['metric_id'] = second['metric_id']
            row['claim']['metric_id'] = second['metric_id']
        packet['amount_contract']['amount_claims'] += packet2['amount_contract']['amount_claims']
        packed = pack_review_evidence(packet)
        refs = list(packed['review_metric_catalog'])
        packed['amount_contract']['amount_claims'][0]['metric_ref'] = refs[1]
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_BINDING_MISMATCH')

    def test_claim_kind_signature_and_evidence_types_are_not_overwritten(self):
        for field, value in (('kind', 'forecast'), ('metric_id', 'wrong'), ('evidence_ids', 'wrong')):
            packed = pack_review_evidence(packet_fixture())
            packed['amount_contract']['amount_claims'][0][field] = value
            self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_BINDING_MISMATCH')
        packed = pack_review_evidence(packet_fixture())
        packed['amount_contract']['amount_claims'][0]['claim']['value_minor'] = 13001
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_BINDING_MISMATCH')

    def test_invalid_json_and_incomplete_metrics_fail_controlled(self):
        for value in (float('nan'), float('inf'), (1, 2), {1: 'nonstring-key'}, '\ud800'):
            self.assert_error(lambda: pack_review_evidence({'draft': value}), 'REVIEW_EVIDENCE_INVALID_JSON')
        for mutate in ('missing', 'extra', 'bool_amount', 'dual_source'):
            packet = packet_fixture()
            metric = packet['amount_contract']['amount_claims'][0]['metric']
            if mutate == 'missing':
                del metric['assumed_inputs']
            elif mutate == 'extra':
                metric['unexpected'] = 'not silently discarded'
            elif mutate == 'bool_amount':
                metric['value_minor'] = True
            else:
                metric['source_version_ref'] = 'vset_fake'
            self.assert_error(lambda: pack_review_evidence(packet))

    def test_malformed_kind_catalog_and_ref_fail_with_fixed_codes(self):
        packet = packet_fixture()
        packet['amount_contract']['amount_claims'][0]['kind'] = []
        self.assert_error(lambda: pack_review_evidence(packet), 'REVIEW_EVIDENCE_BINDING_MISMATCH')
        packed = pack_review_evidence(packet_fixture())
        packed['amount_contract']['amount_claims'][0]['metric_ref'] = []
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_UNKNOWN_REF')
        packed = pack_review_evidence(packet_fixture())
        packed['review_metric_catalog'] = []
        self.assert_error(lambda: unpack_review_evidence(packed), 'REVIEW_EVIDENCE_INVALID_CATALOG')
        packet = packet_fixture()
        packet['amount_contract']['amount_claims'][0]['metric']['excluded_items'] = ['\ud800']
        self.assert_error(lambda: pack_review_evidence(packet), 'REVIEW_EVIDENCE_INVALID_JSON')

    def test_local_aliases_text_and_all_claims_are_never_restored_or_normalized(self):
        packet = packet_fixture()
        packet['draft']['text'] = 'obj_subject_1　１３０元，\n原文保留'
        packet['draft']['claims'] = [{'text': '全角，逗号', 'evidence_ids': ['obj_evidence_1']}]
        before = deepcopy(packet)
        packed = pack_review_evidence(packet)
        self.assertEqual(packed['draft'], before['draft'])
        self.assertEqual(unpack_review_evidence(packed), before)

    def test_hash_is_consistency_not_authentication_of_recomputed_content(self):
        # 可信 producer 是前置边界；此 helper 不声称对完整重算攻击做 HMAC 鉴权。
        packed = pack_review_evidence(packet_fixture())
        ref, metric = next(iter(packed['review_metric_catalog'].items()))
        metric['excluded_items'].append('重算后的篡改仍需上游权限验签拒绝')
        replacement = ref_for(metric)
        packed['review_metric_catalog'] = {replacement: metric}
        for row in packed['amount_contract']['amount_claims']:
            row['metric_ref'] = replacement
        restored = unpack_review_evidence(packed)
        self.assertEqual(restored['amount_contract']['amount_claims'][0]['metric'], metric)

    def test_measured_json_size_decreases_without_truncation(self):
        packet = packet_fixture()
        packed = pack_review_evidence(packet)
        size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
        self.assertLess(size(packed), size(packet))
        self.assertEqual(unpack_review_evidence(packed), packet)


if __name__ == '__main__':
    unittest.main()
