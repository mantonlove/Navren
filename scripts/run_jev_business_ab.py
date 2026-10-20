#!/usr/bin/env python3
"""有出处业务开发对照：冻结、无云预检、真实 HTTP 采集，保留全部首次结果。"""
from __future__ import annotations
import argparse
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
import difflib
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_jev_ab import Recorder, digest, paired_plan, write
from evaluation.jev_business.catalog import load_dataset, selected_cases

DEFAULT_PUBLIC = Path(os.environ.get('NAVREN_PUBLIC_KNOWLEDGE_DB') or ROOT/'data/public-knowledge.sqlite3')

# 这是一笔有实际双审原件的有限阶段迁移，不是通用 PASS 旗标豁免。
V2_REVIEW_ID = 'e44ec998-61b0-47d6-9f7f-9a53b6d95f68'
V2_ARTIFACT_SHA = '8946ad9607fa9381a68b84f2cf4abd875d8970a9d30cc870192041a45259d616'
V2_REVIEW_ARTIFACTS = {
    'review-attempt-2.json':'e5783a92b63257a06f1f6c79a258755bd148e64e447befab268859b27baaff8e',
    'proposal-frozen.md':'afde787a83cf8901b5b2f3be2c03c133614b230f204b00cd45cf8c99970c57d3',
    'candidate-source-review.md':'365315598be44cdec80cd33628d91f5fcf87ec14eb1e13202304370beeeec4f4',
    'dry-run.json':'ab9fe59a505a67d3814c18f300c3d26b1f8db70d5cd17d980b7beda6c6df06ea',
}
V2_OVERLAY_SHA = {
    'agent_runtime/conversation.py':'cab4da9b8b22109d25eaffa6b8becee26cc6d0ba2e53c2dd188c0c78d502cd42',
    'agent_runtime/jev_plans.py':'11965a2e07f2634a1ef3a43d7891d7b8d41f7a99e022929cdd4e11de0065f6b3',
    'skills/bank-service/SKILL.md':'e8928d59e3efe861bf147add1aca313b1b7afd49dd38a39e579574829b054b76',
}
V2_PILOT_OUT = ROOT/'outputs/jev-iteration-20261007/pilot-07-v4-component'
V2_POST_PREVIEW_PROTOCOL_SHA = 'dd8469da86ea52e46847c11056f9dd8e61b30bbe127ed0eedf3b019d3dbc6e3b'
V2_POST_PREVIEW_SHA = '72d2bf4b8753d3ea49d2adbf28316d2545152408bb6ed3fdb1c8748487c53fd4'
V2_BOUNDARY_DIAGNOSTIC_SHA = '47d17ec3a6bcbf587a9c251e7cdc2529062e97ab0c8121b7e1633cdb606ebaa0'
V2_OLD_RESULTS_SHA = '635e72dd6007efbda55f496597d7ac8da00721caeee313dcfb5308cad8c16f45'
SHARED_OPTIONS = {'tool_routing':'progressive','timeout_seconds':105,'max_tokens':100000,'max_rounds':8,'max_tools':12}
CHANNELS = {'primary':'ai_ping','reviewer':'ai_ping','jev':'typesafe_official'}
V2_COLLECTION_LIMITS = {'total_tokens':200000,'estimated_cost_upper_cny':0.56}
V2_BOUNDARY_REVIEW_ID = 'a360679f-363c-4e07-97cf-a7e4e008c2fa'
V2_BOUNDARY_ARTIFACT_SHA = 'e2f9e548be1f84c4212f8d8956cd5170f86167ab983e36dbca995338a01caf78'
V2_BOUNDARY_REVIEW_ARTIFACTS = {
    'supplemental-boundary-review.json':'351b37f391d9492c90e5853cda9d44bdf805641dd378a251f84a11eac67ae847',
    'supplemental-boundary-proposal.md':'f60898b5156112be2b406d42539c85bd0a018bb87ebb8d3b9a38bdcf159c8b68',
    'supplemental-boundary-dry-run.json':'8ee5503519359a1a5252c100992565b520eaa841b381df952fc3ca09e05c35a0',
    'supplemental-boundary-disposition.json':'486666f88febbc616d59a967d67bd86638eb98c61b62a02fa6d0c16f41aa385e',
}
V2_STOP_CONDITIONS = ['unknown_usage_or_cost','boundary_or_budget_or_final_review_structure_failure',
    'fixture_runner_source_or_cleanup_invalid','protected_state_or_financial_action','known_serious_rule_constraint_failure']
V3_SPEC_SHA = '4c59e42f372fd4b20bcbec981a64821c998644761cbbf4879ddc8cf5ec55a33a'
V3_PATCH_SHA = '8355b1d5b30c2a8d9fad37c3b3fde65fa8fbeeeb1c458310584334c6f07f07ac'
V4_TARGETS = frozenset({'agent_runtime/conversation.py','agent_runtime/jev_plans.py',
    'agent_runtime/bank_tools.py','agent_runtime/data_boundary.py','agent_runtime/service_metric_text.py',
    'agent_runtime/terminal_metric_text.py','skills/bank-service/SKILL.md','base_runtime/reply_interaction.py'})
V4_NEW_MODULES = frozenset({'agent_runtime/service_metric_text.py','agent_runtime/terminal_metric_text.py'})
LIMITED_METHOD_SCHEMAS = ('navren.jev-business-method.v2','navren.jev-business-method.v3','navren.jev-business-method.v4')
LIMITED_ARMS = {'baseline':{'jev_policy':'off'},'jev':{'jev_policy':'read_plan'}}
V4_SELECTION = {'policy_version':'jev-finite-read-plan/2','initial_probability':0.75,
                'initial_confidence':0.6,'status':'development_initial_not_calibrated'}
V4_P08_QUESTION_SHA = 'ed100e97eac87c55a3cb6e7552366adc8a40f84da37a085a611e94051ece6c0d'
V4_RUNNER_BASE_SHA = 'deacf243f028c75c633ce29dee4bcede23617aa27fb7df57e8c5ee1603f45dc6'

def same_json(actual,expected):
    # bool 与 0/1、整数与浮点在 Python 中可能相等，冻结合同需保持 JSON 类型。
    return json.dumps(actual,sort_keys=True,allow_nan=False)==json.dumps(expected,sort_keys=True,allow_nan=False)

def checked_evidence(path, name, expected):
    if (not isinstance(name,str) or Path(name).name!=name or not isinstance(expected,str)
            or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected)):
        raise ValueError('method_review_evidence_invalid_name_or_sha')
    target=Path(path)/name
    if target.is_symlink() or not target.is_file() or digest(target)!=expected:
        raise ValueError('method_review_evidence_changed:'+name)
    return target

def validate_method_v2_review(method, evidence_root):
    """校验实际原件的双路完成、覆盖、渠道与完整计量，而非聚合 PASS 一字段。"""
    if method.get('review_id')!=V2_REVIEW_ID or method.get('review_artifact_sha256')!=V2_ARTIFACT_SHA:
        raise ValueError('method_v2_actual_review_identity_required')
    hashes=method.get('review_artifacts',{})
    for name, expected in V2_REVIEW_ARTIFACTS.items():
        if hashes.get(name)!=expected:
            raise ValueError('method_v2_actual_review_sha_required:'+name)
        checked_evidence(evidence_root,name,expected)
    packet=json.loads((Path(evidence_root)/'dry-run.json').read_text())['review_packet']
    # dry-run 是独立组包操作，review_id 可不同；受审内容摘要必须相同。
    if packet.get('artifact_sha256')!=V2_ARTIFACT_SHA:
        raise ValueError('method_v2_review_packet_identity_mismatch')
    review=json.loads((Path(evidence_root)/'review-attempt-2.json').read_text())
    validate_actual_dual_review(review,V2_REVIEW_ID,V2_ARTIFACT_SHA)

def validate_actual_dual_review(review,review_id,artifact_sha):
    models={'deepseek_flash':'DeepSeek-V4.1-Flash','glm_5_3_flash':'GLM-5.3-Flash'}
    if (review.get('verdict')!='PASS' or review.get('review_id')!=review_id
            or review.get('artifact_sha256')!=artifact_sha
            or set(review.get('reviewers_required',[]))!=set(models)
            or review.get('reviewers_completed')!=2 or set(review.get('reviews',{}))!=set(models)
            or review.get('resumed_reviewers')!=[]):
        raise ValueError('method_v2_actual_dual_review_pass_required')
    for ident,model in models.items():
        item=review['reviews'][ident];result=item.get('review',{});meta=result.get('review_metadata',{})
        confidence=result.get('confidence')
        if (item.get('status')!='COMPLETED' or item.get('verdict')!='PASS'
                or type(item.get('attempts')) is not int or item['attempts']!=1
                or result.get('verdict')!='PASS' or result.get('coverage')!='complete'
                or type(confidence) not in (int,float) or not math.isfinite(confidence) or not .75<=confidence<=1
                or result.get('review_id')!=review_id or result.get('artifact_sha256')!=artifact_sha
                or not isinstance(result.get('blocking_issues'),list) or result['blocking_issues']):
            raise ValueError('method_v2_reviewer_completion_or_coverage_invalid:'+ident)
        route=review.get('review_configuration',{}).get('reviewers',{}).get(ident,{})
        expected_route={'provider':'Aiping OpenAI-compatible API','endpoint':'https://aiping.cn/api/v1/chat/completions',
                        'requested_model':model}
        if (any(meta.get(k)!=v or route.get('primary',{}).get(k)!=v for k,v in expected_route.items())
                or meta.get('returned_model')!=model or meta.get('route')!='primary'
                or route.get('fallback') is not None or not meta.get('response_id')
                or meta.get('finish_reason')!='stop'):
            raise ValueError('method_v2_actual_aiping_route_required:'+ident)
        usage=meta.get('usage')
        if (not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0
                for k in ('prompt_tokens','completion_tokens','total_tokens'))
                or usage['prompt_tokens']<=0 or usage['completion_tokens']<=0
                or usage['total_tokens']!=usage['prompt_tokens']+usage['completion_tokens']):
            raise ValueError('method_v2_complete_review_usage_required:'+ident)

