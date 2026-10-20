"""草案的公开证据绑定。模型只能引用；执行在原确认事务内再回查。"""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from copy import deepcopy
import asyncio
import json
import hashlib
import sqlite3
import threading


def public_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


_held_market_connection=ContextVar('navren_held_market_publication_connection',default=None)


def _market_guard_owner():
    try:task=asyncio.current_task()
    except RuntimeError:task=None
    return threading.get_ident(),id(task) if task is not None else None


class MetricEvidenceObserver:
    """记录本轮由原金融服务签发的批次，供最终发布/确认同事务回查。"""
    def __init__(self, service):
        self.service,self.batches=service,[]

    def __getattr__(self,name):return getattr(self.service,name)

    def bound_gateway(self,gateway):
        observer=MetricEvidenceObserver(self.service.bound_gateway(gateway))
        observer.batches=self.batches
        return observer

    def metrics_for_tool(self,workspace,name,arguments,data):
        method=getattr(self.service,'metrics_for_tool',None)
        if method is None:return None
        batch=method(workspace,name,arguments,data)
        if batch is not None:self.batches.append(deepcopy(batch))
        return batch

    def prepare_bank_operation(self,workspace,payload,*,agent_origin=False,metric_required=False):
        return self.service.prepare_bank_operation(workspace,payload,agent_origin=agent_origin,
                metric_required=metric_required,metric_batches=self.batches)

    def method_report(self,workspace,name,arguments):
        result=self.service.method_report(workspace,name,arguments)
        batch=result.get('metric_batch') if isinstance(result,dict) else None
        if batch is None:raise BankError('METRIC_BINDING_REQUIRED','金融方法未发行正式批次。',409)
        self.service.validate(workspace,batch)
        self.batches.append(deepcopy(batch))
        return result


class PublicEvidenceObserver:
    """每轮包装同一权威provider，记录模型实际读取版本，不保存第二套正文。"""
    def __init__(self, provider):
        self.provider,self.pins=provider,{}
        # RoundProvider是本请求独有对象，挂钩身份词典读取，不改共享provider。
        if type(provider).__name__=='RoundProvider':
            provider.observe_identity=self.resolve_public_evidence
            provider.observe_identity_batch=self.resolve_public_evidence_batch

    def __getattr__(self, name):return getattr(self.provider,name)

    def resolve_public_evidence(self, citation_id, release_id, as_of):
        value=self.provider.resolve_public_evidence(citation_id,release_id,as_of)
        if value is not None:
            key=(citation_id,release_id,as_of);digest=public_digest(value)
            if key in self.pins and self.pins[key]!=digest:
                raise BankError('STALE_KNOWLEDGE_EVIDENCE','本轮公开引用已修订，请重新查询。',409)
            self.pins[key]=digest
        return value

    def resolve_public_evidence_batch(self, citation_ids, release_id, as_of):
        batch=getattr(self.provider,'resolve_public_evidence_batch',None)
        if batch is None:
            return {cid:self.resolve_public_evidence(cid,release_id,as_of) for cid in citation_ids}
        values=batch(citation_ids,release_id,as_of)
        for cid,value in values.items():
            if value is None:continue
            key=(cid,release_id,as_of);digest=public_digest(value)
            if key in self.pins and self.pins[key]!=digest:
                raise BankError('STALE_KNOWLEDGE_EVIDENCE','本轮公开引用已修订，请重新查询。',409)
            self.pins[key]=digest
        return values

    def assert_fresh(self):
        """每次云调用之前回查已读原件；不向云发送已撤回身份或字段。"""
        groups={}
        for cid,release,as_of in self.pins:groups.setdefault((release,as_of),[]).append(cid)
        for (release,as_of),ids in groups.items():
            batch=getattr(self.provider,'resolve_public_evidence_batch',None)
            for offset in range(0,len(ids),64):
                part=ids[offset:offset+64]
                values=batch(part,release,as_of) if batch else {
                    cid:self.provider.resolve_public_evidence(cid,release,as_of) for cid in part}
                if any(values.get(cid) is None or public_digest(values[cid])!=self.pins[cid,release,as_of] for cid in part):
                    raise BankError('STALE_KNOWLEDGE_EVIDENCE','本轮公开原件或许可已变化，请重新查询。',409)

    def snapshot(self, knowledge):
        # public_products也通过本轮RAG逐字段回查，必须绑定最终文字/语音门。
        # pins只来自服务端读取；客户端与模型均不能提供这些摘要。
        if knowledge is None:
            if not self.pins:return None
            releases={r for _,r,_ in self.pins}
            if len(releases)!=1:
                raise BankError('KNOWLEDGE_BINDING_REQUIRED','本轮公开引用版本不一致，答复未发布。',409)
            knowledge={'release_id':next(iter(releases)),
                       'citations':[{'citation_id':c,'as_of':d} for c,_,d in self.pins]}
        else:
            knowledge=deepcopy(knowledge)
            seen={(c['citation_id'],c['as_of']) for c in knowledge['citations']}
            for cid,release,as_of in self.pins:
                if release!=knowledge['release_id']:
                    raise BankError('KNOWLEDGE_BINDING_REQUIRED','本轮公开引用版本不一致，答复未发布。',409)
                if (cid,as_of) not in seen:
                    knowledge['citations'].append({'citation_id':cid,'as_of':as_of});seen.add((cid,as_of))
        digests=[]
        for citation in knowledge['citations']:
            key=(citation['citation_id'],knowledge['release_id'],citation['as_of'])
            if key not in self.pins:
                raise BankError('KNOWLEDGE_BINDING_REQUIRED','引用缺少本轮权威读取记录，答复未发布。',409)
            digests.append({**citation,'sha256':self.pins[key]})
        return {**knowledge,'canonical_digests':digests}


