"""One-shot selectors over an unchanged model draft and one delivered public original.

No amount conversion, source inference, financial conclusion, or model request.
A line selection is a locator, not a financial proof. Original amount/source/GLM
checks still decide whether the rendered answer can be delivered.
"""
from copy import deepcopy
from collections.abc import Mapping
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
    if not ratios:
        raise ValueError('choice_recovery_unavailable')
    catalog=[row for row in catalog if ratios & set(re.findall(r'(?<![0-9.])(?:0|[1-9][0-9]*)(?:\.[0-9]+)?%',unicodedata.normalize('NFKC',row['text'])))]
    # The model selects a verbatim row for each slot; the host never chooses
    # among rows, infers a different fee, or converts numbers.
    if not catalog:
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


# Finite fee vocabulary, not a general financial-language proof. An unresolved
# fee pronoun fails closed; it never borrows a subject across sentence boundaries.
_FEES = re.compile(r'管理费|托管费|销售服务费')
_FEE_ACTION = re.compile(r'计提|提取|扣除|扣取|扣收|扣费|支付|收取')
_FEE_FREQUENCY = re.compile(r'每(?:个)?(?:工作日|自然日|日|天|月|年|周|季)|逐日|按(?:工作日|日|月|年|周|季)(?!费率)|定期')
_FEE_PRONOUN = re.compile(r'(?:这些|上述|以上|该等|相关)费用')
_FEE_SUBJECT = r'(?:(?:'+_FEES.pattern+r')(?:、|和|及)?|'+_FEE_PRONOUN.pattern+r')*'
_FEE_UNKNOWN = re.compile(
    r'(?:本次|目前|现有(?:资料|原文))?(?:尚)?(?:未核实|未能确认|无法确认|无法判断|不能据此认定)'
    +_FEE_SUBJECT+r'是否(?:(?:从基金资产中)|(?:'+_FEE_FREQUENCY.pattern+r'))*'
    +r'(?:'+_FEE_ACTION.pattern+r')'+_FEE_SUBJECT)


# Two fee relationship families; NAV reflection and effect remain distinct.
# An annual rate or asset deduction is not proof
# of unconditional occurrence or any particular effect on net asset value.
_HOLDING_DURATION = r'(?:持有(?:期限|时间|天数)(?:长短|多久|多长)?|持有多久|持有多长时间)'
_FEE_RELATIONS = (
    ('holding_duration_independence', re.compile(r'(?:与|和)'+_HOLDING_DURATION
        +r'(?:(?:并非|并不|不是|是否|可能|或许|也许|不|并|完全|毫)\s*){0,2}无关')),
    # Continuous holding-period burden is distinct from duration-independent occurrence.
    ('holding_period_continuous_burden', re.compile(r'(?:投资者)?(?:在)?持有(?:期间|期内)'
        +r'(?:是否|不|未)?(?:持续|一直|始终)(?:不|未)?承担')),
    ('holding_duration_unconditional', re.compile(r'(?:无论|不论|不管)'+_HOLDING_DURATION
        +_FEE_SUBJECT+r'(?:都|均|一律)(?:不|未)?(?:会|需|须|需要)?(?:不|未)?(?:产生|发生|承担|收取)')),
    ('nav_reflection', re.compile(r'(?:反映|体现|计入)(?:在|于)?(?:基金(?:份额)?|份额|单位)?(?:资产)?净值(?:中|内)?')),
    ('nav_effect', re.compile(r'影响(?:基金(?:份额)?|份额|单位)?(?:资产)?净值')))
_FEE_RELATION_UNKNOWN = re.compile(
    r'(?:本次|目前|现有(?:资料|原文))?(?:尚)?(?:未核实|未能确认|无法确认|无法判断|不能据此认定)'
    +_FEE_SUBJECT+r'是否(?:会)?(?:'+'|'.join(pattern.pattern for _,pattern in _FEE_RELATIONS)+r')')


