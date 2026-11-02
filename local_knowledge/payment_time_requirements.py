"""Bounded source-side payment-time recall requirements, never semantic proof.

Only already eligible canonical rows from one fixed release/as-of are accepted.
The caller keeps all identity, permission, publication and graph gates. This
module requests complete same-version counting definitions; final answer/time
validation remains independent. It reads no database and makes no model call.
"""
from __future__ import annotations

from datetime import date
import json
import re
import unicodedata


POLICY = 'payment-time-definition-recall-requirements/1'
_NUMBER = re.compile(r'(?<![a-z0-9])t\+(0|[1-9][0-9]{0,3})(?![0-9])', re.I)
_AFTER = re.compile(r'(?:(?:个)?(?:工作日|自然日)|日|天)?(?:\([^)]{0,40}\))?'
    r'(?:内|以内|之前|前|后)?(?:支付|划付|到账|结算)')
_BEFORE = re.compile(r'(?:支付|划付|到账|结算)(?:期限|时限|时间)?(?:为|是|在)?$')
_DEFINITION = re.compile(r't\+(?P<target>n|0|[1-9][0-9]{0,3})(?![0-9a-z])(?:日|天)[:：]?'
    r'(?:指|是|为)(?:自|从)?t日(?:起|后)(?:的)?第'
    r'(?P<ordinal>n|0|[1-9][0-9]{0,3})(?![0-9a-z])个'
    r'(?P<unit>工作日|自然日)(?P<tail>[^。；;！!\n]{0,60})', re.I)
_SCOPE = ('product_id', 'share_class_id', 'channel_id', 'document_id', 'version_id')


def _text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value).lower())


def _payment_numbers(text):
    numbers = set()
    for clause in re.split(r'[，,。；;！!\n]', _text(text)):
        for match in _NUMBER.finditer(clause):
            if _AFTER.match(clause[match.end():]) or _BEFORE.search(clause[:match.start()]):
                numbers.add(str(int(match[1])))
    return sorted(numbers, key=int)


def _definitions(text):
    values = []
    for match in _DEFINITION.finditer(_text(text)):
        target, ordinal = match['target'], match['ordinal']
        if (target == 'n' and ordinal != 'n') or (target != 'n' and target != ordinal):
            continue
        values.append({'number': None if target == 'n' else str(int(target)),
            'counting_unit': match['unit'],
            'excludes_t': True if re.search(r'不(?:包含|包括|含)t日', match['tail']) else None})
    return values


def _facets(row):
    value = row.get('facets', [])
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, (list, tuple)) or any(type(v) is not str for v in value):
        raise ValueError('invalid_eligible_facets')
    return set(value)


def payment_time_requirements(selected_ids, eligible_rows, *, release_id, as_of):
    """Describe all required limit/definition IDs for a proposed selected set.

    Generic T+n and exact same-number definitions qualify as recall candidates.
    Multiple qualifying definitions, even equal wording, are ambiguous. No
    other horizon, document, version, share or channel can supply a definition.
    The caller must plan both seeds together and reject an incomplete result.
    """
    if (not isinstance(selected_ids, (list, tuple)) or len(selected_ids) > 8
            or any(type(cid) is not str or not cid for cid in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)
            or type(release_id) is not str or not release_id
            or type(as_of) is not str or date.fromisoformat(as_of).isoformat() != as_of):
        raise ValueError('invalid_payment_time_requirement_input')
    rows = {}
    for raw in eligible_rows:
        row = dict(raw); cid = row.get('id')
        if (type(cid) is not str or not cid or cid in rows or len(rows) >= 5000
                or type(row.get('excerpt')) is not str):
            raise ValueError('invalid_eligible_payment_row')
        rows[cid] = row
    if not set(selected_ids) <= set(rows):
        raise ValueError('selected_payment_row_not_eligible')
    result = {'policy': POLICY, 'scope': 'eligible_source_recall_requirements_not_semantic_proof',
        'release_id': release_id, 'as_of': as_of, 'active': False,
        'pair_seed_ids': [], 'pairs': [], 'coverage_gaps': [], 'semantic_proof': False}
    definitions = {}
    for cid, row in rows.items():
        if {'liquidity', 'terms'} & _facets(row):
            definitions[cid] = _definitions(row['excerpt'])
    for cid in selected_ids:
        row = rows[cid]
        if not {'liquidity', 'terms'} & _facets(row):
            continue
        for number in _payment_numbers(row['excerpt']):
            result['active'] = True
            scope = tuple(row.get(k) for k in _SCOPE)
            if (any(type(row.get(k)) is not str or not row[k]
                    for k in ('product_id', 'document_id', 'version_id'))
                    or any(not (row.get(k) is None or type(row.get(k)) is str)
                        for k in ('share_class_id', 'channel_id'))):
                result['coverage_gaps'].append({'code': 'payment_time_pair_scope_missing',
                    'citation_id': cid, 'notation': 'T+' + number})
                continue
            matches = [(did, definition) for did, candidate in rows.items()
                if tuple(candidate.get(k) for k in _SCOPE) == scope
                for definition in definitions.get(did, []) if definition['number'] in (None, number)]
            if len(matches) != 1:
                result['coverage_gaps'].append({'code': 'payment_time_definition_missing'
                    if not matches else 'payment_time_definition_ambiguous',
                    'citation_id': cid, 'notation': 'T+' + number,
                    'candidate_definition_count': len(matches), 'role': 'time_definition'})
                continue
            did, definition = matches[0]
            result['pairs'].append({'limit_citation_id': cid, 'definition_citation_id': did,
                'notation': 'T+' + number, **{k: row.get(k) for k in _SCOPE},
                'release_id': release_id, 'as_of': as_of,
                'definition_number': definition['number'],
                'counting_unit': definition['counting_unit'], 'excludes_t': definition['excludes_t']})
            for required in (cid, did):
                if required not in result['pair_seed_ids']:
                    result['pair_seed_ids'].append(required)
    return result
