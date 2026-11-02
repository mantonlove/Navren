"""SQLite 原文权威库、FTS5 与本地向量混合检索。

写入方法只供受信本地应用调用，不暴露给模型。候选、准入、发布是不同步骤；
撤回和确认修订即时影响已有快照，历史引文仍可按明确时点查询。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Mapping
from urllib.parse import urlsplit,unquote
import uuid

FACETS = frozenset({'fees','liquidity','risk','strategy','eligibility','sale_status',
                    'returns','general','terms'})
MAX_TEXT = 2_000_000
MAX_CHUNKS = 5000
PARSER = 'exact-text/1'
WEBANK_PARSER = 'webank-record-json/1'
PDF_PARSER = 'pdfplumber-text/1'
TOKENIZER = 'cjk-bigrams/1'
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
FACET_LABELS={'fees':'费用费率申购费赎回费','liquidity':'流动性赎回到账','risk':'风险损失',
              'strategy':'策略资产投资','eligibility':'准入资格购买条件','sale_status':'销售在售状态',
              'returns':'收益净值表现','general':'一般资料','terms':'合同条款规则'}
PUBLIC_CONCEPTS={'fees':['费用','费率','申购费','赎回费'], 'liquidity':['流动性','赎回','到账'],
                 'risk':['风险','本金损失'],'strategy':['投资策略','资产配置'],
                 'eligibility':['购买资格','准入条件'],'sale_status':['销售状态','在售'],
                 'returns':['收益','净值'],'general':['公开资料'],'terms':['合同','条款']}


class KnowledgeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def today():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def now():
    return datetime.now(timezone.utc).isoformat()


def default_path():
    return Path(os.getenv('NAVREN_KNOWLEDGE_DB', str(Path.home() / 'Library/Application Support/Navren/public-knowledge.sqlite3')))


def ident(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise KnowledgeError('invalid_identifier')
    return value


def day(value, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise KnowledgeError('invalid_date')
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise KnowledgeError('invalid_date') from None
    return value


def tokens(text):
    """中文二元词及完整拉丁词；原文不改写，不能称为语义检索。"""
    result = []
    for part in re.findall(r'[\u3400-\u9fff]+|[A-Za-z0-9_]+', text.casefold()):
        result.extend([part] if not '\u3400' <= part[0] <= '\u9fff' or len(part) == 1
                      else [part[i:i+2] for i in range(len(part)-1)])
    return list(dict.fromkeys(result))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY NOT NULL,publisher TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('official','licensed')),
 endpoints TEXT NOT NULL CHECK(json_valid(endpoints)),read_allowed INTEGER NOT NULL CHECK(read_allowed IN(0,1)),
 outbound_allowed INTEGER NOT NULL CHECK(outbound_allowed IN(0,1)),enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN(0,1)),parser_id TEXT NOT NULL,shape TEXT,
 freshness TEXT NOT NULL CHECK(json_valid(freshness)),permission_basis TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY NOT NULL,issuer TEXT NOT NULL,scheme TEXT NOT NULL,
 code TEXT NOT NULL,name TEXT NOT NULL,verified INTEGER NOT NULL CHECK(verified IN(0,1)),UNIQUE(issuer,scheme,code));
CREATE TABLE IF NOT EXISTS share_classes(id TEXT PRIMARY KEY NOT NULL,product_id TEXT NOT NULL REFERENCES products(id),
 scheme TEXT NOT NULL,code TEXT NOT NULL,currency TEXT NOT NULL CHECK(length(currency)=3),UNIQUE(scheme,code),UNIQUE(id,product_id));
CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY NOT NULL,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_artifacts(sha256 TEXT PRIMARY KEY NOT NULL,payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY NOT NULL,source_id TEXT NOT NULL REFERENCES sources(id),
 source_url TEXT NOT NULL,document_key TEXT NOT NULL,visibility TEXT NOT NULL CHECK(visibility='public'),data_origin TEXT NOT NULL CHECK(data_origin='real'),
 UNIQUE(source_id,source_url,document_key));
CREATE TABLE IF NOT EXISTS document_versions(
 id TEXT PRIMARY KEY NOT NULL,document_id TEXT NOT NULL REFERENCES documents(id),version_no INTEGER NOT NULL,
 raw_sha256 TEXT NOT NULL REFERENCES raw_artifacts(sha256),title TEXT NOT NULL,parsed_text TEXT NOT NULL,
 parsed_sha256 TEXT NOT NULL,parser_id TEXT NOT NULL,shape TEXT NOT NULL,parse_id TEXT NOT NULL UNIQUE,
 published_on TEXT,retrieved_on TEXT NOT NULL,effective_from TEXT,effective_to TEXT,
 status TEXT NOT NULL CHECK(status IN('candidate','verified','rejected','withdrawn')),
 revoked_from TEXT,verification_basis TEXT,created_at TEXT NOT NULL,
 CHECK(effective_to IS NULL OR effective_from IS NULL OR effective_from<effective_to),UNIQUE(document_id,version_no));
CREATE TABLE IF NOT EXISTS chunks(
 id TEXT PRIMARY KEY NOT NULL,row_id INTEGER NOT NULL UNIQUE,version_id TEXT NOT NULL REFERENCES document_versions(id),
 start_char INTEGER NOT NULL CHECK(start_char>=0),end_char INTEGER NOT NULL CHECK(end_char>start_char),
 excerpt TEXT NOT NULL CHECK(length(excerpt)>0 AND length(excerpt)<=1200),excerpt_sha256 TEXT NOT NULL,
 paragraph TEXT NOT NULL,page INTEGER,product_id TEXT REFERENCES products(id),share_class_id TEXT,channel_id TEXT REFERENCES channels(id),
 facets TEXT NOT NULL CHECK(json_valid(facets)),conditions TEXT NOT NULL CHECK(json_valid(conditions)),
 data_on TEXT,title_terms TEXT NOT NULL,body_terms TEXT NOT NULL,
 FOREIGN KEY(share_class_id,product_id) REFERENCES share_classes(id,product_id));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title_terms,body_terms,content='chunks',content_rowid='row_id',tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
 INSERT INTO chunks_fts(rowid,title_terms,body_terms) VALUES(NEW.row_id,NEW.title_terms,NEW.body_terms); END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
 INSERT INTO chunks_fts(chunks_fts,rowid,title_terms,body_terms) VALUES('delete',OLD.row_id,OLD.title_terms,OLD.body_terms); END;
CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY NOT NULL,manifest_sha256 TEXT NOT NULL,
 temporary INTEGER NOT NULL CHECK(temporary IN(0,1)),created_at TEXT NOT NULL,sealed INTEGER NOT NULL CHECK(sealed IN(0,1)));
CREATE TABLE IF NOT EXISTS release_chunks(release_id TEXT NOT NULL REFERENCES releases(id),chunk_id TEXT NOT NULL REFERENCES chunks(id),
 PRIMARY KEY(release_id,chunk_id));
CREATE TABLE IF NOT EXISTS current_release(scope TEXT PRIMARY KEY NOT NULL CHECK(scope='public'),release_id TEXT NOT NULL REFERENCES releases(id));
CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY NOT NULL,product_id TEXT,share_class_id TEXT,channel_id TEXT,
 facets TEXT NOT NULL CHECK(json_valid(facets)),from_on TEXT NOT NULL,to_on TEXT,active INTEGER NOT NULL CHECK(active IN(0,1)),
 reason TEXT NOT NULL,CHECK(to_on IS NULL OR from_on<to_on));
CREATE TABLE IF NOT EXISTS ingest_jobs(id TEXT PRIMARY KEY NOT NULL,source_id TEXT NOT NULL REFERENCES sources(id),
 public_key TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN('candidate','succeeded','failed','paused')),
 started_at TEXT NOT NULL,ended_at TEXT NOT NULL,error_code TEXT,version_ids TEXT NOT NULL CHECK(json_valid(version_ids)));
CREATE TABLE IF NOT EXISTS checks(version_id TEXT NOT NULL REFERENCES document_versions(id),checked_on TEXT NOT NULL,
 PRIMARY KEY(version_id,checked_on));
CREATE INDEX IF NOT EXISTS versions_document ON document_versions(document_id,version_no);
CREATE INDEX IF NOT EXISTS chunks_subject ON chunks(product_id,share_class_id,channel_id,version_id);
CREATE TRIGGER IF NOT EXISTS immutable_version BEFORE UPDATE ON document_versions
 WHEN NEW.id IS NOT OLD.id OR NEW.document_id IS NOT OLD.document_id OR NEW.version_no IS NOT OLD.version_no
 OR NEW.raw_sha256 IS NOT OLD.raw_sha256 OR NEW.parsed_text IS NOT OLD.parsed_text OR NEW.parsed_sha256 IS NOT OLD.parsed_sha256
 OR NEW.parse_id IS NOT OLD.parse_id OR NEW.parser_id IS NOT OLD.parser_id OR NEW.title IS NOT OLD.title
 OR NEW.published_on IS NOT OLD.published_on OR NEW.retrieved_on IS NOT OLD.retrieved_on
 OR NEW.effective_from IS NOT OLD.effective_from OR NEW.effective_to IS NOT OLD.effective_to OR NEW.shape IS NOT OLD.shape
 BEGIN SELECT RAISE(ABORT,'immutable_document_version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_chunk BEFORE UPDATE ON chunks BEGIN SELECT RAISE(ABORT,'immutable_chunk'); END;
CREATE TRIGGER IF NOT EXISTS immutable_raw BEFORE UPDATE ON raw_artifacts BEGIN SELECT RAISE(ABORT,'immutable_raw'); END;
CREATE TRIGGER IF NOT EXISTS immutable_release BEFORE UPDATE ON releases
 WHEN OLD.sealed=1 OR NEW.id IS NOT OLD.id OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
 OR NEW.temporary IS NOT OLD.temporary OR NEW.created_at IS NOT OLD.created_at OR NEW.sealed!=1
 BEGIN SELECT RAISE(ABORT,'immutable_release'); END;
CREATE TRIGGER IF NOT EXISTS immutable_member BEFORE UPDATE ON release_chunks BEGIN SELECT RAISE(ABORT,'immutable_release_member'); END;
CREATE TRIGGER IF NOT EXISTS sealed_member_insert BEFORE INSERT ON release_chunks
 WHEN (SELECT sealed FROM releases WHERE id=NEW.release_id)=1 BEGIN SELECT RAISE(ABORT,'sealed_release_member'); END;
CREATE TRIGGER IF NOT EXISTS sealed_member_delete BEFORE DELETE ON release_chunks
 WHEN (SELECT sealed FROM releases WHERE id=OLD.release_id)=1 BEGIN SELECT RAISE(ABORT,'sealed_release_member'); END;
CREATE TABLE IF NOT EXISTS market_lineage(
 version_id TEXT PRIMARY KEY NOT NULL REFERENCES document_versions(id),binding TEXT NOT NULL CHECK(json_valid(binding)));
CREATE TRIGGER IF NOT EXISTS immutable_market_lineage_update BEFORE UPDATE ON market_lineage
 BEGIN SELECT RAISE(ABORT,'immutable_market_lineage'); END;
CREATE TRIGGER IF NOT EXISTS immutable_market_lineage_delete BEFORE DELETE ON market_lineage
 BEGIN SELECT RAISE(ABORT,'immutable_market_lineage'); END;
PRAGMA user_version=1;
'''


def _market_operation(*, write=False):
    def decorate(method):
        @wraps(method)
        def run(self,*args,**kwargs):
            with self.market_read_snapshot(write=write):
                return method(self,*args,**kwargs)
        return run
    return decorate


class LocalKnowledgeProvider:
    provider_id = 'navren.sqlite.graphrag.v1'

    def __init__(self, path=None, *, release_id=None, encoder=None, market_authority=None):
        self.path = Path(path or default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fixed_release = release_id
        self.vector_encoder = encoder
        self._market_authority = None
        self._market_context = ContextVar('market-authority-'+uuid.uuid4().hex,default=None)
        if market_authority is not None:self.bind_market_authority(market_authority)
        with self.db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('workspaces','user_memories','agent_dialogues','personal_decisions')").fetchone():
                raise KnowledgeError('personal_database_forbidden')
            db.executescript(SCHEMA)
            from .graph import SCHEMA as GRAPH_SCHEMA
            db.executescript(GRAPH_SCHEMA)
            from .vectors import SCHEMA as VECTOR_SCHEMA
            db.executescript(VECTOR_SCHEMA)
            from .structure_index import SCHEMA as STRUCTURE_SCHEMA
            db.executescript(STRUCTURE_SCHEMA)
        self.path.chmod(0o600)
        from .vectors import schedule
        with self.db() as db:
            current = db.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()
        schedule(self, release_id or (current[0] if current else None))

    def bind_market_authority(self,market_store):
        """Trusted bootstrap only: never accept a path or authority from DB/model metadata."""
        from .market_authority import MarketAuthority
        authority=market_store if type(market_store) is MarketAuthority else MarketAuthority(market_store)
        if self._market_authority is not None and self._market_authority.identity!=authority.identity:
            raise KnowledgeError('market_authority_conflict')
        self._market_authority=authority
        return self

    @contextmanager
    def market_read_snapshot(self, *, write=False):
        """One provider operation freezes upstream eligibility. Not a cross-DB read instant.

        Admission uses an upstream IMMEDIATE lock held through KG commit, always
        Market -> KG. Public reads use upstream BEGIN and permit concurrent WAL
        writers; a later call rechecks. No missing binding is auto-discovered.
        """
        current=self._market_context.get()
        if current is not None and (current[1] is None or current[1].reusable()):
            if write and not current[0]:raise KnowledgeError('market_authority_write_scope_required')
            yield current[1];return
        authority=self._market_authority
        if authority is None:
            token=self._market_context.set((write,None))
            try:yield None
            finally:self._market_context.reset(token)
            return
        with authority.snapshot(write=write) as snapshot:
            token=self._market_context.set((write,snapshot))
            try:yield snapshot
            finally:self._market_context.reset(token)

    def _market_available(self,db,row):
        # market-* is a reserved derived-source namespace. Old rows without a
        # lineage cannot be upgraded by copying fields into their existing ID.
        if not (row['source_id'].startswith('market-') or row['shape']=='market-official-full-text-v1'):return True
        scope=self._market_context.get();snapshot=scope[1] if scope else None
        if snapshot is None:return False
        version=row['version_id'] if 'version_id' in row.keys() else row['id']
        lineage=db.execute('SELECT binding FROM market_lineage WHERE version_id=?',(version,)).fetchone()
        if lineage is None:return False
        try:
            binding=json.loads(lineage['binding'])
            document=db.execute('SELECT document_key FROM documents WHERE id=?',(row['document_id'],)).fetchone()
            return bool(snapshot.valid(binding) and row['source_id']=='market-'+binding['source_id']
                        and row['parsed_sha256']==binding['parsed_sha256']
                        and row['source_url']==snapshot.capture(binding['article_id']).article['url']
                        and document and document[0]=='market-'+sha(packed(binding))[:40])
        except (KnowledgeError,ValueError,KeyError,TypeError):return False

    def _market_member_available(self,db,version):
        row=db.execute('''SELECT v.*,d.source_id,d.source_url FROM document_versions v
            JOIN documents d ON d.id=v.document_id WHERE v.id=?''',(version,)).fetchone()
        return bool(row and self._market_available(db,row))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def register_source(self, source_id, *, publisher, endpoints, parser_id=PARSER,
                        freshness=None, permission_basis, kind='official',
                        read_allowed=True, outbound_allowed=True, enabled=True, shape=None):
        ident(source_id)
        if kind not in ('official','licensed') or not publisher or not permission_basis:
            raise KnowledgeError('source_permission_missing')
        if parser_id not in (PARSER,WEBANK_PARSER,PDF_PARSER):raise KnowledgeError('unregistered_parser')
        if not isinstance(endpoints, list) or not endpoints:
            raise KnowledgeError('source_allowlist_required')
        for entry in endpoints:
            self._valid_endpoint(entry)
        freshness = dict(freshness or {})
        for facet, maximum in freshness.items():
            if facet not in FACETS or (maximum is not None and (type(maximum) is not int or maximum < 0)):
                raise KnowledgeError('invalid_freshness_policy')
        with self.db() as db:
            db.execute('''INSERT INTO sources(id,publisher,kind,endpoints,read_allowed,outbound_allowed,enabled,parser_id,shape,freshness,permission_basis)
             VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET publisher=excluded.publisher,kind=excluded.kind,
             endpoints=excluded.endpoints,read_allowed=excluded.read_allowed,outbound_allowed=excluded.outbound_allowed,
             enabled=excluded.enabled,parser_id=excluded.parser_id,freshness=excluded.freshness,permission_basis=excluded.permission_basis,
             revision=sources.revision+1''',
             (source_id,publisher,kind,packed(endpoints),int(read_allowed),int(outbound_allowed),int(enabled),parser_id,shape,packed(freshness),permission_basis))

    @staticmethod
    def _valid_endpoint(url):
        if not isinstance(url,str) or len(url)>2048:raise KnowledgeError('source_url_denied')
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443) or p.query or p.fragment:
            raise KnowledgeError('source_url_denied')
        path=p.path
        # 浏览器、代理和服务器可能解码后消解点段；禁止借非规范路径绕过前缀。
        for _ in range(8):
            if '\\' in path or '//' in path or any(c.isspace() or ord(c)<32 for c in path) or any(seg in ('.','..') for seg in path.split('/')):
                raise KnowledgeError('source_url_denied')
            try:decoded=unquote(path,errors='strict')
            except (UnicodeError,ValueError):raise KnowledgeError('source_url_denied') from None
            if decoded==path:break
            path=decoded
        else:
            raise KnowledgeError('source_url_denied')
        # 注册只允许 HTTPS 域名及精确路径或路径前缀，不支持任意主机模板。
        from ipaddress import ip_address
        try:
            ip_address(p.hostname)
        except ValueError:
            if '.' not in p.hostname or p.hostname.endswith(('.local','.internal','.localhost')) or '\\' in url or any(c.isspace() for c in url):
                raise KnowledgeError('source_url_denied')
        else:
            raise KnowledgeError('source_url_denied')

    def source(self, source_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
        if row is None:
            raise KnowledgeError('source_unavailable')
        value = dict(row)
        for k in ('endpoints','freshness'):
            value[k] = json.loads(value[k])
        return value

    def validate_source_url(self, source_id, url):
        source = self.source(source_id)
        self._valid_endpoint(url)
        if not any(url == entry or (entry.endswith('/') and url.startswith(entry)) for entry in source['endpoints']):
            raise KnowledgeError('source_url_denied')
        return source

    def set_source_state(self, source_id, *, enabled=None, outbound_allowed=None, paused=None):
        changes = {k:int(v) for k,v in {'enabled':enabled,'outbound_allowed':outbound_allowed,'paused':paused}.items() if v is not None}
        if not changes:
            return
        with self.db() as db:
            if not db.execute('SELECT 1 FROM sources WHERE id=?',(source_id,)).fetchone():
                raise KnowledgeError('source_unavailable')
            db.execute('UPDATE sources SET '+','.join(k+'=?' for k in changes)+',revision=revision+1 WHERE id=?',(*changes.values(),source_id))

    def acknowledge_structure(self, source_id, shape):
        if not shape or len(shape)>200:
            raise KnowledgeError('invalid_structure')
        with self.db() as db:
            db.execute('UPDATE sources SET shape=?,paused=0,revision=revision+1 WHERE id=?',(shape,source_id))

    def register_product(self, product_id, *, issuer, scheme, code, name, verified=False):
        ident(product_id)
        if not all(isinstance(x,str) and x.strip() for x in (issuer,scheme,code,name)):
            raise KnowledgeError('identity_unverified')
        with self.db() as db:
            old = db.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
            if old and (old['issuer'],old['scheme'],old['code']) != (issuer,scheme,code):
                raise KnowledgeError('identity_mismatch')
            db.execute('INSERT INTO products VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,verified=excluded.verified',
                       (product_id,issuer,scheme,code,name,int(verified)))

    def register_share(self, share_class_id, *, product_id, scheme, code, currency):
        ident(share_class_id); ident(product_id)
        if not re.fullmatch('[A-Z]{3}',currency):
            raise KnowledgeError('invalid_currency')
        with self.db() as db:
            db.execute('INSERT INTO share_classes VALUES(?,?,?,?,?)',(share_class_id,product_id,scheme,code,currency))

    def register_channel(self, channel_id, *, name):
        ident(channel_id)
        with self.db() as db:
            db.execute('INSERT INTO channels VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(channel_id,name))

    @_market_operation(write=True)
    def stage_document(self, *, source_id, source_url, title, text, published_on=None,
                       retrieved_on=None, effective_from=None, effective_to=None,
                       product_id=None, share_class_id=None, channel_id=None, facets=None,
                       conditions=None, data_on=None, parser_id=PARSER, shape='plain-text-v1',
                       visibility='public', data_origin='real', read_scope='full_text',
                       complete=True, raw_bytes=None, spans=None,document_key='document',_market_capture=None):
        """新增候选，不准入、不发布；spans 必须逐字对应完整解析文本。"""
        source = self.validate_source_url(source_id,source_url)
        market_binding=None
        if source_id.startswith('market-') or shape=='market-official-full-text-v1':
            from .market_authority import MarketCapture
            scope=self._market_context.get();snapshot=scope[1] if scope else None
            if type(_market_capture) is not MarketCapture or snapshot is None or not snapshot.valid(_market_capture.binding):
                raise KnowledgeError('source_permission_missing')
            original=snapshot.capture(_market_capture.binding['article_id'])
            a=original.article;market_binding=original.binding
            if (original!=_market_capture or source_id!='market-'+a['source_id']
                    or source_url!=a['url'] or title!=a['title'] or text!=a['evidence_text']
                    or published_on!=a['published_at'][:10] or facets!=['general']
                    or parser_id!=PARSER or shape!='market-official-full-text-v1'
                    or document_key!='market-'+sha(packed(market_binding))[:40]):
                raise KnowledgeError('source_permission_missing')
        if visibility != 'public' or data_origin != 'real':
            raise KnowledgeError('private_or_simulated_evidence')
        if read_scope != 'full_text' or not complete:
            raise KnowledgeError('incomplete_full_text')
        if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT or not isinstance(title,str) or not 1<=len(title)<=240:
            raise KnowledgeError('invalid_document')
        if any(ord(c)<32 and c not in '\n\r\t' for c in text):
            raise KnowledgeError('invalid_document')
        retrieved_on = day(retrieved_on or today())
        published_on = day(published_on,True)
        effective_from,effective_to = day(effective_from,True),day(effective_to,True)
        data_on = day(data_on,True)
        if effective_from and effective_to and effective_from>=effective_to:
            raise KnowledgeError('invalid_effective_period')
        facets = list(dict.fromkeys(facets or ['general']))
        conditions = conditions or []
        self._annotations(facets,conditions)
        if share_class_id and not product_id:
            raise KnowledgeError('identity_unverified')
        for value in (product_id,share_class_id,channel_id):
            if value is not None:ident(value)
        if parser_id != source['parser_id']:
            raise KnowledgeError('source_structure_drift')
        raw = text.encode() if raw_bytes is None else raw_bytes
        if not isinstance(raw,bytes) or not raw or len(raw)>16*1024*1024:
            raise KnowledgeError('invalid_raw_document')
        parsed=self._parse_matches(raw,text,parser_id,source_id,source_url,product_id,share_class_id,channel_id,title,published_on)
        rows = self._spans(text,spans,product_id,share_class_id,channel_id,facets,conditions,data_on)
        if parser_id==PDF_PARSER:
            from .pdf_ingest import validate_page_spans,SHAPE
            if shape!=SHAPE:raise KnowledgeError('source_structure_drift')
            validate_page_spans(rows,parsed)
        if len(rows)>MAX_CHUNKS:
            raise KnowledgeError('document_chunk_limit')
        ident(document_key)
        document_id = 'd'+sha(packed([source_id,source_url,document_key]))[:32]
        identity_hash = sha(packed([{k:r[k] for k in ('start_char','end_char','product_id','share_class_id','channel_id','facets','conditions','data_on','paragraph','page')} for r in rows]))
        content_hash = sha(packed([document_id,sha(raw),sha(text),title,published_on,effective_from,effective_to,parser_id,shape,identity_hash]))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if source['shape'] is not None and source['shape'] != shape:
                db.execute('UPDATE sources SET paused=1,revision=revision+1 WHERE id=?',(source_id,))
            db.execute('INSERT OR IGNORE INTO raw_artifacts VALUES(?,?)',(sha(raw),raw))
            db.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?)',(document_id,source_id,source_url,document_key,'public','real'))
            version_id = 'v'+content_hash[:40]
            existing = db.execute('SELECT id FROM document_versions WHERE id=?',(version_id,)).fetchone()
            if existing:
                if market_binding is not None:
                    previous=db.execute('SELECT binding FROM market_lineage WHERE version_id=?',(version_id,)).fetchone()
                    if not previous or previous[0]!=packed(market_binding):raise KnowledgeError('source_permission_missing')
                db.execute('INSERT OR IGNORE INTO checks VALUES(?,?)',(version_id,retrieved_on))
                return version_id
            number = db.execute('SELECT coalesce(max(version_no),0)+1 FROM document_versions WHERE document_id=?',(document_id,)).fetchone()[0]
            parse_id='p'+sha(version_id+parser_id)[:40]
            db.execute('INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (version_id,document_id,number,sha(raw),title,text,sha(text),parser_id,shape,parse_id,published_on,
                        retrieved_on,effective_from,effective_to,'candidate',None,None,now()))
            if market_binding is not None:
                db.execute('INSERT INTO market_lineage VALUES(?,?)',(version_id,packed(market_binding)))
            start_id = db.execute('SELECT coalesce(max(row_id),0) FROM chunks').fetchone()[0]
            for i,row in enumerate(rows):
                chunk_id = 'c'+sha(version_id+packed(row))[:40]
                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (chunk_id,start_id+i+1,version_id,row['start_char'],row['end_char'],row['excerpt'],sha(row['excerpt']),
                            row['paragraph'],row['page'],row['product_id'],row['share_class_id'],row['channel_id'],
                            packed(row['facets']),packed(row['conditions']),row['data_on'],
                            ' '.join(tokens(title+' '+ ' '.join(FACET_LABELS[f] for f in row['facets']))), ' '.join(tokens(row['excerpt']))))
            db.execute('INSERT INTO checks VALUES(?,?)',(version_id,retrieved_on))
        return version_id

    @staticmethod
    def _annotations(facets,conditions):
        if not isinstance(facets,list) or not 1<=len(facets)<=8 or any(f not in FACETS for f in facets):
            raise KnowledgeError('invalid_facets')
        if not isinstance(conditions,list) or len(conditions)>8 or any(not isinstance(x,str) or not 1<=len(x)<=240 for x in conditions):
            raise KnowledgeError('invalid_conditions')

    @staticmethod
    def _parse_matches(raw,text,parser_id,source_id,source_url,product_id,share_class_id,channel_id,title,published_on):
        if parser_id==PARSER:
            try:valid=raw.decode('utf-8')==text
            except UnicodeError:valid=False
            if not valid:raise KnowledgeError('raw_parse_mismatch')
            return
        if parser_id==WEBANK_PARSER:
            from webank_catalog import decode_catalog,URL,FIELD_MAP
            try:
                source_date,_=decode_catalog(raw)
                wrapper=raw.decode('utf-8').strip()
                decoded=json.loads(wrapper[wrapper.index('(')+1:wrapper.rindex(')')])
                if set(decoded)-{'extractDate','productPresentation'} or any(set(row)-set(FIELD_MAP) for row in decoded['productPresentation']):
                    raise KnowledgeError('source_structure_drift')
                match=next((row for row in decoded['productPresentation'] if 'WEBANK:'+row['code']==product_id),None)
            except (ValueError,KeyError,TypeError,UnicodeError):raise KnowledgeError('source_structure_drift') from None
            if (source_id!='webank-direct-disclosure' or source_url!=URL or share_class_id is not None or channel_id!='WEBANK'
                    or match is None or packed(match)!=text or match['name']!=title or source_date.isoformat()!=published_on):
                raise KnowledgeError('raw_parse_mismatch')
            return
        if parser_id==PDF_PARSER:
            from .pdf_ingest import extract_pdf
            extracted=extract_pdf(raw)
            if extracted['text']!=text:raise KnowledgeError('raw_parse_mismatch')
            return extracted
        raise KnowledgeError('unregistered_parser')

    def _spans(self,text,spans,product,share,channel,facets,conditions,data_on):
        if spans is None:
            spans=[]
            # 精确保留半开字符范围；换行和空格属于原文，不做合成摘要。
            for start in range(0,len(text),1000):
                end=min(start+1000,len(text))
                if text[start:end].strip():spans.append({'start_char':start,'end_char':end,'paragraph':f'chars:{start}-{end}'})
        if not isinstance(spans,list) or not spans:
            raise KnowledgeError('invalid_spans')
        output=[]
        for span in spans:
            if not isinstance(span,Mapping):raise KnowledgeError('invalid_spans')
            start,end=span.get('start_char'),span.get('end_char')
            if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text) or end-start>1200:
                raise KnowledgeError('invalid_spans')
            excerpt=text[start:end]
            if not excerpt.strip() or ('excerpt' in span and span['excerpt'] != excerpt):raise KnowledgeError('quote_mismatch')
            row={k:span.get(k,v) for k,v in {'product_id':product,'share_class_id':share,'channel_id':channel,
                                          'facets':facets,'conditions':conditions,'data_on':data_on,'page':None,
                                          'paragraph':f'chars:{start}-{end}'}.items()}
            self._annotations(row['facets'],row['conditions'])
            day(row['data_on'],True)
            for k in ('product_id','share_class_id','channel_id'):
                if row[k] is not None:ident(row[k])
            if not isinstance(row['paragraph'],str) or not 1<=len(row['paragraph'])<=160:
                raise KnowledgeError('invalid_locator')
            if row['page'] is not None and (type(row['page']) is not int or not 1<=row['page']<=100000):
                raise KnowledgeError('invalid_locator')
            if row['share_class_id'] and not row['product_id']:raise KnowledgeError('identity_unverified')
            row.update(start_char=start,end_char=end,excerpt=excerpt)
            output.append(row)
        return output

    @_market_operation(write=True)
    def approve_version(self,version_id, *, verification_basis, supersedes=True):
        """本地受信准入：不会因域名、检索得分或模型推荐自动批准候选。"""
        if not isinstance(verification_basis,str) or not verification_basis.strip():
            raise KnowledgeError('verification_required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT v.*,s.enabled,s.read_allowed,s.outbound_allowed,s.paused,s.parser_id expected_parser,s.shape expected_shape,
                      s.freshness,s.permission_basis,d.source_id,d.source_url FROM document_versions v JOIN documents d ON d.id=v.document_id
                      JOIN sources s ON s.id=d.source_id WHERE v.id=?''',(version_id,)).fetchone()
            if row is None:raise KnowledgeError('version_missing')
            if not self._market_available(db,row):raise KnowledgeError('source_permission_missing')
            if row['status'] not in ('candidate','verified'):raise KnowledgeError('withdrawn_evidence')
            if not row['enabled'] or not row['read_allowed'] or not row['outbound_allowed']:
                raise KnowledgeError('source_permission_missing')
            if row['paused'] or row['parser_id']!=row['expected_parser'] or (row['expected_shape'] and row['shape']!=row['expected_shape']):
                raise KnowledgeError('source_structure_drift')
            webank_new = row['parser_id']==WEBANK_PARSER and row['status']=='candidate'
            if webank_new:
                # extractDate is bound to the JSONP original by the parser. The
                # insertion sequence and current source registry revision cannot
                # establish chronological order of two disclosed originals.
                day(row['published_on'])
                verified=db.execute('''SELECT published_on,raw_sha256 FROM document_versions
                    WHERE document_id=? AND id<>? AND parser_id=?
                    AND status IN ('verified','withdrawn') AND verification_basis IS NOT NULL ''',
                    (row['document_id'],version_id,WEBANK_PARSER)).fetchall()
                if any(v['published_on'] and v['published_on']>row['published_on'] for v in verified):
                    raise KnowledgeError('source_date_regressed')
                if any(v['published_on']==row['published_on'] and v['raw_sha256']!=row['raw_sha256'] for v in verified):
                    raise KnowledgeError('source_version_ambiguous')
            chunks=db.execute('''SELECT c.*,p.verified,sc.product_id share_product FROM chunks c LEFT JOIN products p ON p.id=c.product_id
                           LEFT JOIN share_classes sc ON sc.id=c.share_class_id WHERE c.version_id=?''',(version_id,)).fetchall()
            if not chunks:raise KnowledgeError('not_enough_evidence')
            rules=json.loads(row['freshness'])
            checked={}
            for chunk in chunks:
                if chunk['product_id'] and not chunk['verified']:raise KnowledgeError('identity_unverified')
                if chunk['share_class_id'] and chunk['share_product']!=chunk['product_id']:raise KnowledgeError('subject_mismatch')
                if any(f not in rules for f in json.loads(chunk['facets'])):raise KnowledgeError('freshness_policy_missing')
                if any(condition not in row['parsed_text'] for condition in json.loads(chunk['conditions'])):
                    raise KnowledgeError('condition_evidence_missing')
                if not self._quote_valid(db,row,chunk,checked):raise KnowledgeError('quote_mismatch')
            if row['parser_id']==WEBANK_PARSER and row['status']=='verified':
                # Idempotence rechecks trust and originals, but neither restores
                # an earlier version nor rewrites its approval/graph provenance.
                return version_id
            db.execute("UPDATE document_versions SET status='verified',verification_basis=? WHERE id=?",(verification_basis,version_id))
            if row['parser_id']==WEBANK_PARSER:
                # A verified version may be reread from a warm cache: keep its
                # original revocation boundary. A newly admitted disclosure
                # supersedes by source date, even when staged out of order.
                if webank_new and supersedes:
                    cutoff=row['published_on']
                    db.execute('''UPDATE document_versions SET revoked_from=CASE WHEN revoked_from IS NULL OR revoked_from>? THEN ? ELSE revoked_from END
                      WHERE document_id=? AND id<>? AND published_on<=? AND status='verified' ''',
                      (cutoff,cutoff,row['document_id'],version_id,cutoff))
            else:
                successor=db.execute('''SELECT effective_from,published_on FROM document_versions WHERE document_id=?
                           AND version_no>? AND status='verified' ORDER BY version_no LIMIT 1''',(row['document_id'],row['version_no'])).fetchone()
                if successor:
                    db.execute('UPDATE document_versions SET revoked_from=? WHERE id=?',
                               (successor['effective_from'] or successor['published_on'] or today(),version_id))
                if supersedes:
                    cutoff=row['effective_from'] or row['published_on'] or today()
                    # 已确认新版本，即使尚未共享发布，也不能继续使用旧当前条款。
                    db.execute('''UPDATE document_versions SET revoked_from=CASE WHEN revoked_from IS NULL OR revoked_from>? THEN ? ELSE revoked_from END
                      WHERE document_id=? AND id<>? AND version_no<? AND status='verified' ''',
                      (cutoff,cutoff,row['document_id'],version_id,row['version_no']))
            from .graph import materialize
            materialize(db,version_id,verification_basis)
        return version_id

    @staticmethod
    def _quote_valid(db,version,chunk,checked=None):
        checked={} if checked is None else checked
        key=(chunk['version_id'],version['raw_sha256'],version['parsed_sha256'])
        if key not in checked:
            raw=db.execute('SELECT payload FROM raw_artifacts WHERE sha256=?',(version['raw_sha256'],)).fetchone()
            text=(version['parsed_text'] if 'parsed_text' in version.keys() else
                  db.execute('SELECT parsed_text FROM document_versions WHERE id=?',(chunk['version_id'],)).fetchone()[0])
            checked[key]=(bool(raw and sha(bytes(raw[0]))==version['raw_sha256'] and sha(text)==version['parsed_sha256']),text)
        valid,text=checked[key]
        return bool(valid and 0<=chunk['start_char']<chunk['end_char']<=len(text)
                    and text[chunk['start_char']:chunk['end_char']]==chunk['excerpt']
                    and sha(chunk['excerpt'])==chunk['excerpt_sha256'])

    @_market_operation(write=True)
    def publish(self, *, version_ids=None, activate=True, temporary=False,preserve_current=False):
        """增量快照：复用不可变片段，清单改变才生成新发布。"""
        if temporary and activate:raise KnowledgeError('temporary_cannot_promote')
        if preserve_current and (version_ids is None or temporary or not activate):
            raise KnowledgeError('import_activation_requires_explicit_versions')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            ids=version_ids
            where="v.status='verified' AND s.enabled=1 AND s.read_allowed=1 AND s.outbound_allowed=1 AND s.paused=0"
            args=[]
            if ids is not None:
                ids=list(dict.fromkeys(ids))
                if not ids:raise KnowledgeError('empty_release')
                if any(not self._market_member_available(db,vid) for vid in ids):raise KnowledgeError('source_permission_missing')
                actual={r[0] for r in db.execute('SELECT id FROM document_versions WHERE status=\'verified\' AND id IN ('+','.join('?' for _ in ids)+')',ids)}
                if actual!=set(ids):raise KnowledgeError('unverified_release_member')
                if preserve_current:
                    current=db.execute('''SELECT r.id FROM current_release cr JOIN releases r ON r.id=cr.release_id
                        WHERE cr.scope='public' AND r.sealed=1 AND r.temporary=0''').fetchone()
                    if current:
                        members={r[0] for r in db.execute('''SELECT DISTINCT c.version_id FROM release_chunks rc
                            JOIN chunks c ON c.id=rc.chunk_id WHERE rc.release_id=?''',(current[0],))}
                        # 无新资料的同步不能扩大或重写刻意选择的当前正式快照。
                        if set(ids).issubset(members):return current[0]
                        eligible={r[0] for r in db.execute('''SELECT DISTINCT v.id FROM release_chunks rc
                            JOIN chunks c ON c.id=rc.chunk_id JOIN document_versions v ON v.id=c.version_id
                            JOIN documents d ON d.id=v.document_id JOIN sources s ON s.id=d.source_id
                            WHERE rc.release_id=? AND (v.revoked_from IS NULL OR v.revoked_from>?) AND '''+where,
                            (current[0],today()))}
                        eligible={vid for vid in eligible if self._market_member_available(db,vid)}
                        # 自动当前发布不累积已被确认替代的历史版本；已封存的旧快照
                        # 与其图/原文/向量成员不改，历史请求应固定到原发布代次。
                        # 未来才生效的替代仍保留旧版，避免提前移除现在有效的条款。
                        incoming={r[0] for r in db.execute('''SELECT id FROM document_versions
                            WHERE id IN ('''+','.join('?' for _ in ids)+''')
                            AND (revoked_from IS NULL OR revoked_from>?)''',(*ids,today()))}
                        if not incoming:return current[0]
                        ids=sorted(eligible|incoming)
                where+=' AND v.id IN ('+','.join('?' for _ in ids)+')';args.extend(ids)
            rows=db.execute('''SELECT c.*,v.document_id,v.shape,v.parsed_text,v.parsed_sha256,v.raw_sha256,v.effective_from,v.published_on,d.source_id,d.source_url FROM chunks c JOIN document_versions v ON v.id=c.version_id
                  JOIN documents d ON d.id=v.document_id JOIN sources s ON s.id=d.source_id WHERE '''+where+' ORDER BY c.id',args).fetchall()
            rows=[row for row in rows if self._market_available(db,row)]
            if not rows:raise KnowledgeError('empty_release')
            if len(rows)>MAX_CHUNKS:raise KnowledgeError('release_size_limit')
            if ids is not None and {r['version_id'] for r in rows}!=set(ids):raise KnowledgeError('unavailable_release_member')
            checked={}
            for row in rows:
                if not self._quote_valid(db,row,row,checked):raise KnowledgeError('quote_mismatch')
                from .graph import paths
                if paths(db,row,row['effective_from'] or row['published_on'] or today()) is None:
                    raise KnowledgeError('graph_path_missing')
            manifest=sha(packed([(r['id'],r['excerpt_sha256']) for r in rows]))
            release_id=('tmp-' if temporary else 'pub-')+manifest[:40]
            existing=db.execute('SELECT manifest_sha256,sealed FROM releases WHERE id=?',(release_id,)).fetchone()
            if existing is None:
                db.execute('INSERT INTO releases VALUES(?,?,?,?,0)',(release_id,manifest,int(temporary),now()))
                db.executemany('INSERT INTO release_chunks VALUES(?,?)',[(release_id,r['id']) for r in rows])
                db.execute('UPDATE releases SET sealed=1 WHERE id=?',(release_id,))
            elif existing['manifest_sha256']!=manifest or not existing['sealed']:
                raise KnowledgeError('release_manifest_mismatch')
            if activate:
                db.execute("INSERT INTO current_release VALUES('public',?) ON CONFLICT(scope) DO UPDATE SET release_id=excluded.release_id",(release_id,))
        from .vectors import schedule
        schedule(self, release_id)
        return release_id

    def for_release(self,release_id):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM releases WHERE id=?',(release_id,)).fetchone():raise KnowledgeError('release_missing')
        return LocalKnowledgeProvider(self.path,release_id=release_id,encoder=self.vector_encoder,market_authority=self._market_authority)

    def build_vector_index(self,release_id=None):
        from .vectors import VectorIndex
        if release_id is None:
            with self.db() as db:
                current=db.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()
            release_id=self.fixed_release or (current[0] if current else None)
        if release_id is None:raise KnowledgeError('release_missing')
        ident(release_id)
        result=VectorIndex(self,self.vector_encoder).build(release_id)
        if os.environ.get('NAVREN_STRUCTURE_ENABLED','0')=='1':
            from .structure_index import StructureIndex,reason
            try:result['structure_index']=StructureIndex(self,self.vector_encoder).build(release_id)
            except Exception as error:result['structure_index']={'ready':False,'reason':reason(error)}
        return result

    def status(self):
        with self.db() as db:
            row=db.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()
            release=self.fixed_release or (row[0] if row else None)
            ready=bool(release and db.execute('SELECT 1 FROM releases WHERE id=? AND sealed=1',(release,)).fetchone())
            count=db.execute('SELECT count(*) FROM release_chunks WHERE release_id=?',(release,)).fetchone()[0] if ready else 0
            graph_edges=db.execute('''SELECT count(*) FROM graph_relations gr JOIN graph_provenance gp ON gp.relation_id=gr.id
                 JOIN release_chunks rc ON rc.chunk_id=gr.citation_id WHERE rc.release_id=? AND gr.status='verified' ''',(release,)).fetchone()[0] if ready else 0
            graph_entities=db.execute('SELECT count(*) FROM graph_entities').fetchone()[0]
            from .vectors import VectorIndex
            vector_status=VectorIndex(self,self.vector_encoder).status(db,release) if ready else {'ready':False,'reason':'release_missing'}
        return {'schema_version':1,'ready':ready,'provider_id':self.provider_id,'public_release_id':release,
                'retrieval_method':('local_fts5_vector_rrf_typed_evidence_graph' if vector_status['ready'] else
                                    'local_fts5_candidates_typed_evidence_graph'),'published_chunk_count':count,
                'private_retrieval_available':False,'graph_ready':bool(ready and graph_edges),
                'graph_schema':'typed-public-evidence/1','graph_entity_count':graph_entities,
                'published_evidenced_relation_count':graph_edges,'neural_embeddings_available':vector_status['ready'],
                'vector_index':vector_status,
                'llm_extraction_available':False,'maximum_graph_depth':4,'maximum_release_chunks':MAX_CHUNKS}

    def withdraw(self,version_id):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM document_versions WHERE id=?',(version_id,)).fetchone():raise KnowledgeError('version_missing')
            db.execute("UPDATE document_versions SET status='withdrawn' WHERE id=?",(version_id,))

    def block_conflict(self, *, facets, product_id=None,share_class_id=None,channel_id=None,from_on=None,to_on=None,reason):
        self._annotations(facets,[])
        from_on,to_on=day(from_on or today()),day(to_on,True)
        if to_on and to_on<=from_on:raise KnowledgeError('invalid_effective_period')
        cid='conflict-'+uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO conflicts VALUES(?,?,?,?,?,?,?,?,?)',(cid,product_id,share_class_id,channel_id,packed(facets),from_on,to_on,1,reason))
        return cid

    def resolve_conflict(self,conflict_id):
        with self.db() as db:
            db.execute('UPDATE conflicts SET active=0 WHERE id=?',(conflict_id,))

    def _available(self,db,row,as_of,checked=None):
        if not self._market_available(db,row):return [],'source_permission_missing'
        if row['status']!='verified' or row['visibility']!='public' or row['data_origin']!='real':return [],'evidence_unavailable'
        if not row['enabled'] or not row['read_allowed'] or not row['outbound_allowed']:return [],'source_permission_missing'
        if not any(row['source_url']==endpoint or (endpoint.endswith('/') and row['source_url'].startswith(endpoint))
                   for endpoint in json.loads(row['endpoints'])):return [],'source_permission_missing'
        if row['product_id'] and not row['verified']:return [],'identity_unverified'
        if row['share_class_id'] and row['share_product']!=row['product_id']:return [],'identity_unverified'
        if row['channel_id'] and row['bound_channel']!=row['channel_id']:return [],'identity_unverified'
        if row['published_on'] and row['published_on']>as_of:return [],'published_after_as_of'
        if row['data_on'] and row['data_on']>as_of:return [],'not_yet_effective'
        if row['effective_from'] and row['effective_from']>as_of:return [],'not_yet_effective'
        if (row['effective_to'] and row['effective_to']<=as_of) or (row['revoked_from'] and row['revoked_from']<=as_of):return [],'expired_evidence'
        if not self._quote_valid(db,row,row,checked):return [],'evidence_unavailable'
        blocked=set()
        for c in db.execute('SELECT * FROM conflicts WHERE active=1 AND from_on<=? AND (to_on IS NULL OR to_on>?)',(as_of,as_of)):
            if all(c[k] is None or c[k]==row[k] for k in ('product_id','share_class_id','channel_id')):
                blocked.update(json.loads(c['facets']))
        result=[];reason=None;rules=json.loads(row['freshness'])
        for facet in json.loads(row['facets']):
            if facet in blocked:reason='conflicting_sources';continue
            if facet not in rules:reason='source_unavailable';continue
            maximum=rules[facet]
            basis=row['data_on'] or row['published_on']
            if maximum is not None and (basis is None or (date.fromisoformat(as_of)-date.fromisoformat(basis)).days>maximum):
                reason='expired_evidence';continue
            result.append(facet)
        # 原文片段不可用标签遮住其中冲突/过期数值；混合片段整体停用。
        # 独立有效维度须由独立 span 支持，禁止将失效数字带给云端再要求其忽略。
        return ([],reason) if reason else (result,None)

    def _rows(self,db,release_id, *, citation_ids=None):
        where='rc.release_id=?';args=[release_id]
        if citation_ids is not None:
            if not citation_ids:return []
            where+=' AND rc.chunk_id IN ('+','.join('?' for _ in citation_ids)+')'
            args.extend(citation_ids)
        return db.execute('''SELECT c.*,v.document_id,v.parse_id,v.title,v.parser_id,v.shape,d.source_id,v.published_on,v.retrieved_on,v.effective_from,v.effective_to,
               v.status,v.revoked_from,v.parsed_sha256,v.raw_sha256,s.publisher,s.enabled,s.read_allowed,s.outbound_allowed,
               s.freshness,s.paused,s.endpoints,d.source_url,d.visibility,d.data_origin,p.verified,
               sc.product_id share_product,ch.id bound_channel
               FROM release_chunks rc JOIN chunks c ON c.id=rc.chunk_id JOIN document_versions v ON v.id=c.version_id
               JOIN documents d ON d.id=v.document_id JOIN sources s ON s.id=d.source_id LEFT JOIN products p ON p.id=c.product_id
               LEFT JOIN share_classes sc ON sc.id=c.share_class_id LEFT JOIN channels ch ON ch.id=c.channel_id
               WHERE '''+where,args).fetchall()

    @staticmethod
    def _release_manifest_valid(db,release_id):
        """Read-side seal and complete membership check within the caller's transaction.

        The manifest is one aggregate digest, so verifying it still reads all member
        IDs and excerpt hashes, never every member's body or joined document data.
        No result is retained across connections, including after trigger removal.
        """
        release=db.execute('SELECT manifest_sha256,sealed,temporary FROM releases WHERE id=?',(release_id,)).fetchone()
        if release is None or not release['sealed']:return False
        members=db.execute('''SELECT rc.chunk_id,c.id,c.excerpt_sha256 FROM release_chunks rc
                   LEFT JOIN chunks c ON c.id=rc.chunk_id WHERE rc.release_id=? ORDER BY rc.chunk_id''',(release_id,)).fetchall()
        if not members or len(members)>MAX_CHUNKS or any(r['id'] is None for r in members):return False
        manifest=sha(packed([(r['chunk_id'],r['excerpt_sha256']) for r in members]))
        prefix='tmp-' if release['temporary'] else 'pub-'
        return manifest==release['manifest_sha256'] and release_id==prefix+manifest[:40]

    @_market_operation()
    def search_public(self,request,release_id):
        ident(release_id)
        as_of=day(request['as_of']);limit=request['limit']
        if request.get('scope')!='public' or type(limit) is not int or not 1<=limit<=8 or not isinstance(request.get('query'),str) or not 1<=len(request['query'])<=500:
            raise KnowledgeError('invalid_request')
        facets=request.get('facets',[])
        if not isinstance(facets,list) or len(facets)>8 or any(f not in FACETS for f in facets):raise KnowledgeError('invalid_request')
        requested=[k for k in ('product_id','share_class_id','channel_id') if request.get(k) is not None]
        for k in requested:ident(request[k])
        terms=tokens(request['query'])[:80]
        gaps=[];matches=[];covered=set()
        with self.db() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM releases WHERE id=?',(release_id,)).fetchone():raise KnowledgeError('release_missing')
            for k,table in (('product_id','products'),('share_class_id','share_classes'),('channel_id','channels')):
                if request.get(k) and not db.execute('SELECT 1 FROM '+table+' WHERE id=?',(request[k],)).fetchone():
                    gaps.append({'code':'identity_unverified'});break
            scores={}
            if terms:
                expression=' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
                # FTS 引擎只返回候选，权威数据和时点准入仍在下方逐项回查。
                scores={r['rowid']:r['score'] for r in db.execute('SELECT rowid,bm25(chunks_fts,3.0,1.0) score FROM chunks_fts WHERE chunks_fts MATCH ?',(expression,))}
            rows=self._rows(db,release_id)
            from .retrieval_ranking import identity_issue
            issue=identity_issue(request['query'],rows,request)
            if issue:
                return {'schema_version':1,'release_id':release_id,'citation_ids':[],
                        'coverage_gaps':[{'code':issue},{'code':'no_verified_evidence'}],
                        'retrieval':{'mode':'fts5','vector_reason':'vector_empty_eligible','vector_used':False,
                                     'vector_candidate_count':0,'lexical_candidate_count':0,
                                     'structured_intent':[],'field_candidate_count':0,'ranking_method':issue}}
            checked={};eligible={};eligible_rows=[];lexical={};fallback=[]
            for row in rows:
                if any(row[k]!=request[k] for k in requested):continue
                available,reason=self._available(db,row,as_of,checked)
                if reason and (not facets or set(facets)&set(json.loads(row['facets']))):gaps.append({'code':reason})
                if row['paused']:gaps.append({'code':'source_unavailable'})
                relevant=set(available)&set(facets) if facets else set(available)
                if not relevant:continue
                from .graph import paths
                if paths(db,row,as_of,request=request,facets=list(relevant)) is None:
                    gaps.append({'code':'not_enough_evidence'});continue
                eligible[row['id']]=relevant
                eligible_rows.append(row)
                if row['row_id'] in scores:lexical[row['id']]=scores[row['row_id']]
                elif request.get('product_id') and facets:fallback.append(row['id'])
            from .vectors import VectorIndex,fuse
            from .retrieval_ranking import structural_intents,lexical_weights,RANKING_POLICY
            intent=structural_intents(request['query'],eligible_rows,request)
            from .public_aliases import query_projection,concept_coverage,POLICY as ALIAS_POLICY
            projection=query_projection(request['query']) if not intent['objects'] else None
            # Derived field labels never rewrite sealed chunks or bypass eligibility.
            for cid in intent['preferred']:lexical.setdefault(cid,0.0)
            semantic_query=intent['residual'].strip() if intent['objects'] and intent['residual'].strip() else request['query']
            semantic_ids={r['id'] for r in eligible_rows if r['product_id'] in intent['objects']} if intent['objects'] else set(eligible)
            from .retrieval_encoder import cross_language_query
            cross_language=not intent['objects'] and cross_language_query(semantic_query,eligible_rows)
            coverage=concept_coverage(semantic_query)
            # Dense existing public terminology keeps its retrieval views; the
            # alignment branch adds original-language recall for sparse coverage.
            alignment_required=cross_language and (not projection or coverage<0.5)
            from .query_features import translated_feature
            from .retrieval_encoder import is_bundle,selection
            from .embeddings import VectorError
            dual_selected=is_bundle(self.vector_encoder)
            if self.vector_encoder is None:
                try:dual_selected=selection()=='dual'
                except VectorError:dual_selected=False
            feature_applicable=(cross_language and bool(re.search(r'[\u3400-\u9fff]',semantic_query))
                                and self.vector_encoder is not False
                                and os.environ.get('NAVREN_VECTOR_ENABLED','1')!='0'
                                and dual_selected)
            translation,translation_meta=translated_feature(semantic_query,applicable=feature_applicable)
            extra_lexical={}
            for name,extra_query in (('public_concepts',projection),('translated',translation)):
                if not extra_query:continue
                extra_terms=tokens(extra_query)[:80]
                if not extra_terms:continue
                expression=' OR '.join('"'+t.replace('"','""')+'"' for t in extra_terms)
                extra_scores={r['rowid']:r['score'] for r in db.execute('SELECT rowid,bm25(chunks_fts,3.0,1.0) score FROM chunks_fts WHERE chunks_fts MATCH ?',(expression,))}
                extra_lexical[name]={row['id']:extra_scores[row['row_id']] for row in eligible_rows if row['row_id'] in extra_scores}
            projected_lexical=extra_lexical.get('public_concepts',{})
            structure={'ready':False,'used':False,'reason':'structure_disabled','parent_groups':[]}
            if os.environ.get('NAVREN_STRUCTURE_ENABLED','0')=='1' and not intent['objects']:
                from .structure_index import StructureIndex
                structure=StructureIndex(self,self.vector_encoder).search(db,release_id,semantic_query,
                    semantic_ids,projected_query=projection,translated_query=translation,
                    cross_language=alignment_required)
            segment_structures=[]
            from .query_segments import POLICY as SEGMENT_POLICY
            segment_meta={'policy':SEGMENT_POLICY,'used':False,'count':1,
                          'reason':'structure_unavailable'}
            if structure['used']:
                from .query_group_retrieval import additional_query_structures
                structure,segment_structures,segment_meta=additional_query_structures(
                    self,db,release_id,semantic_query,semantic_ids,eligible_rows,structure)
            semantic=VectorIndex(self,self.vector_encoder).search(db,release_id,semantic_query,semantic_ids,
                                  projected_query=projection,cross_language=alignment_required,
                                  translated_query=translation)
            weights=lexical_weights(request['query'],eligible_rows,intent)
            lexical_views=None;view_weights=None
            if extra_lexical:
                lexical_views={'original':lexical,**extra_lexical}
                view_weights={'original':weights}
                for name,extra_query in (('public_concepts',projection),('translated',translation)):
                    if name in extra_lexical:view_weights[name]=lexical_weights(extra_query,eligible_rows,intent)
            ranking=fuse(lexical,semantic['scores'],weights,semantic.get('views'),lexical_views,view_weights) if semantic['scores'] or extra_lexical else lexical.copy()
            canonical_ranking=ranking.copy()
            # 对象明确的维度补召回仍保留，但排在真实召回后。
            for cid in fallback:ranking.setdefault(cid,1.0)
            from .retrieval_ranking import adjacent_context
            # 已验收的列举上下文仍从原召回的种子扩展；句段不能关闭该协议。
            initial_ids=sorted(canonical_ranking,key=lambda cid:(cid not in intent['preferred'],canonical_ranking[cid],cid))
            context_ids=adjacent_context(request['query'],initial_ids,eligible_rows) if not intent['objects'] else []
            for cid in context_ids:ranking.setdefault(cid,0.0)
            matches=[(score,cid,eligible[cid]) for cid,score in ranking.items() if cid in eligible]
            retrieval={'mode':'hybrid' if semantic['mode']=='vector' else 'fts5',
                       'vector_reason':semantic['reason'],'vector_candidate_count':len(semantic['scores']),
                       'vector_used':bool(semantic['scores']),
                       'lexical_candidate_count':len(set(lexical).union(*(set(view) for view in extra_lexical.values()))),
                       'structured_intent':intent['fields'],'field_candidate_count':len(intent['preferred']),
                       'query_alias_policy':ALIAS_POLICY,'query_projection_used':bool(projection),
                       'semantic_view_count':sum(bool(v) for v in semantic.get('views',{}).values()),
                       'vector_branches':semantic.get('branches',[]),
                       'query_view_failures':semantic.get('view_failures',{}),
                       'cross_language_route':alignment_required,
                       'public_concept_coverage':round(coverage,6),
                       'query_translation':translation_meta,
                       'query_segments':segment_meta,
                       'structure_index':{k:v for k,v in structure.items()
                                          if k not in ('parent_groups','evidence_groups')},
                       'context_candidate_count':max(0,len(context_ids)-1),
                       'ranking_policy':RANKING_POLICY,
                       'ranking_method':'coverage_weighted_rrf' if semantic['scores'] or extra_lexical else 'bm25_with_field_intent'}
            object_ids={r['id'] for r in eligible_rows if r['product_id'] in intent['objects']}
            matches.sort(key=lambda r:(bool(object_ids) and r[1] not in object_ids,r[1] not in intent['preferred'],
                                      context_ids.index(r[1]) if r[1] in context_ids else len(context_ids),r[0],r[1]))
            from .public_institutions import order_public_institution_coverage, POLICY as INSTITUTION_POLICY
            if intent['objects']:
                retrieval['institution_coverage']={'policy':INSTITUTION_POLICY,'used':False,
                                                   'reason':'structured_product_intent'}
            else:
                matches,institution_coverage=order_public_institution_coverage(request['query'],matches,eligible_rows)
                retrieval['institution_coverage']=institution_coverage
            selected=[]
            if structure['used']:
                from .evidence_group_selection import (plan_evidence_groups,fuse_group_candidates,
                                                       ORIGINAL_GEOMETRY_POLICY)
                institution=retrieval['institution_coverage']
                seed_count=len(institution.get('participating_institution_ids',[])) if institution['used'] else 0
                seed_ids=[match[1] for match in matches[:seed_count]]
                context_groups=[]
                if context_ids:
                    seed_ids=list(dict.fromkeys([*seed_ids,*context_ids]))[:8]
                    rows_by_id={row['id']:row for row in eligible_rows}
                    context_rows=[rows_by_id[cid] for cid in context_ids]
                    context_groups=[{'group_id':'ctx-'+sha(packed(context_ids)),
                        'kind':'required_context','core':True,'complete':True,
                        'citation_ids':context_ids,'version_id':context_rows[0]['version_id'],
                        'start_char':min(row['start_char'] for row in context_rows),
                        'end_char':max(row['end_char'] for row in context_rows),
                        'score':canonical_ranking.get(context_ids[0],0.0)}]
                ranking_ids=[];seed_group_ids=[];preferred_group_ids=[]
                if os.environ.get('NAVREN_RERANK_ENABLED','0')=='1':
                    from .query_group_retrieval import rank_query_groups
                    groups=fuse_group_candidates(matches,structure.get('evidence_groups',[]),
                        eligible_ids=set(eligible),canonical_rows=eligible_rows,context_groups=context_groups)
                    ranking_ids,seed_group_ids,rerank_meta=rank_query_groups(
                        db,semantic_query,groups,eligible_rows,segment_structures,translated_query=translation)
                    retrieval['reranking']=rerank_meta
                    preferred_group_ids=rerank_meta.get('preferred_evidence_group_ids',
                                                       rerank_meta.get('preferred_original_group_ids',[]))
                    if rerank_meta['segment_missing_core_count']:
                        gaps.append({'code':'context_incomplete'})
                selected,context_budget=plan_evidence_groups(matches,structure.get('evidence_groups',[]),
                    eligible_ids=set(eligible),limit=limit,facets=facets,seed_ids=seed_ids,
                    canonical_rows=eligible_rows,context_groups=context_groups,
                    ranking_ids=ranking_ids,seed_group_ids=seed_group_ids,
                    preferred_group_ids=preferred_group_ids)
                retrieval['structure_index']['rank_fusion']='canonical_evidence_group_exact_geometry_rrf/2'
                retrieval['structure_index']['original_geometry_policy']=ORIGINAL_GEOMETRY_POLICY
                retrieval['structure_index']['context_budget']=context_budget
                if (context_budget['incomplete_accepted_groups']
                        or context_budget['uncovered_seed_ids']
                        or context_budget.get('uncovered_seed_group_ids')):
                    gaps.append({'code':'context_incomplete'})
                for cid in selected:covered.update(eligible[cid])
            else:
                # 先覆盖独立维度，再用分数补足。
                for facet in facets:
                    candidate=next((m for m in matches if facet in m[2] and m[1] not in selected),None)
                    if candidate and len(selected)<limit:selected.append(candidate[1]);covered.update(candidate[2])
                for match in matches:
                    if len(selected)>=limit:break
                    if match[1] not in selected:selected.append(match[1]);covered.update(match[2])
            # Payment horizons require their same-version counting original.
            # This recalls already eligible source rows; it grants no answer proof.
            if 'liquidity' in facets:
                from .payment_time_requirements import payment_time_requirements
                time_requirements=payment_time_requirements(selected,eligible_rows,
                    release_id=release_id,as_of=as_of)
                if time_requirements['active']:
                    time_requirements['baseline_citation_ids']=list(selected)
                    time_requirements['complete']=False
                    retrieval['payment_time_requirements']=time_requirements
                    if time_requirements['coverage_gaps']:
                        gaps.extend(time_requirements['coverage_gaps']);selected=[]
                    else:
                        from .evidence_group_selection import plan_evidence_groups
                        original_seeds=seed_ids if structure['used'] else []
                        time_seeds=list(dict.fromkeys([*original_seeds,*time_requirements['pair_seed_ids']]))
                        if len(time_seeds)>8 or len(set(time_seeds))>limit:
                            issue={'code':'payment_time_pair_budget_exceeded','role':'time_definition'}
                            gaps.append(issue);time_requirements['coverage_gaps'].append(issue);selected=[]
                        else:
                            pair_matches=list(matches);ranked_ids={m[1] for m in pair_matches}
                            for cid in time_seeds:
                                if cid not in ranked_ids:
                                    pair_matches.append((0.0,cid,eligible[cid]));ranked_ids.add(cid)
                            selected,time_budget=plan_evidence_groups(pair_matches,
                                structure.get('evidence_groups',[])if structure['used']else [],
                                eligible_ids=set(eligible),limit=limit,facets=facets,seed_ids=time_seeds,
                                canonical_rows=eligible_rows,
                                context_groups=context_groups if structure['used']else [],
                                ranking_ids=ranking_ids if structure['used']else [],
                                seed_group_ids=seed_group_ids if structure['used']else [],
                                preferred_group_ids=preferred_group_ids if structure['used']else [])
                            time_requirements['context_budget']=time_budget
                            # Replanning may recall another deadline. Do not emit
                            # any new isolated horizon or silently infer its unit.
                            final_requirements=payment_time_requirements(selected,eligible_rows,
                                release_id=release_id,as_of=as_of)
                            final_seeds=set(final_requirements['pair_seed_ids'])
                            if (not set(time_seeds)<=set(selected)
                                    or time_budget['incomplete_accepted_groups']
                                    or time_budget.get('uncovered_seed_group_ids')
                                    or final_requirements['coverage_gaps']
                                    or not final_seeds<=set(selected)):
                                issues=final_requirements['coverage_gaps']or[
                                    {'code':'payment_time_pair_selection_incomplete','role':'time_definition'}]
                                gaps.extend(issues);time_requirements['coverage_gaps'].extend(issues);selected=[]
                            else:
                                time_requirements['pairs']=final_requirements['pairs']
                                time_requirements['pair_seed_ids']=final_requirements['pair_seed_ids']
                                time_requirements['complete']=True
                    time_requirements['selected_citation_ids']=list(selected)
                    covered=set()
                    for cid in selected:covered.update(eligible[cid])
            from .field_semantics import assess_semantic_coverage
            semantic_coverage=assess_semantic_coverage(request['query'],eligible_rows,intent['objects'],selected_ids=selected)
            retrieval['semantic_coverage']=semantic_coverage
            gaps.extend(semantic_coverage['coverage_gaps'])
        gaps.extend({'code':'facet_uncovered','facet':f} for f in facets if f not in covered)
        if not selected:gaps.append({'code':'no_verified_evidence'})
        if 'sale_status' in facets:gaps.append({'code':'sale_status_unknown','facet':'sale_status'})
        unique=[];seen=set()
        for gap in gaps:
            key=(gap['code'],gap.get('facet'),gap.get('product_id'),gap.get('role'),gap.get('field'))
            if key not in seen:unique.append(gap);seen.add(key)
        return {'schema_version':1,'release_id':release_id,'citation_ids':selected,'coverage_gaps':unique[:32],
                'retrieval':retrieval}

    def resolve_public_evidence(self,citation_id,release_id,as_of):
        ident(citation_id);ident(release_id);day(as_of)
        return self.resolve_public_evidence_batch([citation_id],release_id,as_of)[citation_id]

    @_market_operation()
    def resolve_public_evidence_batch(self,citation_ids,release_id,as_of):
        """Resolve up to 64 references at one as-of in a single read snapshot.

        Return ``{citation_id: canonical_or_None}``; duplicate IDs are coalesced.
        Raw/text hash reuse is local to this transaction, not a permanent cache.
        A later call rechecks the seal, membership, content and dynamic gates.
        ``paused`` keeps its existing meaning: stop new admission/publication;
        previously verified originals remain readable unless separately withdrawn.
        """
        ident(release_id);day(as_of)
        if not isinstance(citation_ids,(list,tuple)) or len(citation_ids)>64:
            raise KnowledgeError('invalid_citation_batch')
        ids=list(dict.fromkeys(ident(cid) for cid in citation_ids))
        result={cid:None for cid in ids}
        if not ids:return result
        with self.db() as db:
            # sqlite3 does not implicitly begin a transaction for SELECT. All
            # membership, permission, original and graph reads must share one.
            db.execute('BEGIN')
            if not self._release_manifest_valid(db,release_id):return result
            rows=self._rows(db,release_id,citation_ids=ids)
            checked={}
            from .graph import paths
            for row in rows:
                facets,_=self._available(db,row,as_of,checked)
                if not facets or paths(db,row,as_of,facets=facets) is None:continue
                result[row['id']]={'schema_version':1,'citation_id':row['id'],'release_id':release_id,'document_id':row['document_id'],
                    'document_version_id':row['version_id'],'parse_id':row['parse_id'],'span_id':row['id'],
                    'title':row['title'],'publisher':row['publisher'],'source_url':row['source_url'],'excerpt':row['excerpt'],
                    'visibility':'public','outbound_allowed':True,'withdrawn':False,'content_status':'full_text',
                    'product_id':row['product_id'],'share_class_id':row['share_class_id'],'channel_id':row['channel_id'],
                    'locator':{'page':row['page'],'paragraph':row['paragraph'],'start_char':row['start_char'],'end_char':row['end_char']},
                    'published_on':row['published_on'],'retrieved_on':row['retrieved_on'],
                    'effective_from':row['effective_from'],'effective_to':row['effective_to'],
                    'conditions':json.loads(row['conditions']),'facets':facets}
        return result

    def record_job(self,source_id,public_key, *, status,error_code=None,version_ids=None,started_at=None):
        job_id='job-'+uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO ingest_jobs VALUES(?,?,?,?,?,?,?,?)',
                       (job_id,source_id,public_key,status,started_at or now(),now(),error_code,packed(version_ids or [])))
        return job_id

    def history(self,source_url):
        """受信本地管理接口；不通过公共检索输出候选原文。"""
        with self.db() as db:
            return [dict(r) for r in db.execute('''SELECT v.id,v.version_no,v.status,v.published_on,v.effective_from,v.effective_to,v.revoked_from
                    FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.source_url=? ORDER BY v.version_no''',(source_url,))]

    def read_archived_version(self,version_id):
        """仅受信本地管理用途；归档原文不成为当前答复的有效证据。"""
        with self.db() as db:
            row=db.execute('''SELECT v.*,d.source_url FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?''',(version_id,)).fetchone()
            if row is None:raise KnowledgeError('version_missing')
            return {'version_id':version_id,'status':row['status'],'source_url':row['source_url'],
                    'parsed_text':row['parsed_text'],'raw_sha256':row['raw_sha256'],
                    'revoked_from':row['revoked_from'],'local_archive_only':True,'usable_for_current_task':False}

    def import_market_store(self,market_store, **options):
        from .importers import import_market_store
        return import_market_store(self,market_store,**options)

    def import_webank_disclosure(self,body, **options):
        from .importers import import_webank_disclosure
        return import_webank_disclosure(self,body,**options)

    @_market_operation()
    def graph_trace(self,citation_id,release_id,as_of, *, request=None):
        """本地审计接口；消费合同不额外夹带可泄漏字段。"""
        from .graph import paths
        with self.db() as db:
            db.execute('BEGIN')
            row=next((r for r in self._rows(db,release_id) if r['id']==citation_id),None)
            if row is None:return {'paths':[],'coverage_gaps':[{'code':'evidence_unavailable'}]}
            facets,reason=self._available(db,row,as_of)
            if not facets:return {'paths':[],'coverage_gaps':[{'code':reason or 'evidence_unavailable'}]}
            traces=paths(db,row,as_of,request=request,facets=facets)
            return {'citation_id':citation_id,'release_id':release_id,'paths':traces or [],
                    'coverage_gaps':[] if traces else [{'code':'not_enough_evidence','reason':'graph_path_missing'}]}

    @_market_operation()
    def graph_search(self,request,release_id):
        result=self.search_public(request,release_id)
        return {**result,'graph_traces':[self.graph_trace(cid,release_id,request['as_of'],request=request) for cid in result['citation_ids']]}

    def add_reviewed_relation(self, **values):
        from .graph import reviewed_relation
        return reviewed_relation(self,**values)

    def withdraw_relation(self,relation_id):
        with self.db() as db:
            db.execute("UPDATE graph_relations SET status='withdrawn' WHERE id=?",(relation_id,))

    @_market_operation()
    def related_products(self,product_id,release_id,as_of, **options):
        from .graph import related_products
        return related_products(self,product_id,release_id,as_of,**options)

    @_market_operation()
    def public_identity_catalog(self, *, release_id=None, as_of=None, limit=64):
        """Trusted, fixed-release identity discovery only; no terms/fee authority.

        The legacy public_catalog remains unchanged. Labels and parent/share
        links come only from usable reviewed graph edges on general originals.
        A code label is never expanded into an inferred A/C share name.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise KnowledgeError('invalid_limit')
        as_of = day(as_of or today())
        records, seen = [], set()
        with self.db() as db:
            db.execute('BEGIN')
            if release_id is None:
                current = db.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()
                release_id = self.fixed_release or (current[0] if current else None)
            if release_id is None:
                return {'schema': 'navren.public-identity-catalog/1', 'release_id': None,
                        'as_of': as_of, 'entities': []}
            ident(release_id)
            if self.fixed_release is not None and release_id != self.fixed_release:
                raise KnowledgeError('release_mismatch')
            if not self._release_manifest_valid(db, release_id):
                raise KnowledgeError('release_manifest_mismatch')
            from .graph import paths, _usable_edge, entity_id
            checked = {}
            rows = sorted(self._rows(db, release_id), key=lambda r: (
                r['product_id'] or '', r['share_class_id'] or '', r['id']))
            for row in rows:
                facets, _ = self._available(db, row, as_of, checked)
                if 'general' not in facets or paths(db, row, as_of, facets=facets) is None:
                    continue
                edges = [dict(e) for e in db.execute(
                    'SELECT * FROM graph_relations WHERE citation_id=? AND version_id=?',
                    (row['id'], row['version_id'])) if _usable_edge(db, e, as_of)]
                for field, kind in (('product_id', 'product'),
                                    ('share_class_id', 'share_class'), ('channel_id', 'channel')):
                    public_ref = row[field]
                    if not public_ref or (kind, public_ref) in seen:
                        continue
                    gid = entity_id(kind, public_ref)
                    if not any(e['subject_id'] == gid or e['object_id'] == gid for e in edges):
                        continue
                    parent = row['product_id'] if kind == 'share_class' else None
                    if kind == 'share_class' and not any(
                            e['predicate'] == 'has_share_class'
                            and e['subject_id'] == entity_id('product', parent)
                            and e['object_id'] == gid for e in edges):
                        continue
                    named = db.execute("SELECT label FROM graph_entities WHERE id=? AND scope='public'",
                                       (gid,)).fetchone()
                    if not named or not isinstance(named['label'], str):
                        continue
                    records.append({'label': named['label'], 'kind': kind,
                                    'public_ref': public_ref, 'parent_product_ref': parent,
                                    'citation_id': row['id']})
                    seen.add((kind, public_ref))
                    if len(records) == limit:
                        return {'schema': 'navren.public-identity-catalog/1', 'release_id': release_id,
                                'as_of': as_of, 'entities': records}
        return {'schema': 'navren.public-identity-catalog/1', 'release_id': release_id,
                'as_of': as_of, 'entities': records}

    @_market_operation()
    def public_catalog(self, *, limit=5000):
        """稳定公共身份允许名单；条款过期不把历史公开对象变成私有对象。"""
        if type(limit) is not int or not 1<=limit<=5000:raise KnowledgeError('invalid_limit')
        terms=[];identifiers=[];seen=set()
        with self.db() as db:
            db.execute('BEGIN')
            checked={}
            rows=db.execute('''SELECT DISTINCT c.*,v.document_id,v.parse_id,v.shape,v.title,v.published_on,v.retrieved_on,
                v.effective_from,v.effective_to,v.status,v.revoked_from,v.parsed_sha256,v.raw_sha256,
                s.enabled,s.read_allowed,s.outbound_allowed,s.endpoints,d.source_id,d.source_url,d.visibility,d.data_origin,p.verified
                FROM releases r JOIN release_chunks rc ON rc.release_id=r.id JOIN chunks c ON c.id=rc.chunk_id
                JOIN document_versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
                JOIN sources s ON s.id=d.source_id LEFT JOIN products p ON p.id=c.product_id
                WHERE r.sealed=1 AND r.temporary=0 ORDER BY v.created_at DESC,c.id''')
            for row in rows:
                if not self._market_available(db,row):continue
                if (row['status']!='verified' or row['visibility']!='public' or row['data_origin']!='real'
                        or not row['enabled'] or not row['read_allowed'] or not row['outbound_allowed']):continue
                if row['product_id'] and not row['verified']:continue
                if not any(row['source_url']==e or (e.endswith('/') and row['source_url'].startswith(e)) for e in json.loads(row['endpoints'])):continue
                if not self._quote_valid(db,row,row,checked):continue
                for key,kind in (('product_id','product'),('share_class_id','share_class'),('channel_id','channel')):
                    ref=row[key]
                    if ref and ref not in seen:
                        if len(seen)>=limit:break
                        # 名称固定在首次准入图实体；候选更新不能把新标签塞进出站词典。
                        named=db.execute('''SELECT ge.label FROM graph_entities ge JOIN graph_relations gr
                            ON (gr.subject_id=ge.id OR gr.object_id=ge.id) JOIN graph_provenance gp ON gp.relation_id=gr.id
                            WHERE ge.kind=? AND ge.public_ref=? AND gr.citation_id=? AND gr.status='verified' LIMIT 1''',
                            (kind,ref,row['id'])).fetchone()
                        if named is None:continue
                        identifiers.append(ref);seen.add(ref)
                        name=named['label']
                        if name not in seen and len(seen)<limit:terms.append(name);seen.add(name)
                for facet in json.loads(row['facets']):
                    for concept in PUBLIC_CONCEPTS[facet]:
                        if concept not in seen and len(seen)<limit:terms.append(concept);seen.add(concept)
                if len(seen)>=limit:break
        return {'terms':terms,'identifiers':identifiers}
