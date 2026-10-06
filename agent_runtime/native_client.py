"""供应商原生工具协议。隐藏思考仅在一次请求的内存消息链中续传。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .model_adapters import (AIPING_PRIMARY_MODEL, AIPING_URL, DEEPSEEK_MODEL, DEEPSEEK_URL, ModelAdapterError,
                             _key_from_sources, _log_call, _unique_object)


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
        self.urlopen = urlopen or urllib.request.urlopen
        self.key_loader = key_loader or (lambda: _key_from_sources(
            env_name, keychain_service))
        self.env_name, self.keychain_service = env_name, keychain_service
        self.timeout, self.log_path = timeout, log_path
        self.endpoint, self.provider = endpoint, provider
        if model_id is not None:
            self.model_id = model_id

    def request_payload(self, messages, tools):
        """The exact protocol body; credentials belong only to HTTP headers."""
        names = {tool.get('function', {}).get('name') for tool in tools}
        final_only = bool(names) and names <= {'answer_facts', 'answer_reviewed'}
        # Closing needs a specific terminal function. Merely requiring some
        # tool call did not stop the provider returning another read function.
        final_name = 'answer_reviewed' if 'answer_reviewed' in names else 'answer_facts'
        choice = {'type': 'function', 'function': {'name': final_name}} if final_only else 'auto'
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

    def complete(self, messages, tools, *, request_id, remaining_seconds,
                 max_input_bytes=None):
        started = time.monotonic()
        status, outcome, code, usage = None, 'failed', 'provider_error', {}
        request_sent = False
        try:
            wire = self.serialized_request(messages, tools)
            if max_input_bytes is not None and (
                    type(max_input_bytes) is not int or max_input_bytes < 0
                    or len(wire) > max_input_bytes):
                code = 'request_budget_exceeded'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            key = self.key_loader()
            if not key:
                code = 'missing_api_key'
                raise ModelAdapterError(code, model_id=self.model_id, attempts=0)
            req = urllib.request.Request(self.endpoint, method='POST',
                data=wire,
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
            request_sent = True
            with self.urlopen(req, timeout=max(.1, min(self.timeout, remaining_seconds))) as resp:
                status = getattr(resp, 'status', 200)
                envelope = json.loads(resp.read(512_000), object_pairs_hook=_unique_object)
            supplied_usage = envelope.get('usage', {})
            if isinstance(supplied_usage, dict):
                usage = {k: v for k, v in supplied_usage.items()
                         if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                         and type(v) is int and v >= 0}
            choice = envelope['choices'][0]
            if choice.get('finish_reason') == 'length':
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
            for call in calls:
                if (not isinstance(call, dict) or call.get('type') != 'function'
                        or not isinstance(call.get('id'), str) or call['id'] in seen
                        or not isinstance(call.get('function'), dict)
                        or not isinstance(call['function'].get('name'), str)
                        or not isinstance(call['function'].get('arguments'), str)):
                    raise ValueError('invalid_tool_call')
                seen.add(call['id'])
            # 只保留协议字段。reasoning_content 必须按供应商协议原样续传，绝不入审计。
            result = {k: v for k, v in msg.items()
                      if k in ('role', 'content', 'tool_calls', 'reasoning_content')}
            if not calls and not isinstance(msg.get('content'), str):
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
            code = code if code == 'response_truncated' else 'invalid_model_contract'
        finally:
            _log_call({'timestamp': datetime.now(timezone.utc).isoformat(),
                       'request_id': request_id, 'provider': self.provider,
                       'model_id': self.model_id, 'stage': 'conversation.tools',
                       'outcome': outcome, 'duration_ms': round((time.monotonic()-started)*1000),
                       'http_status': status, 'error_code': code, 'usage': usage}, self.log_path)
        error = ModelAdapterError(code, model_id=self.model_id, attempts=1, http_status=status)
        # A rejected/truncated answer still consumed the supplier's reported
        # usage. Expose only counters, never the rejected response body.
        error.usage = usage
        error.request_sent = request_sent
        raise error


class AIPingToolClient(DeepSeekToolClient):
    """OpenAI-compatible function calling through the shared AI Ping key."""

    model_id = AIPING_PRIMARY_MODEL

    def __init__(self, *, urlopen=None, key_loader=None, timeout=35, log_path=None,
                 model_id=None):
        super().__init__(urlopen=urlopen, key_loader=key_loader, timeout=timeout,
            log_path=log_path, endpoint=AIPING_URL, env_name='AIPING_API_KEY',
            keychain_service='CC/AIPING_GLM_API_KEY', provider='ai_ping',
            model_id=model_id or self.model_id)
