"""只读消费真实 PresentationProvider；未建立新版 history/publish 接线。

本模块只由受信服务器构造，identity/language/provider 不来自模型参数。
Base 原件只存本轮局部；不重签指标、不赋予建议执行权，不借旧state保存。
"""
from copy import deepcopy
import unicodedata

from simulated_bank import BankError
from .presentation_consumer import PresentationConsumer, RENDERED_LAYOUTS
from .provider_projection import project_provider_object
from .ratio_claims import percentage_occurrences, RatioClaimError


def reject(code='PRESENTATION_PROVIDER_CONSUMER_INVALID',status=422):
    raise BankError(code,'提供器对象或当前请求审核合同不可用，请重新读取。',status)


class ProviderConsumer:
    def __init__(self,gateway,workspace,request_id,*,language='zh'):
        if type(request_id) is not str or not request_id or len(request_id)>160:reject()
        if language not in ('zh','en'):reject('PRESENTATION_PROVIDER_LANGUAGE')
        self.g,self.w,self.request_id,self.language=gateway,workspace,request_id,language
        # Keep gateway.presentations untouched: the provider and original
        # FinancialMetrics depend on this exact raw service.
        self.service=getattr(gateway,'presentation_provider',None)
        self._registrar=PresentationConsumer(gateway,workspace,request_id)
        self.objects,self.receipts={},{}

    def require_service(self):
        if self.service is None or not all(callable(getattr(self.service,k,None)) for k in ('dispatch','review','verify')):
            reject('PRESENTATION_PROVIDER_UNAVAILABLE',503)
        if not callable(getattr(getattr(self.g,'presentations',None),'review_projection',None)):
            reject('PRESENTATION_PROVIDER_UNAVAILABLE',503)

    def _context(self,bank_db=None):
        return {'trusted_current_request':self.request_id,'trusted_current_message':self.request_id,
                'language':self.language,'bank_db':bank_db}

    def query(self,parameters,*,bank_db=None):return self.dispatch('query',parameters,bank_db=bank_db)

    def dispatch(self,operation,parameters,*,bank_db=None):
        self.require_service()
        obj=self.service.dispatch(self.w,operation,deepcopy(parameters),**self._context(bank_db))
        result=obj['result'];pres=result['presentation']
        review=self.g.presentations.review_projection(self.w,result,bank_db=bank_db)
        if (obj['request_id']!=self.request_id or obj['message_id']!=self.request_id or obj['body_language']!=self.language
                or pres['request_id']!=self.request_id or pres['message_id']!=self.request_id
                or pres['workspace_binding']!=self.w or pres['layout'] not in RENDERED_LAYOUTS):reject('PRESENTATION_PROVIDER_BINDING',409)
        self.g.financial_metrics.validate(self.w,result['metric_batch'],bank_db)
        key=obj['object_ref']['object_id'];entry={'object':deepcopy(obj),'result':deepcopy(result),'review':deepcopy(review)}
        if key in self.objects and self.objects[key]!=entry:reject('PRESENTATION_PROVIDER_TAMPERED',409)
        self.objects[key]=entry
        return deepcopy(obj)

    def _entry(self,obj):
        if type(obj) is not dict or type(obj.get('object_ref')) is not dict:reject()
        entry=self.objects.get(obj['object_ref'].get('object_id'))
        if entry is None or obj!=entry['object']:reject('PRESENTATION_PROVIDER_NOT_ISSUED',409)
        return entry

    def register(self,tools,obj,*,evidence_id):
        entry=self._entry(obj)
        # Exact original signed batch and kind map; the existing registrar
        # validates and appends Registry/Observer/tools collection only once.
        return self._registrar.register(tools,entry,evidence_id)

    def review(self,obj,text,amount_claims=(),*,surface_bindings=(),issued_suggestions=(),
               resolved_suggestions=(),ratio_claims=(),bank_db=None):
        self.require_service();self._entry(obj)
        if type(text) is not str or unicodedata.normalize('NFKC',text)!=text:
            reject('PRESENTATION_PROVIDER_CANONICAL_BODY_REQUIRED')
        if ratio_claims:reject('PRESENTATION_RATIO_BINDING_REQUIRED')
        try:
            if percentage_occurrences(text):reject('PRESENTATION_RATIO_BINDING_REQUIRED')
        except RatioClaimError:reject('PRESENTATION_RATIO_BINDING_REQUIRED')
        # Base complete_review also validates each body occurrence, the own
        # batch signature and the shared display+body+surface limit of 24.
        receipt=self.service.review(self.w,obj,text,amount_claims,surface_bindings=surface_bindings,
            issued_suggestions=issued_suggestions,resolved_suggestions=resolved_suggestions,**self._context(bank_db))
        self.service.verify(self.w,receipt,**self._context(bank_db))
        key=receipt['receipt_ref']['receipt_id']
        if key in self.receipts and self.receipts[key]!=receipt:reject('PRESENTATION_PROVIDER_TAMPERED',409)
        self.receipts[key]=deepcopy(receipt)
        return deepcopy(receipt)

    def verify(self,receipt,*,bank_db=None):
        self.require_service()
        if type(receipt) is not dict or type(receipt.get('receipt_ref')) is not dict:reject()
        key=receipt['receipt_ref'].get('receipt_id')
        if key not in self.receipts or self.receipts[key]!=receipt:reject('PRESENTATION_PROVIDER_NOT_ISSUED',409)
        return self.service.verify(self.w,receipt,**self._context(bank_db))

    def model_object(self,boundary,obj):
        # No unchecked cached object is exported after source/identity drift.
        self.review(obj,'查看已读取对象的范围和缺项。')
        return project_provider_object(boundary,obj)

    def review_packet(self,boundary,receipt,*,compact_metric_sources=False):
        self.verify(receipt)
        complete=receipt['projection']['complete_review']
        # draft and proof are the SAME approved body; no renderer or historical
        # suggestion is appended after this projection/serializer.
        draft={'text':complete['text'],'claims':[],'draft_ids':[],'widget_ids':[],
               'followups':[],'amount_claims':[deepcopy(row['claim']) for row in complete['body_amount_contract']['amount_claims']],
               'ratio_claims':[]}
        raw={'question':'核对本轮提供器完整只读原件。','draft':draft,'provider_review':deepcopy(receipt),
             'amount_contract':deepcopy(complete['body_amount_contract'])}
        return boundary.review_packet(raw,compact_metric_sources=compact_metric_sources)

    def restore_review_receipt(self,boundary,packet):
        """Reverse this boundary's aliases/offsets, then recheck ORIGINAL receipt.

        This is not an input protocol: a model cannot populate receipts or
        catalogues through any tool. Decoding alone is never authorization.
        """
        from .data_boundary import BoundaryError
        full=boundary.expand_metric_sources(packet)
        if type(full) is not dict or 'provider_review' not in full:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        cloud=full['provider_review']
        decoded=boundary._restore(cloud)
        source=cloud['projection']['complete_review']
        target=decoded['projection']['complete_review']
        for old,row in zip(source['body_amount_contract']['amount_claims'],target['body_amount_contract']['amount_claims']):
            before,after=old['claim'],row['claim']
            if 'occurrence_start' not in before:continue
            start=before['occurrence_start']
            if type(start) is not int or source['text'][start:start+len(before['text'])]!=before['text']:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            after['occurrence_start']=len(boundary._restore(source['text'][:start]))
            offset=after['occurrence_start']
            if target['text'][offset:offset+len(after['text'])]!=after['text']:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        self.verify(decoded)
        return decoded

    def publication_state(self,receipt):
        self.verify(receipt)
        # New history_proof has no formal PersonalData/server state interface.
        # Never adapt it by placing arbitrary provider proof into old widgets.
        reject('PRESENTATION_PROVIDER_HISTORY_WIRING_REQUIRED',503)
