"""Web-side encoder proxy; this process never imports ONNX Runtime."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import math
import os
from pathlib import Path
import pwd
import socket
import struct
import sys
import time

from .protocol import ProtocolError, recv, send
from .profile_spec import profile_spec

_ENCODER_DEADLINE: ContextVar[float | None] = ContextVar('navren_encoder_deadline',default=None)


@contextmanager
def encoder_budget(seconds: float):
    """Share one deadline across all vector views of a single search."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('budget_invalid')
    previous = _ENCODER_DEADLINE.get()
    deadline = time.monotonic()+seconds
    token = _ENCODER_DEADLINE.set(min(previous,deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _ENCODER_DEADLINE.reset(token)


class WorkerError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require_worker_peer(connection: socket.socket, expected_uid: int) -> None:
    """Authenticate the connected Unix peer before sending any query text."""
    if not sys.platform.startswith('linux'):
        return  # This candidate's production unit is Linux-only.
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize('3i'))
        _, uid, _ = struct.unpack('3i', raw)
    except (AttributeError, OSError, struct.error):
        raise WorkerError('vector_inference_failed') from None
    if uid != expected_uid:
        raise WorkerError('vector_inference_failed')


class EncoderProxy:
    def __init__(self, socket_path: str | Path, profile: str, *, timeout: float = 40.0,
                 peek_timeout: float = 0.3):
        if profile not in {'e5','aligned'}:
            raise ValueError('profile_invalid')
        if (not math.isfinite(timeout) or not math.isfinite(peek_timeout)
                or timeout <= 0 or peek_timeout <= 0):
            raise ValueError('timeout_invalid')
        self.socket_path = str(socket_path)
        self.profile = profile
        self.timeout = timeout
        self.peek_timeout = min(peek_timeout, timeout)
        self._metadata = profile_spec(profile)
        if sys.platform.startswith('linux'):
            if os.environ.get('NAVREN_ENCODER_BACKEND') == 'simulate':
                self._server_uid = os.geteuid()
            else:
                try:
                    self._server_uid = pwd.getpwnam('navren-encoder').pw_uid
                except KeyError:
                    raise WorkerError('vector_inference_failed') from None
        else:
            self._server_uid = None

    def _request(self, op: str, **payload):
        timeout = self.peek_timeout if op == 'peek' else self.timeout
        deadline = time.monotonic() + timeout
        shared = _ENCODER_DEADLINE.get()
        if op != 'peek' and shared is not None:
            deadline = min(deadline, shared)
        if deadline <= time.monotonic():
            raise WorkerError('vector_inference_failed')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(max(0.001,deadline-time.monotonic()))
                connection.connect(self.socket_path)
                if self._server_uid is not None:
                    _require_worker_peer(connection, self._server_uid)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('worker_deadline_expired')
                connection.settimeout(remaining)
                send(connection, {'profile':self.profile,'op':op,
                                  'deadline_monotonic':deadline,**payload})
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('worker_deadline_expired')
                connection.settimeout(remaining)
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
