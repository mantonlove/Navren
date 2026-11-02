"""Non-authorizing public-name hints for an unchanged local ASR transcript.

Hints only pause auto-send so the person can edit the ordinary input draft.
They never select a product/share, repair speech, or approve a financial action.
"""
from copy import deepcopy
import hashlib,re

from asr_public_name_candidates import MandarinRetrievalKey,propose

SCHEMA = 'navren.asr-name-review.v1'
NOTICE = '请核对产品名称及份额后发送'
# A UI-assistance heuristic, never an accuracy threshold or identity confidence.
MIN_PHONETIC_SIMILARITY = 0.85

def _result(raw,status,reason,names=(),release=None):
    return {'schema_version':SCHEMA,'status':status,'reason':reason,
            'notice':NOTICE if status=='review_required' else '',
            'names':list(names),'raw_sha256':hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            'public_release_id':release,'hint_only':True,'identity_authority':False}

def _catalog_review(raw,catalog):
    """Internal pure projection of a catalog obtained by the trusted provider."""
    if (type(catalog)is not dict or set(catalog)!={'schema','release_id','as_of','entities'}
            or catalog['schema']!='navren.public-identity-catalog/1'
            or type(catalog['release_id'])is not str or not catalog['release_id']
            or type(catalog['entities'])is not list or len(catalog['entities'])>64):
        raise ValueError('PUBLIC_NAME_HINTS_UNAVAILABLE')
    from agent_runtime.data_boundary import CloudDataBoundary
    spellings={};products=[];exact=[]
    for row in catalog['entities']:
        if type(row)is not dict or row.get('kind')!='product':continue
        if (type(row.get('label'))is not str or not 1<=len(row['label'])<=160
                or any(ord(c)<32 or ord(c)==127 for c in row['label'])
                or type(row.get('public_ref'))is not str):
            raise ValueError('PUBLIC_NAME_HINTS_UNAVAILABLE')
        boundary=CloudDataBoundary()
        boundary.register_public_terms([row['label']],[row['public_ref']])
        names=boundary.public_query_hints()['public_terms']
        if not names:continue
        spellings[row['public_ref']]=names;products.append(row)
        for name in names:
            for match in re.finditer(re.escape(name),raw):exact.append((match.start(),match.end()))
    if not products:raise ValueError('PUBLIC_NAME_HINTS_UNAVAILABLE')
    projected={**catalog,'entities':products}
    proposals=propose(raw,projected,spellings,MandarinRetrievalKey())
    if proposals['status']=='manual_target_required':
        return _result(raw,'review_required','candidate_assist_limit',release=catalog['release_id'])
    names=[]
    for item in proposals['candidates']:
        term=item['query_spelling'];span=item['raw_span']
        # Correctly spelled names keep the original automatic flow. Near names
        # wholly covered by an exact native query spelling are not new targets.
        if span==term or any(a<=item['raw_start']and item['raw_end']<=b for a,b in exact):continue
        if (len(re.findall(r'[\u4e00-\u9fff]',term))<4
                or item['phonetic_edit_similarity']<MIN_PHONETIC_SIMILARITY):continue
        if item['label']not in names:names.append(item['label'])
        if len(names)==3:break
    return _result(raw,'review_required'if names else'not_required',
                   'possible_public_name_mismatch'if names else'no_public_name_mismatch',
                   names,catalog['release_id'])

def name_review(raw,provider):
    if type(raw)is not str or not 0<len(raw)<=8000:
        raise ValueError('INVALID_ASR_RAW')
    try:
        method=getattr(provider,'public_identity_catalog',None)
        if not callable(method):raise ValueError('PUBLIC_NAME_HINTS_UNAVAILABLE')
        # Current native provider owns release/availability checks. No client
        # catalog, profile, personal DB, terms/fees, or mutable ASR prompt is used.
        return _catalog_review(raw,method(limit=64))
    except Exception:
        # Never show cached/withdrawn guesses after a catalog/OS failure. Keep
        # the unchanged draft for manual checking; normal send remains available.
        return _result(raw,'review_required','public_name_hints_unavailable')

def attach_name_review(transcript,provider):
    """Only the caller's existing authorized final local-ASR publish path calls this."""
    if (type(transcript)is not dict or transcript.get('type')!='transcript'
            or transcript.get('final')is not True):return transcript
    raw=transcript.get('text')
    review=name_review(raw,provider)
    return {**transcript,'name_review':deepcopy(review)}
