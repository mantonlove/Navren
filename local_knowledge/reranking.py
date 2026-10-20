"""本地单 logit Cross-Encoder 排序；模型分数从不成为事实或概率。

运行时仅使用已封存、已准入公共原文的精确范围，不下载、不建库、不缓存
问句或原文。调用方仍负责发布、时点、权限、图路径和原件准入；本模块重核
公开原件 hash、范围与原子映射。分支候选池只改变顺序，不改变证据许可。
"""
from __future__ import annotations

import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import threading
import time

MODEL_REPOSITORY = 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'
MODEL_REVISION = '1427fd652930e4ba29e8149678df786c240d8825'
MODEL_ARTIFACTS = {
    'onnx/model_qint8_arm64.onnx': {'size':118620017,'sha256':'1825907d6c1a9001ff78124780bbde20a614a8c3df3b63409cf3c72c6fe5c8b4'},
    'tokenizer.json': {'size':17082660,'sha256':'62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626'},
    'config.json': {'size':891,'sha256':'cc2cfe51aa3fd759d21d21acf5dfd6994aa67a3c9210636d22e143699d336c77'},
    'tokenizer_config.json': {'size':435,'sha256':'e7fbfbfa6347b4e414c1cee50d142e2c2f9a895dad68b068ae83a8b564c3837e'},
    'special_tokens_map.json': {'size':239,'sha256':'378eb3bf733eb16e65792d7e3fda5b8a4631387ca04d2015199c4d4f22ae554d'},
    'README.md': {'size':2278,'sha256':'474736a65d6393a060119a8dc304563af67af4d8d86ccfee4a05dd0df107fc11'},
}
ALGORITHM_POLICY = 'local-cross-encoder-q8-complete-canonical-windows-scope-core-tie-batch-v5'
MAX_CANDIDATES = 192
MAX_WINDOWS = 8
MAX_PAIR_TOKENS = 512
MAX_QUERY_CHARS = 500
MAX_CALL_SECONDS = 30.0
MAX_RUN_SECONDS = 3.0
BATCH_SIZE = 4
_BASE_BRANCHES = tuple('lexical.'+name for name in ('original','public_concepts','translated')) + \
    tuple('vector.primary.'+name for name in ('original','public_concepts','translated')) + ('vector.secondary.original',)
RECALL_BRANCH_NAMES = frozenset((*_BASE_BRANCHES,
    *(f'q{slot}.'+name for slot in range(4) for name in _BASE_BRANCHES)))
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
ERROR_CODES = frozenset(('rerank_disabled','rerank_model_missing','rerank_model_invalid',
    'rerank_dependency_missing','rerank_input_invalid','rerank_query_too_long',
    'rerank_evidence_invalid','rerank_candidate_too_long','rerank_inference_failed',
    'rerank_timeout','rerank_empty_candidates','rerank_partial'))


class RerankError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'rerank_inference_failed'
        super().__init__(self.code)


