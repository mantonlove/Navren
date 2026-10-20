"""受信展示对象的短期建议引用；不生成金融事实、结论或执行授权。"""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from agent_runtime.metric_claims import MetricClaimError, monetary_occurrences
from financial_metrics import fingerprint
from simulated_bank import BankError
from simulated_bank.service import digest


CONTRACT = 'navren.view-followups/1'
LOADED_CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
TEMPLATE_INTENTS = {'read_selected_detail':'open_view', 'explain_basis_and_gaps':'ask'}
_PRIVILEGED = re.compile(r'申购|赎回|买入|卖出|转账|支付|扣款|下单|开户|申请|授权|确认执行|执行交易|取消订阅|提高额度')
_URL = re.compile(r'(?:[a-z][a-z0-9+.-]*://|www\.|(?:href|src)\s*=|<[^>]+>)', re.I)


def reject(code='VIEW_FOLLOWUP_INVALID', status=422):
    raise BankError(code,'建议引用、受信模板或所选展示对象无效，请重新读取。',status)


class ViewFollowups:
    """仅供受信服务器调用；发行入口不接受浏览器或模型自由模板。"""
    def __init__(self, gateway, *, ttl_seconds=300, max_suggestions=128, clock=None):
        if (type(ttl_seconds) not in (int,float) or not 0<ttl_seconds<=3600
                or type(max_suggestions) is not int or not 1<=max_suggestions<=512):reject()
        self.g,self.bank,self.presentations = gateway,gateway.bank,gateway.presentations
        self.metrics=gateway.financial_metrics
        self.ttl,self.maximum,self.clock=ttl_seconds,max_suggestions,clock or time.monotonic
        self._suggestions,self._groups,self._resolutions,self._lock=OrderedDict(),OrderedDict(),OrderedDict(),threading.RLock()

    @staticmethod
    def _current(request,message):
        if (type(request) is not str or not request.strip() or len(request)>160
                or type(message) is not str or message!=request):reject('VIEW_FOLLOWUP_BINDING',403)

    @staticmethod
    def _templates(values):
        if type(values) is not list or not 1<=len(values)<=3:reject()
        result=[];seen=set()
        for value in values:
            if (type(value) is not dict or set(value)!={'template_id','intent','label','text','selection_ref'}
                    or type(value['template_id']) is not str or value['template_id'] not in TEMPLATE_INTENTS
                    or value['intent']!=TEMPLATE_INTENTS[value['template_id']]):reject()
            selection=value['selection_ref']
            if (type(selection) is not dict or len(selection)!=1 or not set(selection)<= {'point_ref','item_ref'}
                    or any(type(v) is not str or not v.strip() or len(v)>512 for v in selection.values())):reject()
            for field,maximum in (('label',80),('text',400)):
                text=value[field]
                if (type(text) is not str or not text.strip() or len(text)>maximum
                        or _URL.search(text) or _PRIVILEGED.search(text)):reject('VIEW_FOLLOWUP_TEMPLATE')
                try:
                    if monetary_occurrences(text):reject('VIEW_FOLLOWUP_TEMPLATE')
                except MetricClaimError:reject('VIEW_FOLLOWUP_TEMPLATE')
            key=(value['template_id'],fingerprint(selection))
            if key in seen:reject()
            seen.add(key);result.append(deepcopy(value))
        return result

    @contextmanager
    def _read(self,bank_db=None):
        if bank_db is not None:
            if not isinstance(bank_db,sqlite3.Connection):reject('VIEW_FOLLOWUP_DATABASE',403)
            try:paths=[r[2] for r in bank_db.execute('PRAGMA database_list') if r[1]=='main']
            except sqlite3.Error:reject('VIEW_FOLLOWUP_DATABASE',403)
            if (not bank_db.in_transaction or len(paths)!=1
                    or Path(paths[0]).resolve()!=Path(self.bank.database).resolve()):reject('VIEW_FOLLOWUP_DATABASE',403)
            yield bank_db
        else:
            with self.bank.transaction() as db:yield db

    def _identity(self,w,db):
        sessions=getattr(self.g,'demo_sessions',None)
        context=getattr(sessions,'_request',None)
        request=context.get() if context is not None else None
        if not request or len(request)!=2 or request[1]!=w:reject('VIEW_FOLLOWUP_IDENTITY_REQUIRED',403)
        guard=getattr(self.bank,'request_identity_guard',None)
        if not callable(guard):reject('VIEW_FOLLOWUP_IDENTITY_REQUIRED',403)
        guard(db)
        token_hash=digest(request[0])
        row=db.execute('SELECT * FROM demo_sessions WHERE token_hash=?',(token_hash,)).fetchone()
        if row:
            if row['workspace_id']!=w or row['revoked'] or row['expires_at']<=self.bank.clock():reject('SESSION_CHANGED',409)
            return {'workspace_binding':w,'token_hash':token_hash,'generation':row['generation'],'mode':'demo'}
        row=db.execute('SELECT token_hash FROM workspaces WHERE id=?',(w,)).fetchone()
        if not row or not hmac.compare_digest(row[0],token_hash):reject('SESSION_CHANGED',409)
        return {'workspace_binding':w,'token_hash':token_hash,'generation':0,'mode':'legacy'}

    @staticmethod
    def _selection(stored,selection):
        key,value=next(iter(selection.items()))
        if key=='point_ref':owned=any(p.get('point_ref')==value for p in stored['presentation']['points'])
        else:
            visible={point.get('item_ref') for point in stored['presentation']['points']}
            visible.update(item.get('item_ref') for point in stored['presentation']['points'] for item in point.get('preview',[]))
            owned=value in visible and any(item.get('id')==value for item in stored['items'])
        if not owned:reject('PRESENTATION_SELECTOR',403)

    def _parent(self,w,vid,parent_hash,selection,db,identity=None):
        actual=self._identity(w,db)
        if identity is not None and actual!=identity:reject('VIEW_FOLLOWUP_IDENTITY_CHANGED',409)
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=LOADED_CODE:reject('VIEW_FOLLOWUP_RESTART_REQUIRED',503)
        stored=self.presentations._stored(w,vid,db)
        context=stored.get('trusted_context')
        if not context or context.get('identity')!=actual:reject('PRESENTATION_BINDING',403)
        if stored['presentation']['projection_hash']!=parent_hash:reject('PRESENTATION_TAMPERED',409)
        self._selection(stored,selection)
        return stored,actual

    @staticmethod
    def _object(template,vid,parent_hash,identifier=None,signature=None,reason=None):
        return {'schema_version':1,'id':identifier,'template_id':template['template_id'],
            'intent':template['intent'],'type':'send_message' if template['intent']=='ask' else 'open_view',
            'label':template['label'],'text':template['text'],
            'context_ref':{'view_id':vid,'parent_projection_hash':parent_hash,'selection_ref':deepcopy(template['selection_ref'])},
            'availability':'unavailable' if reason else 'available','reason':reason,'object_version':parent_hash,
            'suggestion_ref':{'suggestion_id':identifier,'service_signature':signature} if identifier else None}

    @staticmethod
    def _valid_reference(ref,key,prefix):
        return (type(ref) is dict and set(ref)=={key,'service_signature'}
            and type(ref[key]) is str and re.fullmatch(re.escape(prefix)+r'[a-f0-9]{32}',ref[key]) is not None
            and type(ref['service_signature']) is str and re.fullmatch(r'[a-f0-9]{64}',ref['service_signature']) is not None)

    def suggestions(self,w,view_id,expected_parent_projection_hash,trusted_templates,*,
                    trusted_current_request,trusted_current_message,bank_db=None):
        """模板由服务器登记窗口提供，所有对象选择仍由发行服务亲自核验。"""
        self._current(trusted_current_request,trusted_current_message)
        templates=self._templates(trusted_templates)
        if len(templates)>self.maximum:reject()
        if (type(view_id) is not str or not view_id or type(expected_parent_projection_hash) is not str
                or len(expected_parent_projection_hash)!=64):reject()
        result=[];group_identity=None
        try:
            with self._read(bank_db) as db:
                group_identity=self._identity(w,db)
                for template in templates:
                    try:
                        stored,identity=self._parent(w,view_id,expected_parent_projection_hash,template['selection_ref'],db)
                        pres=stored['presentation']
                        if (pres['request_id']!=trusted_current_request or pres['message_id']!=trusted_current_message):
                            reject('VIEW_FOLLOWUP_BINDING',403)
                        identifier='view-followup-'+secrets.token_hex(16)
                        body={'contract':CONTRACT,'id':identifier,'workspace_binding':w,
                            'parent_view_id':view_id,'parent_projection_hash':expected_parent_projection_hash,
                            'parent_request_id':pres['request_id'],'parent_message_id':pres['message_id'],
                            'source_versions':deepcopy(pres['source_versions']),
                            'authorization_revision':deepcopy(pres['authorization_revision']),
                            'trusted_context':deepcopy(stored['trusted_context']),
                            'identity':identity,'template':template,'helper_code':LOADED_CODE,
                            'expires_at':self.clock()+self.ttl}
                        signature=self.metrics._tool_signature(body)
                        item=self._object(template,view_id,expected_parent_projection_hash,identifier,signature)
                        with self._lock:
                            self._suggestions[identifier]={'body':body,'signature':signature}
                            while len(self._suggestions)>self.maximum:self._suggestions.popitem(last=False)
                        result.append(item)
                    except BankError as error:
                        result.append(self._object(template,view_id,expected_parent_projection_hash,reason=error.code))
        except BankError as error:
            result=[self._object(template,view_id,expected_parent_projection_hash,reason=error.code) for template in templates]
        group_id='view-followup-batch-'+secrets.token_hex(16)
        body={'contract':CONTRACT,'batch_id':group_id,'workspace_binding':w,
            'request_id':trusted_current_request,'message_id':trusted_current_message,
            'identity':group_identity,'items':deepcopy(result),'helper_code':LOADED_CODE,'expires_at':self.clock()+self.ttl}
        signature=self.metrics._tool_signature(body)
        ref={'batch_id':group_id,'service_signature':signature}
        for item in result:item['suggestion_batch_ref']=deepcopy(ref)
        with self._lock:
            self._groups[group_id]={'body':body,'signature':signature,'items':deepcopy(result)}
            while len(self._groups)>self.maximum:self._groups.popitem(last=False)
        return result

    def review_projection(self,w,issued_suggestions,*,trusted_current_request,trusted_current_message,bank_db=None):
        """Owner审阅/发布/保存前回查整批亲发原件；不能漏条或审核后追加。"""
        self._current(trusted_current_request,trusted_current_message)
        if type(issued_suggestions) is not list or not 1<=len(issued_suggestions)<=3:reject('VIEW_FOLLOWUP_REVIEW',409)
        refs=[item.get('suggestion_batch_ref') if type(item) is dict else None for item in issued_suggestions]
        if (any(type(ref) is not dict or set(ref)!={'batch_id','service_signature'} for ref in refs)
                or any(ref!=refs[0] for ref in refs)):reject('VIEW_FOLLOWUP_REVIEW',409)
        ref=refs[0]
        if not self._valid_reference(ref,'batch_id','view-followup-batch-'):reject('VIEW_FOLLOWUP_REVIEW',409)
        with self._lock:metadata=deepcopy(self._groups.get(ref['batch_id']))
        if metadata is None:reject('VIEW_FOLLOWUP_NOT_ISSUED',404)
        body=metadata['body']
        if (body['workspace_binding']!=w or body['request_id']!=trusted_current_request
                or body['message_id']!=trusted_current_message):reject('VIEW_FOLLOWUP_SCOPE',403)
        if (not hmac.compare_digest(ref['service_signature'],metadata['signature'])
                or not hmac.compare_digest(metadata['signature'],self.metrics._tool_signature(body))
                or issued_suggestions!=metadata['items']):reject('VIEW_FOLLOWUP_TAMPERED',409)
        if self.clock()>=body['expires_at']:reject('VIEW_FOLLOWUP_EXPIRED',410)
        with self._read(bank_db) as db:
            if body['identity'] is None or self._identity(w,db)!=body['identity']:reject('VIEW_FOLLOWUP_IDENTITY_CHANGED',409)
            if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=LOADED_CODE:reject('VIEW_FOLLOWUP_RESTART_REQUIRED',503)
            for item in metadata['items']:
                if item['availability']=='available':
                    suggestion=self._reference(w,item['suggestion_ref'])['body']
                    if self.clock()>=suggestion['expires_at']:reject('VIEW_FOLLOWUP_EXPIRED',410)
                    self._parent(w,suggestion['parent_view_id'],suggestion['parent_projection_hash'],
                        suggestion['template']['selection_ref'],db,suggestion['identity'])
        packet={'schema_version':1,'contract':CONTRACT,'suggestions':deepcopy(metadata['items']),
            'suggestion_batch_ref':deepcopy(ref),'request_id':trusted_current_request,'message_id':trusted_current_message,
            'deterministically_validated':True,'semantic_review_status':'owner_wiring_required',
            'final_publication_status':'owner_wiring_required'}
        packet['projection_hash']=fingerprint(packet)
        return packet

    def _reference(self,w,suggestion_ref):
        if not self._valid_reference(suggestion_ref,'suggestion_id','view-followup-'):reject()
        with self._lock:metadata=deepcopy(self._suggestions.get(suggestion_ref['suggestion_id']))
        if metadata is None:reject('VIEW_FOLLOWUP_NOT_ISSUED',404)
        body=metadata['body']
        if body['workspace_binding']!=w:reject('VIEW_FOLLOWUP_SCOPE',403)
        if (not hmac.compare_digest(suggestion_ref['service_signature'],metadata['signature'])
                or not hmac.compare_digest(metadata['signature'],self.metrics._tool_signature(body))):reject('METRIC_TAMPERED',409)
        return metadata

    def resolve(self,w,suggestion_ref,*,trusted_current_request,trusted_current_message,bank_db=None):
        """消费接口不接收客户 text/type/context，也不迁移旧轮金额或批准。"""
        self._current(trusted_current_request,trusted_current_message)
        metadata=self._reference(w,suggestion_ref);body=metadata['body'];template=body['template']
        base=self._object(template,body['parent_view_id'],body['parent_projection_hash'],body['id'],metadata['signature'])
        resolution_id=None
        try:
            if trusted_current_request==body['parent_request_id']:reject('VIEW_FOLLOWUP_NEW_REQUEST_REQUIRED',403)
            if self.clock()>=body['expires_at']:reject('VIEW_FOLLOWUP_EXPIRED',410)
            with self._read(bank_db) as borrowed_db, self.presentations._reissue_atomic(borrowed_db) as db:
                stored,_=self._parent(w,body['parent_view_id'],body['parent_projection_hash'],template['selection_ref'],db,body['identity'])
                if (stored['trusted_context']!=body['trusted_context']
                        or stored['presentation']['source_versions']!=body['source_versions']
                        or stored['presentation']['authorization_revision']!=body['authorization_revision']):reject('PRESENTATION_STALE',409)
                child=self.presentations.reissue_selected_detail(w,body['parent_view_id'],deepcopy(template['selection_ref']),
                    body['parent_projection_hash'],trusted_current_request=trusted_current_request,
                    trusted_current_message=trusted_current_message,bank_db=db)
                self._parent(w,body['parent_view_id'],body['parent_projection_hash'],template['selection_ref'],db,body['identity'])
                current=self._reference(w,suggestion_ref)
                if current!=metadata:reject('VIEW_FOLLOWUP_TAMPERED',409)
                if self.clock()>=body['expires_at']:reject('VIEW_FOLLOWUP_EXPIRED',410)
                pres=child['presentation']
                result={**base,'context_ref':{'view_id':pres['view_id'],'parent_view_id':body['parent_view_id'],
                    'projection_hash':pres['projection_hash'],'request_id':trusted_current_request,'message_id':trusted_current_message},
                    'result':deepcopy(child),'readonly':True,'execution_authority':False,
                    'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}
                resolution_id='view-followup-resolution-'+secrets.token_hex(16)
                resolution={'contract':CONTRACT,'resolution_id':resolution_id,'workspace_binding':w,
                    'request_id':trusted_current_request,'message_id':trusted_current_message,
                    'suggestion_id':body['id'],'identity':deepcopy(body['identity']),
                    'output':deepcopy(result),'expires_at':min(body['expires_at'],self.clock()+self.ttl)}
                signature=self.metrics._tool_signature(resolution)
                result['resolution_ref']={'resolution_id':resolution_id,'service_signature':signature}
                if self.clock()>=resolution['expires_at']:reject('VIEW_FOLLOWUP_EXPIRED',410)
                with self._lock:
                    self._resolutions[resolution_id]={'body':resolution,'signature':signature,'output':deepcopy(result)}
                    while len(self._resolutions)>self.maximum:self._resolutions.popitem(last=False)
                return result
        except BankError as error:
            if resolution_id is not None:
                with self._lock:self._resolutions.pop(resolution_id,None)
            return {**base,'availability':'unavailable','reason':error.code,'result':None,'execution_authority':False}
        except BaseException:
            if resolution_id is not None:
                with self._lock:self._resolutions.pop(resolution_id,None)
            raise

    def review_resolved_projection(self,w,resolved,*,trusted_current_request,trusted_current_message,bank_db=None):
        """跨轮结果的正文模板和新子对象一起回查，仍待Owner完整语义审核。"""
        self._current(trusted_current_request,trusted_current_message)
        ref=resolved.get('resolution_ref') if type(resolved) is dict else None
        if not self._valid_reference(ref,'resolution_id','view-followup-resolution-'):reject('VIEW_FOLLOWUP_REVIEW',409)
        with self._lock:metadata=deepcopy(self._resolutions.get(ref['resolution_id']))
        if metadata is None:reject('VIEW_FOLLOWUP_NOT_ISSUED',404)
        body=metadata['body']
        if (body['workspace_binding']!=w or body['request_id']!=trusted_current_request
                or body['message_id']!=trusted_current_message):reject('VIEW_FOLLOWUP_SCOPE',403)
        if (not hmac.compare_digest(ref['service_signature'],metadata['signature'])
                or not hmac.compare_digest(metadata['signature'],self.metrics._tool_signature(body))
                or resolved!=metadata['output']):reject('VIEW_FOLLOWUP_TAMPERED',409)
        if self.clock()>=body['expires_at']:reject('VIEW_FOLLOWUP_EXPIRED',410)
        with self._read(bank_db) as db:
            if self._identity(w,db)!=body['identity']:reject('VIEW_FOLLOWUP_IDENTITY_CHANGED',409)
            with self._lock:suggestion=deepcopy(self._suggestions.get(body['suggestion_id']))
            if suggestion is None:reject('VIEW_FOLLOWUP_NOT_ISSUED',404)
            parent=suggestion['body']
            self._parent(w,parent['parent_view_id'],parent['parent_projection_hash'],parent['template']['selection_ref'],db,parent['identity'])
            child=metadata['output']['result'];pres=child['presentation']
            stored=self.presentations._stored(w,pres['view_id'],db)
            if stored['presentation']!=pres:reject('PRESENTATION_TAMPERED',409)
            self.metrics.validate(w,child['metric_batch'],db)
        packet={'schema_version':1,'contract':CONTRACT,'resolved':deepcopy(metadata['output']),
            'request_id':trusted_current_request,'message_id':trusted_current_message,
            'deterministically_validated':True,'semantic_review_status':'owner_wiring_required',
            'final_publication_status':'owner_wiring_required'}
        packet['projection_hash']=fingerprint(packet)
        return packet
