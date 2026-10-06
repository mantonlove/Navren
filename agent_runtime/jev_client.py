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


class JevClient:
    model_id = 'jev-1.13.0'
    url = 'https://api.typesafe.ai/v1/systemone'

    def __init__(self, *, urlopen=None, key_loader=None, timeout=5.0):
        if not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError('invalid_jev_timeout')
        self.urlopen = urlopen or urllib.request.urlopen
        self.key_loader = key_loader or (lambda: _key_from_sources('TYPESAFE_API_KEY', 'CC/TYPESAFE_API_KEY'))
        self.timeout = timeout

    def evaluate(self, state, questions, *, max_input_bytes=16000):
        wire = json.dumps({'model': self.model_id, 'state': state, 'questions': questions},
                          ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        receipt = {'model_requested': self.model_id, 'request_sent': False,
                   'input_bytes': len(wire), 'request_sha256': hashlib.sha256(wire).hexdigest(),
                   'duration_ms': 0, 'usage': None, 'outcome': 'not_sent'}
        if len(wire) > max_input_bytes:
            raise JevError('jev_input_limit', receipt)
        key = self.key_loader()
        if not key:
            raise JevError('jev_key_unavailable', receipt)
        request = urllib.request.Request(self.url, data=wire, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        started = time.monotonic()
        try:
            receipt['request_sent'] = True
            with self.urlopen(request, timeout=self.timeout) as response:
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
