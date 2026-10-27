"""本地派生向量索引：只有封存公共引用 ID，权威性仍由原文库裁决。

5000 片段上限下用精确余弦检索；不另启常驻数据库，不存查询或个人资料。
模型缺失、索引过期/损坏均显式降级；读路径不下载、不建索引。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import sqlite3
import threading
import time

from .store import MAX_CHUNKS, KnowledgeError, now, packed, sha
from .vector_documents import INPUT_POLICY, input_sha, representation


SCHEMA = '''
CREATE TABLE IF NOT EXISTS vector_releases(
 release_id TEXT NOT NULL REFERENCES releases(id),profile_id TEXT NOT NULL,
 release_manifest TEXT NOT NULL,vector_manifest TEXT NOT NULL,dimension INTEGER NOT NULL,
 chunk_count INTEGER NOT NULL,vector_count INTEGER NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(release_id,profile_id));
CREATE TABLE IF NOT EXISTS chunk_vectors(
 release_id TEXT NOT NULL REFERENCES releases(id),profile_id TEXT NOT NULL,
 citation_id TEXT NOT NULL REFERENCES chunks(id),window_no INTEGER NOT NULL,
 text_sha256 TEXT NOT NULL,vector BLOB NOT NULL,vector_sha256 TEXT NOT NULL,
 PRIMARY KEY(release_id,profile_id,citation_id,window_no));
CREATE INDEX IF NOT EXISTS vector_reuse ON chunk_vectors(profile_id,citation_id,text_sha256);
'''
MIN_SIMILARITY = 0.40  # 本地开发题集初校准；召回门槛，非金融可信度/概率。
MAX_SCORE_DISTANCE = 0.12  # 与本次最佳候选的差距上限，避免弱相似候选挤占结果。
SEMANTIC_WEIGHT = 1.25
RRF_K = 60
MAX_REUSE_RELEASES = 8  # 增量只扫描最近的有界历史，不随保留年限无限增长。
WORKER_SEARCH_BUDGET_SECONDS = 60.0  # 单次检索含双 profile / 多视图，低于聊天总预算。
RETRY_DELAYS_SECONDS = (900, 1800, 3600)  # 只对现行公开版本的瞬时构建故障重试。
MAX_BUILD_STATES = 16
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='navren-public-vector')
_jobs = {}
_retry_timers = {}
_build_states = {}
_lock = threading.Lock()


def _build_key(provider, release_id):
    return (str(provider.path.resolve()), release_id)


def _remember_build_state(key, state):
    """Called with _lock held; retain only bounded, non-sensitive codes."""
    _build_states.pop(key, None)
    _build_states[key] = state
    while len(_build_states) > MAX_BUILD_STATES:
        removable = next((old for old in _build_states if old not in _retry_timers and old != key), None)
        if removable is None:
            break
        _build_states.pop(removable, None)


def _transient_build_code(error):
    code = getattr(error, 'code', None)
    if code in ('vector_inference_failed', 'vector_build_timeout'):
        return code
    if isinstance(error, sqlite3.OperationalError):
        sqlite_code = getattr(error, 'sqlite_errorcode', None)
        if (isinstance(sqlite_code, int) and sqlite_code & 0xff in
                (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
            return 'sqlite_busy'
        # Older sqlite3 builds may not expose sqlite_errorcode. Never retain
        # or return the exception message, which may include a file path.
        if any(word in str(error).lower() for word in ('locked', 'busy')):
            return 'sqlite_busy'
    return None


def _build_failure_code(error):
    transient = _transient_build_code(error)
    if transient:
        return transient
    code = getattr(error, 'code', None)
    return code if code in {
        'vector_model_missing', 'vector_model_invalid', 'vector_dependency_missing',
        'vector_index_invalid', 'vector_evidence_changed', 'release_manifest_mismatch',
        'vector_disabled', 'vector_input_invalid', 'vector_text_too_long',
    } else 'vector_build_failed'


def _visible_build_state(provider, release_id):
    if not release_id:
        return None
    try:
        key = _build_key(provider, release_id)
    except (AttributeError, OSError, TypeError):
        return None
    with _lock:
        state = _build_states.get(key)
        return dict(state) if state else None


def _with_build_state(provider, release_id, result):
    # A validated index is authoritative over an older asynchronous failure.
    if not result['ready']:
        state = _visible_build_state(provider, release_id)
        if state:
            result['build_state'] = state
    return result


def _retry_current_release(provider, release_id, key):
    with _lock:
        _retry_timers.pop(key, None)
        state = _build_states.get(key)
        if not state or state['state'] != 'retry_wait':
            return
    try:
        fixed = getattr(provider, 'fixed_release', None) == release_id
        if not fixed:
            with provider.db() as db:
                row = db.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()
            fixed = bool(row and row[0] == release_id)
    except Exception as error:
        _record_build_failure(provider, release_id, key, error)
        return
    if not fixed:
        with _lock:
            _remember_build_state(key, {'state':'stale_release','code':'release_changed',
                                        'retries_scheduled':state['retries_scheduled']})
        return
    schedule(provider, release_id, _from_retry=True)


def _record_build_failure(provider, release_id, key, error):
    code = _build_failure_code(error)
    transient = _transient_build_code(error)
    timer = None
    with _lock:
        state = _build_states.get(key) or {}
        retries = int(state.get('retries_scheduled', 0))
        if (transient and retries < len(RETRY_DELAYS_SECONDS)
                and key not in _retry_timers and len(_retry_timers) < 8):
            delay = RETRY_DELAYS_SECONDS[retries]
            retries += 1
            timer = threading.Timer(delay, lambda:_retry_current_release(provider, release_id, key))
            timer.daemon = True
            _retry_timers[key] = timer
            _remember_build_state(key, {'state':'retry_wait','code':code,
                                        'retries_scheduled':retries,'retry_delay_seconds':delay})
        else:
            result = ('retry_exhausted' if transient and retries >= len(RETRY_DELAYS_SECONDS)
                      else 'retry_unavailable' if transient else 'manual_attention')
            _remember_build_state(key, {'state':result,'code':code,
                                        'retries_scheduled':retries})
    if timer is not None:
        try:
            timer.start()
        except RuntimeError:
            with _lock:
                if _retry_timers.get(key) is timer:
                    _retry_timers.pop(key, None)
                    _remember_build_state(key, {'state':'retry_unavailable','code':code,
                                                'retries_scheduled':retries})


def _text(row):
    return representation(row)['text']


def _manifest(rows):
    return sha(packed([(r['citation_id'], r['window_no'], r['text_sha256'], r['vector_sha256']) for r in rows]))


def fuse(lexical, semantic, lexical_weights=None, semantic_views=None,
         lexical_views=None, lexical_view_weights=None):
    """独立排序后 RRF，避免把 BM25 与 cosine 的量纲相加。"""
    result = {}
    views=list(semantic_views.values()) if semantic_views else [semantic]
    views=[view for view in views if view]
    lexical_branches=[(name,view) for name,view in lexical_views.items() if view] if lexical_views else [('original',lexical)]
    branches=[(sorted(view,key=lambda k:(view[k],k)),1.0/len(lexical_branches),name) for name,view in lexical_branches]
    branches.extend((sorted(view,key=lambda k:(-view[k],k)),SEMANTIC_WEIGHT/len(views),None) for view in views)
    for ranking,weight,lexical_name in branches:
        for rank, cid in enumerate(ranking, 1):
            coverage=1.0
            if lexical_name is not None:
                selected_weights=lexical_view_weights.get(lexical_name,{}) if lexical_view_weights is not None else lexical_weights
                if selected_weights is not None:coverage=selected_weights.get(cid,0.0)
            if coverage <= 0:continue
            result[cid] = result.get(cid, 0.0) + weight * coverage / (RRF_K + rank)
    return {cid: -score for cid, score in result.items()}


class VectorIndex:
    def __init__(self, provider, encoder=None):
        self.provider = provider
        self.encoder = encoder

    @staticmethod
    def _max_windows(encoder):
        value=getattr(encoder,'max_windows',8)
        if type(value) is not int or value not in (8,16):
            raise KnowledgeError('vector_model_invalid')
        return value

    def _encoder(self):
        if self.encoder is False or (self.encoder is None and os.environ.get('NAVREN_VECTOR_ENABLED', '1') == '0'):
            raise KnowledgeError('vector_disabled')
        if self.encoder is not None:
            return self.encoder
        from .retrieval_encoder import get_retrieval_encoder
        return get_retrieval_encoder()

    def _build_rows_valid(self,db,rows):
        checked={}
        for row in rows:
            if (row['visibility'] != 'public' or row['data_origin'] != 'real' or row['status'] != 'verified'
                    or not row['enabled'] or not row['read_allowed'] or not row['outbound_allowed']
                    or not any(row['source_url']==endpoint or (endpoint.endswith('/') and row['source_url'].startswith(endpoint))
                               for endpoint in json.loads(row['endpoints']))
                    or not self.provider._quote_valid(db,row,row,checked)):
                raise KnowledgeError('vector_evidence_changed')

    @staticmethod
    def _reason(error):
        # 不反射异常正文（可能带查询、路径、凭据）。
        allowed = {'vector_disabled','vector_model_missing','vector_model_invalid',
                   'vector_dependency_missing','vector_inference_failed','vector_text_too_long',
                   'vector_query_too_long','vector_input_invalid',
                   'vector_index_missing','vector_index_invalid','vector_build_timeout',
                   'release_manifest_mismatch','vector_empty_eligible','vector_evidence_changed'}
        return getattr(error, 'code', None) if getattr(error, 'code', None) in allowed else 'vector_index_invalid'

    def _load(self, db, release_id, encoder):
        import numpy as np
        if not self.provider._release_manifest_valid(db, release_id):
            raise KnowledgeError('release_manifest_mismatch')
        meta = db.execute('SELECT * FROM vector_releases WHERE release_id=? AND profile_id=?',
                          (release_id, encoder.profile_id)).fetchone()
        if meta is None:
            raise KnowledgeError('vector_index_missing')
        release = db.execute('SELECT manifest_sha256 FROM releases WHERE id=?', (release_id,)).fetchone()
        chunks = self.provider._rows(db,release_id)
        expected = {r['id']: input_sha(r) for r in chunks}
        rows = db.execute('''SELECT * FROM chunk_vectors WHERE release_id=? AND profile_id=?
                       ORDER BY citation_id,window_no''', (release_id, encoder.profile_id)).fetchall()
        max_windows=self._max_windows(encoder)
        if (meta['release_manifest'] != release[0] or meta['dimension'] != encoder.dimension
                or not 0 < meta['chunk_count'] == len(expected) <= MAX_CHUNKS
                or meta['vector_count'] != len(rows) or not len(expected) <= len(rows) <= len(expected)*max_windows
                or set(r['citation_id'] for r in rows) != set(expected) or _manifest(rows) != meta['vector_manifest']):
            raise KnowledgeError('vector_index_invalid')
        vectors = []; ids = []; next_window = {}
        for r in rows:
            cid = r['citation_id']; blob = bytes(r['vector'])
            if (r['text_sha256'] != expected[cid] or r['window_no'] != next_window.get(cid, 0)
                    or len(blob) != encoder.dimension*4 or sha(blob) != r['vector_sha256']):
                raise KnowledgeError('vector_index_invalid')
            vec = np.frombuffer(blob, dtype='<f4')
            if not np.isfinite(vec).all() or abs(float(np.linalg.norm(vec))-1.0) > 0.002:
                raise KnowledgeError('vector_index_invalid')
            next_window[cid] = r['window_no'] + 1
            if next_window[cid] > max_windows:
                raise KnowledgeError('vector_index_invalid')
            vectors.append(vec); ids.append(cid)
        return rows, ids, np.stack(vectors)

    def status(self, db, release_id):
        try:
            encoder = self._encoder()
            from .retrieval_encoder import is_bundle
            if is_bundle(encoder):
                statuses=[]
                for factory in encoder.factories:
                    try:statuses.append(VectorIndex(self.provider,factory()).status(db,release_id))
                    except Exception as error:statuses.append({'ready':False,'reason':self._reason(error)})
                ready=all(item['ready'] for item in statuses)
                return _with_build_state(self.provider, release_id,
                        {'ready':ready,'reason':None if ready else 'vector_partial',
                         'index_ready':all(item.get('index_ready',item['ready']) for item in statuses),
                         'profiles':statuses,'method':'sqlite_exact_cosine','document_policy':INPUT_POLICY})
            rows, _, _ = self._load(db, release_id, encoder)
            if getattr(encoder, 'is_worker_proxy', False):
                # The index may be intact while the model has never been
                # successfully loaded by this worker. A no-load socket peek
                # must not turn static profile metadata into a false ready.
                try:runtime_ready=encoder.runtime_verified()
                except Exception:runtime_ready=False
                if not runtime_ready:
                    return _with_build_state(self.provider, release_id,
                            {'ready':False,'index_ready':True,
                             'reason':'vector_model_not_loaded','profile_id':encoder.profile_id,
                             'dimension':encoder.dimension,'vector_count':len(rows),
                             'method':'sqlite_exact_cosine','document_policy':INPUT_POLICY})
            return _with_build_state(self.provider, release_id,
                    {'ready':True,'reason':None,'profile_id':encoder.profile_id,
                     'dimension':encoder.dimension,'vector_count':len(rows),'method':'sqlite_exact_cosine',
                     'document_policy':INPUT_POLICY})
        except Exception as error:
            return _with_build_state(self.provider, release_id,
                    {'ready':False,'reason':self._reason(error),'method':'sqlite_exact_cosine',
                     'document_policy':INPUT_POLICY})

    def search(self, db, release_id, query, eligible_ids, *, projected_query=None, cross_language=False,
               translated_query=None):
        if os.environ.get('NAVREN_VECTOR_WORKER_SOCKET', '').strip():
            from worker_proto.client import encoder_budget
            with encoder_budget(WORKER_SEARCH_BUDGET_SECONDS):
                return self._search_with_budget(db,release_id,query,eligible_ids,
                                                projected_query=projected_query,
                                                cross_language=cross_language,
                                                translated_query=translated_query)
        return self._search_with_budget(db,release_id,query,eligible_ids,
                                        projected_query=projected_query,
                                        cross_language=cross_language,
                                        translated_query=translated_query)

    def _search_with_budget(self, db, release_id, query, eligible_ids, *, projected_query=None,
                            cross_language=False, translated_query=None):
        try:
            encoder = self._encoder()
            from .retrieval_encoder import is_bundle
            if is_bundle(encoder):
                return self._search_bundle(db,release_id,query,eligible_ids,encoder,
                                           projected_query,cross_language,translated_query)
            _, ids, matrix = self._load(db, release_id, encoder)
            if not eligible_ids:
                return {'scores':{},'mode':'vector','reason':None}
            import numpy as np
            # 先过滤已经通过原文、身份、效期和图核验的 IDs，再做最近邻，避免错误对象挤满 top-k。
            offsets = [i for i,cid in enumerate(ids) if cid in eligible_ids]
            scores = {};views={};failures={}
            if offsets:
                from .retrieval_encoder import score_policy
                minimum,distance=score_policy(encoder)
                queries={'original':query,'public_concepts':projected_query,'translated':translated_query}
                for name,feature_query in queries.items():
                    if not feature_query:continue
                    try:
                        vec=np.asarray(encoder.encode_query(feature_query),dtype=np.float32)
                        if vec.shape != (encoder.dimension,) or not np.isfinite(vec).all() or abs(float(np.linalg.norm(vec))-1)>0.002:
                            raise KnowledgeError('vector_inference_failed')
                        similarities=matrix[offsets]@vec
                        threshold=max(minimum,float(similarities.max())-distance)
                    except Exception as error:
                        # Failures here concern query inference, not the already
                        # validated index. Preserve controlled encoder reasons.
                        code=getattr(error,'code',None)
                        from .embeddings import ERROR_CODES
                        failures[name]=code if code in ERROR_CODES else 'vector_inference_failed'
                        continue
                    extra={}
                    for i,score in zip(offsets,similarities):
                        value=float(score)
                        if value>=threshold:extra[ids[i]]=max(extra.get(ids[i],-1.0),value)
                    views[name]=extra
                    for cid,value in extra.items():scores[cid]=max(scores.get(cid,-1.0),value)
            if failures and not views:
                return {'scores':{},'views':{},'mode':'fts5','reason':next(iter(failures.values())),
                        'view_failures':failures}
            return {'scores':scores,'views':views,'mode':'vector',
                    'reason':'vector_partial' if failures else None,'view_failures':failures}
        except Exception as error:
            return {'scores':{},'mode':'fts5','reason':self._reason(error)}

    def _search_bundle(self,db,release_id,query,eligible_ids,encoder,projected_query,cross_language,translated_query):
        views={};branches=[]
        factories=encoder.factories if cross_language else encoder.factories[:1]
        for index,factory in enumerate(factories):
            try:
                member=factory()
                result=VectorIndex(self.provider,member).search(db,release_id,query,eligible_ids,
                               projected_query=projected_query if index==0 else None,
                               translated_query=translated_query if index==0 else None)
                profile=member.profile_id
            except Exception as error:
                result={'scores':{},'mode':'fts5','reason':self._reason(error)}
                profile=None
            branches.append({'profile_id':profile,'mode':result['mode'],'reason':result['reason'],
                             'view_failures':result.get('view_failures',{})})
            for name,values in result.get('views',{}).items():
                views[str(index)+':'+name]=values
        ready=[branch for branch in branches if branch['mode']=='vector']
        # Values are candidate membership only: no cosine from distinct spaces
        # is compared. RRF consumes the separately ranked, thresholded views.
        candidates={cid:0.0 for view in views.values() for cid in view}
        reason=None if len(ready)==len(branches) and not any(b['reason'] for b in branches) else ('vector_partial' if ready else branches[0]['reason'])
        return {'scores':candidates,'views':views,'mode':'vector' if ready else 'fts5',
                'reason':reason,'branches':branches}

    def build(self, release_id, *, max_seconds=300):
        encoder = self._encoder()
        from .retrieval_encoder import is_bundle
        if is_bundle(encoder):
            started=time.monotonic();results=[]
            for factory in encoder.factories:
                remaining=max_seconds-(time.monotonic()-started)
                if remaining<=0:raise KnowledgeError('vector_build_timeout')
                results.append(VectorIndex(self.provider,factory()).build(release_id,max_seconds=remaining))
            return {'ready':True,'release_id':release_id,'profiles':results,
                    'chunk_count':results[0]['chunk_count'],'document_policy':INPUT_POLICY,
                    'build_seconds':round(time.monotonic()-started,3)}
        import numpy as np
        started = time.monotonic()
        with self.provider.db() as db:
            db.execute('BEGIN')
            if not self.provider._release_manifest_valid(db, release_id):
                raise KnowledgeError('release_manifest_mismatch')
            manifest = db.execute('SELECT manifest_sha256 FROM releases WHERE id=?', (release_id,)).fetchone()[0]
            rows = self.provider._rows(db, release_id)
            rows = sorted(rows, key=lambda r:r['id'])
            self._build_rows_valid(db,rows)
            try:
                self._load(db, release_id, encoder)
                return {'ready':True,'reused':True,'release_id':release_id,'chunk_count':len(rows),'profile_id':encoder.profile_id,
                        'encoded_chunk_count':0,'reused_chunk_count':len(rows),'document_policy':INPUT_POLICY}
            except KnowledgeError:
                pass
            # 原件更新会使所有 citation 改版：按相同表示hash复用数学表示，
            # 新向量成员仍绑定新 citation，不能继承旧引用的权限/时效/身份。
            reusable = {}
            required = {input_sha(row) for row in rows}
            old_releases = db.execute('''SELECT release_id FROM vector_releases WHERE profile_id=?
                     ORDER BY created_at DESC,release_id DESC LIMIT ?''',
                     (encoder.profile_id,MAX_REUSE_RELEASES)).fetchall()
            for old in old_releases:
                if not required:break
                try:
                    verified = self._load(db,old[0],encoder)[0]
                except KnowledgeError:
                    continue
                complete = {}
                for r in verified:
                    if r['text_sha256'] not in required:continue
                    a = np.frombuffer(bytes(r['vector']),dtype='<f4')
                    complete.setdefault((r['citation_id'],r['text_sha256']),[]).append(a)
                for (_,digest),parts in complete.items():
                    # 每个完整表示来自同一已校验 citation，不能拼接不同历史窗口。
                    reusable.setdefault(digest,np.stack(parts))
                    required.discard(digest)
            adaptation = {}; fields = {}; reference_only = 0
            for row in rows:
                value = representation(row)
                adaptation[value['kind']] = adaptation.get(value['kind'],0) + 1
                if value['field']:fields[value['field']] = fields.get(value['field'],0) + 1
                reference_only += value['detail_status'] == 'reference_only'
        generated = []; reused_chunks = 0
        for start in range(0,len(rows),4):
            if time.monotonic()-started > max_seconds:
                raise KnowledgeError('vector_build_timeout')
            batch = rows[start:start+4]; missing = []
            values = {}
            for row in batch:
                key = input_sha(row); cached = reusable.get(key)
                if cached is not None:
                    values[row['id']] = cached; reused_chunks += 1
                else:
                    missing.append(row)
            if missing:
                outputs = encoder.encode_documents([_text(row) for row in missing])
                if len(outputs) != len(missing):raise KnowledgeError('vector_index_invalid')
                values.update({row['id']:arr for row,arr in zip(missing,outputs)})
            for row in batch:
                array = np.asarray(values[row['id']],dtype='<f4')
                if array.ndim != 2 or array.shape[1] != encoder.dimension or not 1 <= len(array) <= self._max_windows(encoder) or not np.isfinite(array).all():
                    raise KnowledgeError('vector_index_invalid')
                for window,vec in enumerate(array):
                    if abs(float(np.linalg.norm(vec))-1) > 0.002:raise KnowledgeError('vector_index_invalid')
                    blob = vec.tobytes()
                    generated.append({'citation_id':row['id'],'window_no':window,'text_sha256':input_sha(row),
                                      'vector':blob,'vector_sha256':sha(blob)})
        generated.sort(key=lambda r:(r['citation_id'],r['window_no']))
        if time.monotonic()-started > max_seconds:raise KnowledgeError('vector_build_timeout')
        with self.provider.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.provider._release_manifest_valid(db,release_id):raise KnowledgeError('release_manifest_mismatch')
            self._build_rows_valid(db,self.provider._rows(db,release_id))
            if time.monotonic()-started > max_seconds:raise KnowledgeError('vector_build_timeout')
            # 原子提交向量与代次清单；失败不覆盖旧可用索引。
            db.execute('DELETE FROM chunk_vectors WHERE release_id=? AND profile_id=?',(release_id,encoder.profile_id))
            db.executemany('INSERT INTO chunk_vectors VALUES(?,?,?,?,?,?,?)',
                           [(release_id,encoder.profile_id,r['citation_id'],r['window_no'],r['text_sha256'],r['vector'],r['vector_sha256']) for r in generated])
            db.execute('INSERT OR REPLACE INTO vector_releases VALUES(?,?,?,?,?,?,?,?)',
                       (release_id,encoder.profile_id,manifest,_manifest(generated),encoder.dimension,len(rows),len(generated),now()))
        return {'ready':True,'release_id':release_id,'profile_id':encoder.profile_id,'chunk_count':len(rows),
                'vector_count':len(generated),'reused_chunk_count':reused_chunks,'encoded_chunk_count':len(rows)-reused_chunks,
                'document_policy':INPUT_POLICY,'adaptation_counts':adaptation,'field_counts':fields,
                'reference_only_chunk_count':reference_only,'reuse_release_limit':MAX_REUSE_RELEASES,
                'build_seconds':round(time.monotonic()-started,3)}


def schedule(provider, release_id, *, _from_retry=False):
    """新封存公共版本后台建索引；最多一个CPU作业，排队有界，不阻塞用户检索。"""
    if not release_id or provider.vector_encoder is not None or os.environ.get('NAVREN_VECTOR_ENABLED','1') == '0':
        if _from_retry and release_id:
            _record_build_failure(provider, release_id, _build_key(provider, release_id),
                                  KnowledgeError('vector_disabled'))
        return
    from .retrieval_encoder import selected_model_directory
    from .embeddings import VectorError
    try:
        if not (selected_model_directory()/'manifest.json').is_file():
            if _from_retry:
                _record_build_failure(provider, release_id, _build_key(provider, release_id),
                                      KnowledgeError('vector_model_missing'))
            return
    except (KnowledgeError, VectorError) as error:
        if _from_retry:
            _record_build_failure(provider, release_id, _build_key(provider, release_id), error)
        return
    key = _build_key(provider, release_id)
    with _lock:
        for old in [k for k,f in _jobs.items() if f.done()]:_jobs.pop(old)
        state = _build_states.get(key)
        if (key in _jobs or key in _retry_timers or len(_jobs) >= 8
                or state and state['state'] in
                   ('manual_attention','retry_exhausted','retry_unavailable','stale_release')
                or state and state['state'] == 'retry_wait' and not _from_retry):
            return
        if _from_retry and (not state or state['state'] != 'retry_wait'):
            return
        retries = int(state.get('retries_scheduled', 0)) if state else 0
        _remember_build_state(key, {'state':'running','retries_scheduled':retries})
        def work():
            try:
                if os.environ.get('NAVREN_VECTOR_WORKER_SOCKET', '').strip():
                    # Full persisted-index validation uses static proxy
                    # metadata; no worker/ONNX session is opened on startup.
                    from .retrieval_encoder import get_retrieval_encoder,is_bundle
                    selected=get_retrieval_encoder()
                    members=[factory() for factory in selected.factories] if is_bundle(selected) else [selected]
                    try:
                        with provider.db() as db:
                            for member in members:
                                VectorIndex(provider,member)._load(db,release_id,member)
                        with _lock:_build_states.pop(key,None)
                        return
                    except Exception:
                        pass
                    # New/invalid index builds run outside the request path.
                    # A short bounded grace leaves startup responsive.
                    time.sleep(15)
                provider.build_vector_index(release_id)
                with _lock:_build_states.pop(key,None)
            except Exception as error:
                # Only worker failure, bounded build timeout and SQLite busy
                # can recover automatically. Other failures need inspection.
                # Preserve only an error code and retry count, never raw text.
                if os.environ.get('NAVREN_VECTOR_WORKER_SOCKET', '').strip():
                    _record_build_failure(provider, release_id, key, error)
        _jobs[key] = _pool.submit(work)
