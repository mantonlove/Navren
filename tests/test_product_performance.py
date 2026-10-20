"""仅验证官方业绩模块的份额、日期、百分数与故障缓存边界；不联网。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.client import IncompleteRead
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from product_performance import (ProductPerformance, IdentityError, SourceError, _number,
                                 _chinaamc, _chinaamc_chart, _jsfund_list, _jsfund_nav,
                                 _westlead, _westlead_api, _OfficialRedirect,
                                 PublicMoneyProducts, MONEY_METRIC, MONEY_SOURCES,
                                 _chinaamc_money, _jsfund_money, _money_row)

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
TODAY = date(2026, 10, 8)
PRODUCT = dict(code='000001', name='华夏成长混合', publish='华夏基金管理有限公司')


def china_page(code='000001', name='华夏成长混合', cutoff='2026-09-30'):
    # 明确的合成页面，只用于故障边界；不作为真实基金业绩。
    return f'''<div id="codetext">基金代码：{code}</div><div id="nametext">{name}</div>
    <div class="item"><div class="t1">基金管理人：</div><div class="t2">华夏基金管理有限公司</div></div>
    <section><div class="table1"><div class="tb">
    <div class="tr"><div class="td">过去一月</div><div class="td">-7.28</div></div>
    <div class="tr"><div class="td">过去一季</div><div class="td">0.00</div></div>
    <div class="tr"><div class="td">过去一年</div><div class="td">--</div></div>
    </div></div><div class="table2"><div class="tb">
    <div class="tr"><div class="td">2026-10-08</div><div class="td">1.174</div><div class="td">3.747</div></div>
    </div></div><div>截止日期：{cutoff}</div></section>'''.encode()


def fixture_opener(calls, body=None):
    def open_(request, **kwargs):
        calls.append((request.full_url, kwargs))
        if request.full_url.endswith('zoust_all.js'):
            return io.BytesIO(b'{"ShowData":["2026-09-30"],"danweijingzhiName":[1.222]}')
        return io.BytesIO(body or china_page())
    return open_


def js_list(name='嘉实中证500ETF联接A', code='000008', value='-1.88%'):
    return f'''<table><tr><td>基金名称</td><td>截止日期</td><td>最新净值↓</td>
    <td>过去1年 收益率↓</td></tr><tr><td><a href="/Services/cn/html/product/index.shtml?fundcode={code}">{name}</a></td>
    <td>2026-09-30</td><td>2.2000</td><td>{value}</td></tr></table>'''.encode()


def js_nav(code='000008', name='嘉实中证500ETF联接A'):
    return f'''<input id="FundCode" value="{code}"><select><option selected value="{code}">{name}</option></select>
    <table><tr><td>日期</td><td>份额净值</td><td>累计净值</td></tr>
    <tr><td>2026-09-30</td><td>2.2025</td><td>2.2685</td></tr>
    <tr><td>2026-09-29</td><td>2.2035</td><td>2.2695</td></tr></table>'''.encode()


class ProductPerformanceTests(unittest.TestCase):
    def test_percent_preserves_negative_zero_and_missing(self):
        self.assertEqual(_number('−7.28%'), '-7.28')
        self.assertEqual(_number('0.00%'), '0.00')
        for value in (None, '', '--', 'NaN', 'Infinity', '1e3', False, '-999999.99'):
            self.assertIsNone(_number(value))
        self.assertIsNone(_number('0', nav=True))

    def test_china_date_is_performance_cutoff_not_latest_nav(self):
        metrics, nav = _chinaamc(china_page(), '000001', PRODUCT['name'], TODAY)
        self.assertEqual([m['period'] for m in metrics], ['1m', '3m'])
        self.assertEqual([m['value_pct'] for m in metrics], ['-7.28', '0.00'])
        self.assertTrue(all(m['as_of'] == '2026-09-30' for m in metrics))
        self.assertEqual(nav[-1]['date'], '2026-10-08')
        self.assertEqual(_chinaamc(china_page(cutoff='2999-01-01'), '000001', PRODUCT['name'], TODAY)[0], [])

    def test_china_does_not_accept_other_code_or_added_share_class(self):
        for code, name in [('000002', PRODUCT['name']), ('000001', '华夏成长混合C')]:
            with self.assertRaises(IdentityError):
                _chinaamc(china_page(), code, name, TODAY)

    def test_nav_array_shape_conflict_and_future(self):
        for body in [b'[]', b'null']:
            with self.assertRaises(SourceError):
                _chinaamc_chart(body, TODAY)
            with self.assertRaises(SourceError):
                _westlead_api(body, '000006', TODAY)
        with self.assertRaises(SourceError):
            _chinaamc_chart(b'{"ShowData":["2026-09-30"],"danweijingzhiName":[]}', TODAY)
        with self.assertRaises(SourceError):
            _chinaamc_chart(b'{"ShowData":["2026-09-30","2026-09-30"],"danweijingzhiName":[1.0,2.0]}', TODAY)
        self.assertEqual(_chinaamc_chart(b'{"ShowData":["2999-01-01"],"danweijingzhiName":[1.0]}', TODAY), [])

    def test_js_exact_code_and_class_only_disclosed_period(self):
        metrics, _ = _jsfund_list(js_list(), '000008', '嘉实中证500ETF联接A', TODAY)
        self.assertEqual([m['period'] for m in metrics], ['1y'])
        self.assertEqual(metrics[0]['value_pct'], '-1.88')
        self.assertEqual(_jsfund_list(js_list(value='0.00%'), '000008', '嘉实中证500ETF联接A', TODAY)[0][0]['value_pct'], '0.00')
        with self.assertRaises(IdentityError):
            _jsfund_list(js_list(), '000008', '嘉实中证500ETF联接C', TODAY)
        with self.assertRaises(IdentityError):
            _jsfund_nav(js_nav(code='070008'), '000008', '嘉实中证500ETF联接A', TODAY)
        self.assertEqual(_jsfund_nav(js_nav(), '000008', '嘉实中证500ETF联接A', TODAY)[-1]['unit_nav'], '2.2025')

    def test_empty_unrelated_table_does_not_drop_valid_js_history(self):
        body = b'<table></table>' + js_nav()
        self.assertEqual(len(_jsfund_nav(body, '000008', '嘉实中证500ETF联接A', TODAY)), 2)

    def test_truncated_http_stream_returns_unknown_not_server_exception(self):
        class BrokenResponse(io.BytesIO):
            def read(self, size=-1):
                raise IncompleteRead(b'<table>')
            read1 = read
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(lambda *a, **kw: BrokenResponse(), tmp, clock=lambda: NOW)
            result = service.read(PRODUCT)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['failure_reason'], 'incomplete_source_response')
            self.assertEqual(result['metrics'], [])

    def test_west_official_empty_dynamic_table_is_not_zero(self):
        page = '''<title>西部利得量化成长混合A - 西部利得基金管理有限公司</title>
        基金代码：000006 基金管理人：西部利得基金管理有限公司
        <table><tr><td>净值日期</td><td>单位净值</td><td>过去一年</td></tr></table>'''.encode()
        self.assertEqual(_westlead(page, '000006', '西部利得量化成长混合A', TODAY), ([], []))
        with self.assertRaises(IdentityError):
            _westlead(page, '000006', '西部利得量化成长混合C', TODAY)

    def test_west_ratio_conversion_only_confirmed_year_and_checks_share(self):
        source = dict(currentPage=1, totalPage=1, dataList=[dict(fundcode='000006', date='2026-09-30',
                      netvalue='2.9941', monthinc='-0.0104', seasoninc='0.0000', f1yearinc='0.2217')])
        metrics, nav = _westlead_api(json.dumps(source).encode(), '000006', TODAY)
        self.assertEqual([m['period'] for m in metrics], ['1y'])
        self.assertEqual([m['value_pct'] for m in metrics], ['22.1700'])
        self.assertEqual(nav[-1]['unit_nav'], '2.9941')
        with self.assertRaises(IdentityError):
            _westlead_api(json.dumps(source).encode(), '011228', TODAY)
        for value, expected in [('-0.0104', '-1.0400'), ('0.0000', '0.0000')]:
            source['dataList'][0]['f1yearinc'] = value
            self.assertEqual(_westlead_api(json.dumps(source).encode(), '000006', TODAY)[0][0]['value_pct'], expected)

    def test_memory_and_disk_cache_do_not_repeat_or_expose_mutable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            service = ProductPerformance(fixture_opener(calls), tmp, clock=lambda: NOW)
            first = service.read(PRODUCT)
            self.assertEqual(len(calls), 2)
            self.assertEqual(first['status'], 'ok')
            self.assertTrue(first['stale'])  # 抓取今天不改变 9 月 30 日业绩日期。
            first['metrics'][0]['value_pct'] = '9999'
            self.assertEqual(service.read(PRODUCT)['metrics'][0]['value_pct'], '-7.28')
            self.assertEqual(len(calls), 2)
            def forbidden(*args, **kwargs):
                raise AssertionError('fresh disk cache should prevent network')
            restart = ProductPerformance(forbidden, tmp, clock=lambda: NOW)
            self.assertTrue(restart.read(PRODUCT)['cache_hit'])
            self.assertTrue(all(0 < call[1]['timeout'] <= 8 for call in calls))

    def test_failed_refresh_preserves_success_date_and_version_marks_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(fixture_opener([]), tmp, ttl=0, clock=lambda: NOW)
            old = service.read(PRODUCT)
            service.opener = lambda *a, **kw: (_ for _ in ()).throw(OSError('offline'))
            result = service.read(PRODUCT)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['fetch_status'], 'failed')
            self.assertTrue(result['stale'])
            for field in ('fetched_at', 'content_version', 'as_of', 'metrics', 'nav'):
                self.assertEqual(result[field], old[field])

    def test_identity_failure_never_falls_back_to_old_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(fixture_opener([]), tmp, ttl=0, clock=lambda: NOW)
            service.read(PRODUCT)
            service.opener = fixture_opener([], china_page(name='华夏成长混合C'))
            result = service.read(PRODUCT)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['fetch_status'], 'identity_mismatch')
            self.assertEqual(result['metrics'], [])
            self.assertEqual(result['nav'], [])

    def test_cold_failure_and_unsupported_do_not_invent_values(self):
        def denied(request, **kwargs):
            raise HTTPError(request.full_url, 403, 'Forbidden', {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(denied, tmp, clock=lambda: NOW)
            result = service.read(dict(code='000006', name='西部利得量化成长混合A', publish='西部利得基金管理有限公司'))
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['fetch_http_status'], 403)
            self.assertEqual(result['metrics'], [])
            self.assertIsNone(result['fetched_at'])
            self.assertIsNotNone(result['checked_at'])
            self.assertEqual(service.read({**PRODUCT, 'publish': '其他管理人'})['status'], 'unsupported')
            self.assertEqual(service.read({**PRODUCT, 'code': '../000001'})['status'], 'unsupported')
            self.assertEqual(service.read({**PRODUCT, 'code': '０００００１'})['status'], 'unsupported')

    def test_same_share_parallel_requests_share_one_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            service = ProductPerformance(fixture_opener(calls), tmp, clock=lambda: NOW)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(service.read, [PRODUCT] * 4))
            self.assertEqual(len(calls), 2)
            self.assertEqual(len({r['content_version'] for r in results}), 1)

    def test_corrupt_disk_cache_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            ProductPerformance(fixture_opener([]), tmp, clock=lambda: NOW).read(PRODUCT)
            path = next(Path(tmp).glob('*.json'))
            data = json.loads(path.read_text())
            data['snapshot']['metrics'][0]['value_pct'] = '9999'
            path.write_text(json.dumps(data))
            def offline(*args, **kwargs):
                raise OSError('offline')
            result = ProductPerformance(offline, tmp, clock=lambda: NOW).read(PRODUCT)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['metrics'], [])

    def test_redirect_does_not_visit_other_domain_http_or_credentials(self):
        handler = _OfficialRedirect()
        request = Request('https://www.chinaamc.com/fund/000001/index.shtml')
        for target in ['http://www.chinaamc.com/a', 'https://example.com/a',
                       'https://www.jsfund.cn/a', 'https://user@www.chinaamc.com/a']:
            with self.assertRaises(SourceError):
                handler.redirect_request(request, None, 302, '', {}, target)

    def test_source_capture_has_exact_bytes_and_never_reconstructs_cached_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            service = ProductPerformance(fixture_opener(calls), tmp, clock=lambda: NOW)
            first = service.read_with_sources(PRODUCT)
            self.assertEqual(first['result']['status'], 'ok')
            self.assertEqual(first['sources'][0]['body'], china_page())
            self.assertEqual(len(first['sources']), 2)
            for source, receipt in zip(first['sources'], first['result']['source_receipts']):
                self.assertIsInstance(source['body'], bytes)
                self.assertEqual(source['url'], receipt['url'])
                self.assertEqual(source['sha256'], hashlib.sha256(source['body']).hexdigest())
                self.assertEqual(source['sha256'], receipt['sha256'])
            # Neither the memory cache nor the on-disk snapshot contains original bytes.
            first['sources'][0]['url'] = 'https://example.test/not-a-source'
            memory = service.read_with_sources(PRODUCT)
            self.assertTrue(memory['result']['cache_hit'])
            self.assertEqual(memory['sources'], [])
            def forbidden(*args, **kwargs):
                raise AssertionError('fresh disk cache should prevent network')
            disk = ProductPerformance(forbidden, tmp, clock=lambda: NOW).read_with_sources(PRODUCT)
            self.assertTrue(disk['result']['cache_hit'])
            self.assertEqual(disk['sources'], [])
            self.assertEqual(len(calls), 2)

    def test_force_refresh_bypasses_memory_and_disk_without_changing_default_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            service = ProductPerformance(fixture_opener(calls), tmp, clock=lambda: NOW)
            initial = service.read(PRODUCT)
            updated = china_page().replace(b'-7.28', b'1.25')
            service.opener = fixture_opener(calls, updated)
            self.assertEqual(service.read(PRODUCT)['content_version'], initial['content_version'])
            self.assertEqual(len(calls), 2)
            fresh = service.read_with_sources(PRODUCT, force=True)
            self.assertFalse(fresh['result']['cache_hit'])
            self.assertEqual(fresh['sources'][0]['body'], updated)
            self.assertEqual(fresh['result']['metrics'][0]['value_pct'], '1.25')
            self.assertNotEqual(fresh['result']['content_version'], initial['content_version'])
            self.assertEqual(len(calls), 4)
            # A fresh instance must also bypass the already-fresh disk snapshot.
            restart = ProductPerformance(fixture_opener(calls), tmp, clock=lambda: NOW)
            result = restart.read(PRODUCT, force=True)
            self.assertFalse(result['cache_hit'])
            self.assertEqual(result['metrics'][0]['value_pct'], '-7.28')
            self.assertEqual(len(calls), 6)

    def test_failed_force_has_no_old_source_bytes_and_can_explicitly_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(fixture_opener([]), tmp, clock=lambda: NOW)
            original = service.read(PRODUCT)
            attempts = []
            def offline(request, **kwargs):
                attempts.append(request.full_url)
                raise OSError('offline')
            service.opener = offline
            failure = service.read_with_sources(PRODUCT, force=True)
            self.assertEqual(failure['sources'], [])
            self.assertEqual(failure['result']['fetch_status'], 'failed')
            self.assertFalse(failure['result']['cache_hit'])
            for field in ('fetched_at', 'content_version', 'as_of', 'metrics', 'nav'):
                self.assertEqual(failure['result'][field], original[field])
            self.assertEqual(service.read_with_sources(PRODUCT)['sources'], [])
            self.assertEqual(len(attempts), 1)
            # force is an explicit recheck, so the negative retry cache cannot hide it.
            service.opener = fixture_opener([])
            retried = service.read_with_sources(PRODUCT, force=True)
            self.assertEqual(retried['result']['fetch_status'], 'ok')
            self.assertEqual(len(retried['sources']), 2)

    def test_partial_capture_keeps_only_successful_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            def partial(request, **kwargs):
                if request.full_url.endswith('zoust_all.js'):
                    raise OSError('history unavailable')
                return io.BytesIO(china_page())
            service = ProductPerformance(partial, tmp, clock=lambda: NOW)
            packet = service.read_with_sources(PRODUCT)
            self.assertEqual(packet['result']['status'], 'ok')
            self.assertEqual(packet['result']['fetch_status'], 'partial')
            self.assertEqual([s['body'] for s in packet['sources']], [china_page()])
            self.assertEqual(packet['result']['source_receipts'][0]['sha256'], packet['sources'][0]['sha256'])

    def test_identity_rejection_keeps_observed_page_but_not_old_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(fixture_opener([]), tmp, clock=lambda: NOW)
            service.read(PRODUCT)
            wrong = china_page(name='华夏成长混合C')
            service.opener = fixture_opener([], wrong)
            rejected = service.read_with_sources(PRODUCT, force=True)
            self.assertEqual(rejected['result']['fetch_status'], 'identity_mismatch')
            self.assertEqual(rejected['result']['metrics'], [])
            self.assertEqual(rejected['result']['nav'], [])
            self.assertEqual([s['body'] for s in rejected['sources']], [wrong])
            self.assertEqual(service.read_with_sources(PRODUCT)['sources'], [])

    def test_concurrent_different_shares_never_mix_captured_bytes(self):
        products = [PRODUCT, dict(code='000002', name='华夏测试混合C', publish=PRODUCT['publish'])]
        barrier = threading.Barrier(2)
        page_bodies = {p['code']: china_page(code=p['code'], name=p['name']) for p in products}
        chart_bodies = {p['code']: json.dumps(dict(ShowData=['2026-09-30'],
                      danweijingzhiName=['1.222' if p['code'] == '000001' else '2.222'])).encode() for p in products}
        def opener(request, **kwargs):
            code = re.search(r'/fund/([0-9]{6})/', request.full_url)[1]
            if request.full_url.endswith('index.shtml'):
                barrier.wait(timeout=5)
                return io.BytesIO(page_bodies[code])
            return io.BytesIO(chart_bodies[code])
        with tempfile.TemporaryDirectory() as tmp:
            service = ProductPerformance(opener, tmp, clock=lambda: NOW)
            with ThreadPoolExecutor(max_workers=2) as pool:
                packets = list(pool.map(service.read_with_sources, products))
            for product, packet in zip(products, packets):
                code = product['code']
                self.assertEqual(packet['result']['product_code'], code)
                self.assertEqual([s['body'] for s in packet['sources']], [page_bodies[code], chart_bodies[code]])
                self.assertTrue(all('/fund/' + code + '/' in s['url'] for s in packet['sources']))

    def test_parallel_same_share_only_fetch_owner_gets_original_bytes(self):
        start = threading.Barrier(4)
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            service = ProductPerformance(fixture_opener(calls), tmp, clock=lambda: NOW)
            def read(_):
                start.wait(timeout=5)
                return service.read_with_sources(PRODUCT)
            with ThreadPoolExecutor(max_workers=4) as pool:
                packets = list(pool.map(read, range(4)))
            self.assertEqual(len(calls), 2)
            self.assertEqual(sorted(len(p['sources']) for p in packets), [0, 0, 0, 2])
            self.assertEqual(sum(p['result']['cache_hit'] for p in packets), 3)

    def test_capture_rejects_incomplete_or_invalid_http_response_bytes(self):
        class Broken(io.BytesIO):
            def read1(self, size=-1):
                raise IncompleteRead(b'partial source')
        class Foreign(io.BytesIO):
            def geturl(self):
                return 'https://example.test/foreign'
        class NotOK(io.BytesIO):
            status = 503
        for response, reason in [(lambda: Broken(), 'incomplete_source_response'),
                                 (lambda: io.BytesIO(b''), 'empty_source'),
                                 (lambda: Foreign(china_page()), 'unregistered_response_origin'),
                                 (lambda: NotOK(china_page()), 'source_http_failure'),
                                 (lambda: io.BytesIO(b'x' * (4 * 1024 * 1024 + 1)), 'source_too_large')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                service = ProductPerformance(lambda *a, **kw: response(), tmp, clock=lambda: NOW)
                packet = service.read_with_sources(PRODUCT)
                self.assertEqual(packet['sources'], [])
                self.assertEqual(packet['result']['failure_reason'], reason)

    def test_capture_scope_is_restored_after_unexpected_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            def broken(request, **kwargs):
                if request.full_url.endswith('zoust_all.js'):
                    raise RuntimeError('fixture unexpected failure')
                return io.BytesIO(china_page())
            service = ProductPerformance(broken, tmp, clock=lambda: NOW)
            with self.assertRaisesRegex(RuntimeError, 'fixture unexpected failure'):
                service.read_with_sources(PRODUCT)
            service.opener = fixture_opener([])
            packet = service.read_with_sources(PRODUCT)
            self.assertEqual(len(packet['sources']), 2)
            self.assertEqual(packet['sources'][0]['body'], china_page())


def money_json(rows):
    return json.dumps([dict(type_id='4', product_list=rows)], ensure_ascii=False).encode()


def china_money(code='000001', value='1.270', on='2026-10-08', **changes):
    return dict(type_id='4', fund_code=code, fund_name='华夏货币' + code + 'A',
                publish_date=on, qirinianhua=value, y1='999', **changes)


def money_table(code='000581', name='嘉实活钱包A', value='1.200%', on='2026-10-08', label='7日年化收益率'):
    return f'''<table id="huobi"><tr><td>基金名称</td><td>截止日期</td><td>{label}</td></tr>
    <tr><td><a href="/Services/cn/html/product/index.shtml?fundcode={code}">{name}</a></td>
    <td>{on}</td><td>{value}</td></tr></table>'''.encode()


def money_opener(calls, china=None, js=None):
    homepage = "$.getJSON('/v3/js/product_list.json' '4': '.hbterm' 七日年化收益率 formatFundRawPercent(fund.qirinianhua)".encode()
    def open_(request, **kwargs):
        url = request.full_url
        calls.append(url)
        if url == MONEY_SOURCES['chinaamc']:
            return io.BytesIO(homepage)
        if url.endswith('/v3/js/product_list.json'):
            return io.BytesIO(china if china is not None else money_json([china_money()]))
        if url == MONEY_SOURCES['jsfund']:
            return io.BytesIO(js if js is not None else money_table())
        raise HTTPError(url, 403, 'Forbidden', {}, io.BytesIO())
    return open_


class PublicMoneyProductsTests(unittest.TestCase):
    def test_explicit_annualized_value_share_and_missing_are_preserved(self):
        rows = _chinaamc_money(money_json([
            china_money(risklevel='低风险'),
            china_money('000002', '9.999', fund_comment='暂估7日年化'),
            china_money('000003', '--'),
            china_money('000004', '2.00', '2999-01-01'),
        ]), TODAY)
        self.assertEqual(rows[0]['metric'], dict(type=MONEY_METRIC, value_pct='1.270', as_of='2026-10-08'))
        self.assertEqual(rows[0]['code'], '000001')
        self.assertEqual(rows[0]['name'], '华夏货币000001A')
        self.assertEqual(rows[0]['risk'], '低风险')
        self.assertTrue(all(not p['executable'] for p in rows))
        self.assertTrue(all(p['metric'] is None for p in rows[1:]))
        self.assertIsNone(rows[1]['risk'])
        self.assertIn('暂估', rows[1]['missing_reason'])

    def test_official_money_table_accepts_short_name_and_not_period_return(self):
        row = _jsfund_money(money_table(), TODAY)[0]
        self.assertEqual(row['name'], '嘉实活钱包A')
        self.assertEqual(row['metric']['value_pct'], '1.200')
        self.assertIsNone(row['risk'])
        for body in [b'<table id="huobi"></table>', money_table(label='7日涨跌幅'), b'<table id="licai"></table>']:
            with self.assertRaises(SourceError):
                _jsfund_money(body, TODAY)

    def test_identity_conflict_foreign_manager_and_non_ascii_code_fail_closed(self):
        a = china_money()
        b = {**a, 'fund_name': '华夏货币000001B'}
        with self.assertRaises(IdentityError):
            _chinaamc_money(money_json([a, b]), TODAY)
        for code, name in [('０００００１', '华夏货币A'), ('000001', '嘉实货币A')]:
            with self.assertRaises(IdentityError):
                _money_row('chinaamc', code, name, '2026-10-08', '1.00', TODAY, MONEY_SOURCES['chinaamc'])

    def test_entire_pool_sorted_before_preview_missing_provisional_stale_excluded(self):
        candidates = [china_money('000001', '.1'), china_money('000002', '0.2'),
                      china_money('000003', '0.3'), china_money('000004', '8.00'),
                      china_money('000005', '7.00'), china_money('000006', '6.00'),
                      china_money('000007', '99.00', '2026-10-03'),
                      china_money('000008', '99.00', fund_comment='暂估7日年化'),
                      china_money('000009', '--')]
        calls = []
        data = PublicMoneyProducts(money_opener(calls, money_json(candidates)), clock=lambda: NOW).read()
        self.assertEqual(data['status'], 'partial')
        self.assertEqual(len(data['products']), 10)
        self.assertEqual([p['code'] for p in data['products'][:3]], ['000004', '000005', '000006'])
        self.assertEqual(data['eligible_count'], 6)
        excluded = {p['code']: p for p in data['products']}
        self.assertTrue(excluded['000007']['stale'])
        self.assertIsNone(excluded['000008']['metric'])
        self.assertIsNone(excluded['000009']['metric'])
        self.assertEqual(len(data['content_version']), 64)
        self.assertEqual(data['stale_after_days'], 3)

    def test_empty_provider_preserves_other_provider_and_declares_partial_coverage(self):
        calls = []
        data = PublicMoneyProducts(money_opener(calls, js=b'<table id="huobi"></table>'), clock=lambda: NOW).read()
        self.assertEqual(data['status'], 'partial')
        self.assertEqual(data['eligible_count'], 1)
        self.assertEqual([p['code'] for p in data['products']], ['000001'])
        coverage = {c['manager']: c for c in data['coverage']}
        self.assertEqual(coverage['chinaamc']['status'], 'ok')
        self.assertEqual(coverage['jsfund']['failure_reason'], 'official_money_columns_changed')
        self.assertEqual(coverage['westlead']['status'], 'unavailable')

    def test_cached_pool_recomputes_age_without_replacing_disclosure_date(self):
        calls, clock = [], [NOW]
        service = PublicMoneyProducts(money_opener(calls), clock=lambda: clock[0])
        first = service.read()
        clock[0] = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        second = service.read()
        self.assertEqual(len(calls), 4)
        self.assertEqual(first['eligible_count'], 2)
        self.assertEqual(second['eligible_count'], 0)
        self.assertTrue(all(p['stale'] for p in second['products']))
        self.assertTrue(all(p['as_of'] == '2026-10-08' for p in second['products']))
        self.assertEqual(first['fetched_at'], second['fetched_at'])

    def test_parallel_readers_share_one_complete_pool_fetch(self):
        calls = []
        service = PublicMoneyProducts(money_opener(calls), clock=lambda: NOW)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: service.read(), range(4)))
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({r['content_version'] for r in results}), 1)
        self.assertTrue(all(r['eligible_count'] == 2 for r in results))


if __name__ == '__main__':
    unittest.main()
