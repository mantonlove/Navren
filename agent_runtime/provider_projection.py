"""新版 Base provider 亲发原件的精确云投影；此层不验证或授予权限。

只允许 Consumer 经 Base HMAC/current guard 核验的本轮对象。引用和签名仅
作为本轮可逆别名传输；模型参数、旧 history state 和发布协议均不扩展。
"""
from copy import deepcopy
import re

from .data_boundary import BoundaryError, _HIDDEN
from .presentation_consumer import project_review
from .metric_claims import _metric_snapshot as validate_metric_snapshot, MetricClaimError, REF_FIELDS

PROVIDER_CONTRACT='navren.presentation-provider/1'
FOLLOWUP_CONTRACT='navren.view-followups/1'
OWNER='owner_wiring_required'
STATUS={'semantic_review_status':OWNER,'final_publication_status':OWNER}
_SELECTION={'point_ref','item_ref'}
_BINDING={'view_id','request_id','message_id'}
_BATCH_FIELDS={'batch_contract','workspace_id','tool_name','arguments','binding','source_versions',
    'source_fingerprint','metrics','metric_kinds','coverage','no_financial_side_effect',
    'service_signature','snapshot_id'}
_KINDS={'recorded_fact','recorded_projection','candidate_scenario','planning_scenario','forecast','hypothesis'}
_COMPOUND_CODES={
    'data_scope':frozenset({'simulation_bank_ledger'}),
    'clock_basis':frozenset({'simulation_business_date'}),
    'metric_name':frozenset({'obligations_aggregate','holds_aggregate'}),
    'basis':frozenset({'ledger_cash_balance'}),
}


def invalid():raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')


