"""Public deterministic fixtures only; never loads a real model or database."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from worker_proto.client import EncoderProxy, WorkerError
from worker_proto.measure_public_fixture import measure
from worker_proto.protocol import MAX_FRAME_BYTES, recv, send
from worker_proto.supervisor import SingleProfileManager


class ManagerTests(unittest.TestCase):
    def test_profile_switch_reaps_old_child_before_next_model(self):
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=5)
        try:
            self.assertFalse(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
            first = manager.handle({'profile':'e5','op':'describe'})
            self.assertTrue(first['ok'])
            self.assertTrue(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
            previous = manager.process
            second = manager.handle({'profile':'aligned','op':'describe'})
            self.assertTrue(second['ok'])
            self.assertFalse(previous.is_alive())
            self.assertNotEqual(previous.pid, manager.process.pid)
            self.assertEqual(manager.profile, 'aligned')
            self.assertFalse(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
            self.assertTrue(manager.handle({'profile':'aligned','op':'peek'})['runtime_verified'])
            manager.process.kill()
            manager.process.join(timeout=2)
            self.assertFalse(manager.handle({'profile':'aligned','op':'peek'})['runtime_verified'])
        finally:
            manager.close()

    def test_malformed_request_does_not_start_model(self):
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=5)
        try:
            result = manager.handle({'profile':'e5','op':'query','text':''})
            self.assertEqual(result, {'ok':False,'code':'request_shape_invalid'})
            self.assertIsNone(manager.process)
        finally:
            manager.close()

    def test_unreaped_old_child_blocks_new_profile(self):
        class Stubborn:
            def is_alive(self):return True
            def join(self, timeout):pass
            def terminate(self):pass
            def kill(self):pass
        class Pipe:
            def send(self, value):pass
            def close(self):pass
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=5)
        old = Stubborn()
        manager.process, manager.conn, manager.profile = old, Pipe(), 'e5'
        result = manager.handle({'profile':'aligned','op':'describe'})
        self.assertEqual(result.get('worker_state'), 'old_worker_not_reaped')
        self.assertIs(manager.process, old)
        self.assertTrue(manager.blocked)
        again = manager.handle({'profile':'aligned','op':'describe'})
        self.assertEqual(again.get('worker_state'), 'old_worker_not_reaped')


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.previous_backend = os.environ.get('NAVREN_ENCODER_BACKEND')
        os.environ['NAVREN_ENCODER_BACKEND'] = 'simulate'
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / 'worker.sock'
        self.process = subprocess.Popen(
            [sys.executable,'-B','-m','worker_proto.supervisor','--socket',str(self.socket_path),
             '--backend','simulate','--load-timeout','5','--request-timeout','0.2'],
            cwd=str(Path(__file__).parent), env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 5
        while not self.socket_path.exists() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            time.sleep(0.02)
        if not self.socket_path.exists():
            raise AssertionError('supervisor_did_not_start:' + (self.process.stderr.read() or '')[:300])

    def tearDown(self):
        self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stderr.close()
        self.temp.cleanup()
        if self.previous_backend is None:os.environ.pop('NAVREN_ENCODER_BACKEND',None)
        else:os.environ['NAVREN_ENCODER_BACKEND']=self.previous_backend

    def test_private_socket_and_dual_query_shapes(self):
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        e5 = EncoderProxy(self.socket_path, 'e5', timeout=5)
        aligned = EncoderProxy(self.socket_path, 'aligned', timeout=5)
        self.assertEqual(e5.dimension, 384)
        first = e5.encode_query('公开测试句子')
        second = aligned.encode_query('public test sentence')
        self.assertEqual(first.shape, (384,))
        self.assertEqual(second.shape, (384,))
        self.assertAlmostEqual(float(first.sum()), 1.0)
        self.assertEqual(len(e5.encode_documents(['公开材料一','公开材料二'])), 2)
        self.assertEqual(e5.encode_documents(['公开材料'])[0].shape, (1,384))

    def test_timeout_and_crash_do_not_kill_supervisor(self):
        proxy = EncoderProxy(self.socket_path, 'e5', timeout=5)
        with self.assertRaises(WorkerError) as timed:
            proxy.encode_query('__slow__')
        self.assertEqual(timed.exception.code, 'vector_inference_failed')
        with self.assertRaises(WorkerError) as crashed:
            proxy.encode_query('__crash__')
        self.assertEqual(crashed.exception.code, 'vector_inference_failed')
        self.assertIsNone(self.process.poll())
        self.assertEqual(proxy.encode_query('恢复后的公开查询').shape, (384,))

    def test_protocol_rejects_oversize_and_raw_text_never_in_error(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            connection.sendall(struct.pack('!I', MAX_FRAME_BYTES+1))
            response = recv(connection)
        self.assertEqual(response.get('code'), 'request_shape_invalid')
        self.assertNotIn('private', str(response))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            send(connection, {'profile':'e5','op':'query','text':'private-should-not-echo',
                              'unexpected':'x'})
            response = recv(connection)
        self.assertEqual(response.get('code'), 'request_shape_invalid')
        self.assertNotIn('private-should-not-echo', str(response))

    def test_public_fixture_measurement_records_only_metrics(self):
        cgroup = Path(self.temp.name) / 'fake-cgroup'
        cgroup.mkdir()
        (cgroup / 'memory.current').write_text('123456')
        (cgroup / 'memory.peak').write_text('234567')
        (cgroup / 'memory.events').write_text('oom_kill 0\n')
        result = measure(self.socket_path, cgroup, 'e5', 5)
        self.assertEqual(result['profile'], 'e5')
        self.assertEqual(result['cgroup_oom_kill_delta'], 0)
        self.assertNotIn('公开测试', str(result))
        self.assertFalse(result['production_memory_accepted'])


if __name__ == '__main__':
    unittest.main()
