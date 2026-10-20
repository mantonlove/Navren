"""按请求份额读取管理人官方公开业绩；不修改销售目录或推算总收益。"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

PARSER_VERSION = 'official-performance-v2'
MAX_BYTES = 4 * 1024 * 1024
MAX_NAV = 400
PERIOD_LABELS = {'1m': '近一月涨跌幅', '3m': '近三月涨跌幅', '1y': '近一年涨跌幅'}
MANAGERS = {
    'chinaamc': ('华夏基金管理有限公司', 'www.chinaamc.com', '华夏'),
    'westlead': ('西部利得基金管理有限公司', 'www.westleadfund.com', '西部利得'),
    'jsfund': ('嘉实基金管理有限公司', 'www.jsfund.cn', '嘉实'),
}
NOTICE = ('业绩与净值来自基金管理人官方公开资料，单列于微众销售目录；'
          '历史涨跌幅不是预期或承诺收益，不代表微众渠道当前销售、费率或可交易状态。'
          '未披露周期保持未知，单位净值图不等于含分红再投资的收益曲线。')


class SourceError(ValueError):
    pass


class IdentityError(SourceError):
    pass


def _normal(value):
    return re.sub(r'[\s（）()·]', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _number(value, *, nav=False):
    """发布的百分数按百分数保留；缺失不转为零，不接受科学记数/非有限值。"""
    if isinstance(value, bool) or value is None:
        return None
    text = unicodedata.normalize('NFKC', str(value)).strip().replace('−', '-')
    text = text.removesuffix('%').strip()
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or (nav and number <= 0) or number == Decimal('-999999.99'):
        return None
    return format(number, 'f')


def _date(value, today):
    text = str(value or '').strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    return text if day.isoformat() == text and day <= today else None


def _period(value):
    text = _normal(value).replace('↓', '').replace('收益率', '').replace('涨跌幅', '')
    aliases = {'1m': {'过去一月', '近一月', '近1月', '过去1月'},
               '3m': {'过去一季', '过去三月', '近三月', '近3月', '过去3月'},
               '1y': {'过去一年', '近一年', '过去1年', '近1年'}}
    return next((p for p, labels in aliases.items() if text in labels), None)


def _identity(code, name, actual_code, names):
    if actual_code != code or _normal(name) not in {_normal(n) for n in names if n}:
        raise IdentityError('official_share_identity_mismatch')


def _navs(rows, today):
    unique = {}
    for on, value in rows:
        on, value = _date(on, today), _number(value, nav=True)
        if on is None or value is None:
            continue
        if on in unique and Decimal(unique[on]) != Decimal(value):
            raise SourceError('conflicting_unit_nav')
        unique[on] = value
    return [{'date': on, 'unit_nav': unique[on]} for on in sorted(unique)[-MAX_NAV:]]


def _metrics(rows, as_of):
    if not as_of:
        return []
    result = {}
    for label, value in rows:
        period, value = _period(label), _number(value)
        if period and value is not None:
            if period in result and Decimal(result[period]['value_pct']) != Decimal(value):
                raise SourceError('conflicting_period_return')
            result[period] = dict(period=period, label=PERIOD_LABELS[period], value_pct=value, as_of=as_of)
    return [result[p] for p in PERIOD_LABELS if p in result]


def _cells(row):
    if row is None:
        return []
    return [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'], recursive=False)]


def _chinaamc(body, code, name, today):
    soup = BeautifulSoup(body, 'html.parser')
    code_node, name_node = soup.select_one('#codetext'), soup.select_one('#nametext')
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', code_node.get_text() if code_node else '')
    _identity(code, name, match[1] if match else None,
              [name_node.get_text(' ', strip=True) if name_node else None])
    manager = next((node.select_one('.t2').get_text(strip=True) for node in soup.select('.item')
                    if node.select_one('.t1') and node.select_one('.t2')
                    and _normal(node.select_one('.t1').get_text()).rstrip(':：') == '基金管理人'), None)
    if manager != MANAGERS['chinaamc'][0]:
        raise IdentityError('official_manager_mismatch')
    table = soup.select_one('.table1')
    as_of = None
    if table:
        for parent in [table, *table.parents]:
            matches = re.findall(r'截止日期[：:\s]*(\d{4}-\d{2}-\d{2})', parent.get_text(' ', strip=True))
            if matches:
                as_of = _date(matches[0], today) if len(set(matches)) == 1 else None
                break
    pairs = []
    if table:
        for row in table.select('.tb .tr'):
            cells = [n.get_text(' ', strip=True) for n in row.select('.td')]
            if len(cells) == 2:
                pairs.append(cells)
    nav_rows = []
    for row in soup.select('.table2 .tb .tr'):
        cells = [n.get_text(' ', strip=True) for n in row.select('.td')]
        if len(cells) >= 2:
            nav_rows.append(cells[:2])
    return _metrics(pairs, as_of), _navs(nav_rows, today)


def _chinaamc_chart(body, today):
    data = json.loads(body, parse_float=Decimal)
    if not isinstance(data, dict):
        raise SourceError('invalid_nav_object')
    days, values = data.get('ShowData'), data.get('danweijingzhiName')
    if not isinstance(days, list) or not isinstance(values, list) or len(days) != len(values):
        raise SourceError('invalid_nav_arrays')
    return _navs(zip(days, values), today)


def _jsfund_list(body, code, name, today):
    soup = BeautifulSoup(body, 'html.parser')
    matches = []
    for row in soup.select('tr'):
        for link in row.select('a[href]'):
            url = urlsplit(urljoin('https://www.jsfund.cn', link['href']))
            params = parse_qs(url.query)
            if (url.hostname == MANAGERS['jsfund'][1]
                    and params.get('fundcode') == [code] and '/product/index.shtml' in url.path):
                matches.append((row, link.get_text(' ', strip=True)))
    if len(matches) != 1:
        raise IdentityError('official_share_not_uniquely_found')
    row, official_name = matches[0]
    _identity(code, name, code, [official_name])
    table = row.find_parent('table')
    if table is None:
        raise SourceError('official_table_missing')
    headers, values = _cells(table.select_one('tr')), _cells(row)
    if len(headers) != len(values):
        raise SourceError('official_table_columns_changed')
    pairs = list(zip(headers, values))
    as_of = next((_date(v, today) for k, v in pairs if _normal(k) == '截止日期'), None)
    nav = next((v for k, v in pairs if _normal(k) == '最新净值'), None)
    # 历史页有更精确单位净值；列表净值保留到历史页确实缺失时才使用。
    return _metrics(pairs, as_of), _navs([(as_of, nav)], today)


def _jsfund_nav(body, code, name, today):
    soup = BeautifulSoup(body, 'html.parser')
    hidden, selected = soup.select_one('input#FundCode'), soup.select('option[selected]')
    if not hidden or len(selected) != 1 or selected[0].get('value') != hidden.get('value'):
        raise IdentityError('official_nav_identity_missing')
    _identity(code, name, hidden.get('value'), [selected[0].get_text(' ', strip=True)])
    tables = [t for t in soup.select('table') if _cells(t.select_one('tr')) == ['日期', '份额净值', '累计净值']]
    if len(tables) != 1:
        raise SourceError('official_nav_table_changed')
    return _navs([_cells(row)[:2] for row in tables[0].select('tr')[1:] if len(_cells(row)) >= 2], today)


def _westlead(body, code, name, today):
    """解析官方已渲染的产品/净值表；空表不猜测动态 API 或收益。"""
    soup = BeautifulSoup(body, 'html.parser')
    plain = soup.get_text(' ', strip=True)
    match = re.search(r'基金代码[：:\s]*(\d{6})(?!\d)', plain)
    names = [n.get_text(' ', strip=True) for n in soup.select('h1')]
    if soup.title:
        names.append(re.split(r'\s+[-—|]\s+', soup.title.get_text(' ', strip=True))[0])
    _identity(code, name, match[1] if match else None, names)
    if not re.search(r'基金管理人[：:\s]*' + re.escape(MANAGERS['westlead'][0]), plain):
        raise IdentityError('official_manager_mismatch')
    pairs, nav_rows, as_of = [], [], None
    for table in soup.select('table'):
        rows = table.select('tr')
        if not rows:
            continue
        headers = _cells(rows[0])
        if '净值日期' not in headers or '单位净值' not in headers:
            continue
        for row in rows[1:]:
            values = _cells(row)
            if len(values) != len(headers):
                continue
            on = _date(values[headers.index('净值日期')], today)
            if on:
                nav_rows.append((on, values[headers.index('单位净值')]))
                if not as_of or on > as_of:
                    as_of, pairs = on, list(zip(headers, values))
    return _metrics(pairs, as_of), _navs(nav_rows, today)


def _westlead_api(body, code, today):
    """仅发布原网页净值表明确标为过去一年的 f1yearinc，按脚本 ×100。

    monthinc/seasoninc 暂不输出，避免未经日窗定义核验的本月/本季与滚动周期混用。
    """
    data = json.loads(body, parse_float=Decimal)
    if not isinstance(data, dict):
        raise SourceError('invalid_official_nav_object')
    rows = data.get('dataList')
    if (type(data.get('currentPage')) is not int or data['currentPage'] != 1
            or type(data.get('totalPage')) is not int or data['totalPage'] < 1
            or not isinstance(rows, list) or len(rows) > MAX_NAV):
        raise SourceError('invalid_official_nav_page')
    valid = []
    for row in rows:
        if not isinstance(row, dict) or row.get('fundcode') != code:
            raise IdentityError('official_nav_share_mismatch')
        if _date(row.get('date'), today) and _number(row.get('netvalue'), nav=True):
            valid.append(row)
    nav = _navs([(r['date'], r['netvalue']) for r in valid], today)
    if not valid:
        return [], nav
    latest = max(valid, key=lambda r: r['date'])
    pairs = []
    for field, label in [('f1yearinc', '近一年')]:
        value = _number(latest.get(field))
        if value is not None:
            pairs.append((label, format(Decimal(value) * 100, 'f')))
    return _metrics(pairs, latest['date']), nav


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target, original = urlsplit(newurl), urlsplit(req.full_url)
        if (target.scheme != 'https' or target.hostname != original.hostname
                or target.username or target.password or target.port not in (None, 443)):
            raise SourceError('unregistered_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProductPerformance:
    """共享实例支持并发请求合并，只有明确请求的产品触发官方读取。

    cache_dir 可用 NAVREN_PRODUCT_PERFORMANCE_CACHE 配置；内存 128 项，
    成功缓存默认 1 小时，失败重试间隔 1 分钟。净值最多保留 400 个真实观测。
    """
    def __init__(self, opener=None, cache_dir=None, *, ttl=3600, timeout=5,
                 stale_days=7, clock=None):
        self.opener = opener or build_opener(_OfficialRedirect()).open
        self.cache_dir = Path(cache_dir if cache_dir is not None else os.environ.get(
            'NAVREN_PRODUCT_PERFORMANCE_CACHE', Path(__file__).resolve().parent / 'outputs/product-performance/cache'))
        self.ttl, self.timeout, self.stale_days = max(0, ttl), max(0.1, min(timeout, 5)), stale_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._memory, self._locks, self._mutex = OrderedDict(), {}, threading.Lock()
        self._capture_local = threading.local()

    def _fetch(self, url, host, deadline, receipts):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != host or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            raise SourceError('unregistered_url')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('source_deadline')
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (Navren public fund disclosure)',
                                    'Accept': 'text/html,application/json', 'Referer': 'https://' + host + '/'})
        with self.opener(req, timeout=min(self.timeout, remaining)) as response:
            final = urlsplit(response.geturl()) if hasattr(response, 'geturl') else parsed
            if final.scheme != 'https' or final.hostname != host:
                raise SourceError('unregistered_response_origin')
            if getattr(response, 'status', 200) != 200:
                raise SourceError('source_http_failure')
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('source_deadline')
                # HTTPResponse.read1 每次只读当前可用缓冲/一次底层读取，避免慢流
                # 为凑足 read(n) 长期阻塞；SSL socket 使用当前剩余总预算。
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None:
                    sock.settimeout(min(self.timeout, remaining))
                read = getattr(response, 'read1', response.read)
                try:
                    chunk = read(min(65536, MAX_BYTES + 1 - len(body)))
                except HTTPException as error:
                    raise SourceError('incomplete_source_response') from error
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise SourceError('source_too_large')
        if not body:
            raise SourceError('empty_source')
        body = bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        receipts.append({'url': url, 'sha256': digest, 'bytes': len(body)})
        capture = getattr(self._capture_local, 'sources', None)
        if capture is not None:
            capture.append({'url': url, 'body': body, 'sha256': digest})
        return body

    def _collect(self, manager, code, name, today):
        company, host, _ = MANAGERS[manager]
        base, receipts = 'https://' + host, []
        deadline = time.monotonic() + 11
        partial = False
        if manager == 'chinaamc':
            url = f'{base}/fund/{code}/index.shtml'
            metrics, nav = _chinaamc(self._fetch(url, host, deadline, receipts), code, name, today)
            nav_url = f'{base}/fund/{code}/zoust_all.js'
            try:
                chart = _chinaamc_chart(self._fetch(nav_url, host, deadline, receipts), today)
                nav = _navs([(p['date'], p['unit_nav']) for p in nav + chart], today)
            except IdentityError:
                raise
            except (OSError, ValueError, TypeError, KeyError, HTTPException):
                partial = True
        elif manager == 'jsfund':
            url = base + '/Services/cn/jsp/product/DetailList.jsp'
            metrics, nav = _jsfund_list(self._fetch(url, host, deadline, receipts), code, name, today)
            nav_url = base + f'/Services/cn/jsp/product/NavHistory.jsp?FundCode={code}&SiteID=1'
            try:
                history = _jsfund_nav(self._fetch(nav_url, host, deadline, receipts), code, name, today)
                if history:
                    nav = history
                else:
                    partial = True
            except IdentityError:
                raise
            except (OSError, ValueError, TypeError, KeyError, HTTPException):
                partial = True
        else:
            # 目录首页用于按请求代码发现真实产品路径，避免猜基金类型目录。
            index = base + '/products/index.html'
            soup = BeautifulSoup(self._fetch(index, host, deadline, receipts), 'html.parser')
            urls = {urljoin(index, a['href']) for a in soup.select('a[href]')
                    if re.fullmatch(r'/products/[a-z]+/' + code + r'/index\.html', urlsplit(urljoin(index, a['href'])).path)
                    and urlsplit(urljoin(index, a['href'])).hostname == host}
            if len(urls) != 1:
                raise IdentityError('official_product_path_not_uniquely_found')
            url = urls.pop()
            page = self._fetch(url, host, deadline, receipts)
            metrics, nav = _westlead(page, code, name, today)
            nav_url = url
            # 仅调用经过原网页实际声明的入口；不执行网页 JavaScript。
            text = page.decode('utf-8')
            site_id = re.search(r'getFundNetTableJson[\s\S]{0,500}?siteId:\s*"([0-9a-f]{32})"', text)
            if '/common-web/chart/fundnettable/getFundNetTableJson' in text and site_id:
                nav_url = base + '/common-web/chart/fundnettable/getFundNetTableJson?' + urlencode(dict(
                    fundcode=code, **{'from': (today - timedelta(days=800)).isoformat(), 'to': today.isoformat()},
                    pages=f'1-{MAX_NAV}', siteId=site_id[1]))
                try:
                    api_metrics, api_nav = _westlead_api(self._fetch(nav_url, host, deadline, receipts), code, today)
                    metrics, nav = api_metrics or metrics, api_nav or nav
                except IdentityError:
                    raise
                except (OSError, ValueError, TypeError, KeyError, HTTPException):
                    partial = True
        if not metrics and not nav:
            raise SourceError('no_dated_official_values')
        dates = [m['as_of'] for m in metrics] or [p['date'] for p in nav]
        result = dict(product_code=code, product_name=name, status='ok', source_name=company + '官方公开资料',
                      source_url=url, as_of=max(dates), fetched_at=self.clock().isoformat(),
                      content_version=hashlib.sha256(json.dumps([PARSER_VERSION, receipts], sort_keys=True).encode()).hexdigest(),
                      stale=False, metrics=metrics, nav=nav, notice=NOTICE,
                      fetch_status='partial' if partial else 'ok', nav_source_url=nav_url,
                      source_receipts=receipts, parser_version=PARSER_VERSION,
                      missing_periods=[p for p in PERIOD_LABELS if p not in {m['period'] for m in metrics}])
        if partial:
            result['notice'] += '部分净值历史入口读取失败，仅展示其他已核验原文中的观测。'
        if metrics and nav and metrics[0]['as_of'] != nav[-1]['date']:
            result['notice'] += '业绩截止日与最新净值日不同，请分别读取各条记录日期。'
        return result

    def _disk(self, key, identity):
        path = self.cache_dir / (key + '.json')
        try:
            if path.stat().st_size > MAX_BYTES:
                return None
            data = json.loads(path.read_text(encoding='utf-8'))
            snapshot = data['snapshot']
            digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if (data.get('identity') != identity or data.get('parser_version') != PARSER_VERSION
                    or data.get('snapshot_sha256') != digest or snapshot.get('status') != 'ok'
                    or not snapshot.get('content_version') or not snapshot.get('source_receipts')):
                return None
            datetime.fromisoformat(snapshot['fetched_at'])
            if snapshot.get('product_code') != identity[1] or _normal(snapshot.get('product_name')) != identity[2]:
                return None
            return snapshot
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save(self, key, identity, snapshot):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        envelope = dict(identity=identity, parser_version=PARSER_VERSION, snapshot=snapshot,
                        snapshot_sha256=hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir, delete=False) as stream:
                path = Path(stream.name)
                json.dump(envelope, stream, ensure_ascii=False, sort_keys=True)
            os.replace(path, self.cache_dir / (key + '.json'))
        finally:
            if path and path.exists():
                path.unlink()

    def _expose(self, result, today, cached=False):
        result = deepcopy(result)
        dates = [m['as_of'] for m in result['metrics']] + ([result['nav'][-1]['date']] if result['nav'] else [])
        result['stale'] = bool(result['stale'] or not dates or any(
            (today - date.fromisoformat(on)).days > self.stale_days for on in dates))
        result['cache_hit'] = cached
        return result

    def read_with_sources(self, product: dict, *, force=False) -> dict:
        """本次实际抓取的原文；缓存命中不造原件，失败仍保留已取字节。

        sources 仅说明完整读取的响应，不表示解析/身份检查通过；须结合 result
        的 fetch_status 使用。线程隔离，且不把原件字节写进业绩缓存。
        """
        previous = getattr(self._capture_local, 'sources', None)
        sources = []
        self._capture_local.sources = sources
        try:
            return {'result': self.read(product, force=force), 'sources': sources}
        finally:
            self._capture_local.sources = previous

    def read(self, product: dict, *, force=False) -> dict:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError('clock_requires_timezone')
        today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
        code, name = product.get('code', product.get('product_code')), product.get('name', product.get('product_name'))
        issuer = product.get('publish') or product.get('manager') or product.get('issuer')
        manager = next((k for k, (company, _, prefix) in MANAGERS.items()
                        if (issuer and _normal(issuer) == _normal(company))
                        or (not issuer and isinstance(name, str) and name.startswith(prefix))), None)
        source_url = ({'chinaamc': f'https://www.chinaamc.com/fund/{code}/index.shtml',
                       'westlead': 'https://www.westleadfund.com/products/index.html',
                       'jsfund': 'https://www.jsfund.cn/Services/cn/jsp/product/DetailList.jsp'}.get(manager)
                      if isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code) else None)
        empty = dict(product_code=code, product_name=name, status='unsupported', source_name=None, source_url=source_url,
                     as_of=None, fetched_at=None, content_version=None, stale=True, metrics=[], nav=[],
                     notice=NOTICE, fetch_status='not_attempted')
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)
                or not isinstance(name, str) or not name.strip() or len(name) > 200 or not manager):
            return empty
        if not name.startswith(MANAGERS[manager][2]):
            return {**empty, 'status': 'unavailable', 'fetch_status': 'identity_mismatch',
                    'notice': NOTICE + '目录产品名与管理人身份不一致，停止关联业绩。'}
        identity = [manager, code, _normal(name)]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        with self._mutex:
            lock, count = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, count + 1)
        try:
            with lock:
                with self._mutex:
                    entry = self._memory.get(key)
                    if entry:
                        self._memory.move_to_end(key)
                if not force and entry and entry[0] > time.monotonic():
                    return self._expose(entry[1], today, cached=True)
                old = entry[1] if entry and entry[1]['status'] == 'ok' else self._disk(key, identity)
                fresh = False
                if old:
                    try:
                        age = (now - datetime.fromisoformat(old['fetched_at'])).total_seconds()
                        fresh = 0 <= age < self.ttl
                    except (ValueError, TypeError):
                        old = None
                if not force and fresh and old.get('fetch_status') != 'failed':
                    result, until, hit = old, time.monotonic() + self.ttl - age, True
                else:
                    hit = False
                    try:
                        result = self._collect(manager, code, name, today)
                        try:
                            self._save(key, identity, result)
                        except OSError:
                            result['cache_write_status'] = 'failed'
                        until = time.monotonic() + self.ttl
                    except IdentityError as error:
                        result = {**empty, 'status': 'unavailable', 'source_name': MANAGERS[manager][0] + '官方公开资料',
                                  'fetch_status': 'identity_mismatch', 'checked_at': now.isoformat(),
                                  'failure_reason': str(error), 'notice': NOTICE + '官方份额身份未匹配，未关联任何收益或净值。'}
                        until = time.monotonic() + 60
                    except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
                        result = deepcopy(old) if old else {**empty, 'status': 'unavailable',
                                                           'source_name': MANAGERS[manager][0] + '官方公开资料'}
                        result.update(stale=True, fetch_status='failed', checked_at=now.isoformat(),
                                      failure_reason=str(error) if isinstance(error, SourceError) else type(error).__name__)
                        if isinstance(getattr(error, 'code', None), int):
                            result['fetch_http_status'] = error.code
                        if callable(getattr(error, 'close', None)):
                            error.close()
                        result['notice'] = NOTICE + ('本次读取失败，保留上次核验的原日期数据。' if old else '官方公开资料本次无法读取，没有可用的已核验缓存。')
                        until = time.monotonic() + 60
                with self._mutex:
                    self._memory[key] = (until, deepcopy(result))
                    self._memory.move_to_end(key)
                    while len(self._memory) > 128:
                        self._memory.popitem(last=False)
                return self._expose(result, today, cached=hit)
        finally:
            with self._mutex:
                saved_lock, count = self._locks[key]
                if count == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (saved_lock, count - 1)


# 管理人公开货币研究池与微众销售快照分开；不推断渠道、适当性或交易权限。
MONEY_METRIC = 'seven_day_annualized'
MONEY_SOURCES = {
    'chinaamc': 'https://www.chinaamc.com/',
    'jsfund': 'https://www.jsfund.cn/Services/cn/jsp/product/DetailList.jsp',
    'westlead': 'https://www.westleadfund.com/',
}


def _money_row(manager, code, name, on, annualized, today, source_url, *, risk=None, provisional=False):
    company, host, prefix = MANAGERS[manager]
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code) or not isinstance(name, str) or not name.startswith(prefix) or len(name) > 200:
        raise IdentityError('official_money_share_identity_missing')
    on, value = _date(on, today), _number(annualized)
    if provisional:
        value = None
    return dict(code=code, name=name, manager=company, source_scope='official_money_market',
                type='货币基金', risk=risk if isinstance(risk, str) and risk.strip() else None,
                source_url=source_url, as_of=on, executable=False,
                metric=dict(type=MONEY_METRIC, value_pct=value, as_of=on) if value is not None and on else None,
                missing_reason='暂估收益不参与排序' if provisional else '七日年化或披露日期未获取' if value is None or not on else None)


def _unique_money(rows):
    seen = {}
    if len(rows) > 300:
        raise SourceError('money_pool_too_large')
    for row in rows:
        if row['code'] in seen and seen[row['code']] != row:
            raise IdentityError('conflicting_money_share')
        seen[row['code']] = row
    if not seen:
        raise SourceError('no_official_money_shares')
    return list(seen.values())


def _chinaamc_money(body, today):
    data = json.loads(body)
    if not isinstance(data, list):
        raise SourceError('invalid_official_money_groups')
    groups = [g for g in data if isinstance(g, dict) and str(g.get('type_id')) == '4']
    if len(groups) != 1 or not isinstance(groups[0].get('product_list'), list):
        raise SourceError('official_money_group_missing')
    rows = []
    for p in groups[0]['product_list']:
        if not isinstance(p, dict) or str(p.get('type_id')) != '4':
            raise IdentityError('non_money_share_in_money_group')
        rows.append(_money_row('chinaamc', p.get('fund_code'), p.get('fund_name'), p.get('publish_date'),
                    p.get('qirinianhua'), today, MONEY_SOURCES['chinaamc'], risk=p.get('risklevel'),
                    provisional='暂估' in str(p.get('fund_comment', ''))))
    return _unique_money(rows)


def _jsfund_money(body, today):
    soup = BeautifulSoup(body, 'html.parser')
    table = soup.select_one('table#huobi')
    if table is None:
        raise SourceError('official_money_table_missing')
    trs = table.select('tr')
    if not trs:
        raise SourceError('official_money_columns_changed')
    labels = [_normal(v).replace('↓', '') for v in _cells(trs[0])]
    needed = ['基金名称', '截止日期', '7日年化收益率']
    if any(labels.count(label) != 1 for label in needed):
        raise SourceError('official_money_columns_changed')
    rows = []
    for tr in trs[1:]:
        cells = _cells(tr)
        links = tr.select('a[href*="fundcode="]')
        if len(cells) != len(labels) or len(links) != 1:
            raise SourceError('official_money_row_changed')
        link = urlsplit(urljoin(MONEY_SOURCES['jsfund'], links[0]['href']))
        codes = parse_qs(link.query).get('fundcode', [])
        if link.scheme != 'https' or link.hostname != MANAGERS['jsfund'][1] or link.username or link.password or link.path != '/Services/cn/html/product/index.shtml' or len(codes) != 1:
            raise IdentityError('official_money_link_identity_missing')
        name = links[0].get_text(' ', strip=True)
        if name != cells[labels.index('基金名称')]:
            raise IdentityError('official_money_name_conflict')
        rows.append(_money_row('jsfund', codes[0], name, cells[labels.index('截止日期')],
                    cells[labels.index('7日年化收益率')], today, MONEY_SOURCES['jsfund']))
    return _unique_money(rows)


def _westlead_money(body, today):
    soup = BeautifulSoup(body, 'html.parser')
    rows = []
    for table in soup.select('table'):
        trs = table.select('tr')
        labels = [_normal(v).replace('↓', '') for v in _cells(trs[0])] if trs else []
        yield_cols = [i for i, label in enumerate(labels) if re.fullmatch(r'(?:7|七)日年化收益率(?:%)?', label)]
        date_cols = [i for i, label in enumerate(labels) if label in ('净值日期', '截止日期', '日期')]
        if len(yield_cols) != 1 or len(date_cols) != 1:
            continue
        for tr in trs[1:]:
            cells = _cells(tr)
            links = tr.select('a[href]')
            shares = []
            for a in links:
                link = urlsplit(urljoin(MONEY_SOURCES['westlead'], a['href']))
                match = re.fullmatch(r'/products/(?:currency|money|huobi)/(\d{6})/index\.html', link.path)
                if link.scheme == 'https' and link.hostname == MANAGERS['westlead'][1] and match:
                    shares.append((match[1], a.get_text(' ', strip=True)))
            if len(cells) != len(labels) or len(shares) != 1:
                raise SourceError('official_money_row_changed')
            code, name = shares[0]
            rows.append(_money_row('westlead', code, name, cells[date_cols[0]], cells[yield_cols[0]], today, MONEY_SOURCES['westlead']))
    return _unique_money(rows)


class PublicMoneyProducts(ProductPerformance):
    """完整官方货币列表的独立只读池；缺失、失败、暂估与过期均不参加排名。"""
    def __init__(self, opener=None, *, ttl=900, timeout=5, stale_days=3, clock=None):
        super().__init__(opener=opener, ttl=ttl, timeout=timeout, stale_days=stale_days, clock=clock)
        self._pool_lock, self._pool, self._pool_until = threading.Lock(), None, 0

    def _money_collect(self, manager, today):
        host = MANAGERS[manager][1]
        receipts, deadline = [], time.monotonic() + 11
        homepage = self._fetch(MONEY_SOURCES[manager], host, deadline, receipts)
        if manager == 'chinaamc':
            page = homepage.decode('utf-8')
            # 原网页明确把此个人货币列表的 qirinianhua 按原数值加百分号呈现。
            if ("$.getJSON('/v3/js/product_list.json'" not in page or "'4': '.hbterm'" not in page
                    or '七日年化收益率' not in page or 'formatFundRawPercent(fund.qirinianhua)' not in page):
                raise SourceError('official_money_field_binding_changed')
            body = self._fetch('https://' + host + '/v3/js/product_list.json', host, deadline, receipts)
            rows = _chinaamc_money(body, today)
        elif manager == 'jsfund':
            rows = _jsfund_money(homepage, today)
        else:
            rows = _westlead_money(homepage, today)
        version = hashlib.sha256(json.dumps(receipts, sort_keys=True).encode()).hexdigest()
        return [dict(row, content_version=version) for row in rows], receipts

    def read(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError('clock_requires_timezone')
        today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
        with self._pool_lock:
            if self._pool is not None and time.monotonic() < self._pool_until:
                result = deepcopy(self._pool)
            else:
                products, coverage, receipts = [], [], []
                def collect(manager):
                    try:
                        rows, proof = self._money_collect(manager, today)
                        return rows, dict(manager=manager, name=MANAGERS[manager][0], status='ok', count=len(rows), source_url=MONEY_SOURCES[manager]), proof
                    except (OSError, ValueError, TypeError, KeyError, HTTPException) as error:
                        status = dict(manager=manager, name=MANAGERS[manager][0], status='unavailable', count=0, source_url=MONEY_SOURCES[manager], failure_reason=str(error) if isinstance(error, SourceError) else type(error).__name__)
                        if callable(getattr(error, 'close', None)):
                            error.close()
                        return [], status, []
                with ThreadPoolExecutor(max_workers=3) as workers:
                    for rows, status, proof in workers.map(collect, MONEY_SOURCES):
                        products.extend(rows); coverage.append(status); receipts.extend(proof)
                content_version = hashlib.sha256(json.dumps(products, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                result = dict(status='ok' if all(c['status'] == 'ok' for c in coverage) else 'partial' if products else 'unavailable',
                              products=products, coverage=coverage, fetched_at=now.isoformat(), content_version=content_version,
                              metric_type=MONEY_METRIC, source_scope='official_money_market', executable=False, stale_after_days=self.stale_days,
                              scope='已接入管理人官网个人货币列表；仅已核验公开资料，不代表全市场或微众在售。', source_receipts=receipts)
                self._pool = deepcopy(result)
                self._pool_until = time.monotonic() + (self.ttl if result['status'] == 'ok' else min(self.ttl, 60))
            # 缓存跨午夜也按真实披露日期重新判旧；不存在用抓取时间替代 as_of。
            for p in result['products']:
                p['stale'] = not p['as_of'] or (today - date.fromisoformat(p['as_of'])).days > self.stale_days
            result['products'].sort(key=lambda p: (-(Decimal(p['metric']['value_pct']) if p['metric'] and not p['stale'] else Decimal('-Infinity')), p['code']))
            result['eligible_count'] = sum(bool(p['metric']) and not p['stale'] for p in result['products'])
            return result
