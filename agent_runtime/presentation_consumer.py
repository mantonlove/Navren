"""消费本轮 Base 发行展示；不计算、签发、确认或代替最终发布守卫。"""
from copy import deepcopy

from simulated_bank import BankError
from .metric_claims import MetricClaimError
from .ratio_claims import RatioClaimError, percentage_occurrences


TOOLS = frozenset({'presentation_query', 'presentation_detail'})
RENDERED_LAYOUTS = frozenset({'month_grid', 'day_cards', 'grouped_bar',
                            'category_bar', 'step_line', 'time_line',
                            'funding_blocks', 'funding_items'})
FUNDING_LAYOUTS = frozenset({'funding_blocks', 'funding_items'})

# Typed field contract from stable navren.presentation/1. Unknown keys fail
# the whole projection; raw records, credentials and HMACs have no cloud slot.
_VALUE = {'metric_id':'id','value_minor':'int?','currency':'str','calculation_status':'str'}
_FOLLOWUP = {'schema_version':'int','id':'id','label':'text','text':'text','type':'str',
            'intent':'str','context_ref':{'view_id':'id'},'availability':'str'}
_SCOPE = {'source':'str','currency':'str','portfolio':'id','accounting_basis':'str',
          'account_id':'id?','kinds':('str', 9),'include_life_events':'bool'}
_RANGE = {'from':'date','to':'date'}
_PAGE = {'offset':'int','limit':'int','total':'int','has_more':'bool'}
_REFUND = {'refund_ref':'id','original_expense_ref':'id','category':'label','subject_id':'id',
           'account_id':'id','currency':'str','settled_on':'date','original_on':'date',
           'cumulative_ref':'id','source_version':'id','cumulative_not_excessive':'bool'}
_POINT_REQUIRED = {'point_ref':'id','start':'date','end':'date','coverage':'str',
                   'item_count':'int','state':'str'}
_POINT_OPTIONAL = {'date':'date','item_ref':'id','title':'label','kind':'str','source_ref':'id',
    'preview':({'item_ref':'id','title':'label','kind':'str','state':'str'},2),
    'value':_VALUE,'observed_value':_VALUE,'cash_delta':_VALUE,'budget':_VALUE,
    'observed_scope':{'type':'str','from':'date','to':'date','record_count':'int','complete_period_proven':'bool'},
    'label':'label','category_values':('label',64),'selected_item_refs':('id',512),
    'value_scope':'str','opening_on':'date?','interval_days':('int?',3660),
    'missing':('text',128),'refund_attributions':(_REFUND,512)}
_PRES_REQUIRED = {'schema_version':'int','contract':'contract','view_id':'id','parent_view_id':'id?',
    'request_id':'id','message_id':'id','workspace_binding':'id',
    'authorization_revision':{'personal':'int','privacy':'int'},'source_versions':'versions',
    'scope':'scope','range':_RANGE,'granularity':'str','timezone':'str','clock_basis':'str','as_of':'date',
    'series_kind':'str','data_scope':'str','measure':'str','title':'text','points':'points',
    'missing':('text',128),'coverage':'str','readonly_actions':('str',3),
    'important_states':('text',128),'authority_reports':({'year':'int','sha256':'id',
        'basis':'str','projection_basis':'str'},12),'measure_basis':'str',
    'allowed_layouts':('str',6),'layout':'str','page':_PAGE,'followups':(_FOLLOWUP,2),
    'parent_projection_hash':'id?','review_status':'str','publication_status':'str','projection_hash':'id'}
_PRES_OPTIONAL = {'detail_page':_PAGE,'denominator_basis':'str?','ratios_available':'bool',
    'refund_attributions':(_REFUND,512),'portfolio_metadata':'portfolio?',
    'lineage':{'old_view_id':'id','parent_projection_hash':'id','selection_ref':'selection'}}
_PORTFOLIO = {'identity':'text','quantity_origin':'text','from':'date','to':'date','currency':'str',
    'accounting_basis':'str','source_revision':'id','expected_funds':('id',128),
    'source_gaps':('id',128),'valuation_dates':('date',10000),'id':'id','kind':'portfolio-kind',
    'effective_on':'date','active':'bool','archived':'bool','archive_notice':'text?'}
_OCCURRENCE = {'path':'str','metric_id':'id','value_minor':'int?','currency':'str'}

