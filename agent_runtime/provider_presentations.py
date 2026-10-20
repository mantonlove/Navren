"""既有两项原生工具消费本轮真实 Provider；不发行旧 message_state。"""
from copy import deepcopy

from simulated_bank import BankError
from .presentation_consumer import PresentationConsumer
from .provider_consumer import ProviderConsumer
from .ratio_claims import percentage_occurrences, RatioClaimError


class ProviderPresentations(PresentationConsumer):
    def __init__(self, gateway, workspace, request_id, *, language='zh'):
        super().__init__(gateway, workspace, request_id)
        self.provider = ProviderConsumer(gateway, workspace, request_id, language=language)
        self.language = language

    def issue(self, name, arguments):
        if name == 'presentation_query':
            operation = 'query'
        elif name == 'presentation_detail':
            operation = 'detail'
            if arguments.get('view_id') not in self.entries:
                raise BankError('PRESENTATION_CURRENT_REQUEST_REQUIRED', '详情只能引用本轮已发行视图。', 422)
        else:
            raise BankError('TOOL_FORBIDDEN', '展示工具未登记。', 403)
        obj = self.provider.dispatch(operation, arguments)
        original = self.provider._entry(obj)
        # 预算估计引用真正签发且可回查的完整只读原件；无语义审批。
        reserve = self.provider.review(obj, '')
        entry = {'result': deepcopy(original['result']), 'review': deepcopy(original['review']),
                 'provider_object': deepcopy(obj), 'reserve_receipt': deepcopy(reserve)}
        vid = entry['result']['presentation']['view_id']
        if vid in self.entries and self.entries[vid] != entry:
            raise BankError('PRESENTATION_TAMPERED', '同一展示标识出现不同原件。', 409)
        self.entries[vid] = entry
        return deepcopy(entry)

    def register(self, tools, entry, evidence_id):
        return self.provider.register(tools, entry['provider_object'], evidence_id=evidence_id)

    def fresh(self):
        for entry in self.entries.values():
            self.provider.verify(entry['reserve_receipt'])
        if self.prepared is not None:
            self.provider.verify(self.prepared['receipt'])

    def prepare_final(self, final, selected_widgets, *, has_drafts=False, language='zh'):
        if type(language) is not str or language != self.language:
            raise BankError('PRESENTATION_LANGUAGE_CHANGED', '本轮语言不能在工具与终审之间变化。', 409)
        selected = [w for w in selected_widgets if w.get('type') == 'financial_presentation']
        if not selected:
            self.prepared = None
            return None
        if len(selected) != 1 or len(selected_widgets) != 1 or has_drafts:
            raise BankError('PRESENTATION_SINGLE_VIEW_REQUIRED', '本轮共审一份只读展示，不混入操作。', 422)
        if final.get('followups'):
            raise BankError('PRESENTATION_FOLLOWUPS_REQUIRED', '展示使用发行原件的建议，请清空额外问题。', 422)
        if final.get('ratio_claims'):
            raise BankError('PRESENTATION_RATIO_BINDING_REQUIRED', '展示比例正文尚无正式绑定接口。', 422)
        try:
            if percentage_occurrences(final['text']):
                raise BankError('PRESENTATION_RATIO_BINDING_REQUIRED', '展示比例正文尚无正式绑定接口。', 422)
        except RatioClaimError as error:
            raise BankError(error.code, '比例正文无正式绑定。', 422) from error
        entry = self.entries.get(selected[0]['widget_id'])
        if entry is None or selected[0].get('presentation') != entry['result']['presentation']:
            raise BankError('PRESENTATION_TAMPERED', '组件不是本轮完整发行原件。', 409)
        own_ids = {m['metric_id'] for m in entry['result']['metric_batch']['metrics']}
        if any(c['metric_id'] not in own_ids for c in final.get('amount_claims', [])):
            raise BankError('PRESENTATION_BODY_BATCH_REQUIRED', '正文只能引用所选展示原批次。', 422)
        receipt = self.provider.review(entry['provider_object'], final['text'], final.get('amount_claims', []),
                                       ratio_claims=final.get('ratio_claims', []))
        complete = receipt['projection']['complete_review']
        self.prepared = {'view_id': selected[0]['widget_id'], 'language': language,
                         'review': deepcopy(complete), 'receipt': deepcopy(receipt)}
        return deepcopy(complete)

    def approved_result(self, final, approval, *, language='zh'):
        if self.prepared is None:
            return {}
        if type(language) is not str or language != self.language or language != self.prepared['language']:
            raise BankError('PRESENTATION_LANGUAGE_CHANGED', '审核后本轮语言不能变化。', 409)
        receipt = self.prepared['receipt']
        self.provider.verify(receipt)
        complete = receipt['projection']['complete_review']
        entry = self.entries[self.prepared['view_id']]
        if complete['text'] != final['text'] or complete['presentation'] != entry['result']['presentation']:
            raise BankError('PRESENTATION_TAMPERED', '审核后正文或展示发生变化。', 409)
        # 原 Provider receipt 在实际 GLM packet 和本地 final-proof 中，
        # 新历史由基座 ApprovedResponseStore 保存，不借旧 state-v1 宣称批准。
        return {'presentation': deepcopy(entry['result']['presentation']),
                'metric_batch': deepcopy(entry['result']['metric_batch']),
                'follow_up_questions': deepcopy(complete['followups']),
                'presentation_review': {'agent_approval': deepcopy(approval),
                    'projection_hash': receipt['projection']['projection_hash'],
                    'server_final_status': 'pending_owner_wiring'}}