def validate_method_v2_free_evidence(method,evidence_root):
    required=method.get('required_free_evidence',{})
    if required.get('status')=='pending':return ['method_v2_required_free_evidence_pending']
    if required.get('status')!='completed_with_limits':
        raise ValueError('method_v2_required_free_evidence_status_invalid')
    post=required.get('post_preview',{})
    if (post.get('artifact')!='v3-post-preview-admission-probe.json' or post.get('sha256')!=V2_POST_PREVIEW_SHA
            or post.get('frozen_protocol_sha256')!=V2_POST_PREVIEW_PROTOCOL_SHA
            or method.get('review_artifacts',{}).get(post['artifact'])!=post['sha256']):
        raise ValueError('method_v2_actual_post_preview_evidence_required')
    proof=json.loads(checked_evidence(evidence_root,post['artifact'],post['sha256']).read_text())
    if (proof.get('scope')!='frozen_source_budget_offline_recalculation_not_full_host_acceptance'
            or proof.get('frozen_protocol_sha256')!=V2_POST_PREVIEW_PROTOCOL_SHA
            or proof.get('probe_executed_as_expected') is not True
            or proof.get('no_closure_frame_or_core_variable_modification') is not True
            or not proof.get('checks') or any(v is not True for v in proof['checks'].values())
            or proof.get('guard_attempts')!=[]):
        raise ValueError('method_v2_post_preview_scope_or_guards_invalid')
    for key in ('paid_calls','network_requests','credential_loads','actual_model_responses_returned'):
        if type(proof.get(key)) is not int or proof[key]!=0:
            raise ValueError('method_v2_post_preview_must_be_free:'+key)
    for key in ('full_host_post_preview_admission_verified','actual_final_answer_tested',
                'exact_actual_review_packet_tested','real_speed_cost_or_accuracy_verified',
                'model_collection_gate_released','ready_for_paid_collection'):
        if proof.get(key) is not False:raise ValueError('method_v2_post_preview_limits_must_be_preserved:'+key)
    arms=proof.get('arms')
    if not isinstance(arms,list) or len(arms)!=2 or {r.get('arm') for r in arms}!={'baseline','jev'}:
        raise ValueError('method_v2_both_free_preview_arms_required')
    question=next(c['question'] for c in load_dataset()['cases'] if c['id']=='P08')
    for arm in arms:
        if (arm.get('question')!=question or arm.get('limits')!=SHARED_OPTIONS
                or arm.get('paid_modes')!={'primary':'paid','reviewer':'paid'}
                or arm.get('preview_verified_against_original_backend') is not True
                or arm.get('preview_draft_count')!=1 or arm.get('application_count')!=0
                or arm.get('protected_state_unchanged') is not True or arm.get('changed_protected_sections')!=[]
                or arm.get('arranged_assistant_messages_are_model_outputs') is not False
                or arm.get('actual_model_responses_returned')!=0 or arm.get('reviewer_review_invocations')!=0):
            raise ValueError('method_v2_original_free_preview_receipts_required')
        receipts=arm.get('actual_tool_receipts',[])
        previews=[r for r in receipts if r.get('name')=='service_offer_preview']
        if (len(previews)!=1 or previews[0].get('data_status')!='previewed'
                or previews[0].get('metric_count')!=6
                or any(r.get('original_super_return_unchanged') is not True for r in receipts)):
            raise ValueError('method_v2_original_free_preview_receipts_required')
        scenarios=arm.get('source_recalculations',[])
        if len(scenarios)!=2:raise ValueError('method_v2_both_budget_assumptions_required')
        indexed={r.get('scenario'):r for r in scenarios}
        for scenario in ('optimistic_prior_usage_zero','conservative_arranged_prior_wire_plus_output_bounds'):
            calculation=indexed.get(scenario,{})
            admission=calculation.get('original_admission',{});reserve=admission.get('reserve_details',{})
            if (calculation.get('frozen_source_ast_executed') is not True
                    or calculation.get('prior_usage_is_actual') is not False
                    or admission.get('stage')!='final' or admission.get('bound_method')!='full_wire_utf8_estimate'
                    or reserve.get('metric_count')!=12 or reserve.get('claim_limit')!=24
                    or reserve.get('worst_candidate_fully_reserved') is not False
                    or calculation.get('all_possible_answers_fit_proven') is not False
                    or calculation.get('exact_reviewer_after_actual_final_tested') is not False
                    or calculation.get('full_host_loop_after_preview_tested') is not False):
                raise ValueError('method_v2_budget_reconstruction_limits_required')
            if scenario=='optimistic_prior_usage_zero':
                minimum=admission.get('exploration_preflight',{}).get('minimum_review_input')
                cap=admission.get('review_input_cap')
                if (calculation.get('prior_usage_assumption')!=0 or admission.get('accepted') is not True
                        or type(minimum) is not int or type(cap) is not int or not 0<minimum<=cap
                        or reserve.get('worst_case_input_bytes',0)<=cap):
                    raise ValueError('method_v2_minimum_review_space_required')
            elif (admission.get('accepted') is not False
                    or calculation.get('admission_failure',{}).get('code')!='AGENT_BUDGET_EXCEEDED'):
                raise ValueError('method_v2_conservative_rejection_must_be_preserved')
    diagnostic=required.get('boundary_diagnostic',{})
    if diagnostic.get('status')=='pending':return ['method_v2_boundary_diagnostic_pending']
    if diagnostic.get('status')!='completed_with_limits':
        raise ValueError('method_v2_boundary_diagnostic_required')
    # 诊断须另有真实免费原件；其具体结论不能由合同里的状态代替。
    artifact=diagnostic.get('artifact');expected=diagnostic.get('sha256')
    if (artifact!='v3-boundary-diagnostic.json' or expected!=V2_BOUNDARY_DIAGNOSTIC_SHA
            or method.get('review_artifacts',{}).get(artifact)!=expected):
        raise ValueError('method_v2_boundary_diagnostic_hash_not_frozen')
    checked_evidence(evidence_root,artifact,expected)
    pending=validate_method_v2_boundary_diagnostic(json.loads((Path(evidence_root)/artifact).read_text()))
    return validate_method_v2_boundary_disposition(method,evidence_root,pending)

def validate_method_v2_boundary_disposition(method,evidence_root,pending):
    disposition=method.get('required_free_evidence',{}).get('boundary_disposition',{})
    if not disposition or disposition.get('status')=='pending':return pending
    expected={'status':'independently_accepted_for_one_limited_pilot','review_id':V2_BOUNDARY_REVIEW_ID,
        'review_artifact_sha256':V2_BOUNDARY_ARTIFACT_SHA,'scope':'one_P08_baseline_first_pair_only',
        'original_boundary_failure_resolved':False,'diagnostic_status':'not_reproduced_unresolved',
        'formal_adoption_approved':False,'performance_pass':False}
    if (any(not same_json(disposition.get(k),v) for k,v in expected.items())
            or disposition.get('review_artifacts')!=V2_BOUNDARY_REVIEW_ARTIFACTS):
        raise ValueError('method_v2_actual_limited_boundary_disposition_required')
    for name,sha in V2_BOUNDARY_REVIEW_ARTIFACTS.items():
        if method.get('review_artifacts',{}).get(name)!=sha:
            raise ValueError('method_v2_actual_supplemental_review_sha_required:'+name)
        checked_evidence(evidence_root,name,sha)
    review=json.loads((Path(evidence_root)/'supplemental-boundary-review.json').read_text())
    validate_actual_dual_review(review,V2_BOUNDARY_REVIEW_ID,V2_BOUNDARY_ARTIFACT_SHA)
    dry=json.loads((Path(evidence_root)/'supplemental-boundary-dry-run.json').read_text())['review_packet']
    if dry.get('artifact_sha256')!=V2_BOUNDARY_ARTIFACT_SHA:
        raise ValueError('method_v2_supplemental_review_packet_identity_mismatch')
    actual=json.loads((Path(evidence_root)/'supplemental-boundary-disposition.json').read_text())
    extra={'schema_version':'navren.jev-boundary-disposition.v1','collection_scope':method['collection_scope'],
        'limits':V2_COLLECTION_LIMITS,'channels':CHANNELS,'stop_conditions':V2_STOP_CONDITIONS,
        'ordinary_semantic_error_is_not_sample_selection':True,'mechanical_serious_errors_only_before_next_arm':True,
        'unknown_failure_fields_remain_unknown':True,'original_first_failure_fixed':False,
        'root_cause_identified':False,'production_gate_released':False,'performance_gate_released':False}
    if any(not same_json(actual.get(k),v) for k,v in {**expected,**extra}.items()):
        raise ValueError('method_v2_frozen_boundary_disposition_scope_mismatch')
    return []

def validate_method_v2_boundary_diagnostic(proof):
    if proof.get('schema_version')!=1 or proof.get('scope')!='frozen_boundary_observation_only':
        raise ValueError('method_v2_boundary_diagnostic_scope_invalid')
    for key in ('paid_calls','network_requests','credential_loads','actual_model_responses_returned'):
        if type(proof.get(key)) is not int or proof[key]!=0:
            raise ValueError('method_v2_boundary_diagnostic_must_be_free:'+key)
    for key in ('original_failure_marked_fixed','model_collection_gate_released',
                'ready_for_paid_collection','full_host_post_preview_admission_verified'):
        if proof.get(key) is not False:raise ValueError('method_v2_boundary_diagnostic_limits_required:'+key)
    original=proof.get('original_failure_preserved',{})
    if (original.get('actual_failed_field_known') is not False
            or original.get('exception',{}).get('type')!='BoundaryError'
            or original.get('failing_source_line')!=1365):
        raise ValueError('method_v2_original_boundary_failure_must_be_preserved')
    if (proof.get('source_and_probe_sha256',{}).get('frozen_protocol_sha256')!=V2_POST_PREVIEW_PROTOCOL_SHA
            or proof.get('source_and_probe_sha256',{}).get('previous_result_sha256')!=V2_POST_PREVIEW_SHA
            or type(proof.get('fresh_fixture_slots_upper_bound')) is not int
            or not 0<proof['fresh_fixture_slots_upper_bound']<=10
            or proof.get('maximum_fresh_fixtures')!=10 or not proof.get('attempts')):
        raise ValueError('method_v2_actual_limited_boundary_diagnostic_required')
    observer=proof.get('observer_contract',{})
    expected={'original_arguments_forwarded_unchanged':True,'original_returns_and_exceptions_forwarded_unchanged':True,
        'actual_boundary_extra_projection_calls':0,'failure_entry_copy_is_inert_until_original_rejection':True,
        'frames_or_closures_read':False,'guard_or_core_variables_modified':False}
    if observer!=expected:raise ValueError('method_v2_boundary_observation_only_required')
    for attempt in proof['attempts']:
        if (any(type(attempt.get(k)) is not int or attempt[k]!=0 for k in ('paid_calls','network_requests','credential_loads'))
                or attempt.get('guard_attempts',[])!=[]
                or any(a.get('protected_state_unchanged') is not True for a in attempt.get('arms',[]))):
            raise ValueError('method_v2_boundary_diagnostic_guards_invalid')
    # 定位一次条件、重复未复现或报告自己写 completed，都不能声称原异常已解决。
    status=proof.get('diagnostic_status')
    if status not in ('root_cause_identified','reproduced_unlocalized','not_reproduced_unresolved','diagnostic_failed'):
        raise ValueError('method_v2_boundary_diagnostic_status_invalid')
    return ['method_v2_boundary_disposition_pending']

