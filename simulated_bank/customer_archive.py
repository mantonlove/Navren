"""版本迁移、只读客户全档案、字段字典和覆盖核验。"""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .customer_schema import SCHEMA_VERSION, SPECS, contract, ddl
from .customer_seed import load_sources, populate


def guard_version(bank):
    with closing(bank.connect()) as db:
        found = db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        if found and db.execute('SELECT coalesce(max(version),0) FROM schema_migrations').fetchone()[0]>SCHEMA_VERSION:
            raise RuntimeError('数据库来自更高版本，禁止降级打开。')


def migrate(bank):
    with closing(bank.connect()) as db:
        found = db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        version = db.execute('SELECT coalesce(max(version),0) FROM schema_migrations').fetchone()[0] if found else 1
        if version > SCHEMA_VERSION:
            raise RuntimeError('数据库来自更高版本，禁止降级打开。')
        old_workspaces = db.execute('SELECT count(*) FROM workspaces').fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if old_workspaces:
        backup = Path(str(bank.database)+'.before-v2-'+str(time.time_ns())+'.sqlite3')
        with closing(bank.connect()) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        backup.chmod(0o600)
    with bank.transaction() as db:
        found = db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        current = db.execute('SELECT coalesce(max(version),0) FROM schema_migrations').fetchone()[0] if found else 1
        if current == SCHEMA_VERSION: return
        if current > SCHEMA_VERSION: raise RuntimeError('禁止降级数据库。')
        # 所有DDL、源信息、批次迁移与客户补充数据同一事务；失败全部回滚。
        db.execute('PRAGMA defer_foreign_keys=ON')
        for statement in ddl(): db.execute(statement)
        load_sources(db)
        for row in db.execute('SELECT * FROM holdings WHERE amount_minor>0').fetchall():
            db.execute('INSERT INTO investment_lots VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('legacy-'+row['account_id']+'-'+row['product_id'],row['workspace_id'],row['account_id'],
                        row['product_id'],None,None,row['locked_until'],row['amount_minor'],row['amount_minor'],'legacy_aggregate'))
        for ws in db.execute('SELECT id FROM workspaces').fetchall(): populate(db,ws['id'],bank.clock)
        if db.execute('PRAGMA foreign_key_check').fetchone(): raise RuntimeError('迁移外键检查失败')
        db.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(SCHEMA_VERSION,bank.clock(),'客户档案与按批次持仓；无旧账本重写'))
        db.execute("INSERT OR REPLACE INTO metadata VALUES('schema_version',?)",(str(SCHEMA_VERSION),))


def seed_new_workspace(db, workspace, clock, core_seed_hash):
    for row in db.execute('SELECT * FROM holdings WHERE workspace_id=? AND amount_minor>0',(workspace,)).fetchall():
        db.execute('INSERT INTO investment_lots VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('fixture-'+row['account_id']+'-'+row['product_id'],workspace,row['account_id'],row['product_id'],
                    None,None,row['locked_until'],row['amount_minor'],row['amount_minor'],'fixture'))
    populate(db,workspace,clock,core_seed_hash)


def read_archive(db, workspace, domain=None):
    output = {}
    for name, group, _, _ in SPECS:
        if domain is None or group == domain:
            output[name] = [dict(r,details=json.loads(r['details'])) for r in db.execute('SELECT * FROM '+name+' WHERE workspace_id=? ORDER BY id',(workspace,))]
    output['investment_lots'] = [dict(r) for r in db.execute('SELECT * FROM investment_lots WHERE workspace_id=? ORDER BY locked_until,id',(workspace,))]
    return output