def _packed(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def default_model_directory():
    configured=os.environ.get('NAVREN_RERANK_DIR','').strip()
    return Path(configured).expanduser() if configured else Path.home()/'Library'/'Application Support'/'Navren'/'models'/'mmarco-mminilmv2-reranker-q8'


def rerank_profile_metadata():
    value={'repository':MODEL_REPOSITORY,'revision':MODEL_REVISION,'artifacts':MODEL_ARTIFACTS,
           'policy':ALGORITHM_POLICY,'max_candidates':MAX_CANDIDATES,'max_windows':MAX_WINDOWS,
           'max_pair_tokens':MAX_PAIR_TOKENS,'max_query_characters':MAX_QUERY_CHARS,
           'batch_size':BATCH_SIZE,'cpu_threads':2,'maximum_call_seconds':MAX_CALL_SECONDS,
           'input':'exact current-version canonical title when present + exact full core span',
           'candidate_ranks':'reserve32complete canonical anchor windows then32distinct explicit source-scope windows; then real branch top16/canonical and child top32 ranks and version-balanced alternatives',
           'pool_tie_policy':'same version and actual rank: stable natural sentence_core first only within sentence_core/context_core/canonical_sentence_window slots; other kinds keep positions; legacy minimum-rank minimum-group-ID version order preserved',
           'preselected_pool':'internal unique known admitted group IDs up to192; inherit common pool without repeating recall qualification; exact evidence validation preserved',
           'pool_duplicates':'same version, exact span and complete citation map only; earliest canonical anchor represents identical complete windows',
           'batch_policy':'stable ascending pair token length; original document owners and full-window maximum preserved',
           'deadline':'shared across views and queries; includes validation, locks, loading, tokenization, inference',
           'native_initialization':'deadline checked before and after; cannot be hard-cancelled in thread',
           'score':'single raw logit; rank only; no confidence threshold'}
    return dict(value,profile_id='mmarco-mminilmv2-reranker-q8:'+_sha(_packed(value)))


def _call_deadline(deadline):
    now=time.monotonic()
    if deadline is None:return now+MAX_CALL_SECONDS
    if isinstance(deadline,bool) or not isinstance(deadline,Real) or not math.isfinite(deadline):
        raise RerankError('rerank_input_invalid')
    value=min(float(deadline),now+MAX_CALL_SECONDS)
    _check_deadline(value)
    return value


def _check_deadline(deadline):
    if deadline is not None and time.monotonic()>=deadline:
        raise RerankError('rerank_timeout')


def _remaining(deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:raise RerankError('rerank_timeout')
    return remaining


def _load_verified_artifacts(directory,*,deadline=None):
    """每次取得模型均验证 pinned bytes；大模型流式 hash，不整份复制到 RAM。"""
    directory=Path(directory)
    try:
        _check_deadline(deadline)
        path=directory/'manifest.json'
        if not path.exists():raise RerankError('rerank_model_missing')
        if directory.is_symlink() or path.is_symlink() or path.stat().st_size>16384:
            raise RerankError('rerank_model_invalid')
        manifest=json.loads(path.read_bytes())
        if (not isinstance(manifest,dict) or manifest.get('format_version')!=1
            or manifest.get('complete') is not True or manifest.get('repository')!=MODEL_REPOSITORY
            or manifest.get('revision')!=MODEL_REVISION or manifest.get('algorithm_policy')!=ALGORITHM_POLICY
            or manifest.get('profile_id')!=rerank_profile_metadata()['profile_id']
            or not isinstance(manifest.get('artifacts'),dict)
            or set(manifest['artifacts'])!=set(MODEL_ARTIFACTS)):
            raise RerankError('rerank_model_invalid')
        paths={}
        for name,expected in MODEL_ARTIFACTS.items():
            _check_deadline(deadline)
            file=directory/name; record=manifest['artifacts'][name]
            if (not isinstance(record,dict) or any(record.get(key)!=expected[key] for key in ('size','sha256'))
                or file.is_symlink() or any(p.is_symlink() for p in file.parents if p!=directory.parent)
                or not file.is_file() or file.stat().st_size!=expected['size']):
                raise RerankError('rerank_model_invalid')
            digest=hashlib.sha256();count=0
            with file.open('rb') as stream:
                while data:=stream.read(1024*1024):
                    _check_deadline(deadline)
                    digest.update(data);count+=len(data)
                    if count>expected['size']:raise RerankError('rerank_model_invalid')
            if count!=expected['size'] or digest.hexdigest()!=expected['sha256']:
                raise RerankError('rerank_model_invalid')
            paths[name]=file
        config=json.loads(paths['config.json'].read_bytes())
        tokenizer=json.loads(paths['tokenizer_config.json'].read_bytes())
        if (config.get('architectures')!=['XLMRobertaForSequenceClassification']
            or config.get('model_type')!='xlm-roberta' or config.get('hidden_size')!=384
            or config.get('num_hidden_layers')!=12 or config.get('vocab_size')!=250002
            or config.get('max_position_embeddings')!=514 or config.get('pad_token_id')!=1
            or config.get('id2label')!={'0':'LABEL_0'} or config.get('type_vocab_size')!=1
            or tokenizer.get('model_max_length')!=MAX_PAIR_TOKENS):
            raise RerankError('rerank_model_invalid')
        _check_deadline(deadline)
        return paths
    except RerankError:
        _check_deadline(deadline);raise
    except FileNotFoundError:
        _check_deadline(deadline);raise RerankError('rerank_model_missing') from None
    except Exception:
        _check_deadline(deadline);raise RerankError('rerank_model_invalid') from None


class LocalReranker:
    def __init__(self,directory,*,deadline=None):
        deadline=_call_deadline(deadline)
        artifacts=_load_verified_artifacts(directory,deadline=deadline)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except Exception:
            _check_deadline(deadline);raise RerankError('rerank_dependency_missing') from None
        _check_deadline(deadline)
        self.profile_id=rerank_profile_metadata()['profile_id']
        self._np=np;self._ort=ort;self._lock=threading.Lock()
        try:
            self._tokenizer=Tokenizer.from_file(str(artifacts['tokenizer.json']))
            self._tokenizer.no_truncation();self._tokenizer.no_padding()
            _check_deadline(deadline)
            ort.disable_telemetry_events()
            options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
            options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
            options.log_severity_level=4;options.enable_profiling=False
            self._session=ort.InferenceSession(str(artifacts['onnx/model_qint8_arm64.onnx']),
                sess_options=options,providers=['CPUExecutionProvider'])
            # 原生初始化不可在线程内硬取消；完成后必须拒绝超时结果。
            _check_deadline(deadline)
            self._inputs={v.name for v in self._session.get_inputs()}
            if (not {'input_ids','attention_mask'}<=self._inputs
                or not self._inputs<={'input_ids','attention_mask','token_type_ids'}
                or [v.name for v in self._session.get_outputs()]!=['logits']
                or self._session.get_providers()!=['CPUExecutionProvider']):
                raise RerankError('rerank_model_invalid')
        except RerankError:
            _check_deadline(deadline);raise
        except Exception:
            _check_deadline(deadline);raise RerankError('rerank_model_invalid') from None

    def _windows(self,query,text,*,deadline=None):
        """覆盖全部原文本 token；超过八窗明确拒绝，绝不静默保留前半句。"""
        def encode(*values,**kwargs):
            _check_deadline(deadline)
            try:return self._tokenizer.encode(*values,**kwargs)
            finally:_check_deadline(deadline)
        _check_deadline(deadline)
        query_tokens=encode(query,add_special_tokens=False)
        _check_deadline(deadline)
        allowance=MAX_PAIR_TOKENS-len(query_tokens.ids)-4
        if allowance<16:raise RerankError('rerank_query_too_long')
        encoded=encode(text,add_special_tokens=False)
        _check_deadline(deadline)
        if not encoded.ids:raise RerankError('rerank_evidence_invalid')
        size=allowance-8 # 再编码窗口边界留有余量；实际 pair token 数仍逐窗检查。
        stride=max(1,size-32); windows=[];cursor=0
        while cursor<len(encoded.ids):
            _check_deadline(deadline)
            stop=min(len(encoded.ids),cursor+size)
            start_char=0 if cursor==0 else encoded.offsets[cursor][0]
            end_char=len(text) if stop==len(encoded.ids) else encoded.offsets[stop-1][1]
            window=text[start_char:end_char]
            pair=encode(query,window,add_special_tokens=True)
            _check_deadline(deadline)
            if not pair.ids or len(pair.ids)>MAX_PAIR_TOKENS:
                raise RerankError('rerank_candidate_too_long')
            windows.append(pair)
            if len(windows)>MAX_WINDOWS:raise RerankError('rerank_candidate_too_long')
            if stop==len(encoded.ids):break
            cursor+=stride
        return windows

    def _run(self,encodings,deadline):
        np=self._np
        length=max(len(value.ids) for value in encodings)
        ids=np.ones((len(encodings),length),dtype=np.int64)
        mask=np.zeros_like(ids);types=np.zeros_like(ids)
        for index,value in enumerate(encodings):
            ids[index,:len(value.ids)]=value.ids
            mask[index,:len(value.ids)]=value.attention_mask
            types[index,:len(value.ids)]=value.type_ids
        feeds={'input_ids':ids,'attention_mask':mask}
        if 'token_type_ids' in self._inputs:feeds['token_type_ids']=types
        remaining=deadline-time.monotonic()
        if remaining<=0:raise RerankError('rerank_timeout')
        options=self._ort.RunOptions()
        timer=threading.Timer(min(MAX_RUN_SECONDS,remaining),lambda:setattr(options,'terminate',True))
        timer.daemon=True;timer.start()
        try:
            value=self._session.run(['logits'],feeds,run_options=options)[0]
            if options.terminate or time.monotonic()>=deadline:raise RerankError('rerank_timeout')
            if value.shape!=(len(encodings),1) or not np.isfinite(value).all():
                raise RerankError('rerank_inference_failed')
            return [float(score) for score in value[:,0]]
        except RerankError:raise
        except Exception:raise RerankError('rerank_timeout' if options.terminate else 'rerank_inference_failed') from None
        finally:timer.cancel()

    def score_documents(self,query,texts,*,deadline=None):
        deadline=_call_deadline(deadline)
        if (not isinstance(query,str) or not query.strip() or len(query)>MAX_QUERY_CHARS
            or not isinstance(texts,(list,tuple)) or len(texts)>MAX_CANDIDATES
            or any(not isinstance(text,str) or not text for text in texts)):
            raise RerankError('rerank_input_invalid')
        if not self._lock.acquire(timeout=_remaining(deadline)):raise RerankError('rerank_timeout')
        try:
            _check_deadline(deadline)
            outputs=[];pairs=[];owners=[]
            for index,text in enumerate(texts):
                _check_deadline(deadline)
                try:
                    windows=self._windows(query,text,deadline=deadline)
                    outputs.append({'score':None,'reason':None,'window_count':len(windows)})
                    pairs.extend(windows);owners.extend([index]*len(windows))
                except RerankError as error:
                    if error.code in ('rerank_query_too_long','rerank_timeout'):raise
                    outputs.append({'score':None,'reason':error.code,'window_count':0})
            # 同长度窗口组 batch，减少短句被长窗口的 padding 放大。稳定
            # 排序仅改变计算批次，owner 映射及全窗口 max 聚合保持原顺序。
            _check_deadline(deadline)
            order=sorted(range(len(pairs)),key=lambda index:len(pairs[index].ids))
            _check_deadline(deadline)
            pairs=[pairs[index] for index in order];owners=[owners[index] for index in order]
            for start in range(0,len(pairs),BATCH_SIZE):
                for owner,score in zip(owners[start:start+BATCH_SIZE],self._run(pairs[start:start+BATCH_SIZE],deadline)):
                    previous=outputs[owner]['score']
                    outputs[owner]['score']=score if previous is None else max(previous,score)
            _check_deadline(deadline)
            return outputs
        finally:self._lock.release()


_model_lock=threading.Lock()
_model=None
_model_directory=None


def get_reranker(*,deadline=None):
    global _model,_model_directory
    deadline=_call_deadline(deadline)
    directory=default_model_directory()
    if not _model_lock.acquire(timeout=_remaining(deadline)):raise RerankError('rerank_timeout')
    try:
        _check_deadline(deadline)
        if _model is None or _model_directory!=str(directory.absolute()):
            _model=LocalReranker(directory,deadline=deadline)
            _model_directory=str(directory.absolute())
        else:
            # 已缓存会话的每次取得也核验固定文件；冷载入由构造器核验。
            _load_verified_artifacts(directory,deadline=deadline)
        _check_deadline(deadline)
        return _model
    finally:_model_lock.release()


def _candidate_pool(groups):
    def geometry(group):
        return (group.get('version_id'),group.get('start_char'),group.get('end_char'),
                tuple(sorted(group['citation_ids'])))
    windows=[group for group in groups if group['kind']=='canonical_sentence_window'
             and group.get('canonical_anchor_rank') is not None
             and group['canonical_anchor_rank']<=32]
    reserved=[];anchors=set();anchor_ranks=set();canonical_geometry=set()
    for group in sorted(windows,key=lambda g:(g['canonical_anchor_rank'],g['group_id'])):
        anchor=group['canonical_anchor_id']
        key=geometry(group)
        if (anchor not in anchors and group['canonical_anchor_rank'] not in anchor_ranks
                and key not in canonical_geometry):
            reserved.append(group);anchors.add(anchor);anchor_ranks.add(group['canonical_anchor_rank'])
            canonical_geometry.add(key)
    canonical_reserved_count=len(reserved)
    known_geometry={geometry(group) for group in reserved}
    scope_windows=[group for group in groups if group['kind']=='canonical_sentence_window'
                   and group.get('source_scope_rank') is not None and group['source_scope_rank']<=32]
    scope_count=0
    for group in sorted(scope_windows,key=lambda g:(g['source_scope_rank'],
                        g.get('canonical_anchor_rank') or 20001,g['group_id'])):
        key=geometry(group)
        if key not in known_geometry and scope_count<32:
            reserved.append(group);known_geometry.add(key);scope_count+=1
    protected_ids={group['group_id'] for group in reserved}
    protected_geometry={geometry(group) for group in reserved}
    # 同一精确范围和完整 map 是同一份事实；保留窗口优先的一个代表。
    # 不以语义相似度、跨度重叠百分比或词表删去不同事实。
    remaining={}
    priority={'canonical_sentence_window':0,'sentence_core':1,'required_context':2,
              'context_core':3,'canonical_singleton':4,'context_supplement':5}
    for group in sorted(groups,key=lambda g:(priority[g['kind']],
                        g.get('canonical_anchor_rank') or 20001,g['group_id'])):
        key=geometry(group)
        if group['group_id'] in protected_ids or key in protected_geometry:
            continue
        if key not in remaining:
            remaining[key]=dict(group,recall_rank_positions=dict(group.get('recall_rank_positions',{})),
                                rank_positions=dict(group.get('rank_positions',{})))
        else:
            representative=remaining[key]
            for branch,rank in group.get('recall_rank_positions',{}).items():
                slots=representative['recall_rank_positions']
                slots[branch]=min(slots.get(branch,rank),rank)
            for branch,rank in group.get('rank_positions',{}).items():
                if rank is not None:
                    slots=representative['rank_positions']
                    previous=slots.get(branch)
                    slots[branch]=min(previous,rank) if previous is not None else rank
            for field in ('child_rank','source_scope_rank'):
                rank=group.get(field)
                if rank is not None:
                    previous=representative.get(field)
                    representative[field]=min(previous,rank) if previous is not None else rank
    candidates=list(remaining.values())
    kind_by_id={group['group_id']:group['kind'] for group in candidates}

    def ordered_bucket(ids):
        ordered=sorted(ids)
        # 同真实 rank 的派生窗口/邻句不能仅靠 cg/cw 字典序挤掉原子自然句。
        # 只交换这些表示已占据的槽位，机构 required_context 等保持原位置。
        slots=[index for index,gid in enumerate(ordered) if kind_by_id[gid]
               in ('sentence_core','context_core','canonical_sentence_window')]
        choices=sorted((ordered[index] for index in slots),
                       key=lambda gid:(kind_by_id[gid]!='sentence_core',gid))
        for index,gid in zip(slots,choices):ordered[index]=gid
        return ordered

    def alternatives(position,maximum):
        byversion={}
        for group in candidates:
            rank=position(group)
            if rank is not None and rank<=maximum:
                byversion.setdefault(group.get('version_id'),{}).setdefault(rank,[]).append(group['group_id'])
        queues=[]
        for version,ranks in byversion.items():
            buckets=[ordered_bucket(ranks[rank]) for rank in sorted(ranks)]
            queue=[];offset=0
            while any(offset<len(values) for values in buckets):
                for values in buckets:
                    if offset<len(values):queue.append(values[offset])
                offset+=1
            # 同版本内部改平局不应反馈成跨版本优先级变动；保留旧 queue[0]
            # 对应的最小 rank 桶最小 gid，不使用重排后首个候选的 gid。
            queues.append((min(ranks),min(ranks[min(ranks)]),queue))
        # 每篇版本先取得一个真实分支候选；版本内部每个真实 rank 先取
        # 一个替代项。资格仍为原 top16/top32，不加入阈值外候选。
        queues.sort(key=lambda value:(value[0],value[1]))
        result=[];offset=0
        while any(offset<len(queue) for _,_,queue in queues):
            for _,_,queue in queues:
                if offset<len(queue):result.append(queue[offset])
            offset+=1
        return result
    branches={}
    for branch in sorted(RECALL_BRANCH_NAMES):
        branches[branch]=alternatives(lambda g:g.get('recall_rank_positions',{}).get(branch),16)
    branches['canonical']=alternatives(lambda g:g.get('rank_positions',{}).get('canonical'),32)
    branches['sentence']=alternatives(lambda g:g.get('child_rank') or g.get('rank_positions',{}).get('sentence'),32)
    branches['source_scope']=alternatives(lambda g:g.get('source_scope_rank')
                                         if g['kind']=='canonical_sentence_window' else None,32)
    branches={k:v for k,v in branches.items() if v}
    result=[group['group_id'] for group in reserved];seen=set(result);offset=0
    # 轮转保留每个真实分支，而不是再次以融合总分裁掉单视图强候选。
    while len(result)<MAX_CANDIDATES and any(offset<len(v) for v in branches.values()):
        for values in branches.values():
            if offset<len(values) and values[offset] not in seen:
                result.append(values[offset]);seen.add(values[offset])
                if len(result)==MAX_CANDIDATES:break
        offset+=1
    counts={branch:len(ids) for branch,ids in branches.items()}
    counts['_reserved_windows']=canonical_reserved_count
    counts['_reserved_scope_windows']=scope_count
    counts['_geometry_duplicates']=len(groups)-len(reserved)-len(candidates)
    return result,counts


def _exact_span(group,rows,parsed_texts,checked):
    ids=group['citation_ids'];members=[rows[cid] for cid in ids]
    version=members[0]['version_id']
    if group.get('version_id')!=version:raise RerankError('rerank_evidence_invalid')
    if version not in checked:
        record=parsed_texts[version]
        if (not isinstance(record,dict) or not isinstance(record.get('parsed_text'),str)
            or not isinstance(record.get('parsed_sha256'),str)
            or _sha(record['parsed_text'])!=record['parsed_sha256']):
            raise RerankError('rerank_evidence_invalid')
        checked[version]=(record['parsed_text'],record['parsed_sha256'])
    text,digest=checked[version]
    for row in members:
        start,end=row.get('start_char'),row.get('end_char')
        if (type(start) is not int or type(end) is not int or not 0<=start<end<=len(text)
            or row.get('parsed_sha256')!=digest or row.get('excerpt')!=text[start:end]
            or row.get('excerpt_sha256')!=_sha(row['excerpt'])
            or row.get('visibility')!='public' or row.get('data_origin')!='real'
            or row.get('status')!='verified' or not row.get('enabled')
            or row.get('paused') or not row.get('read_allowed') or not row.get('outbound_allowed')):
            raise RerankError('rerank_evidence_invalid')
    start,end=group.get('start_char'),group.get('end_char')
    if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):
        raise RerankError('rerank_evidence_invalid')
    overlap={cid for cid,row in rows.items() if row.get('version_id')==version
             and row['start_char']<end and row['end_char']>start}
    if overlap!=set(ids):raise RerankError('rerank_evidence_invalid')
    cursor=start
    for row in sorted(members,key=lambda row:(row['start_char'],row['end_char'])):
        if row['start_char']>cursor:raise RerankError('rerank_evidence_invalid')
        cursor=max(cursor,row['end_char'])
    if cursor<end:raise RerankError('rerank_evidence_invalid')
    # title 与 span 的来源都只能是同事务、同版本的已核验原件。group
    # 自带 title/text/label 不参与评分；不对任意查询生成来源标签。
    titles={row.get('title') for row in rows.values() if row.get('version_id')==version}
    if titles=={None}:
        return text[start:end] # 无标题的合成/历史调用保持精确原文合同。
    if (len(titles)!=1 or any(not isinstance(title,str) or not 1<=len(title)<=240 for title in titles)):
        raise RerankError('rerank_evidence_invalid')
    title=next(iter(titles))
    record=parsed_texts[version]
    if record.get('title',title)!=title:raise RerankError('rerank_evidence_invalid')
    return title+'\n'+text[start:end]


def rerank_groups(query,groups,rows,parsed_texts,reranker=None,*,deadline=None,preselected_ids=None):
    """返回被评分的合法 group ID 顺序；调用方在原次序之后保留未评分组。

    只有显式开启才加载本地模型。失败返回空顺序及固定码，调用方沿用原序；
    不增加相似度阈值、不接受自定义候选文本、也不替代原子预算规划。
    内部共同池可传 preselected_ids；仅省略再次挑池，完整准入和原文校验仍执行。
    """
    meta={'policy':ALGORITHM_POLICY,'used':False,'reason':None,'profile_id':None,
          'pool_count':0,'scored_count':0,'candidate_rejections':{},'admission_filtered_groups':{},'pool_branch_counts':{},
          'pool_reserved_window_count':0,'pool_reserved_scope_window_count':0,
          'pool_geometry_duplicate_count':0,'pool_version_count':0,
          'pool_selection':'automatic_branch_pool' if preselected_ids is None else 'inherited_common_pool',
          'ranked_groups':[]}
    if os.environ.get('NAVREN_RERANK_ENABLED','0')!='1':
        meta['reason']='rerank_disabled';return [],meta
    if not isinstance(query,str) or not query.strip():
        meta['reason']='rerank_input_invalid';return [],meta
    if len(query)>MAX_QUERY_CHARS:
        meta['reason']='rerank_query_too_long';return [],meta
    try:
        deadline=_call_deadline(deadline)
        from .evidence_group_selection import _rows,_normal,SelectionError
        rows=_rows(rows);eligible=set(rows)
        valid=[];seen=set()
        if not isinstance(groups,(list,tuple)) or len(groups)>20000 or not isinstance(parsed_texts,dict):
            raise RerankError('rerank_input_invalid')
        for raw in groups:
            _check_deadline(deadline)
            group=_normal(raw,eligible,rows)
            if group['group_id'] in seen:raise RerankError('rerank_input_invalid')
            seen.add(group['group_id'])
            if group['rejection']:
                if group['rejection'] in ('not_eligible','core_completion_limit','mixed_boundary'):
                    counts=meta['admission_filtered_groups'];code=group['rejection']
                else:
                    counts=meta['candidate_rejections'];code='rerank_evidence_invalid'
                counts[code]=counts.get(code,0)+1
                continue
            recall=group.get('recall_rank_positions',{})
            ranks=group.get('rank_positions',{})
            child_rank=group.get('child_rank')
            if (not isinstance(recall,dict) or any(k not in RECALL_BRANCH_NAMES or type(v) is not int or not 1<=v<=20000 for k,v in recall.items())
                or not isinstance(ranks,dict) or any(k not in ('canonical','sentence','rerank') or (v is not None and (type(v) is not int or not 1<=v<=20000)) for k,v in ranks.items())
                or (child_rank is not None and (type(child_rank) is not int or not 1<=child_rank<=20000))):
                raise RerankError('rerank_input_invalid')
            valid.append(group)
        byid={group['group_id']:group for group in valid}
        if preselected_ids is None:
            pool,counts=_candidate_pool(valid)
            meta.update(pool_reserved_window_count=counts.pop('_reserved_windows'),
                        pool_reserved_scope_window_count=counts.pop('_reserved_scope_windows'),
                        pool_geometry_duplicate_count=counts.pop('_geometry_duplicates'),pool_branch_counts=counts)
        else:
            # 共同池的代表可能借助同几何句组的强分支进入；重新挑池会丢掉
            # 该资格。只继承已准入的 ID，绝不接受未知或被拒组，也不裁短。
            if (not isinstance(preselected_ids,(list,tuple)) or len(preselected_ids)>MAX_CANDIDATES
                or any(not isinstance(gid,str) or not _ID.fullmatch(gid) or gid not in byid for gid in preselected_ids)
                or len(set(preselected_ids))!=len(preselected_ids)):
                raise RerankError('rerank_input_invalid')
            pool=list(preselected_ids)
        meta['pool_count']=len(pool)
        meta['pool_version_count']=len({byid[gid].get('version_id') for gid in pool})
        texts=[];scoring_ids=[];checked={}
        for gid in pool:
            _check_deadline(deadline)
            try:
                texts.append(_exact_span(byid[gid],rows,parsed_texts,checked));scoring_ids.append(gid)
            except (RerankError,KeyError,TypeError):
                code='rerank_evidence_invalid'
                meta['candidate_rejections'][code]=meta['candidate_rejections'].get(code,0)+1
        if not texts:raise RerankError('rerank_empty_candidates')
        _check_deadline(deadline)
        model=get_reranker(deadline=deadline) if reranker is None else reranker
        _check_deadline(deadline)
        meta['profile_id']=model.profile_id
        output=(model.score_documents(query,texts,deadline=deadline) if isinstance(model,LocalReranker)
                else model.score_documents(query,texts))
        _check_deadline(deadline)
        if not isinstance(output,list) or len(output)!=len(texts):raise RerankError('rerank_inference_failed')
        scores={};windows={}
        for gid,value in zip(scoring_ids,output):
            _check_deadline(deadline)
            if not isinstance(value,dict):raise RerankError('rerank_inference_failed')
            if value.get('reason') is not None:
                if value['reason'] not in ERROR_CODES or value.get('score') is not None:
                    raise RerankError('rerank_inference_failed')
                if value['reason']=='rerank_timeout':raise RerankError('rerank_timeout')
                code=value['reason'];meta['candidate_rejections'][code]=meta['candidate_rejections'].get(code,0)+1
                continue
            score=value.get('score');count=value.get('window_count')
            if (isinstance(score,bool) or not isinstance(score,Real) or not math.isfinite(score)
                or type(count) is not int or not 1<=count<=MAX_WINDOWS):
                raise RerankError('rerank_inference_failed')
            scores[gid]=float(score);windows[gid]=count
        ordered=sorted(scores,key=lambda gid:(-scores[gid],gid))
        meta.update(used=bool(ordered),scored_count=len(ordered),
            reason='rerank_partial' if meta['candidate_rejections'] else None,
            ranked_groups=[{'group_id':gid,'rank':index+1,'window_count':windows[gid]} for index,gid in enumerate(ordered)])
        if not ordered:meta['reason']='rerank_empty_candidates'
        _check_deadline(deadline)
        return ordered,meta
    except Exception as error:
        code=getattr(error,'code',None)
        if (not isinstance(deadline,bool) and isinstance(deadline,Real)
            and math.isfinite(deadline) and time.monotonic()>=deadline):code='rerank_timeout'
        # 发布前校验也可能在排序/诊断组装后超时；不保留成功标识或旧顺序。
        meta.update(used=False,scored_count=0,ranked_groups=[])
        meta['reason']=code if code in ERROR_CODES else 'rerank_input_invalid' if isinstance(error,(ValueError,TypeError,KeyError)) else 'rerank_inference_failed'
        return [],meta
