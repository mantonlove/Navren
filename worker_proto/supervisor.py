"""Private Unix-socket supervisor; at most one encoder child exists at a time.

Prototype only. It does not open a business database or hold API credentials.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys

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

    def _stop(self) -> None:
        process, conn = self.process, self.conn
        if process is None:
            self.conn = self.profile = self.metadata = None
            return
        if process.is_alive() and conn is not None:
            try:conn.send({'op':'close'})
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
            raise RuntimeError('old_worker_not_reaped')
        if conn is not None:
            conn.close()
        self.process = self.conn = self.profile = self.metadata = None

    def close(self) -> None:
        self._stop()

    def _ensure(self, profile: str) -> dict:
        if self.blocked:
            raise RuntimeError('old_worker_not_reaped')
        if self.process is not None and self.profile == profile and self.process.is_alive():
            return self.metadata
        self._stop()  # Reap old profile before allocating any new ONNX session.
        parent, child = self.context.Pipe(duplex=True)
        process = self.context.Process(target=child_main, args=(child, profile, self.backend), daemon=True)
        try:
            process.start()
        finally:
            child.close()
        self.process, self.conn, self.profile = process, parent, profile
        if not parent.poll(self.load_timeout):
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
        return self.metadata

    def handle(self, request: dict) -> dict:
        try:
            profile, op, payload = validate_request(request)
            if op == 'peek':
                spec = profile_spec(profile,self.backend)
                return {'ok':True,'metadata':spec,
                        'runtime_verified':bool(self.process is not None
                            and self.process.is_alive() and self.profile == profile
                            and self.metadata == spec)}
            metadata = self._ensure(profile)
            if op == 'describe':
                return {'ok':True,'metadata':metadata}
            self.conn.send({'op':op,'input':payload})
            if not self.conn.poll(self.request_timeout):
                self._stop()
                return {'ok':False,'code':'vector_inference_failed','worker_state':'timeout'}
            try:
                result = self.conn.recv()
            except (EOFError, OSError):
                self._stop()
                return {'ok':False,'code':'vector_inference_failed','worker_state':'crashed'}
            if result.get('ok') is not True:
                return {'ok':False,'code':result.get('code','vector_inference_failed')}
            return {'ok':True,'metadata':metadata,'result':result['result']}
        except ProtocolError:
            return {'ok':False,'code':'request_shape_invalid'}
        except (OSError, RuntimeError) as error:
            code = str(error)
            return {'ok':False,'code':'vector_inference_failed',
                    'worker_state':code if code in {'worker_load_timeout','worker_crashed','worker_load_failed',
                                                   'old_worker_not_reaped'} else 'unavailable'}
        except Exception:
            return {'ok':False,'code':'vector_inference_failed','worker_state':'unavailable'}


def _peer_allowed(sock: socket.socket) -> bool:
    if sys.platform.startswith('linux'):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        _, uid, _ = struct.unpack('3i', raw)
        return uid == os.geteuid()
    if hasattr(sock, 'getpeereid'):
        uid, _ = sock.getpeereid()
        return uid == os.geteuid()
    # macOS test runtime lacks SO_PEERCRED; directory 0700 and socket 0600
    # still restrict the local fixture. Production Linux requires peercred.
    return not sys.platform.startswith('linux')


def serve(path: Path, manager: SingleProfileManager) -> None:
    directory = path.parent
    info = directory.stat()
    if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise RuntimeError('unsafe_socket_directory')
    if path.exists() or path.is_symlink():
        raise RuntimeError('socket_already_exists')
    os.umask(0o077)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    owned_inode = path.stat().st_ino
    listener.listen(4)
    listener.settimeout(1.0)
    try:
        while not stopping:
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            with client:
                if not _peer_allowed(client):
                    continue
                client.settimeout(manager.load_timeout + manager.request_timeout + 5)
                try:
                    request = recv(client)
                    send(client, manager.handle(request))
                except (ProtocolError, OSError, TimeoutError):
                    try:send(client, {'ok':False,'code':'request_shape_invalid'})
                    except (OSError, ProtocolError):pass
    finally:
        listener.close()
        try:
            manager.close()
        finally:
            if path.exists() and path.stat().st_ino == owned_inode:
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--backend', choices=('simulate','real'), default='simulate')
    parser.add_argument('--load-timeout', type=float, default=60.0)
    parser.add_argument('--request-timeout', type=float, default=120.0)
    args = parser.parse_args()
    if args.load_timeout <= 0 or args.request_timeout <= 0:
        parser.error('timeouts must be positive')
    serve(args.socket, SingleProfileManager(args.backend,
          load_timeout=args.load_timeout,request_timeout=args.request_timeout))


if __name__ == '__main__':
    main()
