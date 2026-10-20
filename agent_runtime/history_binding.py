"""Host-only exact historical market reads; no model-configurable authority.

Original receipts and provenance stay in local page entries/cache. Public
snapshots contain an opaque identifier only. Restart/expiry/eviction fail closed.
Exact historical public quotations may render numeric words through the separate
issuer-bound quotation module; historical turns cannot call financial/action tools.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from urllib.parse import unquote
from simulated_bank import BankError

MODE = 'historical_audit'
CONTRACT = 'navren.market-history-audit.v1'
PROVIDER = 'navren.public-market-history.v1'
_AUTHORITY = object()
_CACHE_LOCK = threading.RLock()
_CACHE = {}
_MAX_SNAPSHOTS = 256
_CACHE_SECONDS = 600
_HISTORY_SEND_SECONDS = 10
_REQUEST_KEYS = {'kind', 'id', 'version', 'reading_mode'}
_PUBLIC_FIELDS = frozenset('''id period version title title_cn headline publisher source_id identity url
 published_at published_precision reported_at released_at read_scope link_status access_status
 evidence_excerpt excerpt_notice summary changes heading text conditions transmission
 period_start period_end_exclusive as_of facts quote uncertainty observation_notice
 schedule_notice limitations article_ids article_versions citations event_id event_version
 source_articles supporting_events articles report article status'''.split())
_META_FIELDS = frozenset('''id period version title title_cn headline publisher source_id identity url
 published_at published_precision reported_at released_at read_scope link_status access_status'''.split())

def _error(code='STALE_HISTORY_EVIDENCE', message='历史资料、来源许可或选定版本已变化，请重新选择后查阅。', status=409):
    return BankError(code, message, status)

def packed(value):
    # Exact helper15c format, including default separators.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()

def checked_request(value):
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS or value.get('reading_mode') != MODE:
        raise _error('HISTORY_SELECTION_INVALID', '历史复核需要明确资料身份、版本及查阅模式。', 400)
    kind, ident, version = value['kind'], value['id'], value['version']
    if kind not in ('article', 'weekly') or type(version) is not int or not 1 <= version <= 999999999:
        raise _error('HISTORY_SELECTION_INVALID', '历史版本必须是明确的正整数。', 400)
    if not isinstance(ident, str) or not 1 <= len(ident) <= 128:
        raise _error('HISTORY_SELECTION_INVALID', '历史资料身份无效。', 400)
    if kind == 'article':
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', ident):
            raise _error('HISTORY_SELECTION_INVALID', '历史文章身份无效。', 400)
    else:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ident):
                raise ValueError()
            date.fromisoformat(ident)
        except ValueError:
            raise _error('HISTORY_SELECTION_INVALID', '历史周报期号无效。', 400) from None
    return deepcopy(value)

def parse_route(path, query):
    if not isinstance(query, dict) or query:
        raise _error('HISTORY_QUERY_INVALID', '历史版本路径不接受额外筛选或调用方凭据。', 400)
    match = re.fullmatch(r'/api/market/history/(articles|weeklies)/([^/]+)/versions/([1-9][0-9]{0,8})', path)
    if match is None:
        raise _error('HISTORY_SELECTION_INVALID', '历史资料路径或版次无效。', 400)
    return checked_request({'kind': {'articles': 'article', 'weeklies': 'weekly'}[match[1]],
        'id': unquote(match[2]), 'version': int(match[3]), 'reading_mode': MODE})

def requested_history(reference):
    return (isinstance(reference, dict) and isinstance(reference.get('parameters'), dict)
            and 'reading_mode' in reference['parameters'])

def _market_identity(market):
    try:
        path = Path(market.path).resolve(strict=True)
        info = path.stat()
        if not path.is_file():
            raise ValueError()
    except (OSError, AttributeError, TypeError, ValueError):
        raise _error('MARKET_HISTORY_UNAVAILABLE', '历史资料提供端身份暂时无法核对。', 503) from None
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino}

def verify_packet(packet, request):
    wanted = checked_request(request)
    if (not isinstance(packet, dict) or packet.get('contract') != CONTRACT
            or packet.get('requested') != wanted or packet.get('reading_mode') != MODE
            or packet.get('historical_only') is not True or packet.get('current_evidence_eligible') is not False):
        raise _error()
    if packet.get('status') == 'history_unavailable':
        if packet.get('receipt') is not None:
            raise _error()
        return wanted
    receipt = packet.get('receipt')
    if (packet.get('status') != 'historical_readable' or packet.get('execution_authority') is not False
            or not isinstance(packet.get('data'), dict) or not isinstance(receipt, dict)
            or receipt.get('requested') != wanted or receipt.get('result_sha256') != digest(packet['data'])):
        raise _error()
    return wanted

def make_local_binding(market, packet, *, db=None):
    wanted = verify_packet(packet, packet.get('requested'))
    if packet['status'] != 'historical_readable':
        raise _error(packet.get('reason') or 'HISTORY_UNAVAILABLE', '该历史版本当前不可用于解读，请重新核对来源状态。')
    binding = {'requested': wanted, 'receipt': deepcopy(packet['receipt']), 'market_identity': _market_identity(market)}
    assert_local_binding(market, binding, db=db)
    return binding

def assert_local_binding(market, binding, *, db=None):
    if not isinstance(binding, dict) or set(binding) != {'requested', 'receipt', 'market_identity'}:
        raise _error()
    wanted = checked_request(binding['requested'])
    if binding['market_identity'] != _market_identity(market) or not isinstance(binding['receipt'], dict):
        raise _error()
    if binding['receipt'].get('requested') != wanted:
        raise _error()
    from public_market_history import HistoryError
    try:
        market.assert_history_receipt(binding['receipt'], db=db)
    except HistoryError as error:
        raise _error(error.code, '历史原文、依赖版本或来源许可已变化，请重新选择。', error.status) from error
    return True

def read_exact(market, request, *, expected_binding=None, db=None):
    wanted = checked_request(request)
    if expected_binding is not None:
        assert_local_binding(market, expected_binding, db=db)
        if expected_binding['requested'] != wanted:
            raise _error('HISTORY_SELECTION_CONFLICT', '工具对象与本轮选定的历史版本不一致。')
    from public_market_history import HistoryError
    try:
        if wanted['kind'] == 'article':
            packet = market.history_article(wanted['id'], wanted['version'], reading_mode=MODE, db=db)
        else:
            packet = market.history_weekly(wanted['id'], wanted['version'], reading_mode=MODE, db=db)
    except HistoryError as error:
        raise _error(error.code, '指定历史版本无法核对；本次不会替换为最新资料。', error.status) from error
    verify_packet(packet, wanted)
    if expected_binding is not None and packet.get('receipt') != expected_binding['receipt']:
        raise _error()
    return deepcopy(packet)

def public_view(packet):
    wanted = verify_packet(packet, packet.get('requested'))
    result = {'contract': CONTRACT, 'requested': wanted, 'reading_mode': MODE,
        'historical_only': True, 'current_evidence_eligible': False, 'execution_authority': False,
        'historical_numeric_rendering_supported': False, 'status': packet['status'],
        'notice': '仅解读明确选定的历史版本，不代表当前事实或可投金额；历史数字声明资格尚未开放。'}
    if packet['status'] == 'history_unavailable':
        result['reason'] = packet.get('reason')
        metadata = packet.get('metadata', {})
        result['metadata'] = {key: deepcopy(value) for key, value in metadata.items()
            if key in _META_FIELDS and isinstance(value, (str, int)) and not isinstance(value, bool)}
        return result
    def project(value, field=''):
        if isinstance(value, dict):
            if field == 'article_versions':
                if not 1 <= len(value) <= 48 or any(not isinstance(key, str)
                        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', key)
                        or type(item) is not int or not 1 <= item <= 999999999 for key, item in value.items()):
                    raise _error('HISTORY_PUBLIC_SHAPE_INVALID', '历史来源版次无法核对。', 503)
                return deepcopy(value)
            return {key: project(item, key) for key, item in value.items() if key in _PUBLIC_FIELDS}
        if isinstance(value, list):
            return [project(item) for item in value]
        if value is None or type(value) in (str, int, bool):
            return value
        raise _error('HISTORY_PUBLIC_SHAPE_INVALID', '历史正文格式无法安全展示。', 503)
    result.update(project(packet['data']))
    if wanted['kind'] == 'article':
        result['notice'] += '单篇旧文章的原许可代次未知，仅核验本次当前许可。'
    return result

@contextmanager
def history_publication_guard(market, packet, binding):
    """Public GET lease: same original receipt, market barrier, actual socket checks.

    The initial read is already over; taking a new barrier must validate that
    original read rather than silently issue a fresh receipt. No bank/session
    authority substitutes for the public source's current permission.
    """
    from finance_gateway import _publication_checkpoint, _publication_remaining
    wanted = verify_packet(packet, packet.get('requested'))
    original_sha = digest(packet)
    if (packet['status'] == 'historical_readable') != (binding is not None):
        raise _error()
    deadline = time.monotonic() + _HISTORY_SEND_SECONDS
    checkpoint_token = remaining_token = None
    with market.db() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            forbidden = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                         sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE}
            db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK)
            def checkpoint():
                if deadline <= time.monotonic():
                    raise _error('HISTORY_PUBLICATION_EXPIRED', '历史资料展示已超过本次期限，请重新查阅。')
                fresh = read_exact(market, wanted, expected_binding=binding, db=db)
                if digest(fresh) != original_sha:
                    raise _error()
                if deadline <= time.monotonic():
                    raise _error('HISTORY_PUBLICATION_EXPIRED', '历史资料展示已超过本次期限，请重新查阅。')
            checkpoint()
            checkpoint_token = _publication_checkpoint.set(checkpoint)
            remaining_token = _publication_remaining.set(lambda: deadline - time.monotonic())
            yield
        finally:
            if remaining_token is not None: _publication_remaining.reset(remaining_token)
            if checkpoint_token is not None: _publication_checkpoint.reset(checkpoint_token)
            # An empty read-only transaction must not commit after success bytes.
            db.rollback()

class HostHistoryContext(dict):
    """In-memory host-issued immutable mapping; JSON copies cannot regain authority."""
    def __init__(self, value, binding, *, owner, workspace, session_hash, expires_at, _authority):
        if _authority is not _AUTHORITY:
            raise _error('HISTORY_CONTEXT_REQUIRED', '需要本次已验证页面票据的历史引用。', 403)
        dict.__init__(self, deepcopy(value))
        object.__setattr__(self, '_binding', deepcopy(binding))
        object.__setattr__(self, '_owner', owner)
        object.__setattr__(self, '_workspace', workspace)
        object.__setattr__(self, '_session_hash', session_hash)
        object.__setattr__(self, '_expires', expires_at)
        object.__setattr__(self, '_public_sha', digest(dict(self)))
        object.__setattr__(self, '_binding_sha', digest(binding))
        object.__setattr__(self, '_authority', _authority)
    def __setattr__(self, name, value): raise TypeError('immutable_host_history_context')
    def __setitem__(self, key, value): raise TypeError('immutable_host_history_context')
    def __delitem__(self, key): raise TypeError('immutable_host_history_context')
    def clear(self): raise TypeError('immutable_host_history_context')
    def pop(self, *args): raise TypeError('immutable_host_history_context')
    def popitem(self): raise TypeError('immutable_host_history_context')
    def setdefault(self, *args): raise TypeError('immutable_host_history_context')
    def update(self, *args, **kwargs): raise TypeError('immutable_host_history_context')
    def __ior__(self, other): raise TypeError('immutable_host_history_context')
    def __getitem__(self, key): return deepcopy(dict.__getitem__(self, key))
    def get(self, key, default=None): return deepcopy(dict.get(self, key, default))
    def items(self): return [(key, deepcopy(value)) for key, value in dict.items(self)]
    def values(self): return [deepcopy(value) for value in dict.values(self)]
    def __deepcopy__(self, memo): return self

def issue_host_context(value, binding, *, owner, workspace, session_hash, expires_at):
    # Only authenticated PageContexts.consume calls this function; no public tool.
    return HostHistoryContext(value, binding, owner=owner, workspace=workspace, session_hash=session_hash,
                              expires_at=expires_at, _authority=_AUTHORITY)

def host_binding(reference, *, owner, workspace, market):
    if (type(reference) is not HostHistoryContext or reference._authority is not _AUTHORITY
            or reference._owner is not owner or reference._workspace != workspace
            or reference._expires <= time.time() or reference._public_sha != digest(dict(reference))
            or reference._binding_sha != digest(reference._binding)):
        raise _error('HISTORY_CONTEXT_REQUIRED', '历史引用未由当前页面票据消费生成，或已失效。', 403)
    sessions = getattr(getattr(owner, 'g', None), 'demo_sessions', None)
    request_var = getattr(sessions, '_request', None)
    active = request_var.get() if request_var is not None else None
    if (not isinstance(active, tuple) or len(active) != 2 or active[1] != workspace
            or not isinstance(active[0], str) or hashlib.sha256(active[0].encode()).hexdigest() != reference._session_hash):
        raise _error('HISTORY_CONTEXT_REQUIRED', '历史引用不属于本次已认证会话。', 403)
    sessions.validate(active[0], workspace)
    binding = deepcopy(reference._binding)
    wanted = checked_request(binding['requested'])
    if (reference.get('kind') != wanted['kind'] or reference.get('id') != wanted['id']
            or type(reference.get('version')) is not int or reference.get('version') != wanted['version']
            or reference.get('parameters') != {'reading_mode': MODE}
            or reference.get('source_scope') != 'market' or reference.get('execution_authority') is not False):
        raise _error()
    assert_local_binding(market, binding)
    return binding

def bound_arguments(request, name, args):
    wanted = checked_request(request)
    field = 'article_id' if wanted['kind'] == 'article' else 'period'
    if (name != 'market_' + wanted['kind'] or not isinstance(args, dict)
            or set(args) - {field} or args.get(field, wanted['id']) != wanted['id']):
        raise _error('HISTORY_SELECTION_CONFLICT', '本轮只能读取所选历史对象与版本。')
    return {field: wanted['id']}

class HostHistorySnapshot(HostHistoryContext):
    """Same immutable mechanics, separate exact type; wire JSON is never authority."""

def register_snapshot(market, binding, reference):
    assert_local_binding(market, binding)
    now = time.monotonic()
    token = uuid.uuid4().hex
    with _CACHE_LOCK:
        expired = [key for key, item in _CACHE.items() if item['expires'] <= now]
        for key in expired: _CACHE.pop(key, None)
        if len(_CACHE) >= _MAX_SNAPSHOTS:
            raise _error('HISTORY_SNAPSHOT_CAPACITY', '历史复核任务已达容量，请稍后重新选择资料。', 503)
        value = {'schema_version': 2, 'provider_id': PROVIDER,
                 'requested': deepcopy(binding['requested']), 'snapshot_id': token}
        snapshot = HostHistorySnapshot(value, binding, owner=reference._owner,
            workspace=reference._workspace, session_hash=reference._session_hash,
            expires_at=reference._expires, _authority=_AUTHORITY)
        _CACHE[token] = {'market': market, 'binding': deepcopy(binding), 'expires': now + _CACHE_SECONDS,
                         'snapshot': snapshot, 'owner': reference._owner,
                         'workspace': reference._workspace, 'session_hash': reference._session_hash}
    return snapshot

def assert_snapshot(market, snapshot, *, db=None):
    if (type(snapshot) is not HostHistorySnapshot or snapshot._authority is not _AUTHORITY
            or snapshot._public_sha != digest(dict(snapshot))
            or snapshot._binding_sha != digest(snapshot._binding)
            or set(snapshot) != {'schema_version', 'provider_id', 'requested', 'snapshot_id'}
            or type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] != 2
            or snapshot.get('provider_id') != PROVIDER
            or not isinstance(snapshot.get('snapshot_id'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', snapshot['snapshot_id'])):
        raise _error()
    wanted = checked_request(snapshot['requested'])
    with _CACHE_LOCK:
        original = _CACHE.get(snapshot['snapshot_id'])
        if (original is None or original['snapshot'] is not snapshot or original['market'] is not market
                or original['expires'] <= time.monotonic() or snapshot._expires <= time.time()):
            raise _error()
        binding, deadline = deepcopy(original['binding']), original['expires']
    sessions = getattr(getattr(original['owner'], 'g', None), 'demo_sessions', None)
    request_var = getattr(sessions, '_request', None)
    active = request_var.get() if request_var is not None else None
    if active is not None:
        if (not isinstance(active, tuple) or len(active) != 2 or active[1] != original['workspace']
                or not isinstance(active[0], str)
                or hashlib.sha256(active[0].encode()).hexdigest() != original['session_hash']):
            raise _error('HISTORY_CONTEXT_REQUIRED', '历史快照不属于本次已认证会话。', 403)
        sessions.validate(active[0], original['workspace'])
    if binding['requested'] != wanted:
        raise _error()
    # Cache lock is released before DB access; preserve existing publication lock order.
    assert_local_binding(market, binding, db=db)
    if 'history_quote_answer' in original:
        from .historical_quotes import assert_answer
        assert_answer(market,original['history_quote_session'],original['history_quote_answer'],db=db)
    with _CACHE_LOCK:
        current = _CACHE.get(snapshot['snapshot_id'])
        if current is not original or deadline <= time.monotonic():
            raise _error()
    return True

def assert_bank_session(snapshot,bank_db):
    """用调用方持有的原银行连接回查当前宿主身份；不创建连接或事务。"""
    if type(snapshot) is not HostHistorySnapshot or snapshot._authority is not _AUTHORITY:
        raise _error('HISTORY_CONTEXT_REQUIRED','历史引文缺少本轮受信快照。',403)
    gateway=getattr(snapshot._owner,'g',None)
    bank=getattr(gateway,'bank',None)
    sessions=getattr(gateway,'demo_sessions',None)
    active=sessions._request.get() if sessions is not None else None
    if (bank is None or not isinstance(active,tuple) or len(active)!=2
            or active[1]!=snapshot._workspace or type(active[0]) is not str
            or hashlib.sha256(active[0].encode()).hexdigest()!=snapshot._session_hash
            or snapshot._expires<=time.time()):
        raise _error('HISTORY_CONTEXT_REQUIRED','历史引文不属于当前已认证会话。',403)
    try:
        expected=Path(bank.database).resolve(strict=True)
        paths=[Path(row[2]).resolve(strict=True) for row in bank_db.execute('PRAGMA database_list')
               if row[1]=='main']
        if len(paths)!=1 or paths[0]!=expected or paths[0].stat().st_ino!=expected.stat().st_ino:
            raise ValueError()
    except Exception:
        raise _error('HISTORY_DATABASE_MISMATCH','历史身份回查未使用原银行连接。',409) from None
    hashed=snapshot._session_hash
    session=bank_db.execute('SELECT workspace_id,revoked,expires_at FROM demo_sessions WHERE token_hash=?',
                            (hashed,)).fetchone()
    if session is not None:
        if (session['workspace_id']!=snapshot._workspace or session['revoked']
                or session['expires_at']<=bank.clock()):
            raise _error('HISTORY_CONTEXT_REQUIRED','历史引文账户会话已失效。',403)
        bank._workspace(bank_db,snapshot._workspace)
    elif not bank_db.execute('SELECT 1 FROM workspaces WHERE id=? AND token_hash=?',
                             (snapshot._workspace,hashed)).fetchone():
        raise _error('HISTORY_CONTEXT_REQUIRED','历史引文的本人会话已失效。',403)
    return True

def render_literals(packet):
    wanted = checked_request(packet['requested'])
    allowed = {wanted['id']}
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('published_at', 'reported_at', 'released_at', 'period_start', 'period_end_exclusive', 'as_of') and isinstance(item, str):
                    try:
                        parsed = datetime.fromisoformat(item) if 'T' in item or ' ' in item else date.fromisoformat(item)
                        day = parsed.date() if isinstance(parsed, datetime) else parsed
                    except ValueError: continue
                    allowed.update({item, day.isoformat(), f'{day.year}年{day.month}月{day.day}日',
                                    f'{day.year}年{day.month:02d}月{day.day:02d}日'})
                if isinstance(item, (dict, list)): collect(item)
        elif isinstance(value, list):
            for item in value: collect(item)
    collect(packet['data'])
    return tuple(sorted(allowed, key=len, reverse=True))

def require_safe_rendering(final, request, allowed):
    if not isinstance(final, dict) or final.get('draft_ids') or final.get('amount_claims'):
        raise _error('HISTORY_READ_ONLY', '历史资料不具备当前财务或执行依据资格。', 422)
    texts = [final.get('text', '')] + [claim.get('text', '') for claim in final.get('claims', []) if isinstance(claim, dict)]
    texts += [text for text in final.get('followups', []) if isinstance(text, str)]
    version = checked_request(request)['version']
    for text in texts:
        if not isinstance(text, str): raise _error('HISTORY_RENDER_INVALID', '历史答复格式无效。', 422)
        remainder = unicodedata.normalize('NFKC', text)
        # Check amount/percentage roles before exempting a verified date/ID:
        # e.g. a numeric article ID or a date must never become "5000元".
        chinese_number = r'[零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億点點]'
        value = r'(?:\d[\d.,\s]*|' + chinese_number + r'(?:' + chinese_number + r'|\s)*)'
        if re.search(value + r'\s*(?:万亿|亿|万)?\s*(?:人民币|美元|欧元|英镑|日元|元|圆|CNY|RMB|USD|EUR|GBP|JPY|%|个百分点|百分点|基点)' , remainder, re.I) or re.search(r'百分之\s*(?:\d|' + chinese_number + ')', remainder):
            raise _error('HISTORY_NUMERIC_ROLE_UNAVAILABLE', '历史金额及比例没有独立声明资格，本轮不能渲染为当前资金或历史数值结论。', 422)
        for literal in allowed:
            remainder = remainder.replace(unicodedata.normalize('NFKC', literal), '')
        remainder = re.sub(r'(?:第\s*' + str(version) + r'\s*版|版本\s*' + str(version) + r'(?!\d)|\b[Vv]' + str(version) + r'\b)', '', remainder)
        if re.search(r'\d|[零〇一二两三四五六七八九十百千万亿]+\s*(?:元|圆|美元|欧元|英镑|日元|人民币)', remainder):
            raise _error('HISTORY_NUMERIC_ROLE_UNAVAILABLE', '历史数字声明资格尚未开放；本轮只解读非数值内容及已绑定日期、版本。', 422)
