"""Bounded, lossless, per-review segment catalog for projected goal drafts.

The catalog is untrusted review data, never instructions or financial evidence.
"""
import hashlib
import json
import re
import unicodedata
from simulated_bank import BankError

VERSION='navren.goal-review-segments/1'
MAX_BODY_BYTES=8192
MAX_SEGMENT_CHARS=200
MAX_SEGMENTS=128
MAX_CATALOG_BYTES=24576
# A body byte can grow at most fourfold when JSON-encoded inside the outer
# provider JSON (quote/backslash are escaped at both levels). Each segment
# adds fixed ASCII framing; binding uses a UUID, 64-bit revisions and hashes.
_wire_json=lambda x:json.dumps(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')),ensure_ascii=False).encode('utf-8')
_bound_base={'version':VERSION,'binding':{'request_id':'f'*32,'round_number':128,
    'privacy_revision':(1<<63)-1,'body_sha256':'f'*64,'receipt_sha256':'f'*64},'segments':[]}
_bound_row={'id':'g'+'f'*24,'text':''}
MAX_CATALOG_WIRE_GROWTH=4*MAX_BODY_BYTES+len(_wire_json(_bound_base))+MAX_SEGMENTS*(len(_wire_json(_bound_row))+1)+128

def _bad():
    raise BankError('AGENT_GOAL_REVIEW_INVALID','目标答复审核定位不可用，未发布。',422)

def _strict_equal(left,right):
    # Python's True == 1 is not equivalence for a typed proof binding.
    if type(left) is not type(right):return False
    if type(left) is dict:
        return set(left)==set(right) and all(_strict_equal(left[k],right[k]) for k in left)
    if type(left) is list:
        return len(left)==len(right) and all(_strict_equal(a,b) for a,b in zip(left,right))
    return left==right

def _extend(c):
    n=ord(c)
    return (unicodedata.category(c).startswith('M') or n==0x200d
            or 0x1f3fb<=n<=0x1f3ff or 0xe0020<=n<=0xe007f)

def _safe_cut(text,i):
    if i<=0 or i>=len(text):return True
    if _extend(text[i]) or ord(text[i-1])==0x200d:return False
    def regional(c):return 0x1f1e6<=ord(c)<=0x1f1ff
    if regional(text[i]) and regional(text[i-1]):
        j=i-1
        while j>=0 and regional(text[j]):j-=1
        if (i-1-j)%2:return False
    return True

def _split(text):
    pieces=[];pos=0
    while pos<len(text):
        hard=min(len(text),pos+MAX_SEGMENT_CHARS)
        if hard==len(text):end=hard
        else:
            end=hard
            while end>pos and not _safe_cut(text,end):end-=1
            if end==pos:_bad()
            low=pos+max(1,MAX_SEGMENT_CHARS//2)
            boundary=next((i for i in range(end,low-1,-1)
                           if text[i-1] in '。！？!?；;\n' and _safe_cut(text,i)),None)
            if boundary is not None:end=boundary
        pieces.append(text[pos:end]);pos=end
        if len(pieces)>MAX_SEGMENTS:_bad()
    if ''.join(pieces)!=text or any(not x or len(x)>MAX_SEGMENT_CHARS for x in pieces):_bad()
    return pieces

def build_goal_review_segments(packet, request_id, round_number, privacy_revision):
    from .data_boundary import validate_goal_natural_clarification
    if type(packet) is not dict or 'goal_review_segments' in packet:_bad()
    marker=validate_goal_natural_clarification(packet)
    if marker is None:_bad()
    if (type(request_id) is not str or re.fullmatch(r'[a-f0-9]{32}',request_id) is None
        or type(round_number) is not int or not 1<=round_number<=128
        or type(privacy_revision) is not int or not 0<=privacy_revision<=(1<<63)-1):_bad()
    body=packet['draft']['text']
    try:body_bytes=body.encode('utf-8')
    except UnicodeEncodeError:_bad()
    if (not body_bytes or len(body_bytes)>MAX_BODY_BYTES
        or any(ord(ch)<32 and ch not in '\t\n\r' for ch in body)):_bad()
    binding={'request_id':request_id,'round_number':round_number,
             'privacy_revision':privacy_revision,'body_sha256':hashlib.sha256(body_bytes).hexdigest(),
             'receipt_sha256':marker['receipt_sha256']}
    return _catalog_from_body(body,binding)

def _catalog_from_body(body,binding):
    binder=json.dumps(binding,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    segments=[];ids=set()
    for idx,piece in enumerate(_split(body)):
        ref='g'+hashlib.sha256(binder+b'\0'+str(idx).encode()+b'\0'+piece.encode()).hexdigest()[:24]
        if ref in ids:_bad()
        ids.add(ref);segments.append({'id':ref,'text':piece})
    catalog={'version':VERSION,'binding':binding,'segments':segments}
    if len(json.dumps(catalog,ensure_ascii=False,separators=(',',':')).encode())>MAX_CATALOG_BYTES:_bad()
    return catalog

def verify_stored_goal_review_segments(catalog, request_id, round_number, privacy_revision):
    """Verify encrypted historical evidence using independent stored turn context."""
    if (type(catalog) is not dict or set(catalog)!={'version','binding','segments'}
        or catalog['version']!=VERSION or type(catalog['binding']) is not dict
        or set(catalog['binding'])!={'request_id','round_number','privacy_revision','body_sha256','receipt_sha256'}
        or type(catalog['segments']) is not list or not catalog['segments']
        or any(type(row) is not dict or set(row)!={'id','text'} or type(row['text']) is not str for row in catalog['segments'])):_bad()
    binding=catalog['binding']
    if (type(binding['round_number']) is not int or type(binding['privacy_revision']) is not int
        or binding['request_id']!=request_id or binding['round_number']!=round_number
        or binding['privacy_revision']!=privacy_revision
        or type(request_id) is not str or re.fullmatch(r'[a-f0-9]{32}',request_id) is None
        or type(round_number) is not int or not 1<=round_number<=128
        or type(privacy_revision) is not int or not 0<=privacy_revision<=(1<<63)-1
        or type(binding['receipt_sha256']) is not str or re.fullmatch(r'[a-f0-9]{64}',binding['receipt_sha256']) is None):_bad()
    body=''.join(row['text'] for row in catalog['segments'])
    try:body_bytes=body.encode('utf-8')
    except UnicodeEncodeError:_bad()
    if (not body_bytes or len(body_bytes)>MAX_BODY_BYTES or hashlib.sha256(body_bytes).hexdigest()!=binding['body_sha256']
        or any(ord(ch)<32 and ch not in '\t\n\r' for ch in body)):_bad()
    if not _strict_equal(_catalog_from_body(body,binding),catalog):_bad()
    return catalog

def verify_goal_review_segments(packet, catalog, request_id, round_number, privacy_revision):
    expected=build_goal_review_segments(packet,request_id,round_number,privacy_revision)
    if not _strict_equal(catalog,expected):_bad()
    return expected
