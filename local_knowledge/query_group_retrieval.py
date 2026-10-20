"""追加公共子问句召回；原请求与权威准入始终由 store 保持。

本模块只读当前已准入公共句组。附加句子、译文和原件文本留在进程内，
返回诊断只包含固定政策、计数、原因及公共组 ID。评分不构成事实验证。
"""
from __future__ import annotations

import os
import hashlib
import json
import time
import re
import math
from .query_segments import build_query_segments, POLICY as SEGMENT_POLICY

MAX_CONTEXT_ALTERNATIVES = 512
QUERY_VIEW_POLICY = 'original-exact-geometry-and-script-aligned-rerank-equal-rrf/4'


def _script_language(value):
    """仅识别明确汉字/拉丁字母脚本；混合或未知文本保守保留两视图。"""
    if not isinstance(value,str):
        return None
    han=len(re.findall(r'[\u3400-\u9fff]',value))
    latin=len(re.findall(r'[A-Za-z]',value))
    if sum(char.isalpha() for char in value)!=han+latin:
        return None
    total=han+latin
    if not total:
        return None
    if han/total>=0.8:
        return 'han'
    if latin/total>=0.8:
        return 'latin'
    return None


def with_context_candidates(structure):
    """上下文按完整范围单独评分、单独支付引用预算，不借分给裸核心。"""
    groups = {group['group_id']: dict(group) for group in structure.get('evidence_groups', ())}
    contexts = {}
    from .evidence_group_selection import MAX_CANDIDATES
    ordered = sorted(groups.values(),key=lambda group:(
        min(group.get('recall_rank_positions',{}).values(),default=20001),
        group.get('score',0.0),group['group_id']))
    maximum = min(MAX_CONTEXT_ALTERNATIVES,max(0,MAX_CANDIDATES-len(groups)))
    for group in ordered:
        if not group.get('complete') or group.get('kind') != 'sentence_core':
            continue
        for option in group.get('context_options', ()):
            if not option.get('complete'):
                continue
            identity = [option.get('version_id'), option.get('boundary_key'),
                        option.get('start_char'), option.get('end_char'), option['citation_ids']]
            gid = 'cg-' + hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            positions = group.get('recall_rank_positions', {})
            if gid in contexts:
                previous = contexts[gid]
                for key, value in positions.items():
                    previous['recall_rank_positions'][key] = min(previous['recall_rank_positions'].get(key,value),value)
                previous['score'] = min(previous['score'],group['score'])
            else:
                if len(contexts)>=maximum:
                    continue
                candidate = dict(option,group_id=gid,kind='context_core',core=True,
                                 score=group['score'],context_options=[],
                                 recall_rank_positions=dict(positions))
                candidate.pop('required_group_id',None)
                contexts[gid] = candidate
    return dict(structure,evidence_groups=[*groups.values(),*contexts.values()],
                context_candidate_count=len(contexts))


def merge_query_groups(structure, segment_structures):
    groups = {}
    for index, current in enumerate((structure, *segment_structures)):
        for raw in current.get('evidence_groups', ()):
            group = dict(raw)
            gid = group['group_id']
            positions = {'q'+str(index)+'.'+key: value
                         for key, value in group.get('recall_rank_positions', {}).items()}
            if gid in groups:
                existing = groups[gid]
                identity = ('kind', 'version_id', 'citation_ids', 'start_char', 'end_char', 'complete',
                            'boundary_key','canonical_anchor_id','window_policy')
                if any(existing.get(key) != group.get(key) for key in identity):
                    raise ValueError('query_group_identity_mismatch')
                existing['recall_rank_positions'].update(positions)
                ranks = [value for value in (existing.get('child_rank'), group.get('child_rank'))
                         if type(value) is int and value > 0]
                existing['child_rank'] = min(ranks) if ranks else None
                existing['score'] = min(existing['score'], group['score'])
            else:
                group['recall_rank_positions'] = positions
                groups[gid] = group
    return list(groups.values())


