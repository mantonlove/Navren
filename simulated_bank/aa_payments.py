"""实际体验用户之间的模拟AA结算；统一账本，联系人不授予付款权限。"""
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS navren_payment_contacts(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 user_id TEXT NOT NULL, alias TEXT NOT NULL CHECK(length(alias) BETWEEN 1 AND 64),
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS navren_aa_bills(
 id TEXT PRIMARY KEY, creator_workspace TEXT NOT NULL REFERENCES workspaces(id),
 creator_user_id TEXT NOT NULL, creator_name TEXT NOT NULL,
 account_id TEXT NOT NULL, receiver_card_id TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, total_minor INTEGER NOT NULL CHECK(total_minor>0),
 own_share_minor INTEGER NOT NULL CHECK(own_share_minor>=0),
 split_mode TEXT NOT NULL CHECK(split_mode IN ('equal','custom')), due_at TEXT, deadline REAL,
 source_transaction_id TEXT, status TEXT NOT NULL CHECK(status IN ('open','partially_paid','paid','closed','cancelled')),
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 FOREIGN KEY(creator_workspace,account_id) REFERENCES accounts(workspace_id,id),
 FOREIGN KEY(creator_workspace,receiver_card_id) REFERENCES cards(workspace_id,id),
 FOREIGN KEY(creator_workspace,operation_id) REFERENCES operations(workspace_id,id),
 FOREIGN KEY(creator_workspace,source_transaction_id) REFERENCES transactions(workspace_id,id));
CREATE TABLE IF NOT EXISTS navren_aa_shares(
 id TEXT PRIMARY KEY, bill_id TEXT NOT NULL REFERENCES navren_aa_bills(id),
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL, display_name TEXT NOT NULL,
 share_minor INTEGER NOT NULL CHECK(share_minor>=0), paid_minor INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL CHECK(status IN ('own','pending','paid','declined','cancelled')),
 receipt_id TEXT, updated_at REAL NOT NULL, UNIQUE(bill_id,user_id),
 CHECK(paid_minor>=0 AND paid_minor<=share_minor),
 CHECK((status='paid' AND paid_minor=share_minor AND receipt_id IS NOT NULL) OR
       (status<>'paid' AND paid_minor=0 AND receipt_id IS NULL)));
CREATE TABLE IF NOT EXISTS navren_aa_receipts(
 id TEXT PRIMARY KEY, bill_id TEXT NOT NULL REFERENCES navren_aa_bills(id),
 share_id TEXT NOT NULL UNIQUE REFERENCES navren_aa_shares(id),
 operation_id TEXT NOT NULL UNIQUE, payer_workspace TEXT NOT NULL REFERENCES workspaces(id),
 payer_user_id TEXT NOT NULL, payer_name TEXT NOT NULL, payer_account_id TEXT NOT NULL, card_id TEXT NOT NULL,
 recipient_workspace TEXT NOT NULL REFERENCES workspaces(id), recipient_user_id TEXT NOT NULL,
 recipient_name TEXT NOT NULL, recipient_account_id TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor>0), payer_transaction_id TEXT NOT NULL UNIQUE,
 recipient_transaction_id TEXT NOT NULL UNIQUE, payer_before_minor INTEGER NOT NULL, payer_after_minor INTEGER NOT NULL,
 recipient_before_minor INTEGER NOT NULL, recipient_after_minor INTEGER NOT NULL, paid_at REAL NOT NULL,
 FOREIGN KEY(payer_workspace,operation_id) REFERENCES operations(workspace_id,id),
 FOREIGN KEY(payer_workspace,payer_account_id) REFERENCES accounts(workspace_id,id),
 FOREIGN KEY(payer_workspace,card_id) REFERENCES cards(workspace_id,id),
 FOREIGN KEY(recipient_workspace,recipient_account_id) REFERENCES accounts(workspace_id,id),
 FOREIGN KEY(payer_workspace,payer_transaction_id) REFERENCES transactions(workspace_id,id),
 FOREIGN KEY(recipient_workspace,recipient_transaction_id) REFERENCES transactions(workspace_id,id));
