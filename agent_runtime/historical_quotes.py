"""宿主发行的历史逐字引文；不形成账户指标、比例建议或执行授权。"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
import uuid

from . import history_binding as history

MAX_QUOTE = 120
MAX_SELECTION = 3
INTRO = '以下仅引用明确选定的历史版本，不推断当前财务情况。'
NOTICE = '该旧版本的原文记载不能证明您的当前余额、可投资金或执行资格。'
HISTORICAL_AMOUNT = 'historical_amount'
HISTORICAL_PERCENTAGE = 'historical_percentage'
HISTORICAL_IDENTIFIER = 'historical_identifier'
DOMAINS = frozenset({HISTORICAL_AMOUNT, HISTORICAL_PERCENTAGE, HISTORICAL_IDENTIFIER})
_ISSUER = object()
_NUMBER = r'(?:[0-9０-９][0-9０-９.,．，\s]*|[零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億点點]+)'
_ROLE_PATTERNS = (
    (HISTORICAL_AMOUNT, re.compile(r'(?:人民币|美元|欧元|英镑|日元|CNY|RMB|USD|EUR|GBP|JPY|[¥￥$])?\s*' + _NUMBER + r'\s*(?:万亿|亿|万)?\s*(?:人民币|美元|欧元|英镑|日元|元|圆|CNY|RMB|USD|EUR|GBP|JPY)', re.I)),
    (HISTORICAL_PERCENTAGE, re.compile(r'(?:百分之\s*' + _NUMBER + r'|' + _NUMBER + r'\s*(?:[%％]|个百分点|百分点|基点))', re.I)),
    (HISTORICAL_IDENTIFIER, re.compile(r'(?:编号|代码|标识|ID)\s*[:：]?\s*[A-Za-z0-9０-９_.:-]+|[A-Za-z_][A-Za-z0-9_.:-]*[0-9][A-Za-z0-9_.:-]*|第\s*[0-9０-９]+\s*版|[0-9０-９]{4}[-/年][0-9０-９]{1,2}[-/月][0-9０-９]{1,2}日?', re.I)),
)


@dataclass(frozen=True, slots=True)
class HistoricalNumericRole:
    """只标注原文的词面域，不解析成可计算值，也不转成当前金融证据。"""
    domain: str
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class HistoricalQuote:
    quote_id: str
    article_id: str
    version: int
    body_sha256: str
    source_id: str
    start: int
    end: int
    text: str
    publisher: str
    published_at: str
    source_url: str
    identity: str
    numeric_roles: tuple[HistoricalNumericRole, ...]


def _short_complete_unit(text):
    if type(text) is not str or not text or text[-1] not in '。！？.!?':
        return None
    if len(text) <= MAX_QUOTE:
        return text
    for end in range(1, min(MAX_QUOTE, len(text)) + 1):
        if len(text) % end == 0 and text[end - 1] in '。！？.!?' and text == text[:end] * (len(text) // end):
            return text[:end]
    return None


def numeric_roles(text):
    """纯词面检测；区间始终指向未作 NFKC 改写的原文。"""
    if type(text) is not str:
        raise history._error('HISTORY_QUOTE_INVALID', '历史原文格式无效。', 422)
    roles = [HistoricalNumericRole(domain, m.start(), m.end(), text[m.start():m.end()])
             for domain, pattern in _ROLE_PATTERNS for m in pattern.finditer(text)]
    return tuple(sorted(roles, key=lambda role: (role.start, role.end, role.domain)))


class HistoryQuoteSession:
    __slots__ = ('_authority', '_market', '_snapshot', '_entries', '_entries_sha', '_owner',
                 '_workspace', '_session_hash', '_expires', '_binding_sha', '_public_sha', '_literals', '_literals_sha')

    def __init__(self, market, snapshot, entries, *, allowed_literals=(), _authority=None):
        if _authority is not _ISSUER:
            raise history._error('HISTORY_CONTEXT_REQUIRED', '历史引文必须由本轮宿主发行。', 403)
        values = dict(_authority=_authority, _market=market, _snapshot=snapshot, _entries=tuple(entries),
                      _owner=snapshot._owner, _workspace=snapshot._workspace,
                      _session_hash=snapshot._session_hash, _expires=snapshot._expires,
                      _binding_sha=snapshot._binding_sha, _public_sha=snapshot._public_sha,
                      _literals=tuple(allowed_literals))
        values['_entries_sha'] = history.digest([asdict(q) for q in values['_entries']])
        values['_literals_sha'] = history.digest(values['_literals'])
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, *args):
        raise TypeError('immutable_host_history_quote_session')

    def __deepcopy__(self, memo):
        return self

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def entries(self):
        return self._entries

    @property
    def requested(self):
        return deepcopy(self._snapshot['requested'])

    @property
    def allowed_literals(self):
        return self._literals


class QuoteProof:
    __slots__ = ('_authority', '_session', '_selected', '_spans', '_evidence_ids', '_final_json',
                 '_final_sha', '_text_sha', '_owner', '_workspace', '_session_hash', '_expires')

    def __init__(self, session, selected, spans, evidence_ids, final, *, _authority=None):
        if _authority is not _ISSUER:
            raise history._error('HISTORY_QUOTE_INVALID', '历史答复证明必须由宿主发行。', 403)
        serialized = history.packed(final)
        values = dict(_authority=_authority, _session=session, _selected=tuple(selected), _spans=tuple(spans),
                      _evidence_ids=tuple(evidence_ids), _final_json=serialized,
                      _final_sha=hashlib.sha256(serialized.encode()).hexdigest(),
                      _text_sha=hashlib.sha256(final['text'].encode()).hexdigest(),
                      _owner=session._owner, _workspace=session._workspace,
                      _session_hash=session._session_hash, _expires=session._expires)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, *args):
        raise TypeError('immutable_host_history_quote_proof')

    def __deepcopy__(self, memo):
        return self

    @property
    def final(self):
        return json.loads(self._final_json)

    @property
    def spans(self):
        return self._spans

    @property
    def numeric_roles(self):
        return tuple(HistoricalNumericRole(role.domain, start + role.start, start + role.end, role.text)
                     for quote, (start, unused_end) in zip(self._selected, self._spans)
                     for role in quote.numeric_roles)


def _db_identity(market, db):
    try:
        rows = db.execute('PRAGMA database_list').fetchall()
        paths = [Path(row[2]).resolve(strict=True) for row in rows if row[1] == 'main']
        expected = history._market_identity(market)
        if len(paths) != 1 or str(paths[0]) != expected['path']:
            raise ValueError()
        stat = paths[0].stat()
        if (stat.st_dev, stat.st_ino) != (expected['device'], expected['inode']):
            raise ValueError()
    except Exception:
        raise history._error('HISTORY_DATABASE_MISMATCH', '历史回查连接不属于本轮原始来源。', 409) from None


@contextmanager
def _read_context(market, db=None):
    if db is not None:
        _db_identity(market, db)
        yield db
        return
    with market.db() as connection:
        _db_identity(market, connection)
        if connection.in_transaction:
            raise history._error('HISTORY_DATABASE_MISMATCH', '历史读取事务状态无效。', 409)
        connection.execute('BEGIN')
        try:
            yield connection
        finally:
            connection.rollback()


def _check_session(session, market, *, db):
    if (type(session) is not HistoryQuoteSession or getattr(session, '_authority', None) is not _ISSUER
            or session._market is not market or type(session._snapshot) is not history.HostHistorySnapshot
            or session._owner is not session._snapshot._owner or session._workspace != session._snapshot._workspace
            or session._session_hash != session._snapshot._session_hash or session._expires != session._snapshot._expires
            or session._binding_sha != session._snapshot._binding_sha or session._public_sha != session._snapshot._public_sha
            or session._expires <= time.time() or len(session._entries) > MAX_SELECTION
            or any(type(q) is not HistoricalQuote for q in session._entries)
            or len({q.quote_id for q in session._entries}) != len(session._entries)
            or type(session._literals) is not tuple or any(type(value) is not str for value in session._literals)
            or history.digest(session._literals) != session._literals_sha
            or history.digest([asdict(q) for q in session._entries]) != session._entries_sha):
        raise history._error('HISTORY_CONTEXT_REQUIRED', '历史引文不属于当前受信会话或已失效。', 403)
    history.assert_snapshot(market, session._snapshot, db=db)


def check_raw_sensitive(boundary, text):
    if type(text) is not str:
        raise history._error('HISTORY_QUOTE_INVALID', '历史原文格式无效。', 422)
    redactions = boundary._counts['redactions']
    checked = boundary._text(text)
    if boundary._counts['redactions'] != redactions or checked != unicodedata.normalize('NFKC', text):
        from .data_boundary import BoundaryError
        raise BoundaryError('BOUNDARY_OUTPUT')


def issue_quotes(market, snapshot):
    from .data_boundary import CloudDataBoundary
    entries = []
    with _read_context(market) as db:
        history.assert_snapshot(market, snapshot, db=db)
        packet = history.read_exact(market, snapshot['requested'], expected_binding=snapshot._binding, db=db)
        allowed_literals = history.render_literals(packet)
        bindings = snapshot._binding['receipt']['dependencies']['article_bindings']
        for member in bindings[:MAX_SELECTION]:
            row = db.execute('SELECT body FROM main.market_articles WHERE id=? AND version=?',
                             (member['article_id'], member['version'])).fetchone()
            if row is None or hashlib.sha256(row['body'].encode()).hexdigest() != member['article_body_sha256']:
                raise history._error()
            body = json.loads(row['body'])
            text = _short_complete_unit(body.get('evidence_text'))
            if text is None:
                continue
            check_raw_sensitive(CloudDataBoundary(), text)
            fields = tuple(body.get(key) for key in ('publisher', 'published_at', 'url', 'identity'))
            if any(type(value) is not str or not value for value in fields):
                raise history._error('HISTORY_QUOTE_INVALID', '历史引文来源字段不完整。', 422)
            entries.append(HistoricalQuote('hq_' + uuid.uuid4().hex, member['article_id'], member['version'],
                member['article_body_sha256'], member['source_id'], 0, len(text), text, *fields, numeric_roles(text)))
    return HistoryQuoteSession(market, snapshot, entries, allowed_literals=allowed_literals, _authority=_ISSUER)


def _reread(market, session, quote, *, db):
    if type(quote) is not HistoricalQuote or not any(quote is member for member in session.entries):
        raise history._error('HISTORY_QUOTE_INVALID', '引文不属于本轮原始发行集合。', 422)
    members = session.snapshot._binding['receipt']['dependencies']['article_bindings']
    matches = [member for member in members if member['article_id'] == quote.article_id and member['version'] == quote.version
               and member['article_body_sha256'] == quote.body_sha256 and member['source_id'] == quote.source_id]
    if len(matches) != 1:
        raise history._error()
    row = db.execute('SELECT body FROM main.market_articles WHERE id=? AND version=?',
                     (quote.article_id, quote.version)).fetchone()
    if row is None or hashlib.sha256(row['body'].encode()).hexdigest() != quote.body_sha256:
        raise history._error()
    body = json.loads(row['body'])
    original = body.get('evidence_text')
    if (type(original) is not str or type(quote.start) is not int or type(quote.end) is not int
            or quote.start != 0 or not 0 < quote.end <= len(original)
            or original[quote.start:quote.end] != quote.text or _short_complete_unit(original) != quote.text
            or tuple(body.get(key) for key in ('publisher', 'published_at', 'url', 'identity')) !=
               (quote.publisher, quote.published_at, quote.source_url, quote.identity)
            or quote.numeric_roles != numeric_roles(quote.text)
            or any(role.domain not in DOMAINS for role in quote.numeric_roles)):
        raise history._error()
    return quote.text


def quote_ids(session):
    if type(session) is not HistoryQuoteSession or getattr(session, '_authority', None) is not _ISSUER:
        raise history._error('HISTORY_CONTEXT_REQUIRED', '历史引文需要本轮宿主绑定。', 403)
    return tuple(quote.quote_id for quote in session.entries)


def public_quotes(session, market):
    with _read_context(market) as db:
        _check_session(session, market, db=db)
        return [{'quote_id':q.quote_id, 'text':_reread(market, session, q, db=db)} for q in session.entries]


def public_quote_contract(session, market, *, db=None):
    """只读来源/词面证据；不携带宿主凭据，也不授予任何数值业务资格。"""
    with _read_context(market, db) as connection:
        _check_session(session, market, db=connection)
        rows = []
        for quote in session.entries:
            _reread(market, session, quote, db=connection)
            rows.append({'quote_id':quote.quote_id, 'article_id':quote.article_id, 'version':quote.version,
                'source_id':quote.source_id, 'body_sha256':quote.body_sha256, 'start':quote.start, 'end':quote.end,
                'text':quote.text, 'publisher':quote.publisher, 'published_at':quote.published_at,
                'url':quote.source_url, 'identity':quote.identity,
                'numeric_roles':[asdict(role) for role in quote.numeric_roles]})
        return rows


def _canonical(session, selected, evidence_ids):
    text, spans, claims = INTRO, [], []
    request = session.snapshot['requested']
    for quote in selected:
        context = '所选文章' if request['kind'] == 'article' else '所选历史周报 ' + request['id']
        text += ('\n' + context + '第' + str(request['version']) + '版引用：' + quote.publisher
                 + '；原发布日期 ' + quote.published_at + '；原文第' + str(quote.version) + '版。\n“')
        start = len(text)
        text += quote.text
        spans.append((start, len(text)))
        text += '”'
        claims.append({'text':'“' + quote.text + '”', 'evidence_ids':list(evidence_ids)})
    text += '\n' + NOTICE
    return {'text':text, 'claims':claims, 'followups':[], 'draft_ids':[], 'widget_ids':[],
            'amount_claims':[], 'ratio_claims':[]}, tuple(spans)


def compose(market, session, final, boundary, evidence_ids):
    if type(session) is not HistoryQuoteSession or getattr(session, '_authority', None) is not _ISSUER:
        raise history._error('HISTORY_CONTEXT_REQUIRED', '历史引文需要本轮宿主绑定。', 403)
    if type(final) is not dict or set(final) - {'text','claims','draft_ids','widget_ids','followups',
            'amount_claims','ratio_claims','historical_quote_ids','interaction'}:
        raise history._error('HISTORY_QUOTE_INVALID', '历史答复格式无效。', 422)
    for field in ('draft_ids','widget_ids','amount_claims','ratio_claims'):
        if field in final and (type(final[field]) is not list or final[field]):
            raise history._error('HISTORY_READ_ONLY', '历史引文不具备当前金融或执行资格。', 422)
    if final.get('interaction') is not None:
        raise history._error('HISTORY_READ_ONLY', '历史引文不能生成当前交互操作。', 422)
    ids = final.get('historical_quote_ids', [])
    if (type(ids) is not list or len(ids) > MAX_SELECTION or any(type(value) is not str for value in ids)
            or len(ids) != len(set(ids))):
        raise history._error('HISTORY_QUOTE_INVALID', '历史引文选择无效。', 422)
    if (type(evidence_ids) not in (list, tuple) or len(evidence_ids) > 48
            or any(type(value) is not str or not value for value in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids)) or (ids and not evidence_ids)):
        raise history._error('HISTORY_QUOTE_INVALID', '历史引文缺少本轮实际读取证据。', 422)
    if (type(final.get('text')) is not str or type(final.get('claims', [])) is not list
            or type(final.get('followups', [])) is not list
            or any(type(claim) is not dict or set(claim) != {'text', 'evidence_ids'}
                   or type(claim['text']) is not str or type(claim['evidence_ids']) is not list
                   or any(type(value) is not str for value in claim['evidence_ids'])
                   for claim in final.get('claims', []))
            or any(type(value) is not str for value in final.get('followups', []))):
        raise history._error('HISTORY_QUOTE_INVALID', '历史答复结构无效。', 422)
    check_raw_sensitive(boundary, final['text'])
    for claim in final.get('claims', []):
        check_raw_sensitive(boundary, claim['text'])
    for followup in final.get('followups', []):
        check_raw_sensitive(boundary, followup)
    history.require_safe_rendering(final, session.snapshot['requested'], session.allowed_literals)
    with _read_context(market) as db:
        _check_session(session, market, db=db)
        by_id = {quote.quote_id:quote for quote in session.entries}
        if any(identifier not in by_id for identifier in ids):
            raise history._error('HISTORY_QUOTE_INVALID', '引文不属于本轮受信历史版本。', 422)
        selected = tuple(by_id[identifier] for identifier in ids)
        for quote in selected:
            check_raw_sensitive(boundary, _reread(market, session, quote, db=db))
        if not selected:
            new_final = deepcopy(final)
            new_final.pop('historical_quote_ids', None)
            check_raw_sensitive(boundary, new_final.get('text', ''))
            for claim in new_final.get('claims', []):
                check_raw_sensitive(boundary, claim.get('text', ''))
            for followup in new_final.get('followups', []):
                check_raw_sensitive(boundary, followup)
            return new_final, None
        new_final, spans = _canonical(session, selected, evidence_ids)
        check_raw_sensitive(boundary, new_final['text'])
        return new_final, QuoteProof(session, selected, spans, evidence_ids, new_final, _authority=_ISSUER)


def _assert_answer_db(market, session, proof, final, db):
    _check_session(session, market, db=db)
    if (type(proof) is not QuoteProof or getattr(proof, '_authority', None) is not _ISSUER or proof._session is not session
            or proof._owner is not session._owner or proof._workspace != session._workspace
            or proof._session_hash != session._session_hash or proof._expires != session._expires
            or not 1 <= len(proof._selected) <= MAX_SELECTION
            or len({quote.quote_id for quote in proof._selected}) != len(proof._selected)
            or len(proof._selected) != len(proof._spans)):
        raise history._error('HISTORY_QUOTE_INVALID', '历史答复证明不属于本轮发行集合。', 403)
    for quote in proof._selected:
        _reread(market, session, quote, db=db)
    expected, spans = _canonical(session, proof._selected, proof._evidence_ids)
    serialized = history.packed(expected)
    if (serialized != proof._final_json or hashlib.sha256(serialized.encode()).hexdigest() != proof._final_sha
            or hashlib.sha256(expected['text'].encode()).hexdigest() != proof._text_sha or spans != proof._spans
            or (final is not None and (type(final) is not dict or history.packed(final) != serialized))):
        raise history._error('HISTORY_QUOTE_CHANGED', '历史原文或答复发生变化，请重新查阅。', 409)
    previous = -1
    for quote, (start, end) in zip(proof._selected, proof._spans):
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(expected['text'])
                or start < previous or expected['text'][start:end] != quote.text):
            raise history._error('HISTORY_QUOTE_CHANGED', '历史引文区间发生变化。', 409)
        previous = end
    return proof._spans


def assert_answer(market, session, proof, final=None, *, db=None):
    with _read_context(market, db) as connection:
        return _assert_answer_db(market, session, proof, final, connection)


def seal_snapshot(market, session, proof):
    with _read_context(market) as db:
        _assert_answer_db(market, session, proof, None, db)
        snapshot = session.snapshot
        with history._CACHE_LOCK:
            old = history._CACHE.get(snapshot['snapshot_id'])
            if (old is None or old['snapshot'] is not snapshot or old['market'] is not market
                    or old['owner'] is not session._owner or old['workspace'] != session._workspace
                    or old['session_hash'] != session._session_hash or old['expires'] <= time.monotonic()
                    or snapshot._expires <= time.time()):
                raise history._error()
            if len(history._CACHE) >= history._MAX_SNAPSHOTS:
                raise history._error('HISTORY_SNAPSHOT_CAPACITY', '历史复核容量已满。', 503)
            value = dict(snapshot)
            value['snapshot_id'] = uuid.uuid4().hex
            issued = history.HostHistorySnapshot(value, snapshot._binding, owner=session._owner,
                workspace=session._workspace, session_hash=session._session_hash,
                expires_at=session._expires, _authority=history._AUTHORITY)
            history._CACHE[value['snapshot_id']] = {**old, 'snapshot':issued,
                'history_quote_session':session, 'history_quote_answer':proof}
            return issued


def finalize_review_projection(boundary, raw_packet, already_projected, market, session, proof, *, db=None):
    """仅恢复已验 canonical 文本；别名和其它投影字段逐字保留。

    调用方可传同一 market 连接；本函数不证明调用方持有银行或市场写锁。
    """
    if type(raw_packet) is not dict or type(already_projected) is not dict:
        raise history._error('HISTORY_QUOTE_CHANGED', '历史终审结构无效。', 409)
    assert_answer(market, session, proof, raw_packet.get('draft'), db=db)
    canonical = proof.final
    check_raw_sensitive(boundary, canonical['text'])
    projected = already_projected.get('draft')
    if (type(raw_packet.get('draft')) is not dict or set(raw_packet['draft']) != set(canonical)
            or type(projected) is not dict or set(projected) != set(canonical)
            or type(projected.get('claims')) is not list or len(projected['claims']) != len(canonical['claims'])
            or projected.get('text') not in (canonical['text'], boundary._text(canonical['text']))):
        raise history._error('HISTORY_QUOTE_CHANGED', '完整历史引文未进入终审。', 409)
    for name in set(canonical) - {'text', 'claims'}:
        if history.packed(projected[name]) != history.packed(canonical[name]):
            raise history._error('HISTORY_READ_ONLY', '历史终审包含当前金融或执行内容。', 409)
    for original, claim in zip(canonical['claims'], projected['claims']):
        check_raw_sensitive(boundary, original['text'])
        aliases = [boundary._forward.get(identifier) for identifier in original['evidence_ids']]
        if (type(claim) is not dict or set(claim) != {'text', 'evidence_ids'} or any(alias is None for alias in aliases)
                or claim['evidence_ids'] != aliases
                or any(boundary._reverse.get(alias) != identifier for alias, identifier in zip(aliases, original['evidence_ids']))
                or claim['text'] not in (original['text'], boundary._text(original['text']))):
            raise history._error('HISTORY_QUOTE_CHANGED', '历史引文或原证据别名已改变。', 409)
    result = deepcopy(already_projected)
    result['draft']['text'] = canonical['text']
    for original, claim in zip(canonical['claims'], result['draft']['claims']):
        claim['text'] = original['text']
    return result


def review_projection(boundary, packet, market, session, proof, **kwargs):
    projected = boundary.review_packet(packet, **kwargs)
    return finalize_review_projection(boundary, packet, projected, market, session, proof)


def tts_text(market, session, proof, text, *, db=None):
    assert_answer(market, session, proof, db=db)
    if type(text) is not str or text != proof.final['text']:
        raise history._error('HISTORY_QUOTE_CHANGED', '历史答复与播报原文不一致。', 409)
    return text