def additional_query_structures(provider, db, release_id, query, eligible_ids, eligible_rows,
                                structure):
    """最多三个额外公共查询；复用完整请求的 eligible 集合与固定发布。"""
    if os.environ.get('NAVREN_RERANK_ENABLED', '0') != '1':
        return structure, [], {'policy': SEGMENT_POLICY, 'used': False,
                               'count': 1, 'reason': 'reranking_disabled'}
    structure = with_context_candidates(structure)
    segments = build_query_segments(query)
    diagnostic = {key: segments[key] for key in ('policy', 'used', 'count', 'reason')}
    if not segments['used']:
        return structure, [], diagnostic
    from .structure_index import StructureIndex
    from .public_aliases import query_projection, concept_coverage
    from .retrieval_encoder import cross_language_query, is_bundle, selection
    from .query_features import translated_feature
    from .embeddings import VectorError
    dual = is_bundle(provider.vector_encoder)
    if provider.vector_encoder is None:
        try: dual = selection() == 'dual'
        except VectorError: dual = False
    extra = []
    translated_queries = []
    for value in segments['queries']:
        projection = query_projection(value)
        cross = cross_language_query(value, eligible_rows)
        applicable = (cross and dual and provider.vector_encoder is not False
                      and os.environ.get('NAVREN_VECTOR_ENABLED', '1') != '0')
        translation, _ = translated_feature(value, applicable=applicable)
        translated_queries.append(translation)
        current = StructureIndex(provider, provider.vector_encoder).search(
            db, release_id, value, eligible_ids, projected_query=projection,
            translated_query=translation,
            cross_language=cross and (not projection or concept_coverage(value) < 0.5))
        extra.append(with_context_candidates(current))
    merged = dict(structure)
    merged['evidence_groups'] = merge_query_groups(structure, extra)
    # 原查询失败不被子句的部分成功掩盖。
    diagnostic['ready_count'] = sum(item.get('ready') is True for item in extra)
    diagnostic['failed_count'] = sum(item.get('reason') is not None for item in extra)
    return merged, [(value, item, translated) for value, item, translated in zip(segments['queries'], extra, translated_queries)], diagnostic


