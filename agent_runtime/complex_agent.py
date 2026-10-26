"""有界意图分流；两种提供器共享 Skill、工作流宿主与原业务终结链。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
import uuid

from simulated_bank import BankError
from .conversation import ConversationAgent
from .jev_client import JevClient, JevError
from .model_adapters import (GLM_REVIEW_MODEL, GLM_ALTERNATE_REVIEW_MODEL,
                             ModelAdapterError, _validate_official_transport)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(',', ':'))


class _StrictAiPingOpen:
    """仅保留旧导入名；旧聚合器通道已关闭。"""
    def __init__(self, opener, model):
        self.model = model

    def __call__(self, request, **kwargs):
        raise ModelAdapterError('official_only_legacy_aiping_route_disabled',
                                model_id=self.model, attempts=0)


class AIPingIntentJudge:
    """旧实验入口仅作明确拒绝兼容，不能加载凭据或出站。"""
    def __init__(self, *args, **kwargs):
        raise ValueError('official_only_legacy_intent_route_disabled')

    def evaluate(self, *args, **kwargs):
        raise ValueError('official_only_legacy_intent_route_disabled')


class ComplexRoutingClient:
    def __init__(self, primary, judge, *, provider, request_id, debit,
                 positive_threshold=.9, negative_threshold=.1):
        if provider not in ('native', 'jev'):
            raise ValueError('invalid_route_provider')
        self.primary, self.judge, self.provider = primary, judge, provider
        self.request_id, self.debit = request_id, debit
        self.positive_threshold, self.negative_threshold = positive_threshold, negative_threshold
        self.prepared = False
        self.turn_guard = None
        self.route_admission = None
        self.bypass_reason = None
        self.routing_skills = ()
        self.receipts, self.local_receipts = [], []
        self.runner, self.route_input, self.decision = None, None, None
        self.selection = {'applied': False, 'provider': provider,
                          'reason': 'not_prepared', 'policy_version': 'complex-intent/1'}

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def bind_turn_guards(self, *, before_request, admit_route):
        if self.prepared or not callable(before_request) or not callable(admit_route):
            raise BankError('ROUTING_GUARD_INVALID', '意图分流守卫绑定无效。')
        self.turn_guard, self.route_admission = before_request, admit_route

    def skip_route_for_turn(self, reason):
        if self.prepared or reason != 'deterministic_facts_only_query':
            raise BankError('ROUTING_GUARD_INVALID', '意图分流跳过协议无效。')
        self.bypass_reason = reason

    def _fresh(self):
        if self.turn_guard is None:
            raise BankError('ROUTING_GUARD_UNBOUND', '意图分流缺少请求级守卫。')
        self.turn_guard()
        guard = getattr(self.primary, '_before_request', None)
        if callable(guard): guard()

    def _record(self, receipt):
        self.receipts.append(receipt)
        usage = receipt.get('usage')
        known = (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                 for k in ('input_tokens', 'output_tokens')))
        if known:
            self.debit(usage['input_tokens'] + usage['output_tokens'])
        elif receipt.get('request_sent'):
            raise BankError('MODEL_USAGE_UNVERIFIED', '意图分流用量未知，已停止后续模型调用。', 503)

    def prepare_turn(self, messages, *, deadline, on_event=None):
        started = closed = False
        def progress(event):
            nonlocal started, closed
            if event.get('kind') == 'activity' and event.get('name') == 'intent_route':
                started |= event.get('status') == 'started'
                closed |= event.get('status') in ('completed', 'failed')
            on_event(event)
        try:
            return self._prepare_turn(messages, deadline=deadline,
                                      on_event=progress if on_event else None)
        finally:
            if on_event and started and not closed:
                on_event({'kind': 'activity', 'call_id': 'intent-route', 'activity_kind': 'skill',
                          'name': 'intent_route', 'label': '业务目标识别', 'status': 'failed', 'attempt': 1})

    def _prepare_turn(self, messages, *, deadline, on_event=None):
        if self.prepared: return
        self.prepared = True
        if self.bypass_reason:
            self.selection['reason'] = self.bypass_reason
            return
        if getattr(self.primary, 'allowed_tools', None):
            self.selection['reason'] = 'trusted_scope_already_selected'
            return
        from .intent_routing import build_route_input, questions_for_input, normalize_route_answers
        from .workflow_registry import WorkflowRunner
        # Hash the projected context for freshness binding; do not send the full
        # context to the router. The input builder owns its small allowlist.
        context_version = hashlib.sha256(_encode(messages).encode()).hexdigest()
        try:
            self.route_input = build_route_input(messages, request_id=self.request_id,
                                                 context_version=context_version)
            questions = questions_for_input(self.route_input)
            self._fresh()
            state = json.loads(self.route_input.state_json)
            bound = getattr(self.judge, 'request_input_bound', None)
            if callable(bound):
                input_estimate = bound(state, questions)
                output_estimate = self.judge.output_token_reserve
                self.route_admission(input_estimate, output_estimate)
            elif getattr(self.primary, 'billing_mode', None) != 'unpaid_fixture':
                raise BankError('ROUTING_BOUND_UNAVAILABLE', '意图分流缺少完整输入准入估算。')
            if on_event:
                on_event({'kind': 'activity', 'call_id': 'intent-route', 'activity_kind': 'skill',
                          'name': 'intent_route', 'label': '业务目标识别', 'status': 'started', 'attempt': 1})
            if callable(bound):
                answers, receipt = self.judge.evaluate(state, questions, deadline=deadline,
                                                       before_request=self._fresh)
            else:  # 仅离线注入的测试替身；付费提供器必须具有准入与发送前守卫。
                answers, receipt = self.judge.evaluate(state, questions, deadline=deadline)
        except JevError as error:
            self.selection['reason'] = error.code
            self._record(error.receipt)
            if 'model_version_mismatch' in error.code:
                raise BankError('ROUTING_PROVIDER_INVALID', '意图分流模型身份不符，本轮停止。', 503)
            return
        except ValueError as error:
            self.selection['reason'] = 'route_input_not_ready'
            return
        self._record(receipt)
        try:
            self.decision = normalize_route_answers(self.route_input, answers, provider=self.provider,
                positive_threshold=self.positive_threshold, negative_threshold=self.negative_threshold)
        except ValueError:
            self.selection['reason'] = 'route_answers_invalid'
            return
        self.routing_skills = tuple(self.decision.routing_skills)
        self.selection.update(applied=bool(self.routing_skills), reason=self.decision.reason,
            status=self.decision.status, skills=list(self.routing_skills),
            workflow_ids=list(self.decision.workflow_ids), binding=self.decision.binding)
        if self.decision.status == 'workflow':
            self.runner = WorkflowRunner(self.route_input, self.decision)
        if on_event:
            on_event({'kind': 'activity', 'call_id': 'intent-route', 'activity_kind': 'skill',
                      'name': 'intent_route', 'label': '业务目标识别', 'status': 'completed', 'attempt': 1})

    def local_workflow_step(self, view_messages, schemas, round_number, remaining_tools):
        if self.runner is None: return None
        self._fresh()
        value = self.runner.local_workflow_step(view_messages, schemas, round_number, remaining_tools)
        if value is not None:
            self.local_receipts.append(value[1])
        return value

    def minimum_workflow_rounds_remaining(self):
        if self.runner is None: return 0
        return self.runner.minimum_rounds_remaining()

    def complete(self, messages, tools, **kwargs):
        self._fresh()
        return self.primary.complete(messages, tools, **kwargs)

    @staticmethod
    def _known_usage(receipt):
        usage=receipt.get('usage')
        if (isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0
                for k in ('input_tokens','output_tokens'))):
            return usage['input_tokens']+usage['output_tokens']
        return None

    def augment_runtime(self, runtime):
        if 'intent_route' in runtime: return
        known=sum(self._known_usage(r) or 0 for r in self.receipts)
        complete=all(not r.get('request_sent') or self._known_usage(r) is not None for r in self.receipts)
        runtime['intent_route']={'selection':self.selection,'calls':self.receipts,
            'local_workflow_steps':self.local_receipts,'http_retries':0,
            'score_calibration':'development_initial_not_calibrated',
            'local_receipt_semantics':'proposals; actual execution status is in runtime.trace'}
        runtime['conversation_total_tokens']=runtime.get('total_tokens')
        runtime['total_tokens']=(runtime['conversation_total_tokens']+known
            if runtime['conversation_total_tokens'] is not None and complete else None)
        runtime['usage_complete']=runtime.get('usage_complete',False) and complete
        runtime['budget_tokens']+=known
        runtime['budget_policy']['limit']=self.original_token_limit
        runtime['known_tokens_lower_bound']=runtime.get('known_tokens_lower_bound',0)+known
        runtime['duration_ms']=round((time.monotonic()-self.turn_started)*1000)
        runtime['intent_requests_sent']=sum(r.get('request_sent') is True for r in self.receipts)
        for receipt in self.receipts:
            total=self._known_usage(receipt)
            usage=receipt.get('usage')
            runtime['model_calls'].append({'stage':'intent_route','provider':self.provider,
                'model_id':receipt.get('model_actual') or receipt.get('model_requested'),
                'outcome':receipt.get('outcome','failed'),'request_sent':receipt.get('request_sent',False),
                'usage':({'prompt_tokens':usage['input_tokens'],'completion_tokens':usage['output_tokens'],
                          'total_tokens':total} if total is not None else None),
                'usage_complete':total is not None or not receipt.get('request_sent'),
                'duration_ms':receipt.get('duration_ms'),'billing_mode':'paid',
                'budget_tokens':total,'budget_source':'provider_usage' if total is not None else 'unknown_usage_stop',
                'known_tokens_lower_bound':total or 0})


class ComplexConversationAgent(ConversationAgent):
    """显式候选入口；正式默认服务不因此自动启用 Jev。"""
    def __init__(self, *args, route_provider='off', judge=None,
                 positive_threshold=.9, negative_threshold=.1, **kwargs):
        # 当前正式入口保持 off；未启用的第三模型/旧聚合器分流不能暗中出站。
        if route_provider not in ('off', 'native', 'jev'):
            raise ValueError('invalid_route_provider')
        if route_provider != 'off' or judge is not None:
            raise ValueError('official_only_intent_routing_disabled')
        kwargs.setdefault('tool_routing', 'progressive')
        super().__init__(*args, **kwargs)
        if getattr(self.primary,'billing_mode',None) != 'unpaid_fixture':
            _validate_official_transport(provider=self.primary.provider, model_id=self.primary.model_id,
                url=self.primary.endpoint, env_name=self.primary.env_name,
                keychain_service=self.primary.keychain_service)
        if getattr(self.reviewer,'billing_mode',None) != 'unpaid_fixture':
            review_client = getattr(self.reviewer,'client',None)
            if (review_client is None or self.reviewer.model_id != review_client.model_id
                    or review_client.model_id not in (GLM_REVIEW_MODEL, GLM_ALTERNATE_REVIEW_MODEL)
                    or getattr(review_client,'max_retries',None) != 0):
                raise ValueError('official_glm_reviewer_route_required')
            _validate_official_transport(provider=review_client.provider, model_id=review_client.model_id,
                url=review_client.url, env_name=review_client.env_name,
                keychain_service=review_client.keychain_service)
        self.route_provider = 'off'
        self.judge = None
        self.positive_threshold, self.negative_threshold = positive_threshold, negative_threshold

    def process(self, *args, **kwargs):
        if self.route_provider == 'off': return super().process(*args, **kwargs)
        started = time.monotonic()
        request_id = kwargs.get('request_id') or uuid.uuid4().hex
        kwargs['request_id'] = request_id
        child = ConversationAgent(primary=self.primary, reviewer=self.reviewer,
            max_rounds=self.max_rounds, max_tools=self.max_tools, max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds, tool_routing=self.tool_routing)
        child._locks = self._locks
        def debit(tokens):
            child.max_tokens -= tokens
            if child.max_tokens <= 0:
                raise BankError('AGENT_BUDGET_EXCEEDED', '分流后剩余模型预算不足。', 503)
        wrapper = ComplexRoutingClient(self.primary, self.judge, provider=self.route_provider,
            request_id=request_id, debit=debit, positive_threshold=self.positive_threshold,
            negative_threshold=self.negative_threshold)
        child.primary = wrapper
        wrapper.turn_started,wrapper.original_token_limit=started,self.max_tokens
        gateway = args[0] if args else kwargs['gateway']
        workspace = args[1] if len(args) > 1 else kwargs['workspace']
        audit_written = False
        def enrich(result):
            nonlocal audit_written
            runtime = result['agent_runtime']
            # The original local greeting branch has its own exact contract and
            # never prepares or sends this router. Preserve it before finalizer
            # validation, including its zero-call metadata and local audit.
            if runtime.get('architecture') == 'local_rule' and not wrapper.receipts:
                return result
            wrapper.augment_runtime(runtime)
            if audit_written: return result
            with gateway.bank.transaction() as db:
                gateway.bank._audit(db, workspace, 'agent_intent_route_completed', {
                    'request_id': result['bank_response_id'], 'intent_route': runtime['intent_route'],
                    'total_tokens': runtime['total_tokens'], 'usage_complete': runtime['usage_complete'],
                    'budget_tokens': runtime['budget_tokens'], 'duration_ms': runtime['duration_ms']})
            audit_written = True
            return result
        finalize = kwargs.get('finalize_result')
        if finalize is not None:
            kwargs['finalize_result'] = lambda result, *tail: finalize(enrich(result), *tail)
        return enrich(child.process(*args, **kwargs))
