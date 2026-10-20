"""Pinned, CPU-only embeddings. No network, automatic download, or text logging.

The provisioning command is deliberately separate from this query-time module.
Vectors are retrieval candidates, never evidence of a financial claim.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any

MODEL_REPOSITORY = 'Xenova/bge-small-zh-v1.5'
MODEL_REVISION = '75c43b069aac4d136ba6bc1122f995fedcfd2781'
DIMENSION = 512
QUERY_INSTRUCTION = '为这个句子生成表示以用于检索相关文章：'
MAX_TOKENS = 512
WINDOW_OVERLAP = 64
MAX_WINDOWS = 8
MAX_BATCH = 4
MAX_TEXT_CHARACTERS = 65536
ALGORITHM_POLICY = 'bge-cls-l2-query-instruction-zh512-w512-overlap64-max8-v1'
MODEL_ARTIFACTS = {
    'tokenizer.json': {
        'sha256': '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26',
        'size': 439125,
    },
    'config.json': {
        'sha256': 'd4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f',
        'size': 716,
    },
    'onnx/model_quantized.onnx': {
        'sha256': '15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc',
        'size': 24010842,
    },
}
ERROR_CODES = frozenset({
    'vector_model_missing', 'vector_model_invalid', 'vector_dependency_missing',
    'vector_input_invalid', 'vector_text_too_long', 'vector_query_too_long',
    'vector_inference_failed',
})


class VectorError(RuntimeError):
    """A controlled status code; never expose an underlying path or input text."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else 'vector_inference_failed'
        super().__init__(self.code)


def default_model_directory() -> Path:
    configured = os.environ.get('NAVREN_EMBEDDING_DIR', '').strip()
    if configured:
        return Path(configured).expanduser()
    return (Path.home() / 'Library' / 'Application Support' / 'Navren' /
            'models' / 'bge-small-zh-v1.5-onnx-q8')


def _load_verified_artifacts(directory: Path) -> dict[str, bytes]:
    """Hash the exact bytes subsequently passed to Tokenizer/ORT, avoiding races."""
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
                or configuration.get('max_position_embeddings') != MAX_TOKENS
                or configuration.get('model_type') != 'bert'):
            raise VectorError('vector_model_invalid')
        return artifacts
    except VectorError:
        raise
    except FileNotFoundError:
        raise VectorError('vector_model_missing') from None
    except Exception:
        raise VectorError('vector_model_invalid') from None


class LocalEncoder:
    dimension = DIMENSION

    def __init__(self, directory: Path):
        artifacts = _load_verified_artifacts(directory)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except Exception:
            raise VectorError('vector_dependency_missing') from None
        self._np = np
        self._lock = threading.Lock()
        fingerprint = json.dumps({
            'repository': MODEL_REPOSITORY, 'revision': MODEL_REVISION,
            'artifacts': MODEL_ARTIFACTS, 'policy': ALGORITHM_POLICY,
        }, sort_keys=True, separators=(',', ':')).encode()
        self.profile_id = 'bge-small-zh-v1.5-q8:' + hashlib.sha256(fingerprint).hexdigest()
        try:
            ort.disable_telemetry_events()
            tokenizer = Tokenizer.from_str(artifacts['tokenizer.json'].decode('utf-8'))
            tokenizer.enable_truncation(max_length=MAX_TOKENS, stride=WINDOW_OVERLAP)
            tokenizer.enable_padding(direction='right', pad_id=0, pad_type_id=0,
                                     pad_token='[PAD]')
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.log_severity_level = 4
            options.enable_profiling = False
            self._session = ort.InferenceSession(
                artifacts['onnx/model_quantized.onnx'], sess_options=options,
                providers=['CPUExecutionProvider'],
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

    def _token_windows(self, text: str) -> list[Any]:
        encoding = self._tokenizer.encode(text, add_special_tokens=True)
        # Native tokenizers retains the final partial window in `overflowing`.
        windows = [encoding, *encoding.overflowing]
        if len(windows) > MAX_WINDOWS:
            raise VectorError('vector_text_too_long')
        if any(not item.ids or len(item.ids) > MAX_TOKENS for item in windows):
            raise VectorError('vector_inference_failed')
        return windows

    def _infer(self, windows: list[Any]) -> Any:
        np = self._np
        results = []
        for start in range(0, len(windows), MAX_BATCH):
            batch = windows[start:start + MAX_BATCH]
            length = max(len(item.ids) for item in batch)
            arrays = {
                'input_ids': np.zeros((len(batch), length), dtype=np.int64),
                'attention_mask': np.zeros((len(batch), length), dtype=np.int64),
                'token_type_ids': np.zeros((len(batch), length), dtype=np.int64),
            }
            for row, item in enumerate(batch):
                size = len(item.ids)
                arrays['input_ids'][row, :size] = item.ids
                arrays['attention_mask'][row, :size] = item.attention_mask
                arrays['token_type_ids'][row, :size] = item.type_ids
            output = self._session.run(
                [self._output_name], {name: arrays[name] for name in self._input_names},
            )[0]
            if (output.ndim != 3 or output.shape[0] != len(batch)
                    or output.shape[1] != length or output.shape[2] != DIMENSION):
                raise VectorError('vector_inference_failed')
            vectors = np.asarray(output[:, 0, :], dtype=np.float32)
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
                windows = self._token_windows(QUERY_INSTRUCTION + text)
                if len(windows) != 1:
                    raise VectorError('vector_query_too_long')
                return self._infer(windows)[0]
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
                window_groups = [self._token_windows(text) for text in texts]
                vectors = self._infer([window for group in window_groups for window in group])
                result = []
                offset = 0
                for group in window_groups:
                    result.append(vectors[offset:offset + len(group)].copy())
                    offset += len(group)
                return result
        except VectorError:
            raise
        except Exception:
            raise VectorError('vector_inference_failed') from None


_encoder_lock = threading.Lock()
_encoder: LocalEncoder | None = None
_encoder_directory: str | None = None


def get_encoder() -> LocalEncoder:
    """Share one lazy encoder per process; errors are retryable after provisioning."""
    global _encoder, _encoder_directory
    directory = default_model_directory()
    key = str(directory.absolute())
    with _encoder_lock:
        if _encoder is None or _encoder_directory != key:
            _encoder = LocalEncoder(directory)
            _encoder_directory = key
        return _encoder
