"""逐工具正式指标的真实隔离服务回归；无模型、无 live、临时加密 key。"""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from agent_runtime.bank_tools import BankTools
from agent_runtime.metric_claims import MetricRegistry, merge_source_versions
from finance_gateway import FinanceGateway
from financial_metrics import FinancialMetrics, fingerprint
from financial_methods.contracts import FinancialMetric
from simulated_bank import BankError


class _ToolFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory(prefix='navren-tool-metrics-')
        cls.env=patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB':str(Path(cls.folder.name)/'knowledge.sqlite3')})
        cls.env.start()
        cls.network=patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden'))
        cls.network.start()
        cls.g=FinanceGateway(Path(cls.folder.name)/'bank.sqlite3')

    @classmethod
    def tearDownClass(cls):
        cls.network.stop();cls.env.stop();cls.folder.cleanup()

    def setUp(self):
        self.w,_=self.g.create_session()
        self.service=self.g.financial_metrics
        self.tools=BankTools(self.g,self.w,self.id())
        self.aid=next(a['id'] for a in self.g.overview(self.w)['accounts'] if a['id'].endswith('-main'))

    def invoke(self,name,args):
        result=self.tools.invoke(name,args)
        self.assertTrue(self.tools.metric_batches)
        batch=self.tools.metric_batches[-1]
        self.assertEqual(batch['tool_name'],name)
        self.service.validate(self.w,batch)
        return result,batch

    def find(self,batch,name,subject=None):
        return next(m for m in batch['metrics'] if m['metric_name']==name and (subject is None or m['subject_id']==subject))

    def reject(self,code,callback):
        with self.assertRaises(BankError) as caught:callback()
        self.assertEqual(caught.exception.code,code)

    def rehash(self,batch):
        batch['snapshot_id']='base-tool-metrics-'+fingerprint({k:v for k,v in batch.items() if k!='snapshot_id'})
        return batch


