"""完整模拟汇款单；资金仍只存在accounts/transactions，不连接真实银行。"""
from datetime import datetime, timedelta, timezone
import re
import json
from zoneinfo import ZoneInfo

SCHEMA = '''
CREATE UNIQUE INDEX IF NOT EXISTS navren_remittance_operation_workspace ON operations(workspace_id,id);
CREATE TABLE IF NOT EXISTS navren_remittances(
 operation_id TEXT PRIMARY KEY,
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 account_id TEXT NOT NULL, card_id TEXT NOT NULL,
 sender_name TEXT NOT NULL, sender_phone TEXT NOT NULL, recipient_name TEXT NOT NULL,
 recipient_account TEXT NOT NULL, recipient_bank TEXT NOT NULL, recipient_branch TEXT NOT NULL,
 purpose TEXT NOT NULL, message TEXT NOT NULL, mode TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor>0), execute_on TEXT NOT NULL, execute_at REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('scheduled','succeeded','failed','cancelled')),
 scheduled_payment_id TEXT, payment_id TEXT, transaction_id TEXT,
 balance_before_minor INTEGER NOT NULL, balance_after_minor INTEGER NOT NULL,
 failure_reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(workspace_id,scheduled_payment_id),
 FOREIGN KEY(workspace_id,operation_id) REFERENCES operations(workspace_id,id),
 FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id),
 FOREIGN KEY(workspace_id,card_id) REFERENCES cards(workspace_id,id),
 FOREIGN KEY(workspace_id,scheduled_payment_id) REFERENCES scheduled_payments(workspace_id,id),
 FOREIGN KEY(workspace_id,payment_id) REFERENCES payment_instructions(workspace_id,id),
 FOREIGN KEY(workspace_id,transaction_id) REFERENCES transactions(workspace_id,id));
CREATE INDEX IF NOT EXISTS navren_remittances_workspace ON navren_remittances(workspace_id,status);
'''
FIELDS = {'card_id','sender_name','sender_phone','recipient_name','recipient_account',
          'recipient_bank','recipient_branch','purpose','message','mode'}


def initialize(db):
    present=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='navren_remittances'").fetchone()
    if present:
        columns={row['name'] for row in db.execute('PRAGMA table_info(navren_remittances)')}
        keys=list(db.execute('PRAGMA foreign_key_list(navren_remittances)'))
        valid=('execute_at' in columns and all(any(r['table']==target and r['from']=='workspace_id' for r in keys)
            for target in ('operations','accounts','cards','scheduled_payments','payment_instructions','transactions')))
        if not valid:
            if db.execute('SELECT 1 FROM navren_remittances LIMIT 1').fetchone():
                raise RuntimeError('汇款单结构需离线迁移；原记录保留，禁止自动删除非空旧表。')
            db.execute('DROP TABLE navren_remittances')
    db.executescript(SCHEMA)


