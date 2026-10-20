"""现行目录的真实字段、日期、完整性与故障边界。"""
import io
import json
import os
from datetime import date
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from webank_catalog import PublicProductCatalog, decode_catalog, URL, MAX_BYTES


def row(code='006210', name='东方臻宝纯债债券A'):
    return dict(code=code, name=name, saleStatus='在售', type='开放式基金', placement='公募',
                saleType='代销', risk='R2中低风险', publish='东方基金管理股份有限公司',
                investor='C2稳健型、C3平衡型', charge='详见产品页面',
                chargeWay='详见产品页面', customerGroupType=None)


def payload(rows=None, on='2026-09-24'):
    data={'extractDate':on, 'productPresentation':rows if rows is not None else [row()]}
    return ('_jsonpproductpresentation(' + json.dumps(data, ensure_ascii=False) + ')').encode()


class CurrentCatalogTests(unittest.TestCase):
    def test_new_source_fields_date_and_unknowns(self):
        calls=[]
        def opener(url, **kwargs):
            calls.append(url); return io.BytesIO(payload())
        result=PublicProductCatalog(opener).read()
        self.assertEqual(len(calls),1);self.assertTrue(calls[0].full_url.startswith(URL+'?'))
        self.assertEqual(result['source_date'],'2026-09-24')
        p=result['products'][0]
        self.assertEqual(p['code'],'006210');self.assertEqual(p['risk'],'R2中低风险')
        self.assertEqual(p['sale_status'],'在售');self.assertEqual(p['sales_status_as_of'],'2026-09-24')
        self.assertEqual(p['charge_way'],'详见产品页面');self.assertIsNone(p['customer_group_type'])
        self.assertIsNone(p['current_sale_status']);self.assertIsNone(p['annual_return'])
        self.assertFalse(p['executable']);self.assertTrue(p['source_url'].endswith('/006210'))

    def test_no_truncation_large_catalog_and_search_pagination(self):
        rows=[row(f'{n:06d}',f'产品{n}') for n in range(13658)]
        calls=[]
        def opener(url, **kwargs):
            calls.append(url);return io.BytesIO(payload(rows))
        catalog=PublicProductCatalog(opener)
        first=catalog.read(limit=500);last=catalog.read(limit=500,offset=13500)
        self.assertEqual(first['total'],13658);self.assertEqual(len(first['products']),500)
        self.assertEqual(len(last['products']),158);self.assertFalse(last['has_more'])
        self.assertEqual(first['catalog_version'],last['catalog_version']);self.assertEqual(len(calls),1)
        self.assertEqual(catalog.read('产品13657')['products'][0]['code'],'013657')

    def test_bad_envelope_future_date_duplicate_and_type_are_rejected(self):
        cases=[b'alert(1);',payload(on='2999-01-01'),payload([row(),row()]),
               payload([{**row(),'code':6210}]),payload([{**row(),'customerGroupType':{}}]),
               b'x'*(MAX_BYTES+1)]
        for body in cases:
            with self.subTest(body_length=len(body)):
                with self.assertRaises((ValueError,KeyError,TypeError)):decode_catalog(body)

    def test_failure_keeps_verified_snapshot_with_original_date_and_version(self):
        catalog=PublicProductCatalog(lambda *a,**kw:io.BytesIO(payload()))
        old=catalog.read();catalog.until=0
        catalog.opener=lambda *a,**kw:(_ for _ in ()).throw(OSError())
        new=catalog.read()
        self.assertEqual(new['fetch_status'],'failed');self.assertEqual(new['source_date'],old['source_date'])
        self.assertEqual(new['fetched_at'],old['fetched_at']);self.assertEqual(new['catalog_version'],old['catalog_version'])
        self.assertEqual(len(new['products']),1)

    def test_cold_failure_does_not_substitute_old_or_simulated_data(self):
        result=PublicProductCatalog(lambda *a,**kw:(_ for _ in ()).throw(OSError())).read()
        self.assertEqual(result['status'],'unavailable');self.assertEqual(result['products'],[])

    def test_product_lookup_never_reads_manager_cache(self):
        with patch('fund_history.cached',side_effect=AssertionError('不应读取管理人缓存')) as cached:
            result=PublicProductCatalog(lambda *a,**kw:io.BytesIO(payload())).read()
            cached.assert_not_called()
        self.assertEqual(result['source_policy'],'webank_disclosure_only')
        self.assertEqual(result['verified_nav_products'],[])
        self.assertIsNone(result['products'][0]['annual_return'])
        self.assertIsNone(result['products'][0]['current_sale_status'])

    def test_cached_snapshot_ages_even_if_refresh_fails(self):
        catalog=PublicProductCatalog(lambda *a,**kw:io.BytesIO(payload()))
        with patch('webank_catalog.publication_today') as clock:
            clock.return_value=date(2026,9,27)
            self.assertEqual(catalog.read()['status'],'current_disclosure')
            clock.return_value=date(2026,10,5)
            self.assertTrue(catalog.read()['stale'])
            catalog.until=0
            catalog.opener=lambda *a,**kw:(_ for _ in ()).throw(OSError())
            result=catalog.read()
            self.assertEqual(result['status'],'historical');self.assertTrue(result['stale'])
            self.assertEqual(result['fetch_status'],'failed')

    def test_downloaded_snapshot_whole_dataset(self):
        path=Path('outputs/channel-access/webank-current-catalog-20261005.json')
        if not path.exists():self.skipTest('本地实测快照不存在')
        raw=json.loads(path.read_text())
        body=('_jsonpproductpresentation('+json.dumps(raw,ensure_ascii=False)+')').encode()
        on,rows=decode_catalog(body)
        self.assertEqual(on.isoformat(),raw['extractDate']);self.assertEqual(len(rows),13658)
        self.assertEqual(len({p['code'] for p in rows}),len(rows))
        self.assertEqual(sum(p['customer_group_type'] is None for p in rows),81)
        self.assertTrue(all(p['current_sale_status'] is None and not p['executable'] for p in rows))

    def test_http_pagination_integration_reaches_final_record(self):
        from server import create_server
        from test_bank_integration import Browser
        rows=[row(f'{n:06d}',f'产品{n}') for n in range(13658)]
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{
                'NAVREN_KNOWLEDGE_DB':str(Path(tmp)/'knowledge.sqlite'),'NAVREN_FINANCIAL_DB':''}):
            catalog=PublicProductCatalog(lambda *a,**kw:io.BytesIO(payload(rows)))
            server=create_server(0,Path(tmp)/'bank.sqlite',public_catalog=catalog,public_sync_enabled=False)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                browser=Browser('http://127.0.0.1:'+str(server.server_port));browser.start()
                status,first,_=browser.call('/api/finance/public-products?limit=500&offset=0')
                self.assertEqual(status,200);self.assertEqual(first['limit'],500)
                self.assertEqual(len(first['products']),500);self.assertEqual(first['matched'],13658)
                status,last,_=browser.call('/api/finance/public-products?limit=500&offset=13500')
                self.assertEqual(status,200);self.assertEqual(len(last['products']),158)
                self.assertFalse(last['has_more']);self.assertEqual(last['products'][-1]['code'],'013657')
                self.assertEqual(first['catalog_version'],last['catalog_version'])
                status,found,_=browser.call('/api/finance/public-products?q=013657&limit=500')
                self.assertEqual(status,200);self.assertEqual(found['matched'],1)
                self.assertIsNone(found['products'][0]['current_sale_status'])
                self.assertFalse(found['products'][0]['executable'])
                status,error,_=browser.call('/api/finance/page-contexts','POST',{
                    'question':'查看旧基金资料','return_view':{'page':'recommend','view':'product'},
                    'reference':{'kind':'product','id':'ORIENT:006210','source_scope':'public_nav','version':'old-nav'}})
                self.assertEqual(status,404)
                self.assertEqual(error['error']['code'],'OBJECT_NOT_FOUND')
            finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
