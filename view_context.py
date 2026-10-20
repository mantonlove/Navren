"""短期、会话绑定的跨页显式提问。URL只出现随机票据，不出现个人问题和金额。"""
import hashlib
import re
import threading
import time
import uuid
from copy import deepcopy
from simulated_bank import BankError
from simulated_bank.service import strict_fields
from user_memory import privacy_revision
from contextlib import closing

PAGES={'account','mine','recommend','chat','copilot'}
VIEW_FIELDS={'page','view','object_kind','object_id','source_scope','portfolio_id','object_version','as_of','date','period','edition_date','revision','time_view','filters','selected','return_context_id','workspace_id','privacy_revision','scrollposition','focustarget','listposition'}


class PageContexts:
    def __init__(self,gateway):
        self.g=gateway;self.lock=threading.Lock();self.entries={}

    def revision(self,w):
        with closing(self.g.bank.connect()) as db:return privacy_revision(db,w)

    @staticmethod
    def safe_view(value):
        if not isinstance(value,dict) or set(value)-VIEW_FIELDS or value.get('page') not in PAGES:
            raise BankError('PAGE_CONTEXT_INVALID','返回视图无效。')
        def safe(v,depth=0):
            if depth>4:raise BankError('PAGE_CONTEXT_INVALID','视图状态层级过深。')
            if v is None or isinstance(v,(bool,int,float)):return v
            if isinstance(v,str) and len(v)<=512:return v
            if isinstance(v,list) and len(v)<=10:return [safe(a,depth+1) for a in v]
            if isinstance(v,dict) and len(v)<=20:return {k:safe(a,depth+1) for k,a in v.items() if isinstance(k,str) and len(k)<=80}
            raise BankError('PAGE_CONTEXT_INVALID','返回状态过大。')
        result=safe(value)
        result.pop('workspace_id',None);result.pop('privacy_revision',None)
        return result

    def resolve(self,w,ref):
        if ref is None:return None
        if self._history_requested(ref):
            reference, local_binding = self._resolve_history(w, ref)
            return reference
        strict_fields(ref,['kind','id','source_scope','version','label','parameters'],['kind','id'])
        kind=ref['kind'];rid=ref['id'];source=ref.get('source_scope','simulation');expected=ref.get('version')
        if not isinstance(rid,str) or not 1<=len(rid)<=128:raise BankError('OBJECT_REFERENCE_INVALID','对象标识无效。')
        g=self.g;obj=None;actual=None;parameters=ref.get('parameters',{})
        if kind in ('event','article','weekly'):
            if not getattr(g,'market',None):raise BankError('MARKET_UNAVAILABLE','市场资料暂不可用。',503)
            if not isinstance(parameters,dict) or set(parameters)-{'edition_date','revision','event_version'}:
                raise BankError('OBJECT_REFERENCE_INVALID','市场引用参数无效。')
            try:
                if parameters:
                    if kind!='event' or set(parameters)!={'edition_date','revision','event_version'}:
                        raise BankError('OBJECT_REFERENCE_INVALID','历史热点需要完整的归档日、版次与事件版本。')
                    from market_daily import MarketDailyError
                    try:
                        obj=g.market.history_event(parameters['edition_date'],rid,parameters['revision'],expected_version=parameters['event_version'])
                    except MarketDailyError as error:
                        message={400:'历史热点的日期或版次无效，请重新选择。',404:'这条事件不在指定的历史精选中。',409:'历史事件版本不一致，请重新查阅。'}.get(error.status,'历史热点暂时无法核对。')
                        raise BankError(error.code,message,error.status) from error
                    if obj.get('availability')!='active':
                        raise BankError('MARKET_ARCHIVE_UNAVAILABLE','该历史资料已失效，请先重新查阅状态。',409)
                else:
                    obj={'event':g.market.event,'article':g.market.article,'weekly':g.market.weekly}[kind](rid)
                if kind=='weekly':obj=obj.get('report')
            except KeyError:obj=None
            source='market';actual=obj.get('version') if obj else None
        elif kind=='graph':
            if not isinstance(parameters,dict) or set(parameters)-{'node_id','edge_id','insight_id','view','from','to','source','portfolio','accounting_basis','expected_version'}:raise BankError('OBJECT_REFERENCE_INVALID','关系图参数无效。')
            obj=g.financial_graph.query(w,parameters);actual=obj['version'];source=parameters.get('source','simulation')
        elif kind=='calendar_item':
            obj=g.calendar.item(w,rid);actual=obj.get('source_revision',obj.get('version'));source=obj.get('source_type','simulation')
        elif kind=='calendar':
            if not isinstance(parameters,dict) or set(parameters)-{'from','to','view','source','portfolio','accounting_basis','kinds','include_life_events','simulation_overlay','period'}:raise BankError('OBJECT_REFERENCE_INVALID','日历参数无效。')
            obj=g.calendar.query(w,parameters);actual=obj.get('version',obj.get('source_revision'));source=parameters.get('source','simulation')
        elif kind=='product':
            if source=='bank_product':
                obj=next((p for p in g.bank.products(w) if p['id']==rid),None);actual=obj.get('version') if obj else None
            elif source in ('public_catalog','public_nav'):
                catalog=g.public_products.read(rid,40)
                if source=='public_catalog':obj=next((p for p in catalog['products'] if str(p['code'])==rid),None);actual=catalog.get('sha256')
                else:
                    obj=next((p for p in catalog['verified_nav_products'] if str(p['product'].get('fund_id',p['product'].get('code')))==rid),None)
                    if not obj:
                        from fund_history import FUNDS,cached
                        obj=cached(rid) if rid in FUNDS else None
                    actual=obj.get('version') if obj else None
            else:raise BankError('OBJECT_REFERENCE_INVALID','产品来源未指明。')
        elif kind=='goal':
            obj=g.goals.state(w,rid)['goal'];actual=obj['object_version'];source='simulation'
        elif kind=='research_plan':
            obj=next((p for p in g.research_plans.read(w)['items'] if p['id']==rid),None);actual=obj['object_version'] if obj else None;source='server_research'
        elif kind=='operation':
            obj=g.operation_view(w,rid);actual=obj['operation'].get('version');source='simulation'
        elif kind=='report':
            if not isinstance(parameters,dict) or set(parameters)-{'month','year'}:raise BankError('OBJECT_REFERENCE_INVALID','报告引用期间参数无效。')
            if parameters.get('month') is not None and (not isinstance(parameters['month'],str) or not re.fullmatch(r'\d{4}-\d{2}',parameters['month'])):raise BankError('OBJECT_REFERENCE_INVALID','报告月份无效。')
            if parameters.get('year') is not None and (type(parameters['year'])is not int or not 2025<=parameters['year']<=2100):raise BankError('OBJECT_REFERENCE_INVALID','报告年份无效。')
            state=g.source(w)
            if source not in state['sources']:raise BankError('INVALID_SOURCE','账单来源无效。')
            actual=state['sources'][source]['source_version'];obj=state if actual is not None else None
        else:
            table={'account':'accounts','card':'cards','holding':'holdings','loan':'loans','deposit':'deposit_contracts',
                   'subscription':'subscriptions','mandate':'mandates','task':'orchestration_tasks','plan':'orchestration_tasks',
                   'support':'support_cases','permission':'permission_grants','scheduled_payment':'scheduled_payments','split_bill':'split_bills'}.get(kind)
            if table:
                source='simulation'
                archive=g.bank.customer_archive(w)
                rows=archive.get('accounts',[]) if kind=='account' else archive['records'].get(table,[])
                if kind=='holding':rows=archive['records'].get('holdings',[])
                obj=next((r for r in rows if (str(r.get('account_id'))+'|'+str(r.get('product_id')) if kind=='holding' else r.get('id'))==rid),None);actual=archive['version']
            else:raise BankError('OBJECT_REFERENCE_INVALID','尚不支持此类对象引用，请移除引用后提问。')
        if obj is None:raise BankError('OBJECT_NOT_FOUND','引用对象不存在、已删除或不属于当前账户。',404)
        if expected is not None and (actual is None or str(expected)!=str(actual)):raise BankError('OBJECT_VERSION_CONFLICT','引用对象版本已变化或无法核对，请重新查阅后提问。',409)
        # 公共资料的标题来自本轮核对的对象，不能把浏览器提交的标签当作原文。
        label=(obj.get('headline') or obj.get('title_cn') or obj.get('title')
               if kind in ('event','article','weekly') else None)
        return {'kind':kind,'id':rid,'source_scope':source,'version':actual,'parameters':deepcopy(parameters),
                'label':str(label or ref.get('label',rid))[:100],'execution_authority':False}

    def create(self,w,token,p):
        strict_fields(p,['question','reference','return_view','draft_only'],['question','return_view'])
        draft_only=p.get('draft_only',False)
        if type(draft_only) is not bool:raise BankError('PAGE_CONTEXT_INVALID','提问草稿状态无效。')
        question=p['question']
        if not isinstance(question,str) or len(question.strip())>6000:raise BankError('INVALID_TEXT','问题不能超过6000字。')
        history_binding=None
        if self._history_requested(p.get('reference')):
            initial_revision=self.revision(w)
            reference,history_binding=self._resolve_history(w,p['reference'])
            if initial_revision!=self.revision(w):
                raise BankError('PAGE_CONTEXT_REVOKED','资料授权已变化，请重新选择历史版本。',409)
        else:
            reference=self.resolve(w,p.get('reference'))
        if not question.strip() and reference is None:raise BankError('PAGE_CONTEXT_INVALID','请先输入问题或选择要关联的资料。')
        request_mode=('reference_question' if question.strip() else 'reference_overview') if reference else 'question'
        result={'id':uuid.uuid4().hex,'question':question.strip(),'draft_only':draft_only,'reference':reference,'request_mode':request_mode,
                'return_view':self.safe_view(p['return_view']),'workspace_id':w,'privacy_revision':self.revision(w)}
        result.update(expires_at=time.time()+1200,claimed=False,used=False,token=hashlib.sha256(token.encode()).hexdigest())
        if history_binding is not None:
            if result['privacy_revision']!=initial_revision:
                raise BankError('PAGE_CONTEXT_REVOKED','资料授权已变化，请重新选择历史版本。',409)
            expected_view={'page':'recommend','view':'weekly' if reference['kind']=='weekly' else 'reading',
                'object_kind':reference['kind'],'object_id':reference['id'],'object_version':reference['version'],
                'time_view':'historical_audit'}
            if any(k in result['return_view'] and result['return_view'][k]!=v for k,v in expected_view.items()):
                raise BankError('HISTORY_SELECTION_INVALID','返回视图与所选历史版本不一致。',400)
            if reference['kind']=='weekly' and result['return_view'].get('period',reference['id'])!=reference['id']:
                raise BankError('HISTORY_SELECTION_INVALID','返回周报期号与所选历史版本不一致。',400)
            result['return_view'].update(expected_view)
            result['return_view']['source_scope']='public_market'
            if reference['kind']=='weekly':result['return_view']['period']=reference['id']
            result['_history_binding']=deepcopy(history_binding)
        with self.lock:
            self.entries={k:v for k,v in self.entries.items() if v['expires_at']>time.time()}
            if sum(v['workspace_id']==w for v in self.entries.values())>=32:raise BankError('PAGE_CONTEXT_LIMIT','待处理提问过多，请先完成已有提问。',429)
            self.entries[result['id']]=result
        return {'context_id':result['id'],'expires_at':result['expires_at'],'workspace_id':w,'privacy_revision':result['privacy_revision']}

    def _entry(self,w,token,cid):
        item=self.entries.get(cid)
        if not item or item['workspace_id']!=w or item['token']!=hashlib.sha256(token.encode()).hexdigest():raise BankError('PAGE_CONTEXT_NOT_FOUND','提问票据不属于当前会话。',404)
        if item['expires_at']<=time.time():raise BankError('PAGE_CONTEXT_EXPIRED','提问已过期，请回到原页面重新发送。',409)
        if item['privacy_revision']!=self.revision(w):raise BankError('PAGE_CONTEXT_REVOKED','资料授权已变化，旧提问已失效。',409)
        return item

    def claim(self,w,token,cid):
        with self.lock:
            item=self._entry(w,token,cid)
            if item.get('_history_binding') is not None:
                api=self._history_api();market=getattr(self.g,'market',None)
                if market is None:raise BankError('MARKET_HISTORY_UNAVAILABLE','历史资料暂不可用。',503)
                with market.db() as history_db:
                    history_db.execute('BEGIN')
                    api.assert_local_binding(market,item['_history_binding'],db=history_db)
            fresh=not item['claimed'];item['claimed']=True
            return {k:deepcopy(v) for k,v in item.items() if k not in ('token','expires_at','_history_binding')}|{'open_once':fresh and not item['used'],'send_once':fresh and not item['used'] and not item['draft_only']}

    def consume(self,w,token,cid,text):
        with self.lock:
            item=self._entry(w,token,cid)
            if item['draft_only']:raise BankError('PAGE_CONTEXT_DRAFT','这是聊天草稿，请在对话页确认问题后发送。',409)
            if item['used']:raise BankError('PAGE_CONTEXT_CONSUMED','此提问已经发送，请查询已有答复；不会自动重复提交。',409)
            if text.strip()!=item['question']:raise BankError('PAGE_CONTEXT_MISMATCH','问题已修改，请移除旧引用后重新发送。',409)
            if item.get('_history_binding') is not None:
                api=self._history_api()
                market=getattr(self.g,'market',None)
                if market is None:raise BankError('MARKET_HISTORY_UNAVAILABLE','历史资料暂不可用。',503)
                with market.db() as history_db:
                    history_db.execute('BEGIN IMMEDIATE')
                    api.assert_local_binding(market,item['_history_binding'],db=history_db)
                    reference=deepcopy(item['reference'])
                    reference['request_mode']=item['request_mode']
                    issued=api.issue_host_context(reference,item['_history_binding'],owner=self,workspace=w,
                        session_hash=item['token'],expires_at=item['expires_at'])
                item['used']=True
                return issued
            raw={k:v for k,v in item['reference'].items() if k!='execution_authority'} if item['reference'] else None
            ref=self.resolve(w,raw) if raw else None
            if ref is not None:ref['request_mode']=item['request_mode']
            item['used']=True
            return ref

    @staticmethod
    def _history_requested(ref):
        # 普通引用不加载历史消费模块；仅显式历史参数进入独立合同。
        return isinstance(ref,dict) and (isinstance(ref.get('parameters'),dict)
            and 'reading_mode' in ref['parameters'] or 'reading_mode' in ref)

    @staticmethod
    def _history_api():
        try:
            from agent_runtime import history_binding
            for name in ('MODE','checked_request','read_exact','make_local_binding',
                         'assert_local_binding','issue_host_context'):
                if not hasattr(history_binding,name):raise ImportError('history interface incomplete')
            return history_binding
        except ImportError as error:
            raise BankError('MARKET_HISTORY_UNAVAILABLE','历史查阅接口尚未完整接线，请稍后重试。',503) from error

    def _resolve_history(self,w,ref):
        strict_fields(ref,['kind','id','source_scope','version','label','parameters'],['kind','id','version','parameters'])
        if ref.get('source_scope')!='market' or ref.get('parameters')!={'reading_mode':'historical_audit'}:
            raise BankError('HISTORY_SELECTION_INVALID','历史引用需要明确公开来源与历史复核模式。',400)
        api=self._history_api()
        MODE,checked_request,read_exact,make_local_binding=(api.MODE,api.checked_request,api.read_exact,api.make_local_binding)
        requested=checked_request({'kind':ref['kind'],'id':ref['id'],'version':ref['version'],'reading_mode':MODE})
        market=getattr(self.g,'market',None)
        if market is None:raise BankError('MARKET_HISTORY_UNAVAILABLE','历史资料暂不可用。',503)
        with market.db() as history_db:
            history_db.execute('BEGIN')
            packet=read_exact(market,requested,db=history_db)
            binding=make_local_binding(market,packet,db=history_db)
        body=packet['data'].get('article') if requested['kind']=='article' else packet['data'].get('report')
        if not isinstance(body,dict):raise BankError('HISTORY_SELECTION_INVALID','历史版本正文无法核对。',409)
        label=body.get('headline') or body.get('title_cn') or body.get('title') or requested['id']
        return {'kind':requested['kind'],'id':requested['id'],'source_scope':'market','version':requested['version'],
                'parameters':{'reading_mode':MODE},'label':str(label)[:100],'execution_authority':False,
                'reading_mode':MODE,'historical_only':True,'current_evidence_eligible':False},binding
