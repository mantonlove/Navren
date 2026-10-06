"""风险来源资格的隔离合同测试；不改冻结oracle、不调用模型或现役服务。"""
import copy
from dataclasses import replace
import unittest

from base_runtime.finance_qualification import qualify_risk
from financial_methods.allocation import FormalRisk
from financial_methods.contracts import MetricContext
from simulated_bank import BankError


class RiskQualificationTests(unittest.TestCase):
    def setUp(self):
        self.context = MetricContext('person-1', 'CNY', 'fixture_only', as_of='2026-10-06',
            input_refs=('fixture:assessment', 'fixture:confirmation'),
            source_versions=(('fixture:assessment', 'assessment-v1'),
                             ('fixture:confirmation', 'confirmation-v1')))
        self.provenance = {'risk': ['fixture:assessment', 'fixture:confirmation']}
        self.risk = dict(subject_id='person-1', currency='CNY', data_scope='fixture_only',
            level=3, confirmed=True, effective_on='2026-09-01', valid_through='2027-09-01',
            input_refs=['fixture:assessment', 'fixture:confirmation'],
            source_versions={'fixture:assessment': 'assessment-v1',
                             'fixture:confirmation': 'confirmation-v1'})
        self.source = dict(configuration_source='formal_assessment',
            not_real_risk_assessment=False, assessment_type='formal_assessment',
            assessment_status='completed', subject_id='person-1', currency='CNY',
            data_scope='fixture_only', assessment_ref='fixture:assessment',
            source_version='assessment-v1', confirmation_source='subject_confirmation',
            confirmation_ref='fixture:confirmation', confirmation_source_version='confirmation-v1',
            confirmed_by_subject_id='person-1', effective_on='2026-09-01',
            valid_through='2027-09-01', level=3)

    def call(self, *, risk='default', source='default', context=None, provenance=None):
        return qualify_risk(self.risk if risk == 'default' else risk,
            self.source if source == 'default' else source, context or self.context,
            self.provenance if provenance is None else provenance)

    def error(self, operation, status=422):
        with self.assertRaises(BankError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(caught.exception.code,
            'METRIC_VERSION_CONFLICT' if status == 409 else 'METRIC_ARGUMENTS_INVALID')

    def test_complete_bound_fixture_qualifies_without_production_claim(self):
        result, reasons, classification = self.call()
        self.assertIsInstance(result, FormalRisk)
        self.assertEqual((reasons, classification), ([], 'formal_assessment'))
        self.assertEqual(result.level, 3)
        self.assertEqual(dict(result.source_versions), self.risk['source_versions'])

    def test_typed_risk_uses_same_contract(self):
        row = {**self.risk, 'input_refs': tuple(self.risk['input_refs']),
               'source_versions': tuple(self.risk['source_versions'].items())}
        self.assertEqual(self.call(risk=FormalRisk(**row))[0], FormalRisk(**row))

    def test_c14_synthetic_confirmation_cannot_qualify(self):
        source = {'configuration_source': 'navren_synthetic', 'not_real_risk_assessment': True}
        self.assertEqual(self.call(source=source),
            (None, ['risk_source_not_formal'], 'personal_simulation'))
        self.assertTrue(self.risk['confirmed'])

    def test_nonformal_classifications_do_not_consume_formal_evidence(self):
        for configuration, classification in (('personal_simulation', 'personal_simulation'),
                ('navren_synthetic', 'personal_simulation'), ('ordinary_profile', 'ordinary_profile')):
            with self.subTest(configuration=configuration):
                result, reasons, actual = self.call(source={'configuration_source': configuration},
                    provenance={'risk': []})
                self.assertIsNone(result)
                self.assertEqual((reasons, actual), (['risk_source_not_formal'], classification))
        self.assertEqual(self.call(source={'not_real_risk_assessment': True})[2], 'personal_simulation')

    def test_missing_source_stays_missing_despite_confirmed_risk(self):
        for source in (None, {}, {'configuration_source': 'missing'}):
            with self.subTest(source=source):
                self.assertEqual(self.call(source=source), (None, ['risk_source_missing'], 'missing'))

    def test_native_missing_risk_with_ordinary_profile_does_not_qualify(self):
        self.assertEqual(self.call(risk=None, source={'configuration_source': 'ordinary_profile'}),
            (None, ['risk_source_not_formal'], 'ordinary_profile'))

    def test_missing_formal_risk_stays_missing(self):
        self.assertEqual(self.call(risk=None), (None, ['formal_risk_missing'], 'formal_assessment'))

    def test_confirmed_bool_without_formal_source_contract_is_rejected(self):
        self.error(lambda: self.call(source={'configuration_source': 'formal_assessment'}))
        self.error(lambda: self.call(source={'configuration_source': 'formal_assessment', 'confirmed': True}))

    def test_formal_and_simulation_markers_conflict(self):
        for source in ({**self.source, 'not_real_risk_assessment': True},
                {**self.source, 'configuration_source': 'navren_synthetic'},
                {**self.source, 'configuration_source': 'ordinary_profile'}):
            with self.subTest(source=source):
                self.error(lambda: self.call(source=source))

    def test_completion_and_self_confirmation_are_actual_dependencies(self):
        for status in ('pending', 'in_progress', 'cancelled'):
            with self.subTest(status=status):
                self.assertEqual(self.call(source={**self.source, 'assessment_status': status}),
                    (None, ['formal_risk_not_completed'], 'formal_assessment'))
        for changes in ({'confirmation_source': 'profile'}, {'confirmed_by_subject_id': 'person-2'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.call(source={**self.source, **changes}),
                    (None, ['formal_risk_not_subject_confirmed'], 'formal_assessment'))
        self.assertEqual(self.call(risk={**self.risk, 'confirmed': False}),
            (None, ['formal_risk_not_subject_confirmed'], 'formal_assessment'))

    def test_missing_grade_or_dates_cannot_qualify(self):
        for name in ('level', 'effective_on', 'valid_through'):
            with self.subTest(name=name):
                self.assertEqual(self.call(risk={**self.risk, name: None},
                    source={**self.source, name: None}),
                    (None, ['formal_risk_assessment_details_missing'], 'formal_assessment'))

    def test_expired_and_future_formal_assessment_remain_unavailable(self):
        for name, value in (('effective_on', '2026-11-01'), ('valid_through', '2026-10-05')):
            with self.subTest(name=name):
                self.assertEqual(self.call(risk={**self.risk, name: value},
                    source={**self.source, name: value}),
                    (None, ['formal_risk_not_current'], 'formal_assessment'))

    def test_assessment_current_at_period_start_matches_pure_contract(self):
        context = replace(self.context, as_of=None, period_start='2026-10-06', period_end='2026-10-31')
        self.assertIsInstance(self.call(context=context)[0], FormalRisk)

    def test_context_binding_cannot_be_inferred_from_customer_profile(self):
        for field, value in (('subject_id', 'person-2'), ('currency', 'USD'), ('data_scope', 'customer_json')):
            with self.subTest(field=field):
                self.error(lambda: self.call(source={**self.source, field: value}))
                self.error(lambda: self.call(risk={**self.risk, field: value}))
        self.error(lambda: self.call(risk={**self.risk, 'profile': {'confirmed': True}}))

    def test_source_risk_dates_and_grade_must_match_exactly(self):
        for field, value in (('effective_on', '2026-09-02'), ('valid_through', '2027-09-02'), ('level', 4)):
            with self.subTest(field=field):
                self.error(lambda: self.call(source={**self.source, field: value}))

    def test_assessment_and_confirmation_each_need_actual_bound_refs(self):
        for field in ('assessment_ref', 'confirmation_ref'):
            with self.subTest(field=field):
                self.error(lambda: self.call(source={**self.source, field: 'customer:profile'}))
        self.error(lambda: self.call(provenance={'risk': ['fixture:assessment']}))
        self.error(lambda: self.call(context=replace(self.context, input_refs=('fixture:assessment',))))
        risk = {**self.risk, 'input_refs': ['fixture:assessment'],
                'source_versions': {'fixture:assessment': 'assessment-v1'}}
        self.error(lambda: self.call(risk=risk))

    def test_source_and_record_version_drift_is_409(self):
        for field in ('source_version', 'confirmation_source_version'):
            with self.subTest(field=field):
                self.error(lambda: self.call(source={**self.source, field: 'stale'}), 409)
        risk = copy.deepcopy(self.risk)
        risk['source_versions']['fixture:assessment'] = 'stale'
        self.error(lambda: self.call(risk=risk), 409)
        risk['source_versions'] = [('fixture:assessment', 'assessment-v1'),
                                   ('fixture:assessment', 'stale')]
        self.error(lambda: self.call(risk=risk), 409)

    def test_one_combined_assessment_confirmation_ref_can_be_explicitly_bound(self):
        risk = {**self.risk, 'input_refs': ['fixture:assessment'],
                'source_versions': {'fixture:assessment': 'assessment-v1'}}
        source = {**self.source, 'confirmation_ref': 'fixture:assessment',
                  'confirmation_source_version': 'assessment-v1'}
        self.assertIsInstance(self.call(risk=risk, source=source)[0], FormalRisk)

    def test_malformed_structures_bool_grade_and_bad_dates_are_422(self):
        for risk in ([], 'profile', {**self.risk, 'confirmed': 'true'},
                {**self.risk, 'level': True}, {**self.risk, 'level': 6},
                {**self.risk, 'effective_on': '2026-9-01'},
                {**self.risk, 'input_refs': 'fixture:assessment'},
                {**self.risk, 'source_versions': ['fixture:assessment']}):
            with self.subTest(risk=risk):
                self.error(lambda: self.call(risk=risk))
        for source in ([], 'formal_assessment', {'configuration_source': 'customer_json'},
                {'configuration_source': []}, {'configuration_source': {'confirmed': True}},
                {**self.source, 'assessment_status': True},
                {**self.source, 'assessment_status': []},
                {**self.source, 'not_real_risk_assessment': 'false'},
                {**self.source, 'assessment_ref': ''}, {**self.source, 'level': True},
                {**self.source, 'valid_through': '2026-08-01'},
                {'configuration_source': 'navren_synthetic', 'assessment_ref': []}):
            with self.subTest(source=source):
                self.error(lambda: self.call(source=source))
        self.error(lambda: qualify_risk(self.risk, self.source, {}, self.provenance))
        self.error(lambda: self.call(provenance={'risk': 'fixture:assessment'}))

    def test_no_input_is_mutated(self):
        originals = copy.deepcopy((self.risk, self.source, self.provenance))
        self.call()
        self.assertEqual((self.risk, self.source, self.provenance), originals)


if __name__ == '__main__':
    unittest.main()
