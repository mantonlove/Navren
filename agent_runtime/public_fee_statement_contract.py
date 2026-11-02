"""Finite public-fee statement validation; no model, rewrite, or monetary fact.

Only the explicitly supported table grammars yield canonical statements. This
is intentionally not a general Chinese entailment checker. All witness offsets
are Unicode codepoints in the NFKC delivered excerpt, never PDF/raw offsets.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
import re
import unicodedata


class FeeStatementContractError(ValueError):
    pass


def _fail():
    raise FeeStatementContractError('PUBLIC_FEE_STATEMENT_UNSUPPORTED')


def _nfkc(text):
    if type(text) is not str:
        _fail()
    return unicodedata.normalize('NFKC', text)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def canonical_source_rows(rows, bindings=()):
    """One deterministic representative among actually supplied/cited copies.

    A pre-existing time role keeps its explicitly bound evidence ID. Fee-only
    copies use the smallest supplied evidence ID; unreferenced copies must not
    be supplied by the caller. Conflicting copies never become duplicates.
    """
    groups = {}
    for row in rows:
        if type(row) is not dict or set(row) != {'evidence_id', 'hit', 'as_of'}:
            _fail()
        hit = row['hit']; key = (hit['citation_id'], row['as_of'])
        old = groups.setdefault(key, [])
        if old and old[0]['hit'] != hit:
            _fail()
        old.append(row)
    result = []
    for (citation, day), copies in groups.items():
        preferred = {b[role + '_evidence_id'] for b in bindings for role in ('limit', 'definition')
            if b.get(role + '_citation_id') == citation}
        if len(preferred) > 1:
            _fail()
        eid = next(iter(preferred)) if preferred else min(r['evidence_id'] for r in copies)
        selected = [r for r in copies if r['evidence_id'] == eid]
        if not selected:
            _fail()
        result.append(deepcopy(selected[0]))
    return sorted(result, key=lambda r: ('fees' not in r['hit'].get('facets', []),
        r['hit']['citation_id'], r['as_of'], r['evidence_id']))


_IDENTITY = ('product_id', 'share_class_id', 'channel_id', 'release_id')
_FEES = ('管理费', '托管费', '销售服务费')
_FOLLOWUP = '需要核对本轮原文的其他条款吗？'
_UNKNOWN = '本轮所引原文未核实其他费用关系，暂不作额外结论。'
_LABEL = r'[\u4e00-\u9fff]{2,80}[A-Z]'
_NUMBER = r'[0-9]+(?:\.[0-9]+)?'
_OPERATING = re.compile(
    r'\A以下费用将从基金资产中扣除:\n费用类别 收费方式/年费率或金额 收取方\n'
    r'管理费 (?P<management>' + _NUMBER + r')% 基金管理人(?:和销售机构)?\n'
    r'托管费 (?P<custody>' + _NUMBER + r')% 基金托管人\n'
    r'(?P<label_before>[\u4e00-\u9fff]{1,80})\n'
    r'销售服务费 (?P<service>' + _NUMBER + r')% 销售机构\n'
    r'(?P<label_after>[\u4e00-\u9fff]{0,80}[A-Z])\n?\Z')
_REDEMPTION = re.compile(
    r'\A(?P<label>' + _LABEL + r')\n份额\(S\)或金额\(M\)\n'
    r'费用类型 收费方式/费率\n/持有期限\(N\)\n(?P<rows>[^\r]+)\Z')
_TIER = re.compile(r'(?:赎回费 )?((?:N[<≤>≥][0-9]+日)|(?:[0-9]+日[<≤]N[<≤][0-9]+日)) (' + _NUMBER + r')%')


def _interval(condition):
    single = re.fullmatch(r'N([<≤>≥])([0-9]+)日', condition)
    if single:
        op, number = single[1], int(single[2])
        interval = ((None, False, number, op == '≤') if op in ('<', '≤')
            else (number, op == '≥', None, False))
    else:
        match = re.fullmatch(r'([0-9]+)日([<≤])N([<≤])([0-9]+)日', condition)
        if not match:
            return None
        interval = int(match[1]), match[2] == '≤', int(match[4]), match[3] == '≤'
    lo, lc, hi, hc = interval
    # This only checks literal intervals, not personal eligibility or day units.
    if hi is not None and (hi < 0 or hi == 0 and not hc):
        return None
    if lo is not None and hi is not None and (lo > hi or lo == hi and not (lc and hc)):
        return None
    return interval


def _overlap(a, b):
    for left, right in ((a, b), (b, a)):
        hi, hc, lo, lc = left[2], left[3], right[0], right[1]
        if hi is not None and lo is not None and (hi < lo or hi == lo and not (hc and lc)):
            return False
    return True


def _checked_sources(sources, provider_id):
    if (type(provider_id) is not str or not provider_id or type(sources) is not list
            or not 1 <= len(sources) <= 6):
        _fail()
    identities, versions, days, seen = set(), set(), set(), set()
    for row in sources:
        if type(row) is not dict or set(row) != {'evidence_id', 'hit', 'as_of'}:
            _fail()
        hit, day = row['hit'], row['as_of']
        if (type(hit) is not dict or type(row['evidence_id']) is not str or not row['evidence_id']
                or type(day) is not str or date.fromisoformat(day).isoformat() != day
                or 'fees' not in hit.get('facets', [])
                or hit.get('visibility') != 'public' or hit.get('outbound_allowed') is not True
                or hit.get('content_status') != 'full_text'
                or hit.get('claims_are_account_facts') is not False
                or hit.get('withdrawn', False) is not False
                or any(type(hit.get(k)) is not str or not hit[k] for k in
                    ('citation_id', 'document_id', 'document_version_id', 'product_id', 'share_class_id', 'release_id'))
                or 'channel_id' not in hit or not (hit['channel_id'] is None or type(hit['channel_id']) is str)
                or not 0 < len(_nfkc(hit.get('excerpt'))) <= 1198):
            _fail()
        identity = tuple(hit[k] for k in _IDENTITY)
        key = (hit['citation_id'], day)
        if key in seen:
            _fail()
        seen.add(key); identities.add(identity)
        versions.add((hit['document_id'], hit['document_version_id'])); days.add(day)
    if len(identities) != 1 or len(versions) != 1 or len(days) != 1:
        _fail()
    return {'provider_id': provider_id, **dict(zip(_IDENTITY, next(iter(identities)))),
        'document_id': next(iter(versions))[0], 'document_version_id': next(iter(versions))[1],
        'as_of': next(iter(days))}


def source_rows_from_evidence(evidence):
    """Schema proposal only; the terminal independently checks actual delivery."""
    if not isinstance(evidence, Mapping):
        return None
    rows, providers, contexts = {}, set(), set()
    try:
        for eid, entry in sorted(evidence.items()):
            if not isinstance(entry, Mapping):
                continue
            data = entry.get('data', {})
            hits = data.get('hits', [])
            fees = [h for h in hits if isinstance(h, Mapping) and 'fees' in h.get('facets', [])]
            if not fees:
                continue
            if (entry.get('id') != eid or entry.get('tool') != 'knowledge_search'
                    or entry.get('source') != 'public_knowledge' or not entry.get('context_hash')
                    or data.get('ready') is not True or data.get('scope') != 'public'):
                return None
            providers.add(data.get('provider_id')); contexts.add(entry['context_hash'])
            for hit in fees:
                if data.get('public_release_id') != hit.get('release_id'):
                    return None
                key = (hit.get('citation_id'), data.get('as_of'))
                row = {'evidence_id': eid, 'hit': deepcopy(dict(hit)), 'as_of': data.get('as_of')}
                if key in rows and rows[key]['hit'] != row['hit']:
                    return None
                rows.setdefault(key, row)
        if len(providers) != 1 or len(contexts) != 1:
            return None
        provider = next(iter(providers)); sources = canonical_source_rows(list(rows.values()))
        _checked_sources(sources, provider)
        return sources, provider
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def build_catalog(sources, provider_id):
    """Compile only literal fee fields and one explicitly bounded comparison."""
    scope = _checked_sources(sources, provider_id)
    units, annual, zero, labels = [], [], [], set()

    def witness(row, start, end, role):
        hit = row['hit']; display = _nfkc(hit['excerpt'])
        return {**scope, 'evidence_id': row['evidence_id'], 'citation_id': hit['citation_id'],
            'source_sha256': hashlib.sha256(hit['excerpt'].encode()).hexdigest(),
            'display_sha256': hashlib.sha256(display.encode()).hexdigest(),
            'offset_basis': 'NFKC_delivered_excerpt_unicode_codepoints',
            'start': start, 'end': end, 'text': display[start:end], 'role': role}

    def add(kind, text, witnesses, **fields):
        unit = {'kind': kind, 'text': text, 'evidence_ids': sorted({w['evidence_id'] for w in witnesses}),
            'witnesses': witnesses, **fields}
        unit['unit_id'] = 'fee-unit-' + _digest(unit)
        units.append(unit)
        return unit

    for row in sources:
        text = _nfkc(row['hit']['excerpt'])
        add('quote', '“' + text + '”', [witness(row, 0, len(text), 'complete_original')])
        # Entire frames are consumed. Unknown conditions, extra/indented rows,
        # multiple headers, or a mixed share label produce quotes only.
        operating = _OPERATING.fullmatch(text) if row['hit'].get('conditions') == [] else None
        if operating:
            label = operating['label_before'] + operating['label_after']
            fields = tuple(zip(_FEES, ('management', 'custody', 'service')))
            if re.fullmatch(_LABEL, label) and all(Decimal(operating[key]) <= 100 for _, key in fields):
                labels.add(label)
                scope_witness = witness(row, 0, len(text), 'complete_single_share_annual_table')
                for fee, key in fields:
                    rate = operating[key]
                    u = add('rate', f'本轮原文“{label}”份额的{fee}年费率为{rate}%。',
                        [scope_witness], fee_type=fee, rate=rate, rate_unit='percent_per_year')
                    if Decimal(rate) > 0:
                        annual.append(u)
                add('deduction', f'本轮原文“{label}”列示的管理费、托管费、销售服务费将从基金资产中扣除。',
                    [scope_witness])
        redemption = _REDEMPTION.fullmatch(text) if row['hit'].get('conditions') == [] else None
        if redemption:
            lines = redemption['rows'].splitlines()
            matches = [_TIER.fullmatch(line) for line in lines]
            intervals = [_interval(m[1]) for m in matches] if all(matches) else []
            if (1 <= len(matches) <= 12 and intervals and all(i is not None for i in intervals)
                    and text.count('赎回费') == 1
                    and all(Decimal(m[2]) <= 100 for m in matches)
                    and not any(_overlap(a, b) for i, a in enumerate(intervals) for b in intervals[i + 1:])):
                label = redemption['label']; labels.add(label)
                for m in matches:
                    u = add('redemption', f'本轮原文“{label}”份额的赎回费在原文“{m[1]}”档为{m[2]}%。',
                        [witness(row, 0, len(text), 'complete_single_share_redemption_table')],
                        condition=m[1], rate=m[2], rate_unit='percent_per_redemption_tier')
                    if Decimal(m[2]) == 0:
                        zero.append(u)
    # Multiple tables purporting to supply the same typed field are ambiguous,
    # including equal values: do not choose one arbitrarily for a conclusion.
    rates = [u for u in units if u['kind'] == 'rate']
    tiers = [u for u in units if u['kind'] == 'redemption']
    if (len(labels) > 1 or len({u['fee_type'] for u in rates}) != len(rates)
            or len({u['condition'] for u in tiers}) != len(tiers)):
        units = [u for u in units if u['kind'] == 'quote']; annual = []; zero = []
    if len(zero) == 1 and annual:
        selected = sorted(annual, key=lambda u: (u['fee_type'] != '销售服务费', u['fee_type']))[0]
        add('comparison', '所引赎回费零档只说明该项费率为零；同一份额原文另列非零的'
            + selected['fee_type'] + '年费率，不能认为全部费用为零。',
            zero[0]['witnesses'] + selected['witnesses'],
            rule='zero_redemption_tier_does_not_establish_all_fee_rates_zero')
    add('unknown', _UNKNOWN, [witness(row, 0, len(_nfkc(row['hit']['excerpt'])),
        'delivered_scope_only') for row in sources])
    catalogue = {'contract': 'navren.public-fee-statement-catalog/1', 'scope': scope,
        'units': units, 'followups': [_FOLLOWUP],
        'limitation': 'finite_table_grammar_not_general_natural_language_entailment'}
    catalogue['catalog_sha256'] = _digest(catalogue)
    return catalogue


def extend_time_catalog(catalog, evidence, *, cited_ids=None):
    """Exact whole originals under the unchanged typed time boundary only.

    The existing binding function decides notation, counting unit, origin and
    same-version limit/definition. No protected prose/hash is promoted to a
    semantic proof. Both originals must be displayed; exceptions remain intact.
    """
    from .knowledge_semantics import public_time_basis_bindings
    if not isinstance(evidence, Mapping):
        _fail()
    scope = catalog['scope']; selected = []; contexts = set()
    for eid, entry in sorted(evidence.items()):
        if cited_ids is not None and eid not in cited_ids:
            continue
        data = entry.get('data', {})
        if (entry.get('id') != eid or entry.get('tool') != 'knowledge_search'
                or entry.get('source') != 'public_knowledge' or not entry.get('context_hash')
                or data.get('ready') is not True or data.get('scope') != 'public'
                or data.get('provider_id') != scope['provider_id']
                or data.get('public_release_id') != scope['release_id'] or data.get('as_of') != scope['as_of']):
            continue
        for hit in data.get('hits', []):
            if (not {'liquidity', 'terms'} & set(hit.get('facets', []))
                    or any(hit.get(key) != scope[key] for key in _IDENTITY)
                    or hit.get('visibility') != 'public' or hit.get('outbound_allowed') is not True
                    or hit.get('content_status') != 'full_text' or hit.get('claims_are_account_facts') is not False
                    or hit.get('withdrawn', False) is not False
                    or not all(type(hit.get(k)) is str and hit[k] for k in
                        ('citation_id', 'document_id', 'document_version_id', 'excerpt'))
                    or not 0 < len(hit['excerpt']) <= 1198):
                continue
            contexts.add(entry['context_hash'])
            selected.append({'evidence_id': eid, 'hit': deepcopy(hit), 'as_of': data['as_of']})
    if len(contexts) > 1:
        _fail()
    rows = canonical_source_rows(selected)
    if not rows:
        return deepcopy(catalog)
    # Probe only entire eligible source excerpts, with canonical representatives.
    # This reuses the original explicit/missing judgment without adding grammar.
    projected = {}
    for row in rows:
        eid = row['evidence_id']
        if eid not in projected:
            projected[eid] = deepcopy(evidence[eid]); projected[eid]['data']['hits'] = []
        projected[eid]['data']['hits'].append(deepcopy(row['hit']))
    quotes = [{'text': '“' + _nfkc(r['hit']['excerpt']) + '”', 'evidence_ids': [r['evidence_id']]} for r in rows]
    bindings = public_time_basis_bindings({'text': '\n'.join(q['text'] for q in quotes), 'claims': quotes},
        list(projected.values()))
    bindings = [b for b in bindings if b['answer_status'] == 'explicit']
    if not bindings:
        return deepcopy(catalog)
    result = deepcopy(catalog)
    result.pop('catalog_sha256')
    for binding in bindings:
        witnesses = []; texts = []; eids = set()
        for role in ('limit', 'definition'):
            matches = [r for r in rows if r['evidence_id'] == binding[role + '_evidence_id']
                and r['hit']['citation_id'] == binding[role + '_citation_id']
                and all(r['hit'].get(k) == binding.get(k) for k in (*_IDENTITY, 'document_id', 'document_version_id'))]
            if len(matches) != 1:
                _fail()
            row = matches[0]; hit = row['hit']; display = _nfkc(hit['excerpt'])
            quote = '“' + display + '”'
            if quote not in texts:
                texts.append(quote)
            eids.add(row['evidence_id'])
            witnesses.append({'evidence_id': row['evidence_id'], 'citation_id': hit['citation_id'],
                **{k: hit[k] for k in (*_IDENTITY, 'document_id', 'document_version_id')},
                'provider_id': scope['provider_id'], 'as_of': row['as_of'],
                'source_sha256': hashlib.sha256(hit['excerpt'].encode()).hexdigest(),
                'display_sha256': hashlib.sha256(display.encode()).hexdigest(),
                'offset_basis': 'NFKC_delivered_excerpt_unicode_codepoints', 'start': 0,
                'end': len(display), 'text': display, 'role': role})
        # Pair the full originals in one authored/claim unit: the unchanged
        # time guard requires an explicit counting basis in every time claim.
        unit = {'kind': 'time_exact', 'text': '\n'.join(texts), 'evidence_ids': sorted(eids),
            'witnesses': witnesses, 'time_pairs': [{'pair_id': 'time-pair-' + _digest(binding),
                'roles': ['limit', 'definition'], 'binding': binding}]}
        same = [u for u in result['units'] if u['text'] == unit['text']]
        if same:
            source_keys = lambda u: {tuple(w[k] for k in ('evidence_id', 'citation_id', 'as_of',
                'product_id', 'share_class_id', 'channel_id', 'release_id', 'document_id',
                'document_version_id', 'source_sha256')) for w in u['witnesses']}
            if len(same) != 1 or same[0]['kind'] not in ('quote', 'time_exact') or source_keys(same[0]) != source_keys(unit):
                _fail()
            pairs = {p['pair_id']: p for p in same[0].get('time_pairs', []) + unit['time_pairs']}
            unit['time_pairs'] = [pairs[k] for k in sorted(pairs)]
            unit['unit_id'] = 'time-unit-' + _digest(unit)
            same[0].clear(); same[0].update(unit)
        else:
            unit['unit_id'] = 'time-unit-' + _digest(unit)
            result['units'].append(unit)
    result['catalog_sha256'] = _digest(result)
    return result


_MIXED_INTEGER = r'(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+|0|[1-9][0-9]*)'
_MIXED_M_CONDITION = (r'(?:M[<≤>≥]' + _MIXED_INTEGER + r'|'
    + _MIXED_INTEGER + r'[<≤]M[<≤]' + _MIXED_INTEGER + r')')
_MIXED_PURCHASE_ROW = re.compile(
    r'(?P<prefix>申购费 |\(前收费\) )?(?P<condition>' + _MIXED_M_CONDITION + r') '
    r'(?:(?P<rate>' + _NUMBER + r')%|(?P<amount>' + _MIXED_INTEGER + r'(?:\.[0-9]{1,2})?)元/笔)')
_OPEN_END_LINE = '运作方式 普通开放式 开放频率 每个开放日'


def _mixed_m_interval(condition):
    single = re.fullmatch(r'M([<≤>≥])(' + _MIXED_INTEGER + r')', condition)
    if single:
        op, value = single[1], int(single[2].replace(',', ''))
        if value <= 0:
            return None
        return ((None, False, value, op == '≤') if op in ('<', '≤')
            else (value, op == '≥', None, False))
    pair = re.fullmatch(r'(' + _MIXED_INTEGER + r')([<≤])M([<≤])('
        + _MIXED_INTEGER + r')', condition)
    if pair is None:
        return None
    lo, hi = int(pair[1].replace(',', '')), int(pair[4].replace(',', ''))
    if lo <= 0 or hi <= lo:
        return None
    return lo, pair[2] == '≤', hi, pair[3] == '≤'


def _mixed_complete_intervals(intervals):
    if (not intervals or any(item is None for item in intervals)
            or any(_overlap(a, b) for i, a in enumerate(intervals) for b in intervals[i + 1:])):
        return False
    ordered = sorted(intervals, key=lambda item: (-1 if item[0] is None else item[0]))
    return (ordered[0][0] is None and ordered[-1][2] is None
        and all(a[2] == b[0] and a[3] != b[1] for a, b in zip(ordered, ordered[1:])))


def _mixed_purchase_redemption_frame(row):
    """Consume every M and N row; unknown/missing/overlapping rows yield no facts."""
    if (row['hit'].get('conditions') != [] or type(row['hit'].get('title')) is not str
            or '基金产品资料概要' not in row['hit']['title']):
        return None
    text = _nfkc(row['hit']['excerpt'])
    frame = _REDEMPTION.fullmatch(text)
    if frame is None:
        return None
    lines = frame['rows'].splitlines()
    purchase, redemption, prefixes, saw_redemption = [], [], [], False
    for line in lines:
        tier = _TIER.fullmatch(line)
        if tier is not None:
            saw_redemption = True
            interval = _interval(tier[1])
            if interval is None or not 0 <= Decimal(tier[2]) <= 100:
                return None
            redemption.append({'condition': tier[1], 'rate': tier[2], 'interval': interval})
            continue
        if saw_redemption:
            return None
        match = _MIXED_PURCHASE_ROW.fullmatch(line)
        if match is None:
            return None
        interval = _mixed_m_interval(match['condition'])
        if interval is None:
            return None
        if match['rate'] is not None and not 0 <= Decimal(match['rate']) <= 100:
            return None
        if match['amount'] is not None and Decimal(match['amount'].replace(',', '')) < 0:
            return None
        prefixes.append(match['prefix'])
        purchase.append(interval)
    if (not 2 <= len(purchase) <= 12 or not 1 <= len(redemption) <= 12
            or prefixes.count('申购费 ') != 1 or prefixes.count('(前收费) ') != 1
            or sum(line.startswith('赎回费 ') for line in lines) != 1
            or not _mixed_complete_intervals(purchase)
            or not _mixed_complete_intervals([item['interval'] for item in redemption])):
        return None
    return {'label': frame['label'], 'redemption': redemption}


def _delivery_witness(scope, row, role):
    hit = row['hit']; display = _nfkc(hit['excerpt'])
    return {**scope, **{key: hit[key] for key in (*_IDENTITY, 'document_id', 'document_version_id')},
        'evidence_id': row['evidence_id'], 'citation_id': hit['citation_id'], 'as_of': row['as_of'],
        'source_sha256': hashlib.sha256(hit['excerpt'].encode()).hexdigest(),
        'display_sha256': hashlib.sha256(display.encode()).hexdigest(),
        'offset_basis': 'NFKC_delivered_excerpt_unicode_codepoints', 'start': 0,
        'end': len(display), 'text': display, 'role': role}


def _delivery_unit(kind, text, witnesses, **fields):
    unit = {'kind': kind, 'text': text, 'evidence_ids': sorted({w['evidence_id'] for w in witnesses}),
        'witnesses': deepcopy(witnesses), **fields}
    unit['unit_id'] = 'fee-unit-' + _digest(unit)
    return unit


def _mixed_public_quote_id(row, public_quote_texts):
    # Import the original registration grammar only for the new mixed branch.
    # The representative ID displays the whole source, not one selected amount.
    from .bank_tools import monetary_literals, encoded
    if (not isinstance(public_quote_texts, Mapping)
            or any(type(key) is not str or type(text) is not str
                for key, text in public_quote_texts.items())):
        _fail()
    hit = row['hit']; amounts = set(monetary_literals(hit['excerpt']))
    if not amounts:
        _fail()
    registered = {'p' + hashlib.sha256(encoded([row['evidence_id'], hit['citation_id'], value, currency])
        .encode()).hexdigest()[:20] for value, currency in amounts}
    eligible = sorted(fid for fid in registered if public_quote_texts.get(fid) == hit['excerpt'])
    if not eligible:
        _fail()
    return eligible[0]


def _mixed_open_end_source(sources, evidence, scope, label, fee_title):
    contexts = {evidence[row['evidence_id']]['context_hash'] for row in sources}
    if len(contexts) != 1:
        _fail()
    share = re.search(r'(?:^|:)([0-9]{6})\Z', scope['share_class_id'])
    product = re.search(r'(?:^|:)([0-9]{6})\Z', scope['product_id'])
    if share is None or product is None:
        return None
    context = next(iter(contexts)); candidates = []
    for eid, entry in sorted(evidence.items()):
        data = entry.get('data', {})
        if (entry.get('id') != eid or entry.get('tool') != 'knowledge_search'
                or entry.get('source') != 'public_knowledge' or entry.get('context_hash') != context
                or data.get('ready') is not True or data.get('scope') != 'public'
                or data.get('provider_id') != scope['provider_id']
                or data.get('public_release_id') != scope['release_id'] or data.get('as_of') != scope['as_of']):
            continue
        for hit in data.get('hits', []):
            if (not {'general', 'terms'} <= set(hit.get('facets', [])) or hit.get('title') != fee_title
                    or any(hit.get(key) != scope[key] for key in (*_IDENTITY, 'document_id', 'document_version_id'))
                    or hit.get('visibility') != 'public' or hit.get('outbound_allowed') is not True
                    or hit.get('content_status') != 'full_text' or hit.get('claims_are_account_facts') is not False
                    or hit.get('withdrawn', False) is not False or hit.get('conditions') != []
                    or type(hit.get('citation_id')) is not str or not hit['citation_id']
                    or type(hit.get('excerpt')) is not str or not 0 < len(hit['excerpt']) <= 1198):
                continue
            text = _nfkc(hit['excerpt']); lines = text.splitlines()
            share_lines = re.findall(r'^下属基金简称 (' + _LABEL
                + r') 下属基金交易代码 ([0-9]{6})$', text, re.M)
            fund_codes = re.findall(r'^基金简称 [\u4e00-\u9fff]{2,80} 基金代码 ([0-9]{6})$', text, re.M)
            if (lines.count(_OPEN_END_LINE) != 1 or text.count('运作方式') != 1
                    or text.count('开放频率') != 1
                    or [(name, code) for name, code in share_lines if name == label] != [(label, share[1])]
                    or sum(code == share[1] for _, code in share_lines) != 1
                    or fund_codes != [product[1]]):
                continue
            candidates.append({'evidence_id': eid, 'hit': deepcopy(hit), 'as_of': data['as_of']})
    candidates = canonical_source_rows(candidates)
    return candidates[0] if len(candidates) == 1 else None


def extend_delivery_catalog(catalog, evidence, *, public_quote_texts=None, cited_ids=None):
    """Add only a complete mixed-table/identity grammar; preserve old time semantics.

    ``public_quote_texts`` is a trusted eligible host fact_id -> canonical full
    source text map. It never authorizes an amount by a text fragment or alias.
    New mixed units require explicit selection provenance at both terminals.
    """
    result = extend_time_catalog(catalog, evidence, cited_ids=cited_ids)
    selected_evidence = {eid: entry for eid, entry in evidence.items()
        if cited_ids is None or eid in cited_ids}
    proposed = source_rows_from_evidence(selected_evidence)
    if proposed is None:
        return result
    sources, provider = proposed
    mixed = [(row, frame) for row in sources
        if (frame := _mixed_purchase_redemption_frame(row)) is not None]
    if len(mixed) != 1 or any(unit['kind'] == 'redemption' for unit in catalog['units']):
        return result
    if catalog != build_catalog(sources, provider):
        _fail()
    row, frame = mixed[0]; scope = catalog['scope']
    identity = _mixed_open_end_source(sources, selected_evidence, scope, frame['label'], row['hit']['title'])
    if identity is None:
        # A display label alone does not establish which share code it names.
        # No new facts, including redemption units, may bridge an unknown share.
        return result
    fid = _mixed_public_quote_id(row, public_quote_texts)
    matches = [unit for unit in result['units'] if unit['kind'] == 'quote'
        and unit['evidence_ids'] == [row['evidence_id']]
        and len(unit['witnesses']) == 1
        and unit['witnesses'][0]['citation_id'] == row['hit']['citation_id']
        and unit['text'] == '“' + _nfkc(row['hit']['excerpt']) + '”']
    if len(matches) != 1:
        _fail()
    quote = matches[0]
    pointer = _delivery_unit('public_quote', quote['text'], quote['witnesses'],
        public_quote_fact_id=fid, representative='complete_original_not_selected_amount')
    quote.clear(); quote.update(pointer)
    table_witness = _delivery_witness(scope, row, 'complete_single_share_mixed_purchase_redemption_table')
    for tier in frame['redemption']:
        result['units'].append(_delivery_unit('redemption',
            f'本轮原文“{frame["label"]}”份额的赎回费在原文“{tier["condition"]}”档为{tier["rate"]}%。',
            [table_witness], condition=tier['condition'], rate=tier['rate'],
            rate_unit='percent_per_redemption_tier'))
    if identity is not None:
        identity_witness = _delivery_witness(scope, identity, 'complete_same_share_open_end_identity')
        result['units'].append(_delivery_unit('open_end',
            f'本轮原文“{frame["label"]}”份额列示运作方式为普通开放式，开放频率为每个开放日。',
            [identity_witness]))
        zero = [tier for tier in frame['redemption'] if Decimal(tier['rate']) == 0
            and tier['interval'][0] is not None and tier['interval'][1] is True
            and tier['interval'][2] is None]
        if len(zero) == 1:
            threshold = zero[0]['interval'][0]
            before = [tier for tier in frame['redemption'] if Decimal(tier['rate']) > 0
                and tier['interval'][2] == threshold and tier['interval'][3] is False]
            if len(before) == 1:
                zero_witness = deepcopy(table_witness); zero_witness['role'] = 'zero_redemption_threshold'
                before_witness = deepcopy(table_witness); before_witness['role'] = 'nonzero_prethreshold_redemption_tier'
                result['units'].append(_delivery_unit('comparison',
                    f'本轮原文“{frame["label"]}”份额的“{zero[0]["condition"]}”档列示赎回费率为{zero[0]["rate"]}%；'
                    f'“{before[0]["condition"]}”档另列{before[0]["rate"]}%赎回费率；同份额原文列示“普通开放式”和“每个开放日”。'
                    '零赎回费档是费用条件，不能将该档门槛当作锁定期，也不能据此认定必须达到该持有期限后才能申请赎回。',
                    [zero_witness, before_witness, identity_witness],
                    rule='redemption_fee_threshold_is_not_open_end_redemption_lock',
                    zero_condition=zero[0]['condition'], nonzero_condition=before[0]['condition']))
    result['requires_explicit_unit_selection'] = True
    result['delivery_extension'] = 'complete_mixed_purchase_redemption_and_same_share_open_end/1'
    result.pop('catalog_sha256')
    result['catalog_sha256'] = _digest(result)
    return result


def uses_unit_selections(args):
    """Detect selectors only at actual part positions, including mixed-key parts.

    Detection never admits a selection. The expander rejects malformed shapes,
    text mixed with an ID, and every other non-selector part.
    """
    if type(args) is not dict:
        return False
    for field in ('direct_answer', 'conditions', 'unknowns'):
        rows = [args.get(field)] if field == 'direct_answer' else args.get(field)
        if type(rows) is not list:
            continue
        for parts in rows:
            if type(parts) is not list:
                continue
            if any(type(part) is dict and 'fee_statement_unit_id' in part for part in parts):
                return True
    return False


def _unit_selection_parts(args):
    for field in ('direct_answer', 'conditions', 'unknowns'):
        if field not in args:
            _fail()
        rows = [args[field]] if field == 'direct_answer' else args[field]
        if type(rows) is not list or (field != 'direct_answer' and len(rows) > 6):
            _fail()
        for i, parts in enumerate(rows):
            if type(parts) is not list or not 1 <= len(parts) <= 64:
                _fail()
            for j, part in enumerate(parts):
                if type(part) is not dict:
                    _fail()
                path = f'{field}[{j}]' if field == 'direct_answer' else f'{field}[{i}][{j}]'
                yield field, path, part


def _unit_selection_catalog(catalog):
    if type(catalog) is not dict or type(catalog.get('units')) is not list:
        _fail()
    units = {}
    for unit in catalog['units']:
        if (type(unit) is not dict or type(unit.get('unit_id')) is not str or not unit['unit_id']
                or unit['unit_id'] in units or type(unit.get('text')) is not str
                or unit.get('kind') not in ('quote', 'rate', 'redemption', 'deduction',
                    'comparison', 'unknown', 'time_exact', 'open_end', 'public_quote')
                or type(unit.get('evidence_ids')) is not list
                or any(type(eid) is not str or not eid for eid in unit['evidence_ids'])):
            _fail()
        if (unit['kind'] == 'public_quote' and (type(unit.get('public_quote_fact_id')) is not str
                or not re.fullmatch(r'p[a-f0-9]{20}', unit['public_quote_fact_id'])
                or catalog.get('requires_explicit_unit_selection') is not True)):
            _fail()
        units[unit['unit_id']] = unit
    return units


def expand_unit_selections(args, catalog):
    """Expand only explicitly selected units, with no inference or auto-selection.

    The existing authored validator and original terminal guards still validate
    the expanded result. Public monetary references retain their original gate;
    this function does not mint, resolve, or approve such a reference.
    """
    if (type(args) is not dict or type(args.get('claims')) is not list or args['claims']
            or not {'direct_answer', 'conditions', 'unknowns', 'claims', 'followups'} <= set(args)
            or set(args) - {'direct_answer', 'conditions', 'unknowns', 'claims', 'followups',
                'draft_ids', 'widget_ids', 'amount_claims', 'historical_quote_ids'}
            or any(args.get(k) for k in ('draft_ids', 'widget_ids', 'amount_claims', 'historical_quote_ids'))):
        _fail()
    units = _unit_selection_catalog(catalog)
    expanded = deepcopy(args)
    chosen, claimed, direct_substance = set(), set(), False
    for field, path, part in _unit_selection_parts(expanded):
        if set(part) == {'public_quote_fact_id'}:
            if field == 'unknowns' or type(part['public_quote_fact_id']) is not str or not part['public_quote_fact_id']:
                _fail()
            continue
        if set(part) != {'fee_statement_unit_id'} or type(part['fee_statement_unit_id']) is not str:
            _fail()
        unit_id = part['fee_statement_unit_id']
        unit = units.get(unit_id)
        if unit is None or (field == 'unknowns') != (unit['kind'] == 'unknown'):
            _fail()
        chosen.add(unit_id)
        part.clear()
        if unit['kind'] == 'public_quote':
            part['public_quote_fact_id'] = unit['public_quote_fact_id']
            continue
        part['text'] = unit['text']
        if field == 'direct_answer' and unit['kind'] in ('rate', 'redemption', 'deduction', 'comparison'):
            direct_substance = True
        if unit['kind'] != 'unknown' and unit_id not in claimed:
            expanded['claims'].append({'text': unit['text'], 'evidence_ids': deepcopy(unit['evidence_ids'])})
            claimed.add(unit_id)
    # Full fee originals must be model selections as well. The host never fills
    # a missing quote, fee field, comparison, unknown, or complete time unit.
    if (not direct_substance or not {uid for uid, unit in units.items()
            if unit['kind'] in ('quote', 'public_quote')} <= chosen):
        _fail()
    return expanded


def validate_authored(args, catalog, *, selection_args=None):
    """Reject unsupported whole fields. Never remove, repair, or rewrite text."""
    if (selection_args is None and (catalog.get('requires_explicit_unit_selection') is True
            or any(unit.get('kind') in ('open_end', 'public_quote') for unit in catalog['units']))):
        _fail()
    units = {_nfkc(u['text']): u for u in catalog['units']}
    if len(units) != len(catalog['units']) or type(args) is not dict:
        _fail()
    if (not {'direct_answer', 'conditions', 'unknowns', 'claims', 'followups'} <= set(args)
            or set(args) - {'direct_answer', 'conditions', 'unknowns', 'claims', 'followups',
                'draft_ids', 'widget_ids', 'amount_claims', 'historical_quote_ids'}
            or any(args.get(k) for k in ('draft_ids', 'widget_ids', 'amount_claims', 'historical_quote_ids'))):
        _fail()
    claims, used, proof, direct_substance, time_roles = {}, set(), [], False, {}
    for i, claim in enumerate(args['claims']):
        if type(claim) is not dict or set(claim) != {'text', 'evidence_ids'}:
            _fail()
        text = _nfkc(claim['text']); unit = units.get(text)
        if unit is None or unit['kind'] == 'unknown' or claim['evidence_ids'] != unit['evidence_ids'] or text in claims:
            _fail()
        claims[text] = unit
        proof.append({'path': f'claims[{i}]', 'unit_id': unit['unit_id'], 'text_sha256': _digest(text),
            'evidence_ids': unit['evidence_ids']})
    for field in ('direct_answer', 'conditions', 'unknowns'):
        rows = [args[field]] if field == 'direct_answer' else args[field]
        if type(rows) is not list or (field != 'direct_answer' and len(rows) > 6):
            _fail()
        for i, parts in enumerate(rows):
            if type(parts) is not list or not 1 <= len(parts) <= 64:
                _fail()
            for j, part in enumerate(parts):
                path = f'{field}[{j}]' if field == 'direct_answer' else f'{field}[{i}][{j}]'
                if type(part) is not dict:
                    _fail()
                if set(part) == {'public_quote_fact_id'}:
                    if field == 'unknowns' or type(part['public_quote_fact_id']) is not str or not part['public_quote_fact_id']:
                        _fail()
                    proof.append({'path': path, 'public_quote_fact_id': part['public_quote_fact_id'],
                        'eligibility': 'requires_unchanged_original_public_quote_guards'})
                    continue
                if set(part) != {'text'}:
                    _fail()
                text = _nfkc(part['text']); unit = units.get(text)
                if unit is None or (field == 'unknowns') != (unit['kind'] == 'unknown'):
                    _fail()
                if unit['kind'] != 'unknown':
                    if text not in claims:
                        _fail()
                    used.add(text)
                    if field == 'direct_answer' and unit['kind'] in ('rate', 'redemption', 'deduction', 'comparison'):
                        direct_substance = True
                    for pair in unit.get('time_pairs', []):
                        time_roles.setdefault(pair['pair_id'], set()).update(pair['roles'])
                proof.append({'path': path, 'unit_id': unit['unit_id'], 'text_sha256': _digest(text),
                    'evidence_ids': unit['evidence_ids']})
    if used != set(claims) or not direct_substance or type(args['followups']) is not list:
        _fail()
    if any(roles != {'limit', 'definition'} for roles in time_roles.values()):
        _fail()
    if args['followups'] not in ([], catalog['followups']):
        _fail()
    for i, text in enumerate(args['followups']):
        proof.append({'path': f'followups[{i}]', 'controlled_text_sha256': _digest(text)})
    result = {'contract': 'navren.public-fee-authored-statements/1',
        'arguments_sha256': _digest(args), 'catalog_sha256': catalog['catalog_sha256'],
        'authored_fields': proof, 'host_rewrite': False,
        'guarantee': 'finite_source_bound_statements_only_not_full_financial_acceptance'}
    if selection_args is not None:
        expanded = expand_unit_selections(selection_args, catalog)
        if expanded != args or _digest(expanded) != _digest(args):
            _fail()
        units = _unit_selection_catalog(catalog)
        selected = []
        for field, path, part in _unit_selection_parts(selection_args):
            if 'fee_statement_unit_id' in part:
                unit_id = part['fee_statement_unit_id']
                record = {'path': path, 'unit_id': unit_id,
                    'evidence_ids': deepcopy(units[unit_id]['evidence_ids'])}
                if units[unit_id]['kind'] == 'public_quote':
                    record['public_quote_fact_id'] = units[unit_id]['public_quote_fact_id']
                selected.append(record)
        result['explicit_unit_selection'] = {'arguments_sha256': _digest(selection_args),
            'expanded_arguments_sha256': _digest(expanded), 'selected': selected}
    return result


def restrict_schema(schema, catalog):
    """Same schema builder for real calls and their unchanged budget estimate."""
    props = schema['properties']
    for field in ('direct_answer', 'conditions', 'unknowns'):
        props[field] = deepcopy(props[field])
        parts = props[field] if field == 'direct_answer' else props[field]['items']
        parts['description'] = ('逐片显式选择fee_statement_unit_id；必需货币金额可引用public_quote_fact_id。'
            '禁止提交text；宿主仅逐字展开所选单元，不补选事实或结论。')
        variants = parts['items']['oneOf']
        maximum = variants[0]['properties']['text']['maxLength']
        available = [u for u in catalog['units']
            if (field == 'unknowns') == (u['kind'] == 'unknown') and len(u['text']) <= maximum]
        description = ('只显式选择本轮单元ID，不提交正文或evidence_ids。宿主逐字展开所选正文并绑定claims，'
            '不补选其他单元。所有kind为quote或public_quote的费用完整原文都必须显式选择；'
            'public_quote仅展开为原public_quote_fact_id指针，仍须原金额与完整引文守卫；direct_answer还须选择'
            '至少一个费率、赎回费档、资产扣除或费用比较单元。claims必须显式为[]。本分区unit_id到原文：\n'
            + json.dumps({u['unit_id']: u['text'] for u in available}, ensure_ascii=False))
        variants[0] = {'type': 'object', 'properties': {'fee_statement_unit_id': {
            'type': 'string', 'minLength': 1, 'maxLength': 256,
            'enum': [u['unit_id'] for u in available], 'description': description}},
            'required': ['fee_statement_unit_id'], 'additionalProperties': False}
        if field == 'unknowns':
            variants[1]['properties']['public_quote_fact_id']['enum'] = []
    props['claims']['maxItems'] = 0
    props['claims']['description'] = '必须显式提交空数组[]；宿主仅为显式选择的非unknown单元生成绑定claims。'
    props['followups']['items']['enum'] = catalog['followups']
    props['followups']['maxItems'] = 1
    return schema
