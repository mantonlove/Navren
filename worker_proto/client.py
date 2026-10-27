"""Web-side encoder proxy; this process never imports ONNX Runtime."""
from __future__ import annotations

import base64
from pathlib import Path
import socket

from .protocol import ProtocolError, recv, send
from .profile_spec import profile_spec


class WorkerError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class EncoderProxy:
    def __init__(self, socket_path: str | Path, profile: str, *, timeout: float = 185.0):
        if profile not in {'e5','aligned'}:
            raise ValueError('profile_invalid')
        self.socket_path = str(socket_path)
        self.profile = profile
        self.timeout = timeout
        self._metadata = profile_spec(profile)

    def _request(self, op: str, **payload):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                send(connection, {'profile':self.profile,'op':op,**payload})
                result = recv(connection)
        except (OSError, TimeoutError, ProtocolError):
            raise WorkerError('vector_inference_failed') from None
        if result.get('ok') is not True:
            code = result.get('code')
            allowed = {'vector_input_invalid','vector_query_too_long','vector_text_too_long',
                       'vector_model_missing','vector_model_invalid','vector_dependency_missing',
                       'vector_inference_failed'}
            raise WorkerError(code if code in allowed else 'vector_inference_failed')
        return result

    def _describe(self) -> dict:
        return self._metadata

    is_worker_proxy = True

    def runtime_verified(self) -> bool:
        result = self._request('peek')
        if result.get('metadata') != self._metadata:
            raise WorkerError('vector_model_invalid')
        return result.get('runtime_verified') is True

    @property
    def profile_id(self) -> str:
        return self._describe()['profile_id']

    @property
    def dimension(self) -> int:
        return self._describe()['dimension']

    @property
    def max_windows(self) -> int:
        return self._describe()['max_windows']

    @property
    def model_name(self) -> str:
        return self._describe()['model_name']

    @staticmethod
    def _decode(value):
        import numpy as np
        if type(value) is not dict or set(value) != {'shape','float32_le_b64'}:
            raise WorkerError('vector_inference_failed')
        shape = value['shape']
        if (type(shape) is not list or len(shape) not in (1,2)
                or any(type(part) is not int for part in shape)
                or shape[-1] != 384 or (len(shape) == 2 and not 1 <= shape[0] <= 16)):
            raise WorkerError('vector_inference_failed')
        try:
            raw = base64.b64decode(value['float32_le_b64'], validate=True)
        except (ValueError, TypeError):
            raise WorkerError('vector_inference_failed') from None
        expected = 4 * (384 if len(shape) == 1 else shape[0] * 384)
        if len(raw) != expected:
            raise WorkerError('vector_inference_failed')
        array = np.frombuffer(raw, dtype='<f4').reshape(shape).copy()
        if not np.isfinite(array).all():
            raise WorkerError('vector_inference_failed')
        return array

    def encode_query(self, text: str):
        result = self._request('query', text=text)
        if result.get('metadata') != self._metadata:
            raise WorkerError('vector_model_invalid')
        return self._decode(result.get('result'))

    def encode_documents(self, texts: list[str]):
        response = self._request('documents', texts=texts)
        if response.get('metadata') != self._metadata:
            raise WorkerError('vector_model_invalid')
        result = response.get('result')
        if type(result) is not list or len(result) != len(texts):
            raise WorkerError('vector_inference_failed')
        return [self._decode(value) for value in result]
