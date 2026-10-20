"""Local encoder selection and profile-specific retrieval policy.

No network or mixed mathematical spaces. The default explicit dual policy uses
E5 and an additional sentence alignment encoder only for sparse cross-language
queries. Each needs its own provisioned artifacts and derived index. Missing
branches have explicit diagnostics, never an automatic model download.
"""
import os
from .embeddings import VectorError


def selection():
    value = os.environ.get('NAVREN_VECTOR_ENCODER', 'dual').strip().casefold()
    if value not in ('bge', 'e5', 'aligned', 'dual'):
        raise VectorError('vector_model_invalid')
    return value


def selected_model_directory():
    if selection() == 'bge':
        from .embeddings import default_model_directory
    elif selection() in ('e5', 'dual'):
        from .multilingual_embeddings import default_model_directory
    else:
        from .aligned_embeddings import default_model_directory
    return default_model_directory()


def get_retrieval_encoder():
    if selection() == 'bge':
        from .embeddings import get_encoder
        return get_encoder()
    if selection() == 'e5':
        from .multilingual_embeddings import get_multilingual_encoder
        return get_multilingual_encoder()
    if selection() == 'dual':
        from .multilingual_embeddings import get_multilingual_encoder
        from .aligned_embeddings import get_aligned_encoder
        return EncoderBundle(get_multilingual_encoder, get_aligned_encoder)
    from .aligned_embeddings import get_aligned_encoder
    return get_aligned_encoder()


class EncoderBundle:
    """Explicit independent spaces; compatibility diagnostics use only primary.

    The second CPU session is acquired only for build/status or a cross-language
    query. VectorIndex handles each manifest separately; vectors never mix.
    """
    def __init__(self, primary, secondary_factory):
        self._primary_factory = primary if callable(primary) else lambda: primary
        self._secondary_factory = secondary_factory

    @property
    def primary(self):
        return self._primary_factory()

    @property
    def members(self):
        return (self.primary, self._secondary_factory())

    @property
    def factories(self):
        return (self._primary_factory, self._secondary_factory)

    def __getattr__(self, name):
        return getattr(self.primary, name)


def is_bundle(encoder):
    return isinstance(encoder, EncoderBundle)


def cross_language_query(query, rows):
    """Local script heuristic enables extra recall only after evidence gates."""
    import re
    from .vector_documents import representation
    query_han = bool(re.search(r'[\u4e00-\u9fff]', query))
    for row in rows:
        text = representation(row)['text']
        han = len(re.findall(r'[\u4e00-\u9fff]', text))
        latin = len(re.findall(r'[A-Za-z]', text))
        if query_han and latin >= 40 and latin > 2 * han:
            return True
        if not query_han and han >= 20:
            return True
    return False


def score_policy(encoder):
    # E5 cosine has a compressed, high baseline. Separate retrieval calibration;
    # these values are never answer confidence or evidence-authority thresholds.
    if getattr(encoder, 'model_name', None) == 'multilingual-e5-small-q8':
        return 0.78, 0.08
    if getattr(encoder, 'model_name', None) == 'paraphrase-multilingual-minilm-l12-v2-q8':
        return 0.35, 0.20
    return 0.40, 0.12
