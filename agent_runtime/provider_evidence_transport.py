"""受信 Boundary 投影之后的可逆 Provider JSON 传输；不验签、不批准、不授予权限。

仅指定 Provider 槽可用引用；金额/比例 claim 参数和顶层可声明指标保持原协议。
目录是可读用户数据，hash 只核一致性；有权 producer 与最终原件比较仍不可省略。
"""
from copy import deepcopy
import hashlib
import json
import math

CATALOG = 'provider_evidence_catalog'
REF = 'provider_evidence_ref'
POINTS = 'provider_evidence_points'
ORDER = 'provider_evidence_order'
SHAPE = 'provider_evidence_shape'
VALUES = 'provider_evidence_values'
LITERAL = 'provider_evidence_literal'
_RESERVED = frozenset({CATALOG, REF, POINTS, ORDER, SHAPE, VALUES, LITERAL})
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024
MAX_WORK_BYTES = 16 * 1024 * 1024
MAX_NODES = 200000
MAX_DEPTH = 80
MAX_CATALOG_ENTRIES = 512
MAX_REF_USES = 4096
MIN_SUBTREE_BYTES = 256
# 金融完整原件/参数由原双能力机制处理，本 codec 永不压这些子树。
_LITERAL_KEYS = frozenset({'metric_batch', 'metrics', 'ratios', 'metric',
    'amount_contract', 'body_amount_contract', 'ratio_contract',
    'amount_claims', 'ratio_claims', 'source_versions', 'source_version_sets',
    'financial_metric_catalog', 'financial_metric_ref', 'review_metric_catalog',
    'review_metric_ref', 'source_version_ref'})


class ProviderEvidenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='PROVIDER_EVIDENCE_INVALID_PACKET'):
    raise ProviderEvidenceError(code)


def _json(value, *, canonical=False):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
            sort_keys=canonical, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail()


def _hash(value):
    return hashlib.sha256(_json(value, canonical=True)).hexdigest()


def _check(value):
    count = 0
    active = set()
    def walk(v, depth=0):
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
        if type(v) in (dict, list):
            if id(v) in active: _fail('PROVIDER_EVIDENCE_CYCLE')
            active.add(id(v))
            if type(v) is dict:
                if any(type(k) is not str for k in v): _fail()
                children = v.values()
            else: children = v
            for item in children: walk(item, depth + 1)
            active.remove(id(v))
        elif v is None or type(v) in (str, bool, int): pass
        elif type(v) is float and math.isfinite(v): pass
        else: _fail()
    walk(value)
    if type(value) is not dict: _fail()
    if len(_json(value)) > MAX_INPUT_BYTES:
        _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')


def _reserved(value):
    if type(value) is dict:
        if _RESERVED.intersection(value): _fail('PROVIDER_EVIDENCE_RESERVED_FIELD')
        for item in value.values(): _reserved(item)
    elif type(value) is list:
        for item in value: _reserved(item)


def _slots(packet, review):
    """只接受固定的 already-projected Provider 容器，不搜客户任意字段。"""
    paths = []
    if not review:
        data = packet.get('data')
        if type(data) is dict:
            paths += [('data', k) for k in ('review_projection', 'provider_object') if k in data]
    else:
        for key in ('presentation_review', 'provider_review'):
            if key in packet: paths.append((key,))
        evidence = packet.get('evidence')
        if type(evidence) is list:
            for index, row in enumerate(evidence):
                data = row.get('data') if type(row) is dict else None
                if type(data) is dict:
                    paths += [('evidence', index, 'data', k) for k in
                        ('review_projection', 'provider_object') if k in data]
    return paths


def _get(packet, path):
    for key in path: packet = packet[key]
    return packet


def _put(packet, path, value):
    target = packet
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value


def _presentation(value):
    return (type(value) is dict and type(value.get('points')) is list
        and {'schema_version', 'view_id', 'layout', 'projection_hash', 'source_versions',
             'scope', 'measure', 'coverage', 'title'} <= set(value))


def _literal(value):
    return type(value) is dict and 'metric_id' in value


