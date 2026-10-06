"""真实隔离Base发行/回查；受信来源从同一SQLite快照读取，绝不写现役。"""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from finance_gateway import FinanceGateway
from financial_metrics import fingerprint
from agent_runtime.metric_claims import MetricRegistry
from base_runtime.evidence import MetricEvidenceObserver
from simulated_bank import BankError


class SQLiteSources:
    def snapshot(self, workspace, name, args, *, db):
        row = db.execute('SELECT payload FROM adapter_test_sources WHERE workspace_id=? AND name=?',
                         (workspace, name)).fetchone()
        if row is None:
            raise AssertionError('fixture_not_installed')
        return json.loads(row['payload'])


class FinancialMethodAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='navren-method-adapter-')
        cls.env = patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(Path(cls.folder.name)/'knowledge.sqlite3')})
        cls.env.start()
        cls.network = patch.object(socket, 'create_connection', side_effect=AssertionError('network_forbidden'))
        cls.network.start()
        cls.g = FinanceGateway(Path(cls.folder.name)/'bank.sqlite3')
        cls.service_evidence = []
        cls.g.finance_method_sources = SQLiteSources()
        with cls.g.bank.transaction() as db:
            db.execute('CREATE TABLE adapter_test_sources(workspace_id TEXT,name TEXT,payload TEXT,PRIMARY KEY(workspace_id,name))')

    @classmethod
    def tearDownClass(cls):
        cls.network.stop();cls.env.stop();cls.folder.cleanup()

    def setUp(self):
        self.w, self.token = self.g.create_session()
        self.s = self.g.financial_metrics
        self.g.overview(self.w)

    def envelope(self, *, period=False, scope=None, data_scope='independent_synthetic'):
        refs = ['fixture/'+k for k in ('components', 'coverage', 'opening_cash', 'closing_cash',
                                      'movements', 'refund_originals', 'goals', 'products', 'risk')]
        value = {'subject_id': scope or self.w, 'currency': 'CNY', 'data_scope': data_scope,
            'coverage_complete': True, 'input_refs': refs, 'source_versions': {r: 'v1' for r in refs},
            'records': {}, 'catalog': {'complete': True, 'coverage_ref': 'fixture/coverage'},
            'provenance': {k: ['fixture/'+k] for k in ('components', 'coverage', 'opening_cash',
                         'closing_cash', 'movements', 'refund_originals', 'goals', 'products', 'risk')}}
        value.update(as_of=None if period else '2026-10-03', period_start='2026-10-01' if period else None,
                     period_end='2026-10-03' if period else None)
        return value

    def save(self, name, value):
        with self.g.bank.transaction() as db:
            db.execute('INSERT OR REPLACE INTO adapter_test_sources VALUES(?,?,?)', (self.w, name, json.dumps(value)))

    def point(self):
        value = self.envelope()
        keys = ['cash-a', 'fund-a', 'property-a', 'loan-a']  # 独立对象目录；不取返回行来生成。
        value['catalog']['expected_object_ids'] = keys
        value['records']['components'] = [dict(object_id=i, subject_id=self.w, currency='CNY',
            as_of='2026-10-03', kind=k, amount_minor=n, basis=b,
            evidence_ref='fixture/components', source_version='v1')
            for i,k,n,b in zip(keys, ('cash','financial_asset','nonfinancial_asset','liability'),
                             (100000,50000,200000,80000),
                             ('ledger_cash_balance','carrying_value','carrying_value','outstanding_principal'))]
        return value

    def cash(self):
        value = self.envelope(period=True)
        specs = [('borrow', 'loan_proceeds', 100000, None, 'cash-a'),
                 ('repay', 'loan_principal_repayment', -50000, None, 'cash-a'),
                 ('interest', 'loan_interest', -1000, None, 'cash-a'),
                 ('fee', 'expense', -10000, None, 'cash-a'),
                 ('refund', 'refund', 2000, 'fee', 'cash-a'),
                 ('buy', 'investment_purchase', -20000, None, 'cash-a'),
                 ('x1', 'internal_transfer', -200, 'pair', 'cash-a'),
                 ('x2', 'internal_transfer', 200, 'pair', 'cash-b')]
        movements = [dict(movement_id=i, subject_id=self.w, account_id=a, currency='CNY',
            on='2026-10-02', kind=k, delta_minor=n, related_id=r,
            evidence_ref='fixture/movements', source_version='v1') for i,k,n,r,a in specs]
        value['records'] = {'opening_cash_minor':100000,'closing_cash_minor':121000, 'movements':movements,
            'refund_originals':[deepcopy(movements[3])], 'refund_cumulative_minor':{'fee':2000}}
        value['catalog'].update(expected_movement_ids=['borrow','repay','interest','fee','refund','buy','x1','x2'],
                              expected_account_ids=['cash-a','cash-b'], refunds_complete=True, expected_refund_ids=['refund'])
        value['provenance']['refund_cumulative']=['fixture/refund_originals']
        return value

    def args(self, value, **extra):
        return {'scope':self.w, 'source':'simulation', 'expected_versions':{},
                **{k:value[k] for k in ('as_of','period_start','period_end')}, **extra}

    def reject(self, code, action, status=None):
        with self.assertRaises(BankError) as caught: action()
        self.assertEqual(caught.exception.code, code)
        if status is not None:self.assertEqual(caught.exception.status,status)

    def report(self, name, args):
        with closing(self.g.bank.connect()) as db:
            before=fingerprint({t:[dict(x) for x in db.execute('SELECT * FROM '+t+' WHERE workspace_id=? ORDER BY rowid',(self.w,))]
                for t in ('accounts','holdings','orders','operations','transactions','audit')})
        result = self.s.method_report(self.w, name, args)
        self.s.validate(self.w,result['metric_batch'])
        with closing(self.g.bank.connect()) as db:
            after=fingerprint({t:[dict(x) for x in db.execute('SELECT * FROM '+t+' WHERE workspace_id=? ORDER BY rowid',(self.w,))]
                for t in ('accounts','holdings','orders','operations','transactions','audit')})
            provider=getattr(self.g,'finance_method_sources',None)
            source=provider.snapshot(self.w,name,args,db=db) if provider else {'type':'native'}
        self.assertEqual(before,after)
        self.service_evidence.append({'test':self.id(),'arguments':args,'input':source,'result':result,
            'financial_state_before':before,'financial_state_after':after,'no_side_effect':before==after})
        return result

    def amounts(self,result):
        return {m['metric_name']:m['value_minor'] for m in result['metrics']}

    def test_independent_book_balance_and_cash_do_not_become_investable_cash(self):
        v=self.point();self.save('accounting_report',v)
        result=self.report('accounting_report',self.args(v))
        self.assertEqual(self.amounts(result), {'total_assets':350000,'total_liabilities':80000,'net_assets':270000,'cash_balance':100000})
        self.assertEqual(result['calculation_status'],'calculated')
        self.assertFalse(result['execution_authority'])

    def test_complete_cash_principal_refund_interest_and_transfer_case(self):
        v=self.cash();self.save('accounting_report',v)
        result=self.report('accounting_report',self.args(v))
        self.assertEqual(self.amounts(result),{'cash_income':0,'cash_expenses_gross':11000,'cash_refunds':2000,
            'net_cash_expenses':9000,'operating_cash_surplus':-9000,'total_cash_change':21000,
            'loan_principal_net':50000,'investment_transaction_cash_net':-20000})

    def test_independent_catalog_detects_missing_row(self):
        v=self.point();v['records']['components'].pop();self.save('accounting_report',v)
        r=self.report('accounting_report',self.args(v));self.assertTrue(all(m['value_minor'] is None for m in r['metrics']))

    def test_cash_catalog_cannot_be_inferred_from_residual_rows(self):
        v=self.cash();v['records']['movements'].pop();self.save('accounting_report',v)
        self.assertEqual(self.report('accounting_report',self.args(v))['calculation_status'],'unavailable')

    def test_true_zero_vs_unknown_coverage(self):
        v=self.cash();v['records'].update(movements=[],opening_cash_minor=0,closing_cash_minor=0,refund_originals=[],refund_cumulative_minor={})
        v['catalog']['expected_movement_ids']=[];v['catalog']['expected_refund_ids']=[];self.save('accounting_report',v)
        r=self.report('accounting_report',self.args(v));self.assertTrue(all(m['value_minor']==0 for m in r['metrics']))
        v['catalog']['complete']=False;self.save('accounting_report',v)
        r=self.report('accounting_report',self.args(v));self.assertTrue(all(m['value_minor'] is None for m in r['metrics']))

    def test_refund_original_in_prior_period_is_retained(self):
        v=self.cash();v['records']['refund_originals'][0].update(on='2026-09-30',movement_id='old-fee')
        v['records']['movements'][4]['related_id']='old-fee'
        v['records']['refund_cumulative_minor']={'old-fee':2000}
        self.save('accounting_report',v)
        self.assertEqual(self.amounts(self.report('accounting_report',self.args(v)))['cash_refunds'],2000)

    def test_refund_wrong_owner_original_fee_and_cumulative_excess_unavailable(self):
        for mutate in ('owner','fee','cumulative'):
            with self.subTest(mutate=mutate):
                v=self.cash()
                if mutate=='owner':v['records']['refund_originals'][0]['subject_id']='other'
                elif mutate=='fee':v['records']['refund_originals'][0]['kind']='income'
                else:v['records']['refund_cumulative_minor']['fee']=10001
                self.save('accounting_report',v)
                self.assertEqual(self.report('accounting_report',self.args(v))['calculation_status'],'unavailable')

    def test_refund_original_bad_date_or_exact_version_is_unavailable(self):
        for field,val in [('on',''),('source_version','v2')]:
            v=self.cash();v['records']['refund_originals'][0][field]=val;self.save('accounting_report',v)
            self.assertEqual(self.report('accounting_report',self.args(v))['calculation_status'],'unavailable')

    def test_proven_no_refunds_does_not_require_nonexistent_original_provenance(self):
        v=self.cash();v['records'].update(movements=[],opening_cash_minor=1000,closing_cash_minor=1000,
            refund_originals=[],refund_cumulative_minor={});v['catalog'].update(expected_movement_ids=[],expected_refund_ids=[])
        v['provenance'].pop('refund_originals');v['provenance'].pop('refund_cumulative');self.save('accounting_report',v)
        self.assertEqual(self.report('accounting_report',self.args(v))['calculation_status'],'calculated')

    def test_same_source_time_and_currency_counterexamples(self):
        for field, val in [('as_of','2026-10-02'),('currency','USD'),('subject_id','other')]:
            with self.subTest(field=field):
                v=self.point();v['records']['components'][0][field]=val;self.save('accounting_report',v)
                self.assertEqual(self.report('accounting_report',self.args(v))['calculation_status'],'unavailable')

    def test_illegal_request_and_monetary_structure_are_422(self):
        v=self.point();self.save('accounting_report',v)
        for update in ({'source':'other'},{'as_of':'10/03/2026'},{'period_start':'2026-10-01'}, {'expected_versions':[]},{'total_assets':9}):
            self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.method_report(self.w,'accounting_report',self.args(v,**update)),422)
        for bad in (True, 1.2, '100000'):
            v=self.point();v['records']['components'][0]['amount_minor']=bad;self.save('accounting_report',v)
            self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.method_report(self.w,'accounting_report',self.args(v)),422)

    def test_expected_source_conflict_is_409(self):
        v=self.point();self.save('accounting_report',v)
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.method_report(self.w,'accounting_report',self.args(v,expected_versions={'fixture/components':'v0'})),409)

    def test_consumed_snapshot_cannot_aba_change_after_version_check(self):
        first=self.returns_fixture('B02_internal_settlement');second=deepcopy(first)
        second['records']['closing']['cash_minor']+=100
        second['source_versions']['fixture:cash']='fixture-v2'
        observer=MetricEvidenceObserver(self.s)
        with patch.object(self.g.finance_method_sources,'snapshot',side_effect=[first,second,first,second,first]):
            args=self.returns_args(first);args['expected_versions']={'fixture:cash':'fixture-v1'}
            self.reject('METRIC_VERSION_CONFLICT',lambda:observer.method_report(self.w,'returns_report',args),409)
        self.assertEqual(observer.batches,[])

    def test_source_cannot_supply_reserved_digest_or_malformed_input_refs(self):
        for key in ('finance_input/accounting_report/'+self.w,'finance_adapter_code','tool_table/accounts/'+self.w):
            v=self.point();v['source_versions'][key]='forged';self.save('accounting_report',v)
            self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.method_report(self.w,'accounting_report',self.args(v)),422)
        for refs in (None,'fixture/components',[],[None]):
            v=self.point();v['input_refs']=refs;self.save('accounting_report',v)
            self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.method_report(self.w,'accounting_report',self.args(v)),422)

    def test_authoritative_result_recomputed_and_observer_consumes_white_list(self):
        v=self.point();self.save('accounting_report',v);a=self.args(v)
        result=self.report('accounting_report',a);observer=MetricEvidenceObserver(self.s)
        batch=observer.metrics_for_tool(self.w,'accounting_report',a,result)
        self.assertEqual(len(observer.batches),1);self.assertTrue(batch['metrics'])
        registry=MetricRegistry()
        for m in batch['metrics']:registry.register(m,kind=batch['metric_kinds'][m['metric_id']],evidence_id=batch['snapshot_id'])
        tamper=deepcopy(result);tamper['metrics'][0]['value_minor']+=1
        self.reject('METRIC_RESULT_MISMATCH',lambda:observer.metrics_for_tool(self.w,'accounting_report',a,tamper))

    def test_source_row_changes_without_bank_version_invalidate_signature(self):
        v=self.point();self.save('accounting_report',v);r=self.report('accounting_report',self.args(v))
        v['records']['components'][0]['amount_minor']+=1;self.save('accounting_report',v)
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.validate(self.w,r['metric_batch']))

    def test_missing_provenance_fails_whole_accounting_not_partial(self):
        v=self.cash();v['provenance'].pop('opening_cash');self.save('accounting_report',v)
        r=self.report('accounting_report',self.args(v));self.assertEqual(r['calculation_status'],'unavailable')
        self.assertTrue(all(m['value_minor'] is None for m in r['metrics']))

    def budget(self, amount=500000):
        initial=self.s.snapshot(self.w,60)['funding']['metrics']['current_budget']['value_minor']
        with self.g.bank.transaction() as db:
            account=db.execute('SELECT id FROM accounts WHERE workspace_id=? ORDER BY balance_minor DESC LIMIT 1',(self.w,)).fetchone()['id']
            db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?',(amount-initial,account))
            db.execute('UPDATE workspaces SET version=version+1 WHERE id=?',(self.w,))
        snapshot=self.s.snapshot(self.w,60)
        metric=snapshot['funding']['metrics']['current_budget']
        self.assertEqual(metric['value_minor'],amount)
        return metric

    def allocation(self, funds, *, mixed=True):
        v=self.envelope(period=True,data_scope=funds['data_scope'])
        v.update(as_of=funds['as_of'],period_start=funds['period_start'],period_end=funds['period_end'])
        common={'subject_id':self.w,'currency':'CNY','data_scope':funds['data_scope'], 'confirmed':True,
                'input_refs':['fixture/goals'],'source_versions':{'fixture/goals':'v1'}}
        key={'product_id':'fixture-p','share_class_id':'A','channel_id':'fixture-bank'}
        goals=[{**common,'goal_id':'cash-use','amount_minor':100000,'priority':1,'needed_on':funds['period_end'],'product_key':None}]
        products=[];risk=None
        if mixed:
            goals.append({**common,'goal_id':'investment-use','amount_minor':300000,'priority':2,'needed_on':funds['period_end'],'product_key':key})
            products=[dict(subject_id=self.w,currency='CNY',data_scope=funds['data_scope'],key=key,
                identity_verified=True,current_sale_verified=True,terms_complete=True,risk_level=2,
                minimum_minor=10000,step_minor=10000,entry_fixed_minor=0,entry_rate='0.01',
                exit_fixed_minor=0,exit_rate='0',fee_rounding='half_up',fee_basis='allocated_principal',
                exit_available_on=funds['period_end'],evidence_as_of=funds['period_start'],valid_through=funds['period_end'],
                input_refs=['fixture/products'],source_versions={'fixture/products':'v1'})]
            risk=dict(subject_id=self.w,currency='CNY',data_scope=funds['data_scope'],level=3,confirmed=True,
                effective_on=funds['period_start'],valid_through=funds['period_end'],
                input_refs=['fixture/risk'],source_versions={'fixture/risk':'v1'})
        v['records']={'goals':goals,'products':products,'risk':risk}
        if risk is not None:
            confirmation='fixture/risk-confirmation'
            v['input_refs'].append(confirmation);v['source_versions'][confirmation]='v1'
            v['provenance']['risk'].append(confirmation)
            risk['input_refs'].append(confirmation);risk['source_versions'][confirmation]='v1'
            v['records']['risk_source']={
                'configuration_source':'formal_assessment','assessment_type':'formal_assessment',
                'assessment_status':'completed','not_real_risk_assessment':False,
                'subject_id':self.w,'currency':'CNY','data_scope':funds['data_scope'],
                'assessment_ref':'fixture/risk','source_version':'v1',
                'confirmation_source':'subject_confirmation','confirmation_ref':confirmation,
                'confirmation_source_version':'v1','confirmed_by_subject_id':self.w,
                'effective_on':funds['period_start'],'valid_through':funds['period_end'],'level':3}
        return v

    def test_incomplete_transfer_has_exact_diagnostic_without_shrinking_catalog(self):
        v=self.cash();v['records']['movements']=[m for m in v['records']['movements'] if m['movement_id']!='x2']
        v['records']['closing_cash_minor']=120800;self.save('accounting_report',v)
        result=self.report('accounting_report',self.args(v))
        self.assertEqual(v['catalog']['expected_movement_ids'][-2:],['x1','x2'])
        self.assertEqual(result['calculation_status'],'unavailable')
        self.assertIn('cash_movement_coverage_incomplete',result['reasons'])
        self.assertIn('incomplete_internal_transfer_pair',result['reasons'])
        for metric in result['metrics']:
            self.assertIsNone(metric['value_minor'])
            self.assertIn('incomplete_internal_transfer_pair',metric['coverage_gaps'])

    def test_simulated_or_missing_risk_source_cannot_use_confirmed_bool(self):
        funds=self.budget()
        for source in ({'configuration_source':'navren_synthetic','not_real_risk_assessment':True},None):
            with self.subTest(source=source):
                v=self.allocation(funds);v['records']['risk_source']=source;self.save('allocation_preview',v)
                result=self.report('allocation_preview',self.allocation_args(funds));uses=result['plans'][1]
                self.assertTrue(v['records']['risk']['confirmed'])
                self.assertEqual(uses['calculation_status'],'unavailable')
                self.assertTrue(all(m['value_minor'] is None for m in uses['metrics'].values()))
                self.assertFalse(result['risk_qualification']['formal_qualification_verified'])
                self.assertIn('risk_source_not_formal' if source else 'risk_source_missing',result['reasons'])

    def funding_source(self,balance=None):
        with closing(self.g.bank.connect()) as db:start=self.g.bank._workspace(db,self.w)['as_of']
        from datetime import date,timedelta
        end=(date.fromisoformat(start)+timedelta(days=60)).isoformat()
        refs=['issuer/cash','issuer/events','issuer/holds','issuer/catalog']
        return {'subject_id':self.w,'currency':'CNY','data_scope':'isolated_funding_source',
            'as_of':None,'period_start':start,'period_end':end,'coverage_complete':True,
            'input_refs':refs,'source_versions':{r:'v1' for r in refs},
            'provenance':{'cash':[refs[0]],'events':[refs[1]],'holds':[refs[2]],'coverage':[refs[3]]},
            'catalog':{'complete':True,'coverage_ref':refs[3],'expected_event_ids':[],'expected_hold_ids':[]},
            'records':{'cash':{'subject_id':self.w,'currency':'CNY','as_of':start,'balance_minor':balance,
                'frozen_minor':0,'emergency_floor_minor':0,'coverage_start':start,'coverage_end':end,
                'evidence_ref':refs[0],'source_version':'v1','basis':'ledger_cash_balance','coverage_complete':True},
                'events':[],'holds':[]}}

    def test_trusted_missing_balance_issues_resolvable_unknown_budget_and_unavailable_plans(self):
        from test_base_finance_funding import Provider
        source=self.funding_source();self.g.finance_funding_sources=Provider(source)
        try:
            snapshot=self.s.snapshot(self.w,60);funds=snapshot['funding']['metrics']['current_budget']
            self.assertIsNone(source['records']['cash']['balance_minor'])
            self.assertEqual(funds['calculation_status'],'unavailable');self.assertIsNone(funds['value_minor'])
            self.assertIn('missing_balance',funds['coverage_gaps'])
            self.assertEqual(self.s.resolve_budget_metric(self.w,funds['metric_id']).to_dict(),funds)
            self.assertIn('bank',funds['source_versions']);self.assertIn('finance_funding_adapter_code',funds['source_versions'])
            v=self.allocation(funds);self.save('allocation_preview',v)
            result=self.report('allocation_preview',self.allocation_args(funds))
            for plan in result['plans']:
                self.assertEqual((plan['calculation_status'],plan['feasibility']),('unavailable','pending_dependencies'))
                self.assertIn('funds_not_calculated',plan['coverage_gaps'])
                self.assertTrue(all(m['value_minor'] is None for m in plan['metrics'].values()))
                self.assertFalse(plan['execution_authority'])
            self.assertEqual(result['funds'],funds)
        finally:del self.g.finance_funding_sources

    def test_funding_provider_source_change_and_removal_invalidate_registered_original(self):
        from test_base_finance_funding import Provider
        source=self.funding_source(500000);self.g.finance_funding_sources=Provider(source)
        try:
            snapshot=self.s.snapshot(self.w,60);funds=snapshot['funding']['metrics']['current_budget']
            self.assertEqual(funds['value_minor'],500000)
            source['records']['cash']['balance_minor']=500001
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.resolve_budget_metric(self.w,funds['metric_id']),409)
            source['records']['cash']['balance_minor']=500000
            del self.g.finance_funding_sources
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.resolve_budget_metric(self.w,funds['metric_id']),409)
        finally:
            if hasattr(self.g,'finance_funding_sources'):del self.g.finance_funding_sources

    def test_wrong_basis_source_cannot_legally_issue_or_register_budget(self):
        from test_base_finance_funding import Provider
        source=self.funding_source(500000);source['records']['cash']['basis']='account_balance'
        self.g.finance_funding_sources=Provider(source);before=len(self.s._funding_issuances)
        try:
            self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.snapshot(self.w,60),422)
            self.assertEqual(len(self.s._funding_issuances),before)
        finally:del self.g.finance_funding_sources

    def test_rehashed_funding_snapshot_cannot_pass_final_validation_without_issuance(self):
        from test_base_finance_funding import Provider
        self.g.finance_funding_sources=Provider(self.funding_source(500000))
        try:
            snapshot=self.s.snapshot(self.w,60)
            for field,value in (('value_minor',500001),('subject_id','foreign'),
                                ('period_start','2026-01-01'),('metric_id',None)):
                with self.subTest(field=field):
                    forged=deepcopy(snapshot);metric=forged['funding']['metrics']['current_budget']
                    if value is None:metric.pop(field)
                    else:metric[field]=value
                    forged['snapshot_id']='base-metrics-'+fingerprint({k:v for k,v in forged.items() if k!='snapshot_id'})
                    self.reject('METRIC_BUDGET_NOT_ISSUED',lambda:self.s.validate(self.w,forged),409)
            self.s.validate(self.w,snapshot)
        finally:del self.g.finance_funding_sources

    def test_funding_provider_cannot_write_bank_or_register_failed_read(self):
        source=self.funding_source(500000)
        class WritingProvider:
            def snapshot(provider,workspace,days,*,db):
                db.execute('UPDATE accounts SET balance_minor=balance_minor+1 WHERE workspace_id=?',(workspace,))
                return deepcopy(source)
        self.g.finance_funding_sources=WritingProvider()
        with closing(self.g.bank.connect()) as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY id',(self.w,))]
        registrations=len(self.s._funding_issuances)
        try:
            self.reject('METRIC_SOURCE_NOT_READONLY',lambda:self.s.snapshot(self.w,60),422)
            self.assertEqual(len(self.s._funding_issuances),registrations)
            with closing(self.g.bank.connect()) as db:
                after=[tuple(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY id',(self.w,))]
                self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0],0)
            self.assertEqual(before,after)
        finally:del self.g.finance_funding_sources

    def confirmed_goal_with_hold(self,target=100000):
        """真实reserve/本人goal确认，再发行已扣该hold的预算；不注册测试替身。"""
        from datetime import date,timedelta
        import uuid
        before_hold=self.s.snapshot(self.w,60)['funding']['metrics']['current_budget']['value_minor']
        archive=self.g.bank.customer_archive(self.w)
        account=max(archive['accounts'],key=lambda a:a['balance_minor'])['id']
        task=next(t for t in archive['records']['orchestration_tasks'] if t['status']=='draft')
        start=self.g.goals.state(self.w)['as_of']
        expiry=(date.fromisoformat(start)+timedelta(days=14)).isoformat()
        draft=self.g.bank.prepare(self.w,dict(action='reserve_funds',account_id=account,
            task_id=task['id'],amount_minor=30000,expires_on=expiry,idempotency_key=uuid.uuid4().hex))
        confirm={'confirmed':True,'confirmation_digest':draft['confirmation_digest']}
        if draft['permission']=='red':
            confirm['verification_token']=self.g.bank.verify(self.w,draft['id'],
                {'verification_code':self.g.demo_code(self.token)})['verification_token']
        hold_id=self.g.bank.confirm(self.w,draft['id'],confirm)['result']['hold_id']
        after_hold=self.s.snapshot(self.w,60)['funding']['metrics']['current_budget']['value_minor']
        self.assertEqual(before_hold-after_hold,30000)
        # 有效预留先按原保护完成；再设置隔离来源现金，让预算场景为70000。
        # 账户来源先固定，再经本人确认绑定其真实快照，不绕过prepare/confirm。
        self.budget(70000)
        goal_draft=self.g.goals.prepare(self.w,dict(action='create',name='本人确认并关联预留的现金用途',
            target_minor=target,due_on=(date.fromisoformat(start)+timedelta(days=3)).isoformat(),
            priority=0,funding_kind='cash',occupancy_hold_ids=[hold_id],occupancy_account_id=account,
            expected_version=self.g.goals.state(self.w)['version'],idempotency_key=uuid.uuid4().hex))
        receipt=self.g.goals.confirm(self.w,goal_draft['id'],
            {'confirmed':True,'confirmation_digest':goal_draft['confirmation_digest']})
        snapshot=self.s.snapshot(self.w,60)
        funds=snapshot['funding']['metrics']['current_budget']
        self.assertEqual(funds['value_minor'],70000)
        self.assertEqual(self.s.resolve_budget_metric(self.w,funds['metric_id']).to_dict(),funds)
        row=next(r for r in self.g.goals.constraints(self.w)['goals'] if r['goal_id']==receipt['goal']['id'])
        return funds,row,hold_id

    def test_C07_native_confirmed_occupancy_real_issuer_conserves_70000_new_cash(self):
        funds,row,hold_id=self.confirmed_goal_with_hold()
        self.assertEqual(row['already_occupied_minor'],30000)
        self.assertFalse(row['occupancy_verified_in_baseline'])
        self.assertIn('fund_holds/'+hold_id,funds['input_refs'])
        provider=self.g.finance_method_sources;del self.g.finance_method_sources
        try:
            args=self.allocation_args(funds);args['constraints_ref']='goals/'+row['goal_id']
            result=self.report('allocation_preview',args);plan=result['plans'][1]
            self.assertEqual(plan['calculation_status'],'calculated')
            self.assertEqual(plan['metrics']['assigned_cash']['value_minor'],70000)
            self.assertEqual(plan['metrics']['unassigned_cash']['value_minor'],0)
            self.assertEqual(plan['allocations'][0]['already_occupied_minor'],30000)
            self.assertEqual(plan['allocations'][0]['amount_minor'],70000)
            self.assertEqual(plan['conservation_difference_minor'],0)
            self.assertEqual(plan['cash_partition_difference_minor'],0)
            self.assertFalse(plan['execution_authority'])
        finally:self.g.finance_method_sources=provider

    def test_C08_duplicate_actual_hold_ref_is_infeasible_without_second_credit(self):
        funds,row,hold_id=self.confirmed_goal_with_hold(target=50000)
        source=self.allocation(funds,mixed=False)
        actual={**row,'data_scope':funds['data_scope']}
        source['input_refs']=list(dict.fromkeys([*source['input_refs'],*actual['input_refs']]))
        source['source_versions'].update(actual['source_versions'])
        source['provenance']['goals']=list(dict.fromkeys([*source['provenance']['goals'],*actual['input_refs']]))
        source['records']['goals']=[actual,{**deepcopy(actual),'goal_id':'unregistered-second-use',
            'occupancy_verified_in_baseline':True}]
        self.save('allocation_preview',source)
        result=self.report('allocation_preview',self.allocation_args(funds));plan=result['plans'][1]
        self.assertEqual((plan['calculation_status'],plan['feasibility']),('infeasible','infeasible'))
        self.assertIn('occupancy_reused:fund_holds/'+hold_id,plan['coverage_gaps'])
        self.assertTrue(all(m['value_minor'] is None for m in plan['metrics'].values()))
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM navren_goal_occupancy_links WHERE workspace_id=? AND hold_id=?',
                (self.w,hold_id)).fetchone()[0],1)

    def allocation_args(self,funds):
        return {'scope':self.w,'budget_metric_id':funds['metric_id'],'constraints_ref':'fixture/confirmed',
                'product_evidence_refs':[],'expected_versions':{}}

    def test_real_issued_funds_conservation_500000(self):
        funds=self.budget();v=self.allocation(funds);self.save('allocation_preview',v)
        r=self.report('allocation_preview',self.allocation_args(funds));uses=r['plans'][1]
        self.assertEqual(uses['calculation_status'],'calculated')
        m={k:x['value_minor'] for k,x in uses['metrics'].items()}
        self.assertEqual(m,{'investment_principal':300000,'assigned_cash':100000,'entry_fees':3000,
                          'exit_fee_reserve':0,'remaining_cash':197000,'unassigned_cash':97000})
        self.assertEqual(500000,100000+300000+3000+97000)
        self.assertEqual(uses['conservation_difference_minor'],0)
        self.assertEqual(uses['cash_partition_difference_minor'],0)
        self.assertTrue(all(kind=='planning_scenario' for kind in r['metric_batch']['metric_kinds'].values()))
        self.assertEqual(r['funds'],funds)

    def test_cash_goals_do_not_require_products_or_risk(self):
        funds=self.budget();v=self.allocation(funds,mixed=False);self.save('allocation_preview',v)
        self.assertEqual(self.report('allocation_preview',self.allocation_args(funds))['plans'][1]['calculation_status'],'calculated')

    def test_goal_product_and_risk_input_versions_cannot_escape_context(self):
        funds=self.budget()
        for category in ('goals','products','risk'):
            v=self.allocation(funds)
            row=v['records'][category][0] if category!='risk' else v['records']['risk']
            row['source_versions']['fixture/'+category]='v2';self.save('allocation_preview',v)
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.method_report(self.w,'allocation_preview',self.allocation_args(funds)),409)
            for ref in ('unbound/'+category,'fixture/coverage'):
                v=self.allocation(funds)
                row=v['records'][category][0] if category!='risk' else v['records']['risk']
                row['input_refs']=[ref];row['source_versions']={ref:'v1'};self.save('allocation_preview',v)
                self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.method_report(self.w,'allocation_preview',self.allocation_args(funds)),422)
        v=self.allocation(funds,mixed=False);v['records']['goals'][0].pop('product_key');self.save('allocation_preview',v)
        self.assertEqual(self.report('allocation_preview',self.allocation_args(funds))['plans'][1]['calculation_status'],'unavailable')

    def test_bank_guard_validation_does_not_wait_for_issuer_bank_lock(self):
        funds=self.budget();v=self.allocation(funds,mixed=False);self.save('allocation_preview',v)
        args=self.allocation_args(funds);batch=self.report('allocation_preview',args)['metric_batch']
        ready=threading.Event();done=threading.Event();errors=[]
        def read():
            ready.set()
            try:self.s.method_report(self.w,'allocation_preview',args)
            except Exception as error:errors.append(error)
            finally:done.set()
        with self.g.bank.transaction() as db:
            worker=threading.Thread(target=read);worker.start()
            self.assertTrue(ready.wait(1))
            self.s.validate(self.w,batch,db=db)
            self.assertFalse(done.is_set())
        worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])

    def test_missing_goals_or_priority_and_mixed_missing_risk_whole_use_unavailable(self):
        funds=self.budget()
        for mode in ('no_goals','priority','risk','confirmation'):
            with self.subTest(mode=mode):
                v=self.allocation(funds)
                if mode=='no_goals':v['records']['goals']=[]
                elif mode=='priority':v['records']['goals'][0]['priority']=None
                elif mode=='confirmation':v['records']['goals'][0]['confirmed']=False
                else:v['records']['risk']=None
                self.save('allocation_preview',v);r=self.report('allocation_preview',self.allocation_args(funds))
                self.assertEqual([p['calculation_status'] for p in r['plans']],['calculated','unavailable'])
                self.assertTrue(all(m['value_minor'] is None for m in r['plans'][1]['metrics'].values()))

    def test_shortfall_retains_partial_and_conservation(self):
        funds=self.budget(200000);v=self.allocation(funds);self.save('allocation_preview',v)
        uses=self.report('allocation_preview',self.allocation_args(funds))['plans'][1]
        self.assertEqual(uses['calculation_status'],'partial');self.assertEqual(uses['feasibility'],'infeasible')
        self.assertEqual(uses['metrics']['assigned_cash']['value_minor'],100000)
        self.assertEqual(uses['conservation_difference_minor'],0)

    def test_priority_change_consumed_and_not_just_explanation(self):
        funds=self.budget(350000);v=self.allocation(funds);self.save('allocation_preview',v)
        a=self.report('allocation_preview',self.allocation_args(funds))['plans'][1]
        v['records']['goals'][0]['priority']=3;self.save('allocation_preview',v)
        b=self.report('allocation_preview',self.allocation_args(funds))['plans'][1]
        self.assertEqual(a['metrics']['assigned_cash']['value_minor'],100000)
        self.assertEqual(b['metrics']['investment_principal']['value_minor'],300000)

    def test_negative_funds_not_clamped_and_both_infeasible(self):
        funds=self.budget(-10000);v=self.allocation(funds);self.save('allocation_preview',v)
        r=self.report('allocation_preview',self.allocation_args(funds))
        self.assertEqual(r['funds']['value_minor'],-10000)
        self.assertEqual([p['calculation_status'] for p in r['plans']],['infeasible','infeasible'])

    def test_unissued_client_metric_and_direct_numeric_budget_rejected(self):
        self.reject('METRIC_BUDGET_NOT_ISSUED',lambda:self.s.resolve_budget_metric(self.w,'fm-made-up'),409)
        funds=self.budget();v=self.allocation(funds);self.save('allocation_preview',v)
        a=self.allocation_args(funds);a['funds_minor']=500000
        self.reject('METRIC_ARGUMENTS_INVALID',lambda:self.s.method_report(self.w,'allocation_preview',a),422)

    def test_budget_amount_change_without_bank_version_invalidates_even_unchanged_cash_snapshot_hash(self):
        funds=self.budget()
        with self.g.bank.transaction() as db:db.execute('UPDATE accounts SET balance_minor=balance_minor+1 WHERE workspace_id=?',(self.w,))
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.resolve_budget_metric(self.w,funds['metric_id']),409)

    def test_cross_scope_date_and_fee_changes_block_or_change_result(self):
        funds=self.budget();v=self.allocation(funds);self.save('allocation_preview',v)
        first=self.report('allocation_preview',self.allocation_args(funds))
        v['records']['products'][0]['entry_rate']='0.02';self.save('allocation_preview',v)
        second=self.report('allocation_preview',self.allocation_args(funds))
        self.assertEqual(second['plans'][1]['metrics']['entry_fees']['value_minor'],6000)
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.s.validate(self.w,first['metric_batch']))
        v['records']['goals'][0]['needed_on']='2026-01-01';self.save('allocation_preview',v)
        self.assertEqual(self.report('allocation_preview',self.allocation_args(funds))['plans'][1]['calculation_status'],'infeasible')
        v['records']['goals'][0]['data_scope']='historical_research_portfolio';self.save('allocation_preview',v)
        self.assertEqual(self.report('allocation_preview',self.allocation_args(funds))['plans'][1]['calculation_status'],'infeasible')

    def test_readonly_no_financial_side_effect_and_signature_tamper_rejected(self):
        v=self.point();self.save('accounting_report',v)
        def state():
            with closing(self.g.bank.connect()) as db:
                return fingerprint({t:[dict(x) for x in db.execute('SELECT * FROM '+t+' WHERE workspace_id=? ORDER BY rowid',(self.w,))]
                    for t in ('accounts','holdings','orders','operations','transactions','audit')})
        before=state();r=self.report('accounting_report',self.args(v));self.assertEqual(before,state())
        bad=deepcopy(r['metric_batch']);bad['metrics'][0]['value_minor']+=1
        self.reject('METRIC_TAMPERED',lambda:self.s.validate(self.w,bad))

    def test_unconnected_native_sources_are_accessible_but_unavailable(self):
        provider=self.g.finance_method_sources
        del self.g.finance_method_sources
        try:
            r=self.report('accounting_report',self.args(self.envelope()))
            self.assertEqual(r['calculation_status'],'unavailable')
        finally:self.g.finance_method_sources=provider

    def test_native_cash_goal_uses_real_committed_confirmation_not_default_priority(self):
        provider=self.g.finance_method_sources;del self.g.finance_method_sources
        try:
            state=self.g.goals.state(self.w)
            draft=self.g.goals.prepare(self.w,dict(action='create',name='本人确认的学费留款',target_minor=100000,
                due_on=state['as_of'],priority=0,funding_kind='cash',expected_version=state['version'],
                idempotency_key='native-cash-goal'))
            receipt=self.g.goals.confirm(self.w,draft['id'],dict(confirmed=True,confirmation_digest=draft['confirmation_digest']))
            funds=self.budget();args=self.allocation_args(funds);args['constraints_ref']='goals/'+receipt['goal']['id']
            r=self.report('allocation_preview',args)
            self.assertEqual(r['plans'][1]['calculation_status'],'calculated')
            self.assertEqual(r['plans'][1]['metrics']['assigned_cash']['value_minor'],100000)
            self.assertEqual(r['plans'][1]['metrics']['investment_principal']['value_minor'],0)
            self.assertEqual(r['plans'][1]['allocations'][0]['priority'],0)
        finally:self.g.finance_method_sources=provider

    def test_native_default_goal_metadata_does_not_supply_confirmation(self):
        provider=self.g.finance_method_sources;del self.g.finance_method_sources
        try:
            funds=self.budget();args=self.allocation_args(funds);args['constraints_ref']='goals/current'
            r=self.report('allocation_preview',args)
            self.assertEqual([p['calculation_status'] for p in r['plans']],['calculated','unavailable'])
        finally:self.g.finance_method_sources=provider

    def test_cross_workspace_request_and_envelope_refused(self):
        v=self.point();self.save('accounting_report',v)
        self.reject('METRIC_SUBJECT_MISMATCH',lambda:self.s.method_report(self.w,'accounting_report',self.args(v,scope='other')),403)
        v['subject_id']='other';self.save('accounting_report',v)
        self.reject('METRIC_SUBJECT_MISMATCH',lambda:self.s.method_report(self.w,'accounting_report',self.args(v)),403)

    def test_stale_methods_batch_rejected_at_final_response_guard(self):
        v=self.point();self.save('accounting_report',v);r=self.report('accounting_report',self.args(v))
        with closing(self.g.bank.connect()) as db:
            from user_memory import privacy_revision
            from simulated_bank.agent_guard import context_hash
            privacy=privacy_revision(db,self.w);context=context_hash(db,self.w)
        personal=self.g.personal_data.revision(self.w)
        v['records']['components'][0]['amount_minor']+=1;self.save('accounting_report',v)
        def send():
            with self.g.response_guard(self.w,privacy,personal,expected_context=context,metric_batches=[r['metric_batch']]):
                self.fail('stale_response_was_published')
        self.reject('METRIC_VERSION_CONFLICT',send,409)

    def returns_fixture(self, case):
        from test_base_returns_adapter import trusted_case
        ctx,records,catalog,provenance=trusted_case(case)
        def subject(value):
            if isinstance(value,dict):return {k:self.w if k=='subject_id' else subject(v) for k,v in value.items()}
            if isinstance(value,list):return [subject(x) for x in value]
            if isinstance(value,tuple):return [subject(x) for x in value]
            return value
        catalog['portfolio_ref']='fixture-independent-research'
        return {'subject_id':self.w,'currency':ctx.currency,'data_scope':ctx.data_scope,
            'as_of':ctx.as_of,'period_start':ctx.period_start,'period_end':ctx.period_end,
            'coverage_complete':ctx.coverage_complete,'input_refs':list(ctx.input_refs),
            'source_versions':dict(ctx.source_versions),'records':subject(records),
            'catalog':subject(catalog),'provenance':provenance}

    def returns_args(self,v):
        return {'scope':self.w,'portfolio_ref':'fixture-independent-research',
            'accounting_basis':v['records']['accounting_basis'],'expected_versions':{},
            **{k:v[k] for k in ('as_of','period_start','period_end')}}

    def test_real_B_issuance_5950_and_all_consumed_input_sources(self):
        v=self.returns_fixture('B03_capitalized_buy_fee');self.save('returns_report',v)
        r=self.report('returns_report',self.returns_args(v))
        self.assertEqual(self.amounts(r)['period_pnl'],5950)
        self.assertEqual(self.amounts(r)['remaining_cost'],17680)
        for metric in r['metrics']:
            for category in ('costs','events','fees','dividends','splits','prices','cash','coverage'):
                self.assertIn(v['provenance'][category][0],metric['input_refs'])
                self.assertIn(v['provenance'][category][0],metric['source_versions'])
        observer=MetricEvidenceObserver(self.s)
        b=observer.metrics_for_tool(self.w,'returns_report',self.returns_args(v),r)
        self.assertEqual(len(observer.batches),1);self.assertIsNotNone(b)

    def test_ratio_is_separate_signed_report_not_amount_registry(self):
        v=self.returns_fixture('B08_twr_external_flow');self.save('returns_report',v)
        r=self.report('returns_report',self.returns_args(v))
        twr=next(x for x in r['ratio_reports'] if x['metric_name']=='time_weighted_return')
        self.assertEqual(twr['value_decimal'],'-0.0025');self.assertEqual(twr['unit'],'ratio')
        self.assertNotIn('value_minor',twr)
        self.assertTrue(all(m['unit']=='minor_currency' for m in r['metric_batch']['metrics']))
        self.assertIn(twr,r['metric_batch']['coverage']['ratio_reports'])
        bad=deepcopy(r);bad['ratio_reports'][0]['value_decimal']='0.99'
        self.reject('METRIC_RESULT_MISMATCH',lambda:self.s.metrics_for_tool(self.w,'returns_report',self.returns_args(v),bad))

    def test_B_missing_shares_price_boundary_and_independent_ids_do_not_fill(self):
        for case in ('B09_unknown_quantity','B10_missing_closing_price','B11_incomplete_corporate_actions','B14_twr_boundary_mismatch'):
            with self.subTest(case=case):
                v=self.returns_fixture(case);self.save('returns_report',v)
                r=self.report('returns_report',self.returns_args(v))
                if case=='B14_twr_boundary_mismatch':self.assertTrue(all(x['value_decimal'] is None for x in r['ratio_reports']))
                elif case=='B10_missing_closing_price':self.assertEqual(r['calculation_status'],'partial')
                else:self.assertEqual(r['calculation_status'],'unavailable')
        v=self.returns_fixture('B03_capitalized_buy_fee');v['records']['events'].pop();self.save('returns_report',v)
        self.assertEqual(self.report('returns_report',self.returns_args(v))['calculation_status'],'unavailable')

    def test_saved_or_self_claimed_occupancy_not_treated_as_baseline_deduction(self):
        funds=self.budget();v=self.allocation(funds,mixed=False)
        v['records']['goals'][0].update(already_occupied_minor=30000,occupancy_refs=['fixture/goals'],occupancy_verified_in_baseline=True)
        self.save('allocation_preview',v)
        r=self.report('allocation_preview',self.allocation_args(funds))
        self.assertEqual(r['plans'][1]['calculation_status'],'unavailable')


if __name__=='__main__': unittest.main()