def validate_method_v2(method, evidence_root, overlay_metadata, *, out, case_ids, repetitions,
                       regression=False, baseline_pilot_only=False):
    if (method.get('schema_version')!='navren.jev-business-method.v2'
            or method.get('status')!='method_pass_only_not_implementation_or_performance'
            or not same_json(method.get('channels'),CHANNELS) or not same_json(method.get('shared_options'),SHARED_OPTIONS)):
        raise ValueError('method_v2_reviewed_contract_required')
    scope={'kind':'limited_component_pilot','case_ids':['P08'],'repetitions_per_arm':1,
           'arms':['baseline','jev'],'approved_output_dir':str(V2_PILOT_OUT.resolve()),'max_freezes':1,
           'baseline_first':True,'baseline_technical_or_accounting_failure_stops':True}
    if (not same_json(method.get('collection_scope'),scope) or Path(out).resolve()!=V2_PILOT_OUT.resolve()
            or case_ids!=['P08'] or type(repetitions) is not int or repetitions!=1
            or regression or baseline_pilot_only):
        raise ValueError('method_v2_only_one_limited_P08_component_pilot')
    if (not isinstance(overlay_metadata,dict)
            or overlay_metadata.get('schema_version')!='navren.jev-candidate-overlay.v3'
            or overlay_metadata.get('status')!='offline_candidate_only'
            or overlay_metadata.get('ready_for_paid_collection') is not False
            or overlay_metadata.get('same_method_for_both_arms') is not True
            or method.get('approved_overlay_sha256')!=V2_OVERLAY_SHA
            or {k:v.get('candidate_sha256') for k,v in overlay_metadata.get('validated_targets',{}).items()}!=V2_OVERLAY_SHA):
        raise ValueError('method_v2_exact_reviewed_v3_overlay_required')
    expected_stage={'prior_pilot':'pilot-04-readplans','prior_pilot_permanently_closed':True,
        'prior_sent_usage_status':'unverified','prior_full_cost_status':'unknown','historical_accounting_resolved':False,
        'old_results_sha256':V2_OLD_RESULTS_SHA,'new_stage_excludes_prior_trials':True,'no_retry_of_prior_request':True}
    if not same_json(method.get('stage_disposition'),expected_stage):
        raise ValueError('method_v2_old_stage_closed_unknown_retained_required')
    validate_method_v2_review(method,evidence_root)
    return validate_method_v2_free_evidence(method,evidence_root)

def validate_method_v3_patch(evidence_root, evidence):
    # 差异原件与四目标分别锁定；补丁不能仅凭自身与收据自洽借用受审范围。
    if evidence.get('candidate.patch') != V3_PATCH_SHA:
        raise ValueError('method_v3_reviewed_patch_sha_required')
    return checked_evidence(evidence_root,'candidate.patch',V3_PATCH_SHA)

def validate_method_v3(method, evidence_root, overlay_metadata, *, out, case_ids, repetitions,
                       regression=False, baseline_pilot_only=False):
    """新终结修复只放行锁定规格的一次 pair；实际双审必须绑定完整规格。"""
    spec_path=checked_evidence(evidence_root,'approved-spec.json',V3_SPEC_SHA)
    spec=json.loads(spec_path.read_text())
    if any(not same_json(method.get(k),v) for k,v in spec.items()):
        raise ValueError('method_v3_approved_spec_changed')
    scope=spec['collection_scope']
    if (Path(out).resolve()!=Path(scope['approved_output_dir']).resolve() or case_ids!=['P08']
            or type(repetitions) is not int or repetitions!=1 or regression or baseline_pilot_only
            or not same_json(spec['shared_options'],SHARED_OPTIONS)
            or not same_json(spec['collection_limits'],V2_COLLECTION_LIMITS)
            or not same_json(spec['channels'],CHANNELS)):
        raise ValueError('method_v3_only_reviewed_limited_P08_pair')
    if (not isinstance(overlay_metadata,dict)
            or overlay_metadata.get('schema_version')!=spec['approved_overlay_schema']
            or overlay_metadata.get('status')!='offline_candidate_only'
            or overlay_metadata.get('same_method_for_both_arms') is not True
            or overlay_metadata.get('ready_for_paid_collection') is not False
            or {k:v.get('candidate_sha256') for k,v in overlay_metadata.get('validated_targets',{}).items()}
                !=spec['approved_overlay_sha256']):
        raise ValueError('method_v3_exact_reviewed_overlay_required')
    evidence=method.get('review_artifacts',{})
    required={'approved-spec.json','approved-spec-review.json','candidate.patch','proposal.md','dry-run.json','review-attempt-4.json',
              spec['offline_evidence']['artifact']}
    if not required.issubset(evidence):raise ValueError('method_v3_review_evidence_missing')
    validate_method_v3_patch(evidence_root,evidence)
    for name,sha in evidence.items():checked_evidence(evidence_root,name,sha)
    if evidence['approved-spec.json']!=V3_SPEC_SHA or evidence[spec['offline_evidence']['artifact']]!=spec['offline_evidence']['sha256']:
        raise ValueError('method_v3_actual_spec_and_offline_evidence_required')
    packet=json.loads((Path(evidence_root)/'dry-run.json').read_text()).get('review_packet',{})
    payload={k:v for k,v in packet.items() if k not in ('review_id','artifact_sha256')}
    computed=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if computed!=packet.get('artifact_sha256') or computed!=method.get('review_artifact_sha256'):
        raise ValueError('method_v3_review_packet_digest_mismatch')
    # 被审核副本必须含未脱敏的完整锁定 JSON 规格与实际修复差异，避免借别的 PASS。
    rendered=json.loads((Path(evidence_root)/'approved-spec-review.json').read_text())
    # 唯一转换是两个被自动脱敏误识别的非秘密预算字段名，值与其他字段必须逐字同义。
    rendered['shared_options']['max_tokens']=rendered['shared_options'].pop('单启动总预算')
    rendered['collection_limits']['total_tokens']=rendered['collection_limits'].pop('双启动总预算')
    if not same_json(rendered,spec):raise ValueError('method_v3_reviewed_budget_translation_mismatch')
    for name in ('approved-spec-review.json','candidate.patch','proposal.md'):
        raw=(Path(evidence_root)/name).read_text()
        numbered='\n'.join(f'L{i:05d} {line}' for i,line in enumerate(raw.splitlines(),1))
        items=[x for x in packet.get('candidate_artifacts',[]) if x.get('sha256')==evidence[name]]
        if (len(items)!=1 or items[0].get('redacted_lines')!=[]
                or items[0].get('content_with_line_numbers')!=numbered):
            raise ValueError('method_v3_review_must_cover_unredacted_candidate:'+name)
    review=json.loads((Path(evidence_root)/'review-attempt-4.json').read_text())
    validate_actual_dual_review(review,method.get('review_id'),computed)
    proof=json.loads((Path(evidence_root)/spec['offline_evidence']['artifact']).read_text())
    if (proof.get('status')!='completed_with_limits'
            or proof.get('branch_and_tested_shape_consistency_verified') is not True
            or proof.get('unchanged_ast_outside_INTERACTION_SCHEMA') is not True
            or proof.get('protected_source_and_historical_artifacts_unchanged') is not True
            or any(type(proof.get(k)) is not int or proof[k]!=0 for k in ('paid_calls','network_requests','credential_loads'))):
        raise ValueError('method_v3_free_original_guard_checks_required')
    return []

def canonical_v4_patch(base_root, candidate_root, targets):
    """从实际八份原件生成受审 diff；收据自洽不能替代原件一致。"""
    if set(targets)!=V4_TARGETS:raise ValueError('method_v4_exact_eight_targets_required')
    pieces=[]
    for name in sorted(targets):
        target=targets[name];base=Path(base_root)/name;candidate=Path(candidate_root)/name
        if (not isinstance(target,dict) or set(target)!={'base_sha256','candidate_sha256'}
                or candidate.is_symlink() or not candidate.is_file()
                or digest(candidate)!=target['candidate_sha256']):
            raise ValueError('method_v4_candidate_source_changed:'+name)
        if target['base_sha256'] is None:
            if name not in V4_NEW_MODULES or base.exists() or base.is_symlink():
                raise ValueError('method_v4_new_module_base_must_be_absent:'+name)
            old=[];fromfile='/dev/null'
        else:
            if base.is_symlink() or not base.is_file() or digest(base)!=target['base_sha256']:
                raise ValueError('method_v4_base_source_changed:'+name)
            old=base.read_text().splitlines(keepends=True);fromfile='a/'+name
        new=candidate.read_text().splitlines(keepends=True)
        pieces.extend(difflib.unified_diff(old,new,fromfile=fromfile,tofile='b/'+name,n=3))
    return ''.join(pieces).encode('utf-8')

