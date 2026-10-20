"""微众完整候选目录与第三方公开近一周收益的只读交集；不推定销售权限。"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
from http.client import HTTPException
import json
import re
import threading
import time
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.error import URLError
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from product_performance import ProductPerformance, SourceError, _date, _normal, _number

RANK_URL = 'https://fund.eastmoney.com/data/fundranking.html'
RANK_HOST = 'fund.eastmoney.com'
SCRIPT_HOST = 'j5.dfcfw.com'
METRIC_TYPE = 'one_week_return'
SOURCE_SCOPE = 'public_product_research'
MAX_SOURCE_ROWS = 30_000
MAX_CANDIDATES = 100_000


def _binding(page, script=None):
    """字段只采用网页与脚本共同声明的近1周列，绝不执行远端脚本。"""
    soup = BeautifulSoup(page, 'html.parser')
    headers = soup.select('th[col="zzf"]')
    settings = re.findall(r'\bvar\s+defaults\s*=\s*\{([^{}]+)\}', page)
    if (len(headers) != 1 or headers[0].get_text(strip=True) != '近1周'
            or '东方财富Choice数据' not in soup.get_text(' ', strip=True)
            or len(settings) != 1
            or not all(re.search(r'\b' + k + r'\s*:\s*"' + v + r'"', settings[0])
                       for k, v in [('src', 'rankhandler.aspx'), ('op', 'ph'), ('dt', 'kf'), ('ft', 'all')])):
        raise SourceError('ranking_field_binding_changed')
    urls = []
    for tag in soup.select('script[src]'):
        url = urlsplit(urljoin(RANK_URL, tag['src']))
        if (url.scheme == 'https' and url.hostname == SCRIPT_HOST and not url.username and not url.password
                and re.fullmatch(r'/sc/js/data/jskrank_min_[0-9]{8}\.js', url.path)):
            urls.append(url.geturl())
    if len(set(urls)) != 1:
        raise SourceError('ranking_script_binding_changed')
    if script is not None:
        compact = re.sub(r'\s+', '', script)
        required = ['this.dbObj.datas[i-1].split(",")', '"&sc="+defaults.sc', '"&pn="+defaults.pn',
                    'this.styleDateFormat(arrdata[3])', 'this.styleFormat(arrdata[4],4,false)',
                    'this.styleFormat(arrdata[5],4,false)']
        if (not all(part in compact for part in required)
                or not re.search(r'var_td_zzf[\s\S]{0,350}?\.styleFormat\(arrdata\[7\],2,true\)', compact)):
            raise SourceError('ranking_column_binding_changed')
    return urls[0]


def _rank_feed(body, today):
    """严格解析赋值包装、完整分页和原有周收益顺序；不 eval、不猜列。"""
    text = body.decode('utf-8-sig')
    start = re.match(r'\s*var\s+rankData\s*=\s*\{\s*datas\s*:\s*', text)
    if not start:
        raise SourceError('ranking_envelope_changed')
    datas, end = json.JSONDecoder().raw_decode(text[start.end():])
    tail = text[start.end() + end:]
    if not re.fullmatch(r'(?:\s*,\s*[A-Za-z_][A-Za-z_0-9]*\s*:\s*[0-9]+)+\s*\}\s*;\s*', tail):
        raise SourceError('ranking_metadata_changed')
    pairs = re.findall(r',\s*([A-Za-z_][A-Za-z_0-9]*)\s*:\s*([0-9]+)', tail)
    meta = {k: int(v) for k, v in pairs}
    if len(meta) != len(pairs) or not isinstance(datas, list) or not 0 < len(datas) <= MAX_SOURCE_ROWS:
        raise SourceError('ranking_rows_invalid')
    if (meta.get('pageIndex') != 1 or meta.get('pageNum') != MAX_SOURCE_ROWS or meta.get('allPages') != 1
            or meta.get('allRecords') != len(datas) or meta.get('allNum') != len(datas)):
        raise SourceError('ranking_incomplete')
    rows, codes, previous = [], set(), Decimal('Infinity')
    for raw in datas:
        if not isinstance(raw, str):
            raise SourceError('ranking_row_invalid')
        cells = raw.split(',')
        if len(cells) != 25:
            raise SourceError('ranking_columns_changed')
        code, name = cells[:2]
        if not re.fullmatch(r'[0-9]{6}', code) or code in codes or not name or len(name) > 200:
            raise SourceError('ranking_share_identity_invalid')
        codes.add(code)
        value = _number(cells[7])
        order_value = Decimal(value) if value is not None else Decimal('-Infinity')
        if order_value > previous:
            raise SourceError('ranking_order_changed')
        previous = order_value
        on = _date(cells[3], today)
        provisional = any(word in raw for word in ('暂估', '估算'))
        reason = ('暂估收益不参与排序' if provisional else '近1周收益未获取' if value is None
                  else '收益披露日期未核验' if on is None else None)
        rows.append(dict(code=code, name=name, as_of=on,
                         metric=dict(type=METRIC_TYPE, label='近1周涨跌幅', value_pct=value, as_of=on)
                         if reason is None else None,
                         unit_nav=_number(cells[4], nav=True), cumulative_nav=_number(cells[5], nav=True),
                         missing_reason=reason))
    return rows, meta


class PublicProductResearch(ProductPerformance):
    """首页不逐份额抓取；完整源与完整候选目录核验后才发布交集榜单。"""
    def __init__(self, catalog, opener=None, *, ttl=900, timeout=5, stale_days=7, clock=None):
        super().__init__(opener=opener, ttl=ttl, timeout=timeout, stale_days=stale_days, clock=clock)
        self.catalog = catalog
        self._research_lock = threading.Lock()
        self._research_pool, self._research_until = None, 0
        self._research_head_signature = None
        self._catalog_failure_signature, self._catalog_failure_reason, self._catalog_failure_until = None, None, 0

    @staticmethod
    def _head_signature(head):
        first = head['products'][0]
        return (head['catalog_version'], head['total'], first['code'], first['name'], first['type'])

    @staticmethod
    def _catalog_fetch_ok(page):
        sync = page.get('catalog_sync')
        return (page.get('fetch_status') in ('ok', 'unchanged')
                and ('catalog_sync' not in page or isinstance(sync, dict) and sync.get('fetch_status') in ('ok', 'unchanged')))

    @staticmethod
    def _catalog_is_fetching(page):
        sync = page.get('catalog_sync')
        return (page.get('fetch_status') == 'fetching' and isinstance(sync, dict)
                and sync.get('fetch_status') == 'fetching')

    def _cached_catalog_fetching(self, head, now):
        # 后台校对窗口只能沿用本进程已完整核验的同版本池，不能从冷快照建立新榜。
        pool, sync = self._research_pool, head.get('catalog_sync')
        if (not self._catalog_is_fetching(head) or not pool or not pool['ranking_complete']
                or pool.get('catalog_version') != head['catalog_version']
                or self._research_head_signature != self._head_signature(head)
                or sync.get('source_date') != head['source_date']):
            return False
        success = sync.get('last_success_at')
        if not isinstance(success, str):
            return False
        try:
            stamp = datetime.fromisoformat(success.replace('Z', '+00:00'))
            return stamp.tzinfo is not None and stamp <= now
        except ValueError:
            return False

    def _catalog_head(self, today, now):
        head = self.catalog.read(query='', limit=1, offset=0)
        if not isinstance(head, dict):
            raise SourceError('candidate_catalog_head_invalid')
        ready = self._catalog_fetch_ok(head)
        if (not ready and not self._catalog_is_fetching(head)) or head.get('status') == 'unavailable':
            return head, 'candidate_catalog_unavailable'
        count, version, rows = head.get('matched'), head.get('catalog_version'), head.get('products')
        if (not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{64}', version)
                or type(count) is not int or not 0 < count <= MAX_CANDIDATES or head.get('total') != count
                or head.get('offset') != 0 or head.get('limit') != 1 or not isinstance(rows, list) or len(rows) != 1
                or head.get('has_more') is not (count > 1) or not _date(head.get('source_date'), today)
                or not isinstance(rows[0], dict) or not isinstance(rows[0].get('code'), str) or not rows[0]['code']
                or not isinstance(rows[0].get('name'), str) or not rows[0]['name'] or not isinstance(rows[0].get('type'), str)):
            return head, 'candidate_catalog_head_invalid'
        if not ready and not self._cached_catalog_fetching(head, now):
            return head, 'candidate_catalog_sync_pending'
        return head, None

    @staticmethod
    def _catalog_metadata(result, head):
        # 检查时刻仅属于目录同步，绝不改写收益抓取时刻或净值披露日。
        sync = head.get('catalog_sync')
        result.update(catalog_version=head.get('catalog_version'), catalog_source_url=head.get('source_url'),
                      catalog_source_date=head.get('source_date'), catalog_status=head.get('status'),
                      catalog_fetch_status=head.get('fetch_status'), catalog_stale=head.get('stale', True),
                      catalog_sync=deepcopy(sync) if isinstance(sync, dict) else {k: head.get(k) for k in (
                          'checked_at', 'last_success_at', 'next_check_at', 'refresh_interval_seconds',
                          'fetch_status', 'source_date', 'background_enabled')})

    def _catalog_blocked(self, head, reason):
        # 暂存的旧池只用于后续成功时复用；失败响应不能带旧榜或旧详情。
        old = self._research_pool or {}
        result = dict(status='unavailable', ranking_complete=False, products=[], metric_type=METRIC_TYPE,
                      source_scope=SOURCE_SCOPE, executable=False, fetched_at=old.get('fetched_at'),
                      stale_after_days=self.stale_days, provider='天天基金／东方财富Choice公开数据',
                      rank_source_url=RANK_URL, source_version=None, source_receipts=[], failure_reason=reason,
                      coverage=dict(candidate_count=0, type_counts={}, source_count=0, matched_count=0, eligible_count=0),
                      content_version=hashlib.sha256(json.dumps([head.get('catalog_version'), reason], ensure_ascii=False).encode()).hexdigest())
        self._catalog_metadata(result, head)
        return result

    def _catalog(self, today, head=None):
        rows, expected, total, metadata, offset = [], None, None, None, 0
        if head is not None:
            expected, total, metadata = head['catalog_version'], head['total'], head
        while True:
            page = self.catalog.read(query='', limit=500, offset=offset)
            if not isinstance(page, dict):
                raise SourceError('candidate_catalog_pagination_changed')
            version = page.get('catalog_version')
            if (not self._catalog_fetch_ok(page) or page.get('status') == 'unavailable'
                    or not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{64}', version)):
                raise SourceError('candidate_catalog_unavailable')
            count = page.get('matched')
            if (type(count) is not int or not 0 < count <= MAX_CANDIDATES
                    or page.get('total') != count or page.get('offset') != offset
                    or page.get('limit') != 500 or not isinstance(page.get('products'), list)):
                raise SourceError('candidate_catalog_pagination_changed')
            if expected is None:
                expected, total, metadata = version, count, page
                if not _date(page.get('source_date'), today):
                    raise SourceError('candidate_catalog_date_invalid')
            if (version != expected or count != total or page.get('source_date') != metadata['source_date']
                    or len(page['products']) != min(500, total - offset)
                    or page.get('has_more') is not (offset + len(page['products']) < total)):
                raise SourceError('candidate_catalog_incomplete')
            rows.extend(page['products'])
            if not page['has_more']:
                break
            offset += 500
        codes = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('code'), str) or not row['code']
                    or row['code'] in codes or not isinstance(row.get('name'), str) or not row['name']
                    or not isinstance(row.get('type'), str)):
                raise SourceError('candidate_catalog_identity_invalid')
            codes.add(row['code'])
        return rows, {k: v for k, v in metadata.items() if k != 'products'}

    def _ranking(self, today):
        receipts, deadline, retried = [], time.monotonic() + 25, False
        def fetch(url, host):
            nonlocal retried
            try:
                return self._fetch(url, host, deadline, receipts)
            except (TimeoutError, URLError) as error:
                # 暂时超时只补读当前阶段一次；所有阶段与重试共用总预算。
                if (not isinstance(error, TimeoutError) and not isinstance(error.reason, TimeoutError)
                        or retried or deadline - time.monotonic() < self.timeout):
                    raise
                retried = True
                return self._fetch(url, host, deadline, receipts)
        # 复用官方业绩模块的HTTPS、注册host、同host重定向、总预算及4MiB上限。
        page = fetch(RANK_URL, RANK_HOST).decode('utf-8')
        script_url = _binding(page)
        script = fetch(script_url, SCRIPT_HOST).decode('utf-8')
        _binding(page, script)
        try:
            last_year = today.replace(year=today.year - 1)
        except ValueError:
            last_year = today.replace(year=today.year - 1, day=28)
        params = dict(op='ph', dt='kf', ft='all', rs='', gs='0', sc='zzf', st='desc',
                      sd=last_year.isoformat(), ed=today.isoformat(), qdii='', tabSubtype=',,,,,',
                      pi='1', pn=str(MAX_SOURCE_ROWS), dx='1')
        url = 'https://' + RANK_HOST + '/data/rankhandler.aspx?' + urlencode(params)
        body = fetch(url, RANK_HOST)
        rows, meta = _rank_feed(body, today)
        version = hashlib.sha256(json.dumps(receipts, sort_keys=True).encode()).hexdigest()
        return rows, dict(source_version=version, source_receipts=receipts, pagination=meta)

    @staticmethod
    def _failed(error):
        reason = str(error) if isinstance(error, SourceError) else type(error).__name__
        if callable(getattr(error, 'close', None)):
            error.close()
        return None, reason

    def _collect_research(self, now, today, head=None):
        def collect(fn):
            try:
                return fn(today), None
            except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
                return self._failed(error)
        with ThreadPoolExecutor(max_workers=2) as workers:
            catalog_future = workers.submit(collect, lambda current: self._catalog(current, head))
            ranking_future = workers.submit(collect, self._ranking)
            catalog, catalog_failure = catalog_future.result()
            ranking, ranking_failure = ranking_future.result()
        candidates, catalog_meta = catalog if catalog else ([], {})
        source_rows, source_meta = ranking if ranking else ([], {})
        complete = catalog is not None and ranking is not None
        source_by_code = {p['code']: p for p in source_rows}
        matched, products = 0, []
        for candidate in candidates:
            source = source_by_code.get(candidate['code'])
            if source and _normal(source['name']) != _normal(candidate['name']):
                source = None
                reason = '源份额名称与目录不一致'
            else:
                reason = source['missing_reason'] if source else '未取得同周期收益资料'
            if source:
                matched += 1
            code = candidate['code']
            item_version = hashlib.sha256(json.dumps([catalog_meta.get('catalog_version'),
                source_meta.get('source_version'), code, candidate['name']], ensure_ascii=False).encode()).hexdigest()
            products.append(dict(code=code, name=candidate['name'], type=candidate['type'],
                risk=candidate.get('risk'), manager=candidate.get('publish'), source_scope=SOURCE_SCOPE,
                executable=False, content_version=item_version, catalog_version=catalog_meta.get('catalog_version'),
                source_url=f'https://{RANK_HOST}/{code}.html' if source else None, rank_source_url=RANK_URL,
                as_of=source['as_of'] if source else None, metric=deepcopy(source['metric']) if source else None,
                raw=dict(source_name=source['name'] if source else None,
                         unit_nav=source['unit_nav'] if source else None,
                         cumulative_nav=source['cumulative_nav'] if source else None,
                         source_period='近1周', catalog_source_date=catalog_meta.get('source_date'),
                         catalog_source_url=candidate.get('source_url'), missing_reason=reason)))
        version = hashlib.sha256(json.dumps([catalog_meta.get('catalog_version'), source_meta.get('source_version'),
            catalog_failure, ranking_failure], ensure_ascii=False).encode()).hexdigest()
        return dict(status='ok' if complete else 'unavailable', ranking_complete=complete,
                    products=products, metric_type=METRIC_TYPE, source_scope=SOURCE_SCOPE, executable=False,
                    content_version=version, catalog_version=catalog_meta.get('catalog_version'),
                    catalog_source_date=catalog_meta.get('source_date'), catalog_stale=catalog_meta.get('stale', True),
                    fetched_at=now.isoformat(), stale_after_days=self.stale_days,
                    provider='天天基金／东方财富Choice公开数据', rank_source_url=RANK_URL,
                    coverage=dict(candidate_count=len(candidates), type_counts=dict(Counter(p['type'] for p in candidates)),
                                  source_count=len(source_rows), matched_count=matched, eligible_count=0),
                    failure_reason=catalog_failure or ranking_failure,
                    source_version=source_meta.get('source_version'), source_receipts=source_meta.get('source_receipts', [])), catalog_failure, ranking_failure

    def read(self, code=None):
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)):
            raise SourceError('invalid_research_share_code')
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError('clock_requires_timezone')
        today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
        with self._research_lock:
            try:
                head, failure = self._catalog_head(today, now)
            except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
                _, failure = self._failed(error)
                return self._catalog_blocked({}, failure)
            version = head.get('catalog_version')
            if self._research_pool is not None and version != self._research_pool.get('catalog_version'):
                self._research_until = 0
            if failure:
                return self._catalog_blocked(head, failure)
            signature = self._head_signature(head)
            if self._research_head_signature != signature:
                self._research_until = 0
            if self._catalog_failure_signature == signature and time.monotonic() < self._catalog_failure_until:
                return self._catalog_blocked(head, self._catalog_failure_reason)
            if not self._catalog_is_fetching(head) and (self._research_pool is None or time.monotonic() >= self._research_until):
                fresh, catalog_failure, ranking_failure = self._collect_research(now, today, head)
                try:
                    latest, failure = self._catalog_head(today, now)
                except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
                    _, failure = self._failed(error)
                    latest = {}
                if failure or self._head_signature(latest) != signature:
                    self._research_until = 0
                    return self._catalog_blocked(latest, failure or 'candidate_catalog_changed_during_research')
                if catalog_failure and not ranking_failure:
                    # 分页失败响应有短缓存，原完整池只留待目录恢复，不能盖过本次失败。
                    self._catalog_failure_signature, self._catalog_failure_reason = signature, catalog_failure
                    self._catalog_failure_until = time.monotonic() + min(self.ttl, 60)
                    return self._catalog_blocked(latest, catalog_failure)
                if not self._catalog_is_fetching(latest) or not fresh['ranking_complete']:
                    self._catalog_metadata(fresh, latest)
                    self._research_pool = fresh
                    self._research_head_signature = signature
                    self._research_until = time.monotonic() + (self.ttl if self._research_pool['ranking_complete'] else min(self.ttl, 60))
                    self._catalog_failure_signature, self._catalog_failure_reason, self._catalog_failure_until = None, None, 0
                head = latest
            # 元信息与最终选中项独立复制，不在每次读取时复制完整候选详情。
            result = deepcopy({k: v for k, v in self._research_pool.items() if k != 'products'})
            products = self._research_pool['products']
            self._catalog_metadata(result, head)
            if not result['ranking_complete']:
                result['coverage']['eligible_count'], result['products'] = 0, []
                return result
            eligible = []
            def is_stale(product):
                on = product['as_of']
                return not on or on > today.isoformat() or (today - date.fromisoformat(on)).days > self.stale_days
            for product in products:
                if product['metric'] and not is_stale(product):
                    eligible.append(product)
            eligible.sort(key=lambda p: (-Decimal(p['metric']['value_pct']), p['code']))
            result['coverage']['eligible_count'] = len(eligible)
            selected = [p for p in products if p['code'] == code] if code is not None else eligible[:3]
            result['products'] = deepcopy(selected)
            for product in result['products']:
                product['stale'] = is_stale(product)
            return result
