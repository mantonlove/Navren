"""微众现行官网的带日期销售披露；公开资料与可执行银行产品分离。"""
from datetime import date, datetime, timezone
import hashlib
import json
import re
import threading
import time
from urllib.parse import quote
import urllib.request
from zoneinfo import ZoneInfo

URL = 'https://tctp.tctpwebankcdn.net/owb-admin/data/appsystems/owb-admin/productpresentation.json'
PAGE = 'https://www.webank.com/inquiryCenter/query'
MAX_BYTES = 12 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_PAGE = 500
FIELD_MAP = {
    'code': 'code', 'name': 'name', 'saleStatus': 'sale_status',
    'type': 'type', 'placement': 'placement', 'saleType': 'sale_type',
    'risk': 'risk', 'publish': 'publish', 'investor': 'investor',
    'charge': 'charge', 'chargeWay': 'charge_way',
    'customerGroupType': 'customer_group_type',
}


def publication_today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def decode_catalog(body):
    if len(body) > MAX_BYTES:
        raise ValueError('catalog_too_large')
    match = re.fullmatch(r'\s*_jsonpproductpresentation\((.*)\)\s*;?\s*',
                         body.decode('utf-8'), re.S)
    if not match:
        raise ValueError('invalid_catalog_wrapper')
    raw = json.loads(match[1])
    source_date = date.fromisoformat(raw['extractDate'])
    if source_date.isoformat() != raw['extractDate'] or source_date > publication_today():
        raise ValueError('invalid_catalog_date')
    rows = raw['productPresentation']
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_RECORDS:
        raise ValueError('invalid_catalog_rows')
    products, codes = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_catalog_row')
        if any(not isinstance(row.get(k), str) for k in FIELD_MAP if k != 'customerGroupType'):
            raise ValueError('invalid_catalog_fields')
        group = row.get('customerGroupType')
        if group is not None and not isinstance(group, str):
            raise ValueError('invalid_customer_group')
        code = row['code']
        if not code or not row['name'] or code in codes:
            raise ValueError('missing_or_duplicate_product_identity')
        codes.add(code)
        product = {target: row.get(source) for source, target in FIELD_MAP.items()}
        product.update(source_url='https://www.webank.com/product/query/' + quote(code, safe=''),
                       source_date=source_date.isoformat(), sales_status_as_of=source_date.isoformat(),
                       current_sale_status=None, annual_return=None, executable=False)
        products.append(product)
    return source_date, products


class PublicProductCatalog:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen
        self.lock, self.cached, self.until = threading.Lock(), None, 0

    def read(self, query='', limit=24, offset=0):
        with self.lock:
            if time.monotonic() > self.until:
                checked = datetime.now(timezone.utc).isoformat()
                try:
                    with self.opener(URL, timeout=15) as response:
                        body = response.read(MAX_BYTES + 1)
                    source_date, products = decode_catalog(body)
                    stale = (publication_today() - source_date).days > 7
                    self.cached = dict(
                        provider='微众银行官方公开产品查询', source_url=PAGE, data_url=URL,
                        source_date=source_date.isoformat(), stale=stale,
                        fetched_at=checked, checked_at=checked, fetch_status='ok',
                        sha256=hashlib.sha256(body).hexdigest(),
                        status='historical' if stale else 'current_disclosure',
                        disclosure_scope='dated_channel_snapshot', total=len(products), products=products,
                        notice='销售状态是资料日期对应的微众官方快照；当天是否可申购、额度、限额和渠道费率须另行核验。净值与收益未披露时保持未知。')
                except Exception:
                    if self.cached and self.cached.get('products'):
                        self.cached = {**self.cached, 'checked_at': checked, 'fetch_status': 'failed',
                                       'notice': '官方目录本次读取失败，保留上次核验的带日期快照；没有更新为今天的在售状态。'}
                    else:
                        self.cached = dict(provider='微众银行官方公开产品查询', source_url=PAGE,
                                           data_url=URL, source_date=None, stale=True,
                                           checked_at=checked, fetch_status='failed', status='unavailable',
                                           disclosure_scope='dated_channel_snapshot', total=0, products=[],
                                           notice='官方资料暂时无法读取；没有用旧接口或模拟产品替代。')
                self.until = time.monotonic() + 3600
            if self.cached.get('source_date'):
                # 失败或命中缓存时，资料日期仍会随时间变旧。
                stale = (publication_today() - date.fromisoformat(self.cached['source_date'])).days > 7
                self.cached['stale'] = stale
                self.cached['status'] = 'historical' if stale else 'current_disclosure'
            result = {k: v for k, v in self.cached.items() if k != 'products'}
            rows = self.cached['products']
            if query:
                wanted = query.casefold()
                rows = [p for p in rows if wanted in ' '.join(str(p.get(k) or '') for k in FIELD_MAP.values()).casefold()]
            limit = max(1, min(int(limit), MAX_PAGE))
            offset = max(0, int(offset))
            result.update(matched=len(rows), offset=offset, limit=limit,
                          has_more=offset + limit < len(rows),
                          catalog_version=result.get('sha256'), products=rows[offset:offset + limit])
        result['source_policy'] = 'webank_disclosure_only'
        # 保留兼容字段，但产品查询不再附带其他机构的净值或条款。
        result['verified_nav_products'] = []
        result['verified_nav_scope'] = '产品资料仅采用微众银行直接公开披露；该目录未提供连续净值、具体渠道费率和实时申购条件，缺项保持未知。'
        return result
