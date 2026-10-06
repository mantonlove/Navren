#!/usr/bin/env python3
"""生成四个独立用户的十年合成财务账本；不修改运行中的银行数据库。"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import math
import os
import random
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILES = ROOT / 'simulated_bank/fixtures/contest_profiles.json'
SOURCE = 'navren_synthetic'

DDL = '''
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE users(user_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,risk_level INTEGER NOT NULL,
 horizon_days INTEGER NOT NULL,reserve_minor INTEGER NOT NULL,source TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL);
CREATE TABLE ledger_accounts(account_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users,
 code TEXT NOT NULL,name TEXT NOT NULL,account_class TEXT NOT NULL,scope TEXT NOT NULL,
 opening_balance_minor INTEGER NOT NULL,source TEXT NOT NULL,UNIQUE(user_id,code));
CREATE TABLE journal_events(event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users,
 occurred_at TEXT NOT NULL,event_type TEXT NOT NULL,category TEXT NOT NULL,scope TEXT NOT NULL,
 counterparty TEXT NOT NULL,source TEXT NOT NULL,reference_id TEXT);
CREATE TABLE journal_lines(line_id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES journal_events,
 user_id TEXT NOT NULL REFERENCES users,account_id TEXT NOT NULL REFERENCES ledger_accounts,
 debit_minor INTEGER NOT NULL CHECK(debit_minor>=0),credit_minor INTEGER NOT NULL CHECK(credit_minor>=0),
 CHECK((debit_minor>0 AND credit_minor=0) OR (credit_minor>0 AND debit_minor=0)));
CREATE TABLE cash_transactions(transaction_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users,
 occurred_at TEXT NOT NULL,account_id TEXT NOT NULL REFERENCES ledger_accounts,merchant TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor>0),signed_minor INTEGER NOT NULL,kind TEXT NOT NULL,
 category TEXT NOT NULL,scope TEXT NOT NULL,event_id TEXT NOT NULL REFERENCES journal_events,
 transfer_group_id TEXT,source TEXT NOT NULL);
CREATE TABLE daily_balances(user_id TEXT NOT NULL REFERENCES users,date TEXT NOT NULL,cash_minor INTEGER NOT NULL,
 investment_minor INTEGER NOT NULL,real_assets_minor INTEGER NOT NULL,liabilities_minor INTEGER NOT NULL,
 total_assets_minor INTEGER NOT NULL,net_worth_minor INTEGER NOT NULL,PRIMARY KEY(user_id,date));
CREATE TABLE asset_values(user_id TEXT NOT NULL REFERENCES users,date TEXT NOT NULL,
 asset_id TEXT NOT NULL REFERENCES ledger_accounts,book_value_minor INTEGER NOT NULL,
 cost_basis_minor INTEGER NOT NULL,unrealized_pnl_minor INTEGER NOT NULL,PRIMARY KEY(user_id,date,asset_id));
CREATE TABLE loan_balances(user_id TEXT NOT NULL REFERENCES users,date TEXT NOT NULL,
 loan_id TEXT NOT NULL REFERENCES ledger_accounts,principal_minor INTEGER NOT NULL,PRIMARY KEY(user_id,date,loan_id));
CREATE TABLE monthly_summaries(user_id TEXT NOT NULL REFERENCES users,period_start TEXT NOT NULL,period_end TEXT NOT NULL,
 partial_period INTEGER NOT NULL,income_minor INTEGER NOT NULL,expense_minor INTEGER NOT NULL,refund_minor INTEGER NOT NULL,
 net_saving_minor INTEGER NOT NULL,cash_inflow_minor INTEGER NOT NULL,cash_outflow_minor INTEGER NOT NULL,
 opening_cash_minor INTEGER NOT NULL,closing_cash_minor INTEGER NOT NULL,realized_return_minor INTEGER NOT NULL,
 unrealized_change_minor INTEGER NOT NULL,PRIMARY KEY(user_id,period_start));
CREATE TABLE annual_summaries AS SELECT * FROM monthly_summaries WHERE 0;
CREATE UNIQUE INDEX annual_summary_key ON annual_summaries(user_id,period_start);
CREATE TABLE recurring_obligations(obligation_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users,
 category TEXT NOT NULL,scope TEXT NOT NULL,amount_minor INTEGER NOT NULL,frequency TEXT NOT NULL,
 next_due_on TEXT NOT NULL,status TEXT NOT NULL,source TEXT NOT NULL);
CREATE TABLE financial_targets(target_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users,
 category TEXT NOT NULL,target_minor INTEGER NOT NULL,allocated_minor INTEGER NOT NULL,
 as_of TEXT NOT NULL,source TEXT NOT NULL);
CREATE INDEX journal_user_time ON journal_events(user_id,occurred_at);
CREATE INDEX journal_account ON journal_lines(account_id,event_id);
CREATE INDEX cash_user_time ON cash_transactions(user_id,occurred_at);
'''


def cents(value):
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def dated_range(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def grown_amount(item, day, start, annual=False):
    base = date.fromisoformat(item.get('start_date') or start.isoformat())
    years = max(0,day.year-base.year) if annual else max(0, (day.year-base.year)*12+day.month-base.month)/12
    return max(1, cents(item['amount_minor'] * (1+item['annual_growth_bps']/10000)**years))


def active(item, day):
    return (not item.get('start_date') or item['start_date'] <= day.isoformat()) and (
        not item.get('end_date') or day.isoformat() <= item['end_date'])


class Ledger:
    def __init__(self, db, profile, start, end, seed):
        self.db, self.profile, self.uid = db, profile, profile['id']
        self.start, self.end, self.seed = start, end, seed
        self.accounts, self.balances, self.classes = {}, defaultdict(int), {}
        self.costs, self.asset_specs, self.loan_specs = {}, {}, {}
        self.sequence = 0
        self.day_sequence = defaultdict(int)
        self.original_expenses = []
        self.cash_deltas = defaultdict(int)
        self.metrics = defaultdict(lambda: defaultdict(int))
        self.day_open, self.day_close = {}, {}
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)', (
            self.uid, profile['display_name'], profile['risk_level'], profile['horizon_days'],
            profile['reserve_minor'], SOURCE, start.isoformat(), end.isoformat()))
        opening = {}
        for key, value in profile['opening_cash'].items():
            account = self.account('cash:'+key, 'cash', {'daily':'个人日常账户','reserve':'个人储备账户','operating':'个人经营结算账户'}[key],
                                   'self_employed' if key == 'operating' else 'personal', value)
            opening[account] = value
        for asset in profile['assets']:
            kind = 'investment' if asset['type'].startswith('fund_') else 'real_asset'
            account = self.account('asset:'+asset['id'], kind, asset['name'], 'personal', asset['opening_value_minor'])
            opening[account] = asset['opening_value_minor']
            self.asset_specs[account] = asset
            self.costs[account] = asset['opening_value_minor']
        for loan in profile['loans']:
            account = self.account('liability:'+loan['id'], 'liability', loan['name'], loan.get('scope','household'), -loan['opening_principal_minor'])
            opening[account] = -loan['opening_principal_minor']
            self.loan_specs[account] = loan
        equity = self.account('equity:opening', 'equity', '覆盖期前累计净资产', 'personal', -sum(opening.values()))
        opening[equity] = -sum(opening.values())
        self.event(start, 'opening', '期初登记', 'personal', '期初财务登记', opening, hour=0)

    def account(self, code, kind, name=None, scope='personal', opening=0):
        if code in self.accounts:
            return self.accounts[code]
        account_id = self.uid+'::'+code
        self.accounts[code], self.classes[account_id] = account_id, kind
        self.db.execute('INSERT INTO ledger_accounts VALUES(?,?,?,?,?,?,?,?)',
                        (account_id, self.uid, code, name or code.split(':',1)[-1], kind, scope, opening, SOURCE))
        return account_id

    def total_cash(self):
        return sum(v for a,v in self.balances.items() if self.classes[a]=='cash')

    def event(self, day, event_type, category, scope, merchant, changes, reference=None, hour=12):
        changes = {a:v for a,v in changes.items() if v}
        if not changes:
            return None
        if sum(changes.values()):
            raise ValueError('非平衡分录 '+str(changes))
        self.sequence += 1
        event_id = f'{self.uid}-e{self.sequence:07d}'
        # 合成时刻严格保留记账顺序，避免先用了尚未到账的收入。
        self.day_sequence[day] += 1
        seconds = self.day_sequence[day]
        occurred = (f'{day.isoformat()}T00:00:00+08:00' if event_type=='opening' else
                    f'{day.isoformat()}T{8+seconds//3600:02d}:{(seconds//60)%60:02d}:{seconds%60:02d}+08:00')
        self.db.execute('INSERT INTO journal_events VALUES(?,?,?,?,?,?,?,?,?)', (
            event_id,self.uid,occurred,event_type,category,scope,merchant,SOURCE,reference))
        for index,(account,delta) in enumerate(changes.items()):
            self.balances[account] += delta
            account_class = self.classes[account]
            if account_class in ('cash','investment','real_asset') and self.balances[account]<0:
                raise ValueError(f'{self.uid} {day} 资产不足 {account}')
            if account_class=='liability' and self.balances[account]>0:
                raise ValueError('负债多还 '+account)
            self.db.execute('INSERT INTO journal_lines VALUES(?,?,?,?,?,?)', (
                event_id+f'-l{index}',event_id,self.uid,account,max(delta,0),max(-delta,0)))
            if event_type == 'opening':
                continue
            metric = self.metrics[day]
            code = account.split('::',1)[1]
            if account_class=='income':
                if code.startswith('income:unrealized'):
                    metric['unrealized_change_minor'] -= delta
                else:
                    metric['income_minor'] -= delta
                if code=='income:investment_realized':
                    metric['realized_return_minor'] -= delta
            elif account_class=='expense' and not code.startswith('expense:depreciation'):
                metric['expense_minor'] += delta
            if account_class=='cash':
                metric['cash_inflow_minor' if delta>0 else 'cash_outflow_minor'] += abs(delta)
                self.cash_deltas[day] += delta
                kind = event_type
                transfer = event_id if event_type=='internal_transfer' else None
                self.db.execute('INSERT INTO cash_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    event_id+f'-c{index}',self.uid,occurred,account,merchant,abs(delta),delta,kind,
                    category,scope,event_id,transfer,SOURCE))
        if event_type=='refund':
            self.metrics[day]['refund_minor'] += sum(v for a,v in changes.items() if self.classes[a]=='cash' and v>0)
        return event_id

    def transfer(self, day, source, target, amount):
        if not amount:
            return
        self.event(day,'internal_transfer','本人账户调拨','personal','本人账户调拨', {source:-amount,target:amount},hour=8)

    def ensure_cash(self, day, account, amount):
        need = max(0, amount-self.balances[account])
        for code in ('cash:daily','cash:operating','cash:reserve'):
            other = self.accounts[code]
            if other==account:
                continue
            take = min(need,self.balances[other])
            if take:
                self.transfer(day,other,account,take)
                need -= take
        for other in self.asset_specs:
            if not need:
                break
            if self.classes[other]=='investment':
                take = min(need,self.balances[other])
                if take:
                    self.redeem(day,other,account,take,'支出前资金调拨')
                    need -= take
        if need:
            raise ValueError(f'{self.uid} {day} 无法支付 {amount} 分；缺口 {need}，不能伪造补款')

    def receive(self, day, category, amount, scope='personal'):
        cash = self.accounts['cash:operating' if scope=='self_employed' else 'cash:daily']
        income = self.account('income:'+category,'income',category,scope)
        return self.event(day,'income',category,scope,'合成'+category,{cash:amount,income:-amount},hour=9)

    def spend(self, day, category, amount, scope='personal', refundable=False):
        cash = self.accounts['cash:operating' if scope=='self_employed' else 'cash:daily']
        self.ensure_cash(day,cash,amount)
        expense = self.account('expense:'+category,'expense',category,scope)
        event_id = self.event(day,'expense',category,scope,'合成'+category,{cash:-amount,expense:amount},hour=14)
        if refundable:
            self.original_expenses.append((day,event_id,cash,expense,amount,category,scope))

    def refund(self, day):
        candidates = [x for x in self.original_expenses if 3 <= (day-x[0]).days <= 18]
        if not candidates:
            return
        original = candidates[-1]
        self.original_expenses.remove(original)
        _,event_id,cash,expense,amount,category,scope = original
        value = max(1,amount*37//100)
        self.event(day,'refund',category,scope,'合成退款',{cash:value,expense:-value},event_id,hour=15)

    def redeem(self, day, asset, cash, amount, category):
        current = self.balances[asset]
        if not 0<amount<=current:
            raise ValueError('非法赎回')
        removed_cost = self.costs[asset] if amount==current else self.costs[asset]*amount//current
        realized = amount-removed_cost
        self.costs[asset] -= removed_cost
        changes = {cash:amount,asset:-amount}
        if realized:
            unrealized = self.account('income:unrealized_fund','income','投资未实现估值变动')
            realized_account = self.account('income:investment_realized','income','已实现投资损益')
            changes[unrealized] = realized
            changes[realized_account] = -realized
        self.event(day,'investment_redemption',category,'personal','合成投资赎回',changes,hour=8)

    def invest(self, day):
        cash = self.accounts['cash:daily']
        spare = max(0,self.total_cash()-self.profile['reserve_minor']*2)
        amount = min(self.balances[cash]//10,spare//12)
        if amount<10000:
            return
        allocations = {1:(75,25,0),2:(45,45,10),3:(25,45,30),4:(15,30,55),5:(10,20,70)}[self.profile['risk_level']]
        eligible = [(a,allocations[('fund_cash','fund_bond','fund_equity').index(spec['type'])])
                    for a,spec in self.asset_specs.items() if spec['type'].startswith('fund_')]
        eligible = [(a,w) for a,w in eligible if w]
        total_weight = sum(w for _,w in eligible)
        if not total_weight:
            return
        remaining = amount
        for index,(asset,weight) in enumerate(eligible):
            take = remaining if index==len(eligible)-1 else amount*weight//total_weight
            remaining -= take
            if take:
                self.costs[asset] += take
                self.event(day,'investment_purchase','投资申购','personal','合成投资申购',{cash:-take,asset:take},hour=16)

    def values(self, day):
        # 所有收益/估值参数均为合成；从期初基准按日计提，不引用真实基金名称或净值。
        if day==self.start:
            return
        for account,spec in self.asset_specs.items():
            value = self.balances[account]
            if not value:
                continue
            kind = spec['type']
            if kind.startswith('fund_'):
                digest = hashlib.sha256(f'{self.seed}:{day}:{kind}'.encode()).digest()
                rng = random.Random(int.from_bytes(digest[:8],'big'))
                rate = spec['annual_drift_bps']/10000/365.25 + rng.gauss(0,spec['volatility_bps']/10000/math.sqrt(365.25))
                rate = max(-.08,min(.08,rate))
                delta = cents(value*rate)
                offset = self.account('income:unrealized_fund','income','投资未实现估值变动')
                category = '投资估值变动'
            else:
                if day.day!=calendar.monthrange(day.year,day.month)[1]:
                    continue
                digest = hashlib.sha256(f'{self.seed}:{day}:{account}'.encode()).digest()
                rng = random.Random(int.from_bytes(digest[:8],'big'))
                rate = spec['annual_drift_bps']/10000/12 + rng.gauss(0,spec['volatility_bps']/10000/math.sqrt(12))
                rate = max(-.08,min(.08,rate))
                delta = cents(value*rate)
                if kind=='equipment':
                    delta = -min(max(0,-delta),max(0,value-spec['opening_value_minor']//5))
                    offset = self.account('expense:depreciation','expense','设备非现金折旧','self_employed')
                    category = '设备非现金折旧'
                else:
                    offset = self.account('income:unrealized_real_asset','income','非金融资产合成估值变动')
                    category = '非金融资产估值变动'
            self.event(day,'valuation',category,'self_employed' if kind=='equipment' else 'personal',
                       '合成估值模型',{account:delta,offset:-delta},hour=7)

    def scheduled(self, day):
        for collection, income, seasonal in [('monthly_income',True,False),('annual_income',True,True),
                                              ('monthly_expense',False,False),('seasonal_expense',False,True)]:
            for item in self.profile[collection]:
                if not active(item,day) or day.day!=min(item['day'],calendar.monthrange(day.year,day.month)[1]):
                    continue
                if seasonal and day.month not in item['months']:
                    continue
                amount = grown_amount(item,day,self.start,annual=seasonal)
                if seasonal:
                    months = sorted(item['months'])
                    amount = amount//len(months)+int(months.index(day.month)<amount%len(months))
                scope = item.get('scope','personal')
                if income:
                    # 年度经营和农业回款属于该个体的个人经营结算，不含公司营业额。
                    self.receive(day,item['category'],amount,scope)
                else:
                    self.spend(day,item['category'],amount,scope,refundable=False)
        if day.day==18:
            for account,spec in self.loan_specs.items():
                balance = -self.balances[account]
                if not balance:
                    continue
                principal = min(balance,spec['monthly_principal_minor'])
                interest = cents(balance*spec['annual_rate_bps']/10000/12)
                scope = spec.get('scope','household')
                cash = self.accounts['cash:operating' if scope=='self_employed' else 'cash:daily']
                self.ensure_cash(day,cash,principal+interest)
                changes = {cash:-(principal+interest),account:principal}
                if interest:
                    changes[self.account('expense:贷款利息:'+scope,'expense','贷款利息',scope)] = interest
                self.event(day,'loan_repayment','贷款偿还',scope,'合成贷款偿还',changes,hour=13)
        if day.day==21:
            self.invest(day)
        if day.day==27:
            cash = self.accounts['cash:reserve']
            interest = self.balances[cash]*120//10000//12
            if interest:
                income = self.account('income:存款利息','income','存款利息')
                self.event(day,'income','存款利息','personal','合成储备账户利息',{cash:interest,income:-interest},hour=10)

    def discretionary(self, day):
        # 高频个人消费独立于固定/经营开销；幅度从已声明生活预算导出。
        digest = hashlib.sha256(f'{self.seed}:{self.uid}:{day}:spending'.encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8],'big'))
        personal = [x for x in self.profile['monthly_expense'] if active(x,day) and x.get('scope','personal')!='self_employed']
        budget = sum(grown_amount(x,day,self.start) for x in personal)
        scale = max(2500,budget//220)
        if rng.random()<.88:
            category = rng.choice(['餐饮','日用品','交通','休闲娱乐','个人购物'])
            self.spend(day,category,max(1,cents(scale*rng.uniform(.35,2.2))),refundable=True)
        if day.day in (11,24) and rng.random()<.55:
            category = rng.choice(['健康医疗','家庭往来','家居用品'])
            self.spend(day,category,max(1,cents(scale*rng.uniform(3,12))),'household',True)
        if day.day==25 and rng.random()<.6:
            self.refund(day)
        # 周期性支出前主动取回部分持仓，保留正负收益和本金的区别。
        if day.day==3 and day.month in (3,9):
            positions = [a for a,s in self.asset_specs.items() if s['type']=='fund_equity' and self.balances[a]>100000]
            if positions:
                asset = positions[0]
                self.redeem(day,asset,self.accounts['cash:daily'],self.balances[asset]//12,'半年度投资资金回收')

    def snapshot(self, day):
        grouped = defaultdict(int)
        for account,value in self.balances.items():
            grouped[self.classes[account]] += value
        total = grouped['cash']+grouped['investment']+grouped['real_asset']
        liability = -grouped['liability']
        self.db.execute('INSERT INTO daily_balances VALUES(?,?,?,?,?,?,?,?)', (
            self.uid,day.isoformat(),grouped['cash'],grouped['investment'],grouped['real_asset'],liability,total,total-liability))
        self.day_close[day] = grouped['cash']
        self.db.executemany('INSERT INTO asset_values VALUES(?,?,?,?,?,?)', [
            (self.uid,day.isoformat(),a,self.balances[a],self.costs[a],self.balances[a]-self.costs[a]) for a in self.asset_specs])
        self.db.executemany('INSERT INTO loan_balances VALUES(?,?,?,?)', [
            (self.uid,day.isoformat(),a,-self.balances[a]) for a in self.loan_specs])

    def summarize(self):
        for table, annual in [('monthly_summaries',False),('annual_summaries',True)]:
            buckets = defaultdict(list)
            for day in self.day_close:
                buckets[(day.year,) if annual else (day.year,day.month)].append(day)
            for key,days in buckets.items():
                first,last = min(days),max(days)
                full_start = date(first.year,1 if annual else first.month,1)
                full_end = date(last.year,12,31) if annual else date(last.year,last.month,calendar.monthrange(last.year,last.month)[1])
                sums = {k:sum(self.metrics[day][k] for day in days) for k in (
                    'income_minor','expense_minor','refund_minor','cash_inflow_minor','cash_outflow_minor',
                    'realized_return_minor','unrealized_change_minor')}
                self.db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    self.uid,first.isoformat(),last.isoformat(),int(first!=full_start or last!=full_end),
                    sums['income_minor'],sums['expense_minor'],sums['refund_minor'],sums['income_minor']-sums['expense_minor'],
                    sums['cash_inflow_minor'],sums['cash_outflow_minor'],self.day_open[first],self.day_close[last],
                    sums['realized_return_minor'],sums['unrealized_change_minor']))
        for collection,frequency in [('monthly_expense','monthly'),('seasonal_expense','seasonal')]:
            for index,item in enumerate(self.profile[collection]):
                if not active(item,self.end):
                    continue
                candidate = self.end+timedelta(days=1)
                while candidate.day!=min(item['day'],calendar.monthrange(candidate.year,candidate.month)[1]) or (
                    frequency=='seasonal' and candidate.month not in item['months']):
                    candidate += timedelta(days=1)
                amount = grown_amount(item,candidate,self.start,annual=frequency=='seasonal')
                if frequency=='seasonal':
                    months = sorted(item['months'])
                    amount = amount//len(months)+int(months.index(candidate.month)<amount%len(months))
                self.db.execute('INSERT INTO recurring_obligations VALUES(?,?,?,?,?,?,?, ?,?)', (
                    f'{self.uid}-{collection}-{index}',self.uid,item['category'],item.get('scope','personal'),amount,
                    frequency,candidate.isoformat(),'forecast_not_paid',SOURCE))
        reserve = self.profile['reserve_minor']
        self.db.execute('INSERT INTO financial_targets VALUES(?,?,?,?,?,?,?)', (
            self.uid+'-reserve',self.uid,'应急资金储备',reserve,min(reserve,self.total_cash()),self.end.isoformat(),SOURCE))

    def run(self):
        for day in dated_range(self.start,self.end):
            self.day_open[day] = self.total_cash()
            self.values(day)
            self.scheduled(day)
            self.discretionary(day)
            self.snapshot(day)
        self.summarize()


def build_dataset(path, profiles_path=None):
    """独立构建SQLite文件；成功后原子发布，可供审计器直接调用。"""
    profiles_file = Path(profiles_path or DEFAULT_PROFILES)
    raw = profiles_file.read_bytes()
    config = json.loads(raw)
    start,end = date.fromisoformat(config['start_date']),date.fromisoformat(config['end_date'])
    if start.replace(year=start.year+10)-timedelta(days=1)!=end:
        raise ValueError('覆盖期必须为精确十年闭区间')
    if len(config['profiles'])!=4 or len({p['id'] for p in config['profiles']})!=4:
        raise ValueError('必须为四个独立账户')
    target = Path(path).resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary = tempfile.mkstemp(prefix='.'+target.name+'-',suffix='.sqlite3',dir=target.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript(DDL)
            meta = {
                'schema_version':'1','source':SOURCE,'currency':'CNY','amount_unit':'integer_cent',
                'start_date':start.isoformat(),'end_date':end.isoformat(),'timezone':'Asia/Shanghai (+08:00)',
                'seed':str(config['seed']),'config_sha256':hashlib.sha256(raw).hexdigest(),
                'is_real_bank_data':'false','is_real_product_return':'false',
                'valuation_method':'合成账面估值；金融资产按日、其他资产按月。期初成本假设等于期初价值。',
                'opening_method':'覆盖期之前的累计资产负债作为期初，不伪造覆盖期前交易。',
                'income_method':'已确认收入科目净贷方，排除未实现估值；含已实现投资损益。',
                'expense_method':'费用科目净借方扣退款，排除非现金设备折旧；投资、贷款本金和本人转账不计消费。',
                'forecast_method':'recurring_obligations为覆盖期后预测，未记账，未包括在历史统计。',
                'timestamp_method':'合成记账顺序时间；明确+08:00，不声称真实交易时刻。',
                'cashflow_method':'月度基准支出与额外个人消费分别记录；季节性年度总额按日历年增长并分摊到约定月份。',
            }
            db.executemany('INSERT INTO metadata VALUES(?,?)',meta.items())
            for profile in config['profiles']:
                Ledger(db,profile,start,end,config['seed']).run()
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('生成数据违反外键约束')
            db.commit()
        with open(temporary,'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary,target)
        directory = os.open(target.parent,os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return dataset_summary(target)


def dataset_summary(path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        meta = dict(db.execute('SELECT key,value FROM metadata'))
        users = []
        for user in db.execute('SELECT * FROM users ORDER BY user_id'):
            uid = user['user_id']
            last = dict(db.execute('SELECT * FROM daily_balances WHERE user_id=? ORDER BY date DESC LIMIT 1',(uid,)).fetchone())
            users.append({**dict(user),'cash_transaction_count':db.execute('SELECT count(*) FROM cash_transactions WHERE user_id=?',(uid,)).fetchone()[0],
                          'journal_event_count':db.execute('SELECT count(*) FROM journal_events WHERE user_id=?',(uid,)).fetchone()[0],
                          'daily_snapshot_count':db.execute('SELECT count(*) FROM daily_balances WHERE user_id=?',(uid,)).fetchone()[0],
                          'latest_balances':last})
        return {'metadata':meta,'users':users,'table_counts':{r[0]:db.execute('SELECT count(*) FROM '+r[0]).fetchone()[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}}


def export_dataset(database, output):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    summary = dataset_summary(database)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        contract = {'source':SOURCE,'amount_unit':'整数人民币分','schema_version':1,'tables':{}}
        for table in summary['table_counts']:
            contract['tables'][table] = [dict(zip(['cid','name','type','notnull','default','pk'],r))
                                        for r in db.execute('PRAGMA table_info('+table+')')]
        (output/'data_contract.json').write_text(json.dumps(contract,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        tables = ('cash_transactions','daily_balances','monthly_summaries','annual_summaries','asset_values','loan_balances')
        for user in summary['users']:
            folder = output/user['user_id']
            folder.mkdir(exist_ok=True)
            uid = user['user_id']
            for table in tables:
                rows = db.execute('SELECT * FROM '+table+' WHERE user_id=? ORDER BY rowid',(uid,))
                headers = [x[0] for x in rows.description]
                with (folder/(table+'.csv')).open('w',encoding='utf-8-sig',newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(headers)
                    writer.writerows(rows)
            current = {**user,'accounts':[],'assets':[],'loans':[],
                       'recurring_obligations':[dict(r) for r in db.execute('SELECT * FROM recurring_obligations WHERE user_id=?',(uid,))],
                       'financial_targets':[dict(r) for r in db.execute('SELECT * FROM financial_targets WHERE user_id=?',(uid,))]}
            for account in db.execute('SELECT * FROM ledger_accounts WHERE user_id=?',(uid,)):
                if account['account_class'] in ('income','expense','equity'):
                    continue
                balance = db.execute('SELECT coalesce(sum(debit_minor-credit_minor),0) FROM journal_lines WHERE account_id=?',(account['account_id'],)).fetchone()[0]
                record = {**dict(account),'balance_minor':balance}
                if account['account_class']=='liability':
                    record['outstanding_principal_minor'] = -balance
                elif account['account_class'] in ('investment','real_asset'):
                    value = db.execute('SELECT * FROM asset_values WHERE user_id=? AND asset_id=? ORDER BY date DESC LIMIT 1',
                                       (uid,account['account_id'])).fetchone()
                    record.update({k:value[k] for k in ('book_value_minor','cost_basis_minor','unrealized_pnl_minor')})
                key = 'accounts' if account['account_class']=='cash' else ('loans' if account['account_class']=='liability' else 'assets')
                current[key].append(record)
            (folder/'current_finances.json').write_text(json.dumps(current,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles',type=Path,default=DEFAULT_PROFILES)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/contest-finances')
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    database = args.output/'financial_data.sqlite3'
    summary = build_dataset(database,args.profiles)
    export_dataset(database,args.output)
    print(json.dumps({'database':str(database),'table_counts':summary['table_counts'],
                      'users':[{k:u[k] for k in ('user_id','display_name','cash_transaction_count','daily_snapshot_count')} for u in summary['users']]},ensure_ascii=False))


if __name__=='__main__':
    main()