def _fee_relations(sentence, fee_context, *, question=False):
    for kind, pattern in _FEE_RELATIONS:
        for match in pattern.finditer(sentence):
            comma = max(sentence.rfind(',',0,match.start()),sentence.rfind('，',0,match.start()))
            tail = re.search(r'[,，]',sentence[match.end():])
            end = match.end()+tail.start() if tail else len(sentence)
            clause = sentence[comma+1:end]
            if _FEE_RELATION_UNKNOWN.fullmatch(re.sub(r'\s+','',clause)):
                continue
            if kind == 'holding_duration_independence':
                local = sentence[comma+1:match.start()].strip()
                if local.startswith('“'):local=local[1:].lstrip()
                fee_group = r'(?:'+_FEES.pattern+r')(?:\s*(?:、|和|及|与)\s*(?:'+_FEES.pattern+r'))*'
                # This predicate is about fee occurrence. A pure rate/amount
                # subject cannot prove it and must not be relabelled as it.
                if re.fullmatch(r'(?:'+fee_group+r'|'+_FEE_PRONOUN.pattern
                        +r')(?:的)?(?:年费率|费率|金额|收取标准)',local):continue
                subject = re.fullmatch(r'(?P<fees>'+fee_group+r'|'+_FEE_PRONOUN.pattern
                    +r')(?:的(?:产生|发生|收取|承担))?',local)
                fees = tuple(dict.fromkeys(_FEES.findall(local)))
                reference = bool(fees or _FEE_PRONOUN.search(local))
                groups = [tuple(dict.fromkeys(_FEES.findall(part)))
                    for part in re.split(r'[,，]',sentence[:comma+1]) if _FEES.search(part)]
                if (not reference and len(groups)==1
                        and re.fullmatch(r'(?:并|且|还|也|均|会|将|同时|因此|从而|都)*',local)):
                    # An omitted subject cannot evade detection. Its antecedent
                    # may describe a rate rather than occurrence: fail closed.
                    reference=True;fees=groups[0]
                if not reference or not fee_context:continue
                if subject and not fees:
                    if len(groups)==1:fees=groups[0]
                # Only the matched intrinsic '无关' loses its negative marker.
                # Rate/amount subjects, competing subjects and external scope
                # cannot establish duration-independent occurrence of fees.
                polarity = sentence[:match.start()]+match.group()[:-2]+sentence[match.end():]
                affirmative = bool(subject and fees) and not question and not bool(re.search(
                    r'不|未|无|非|是否|可能|或许|假设|假如|如果|若|也许|待核实|待确认|存疑|未知|不明|吗|么|呢|[?？]',polarity))
                conditional=bool(re.search(r'(?:仅|只)?在[^。！？!?]*(?:情况|情形|条件)下|'
                    r'(?:仅|只)在[^。！？!?]*时|当[^。！？!?]*时|'
                    r'仅限|只限|仅适用|限定|除外|例外',sentence))
                affirmative=affirmative and not conditional
                yield {'fees':list(fees),'action':kind,'frequency':[],
                    'sentence':sentence.strip(),'affirmative':affirmative}
                continue
            prefix = sentence[:match.start()]
            fees = tuple(dict.fromkeys(_FEES.findall(sentence[comma+1:match.start()]+match.group())))
            local_prefix = sentence[comma+1:match.start()]
            explicit_reference = bool(fees or _FEE_PRONOUN.search(local_prefix))
            if not fees:
                groups = [tuple(dict.fromkeys(_FEES.findall(part))) for part in re.split(r'[,，]',prefix) if _FEES.search(part)]
                elliptical = bool(groups) and bool(re.fullmatch(r'(?:并|且|还|也|均|会|将|最终|同时|因此|从而|直接|都)*',local_prefix.strip()))
                explicit_reference = explicit_reference or elliptical
                if explicit_reference and len(groups)==1:fees=groups[0]
            # A risk/market-price subject is not a fee subject. Global delivery
            # context cannot turn an ordinary NAV sentence into a fee claim.
            if not fee_context or not explicit_reference:continue
            # Universal markers are intrinsic positive scope, not negations.
            # Any additional uncertainty/negative/hypothetical scope cannot
            # support an affirmative fee relation, including across commas.
            # A new explicit fee subject owns its local relation; an elliptical
            # reference retains the antecedent's uncertainty. Never turn a
            # different clause's deduction/denial into support for this action.
            scope = sentence[comma+1:match.end()] if _FEES.search(local_prefix+match.group()) else sentence[:match.end()]
            polarity = re.sub(r'无论|不论|不管','',scope)
            affirmative = not bool(re.search(r'不|未|无|非|是否|可能|或许|假设|假如|如果|若|也许',polarity)
                or re.search(r'假设|假如|如果|若',sentence[:match.start()]))
            if kind == 'holding_period_continuous_burden':
                # Only this new relation retains trailing uncertainty/question scope.
                # Never promote an annual rate, a question, or an uncertain tail to support.
                affirmative = affirmative and not question and not bool(re.search(
                    r'不|未|无|非|是否|可能|或许|假设|假如|如果|若|也许|待核实|待确认|存疑|吗|么|呢|[?？]',
                    sentence[match.end():]))
            yield {'fees':list(fees),'action':kind,'frequency':[],
                'sentence':sentence.strip(),'affirmative':affirmative}


