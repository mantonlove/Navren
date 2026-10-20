"""本地审核证据的无损 JSON 整理，不计算金额、不注册指标、不授予权限。

输入只能来自已经完成边界和指标核验的服务端 packet。catalog 是本地
serializer 生成的传输表，不能接收模型提供的扩展。SHA256 只检测引用与
内容是否一致，不能替代 Base HMAC、MetricRegistry 或来源新鲜度校验；
攻击者同时改原件并重算引用仍需要由这些原有权限边界拒绝。

仅 amount_contract.amount_claims[*].metric 可去重。草案、每条 claim、kind、
evidence_ids、全部来源/假设/缺项/排除项和已有 source ref 协议原样保留。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re


_CATALOG = 'review_metric_catalog'
_REF = 'metric_ref'
_FIELDS = frozenset(('contract_version', 'metric_id', 'subject_id', 'metric_name',
    'currency', 'unit', 'value_minor', 'as_of', 'period_start', 'period_end',
    'data_scope', 'calculation_status', 'basis', 'formula_id', 'input_refs',
    'source_versions', 'excluded_items', 'coverage_gaps', 'assumed_inputs'))
_SIGNATURE = ('metric_id', 'subject_id', 'metric_name', 'currency', 'unit',
    'value_minor', 'as_of', 'period_start', 'period_end', 'data_scope',
    'calculation_status', 'basis', 'formula_id', 'source_versions')
_KINDS = frozenset(('recorded_fact', 'recorded_projection', 'candidate_scenario',
                   'planning_scenario', 'forecast', 'hypothesis'))
_REF_PATTERN = re.compile(r'rmetric_[0-9a-f]{64}\Z')


class ReviewEvidenceError(ValueError):
    """固定错误码不包含原件、用户资料或密钥。"""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='REVIEW_EVIDENCE_INVALID_PACKET'):
    raise ReviewEvidenceError(code)


def _check_json(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check_json(item)
        return
    _fail('REVIEW_EVIDENCE_INVALID_JSON')


def _copy_packet(packet):
    if type(packet) is not dict:
        _fail()
    try:
        _check_json(packet)
        _encoded(packet)
        return deepcopy(packet)
    except (RecursionError, OverflowError):
        _fail('REVIEW_EVIDENCE_INVALID_JSON')


def _encoded(value, *, canonical=False):
    # 使用生产 encoded 的紧凑 JSON 分隔符；canonical 只用于摘要/类型精确比较。
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=canonical, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        _fail('REVIEW_EVIDENCE_INVALID_JSON')


def _reserved(value, path=(), *, packed=False):
    if type(value) is dict:
        for key, item in value.items():
            if key == _CATALOG and (not packed or path):
                _fail('REVIEW_EVIDENCE_RESERVED_FIELD')
            if key == _REF and (not packed or len(path) != 3
                    or path[:2] != ('amount_contract', 'amount_claims')
                    or type(path[2]) is not int):
                _fail('REVIEW_EVIDENCE_RESERVED_FIELD')
            _reserved(item, path + (key,), packed=packed)
    elif type(value) is list:
        for index, item in enumerate(value):
            _reserved(item, path + (index,), packed=packed)


def _rows(packet, *, required=False):
    contract = packet.get('amount_contract')
    if type(contract) is not dict or contract.get('status') != 'validated':
        if required:
            _fail()
        return []
    rows = contract.get('amount_claims')
    if type(rows) is not list:
        _fail()
    return rows


def _sources(value):
    if (type(value) is not dict or not value or any(
            type(key) is not str or not key or type(version) is not str or not version
            for key, version in value.items())):
        _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    return value


def _full_sources(value, packet):
    full, short = 'source_versions' in value, 'source_version_ref' in value
    if full == short:
        _fail('REVIEW_EVIDENCE_SOURCE_CONFLICT')
    if full:
        return _sources(value['source_versions'])
    ref, catalog = value['source_version_ref'], packet.get('source_version_sets')
    if (type(ref) is not str or type(catalog) is not dict or ref not in catalog):
        _fail('REVIEW_EVIDENCE_UNKNOWN_SOURCE_REF')
    return _sources(catalog[ref])


def _full_metric(metric, packet):
    if type(metric) is not dict:
        _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    keys = set(metric)
    if keys not in (_FIELDS, _FIELDS - {'source_versions'} | {'source_version_ref'}):
        _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    for key in ('contract_version', 'metric_id', 'subject_id', 'metric_name',
                'currency', 'unit', 'data_scope', 'calculation_status', 'basis', 'formula_id'):
        if type(metric[key]) is not str or not metric[key]:
            _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    if metric['value_minor'] is not None and type(metric['value_minor']) is not int:
        _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    for key in ('as_of', 'period_start', 'period_end'):
        if metric[key] is not None and type(metric[key]) is not str:
            _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    for key in ('input_refs', 'excluded_items', 'coverage_gaps'):
        if type(metric[key]) is not list or any(type(item) is not str for item in metric[key]):
            _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    if (type(metric['assumed_inputs']) is not list
            or any(type(item) is not dict for item in metric['assumed_inputs'])):
        _fail('REVIEW_EVIDENCE_INVALID_METRIC')
    result = {key: deepcopy(value) for key, value in metric.items()
              if key != 'source_version_ref'}
    result['source_versions'] = deepcopy(_full_sources(metric, packet))
    return result


def _identity(metric, packet):
    return 'rmetric_' + hashlib.sha256(_encoded(_full_metric(metric, packet),
                                              canonical=True)).hexdigest()


def _check_row(row, metric, packet):
    full = _full_metric(metric, packet)
    if (type(row) is not dict or row.get('metric_id') != full['metric_id']
            or type(row.get('kind')) is not str or row['kind'] not in _KINDS
            or type(row.get('evidence_ids')) is not list
            or any(type(item) is not str for item in row['evidence_ids'])):
        _fail('REVIEW_EVIDENCE_BINDING_MISMATCH')
    claim = row.get('claim')
    if type(claim) is not dict:
        _fail('REVIEW_EVIDENCE_BINDING_MISMATCH')
    claim_full = {key: value for key, value in claim.items() if key != 'source_version_ref'}
    claim_full['source_versions'] = _full_sources(claim, packet)
    if (claim.get('claim_kind') != row['kind'] or type(claim.get('text')) is not str
            or any(key not in claim_full or _encoded(claim_full[key], canonical=True)
                   != _encoded(full[key], canonical=True) for key in _SIGNATURE)):
        _fail('REVIEW_EVIDENCE_BINDING_MISMATCH')


def pack_review_evidence(packet: dict) -> dict:
    """复制可信服务端 packet；仅整包 JSON 实际更短时去重完整 metric。

    输入已含 catalog/ref 会拒绝，避免把模型或重复 encoder 的内容当作新原件。
    即使不值得压缩，同 ID 不同原件/签名冲突也不会被静默覆盖。
    """
    original = _copy_packet(packet)
    _reserved(original)
    rows = _rows(original)
    if not rows:
        return original
    result = deepcopy(original)
    catalog, identities = {}, {}
    for row in _rows(result):
        if type(row) is not dict or 'metric' not in row:
            _fail('REVIEW_EVIDENCE_INVALID_METRIC')
        metric = row['metric']
        _check_row(row, metric, result)
        ref = _identity(metric, result)
        metric_id = metric['metric_id']
        previous = identities.get(metric_id)
        if previous is not None and (previous != ref or catalog[previous] != metric):
            _fail('REVIEW_EVIDENCE_METRIC_CONFLICT')
        if ref in catalog and catalog[ref] != metric:
            _fail('REVIEW_EVIDENCE_METRIC_CONFLICT')
        identities[metric_id] = ref
        catalog[ref] = deepcopy(metric)
        del row['metric']
        row[_REF] = ref
    result[_CATALOG] = catalog
    return result if len(_encoded(result)) < len(_encoded(original)) else original


def unpack_review_evidence(packet: dict) -> dict:
    """精确还原本地 encoder 的 packet；摘要一致性不等于金融事实/权限有效。"""
    result = _copy_packet(packet)
    _reserved(result, packed=True)
    if _CATALOG not in result:
        contract = result.get('amount_contract', {})
        rows = contract.get('amount_claims', []) if type(contract) is dict else []
        if type(rows) is list and any(type(row) is dict and _REF in row for row in rows):
            _fail('REVIEW_EVIDENCE_UNKNOWN_REF')
        identities = {}
        for row in _rows(result):
            if type(row) is not dict or 'metric' not in row:
                _fail('REVIEW_EVIDENCE_INVALID_METRIC')
            metric = row['metric']
            _check_row(row, metric, result)
            identity = (_identity(metric, result), _encoded(metric, canonical=True))
            if metric['metric_id'] in identities and identities[metric['metric_id']] != identity:
                _fail('REVIEW_EVIDENCE_METRIC_CONFLICT')
            identities[metric['metric_id']] = identity
        return result
    catalog = result[_CATALOG]
    if type(catalog) is not dict or not catalog:
        _fail('REVIEW_EVIDENCE_INVALID_CATALOG')
    identities = {}
    for ref, metric in catalog.items():
        if not _REF_PATTERN.fullmatch(ref) or ref != _identity(metric, result):
            _fail('REVIEW_EVIDENCE_CATALOG_MISMATCH')
        metric_id = metric['metric_id']
        if metric_id in identities and identities[metric_id] != ref:
            _fail('REVIEW_EVIDENCE_METRIC_CONFLICT')
        identities[metric_id] = ref
    used = set()
    for row in _rows(result, required=True):
        if type(row) is not dict or ('metric' in row and _REF in row):
            _fail('REVIEW_EVIDENCE_RESERVED_FIELD')
        if _REF not in row or type(row[_REF]) is not str or row[_REF] not in catalog:
            _fail('REVIEW_EVIDENCE_UNKNOWN_REF')
        ref = row[_REF]
        _check_row(row, catalog[ref], result)
        used.add(ref)
        row['metric'] = deepcopy(catalog[ref])
        del row[_REF]
    if used != set(catalog):
        _fail('REVIEW_EVIDENCE_UNUSED_REF')
    del result[_CATALOG]
    return result


# Existing reversible whole-provider metric transport; not a metric issuer.
_RATIO_FIELDS=(_FIELDS-{"value_minor"})|{"value_decimal"}
_FINANCE_CATALOG='financial_metric_catalog'
_FINANCE_REF='financial_metric_ref'
_FINANCE_REF_PATTERN=re.compile(r'fmetric_[a-f0-9]{64}\Z')


def _financial_snapshot(value):
    if type(value) is not dict:return False
    fields=_RATIO_FIELDS if value.get('unit')=='ratio' else _FIELDS
    return set(value) in (fields,fields-{'source_versions'}|{'source_version_ref'})


def _financial_ref(metric, packet):
    if metric.get('unit') not in ('minor_currency','ratio'):_fail('FINANCIAL_EVIDENCE_UNIT_INVALID')
    return 'fmetric_'+hashlib.sha256(_encoded(_full_metric(metric,packet),canonical=True)).hexdigest()


def _check_financial_reserved(value):
    if type(value) is dict:
        if _FINANCE_CATALOG in value or _FINANCE_REF in value:_fail('FINANCIAL_EVIDENCE_RESERVED_FIELD')
        for item in value.values():_check_financial_reserved(item)
    elif type(value) is list:
        for item in value:_check_financial_reserved(item)


def _financial_data_path(path, review):
    return bool(path and (path[0]=='data' if not review else
        path[0] in ('presentation_review','provider_review','review_metric_catalog')
        or len(path)>=3 and path[0]=='evidence' and type(path[1]) is int and path[2]=='data'))


def _financial_top_records(packet, *, review=False):
    """Native 字面完整原件可被 data 引用；绝不引用模型 claim。"""
    tops={};identities={}
    if review:return tops,identities
    for collection,unit in (('metrics','minor_currency'),('ratios','ratio')):
        if collection not in packet:continue
        rows=packet[collection]
        # A whole boundary gap remains literal and is never a reference target.
        if type(rows) is not list:continue
        for index,row in enumerate(rows):
            metric=row.get('metric') if type(row) is dict else None
            if not _financial_snapshot(metric):continue
            if metric['unit']!=unit:
                _fail('FINANCIAL_EVIDENCE_TOP_INVALID')
            ref=_financial_ref(metric,packet);mid=metric['metric_id']
            if row.get('metric_id')!=mid:_fail('FINANCIAL_EVIDENCE_TOP_INVALID')
            if mid in identities and identities[mid]!=ref:_fail('FINANCIAL_EVIDENCE_METRIC_CONFLICT')
            identities[mid]=ref
            tops.setdefault(ref,({'collection':collection,'index':index,'metric_id':mid,'unit':unit},metric))
    return tops,identities


def _financial_catalog_metric(entry, packet, *, review=False):
    if type(entry) is dict and set(entry)=={'collection','index','metric_id','unit'}:
        collection,index=entry['collection'],entry['index']
        if (review or collection not in ('metrics','ratios') or type(index) is not int or index<0
                or type(packet.get(collection)) is not list or index>=len(packet[collection])):
            _fail('FINANCIAL_EVIDENCE_TOP_INVALID')
        row=packet[collection][index]
        metric=row.get('metric') if type(row) is dict else None
        expected_unit='ratio' if collection=='ratios' else 'minor_currency'
        if (not _financial_snapshot(metric) or entry['unit']!=expected_unit or metric['unit']!=expected_unit
                or entry['metric_id']!=metric['metric_id'] or row.get('metric_id')!=metric['metric_id']):
            _fail('FINANCIAL_EVIDENCE_TOP_INVALID')
        return metric
    if not _financial_snapshot(entry):_fail('FINANCIAL_EVIDENCE_CATALOG_MISMATCH')
    return entry


def pack_financial_evidence(packet, *, review=False):
    """仅可信投影后producer调用。顶层claimable metrics/ratios原样保留。"""
    original=_copy_packet(packet);_check_financial_reserved(original)
    tops,identities=_financial_top_records(original,review=review)
    originals={};counts={}
    def collect(value,path=()):
        if _financial_data_path(path,review) and _financial_snapshot(value):
            ref=_financial_ref(value,original);mid=value['metric_id']
            if mid in identities and identities[mid]!=ref:_fail('FINANCIAL_EVIDENCE_METRIC_CONFLICT')
            identities[mid]=ref
            if ref in originals and _encoded(originals[ref],canonical=True)!=_encoded(value,canonical=True):
                _fail('FINANCIAL_EVIDENCE_REPRESENTATION_CONFLICT')
            originals[ref]=value;counts[ref]=counts.get(ref,0)+1
        elif type(value) is dict:
            for key,item in value.items():collect(item,path+(key,))
        elif type(value) is list:
            for index,item in enumerate(value):collect(item,path+(index,))
    collect(original)
    catalog={}
    for ref,value in originals.items():
        if ref in tops and _encoded(tops[ref][1],canonical=True)==_encoded(value,canonical=True):
            catalog[ref]=deepcopy(tops[ref][0])
        elif counts[ref]>1:catalog[ref]=deepcopy(value)
    if not catalog:return original
    if len(catalog)>64:_fail('FINANCIAL_EVIDENCE_CATALOG_LIMIT')
    def pack(value,path=()):
        if _financial_data_path(path,review) and _financial_snapshot(value):
            ref=_financial_ref(value,original)
            if ref in catalog:return {_FINANCE_REF:ref,'metric_id':value['metric_id'],'unit':value['unit']}
        if type(value) is dict:return {k:pack(v,path+(k,)) for k,v in value.items()}
        if type(value) is list:return [pack(v,path+(i,)) for i,v in enumerate(value)]
        return value
    packed=pack(original);packed[_FINANCE_CATALOG]=catalog
    if len(_encoded(packed))>=len(_encoded(original)):return original
    if unpack_financial_evidence(packed,review=review)!=original:_fail('FINANCIAL_EVIDENCE_ROUNDTRIP')
    return packed


def unpack_financial_evidence(packet, *, review=False):
    """纯一致性解包，不注册指标、不验签、不补模型claim的任何字段。"""
    result=_copy_packet(packet)
    if _FINANCE_CATALOG not in result:
        _check_financial_reserved(result);return result
    catalog=result.pop(_FINANCE_CATALOG)
    if type(catalog) is not dict or not 1<=len(catalog)<=64:_fail('FINANCIAL_EVIDENCE_CATALOG_INVALID')
    _,identities=_financial_top_records(result,review=review)
    originals={}
    for ref,entry in catalog.items():
        metric=_financial_catalog_metric(entry,result,review=review)
        if (type(ref) is not str or not _FINANCE_REF_PATTERN.fullmatch(ref)
                or _financial_ref(metric,result)!=ref):_fail('FINANCIAL_EVIDENCE_CATALOG_MISMATCH')
        _check_financial_reserved(entry)
        if metric['metric_id'] in identities and identities[metric['metric_id']]!=ref:
            _fail('FINANCIAL_EVIDENCE_METRIC_CONFLICT')
        identities[metric['metric_id']]=ref;originals[ref]=metric
    used=set()
    def unpack(value,path=()):
        if type(value) is dict:
            if _FINANCE_CATALOG in value:_fail('FINANCIAL_EVIDENCE_RESERVED_FIELD')
            if _FINANCE_REF in value:
                if not _financial_data_path(path,review) or set(value)!={_FINANCE_REF,'metric_id','unit'}:
                    _fail('FINANCIAL_EVIDENCE_RESERVED_FIELD')
                ref=value[_FINANCE_REF]
                if type(ref) is not str or ref not in catalog:_fail('FINANCIAL_EVIDENCE_UNKNOWN_REF')
                metric=originals[ref]
                if value['metric_id']!=metric['metric_id'] or value['unit']!=metric['unit']:
                    _fail('FINANCIAL_EVIDENCE_BINDING_MISMATCH')
                used.add(ref);return deepcopy(metric)
            if _financial_data_path(path,review) and _financial_snapshot(value):
                ref=_financial_ref(value,result);mid=value['metric_id']
                if mid in identities and identities[mid]!=ref:_fail('FINANCIAL_EVIDENCE_METRIC_CONFLICT')
                identities[mid]=ref
            return {k:unpack(v,path+(k,)) for k,v in value.items()}
        if type(value) is list:return [unpack(v,path+(i,)) for i,v in enumerate(value)]
        return value
    result=unpack(result)
    if used!=set(catalog):_fail('FINANCIAL_EVIDENCE_UNUSED_REF')
    return result
