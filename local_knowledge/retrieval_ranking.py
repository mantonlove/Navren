"""Read-only ranking features from canonical public spans, never financial facts.

The caller supplies only eligible evidence. Object names identify candidates;
they do not give every field a full lexical vote. Exact field intent is limited
to the registered disclosure shape, and never establishes buying eligibility.
"""
from __future__ import annotations

import re
from .store import tokens
from .vector_documents import FIELD_LABELS, representation

RANKING_POLICY = 'public-hybrid-object-field-coverage/2'
# Small explicit vocabulary; unknown wording continues through the encoder.
FIELD_PHRASES = {
    'risk': ('风险等级', '风险级别', '风险几级', '风险档次'),
    'publish': ('发行机构', '发行公司', '发行人', '哪家公司发行', '谁发行', '保险公司是哪家'),
    'charge': ('收费标准', '费用标准', '费率', '收多少', '收费金额', '收费比例', '管理费'),
    'chargeWay': ('收费方式', '扣费方式', '扣费的做法', '怎么收费', '怎么收取', '怎么交费用'),
    'investor': ('合理投资范围', '投资者范围', '风险承受', '风险偏好', '投资者要求', '核保'),
    'customerGroupType': ('客群类别', '客户类别', '个人还是机构', '个人还是企业', '企业客户',
                          '机构客户', '公司名义', '企业能投保', '自然人'),
    'saleType': ('销售类型', '销售角色', '代销', '自营', '代理销售', '替别人销售'),
    'placement': ('招募类型', '募集性质', '公募', '私募', '公众募集', '公开募集'),
    'saleStatus': ('销售状态', '在售', '还能下单', '现在能买'),
    'type': ('产品类型', '什么类型'), 'code': ('产品代码',), 'name': ('产品名称',),
}


def _code_pattern(code):
    # Bare numeric codes can be fund IDs or money amounts; namespace is required.
    qualified = (r'(?<![A-Za-z0-9._:-])WEBANK:' + re.escape(code)
                 + r'(?![A-Za-z0-9._:-])')
    disclosed = (r'(?:渠道披露代码|微众披露代码|微众产品代码|渠道代码)\s*[：:]?\s*'
                 + re.escape(code) + r'(?![A-Za-z0-9._:-])')
    return qualified + '|' + disclosed


def _contains_code(query, code):
    return bool(re.search(_code_pattern(code), query, re.I))


def _objects(query, rows, request):
    """Resolve only full names / qualified channel codes, including ambiguity.

    This signal cannot admit evidence or expose a withdrawn quote. Partial names
    and implicit share mappings do not establish an identity.
    """
    structured = [r for r in rows if representation(r)['kind'] == 'webank_field']
    names = sorted({r['title'] for r in structured}, key=lambda t: (-len(t), t))
    residual = query
    named_groups = []
    for name in names:
        if name and name in residual:
            named_groups.append({r['product_id'] for r in structured if r['title'] == name})
            residual = residual.replace(name, ' ')
    coded = set()
    for row in structured:
        code = row['product_id'][7:]
        if _contains_code(residual, code):
            coded.add(row['product_id'])
            residual = re.sub(_code_pattern(code), ' ', residual, flags=re.I)
    selected = request.get('product_id')
    mentioned = set(coded)
    for group in named_groups:
        if selected in group:
            mentioned.add(selected)
        elif coded & group:
            mentioned.update(coded & group)
        elif len(group) > 1:
            return set(), residual, 'identity_ambiguous'
        else:
            mentioned.update(group)
    if selected and mentioned - {selected}:
        return set(), residual, 'identity_conflict'
    if selected and any(r['product_id'] == selected for r in structured):
        mentioned.add(selected)
    return mentioned, residual, None


def identity_issue(query, rows, request):
    return _objects(query, rows, request)[2]