def rank_query_groups(db, query, groups, rows, segment_structures, *, translated_query=None):
    from .reranking import rerank_groups, MAX_CALL_SECONDS
    deadline = time.monotonic() + MAX_CALL_SECONDS
    versions = {row['version_id'] for row in rows}
    parsed_texts = {}
    for version_id in versions:
        row = db.execute('SELECT parsed_text,parsed_sha256 FROM document_versions WHERE id=?',
                         (version_id,)).fetchone()
        if row is not None:
            parsed_texts[version_id] = dict(row)
    def rank_views(value, candidates, translated=None):
        from .vectors import RRF_K
        from .source_coverage import apply_source_coverage
        from .reranking import _candidate_pool
        candidates,source_coverage=apply_source_coverage(value,candidates,rows)
        common_ids,common_branch_counts=_candidate_pool([item for item in candidates if not item.get('rejection')])
        common=set(common_ids)
        candidates=[item for item in candidates if item['group_id'] in common]
        results=[];views=[];view_names=[];lane_counts={}
        languages={}
        for candidate in candidates:
            record=parsed_texts.get(candidate.get('version_id'),{})
            text=record.get('parsed_text','')
            start,end=candidate.get('start_char'),candidate.get('end_char')
            if type(start) is int and type(end) is int and 0<=start<end<=len(text):
                languages[candidate['group_id']]=_script_language(text[start:end])
        aligned=(_script_language(value)=='han' and _script_language(translated)=='latin')
        for feature in dict.fromkeys(item for item in (value,translated) if item):
            lane='latin' if aligned and feature==translated else 'han' if aligned else 'all'
            current=[candidate for candidate in candidates
                     if lane=='all' or languages.get(candidate['group_id']) in (None,lane)]
            lane_counts[lane]=len(current)
            if not current:
                continue
            if time.monotonic() >= deadline:
                results.append({'used':False,'reason':'rerank_timeout','profile_id':None})
                continue
            # 同几何代表的资格已在共同池确定；分语言只能继承子集，不能
            # 用代表的弱原始 rank 再裁一次，也不能给 lane 追加候选。
            ordered,meta=rerank_groups(feature,current,rows,parsed_texts,deadline=deadline,
                                     preselected_ids=[item['group_id'] for item in current])
            results.append(meta)
            if meta.get('used'):
                views.append({gid:index+1 for index,gid in enumerate(ordered)})
                view_names.append('translated' if feature!=value else 'original')
        if not views:
            return [],dict(results[0] if results else {'used':False,'reason':'rerank_empty_candidates'},
                           query_view_count=0)
        ids=set().union(*(set(view) for view in views))
        score={gid:sum(1/(RRF_K+view[gid]) for view in views if gid in view)
                   /sum(gid in view for view in views) for gid in ids}
        by_id={item['group_id']:item for item in candidates}
        # 模型相关性是追加视图，不能清空已经由真实 canonical/sentence
        # 分支确定的证据次序。原检索的RRF政策与阈值不变；两视图等权。
        for gid in ids:
            original=by_id[gid].get('fused_score')
            if (not isinstance(original,bool) and isinstance(original,(int,float))
                    and math.isfinite(original) and -1/(RRF_K+1)<=original<0):
                score[gid]=(score[gid]-original)/2
        ordered=sorted(ids,key=lambda gid:(-score[gid],
            by_id[gid].get('source_scope_rank',20001),
            by_id[gid].get('fused_score',by_id[gid].get('score',0.0)),gid))
        if time.monotonic() >= deadline:
            return [],dict(results[0],used=False,reason='rerank_timeout',scored_count=0,
                           ranked_groups=[],query_view_count=0)
        successful=next(meta for meta in results if meta.get('used'))
        failures=[meta.get('reason') for meta in results if not meta.get('used')]
        partials=[meta.get('reason') for meta in results if meta.get('used') and meta.get('reason')]
        window_counts={}
        for result in results:
            if result.get('used'):
                for item in result.get('ranked_groups', ()):
                    window_counts[item['group_id']]=max(window_counts.get(item['group_id'],0),item['window_count'])
        rejection_counts={};admission_counts={}
        for result in results:
            for name,target in (('candidate_rejections',rejection_counts),
                                ('admission_filtered_groups',admission_counts)):
                for code,count in result.get(name,{}).items():
                    target[code]=target.get(code,0)+count
        def rank_record(gid, index):
            candidate=by_id[gid]
            original=candidate.get('fused_score')
            record={'group_id':gid,'rank':index+1,
                    'query_view_rank_positions':{name:view[gid]
                        for name,view in zip(view_names,views) if gid in view}}
            if gid in window_counts:record['window_count']=window_counts[gid]
            if (not isinstance(original,bool) and isinstance(original,(int,float))
                    and math.isfinite(original) and -1/(RRF_K+1)<=original<0):
                record['original_rrf_score']=original
            from .evidence_group_selection import ORIGINAL_GEOMETRY_POLICY
            for field in ('rank_positions','original_geometry_rank_positions'):
                positions=candidate.get(field)
                if (not isinstance(positions,dict) or (field=='original_geometry_rank_positions'
                        and candidate.get('original_geometry_policy')!=ORIGINAL_GEOMETRY_POLICY)):
                    continue
                record[field]={key:rank for key,rank in positions.items()
                    if key in ('canonical','sentence')
                    and (rank is None or (type(rank) is int and 1<=rank<=20000))}
            if 'original_geometry_rank_positions' in record:
                record['original_geometry_policy']=ORIGINAL_GEOMETRY_POLICY
            return record
        diagnostic=dict(successful,used=True,query_view_count=len(views),
                        pool_count=len(common),pool_selection='shared_query_candidate_pool',
                        pool_branch_counts={key:count for key,count in common_branch_counts.items()
                                            if not key.startswith('_')},
                        pool_reserved_window_count=common_branch_counts.get('_reserved_windows',0),
                        pool_reserved_scope_window_count=common_branch_counts.get('_reserved_scope_windows',0),
                        pool_geometry_duplicate_count=common_branch_counts.get('_geometry_duplicates',0),
                        pool_version_count=len({item.get('version_id') for item in candidates}),
                        candidate_rejections=rejection_counts,admission_filtered_groups=admission_counts,
                        scored_count=len(ordered),
                        scored_pair_count=sum(meta.get('scored_count',0) for meta in results if meta.get('used')),
                        query_candidate_pool_count=len(common),
                        query_candidate_pool_branch_counts=common_branch_counts,
                        ranked_groups=[rank_record(gid,index) for index,gid in enumerate(ordered)],
                        reason='rerank_partial' if failures or partials else None,
                        query_view_policy=QUERY_VIEW_POLICY,
                        script_alignment_used=aligned,query_lane_candidate_counts=lane_counts,
                        query_view_failures=failures,query_view_partials=partials,
                        source_coverage=source_coverage)
        return ordered,diagnostic
    ranking, diagnostic = rank_views(query, groups, translated_query)
    seeds, segment_diagnostics, missing_core_count = [], [], 0
    # 单一请求保留原 canonical 首位的完整窗口作为预算偏好，而非让
    # 可选模型相关性或邻句预算挤掉原首位证据。只能取已实际评分的合法
    # 完整窗口；它不能降低原facet/机构/子问必要覆盖。多问句仍由各自
    # 必要组规划，避免额外原问挤占联合预算。
    preferred=[]
    preference_origin='original_canonical_first'
    if not segment_structures and diagnostic.get('used'):
        by_id={item['group_id']:item for item in groups}
        seed=next((gid for gid in ranking
                   if by_id[gid].get('kind')=='canonical_sentence_window'
                   and by_id[gid].get('canonical_anchor_rank')==1
                   and by_id[gid].get('complete') and not by_id[gid].get('rejection')),None)
        if seed is not None:preferred.append(seed)
    form_meta={'used':False,'reason':'answer_form_disabled'}
    if (os.environ.get('NAVREN_ANSWER_FORM_ENABLED','0')=='1'
            and not segment_structures and diagnostic.get('used')):
        from .answer_form_preference import preferred_answer_form
        form_preferred,form_meta=preferred_answer_form(
            query,translated_query,groups,rows,parsed_texts,
            diagnostic.get('ranked_groups',[]),deadline=deadline)
        # 新线索是可审计的软偏好替代策略，原模型/召回rank与candidate池
        # 不动。原硬规划由planner独立保留；不能把形态当硬事实覆盖。
        if form_preferred:
            preferred=form_preferred
            preference_origin='literal_answer_form'
    for segment in segment_structures:
        value, structure = segment[:2]
        translated = segment[2] if len(segment)>2 else None
        member_ids = {item['group_id'] for item in structure.get('evidence_groups', ())}
        pool = [item for item in groups if item['group_id'] in member_ids]
        ordered, meta = rank_views(value, pool, translated)
        seed = None
        if meta.get('used') and ordered:
            by_id = {item['group_id']: item for item in pool}
            seed = next((gid for gid in ordered if by_id[gid].get('kind') in ('sentence_core','context_core','canonical_sentence_window')
                         and by_id[gid].get('complete') and not by_id[gid].get('rejection')), None)
            if seed is not None and seed not in seeds:
                seeds.append(seed)
        if seed is None:
            missing_core_count += 1
        segment_diagnostics.append(meta)
    diagnostic = dict(diagnostic, segment_count=len(segment_structures),
                      segment_used_count=sum(item.get('used') is True for item in segment_diagnostics),
                      segment_failures=[item.get('reason') for item in segment_diagnostics
                                        if not item.get('used')], seed_group_count=len(seeds),
                      segment_missing_core_count=missing_core_count)
    original_preferred=preferred if preference_origin=='original_canonical_first' else []
    diagnostic['preferred_original_group_ids']=original_preferred
    diagnostic['original_canonical_preference_count']=len(original_preferred)
    diagnostic['preferred_evidence_group_ids']=preferred
    diagnostic['preference_origin']=preference_origin if preferred else None
    diagnostic['answer_form_preference']=form_meta
    if time.monotonic() >= deadline:
        diagnostic['attempted_scored_pair_count']=diagnostic.get('scored_pair_count',0)
        diagnostic.update(used=False,reason='rerank_timeout',scored_count=0,ranked_groups=[],
                          scored_pair_count=0,query_view_count=0,
                          segment_used_count=0,seed_group_count=0,
                          preferred_original_group_ids=[],original_canonical_preference_count=0,
                          preferred_evidence_group_ids=[],preference_origin=None,
                          segment_missing_core_count=len(segment_structures))
        diagnostic['answer_form_preference']=dict(form_meta,used=False,reason='rerank_timeout',
            matched_group_count=0,preferred_group_id=None,witnesses=[])
        return [],[],diagnostic
    return ranking, seeds, diagnostic
