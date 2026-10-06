"""仅按登记的公开对象键补充候选；无用户查询外发，无自动准入或共享晋升。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import threading
import time
from typing import Callable

from .store import KnowledgeError, day, ident, packed, sha, now


@dataclass(frozen=True)
class FetchRequest:
    source_id: str
    endpoint: str
    public_key: str
    product_id: str | None
    share_class_id: str | None
    channel_id: str | None
    facets: tuple[str,...]
    timeout_seconds: float
    max_bytes: int


@dataclass(frozen=True)
class FetchResult:
    documents: tuple[dict,...] = field(default_factory=tuple)
    error_code: str | None = None


class SupplementService:
    """同键合并、限时、限量、缓存；回调只接收服务端登记的公开字段。

    fetcher 属于受信应用代码，必须在超时内终止网络读取，并按登记源检查重定向。
    超时结果不入库；本服务最多两个工作线程、四个未完成键，不能由模型注册源。
    """
    def __init__(self,provider, *, positive_ttl=300,negative_ttl=30,max_pending=4,
                 max_documents=4,max_bytes=2*1024*1024,clock=time.monotonic):
        self.provider=provider
        self.positive_ttl,self.negative_ttl=positive_ttl,negative_ttl
        self.max_pending,self.max_documents,self.max_bytes=max_pending,max_documents,max_bytes
        self.clock=clock;self.lock=threading.RLock()
        self.executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='local-evidence')
        self.registry={};self.cache={};self.pending={}

    def register(self,source_id,public_key, *, endpoint,fetcher: Callable[[FetchRequest],FetchResult],
                 product_id=None,share_class_id=None,channel_id=None,facets=('general',)):
        ident(source_id);ident(public_key)
        source=self.provider.validate_source_url(source_id,endpoint)
        if not source['enabled'] or not source['read_allowed']:
            raise KnowledgeError('source_permission_missing')
        self.provider._annotations(list(facets),[])
        for v in (product_id,share_class_id,channel_id):
            if v is not None:ident(v)
        # 独立公共对象允许名单；可配置源并不允许私有账户ID成为外部参数。
        with self.provider.db() as db:
            if product_id and not db.execute('SELECT 1 FROM products WHERE id=? AND verified=1',(product_id,)).fetchone():
                raise KnowledgeError('identity_unverified')
            if share_class_id and not db.execute('SELECT 1 FROM share_classes WHERE id=? AND product_id=?',(share_class_id,product_id)).fetchone():
                raise KnowledgeError('subject_mismatch')
            if channel_id and not db.execute('SELECT 1 FROM channels WHERE id=?',(channel_id,)).fetchone():
                raise KnowledgeError('identity_unverified')
            if product_id:
                permitted={product_id,db.execute('SELECT code FROM products WHERE id=?',(product_id,)).fetchone()[0]}
                if share_class_id:permitted.update({share_class_id,db.execute('SELECT code FROM share_classes WHERE id=?',(share_class_id,)).fetchone()[0]})
                if public_key not in permitted:raise KnowledgeError('public_key_unregistered')
        if not product_id and public_key not in {'PUBLIC-CONCEPT:'+facet for facet in facets}:
            raise KnowledgeError('public_concept_unregistered')
        self.registry[(source_id,public_key)]={'endpoint':endpoint,'fetcher':fetcher,
            'product_id':product_id,'share_class_id':share_class_id,'channel_id':channel_id,'facets':tuple(facets)}

    def supplement(self,request, *, timeout_seconds=8,max_sources=2):
        """与消费接口分开：返回候选和缺口，不悄悄更换已固定的 release_id。"""
        if request.get('scope','public')!='public':raise KnowledgeError('private_evidence')
        day(request['as_of'])
        timeout_seconds=max(.01,min(float(timeout_seconds),15))
        max_sources=max(1,min(int(max_sources),2))
        facets=set(request.get('facets') or ['general'])
        self.provider._annotations(list(facets),[])
        identities={k:request.get(k) for k in ('product_id','share_class_id','channel_id')}
        for v in identities.values():
            if v is not None:ident(v)
        # query/history/holdings 等任意输入从来不进入 FetchRequest；对象键须已登记。
        selected=[]
        for (source_id,public_key),entry in self.registry.items():
            if not facets.intersection(entry['facets']):continue
            if any(v is not None and entry[k]!=v for k,v in identities.items()):continue
            # 没有明确公开身份时不猜测来源；概念也须通过显式 public_key 选择。
            if not any(identities.values()) and request.get('public_key')!=public_key:continue
            selected.append((source_id,public_key,entry))
        report={'candidate_version_ids':[],'coverage_gaps':[],'published':False,
                'release_id':self.provider.status()['public_release_id'],'checked_sources':[]}
        if not selected:
            report['coverage_gaps']=[{'code':'source_unavailable','facet':f} for f in sorted(facets)]
            return report
        deadline=self.clock()+timeout_seconds
        for source_id,public_key,entry in selected[:max_sources]:
            report['checked_sources'].append(source_id)
            remaining=deadline-self.clock()
            if remaining<=0:
                report['coverage_gaps'].append({'code':'source_unavailable','reason':'deadline_exceeded'});break
            result=self._one(source_id,public_key,entry,remaining)
            report['candidate_version_ids'].extend(result['candidate_version_ids'])
            report['coverage_gaps'].extend(result['coverage_gaps'])
        if report['candidate_version_ids']:
            report['coverage_gaps'].append({'code':'not_enough_evidence','reason':'candidate_admission_required'})
        return report

    def _one(self,source_id,public_key,entry,timeout):
        source=self.provider.validate_source_url(source_id,entry['endpoint'])
        if not source['enabled'] or not source['read_allowed'] or not source['outbound_allowed']:
            return {'candidate_version_ids':[],'coverage_gaps':[{'code':'source_permission_missing'}]}
        if source['paused']:
            return {'candidate_version_ids':[],'coverage_gaps':[{'code':'source_unavailable','reason':'source_structure_drift'}]}
        key=sha(packed([source_id,public_key,source['revision'],entry['endpoint'],entry['product_id'],
                        entry['share_class_id'],entry['channel_id'],entry['facets']]))
        with self.lock:
            cached=self.cache.get(key)
            if cached and cached[0]>self.clock():return cached[1]
            pending=self.pending.get(key)
            if pending is None:
                if len(self.pending)>=self.max_pending:
                    return {'candidate_version_ids':[],'coverage_gaps':[{'code':'source_unavailable','reason':'source_budget_exceeded'}]}
                fetch_request=FetchRequest(source_id,entry['endpoint'],public_key,entry['product_id'],entry['share_class_id'],
                                           entry['channel_id'],entry['facets'],timeout,self.max_bytes)
                # 一个键的下载、验证、候选写入共享同一 future，避免重复版本和写竞争。
                pending=self.executor.submit(self._fetch_stage,entry['fetcher'],fetch_request,self.clock()+timeout)
                self.pending[key]=pending
                pending.add_done_callback(lambda future,k=key:self._completed(k,future))
        try:
            return pending.result(timeout=max(.001,timeout))
        except FutureTimeout:
            return {'candidate_version_ids':[],'coverage_gaps':[{'code':'source_unavailable','reason':'deadline_exceeded'}]}
        except Exception:
            return {'candidate_version_ids':[],'coverage_gaps':[{'code':'source_unavailable','reason':'fetch_failed'}]}

    def _completed(self,key,future):
        try:result=future.result()
        except Exception:result={'candidate_version_ids':[],'coverage_gaps':[{'code':'source_unavailable','reason':'fetch_failed'}]}
        with self.lock:
            self.pending.pop(key,None)
            ttl=self.positive_ttl if result['candidate_version_ids'] else self.negative_ttl
            self.cache[key]=(self.clock()+ttl,result)

    def _fetch_stage(self,fetcher,request,deadline):
        started=now();versions=[];code=None
        try:
            fetched=fetcher(request)
            if self.clock()>deadline:raise KnowledgeError('deadline_exceeded')
            if not isinstance(fetched,FetchResult):raise KnowledgeError('source_structure_drift')
            if fetched.error_code:raise KnowledgeError(fetched.error_code)
            if not fetched.documents:raise KnowledgeError('empty_source_response')
            if len(fetched.documents)>self.max_documents:raise KnowledgeError('document_limit_exceeded')
            if sum(len(d.get('text','').encode()) for d in fetched.documents)>request.max_bytes:
                raise KnowledgeError('response_too_large')
            for doc in fetched.documents:
                if self.clock()>deadline:raise KnowledgeError('deadline_exceeded')
                # 下载适配方也不能换到另一来源、产品、份额、渠道或额外维度。
                if doc.get('source_id',request.source_id)!=request.source_id:raise KnowledgeError('source_url_denied')
                for k in ('product_id','share_class_id','channel_id'):
                    if doc.get(k,getattr(request,k))!=getattr(request,k):raise KnowledgeError('subject_mismatch')
                if not set(doc.get('facets',request.facets)).issubset(request.facets):raise KnowledgeError('subject_mismatch')
                values={**doc,'source_id':request.source_id,'product_id':request.product_id,'facets':list(doc.get('facets',request.facets)),
                        'share_class_id':request.share_class_id,'channel_id':request.channel_id}
                versions.append(self.provider.stage_document(**values))
        except Exception as error:
            code=error.code if isinstance(error,KnowledgeError) else 'fetch_failed'
            if code=='source_structure_drift':self.provider.set_source_state(request.source_id,paused=True)
        status='candidate' if versions and code is None else 'paused' if code=='source_structure_drift' else 'failed'
        self.provider.record_job(request.source_id,request.public_key,status=status,error_code=code,version_ids=versions,started_at=started)
        if code:
            return {'candidate_version_ids':[],'coverage_gaps':[{'code':'source_unavailable','reason':code}]}
        return {'candidate_version_ids':versions,'coverage_gaps':[]}

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