def structural_intents(query, rows, request):
    """Return intent and preferred IDs only for explicitly identified objects.

    No identity inference from substring fund codes/short names. A full public
    title or an exact channel disclosure code is required unless product_id is
    already verified by the caller. Multiple named objects retain their union.
    """
    structured = {r['id']: (r, representation(r)) for r in rows}
    structured = {cid: (r, v) for cid, (r, v) in structured.items() if v['kind'] == 'webank_field'}
    objects, residual, issue = _objects(query, rows, request)
    if issue:
        objects = set()
    fields = []
    if objects:
        folded = residual.casefold()
        fields = [field for field, phrases in FIELD_PHRASES.items() if any(p in folded for p in phrases)]
        from .field_semantics import query_semantics
        fields=list(dict.fromkeys(fields+query_semantics(residual)['fields']))
        if ('publish' not in fields and re.search(r'(?:公司|机构|主体).{0,8}(?:负责|登记)|(?:负责|登记).{0,8}(?:公司|机构|主体)',folded)):
            fields.append('publish')
        # Interrogative subject plus an issuance predicate, not a product/code
        # exception. Sales-channel role questions do not imply an issuer.
        if ('publish' not in fields and re.search(r'(?:谁|哪家|什么公司).{0,6}(?:发行|发售|发起|推出|发的)',folded)):
            fields.append('publish')
        # Unqualified fee wording is ambiguous: preserve both disclosure columns.
        if not {'charge', 'chargeWay'} & set(fields) and any(p in folded for p in ('费用', '收费', '扣费', '保费')):
            fields.extend(('charge', 'chargeWay'))
    preferred = {cid for cid, (row, v) in structured.items()
                 if row['product_id'] in objects and v['field'] in fields}
    return {'fields': fields, 'preferred': preferred, 'objects': objects, 'residual': residual}


def lexical_weights(query, rows, intent):
    """Weight an OR lexical vote by actual query-token coverage, not title rank.

    Cross-language spans naturally have little lexical overlap and remain
    eligible through vectors. This is only a ranking feature, not a support gate.
    """
    result = {}
    for row in rows:
        value = representation(row)
        structured = value['kind'] == 'webank_field' and row['product_id'] in intent['objects']
        search_query = intent['residual'] if structured else query
        terms = set(tokens(search_query))
        content = (FIELD_LABELS[value['field']] + '\n' + row['excerpt']) if structured else row['title'] + '\n' + row['excerpt']
        overlap = terms & set(tokens(content))
        result[row['id']] = len(overlap) / len(terms) if terms else 0.0
        if row['id'] in intent['preferred']:
            result[row['id']] = 1.0
    return result


def adjacent_context(query, ranked_ids, rows):
    """Bounded contiguous context for enumerations, only from eligible originals.

    A list can cross a chunk boundary. Retain its best seed and at most two
    adjacent spans in that same immutable version; do not follow other versions,
    products, shares, documents or sources. This is recall, never claim support.
    """
    if (len(ranked_ids)<2 or re.search(r'比较|对比|相比|区别|各自|分别|不同|compare|versus',query,re.I)
            or not re.search(r'哪些|哪几个|几个方面|三个方面|列出|which areas|list the', query, re.I)):
        return []
    by_id = {row['id']: row for row in rows}
    ranked_ids = [cid for cid in ranked_ids if cid in by_id]
    if len(ranked_ids)<2:
        return []
    named_titles = {row['title'] for row in rows
                    if representation(row)['kind'] == 'market_full_text' and row['title'] in query}
    if len(named_titles)>1:
        return []
    seed = by_id[ranked_ids[0]]
    if (representation(seed)['kind'] != 'market_full_text'
            or by_id[ranked_ids[1]]['version_id'] != seed['version_id']):
        return []
    group = sorted((row for row in rows if row['version_id'] == seed['version_id']
                    and row['product_id'] == seed['product_id'] and row['share_class_id'] == seed['share_class_id']
                    and row['channel_id'] == seed['channel_id']
                    and representation(row)['kind'] == 'market_full_text'), key=lambda row: row['start_char'])
    index = next(i for i, row in enumerate(group) if row['id'] == seed['id'])
    result = [seed['id']]
    previous = seed
    for row in group[index+1:index+3]:
        if row['start_char'] != previous['end_char']:
            break
        result.append(row['id']); previous = row
    return result if len(result) > 1 else []
