"""用户日历元数据：周期、版本草案、事务导入及站内提醒。没有资金执行能力。"""
import calendar
import csv
import hashlib
import io
import json
import re
import uuid
from datetime import date, timedelta
from contextlib import closing
from decimal import Decimal, InvalidOperation
from simulated_bank import BankError
from simulated_bank.service import encoded, strict_fields

SCHEMA = '''
CREATE TABLE IF NOT EXISTS calendar_series(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
 external_id TEXT, fingerprint TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(workspace_id,external_id));
CREATE TABLE IF NOT EXISTS calendar_occurrence_overrides(
 workspace_id TEXT NOT NULL, series_id TEXT NOT NULL, original_on TEXT NOT NULL,
 payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(workspace_id,series_id,original_on));
CREATE TABLE IF NOT EXISTS calendar_event_drafts(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,payload TEXT NOT NULL,target_revision INTEGER,
 status TEXT NOT NULL,expires_at REAL NOT NULL,result TEXT,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS calendar_import_batches(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,fingerprint TEXT NOT NULL,payload TEXT NOT NULL,
 status TEXT NOT NULL,expires_at REAL NOT NULL,result TEXT);
CREATE TABLE IF NOT EXISTS calendar_reminder_receipts(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,series_id TEXT NOT NULL,occurrence_on TEXT NOT NULL,
 due_on TEXT NOT NULL,read_at REAL,UNIQUE(workspace_id,series_id,occurrence_on));
CREATE INDEX IF NOT EXISTS calendar_series_owner ON calendar_series(workspace_id);
'''
FIELDS = {'title','kind','start_on','recurrence','month_policy','budget_minor','currency','remind_days',
          'note','relation_label','end_on','linked_commitment_ids','linked_memory_ids','status'}
KINDS = {'birthday','life_event','bill','subscription'}


def day(raw):
    try:
        d=date.fromisoformat(raw)
        if d.isoformat()!=raw: raise ValueError()
        return d
    except (ValueError,TypeError): raise BankError('CALENDAR_DATE','日期必须是有效的公历 YYYY-MM-DD。')


def normalize(data):
    strict_fields(data,FIELDS,{'title','kind','start_on'})
    p = {'recurrence':'once','month_policy':'skip','remind_days':0,'currency':'CNY','note':'',
         'linked_commitment_ids':[],'linked_memory_ids':[],'status':'planned',**data}
    if p['kind'] not in KINDS or p['recurrence'] not in {'once','weekly','monthly','yearly'} or p['month_policy'] not in {'skip','last_day'}:
        raise BankError('CALENDAR_RULE','事件类型、周期或月份日期策略无效。')
    if p['status'] not in {'planned','completed','cancelled'}:
        raise BankError('CALENDAR_STATE','手工完成不能标记银行已支付。')
    for k,n in [('title',120),('note',1000),('relation_label',80)]:
        if k in p and (not isinstance(p[k],str) or len(p[k])>n or (k=='title' and not p[k].strip())):
            raise BankError('CALENDAR_TEXT','标题/备注过长或无效。')
    day(p['start_on'])
    if p.get('end_on') and day(p['end_on'])<day(p['start_on']): raise BankError('CALENDAR_DATE','结束日期不能早于开始日期。')
    if p['currency'] not in {'CNY','USD','EUR','GBP','JPY'}: raise BankError('CALENDAR_CURRENCY','币种不支持。')
    if p.get('budget_minor') is not None and (type(p['budget_minor']) is not int or not 0<=p['budget_minor']<=100000000):
        raise BankError('CALENDAR_AMOUNT','预算必须是0至100000000之间的整数分。')
    if type(p['remind_days']) is not int or not 0<=p['remind_days']<=366: raise BankError('CALENDAR_REMINDER','提前提醒须为0至366天。')
    if not isinstance(p['linked_commitment_ids'],list) or len(p['linked_commitment_ids'])>20 or any(not isinstance(x,str) or len(x)>160 for x in p['linked_commitment_ids']):
        raise BankError('CALENDAR_LINK','关联承诺格式无效。')
    if not isinstance(p['linked_memory_ids'],list) or len(p['linked_memory_ids'])>10 or any(not isinstance(x,str) or len(x)>64 for x in p['linked_memory_ids']):
        raise BankError('CALENDAR_LINK','关联记忆格式无效。')
    return p


