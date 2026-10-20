"""One-shot selectors over an unchanged model draft and one delivered public original.

No amount conversion, source inference, financial conclusion, or model request.
A line selection is a locator, not a financial proof. Original amount/source/GLM
checks still decide whether the rendered answer can be delivered.
"""
from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from .metric_claims import _MONEY, monetary_occurrences, MetricClaimError


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _row(answer, path):
    match = re.fullmatch(r'(direct_answer|conditions|unknowns)\[(\d+)\](?:\[(\d+)\])?\.text', path)
    if match is None:
        raise ValueError('invalid_recovery_path')
    field, first, second = match.groups()
    if (field == 'direct_answer') != (second is None):
        raise ValueError('invalid_recovery_path')
    row = answer[field] if second is None else answer[field][int(first)]
    return row, int(first) if second is None else int(second)


def build_choice_repair(answer, paths, originals):
    """Narrow admission: one complete delivered public fact; lexical money slots only."""
    if len(originals) != 1 or not paths or len(paths) != len(set(paths)):
        raise ValueError('choice_recovery_unavailable')
    fact_id, source = next(iter(originals.items()))
    if not isinstance(source, str) or not 0 < len(source) <= 1198:
        raise ValueError('choice_recovery_unavailable')
    lines = source.splitlines()
    if not 1 <= len(lines) <= 64:
        raise ValueError('choice_recovery_unavailable')
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    catalog = [{'line_ref': 'line_' + _digest([fact_id, source_sha, number, text])[:24],
        'line_number': number, 'text': text}
        for number, text in enumerate(lines, 1) if text.strip()]
    authored=''.join(part.get('text','') for part in answer.get('direct_answer',[]) if isinstance(part,dict))
    ratios=set(re.findall(r'(?<![0-9.])(?:0|[1-9][0-9]*)(?:\.[0-9]+)?%',unicodedata.normalize('NFKC',authored)))
    if len(ratios)!=1:
        raise ValueError('choice_recovery_unavailable')
    ratio=next(iter(ratios))
    catalog=[row for row in catalog if ratio in set(re.findall(r'(?<![0-9.])(?:0|[1-9][0-9]*)(?:\.[0-9]+)?%',unicodedata.normalize('NFKC',row['text'])))]
    # A unique verbatim ratio-bearing row binds the condition locator. Do not
    # choose among multiple rows, infer a different fee, or convert numbers.
    if len(catalog)!=1:
        raise ValueError('choice_recovery_unavailable')
    slots = []
    for path in paths:
        row, index = _row(answer, path)
        part = row[index]
        if not isinstance(part, dict) or set(part) != {'text'}:
            raise ValueError('choice_recovery_unavailable')
        text = part['text']; matches = list(_MONEY.finditer(text))
        if not matches:
            raise ValueError('choice_recovery_unavailable')
        masked = text
        for match in reversed(matches):
            # Reject broken numeric prefixes rather than repairing their value.
            previous=unicodedata.normalize('NFKC',text[match.start()-1]) if match.start() else ''
            if previous and previous in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,+-−':
                before=unicodedata.normalize('NFKC',text[:match.start()-1]).rstrip()
                if not(previous==',' and before and before[-1] not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,+-−'):
                    raise ValueError('choice_recovery_unavailable')
            literal = match.group(0)
            if len(monetary_occurrences(unicodedata.normalize('NFKC', literal))) != 1:
                raise ValueError('choice_recovery_unavailable')
            slots.append({'slot_id': 'slot_' + _digest([path, text, match.span()])[:24],
                'path': path, 'start': match.start(), 'end': match.end(), 'literal': literal})
            masked = masked[:match.start()] + '所引原文指定行的金额界限' + masked[match.end():]
        # Do not guess positions when the unchanged parser still finds money or
        # unsupported syntax after all exact lexical substitutions.
        if monetary_occurrences(unicodedata.normalize('NFKC', masked)):
            raise ValueError('choice_recovery_unavailable')
    if not 1 <= len(slots) <= 12:
        raise ValueError('choice_recovery_unavailable')
    return {'contract': 'navren.public-quote-choice-repair/1',
        'base_sha256': _digest(answer), 'fact_id': fact_id,
        'source_sha256': source_sha, 'paths': list(paths),
        'slots': slots, 'line_catalog': catalog}


def choice_repair_schema(context):
    slots=[s['slot_id'] for s in context['slots']]
    line={'type':'string','enum':[r['line_ref'] for r in context['line_catalog']]}
    return {'type':'object','additionalProperties':False,
        'required':['base_sha256','selections'],'properties':{
        'base_sha256':{'type':'string','enum':[context['base_sha256']]},
        'selections':{'type':'object','additionalProperties':False,
            'required':slots,'properties':{slot:deepcopy(line) for slot in slots}}}}


def apply_choice_repair(answer, context, originals, selection):
    # Rebuild against the current local original; a stale or modified context
    # cannot mint a different line reference or alter any retained draft byte.
    rebuilt = build_choice_repair(answer, context['paths'], originals)
    if rebuilt != context or set(selection) != {'base_sha256', 'selections'} or selection['base_sha256'] != context['base_sha256']:
        raise ValueError('choice_recovery_invalid')
    choices = selection['selections']
    slots = {s['slot_id']: s for s in context['slots']}
    lines = {r['line_ref']: r for r in context['line_catalog']}
    if not isinstance(choices,dict) or set(choices)!=set(slots):
        raise ValueError('choice_recovery_invalid')
    if any(not isinstance(ref,str) or ref not in lines for ref in choices.values()):
        raise ValueError('choice_recovery_invalid')
    chosen={slot:lines[ref] for slot,ref in choices.items()}
    result = deepcopy(answer)
    for path in context['paths']:
        row, index = _row(result, path)
        text = row[index]['text']
        for slot in sorted((s for s in context['slots'] if s['path'] == path), key=lambda s: s['start'], reverse=True):
            line = chosen[slot['slot_id']]
            pointer = '所引原文第' + str(line['line_number']) + '行所列金额界限'
            text = text[:slot['start']] + pointer + text[slot['end']:]
        if monetary_occurrences(unicodedata.normalize('NFKC', text)):
            raise ValueError('choice_recovery_invalid')
        row[index]['text'] = text
    # Bind the locator to the unchanged complete original; existing renderer
    # deduplicates the complete quotation, and existing proof checks it afresh.
    if not any(part.get('public_quote_fact_id') == context['fact_id'] for part in result['direct_answer']):
        result['direct_answer'].append({'public_quote_fact_id': context['fact_id']})
    return result
