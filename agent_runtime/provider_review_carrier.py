"""有限 GLM Provider 载体；只引用同包三处相同六点图，不发行/授权/补图。

默认 literal-v1。调用方的本地能力门、Base 回查与 Registry 不由 hash 代替。
只有已验 raw Provider + 当前 Boundary 发行的不可 JSON 化 witness 可启用 v2。
"""
from copy import deepcopy
import hashlib
import json
import math

LITERAL = 'literal-v1'
CONTRACT = 'navren.provider-review-carrier/2'
KEY = 'provider_review_carrier'
REF = 'provider_p_ref'
ROLES = ('evidence_review', 'presentation_review', 'provider_complete_review')
GUIDE = ('本包使用navren.provider-review-carrier/2；provider_p_ref只引用本包唯一完整canonical presentation。'
         '按三个role核对同一原件的全部金额、来源、范围、缺项和条件；不得凭引用补缺失字段或另建批准。')
_AUTHORITY = object()


class ProviderReviewCarrierError(ValueError):
    def __init__(self, code='PROVIDER_REVIEW_CARRIER_INVALID'):
        self.code = code
        super().__init__(code)


def fail(code='PROVIDER_REVIEW_CARRIER_INVALID'):
    raise ProviderReviewCarrierError(code)


def version(value):
    if type(value) is not str or value not in (LITERAL, CONTRACT):
        fail('PROVIDER_REVIEW_CARRIER_UNSUPPORTED')
    return value


