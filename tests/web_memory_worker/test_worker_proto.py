"""Public deterministic fixtures only; never loads a real model or database."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from worker_proto.client import EncoderProxy, WorkerError, encoder_budget
from worker_proto.measure_public_fixture import measure
from worker_proto.protocol import MAX_FRAME_BYTES, recv, send
from worker_proto.supervisor import SingleProfileManager, _peer_allowed, serve


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
            self.assertTrue(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
            self.assertTrue(manager.handle({'profile':'aligned','op':'peek'})['runtime_verified'])
            manager.process.kill()
            manager.process.join(timeout=2)
            self.assertFalse(manager.handle({'profile':'aligned','op':'peek'})['runtime_verified'])
            self.assertTrue(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
        finally:
            manager.close()

    def test_failed_profile_invalidates_only_itself_and_success_restores_it(self):
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=5)
        try:
            self.assertTrue(manager.handle({'profile':'e5','op':'describe'})['ok'])
            self.assertTrue(manager.handle({'profile':'aligned','op':'describe'})['ok'])
            self.assertTrue(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
            self.assertTrue(manager.handle({'profile':'aligned','op':'peek'})['runtime_verified'])
            crashed = manager.handle({'profile':'e5','op':'query','text':'__crash__'})
            self.assertFalse(crashed['ok'])
            self.assertFalse(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
            self.assertTrue(manager.handle({'profile':'aligned','op':'peek'})['runtime_verified'])
            recovered = manager.handle({'profile':'e5','op':'query','text':'公开恢复查询'})
            self.assertTrue(recovered['ok'])
            self.assertTrue(manager.handle({'profile':'e5','op':'peek'})['runtime_verified'])
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

    def test_expired_request_never_starts_model(self):
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=5)
        try:
            result = manager.handle({'profile':'e5','op':'query','text':'stale public text',
                                     'deadline_monotonic':time.monotonic()-1})
            self.assertEqual(result.get('worker_state'), 'deadline_expired')
            self.assertIsNone(manager.process)
        finally:
            manager.close()

    def test_status_and_busy_rejection_do_not_queue_behind_inference(self):
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=0.8)
        try:
            self.assertTrue(manager.handle({'profile':'e5','op':'describe'})['ok'])
            running = threading.Thread(target=lambda:manager.handle({
                'profile':'e5','op':'query','text':'__slow__',
                'deadline_monotonic':time.monotonic()+2}),daemon=True)
            running.start()
            deadline = time.monotonic()+1
            while not manager._operation_lock.locked() and time.monotonic()<deadline:
                time.sleep(0.005)
            self.assertTrue(manager._operation_lock.locked())
            started = time.monotonic()
            peek = manager.handle({'profile':'e5','op':'peek'})
            busy = manager.handle({'profile':'e5','op':'query','text':'__crash__',
                                   'deadline_monotonic':time.monotonic()+2})
            elapsed = time.monotonic()-started
            self.assertLess(elapsed,0.4)
            self.assertTrue(peek['ok'])
            self.assertTrue(peek['runtime_verified'])
            self.assertEqual(busy.get('worker_state'),'busy')
            running.join(timeout=3)
            self.assertFalse(running.is_alive())
        finally:
            manager.close()

    def test_malformed_deadline_rejected_before_model(self):
        manager = SingleProfileManager('simulate', load_timeout=5, request_timeout=5)
        try:
            result = manager.handle({'profile':'e5','op':'query','text':'public text',
                                     'deadline_monotonic':float('nan')})
            self.assertEqual(result.get('code'),'request_shape_invalid')
            self.assertIsNone(manager.process)
        finally:
            manager.close()


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.previous_backend = os.environ.get('NAVREN_ENCODER_BACKEND')
        os.environ['NAVREN_ENCODER_BACKEND'] = 'simulate'
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / 'worker.sock'
        self.process = subprocess.Popen(
            [sys.executable,'-B','-m','worker_proto.supervisor','--socket',str(self.socket_path),
             '--backend','simulate','--client-user',pwd.getpwuid(os.geteuid()).pw_name,
             '--load-timeout','5','--request-timeout','0.8'],
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
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o660)
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

    def test_mode_0710_runtime_directory_starts_and_serves_authorized_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory,0o710)
            path=Path(directory)/'worker.sock'
            process=subprocess.Popen(
                [sys.executable,'-B','-m','worker_proto.supervisor','--socket',str(path),
                 '--backend','simulate','--client-user',pwd.getpwuid(os.geteuid()).pw_name,
                 '--load-timeout','5','--request-timeout','5'],
                cwd=str(Path(__file__).parent),
                env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+5
                while not path.exists() and time.monotonic()<deadline:
                    if process.poll() is not None:break
                    time.sleep(0.02)
                self.assertTrue(path.exists(),(process.stderr.read() if process.poll() is not None else 'startup timeout')[:300])
                self.assertEqual(EncoderProxy(path,'e5',timeout=5).encode_query('公开测试').shape,(384,))
            finally:
                process.terminate()
                process.wait(timeout=5)
                process.stderr.close()

    def test_mode_0770_runtime_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory,0o770)
            path=Path(directory)/'worker.sock'
            manager=SingleProfileManager('simulate',load_timeout=5,request_timeout=5)
            with self.assertRaisesRegex(RuntimeError,'unsafe_socket_directory'):
                serve(path,manager,allowed_uid=os.geteuid())
            self.assertFalse(path.exists())

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

    def test_socket_peek_is_fast_while_query_is_running(self):
        proxy = EncoderProxy(self.socket_path, 'e5', timeout=5, peek_timeout=0.12)
        self.assertEqual(proxy.encode_query('公开预热').shape, (384,))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.socket_path))
            send(connection, {'profile':'e5','op':'query','text':'__slow__',
                              'deadline_monotonic':time.monotonic()+2})
            time.sleep(0.04)
            started = time.monotonic()
            self.assertTrue(proxy.runtime_verified())
            self.assertLess(time.monotonic()-started,0.12)
            self.assertEqual(recv(connection).get('worker_state'),'timeout')

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

    def test_shared_search_budget_rejects_later_view_without_worker_call(self):
        proxy = EncoderProxy(self.socket_path, 'e5', timeout=5)
        with encoder_budget(0.01):
            time.sleep(0.02)
            started = time.monotonic()
            with self.assertRaises(WorkerError) as expired:
                proxy.encode_query('公开测试的第二个视图')
        self.assertEqual(expired.exception.code,'vector_inference_failed')
        self.assertLess(time.monotonic()-started,0.1)

    def test_peer_identity_is_checked_against_explicit_web_uid(self):
        class Peer:
            def __init__(self, uid):self.uid=uid
            def getsockopt(self, _level, _option, _size):
                return struct.pack('3i', 42, self.uid, os.getegid())
        web_uid=os.geteuid()
        with patch.object(sys, 'platform', 'linux'), patch.object(socket, 'SO_PEERCRED', 17, create=True):
            self.assertTrue(_peer_allowed(Peer(web_uid), web_uid))
            self.assertFalse(_peer_allowed(Peer(web_uid+1), web_uid))
            if web_uid != 0:
                self.assertFalse(_peer_allowed(Peer(0), web_uid))


if __name__ == '__main__':
    unittest.main()