def _contains_literal(value):
    if _literal(value): return True
    if type(value) is dict:
        return bool(_LITERAL_KEYS.intersection(value)) or any(_contains_literal(v) for v in value.values())
    return type(value) is list and any(_contains_literal(v) for v in value)


def _residual(value):
    """展示/complete_review 仅共享非金融残余，来源/指标/claim 原位字面保留。"""
    if type(value) is not dict or not (_presentation(value) or
            {'body_amount_contract', 'presentation', 'language', 'text'} <= set(value)):
        return None
    literal = {k: v for k, v in value.items() if k == 'points' or
        k in _LITERAL_KEYS or _contains_literal(v)}
    header = {k: v for k, v in value.items() if k not in literal}
    return (header, literal) if literal and header else None


def pack_provider_evidence(packet, *, review=False):
    """本地受信 producer 使用；只有整个紧凑 JSON 更短才返回目录形式。"""
    if type(review) is not bool: _fail()
    _check(packet)
    _reserved(packet)
    original = deepcopy(packet)
    variants = {}
    def collect(value):
        if _literal(value): return
        if type(value) in (dict, list):
            ordered = _json(value)
            if not _contains_literal(value) and len(ordered) >= MIN_SUBTREE_BYTES:
                variants.setdefault(_hash(value), {}).setdefault(ordered,
                    [value, 0])[1] += 1
            residual = _residual(value)
            if residual:
                header, _ = residual
                ordered = _json(header)
                if len(ordered) >= MIN_SUBTREE_BYTES:
                    variants.setdefault(_hash(header), {}).setdefault(ordered,
                        [header, 0])[1] += 1
            for child in ([v for k, v in value.items() if k not in _LITERAL_KEYS]
                    if type(value) is dict else value): collect(child)
    slots = _slots(original, review)
    for path in slots: collect(_get(original, path))
    chosen = {}
    for key, orders in variants.items():
        ordered, (value, count) = max(orders.items(), key=lambda item: item[1][1])
        # Different dict orders are never silently unified. Header references
        # also carry each original key order explicitly.
        if count > 1 and (count - 1) * len(ordered) > count * 150 + 120:
            chosen[key] = (ordered, value)
    catalog = []
    indices = {}
    building = set()
    def reference(value):
        key = _hash(value)
        selected = chosen.get(key)
        return key if selected is not None and _json(value) == selected[0] else None
    def ensure(key):
        if key in indices: return indices[key]
        if key in building: _fail('PROVIDER_EVIDENCE_CYCLE')
        if len(catalog) >= MAX_CATALOG_ENTRIES: _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
        index = len(catalog)
        indices[key] = index
        catalog.append(None)
        building.add(key)
        value = chosen[key][1]
        packed = pack_node(value, skip=key)
        catalog[index] = {'kind': 'dict' if type(value) is dict else 'list', 'sha256': key, 'value': packed}
        building.remove(key)
        return index
    def pack_node(value, skip=None):
        if _literal(value): return deepcopy(value)
        if type(value) in (dict, list):
            key = reference(value) if not _contains_literal(value) else None
            if key is not None and key != skip:
                return {REF: ensure(key)}
            residual = _residual(value)
            if residual:
                header, literal = residual
                key = reference(header)
                if key is not None and key != skip:
                    index = ensure(key)
                    return {REF: index, LITERAL: {k: deepcopy(v) if k in _LITERAL_KEYS
                        else pack_node(v) for k, v in literal.items()}, ORDER: list(value)}
            if type(value) is dict:
                return {k: deepcopy(v) if k in _LITERAL_KEYS else pack_node(v) for k, v in value.items()}
            return [pack_node(v) for v in value]
        return value
    result = deepcopy(original)
    for path in slots: _put(result, path, pack_node(_get(original, path)))
    # A second pass interns readable repeated strings and repeated ordered key
    # shapes only inside these same typed slots/producer catalog values. Values
    # never leave this JSON; top claimable records/claim syntax are unchanged.
    strings, shapes = {}, {}
    def count(value):
        if _literal(value): return
        if type(value) is str:
            if len(_json(value)) >= 40: strings[value] = strings.get(value, 0) + 1
        elif type(value) is dict:
            if REF in value:
                if LITERAL in value:
                    for k, v in value[LITERAL].items():
                        if k not in _LITERAL_KEYS: count(v)
                return
            keys = tuple(value)
            if not _contains_literal(value): shapes[keys] = shapes.get(keys, 0) + 1
            for k, child in value.items():
                if k not in _LITERAL_KEYS: count(child)
        elif type(value) is list:
            for child in value: count(child)
    for path in slots: count(_get(result, path))
    for entry in catalog: count(entry['value'])
    string_targets = {v for v, n in strings.items()
        if n > 1 and (n - 1) * len(_json(v)) > n * 32 + 120}
    shape_targets = {keys for keys, n in shapes.items() if keys and n > 1
        and n * (sum(len(_json(k)) + 1 for k in keys) - 62)
            > len(_json(list(keys))) + 120}
    leaf_indices = {}
    def leaf(kind, value):
        identity = (kind, _hash(value))
        if identity not in leaf_indices:
            if len(catalog) >= MAX_CATALOG_ENTRIES: _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
            leaf_indices[identity] = len(catalog)
            catalog.append({'kind': kind, 'sha256': identity[1], 'value': deepcopy(value)})
        return leaf_indices[identity]
    def factor(value):
        if _literal(value): return deepcopy(value)
        if type(value) is str and value in string_targets: return {REF: leaf('str', value)}
        if type(value) is dict:
            if REF in value:
                return {k: ({a: deepcopy(b) if a in _LITERAL_KEYS else factor(b)
                    for a, b in v.items()} if k == LITERAL else deepcopy(v)) for k, v in value.items()}
            values = [deepcopy(v) if k in _LITERAL_KEYS else factor(v) for k, v in value.items()]
            if not _contains_literal(value) and tuple(value) in shape_targets:
                return {SHAPE: leaf('shape', list(value)), VALUES: values}
            return dict(zip(value, values))
        if type(value) is list: return [factor(v) for v in value]
        return value
    original_catalog_count = len(catalog)
    for path in slots: _put(result, path, factor(_get(result, path)))
    for index in range(original_catalog_count): catalog[index]['value'] = factor(catalog[index]['value'])
    if not catalog: return original
    result[CATALOG] = catalog
    if len(_json(result)) >= len(_json(original)): return original
    # This also checks limits/reachable entries of the precise encoder graph.
    restored = unpack_provider_evidence(result, review=review)
    if _json(restored) != _json(original): _fail('PROVIDER_EVIDENCE_ROUNDTRIP')
    return result