def _fee_predicates(text, *, fee_context=False):
    normalized = unicodedata.normalize('NFKC', text)
    # Context only enables reporting a missing subject; never inherit fee names.
    fee_context = fee_context or bool(_FEES.search(normalized) or _FEE_PRONOUN.search(normalized))
    for sentence in re.split(r'[。！？；;\n]', normalized):
        actions = list(_FEE_ACTION.finditer(sentence))
        for index, action in enumerate(actions):
            left = actions[index-1].end() if index else 0
            right = actions[index+1].start() if index+1 < len(actions) else len(sentence)
            comma = max(sentence.rfind(',', 0, action.start()), sentence.rfind('，', 0, action.start()))
            end = re.search(r'[,，]', sentence[action.end():])
            end = action.end()+end.start() if end else len(sentence)
            clause = sentence[comma+1:end]
            # Exemption is the complete fee-action question, not a prefix that
            # could also describe an unrelated unknown channel or other fact.
            prefix = sentence[comma+1:action.start()]
            if _FEE_UNKNOWN.fullmatch(re.sub(r'\s+', '', clause)):
                continue
            fees = tuple(dict.fromkeys(_FEES.findall(prefix)))
            if not fees and _FEE_PRONOUN.search(prefix):
                antecedents = [tuple(dict.fromkeys(_FEES.findall(part)))
                    for part in re.split(r'[,，]', sentence[left:comma+1]) if _FEES.search(part)]
                if len(antecedents) == 1:fees = antecedents[0]
            if not fees and len(actions) == 1 and not _FEE_PRONOUN.search(prefix):
                fees = tuple(dict.fromkeys(_FEES.findall(sentence[action.end():right])))
            frequency = tuple(dict.fromkeys(_FEE_FREQUENCY.findall(clause)))
            verb = action.group()
            kind = 'accrual' if verb in ('计提','提取') else ('deduction' if verb in ('扣除','扣取','扣收','扣费') else 'payment')
            if (kind == 'accrual' or frequency) and fee_context:
                yield {'fees': list(fees), 'action': kind,
                    'frequency': list(frequency), 'sentence': sentence.strip(),
                    'affirmative': not bool(re.search(r'不|未|无|非|是否|可能|有时', prefix))}
        # re.split removes Chinese question punctuation; preserve it only for
        # the new relation. Existing relation kinds ignore this extra argument.
        question = bool(re.search(re.escape(sentence)+r'\s*[?？]', normalized)
            or re.search(r'[?？]\s*$', sentence))
        yield from _fee_relations(sentence, fee_context, question=question)


