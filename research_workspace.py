"""版本化研究与保单私有工作区；不从公开目录或研究草案授予交易权限。"""
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import base64
import hashlib
import json
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from simulated_bank import BankError
from user_memory import privacy_revision

CONTRACT = 'navren.research-workspace/1'
TZ = ZoneInfo('Asia/Shanghai')
KINDS = ('research', 'comparison', 'policy', 'task')
CONDITIONS = {'amount','currency','holding_days','target_date','channel','account_id',
              'risk_confirmation_id','obligation_ids','allowed_disclosure'}
STAGES = {'clarifying','evidence','ready','analyzing','reviewing','published','needs_input','conflict',
          'prepared','verifying','executing','succeeded','failed','unknown','cancelled'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS research_workspace_versions(
 registered_user_id TEXT NOT NULL,workspace_id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS research_workspace_records(
 id TEXT PRIMARY KEY,registered_user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
 kind TEXT NOT NULL,record_revision INTEGER NOT NULL,status TEXT NOT NULL,value TEXT NOT NULL,
 created_at REAL NOT NULL,updated_at REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS research_workspace_owner ON research_workspace_records(workspace_id,kind,deleted);
CREATE TABLE IF NOT EXISTS research_workspace_history(
 record_id TEXT NOT NULL,registered_user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
 record_revision INTEGER NOT NULL,value TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(record_id,record_revision));
CREATE TABLE IF NOT EXISTS research_workspace_attachments(
 id TEXT PRIMARY KEY,registered_user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
 upload_key TEXT NOT NULL,mime_type TEXT NOT NULL,sha256 TEXT NOT NULL,byte_count INTEGER NOT NULL,
 payload_cipher BLOB NOT NULL,value TEXT NOT NULL,created_at REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,
 UNIQUE(workspace_id,upload_key));
CREATE TABLE IF NOT EXISTS research_workspace_outbox(
 id TEXT PRIMARY KEY,registered_user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
 value TEXT NOT NULL,event_hash TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at REAL NOT NULL);
'''


def fail(code='RESEARCH_INVALID', message='研究资料格式无效。', status=422):
    raise BankError(code, message, status)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value): return hashlib.sha256(packed(value).encode()).hexdigest()


def fields(value, allowed, required=()):
    if type(value) is not dict or set(value)-set(allowed) or not set(required)<=set(value): fail()


def text(value, limit=160, empty=False):
    if (type(value) is not str or len(value)>limit or not empty and not value.strip()
            or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', value)): fail()
    return value.strip()


def ident(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value): fail()
    return value


def day(value, nullable=False):
    if value is None and nullable: return None
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat()!=value: raise ValueError
    except (ValueError,TypeError): fail(message='日期必须为有效的YYYY-MM-DD。')
    return value


def money(value):
    if type(value) is not str or not re.fullmatch(r'(?:0|[1-9]\d{0,12})(?:\.\d{1,2})?', value): fail(message='金额必须为最多两位小数的十进制字符串。')
    amount=Decimal(value)*100
    if amount>2**53-1: fail()
    return int(amount)


def bounded(value, depth=0):
    if depth>8: fail()
    if value is None or type(value) is bool: return
    if type(value) is int and abs(value)<=2**53-1: return
    if type(value) is str: text(value, 8000, True); return
    if type(value) in (dict,list) and len(value)<=100:
        if type(value) is dict:
            for k,v in value.items():
                if type(k) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', k) or k in ('__proto__','constructor','prototype'): fail()
                bounded(v,depth+1)
        else:
            for v in value: bounded(v,depth+1)
        return
    fail()


class ResearchWorkspace:
    def __init__(self, gateway, *, provider=None):
        self.g,self.store,self._provider=gateway,gateway.personal_data,provider
        self._request_privacy=ContextVar('research_request_privacy',default=None)
        self._failpoint=None
        self._refresh_mutex=threading.Lock();self._refresh_locks={};self._refresh_cache={}
        with closing(self.store.connect()) as db:
            db.executescript(SCHEMA);db.commit()
        self.store._permissions()
        with self.g.bank.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS research_workspace_receipts(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,event_hash TEXT NOT NULL,applied_at REAL NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS research_operation_intents(
                workspace_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,record_id TEXT NOT NULL,
                record_revision INTEGER NOT NULL,record_digest TEXT NOT NULL,privacy_revision INTEGER NOT NULL,
                provider_revision TEXT,core_digest TEXT NOT NULL,created_at REAL NOT NULL,
                dependencies TEXT NOT NULL DEFAULT '{}',funding TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(workspace_id,idempotency_key))''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(research_operation_intents)')}
            if 'dependencies' not in columns:db.execute("ALTER TABLE research_operation_intents ADD COLUMN dependencies TEXT NOT NULL DEFAULT '{}'")
            if 'funding' not in columns:db.execute("ALTER TABLE research_operation_intents ADD COLUMN funding TEXT NOT NULL DEFAULT '{}'")

    def install(self):
        """组合原确认事务守卫；不替换指标、权限或业务审核。"""
        original=self.g.bank.base_evidence_guard
        @contextmanager
        def guarded(db,w,op):
            with original(db,w,op),self.operation_guard(db,w,op):yield
        self.g.bank.base_evidence_guard=guarded
        setter=getattr(self.g.public_products,'set_snapshot_consumer',None)
        if setter:setter(self.publish_catalog_snapshot)

    def publish_catalog_snapshot(self,body,**metadata):
        result=self.provider.publish_catalog_snapshot(body,**metadata)
        if result['status'] in ('published','unchanged'):
            try:self._invalidate_public_operations(result['provider_revision'])
            except Exception:result={**result,'operation_invalidation_status':'deferred_to_strict_confirmation_guard'}
        return result

    def _invalidate_public_operations(self,revision):
        with self.g.bank.transaction() as bank:
            rows=bank.execute("SELECT o.id,o.workspace_id FROM operations o JOIN research_operation_intents i ON i.workspace_id=o.workspace_id AND i.idempotency_key=o.idempotency_key WHERE o.state='pending_confirmation' AND i.provider_revision IS NOT NULL AND i.provider_revision<>?",(revision,)).fetchall()
            for row in rows:
                bank.execute("UPDATE operations SET state='invalidated',verification_hash=NULL WHERE id=?",(row['id'],))
                self.g.bank._audit(bank,row['workspace_id'],'research_source_changed',{'provider_revision':revision},row['id'])

    def _invalidate_dependents(self,bank,w,rid):
        intents=bank.execute('SELECT * FROM research_operation_intents WHERE workspace_id=?',(w,)).fetchall()
        for intent in intents:
            if intent['record_id']==rid or rid in json.loads(intent['dependencies']):
                bank.execute("UPDATE operations SET state='invalidated',verification_hash=NULL WHERE workspace_id=? AND idempotency_key=? AND state='pending_confirmation'",(w,intent['idempotency_key']))

    @property
    def provider(self):
        if self._provider is None:
            try:
                from research_evidence import ResearchEvidenceStore
                self._provider=ResearchEvidenceStore()
            except (ImportError,TypeError): fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开研究资料提供器尚未接通。',503)
        return self._provider

    def binding(self, w, db): return self.g.current_registered_binding(w,db)

    def _provider_call(self, method, *args):
        try:return getattr(self.provider,method)(*args)
        except BankError:raise
        except Exception as exc:
            if hasattr(exc,'code') and hasattr(exc,'status'):
                fail(exc.code,str(exc),exc.status)
            raise

    @contextmanager
    def evidence_guard(self,revision):
        provider=self.provider
        if not callable(getattr(provider,'guard',None)):
            fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开资料缺少版本发布守卫。',503)
        try:
            with provider.guard(revision):yield provider
        except BankError:raise
        except Exception as exc:
            if hasattr(exc,'code') and hasattr(exc,'status'):fail(exc.code,str(exc),exc.status)
            raise

    def _revision(self, db, binding):
        row=db.execute('SELECT * FROM research_workspace_versions WHERE workspace_id=?',(binding['workspace_id'],)).fetchone()
        if row and row['registered_user_id']!=binding['registered_user_id']: fail('RESEARCH_OWNER_CHANGED','研究工作区归属无法核对。',403)
        return row['revision'] if row else 0

    @contextmanager
    def transaction(self, w, expected=None):
        applied=[]
        with self.g.bank.transaction() as bank,closing(self.store.connect()) as db:
            binding=self.binding(w,bank);db.execute('BEGIN IMMEDIATE');revision=self._revision(db,binding)
            expected_privacy=self._request_privacy.get()
            if expected_privacy is not None and privacy_revision(bank,w)!=expected_privacy:
                fail('PRIVACY_REVISION_CHANGED','当前资料权限已变化，请重新读取研究。',409)
            if expected is not None and (type(expected) is not int or expected!=revision):
                fail('RESEARCH_VERSION_CONFLICT','研究状态已更新，请重新读取后再提交。',409)
            try:
                yield bank,db,binding,revision
                db.commit()
                if self._failpoint:self._failpoint('after_private_commit')
                applied=self._apply_outbox(bank,db,binding)
            except Exception:
                db.rollback();raise
        # 私库原子提交留pending；银行提交失败或进程中断后，下一受信请求重放。
        # 银行receipt和提醒同事务，提交后才标私库done；重复恢复按event_hash精确去重。
        if applied:
            with closing(self.store.connect()) as db,db:
                db.executemany("UPDATE research_workspace_outbox SET status='done' WHERE id=? AND event_hash=?",applied)
        self.store._permissions()

    def _emit(self,db,binding,event):
        event={**event,'workspace_revision':self._revision(db,binding)}
        eid=uuid.uuid4().hex;raw=packed(event)
        db.execute('INSERT INTO research_workspace_outbox VALUES(?,?,?,?,?,\'pending\',?)',
            (eid,binding['registered_user_id'],binding['workspace_id'],self.store.protect(raw),fingerprint(event),self.g.bank.clock()))

    def _apply_outbox(self,bank,private,binding):
        applied=[];w=binding['workspace_id']
        rows=private.execute("SELECT * FROM research_workspace_outbox WHERE workspace_id=? AND registered_user_id=? AND status='pending' ORDER BY created_at,id",(w,binding['registered_user_id'])).fetchall()
        # 银行业务时钟可固定，不能用同一时间戳下的随机event ID决定保存/撤销顺序。
        rows.sort(key=lambda r:(json.loads(r['value']).get('workspace_revision',json.loads(r['value'])['record_revision']),r['created_at'],r['id']))
        for row in rows:
            event=json.loads(row['value'])
            if fingerprint(event)!=row['event_hash']:fail('RESEARCH_UNREADABLE','研究恢复日志完整性无法核对。',503)
            receipt=bank.execute('SELECT * FROM research_workspace_receipts WHERE id=?',(row['id'],)).fetchone()
            if receipt:
                if receipt['workspace_id']!=w or receipt['event_hash']!=row['event_hash']:fail('RESEARCH_UNREADABLE','研究恢复回执发生冲突。',503)
            else:
                self._apply_reminders(bank,binding,event)
                self.g.bank._audit(bank,w,event['event'],{'record_id':event['record_id'],'kind':event['kind'],'record_revision':event['record_revision']})
                bank.execute('INSERT INTO research_workspace_receipts VALUES(?,?,?,?)',(row['id'],w,row['event_hash'],self.g.bank.clock()))
            applied.append((row['id'],row['event_hash']))
        return applied

    def _apply_reminders(self,bank,binding,event):
        if event['kind']!='task':return
        w=binding['workspace_id'];rid=event['record_id'];reminder=event['reminder'];due=event.get('due_on')
        calendar_id=hashlib.sha256((w+':research-calendar:'+rid).encode()).hexdigest()[:32]
        message_id=hashlib.sha256((w+':research-message:'+rid).encode()).hexdigest()[:32]
        if reminder['enabled'] and reminder['calendar'] and due:
            from calendar_events import normalize
            value=normalize({'title':'研究与保障任务','kind':'life_event','start_on':due,'note':'本人研究任务：'+rid,'remind_days':0})
            previous=bank.execute('SELECT revision,created_at FROM calendar_series WHERE workspace_id=? AND id=?',(w,calendar_id)).fetchone()
            now=self.g.bank.clock()
            bank.execute('INSERT INTO calendar_series VALUES(?,?,?, ?,\'active\',?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,status=excluded.status,fingerprint=excluded.fingerprint,updated_at=excluded.updated_at',
                (calendar_id,w,packed(value),previous['revision']+1 if previous else 1,'research:'+rid,fingerprint(value),previous['created_at'] if previous else now,now))
        else:bank.execute("UPDATE calendar_series SET status='cancelled',revision=revision+1 WHERE workspace_id=? AND id=? AND status!='cancelled'",(w,calendar_id))
        if reminder['enabled'] and reminder['message']:
            now=self.g.bank.clock();source=packed({'kind':'research_task','record_id':rid,'record_revision':event['record_revision']})
            bank.execute('INSERT INTO navren_messages VALUES(?,?,?,?,?,?,?,?,?,NULL,\'pending\',NULL,NULL,NULL,?,NULL,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,due_at=excluded.due_at,status=excluded.status,source=excluded.source',
                (message_id,w,'research:'+rid,'research','research_task','研究与保障任务','本人已确认的研究或保障待办。',now,due,'user_confirmed',source))
        else:bank.execute("UPDATE navren_messages SET status='cancelled' WHERE workspace_id=? AND id=?",(w,message_id))

    def _increment(self, db, binding, revision):
        db.execute('INSERT INTO research_workspace_versions VALUES(?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET revision=excluded.revision',
                   (binding['registered_user_id'],binding['workspace_id'],revision+1))

    @staticmethod
    def _row(db,binding,rid):
        row=db.execute('SELECT * FROM research_workspace_records WHERE id=? AND workspace_id=? AND registered_user_id=? AND deleted=0',
                       (rid,binding['workspace_id'],binding['registered_user_id'])).fetchone()
        if not row: fail('RESEARCH_NOT_FOUND','研究记录不存在或不属于当前工作区。',404)
        return row

    def public_detail(self, entity_id):
        entity_id=ident(entity_id);revision=self._provider_call('revision')
        with self.evidence_guard(revision):detail=self._provider_call('detail',entity_id)
        if (type(detail) is not dict or detail.get('entity_id')!=entity_id or detail.get('provider_revision')!=revision
                or type(detail.get('evidence_version')) is not str or not detail['evidence_version']):
            fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开资料实体或版本无法核对。',503)
        return {'contract':CONTRACT,'data_scope':'public','entity_id':entity_id,'provider_revision':revision,
                'evidence_version':detail['evidence_version'],'entity_version':detail.get('entity_version'), 'entity':deepcopy(detail),
                'read_mode':'published_cache','network_recheck_performed':False}

    def deep_refresh(self,entity_id):
        """仅显式深度检查调用；列表、detail和历史恢复绝不发起外网请求。"""
        eid=ident(entity_id)
        for name in ('refresh_plan','freshness','refresh_entity'):
            if not callable(getattr(self.provider,name,None)):fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开资料未提供有界重核接口。',503)
        self.public_detail(eid)  # 未登记实体在分配singleflight状态前拒绝。
        with self._refresh_mutex:
            lock=self._refresh_locks.setdefault(eid,threading.Lock())
        if not lock.acquire(blocking=False):return {**self.public_detail(eid),'refresh':{'status':'in_progress','network_recheck_performed':False}}
        started=time.monotonic()
        try:
            plan=self._provider_call('refresh_plan',eid);fresh=self._provider_call('freshness',eid)
            with self._refresh_mutex:previous=self._refresh_cache.get(eid)
            if previous and started<previous['retry_after']:
                return {**self.public_detail(eid),'refresh':{**deepcopy(previous['receipt']),'status':'warm_cache',
                    'elapsed_seconds':round(time.monotonic()-started,3),'network_recheck_performed':False},'freshness':fresh}
            check=fresh.get('performance_check') or {};stamp=check.get('checked_at')
            try:age=(datetime.now(TZ)-datetime.fromisoformat(stamp)).total_seconds()
            except (ValueError,TypeError):age=float('inf')
            nav_due=bool(plan.get('performance_supported')) and not 0<=age<plan.get('nav_interval_seconds',3600)
            docs_due=any(row.get('due') for row in plan.get('documents',[]))
            if not nav_due and not docs_due:
                return {**self.public_detail(eid),'refresh':{'status':'not_due','elapsed_seconds':round(time.monotonic()-started,3),
                    'network_recheck_performed':False},'freshness':fresh}
            # 复用gateway原有业绩适配器及其singleflight/缓存；不建第二worker。
            with self.g._product_performance_lock:
                if self.g.product_performance is None:
                    from product_performance import ProductPerformance
                    self.g.product_performance=ProductPerformance()
                adapter=self.g.product_performance
            try:
                receipt=self.provider.refresh_entity(eid,performance_provider=adapter,check_documents=docs_due,max_documents=1,
                    force=nav_due,expected_revision=plan['provider_revision'])
                receipt={**receipt,'elapsed_seconds':round(time.monotonic()-started,3),'network_recheck_performed':True,
                    'budget':'existing NAV<=11s + originals<=12s, one original per explicit check; no per-list network'}
                failed=receipt.get('performance_status') in ('failed','unavailable','parse_or_identity_failed','nav_date_regressed') or any(r.get('status') in ('failed','unavailable') for r in receipt.get('checks',[]))
                receipt['source_check_status']='failed' if failed else 'checked'
                if failed:receipt['notice']='来源检查失败或部分不可用；原值及原披露日期保留，未视为当前可交易报价。'
                if receipt.get('provider_revision'):
                    try:self._invalidate_public_operations(receipt['provider_revision'])
                    except Exception:receipt['operation_invalidation_status']='deferred_to_strict_confirmation_guard'
                retry=60 if failed else 300
            except Exception as exc:
                receipt={'status':'failed','error':getattr(exc,'code',type(exc).__name__),
                    'elapsed_seconds':round(time.monotonic()-started,3),'network_recheck_performed':True,
                    'notice':'本次重核失败，保留已核发布的原值、披露日及来源状态；未将旧值升级为当前报价。'}
                retry=60
            with self._refresh_mutex:self._refresh_cache[eid]={'retry_after':time.monotonic()+retry,'receipt':deepcopy(receipt)}
            return {**self.public_detail(eid),'refresh':receipt,'freshness':self._provider_call('freshness',eid)}
        finally:lock.release()

    def _evidence(self, entity_ids):
        if type(entity_ids) is not list or len(entity_ids)>12:fail()
        for e in entity_ids:ident(e)
        if len(set(entity_ids))!=len(entity_ids):fail()
        if not entity_ids:return {}
        revision=self._provider_call('revision')
        with self.evidence_guard(revision):return {e:self.public_detail(e) for e in entity_ids}

    def _source_state(self, payload, kind):
        if kind=='policy': return 'historical_contract'
        for eid,old in payload.get('evidence',{}).items():
            try:
                if self.public_detail(eid)['evidence_version']!=old['evidence_version']: return 'changed'
            except BankError:return 'unavailable'
        return 'current'

    def _view(self, row, *, evaluate=True):
        try: payload=json.loads(row['value'])
        except (ValueError,TypeError):fail('RESEARCH_UNREADABLE','研究记录无法读取。',503)
        links=deepcopy(payload.get('operation_links',[]))
        for link in links:
            op=self.g.bank.operation(row['workspace_id'],link['operation_id'])
            link.update(state=op['state'],receipt=op.get('result'),permission=op['permission'])
        return {'id':row['id'],'kind':row['kind'],'record_revision':row['record_revision'],'status':row['status'],
                'value':payload['value'],'evidence_versions':{e:v['evidence_version'] for e,v in payload.get('evidence',{}).items()},
                'evidence':payload.get('evidence',{}),'source_state':self._source_state(payload,row['kind']) if evaluate else 'not_checked',
                'created_at':datetime.fromtimestamp(row['created_at'],TZ).isoformat(timespec='seconds'),
                'updated_at':datetime.fromtimestamp(row['updated_at'],TZ).isoformat(timespec='seconds'),
                'operation_links':links,'links':payload.get('links',{}),'execution_authority':False,
                'execution':{'status':links[-1]['state'] if links else 'not_started','public_share_executed':False}}

    def read(self,w,*,kind=None,rid=None,history=False,limit=100,offset=0):
        if type(limit) is not int or not 1<=limit<=250 or type(offset) is not int or not 0<=offset<=100000:fail()
        if kind is not None and kind not in KINDS:fail()
        with self.transaction(w) as (_,db,binding,revision):
            result={'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision,'execution_authority':False}
            if rid:
                row=self._row(db,binding,ident(rid))
                if history:
                    result['items']=[json.loads(r['value']) for r in db.execute('SELECT value FROM research_workspace_history WHERE workspace_id=? AND registered_user_id=? AND record_id=? ORDER BY record_revision',
                        (w,binding['registered_user_id'],rid))]
                else:result['record']=self._view(row)
            else:
                rows=db.execute('SELECT * FROM research_workspace_records WHERE workspace_id=? AND registered_user_id=? AND deleted=0'+(' AND kind=?' if kind else '')+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',
                                (w,binding['registered_user_id'],kind,limit,offset) if kind else (w,binding['registered_user_id'],limit,offset)).fetchall()
                result['items' if kind else 'records']=[self._view(r) for r in rows]
                result['total']=db.execute('SELECT count(*) FROM research_workspace_records WHERE workspace_id=? AND registered_user_id=? AND deleted=0'+(' AND kind=?' if kind else ''),
                    (w,binding['registered_user_id'],kind) if kind else (w,binding['registered_user_id'])).fetchone()[0]
                result['has_more']=result['total']>offset+len(rows)
                result.update(limit=limit,offset=offset)
                if kind=='policy':
                    existing=self.g.financial_query(w,'list_insurance',{})
                    result['existing_policy_records']={'source_scope':'navren_synthetic','dataset_version':existing['dataset_version'],
                        'as_of':existing['as_of'],'items':existing['data']['items'],
                        'notice':'当前体验角色既有保障记录，尚不代表完整合同；研究补充档案单独保存。'}
                    result['research_enrichments']=result['items']
                if not kind:
                    result['attachments']=[self._attachment_view(r) for r in db.execute('SELECT * FROM research_workspace_attachments WHERE workspace_id=? AND registered_user_id=? AND deleted=0 ORDER BY created_at,id',(w,binding['registered_user_id']))]
                    result['capabilities']={'public_provider_loaded':self._provider is not None,'private_persistence':True,'prepare_requires_verified_mapping':True}
            return result

    def _conditions(self, value):
        fields(value,CONDITIONS);bounded(value)
        if 'amount' in value:money(value['amount'])
        if 'currency' in value and value['currency'] not in ('CNY','USD','EUR','GBP','JPY'):fail()
        if 'holding_days' in value and (type(value['holding_days']) is not int or not 1<=value['holding_days']<=36500):fail()
        if 'target_date' in value:day(value['target_date'])
        if 'allowed_disclosure' in value and type(value['allowed_disclosure']) is not bool:fail()
        if 'obligation_ids' in value and (type(value['obligation_ids']) is not list or len(value['obligation_ids'])>50):fail()
        for k in ('account_id','risk_confirmation_id'):
            if k in value:ident(value[k])
        if 'obligation_ids' in value:
            for oid in value['obligation_ids']:ident(oid)
            if len(set(value['obligation_ids']))!=len(value['obligation_ids']):fail()
        return deepcopy(value)

    def _validate(self, kind, value):
        bounded(value)
        if kind=='research':
            fields(value,{'name','goal','conditions','entity_ids','deadline','notes'},{'name','goal','conditions','entity_ids'})
            text(value['name'],120);text(value['goal'],2000);self._conditions(value['conditions']);day(value.get('deadline'),True)
            text(value.get('notes',''),4000,True)
        elif kind=='policy':
            fields(value,{'label','entity_id','contract_version','insured_object','start_on','end_on','premium','coverages','attachment_ids','notes','consent','existing_policy_id','existing_policy_version'},
                   {'label','entity_id','contract_version','insured_object','start_on','end_on','premium','coverages','attachment_ids','consent'})
            text(value['label'],120);text(value['contract_version'],160);day(value['start_on']);day(value['end_on'])
            if value['end_on']<value['start_on']:fail()
            fields(value['insured_object'],{'kind','id','label','relationship'},{'kind','id','label'})
            if value['insured_object']['kind'] not in ('person','house','vehicle','equipment','other'):fail()
            text(value['insured_object']['id']);text(value['insured_object']['label'],120)
            if 'relationship' in value['insured_object']:text(value['insured_object']['relationship'],120)
            p=value['premium'];fields(p,{'amount','currency','frequency','next_due_on'},{'amount','currency','frequency','next_due_on'});money(p['amount']);day(p['next_due_on'],True)
            if p['currency'] not in ('CNY','USD','EUR','GBP','JPY') or p['frequency'] not in ('monthly','quarterly','yearly','once'):fail()
            if type(value['coverages']) is not list or type(value['attachment_ids']) is not list:fail()
            c=value['consent'];fields(c,{'purpose','health_data_authorized'},{'purpose','health_data_authorized'});text(c['purpose'],200)
            if type(c['health_data_authorized']) is not bool:fail()
            if value['coverages'] and c['health_data_authorized'] is not True:fail('CONFIRMATION_REQUIRED','保存保障及健康责任细节需要本人明确授权。',409)
            if ('existing_policy_id' in value)!=('existing_policy_version' in value):fail()
        elif kind=='task':
            fields(value,{'title','kind','record_ids','due_on','status','conditions','notes','reminder'},{'title','kind','record_ids','due_on','status','conditions','reminder'})
            text(value['title'],120);day(value['due_on'],True);self._conditions(value['conditions'])
            if value['kind'] not in ('research','renewal','claim_materials','premium','reserve','investment') or value['status'] not in STAGES:fail()
            if value['status'] in ('prepared','verifying','executing','succeeded','unknown'):fail(message='执行状态只能按正式操作回执推进。')
            if type(value['record_ids']) is not list or len(value['record_ids'])>30:fail()
            fields(value['reminder'],{'enabled','calendar','message'},{'enabled','calendar','message'})
            if any(type(v) is not bool for v in value['reminder'].values()):fail()
            for rid in value['record_ids']:ident(rid)
            if len(value['record_ids'])!=len(set(value['record_ids'])):fail()
            text(value.get('notes',''),4000,True)
        else:fail()
        clean=deepcopy(value)
        if kind=='task' and clean['status']=='cancelled':clean['reminder']={'enabled':False,'calendar':False,'message':False}
        return clean

    def save(self,w,payload,*,kind='research'):
        fields(payload,{'expected_revision','id','value','confirmed','kind'},{'expected_revision','value','confirmed'})
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','保存本人研究资料需要明确确认。',409)
        if 'kind' in payload and payload['kind']!=kind:fail()
        clean=self._validate(kind,payload['value']);ids=clean.get('entity_ids',[]) if kind=='research' else [clean['entity_id']] if kind=='policy' and clean['entity_id'] is not None else []
        evidence=self._evidence(ids)
        return self._persist(w,payload,kind,clean,evidence)

    def _persist(self,w,payload,kind,clean,evidence):
        rid=ident(payload['id']) if 'id' in payload else uuid.uuid4().hex;now=self.g.bank.clock()
        with self.transaction(w,payload['expected_revision']) as (bank,db,binding,revision):
            old=self._row(db,binding,rid) if 'id' in payload else None
            if old and old['kind']!=kind:fail()
            if clean.get('source_scope',{}).get('bank_version') is not None:
                if self.g.bank._workspace(bank,w)['version']!=clean['source_scope']['bank_version']:fail('RESEARCH_VERSION_CONFLICT','测算依赖账本已变化。',409)
                if clean['source_scope'].get('bank_protection_signature') and self._bank_funding_signature(bank,w,clean['source_scope']['account_id'])!=clean['source_scope']['bank_protection_signature']:
                    fail('RESEARCH_VERSION_CONFLICT','银行已有保护在报价提交前变化。',409)
            if kind!='policy' and clean.get('method')!='check_coverage_scenario':
                for captured in evidence.values():
                    with self.evidence_guard(captured['provider_revision']):pass
            if kind=='task':
                evidence={}
                for record in clean['record_ids']:
                    related=self._row(db,binding,ident(record))
                    if related['kind']=='task':fail('RESEARCH_INVALID','任务只关联研究、比较或保单，不能递归引用任务。',422)
                    evidence.update(json.loads(related['value']).get('evidence',{}))
            if kind=='policy':
                for attachment in clean['attachment_ids']:self._attachment_row(db,binding,ident(attachment))
                if clean['attachment_ids'] and clean['consent']['health_data_authorized'] is not True:fail('CONFIRMATION_REQUIRED','关联原件需要本人明确授权。',409)
                if 'existing_policy_id' in clean:
                    existing=self.g.financial_query(w,'list_insurance',{})
                    if str(existing['dataset_version'])!=str(clean['existing_policy_version']):fail('RESEARCH_VERSION_CONFLICT','既有保障资料版本已变化。',409)
                    if not any(r.get('policy_id',r.get('id'))==clean['existing_policy_id'] for r in existing['data']['items']):
                        fail('RESEARCH_NOT_FOUND','既有保单不属于当前体验角色。',404)
                if old:
                    original=json.loads(old['value'])
                    if original['value']['contract_version']==clean['contract_version']:
                        if original['value']['entity_id']!=clean['entity_id']:fail('RESEARCH_CONTRACT_CONFLICT','同一合同版本不能改成另一保险实体。',409)
                        evidence=original['evidence']  # 同一旧保单合同不因公开provider更新而重写。
            record_revision=old['record_revision']+1 if old else 1
            package={'value':clean,'evidence':evidence,'operation_links':[], 'links':{}}
            if old:
                self._invalidate_dependents(bank,w,rid)
                previous=self._view(old,evaluate=False)
                db.execute('INSERT INTO research_workspace_history VALUES(?,?,?,?,?,?)',(rid,binding['registered_user_id'],w,old['record_revision'],self.store.protect(packed(previous)),now))
                prior=json.loads(old['value']);package['operation_links']=prior.get('operation_links',[]);package['links']=prior.get('links',{})
            if kind=='task':
                package['links']={'calendar_id':hashlib.sha256((w+':research-calendar:'+rid).encode()).hexdigest()[:32] if clean['reminder']['enabled'] and clean['reminder']['calendar'] and clean['due_on'] else None,
                                  'message_id':hashlib.sha256((w+':research-message:'+rid).encode()).hexdigest()[:32] if clean['reminder']['enabled'] and clean['reminder']['message'] else None}
            status=clean.get('status','recorded' if kind=='policy' else 'evidence' if evidence else 'clarifying')
            db.execute('INSERT INTO research_workspace_records VALUES(?,?,?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET record_revision=excluded.record_revision,status=excluded.status,value=excluded.value,updated_at=excluded.updated_at',
                (rid,binding['registered_user_id'],w,kind,record_revision,status,self.store.protect(packed(package)),old['created_at'] if old else now,now))
            self._increment(db,binding,revision)
            self._emit(db,binding,{'event':'research_record_saved','record_id':rid,'kind':kind,'record_revision':record_revision,
                **({'reminder':clean['reminder'],'due_on':clean['due_on']} if kind=='task' else {})})
            result={'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision+1,'record':self._view(self._row(db,binding,rid))}
        return result

    @staticmethod
    def _trusted_fact(evidence,field,*,currency,channel=None):
        entity=evidence['entity'];eid=evidence['entity_id'];facts=entity.get('facts',[])
        if type(facts) is not list:fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开事实列表无法核对。',503)
        candidates=[f for f in facts if isinstance(f,dict) and f.get('field')==field and (channel is None or f.get('channel')==channel)]
        if not candidates:
            return {'entity_id':eid,'field':field,'value':None,'state':'missing','missing_reason':'未取得对应实体、渠道的确定性字段。',
                    'currency':currency,**({'channel':channel} if channel is not None else {})}
        if len(candidates)!=1:fail('RESEARCH_EVIDENCE_CHANGED','公开字段存在多个未消解版本。',409)
        fact=deepcopy(candidates[0]);source=fact.get('source_id');version=fact.get('source_version')
        if fact.get('entity_id')!=eid:fail('RESEARCH_DEPENDENCY_UNAVAILABLE','字段实体与提供器返回身份不一致。',503)
        if fact.get('state')=='verified':
            if (not isinstance(source,str) or not version or entity.get('source_versions',{}).get(source)!=version
                    or fact.get('currency')!=currency or not fact.get('locator')):
                fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开计算字段的来源版本、定位或币种不完整。',503)
        return fact

    def compare(self,w,payload):
        required={'expected_revision','entity_ids','amount','currency','holding_days','channel','evidence_versions','confirmed'}
        fields(payload,required|{'id','target_date','valuation_basis'},required)
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','保存比较需要本人确认。',409)
        money(payload['amount'])
        ids=payload['entity_ids']
        if type(ids) is not list or not 2<=len(ids)<=3:fail()
        if type(payload['holding_days']) is not int or not 1<=payload['holding_days']<=36500:fail()
        if payload['currency'] not in ('CNY','USD','EUR','GBP','JPY'):fail()
        for eid in ids:ident(eid)
        text(payload['channel'],80);fields(payload['evidence_versions'],ids,ids)
        if 'target_date' in payload:day(payload['target_date'])
        mode=payload.get('valuation_basis','constant_gross_value')
        if mode not in ('constant_gross_value','observed_net_nav'):fail()
        provider_revision=self._provider_call('revision')
        with self.evidence_guard(provider_revision):
            evidence=self._evidence(ids);shares=[];versions={}
            for eid,item in evidence.items():
                if payload['evidence_versions'][eid]!=item['evidence_version']:fail('RESEARCH_EVIDENCE_CHANGED','比较输入的实体资料已变化。',409)
                entity=item['entity']
                if entity.get('quarantine'):
                    fail('RESEARCH_ENTITY_QUARANTINED','该实体的身份或资料存在待核冲突，不能进入比较。',409)
                versions.update(entity.get('source_versions',{}))
                fees=self._trusted_fact(item,'fees',currency=payload['currency'],channel=payload['channel'])
                share={'entity_id':eid,'fees':fees}
                if mode=='observed_net_nav':share['net_nav_value']=self._trusted_fact(item,'net_nav_value',currency=payload['currency'],channel=payload['channel'])
                shares.append(share)
            from research_methods import compare_share_costs
            inputs={'as_of':datetime.now(TZ).date().isoformat(),'currency':payload['currency'],'evidence_versions':versions,
                    'amount':payload['amount'],'holding_days':payload['holding_days'],'channel':payload['channel'],'valuation_basis':mode,'shares':shares}
            result=compare_share_costs(inputs)
            value={'entity_ids':ids,'amount':payload['amount'],'currency':payload['currency'],'holding_days':payload['holding_days'],
                   'channel':payload['channel'],'valuation_basis':mode,'target_date':payload.get('target_date'),
                   'inputs':inputs,'result':result,'provider_revision':provider_revision,'algorithm_version':result['algorithm_version'],
                   'status':'ready' if result['status']=='complete' else 'needs_input',
                   'scenario_notice':'固定参考本金下的费用影响情景，不预测未来收益；管理人标准费率不等于微众实际费率。' if mode=='constant_gross_value' else '按已核实净值金额口径核对费用，不重复扣除净值已反映的运作费。'}
        return self._persist(w,payload,'comparison',value,evidence)

    def _attachment_row(self,db,binding,aid):
        row=db.execute('SELECT * FROM research_workspace_attachments WHERE id=? AND workspace_id=? AND registered_user_id=? AND deleted=0',(aid,binding['workspace_id'],binding['registered_user_id'])).fetchone()
        if row is None:fail('RESEARCH_NOT_FOUND','资料原件不存在或不属于当前工作区。',404)
        return row

    def _bank_funding_signature(self,bank,w,account_id):
        """只读原保护输入；确认事务不能另开overview或依赖未变化的版本号猜测。"""
        values={'as_of':self.g.bank._workspace(bank,w)['as_of']}
        queries={
            'accounts':('SELECT * FROM accounts WHERE workspace_id=? ORDER BY id',(w,)),
            'profiles':('SELECT * FROM profiles WHERE workspace_id=?',(w,)),
            'mandates':('SELECT * FROM mandates WHERE workspace_id=? AND account_id=? ORDER BY id',(w,account_id)),
            'subscriptions':('SELECT * FROM subscriptions WHERE workspace_id=? AND account_id=? ORDER BY id',(w,account_id)),
            'scheduled_payments':('SELECT * FROM scheduled_payments WHERE workspace_id=? AND account_id=? ORDER BY id',(w,account_id)),
            'fund_holds':('SELECT * FROM fund_holds WHERE workspace_id=? AND account_id=? ORDER BY id',(w,account_id)),
            'remittances':('SELECT * FROM navren_remittances WHERE workspace_id=? AND account_id=? ORDER BY operation_id',(w,account_id)),
            'loans':('SELECT i.* FROM loan_installments i JOIN loans l ON l.id=i.loan_id AND l.workspace_id=i.workspace_id WHERE i.workspace_id=? AND l.repayment_account_id=? ORDER BY i.id',(w,account_id)),
            'credit':('SELECT s.* FROM credit_statements s JOIN cards c ON c.id=s.card_id AND c.workspace_id=s.workspace_id WHERE s.workspace_id=? AND c.account_id=? ORDER BY s.id',(w,account_id))}
        for key,(sql,args) in queries.items():values[key]=[dict(r) for r in bank.execute(sql,args)]
        return fingerprint(values)

    def _policy_inventory(self,bank,private,binding,account_id,selected,bank_commitments):
        """以确切保单/合同/账户映射去重；同名、同额、同日都不是关联证明。"""
        w=binding['workspace_id'];private_events={};aliases={};refs=[];linkage=[]
        for rid in selected:
            row=self._row(private,binding,ident(rid))
            if row['kind']!='policy':fail()
            value=json.loads(row['value'])['value'];premium=value['premium'];due=premium['next_due_on']
            if premium['currency']!='CNY' or not due:fail('RESEARCH_INVALID','所选保单未提供人民币下期缴费日。',422)
            oid='policy:'+rid;canonical=oid
            refs.append({'record_id':rid,'record_revision':row['record_revision']})
            if value.get('existing_policy_id'):
                original=self.g.financial_query(w,'get_policy',{'policy_id':value['existing_policy_id']})
                if str(original['dataset_version'])!=str(value['existing_policy_version']):fail('RESEARCH_VERSION_CONFLICT','原保单来源版本已变化，须重核缴费关系。',409)
                policy=original['data']['policy'];source_account=policy['payment_account_id']
                try:runtime_account=self.g.demo_accounts.map_account_id(w,source_account)
                except BankError as error:
                    if error.code!='DEMO_ACCOUNT_MAPPING_NOT_FOUND':raise
                    connected=bank.execute('SELECT account_id FROM connected_account_links WHERE workspace_id=? AND source_account_id=?',(w,source_account)).fetchall()
                    if len(connected)!=1:fail('RESEARCH_POLICY_ACCOUNT_MISMATCH','原保单付款账户尚无唯一受信运行映射。',409)
                    runtime_account=connected[0]['account_id']
                if runtime_account!=account_id:fail('RESEARCH_POLICY_ACCOUNT_MISMATCH','补充保单的付款账户不属于本次报价账户。',409)
                source=self.g.demo_accounts.active_history(w) or self.g.financial_data
                subject=self.g.demo_accounts.subject_for(w)
                with closing(source._connect()) as db:
                    mids={r['mandate_id'] for r in db.execute('SELECT mandate_id FROM mandates WHERE subject_id=? AND contract_id=? AND account_id=?',
                        (subject,policy['contract_id'],source_account))}
                declared=policy.get('details',{})
                if declared.get('bank_mandate_id'):mids.add(declared['bank_mandate_id'])
                matches=[]
                for commitment in bank_commitments:
                    same=commitment['kind']=='mandate' and commitment['evidence_id'] in mids
                    if commitment['kind']=='subscription':
                        sub=bank.execute('SELECT mandate_id,details FROM subscriptions WHERE workspace_id=? AND id=?',(w,commitment['evidence_id'])).fetchone()
                        meta=json.loads(sub['details']) if sub else {}
                        same=bool(sub and (sub['mandate_id'] in mids or (meta.get('policy_id')==value['existing_policy_id']
                            and str(meta.get('policy_dataset_version'))==str(value['existing_policy_version']))))
                    if declared.get('bank_commitment_id')==commitment['kind']+':'+commitment['evidence_id']:same=True
                    if same and commitment['due_on']==due:
                        if commitment['account_id']!=runtime_account or commitment['amount_minor']!=money(premium['amount']):
                            fail('RESEARCH_POLICY_BILL_CONFLICT','明确关联的原保费账单与补充金额或账户冲突，须重核。',409)
                        matches.append(commitment)
                if len(matches)>1:fail('RESEARCH_POLICY_BILL_CONFLICT','同一保单期次关联多个原账单，不能静默去重。',409)
                if matches:
                    c=matches[0];canonical=c['kind']+':'+c['evidence_id']
                    linkage.append({'record_id':rid,'existing_policy_id':value['existing_policy_id'],'dataset_version':original['dataset_version'],
                        'contract_id':policy['contract_id'],'account_id':runtime_account,'due_on':due,'canonical_obligation_id':canonical,
                        'basis':'same source policy/contract/mandate identity, version, payment account and due date; no amount-only match'})
                else:canonical='existing-policy:'+fingerprint([value['existing_policy_id'],value['existing_policy_version'],runtime_account,due])[:32]
            aliases[oid]=canonical
            if canonical not in {c['kind']+':'+c['evidence_id'] for c in bank_commitments}:
                event={'obligation_id':canonical,'amount':premium['amount'],'due_date':due,'status':'confirmed'}
                if canonical in private_events and private_events[canonical]!=event:fail('RESEARCH_POLICY_BILL_CONFLICT','同一期原保单的补充档案存在冲突。',409)
                private_events[canonical]=event
        return list(private_events.values()),aliases,refs,linkage

    def _funding_hold_links(self,bank,private,binding,policies,aliases):
        """只从同工作区成功的原operation/hold及服务端保存意图读关系。"""
        selected={r['record_id']:r for r in policies};links={};details=[]
        selected_values={rid:json.loads(self._row(private,binding,rid)['value'])['value'] for rid in selected}
        rows=bank.execute("SELECT i.*,o.id AS operation_id,o.payload AS operation_payload,o.result AS operation_result FROM research_operation_intents i JOIN operations o ON o.workspace_id=i.workspace_id AND o.idempotency_key=i.idempotency_key WHERE i.workspace_id=? AND o.state='succeeded' AND o.action='reserve_funds'",(binding['workspace_id'],)).fetchall()
        for row in rows:
            funding=json.loads(row['funding']);allocations=funding.get('allocations') or ([funding['allocation']] if funding.get('allocation') else [])
            if not allocations:continue
            payload=json.loads(row['operation_payload']);receipt=json.loads(row['operation_result'])
            hold=bank.execute("SELECT * FROM fund_holds WHERE workspace_id=? AND id=? AND status='held'",(binding['workspace_id'],receipt.get('hold_id'))).fetchone()
            if not hold:continue
            if (fingerprint(payload)!=row['core_digest'] or hold['account_id']!=payload['account_id'] or hold['task_id']!=payload['task_id']
                    or hold['amount_minor']!=sum(a['amount_minor'] for a in allocations)
                    or len({a['canonical_obligation_id'] for a in allocations})!=len(allocations)):
                fail('RESEARCH_FUNDING_LINK_CONFLICT','资金预留回执或分配完整性无法核对。',409)
            links[hold['id']]=[]
            for allocation in allocations:
                policy=self._row(private,binding,allocation['policy_record_id']);value=json.loads(policy['value'])['value']
                current=aliases.get('policy:'+policy['id'])
                if current is None and value.get('existing_policy_id'):
                    equivalent=[rid for rid,v in selected_values.items() if (v.get('existing_policy_id'),v.get('existing_policy_version'),v['premium']['next_due_on'])
                        ==(value['existing_policy_id'],value.get('existing_policy_version'),value['premium']['next_due_on'])]
                    if equivalent:current=aliases['policy:'+equivalent[0]]
                if current is None:continue
                if (policy['record_revision']!=allocation['policy_record_revision'] or value['premium']['next_due_on']!=allocation['due_on']
                        or value['contract_version']!=allocation['contract_version']):
                    fail('RESEARCH_VERSION_CONFLICT','已有预留绑定的保单或缴费期已变化，须先重核该分配。',409)
                if current!=allocation['canonical_obligation_id']:
                    fail('RESEARCH_FUNDING_LINK_CONFLICT','同一缴费的银行保护关系已变化，须先核对已有hold。',409)
                self._assert_record_inputs(private,binding,policy)
                links[hold['id']].append((current,allocation['amount_minor']))
                details.append({'hold_id':hold['id'],'operation_id':row['operation_id'],**allocation})
        return links,details

    def _reserve_inventory(self,bank,private,binding,state,budget,account,selected,until,signature):
        from dataclasses import asdict
        from financial_methods import MetricContext
        from financial_methods.funding import CashSnapshot,FundingEvent,Hold
        w=binding['workspace_id'];as_of=state['as_of'];commitments=budget['commitments']
        private_events,aliases,refs,policy_links=self._policy_inventory(bank,private,binding,account['id'],selected,commitments)
        bank_events=[{'obligation_id':r['kind']+':'+r['evidence_id'],'amount':format(Decimal(r['amount_minor'])/100,'.2f'),
            'due_date':r['due_on'],'status':'confirmed'} for r in commitments]
        obligations=bank_events+private_events;by_id={e['obligation_id']:e for e in obligations}
        hold_links,allocations=self._funding_hold_links(bank,private,binding,refs,aliases);reservations=[]
        for hold in state['fund_holds']:
            if hold['account_id']!=account['id']:continue
            assigned=hold_links.get(hold['id'],[])
            # 原银行承诺已被budget扣除，不允许用额外hold再次替代同一承诺保护。
            if any(oid in {e['obligation_id'] for e in bank_events} for oid,_ in assigned):fail('RESEARCH_FUNDING_LINK_CONFLICT','私人预留的义务现在已进入银行承诺，须先重核重复保护。',409)
            # 纯义务核算每行只接受一个义务，按受信分配拆成计算行；真实hold仍只有一笔。
            status='held' if hold['status']=='held' else 'released';remainder=hold['amount_minor']
            for oid,part in assigned:
                reservations.append({'reservation_id':hold['id']+':'+oid,'obligation_id':oid if oid in by_id else None,
                    'amount':format(Decimal(part)/100,'.2f'),'status':status})
                remainder-=part
            if remainder:
                reservations.append({'reservation_id':hold['id'],'obligation_id':None,
                    'amount':format(Decimal(remainder)/100,'.2f'),'status':status})
        for remittance in state['remittances']:
            if remittance['account_id']==account['id'] and remittance['status']=='scheduled':
                reservations.append({'reservation_id':'remittance:'+remittance['operation_id'],'obligation_id':None,
                    'amount':format(Decimal(remittance['amount_minor'])/100,'.2f'),'status':'held'})
        if sum(money(h['amount']) for h in reservations if h['status']=='held')!=budget['held_minor']:
            fail('RESEARCH_FUNDING_LINK_CONFLICT','原银行保护中的实际占用与明细不一致，停止报价。',409)
        sid='private-bank:'+w;version=fingerprint({'bank_signature':signature,'budget':budget,'obligations':obligations,
            'holds':reservations,'policy_refs':refs,'aliases':aliases,'allocation_sources':allocations})
        versions={sid:version}
        def fact(value):return {'value':value,'state':'verified','source_id':sid,'source_version':version,'currency':'CNY','effective_from':as_of,'effective_to':as_of}
        inputs={'as_of':as_of,'currency':'CNY','evidence_versions':versions,'until':until,'cash_basis':'ledger_cash_before_reservations',
            'available_cash':fact(format(Decimal(account['balance_minor'])/100,'.2f')),
            'obligations':fact({'complete':True,'items':obligations}),'reservations':fact({'complete':True,'items':reservations})}
        # 冻结S10模型使用原银行30日窗口；过去未付义务按既有cashflow_timeline的
        # max(due,as_of)投影，另保留原到期日，不把旧账改成新到期。
        end=state['commitment_window_end'];events=[];date_lineage=[]
        active=bank_events+[e for e in private_events if e['due_date']<=until]
        for event in active:
            on=max(as_of,event['due_date'])
            events.append(FundingEvent(event['obligation_id'],w,'CNY',on,money(event['amount']),'outflow','confirmed',sid,version))
            date_lineage.append({'obligation_id':event['obligation_id'],'scheduled_date':event['due_date'],'projection_date':on,
                'basis':'cashflow_timeline._event max(due,as_of); original date retained'})
        active_ids={e.event_id for e in events};holds=[]
        for hold in state['fund_holds']:
            if hold['account_id']!=account['id'] or hold['status']!='held':continue
            parts=tuple((oid,part) for oid,part in hold_links.get(hold['id'],[]) if oid in active_ids)
            holds.append(Hold(hold['id'],w,'CNY',hold['amount_minor'],sid,version,
                commitment_allocations=parts,release_on_payment=bool(parts)))
        for r in state['remittances']:
            if r['account_id']==account['id'] and r['status']=='scheduled':
                holds.append(Hold('remittance:'+r['operation_id'],w,'CNY',r['amount_minor'],sid,version))
        snapshot=CashSnapshot(w,'CNY',as_of,account['balance_minor'],0,budget['reserve_minor'],as_of,end,sid,version)
        context=MetricContext(w,'CNY','navren_research_selected_obligations',period_start=as_of,period_end=end,
            input_refs=(sid,),source_versions=((sid,version),),coverage_complete=True)
        funding={'context':context,'snapshot':snapshot,'events':events,'holds':holds}
        scope={'account_id':account['id'],'selected_policy_records':refs,'bank_version':state['version'],'bank_protection_signature':signature,
            'bank_budget':budget,'target_obligation_window_end':until,'bank_protection_window_end':end,
            'policy_obligation_aliases':aliases,'existing_policy_bill_links':policy_links,'actual_hold_allocations':allocations,
            'inventory_scope':'原银行30日保护保持；另计本人选定私人保单在目标窗口内的义务。底线不是bank hold。'}
        serialized={'snapshot':asdict(snapshot),'events':[asdict(e) for e in events],'holds':[asdict(h) for h in holds],
            'source_versions':versions,'projection_date_lineage':date_lineage,'purpose':'current protection reconciliation, not a guarantee of settlement dates'}
        return inputs,scope,funding,serialized,{e['obligation_id'] for e in bank_events}

    @staticmethod
    def _missing_calculation_fact(fact,reason):
        return {**deepcopy(fact),'value':None,'state':'missing','missing_reason':reason}

    @staticmethod
    def _derived_fact(fact,value,versions,private_binding):
        """公开规则和本人情景的本地组合身份；不冒称私人计数来自公开PDF。"""
        version=fingerprint({'public_fact':fact,'private_binding':private_binding,'value':value})
        sid='research-derived:'+version[:24];versions[sid]=version
        return {**deepcopy(fact),'value':value,'source_id':sid,'source_version':version,
            'source_scope':'public_rules_plus_user_confirmed_private_scenario',
            'locator':{'kind':'local_composite','public_source_id':fact['source_id'],'public_source_version':fact['source_version'],
                'public_locator':fact['locator'],'private_binding':private_binding},
            'private_assumptions_state':'user_confirmed_not_insurer_verified'}

    def _coverage_template_terms(self,item,fact,policy,context,versions,scenario):
        """消费RAG现有源绑定模板；只开放它明确声明的私人空位，不接收任意规则。"""
        if fact.get('requires_private_binding') is not True:return fact
        if context is None:return self._missing_calculation_fact(fact,'公开模板需要本人保单、年度使用情况和支持分支的明确绑定。')
        fields(context,{'template_id','bindings','attestations','scenario_facts'},{'template_id','bindings','attestations','scenario_facts'})
        if context['template_id']!=fact.get('template_id'):fail('RESEARCH_INVALID','情景与公开模板不一致。',422)
        for sid,version in fact.get('dependency_versions',{}).items():
            if item['entity'].get('source_versions',{}).get(sid)!=version:fail('RESEARCH_EVIDENCE_CHANGED','公开模板依赖原件版本无法核对。',409)
        constraints={r['id'] for r in fact.get('supported_scenario_constraints',[])}
        fields(context['attestations'],constraints,constraints)
        if any(v is not True for v in context['attestations'].values()):
            return self._missing_calculation_fact(fact,'模板支持条件尚未由本人确认，不以模型推断替代。')
        if type(context['bindings']) is not list or len(context['bindings'])>100:fail()
        supplied={}
        for binding in context['bindings']:
            fields(binding,{'pointer','value'},{'pointer','value'});pointer=text(binding['pointer'],160)
            if pointer in supplied:fail()
            supplied[pointer]=binding['value']
        terms=deepcopy(fact['value']);private=json.loads(policy['value'])['value']
        declared={r['pointer']:r for r in fact.get('private_bindings',[]) if r['scope']=='terms_value'}
        auto={'/policy_start':private['start_on'],'/policy_end':private['end_on']}
        if set(supplied)!=set(declared)-set(auto):
            return self._missing_calculation_fact(fact,'私人绑定须恰好覆盖公开模板声明的全部空位；不能添加费率等非私人规则。')
        for pointer,rule in declared.items():
            value=auto[pointer] if pointer in auto else supplied[pointer]
            if value is None:return self._missing_calculation_fact(fact,'私人保单或使用情况仍有未知字段，未填成零或无限。')
            kind=rule['expected_type']
            if kind=='ISO date':day(value)
            elif kind=='boolean':
                if type(value) is not bool:fail()
            elif kind=='money string':money(value)
            elif kind=='money string or unlimited':
                if value!='unlimited':money(value)
            elif kind=='array of known scenario cost IDs':
                if type(value) is not list or len(value)>100:fail()
                for cid in value:ident(cid)
            else:fail('RESEARCH_DEPENDENCY_UNAVAILABLE','尚未支持此私人绑定类型。',503)
            parts=pointer.lstrip('/').split('/');target=terms
            for part in parts[:-1]:target=target[int(part)] if isinstance(target,list) else target[part]
            key=int(parts[-1]) if isinstance(target,list) else parts[-1]
            if target[key] is not None:fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开模板的私人绑定覆盖了非空规则。',503)
            target[key]=deepcopy(value)
        # 结构约束由代码复验；attestation只是本人声明的情景条件，不是理赔决定。
        facts=context['scenario_facts'];bounded(facts)
        common={'optional_responsibility_ids','prior_claim_count','prior_payout_count','prior_eligible_medical_expenses'}
        medical=fact['template_id']=='medical_general_first_annual_claim_standard_ratio'
        critical=fact['template_id']=='critical_first_only_no_functional_event'
        specific={'social_insured','social_settled','special_disease_reduction_applies','cost_categories'} if medical else {'functional_impairment_event','default_fixed_terms_confirmed','policy_cap_confirmed'}
        if not (medical or critical):return self._missing_calculation_fact(fact,'尚未实现该模板的完整分支验证。')
        fields(facts,common|specific,common|specific)
        if facts['optional_responsibility_ids']!=[] or type(facts['prior_claim_count']) is not int or type(facts['prior_payout_count']) is not int:
            return self._missing_calculation_fact(fact,'可选责任或既往历史不属于当前受限模板。')
        if facts['prior_claim_count']!=0 or facts['prior_payout_count']!=0 or money(facts['prior_eligible_medical_expenses'])!=0:
            return self._missing_calculation_fact(fact,'此模板仅支持已确认首次情景；后续累计或责任终止状态须另核。')
        if money(terms['policy_annual_used'])!=0 or any(money(r['annual_used'])!=0 for r in terms['responsibilities']):
            return self._missing_calculation_fact(fact,'该公开模板要求首次年度使用，不能将累计给付忽略后计算。')
        if any(money(r['used'])!=0 for r in terms['deductibles']):
            return self._missing_calculation_fact(fact,'当前首次情景不能包含先前已抵扣免赔额。')
        conditions=scenario.get('conditions',{}) if isinstance(scenario,dict) else {}
        if any(conditions.get(k) is not True for k in ('in_force','covered_event','exclusions_cleared')):
            return self._missing_calculation_fact(fact,'有效性、事件或排除条件尚未满足当前受限情景。')
        if medical:
            costs=scenario.get('costs',[])
            if type(costs) is not list:fail()
            for cost in costs:
                fields(cost,{'cost_id','amount','other_reimbursements'},{'cost_id','amount','other_reimbursements'});ident(cost['cost_id'])
            cost_ids=[r['cost_id'] for r in costs]
            fields(facts['cost_categories'],cost_ids,cost_ids)
            if (facts['social_insured'] is not True or facts['social_settled'] is not True or facts['special_disease_reduction_applies'] is not False
                    or any(v!='ordinary_general_medical_no_sublimit' for v in facts['cost_categories'].values())
                    or conditions.get('provider_eligible') is not True or scenario.get('fixed_events')!=[]):
                return self._missing_calculation_fact(fact,'社保、医院或费用细类不属于已规范的普通一般医疗分支。')
            for row in terms['responsibilities']:
                expected=cost_ids if row['responsibility_id']=='ZXG499' else []
                if set(row['cost_ids'])!=set(expected):return self._missing_calculation_fact(fact,'费用归属不满足仅一般医疗责任的模板条件。')
        else:
            if (scenario.get('fixed_events')!=['ZXG316'] or scenario.get('costs')!=[] or facts['functional_impairment_event'] is not False
                    or facts['default_fixed_terms_confirmed'] is not True or facts['policy_cap_confirmed'] is not True):
                return self._missing_calculation_fact(fact,'首次重疾模板不能自动合并功能损伤、可选责任或未核保单总限额。')
        start=day(terms['usage_period_start']);end=day(terms['usage_period_end']);event=day(scenario.get('event_date'))
        if not terms['policy_start']<=start<=event<=end<=terms['policy_end']:
            fail('RESEARCH_INVALID','私人使用期间须覆盖事故并处于当前保单期间。',422)
        if (start!=terms['policy_start'] or end!=terms['policy_end']
                or self._anniversary(date.fromisoformat(start),1)-timedelta(days=1)!=date.fromisoformat(end)):
            return self._missing_calculation_fact(fact,'此模板只接受完整的同一一年期合同年度，其他期间须另核。')
        result=self._derived_fact(fact,terms,versions,{'policy_record_id':policy['id'],'record_revision':policy['record_revision'],
            'public_plan_entity_id':item['entity_id'],'template_id':fact['template_id'],'context':deepcopy(context),
            'assertion_state':'user_confirmed_conditional_scenario_not_insurer_verified'})
        return {**result,'entity_id':policy['id'],'public_entity_id':item['entity_id']}

    @staticmethod
    def _anniversary(anchor,years=0,months=0,days=0):
        if any(type(n) is not int or n<0 for n in (years,months,days)) or years>150 or months>1800 or days>55000:fail()
        index=anchor.year*12+anchor.month-1+years*12+months
        try:return date(index//12,index%12+1,anchor.day)+timedelta(days=days)
        except (ValueError,OverflowError):fail('RESEARCH_PRIVATE_INPUT_REQUIRED','周年日期存在闰日或月末歧义，需要合同规则明确，未自行调整日期。',409)

    def _coverage_terms(self,item,policy,context,versions,scenario):
        fact=self._trusted_fact(item,'coverage_scenario_rules',currency='CNY')
        if fact['state']=='missing':
            original=self._trusted_fact(item,'coverage_scenario_terms',currency='CNY')
            if original['state']=='verified':return self._coverage_template_terms(item,original,policy,context,versions,scenario)
            return original
        if fact['state']!='verified':return fact
        rules=deepcopy(fact['value'])
        allowed={'schema_version','scope','representation_complete','unrepresented_terms','terms_template','waiting_by_category',
            'annual_period_basis','tier_basis','mutually_exclusive_groups','tier_variants'}
        if type(rules) is not dict or set(rules)-allowed or rules.get('schema_version')!=1:
            return self._missing_calculation_fact(fact,'公开保障规则规范版本或字段尚不能完整消费。')
        if rules.get('representation_complete') is not True or rules.get('unrepresented_terms')!=[]:
            return self._missing_calculation_fact(fact,'所选责任仍有未表达的合同规则，不能忽略后计算。')
        if context is None:return self._missing_calculation_fact(fact,'需要本人明确年度使用期间、已用额度、等待类别和发票分配；未知不当作零。')
        required={'usage_period_start','usage_period_end','policy_annual_used','shared_used','deductible_used',
            'responsibility_used','responsibility_cost_ids','prior_eligible_costs','waiting_category'}
        fields(context,required|{'settlement_variant'},required)
        ident(context['waiting_category'])
        if 'settlement_variant' in context:ident(context['settlement_variant'])
        fixed=scenario.get('fixed_events',[]) if isinstance(scenario,dict) else []
        for group in rules.get('mutually_exclusive_groups',[]):
            if type(group) is not list:fail()
            if any(type(x) is not str for x in fixed) or len(set(fixed)&set(group))>1:
                return self._missing_calculation_fact(fact,'同一情景选择了互斥给付责任，不能直接加总。')
        value=json.loads(policy['value'])['value'];start=date.fromisoformat(value['start_on']);end=date.fromisoformat(value['end_on'])
        usage_start=date.fromisoformat(day(context['usage_period_start']));usage_end=date.fromisoformat(day(context['usage_period_end']))
        basis=rules.get('annual_period_basis')
        if basis=='policy_year':
            if (usage_start.month,usage_start.day)!=(start.month,start.day) or usage_start<start:
                fail('RESEARCH_INVALID','年度计数须绑定保单年度，不能套用其他年度额度。',422)
            expected_end=min(self._anniversary(usage_start,1)-timedelta(days=1),end)
        elif basis=='calendar_year':
            if usage_start!=max(date(usage_start.year,1,1),start):fail()
            expected_end=min(date(usage_start.year,12,31),end)
        else:return self._missing_calculation_fact(fact,'年度使用期间的合同计量基础尚未明确。')
        if usage_end!=expected_end or usage_start>end:fail('RESEARCH_INVALID','年度已用额度的期间与保单合同年度不一致。',422)
        waiting=rules.get('waiting_by_category',{}).get(context['waiting_category'])
        if type(waiting) is not dict or set(waiting)!={'days','start_day_counts_as_one','end_inclusive'}:
            return self._missing_calculation_fact(fact,'该情景等待期类别或计算边界尚未完整取得。')
        if type(waiting['days']) is not int or not 0<=waiting['days']<=3650 or any(type(waiting[k]) is not bool for k in ('start_day_counts_as_one','end_inclusive')):fail()
        waiting_end=start+timedelta(days=max(0,waiting['days']-int(waiting['start_day_counts_as_one'])))
        terms=deepcopy(rules.get('terms_template'))
        fields(terms,{'deduction_order','policy_annual_limit','shared_limits','deductibles','responsibilities','payment_order'},
            {'deduction_order','policy_annual_limit','shared_limits','deductibles','responsibilities','payment_order'})
        for key in ('shared_limits','deductibles','responsibilities'):
            if type(terms[key]) is not list or len(terms[key])>100:fail()
        for row in terms['shared_limits']:fields(row,{'group_id','limit'},{'group_id','limit'})
        for row in terms['deductibles']:fields(row,{'deductible_id','amount','period'},{'deductible_id','amount','period'})
        for row in terms['responsibilities']:
            fields(row,{'responsibility_id','mechanism','shared_limit_id','deductible_id','annual_limit','per_claim_limit','reimbursement_tiers','benefit_amount'},
                {'responsibility_id','mechanism','shared_limit_id','deductible_id','annual_limit','per_claim_limit'})
        groups=[ident(r['group_id']) for r in terms['shared_limits']];deductibles=[ident(r['deductible_id']) for r in terms['deductibles']]
        responsibilities=[ident(r['responsibility_id']) for r in terms['responsibilities']]
        if len(set(responsibilities))!=len(responsibilities):fail()
        maps=[('shared_used',groups),('deductible_used',deductibles),('responsibility_used',responsibilities),('responsibility_cost_ids',responsibilities)]
        for key,ids in maps:fields(context[key],ids,ids)
        counter_values=[context['policy_annual_used'],*context['shared_used'].values(),*context['deductible_used'].values(),*context['responsibility_used'].values()]
        if any(v is None for v in counter_values):return self._missing_calculation_fact(fact,'私人已用额度不完整，未默认填零。')
        for v in counter_values:money(v)
        terms.update(policy_start=start.isoformat(),policy_end=end.isoformat(),waiting_end=waiting_end.isoformat(),
            waiting_end_inclusive=waiting['end_inclusive'] if waiting['days'] else False,
            usage_period_start=usage_start.isoformat(),usage_period_end=usage_end.isoformat(),policy_annual_used=context['policy_annual_used'])
        for row in terms['shared_limits']:row['used']=context['shared_used'][row['group_id']]
        for row in terms['deductibles']:row['used']=context['deductible_used'][row['deductible_id']]
        annual_ids={r for r,b in rules.get('tier_basis',{}).items() if b=='annual_cumulative_eligible_cost'}
        fields(context['prior_eligible_costs'],annual_ids,annual_ids)
        variants=rules.get('tier_variants',{})
        if variants:
            selected=variants.get(context.get('settlement_variant'))
            if not isinstance(selected,dict):return self._missing_calculation_fact(fact,'须明确社保结算等费率变体，不能默认采用高比例。')
        else:selected={}
        for row in terms['responsibilities']:
            rid=row['responsibility_id'];costs=context['responsibility_cost_ids'][rid]
            if type(costs) is not list or len(costs)>100:fail()
            for cid in costs:ident(cid)
            row.update(annual_used=context['responsibility_used'][rid],cost_ids=costs)
            if rid in selected:row['reimbursement_tiers']=deepcopy(selected[rid])
            tier_basis=rules.get('tier_basis',{}).get(rid)
            if row['mechanism']=='expense_compensation' and tier_basis not in ('per_claim_eligible_cost','annual_cumulative_eligible_cost'):
                return self._missing_calculation_fact(fact,'赔付比例的累计基础尚未明确。')
            if rid in annual_ids:
                prior=context['prior_eligible_costs'][rid]
                if prior is None:return self._missing_calculation_fact(fact,'年度累计合格费用未知，未把比例阶梯按每次出险重新起算。')
                used=money(prior);tiers=[]
                for tier in row['reimbursement_tiers']:
                    upper=money(tier['up_to']) if tier['up_to'] is not None else None
                    if upper is not None and upper<=used:continue
                    tiers.append({**tier,'up_to':format(Decimal(upper-used)/100,'.2f') if upper is not None else None})
                row['reimbursement_tiers']=tiers
        return self._derived_fact(fact,terms,versions,{'policy_record_id':policy['id'],'record_revision':policy['record_revision'],
            'contract_version':value['contract_version'],'context':deepcopy(context),'scope':rules.get('scope')})

    def _pension_schedule(self,item,context,versions):
        fact=self._trusted_fact(item,'pension_cashflow_rule_set',currency='CNY')
        if fact['state']=='missing':return self._trusted_fact(item,'cashflow_schedule',currency='CNY')
        if fact['state']!='verified':return fact
        rules=deepcopy(fact['value'])
        if type(rules) is not dict or rules.get('schema_version')!=1 or rules.get('coordinate_basis')!='calendar_anniversary':
            return self._missing_calculation_fact(fact,'养老现金流的公开时间坐标和规范尚不完整。')
        allowed={'schema_version','scenario_id','source_scenario','coordinate_basis','complete','projection_end_offset_years',
            'projection_end_offset_months','items','terminal_basis','unsupported_terms'}
        if set(rules)-allowed:return self._missing_calculation_fact(fact,'养老规范包含尚未实现的规则，未忽略后计算。')
        if rules.get('unsupported_terms',[]) or not rules.get('terminal_basis') and rules.get('complete') is True:
            return self._missing_calculation_fact(fact,'养老现金流仍有未表达条款或终值缺口。')
        if context is None:return self._missing_calculation_fact(fact,'请选择完整公开示例及本人确认的研究起始日。')
        fields(context,{'scenario_id','policy_start_on'},{'scenario_id','policy_start_on'})
        text(context['scenario_id'],160)
        if context['scenario_id']!=rules.get('scenario_id'):fail('RESEARCH_INVALID','私人选择与公开养老示例条件不一致，不能自行改保费或给付。',422)
        anchor=date.fromisoformat(day(context['policy_start_on']));flows=[]
        if type(rules.get('items')) is not list or len(rules['items'])>1000:fail()
        for row in rules['items']:
            fields(row,{'cashflow_id','offset_years','offset_months','offset_days','amount','kind','guaranteed'},
                {'cashflow_id','offset_years','offset_months','offset_days','amount','kind','guaranteed'})
            when=self._anniversary(anchor,row['offset_years'],row['offset_months'],row['offset_days'])
            flows.append({k:row[k] for k in ('cashflow_id','amount','kind','guaranteed')}|{'date':when.isoformat()})
        end=self._anniversary(anchor,rules.get('projection_end_offset_years'),rules.get('projection_end_offset_months',0))
        return self._derived_fact(fact,{'complete':rules.get('complete'),'projection_end':end.isoformat(),'items':flows},versions,
            {'scenario_id':rules['scenario_id'],'source_scenario':rules.get('source_scenario'),'context':deepcopy(context),
             'date_convention':'本人确认的周年日研究坐标，不是保险公司保证的划款时刻'})

    def calculate(self,w,payload):
        """服务端解析事实的纯计算入口；不接收客户端verified、费率或赔付规则。"""
        required={'expected_revision','method','confirmed'}
        fields(payload,required|{'id','entity_id','policy_record_id','scenario','currency','until','account_id','policy_record_ids','day_basis','coverage_context','pension_context'},required)
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','保存本人测算需要确认。',409)
        method=payload['method'];evidence={}
        if method not in ('reserve_obligations','check_coverage_scenario','pension_cashflows'):fail()
        currency=payload.get('currency','CNY')
        if currency!='CNY':fail('RESEARCH_INVALID','此私有测算只接入人民币原始账本。',422)
        state=None;bank_budget=None;funding=None;serialized_funding=None;bank_obligation_ids=set()
        if method=='reserve_obligations':
            account_id=ident(payload.get('account_id'))
            with self.g.bank.transaction() as bank:
                self.binding(w,bank);captured_signature=self._bank_funding_signature(bank,w,account_id)
            state=self.g.overview(w);bank_budget=self.g.budget(w,account_id)
        with self.transaction(w,payload['expected_revision']) as (bank,private,binding,revision):
            as_of=self.g.bank._workspace(bank,w)['as_of'];versions={}
            if method=='reserve_obligations':
                until=day(payload.get('until'));account=self.g.bank._account(bank,w,ident(payload.get('account_id')))
                if not 0<=(date.fromisoformat(until)-date.fromisoformat(as_of)).days<=30:
                    fail('RESEARCH_INVALID','当前账本承诺清单明确覆盖30天，请选择此范围或补齐来源。',422)
                if (state['version']!=self.g.bank._workspace(bank,w)['version'] or bank_budget['version']!=state['version']
                        or captured_signature!=self._bank_funding_signature(bank,w,account['id'])):
                    fail('RESEARCH_VERSION_CONFLICT','银行底线、承诺、余额或占用在报价期间变化。',409)
                selected=payload.get('policy_record_ids',[])
                if type(selected) is not list or len(selected)>50:fail()
                for rid in selected:ident(rid)
                if len(selected)!=len(set(selected)):fail()
                inputs,scope,funding,serialized_funding,bank_obligation_ids=self._reserve_inventory(
                    bank,private,binding,state,bank_budget,account,selected,until,captured_signature)
            else:
                eid=ident(payload.get('entity_id'));item=self.public_detail(eid);evidence={eid:item};versions=dict(item['entity'].get('source_versions',{}))
                if method=='pension_cashflows':
                    basis=payload.get('day_basis',365)
                    if type(basis) is not int or basis not in (365,366):fail()
                    inputs={'as_of':datetime.now(TZ).date().isoformat(),'currency':currency,'evidence_versions':versions,'entity_id':eid,
                        'cashflow_schedule':self._pension_schedule(item,payload.get('pension_context'),versions),'day_basis':basis}
                    scope={'entity_id':eid,'pension_context':deepcopy(payload.get('pension_context')),
                        'notice':'政府个人养老金机制与商业养老产品现金流分开；缺失的保证给付、退保价值不推算，官方示例及私人日期锚定保持标签。'}
                else:
                    rid=ident(payload.get('policy_record_id'));policy=self._row(private,binding,rid)
                    if policy['kind']!='policy':fail()
                    value=json.loads(policy['value'])['value']
                    if value['entity_id']!=eid or value['consent']['health_data_authorized'] is not True:fail('CONFIRMATION_REQUIRED','情景保单身份必须一致且已授权保障资料处理。',409)
                    item=json.loads(policy['value']).get('evidence',{}).get(eid,item)
                    evidence={eid:item};versions=dict(item['entity'].get('source_versions',{}))
                    bounded(payload.get('scenario'));scenario=deepcopy(payload.get('scenario'))
                    bounded(payload.get('coverage_context'))
                    terms=self._coverage_terms(item,policy,payload.get('coverage_context'),versions,scenario)
                    terms={**terms,'entity_id':rid,'public_entity_id':eid}
                    inputs={'as_of':datetime.now(TZ).date().isoformat(),'currency':currency,'evidence_versions':versions,'policy_id':rid,
                        'terms':terms,'scenario':scenario}
                    scope={'policy_record_id':rid,'policy_record_revision':policy['record_revision'],'contract_version':value['contract_version'],
                        'coverage_context':deepcopy(payload.get('coverage_context')),'private_context_state':'user_confirmed_not_insurer_verified',
                        'notice':'仅在明确情景条件下核算，不产生承保、续保、理赔或既往已用额度事实。'}
        from research_methods import reserve_obligations,check_coverage_scenario,pension_cashflows
        result={'reserve_obligations':reserve_obligations,'check_coverage_scenario':check_coverage_scenario,'pension_cashflows':pension_cashflows}[method](inputs)
        if method=='reserve_obligations':
            from financial_methods.funding import evaluate_funding
            obligation_result=deepcopy(result)
            reconciliation=evaluate_funding(funding['context'],funding['snapshot'],funding['events'],funding['holds']).to_dict()
            if result['status']=='complete' and reconciliation['status']=='calculated':
                current=reconciliation['metrics']['current_budget']['value_minor']
                additional=sum(r['additional_reserve_minor'] for r in result['rows'] if r['obligation_id'] not in bank_obligation_ids)
                available=max(0,current)
                if available!=max(0,bank_budget['investable_minor']-additional):
                    fail('RESEARCH_FUNDING_LINK_CONFLICT','研究报价与原银行已有保护不能对齐，未另造净额。',409)
                result['calculations']={**result['calculations'],'required_reserve_minor':funding['snapshot'].balance_minor-current,
                    'additional_reserve_minor':additional,'uncommitted_cash_minor':available,'shortfall_minor':max(0,-current),
                    'bank_floor_minor':bank_budget['reserve_minor'],'bank_commitments_minor':bank_budget['upcoming_due_minor'],
                    'actual_held_minor':bank_budget['held_minor'],'already_protected_minor':bank_budget['reserve_minor']+bank_budget['upcoming_due_minor']+bank_budget['held_minor'],
                    'bank_current_available_minor':bank_budget['investable_minor'],'new_hold_required':additional>0,
                    'notice':'总需保护与新增hold不同；底线和原银行承诺不是新增冻结，原30日保护不会因较短目标窗口释放。'}
                result['rows']=[{**r,'additional_reserve_minor':0 if r['obligation_id'] in bank_obligation_ids else r['additional_reserve_minor'],
                    'bank_protected_minor':r['amount_minor'] if r['obligation_id'] in bank_obligation_ids else 0,
                    'protection_basis':'original_bank_commitment' if r['obligation_id'] in bank_obligation_ids else 'explicit_private_obligation_and_actual_allocations'}
                    for r in result['rows']]
                result['bank_protection_rows']=[{**r,'obligation_id':r['kind']+':'+r['evidence_id'],
                    'bank_protected_minor':r['amount_minor'],'additional_reserve_minor':0} for r in bank_budget['commitments']]
            else:
                result={**result,'status':'missing','calculations':None,
                    'missing':[*(result.get('missing') or []),{'path':'bank_protection_reconciliation','reason':reconciliation.get('coverage_gaps',[])}]}
            result['algorithm_version']='navren.research-protection-binding/1'
            result['component_algorithms']={'obligations':'navren.research-methods/1','funding':'financial_methods/1.0.0'}
            result['obligation_calculation']=obligation_result
            result['funding_reconciliation']=reconciliation
        value={'method':method,'inputs':inputs,'result':result,'source_scope':scope,'algorithm_version':result['algorithm_version'],
            'status':'ready' if result['status']=='complete' else 'needs_input'}
        if serialized_funding is not None:value['funding_inputs']=serialized_funding
        return self._persist(w,payload,'comparison',value,evidence)

    @staticmethod
    def _attachment_view(row):
        return {k:row[k] for k in ('id','mime_type','sha256','byte_count','created_at')}|{'parse_status':'pending_user_review','execution_authority':False}

    def upload(self,w,body,*,mime_type,upload_key,expected_revision,confirmed,health_data_authorized):
        if confirmed is not True or health_data_authorized is not True:
            fail('CONFIRMATION_REQUIRED','上传保单或健康原件需要明确授权保存。',409)
        ident(upload_key)
        if not isinstance(body,bytes) or not 0<len(body)<=10*1024*1024:fail('UPLOAD_TOO_LARGE','原件须为1字节至10MiB。',413)
        signatures={'application/pdf':body.startswith(b'%PDF-'),'image/png':body.startswith(b'\x89PNG\r\n\x1a\n'),
                    'image/jpeg':body.startswith(b'\xff\xd8\xff'),'image/webp':body.startswith(b'RIFF') and body[8:12]==b'WEBP'}
        if not signatures.get(mime_type):fail('RESEARCH_INVALID','原件格式须为PDF、PNG、JPEG或WebP，且与内容一致。',422)
        sha=hashlib.sha256(body).hexdigest()
        with self.transaction(w) as (_,db,binding,revision):
            old=db.execute('SELECT * FROM research_workspace_attachments WHERE workspace_id=? AND registered_user_id=? AND upload_key=?',
                (w,binding['registered_user_id'],upload_key)).fetchone()
            if old:
                if old['deleted'] or old['sha256']!=sha or old['mime_type']!=mime_type:
                    fail('IDEMPOTENCY_CONFLICT','同一上传键不能对应另一原件。',409)
                return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision,'attachment':self._attachment_view(old)}
            if type(expected_revision) is not int or revision!=expected_revision:fail('RESEARCH_VERSION_CONFLICT','研究已更新，请重读后上传。',409)
            aid=uuid.uuid4().hex;now=self.g.bank.clock()
            encrypted=self.store.protect(base64.b64encode(body).decode('ascii')).encode('ascii')
            db.execute('INSERT INTO research_workspace_attachments VALUES(?,?,?,?,?,?,?,?,?,?,0)',
                (aid,binding['registered_user_id'],w,upload_key,mime_type,sha,len(body),encrypted,
                 self.store.protect(packed({'purpose':'private_policy_research','health_data_authorized':True})),now))
            self._increment(db,binding,revision)
            return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision+1,
                    'attachment':self._attachment_view(self._attachment_row(db,binding,aid))}

    def attachment(self,w,aid):
        with self.transaction(w) as (_,db,binding,revision):
            row=self._attachment_row(db,binding,ident(aid))
            try:body=base64.b64decode(self.store._decode(bytes(row['payload_cipher']).decode('ascii')),validate=True)
            except (ValueError,TypeError,UnicodeError):fail('RESEARCH_UNREADABLE','资料原件无法解密。',503)
            if len(body)!=row['byte_count'] or hashlib.sha256(body).hexdigest()!=row['sha256']:
                fail('RESEARCH_UNREADABLE','资料原件完整性校验失败。',503)
            return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision,'attachment':self._attachment_view(row)},body

    def delete_attachment(self,w,aid,payload):
        fields(payload,{'expected_revision','confirmed'},{'expected_revision','confirmed'})
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','删除原件需要本人确认。',409)
        with self.transaction(w,payload['expected_revision']) as (_,db,binding,revision):
            self._attachment_row(db,binding,ident(aid))
            # 清除原始密文；保留不可变哈希和审计标识。历史保单不再能读取已删原件。
            db.execute("UPDATE research_workspace_attachments SET payload_cipher=x'',value=?,deleted=1 WHERE id=?",(self.store.protect('{}'),aid))
            self._increment(db,binding,revision)
            return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision+1,'id':aid,'deleted':True}

    @staticmethod
    def _record_digest(row):
        value=json.loads(row['value'])
        return fingerprint({'value':value['value'],'evidence':value.get('evidence',{}),'record_revision':row['record_revision']})

    def _expanded_dependencies(self,private,binding,rows):
        result={};queue=list(rows)
        while queue:
            row=queue.pop(0)
            if row['id'] in result:continue
            if len(result)>=100:fail('RESEARCH_INVALID','研究依赖超过明确核验范围。',422)
            result[row['id']]=row
            value=json.loads(row['value'])['value'];scope=value.get('source_scope',{})
            refs=list(scope.get('selected_policy_records',[]))
            if scope.get('policy_record_id'):refs.append({'record_id':scope['policy_record_id'],'record_revision':scope['policy_record_revision']})
            for ref in refs:
                parent=self._row(private,binding,ref['record_id'])
                if parent['record_revision']!=ref['record_revision']:fail('RESEARCH_VERSION_CONFLICT','计算依赖的保单已更正，请重新计算后准备。',409)
                queue.append(parent)
            self._assert_record_inputs(private,binding,row)
        return list(result.values())

    def _assert_record_inputs(self,private,binding,row):
        value=json.loads(row['value'])['value']
        if row['kind']=='policy':
            for aid in value['attachment_ids']:self._attachment_row(private,binding,aid)
            if value.get('existing_policy_id'):
                current=self.g.financial_query(binding['workspace_id'],'get_policy',{'policy_id':value['existing_policy_id']})
                if str(current['dataset_version'])!=str(value['existing_policy_version']):
                    fail('RESEARCH_VERSION_CONFLICT','补充保单的原始资料版本已变化。',409)

    def _private_funding_intent(self,bank,private,binding,value,account_id,amount,linked):
        ids=value['conditions'].get('obligation_ids',[])
        reserve_quotes=[r for r in linked if json.loads(r['value'])['value'].get('method')=='reserve_obligations']
        if not ids:
            if reserve_quotes:fail('RESEARCH_ALLOCATION_REQUIRED','已关联保费报价，须重新报价并明确要保护的保单义务；额外现金预留请另建无报价任务。',409)
            return {}
        if len(set(ids))!=len(ids) or any(not oid.startswith('policy:') for oid in ids):
            fail('RESEARCH_INVALID','新增保费预留须选择不重复的明确私人保单义务。',422)
        policies=[self._row(private,binding,ident(oid[len('policy:'):])) for oid in ids]
        if any(p['kind']!='policy' for p in policies):fail()
        matches=[]
        for record in linked:
            v=json.loads(record['value'])['value'];scope=v.get('source_scope',{})
            if (v.get('method')=='reserve_obligations' and scope.get('account_id')==account_id
                    and all(any(r['record_id']==p['id'] and r['record_revision']==p['record_revision'] for r in scope.get('selected_policy_records',[])) for p in policies)):
                matches.append((record,v))
        if len(matches)!=1:fail('RESEARCH_CALCULATION_REQUIRED','新增保费预留须关联一份同账户、同保单版本的有效报价。',409)
        quote,v=matches[0];scope=v['source_scope']
        if v.get('algorithm_version')!='navren.research-protection-binding/1':
            fail('RESEARCH_CALCULATION_REQUIRED','历史报价保留只读；执行前须按当前保护口径重新报价。',409)
        if v['result'].get('status')!='complete' or v['result'].get('calculations') is None:
            fail('RESEARCH_CALCULATION_REQUIRED','报价尚未完成来源及保护对账，不能准备预留。',409)
        signature=self._bank_funding_signature(bank,binding['workspace_id'],account_id)
        if scope.get('bank_protection_signature')!=signature:
            fail('RESEARCH_VERSION_CONFLICT','账户底线、义务或已有预留已变化，请重新报价。',409)
        known={r['kind']+':'+r['evidence_id'] for r in scope['bank_budget']['commitments']}
        rows={r['obligation_id']:r for r in v['result'].get('rows',[])};allocations=[];seen=set()
        for oid,policy in zip(ids,policies):
            data=json.loads(policy['value'])['value'];canonical=scope['policy_obligation_aliases'][oid]
            if canonical in seen:fail('RESEARCH_DUPLICATE_OBLIGATION','所选档案映射同一保单缴费期，不能重复分配。',409)
            seen.add(canonical)
            if data['premium']['next_due_on']>value['due_on']:
                fail('RESEARCH_INVALID','预留到期日不能早于所绑定的保费到期日。',422)
            if canonical in known:fail('RESEARCH_ALREADY_PROTECTED','该期保费已在原银行承诺中保护，无需重复冻结。',409)
            obligation=rows.get(canonical)
            if not obligation:fail('RESEARCH_OBLIGATION_OUTSIDE_WINDOW','该缴费不在本次报价目标窗口内。',409)
            part=amount if len(ids)==1 else obligation['additional_reserve_minor']
            if part<=0:fail('RESEARCH_NO_ADDITIONAL_HOLD_REQUIRED','所选缴费无需再冻结，可保存安排或提醒。',409)
            if part>obligation['additional_reserve_minor']:
                fail('RESEARCH_INCREMENTAL_AMOUNT_EXCEEDED','新增hold超过该义务尚未保护的金额；不能把总保护再次冻结。',409)
            allocations.append({'obligation_id':oid,'canonical_obligation_id':canonical,'policy_record_id':policy['id'],
                'policy_record_revision':policy['record_revision'],'contract_version':data['contract_version'],
                'due_on':data['premium']['next_due_on'],'amount_minor':part,
                'quote_record_id':quote['id'],'quote_record_revision':quote['record_revision']})
            if not any(r['id']==policy['id'] for r in linked):linked.append(policy)
        if sum(a['amount_minor'] for a in allocations)!=amount:
            fail('RESEARCH_ALLOCATION_AMOUNT_MISMATCH','多项预留须按所选义务剩余需求整笔补足，分配总额须等于实际hold。',409)
        return {'account_id':account_id,'bank_protection_signature':signature,
            'allocations':allocations}

    @contextmanager
    def operation_guard(self,bank,w,op):
        """由原银行确认/SMS事务调用；bank锁先于公共发布锁，持续到提交。"""
        intent=bank.execute('SELECT * FROM research_operation_intents WHERE workspace_id=? AND idempotency_key=?',
            (w,op['idempotency_key'])).fetchone()
        if intent is None:yield;return
        binding=self.binding(w,bank)
        with closing(self.store.connect()) as private:
            row=self._row(private,binding,intent['record_id'])
            if (row['record_revision']!=intent['record_revision'] or self._record_digest(row)!=intent['record_digest']
                    or privacy_revision(bank,w)!=intent['privacy_revision']):
                fail('RESEARCH_VERSION_CONFLICT','研究条件或授权已变化，请重新准备。',409)
            if fingerprint(json.loads(op['payload']))!=intent['core_digest']:
                fail('RESEARCH_OPERATION_MISMATCH','操作与本人所选研究安排不一致。',409)
            funding=json.loads(intent['funding'])
            if funding and self._bank_funding_signature(bank,w,funding['account_id'])!=funding['bank_protection_signature']:
                fail('RESEARCH_VERSION_CONFLICT','原银行底线、义务、账户或已有hold已变化，请重新准备。',409)
            for rid,digest in json.loads(intent['dependencies']).items():
                dependency=self._row(private,binding,rid)
                if self._record_digest(dependency)!=digest:
                    fail('RESEARCH_VERSION_CONFLICT','关联研究条件已经变化，请重新准备。',409)
                self._assert_record_inputs(private,binding,dependency)
                source=json.loads(dependency['value'])['value'].get('source_scope',{})
                if source.get('bank_version') is not None and self.g.bank._workspace(bank,w)['version']!=source['bank_version']:
                    fail('RESEARCH_VERSION_CONFLICT','计算依赖的账本已变化，请重新核对。',409)
                if source.get('bank_protection_signature') and self._bank_funding_signature(bank,w,source['account_id'])!=source['bank_protection_signature']:
                    fail('RESEARCH_VERSION_CONFLICT','计算依赖的原银行保护输入已变化。',409)
            guard=self.evidence_guard(intent['provider_revision']) if intent['provider_revision'] else nullcontext()
            with guard:yield

    def prepare(self,w,payload,*,agent_origin=False):
        required={'expected_revision','record_id','action','account_id','idempotency_key','confirmed','execution_choice'}
        fields(payload,required|{'entity_id','amount','object_id','expires_on'},required)
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','需明确选择具体体验操作。',409)
        for k in ('record_id','account_id','idempotency_key'):ident(payload[k])
        action=payload['action'];choice=payload['execution_choice']
        if action in ('purchase','redeem'):
            fail('RESEARCH_MAPPING_UNAVAILABLE','公开基金份额尚无相同身份的执行模型，不能用SIM产品替代成交。',409)
        if action!='reserve_funds' or choice!='reserve_and_keep_cash':
            fail('RESEARCH_MAPPING_UNAVAILABLE','当前研究入口仅接入本人明确选择的资金预留，其他操作仍走原业务入口。',409)
        amount=money(payload.get('amount'))
        if amount==0:fail('RESEARCH_NO_ADDITIONAL_HOLD_REQUIRED','无需再冻结；可以保存安排或提醒，不产生零额金融操作。',409)
        expires=day(payload.get('expires_on'))
        with self.transaction(w,payload['expected_revision']) as (bank,private,binding,revision):
            row=self._row(private,binding,payload['record_id'])
            if row['kind']!='task':fail('RESEARCH_INVALID','请先保存本人资金安排任务。',422)
            package=json.loads(row['value']);value=package['value'];conditions=value['conditions']
            if (value['kind']!='reserve' or value['status'] in ('cancelled','failed')
                or conditions.get('amount')!=payload['amount'] or conditions.get('currency')!='CNY'
                or conditions.get('account_id')!=payload['account_id'] or value['due_on']!=expires):
                fail('RESEARCH_OPERATION_MISMATCH','预留金额、账户和到期日必须与本人保存的任务一致。',409)
            # 任务关联的研究仍须来源有效；不把真实份额选中解释为真实申购。
            linked=self._expanded_dependencies(private,binding,[self._row(private,binding,ident(rid)) for rid in value['record_ids']])
            funding=self._private_funding_intent(bank,private,binding,value,payload['account_id'],amount,linked)
            evidence={**package.get('evidence',{})}
            for related in linked:
                evidence.update(json.loads(related['value']).get('evidence',{}))
            provider_revision=self._provider_call('revision') if evidence else None
            if evidence:
                with self.evidence_guard(provider_revision):
                    for eid,old in evidence.items():
                        if self.public_detail(eid)['evidence_version']!=old['evidence_version']:
                            fail('RESEARCH_EVIDENCE_CHANGED','所关联研究的公开资料已更新，请先重新核对。',409)
            account=self.g.bank._account(bank,w,payload['account_id'])
            if expires<=self.g.bank._workspace(bank,w)['as_of']:fail('RESEARCH_INVALID','预留到期日须晚于银行业务日。',422)
            key=payload['idempotency_key']
            task_id=hashlib.sha256((w+':research-reserve:'+row['id']).encode()).hexdigest()[:32]
            existing_task=bank.execute('SELECT * FROM orchestration_tasks WHERE workspace_id=? AND id=?',(w,task_id)).fetchone()
            if existing_task is None:
                owner=bank.execute("SELECT customer_id FROM signatories WHERE workspace_id=? AND account_id=? AND role='owner' AND status='active' ORDER BY id LIMIT 1",(w,account['id'])).fetchone()
                if owner is None and not bank.execute('SELECT 1 FROM signatories WHERE workspace_id=? AND account_id=?',(w,account['id'])).fetchone():
                    # 四角色模板已有明确开户主体，未生成signatory；仅在无任何签署授权记录时复用。
                    owner=bank.execute("SELECT customer_id FROM account_details WHERE workspace_id=? AND account_id=? AND status='active' ORDER BY id LIMIT 1",(w,account['id'])).fetchone()
                if not owner:fail('RESEARCH_MAPPING_UNAVAILABLE','无法核对预留任务所属账户的本人身份。',409)
                self.g.bank._insert_archive_row(bank,'orchestration_tasks',w,{'id':task_id,'customer_id':owner['customer_id'],
                    'title':'本人确认的研究资金安排','budget_minor':amount,'due_on':expires,'status':'draft','approval_policy':'原确认及操作级强验证'})
            elif existing_task['budget_minor']!=amount or existing_task['due_on']!=expires:
                fail('RESEARCH_VERSION_CONFLICT','已有预留任务的金额或期限不同，请另建明确任务。',409)
            core={'action':action,'account_id':account['id'],'amount_minor':amount,'task_id':task_id,'expires_on':expires,'idempotency_key':key}
            intent=(w,key,row['id'],row['record_revision'],self._record_digest(row),privacy_revision(bank,w),provider_revision,fingerprint(core),self.g.bank.clock(),
                    packed({r['id']:self._record_digest(r) for r in linked}),packed(funding))
            old=bank.execute('SELECT * FROM research_operation_intents WHERE workspace_id=? AND idempotency_key=?',(w,key)).fetchone()
            if old:
                if tuple(old)[2:8]!=intent[2:8] or old['dependencies']!=intent[9] or old['funding']!=intent[10]:fail('IDEMPOTENCY_CONFLICT','同一研究意图不能改变条件或资料版本。',409)
            else:
                if bank.execute('SELECT 1 FROM operations WHERE workspace_id=? AND idempotency_key=?',(w,key)).fetchone():fail('IDEMPOTENCY_CONFLICT','该操作键已用于另一业务。',409)
                bank.execute('INSERT INTO research_operation_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)',intent)
        # 意图先提交；即使准备后断连或记录链接前崩溃，原确认事务仍能查到守卫。
        prepared=self.g.prepare(w,core,agent_origin=agent_origin)
        op=prepared['operation']
        with self.g.bank.transaction() as bank:
            saved=self.g.bank._operation(bank,w,op['id'])
            with self.operation_guard(bank,w,saved):pass
        linked=self.associate(w,row['id'],{'expected_revision':payload['expected_revision'],'operation_id':op['id'],'confirmed':True})
        return {**linked,**prepared,'research_relation':'goal_funding_allocation','public_share_execution':False}

    def associate(self,w,rid,payload):
        fields(payload,{'expected_revision','operation_id','confirmed'},{'expected_revision','operation_id','confirmed'})
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','关联本人操作需要确认。',409)
        with self.transaction(w,payload['expected_revision']) as (bank,private,binding,revision):
            row=self._row(private,binding,ident(rid));op=self.g.bank._operation(bank,w,ident(payload['operation_id']))
            intent=bank.execute('SELECT * FROM research_operation_intents WHERE workspace_id=? AND idempotency_key=?',(w,op['idempotency_key'])).fetchone()
            if not intent or intent['record_id']!=rid or intent['record_digest']!=self._record_digest(row):
                fail('RESEARCH_OPERATION_MISMATCH','该操作未按这份研究的相同条件准备。',409)
            package=json.loads(row['value']);links=package.setdefault('operation_links',[])
            if not any(x['operation_id']==op['id'] for x in links):
                links.append({'operation_id':op['id'],'relation':'goal_funding_allocation','action':op['action'],'public_share_execution':False,
                    'funding_allocations':json.loads(intent['funding']).get('allocations',[]),
                    'funding_allocation':json.loads(intent['funding']).get('allocation')})
                private.execute('UPDATE research_workspace_records SET value=?,updated_at=? WHERE id=?',
                    (self.store.protect(packed(package)),self.g.bank.clock(),rid))
                self._increment(private,binding,revision);revision+=1
            return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision,'record':self._view(self._row(private,binding,rid))}

    def operation(self,w,oid):
        with self.transaction(w) as (bank,private,binding,revision):
            op=self.g.bank._operation(bank,w,ident(oid))
            intent=bank.execute('SELECT * FROM research_operation_intents WHERE workspace_id=? AND idempotency_key=?',(w,op['idempotency_key'])).fetchone()
            if intent is None:fail('RESEARCH_NOT_FOUND','此操作不是研究任务产生的操作。',404)
            row=self._row(private,binding,intent['record_id'])
            return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision,'operation':self.g.bank._public_operation(op),
                    'record_id':row['id'],'research_relation':'goal_funding_allocation','public_share_execution':False}

    @contextmanager
    def send_guard(self,w,expected_revision):
        with self.g.bank.transaction() as db,closing(self.store.connect()) as private:
            binding=self.binding(w,db)
            if self._revision(private,binding)!=expected_revision:fail('RESEARCH_VERSION_CONFLICT','研究已变化，旧响应未发送。',409)
            if private.execute("SELECT 1 FROM research_workspace_outbox WHERE workspace_id=? AND status='pending' LIMIT 1",(w,)).fetchone():fail('RESEARCH_RECOVERY_PENDING','研究的关联记录尚待恢复，未发送成功回执。',503)
            yield db

    def delete(self,w,rid,payload):
        fields(payload,{'expected_revision','confirmed'},{'expected_revision','confirmed'})
        if payload['confirmed'] is not True:fail('CONFIRMATION_REQUIRED','删除研究资料需要明确确认。',409)
        with self.transaction(w,payload['expected_revision']) as (bank,db,binding,revision):
            row=self._row(db,binding,ident(rid));now=self.g.bank.clock()
            self._invalidate_dependents(bank,w,rid)
            db.execute('INSERT INTO research_workspace_history VALUES(?,?,?,?,?,?)',(rid,binding['registered_user_id'],w,row['record_revision'],self.store.protect(packed(self._view(row,evaluate=False))),now))
            db.execute('UPDATE research_workspace_records SET deleted=1,status=\'cancelled\',record_revision=record_revision+1,updated_at=? WHERE id=?',(now,rid))
            self._increment(db,binding,revision)
            self._emit(db,binding,{'event':'research_record_deleted','record_id':rid,'kind':row['kind'],'record_revision':row['record_revision']+1,
                **({'reminder':{'enabled':False,'calendar':False,'message':False},'due_on':None} if row['kind']=='task' else {})})
            return {'contract':CONTRACT,**binding,'workspace_binding':w,'revision':revision+1,'id':rid,'deleted':True,'executed_bank_operations_retracted':False}

    def public(self,path,query):
        prefix='/api/finance/research/'
        if path==prefix+'catalog':
            if set(query)-{'q','kind','customer_group','limit','offset','capability'}:fail('INVALID_QUERY','研究目录查询参数无效。',400)
            params={k:v[0] for k,v in query.items()}
            for k in ('limit','offset'):
                if k in params:
                    if not re.fullmatch(r'\d{1,6}',params[k]):fail('INVALID_QUERY','分页参数无效。',400)
                    params[k]=int(params[k])
            revision=self._provider_call('revision')
            with self.evidence_guard(revision):raw=self._provider_call('catalog',params)
            if type(raw) is not dict:fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开目录结构无法核对。',503)
            return {**deepcopy(raw),'contract':CONTRACT,'data_scope':'public','provider_revision':revision,'execution_authority':False}
        refresh=query=={'refresh':['1']}
        if query and not refresh:fail('INVALID_QUERY','公开详情只接受显式refresh=1，不接受身份参数。',400)
        match=re.fullmatch(prefix+r'entities/([^/]+)(/documents)?',path)
        if not match:fail('NOT_FOUND','研究接口不存在。',404)
        entity=ident(unquote(match[1]))
        if refresh:
            if match[2]:fail('INVALID_QUERY','请从实体深度检查原件，文档列表保持轻读取。',400)
            return self.deep_refresh(entity)
        if match[2]:
            revision=self._provider_call('revision')
            with self.evidence_guard(revision):docs=self._provider_call('documents',entity)
            if type(docs) is not dict or docs.get('entity_id')!=entity or docs.get('provider_revision')!=revision or type(docs.get('items')) is not list:
                fail('RESEARCH_DEPENDENCY_UNAVAILABLE','公开文档实体或版本无法核对。',503)
            return {**deepcopy(docs),'contract':CONTRACT,'data_scope':'public','execution_authority':False}
        return self.public_detail(entity)

    @staticmethod
    def _json(handler,status,data):
        original=handler.end_headers
        def end():
            handler.send_header('Cache-Control','no-store')
            return original()
        handler.end_headers=end
        try:return handler._finance_json(status,data)
        finally:handler.end_headers=original

    @staticmethod
    def _upload_body(handler):
        lengths=handler.headers.get_all('Content-Length',[])
        if handler.headers.get('Transfer-Encoding') or len(lengths)!=1 or not re.fullmatch(r'\d{1,9}',lengths[0]):
            handler.close_connection=True;fail('RESEARCH_INVALID','原件上传需要单一有效Content-Length。',400)
        size=int(lengths[0])
        if not 0<size<=10*1024*1024:handler.close_connection=True;fail('UPLOAD_TOO_LARGE','原件不能超过10MiB。',413)
        old_timeout=handler.connection.gettimeout();handler.connection.settimeout(15)
        try:body=handler.rfile.read(size)
        finally:handler.connection.settimeout(old_timeout)
        if len(body)!=size:fail('RESEARCH_INVALID','上传未完成。',400)
        return body

    def dispatch(self,handler):
        from finance_gateway import request_json
        parsed=urlsplit(handler.path);path=parsed.path;public=path.endswith('/catalog') or '/entities/' in path
        try:
            self.g.origin_check(handler);query=parse_qs(parsed.query,keep_blank_values=True)
            if any(len(v)!=1 for v in query.values()):fail('INVALID_QUERY','查询参数不可重复。',400)
            if public:
                if handler.command!='GET':fail('METHOD_NOT_ALLOWED','公开研究接口仅供读取。',405)
                self._json(handler,200,self.public(path,query));return True
            pagination={}
            if query:
                if handler.command!='GET' or path not in ('/api/finance/research/workspace','/api/finance/research/policies','/api/finance/research/tasks') or set(query)-{'limit','offset'}:
                    fail('INVALID_QUERY','私人研究接口仅支持列表分页参数。',400)
                for k,v in query.items():
                    if not re.fullmatch(r'\d{1,6}',v[0]):fail('INVALID_QUERY','分页参数无效。',400)
                    pagination[k]=int(v[0])
            if getattr(handler,'finance_send_guard_contract',None)!='navren.finance-send-guard/1':fail('RESEARCH_SEND_GUARD_REQUIRED','研究记录缺少正式发送守卫。',503)
            w,token=self.g.identity(handler);handler._navren_workspace=w
            with self.g.demo_sessions.request(token,w):
                with self.g.bank.transaction() as bank:
                    self.binding(w,bank);start=privacy_revision(bank,w)
                handler._navren_start_revision=start;context=self._request_privacy.set(start)
                try:
                    prefix='/api/finance/research/'
                    match=re.fullmatch(prefix+r'records/([^/]+)(/versions|/delete|/operations)?',path)
                    attachment=re.fullmatch(prefix+r'attachments/([^/]+)(/delete)?',path)
                    operation=re.fullmatch(prefix+r'operations/([^/]+)',path)
                    if handler.command=='GET':
                        if path==prefix+'workspace':result=self.read(w,**pagination)
                        elif path==prefix+'policies':result=self.read(w,kind='policy',**pagination)
                        elif path==prefix+'tasks':result=self.read(w,kind='task',**pagination)
                        elif match and match[2] in (None,'/versions'):result=self.read(w,rid=unquote(match[1]),history=match[2]=='/versions')
                        elif operation:result=self.operation(w,unquote(operation[1]))
                        elif attachment and not attachment[2]:
                            result,body=self.attachment(w,unquote(attachment[1]))
                            with self.send_guard(w,result['revision']) as bank:
                                if privacy_revision(bank,w)!=start:fail('PRIVACY_REVISION_CHANGED','原件权限已变化。',409)
                                handler.send_response(200)
                                handler.send_header('Content-Type',result['attachment']['mime_type'])
                                handler.send_header('Content-Length',str(len(body)))
                                handler.send_header('Content-Disposition','attachment; filename="'+result['attachment']['id']+'"')
                                handler.send_header('X-Content-Type-Options','nosniff')
                                handler.send_header('Cache-Control','no-store')
                                handler.send_header('X-Navren-Workspace',w)
                                handler.send_header('X-Navren-Privacy-Revision',str(start))
                                handler.send_header('X-Navren-Content-SHA256',result['attachment']['sha256'])
                                handler.end_headers();handler.wfile.write(body)
                            return True
                        else:fail('NOT_FOUND','私人研究读取接口不存在。',404)
                    elif handler.command=='POST':
                        # 已确认当前会话后有界读取原始字节；尚不解释或保存原件。
                        # 否则提前4xx会在welcome发送大请求时造成BrokenPipe并掩盖真实拒绝。
                        upload_body=self._upload_body(handler) if path==prefix+'attachments' else None
                        self.g.check_csrf(handler,token)
                        if handler.headers.get('X-Navren-Workspace')!=w or handler.headers.get('X-Navren-Privacy-Revision')!=str(start):
                            fail('PRIVACY_REVISION_CHANGED','研究保存的工作区或资料权限已变化。',409)
                        if path==prefix+'attachments':
                            header=handler.headers.get('X-Navren-Expected-Revision','')
                            if not re.fullmatch(r'\d{1,12}',header):handler.close_connection=True;fail()
                            result=self.upload(w,upload_body,mime_type=handler.headers.get('Content-Type'),
                                upload_key=handler.headers.get('X-Navren-Upload-Key'),expected_revision=int(header),
                                confirmed=handler.headers.get('X-Navren-Confirmed')=='true',
                                health_data_authorized=handler.headers.get('X-Navren-Health-Consent')=='true')
                            payload=None
                        else:payload=request_json(handler)
                        if path==prefix+'attachments':pass
                        elif path==prefix+'workspace':result=self.save(w,payload)
                        elif path==prefix+'policies':result=self.save(w,payload,kind='policy')
                        elif path==prefix+'tasks':result=self.save(w,payload,kind='task')
                        elif path==prefix+'compare':result=self.compare(w,payload)
                        elif path==prefix+'calculate':result=self.calculate(w,payload)
                        elif path==prefix+'prepare':result=self.prepare(w,payload)
                        elif match and match[2]=='/operations':result=self.associate(w,unquote(match[1]),payload)
                        elif attachment and attachment[2]=='/delete':result=self.delete_attachment(w,unquote(attachment[1]),payload)
                        elif match and match[2]=='/delete':result=self.delete(w,unquote(match[1]),payload)
                        else:fail('NOT_FOUND','私人研究写入接口不存在。',404)
                    else:fail('METHOD_NOT_ALLOWED','研究接口仅支持GET或POST。',405)
                    handler._navren_finance_send_guard=lambda body:self.send_guard(w,result['revision'])
                    self._json(handler,200,result)
                finally:self._request_privacy.reset(context)
        except BankError as error:self._json(handler,error.status,{'error':error.as_dict()})
        return True
