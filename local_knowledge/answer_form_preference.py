"""本地字面问句形态与原文形态的软检索线索；不授予事实支持。

只检查本轮已成功评分的完整原句窗口，保留其全部原引用及条件所在窗口。
不新增候选、借用父分数、改原排名或抽取数值答案。问题、译文和字面词项
仅存在本地内存；诊断只含固定枚举、公共ID及原文定位。
"""
from __future__ import annotations

import math
import re
import time

POLICY = 'literal-answer-form-complete-window-soft-preference/1'
_CLAUSE = r'[^.!?;\n]{2,320}'
_ENUMERATION = re.compile(
    r'\b(?:include[sd]?|including|comprise[sd]?|comprising|'
    r'consist(?:s|ed)? of|range(?:s|d)? from|such as)\b'
    + _CLAUSE + r'\b(?:and|or|as well as|to)\b[^.!?;\n]{2,160}', re.I)
_NEGATIVE_CONDITION = re.compile(
    r'\b(?:if|when)\b[^,.!?;\n]{0,160}\b(?:not|without|no longer|fail(?:s|ed)? to)\b'
    r'[^,.!?;\n]{1,160}', re.I)
_PAIR = re.compile(r'(?<![\w.])\d+(?:\.\d+)?\s*[-–—:]\s*\d+(?:\.\d+)?'
                   r'\s+([A-Za-z][A-Za-z-]{1,35})\b')
_NEGATIVE_HAN = re.compile(r'没有|未(?!来|必)|不(?!仅|但|光|止|论|管|妨)')


def _singular(word):
    word = word.casefold()
    # 仅有限字面屈折，不补写金融词、单位或事件。
    return word[:-1] if len(word) > 3 and word.endswith('s') and not word.endswith('ss') else word


def _request_form(query, translated):
    if not isinstance(query, str) or not 0 < len(query) <= 500:
        return None, set()
    # 原问明确要求不回答计数时，不能用被否定的how many/多少片段
    # 替代真正的日期等请求。只识别字面禁令，不尝试泛化解释用户意图。
    forbidden_count = re.search(
        r'\b(?:do not|don\x27t|never|avoid)\s+(?:tell(?:ing)?|say(?:ing)?|report(?:ing)?|'
        r'giv(?:e|ing)|answer(?:ing)?|includ(?:e|ing)|mention(?:ing)?)\b'
        r'[^.!?;\n]{0,80}\bhow many\b', query, re.I)
    forbidden_han_count = re.search(
        r'(?:不要|别|无需|不必|不用|不需要|勿)[^，,。!?？；;\n]{0,12}'
        r'(?:告诉|说|报告|回答|列出|给出|提及)[^，,。!?？；;\n]{0,40}(?:多少|几(?:个|家|名|人|次|票|项))', query)
    if forbidden_count or forbidden_han_count:
        return None, set()
    condition = re.search(r'(?:如果|若是|假如|倘若|若)[^，,；;。!?？\n]{1,160}', query)
    options = re.search(r'(?:哪些|哪几|什么)[^，,；;。!?？\n]{0,12}(?:措施|办法|方法|工具|行动|干预)', query)
    english_condition = re.search(r'\bif\b[^,.!?;\n]{0,160}\b(?:not|without|fail(?:s|ed)? to)\b', query, re.I)
    english_options = re.search(r'\b(?:which|what)\b.{0,60}\b(?:measures|options|methods|actions|tools)\b', query, re.I)
    if ((condition and _NEGATIVE_HAN.search(condition.group()) and options)
            or (english_condition and english_options)):
        return 'negative_condition_options', set()
    # 多个明确计数槽才识别为配对数量请求。单位必须在实际已有语言视图
    # 中字面出现，不能根据国家、标题或已知用例猜测一个计数单位。
    repeated = len(re.findall(r'多少|几(?:个|家|名|人|次|票|项)', query)) >= 2
    repeated |= len(re.findall(r'\bhow many\b', query, re.I)) >= 2
    if repeated:
        units = set()
        for view in (query, translated):
            if not isinstance(view, str):
                continue
            units.update(_singular(m.group(1)) for m in re.finditer(
                r'\bhow many\s+([A-Za-z][A-Za-z-]{1,35})\b', view, re.I))
        if len(units) == 1:
            return 'paired_numeric', units
    return None, set()