def canonical_v4_runner_patch(before,after):
    return ''.join(difflib.unified_diff(before.decode('utf-8').splitlines(keepends=True),
        after.decode('utf-8').splitlines(keepends=True),fromfile='a/scripts/run_jev_business_ab.py',
        tofile='b/scripts/run_jev_business_ab.py',n=3)).encode('utf-8')

def canonical_v4_oracle_patch(before,after):
    return ''.join(difflib.unified_diff(before.decode('utf-8').splitlines(keepends=True),
        after.decode('utf-8').splitlines(keepends=True),fromfile='a/evaluation/jev_business/oracle.py',
        tofile='b/evaluation/jev_business/oracle.py',n=3)).encode('utf-8')

def validate_method_v4(method,evidence_root,overlay_metadata,*,out,case_ids,repetitions,
                       regression=False,baseline_pilot_only=False,candidate_root=None,base_root=None,
                       question_root=None):
    """真实新双审绑定八目标与差异，只批准原题的一次有限 pair。"""
    evidence=method.get('review_artifacts',{})
    spec_sha=method.get('review_spec_sha256')
    spec_path=checked_evidence(evidence_root,'approved-spec.json',spec_sha)
    spec=json.loads(spec_path.read_text())
    if (not isinstance(spec,dict) or spec.get('schema_version')!='navren.jev-business-method.v4'
            or spec.get('status')!='method_pass_only_not_implementation_or_performance'
            or any(not same_json(method.get(k),v) for k,v in spec.items())
            or any(k in spec for k in ('review_id','review_artifact_sha256','review_artifacts','review_spec_sha256','review_result_artifact'))):
        raise ValueError('method_v4_approved_spec_changed')
    scope=spec.get('collection_scope',{})
    approved_out=scope.get('approved_output_dir')
    expected_scope={'kind':'limited_component_pilot','case_ids':['P08'],'repetitions_per_arm':1,
        'arms':['baseline','jev'],'approved_output_dir':approved_out,'max_freezes':1,
        'baseline_first':True,'baseline_technical_or_accounting_failure_stops':True}
    if (not isinstance(approved_out,str) or not Path(approved_out).is_absolute()
            or not same_json(scope,expected_scope) or Path(out).resolve()!=Path(approved_out).resolve()
            or case_ids!=['P08'] or type(repetitions) is not int or repetitions!=1
            or regression or baseline_pilot_only or not same_json(spec.get('shared_options'),SHARED_OPTIONS)
            or not same_json(spec.get('collection_limits'),V2_COLLECTION_LIMITS)
            or not same_json(spec.get('channels'),CHANNELS) or not same_json(spec.get('arms'),LIMITED_ARMS)
            or not same_json(spec.get('selection'),V4_SELECTION)
            or not same_json(spec.get('stop_conditions'),V2_STOP_CONDITIONS)
            or spec.get('ordinary_semantic_error_is_not_sample_selection') is not True
            or spec.get('question_rule')!='verbatim_existing_source_questions_no_synthetic_new_difficulty_or_tool_plan_hint'
            or spec.get('formal_adoption_approved') is not False or spec.get('performance_pass') is not False):
        raise ValueError('method_v4_only_reviewed_limited_P08_pair')
    question_root=Path(question_root or ROOT)
    cases=load_dataset(root=question_root)['cases']
    question=next(c['question'] for c in cases if c['id']=='P08')
    if (spec.get('original_question_sha256')!=digest(question_root/'evaluation/jev_business/cases.json')
            or spec.get('original_question_text_sha256')!=V4_P08_QUESTION_SHA
            or hashlib.sha256(question.encode('utf-8')).hexdigest()!=V4_P08_QUESTION_SHA):
        raise ValueError('method_v4_original_question_changed')
    targets=spec.get('approved_overlay_targets',{})
    if (set(targets)!=V4_TARGETS or not isinstance(overlay_metadata,dict)
            or spec.get('approved_overlay_schema')!='navren.jev-candidate-overlay.v5'
            or overlay_metadata.get('schema_version')!=spec['approved_overlay_schema']
            or overlay_metadata.get('status')!='offline_candidate_only'
            or overlay_metadata.get('same_method_for_both_arms') is not True
            or overlay_metadata.get('ready_for_paid_collection') is not False
            or not same_json(overlay_metadata.get('targets'),targets)
            or not same_json(overlay_metadata.get('validated_targets'),targets)
            or spec.get('approved_overlay_manifest_sha256')!=overlay_metadata.get('manifest_sha256')
            or not same_json(spec.get('approved_overlay_sha256'),{name:target.get('candidate_sha256') for name,target in targets.items()})):
        raise ValueError('method_v4_exact_reviewed_overlay_required')
    review_name=method.get('review_result_artifact')
    if (not isinstance(review_name,str) or not review_name.startswith('review-attempt-')
            or not review_name.endswith('.json') or not review_name[len('review-attempt-'):-len('.json')].isdigit()):
        raise ValueError('method_v4_actual_review_artifact_required')
    required={'approved-spec.json','approved-spec-review.json','candidate.patch','proposal.md','dry-run.json',review_name,
              'runner-before.py','runner.patch'}
    outcome_version=spec.get('outcome_classification_version')
    if outcome_version is not None and outcome_version!='navren.business-outcome/2':
        raise ValueError('method_v4_outcome_classification_version_invalid')
    if outcome_version=='navren.business-outcome/2':required.update({'oracle-before.py','oracle.patch'})
    if not isinstance(evidence,dict) or not required.issubset(evidence):
        raise ValueError('method_v4_review_evidence_missing')
    for name,sha in evidence.items():checked_evidence(evidence_root,name,sha)
    if (evidence['approved-spec.json']!=spec_sha or evidence['candidate.patch']!=spec.get('candidate_patch_sha256')):
        raise ValueError('method_v4_actual_spec_or_patch_sha_required')
    runner_source=question_root/'scripts/run_jev_business_ab.py'
    if (evidence['runner-before.py']!=V4_RUNNER_BASE_SHA
            or spec.get('runner_base_sha256')!=V4_RUNNER_BASE_SHA
            or spec.get('runner_sha256')!=digest(runner_source)
            or evidence['runner.patch']!=spec.get('runner_patch_sha256')
            or canonical_v4_runner_patch((Path(evidence_root)/'runner-before.py').read_bytes(),runner_source.read_bytes())
                !=(Path(evidence_root)/'runner.patch').read_bytes()):
        raise ValueError('method_v4_actual_reviewed_runner_diff_required')
    if outcome_version=='navren.business-outcome/2':
        oracle_source=question_root/'evaluation/jev_business/oracle.py'
        if (spec.get('oracle_base_sha256')!=evidence['oracle-before.py']
                or spec.get('approved_oracle_sha256')!=digest(oracle_source)
                or spec.get('oracle_patch_sha256')!=evidence['oracle.patch']
                or canonical_v4_oracle_patch((Path(evidence_root)/'oracle-before.py').read_bytes(),oracle_source.read_bytes())
                    !=(Path(evidence_root)/'oracle.patch').read_bytes()):
            raise ValueError('method_v4_actual_reviewed_oracle_diff_required')
    if candidate_root is None or base_root is None:
        raise ValueError('method_v4_actual_source_roots_required')
    patch=canonical_v4_patch(base_root,candidate_root,targets)
    if patch!=(Path(evidence_root)/'candidate.patch').read_bytes():
        raise ValueError('method_v4_reviewed_patch_does_not_match_sources')
    packet=json.loads((Path(evidence_root)/'dry-run.json').read_text()).get('review_packet',{})
    payload={k:v for k,v in packet.items() if k not in ('review_id','artifact_sha256')}
    computed=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if computed!=packet.get('artifact_sha256') or computed!=method.get('review_artifact_sha256'):
        raise ValueError('method_v4_review_packet_digest_mismatch')
    rendered=json.loads((Path(evidence_root)/'approved-spec-review.json').read_text())
    try:
        if 'max_tokens' in rendered['shared_options'] or 'total_tokens' in rendered['collection_limits']:
            raise ValueError('ambiguous_budget_field')
        rendered['shared_options']['max_tokens']=rendered['shared_options'].pop('单启动总预算')
        rendered['collection_limits']['total_tokens']=rendered['collection_limits'].pop('双启动总预算')
    except (KeyError,TypeError,ValueError) as cause:
        raise ValueError('method_v4_reviewed_budget_translation_mismatch') from cause
    if not same_json(rendered,spec):raise ValueError('method_v4_reviewed_budget_translation_mismatch')
    reviewed_names=['approved-spec-review.json','candidate.patch','proposal.md','runner.patch']
    if outcome_version=='navren.business-outcome/2':reviewed_names.append('oracle.patch')
    for name in reviewed_names:
        raw=(Path(evidence_root)/name).read_text()
        numbered='\n'.join(f'L{i:05d} {line}' for i,line in enumerate(raw.splitlines(),1))
        items=[item for item in packet.get('candidate_artifacts',[]) if item.get('sha256')==evidence[name]]
        if (len(items)!=1 or items[0].get('redacted_lines')!=[]
                or items[0].get('content_with_line_numbers')!=numbered):
            raise ValueError('method_v4_review_must_cover_unredacted_candidate:'+name)
    review=json.loads((Path(evidence_root)/review_name).read_text())
    validate_actual_dual_review(review,method.get('review_id'),computed)
    return []

def source_files(root):
    # 原对照runner只需JSON题集；本轮原问句在JSONL，必须一起冻结。
    excluded={'.git','.venv','venv','node_modules','__pycache__','outputs','logs','upstream',
              'archive','archives','backups','.pytest_cache'}
    suffixes={'.py','.json','.jsonl','.csv','.md','.sql','.yaml','.yml','.html','.js','.css','.png','.svg','.txt'}
    result=[]
    for directory,dirs,names in os.walk(root):
        dirs[:]=[d for d in dirs if d not in excluded and not (Path(directory)/d).is_symlink()]
        for name in names:
            p=Path(directory)/name
            if p.suffix in suffixes and not name.startswith('.env') and not p.is_symlink():result.append(p)
    return result

def now(): return datetime.now(timezone.utc).isoformat()

