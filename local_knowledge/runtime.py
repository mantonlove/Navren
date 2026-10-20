"""本轮模型调用前的公共证据准备；不持久化提问、账户或个人条件。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import json
import re
import threading
import time
import weakref
from urllib.parse import quote

from .store import KnowledgeError, now, packed, sha, today

PROMOTIONS = '''
CREATE TABLE IF NOT EXISTS evidence_promotions(
 id TEXT PRIMARY KEY,version_ids TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,completed_at TEXT,error_code TEXT);
'''
FACET_WORDS = {
    'fees': ('费用', '费率', '申购费', '赎回费', '收费'),
    'risk': ('风险', '保本', '损失'),
    'eligibility': ('资格', '投资者', '客户'),
    'sale_status': ('在售', '销售', '能买', '申购'),
    'liquidity': ('到账', '赎回', '流动性', '持有期'),
    'returns': ('净值', '收益', '回报'),
}


@dataclass(frozen=True)
class PreparedEvidence:
    provider: object
    report: dict


class RoundProvider:
    """一个只读固定快照；本轮新公共身份不进入全局词典。"""
    def __init__(self, provider, records=(), *, release_id=None,catalog_info=None):
        self.provider = provider
        self.records = tuple(deepcopy(records))
        self.release_id=release_id
        self.catalog_info=deepcopy(catalog_info or {})

    def status(self):
        status={**self.provider.status(),'public_release_id':self.release_id,'ready':bool(self.release_id)}
        if not self.release_id:status.update(graph_ready=False,published_chunk_count=0,published_evidenced_relation_count=0)
        return status

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def public_identity_catalog(self, *, limit=64, as_of=None):
        """Optional bounded capability on this round's exact fixed release."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('invalid_limit')
        method = getattr(self.provider, 'public_identity_catalog', None)
        if not callable(method) or not self.release_id:
            return None
        date_value = as_of or today()
        value = method(release_id=self.release_id, as_of=date_value, limit=limit)
        if (type(value) is not dict or set(value) != {'schema', 'release_id', 'as_of', 'entities'}
                or value['schema'] != 'navren.public-identity-catalog/1'
                or value['release_id'] != self.release_id or value['as_of'] != date_value
                or type(value['entities']) is not list or len(value['entities']) > limit):
            raise KnowledgeError('public_identity_catalog_invalid')
        rows = value['entities']
        keys = {'label', 'kind', 'public_ref', 'parent_product_ref', 'citation_id'}
        if any(type(row) is not dict or set(row) != keys
               or row['kind'] not in ('product', 'share_class', 'channel')
               or any(type(row[k]) is not str or not row[k] for k in ('label', 'public_ref', 'citation_id'))
               or row['parent_product_ref'] is not None and type(row['parent_product_ref']) is not str
               for row in rows):
            raise KnowledgeError('public_identity_catalog_invalid')
        ids = list(dict.fromkeys(row['citation_id'] for row in rows))
        batch = getattr(self, 'observe_identity_batch', None)
        if batch is None:
            batch = self.provider.resolve_public_evidence_batch
        originals = batch(ids, self.release_id, date_value)
        retained = []
        for row in rows:
            original = originals.get(row['citation_id'])
            field = {'product': 'product_id', 'share_class': 'share_class_id', 'channel': 'channel_id'}[row['kind']]
            if (not original or 'general' not in original['facets']
                    or original[field] != row['public_ref']
                    or row['kind'] == 'share_class' and original['product_id'] != row['parent_product_ref']):
                continue
            retained.append(deepcopy(row))
        return {**value, 'entities': retained}

    def public_catalog(self, *, limit=5000):
        catalog = self.provider.public_catalog(limit=limit)
        terms, ids = list(catalog['terms']), list(catalog['identifiers'])
        release = self.release_id
        # 只从当前已封存快照的准入图标签补充；不使用后来可变的产品名称。
        selected = ['WEBANK:' + r['code'] for r in self.records]
        if release and selected:
            from .store import WEBANK_PARSER
            from webank_catalog import URL
            with self.provider.db() as db:
                rows = db.execute('''SELECT DISTINCT ge.kind,ge.public_ref,ge.label,gr.citation_id
                    FROM release_chunks rc JOIN chunks c ON c.id=rc.chunk_id
                    JOIN graph_relations gr ON gr.citation_id=rc.chunk_id
                    JOIN graph_provenance gp ON gp.relation_id=gr.id
                    JOIN graph_entities ge ON (ge.id=gr.subject_id OR ge.id=gr.object_id)
                    WHERE rc.release_id=? AND gr.status='verified'
                    AND ge.kind IN ('product','share_class','channel')
                    AND c.product_id IN (''' + ','.join('?' for _ in selected) + ')',
                    (release, *selected)).fetchall()
                current_ids=[r[0] for r in db.execute('''SELECT c.id FROM release_chunks rc
                    JOIN chunks c ON c.id=rc.chunk_id JOIN document_versions v ON v.id=c.version_id
                    JOIN documents d ON d.id=v.document_id WHERE rc.release_id=?
                    AND d.source_id='webank-direct-disclosure' AND d.source_url=? AND v.parser_id=?
                    AND c.channel_id='WEBANK' AND c.share_class_id IS NULL
                    AND (c.paragraph LIKE '%/name' OR c.paragraph LIKE '%/code')
                    AND c.product_id IN (''' + ','.join('?' for _ in selected) + ')',
                    (release,URL,WEBANK_PARSER,*selected))]
            valid = {}
            for row in rows:
                resolve=getattr(self,'observe_identity',self.provider.resolve_public_evidence)
                cid=row['citation_id']
                if cid not in valid:valid[cid]=resolve(cid,release,today()) is not None
                if not valid[cid]:continue
                if row['public_ref'] and row['public_ref'] not in ids:
                    ids.append(row['public_ref'])
                if row['label'] and row['label'] not in terms:
                    terms.append(row['label'])
            # 图实体保留历史label；新公开名称须从本轮原字段重新回查并绑定。
            batch=getattr(self,'observe_identity_batch',self.provider.resolve_public_evidence_batch)
            for offset in range(0,len(current_ids),64):
                values=batch(current_ids[offset:offset+64],release,today())
                for hit in values.values():
                    if not hit or 'general' not in hit['facets']:continue
                    key=hit['locator']['paragraph'].rsplit('/',1)[-1]
                    try:value=json.loads('{'+hit['excerpt']+'}')[key]
                    except (ValueError,KeyError):continue
                    if key in ('name','code') and isinstance(value,str) and value and value not in terms:terms.append(value)
        return {'terms': terms[:limit], 'identifiers': ids[:limit]}


