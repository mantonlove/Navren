"""Opt-in Linux disk snapshots for verified CPU ONNX inputs.

The default keeps the existing bytes input. Snapshot mode preserves the exact
verified model and its descriptor for the entire encoder/session lifetime.
No model download, inference, profile or retrieval policy lives in this module.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .embeddings import VectorError


def _require_disk_mount(directory):
    """Reject RAM-backed or unidentifiable mounts; do not guess an ABI struct."""
    resolved = directory.resolve(strict=True)
    best = None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, after = line.split(' - ', 1)
        fields = before.split()
        mountpoint = re.sub(r'\\([0-7]{3})',
                            lambda match: chr(int(match.group(1), 8)), fields[4])
        mount = Path(mountpoint)
        if resolved == mount or mount in resolved.parents:
            candidate = (len(mount.parts), after.split()[0])
            if best is None or candidate[0] >= best[0]:
                best = candidate
    if best is None or best[1] in {'tmpfs', 'ramfs', 'devtmpfs', 'hugetlbfs'}:
        raise VectorError('vector_model_invalid')


def create_cpu_session(ort, artifacts, model_key, expected, options):
    disable_arena = os.environ.get("NAVREN_ONNX_DISABLE_CPU_ARENA", "0")
    if disable_arena not in {"0", "1"}:
        raise VectorError("vector_model_invalid")
    if disable_arena == "1":
        options.enable_cpu_mem_arena = False
    mode = os.environ.get('NAVREN_ONNX_MODEL_INPUT', 'bytes').strip()
    if mode == 'bytes':
        return ort.InferenceSession(
            artifacts[model_key], sess_options=options,
            providers=['CPUExecutionProvider'],
        ), None
    if mode != 'disk-snapshot' or not sys.platform.startswith('linux'):
        raise VectorError('vector_model_invalid')
    configured = os.environ.get('NAVREN_ONNX_SNAPSHOT_DIR', '').strip()
    if not configured or not Path(configured).is_absolute():
        raise VectorError('vector_model_invalid')
    directory = Path(configured)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not Path('/proc/self/fd').is_dir()):
        raise VectorError('vector_model_invalid')
    _require_disk_mount(directory)

    # The explicit directory passed the RAM-filesystem guard. This ordinary
    # TemporaryFile is unlinked; no memfd or default /tmp fallback is used.
    # Keep the descriptor alive so ORT's public path-based session recreation
    # remains possible; process exit also releases the unlinked file's blocks.
    snapshot = tempfile.TemporaryFile(mode='w+b', prefix='navren-onnx-', dir=directory)
    try:
        model = artifacts.pop(model_key)
        if type(model) is not bytes or len(model) != expected['size']:
            raise VectorError('vector_model_invalid')
        if snapshot.write(model) != expected['size']:
            raise VectorError('vector_model_invalid')
        snapshot.flush()
        if os.fstat(snapshot.fileno()).st_size != expected['size']:
            raise VectorError('vector_model_invalid')
        snapshot.seek(0)
        if hashlib.file_digest(snapshot, 'sha256').hexdigest() != expected['sha256']:
            raise VectorError('vector_model_invalid')
        snapshot.seek(0)
        os.fchmod(snapshot.fileno(), 0o400)
        # No original serialized-model reference remains in artifacts or this
        # helper before ORT constructs its own graph/weights from the file.
        del model
        path = '/proc/self/fd/' + str(snapshot.fileno())
        session = ort.InferenceSession(
            path, sess_options=options, providers=['CPUExecutionProvider'],
        )
        return session, snapshot
    except BaseException:
        snapshot.close()
        raise