class ToolMetricsTests(_ToolFixture):

    def test_account_amounts_use_account_and_workspace_distinct_subjects(self):
        result,batch=self.invoke('account_summary',{})
        accounts=result['data']['accounts'];total=sum(a['balance_minor'] for a in accounts)
        self.assertEqual(self.find(batch,'settled_cash_total',self.w)['value_minor'],total)
        self.assertEqual(self.find(batch,'settled_cash_balance',self.aid)['value_minor'],
                         next(a['balance_minor'] for a in accounts if a['id']==self.aid))
        self.assertNotEqual(self.find(batch,'settled_cash_total')['subject_id'],self.aid)

    def test_full_contract_and_fixed_kind_are_consumable(self):
        _,batch=self.invoke('account_summary',{})
        registry=MetricRegistry()
        for metric in batch['metrics']:
            restored=FinancialMetric(**{**metric,'source_versions':tuple(metric['source_versions'].items()),
                'input_refs':tuple(metric['input_refs']),'excluded_items':tuple(metric['excluded_items']),
                'coverage_gaps':tuple(metric['coverage_gaps']),'assumed_inputs':tuple(metric['assumed_inputs'])})
            self.assertEqual(restored.to_dict(),metric)
            registry.register(metric,kind=batch['metric_kinds'][metric['metric_id']],evidence_id=batch['snapshot_id'])
        merged=merge_source_versions(batch['metrics'],base_versions=batch['source_versions'])
        self.assertGreaterEqual(len(merged),len(batch['source_versions']))
        json.dumps(batch,allow_nan=False)

    def test_incoming_client_amount_is_not_signed(self):
        result,_=self.invoke('account_summary',{})
        tamper=deepcopy(result['data']);tamper['accounts'][0]['balance_minor']+=1
        self.reject('METRIC_RESULT_MISMATCH',lambda:self.service.metrics_for_tool(self.w,'account_summary',{},tamper))

    def test_incoming_client_currency_is_not_signed(self):
        result,_=self.invoke('account_summary',{})
        tamper=deepcopy(result['data']);tamper['accounts'][0]['currency']='USD'
        self.reject('METRIC_RESULT_MISMATCH',lambda:self.service.metrics_for_tool(self.w,'account_summary',{},tamper))

    def test_tampered_batch_and_recomputed_plain_hash_are_rejected(self):
        _,batch=self.invoke('account_summary',{})
        tamper=deepcopy(batch);tamper['metrics'][0]['value_minor']+=1
        self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,tamper))
        self.rehash(tamper)
        self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,tamper))

    def test_wrong_subject_and_subject_relabel_are_rejected(self):
        _,batch=self.invoke('account_summary',{})
        other,_=self.g.create_session()
        self.reject('METRIC_SUBJECT_MISMATCH',lambda:self.service.validate(other,batch))
        tamper=deepcopy(batch);tamper['metrics'][0]['subject_id']=self.w;self.rehash(tamper)
        self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,tamper))

    def test_model_cannot_change_kind(self):
        _,batch=self.invoke('money_calculation',{'operation':'sum','values_minor':[1200,3400],'currency':'CNY'})
        tamper=deepcopy(batch);mid=tamper['metrics'][0]['metric_id'];tamper['metric_kinds'][mid]='recorded_fact'
        self.rehash(tamper)
        self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,tamper))

    def test_bank_change_even_without_version_increment_invalidates_batch(self):
        _,batch=self.invoke('account_summary',{})
        with self.g.bank.transaction() as db:db.execute('UPDATE accounts SET balance_minor=balance_minor+1 WHERE id=?',(self.aid,))
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_customer_record_change_without_version_increment_invalidates_batch(self):
        _,batch=self.invoke('customer_records',{'domain':'loans'})
        with self.g.bank.transaction() as db:db.execute('UPDATE loans SET outstanding_minor=outstanding_minor+1 WHERE workspace_id=?',(self.w,))
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_personal_revision_invalidates_batch(self):
        _,batch=self.invoke('account_summary',{})
        self.g.memories.create(self.w,{'category':'goal','value':'测试储蓄目标','expected_revision':self.g.personal_data.revision(self.w)})
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_methods_code_drift_fails_closed(self):
        _,batch=self.invoke('account_summary',{})
        with patch('financial_metrics.methods_fingerprint',return_value='changed'):
            self.reject('METHODS_RESTART_REQUIRED',lambda:self.service.validate(self.w,batch))

    def test_tool_api_code_drift_fails_closed(self):
        _,batch=self.invoke('account_summary',{})
        with patch('financial_metrics.LOADED_TOOL_METRICS_CODE','changed'):
            self.reject('TOOL_METRICS_RESTART_REQUIRED',lambda:self.service.validate(self.w,batch))

    def test_service_restart_rejects_prior_tool_batches(self):
        _,batch=self.invoke('account_summary',{})
        restarted=FinancialMetrics(self.g)
        self.reject('METRIC_TAMPERED',lambda:restarted.validate(self.w,batch))
        # 恢复正式运行实例的现有操作 guard；测试不泄漏到其它用例。
        self.g.bank.base_metrics_guard=self.service.execution_guard
        self.g.bank.base_metrics_register=self.service.register_required

    def test_validate_inside_existing_write_transaction_is_read_only(self):
        _,batch=self.invoke('cashflow',{'days':30})
        with self.g.bank.transaction() as db:
            with (patch.object(self.g.bank,'transaction',side_effect=AssertionError('nested_write_forbidden')),
                  patch.object(self.g,'overview',side_effect=AssertionError('overview_forbidden')),
                  patch.object(self.g,'cashflow_timeline',side_effect=AssertionError('cashflow_recompute_forbidden'))):
                self.service.validate(self.w,batch,db)

    def test_budget_is_30_day_account_not_60_day_workspace(self):
        result,batch=self.invoke('budget',{'account_id':self.aid})
        metric=self.find(batch,'arrangeable_now')
        self.assertEqual(metric['subject_id'],self.aid)
        self.assertEqual(metric['period_end'],self.g.overview(self.w)['commitment_window_end'])
        self.assertEqual(metric['value_minor'],result['data']['investable_minor'])
        self.assertEqual(batch['metric_kinds'][metric['metric_id']],'recorded_fact')

    def test_budget_higher_user_floor_is_planning_not_recorded_profile(self):
        floor=self.g.budget(self.w,self.aid)['reserve_minor']+1000
        _,batch=self.invoke('budget',{'account_id':self.aid,'floor_minor':floor})
        metric=self.find(batch,'planning_arrangeable_now')
        self.assertEqual(batch['metric_kinds'][metric['metric_id']],'planning_scenario')
        self.assertEqual(metric['assumed_inputs'],[{'type':'user_requested_emergency_floor','amount_minor':floor}])
        setting=self.find(batch,'reserve')
        self.assertEqual(batch['metric_kinds'][setting['metric_id']],'planning_scenario')
        self.assertIn('user_requested',setting['basis'])
        baseline=self.find(batch,'baseline_investable')
        self.assertEqual(batch['metric_kinds'][baseline['metric_id']],'planning_scenario')
        self.assertIn('planning_floor',baseline['basis'])
        self.assertEqual(baseline['assumed_inputs'],metric['assumed_inputs'])
        self.assertEqual(batch['metric_kinds'][self.find(batch,'cash')['metric_id']],'recorded_fact')
        self.assertEqual(batch['metric_kinds'][self.find(batch,'upcoming_due')['metric_id']],'recorded_fact')
        self.assertNotEqual(self.g.overview(self.w)['profile']['reserve_minor'],floor)

    def test_budget_reserves_explicit_ids_deduplicate_known_obligation(self):
        baseline,_=self.invoke('budget',{'account_id':self.aid})
        commitment=next(c for c in baseline['data']['commitments'] if c['account_id']==self.aid)
        reserve={'label':'已知还款再留一点','amount_minor':commitment['amount_minor']+1700,'commitment_ids':[commitment['evidence_id']]}
        result,batch=self.invoke('budget',{'account_id':self.aid,'reserves':[reserve]})
        self.assertEqual(result['data']['additional_reserve_minor'],1700)
        self.assertEqual(self.find(batch,'planning_arrangeable_now')['value_minor'],max(0,baseline['data']['investable_minor']-1700))
        metric=self.find(batch,'planning_arrangeable_now')
        self.assertEqual(batch['metric_kinds'][metric['metric_id']],'planning_scenario')
        self.assertEqual(metric['assumed_inputs'][0]['already_budgeted_minor'],commitment['amount_minor'])
        self.assertFalse(batch['coverage']['real_reservation_created'])

    def test_new_reserve_without_link_deducts_full_amount(self):
        baseline,_=self.invoke('budget',{'account_id':self.aid})
        result,batch=self.invoke('budget',{'account_id':self.aid,'reserves':[{'label':'全新支出','amount_minor':2500,'commitment_ids':[]}]})
        self.assertEqual(self.find(batch,'additional_planning_reserve')['value_minor'],2500)
        self.assertEqual(result['data']['investable_minor'],max(0,baseline['data']['investable_minor']-2500))

    def test_reserve_link_requires_prior_formal_baseline(self):
        raw=self.g.budget(self.w,self.aid)
        raw.update(account_id=self.aid,currency='CNY',name='unused')
        self.reject('BUDGET_BASELINE_REQUIRED',lambda:self.service.metrics_for_tool(self.w,'budget',
            {'account_id':self.aid,'reserves':[{'label':'测试','amount_minor':100,'commitment_ids':[]}]},raw))

    def test_repeated_or_foreign_commitment_ids_never_credit_twice(self):
        baseline,_=self.invoke('budget',{'account_id':self.aid})
        cid=next(c['evidence_id'] for c in baseline['data']['commitments'] if c['account_id']==self.aid)
        for links in ([cid,cid],['invented-id']):
            args={'account_id':self.aid,'reserves':[{'label':'测试','amount_minor':1000,'commitment_ids':links}]}
            self.reject('INVALID_RESERVE_LINK',lambda:self.service.metrics_for_tool(self.w,'budget',args,{}))
        reserve={'label':'测试','amount_minor':1000,'commitment_ids':[cid]}
        self.reject('INVALID_RESERVE_LINK',lambda:self.service.metrics_for_tool(self.w,'budget',
            {'account_id':self.aid,'reserves':[reserve,reserve]},{}))

    def test_changed_budget_observation_cannot_issue_reserve_batch(self):
        self.invoke('budget',{'account_id':self.aid})
        with self.g.bank.transaction() as db:db.execute('UPDATE loans SET outstanding_minor=outstanding_minor+1 WHERE workspace_id=?',(self.w,))
        self.reject('BUDGET_BASELINE_REQUIRED',lambda:self.service.metrics_for_tool(self.w,'budget',
            {'account_id':self.aid,'reserves':[{'label':'测试','amount_minor':100,'commitment_ids':[]}]},{}))

    def test_cashflow_requested_period_and_account_branches(self):
        result,batch=self.invoke('cashflow',{'days':7})
        metric=self.find(batch,'minimum_available_after_confirmed',self.aid)
        row=next(a for a in result['data']['accounts'] if a['account_id']==self.aid)
        self.assertEqual(metric['value_minor'],row['minimum_available_after_confirmed_minor'])
        self.assertEqual(metric['period_end'],result['data']['horizon_end'])
        self.assertEqual(batch['metric_kinds'][metric['metric_id']],'recorded_projection')
        candidate=self.find(batch,'minimum_available_if_candidates_occur',self.aid)
        self.assertEqual(batch['metric_kinds'][candidate['metric_id']],'candidate_scenario')

    def test_cashflow_period_tamper_and_reusing_another_period_result_reject(self):
        result,batch=self.invoke('cashflow',{'days':7})
        tamper=deepcopy(batch);tamper['metrics'][0]['as_of']='2026-01-01';self.rehash(tamper)
        self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,tamper))
        self.reject('METRIC_RESULT_MISMATCH',lambda:self.service.metrics_for_tool(self.w,'cashflow',{'days':60},result['data']))

    def test_empty_month_is_unavailable_not_zero(self):
        _,batch=self.invoke('monthly_statement',{'month':'2040-05'})
        self.assertTrue(batch['coverage']['no_records'])
        self.assertTrue(batch['metrics'])
        self.assertTrue(all(m['value_minor'] is None and m['calculation_status']=='unavailable' for m in batch['metrics']))

    def test_partial_annual_report_marks_missing_months(self):
        _,batch=self.invoke('annual_statement',{'year':2026})
        self.assertFalse(batch['coverage']['complete_period'])
        self.assertTrue(any(g.startswith('uncovered_month:') for g in batch['coverage']['coverage_gaps']))
        self.assertTrue(all(m['calculation_status']=='partial' for m in batch['metrics']))
        with closing(self.g.bank.connect()) as db:
            last=db.execute("SELECT max(occurred_at) FROM transactions WHERE workspace_id=? AND substr(occurred_at,1,4)='2026'",(self.w,)).fetchone()[0]
        self.assertGreaterEqual(batch['coverage']['period_end'],last[:10])

    def test_imported_statement_is_partial_flow_and_not_bank_cash(self):
        csv='流水号,日期,商户,金额,类型,分类,币种,备注\n'
        csv+='isolated-income,2026-09-01,工资,100.01,收入,,CNY,\n'
        csv+='isolated-expense,2026-09-02,咖啡店,20.10,支出,餐饮,CNY,\n'
        preview=self.g.statements.preview(self.w,csv)
        self.g.statements.commit(self.w,{'batch_id':preview['batch_id'],'confirmed':True})
        self.g.set_source(self.w,{'source':'imported'})
        self.tools=BankTools(self.g,self.w,'fresh-imported-report')
        _,batch=self.invoke('monthly_statement',{'month':'2026-09'})
        income=self.find(batch,'income')
        self.assertEqual(income['value_minor'],10001)
        self.assertEqual(income['data_scope'],'imported_statement_records')
        self.assertEqual(income['calculation_status'],'partial')
        self.assertEqual((income['period_start'],income['period_end']),('2026-09-01','2026-09-02'))
        delta=self.find(batch,'cash_delta')
        self.assertIsNone(delta['value_minor']);self.assertEqual(delta['calculation_status'],'unavailable')
        with self.g.bank.transaction() as db:
            db.execute("UPDATE imported_statement_transactions SET amount_minor=amount_minor+1 WHERE workspace_id=?",(self.w,))
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch,db))

    def import_report_fixture(self):
        csv='流水号,日期,商户,金额,类型,分类,币种,备注\n'
        csv+='bound-income,2026-09-01,工资,100.01,收入,,CNY,\n'
        preview=self.g.statements.preview(self.w,csv)
        self.g.statements.commit(self.w,{'batch_id':preview['batch_id'],'confirmed':True})
        return self.g.source(self.w)['sources']['imported']['source_version']

    def test_bound_gateway_keeps_key_lock_and_bank_guards(self):
        scoped=self.g.bound_report_source(self.w,'simulation',self.service.versions(self.w)['bank'])
        self.assertIs(scoped.financial_metrics._tool_key,self.service._tool_key)
        self.assertIs(scoped.financial_metrics._tool_lock,self.service._tool_lock)
        self.assertIs(scoped.financial_metrics._budget_observations,self.service._budget_observations)
        self.assertIs(self.g.bank.base_metrics_guard.__self__,self.service)
        other=FinanceGateway(Path(self.folder.name)/'unrelated-bank.sqlite3')
        self.reject('METRIC_SUBJECT_MISMATCH',lambda:self.service.bound_gateway(other))

    def test_bound_imported_report_succeeds_while_active_is_simulation(self):
        version=self.import_report_fixture()
        self.assertEqual(self.g.source(self.w)['active_source'],'simulation')
        scoped=self.g.bound_report_source(self.w,'imported',version)
        tools=BankTools(scoped,self.w,'bound-imported-page')
        result=tools.invoke('monthly_statement',{'month':'2026-09'});batch=tools.metric_batches[-1]
        metric=self.find(batch,'income')
        self.assertEqual(metric['value_minor'],10001)
        self.assertEqual(metric['data_scope'],'imported_statement_records')
        self.assertEqual(batch['binding']['report_source'],{'workspace_id':self.w,'source':'imported','source_version':version})
        self.assertEqual(result['data']['source'],'imported_local_csv')
        with self.g.bank.transaction() as db:self.service.validate(self.w,batch,db)

    def test_bound_simulation_report_succeeds_while_active_is_imported(self):
        self.import_report_fixture();self.g.set_source(self.w,{'source':'imported'})
        version=self.g.source(self.w)['sources']['simulation']['source_version']
        scoped=self.g.bound_report_source(self.w,'simulation',version)
        tools=BankTools(scoped,self.w,'bound-simulation-page')
        result=tools.invoke('monthly_statement',{'month':'2026-09'});batch=tools.metric_batches[-1]
        self.assertEqual(result['data']['source'],'simulation')
        self.assertNotEqual(self.find(batch,'income')['value_minor'],10001)
        self.assertEqual(batch['binding']['report_source']['source'],'simulation')
        self.service.validate(self.w,batch)

    def test_bound_report_rejects_source_forgery_and_changed_chosen_version(self):
        version=self.import_report_fixture()
        scoped=self.g.bound_report_source(self.w,'imported',version)
        data=scoped.analysis(self.w,'2026-09')
        batch=scoped.financial_metrics.metrics_for_tool(self.w,'monthly_statement',{'month':'2026-09'},data)
        forged=deepcopy(data);forged['source']='simulation'
        self.reject('METRIC_RESULT_MISMATCH',lambda:scoped.financial_metrics.metrics_for_tool(self.w,'monthly_statement',{'month':'2026-09'},forged))
        tamper=deepcopy(batch);tamper['binding']['report_source']['source']='simulation';self.rehash(tamper)
        self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,tamper))
        with self.g.bank.transaction() as db:
            db.execute('UPDATE statement_datasets SET version=version+1 WHERE workspace_id=?',(self.w,))
            self.reject('SOURCE_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch,db))

    def test_customer_record_origination_current_debt_and_historical_paid_interest_are_separate(self):
        _,loans=self.invoke('customer_records',{'domain':'loans'})
        original=self.find(loans,'original_principal');current=self.find(loans,'outstanding')
        self.assertIn('historical_origination',original['basis'])
        self.assertNotEqual(original['as_of'],current['as_of'])
        self.assertNotEqual(current['subject_id'],self.aid)
        _,deposits=self.invoke('customer_records',{'domain':'deposits'})
        interest=self.find(deposits,'interest_paid')
        self.assertIsNone(interest['as_of']);self.assertIn('historically_recorded',interest['basis'])

    def test_missing_insurance_returns_explicit_gap_without_fake_zero(self):
        _,batch=self.invoke('customer_records',{'domain':'insurance'})
        self.assertEqual(batch['metrics'],[])
        self.assertEqual(batch['coverage']['unavailable_entities'],['insurance_policies','insurance_claims'])

    def test_money_arithmetic_only_hypothesis_with_raw_inputs(self):
        for operation,args,expected in (
            ('sum',{'values_minor':[300,40]},340),
            ('subtract',{'values_minor':[300,40,10]},250),
            ('multiply',{'values_minor':[300],'factor':4},1200)):
            _,batch=self.invoke('money_calculation',{'operation':operation,'currency':'USD',**args})
            metric=batch['metrics'][0]
            self.assertEqual(metric['value_minor'],expected);self.assertEqual(metric['currency'],'USD')
            self.assertEqual(metric['data_scope'],'user_input_scenario')
            self.assertEqual(batch['metric_kinds'][metric['metric_id']],'hypothesis')
            self.assertTrue(metric['assumed_inputs'])

    def test_untrusted_hypothesis_result_is_recomputed_not_signed(self):
        args={'operation':'sum','values_minor':[10,20],'currency':'CNY'}
        self.reject('METRIC_RESULT_MISMATCH',lambda:self.service.metrics_for_tool(self.w,'money_calculation',args,
            {'currency':'CNY','result_minor':31,'source':'user_input_scenario'}))

    def test_prepare_metric_reads_real_pending_operation_and_projection(self):
        self.invoke('account_summary',{})
        args={'action':'purchase','account_id':self.aid,'product_id':'SIM-CASH-01','amount_minor':10000}
        view=self.g.prepare(self.w,{**args,'idempotency_key':'tool-metrics-prepared'})
        # 正式来源重新构造同一草案的条件投影，不执行。
        view['projected_after_confirmation']={'currency':'CNY','balance_minor':view['account']['balance_minor']-10000,
            'investable_minor':max(0,view['budget']['investable_minor']-10000)}
        batch=self.service.metrics_for_tool(self.w,'prepare_operation',args,view)
        self.service.validate(self.w,batch)
        metric=self.find(batch,'pending_operation_amount')
        self.assertEqual(metric['subject_id'],view['operation']['id'])
        self.assertEqual(batch['metric_kinds'][metric['metric_id']],'planning_scenario')
        self.assertEqual(self.g.bank.operation(self.w,view['operation']['id'])['state'],'pending_confirmation')
        with self.g.bank.transaction() as db:
            self.g.bank._audit(db,self.w,'unrelated_response_evidence',{},view['operation']['id'])
            self.service.validate(self.w,batch,db)

    def test_prepare_wrong_action_amount_or_foreign_operation_is_rejected(self):
        args={'action':'purchase','account_id':self.aid,'product_id':'SIM-CASH-01','amount_minor':10000}
        view=self.g.prepare(self.w,{**args,'idempotency_key':'tool-metrics-wrong'})
        self.reject('METRIC_OPERATION_MISMATCH',lambda:self.service.metrics_for_tool(self.w,'prepare_operation',{**args,'amount_minor':10001},view))
        other,_=self.g.create_session()
        self.reject('NOT_FOUND',lambda:self.service.metrics_for_tool(other,'prepare_operation',args,view))

    def test_pending_operation_expiry_and_cancellation_fail_closed(self):
        args={'action':'purchase','account_id':self.aid,'product_id':'SIM-CASH-01','amount_minor':10000}
        view=self.g.prepare(self.w,{**args,'idempotency_key':'tool-metrics-expiry'})
        view['projected_after_confirmation']={'currency':'CNY','balance_minor':view['account']['balance_minor']-10000,
            'investable_minor':max(0,view['budget']['investable_minor']-10000)}
        batch=self.service.metrics_for_tool(self.w,'prepare_operation',args,view)
        with patch.object(self.g.bank,'clock',return_value=view['operation']['expires_at']):
            self.reject('STALE_OPERATION_METRICS',lambda:self.service.validate(self.w,batch))
        with self.g.bank.transaction() as db:
            db.execute("UPDATE operations SET state='cancelled' WHERE id=?",(view['operation']['id'],))
            self.reject('STALE_OPERATION_METRICS',lambda:self.service.validate(self.w,batch,db))

    def test_service_rule_fees_are_terms_and_never_cash(self):
        result,batch=self.invoke('service_offer_rules',{})
        self.assertTrue(batch['metrics'])
        for metric in batch['metrics']:
            self.assertEqual(metric['data_scope'],'navren_synthetic_service_terms_not_cash')
            self.assertIn('not_cash',metric['basis'])
            self.assertIn('not_awarded_benefit',metric['excluded_items'])
        rule=result['data']['offers'][0]
        self.assertEqual(self.find(batch,'max_cost',rule['offer_id'])['value_minor'],rule['max_cost_minor'])

    def test_service_preview_reloads_exact_draft_and_expiry(self):
        self.tools.user_text='我希望了解电子账单的服务费用减免。'
        result,batch=self.invoke('service_offer_preview',{})
        draft=result['data']['draft_id']
        self.assertEqual(batch['binding']['draft_id'],draft)
        with self.g.bank.transaction() as db:
            self.service.validate(self.w,batch,db)
            expiry=db.execute('SELECT expires_at FROM service_negotiation_drafts WHERE id=?',(draft,)).fetchone()[0]
        with patch.object(self.g.bank,'clock',return_value=expiry):
            self.reject('STALE_SERVICE_EVIDENCE',lambda:self.service.validate(self.w,batch))
        tamper=deepcopy(result['data']);tamper['offers'][0]['max_cost_minor']+=1
        self.reject('METRIC_RESULT_MISMATCH',lambda:self.service.metrics_for_tool(self.w,'service_offer_preview',{},tamper))

    def test_service_application_status_keeps_pending_terms(self):
        self.tools.user_text='我希望了解电子账单的服务费用减免。'
        result,_=self.invoke('service_offer_preview',{})
        preview=result['data']
        self.g.service_negotiations.accept(self.w,preview['draft_id'],
            {'offer_id':preview['offers'][0]['offer_id'],'confirmed':True})
        # 接受申请改变预览状态：新一轮读取回执，不复用旧预览证据。
        self.tools=BankTools(self.g,self.w,'fresh-application-status')
        result,batch=self.invoke('service_application_status',{})
        application=result['data']['applications'][0]
        metric=self.find(batch,'max_cost',application['application_id'])
        self.assertEqual(metric['assumed_inputs'][0]['status'],'submitted_simulation_pending_eligibility')
        self.assertIn('not_awarded_benefit',metric['excluded_items'])

    def test_service_rule_change_invalidates_already_issued_batch(self):
        _,batch=self.invoke('service_offer_rules',{})
        rules=deepcopy(self.g.service_negotiations.rules());rules['offers'][0]['monthly_value_minor']+=1
        with patch.object(self.g.service_negotiations,'rules',return_value=rules):
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_old_funding_snapshot_identity_and_operation_guard_unchanged(self):
        before=self.service.snapshot(self.w,60)
        self.invoke('account_summary',{})
        after=self.service.snapshot(self.w,60)
        self.assertEqual(before,after)
        self.assertEqual(before['snapshot_id'],'base-metrics-'+fingerprint({k:v for k,v in before.items() if k!='snapshot_id'}))
        self.service.validate(self.w,before)

    def test_unknown_tool_returns_none(self):
        self.assertIsNone(self.service.metrics_for_tool(self.w,'unimplemented_tool',{},{}))


class SyntheticHistoryToolMetricsTests(_ToolFixture):
    """只选新建临时库中的离线演示账号，源历史文件只读。"""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.g.demo_accounts.initialize_offline(allow_offline=True)

    def setUp(self):
        self.w=self.g.demo_accounts.workspace_for('tycoon')
        self.service=self.g.financial_metrics
        self.tools=BankTools(self.g,self.w,self.id())
        self.aid=next(a['id'] for a in self.g.overview(self.w)['accounts'] if a['id'].endswith('-main'))

    def test_synthetic_partial_first_year_retains_true_coverage(self):
        # Agent schema 的旧下限仍是2025；这里只验基座支持的真实2016覆盖。
        data=self.g.annual_analysis(self.w,2016)
        batch=self.service.metrics_for_tool(self.w,'annual_statement',{'year':2016},data)
        self.service.validate(self.w,batch)
        self.assertEqual(batch['coverage']['source_coverage_start'],'2016-10-04')
        self.assertFalse(batch['coverage']['complete_period'])
        metric=self.find(batch,'income')
        self.assertEqual(metric['data_scope'],'navren_synthetic')
        self.assertEqual(metric['period_start'],'2016-10-04')
        self.assertEqual(metric['value_minor'],data['totals_minor']['income'])
        self.assertEqual(metric['calculation_status'],'partial')

    def test_synthetic_last_month_is_partial_and_not_current_cash(self):
        _,batch=self.invoke('monthly_statement',{'month':'2026-10'})
        metric=self.find(batch,'income_expense_surplus')
        self.assertEqual(metric['period_end'],'2026-10-03')
        self.assertEqual(metric['data_scope'],'navren_synthetic')
        self.assertIn('not_current_cash',metric['basis'])
        self.assertEqual(metric['calculation_status'],'partial')


if __name__=='__main__':unittest.main()
