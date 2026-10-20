"""本地公开研究资料库：原件、字段证据、关系与不可变发布；不授予交易权限。

无模型、无网络、无私人资料入口。JSON 资料包通过同一发布锁导入本地 SQLite。
guard 钉住已校验发布，其他进程的发布在上下文结束前阻塞。
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

CONTRACT = 'navren.research-evidence/1'
DEFAULT_ROOT = Path(__file__).resolve().parent / 'data/research_evidence'
STATES = {'verified', 'missing', 'conflict', 'stale', 'superseded', 'restricted'}
FACT_REQUIRED = {'entity_id', 'field', 'value', 'unit', 'state', 'source_scope',
                 'source_version', 'published_at', 'effective_from', 'effective_to',
                 'locator', 'missing_reason'}
IDENT = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()
SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(revision TEXT PRIMARY KEY,published_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects(hash TEXT PRIMARY KEY,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(
 revision TEXT NOT NULL,entity_id TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,
 customer_group TEXT NOT NULL,search_text TEXT NOT NULL,capabilities TEXT NOT NULL,
 object_hash TEXT NOT NULL,PRIMARY KEY(revision,entity_id));
CREATE INDEX IF NOT EXISTS entity_filters ON entities(revision,kind,customer_group);
CREATE TABLE IF NOT EXISTS documents(
 revision TEXT NOT NULL,document_id TEXT NOT NULL,object_hash TEXT NOT NULL,
 PRIMARY KEY(revision,document_id));
CREATE TABLE IF NOT EXISTS relations(
 revision TEXT NOT NULL,source TEXT NOT NULL,predicate TEXT NOT NULL,target TEXT NOT NULL,
 body TEXT NOT NULL,PRIMARY KEY(revision,source,predicate,target));
CREATE INDEX IF NOT EXISTS relation_lookup ON relations(revision,source);
'''


class ResearchEvidenceError(ValueError):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code, self.message, self.status, self.status_code = code, message, status, status


class EvidenceChanged(ResearchEvidenceError):
    def __init__(self, message='公开研究资料版本已变化，请重新读取。'):
        super().__init__('RESEARCH_EVIDENCE_CHANGED', message, 409)


class DependencyUnavailable(ResearchEvidenceError):
    def __init__(self, message='公开研究原件或资料包无法核验。'):
        super().__init__('RESEARCH_DEPENDENCY_UNAVAILABLE', message, 503)


def _invalid(message='研究查询或资料格式无效。'):
    raise ResearchEvidenceError('RESEARCH_INVALID', message, 422)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _semantic(value):
    if isinstance(value, dict):
        return {k: _semantic(v) for k, v in value.items() if k not in ('fetched_at', 'checked_at')}
    if isinstance(value, list):
        return [_semantic(v) for v in value]
    return value


def _digest(value):
    return hashlib.sha256(_json(_semantic(value)).encode()).hexdigest()


def _ident(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        _invalid('实体或来源 ID 无效。')
    return value


def _day(value):
    if value is None:
        return None
    if not isinstance(value, str):
        _invalid('资料日期须为 ISO 日期、带时区时刻或 null。')
    try:
        if 'T' in value:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                _invalid('资料时刻须保留明确时区。')
            return moment.date()
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except ValueError:
        pass
    _invalid('资料日期须为 ISO 日期、带时区时刻或 null。')


def _public_url(value):
    if not isinstance(value, str):
        _invalid('原件 URL 缺失。')
    u = urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        _invalid('原件仅允许无凭证的 HTTPS 公开 URL。')
    return value


class ResearchEvidenceStore:
    """导入本地受信资料包；新发布追加，旧发布和原件保留。"""

    def __init__(self, root=None, *, db_path=None, clock=None):
        self.root = Path(root or DEFAULT_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else self.root / 'index.sqlite3'
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + '.lock')
        self.clock = clock or (lambda: datetime.now(ZoneInfo('Asia/Shanghai')).date())
        self._local = threading.local()
        self._last_input, self._current = None, None
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.lock_path.resolve()), threading.RLock())
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _path(self, relative):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            _invalid('原件必须使用资料库内相对路径。')
        path = self.root / relative
        if '..' in Path(relative).parts or any(p.is_symlink() for p in [path, *path.parents] if p != self.root.parent):
            _invalid('原件路径越界或含符号链接。')
        if not path.resolve().is_relative_to(self.root):
            _invalid('原件路径越界。')
        return path

    def _load(self, path):
        if not path.is_file() or path.stat().st_size > 20_000_000:
            raise DependencyUnavailable('资料包缺失或超出本地读取上限。')
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise DependencyUnavailable('资料包未完整写入或 JSON 无效。') from exc

    @contextmanager
    def _publication_lock(self):
        with self._lock, self.lock_path.open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _today(self):
        value = self.clock()
        if isinstance(value, datetime):
            value = value.date()
        return value if isinstance(value, date) else date.fromisoformat(value)

    def _read_inputs(self, manifest=None):
        manifest = self._load(self.root / 'manifest.json') if manifest is None else manifest
        if manifest.get('schema_version') != 1 or manifest.get('data_scope') != 'public':
            raise DependencyUnavailable('只接受 v1 公开资料清单。')
        if 'bundles' in manifest:
            bundles = []
            for entry in manifest['bundles']:
                path = self._path(entry['path'])
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                    raise DependencyUnavailable('已发布资料包的固定版本无法核验。')
                bundles.append(self._load(path))
        else:
            bundles = [self._load(p) for p in sorted(self.root.glob('*/bundle.json'))]
        docs = [manifest['catalog'], *manifest.get('documents', [])]
        for bundle in bundles:
            if bundle.get('schema_version') != 1:
                raise DependencyUnavailable('资料包版本不兼容。')
            docs.extend(bundle.get('documents', []))
        signature = [manifest, bundles, self._today().isoformat()]
        observed, archives = {}, {}
        for doc in docs:
            path = self._path(doc.get('path'))
            if not path.is_file() or path.stat().st_size > 40_000_000:
                observed[doc['document_id']] = None
            else:
                observed[doc['document_id']] = hashlib.sha256(path.read_bytes()).hexdigest()
            if not isinstance(doc.get('sha256'), str) or not SHA.fullmatch(doc['sha256']):
                _invalid('文档 SHA-256 无效。')
            archive = self._path('.objects/' + doc['sha256'])
            archives[doc['sha256']] = hashlib.sha256(archive.read_bytes()).hexdigest() if archive.is_file() else None
        return manifest, bundles, docs, observed, _digest([signature, observed, archives])

    def _document(self, raw, observed):
        required = {'document_id', 'url', 'sha256', 'path', 'document_type', 'source_scope',
                    'published_at', 'effective_from', 'effective_to', 'fetched_at'}
        if not isinstance(raw, dict) or not required <= raw.keys():
            _invalid('文档缺少来源、版本或日期字段。')
        _ident(raw['document_id'])
        _public_url(raw['url'])
        if not isinstance(raw['sha256'], str) or not SHA.fullmatch(raw['sha256']):
            _invalid('文档 SHA-256 无效。')
        for key in ('published_at', 'effective_from', 'effective_to'):
            _day(raw[key])
        if raw.get('access', 'public') != 'public':
            _invalid('受限或私有原件不得进入公开资料库。')
        doc = {k: deepcopy(v) for k, v in raw.items() if k != 'path'}
        doc['source_id'] = raw.get('source_id', raw['document_id'])
        _ident(doc['source_id'])
        doc['source_version'] = raw.get('source_version', raw['sha256'])
        doc['integrity'] = ('verified' if observed.get(raw['document_id']) == raw['sha256']
                            else 'missing' if observed.get(raw['document_id']) is None else 'conflict')
        doc['observed_sha256'] = observed.get(raw['document_id'])
        if doc['integrity'] == 'verified':
            archive = self.root / '.objects' / raw['sha256']
            archive.parent.mkdir(exist_ok=True)
            if not archive.exists():
                shutil.copyfile(self._path(raw['path']), archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != raw['sha256']:
                raise DependencyUnavailable('原件归档字节校验失败。')
        return doc

    @staticmethod
    def _audience(raw):
        raw = raw or ''
        personal, institution = '个人' in raw, '机构' in raw
        return 'both' if personal and institution else 'individual' if personal else 'institution' if institution else 'unknown'

    def _fact(self, raw, entity_id, docs):
        if not isinstance(raw, dict) or not FACT_REQUIRED <= raw.keys() or raw['entity_id'] != entity_id:
            _invalid('字段事实缺少必要证据属性或实体不一致。')
        if raw['state'] not in STATES:
            _invalid('未知事实状态。')
        fact = deepcopy(raw)
        for key in ('published_at', 'effective_from', 'effective_to'):
            _day(fact[key])
        doc = docs.get(fact.get('document_id'))
        if fact['state'] == 'verified':
            if not doc or not fact.get('locator'):
                _invalid('已核验字段必须绑定原件和具体定位。')
            if (fact.get('source_sha256', doc['sha256']) != doc['sha256']
                    or fact['source_version'] != doc['source_version']
                    or fact.get('source_url', doc['url']) != doc['url']):
                _invalid('字段与原件版本或 URL 不一致。')
            fact.update(source_id=doc['source_id'], source_url=doc['url'], source_sha256=doc['sha256'])
            if doc['integrity'] != 'verified':
                fact.update(state=doc['integrity'], missing_reason='source_bytes_' + doc['integrity'])
            start, end = _day(fact['effective_from']), _day(fact['effective_to'])
            if start and end and start > end:
                _invalid('字段生效区间倒置。')
            if end and self._today() > end:
                fact.update(state='stale', missing_reason='effective_period_expired')
            if start and self._today() < start:
                fact.update(state='missing', missing_reason='not_yet_effective')
            if doc.get('source_observation', {}).get('status') in ('changed', 'unavailable'):
                fact.update(state='stale', missing_reason='source_changed_or_unavailable_pending_recheck')
            as_of = fact.get('value', {}).get('as_of') if isinstance(fact.get('value'), dict) else None
            if fact['field'].startswith('performance.') and as_of and (self._today() - _day(as_of)).days > 7:
                fact.update(state='stale', missing_reason='dated_performance_older_than_seven_days')
        elif fact.get('document_id') in docs:
            fact.setdefault('source_id', docs[fact['document_id']]['source_id'])
        if fact['state'] != 'verified' and not fact.get('missing_reason'):
            _invalid('未核验字段必须说明原因。')
        return fact

    def _build(self, manifest, bundles, raw_docs, observed):
        docs, source_versions = {}, {}
        for raw in raw_docs:
            doc = self._document(raw, observed)
            check = manifest.get('source_checks', {}).get(doc['document_id'])
            if check and check.get('status') in ('changed', 'unavailable'):
                doc['source_observation'] = {k: v for k, v in check.items() if k != 'checked_at'}
            previous = docs.get(doc['document_id'])
            if previous and previous != doc:
                _invalid('同一发布内出现重复来源 ID 的不同版本。')
            if doc['source_id'] in source_versions and source_versions[doc['source_id']] != doc['source_version']:
                _invalid('同一来源 ID 的多个版本不能在一次发布中静默覆盖。')
            source_versions[doc['source_id']] = doc['source_version']
            docs[doc['document_id']] = doc
        source = manifest['catalog']
        if docs[source['document_id']]['integrity'] != 'verified':
            raise DependencyUnavailable('完整渠道目录原件无法通过 SHA 校验。')
        blob = self._path(source['path']).read_text(encoding='utf-8-sig')
        match = re.fullmatch(r'\s*_jsonpproductpresentation\((.*)\)\s*;?\s*', blob, re.S)
        if not match:
            raise DependencyUnavailable('渠道目录包装发生变化。')
        catalog = json.loads(match[1])
        _day(catalog['extractDate'])
        rows = catalog['productPresentation']
        if not isinstance(rows, list) or not rows or len(rows) > 100_000:
            raise DependencyUnavailable('渠道目录范围无效。')
        entities, codes = {}, set()
        kinds = {'开放式基金': 'fund_share', '理财产品': 'wealth_product', '保险': 'insurance_offer',
                 '集合资产管理计划': 'asset_management_product', '集合资金信托计划': 'trust_product'}
        quarantine = manifest.get('quarantines', {})
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ('code', 'name', 'type')):
                raise DependencyUnavailable('目录身份字段缺失。')
            code = row['code']
            if code in codes:
                raise DependencyUnavailable('渠道代码重复，禁止静默去重。')
            codes.add(code)
            kind = kinds.get(row['type'], 'other_product')
            eid = 'fund:CN:' + code if kind == 'fund_share' and re.fullmatch(r'\d{6}', code) else 'offer:WEBANK:' + code
            _ident(eid)
            entity = dict(entity_id=eid, kind=kind, code=code, name=row['name'], customer_group=self._audience(row.get('customerGroupType', '')),
                          channel_scope='WEBANK', catalog_record=row, catalog_document_id=source['document_id'],
                          catalog_index=index, catalog_source_date=catalog['extractDate'], document_ids=[source['document_id']],
                          facts=[], relations=[], limitations=['渠道目录仅证明历史披露，不证明当前可购买、承保或执行资格。'])
            if code in quarantine:
                entity['quarantine'] = deepcopy(quarantine[code])
                entity['limitations'].append('主体身份冲突已隔离，禁止自动关联推荐或执行。')
                entity['document_ids'] += quarantine[code].get('document_ids', [])
            entities[eid] = entity
        deep_ids = set()
        for bundle in bundles:
            for raw in bundle.get('entities', []):
                if any(key in raw for key in ('registered_user_id', 'workspace_id', 'private', 'attachment_ids')):
                    _invalid('私有字段不得进入公开研究实体。')
                eid = _ident(raw['entity_id'])
                if eid in deep_ids:
                    _invalid('同一发布内重复定义深度实体。')
                deep_ids.add(eid)
                old = entities.get(eid)
                if old and old.get('quarantine'):
                    _invalid('隔离实体不能由资料包静默解除隔离。')
                if old and old['kind'] != raw.get('kind'):
                    _invalid('资料包试图更换实体类别。')
                entity = deepcopy(old) if old else {}
                entity.update({k: deepcopy(v) for k, v in raw.items() if k not in ('facts', 'capabilities', 'document_ids')})
                entity['customer_group'] = raw.get('customer_group', raw.get('customer_type', entity.get('customer_group', 'unknown')))
                if entity['customer_group'] not in ('individual', 'institution', 'both', 'unknown'):
                    _invalid('客户群分类无效。')
                entity['document_ids'] = sorted(set(entity.get('document_ids', []) + raw.get('document_ids', [])))
                if not entity['document_ids'] or any(d not in docs for d in entity['document_ids']):
                    _invalid('实体引用未注册文档。')
                entity['facts'] = [self._fact(f, eid, docs) for f in raw.get('facts', [])]
                for fact in entity['facts']:
                    if fact.get('document_id') and fact['document_id'] not in entity['document_ids']:
                        _invalid('字段来源不属于该实体资料包。')
                    bindings = fact.get('locator', {}).get('bindings', []) if isinstance(fact.get('locator'), dict) else []
                    if bindings:
                        dependencies = {fact['source_id']: fact['source_version']} if fact.get('source_id') else {}
                        for binding in bindings:
                            did = binding.get('document_id') if isinstance(binding, dict) else None
                            if did not in entity['document_ids'] or not binding.get('locator') or not binding.get('field'):
                                _invalid('组合事实必须绑定本实体原件、字段和具体定位。')
                            peers = [f for f in entity['facts'] if f['field'] == binding['field']
                                     and f.get('document_id') == did and f.get('locator') == binding['locator']]
                            if not peers:
                                _invalid('组合事实引用未注册的字段证据。')
                            dependencies[docs[did]['source_id']] = docs[did]['source_version']
                            bad = next((f for f in peers if f['state'] != 'verified'), None)
                            if bad and fact['state'] == 'verified':
                                fact.update(state=bad['state'], missing_reason='composite_dependency_' + bad['state'])
                        fact['dependency_versions'] = dependencies
                entity.setdefault('relations', [])
                entity.setdefault('limitations', [])
                if entity.get('kind') == 'fund_share':
                    identities = [f for f in entity['facts'] if f['state'] == 'verified' and f['field'] == 'identity.code']
                    if identities and any(f['value'] != eid.rsplit(':', 1)[-1] for f in identities):
                        _invalid('基金份额代码与原件身份不一致。')
                    managers = [f for f in entity['facts'] if f['state'] == 'verified' and f['field'] == 'identity.manager']
                    publisher = entity.get('catalog_record', {}).get('publish')
                    if publisher and managers and any(f['value'] != publisher for f in managers):
                        entity['quarantine'] = {'state': 'conflict', 'reason': 'catalog_publish_label_disagrees_with_manager_identity'}
                entities[eid] = entity
        for eid, entity in entities.items():
            review = manifest.get('entity_review_flags', {}).get(eid, {})
            performance_check = manifest.get('performance_checks', {}).get(eid, {})
            for fact in entity['facts']:
                if fact['state'] == 'verified' and (
                        review.get('status') in ('changed', 'unavailable') and not fact['field'].startswith('performance.')
                        or performance_check.get('status') == 'unavailable' and fact['field'].startswith('performance.')):
                    fact.update(state='stale', missing_reason='related_disclosure_needs_recheck')
            available = [f for f in entity['facts'] if f['state'] == 'verified']
            caps = ['catalog'] if entity.get('catalog_record') else ['public_research']
            if available:
                caps += ['field_evidence', 'documents']
            if any(f['field'] == 'fees' and f.get('channel') == 'manager_standard' for f in available):
                caps.append('manager_standard_cost_scenario')
            if any(f['field'] == 'fees' and f.get('channel') == 'WEBANK' for f in available):
                caps.append('channel_cost_scenario')
            if any(f['field'] in ('coverage', 'coverage_rules') or f['field'].startswith('coverage.') for f in available):
                caps.append('coverage_evidence')
            if entity.get('kind') == 'pension_sample' and available:
                caps.append('pension_mechanism_evidence')
            if entity.get('quarantine'):
                caps = ['catalog', 'identity_conflict']
            entity.update(capabilities=caps, executable=False, data_scope='public')
            evidence_docs = [{k: v for k, v in docs[d].items() if k not in ('fetched_at',)} for d in entity['document_ids']]
            entity['evidence_version'] = _digest([entity, evidence_docs])
            entity['entity_version'] = entity['evidence_version']
        return entities, docs

    def _refresh_locked(self, *, inputs=None, snapshot=None):
        try:
            manifest, bundles, raw_docs, observed, signature = inputs if inputs is not None else self._read_inputs()
            if signature == self._last_input and self._current:
                return self._current
            entities, docs = snapshot if snapshot is not None else self._build(manifest, bundles, raw_docs, observed)
            objects = {}
            entity_hash = {eid: _digest(e) for eid, e in entities.items()}
            doc_hash = {did: _digest(d) for did, d in docs.items()}
            revision = _digest([CONTRACT, entity_hash, doc_hash])
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM revisions WHERE revision=?', (revision,)).fetchone():
                    for eid, entity in entities.items():
                        objects[entity_hash[eid]] = _json(entity)
                    for did, doc in docs.items():
                        objects[doc_hash[did]] = _json(doc)
                    db.executemany('INSERT OR IGNORE INTO objects VALUES(?,?)', objects.items())
                    db.execute('INSERT INTO revisions VALUES(?,?)', (revision, datetime.now(timezone.utc).isoformat()))
                    db.executemany('INSERT INTO entities VALUES(?,?,?,?,?,?,?,?)', [
                        (revision, eid, e['kind'], e['name'], e['customer_group'],
                         ' '.join(str(e.get(k, '')) for k in ('name', 'code', 'entity_id', 'family_id')).casefold(),
                         _json(e['capabilities']), entity_hash[eid]) for eid, e in entities.items()])
                    db.executemany('INSERT INTO documents VALUES(?,?,?)', [(revision, d, h) for d, h in doc_hash.items()])
                    edges = []
                    for eid, e in entities.items():
                        for did in e['document_ids']:
                            edges.append((revision, eid, 'supported_by', did, _json({'source': eid, 'predicate': 'supported_by', 'target': did})))
                        if e.get('family_id'):
                            edges.append((revision, eid, 'share_of', e['family_id'], _json({'source': eid, 'predicate': 'share_of', 'target': e['family_id']})))
                        for relation in e.get('relations', []):
                            if isinstance(relation, dict) and relation.get('predicate') and relation.get('target'):
                                edges.append((revision, eid, str(relation['predicate']), str(relation['target']), _json(relation)))
                    db.executemany('INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?)', edges)
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('current_revision', revision))
            self._current, self._last_input = revision, signature
            return revision
        except ResearchEvidenceError:
            raise
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            raise DependencyUnavailable('公开资料导入失败：' + type(exc).__name__) from exc

    def revision(self):
        if getattr(self._local, 'revision', None):
            return self._local.revision
        with self._publication_lock():
            return self._refresh_locked()

    @staticmethod
    def _atomic_bytes(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def publish_catalog_snapshot(self, body, *, source_url, fetched_at,
                                 expected_sha256, expected_revision=None):
        """接收既有目录worker的原始快照；无网络。无效候选不替换上一有效发布。

        仅基座的受信同步入口调用，不作为用户/模型工具暴露。所有发布与guard同锁。
        """
        if getattr(self._local, 'revision', None):
            raise EvidenceChanged('当前上下文已锁定旧发布，不能同时更新目录。')
        if (not isinstance(body, bytes) or not body or len(body) > 12 * 1024 * 1024
                or not isinstance(expected_sha256, str) or not SHA.fullmatch(expected_sha256)
                or hashlib.sha256(body).hexdigest() != expected_sha256):
            _invalid('目录快照字节或声明 SHA 无效。')
        if not isinstance(fetched_at, str) or 'T' not in fetched_at:
            _invalid('抓取时间须为带时区的完整时刻。')
        _day(fetched_at)
        # 复用现役目录解析器的完整字段/大小/唯一代码约束；导入不启动线程或网络。
        from webank_catalog import decode_catalog
        try:
            source_day, _ = decode_catalog(body)
            if source_day > self._today():
                _invalid('目录披露日期不能在未来。')
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            if isinstance(exc, ResearchEvidenceError):
                raise
            _invalid('目录快照结构或来源日期无效。')
        with self._publication_lock():
            current = self._refresh_locked()
            if expected_revision is not None and expected_revision != current:
                raise EvidenceChanged()
            manifest_path = self.root / 'manifest.json'
            previous_bytes = manifest_path.read_bytes()
            manifest = json.loads(previous_bytes)
            old = manifest['catalog']
            if source_url != old['url']:
                _invalid('同步目录必须来自已登记的原始公开 URL。')
            old_source_day, _ = decode_catalog(self._path(old['path']).read_bytes())
            if source_day < old_source_day:
                _invalid('拒绝回退到更早的目录披露日期。')
            result = dict(provider_revision=current, catalog_sha256=expected_sha256,
                          source_date=source_day.isoformat(), fetched_at=fetched_at)
            if expected_sha256 == old['sha256']:
                return dict(status='unchanged', **result)
            relative = '.snapshots/catalog/' + expected_sha256 + '.jsonp'
            path = self._path(relative)
            if path.exists():
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
                    raise DependencyUnavailable('同名不可变目录原件已损坏。')
            else:
                self._atomic_bytes(path, body)
            candidate = deepcopy(manifest)
            candidate['catalog'] = dict(old, path=relative, sha256=expected_sha256,
                source_version=('sha256:' if old['source_version'].startswith('sha256:') else '') + expected_sha256,
                source_date=source_day.isoformat(), published_at=None, fetched_at=fetched_at)
            try:
                inputs = self._read_inputs(candidate)
                snapshot = self._build(*inputs[:4])
            except ResearchEvidenceError:
                raise
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise DependencyUnavailable('同步候选未通过完整资料校验。') from exc
            # 原件和完整候选先核验，再切指针；竞争读者均受同一发布锁保护。
            try:
                self._atomic_bytes(manifest_path, (_json(candidate) + '\n').encode())
                revision = self._refresh_locked(inputs=inputs, snapshot=snapshot)
            except Exception as exc:
                self._atomic_bytes(manifest_path, previous_bytes)
                self._last_input, self._current = None, None
                if isinstance(exc, ResearchEvidenceError):
                    raise
                raise DependencyUnavailable('同步候选发布失败，已保留上一有效版本。') from exc
            return dict(status='published', **dict(result, provider_revision=revision))

    @contextmanager
    def guard(self, expected_revision):
        """固定公共发布。所有发布走同一跨进程锁；上下文内调用不自动换版。"""
        if not isinstance(expected_revision, str) or not SHA.fullmatch(expected_revision):
            _invalid('expected_revision 必须为公共提供端发布哈希。')
        pinned = getattr(self._local, 'revision', None)
        if pinned:
            if pinned != expected_revision:
                raise EvidenceChanged()
            yield self
            return
        with self._publication_lock():
            revision = self._refresh_locked()
            if revision != expected_revision:
                raise EvidenceChanged()
            self._local.revision = revision
            try:
                yield self
            finally:
                self._local.revision = None

    def _profile(self, entity_id, revision):
        _ident(entity_id)
        with self._connect() as db:
            row = db.execute('SELECT o.body FROM entities e JOIN objects o ON o.hash=e.object_hash WHERE e.revision=? AND e.entity_id=?', (revision, entity_id)).fetchone()
        if not row:
            raise ResearchEvidenceError('RESEARCH_NOT_FOUND', '该实体未进入此公开发布。', 404)
        return json.loads(row['body'])

    def _docs(self, ids, revision):
        with self._connect() as db:
            result = []
            for did in ids:
                row = db.execute('SELECT o.body FROM documents d JOIN objects o ON o.hash=d.object_hash WHERE d.revision=? AND d.document_id=?', (revision, did)).fetchone()
                if not row:
                    raise DependencyUnavailable('已发布文档索引不完整。')
                result.append(json.loads(row['body']))
        return result

    def catalog(self, query=None):
        if query is None:
            query = {}
        if isinstance(query, str):
            query = {'q': query}
        if not isinstance(query, dict) or set(query) - {'q', 'kind', 'customer_group', 'limit', 'offset', 'capability'}:
            _invalid()
        q = query.get('q', '')
        if not isinstance(q, str) or len(q) > 200:
            _invalid()
        def integer(key, default, maximum):
            value = query.get(key, default)
            if isinstance(value, str) and re.fullmatch(r'\d{1,6}', value):
                value = int(value)
            if type(value) is not int or not (0 if key == 'offset' else 1) <= value <= maximum:
                _invalid()
            return value
        limit, offset = integer('limit', 50, 200), integer('offset', 0, 100_000)
        revision = self.revision()
        clauses, args = ['e.revision=?'], [revision]
        if q:
            clauses.append('instr(e.search_text,?)>0'); args.append(q.strip().casefold())
        for key in ('kind', 'customer_group'):
            value = query.get(key)
            if value:
                if not isinstance(value, str) or len(value) > 80:
                    _invalid()
                clauses.append('e.' + key + '=?'); args.append(value)
        if query.get('capability'):
            value = query['capability']
            if not isinstance(value, str) or len(value) > 80:
                _invalid()
            clauses.append('instr(e.capabilities,?)>0'); args.append(_json(value))
        where = ' AND '.join(clauses)
        with self._connect() as db:
            total = db.execute('SELECT count(*) FROM entities e WHERE ' + where, args).fetchone()[0]
            rows = db.execute('SELECT o.body FROM entities e JOIN objects o ON o.hash=e.object_hash WHERE ' + where + ' ORDER BY e.name,e.entity_id LIMIT ? OFFSET ?', [*args, limit, offset]).fetchall()
            facets = [dict(r) for r in db.execute('SELECT kind,customer_group,count(*) AS count FROM entities WHERE revision=? GROUP BY kind,customer_group', (revision,))]
        items = []
        for row in rows:
            entity = json.loads(row['body'])
            items.append({k: entity.get(k) for k in ('entity_id', 'kind', 'name', 'code', 'family_id', 'customer_group', 'channel_scope', 'catalog_source_date', 'capabilities', 'limitations', 'quarantine', 'evidence_version', 'entity_version', 'executable')})
        return dict(contract=CONTRACT, data_scope='public', provider_revision=revision, items=items,
                    total=total, offset=offset, limit=limit, has_more=offset + len(items) < total,
                    facets=facets, executable=False)

    def detail(self, entity_id):
        revision = self.revision()
        entity = self._profile(entity_id, revision)
        documents = self._docs(entity['document_ids'], revision)
        if entity.get('catalog_record'):
            doc = next(d for d in documents if d['document_id'] == entity['catalog_document_id'])
            for key, field in (('publish', 'catalog_publisher_label'), ('risk', 'channel_risk_rating'),
                               ('saleStatus', 'sale_status_at_source_date'), ('customerGroupType', 'channel_customer_group')):
                value = entity['catalog_record'].get(key)
                entity['facts'].append(dict(entity_id=entity_id, field=field, value=value,
                    unit=None, state='verified' if value else 'missing', source_scope='channel_catalog', source_id=doc['source_id'],
                    source_version=doc['source_version'], source_url=doc['url'], source_sha256=doc['sha256'],
                    published_at=doc['published_at'], source_date=entity['catalog_source_date'], effective_from=None, effective_to=None,
                    document_id=doc['document_id'], locator={'json_pointer': '/productPresentation/' + str(entity['catalog_index']) + '/' + key},
                    missing_reason=None if value else 'channel_field_not_disclosed', channel='WEBANK'))
        entity.update(contract=CONTRACT, provider_revision=revision, documents=documents,
                      source_versions={d['source_id']: d['source_version'] for d in documents}, execution_authority=False)
        with self._connect() as db:
            entity['graph'] = [json.loads(r[0]) for r in db.execute('SELECT body FROM relations WHERE revision=? AND source=? ORDER BY predicate,target', (revision, entity_id))]
        return entity

    def documents(self, entity_id):
        revision = self.revision()
        entity = self._profile(entity_id, revision)
        return dict(contract=CONTRACT, data_scope='public', entity_id=entity_id, provider_revision=revision,
                    evidence_version=entity['evidence_version'], items=self._docs(entity['document_ids'], revision))

    def document_bytes(self, document_id, *, revision=None):
        """仅本地原件读取；历史发布必须显式指定，不改变当前发布。"""
        revision = revision or self.revision()
        doc = self._docs([_ident(document_id)], revision)[0]
        if doc['integrity'] != 'verified':
            raise DependencyUnavailable('该版本原件未通过完整性校验。')
        path = self.root / '.objects' / doc['sha256']
        if not path.is_file():
            raise DependencyUnavailable('该版本原件归档缺失。')
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != doc['sha256']:
            raise DependencyUnavailable('原件归档已变化，禁止返回。')
        return payload

    def refresh_plan(self, entity_id):
        """给既有worker或显式查询的有界检查计划；本方法不联网。"""
        revision = self.revision()
        entity = self._profile(entity_id, revision)
        documents = self._docs(entity['document_ids'], revision)
        checks = self._load(self.root / 'manifest.json').get('source_checks', {})
        rows = [dict(document_id=d['document_id'], url=d['url'], sha256=d['sha256'],
                     check_interval_seconds=86400, purpose='registered_original') for d in documents
                if d['source_scope'] != 'channel_catalog']
        manager = next((f['value'] for f in entity['facts'] if f['field'] == 'identity.manager'), None)
        supported = entity['kind'] == 'fund_share' and manager == '华夏基金管理有限公司' and not entity.get('quarantine')
        if supported:
            code = entity_id.rsplit(':', 1)[-1]
            baseline = self.root / 'funds/raw' / (code + '-jijingonggao.html')
            if baseline.is_file():
                rows.insert(0, dict(document_id='watch:chinaamc:' + code + ':announcements',
                    url='https://www.chinaamc.com/fund/' + code + '/jijingonggao.shtml',
                    sha256=hashlib.sha256(baseline.read_bytes()).hexdigest(),
                    check_interval_seconds=86400, purpose='announcement_index'))
        now = datetime.now(timezone.utc)
        for row in rows:
            checked = checks.get(row['document_id'], {}).get('checked_at')
            try:
                elapsed = (now - datetime.fromisoformat(checked.replace('Z', '+00:00'))).total_seconds()
            except (ValueError, TypeError, AttributeError):
                elapsed = float('inf')
            row['due'] = elapsed >= row['check_interval_seconds'] or elapsed < 0
        return dict(entity_id=entity_id, provider_revision=revision, documents=rows,
                    performance_supported=supported, nav_interval_seconds=3600,
                    automatic_fields=['performance.nav_observations', 'performance.period_returns'] if supported else [],
                    manual_fields='fee/PDF/contract/holdings and new-announcement semantics require original review after SHA change')

    def freshness(self, entity_id):
        revision = self.revision()
        entity = self._profile(entity_id, revision)
        manifest = self._load(self.root / 'manifest.json')
        allowed = set(entity['document_ids']) | {'watch:chinaamc:' + entity_id.rsplit(':', 1)[-1] + ':announcements'}
        return dict(entity_id=entity_id, provider_revision=revision,
            checks=[dict(v, document_id=k) for k, v in manifest.get('source_checks', {}).items() if k in allowed],
            performance_check=manifest.get('performance_checks', {}).get(entity_id),
            notice='原披露日与本次检查时间分列；未核验的新原文不升级为事实。')

    def _apply_observations(self, candidate, entity_id, observations, plan):
        allowed = {row['document_id']: row for row in plan['documents']}
        records = candidate.setdefault('source_checks', {})
        updated = []
        if not isinstance(observations, list) or len(observations) > 16:
            _invalid('单次原文检查最多16项。')
        for item in observations:
            row = allowed.get(item.get('document_id')) if isinstance(item, dict) else None
            if not row or item.get('url') != row['url'] or item.get('status') not in ('ok', 'failed', 'not_modified'):
                _invalid('检查对象不是本实体登记原文或状态无效。')
            checked = item.get('checked_at')
            if not isinstance(checked, str) or 'T' not in checked:
                _invalid('原文检查须记录带时区的检查时刻。')
            _day(checked)
            previous = records.get(row['document_id'], {})
            if previous.get('checked_at') and datetime.fromisoformat(checked.replace('Z', '+00:00')) < datetime.fromisoformat(previous['checked_at'].replace('Z', '+00:00')):
                raise EvidenceChanged('迟到的旧原文检查不能覆盖较新的来源状态。')
            if item['status'] == 'ok':
                body = item.get('body')
                if not isinstance(body, bytes) or not body or len(body) > 40_000_000:
                    _invalid('原文检查缺少有界原始字节。')
                observed = hashlib.sha256(body).hexdigest()
                archive = self._path('.observed/' + observed)
                if not archive.exists():
                    self._atomic_bytes(archive, body)
                elif hashlib.sha256(archive.read_bytes()).hexdigest() != observed:
                    raise DependencyUnavailable('检查原件归档损坏。')
                status = 'unchanged' if observed == row['sha256'] else 'changed'
            elif item['status'] == 'not_modified':
                observed = previous.get('observed_sha256', row['sha256'])
                status = 'unchanged' if observed == row['sha256'] else 'changed'
            else:
                observed, status = previous.get('observed_sha256'), 'unavailable'
            if (previous.get('checked_at') and datetime.fromisoformat(checked.replace('Z', '+00:00')) == datetime.fromisoformat(previous['checked_at'].replace('Z', '+00:00'))
                    and (status, observed) != (previous.get('status'), previous.get('observed_sha256'))):
                raise EvidenceChanged('同一检查时刻的冲突结果不能互相覆盖。')
            record = dict(url=row['url'], checked_at=checked, status=status,
                          observed_sha256=observed, expected_sha256=row['sha256'])
            if status == 'unavailable':
                record['error'] = str(item.get('error', 'source_unavailable'))[:120]
            records[row['document_id']] = record
            if row['purpose'] == 'announcement_index':
                candidate.setdefault('entity_review_flags', {})[entity_id] = record
            updated.append(dict(record, document_id=row['document_id']))
        return updated

    def _commit_manifest(self, candidate):
        previous = (self.root / 'manifest.json').read_bytes()
        inputs = self._read_inputs(candidate)
        snapshot = self._build(*inputs[:4])
        try:
            self._atomic_bytes(self.root / 'manifest.json', (_json(candidate) + '\n').encode())
            return self._refresh_locked(inputs=inputs, snapshot=snapshot)
        except Exception:
            self._atomic_bytes(self.root / 'manifest.json', previous)
            self._last_input, self._current = None, None
            raise

    def apply_source_observations(self, entity_id, observations, *, expected_revision=None):
        """受信公开原文观察入口；变化/失败先失效，保留原件和原事实日期。"""
        if getattr(self._local, 'revision', None):
            raise EvidenceChanged('锁定读取期间不能发布原文检查。')
        plan = self.refresh_plan(entity_id)
        with self._publication_lock():
            before = self._refresh_locked()
            if before != (expected_revision or plan['provider_revision']):
                raise EvidenceChanged()
            candidate = self._load(self.root / 'manifest.json')
            updated = self._apply_observations(candidate, entity_id, observations, plan)
            after = self._commit_manifest(candidate)
        return dict(status='published' if after != before else 'unchanged', provider_revision=after, checks=updated)

    def refresh_entity(self, entity_id, *, performance_provider=None, check_documents=False,
                       max_documents=2, force=False, expected_revision=None):
        """显式、有限公开读取；复用现有适配器，不启动线程/模型。

        华夏净值与明确周期指标可本地解析；其他原文变化只失效，不能凭SHA猜新规则。
        """
        if getattr(self._local, 'revision', None):
            raise EvidenceChanged('请在取得操作版本锁之前完成公开来源重核。')
        if type(max_documents) is not int or not 0 <= max_documents <= 4:
            _invalid('单次文档检查上限为4。')
        plan = self.refresh_plan(entity_id)
        before = plan['provider_revision']
        if expected_revision is not None and expected_revision != before:
            raise EvidenceChanged()
        entity = self._profile(entity_id, before)
        from product_performance import ProductPerformance, _chinaamc, _chinaamc_chart, _navs, PARSER_VERSION
        adapter = performance_provider or getattr(self, '_performance', None)
        if adapter is None:
            adapter = ProductPerformance(cache_dir=self.root / '.performance-cache')
            self._performance = adapter
        product = dict(code=entity.get('code', entity_id.rsplit(':', 1)[-1]),
                       name=entity.get('catalog_record', {}).get('name', entity['name']), publish='华夏基金管理有限公司')
        captured = adapter.read_with_sources(product, force=force) if plan['performance_supported'] else {'result': {'status': 'unsupported'}, 'sources': []}
        result, sources = captured['result'], captured['sources']
        now = datetime.now(timezone.utc).isoformat()
        observations = []
        for source in sources:
            for row in plan['documents']:
                if row['url'] == source['url']:
                    observations.append(dict(document_id=row['document_id'], url=row['url'],
                                             checked_at=now, status='ok', body=source['body']))
        if check_documents:
            deadline = time.monotonic() + 12
            seen = {o['document_id'] for o in observations}
            for row in [r for r in plan['documents'] if (force or r['due']) and r['document_id'] not in seen][:max_documents]:
                try:
                    body = adapter._fetch(row['url'], urlsplit(row['url']).hostname, deadline, [])
                    observations.append(dict(document_id=row['document_id'], url=row['url'], checked_at=now, status='ok', body=body))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    observations.append(dict(document_id=row['document_id'], url=row['url'], checked_at=now, status='failed', error=type(exc).__name__))
        parsed = None
        if plan['performance_supported'] and sources and result.get('status') == 'ok' and result.get('fetch_status') != 'failed':
            try:
                index_url = 'https://www.chinaamc.com/fund/' + product['code'] + '/index.shtml'
                raw = next(s['body'] for s in sources if s['url'] == index_url)
                metrics, nav = _chinaamc(raw, product['code'], product['name'], self._today())
                nav_source, locator = (index_url, raw), {'css': '.table2 .tb .tr', 'columns': ['date', 'unit_nav']}
                chart_url = index_url.rsplit('/', 1)[0] + '/zoust_all.js'
                chart = next((s['body'] for s in sources if s['url'] == chart_url), None)
                if chart is not None:
                    try:
                        chart_nav = _chinaamc_chart(chart, self._today())
                    except (ValueError, TypeError, KeyError):
                        chart_nav = []
                    _navs([(p['date'], p['unit_nav']) for p in nav + chart_nav], self._today())
                    if chart_nav and (not nav or chart_nav[-1]['date'] >= nav[-1]['date']):
                        nav, nav_source, locator = chart_nav, (chart_url, chart), {'json_pointers': ['/ShowData', '/danweijingzhiName']}
                if nav:
                    old_as_of = next((f.get('value', {}).get('as_of') for f in entity['facts'] if f['field'] == 'performance.nav_observations' and isinstance(f.get('value'), dict)), None)
                    if old_as_of and nav[-1]['date'] < old_as_of:
                        result = dict(result, status='unavailable', fetch_status='nav_date_regressed')
                    else:
                        parsed = (nav, metrics, nav_source, locator, (index_url, raw))
            except (ValueError, TypeError, KeyError, StopIteration):
                result = dict(result, status='unavailable', fetch_status='parse_or_identity_failed')
        with self._publication_lock():
            if self._refresh_locked() != before:
                raise EvidenceChanged()
            candidate = self._load(self.root / 'manifest.json')
            updated = self._apply_observations(candidate, entity_id, observations, plan)
            if parsed:
                nav, metrics, nav_source, locator, index_source = parsed
                for reference in candidate.get('bundles', []):
                    bundle = self._load(self._path(reference['path']))
                    target = next((e for e in bundle['entities'] if e['entity_id'] == entity_id), None)
                    if target is None:
                        continue
                    before_bundle = deepcopy(bundle)
                    def observed_fact(field, value, source, location, role):
                        url, body = source
                        sha = hashlib.sha256(body).hexdigest()
                        did = 'automatic:chinaamc:' + product['code'] + ':' + role
                        relative = '.snapshots/research/' + sha
                        path = self._path(relative)
                        if not path.exists():
                            self._atomic_bytes(path, body)
                        doc = dict(document_id=did, source_id=did, url=url, sha256=sha, path=relative,
                            source_version='sha256:' + sha, document_type='manager_performance_observation',
                            source_scope='manager_disclosure', published_at=None, effective_from=None,
                            effective_to=None, fetched_at=now, access='public')
                        candidate.setdefault('source_checks', {})[did] = dict(url=url, checked_at=now,
                            status='unchanged', observed_sha256=sha, expected_sha256=sha)
                        bundle['documents'] = [d for d in bundle['documents'] if d['document_id'] != did] + [doc]
                        if did not in target['document_ids']:
                            target['document_ids'].append(did)
                        return dict(entity_id=entity_id, field=field, value=value, unit=None, state='verified',
                            source_scope='manager_disclosure', source_version=doc['source_version'],
                            published_at=None, effective_from=None, effective_to=None,
                            locator=dict(location, parser_version=PARSER_VERSION), missing_reason=None,
                            document_id=did, source_id=did, source_url=url, source_sha256=sha)
                    fields = {'performance.nav_observations', 'performance.period_returns'}
                    target['facts'] = [f for f in target['facts'] if f['field'] not in fields]
                    target['facts'].append(observed_fact('performance.nav_observations',
                        dict(observations=nav, as_of=nav[-1]['date'], definition='unit_NAV_only_not_adjusted_total_return'), nav_source, locator, 'nav'))
                    if metrics:
                        target['facts'].append(observed_fact('performance.period_returns',
                            dict(metrics=metrics, as_of=max(m['as_of'] for m in metrics)), index_source,
                            {'css': '.table1 .tb .tr', 'date_label': '截止日期'}, 'metrics'))
                    if _digest(bundle) != _digest(before_bundle):
                        payload = (_json(bundle) + '\n').encode()
                        sha = hashlib.sha256(payload).hexdigest()
                        path = self._path('.bundles/' + sha + '.json')
                        if not path.exists():
                            self._atomic_bytes(path, payload)
                        reference.update(path='.bundles/' + sha + '.json', sha256=sha)
                    break
                candidate.setdefault('performance_checks', {})[entity_id] = dict(status='ok', checked_at=now,
                    as_of=nav[-1]['date'], parser_version=PARSER_VERSION)
            elif plan['performance_supported'] and (result.get('status') != 'ok' or result.get('fetch_status') == 'failed'):
                candidate.setdefault('performance_checks', {})[entity_id] = dict(status='unavailable', checked_at=now)
            after = self._commit_manifest(candidate)
        return dict(entity_id=entity_id, status='published' if after != before else 'unchanged',
                    provider_revision=after, automatic_nav_updated=parsed is not None and after != before,
                    automatic_nav_parsed=parsed is not None, checks=updated,
                    performance_status=result.get('fetch_status', result.get('status')), source_calls=len(sources),
                    limitations='Fees/contracts/holdings/new announcement semantics are not automatically inferred.')