# Exact FundingPresentations fields. These are source context, not new
# display amounts, denominators, execution authority or public catalog IDs.
_FUNDING_RULE = {'subject':'literal:whole_workspace',
    'emergency_policy':'literal:workspace_profile_once',
    'future_inflows_pre_spent':'false',
    'holds_deduplicated_by':'literal:explicit_identity_links',
    'per_account_budget_issued':'false'}
_FUNDING_QUERY = {'days':'funding_days','method_id':'literal:funds.current_budget/1',
    'rule_mode':'funding_mode?','subject':'literal:whole_workspace','as_of':'date',
    'period_start':'date','period_end':'date','coverage_scope':'text',
    'source_scope':'id','source_versions':'versions'}
_FUNDING_LIMITS = {'confirmed_floor_breached':'bool','confirmed_floor_touched':'bool',
    'candidate_floor_breached':'bool','candidate_floor_touched':'bool',
    'confirmed_shortfall_minor':'nonnegative_int','candidate_shortfall_minor':'nonnegative_int',
    'confirmed_minimum_on':'date','candidate_minimum_on':'date',
    'adequacy':'literal:unknown_without_living_costs_income_stability_and_goals',
    'intraday_solvency':'literal:not_assessed_daily_net_only'}
_FUNDING_BLOCKS = frozenset({'cash_balance','current_available','current_budget',
                           'emergency_floor','obligations','holds'})
_FUNDING_POINT = {'point_ref':'funding_point_ref','date':'date','start':'date','end':'date',
    'label':'label','block':'funding_block','coverage':'str','value':_VALUE,
    'item_count':'nonnegative_int?','preview':'empty_list','missing':('text',128)}
_FUNDING_DETAIL = {'item_ref':'id','state':'str','evidence_ref':'id'}
_FUNDING_FIELDS = {'geometry':{'available':'false',
        'reason':'literal:no_ratio_or_composition_denominator_issued'},
    'funding_reference':{'snapshot_id':'id','snapshot_hash':'digest'},
    'funding_status':'funding_status','funding_mode':'funding_mode?',
    'funding_limits':'funding_limits'}


