"""Finite, explicitly selected public time statements; no delivery authority.

The catalogue is a source proposal. Producer and Boundary must independently
verify its witnesses against their actual delivered/pinned request snapshots.
No database, model, rewrite, monetary exemption, or fee-source selection occurs
here. Original renderer, amount, fee, time, reviewer and FinalProof gates remain.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import hashlib
import json
import re
import unicodedata


class TimeStatementContractError(ValueError):
    pass


CATALOG_CONTRACT = 'navren.public-time-statement-catalog/1'
SELECTION_CONTRACT = 'navren.public-time-statement-selection/1'
PROOF_CONTRACT = 'navren.public-time-authored-statements/1'
REFERENCE_CONTRACT = 'navren.public-time-reference-selection/1'
_IDENTITY = ('product_id', 'share_class_id', 'channel_id', 'release_id')
_REQUIRED = ('direct_answer', 'conditions', 'unknowns', 'claims', 'followups')
_EMPTY_OPTIONAL = ('draft_ids', 'widget_ids', 'amount_claims', 'historical_quote_ids')


def _fail():
    raise TimeStatementContractError('PUBLIC_TIME_STATEMENT_UNSUPPORTED')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _nfkc(value):
    if type(value) is not str:
        _fail()
    return unicodedata.normalize('NFKC', value)


def _unit_id(unit):
    return 'time-unit-' + _digest({k: v for k, v in unit.items() if k != 'unit_id'})


def _conclusion(exact):
    pair = exact['time_pairs'][0]
    binding = pair['binding']
    match = re.fullmatch(r'T\+([1-9][0-9]{0,3})', binding.get('notation', ''))
    # These are the actual original binding enums, not an invented translation.
    if (not match or binding.get('counting_unit') != '工作日'
            or binding.get('answer_status') != 'explicit'
            or pair.get('roles') != ['limit', 'definition']):
        _fail()
    number = match[1]
    unit = {'kind': 'time_conclusion',
        'text': f'本轮所引原文的T+{number}使用工作日计日，不能据此保证{number}个自然日到账；'
            '具体支付条件和例外以所引完整原文为准。',
        'evidence_ids': deepcopy(exact['evidence_ids']),
        'witnesses': deepcopy(exact['witnesses']),
        'time_pairs': deepcopy(exact['time_pairs']),
        'required_exact_unit_id': exact['unit_id'],
        'rule': 'working_day_basis_does_not_guarantee_same_number_of_calendar_days'}
    unit['unit_id'] = _unit_id(unit)
    return unit


def build_time_catalog(evidence):
    """Return one complete time pair and its finite conclusion, or None.

    Only request-local knowledge_search time-facet originals participate. Other
    facets, including incidental fee/money hits in the same search, never enter
    the catalogue or receive an amount qualification. Unsupported, incomplete,
    ambiguous or monetary time originals fail closed without a fallback unit.
    """
    from .public_fee_statement_contract import extend_time_catalog
    from .knowledge_semantics import _concrete_time_conclusions, public_time_basis_bindings
    from .metric_claims import monetary_occurrences, MetricClaimError

    if not isinstance(evidence, Mapping):
        return None
    try:
        searches = [(eid, entry) for eid, entry in evidence.items()
            if isinstance(entry, Mapping) and entry.get('tool') == 'knowledge_search']
        if len(searches) != 1:
            return None
        eid, entry = searches[0]
        data = entry.get('data')
        if (type(eid) is not str or not eid or entry.get('id') != eid
                or entry.get('source') != 'public_knowledge'
                or type(entry.get('context_hash')) is not str or not entry['context_hash']
                or not isinstance(data, Mapping) or data.get('ready') is not True
                or data.get('scope') != 'public' or data.get('status') not in ('ready', 'partial')
                or any(type(data.get(k)) is not str or not data[k]
                    for k in ('provider_id', 'public_release_id', 'as_of'))
                or date.fromisoformat(data['as_of']).isoformat() != data['as_of']
                or type(data.get('hits')) is not list or not data['hits']):
            return None
        hits = []
        identities, versions, citations = set(), set(), set()
        for hit in data['hits']:
            if not isinstance(hit, Mapping) or type(hit.get('facets')) is not list:
                return None
            if not {'liquidity', 'terms'} & set(hit['facets']):
                continue
            if ('fees' in hit['facets'] or hit.get('visibility') != 'public'
                    or hit.get('outbound_allowed') is not True or hit.get('content_status') != 'full_text'
                    or hit.get('claims_are_account_facts') is not False or hit.get('withdrawn', False) is not False
                    or hit.get('release_id') != data['public_release_id'] or hit.get('conditions') != []
                    or any(type(hit.get(k)) is not str or not hit[k] for k in
                        ('citation_id', 'document_id', 'document_version_id', 'product_id', 'share_class_id'))
                    or 'channel_id' not in hit
                    or not (hit['channel_id'] is None or type(hit['channel_id']) is str)
                    or type(hit.get('excerpt')) is not str or not 0 < len(hit['excerpt']) <= 1198
                    or monetary_occurrences(_nfkc(hit['excerpt']))
                    or hit['citation_id'] in citations):
                return None
            identities.add(tuple(hit[k] for k in _IDENTITY))
            versions.add((hit['document_id'], hit['document_version_id']))
            citations.add(hit['citation_id'])
            hits.append(deepcopy(dict(hit)))
        if not hits or len(identities) != 1 or len(versions) != 1:
            return None
        identity = next(iter(identities)); version = next(iter(versions))
        scope = {'provider_id': data['provider_id'], 'release_id': data['public_release_id'],
            'as_of': data['as_of'], 'context_hash': entry['context_hash'],
            **dict(zip(_IDENTITY, identity)),
            'document_id': version[0], 'document_version_id': version[1]}
        seed = {'contract': CATALOG_CONTRACT, 'scope': scope, 'units': [], 'followups': [],
            'requires_explicit_unit_selection': True,
            'limitation': 'finite_source_bound_time_statements_not_full_financial_acceptance'}
        seed['catalog_sha256'] = _digest(seed)
        projected = {eid: deepcopy(dict(entry))}
        projected[eid]['data'] = deepcopy(dict(data))
        projected[eid]['data']['hits'] = hits
        extended = extend_time_catalog(seed, projected, cited_ids={eid})
        units = extended['units']
        # No arbitrary representative among several horizons or complete pairs.
        if (len(units) != 1 or units[0].get('kind') != 'time_exact'
                or len(units[0].get('time_pairs', [])) != 1):
            return None
        exact = deepcopy(units[0]); pair = exact['time_pairs'][0]
        if (len(exact.get('witnesses', [])) != 2
                or {w['role'] for w in exact['witnesses']} != {'limit', 'definition'}
                or {w['citation_id'] for w in exact['witnesses']} != citations
                or exact.get('evidence_ids') != [eid]
                or any(w['evidence_id'] != eid for w in exact['witnesses'])
                or pair.get('roles') != ['limit', 'definition']
                or exact.get('unit_id') != _unit_id(exact)):
            return None
        binding = pair['binding']
        if (any(binding.get(k) != scope[k] for k in (*_IDENTITY, 'document_id', 'document_version_id'))
                or any(binding.get(role + '_evidence_id') != eid for role in ('limit', 'definition'))):
            return None
        conclusion = _conclusion(exact)
        # Verify the original exact pair still contains the complete affirmative
        # horizon. The finite conclusion never substitutes for the full pair.
        if _concrete_time_conclusions(exact['text']) != {binding['notation'][2:]}:
            return None
        checked = public_time_basis_bindings({'text': exact['text'], 'claims': [
            {'text': exact['text'], 'evidence_ids': [eid]}]}, list(projected.values()))
        if checked != [binding]:
            return None
        if (len(exact['text']) > 1200 or len(conclusion['text']) > 1200
                or len(exact['text']) > 600 and len(exact['text']) + len(conclusion['text']) > 1200):
            return None
        result = deepcopy(seed)
        result['units'] = [exact, conclusion]
        result.pop('catalog_sha256')
        result['catalog_sha256'] = _digest(result)
        return result
    except (KeyError, ValueError, TypeError, AttributeError, MetricClaimError):
        return None


def _catalog_units(catalog):
    if (type(catalog) is not dict or catalog.get('contract') != CATALOG_CONTRACT
            or type(catalog.get('scope')) is not dict
            or catalog.get('requires_explicit_unit_selection') is not True
            or catalog.get('followups') != [] or type(catalog.get('units')) is not list
            or len(catalog['units']) != 2
            or catalog.get('catalog_sha256') != _digest({k: v for k, v in catalog.items()
                if k != 'catalog_sha256'})):
        _fail()
    units = {}
    for unit in catalog['units']:
        if (type(unit) is not dict or unit.get('kind') not in ('time_exact', 'time_conclusion')
                or type(unit.get('text')) is not str or not unit['text']
                or not re.fullmatch(r'time-unit-[a-f0-9]{64}', unit.get('unit_id', ''))
                or unit['unit_id'] != _unit_id(unit) or unit['unit_id'] in units
                or type(unit.get('evidence_ids')) is not list or len(unit['evidence_ids']) != 1
                or type(unit['evidence_ids'][0]) is not str or not unit['evidence_ids'][0]
                or type(unit.get('time_pairs')) is not list or len(unit['time_pairs']) != 1):
            _fail()
        units[unit['unit_id']] = unit
    exacts = [u for u in units.values() if u['kind'] == 'time_exact']
    conclusions = [u for u in units.values() if u['kind'] == 'time_conclusion']
    if len(exacts) != 1 or len(conclusions) != 1 or conclusions[0] != _conclusion(exacts[0]):
        _fail()
    return units


def _selector_schema(units):
    return {'type': 'object', 'properties': {'fee_statement_unit_id': {
        'type': 'string', 'minLength': 1, 'maxLength': 256,
        'enum': [u['unit_id'] for u in units],
        'description': '显式选择本轮完整时间单元，不提交text或public_quote_fact_id。单元原文：\n'
            + json.dumps({u['unit_id']: u['text'] for u in units}, ensure_ascii=False, sort_keys=True)}},
        'required': ['fee_statement_unit_id'], 'additionalProperties': False}


def restrict_time_schema(schema, catalog):
    """Expose two explicit request-bound references; no model-authored body."""
    units = list(_catalog_units(catalog).values())
    if type(schema) is not dict or type(schema.get('properties')) is not dict:
        _fail()
    exact = next(unit for unit in units if unit['kind'] == 'time_exact')
    conclusion = next(unit for unit in units if unit['kind'] == 'time_conclusion')
    pair_ref = exact['time_pairs'][0].get('pair_id')
    if type(pair_ref) is not str or not re.fullmatch(r'time-pair-[a-f0-9]{64}', pair_ref):
        _fail()
    schema.clear()
    schema.update(type='object', properties={
        'pair_ref': {'type': 'string', 'enum': [pair_ref],
            'description': '显式选择本轮完整支付期限和计日定义配对；保留全部条件及例外：' + exact['text']},
        'relation': {'type': 'string', 'enum': [conclusion['rule']],
            'description': '显式选择该原文配对支持的结论：' + conclusion['text']}},
        required=['pair_ref', 'relation'], additionalProperties=False,
        description=REFERENCE_CONTRACT + '：只提交pair_ref和relation；不要正文、claims、proof或其他字段。'
            '宿主按这两项显式选择逐字重建完整原文与有限结论，仍须通过全部原业务校验及完整审核。')
    return schema


def uses_time_reference(args):
    """Recognize partial references too so malformed selections fail closed."""
    return type(args) is dict and bool({'pair_ref', 'relation'} & set(args))


def _normalized_selection(args, catalog):
    if not uses_time_reference(args):
        return args
    if (type(args) is not dict or set(args) != {'pair_ref', 'relation'}
            or type(args['pair_ref']) is not str or type(args['relation']) is not str):
        _fail()
    units = list(_catalog_units(catalog).values())
    exact = next(unit for unit in units if unit['kind'] == 'time_exact')
    conclusion = next(unit for unit in units if unit['kind'] == 'time_conclusion')
    if (args['pair_ref'] != exact['time_pairs'][0].get('pair_id')
            or args['relation'] != conclusion['rule']
            or conclusion['required_exact_unit_id'] != exact['unit_id']):
        _fail()
    # Both references are explicit. This does not infer an omitted relation,
    # choose among sources, repair JSON, or create a free-form statement.
    direct = [{'fee_statement_unit_id': conclusion['unit_id']}]
    conditions = [[{'fee_statement_unit_id': exact['unit_id']}]]
    if len(exact['text']) > 600:
        # Preserve the existing legal long-original layout and length gates;
        # only placement changes, never the explicitly selected content.
        direct.append({'fee_statement_unit_id': exact['unit_id']})
        conditions = []
    return {'direct_answer': direct, 'conditions': conditions,
        'unknowns': [], 'claims': [], 'followups': [],
        'draft_ids': [], 'widget_ids': [], 'amount_claims': []}


def _selection_parts(args):
    if (type(args) is not dict or not set(_REQUIRED) <= set(args)
            or set(args) - set((*_REQUIRED, *_EMPTY_OPTIONAL))
            or any(type(args.get(k)) is not list or args[k] for k in ('unknowns', 'claims', 'followups'))
            or any(k in args and (type(args[k]) is not list or args[k]) for k in _EMPTY_OPTIONAL)):
        _fail()
    count = 0
    for field in ('direct_answer', 'conditions'):
        rows = [args[field]] if field == 'direct_answer' else args[field]
        if type(rows) is not list or field == 'conditions' and len(rows) > 6:
            _fail()
        for i, row in enumerate(rows):
            if type(row) is not list or not 1 <= len(row) <= 64:
                _fail()
            for j, part in enumerate(row):
                if (type(part) is not dict or set(part) != {'fee_statement_unit_id'}
                        or type(part['fee_statement_unit_id']) is not str):
                    _fail()
                count += 1
                if count > 64:
                    _fail()
                path = f'{field}[{j}]' if field == 'direct_answer' else f'{field}[{i}][{j}]'
                yield field, path, part


def expand_time_selection(args, catalog):
    """Expand precisely two explicit units; no text editing or auto-selection."""
    units = _catalog_units(catalog)
    normalized = _normalized_selection(args, catalog)
    parts = list(_selection_parts(normalized))
    selected = [part['fee_statement_unit_id'] for _, _, part in parts]
    if (len(selected) != 2 or len(set(selected)) != 2 or set(selected) != set(units)
            or not any(field == 'direct_answer' and units.get(part['fee_statement_unit_id'], {}).get('kind')
                == 'time_conclusion' for field, _, part in parts)
            or any(field == 'conditions' and units[part['fee_statement_unit_id']]['kind'] != 'time_exact'
                for field, _, part in parts)):
        _fail()
    expanded = deepcopy(normalized)
    expanded['claims'] = []
    for field in ('direct_answer', 'conditions'):
        rows = [expanded[field]] if field == 'direct_answer' else expanded[field]
        for row in rows:
            if sum(len(units[part['fee_statement_unit_id']]['text']) for part in row) > (1200 if field == 'direct_answer' else 600):
                _fail()
            for part in row:
                unit = units[part['fee_statement_unit_id']]
                part.clear(); part['text'] = unit['text']
                expanded['claims'].append({'text': unit['text'], 'evidence_ids': deepcopy(unit['evidence_ids'])})
    return expanded


def validate_time_authored(args, catalog, *, selection_args=None):
    """Reconstruct every authored part/claim exactly; no publication approval."""
    if selection_args is None:
        _fail()
    expanded = expand_time_selection(selection_args, catalog)
    if type(args) is not dict or expanded != args or _digest(expanded) != _digest(args):
        _fail()
    units = _catalog_units(catalog)
    selected = [{'path': path, 'unit_id': part['fee_statement_unit_id'],
        'kind': units[part['fee_statement_unit_id']]['kind'],
        'evidence_ids': deepcopy(units[part['fee_statement_unit_id']]['evidence_ids'])}
        for _, path, part in _selection_parts(_normalized_selection(selection_args, catalog))]
    exact = next(unit for unit in units.values() if unit['kind'] == 'time_exact')
    return {'contract': PROOF_CONTRACT, 'catalog_sha256': catalog['catalog_sha256'],
        'arguments_sha256': _digest(args), 'selection_sha256': _digest(selection_args),
        'selection_contract': REFERENCE_CONTRACT if uses_time_reference(selection_args) else SELECTION_CONTRACT,
        'explicit_unit_selection': {'arguments_sha256': _digest(selection_args),
            'expanded_arguments_sha256': _digest(expanded), 'selected': selected},
        'source_witnesses': deepcopy(exact['witnesses']), 'time_pairs': deepcopy(exact['time_pairs']),
        'coverage': 'all_selected_body_parts_and_all_claims_exactly_reconstructed',
        'host_rewrite': False,
        'guarantee': 'finite_source_bound_time_statements_only_not_full_financial_acceptance'}
