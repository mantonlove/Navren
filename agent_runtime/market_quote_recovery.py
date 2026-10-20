"""One bounded market-only repair: rephrase blocked paragraphs, select exact source money slots.

Source locators are not semantic approval or personal financial authority.
The original amount/evidence/fresh/GLM/final-publication gates still apply.
"""
from copy import deepcopy
import unicodedata
from .metric_claims import _MONEY, monetary_occurrences
from .public_quote_recovery import _digest, _row


def build_market_repair(answer, paths, originals):
    if not originals or len(originals)>48 or not paths or len(set(paths))!=len(paths):
        raise ValueError('market_repair_unavailable')
    amounts={}
    for fid,text in originals.items():
        if not isinstance(text,str) or not 0<len(text)<=1198:raise ValueError('invalid_original')
        amounts[fid]={(r['value_minor'],r['currency']) for r in monetary_occurrences(unicodedata.normalize('NFKC',text))}
    slots=[]
    for path in paths:
        row,index=_row(answer,path);part=row[index]
        if not isinstance(part,dict) or set(part)!={'text'}:raise ValueError('invalid_part')
        text=part['text'];matches=list(_MONEY.finditer(text));masked=text
        if not matches:raise ValueError('unlocatable_money')
        for match in reversed(matches):
            # Keep precisely the same unambiguous syntax requirement as the
            # full monetary parser, including fullwidth normalization.
            literal=unicodedata.normalize('NFKC',match.group(0))
            parsed=monetary_occurrences(literal)
            if len(parsed)!=1 or parsed[0]['start']!=0 or parsed[0]['end']!=len(literal):
                raise ValueError('ambiguous_money')
            before=unicodedata.normalize('NFKC',text[match.start()-1]) if match.start() else ''
            if before and (before.isascii() and (before.isalnum() or before in '.+-−')):
                raise ValueError('broken_money_prefix')
            value=(parsed[0]['value_minor'],parsed[0]['currency'])
            choices=[fid for fid,values in amounts.items() if value in values]
            if not choices:raise ValueError('no_exact_source_amount')
            slots.append({'slot_id':'market_slot_'+_digest([path,text,match.span()])[:24],
                'path':path,'start':match.start(),'end':match.end(),'literal':match.group(0),
                'source_choices':choices})
            masked=masked[:match.start()]+'所引报道中的该项金额'+masked[match.end():]
        if monetary_occurrences(unicodedata.normalize('NFKC',masked)):
            raise ValueError('unlocatable_money')
    if not 1<=len(slots)<=12:raise ValueError('slot_limit')
    return {'contract':'navren.market-source-slot-repair/2','base_sha256':_digest(answer),
        'originals_sha256':_digest(originals),'paths':list(paths),'slots':slots}


def market_repair_schema(context):
    return {'type':'object','additionalProperties':False,'required':['base_sha256','selections','paragraphs'],
        'properties':{'base_sha256':{'type':'string','enum':[context['base_sha256']]},
            'paragraphs':{'type':'object','additionalProperties':False,
                'required':list(context['paths']),
                'description':'只重写这几个金额受阻段的非货币解释。用自然完整的句子保留原有因果、资格和未知边界；金额由所选完整原文展开。不能复制货币数值、用占位句指代金额或添加新事实。',
                'properties':{path:{'type':'string','minLength':1,
                    'maxLength':1200 if path.startswith('direct_answer[') else 600}
                    for path in context['paths']}},
            'selections':{'type':'object','additionalProperties':False,
                'required':[s['slot_id'] for s in context['slots']],
                'properties':{s['slot_id']:{'type':'string','enum':s['source_choices']}
                    for s in context['slots']}}}}


def apply_market_repair(answer, context, originals, selection):
    if (build_market_repair(answer,context['paths'],originals)!=context
            or not isinstance(selection,dict) or set(selection)!={'base_sha256','selections','paragraphs'}
            or selection['base_sha256']!=context['base_sha256']):raise ValueError('stale_repair')
    choices=selection['selections'];slots={s['slot_id']:s for s in context['slots']}
    if (not isinstance(choices,dict) or set(choices)!=set(slots)
            or any(not isinstance(fid,str) or fid not in slots[sid]['source_choices']
                   for sid,fid in choices.items())):raise ValueError('invalid_selection')
    paragraphs=selection['paragraphs']
    if not isinstance(paragraphs,dict) or set(paragraphs)!=set(context['paths']):
        raise ValueError('invalid_paragraphs')
    for path,text in paragraphs.items():
        if (not isinstance(text,str) or not text.strip()
                or len(text)>(1200 if path.startswith('direct_answer[') else 600)
                or monetary_occurrences(unicodedata.normalize('NFKC',text))
                or any(word in text for word in ('所引报道中的该项金额','金额占位','占位符'))):
            raise ValueError('invalid_paragraphs')
    result=deepcopy(answer)
    # Only the explicitly blocked text parts may be rephrased. Every selected
    # source must still match the original draft amount and currency exactly;
    # all other parts, claims, qualifications and unknowns stay byte-identical.
    for path in reversed(context['paths']):
        row,index=_row(result,path)
        selected=[choices[slot['slot_id']] for slot in
            sorted((x for x in slots.values() if x['path']==path),key=lambda x:x['start'])]
        row[index:index+1]=[{'text':paragraphs[path]},
            *[{'public_quote_fact_id':fid} for fid in dict.fromkeys(selected)]]
    return result