def unpack_provider_evidence(packet, *, review=False):
    """只还原固定 Provider 槽；先于旧金融/审核/source-ref 解码器使用。"""
    if type(review) is not bool: _fail()
    _check(packet)
    if CATALOG not in packet:
        _reserved(packet)
        return deepcopy(packet)
    catalog = packet[CATALOG]
    if type(catalog) is not list or not 1 <= len(catalog) <= MAX_CATALOG_ENTRIES:
        _fail('PROVIDER_EVIDENCE_INVALID_CATALOG')
    for entry in catalog:
        if (type(entry) is not dict or set(entry) != {'kind', 'sha256', 'value'}
                or entry['kind'] not in ('dict', 'list', 'str', 'shape')
                or type(entry['sha256']) is not str or len(entry['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in entry['sha256'])):
            _fail('PROVIDER_EVIDENCE_INVALID_CATALOG')
    result = deepcopy(packet)
    del result[CATALOG]
    slots = _slots(result, review)
    # Reserved fields outside these slots cannot create a transport input.
    outside = deepcopy(result)
    for path in slots: _put(outside, path, None)
    _reserved(outside)
    cache, active, dependencies = {}, set(), {}
    ref_uses, work_bytes = 0, 0
    def resolve(key):
        if type(key) is not int or not 0 <= key < len(catalog): _fail('PROVIDER_EVIDENCE_UNKNOWN_REF')
        if key in active: _fail('PROVIDER_EVIDENCE_CYCLE')
        if key in cache: return cache[key]
        if len(active) >= MAX_DEPTH: _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
        active.add(key)
        edges = set()
        value = decode(catalog[key]['value'], edges)
        expected = {'dict': dict, 'list': list, 'str': str, 'shape': list}[catalog[key]['kind']]
        if type(value) is not expected or _hash(value) != catalog[key]['sha256']:
            _fail('PROVIDER_EVIDENCE_CATALOG_MISMATCH')
        if catalog[key]['kind'] == 'shape' and (any(type(k) is not str for k in value)
                or len(value) != len(set(value)) or _LITERAL_KEYS.intersection(value) or 'metric_id' in value):
            _fail('PROVIDER_EVIDENCE_CATALOG_MISMATCH')
        if catalog[key]['kind'] in ('dict', 'list') and _contains_literal(value):
            _fail('PROVIDER_EVIDENCE_FINANCIAL_LITERAL_REQUIRED')
        cache[key], dependencies[key] = value, edges
        active.remove(key)
        return value
    def decode(value, used):
        nonlocal ref_uses, work_bytes
        if type(value) is dict and REF in value:
            if set(value) not in ({REF}, {REF, LITERAL, ORDER}):
                _fail('PROVIDER_EVIDENCE_INVALID_REF')
            key = value[REF]
            if type(key) is not int: _fail('PROVIDER_EVIDENCE_UNKNOWN_REF')
            ref_uses += 1
            if ref_uses > MAX_REF_USES: _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
            original = resolve(key)
            if catalog[key]['kind'] == 'shape': _fail('PROVIDER_EVIDENCE_INVALID_REF')
            work_bytes += len(_json(original))
            if work_bytes > MAX_WORK_BYTES: _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
            used.add(key)
            expanded = deepcopy(original)
            if LITERAL in value:
                order, literal = value[ORDER], value[LITERAL]
                if (type(expanded) is not dict or type(literal) is not dict or not literal
                        or set(expanded).intersection(literal) or type(order) is not list
                        or any(type(k) is not str for k in order) or len(order) != len(set(order))
                        or set(order) != set(expanded) | set(literal)):
                    _fail('PROVIDER_EVIDENCE_INVALID_REF')
                for field, item in literal.items():
                    if field in _LITERAL_KEYS or _literal(item):
                        _reserved(item)
                        expanded[field] = deepcopy(item)
                    else: expanded[field] = decode(item, used)
                expanded = {k: expanded[k] for k in order}
                if _residual(expanded) is None: _fail('PROVIDER_EVIDENCE_INVALID_REF')
            return expanded
        if type(value) is dict and SHAPE in value:
            if set(value) != {SHAPE, VALUES}: _fail('PROVIDER_EVIDENCE_INVALID_REF')
            index = value[SHAPE]
            keys = resolve(index)
            if catalog[index]['kind'] != 'shape' or type(value[VALUES]) is not list or len(keys) != len(value[VALUES]):
                _fail('PROVIDER_EVIDENCE_INVALID_REF')
            ref_uses += 1
            if ref_uses > MAX_REF_USES: _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
            used.add(index)
            return {k: decode(v, used) for k, v in zip(keys, value[VALUES])}
        if type(value) is dict:
            if _RESERVED.intersection(value): _fail('PROVIDER_EVIDENCE_RESERVED_FIELD')
            if _literal(value):
                _reserved(value)
                return deepcopy(value)
            result = {}
            for k, v in value.items():
                if k in _LITERAL_KEYS:
                    _reserved(v)
                    result[k] = deepcopy(v)
                else: result[k] = decode(v, used)
            return result
        if type(value) is list: return [decode(v, used) for v in value]
        return value
    # Validate every catalog member, including entries not reached by roots.
    for key in range(len(catalog)): resolve(key)
    used = set()
    for path in slots: _put(result, path, decode(_get(result, path), used))
    reachable = set()
    def visit(key):
        if key in reachable: return
        reachable.add(key)
        for child in dependencies[key]: visit(child)
    for key in used: visit(key)
    if reachable != set(range(len(catalog))): _fail('PROVIDER_EVIDENCE_UNUSED_REF')
    if len(_json(result)) > MAX_EXPANDED_BYTES:
        _fail('PROVIDER_EVIDENCE_GRAPH_LIMIT')
    _check(result)
    return result