def public_fee_source_rejections(args, tools):
    """Bind every body/claim predicate to this request's delivered fee original.

    Different actions/frequencies, fee subjects, shares or canonical versions
    cannot be combined. Annual percentage rates never establish a frequency.
    Freshness/publication checks still run on the ordinary BankTools path.
    """
    evidence = tools.evidence
    pinned = getattr(tools.knowledge, '_pinned_status', None)
    delivered = getattr(tools.knowledge, '_delivered', {})
    groups = {}
    if isinstance(pinned,dict) and pinned.get('ready') is True:
        for eid, entry in evidence.items():
            data = entry.get('data', {})
            if (entry.get('id') != eid or entry.get('context_hash') != tools.start_context
                    or entry.get('tool') != 'knowledge_search' or entry.get('source') != 'public_knowledge'
                    or data.get('ready') is not True or data.get('scope') != 'public'
                    or data.get('provider_id') != pinned.get('provider_id')
                    or data.get('public_release_id') != pinned.get('public_release_id')):
                continue
            for hit in data.get('hits', []):
                receipt = delivered.get((hit.get('citation_id'), data.get('as_of')))
                if (not receipt or receipt[0] != hit or 'fees' not in hit.get('facets', [])
                        or hit.get('release_id') != pinned.get('public_release_id')
                        or hit.get('visibility') != 'public' or hit.get('outbound_allowed') is not True
                        or hit.get('content_status') != 'full_text' or hit.get('claims_are_account_facts') is not False
                        or not isinstance(hit.get('excerpt'),str)):
                    continue
                key = tuple(hit.get(k) for k in ('product_id','share_class_id','channel_id','document_id','document_version_id','release_id'))
                if not all(key[i] for i in (0,3,4,5)):
                    continue
                groups.setdefault(eid, {}).setdefault(key, []).extend(_fee_predicates(hit['excerpt']))
    claims = args.get('claims', [])
    body_ids = set(eid for claim in claims for eid in claim.get('evidence_ids', []))
    fields = [('text', args.get('text',''), body_ids)] + [
        ('claims['+str(i)+'].text', claim.get('text',''), set(claim.get('evidence_ids', [])))
        for i, claim in enumerate(claims)]
    rejected = []
    for location, text, ids in fields:
        candidates = {}
        for eid in ids:
            for key, predicates in groups.get(eid, {}).items():
                candidates.setdefault(key, []).extend(predicates)
        for predicate in _fee_predicates(text, fee_context=bool(candidates)):
            supported = False
            if predicate['affirmative'] and predicate['fees'] and len(candidates) == 1:
                originals = next(iter(candidates.values()))
                supported = all(any(original['affirmative'] and fee in original['fees'] and original['action'] == predicate['action']
                    and set(predicate['frequency']) <= set(original['frequency']) for original in originals)
                    for fee in predicate['fees'])
            if not supported:
                rejected.append(dict(predicate, location=location))
    return rejected


def _fee_texts(answer):
    for field in ('direct_answer','conditions','unknowns'):
        rows = [answer[field]] if field == 'direct_answer' else answer[field]
        for ri, row in enumerate(rows):
            for pi, part in enumerate(row):
                if set(part) == {'text'}:
                    path = field+'['+str(pi)+'].text' if field == 'direct_answer' else field+'['+str(ri)+']['+str(pi)+'].text'
                    yield path, part, 1200 if field == 'direct_answer' else 600
    for index, claim in enumerate(answer.get('claims', [])):
        yield 'claims['+str(index)+'].text', claim, 1200


_DEADLINE_ENDPOINT = re.compile(r'(?<![A-Za-z0-9])T\+([0-9]{1,4})\s*(?:日|天)\s*'
    r'(?P<endpoint>\(\s*(?:含|包含|包括)\s*该日\s*\))\s*(?:以内|内)\s*'
    r'(?P<action>支付|划付|到账|结算)', re.I)
_ENDPOINT_IDENTITY = ('product_id','share_class_id','channel_id','document_id','document_version_id','release_id')


def has_deadline_endpoint(answer):
    texts=[answer.get('text','')]+[c.get('text','')for c in answer.get('claims',[])]
    if 'direct_answer'in answer:texts.extend(part['text']for _,part,_ in _fee_texts(answer))
    return any(isinstance(text,str)and _DEADLINE_ENDPOINT.search(unicodedata.normalize('NFKC',text))for text in texts)


