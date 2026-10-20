"""防止实验把预检、HTTP200、approved或未知费用当正确结果；全程无云。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.jev_business.catalog import load_dataset, selected_cases
from evaluation.jev_business.oracle import build_fixture, preflight, snapshot, classify, clone_database, independent_accounts
from scripts.run_jev_business_ab import ROOT, cost, validate_pricing, source_files, validate_overlay, TrialServerLifecycle
import scripts.run_jev_business_ab as business_runner


class BusinessHarnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=load_dataset()

    def case(self,ident):return next(c for c in self.data['cases'] if c['id']==ident)

    def record(self):
        with tempfile.TemporaryDirectory() as temp:
            c=self.case('P07');g,w,_=build_fixture(c,temp);pre=preflight(g,w,c);state=snapshot(g,w)
        return {'id':'P07','question':c['question'],'preflight':pre,'before':state,'after':deepcopy(state),
            'http_status':200,'result':{'reply':'请补充产品名称、代码或页面。','bank_response_id':'local-fixture',
                'answer_contract':{'kind':'reviewed_text','evidence_ids':[]},'agent_review':{'status':'approved'},
                'agent_runtime':{},'bank_actions':[]},'tool_outputs':[]}

    def test_original_question_sources_and_groups(self):
        self.assertEqual(len(selected_cases(self.data)),6)
        self.assertEqual(len(selected_cases(self.data,regression=True)),2)
        for pending in ('P03','P06'):
            with self.assertRaisesRegex(ValueError,'oracle_pending'):selected_cases(self.data,[pending])
        with self.assertRaisesRegex(ValueError,'case_group_mismatch'):selected_cases(self.data,['R01'])
        corpus={str(p.relative_to(ROOT)) for p in source_files(ROOT)}
        self.assertTrue(set(self.data['input_source_sha256']).issubset(corpus))

    def test_explicit_temporary_databases_and_seed_oracles_ignore_live_environment(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ',{'NAVREN_KNOWLEDGE_DB':'/must-not-open/private.sqlite','NAVREN_FINANCIAL_DB':'/must-not-open/financial.sqlite'}):
            g,w,_=build_fixture(self.case('P01'),temp)
            self.assertEqual(preflight(g,w,self.case('P01'))['status'],'passed')
            self.assertIsNone(g.financial_data)
            self.assertTrue(g.bank.database.is_relative_to(Path(temp).resolve()))
            self.assertEqual(g._jev_business_fixture['accounts'],independent_accounts())

    def test_calendar_original_range_and_service_no_application_preflight(self):
        for ident in ('P02','P08'):
            with tempfile.TemporaryDirectory() as temp:
                c=self.case(ident);g,w,_=build_fixture(c,temp);before=snapshot(g,w)
                self.assertEqual(preflight(g,w,c)['status'],'passed')
                self.assertEqual(snapshot(g,w),before)

    def test_historical_fee_not_ready_cannot_be_promoted_by_provider_pass(self):
        c=self.case('P05')
        self.assertEqual(c['fixture_status'],'historical_context_unavailable')
        r=self.record();r.update(id=c['id'],question=c['question'])
        r['preflight']['status']='not_ready'
        self.assertEqual(classify(r,c)['status'],'fixture_or_runner_error')

    def test_review_approved_is_never_semantic_gold(self):
        r=self.record();verdict=classify(r,self.case('P07'))
        self.assertEqual(verdict['status'],'needs_independent_semantic_review')
        self.assertIsNone(verdict['semantic_pass'])
        self.assertFalse(verdict['reviewer_approved_used_as_gold'])

    def test_http200_and_visible_text_without_terminal_contract_fails(self):
        r=self.record();r['result']['answer_contract']=None
        v=classify(r,self.case('P07'))
        self.assertFalse(v['technical_completed']);self.assertEqual(v['status'],'technical_failure')

    def test_fallback_side_effect_and_question_substitution_detected(self):
        c=self.case('P07');r=self.record();r['result']['isFallback']=True
        self.assertEqual(classify(r,c)['status'],'technical_failure')
        r=self.record();r['after']['accounts'][0]['balance_minor']+=1
        self.assertEqual(classify(r,c)['status'],'necessary_condition_failure')
        r=self.record();r['question']+='额外指定一个产品'
        self.assertEqual(classify(r,c)['status'],'necessary_condition_failure')

    def test_public_clone_requires_new_target_and_preserves_source(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            src=Path(temp)/'source.db';dest=Path(temp)/'clone.db'
            with sqlite3.connect(src) as db:db.execute('CREATE TABLE gold(x)');db.execute('INSERT INTO gold VALUES(7)')
            original=src.read_bytes();clone_database(src,dest)
            with sqlite3.connect(dest) as db:db.execute('UPDATE gold SET x=8')
            self.assertEqual(src.read_bytes(),original)
            with self.assertRaises(ValueError):clone_database(src,dest)

    def pricing(self):
        return {'schema_version':'navren.jev-business-pricing.v1','verified':True,'verified_at':'2026-10-07','sources':['fixture-pricing-not-real-experiment'],
            'comparison_fx_cny_per_usd':7.2,'rates':{
                'primary':{'model':'DeepSeek-V4-Flash','provider':'ai_ping','currency':'CNY','input_range':[1,1],'output_range':[2,2]},
                'reviewer':{'model':'GLM-5.3-Flash','provider':'ai_ping','currency':'CNY','input_range':[1,1],'output_range':[2,3]},
                'jev':{'model':'jev-1.13.0','provider':'typesafe_official','currency':'USD','input_range':[0.042,0.042],'output_range':[0,0]}}}

    def test_usage_unknown_model_mismatch_or_missing_prices_never_zero_cost(self):
        p=validate_pricing(self.pricing())
        call={'stage':'primary','model_actual':'DeepSeek-V4-Flash','usage':{'prompt_tokens':100,'completion_tokens':10}}
        self.assertTrue(cost([call],p)['usage_and_price_verified'])
        for calls,prices in (([dict(call,usage=None)],p),([dict(call,model_actual='wrong')],p),([call],None)):
            result=cost(calls,prices);self.assertFalse(result['usage_and_price_verified']);self.assertIsNone(result['estimated_cny_range'])
        bad=self.pricing();bad['verified']=False
        with self.assertRaisesRegex(ValueError,'verified_pricing'):validate_pricing(bad)

    def test_overlay_rejects_stale_base_altered_candidate_and_extra_module(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)/'base';over=Path(temp)/'over'
            for p in (base/'agent_runtime',over/'agent_runtime'):p.mkdir(parents=True)
            (base/'agent_runtime/conversation.py').write_text('original')
            (over/'agent_runtime/conversation.py').write_text('candidate')
            m={'source':'agent_runtime/conversation.py','base_sha256':hashlib.sha256(b'original').hexdigest(),'overlay_sha256':hashlib.sha256(b'candidate').hexdigest()}
            (over/'overlay.json').write_text(json.dumps(m));validate_overlay(over,base)
            (base/'agent_runtime/conversation.py').write_text('changed')
            with self.assertRaisesRegex(ValueError,'base_source_changed'):validate_overlay(over,base)
            (base/'agent_runtime/conversation.py').write_text('original')
            (over/'agent_runtime/conversation.py').write_text('changed')
            with self.assertRaisesRegex(ValueError,'file_hash_mismatch'):validate_overlay(over,base)
            (over/'agent_runtime/extra.py').write_text('unauthorized')
            with self.assertRaisesRegex(ValueError,'only_cover'):validate_overlay(over,base)

    def test_trial_server_waits_late_http_worker_before_temporary_cleanup(self):
        import http.server
        import threading
        import time
        import urllib.request
        worker_finished=threading.Event()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    self.send_response(200);self.send_header('Content-Length','2');self.end_headers()
                    self.wfile.write(b'{}');self.wfile.flush()
                    time.sleep(.05)
                    (root/'late-handler-write').write_text('after HTTP response')
                    worker_finished.set()
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            with TrialServerLifecycle() as lifecycle:
                lifecycle.start(server)
                with urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)) as response:
                    self.assertEqual(response.read(),b'{}')
            self.assertTrue(worker_finished.is_set())
            self.assertFalse(lifecycle.thread.is_alive())
            self.assertTrue((root/'late-handler-write').exists())
        self.assertFalse(root.exists())

    def test_v2_overlay_limits_common_method_and_cannot_mark_paid_ready(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)/'base';over=Path(temp)/'over'
            targets={}
            for rel in ('agent_runtime/conversation.py','skills/bank-service/SKILL.md'):
                for root in (base,over):(root/rel).parent.mkdir(parents=True,exist_ok=True)
                (base/rel).write_text('original');(over/rel).write_text('candidate')
                targets[rel]={'base_sha256':hashlib.sha256(b'original').hexdigest(),
                              'candidate_sha256':hashlib.sha256(b'candidate').hexdigest()}
            metadata={'schema_version':'navren.jev-candidate-overlay.v2','status':'offline_candidate_only',
                'ready_for_paid_collection':False,'same_method_for_both_arms':True,'targets':targets}
            path=over/'overlay.json';path.write_text(json.dumps(metadata))
            self.assertEqual(set(validate_overlay(over,base)['validated_targets']),set(targets))
            metadata['ready_for_paid_collection']=True;path.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError,'offline_candidate_required'):validate_overlay(over,base)
            metadata['ready_for_paid_collection']=False;path.write_text(json.dumps(metadata))
            (over/'agent_runtime/bank_tools.py').write_text('must not replace bank controls')
            with self.assertRaisesRegex(ValueError,'declared_common_targets'):validate_overlay(over,base)

    def test_offline_collection_gate_precedes_preflight_and_credentials(self):
        from scripts.run_jev_business_ab import collect
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            (out/'protocol.json').write_text(json.dumps({'source_sha256':{},
                'collection_readiness_blocks':['candidate_common_method_not_cloud_reapproved','prior_sent_usage_unverified']}))
            with self.assertRaisesRegex(ValueError,'offline_candidate_collection_blocked'):
                collect(out)
            self.assertFalse((out/'collection-start.json').exists())

    def test_v3_overlay_allows_registered_plan_file_but_not_arbitrary_execution(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)/'base';over=Path(temp)/'over';targets={}
            for rel in ('agent_runtime/conversation.py','agent_runtime/jev_plans.py','skills/bank-service/SKILL.md'):
                for root in (base,over):(root/rel).parent.mkdir(parents=True,exist_ok=True)
                (base/rel).write_text('original');(over/rel).write_text('candidate')
                targets[rel]={'base_sha256':hashlib.sha256(b'original').hexdigest(),
                              'candidate_sha256':hashlib.sha256(b'candidate').hexdigest()}
            metadata={'schema_version':'navren.jev-candidate-overlay.v3','status':'offline_candidate_only',
                'ready_for_paid_collection':False,'same_method_for_both_arms':True,'targets':targets}
            (over/'overlay.json').write_text(json.dumps(metadata))
            self.assertEqual(set(validate_overlay(over,base)['validated_targets']),set(targets))
            (over/'agent_runtime/bank_tools.py').write_text('must not override deterministic execution')
            with self.assertRaisesRegex(ValueError,'declared_common_targets'):validate_overlay(over,base)

    def test_trial_server_closes_on_exception_before_directory_exit(self):
        import threading
        done=threading.Event();events=[]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            class Server:
                def serve_forever(self):done.wait()
                def shutdown(self):
                    events.append(('shutdown',root.exists()));done.set()
                def server_close(self):events.append(('close',root.exists()))
            with self.assertRaisesRegex(ValueError,'request_failed'):
                with TrialServerLifecycle() as lifecycle:
                    lifecycle.start(Server());raise ValueError('request_failed')
            self.assertEqual(events,[('shutdown',True),('close',True)])
            self.assertFalse(lifecycle.thread.is_alive())
        self.assertFalse(root.exists())

    def test_trial_server_not_created_failure_requires_no_shutdown(self):
        with self.assertRaisesRegex(ValueError,'fixture_failed'):
            with TrialServerLifecycle() as lifecycle:raise ValueError('fixture_failed')
        self.assertEqual(lifecycle.cleanup_ms,0)

    def test_v3_candidate_cannot_borrow_prior_pass_or_change_limits(self):
        stage=ROOT/'outputs/jev-iteration-20261007/v4-terminal-review-20261008'
        method=json.loads((stage/'approved-spec.json').read_text())
        overlay=validate_overlay(ROOT/'outputs/jev-iteration-20261007/candidate-v4-overlay')
        kwargs={'out':method['collection_scope']['approved_output_dir'],'case_ids':['P08'],'repetitions':1}
        with self.assertRaisesRegex(ValueError,'review_evidence_missing'):
            business_runner.validate_method_v3(method,stage,overlay,**kwargs)
        changed=deepcopy(method);changed['shared_options']['max_tokens']+=1
        with self.assertRaisesRegex(ValueError,'approved_spec_changed'):
            business_runner.validate_method_v3(changed,stage,overlay,**kwargs)
        with self.assertRaisesRegex(ValueError,'only_reviewed_limited'):
            business_runner.validate_method_v3(method,stage,overlay,**{**kwargs,'case_ids':['P01']})
        altered=deepcopy(overlay);altered['validated_targets']['base_runtime/reply_interaction.py']['candidate_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'exact_reviewed_overlay'):
            business_runner.validate_method_v3(method,stage,altered,**kwargs)

    def test_v3_patch_and_receipt_cannot_self_consistently_replace_reviewed_diff(self):
        stage=ROOT/'outputs/jev-iteration-20261007/v4-terminal-review-20261008'
        original=stage/'candidate.patch'
        self.assertEqual(business_runner.digest(original),business_runner.V3_PATCH_SHA)
        self.assertEqual(original.read_bytes(),
            (ROOT/'outputs/jev-iteration-20261007/candidate-v4-overlay/from-v3.patch').read_bytes())
        self.assertEqual(business_runner.validate_method_v3_patch(stage,
            {'candidate.patch':business_runner.V3_PATCH_SHA}),original)
        method=json.loads((stage/'approved-spec.json').read_text())
        overlay=validate_overlay(ROOT/'outputs/jev-iteration-20261007/candidate-v4-overlay')
        kwargs={'out':method['collection_scope']['approved_output_dir'],'case_ids':['P08'],'repetitions':1}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'approved-spec.json').write_bytes((stage/'approved-spec.json').read_bytes())
            changed=root/'candidate.patch'
            changed.write_bytes(original.read_bytes()+b'\n# altered reviewed diff\n')
            changed_sha=business_runner.digest(changed)
            method['review_artifacts']={name:business_runner.V3_PATCH_SHA for name in
                ('approved-spec.json','approved-spec-review.json','candidate.patch','proposal.md',
                 'dry-run.json','review-attempt-4.json',method['offline_evidence']['artifact'])}
            method['review_artifacts']['candidate.patch']=changed_sha
            self.assertEqual(business_runner.digest(changed),method['review_artifacts']['candidate.patch'])
            self.assertFalse((root/'review-attempt-4.json').exists())
            with self.assertRaisesRegex(ValueError,'method_v3_reviewed_patch_sha_required'):
                business_runner.validate_method_v3_patch(root,method['review_artifacts'])
            with self.assertRaisesRegex(ValueError,'method_v3_reviewed_patch_sha_required'):
                business_runner.validate_method_v3(method,root,overlay,**kwargs)
            with self.assertRaisesRegex(ValueError,'method_review_evidence_changed:candidate.patch'):
                business_runner.validate_method_v3_patch(root,{'candidate.patch':business_runner.V3_PATCH_SHA})

    def v2_contract(self,evidence_dir):
        """本地复制实际审核原件；合约的免费证明仍 pending，不产生付费准入。"""
        import shutil
        directory=ROOT/'outputs/jev-iteration-20261007/method-review-v4-20261008'
        for name in business_runner.V2_REVIEW_ARTIFACTS:
            shutil.copyfile(directory/name,evidence_dir/name)
        return {'schema_version':'navren.jev-business-method.v2',
            'status':'method_pass_only_not_implementation_or_performance',
            'review_id':business_runner.V2_REVIEW_ID,'review_artifact_sha256':business_runner.V2_ARTIFACT_SHA,
            'review_artifacts':deepcopy(business_runner.V2_REVIEW_ARTIFACTS),
            'approved_overlay_sha256':deepcopy(business_runner.V2_OVERLAY_SHA),
            'channels':deepcopy(business_runner.CHANNELS),'shared_options':deepcopy(business_runner.SHARED_OPTIONS),
            'collection_scope':{'kind':'limited_component_pilot','case_ids':['P08'],'repetitions_per_arm':1,
                'arms':['baseline','jev'],'approved_output_dir':str(business_runner.V2_PILOT_OUT.resolve()),'max_freezes':1,
                'baseline_first':True,'baseline_technical_or_accounting_failure_stops':True},
            'stage_disposition':{'prior_pilot':'pilot-04-readplans','prior_pilot_permanently_closed':True,
                'prior_sent_usage_status':'unverified','prior_full_cost_status':'unknown','historical_accounting_resolved':False,
                'old_results_sha256':business_runner.V2_OLD_RESULTS_SHA,'new_stage_excludes_prior_trials':True,
                'no_retry_of_prior_request':True},'required_free_evidence':{'status':'pending'}}

    def v3_metadata(self):
        return validate_overlay(ROOT/'outputs/jev-iteration-20261007/candidate-v3-overlay')

    def validate_v2(self,method,evidence_dir,**changes):
        args={'out':business_runner.V2_PILOT_OUT,'case_ids':['P08'],'repetitions':1}
        args.update(changes)
        return business_runner.validate_method_v2(method,evidence_dir,self.v3_metadata(),**args)

    def test_actual_v2_method_review_does_not_clear_pending_free_proof(self):
        marker=business_runner.V2_PILOT_OUT/'collection-start.json'
        before=marker.read_bytes() if marker.exists() else None
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            dry=json.loads((root/'dry-run.json').read_text())['review_packet']
            self.assertNotEqual(dry['review_id'],method['review_id'])
            self.assertEqual(dry['artifact_sha256'],method['review_artifact_sha256'])
            self.assertEqual(self.validate_v2(method,root),['method_v2_required_free_evidence_pending'])
            self.assertEqual(marker.read_bytes() if marker.exists() else None,before)

    def test_v2_fake_pass_cannot_self_declare_reviewer_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            path=root/'review-attempt-2.json';path.write_text(json.dumps({'verdict':'PASS'}))
            method['review_artifacts'][path.name]=business_runner.digest(path)
            with self.assertRaisesRegex(ValueError,'actual_review_sha_required'):
                self.validate_v2(method,root)

    def test_v2_deep_reviewer_validation_rejects_missing_usage_even_with_test_trust_anchor(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root);path=root/'review-attempt-2.json'
            review=json.loads(path.read_text());review['reviews']['deepseek_flash']['review']['review_metadata']['usage']=None
            path.write_text(json.dumps(review));sha=business_runner.digest(path)
            method['review_artifacts'][path.name]=sha
            # 仅为单元测试越过外层原件摘要，确保内部 usage 校验自身也 fail closed。
            with patch.dict(business_runner.V2_REVIEW_ARTIFACTS,{path.name:sha}):
                with self.assertRaisesRegex(ValueError,'complete_review_usage_required'):
                    self.validate_v2(method,root)

    def test_v2_deep_reviewer_validation_rejects_partial_coverage_wrong_route_or_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root);path=root/'review-attempt-2.json';original=json.loads(path.read_text())
            for kind in ('coverage','route','attempts','critical'):
                value=deepcopy(original);entry=value['reviews']['glm_5_3_flash']
                if kind=='coverage':entry['review']['coverage']='partial'
                elif kind=='route':entry['review']['review_metadata']['endpoint']='https://must-not-call.example/api'
                elif kind=='attempts':entry['attempts']=2
                else:entry['review']['blocking_issues']=[{'severity':'critical','reason':'fixture'}]
                path.write_text(json.dumps(value));sha=business_runner.digest(path);method['review_artifacts'][path.name]=sha
                with patch.dict(business_runner.V2_REVIEW_ARTIFACTS,{path.name:sha}):
                    with self.assertRaises(ValueError):self.validate_v2(method,root)

    def test_v2_rejects_unreviewed_overlay_sha_and_missing_overlay(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            method['approved_overlay_sha256']['agent_runtime/conversation.py']='0'*64
            with self.assertRaisesRegex(ValueError,'exact_reviewed_v3_overlay_required'):self.validate_v2(method,root)
            method=self.v2_contract(root)
            with self.assertRaisesRegex(ValueError,'exact_reviewed_v3_overlay_required'):
                business_runner.validate_method_v2(method,root,None,out=business_runner.V2_PILOT_OUT,case_ids=['P08'],repetitions=1)

    def test_v2_wrong_case_repeat_regression_output_or_old_resolved_stage_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            for changes in ({'case_ids':['P04']},{'repetitions':2},{'regression':True},{'baseline_pilot_only':True},
                            {'out':root/'another-pilot'}):
                with self.assertRaisesRegex(ValueError,'only_one_limited_P08'):self.validate_v2(method,root,**changes)
            method['stage_disposition']['historical_accounting_resolved']=True
            with self.assertRaisesRegex(ValueError,'old_stage_closed_unknown_retained'):self.validate_v2(method,root)

    def test_v2_free_evidence_status_flag_cannot_substitute_actual_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            method['required_free_evidence']={'status':'completed_with_limits'}
            with self.assertRaisesRegex(ValueError,'actual_post_preview_evidence_required'):self.validate_v2(method,root)

    def test_v2_actual_post_preview_limits_kept_and_pending_boundary_still_blocks(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root);name='v3-post-preview-admission-probe.json'
            shutil.copyfile(ROOT/'outputs/jev-iteration-20261007'/name,root/name)
            sha=business_runner.digest(root/name);method['review_artifacts'][name]=sha
            method['required_free_evidence']={'status':'completed_with_limits','post_preview':{
                'artifact':name,'sha256':sha,'frozen_protocol_sha256':business_runner.V2_POST_PREVIEW_PROTOCOL_SHA},
                'boundary_diagnostic':{'status':'pending'}}
            self.assertEqual(self.validate_v2(method,root),['method_v2_boundary_diagnostic_pending'])

    def test_v2_unresolved_actual_boundary_diagnostic_cannot_release_collection(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            post='v3-post-preview-admission-probe.json';diagnostic='v3-boundary-diagnostic.json'
            for name in (post,diagnostic):
                shutil.copyfile(ROOT/'outputs/jev-iteration-20261007'/name,root/name)
                method['review_artifacts'][name]=business_runner.digest(root/name)
            method['required_free_evidence']={'status':'completed_with_limits','post_preview':{
                'artifact':post,'sha256':method['review_artifacts'][post],
                'frozen_protocol_sha256':business_runner.V2_POST_PREVIEW_PROTOCOL_SHA},
                'boundary_diagnostic':{'status':'completed_with_limits','artifact':diagnostic,
                    'sha256':method['review_artifacts'][diagnostic]}}
            self.assertEqual(self.validate_v2(method,root),['method_v2_boundary_disposition_pending'])
            report=json.loads((root/diagnostic).read_text());report['diagnostic_status']='root_cause_identified'
            (root/diagnostic).write_text(json.dumps(report));sha=business_runner.digest(root/diagnostic)
            method['review_artifacts'][diagnostic]=sha;method['required_free_evidence']['boundary_diagnostic']['sha256']=sha
            with self.assertRaisesRegex(ValueError,'boundary_diagnostic_hash_not_frozen'):self.validate_v2(method,root)

    def test_v2_json_types_cannot_turn_unknown_into_boolean_or_raise_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.v2_contract(root)
            method['stage_disposition']['historical_accounting_resolved']=0
            with self.assertRaisesRegex(ValueError,'old_stage_closed_unknown_retained'):self.validate_v2(method,root)
            method=self.v2_contract(root);method['shared_options']['max_tokens']=100000.0
            with self.assertRaisesRegex(ValueError,'reviewed_contract_required'):self.validate_v2(method,root)

    def completed_v2_evidence(self,root):
        import shutil
        method=self.v2_contract(root);post='v3-post-preview-admission-probe.json';diag='v3-boundary-diagnostic.json'
        for name in (post,diag):
            shutil.copyfile(ROOT/'outputs/jev-iteration-20261007'/name,root/name)
            method['review_artifacts'][name]=business_runner.digest(root/name)
        for name,sha in business_runner.V2_BOUNDARY_REVIEW_ARTIFACTS.items():
            shutil.copyfile(ROOT/'outputs/jev-iteration-20261007/method-review-v4-20261008'/name,root/name)
            method['review_artifacts'][name]=sha
        method['required_free_evidence']={'status':'completed_with_limits','post_preview':{
            'artifact':post,'sha256':method['review_artifacts'][post],
            'frozen_protocol_sha256':business_runner.V2_POST_PREVIEW_PROTOCOL_SHA},
            'boundary_diagnostic':{'status':'completed_with_limits','artifact':diag,'sha256':method['review_artifacts'][diag]},
            'boundary_disposition':{'status':'independently_accepted_for_one_limited_pilot',
                'review_id':business_runner.V2_BOUNDARY_REVIEW_ID,'review_artifact_sha256':business_runner.V2_BOUNDARY_ARTIFACT_SHA,
                'review_artifacts':deepcopy(business_runner.V2_BOUNDARY_REVIEW_ARTIFACTS),
                'scope':'one_P08_baseline_first_pair_only','original_boundary_failure_resolved':False,
                'diagnostic_status':'not_reproduced_unresolved','formal_adoption_approved':False,'performance_pass':False}}
        return method

    def test_actual_supplemental_review_allows_only_limited_pilot_without_fixed_claim(self):
        marker=business_runner.V2_PILOT_OUT/'collection-start.json'
        before=marker.read_bytes() if marker.exists() else None
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.completed_v2_evidence(root)
            self.assertEqual(self.validate_v2(method,root),[])
            diagnostic=json.loads((root/'v3-boundary-diagnostic.json').read_text())
            self.assertEqual(diagnostic['diagnostic_status'],'not_reproduced_unresolved')
            self.assertFalse(diagnostic['original_failure_marked_fixed'])
            self.assertEqual(marker.read_bytes() if marker.exists() else None,before)

    def test_supplemental_pass_flag_or_resolved_claim_cannot_replace_real_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.completed_v2_evidence(root)
            proof=method['required_free_evidence']['boundary_disposition'];proof['original_boundary_failure_resolved']=True
            with self.assertRaisesRegex(ValueError,'actual_limited_boundary_disposition_required'):self.validate_v2(method,root)
            method=self.completed_v2_evidence(root);path=root/'supplemental-boundary-review.json'
            path.write_text(json.dumps({'verdict':'PASS'}));sha=business_runner.digest(path)
            method['review_artifacts'][path.name]=sha
            method['required_free_evidence']['boundary_disposition']['review_artifacts'][path.name]=sha
            with self.assertRaisesRegex(ValueError,'actual_limited_boundary_disposition_required'):self.validate_v2(method,root)

    def test_supplemental_review_missing_usage_rejected_below_trust_anchor(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);method=self.completed_v2_evidence(root);path=root/'supplemental-boundary-review.json'
            review=json.loads(path.read_text());review['reviews']['glm_5_3_flash']['review']['review_metadata']['usage']=None
            path.write_text(json.dumps(review));sha=business_runner.digest(path)
            method['review_artifacts'][path.name]=sha
            method['required_free_evidence']['boundary_disposition']['review_artifacts'][path.name]=sha
            with patch.dict(business_runner.V2_BOUNDARY_REVIEW_ARTIFACTS,{path.name:sha}):
                with self.assertRaisesRegex(ValueError,'complete_review_usage_required'):self.validate_v2(method,root)

    def test_known_P08_rule_constraint_failure_stops_while_ordinary_semantic_error_does_not(self):
        row={'preflight':{'status':'passed'},'technical_completed':True,'before':{'balance':7},'after':{'balance':7},
             'result':{'bank_actions':[]},'independent_checks':{'semantic_pass':False,'failed_checks':['普通解释不充分']}}
        self.assertIsNone(business_runner.limited_component_stop_reason(row))
        row['independent_checks']['failed_checks']=['两原候选与拒买底线']
        self.assertIsNone(business_runner.limited_component_stop_reason(row))
        row['authoritative_tool_outputs']=[{'name':'service_offer_preview','output':{'data':{'offers':[]}}}]
        self.assertEqual(business_runner.limited_component_stop_reason(row),'limited_component_known_serious_rule_constraint_failure')

    def test_failure_observer_forwards_original_arguments_returns_and_same_exception(self):
        class BoundaryError(Exception):code='BOUNDARY_UNKNOWN_STRUCTURE'
        class Boundary:
            def __init__(self):self.calls=[];self.error=BoundaryError();self.returned=None
            def _contract_string(self,value,**kwargs):
                self.calls.append((value,kwargs))
                if value=='synthetic-rejected-value':raise self.error
                return value
            def _metric_signature(self,metric):
                self.returned={key:self._contract_string(value,identifier=True) for key,value in metric.items()}
                return self.returned
        original_string=Boundary._contract_string;original_signature=Boundary._metric_signature
        boundary=Boundary();observer=business_runner.BoundaryFailureObserver(Boundary,BoundaryError)
        with observer:
            result=boundary._metric_signature({'metric_id':'synthetic-safe-value'})
            self.assertIs(result,boundary.returned);self.assertEqual(len(boundary.calls),1)
            self.assertEqual(observer.events,[])
            with self.assertRaises(BoundaryError) as caught:
                boundary._metric_signature({'metric_id':'synthetic-rejected-value','subject_id':'synthetic-rejected-value'})
            self.assertIs(caught.exception,boundary.error)
        self.assertIs(Boundary._contract_string,original_string);self.assertIs(Boundary._metric_signature,original_signature)
        event=observer.report()['events'][0]
        self.assertEqual(event['field_status'],'ambiguous');self.assertFalse(event['matched_field_proven'])
        self.assertEqual(event['code'],'BOUNDARY_UNKNOWN_STRUCTURE')
        self.assertEqual(len(boundary.calls),2)
        self.assertNotIn('synthetic-rejected-value',json.dumps(observer.report()))

    def test_failure_observer_keeps_unknown_field_and_restores_guard_after_exception(self):
        class BoundaryError(Exception):code='BOUNDARY_UNKNOWN_STRUCTURE'
        class Boundary:
            def _contract_string(self,value,**kwargs):raise BoundaryError()
            def _metric_signature(self,metric):return self._contract_string('synthetic-not-in-six-fields')
        original=Boundary._contract_string;observer=business_runner.BoundaryFailureObserver(Boundary,BoundaryError)
        with self.assertRaises(BoundaryError):
            with observer:Boundary()._metric_signature({'metric_id':'synthetic-other-value'})
        self.assertIs(Boundary._contract_string,original)
        event=observer.report()['events'][0]
        self.assertEqual(event['field_status'],'unknown');self.assertEqual(event['candidate_fields'],[])
        row={'preflight':{'status':'passed'},'technical_completed':True,'before':{},'after':{},'result':{},
             'boundary_failure_observation':observer.report()}
        self.assertEqual(business_runner.limited_component_stop_reason(row),'limited_component_observed_boundary_failure')

    def test_v1_offline_blockers_cannot_be_erased_from_frozen_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);protocol={'source_sha256':{},'collection_readiness_blocks':[],
                'candidate_overlay_metadata':{'status':'offline_candidate_only','schema_version':'navren.jev-candidate-overlay.v3'},
                'method_contract':{'schema_version':'navren.jev-business-method.v1'}}
            with self.assertRaisesRegex(ValueError,'blockers_cannot_be_removed'):
                business_runner.verify_source(out,protocol)

    def test_limited_component_stops_technical_failure_but_not_semantic_sample_selection(self):
        row={'preflight':{'status':'passed'},'technical_completed':True,'before':{'balance':7},'after':{'balance':7},
             'result':{'bank_actions':[]},'independent_checks':{'semantic_pass':False,'status':'necessary_condition_failure'}}
        self.assertIsNone(business_runner.limited_component_stop_reason(row))
        row['technical_completed']=False
        self.assertEqual(business_runner.limited_component_stop_reason(row),'limited_component_technical_chain_failed')
        row['technical_completed']=True;row['after']['balance']=8
        self.assertEqual(business_runner.limited_component_stop_reason(row),'limited_component_protected_state_or_financial_action')

    def pilot10_record(self):
        path=ROOT/'outputs/jev-iteration-20261007/pilot-10-v5-component/trials/01-baseline-P08/record.json'
        return path,json.loads(path.read_text())

    def classify_p08_for_stop(self,row):
        row=deepcopy(row);verdict=classify(row,self.case('P08'))
        row.update(technical_completed=verdict['technical_completed'],independent_checks=verdict)
        return row

    def test_actual_pilot10_native_terminal_is_business_incomplete_without_rewriting_history(self):
        path,row=self.pilot10_record();before=path.read_bytes()
        self.assertEqual(row['stage_requests_sent'],{'primary':2,'reviewer':1,'jev':0})
        self.assertEqual(row['authoritative_tool_outputs'],[])
        self.assertEqual(row['independent_checks']['status'],'technical_failure')
        revised=self.classify_p08_for_stop(row);verdict=revised['independent_checks']
        self.assertTrue(verdict['technical_completed'])
        self.assertEqual(verdict['status'],'necessary_condition_failure')
        self.assertEqual(verdict['failed_checks'],row['independent_checks']['failed_checks'])
        self.assertIsNone(verdict['semantic_pass'])
        self.assertFalse(verdict['reviewer_approved_used_as_gold'])
        self.assertIsNone(business_runner.limited_component_stop_reason(revised))
        self.assertEqual(path.read_bytes(),before)

    def test_missing_or_failed_preview_is_incomplete_not_observed_serious_bottom_line_violation(self):
        _,row=self.pilot10_record()
        for outputs in ([],[{'name':'service_offer_preview','output':{'error':{'code':'OFFLINE_FIXTURE'}}}]):
            with self.subTest(outputs=outputs):
                row['authoritative_tool_outputs']=outputs;revised=self.classify_p08_for_stop(row)
                self.assertEqual(revised['independent_checks']['status'],'necessary_condition_failure')
                self.assertIn('两原候选与拒买底线',revised['independent_checks']['failed_checks'])
                self.assertIsNone(business_runner.limited_component_stop_reason(revised))

    def test_observed_preview_wrong_candidates_or_refusal_violation_remains_serious(self):
        _,row=self.pilot10_record();original=row['preflight']['oracle']['service_oracle']
        for mutation in ('offer_id','refusal','requires_investment'):
            with self.subTest(mutation=mutation):
                preview=deepcopy(original)
                if mutation=='offer_id':preview['offers'][0]['offer_id']='offline-wrong-candidate'
                elif mutation=='refusal':preview['conditions']['investment_purchase_refused']=False
                else:preview['offers'][0]['requires_investment_purchase']=True
                row['authoritative_tool_outputs']=[{'name':'service_offer_preview','output':{'data':preview}}]
                revised=self.classify_p08_for_stop(row)
                self.assertTrue(revised['technical_completed'])
                self.assertEqual(revised['independent_checks']['status'],'necessary_condition_failure')
                self.assertEqual(business_runner.limited_component_stop_reason(revised),
                    'limited_component_known_serious_rule_constraint_failure')
        row['authoritative_tool_outputs']=[{'name':'service_offer_preview','output':{'data':deepcopy(original)}}]
        revised=self.classify_p08_for_stop(row)
        self.assertNotIn('两原候选与拒买底线',revised['independent_checks']['failed_checks'])
        self.assertIsNone(business_runner.limited_component_stop_reason(revised))

    def test_genuine_terminal_transport_and_protected_state_failures_still_stop(self):
        _,original=self.pilot10_record()
        for mutation in ('fallback','runtime','contract','response_id','http'):
            with self.subTest(mutation=mutation):
                row=deepcopy(original)
                if mutation=='fallback':row['result']['isFallback']=True
                elif mutation=='runtime':row['result']['agent_runtime']['failure']={'code':'OFFLINE_FIXTURE'}
                elif mutation=='contract':row['result']['answer_contract']=None
                elif mutation=='response_id':row['result']['bank_response_id']=None
                else:row['http_status']=503
                revised=self.classify_p08_for_stop(row)
                self.assertEqual(revised['independent_checks']['status'],'technical_failure')
                self.assertEqual(business_runner.limited_component_stop_reason(revised),'limited_component_technical_chain_failed')
        for mutation in ('state','action','boundary'):
            with self.subTest(mutation=mutation):
                row=deepcopy(original)
                if mutation=='state':row['after']['offline_state_mutation']=True
                elif mutation=='action':row['result']['bank_actions']=[{'type':'offline-fixture-action'}]
                else:row['boundary_failure_observation']={'events':[{'code':'OFFLINE_FIXTURE'}]}
                revised=self.classify_p08_for_stop(row)
                expected=('limited_component_observed_boundary_failure' if mutation=='boundary'
                    else 'limited_component_protected_state_or_financial_action')
                self.assertEqual(business_runner.limited_component_stop_reason(revised),expected)

    def test_v2_freeze_single_new_pending_pilot_baseline_first_and_no_overwrite(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/'base';over=root/'overlay';evidence=root/'evidence';evidence.mkdir()
            rels=list(business_runner.V2_OVERLAY_SHA)+['evaluation/jev_business/cases.json','evaluation/jev_business/oracle.py']
            for name in rels:
                target=base/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
            shutil.copytree(ROOT/'outputs/jev-iteration-20261007/candidate-v3-overlay',over)
            out=root/'pilot-07-v4-component'
            with patch.object(business_runner,'ROOT',base),patch.object(business_runner,'V2_PILOT_OUT',out):
                method=self.v2_contract(evidence);path=evidence/'method.json';path.write_text(json.dumps(method))
                result=business_runner.freeze(out,ids=['P08'],repetitions=1,overlay=over,method_contract=path)
                self.assertEqual(result['trials'],2)
                protocol=json.loads((out/'protocol.json').read_text())
                self.assertEqual([x['variant'] for x in protocol['plan']],['baseline','jev'])
                self.assertEqual(protocol['limits'],{'total_tokens':200000,'estimated_cost_upper_cny':0.56})
                self.assertEqual(protocol['collection_readiness_blocks'],['method_v2_required_free_evidence_pending'])
                changed=deepcopy(protocol);changed['limits']['total_tokens']=4000000
                with self.assertRaisesRegex(ValueError,'frozen_scope_or_readiness_changed'):
                    business_runner.verify_source(out,changed)
                with self.assertRaisesRegex(ValueError,'offline_candidate_collection_blocked'):
                    business_runner.collect(out)
                self.assertFalse((out/'collection-start.json').exists())
                with self.assertRaisesRegex(ValueError,'refuse_overwrite_experiment'):
                    business_runner.freeze(out,ids=['P08'],repetitions=1,overlay=over,method_contract=path)

    def v4_fixture(self,root):
        """仅临时结构单测；合成审查包不能作为真实 v5 审批收据。"""
        import shutil
        base=root/'base';over=root/'overlay';stage=root/'evidence';stage.mkdir()
        targets={}
        for name in sorted(business_runner.V4_TARGETS):
            candidate=over/name;candidate.parent.mkdir(parents=True,exist_ok=True)
            if name in business_runner.V4_NEW_MODULES:
                content=b'# new local unit-test module\n';base_sha=None
            else:
                original=base/name;original.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(ROOT/name,original)
                content=original.read_bytes()+b'\n# local unit-test candidate\n';base_sha=business_runner.digest(original)
            candidate.write_bytes(content)
            targets[name]={'base_sha256':base_sha,'candidate_sha256':business_runner.digest(candidate)}
        for name in ['evaluation/jev_business/cases.json','evaluation/jev_business/oracle.py',
                     'scripts/run_jev_business_ab.py',*self.data['input_source_sha256']]:
            target=base/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
        metadata={'schema_version':'navren.jev-candidate-overlay.v5','status':'offline_candidate_only',
            'ready_for_paid_collection':False,'same_method_for_both_arms':True,'targets':targets}
        (over/'overlay.json').write_text(json.dumps(metadata))
        spec=deepcopy(json.loads((ROOT/'outputs/jev-iteration-20261007/v4-terminal-review-20261008/approved-spec.json').read_text()))
        spec.pop('offline_evidence',None)
        spec.update(schema_version='navren.jev-business-method.v4',approved_overlay_schema=metadata['schema_version'],
            approved_overlay_targets=targets,approved_overlay_sha256={name:target['candidate_sha256'] for name,target in targets.items()},
            approved_overlay_manifest_sha256=business_runner.digest(over/'overlay.json'),
            arms=deepcopy(business_runner.LIMITED_ARMS),original_question_text_sha256=business_runner.V4_P08_QUESTION_SHA)
        spec['collection_scope']['approved_output_dir']=str(root/'one-pair')
        method=self.v4_evidence(base,over,stage,spec)
        return base,over,stage,method

    def v4_evidence(self,base,over,stage,spec,*,patch_bytes=None,oracle_patch_bytes=None):
        """构造可篡改的免费 parser fixture；使用量/模型结果均非新真实调用。"""
        patch_bytes=patch_bytes if patch_bytes is not None else business_runner.canonical_v4_patch(base,over,spec['approved_overlay_targets'])
        (stage/'candidate.patch').write_bytes(patch_bytes)
        before=(ROOT/'outputs/jev-iteration-20261007/v5-runner-verification.before.py').read_bytes()
        (stage/'runner-before.py').write_bytes(before)
        (stage/'runner.patch').write_bytes(business_runner.canonical_v4_runner_patch(before,(base/'scripts/run_jev_business_ab.py').read_bytes()))
        spec.update(candidate_patch_sha256=business_runner.digest(stage/'candidate.patch'),
            runner_base_sha256=business_runner.V4_RUNNER_BASE_SHA,runner_sha256=business_runner.digest(base/'scripts/run_jev_business_ab.py'),
            runner_patch_sha256=business_runner.digest(stage/'runner.patch'))
        reviewed_names=['approved-spec-review.json','candidate.patch','proposal.md','runner.patch']
        if spec.get('outcome_classification_version')=='navren.business-outcome/2':
            oracle_before=(ROOT/'outputs/jev-iteration-20261007/v6-harness-before/evaluation/jev_business/oracle.py').read_bytes()
            (stage/'oracle-before.py').write_bytes(oracle_before)
            oracle_patch_bytes=(oracle_patch_bytes if oracle_patch_bytes is not None else
                business_runner.canonical_v4_oracle_patch(oracle_before,(base/'evaluation/jev_business/oracle.py').read_bytes()))
            (stage/'oracle.patch').write_bytes(oracle_patch_bytes)
            spec.update(oracle_base_sha256=business_runner.digest(stage/'oracle-before.py'),
                approved_oracle_sha256=business_runner.digest(base/'evaluation/jev_business/oracle.py'),
                oracle_patch_sha256=business_runner.digest(stage/'oracle.patch'))
            reviewed_names.append('oracle.patch')
        (stage/'approved-spec.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2))
        rendered=deepcopy(spec)
        rendered['shared_options']['单启动总预算']=rendered['shared_options'].pop('max_tokens')
        rendered['collection_limits']['双启动总预算']=rendered['collection_limits'].pop('total_tokens')
        (stage/'approved-spec-review.json').write_text(json.dumps(rendered,ensure_ascii=False,indent=2))
        (stage/'proposal.md').write_text('仅离线结构单测；不是模型审批。\n')
        artifacts=[]
        for name in reviewed_names:
            raw=(stage/name).read_text()
            artifacts.append({'sha256':business_runner.digest(stage/name),'redacted_lines':[],
                'content_with_line_numbers':'\n'.join(f'L{i:05d} {line}' for i,line in enumerate(raw.splitlines(),1))})
        payload={'task':'offline schema fixture only','candidate_artifacts':artifacts}
        artifact_sha=business_runner.hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        packet={**payload,'review_id':'offline-dry-run-independent-id','artifact_sha256':artifact_sha}
        (stage/'dry-run.json').write_text(json.dumps({'review_packet':packet},ensure_ascii=False))
        review=json.loads((ROOT/'outputs/jev-iteration-20261007/method-review-v4-20261008/review-attempt-2.json').read_text())
        review_id='offline-v4-parser-fixture-only'
        review.update(review_id=review_id,artifact_sha256=artifact_sha)
        for row in review['reviews'].values():row['review'].update(review_id=review_id,artifact_sha256=artifact_sha)
        (stage/'review-attempt-99.json').write_text(json.dumps(review,ensure_ascii=False))
        evidence={path.name:business_runner.digest(path) for path in stage.iterdir() if path.is_file()}
        return {**deepcopy(spec),'review_id':review_id,'review_artifact_sha256':artifact_sha,
            'review_spec_sha256':evidence['approved-spec.json'],'review_result_artifact':'review-attempt-99.json',
            'review_artifacts':evidence}

    def validate_v4(self,base,over,stage,method,**changes):
        args={'out':method['collection_scope']['approved_output_dir'],'case_ids':['P08'],'repetitions':1,
              'candidate_root':over,'base_root':base,'question_root':base}
        args.update(changes)
        return business_runner.validate_method_v4(method,stage,business_runner.validate_overlay(over,base),**args)

    def test_v5_overlay_exact_eight_targets_and_null_only_for_absent_new_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            self.assertEqual(set(business_runner.validate_overlay(over,base)['validated_targets']),business_runner.V4_TARGETS)
            path=base/'agent_runtime/terminal_metric_text.py';path.write_text('unexpected formal file\n')
            with self.assertRaisesRegex(ValueError,'new_module_base_must_be_absent'):
                business_runner.validate_overlay(over,base)
            path.unlink();metadata=json.loads((over/'overlay.json').read_text())
            metadata['targets']['agent_runtime/conversation.py']['base_sha256']=None
            (over/'overlay.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError,'new_module_base_must_be_absent'):
                business_runner.validate_overlay(over,base)

    def test_v4_parser_requires_complete_dual_review_and_not_fake_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            self.assertEqual(self.validate_v4(base,over,stage,method),[])
            path=stage/'review-attempt-99.json';path.write_text(json.dumps({'verdict':'PASS'}))
            method['review_artifacts'][path.name]=business_runner.digest(path)
            with self.assertRaisesRegex(ValueError,'actual_dual_review_pass_required'):
                self.validate_v4(base,over,stage,method)

    def outcome_v2_fixture(self,root):
        base,over,stage,_=self.v4_fixture(root)
        spec=json.loads((stage/'approved-spec.json').read_text())
        spec['outcome_classification_version']='navren.business-outcome/2'
        return base,over,stage,self.v4_evidence(base,over,stage,spec)

    def test_outcome_v2_binds_actual_oracle_diff_and_freezes_new_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base,over,stage,method=self.outcome_v2_fixture(root)
            self.assertEqual(self.validate_v4(base,over,stage,method),[])
            self.assertEqual(method['approved_oracle_sha256'],business_runner.digest(base/'evaluation/jev_business/oracle.py'))
            path=stage/'method.json';path.write_text(json.dumps(method));out=Path(method['collection_scope']['approved_output_dir'])
            with patch.object(business_runner,'ROOT',base):
                business_runner.freeze(out,ids=['P08'],repetitions=1,overlay=over,method_contract=path)
                protocol=json.loads((out/'protocol.json').read_text());business_runner.verify_source(out,protocol)
                self.assertEqual(protocol['method_contract']['outcome_classification_version'],'navren.business-outcome/2')
                self.assertEqual(protocol['limits'],business_runner.V2_COLLECTION_LIMITS)

    def test_outcome_v2_rejects_missing_oracle_proof_and_unknown_version(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.outcome_v2_fixture(Path(temp))
            for name in ('oracle-before.py','oracle.patch'):
                changed=deepcopy(method);changed['review_artifacts'].pop(name)
                with self.subTest(name=name),self.assertRaisesRegex(ValueError,'review_evidence_missing'):
                    self.validate_v4(base,over,stage,changed)
            spec=json.loads((stage/'approved-spec.json').read_text());spec['outcome_classification_version']='navren.business-outcome/unknown'
            changed=self.v4_evidence(base,over,stage,spec)
            with self.assertRaisesRegex(ValueError,'outcome_classification_version_invalid'):
                self.validate_v4(base,over,stage,changed)

    def test_outcome_v2_self_consistent_patch_or_source_tamper_cannot_replace_actual_oracle(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.outcome_v2_fixture(Path(temp))
            spec=json.loads((stage/'approved-spec.json').read_text())
            changed=self.v4_evidence(base,over,stage,spec,oracle_patch_bytes=(stage/'oracle.patch').read_bytes()+b'\n# false oracle diff\n')
            with self.assertRaisesRegex(ValueError,'actual_reviewed_oracle_diff_required'):
                self.validate_v4(base,over,stage,changed)
            method=self.v4_evidence(base,over,stage,spec)
            source=base/'evaluation/jev_business/oracle.py';source.write_bytes(source.read_bytes()+b'\n# unreviewed oracle mutation\n')
            with self.assertRaisesRegex(ValueError,'actual_reviewed_oracle_diff_required'):
                self.validate_v4(base,over,stage,method)

    def test_outcome_v2_review_packet_requires_full_unredacted_oracle_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.outcome_v2_fixture(Path(temp))
            path=stage/'dry-run.json';original=json.loads(path.read_text())
            for mutation in ('missing','redacted','content'):
                value=deepcopy(original);packet=value['review_packet'];items=packet['candidate_artifacts']
                item=next(x for x in items if x['sha256']==method['oracle_patch_sha256'])
                if mutation=='missing':items.remove(item)
                elif mutation=='redacted':item['redacted_lines']=[1]
                else:item['content_with_line_numbers']+='\nL99999 unreviewed content'
                payload={key:val for key,val in packet.items() if key not in ('review_id','artifact_sha256')}
                sha=business_runner.hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                packet['artifact_sha256']=sha;path.write_text(json.dumps(value))
                changed=deepcopy(method);changed['review_artifact_sha256']=sha;changed['review_artifacts'][path.name]=business_runner.digest(path)
                with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'review_must_cover_unredacted_candidate:oracle.patch'):
                    self.validate_v4(base,over,stage,changed)

    def test_v4_actual_usage_coverage_route_and_attempts_cannot_be_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            path=stage/'review-attempt-99.json';original=json.loads(path.read_text())
            for kind in ('usage','coverage','route','retry','blocking'):
                with self.subTest(kind=kind):
                    review=deepcopy(original);entry=review['reviews']['glm_5_3_flash']
                    if kind=='usage':entry['review']['review_metadata']['usage']=None
                    elif kind=='coverage':entry['review']['coverage']='partial'
                    elif kind=='route':entry['review']['review_metadata']['endpoint']='https://must-not-call.example/api'
                    elif kind=='retry':entry['attempts']=2
                    else:entry['review']['blocking_issues']=[{'severity':'major','reason':'offline fixture'}]
                    path.write_text(json.dumps(review));method['review_artifacts'][path.name]=business_runner.digest(path)
                    with self.assertRaises(ValueError):self.validate_v4(base,over,stage,method)

    def test_v4_patch_receipt_and_spec_cannot_self_consistently_replace_actual_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            spec=json.loads((stage/'approved-spec.json').read_text())
            method=self.v4_evidence(base,over,stage,spec,patch_bytes=(stage/'candidate.patch').read_bytes()+b'\n# self-consistent receipt mutation\n')
            with self.assertRaisesRegex(ValueError,'reviewed_patch_does_not_match_sources'):
                self.validate_v4(base,over,stage,method)

    def test_v4_reviewed_scope_options_stops_channels_arms_and_question_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            original=json.loads((stage/'approved-spec.json').read_text())
            for kind in ('options','limits','stop','channels','arms','scope','question'):
                with self.subTest(kind=kind):
                    spec=deepcopy(original)
                    if kind=='options':spec['shared_options']['max_tokens']=100001
                    elif kind=='limits':spec['collection_limits']['total_tokens']=4000000
                    elif kind=='stop':spec['stop_conditions']=spec['stop_conditions'][:-1]
                    elif kind=='channels':spec['channels']['primary']='official'
                    elif kind=='arms':spec['arms']['baseline']['jev_policy']='read_plan'
                    elif kind=='scope':spec['collection_scope']['baseline_first']=False
                    else:spec['original_question_text_sha256']='0'*64
                    changed=self.v4_evidence(base,over,stage,spec)
                    with self.assertRaisesRegex(ValueError,'only_reviewed_limited|original_question_changed'):
                        self.validate_v4(base,over,stage,changed)

    def test_v4_wrong_question_repeat_regression_and_baseline_only_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            for changes in ({'case_ids':['P01']},{'repetitions':2},{'regression':True},{'baseline_pilot_only':True},
                            {'out':str(Path(temp)/'another-output')}):
                with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'only_reviewed_limited'):
                    self.validate_v4(base,over,stage,method,**changes)

    def test_v4_target_base_hash_and_candidate_hash_bind_spec_both_directions(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            for field in ('base_sha256','candidate_sha256'):
                changed=deepcopy(method);changed['approved_overlay_targets']['agent_runtime/conversation.py'][field]='0'*64
                with self.subTest(field=field),self.assertRaisesRegex(ValueError,'approved_spec_changed'):
                    self.validate_v4(base,over,stage,changed)
            spec=json.loads((stage/'approved-spec.json').read_text())
            spec['approved_overlay_manifest_sha256']='0'*64
            changed=self.v4_evidence(base,over,stage,spec)
            with self.assertRaisesRegex(ValueError,'exact_reviewed_overlay'):
                self.validate_v4(base,over,stage,changed)

    def test_v4_review_must_include_unredacted_full_runner_and_candidate_originals(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            dry=stage/'dry-run.json';value=json.loads(dry.read_text());packet=value['review_packet']
            packet['candidate_artifacts'][-1]['redacted_lines']=[1]
            payload={key:val for key,val in packet.items() if key not in ('review_id','artifact_sha256')}
            sha=business_runner.hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            packet['artifact_sha256']=sha;dry.write_text(json.dumps(value));method['review_artifact_sha256']=sha
            method['review_artifacts'][dry.name]=business_runner.digest(dry)
            with self.assertRaisesRegex(ValueError,'review_must_cover_unredacted_candidate:runner.patch'):
                self.validate_v4(base,over,stage,method)

    def test_v4_runner_diff_must_reproduce_actual_saved_baseline_and_frozen_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            path=stage/'runner.patch';path.write_bytes(path.read_bytes()+b'\n# false runner diff\n')
            spec=json.loads((stage/'approved-spec.json').read_text());spec['runner_patch_sha256']=business_runner.digest(path)
            (stage/'approved-spec.json').write_text(json.dumps(spec))
            method['runner_patch_sha256']=spec['runner_patch_sha256'];method['review_spec_sha256']=business_runner.digest(stage/'approved-spec.json')
            method['review_artifacts']['approved-spec.json']=method['review_spec_sha256'];method['review_artifacts'][path.name]=business_runner.digest(path)
            with self.assertRaisesRegex(ValueError,'actual_reviewed_runner_diff_required'):
                self.validate_v4(base,over,stage,method)

    def test_v4_freeze_verify_exact_one_pair_and_rejects_added_module_or_changed_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base,over,stage,method=self.v4_fixture(root)
            path=stage/'method.json';path.write_text(json.dumps(method));out=Path(method['collection_scope']['approved_output_dir'])
            with patch.object(business_runner,'ROOT',base):
                business_runner.freeze(out,ids=['P08'],repetitions=1,overlay=over,method_contract=path)
                protocol=json.loads((out/'protocol.json').read_text());business_runner.verify_source(out,protocol)
                self.assertEqual(protocol['plan'],[{'case_id':'P08','repetition':1,'variant':arm} for arm in ('baseline','jev')])
                self.assertEqual(protocol['limits'],business_runner.V2_COLLECTION_LIMITS)
                self.assertEqual(protocol['collection_readiness_blocks'],[])
                self.assertEqual(len(protocol['overlay_base_sha256']),6)
                with self.assertRaisesRegex(ValueError,'refuse_overwrite_experiment'):
                    business_runner.freeze(out,ids=['P08'],repetitions=1,overlay=over,method_contract=path)
                added=out/'source/agent_runtime/extra.py';added.write_text('# undeclared runtime module\n')
                with self.assertRaisesRegex(ValueError,'frozen_source_file_set_changed'):
                    business_runner.verify_source(out,protocol)
                added.unlink();old=out/'overlay-base/agent_runtime/conversation.py';old.write_bytes(old.read_bytes()+b'# baseline changed\n')
                with self.assertRaisesRegex(ValueError,'base_source_changed'):
                    business_runner.verify_source(out,protocol)

    def test_v4_embedded_version_cannot_remove_blockers_without_frozen_review_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            protocol={'source_sha256':{},'collection_readiness_blocks':[],
                'candidate_overlay_metadata':{'schema_version':'navren.jev-candidate-overlay.v5','status':'offline_candidate_only'},
                'method_contract':{'schema_version':'navren.jev-business-method.v4'}}
            with self.assertRaisesRegex(ValueError,'frozen_method_receipt_required'):
                business_runner.verify_source(Path(temp),protocol)

    def test_v4_frozen_protocol_cannot_change_plan_channels_limits_or_candidate_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            base,over,stage,method=self.v4_fixture(Path(temp))
            path=stage/'method.json';path.write_text(json.dumps(method));out=Path(method['collection_scope']['approved_output_dir'])
            with patch.object(business_runner,'ROOT',base):
                business_runner.freeze(out,ids=['P08'],repetitions=1,overlay=over,method_contract=path)
                original=json.loads((out/'protocol.json').read_text())
                for kind in ('plan','channels','limits','candidate','arms','scope'):
                    with self.subTest(kind=kind):
                        protocol=deepcopy(original)
                        if kind=='plan':protocol['plan'].reverse()
                        elif kind=='channels':protocol['primary_provider']='official'
                        elif kind=='limits':protocol['limits']['total_tokens']=4000000
                        elif kind=='candidate':protocol['candidate_overlay_sha256']['agent_runtime/conversation.py']='0'*64
                        elif kind=='arms':protocol['arms']['baseline']['jev_policy']='read_plan'
                        else:protocol['collection_scope']['baseline_first']=False
                        with self.assertRaisesRegex(ValueError,'frozen_scope_or_readiness_changed'):
                            business_runner.verify_source(out,protocol)

if __name__=='__main__':unittest.main()
