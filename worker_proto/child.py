"""One model profile per short-lived child process; no database access."""
from __future__ import annotations

import base64
import hashlib
import os
import time

ALLOWED_ERRORS = frozenset({
    'vector_input_invalid', 'vector_query_too_long', 'vector_text_too_long',
    'vector_model_missing', 'vector_model_invalid', 'vector_dependency_missing',
    'vector_inference_failed',
})


class SimulatedEncoder:
    """Deterministic fixture only; cannot run real ONNX by accident."""
    dimension = 384
    max_windows = 8

    def __init__(self, profile: str):
        self.profile_id = 'fixture-' + profile + '-384'
        self.model_name = 'fixture-' + profile

    @staticmethod
    def _vector(text: str):
        import numpy as np
        if text == '__crash__':
            os._exit(77)
        if text == '__slow__':
            time.sleep(2)
        digest = hashlib.sha256(text.encode()).digest()
        value = np.zeros(384, dtype='<f4')
        value[digest[0]] = 1.0
        return value

    def encode_query(self, text: str):
        return self._vector(text)

    def encode_documents(self, texts: list[str]):
        return [self._vector(text)[None, :] for text in texts]


def _load(profile: str, backend: str):
    if backend == 'simulate':
        return SimulatedEncoder(profile)
    if backend != 'real':
        raise RuntimeError('backend_invalid')
    # This path runs only inside the isolated worker process. The Web process
    # must use a proxy and must never import or instantiate these sessions.
    if profile == 'e5':
        from local_knowledge.multilingual_embeddings import get_multilingual_encoder
        return get_multilingual_encoder()
    if profile == 'aligned':
        from local_knowledge.aligned_embeddings import get_aligned_encoder
        return get_aligned_encoder()
    raise RuntimeError('profile_invalid')


def _pack(value):
    import numpy as np
    array = np.asarray(value, dtype='<f4')
    if array.ndim not in (1, 2) or array.shape[-1] != 384 or (array.ndim == 2 and not 1 <= array.shape[0] <= 16):
        raise RuntimeError('encoder_shape_invalid')
    if not np.isfinite(array).all():
        raise RuntimeError('encoder_value_invalid')
    return {'shape':list(array.shape), 'float32_le_b64':base64.b64encode(array.tobytes()).decode('ascii')}


def child_main(conn, profile: str, backend: str) -> None:
    # The service unit must not load credentials; remove these defensively.
    for name in ('DEEPSEEK_API_KEY', 'GLM_API_KEY', 'NAVREN_SMS_ACCESS_KEY_ID',
                 'NAVREN_SMS_ACCESS_KEY_SECRET', 'NAVREN_PERSONAL_DATA_KEY'):
        os.environ.pop(name, None)
    try:
        encoder = _load(profile, backend)
        conn.send({'ready':True,'profile_id':encoder.profile_id,'dimension':encoder.dimension,
                   'max_windows':getattr(encoder, 'max_windows', 8),
                   'model_name':encoder.model_name})
        while True:
            request = conn.recv()
            if request['op'] == 'close':
                break
            try:
                if request['op'] == 'query':
                    result = _pack(encoder.encode_query(request['input']))
                elif request['op'] == 'documents':
                    result = [_pack(value) for value in encoder.encode_documents(request['input'])]
                else:
                    raise RuntimeError('op_invalid')
                conn.send({'ok':True,'result':result})
            except Exception as error:
                code = getattr(error, 'code', None)
                conn.send({'ok':False,'code':code if code in ALLOWED_ERRORS else 'vector_inference_failed'})
    except EOFError:
        pass
    except Exception as error:
        code = getattr(error, 'code', None)
        try:conn.send({'ready':False,'code':code if code in ALLOWED_ERRORS else 'vector_model_invalid'})
        except Exception:pass
    finally:
        conn.close()
