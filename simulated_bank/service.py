"""SQLite模拟业务服务：精确金额、授权、幂等、状态及审计。"""
import calendar
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import statistics
import time
import uuid
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from .customer_archive import guard_version, migrate, seed_new_workspace, read_archive, coverage, verify_invariants
from .customer_schema import SCHEMA_VERSION, contract


class BankError(Exception):
    def __init__(self, code, message, status=400, persist=False):
        super().__init__(message)
        self.code, self.message, self.status, self.persist = code, message, status, persist

    def as_dict(self):
        return {'code': self.code, 'message': self.message, 'environment': 'simulation'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def integer(value, name, minimum=0, maximum=100000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BankError('INVALID_INTEGER', f'{name}必须为{minimum}至{maximum}之间的整数。')
    return value


def strict_fields(payload, allowed, required=()):
    if not isinstance(payload, dict) or set(payload)-set(allowed) or set(required)-set(payload):
        raise BankError('INVALID_FIELDS', '请求字段缺失或包含不允许的字段。')


def add_month(day):
    year, month = day.year+(day.month == 12), day.month % 12+1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class SimulatedBank:
    def __init__(self, database, *, fixture=None, clock=time.time):
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        fixture = Path(fixture) if fixture else Path(__file__).parent/'fixtures/demo.json'
        self.fixture = json.loads(fixture.read_text())
        self.seed_hash = digest(encoded(self.fixture))
        guard_version(self)
        self._initialize()
        migrate(self)
        self.database.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BankError as error:
            # 仅授权失败和执行前拒绝使用persist；业务写入后的异常始终回滚。
            db.commit() if error.persist else db.rollback()
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, agent_token_hash TEXT NOT NULL UNIQUE, pin_hash TEXT NOT NULL,
 as_of TEXT NOT NULL, version INTEGER NOT NULL, created_at REAL NOT NULL,
 failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS profiles(
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), risk_level INTEGER NOT NULL,
 horizon_days INTEGER NOT NULL, reserve_minor INTEGER NOT NULL CHECK(reserve_minor>=0));
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), name TEXT NOT NULL,
 currency TEXT NOT NULL CHECK(currency='CNY'), opening_balance_minor INTEGER NOT NULL,
 balance_minor INTEGER NOT NULL CHECK(balance_minor>=0));
CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 account_id TEXT NOT NULL REFERENCES accounts(id), occurred_at TEXT NOT NULL,
 merchant TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 delta_minor INTEGER NOT NULL, kind TEXT NOT NULL, category TEXT NOT NULL,
 note TEXT NOT NULL, mandate_id TEXT, operation_id TEXT);
CREATE TABLE IF NOT EXISTS mandates(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 account_id TEXT NOT NULL REFERENCES accounts(id), merchant TEXT NOT NULL,
 amount_minor INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','closed')),
 next_charge_date TEXT NOT NULL, merchant_contract_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holdings(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), account_id TEXT NOT NULL REFERENCES accounts(id),
 product_id TEXT NOT NULL REFERENCES products(id), amount_minor INTEGER NOT NULL CHECK(amount_minor>=0),
 locked_until TEXT NOT NULL, PRIMARY KEY(workspace_id,account_id,product_id));
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 idempotency_key TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 action TEXT NOT NULL, permission TEXT NOT NULL, state TEXT NOT NULL,
 confirmation_digest TEXT NOT NULL, version INTEGER NOT NULL, expires_at REAL NOT NULL,
 verification_hash TEXT, verification_expires_at REAL, result TEXT, created_at REAL NOT NULL,
 UNIQUE(workspace_id,idempotency_key));
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id), account_id TEXT NOT NULL REFERENCES accounts(id),
 product_id TEXT NOT NULL REFERENCES products(id), side TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor>0), status TEXT NOT NULL,
 settlement_date TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 operation_id TEXT, event TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tx_workspace ON transactions(workspace_id,occurred_at);
