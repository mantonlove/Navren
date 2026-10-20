"""有界、可计量的 TypeSafe HTTP 客户端；密钥只进入请求头。"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request

from .model_adapters import _key_from_sources, _unique_object


class JevError(Exception):
    def __init__(self, code, receipt):
        super().__init__(code)
        self.code, self.receipt = code, receipt


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, 'jev_redirect_rejected', headers, response)


class JevClient:
    model_id = 'jev-1.13.0'
    url = 'https://api.typesafe.ai/v1/systemone'
    output_token_reserve = 4096  # 本地估算准入，提供器没有可设置的输出token硬上限。

    def serialized_request(self, state, questions):
        return json.dumps({'model': self.model_id, 'state': state, 'questions': questions},
                          ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

    def request_input_bound(self, state, questions):
        return len(self.serialized_request(state, questions))

    def __init__(self, *, urlopen=None, key_loader=None, timeout=5.0):
        if not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError('invalid_jev_timeout')
        # 固定官方连接，不继承系统代理或把认证头转交重定向主机。
        self.urlopen = urlopen or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()).open
        self.key_loader = key_loader or (lambda: _key_from_sources('TYPESAFE_API_KEY', 'CC/TYPESAFE_API_KEY'))
        self.timeout = timeout

    def evaluate(self, state, questions, *, max_input_bytes=16000, deadline=None, before_request=None):
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError('invalid_jev_deadline')
        wire = self.serialized_request(state, questions)
        receipt = {'model_requested': self.model_id, 'request_sent': False,
                   'input_bytes': len(wire), 'request_sha256': hashlib.sha256(wire).hexdigest(),
                   'duration_ms': 0, 'usage': None, 'outcome': 'not_sent'}
        # 先核对有限协议，错误问题不能产生付费请求。
        if (not isinstance(questions, dict) or not 1 <= len(questions) <= 64
                or any(not isinstance(name, str) or not name or len(name) > 128
                       for name in questions)):
            raise JevError('jev_invalid_questions', receipt)
        for question in questions.values():
            if (not isinstance(question, dict)
                    or set(question) - {'type', 'instructions', 'criteria'}
                    or question.get('type') not in ('choice', 'noul')):
                raise JevError('jev_unsupported_question', receipt)
            if 'instructions' in question and (
                    not isinstance(question['instructions'], str)
                    or not 1 <= len(question['instructions']) <= 4096):
                raise JevError('jev_invalid_question_instructions', receipt)
            criteria = question.get('criteria')
            if question['type'] == 'choice':
                if (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 128
                        or any(not isinstance(k, str) or not k or len(k) > 256
                               or v is not None and (not isinstance(v, str) or len(v) > 4096)
                               for k, v in criteria.items())):
                    raise JevError('jev_invalid_choice_criteria', receipt)
            elif (not question.get('instructions') or criteria is not None and (
                    not isinstance(criteria, dict) or set(criteria) != {'true', 'false'}
                    or any(not isinstance(v, str) or not v or len(v) > 4096
                           for v in criteria.values()))):
                raise JevError('jev_invalid_noul_criteria', receipt)
        if len(wire) > max_input_bytes:
            raise JevError('jev_input_limit', receipt)
        if deadline is not None and time.monotonic() >= deadline:
            raise JevError('jev_deadline_exceeded', receipt)
        key = self.key_loader()
        # 钥匙串加载也消耗同一轮时间；取到key后不能沿用旧剩余时间。
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise JevError('jev_deadline_exceeded', receipt)
        if not key:
            raise JevError('jev_key_unavailable', receipt)
        if before_request is not None:
            before_request()
        request = urllib.request.Request(self.url, data=wire, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        started = time.monotonic()
        remaining = None if deadline is None else deadline - started
        if remaining is not None and remaining <= 0:
            raise JevError('jev_deadline_exceeded', receipt)
        try:
            receipt['request_sent'] = True
            with self.urlopen(request, timeout=self.timeout if remaining is None else min(self.timeout, remaining)) as response:
                raw = response.read(256000)
            result = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(result, dict):
                raise ValueError('jev_invalid_envelope')
            usage = result.get('usage', {})
            if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                    for k in ('input_tokens', 'output_tokens'))):
                raise ValueError('jev_usage_unverified')
            receipt['usage'] = {k: usage[k] for k in ('input_tokens', 'output_tokens')}
            receipt['model_actual'] = result.get('model')
            if result.get('model') != self.model_id:
                raise ValueError('jev_model_version_mismatch')
            answers = result.get('answers')
            if not isinstance(answers, dict) or set(answers) != set(questions):
                raise ValueError('jev_answer_keys_mismatch')
            for name, question in questions.items():
                answer = answers[name]
                if not isinstance(answer, dict) or answer.get('type') != question['type']:
                    raise ValueError('jev_answer_type_mismatch')
                if question['type'] == 'choice':
                    probabilities = answer.get('probabilities', {})
                    if (not isinstance(probabilities, dict) or set(probabilities) != set(question['criteria']) or
                            answer.get('choice') not in probabilities):
                        raise ValueError('jev_invalid_choice')
                    values = list(probabilities.values()) + [answer.get('confidence')]
                    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                        raise ValueError('jev_invalid_probability')
                    if abs(sum(probabilities.values()) - 1) > 0.01:
                        raise ValueError('jev_probability_sum')
                elif question['type'] == 'noul':
                    if (set(answer) != {'type', 'noul'}
                            or type(answer.get('noul')) not in (int, float)
                            or not math.isfinite(answer['noul'])
                            or not 0 <= answer['noul'] <= 1):
                        raise ValueError('jev_invalid_noul_probability')
                else:
                    raise ValueError('jev_unsupported_question')
            receipt['outcome'] = 'completed'
            return answers, receipt
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
            receipt['outcome'] = 'failed'
            receipt['error_code'] = str(error) if isinstance(error, ValueError) else type(error).__name__
            raise JevError(receipt['error_code'], receipt) from None
        finally:
            receipt['duration_ms'] = round((time.monotonic() - started) * 1000)
