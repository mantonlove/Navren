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
            + selected['fee_type'] + '年费率，不能据此认定全部费用为零。',
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


def validate_authored(args, catalog):
    """Reject unsupported whole fields. Never remove, repair, or rewrite text."""
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
    return {'contract': 'navren.public-fee-authored-statements/1',
        'arguments_sha256': _digest(args), 'catalog_sha256': catalog['catalog_sha256'],
        'authored_fields': proof, 'host_rewrite': False,
        'guarantee': 'finite_source_bound_statements_only_not_full_financial_acceptance'}


def restrict_schema(schema, catalog):
    """Same schema builder for real calls and their unchanged budget estimate."""
    props = schema['properties']
    for field in ('direct_answer', 'conditions', 'unknowns'):
        parts = props[field] if field == 'direct_answer' else props[field]['items']
        text = parts['items']['oneOf'][0]['properties']['text']
        text['enum'] = [u['text'] for u in catalog['units']
            if (field == 'unknowns') == (u['kind'] == 'unknown') and len(u['text']) <= text['maxLength']]
    props['claims']['items']['properties']['text']['enum'] = [u['text'] for u in catalog['units'] if u['kind'] != 'unknown']
    props['followups']['items']['enum'] = catalog['followups']
    props['followups']['maxItems'] = 1
    return schema
