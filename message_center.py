"""本人持久化站内收件箱。确定性来源投影，不调用模型或执行金融操作。"""
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import re
import threading
from zoneinfo import ZoneInfo

from simulated_bank.service import BankError, encoded, strict_fields
from cashflow_timeline import build_cashflow_timeline
from calendar_events import occurrences, FIELDS

TZ = ZoneInfo('Asia/Shanghai')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS navren_messages(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 event_key TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, due_at TEXT,
 read_at REAL, status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled','declined','info')),
 amount_minor INTEGER, currency TEXT, counterparty TEXT, certainty TEXT NOT NULL,
 action TEXT, source TEXT NOT NULL, UNIQUE(workspace_id,event_key));
CREATE INDEX IF NOT EXISTS navren_messages_owner ON navren_messages(workspace_id,created_at,id);
'''


def initialize(db):
    # 单条execute保留调用者现有事务；不能在AA结算中executescript隐式提交。
    for statement in SCHEMA.split(';'):
        if statement.strip(): db.execute(statement)


def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, TZ).isoformat(timespec='seconds')


def _upsert(db, workspace, key, *, kind, title, body, created_at, status='info',
            category='due', due_at=None, amount_minor=None, currency=None,
            counterparty=None, certainty='confirmed', action=None, source=None, read_at=None):
    mid = sha256((workspace + '\0' + key).encode()).hexdigest()[:32]
    db.execute('''INSERT INTO navren_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(workspace_id,event_key) DO UPDATE SET
        category=excluded.category,kind=excluded.kind,title=excluded.title,body=excluded.body,
        due_at=excluded.due_at,status=excluded.status,amount_minor=excluded.amount_minor,
        currency=excluded.currency,counterparty=excluded.counterparty,certainty=excluded.certainty,
        action=excluded.action,source=excluded.source,
        read_at=COALESCE(navren_messages.read_at,excluded.read_at)''',
        (mid, workspace, key, category, kind, title, body, created_at, due_at, read_at, status,
         amount_minor, currency, encoded(counterparty) if counterparty else None, certainty,
         encoded(action) if action else None, encoded(source or {})))
    return mid


def sync_aa_bill(bank, db, bill_id, *, only_workspace=None):
    """在同一AA金融事务内调用；写入异常交由上层回滚。启动也可从账单重放。"""
    bill = db.execute('SELECT * FROM navren_aa_bills WHERE id=?', (bill_id,)).fetchone()
    if not bill: return
    now = bank.clock()
    creator = {'user_id': bill['creator_user_id'], 'display_name': bill['creator_name']}
    for share in db.execute('SELECT * FROM navren_aa_shares WHERE bill_id=?', (bill_id,)).fetchall():
        w = share['workspace_id']
        if share['status'] == 'own' or only_workspace and w != only_workspace: continue
        status = {'paid':'completed','declined':'declined','cancelled':'cancelled'}.get(share['status'], 'pending')
        expired = status == 'pending' and bill['deadline'] is not None and now >= bill['deadline']
        body = {
            'pending': f'{bill["creator_name"]}发起了“{bill["title"]}”。请查看本人份额，付款仍需本人确认。',
            'completed': f'“{bill["title"]}”的本人份额已支付，详情可查看回执。',
            'declined': f'本人已拒绝“{bill["title"]}”的分摊请求，未扣款。',
            'cancelled': f'“{bill["title"]}”的未付请求已取消，未扣款。',
            'info': f'“{bill["title"]}”已过支付截止时间，未记录付款；请查看当前账单。'
        }[status]
        if expired:
            body=f'“{bill["title"]}”已过支付截止时间，份额仍未处理，付款入口已关闭；请查看当前账单。'
        action = {'type':'aa_bill','object_id':bill_id,'share_id':share['id'],'label':'查看账单'}
        _upsert(db,w,'aa-request:'+share['id'],category='payments',kind='aa_request',
                title='AA分摊 · '+bill['title'],body=body,created_at=bill['created_at'],
                status=status,due_at=bill['due_at'],amount_minor=share['share_minor'],currency='CNY',
                counterparty=creator,action=action,source={'type':'aa','bill_id':bill_id})
        if share['status'] in ('declined','cancelled'):
            _upsert(db,w,'aa-'+share['status']+':'+share['id'],category='payments',
                    kind='aa_'+share['status'],title='AA请求已处理',body=body,created_at=share['updated_at'],
                    status=status,amount_minor=share['share_minor'],currency='CNY',counterparty=creator,
                    action=action,source={'type':'aa','bill_id':bill_id})
    receipts = db.execute('SELECT * FROM navren_aa_receipts WHERE bill_id=?', (bill_id,)).fetchall()
    for receipt in receipts:
        for side, other, title, verb in [('payer','recipient','AA付款成功','已向'),('recipient','payer','收到AA分摊款','已收到')]:
            w = receipt[side+'_workspace']
            if only_workspace and w != only_workspace: continue
            cp = {'user_id':receipt[other+'_user_id'],'display_name':receipt[other+'_name']}
            _upsert(db,w,'aa-payment:'+receipt['id']+':'+side,category='payments',kind='aa_'+side,
                    title=title,body=verb+cp['display_name']+'的“'+bill['title']+'”分摊款，详情可查看回执。' if side=='recipient' else
                    '已向'+cp['display_name']+'支付“'+bill['title']+'”的本人份额，详情可查看回执。',
                    created_at=receipt['paid_at'],status='completed',amount_minor=receipt['amount_minor'],currency='CNY',
                    counterparty=cp,action={'type':'aa_bill','object_id':bill_id,'receipt_id':receipt['id'],'label':'查看回执'},
                    source={'type':'aa','bill_id':bill_id})
    w = bill['creator_workspace']
    if not only_workspace or w == only_workspace:
        for share in db.execute("SELECT * FROM navren_aa_shares WHERE bill_id=? AND status='declined'", (bill_id,)).fetchall():
            _upsert(db,w,'aa-declined-owner:'+share['id'],category='payments',kind='aa_declined',
                    title='AA参与人已拒绝',body=share['display_name']+'已拒绝“'+bill['title']+'”的分摊请求，未收款。',
                    created_at=share['updated_at'],status='declined',amount_minor=share['share_minor'],currency='CNY',
                    counterparty={'user_id':share['user_id'],'display_name':share['display_name']},
                    action={'type':'aa_bill','object_id':bill_id,'label':'查看账单'},source={'type':'aa','bill_id':bill_id})
        if bill['status'] == 'cancelled':
            _upsert(db,w,'aa-cancelled-owner:'+bill_id,category='payments',kind='aa_cancelled',
                    title='AA账单已取消',body='“'+bill['title']+'”的未付请求已取消，已付回执保留，不自动退款。',
                    created_at=bill['updated_at'],status='cancelled',
                    action={'type':'aa_bill','object_id':bill_id,'label':'查看账单'},source={'type':'aa','bill_id':bill_id})


class _Stopped(Exception): pass


class MessageCenter:
    interval_seconds = 60
    def __init__(self, gateway):
        self.g = gateway
        self.bank = gateway.bank
        with closing(self.bank.connect()) as db, db: initialize(db)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._cycles = 0
        self._last_attempt = self._last_success = None
        self._last_error = None

    def _reference(self, db, w, item, source='simulation'):
        ref = {'date':item['date'],'source':source,'simulation_overlay':False,
               'portfolio':'current','item':item}
        db.execute('INSERT INTO calendar_items VALUES(?,?,?) ON CONFLICT(id,workspace_id) DO UPDATE SET reference=excluded.reference',
                   (item['id'],w,encoded(ref)))
        return {'type':'calendar','object_id':item['id'],'date':item['date'],'label':'查看事项',
                **({'occurrence_id':item['occurrence_id']} if 'occurrence_id' in item else {})}

    def _calendar(self, db, w, today, *, background=False, calendar_source='simulation'):
        series = {r['id']:dict(r) for r in db.execute('SELECT * FROM calendar_series WHERE workspace_id=?',(w,))}
        overrides = {(r['series_id'],r['original_on']):json.loads(r['payload']) for r in
                     db.execute('SELECT * FROM calendar_occurrence_overrides WHERE workspace_id=?',(w,))}
        existing = db.execute("SELECT event_key,source FROM navren_messages WHERE workspace_id=? AND kind='calendar_reminder'",(w,)).fetchall()
        old_dates = {}; old_keys = {}
        for row in existing:
            source = json.loads(row['source'])
            old_dates.setdefault(source['series_id'],set()).add(source['original_on'])
            old_keys.setdefault(source['series_id'],[]).append(row['event_key'])
        for sid, row in series.items():
            if background and self._stop.is_set(): raise _Stopped()
            p = json.loads(row['payload'])
            # 一次事项追溯原日期；周期启动补查一年，已生成的旧期次仍逐条重验。
            dates = set(old_dates.get(sid, ()))
            once_key=old_keys[sid][0] if p['recurrence']=='once' and len(old_keys.get(sid,[]))==1 else None
            if row['status'] == 'active':
                begin = date.fromisoformat(p['start_on']) if p['recurrence']=='once' else today-timedelta(days=366)
                dates.update(d.isoformat() for d in occurrences(p,begin,today+timedelta(days=p['remind_days'])))
                dates.update(k[1] for k in overrides if k[0]==sid)
                if once_key:dates.add(p['start_on'])
            for original in sorted(dates):
                if background and self._stop.is_set(): raise _Stopped()
                override = overrides.get((sid,original),{})
                if once_key and row['status']=='active' and original!=p['start_on'] and not override:
                    continue  # 一次事项的系列改日期仍是同一事项，用真实新original_on更新原消息。
                entry = {**p,**override}
                actual = override.get('start_on') or original
                on = date.fromisoformat(actual)
                key = once_key or 'calendar:'+sid+':'+original
                known = bool(once_key) or original in old_dates.get(sid, ())
                if not known and (row['status']!='active' or entry['status']!='planned' or on-timedelta(days=entry['remind_days'])>today): continue
                # 系列改周期/起点后不再有效的旧期次也必须结束，不能一直待处理。
                valid = any(d.isoformat()==original for d in occurrences(p,date.fromisoformat(original),date.fromisoformat(original)))
                status = 'cancelled' if row['status']=='deleted' or not valid else {'completed':'completed','cancelled':'cancelled'}.get(entry['status'],'pending')
                body = {'pending':'本人保存的日历事项'+('已过日期，尚未标记完成。' if on<today else '进入提醒期，请查看安排。'),
                        'completed':'日历事项已手工标记完成；该标记不代表已支付或已扣款。',
                        'cancelled':'日历事项已删除、取消或原期次已被修改。'}[status]
                if status=='pending' and on-timedelta(days=entry['remind_days'])>today:
                    body='原日历事项的提醒日期已调整，请查看当前安排；尚未标记完成。'
                item = {'id':f'life:{sid}:{original}','occurrence_id':f'{sid}:{original}',
                        'series_id':sid,'original_on':original,'date':actual,'scheduled_date':original,
                        'title':entry['title'],'kind':entry['kind'],'currency':entry['currency'],
                        'budget_minor':entry.get('budget_minor'),'amount_minor':None,'cash_delta_minor':None,
                        'state':entry['status'] if status!='cancelled' else 'cancelled','source_type':'user_calendar',
                        'source_ref':sid,'source_revision':row['revision'],'payload':entry,
                        'time_precision':'date','timezone':'Asia/Shanghai','linked_commitment_ids':entry['linked_commitment_ids'],
                        'linked_memory_ids':entry.get('linked_memory_ids',[]),
                        'editable_fields':sorted(FIELDS),'allowed_actions':['edit','delete','complete'] if status!='cancelled' else []}
                rid = sha256((w+item['id']).encode()).hexdigest()
                due = (on-timedelta(days=entry['remind_days'])).isoformat()
                db.execute('INSERT INTO calendar_reminder_receipts VALUES(?,?,?,?,?,NULL) ON CONFLICT(workspace_id,series_id,occurrence_on) DO UPDATE SET due_on=excluded.due_on',
                           (rid,w,sid,original,due))
                receipt = db.execute('SELECT read_at FROM calendar_reminder_receipts WHERE id=? AND workspace_id=?',(rid,w)).fetchone()
                mid = _upsert(db,w,key,kind='calendar_reminder',title=entry['title'],body=body,
                        created_at=self.bank.clock(),due_at=actual,status=status,certainty='user_defined',
                        action=self._reference(db,w,item,calendar_source),source={'type':'calendar','series_id':sid,'original_on':original,'receipt_id':rid},
                        read_at=receipt['read_at'])
                # 从消息页已读向旧日历入口同步；两者后续都不重置首次read_at。
                db.execute('UPDATE calendar_reminder_receipts SET read_at=COALESCE(read_at,(SELECT read_at FROM navren_messages WHERE id=? AND workspace_id=?)) WHERE id=? AND workspace_id=?',(mid,w,rid,w))
        for sid, dates in old_dates.items():
            if sid not in series:
                for original in dates:
                    db.execute("UPDATE navren_messages SET status='cancelled',body='原日历来源已删除。' WHERE workspace_id=? AND event_key=?",(w,'calendar:'+sid+':'+original))

    def _due(self, db, w, today, as_of, *, background=False):
        tables = ('loans','loan_installments','cards','credit_statements','subscriptions','mandates',
                  'beneficiaries','scheduled_payments','orders','fund_holds')
        records = {t:[dict(r) for r in db.execute('SELECT * FROM '+t+' WHERE workspace_id=?',(w,))] for t in tables}
        records['operations'] = []  # 草案绝不转换为已发送提醒。
        accounts = self.bank._accounts(db,w)
        archive = {'as_of':as_of,'accounts':accounts,'profile':self.bank._profile(db,w),'records':records}
        timeline = build_cashflow_timeline(archive,3,w)
        events = [e for slot in timeline['timeline'] for e in slot['events'] if e['kind']!='reservation_release']
        current = set()
        for e in events:
            if background and self._stop.is_set(): raise _Stopped()
            key = 'due:'+e['id']; current.add(key)
            candidate = e['certainty']=='candidate'
            body = ('这是现有合同/计划的候选日期，未确认、未记录本期扣款，不会因提醒自动扣款。' if candidate else
                    '这是现有账本的待办/计划日期，提醒不代表已支付或已到账，请核对当前事项。')
            if e['scheduled_date']<as_of: body='原日期已过。'+body
            kind = 'subscription' if candidate else 'settlement' if e['kind']=='redemption_settlement' else 'bill'
            item = {'id':'simulation:future:'+e['id'],'date':e['scheduled_date'],'scheduled_date':e['scheduled_date'],
                    'kind':kind,'title':e['title'],'currency':e['currency'],'amount_minor':e['amount_minor'],
                    'cash_delta_minor':e['cash_delta_minor'],'state':'candidate' if candidate else 'planned',
                    'source_type':'simulation','source_ref':e['evidence_ref'],'evidence_ids':[e['evidence_id']],
                    'linked_commitment_ids':[e['evidence_id']],'allowed_actions':['ask'],'editable_fields':[]}
            _upsert(db,w,key,kind=e['kind'],title=e['title'],body=body,created_at=self.bank.clock(),
                    due_at=e['scheduled_date'],status='pending',amount_minor=e['amount_minor'],currency=e['currency'],
                    certainty=e['certainty'],action=self._reference(db,w,item),
                    source={'type':'due','table':e['source_table'],'id':e['evidence_id'],'on':e['scheduled_date']})
        maps = {t:{r['id']:r for r in records[t]} for t in tables}
        for old in db.execute("SELECT * FROM navren_messages WHERE workspace_id=? AND json_extract(source,'$.type')='due'",(w,)).fetchall():
            if background and self._stop.is_set(): raise _Stopped()
            if old['event_key'] in current: continue
            s = json.loads(old['source']); row = maps.get(s['table'],{}).get(s['id'])
            status, body = 'info','原来源或期次已变化；未据此认定已扣款，请查看当前来源。'
            if not row or row.get('status') in ('cancelled','closed') or row.get('merchant_status')=='cancelled':
                status,body='cancelled','原待办来源已取消或关闭，历史提醒保留。'
            elif s['table']=='loan_installments' and (row['status']=='paid' or row['paid_minor']>=row['principal_minor']+row['interest_minor']+row['fees_minor']):
                status,body='completed','账本已记录本期还款，历史提醒保留。'
            elif s['table']=='credit_statements' and row['paid_minor']>=row['total_due_minor']:
                status,body='completed','账本已记录本期账单偿还，历史提醒保留。'
            elif s['table']=='orders' and row['status']=='settled':
                status,body='completed','原赎回订单已结算，历史提醒保留。'
            elif s['table']=='scheduled_payments' and row['status']=='completed':
                status,body='completed','原定时转账来源已标记完成，请查看对应账本记录。'
            db.execute('UPDATE navren_messages SET status=?,body=? WHERE workspace_id=? AND id=?',(status,body,w,old['id']))
        # 当前持仓的实际锁定日期，解锁本身不等于赎回/到账，不构造现金流。
        holdings = [dict(r) for r in db.execute('SELECT * FROM holdings WHERE workspace_id=?',(w,))]
        currencies = {a['id']:a['currency'] for a in accounts}
        active = set()
        for h in holdings:
            if background and self._stop.is_set(): raise _Stopped()
            on = h.get('locked_until')
            if not on or h['amount_minor']<=0 or on> (today+timedelta(days=3)).isoformat(): continue
            key='maturity:'+h['product_id']+':'+h['account_id']+':'+on;active.add(key)
            item={'id':'simulation:'+key,'date':on,'kind':'maturity','title':h['product_id']+' 解锁日期',
                  'currency':currencies[h['account_id']],'amount_minor':h['amount_minor'],'cash_delta_minor':None,
                  'state':'planned','source_type':'simulation','source_ref':h['product_id'],
                  'allowed_actions':['ask'],'editable_fields':[],'notice':'解锁不等于自动赎回或现金到账。'}
            _upsert(db,w,key,kind='holding_maturity',title=item['title'],body=item['notice'],created_at=self.bank.clock(),
                    due_at=on,status='info',amount_minor=h['amount_minor'],currency=item['currency'],
                    action=self._reference(db,w,item),source={'type':'maturity'})
        for old in db.execute("SELECT id,event_key FROM navren_messages WHERE workspace_id=? AND kind='holding_maturity'",(w,)).fetchall():
            if old['event_key'] not in active:
                db.execute("UPDATE navren_messages SET status='info',body='该原持仓来源已变化；历史解锁提醒不代表已到账。' WHERE id=? AND workspace_id=?",(old['id'],w))

    def _sync(self, db, w, *, background=False):
        as_of = self.bank._workspace(db,w)['as_of']
        bills = db.execute('''SELECT id FROM navren_aa_bills WHERE creator_workspace=? UNION
            SELECT bill_id FROM navren_aa_shares WHERE workspace_id=?''',(w,w)).fetchall()
        for row in bills:
            if background and self._stop.is_set(): raise _Stopped()
            sync_aa_bill(self.bank,db,row[0],only_workspace=w)
        calendar_on,calendar_source=self._calendar_clock(db,w,as_of)
        self._calendar(db,w,date.fromisoformat(calendar_on),background=background,calendar_source=calendar_source)
        if background and self._stop.is_set(): raise _Stopped()
        self._due(db,w,date.fromisoformat(as_of),as_of,background=background)
        self._goals(db,w,date.fromisoformat(as_of),background=background)
        if background and self._stop.is_set(): raise _Stopped()
        return as_of

    def _calendar_clock(self, db, w, bank_as_of):
        # 与FinancialCalendar.clock同一来源语义；复用当前事务快照，避免嵌套写锁。
        selected=db.execute('SELECT source FROM finance_data_sources WHERE workspace_id=?',(w,)).fetchone()
        source=selected['source'] if selected else 'simulation'
        on=bank_as_of if source=='simulation' else datetime.fromtimestamp(self.bank.clock(),TZ).date().isoformat()
        return on,source

    def _goals(self, db, w, today, *, background=False):
        from goal_service import GoalService
        rows={r['id']:dict(r) for r in db.execute('SELECT * FROM wealth_goals WHERE workspace_id=?',(w,))}
        previous={json.loads(r['source'])['goal_id']:r for r in db.execute("SELECT * FROM navren_messages WHERE workspace_id=? AND kind='goal_deadline'",(w,))}
        for gid, row in rows.items():
            if background and self._stop.is_set():raise _Stopped()
            on=row['due_on'];known=gid in previous
            if not on or row['status']!='active' and not known or on>(today+timedelta(days=3)).isoformat() and not known:continue
            status='completed' if row['status']=='completed' else 'pending'
            body='已保存目标的截止日'+('已过，请查看当前安排。' if on<today.isoformat() else '即将到达，请查看当前安排。')
            if on>(today+timedelta(days=3)).isoformat():body='原目标的截止日已调整，请查看当前安排。'
            if status=='completed':body='目标已标记完成；该标记不代表已付款。'
            body+='目标金额和已保存计划不等于资金冻结或实际付款。'
            item={'id':'simulation:goal:'+gid+':'+on,'kind':'life_event','date':on,'title':row['name']+' · 目标截止日',
                  'goal_id':gid,'linked_role':'goal_deadline','source_revision':GoalService._item(row)['object_version'],
                  'state':'completed' if status=='completed' else 'planned','source_type':'simulation','source_ref':gid,
                  'currency':'CNY','amount_minor':None,'cash_delta_minor':None,'budget_minor':None,
                  'allowed_actions':['ask','goal_detail'],'editable_fields':[]}
            _upsert(db,w,'goal-deadline:'+gid,kind='goal_deadline',title=item['title'],body=body,
                    created_at=self.bank.clock(),due_at=on,status=status,certainty='user_defined',
                    action=self._reference(db,w,item) if status=='pending' else None,source={'type':'goal','goal_id':gid})
        for gid, old in previous.items():
            if gid not in rows or not rows[gid]['due_on']:
                db.execute("UPDATE navren_messages SET status='cancelled',action=NULL,body='原目标或截止日期已删除，历史提醒保留。' WHERE workspace_id=? AND id=?",(w,old['id']))

    @staticmethod
    def _item(row):
        keys=('id','category','kind','title','body','due_at','status','amount_minor','currency','certainty')
        return {**{k:row[k] for k in keys},'created_at':_iso(row['created_at']),'read_at':_iso(row['read_at']),
                'counterparty':json.loads(row['counterparty']) if row['counterparty'] else None,
                'action':json.loads(row['action']) if row['action'] else None}

    def _summary(self, db, w, as_of):
        r = db.execute("SELECT COALESCE(SUM(read_at IS NULL),0),COALESCE(SUM(status='pending'),0) FROM navren_messages WHERE workspace_id=?",(w,)).fetchone()
        calendar_on,source=self._calendar_clock(db,w,as_of)
        return {'unread_count':r[0],'pending_count':r[1],'as_of':as_of,
                'calendar_as_of':calendar_on,'calendar_source':source,'environment':'simulation'}

    @staticmethod
    def _privacy(db, w, expected):
        from user_memory import privacy_revision
        if expected is not None and privacy_revision(db,w)!=expected:
            raise BankError('PRIVACY_REVISION_CHANGED','资料授权已变化，旧消息请求未执行。',409)

    def read(self, w, parameters=None, *, message_id=None, summary=False, expected_privacy=None):
        if any(isinstance(v,list) and len(v)!=1 for v in (parameters or {}).values()):
            raise BankError('INVALID_QUERY','消息参数不能重复。')
        p={k:(v[0] if isinstance(v,list) else v) for k,v in (parameters or {}).items()}
        strict_fields(p,{'view','category','unread','offset','limit'})
        if p.get('view','all') not in ('all','pending') or p.get('category','all') not in ('all','payments','due','security','service') or p.get('unread','false') not in ('true','false'):
            raise BankError('INVALID_QUERY','消息筛选值无效。')
        numbers={}
        for k,default,minimum,maximum in [('offset','0',0,10000),('limit','30',1,100)]:
            v=p.get(k,default)
            if not isinstance(v,str) or not re.fullmatch(r'0|[1-9][0-9]{0,4}',v) or not minimum<=int(v)<=maximum:
                raise BankError('INVALID_QUERY','消息分页参数无效。')
            numbers[k]=int(v)
        with self.bank.transaction() as db:
            self._privacy(db,w,expected_privacy)
            as_of=self._sync(db,w)
            base=self._summary(db,w,as_of)
            if summary: return base
            if message_id:
                row=db.execute('SELECT * FROM navren_messages WHERE workspace_id=? AND id=?',(w,message_id)).fetchone()
                if not row: raise BankError('MESSAGE_NOT_FOUND','消息不存在或无权查看。',404)
                return {'item':self._item(row),**{k:base[k] for k in ('as_of','calendar_as_of','calendar_source','environment')}}
            where=['workspace_id=?'];values=[w]
            if p.get('view')=='pending':where.append("status='pending'")
            if p.get('category','all')!='all':where.append('category=?');values.append(p['category'])
            if p.get('unread')=='true':where.append('read_at IS NULL')
            rows=db.execute('SELECT * FROM navren_messages WHERE '+' AND '.join(where)+' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(*values,numbers['limit']+1,numbers['offset'])).fetchall()
            return {**base,**numbers,'items':[self._item(r) for r in rows[:numbers['limit']]],'has_more':len(rows)>numbers['limit']}

    def mark_read(self, w, payload, *, message_id=None, expected_privacy=None):
        strict_fields(payload,[])
        with self.bank.transaction() as db:
            self._privacy(db,w,expected_privacy)
            # 只标当前事务内已存在消息；不因read-all回填新事件。
            as_of=self.bank._workspace(db,w)['as_of']
            if message_id:
                rows=db.execute('SELECT * FROM navren_messages WHERE workspace_id=? AND id=?',(w,message_id)).fetchall()
                if not rows: raise BankError('MESSAGE_NOT_FOUND','消息不存在或无权查看。',404)
            else: rows=db.execute('SELECT * FROM navren_messages WHERE workspace_id=? AND read_at IS NULL',(w,)).fetchall()
            updated=0
            for row in rows:
                source=json.loads(row['source'])
                first_read=self.bank.clock()
                if source.get('receipt_id'):
                    receipt=db.execute('SELECT read_at FROM calendar_reminder_receipts WHERE workspace_id=? AND id=?',(w,source['receipt_id'])).fetchone()
                    if receipt and receipt['read_at'] is not None:first_read=receipt['read_at']
                updated+=db.execute('UPDATE navren_messages SET read_at=COALESCE(read_at,?) WHERE workspace_id=? AND id=? AND read_at IS NULL',(first_read,w,row['id'])).rowcount
                if source.get('receipt_id'):
                    db.execute('UPDATE calendar_reminder_receipts SET read_at=COALESCE(read_at,?) WHERE workspace_id=? AND id=?',(first_read,w,source['receipt_id']))
            result=self._summary(db,w,as_of)
            if message_id:
                row=db.execute('SELECT * FROM navren_messages WHERE workspace_id=? AND id=?',(w,message_id)).fetchone()
                result['item']=self._item(row)
            else: result['updated_count']=updated
            return result

    @contextmanager
    def _background_transaction(self):
        # 后台不抢占用户写锁10秒；锁忙时本轮失败，下一轮重查。请求仍走原身份guard。
        with closing(self.bank.connect()) as db:
            db.execute('PRAGMA busy_timeout=250')
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _run(self):
        while not self._stop.is_set():
            with self._lock: self._last_attempt=_iso(self.bank.clock())
            try:
                with closing(self.bank.connect()) as db:
                    workspaces=[r[0] for r in db.execute('SELECT id FROM workspaces ORDER BY id')]
                for w in workspaces:
                    if self._stop.is_set(): raise _Stopped()
                    with self._background_transaction() as db: self._sync(db,w,background=True)
                with self._lock:
                    self._cycles+=1; self._last_success=_iso(self.bank.clock()); self._last_error=None
            except _Stopped: break
            except Exception as e:
                with self._lock: self._last_error=type(e).__name__
            self._stop.wait(self.interval_seconds)

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive(): return False
            self._stop.clear()
            self._thread=threading.Thread(target=self._run,name='navren-message-center',daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=2):
        with self._lock:
            self._stop.set(); thread=self._thread
        if thread and thread is not threading.current_thread(): thread.join(timeout)
        return not (thread and thread.is_alive())

    def status(self):
        with self._lock:
            return {'running':bool(self._thread and self._thread.is_alive()),'stop_requested':self._stop.is_set(),
                    'interval_seconds':self.interval_seconds,'cycles':self._cycles,'last_attempt':self._last_attempt,
                    'last_success':self._last_success,'last_error':self._last_error,'model_calls':0,
                    'external_push':False}
