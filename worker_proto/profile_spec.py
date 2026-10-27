"""Static, source-pinned profile descriptors; no ONNX session construction.

The real path computes the exact fingerprint formula used by the two current
encoder constructors. Tests compare this result against the constructors'
pre-session assignment, and the worker compares it to every loaded session.
"""
from __future__ import annotations

import hashlib
import json
import os


def profile_spec(profile: str, backend: str | None = None) -> dict:
    backend = backend or os.environ.get('NAVREN_ENCODER_BACKEND', 'real')
    if profile not in ('e5','aligned'):
        raise ValueError('profile_invalid')
    if backend == 'simulate':
        return {'profile_id':'fixture-' + profile + '-384', 'dimension':384,
                'max_windows':8, 'model_name':'fixture-' + profile}
    if backend != 'real':
        raise ValueError('backend_invalid')
    if profile == 'e5':
        from local_knowledge import multilingual_embeddings as model
        prefix_tokenization = 'trim-prefix-trailing-space-before-separate-content-v1'
        name = model.MultilingualEncoder.model_name
        max_windows = model.MAX_WINDOWS
    else:
        from local_knowledge import aligned_embeddings as model
        prefix_tokenization = 'none'
        name = model.AlignedEncoder.model_name
        max_windows = model.MAX_WINDOWS
    fingerprint = json.dumps({
        'repository':model.MODEL_REPOSITORY,
        'revision':model.MODEL_REVISION,
        'artifacts':model.MODEL_ARTIFACTS,
        'policy':model.ALGORITHM_POLICY,
        'pooling':'attention-mask-mean-l2',
        'dimension':model.DIMENSION,
        'query_prefix':model.QUERY_PREFIX,
        'document_prefix':model.DOCUMENT_PREFIX,
        'prefix_tokenization':prefix_tokenization,
        'window_tokens':model.MAX_TOKENS,
        'overlap_tokens':model.WINDOW_OVERLAP,
        'max_windows':max_windows,
        'special_token_ids':model.SPECIAL_TOKEN_IDS,
    }, sort_keys=True, separators=(',', ':')).encode()
    return {'profile_id':name + ':' + hashlib.sha256(fingerprint).hexdigest(),
            'dimension':model.DIMENSION,'max_windows':max_windows,'model_name':name}
