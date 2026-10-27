"""Bounded one-request JSON frames over a private Unix socket."""
from __future__ import annotations

import json
import math
import socket
import struct
import time

MAX_FRAME_BYTES = 1_048_576
MAX_TEXT_CHARACTERS = 65_536
MAX_DOCUMENTS = 4
PROFILES = frozenset({'e5', 'aligned'})
MAX_DEADLINE_AHEAD_SECONDS = 180.0


class ProtocolError(Exception):
    pass


def _exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        part = sock.recv(count)
        if not part:
            raise ProtocolError('truncated_frame')
        chunks.append(part)
        count -= len(part)
    return b''.join(chunks)


def recv(sock: socket.socket) -> dict:
    length = struct.unpack('!I', _exact(sock, 4))[0]
    if not 2 <= length <= MAX_FRAME_BYTES:
        raise ProtocolError('frame_size_invalid')
    try:
        value = json.loads(_exact(sock, length))
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError('frame_json_invalid') from None
    if type(value) is not dict:
        raise ProtocolError('frame_shape_invalid')
    return value


def send(sock: socket.socket, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if not 2 <= len(data) <= MAX_FRAME_BYTES:
        raise ProtocolError('frame_size_invalid')
    sock.sendall(struct.pack('!I', len(data)) + data)


def validate_request(value: dict) -> tuple[str, str, object]:
    if set(value) - {'profile', 'op', 'text', 'texts', 'deadline_monotonic'}:
        raise ProtocolError('request_shape_invalid')
    if 'deadline_monotonic' in value:
        deadline = value['deadline_monotonic']
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= 0
                or deadline > time.monotonic() + MAX_DEADLINE_AHEAD_SECONDS):
            raise ProtocolError('request_shape_invalid')
    profile, op = value.get('profile'), value.get('op')
    if profile not in PROFILES or op not in {'peek', 'describe', 'query', 'documents'}:
        raise ProtocolError('request_shape_invalid')
    if op in ('peek', 'describe'):
        if 'text' in value or 'texts' in value:
            raise ProtocolError('request_shape_invalid')
        return profile, op, None
    if op == 'query':
        text = value.get('text')
        if type(text) is not str or not text.strip() or len(text) > MAX_TEXT_CHARACTERS or 'texts' in value:
            raise ProtocolError('request_shape_invalid')
        return profile, op, text
    texts = value.get('texts')
    if (type(texts) is not list or not 1 <= len(texts) <= MAX_DOCUMENTS
            or any(type(text) is not str or not text.strip() or len(text) > MAX_TEXT_CHARACTERS
                   for text in texts) or 'text' in value):
        raise ProtocolError('request_shape_invalid')
    return profile, op, texts