def project_review(boundary, review, *, presentation_only=False):
    """Project all supported signed/visible fields; never a partial success."""
    from .data_boundary import BoundaryError, _HIDDEN

    def invalid():
        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')

    def record(raw, required, optional=None):
        optional = optional or {}
        if not isinstance(raw, dict) or not set(required) <= set(raw) or set(raw) - set(required) - set(optional):
            invalid()
        return {k: value(v, (required | optional)[k]) for k,v in raw.items()}

    def value(raw, kind):
        if isinstance(kind, dict):
            return record(raw, kind)
        if isinstance(kind, tuple):
            item, limit = kind
            if not isinstance(raw, list) or len(raw) > limit:invalid()
            return [value(v, item) for v in raw]
        if kind.endswith('?'):
            return None if raw is None else value(raw, kind[:-1])
        if kind == 'int':
            if type(raw) is not int:invalid()
            return raw
        if kind.startswith('literal:'):
            if type(raw) is not str or raw != kind[len('literal:'):]:invalid()
            # Known code literals can be replaced in matching prose; they
            # never enter _public_ids/_catalog_ids or grant read authority.
            return boundary._alias(raw)
        if kind == 'digest':
            import re
            if type(raw) is not str or re.fullmatch(r'[0-9a-f]{64}',raw) is None:invalid()
            return boundary._alias(raw,prose=False)
        if kind == 'false':
            if raw is not False:invalid()
            return raw
        if kind == 'empty_list':
            if type(raw) is not list or raw:invalid()
            return []
        if kind == 'funding_days':
            if type(raw) is not int or not 1<=raw<=365:invalid()
            return raw
        if kind == 'nonnegative_int':
            if type(raw) is not int or raw<0:invalid()
            return raw
        if kind in ('funding_status','funding_mode'):
            allowed=({'calculated','unavailable'} if kind=='funding_status'
                else {'recorded_items_projection','hypothetical_scenario'})
            if type(raw) is not str or raw not in allowed:invalid()
            return boundary._alias(raw)
        if kind == 'bool':
            if type(raw) is not bool:invalid()
            return raw
        if kind == 'date':return boundary._contract_date(raw)
        if kind == 'contract':
            if raw!='navren.presentation/1':invalid()
            return raw
        if kind == 'portfolio-kind':
            if raw not in ('historical','current'):invalid()
            return raw
        if kind == 'id':return boundary._contract_string(raw, identifier=True, prose=False)
        if kind == 'str':return boundary._contract_string(raw, maximum=512)
        if kind == 'text':
            if not isinstance(raw,str) or len(raw)>2000:invalid()
            checked=boundary._text(raw, maximum=2000)
            if _HIDDEN in checked:invalid()
            # Signed visible fields retain original punctuation after the
            # ordinary bulk/credential scan; only approved aliases change.
            return boundary._contract_string(raw,maximum=2000) if raw else ''
        if kind == 'label':
            from .data_boundary import _SAFE_LABELS
            checked=value(raw,'text')
            return checked if raw in _SAFE_LABELS else boundary._alias(raw,'label')
        if kind == 'body':
            if not isinstance(raw,str) or len(raw)>6000:invalid()
            return boundary._text(raw, maximum=6000)
        if kind == 'versions':
            if not isinstance(raw,dict) or not 1 <= len(raw) <= 128:invalid()
            return {value(k,'id'):value(v,'id') for k,v in raw.items()}
        if kind == 'selection':
            if type(raw) is not dict or len(raw)!=1 or not set(raw)<= {'point_ref','item_ref'}:invalid()
            return {k:value(v,'id') for k,v in raw.items()}
        if kind == 'scope':return record(raw,_SCOPE,{'selection_ref':'id'})
        if kind == 'funding_scope':
            scope=_SCOPE | {'source':'literal:simulation','currency':'literal:CNY',
                'portfolio':'literal:current','accounting_basis':'literal:funds.current_budget/1',
                'account_id':'null','kinds':'empty_list','include_life_events':'false',
                'funding_rule':_FUNDING_RULE,'funding_query':_FUNDING_QUERY}
            return record(raw,scope,{'selection_ref':'id'})
        if kind == 'null':
            if raw is not None:invalid()
            return None
        if kind == 'funding_block':
            if type(raw) is str and raw in _FUNDING_BLOCKS:return boundary._alias(raw)
            # Detail block is the exact issued item identity, not another
            # summary enum or arbitrary merchant/record object.
            if (type(raw) is not str or not raw.startswith(('obligation/','hold/'))
                    or len(raw.split('/'))!=2 or not raw.split('/')[1]):invalid()
            return value(raw,'id')
        if kind == 'funding_point_ref':
            import re
            if type(raw) is not str:invalid()
            parts=raw.split(':funds:',1)
            if len(parts)!=2 or re.fullmatch(r'[0-9a-f]{32}',parts[0]) is None:invalid()
            value(parts[1],'funding_block')
            return boundary._alias(raw,prose=False)
        if kind == 'funding_limits':
            return record(raw,_FUNDING_LIMITS) if raw else record(raw,{})
        if kind == 'points':
            if not isinstance(raw,list) or len(raw)>93:invalid()
            for point in raw:
                if not isinstance(point,dict):invalid()
                if 'category_values' in point:
                    if not {'label','selected_item_refs','value','value_scope'} <= set(point) or 'preview' in point:invalid()
                elif not {'date','preview'} <= set(point):invalid()
            return [record(p,_POINT_REQUIRED,_POINT_OPTIONAL) for p in raw]
        if kind == 'portfolio':return record(raw,_PORTFOLIO)
        if kind == 'presentation':
            if (not isinstance(raw,dict) or raw.get('contract')!='navren.presentation/1' or raw.get('schema_version')!=1
                    or raw.get('layout') not in RENDERED_LAYOUTS
                    or raw.get('review_status')!='owner_wiring_required'
                    or raw.get('publication_status')!='owner_wiring_required'):
                invalid()
            if raw['layout'] in FUNDING_LAYOUTS:
                if (raw.get('series_kind')!='funding_breakdown'
                        or raw.get('measure')!='arrangeable_now'
                        or raw.get('measure_basis')!='funds.current_budget/1'
                        or raw.get('allowed_layouts')!=[raw['layout']]):invalid()
                points=raw.get('points')
                if type(points) is not list or len(points)>8:invalid()
                for point in points:
                    if type(point) is not dict:invalid()
                    if point.get('point_ref')!=raw['view_id']+':funds:'+str(point.get('block')):invalid()
                    if raw['layout']=='funding_blocks':
                        if point.get('block') not in _FUNDING_BLOCKS:invalid()
                    elif (not set(_FUNDING_DETAIL)<=set(point)
                            or point.get('block')!=point.get('item_ref')
                            or point.get('block') in _FUNDING_BLOCKS):invalid()
                required=_PRES_REQUIRED | _FUNDING_FIELDS | {
                    'authorization_revision':{'personal':'int','privacy':'nonnegative_int?'},
                    'scope':'funding_scope','data_scope':'id','measure_basis':'literal:funds.current_budget/1',
                    'points':(_FUNDING_POINT | (_FUNDING_DETAIL if raw['layout']=='funding_items' else {}),8)}
                return record(raw,required,_PRES_OPTIONAL)
            return record(raw,_PRES_REQUIRED,_PRES_OPTIONAL)
        invalid()

    common = {'presentation':'presentation','projection_hash':'id','metric_snapshot_id':'id',
              'semantic_review_status':'str','final_publication_status':'str'}
    if not isinstance(review,dict):invalid()
    if presentation_only:return value(review,'presentation')
    if 'text' not in review:
        return record(review, common | {'amount_occurrences':(_OCCURRENCE,24),
                                      'deterministically_validated':'bool'})
    required=common | {'schema_version':'int','text':'body','followups':(_FOLLOWUP,2),
                      'display_amount_occurrences':(_OCCURRENCE,24),'visible_amount_occurrence_count':'int'}
    optional={'language':'str'}
    if not (set(required)|{'body_amount_contract'})<=set(review) or set(review)-set(required)-{'body_amount_contract','language'}:invalid()
    if 'language' in review and review['language'] not in ('zh','en'):invalid()
    result=record({k:v for k,v in review.items() if k!='body_amount_contract'},required,optional)
    result['body_amount_contract']=boundary._amount_contract(review['body_amount_contract'],
        source_text=review['text'],target_text=result['text'])
    return result