def validate_overlay(overlay, base_root=ROOT):
    overlay=Path(overlay).resolve();metadata_path=overlay/'overlay.json'
    metadata=json.loads(metadata_path.read_text())
    core='agent_runtime/conversation.py'
    if metadata.get('schema_version') in ('navren.jev-candidate-overlay.v2','navren.jev-candidate-overlay.v3','navren.jev-candidate-overlay.v4','navren.jev-candidate-overlay.v5'):
        allowed={core,'skills/bank-service/SKILL.md'}
        if metadata['schema_version'] in ('navren.jev-candidate-overlay.v3','navren.jev-candidate-overlay.v4'):allowed.add('agent_runtime/jev_plans.py')
        if metadata['schema_version']=='navren.jev-candidate-overlay.v4':allowed.add('base_runtime/reply_interaction.py')
        if metadata['schema_version']=='navren.jev-candidate-overlay.v5':allowed=set(V4_TARGETS)
        targets=metadata.get('targets',{})
        if (set(targets)!=allowed or metadata.get('same_method_for_both_arms') is not True
                or metadata.get('status')!='offline_candidate_only'
                or metadata.get('ready_for_paid_collection') is not False):
            raise ValueError('overlay_exact_common_offline_candidate_required')
        files={str(p.relative_to(overlay)) for directory in ('agent_runtime','skills','base_runtime')
               for p in (overlay/directory).rglob('*')
               if p.is_file() and '__pycache__' not in p.parts}
        if files!=allowed:raise ValueError('overlay_must_only_cover_declared_common_targets')
    else:
        if metadata.get('schema_version'):raise ValueError('unknown_overlay_schema')
        allowed={core}
        files={str(p.relative_to(overlay)) for p in overlay.rglob('*.py') if '__pycache__' not in p.parts}
        if metadata.get('source')!=core or files!=allowed:
            raise ValueError('overlay_must_only_cover_conversation')
        targets={core:{'base_sha256':metadata.get('base_sha256'),'candidate_sha256':metadata.get('overlay_sha256')}}
    for rel, target in targets.items():
        candidate=overlay/rel
        if any(p.is_symlink() for p in (candidate,*candidate.parents)):
            raise ValueError('overlay_symlinks_forbidden')
        base=Path(base_root)/rel
        if metadata.get('schema_version')=='navren.jev-candidate-overlay.v5':
            if not isinstance(target,dict) or set(target)!={'base_sha256','candidate_sha256'}:
                raise ValueError('overlay_exact_target_hash_fields_required:'+rel)
            if target['base_sha256'] is None:
                if rel not in V4_NEW_MODULES or base.exists() or base.is_symlink():
                    raise ValueError('overlay_new_module_base_must_be_absent:'+rel)
            elif base.is_symlink() or not base.is_file() or target['base_sha256']!=digest(base):
                raise ValueError('overlay_base_source_changed:'+rel)
        elif target.get('base_sha256')!=digest(base):
            raise ValueError('overlay_base_source_changed:'+rel)
        if target.get('candidate_sha256')!=digest(candidate):
            raise ValueError('overlay_file_hash_mismatch:'+rel)
    return {**metadata,'validated_targets':targets,'manifest_sha256':digest(metadata_path)}

def validate_pricing(value):
    """独立当日资料输入，无默认旧费率。所有价均为每百万tokens。"""
    if value.get('schema_version')!='navren.jev-business-pricing.v1' or value.get('verified') is not True:
        raise ValueError('independently_verified_pricing_required')
    if not value.get('verified_at') or not value.get('sources'):
        raise ValueError('pricing_provenance_missing')
    fx=value.get('comparison_fx_cny_per_usd')
    if type(fx) not in (int,float) or not math.isfinite(fx) or fx<=0:
        raise ValueError('comparison_fx_required')
    models={'primary':('DeepSeek-V4-Flash','ai_ping'), 'reviewer':('GLM-5.3-Flash','ai_ping'),
            'jev':('jev-1.13.0','typesafe_official')}
    for stage,(model,provider) in models.items():
        rate=value.get('rates',{}).get(stage,{})
        if rate.get('model')!=model or rate.get('provider')!=provider or rate.get('currency') not in ('CNY','USD'):
            raise ValueError('pricing_model_or_route_mismatch:'+stage)
        for key in ('input_range','output_range'):
            x=rate.get(key)
            if not isinstance(x,list) or len(x)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in x) or x[0]>x[1]:
                raise ValueError('invalid_price_range:'+stage+':'+key)
    return value

def cost(calls, pricing):
    tokens, lo, hi, unknown=0,0.,0.,[]
    for index,call in enumerate(calls):
        usage=call.get('usage');stage=call['stage']
        ikey,okey=('input_tokens','output_tokens') if stage=='jev' else ('prompt_tokens','completion_tokens')
        if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0 for k in (ikey,okey)):
            unknown.append(index);continue
        i,o=usage[ikey],usage[okey];tokens+=i+o
        if pricing is None:
            unknown.append(index);continue
        rate=pricing['rates'][stage]
        if call.get('model_actual') != rate['model']:
            unknown.append(index);continue
        factor=pricing['comparison_fx_cny_per_usd'] if rate['currency']=='USD' else 1
        lo+=(i*rate['input_range'][0]+o*rate['output_range'][0])*factor/1e6
        hi+=(i*rate['input_range'][1]+o*rate['output_range'][1])*factor/1e6
    return {'usage_and_price_verified':not unknown and pricing is not None,'actual_tokens':tokens,
            'unknown_call_indices':unknown,'estimated_cny_range':[lo,hi] if not unknown and pricing is not None else None,
            'partial_known_estimate_cny_range':[lo,hi], 'is_invoice':False}

def freeze(out, *, ids=None, repetitions=2, regression=False, public_database=DEFAULT_PUBLIC,
           overlay=None, pricing_json=None, baseline_pilot_only=False, method_contract=None):
    out=Path(out).resolve()
    if out.exists():raise ValueError('refuse_overwrite_experiment')
    data=load_dataset();cases=selected_cases(data,ids,regression=regression)
    plan=paired_plan(cases,repetitions,seed=20261007)
    if baseline_pilot_only:
        if regression or len(cases)!=1 or cases[0]['id']!='P01' or repetitions!=1:
            raise ValueError('baseline_pilot_only_requires_P01_one_repetition')
        plan=[r for r in plan if r['variant']=='baseline']
    pricing=validate_pricing(json.loads(Path(pricing_json).read_text())) if pricing_json else None
    overlay_metadata=validate_overlay(overlay) if overlay else None
    method = None
    method_evidence = {}
    method_readiness_blocks=None
    if method_contract:
        method_path = Path(method_contract).resolve()
        method = json.loads(method_path.read_text())
        if (method.get('schema_version') not in ('navren.jev-business-method.v1',*LIMITED_METHOD_SCHEMAS)
                or method.get('status') != 'method_pass_only_not_implementation_or_performance'
                or not method.get('review_id')):
            raise ValueError('reviewed_method_contract_required')
        if method.get('channels') != CHANNELS:
            raise ValueError('method_channels_mismatch')
        for name, expected in method.get('review_artifacts', {}).items():
            checked_evidence(method_path.parent,name,expected)
            method_evidence[name] = expected
        if not method_evidence:
            raise ValueError('method_review_evidence_missing')
        if method['schema_version'] in LIMITED_METHOD_SCHEMAS:
            validator={'navren.jev-business-method.v2':validate_method_v2,'navren.jev-business-method.v3':validate_method_v3,
                'navren.jev-business-method.v4':validate_method_v4}[method['schema_version']]
            roots={'candidate_root':overlay,'base_root':ROOT,'question_root':ROOT} if method['schema_version'].endswith('.v4') else {}
            method_readiness_blocks=validator(method,method_path.parent,overlay_metadata,
                out=out,case_ids=[c['id'] for c in cases],repetitions=repetitions,
                regression=regression,baseline_pilot_only=baseline_pilot_only,**roots)
            # 此有限首次 pilot 明文先检验共同基线；旧协议/执行顺序原件不改。
            plan=[{'case_id':'P08','repetition':1,'variant':arm} for arm in ('baseline','jev')]
    files=source_files(ROOT);before={str(p.relative_to(ROOT)):digest(p) for p in files}
    out.mkdir(parents=True)
    if method is not None:
        shutil.copyfile(method_path,out/'method-contract.json')
        for name in method_evidence:
            dest=out/'method-evidence'/name;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(method_path.parent/name,dest)
        if method.get('schema_version')=='navren.jev-business-method.v4':
            for name,target in method['approved_overlay_targets'].items():
                if target['base_sha256'] is not None:
                    dest=out/'overlay-base'/name;dest.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(ROOT/name,dest)
    for p in files:
        dest=out/'source'/p.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    after={str(p.relative_to(ROOT)):digest(p) for p in files}
    if before!=after or any(digest(out/'source'/n)!=h for n,h in before.items()):
        raise ValueError('live_source_changed_during_freeze')
    overlay_sha={}
    if overlay:
        overlay=Path(overlay).resolve()
        if overlay_metadata!=validate_overlay(overlay):raise ValueError('overlay_changed_during_freeze')
        # 只复制已校验目标；说明、脚本和任意其他文件均不进入应用源码。
        for name in sorted(overlay_metadata['validated_targets']):
            rel=Path(name);p=overlay/rel
            dest=out/'source'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
            overlay_sha[str(rel)]=digest(p)
        write(out/'candidate-overlay.json',overlay_metadata)
    manifest={n:digest(out/'source'/n) for n in before}
    manifest.update(overlay_sha)
    from evaluation.jev_business.oracle import clone_database
    need_public=any(c.get('public_original') for c in cases)
    if need_public:
        clone_database(public_database,out/'public.sqlite3')
    protocol={'schema_version':'navren.jev-business-ab.v1','frozen_at':now(),'scope':data['scope'],
        'case_ids':[c['id'] for c in cases],'group':'regression' if regression else 'development',
        'baseline_pilot_only':baseline_pilot_only,
        'is_full_development_gate':False if baseline_pilot_only or len(cases)!=6 or repetitions<2 else None,
        'plan':plan,'repetitions':repetitions,'random_seed':20261007,'source_sha256':manifest,
        'live_source_before_sha256':before,'live_source_after_sha256':after,'candidate_overlay_sha256':overlay_sha,
        'candidate_overlay_metadata':overlay_metadata,
        'collection_readiness_blocks':method_readiness_blocks if method_readiness_blocks is not None else (['candidate_common_method_not_cloud_reapproved','prior_sent_usage_unverified']
            + (['candidate_plan_contract_not_cloud_reapproved'] if overlay_metadata.get('schema_version')=='navren.jev-candidate-overlay.v3' else [])
            if overlay_metadata and overlay_metadata.get('status')=='offline_candidate_only' else []),
        'dataset_sha256':digest(out/'source/evaluation/jev_business/cases.json'),
        'oracle_sha256':digest(out/'source/evaluation/jev_business/oracle.py'),
        'public_snapshot_sha256':digest(out/'public.sqlite3') if need_public else None,
        'public_snapshot_policy':'read_only_source_sqlite_backup_to_frozen_clone; per_trial_temporary_clone; no_cold_fetch',
        'primary_provider':'ai_ping','reviewer_provider':'ai_ping','jev_provider':'typesafe_official',
        'agent_module':'agent_runtime.jev_agent','agent_class':'ReadPlanConversationAgent',
        'shared_options':deepcopy(SHARED_OPTIONS),
        'round_limit_semantics':'same_total_loop_slots; accepted_local_plan_uses_round_zero; primary_HTTP_requests_measured_separately',
        'arms':{'baseline':{'jev_policy':'off'},'jev':{'jev_policy':'read_plan'}},
        'intervention':'same_agent_and_commonhook_only_jev_read_plan_changes; local_plan_not_primary_http_request',
        'instrumentation':'same_RecordingBankTools_super_invoke_return_unchanged; selected_readonly_raw_outputs_for_identity_oracle',
        'limits':deepcopy(V2_COLLECTION_LIMITS) if method is not None and method.get('schema_version') in LIMITED_METHOD_SCHEMAS else {'total_tokens':4000000,'estimated_cost_upper_cny':20},
        'pricing':pricing,'pricing_sha256':digest(pricing_json) if pricing_json else None,
        'method_contract':method,'method_contract_sha256':digest(out/'method-contract.json') if method is not None else None,
        'collection_scope':deepcopy(method.get('collection_scope')) if method is not None and method.get('schema_version') in LIMITED_METHOD_SCHEMAS else None,
        'overlay_base_sha256':{name:target['base_sha256'] for name,target in method['approved_overlay_targets'].items()
            if target['base_sha256'] is not None} if method is not None and method.get('schema_version')=='navren.jev-business-method.v4' else None,
        'method_evidence_sha256':method_evidence,
        'semantic_rule':'necessary_conditions_not_accuracy; blind_whole_answer_independent_review_required',
        'denominator':'all_started_trials; regression_separate; no_repeat_after_unknown_usage'}
    if method is not None and method.get('shared_options')!=protocol['shared_options']:
        raise ValueError('reviewed_shared_options_mismatch')
    write(out/'protocol.json',protocol)
    return {'status':'frozen','trials':len(plan),'out':str(out),'paid_model_calls':0,
            'not_ready_case_ids':[c['id'] for c in cases if c.get('fixture_status')]}

