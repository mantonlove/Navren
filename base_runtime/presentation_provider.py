"""原展示服务的受信只读桥；确定性回执不是语义批准或发布授权。"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import threading
import time

from financial_metrics import fingerprint
from simulated_bank import BankError


CONTRACT='navren.presentation-provider/1'
CODE=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def reject(code='PRESENTATION_PROVIDER_INVALID',status=422):
    raise BankError(code,'提供端对象或审核回执无效，请以当前服务上下文重新读取。',status)


class PresentationProvider:
    def __init__(self,gateway,*,followups=None,ttl_seconds=300,max_receipts=128,clock=None):
        if (type(ttl_seconds) not in (int,float) or not 0<ttl_seconds<=3600
                or type(max_receipts) is not int or not 1<=max_receipts<=512):reject()
        self.g,self.p,self.metrics=gateway,gateway.presentations,gateway.financial_metrics
        self.followups=followups
        self.ttl,self.maximum,self.clock=ttl_seconds,max_receipts,clock or time.monotonic
        self._objects,self._receipts,self._lock=OrderedDict(),OrderedDict(),threading.RLock()

    @staticmethod
    def _current(request,message,language):
        if (type(request) is not str or not request.strip() or len(request)>160
                or type(message) is not str or message!=request):reject('PRESENTATION_PROVIDER_BINDING',403)
        if type(language) is not str or language not in {'zh','en'}:reject('PRESENTATION_PROVIDER_LANGUAGE')

    @staticmethod
    def _params(operation,parameters):
        if type(operation) is not str or operation not in {'query','resolve','detail','item_page','reissue'}:reject()
        if type(parameters) is not dict:reject()
        allowed={'resolve':({'view_id'},{'view_id'}),'detail':({'view_id','selector'},{'view_id','selector'}),
            'item_page':({'view_id','point_ref'},{'view_id','point_ref','offset','limit'}),
            'reissue':({'old_view_id','selection_ref','expected_parent_projection_hash'},
                       {'old_view_id','selection_ref','expected_parent_projection_hash'})}
        if operation=='query':
            if set(parameters)&{'approved','client_approved','language','request_id','message_id','bank_db','parent','selected_items'}:reject()
        else:
            required,possible=allowed[operation]
            if not required<=set(parameters)<=possible:reject()
        if operation=='item_page':
            if (type(parameters.get('offset',0)) is not int or parameters.get('offset',0)<0
                    or type(parameters.get('limit',8)) is not int or not 1<=parameters.get('limit',8)<=8):reject('PRESENTATION_PAGE')
        return deepcopy(parameters)

    def _identity(self,w,db):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=CODE:reject('PRESENTATION_PROVIDER_RESTART_REQUIRED',503)
        return self.p._session_binding(w,db,required=True)

    def _seal(self,kind,w,request,message,language,identity,public,*,expires=None):
        key=kind+'_id';identifier='provider-'+kind+'-'+secrets.token_hex(16)
        body={'contract':CONTRACT,key:identifier,'workspace_binding':w,'request_id':request,'message_id':message,
            'body_language':language,'identity':deepcopy(identity),'provider_code':CODE,
            'output':deepcopy(public),'expires_at':min(self.clock()+self.ttl,expires) if expires is not None else self.clock()+self.ttl}
        signature=self.metrics._tool_signature(body)
        output={**deepcopy(public),kind+'_ref':{key:identifier,'service_signature':signature}}
        metadata={'body':body,'signature':signature,'output':deepcopy(output)}
        pool=self._objects if kind=='object' else self._receipts
        with self._lock:
            pool[identifier]=metadata
            while len(pool)>self.maximum:pool.popitem(last=False)
        return output

    def _lookup(self,kind,w,output,request,message,language):
        key=kind+'_id';ref=output.get(kind+'_ref') if type(output) is dict else None
        if (type(ref) is not dict or set(ref)!={key,'service_signature'} or type(ref[key]) is not str
                or re.fullmatch('provider-'+kind+r'-[a-f0-9]{32}',ref[key]) is None
                or type(ref['service_signature']) is not str or re.fullmatch(r'[a-f0-9]{64}',ref['service_signature']) is None):reject()
        pool=self._objects if kind=='object' else self._receipts
        with self._lock:metadata=deepcopy(pool.get(ref[key]))
        if metadata is None:reject('PRESENTATION_PROVIDER_NOT_ISSUED',404)
        body=metadata['body']
        if (body['workspace_binding']!=w or body['request_id']!=request or body['message_id']!=message
                or body['body_language']!=language):reject('PRESENTATION_PROVIDER_BINDING',403)
        if (not hmac.compare_digest(ref['service_signature'],metadata['signature'])
                or not hmac.compare_digest(metadata['signature'],self.metrics._tool_signature(body))
                or output!=metadata['output']):reject('PRESENTATION_PROVIDER_TAMPERED',409)
        if self.clock()>=body['expires_at']:reject('PRESENTATION_PROVIDER_EXPIRED',410)
        return metadata

    def _guard_object(self,w,obj,request,message,language,db):
        metadata=self._lookup('object',w,obj,request,message,language)
        if self._identity(w,db)!=metadata['body']['identity']:reject('PRESENTATION_PROVIDER_IDENTITY_CHANGED',409)
        result=metadata['output']['result'];pres=result['presentation']
        if pres['request_id']!=request or pres['message_id']!=message:reject('PRESENTATION_PROVIDER_BINDING',403)
        stored=self.p._stored(w,pres['view_id'],db)
        if stored.get('trusted_context') is None or stored['presentation']!=pres:reject('PRESENTATION_PROVIDER_TAMPERED',409)
        if pres.get('parent_view_id'):self.p._stored(w,pres['parent_view_id'],db)
        self.metrics.validate(w,result['metric_batch'],db)
        return metadata

    def dispatch(self,w,operation,parameters,*,trusted_current_request,trusted_current_message,language='zh',bank_db=None):
        self._current(trusted_current_request,trusted_current_message,language)
        args=self._params(operation,parameters);issued=None
        try:
            boundary=self.p._publication if operation=='resolve' else self.p._reissue_atomic
            with boundary(bank_db) as db:
                identity=self._identity(w,db)
                current={'request_id':trusted_current_request,'message_id':trusted_current_message,'bank_db':db}
                if operation=='query':result=self.p.query(w,args,**current)
                elif operation=='resolve':result=self.p.resolve(w,args['view_id'],**current)
                elif operation=='detail':result=self.p.detail(w,args['view_id'],args['selector'],**current)
                elif operation=='item_page':result=self.p.item_page(w,args['view_id'],args['point_ref'],
                    offset=args.get('offset',0),limit=args.get('limit',8),**current)
                else:result=self.p.reissue_selected_detail(w,**args,trusted_current_request=trusted_current_request,
                    trusted_current_message=trusted_current_message,bank_db=db)
                public={'schema_version':1,'contract':CONTRACT,'operation':operation,'parameters':args,
                    'request_id':trusted_current_request,'message_id':trusted_current_message,'body_language':language,
                    'source_label_policy':{'status':'original_source_text','translation_performed':False},
                    'result':deepcopy(result),'readonly':True,'execution_authority':False}
                issued=self._seal('object',w,trusted_current_request,trusted_current_message,language,identity,public)
                self._guard_object(w,issued,trusted_current_request,trusted_current_message,language,db)
                return issued
        except BaseException:
            if issued is not None:
                with self._lock:self._objects.pop(issued['object_ref']['object_id'],None)
            raise

    def _projection(self,w,obj,text,claims,surfaces,suggestions,resolved,request,message,language,db):
        self._guard_object(w,obj,request,message,language,db)
        complete=self.p.complete_review_projection(w,obj['result'],text,claims,surface_bindings=surfaces,language=language,bank_db=db)
        suggestion_review=None;resolved_reviews=[]
        context={'trusted_current_request':request,'trusted_current_message':message,'bank_db':db}
        if suggestions:
            if self.followups is None:reject('PRESENTATION_PROVIDER_FOLLOWUPS')
            suggestion_review=self.followups.review_projection(w,suggestions,**context)
            for item in suggestions:
                ref=item['context_ref']
                if ref['view_id']!=obj['result']['presentation']['view_id'] or ref['parent_projection_hash']!=obj['result']['presentation']['projection_hash']:
                    reject('PRESENTATION_PROVIDER_FOLLOWUPS',409)
        for item in resolved:
            if self.followups is None:reject('PRESENTATION_PROVIDER_FOLLOWUPS')
            projection=self.followups.review_resolved_projection(w,item,**context)
            # 一张图只有一份显示；不同图须另行纳入完整多对象金额审核。
            child=item['result'];primary=obj['result']
            if (child['presentation']!=primary['presentation'] or child['metric_batch']['metrics']!=primary['metric_batch']['metrics']
                    or child['metric_batch']['metric_kinds']!=primary['metric_batch']['metric_kinds']):reject('PRESENTATION_PROVIDER_FOLLOWUPS',409)
            resolved_reviews.append(projection)
        self._guard_object(w,obj,request,message,language,db)
        packet={'schema_version':1,'contract':CONTRACT,'body_language':language,
            'source_label_policy':deepcopy(obj['source_label_policy']),'operation':obj['operation'],'parameters':deepcopy(obj['parameters']),
            'request_id':request,'message_id':message,'object_ref':deepcopy(obj['object_ref']),
            'complete_review':complete,'suggestions_review':suggestion_review,'resolved_followup_reviews':resolved_reviews,
            'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}
        packet['projection_hash']=fingerprint(packet)
        return packet

    def review(self,w,obj,text,amount_claims=(),*,surface_bindings=(),issued_suggestions=(),resolved_suggestions=(),
               trusted_current_request,trusted_current_message,language='zh',bank_db=None):
        self._current(trusted_current_request,trusted_current_message,language)
        if (type(text) is not str or len(text.encode())>32768
                or type(amount_claims) not in (list,tuple) or type(surface_bindings) not in (list,tuple)
                or type(issued_suggestions) not in (list,tuple) or len(issued_suggestions)>3
                or type(resolved_suggestions) not in (list,tuple) or len(resolved_suggestions)>3):reject()
        claims,surfaces,suggestions,resolved=map(deepcopy,map(list,(amount_claims,surface_bindings,issued_suggestions,resolved_suggestions)))
        issued=None
        try:
            with self.p._publication(bank_db) as db:
                metadata=self._guard_object(w,obj,trusted_current_request,trusted_current_message,language,db)
                projection=self._projection(w,obj,text,claims,surfaces,suggestions,resolved,
                    trusted_current_request,trusted_current_message,language,db)
                complete=projection['complete_review']
                proof={'schema_version':1,'provider_contract':CONTRACT,'request_id':trusted_current_request,
                    'message_id':trusted_current_message,'workspace_binding':w,'body_language':language,
                    'body_text_hash':fingerprint(text),'object_ref':deepcopy(obj['object_ref']),
                    'view_id':obj['result']['presentation']['view_id'],'view_projection_hash':obj['result']['presentation']['projection_hash'],
                    'parent_view_id':obj['result']['presentation'].get('parent_view_id'),
                    'parent_projection_hash':obj['result']['presentation'].get('parent_projection_hash'),
                    'metric_snapshot_id':obj['result']['metric_batch']['snapshot_id'],'review_projection_hash':projection['projection_hash'],
                    'bodyAmountOccurrenceCount':complete['body_amount_contract']['amount_occurrence_count'],
                    'totalAmountOccurrenceCount':complete['visible_amount_occurrence_count'],
                    'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}
                public={'schema_version':1,'contract':CONTRACT,'projection':projection,'history_proof':proof,
                    'original_receipt_hash':fingerprint({'projection':projection,'history_proof':proof}),
                    'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}
                # 完整重构再比对，额外建议不能在审核后追加。
                final=self._projection(w,obj,text,claims,surfaces,suggestions,resolved,
                    trusted_current_request,trusted_current_message,language,db)
                if final!=projection:reject('PRESENTATION_PROVIDER_TAMPERED',409)
                issued=self._seal('receipt',w,trusted_current_request,trusted_current_message,language,
                    metadata['body']['identity'],public,expires=metadata['body']['expires_at'])
                with self._lock:
                    cached=self._receipts.get(issued['receipt_ref']['receipt_id'])
                    if cached is None:reject('PRESENTATION_PROVIDER_NOT_ISSUED',404)
                    cached['inputs']={'object':deepcopy(obj),'text':text,'claims':claims,'surfaces':surfaces,'suggestions':suggestions,'resolved':resolved}
                self._lookup('receipt',w,issued,trusted_current_request,trusted_current_message,language)
                return issued
        except BaseException:
            if issued is not None:
                with self._lock:self._receipts.pop(issued['receipt_ref']['receipt_id'],None)
            raise

    def verify(self,w,receipt,*,trusted_current_request,trusted_current_message,language='zh',bank_db=None):
        self._current(trusted_current_request,trusted_current_message,language)
        metadata=self._lookup('receipt',w,receipt,trusted_current_request,trusted_current_message,language)
        with self.p._publication(bank_db) as db:
            if self._identity(w,db)!=metadata['body']['identity']:reject('PRESENTATION_PROVIDER_IDENTITY_CHANGED',409)
            inputs=metadata['inputs']
            projection=self._projection(w,inputs['object'],inputs['text'],inputs['claims'],inputs['surfaces'],inputs['suggestions'],inputs['resolved'],
                trusted_current_request,trusted_current_message,language,db)
            if projection!=metadata['output']['projection']:reject('PRESENTATION_PROVIDER_TAMPERED',409)
            self._lookup('receipt',w,receipt,trusted_current_request,trusted_current_message,language)
        canonical=metadata['output'];proof=canonical['history_proof']
        return {'original_receipt_hash':canonical['original_receipt_hash'],
            'bodyAmountOccurrenceCount':proof['bodyAmountOccurrenceCount'],
            'totalAmountOccurrenceCount':proof['totalAmountOccurrenceCount'],
            'history_proof':deepcopy(proof),'receipt_ref':deepcopy(canonical['receipt_ref']),
            'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}

    def revalidate(self,w,receipt,**trusted_context):return self.verify(w,receipt,**trusted_context)
