"""Linux Unix-peer checks with no live socket, model, or user data."""
from __future__ import annotations

import os
import socket
import struct
import unittest
from unittest.mock import patch

from worker_proto import client


class FakeConnection:
    def __init__(self, uid: int):
        self.uid = uid
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        pass

    def settimeout(self, _seconds):
        pass

    def connect(self, _path):
        pass

    def getsockopt(self, _level, _option, _length):
        return struct.pack('3i', 123, self.uid, 456)

    def sendall(self, _data):
        self.sent = True
        raise AssertionError('request sent before peer verification')


class WorkerPeerIdentityTests(unittest.TestCase):
    def test_mismatched_peer_rejects_before_query_text_is_sent(self):
        fake = FakeConnection(os.geteuid()+1)
        with patch.dict(os.environ, {'NAVREN_ENCODER_BACKEND':'simulate'}), \
             patch.object(client.sys, 'platform', 'linux'), \
             patch.object(socket, 'SO_PEERCRED', 17, create=True), \
             patch.object(client.socket, 'socket', return_value=fake):
            proxy = client.EncoderProxy('/unused/worker.sock', 'e5', timeout=1)
            with self.assertRaises(client.WorkerError) as caught:
                proxy.encode_query('fixed public test text')
        self.assertEqual(caught.exception.code, 'vector_inference_failed')
        self.assertFalse(fake.sent)

    def test_matching_peer_and_missing_peer_credentials(self):
        with patch.object(client.sys, 'platform', 'linux'), \
             patch.object(socket, 'SO_PEERCRED', 17, create=True):
            client._require_worker_peer(FakeConnection(491), 491)
            with self.assertRaises(client.WorkerError):
                client._require_worker_peer(FakeConnection(492), 491)
            class Unreadable:
                def getsockopt(self, *_args):
                    raise PermissionError
            with self.assertRaises(client.WorkerError):
                client._require_worker_peer(Unreadable(), 491)

    def test_real_backend_requires_dedicated_account(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(client.sys, 'platform', 'linux'), \
             patch.object(client, 'profile_spec', return_value={'profile_id':'public-fixture'}), \
             patch.object(client.pwd, 'getpwnam', side_effect=KeyError):
            with self.assertRaises(client.WorkerError):
                client.EncoderProxy('/unused/worker.sock', 'e5')


if __name__ == '__main__':
    unittest.main()
