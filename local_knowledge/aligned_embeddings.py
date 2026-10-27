"""Pinned cross-language sentence alignment, entirely local after provisioning.

The official model and ONNX artifact declare Apache-2.0 in their model card.
It is an encoder, not a text generator; similarity never proves a financial fact.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .embeddings import VectorError
from .onnx_memory import create_cpu_session

MODEL_REPOSITORY = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
MODEL_REVISION = 'e8f8c211226b894fcb81acc59f3b34ba3efd5f42'
UPSTREAM_REPOSITORY = MODEL_REPOSITORY
UPSTREAM_REVISION = MODEL_REVISION
DIMENSION = 384
QUERY_PREFIX = ''
DOCUMENT_PREFIX = ''
MAX_TOKENS = 128
MODEL_MAX_POSITIONS = 512
WINDOW_OVERLAP = 16
MAX_WINDOWS = 16
MAX_BATCH = 2
MAX_TEXT_CHARACTERS = 65536
MAX_CALL_SECONDS = 120.0
MAX_BATCH_SECONDS = 30.0
SPECIAL_TOKEN_IDS = {'<s>': 0, '<pad>': 1, '</s>': 2}
ALGORITHM_POLICY = 'minilm-aligned-mask-mean-l2-no-prefix384-w128-overlap16-max16-v1'
MODEL_ARTIFACTS = {
    'tokenizer.json': {
        'sha256': '2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8',
        'size': 9081518,
    },
    'config.json': {
        'sha256': '6300193cb75e01cf80c96decef7187dfb33094d97cc1490b7ead6ff134476e4e',
        'size': 645,
    },
    'onnx/model_qint8_arm64.onnx': {
        'sha256': '783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672',
        'size': 118412398,
    },
}


def default_model_directory() -> Path:
    configured = os.environ.get('NAVREN_ALIGNED_EMBEDDING_DIR', '').strip()
    if configured:
        return Path(configured).expanduser()
    return (Path.home() / 'Library' / 'Application Support' / 'Navren' /
            'models' / 'paraphrase-multilingual-minilm-l12-v2-onnx-q8')


def _load_verified_artifacts(directory: Path) -> dict[str, bytes]:
    """Verify exact bytes subsequently passed to the tokenizer and ONNX runtime."""
    try:
        manifest_path = directory / 'manifest.json'
        if not manifest_path.exists():
            raise VectorError('vector_model_missing')
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 16384:
            raise VectorError('vector_model_invalid')
        manifest = json.loads(manifest_path.read_bytes())
        if (not isinstance(manifest, dict) or manifest.get('format_version') != 1
                or manifest.get('repository') != MODEL_REPOSITORY
                or manifest.get('revision') != MODEL_REVISION
                or manifest.get('algorithm_policy') != ALGORITHM_POLICY
                or manifest.get('dimension') != DIMENSION
                or manifest.get('complete') is not True
                or not isinstance(manifest.get('artifacts'), dict)
                or set(manifest['artifacts']) != set(MODEL_ARTIFACTS)):
            raise VectorError('vector_model_invalid')
        artifacts = {}
        for relative_path, expected in MODEL_ARTIFACTS.items():
            record = manifest['artifacts'][relative_path]
            path = directory / relative_path
            if (not isinstance(record, dict) or record.get('sha256') != expected['sha256']
                    or record.get('size') != expected['size'] or path.is_symlink()
                    or path.stat().st_size != expected['size']):
                raise VectorError('vector_model_invalid')
            with path.open('rb') as stream:
                data = stream.read(expected['size'] + 1)
            if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise VectorError('vector_model_invalid')
            artifacts[relative_path] = data
        configuration = json.loads(artifacts['config.json'])
        if (configuration.get('hidden_size') != DIMENSION
                or configuration.get('max_position_embeddings') != MODEL_MAX_POSITIONS
                or configuration.get('model_type') != 'bert'
                or configuration.get('pad_token_id') != 0):
            raise VectorError('vector_model_invalid')
        return artifacts
    except VectorError:
        raise
    except FileNotFoundError:
        raise VectorError('vector_model_missing') from None
    except Exception:
        raise VectorError('vector_model_invalid') from None


class AlignedEncoder:
    dimension = DIMENSION
    model_name = 'paraphrase-multilingual-minilm-l12-v2-q8'
    max_windows = MAX_WINDOWS

    def __init__(self, directory: Path):
        artifacts = _load_verified_artifacts(directory)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except Exception:
            raise VectorError('vector_dependency_missing') from None
        self._np = np
        self._ort = ort
        self._lock = threading.Lock()
        fingerprint = json.dumps({
            'repository': MODEL_REPOSITORY, 'revision': MODEL_REVISION,
            'artifacts': MODEL_ARTIFACTS, 'policy': ALGORITHM_POLICY,
            'pooling': 'attention-mask-mean-l2', 'dimension': DIMENSION,
            'query_prefix': QUERY_PREFIX, 'document_prefix': DOCUMENT_PREFIX,
            'prefix_tokenization': 'none',
            'window_tokens': MAX_TOKENS, 'overlap_tokens': WINDOW_OVERLAP,
            'max_windows': MAX_WINDOWS, 'special_token_ids': SPECIAL_TOKEN_IDS,
        }, sort_keys=True, separators=(',', ':')).encode()
        self.profile_id = self.model_name + ':' + hashlib.sha256(fingerprint).hexdigest()
        try:
            ort.disable_telemetry_events()
            tokenizer = Tokenizer.from_str(artifacts['tokenizer.json'].decode('utf-8'))
            tokenizer.no_truncation()
            tokenizer.no_padding()
            if any(tokenizer.token_to_id(token) != value
                   for token, value in SPECIAL_TOKEN_IDS.items()):
                raise VectorError('vector_model_invalid')
            # The pinned export config declares pad_id=0, but its tokenizer's
            # <pad> is 1. Use the actual tokenizer IDs, with padding masked out.
            self._pad_id = SPECIAL_TOKEN_IDS['<pad>']
            self._bos_id = SPECIAL_TOKEN_IDS['<s>']
            self._eos_id = SPECIAL_TOKEN_IDS['</s>']
            self._prefix_ids = {'': []}
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.log_severity_level = 4
            options.enable_profiling = False
            self._session, self._model_snapshot = create_cpu_session(
                ort, artifacts, 'onnx/model_qint8_arm64.onnx',
                MODEL_ARTIFACTS['onnx/model_qint8_arm64.onnx'], options,
            )
            self._input_names = {item.name for item in self._session.get_inputs()}
            self._output_name = self._session.get_outputs()[0].name
            if (not {'input_ids', 'attention_mask'}.issubset(self._input_names)
                    or self._input_names - {'input_ids', 'attention_mask', 'token_type_ids'}
                    or self._session.get_providers() != ['CPUExecutionProvider']):
                raise VectorError('vector_model_invalid')
            self._tokenizer = tokenizer
        except VectorError:
            raise
        except Exception:
            raise VectorError('vector_model_invalid') from None

    @staticmethod
    def _check_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise VectorError('vector_input_invalid')
        if len(text) > MAX_TEXT_CHARACTERS:
            raise VectorError('vector_text_too_long')

    def _token_windows(self, text: str, prefix: str, *, query: bool = False) -> list[list[int]]:
        """Every window has BOS, its task prefix, actual content, and EOS."""
        content = self._tokenizer.encode(text, add_special_tokens=False).ids
        prefix_ids = self._prefix_ids[prefix]
        capacity = MAX_TOKENS - len(prefix_ids) - 2
        if not content:
            raise VectorError('vector_input_invalid')
        if query and len(content) > capacity:
            raise VectorError('vector_query_too_long')
        windows = []
        offset = 0
        while offset < len(content):
            end = min(offset + capacity, len(content))
            windows.append([self._bos_id, *prefix_ids, *content[offset:end], self._eos_id])
            if len(windows) > MAX_WINDOWS:
                raise VectorError('vector_text_too_long')
            if end == len(content):
                break
            offset = end - WINDOW_OVERLAP
        return windows

    def _infer(self, windows: list[list[int]], deadline: float) -> Any:
        np = self._np
        results = []
        for start in range(0, len(windows), MAX_BATCH):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VectorError('vector_inference_failed')
            batch = windows[start:start + MAX_BATCH]
            length = max(len(ids) for ids in batch)
            arrays = {
                'input_ids': np.full((len(batch), length), self._pad_id, dtype=np.int64),
                'attention_mask': np.zeros((len(batch), length), dtype=np.int64),
                'token_type_ids': np.zeros((len(batch), length), dtype=np.int64),
            }
            for row, ids in enumerate(batch):
                arrays['input_ids'][row, :len(ids)] = ids
                arrays['attention_mask'][row, :len(ids)] = 1
            run_options = self._ort.RunOptions()
            timer = threading.Timer(min(MAX_BATCH_SECONDS, remaining),
                                    lambda: setattr(run_options, 'terminate', True))
            timer.daemon = True
            timer.start()
            try:
                output = self._session.run(
                    [self._output_name], {name: arrays[name] for name in self._input_names},
                    run_options=run_options,
                )[0]
                if run_options.terminate or time.monotonic() >= deadline:
                    raise VectorError('vector_inference_failed')
            finally:
                timer.cancel()
            if (output.ndim != 3 or output.shape[0] != len(batch)
                    or output.shape[1] != length or output.shape[2] != DIMENSION):
                raise VectorError('vector_inference_failed')
            mask = arrays['attention_mask'][..., None].astype(np.float32)
            hidden = np.asarray(output, dtype=np.float32)
            if not np.isfinite(hidden).all():
                raise VectorError('vector_inference_failed')
            vectors = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (not np.isfinite(vectors).all() or not np.isfinite(norms).all()
                    or (norms <= 1e-12).any()):
                raise VectorError('vector_inference_failed')
            results.append(vectors / norms)
        return np.concatenate(results, axis=0).astype(np.float32, copy=False)

    def encode_query(self, text: str) -> Any:
        self._check_text(text)
        try:
            with self._lock:
                deadline = time.monotonic() + MAX_CALL_SECONDS
                windows = self._token_windows(text, QUERY_PREFIX, query=True)
                return self._infer(windows, deadline)[0]
        except VectorError:
            raise
        except Exception:
            raise VectorError('vector_inference_failed') from None

    def encode_documents(self, texts: list[str]) -> list[Any]:
        if not isinstance(texts, list):
            raise VectorError('vector_input_invalid')
        for text in texts:
            self._check_text(text)
        if not texts:
            return []
        try:
            with self._lock:
                deadline = time.monotonic() + MAX_CALL_SECONDS
                groups = [self._token_windows(text, DOCUMENT_PREFIX) for text in texts]
                vectors = self._infer([window for group in groups for window in group], deadline)
                result = []
                offset = 0
                for group in groups:
                    result.append(vectors[offset:offset + len(group)].copy())
                    offset += len(group)
                return result
        except VectorError:
            raise
        except Exception:
            raise VectorError('vector_inference_failed') from None


_encoder_lock = threading.Lock()
_encoder: AlignedEncoder | None = None
_encoder_directory: str | None = None


def get_aligned_encoder() -> AlignedEncoder:
    """Share a lazy local session per process and model directory; never download."""
    global _encoder, _encoder_directory
    directory = default_model_directory()
    key = str(directory.absolute())
    with _encoder_lock:
        if _encoder is None or _encoder_directory != key:
            _encoder = AlignedEncoder(directory)
            _encoder_directory = key
        return _encoder
