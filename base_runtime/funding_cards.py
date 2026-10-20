"""本轮资金卡：原资金服务发行、有限引用/分页，不计算预算或授予执行权。"""
from collections import OrderedDict
from contextlib import closing, contextmanager
from copy import deepcopy
import hmac
import hashlib
import json
import re
from pathlib import Path
import secrets
import threading
import time

from agent_runtime.metric_claims import MetricRegistry, REF_FIELDS, MetricClaimError, monetary_occurrences
import financial_metrics
from financial_metrics import fingerprint
from simulated_bank import BankError
from simulated_bank.service import digest
from user_memory import privacy_revision

CONTRACT = 'navren.funding-card/1'
SECTIONS = frozenset({'input_refs','source_versions','excluded_items','coverage_gaps','assumed_inputs'})
LOADED_HELPER_CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LOADED_FINANCE_CODE = hashlib.sha256(Path(financial_metrics.__file__).read_bytes()).hexdigest()


def reject(code='FUNDING_CARD_INVALID', status=422):
    raise BankError(code,'资金卡引用、范围或来源无效，请重新读取。',status)


class FundingCards:
    """短期内存发行凭据；财务原件仍只保存在 FinancialMetrics 的原发行池。"""
    def __init__(self, gateway, *, ttl_seconds=300, max_cards=128, clock=None):
        if (type(ttl_seconds) not in (int,float) or not 0 < ttl_seconds <= 3600
                or type(max_cards) is not int or not 1 <= max_cards <= 512):reject()
        self.g,self.bank,self.metrics = gateway,gateway.bank,gateway.financial_metrics
        self.ttl,self.maximum,self.clock = ttl_seconds,max_cards,clock or time.monotonic
        self._cards,self._lock = OrderedDict(),threading.RLock()

    @staticmethod
    def _request_id(value):
        if type(value) is not str or not value.strip() or len(value)>160:reject()

    @contextmanager
    def _read(self, db=None):
        if db is not None:
            paths=[r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main']
            if (not db.in_transaction or len(paths)!=1
                    or Path(paths[0]).resolve()!=Path(self.bank.database).resolve()):reject()
            yield db
            return
        with closing(self.bank.connect()) as connection:
            connection.execute('BEGIN')
            try:yield connection
            finally:connection.rollback()

    def _identity(self, workspace, db):
        guard=getattr(self.bank,'request_identity_guard',None)
        if not callable(guard):reject('FUNDING_CARD_IDENTITY_REQUIRED',403)
        guard(db)  # 普通读取事务不会自动调用银行交易身份门。
        sessions=getattr(self.g,'demo_sessions',None)
        context=getattr(sessions,'_request',None)
        request=context.get() if context is not None else None
        if not request or len(request)!=2 or request[1]!=workspace:reject('FUNDING_CARD_IDENTITY_REQUIRED',403)
        token_hash=digest(request[0])
        row=db.execute('SELECT * FROM demo_sessions WHERE token_hash=?',(token_hash,)).fetchone()
        if row:
            if row['workspace_id']!=workspace or row['revoked'] or row['expires_at']<=self.bank.clock():
                reject('SESSION_CHANGED',409)
            return {'workspace_id':workspace,'token_hash':token_hash,'generation':row['generation'],'mode':'demo'}
        row=db.execute('SELECT token_hash FROM workspaces WHERE id=?',(workspace,)).fetchone()
        if not row or not hmac.compare_digest(row['token_hash'],token_hash):reject('SESSION_CHANGED',409)
        return {'workspace_id':workspace,'token_hash':token_hash,'generation':0,'mode':'legacy'}

    @staticmethod
    def _code_versions():
        return {'funding_cards':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'financial_metrics':hashlib.sha256(Path(financial_metrics.__file__).read_bytes()).hexdigest()}

    @staticmethod
    def _source_rows(workspace, db):
        # cashflow 额外消费的原行；不能依赖未必递增的 workspace.version。
        result={table:fingerprint([dict(r) for r in db.execute(
            'SELECT * FROM '+table+' WHERE workspace_id=? ORDER BY rowid',(workspace,))])
            for table in ('orders','beneficiaries','operations')}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='navren_demo_accounts'").fetchone():
            result['navren_demo_accounts']=fingerprint([dict(r) for r in db.execute(
                'SELECT * FROM navren_demo_accounts WHERE workspace_id=? ORDER BY rowid',(workspace,))])
        else:result['navren_demo_accounts']='not_connected'
        return result

    @staticmethod
    def _projection_hash(card):
        projection=deepcopy(card)
        projection['card_ref'].pop('service_signature')
        projection['details_request']['card_ref'].pop('service_signature')
        return fingerprint(projection)

    def _coverage(self, workspace, parent, db):
        binding=parent.get('funding_source_binding')
        if binding is None:return None,'explicit_unknown',parent['funding']['metrics']['current_budget']['data_scope']
        source=self.metrics._funding_source(workspace,binding['days'],db)
        if source['envelope_digest']!=binding['envelope_digest']:reject('METRIC_VERSION_CONFLICT',409)
        return source['coverage_complete'],('explicit_complete' if source['coverage_complete'] else 'explicit_incomplete'),source['data_scope']

    @staticmethod
    def _funding_limits(parent):
        judgments=parent['funding'].get('floor_judgments',{})
        if type(judgments) is not dict:reject()
        limits={key:deepcopy(judgments.get(key)) for key in ('adequacy','intraday_solvency')}
        for value in limits.values():
            if value is not None:
                if type(value) is not str or not value or len(value)>1000:reject()
                try:
                    if monetary_occurrences(value):reject()
                except MetricClaimError:reject()
        limits['undeclared']=[key for key,value in limits.items() if value is None]
        return limits

    @staticmethod
    def _items(metric, section):
        value=metric[section]
        if section=='source_versions':
            return [{'source':k,'version':v} for k,v in sorted(value.items())]
        return deepcopy(value)

    def _parent(self, workspace, metadata, db):
        if self._code_versions()!=metadata['body']['code_versions']:reject('FUNDING_CARD_RESTART_REQUIRED',503)
        identity=self._identity(workspace,db)
        if identity!=metadata['identity']:reject('FUNDING_CARD_IDENTITY_CHANGED',409)
        if (privacy_revision(db,workspace)!=metadata['body']['privacy_revision']
                or self.metrics.personal_revision(workspace,db)!=metadata['body']['personal_revision']):
            reject('PERSONAL_VERSION_CONFLICT',409)
        if self._source_rows(workspace,db)!=metadata['body']['cashflow_source_rows']:
            reject('METRIC_VERSION_CONFLICT',409)
        with self.metrics._tool_lock:
            issued=self.metrics._funding_issuances.get(metadata['body']['parent_snapshot_id'])
            parent=deepcopy(issued[0]) if issued else None
        if parent is None:reject('METRIC_BUDGET_NOT_ISSUED',409)
        if fingerprint(parent)!=metadata['body']['parent_full_hash']:reject('METRIC_TAMPERED',409)
        self.metrics.validate(workspace,parent,db)
        if parent['source_versions']!=metadata['source_versions']:reject('METRIC_VERSION_CONFLICT',409)
        metric=parent['funding']['metrics']['current_budget']
        if (metric['metric_id']!=metadata['body']['parent_metric_id']
                or metric['formula_id']!='funds.current_budget/1' or metric['basis']!='floor_surplus'):
            reject('METRIC_BUDGET_INVALID',409)
        registry=MetricRegistry();registry.register(metric,kind='recorded_fact',evidence_id=parent['snapshot_id'])
        return parent,registry.lookup(metric['metric_id'])['metric']

    def query(self, workspace, *, request_id, days=60):
        self._request_id(request_id)
        if type(days) is not int or not 1<=days<=365:reject()
        code=self._code_versions()
        if code!={'funding_cards':LOADED_HELPER_CODE,'financial_metrics':LOADED_FINANCE_CODE}:
            reject('FUNDING_CARD_RESTART_REQUIRED',503)
        with self._read() as db:
            identity=self._identity(workspace,db);extra_sources=self._source_rows(workspace,db)
        # 必须亲读本轮原件，不接收客户/模型提供的 parent 或金额。
        parent=self.metrics.snapshot(workspace,days=days)
        with self._read() as db:
            if self._identity(workspace,db)!=identity:reject('FUNDING_CARD_IDENTITY_CHANGED',409)
            if self._source_rows(workspace,db)!=extra_sources:reject('METRIC_VERSION_CONFLICT',409)
            self.metrics.validate(workspace,parent,db)
            privacy=privacy_revision(db,workspace);personal=self.metrics.personal_revision(workspace,db)
            if parent['privacy_revision']!=privacy:reject('PERSONAL_VERSION_CONFLICT',409)
            metric=parent['funding']['metrics']['current_budget']
            if metric['formula_id']!='funds.current_budget/1' or metric['basis']!='floor_surplus':reject()
            registry=MetricRegistry();registry.register(metric,kind='recorded_fact',evidence_id=parent['snapshot_id'])
            metric=registry.lookup(metric['metric_id'])['metric']
            coverage,coverage_status,source_scope=self._coverage(workspace,parent,db)
            nonce='funding-card-'+secrets.token_hex(16)
            body={'contract':CONTRACT,'card_id':nonce,'request_id':request_id,'workspace_id':workspace,
                  'arguments':{'days':days},'parent_snapshot_id':parent['snapshot_id'],
                  'parent_metric_id':metric['metric_id'],'parent_full_hash':fingerprint(parent),
                  'source_versions_hash':fingerprint(parent['source_versions']),
                  'identity_hash':fingerprint(identity),'privacy_revision':privacy,'personal_revision':personal,
                  'code_versions':code,'cashflow_source_rows':extra_sources,
                  'metric_kind':'recorded_fact','sections':sorted(SECTIONS),'expires_at':self.clock()+self.ttl}
            signature=self.metrics._tool_signature(body)
            ref={'card_id':nonce,'service_signature':signature}
            counts={key:len(self._items(metric,key)) for key in sorted(SECTIONS)}
            card={'contract':CONTRACT,'card_ref':ref,'request_id':request_id,'workspace_id':workspace,
                  'parent_snapshot_id':parent['snapshot_id'],'parent_full_hash':body['parent_full_hash'],
                  'source':deepcopy(parent.get('source')),
                  'source_status':'declared' if parent.get('source') is not None else 'unknown',
                  'metric':{key:deepcopy(metric[key]) for key in ('contract_version',*REF_FIELDS) if key!='source_versions'},
                  'metric_kind':'recorded_fact','details_counts':counts,
                  'status':metric['calculation_status'],'summary_complete':False,
                  'financial_coverage_complete':coverage,'financial_coverage_status':coverage_status,
                  'source_scope':source_scope,'funding_baseline_kind':parent['funding_baseline_kind'],
                  'funding_mode':parent['funding'].get('mode'),
                  'funding_limits':self._funding_limits(parent),
                  'coverage_scope':parent['coverage_scope'],
                  'coverage_gaps_preview':deepcopy(metric['coverage_gaps'][:8]),
                  'coverage_gaps_truncated':counts['coverage_gaps']>8,
                  'assumed_inputs_preview':deepcopy(metric['assumed_inputs'][:8]),
                  'assumed_inputs_truncated':counts['assumed_inputs']>8,
                  'parent_coverage_gaps':deepcopy(parent.get('coverage_gaps',[])),
                  'parent_coverage_gap_count':len(parent.get('coverage_gaps',[])),
                  'details_request':{'method':'detail','card_ref':deepcopy(ref),'request_id':request_id,
                                     'sections':sorted(SECTIONS),'max_sections':2,'max_limit':8},
                  'summary_only':True,'complete':False,'requires_detail':any(counts.values()),
                  'required_followup_sections':[key for key in ('coverage_gaps','assumed_inputs') if counts[key]>8],
                  'no_financial_side_effect':True,'execution_authority':False,
                  'amount_claim_limit':24,'literal_validation_required':True}
            for marker in ('synthetic_history_metrics','source_snapshot_is_historical'):
                if marker in parent:card[marker]=deepcopy(parent[marker])
            # 有界卡不能把超长重要缺项截成“完整”；超限整体拒绝。
            if len(json.dumps(card,ensure_ascii=False,allow_nan=False).encode())>16384:
                reject('FUNDING_CARD_DELIVERY_LIMIT',409)
            body['model_projection_hash']=self._projection_hash(card)
            signature=self.metrics._tool_signature(body)
            ref['service_signature']=signature
            card['details_request']['card_ref']['service_signature']=signature
            metadata={'body':body,'signature':signature,'identity':identity,
                      'source_versions':deepcopy(parent['source_versions']),'model_card':deepcopy(card)}
            self._parent(workspace,metadata,db)
        with self._lock:
            self._cards[nonce]=metadata
            while len(self._cards)>self.maximum:self._cards.popitem(last=False)
        return {'local_parent_snapshot':deepcopy(parent),'model_card':card}

    def _reference(self, workspace, card_ref, request_id):
        self._request_id(request_id)
        if (type(card_ref) is not dict or set(card_ref)!={'card_id','service_signature'}
                or any(type(card_ref[k]) is not str or not card_ref[k] for k in card_ref)
                or re.fullmatch(r'funding-card-[0-9a-f]{32}',card_ref['card_id']) is None
                or re.fullmatch(r'[0-9a-f]{64}',card_ref['service_signature']) is None):reject()
        with self._lock:metadata=deepcopy(self._cards.get(card_ref['card_id']))
        if metadata is None:reject('FUNDING_CARD_NOT_ISSUED',409)
        body=metadata['body']
        if body['workspace_id']!=workspace or body['request_id']!=request_id:reject('FUNDING_CARD_SCOPE',409)
        if (not hmac.compare_digest(card_ref['service_signature'],metadata['signature'])
                or not hmac.compare_digest(metadata['signature'],self.metrics._tool_signature(body))):
            reject('METRIC_TAMPERED',409)
        if self._projection_hash(metadata['model_card'])!=body['model_projection_hash']:
            reject('METRIC_TAMPERED',409)
        if self.clock()>=body['expires_at']:reject('FUNDING_CARD_EXPIRED',409)
        return metadata

    def resolve(self, workspace, card_ref, *, request_id, db=None):
        """只返回服务亲发投影；调用方改写 model_card 不成为审阅或预算依据。"""
        metadata=self._reference(workspace,card_ref,request_id)
        with self._read(db) as connection:
            parent,_=self._parent(workspace,metadata,connection)
            return {'model_card':deepcopy(metadata['model_card']),'local_parent_snapshot':parent}

    def detail(self, workspace, card_ref, *, request_id, sections, db=None):
        metadata=self._reference(workspace,card_ref,request_id)
        if type(sections) is not list or not 1<=len(sections)<=2:reject()
        seen=set()
        for page in sections:
            if (type(page) is not dict or set(page)!={'section','offset','limit'}
                    or type(page['section']) is not str or page['section'] not in SECTIONS
                    or page['section'] in seen or type(page['offset']) is not int or page['offset']<0
                    or type(page['limit']) is not int or not 1<=page['limit']<=8):reject()
            seen.add(page['section'])
        body=metadata['body']
        with self._read(db) as connection:
            parent,metric=self._parent(workspace,metadata,connection)
            pages=[]
            for page in sections:
                values=self._items(metric,page['section']);offset,limit=page['offset'],page['limit']
                if offset>len(values):reject()
                items=values[offset:offset+limit];end=offset+len(items)
                pages.append({**page,'total_count':len(values),'returned_count':len(items),
                              'complete':offset==0 and end==len(values),'items':items,
                              'next_offset':end if end<len(values) else None})
            result={'contract':CONTRACT,'card_ref':deepcopy(card_ref),'request_id':request_id,
                    'workspace_id':workspace,'parent_snapshot_id':parent['snapshot_id'],
                    'parent_full_hash':body['parent_full_hash'],'metric_id':metric['metric_id'],
                    'metric_kind':'recorded_fact','sections':pages,'complete':False,
                    'no_financial_side_effect':True,'execution_authority':False}
            if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>32768:
                reject('FUNDING_CARD_DELIVERY_LIMIT',409)
            return result