def verify_source(out,protocol):
    if (protocol.get('method_contract') or {}).get('schema_version')=='navren.jev-business-method.v4':
        if not protocol.get('method_contract_sha256'):
            raise ValueError('method_v4_frozen_method_receipt_required')
        if any(path.is_symlink() for path in (out/'source').rglob('*')):
            raise ValueError('method_v4_frozen_source_file_set_changed')
    mismatches=[name for name,expected in protocol['source_sha256'].items() if not (out/'source'/name).is_file() or digest(out/'source'/name)!=expected]
    if mismatches:raise ValueError('frozen_source_changed:'+','.join(mismatches[:5]))
    public=out/'public.sqlite3'
    if protocol.get('public_snapshot_sha256') and digest(public)!=protocol['public_snapshot_sha256']:
        raise ValueError('frozen_public_snapshot_changed')
    if protocol.get('method_contract_sha256'):
        if digest(out/'method-contract.json') != protocol['method_contract_sha256']:
            raise ValueError('frozen_method_contract_changed')
        for name, expected in protocol['method_evidence_sha256'].items():
            if digest(out/'method-evidence'/name) != expected:
                raise ValueError('frozen_method_evidence_changed:'+name)
        method=json.loads((out/'method-contract.json').read_text())
        if protocol.get('method_contract')!=method:
            raise ValueError('frozen_method_contract_embedded_mismatch')
        if method.get('schema_version') in LIMITED_METHOD_SCHEMAS:
            validator={'navren.jev-business-method.v2':validate_method_v2,'navren.jev-business-method.v3':validate_method_v3,
                'navren.jev-business-method.v4':validate_method_v4}[method['schema_version']]
            roots={'candidate_root':out/'source','base_root':out/'overlay-base','question_root':out/'source'} if method['schema_version'].endswith('.v4') else {}
            blocks=validator(method,out/'method-evidence',protocol.get('candidate_overlay_metadata'),
                out=out,case_ids=protocol.get('case_ids'),repetitions=protocol.get('repetitions'),
                regression=protocol.get('group')!='development',baseline_pilot_only=protocol.get('baseline_pilot_only'),**roots)
            plan=[{'case_id':'P08','repetition':1,'variant':arm} for arm in ('baseline','jev')]
            if (protocol.get('plan')!=plan or protocol.get('collection_scope')!=method['collection_scope']
                    or protocol.get('candidate_overlay_sha256')!=method.get('approved_overlay_sha256')
                    or not same_json(protocol.get('shared_options'),SHARED_OPTIONS)
                    or not same_json(protocol.get('limits'),V2_COLLECTION_LIMITS)
                    or {s:protocol.get(s+'_provider') for s in CHANNELS}!=CHANNELS
                    or protocol.get('arms')!={'baseline':{'jev_policy':'off'},'jev':{'jev_policy':'read_plan'}}
                    or protocol.get('collection_readiness_blocks')!=blocks):
                version='v4' if method['schema_version'].endswith('.v4') else 'v2'
                raise ValueError('method_'+version+'_frozen_scope_or_readiness_changed')
            if method['schema_version']=='navren.jev-business-method.v4':
                actual={str(path.relative_to(out/'source')) for path in source_files(out/'source')}
                if (actual!=set(protocol['source_sha256'])
                        or any(path.is_symlink() for path in (out/'source').rglob('*'))):
                    raise ValueError('method_v4_frozen_source_file_set_changed')
                bases={name:target['base_sha256'] for name,target in method['approved_overlay_targets'].items()
                       if target['base_sha256'] is not None}
                actual_bases={str(path.relative_to(out/'overlay-base')) for path in (out/'overlay-base').rglob('*') if path.is_file()}
                if (not same_json(protocol.get('overlay_base_sha256'),bases) or actual_bases!=set(bases)
                        or any(path.is_symlink() for path in (out/'overlay-base').rglob('*'))
                        or any(protocol['source_sha256'].get(name)!=target['candidate_sha256']
                            or protocol['live_source_before_sha256'].get(name)!=target['base_sha256']
                            or protocol['live_source_after_sha256'].get(name)!=target['base_sha256']
                            for name,target in method['approved_overlay_targets'].items())
                        or not same_json(json.loads((out/'candidate-overlay.json').read_text()),protocol['candidate_overlay_metadata'])):
                    raise ValueError('method_v4_frozen_overlay_or_baseline_changed')
    metadata=protocol.get('candidate_overlay_metadata') or {}
    method=protocol.get('method_contract') or {}
    if metadata.get('status')=='offline_candidate_only' and method.get('schema_version') not in LIMITED_METHOD_SCHEMAS:
        required={'candidate_common_method_not_cloud_reapproved','prior_sent_usage_unverified'}
        if metadata.get('schema_version')=='navren.jev-candidate-overlay.v3':required.add('candidate_plan_contract_not_cloud_reapproved')
        if not required.issubset(set(protocol.get('collection_readiness_blocks',[]))):
            raise ValueError('offline_candidate_blockers_cannot_be_removed_without_reviewed_v2')

def activate_source(out):
    """只能在新 CLI 进程中使用，防已导入生产 core 偷混冻结source。"""
    if any(n.startswith(('agent_runtime.','finance_gateway','simulated_bank.')) for n in sys.modules):
        raise ValueError('frozen_source_requires_fresh_process')
    for n in list(sys.modules):
        if n=='evaluation' or n.startswith('evaluation.') or n=='scripts' or n.startswith('scripts.'):
            sys.modules.pop(n,None)
    sys.path.insert(0,str(out/'source'))
    return importlib.import_module('evaluation.jev_business.oracle')

def preflight(out):
    out=Path(out).resolve();p=json.loads((out/'protocol.json').read_text());verify_source(out,p)
    oracle=activate_source(out);data=oracle.load_dataset();cases=[c for c in data['cases'] if c['id'] in p['case_ids']]
    report={'scope':'fixture_and_interface_only_no_model_quality_result','paid_model_calls':0,'records':[]}
    for c in cases:
        try:
            with tempfile.TemporaryDirectory(prefix='navren-jev-business-pre-') as tmp:
                g,w,_=oracle.build_fixture(c,tmp,public_database=out/'public.sqlite3' if c.get('public_original') else None)
                result=oracle.preflight(g,w,c)
                report['records'].append({'id':c['id'],'preflight':result})
        except Exception as e:
            report['records'].append({'id':c['id'],'preflight':{'status':'failed','error':type(e).__name__+':'+str(e)[:180]}})
    try:
        module=importlib.import_module(p['agent_module']);cls=getattr(module,p['agent_class'])
        supported=getattr(cls,'supports_local_read_plans',False) is True
        report['agent_interface_ready']=supported
        if not supported: report['interface_reason']='shared_local_read_plan_hook_missing'
    except (ImportError,AttributeError) as e:
        report['agent_interface_ready']=False;report['interface_reason']=type(e).__name__+':'+str(e)[:120]
    report['pricing_verified']=p.get('pricing') is not None
    report['reviewed_method_frozen']=p.get('method_contract_sha256') is not None
    report['fixtures_ready']=all(r['preflight']['status']=='passed' for r in report['records'])
    report['collection_readiness_blocks']=p.get('collection_readiness_blocks',[])
    report['ready_for_paid_collection']=(report['fixtures_ready'] and report['agent_interface_ready']
        and report['pricing_verified'] and report['reviewed_method_frozen'] and not report['collection_readiness_blocks'])
    report['protocol_sha256']=digest(out/'protocol.json');write(out/'preflight.json',report)
    return report

