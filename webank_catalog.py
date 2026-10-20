"""微众现行官网的带日期销售披露；公开资料与可执行银行产品分离。"""
from datetime import date, datetime, timezone
import base64
from copy import deepcopy
import math
import os
from pathlib import Path
import tempfile
import hashlib
import json
import re
import threading
import time
from urllib.parse import quote, urlsplit
import urllib.error
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
    """服务自带同步；读取不持锁等待网络，公开原件与私有银行库分开。"""
    def __init__(self, opener=None, *, data_dir=None, refresh_interval_seconds=300,
                 clock=time.time, monotonic=time.monotonic):
        interval=float(refresh_interval_seconds)
        if not math.isfinite(interval) or not .01<=interval<=86400:
            raise ValueError('invalid_public_sync_interval')
        self.opener=opener or urllib.request.urlopen
        self.interval=interval;self.clock=clock;self.monotonic=monotonic
        self.lock=threading.RLock();self._fetch_lock=threading.Lock()
        self.cached=None;self.until=0;self._raw=None;self._validators={}
        self._stop=threading.Event();self._thread=None;self._closed=False
        self._background_enabled=False
        self._meta={'checked_at':None,'last_success_at':None,'fetch_status':'not_checked'}
        self._snapshot_consumer=None
        self.data_dir=Path(data_dir).expanduser().resolve() if data_dir is not None else None
        self._load()

    def set_snapshot_consumer(self, consumer):
        """受信基座发布回调；复用此worker，回调不得发起网络抓取。"""
        if not callable(consumer):raise TypeError('invalid_snapshot_consumer')
        with self.lock:self._snapshot_consumer=consumer

    def _publish_research(self, body, snapshot):
        with self.lock:consumer=self._snapshot_consumer
        if consumer is None:return
        try:
            published=consumer(body, source_url=URL, fetched_at=snapshot['fetched_at'],
                               expected_sha256=snapshot['sha256'])
            if not isinstance(published,dict) or published.get('status') not in ('published','unchanged'):
                raise ValueError('invalid_research_publish_receipt')
            with self.lock:self._meta['research_publication']={**published,'last_attempt_at':self._iso(self.clock())}
        except Exception as error:
            # 公开抓取与研究发布分列；发布失败不替换研究端上一有效版本，下一检查重试。
            with self.lock:
                previous=self._meta.get('research_publication') or {}
                self._meta['research_publication']={'status':'failed','error':type(error).__name__,
                    'candidate_sha256':snapshot['sha256'],'last_valid_revision':previous.get('provider_revision',previous.get('last_valid_revision')),
                    'last_attempt_at':self._iso(self.clock())}

    @staticmethod
    def _iso(stamp):
        return datetime.fromtimestamp(stamp,timezone.utc).isoformat()

    @staticmethod
    def _header(response,name):
        return (getattr(response,'headers',None) or {}).get(name)

    def _snapshot(self,body,checked):
        source_date,products=decode_catalog(body)
        stale=(publication_today()-source_date).days>7
        return dict(provider='微众银行官方公开产品查询',source_url=PAGE,data_url=URL,
            source_date=source_date.isoformat(),stale=stale,fetched_at=checked,
            sha256=hashlib.sha256(body).hexdigest(),status='historical' if stale else 'current_disclosure',
            disclosure_scope='dated_channel_snapshot',total=len(products),products=products,
            notice='销售状态是资料日期对应的微众官方快照；当天是否可申购、额度、限额和渠道费率须另行核验。净值与收益未披露时保持未知。')

    def _atomic(self,name,value,*,cancel_on_stop=False):
        if self.data_dir is None:return
        self.data_dir.mkdir(parents=True,exist_ok=True)
        encoded=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
        fd,temporary=tempfile.mkstemp(prefix='.'+name+'-',dir=self.data_dir)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            if cancel_on_stop and self._stop.is_set():raise InterruptedError('sync_stopped')
            os.replace(temporary,self.data_dir/name)
            directory=os.open(self.data_dir,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    def _persist_snapshot(self,snapshot,body,meta,validators):
        self._atomic('webank-product-catalog.json',{'schema_version':1,'data_url':URL,
            'sha256':snapshot['sha256'],'body_base64':base64.b64encode(body).decode('ascii'),
            'fetched_at':snapshot['fetched_at'],'last_success_at':meta['last_success_at'],
            'validators':validators},cancel_on_stop=True)

    def _persist_status(self):
        if self.data_dir is None:return
        with self.lock:
            record={'schema_version':1,'data_url':URL,'sha256':(self.cached or {}).get('sha256'),**self._meta}
        self._atomic('webank-product-sync.json',record)

    def _load(self):
        if self.data_dir is None:return
        path=self.data_dir/'webank-product-catalog.json'
        try:
            if path.stat().st_size>2*MAX_BYTES+16384:raise ValueError('snapshot_too_large')
            stored=json.loads(path.read_bytes())
            if not isinstance(stored,dict):raise ValueError('invalid_snapshot_document')
            if stored.get('schema_version')!=1 or stored.get('data_url')!=URL:raise ValueError('wrong_snapshot_source')
            body=base64.b64decode(stored['body_base64'],validate=True)
            snapshot=self._snapshot(body,stored['fetched_at'])
            if snapshot['sha256']!=stored['sha256']:raise ValueError('snapshot_hash_mismatch')
            for key in ('fetched_at','last_success_at'):
                if datetime.fromisoformat(stored[key]).tzinfo is None:raise ValueError('invalid_snapshot_time')
            headers=stored.get('validators',{})
            if not isinstance(headers,dict):raise ValueError('invalid_snapshot_validators')
            self.cached=snapshot;self._raw=body
            self._validators={k:v for k,v in headers.items()
                if k in ('ETag','Last-Modified') and isinstance(v,str) and len(v)<=512 and not any(c in v for c in '\r\n')}
            self._meta.update(last_success_at=stored['last_success_at'])
            status=self.data_dir/'webank-product-sync.json'
            if status.is_file() and status.stat().st_size<=16384:
                record=json.loads(status.read_bytes())
                if not isinstance(record,dict):raise ValueError('invalid_sync_status_document')
                checked=record.get('checked_at')
                if (record.get('schema_version')==1 and record.get('data_url')==URL
                    and record.get('sha256')==snapshot['sha256'] and record.get('fetch_status') in ('ok','unchanged','failed')
                    and record.get('last_success_at')==stored['last_success_at']
                    and isinstance(checked,str) and datetime.fromisoformat(checked).tzinfo is not None):
                    self._meta.update({k:record.get(k) for k in ('checked_at','last_success_at','fetch_status','research_publication')})
        except FileNotFoundError:pass
        except (OSError,ValueError,KeyError,TypeError,UnicodeError):
            # 损坏缓存不能替代来源，不改原文件，下一次仍到官网核验。
            if self.cached is None:self._meta['cache_load_status']='invalid'

    def _fetch(self,request):
        started=self.monotonic()
        with self.opener(request,timeout=15) as response:
            final=response.geturl() if hasattr(response,'geturl') else request.full_url
            expected=urlsplit(URL);actual=urlsplit(final)
            if (actual.scheme,actual.netloc,actual.path)!=(expected.scheme,expected.netloc,expected.path):
                raise ValueError('unexpected_catalog_redirect')
            status=getattr(response,'status',None) or 200
            validators={name:self._header(response,name) for name in ('ETag','Last-Modified') if self._header(response,name)}
            if status==304:return None,validators
            if status!=200:raise ValueError('unexpected_catalog_status')
            length=self._header(response,'Content-Length')
            if length is not None and int(length)>MAX_BYTES:raise ValueError('catalog_too_large')
            chunks=[];size=0
            # read1只做一次底层读取，避免read(n)内部等待累计字节而反复重置超时。
            reader=getattr(response,'read1',response.read)
            raw=getattr(getattr(response,'fp',None),'raw',None)
            sock=getattr(raw,'_sock',None)
            while True:
                if self._stop.is_set():raise InterruptedError('sync_stopped')
                remaining=20-(self.monotonic()-started)
                if remaining<=0:raise TimeoutError('catalog_deadline')
                if sock is not None and sock.fileno()>=0:sock.settimeout(min(15,remaining))
                chunk=reader(min(65536,MAX_BYTES+1-size))
                if not chunk:break
                size+=len(chunk)
                if size>MAX_BYTES:raise ValueError('catalog_too_large')
                chunks.append(chunk)
            return b''.join(chunks),validators

    def refresh(self, *, force=False):
        if not self._fetch_lock.acquire(blocking=False):return False
        try:
            with self.lock:
                if self._closed or (not force and self.monotonic()<self.until):return False
                checked=self._iso(self.clock());self._meta.update(checked_at=checked,fetch_status='fetching')
                validators=dict(self._validators);old=self.cached
            headers={'Cache-Control':'no-cache, max-age=0','Pragma':'no-cache',
                     'User-Agent':'Navren-PublicCatalog/1.0','Accept':'application/json'}
            if self._raw is not None:
                if validators.get('ETag'):headers['If-None-Match']=validators['ETag']
                if validators.get('Last-Modified'):headers['If-Modified-Since']=validators['Last-Modified']
            request=urllib.request.Request(URL+'?'+quote(checked,safe=''),headers=headers)
            try:
                try:body,new_validators=self._fetch(request)
                except urllib.error.HTTPError as error:
                    if error.code!=304:raise
                    expected=urlsplit(URL);actual=urlsplit(error.geturl())
                    if (actual.scheme,actual.netloc,actual.path)!=(expected.scheme,expected.netloc,expected.path):
                        error.close();raise ValueError('unexpected_catalog_redirect')
                    body=None;new_validators={name:error.headers.get(name) for name in ('ETag','Last-Modified') if error.headers and error.headers.get(name)}
                    error.close()
                if self._stop.is_set():raise InterruptedError('sync_stopped')
                success=self._iso(self.clock())
                not_modified=body is None
                if body is None:
                    if old is None or self._raw is None:raise ValueError('unverifiable_not_modified')
                    snapshot=old;body=self._raw;state='unchanged'
                else:
                    snapshot=self._snapshot(body,success)
                    if old and snapshot['source_date']<old['source_date']:raise ValueError('regressed_catalog_date')
                    state='unchanged' if old and old['sha256']==snapshot['sha256'] else 'ok'
                    if state=='unchanged':snapshot=old
                meta={**self._meta,'checked_at':checked,'last_success_at':success,'fetch_status':state}
                meta.pop('fetch_error',None)
                valid_headers={**(validators if not_modified else {}),**{k:v for k,v in new_validators.items() if isinstance(v,str) and len(v)<=512 and not any(c in v for c in '\r\n')}}
                if self._stop.is_set():raise InterruptedError('sync_stopped')
                self._persist_snapshot(snapshot,body,meta,valid_headers)
                with self.lock:
                    if self._stop.is_set():raise InterruptedError('sync_stopped')
                    self.cached=snapshot;self._raw=body;self._validators=valid_headers;self._meta=meta
                self._publish_research(body,snapshot)
            except Exception as error:
                with self.lock:
                    self._meta.update(fetch_status='failed',fetch_error=type(error).__name__)
            finally:
                with self.lock:self.until=self.monotonic()+self.interval
                try:self._persist_status()
                except OSError:
                    with self.lock:self._meta['persistence_status']='failed'
            return True
        finally:self._fetch_lock.release()

    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():return False
            self._closed=False;self._stop.clear();self._background_enabled=True;self.until=0
            self._thread=threading.Thread(target=self._run,name='navren-webank-catalog-sync',daemon=True)
            self._thread.start()
            return True

    def _run(self):
        try:
            with self.lock:body,snapshot=self._raw,deepcopy(self.cached)
            if body is not None and snapshot is not None:self._publish_research(body,snapshot)
            while not self._stop.is_set():
                self.refresh()
                with self.lock:wait=max(.01,self.until-self.monotonic())
                self._stop.wait(wait)
        finally:
            with self.lock:self._background_enabled=False

    def stop(self, timeout=2):
        # 不等待同步线程的文件/网络操作持有的锁，关闭调用本身有界。
        self._closed=True;self._background_enabled=False;self._stop.set();thread=self._thread
        if thread and thread is not threading.current_thread():thread.join(max(0,float(timeout)))
        return not thread or not thread.is_alive()

    def _sync_locked(self):
        next_at=None
        if self._background_enabled and not self._closed:
            next_at=self._iso(self.clock()+max(0,self.until-self.monotonic()))
        return {key:self._meta.get(key) for key in ('checked_at','last_success_at','fetch_status')} | {
            'next_check_at':next_at,'refresh_interval_seconds':self.interval,
            'source_date':(self.cached or {}).get('source_date'),'background_enabled':self._background_enabled,
            'research_publication':deepcopy(self._meta.get('research_publication'))}

    def sync_status(self):
        with self.lock:
            sync=self._sync_locked()
            return {'provider':'微众银行官方公开产品查询','source_url':PAGE,'data_url':URL,
                    'source_date':sync['source_date'],'sha256':(self.cached or {}).get('sha256'),
                    'catalog_version':(self.cached or {}).get('sha256'), 'catalog_sync':sync,
                    'checked_at':sync['checked_at'],'fetch_status':sync['fetch_status'],
                    'thread_alive':bool(self._thread and self._thread.is_alive())}

    def read(self, query='', limit=24, offset=0):
        with self.lock:lazy=not self._background_enabled and not self._closed and self.monotonic()>=self.until
        if lazy:self.refresh()
        with self.lock:
            cached=self.cached or dict(provider='微众银行官方公开产品查询',source_url=PAGE,data_url=URL,
                source_date=None,stale=True,status='unavailable',disclosure_scope='dated_channel_snapshot',total=0,products=[],
                notice='官方资料暂时无法读取；没有用旧接口或模拟产品替代。')
            result={k:v for k,v in cached.items() if k!='products'}
            if result.get('source_date'):
                stale=(publication_today()-date.fromisoformat(result['source_date'])).days>7
                result.update(stale=stale,status='historical' if stale else 'current_disclosure')
            sync=self._sync_locked()
            result.update(checked_at=sync['checked_at'],fetch_status=sync['fetch_status'],
                last_success_at=sync['last_success_at'],next_check_at=sync['next_check_at'],
                refresh_interval_seconds=sync['refresh_interval_seconds'],background_enabled=sync['background_enabled'],catalog_sync=sync)
            if sync['fetch_status']=='failed' and self.cached:
                result['notice']='官方目录本次读取失败，保留上次核验的带日期快照；没有更新为今天的在售状态。'
            rows=cached['products']
            if query:
                wanted=query.casefold()
                rows=[p for p in rows if wanted in ' '.join(str(p.get(k) or '') for k in FIELD_MAP.values()).casefold()]
            limit=max(1,min(int(limit),MAX_PAGE));offset=max(0,int(offset))
            result.update(matched=len(rows),offset=offset,limit=limit,has_more=offset+limit<len(rows),
                catalog_version=result.get('sha256'),products=deepcopy(rows[offset:offset+limit]))
        result['source_policy']='webank_disclosure_only';result['verified_nav_products']=[]
        result['verified_nav_scope']='产品资料仅采用微众银行直接公开披露；该目录未提供连续净值、具体渠道费率和实时申购条件，缺项保持未知。'
        result['insurance_disclosure_url']='https://www.webank.com/disclosure/insurance/prods'
        return result
