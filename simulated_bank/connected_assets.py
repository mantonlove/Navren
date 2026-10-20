"""模拟银行连接：账户与执行共用银行账本，封存历史只作开账及流水原件。

迁移只允许没有金融操作的四画像工作区，必须由显式 CLI 执行。
净值来自版本化、确定性的模拟提供者；不会把历史未知份额说成真实份额。
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from .service import BankError, encoded

SCHEMA_VERSION = 'navren.connected-assets.v1'
MIGRATION_VERSION = 'connected-assets-opening.v1'
NAV_VERSION = 'navren.mock-nav.v1'
TICK_SECONDS = 60
SHANGHAI = ZoneInfo('Asia/Shanghai')
TRANSACTION_KINDS = ('income','expense','refund','transfer','internal_transfer',
                     'investment_purchase','investment_redemption','loan_repayment','insurance_premium')
INSTITUTIONS = {'工行合成连接':'中国工商银行', '建行合成连接':'中国建设银行',
                '农行合成连接':'中国农业银行', '邮储合成连接':'中国邮政储蓄银行',
                '招行合成连接':'招商银行', '农商行合成连接':'浙江农商联合银行',
                '中信证券合成连接':'中信证券', '支付宝合成连接':'支付宝', '微信支付合成连接':'微信支付'}
DDL = (
    '''CREATE TABLE IF NOT EXISTS connected_asset_migrations(
       workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),subject_id TEXT NOT NULL,
       migration_version TEXT NOT NULL,dataset_version TEXT NOT NULL,source_path TEXT NOT NULL,
       source_sha256 TEXT NOT NULL,applied_at REAL NOT NULL,opening_as_of TEXT NOT NULL,
       opening_version INTEGER NOT NULL,snapshot TEXT NOT NULL,audit TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS connected_account_links(
       workspace_id TEXT NOT NULL,account_id TEXT NOT NULL,source_account_id TEXT NOT NULL,
       connection_id TEXT NOT NULL,account_type TEXT NOT NULL,
       PRIMARY KEY(workspace_id,account_id),UNIQUE(workspace_id,source_account_id),
       FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id))''',
    '''CREATE TABLE IF NOT EXISTS connected_card_links(
       workspace_id TEXT NOT NULL,card_id TEXT NOT NULL,source_card_id TEXT NOT NULL,
       facility_id TEXT,role TEXT NOT NULL,
       PRIMARY KEY(workspace_id,card_id),UNIQUE(workspace_id,source_card_id),
       FOREIGN KEY(workspace_id,card_id) REFERENCES cards(workspace_id,id))''',
    '''CREATE TABLE IF NOT EXISTS connected_position_seeds(
       workspace_id TEXT NOT NULL,product_id TEXT NOT NULL,source_position_id TEXT NOT NULL,
       initial_principal_minor INTEGER NOT NULL,initial_value_minor INTEGER NOT NULL,
       seed TEXT NOT NULL,PRIMARY KEY(workspace_id,product_id),
       FOREIGN KEY(product_id) REFERENCES products(id))''',
)


def _hash_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _id(workspace, kind, source):
    return 'connected-' + kind + '-' + workspace + '-' + hashlib.sha256(source.encode()).hexdigest()[:16]


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec='seconds')


def _integer(value, name, signed=False):
    if type(value) is not int or not -(2**53-1 if signed else 0) <= value <= 2**53-1:
        raise BankError('CONNECTED_SOURCE_INVALID', name + '不是安全的整数分。', 422)
    return value


def _source(financial_database):
    from financial_data_service import FinancialDataService
    from financial_data_service.presentation import FinancialPresentationService
    path = Path(financial_database).expanduser().resolve()
    service = FinancialDataService(path)
    if service.metadata.get('source') != 'navren_synthetic':
        raise BankError('CONNECTED_SOURCE_INVALID', '开账来源必须是封存模拟财务库。', 422)
    with closing(service._connect()) as db:
        subjects = [dict(row) for row in db.execute('SELECT * FROM subjects ORDER BY subject_id')]
    snapshots = {}
    for subject in subjects:
        result = FinancialPresentationService(service).query(subject['subject_id'], {'view':'overview'})
        if result.get('error'):
            raise BankError('CONNECTED_SOURCE_INVALID', result['error']['message'], 422)
        # 已结清贷款仍须保留主体/账户关联；展示聚合原来会过滤关闭合同。
        with closing(service._connect()) as db:
            contracts = [dict(row, details=json.loads(row['details'])) for row in db.execute(
                'SELECT * FROM contracts WHERE subject_id=? ORDER BY contract_id',(subject['subject_id'],))]
        result['data']['contracts'] = contracts
        snapshots[subject['subject_id']] = result['data']
    return service, subjects, snapshots, _hash_file(path)


def ensure_registered_schema(db):
    """只扩展模板主体的一对多实例基数，不改原迁移记录或金融对象。"""
    if _table(db,'connected_asset_migrations'):
        unique_subject = any(row[2] and [c[2] for c in db.execute('PRAGMA index_info('+row[1]+')')]==['subject_id']
                             for row in db.execute('PRAGMA index_list(connected_asset_migrations)'))
        if unique_subject:
            db.execute('ALTER TABLE connected_asset_migrations RENAME TO connected_asset_migrations_uss_old')
            db.execute(DDL[0])
            db.execute('INSERT INTO connected_asset_migrations SELECT * FROM connected_asset_migrations_uss_old')
            db.execute('DROP TABLE connected_asset_migrations_uss_old')
    for statement in DDL:db.execute(statement)


def _plan_workspace(db, subject, snapshot, service, source_hash, *, registry_table='navren_demo_accounts'):
    workspace, uid = subject['workspace_id'], subject['subject_id']
    if registry_table not in ('navren_demo_accounts','registered_demo_sources'):
        raise ValueError('unsupported_template_registry')
    if not _table(db, registry_table):
        raise BankError('CONNECTED_BINDING_MISSING', '现役库未登记四画像身份。', 409)
    registry = db.execute('SELECT * FROM '+registry_table+' WHERE workspace_id=? AND user_id=?', (workspace, uid)).fetchone()
    ws = db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone()
    if not registry or registry['status'] != 'ready' or not ws:
        raise BankError('CONNECTED_BINDING_MISSING', '来源主体与现役工作区无法关联。', 409)
    if json.loads(registry['source_hashes']).get('financial_data.sqlite3') != service.metadata.get('source_database_sha256'):
        raise BankError('CONNECTED_SOURCE_CHANGED', '开账来源与现役画像来源不同。', 409)
    if _table(db, 'connected_asset_migrations'):
        previous = db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?', (workspace,)).fetchone()
        if previous:
            if previous['migration_version'] != MIGRATION_VERSION or previous['dataset_version'] != service.dataset_version or previous['source_sha256'] != source_hash:
                raise BankError('CONNECTED_MIGRATION_CONFLICT', '该工作区已经采用其他开账版本，不能重复初始化。', 409)
            return {'workspace_id':workspace, 'subject_id':uid, 'already_migrated':True, 'version':ws['version']}
    for table in ('operations','orders','transactions','fund_holds','payment_instructions','scheduled_payments'):
        if _table(db, table) and db.execute('SELECT 1 FROM '+table+' WHERE workspace_id=? LIMIT 1', (workspace,)).fetchone():
            raise BankError('CONNECTED_HAS_FINANCIAL_OPERATIONS', '已有金融操作或资金任务，不能自动迁移；原记录保持不变。', 409)
    if db.execute('SELECT 1 FROM cards WHERE workspace_id=? LIMIT 1', (workspace,)).fetchone():
        raise BankError('CONNECTED_CARD_CONFLICT', '现役卡片档案已存在，不能覆盖。', 409)
    mapping = json.loads(registry['account_mapping'])
    old = {row['id']:dict(row) for row in db.execute('SELECT * FROM accounts WHERE workspace_id=?', (workspace,))}
    if len(old) != 3 or set(old) != set(mapping.values()):
        raise BankError('CONNECTED_ACCOUNT_CONFLICT', '现役现金账户不是完整的三个已登记资金池。', 409)
    accounts = snapshot['accounts']
    if len({row['account_id'] for row in accounts}) != len(accounts):
        raise BankError('CONNECTED_SOURCE_INVALID', '开账账户重复。', 422)
    current_mapping, pool_cash, pool_credit = {}, {}, {}
    for account in accounts:
        role = account['account_id'].rsplit(':',1)[-1]
        pool = account['source_pool_id']
        if pool not in mapping or account['currency'] != 'CNY':
            raise BankError('CONNECTED_BINDING_MISSING', '分账户资金池或币种无法关联。', 409)
        value = _integer(account['balance_minor'], '开账余额', signed=True)
        credit = account['account_type'] == 'credit_card'
        if credit:
            if value > 0:
                raise BankError('CONNECTED_SOURCE_INVALID', '信用负债方向错误。', 422)
            pool_credit[pool] = pool_credit.get(pool,0)-value
        else:
            if value < 0:
                raise BankError('CONNECTED_SOURCE_INVALID', '现金开账余额不能为负。', 422)
            pool_cash[pool] = pool_cash.get(pool,0)+value
        current_mapping[account['account_id']] = mapping[pool] if role in ('daily','operating','reserve') else _id(workspace,'account',account['account_id'])
    if len(set(current_mapping.values())) != len(current_mapping):
        raise BankError('CONNECTED_SOURCE_INVALID', '规范化账户映射重复。', 422)
    adjustments = []
    for pool, runtime_id in mapping.items():
        balance = old[runtime_id]['balance_minor']
        if old[runtime_id]['opening_balance_minor'] != balance or pool_cash.get(pool,0)-pool_credit.get(pool,0) != balance:
            raise BankError('CONNECTED_OPENING_MISMATCH', '分账户现金减信用负债与现役资金池无法对账。', 409)
        adjustments.append({'source_pool_id':pool, 'runtime_account_id':runtime_id,
                            'old_cash_minor':balance, 'new_gross_cash_minor':pool_cash.get(pool,0),
                            'credit_reclassification_minor':pool_credit.get(pool,0), 'net_difference_minor':0})
    holdings = []
    positions = {row['asset_id']:row for row in snapshot['positions']}
    for row in db.execute('SELECT h.*,p.data FROM holdings h JOIN products p ON p.id=h.product_id WHERE h.workspace_id=?', (workspace,)):
        holding, product = dict(row), json.loads(row['data'])
        source_asset = product.get('source_asset_id')
        position = positions.pop(source_asset, None)
        if not product.get('historical_synthetic_position') or not position or holding['amount_minor'] != position['cost_basis_minor']:
            raise BankError('CONNECTED_POSITION_CONFLICT', '现役持仓本金与来源持仓无法唯一关联。', 409)
        lot_total = db.execute('SELECT coalesce(sum(remaining_minor),0) FROM investment_lots WHERE workspace_id=? AND account_id=? AND product_id=?',
                               (workspace,holding['account_id'],holding['product_id'])).fetchone()[0]
        if lot_total != holding['amount_minor']:
            raise BankError('CONNECTED_POSITION_CONFLICT', '现役持仓批次与本金不一致。', 409)
        holdings.append({'product_id':holding['product_id'], 'old_account_id':holding['account_id'],
                         'account_id':current_mapping[position['account_id']], 'position':position})
    if positions:
        raise BankError('CONNECTED_POSITION_CONFLICT', '来源持仓未完整关联到现役账本。', 409)
    loans = []
    for loan in db.execute('SELECT * FROM loans WHERE workspace_id=?', (workspace,)):
        source_id = json.loads(loan['details']).get('source_loan_id','')
        source_contract = next((r for r in snapshot['contracts'] if r['contract_type']=='loan' and r['contract_id'].rsplit(':',1)[-1]==source_id.rsplit(':',1)[-1]), None)
        if not source_contract or source_contract['principal_minor'] != loan['outstanding_minor']:
            raise BankError('CONNECTED_LOAN_CONFLICT', '贷款本金与来源合同无法关联。', 409)
        loans.append({'loan_id':loan['id'], 'contract_id':source_contract['contract_id'],
                      'account_id':current_mapping[source_contract['account_id']]})
    if len(loans) != sum(r['contract_type']=='loan' for r in snapshot['contracts']):
        raise BankError('CONNECTED_LOAN_CONFLICT', '来源贷款未完整关联到现役账本。', 409)
    total_cash = sum(pool_cash.values())
    credit = sum(pool_credit.values())
    if total_cash != snapshot['summary']['cash_minor'] or credit != snapshot['summary']['credit_card_outstanding_minor']:
        raise BankError('CONNECTED_OPENING_MISMATCH', '来源现金与信用负债汇总不一致。', 409)
    return {'workspace_id':workspace,'subject_id':uid,'already_migrated':False,'version':ws['version'],
            'opening_as_of':service.metadata['end_date'],'mapping':current_mapping,'adjustments':adjustments,
            'holdings':holdings,'loans':loans,'old_cash_minor':sum(row['balance_minor'] for row in old.values()),
            'new_cash_minor':total_cash,'credit_card_outstanding_minor':credit}


def _archive_insert(db, table, workspace, ident, fields, details=None):
    value = {'id':ident,'workspace_id':workspace,'source_id':'qingzhou','data_origin':'qingzhou_synthetic',
             'details':encoded({'source':'navren_simulated_bank','migration_version':MIGRATION_VERSION,**(details or {})}),**fields}
    db.execute('INSERT INTO '+table+'('+','.join(value)+') VALUES('+','.join('?' for _ in value)+')', tuple(value.values()))


def _apply_workspace(db, plan, snapshot, service, source_hash, now):
    workspace, uid, mapping = plan['workspace_id'], plan['subject_id'], plan['mapping']
    customer = db.execute('SELECT id FROM customers WHERE workspace_id=?', (workspace,)).fetchone()
    if not customer:
        raise BankError('CONNECTED_BINDING_MISSING', '现役客户主体不存在。', 409)
    branches = {}
    for connection in snapshot['connections']:
        ident = _id(workspace,'branch',connection['connection_id'])
        bank_name = INSTITUTIONS.get(connection['institution_name'],connection['institution_name'])
        _archive_insert(db,'bank_branches',workspace,ident,{'name':bank_name+'模拟接入','bank_name':bank_name,
                        'bank_code':'MOCK-'+hashlib.sha256(bank_name.encode()).hexdigest()[:8].upper(),
                        'city':'未提供','channel':'simulation'}, {'source_connection_id':connection['connection_id']})
        branches[connection['connection_id']] = ident
    for account in snapshot['accounts']:
        ident = mapping[account['account_id']]
        credit = account['account_type']=='credit_card'
        value = 0 if credit else account['balance_minor']
        if db.execute('SELECT 1 FROM accounts WHERE workspace_id=? AND id=?',(workspace,ident)).fetchone():
            db.execute('UPDATE accounts SET name=?,opening_balance_minor=?,balance_minor=? WHERE workspace_id=? AND id=?',
                       (account['name'],value,value,workspace,ident))
            db.execute('UPDATE account_details SET branch_id=?,masked_number=? WHERE workspace_id=? AND account_id=?',
                       (branches[account['connection_id']],'账户尾号 '+str(int(hashlib.sha256(ident.encode()).hexdigest()[:8],16)%10000).zfill(4),workspace,ident))
        else:
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)',(ident,workspace,account['name'],'CNY',value,value))
            _archive_insert(db,'account_details',workspace,_id(workspace,'details',ident),
                            {'customer_id':customer['id'],'account_id':ident,'branch_id':branches[account['connection_id']],
                             'masked_number':'账户尾号 '+str(int(hashlib.sha256(ident.encode()).hexdigest()[:8],16)%10000).zfill(4),
                             'type':'current','status':'active','opened_on':account['opened_on'], 'interest_bps':0,'overdraft_minor':0},
                            {'source_account_id':account['account_id'],'account_type':account['account_type']})
            if not credit:
                for kind, limit in (('transfer_daily',5000000),('transfer_single',2000000),('cash_daily',1000000)):
                    _archive_insert(db,'account_limits',workspace,_id(workspace,'limit',ident+kind),
                                    {'account_id':ident,'kind':kind,'limit_minor':limit,'used_minor':0,'effective_on':plan['opening_as_of']})
        db.execute('INSERT INTO connected_account_links VALUES(?,?,?,?,?)',
                   (workspace,ident,account['account_id'],account['connection_id'],account['account_type']))
    active_cards = [r for r in snapshot['cards'] if r['status'] in ('active','lost')]
    primary = {r['facility_id']:r for r in active_cards if r.get('facility_id') and r['role']=='primary' and r['status']=='active'}
    source_card_map = {}
    facilities = {r['facility_id']:r for r in snapshot['credit_facilities']}
    for card in active_cards:
        ident = _id(workspace,'card',card['card_id'])
        facility = facilities.get(card.get('facility_id'))
        debt = facility['outstanding_minor'] if facility and primary.get(card['facility_id'])==card else 0
        limit = facility['limit_minor'] if facility else 0
        tail = re.search(r'(\d{4})$',card['masked_number'])
        if not tail:
            raise BankError('CONNECTED_SOURCE_INVALID', '来源卡号尾号无效。', 422)
        issued = card['issued_on']
        expires = str(int(issued[:4])+10)+issued[4:]
        _archive_insert(db,'cards',workspace,ident,{'customer_id':customer['id'],'account_id':mapping[card['account_id']],
                        'type':'credit' if card['card_type']=='credit' else 'debit','network':'UnionPay',
                        'last_four':tail[1],'status':card['status'],'issued_on':issued,'expires_on':expires,
                        'credit_limit_minor':limit,'outstanding_minor':debt},
                        {'source_card_id':card['card_id'],'expiry_origin':'versioned_simulated_card_terms'})
        db.execute('INSERT INTO connected_card_links VALUES(?,?,?,?,?)',
                   (workspace,ident,card['card_id'],card.get('facility_id'),card['role']))
        source_card_map[card['card_id']] = ident
        for kind in ('online','overseas','contactless','single_limit'):
            _archive_insert(db,'card_controls',workspace,_id(workspace,'control',ident+kind),
                            {'card_id':ident,'control_type':kind,'enabled':0 if card['status']=='lost' else 1,
                             'limit_minor':min(limit,2000000) if facility else 2000000,'updated_on':plan['opening_as_of']})
    with closing(service._connect()) as source_db:
        statements = [dict(r) for r in source_db.execute('SELECT * FROM card_statements WHERE subject_id=? ORDER BY period_start',(uid,))]
    for facility_id, card in primary.items():
        ident = source_card_map[card['card_id']]
        rows = [row for row in statements if row['account_id']==card['account_id']]
        open_debt = 0
        for row in rows:
            debt = row['statement_minor']-row['paid_minor']
            if row['status']=='open':
                open_debt += debt
            _archive_insert(db,'credit_statements',workspace,_id(workspace,'statement',row['statement_id']),
                            {'card_id':ident,'period_start':row['period_start'],'period_end':row['period_end'],
                             'total_due_minor':row['statement_minor'],'minimum_due_minor':(debt+9)//10,
                             'due_on':row['due_on'],'paid_minor':row['paid_minor'],'status':'open' if debt else 'paid'},
                            {'source_statement_id':row['statement_id'],'statement_state':row['status']})
        remainder = facilities[facility_id]['outstanding_minor']-open_debt
        if remainder < 0:
            raise BankError('CONNECTED_OPENING_MISMATCH','信用账单余额超过信用负债。',409)
        if remainder:
            _archive_insert(db,'credit_statements',workspace,_id(workspace,'unbilled',facility_id),
                            {'card_id':ident,'period_start':plan['opening_as_of'][:7]+'-01','period_end':plan['opening_as_of'],
                             'total_due_minor':remainder,'minimum_due_minor':0,'due_on':'2026-11-10','paid_minor':0,'status':'open'},
                            {'statement_state':'unbilled','not_issued_statement':True,'origin':'opening_unbilled_credit_balance'})
    for holding in plan['holdings']:
        product, old, target, position = holding['product_id'], holding['old_account_id'], holding['account_id'], holding['position']
        db.execute('UPDATE holdings SET account_id=? WHERE workspace_id=? AND account_id=? AND product_id=?',(target,workspace,old,product))
        db.execute('UPDATE investment_lots SET account_id=? WHERE workspace_id=? AND account_id=? AND product_id=?',(target,workspace,old,product))
        seed = hashlib.sha256((MIGRATION_VERSION+':'+workspace+':'+product).encode()).hexdigest()
        db.execute('INSERT INTO connected_position_seeds VALUES(?,?,?,?,?,?)',
                   (workspace,product,position['position_id'],position['cost_basis_minor'],position['book_value_minor'],seed))
    for loan in plan['loans']:
        row = db.execute('SELECT details FROM loans WHERE workspace_id=? AND id=?',(workspace,loan['loan_id'])).fetchone()
        details = json.loads(row['details']);details['connected_contract_id'] = loan['contract_id']
        db.execute('UPDATE loans SET repayment_account_id=?,details=? WHERE workspace_id=? AND id=?',
                   (loan['account_id'],encoded(details),workspace,loan['loan_id']))
    audit = {key:value for key,value in plan.items() if key not in ('holdings','mapping','loans')}
    audit.update(account_mapping=mapping,operation_replayed=False,credentials_unchanged=True,
                 opening_rule='gross_cash_minus_credit_equals_old_pool_cash',mock_quantity_origin=MIGRATION_VERSION)
    db.execute('INSERT INTO connected_asset_migrations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (workspace,uid,MIGRATION_VERSION,service.dataset_version,str(service.database_path),source_hash,now,
                plan['opening_as_of'],plan['version'],encoded(snapshot),encoded(audit)))
    from .account_connections import SCHEMA as CONNECTION_SCHEMA,seed_defaults
    db.execute(CONNECTION_SCHEMA)
    seed_defaults(db,workspace,db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone(),now)
    db.execute('UPDATE workspaces SET version=version+1,as_of=? WHERE id=? AND version=?',
               (datetime.fromtimestamp(now,SHANGHAI).date().isoformat(),workspace,plan['version']))
    db.execute('INSERT INTO audit(workspace_id,operation_id,event,details,created_at) VALUES(?,NULL,?,?,?)',
               (workspace,'connected_assets_migrated',encoded(audit),now))


def migrate_connected_assets(bank_database, financial_database, *, apply=False, expected_versions=None, clock=time.time):
    """默认只读计划；apply原子采用，重复采用不会重置交易后余额。"""
    bank_path = Path(bank_database).expanduser().resolve()
    if not bank_path.is_file():
        raise BankError('CONNECTED_BANK_MISSING','现役银行库不存在。',409)
    service, subjects, snapshots, source_hash = _source(financial_database)
    if {s['subject_id'] for s in subjects} != {'tycoon','grain_farmer','working_landlady','retired_entrepreneur'}:
        raise BankError('CONNECTED_SOURCE_INVALID','开账来源必须完整覆盖四画像。',422)
    mode = 'rw' if apply else 'ro'
    db = sqlite3.connect(bank_path.as_uri()+'?mode='+mode,uri=True,timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if not apply:
        db.execute('PRAGMA query_only=ON')
    try:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        plans = [_plan_workspace(db,s,snapshots[s['subject_id']],service,source_hash) for s in subjects]
        for plan in plans:
            if expected_versions is not None and expected_versions.get(plan['workspace_id']) != plan['version']:
                raise BankError('CONNECTED_VERSION_CHANGED','现役账本版本已变化，迁移未采用。',409)
        if apply:
            for statement in DDL:
                db.execute(statement)
            for plan in plans:
                if not plan['already_migrated']:
                    _apply_workspace(db,plan,snapshots[plan['subject_id']],service,source_hash,clock())
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise BankError('CONNECTED_INVARIANT_FAILED','迁移关联校验失败。',409)
            for plan in plans:
                if not plan['already_migrated']:
                    for row in db.execute('SELECT * FROM accounts WHERE workspace_id=?',(plan['workspace_id'],)):
                        delta = db.execute('SELECT coalesce(sum(delta_minor),0) FROM transactions WHERE workspace_id=? AND account_id=?',(plan['workspace_id'],row['id'])).fetchone()[0]
                        if row['balance_minor'] != row['opening_balance_minor']+delta:
                            raise BankError('CONNECTED_INVARIANT_FAILED','迁移后的现金账户不能对账。',409)
            if _hash_file(service.database_path) != source_hash:
                raise BankError('CONNECTED_SOURCE_CHANGED','迁移期间来源发生变化。',409)
            db.commit()
        else:
            db.rollback()
        return {'migration_version':MIGRATION_VERSION,'dataset_version':service.dataset_version,'source_sha256':source_hash,
                'applied':apply,'changed':apply and any(not p['already_migrated'] for p in plans),
                'workspaces':[{key:value for key,value in plan.items() if key not in ('holdings','loans')} for plan in plans],
                'operation_replayed':False,'history_source_modified':False}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class ConnectedAssets:
    def __init__(self, gateway):
        self.g = gateway
        self.bank = gateway.bank

    def _binding(self, db, workspace):
        if not _table(db,'connected_asset_migrations'):
            raise BankError('CONNECTED_ASSETS_NOT_MIGRATED','银行资产连接尚未完成开账迁移。',503)
        row = db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
        if not row:
            raise BankError('CONNECTED_ASSETS_NOT_MIGRATED','当前账户尚未完成银行资产连接迁移。',503)
        subject = self.g.demo_accounts.subject_for(workspace)
        if row['subject_id'] != subject or row['migration_version'] != MIGRATION_VERSION:
            raise BankError('CONNECTED_IDENTITY_MISMATCH','银行资产连接与当前主体不一致。',409)
        guard = getattr(self.bank,'request_identity_guard',None)
        if guard:
            guard(db)
        return dict(row)

    def _envelope(self, db, workspace, binding, now, data):
        from user_memory import privacy_revision
        ws = self.bank._workspace(db,workspace)
        tick = int(now)//TICK_SECONDS
        version = ws['version']
        return {'schema_version':SCHEMA_VERSION,'source':'navren_simulated_bank','source_scope':'connected_bank',
                'environment':'simulation','read_only':True,'runtime_ledger_included':True,
                'workspace_binding':workspace,'subject_id':binding['subject_id'],'current_account_id':binding['subject_id'],
                'privacy_revision':privacy_revision(db,workspace),'version':version,'ledger_version':version,
                'dataset_version':binding['dataset_version']+':connected:'+str(version)+':'+NAV_VERSION+':'+str(tick),
                'migration_version':MIGRATION_VERSION,'as_of':ws['as_of'],
                'observed_at':_iso(now),'valuation_at':_iso(tick*TICK_SECONDS),'valuation_version':NAV_VERSION+':'+str(tick),
                'data':data,'evidence':{'provider':'navren_mock_bank','bank_integration':False,
                'runtime_operations_included':True,'opening_as_of':binding['opening_as_of'],
                'historical_dataset_version':binding['dataset_version'],
                'valuation_provider':'deterministic_simulation','valuation_rule_version':NAV_VERSION,
                'tick_seconds':TICK_SECONDS,'quantity_origin':'versioned_simulated_opening_not_real_historical_quantity'}}

    def overview(self, workspace):
        self.bank.process_due_remittances(workspace)
        now = self.bank.clock()
        with closing(self.bank.connect()) as db:
            db.execute('BEGIN')
            binding = self._binding(db,workspace)
            opening = json.loads(binding['snapshot'])
            data = deepcopy(opening)
            links = [dict(r) for r in db.execute('SELECT * FROM connected_account_links WHERE workspace_id=? ORDER BY account_id',(workspace,))]
            mapping = {row['source_account_id']:row['account_id'] for row in links}
            metadata = {r['account_id']:r for r in opening['accounts']}
            runtime_accounts = {r['id']:dict(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=?',(workspace,))}
            if set(runtime_accounts) != {r['account_id'] for r in links}:
                raise BankError('CONNECTED_ACCOUNT_INCOMPLETE','运行账户与银行连接目录不完整。',503)
            connection_states=self.g.account_connections.states(db,workspace)
            managed={r['connection_id']:r for r in connection_states}
            known={r['connection_id'] for r in data['connections']}
            local_metadata={r['connection_id']:json.loads(r['metadata']) for r in db.execute('SELECT connection_id,metadata FROM navren_account_connections WHERE workspace_id=?',(workspace,))}
            data['connections'] += [deepcopy(meta) for cid,meta in local_metadata.items() if cid not in known]
            data['account_connections']=connection_states
            for connection in data['connections']:
                connection.update(institution_name=INSTITUTIONS.get(connection['institution_name'],connection['institution_name']),
                                  status='connected',source='navren_simulated_bank')
                state=managed.get(connection['connection_id'])
                connection.update(binding_id=state['id'] if state else None,connection_status=state['status'] if state else 'connected',
                                  connection_active=state['active'] if state else True,real_connection=False)
                if state and not state['active']:connection['status']='disconnected'
            data['accounts'] = []
            for link in links:
                runtime = runtime_accounts[link['account_id']]
                original = metadata.get(link['source_account_id'])
                account = deepcopy(original) if original else {'subject_id':binding['subject_id'],
                    'connection_id':link['connection_id'],'account_type':link['account_type'],'currency':'CNY',
                    'status':'active','source_pool_id':None,'scope':'personal','opened_on':binding['opening_as_of'],
                    'details':{'source':'navren_simulated_bank','origin':'runtime_card_issuance'}}
                account.update(account_id=link['account_id'],id=link['account_id'],runtime_account_id=link['account_id'],
                               source_account_id=link['source_account_id'] if original else None,name=runtime['name'],balance_minor=runtime['balance_minor'],
                               opening_balance_minor=runtime['opening_balance_minor'],observed_at=_iso(now))
                state=managed.get(link['connection_id'])
                account.update(binding_id=state['id'] if state else None,connection_status=state['status'] if state else 'connected',
                               connection_active=state['active'] if state else True)
                if not original and state and state['source']=='local_initialized':
                    account['opened_on']=local_metadata[link['connection_id']]['opened_on']
                    account['details'].update(origin='runtime_account_connection',history_initialized=False,real_connection=False)
                held=self.bank._active_holds(db,workspace,link['account_id'])
                remittance_held=db.execute("SELECT coalesce(sum(amount_minor),0) FROM navren_remittances WHERE workspace_id=? AND account_id=? AND status='scheduled'",
                                          (workspace,link['account_id'])).fetchone()[0]
                account.update(held_minor=held,available_minor=max(0,runtime['balance_minor']-held),
                    available_balance_minor=max(0,runtime['balance_minor']-held),remittance_held_minor=remittance_held)
                data['accounts'].append(account)
            source_cards = {r['card_id']:r for r in opening['cards']}
            card_links = {r['card_id']:dict(r) for r in db.execute('SELECT * FROM connected_card_links WHERE workspace_id=?',(workspace,))}
            data['cards'], facilities = [], {}
            for row in db.execute('SELECT * FROM cards WHERE workspace_id=? ORDER BY issued_on,id',(workspace,)):
                card, link = dict(row), card_links.get(row['id'])
                if not link:
                    raise BankError('CONNECTED_CARD_INCOMPLETE','运行卡片缺少银行连接关联。',503)
                original = source_cards.get(link['source_card_id'],{})
                projected_card = {**original,'card_id':card['id'],'source_card_id':link['source_card_id'] if original else None,
                    'account_id':card['account_id'],'card_type':card['type'],'network':card['network'],'masked_number':'**** **** **** '+card['last_four'],
                    'status':card['status'],'issued_on':card['issued_on'],'expires_on':card['expires_on'],
                    'facility_id':link['facility_id'],'role':link['role'],'observed_at':_iso(now)}
                card_details = json.loads(card['details'])
                if card_details.get('origin') == 'user_card_registration' and card_details.get('registration_only') is True:
                    projected_card.update(registration_only=True,registered_on=card['issued_on'])
                    if card_details.get('issued_on_unknown') is True:
                        projected_card['issued_on'] = None
                    if card_details.get('expires_on_unknown') is True:
                        projected_card['expires_on'] = None
                data['cards'].append(projected_card)
                if link['facility_id']:
                    facility = facilities.setdefault(link['facility_id'],{'facility_id':link['facility_id'],'account_id':card['account_id'],
                                                                         'limit_minor':0,'outstanding_minor':0})
                    if facility['account_id'] != card['account_id']:
                        raise BankError('CONNECTED_CARD_INCOMPLETE','共享授信关联到不同账户。',503)
                    facility['limit_minor'] = max(facility['limit_minor'],card['credit_limit_minor'])
                    facility['outstanding_minor'] += card['outstanding_minor']
            data['credit_facilities'] = list(facilities.values())
            for account in data['accounts']:
                if account['account_type']=='credit_card':
                    account['balance_minor'] = -sum(f['outstanding_minor'] for f in facilities.values() if f['account_id']==account['account_id'])
                account['card_ids'] = [r['card_id'] for r in data['cards'] if r['account_id']==account['account_id']]
            for table, field in (('contracts','account_id'),('policies','payment_account_id'),('obligations','account_id')):
                for row in data[table]:
                    row[field] = mapping[row[field]]
            for loan in db.execute('SELECT * FROM loans WHERE workspace_id=?',(workspace,)):
                contract_id = json.loads(loan['details']).get('connected_contract_id')
                contract = next((r for r in data['contracts'] if r['contract_id']==contract_id),None)
                if not contract:
                    raise BankError('CONNECTED_LOAN_INCOMPLETE','运行贷款缺少已关联的合同。',503)
                contract.update(principal_minor=loan['outstanding_minor'],account_id=loan['repayment_account_id'])
            data['positions'] = []
            positions = {r['position_id']:r for r in opening['positions']}
            assets = {r['asset_id']:r for r in data['assets']}
            seeds = {r['product_id']:dict(r) for r in db.execute('SELECT * FROM connected_position_seeds WHERE workspace_id=?',(workspace,))}
            tick = int(now)//TICK_SECONDS
            for row in db.execute('SELECT h.*,p.data FROM holdings h JOIN products p ON p.id=h.product_id WHERE h.workspace_id=? AND h.amount_minor>0 ORDER BY h.account_id,h.product_id',(workspace,)):
                holding, product = dict(row), json.loads(row['data'])
                if holding['account_id'] not in runtime_accounts:
                    raise BankError('CONNECTED_POSITION_INCOMPLETE','持仓所属运行账户不存在。',503)
                seed = seeds.get(holding['product_id'])
                if seed:
                    position = deepcopy(positions[seed['source_position_id']])
                    initial_principal, initial_value = seed['initial_principal_minor'],seed['initial_value_minor']
                    if initial_principal <= 0:
                        raise BankError('CONNECTED_POSITION_INCOMPLETE','开账持仓本金无效。',503)
                    quantity = Decimal(initial_value)/Decimal(100)*Decimal(holding['amount_minor'])/Decimal(initial_principal)
                    seed_key = seed['seed']
                else:
                    # 确认购买的新运行头寸使用模拟本金单位份额，费用仍由原执行账本控制。
                    key = holding['account_id']+':'+holding['product_id']
                    asset_id, contract_id = _id(workspace,'asset',key),_id(workspace,'contract',key)
                    position = {'position_id':_id(workspace,'position',key),'asset_id':asset_id,'contract_id':contract_id,
                                'source_position_id':None}
                    assets[asset_id] = {'asset_id':asset_id,'name':product['name'],'asset_type':'fund_simulated',
                                        'ownership_scope':'personal','source_asset_id':None,'details':{'source':'navren_simulated_bank'}}
                    data['contracts'].append({'contract_id':contract_id,'account_id':holding['account_id'],'asset_id':asset_id,
                                              'contract_type':'investment','name':product['name']+'持仓','status':'active',
                                              'principal_minor':holding['amount_minor'],'start_date':None,'end_date':None})
                    quantity = Decimal(holding['amount_minor'])/Decimal(100)
                    seed_key = hashlib.sha256((NAV_VERSION+':'+workspace+':'+key).encode()).hexdigest()
                # 原可执行产品合同为固定1元、本金赎回；不能显示虚构涨跌却仍按本金结算。
                nav_micro = (1000000 + int(hashlib.sha256((seed_key+':'+str(tick)).encode()).hexdigest()[:8],16)%2001-1000) if seed else 1000000
                quantity = quantity.quantize(Decimal('.00000001'),rounding=ROUND_HALF_UP)
                value = int((quantity*Decimal(nav_micro)/Decimal(10000)).quantize(Decimal(1),rounding=ROUND_HALF_UP))
                position.update(account_id=holding['account_id'],product_id=holding['product_id'],cost_basis_minor=holding['amount_minor'],
                                book_value_minor=value,quantity=str(quantity),unit_nav=str(Decimal(nav_micro)/Decimal(1000000)),
                                valuation_date=datetime.fromtimestamp(tick*TICK_SECONDS,SHANGHAI).date().isoformat(),
                                valuation_at=_iso(tick*TICK_SECONDS),valuation_source='navren_simulated_bank',
                                valuation_rule_version=NAV_VERSION,quantity_origin='versioned_simulated_opening',
                                execution_enabled=product.get('execution_enabled',True),source_position_id=seed['source_position_id'] if seed else None)
                data['positions'].append(position)
                assets[position['asset_id']].update(book_value_minor=value,cost_basis_minor=holding['amount_minor'],
                                                    valuation_date=position['valuation_date'],valuation_at=position['valuation_at'],
                                                    valuation_source='navren_simulated_bank')
            # 归零基金仍保留0金额对象，既有合同引用完整；保险地块标的不伪补估值。
            position_assets = {p['asset_id'] for p in data['positions']}
            for asset in assets.values():
                if asset['asset_type'].startswith('fund_') and asset['asset_id'] not in position_assets:
                    asset.update(book_value_minor=0,cost_basis_minor=0,valuation_source='navren_simulated_bank',
                                 valuation_date=datetime.fromtimestamp(tick*TICK_SECONDS,SHANGHAI).date().isoformat(),
                                 valuation_at=_iso(tick*TICK_SECONDS))
                excluded = asset['asset_type']=='crop_exposure' and asset['book_value_minor'] is None and asset.get('details',{}).get('not_added_to_assets') is True
                asset['included_in_total_assets'] = not excluded
                if asset['book_value_minor'] is None and not excluded:
                    raise BankError('CONNECTED_VALUATION_INCOMPLETE','当前资产估值缺失。',503)
            data['assets'] = list(assets.values())
            principals = {p['contract_id']:p['cost_basis_minor'] for p in data['positions']}
            for contract in data['contracts']:
                if contract['contract_type']=='investment':
                    contract['principal_minor'] = principals.get(contract['contract_id'],0)
                    if contract['principal_minor']==0:
                        contract['status'] = 'closed'
            cash = sum(a['balance_minor'] for a in data['accounts'] if a['account_type']!='credit_card')
            credit = sum(f['outstanding_minor'] for f in facilities.values())
            loans = db.execute('SELECT coalesce(sum(outstanding_minor),0) FROM loans WHERE workspace_id=?',(workspace,)).fetchone()[0]
            pending = db.execute("SELECT coalesce(sum(amount_minor),0) FROM orders WHERE workspace_id=? AND status='pending_settlement'",(workspace,)).fetchone()[0]
            investment = sum(p['book_value_minor'] for p in data['positions'])
            other = sum(a['book_value_minor'] for a in data['assets'] if not a['asset_type'].startswith('fund_') and a['included_in_total_assets'])
            total = cash+investment+other+pending
            data['summary'] = {'cash_minor':cash,'investment_minor':investment,'real_assets_minor':other,
                               'loan_principal_minor':loans,'credit_card_outstanding_minor':credit,'liabilities_minor':loans+credit,
                               'pending_redemption_minor':pending,'total_assets_minor':total,'net_worth_minor':total-loans-credit,
                               'holding_principal_minor':sum(p['cost_basis_minor'] for p in data['positions']),
                               'runtime_operations_included':True,'insurance_cash_value_minor':None,
                               'credit_limits_excluded':True,'insurance_sum_excluded':True}
            data['cards_include_historical'] = False
            data['credit_statements'] = [dict(r,details=json.loads(r['details'])) for r in db.execute('SELECT * FROM credit_statements WHERE workspace_id=? ORDER BY period_start DESC,id',(workspace,))]
            result = self._envelope(db,workspace,binding,now,data)
            return result

    def transactions(self, workspace, *, month, limit=8, offset=0, account_id=None, kind=None):
        if not isinstance(month,str) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])',month):
            raise BankError('INVALID_MONTH','月份必须为YYYY-MM。',400)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
            raise BankError('INVALID_PAGINATION','limit为1至100整数，offset为0至10000整数。',400)
        if kind is not None and (not isinstance(kind,str) or kind not in TRANSACTION_KINDS):
            raise BankError('INVALID_TRANSACTION_KIND','流水类型无效。',400)
        now = self.bank.clock()
        with closing(self.bank.connect()) as db:
            db.execute('BEGIN')
            binding = self._binding(db,workspace)
            links = [dict(r) for r in db.execute('SELECT * FROM connected_account_links WHERE workspace_id=?',(workspace,))]
            mapping = {r['source_account_id']:r['account_id'] for r in links}
            reverse = {value:key for key,value in mapping.items()}
            if account_id is not None and account_id not in reverse:
                raise BankError('CONNECTED_ACCOUNT_NOT_ACCESSIBLE','流水账户不属于当前银行连接主体。',404)
            service = self.g.demo_accounts.active_history(workspace) or self.g.financial_data
            if service is None or service.dataset_version != binding['dataset_version'] or str(service.database_path) != binding['source_path']:
                raise BankError('CONNECTED_HISTORY_UNAVAILABLE','开账历史版本尚未装配，不能混用其他流水来源。',503)
            card_map = {r['source_card_id']:r['card_id'] for r in db.execute('SELECT * FROM connected_card_links WHERE workspace_id=?',(workspace,))}
            source_values = [binding['subject_id'],month]
            source_where = 'subject_id=? AND substr(booked_on,1,7)=?'
            current_values = [workspace,month]
            current_where = 'workspace_id=? AND substr(occurred_at,1,7)=?'
            if account_id is not None:
                source_where += ' AND account_id=?';source_values.append(reverse[account_id])
                current_where += ' AND account_id=?';current_values.append(account_id)
            available = {r[0] for r in db.execute('SELECT DISTINCT kind FROM transactions WHERE '+current_where,current_values)}
            with closing(service._connect()) as history:
                history.execute('BEGIN')
                available.update(r[0] for r in history.execute('SELECT DISTINCT kind FROM transactions WHERE '+source_where,source_values))
                if kind is not None:
                    # 两个来源在计数与取分页候选前筛选；外部转账与普通转账共用展示类别。
                    kinds = ('transfer','external_transfer') if kind == 'transfer' else (kind,)
                    condition = ' AND kind IN ('+','.join('?' for _ in kinds)+')'
                    source_where += condition;source_values.extend(kinds)
                    current_where += condition;current_values.extend(kinds)
                source_total = history.execute('SELECT count(*) FROM transactions WHERE '+source_where,source_values).fetchone()[0]
                rows = [dict(r) for r in history.execute('SELECT * FROM transactions WHERE '+source_where+' ORDER BY occurred_at DESC,transaction_id DESC LIMIT ?',source_values+[offset+limit])]
            for row in rows:
                row.update(source_transaction_id=row['transaction_id'],transaction_id='history:'+row['transaction_id'],
                           source_account_id=row['account_id'],account_id=mapping[row['account_id']],
                           source_card_id=row['card_id'],card_id=card_map.get(row['card_id']),historical_import=True)
            current_total = db.execute('SELECT count(*) FROM transactions WHERE '+current_where,current_values).fetchone()[0]
            for row in db.execute('SELECT * FROM transactions WHERE '+current_where+' ORDER BY occurred_at DESC,id DESC LIMIT ?',current_values+[offset+limit]):
                if row['account_id'] not in reverse:
                    raise BankError('CONNECTED_ACCOUNT_INCOMPLETE','运行流水账户缺少银行连接关联。',503)
                rows.append({**dict(row),'transaction_id':'runtime:'+row['id'],'source_transaction_id':None,
                             'signed_minor':row['delta_minor'],'currency':'CNY','counterparty':row['merchant'],
                             'booked_on':row['occurred_at'][:10],'card_id':None,'historical_import':False,'status':'booked'})
            rows.sort(key=lambda r:(r['occurred_at'],r['transaction_id']),reverse=True)
            total = source_total+current_total
            available = {'transfer' if value == 'external_transfer' else value for value in available}
            return self._envelope(db,workspace,binding,now,{'items':rows[offset:offset+limit],'total':total,
                                  'offset':offset,'limit':limit,'has_more':offset+limit<total,'month':month,'account_id':account_id,
                                  'kind':kind,'available_kinds':sorted(available)})


HISTORY_UPGRADE_VERSION = 'connected-history-upgrade.v1'
_HISTORY_BINDING_TABLES = {'connected_asset_migrations','navren_financial_data_bindings'}


def history_bank_fingerprint(db):
    """逻辑快照包含业务表及结构；仅输出哈希和数量，不输出凭据。"""
    result={}
    for item in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        name=item['name']
        if name in _HISTORY_BINDING_TABLES:
            continue
        quoted='"'+name.replace('"','""')+'"'
        digest=hashlib.sha256()
        rows=sorted(encoded(list(r)) for r in db.execute('SELECT * FROM '+quoted))
        digest.update((item['sql'] or '').encode())
        for row in rows:digest.update(row.encode()+b'\n')
        result[name]={'rows':len(rows),'sha256':digest.hexdigest()}
    return result


def upgrade_financial_history(bank_database, parent_database, financial_database, *,
                              apply=False, expected=None, quality_receipt=None, clock=time.time):
    """只改历史绑定和封存快照；已有开账、实时余额和业务记录绝不重建。"""
    from financial_data_service.validate_realistic import endpoint
    bank_path=Path(bank_database).expanduser().resolve()
    parent_path=Path(parent_database).expanduser().resolve()
    candidate_path=Path(financial_database).expanduser().resolve()
    if not bank_path.is_file() or len({bank_path,parent_path,candidate_path})!=3:
        raise BankError('HISTORY_PATH_INVALID','升级路径缺失或相互覆盖。',409)
    parent,old_subjects,old_snapshots,parent_hash=_source(parent_path)
    candidate,subjects,snapshots,candidate_hash=_source(candidate_path)
    metadata=candidate.metadata
    if (metadata.get('history_profile')!='realistic_three_year'
            or metadata.get('historical_parent_dataset_version')!=parent.dataset_version
            or metadata.get('historical_parent_sha256')!=parent_hash
            or Path(metadata.get('historical_parent_path','')).resolve()!=parent_path
            or metadata.get('start_date')!='2023-10-04' or metadata.get('end_date')!='2026-10-03'
            or parent.metadata.get('end_date')!=metadata['end_date']
            or subjects!=old_subjects):
        raise BankError('HISTORY_SOURCE_CHANGED','三年历史来源或主体连续性不符合升级合同。',409)
    with closing(parent._connect()) as old, closing(candidate._connect()) as new:
        if endpoint(old)!=endpoint(new):
            raise BankError('HISTORY_ENDPOINT_CHANGED','账户、持仓、贷款、保单或期末金额发生变化。',409)
        mappings={s['subject_id']:[dict(r) for r in new.execute(
            'SELECT account_id,source_pool_id,account_type FROM accounts WHERE subject_id=? ORDER BY account_id',
            (s['subject_id'],))] for s in subjects}
    if quality_receipt is not None:
        quality=json.loads(Path(quality_receipt).read_text())
        if (quality.get('status')!='passed' or quality.get('candidate_sha256')!=candidate_hash
                or quality.get('dataset_version')!=candidate.dataset_version
                or not quality.get('checks') or not all(c.get('passed') is True for c in quality['checks'])):
            raise BankError('HISTORY_QUALITY_NOT_PASSED','候选质量报告未通过或与当前库不符。',409)
    elif apply:
        raise BankError('HISTORY_QUALITY_REQUIRED','应用升级必须提供通过的候选质量报告。',409)
    db=sqlite3.connect(bank_path.as_uri()+('?mode=rw' if apply else '?mode=ro'),uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if not apply:db.execute('PRAGMA query_only=ON')
    try:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        if not _table(db,'connected_asset_migrations') or not _table(db,'navren_demo_accounts'):
            raise BankError('HISTORY_OPENING_REQUIRED','工作区必须已经完成原有开账。',409)
        before=history_bank_fingerprint(db)
        current=[dict(r) for r in db.execute('SELECT * FROM connected_asset_migrations ORDER BY workspace_id')]
        read_rows=[dict(r) for r in db.execute('SELECT * FROM navren_financial_data_bindings ORDER BY workspace_id')] if _table(db,'navren_financial_data_bindings') else []
        guard={'business_tables':before,'migration_rows_sha256':hashlib.sha256(encoded(current).encode()).hexdigest(),
               'read_bindings_sha256':hashlib.sha256(encoded(read_rows).encode()).hexdigest(),
               'parent_sha256':parent_hash,'candidate_sha256':candidate_hash}
        plans=[]
        if len(current)!=4:
            raise BankError('HISTORY_BINDING_INCOMPLETE','原有四个历史绑定不完整。',409)
        for subject in subjects:
            uid,workspace=subject['subject_id'],subject['workspace_id']
            registry=db.execute('SELECT * FROM navren_demo_accounts WHERE user_id=? AND workspace_id=?',(uid,workspace)).fetchone()
            previous=next((r for r in current if r['workspace_id']==workspace),None)
            ws=db.execute('SELECT version FROM workspaces WHERE id=?',(workspace,)).fetchone()
            if not registry or registry['status']!='ready' or not ws or not previous or previous['subject_id']!=uid or previous['migration_version']!=MIGRATION_VERSION or previous['opening_as_of']!=metadata['end_date']:
                raise BankError('HISTORY_BINDING_INCOMPLETE','主体、工作区或开账日期不连续。',409)
            old_match=(previous['dataset_version']==parent.dataset_version and Path(previous['source_path']).resolve()==parent_path and previous['source_sha256']==parent_hash)
            new_match=(previous['dataset_version']==candidate.dataset_version and Path(previous['source_path']).resolve()==candidate_path and previous['source_sha256']==candidate_hash)
            if not old_match and not new_match:
                raise BankError('HISTORY_BINDING_CONFLICT','原绑定采用了其他来源，拒绝覆盖。',409)
            previous_read=next((r for r in read_rows if r['workspace_id']==workspace),None)
            read_new=False
            if previous_read:
                read_old=previous_read['dataset_version']==parent.dataset_version and Path(previous_read['financial_database']).resolve()==parent_path
                read_new=previous_read['dataset_version']==candidate.dataset_version and Path(previous_read['financial_database']).resolve()==candidate_path
                actual_mapping=sorted(json.loads(previous_read['account_mapping']),key=lambda r:r['account_id'])
                if previous_read['subject_id']!=uid or not (read_old or read_new) or actual_mapping!=mappings[uid]:
                    raise BankError('HISTORY_BINDING_CONFLICT','只读绑定与来源不连续。',409)
            if json.loads(registry['source_hashes']).get('financial_data.sqlite3')!=parent.metadata.get('source_database_sha256'):
                raise BankError('HISTORY_SOURCE_CHANGED','历史来源与体验用户的原始资料不同。',409)
            reference=snapshots[uid] if new_match else old_snapshots[uid]
            stored=json.loads(previous['snapshot'])
            fields={'accounts':('account_id','account_type','currency','balance_minor'),
                    'cards':('card_id','account_id','facility_id','role'),
                    'credit_facilities':('facility_id','account_id','limit_minor','outstanding_minor'),
                    'positions':('position_id','account_id','asset_id','contract_id','cost_basis_minor','book_value_minor'),
                    'contracts':('contract_id','account_id','asset_id','contract_type','principal_minor'),
                    'policies':('policy_id','contract_id','payment_account_id','premium_minor','sum_insured_minor')}
            for section,keys in fields.items():
                left=sorted(encoded([r.get(k) for k in keys]) for r in stored.get(section,[]))
                right=sorted(encoded([r.get(k) for k in keys]) for r in reference[section])
                if left!=right:raise BankError('HISTORY_OPENING_CHANGED','原开账快照与来源财务实体不一致。',409)
            account_ids={r['account_id'] for r in old_snapshots[uid]['accounts']}
            if not account_ids.issubset({r[0] for r in db.execute('SELECT source_account_id FROM connected_account_links WHERE workspace_id=?',(workspace,))}):
                raise BankError('HISTORY_BINDING_INCOMPLETE','原账户关联缺失。',409)
            plans.append({'subject_id':uid,'workspace_id':workspace,'version':ws['version'],
                          'already_upgraded':new_match and read_new,
                          'opening_version':previous['opening_version']})
        if apply and expected is None:
            raise BankError('HISTORY_PLAN_REQUIRED','应用升级必须提供此前只读计划。',409)
        changed=any(not p['already_upgraded'] for p in plans)
        if apply and changed and expected.get('guard')!=guard:
            raise BankError('HISTORY_PLAN_STALE','银行记录或历史来源已变化，请重新生成计划。',409)
        if apply and changed:
            db.execute('''CREATE TABLE IF NOT EXISTS navren_financial_data_bindings(
                workspace_id TEXT PRIMARY KEY,subject_id TEXT UNIQUE NOT NULL,
                dataset_version TEXT NOT NULL,financial_database TEXT NOT NULL,account_mapping TEXT NOT NULL)''')
            for plan in plans:
                uid,workspace=plan['subject_id'],plan['workspace_id']
                previous=next(r for r in current if r['workspace_id']==workspace)
                if plan['already_upgraded']:continue
                audit=json.loads(previous['audit'])
                upgrades=list(audit.get('history_upgrades',[]))
                upgrades.append({'upgrade_version':HISTORY_UPGRADE_VERSION,'parent_dataset_version':parent.dataset_version,
                                 'parent_sha256':parent_hash,'dataset_version':candidate.dataset_version,
                                 'source_sha256':candidate_hash,'applied_at':clock(),'financial_operations_replayed':False})
                audit['history_upgrades']=upgrades
                count=db.execute('UPDATE connected_asset_migrations SET dataset_version=?,source_path=?,source_sha256=?,snapshot=?,audit=? WHERE workspace_id=? AND dataset_version=? AND source_sha256=?',
                    (candidate.dataset_version,str(candidate_path),candidate_hash,encoded(snapshots[uid]),encoded(audit),workspace,previous['dataset_version'],previous['source_sha256'])).rowcount
                if count!=1:raise BankError('HISTORY_PLAN_STALE','历史绑定在升级时变化。',409)
                db.execute('INSERT OR REPLACE INTO navren_financial_data_bindings VALUES(?,?,?,?,?)',
                    (workspace,uid,candidate.dataset_version,str(candidate_path),encoded(mappings[uid])))
            if history_bank_fingerprint(db)!=before or db.execute('PRAGMA foreign_key_check').fetchone():
                raise BankError('HISTORY_BUSINESS_CHANGED','升级影响现行业务或关联，整笔回滚。',409)
        if _hash_file(parent_path)!=parent_hash or _hash_file(candidate_path)!=candidate_hash:
            raise BankError('HISTORY_SOURCE_CHANGED','升级期间历史来源发生变化。',409)
        if apply:db.commit()
        else:db.rollback()
        return {'upgrade_version':HISTORY_UPGRADE_VERSION,'applied':apply,'changed':apply and changed,
                'dataset_version':candidate.dataset_version,'candidate_sha256':candidate_hash,
                'bank_database':str(bank_path),'financial_database':str(candidate_path),
                'guard':guard,'workspaces':plans,'business_tables_preserved':True,
                'opening_replayed':False,'financial_operations_replayed':False,
                'opening_as_of_preserved':True,'live_activation_performed':False}
    except Exception:
        db.rollback()
        raise
    finally:db.close()