class RoundProductCatalog:
    """public_products 兼容投影；每个返回字段先从本轮 RAG 原件回查。"""
    def __init__(self, provider, records=()):
        self.provider, self.records = provider, tuple(deepcopy(records))

    def project(self, record, release):
        """只消费同一微众JSONP版本；其他来源有效也不能冒充渠道字段。"""
        from webank_catalog import FIELD_MAP, URL
        from .store import WEBANK_PARSER
        pid='WEBANK:' + record['code']
        with self.provider.db() as db:
            rows=db.execute('''SELECT c.id,c.version_id,v.published_on FROM chunks c
                JOIN release_chunks rc ON rc.chunk_id=c.id
                JOIN document_versions v ON v.id=c.version_id
                JOIN documents d ON d.id=v.document_id
                WHERE rc.release_id=? AND c.product_id=? AND c.channel_id='WEBANK'
                AND c.share_class_id IS NULL AND d.source_id='webank-direct-disclosure'
                AND d.source_url=? AND v.parser_id=? ORDER BY v.version_no DESC,c.id''',
                (release,pid,URL,WEBANK_PARSER)).fetchall()
        versions={}
        for row in rows:versions.setdefault(row['version_id'],[]).append(row['id'])
        field_facets={'charge':'fees','chargeWay':'fees','risk':'risk',
                      'investor':'eligibility','customerGroupType':'eligibility','saleStatus':'sale_status'}
        for ids in versions.values():
            fields, covered, published={},set(),None
            batch=getattr(self.provider,'resolve_public_evidence_batch',None)
            values=batch(ids,release,today()) if batch and len(ids)<=64 else {
                cid:self.provider.resolve_public_evidence(cid,release,today()) for cid in ids}
            for hit in values.values():
                if not hit or hit['source_url']!=URL or hit['channel_id']!='WEBANK':continue
                key=hit['locator']['paragraph'].rsplit('/',1)[-1]
                facet=field_facets.get(key,'general')
                if key not in FIELD_MAP or facet not in hit['facets']:continue
                try:value=json.loads('{' + hit['excerpt'] + '}')[key]
                except (ValueError,KeyError):continue
                fields[FIELD_MAP[key]]=value
                if isinstance(value,str) and value.strip():covered.add(facet)
                published=hit['published_on']
            if fields.get('code')!=record['code'] or not fields.get('name'):continue
            product={**{k:None for k in FIELD_MAP.values()},**fields,
                     'source_url':'https://www.webank.com/product/query/' + quote(record['code'],safe=''),
                     'source_date':published,'sales_status_as_of':published,
                     'current_sale_status':None,'annual_return':None,'executable':False}
            return product,covered
        return None,set()

    def read(self, query='', limit=24, offset=0):
        limit, offset = max(1, min(int(limit), 12)), max(0, int(offset))
        wanted = query.casefold()
        selected = [r for r in self.records if not wanted or wanted in
                    ('WEBANK:' + r['code'] + ' ' +
                     ' '.join(str(r.get(k) or '') for k in ('code', 'name'))).casefold()]
        release = self.provider.status()['public_release_id']
        products, dates, coverage = [], [], {}
        from webank_catalog import FIELD_MAP, PAGE, URL
        for record in selected[offset:offset+limit]:
            product, facets=self.project(record,release)
            if product:
                products.append(product);coverage['WEBANK:'+product['code']]=sorted(facets)
                if product['source_date']:dates.append(product['source_date'])
        source_date = max(dates) if dates else None
        info=getattr(self.provider,'catalog_info',{})
        return {'provider': '微众银行官方公开产品查询', 'source_url': PAGE, 'data_url': URL,
                'source_date': source_date, 'stale': False if products else None,
                'availability_reason':None if products else 'no_matching_verified_object',
                'status': 'current_disclosure' if products else 'unavailable',
                'fetch_status': 'ok' if products else 'unavailable',
                'disclosure_scope': 'dated_channel_snapshot', 'total': len(self.records),
                'matched': len(products), 'offset': offset, 'limit': limit,
                'has_more': offset + limit < len(selected), 'catalog_version': release,
                'total_scope':'selected_round_objects','selected_round_count':len(self.records),
                'pagination_scope':'selected_round_objects',
                'catalog_total_count':info.get('total_count'),
                'catalog_source_date':info.get('source_date'),
                'catalog_truncated':info.get('total_count')>len(self.records) if info.get('total_count') is not None else None,
                'products': products, 'source_policy': 'webank_disclosure_only',
                'field_coverage': coverage,
                'verified_nav_products': [],
                'verified_nav_scope': '底层份额、连续净值与即时申购条件尚未核实。',
                'notice': '只返回本轮选择核验的对象，并非银行完整目录。资料销售状态不是即时可买，完整费率条件、收益及底层份额仍需独立核验。'}


