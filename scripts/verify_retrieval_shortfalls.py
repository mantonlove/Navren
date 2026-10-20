#!/usr/bin/env python3
"""复现冻结短板题集：隔离库、本地编码、公共原件核对，无云端调用。

例：
  PYTHONPATH=.:tests .venv/bin/python scripts/verify_retrieval_shortfalls.py \
    --suite regression --split dev --encoder dual \
    --translation enabled \
    --output outputs/retrieval-shortfalls/regression-dual-dev.json

原104题保留为回归集；holdout2须显式选择test/all，不能根据其结果调参。
既有验收JSON和执行输入不覆盖；复跑须指定新的输出路径。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib
import json
import os
from pathlib import Path
import resource
import socket
import sqlite3
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_knowledge import LocalKnowledgeProvider
from local_knowledge.store import packed, sha
from local_knowledge.vectors import VectorIndex
from local_knowledge.retrieval_encoder import get_retrieval_encoder, is_bundle, score_policy
from scripts.verify_vector_data_adaptation import (
    PDF_SOURCE, PDF_QUESTIONS, WEBANK_CASES, CATALOG_AS_OF, candidates, canonical_proof, ranked,
    source_inventory,
)

OUT = ROOT / 'outputs/retrieval-shortfalls'
SUITES = {
    'regression': {
        'path': ROOT / 'local_knowledge/acceptance/retrieval_shortfall_cases.json',
        'sha256': '891a5d748fe7e20af9a6ca57b13251cceb8095997027ccaa8ec6ab3eb7f04502',
        'question_count': 104,
        'evaluation_role': 'previously_exposed_regression_keep_first_run_failures',
    },
    'holdout2': {
        'path': ROOT / 'local_knowledge/acceptance/retrieval_shortfall_holdout2.json',
        'sha256': '1785faa7be4329269d75199740e57fa1541e5d291ed480991c07d3c8ccaf84bc',
        'question_count': 23,
        'evaluation_role': 'previously_exposed_followup_regression',
    },
    'blind3': {
        'path': ROOT / 'outputs/rag-structure-repair-20261011/blind3.json',
        'sha256': '1aab0535cdb1cf351390d5f054b6712a300bab8e9e41b589b100d2e0f7425653',
        'question_count': 16,
        'evaluation_role': 'previously_exposed_structure_followup_regression',
    },
    'blind4': {
        'path': ROOT / 'outputs/rag-structure-repair-20261011/blind4.json',
        'sha256': 'f98aec441da6732ec752424298f44f106add4578c177d538dc3967718e86a4be',
        'question_count': 8,
        'evaluation_role': 'previously_exposed_structure_v3_regression',
    },
}
CASE_SCHEMA = 'navren_retrieval_shortfall_cases/v1'
OLD_CODES = ('000001', '000006', '000008', '17321D', 'GRKYB0301', 'B99353')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def output_path(value):
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(OUT.resolve()) or path.suffix != '.json':
        raise ValueError('output_outside_isolated_acceptance_directory')
    return path


def reset_database(path):
    if not path.resolve().is_relative_to(OUT.resolve()) or not path.name.startswith('shortfalls-'):
        raise ValueError('database_destination_out_of_scope')
    path.parent.mkdir(parents=True, exist_ok=True)
    for item in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
        if item.exists(): item.unlink()


def backup(source, target):
    reset_database(target)
    reader = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    writer = sqlite3.connect(target)
    try: reader.backup(writer)
    finally: writer.close(); reader.close()


def select_encoder(name):
    # 与正式运行路径共用选择器；不猜模块工厂，不改变宿主原有环境。
    with patch.dict(os.environ, {'NAVREN_VECTOR_ENCODER': name}):
        encoder = get_retrieval_encoder()
    members = encoder.members if is_bundle(encoder) else (encoder,)
    module_names = {
        'bge': ('local_knowledge.embeddings',),
        'e5': ('local_knowledge.multilingual_embeddings',),
        'aligned': ('local_knowledge.aligned_embeddings',),
        'dual': ('local_knowledge.multilingual_embeddings', 'local_knowledge.aligned_embeddings'),
    }
    if len(members) != len(module_names[name]):
        raise ValueError('selected_encoder_profile_count_invalid')
    profiles = []
    for member, module_name in zip(members, module_names[name]):
        module = importlib.import_module(module_name)
        minimum, distance = score_policy(member)
        profiles.append({
            'repository': module.MODEL_REPOSITORY, 'revision': module.MODEL_REVISION,
            'model_name': member.model_name, 'profile_id': member.profile_id,
            'dimension': member.dimension,
            'model_bytes': sum(item['size'] for item in module.MODEL_ARTIFACTS.values()),
            'artifact_manifest': module.MODEL_ARTIFACTS,
            'score_policy': {'minimum_similarity': minimum, 'maximum_score_distance': distance},
        })
    primary = profiles[0]
    return encoder, {
        'name': name, 'repository': primary['repository'], 'revision': primary['revision'],
        'profile_id': primary['profile_id'], 'dimension': primary['dimension'],
        'compatibility_profile_scope': 'primary_only' if is_bundle(encoder) else 'single_profile',
        'architecture': 'independent_profiles' if is_bundle(encoder) else 'single_profile',
        'model_bytes': sum(profile['model_bytes'] for profile in profiles),
        'profiles': profiles,
        'score_policy': {'mode': 'independent_profiles', 'profiles': {
            profile['profile_id']: profile['score_policy'] for profile in profiles
        }} if is_bundle(encoder) else primary['score_policy'],
    }


def translation_metadata(mode):
    """纯固定描述；元数据阶段不创建翻译会话，不验证或下载模型资产。"""
    module = importlib.import_module('local_knowledge.query_translation')
    descriptor = module.translation_profile_metadata()
    return {
        'requested_mode': mode, 'enabled': mode == 'enabled',
        'role': 'local_query_feature_generator_not_embedding_space',
        'runtime_ready': 'not_checked_by_metadata',
        'lazy_session_initialization': True,
        'pinned_artifact_bytes': sum(item['size'] for item in module.MODEL_ARTIFACTS.values()),
        'disk_bytes_scope': 'pinned_model_artifacts_only_not_peak_rss',
        'metadata': descriptor,
    }


def translation_runtime_summary(results):
    """仅统计实际提供器的固定诊断；译文不进入验收报告或基线。"""
    diagnostics = [item['retrieval']['query_translation'] for item in results
                   if isinstance(item['retrieval'].get('query_translation'), dict)]
    infrastructure_codes = {
        'query_translation_model_missing', 'query_translation_model_invalid',
        'query_translation_dependency_missing', 'query_translation_timeout',
        'query_translation_inference_failed', 'query_translation_output_invalid',
        'query_translation_generation_limit', 'query_translation_generation_incomplete',
    }
    used = [meta for meta in diagnostics if meta.get('used') is True]
    failures = [meta for meta in diagnostics if meta.get('reason') in infrastructure_codes]
    rejected = [meta for meta in diagnostics if meta.get('reason')
                and meta['reason'] != 'query_translation_disabled'
                and meta['reason'] not in infrastructure_codes]
    return {
        'diagnostic_count': len(diagnostics), 'no_diagnostic_count': len(results) - len(diagnostics),
        'used_count': len(used), 'failure_count': len(failures),
        'controlled_rejection_count': len(rejected),
        'reason_counts': dict(Counter(meta.get('reason') or
                                     ('used' if meta.get('used') is True else 'not_applicable')
                                     for meta in diagnostics)),
        'used_profile_counts': dict(Counter(meta.get('profile_id') for meta in used)),
        'runtime_status': 'failed_or_partial_failure' if failures else
                          'used_with_controlled_rejections' if used and rejected else
                          'used_without_reported_failure' if used else 'not_exercised',
        'quality_scope': 'runtime_feature_availability_only_not_translation_semantic_accuracy',
    }


def build_summary(built, status):
    """双路只累计存储计数；数学空间和构建状态按profile分别保留。"""
    profiles = built.get('profiles', [built])
    status_profiles = {profile.get('profile_id'): profile
                       for profile in status.get('profiles', [status])}
    return {
        'ready': built.get('ready') is True and status.get('ready') is True,
        'profile_count': len(profiles),
        'total_stored_vector_count': sum(profile.get('vector_count', 0) for profile in profiles),
        'profiles': [{key: profile.get(key, status_profiles.get(profile.get('profile_id'), {}).get(key)) for key in
                      ('profile_id', 'ready', 'dimension', 'chunk_count', 'vector_count',
                       'encoded_chunk_count', 'reused_chunk_count')}
                     for profile in profiles],
    }


def percentile(values, fraction):
    if not values: return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    low, high = int(index), min(int(index) + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (index - low), 3)


def timing_summary(values):
    return {'count': len(values), 'p50_ms': percentile(values, .5),
            'p95_ms': percentile(values, .95), 'max_ms': round(max(values), 3) if values else None}


def prepare_provider(destination, encoder, source=None, catalog=None, codes=()):
    if source:
        backup(source, destination)
    else:
        reset_database(destination)
    provider = LocalKnowledgeProvider(destination, encoder=encoder)
    imported = provider.import_webank_disclosure(catalog, codes=codes, publish=True,
                                                limit=len(codes)) if catalog else None
    with provider.db() as database:
        release_id = database.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()[0]
        rows = {r['id']: dict(r) for r in provider._rows(database, release_id)}
    started = time.monotonic()
    built = provider.build_vector_index(release_id)
    build_seconds = time.monotonic() - started
    with provider.db() as database:
        status = VectorIndex(provider, encoder).status(database, release_id)
    return provider, release_id, rows, {
        'database': str(destination.relative_to(ROOT)), 'release_id': release_id,
        'build_seconds': round(build_seconds, 3), 'build': built, 'index_status': status,
        'build_summary': build_summary(built, status),
        'import': imported, 'inventory': source_inventory(provider, release_id),
    }


def citation_detail(ids, rows, scores=None):
    return [{'citation_id': cid, 'title': rows[cid]['title'],
             'document_id': rows[cid]['document_id'], 'document_version_id': rows[cid]['version_id'],
             'source_url': rows[cid]['source_url'], 'product_id': rows[cid]['product_id'],
             'share_class_id': rows[cid]['share_class_id'], 'paragraph': rows[cid]['paragraph'],
             'field': rows[cid]['paragraph'].rsplit('/', 1)[-1],
             'excerpt': rows[cid]['excerpt'],
             **({'score': round(float(scores[cid]), 7)} if scores and cid in scores else {})}
            for cid in ids]


def retrieve(provider, release_id, rows, request):
    """实际提供器是被验收对象；基线只用于诊断，不参与硬门裁决。"""
    started = time.monotonic()
    actual = provider.search_public(request, release_id)
    actual_ms = (time.monotonic() - started) * 1000
    primary = provider.vector_encoder.primary if is_bundle(provider.vector_encoder) else provider.vector_encoder
    # 旧候选辅助函数的原始余弦基线只能处于同一数学空间。
    # 实际provider调用已经完成，临时替换只影响随后的诊断基线。
    with patch.object(provider, 'vector_encoder', primary):
        eligible, lexical, direct = candidates(provider, release_id, request)
    # 开发集校准要看到门槛前的真实余弦分布，不能只看已截断的候选。
    raw_scores = {}
    if eligible:
        import numpy as np
        with provider.db() as database:
            database.execute('BEGIN')
            _, vector_ids_all, matrix = VectorIndex(provider, primary)._load(database, release_id, primary)
            offsets = [i for i, cid in enumerate(vector_ids_all) if cid in eligible]
            query_vector = np.asarray(primary.encode_query(request['query']), dtype=np.float32)
            for offset, value in zip(offsets, matrix[offsets] @ query_vector):
                cid = vector_ids_all[offset]
                raw_scores[cid] = max(raw_scores.get(cid, -1.0), float(value))
    vector_ids = ranked(direct['scores'], descending=True, limit=request['limit'])
    lexical_ids = ranked(lexical, limit=request['limit'])
    ids = list(dict.fromkeys(actual['citation_ids'] + lexical_ids + vector_ids))
    proof = canonical_proof(provider, release_id, request['as_of'], ids, rows, request)
    return {
        'request': request, 'actual_query_ms': round(actual_ms, 3),
        'citation_ids': actual['citation_ids'], 'coverage_gaps': actual['coverage_gaps'],
        'retrieval': actual['retrieval'], 'canonical_proof': proof,
        'eligible_count': len(eligible), 'direct_vector_reason': direct['reason'],
        'raw_semantic_baseline': {
            'scope': 'primary_only' if is_bundle(provider.vector_encoder) else 'single_profile',
            'profile_id': primary.profile_id, 'model_name': primary.model_name,
            'applies_to': ['raw_cosine', 'vector_candidate_scores',
                          'unthresholded_cosine_top16', 'unthresholded_cosine_summary'],
        },
        'rankings': {
            'actual': citation_detail(actual['citation_ids'], rows),
            'raw_bm25': citation_detail(lexical_ids, rows, lexical),
            'raw_cosine': citation_detail(vector_ids, rows, direct['scores']),
        },
        'vector_candidate_scores': citation_detail(ranked(direct['scores'], descending=True, limit=16),
                                                  rows, direct['scores']),
        'unthresholded_cosine_top16': citation_detail(ranked(raw_scores, descending=True, limit=16), rows, raw_scores),
        'unthresholded_cosine_summary': {
            'candidate_count': len(raw_scores),
            'maximum': round(max(raw_scores.values()), 7) if raw_scores else None,
            'third': round(sorted(raw_scores.values(), reverse=True)[2], 7) if len(raw_scores) >= 3 else None,
        },
    }


def field_evaluation(case, actual, rows):
    expected = case['expected']
    groups = expected.get('field_groups', [{'field': expected['field'], 'value': expected['value'],
                                           'paragraph': expected['paragraph']}]) if 'field' in expected else expected.get('field_groups', [])
    targets = [[cid for cid, row in rows.items()
                if row['product_id'] == 'WEBANK:' + case['code'] and row['paragraph'] == group['paragraph']]
               for group in groups]
    checks = []
    for group, ids in zip(groups, targets):
        if len(ids) != 1: raise ValueError('expected_field_not_unique_in_originals')
        cid = ids[0]
        if rows[cid]['excerpt'] != packed(group['field']) + ':' + packed(group['value']):
            raise ValueError('frozen_field_does_not_match_original')
        checks.append({'field': group['field'], 'expected_citation_id': cid,
                       'hit_at_1': cid in actual['citation_ids'][:1],
                       'hit_at_3': cid in actual['citation_ids'][:3],
                       'hit_at_limit': cid in actual['citation_ids']})
    if expected.get('should_no_support'):
        empty_required = expected.get('require_empty_citations') or case['kind'] == 'identity_conflict'
        role_contract=expected.get('semantic_role_contract')
        semantic_guard=None
        if role_contract:
            gaps=actual['coverage_gaps']
            support=actual['retrieval'].get('semantic_coverage',{}).get('supported_roles',[])
            obj='WEBANK:'+case['code']
            semantic_guard=(all(any(g.get('code')=='unsupported_semantic_role' and g.get('role')==role
                                  and g.get('product_id')==obj for g in gaps)
                                for role in role_contract['unproven_roles'])
                            and not any(item.get('role') in role_contract['unproven_roles']
                                        and item.get('product_id')==obj for item in support))
        return {'field_checks': checks, 'no_fact_support': True,
                'support_annotation_basis': 'frozen_original_review',
                'topic_hits_present': bool(actual['citation_ids']),
                'topic_support_allowed': expected.get('topic_support_allowed', False),
                'runtime_specific_fact_guard': 'not_tested_no_final_answer_or_claim_validator',
                'require_empty_citations': bool(empty_required),
                'guard_pass': not actual['citation_ids'] if empty_required else None,
                'semantic_role_guard_pass':semantic_guard,
                'evaluation_scope': 'identity_time_gate' if empty_required else 'evidence_support_label_only',
                'required_action': expected.get('required_action'),
                'rationale': expected.get('rationale')}
    return {'field_checks': checks, 'all_required_hit_at_1': all(x['hit_at_1'] for x in checks),
            'all_required_hit_at_3': all(x['hit_at_3'] for x in checks),
            'all_required_hit_at_limit': all(x['hit_at_limit'] for x in checks),
            'guard_pass': None}


def webank(case_set, split, provider, release_id, rows):
    results = []
    for case in case_set['webank_cases'] + case_set['webank_special_cases']:
        if split != 'all' and case['split'] != split: continue
        for identity_mode in case['identity_modes']:
            request = {'scope': 'public', 'query': case['query'], 'as_of': case['as_of'],
                       'facets': case['facets'], 'limit': case['limit']}
            if identity_mode == 'explicit_identity':
                request.update(product_id='WEBANK:' + case['code'], channel_id='WEBANK')
            request.update(case.get('request_override', {}))
            actual = retrieve(provider, release_id, rows, request)
            results.append({'id': case['id'], 'split': case['split'], 'kind': case['kind'],
                            'identity_mode': identity_mode, 'code': case['code'],
                            'expected': case['expected'], **actual,
                            'evaluation': field_evaluation(case, actual, rows)})
    return results


def legacy_webank(provider, release_id, rows):
    """复用上一轮冻结的30道字段题，不重新导入或创建第二份产品库。"""
    cases = []
    for previous in WEBANK_CASES:
        paragraph = 'productPresentation:' + previous['code'] + '/' + previous['field']
        originals = [row for row in rows.values() if row['product_id'] == 'WEBANK:' + previous['code']
                     and row['paragraph'] == paragraph]
        if len(originals) != 1: raise ValueError('legacy_expected_field_not_unique')
        field = json.loads('{' + originals[0]['excerpt'] + '}')
        value = field[previous['field']]
        cases.append({**previous, 'id': 'legacy-' + previous['id'], 'split': 'legacy_frozen',
                      'kind': 'legacy_single_field', 'as_of': CATALOG_AS_OF, 'facets': [], 'limit': 3,
                      'identity_modes': ['explicit_identity', 'text_only'],
                      'expected': {'field': previous['field'], 'value': value, 'paragraph': paragraph,
                                   'support_level': 'reference_only' if value in {
                                       '详见产品页', '详见产品页面', '以保险公司核定为准'} else 'field_quote'}})
    return webank({'webank_cases': cases, 'webank_special_cases': []}, 'all', provider, release_id, rows)


def vector_branch_valid(item):
    """身份拒绝是正常安全路径；不能把模型错误或无证据泛化为正常拒绝。"""
    if item['direct_vector_reason'] is not None:
        return False
    reason = item['retrieval']['vector_reason']
    if reason is None:
        return True
    if reason != 'vector_empty_eligible' or item['citation_ids']:
        return False
    expected = item.get('expected', {})
    explicitly_empty = expected.get('should_no_support') and (
        expected.get('require_empty_citations') or item.get('kind') == 'identity_conflict')
    actual_identity_gap = any(gap['code'] in {'identity_conflict', 'identity_ambiguous', 'identity_unverified'}
                              for gap in item['coverage_gaps'])
    return bool(explicitly_empty or actual_identity_gap)


def span_covered(group, ids, rows):
    intervals = sorted((rows[cid]['start_char'], rows[cid]['end_char']) for cid in ids
                       if rows[cid]['version_id'] == group['document_version_id'])
    cursor = group['span_start_char']
    for start, end in intervals:
        if end <= cursor: continue
        if start > cursor: break
        cursor = end
        if cursor >= group['span_end_char']: return True
    return False


def market(case_set, split, provider, release_id, rows):
    results = []
    for case in case_set['market_cases']:
        if split != 'all' and case['split'] != split: continue
        request = {'scope': 'public', 'query': case['query'], 'as_of': case['as_of'],
                   'facets': case['facets'], 'limit': case['limit'], **case.get('request_override', {})}
        actual = retrieve(provider, release_id, rows, request)
        expected = case['expected']; ids = actual['citation_ids']
        if expected.get('should_no_support'):
            evaluation = {'no_fact_support': True, 'support_annotation_basis': 'frozen_original_review',
                          'topic_hits_present': bool(ids), 'topic_support_allowed': True,
                          'runtime_specific_fact_guard': 'not_tested_no_final_answer_or_claim_validator',
                          'guard_pass': None, 'evaluation_scope': 'evidence_support_label_only',
                          'rationale': expected['rationale']}
        else:
            groups = expected['evidence_groups']
            group_checks = []
            for group in groups:
                doc = group['document_id']
                group_checks.append({'document_id': doc, 'title': group['title'],
                    'required_original_span': group['required_original_span'],
                    'document_hit_at_1': any(rows[cid]['document_id'] == doc for cid in ids[:1]),
                    'document_hit_at_3': any(rows[cid]['document_id'] == doc for cid in ids[:3]),
                    'document_hit_at_limit': any(rows[cid]['document_id'] == doc for cid in ids),
                    'span_hit_at_3': span_covered(group, ids[:3], rows),
                    'span_hit_at_limit': span_covered(group, ids, rows)})
            evaluation = {'evidence_group_checks': group_checks,
                          'any_document_hit_at_1': any(x['document_hit_at_1'] for x in group_checks),
                          'all_documents_hit_at_3': all(x['document_hit_at_3'] for x in group_checks),
                          'all_documents_hit_at_limit': all(x['document_hit_at_limit'] for x in group_checks),
                          'all_required_spans_hit_at_3': all(x['span_hit_at_3'] for x in group_checks),
                          'all_required_spans_hit_at_limit': all(x['span_hit_at_limit'] for x in group_checks),
                          'guard_pass': None}
        results.append({'id': case['id'], 'split': case['split'], 'kind': case['kind'],
                        'expected': expected, **actual, 'evaluation': evaluation})
    return results


def pdf(provider, release_id, rows):
    """旧PDF冻结题只作为不回退检查，不混入新增heldout分数。"""
    results = []
    for case in json.loads(PDF_QUESTIONS.read_bytes())['cases']:
        actual = retrieve(provider, release_id, rows, {'scope': 'public', **case['request']})
        ids = actual['citation_ids']
        keys = {rows[cid]['paragraph'].split('/', 1)[-1] for cid in ids}
        combined = '\n'.join(rows[cid]['excerpt'] for cid in ids)
        results.append({'id': case['id'], **actual, 'evaluation': {
            'required_keys_covered': set(case['expected_review_keys']).issubset(keys),
            'required_original_fragments_present': all(fragment in combined
                                                      for fragment in case['required_original_fragments'])}})
    return results


def summarize(results, domain):
    summary = {'executions': len(results), 'by_split': dict(Counter(x.get('split', 'existing_pdf') for x in results)),
               'query_latency': timing_summary([x['actual_query_ms'] for x in results]),
               'canonical_failures': sum(not x['canonical_proof']['all_valid'] for x in results),
               'vector_failures': sum(not vector_branch_valid(x) for x in results)}
    summary['query_translation'] = translation_runtime_summary(results)
    negatives = [x for x in results if x['evaluation'].get('no_fact_support')]
    summary['no_fact_support_original_annotations'] = len(negatives)
    summary['negative_cases_with_theme_hits'] = sum(x['evaluation']['topic_hits_present'] for x in negatives)
    guarded = [x for x in results if x['evaluation'].get('guard_pass') is not None]
    summary['identity_or_time_guard'] = {'total': len(guarded),
                                        'passed': sum(x['evaluation']['guard_pass'] for x in guarded)}
    summary['specific_fact_final_answer_guard'] = 'not_tested'
    failures = []; categories = defaultdict(lambda: Counter())
    for item in results:
        evaluation = item['evaluation']
        if domain == 'webank' and not evaluation.get('no_fact_support'):
            for check in evaluation['field_checks']:
                count = categories[check['field']]; count['total'] += 1
                for key in ('hit_at_1', 'hit_at_3', 'hit_at_limit'): count[key] += check[key]
            if not evaluation['all_required_hit_at_limit']: failures.append(item['id'] + '/' + item['identity_mode'])
        elif domain == 'market' and not evaluation.get('no_fact_support'):
            count = categories[item['kind']]; count['total'] += 1
            for key in ('any_document_hit_at_1', 'all_documents_hit_at_3', 'all_documents_hit_at_limit',
                        'all_required_spans_hit_at_3', 'all_required_spans_hit_at_limit'):
                count[key] += evaluation[key]
            if not evaluation['all_required_spans_hit_at_limit']: failures.append(item['id'])
        elif domain == 'pdf':
            count = categories['existing_frozen_pdf']; count['total'] += 1
            for key in ('required_keys_covered', 'required_original_fragments_present'): count[key] += evaluation[key]
            if not all(evaluation.values()): failures.append(item['id'])
        if evaluation.get('guard_pass') is False: failures.append(item['id'])
        if evaluation.get('semantic_role_guard_pass') is False: failures.append(item['id']+'/semantic_role_guard')
    summary['by_field_or_topic'] = {name: dict(count) for name, count in categories.items()}
    summary['failures'] = failures
    return summary


def verify_frozen(case_set):
    """只核预期与原件，不调用检索；不从已有答案倒推标签。"""
    for code, record in case_set['sources']['webank']['selected_records'].items():
        if record['record']['code'] != code or sha(packed(record['record'])) != record['record_sha256']:
            raise ValueError('frozen_product_record_invalid')
    market_source = ROOT / case_set['sources']['market']['path']
    reader = sqlite3.connect(market_source.resolve().as_uri() + '?mode=ro', uri=True)
    reader.row_factory = sqlite3.Row
    try:
        for case in case_set['market_cases']:
            for group in case['expected'].get('evidence_groups', []):
                row = reader.execute('SELECT * FROM document_versions WHERE id=?',
                                     (group['document_version_id'],)).fetchone()
                doc = reader.execute('SELECT * FROM documents WHERE id=?', (group['document_id'],)).fetchone()
                if (not row or not doc or row['document_id'] != group['document_id']
                        or row['parsed_sha256'] != group['parsed_sha256'] or row['title'] != group['title']
                        or doc['source_url'] != group['source_url']
                        or row['parsed_text'][group['span_start_char']:group['span_end_char']] != group['required_original_span']):
                    raise ValueError('frozen_market_original_mismatch')
    finally: reader.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--suite', choices=tuple(SUITES), default='regression',
                        help='原104题为regression；独立后续题为holdout2')
    parser.add_argument('--split', choices=('dev', 'test', 'all'), default='dev')
    parser.add_argument('--encoder', choices=('bge', 'e5', 'aligned', 'dual'), default='bge')
    parser.add_argument('--translation', choices=('enabled', 'disabled'), default='disabled',
                        help='本地query翻译特征候选，默认关闭；不作为事实或embedding profile')
    parser.add_argument('--structure',choices=('enabled','disabled'),default='disabled',help='句段父子索引候选，默认关闭')
    parser.add_argument('--rerank',choices=('enabled','disabled'),default='disabled',help='本地配对相关性重排候选，默认关闭')
    parser.add_argument('--output', help='仅限 outputs/retrieval-shortfalls 内的JSON路径')
    args = parser.parse_args()
    if args.suite in ('holdout2','blind3','blind4') and args.split == 'dev':
        parser.error('后续题集只有test题；需显式指定 --split test 或 --split all')
    translation_suffix = '-translation-enabled' if args.translation == 'enabled' else ''
    destination = output_path(args.output or str(OUT / f'{args.suite}-{args.encoder}-{args.split}{translation_suffix}.json'))
    execution_input = destination.with_name(destination.stem + '-execution-input.json')
    if destination.exists() or execution_input.exists():
        parser.error('既有验收产物不可覆盖；请用 --output 指定新的JSON路径')
    suite = SUITES[args.suite]
    case_path, frozen_sha = suite['path'], suite['sha256']
    case_bytes = case_path.read_bytes()
    if sha(case_bytes) != frozen_sha: raise ValueError('frozen_case_file_changed')
    case_set = json.loads(case_bytes)
    if case_set.get('schema') != CASE_SCHEMA:
        raise ValueError('frozen_case_schema_invalid')
    case_groups = {key: case_set[key] for key in ('webank_cases', 'webank_special_cases', 'market_cases')}
    case_count = sum(len(group) for group in case_groups.values())
    if case_count != suite['question_count']:
        raise ValueError('frozen_suite_question_count_invalid')
    selected_case_count = sum(args.split == 'all' or case['split'] == args.split
                              for group in case_groups.values() for case in group)
    if not selected_case_count:
        raise ValueError('selected_suite_split_empty')
    suite_metadata = {
        'name': args.suite, 'case_file': str(case_path.relative_to(ROOT)),
        'frozen_case_file_sha256': frozen_sha, 'question_count': case_count,
        'selected_question_count': selected_case_count,
        'evaluation_role': suite['evaluation_role'],
        'benchmark_id': case_set.get('benchmark_id', args.suite),
        'first_round_regression': case_set.get('first_round_regression'),
        'previous_acceptance_artifacts_preserved': True,
        'case_expectations_modified': False,
    }
    verify_frozen(case_set)
    catalog_path = ROOT / case_set['sources']['webank']['path']
    market_path = ROOT / case_set['sources']['market']['path']
    sources = (case_path, catalog_path, market_path, PDF_SOURCE, PDF_QUESTIONS)
    source_hashes = {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in sources}
    if source_hashes[str(catalog_path.relative_to(ROOT))] != case_set['sources']['webank']['sha256']:
        raise ValueError('frozen_catalog_file_changed')
    if source_hashes[str(market_path.relative_to(ROOT))] != case_set['sources']['market']['sha256']:
        raise ValueError('frozen_market_file_changed')
    catalog = catalog_path.read_bytes(); wrapper = catalog.decode('utf-8')
    decoded = json.loads(wrapper[wrapper.index('(') + 1:wrapper.rindex(')')])
    for code, frozen in case_set['sources']['webank']['selected_records'].items():
        if decoded['productPresentation'][frozen['row_index_zero_based']] != frozen['record']:
            raise ValueError('frozen_catalog_row_changed')
    code_inputs = [ROOT / name for name in ('scripts/verify_retrieval_shortfalls.py',
        'scripts/verify_vector_data_adaptation.py', 'local_knowledge/store.py',
        'local_knowledge/structure_index.py','local_knowledge/retrieval_units.py','local_knowledge/field_semantics.py',
        'local_knowledge/evidence_group_selection.py','local_knowledge/answer_form_preference.py',
        'local_knowledge/query_segments.py','local_knowledge/query_group_retrieval.py','local_knowledge/source_coverage.py',
        'local_knowledge/reranking.py','scripts/provision_reranker.py',
        'agent_runtime/knowledge_retrieval.py', 'agent_runtime/knowledge_semantics.py',
        'agent_runtime/data_boundary.py',
        'local_knowledge/vectors.py', 'local_knowledge/vector_documents.py',
        'local_knowledge/importers.py', 'local_knowledge/embeddings.py',
        'local_knowledge/multilingual_embeddings.py', 'local_knowledge/aligned_embeddings.py',
        'local_knowledge/retrieval_encoder.py', 'local_knowledge/retrieval_ranking.py',
        'local_knowledge/public_aliases.py', 'scripts/build_local_vectors.py',
        'local_knowledge/query_features.py', 'local_knowledge/query_translation.py',
        'local_knowledge/public_institutions.py', 'scripts/provision_query_translation.py')]
    code_inputs.extend(path for path in (ROOT / 'local_knowledge').rglob('public_aliases*.py')
                       if path.is_file() and path not in code_inputs)
    code_hashes = {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in code_inputs}
    selected = list(case_set['sources']['webank']['selected_records'])
    atomic_json(execution_input, {
        'suite': suite_metadata, 'split': args.split, 'encoder': args.encoder,
        'translation': args.translation,
        'structure': args.structure,
        'rerank': args.rerank,
        'environment_overrides': {'NAVREN_QUERY_TRANSLATION_ENABLED':
                                  '1' if args.translation == 'enabled' else '0',
                                  'NAVREN_STRUCTURE_ENABLED':'1' if args.structure=='enabled' else '0',
                                  'NAVREN_ANSWER_FORM_ENABLED':os.environ.get('NAVREN_ANSWER_FORM_ENABLED','0'),
                                  'NAVREN_RERANK_ENABLED':'1' if args.rerank=='enabled' else '0'},
        'frozen_case_file_sha256': frozen_sha,
        'source_hashes': source_hashes, 'code_hashes': code_hashes,
        'selected_product_codes': selected, 'product_distractors': list(OLD_CODES),
        'legacy_question_set_sha256': sha(packed(WEBANK_CASES)),
        'thresholds': 'profile_specific_score_policy_loaded_with_encoder',
        'test_tuning_forbidden': True})
    network_attempts = []
    def forbidden(*values, **keywords):
        network_attempts.append('socket_network_attempt')
        raise AssertionError('network_forbidden_during_local_acceptance')
    started = time.monotonic(); sections = {}
    with patch.dict(os.environ, {'NAVREN_QUERY_TRANSLATION_ENABLED':
                                '1' if args.translation == 'enabled' else '0',
                                'NAVREN_STRUCTURE_ENABLED':'1' if args.structure=='enabled' else '0',
                                'NAVREN_RERANK_ENABLED':'1' if args.rerank=='enabled' else '0'}), \
            patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden), \
            patch.object(socket, 'create_connection', forbidden), patch.object(socket, 'getaddrinfo', forbidden):
        cold_started = time.monotonic(); encoder, model = select_encoder(args.encoder)
        cold = time.monotonic() - cold_started
        model['query_translator'] = translation_metadata(args.translation)
        from local_knowledge.reranking import rerank_profile_metadata
        model['reranker']={'enabled':args.rerank=='enabled','metadata':rerank_profile_metadata()}
        for domain in ('webank', 'market', 'pdf'):
            section_started = time.monotonic()
            database = destination.parent / f'shortfalls-{destination.stem}-{domain}.sqlite3'
            provider, release_id, rows, meta = prepare_provider(database, encoder,
                source=market_path if domain == 'market' else PDF_SOURCE if domain == 'pdf' else None,
                catalog=catalog if domain == 'webank' else None,
                codes=tuple(selected) + OLD_CODES if domain == 'webank' else ())
            results = webank(case_set, args.split, provider, release_id, rows) if domain == 'webank' else \
                      market(case_set, args.split, provider, release_id, rows) if domain == 'market' else \
                      pdf(provider, release_id, rows)
            summary = summarize(results, domain)
            sections[domain] = {**meta, 'cases': results, 'summary': summary,
                                'section_seconds': round(time.monotonic() - section_started, 3)}
            print(json.dumps({'suite': args.suite, 'stage': domain, 'executions': summary['executions'],
                              'failure_count': len(summary['failures']), 'failed_ids': summary['failures'][:16],
                              'build_seconds': meta['build_seconds'],
                              'build_summary': meta['build_summary'],
                              'query_translation': summary['query_translation'],
                              'query_latency': summary['query_latency']},
                             ensure_ascii=False), flush=True)
            if domain == 'webank':
                legacy_started = time.monotonic()
                legacy_results = legacy_webank(provider, release_id, rows)
                legacy_summary = summarize(legacy_results, 'webank')
                sections['legacy_webank'] = {
                    'database': meta['database'], 'release_id': release_id,
                    'inventory': meta['inventory'],
                    'question_set_sha256': sha(packed(WEBANK_CASES)), 'question_count': len(WEBANK_CASES),
                    'cases': legacy_results, 'summary': legacy_summary,
                    'section_seconds': round(time.monotonic() - legacy_started, 3),
                    'scope': 'previous_30_fields_on_same_14_product_isolated_database'}
                print(json.dumps({'suite': args.suite, 'stage': 'legacy_webank', 'questions': len(WEBANK_CASES),
                                  'executions': legacy_summary['executions'],
                                  'failure_count': len(legacy_summary['failures']),
                                  'failed_ids': legacy_summary['failures'][:16],
                                  'query_translation': legacy_summary['query_translation'],
                                  'query_latency': legacy_summary['query_latency']}, ensure_ascii=False), flush=True)
    all_results = [item for section in sections.values() for item in section['cases']]
    translation_runtime = translation_runtime_summary(all_results)
    model['query_translator']['runtime'] = translation_runtime
    original_checks = {
        'zero_python_network_attempts': not network_attempts,
        'source_files_unchanged': all(sha(path.read_bytes()) == source_hashes[str(path.relative_to(ROOT))] for path in sources),
        'code_files_unchanged': all(sha(path.read_bytes()) == code_hashes[str(path.relative_to(ROOT))] for path in code_inputs),
        'canonical_citation_checks': all(item['canonical_proof']['all_valid'] for item in all_results),
        'vector_branch_checks': all(vector_branch_valid(item) for item in all_results),
        'all_selected_profiles_built_and_ready': all(
            sections[domain]['build_summary']['ready']
            and {item['profile_id'] for item in sections[domain]['build_summary']['profiles']}
                == {item['profile_id'] for item in model['profiles']}
            for domain in ('webank', 'market', 'pdf')),
        'public_raw_payload_hashes_valid': all(section['inventory']['raw_payload_hashes_valid'] for section in sections.values()),
        'all_selected_official_products_imported': sections['webank']['import']['imported'] == len(set(selected).union(OLD_CODES)),
    }
    retrieval_checks = {f'{name}_frozen_expectations': not section['summary']['failures']
                        for name, section in sections.items()}
    if args.structure=='enabled':
        original_checks['structure_index_built']=sections['market']['build'].get('structure_index',{}).get('ready') is True
        market_results=sections['market']['cases']
        original_checks['structure_used_and_integrity_valid']=all(
            item['retrieval'].get('structure_index',{}).get('ready') is True
            and item['retrieval']['structure_index'].get('used') is True
            and item['retrieval']['structure_index'].get('reason') is None
            for item in market_results if item['citation_ids'])
    if args.translation == 'enabled':
        # 模型缺失或推理失败不能靠原路径恰好过题来冒充该候选已验收。
        original_checks['query_translation_used_and_no_runtime_failure'] = (
            translation_runtime['used_count'] > 0 and translation_runtime['failure_count'] == 0)
        original_checks['query_translation_profile_matches_pin'] = all(
            profile == model['query_translator']['metadata']['profile_id']
            for profile in translation_runtime['used_profile_counts'])
    else:
        original_checks['query_translation_disabled_by_cli'] = translation_runtime['used_count'] == 0
    if args.rerank=='enabled':
        rerank_results=[item['retrieval'].get('reranking',{})
                        for item in sections['market']['cases'] if item['citation_ids']]
        original_checks['reranker_used_and_no_runtime_failure']=bool(rerank_results) and all(
            meta.get('used') is True and meta.get('reason') is None
            and not meta.get('segment_failures') for meta in rerank_results)
        original_checks['reranker_profile_matches_pin']=all(
            meta.get('profile_id')==model['reranker']['metadata']['profile_id']
            for meta in rerank_results if meta.get('used'))
        model['reranker']['runtime']={'eligible_executions':len(rerank_results),
            'used_count':sum(meta.get('used') is True for meta in rerank_results),
            'failure_count':sum(meta.get('reason') is not None for meta in rerank_results),
            'maximum_pool_count':max((meta.get('pool_count',0) for meta in rerank_results),default=0)}
    all_checks = {**original_checks, **retrieval_checks}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {'schema': 'navren_retrieval_shortfalls_acceptance/v1',
        'status': 'provider_retrieval_pass_answer_guard_unverified' if all(all_checks.values()) else 'failed',
        'executed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'suite': suite_metadata, 'split': args.split, 'model': model,
        'translation': args.translation,
        'structure': args.structure,'rerank': args.rerank,
        'frozen_case_file_sha256': frozen_sha, 'source_hashes': source_hashes, 'code_hashes': code_hashes,
        'model_mode': 'real_local_onnx_cpu', 'data_mode': 'fixed_public_official_originals_isolated_copies',
        'cold_load_seconds': round(cold, 3), 'elapsed_seconds': round(time.monotonic() - started, 3),
        'query_latency': timing_summary([item['actual_query_ms'] for item in all_results]),
        'process_max_rss_bytes': rss if sys.platform == 'darwin' else rss * 1024,
        'network_attempt_count': len(network_attempts), 'cloud_model_calls': 0,
        'thresholds': model['score_policy'],
        'checks': all_checks, **sections,
        'limitations': ['未写当前运行知识库，未部署服务，未调用云端模型或个人数据库。',
            '负面具体事实的不支持标签来自冻结原件审阅，主题hits允许存在；这不等于系统已拒答。',
            '未执行最终回答或具体claim验证器，runtime_specific_fact_guard均标记not_tested。',
            'dev可诊断通用算法；test结果不能用于修改题集、排序参数或阈值。',
            '原104题已成为回归集，旧失败记录保留，不声称其后续结果为首次独立验收。',
            '原始BM25和直接余弦只用于诊断；新增排序和安全门由实际provider路径验收。',
            'dual的原始语义诊断为primary_only；不同profile的余弦不合并或比较。',
            '本地译文仅作额外query特征；翻译器独立记录，不加入embedding数学profile或事实原文。',
            '翻译固定资产字节不等于峰值内存，纯元数据不证明会话可用或翻译质量。',
            '翻译runtime统计只验证使用/失效/受控拒绝；未单独检验每条译文的语义或否定保真。',
            'cold_load_seconds包括验收所选全部profile的初始化；不等同于后台按需加载时延。',
            'Python socket守卫不构成全系统网络审计；process RSS不是后台服务增量内存。']}
    atomic_json(destination, result)
    print(json.dumps({'suite': args.suite, 'status': result['status'], 'passed_checks': sum(all_checks.values()),
                      'check_count': len(all_checks), 'output': str(destination),
                      'query_translation': translation_runtime,
                      'elapsed_seconds': result['elapsed_seconds']}, ensure_ascii=False), flush=True)
    return 0 if all(all_checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