class PresentationConsumer:
    def __init__(self, gateway, workspace, request_id):
        self.g, self.w, self.request_id = gateway, workspace, request_id
        self.service = getattr(gateway, 'presentations', None)
        self.entries = {}
        self.prepared = None

    def require_service(self):
        methods = ('query', 'detail', 'review_projection', 'complete_review_projection',
                   'message_state', 'validate_message_state')
        if self.service is None or not all(callable(getattr(self.service, k, None)) for k in methods):
            raise BankError('PRESENTATION_UNAVAILABLE', '受信展示服务尚未接入，本轮不能生成该视图。', 503)

    def issue(self, name, arguments):
        self.require_service()
        if name == 'presentation_query':
            result = self.service.query(self.w, deepcopy(arguments),
                request_id=self.request_id, message_id=self.request_id)
        elif name == 'presentation_detail':
            view_id = arguments['view_id']
            if view_id not in self.entries:
                raise BankError('PRESENTATION_CURRENT_REQUEST_REQUIRED', '详情只能引用本轮已发行视图。', 422)
            result = self.service.detail(self.w, view_id, deepcopy(arguments['selector']),
                request_id=self.request_id, message_id=self.request_id)
        else:
            raise BankError('TOOL_FORBIDDEN', '展示工具未登记。', 403)
        # The original issuer checks its HMAC, workspace, versions and every
        # value slot. No model JSON or digest can stand in for that validation.
        review = self.service.review_projection(self.w, result)
        p = result['presentation']
        if (p['request_id'] != self.request_id or p['message_id'] != self.request_id
                or p['workspace_binding'] != self.w or p['layout'] not in RENDERED_LAYOUTS):
            raise BankError('PRESENTATION_BINDING', '展示身份或图型与本轮不一致。', 409)
        entry = {'result': deepcopy(result), 'review': deepcopy(review)}
        previous = self.entries.get(p['view_id'])
        if previous is not None and previous != entry:
            raise BankError('PRESENTATION_TAMPERED', '同一展示标识出现不同原件。', 409)
        self.entries[p['view_id']] = entry
        return deepcopy(entry)

    def register(self, tools, entry, evidence_id):
        batch = entry['result']['metric_batch']
        self.g.financial_metrics.validate(self.w, batch)
        pending = deepcopy(tools.metrics)
        try:
            for metric in batch['metrics']:
                pending.register(metric, kind=batch['metric_kinds'][metric['metric_id']],
                                 evidence_id=evidence_id)
        except MetricClaimError as error:
            raise BankError(error.code, '展示的正式指标合同不完整。', 422) from error
        # The HTTP request Observer may differ from the gateway bound to the
        # issuer. Collect only the actual validated original, once per batch.
        from base_runtime.evidence import MetricEvidenceObserver
        collections = [tools.metric_batches]
        if isinstance(self.g.financial_metrics, MetricEvidenceObserver):
            collections.append(self.g.financial_metrics.batches)
        for collection in collections:
            same = [b for b in collection if b['snapshot_id'] == batch['snapshot_id']]
            if same and any(b != batch for b in same):
                raise BankError('PRESENTATION_TAMPERED', '同一签名批次出现不同内容。', 409)
        for collection in collections:
            if not any(b['snapshot_id'] == batch['snapshot_id'] for b in collection):
                collection.append(deepcopy(batch))
        tools.metrics = pending
        return [tools.metrics.lookup(m['metric_id']) for m in batch['metrics']]

    def fresh(self):
        for entry in self.entries.values():
            self.service.review_projection(self.w, entry['result'])

    def prepare_final(self, final, selected_widgets, *, has_drafts=False, language='zh'):
        if type(language) is not str or language not in ('zh','en'):
            raise BankError('PRESENTATION_LANGUAGE', '本轮语言必须为zh或en。', 422)
        selected = [w for w in selected_widgets if w.get('type') == 'financial_presentation']
        if not selected:
            self.prepared = None
            return None
        if len(selected) != 1 or len(selected_widgets) != 1 or has_drafts:
            raise BankError('PRESENTATION_SINGLE_VIEW_REQUIRED', '首版仅共审一份只读展示，不混入其他组件或操作。', 422)
        if final.get('followups'):
            raise BankError('PRESENTATION_FOLLOWUPS_REQUIRED', '展示本轮只使用已发行的两条建议，请清空额外建议问题。', 422)
        if final.get('ratio_claims'):
            raise BankError('PRESENTATION_RATIO_BINDING_REQUIRED', '展示状态尚无比例正文绑定接口，请暂只说明范围和缺项。', 422)
        try:
            if percentage_occurrences(final['text']):
                raise BankError('PRESENTATION_RATIO_BINDING_REQUIRED', '展示状态尚无比例正文绑定接口。', 422)
        except RatioClaimError as error:
            raise BankError('PRESENTATION_RATIO_BINDING_REQUIRED', '展示状态尚无比例正文绑定接口。', 422) from error
        vid = selected[0]['widget_id']
        entry = self.entries.get(vid)
        if entry is None or selected[0]['presentation'] != entry['result']['presentation']:
            raise BankError('PRESENTATION_TAMPERED', '组件不是本轮已发行原件。', 409)
        own_ids = {m['metric_id'] for m in entry['result']['metric_batch']['metrics']}
        if any(c['metric_id'] not in own_ids for c in final.get('amount_claims', [])):
            raise BankError('PRESENTATION_BODY_BATCH_REQUIRED', '首版正文金额只能引用所选展示的原批次。', 422)
        complete = self.service.complete_review_projection(self.w, entry['result'], final['text'],
            final.get('amount_claims', []), surface_bindings=[], language=language)
        state = self.service.message_state(self.w, entry['result'], final['text'], language,
            final.get('amount_claims', []), surface_bindings=[])
        self.prepared = {'view_id': vid, 'language':language,'review': deepcopy(complete), 'state': deepcopy(state)}
        return deepcopy(complete)

    def approved_result(self, final, approval, *, language='zh'):
        if self.prepared is None:
            return {}
        if (type(language) is not str or language not in ('zh','en')
                or language!=self.prepared['language']):
            raise BankError('PRESENTATION_LANGUAGE_CHANGED', '审核后本轮语言不能变化。', 409)
        vid = self.prepared['view_id']
        entry = self.entries[vid]
        current = self.service.message_state(self.w, entry['result'], final['text'], language,
            final.get('amount_claims', []), surface_bindings=[])
        if current != self.prepared['state']:
            raise BankError('PRESENTATION_TAMPERED', '审核后展示或正文发生变化。', 409)
        return {'presentation': deepcopy(entry['result']['presentation']),
                'metric_batch': deepcopy(entry['result']['metric_batch']),
                'message_state': deepcopy(current),
                'follow_up_questions': deepcopy(current['followups']),
                'presentation_review': {'agent_approval': deepcopy(approval),
                    'projection_hash': self.prepared['review']['projection_hash'],
                    'server_final_status': 'pending_owner_wiring'}}

    def validate_message_state(self, workspace, state, *, bank_db):
        # The Base issuer reconstructs the original within the final guard's
        # transaction. This callback is local code, never a model argument.
        return self.service.validate_message_state(workspace, state, bank_db=bank_db)
