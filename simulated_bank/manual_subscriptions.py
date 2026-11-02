"""用户明确登记的本机模拟订阅；不验证、联系或取消真实商户和银行。"""
import hashlib
import json
import re
import uuid
from datetime import date

from .service import BankError, integer, strict_fields

SOURCE_ID = 'navren-user-entered-simulation-v1'
SOURCE = (SOURCE_ID, '用户登记的本机模拟记录', '1', '', 'user_entered',
          '仅用于本机模拟，不构成真实商户合同、商户回执或银行授权')


def _text(value, label):
    if not isinstance(value, str):
        raise BankError('INVALID_FIELDS', label+'须为文字。')
    result = value.strip()
    if not 1 <= len(result) <= 100 or any(ord(ch) < 32 or ord(ch) == 127 for ch in result):
        raise BankError('INVALID_FIELDS', label+'须为1至100个可见字符。')
    return result


def register(bank, workspace, payload):
    fields = ('account_id','merchant','plan_name','amount_minor','cycle','next_renewal_on',
              'expected_version','idempotency_key','confirmed')
    strict_fields(payload, fields, fields)
    if payload['confirmed'] is not True:
        raise BankError('CONFIRMATION_REQUIRED','登记模拟记录前须明确确认。')
    account = _text(payload['account_id'],'账户')
    merchant = _text(payload['merchant'],'商户名称')
    plan = _text(payload['plan_name'],'记录名称')
    amount = integer(payload['amount_minor'],'金额')
    if payload['cycle'] not in ('monthly','yearly'):
        raise BankError('INVALID_FIELDS','请选择每月或每年的模拟周期。')
    raw_date = payload['next_renewal_on']
    try:
        next_day = date.fromisoformat(raw_date) if isinstance(raw_date,str) else None
    except ValueError:
        next_day = None
    if next_day is None or next_day.isoformat() != raw_date:
        raise BankError('INVALID_FIELDS','请选择有效的下次日期。')
    version = integer(payload['expected_version'],'资料版本',0,2**53-1)
    key = payload['idempotency_key']
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',key):
        raise BankError('INVALID_FIELDS','登记请求标识无效。')
    stable = json.dumps([account,merchant,plan,amount,payload['cycle'],raw_date,version],
                        ensure_ascii=False,separators=(',',':'))
    fingerprint = hashlib.sha256(stable.encode()).hexdigest()
    namespace = uuid.uuid5(uuid.NAMESPACE_URL,'navren.manual-simulation/1:'+workspace+':'+key)
    sub_id, mandate_id, merchant_id = (uuid.uuid5(namespace,label).hex for label in ('subscription','mandate','merchant'))
    with bank.transaction() as db:
        ws = bank._workspace(db,workspace)
        bank.permissions.write_allowed(db,workspace)
        account_row = bank._account(db,workspace,account)
        if account_row['currency'] != 'CNY':
            raise BankError('UNSUPPORTED_CURRENCY','模拟订阅仅支持人民币账户。',422)
        existing = db.execute('SELECT details FROM subscriptions WHERE workspace_id=? AND id=?',(workspace,sub_id)).fetchone()
        if existing:
            details = json.loads(existing['details'])
            if details.get('registration_fingerprint') != fingerprint or details.get('record_origin') != 'user_entered_simulation':
                raise BankError('IDEMPOTENCY_CONFLICT','同一登记请求不能更改内容。',409)
            return {'subscription_id':sub_id,'created':False,'workspace_id':workspace,
                    'version':ws['version'],'notice':'本机模拟记录；没有联系真实商户或银行。'}
        if ws['version'] != version:
            raise BankError('STALE_CONFIRMATION','账户资料已变化，请重新核对后登记。',409)
        if next_day < date.fromisoformat(ws['as_of']):
            raise BankError('INVALID_FIELDS','下次日期不能早于当前业务日期。')
        source = db.execute('SELECT id,name,version,url,license,usage FROM data_sources WHERE id=?',(SOURCE_ID,)).fetchone()
        if source and tuple(source) != SOURCE:
            raise BankError('SOURCE_CONFLICT','模拟登记来源无法核对。',409)
        if not source:
            db.execute('INSERT INTO data_sources(id,name,version,url,license,usage) VALUES(?,?,?,?,?,?)',SOURCE)
        details = json.dumps({'record_origin':'user_entered_simulation',
                              'registration_fingerprint':fingerprint},
                             ensure_ascii=False,separators=(',',':'))
        db.execute('INSERT INTO merchants(id,workspace_id,source_id,data_origin,details,name,mcc,category,channel) VALUES(?,?,?,?,?,?,?,?,?)',
                   (merchant_id,workspace,SOURCE_ID,'qingzhou_synthetic',details,merchant,'unknown','用户登记模拟','local_manual'))
        db.execute('INSERT INTO mandates(id,workspace_id,account_id,merchant,amount_minor,status,next_charge_date,merchant_contract_status) VALUES(?,?,?,?,?,?,?,?)',
                   (mandate_id,workspace,account,merchant,amount,'active',raw_date,'user_entered_simulation'))
        db.execute('INSERT INTO subscriptions(id,workspace_id,source_id,data_origin,details,account_id,mandate_id,merchant_id,plan_name,amount_minor,cycle,next_renewal_on,merchant_status,cancellation_route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sub_id,workspace,SOURCE_ID,'qingzhou_synthetic',details,account,mandate_id,merchant_id,
                    plan,amount,payload['cycle'],raw_date,'active','仅本机模拟；不会联系真实商户'))
        bank._bump(db,workspace)
        bank._audit(db,workspace,'manual_simulation_subscription_registered',
                    {'subscription_id':sub_id,'record_origin':'user_entered_simulation'})
        return {'subscription_id':sub_id,'created':True,'workspace_id':workspace,
                'version':version+1,'notice':'本机模拟记录；没有联系真实商户或银行。'}
