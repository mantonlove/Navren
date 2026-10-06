"""内部资金原件合同的隔离测试；无模型、密钥、网络或现役银行库。"""
from copy import deepcopy
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch

from base_runtime.finance_funding import evaluate_source, read_funding_source
from simulated_bank import BankError


def fixture():
    refs = ['source/cash', 'source/events', 'source/holds', 'source/catalog']
    common = {'subject_id':'workspace-A', 'currency':'CNY', 'source_version':'v1'}
    return {'subject_id':'workspace-A', 'currency':'CNY', 'data_scope':'isolated_funding_source',
        'as_of':None, 'period_start':'2026-10-06', 'period_end':'2026-12-05',
        'coverage_complete':True, 'input_refs':refs,
        'source_versions':{ref:'v1' for ref in refs},
        'provenance':{'cash':['source/cash'], 'events':['source/events'],
                      'holds':['source/holds'], 'coverage':['source/catalog']},
        'catalog':{'complete':True, 'coverage_ref':'source/catalog',
                   'expected_event_ids':['bill','salary','forecast'], 'expected_hold_ids':['hold']},
        'records':{'cash':{**common, 'as_of':'2026-10-06', 'balance_minor':500000,
            'frozen_minor':20000, 'emergency_floor_minor':100000,
            'coverage_start':'2026-10-06', 'coverage_end':'2026-12-05',
            'evidence_ref':'source/cash', 'basis':'ledger_cash_balance', 'coverage_complete':True},
            'events':[{**common, 'event_id':identifier, 'on':on, 'amount_minor':amount,
                       'direction':direction, 'certainty':certainty,
                       'evidence_ref':'source/events', 'kind':'obligation'}
                      for identifier,on,amount,direction,certainty in (
                          ('bill','2026-10-20',80000,'outflow','confirmed'),
                          ('salary','2026-10-15',70000,'inflow','confirmed'),
                          ('forecast','2026-10-16',900000,'inflow','forecast'))],
            'holds':[{**common, 'hold_id':'hold', 'amount_minor':30000,
                      'evidence_ref':'source/holds', 'commitment_allocations':[['bill',30000]],
                      'release_on_payment':True, 'release_on':None}]}}


class Provider:
    def __init__(self, source):
        self.source = source
        self.calls = []

    def snapshot(self, workspace, days, *, db):
        self.calls.append((workspace, days, db))
        return self.source


class FundingSourceTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('network_forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)

    def read(self, source=None):
        return read_funding_source(Provider(fixture() if source is None else source), 'workspace-A', 60, self.db)

    def reject(self, source, code='METRIC_ARGUMENTS_INVALID', status=422):
        with self.assertRaises(BankError) as caught:
            self.read(source)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def test_known_source_arithmetic_and_exact_consumed_provenance(self):
        source = self.read()
        result = evaluate_source(source)
        budget = result['metrics']['current_budget']
        # 500000 - 20000 - 100000 - 30000 - 80000 + 30000 = 300000。
        self.assertEqual((result['status'], budget['value_minor']), ('calculated',300000))
        self.assertEqual((budget['basis'], budget['formula_id']), ('floor_surplus','funds.current_budget/1'))
        self.assertEqual(set(budget['input_refs']), set(fixture()['input_refs']))
        self.assertEqual(budget['source_versions'], fixture()['source_versions'])
        self.assertIn('forecast:forecast:not_counted',result['excluded_items'])

    def test_true_unknown_balance_keeps_complete_coverage_and_never_becomes_zero(self):
        raw = fixture();raw['records']['cash']['balance_minor'] = None
        before = deepcopy(raw)
        source = self.read(raw);result = evaluate_source(source)
        self.assertEqual(raw,before)
        self.assertTrue(source['coverage_complete'])
        self.assertTrue(source['records']['cash']['coverage_complete'])
        self.assertIsNone(source['records']['cash']['balance_minor'])
        budget = result['metrics']['current_budget']
        self.assertEqual((result['status'],budget['calculation_status']),('unavailable','unavailable'))
        self.assertIsNone(budget['value_minor'])
        self.assertEqual((budget['basis'],budget['formula_id']),('floor_surplus','funds.current_budget/1'))
        self.assertIn('missing_balance',budget['coverage_gaps'])
        self.assertIn('balance必须是非负整数分',budget['coverage_gaps'])
        self.assertIn('missing_balance',result['coverage_gaps'])
        self.assertTrue(all(metric['value_minor'] is None for metric in result['metrics'].values()))

    def test_authoritative_versions_are_attached_only_by_metric_service(self):
        source = self.read()
        authority = {'bank':'5','financial_methods_code':'method-sha256'}
        result = evaluate_source(source,additional_versions=authority.items())
        budget = result['metrics']['current_budget']
        self.assertEqual(budget['source_versions'],{**source['source_versions'],**authority})
        self.assertEqual(set(budget['input_refs']),set(source['input_refs']))
        self.assertEqual(source['source_versions'],fixture()['source_versions'])
        with self.assertRaises(BankError) as caught:
            evaluate_source(source,additional_versions=(('source/cash','v2'),))
        self.assertEqual((caught.exception.code,caught.exception.status),('METRIC_VERSION_CONFLICT',409))

    def test_unknown_obligation_or_hold_is_preserved_for_evaluator(self):
        for category in ('events','holds'):
            with self.subTest(category=category):
                raw = fixture();raw['records'][category][0]['amount_minor'] = None
                source = self.read(raw)
                self.assertIsNone(source['records'][category][0]['amount_minor'])
                result = evaluate_source(source)
                self.assertIsNone(result['metrics']['current_budget']['value_minor'])
                self.assertNotIn('missing_balance',result['coverage_gaps'])

    def test_explicit_incomplete_catalog_produces_unknown(self):
        raw = fixture();raw['coverage_complete'] = False;raw['catalog']['complete'] = False
        raw['catalog']['expected_event_ids'].append('missing-obligation')
        source = self.read(raw);budget = evaluate_source(source)['metrics']['current_budget']
        self.assertIsNone(budget['value_minor'])
        self.assertIn('input_coverage_incomplete',budget['coverage_gaps'])

    def test_zero_balance_is_a_known_amount_distinct_from_missing(self):
        raw = fixture();raw['records']['cash'].update(balance_minor=0,frozen_minor=0)
        raw['records']['events'] = [];raw['catalog']['expected_event_ids'] = []
        raw['records']['holds'] = [];raw['catalog']['expected_hold_ids'] = []
        known = self.read(raw);budget = evaluate_source(known)['metrics']['current_budget']
        self.assertEqual((budget['calculation_status'],budget['value_minor']),('calculated',-100000))
        raw['records']['cash']['balance_minor'] = None
        missing = self.read(raw)
        self.assertNotEqual(known['envelope_digest'],missing['envelope_digest'])
        self.assertIsNone(evaluate_source(missing)['metrics']['current_budget']['value_minor'])

    def test_cash_basis_is_rejected_before_pure_evaluation(self):
        for basis in ('floor_surplus','account_balance',None):
            with self.subTest(basis=basis):
                raw = fixture();raw['records']['cash']['basis'] = basis
                self.reject(raw)

    def test_bool_float_string_and_negative_minor_amounts_are_rejected(self):
        for bad in (True,False,1.0,'1',-1):
            for category,field in (('cash','balance_minor'),('cash','frozen_minor'),
                                    ('cash','emergency_floor_minor'),('events','amount_minor'),('holds','amount_minor')):
                with self.subTest(bad=bad,category=category,field=field):
                    raw = fixture();row = raw['records'][category]
                    (row if category=='cash' else row[0])[field] = bad
                    self.reject(raw)
        raw = fixture();raw['records']['holds'][0]['commitment_allocations'][0][1] = True
        self.reject(raw)

    def test_envelope_and_record_identity_are_bound(self):
        for category in ('envelope','cash','events','holds'):
            for field,value,code,status in (('subject_id','workspace-B','METRIC_SUBJECT_MISMATCH',403),
                                           ('currency','USD','METRIC_CURRENCY_UNSUPPORTED',422)):
                with self.subTest(category=category,field=field):
                    raw = fixture();row = raw if category=='envelope' else raw['records'][category]
                    (row if category in ('envelope','cash') else row[0])[field] = value
                    self.reject(raw,code,status)

    def test_refs_and_versions_cannot_escape_category_or_context(self):
        mutations = (
            lambda s:s['source_versions'].pop('source/cash'),
            lambda s:s['source_versions'].update({'source/cash':''}),
            lambda s:s['records']['cash'].update(source_version='v2'),
            lambda s:s['records']['cash'].update(evidence_ref='unbound'),
            lambda s:s['records']['cash'].update(evidence_ref='source/events'),
            lambda s:s['input_refs'].append('source/cash'),
            lambda s:s['source_versions'].update({'not-an-input':'v1'}),
            lambda s:s['provenance']['coverage'].clear(),
        )
        for index,mutate in enumerate(mutations):
            with self.subTest(index=index):
                raw = fixture();mutate(raw);self.reject(raw)

    def test_reserved_bank_and_service_names_cannot_be_overridden(self):
        for ref in ('bank','business_date','private_context','personal','demo_dataset',
                    'financial_methods_code','financial_funding_envelope_digest',
                    'finance_input/test','finance_adapter_code','finance_funding_input/test',
                    'finance_funding_contract','tool_table/accounts/workspace-A'):
            with self.subTest(ref=ref):
                raw = fixture();raw['input_refs'].append(ref);raw['source_versions'][ref] = 'v1'
                raw['provenance']['cash'].append(ref);self.reject(raw)

    def test_independent_catalog_must_match_complete_records(self):
        mutations = (
            lambda s:s['catalog']['expected_event_ids'].append('missing'),
            lambda s:s['catalog']['expected_hold_ids'].clear(),
            lambda s:s['catalog'].update(complete=False),
            lambda s:s.update(coverage_complete=1),
            lambda s:s['records']['cash'].update(coverage_complete=False),
            lambda s:s['records']['events'].append(deepcopy(s['records']['events'][0])),
        )
        for index,mutate in enumerate(mutations):
            with self.subTest(index=index):
                raw = fixture();mutate(raw);self.reject(raw)

    def test_period_and_snapshot_dates_must_match_request(self):
        for target,field,value in (('envelope','period_end','2026-12-04'),
                                   ('envelope','as_of','2026-10-06'),
                                   ('cash','as_of','2026-10-07'),
                                   ('cash','coverage_end','2026-12-04')):
            with self.subTest(target=target,field=field):
                raw = fixture();(raw if target=='envelope' else raw['records']['cash'])[field] = value
                self.reject(raw)

    def test_provider_reads_same_existing_transaction_without_writes(self):
        self.db.execute('CREATE TABLE source(payload TEXT)')
        self.db.execute('INSERT INTO source VALUES(?)',(json.dumps(fixture()),));self.db.commit()
        self.db.execute('BEGIN')
        before = self.db.total_changes
        expected_db = self.db
        class SQLiteProvider:
            def snapshot(self, workspace, days, *, db):
                self_db = db
                if self_db is not expected_db or not self_db.in_transaction:
                    raise AssertionError('same_transaction_required')
                return json.loads(db.execute('SELECT payload FROM source').fetchone()[0])
        source = read_funding_source(SQLiteProvider(),'workspace-A',60,self.db)
        self.assertEqual(evaluate_source(source)['metrics']['current_budget']['value_minor'],300000)
        self.assertEqual(self.db.total_changes,before)
        self.assertTrue(self.db.in_transaction)

    def test_full_envelope_digest_changes_despite_unchanged_source_versions(self):
        raw = fixture();first = self.read(raw)
        raw['records']['cash']['balance_minor'] += 1
        second = self.read(raw)
        self.assertEqual(first['source_versions'],second['source_versions'])
        self.assertNotEqual(first['envelope_digest'],second['envelope_digest'])
        raw['records']['cash']['balance_minor'] -= 1
        self.assertEqual(first['envelope_digest'],self.read(raw)['envelope_digest'])
        second['records']['cash']['balance_minor'] += 1
        with self.assertRaises(BankError) as caught:evaluate_source(second)
        self.assertEqual((caught.exception.code,caught.exception.status),('METRIC_ARGUMENTS_INVALID',422))

    def test_source_is_copied_and_caller_cannot_add_metric_fields(self):
        raw = fixture();provider = Provider(raw)
        source = read_funding_source(provider,'workspace-A',60,self.db)
        self.assertEqual(provider.calls,[('workspace-A',60,self.db)])
        raw['records']['cash']['balance_minor'] = None
        self.assertEqual(source['records']['cash']['balance_minor'],500000)
        raw = fixture();raw['funds_value_minor'] = 500000;self.reject(raw)
        raw = fixture();raw['records']['cash']['formula_id'] = 'funds.current_budget/1';self.reject(raw)

    def test_invalid_provider_horizon_and_missing_transaction_are_rejected(self):
        for provider,days,db in ((object(),60,self.db),(Provider(fixture()),True,self.db),
                                 (Provider(fixture()),0,self.db),(Provider(fixture()),366,self.db),
                                 (Provider(fixture()),60,None)):
            with self.subTest(days=days,db_missing=db is None):
                with self.assertRaises(BankError) as caught:
                    read_funding_source(provider,'workspace-A',days,db)
                self.assertEqual((caught.exception.code,caught.exception.status),('METRIC_ARGUMENTS_INVALID',422))


if __name__ == '__main__':
    unittest.main()
