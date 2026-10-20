#!/usr/bin/env python3
"""The explicit network entry point for the pinned multilingual retrieval model.

Run explicitly once, before enabling semantic indexing. The query runtime never
downloads a model. A complete directory is published only after all hashes match.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
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
from local_knowledge import multilingual_embeddings as _EMBEDDINGS
MODEL_REPOSITORY = _EMBEDDINGS.MODEL_REPOSITORY
MODEL_REVISION = _EMBEDDINGS.MODEL_REVISION
MODEL_ARTIFACTS = _EMBEDDINGS.MODEL_ARTIFACTS
_BASE = 'https://huggingface.co'
_USER_AGENT = 'Navren-multilingual-model-provision/1'


class ProvisionError(RuntimeError):
    pass


class _PublicModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or not any(host == suffix or host.endswith('.' + suffix)
                           for suffix in ('huggingface.co', 'hf.co', 'xethub.hf.co'))):
            raise ProvisionError('model_redirect_rejected')
        return super().redirect_request(request, response, code, message, headers, url)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _download(opener, url: str, deadline: float, *, path: Path | None = None,
              expected: dict | None = None) -> bytes | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProvisionError('model_download_timeout')
    request = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    limit = expected['size'] if expected else 2 * 1024 * 1024
    digest = hashlib.sha256()
    received = 0
    fragments = []
    with opener.open(request, timeout=min(20, remaining)) as response:
        if response.status != 200:
            raise ProvisionError('model_download_failed')
        if expected and response.headers.get('Content-Length'):
            if int(response.headers['Content-Length']) != expected['size']:
                raise ProvisionError('model_download_size_mismatch')
        stream = path.open('xb') if path else None
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise ProvisionError('model_download_timeout')
                data = response.read(min(256 * 1024, limit - received + 1))
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


def _worker(stage: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    opener = urllib.request.build_opener(_PublicModelRedirect())
    metadata_bytes = _download(
        opener, f'{_BASE}/api/models/{MODEL_REPOSITORY}/revision/{MODEL_REVISION}?blobs=true',
        deadline,
    )
    metadata = json.loads(metadata_bytes)
    if metadata.get('sha') != MODEL_REVISION:
        raise ProvisionError('model_revision_mismatch')
    siblings = {item.get('rfilename'): item for item in metadata.get('siblings', [])}
    for name, expected in MODEL_ARTIFACTS.items():
        item = siblings.get(name, {})
        if item.get('size') != expected['size']:
            raise ProvisionError('model_metadata_mismatch')
        if name.endswith('.onnx') and item.get('lfs', {}).get('sha256') != expected['sha256']:
            raise ProvisionError('model_metadata_mismatch')
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _download(opener, f'{_BASE}/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}',
                  deadline, path=path, expected=expected)
    _fsync_directory(stage / 'onnx')
    _fsync_directory(stage)


def provision(destination: Path, timeout_seconds: float, replace: bool = False) -> dict:
    """A bounded child handles downloads; the parent owns cleanup and publication."""
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ProvisionError('model_destination_rejected')
    if destination.exists():
        try:
            _EMBEDDINGS._load_verified_artifacts(destination)
            return {'status': 'already_verified', 'revision': MODEL_REVISION,
                    'artifacts': len(MODEL_ARTIFACTS), 'network_requests': 0}
        except _EMBEDDINGS.VectorError:
            if not replace:
                raise ProvisionError('model_existing_directory_invalid') from None
        if not destination.is_dir():
            raise ProvisionError('model_destination_rejected')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.navren-multilingual-embedding-stage-', dir=destination.parent))
    os.chmod(stage, 0o700)
    backup = None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), '--worker-stage', str(stage),
                   '--timeout-seconds', str(timeout_seconds)]
        # subprocess timeout kills and waits for the child before removing staging;
        # even a stuck DNS/read cannot make provisioning run indefinitely.
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            raise ProvisionError('model_download_timeout') from None
        if completed.returncode:
            allowed_codes = {
                'model_download_timeout', 'model_redirect_rejected', 'model_download_failed',
                'model_download_size_mismatch', 'model_download_hash_mismatch',
                'model_revision_mismatch', 'model_metadata_mismatch',
            }
            code = completed.stdout.strip()
            raise ProvisionError(code if code in allowed_codes else 'model_download_failed')
        manifest = {
            'format_version': 1, 'complete': True,
            'repository': MODEL_REPOSITORY, 'revision': MODEL_REVISION,
            'dimension': _EMBEDDINGS.DIMENSION,
            'algorithm_policy': _EMBEDDINGS.ALGORITHM_POLICY,
            'artifacts': {
                name: {**values, 'source_url':
                       f'{_BASE}/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}'}
                for name, values in MODEL_ARTIFACTS.items()
            },
            'license': {
                'upstream_spdx': 'MIT',
                'upstream_model': _EMBEDDINGS.UPSTREAM_REPOSITORY,
                'upstream_model_card':
                    f'{_BASE}/{_EMBEDDINGS.UPSTREAM_REPOSITORY}/blob/'
                    f'{_EMBEDDINGS.UPSTREAM_REVISION}/README.md',
                'conversion_repository': f'{_BASE}/{MODEL_REPOSITORY}/tree/{MODEL_REVISION}',
                'conversion_declared_license': None,
                'notice': 'The upstream model card declares MIT. The ONNX export card '
                          'identifies this upstream but does not separately declare a license.',
            },
            'tokenization': {
                'max_tokens': _EMBEDDINGS.MAX_TOKENS,
                'window_overlap': _EMBEDDINGS.WINDOW_OVERLAP,
                'max_windows': _EMBEDDINGS.MAX_WINDOWS,
                'query_prefix': _EMBEDDINGS.QUERY_PREFIX,
                'document_prefix_per_window': _EMBEDDINGS.DOCUMENT_PREFIX,
                'special_token_ids': _EMBEDDINGS.SPECIAL_TOKEN_IDS,
                'export_config_pad_token_id': 0,
                'padding_policy': 'tokenizer pad token ID 1, attention mask 0',
                'pooling': 'attention-mask mean, then L2 normalization',
            },
            'provisioned_at': datetime.now(timezone.utc).isoformat(),
            'inference': {'provider': 'CPUExecutionProvider', 'intra_op_threads': 2,
                          'inter_op_threads': 1, 'max_batch': _EMBEDDINGS.MAX_BATCH,
                          'query_network_requests': 0},
        }
        with (stage / 'manifest.json').open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        _EMBEDDINGS._load_verified_artifacts(stage)
        _fsync_directory(stage)
        if destination.exists():
            # Recheck at publication time: never overwrite a concurrent provision.
            if not replace:
                raise ProvisionError('model_destination_changed')
            backup = destination.parent / (stage.name + '-previous')
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
            _fsync_directory(destination.parent)
        except Exception:
            if backup and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup and backup.exists():
            shutil.rmtree(backup)
        return {'status': 'provisioned', 'revision': MODEL_REVISION,
                'artifacts': len(MODEL_ARTIFACTS),
                'bytes': sum(item['size'] for item in MODEL_ARTIFACTS.values()),
                'algorithm_policy': _EMBEDDINGS.ALGORITHM_POLICY}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description='准备已固定版本的本地多语向量模型；查询阶段不联网。')
    parser.add_argument('--destination', type=Path, default=_EMBEDDINGS.default_model_directory())
    parser.add_argument('--timeout-seconds', type=float, default=540)
    parser.add_argument('--replace', action='store_true', help='替换校验失败的已有模型目录')
    parser.add_argument('--worker-stage', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 10 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds 必须在 10 到 600 秒之间')
    try:
        if args.worker_stage:
            _worker(args.worker_stage, args.timeout_seconds)
        else:
            print(json.dumps(provision(args.destination, args.timeout_seconds, args.replace),
                             ensure_ascii=False, sort_keys=True))
        return 0
    except ProvisionError as error:
        print(str(error) if args.worker_stage else json.dumps({'status': 'failed', 'code': str(error)}))
        return 1
    except Exception:
        print('model_download_failed' if args.worker_stage else
              json.dumps({'status': 'failed', 'code': 'model_provision_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