class EvidenceGuardedClient:
    """请求内云客户端代理；保留原适配器、预算和使用量记录。"""
    def __init__(self,client,observer,preparation=None):
        self.client,self.observer=client,observer
        # 请求内已有prepare_round原件的副本；不增加来源读取，也不从模型取范围。
        self._review_preparation=deepcopy(preparation)
        self._original_review_preparation_digest=public_digest(self._review_preparation)
        self.notice=None
        if preparation and preparation.get('status') in ('prepared','reused','gap'):
            self.notice='本轮只核验选中公开对象的目录字段，不能据此推断即时可买、完整费率、收益或底层份额。'
            if preparation.get('coverage_gaps'):
                self.notice+='仍有对象、维度或时效未核实；只能回答已核实部分并说明缺口，比较对象不齐须追问。'
        self._original_review_notice=self.notice
        self._notice_sealed=False
        self._catalog_information=None
        if preparation and 'catalog_information' in preparation:
            from local_knowledge.catalog_information import validate_snapshot_shape, CatalogInformationObserver
            information=getattr(observer,'information',None)
            if type(information) is not CatalogInformationObserver or information.snapshot()!=preparation['catalog_information']:
                raise BankError('CATALOG_INFORMATION_BINDING_REQUIRED','历史目录缺少实际宿主固定来源。',503)
            self._catalog_information=validate_snapshot_shape(preparation['catalog_information'])
    def __getattr__(self,name):return getattr(self.client,name)
    def append_notice(self,text):
        """仅本地装配在首次估算前追加受信工具说明，公开原范围仍封口。"""
        if (self._notice_sealed or type(text) is not str or not text or len(text)>8000
                or self.notice!=self._original_review_notice
                or public_digest(self._review_preparation)!=self._original_review_preparation_digest):
            raise BankError('PUBLIC_REVIEW_SCOPE_CHANGED','受信提示已封存或原公开范围变化。',409)
        self.notice=(self.notice or '')+text
        self._original_review_notice=self.notice
    def prepare_review_packet(self,packet,*,publication_sources):
        """Agent在原始审核包投影/预算前调用；仅复用已捕获的固定来源。"""
        if (type(packet) is not dict or type(publication_sources) is not dict
                or set(publication_sources)!={'knowledge_preparation','financial_metric_snapshot'}
                or type(publication_sources['knowledge_preparation']) is not dict
                or publication_sources['financial_metric_snapshot'] is not None and type(publication_sources['financial_metric_snapshot']) is not dict):
            raise BankError('PUBLIC_REVIEW_SOURCE_INVALID','公开审核包缺少原固定来源。',503)
        if public_digest(publication_sources['knowledge_preparation'])!=self._original_review_preparation_digest:
            raise BankError('PUBLIC_REVIEW_SOURCE_CHANGED','公开审核包来源已变化。',409)
        self._before_request()
        return self._request_args((deepcopy(packet),))[0]
    def _request_args(self,args):
        if (self.notice!=self._original_review_notice or type(self.notice) is not type(self._original_review_notice)
                or public_digest(self._review_preparation)!=self._original_review_preparation_digest):
            raise BankError('PUBLIC_REVIEW_SCOPE_CHANGED','公开审核范围已变化。',409)
        self._notice_sealed=True
        if not args:return args
        value=args[0]
        if isinstance(value,dict) and 'public_evidence_limits' in value:
            if type(value['public_evidence_limits']) is not str or value['public_evidence_limits']!=self.notice:
                raise BankError('PUBLIC_REVIEW_PACKET_INVALID','公开审核范围不是本轮可信原件。',503)
        if self._catalog_information is not None and isinstance(value,dict):
            if 'catalog_information' in value and value['catalog_information']!=self._catalog_information:
                raise BankError('CATALOG_INFORMATION_SOURCE_CHANGED','历史审核包来源变化。',409)
            value={**value,'catalog_information':deepcopy(self._catalog_information)}
            args=(value,*args[1:])
        if not self.notice:return args
        if isinstance(value,list):value=[*value,{'role':'system','content':self.notice}]
        elif isinstance(value,dict):value={**value,'public_evidence_limits':self.notice}
        else:raise TypeError('模型输入格式不支持公开证据提示。')
        return (value,*args[1:])

    def request_payload(self,*args,**kwargs):
        return self.client.request_payload(*self._request_args(args),**kwargs)

    def serialized_request(self,*args,**kwargs):
        return self.client.serialized_request(*self._request_args(args),**kwargs)

    def request_input_bound(self,*args):
        # 准入与发送使用同一变换。不能在发送前上调Agent已经分配的预算。
        guarded=self._request_args(args)
        estimator=getattr(self.client,'request_input_bound',None)
        if callable(estimator):return estimator(*guarded)
        if getattr(self.client,'billing_mode',None)=='unpaid_fixture':
            return len(json.dumps(guarded,ensure_ascii=False,allow_nan=False).encode('utf-8'))
        raise BankError('AGENT_BUDGET_ESTIMATOR_UNAVAILABLE','模型请求的完整预算接口尚未接入。',503)

    def request_input_token_estimate(self,*args):
        # 原生Token估计也必须先经过与发送相同的公开提示/工具目录变换。
        guarded=self._request_args(args)
        estimator=getattr(self.client,'request_input_token_estimate',None)
        if callable(estimator):return estimator(*guarded)
        raise BankError('AGENT_BUDGET_ESTIMATOR_UNAVAILABLE','模型请求的完整Token估计接口尚未接入。',503)

    def complete(self,messages,*args,**kwargs):
        self._before_request()
        return self.client.complete(*self._request_args((messages,*args)),**kwargs)
    def review(self,packet,*args,**kwargs):
        self._before_request()
        return self.client.review(*self._request_args((packet,*args)),**kwargs)

    def _before_request(self):
        try:self.observer.assert_fresh()
        except Exception as error:
            # 只标记受信本地门拒绝；delegate异常可能已产生真实费用，不能改写。
            error.request_sent=False
            error.attempts=0
            raise