CREATE INDEX IF NOT EXISTS audit_workspace ON audit(workspace_id,id);
''')
            previous = db.execute("SELECT value FROM metadata WHERE key='seed_hash'").fetchone()
            db.execute("INSERT OR IGNORE INTO metadata VALUES('seed_hash',?)", (self.seed_hash,))
            for product in self.fixture['products']:
                existing = db.execute('SELECT data FROM products WHERE id=?',(product['id'],)).fetchone()
                if existing and existing['data'] != encoded(product):
                    raise RuntimeError('已有产品定义不能随样本变化而覆盖；请通过版本化迁移或新产品ID更新条款。')
                db.execute('INSERT OR IGNORE INTO products VALUES(?,?)', (product['id'], encoded(product)))

    def _workspace(self, db, workspace):
        row = db.execute('SELECT * FROM workspaces WHERE id=?', (workspace,)).fetchone()
        if not row:
            raise BankError('NOT_FOUND', '模拟工作区不存在。', 404)
        return row

    def session(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise BankError('UNAUTHENTICATED', '请先创建或恢复模拟工作区。', 401)
        hashed = digest(token)
        with closing(self.connect()) as db:
            row = db.execute('SELECT id,token_hash FROM workspaces WHERE token_hash=? OR agent_token_hash=?', (hashed,hashed)).fetchone()
        if not row:
            raise BankError('UNAUTHENTICATED', '模拟会话无效。', 401)
        return row['id'], 'human' if hmac.compare_digest(row['token_hash'],hashed) else 'agent'

    def authenticate(self, token):
        return self.session(token)[0]

    def _audit(self, db, workspace, event, details, operation=None):
        db.execute('INSERT INTO audit(workspace_id,operation_id,event,details,created_at) VALUES(?,?,?,?,?)',
                   (workspace, operation, event, encoded(details), self.clock()))

    def _bump(self, db, workspace):
        db.execute('UPDATE workspaces SET version=version+1 WHERE id=?', (workspace,))

    def create_workspace(self):
        workspace, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        agent_token = secrets.token_urlsafe(32)
        pin = f'{secrets.randbelow(1000000):06d}'
        short = workspace[:12]
        accounts = {r['key']: f'acct-{short}-{r["key"]}' for r in self.fixture['accounts']}
        mandates = {r['key']: f'mandate-{short}-{r["key"]}' for r in self.fixture['mandates']}
        with self.transaction() as db:
            db.execute('INSERT INTO workspaces(id,token_hash,agent_token_hash,pin_hash,as_of,version,created_at) VALUES(?,?,?,?,?,1,?)',
                       (workspace, digest(token), digest(agent_token), digest(workspace+':'+pin), self.fixture['as_of'], self.clock()))
            profile = self.fixture['profile']
            db.execute('INSERT INTO profiles VALUES(?,?,?,?)',
                       (workspace, profile['risk_level'], profile['horizon_days'], profile['reserve_minor']))
            for item in self.fixture['accounts']:
                opening = integer(item['opening_balance_minor'], '期初余额')
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)',
                           (accounts[item['key']], workspace, item['name'], 'CNY', opening, opening))
            for item in self.fixture['mandates']:
                db.execute('INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?)',
                           (mandates[item['key']], workspace, accounts['main'], item['merchant'],
                            item['amount_minor'], 'active', f'2026-10-{item["day"]:02d}', 'unchanged'))
            for item in self.fixture['transactions']:
                amount = integer(item['amount_minor'], '流水金额', 1)
                delta = amount if item['kind'] in ('income', 'refund') else -amount
                account = accounts[item['account_key']]
                db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?', (delta, account))
                db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
                           (f'{short}-{item["key"]}', workspace, account, item['occurred_at'], item['merchant'],
                            amount, delta, item['kind'], item['category'], item['note'], mandates.get(item['mandate_key'])))
            for item in self.fixture['holdings']:
                db.execute('INSERT INTO holdings VALUES(?,?,?,?,?)',
                           (workspace, accounts[item['account_key']], item['product_id'],
                            item['amount_minor'], item['locked_until']))
            seed_new_workspace(db, workspace, self.clock, self.seed_hash)
            self._audit(db, workspace, 'workspace_created', {'scenario_id': self.fixture['scenario_id'], 'seed_hash': self.seed_hash})
        return {'workspace_id': workspace, 'session_token': token, 'agent_token': agent_token, 'human_verification_code': pin,
                'verification_notice': '仅模拟额外验证；此码交给演示者，勿写入Agent提示或执行日志。',
                'environment': 'simulation', 'as_of': self.fixture['as_of']}

    def capabilities(self):
        return {'environment': 'simulation', 'currency': 'CNY', 'amount_unit': '人民币分',
                'scenario_id': self.fixture['scenario_id'], 'seed_hash': self.seed_hash,
                'enabled': ['statement_query', 'bill_analysis', 'periodic_charge_detection',
                            'product_comparison', 'purchase', 'redemption', 'mandate_close',
                            'operation_query', 'audit'],
                'unavailable': ['real_bank_connection', 'real_money_transfer', 'merchant_contract_cancel',
                                'card_issuance', 'real_identity_verification'],
                'schema_version': SCHEMA_VERSION,
                'data_ready': ['customer_archive','beneficiaries','scheduled_payments','split_bills','cards','credit','loans','subscriptions','cross_scene_tasks','investment_lots'],
                'settlement_notice': '模拟工作日规则简化为日历日；异步赎回需演示者用CLI推进模拟日期。'}

    def customer_archive(self, workspace):
        with self.transaction() as db:
            ws = self._workspace(db,workspace)
            records = read_archive(db,workspace)
            for table in ('transactions','mandates','holdings','orders'):
                records[table] = [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE workspace_id=?',(workspace,))]
            records['operations'] = [self._public_operation(r) for r in db.execute('SELECT * FROM operations WHERE workspace_id=?',(workspace,))]
            records['audit'] = [dict(r,details=json.loads(r['details'])) for r in db.execute('SELECT * FROM audit WHERE workspace_id=?',(workspace,))]
            pending = sum(r['amount_minor'] for r in records['orders'] if r['status']=='pending_settlement')
            cash = sum(r['balance_minor'] for r in self._accounts(db,workspace))
            held = sum(r['amount_minor'] for r in records['holdings'])
            debt = sum(r['outstanding_minor'] for r in records['loans'])+sum(r['outstanding_minor'] for r in records['cards'] if r['type']=='credit')
            return {'environment':'simulation','as_of':ws['as_of'],'version':ws['version'],
                    'schema_version':SCHEMA_VERSION,'records':records,
                    'accounts':self._accounts(db,workspace),'profile':self._profile(db,workspace),
                    'financial_summary':{'cash_minor':cash,'holding_principal_minor':held,'pending_redemption_minor':pending,
                        'liabilities_minor':debt,'net_assets_principal_minor':cash+held+pending-debt,
                        'proposed_reservations_minor':sum(r['amount_minor'] for r in records['fund_holds'] if r['status']=='proposed'),
                        'valuation_notice':'本金口径；贷款及信用卡只读档案未接执行；预留草案不从现金中扣除。'},
                    'coverage':coverage(db,workspace),'integrity':verify_invariants(db,workspace),
                    'notice':'只读完整模拟客户档案；草案记录不会自动转账、发卡或锁定现金。'}

    def data_contract(self):
        result = contract()
        with closing(self.connect()) as db:
            names = [r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            result['database_tables'] = {name:[dict(r) for r in db.execute('PRAGMA table_info('+name+')')] for name in names}
        return result

    def annual_analysis(self, workspace, year):
        if type(year) is not int or not 2025 <= year <= 2100:
            raise BankError('INVALID_YEAR','年度必须为2025至2100之间的整数。')
        with self.transaction() as db:
            ws = self._workspace(db,workspace)
            historical = [dict(r) for r in db.execute('SELECT * FROM archived_transactions WHERE workspace_id=? AND substr(occurred_at,1,4)=?',(workspace,str(year)))]
            live = [dict(r) for r in db.execute('SELECT * FROM transactions WHERE workspace_id=? AND substr(occurred_at,1,4)=?',(workspace,str(year)))]
            periods = [r['period_start'][:7] for r in db.execute('SELECT DISTINCT period_start FROM statement_periods WHERE workspace_id=? AND substr(period_start,1,4)=?',(workspace,str(year)))]
            live_months = {r['occurred_at'][:7] for r in live}
            covered = sorted(set(periods)|live_months)
            rows = historical+live
            totals = defaultdict(int)
            categories = defaultdict(int)
            for row in rows:
                totals[row['kind']] += row['amount_minor']
                if row['kind']=='expense': categories[row['category']] += row['amount_minor']
            return {'environment':'simulation','year':year,'as_of':ws['as_of'],'covered_months':covered,
                    'complete_calendar_year':len(covered)==12,'archived_count':len(historical),'live_count':len(live),
                    'totals_minor':dict(totals),'category_expense_minor':dict(categories),
                    'cash_delta_minor':sum(r['delta_minor'] for r in rows),'notice':'仅汇总银行活期流水；信用卡消费与贷款档案单列，避免重复计算。'}

    def _accounts(self, db, workspace):
        return [dict(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY name', (workspace,))]

    def _account(self, db, workspace, account):
        row = db.execute('SELECT * FROM accounts WHERE id=? AND workspace_id=?', (account, workspace)).fetchone()
        if not row:
            raise BankError('NOT_FOUND', '当前工作区没有该账户。', 404)
        return row

    def _product(self, db, product):
        row = db.execute('SELECT data FROM products WHERE id=?', (product,)).fetchone()
        if not row:
            raise BankError('NOT_FOUND', '模拟产品不存在。', 404)
        return json.loads(row['data'])

    def _profile(self, db, workspace):
        return dict(db.execute('SELECT * FROM profiles WHERE workspace_id=?', (workspace,)).fetchone())

    def profile(self, workspace, payload=None):
        if payload is None:
            with closing(self.connect()) as db:
                self._workspace(db, workspace)
                return self._profile(db, workspace)
        strict_fields(payload, ['risk_level', 'horizon_days', 'reserve_minor', 'confirmed'], ['risk_level', 'horizon_days', 'reserve_minor', 'confirmed'])
        if payload['confirmed'] is not True:
            raise BankError('CONFIRMATION_REQUIRED', '修改模拟风险画像需要明确确认。')
        values = (integer(payload['risk_level'], '风险等级', 1, 5),
                  integer(payload['horizon_days'], '投资期限', 0, 3650),
                  integer(payload['reserve_minor'], '保留资金'))
        with self.transaction() as db:
            self._workspace(db, workspace)
            db.execute('UPDATE profiles SET risk_level=?,horizon_days=?,reserve_minor=? WHERE workspace_id=?', (*values, workspace))
            self._bump(db, workspace)
            self._audit(db, workspace, 'profile_updated', payload)
            db.execute("UPDATE risk_assessments SET status='superseded' WHERE workspace_id=? AND status='current'",(workspace,))
            customer = db.execute('SELECT id FROM customers WHERE workspace_id=?',(workspace,)).fetchone()['id']
            today = self._workspace(db,workspace)['as_of']
            expiry = (date.fromisoformat(today)+timedelta(days=365)).isoformat()
            db.execute('''INSERT INTO risk_assessments(id,workspace_id,source_id,data_origin,customer_id,
                       assessed_on,expires_on,risk_level,horizon_days,reserve_minor,status,answers_json)
                       VALUES(?,?,'qingzhou','qingzhou_synthetic',?,?,?,?,?,?,'current','{}')''',
                       (uuid.uuid4().hex,workspace,customer,today,expiry,*values))
            return self._profile(db, workspace)

    def transactions(self, workspace, month=None):
        if month is not None and (not isinstance(month,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month)):
            raise BankError('INVALID_MONTH', '月份格式为YYYY-MM。')
        with closing(self.connect()) as db:
            self._workspace(db, workspace)
            if month:
                rows = db.execute('SELECT * FROM transactions WHERE workspace_id=? AND substr(occurred_at,1,7)=? ORDER BY occurred_at,id', (workspace, month))
            else:
                rows = db.execute('SELECT * FROM transactions WHERE workspace_id=? ORDER BY occurred_at,id', (workspace,))
            return [dict(row) for row in rows]

    def analysis(self, workspace, month):
        if not isinstance(month,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
            raise BankError('INVALID_MONTH','月报需要指定YYYY-MM格式的月份。')
        rows = self.transactions(workspace, month)
        totals = {key: 0 for key in ('income', 'expense', 'refund', 'transfer', 'investment_purchase', 'investment_redemption')}
        categories = defaultdict(int)
        for row in rows:
            totals[row['kind']] += row['amount_minor']
            if row['kind'] == 'expense':
                categories[row['category']] += row['amount_minor']
        return {'source': 'simulation', 'month': month, 'transaction_count': len(rows),
                'recorded_range': [rows[0]['occurred_at'], rows[-1]['occurred_at']] if rows else None,
                'totals_minor': totals, 'category_expense_minor': dict(categories),
                'income_expense_surplus_minor': totals['income']-totals['expense']+totals['refund'],
                'cash_delta_minor': sum(r['delta_minor'] for r in rows),
                'notice': '转账及投资流转单列，消费收支结余不是当前可用余额；空记录不证明没有消费。'}

    def anomalies(self, workspace):
        rows = [r for r in self.transactions(workspace) if r['kind'] == 'expense']
        duplicate, high, history, recent = [], [], defaultdict(list), defaultdict(list)
        for row in rows:
            key = (row['account_id'], row['merchant'], row['amount_minor'])
            moment = datetime.fromisoformat(row['occurred_at'])
            # 日期不含精确时间时，不做五分钟重复判定。
            if 'T' in row['occurred_at']:
                for previous in recent[key]:
                    seconds = (moment-datetime.fromisoformat(previous['occurred_at'])).total_seconds()
                    if 0 <= seconds <= 300:
                        duplicate.append({'rule': 'duplicate_within_5_minutes', 'status': '待核实',
                                          'transaction_ids': [previous['id'], row['id']]})
                recent[key].append(row)
            baseline = history[(row['account_id'], row['category'])]
            median = statistics.median_low(baseline) if baseline else 0
            if len(baseline) >= 5 and row['amount_minor'] >= 300000 and row['amount_minor'] > median*3:
                high.append({'rule': 'high_amount', 'status': '待核实', 'transaction_ids': [row['id']],
                             'baseline_count': len(baseline), 'baseline_median_minor': median})
            baseline.append(row['amount_minor'])
        return {'rule_version': 'sim-anomaly-v1', 'duplicates': duplicate, 'high_amounts': high,
                'notice': '演示规则，未经过真实反欺诈验证。'}

    def periodic_charges(self, workspace):
        groups = defaultdict(list)
        for row in self.transactions(workspace):
            if row['kind'] == 'expense':
                groups[(row['account_id'], row['merchant'])].append(row)
        candidates = []
        for (account, merchant), rows in groups.items():
            by_month = defaultdict(list)
            for row in rows:
                by_month[row['occurred_at'][:7]].append(row)
            # 多次普通消费不能只因同名商户就成为月度订阅。
            if len(by_month) < 3 or any(len(v) != 1 for v in by_month.values()):
                continue
            dates = [date.fromisoformat(r['occurred_at'][:10]) for r in rows]
            amounts = [r['amount_minor'] for r in rows]
            if not all(25 <= (b-a).days <= 35 and (b.year*12+b.month)-(a.year*12+a.month) == 1 for a,b in zip(dates,dates[1:])):
                continue
            median = statistics.median_low(amounts)
            if not all(abs(amount-median) <= max(100, median//10) for amount in amounts):
                continue
            next_date = add_month(dates[-1]).isoformat()
            candidates.append({'account_id': account, 'merchant': merchant,
                               'classification': '固定生活支出' if rows[-1]['category'] in ('住房','生活缴费','交通') else '疑似周期扣费',
                               'amount_minor': median, 'expected_next_date': next_date,
                               'transaction_ids': [r['id'] for r in rows],
                               'mandate_id': rows[-1]['mandate_id'], 'status': '需用户核实'})
        return {'rule_version': 'sim-monthly-v1', 'candidates': candidates}

    def mandates(self, workspace):
        with closing(self.connect()) as db:
            self._workspace(db, workspace)
            return [dict(r) for r in db.execute('SELECT * FROM mandates WHERE workspace_id=? ORDER BY merchant', (workspace,))]

    def holdings(self, workspace):
        with closing(self.connect()) as db:
            self._workspace(db, workspace)
            return [dict(r) for r in db.execute('SELECT * FROM holdings WHERE workspace_id=? AND amount_minor>0 ORDER BY account_id,product_id', (workspace,))]

    def products(self, workspace, product_ids=None):
        if product_ids is not None and (not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 20 or any(not isinstance(p,str) for p in product_ids)):
            raise BankError('INVALID_PRODUCTS', '产品标识须为1至20个字符串。')
        with closing(self.connect()) as db:
            self._workspace(db, workspace)
            profile = self._profile(db, workspace)
            products = [self._product(db, p) for p in product_ids] if product_ids else [json.loads(r['data']) for r in db.execute('SELECT data FROM products ORDER BY id')]
        output = []
        for product in products:
            reasons = self._eligibility(product, profile)
            output.append({**product, 'profile_eligible': not reasons, 'exclusion_reasons': reasons})
        return output

    def _eligibility(self, product, profile):
        reasons = []
        if product['risk_level'] > profile['risk_level']:
            reasons.append('产品风险高于模拟风险画像。')
        if product['term_days'] > profile['horizon_days']:
            reasons.append('锁定期限超过可投资期限。')
        return reasons

    def snapshot(self, workspace, month='2026-09'):
        # 月报来自同一读事务，避免并发执行时各查询混用业务版本。
        with self.transaction() as db:
            ws = self._workspace(db, workspace)
            accounts = self._accounts(db, workspace)
            holdings = [dict(r) for r in db.execute('SELECT * FROM holdings WHERE workspace_id=? AND amount_minor>0', (workspace,))]
            pending = db.execute("SELECT coalesce(sum(amount_minor),0) FROM orders WHERE workspace_id=? AND status='pending_settlement'", (workspace,)).fetchone()[0]
            # 持有BEGIN IMMEDIATE锁时其他写入不能插入；辅助只读查询读取同一已提交版本。
            return {'environment': 'simulation', 'as_of': ws['as_of'], 'version': ws['version'],
                    'accounts': accounts, 'holdings': holdings, 'pending_redemption_minor': pending,
                    'total_cash_minor': sum(r['balance_minor'] for r in accounts),
                    'holding_principal_minor': sum(r['amount_minor'] for r in holdings),
                    'profile': self._profile(db, workspace), 'monthly_report': self.analysis(workspace, month),
                    'anomalies': self.anomalies(workspace), 'periodic_charges': self.periodic_charges(workspace),
                    'notice': '现金为模拟账本状态；持仓按本金记账，未模拟市场估值或收益。'}

    def _operation(self, db, workspace, operation):
        row = db.execute('SELECT * FROM operations WHERE id=? AND workspace_id=?', (operation, workspace)).fetchone()
        if not row:
            raise BankError('NOT_FOUND', '当前工作区没有该操作。', 404)
        return row

    def _public_operation(self, row):
        return {key: json.loads(row[key]) if key in ('payload','result') and row[key] else row[key]
                for key in ('id','action','permission','state','confirmation_digest','version','expires_at','payload','result')}

    def operation(self, workspace, operation):
        with closing(self.connect()) as db:
            return self._public_operation(self._operation(db, workspace, operation))

    def _validate_action(self, db, workspace, payload):
        action = payload['action']
        account = self._account(db, workspace, payload['account_id'])
        ws = self._workspace(db, workspace)
        if action == 'close_mandate':
            mandate = db.execute('SELECT * FROM mandates WHERE id=? AND workspace_id=? AND account_id=?', (payload['mandate_id'], workspace, account['id'])).fetchone()
            if not mandate:
                raise BankError('NOT_FOUND', '账户下没有该模拟代扣协议。', 404)
            if mandate['status'] != 'active':
                raise BankError('MANDATE_CLOSED', '该模拟代扣协议已经关闭。', 409)
            return {'mandate': dict(mandate), 'account': dict(account)}
        amount = integer(payload['amount_minor'], '操作金额（分）', 1)
        product = self._product(db, payload['product_id'])
        profile = self._profile(db, workspace)
        if action == 'purchase':
            reasons = self._eligibility(product, profile)
            if reasons:
                raise BankError('UNSUITABLE_PRODUCT', ' '.join(reasons), 422)
            if amount < product['min_purchase_minor']:
                raise BankError('BELOW_MINIMUM', '金额未达到模拟产品起购要求。', 422)
            if account['balance_minor']-amount < profile['reserve_minor']:
                raise BankError('INSUFFICIENT_AVAILABLE', '申购后将不足以保留指定生活资金。', 422)
        elif action == 'redeem':
            holding = db.execute('SELECT * FROM holdings WHERE workspace_id=? AND account_id=? AND product_id=?', (workspace, account['id'], product['id'])).fetchone()
            if not holding or holding['amount_minor'] < amount:
                raise BankError('INSUFFICIENT_HOLDING', '模拟持仓不足。', 422)
            lots = db.execute('SELECT * FROM investment_lots WHERE workspace_id=? AND account_id=? AND product_id=? AND remaining_minor>0',
                              (workspace,account['id'],product['id'])).fetchall()
            eligible = sum(r['remaining_minor'] for r in lots if product['early_redemption'] or r['locked_until']<=ws['as_of'])
            if eligible < amount:
                raise BankError('HOLDING_LOCKED', '模拟持仓尚未到可赎回日期。', 422)
        return {'product': product, 'account': dict(account)}

    def prepare(self, workspace, payload):
        strict_fields(payload, ['action','account_id','product_id','amount_minor','mandate_id','idempotency_key'],
                      ['action','account_id','idempotency_key'])
        action = payload['action']
        if action not in ('purchase','redeem','close_mandate'):
            raise BankError('ACTION_UNAVAILABLE', '该业务未接入模拟银行。', 422)
        required = {'action','account_id','idempotency_key'} | ({'mandate_id'} if action=='close_mandate' else {'product_id','amount_minor'})
        strict_fields(payload, required, required)
        if any(not isinstance(payload[k], str) or not 1 <= len(payload[k]) <= 128 for k in required-{'amount_minor'}):
            raise BankError('INVALID_FIELDS', '对象标识和幂等键必须为不超过128字符的字符串。')
        key, payload_hash = payload['idempotency_key'], digest(encoded(payload))
        with self.transaction() as db:
            ws = self._workspace(db, workspace)
            existing = db.execute('SELECT * FROM operations WHERE workspace_id=? AND idempotency_key=?', (workspace,key)).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash:
                    raise BankError('IDEMPOTENCY_CONFLICT', '同一幂等键不能对应不同操作内容。', 409)
                return self._public_operation(existing)
            details = self._validate_action(db, workspace, payload)
            operation, expires = uuid.uuid4().hex, self.clock()+300
            permission = 'yellow' if action=='close_mandate' else 'red'
            confirmation = digest(encoded({'workspace': workspace, 'operation': operation, 'payload':payload,
                                           'version':ws['version'], 'expires_at':expires, 'details':details}))
            db.execute('''INSERT INTO operations(id,workspace_id,idempotency_key,payload,payload_hash,action,
                       permission,state,confirmation_digest,version,expires_at,created_at)
                       VALUES(?,?,?,?,?,?,?,'pending_confirmation',?,?,?,?)''',
                       (operation,workspace,key,encoded(payload),payload_hash,action,permission,confirmation,ws['version'],expires,self.clock()))
            self._audit(db, workspace, 'operation_prepared', {'action':action,'permission':permission,'version':ws['version']}, operation)
            return self._public_operation(self._operation(db,workspace,operation))

    def _check_pending(self, db, workspace, row):
        if row['state'] != 'pending_confirmation':
            raise BankError('INVALID_STATE', '该操作已不处于待确认状态。', 409)
        if self.clock() >= row['expires_at']:
            self._reject(db,workspace,row,'EXPIRED','确认已过期，请重新准备操作。','expired')
        if self._workspace(db,workspace)['version'] != row['version']:
            self._reject(db,workspace,row,'STALE_CONFIRMATION','账户或画像已变化，请重新准备并确认。','invalidated')

    def _reject(self, db, workspace, row, code, message, state=None):
        # 只在任何业务写入之前调用；持久化拒绝记录及旧确认失效状态。
        if state:
            db.execute('UPDATE operations SET state=?,verification_hash=NULL WHERE id=?', (state,row['id']))
        self._audit(db,workspace,'operation_rejected',{'code':code},row['id'])
        raise BankError(code,message,409,persist=True)

    def verify(self, workspace, operation, payload):
        strict_fields(payload, ['verification_code'], ['verification_code'])
        pin = payload['verification_code']
        if not isinstance(pin,str) or not re.fullmatch(r'\d{6}',pin):
            raise BankError('INVALID_VERIFICATION', '模拟验证码为六位数字。')
        with self.transaction() as db:
            row = self._operation(db, workspace, operation)
            self._check_pending(db,workspace,row)
            if row['permission'] != 'red':
                raise BankError('VERIFICATION_UNNECESSARY','此操作只需明确确认。',409)
            ws = self._workspace(db,workspace)
            if self.clock() < ws['locked_until']:
                raise BankError('VERIFICATION_LOCKED','模拟验证已暂时锁定，请稍后重试。',429)
            if not hmac.compare_digest(ws['pin_hash'], digest(workspace+':'+pin)):
                attempts = ws['failed_attempts']+1 if ws['locked_until']==0 else 1
                locked_until = self.clock()+60 if attempts>=3 else 0
                db.execute('UPDATE workspaces SET failed_attempts=?,locked_until=? WHERE id=?', (attempts,locked_until,workspace))
                self._audit(db,workspace,'verification_rejected',{'attempts':attempts,'locked':bool(locked_until)},operation)
                raise BankError('VERIFICATION_FAILED','模拟验证码错误。',403,persist=True)
            token = secrets.token_urlsafe(32)
            db.execute('UPDATE workspaces SET failed_attempts=0,locked_until=0 WHERE id=?', (workspace,))
            expiry = min(row['expires_at'], self.clock()+120)
            db.execute('UPDATE operations SET verification_hash=?,verification_expires_at=? WHERE id=?', (digest(token),expiry,operation))
            self._audit(db,workspace,'verification_passed',{'expires_at':expiry},operation)
            return {'verification_token':token, 'operation_id':operation, 'expires_at':expiry,
                    'notice':'模拟额外验证，不代表银行身份认证。'}

    def confirm(self, workspace, operation, payload):
        strict_fields(payload,['confirmed','confirmation_digest','verification_token'],['confirmed','confirmation_digest'])
        if payload['confirmed'] is not True:
            raise BankError('CONFIRMATION_REQUIRED','必须明确确认具体操作。')
        if not isinstance(payload['confirmation_digest'],str):
            raise BankError('INVALID_CONFIRMATION','确认摘要格式错误。')
        with self.transaction() as db:
            row = self._operation(db,workspace,operation)
            if not hmac.compare_digest(row['confirmation_digest'],payload['confirmation_digest']):
                self._reject(db,workspace,row,'CONFIRMATION_MISMATCH','确认内容与操作不一致。')
            # 相同已授权操作重放仅返回保存的回执，绝不重复改余额。
            if row['state'] in ('succeeded','pending_settlement','settled'):
                return self._public_operation(row)
            self._check_pending(db,workspace,row)
            if row['permission']=='red':
                if self.clock() < self._workspace(db,workspace)['locked_until']:
                    self._reject(db,workspace,row,'VERIFICATION_LOCKED','模拟验证已暂时锁定，请稍后重试。')
                token = payload.get('verification_token')
                if not isinstance(token,str) or not row['verification_hash'] or not hmac.compare_digest(row['verification_hash'],digest(token)) or self.clock() >= row['verification_expires_at']:
                    self._reject(db,workspace,row,'VERIFICATION_REQUIRED','请完成绑定此操作的模拟额外验证。')
            original = json.loads(row['payload'])
            details = self._validate_action(db,workspace,original)
            before = details['account']['balance_minor']
            if row['action']=='close_mandate':
                mandate = details['mandate']
                db.execute("UPDATE mandates SET status='closed' WHERE id=?", (mandate['id'],))
                state = 'succeeded'
                result = {'mandate_id':mandate['id'],'status':'closed','merchant_contract_status':'unchanged',
                          'notice':'仅关闭本模拟银行代扣协议，商户订阅合同未取消。'}
            else:
                product, amount, account = details['product'], original['amount_minor'], original['account_id']
                db.execute('PRAGMA defer_foreign_keys=ON')
                as_of = date.fromisoformat(self._workspace(db,workspace)['as_of'])
                order = uuid.uuid4().hex
                if row['action']=='purchase':
                    locked_until = (as_of+timedelta(days=product['term_days'])).isoformat()
                    db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?', (amount,account))
                    db.execute('''INSERT INTO holdings VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,account_id,product_id)
                               DO UPDATE SET amount_minor=amount_minor+excluded.amount_minor,locked_until=max(locked_until,excluded.locked_until)''',
                               (workspace,account,product['id'],amount,locked_until))
                    db.execute('INSERT INTO investment_lots VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (uuid.uuid4().hex,workspace,account,product['id'],order,as_of.isoformat(),locked_until,amount,amount,'purchase'))
                    self._ledger(db,workspace,account,as_of,product['name'],amount,-amount,'investment_purchase',operation)
                    state, order_status, settlement = 'succeeded','succeeded',as_of.isoformat()
                else:
                    remaining = amount
                    lots = db.execute('''SELECT * FROM investment_lots WHERE workspace_id=? AND account_id=?
                                      AND product_id=? AND remaining_minor>0 ORDER BY coalesce(acquired_on,''),id''',
                                      (workspace,account,product['id'])).fetchall()
                    for lot in lots:
                        if remaining==0: break
                        if not product['early_redemption'] and lot['locked_until']>as_of.isoformat(): continue
                        redeemed = min(remaining,lot['remaining_minor'])
                        db.execute('UPDATE investment_lots SET remaining_minor=remaining_minor-? WHERE id=?',(redeemed,lot['id']))
                        db.execute('INSERT INTO lot_redemptions VALUES(?,?,?,?)',(workspace,lot['id'],order,redeemed))
                        remaining -= redeemed
                    if remaining: raise RuntimeError('批次资金核验失败；回滚赎回。')
                    db.execute('UPDATE holdings SET amount_minor=amount_minor-? WHERE workspace_id=? AND account_id=? AND product_id=?', (amount,workspace,account,product['id']))
                    settlement = (as_of+timedelta(days=product['redemption_delay_days'])).isoformat()
                    if product['redemption_delay_days']:
                        state, order_status = 'pending_settlement','pending_settlement'
                    else:
                        db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?', (amount,account))
                        self._ledger(db,workspace,account,as_of,product['name'],amount,amount,'investment_redemption',operation)
                        state, order_status = 'succeeded','succeeded'
                db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (order,workspace,operation,account,product['id'],row['action'],amount,order_status,settlement,self.clock()))
                after = self._account(db,workspace,account)['balance_minor']
                result = {'order_id':order,'status':order_status,'amount_minor':amount,'settlement_date':settlement,
                          'balance_before_minor':before,'balance_after_minor':after,
                          'notice':'赎回待结算不计入可用现金。' if state=='pending_settlement' else '模拟业务已记账。'}
            db.execute('UPDATE operations SET state=?,result=?,verification_hash=NULL WHERE id=?', (state,encoded(result),operation))
            self._bump(db,workspace)
            self._audit(db,workspace,'operation_executed',{'state':state,'result':result},operation)
            return self._public_operation(self._operation(db,workspace,operation))

    def _ledger(self, db, workspace, account, day, merchant, amount, delta, kind, operation):
        db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)',
                   (uuid.uuid4().hex,workspace,account,day.isoformat()+'T12:00:00+08:00',merchant,amount,delta,kind,'投资流转','模拟订单记账',operation))

    def cancel(self, workspace, operation):
        with self.transaction() as db:
            row = self._operation(db,workspace,operation)
            if row['state']=='cancelled':
                return self._public_operation(row)
            if row['state'] != 'pending_confirmation':
                raise BankError('INVALID_STATE','已提交的业务不能按待确认操作取消。',409)
            db.execute("UPDATE operations SET state='cancelled',verification_hash=NULL WHERE id=?", (operation,))
            self._audit(db,workspace,'operation_cancelled',{},operation)
            return self._public_operation(self._operation(db,workspace,operation))

    def orders(self, workspace):
        with closing(self.connect()) as db:
            self._workspace(db,workspace)
            return [dict(r) for r in db.execute('SELECT * FROM orders WHERE workspace_id=? ORDER BY created_at,id',(workspace,))]

    def audit(self, workspace):
        with closing(self.connect()) as db:
            self._workspace(db,workspace)
            return [dict(row,details=json.loads(row['details'])) for row in db.execute('SELECT * FROM audit WHERE workspace_id=? ORDER BY id',(workspace,))]

    def advance_days(self, workspace, days):
        """演示者CLI控制模拟日期；不注册为HTTP或Agent工具。"""
        integer(days,'推进天数',1,365)
        with self.transaction() as db:
            ws = self._workspace(db,workspace)
            day = date.fromisoformat(ws['as_of'])+timedelta(days=days)
            db.execute('UPDATE workspaces SET as_of=? WHERE id=?',(day.isoformat(),workspace))
            orders = list(db.execute("SELECT * FROM orders WHERE workspace_id=? AND status='pending_settlement' AND settlement_date<=?",(workspace,day.isoformat())))
            for order in orders:
                amount = order['amount_minor']
                db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?',(amount,order['account_id']))
                self._ledger(db,workspace,order['account_id'],day,'模拟赎回结算',amount,amount,'investment_redemption',order['operation_id'])
                db.execute("UPDATE orders SET status='settled' WHERE id=?",(order['id'],))
                op = self._operation(db,workspace,order['operation_id'])
                result = json.loads(op['result'])
                result.update(status='settled',settled_on=day.isoformat(),balance_after_minor=self._account(db,workspace,order['account_id'])['balance_minor'])
                db.execute("UPDATE operations SET state='settled',result=? WHERE id=?",(encoded(result),op['id']))
                self._audit(db,workspace,'redemption_settled',{'order_id':order['id'],'amount_minor':amount},op['id'])
            self._bump(db,workspace)
            self._audit(db,workspace,'simulation_date_advanced',{'from':ws['as_of'],'to':day.isoformat(),'settled_orders':len(orders)})
            return {'as_of':day.isoformat(),'settled_orders':len(orders),'environment':'simulation'}
