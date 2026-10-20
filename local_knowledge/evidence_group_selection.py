"""有界公共证据组规划；不接收问题或原文，不读写数据库。

句子的全部原引文是原子核心，邻句仅为显式可选补充。先规划 facet/公共
锚点覆盖，再按统一的组级 rank 加入完整组；失败候选不是已接受证据的缺口。
调用方负责同一发布、时点、原件、身份、许可及图路径的 eligible 准入。
"""
from __future__ import annotations

import hashlib
import json
import math
from numbers import Real
import re


POLICY = 'minimal-evidence-group-selection/1'
PREFERENCE_POLICY = 'canonical-sentence-window-hard-plan-preserving-preference/2'
ORIGINAL_GEOMETRY_POLICY = 'canonical-sentence-exact-geometry-original-rrf/1'
MAX_CANDIDATES = 20000
MAX_TRACE = 128
MAX_BEAM = 128
MAX_REQUIREMENT_ALTERNATIVES = 32
MAX_OPTIONAL_CITATIONS = 2
MAX_CONTEXT_CHARS = 1600
MAX_CANONICAL_WINDOW_CHARS = 6400
_AUTO = re.compile(r'chars:(\d+)-(\d+)\Z')
_PUBLIC_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_FACETS = frozenset(('fees','liquidity','risk','strategy','eligibility','sale_status',
                     'returns','general','terms'))
_KINDS = frozenset(('sentence_core','canonical_singleton','required_context','context_supplement','context_core',
                    'canonical_sentence_window'))
_REJECTIONS = frozenset(('not_eligible','core_completion_limit','mixed_boundary','invalid_span'))


class SelectionError(ValueError):
    code = 'evidence_selection_invalid'


def _packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _id(kind, ids):
    return 'eg-' + hashlib.sha256(_packed([POLICY, kind, ids]).encode()).hexdigest()