def _witnesses(body, form, units):
    if form == 'paired_numeric':
        # A-B及单位只证明字面配对形态；不将A/B解释为方向、人数或合计。
        match = next((m for m in _PAIR.finditer(body) if _singular(m.group(1)) in units), None)
        return [(match.start(), match.end(), 'paired_numeric')] if match else []
    if form == 'negative_condition_options':
        for condition in _NEGATIVE_CONDITION.finditer(body):
            for listing in _ENUMERATION.finditer(body):
                # 只保留原文中先条件后列举的邻近见证；不跨段或倒序拼接。
                between = body[condition.end():listing.start()]
                if (0 <= listing.start() - condition.end() <= 640
                        and '\n' not in between and len(re.findall(r'[.!?]', between)) <= 2):
                    return [(condition.start(), condition.end(), 'negative_condition'),
                            (listing.start(), listing.end(), 'explicit_enumeration')]
    return []


def preferred_answer_form(query, translated, groups, rows, parsed_texts, rank_records, *, deadline):
    """最多一个软偏好；规划器仍须保护原硬覆盖，整窗付原CID成本。

    形态相容不证明同一主体、事件、日期、方向、条件或措施。此处不写
    graph assertion、semantic support或答案值。模型名次仅作候选次序。
    """
    diagnostic = {'policy': POLICY, 'authority': 'retrieval_hint_only', 'used': False,
                  'form': None, 'matched_group_count': 0, 'preferred_group_id': None,
                  'witnesses': [], 'reason': 'request_form_unknown'}
    try:
        valid_deadline = (type(deadline) in (int, float) and math.isfinite(deadline))
    except (OverflowError, ValueError):
        valid_deadline = False
    if (not valid_deadline or not isinstance(rank_records, (list, tuple))
            or len(rank_records) > 192 or not isinstance(groups, (list, tuple))
            or not isinstance(rows, (list, tuple)) or not isinstance(parsed_texts, dict)):
        diagnostic['reason'] = 'answer_form_input_invalid'
        return [], diagnostic
    if time.monotonic() >= deadline:
        diagnostic['reason'] = 'rerank_timeout'
        return [], diagnostic
    form, units = _request_form(query, translated)
    if form is None:
        return [], diagnostic
    diagnostic['form'] = form
    from .evidence_group_selection import _normal
    from .reranking import _exact_span, RerankError
    try:
        by_id = {group['group_id']: group for group in groups}
        by_cid = {row['id']: dict(row) for row in rows}
    except (KeyError, TypeError, ValueError):
        diagnostic['reason'] = 'answer_form_input_invalid'
        return [], diagnostic
    if len(by_id) != len(groups) or len(by_cid) != len(rows):
        diagnostic['reason'] = 'answer_form_input_invalid'
        return [], diagnostic
    checked, matches = {}, []
    # 调用方只传本轮成功评分记录。仍逐项复验，不能靠提供方加一个shape
    # 字段或伪造窗口说明取得偏好。无record的core不会借窗口获得资格。
    for record in rank_records[:192]:
        if time.monotonic() >= deadline:
            diagnostic.update(reason='rerank_timeout', matched_group_count=0)
            return [], diagnostic
        if not isinstance(record, dict):
            continue
        gid = record.get('group_id')
        if not isinstance(gid, str) or type(record.get('rank')) is not int or not 1 <= record['rank'] <= 192:
            continue
        group = by_id.get(gid)
        ranks = record.get('query_view_rank_positions', {})
        if (not group or group.get('kind') != 'canonical_sentence_window'
                or not group.get('complete') or group.get('rejection')
                or not isinstance(ranks, dict) or not ranks
                or any(name not in ('original', 'translated') or type(rank) is not int
                       or not 1 <= rank <= 192 for name, rank in ranks.items())):
            continue
        try:
            normal = _normal(group, set(by_cid), by_cid)
            if normal['rejection']:
                continue
            _exact_span(normal, by_cid, parsed_texts, checked)
            body = parsed_texts[normal['version_id']]['parsed_text'][normal['start_char']:normal['end_char']]
            witnesses = _witnesses(body, form, units)
        except (RerankError, KeyError, TypeError, ValueError):
            continue
        if witnesses:
            matches.append((min(ranks.values()), record['rank'], gid,
                            [{'shape': shape, 'start_char': normal['start_char'] + start,
                              'end_char': normal['start_char'] + end}
                             for start, end, shape in witnesses]))
    if time.monotonic() >= deadline:
        diagnostic.update(reason='rerank_timeout', matched_group_count=0)
        return [], diagnostic
    diagnostic['matched_group_count'] = len(matches)
    if not matches:
        diagnostic['reason'] = 'scored_complete_window_form_missing'
        return [], diagnostic
    _, _, gid, witnesses = min(matches, key=lambda item: item[:3])
    diagnostic.update(used=True, preferred_group_id=gid, witnesses=witnesses, reason=None)
    if time.monotonic() >= deadline:
        diagnostic.update(used=False, reason='rerank_timeout', matched_group_count=0,
                          preferred_group_id=None, witnesses=[])
        return [], diagnostic
    return [gid], diagnostic
