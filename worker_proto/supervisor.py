"""Private Unix-socket supervisor; at most one encoder child exists at a time.

Prototype only. It does not open a business database or hold API credentials.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path
import pwd
import signal
import socket
import stat
import struct
import sys
import threading
import time

from .child import child_main
from .profile_spec import profile_spec
from .protocol import ProtocolError, recv, send, validate_request


class SingleProfileManager:
    def __init__(self, backend: str, *, load_timeout: float, request_timeout: float):
        self.backend = backend
        self.load_timeout = load_timeout
        self.request_timeout = request_timeout
        self.context = multiprocessing.get_context('spawn')
        self.profile = None
        self.process = None
        self.conn = None
        self.metadata = None
        self.blocked = False
        self._operation_lock = threading.Lock()
        # One immutable reference lets peek report status without waiting for
        # a cold model load or inference holding the operation lock.
        self._verified_snapshot = None
        # A clean profile switch frees its child but does not undo a successful
        # load in this supervisor lifetime. Keep copy-on-write snapshots so
        # status peeks never wait for the model-operation lock.
        self._validated_profiles: dict[str, dict] = {}

    def _remember(self, profile: str, metadata: dict) -> None:
        updated = dict(self._validated_profiles)
        updated[profile] = metadata
        self._validated_profiles = updated

    def _forget(self, profile: str | None) -> None:
        if profile is not None and profile in self._validated_profiles:
            updated = dict(self._validated_profiles)
            updated.pop(profile, None)
            self._validated_profiles = updated

    def _stop(self) -> None:
        self._verified_snapshot = None
        process, conn = self.process, self.conn
        old_profile = self.profile
        if process is None:
            self.conn = self.profile = self.metadata = None
            return
        close_sent = False
        if process.is_alive() and conn is not None:
            try:
                conn.send({'op':'close'})
                close_sent = True
            except (BrokenPipeError, EOFError, OSError):pass
        process.join(timeout=1.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=2.0)
        if process.is_alive():
            # Do not release the reference or start another model if the old
            # child cannot be proven dead. This manager must fail closed.
            self.blocked = True
            self._validated_profiles = {}
            raise RuntimeError('old_worker_not_reaped')
        if not (close_sent and getattr(process, 'exitcode', None) == 0):
            self._forget(old_profile)
        if conn is not None:
            conn.close()
        self.process = self.conn = self.profile = self.metadata = None

    def close(self) -> None:
        with self._operation_lock:
            self._stop()

    @staticmethod
    def _remaining(deadline: float | None, maximum: float) -> float:
        if deadline is None:
            return maximum
        return max(0.0, min(maximum, deadline - time.monotonic()))

    def _ensure(self, profile: str, deadline: float | None = None) -> dict:
        if self.blocked:
            raise RuntimeError('old_worker_not_reaped')
        if self.process is not None and self.profile == profile and self.process.is_alive():
            return self.metadata
        self._stop()  # Reap old profile before allocating any new ONNX session.
        if deadline is not None and time.monotonic() >= deadline:
            raise RuntimeError('worker_deadline_expired')
        self._forget(profile)  # A failed reload revokes any earlier validation.
        parent, child = self.context.Pipe(duplex=True)
        process = self.context.Process(target=child_main, args=(child, profile, self.backend), daemon=True)
        try:
            process.start()
        finally:
            child.close()
        self.process, self.conn, self.profile = process, parent, profile
        load_wait = self._remaining(deadline, self.load_timeout)
        if load_wait <= 0 or not parent.poll(load_wait):
            self._stop()
            raise RuntimeError('worker_load_timeout')
        try:
            ready = parent.recv()
        except (EOFError, OSError):
            self._stop()
            raise RuntimeError('worker_crashed') from None
        if ready.get('ready') is not True:
            self._stop()
            raise RuntimeError(ready.get('code', 'worker_load_failed'))
        self.metadata = {key:ready[key] for key in ('profile_id','dimension','max_windows','model_name')}
        if self.metadata != profile_spec(profile, self.backend):
            self._stop()
            raise RuntimeError('worker_metadata_mismatch')
        self._verified_snapshot = (profile, self.metadata, process)
        self._remember(profile, self.metadata)
        return self.metadata

    def _peek(self, profile: str) -> dict:
        spec = profile_spec(profile, self.backend)
        snapshot = self._verified_snapshot
        verified = not self.blocked and self._validated_profiles.get(profile) == spec
        if snapshot is not None and snapshot[0] == profile and not snapshot[2].is_alive():
            self._forget(profile)
            verified = False
        return {'ok':True,'metadata':spec,'runtime_verified':verified}

    def _handle_work(self, profile: str, op: str, payload: object,
                     deadline: float | None) -> dict:
        if deadline is not None and time.monotonic() >= deadline:
            return {'ok':False,'code':'vector_inference_failed','worker_state':'deadline_expired'}
        metadata = self._ensure(profile, deadline)
        if op == 'describe':
            return {'ok':True,'metadata':metadata}
        if deadline is not None and time.monotonic() >= deadline:
            return {'ok':False,'code':'vector_inference_failed','worker_state':'deadline_expired'}
        try:
            self.conn.send({'op':op,'input':payload})
        except (BrokenPipeError, EOFError, OSError):
            self._forget(profile)
            self._stop()
            return {'ok':False,'code':'vector_inference_failed','worker_state':'crashed'}
        request_wait = self._remaining(deadline, self.request_timeout)
        if request_wait <= 0 or not self.conn.poll(request_wait):
            self._forget(profile)
            self._stop()
            return {'ok':False,'code':'vector_inference_failed','worker_state':'timeout'}
        try:
            result = self.conn.recv()
        except (EOFError, OSError):
            self._forget(profile)
            self._stop()
            return {'ok':False,'code':'vector_inference_failed','worker_state':'crashed'}
        if result.get('ok') is not True:
            if result.get('code') not in {'vector_input_invalid','vector_query_too_long',
                                          'vector_text_too_long'}:
                self._forget(profile)
            return {'ok':False,'code':result.get('code','vector_inference_failed')}
        self._remember(profile, metadata)
        return {'ok':True,'metadata':metadata,'result':result['result']}

    def handle(self, request: dict) -> dict:
        try:
            profile, op, payload = validate_request(request)
            deadline = request.get('deadline_monotonic')
            if op == 'peek':
                return self._peek(profile)
            if deadline is None:
                deadline = time.monotonic()+40.0
            if deadline is not None and time.monotonic() >= deadline:
                return {'ok':False,'code':'vector_inference_failed','worker_state':'deadline_expired'}
            if not self._operation_lock.acquire(blocking=False):
                return {'ok':False,'code':'vector_inference_failed','worker_state':'busy'}
            try:
                return self._handle_work(profile, op, payload, deadline)
            finally:
                self._operation_lock.release()
        except ProtocolError:
            return {'ok':False,'code':'request_shape_invalid'}
        except (OSError, RuntimeError) as error:
            code = str(error)
            return {'ok':False,'code':'vector_inference_failed',
                    'worker_state':code if code in {'worker_load_timeout','worker_crashed','worker_load_failed',
                                                   'old_worker_not_reaped','worker_deadline_expired'} else 'unavailable'}
        except Exception:
            return {'ok':False,'code':'vector_inference_failed','worker_state':'unavailable'}


def _peer_allowed(sock: socket.socket, allowed_uid: int) -> bool:
    if sys.platform.startswith('linux'):
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            _, uid, _ = struct.unpack('3i', raw)
            return uid == allowed_uid
        except (AttributeError, OSError, struct.error):
            return False
    if hasattr(sock, 'getpeereid'):
        try:
            uid, _ = sock.getpeereid()
            return uid == allowed_uid
        except OSError:
            return False
    # Local fixture fallback. The production Linux path always requires
    # SO_PEERCRED and rejects a missing or unreadable peer identity.
    return not sys.platform.startswith('linux') and allowed_uid == os.geteuid()


def serve(path: Path, manager: SingleProfileManager, *, allowed_uid: int) -> None:
    directory = path.parent
    info = directory.stat()
    if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) not in (0o700, 0o710)):
        raise RuntimeError('unsafe_socket_directory')
    if path.exists() or path.is_symlink():
        raise RuntimeError('socket_already_exists')
    os.umask(0o077)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stopping = False
    workers: set[threading.Thread] = set()
    slots = threading.BoundedSemaphore(8)

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    listener.bind(str(path))
    os.chmod(path, 0o660)
    if path.stat().st_gid != os.getegid():
        listener.close()
        path.unlink()
        raise RuntimeError('unsafe_socket_group')
    owned_inode = path.stat().st_ino
    listener.listen(4)
    listener.settimeout(1.0)

    def handle_client(client: socket.socket) -> None:
        try:
            with client:
                client.settimeout(1.0)
                try:
                    request = recv(client)
                    response = (manager.handle(request) if not stopping else
                                {'ok':False,'code':'vector_inference_failed'})
                except (ProtocolError, OSError, TimeoutError):
                    response = {'ok':False,'code':'request_shape_invalid'}
                try:send(client, response)
                except (ProtocolError, OSError, TimeoutError):pass
        finally:
            slots.release()

    try:
        while not stopping:
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            if not _peer_allowed(client, allowed_uid):
                client.close()
                continue
            if not slots.acquire(blocking=False):
                try:send(client, {'ok':False,'code':'vector_inference_failed'})
                except (OSError, ProtocolError, TimeoutError):pass
                client.close()
                continue
            worker = threading.Thread(target=handle_client,args=(client,),daemon=True)
            workers.add(worker)
            worker.start()
            workers = {thread for thread in workers if thread.is_alive()}
    finally:
        listener.close()
        for worker in workers:
            worker.join(timeout=2.0)
        try:
            manager.close()
        finally:
            if path.exists() and path.stat().st_ino == owned_inode:
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--backend', choices=('simulate','real'), default='simulate')
    parser.add_argument('--client-user', required=True)
    parser.add_argument('--load-timeout', type=float, default=60.0)
    parser.add_argument('--request-timeout', type=float, default=120.0)
    args = parser.parse_args()
    if args.load_timeout <= 0 or args.request_timeout <= 0:
        parser.error('timeouts must be positive')
    try:
        allowed_uid = pwd.getpwnam(args.client_user).pw_uid
    except KeyError:
        parser.error('client user does not exist')
    if args.backend == 'real' and allowed_uid == os.geteuid():
        parser.error('real worker must run under a separate uid')
    serve(args.socket, SingleProfileManager(args.backend,
          load_timeout=args.load_timeout,request_timeout=args.request_timeout),
          allowed_uid=allowed_uid)


if __name__ == '__main__':
    main()