CREATE INDEX IF NOT EXISTS navren_aa_share_workspace ON navren_aa_shares(workspace_id,status,bill_id);
CREATE INDEX IF NOT EXISTS navren_aa_bill_creator ON navren_aa_bills(creator_workspace,created_at);
'''
ACTIONS = {'aa_create','aa_pay','aa_decline','aa_cancel'}


def initialize(db):
    db.executescript(SCHEMA)
    from .aa_evidence import initialize as initialize_evidence
    initialize_evidence(db)
    from message_center import initialize as initialize_messages
    initialize_messages(db)


def text(value, name, maximum=128):
    from .service import BankError
    if (not isinstance(value,str) or not 1<=len(value)<=maximum or value!=value.strip()
            or any(ord(c)<32 or ord(c)==127 for c in value)):
        raise BankError('INVALID_FIELDS',name+'必须为有效字符串。')
    return value


def identity(db, workspace=None, user_id=None, owner_workspace=None):
    from .service import BankError
    key,value=('workspace_id',workspace) if workspace is not None else ('user_id',user_id)
    text(value,key)
    from registered_instances import experience_identity
    return experience_identity(db,workspace=workspace,persona=user_id,owner_workspace=owner_workspace)


def account_card(bank,db,workspace,account_id,card_id=None):
    """同汇款的有效借记卡和明确归属证据；收款不要求已有余额。"""
    from .service import BankError
    from .remittance import connected_owner
    text(account_id,'account_id')
    account=bank._account(db,workspace,account_id)
    detail=db.execute('SELECT * FROM account_details WHERE workspace_id=? AND account_id=?',(workspace,account_id)).fetchone()
    if not detail or detail['status']!='active' or account['currency']!='CNY':
        raise BankError('AA_ACCOUNT_UNAVAILABLE','AA仅支持有效人民币银行账户。',422)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='connected_account_links'").fetchone():
        link=db.execute('SELECT account_type FROM connected_account_links WHERE workspace_id=? AND account_id=?',(workspace,account_id)).fetchone()
        migrated=db.execute('SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
        if migrated and (not link or link['account_type']!='bank_settlement'):
            raise BankError('AA_ACCOUNT_UNAVAILABLE','AA收付款仅支持银行借记结算账户。',422)
    day=bank._workspace(db,workspace)['as_of']
    if card_id is not None:
        text(card_id,'card_id');cards=[bank._card(db,workspace,card_id)]
    else:
        cards=list(db.execute("SELECT * FROM cards WHERE workspace_id=? AND account_id=? AND type='debit' AND status='active' AND expires_on>=? ORDER BY id",(workspace,account_id,day)))
    for card in cards:
        if card['account_id']!=account_id or card['type']!='debit' or card['status']!='active' or card['expires_on']<day:
            continue
        active=db.execute("SELECT 1 FROM customers WHERE workspace_id=? AND id=? AND status='active'",(workspace,card['customer_id'])).fetchone()
        signed=db.execute("SELECT 1 FROM signatories WHERE workspace_id=? AND account_id=? AND customer_id=? AND status='active' AND role IN ('owner','authorized')",(workspace,account_id,card['customer_id'])).fetchone()
        any_signatory=db.execute('SELECT 1 FROM signatories WHERE workspace_id=? AND account_id=?',(workspace,account_id)).fetchone()
        if active and (signed or not any_signatory and connected_owner(db,workspace,account,card,detail)):
            return {'account':dict(account),'card':dict(card)}
    raise BankError('AA_PERMISSION_REQUIRED' if cards else 'AA_CARD_UNAVAILABLE','账户没有本人的有效借记卡付款/收款权限。',403 if cards else 422)


def canonical(bank,db,workspace,payload):
    """金额和实际身份由已保存份额派生；创建时绑定具体收款卡。"""
    from .service import strict_fields,BankError
    if payload['action']!='aa_create':return payload
    form=payload.get('aa_bill')
    from .aa_evidence import EVIDENCE_FIELDS,canonical_evidence
    allowed={'title','total_minor','split_mode','participants','due_at','source_transaction_id','receiver_card_id'}|EVIDENCE_FIELDS
    strict_fields(form,allowed,{'title','total_minor','split_mode','participants'})
    old=db.execute('SELECT action,payload FROM operations WHERE workspace_id=? AND idempotency_key=?',
                   (workspace,payload['idempotency_key'])).fetchone()
    if old and old['action']!='aa_create':
        raise BankError('IDEMPOTENCY_CONFLICT','同一幂等键不能对应不同操作内容。',409)
    # 重试按原请求语义比较，不让卡片后来状态变化改写原确认内容。
    saved=json.loads(old['payload'])['aa_bill'] if old else None
    receiver_card=(form.get('receiver_card_id') or saved['receiver_card_id']) if saved else account_card(
        bank,db,workspace,payload['account_id'],form.get('receiver_card_id'))['card']['id']
    source=form.get('source_transaction_id')
    if isinstance(source,str) and source.startswith('runtime:'):source=source[len('runtime:'):]
    if isinstance(source,str) and source.startswith('history:'):
        raise BankError('AA_SOURCE_INVALID','第一版只关联运行账本已记账消费，封存历史请先手工创建账单。',422)
    normalized={**form,'due_at':form.get('due_at'),'source_transaction_id':form.get('source_transaction_id'),
                'receiver_card_id':receiver_card,'source_transaction_id':source}
    return {**payload,'aa_bill':canonical_evidence(bank,db,workspace,normalized,saved)}


def bill_row(db,bill_id):
    from .service import BankError
    text(bill_id,'bill_id')
    bill=db.execute('SELECT * FROM navren_aa_bills WHERE id=?',(bill_id,)).fetchone()
    if not bill:raise BankError('AA_NOT_FOUND','AA账单不存在或无权访问。',404)
    return bill


def share_row(db,workspace,share_id):
    from .service import BankError
    text(share_id,'share_id')
    row=db.execute('SELECT * FROM navren_aa_shares WHERE id=? AND workspace_id=?',(share_id,workspace)).fetchone()
    if not row:raise BankError('AA_NOT_FOUND','AA份额不存在或无权访问。',404)
    return row


def payable(bank,bill,share):
    from .service import BankError
    if (share['status']!='pending' or bill['status'] not in ('open','partially_paid') or
            bill['deadline'] is not None and bank.clock()>=bill['deadline']):
        raise BankError('AA_NOT_PAYABLE','该份额已支付、已拒绝、已取消或已过截止时间。',409)


def validate(bank,db,workspace,payload):
    from .service import BankError,integer,strict_fields
    me=identity(db,workspace=workspace);action=payload['action']
    account=bank._account(db,workspace,payload['account_id'])
    if action=='aa_create':
        form=payload['aa_bill'];recipient=account_card(bank,db,workspace,account['id'],form['receiver_card_id'])
        text(form['title'],'title',100);total=integer(form['total_minor'],'AA总额',1)
        mode=form['split_mode'];people=form['participants']
        if mode not in ('equal','custom') or not isinstance(people,list) or not 2<=len(people)<=4:
            raise BankError('AA_PARTICIPANTS_INVALID','请选择2至4位实际体验用户并指定均分或自定义。',422)
        members=[];seen=set()
        for p in people:
            fields={'user_id'} if mode=='equal' else {'user_id','share_minor'}
            strict_fields(p,fields,fields)
            person=identity(db,user_id=p['user_id'],owner_workspace=workspace)
            if person['user_id'] in seen:raise BankError('AA_PARTICIPANTS_INVALID','参与人不能重复。',422)
            seen.add(person['user_id']);members.append({**person,'share_minor':integer(p['share_minor'],'AA份额') if mode=='custom' else 0})
        if me['user_id'] not in seen:raise BankError('AA_PARTICIPANTS_INVALID','分摊成员必须包含本人份额。',422)
        members.sort(key=lambda r:r['user_id'])
        if mode=='equal':
            unit,remainder=divmod(total,len(members))
            for i,p in enumerate(members):p['share_minor']=unit+(i<remainder)
        if sum(p['share_minor'] for p in members)!=total:
            raise BankError('AA_TOTAL_MISMATCH','成员份额之和必须等于账单总额。',422)
        if any(p['share_minor']==0 for p in members if p['user_id']!=me['user_id']):
            raise BankError('AA_PARTICIPANTS_INVALID','其他参与人的待付份额必须大于零。',422)
        deadline=None
        if form['due_at'] is not None:
            text(form['due_at'],'due_at',40)
            try:
                moment=datetime.fromisoformat(form['due_at'])
                if moment.tzinfo is None:raise ValueError()
                deadline=moment.timestamp()
            except (ValueError,OverflowError):raise BankError('INVALID_DATE','截止时间必须为带时区的ISO时间。')
            if deadline<=bank.clock():raise BankError('INVALID_DATE','截止时间必须晚于当前时间。')
        from .aa_evidence import validate_create
        operation=db.execute('SELECT id FROM operations WHERE workspace_id=? AND idempotency_key=?',(workspace,payload['idempotency_key'])).fetchone()
        validate_create(bank,db,workspace,form,total,operation['id'] if operation else None)
        return {'account':dict(account),'identity':me,'receiver_card':recipient['card'],'members':members,
                'own_share_minor':next(p['share_minor'] for p in members if p['user_id']==me['user_id']),
                'deadline':deadline,'form':form}
    if action=='aa_cancel':
        bill=bill_row(db,payload['aa_bill_id'])
        if bill['creator_workspace']!=workspace or bill['account_id']!=account['id']:
            raise BankError('AA_NOT_FOUND','AA账单不存在或无权访问。',404)
        if not db.execute("SELECT 1 FROM navren_aa_shares WHERE bill_id=? AND status='pending'",(bill['id'],)).fetchone():
            raise BankError('AA_NOT_PAYABLE','此账单没有可取消的未付份额。',409)
        return {'account':dict(account),'identity':me,'bill':dict(bill)}
    share=share_row(db,workspace,payload['aa_share_id']);bill=bill_row(db,share['bill_id'])
    if share['user_id']!=me['user_id'] or bill['creator_workspace']==workspace:
        raise BankError('AA_NOT_PAYABLE','本人承担的份额不能支付或拒绝。',409)
    if action=='aa_decline':
        if share['status']!='pending' or bill['status'] not in ('open','partially_paid'):
            raise BankError('AA_NOT_PAYABLE','只能拒绝本人尚未支付的份额。',409)
        return {'account':dict(account),'identity':me,'bill':dict(bill),'share':dict(share)}
    payable(bank,bill,share)
    from .aa_evidence import validate_payment
    validate_payment(bank,db,bill)
    payer=account_card(bank,db,workspace,account['id'],payload['card_id'])
    recipient_identity=identity(db,workspace=bill['creator_workspace'])
    if recipient_identity['user_id']!=bill['creator_user_id']:
        raise BankError('AA_IDENTITY_REQUIRED','收款用户绑定已变化。',409)
    account_card(bank,db,bill['creator_workspace'],bill['account_id'],bill['receiver_card_id'])
    amount=share['share_minor'];available=max(0,account['balance_minor']-bank._active_holds(db,workspace,account['id']))
    if amount>available:raise BankError('INSUFFICIENT_AVAILABLE','AA金额超过余额扣除实际冻结后的可用资金。',422)
    executed_day=datetime.fromtimestamp(bank.clock(),timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    bank._transfer_limits(db,workspace,account['id'],executed_day,amount)
    online=db.execute("SELECT enabled FROM card_controls WHERE workspace_id=? AND card_id=? AND control_type='online'",(workspace,payload['card_id'])).fetchone()
    if online and not online['enabled']:
        raise BankError('AA_CARD_UNAVAILABLE','付款卡已关闭线上交易，AA未执行。',422)
    control=db.execute("SELECT enabled,limit_minor FROM card_controls WHERE workspace_id=? AND card_id=? AND control_type='single_limit'",(workspace,payload['card_id'])).fetchone()
    if control and control['enabled'] and amount>control['limit_minor']:
        raise BankError('TRANSFER_LIMIT','AA金额超过付款卡单笔限额。',422)
    return {'account':dict(account),'identity':me,'card':payer['card'],'bill':dict(bill),'share':dict(share),'amount_minor':amount}


def update_status(bank,db,bill_id):
    bill=bill_row(db,bill_id)
    if bill['status']=='cancelled':return 'cancelled'
    states=[r[0] for r in db.execute("SELECT status FROM navren_aa_shares WHERE bill_id=? AND status<>'own'",(bill_id,))]
    state=('paid' if all(s=='paid' for s in states) else 'closed' if 'pending' not in states else
           'partially_paid' if 'paid' in states else 'open')
    db.execute('UPDATE navren_aa_bills SET status=?,updated_at=? WHERE id=?',(state,bank.clock(),bill_id))
    return state


def receipt_view(row,workspace):
    from .service import BankError
    if workspace not in (row['payer_workspace'],row['recipient_workspace']):raise BankError('AA_NOT_FOUND','AA回执不存在或无权访问。',404)
    payer=workspace==row['payer_workspace'];side='payer' if payer else 'recipient';other='recipient' if payer else 'payer'
    result={k:row[k] for k in ('id','bill_id','share_id','amount_minor','paid_at')}
    result['paid_at']=datetime.fromtimestamp(row['paid_at'],timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    result.update(status='succeeded',side=side,account_id=row[side+'_account_id'],
        transaction_id=row[side+'_transaction_id'],balance_before_minor=row[side+'_before_minor'],
        balance_after_minor=row[side+'_after_minor'],counterparty={'user_id':row[other+'_user_id'],'display_name':row[other+'_name']},
        accounting_kind='advance_recovery',environment='simulation',notice='仅在体验用户模拟账本之间结算，未连接真实银行。')
    if payer:result['card_id']=row['card_id']
    return result


def execute(bank,db,workspace,operation,payload,details):
    from .service import BankError
    from message_center import sync_aa_bill
    now=bank.clock();action=payload['action'];me=details['identity']
    if action=='aa_create':
        form=details['form'];bill_id=uuid.uuid4().hex
        db.execute('INSERT INTO navren_aa_bills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (bill_id,workspace,me['user_id'],me['display_name'],payload['account_id'],details['receiver_card']['id'],operation,
             form['title'],form['total_minor'],details['own_share_minor'],form['split_mode'],form['due_at'],details['deadline'],
             form['source_transaction_id'],'open',now,now))
        for p in details['members']:
            db.execute('INSERT INTO navren_aa_shares VALUES(?,?,?,?,?,?,0,?,NULL,?)',
                (uuid.uuid4().hex,bill_id,p['workspace_id'],p['user_id'],p['display_name'],p['share_minor'],
                 'own' if p['user_id']==me['user_id'] else 'pending',now))
        from .aa_evidence import save_bill
        save_bill(bank,db,workspace,bill_id,form)
        sync_aa_bill(bank,db,bill_id)
        return {'bill_id':bill_id,'status':'open','own_share_minor':details['own_share_minor'],
                'receivable_minor':form['total_minor']-details['own_share_minor'],'environment':'simulation'}
    bill=details['bill'];share=details.get('share')
    if action=='aa_cancel':
        ids=[r[0] for r in db.execute("SELECT id FROM navren_aa_shares WHERE bill_id=? AND status='pending' ORDER BY id",(bill['id'],))]
        db.execute("UPDATE navren_aa_shares SET status='cancelled',updated_at=? WHERE bill_id=? AND status='pending'",(now,bill['id']))
        db.execute("UPDATE navren_aa_bills SET status='cancelled',updated_at=? WHERE id=?",(now,bill['id']))
        sync_aa_bill(bank,db,bill['id'])
        return {'bill_id':bill['id'],'status':'cancelled','cancelled_share_ids':ids,'environment':'simulation'}
    if action=='aa_decline':
        db.execute("UPDATE navren_aa_shares SET status='declined',updated_at=? WHERE id=? AND status='pending'",(now,share['id']))
        update_status(bank,db,bill['id']);bank._bump(db,bill['creator_workspace'])
        sync_aa_bill(bank,db,bill['id'])
        return {'bill_id':bill['id'],'share_id':share['id'],'status':'declined','environment':'simulation'}
    # 调用方已在BEGIN IMMEDIATE内重验份额、双方身份、账户、卡与实际冻结。
    amount=details['amount_minor'];recipient=bank._account(db,bill['creator_workspace'],bill['account_id'])
    payer_before=details['account']['balance_minor'];recipient_before=recipient['balance_minor']
    changed=db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE workspace_id=? AND id=? AND balance_minor>=?',
        (amount,workspace,payload['account_id'],amount)).rowcount
    if changed!=1:raise BankError('INSUFFICIENT_AVAILABLE','付款余额发生变化。',409)
    db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE workspace_id=? AND id=?',
        (amount,bill['creator_workspace'],bill['account_id']))
    executed_at=datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
    payer_day=receiver_day=executed_at.date()
    note='AA垫付回收 · '+bill['title']+' · '+bill['id']+' · '+share['id']
    payer_tx=bank._ledger(db,workspace,payload['account_id'],payer_day,bill['creator_name'],amount,-amount,
        'transfer',operation,category='AA分摊付款',note=note)
    recipient_tx=bank._ledger(db,bill['creator_workspace'],bill['account_id'],receiver_day,me['display_name'],amount,amount,
        'transfer',None,category='AA垫付回收',note=note)  # 不向收款侧流水泄露付款人的操作标识。
    db.execute('UPDATE transactions SET occurred_at=? WHERE id IN (?,?)',
        (executed_at.isoformat(timespec='seconds'),payer_tx,recipient_tx))
    receipt_id=uuid.uuid4().hex
    db.execute('INSERT INTO navren_aa_receipts VALUES('+','.join('?' for _ in range(21))+')',
        (receipt_id,bill['id'],share['id'],operation,workspace,me['user_id'],me['display_name'],payload['account_id'],payload['card_id'],
         bill['creator_workspace'],bill['creator_user_id'],bill['creator_name'],bill['account_id'],amount,payer_tx,recipient_tx,
         payer_before,payer_before-amount,recipient_before,recipient_before+amount,now))
    changed=db.execute("UPDATE navren_aa_shares SET status='paid',paid_minor=share_minor,receipt_id=?,updated_at=? WHERE id=? AND status='pending'",(receipt_id,now,share['id'])).rowcount
    if changed!=1:raise BankError('AA_NOT_PAYABLE','份额已处理，整笔AA结算已回滚。',409)
    update_status(bank,db,bill['id']);bank._bump(db,bill['creator_workspace'])
    bank._audit(db,bill['creator_workspace'],'aa_payment_received',{'bill_id':bill['id'],'share_id':share['id'],'receipt_id':receipt_id,'amount_minor':amount})
    sync_aa_bill(bank,db,bill['id'])
    return receipt_view(db.execute('SELECT * FROM navren_aa_receipts WHERE id=?',(receipt_id,)).fetchone(),workspace)


def share_view(row,*,payable_now=False):
    return {**{k:row[k] for k in ('id','user_id','display_name','share_minor','status','paid_minor','receipt_id')},'payable':payable_now and row['status']=='pending'}


def view(bank,db,workspace,bill_id):
    from .service import BankError
    me=identity(db,workspace=workspace);bill=bill_row(db,bill_id)
    rows=list(db.execute('SELECT * FROM navren_aa_shares WHERE bill_id=? ORDER BY user_id',(bill_id,)))
    my=next((r for r in rows if r['workspace_id']==workspace and r['user_id']==me['user_id']),None)
    creator=bill['creator_workspace']==workspace
    if not my or creator and bill['creator_user_id']!=me['user_id']:
        raise BankError('AA_NOT_FOUND','AA账单不存在或无权访问。',404)
    overdue=bill['deadline'] is not None and bank.clock()>=bill['deadline']
    result={k:bill[k] for k in ('id','title','total_minor','split_mode','due_at','status','created_at','updated_at')}
    result.update(creator={'user_id':bill['creator_user_id'],'display_name':bill['creator_name']},role='creator' if creator else 'participant',
        source_transaction_id=bill['source_transaction_id'] if creator else None,
        my_share=share_view(my,payable_now=not overdue and bill['status'] in ('open','partially_paid')),
        overdue=overdue,environment='simulation')
    if creator:
        result.update(account_id=bill['account_id'],own_share_minor=bill['own_share_minor'],
            receivable_minor=bill['total_minor']-bill['own_share_minor'],collected_minor=sum(r['paid_minor'] for r in rows),
            outstanding_minor=sum(r['share_minor'] for r in rows if r['status']=='pending'),
            participants=[share_view(r) for r in rows])
    receipts=db.execute('SELECT * FROM navren_aa_receipts WHERE bill_id=? AND (payer_workspace=? OR recipient_workspace=?) ORDER BY paid_at,id',(bill_id,workspace,workspace))
    result['receipts']=[receipt_view(r,workspace) for r in receipts]
    from .aa_evidence import bill_evidence
    result.update(bill_evidence(db,bill_id))
    return result


def bills(bank,workspace,bill_id=None):
    with closing(bank.connect()) as db:
        db.execute('BEGIN')
        me=identity(db,workspace=workspace)
        if bill_id:return view(bank,db,workspace,bill_id)
        ids=[r[0] for r in db.execute('SELECT bill_id FROM navren_aa_shares WHERE workspace_id=? AND user_id=? ORDER BY updated_at DESC,id',(workspace,me['user_id']))]
        values=[view(bank,db,workspace,b) for b in ids]
        return {'initiated':[b for b in values if b['role']=='creator'],
            'participating':[b for b in values if b['role']=='participant'],
            'pending':[b for b in values if b['my_share']['payable']],
            'current_user_id':me['user_id'],'environment':'simulation'}


def operation_view(bank,workspace,operation,*,include_operation=False):
    with closing(bank.connect()) as db:
        db.execute('BEGIN')
        row=bank._operation(db,workspace,operation);payload=json.loads(row['payload']);action=payload['action']
        def wrap(value):return {'operation':bank._public_operation(row),'aa':value} if include_operation else value
        if action not in ACTIONS:return None
        if action=='aa_create' and row['result'] is None:
            # 拒绝/过期的创建草案仍可核对原表单；不会创建应收。
            form=payload['aa_bill'];me=identity(db,workspace=workspace)
            members=[];people=sorted(form['participants'],key=lambda p:p['user_id'])
            unit,remainder=divmod(form['total_minor'],len(people))
            for i,p in enumerate(people):
                user=identity(db,user_id=p['user_id'],owner_workspace=workspace);amount=unit+(i<remainder) if form['split_mode']=='equal' else p['share_minor']
                members.append({'user_id':user['user_id'],'display_name':user['display_name'],'share_minor':amount,
                    'status':'own' if user['user_id']==me['user_id'] else 'pending'})
            own=next(p['share_minor'] for p in members if p['user_id']==me['user_id'])
            return wrap({'kind':'create','bill':{**{k:form[k] for k in ('title','total_minor','split_mode','due_at','source_transaction_id')},
                'own_share_minor':own,'receivable_minor':form['total_minor']-own,'participants':members,
                'consumption':form.get('consumption_snapshot'),'attachments':form.get('attachment_snapshots',[])}})
        if action=='aa_create':bill_id=json.loads(row['result'])['bill_id']
        elif action=='aa_cancel':bill_id=payload['aa_bill_id']
        else:bill_id=share_row(db,workspace,payload['aa_share_id'])['bill_id']
        bill=view(bank,db,workspace,bill_id)
        result={'kind':action.removeprefix('aa_'),'bill':bill}
        if action in ('aa_pay','aa_decline'):result['share']=bill['my_share']
        return wrap(result)


def contacts(bank,workspace,payload=None,contact_id=None,action=None):
    from .service import BankError,strict_fields
    with bank.transaction() as db:
        me=identity(db,workspace=workspace)
        if action:
            if action=='create':
                strict_fields(payload,{'user_id','alias'},{'user_id','alias'});other=identity(db,user_id=payload['user_id'],owner_workspace=workspace)
                if other['user_id']==me['user_id']:raise BankError('INVALID_FIELDS','本人不需要添加为收付款联系人。')
                text(payload['alias'],'alias',64)
                old=db.execute('SELECT * FROM navren_payment_contacts WHERE workspace_id=? AND user_id=?',(workspace,other['user_id'])).fetchone()
                if old and old['alias']!=payload['alias']:raise BankError('CONTACT_EXISTS','用户已在联系人中，请编辑别名。',409)
                contact_id=old['id'] if old else uuid.uuid4().hex
                if not old:db.execute('INSERT INTO navren_payment_contacts VALUES(?,?,?,?,?,?)',(contact_id,workspace,other['user_id'],payload['alias'],bank.clock(),bank.clock()))
            else:
                text(contact_id,'contact_id')
                row=db.execute('SELECT * FROM navren_payment_contacts WHERE workspace_id=? AND id=?',(workspace,contact_id)).fetchone()
                if not row:raise BankError('AA_NOT_FOUND','联系人不存在或无权访问。',404)
                if action=='delete':
                    strict_fields(payload,set());db.execute('DELETE FROM navren_payment_contacts WHERE id=? AND workspace_id=?',(contact_id,workspace))
                    return {'deleted':True,'id':contact_id,'environment':'simulation'}
                strict_fields(payload,{'alias'},{'alias'});text(payload['alias'],'alias',64)
                db.execute('UPDATE navren_payment_contacts SET alias=?,updated_at=? WHERE workspace_id=? AND id=?',(payload['alias'],bank.clock(),workspace,contact_id))
        from registered_instances import experience_members
        members=experience_members(db,workspace)
        names={r['user_id']:r['display_name'] for r in members}
        rows=[{**dict(r),'display_name':names[r['user_id']]} for r in db.execute('SELECT * FROM navren_payment_contacts WHERE workspace_id=? ORDER BY created_at,id',(workspace,)) if r['user_id'] in names]
        allowed=('id','user_id','alias','display_name','created_at','updated_at')
        values=[{k:r[k] for k in allowed} for r in rows]
        if action:return {'contact':next(c for c in values if c['id']==contact_id),'environment':'simulation'}
        users=[{'user_id':r['user_id'],'display_name':r['display_name']} for r in members if r['user_id']!=me['user_id']]
        return {'contacts':values,'available_users':users,'current_user_id':me['user_id'],'environment':'simulation'}
