"""Host-bound complete current-request quotations, never financial facts.

The only display is a standalone attributed source followed by the model's
unchanged business body.  Provenance is deterministic; semantic use still
requires the original complete review and publication gates.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import unicodedata
import uuid

from simulated_bank import BankError

SOURCE_CONTRACT = 'navren.current-request-quote-source/1'
PROOF_CONTRACT = 'navren.current-request-quote-proof/2'
LABEL = '你刚才说：'
MAX_SOURCE = 600
MAX_DISPLAY_BYTES = 8192
_ISSUER = object()


def _packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _digest(value):
    return _sha(_packed(value))


def _error(code='REQUEST_QUOTE_INVALID', status=422):
    return BankError(code, '本轮原话引用未通过来源或展示校验，未展示该引用。', status)


def _nfkc(text):
    return unicodedata.normalize('NFKC', text)


def lexeme_roles(text):
    """Raw-character money/percentage spans; no value or currency inference."""
    if type(text) is not str:
        raise _error()
    from . import metric_claims as money
    normalized = _nfkc(text)
    # Prefix lengths map normalized recognition back onto complete raw chars.
    ends = [len(_nfkc(text[:i])) for i in range(len(text) + 1)]
    found = []
    # Use the existing monetary parser's exact suppression and unsupported
    # classification; these are lexical spans, never issued monetary facts.
    found.extend(('money', start, end) for start, end in money.monetary_lexeme_spans(normalized))
    percentage = re.compile(r'(?:百分之\s*[0-9零〇一二两三四五六七八九十百千万亿点.]+|'
                            r'[-+]?\d+(?:\.\d+)?\s*(?:%|个百分点|百分点|基点))')
    found.extend(('percentage', *m.span()) for m in percentage.finditer(normalized))
    selected = []
    # A currency-bearing literal and its bare-scale subtoken are one lexeme.
    for kind, start, end in sorted(found, key=lambda x: (x[1], -(x[2] - x[1]), x[0])):
        if any(a <= start and end <= b for _, a, b in selected):
            continue
        if any(start < b and a < end for _, a, b in selected):
            raise _error('REQUEST_QUOTE_LEXEME_AMBIGUOUS')
        selected.append((kind, start, end))
    result = []
    for kind, start, end in selected:
        left = next((i for i, value in enumerate(ends) if value == start), None)
        right = next((i for i, value in enumerate(ends) if value == end), None)
        if left is None or right is None or left >= right or _nfkc(text[left:right]) != normalized[start:end]:
            raise _error('REQUEST_QUOTE_LEXEME_AMBIGUOUS')
        result.append({'kind': kind, 'start': left, 'end': right, 'text': text[left:right]})
    return tuple(result)


def safe_source_text(text):
    """Refuse a source that cannot remain complete, private-safe plain text."""
    if (type(text) is not str or not text.strip() or len(text) > MAX_SOURCE
            or any(unicodedata.category(ch) in ('Cc', 'Cf', 'Cs') for ch in text)
            or any(ch in text for ch in '<>[]`\\')
            or re.search(r'(?:https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})', text, re.I)):
        return False
    from personal_data import redact_sensitive
    if redact_sensitive(text) != text or '[已隐藏' in text:
        return False
    # No source-specific privacy exemption: the normal output boundary must
    # accept the complete original. The caller also checks its seeded boundary.
    from .data_boundary import CloudDataBoundary, BoundaryError
    try:
        boundary = CloudDataBoundary()
        output = boundary.check_output({'text': text, 'claims': [], 'draft_ids': [], 'followups': []})
        if output.get('text') != text:
            return False
        lexeme_roles(text)
    except (BankError, BoundaryError, ValueError, TypeError, AttributeError):
        return False
    return True


class RequestQuoteSource:
    __slots__ = ('_authority', '_owner', '_issuer', '_binding_json', '_row_sha',
                 '_text', '_quote_ref', '_snapshot_json')

    def __init__(self, owner, issuer, binding, row_sha, text, *, _authority=None):
        if (_authority is not _ISSUER or issuer is not getattr(owner, '_request_quote_issuer', None)
                or not safe_source_text(text)):
            raise _error()
        values = {'_authority': _authority, '_owner': owner, '_issuer': issuer,
                  '_binding_json': _packed(binding), '_row_sha': row_sha,
                  '_text': text, '_quote_ref': 'uq_' + uuid.uuid4().hex}
        for key, value in values.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, '_snapshot_json', _packed(self._snapshot()))

    def __setattr__(self, *_):
        raise TypeError('immutable_host_request_quote_source')

    def __deepcopy__(self, memo):
        return self

    @property
    def quote_ref(self): return self._quote_ref

    @property
    def text(self): return self._text

    def _snapshot(self):
        binding = json.loads(self._binding_json)
        roles = lexeme_roles(self._text)
        return {'contract': SOURCE_CONTRACT, 'quote_ref': self._quote_ref,
                'source_sha256': _sha(self._text), 'source_nfkc_sha256': _sha(_nfkc(self._text)),
                'source_character_count': len(self._text), 'source_utf8_bytes': len(self._text.encode()),
                'binding_sha256': _digest({'binding': binding, 'row_sha256': self._row_sha}),
                **{k: binding[k] for k in ('archive_sequence', 'archive_epoch', 'privacy_revision', 'personal_revision')},
                'source_kind': 'current_authenticated_user_request', 'epistemic_status': 'unverified_user_input',
                'full_current_request': True, 'financial_fact_eligible': False, 'execution_authority': False,
                'raw_money_lexeme_count': sum(r['kind'] == 'money' for r in roles),
                'raw_percentage_lexeme_count': sum(r['kind'] == 'percentage' for r in roles)}

    def snapshot(self):
        assert_source(self)
        return json.loads(self._snapshot_json)


def _issue_source(owner, binding, row_sha, text):
    return RequestQuoteSource(owner, owner._request_quote_issuer, binding, row_sha, text, _authority=_ISSUER)


def assert_source(source, *, owner=None, workspace=None, request_id=None):
    if (type(source) is not RequestQuoteSource or source._authority is not _ISSUER
            or source._issuer is not getattr(source._owner, '_request_quote_issuer', None)
            or owner is not None and source._owner is not owner
            or re.fullmatch(r'uq_[a-f0-9]{32}', source._quote_ref) is None
            or re.fullmatch(r'[a-f0-9]{64}', source._row_sha) is None
            or not safe_source_text(source._text)
            or _packed(source._snapshot()) != source._snapshot_json):
        raise _error()
    binding = json.loads(source._binding_json)
    if (workspace is not None and binding['workspace_id'] != workspace
            or request_id is not None and binding['request_id'] != request_id):
        raise _error('REQUEST_QUOTE_SCOPE_MISMATCH', 403)
    return True


def _body_question_display(body, question):
    """Preserve both model fields; never infer that their contexts coincide."""
    if question is None:
        return body, None, None, False
    display=body+'\n\n'+question
    start=len(_nfkc(body+'\n\n'))
    return display,start,start+len(_nfkc(question)),False

def _render(source, body, question):
    prefix = LABEL + '\n“'
    quote_start = len(prefix)
    rendered, _, _, _ = _body_question_display(body, question)
    result = prefix + source.text + '”\n\n' + rendered
    return result, (quote_start, quote_start + len(source.text))


class RequestQuoteProof:
    __slots__ = ('_authority', '_source', '_body', '_question', '_display', '_span', '_snapshot_json')

    def __init__(self, source, body, question, *, _authority=None):
        if _authority is not _ISSUER: raise _error()
        assert_source(source)
        if (type(body) is not str or not body.strip()
                or len(body) > (1962 if question is None else 1800)
                or not (question is None or type(question) is str and question.strip() and 1 <= len(question) <= 160)):
            raise _error()
        # Free fields keep the original parser; this class cannot legalize any
        # financial number written by the model, even if equal to the source.
        from .metric_claims import monetary_occurrences, MetricClaimError
        try:
            for value in (body, question):
                if value is not None and monetary_occurrences(_nfkc(value)): raise _error()
        except MetricClaimError as error:
            raise _error(error.code) from error
        display, span = _render(source, body, question)
        if len(display.encode()) > MAX_DISPLAY_BYTES: raise _error('REQUEST_QUOTE_DISPLAY_LIMIT')
        for key, value in {'_authority': _authority, '_source': source, '_body': body,
                           '_question': question, '_display': display, '_span': span}.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, '_snapshot_json', _packed(self._snapshot()))

    def __setattr__(self, *_): raise TypeError('immutable_host_request_quote_proof')
    def __deepcopy__(self, memo): return self
    @property
    def source(self): return self._source
    @property
    def final_text(self): return self._display
    @property
    def display(self): return self._display
    @property
    def quote_span(self): return self._span
    @property
    def financial_text(self): return _body_question_display(self._body, self._question)[0]

    def _snapshot(self):
        raw = lexeme_roles(self._display); quoted = lexeme_roles(self._source.text); financial = lexeme_roles(self.financial_text)
        start, end = self._span
        result = {'contract': PROOF_CONTRACT, 'source': self._source.snapshot(), 'attribution_label': LABEL,
                  'final_raw_sha256': _sha(self._display), 'final_nfkc_sha256': _sha(_nfkc(self._display)),
                  'body_raw_sha256': _sha(self._body), 'body_nfkc_sha256': _sha(_nfkc(self._body)),
                  'question_raw_sha256': _sha(self._question) if self._question is not None else None,
                  'question_nfkc_sha256': _sha(_nfkc(self._question)) if self._question is not None else None,
                  'quote_start': start, 'quote_end': end, 'quote_start_nfkc': len(_nfkc(self._display[:start])),
                  'quote_end_nfkc': len(_nfkc(self._display[:end])), 'model_body_preserved_verbatim': True,
                  'source_preserved_verbatim': True, 'host_added_standalone_attribution': True,
                  'financial_fact_eligible': False, 'execution_authority': False}
        for kind, name in (('money', 'money'), ('percentage', 'percentage')):
            result['raw_' + name + '_lexeme_count'] = sum(r['kind'] == kind for r in raw)
            result['source_quote_' + name + '_lexeme_count'] = sum(r['kind'] == kind for r in quoted)
            result['financial_text_' + name + '_lexeme_count'] = sum(r['kind'] == kind for r in financial)
        return result

    def snapshot(self):
        assert_display(self, self._display)
        return json.loads(self._snapshot_json)

    def validate_display(self, text): return assert_display(self, text)

    def quote_spans(self, text):
        assert_display(self, text)
        if text == self._display: return (self._span,)
        return ((len(_nfkc(self._display[:self._span[0]])), len(_nfkc(self._display[:self._span[1]]))),)


def compose(source, body, clarification_question=None):
    proof = RequestQuoteProof(source, body, clarification_question, _authority=_ISSUER)
    return proof.final_text, proof


def assert_display(proof, display_text, *, source=None):
    if (type(proof) is not RequestQuoteProof or proof._authority is not _ISSUER
            or source is not None and proof.source is not source):
        raise _error()
    assert_source(proof._source)
    expected, span = _render(proof._source, proof._body, proof._question)
    if (expected != proof._display or span != proof._span
            or _packed(proof._snapshot()) != proof._snapshot_json
            or type(display_text) is not str or display_text not in (expected, _nfkc(expected))):
        raise _error('REQUEST_QUOTE_DISPLAY_MISMATCH')
    return True
