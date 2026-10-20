"""全类别候选与完整公开周收益交集的故障边界；合成夹具，禁止联网。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import io
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from finance_gateway import FinanceGateway
from product_performance import SourceError
from product_research import PublicProductResearch, RANK_URL, METRIC_TYPE, _binding, _rank_feed
from simulated_bank import BankError

NOW = datetime(2026, 10, 9, 2, tzinfo=timezone.utc)
TODAY = date(2026, 10, 9)
VERSION = 'a' * 64
SCRIPT_URL = 'https://j5.dfcfw.com/sc/js/data/jskrank_min_20220722.js'
PAGE = '''<p>数据来源：东方财富Choice数据</p><th col="zzf">近1周</th>
<script>var defaults = {src:"rankhandler.aspx",op:"ph",dt:"kf",ft:"all"};</script>
<script src="//j5.dfcfw.com/sc/js/data/jskrank_min_20220722.js"></script>'''
SCRIPT = '''this.dbObj.datas[i-1].split(","); "&sc="+defaults.sc; "&pn="+defaults.pn;
this.styleDateFormat(arrdata[3]); this.styleFormat(arrdata[4],4,false); this.styleFormat(arrdata[5],4,false);
var _td_zzf=document.createElement("td");_td_zzf.innerHTML=this.styleFormat(arrdata[7],2,true);'''


def candidate(code, name=None, kind='开放式基金', **changes):
    return dict(code=code, name=name or '产品' + code, type=kind, risk='R5高风险', publish='目录披露机构',
                source_url='https://www.webank.com/product/query/' + code, **changes)


def rank(code, value, name=None, on='2026-10-08', note=''):
    cells = [''] * 25
    cells[0:2] = [code, name or '产品' + code]
    cells[3:8] = [on, '1.0001', '1.1001', '0.01', value]
    cells[24] = note
    return ','.join(cells)


def feed(rows, **meta_changes):
    meta = dict(allRecords=len(rows), pageIndex=1, pageNum=30000, allPages=1, allNum=len(rows))
    meta.update(meta_changes)
    tail = ','.join(f'{k}:{v}' for k, v in meta.items())
    return ('var rankData = {datas:' + json.dumps(rows, ensure_ascii=False) + ',' + tail + '};').encode()


class Catalog:
    def __init__(self, rows):
        self.rows, self.calls, self.failed, self.version = rows, [], False, VERSION
        self.change_version_at = None
        self.source_date, self.catalog_status = '2026-09-24', 'historical'
        self.sync = dict(checked_at='2026-10-09T02:00:00+00:00', last_success_at='2026-10-09T02:00:00+00:00',
                         next_check_at='2026-10-09T02:05:00+00:00', refresh_interval_seconds=300,
                         fetch_status='ok', source_date=self.source_date, background_enabled=True)
        self.after_read = None
        self.outer_status = None

    def read(self, query='', limit=500, offset=0):
        self.calls.append((query, limit, offset))
        result = dict(status=self.catalog_status, fetch_status='failed' if self.failed else self.outer_status or self.sync['fetch_status'],
                    catalog_version='b' * 64 if offset == self.change_version_at else self.version,
                    source_date=self.source_date, source_url='https://www.webank.com/inquiryCenter/query',
                    catalog_sync=dict(self.sync), stale=self.catalog_status == 'historical', total=len(self.rows), matched=len(self.rows),
                    offset=offset, limit=limit, has_more=offset + limit < len(self.rows),
                    products=self.rows[offset:offset + limit])
        if self.after_read:
            self.after_read(limit, offset)
        return result


class Reader:
    def __init__(self, body):
        self.body, self.calls, self.failed, self.page, self.script = body, [], False, PAGE, SCRIPT

    def __call__(self, request, **kwargs):
        url = request.full_url
        self.calls.append(url)
        if self.failed:
            raise OSError('offline')
        if url == RANK_URL:
            return io.BytesIO(self.page.encode())
        if url == SCRIPT_URL:
            return io.BytesIO(self.script.encode())
        if url.startswith('https://fund.eastmoney.com/data/rankhandler.aspx?'):
            return io.BytesIO(self.body)
        raise AssertionError('unexpected URL: ' + url)


def service(candidates, rows, **kwargs):
    catalog, reader = Catalog(candidates), Reader(feed(rows))
    return PublicProductResearch(catalog, reader, clock=lambda: NOW, **kwargs), catalog, reader


class ProductResearchTests(unittest.TestCase):
    def test_complete_candidate_pool_then_intersection_then_sort_not_catalog_first_three(self):
        rows = [candidate('000001'), candidate('000002'), candidate('000003'),
                candidate('000004', kind='理财产品'), candidate('000005', kind='集合资产管理计划'),
                candidate('000006', kind='其他'), candidate('bank-seven', kind='保险')]
        source = [rank('999999', '99'), rank('000006', '8'), rank('000005', '7'), rank('000004', '6'),
                  rank('000003', '0.3'), rank('000002', '0.2'), rank('000001', '0.1')]
        s, catalog, reader = service(rows, source)
        result = s.read()
        self.assertTrue(result['ranking_complete'])
        self.assertEqual([p['code'] for p in result['products']], ['000006', '000005', '000004'])
        self.assertEqual(result['coverage'], dict(candidate_count=7, type_counts={'开放式基金': 3, '理财产品': 1,
                         '集合资产管理计划': 1, '其他': 1, '保险': 1}, source_count=7, matched_count=6, eligible_count=6))
        self.assertEqual(len(reader.calls), 3)
        self.assertEqual(catalog.calls, [('', 1, 0), ('', 500, 0), ('', 1, 0)])
        self.assertTrue(result['catalog_stale'])
        self.assertTrue(all(p['metric']['type'] == METRIC_TYPE and not p['executable'] for p in result['products']))
        self.assertTrue(all(p['source_url'] == 'https://fund.eastmoney.com/' + p['code'] + '.html' for p in result['products']))
        self.assertIn('Choice', result['provider'])

    def test_strict_same_share_name_unknown_products_and_code_detail(self):
        s, _, _ = service([candidate('000001', '基金A'), candidate('000002', '基金C'),
                           candidate('000003', '其他资料', kind='理财产品')],
                          [rank('000001', '8', '基金C'), rank('000002', '7', '基金 C')])
        result = s.read()
        self.assertEqual([p['code'] for p in result['products']], ['000002'])
        self.assertEqual(result['coverage']['matched_count'], 1)
        self.assertEqual(result['coverage']['eligible_count'], 1)
        mismatch = s.read('000001')['products'][0]
        self.assertIsNone(mismatch['metric'])
        self.assertIn('不一致', mismatch['raw']['missing_reason'])
        other = s.read('000003')['products'][0]
        self.assertIsNone(other['metric'])
        self.assertIsNone(other['source_url'])
        self.assertTrue(other['raw']['catalog_source_url'].endswith('000003'))
        self.assertEqual(s.read('123456')['products'], [])

    def test_negative_zero_stale_provisional_missing_and_future_date(self):
        codes = ['000001', '000002', '000003', '000004', '000005', '000006', '000007']
        s, _, _ = service([candidate(c) for c in codes],
                          [rank('000001', '90', on='2026-10-01'), rank('000002', '80', note='暂估'),
                           rank('000003', '70', on='2999-01-01'), rank('000004', '1.00', on='2026-10-02'),
                           rank('000005', '0.00'), rank('000006', '-2.50'), rank('000007', '--')])
        result = s.read()
        self.assertEqual([p['code'] for p in result['products']], ['000004', '000005', '000006'])
        self.assertEqual([p['metric']['value_pct'] for p in result['products']], ['1.00', '0.00', '-2.50'])
        self.assertEqual(result['coverage']['eligible_count'], 3)
        self.assertTrue(s.read('000001')['products'][0]['stale'])
        for code in ('000002', '000003', '000007'):
            self.assertIsNone(s.read(code)['products'][0]['metric'])

    def test_feed_rejects_incomplete_pagination_duplicates_reverse_order_and_wrong_columns(self):
        valid = [rank('000001', '3'), rank('000002', '2')]
        cases = [feed(valid, allRecords=3), feed(valid, pageIndex=2), feed(valid, pageNum=50),
                 feed(valid, allPages=2), feed(valid, allNum=3), feed([valid[0], valid[0]]),
                 feed(list(reversed(valid))), feed([valid[0] + ',extra']), b'alert(1);',
                 feed([rank('０００００１', '3')])]
        for body in cases:
            with self.subTest(body=body[:80]):
                with self.assertRaises((SourceError, ValueError)):
                    _rank_feed(body, TODAY)

    def test_real_page_and_script_binding_is_required_before_feed_read(self):
        self.assertEqual(_binding(PAGE, SCRIPT), SCRIPT_URL)
        for page, script in [(PAGE.replace('近1周', '近1年'), SCRIPT),
                             (PAGE.replace('Choice', '来源'), SCRIPT),
                             (PAGE.replace('rankhandler.aspx', 'other.aspx'), SCRIPT),
                             (PAGE, SCRIPT.replace('arrdata[7]', 'arrdata[11]')),
                             (PAGE, SCRIPT.replace('"&sc="+defaults.sc', 'other'))]:
            with self.assertRaises(SourceError):
                _binding(page, script)
        s, _, reader = service([candidate('000001')], [rank('000001', '1')])
        reader.script = SCRIPT.replace('arrdata[7]', 'arrdata[11]')
        result = s.read()
        self.assertFalse(result['ranking_complete'])
        self.assertEqual(result['products'], [])
        self.assertEqual(len(reader.calls), 2)

    def test_entire_catalog_pagination_and_version_change_fail_closed(self):
        candidates = [candidate(f'{n:06d}') for n in range(601)]
        s, catalog, _ = service(candidates, [rank('000600', '4')])
        self.assertEqual(s.read()['products'][0]['code'], '000600')
        self.assertEqual(catalog.calls, [('', 1, 0), ('', 500, 0), ('', 500, 500), ('', 1, 0)])
        s, catalog, _ = service(candidates, [rank('000600', '4')])
        catalog.change_version_at = 500
        result = s.read()
        self.assertFalse(result['ranking_complete'])
        self.assertEqual(result['products'], [])

    def test_source_or_catalog_failure_does_not_publish_old_ranking(self):
        for part in ('source', 'catalog'):
            s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
            self.assertEqual(len(s.read()['products']), 1)
            s._research_until = 0
            if part == 'source':
                reader.failed = True
            else:
                catalog.failed = True
            new = s.read()
            self.assertFalse(new['ranking_complete'])
            self.assertEqual(new['status'], 'unavailable')
            self.assertEqual(new['products'], [])
            self.assertEqual(new['coverage']['eligible_count'], 0)

    def test_cached_ranking_rechecks_age_across_midnight_and_preserves_original_date(self):
        s, _, reader = service([candidate('000001')], [rank('000001', '1.5', on='2026-10-02')])
        first = s.read()
        s.clock = lambda: datetime(2026, 10, 10, 0, tzinfo=timezone.utc)
        new = s.read()
        self.assertTrue(new['ranking_complete'])
        self.assertEqual(new['products'], [])
        self.assertEqual(new['coverage']['eligible_count'], 0)
        detail = s.read('000001')['products'][0]
        self.assertTrue(detail['stale'])
        self.assertEqual(detail['as_of'], '2026-10-02')
        self.assertEqual(first['fetched_at'], new['fetched_at'])
        self.assertEqual(len(reader.calls), 3)

    def test_parallel_readers_share_one_complete_source_request(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: s.read(), range(4)))
        self.assertEqual(len(reader.calls), 3)
        self.assertEqual(sum(limit == 500 for _, limit, _ in catalog.calls), 1)
        self.assertEqual(sum(limit == 1 for _, limit, _ in catalog.calls), 5)
        self.assertEqual(len({r['content_version'] for r in results}), 1)

    def test_cached_read_refreshes_catalog_metadata_without_relabeling_returns(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        first = s.read()
        catalog.source_date, catalog.catalog_status = '2026-10-08', 'current_disclosure'
        catalog.sync.update(checked_at='2026-10-09T02:03:00+00:00', next_check_at='2026-10-09T02:08:00+00:00',
                            source_date=catalog.source_date, fetch_status='unchanged')
        s.clock = lambda: datetime(2026, 10, 9, 2, 3, tzinfo=timezone.utc)
        new = s.read()
        self.assertTrue(new['ranking_complete'])
        self.assertEqual(new['catalog_sync'], catalog.sync)
        self.assertEqual(new['catalog_source_date'], '2026-10-08')
        self.assertEqual(new['catalog_status'], 'current_disclosure')
        self.assertEqual(new['catalog_fetch_status'], 'unchanged')
        self.assertEqual(new['catalog_source_url'], 'https://www.webank.com/inquiryCenter/query')
        self.assertEqual(new['products'][0]['as_of'], '2026-10-08')
        self.assertEqual(new['products'][0]['metric'], first['products'][0]['metric'])
        self.assertEqual(new['fetched_at'], first['fetched_at'])
        self.assertEqual(new['content_version'], first['content_version'])
        self.assertEqual(len(reader.calls), 3)
        self.assertEqual(catalog.calls[-1], ('', 1, 0))

    def test_new_catalog_version_invalidates_research_cache_before_ttl(self):
        s, catalog, reader = service([candidate('000001'), candidate('000002')],
                                    [rank('000001', '9'), rank('000002', '8')])
        first = s.read()
        catalog.rows, catalog.version = [candidate('000002')], 'b' * 64
        new = s.read()
        self.assertTrue(new['ranking_complete'])
        self.assertEqual(new['catalog_version'], 'b' * 64)
        self.assertEqual([p['code'] for p in new['products']], ['000002'])
        self.assertEqual(new['coverage']['candidate_count'], 1)
        self.assertNotEqual(new['content_version'], first['content_version'])
        self.assertEqual(len(reader.calls), 6)

    def test_latest_head_failure_blocks_cached_ranking_and_details_without_source_reads(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        first = s.read()
        pool = s._research_pool
        catalog.failed = True
        catalog.sync.update(fetch_status='failed', checked_at='2026-10-09T02:04:00+00:00')
        for code in (None, '000001'):
            blocked = s.read(code)
            self.assertFalse(blocked['ranking_complete'])
            self.assertEqual(blocked['products'], [])
            self.assertEqual(blocked['catalog_fetch_status'], 'failed')
            self.assertEqual(blocked['catalog_sync']['checked_at'], catalog.sync['checked_at'])
        self.assertIs(s._research_pool, pool)
        self.assertEqual(len(reader.calls), 3)
        catalog.failed = False
        catalog.sync['fetch_status'] = 'unchanged'
        recovered = s.read()
        self.assertTrue(recovered['ranking_complete'])
        self.assertEqual(recovered['fetched_at'], first['fetched_at'])
        self.assertEqual(len(reader.calls), 3)

    def test_nested_sync_failure_cannot_be_hidden_by_compatible_outer_ok(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        s.read()
        catalog.sync['fetch_status'] = 'failed'
        catalog.outer_status = 'ok'
        blocked = s.read()
        self.assertFalse(blocked['ranking_complete'])
        self.assertEqual(blocked['products'], [])
        self.assertEqual(blocked['catalog_fetch_status'], 'ok')
        self.assertEqual(blocked['catalog_sync']['fetch_status'], 'failed')
        self.assertEqual(len(reader.calls), 3)

    def test_head_version_change_during_collection_cannot_publish_old_candidates(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        def update_after_page(limit, offset):
            if limit == 500:
                catalog.version = 'b' * 64
        catalog.after_read = update_after_page
        blocked = s.read()
        self.assertFalse(blocked['ranking_complete'])
        self.assertEqual(blocked['products'], [])
        self.assertEqual(blocked['catalog_version'], 'b' * 64)
        self.assertEqual(blocked['failure_reason'], 'candidate_catalog_changed_during_research')
        self.assertIsNone(s._research_pool)
        catalog.after_read = None
        self.assertTrue(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 6)

    def test_fetching_reuses_only_verified_same_version_pool_and_original_returns(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        first = s.read()
        catalog.sync.update(fetch_status='fetching', checked_at='2026-10-09T02:01:00+00:00')
        s._research_until = 0
        s.clock = lambda: datetime(2026, 10, 9, 2, 1, tzinfo=timezone.utc)
        new = s.read()
        self.assertTrue(new['ranking_complete'])
        self.assertEqual(new['catalog_fetch_status'], 'fetching')
        self.assertEqual(new['catalog_sync'], catalog.sync)
        self.assertEqual(new['products'], first['products'])
        self.assertEqual(new['fetched_at'], first['fetched_at'])
        self.assertEqual(new['content_version'], first['content_version'])
        self.assertEqual(len(reader.calls), 3)
        catalog.sync['fetch_status'] = 'unchanged'
        self.assertTrue(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 6)

    def test_fetching_without_pool_or_with_changed_version_is_not_a_complete_ranking(self):
        for mode in ('cold', 'version', 'structure', 'incomplete'):
            with self.subTest(mode=mode):
                s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
                if mode != 'cold':
                    s.read()
                if mode == 'version':
                    catalog.version = 'b' * 64
                elif mode == 'structure':
                    catalog.rows = [candidate('000002')]
                elif mode == 'incomplete':
                    s._research_pool['ranking_complete'] = False
                catalog.sync['fetch_status'] = 'fetching'
                expected_reads = len(reader.calls)
                blocked = s.read()
                self.assertFalse(blocked['ranking_complete'])
                self.assertEqual(blocked['products'], [])
                self.assertEqual(blocked['catalog_fetch_status'], 'fetching')
                self.assertEqual(len(reader.calls), expected_reads)

    def test_fetching_requires_valid_success_timestamp_and_matching_snapshot_date(self):
        for success, source_date in [(None, '2026-09-24'), ('invalid', '2026-09-24'),
                                     ('2026-10-09T02:00:00', '2026-09-24'),
                                     ('2026-10-09T02:00:01+00:00', '2026-09-24'),
                                     ('2026-10-09T02:00:00+00:00', '2026-10-08')]:
            with self.subTest(success=success, source_date=source_date):
                s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
                s.read()
                catalog.sync.update(fetch_status='fetching', last_success_at=success, source_date=source_date)
                blocked = s.read()
                self.assertFalse(blocked['ranking_complete'])
                self.assertEqual(blocked['products'], [])
                self.assertEqual(len(reader.calls), 3)

    def test_fetching_at_post_collection_head_keeps_old_verified_pool(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        first = s.read()
        s._research_until = 0
        reader.body = feed([rank('000001', '2')])
        def fetching_after_page(limit, offset):
            if limit == 500:
                catalog.sync['fetch_status'] = 'fetching'
        catalog.after_read = fetching_after_page
        retained = s.read()
        self.assertTrue(retained['ranking_complete'])
        self.assertEqual(retained['catalog_fetch_status'], 'fetching')
        self.assertEqual(retained['products'], first['products'])
        self.assertEqual(retained['fetched_at'], first['fetched_at'])
        self.assertEqual(len(reader.calls), 6)

    def test_unknown_or_malformed_latest_head_never_publishes_cached_pool(self):
        for mode in ('unknown', 'rows', 'timestamp'):
            with self.subTest(mode=mode):
                s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
                s.read()
                if mode == 'unknown':
                    catalog.sync['fetch_status'] = 'not_checked'
                elif mode == 'rows':
                    catalog.rows = []
                else:
                    catalog.source_date = 'not-a-date'
                blocked = s.read()
                self.assertFalse(blocked['ranking_complete'])
                self.assertEqual(blocked['products'], [])
                self.assertEqual(len(reader.calls), 3)

    def test_full_catalog_pages_must_match_current_head_and_keep_failures_closed(self):
        for mode in ('mapping', 'version', 'date', 'failed'):
            with self.subTest(mode=mode):
                s, catalog, _ = service([candidate('000001')], [rank('000001', '1')])
                original_read = catalog.read
                def changed_page(query='', limit=500, offset=0):
                    page = original_read(query, limit, offset)
                    if limit == 500:
                        if mode == 'mapping':
                            return None
                        if mode == 'version':
                            page['catalog_version'] = 'b' * 64
                        elif mode == 'date':
                            page['source_date'] = '2026-10-08'
                        else:
                            page['catalog_sync']['fetch_status'] = 'failed'
                    return page
                catalog.read = changed_page
                result = s.read()
                self.assertFalse(result['ranking_complete'])
                self.assertEqual(result['products'], [])
                self.assertIn('candidate_catalog_', result['failure_reason'])

    def test_head_exception_or_cold_post_collection_fetching_does_not_publish_new_or_old_pool(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        first = s.read()
        pool = s._research_pool
        def failed_head(**kwargs):
            raise OSError('offline catalog head')
        catalog.read = failed_head
        blocked = s.read()
        self.assertFalse(blocked['ranking_complete'])
        self.assertEqual(blocked['products'], [])
        self.assertEqual(blocked['fetched_at'], first['fetched_at'])
        self.assertIs(s._research_pool, pool)
        self.assertEqual(len(reader.calls), 3)

        s, catalog, _ = service([candidate('000001')], [rank('000001', '1')])
        def fetching_after_page(limit, offset):
            if limit == 500:
                catalog.sync['fetch_status'] = 'fetching'
        catalog.after_read = fetching_after_page
        self.assertFalse(s.read()['ranking_complete'])
        self.assertIsNone(s._research_pool)

    def test_catalog_pagination_failure_keeps_pool_with_short_failure_cache_and_version_invalidation(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        s.read()
        pool, original_read = s._research_pool, catalog.read
        s._research_until = 0
        def failed_page(query='', limit=500, offset=0):
            page = original_read(query, limit, offset)
            return None if limit == 500 else page
        catalog.read = failed_page
        first_failure = s.read()
        self.assertFalse(first_failure['ranking_complete'])
        self.assertIs(s._research_pool, pool)
        self.assertEqual(len(reader.calls), 6)
        for code in (None, '000001'):
            cached_failure = s.read(code)
            self.assertFalse(cached_failure['ranking_complete'])
            self.assertEqual(cached_failure['products'], [])
        self.assertEqual(len(reader.calls), 6)
        catalog.read = original_read
        catalog.sync['fetch_status'] = 'fetching'
        self.assertFalse(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 6)
        catalog.sync['fetch_status'] = 'ok'
        catalog.version = 'b' * 64
        self.assertTrue(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 9)
        self.assertIsNot(s._research_pool, pool)

    def test_catalog_pagination_failure_recovers_same_version_after_failure_cache_expires(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        s.read()
        pool, original_read = s._research_pool, catalog.read
        s._research_until = 0
        def failed_page(query='', limit=500, offset=0):
            return None if limit == 500 else original_read(query, limit, offset)
        catalog.read = failed_page
        self.assertFalse(s.read()['ranking_complete'])
        self.assertIs(s._research_pool, pool)
        catalog.read = original_read
        s._catalog_failure_until = 0
        self.assertTrue(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 9)

    def test_ranking_source_failure_stays_failed_for_home_and_details_without_historical_pool(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        s.read()
        old_pool = s._research_pool
        s._research_until = 0
        reader.failed = True
        self.assertFalse(s.read()['ranking_complete'])
        self.assertIsNot(s._research_pool, old_pool)
        for code in (None, '000001'):
            failed = s.read(code)
            self.assertFalse(failed['ranking_complete'])
            self.assertEqual(failed['products'], [])
        self.assertEqual(len(reader.calls), 4)
        catalog.sync['fetch_status'] = 'fetching'
        self.assertFalse(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 4)

    def test_combined_catalog_and_ranking_failure_cannot_resume_historical_ranking(self):
        s, catalog, reader = service([candidate('000001')], [rank('000001', '1')])
        s.read()
        old_pool, original_read = s._research_pool, catalog.read
        s._research_until = 0
        reader.failed = True
        def failed_page(query='', limit=500, offset=0):
            return None if limit == 500 else original_read(query, limit, offset)
        catalog.read = failed_page
        failed = s.read()
        self.assertFalse(failed['ranking_complete'])
        self.assertEqual(failed['products'], [])
        self.assertIsNot(s._research_pool, old_pool)
        catalog.read = original_read
        catalog.sync['fetch_status'] = 'fetching'
        self.assertFalse(s.read()['ranking_complete'])
        self.assertEqual(len(reader.calls), 4)

    def test_same_version_head_structure_change_invalidates_old_candidates(self):
        s, catalog, reader = service([candidate('000001'), candidate('000002')],
                                    [rank('000001', '9'), rank('000002', '8')])
        s.read()
        catalog.rows = [candidate('000002')]
        new = s.read()
        self.assertTrue(new['ranking_complete'])
        self.assertEqual([p['code'] for p in new['products']], ['000002'])
        self.assertEqual(new['coverage']['candidate_count'], 1)
        self.assertEqual(len(reader.calls), 6)

    def test_parallel_readers_after_version_change_share_one_new_research_pool(self):
        s, catalog, reader = service([candidate('000001'), candidate('000002')],
                                    [rank('000001', '9'), rank('000002', '8')])
        s.read()
        catalog.rows, catalog.version = [candidate('000002')], 'b' * 64
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: s.read(), range(4)))
        self.assertEqual(len(reader.calls), 6)
        self.assertTrue(all(r['catalog_version'] == 'b' * 64 and r['ranking_complete'] for r in results))
        self.assertTrue(all([p['code'] for p in r['products']] == ['000002'] for r in results))
        self.assertEqual(len({r['content_version'] for r in results}), 1)

    def test_timeout_retries_only_failed_stage_and_records_only_complete_receipts(self):
        for failed_stage in ('page', 'script', 'feed'):
            for wrapped in (False, True):
                with self.subTest(stage=failed_stage, wrapped=wrapped):
                    reader, attempts, pending = Reader(feed([rank('000001', '1')])), [], [True]
                    def flaky(request, **kwargs):
                        url = request.full_url
                        stage = 'page' if url == RANK_URL else 'script' if url == SCRIPT_URL else 'feed'
                        attempts.append(stage)
                        if stage == failed_stage and pending[0]:
                            pending[0] = False
                            error = TimeoutError('synthetic timeout')
                            raise URLError(error) if wrapped else error
                        return reader(request, **kwargs)
                    s = PublicProductResearch(Catalog([candidate('000001')]), flaky, clock=lambda: NOW)
                    result = s.read()
                    self.assertTrue(result['ranking_complete'])
                    expected = ['page', 'script', 'feed']
                    expected.insert(expected.index(failed_stage), failed_stage)
                    self.assertEqual(attempts, expected)
                    self.assertEqual(len(result['source_receipts']), 3)
                    self.assertEqual(len({r['url'] for r in result['source_receipts']}), 3)

    def test_only_one_timeout_retry_is_available_for_the_entire_collection(self):
        reader, attempts, page_failed = Reader(feed([rank('000001', '1')])), [], [False]
        def flaky(request, **kwargs):
            url = request.full_url
            attempts.append(url)
            if url == RANK_URL and not page_failed[0]:
                page_failed[0] = True
                raise TimeoutError('first stage timeout')
            if url == SCRIPT_URL:
                raise URLError(TimeoutError('second stage timeout'))
            return reader(request, **kwargs)
        s = PublicProductResearch(Catalog([candidate('000001')]), flaky, clock=lambda: NOW)
        result = s.read()
        self.assertFalse(result['ranking_complete'])
        self.assertEqual(result['products'], [])
        self.assertEqual(attempts, [RANK_URL, RANK_URL, SCRIPT_URL])

    def test_retry_shares_total_deadline_and_does_not_start_with_insufficient_budget(self):
        for remaining, expected_feed_attempts in ((0, 1), (3, 1), (7, 2)):
            with self.subTest(remaining=remaining):
                tick, attempts = [100.0], []
                reader = Reader(feed([rank('000001', '1')]))
                def budgeted(request, **kwargs):
                    url = request.full_url
                    attempts.append(url)
                    self.assertGreater(kwargs['timeout'], 0)
                    self.assertLessEqual(kwargs['timeout'], 5)
                    if url not in (RANK_URL, SCRIPT_URL):
                        feed_attempts = sum(u not in (RANK_URL, SCRIPT_URL) for u in attempts)
                        tick[0] = 125.0 - remaining if feed_attempts == 1 else 125.0
                        if feed_attempts == 1:
                            raise TimeoutError('stream timeout')
                    return reader(request, **kwargs)
                s = PublicProductResearch(Catalog([candidate('000001')]), budgeted, clock=lambda: NOW)
                with patch('product_research.time.monotonic', side_effect=lambda: tick[0]):
                    result = s.read()
                self.assertFalse(result['ranking_complete'])
                self.assertEqual(result['products'], [])
                self.assertEqual(result['failure_reason'], 'TimeoutError')
                self.assertEqual(sum(u not in (RANK_URL, SCRIPT_URL) for u in attempts), expected_feed_attempts)

    def test_non_timeout_and_source_structure_errors_are_never_retried(self):
        for mode in ('url_error', 'binding', 'pagination'):
            with self.subTest(mode=mode):
                reader, attempts = Reader(feed([rank('000001', '1')])), []
                if mode == 'binding':
                    reader.script = SCRIPT.replace('arrdata[7]', 'arrdata[11]')
                elif mode == 'pagination':
                    reader.body = feed([rank('000001', '1')], allRecords=2)
                def failing(request, **kwargs):
                    attempts.append(request.full_url)
                    if mode == 'url_error':
                        raise URLError(OSError('synthetic unavailable'))
                    return reader(request, **kwargs)
                s = PublicProductResearch(Catalog([candidate('000001')]), failing, clock=lambda: NOW)
                result = s.read()
                self.assertFalse(result['ranking_complete'])
                self.assertEqual(result['products'], [])
                self.assertEqual(len(attempts), {'url_error': 1, 'binding': 2, 'pagination': 3}[mode])

    def test_two_timeout_attempts_publish_empty_result_and_share_sixty_second_failure_cache(self):
        attempts = []
        def always_timeout(request, **kwargs):
            attempts.append(request.full_url)
            raise TimeoutError('synthetic cold source failure')
        s = PublicProductResearch(Catalog([candidate('000001')]), always_timeout, clock=lambda: NOW)
        with patch('product_research.time.monotonic', return_value=100.0):
            first = s.read()
            self.assertEqual(s._research_until, 160.0)
            for code in (None, '000001'):
                cached = s.read(code)
                self.assertFalse(cached['ranking_complete'])
                self.assertEqual(cached['products'], [])
                self.assertEqual(cached['fetched_at'], first['fetched_at'])
        self.assertEqual(first['failure_reason'], 'TimeoutError')
        self.assertEqual(attempts, [RANK_URL, RANK_URL])

    def test_only_selected_rows_are_copied_under_lock_and_caller_cannot_mutate_pool(self):
        codes = [f'{i:06d}' for i in range(1, 8)]
        s, _, _ = service([candidate(code) for code in codes],
                          [rank(code, str(i)) for i, code in reversed(list(enumerate(codes, 1)))])
        s.read()
        copied = []
        test = self
        class Payload:
            def __init__(self, code):
                self.code = code
            def __deepcopy__(self, memo):
                test.assertTrue(s._research_lock.locked(), '原池选项须在锁内复制')
                copied.append(self.code)
                return {'code': self.code}
        for row in s._research_pool['products']:
            row['raw']['copy_probe'] = Payload(row['code'])
        home = s.read()
        self.assertEqual(copied, ['000007', '000006', '000005'])
        self.assertTrue(all('stale' not in row for row in s._research_pool['products']))
        home['products'][0]['metric']['value_pct'] = '999'
        home['products'][0]['raw']['unit_nav'] = '99'
        home['coverage']['type_counts']['开放式基金'] = 999
        copied.clear()
        detail = s.read('000001')
        self.assertEqual(copied, ['000001'])
        self.assertEqual(detail['products'][0]['metric']['value_pct'], '1')
        detail['products'][0]['raw']['unit_nav'] = '88'
        home_again = s.read()
        self.assertEqual(home_again['products'][0]['metric']['value_pct'], '7')
        self.assertEqual(home_again['products'][0]['raw']['unit_nav'], '1.0001')
        self.assertEqual(home_again['coverage']['type_counts']['开放式基金'], 7)
        self.assertEqual(s.read('000001')['products'][0]['raw']['unit_nav'], '1.0001')

    def test_gateway_query_accepts_only_optional_single_ascii_share_code(self):
        gateway = FinanceGateway.__new__(FinanceGateway)
        class FakeResearch:
            def read(self, code=None):
                return dict(ranking_complete=True, products=[dict(code=code)] if code != '123456' else [])
        gateway.public_product_research = FakeResearch()
        gateway._product_performance_lock = threading.Lock()
        self.assertIsNone(gateway.public_research_products_read({})['products'][0]['code'])
        self.assertEqual(gateway.public_research_products_read({'code': ['000001']})['products'][0]['code'], '000001')
        for query in [{'code': ['']}, {'code': ['000001', '000002']}, {'code': ['０００００１']},
                      {'code': ['../000001']}, {'limit': ['3']}, {'code': []}]:
            with self.assertRaises(BankError):
                gateway.public_research_products_read(query)
        with self.assertRaises(BankError) as error:
            gateway.public_research_products_read({'code': ['123456']})
        self.assertEqual(error.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