def execution_time(bank,mode,*,now=None):
    now=bank.clock() if now is None else now
    if mode=='ordinary':stamp=now+7200
    elif mode=='next_day':
        local=datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        stamp=(local+timedelta(days=1)).replace(hour=9,minute=0,second=0,microsecond=0).timestamp()
    else:stamp=now
    return stamp,datetime.fromtimestamp(stamp,timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()


def connected_owner(db,workspace,account,card,details):
    """旧连接迁移的新账户无signatories行时，使用已登记开账主体的明确归属证据。"""
    from .service import BankError
    needed={'connected_asset_migrations','connected_account_links','connected_card_links'}
    present={r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not needed<=present or details['customer_id']!=card['customer_id']:return False
    from registered_instances import experience_identity
    try:actor=experience_identity(db,workspace=workspace)
    except BankError:return False
    binding=db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=? AND subject_id=?',
                       (workspace,actor['user_id'])).fetchone()
    link=db.execute('SELECT * FROM connected_account_links WHERE workspace_id=? AND account_id=?',(workspace,account['id'])).fetchone()
    card_link=db.execute('SELECT * FROM connected_card_links WHERE workspace_id=? AND card_id=?',(workspace,card['id'])).fetchone()
    if not binding or not link or not card_link:return False
    try:
        source=json.loads(binding['snapshot'])
        owned=next(a for a in source['accounts'] if a['account_id']==link['source_account_id'])
        payer=next(c for c in source['cards'] if c['card_id']==card_link['source_card_id'])
        return (owned['subject_id']==binding['subject_id'] and payer['subject_id']==binding['subject_id']
                and payer['account_id']==owned['account_id'] and card_link['role']==payer['role'])
    except (ValueError,KeyError,TypeError,StopIteration):return False


def sender_identity(bank,db,workspace,account_id,card_id):
    """只从付款卡的本工作区客户档案读取；脱敏联系方式不还原。"""
    from .service import BankError
    for key,value in (('account_id',account_id),('card_id',card_id)):
        if (not isinstance(value,str) or not 1<=len(value)<=128 or value!=value.strip()
                or any(ord(char)<32 or ord(char)==127 for char in value)):
            raise BankError('INVALID_REMITTANCE_FIELD',key+'格式无效。')
    account=bank._account(db,workspace,account_id)
    card=bank._card(db,workspace,card_id)
    if card['account_id']!=account['id']:
        raise BankError('REMITTANCE_CARD_MISMATCH','付款银行卡不属于所选账户。',422)
    customer=db.execute("SELECT display_name FROM customers WHERE workspace_id=? AND id=? AND status='active'",
                        (workspace,card['customer_id'])).fetchone()
    if not customer:
        raise BankError('REMITTANCE_PERMISSION_REQUIRED','当前银行卡持有人没有有效账户付款权限。',403)
    contacts=list(db.execute("SELECT masked_value FROM contacts WHERE workspace_id=? AND customer_id=? "
                             "AND kind='mobile' AND verified=1 AND preferred=1 ORDER BY id",
                             (workspace,card['customer_id'])))
    if len(contacts)>1:
        raise BankError('REMITTANCE_SENDER_UNAVAILABLE','当前客户有多条首选联系电话，请先核对客户档案。',422)
    result={'sender_name':customer['display_name'],'sender_phone':contacts[0]['masked_value'] if contacts else ''}
    for key,value in result.items():
        if (not isinstance(value,str) or len(value)>64 or value!=value.strip()
                or any(ord(char)<32 or ord(char)==127 for char in value) or (key=='sender_name' and not value)):
            raise BankError('REMITTANCE_SENDER_UNAVAILABLE','当前客户的姓名或已存联系方式无法核对。',422)
    return result


def canonical_payload(bank,db,workspace,payload):
    """兼容旧请求形状，但客户端姓名/电话不进入确认内容或汇款单。"""
    from .service import strict_fields
    form=payload.get('remittance')
    strict_fields(form,FIELDS,FIELDS-{'sender_name','sender_phone','message'})
    canonical={**form,'message':form.get('message','')}
    previous=db.execute('SELECT payload,state FROM operations WHERE workspace_id=? AND idempotency_key=?',
                        (workspace,payload['idempotency_key'])).fetchone()
    if previous and previous['state'] in ('succeeded','scheduled','failed','cancelled','expired','invalidated'):
        stored=json.loads(previous['payload']).get('remittance')
        if isinstance(stored,dict) and all(key in stored for key in ('sender_name','sender_phone')):
            # 已接受/关闭操作重试保留历史身份；Bank.prepare仍比对其余完整payload的原hash。
            canonical.update({key:stored[key] for key in ('sender_name','sender_phone')})
            return {**payload,'remittance':canonical}
    canonical.update(sender_identity(bank,db,workspace,payload['account_id'],form['card_id']))
    return {**payload,'remittance':canonical}


def validate(bank, db, workspace, payload, *, exclude_operation=None):
    from .service import BankError, integer, strict_fields
    form=payload.get('remittance')
    strict_fields(form,FIELDS,FIELDS)
    limits={'card_id':128,'sender_name':64,'sender_phone':64,'recipient_name':64,
            'recipient_account':32,'recipient_bank':100,'recipient_branch':100,'purpose':100,'message':240,'mode':16}
    for key,maximum in limits.items():
        value=form[key]
        if (not isinstance(value,str) or len(value)>maximum or value!=value.strip()
                or any(ord(char)<32 or ord(char)==127 for char in value) or (key not in ('message','sender_phone') and not value)):
            raise BankError('INVALID_REMITTANCE_FIELD',key+'格式无效。')
    if form['mode'] not in ('realtime','ordinary','next_day'):
        raise BankError('INVALID_REMITTANCE_MODE','汇款方式仅支持实时、普通或次日。')
    if not re.fullmatch(r'[0-9]{10,32}',form['recipient_account']) or not form['recipient_account'].strip('0'):
        raise BankError('INVALID_REMITTANCE_ACCOUNT','请提供完整的10至32位数字收款账号。')
    if 'beneficiary_id' in payload:
        raise BankError('INVALID_FIELDS','完整汇款资料与既有收款人ID不能混用。')
    amount=integer(payload.get('amount_minor'),'汇款金额',1)
    account=bank._account(db,workspace,payload['account_id'])
    ws=bank._workspace(db,workspace)
    card=bank._card(db,workspace,form['card_id'])
    if card['account_id']!=account['id']:
        raise BankError('REMITTANCE_CARD_MISMATCH','付款银行卡不属于所选账户。',422)
    sender=sender_identity(bank,db,workspace,account['id'],card['id'])
    if any(form[key]!=value for key,value in sender.items()):
        raise BankError('REMITTANCE_SENDER_MISMATCH','付款银行卡绑定的客户姓名或联系方式已变化，请重新核对汇款。',422)
    if card['type']!='debit' or card['status']!='active' or card['expires_on']<ws['as_of']:
        raise BankError('REMITTANCE_CARD_UNAVAILABLE','仅可使用未挂失、未过期的有效借记卡。',422)
    details=db.execute('SELECT * FROM account_details WHERE workspace_id=? AND account_id=?',
                       (workspace,account['id'])).fetchone()
    if not details or details['status']!='active':
        raise BankError('REMITTANCE_ACCOUNT_UNAVAILABLE','付款账户已冻结、关闭或未完成模拟接入。',422)
    owner=db.execute("SELECT 1 FROM signatories s JOIN customers c ON c.workspace_id=s.workspace_id AND c.id=s.customer_id "
                     "WHERE s.workspace_id=? AND s.account_id=? AND s.customer_id=? AND s.status='active' "
                     "AND s.role IN ('owner','authorized') AND c.status='active'",
                     (workspace,account['id'],card['customer_id'])).fetchone()
    any_signatory=db.execute('SELECT 1 FROM signatories WHERE workspace_id=? AND account_id=?',(workspace,account['id'])).fetchone()
    active_customer=db.execute("SELECT 1 FROM customers WHERE workspace_id=? AND id=? AND status='active'",(workspace,card['customer_id'])).fetchone()
    fallback=bool(active_customer) and not any_signatory and connected_owner(db,workspace,account,card,details)
    if not owner and not fallback:
        raise BankError('REMITTANCE_PERMISSION_REQUIRED','当前银行卡持有人没有有效账户付款权限。',403)
    if account['currency']!='CNY':
        raise BankError('CURRENCY_UNAVAILABLE','模拟汇款仅支持人民币。',422)
    held=bank._active_holds(db,workspace,account['id'])
    if exclude_operation:
        own=db.execute("SELECT amount_minor FROM navren_remittances WHERE workspace_id=? AND operation_id=? "
                       "AND account_id=? AND status='scheduled'",(workspace,exclude_operation,account['id'])).fetchone()
        if own:held-=own['amount_minor']
    available=max(0,account['balance_minor']-held)
    if amount>available:
        raise BankError('INSUFFICIENT_AVAILABLE','汇款金额超过扣除实际冻结资金后的可支配余额。',422)
    return {'account':dict(account),'card':dict(card),'amount_minor':amount,'available_minor':available,
            'permission_basis':'active_signatory' if owner else 'connected_opening_owner',
            'remittance':dict(form),'beneficiary':{'id':None,'display_name':form['recipient_name'],
                'bank_name':form['recipient_bank'],'currency':'CNY','verified':1,'status':'active'}}


def beneficiary(bank,db,workspace,form):
    return bank._insert_archive_row(db,'beneficiaries',workspace,{
        'display_name':form['recipient_name'],'alias':'模拟汇款收款人','masked_mobile':'',
        'masked_account':'*'*(len(form['recipient_account'])-4)+form['recipient_account'][-4:],
        'bank_name':form['recipient_bank'],'currency':'CNY','verified':1,'status':'active'})


def save(bank,db,workspace,operation,payload,result):
    form=payload['remittance']
    fields={'operation_id':operation,'workspace_id':workspace,'account_id':payload['account_id'],**form,
        'amount_minor':payload['amount_minor'],'execute_on':result['execute_on'],'execute_at':result['execute_at'],'status':result['status'],
        'scheduled_payment_id':result.get('scheduled_payment_id'),'payment_id':result.get('payment_id'),
        'transaction_id':result.get('transaction_id'),'balance_before_minor':result['balance_before_minor'],
        'balance_after_minor':result['balance_after_minor'],'failure_reason':None,
        'created_at':bank.clock(),'updated_at':bank.clock()}
    db.execute('INSERT INTO navren_remittances('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',tuple(fields.values()))


def finish(bank,db,row,result):
    from .service import encoded
    db.execute('UPDATE navren_remittances SET status=?,payment_id=?,transaction_id=?,balance_before_minor=?,balance_after_minor=?,'
               'failure_reason=?,updated_at=? WHERE operation_id=? AND workspace_id=?',
               (result['status'],result.get('payment_id'),result.get('transaction_id'),result['balance_before_minor'],
                result['balance_after_minor'],result.get('failure_reason'),bank.clock(),row['operation_id'],row['workspace_id']))
    db.execute('UPDATE operations SET state=?,result=?,verification_hash=NULL WHERE id=? AND workspace_id=?',
               (result['status'],encoded(result),row['operation_id'],row['workspace_id']))


def public(row):
    result=dict(row)
    result.update(remittance_id=row['operation_id'],environment='simulation',
                  held_minor=row['amount_minor'] if row['status']=='scheduled' else 0,
                  notice='仅为本机模拟汇款，未向真实银行或收款账户发送资金。')
    return result


def preview(bank,db,workspace,operation):
    from .service import BankError
    import json
    op=bank._operation(db,workspace,operation);payload=json.loads(op['payload'])
    if 'remittance' not in payload:raise BankError('NOT_FOUND','该操作不是完整模拟汇款单。',404)
    row=db.execute('SELECT * FROM navren_remittances WHERE workspace_id=? AND operation_id=?',(workspace,operation)).fetchone()
    if row:
        result=public(row);receipt=json.loads(op['result']) if op['result'] else {}
        account=bank._account(db,workspace,row['account_id'])
        result.update(available_minor=max(0,account['balance_minor']-bank._active_holds(db,workspace,account['id'])),
                      available_after_minor=receipt.get('available_after_minor'),
                      execute_business_on=payload.get('next_due_on'),business_on=receipt.get('business_on'),
                      processed_at=receipt.get('processed_at'))
        return result
    account=bank._account(db,workspace,payload['account_id'])
    execute_at,execute_on=execution_time(bank,payload['remittance']['mode'],now=op['created_at'])
    return {'remittance_id':operation,'operation_id':operation,'account_id':payload['account_id'],**payload['remittance'],
        'amount_minor':payload['amount_minor'],'execute_on':execute_on,'execute_at':execute_at,'execute_business_on':payload.get('next_due_on'),
        'status':op['state'],'payment_id':None,'transaction_id':None,'scheduled_payment_id':None,'held_minor':0,
        'balance_before_minor':account['balance_minor'],'balance_after_minor':account['balance_minor'],
        'available_after_minor':max(0,account['balance_minor']-bank._active_holds(db,workspace,account['id'])),
        'environment':'simulation','notice':'模拟汇款尚未执行，未向真实银行发送资金。'}