class _Projector:
    def __init__(self,boundary):self.b=boundary

    def preseed_compound_codes(self,presentation,metrics,rows=()):
        """Transport only: the Consumer verified the original before entry.

        Validate every full19 original first. Register only the exact issued
        codes in these typed slots, before shorter funding literals can split
        them into an alias plus suffix. Never inspect prose, gaps, IDs or URLs
        for vocabulary and never add public/catalog/query authority.
        """
        if type(presentation) is not dict or type(metrics) is not list or len(metrics)>24:invalid()
        try:validated=[validate_metric_snapshot(metric) for metric in metrics]
        except MetricClaimError as error:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from error
        codes=set()
        for field in ('data_scope','clock_basis'):
            value=presentation.get(field)
            if type(value) is str and value in _COMPOUND_CODES[field]:codes.add(value)
        for metric in validated:
            # These two issued statuses also occur in funding points before
            # funding_status. Seed them first so every copy uses one alias.
            if metric['calculation_status'] in ('calculated','unavailable'):
                codes.add(metric['calculation_status'])
            for field in ('data_scope','metric_name','basis'):
                if metric[field] in _COMPOUND_CODES[field]:codes.add(metric[field])
        for row,metric in zip(rows,validated):
            claim=row.get('claim')
            if (type(claim) is not dict or row.get('metric_id')!=metric['metric_id']
                    or row.get('kind') not in _KINDS or claim.get('claim_kind')!=row['kind']
                    or any(field not in claim or type(claim[field]) is not type(metric[field])
                           or claim[field]!=metric[field] for field in REF_FIELDS)):invalid()
            # The same complete signature is already checked above; a claim
            # cannot invent a code or register a different object's alias.
            for field in ('data_scope','metric_name','basis'):
                if claim[field] in _COMPOUND_CODES[field]:codes.add(claim[field])
        if rows and len(rows)!=len(validated):invalid()
        for code in sorted(codes):self.b._alias(code)

    @staticmethod
    def exact(raw,keys):
        if type(raw) is not dict or set(raw)!=set(keys):invalid()

    @staticmethod
    def enum(raw,values):
        if type(raw) is not str or raw not in values:invalid()
        return raw

    @staticmethod
    def integer(raw,maximum=24):
        if type(raw) is not int or not 0<=raw<=maximum:invalid()
        return raw

    @staticmethod
    def flag(raw,expected):
        if type(raw) is not bool or raw is not expected:invalid()
        return raw

    def identity(self,raw):
        return self.b._contract_string(raw,identifier=True,prose=False,maximum=512)

    def digest(self,raw):
        if type(raw) is not str or re.fullmatch(r'[0-9a-f]{64}',raw) is None:invalid()
        # Opaque exact shape only. Never bypass credential scanning for prose.
        return self.b._alias(raw,prose=False)

    def text(self,raw,maximum):
        if type(raw) is not str or not raw.strip() or len(raw)>maximum:invalid()
        checked=self.b._text(raw,maximum=maximum)
        if _HIDDEN in checked:invalid()
        return self.b._contract_string(raw,maximum=maximum)

    def version(self,raw):
        if type(raw) is not int or raw!=1:invalid()
        return raw

    def status(self,raw):
        for k,v in STATUS.items():
            if raw.get(k)!=v:invalid()
        return deepcopy(STATUS)

    def ref(self,raw,key,prefix):
        self.exact(raw,{key,'service_signature'})
        value=raw[key]
        if type(value) is not str or re.fullmatch(re.escape(prefix)+r'[0-9a-f]{32}',value) is None:invalid()
        return {key:self.b._alias(value,prose=False),'service_signature':self.digest(raw['service_signature'])}

    def selection(self,raw):
        if type(raw) is not dict or len(raw)!=1 or not set(raw)<=_SELECTION:invalid()
        return {k:self.identity(v) for k,v in raw.items()}

    def policy(self,raw):
        self.exact(raw,{'status','translation_performed'})
        if raw!={'status':'original_source_text','translation_performed':False}:invalid()
        return deepcopy(raw)

    def parameters(self,operation,raw):
        if type(raw) is not dict:invalid()
        # Match existing model-facing field schemas; no provider objects or
        # signatures become model arguments by adding this read-only consumer.
        from .bank_tools import SPECS,validate
        if operation in ('query','detail'):
            try:validate(raw,SPECS['presentation_'+operation][1])
            except Exception as error:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from error
        elif operation=='resolve':self.exact(raw,{'view_id'})
        elif operation=='item_page':
            if not {'view_id','point_ref'}<=set(raw)<= {'view_id','point_ref','offset','limit'}:invalid()
            self.integer(raw.get('offset',0),1000000)
            limit=raw.get('limit',8)
            if type(limit) is not int or not 1<=limit<=8:invalid()
        elif operation=='reissue':self.exact(raw,{'old_view_id','selection_ref','expected_parent_projection_hash'})
        else:invalid()
        result={}
        ids={'view_id','old_view_id','point_ref','item_ref','account_id','portfolio'}
        # Base funding query vocabulary is exact field-scoped transport data.
        # This neither registers public IDs nor permits arbitrary prose/refs.
        funding_literals={'intent':'funds','accounting_basis':'funds.current_budget/1',
            'measure':'arrangeable_now','layout':{'funding_blocks','funding_items'}}
        for key,value in raw.items():
            if (operation=='query' and raw.get('intent')=='funds' and key in funding_literals):
                expected=funding_literals[key]
                if type(value) is not str or (value not in expected if type(expected) is set else value!=expected):invalid()
                result[key]=self.b._alias(value)
            elif key in ids:result[key]=self.identity(value)
            elif key=='expected_parent_projection_hash':result[key]=self.digest(value)
            elif key=='selection_ref':result[key]=self.selection(value)
            elif key=='selector':
                result[key]={k:self.identity(v) if k in _SELECTION else
                    self.b._contract_date(v) if k in ('from','to') else self.integer(v,1000000)
                    for k,v in value.items()}
            elif key in ('from','to'):result[key]=self.b._contract_date(value)
            elif type(value) is str:result[key]=self.text(value,256)
            elif type(value) is bool:result[key]=value
            elif type(value) is int:result[key]=self.integer(value,1000000)
            elif key=='kinds' and type(value) is list:result[key]=[self.text(v,256) for v in value]
            else:invalid()
        return result

    def presentation(self,raw):return project_review(self.b,raw,presentation_only=True)

    def batch(self,raw):
        self.exact(raw,_BATCH_FIELDS)
        if raw['batch_contract']!='navren.financial-tool-metrics/1' or raw['tool_name']!='presentation_view':invalid()
        for key in ('arguments','binding'):self.exact(raw[key],_BINDING)
        if raw['arguments']!=raw['binding']:invalid()
        if type(raw['metrics']) is not list or len(raw['metrics'])>24:invalid()
        metrics=[self.b._metric_snapshot(m) for m in raw['metrics']]
        ids=[m['metric_id'] for m in raw['metrics']]
        if len(ids)!=len(set(ids)) or type(raw['metric_kinds']) is not dict or set(raw['metric_kinds'])!=set(ids):invalid()
        kinds={self.identity(k):self.enum(v,_KINDS) for k,v in raw['metric_kinds'].items()}
        versions=raw['source_versions']
        if type(versions) is not dict or not 1<=len(versions)<=128:invalid()
        coverage=raw['coverage']
        self.exact(coverage,{'projection_hash','view_id','visible_amount_occurrences','final_review_wired'})
        return {'batch_contract':raw['batch_contract'],'workspace_id':self.identity(raw['workspace_id']),
            'tool_name':raw['tool_name'],'arguments':{k:self.identity(v) for k,v in raw['arguments'].items()},
            'binding':{k:self.identity(v) for k,v in raw['binding'].items()},
            'source_versions':{self.identity(k):self.identity(v) for k,v in versions.items()},
            'source_fingerprint':self.digest(raw['source_fingerprint']),'metrics':metrics,'metric_kinds':kinds,
            'coverage':{'projection_hash':self.digest(coverage['projection_hash']),'view_id':self.identity(coverage['view_id']),
                'visible_amount_occurrences':self.integer(coverage['visible_amount_occurrences']),
                'final_review_wired':self.flag(coverage['final_review_wired'],False)},
            'no_financial_side_effect':self.flag(raw['no_financial_side_effect'],True),
            'service_signature':self.digest(raw['service_signature']),'snapshot_id':self.identity(raw['snapshot_id'])}

    def result(self,raw):
        self.exact(raw,{'presentation','metric_batch'})
        self.exact(raw['metric_batch'],_BATCH_FIELDS)
        self.preseed_compound_codes(raw['presentation'],raw['metric_batch']['metrics'])
        return {'presentation':self.presentation(raw['presentation']),'metric_batch':self.batch(raw['metric_batch'])}

    def obj(self,raw):
        self.exact(raw,{'schema_version','contract','operation','parameters','request_id','message_id','body_language',
            'source_label_policy','result','readonly','execution_authority','object_ref'})
        if raw['contract']!=PROVIDER_CONTRACT:invalid()
        operation=self.enum(raw['operation'],{'query','resolve','detail','item_page','reissue'})
        return {'schema_version':self.version(raw['schema_version']),'contract':raw['contract'],'operation':operation,
            'parameters':self.parameters(operation,raw['parameters']),
            'request_id':self.identity(raw['request_id']),'message_id':self.identity(raw['message_id']),
            'body_language':self.enum(raw['body_language'],{'zh','en'}),'source_label_policy':self.policy(raw['source_label_policy']),
            'result':self.result(raw['result']),'readonly':self.flag(raw['readonly'],True),
            'execution_authority':self.flag(raw['execution_authority'],False),
            'object_ref':self.ref(raw['object_ref'],'object_id','provider-object-')}

    def suggestion(self,raw,resolved=False):
        common={'schema_version','id','template_id','intent','type','label','text','context_ref',
            'availability','reason','object_version','suggestion_ref'}
        extra=({'result','readonly','execution_authority','semantic_review_status','final_publication_status','resolution_ref'}
            if resolved else {'suggestion_batch_ref'})
        self.exact(raw,common|extra)
        self.enum(raw['template_id'],{'read_selected_detail','explain_basis_and_gaps'})
        expected='open_view' if raw['template_id']=='read_selected_detail' else 'ask'
        if raw['intent']!=expected or raw['type']!=('open_view' if expected=='open_view' else 'send_message'):invalid()
        availability=self.enum(raw['availability'],{'available','unavailable'})
        if resolved and availability!='available':invalid()
        if availability=='available' and (raw['reason'] is not None or raw['id'] is None or raw['suggestion_ref'] is None):invalid()
        if availability=='unavailable' and (raw['reason'] is None or raw['id'] is not None or raw['suggestion_ref'] is not None):invalid()
        context=raw['context_ref']
        if resolved:
            self.exact(context,{'view_id','parent_view_id','projection_hash','request_id','message_id'})
            context={k:self.identity(v) for k,v in context.items()}
        else:
            self.exact(context,{'view_id','parent_projection_hash','selection_ref'})
            context={'view_id':self.identity(context['view_id']),'parent_projection_hash':self.digest(context['parent_projection_hash']),
                'selection_ref':self.selection(context['selection_ref'])}
        reference=None if raw['suggestion_ref'] is None else self.ref(raw['suggestion_ref'],'suggestion_id','view-followup-')
        if reference is not None and raw['id']!=raw['suggestion_ref']['suggestion_id']:invalid()
        identifier=None if reference is None else reference['suggestion_id']
        result={'schema_version':self.version(raw['schema_version']),'id':identifier,'template_id':raw['template_id'],
            'intent':raw['intent'],'type':raw['type'],'label':self.text(raw['label'],80),'text':self.text(raw['text'],400),
            'context_ref':context,'availability':availability,
            'reason':None if raw['reason'] is None else self.text(raw['reason'],256),
            'object_version':self.digest(raw['object_version']),
            'suggestion_ref':reference}
        if resolved:
            result.update(result=self.result(raw['result']),readonly=self.flag(raw['readonly'],True),
                execution_authority=self.flag(raw['execution_authority'],False),
                resolution_ref=self.ref(raw['resolution_ref'],'resolution_id','view-followup-resolution-'),**self.status(raw))
        else:result['suggestion_batch_ref']=self.ref(raw['suggestion_batch_ref'],'batch_id','view-followup-batch-')
        return result

    def followup_review(self,raw,resolved=False):
        key='resolved' if resolved else 'suggestions'
        keys={'schema_version','contract',key,'request_id','message_id','deterministically_validated',
            'semantic_review_status','final_publication_status','projection_hash'}
        if not resolved:keys.add('suggestion_batch_ref')
        self.exact(raw,keys)
        if raw['contract']!=FOLLOWUP_CONTRACT:invalid()
        result={'schema_version':self.version(raw['schema_version']),'contract':raw['contract'],
            'request_id':self.identity(raw['request_id']),'message_id':self.identity(raw['message_id']),
            'deterministically_validated':self.flag(raw['deterministically_validated'],True),
            'projection_hash':self.digest(raw['projection_hash']),**self.status(raw)}
        if resolved:result[key]=self.suggestion(raw[key],True)
        else:
            if type(raw[key]) is not list or not 1<=len(raw[key])<=3:invalid()
            result[key]=[self.suggestion(item) for item in raw[key]]
            result['suggestion_batch_ref']=self.ref(raw['suggestion_batch_ref'],'batch_id','view-followup-batch-')
        return result

    def projection(self,raw):
        self.exact(raw,{'schema_version','contract','body_language','source_label_policy','operation','parameters',
            'request_id','message_id','object_ref','complete_review','suggestions_review','resolved_followup_reviews',
            'semantic_review_status','final_publication_status','projection_hash'})
        if raw['contract']!=PROVIDER_CONTRACT:invalid()
        operation=self.enum(raw['operation'],{'query','resolve','detail','item_page','reissue'})
        language=self.enum(raw['body_language'],{'zh','en'})
        complete=raw['complete_review']
        if type(complete) is not dict or complete.get('language')!=language:invalid()
        # Consumer already checked this ORIGINAL receipt with Base HMAC and
        # current sources. Preseed only its exact full19 metric snapshots,
        # never text or arbitrary customer data, before the same body scan.
        # This neither registers claim authority nor changes any signed field.
        contract=complete.get('body_amount_contract')
        if (type(contract) is not dict or set(contract)!= {'status','amount_claims','amount_occurrence_count','independent_review_required'}
                or type(contract['amount_claims']) is not list or len(contract['amount_claims'])>24):invalid()
        for row in contract['amount_claims']:
            if type(row) is not dict or set(row)!= {'metric_id','metric','kind','evidence_ids','claim'}:invalid()
        self.preseed_compound_codes(complete.get('presentation'),
            [row['metric'] for row in contract['amount_claims']],contract['amount_claims'])
        for row in contract['amount_claims']:
            self.b._metric_snapshot(row['metric'])
        resolved=raw['resolved_followup_reviews']
        if type(resolved) is not list or len(resolved)>3:invalid()
        result={'schema_version':self.version(raw['schema_version']),'contract':raw['contract'],'body_language':language,
            'source_label_policy':self.policy(raw['source_label_policy']),'operation':operation,
            'parameters':self.parameters(operation,raw['parameters']),'request_id':self.identity(raw['request_id']),
            'message_id':self.identity(raw['message_id']),'object_ref':self.ref(raw['object_ref'],'object_id','provider-object-'),
            'complete_review':project_review(self.b,raw['complete_review']),
            'suggestions_review':None if raw['suggestions_review'] is None else self.followup_review(raw['suggestions_review']),
            'resolved_followup_reviews':[self.followup_review(item,True) for item in resolved],
            'projection_hash':self.digest(raw['projection_hash']),**self.status(raw)}
        return result

    def proof(self,raw):
        self.exact(raw,{'schema_version','provider_contract','request_id','message_id','workspace_binding','body_language',
            'body_text_hash','object_ref','view_id','view_projection_hash','parent_view_id','parent_projection_hash',
            'metric_snapshot_id','review_projection_hash','bodyAmountOccurrenceCount','totalAmountOccurrenceCount',
            'semantic_review_status','final_publication_status'})
        if raw['provider_contract']!=PROVIDER_CONTRACT:invalid()
        result={'schema_version':self.version(raw['schema_version']),'provider_contract':raw['provider_contract'],
            'body_language':self.enum(raw['body_language'],{'zh','en'}),
            'object_ref':self.ref(raw['object_ref'],'object_id','provider-object-'),
            'bodyAmountOccurrenceCount':self.integer(raw['bodyAmountOccurrenceCount']),
            'totalAmountOccurrenceCount':self.integer(raw['totalAmountOccurrenceCount']),**self.status(raw)}
        for key in ('request_id','message_id','workspace_binding','view_id','metric_snapshot_id'):
            result[key]=self.identity(raw[key])
        for key in ('body_text_hash','view_projection_hash','review_projection_hash'):result[key]=self.digest(raw[key])
        for key in ('parent_view_id','parent_projection_hash'):
            result[key]=None if raw[key] is None else self.identity(raw[key])
        return result

    def receipt(self,raw):
        self.exact(raw,{'schema_version','contract','projection','history_proof','original_receipt_hash',
            'semantic_review_status','final_publication_status','receipt_ref'})
        if raw['contract']!=PROVIDER_CONTRACT:invalid()
        return {'schema_version':self.version(raw['schema_version']),'contract':raw['contract'],
            'projection':self.projection(raw['projection']),'history_proof':self.proof(raw['history_proof']),
            'original_receipt_hash':self.digest(raw['original_receipt_hash']),
            'receipt_ref':self.ref(raw['receipt_ref'],'receipt_id','provider-receipt-'),**self.status(raw)}


def project_provider_object(boundary,obj):return _Projector(boundary).obj(obj)


def project_provider_receipt(boundary,receipt):return _Projector(boundary).receipt(receipt)