def coverage(db, workspace):
    counts = {name:db.execute('SELECT count(*) FROM '+name+' WHERE workspace_id=?',(workspace,)).fetchone()[0] for name,_,_,_ in SPECS}
    scenes = {
        '智能转账':['beneficiaries','transfer_templates','payment_instructions','scheduled_payments','split_bills','split_participants'],
        '账单分析':['archived_transactions','statement_periods','merchants','disputes'],
        '理财操作':['product_terms','risk_assessments','wealth_goals','budgets','advice_records'],
        '卡片管理':['cards','card_controls','card_applications','card_limit_requests','card_events','credit_statements'],
        '订阅与代扣管理':['subscriptions','subscription_events','notifications'],
        '跨场景联动':['orchestration_tasks','task_steps','fund_holds','service_providers','service_quotes','service_reservations'],
    }
    return {'scope':contract()['scope'],'counts':counts,'scenes':scenes,
            'absent':{'collateral':'当前虚构客户的消费贷款无抵押，所以0条；结构已定义。'},
            'unrepresented':contract()['excluded'],'prepared_only':['转账执行、定时扣款、AA收款','卡片申请审批、提额、挂失和解锁执行','商户合同取消','生效的资金锁定及商户预订','贷款放款和自动还款'],
            'schema_version':SCHEMA_VERSION,'notice':'源标准提供结构依据，档案金额与状态由轻舟模拟，不代表真实银行授权。'}


def verify_invariants(db, workspace):
    problems = []
    for a in db.execute('SELECT * FROM accounts WHERE workspace_id=?',(workspace,)):
        delta = db.execute('SELECT coalesce(sum(delta_minor),0) FROM transactions WHERE workspace_id=? AND account_id=?',(workspace,a['id'])).fetchone()[0]
        if a['balance_minor'] != a['opening_balance_minor']+delta: problems.append('活期余额无法对账:'+a['id'])
        rows = db.execute('SELECT * FROM statement_periods WHERE workspace_id=? AND account_id=? ORDER BY period_start',(workspace,a['id'])).fetchall()
        previous = None
        for r in rows:
            movements = db.execute('SELECT coalesce(sum(delta_minor),0) FROM archived_transactions WHERE workspace_id=? AND account_id=? AND substr(occurred_at,1,10) BETWEEN ? AND ?',
                                   (workspace,a['id'],r['period_start'],r['period_end'])).fetchone()[0]
            if r['closing_minor']-r['opening_minor']!=movements or r['closing_minor']!=r['opening_minor']+r['credits_minor']-r['debits_minor']: problems.append('封存月结无法对账:'+r['id'])
            if previous is not None and previous!=r['opening_minor']: problems.append('封存月结不连续:'+r['id'])
            previous = r['closing_minor']
        if rows and rows[-1]['closing_minor']!=a['opening_balance_minor']: problems.append('封存账本与活动账本不衔接:'+a['id'])
    for r in db.execute('SELECT * FROM holdings WHERE workspace_id=?',(workspace,)):
        amount = db.execute('SELECT coalesce(sum(remaining_minor),0) FROM investment_lots WHERE workspace_id=? AND account_id=? AND product_id=?',(workspace,r['account_id'],r['product_id'])).fetchone()[0]
        if amount!=r['amount_minor']: problems.append('批次与汇总持仓不一致')
    for r in db.execute('SELECT * FROM loans WHERE workspace_id=?',(workspace,)):
        scheduled = db.execute("SELECT coalesce(sum(principal_minor),0) FROM loan_installments WHERE workspace_id=? AND loan_id=? AND status IN ('scheduled','overdue')",(workspace,r['id'])).fetchone()[0]
        if scheduled!=r['outstanding_minor']: problems.append('贷款计划本金与负债不一致')
    for r in db.execute('SELECT * FROM split_bills WHERE workspace_id=?',(workspace,)):
        shares = db.execute('SELECT coalesce(sum(share_minor),0) FROM split_participants WHERE workspace_id=? AND split_id=?',(workspace,r['id'])).fetchone()[0]
        if shares!=r['total_minor']: problems.append('AA分摊不等于总额')
    for r in db.execute('SELECT * FROM cards WHERE workspace_id=?',(workspace,)):
        debt = db.execute("SELECT coalesce(sum(total_due_minor-paid_minor),0) FROM credit_statements WHERE workspace_id=? AND card_id=? AND status='open'",(workspace,r['id'])).fetchone()[0]
        if r['type']=='credit' and debt!=r['outstanding_minor']: problems.append('信用卡负债与账单不一致')
    return {'ok':not problems,'problems':problems,'checks':['活动现金账本','封存月结和衔接','批次持仓','贷款本金','AA份额','信用卡负债'],'workspace_id':workspace}