from simulated_bank import BankError


class OperationEvidence:
    def __init__(self,gateway):
        self.g,self.bank=gateway,gateway.bank
        with self.bank.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS base_operation_evidence(
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),workspace_id TEXT NOT NULL,
                snapshot TEXT NOT NULL,created_at REAL NOT NULL)''')
        self.bank.base_evidence_guard=self.execution_guard

    def current_market_connection(self,market):
        held=_held_market_connection.get()
        valid=(isinstance(held,tuple) and len(held)==5 and held[0] is self
               and held[1] is market and market is getattr(self.g,'market',None)
               and held[3]==_market_guard_owner() and held[4][0] is True)
        if valid:
            try:valid=held[2].in_transaction is True
            except (sqlite3.Error,AttributeError):valid=False
        if not valid:
            raise BankError('HISTORY_PUBLICATION_GUARD_REQUIRED','历史引文缺少原市场发布屏障。',503)
        return held[2]

    @contextmanager
    def public_guard(self, snapshot):
        with ExitStack() as locks:
            market_snapshot=snapshot.get('market')
            token=None;lease=None
            try:
                if market_snapshot:
                    market=getattr(self.g,'market',None)
                    if market is None:raise BankError('MARKET_NOT_CONFIGURED','市场证据提供端未配置。',503)
                    db=locks.enter_context(market.db());db.execute('BEGIN IMMEDIATE')
                    lease=[True]
                    token=_held_market_connection.set((self,market,db,_market_guard_owner(),lease))
                knowledge=snapshot.get('knowledge')
                if knowledge:
                    db=locks.enter_context(self.g.knowledge_provider.db());db.execute('BEGIN IMMEDIATE')
                self.assert_public_evidence(snapshot)
                yield
            finally:
                if lease is not None:lease[0]=False
                if token is not None:_held_market_connection.reset(token)

    def assert_public_evidence(self,snapshot):
        """只读出口复验；调用方已经持有bank→market→knowledge的发布锁。"""
        if snapshot.get('market'):
            from agent_runtime.bank_tools import BankTools
            BankTools.assert_market_snapshot(self.g.market,snapshot['market'])
        knowledge=snapshot.get('knowledge')
        if not knowledge:return
        if not isinstance(knowledge,dict) or not isinstance(knowledge.get('citations'),list) or not knowledge['citations']:
            raise BankError('STALE_KNOWLEDGE_EVIDENCE','公开引用快照无效，请重新准备。',409)
        for citation in knowledge['citations']:
            current=self.g.knowledge_provider.resolve_public_evidence(citation['citation_id'],knowledge['release_id'],citation['as_of'])
            if current is None:
                raise BankError('STALE_KNOWLEDGE_EVIDENCE','公开条款或来源已失效，请重新准备。',409)
            if 'canonical_digests' in knowledge:
                expected=next((r['sha256'] for r in knowledge['canonical_digests'] if
                               r['citation_id']==citation['citation_id'] and r['as_of']==citation['as_of']),None)
                if expected!=public_digest(current):
                    raise BankError('STALE_KNOWLEDGE_EVIDENCE','公开正文、版本或准入条件已变化，请重新准备。',409)

    def bind_answer(self, workspace, result, db):
        """仅服务最终发布闸门调用；接口不接收客户端提交的证据快照。"""
        actions=result.get('bank_actions',[])
        # 旧对照接口可返回尚未准备的建议参数；没有operation_id就没有银行授权对象。
        actions=[a for a in actions if a.get('operation_id')]
        if not actions:return
        if result.get('isFallback') or result.get('compliance_pass') is not True:
            raise BankError('AGENT_REVIEW_REQUIRED','答复没有通过核验，不能放行草案。',409)
        snapshot={'market':result.get('market_snapshot'),'knowledge':result.get('knowledge_snapshot'),
                  'service':result.get('service_snapshot'),'metric_batches':result.get('financial_metric_batches',[])}
        if snapshot['knowledge'] and not snapshot['knowledge'].get('canonical_digests'):
            raise BankError('KNOWLEDGE_BINDING_REQUIRED','草案缺少公开引用的版本摘要。',409)
        body=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
        for action in actions:
            operation_id=action['operation_id']
            self.bank._operation(db,workspace,operation_id)
            review=db.execute('SELECT status FROM agent_draft_reviews WHERE operation_id=? AND workspace_id=?',
                              (operation_id,workspace)).fetchone()
            if not review or review['status']!='approved':
                raise BankError('AGENT_REVIEW_REQUIRED','草案未通过独立审核。',409)
            old=db.execute('SELECT snapshot FROM base_operation_evidence WHERE operation_id=?',(operation_id,)).fetchone()
            if old:
                if old['snapshot']!=body:raise BankError('OPERATION_EVIDENCE_CHANGED','不能覆盖原草案引用，请重新准备。',409)
                continue
            db.execute('INSERT INTO base_operation_evidence VALUES(?,?,?,?)',
                       (operation_id,workspace,body,self.bank.clock()))
            self.bank._audit(db,workspace,'operation_public_evidence_bound',{'has_market':bool(snapshot['market']),
                             'has_knowledge':bool(snapshot['knowledge'])},operation_id)

    @contextmanager
    def execution_guard(self, db, workspace, operation):
        row=db.execute('SELECT workspace_id,snapshot FROM base_operation_evidence WHERE operation_id=?',
                       (operation['id'],)).fetchone()
        agent=db.execute('SELECT 1 FROM agent_draft_reviews WHERE operation_id=?',(operation['id'],)).fetchone()
        if row is None:
            if agent:raise BankError('OPERATION_EVIDENCE_REQUIRED','草案尚未完成最终证据绑定，请重新查询。',409)
            yield;return
        if row['workspace_id']!=workspace:raise BankError('WORKSPACE_CHANGED','草案证据不属于当前账户。',409)
        snapshot=json.loads(row['snapshot'])
        for batch in snapshot.get('metric_batches',[]):self.g.financial_metrics.validate(workspace,batch,db)
        # 锁持续到银行交易提交；公开资料的撤回不能插在回查和扣款之间。
        with ExitStack() as locks:
            locks.enter_context(self.public_guard(snapshot))
            if snapshot.get('service'):
                locks.enter_context(self.g.service_negotiations.evidence_guard(workspace,snapshot['service'],db=db))
            self.assert_public_evidence(snapshot)
            if snapshot.get('service'):self.g.service_negotiations.assert_evidence(workspace,snapshot['service'],db=db)
            yield
