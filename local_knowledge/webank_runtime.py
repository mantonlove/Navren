"""受信微众目录的本地发现与确定性准入；不发布、不接收查询或任意 URL。

``read_webank_catalog`` 只读固定官方端点并返回本地原件及受控公开记录。
调用方在本地选定公开渠道产品后，把同一原件交给 ``decode_webank_catalog``。
准入仅证明 JSONP 字段、渠道代码与原文的绑定，不证明底层份额、完整金融
语义、当前可买或交易资格。临时发布和正式晋升均由上层运行服务决定。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date
import json
import math
import re
import threading
import time

from webank_catalog import URL, PAGE, MAX_BYTES, FIELD_MAP, decode_catalog

from .importers import import_webank_disclosure
from .sources import fetch_registered
from .store import FACETS, WEBANK_PARSER, KnowledgeError, ident, packed, sha, today


SOURCE_ID = 'webank-direct-disclosure'
SHAPE = 'webank-productpresentation-v1'
MAX_PRODUCTS = 12
FRESHNESS = {'sale_status': 0, 'fees': 7, 'risk': 7, 'eligibility': 7, 'general': 7}
FIELD_FACETS = {
    'general': ('code', 'name', 'type', 'placement', 'saleType', 'publish'),
    'fees': ('charge', 'chargeWay'),
    'risk': ('risk',),
    'eligibility': ('investor', 'customerGroupType'),
    'sale_status': ('saleStatus',),
}
_INJECTED_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix='webank-fixture-reader')
_INJECTED_SLOTS = threading.BoundedSemaphore(2)


def _pause(provider):
    try:
        if not provider.source(SOURCE_ID)['paused']:
            provider.set_source_state(SOURCE_ID, paused=True)
    except KnowledgeError as error:
        if error.code != 'source_unavailable':
            raise


def ensure_webank_source(provider):
    """只登记固定、已受信元数据；不抓取，不覆盖既有许可或撤销状态。"""
    # INSERT OR IGNORE avoids the register_source upsert restoring a permission
    # changed concurrently between an initial lookup and first registration.
    with provider.db() as db:
        db.execute('''INSERT OR IGNORE INTO sources
            (id,publisher,kind,endpoints,read_allowed,outbound_allowed,enabled,paused,
             parser_id,shape,freshness,permission_basis)
            VALUES(?,?,?,?,1,1,1,0,?,?,?,?)''',
            (SOURCE_ID, '微众银行', 'official', packed([URL]), WEBANK_PARSER, SHAPE,
             packed(FRESHNESS), 'official_public_dated_channel_disclosure_short_quotes'))
    source = provider.source(SOURCE_ID)
    if (source['publisher'] != '微众银行' or source['kind'] != 'official'
            or source['endpoints'] != [URL] or source['parser_id'] != WEBANK_PARSER
            or source['shape'] != SHAPE):
        _pause(provider)
        raise KnowledgeError('source_structure_drift')
    return source


def _permitted_source(provider):
    source = ensure_webank_source(provider)
    if source['paused']:
        raise KnowledgeError('source_structure_drift')
    if not source['enabled'] or not source['read_allowed'] or not source['outbound_allowed']:
        raise KnowledgeError('source_permission_missing')
    if not source['permission_basis']:
        raise KnowledgeError('source_permission_missing')
    if any(facet not in source['freshness'] for facet in FIELD_FACETS):
        raise KnowledgeError('freshness_policy_missing')
    provider.validate_source_url(SOURCE_ID, URL)
    return source


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_catalog_key')
        value[key] = item
    return value


def _non_json_constant(_):
    raise ValueError('non_json_catalog_constant')


def _strict_catalog(provider, body):
    """全原件先验校验；不能因只选少量产品而漏过其他行的 schema 漂移。"""
    if not isinstance(body, bytes) or not body:
        raise KnowledgeError('invalid_source_response')
    if len(body) > MAX_BYTES:
        raise KnowledgeError('response_too_large')
    try:
        wrapper = re.fullmatch(r'\s*_jsonpproductpresentation\((.*)\)\s*;?\s*',
                               body.decode('utf-8'), re.S)
        if wrapper is None:
            raise ValueError('invalid_catalog_wrapper')
        raw = json.loads(wrapper[1], object_pairs_hook=_unique_object,
                         parse_constant=_non_json_constant)
        if not isinstance(raw, dict) or set(raw) != {'extractDate', 'productPresentation'}:
            raise ValueError('invalid_catalog_shape')
        source_date, decoded = decode_catalog(body)
        rows = raw['productPresentation']
        required = set(FIELD_MAP) - {'customerGroupType'}
        for row in rows:
            if not required.issubset(row) or set(row) - set(FIELD_MAP):
                raise ValueError('invalid_catalog_fields')
            ident('WEBANK:' + row['code'])
            if not 1 <= len(row['name']) <= 240:
                raise ValueError('invalid_catalog_name')
            for value in row.values():
                if value is not None and (len(value) > 4000 or any(
                        ord(c) < 32 and c not in '\n\r\t' for c in value)):
                    raise ValueError('invalid_catalog_field')
        records = []
        for product in decoded:
            records.append({**product, 'product_id': 'WEBANK:' + product['code'],
                            'channel_id': 'WEBANK', 'share_class_id': None,
                            'identity_scope': 'webank_channel_disclosure_code',
                            'share_mapping_verified': False})
        return source_date, rows, records
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        _pause(provider)
        raise KnowledgeError('source_structure_drift') from None


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise KnowledgeError('invalid_timeout')
    return max(.01, min(float(value), 15.0))


def _read(provider, timeout, reader):
    if reader is None:
        return fetch_registered(provider, SOURCE_ID, URL, timeout=timeout, max_bytes=MAX_BYTES)
    # Injected readers are trusted local test/application functions, never tool
    # parameters. Bound late readers too: a fixture cannot accidentally admit a
    # response arriving after the caller's deadline or create unbounded threads.
    if not callable(reader):
        raise KnowledgeError('invalid_source_reader')
    if not _INJECTED_SLOTS.acquire(blocking=False):
        raise KnowledgeError('source_reader_busy')
    deadline = time.monotonic() + timeout
    try:
        future = _INJECTED_READERS.submit(reader, provider, SOURCE_ID, URL,
                                        timeout=timeout, max_bytes=MAX_BYTES)
    except BaseException:
        _INJECTED_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _INJECTED_SLOTS.release())
    try:
        body = future.result(timeout=max(.001, deadline - time.monotonic()))
    except FutureTimeout:
        future.cancel()
        raise KnowledgeError('deadline_exceeded') from None
    if time.monotonic() > deadline:
        raise KnowledgeError('deadline_exceeded')
    return body


def read_webank_catalog(provider, *, timeout=8, reader=None):
    """固定端点本地发现；返回全目录，不把用户查询送到远端。"""
    timeout = _timeout(timeout)
    source = _permitted_source(provider)
    try:
        body = _read(provider, timeout, reader)
    except KnowledgeError:
        raise
    except (OSError, ValueError, TypeError):
        # Transport failure does not declare a schema drift or revive old data.
        raise KnowledgeError('source_unavailable') from None
    source_date, _, records = _strict_catalog(provider, body)
    current = _permitted_source(provider)
    if current['revision'] != source['revision']:
        raise KnowledgeError('source_revision_changed')
    return {'raw_bytes': body, 'records': records, 'source_date': source_date.isoformat(),
            'raw_sha256': sha(body), 'source_id': SOURCE_ID, 'source_url': URL,
            'source_page': PAGE, 'source_revision': current['revision'],
            'scope': 'dated_channel_disclosure', 'share_mapping_verified': False,
            'current_buyability_verified': False, 'transaction_authorized': False}


def _selection(product_ids, facets, max_products):
    if type(max_products) is not int or not 1 <= max_products <= MAX_PRODUCTS:
        raise KnowledgeError('invalid_product_limit')
    if not isinstance(product_ids, (list, tuple)) or not 1 <= len(product_ids) <= max_products:
        raise KnowledgeError('invalid_product_selection')
    selected = []
    for product_id in product_ids:
        ident(product_id)
        if not product_id.startswith('WEBANK:') or not product_id[7:]:
            raise KnowledgeError('identity_unverified')
        if product_id not in selected:
            selected.append(product_id)
    if (not isinstance(facets, (list, tuple)) or not 1 <= len(facets) <= 8
            or any(not isinstance(f, str) or f not in FACETS for f in facets)):
        raise KnowledgeError('invalid_facets')
    return selected, list(dict.fromkeys(facets))


def _coverage(rows, facets, source_date, source, as_of):
    age = (as_of - source_date).days
    coverage, covered, gaps = {}, {}, []
    for row in rows:
        product_id = 'WEBANK:' + row['code']
        coverage[product_id], covered[product_id] = {}, []
        for facet in facets:
            fields = [key for key in FIELD_FACETS.get(facet, ())
                      if isinstance(row.get(key), str) and row[key].strip()]
            maximum = source['freshness'].get(facet)
            reason = ('facet_not_disclosed' if facet not in FIELD_FACETS else
                      'not_yet_effective' if age < 0 else
                      'stale_evidence' if maximum is not None and age > maximum else
                      'source_field_missing' if not fields else None)
            coverage[product_id][facet] = {
                'available': reason is None, 'fields': fields,
                'scope': 'exact_catalog_field_disclosure',
                'semantic_sufficiency_verified': False,
                'data_on': source_date.isoformat(), 'age_days': age}
            if reason is None:
                covered[product_id].append(facet)
            else:
                gaps.append({'product_id': product_id, 'facet': facet,
                             'reason': reason, 'code': reason})
    return coverage, covered, gaps


def decode_webank_catalog(provider, body, *, product_ids, facets=('general',),
                          max_products=MAX_PRODUCTS, as_of=None):
    """同一原始 JSONP 的选中记录准入；只 stage/approve，绝不 publish。

    来源在读取和准入时都须仍有许可。完整预检在写产品、候选前完成；
    未知代码整批拒绝，不以客户端声称或代码格式证明公开身份。
    """
    selected, facets = _selection(product_ids, facets, max_products)
    try:
        observed = date.fromisoformat(as_of if as_of is not None else today())
        if as_of is not None and observed.isoformat() != as_of:
            raise ValueError()
    except (ValueError, TypeError):
        raise KnowledgeError('invalid_date') from None
    source = _permitted_source(provider)
    source_date, raw_rows, records = _strict_catalog(provider, body)
    by_id = {'WEBANK:' + row['code']: row for row in raw_rows}
    if any(product_id not in by_id for product_id in selected):
        raise KnowledgeError('identity_unverified')
    rows = [by_id[product_id] for product_id in selected]
    # Preflight every selected identity before importer writes any product.
    with provider.db() as db:
        for product_id, row in zip(selected, rows):
            existing = db.execute('SELECT issuer,scheme,code FROM products WHERE id=?', (product_id,)).fetchone()
            if existing and tuple(existing) != ('WEBANK_DISCLOSURE', 'webank_channel_product_code', row['code']):
                raise KnowledgeError('identity_mismatch')
    coverage, covered, gaps = _coverage(rows, facets, source_date, source, observed)
    usable = [product_id for product_id in selected if covered[product_id]]
    result = {'imported': 0, 'rejected': 0, 'version_ids': [], 'release_id': None}
    if usable:
        result.update(import_webank_disclosure(provider, body, codes=[p[7:] for p in usable],
                                              publish=False, limit=max_products))
    current = _permitted_source(provider)
    if current['revision'] != source['revision']:
        raise KnowledgeError('source_revision_changed')
    selected_set = set(selected)
    result.update(records=[record for record in records if record['product_id'] in selected_set],
                  source_id=SOURCE_ID, source_url=URL, source_page=PAGE,
                  source_revision=current['revision'], source_date=source_date.isoformat(),
                  raw_sha256=sha(body), as_of=observed.isoformat(),
                  facet_coverage=coverage, covered_facets=covered, coverage_gaps=gaps,
                  verification_scope='strict_jsonp_exact_channel_field_binding',
                  semantic_sufficiency_verified=False, share_mapping_verified=False,
                  current_buyability_verified=False, complete_fee_schedule_verified=False,
                  transaction_authorized=False, scope='dated_channel_disclosure',
                  published=False)
    return result


def supplement_webank_catalog(provider, *, product_ids, facets=('general',),
                              max_products=MAX_PRODUCTS, timeout=8, reader=None, as_of=None):
    """固定公开目录读取加准入；需要发现名称时优先 read 后复用原件。"""
    _selection(product_ids, facets, max_products)
    catalog = read_webank_catalog(provider, timeout=timeout, reader=reader)
    return decode_webank_catalog(provider, catalog['raw_bytes'], product_ids=product_ids,
                                 facets=facets, max_products=max_products, as_of=as_of)
