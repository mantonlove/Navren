"""供应商原生工具协议。隐藏思考仅在一次请求的内存消息链中续传。"""
from __future__ import annotations

import json
import hashlib
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .model_adapters import (DEEPSEEK_MODEL, DEEPSEEK_URL, ModelAdapterError,
                             _key_from_sources, _log_call, _unique_object,
                             _validate_official_transport, _official_urlopen)
from .request_budget import RequestBudgetError, estimate_request


class DeepSeekToolClient:
    model_id = DEEPSEEK_MODEL
    billing_mode = 'paid'
    output_token_limit = 4000
    # Transport capability only. Full monetary signatures stay in the local
    # registry; a turn-scoped source catalogue is expanded before validation.
    supports_compact_metric_sources = True

    def __init__(self, *, urlopen=None, key_loader=None, timeout=35, log_path=None,
                 endpoint=DEEPSEEK_URL, env_name='DEEPSEEK_API_KEY',
                 keychain_service='CC/DEEPSEEK_API_KEY', provider='deepseek',
                 model_id=None):
        if provider != 'deepseek' or (model_id or self.model_id) != DEEPSEEK_MODEL:
            raise ModelAdapterError('official_native_deepseek_required',
                                    model_id=model_id or self.model_id, attempts=0)
        _validate_official_transport(provider=provider, model_id=model_id or self.model_id,
            url=endpoint, env_name=env_name, keychain_service=keychain_service)
        self.urlopen = urlopen or _official_urlopen
        self.key_loader = key_loader or (lambda: _key_from_sources(
            env_name, keychain_service))
        self.env_name, self.keychain_service = env_name, keychain_service
        self.timeout, self.log_path = timeout, log_path
        self.endpoint, self.provider = endpoint, provider
        if model_id is not None:
            self.model_id = model_id

    def _validate_transport(self):
        if self.provider != 'deepseek' or self.model_id != DEEPSEEK_MODEL:
            raise ModelAdapterError('official_native_deepseek_required',
                                    model_id=self.model_id, attempts=0)
        _validate_official_transport(provider=self.provider, model_id=self.model_id,
            url=self.endpoint, env_name=self.env_name, keychain_service=self.keychain_service)

    def request_payload(self, messages, tools):
        """The exact protocol body; credentials belong only to HTTP headers."""
        self._validate_transport()
        names = {tool.get('function', {}).get('name') for tool in tools}
        final_only = bool(names) and names <= {'answer_facts', 'answer_reviewed'}
        # Closing needs a specific terminal function. Merely requiring some
        # tool call did not stop the provider returning another read function.
        final_name = 'answer_reviewed' if 'answer_reviewed' in names else 'answer_facts'
        choice = ({'type': 'function', 'function': {'name': final_name}} if final_only
                  else 'required' if names else 'auto')
        return {'model': self.model_id, 'messages': messages, 'tools': tools,
                'tool_choice': choice,
                'temperature': 0, 'max_tokens': 4000,
                'thinking': {'type': 'disabled'}}

    def serialized_request(self, messages, tools):
        return json.dumps(self.request_payload(messages, tools), ensure_ascii=False,
                          allow_nan=False, separators=(',', ':')).encode('utf-8')

    def request_input_bound(self, messages, tools):
        """Full wire-body bytes used for local admission, not actual token usage."""
        return len(self.serialized_request(messages, tools))

    def request_input_token_estimate(self, messages, tools):
        return estimate_request(self.serialized_request(messages, tools))

    def complete(self, messages, tools, *, request_id, remaining_seconds,
                 max_input_bytes=None, expected_request_sha256=None,
                 max_input_token_estimate=None):
        started = time.monotonic()
        status, outcome, code, usage = None, 'failed', 'provider_error', {}
        request_sent = False
        response_protocol = {}
        try:
            self._validate_transport()
            if (type(remaining_seconds) not in (int, float)
                    or type(self.timeout) not in (int, float)
                    or not math.isfinite(remaining_seconds) or not math.isfinite(self.timeout)
                    or min(remaining_seconds, self.timeout) <= 0):
                code = 'request_deadline_exceeded'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            deadline = started + min(self.timeout, remaining_seconds)
            def remaining():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ModelAdapterError('request_deadline_exceeded',
                        model_id=self.model_id, attempts=int(request_sent))
                return left
            wire = self.serialized_request(messages, tools)
            actual_payload = json.loads(wire, object_pairs_hook=_unique_object)
            if (actual_payload.get('model') != self.model_id
                    or actual_payload.get('thinking') != {'type': 'disabled'}
                    or 'enable_thinking' in actual_payload or 'provider' in actual_payload):
                code = 'official_model_payload_mismatch'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            try:
                admission = estimate_request(wire)
            except (RequestBudgetError, OSError):
                code = 'request_budget_estimator_unavailable'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0) from None
            if (expected_request_sha256 is not None
                    and admission['request_sha256'] != expected_request_sha256):
                code = 'request_budget_protocol_mismatch'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            if max_input_token_estimate is not None and (
                    type(max_input_token_estimate) is not int
                    or max_input_token_estimate < admission['input_token_estimate']):
                code = 'request_budget_exceeded'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            if max_input_bytes is not None and (
                    type(max_input_bytes) is not int or max_input_bytes < 0
                    or len(wire) > max_input_bytes):
                code = 'request_budget_exceeded'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            remaining()
            self._validate_transport()
            key = self.key_loader()
            if not key:
                code = 'missing_api_key'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            self._validate_transport()
            req = urllib.request.Request(self.endpoint, method='POST',
                data=wire,
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
            # Preparation and key loading consume the caller's existing
            # allowance. urllib still applies a socket I/O timeout; this is
            # not a claim that DNS/connect/body phases share a hard deadline.
            request_timeout = remaining()
            self._validate_transport()
            _validate_official_transport(provider=self.provider, model_id=self.model_id,
                url=req.full_url, env_name=self.env_name, keychain_service=self.keychain_service)
            request_sent = True
            with self.urlopen(req, timeout=request_timeout) as resp:
                status = getattr(resp, 'status', 200)
                remaining()
                envelope = json.loads(resp.read(512_000), object_pairs_hook=_unique_object)
            supplied_usage = envelope.get('usage', {})
            if isinstance(supplied_usage, dict):
                usage = {k: v for k, v in supplied_usage.items()
                         if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                         and type(v) is int and v >= 0}
            # A late body can still provide auditable usage, but cannot become
            # an admitted model answer. The original caller also checks its
            # turn deadline before review and final publication.
            remaining()
            details = supplied_usage.get('completion_tokens_details') if isinstance(supplied_usage,dict) else None
            reasoning_tokens = details.get('reasoning_tokens') if isinstance(details,dict) else None
            response_protocol['reasoning_tokens'] = reasoning_tokens if type(reasoning_tokens) is int and reasoning_tokens>=0 else None
            if usage.get('prompt_tokens', 0) > admission['input_token_estimate']:
                code = 'budget_estimator_drift'
                raise ValueError(code)
            choice = envelope['choices'][0]
            finish_reason = choice.get('finish_reason')
            response_protocol['finish_reason'] = (finish_reason if finish_reason in
                ('stop','length','tool_calls','function_call','content_filter',None) else 'other')
            if finish_reason == 'length':
                code = 'response_truncated'
                raise ValueError(code)
            msg = choice['message']
            if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                raise ValueError('invalid_provider_message')
            if (msg.get('content') is not None and not isinstance(msg['content'],str)
                    or msg.get('reasoning_content') is not None and not isinstance(msg['reasoning_content'],str)):
                raise ValueError('invalid_provider_message')
            calls = msg.get('tool_calls') or []
            if not isinstance(calls, list) or len(calls) > 12:
                raise ValueError('invalid_tool_calls')
            seen = set()
            response_protocol['tool_arguments'] = []
            for call in calls:
                if (not isinstance(call, dict) or call.get('type') != 'function'
                        or not isinstance(call.get('id'), str) or call['id'] in seen
                        or not isinstance(call.get('function'), dict)
                        or not isinstance(call['function'].get('name'), str)
                        or not isinstance(call['function'].get('arguments'), str)):
                    raise ValueError('invalid_tool_call')
                seen.add(call['id'])
                raw_arguments = call['function']['arguments']
                parse_kind = None
                try:
                    parsed_arguments = json.loads(raw_arguments,object_pairs_hook=_unique_object,
                        parse_constant=lambda _:(_ for _ in()).throw(ValueError()))
                    if not isinstance(parsed_arguments,dict):parse_kind = 'not_object'
                except json.JSONDecodeError:
                    parse_kind = 'invalid_json'
                except (ValueError,TypeError):
                    parse_kind = 'duplicate_key_or_nonfinite_json'
                response_protocol['tool_arguments'].append({'index':len(response_protocol['tool_arguments']),
                    'parse_error_kind':parse_kind,'length':len(raw_arguments),
                    'sha256':hashlib.sha256(raw_arguments.encode('utf-8')).hexdigest()})
            # 只保留协议字段。reasoning_content 必须按供应商协议原样续传，绝不入审计。
            result = {k: v for k, v in msg.items()
                      if k in ('role', 'content', 'tool_calls', 'reasoning_content')}
            if not calls and (not isinstance(msg.get('content'), str) or not msg['content'].strip()):
                raise ValueError('empty_model_response')
            outcome, code = 'completed', ''
            return result, usage
        except ModelAdapterError as error:
            code=error.code
            error.request_sent = request_sent
            if request_sent and error.attempts == 0:error.attempts = 1
            supplied = usage or getattr(error,'usage',{})
            usage = {k:v for k,v in supplied.items() if k in
                ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0} if isinstance(supplied,dict) else {}
            error.usage = dict(usage)
            raise
        except urllib.error.HTTPError as error:
            status, code = error.code, 'provider_http_error'
        except (urllib.error.URLError, OSError, TimeoutError):
            code = 'provider_unavailable'
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            code = code if code in ('response_truncated', 'budget_estimator_drift') else 'invalid_model_contract'
        finally:
            _log_call({'timestamp': datetime.now(timezone.utc).isoformat(),
                       'request_id': request_id, 'provider': self.provider,
                       'model_id': self.model_id, 'stage': 'conversation.tools',
                       'outcome': outcome, 'duration_ms': round((time.monotonic()-started)*1000),
                       'http_status': status, 'error_code': code, 'usage': usage,
                       'response_protocol':response_protocol}, self.log_path)
        error = ModelAdapterError(code, model_id=self.model_id, attempts=1, http_status=status)
        # A rejected/truncated answer still consumed the supplier's reported
        # usage. Expose only counters, never the rejected response body.
        error.usage = usage
        error.request_sent = request_sent
        raise error


class AIPingToolClient(DeepSeekToolClient):
    """兼容旧公共类名；仅使用官方 DeepSeek 原生工具协议。"""

    model_id = DEEPSEEK_MODEL
    metric_selection_support = True

    def request_payload(self, messages, tools):
        payload = super().request_payload(messages, tools)
        if self.supports_financial_metric_catalog:
            from .primary_tool_transport import pack_primary_payload
            payload = pack_primary_payload(payload)
        return payload

    def __init__(self, *, urlopen=None, key_loader=None, timeout=105, log_path=None,
                 model_id=None):
        super().__init__(urlopen=urlopen, key_loader=key_loader, timeout=timeout,
            log_path=log_path, endpoint=DEEPSEEK_URL, env_name='DEEPSEEK_API_KEY',
            keychain_service='CC/DEEPSEEK_API_KEY', provider='deepseek',
            model_id=model_id or self.model_id)
        # 能力只绑定当前已支持的官方模型和传输；未知/变异配置不能取得引用能力。
        self.supports_financial_metric_catalog = (
            self.provider == 'deepseek' and self.endpoint == DEEPSEEK_URL
            and self.model_id == DEEPSEEK_MODEL and self.env_name == 'DEEPSEEK_API_KEY'
            and self.keychain_service == 'CC/DEEPSEEK_API_KEY')
