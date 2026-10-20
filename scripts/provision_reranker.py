#!/usr/bin/env python3
"""Explicitly provision the pinned public ONNX cross-encoder, without Torch.

This is the only network entry point for this model. Query-time reranking reads
verified local artifacts and never calls this script or downloads dependencies.
Only a complete, verified directory is published; partial downloads stay private.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_knowledge import reranking as _RERANKER

MODEL_REPOSITORY = _RERANKER.MODEL_REPOSITORY
MODEL_REVISION = _RERANKER.MODEL_REVISION
MODEL_ARTIFACTS = _RERANKER.MODEL_ARTIFACTS
_BASE = 'https://huggingface.co'
_USER_AGENT = 'Navren-public-reranker-provision/1'
_METADATA_LIMIT = 2 * 1024 * 1024
_BLOCK_SIZE = 256 * 1024
_ERROR_CODES = frozenset({
    'model_destination_rejected', 'model_existing_directory_invalid',
    'model_destination_changed', 'model_provision_busy',
    'model_local_artifacts_invalid', 'model_download_timeout',
    'model_redirect_rejected', 'model_download_failed',
    'model_download_size_mismatch', 'model_download_hash_mismatch',
    'model_revision_mismatch', 'model_metadata_mismatch',
    'model_provision_failed',
})


class ProvisionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code if code in _ERROR_CODES else 'model_provision_failed')


class _PublicModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or '').lower()
        # HF's public LFS/Xet storage uses these HTTPS domains. No arbitrary
        # redirect, embedded credential, nonstandard port or HTTP downgrade.
        allowed = ('huggingface.co', 'hf.co', 'xethub.hf.co')
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not any(host == suffix or host.endswith('.' + suffix)
                           for suffix in allowed)):
            raise ProvisionError('model_redirect_rejected')
        return super().redirect_request(request, response, code, message, headers, url)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _plain_artifact(directory: Path, name: str) -> Path | None:
    """Never reuse artifact bytes through a file or intermediate symlink."""
    current = directory
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink():
            return None
    return current if current.is_file() else None


def _ensure_artifact_parent(directory: Path, name: str) -> Path:
    current = directory
    for part in Path(name).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ProvisionError('model_destination_rejected')
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise ProvisionError('model_destination_rejected')
    return directory / name


def _file_matches(path: Path | None, expected: dict) -> bool:
    if path is None or path.stat().st_size != expected['size']:
        return False
    digest = hashlib.sha256()
    received = 0
    with path.open('rb') as stream:
        while data := stream.read(_BLOCK_SIZE):
            received += len(data)
            if received > expected['size']:
                return False
            digest.update(data)
    return received == expected['size'] and digest.hexdigest() == expected['sha256']


def _download(opener, url: str, deadline: float, *, path: Path | None = None,
              expected: dict | None = None) -> bytes | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProvisionError('model_download_timeout')
    # No Authorization header, token, private content or user query is sent.
    request = urllib.request.Request(url, headers={
        'User-Agent': _USER_AGENT, 'Accept-Encoding': 'identity',
    })
    limit = expected['size'] if expected else _METADATA_LIMIT
    digest = hashlib.sha256()
    received = 0
    fragments = []
    with opener.open(request, timeout=min(20, remaining)) as response:
        if response.status != 200:
            raise ProvisionError('model_download_failed')
        length = response.headers.get('Content-Length')
        if length is not None:
            try:
                announced = int(length)
            except (ValueError, TypeError):
                raise ProvisionError('model_download_size_mismatch') from None
            if announced < 0 or announced > limit or (expected and announced != limit):
                raise ProvisionError('model_download_size_mismatch')
        stream = path.open('xb') if path else None
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise ProvisionError('model_download_timeout')
                data = response.read(min(_BLOCK_SIZE, limit - received + 1))
                if not data:
                    break
                received += len(data)
                if received > limit:
                    raise ProvisionError('model_download_size_mismatch')
                digest.update(data)
                if stream:
                    stream.write(data)
                else:
                    fragments.append(data)
            if expected and (received != expected['size']
                             or digest.hexdigest() != expected['sha256']):
                raise ProvisionError('model_download_hash_mismatch')
            if stream:
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if stream:
                stream.close()
    return b''.join(fragments) if path is None else None


def _worker(stage: Path, timeout_seconds: float) -> dict:
    if stage.is_symlink() or not stage.is_dir():
        raise ProvisionError('model_destination_rejected')
    deadline = time.monotonic() + timeout_seconds
    opener = urllib.request.build_opener(_PublicModelRedirect())
    metadata_bytes = _download(
        opener, f'{_BASE}/api/models/{MODEL_REPOSITORY}/revision/{MODEL_REVISION}?blobs=true',
        deadline,
    )
    metadata = json.loads(metadata_bytes)
    if not isinstance(metadata, dict) or metadata.get('sha') != MODEL_REVISION:
        raise ProvisionError('model_revision_mismatch')
    if metadata.get('cardData', {}).get('license') != 'apache-2.0':
        raise ProvisionError('model_metadata_mismatch')
    siblings = {item.get('rfilename'): item for item in metadata.get('siblings', [])
                if isinstance(item, dict)}
    downloads = 0
    for name, expected in MODEL_ARTIFACTS.items():
        item = siblings.get(name, {})
        if item.get('size') != expected['size']:
            raise ProvisionError('model_metadata_mismatch')
        lfs = item.get('lfs')
        if (lfs is not None and (not isinstance(lfs, dict)
                or lfs.get('sha256') != expected['sha256']
                or lfs.get('size') != expected['size'])):
            raise ProvisionError('model_metadata_mismatch')
        if (name.endswith('.onnx') or name == 'tokenizer.json') and lfs is None:
            raise ProvisionError('model_metadata_mismatch')
        path = _ensure_artifact_parent(stage, name)
        if path.exists() or path.is_symlink():
            if not _file_matches(_plain_artifact(stage, name), expected):
                raise ProvisionError('model_download_hash_mismatch')
            continue
        _download(opener, f'{_BASE}/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}',
                  deadline, path=path, expected=expected)
        downloads += 1
    _fsync_directory(stage / 'onnx')
    _fsync_directory(stage)
    return {'artifact_downloads': downloads, 'metadata_requests': 1}


def _manifest() -> dict:
    return {
        'format_version': 1, 'complete': True,
        'repository': MODEL_REPOSITORY, 'revision': MODEL_REVISION,
        'algorithm_policy': _RERANKER.ALGORITHM_POLICY,
        'profile_id': _RERANKER.rerank_profile_metadata()['profile_id'],
        'artifacts': {
            name: {**values, 'source_url':
                   f'{_BASE}/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}'}
            for name, values in MODEL_ARTIFACTS.items()
        },
        'license': {
            'upstream_declared_license': 'apache-2.0',
            'model_card': f'{_BASE}/{MODEL_REPOSITORY}/blob/{MODEL_REVISION}/README.md',
            'model_card_sha256': MODEL_ARTIFACTS['README.md']['sha256'],
        },
        'provisioned_at': datetime.now(timezone.utc).isoformat(),
        'inference': {
            'provider': 'CPUExecutionProvider', 'model_format': 'ONNX',
            'quantization': 'qint8_arm64', 'query_network_requests': 0,
            'generates_text': False, 'authority': False,
        },
    }


def _verified(directory: Path) -> bool:
    try:
        _RERANKER._load_verified_artifacts(directory)
        return True
    except _RERANKER.RerankError:
        return False


def _identity(directory: Path) -> tuple | None:
    """Detect another publisher or edited fixed assets before replacing a dir."""
    if not directory.exists():
        return None
    entries = []
    for name in ('', 'manifest.json', *MODEL_ARTIFACTS):
        path = directory / name if name else directory
        try:
            stat = path.lstat()
            entries.append((name, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            entries.append((name, None))
    return tuple(entries)


def _result(status: str, *, local_only: bool, requests: dict | None = None) -> dict:
    return {
        'status': status, 'revision': MODEL_REVISION,
        'artifacts': len(MODEL_ARTIFACTS),
        'bytes': sum(item['size'] for item in MODEL_ARTIFACTS.values()),
        'algorithm_policy': _RERANKER.ALGORITHM_POLICY,
        'profile_id': _RERANKER.rerank_profile_metadata()['profile_id'],
        'local_only': local_only,
        'explicit_network_requests': sum((requests or {}).values()),
        'redirect_requests_included': False,
    }


def provision(destination: Path, timeout_seconds: float, replace: bool = False,
              local_only: bool = False) -> dict:
    """Bound downloads in a child; parent verifies, cleans up and publishes."""
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 10 <= timeout_seconds <= 600):
        raise ProvisionError('model_provision_failed')
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ProvisionError('model_destination_rejected')
    if destination.exists():
        if not destination.is_dir():
            raise ProvisionError('model_destination_rejected')
        if _verified(destination):
            return _result('already_verified', local_only=True)
        if not replace:
            raise ProvisionError('model_existing_directory_invalid')
    initial_identity = _identity(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = destination.parent / ('.' + destination.name + '.provision-lock')
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise ProvisionError('model_provision_busy') from None
    stage = None
    backup = None
    try:
        if _identity(destination) != initial_identity:
            raise ProvisionError('model_destination_changed')
        stage = Path(tempfile.mkdtemp(prefix='.navren-reranker-stage-', dir=destination.parent))
        os.chmod(stage, 0o700)
        # Reuse only exact fixed public model bytes. Never copy user data.
        if destination.is_dir():
            for name, expected in MODEL_ARTIFACTS.items():
                existing = _plain_artifact(destination, name)
                if _file_matches(existing, expected):
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(existing, target)
                    with target.open('rb') as stream:
                        os.fsync(stream.fileno())
        if local_only and any(not _file_matches(_plain_artifact(stage, name), expected)
                              for name, expected in MODEL_ARTIFACTS.items()):
            raise ProvisionError('model_local_artifacts_invalid')
        requests = None
        if not local_only:
            command = [sys.executable, str(Path(__file__).resolve()),
                       '--worker-stage', str(stage), '--timeout-seconds', str(timeout_seconds)]
            try:
                completed = subprocess.run(command, capture_output=True, text=True,
                                           timeout=timeout_seconds, check=False)
            except subprocess.TimeoutExpired:
                raise ProvisionError('model_download_timeout') from None
            if completed.returncode:
                code = completed.stdout.strip()
                raise ProvisionError(code if code in _ERROR_CODES else 'model_download_failed')
            requests = json.loads(completed.stdout)
            if (not isinstance(requests, dict)
                    or set(requests) != {'artifact_downloads', 'metadata_requests'}
                    or any(type(value) is not int or not 0 <= value <= len(MODEL_ARTIFACTS)
                           for value in requests.values())
                    or requests['metadata_requests'] != 1):
                raise ProvisionError('model_download_failed')
        with (stage / 'manifest.json').open('x', encoding='utf-8') as stream:
            json.dump(_manifest(), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Same validator as query runtime: source revision, profile, all hashes
        # and model/tokenizer architecture must match before publication.
        _RERANKER._load_verified_artifacts(stage)
        _fsync_directory(stage / 'onnx')
        _fsync_directory(stage)
        if _identity(destination) != initial_identity:
            raise ProvisionError('model_destination_changed')
        if destination.exists():
            if destination.is_symlink() or not replace:
                raise ProvisionError('model_destination_changed')
            # Another actor may have repaired the same directory in place.
            # A verified directory is preserved even with --replace.
            if _verified(destination):
                return _result('already_verified', local_only=True)
            backup = destination.parent / (stage.name + '-previous')
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
            _fsync_directory(destination.parent)
        except Exception:
            if backup and backup.exists() and not destination.exists():
                os.replace(backup, destination)
                _fsync_directory(destination.parent)
            raise
        if backup and backup.exists():
            shutil.rmtree(backup)
        return _result('provisioned', local_only=local_only, requests=requests)
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        lock.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description='显式准备固定版本本地 ONNX 重排模型；查询阶段不联网。')
    parser.add_argument('--destination', type=Path, default=_RERANKER.default_model_directory())
    parser.add_argument('--timeout-seconds', type=float, default=540)
    parser.add_argument('--replace', action='store_true', help='显式替换校验失败的已有模型目录')
    parser.add_argument('--verify-only', action='store_true', help='只校验已有目录，不联网、不改文件')
    parser.add_argument('--refresh-manifest-local', action='store_true',
                        help='仅用本地固定资产重建清单；缺失或损坏即失败，不联网')
    parser.add_argument('--worker-stage', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or not 10 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds 必须在 10 到 600 秒之间')
    if args.verify_only and (args.worker_stage or args.refresh_manifest_local or args.replace):
        parser.error('--verify-only 不能与下载或替换选项同时使用')
    try:
        if args.worker_stage:
            print(json.dumps(_worker(args.worker_stage, args.timeout_seconds), sort_keys=True))
        elif args.verify_only:
            directory = args.destination.expanduser()
            if directory.is_symlink() or not _verified(directory):
                raise ProvisionError('model_local_artifacts_invalid')
            print(json.dumps(_result('verified', local_only=True), ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(provision(args.destination, args.timeout_seconds,
                                       args.replace or args.refresh_manifest_local,
                                       local_only=args.refresh_manifest_local),
                             ensure_ascii=False, sort_keys=True))
        return 0
    except ProvisionError as error:
        print(str(error) if args.worker_stage else
              json.dumps({'status': 'failed', 'code': str(error)}, sort_keys=True))
        return 1
    except _RERANKER.RerankError:
        print('model_local_artifacts_invalid' if args.worker_stage else
              json.dumps({'status': 'failed', 'code': 'model_local_artifacts_invalid'}))
        return 1
    except Exception:
        print('model_download_failed' if args.worker_stage else
              json.dumps({'status': 'failed', 'code': 'model_provision_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
