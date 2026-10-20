"""体验身份的本地平台关联；资金/流水仍属于原账本，不访问真实渠道。"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .service import BankError,encoded,strict_fields

CHANNELS={
    ('payment','wechat'):('微信支付','payment_wallet'),
    ('payment','alipay'):('支付宝','payment_wallet'),
    ('securities','citic'):('中信证券','securities_cash'),
    ('securities','huatai'):('华泰证券','securities_cash'),
}
SCHEMA='''CREATE TABLE IF NOT EXISTS navren_account_connections(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 connection_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('payment','securities')),
 institution TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('bound','disconnected')),
 origin TEXT NOT NULL CHECK(origin IN ('migrated','local_initialized')),
 metadata TEXT NOT NULL CHECK(json_valid(metadata)),created_at REAL NOT NULL,updated_at REAL NOT NULL,
 UNIQUE(workspace_id,connection_id),UNIQUE(workspace_id,kind,institution));'''
NOTICE='比赛本地演示关联，不连接真实支付平台或券商；解除绑定只解除平台关联，账户资金、流水和持仓仍保留。'


def ident(workspace,connection):
    return hashlib.sha256(('navren:account-connection:'+workspace+':'+connection).encode()).hexdigest()[:32]


def pair(kind,institution):
    if not isinstance(kind,str) or not isinstance(institution,str) or (kind,institution) not in CHANNELS:
        raise BankError('CONNECTION_CHANNEL_INVALID','请选择支持的本地演示渠道。',422)
    return CHANNELS[kind,institution]


def seed_defaults(db,workspace,binding,now):
    """仅首次登记受控迁移连接；INSERT OR IGNORE永不重置已解除的状态。"""
    from .connected_assets import INSTITUTIONS
    opening=json.loads(binding['snapshot']);links=list(db.execute('SELECT * FROM connected_account_links WHERE workspace_id=?',(workspace,)))
    for connection in opening['connections']:
        name=INSTITUTIONS.get(connection['institution_name'],connection['institution_name'])
        selected=next((key for key,(label,_) in CHANNELS.items() if label==name and key[0]==connection['institution_type']),None)
        if not selected:continue
        types={r['account_type'] for r in links if r['connection_id']==connection['connection_id']}
        if CHANNELS[selected][1] not in types:continue
        metadata={**connection,'institution_name':name,'source':'navren_simulated_bank','real_connection':False}
        db.execute('INSERT OR IGNORE INTO navren_account_connections VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ident(workspace,connection['connection_id']),workspace,connection['connection_id'],*selected,'bound','migrated',encoded(metadata),now,now))


class AccountConnections:
    def __init__(self,gateway):
        self.g=gateway;self.bank=gateway.bank
        with self.bank.transaction() as db:
            db.execute(SCHEMA)
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {'connected_asset_migrations','connected_account_links'}<=tables:
                for row in db.execute('SELECT * FROM connected_asset_migrations').fetchall():
                    seed_defaults(db,row['workspace_id'],row,self.bank.clock())

    def _identity(self,db,workspace):
        from registered_instances import experience_identity
        user=experience_identity(db,workspace=workspace)
        binding=self.g.connected_assets._binding(db,workspace)
        if user['user_id']!=binding['subject_id']:
            raise BankError('CONNECTED_IDENTITY_MISMATCH','平台关联与当前身份不一致。',409)
        return binding

    def states(self,db,workspace):
        """供宿主同一读取快照叠加状态；不改原opening或余额。"""
        rows=db.execute('SELECT * FROM navren_account_connections WHERE workspace_id=? ORDER BY kind,institution',(workspace,)).fetchall()
        result=[]
        for row in rows:
            label,typ=pair(row['kind'],row['institution'])
            accounts=[{'account_id':a['id'],'name':a['name'],'account_type':a['account_type'],'currency':a['currency'],
                'balance_minor':a['balance_minor'],'connection_status':row['status'],'connection_active':row['status']=='bound'}
                for a in db.execute('SELECT a.*,l.account_type FROM connected_account_links l JOIN accounts a ON a.workspace_id=l.workspace_id AND a.id=l.account_id WHERE l.workspace_id=? AND l.connection_id=? ORDER BY a.id',(workspace,row['connection_id']))]
            if not accounts or any(a['account_type']!=typ for a in accounts):
                raise BankError('CONNECTED_ACCOUNT_INCOMPLETE','平台关联账户目录不完整。',503)
            result.append({'id':row['id'],'kind':row['kind'],'institution':row['institution'],'institution_name':label,
                'connection_id':row['connection_id'],'status':row['status'],'active':row['status']=='bound',
                'source':row['origin'],'accounts':accounts,'environment':'simulation','real_connection':False})
        return result

    def read(self,workspace,query):
        if not isinstance(query,dict) or set(query)-{'kind','institution'} or any(not isinstance(v,list) or len(v)!=1 for v in query.values()):
            raise BankError('INVALID_QUERY','平台关联查询参数无效。',400)
        kind=query.get('kind',['payment'])[0];institution=query.get('institution',[None])[0]
        if kind not in ('payment','securities'):raise BankError('CONNECTION_CHANNEL_INVALID','关联类型无效。',422)
        if institution is not None:pair(kind,institution)
        with closing(self.bank.connect()) as db:
            db.execute('BEGIN');self._identity(db,workspace)
            states=[b for b in self.states(db,workspace) if b['kind']==kind and (institution is None or b['institution']==institution)]
            existing={b['institution']:b for b in states}
            channels=[]
            for (k,key),(label,_) in CHANNELS.items():
                if k!=kind or institution is not None and key!=institution:continue
                bound=existing.get(key)
                channels.append({'kind':k,'institution':key,'institution_name':label,'mode':'local_simulation',
                    'status':bound['status'] if bound else 'unbound','binding_id':bound['id'] if bound else None,
                    'can_bind':True,'needs_initialization':bound is None})
            return {'environment':'simulation','kind':kind,'institution':institution,'channels':channels,
                'bindings':states,'active_bindings':[b for b in states if b['active']],'notice':NOTICE}

    def overview_states(self,workspace):
        # 旧普通工作区无此目录，不阻断其既有overview。
        with closing(self.bank.connect()) as db:
            db.execute('BEGIN')
            guard=getattr(self.bank,'request_identity_guard',None)
            if guard:guard(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='connected_asset_migrations'").fetchone():return []
            if not db.execute('SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone():return []
            self._identity(db,workspace)
            return self.states(db,workspace)

    def _create_zero(self,db,workspace,binding,kind,institution,now):
        from .connected_assets import _id,_archive_insert
        label,typ=pair(kind,institution)
        customer=db.execute('SELECT id FROM customers WHERE workspace_id=?',(workspace,)).fetchone()
        if not customer:raise BankError('CONNECTED_IDENTITY_MISMATCH','本人客户档案未完整接入。',409)
        source='local:'+binding['subject_id']+':'+kind+':'+institution+':cash'
        connection=_id(workspace,'connection','local:'+kind+':'+institution)
        account=_id(workspace,'account',source);branch=_id(workspace,'branch',connection)
        day=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat()
        origin={'origin':'runtime_account_connection','real_connection':False,'source':'navren_local_account_connection',
            'source_account_id':source,'account_type':typ,'opening_amount_minor':0,'history_initialized':False}
        _archive_insert(db,'bank_branches',workspace,branch,{'name':label+'本地演示关联','bank_name':label,
            'bank_code':'LOCAL-'+institution.upper(),'city':'未提供','channel':'simulation'},origin)
        db.execute('INSERT INTO accounts VALUES(?,?,?,?,0,0)',(account,workspace,label+('支付钱包' if kind=='payment' else '证券资金账户'),'CNY'))
        _archive_insert(db,'account_details',workspace,_id(workspace,'details',account),
            {'customer_id':customer['id'],'account_id':account,'branch_id':branch,'masked_number':'本地演示账户',
             'type':'current','status':'active','opened_on':day,'interest_bps':0,'overdraft_minor':0},origin)
        for category,limit in (('transfer_daily',5000000),('transfer_single',2000000),('cash_daily',1000000)):
            _archive_insert(db,'account_limits',workspace,_id(workspace,'limit',account+category),
                {'account_id':account,'kind':category,'limit_minor':limit,'used_minor':0,'effective_on':day},origin)
        db.execute('INSERT INTO connected_account_links VALUES(?,?,?,?,?)',(workspace,account,source,connection,typ))
        metadata={'subject_id':binding['subject_id'],'connection_id':connection,'institution_name':label,
            'institution_type':kind,'source':'navren_local_account_connection','real_connection':False,'opened_on':day,
            'origin':'runtime_account_connection','history_initialized':False}
        db.execute('INSERT INTO navren_account_connections VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ident(workspace,connection),workspace,connection,kind,institution,'bound','local_initialized',encoded(metadata),now,now))
        return ident(workspace,connection)

    def change(self,workspace,payload,binding_id=None):
        strict_fields(payload,{'confirmed'} if binding_id is not None else {'kind','institution','confirmed'},
            {'confirmed'} if binding_id is not None else {'kind','institution','confirmed'})
        if payload['confirmed'] is not True:raise BankError('CONNECTION_CONFIRMATION_REQUIRED','请明确确认本地平台关联操作。',422)
        if binding_id is not None and (not isinstance(binding_id,str) or not re.fullmatch(r'[a-f0-9]{32}',binding_id)):
            raise BankError('ACCOUNT_CONNECTION_NOT_FOUND','平台关联不存在或无权访问。',404)
        with self.bank.transaction() as db:
            binding=self._identity(db,workspace);now=self.bank.clock();created=False
            if binding_id is not None:
                row=db.execute('SELECT * FROM navren_account_connections WHERE workspace_id=? AND id=?',(workspace,binding_id)).fetchone()
                if not row:raise BankError('ACCOUNT_CONNECTION_NOT_FOUND','平台关联不存在或无权访问。',404)
                status='disconnected';changed=row['status']!=status
            else:
                kind,institution=payload['kind'],payload['institution'];pair(kind,institution)
                row=db.execute('SELECT * FROM navren_account_connections WHERE workspace_id=? AND kind=? AND institution=?',(workspace,kind,institution)).fetchone()
                status='bound';changed=row is None or row['status']!=status
                if row:binding_id=row['id']
                else:binding_id=self._create_zero(db,workspace,binding,kind,institution,now);created=True
            if changed:
                db.execute('UPDATE navren_account_connections SET status=?,updated_at=? WHERE workspace_id=? AND id=?',(status,now,workspace,binding_id))
                self.bank._bump(db,workspace)
                self.bank._audit(db,workspace,'account_connection_bound' if status=='bound' else 'account_connection_disconnected',
                    {'binding_id':binding_id,'account_created':created,'real_connection':False,'funds_preserved':True})
            result=next(b for b in self.states(db,workspace) if b['id']==binding_id)
            return {'environment':'simulation','binding':result,'changed':changed,'account_created':created,'notice':NOTICE}