def delivered_time_endpoint_sources(bindings, *, evidence, delivered, pinned, context_hash):
    """Private witness from this request's already-delivered originals only."""
    if (not isinstance(bindings,(list,tuple))or not 1<=len(bindings)<=12
            or not isinstance(pinned,dict)or pinned.get('ready')is not True):
        raise ValueError('time_endpoint_source_unavailable')
    records=[]
    for binding in bindings:
        record={'contract':'navren.delivered-time-endpoint/1','binding_sha256':_digest(binding),
            'provider_id':pinned.get('provider_id')};days=set()
        for role in ('limit','definition'):
            eid=binding.get(role+'_evidence_id');entry=evidence.get(eid,{});data=entry.get('data',{})
            if (entry.get('id')!=eid or entry.get('context_hash')!=context_hash
                    or entry.get('tool')!='knowledge_search'or entry.get('source')!='public_knowledge'
                    or data.get('ready')is not True or data.get('scope')!='public'
                    or data.get('provider_id')!=pinned.get('provider_id')
                    or data.get('public_release_id')!=pinned.get('public_release_id')):
                raise ValueError('time_endpoint_source_unavailable')
            hits=[h for h in data.get('hits',[])if h.get('citation_id')==binding.get(role+'_citation_id')]
            if len(hits)!=1:raise ValueError('time_endpoint_source_unavailable')
            hit=hits[0];receipt=delivered.get((hit.get('citation_id'),data.get('as_of')))
            if (not receipt or receipt[0]!=hit or any(hit.get(k)!=binding.get(k)for k in _ENDPOINT_IDENTITY)
                    or hit.get('release_id')!=pinned.get('public_release_id')
                    or hit.get('visibility')!='public'or hit.get('outbound_allowed')is not True
                    or hit.get('content_status')!='full_text'or hit.get('claims_are_account_facts')is not False
                    or hit.get('withdrawn',False)is not False
                    or not {'liquidity','terms'}&set(hit.get('facets',[]))
                    or not isinstance(hit.get('excerpt'),str)or not 0<len(hit['excerpt'])<=1198):
                raise ValueError('time_endpoint_source_unavailable')
            record[role+'_hit']=deepcopy(hit)
            record[role+'_sha256']=hashlib.sha256(hit['excerpt'].encode()).hexdigest();days.add(data.get('as_of'))
        if len(days)!=1 or not all(isinstance(day,str)and re.fullmatch(r'\d{4}-\d{2}-\d{2}',day)for day in days):
            raise ValueError('time_endpoint_source_unavailable')
        record['as_of']=next(iter(days));records.append(record)
    _validated_endpoint_sources(bindings,records)
    return records


def _validated_endpoint_sources(bindings, records):
    keys={'contract','binding_sha256','provider_id','as_of','limit_hit','limit_sha256','definition_hit','definition_sha256'}
    if not isinstance(records,(list,tuple))or len(records)!=len(bindings):raise ValueError('time_endpoint_source_invalid')
    for binding,record in zip(bindings,records):
        if (type(record)is not dict or set(record)!=keys or record['contract']!='navren.delivered-time-endpoint/1'
                or record['binding_sha256']!=_digest(binding)or not isinstance(record['provider_id'],str)
                or not record['provider_id']or not isinstance(record['as_of'],str)):
            raise ValueError('time_endpoint_source_invalid')
        for role in ('limit','definition'):
            hit=record[role+'_hit']
            if (type(hit)is not dict or hit.get('citation_id')!=binding.get(role+'_citation_id')
                    or any(hit.get(k)!=binding.get(k)for k in _ENDPOINT_IDENTITY)
                    or not isinstance(hit.get('excerpt'),str)
                    or hashlib.sha256(hit['excerpt'].encode()).hexdigest()!=record[role+'_sha256']):
                raise ValueError('time_endpoint_source_invalid')
        compact=lambda v:re.sub(r'\s+','',unicodedata.normalize('NFKC',v).lower())
        if (not isinstance(binding.get('definition'),str)or not binding['definition']
                or compact(binding['definition'])not in compact(record['definition_hit']['excerpt'])
                or not any(str(int(m[1]))==binding.get('notation','')[2:]for m in
                    _DEADLINE_ENDPOINT.finditer(unicodedata.normalize('NFKC',record['limit_hit']['excerpt'])))):
            raise ValueError('time_endpoint_source_invalid')
    return records


