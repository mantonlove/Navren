"""CF60 到准备操作的真实隔离服务增量回归；不调用模型/live。"""
from contextlib import closing
from copy import copy, deepcopy
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet

from agent_runtime.bank_tools import BankTools
from base_runtime.evidence import MetricEvidenceObserver
from finance_gateway import FinanceGateway
from financial_metrics import fingerprint
from simulated_bank import BankError
from simulated_bank.agent_guard import bind_review, context_hash


class _RequestObserver(MetricEvidenceObserver):
    """只替代未接线的一个转调入口；发行/银行/gateway/Agent 都使用正式实现。"""
    def prepare_bank_operation(self, workspace, payload, *, agent_origin=False, metric_required=False):
        return self.service.prepare_bank_operation(workspace,payload,agent_origin=agent_origin,
                                                   metric_required=metric_required,metric_batches=self.batches)


class _PrepareDispatch:
    """把真实 gateway 原 bank.prepare 调度到拟接线入口，不重写准备业务。"""
    def __init__(self, bank, observer):self.bank,self.observer=bank,observer
    def __getattr__(self, name):return getattr(self.bank,name)
    def prepare(self, *args, **kwargs):return self.observer.prepare_bank_operation(*args,**kwargs)


class FinancialTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory(prefix='navren-cf-transition-')
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
        credentials=self.g.bank.create_workspace();self.w=credentials['workspace_id']
        self.pin=credentials['human_verification_code']
        self.service=self.g.financial_metrics
        self.view=copy(self.g)
        self.observer=_RequestObserver(self.service.bound_gateway(self.view))
        self.view.financial_metrics=self.observer
        self.view.bank=_PrepareDispatch(self.g.bank,self.observer)
        self.tools=BankTools(self.view,self.w,uuid.uuid4().hex)
        self.aid=next(a['id'] for a in self.g.overview(self.w)['accounts'] if a['id'].endswith('-main'))
        self.args={'action':'purchase','account_id':self.aid,'product_id':'SIM-CASH-01','amount_minor':10000}

    def reject(self, code, callback):
        with self.assertRaises(BankError) as caught:callback()
        self.assertEqual(caught.exception.code,code)

    def cashflow(self):
        result=self.tools.invoke('cashflow',{'days':60})
        return result,self.tools.metric_batches[-1]

    def external(self, *, agent=False):
        return self.g.prepare(self.w,{**self.args,'idempotency_key':'external-'+uuid.uuid4().hex},agent_origin=agent)

    def prepared(self):
        self.cashflow();self.tools.invoke('products',{})
        return self.tools.invoke('prepare_operation',self.args)

    def guard(self):
        return self.view.response_guard(self.w,self.tools.privacy_revision,self.g.personal_data.revision(self.w),
            expected_context=self.tools.start_context,metric_batches=self.observer.batches)

    def test_cf60_prepare_fresh_same_signed_batch_and_final_amount_contract(self):
        initial,batch=self.cashflow();original=deepcopy(batch)
        self.tools.invoke('products',{})
        result=self.tools.invoke('prepare_operation',self.args)
        self.assertEqual(result['data']['operation']['state'],'pending_confirmation')
        self.assertEqual(batch,original)
        self.tools.fresh()
        self.tools.amount_contract({'text':'草案已准备，等待本人确认。','claims':[],'amount_claims':[]})
        with self.guard() as db:
            for issued in self.observer.batches:self.service.validate(self.w,issued,db)
        current=self.g.cashflow_timeline(self.w,60)
        for account in initial['data']['accounts']:
            after=next(a for a in current['accounts'] if a['account_id']==account['account_id'])
            for field,value in account.items():
                if field.endswith('_minor'):self.assertEqual(value,after[field])
        self.assertEqual(len(current['not_included']['unconfirmed_operations']),1)
        cf_key='cashflow_operations/'+self.w;op_key='operation_business/'+self.w
        self.assertIn(cf_key,batch['source_versions'])
        self.assertNotIn(op_key,batch['source_versions'])
        self.assertIn(op_key,self.tools.metric_batches[-1]['source_versions'])

    def test_other_request_adds_pending_after_cf_without_preparing_is_rejected(self):
        _,batch=self.cashflow();self.external()
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_other_request_prepare_cannot_authorize_this_requests_cf(self):
        _,batch=self.cashflow()
        outsider=copy(self.view);outsider.financial_metrics=_RequestObserver(self.service.bound_gateway(self.g))
        outsider.bank=_PrepareDispatch(self.g.bank,outsider.financial_metrics)
        outsider.prepare(self.w,{**self.args,'idempotency_key':'other-request'})
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_identical_cf_reads_in_two_requests_have_distinct_transition_authority(self):
        _,batch=self.cashflow()
        outsider=copy(self.g);observer=_RequestObserver(self.service.bound_gateway(outsider))
        outsider.financial_metrics=observer;outsider.bank=_PrepareDispatch(self.g.bank,observer)
        tools=BankTools(outsider,self.w,'same-workspace-second-request')
        tools.invoke('cashflow',{'days':60});other=tools.metric_batches[0]
        self.assertEqual(batch['metrics'],other['metrics'])
        self.assertNotEqual(batch['snapshot_id'],other['snapshot_id'])
        tools.invoke('products',{});tools.invoke('prepare_operation',self.args)
        tools.fresh()
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_stale_cf_cannot_be_laundered_by_own_prepare(self):
        _,batch=self.cashflow();self.external()
        before=self.g.bank.customer_archive(self.w)['records']['operations']
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
            self.w,{**self.args,'idempotency_key':'no-laundering'},agent_origin=True,metric_required=True))
        self.assertEqual(before,self.g.bank.customer_archive(self.w)['records']['operations'])

    def test_external_insert_during_own_prepare_is_rejected(self):
        self.cashflow();original=self.g.bank.prepare
        def raced(workspace,payload,**kwargs):
            original(workspace,{**self.args,'idempotency_key':'race-external'},agent_origin=False)
            return original(workspace,payload,**kwargs)
        with patch.object(self.g.bank,'prepare',side_effect=raced):
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'race-own'},agent_origin=True,metric_required=True))

    def test_external_insert_between_validation_and_capture_is_not_laundered(self):
        self.cashflow();original=self.observer.service.validate;changed=False
        def raced(workspace,batch,db=None):
            nonlocal changed
            value=original(workspace,batch,db)
            if not changed:
                changed=True;self.external()
            return value
        with patch.object(self.observer.service,'validate',side_effect=raced):
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'coherent-read'},agent_origin=True,metric_required=True))

    def test_cf_transition_requires_protected_bank_insert(self):
        self.cashflow()
        with patch.object(self.g.bank,'prepare',side_effect=AssertionError('prepare_forbidden')):
            self.reject('METRIC_BINDING_REQUIRED',lambda:self.observer.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'unprotected'},agent_origin=True))

    def test_external_old_business_row_change_during_prepare_is_rejected(self):
        old=self.external()['operation']['id'];self.cashflow();original=self.g.bank.prepare
        def raced(workspace,payload,**kwargs):
            with self.g.bank.transaction() as db:
                db.execute("UPDATE operations SET permission='yellow' WHERE id=?",(old,))
            return original(workspace,payload,**kwargs)
        with patch.object(self.g.bank,'prepare',side_effect=raced):
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'race-old'},agent_origin=True,metric_required=True))

    def test_actual_new_operation_cannot_launder_changed_business_fields(self):
        self.cashflow();original=self.g.bank.prepare
        for field,value in (('permission','yellow'),('created_at',0),('payload_hash','tampered')):
            with self.subTest(field=field):
                def raced(workspace,payload,**kwargs):
                    op=original(workspace,payload,**kwargs)
                    with self.g.bank.transaction() as db:
                        db.execute('UPDATE operations SET '+field+'=? WHERE id=?',(value,op['id']))
                    return op
                with patch.object(self.g.bank,'prepare',side_effect=raced):
                    self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
                        self.w,{**self.args,'idempotency_key':'new-tamper-'+field},agent_origin=True,metric_required=True))
                # 每子例独立工作区，避免已拒绝的准备影响下一条原始预期。
                self.setUp();self.cashflow()

    def test_exact_actual_agent_supersede_is_allowed(self):
        old=self.external(agent=True)['operation']['id'];self.prepared()
        self.assertEqual(self.g.bank.operation(self.w,old)['state'],'invalidated')
        self.tools.fresh()
        with self.guard():pass

    def test_forged_supersede_without_actual_prepared_audit_is_rejected(self):
        old=self.external(agent=True)['operation']['id'];self.cashflow();original=self.g.bank.prepare
        def raced(workspace,payload,**kwargs):
            with self.g.bank.transaction() as db:
                db.execute("UPDATE operations SET state='invalidated' WHERE id=?",(old,))
                db.execute("UPDATE agent_draft_reviews SET status='superseded' WHERE operation_id=?",(old,))
            return original(workspace,payload,**kwargs)
        with patch.object(self.g.bank,'prepare',side_effect=raced):
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'fake-supersede'},agent_origin=True,metric_required=True))

    def test_external_operation_change_after_authorized_transition_is_rejected(self):
        result=self.prepared();oid=result['data']['operation']['id']
        with self.g.bank.transaction() as db:db.execute("UPDATE operations SET state='cancelled' WHERE id=?",(oid,))
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,self.tools.metric_batches[0]))

    def test_new_external_pending_after_authorized_transition_is_rejected(self):
        self.prepared();self.external()
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,self.tools.metric_batches[0]))

    def test_ledger_change_even_without_bank_version_is_rejected(self):
        self.prepared()
        with self.g.bank.transaction() as db:db.execute('UPDATE accounts SET balance_minor=balance_minor+1 WHERE id=?',(self.aid,))
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,self.tools.metric_batches[0]))

    def test_personal_change_during_prepare_is_rejected(self):
        self.cashflow();original=self.g.bank.prepare
        def raced(workspace,payload,**kwargs):
            result=original(workspace,payload,**kwargs)
            self.g.memories.create(self.w,{'category':'goal','value':'独立版本测试目标',
                'expected_revision':self.g.personal_data.revision(self.w)})
            return result
        with patch.object(self.g.bank,'prepare',side_effect=raced):
            self.reject('METRIC_VERSION_CONFLICT',lambda:self.observer.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'race-personal'},agent_origin=True,metric_required=True))

    def test_tampered_transition_batch_is_rejected_before_any_prepare(self):
        _,batch=self.cashflow();bad=deepcopy(batch);bad['metrics'][0]['value_minor']+=1
        bad['snapshot_id']='base-tool-metrics-'+fingerprint({k:v for k,v in bad.items() if k!='snapshot_id'})
        with patch.object(self.g.bank,'prepare',side_effect=AssertionError('prepare_forbidden')):
            self.reject('METRIC_TAMPERED',lambda:self.service.prepare_bank_operation(
                self.w,{**self.args,'idempotency_key':'tampered'},metric_batches=[bad],metric_required=True))

    def test_validate_existing_bank_transaction_never_prepares_or_opens_write(self):
        self.prepared()
        with self.g.bank.transaction() as db:
            with (patch.object(self.g.bank,'transaction',side_effect=AssertionError('nested_write_forbidden')),
                  patch.object(self.g.bank,'prepare',side_effect=AssertionError('prepare_forbidden'))):
                for batch in self.tools.metric_batches:self.service.validate(self.w,batch,db)

    def approval(self, oid):
        with closing(self.g.bank.connect()) as db:context=context_hash(db,self.w)
        bind_review(self.g.bank,self.w,oid,expected_context=context,evidence_hash='isolated-approval',
                    reviewer_model='isolated-no-model',review_id=uuid.uuid4().hex)
        with self.guard() as db:
            self.g.operation_evidence.bind_answer(self.w,{'compliance_pass':True,
                'bank_actions':[{'operation_id':oid}],'financial_metric_batches':self.observer.batches},db)

    def test_legitimate_bank_verification_does_not_stale_business_metrics(self):
        result=self.prepared();op=self.g.bank.operation(self.w,result['data']['operation']['id']);self.approval(op['id'])
        # create_workspace 的隔离模拟口令，不读取真实用户凭据。
        verification=self.g.bank.verify(self.w,op['id'],{'verification_code':self.pin})
        self.tools.fresh()
        with self.guard():pass
        receipt=self.g.bank.confirm(self.w,op['id'],{'confirmed':True,
            'confirmation_digest':op['confirmation_digest'],'verification_token':verification['verification_token']})
        self.assertEqual(receipt['state'],'succeeded')
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM transactions WHERE operation_id=?',(op['id'],)).fetchone()[0],1)

    def test_expired_verification_token_still_rejected_without_funds_change(self):
        result=self.prepared();op=self.g.bank.operation(self.w,result['data']['operation']['id']);self.approval(op['id'])
        verification=self.g.bank.verify(self.w,op['id'],{'verification_code':self.pin})
        before=self.g.overview(self.w)['total_cash_minor']
        with patch.object(self.g.bank,'clock',return_value=verification['expires_at']):
            self.reject('VERIFICATION_REQUIRED',lambda:self.g.bank.confirm(self.w,op['id'],{'confirmed':True,
                'confirmation_digest':op['confirmation_digest'],'verification_token':verification['verification_token']}))
        self.assertEqual(before,self.g.overview(self.w)['total_cash_minor'])

    def test_replaced_verification_token_still_rejected(self):
        result=self.prepared();op=self.g.bank.operation(self.w,result['data']['operation']['id']);self.approval(op['id'])
        old=self.g.bank.verify(self.w,op['id'],{'verification_code':self.pin})
        self.g.bank.verify(self.w,op['id'],{'verification_code':self.pin})
        self.reject('VERIFICATION_REQUIRED',lambda:self.g.bank.confirm(self.w,op['id'],{'confirmed':True,
            'confirmation_digest':op['confirmation_digest'],'verification_token':old['verification_token']}))

    def test_wrong_bank_verification_code_still_rejected_without_funds_change(self):
        result=self.prepared();oid=result['data']['operation']['id'];self.approval(oid)
        before=self.g.overview(self.w)['total_cash_minor']
        wrong=f'{(int(self.pin)+1)%1000000:06d}'
        self.reject('VERIFICATION_FAILED',lambda:self.g.bank.verify(self.w,oid,{'verification_code':wrong}))
        self.assertEqual(before,self.g.overview(self.w)['total_cash_minor'])

    def test_conditional_service_rule_metrics_keep_planning_kind_and_type(self):
        result=self.tools.invoke('service_offer_rules',{})
        self.assertTrue(result['metrics'])
        batch=self.tools.metric_batches[-1]
        for metric in batch['metrics']:
            self.assertEqual(batch['metric_kinds'][metric['metric_id']],'planning_scenario')
            self.assertEqual(metric['assumed_inputs'][0]['type'],'conditional_service_terms')
            self.assertIn('not_account_cash',metric['excluded_items'])


if __name__=='__main__':unittest.main()