class FrozenPublicRuntime:
    def __init__(self,provider,release):
        from local_knowledge.runtime import RoundProvider
        self.provider=RoundProvider(provider.for_release(release) if release else provider, records=(),release_id=release)
    def prepare_round(self,text):
        from local_knowledge.runtime import PreparedEvidence
        return PreparedEvidence(self.provider,{'mode':'jev_business_frozen','cold_fetch_enabled':False,
            'semantic_validation':False,'release_id':self.provider.release_id})
    def close(self):pass

class TrialServerLifecycle:
    """临时目录的内层资源：等待监听与请求线程结束后才允许删库。"""
    def __init__(self):
        self.server=None;self.thread=None;self.cleanup_ms=0;self.cleanup_error=None
    def __enter__(self):return self
    def start(self,server):
        if self.server is not None:raise ValueError('trial_server_already_attached')
        self.server=server
        # ThreadingHTTPServer默认daemon请求线程不会被server_close等待。
        # 本地验收必须等待HTTP处理器最后一次存储/退出，防止删库后重建目录。
        server.daemon_threads=False
        server.block_on_close=True
        self.thread=threading.Thread(target=server.serve_forever,daemon=True)
        self.thread.start()
        return server
    def close(self):
        started=time.monotonic()
        try:
            if self.server is None:return
            try:
                if self.thread is not None and self.thread.is_alive():self.server.shutdown()
            finally:
                try:self.server.server_close()
                finally:
                    if self.thread is not None and self.thread.ident is not None:
                        self.thread.join(timeout=3)
                        if self.thread.is_alive():raise RuntimeError('trial_server_thread_not_stopped')
        finally:
            self.cleanup_ms=round((time.monotonic()-started)*1000)
    def __exit__(self,exc_type,exc,tb):
        try:self.close()
        except BaseException as error:
            self.cleanup_error={'type':type(error).__name__,'code':str(error)[:180]}
            if exc_type is None:raise
        return False

class BoundaryFailureObserver:
    """仅供合成 P08 的两臂观察：不增加投影，不保存原值，不检查栈帧。"""
    fields=('metric_id','subject_id','metric_name','data_scope','basis','formula_id')
    def __init__(self,boundary_class,boundary_error):
        self.boundary_class=boundary_class;self.boundary_error=boundary_error
        self.local=threading.local();self.events=[]
    @staticmethod
    def summary(value):
        return {'type':type(value).__name__,'length':len(value) if type(value) is str else None,
            'sha256':hashlib.sha256(value.encode('utf-8',errors='surrogatepass')).hexdigest() if type(value) is str else None}
    def __enter__(self):
        original_string=self.boundary_class._contract_string
        original_signature=self.boundary_class._metric_signature
        self.original_string=original_string;self.original_signature=original_signature
        def string_observer(boundary,value,*args,**kwargs):
            try:return original_string(boundary,value,*args,**kwargs)
            except self.boundary_error as error:
                item=self.summary(value)
                candidates=[f for f in getattr(self.local,'metric_fields',[]) if f['summary']==item]
                status='ambiguous' if len(candidates)>1 else 'single_matching_candidate' if candidates else 'unknown'
                self.events.append({'method':'_contract_string','code':getattr(error,'code',None),
                    'exception_type':type(error).__name__,'value_summary':item,'field_status':status,
                    'candidate_fields':candidates,'matched_field_proven':False,
                    'identifier':kwargs.get('identifier') if type(kwargs.get('identifier')) is bool else None,
                    'prose':kwargs.get('prose') if type(kwargs.get('prose')) is bool else None})
                self.local.last_error_id=id(error)
                raise
        def signature_observer(boundary,metric,*args,**kwargs):
            prior=getattr(self.local,'metric_fields',[]);prior_error=getattr(self.local,'last_error_id',None)
            self.local.metric_fields=[{'name':k,'summary':self.summary(metric[k])} for k in self.fields
                                      if isinstance(metric,dict) and k in metric]
            self.local.last_error_id=None
            try:return original_signature(boundary,metric,*args,**kwargs)
            except self.boundary_error as error:
                if self.local.last_error_id!=id(error):
                    self.events.append({'method':'_metric_signature','code':getattr(error,'code',None),
                        'exception_type':type(error).__name__,'value_summary':None,'field_status':'unknown',
                        'candidate_fields':[],'matched_field_proven':False})
                raise
            finally:
                self.local.metric_fields=prior;self.local.last_error_id=prior_error
        self.boundary_class._contract_string=string_observer
        self.boundary_class._metric_signature=signature_observer
        return self
    def __exit__(self,exc_type,exc,tb):
        self.boundary_class._contract_string=self.original_string
        self.boundary_class._metric_signature=self.original_signature
        return False
    def report(self):
        return {'scope':'synthetic_P08_same_guards_failure_observation_only','events':deepcopy(self.events),
            'original_arguments_returns_exceptions_unchanged':True,'extra_projection_calls':0,
            'raw_values_recorded':False,'frames_or_closures_read':False,'failure_fields_not_inferred':True}