def occurrences(p, begin, end):
    anchor=day(p['start_on']); stop=min(end,day(p['end_on'])) if p.get('end_on') else end
    if p['recurrence']=='once':
        if begin<=anchor<=stop: yield anchor
        return
    if p['recurrence']=='weekly':
        cursor=anchor+timedelta(days=max(0,((begin-anchor).days+6)//7)*7)
        while cursor<=stop: yield cursor;cursor+=timedelta(days=7)
        return
    cursor=max(anchor.year*12+anchor.month-1,begin.year*12+begin.month-1)
    while cursor<=stop.year*12+stop.month-1:
        year,m=divmod(cursor,12); m+=1
        if p['recurrence']!='yearly' or m==anchor.month:
            last=calendar.monthrange(year,m)[1]
            if anchor.day<=last or p['month_policy']=='last_day':
                d=date(year,m,min(anchor.day,last))
                if max(begin,anchor)<=d<=stop: yield d
        cursor+=1


class CalendarEvents:
    def __init__(self,gateway):
        self.g=gateway;self.bank=gateway.bank
        with closing(self.bank.connect()) as db: db.executescript(SCHEMA)

    def _series(self,db,w,sid):
        row=db.execute('SELECT * FROM calendar_series WHERE workspace_id=? AND id=?',(w,sid)).fetchone()
        if not row: raise BankError('CALENDAR_NOT_FOUND','日历事件不存在或不属于当前账户。',404)
        return row

    def _links(self,w,p,db,existing=()):
        ids=p.get('linked_commitment_ids',[])
        memories=p.get('linked_memory_ids',[])
        if not ids and not memories:return
        known={r[0] for table in ('loan_installments','credit_statements','subscriptions','mandates','scheduled_payments','fund_holds')
               for r in db.execute('SELECT id FROM '+table+' WHERE workspace_id=?',(w,))}
        if len(set(ids))!=len(ids) or set(ids)-known:
            raise BankError('CALENDAR_LINK','关联事项不属于当前账户或存在重复。')
        if memories:
            with self.g.memories.store.connection(w) as personal_db:
                setting=personal_db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?',(w,)).fetchone()
            if (not setting or not setting[0]) and set(memories)-set(existing):raise BankError('MEMORY_DISABLED','用户未开启记忆，不能新增画像关联。')
            as_of=db.execute('SELECT as_of FROM workspaces WHERE id=?',(w,)).fetchone()[0]
            with self.g.memories.store.connection(w) as personal_db:
                known_memories={r[0] for r in personal_db.execute('SELECT id FROM user_memories WHERE workspace_id=? AND user_confirmed=1 AND (expires_on IS NULL OR expires_on>=?)',(w,as_of))}
            # 已保存的关联只保留引用；停用、到期或删除画像不阻止编辑日历本身。
            if len(set(memories))!=len(memories) or set(memories)-set(existing)-known_memories:raise BankError('CALENDAR_LINK','只能新增关联当前用户已确认且未到期的资料。')

    def list(self,w,begin,end):
        with self.bank.transaction() as db:
            rows=db.execute("SELECT * FROM calendar_series WHERE workspace_id=? AND status='active'",(w,)).fetchall()
            overrides={(r['series_id'],r['original_on']):json.loads(r['payload']) for r in db.execute('SELECT * FROM calendar_occurrence_overrides WHERE workspace_id=?',(w,))}
        items=[]
        for row in rows:
            p=json.loads(row['payload']); sid=row['id']
            dates={d.isoformat() for d in occurrences(p,begin,end)}
            dates.update(k[1] for k,v in overrides.items() if k[0]==sid and v.get('start_on') and begin<=day(v['start_on'])<=end)
            for original in sorted(dates):
                override=overrides.get((sid,original),{}); entry={**p,**override}; actual=entry.get('start_on') if override.get('start_on') else original
                if not begin<=day(actual)<=end: continue
                items.append({'id':f'life:{sid}:{original}','occurrence_id':f'{sid}:{original}',
                    'series_id':sid,'original_on':original,'date':actual,'scheduled_date':original,
                    'title':entry['title'],'kind':entry['kind'],'currency':entry['currency'],
                    'budget_minor':entry.get('budget_minor'),'cash_delta_minor':None,'amount_minor':None,
                    'state':entry['status'],'source_type':'user_calendar','source_ref':sid,
                    'source_revision':row['revision'],'payload':entry,'time_precision':'date','timezone':'Asia/Shanghai',
                    'linked_commitment_ids':entry['linked_commitment_ids'],'linked_memory_ids':entry.get('linked_memory_ids',[]),
                    'editable_fields':sorted(FIELDS),'allowed_actions':['edit','delete','complete']})
        return items

    def prepare(self,w,data,agent=False):
        strict_fields(data,{'action','series_id','original_on','scope','event'},{'action'})
        if data['action'] not in {'create','edit','delete'}: raise BankError('CALENDAR_ACTION','事件操作无效。')
        action=data['action']; normalized=dict(data); normalized.setdefault('scope','series')
        if normalized['scope'] not in {'occurrence','future','series'}: raise BankError('CALENDAR_SCOPE','请选择仅这次、这次及以后或整个系列。')
        revision=None;before=None
        with self.bank.transaction() as db:
            if action=='create': normalized['event']=normalize(data.get('event',{}))
            else:
                row=self._series(db,w,data.get('series_id')); revision=row['revision']; before=json.loads(row['payload'])
                if row['status']!='active': raise BankError('CALENDAR_DELETED','事件系列已删除。',409)
                if normalized['scope']!='series':
                    original=day(data.get('original_on'))
                    if not list(occurrences(before,original,original)): raise BankError('CALENDAR_INSTANCE','该日期不是原系列中的实例。')
                    if normalized['scope']=='occurrence':
                        override=db.execute('SELECT payload FROM calendar_occurrence_overrides WHERE workspace_id=? AND series_id=? AND original_on=?',(w,row['id'],data['original_on'])).fetchone()
                        before={**before,'start_on':data['original_on'],**(json.loads(override[0]) if override else {})}
                if action=='edit':
                    strict_fields(data.get('event',{}),FIELDS)
                    normalized['event']=normalize({**before,**data['event']})
            did=uuid.uuid4().hex
            if normalized.get('event'):self._links(w,normalized['event'],db,(before or {}).get('linked_memory_ids',[]))
            if agent:
                db.execute("UPDATE calendar_event_drafts SET status='superseded' WHERE workspace_id=? AND status IN ('ready','pending_review')",(w,))
            db.execute('INSERT INTO calendar_event_drafts VALUES(?,?,?,?,?,?,?,?)',
                (did,w,encoded(normalized),revision,'pending_review' if agent else 'ready',self.bank.clock()+600,None,self.bank.clock()))
        return {'draft_id':did,'action':action,'scope':normalized['scope'],'before':before,
                'after':normalized.get('event'),'series_id':data.get('series_id'),'original_on':data.get('original_on'),
                'expires_at':self.bank.clock()+600,'notice':'仅保存日历元数据；预算不扣款，完成不证明支付。'}

    def release(self,w,ids,approved):
        with self.bank.transaction() as db:
            for did in ids: db.execute("UPDATE calendar_event_drafts SET status=? WHERE id=? AND workspace_id=? AND status='pending_review'",('ready' if approved else 'rejected',did,w))

    def confirm(self,w,did,data):
        strict_fields(data,{'confirmed'},{'confirmed'})
        if data['confirmed'] is not True: raise BankError('CONFIRMATION_REQUIRED','请核对日历草案后确认。')
        with self.bank.transaction() as db:
            row=db.execute('SELECT * FROM calendar_event_drafts WHERE id=? AND workspace_id=?',(did,w)).fetchone()
            if not row: raise BankError('CALENDAR_NOT_FOUND','草案不存在。',404)
            if row['status']=='committed': return json.loads(row['result'])
            if self.bank.clock()>=row['expires_at']: raise BankError('CALENDAR_EXPIRED','草案已过期，请重新准备。',409)
            if row['status']!='ready': raise BankError('CALENDAR_REVIEW_REQUIRED','草案尚未放行。',409)
            p=json.loads(row['payload']);sid=p.get('series_id')
            if p['action']!='create':
                target=self._series(db,w,sid)
                if target['revision']!=row['target_revision'] or target['status']!='active': raise BankError('CALENDAR_STALE','事件已变化，请重新准备。',409)
            if p.get('event'):self._links(w,p['event'],db,json.loads(target['payload']).get('linked_memory_ids',[]) if p['action']!='create' else [])
            if p['action']=='create': sid=self._insert(db,w,p['event'])
            elif p['scope']=='occurrence':
                payload=p.get('event',{}) if p['action']=='edit' else {'status':'cancelled'}
                if p['action']=='edit' and p['event']['start_on']==json.loads(target['payload'])['start_on']:
                    payload={**payload,'start_on':p['original_on']}
                db.execute('INSERT INTO calendar_occurrence_overrides VALUES(?,?,?,?,1) ON CONFLICT(workspace_id,series_id,original_on) DO UPDATE SET payload=excluded.payload,revision=revision+1',(w,sid,p['original_on'],encoded(payload)))
                db.execute('UPDATE calendar_series SET revision=revision+1,updated_at=? WHERE id=? AND workspace_id=?',(self.bank.clock(),sid,w))
            elif p['scope']=='future':
                old=json.loads(target['payload']);old['end_on']=(day(p['original_on'])-timedelta(days=1)).isoformat()
                db.execute('UPDATE calendar_series SET payload=?,revision=revision+1,updated_at=? WHERE id=? AND workspace_id=?',(encoded(old),self.bank.clock(),sid,w))
                if p['action']=='edit':
                    nxt={**p['event'],'start_on':p['event']['start_on'] if p['event']['start_on']!=json.loads(target['payload'])['start_on'] else p['original_on']}
                    nxt.pop('end_on',None);sid=self._insert(db,w,nxt)
            else:
                if p['action']=='delete': db.execute("UPDATE calendar_series SET status='deleted',revision=revision+1 WHERE id=? AND workspace_id=?",(sid,w))
                else:
                    db.execute('UPDATE calendar_series SET payload=?,fingerprint=?,revision=revision+1,updated_at=? WHERE id=? AND workspace_id=?',(encoded(p['event']),hashlib.sha256(encoded(p['event']).encode()).hexdigest(),self.bank.clock(),sid,w))
                    db.execute('DELETE FROM calendar_occurrence_overrides WHERE series_id=? AND workspace_id=?',(sid,w))
            result={'status':'committed','series_id':sid,'draft_id':did,'financial_effect':False}
            db.execute("UPDATE calendar_event_drafts SET status='committed',result=? WHERE id=?",(encoded(result),did))
            self.bank._audit(db,w,'calendar_event_confirmed',{'draft_id':did,'action':p['action']})
            return result

    def _insert(self,db,w,p,external=None):
        sid=uuid.uuid4().hex;fp=hashlib.sha256(encoded(p).encode()).hexdigest()
        db.execute('INSERT INTO calendar_series VALUES(?,?,?,1,?,?,?,?,?)',(sid,w,encoded(p),'active',external,fp,self.bank.clock(),self.bank.clock()))
        return sid

    def preview(self,w,text):
        if not isinstance(text,str) or not 0<len(text.encode())<=1572864: raise BankError('CSV_SIZE','文件必须为UTF-8，且不超过1.5MB。')
        headers=['外部编号','标题','类型','日期','重复周期','月份日期策略','预算金额','币种','提前提醒天数','备注']
        reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
        if reader.fieldnames!=headers: raise BankError('CSV_HEADER','请使用生活事件中文模板。')
        rows=[];errors=[];seen=set()
        with self.bank.transaction() as db:
            for i,r in enumerate(reader,2):
                if i>1001: raise BankError('CSV_ROWS','最多1000个事件。')
                try:
                    if None in r or any(v is None for v in r.values()): raise ValueError('列数不一致')
                    ext=r['外部编号'].strip() or None
                    if ext and (len(ext)>120 or ext in seen): raise ValueError('外部编号过长或本批重复')
                    if ext: seen.add(ext)
                    p={'title':r['标题'],'kind':{'生日':'birthday','生活事件':'life_event','固定开支':'bill','订阅':'subscription'}.get(r['类型']),
                       'start_on':r['日期'],'recurrence':{'单次':'once','每周':'weekly','每月':'monthly','每年':'yearly'}.get(r['重复周期']),
                       'month_policy':{'跳过该期':'skip','当月最后一天':'last_day'}.get(r['月份日期策略']),
                       'currency':r['币种'] or 'CNY','remind_days':int(r['提前提醒天数'] or '0'),'note':r['备注']}
                    if r['预算金额'].strip():
                        if not re.fullmatch(r'\d+(?:\.\d{1,2})?',r['预算金额'].strip()): raise ValueError('金额格式无效')
                        p['budget_minor']=int(Decimal(r['预算金额'].strip())*100)
                    p=normalize(p);self._links(w,p,db);fp=hashlib.sha256(encoded(p).encode()).hexdigest()
                    old=db.execute('SELECT * FROM calendar_series WHERE workspace_id=? AND '+('external_id=?' if ext else 'fingerprint=?'),(w,ext or fp)).fetchone()
                    op='duplicate' if old and old['fingerprint']==fp else 'update' if old else 'create'
                    rows.append({'row':i,'event':p,'external_id':ext,'fingerprint':fp,'action':op,
                                 'target_id':old['id'] if old else None,'target_revision':old['revision'] if old else None,
                                 'before':json.loads(old['payload']) if old else None})
                except (BankError,ValueError,InvalidOperation) as error: errors.append({'row':i,'message':str(error)})
            if not rows and not errors: errors.append({'row':1,'message':'文件没有事件'})
            bid=uuid.uuid4().hex;payload={'rows':rows,'errors':errors}
            db.execute('INSERT INTO calendar_import_batches VALUES(?,?,?,?,?,?,NULL)',(bid,w,hashlib.sha256(text.encode()).hexdigest(),encoded(payload),'preview',self.bank.clock()+600))
        return {'batch_id':bid,**payload,'can_commit':not errors,'expires_at':self.bank.clock()+600}

    def commit(self,w,p):
        strict_fields(p,{'batch_id','confirmed'},{'batch_id','confirmed'})
        if p['confirmed'] is not True: raise BankError('CONFIRMATION_REQUIRED','导入需要确认。')
        with self.bank.transaction() as db:
            batch=db.execute('SELECT * FROM calendar_import_batches WHERE id=? AND workspace_id=?',(p['batch_id'],w)).fetchone()
            if not batch: raise BankError('CALENDAR_NOT_FOUND','导入批次不存在。',404)
            if batch['status']=='committed': return json.loads(batch['result'])
            if self.bank.clock()>=batch['expires_at']: raise BankError('CALENDAR_EXPIRED','预览已过期。',409)
            data=json.loads(batch['payload'])
            if data['errors']: raise BankError('CSV_ERRORS','本批有错误，整批未提交。',409)
            for r in data['rows']:
                if r['target_id']:
                    old=self._series(db,w,r['target_id'])
                    if old['revision']!=r['target_revision']: raise BankError('CALENDAR_STALE','预览后事件变化，整批未提交。',409)
                elif r['external_id'] and db.execute('SELECT 1 FROM calendar_series WHERE workspace_id=? AND external_id=?',(w,r['external_id'])).fetchone():
                    raise BankError('CALENDAR_STALE','外部编号已被使用，整批未提交。',409)
            counts={'create':0,'update':0,'duplicate':0}
            for r in data['rows']:
                if r['action']=='create':
                    # 无外部编号的并发预览也按内容去重。
                    found=db.execute('SELECT 1 FROM calendar_series WHERE workspace_id=? AND fingerprint=?',(w,r['fingerprint'])).fetchone()
                    if found and not r['external_id']: counts['duplicate']+=1;continue
                    self._insert(db,w,r['event'],r['external_id'])
                elif r['action']=='update':
                    db.execute("UPDATE calendar_series SET payload=?,fingerprint=?,revision=revision+1,status='active',updated_at=? WHERE id=? AND workspace_id=?",(encoded(r['event']),r['fingerprint'],self.bank.clock(),r['target_id'],w))
                    db.execute('DELETE FROM calendar_occurrence_overrides WHERE series_id=? AND workspace_id=?',(r['target_id'],w))
                counts[r['action']]+=1
            result={'status':'committed','counts':counts,'financial_effect':False}
            db.execute("UPDATE calendar_import_batches SET status='committed',result=? WHERE id=?",(encoded(result),batch['id']))
            self.bank._audit(db,w,'calendar_import_confirmed',counts)
            return result

    def reminders(self,w,as_of):
        today=day(as_of);items=self.list(w,today-timedelta(days=366),today+timedelta(days=366));result=[]
        with self.bank.transaction() as db:
            for e in items:
                if e['state']!='planned': continue
                due=day(e['date'])-timedelta(days=e['payload']['remind_days'])
                if not due<=today<=day(e['date']): continue
                rid=hashlib.sha256((w+e['id']).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO calendar_reminder_receipts VALUES(?,?,?,?,?,NULL)',(rid,w,e['series_id'],e['original_on'],due.isoformat()))
                receipt=db.execute('SELECT read_at FROM calendar_reminder_receipts WHERE id=? AND workspace_id=?',(rid,w)).fetchone()
                if receipt['read_at'] is None: result.append({'id':rid,'item':e,'due_on':due.isoformat(),'channel':'in_app'})
        return {'as_of':as_of,'items':result,'notice':'仅在应用打开或查询时计算站内提醒，不提供应用关闭后的推送。'}

    def read_reminder(self,w,rid):
        with self.bank.transaction() as db:
            if not db.execute('SELECT 1 FROM calendar_reminder_receipts WHERE id=? AND workspace_id=?',(rid,w)).fetchone(): raise BankError('CALENDAR_NOT_FOUND','提醒不存在。',404)
            db.execute('UPDATE calendar_reminder_receipts SET read_at=COALESCE(read_at,?) WHERE id=? AND workspace_id=?',(self.bank.clock(),rid,w))
        return {'status':'read'}