def _mask_bound_deadline_endpoints(text, bindings, records):
    if not records:return text
    _validated_endpoint_sources(bindings,records);allowed=set()
    for binding,record in zip(bindings,records):
        for m in _DEADLINE_ENDPOINT.finditer(unicodedata.normalize('NFKC',record['limit_hit']['excerpt'])):
            if str(int(m[1]))==binding['notation'][2:]:allowed.add((binding['notation'],m['action']))
    def mask(m):
        if m[1]!=str(int(m[1]))or ('T+'+m[1],m['action'])not in allowed:
            raise ValueError('time_endpoint_source_invalid')
        start,end=m.span('endpoint');offset=start-m.start()
        # Analysis view only. Protected sentence hashes and displayed bytes stay intact.
        return m[0][:offset]+' '*(end-start)+m[0][offset+end-start:]
    return _DEADLINE_ENDPOINT.sub(mask,text)


def _continuous_fee_safe_edits(answer, rejected, paths):
    if not any(item.get('action')=='holding_period_continuous_burden'for item in rejected):return None
    if any(item.get('action')!='holding_period_continuous_burden'or item.get('affirmative')is not True for item in rejected):
        raise ValueError('fee_recovery_unavailable')
    pattern=next(p for kind,p in _FEE_RELATIONS if kind=='holding_period_continuous_burden')
    removable=re.compile(r'[,，]\s*(?:'+pattern.pattern+r')\s*(?=[。！？!?；;]|$)')
    fields={path:part for path,part,_ in _fee_texts(answer)};edits={}
    for path in paths:
        text=fields[path]['text'];matches=list(removable.finditer(text))
        if not matches or len(matches)!=len(list(pattern.finditer(text))):raise ValueError('fee_recovery_unavailable')
        target=removable.sub('',text)
        if not target.strip()or target==text:raise ValueError('fee_recovery_unavailable')
        edits[path]={'original_sha256':hashlib.sha256(text.encode()).hexdigest(),'text':target}
    return edits


def _joint_time_model_sentences(answer, bindings, rejected=(), *, time_endpoint_sources=()):
    """Finite absence-only admission over model text, before any host excerpt."""
    from .knowledge_semantics import _TIME_NUMBER, _concrete_time_conclusions
    numbers={binding['notation'][2:] for binding in bindings};records=[]
    for path,part,_ in _fee_texts(answer):
        text=unicodedata.normalize('NFKC',part['text'])
        analysis=_mask_bound_deadline_endpoints(text,bindings,time_endpoint_sources)
        # Every other authored basis remains outside automatic completion.
        # Do not let a later correct quote override an existing/ambiguous basis.
        if re.search(r'工作日|自然日|交易日|营业日|开放日|日历日|计息日|结算日|计日|'
                r't\s*日|(?:申请|受理|生效|确认)(?:当日|当天)|'
                r'(?:上述|以上|该|这一)(?:期限|时限|时间)[^。！？!?\n]{0,20}(?:计|算|口径|单位)|'
                r'(?:含|包含|包括|计入|算入|算上)\s*(?:当日|当天|首日|该日)',analysis,re.I):
            raise ValueError('joint_time_recovery_unavailable')
        for clause in re.split(r'[，,；;\n。！？!?]',analysis):
            for match in _TIME_NUMBER.finditer(clause):
                number=str(int(match[1]));head=re.sub(r'\s+','',clause[:match.start()]);tail=clause[match.end():]
                after=re.match(r'\s*(?:日|天)?\s*(?:以内|内|之前|前|后)?\s*(?:支付|划付|到账|结算)',tail)
                before=(re.search(r'(?:支付|划付|到账|结算)(?:期限|时限|时间)?(?:为|是|在)?$',head)
                    and re.fullmatch(r'\s*(?:日|天)?\s*(?:以内|内|之前|前|后)?\s*',tail))
                if (number not in numbers or number not in _concrete_time_conclusions(clause)
                        or not (after or before)):
                    raise ValueError('joint_time_recovery_unavailable')
        for raw in re.findall(r'[^。！？!?]+(?:[。！？!?]|$)',text):
            sentence=raw.strip();mentioned={str(int(match[1]))for match in _TIME_NUMBER.finditer(sentence)}
            if not mentioned:continue
            predicates=list(_fee_predicates(sentence,fee_context=True))
            if any(all(predicate[key]==item[key]for key in ('fees','action','frequency','sentence'))
                    for predicate in predicates for item in rejected):
                raise ValueError('joint_time_recovery_unavailable')
            for number in sorted(mentioned,key=int):
                row={'path':path,'notation':'T+'+number,'text_nfkc':sentence,
                    'text_nfkc_sha256':hashlib.sha256(sentence.encode('utf-8')).hexdigest(),
                    'min_occurrences':text.count(sentence)}
                if row not in records:records.append(row)
    return records