def _annotation(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise SelectionError() from None
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise SelectionError()
    return tuple(sorted(set(value)))


def _boundary(row):
    locator = row.get('paragraph')
    auto = _AUTO.fullmatch(locator or '')
    if auto and tuple(map(int, auto.groups())) == (row.get('start_char'), row.get('end_char')):
        locator = None
    return (row.get('version_id'), row.get('product_id'), row.get('share_class_id'),
            row.get('channel_id'), row.get('page'), row.get('data_on'), locator,
            _annotation(row.get('facets', [])), _annotation(row.get('conditions', [])),
            row.get('parser_id'), row.get('shape'), row.get('source_id'))


def _rows(values):
    result = {}
    for row in values:
        try:
            value = dict(row)
            cid = value['id']
        except (TypeError, ValueError, KeyError):
            raise SelectionError() from None
        if (not isinstance(cid, str) or not _PUBLIC_ID.fullmatch(cid)
                or cid in result or len(result) >= 5000):
            raise SelectionError()
        result[cid] = value
    return result


def _facets(values):
    if (not isinstance(values, (list, tuple, set, frozenset)) or len(values) > len(_FACETS)
            or any(not isinstance(v, str) or v not in _FACETS for v in values)):
        raise SelectionError()
    return set(values)


def _ids(value):
    if (not isinstance(value, (list, tuple)) or not value or len(value) > 5000
            or any(not isinstance(cid, str) or not _PUBLIC_ID.fullmatch(cid) for cid in value)
            or len(set(value)) != len(value)):
        raise SelectionError()
    return tuple(value)


def _normal(group, eligible_ids, rows):
    if not isinstance(group, dict):
        raise SelectionError()
    citations = _ids(group.get('citation_ids'))
    score = group.get('score', 0.0)
    gid = group.get('group_id', group.get('parent_id')) or _id(group.get('kind', 'core'), citations)
    if (not isinstance(gid, str) or not _PUBLIC_ID.fullmatch(gid) or isinstance(score, bool)
            or not isinstance(score, Real) or not math.isfinite(score)):
        raise SelectionError()
    result = dict(group)
    result.update(group_id=gid, citation_ids=citations, score=float(score),
                  kind=group.get('kind', 'sentence_core'), core=group.get('core', True),
                  complete=group.get('complete', True), rejection=None)
    prior_rejection = group.get('rejection')
    if prior_rejection is not None and (not isinstance(prior_rejection,str)
                                       or prior_rejection not in _REJECTIONS):
        raise SelectionError()
    if type(result['complete']) is not bool or type(result['core']) is not bool:
        raise SelectionError()
    if (not isinstance(result['kind'], str) or result['kind'] not in _KINDS
            or result['core'] != (result['kind'] != 'context_supplement')):
        raise SelectionError()
    for key in ('start_char','end_char'):
        value = result.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise SelectionError()
    start, end = result.get('start_char'), result.get('end_char')
    if (start is None) != (end is None) or (start is not None and start >= end):
        raise SelectionError()
    anchor = result.get('canonical_anchor_id')
    if result['kind'] == 'canonical_sentence_window' and (
            not isinstance(anchor,str) or not _PUBLIC_ID.fullmatch(anchor) or anchor not in citations):
        raise SelectionError()
    for key in ('canonical_anchor_rank','source_scope_rank'):
        rank=result.get(key)
        if rank is not None and (type(rank) is not int or not 1<=rank<=MAX_CANDIDATES):
            raise SelectionError()
    if (not isinstance(result.get('context_options', ()), (list, tuple))
            or len(result.get('context_options', ())) > 3):
        raise SelectionError()
    from .reranking import RECALL_BRANCH_NAMES
    recall=result.get('recall_rank_positions',{})
    if (not isinstance(recall,dict) or any(key not in RECALL_BRANCH_NAMES
            or type(rank) is not int or not 1<=rank<=MAX_CANDIDATES for key,rank in recall.items())):
        raise SelectionError()
    if not set(citations) <= eligible_ids:
        result['rejection'] = 'not_eligible'
    elif not result['complete']:
        result['rejection'] = 'core_completion_limit'
    elif (result['kind'] == 'context_core' and
          (start is None or end-start > MAX_CONTEXT_CHARS or len(citations) > 8)):
        result['rejection'] = 'core_completion_limit'
    elif (result['kind'] == 'canonical_sentence_window' and
          (start is None or end-start > MAX_CANONICAL_WINDOW_CHARS or len(citations) > 8)):
        result['rejection'] = 'core_completion_limit'
    elif rows:
        if (any(cid not in rows for cid in citations)
                or len({_boundary(rows[cid]) for cid in citations}) != 1):
            result['rejection'] = 'mixed_boundary'
        elif (result.get('version_id') is not None
                and result['version_id'] != rows[citations[0]].get('version_id')):
            result['rejection'] = 'mixed_boundary'
        elif start is not None:
            # 同版本当前全集中的所有重叠 canonical 引文必须保留。仅校验
            # 列出的成员会让漏映射组在重排失败后的原序 fallback 中被接受。
            version = rows[citations[0]].get('version_id')
            version_rows = {cid: row for cid, row in rows.items()
                            if row.get('version_id') == version}
            if any(type(row.get('start_char')) is not int
                   or type(row.get('end_char')) is not int
                   or not 0 <= row['start_char'] < row['end_char']
                   for row in version_rows.values()):
                result['rejection'] = 'invalid_span'
            else:
                overlap = {cid for cid, row in version_rows.items()
                           if row['start_char'] < end and row['end_char'] > start}
                cursor = start
                for row in sorted((rows[cid] for cid in citations),
                                  key=lambda row: (row['start_char'], row['end_char'])):
                    if row['start_char'] > cursor:
                        break
                    cursor = max(cursor, row['end_char'])
                if overlap != set(citations) or cursor < end:
                    result['rejection'] = 'invalid_span'
    # 既有固定拒绝不能因再归一化而消失；拒绝码也不能携带任意原文。
    if prior_rejection == 'invalid_span':
        result['rejection'] = 'invalid_span'
    elif result['rejection'] is None:
        result['rejection'] = prior_rejection
    return result


def _original_geometry(group, rows):
    """仅由完整原件映射证明的等价核心可共享原分支票。

    两种现有 producer 的 boundary 表示不同，分别从同一原件元数据重算
    校验后使用统一身份。未知表示保持原票，不猜测或删除候选。
    调用者已用 _normal 校验同版、完整重叠 map、连续覆盖与准入。
    """
    if (group['rejection'] or not group['core'] or not group['complete']
            or not rows or group.get('start_char') is None
            or not isinstance(group.get('version_id'), str) or not group['version_id']
            or not isinstance(group.get('boundary_key'), str)):
        return None
    members = [rows[cid] for cid in group['citation_ids']]
    required = ('version_id', 'start_char', 'end_char', 'paragraph', 'product_id',
                'share_class_id', 'channel_id', 'page', 'data_on', 'facets',
                'conditions', 'parser_id', 'shape', 'source_id')
    if (any(any(key not in row for key in required) for row in members)
            or any(not isinstance(row[key], str) or not row[key]
                   for row in members for key in ('paragraph', 'parser_id', 'shape', 'source_id'))):
        return None
    anchor = members[0]
    boundary = _boundary(anchor)
    if group['version_id'] != anchor['version_id']:
        return None
    # retrieval_units JSON 与 canonical window hash 都是严格原件边界的
    # 派生表示；不让未知 raw key 自称等价，也不把格式差异当成源边界差异。
    unit_boundary = _packed(dict(zip(
        ('version_id', 'product_id', 'share_class_id', 'channel_id', 'page',
         'data_on', 'explicit_paragraph', 'facets', 'conditions', 'parser_id',
         'shape', 'source_id'), boundary)))
    window_boundary = 'cw-boundary-' + hashlib.sha256(_packed(boundary).encode()).hexdigest()
    if group['boundary_key'] not in (unit_boundary, window_boundary):
        return None
    return (group['version_id'], boundary, group['start_char'], group['end_char'],
            tuple(sorted(group['citation_ids'])))


def fuse_group_candidates(matches, evidence_groups, *, eligible_ids,
                          canonical_rows=(), context_groups=()):
    """统一组级 RRF；同 CID 的多个句组作为独立替代项保留。

    原 matches 的顺序保留机构/对象优先协议。句组以所涉及原候选的最佳
    位置取得 canonical rank，并保留独立句子 rank。已有句组覆盖的 ID
    不再另造可绕过其原子核心的单引文候选。
    """
    from .vectors import RRF_K
    if (not isinstance(eligible_ids, (set, frozenset)) or len(eligible_ids) > 5000
            or any(not isinstance(cid, str) or not _PUBLIC_ID.fullmatch(cid) for cid in eligible_ids)):
        raise SelectionError()
    rows = _rows(canonical_rows)
    matched = {}
    for rank, item in enumerate(matches, 1):
        if (not isinstance(item, (tuple, list)) or len(item) != 3
                or not isinstance(item[1], str) or item[1] in matched or item[1] not in eligible_ids
                or isinstance(item[0], bool) or not isinstance(item[0], Real)
                or not math.isfinite(item[0])):
            raise SelectionError()
        matched[item[1]] = (rank, _facets(item[2]))
    groups = []
    for value in (*evidence_groups, *context_groups):
        groups.append(_normal(value, eligible_ids, rows))
        if len(groups) > MAX_CANDIDATES:
            raise SelectionError()
    covered = {cid for group in groups for cid in group['citation_ids']}
    for cid, (rank, facets) in matched.items():
        if cid not in covered:
            row = rows.get(cid, {})
            groups.append(_normal({'group_id': _id('canonical', [cid]),
                'citation_ids': [cid], 'kind': 'canonical_singleton',
                'score': 0.0, 'version_id': row.get('version_id'),
                'start_char': row.get('start_char'), 'end_char': row.get('end_char')},
                eligible_ids, rows))
    if len(groups) > MAX_CANDIDATES or len({g['group_id'] for g in groups}) != len(groups):
        raise SelectionError()
    canonical = {}; sentences = {}
    for group in groups:
        gid, citations = group['group_id'], group['citation_ids']
        positions = [matched[cid][0] for cid in citations if cid in matched]
        if group['kind'] == 'canonical_sentence_window':
            anchor = group['canonical_anchor_id']
            positions = [matched[anchor][0]] if anchor in matched else []
            group['canonical_anchor_rank'] = positions[0] if positions else None
        group['canonical_rank'] = min(positions) if positions else None
        if positions and not group['rejection']:
            canonical[gid] = -min(positions)
        if group['kind'] == 'sentence_core' and not group['rejection']:
            sentences[gid] = -group['score']
        facet_sets = [matched[cid][1] if cid in matched else
                      set(_annotation(rows[cid].get('facets', []))) if cid in rows else
                      _facets(group.get('facets', [])) for cid in citations]
        group['facets'] = sorted(set.intersection(*facet_sets)) if facet_sets else []
    sentence_order = sorted(sentences, key=lambda gid: (-sentences[gid], gid))
    sentence_positions = {}
    previous = None; position = 0
    for index, gid in enumerate(sentence_order, 1):
        if previous is None or sentences[gid] != previous:
            position = index
        sentence_positions[gid] = position
        previous = sentences[gid]
    geometry_ranks = {}; geometry_keys = {}
    for group in groups:
        group['rank_positions'] = {'canonical': group['canonical_rank'],
                                   'sentence': sentence_positions.get(group['group_id'])}
        # 不信任传入的派生字段。原 rank/anchor 保留用于原分支准入与解释。
        group.pop('original_geometry_rank_positions', None)
        group.pop('original_geometry_policy', None)
        geometry = _original_geometry(group, rows)
        geometry_keys[group['group_id']] = geometry
        if geometry is not None:
            positions = geometry_ranks.setdefault(geometry, {})
            for branch, rank in group['rank_positions'].items():
                if rank is not None:
                    positions[branch] = min(positions.get(branch, rank), rank)
    for group in groups:
        geometry = geometry_keys[group['group_id']]
        positions = group['rank_positions']
        if geometry is not None:
            positions = {branch: geometry_ranks[geometry].get(branch)
                         for branch in ('canonical', 'sentence')}
            group['original_geometry_policy'] = ORIGINAL_GEOMETRY_POLICY
            group['original_geometry_rank_positions'] = positions
        # 复制一个 canonical ID 的多个句组必须共享同一原 rank，不能再按
        # group_id 重排投票。按实际可用视图取均值，避免资料类型因多分支获
        # 固定奖励，压过原件分支真正最强的普通资料。
        # 精确等价表示每分支只一票；选窗口代表不会丢掉同一原句的强票。
        # 这不改变原 competition rank，不承诺复制对其他原 rank 无影响。
        votes = [1.0/(RRF_K+rank) for rank in positions.values()
                 if rank is not None] if not group['rejection'] else []
        group['fused_score'] = -sum(votes)/len(votes) if votes else 0.0
    return sorted(groups, key=lambda group: (group['fused_score'], group['group_id']))


def _trace(group, reason, before, after):
    citations = group['citation_ids']
    result = {'group_id': group['group_id'], 'kind': group['kind'],
            'citation_ids': list(citations[:16]), 'citation_count': len(citations),
            'citation_ids_truncated_count': max(0, len(citations)-16),
            'child_ids': [v for v in group.get('child_ids', ())
                          if isinstance(v,str) and _PUBLIC_ID.fullmatch(v)][:16],
            'parent_ids': [v for v in group.get('parent_ids', ())
                           if isinstance(v,str) and _PUBLIC_ID.fullmatch(v)][:16],
            'start_char': group.get('start_char'), 'end_char': group.get('end_char'),
            'canonical_anchor_id': group.get('canonical_anchor_id'),
            'canonical_anchor_rank': group.get('canonical_anchor_rank'),
            'source_scope_rank': group.get('source_scope_rank'),
            'rank_positions': {k:v for k,v in group.get('rank_positions', {}).items()
                               if k in ('canonical','sentence','rerank')
                               and (v is None or (type(v) is int and v > 0))},
            'reason': reason, 'budget_before': before, 'budget_after': after}
    positions = group.get('original_geometry_rank_positions')
    if (group.get('original_geometry_policy') == ORIGINAL_GEOMETRY_POLICY
            and isinstance(positions, dict)):
        result['original_geometry_policy'] = ORIGINAL_GEOMETRY_POLICY
        result['original_geometry_rank_positions'] = {
            branch: rank for branch, rank in positions.items()
            if branch in ('canonical', 'sentence')
            and (rank is None or (type(rank) is int and 1 <= rank <= MAX_CANDIDATES))}
    return result


def _beam_required_plan(groups, facets, seeds, seed_groups, limit, preferred_groups=()):
    """硬覆盖优先的有界 beam；偏好只打破同等硬覆盖的平局。

    仅返回在硬要求步骤加入的组，软偏好由调用方在可选补充前独立接受。
    未传偏好时保留原覆盖、成本与剪枝排序。
    """
    hard_needs = [('facet', facet) for facet in facets] + [('seed', cid) for cid in seeds] + [('group',gid) for gid in seed_groups]
    soft_needs = [('preference', gid) for gid in preferred_groups]
    needs = hard_needs + soft_needs
    hard_tokens, soft_tokens = set(hard_needs), set(soft_needs)
    tokens = hard_tokens | soft_tokens
    coverage = {group['group_id']: ({('facet', f) for f in group['facets']} |
                {('seed', cid) for cid in group['citation_ids']} |
                {('group',group['group_id']), ('preference',group['group_id'])}) & tokens for group in groups}
    alternatives = {need: [i for i, group in enumerate(groups)
                           if need in coverage[group['group_id']]][:MAX_REQUIREMENT_ALTERNATIVES]
                    for need in needs}
    # (citation union, all chosen indices, coverage, rank cost, hard-step indices)
    states = [(frozenset(), (), frozenset(), 0, ())]

    def order(state):
        if soft_tokens:
            return (-len(state[2] & hard_tokens), -len(state[2] & soft_tokens),
                    state[3], len(state[0]), state[1])
        return (-len(state[2]), state[3], len(state[0]), state[1])

    pruned = False
    for need in needs:
        expanded = []
        for ids, chosen, covered, cost, hard_chosen in states:
            if need in covered:
                expanded.append((ids, chosen, covered, cost, hard_chosen)); continue
            made = False
            for index in alternatives[need]:
                group = groups[index]
                merged = ids | frozenset(group['citation_ids'])
                if len(merged) <= limit:
                    expanded.append((merged, chosen + (index,),
                        covered | frozenset(coverage[group['group_id']]), cost + index + 1,
                        hard_chosen + (index,) if need in hard_tokens else hard_chosen))
                    made = True
            if not made:
                expanded.append((ids, chosen, covered, cost, hard_chosen))
        best = {}
        for state in expanded:
            key = (state[0], state[2])
            if key not in best or (state[3], state[1]) < (best[key][3], best[key][1]):
                best[key] = state
        ordered = sorted(best.values(), key=order)
        pruned |= len(ordered) > MAX_BEAM
        states = ordered[:MAX_BEAM]
    best = min(states, key=order)
    return list(dict.fromkeys(best[4])), pruned


def _required_plan(groups, facets, seeds, seed_groups, limit, preferred_groups=()):
    """原硬规划独立保留；软偏好不能利用剪枝丢掉既有硬覆盖。

    两条 beam 都保留原固定上限。偏好方案必须包含原方案实际覆盖的每个
    硬 token，而非仅比较数量；不满足则回到完全不受 soft 影响的原方案。
    这是相对原有有界规划的非回退保证，不承诺全局最优覆盖。
    """
    baseline, baseline_pruned = _beam_required_plan(groups, facets, seeds, seed_groups, limit)
    if not preferred_groups:
        return baseline, baseline_pruned, False
    candidate, candidate_pruned = _beam_required_plan(
        groups, facets, seeds, seed_groups, limit, preferred_groups)
    hard_tokens = ({('facet', facet) for facet in facets} |
                   {('seed', cid) for cid in seeds} |
                   {('group', gid) for gid in seed_groups})

    def covered(indices):
        return set().union(*[({('facet', facet) for facet in groups[index]['facets']} |
                              {('seed', cid) for cid in groups[index]['citation_ids']} |
                              {('group', groups[index]['group_id'])}) & hard_tokens
                             for index in indices]) if indices else set()

    fallback = not covered(baseline) <= covered(candidate)
    return (baseline if fallback else candidate), baseline_pruned or candidate_pruned, fallback


def plan_evidence_groups(matches, evidence_groups, *, eligible_ids, limit,
                         facets=(), seed_ids=(), canonical_rows=(), context_groups=(),
                         max_trace=64, ranking_ids=(), seed_group_ids=(),
                         preferred_group_ids=()):
    """返回完整核心与可选完整补充；从不先插入 facet 单引文种子。

    trace 仅公共 ID、位置、分支 rank、预算及固定理由。未接受候选与已接受
    核心的完整性分别统计；共享一个 ID 不代表某候选组已被接受。
    """
    if (type(limit) is not int or not 1 <= limit <= 8 or type(max_trace) is not int
            or not 0 <= max_trace <= MAX_TRACE
            or not isinstance(facets, (list, tuple, set, frozenset))
            or not isinstance(seed_ids, (list, tuple))
            or not isinstance(seed_group_ids,(list,tuple)) or len(seed_group_ids)>3
            or any(not isinstance(gid,str) or not _PUBLIC_ID.fullmatch(gid) for gid in seed_group_ids)
            or len(set(seed_group_ids))!=len(seed_group_ids)
            or not isinstance(preferred_group_ids,(list,tuple)) or len(preferred_group_ids)>1
            or any(not isinstance(gid,str) or not _PUBLIC_ID.fullmatch(gid) for gid in preferred_group_ids)
            or len(set(preferred_group_ids))!=len(preferred_group_ids)
            or not isinstance(ranking_ids,(list,tuple)) or len(ranking_ids)>192
            or any(not isinstance(gid,str) or not _PUBLIC_ID.fullmatch(gid) for gid in ranking_ids)
            or len(set(ranking_ids))!=len(ranking_ids)
            or not isinstance(eligible_ids, (set, frozenset))
            or len(facets) > 8 or len(seed_ids) > 8
            or any(not isinstance(value, str) or value not in _FACETS for value in facets)
            or any(not isinstance(value, str) for value in seed_ids)
            or not set(seed_ids) <= eligible_ids):
        raise SelectionError()
    facets = tuple(sorted(facets)) if isinstance(facets, (set, frozenset)) else tuple(dict.fromkeys(facets))
    seeds = tuple(dict.fromkeys(seed_ids))
    canonical_rows = tuple(_rows(canonical_rows).values())
    ranked = fuse_group_candidates(matches, evidence_groups, eligible_ids=eligible_ids,
                                  canonical_rows=canonical_rows, context_groups=context_groups)
    known={group['group_id']:group for group in ranked}
    if (not set(ranking_ids)<=set(known) or not set(seed_group_ids)<=set(known)
            or any(not known[gid]['core'] for gid in seed_group_ids)
            or not set(preferred_group_ids)<=set(known)
            or not set(preferred_group_ids)<=set(ranking_ids)
            or any(not known[gid]['core'] or not known[gid]['complete']
                   or known[gid]['kind'] != 'canonical_sentence_window'
                   or known[gid]['rejection'] is not None for gid in preferred_group_ids)):
        raise SelectionError()
    if ranking_ids:
        positions={gid:index+1 for index,gid in enumerate(ranking_ids)}
        for group in ranked:
            if group['group_id'] in positions:group['rank_positions']['rerank']=positions[group['group_id']]
        original={group['group_id']:index for index,group in enumerate(ranked)}
        ranked.sort(key=lambda group:(group['group_id'] not in positions,
            positions.get(group['group_id'],original[group['group_id']])))
    valid = [group for group in ranked if not group['rejection']]
    invalid = [group for group in ranked if group['rejection']]
    required, pruned, preference_fallback = _required_plan(valid, facets, seeds,
        tuple(dict.fromkeys(seed_group_ids)), limit, preferred_group_ids)
    result = []; selected = set(); accepted = {}; rejected = {}
    accepted_trace = []; rejected_trace = []; optional_added = 0; trace_count = 0
    identities = {g['group_id']: (g['kind'], g['citation_ids'], g.get('version_id'),
                                 g.get('start_char'), g.get('end_char')) for g in ranked}

    def identify(group):
        identity = (group['kind'], group['citation_ids'], group.get('version_id'),
                    group.get('start_char'), group.get('end_char'))
        previous = identities.setdefault(group['group_id'], identity)
        if previous != identity:
            raise SelectionError()

    def reject(group, why):
        if group['group_id'] not in accepted:
            rejected[group['group_id']] = (group, why, limit-len(selected))

    def accept(group, why, budget_reason='budget_exceeded'):
        nonlocal trace_count
        gid = group['group_id']
        if gid in accepted:
            return True
        before = limit-len(selected)
        missing = [cid for cid in group['citation_ids'] if cid not in selected]
        if len(missing) > before:
            reject(group, budget_reason)
            return False
        result.extend(missing); selected.update(missing)
        accepted[gid] = group
        rejected.pop(gid, None)
        trace_count += 1
        if len(accepted_trace) < max_trace:
            accepted_trace.append(_trace(group, why, before, limit-len(selected)))
        return True

    # 先原子规划全部 facet/机构要求，补充不能挤掉这些已规划核心。
    for index in required:
        accept(valid[index], 'required_coverage')
    preferred_accepted = 0
    for gid in preferred_group_ids:
        if accept(known[gid], 'preferred_coverage', 'preferred_budget_exceeded'):
            preferred_accepted += 1
    rows = _rows(canonical_rows)

    def add_options(group):
        nonlocal optional_added
        for raw in group.get('context_options', ()):
            option = _normal(raw, eligible_ids, rows)
            identify(option)
            if (option.get('boundary_key') != group.get('boundary_key')
                    or option.get('version_id') != group.get('version_id')):
                option['rejection'] = 'mixed_boundary'
            if (option['kind'] != 'context_supplement' or option['core']
                    or option.get('required_group_id') != group['group_id']
                    or not set(group['citation_ids']) <= set(option['citation_ids'])
                    or option.get('start_char') is None or group.get('start_char') is None
                    or not option['start_char'] <= group['start_char'] < group['end_char'] <= option['end_char']
                    or option['end_char']-option['start_char'] > MAX_CONTEXT_CHARS):
                option['rejection'] = 'invalid_context'
            missing = set(option['citation_ids'])-selected
            if option['rejection']:
                reject(option, option['rejection'])
            elif optional_added + len(missing) > MAX_OPTIONAL_CITATIONS:
                reject(option, 'context_budget_exceeded')
            elif accept(option, 'optional_context'):
                optional_added += len(missing)

    for group in list(accepted.values()):
        add_options(group)
    for group in valid:
        if group['group_id'] in preferred_group_ids:
            continue
        if accept(group, 'ranked_core'):
            add_options(group)
    for group in invalid:
        reject(group, group['rejection'])
    for group, why, before in rejected.values():
        trace_count += 1
        if len(accepted_trace)+len(rejected_trace) < max_trace:
            rejected_trace.append(_trace(group, why, before, before))
    covered_facets = {facet for group in accepted.values() for facet in group.get('facets', [])}
    incomplete = sum(not set(group['citation_ids']) <= selected for group in accepted.values())
    partial_unaccepted = sum(bool(set(group['citation_ids']) & selected)
                             and not set(group['citation_ids']) <= selected
                             for group, _, _ in rejected.values())
    accepted_display = accepted_trace[:max_trace]
    rejected_display = rejected_trace[:max(0, max_trace-len(accepted_display))]
    return result, {'policy': POLICY, 'preference_policy': PREFERENCE_POLICY,
        'original_geometry_policy': ORIGINAL_GEOMETRY_POLICY,
        'preference_plan_fallback': preference_fallback,
        'preferred_group_attempt_count': len(preferred_group_ids),
        'preferred_group_accepted_count': preferred_accepted,
        'preferred_group_rejected_count': len(preferred_group_ids)-preferred_accepted,
        'candidate_count': len(ranked),
        'accepted_group_count': len(accepted), 'incomplete_accepted_groups': incomplete,
        'unaccepted_partial_groups': partial_unaccepted,
        'budget_rejected_groups': sum(why in ('budget_exceeded','context_budget_exceeded',
                                            'preferred_budget_exceeded')
                                     for _, why, _ in rejected.values()),
        'ineligible_rejected_groups': sum(why == 'not_eligible' for _, why, _ in rejected.values()),
        'invalid_boundary_groups': sum(why == 'mixed_boundary' for _, why, _ in rejected.values()),
        'core_completion_rejected_groups': sum(why == 'core_completion_limit' for _, why, _ in rejected.values()),
        'invalid_context_groups': sum(why == 'invalid_context' for _, why, _ in rejected.values()),
        'invalid_span_groups': sum(why == 'invalid_span' for _, why, _ in rejected.values()),
        'uncovered_facets': [f for f in facets if f not in covered_facets],
        'uncovered_seed_ids': [cid for cid in seeds if cid not in selected],
        'uncovered_seed_group_ids': [gid for gid in dict.fromkeys(seed_group_ids) if gid not in accepted],
        'accepted_groups': accepted_display, 'rejected_groups': rejected_display,
        'trace_truncated_count': max(0, trace_count-len(accepted_display)-len(rejected_display)),
        'beam_pruned': pruned, 'selected_count': len(selected),
        'optional_context_citations': optional_added, 'remaining_budget': limit-len(selected)}
