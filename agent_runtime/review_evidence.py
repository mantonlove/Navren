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