def _fee_source_time_sentences(answer):
    """Freeze existing concrete time sentences before a fee-only replacement."""
    from .knowledge_semantics import _concrete_time_conclusions
    records=[]
    for path,part,_ in _fee_texts(answer):
        text=unicodedata.normalize('NFKC',part['text'])
        for raw in re.findall(r'[^。！？!?]+(?:[。！？!?]|$)',text):
            sentence=raw.strip();numbers=sorted(_concrete_time_conclusions(sentence),key=int)
            if not numbers:continue
            row={'path':path,'text_nfkc':sentence,
                'text_nfkc_sha256':hashlib.sha256(sentence.encode()).hexdigest(),
                'min_occurrences':text.count(sentence),'notations':['T+'+n for n in numbers]}
            if row not in records:records.append(row)
    return records


def build_fee_repair(answer, rejected, *, time_basis_rejections=(), time_endpoint_sources=()):
    paths = []
    for path, part, maximum in _fee_texts(answer):
        predicates = list(_fee_predicates(part['text'], fee_context=True))
        for item in rejected:
            if ((item['location'] == 'text' and not path.startswith('claims['))
                    or item['location'] == path):
                if any(all(predicate[k] == item[k] for k in ('fees','action','frequency','sentence'))
                        for predicate in predicates):
                    paths.append(path);break
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('fee_recovery_unavailable')
    if not isinstance(time_basis_rejections, (list, tuple)):
        raise ValueError('fee_recovery_unavailable')
    fee_paths = list(paths); time_paths = []
    if time_basis_rejections:
        from .knowledge_semantics import _concrete_time_conclusions
        binding_keys = {'notation','definition','counting_unit','excludes_t','answer_status',
            'product_id','share_class_id','channel_id','document_id','document_version_id','release_id',
            'limit_evidence_id','limit_citation_id','definition_evidence_id','definition_citation_id',
            'location','scope'}
        for binding in time_basis_rejections:
            if (not isinstance(binding, Mapping) or not binding_keys <= set(binding)
                    or binding['answer_status'] != 'missing' or binding['location'] != 'text'
                    or binding['scope'] != 'source_time_condition_not_execution_or_current_validity'
                    or binding['counting_unit'] not in ('工作日','自然日')
                    or not (binding['excludes_t'] is None or binding['excludes_t'] is True)
                    or not isinstance(binding['definition'], str) or not binding['definition']):
                raise ValueError('fee_recovery_unavailable')
            match = re.fullmatch(r'T\+([0-9]+)', binding['notation'] or '')
            if match is None or match[1] != str(int(match[1])):
                raise ValueError('fee_recovery_unavailable')
            matched = [path for path, part, _ in _fee_texts(answer)
                if not path.startswith('claims[') and match[1] in _concrete_time_conclusions(part['text'])]
            if not matched:raise ValueError('fee_recovery_unavailable')
            time_paths.extend(path for path in matched if path not in time_paths)
    if len(paths) > 12:raise ValueError('fee_recovery_unavailable')
    context = {'contract':'navren.public-fee-source-repair/1',
        'base_sha256':_digest(answer), 'paths':paths}
    safe_edits=_continuous_fee_safe_edits(answer,rejected,fee_paths)
    if safe_edits is not None:context['fee_safe_edits']=safe_edits
    if time_basis_rejections:
        if time_endpoint_sources:context['time_endpoint_sources']=deepcopy(time_endpoint_sources)
        records=_joint_time_model_sentences(answer,time_basis_rejections,rejected,
            time_endpoint_sources=time_endpoint_sources)
        protected=[row for row in records if not row['path'].startswith('claims[')]
        if set(time_paths)!={row['path']for row in protected}:
            raise ValueError('fee_recovery_unavailable')
        context.update(fee_paths=fee_paths,time_paths=time_paths,
            time_basis_rejections=deepcopy(list(time_basis_rejections)),
            protected_time_sentences=protected,model_time_sentences=records)
    else:
        context['fee_source_time_sentences']=_fee_source_time_sentences(answer)
    return context


