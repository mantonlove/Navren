"""原S10真实只读工具链及金额合同；不运行模型或更改黄金题。"""
from contextlib import closing
from copy import deepcopy
import unittest

from agent_runtime.data_boundary import CloudDataBoundary
from agent_runtime.metric_claims import REF_FIELDS
from financial_metrics import fingerprint
from test_base_tool_metrics import _ToolFixture


class BudgetDescriptorTests(_ToolFixture):
    evidence=[]

    def business_state(self):
        names=('accounts','profiles','cards','credit_statements','fund_holds','holdings',
               'operations','orders','transactions','wealth_goals','goal_allocations',
               'navren_goal_drafts','navren_goal_occupancy_links','orchestration_tasks','audit')
        with closing(self.g.bank.connect()) as db:
            value={name:[dict(r) for r in db.execute('SELECT * FROM '+name+' WHERE workspace_id=? ORDER BY rowid',(self.w,))]
                   for name in names}
            value['workspace']=dict(db.execute('SELECT * FROM workspaces WHERE id=?',(self.w,)).fetchone())
        return fingerprint(value)

    def s10(self):
        before=self.business_state()
        baseline,_=self.invoke('budget',{'account_id':self.aid})
        commitment=next(c for c in baseline['data']['commitments']
                        if c['kind']=='credit_card' and c['account_id']==self.aid)
        args={'account_id':self.aid,'reserves':[{'label':'本期信用卡目标留款',
               'amount_minor':250000,'commitment_ids':[commitment['evidence_id']]}]}
        result,batch=self.invoke('budget',args)
        after=self.business_state();self.assertEqual(before,after)
        return baseline,commitment,result,batch,before

    def answer(self,batch):
        pieces=[('planning_reserve_target','目标2500元'),
                ('already_budgeted_linked_commitment','已有2000元计入'),
                ('additional_planning_reserve','本次额外留款为500元'),
                ('planning_arrangeable_now','可安排9776元')]
        claims=[{**self.tools.metrics.binding(self.find(batch,name)['metric_id']),'text':text}
                for name,text in pieces]
        return {'text':'目标2500元中已有2000元计入，本次额外留款为500元，可安排9776元。预算情景没有实际冻结、预留或支付。',
                'claims':[],'amount_claims':claims,'draft_ids':[],'followups':[]}

    def test_original_S10_actual_source_four_independent_signed_amounts_without_writes(self):
        baseline,commitment,result,batch,before=self.s10()
        self.assertEqual(baseline['data']['investable_minor'],1027600)
        self.assertEqual(baseline['data']['upcoming_due_minor'],261400)
        self.assertEqual(commitment['amount_minor'],200000)
        self.assertEqual(result['data']['additional_reserve_minor'],50000)
        self.assertEqual(result['data']['investable_minor'],977600)
        target=self.find(batch,'planning_reserve_target');linked=self.find(batch,'already_budgeted_linked_commitment')
        self.assertEqual(target['value_minor'],250000);self.assertEqual(linked['value_minor'],200000)
        self.assertEqual(batch['metric_kinds'][target['metric_id']],'planning_scenario')
        self.assertEqual(batch['metric_kinds'][linked['metric_id']],'recorded_fact')
        self.assertIn('not_actual_hold',target['basis']);self.assertIn('already_deducted',linked['basis'])
        self.assertEqual(target['formula_id'],'tools.budget_planning/1')
        self.assertEqual(linked['formula_id'],'tools.budget_inputs/1')
        self.assertEqual(linked['assumed_inputs'],[])
        self.assertEqual(target['assumed_inputs'][0]['type'],'user_planning_reserve')
        reserve_ref=next(r for r in target['input_refs'] if r.startswith('planning_reserve/'))
        tool_ref=next(r for r in target['input_refs'] if r.startswith('tool_evidence/budget/'))
        self.assertEqual(target['source_versions'][reserve_ref],target['source_versions'][tool_ref])
        with closing(self.g.bank.connect()) as db:
            row=dict(db.execute('SELECT * FROM credit_statements WHERE workspace_id=? AND id=?',
                     (self.w,commitment['evidence_id'])).fetchone())
            card=db.execute('SELECT account_id FROM cards WHERE workspace_id=? AND id=?',(self.w,row['card_id'])).fetchone()
        self.assertEqual(card['account_id'],self.aid);self.assertEqual(row['status'],'open')
        self.assertEqual(max(0,row['total_due_minor']-row['paid_minor']),linked['value_minor'])
        for metric in (target,linked):
            self.assertEqual(metric['subject_id'],self.aid);self.assertEqual(metric['currency'],'CNY')
            self.assertIsNone(metric['as_of']);self.assertEqual(metric['period_start'],baseline['data']['as_of'])
            self.assertEqual(metric['period_end'],self.g.overview(self.w)['commitment_window_end'])
            self.assertIn('accounts/'+self.aid,metric['input_refs'])
            self.assertIn('credit_statements/'+row['id'],metric['input_refs'])
            self.assertIn('tool_table/credit_statements/'+self.w,metric['source_versions'])
        answer=self.answer(batch);contract=self.tools.amount_contract(answer)
        self.assertEqual(contract['amount_occurrence_count'],4)
        self.assertEqual(len(set(c['metric_id'] for c in answer['amount_claims'])),4)
        self.assertFalse(batch['coverage']['real_reservation_created'])
        self.assertEqual(self.business_state(),before)
        self.evidence.append({'test':self.id(),'baseline':baseline,'actual_credit_statement':row,
            'arguments':batch['arguments'],'result':result,'signed_batch':batch,'answer':answer,
            'amount_contract':contract,'business_state_before':before,'business_state_after':self.business_state(),
            'scope':'actual BankTools+local money binding; no model review or semantic acceptance'})

    def test_original_S10_full_and_compact_boundary_roundtrip_keeps_claimable_descriptors(self):
        _,_,result,batch,before=self.s10()
        names=('planning_reserve_target','already_budgeted_linked_commitment',
               'additional_planning_reserve','planning_arrangeable_now')
        for compact in (False,True):
            with self.subTest(compact=compact):
                boundary=CloudDataBoundary()
                projected=boundary.model_tool_result('budget',result,compact_metric_sources=compact)
                self.assertEqual(boundary.restore_output(projected)['metrics'],result['metrics'])
                self.assertEqual(projected['metrics_coverage']['status'],'complete')
                selected={r['metric']['metric_name']:r for r in projected['metrics']}
                answer=self.answer(batch);model_claims=[]
                for claim,name in zip(answer['amount_claims'],names):
                    row=selected[name];metric=row['metric']
                    model_claim={key:deepcopy(metric[key]) for key in REF_FIELDS if key!='source_versions'}
                    key='source_version_ref' if compact else 'source_versions'
                    model_claim.update({key:deepcopy(metric[key]),'text':claim['text'],'claim_kind':row['kind']})
                    model_claims.append(model_claim)
                restored=boundary.restore_arguments('answer_reviewed',{**answer,'amount_claims':model_claims})
                self.assertEqual(restored['amount_claims'],answer['amount_claims'])
                contract=self.tools.amount_contract(restored)
                self.assertEqual(contract['amount_occurrence_count'],4)
        self.assertEqual(self.business_state(),before)

    def test_wrong_kind_subject_period_currency_amount_cannot_explain_original_target(self):
        _,_,_,batch,_=self.s10();answer=self.answer(batch)
        for field,value in (('claim_kind','recorded_fact'),('subject_id',self.w),('currency','USD'),
                            ('period_start','2026-09-01'),('value_minor',200000)):
            with self.subTest(field=field):
                bad=deepcopy(answer);bad['amount_claims'][0][field]=value
                self.reject('METRIC_KIND_MISMATCH' if field=='claim_kind' else 'METRIC_CLAIM_MISMATCH',
                            lambda:self.tools.amount_contract(bad))
        bad=deepcopy(answer);bad['amount_claims'][1]['claim_kind']='planning_scenario'
        self.reject('METRIC_KIND_MISMATCH',lambda:self.tools.amount_contract(bad))

    def test_same_bank_version_credit_statement_change_invalidates_new_batch(self):
        _,commitment,_,batch,_=self.s10()
        with self.g.bank.transaction() as db:
            version=db.execute('SELECT version FROM workspaces WHERE id=?',(self.w,)).fetchone()[0]
            db.execute('UPDATE credit_statements SET paid_minor=paid_minor+1 WHERE workspace_id=? AND id=?',
                       (self.w,commitment['evidence_id']))
            self.assertEqual(db.execute('SELECT version FROM workspaces WHERE id=?',(self.w,)).fetchone()[0],version)
        self.reject('METRIC_VERSION_CONFLICT',lambda:self.service.validate(self.w,batch))

    def test_new_descriptors_tamper_and_public_rehash_cannot_pass_service_signature(self):
        _,_,_,batch,_=self.s10()
        for name in ('planning_reserve_target','already_budgeted_linked_commitment'):
            with self.subTest(name=name):
                bad=deepcopy(batch);self.find(bad,name)['value_minor']+=1
                self.rehash(bad)
                self.reject('METRIC_TAMPERED',lambda:self.service.validate(self.w,bad))

    def test_empty_links_have_actual_zero_overlap_and_full_new_target_deduction(self):
        baseline,_=self.invoke('budget',{'account_id':self.aid})
        result,batch=self.invoke('budget',{'account_id':self.aid,'reserves':[{
            'label':'本期信用卡目标留款','amount_minor':250000,'commitment_ids':[]}]})
        target=self.find(batch,'planning_reserve_target');linked=self.find(batch,'already_budgeted_linked_commitment')
        self.assertEqual(target['value_minor'],250000);self.assertEqual(linked['value_minor'],0)
        self.assertFalse(any(r.startswith('credit_statements/') for r in linked['input_refs']))
        self.assertEqual(result['data']['additional_reserve_minor'],250000)
        self.assertEqual(result['data']['investable_minor'],baseline['data']['investable_minor']-250000)

    def test_same_amount_and_label_reserves_keep_distinct_parameter_identity(self):
        self.invoke('budget',{'account_id':self.aid})
        reserve={'label':'相同名称的独立规划','amount_minor':2500,'commitment_ids':[]}
        _,batch=self.invoke('budget',{'account_id':self.aid,'reserves':[reserve,deepcopy(reserve)]})
        targets=[m for m in batch['metrics'] if m['metric_name']=='planning_reserve_target']
        self.assertEqual(len(targets),2);self.assertNotEqual(targets[0]['metric_id'],targets[1]['metric_id'])
        refs=[{r for r in m['input_refs'] if r.startswith('planning_reserve/')} for m in targets]
        self.assertFalse(refs[0]&refs[1])
        for i,metric in enumerate(targets):
            self.assertEqual(batch['coverage']['planning_reserve_metrics'][i]['target_metric_id'],metric['metric_id'])


if __name__=='__main__':unittest.main()
