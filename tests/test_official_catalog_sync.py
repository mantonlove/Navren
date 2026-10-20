"""只验证官网目录同步、故障保留、公共缓存和隔离服务生命周期。"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit

from webank_catalog import PublicProductCatalog, URL, MAX_BYTES
from test_current_catalog import payload, row


class Response(io.BytesIO):
    def __init__(self, body=b'', *, status=200, headers=None, url=URL):
        super().__init__(body)
        self.status=status;self.headers=headers or {};self.url=url
    def geturl(self):return self.url


class Clock:
    def __init__(self):self.wall=1791504000.;self.tick=100.
    def now(self):return self.wall
    def monotonic(self):return self.tick
    def advance(self,seconds):self.wall+=seconds;self.tick+=seconds


class OfficialCatalogSyncTests(unittest.TestCase):
    def test_status_read_never_fetches_and_default_contract(self):
        calls=[]
        catalog=PublicProductCatalog(lambda *a,**k:calls.append(a))
        status=catalog.sync_status()
        self.assertEqual(calls,[])
        self.assertEqual(set(status['catalog_sync']),{'checked_at','last_success_at','next_check_at',
            'refresh_interval_seconds','fetch_status','source_date','background_enabled'})
        self.assertEqual(status['catalog_sync']['refresh_interval_seconds'],300)
        self.assertEqual(status['fetch_status'],'not_checked')
        self.assertIsNone(status['catalog_sync']['next_check_at'])

    def test_real_request_cache_bust_no_cache_and_conditional_validation(self):
        clock=Clock();calls=[]
        def opener(request,**kwargs):
            calls.append((request,kwargs))
            return Response(payload(),headers={'ETag':'"v1"','Last-Modified':'Tue, 06 Oct 2026 18:14:01 GMT'})
        catalog=PublicProductCatalog(opener,clock=clock.now,monotonic=clock.monotonic)
        catalog.refresh();first=catalog.read();clock.advance(300);catalog.refresh()
        request,options=calls[1]
        self.assertEqual(options['timeout'],15)
        self.assertEqual(unquote(urlsplit(request.full_url).query),datetime.fromtimestamp(clock.wall,timezone.utc).isoformat())
        self.assertEqual(request.get_header('Cache-control'),'no-cache, max-age=0')
        self.assertEqual(request.get_header('Pragma'),'no-cache')
        self.assertEqual(request.get_header('If-none-match'),'"v1"')
        self.assertIn('06 Oct 2026',request.get_header('If-modified-since'))
        second=catalog.read()
        self.assertEqual(second['fetch_status'],'unchanged')
        self.assertEqual(second['fetched_at'],first['fetched_at'])
        self.assertEqual(second['source_date'],first['source_date'])
        self.assertNotEqual(second['last_success_at'],first['last_success_at'])

    def test_restart_recovers_verified_bytes_status_and_no_read_side_effect(self):
        clock=Clock()
        with tempfile.TemporaryDirectory() as directory:
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload()),data_dir=directory,
                clock=clock.now,monotonic=clock.monotonic)
            first=catalog.read();catalog.stop()
            calls=[]
            restored=PublicProductCatalog(lambda *a,**k:calls.append(a),data_dir=directory)
            status=restored.sync_status();restored.stop();second=restored.read()
            self.assertEqual(status['fetch_status'],'ok')
            self.assertEqual(status['catalog_sync']['checked_at'],first['checked_at'])
            self.assertEqual(second['catalog_version'],first['catalog_version'])
            self.assertEqual(second['products'],first['products'])
            self.assertEqual(second['fetched_at'],first['fetched_at'])
            self.assertEqual(calls,[])

    def test_bad_responses_keep_exact_snapshot_and_last_success_across_restart(self):
        clock=Clock()
        with tempfile.TemporaryDirectory() as directory:
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload(on='2026-09-30')),data_dir=directory,
                clock=clock.now,monotonic=clock.monotonic)
            first=catalog.read();path=Path(directory)/'webank-product-catalog.json';original=path.read_bytes()
            for bad in (b'broken',payload(on='2026-09-24'),payload([row(),row()]),b'x'*(MAX_BYTES+1)):
                with self.subTest(bytes=len(bad)):
                    clock.advance(300);catalog.opener=lambda *a,**k:Response(bad)
                    self.assertTrue(catalog.refresh())
                    latest=catalog.read()
                    self.assertEqual(latest['fetch_status'],'failed')
                    self.assertEqual(latest['source_date'],'2026-09-30')
                    self.assertEqual(latest['catalog_version'],first['catalog_version'])
                    self.assertEqual(latest['last_success_at'],first['last_success_at'])
                    self.assertEqual(path.read_bytes(),original)
                    self.assertEqual(catalog.until,clock.monotonic()+300)
            restored=PublicProductCatalog(data_dir=directory);restored.stop()
            self.assertEqual(restored.read()['fetch_status'],'failed')
            self.assertEqual(restored.read()['catalog_version'],first['catalog_version'])

    def test_corrupt_or_foreign_cache_is_rejected_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload()),data_dir=directory)
            catalog.refresh();path=Path(directory)/'webank-product-catalog.json'
            valid=json.loads(path.read_bytes())
            for change in ({'sha256':'wrong'},{'data_url':'https://example.com/catalog'},
                           {'body_base64':'broken'},{'validators':[]}):
                with self.subTest(change=change):
                    path.write_text(json.dumps({**valid,**change}));before=path.read_bytes()
                    restored=PublicProductCatalog(data_dir=directory);restored.stop()
                    self.assertEqual(restored.read()['products'],[])
                    self.assertEqual(path.read_bytes(),before)

    def test_invalid_document_shape_does_not_crash_service_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'webank-product-catalog.json';path.write_text('[]')
            catalog=PublicProductCatalog(data_dir=directory);catalog.stop()
            self.assertEqual(catalog.read()['products'],[])
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload()),data_dir=directory)
            first=catalog.read();(Path(directory)/'webank-product-sync.json').write_text('[]')
            restored=PublicProductCatalog(data_dir=directory);restored.stop()
            self.assertEqual(restored.read()['catalog_version'],first['catalog_version'])
            self.assertEqual(restored.read()['products'],first['products'])

    def test_atomic_write_failure_keeps_previous_memory_and_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload()),data_dir=directory)
            first=catalog.read();path=Path(directory)/'webank-product-catalog.json';before=path.read_bytes()
            catalog.opener=lambda *a,**k:Response(payload([row('NEW')]))
            with patch.object(catalog,'_persist_snapshot',side_effect=OSError('disk')):
                catalog.refresh(force=True)
            self.assertEqual(catalog.read()['catalog_version'],first['catalog_version'])
            self.assertEqual(catalog.read()['fetch_status'],'failed')
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(Path(directory).glob('.*')),[])

    def test_valid_304_keeps_source_hash_fetched_time_advances_success(self):
        clock=Clock()
        catalog=PublicProductCatalog(lambda *a,**k:Response(payload(),headers={'ETag':'"one"'}),
            clock=clock.now,monotonic=clock.monotonic)
        first=catalog.read();clock.advance(300)
        def unchanged(request,**kwargs):
            raise HTTPError(request.full_url,304,'Not Modified',{'ETag':'"one"'},io.BytesIO())
        catalog.opener=unchanged;catalog.refresh()
        second=catalog.read()
        for key in ('source_date','catalog_version','fetched_at'):self.assertEqual(second[key],first[key])
        self.assertEqual(second['fetch_status'],'unchanged')
        self.assertNotEqual(second['last_success_at'],first['last_success_at'])

    def test_cold_304_cannot_create_snapshot(self):
        catalog=PublicProductCatalog(lambda *a,**k:Response(status=304))
        result=catalog.read()
        self.assertEqual(result['fetch_status'],'failed')
        self.assertEqual(result['products'],[])
        self.assertIsNone(result['last_success_at'])

    def test_changed_200_drops_obsolete_conditional_headers(self):
        catalog=PublicProductCatalog(lambda *a,**k:Response(payload(),headers={'ETag':'"one"'}))
        first=catalog.read()
        catalog.opener=lambda *a,**k:Response(payload([row('NEW')]))
        catalog.refresh(force=True)
        self.assertNotEqual(catalog.read()['catalog_version'],first['catalog_version'])
        self.assertEqual(catalog._validators,{})

    def test_single_flight_and_cached_read_does_not_wait_for_network(self):
        entered=threading.Event();release=threading.Event();calls=[]
        catalog=PublicProductCatalog(lambda *a,**k:Response(payload()))
        first=catalog.read()
        def blocked(*args,**kwargs):
            calls.append(args);entered.set();release.wait(2);return Response(payload([row('NEW')]))
        catalog.opener=blocked;catalog.until=0
        thread=threading.Thread(target=catalog.refresh);thread.start()
        try:
            self.assertTrue(entered.wait(1));started=time.monotonic()
            self.assertFalse(catalog.refresh(force=True))
            self.assertEqual(catalog.read()['catalog_version'],first['catalog_version'])
            self.assertLess(time.monotonic()-started,.2)
            self.assertEqual(len(calls),1)
        finally:release.set();thread.join(2);catalog.stop()
        self.assertEqual(catalog.read()['products'][0]['code'],'NEW')

    def test_background_periodic_start_stop_and_start_again(self):
        twice=threading.Event();calls=[]
        def opener(*a,**k):
            calls.append(a)
            if len(calls)>=2:twice.set()
            return Response(payload())
        catalog=PublicProductCatalog(opener,refresh_interval_seconds=.03)
        self.assertTrue(catalog.start());self.assertFalse(catalog.start())
        self.assertTrue(twice.wait(1));self.assertTrue(catalog.stop(timeout=1))
        count=len(calls);time.sleep(.08)
        self.assertEqual(len(calls),count)
        self.assertFalse(catalog.sync_status()['thread_alive'])
        self.assertFalse(catalog.sync_status()['catalog_sync']['background_enabled'])
        self.assertIsNone(catalog.sync_status()['catalog_sync']['next_check_at'])
        self.assertTrue(catalog.start());self.assertTrue(catalog.stop(timeout=1))

    def test_bounded_stop_inflight_request_cannot_publish_after_close(self):
        entered=threading.Event();release=threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload()),data_dir=directory)
            first=catalog.read();path=Path(directory)/'webank-product-catalog.json';before=path.read_bytes()
            def blocked(*a,**k):
                entered.set();release.wait(2);return Response(payload([row('NEW')]))
            catalog.opener=blocked;catalog.start()
            try:
                self.assertTrue(entered.wait(1));start=time.monotonic()
                self.assertFalse(catalog.stop(timeout=.03))
                self.assertLess(time.monotonic()-start,.2)
                self.assertEqual(catalog.read()['catalog_version'],first['catalog_version'])
            finally:release.set();catalog._thread.join(1)
            self.assertFalse(catalog.sync_status()['thread_alive'])
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(catalog.read()['catalog_version'],first['catalog_version'])

    def test_body_deadline_and_byte_bounds(self):
        clock=Clock()
        class Slow(Response):
            def read1(self,size):
                clock.advance(21);return b'x'
        for opener in (lambda *a,**k:Slow(),lambda *a,**k:Response(headers={'Content-Length':str(MAX_BYTES+1)}),
                       lambda *a,**k:Response(b'x'*(MAX_BYTES+1))):
            with self.subTest(opener=opener):
                catalog=PublicProductCatalog(opener,clock=clock.now,monotonic=clock.monotonic)
                self.assertEqual(catalog.read()['fetch_status'],'failed')
                self.assertIsNone(catalog.cached)

    def test_response_socket_closed_on_last_chunk_does_not_fail_valid_body(self):
        class Socket:
            closed=False
            def fileno(self):return -1 if self.closed else 1
            def settimeout(self,value):
                if self.closed:raise OSError('Bad file descriptor')
        class ClosingResponse(Response):
            def __init__(self):
                super().__init__(payload());self.sock=Socket()
                self.fp=SimpleNamespace(raw=SimpleNamespace(_sock=self.sock))
            def read1(self,size):
                block=super().read1(size)
                self.sock.closed=True
                return block
        catalog=PublicProductCatalog(lambda *a,**k:ClosingResponse())
        self.assertEqual(catalog.read()['fetch_status'],'ok')
        self.assertEqual(catalog.read()['total'],1)

    def test_wrong_redirect_and_non_200_are_rejected(self):
        for response in (Response(payload(),url='https://example.com/source'),Response(payload(),status=500)):
            with self.subTest(response=response):
                catalog=PublicProductCatalog(lambda *a,**k:response)
                self.assertEqual(catalog.read()['products'],[])
                self.assertEqual(catalog.sync_status()['fetch_status'],'failed')

    def test_returned_products_cannot_poison_cache(self):
        catalog=PublicProductCatalog(lambda *a,**k:Response(payload()))
        result=catalog.read();result['products'][0]['code']='POISON'
        self.assertEqual(catalog.read()['products'][0]['code'],'006210')

    def test_configuration_rejects_non_finite_and_invalid_intervals(self):
        for value in (0,-1,float('nan'),float('inf'),'invalid',86401):
            with self.subTest(interval=value),self.assertRaises(ValueError):
                PublicProductCatalog(refresh_interval_seconds=value)


class IsolatedServiceSyncTests(unittest.TestCase):
    def isolated_env(self,directory,**values):
        return patch.dict(os.environ,{'NAVREN_KNOWLEDGE_DB':str(Path(directory)/'knowledge.sqlite'),
            'NAVREN_FINANCIAL_DB':'','NAVREN_PUBLIC_DATA_DIR':'',
            'NAVREN_PUBLIC_SYNC_INTERVAL_SECONDS':'300','NAVREN_PUBLIC_SYNC_ENABLED':'true',**values})

    def test_gateway_default_path_and_portable_configuration(self):
        from finance_gateway import FinanceGateway
        with tempfile.TemporaryDirectory() as directory,self.isolated_env(directory):
            gateway=FinanceGateway(Path(directory)/'bank.sqlite')
            self.assertEqual(gateway.public_products.data_dir,(Path(directory)/'public-data').resolve())
            self.assertEqual(gateway.public_products.interval,300)
            self.assertFalse(gateway.public_products.sync_status()['thread_alive'])
        with tempfile.TemporaryDirectory() as directory,self.isolated_env(directory,
            NAVREN_PUBLIC_DATA_DIR=str(Path(directory)/'alternate'),NAVREN_PUBLIC_SYNC_INTERVAL_SECONDS='600',
            NAVREN_PUBLIC_SYNC_ENABLED='false'):
            gateway=FinanceGateway(Path(directory)/'bank.sqlite')
            self.assertEqual(gateway.public_products.data_dir,(Path(directory)/'alternate').resolve())
            self.assertEqual(gateway.public_products.interval,600)
            self.assertFalse(gateway.start_public_sync())
            self.assertTrue(gateway.stop_public_sync())

    def test_http_shared_snapshot_read_only_status_and_service_owned_stop(self):
        from server import create_server
        from test_bank_integration import Browser
        ready=threading.Event()
        rows=[row('FUND'),{**row('INSURANCE','测试保险'),'type':'保险'}]
        def opener(*a,**k):ready.set();return Response(payload(rows,on='2026-09-30'))
        with tempfile.TemporaryDirectory() as directory,self.isolated_env(directory):
            catalog=PublicProductCatalog(opener,data_dir=Path(directory)/'public-data')
            server=create_server(0,Path(directory)/'bank.sqlite',public_catalog=catalog)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                self.assertTrue(ready.wait(1))
                for _ in range(100):
                    if catalog.sync_status()['fetch_status']=='ok':break
                    time.sleep(.01)
                browser=Browser('http://127.0.0.1:'+str(server.server_port));browser.start()
                status,all_products,_=browser.call('/api/finance/public-products?limit=500')
                self.assertEqual(status,200)
                status,insurance,_=browser.call('/api/finance/public-products?q='+quote('保险'))
                self.assertEqual(status,200);self.assertEqual(insurance['matched'],1)
                status,metadata,_=browser.call('/api/finance/public-products/sync-status')
                self.assertEqual(status,200)
                self.assertEqual(all_products['catalog_version'],insurance['catalog_version'])
                self.assertEqual(metadata['catalog_version'],all_products['catalog_version'])
                for key in metadata['catalog_sync']:
                    if key!='next_check_at':self.assertEqual(metadata['catalog_sync'][key],all_products['catalog_sync'][key])
                self.assertTrue(metadata['catalog_sync']['background_enabled'])
                status,_,_=browser.call('/api/finance/public-products/sync-status?refresh=1')
                self.assertEqual(status,400)
                status,_,_=browser.call('/api/finance/public-products/sync-status','POST',{})
                self.assertIn(status,(400,404,405))
            finally:server.shutdown();server.server_close();thread.join(2)
            self.assertFalse(catalog.sync_status()['thread_alive'])
            self.assertFalse(catalog.sync_status()['catalog_sync']['background_enabled'])

    def test_invalid_enabled_flag_closes_new_server_socket(self):
        import server as module
        instances=[];original=module.NavrenHTTPServer
        def factory(*a,**k):
            instance=original(*a,**k);instances.append(instance);return instance
        with tempfile.TemporaryDirectory() as directory,self.isolated_env(directory,NAVREN_PUBLIC_SYNC_ENABLED='bad'):
            catalog=PublicProductCatalog(lambda *a,**k:Response(payload()))
            with patch.object(module,'NavrenHTTPServer',side_effect=factory),self.assertRaises(ValueError):
                module.create_server(0,Path(directory)/'bank.sqlite',public_catalog=catalog)
            self.assertEqual(instances[0].socket.fileno(),-1)
            self.assertFalse(catalog.sync_status()['thread_alive'])


if __name__=='__main__':unittest.main()