def collect(out):
    out=Path(out).resolve();p=json.loads((out/'protocol.json').read_text());verify_source(out,p)
    if (out/'results.json').exists() or (out/'collection-start.json').exists():
        raise ValueError('refuse_repeat_paid_collection_even_if_interrupted')
    if p.get('collection_readiness_blocks'):
        raise ValueError('offline_candidate_collection_blocked:'+','.join(p['collection_readiness_blocks']))
    pre=json.loads((out/'preflight.json').read_text())
    if pre.get('protocol_sha256')!=digest(out/'protocol.json') or not pre.get('ready_for_paid_collection'):
        raise ValueError('ready_verified_preflight_required')
    validate_pricing(p['pricing'])
    oracle=activate_source(out);data=oracle.load_dataset()
    # Recorder也必须来自冻结源码，不能沿用启动脚本预导入的当前目录版本。
    FrozenRecorder=importlib.import_module('scripts.run_jev_ab').Recorder
    from agent_runtime.conversation import GLMConversationReviewer
    from agent_runtime.jev_client import JevClient
    from agent_runtime.native_client import AIPingToolClient
    from agent_runtime.model_adapters import OpenAICompatibleJsonClient,AIPING_URL,GLM_REVIEW_MODEL
    from server import create_server
    import agent_runtime.conversation as conversation_module
    original_tools=conversation_module.BankTools
    agent_class=getattr(importlib.import_module(p['agent_module']),p['agent_class'])
    if getattr(agent_class,'supports_local_read_plans',False) is not True:raise ValueError('local_plan_interface_not_ready')
    class Primary(AIPingToolClient):
        def __init__(self,recorder):
            super().__init__(urlopen=recorder.opener('primary'),log_path=str(recorder.out/'native-calls.jsonl'))
            self.outputs=[];self.seen=set();self.tools=[];self.calls=[]
        def complete(self,messages,tools,**kwargs):
            # 本地计划工具输出也出现在下一真实 primary message，不伪装模型call。
            for m in messages:
                if m.get('role')=='tool' and m.get('tool_call_id') not in self.seen:
                    self.seen.add(m.get('tool_call_id'))
                    self.outputs.append({'call_id':m.get('tool_call_id'),'name':m.get('name'),'output':json.loads(m['content'])})
            self.tools.append([t.get('function',{}).get('name') for t in tools])
            message,usage=super().complete(messages,tools,**kwargs)
            for c in message.get('tool_calls') or []:
                self.calls.append({'id':c['id'],'name':c['function']['name'],'arguments':json.loads(c['function']['arguments'])})
            return message,usage
    report={'protocol_sha256':digest(out/'protocol.json'),'scope':p['scope'],'complete':False,
            'planned_trials':len(p['plan']),'records':[],'semantic_pass_count':None}
    write(out/'collection-start.json',{'started_at':now(),'protocol_sha256':report['protocol_sha256'],'plan':p['plan']})
    write(out/'results.json',report)
    grand_tokens=0;grand_upper=0.
    for index,planned in enumerate(p['plan']):
        next_trial_max = p['shared_options']['max_tokens'] * max(
            max(rate['input_range']+rate['output_range']) *
            (p['pricing']['comparison_fx_cny_per_usd'] if rate['currency']=='USD' else 1)
            for rate in p['pricing']['rates'].values()) / 1e6
        if (grand_tokens+p['shared_options']['max_tokens']>p['limits']['total_tokens'] or
            grand_upper+next_trial_max>p['limits']['estimated_cost_upper_cny']):
            report['stop_reason']='global_budget_preflight_reserve';break
        c=next(c for c in data['cases'] if c['id']==planned['case_id'])
        dest=out/'trials'/f'{index+1:02}-{planned["variant"]}-{c["id"]}'
        if dest.exists():raise ValueError('trial_directory_already_exists')
        dest.mkdir(parents=True)
        row={**planned,'id':c['id'],'question':c['question'],'trial_index':index+1,'started_at':now(),
             'status':'started','source_before_verified':True,'semantic_pass':None,
             'source_before_manifest_sha256':hashlib.sha256(json.dumps(p['source_sha256'],sort_keys=True).encode()).hexdigest()}
        write(dest/'start.json',row)
        recorder=FrozenRecorder(dest);primary=Primary(recorder)
        review_client=OpenAICompatibleJsonClient(provider='ai_ping',model_id=GLM_REVIEW_MODEL,url=AIPING_URL,
            env_name='AIPING_API_KEY',keychain_service='CC/AIPING_GLM_API_KEY',timeout=35,max_retries=0,
            urlopen=recorder.opener('reviewer'),log_path=str(dest/'native-calls.jsonl'))
        judge=JevClient(urlopen=recorder.opener('jev')) if planned['variant']=='jev' else None
        agent=agent_class(primary=primary,reviewer=GLMConversationReviewer(client=review_client),
            judge=judge,**p['arms'][planned['variant']],**p['shared_options'])
        authoritative=[]
        class RecordingBankTools(original_tools):
            def invoke(inner,name,args):
                output=super().invoke(name,args)
                if name in {'account_summary','calendar_query','knowledge_search','knowledge_status',
                            'service_offer_rules','service_offer_preview'}:
                    authoritative.append({'name':name,'arguments':deepcopy(args),'output':deepcopy(output)})
                return output
        conversation_module.BankTools=RecordingBankTools
        observer=None
        if p.get('collection_scope'):
            from agent_runtime.data_boundary import CloudDataBoundary,BoundaryError
            observer=BoundaryFailureObserver(CloudDataBoundary,BoundaryError)
        server=None;lifecycle=None;started=time.monotonic();runner_started=started
        try:
            with (observer if observer is not None else nullcontext()), tempfile.TemporaryDirectory(prefix='navren-jev-business-trial-') as tmp:
                with TrialServerLifecycle() as lifecycle:
                    g,w,token=oracle.build_fixture(c,tmp,public_database=out/'public.sqlite3' if c.get('public_original') else None)
                    row['preflight']=oracle.preflight(g,w,c)
                    if row['preflight']['status']!='passed':raise ValueError('fixture_drift')
                    row['before']=oracle.snapshot(g,w)
                    release=oracle.reviewed_suite()['reviewed_release_id'] if c.get('public_original') else g.knowledge_provider.status().get('public_release_id')
                    runtime=FrozenPublicRuntime(g.knowledge_provider,release)
                    # bootstrap同样只有临时数据库；避免create_server默认导入改动冻结public。
                    with oracle.isolated_environment(Path(tmp)/'bootstrap'):
                        bootstrap=Path(tmp)/'bootstrap';bootstrap.mkdir()
                        server=create_server(port=0,database=str(bootstrap/'bank.sqlite3'),conversation_agent=agent,
                            evidence_runtime=runtime,market_database=bootstrap/'market.sqlite3')
                    server.finance,server.market=g,g.market
                    lifecycle.start(server)
                    base='http://127.0.0.1:'+str(server.server_port)
                    request=urllib.request.Request(base+'/api/agent',data=json.dumps({'text':c['question'],'mode':'classic'},ensure_ascii=False).encode(),
                        headers={'Content-Type':'application/json','Origin':base,'Cookie':'qz_bank_user='+token,'X-QZ-CSRF':g.csrf(token)})
                    started=time.monotonic()
                    try:
                        with urllib.request.urlopen(request,timeout=130) as response:row['http_status'],row['result']=response.status,json.load(response)
                    except urllib.error.HTTPError as e:row['http_status'],row['result']=e.code,json.load(e)
                    # 用户时延只计实际HTTP响应读取；拆服务/删临时库另列。
                    row['elapsed_ms']=round((time.monotonic()-started)*1000)
                    row['after']=oracle.snapshot(g,w)
        except BaseException as e:
            row['runner_failure']={'type':type(e).__name__,'code':str(e)[:180]}
        finally:
            row.setdefault('elapsed_ms',round((time.monotonic()-started)*1000))
            row['cleanup_ms']=lifecycle.cleanup_ms if lifecycle is not None else 0
            row['runner_elapsed_ms']=round((time.monotonic()-runner_started)*1000)
            if lifecycle is not None and lifecycle.cleanup_error:row['cleanup_failure']=lifecycle.cleanup_error
            conversation_module.BankTools=original_tools
            if observer is not None:row['boundary_failure_observation']=observer.report()
        # Tool name可由assistant的实际id补回，含local plan的消息name如被省略则查output.tool。
        call_names={c['id']:c['name'] for c in primary.calls}
        for o in primary.outputs:
            o['name']=o.get('name') or call_names.get(o['call_id']) or o['output'].get('tool')
        row['model_tool_calls'],row['tool_outputs']=primary.calls,primary.outputs
        row['authoritative_tool_outputs']=authoritative
        row['provider_calls']=recorder.calls;row['primary_requested_tool_views']=primary.tools
        row['cost']=cost(recorder.calls,p['pricing']);row['independent_checks']=oracle.classify(row,c)
        row['technical_completed']=row['independent_checks']['technical_completed'];row['status']='recorded'
        row['stage_requests_sent']={s:sum(x['stage']==s and x.get('request_sent') is True for x in recorder.calls) for s in ('primary','reviewer','jev')}
        try:verify_source(out,p);row['source_after_verified']=True
        except ValueError:row['source_after_verified']=False
        actual_manifest={name:digest(out/'source'/name) for name in p['source_sha256'] if (out/'source'/name).is_file()}
        row['source_after_manifest_sha256']=hashlib.sha256(json.dumps(actual_manifest,sort_keys=True).encode()).hexdigest()
        grand_tokens+=row['cost']['actual_tokens']
        if row['cost']['usage_and_price_verified']:grand_upper+=row['cost']['estimated_cny_range'][1]
        report['records'].append(row);report['actual_tokens']=grand_tokens;report['estimated_cost_upper_cny']=grand_upper
        write(dest/'record.json',row);write(out/'results.json',report)
        print(json.dumps({'trial':index+1,'id':c['id'],'arm':planned['variant'],'status':row['independent_checks']['status'],
            'actual_primary_http_requests':row['stage_requests_sent']['primary'],'elapsed_ms':row['elapsed_ms']},ensure_ascii=False),flush=True)
        if row.get('runner_failure') or not row['cost']['usage_and_price_verified']:
            report['stop_reason']='runner_or_usage_or_pricing_unverified';break
        if not row['source_after_verified']:report['stop_reason']='source_or_public_drift';break
        if p.get('collection_scope'):
            reason=limited_component_stop_reason(row)
            if reason:report['stop_reason']=reason;break
        if grand_tokens>=p['limits']['total_tokens'] or grand_upper>=p['limits']['estimated_cost_upper_cny']:
            report['stop_reason']='global_budget';break
    report['complete']=len(report['records'])==len(p['plan']);report['recorded_trials']=len(report['records'])
    report['accounting_complete']=all(r['cost']['usage_and_price_verified'] for r in report['records'])
    write(out/'results.json',report)
    # 匿名全文包只包含正文/证据/题目oracle，不含组别、延时、Jev选项和批准作为gold。
    blind=[];keys=[]
    for r in report['records']:
        bid=hashlib.sha256((report['protocol_sha256']+str(r['trial_index'])).encode()).hexdigest()[:16]
        blind.append({'blind_id':bid,'question':r['question'],'reply':r.get('result',{}).get('reply'),
            'reply_presentation':r.get('result',{}).get('reply_presentation'),
            'widgets':r.get('result',{}).get('widgets',[]),
            'tool_outputs':[{'name':o['name'],'output':o['output']} for o in r['authoritative_tool_outputs']],
            'oracle':r.get('preflight',{}).get('oracle'),
            'review_template':r['independent_checks']['human_review']})
        keys.append({'blind_id':bid,'trial_index':r['trial_index'],'variant':r['variant'],'case_id':r['id']})
    blind.sort(key=lambda x:x['blind_id']);write(out/'blind-review-pack.json',blind);write(out/'blind-review-key.json',keys)
    return {k:report.get(k) for k in ('complete','recorded_trials','stop_reason','accounting_complete')}

def limited_component_stop_reason(row):
    """有限首次 pilot 的技术熔断不以基线语义正确作为挑样条件。"""
    if row.get('cleanup_failure') or row.get('preflight',{}).get('status')!='passed':
        return 'limited_component_fixture_or_cleanup_invalid'
    if row.get('technical_completed') is not True:
        return 'limited_component_technical_chain_failed'
    if row.get('before')!=row.get('after') or row.get('result',{}).get('bank_actions'):
        return 'limited_component_protected_state_or_financial_action'
    serious={'金融和申请记忆状态不变','无操作确认卡'}
    observed=row.get('authoritative_tool_outputs',row.get('tool_outputs',[]))
    preview_observed=any(item.get('name')=='service_offer_preview'
        and isinstance(item.get('output'),dict) and not item['output'].get('error') for item in observed)
    # 没读取预览仍是业务失败；只有实际原件违反候选/底线才是
    # 已知严重规则错误，不能将 any([])=False 当作违规证据。
    if preview_observed:serious.add('两原候选与拒买底线')
    if serious.intersection(row.get('independent_checks',{}).get('failed_checks',[])):
        return 'limited_component_known_serious_rule_constraint_failure'
    if row.get('boundary_failure_observation',{}).get('events'):
        return 'limited_component_observed_boundary_failure'
    return None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group(required=True)
    for action in ('freeze','preflight','collect'):actions.add_argument('--'+action,action='store_true')
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--case-subset',nargs='+')
    parser.add_argument('--reps',type=int,default=2);parser.add_argument('--regression',action='store_true')
    parser.add_argument('--source-database',type=Path,default=DEFAULT_PUBLIC)
    parser.add_argument('--overlay-root',type=Path);parser.add_argument('--pricing-json',type=Path)
    parser.add_argument('--method-contract',type=Path)
    parser.add_argument('--baseline-pilot-only',action='store_true',help='仅P01基线1次链路确认，不构成AB或开发门')
    args=parser.parse_args()
    if not args.freeze and (args.case_subset or args.reps!=2 or args.regression or args.overlay_root or args.pricing_json or args.baseline_pilot_only or args.method_contract):
        parser.error('采集/预检只能按已冻结协议运行')
    if args.freeze:result=freeze(args.out,ids=args.case_subset,repetitions=args.reps,regression=args.regression,
        public_database=args.source_database,overlay=args.overlay_root,pricing_json=args.pricing_json,baseline_pilot_only=args.baseline_pilot_only,method_contract=args.method_contract)
    elif args.preflight:
        full=preflight(args.out)
        result={k:v for k,v in full.items() if k!='records'}
        result['case_statuses']=[{'id':r['id'],'status':r['preflight']['status']} for r in full['records']]
    else:result=collect(args.out)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