def strict_equal(a, b):
    if type(a) is not type(b): return False
    if type(a) is dict: return set(a) == set(b) and all(strict_equal(a[k], b[k]) for k in a)
    if type(a) is list: return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _json(value):
    if value is None or type(value) in (str, bool, int): return
    if type(value) is float and math.isfinite(value): return
    if type(value) is list:
        for item in value: _json(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values(): _json(item)
        return
    fail()


def _copy(value):
    try:
        if type(value) is not dict: fail()
        _json(value)
        return deepcopy(value)
    except (RecursionError, OverflowError):
        fail()


def digest(value):
    try:
        _json(value)
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    except (RecursionError, UnicodeError, ValueError, TypeError, OverflowError):
        fail()


def _reserved(value, path=()):
    found = []
    if type(value) is dict:
        for key, item in value.items():
            if key in (KEY, REF): found.append(path + (key,))
            found.extend(_reserved(item, path + (key,)))
    elif type(value) is list:
        for index, item in enumerate(value): found.extend(_reserved(item, path + (index,)))
    return found


def _locate(packet):
    """同包唯一 canonical 与原绑定；layout/measure 的语义由 witness 单独核。"""
    try:
        receipt = packet['provider_review']; projection = receipt['projection']
        if receipt['contract'] != 'navren.presentation-provider/1': fail()
        if type(packet['evidence']) is not list: fail()
        rows = [(i, r) for i, r in enumerate(packet['evidence'])
            if type(r) is dict and type(r.get('data')) is dict
            and type(r['data'].get('provider_object')) is dict
            and strict_equal(r['data']['provider_object'].get('object_ref'), projection['object_ref'])]
        if len(rows) != 1: fail()
        index, row = rows[0]; obj = row['data']['provider_object']; p = obj['result']['presentation']
        if (type(row.get('id')) is not str or not row['id']
                or obj['contract'] != 'navren.presentation-provider/1' or obj['operation'] != 'query'
                or type(p) is not dict or p['contract'] != 'navren.presentation/1'
                or type(p['schema_version']) is not int or p['schema_version'] != 1
                or type(p['layout']) is not str or type(p['measure']) is not str
                or type(p['points']) is not list or len(p['points']) != 6): fail()
        language = obj['body_language']
        if type(language) is not str or language not in ('zh', 'en') or projection['body_language'] != language: fail()
        proof = receipt['history_proof']; batch = obj['result']['metric_batch']
        for key in ('request_id', 'message_id'):
            if not all(strict_equal(v, p[key]) for v in
                       (obj[key], projection[key], proof[key], batch['binding'][key])): fail()
        if (not strict_equal(proof['workspace_binding'], p['workspace_binding'])
                or not strict_equal(batch['workspace_id'], p['workspace_binding'])
                or not strict_equal(proof['view_id'], p['view_id'])
                or not strict_equal(batch['binding']['view_id'], p['view_id'])
                or not strict_equal(proof['view_projection_hash'], p['projection_hash'])
                or not strict_equal(proof['object_ref'], obj['object_ref'])
                or projection['complete_review']['language'] != language): fail()
        binding = {k: deepcopy(p[k]) for k in
                   ('workspace_binding', 'request_id', 'message_id', 'view_id', 'projection_hash')}
        binding.update(body_language=language, object_ref=deepcopy(obj['object_ref']),
            receipt_ref=deepcopy(receipt['receipt_ref']), original_receipt_hash=receipt['original_receipt_hash'],
            provider_projection_hash=projection['projection_hash'])
        return index, row, p, binding
    except (KeyError, TypeError, AttributeError, ValueError):
        fail()


def _slots(packet, row):
    try:
        return (row['data']['review_projection'], packet['presentation_review'],
                packet['provider_review']['projection']['complete_review'])
    except (KeyError, TypeError): fail()


class EligibilityWitness:
    """请求局部 sidecar，不可从 packet/模型 JSON 创建，deepcopy 不失 origin。"""
    __slots__ = ('_authority', '_canonical', '_binding')

    def __init__(self, authority, canonical, binding):
        if authority is not _AUTHORITY: fail('PROVIDER_REVIEW_CARRIER_WITNESS_REQUIRED')
        object.__setattr__(self,'_authority',authority)
        object.__setattr__(self,'_canonical',canonical)
        object.__setattr__(self,'_binding',binding)

    def __setattr__(self, name, value):
        raise AttributeError('immutable local carrier witness')

    def __deepcopy__(self, memo): return self

    def matches(self, packet):
        if self._authority is not _AUTHORITY: return False
        try:
            _, _, p, binding = _locate(packet)
            return self._canonical == digest(p) and self._binding == digest(binding)
        except ProviderReviewCarrierError: return False


def eligibility_witness(provider, raw, projected, boundary, *, metric_batches):
    """只由 Convo 在已验原件→真实 Boundary 后调用；不增 public/alias 权限。"""
    from .provider_consumer import ProviderConsumer
    from .data_boundary import CloudDataBoundary
    if type(provider) is not ProviderConsumer or type(boundary) is not CloudDataBoundary:
        fail('PROVIDER_REVIEW_CARRIER_WITNESS_REQUIRED')
    if type(raw) is not dict or type(projected) is not dict or type(metric_batches) is not list: fail()
    if 'provider_review' not in raw: return None
    receipt = raw['provider_review']
    # Genuine service HMAC/current request/language/TTL first; failures propagate.
    provider.verify(receipt)
    try:
        matches=[row['data']['provider_object'] for row in raw['evidence']
            if type(row) is dict and type(row.get('data')) is dict
            and type(row['data'].get('provider_object')) is dict
            and strict_equal(row['data']['provider_object'].get('object_ref'),receipt['projection']['object_ref'])]
        if len(matches)!=1:fail()
        obj=matches[0]
        rawp=obj['result']['presentation']
    except (KeyError,TypeError):fail()
    provider._entry(obj)  # exact locally issued object, not a legal foreign batch
    batch = obj['result']['metric_batch']
    if not any(strict_equal(batch, b) for b in metric_batches):
        fail('PROVIDER_REVIEW_CARRIER_WITNESS_REQUIRED')
    provider.g.financial_metrics.validate(provider.w, batch)
    # Genuine other operations/layouts/scopes remain literal v1, not an error
    # caused by this bounded transport's six-point eligibility restriction.
    if (obj['operation']!='query' or rawp['layout']!='funding_blocks'
            or rawp['measure']!='arrangeable_now' or type(rawp['points']) is not list
            or len(rawp['points'])!=6):return None
    _, rawrow, rawp, rawbinding = _locate(raw)
    _, row, p, binding = _locate(projected)
    try:
        # Only these exact typed fields are interpreted locally. No new alias
        # is registered; alias text/unknown shapes cannot establish eligibility.
        if (boundary._restore(p['layout']) != rawp['layout']
                or boundary._restore(p['measure']) != rawp['measure']
                or not strict_equal(boundary._restore(binding), rawbinding)
                or not all(strict_equal(part['presentation'], p) for part in _slots(projected, row))):
            return None
    except (KeyError, TypeError): fail()
    return EligibilityWitness(_AUTHORITY, digest(p), digest(binding))


def _witness(value, packet):
    return type(value) is EligibilityWitness and getattr(value,'_authority',None) is _AUTHORITY and value.matches(packet)


def pack_review_carrier(packet, *, allowed_version=LITERAL, witness=None):
    """返回(actual packet, effective version)；配置 v2 不等于实际 wire v2。"""
    version(allowed_version); out = _copy(packet)
    if _reserved(out): fail('PROVIDER_REVIEW_CARRIER_RESERVED')
    if allowed_version == LITERAL or witness is None: return out, LITERAL
    if not _witness(witness, out): fail('PROVIDER_REVIEW_CARRIER_WITNESS_REQUIRED')
    index, row, p, binding = _locate(out); parts = _slots(out, row)
    if not all(strict_equal(part['presentation'], p) for part in parts): return out, LITERAL
    dh, bh = digest(p), digest(binding)
    out[KEY] = {'contract': CONTRACT, 'canonical_evidence_id': row['id'],
        'canonical_path': 'provider_object.result.presentation', 'canonical_sha256': dh,
        'binding': binding, 'binding_sha256': bh}
    for role, part in zip(ROLES, parts):
        part['presentation'] = {REF: {'contract': CONTRACT, 'role': role,
            'canonical_evidence_id': row['id'], 'canonical_sha256': dh, 'binding_sha256': bh}}
    return out, CONTRACT


def unpack_review_carrier(packet, *, allowed_version=LITERAL, witness=None):
    """只从实际同包 canonical 恢复；v1/未知协议不接收 v2 引用。"""
    version(allowed_version); out = _copy(packet)
    if KEY not in out:
        if _reserved(out): fail('PROVIDER_REVIEW_CARRIER_UNSUPPORTED')
        return out
    if allowed_version != CONTRACT: fail('PROVIDER_REVIEW_CARRIER_UNSUPPORTED')
    marker = out.pop(KEY)
    if (type(marker) is not dict or set(marker) != {'contract', 'canonical_evidence_id',
            'canonical_path', 'canonical_sha256', 'binding', 'binding_sha256'}
            or marker['contract'] != CONTRACT
            or marker['canonical_path'] != 'provider_object.result.presentation'): fail()
    index, row, p, binding = _locate(out)
    paths = {('evidence', index, 'data', 'review_projection', 'presentation', REF),
        ('presentation_review', 'presentation', REF),
        ('provider_review', 'projection', 'complete_review', 'presentation', REF)}
    if set(_reserved(out)) != paths: fail()
    if (not strict_equal(marker['canonical_evidence_id'], row['id'])
            or marker['canonical_sha256'] != digest(p) or not strict_equal(marker['binding'], binding)
            or marker['binding_sha256'] != digest(binding)): fail()
    for role, part in zip(ROLES, _slots(out, row)):
        expected = {REF: {'contract': CONTRACT, 'role': role, 'canonical_evidence_id': row['id'],
            'canonical_sha256': marker['canonical_sha256'], 'binding_sha256': marker['binding_sha256']}}
        if not strict_equal(part['presentation'], expected): fail()
        part['presentation'] = deepcopy(p)
    if not _witness(witness, out): fail('PROVIDER_REVIEW_CARRIER_WITNESS_REQUIRED')
    return out


def transport_record(packet, *, allowed_version=LITERAL, witness=None):
    """记录实际物理格式，只含协议/hash；原 Base proof JSON 不增加字段。"""
    full = unpack_review_carrier(packet, allowed_version=allowed_version, witness=witness)
    if KEY not in packet: return {'effective_version': LITERAL}
    return {'effective_version': CONTRACT,
        'canonical_sha256': packet[KEY]['canonical_sha256'],
        'binding_sha256': packet[KEY]['binding_sha256']}