def fee_repair_schema(context):
    schema={'type':'object','additionalProperties':False,
        'required':['base_sha256','replacements'],'properties':{
        'base_sha256':{'type':'string','enum':[context['base_sha256']]},
        'replacements':{'type':'array','minItems':len(context['paths']),'maxItems':len(context['paths']),
            'items':{'type':'object','additionalProperties':False,'required':['path','text'],
                'properties':{'path':{'type':'string','enum':context['paths']},
                    'text':{'type':'string','minLength':1,'maxLength':1200}}}}}}
    if context.get('fee_safe_edits'):
        base=schema['properties']['replacements']['items'];variants=[]
        for path,edit in context['fee_safe_edits'].items():
            item=deepcopy(base)
            item['properties']['path']['enum']=[path]
            item['properties']['text']['enum']=[edit['text']]
            variants.append(item)
        schema['properties']['replacements']['items']={'oneOf':variants}
    return schema


def apply_fee_repair(answer, context, patch):
    if (set(patch) != {'base_sha256','replacements'} or _digest(answer) != context['base_sha256']
            or patch['base_sha256'] != context['base_sha256'] or not isinstance(patch['replacements'],list)
            or len(patch['replacements']) != len(context['paths'])):
        raise ValueError('fee_recovery_invalid')
    result = deepcopy(answer);parts = {path:(part,maximum) for path,part,maximum in _fee_texts(result)}
    seen = set()
    for item in patch['replacements']:
        if not isinstance(item,dict) or set(item) != {'path','text'}:
            raise ValueError('fee_recovery_invalid')
        path = item['path'];text = item['text']
        if (not isinstance(path,str) or path not in context['paths'] or path in seen
                or not isinstance(text,str) or not text.strip() or len(text)>parts[path][1]
                or text == parts[path][0]['text']
                or (path in context.get('append_only_paths', ())
                    and not text.startswith(parts[path][0]['text']))):
            raise ValueError('fee_recovery_invalid')
        if context.get('fee_safe_edits'):
            edit=context['fee_safe_edits'].get(path)
            if (not edit or hashlib.sha256(parts[path][0]['text'].encode()).hexdigest()!=edit['original_sha256']
                    or text!=edit['text']):raise ValueError('fee_recovery_invalid')
        parts[path][0]['text'] = text;seen.add(path)
    if seen != set(context['paths']):raise ValueError('fee_recovery_invalid')
    if context.get('time_basis_rejections'):
        current=_joint_time_model_sentences(result,context['time_basis_rejections'],
            time_endpoint_sources=context.get('time_endpoint_sources',()))
        key=lambda row:(row['path'],row['notation'],row['text_nfkc_sha256'])
        original={key(row):row for row in context['model_time_sentences']}
        restored={key(row):row for row in current}
        if (set(original)!=set(restored) or any(restored[k]['min_occurrences']<row['min_occurrences']
                or restored[k]['text_nfkc']!=row['text_nfkc'] for k,row in original.items())):
            raise ValueError('fee_recovery_invalid')
    elif context.get('fee_source_time_sentences')!=_fee_source_time_sentences(result):
        raise ValueError('fee_recovery_invalid')
    return result
