"""受信宿主发行的合成/历史目录信息。候选；不注册 RAG、金融指标或交易资格。"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import date
import hashlib
import json
import math
import re
import secrets
import threading
import time
import unicodedata
from urllib.parse import urlsplit
from simulated_bank import BankError

CONTRACT = 'navren.catalog-information/1'
SOURCE_FIELDS = frozenset({'source','as_of','sha256','products','verified_nav_products','notice'})
PRODUCT_FIELDS = frozenset({'code','name','share_class_id','channel_id','source_url','disclosure_as_of','sale_status','fee_status'})
SNAPSHOT_FIELDS = frozenset({'contract','source_handle','issuer_generation','source_revision','revocation_generation','request_binding','full_package_sha256','source_kind','original_source','original_as_of','historical_only','execution_authority','current_evidence_eligible','rag_canonical_eligible','monetary_claimable'})

def fail(code='CATALOG_INFORMATION_INVALID'):
    raise BankError(code,'历史目录资料或许可已变化；本轮不能发布，请重新查询。',409)

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _text(value, maximum):
    if type(value) is not str or not value or len(value)>maximum or any(ord(c)<32 for c in value):fail()
    if re.search(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d)',value):fail()
    return value

def _dated(value):
    if type(value) is not str:fail()
    try:
        if date.fromisoformat(value).isoformat()!=value:fail()
    except ValueError:fail()
    return value

def validate_source(value, source_kind):
    # 此版本仅支持原 S06 的信息结构；不放开数值、其他商品字段或任意正文。
    if source_kind not in ('synthetic','historical') or type(value) is not dict or set(value)!=SOURCE_FIELDS:fail()
    _text(value['source'],80);_dated(value['as_of']);_text(value['sha256'],100);_text(value['notice'],500)
    if value['verified_nav_products']!=[] or type(value['products']) is not list or not 1<=len(value['products'])<=12:fail()
    seen=set()
    for row in value['products']:
        if type(row) is not dict or set(row)!=PRODUCT_FIELDS:fail()
        for field in ('code','name','share_class_id','channel_id'):_text(row[field],120)
        if not re.fullmatch(r'[A-Za-z0-9._:-]+',row['code']) or not re.fullmatch(r'[A-Za-z0-9._:-]+',row['share_class_id']) or not re.fullmatch(r'[A-Za-z0-9._:-]+',row['channel_id']):fail()
        url=_text(row['source_url'],500)
        try:parsed=urlsplit(url)
        except ValueError:fail()
        if parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or re.search(r'\s',url):fail()
        if row['sale_status']!='unknown' or row['fee_status']!='unknown':fail()
        if _dated(row['disclosure_as_of'])!=value['as_of']:fail()
        key=tuple(row[k] for k in ('code','share_class_id','channel_id'))
        if key in seen:fail()
        seen.add(key)
    return deepcopy(value)

def validate_snapshot_shape(snapshot):
    if type(snapshot) is not dict or set(snapshot)!=SNAPSHOT_FIELDS or snapshot['contract']!=CONTRACT:fail()
    for key in ('source_handle','issuer_generation','source_revision','request_binding','full_package_sha256'):
        if type(snapshot[key]) is not str or not re.fullmatch(r'[0-9a-f]{64}',snapshot[key]):fail()
    if type(snapshot['revocation_generation']) is not int or snapshot['revocation_generation']<0:fail()
    if snapshot['source_kind'] not in ('synthetic','historical'):fail()
    _text(snapshot['original_source'],80);_dated(snapshot['original_as_of'])
    for key,expected in {'historical_only':True,'execution_authority':False,'current_evidence_eligible':False,'rag_canonical_eligible':False,'monetary_claimable':False}.items():
        if type(snapshot[key]) is not bool or snapshot[key] is not expected:fail()
    return deepcopy(snapshot)

class CatalogInformationSource:
    """只由宿主 bootstrap 构造；HTTP/model 的资料字典没有发行入口。"""
    def __init__(self, original_package, *, source_kind, clock=time.monotonic, ttl=105):
        self._package=validate_source(original_package,source_kind)
        self._kind=source_kind;self._digest=digest(self._package)
        self._generation=secrets.token_hex(32);self._revision=secrets.token_hex(32)
        if type(ttl) not in (int,float) or not math.isfinite(ttl) or not 1<=ttl<=105 or not callable(clock):fail()
        self._clock=clock;self._ttl=float(ttl)
        self._lock=threading.RLock();self._revocation=0;self._enabled=True;self._pins={};self._history_authority=None
    def pin(self, *, trusted_request_binding):
        if type(trusted_request_binding) is not tuple or len(trusted_request_binding)!=4 or any(type(v) not in (str,int) for v in trusted_request_binding):fail()
        with self._lock:
            if not self._enabled:fail('CATALOG_INFORMATION_REVOKED')
            handle=secrets.token_hex(32)
            snapshot={'contract':CONTRACT,'source_handle':handle,'issuer_generation':self._generation,
                'source_revision':self._revision,'revocation_generation':self._revocation,
                'request_binding':digest(list(trusted_request_binding)),'full_package_sha256':self._digest,
                'source_kind':self._kind,'original_source':self._package['source'],'original_as_of':self._package['as_of'],
                'historical_only':True,'execution_authority':False,'current_evidence_eligible':False,
                'rag_canonical_eligible':False,'monetary_claimable':False}
            if len(self._pins)>=256:self._pins.pop(next(iter(self._pins)))
            self._pins[handle]=(deepcopy(snapshot),trusted_request_binding,self._clock()+self._ttl)
            return CatalogInformationObserver(self,snapshot,trusted_request_binding)
    def assert_snapshot(self,snapshot,binding):
        validate_snapshot_shape(snapshot)
        with self._lock:
            pin=self._pins.get(snapshot['source_handle'])
            if (not self._enabled or not pin or pin[0]!=snapshot or pin[1]!=binding or pin[2]<=self._clock()
                or snapshot['revocation_generation']!=self._revocation or snapshot['issuer_generation']!=self._generation
                or snapshot['source_revision']!=self._revision or snapshot['full_package_sha256']!=digest(self._package)):fail('CATALOG_INFORMATION_STALE')
    def assert_bound_snapshot(self,snapshot,*,workspace,privacy_revision,personal_revision,request_id=None):
        validate_snapshot_shape(snapshot)
        with self._lock:
            pin=self._pins.get(snapshot['source_handle'])
            if not pin:fail('CATALOG_INFORMATION_STALE')
            binding=pin[1]
            if (binding[1:]!=(workspace,privacy_revision,personal_revision)
                or request_id is not None and binding[0]!=request_id):fail('CATALOG_INFORMATION_STALE')
            self.assert_snapshot(snapshot,binding)
    @contextmanager
    def publication_guard(self,snapshot,*,workspace,privacy_revision,personal_revision,request_id=None):
        with self._lock:
            checkpoint=lambda:self.assert_bound_snapshot(snapshot,workspace=workspace,privacy_revision=privacy_revision,
                personal_revision=personal_revision,request_id=request_id)
            checkpoint();yield checkpoint;checkpoint()
    def bind_history_authority(self, authority):
        if not callable(getattr(authority, 'catalog_revoke_guard', None)):fail('CATALOG_HISTORY_AUTHORITY_REQUIRED')
        with self._lock:
            previous=self._history_authority
            if previous is not None and (getattr(previous, 'g', None) is not getattr(authority, 'g', None)
                    or getattr(previous, 'store', None) is not getattr(authority, 'store', None)):
                fail('CATALOG_HISTORY_AUTHORITY_CHANGED')
            if previous is None:self._history_authority=authority
    def revoke(self, *, selection_only=False):
        if type(selection_only) is not bool:fail()
        authority=self._history_authority
        guard=authority.catalog_revoke_guard(self) if authority is not None and not selection_only else nullcontext()
        # durable revoke 先持原bank锁，再取得source锁；与历史发送一致。
        with guard:
            with self._lock:self._enabled=False;self._revocation+=1
    def enable(self):
        with self._lock:self._enabled=True  # 旧 generation 快照仍被拒绝，不能复活。

class CatalogInformationObserver:
    def __init__(self,source,snapshot,binding):
        if type(source) is not CatalogInformationSource:fail()
        self.source=source;self._snapshot=deepcopy(snapshot);self._binding=binding;self.read_count=0
    def snapshot(self):
        self.assert_fresh();return deepcopy(self._snapshot)
    def assert_fresh(self):self.source.assert_snapshot(self._snapshot,self._binding)
    @contextmanager
    def publication_guard(self):
        with self.source._lock:
            self.assert_fresh()
            yield self.assert_fresh
            self.assert_fresh()
    def read(self,query='',limit=12):
        if query!='' or type(limit) is not int or not 1<=limit<=12:fail()
        with self.source._lock:
            self.assert_fresh();original=deepcopy(self.source._package)
            selected=[r for r in original['products'] if not query or query.casefold() in ' '.join(r[k] for k in ('code','name','share_class_id','channel_id')).casefold()]
            original['products']=selected[:limit]
            original['catalog_information']=self.snapshot()
            self.read_count+=1
            return original
    def assert_answer(self,args,user_text,knowledge_ready):
        self.assert_fresh()
        if self.read_count<1 or type(args) is not dict:fail('CATALOG_INFORMATION_UNSUPPORTED_CLAIM')
        expected=render_conditions(self.read(),user_text,knowledge_ready=knowledge_ready)
        # 原审核 wire 使用 NFKC；只接纳这一受信原件的精确两种载体。
        if args.get('text') not in (expected,unicodedata.normalize('NFKC',expected)):fail('CATALOG_INFORMATION_UNSUPPORTED_CLAIM')
        if args.get('amount_claims') or args.get('draft_ids') or args.get('widget_ids') or args.get('interaction'):fail('CATALOG_INFORMATION_EXECUTION_FORBIDDEN')
    def require_tool(self,name):
        if name not in {'load_skill','knowledge_status','knowledge_search','public_products','answer_reviewed'}:fail('CATALOG_INFORMATION_TOOL_FORBIDDEN')

class ScopedProductCatalog:
    def __init__(self,current,information=None):
        if information is not None and type(information) is not CatalogInformationObserver:fail()
        self.current,self.information=current,information
    def read(self,query='',limit=24,offset=0,*,view='current_disclosure'):
        if view=='current_disclosure':return self.current.read(query,limit,offset)
        if view!='historical_conditions' or self.information is None or offset!=0:fail()
        return self.information.read(query,min(limit,12))

def project_information_data(value):
    if type(value) is not dict or set(value) not in (SOURCE_FIELDS|{'catalog_information'},SOURCE_FIELDS|{'catalog_information','conditions_draft'}):fail()
    snapshot=validate_snapshot_shape(value['catalog_information'])
    raw={k:deepcopy(value[k]) for k in SOURCE_FIELDS}
    # 完整包首次读取/审核以 hash 绑定，分页须由独立契约支持，当前禁止分页/筛选投影。
    validate_source(raw,snapshot['source_kind'])
    if digest(raw)!=snapshot['full_package_sha256'] or raw['as_of']!=snapshot['original_as_of'] or raw['source']!=snapshot['original_source']:fail()
    result={**raw,'catalog_information':snapshot}
    if 'conditions_draft' in value:
        # 有限派生正文不参与原六字段 SHA；不接纳自由正文或用户私密原话。
        draft=_text(value['conditions_draft'],6000)
        expected={_conditions_text(result,due_next_week=due,knowledge_ready=ready) for due in (False,True) for ready in (False,True)}
        expected|={unicodedata.normalize('NFKC',text) for text in expected}
        if draft not in expected:fail('CATALOG_INFORMATION_UNSUPPORTED_CLAIM')
        result['conditions_draft']=draft
    return result

def render_conditions(value,user_text,*,knowledge_ready):
    data=project_information_data(value)
    if type(user_text) is not str or type(knowledge_ready) is not bool:fail()
    return _conditions_text(data,due_next_week='下周' in user_text,knowledge_ready=knowledge_ready)

def _conditions_text(data,*,due_next_week,knowledge_ready):
    # 仅消费已经校验的完整原包；不调用投影/renderer，避免派生字段验证递归。
    rows=data['products'];identities='；'.join(r['name']+'（份额 '+r['share_class_id']+'，历史渠道 '+r['channel_id']+'）' for r in rows)
    label='合成历史' if data['catalog_information']['source_kind']=='synthetic' else '历史'
    deadline='你提到下周要用钱，' if due_next_week else '你的用款期限，'
    rag_status='资料检索尚未接入；' if not knowledge_ready else '真实资料检索状态单独核验；'
    return (rag_status+'截至'+data['as_of']+'的'+label+'目录只支持身份与缺项比较。'+identities+'。'
        '各份额、渠道必须分别核对适用费用、折扣和赎回条件，不能从名称推断费率相同。'
        +deadline+'交易日规则尚未核实，到账期限也未核实，无法确认能否及时退出并到账。'
        '当前费用孰低与渠道在售状态未知；旧资料及模拟产品规则不能替代公开真实费用，本轮不推荐购买。')

class CatalogInformationEvidenceObserver:
    """原 canonical observer 的本轮组合；历史资料不进入 RAG pins/snapshot。"""
    def __init__(self,current,information):
        if type(information) is not CatalogInformationObserver:fail()
        self.current,self.information=current,information
    def __getattr__(self,name):return getattr(self.current,name)
    def assert_fresh(self):
        self.current.assert_fresh();self.information.assert_fresh()
    def snapshot(self,knowledge):return self.current.snapshot(knowledge)