class EvidenceRuntime:
    """本地冷补充与有界后台晋升。网络只读固定源，不发送提问文本。"""
    def __init__(self, provider, *, reader=None, clock=time.monotonic, timeout=8,
                 positive_ttl=300, negative_ttl=30, max_products=12, background=True):
        self.provider, self.reader, self.clock = provider, reader, clock
        self.timeout = max(.01, min(float(timeout), 15))
        self.positive_ttl, self.negative_ttl = positive_ttl, negative_ttl
        self.max_products = max(1, min(int(max_products), 12))
        self.lock = threading.RLock()
        self.catalog_cache = None
        self.catalog_failure_until = 0
        self.catalog_pending = None
        self.invalid_catalog_futures = weakref.WeakSet()
        self.catalog_generation = 0
        self.closed = False
        self.background = background
        self.fetch_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix='evidence-discovery')
        self.promotion_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix='evidence-promotion')
        self.promotion_pending = None
        with provider.db() as db:
            db.executescript(PROMOTIONS)
        if background:
            with provider.db() as db:
                pending = db.execute("SELECT 1 FROM evidence_promotions WHERE status='pending' LIMIT 1").fetchone()
            if pending:
                self._schedule_promotions()

    def _catalog(self):
        from .webank_runtime import ensure_webank_source, read_webank_catalog
        ensure_webank_source(self.provider)
        source = self.provider.source('webank-direct-disclosure')
        if not all(source[k] for k in ('enabled', 'read_allowed', 'outbound_allowed')) or source['paused']:
            raise KnowledgeError('source_permission_missing')
        revision = source['revision']
        with self.lock:
            if self.catalog_cache and self.catalog_cache[0] == revision and self.catalog_cache[1] > self.clock():
                return self.catalog_cache[2], True
            if self.catalog_failure_until > self.clock():
                raise KnowledgeError('source_unavailable')
            if self.catalog_pending in self.invalid_catalog_futures:
                if not self.catalog_pending.done():
                    raise KnowledgeError('source_reader_busy')
                self.catalog_pending = None
            if self.catalog_pending is None:
                self.catalog_generation += 1
                self.catalog_pending = self.fetch_workers.submit(read_webank_catalog, self.provider,
                    timeout=self.timeout, reader=self.reader)
            future = self.catalog_pending
            generation = self.catalog_generation
        try:
            result = future.result(timeout=self.timeout)
        except Exception as error:
            with self.lock:
                self.invalid_catalog_futures.add(future)
                if generation==self.catalog_generation:
                    self.catalog_failure_until = self.clock() + self.negative_ttl
                    if future.done() and self.catalog_pending is future:self.catalog_pending=None
            raise KnowledgeError(error.code if isinstance(error, KnowledgeError) else 'source_unavailable') from None
        with self.lock:
            if future in self.invalid_catalog_futures or generation!=self.catalog_generation:
                raise KnowledgeError('deadline_exceeded')
            self.catalog_cache = (revision, self.clock() + self.positive_ttl, result)
            if self.catalog_pending is future:self.catalog_pending=None
        return result, False

    @staticmethod
    def _match(text, records, maximum):
        financial = any(word in text for word in ('微众', '产品', '理财', '基金'))
        result = []
        for record in records:
            name, code = record['name'], record['code']
            if name in text or ('WEBANK:' + code) in text or (financial and
                    re.search(r'(?<![\w])' + re.escape(code) + r'(?![\w])', text)):
                result.append('WEBANK:' + code)
                if len(result) >= maximum:
                    break
        # 全渠道泛问仅选有限个公开对象；不猜测未知产品名称。
        if not result and any(word in text for word in ('微众有哪些产品', '微众理财产品', '微众产品目录')):
            result = ['WEBANK:' + r['code'] for r in records[:maximum]]
        return result

    def prepare_round(self, text):
        started = self.clock()
        status = self.provider.status()
        base_release = status['public_release_id']
        base = self.provider.for_release(base_release) if base_release else self.provider
        report = {'status': 'not_requested', 'base_release_id': base_release,
                  'release_id': base_release, 'temporary': False, 'catalog_cache_hit': False,
                  'coverage_gaps': [], 'prepared_version_count': 0}
        records = []
        catalog_info={}
        known_selection=set()
        comparison=isinstance(text,str) and any(w in text for w in ('对比','比较','相比','哪个好'))
        # 先本地复用固定正式快照；产品名称问法不依赖“微众”触发词。
        if isinstance(text,str) and base_release:
            from webank_catalog import URL
            from .store import WEBANK_PARSER
            with base.db() as db:
                rows=db.execute('''SELECT DISTINCT v.parsed_text FROM release_chunks rc
                    JOIN chunks c ON c.id=rc.chunk_id JOIN document_versions v ON v.id=c.version_id
                    JOIN documents d ON d.id=v.document_id WHERE rc.release_id=?
                    AND d.source_id='webank-direct-disclosure' AND d.source_url=?
                    AND v.parser_id=? AND c.channel_id='WEBANK' AND v.status='verified'
                    AND (v.revoked_from IS NULL OR v.revoked_from>?) ''',
                    (base_release,URL,WEBANK_PARSER,today())).fetchall()
            known_by_code={}
            for row in rows:
                try:item=json.loads(row[0])
                except ValueError:continue
                if isinstance(item,dict) and isinstance(item.get('name'),str) and isinstance(item.get('code'),str):
                    known_by_code[item['code']]=item
            known=list(known_by_code.values())
            selected=set(self._match(text,known,self.max_products))
            known_selection=selected
            # 正式库命中一个对象不能替代整个比较问题。受信目录缓存仅本地补充
            # 目标识别，仍须后续逐对象准入；不用未验证名称建立云端身份。
            with self.lock:
                cached_records=self.catalog_cache[2]['records'] if self.catalog_cache else []
            expected=selected | set(self._match(text,cached_records,self.max_products))
            explicit=set(re.findall(r'WEBANK:[A-Za-z0-9._:-]+',text))
            expected |= explicit
            wanted={'general'} | {f for f,words in FACET_WORDS.items() if any(w in text for w in words)}
            projection=RoundProductCatalog(base)
            for item in known:
                if 'WEBANK:'+item['code'] not in selected:continue
                product,covered=projection.project(item,base_release)
                if product and wanted<=covered:records.append(product)
            if selected and len(records)==len(expected) and (not comparison or len(expected)>=2):
                report.update(status='reused',prepared_version_count=0,
                              preparation_ms=round((self.clock()-started)*1000,3))
                return PreparedEvidence(RoundProvider(base,records,release_id=base_release),report)
            records=[]
        # 原始问题只在栈上本地匹配，不进入任务、外网请求或公共数据库。
        if isinstance(text, str) and (known_selection or any(w in text for w in ('微众', 'WEBANK:', '理财产品', '产品目录'))):
            try:
                catalog, cached = self._catalog()
                report['catalog_cache_hit'] = cached
                matched = self._match(text, catalog['records'], self.max_products+1)
                product_ids=matched[:self.max_products]
                if not product_ids:
                    report.update(status='gap', coverage_gaps=[{'code': 'identity_unverified'}])
                else:
                    from .webank_runtime import decode_webank_catalog
                    # 本地确定性自动准入仅限已验收官方结构字段；没有任意approve回调给模型。
                    admitted = decode_webank_catalog(self.provider, catalog['raw_bytes'],
                        product_ids=product_ids, facets=('general','fees','risk','eligibility','sale_status'),
                        max_products=self.max_products)
                    versions = admitted['version_ids']
                    if not versions:
                        report.update(status='gap', coverage_gaps=admitted['coverage_gaps'])
                    else:
                        records = admitted['records']
                        catalog_info={'total_count':len(catalog['records']),'source_date':catalog['source_date']}
                        with self.provider.db() as db:
                            baseline = [r[0] for r in db.execute('''SELECT DISTINCT c.version_id
                                FROM release_chunks rc JOIN chunks c ON c.id=rc.chunk_id
                                JOIN document_versions v ON v.id=c.version_id
                                JOIN documents d ON d.id=v.document_id JOIN sources s ON s.id=d.source_id
                                WHERE rc.release_id=? AND v.status='verified' AND s.enabled=1
                                AND s.read_allowed=1 AND s.outbound_allowed=1 AND s.paused=0''', (base_release,))]
                        release = self.provider.publish(version_ids=list(dict.fromkeys(baseline + versions)),
                                                        temporary=True, activate=False)
                        base = self.provider.for_release(release)
                        report.update(status='prepared', release_id=release, temporary=True,
                                      prepared_version_count=len(versions), coverage_gaps=admitted['coverage_gaps'])
                        explicit=set(re.findall(r'WEBANK:[A-Za-z0-9._:-]+',text))
                        if (comparison and len(product_ids)<2) or explicit-set(product_ids):
                            report['coverage_gaps'].append({'code':'comparison_object_unconfirmed' if comparison else 'identity_unverified'})
                        if len(matched)>self.max_products:
                            report['coverage_gaps'].append({'code':'selection_limit_reached'})
                        if not self.enqueue_promotions(versions):
                            report['coverage_gaps'].append({'code':'promotion_queue_full'})
            except KnowledgeError as error:
                report.update(status='gap', coverage_gaps=[{'code': error.code}])
        report['preparation_ms'] = round((self.clock()-started)*1000, 3)
        return PreparedEvidence(RoundProvider(base, records,release_id=report['release_id'],catalog_info=catalog_info), report)

    def enqueue_promotions(self, version_ids):
        versions = sorted(set(version_ids))
        if not versions:
            return
        # 作业只收官方适配器已准入版本。没有owner、提问、回答或个人推论。
        with self.provider.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for vid in versions:
                row = db.execute('''SELECT v.status,d.visibility,d.data_origin,d.source_id,v.revoked_from
                    FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?''', (vid,)).fetchone()
                if (row is None or tuple(row)[:4] != ('verified','public','real','webank-direct-disclosure')
                        or row['revoked_from'] is not None and row['revoked_from']<=today()):
                    raise KnowledgeError('promotion_admission_required')
            job_id='promote-' + sha(packed(versions))
            existing=db.execute('SELECT status FROM evidence_promotions WHERE id=?',(job_id,)).fetchone()
            if existing and existing['status'] in ('completed','pending'):return True
            pending=db.execute("SELECT count(*) FROM evidence_promotions WHERE status='pending'").fetchone()[0]
            if pending>=64:return False
            db.execute("INSERT OR IGNORE INTO evidence_promotions VALUES(?,?,'pending',?,NULL,NULL)",
                       (job_id, packed(versions), now()))
            # 只有本轮重新准入且许可仍在的版本才可恢复失败晋升；不恢复撤回版本。
            for vid in versions:
                source=db.execute('''SELECT s.enabled,s.read_allowed,s.outbound_allowed,s.paused
                    FROM document_versions v JOIN documents d ON d.id=v.document_id
                    JOIN sources s ON s.id=d.source_id WHERE v.id=?''',(vid,)).fetchone()
                if not source or not all(source[k] for k in ('enabled','read_allowed','outbound_allowed')) or source['paused']:
                    raise KnowledgeError('source_permission_missing')
            db.execute("UPDATE evidence_promotions SET status='pending',completed_at=NULL,error_code=NULL WHERE id=? AND status='failed'",(job_id,))
        if self.background:
            self._schedule_promotions()
        return True

    def _schedule_promotions(self):
        with self.lock:
            if not self.closed and (self.promotion_pending is None or self.promotion_pending.done()):
                self.promotion_pending = self.promotion_workers.submit(self.drain_promotions)
                self.promotion_pending.add_done_callback(self._promotion_done)

    def _promotion_done(self, future):
        with self.lock:
            if self.closed:return
            if self.promotion_pending is future:self.promotion_pending=None
            with self.provider.db() as db:
                remaining=db.execute("SELECT 1 FROM evidence_promotions WHERE status='pending' LIMIT 1").fetchone()
            if remaining:self._schedule_promotions()

    def drain_promotions(self):
        with self.provider.db() as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM evidence_promotions WHERE status='pending' ORDER BY created_at LIMIT 16")]
        for job in jobs:
            error = None
            try:
                self.provider.publish(version_ids=json.loads(job['version_ids']), preserve_current=True)
            except KnowledgeError as failure:
                error = failure.code
            except Exception:
                error = 'promotion_failed'
            with self.provider.db() as db:
                db.execute('UPDATE evidence_promotions SET status=?,completed_at=?,error_code=? WHERE id=?',
                           ('failed' if error else 'completed', now(), error, job['id']))

    def close(self):
        with self.lock:
            self.closed = True
        self.fetch_workers.shutdown(wait=True, cancel_futures=True)
        self.promotion_workers.shutdown(wait=True, cancel_futures=True)
