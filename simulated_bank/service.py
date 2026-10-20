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
from contextlib import closing, contextmanager, ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path

from .customer_archive import guard_version, migrate, seed_new_workspace, read_archive, coverage, verify_invariants
from .customer_schema import SCHEMA_VERSION, contract
from .agent_guard import SCHEMA as AGENT_SCHEMA, register as register_agent_draft, require_approved
from .remittance import initialize as initialize_remittances
from .aa_payments import initialize as initialize_aa
from .remittance_contacts import initialize as initialize_remittance_contacts


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
    def __init__(self, database, *, fixture=None, clock=time.time,
                 sms_provider=None, allow_test_sms_provider=False):
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        fixture = Path(fixture) if fixture else Path(__file__).parent/'fixtures/demo.json'
        self.fixture = json.loads(fixture.read_text())
        self.seed_hash = digest(encoded(self.fixture))
        guard_version(self)
        self._initialize()
        migrate(self)
        with closing(self.connect()) as db, db:
            db.executescript(AGENT_SCHEMA)
            initialize_remittances(db)
            initialize_aa(db)
            initialize_remittance_contacts(db)
            from .permission_policy import BankPermissions
            self.permissions = BankPermissions(self, sms_provider=sms_provider,
                                               allow_test_sms_provider=allow_test_sms_provider)
            self.permissions.initialize(db)
            from .security_credentials import SCHEMA as PASSWORD_SCHEMA
            db.executescript(PASSWORD_SCHEMA)
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
            identity_guard=getattr(self,'request_identity_guard',None)
            if identity_guard:identity_guard(db)
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
CREATE TABLE IF NOT EXISTS periodic_candidate_marks(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), candidate_id TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('confirmed','ignored')), details TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(workspace_id,candidate_id));
CREATE TABLE IF NOT EXISTS user_memory_settings(
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS user_memories(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), category TEXT NOT NULL
 CHECK(category IN ('goal','planned_commitment','liquidity_preference','product_interest','knowledge_preference')),
 value TEXT NOT NULL CHECK(length(value) BETWEEN 1 AND 240), origin TEXT NOT NULL CHECK(origin='user_declared'),
 user_confirmed INTEGER NOT NULL CHECK(user_confirmed=1), created_at REAL NOT NULL, updated_at REAL NOT NULL,
 expires_on TEXT, source_type TEXT NOT NULL DEFAULT 'user_entered'
 CHECK(source_type IN ('user_entered','conversation_user_statement')),
 source_ref TEXT, confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1), confirmed_at REAL);
CREATE TABLE IF NOT EXISTS user_memory_relations(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 source_node_id TEXT NOT NULL REFERENCES user_memories(id) ON DELETE CASCADE,
 target_node_id TEXT NOT NULL REFERENCES user_memories(id) ON DELETE CASCADE,
 relation TEXT NOT NULL CHECK(relation IN ('supports','conflicts_with','depends_on','related_to')),
 source_type TEXT NOT NULL DEFAULT 'user_confirmed', created_at REAL NOT NULL,
 CHECK(source_node_id<>target_node_id),
 UNIQUE(workspace_id,source_node_id,target_node_id,relation));
CREATE TABLE IF NOT EXISTS service_negotiation_drafts(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 conditions_json TEXT NOT NULL, offer_ids_json TEXT NOT NULL, memory_node_ids_json TEXT NOT NULL,
 memory_revision TEXT, share_memory INTEGER NOT NULL CHECK(share_memory IN (0,1)),
 rule_version TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('previewed','expired','superseded')),
 expires_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS service_negotiation_applications(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 draft_id TEXT NOT NULL REFERENCES service_negotiation_drafts(id), offer_id TEXT NOT NULL,
 status TEXT NOT NULL, terms_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(draft_id,offer_id));
CREATE INDEX IF NOT EXISTS tx_workspace ON transactions(workspace_id,occurred_at);
CREATE INDEX IF NOT EXISTS audit_workspace ON audit(workspace_id,id);
CREATE INDEX IF NOT EXISTS user_memories_workspace ON user_memories(workspace_id,category,updated_at);
CREATE INDEX IF NOT EXISTS user_memory_relations_workspace ON user_memory_relations(workspace_id,source_node_id,target_node_id);
CREATE INDEX IF NOT EXISTS service_negotiation_workspace ON service_negotiation_drafts(workspace_id,created_at);
CREATE INDEX IF NOT EXISTS service_negotiation_application_workspace ON service_negotiation_applications(workspace_id,created_at);
''')
            memory_columns = {row['name'] for row in db.execute('PRAGMA table_info(user_memories)')}
            if 'expires_on' not in memory_columns:
                db.execute('ALTER TABLE user_memories ADD COLUMN expires_on TEXT')
            memory_migrations = {
                'source_type': "TEXT NOT NULL DEFAULT 'user_entered'",
                'source_ref': 'TEXT',
                'confidence': 'REAL NOT NULL DEFAULT 1.0',
                'confirmed_at': 'REAL',
            }
            for name, definition in memory_migrations.items():
                if name not in memory_columns:
                    db.execute(f'ALTER TABLE user_memories ADD COLUMN {name} {definition}')
            db.execute('UPDATE user_memories SET confirmed_at=created_at WHERE confirmed_at IS NULL')
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
        connected = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_asset_migrations'").fetchone()
        if connected and db.execute('SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone():
            from zoneinfo import ZoneInfo
            result = dict(row)
            provider_day = datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).date().isoformat()
            result['as_of'] = max(row['as_of'],provider_day)
            return result
        return row

    def session(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise BankError('UNAUTHENTICATED', '请先创建或恢复模拟工作区。', 401)
        hashed = digest(token)
        with closing(self.connect()) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='demo_sessions'").fetchone():
                demo = db.execute('SELECT workspace_id,revoked,expires_at FROM demo_sessions WHERE token_hash=?', (hashed,)).fetchone()
                if demo:
                    if demo['revoked'] or demo['expires_at'] <= self.clock():
                        raise BankError('SESSION_CHANGED', '账户已切换，旧会话已失效。', 409)
                    self._workspace(db, demo['workspace_id'])
                    return demo['workspace_id'], 'human'
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
                            'local_periodic_candidate_mark',
                            'product_comparison', 'purchase', 'redemption', 'mandate_close',
                            'card_query','card_control','card_lost_report','card_application_submission','card_limit_request',
                            'simulated_transfer','scheduled_transfer','split_bill_requests','split_bill_receipts',
                            'merchant_subscription_cancellation','fund_reservations','service_booking',
                            'simulated_card_application_review','simulated_card_limit_review','simulation_clock',
                            'operation_query', 'audit'],
                'unavailable': ['real_bank_connection', 'real_money_transfer','real_merchant_cancellation',
                                'real_card_issuance','real_card_issuance_approval','real_card_limit_approval','lost_card_unfreeze',
                                'real_service_booking','real_card_controls','real_identity_verification'],
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
            records['remittances'] = [dict(r) for r in db.execute('SELECT * FROM navren_remittances WHERE workspace_id=? ORDER BY created_at,operation_id',(workspace,))]
            records['audit'] = [dict(r,details=json.loads(r['details'])) for r in db.execute('SELECT * FROM audit WHERE workspace_id=?',(workspace,))]
            demos=getattr(self,'demo_accounts',None)
            demo=demos.current(workspace) if demos and demos.is_demo(workspace) else None
            if demo:
                from base_runtime.history import cash_rows
                records['archived_transactions']+=cash_rows(demos,workspace)
            pending = sum(r['amount_minor'] for r in records['orders'] if r['status']=='pending_settlement')
            cash = sum(r['balance_minor'] for r in self._accounts(db,workspace))
            held = sum(r['amount_minor'] for r in records['holdings'])
            debt = sum(r['outstanding_minor'] for r in records['loans'])+sum(r['outstanding_minor'] for r in records['cards'] if r['type']=='credit')
            return {'environment':'simulation','as_of':ws['as_of'],'version':ws['version'],
                    'data_scope':'navren_synthetic' if demo else 'simulation',
                    'demo_source_version':demo['source_version'] if demo else None,
                    'synthetic_terminal_snapshot':demo['latest_balances'] if demo else None,
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
        demos=getattr(self,'demo_accounts',None)
        if demos and demos.is_demo(workspace):
            if type(year) is not int or not 2016<=year<=2100:raise BankError('INVALID_YEAR','年度必须为2016至2100之间的整数。')
            from base_runtime.history import analysis
            return analysis(self,workspace,str(year))
        if type(year) is not int or not 2025 <= year <= 2100:
            raise BankError('INVALID_YEAR','年度必须为2025至2100之间的整数。')
        with closing(self.connect()) as db:
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

    def _card(self, db, workspace, card_id):
        row = db.execute('SELECT * FROM cards WHERE id=? AND workspace_id=?', (card_id,workspace)).fetchone()
        if not row:
            raise BankError('NOT_FOUND','当前模拟账户没有该卡片。',404)
        return row

    def _archive_row(self, db, table, workspace, record_id):
        # table is selected only from this module's fixed internal action map.
        row = db.execute('SELECT * FROM '+table+' WHERE id=? AND workspace_id=?',(record_id,workspace)).fetchone()
        if not row:
            raise BankError('NOT_FOUND','模拟档案中没有该记录。',404)
        return row

    @staticmethod
    def _insert_archive_row(db, table, workspace, values):
        row = {'id':uuid.uuid4().hex,'workspace_id':workspace,'source_id':'qingzhou',
               'data_origin':'qingzhou_synthetic','details':'{}',**values}
        columns = ','.join(row)
        placeholders = ','.join('?' for _ in row)
        db.execute('INSERT INTO '+table+'('+columns+') VALUES('+placeholders+')',tuple(row.values()))
        return row['id']

    def _active_holds(self, db, workspace, account_id=None, exclude_id=None):
        sql = "SELECT coalesce(sum(amount_minor),0) FROM fund_holds WHERE workspace_id=? AND status='held'"
        params = [workspace]
        if account_id:
            sql += ' AND account_id=?'; params.append(account_id)
        if exclude_id:
            sql += ' AND id<>?'; params.append(exclude_id)
        total=db.execute(sql,params).fetchone()[0]
        remittance_sql="SELECT coalesce(sum(amount_minor),0) FROM navren_remittances WHERE workspace_id=? AND status='scheduled'"
        remittance_params=[workspace]
        if account_id:
            remittance_sql+=' AND account_id=?';remittance_params.append(account_id)
        return total+db.execute(remittance_sql,remittance_params).fetchone()[0]

    def _known_due_minor(self, db, workspace, account_id, day):
        end=(date.fromisoformat(day)+timedelta(days=30)).isoformat()
        total=0
        total+=db.execute("SELECT coalesce(sum(max(0,principal_minor+interest_minor+fees_minor-paid_minor)),0) FROM loan_installments i JOIN loans l ON l.workspace_id=i.workspace_id AND l.id=i.loan_id WHERE i.workspace_id=? AND l.repayment_account_id=? AND i.status IN ('scheduled','overdue') AND i.due_on<=?",
                          (workspace,account_id,end)).fetchone()[0]
        total+=db.execute("SELECT coalesce(sum(max(0,total_due_minor-paid_minor)),0) FROM credit_statements s JOIN cards c ON c.workspace_id=s.workspace_id AND c.id=s.card_id WHERE s.workspace_id=? AND c.account_id=? AND s.status='open' AND s.due_on<=?",
                          (workspace,account_id,end)).fetchone()[0]
        total+=db.execute("SELECT coalesce(sum(amount_minor),0) FROM subscriptions WHERE workspace_id=? AND account_id=? AND merchant_status='active' AND next_renewal_on<=?",
                          (workspace,account_id,end)).fetchone()[0]
        total+=db.execute("SELECT coalesce(sum(m.amount_minor),0) FROM mandates m WHERE m.workspace_id=? AND m.account_id=? AND m.status='active' AND m.next_charge_date<=? AND NOT EXISTS(SELECT 1 FROM subscriptions s WHERE s.workspace_id=m.workspace_id AND s.mandate_id=m.id)",
                          (workspace,account_id,end)).fetchone()[0]
        total+=db.execute("SELECT coalesce(sum(amount_minor),0) FROM scheduled_payments s WHERE workspace_id=? AND account_id=? AND status='active' AND next_due_on<=? "
                          "AND NOT EXISTS(SELECT 1 FROM navren_remittances r WHERE r.workspace_id=s.workspace_id AND r.scheduled_payment_id=s.id AND r.status='scheduled')",
                          (workspace,account_id,end)).fetchone()[0]
        return total

    def _transfer_limits(self, db, workspace, account_id, day, amount, check_daily=True):
        limits = {r['kind']:r['limit_minor'] for r in db.execute(
            'SELECT kind,limit_minor FROM account_limits WHERE workspace_id=? AND account_id=?',(workspace,account_id))}
        single = limits.get('transfer_single')
        daily = limits.get('transfer_daily')
        used = db.execute("SELECT coalesce(sum(-delta_minor),0) FROM transactions WHERE workspace_id=? AND account_id=? AND kind='transfer' AND delta_minor<0 AND substr(occurred_at,1,10)=?",
                          (workspace,account_id,day)).fetchone()[0]
        if single is not None and amount>single:
            raise BankError('TRANSFER_SINGLE_LIMIT','金额超过模拟单笔转账限额。',422)
        if check_daily and daily is not None and used+amount>daily:
            raise BankError('TRANSFER_DAILY_LIMIT','金额超过模拟当日转账限额。',422)

    def _record_payment(self, db, workspace, account_id, beneficiary_id, amount, day, status,
                        reference, failure_reason=None):
        return self._insert_archive_row(db,'payment_instructions',workspace,{
            'account_id':account_id,'beneficiary_id':beneficiary_id,'amount_minor':amount,'currency':'CNY',
            'status':status,'requested_on':day,'reference':reference,'failure_reason':failure_reason,
        })

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
            result=[dict(row) for row in rows]
        demos=getattr(self,'demo_accounts',None)
        if demos and demos.is_demo(workspace):
            from base_runtime.history import cash_rows
            result=cash_rows(demos,workspace,month)+result
        return result

    def analysis(self, workspace, month):
        if not isinstance(month,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
            raise BankError('INVALID_MONTH','月报需要指定YYYY-MM格式的月份。')
        demos=getattr(self,'demo_accounts',None)
        if demos and demos.is_demo(workspace):
            from base_runtime.history import analysis
            return analysis(self,workspace,month)
        rows = self.transactions(workspace, month)
        totals = {key: 0 for key in ('income', 'expense', 'refund', 'transfer', 'investment_purchase', 'investment_redemption')}
        categories = defaultdict(int)
        category_ids = defaultdict(list)
        for row in rows:
            totals[row['kind']] += row['amount_minor']
            if row['kind'] == 'expense':
                categories[row['category']] += row['amount_minor']
                category_ids[row['category']].append(row['id'])
        return {'source': 'simulation', 'month': month, 'transaction_count': len(rows),
                'recorded_range': [rows[0]['occurred_at'], rows[-1]['occurred_at']] if rows else None,
                'totals_minor': totals, 'category_expense_minor': dict(categories),
                'transaction_ids': [r['id'] for r in rows], 'category_transaction_ids': dict(category_ids),
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
        candidates = self._periodic_candidate_rows(self.transactions(workspace))
        with closing(self.connect()) as db:
            marks = {r['candidate_id']:r for r in db.execute(
                'SELECT candidate_id,decision,details,created_at,updated_at FROM periodic_candidate_marks WHERE workspace_id=?',
                (workspace,))}
        for candidate in candidates:
            mark = marks.get(candidate['candidate_id'])
            if mark:
                candidate['user_decision'] = mark['decision']
                candidate['status'] = '已确认是周期扣费（仅本地标记）' if mark['decision']=='confirmed' else '已忽略（仅本地标记）'
                candidate['marked_at'] = mark['updated_at']
        return {'rule_version': 'sim-monthly-v1', 'candidates': candidates}

    def mark_periodic_candidate(self, workspace, payload):
        strict_fields(payload, ['candidate_id','decision'], ['candidate_id','decision'])
        candidate_id, decision = payload['candidate_id'], payload['decision']
        if not isinstance(candidate_id,str) or not re.fullmatch(r'[a-f0-9]{64}',candidate_id):
            raise BankError('INVALID_CANDIDATE','周期扣费候选标识无效。')
        if decision not in ('confirmed','ignored'):
            raise BankError('INVALID_DECISION','请选择确认候选或忽略候选。')
        with self.transaction() as db:
            self._workspace(db,workspace)
            records = [dict(r) for r in db.execute(
                'SELECT * FROM transactions WHERE workspace_id=? ORDER BY occurred_at,id',(workspace,))]
            current = self._periodic_candidate_rows(records)
            candidate = next((item for item in current if item['candidate_id']==candidate_id),None)
            if not candidate:
                raise BankError('CANDIDATE_CHANGED','流水已变化，这条周期扣费候选已失效，请刷新后复核。',409)
            now = self.clock()
            old = db.execute('SELECT decision FROM periodic_candidate_marks WHERE workspace_id=? AND candidate_id=?',
                             (workspace,candidate_id)).fetchone()
            if old and old['decision']==decision:
                return {'candidate_id':candidate_id,'decision':decision,
                        'status':'已确认是周期扣费（仅本地标记）' if decision=='confirmed' else '已忽略（仅本地标记）',
                        'merchant':candidate['merchant'],'transaction_ids':candidate['transaction_ids'],
                        'notice':'此标记只保存在本地分析记录，不会修改银行代扣或商户订阅。'}
            db.execute('''INSERT INTO periodic_candidate_marks(workspace_id,candidate_id,decision,details,created_at,updated_at)
                          VALUES(?,?,?,?,?,?) ON CONFLICT(workspace_id,candidate_id) DO UPDATE SET
                          decision=excluded.decision,details=excluded.details,updated_at=excluded.updated_at''',
                       (workspace,candidate_id,decision,encoded(candidate),now,now))
            self._audit(db,workspace,'periodic_candidate_marked',{
                'candidate_id':candidate_id,'merchant':candidate['merchant'],'decision':decision,
                'previous_decision':old['decision'] if old else None,'scope':'local_analysis_only',
                'transaction_ids':candidate['transaction_ids'],'rule_version':'sim-monthly-v1'})
            return {'candidate_id':candidate_id,'decision':decision,
                    'status':'已确认是周期扣费（仅本地标记）' if decision=='confirmed' else '已忽略（仅本地标记）',
                    'merchant':candidate['merchant'],'transaction_ids':candidate['transaction_ids'],
                    'updated_at':now,'notice':'此标记只保存在本地分析记录，不会修改银行代扣或商户订阅。'}

    @staticmethod
    def _periodic_candidate_rows(rows):
        groups = defaultdict(list)
        for row in rows:
            if row['kind'] == 'expense':
                groups[(row['account_id'], row['merchant'])].append(row)
        candidates = []
        for (account, merchant), group_rows in groups.items():
            by_month = defaultdict(list)
            for row in group_rows:
                by_month[row['occurred_at'][:7]].append(row)
            if len(by_month) < 3 or any(len(v) != 1 for v in by_month.values()):
                continue
            dates = [date.fromisoformat(r['occurred_at'][:10]) for r in group_rows]
            amounts = [r['amount_minor'] for r in group_rows]
            if not all(25 <= (b-a).days <= 35 and (b.year*12+b.month)-(a.year*12+a.month) == 1 for a,b in zip(dates,dates[1:])):
                continue
            median = statistics.median_low(amounts)
            if not all(abs(amount-median) <= max(100, median//10) for amount in amounts):
                continue
            transaction_ids = [r['id'] for r in group_rows]
            candidate_id = digest(encoded({'rule_version':'sim-monthly-v1','account_id':account,
                                           'merchant':merchant,'transaction_ids':transaction_ids}))
            candidates.append({'candidate_id':candidate_id,'account_id':account,'merchant':merchant,
                               'classification':'固定生活支出' if group_rows[-1]['category'] in ('住房','生活缴费','交通') else '疑似周期扣费',
                               'amount_minor':median,'expected_next_date':add_month(dates[-1]).isoformat(),
                               'transaction_ids':transaction_ids,'mandate_id':group_rows[-1]['mandate_id'],
                               'status':'需用户核实','user_decision':None})
        return candidates

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
            if product.get('workspace_id') and product['workspace_id']!=workspace:
                if product_ids:raise BankError('NOT_FOUND','当前账户没有这条合成持仓资料。',404)
                continue
            reasons = self._eligibility(product, profile)
            output.append({**product, 'profile_eligible': not reasons, 'exclusion_reasons': reasons})
        return output

    def _eligibility(self, product, profile):
        if product.get('execution_enabled') is False or product.get('historical_synthetic_position'):
            return ['合成历史持仓缺少可交易份额和条款，仅供研究。']
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

    def _reserve_floor(self, db, workspace, account_id):
        reserve=self._profile(db,workspace)['reserve_minor']
        demos=getattr(self,'demo_accounts',None)
        if demos and demos.is_demo(workspace):
            from cashflow_timeline import demo_reserve_floors
            return demo_reserve_floors([dict(a) for a in self._accounts(db,workspace)],reserve)[account_id]
        return reserve

    def _ordinary_transfer_terms(self, payload, account):
        # 固定规则只适用于普通本机SIM人民币收款人转账；客户字段不能提供费用。
        if payload.get('action')!='transfer' or 'remittance' in payload or account['currency']!='CNY':
            return None
        return {'terms_version':'navren.sim-beneficiary-transfer.zero-fee/1','environment':'simulation',
                'scope':'ordinary_beneficiary_transfer','account_id':account['id'],
                'beneficiary_id':payload['beneficiary_id'],'amount_minor':payload['amount_minor'],
                'currency':'CNY','fee_minor':0}

    def ordinary_transfer_terms(self, db, workspace, operation):
        row=self._operation(db,workspace,operation)
        payload=json.loads(row['payload'])
        terms=self._ordinary_transfer_terms(payload,self._account(db,workspace,payload['account_id']))
        if terms is None or row['payload_hash']!=digest(encoded(payload)):
            return None
        expected={**terms,'workspace_id':workspace,'operation_id':row['id'],
                  'operation_version':row['version'],'payload_hash':row['payload_hash']}
        prepared=db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? "
                            "AND event='operation_prepared' ORDER BY id LIMIT 1",(workspace,operation)).fetchone()
        try:
            stored=json.loads(prepared['details']).get('transfer_terms') if prepared else None
        except (TypeError,ValueError):
            return None
        # Canonical JSON保留bool/int/float差别；旧草案没有该字段时不得GET现算成已披露。
        return stored if isinstance(stored,dict) and encoded(stored)==encoded(expected) else None

    def _validate_action(self, db, workspace, payload):
        action = payload['action']
        account = self._account(db, workspace, payload['account_id'])
        ws = self._workspace(db, workspace)
        if action in ('aa_create','aa_pay','aa_decline','aa_cancel'):
            from .aa_payments import validate
            return validate(self,db,workspace,payload)
        if action in ('transfer','schedule_transfer'):
            if 'remittance' in payload:
                from .remittance import validate
                details=validate(self,db,workspace,payload)
                mode=details['remittance']['mode']
                if ((action=='transfer')!=(mode=='realtime') or
                    action=='schedule_transfer' and (payload['frequency']!='once' or payload.get('end_on') is not None
                        or payload['next_due_on']!=(date.fromisoformat(ws['as_of'])+timedelta(days=1)).isoformat())):
                    raise BankError('INVALID_REMITTANCE_MODE','汇款方式或处理日期与模拟业务规则不一致。')
                self._transfer_limits(db,workspace,account['id'],ws['as_of'],details['amount_minor'],check_daily=action=='transfer')
                return details
            amount = integer(payload['amount_minor'],'转账金额',1)
            beneficiary = self._archive_row(db,'beneficiaries',workspace,payload['beneficiary_id'])
            if beneficiary['status']!='active' or not beneficiary['verified']:
                raise BankError('BENEFICIARY_NOT_VERIFIED','收款人未通过模拟核验或已被拦截。',422)
            if beneficiary['currency']!='CNY':
                raise BankError('CURRENCY_UNAVAILABLE','当前模拟转账仅支持人民币。',422)
            self._transfer_limits(db,workspace,account['id'],ws['as_of'],amount,check_daily=action=='transfer')
            if action=='transfer':
                held = self._active_holds(db,workspace,account['id'])
                if account['balance_minor']-amount-held < 0:
                    raise BankError('INSUFFICIENT_AVAILABLE','转账金额超过扣除实际冻结资金后的可支配余额。',422)
            else:
                if payload['next_due_on']<ws['as_of']:
                    raise BankError('INVALID_DUE_DATE','首次执行日期不能早于模拟业务日。',422)
                if payload.get('end_on') and (payload['frequency']!='monthly' or payload['end_on']<payload['next_due_on']):
                    raise BankError('INVALID_END_DATE','结束日期仅适用于每月转账，且不能早于首次执行日期。',422)
            details={'account':dict(account),'beneficiary':dict(beneficiary),'amount_minor':amount}
            if action=='transfer':details['transfer_terms']=self._ordinary_transfer_terms(payload,account)
            return details
        if action=='cancel_scheduled_payment':
            schedule = self._archive_row(db,'scheduled_payments',workspace,payload['scheduled_payment_id'])
            if schedule['account_id']!=account['id']:
                raise BankError('NOT_FOUND','定时转账不属于所选账户。',404)
            if schedule['status']!='active':
                raise BankError('SCHEDULE_NOT_ACTIVE','该定时转账当前不可取消。',409)
            return {'account':dict(account),'schedule':dict(schedule)}
        if action=='request_split':
            split = self._archive_row(db,'split_bills',workspace,payload['split_id'])
            if split['account_id']!=account['id']:
                raise BankError('NOT_FOUND','AA账单不属于所选账户。',404)
            if split['status']!='draft':
                raise BankError('SPLIT_NOT_DRAFT','该AA账单已提交或已结束。',409)
            people = db.execute('SELECT * FROM split_participants WHERE workspace_id=? AND split_id=? ORDER BY id',
                                (workspace,split['id'])).fetchall()
            if not people or sum(r['share_minor'] for r in people)!=split['total_minor']:
                raise BankError('SPLIT_TOTAL_MISMATCH','AA分摊金额与参与人份额不一致。',422)
            return {'account':dict(account),'split':dict(split),'participants':[dict(r) for r in people]}
        if action=='collect_split_payment':
            if db.execute('SELECT 1 FROM navren_aa_bills WHERE id=?',(payload['split_id'],)).fetchone():
                raise BankError('AA_LEGACY_COLLECTION_FORBIDDEN','新AA账单必须由实际参与人本人确认双边支付。',409)
            split = self._archive_row(db,'split_bills',workspace,payload['split_id'])
            if split['account_id']!=account['id'] or split['status'] not in ('open','partially_paid'):
                raise BankError('SPLIT_NOT_OPEN','该AA账单当前不能登记收款。',409)
            person = self._archive_row(db,'split_participants',workspace,payload['participant_id'])
            if person['split_id']!=split['id'] or person['status']!='requested':
                raise BankError('PARTICIPANT_NOT_REQUESTED','该参与人没有待收款请求。',409)
            due = person['share_minor']-person['paid_minor']
            if due<=0:
                raise BankError('SPLIT_ALREADY_PAID','该参与人的份额已结清。',409)
            return {'account':dict(account),'split':dict(split),'participant':dict(person),'amount_minor':due}
        if action=='cancel_subscription':
            subscription = self._archive_row(db,'subscriptions',workspace,payload['subscription_id'])
            if subscription['account_id']!=account['id']:
                raise BankError('NOT_FOUND','订阅不属于所选账户。',404)
            if subscription['merchant_status']!='active':
                raise BankError('SUBSCRIPTION_NOT_ACTIVE','该模拟商户订阅已经取消。',409)
            return {'account':dict(account),'subscription':dict(subscription)}
        if action=='reserve_funds':
            task = self._archive_row(db,'orchestration_tasks',workspace,payload['task_id'])
            if task['status'] not in ('draft','in_progress'):
                raise BankError('TASK_NOT_OPEN','该跨场景任务当前不能预留资金。',409)
            amount = integer(payload['amount_minor'],'预留金额',1)
            if amount>task['budget_minor']:
                raise BankError('TASK_BUDGET_EXCEEDED','预留金额超过任务预算。',422)
            if db.execute("SELECT 1 FROM fund_holds WHERE workspace_id=? AND task_id=? AND status='held'",
                          (workspace,task['id'])).fetchone():
                raise BankError('HOLD_ALREADY_ACTIVE','该任务已有生效中的模拟资金预留。',409)
            profile=self._profile(db,workspace)
            held=self._active_holds(db,workspace,account['id'])
            due=self._known_due_minor(db,workspace,account['id'],ws['as_of'])
            if account['balance_minor']-held-due-amount<self._reserve_floor(db,workspace,account['id']):
                raise BankError('INSUFFICIENT_AVAILABLE','预留后将低于模拟应急保留资金。',422)
            expires=payload['expires_on']
            if expires<=ws['as_of'] or expires>task['due_on']:
                raise BankError('INVALID_HOLD_EXPIRY','预留到期日必须晚于业务日且不晚于任务日期。',422)
            return {'account':dict(account),'task':dict(task),'amount_minor':amount,'expires_on':expires}
        if action=='release_funds':
            hold=self._archive_row(db,'fund_holds',workspace,payload['hold_id'])
            if hold['account_id']!=account['id'] or hold['status']!='held':
                raise BankError('HOLD_NOT_ACTIVE','该资金预留不属于所选账户或已不生效。',409)
            return {'account':dict(account),'hold':dict(hold)}
        if action=='book_services':
            task=self._archive_row(db,'orchestration_tasks',workspace,payload['task_id'])
            hold=self._archive_row(db,'fund_holds',workspace,payload['hold_id'])
            if task['status']!='in_progress' or hold['account_id']!=account['id'] or hold['task_id']!=task['id'] or hold['status']!='held':
                raise BankError('HOLD_NOT_ACTIVE','预约必须使用该任务当前生效的资金预留。',409)
            quote_ids=payload['quote_ids']
            if not isinstance(quote_ids,list) or not quote_ids or len(quote_ids)>6 or len(set(quote_ids))!=len(quote_ids) or any(not isinstance(x,str) for x in quote_ids):
                raise BankError('INVALID_QUOTES','请提供1至6个不重复的模拟服务报价。')
            quotes=[]
            for quote_id in quote_ids:
                quote=self._archive_row(db,'service_quotes',workspace,quote_id)
                if quote['status']!='illustrative' or quote['valid_until']<ws['as_of']:
                    raise BankError('QUOTE_EXPIRED','报价已失效或不可用于模拟预约。',409)
                if db.execute("SELECT 1 FROM service_reservations WHERE workspace_id=? AND quote_id=? AND status IN ('confirmed','completed')",
                              (workspace,quote['id'])).fetchone():
                    raise BankError('QUOTE_ALREADY_BOOKED','该报价已经关联有效预约。',409)
                quotes.append(dict(quote))
            total=sum(q['price_minor'] for q in quotes)
            if total>hold['amount_minor'] or total>task['budget_minor']:
                raise BankError('HOLD_TOO_SMALL','报价总额超过任务预算或当前资金预留。',422)
            due=self._known_due_minor(db,workspace,account['id'],ws['as_of'])
            if account['balance_minor']-total-self._active_holds(db,workspace,account['id'],hold['id'])-due < self._reserve_floor(db,workspace,account['id']):
                raise BankError('INSUFFICIENT_AVAILABLE','预约扣款后将低于应急保留资金或其他任务的预留。',422)
            return {'account':dict(account),'task':dict(task),'hold':dict(hold),'quotes':quotes,'amount_minor':total}
        if action == 'close_mandate':
            mandate = db.execute('SELECT * FROM mandates WHERE id=? AND workspace_id=? AND account_id=?', (payload['mandate_id'], workspace, account['id'])).fetchone()
            if not mandate:
                raise BankError('NOT_FOUND', '账户下没有该模拟代扣协议。', 404)
            if mandate['status'] != 'active':
                raise BankError('MANDATE_CLOSED', '该模拟代扣协议已经关闭。', 409)
            return {'mandate': dict(mandate), 'account': dict(account)}
        if action == 'apply_card':
            requested_type=payload['requested_type']
            if requested_type not in ('debit','credit','virtual'):
                raise BankError('INVALID_CARD_TYPE','申请卡片类型无效。')
            requested=integer(payload['requested_limit_minor'],'申请信用额度',0,100000000)
            if requested_type=='credit' and requested<1:
                raise BankError('INVALID_CARD_LIMIT','信用卡申请需填写正数的模拟额度。')
            if requested_type=='debit' and requested!=0:
                raise BankError('INVALID_CARD_LIMIT','借记卡申请不设置信用额度。')
            if requested_type=='virtual' and (requested<1 or payload.get('card_id')):
                raise BankError('INVALID_CARD_LIMIT','模拟虚拟卡需要正数单笔限额，不使用实体补卡关联。')
            purpose=payload.get('purpose','online_payment')
            if ('purpose' in payload and requested_type!='virtual') or purpose not in ('online_payment','subscription','travel'):
                raise BankError('INVALID_CARD_PURPOSE','模拟虚拟卡用途须为线上支付、订阅或旅行。')
            customer=db.execute('SELECT customer_id FROM account_details WHERE workspace_id=? AND account_id=?',
                                (workspace,account['id'])).fetchone()
            if not customer:
                raise BankError('NOT_FOUND','账户关联客户不存在。',404)
            connected = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_asset_migrations'").fetchone()
            connected = connected and db.execute('SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
            replacement=None
            if payload.get('card_id'):
                replacement=self._card(db,workspace,payload['card_id'])
                if connected and db.execute("SELECT 1 FROM card_applications WHERE workspace_id=? AND card_id=? AND status IN ('submitted','issued') LIMIT 1",
                                            (workspace,replacement['id'])).fetchone():
                    raise BankError('CARD_REPLACEMENT_ALREADY_REQUESTED','该卡已有补卡申请或已完成换卡，请勿重复申请。',409)
                if replacement['account_id']!=account['id'] or replacement['status']!='lost':
                    raise BankError('INVALID_REPLACEMENT_CARD','补卡申请必须关联本账户下已挂失的卡片。',422)
                if replacement['type']!=requested_type:
                    raise BankError('INVALID_REPLACEMENT_CARD','补发卡片类型必须与已挂失卡片一致。',422)
                replacement=dict(replacement)
            if connected:
                link = db.execute('SELECT * FROM connected_account_links WHERE workspace_id=? AND account_id=?',
                                  (workspace,account['id'])).fetchone()
                expected_type = 'credit_card' if replacement and requested_type=='credit' else 'bank_settlement'
                if not link or link['account_type']!=expected_type:
                    raise BankError('INVALID_CARD_APPLICATION_ACCOUNT','新卡申请需选择已连接的银行结算账户；补卡必须使用原卡账务账户。',422)
                if replacement and requested_type=='credit':
                    card_link = db.execute('SELECT facility_id FROM connected_card_links WHERE workspace_id=? AND card_id=?',
                                           (workspace,replacement['id'])).fetchone()
                    if not card_link or not card_link['facility_id']:
                        raise BankError('INVALID_REPLACEMENT_CARD','信用补卡缺少原卡的授信关联。',422)
            return {'account':dict(account),'customer_id':customer['customer_id'],
                    'requested_type':requested_type,'requested_limit_minor':requested,'replacement':replacement,
                    'virtual_terms':{'purpose':purpose,'fee_minor':0,'validity_days':365,
                        'single_transaction_limit_minor':requested,'credit_limit_minor':0,
                        'real_payment_capability':False,'terms_version':'navren.virtual-card.demo/1'} if requested_type=='virtual' else None}
        if action=='password_change':
            from .security_credentials import details as password_details
            return {'account':dict(account), **password_details(self,db,workspace,payload['replacement_id'])}
        if action in ('card_control','request_card_limit','report_card_lost','unblock_card'):
            card=self._card(db,workspace,payload['card_id'])
            if card['account_id']!=account['id']:
                raise BankError('NOT_FOUND','卡片不属于所选账户。',404)
            if action=='unblock_card':
                if card['status']!='lost' or card['expires_on']<=ws['as_of']:
                    raise BankError('CARD_UNBLOCK_UNAVAILABLE','只能解除本账户仍有效卡片的模拟挂失。',409)
                return {'account':dict(account),'card':dict(card),
                        'controls_after_unblock':'stay_disabled_until_separately_confirmed'}
            if action=='report_card_lost':
                if card['status']!='active':
                    raise BankError('CARD_NOT_ACTIVE','只有正常状态的卡片可以挂失。',409)
                return {'account':dict(account),'card':dict(card)}
            if card['status']!='active':
                raise BankError('CARD_NOT_ACTIVE','挂失或过期卡片不能修改交易控制。',409)
            if action=='card_control':
                kind=payload['control_type']
                if kind not in ('online','overseas','contactless','single_limit'):
                    raise BankError('INVALID_CARD_CONTROL','卡片交易控制类型无效。')
                control=db.execute('SELECT * FROM card_controls WHERE workspace_id=? AND card_id=? AND control_type=?',
                                  (workspace,card['id'],kind)).fetchone()
                if not control:
                    raise BankError('CARD_CONTROL_MISSING','该卡片没有此项模拟交易控制。',404)
                enabled=payload.get('enabled')
                if type(enabled) is not bool:
                    raise BankError('INVALID_CARD_CONTROL','交易开关必须为布尔值。')
                limit=control['limit_minor']
                if kind=='single_limit':
                    if enabled is not True:
                        raise BankError('INVALID_CARD_CONTROL','单笔限额不可关闭，只能调整金额。')
                    limit=integer(payload.get('limit_minor'),'单笔限额（分）',1,100000000)
                    if card['type']=='credit' and limit>card['credit_limit_minor']:
                        raise BankError('CARD_LIMIT_EXCEEDS_CREDIT','单笔限额不能超过当前信用额度。',422)
                elif 'limit_minor' in payload:
                    raise BankError('INVALID_CARD_CONTROL','交易开关不接受限额字段。')
                if enabled==bool(control['enabled']) and limit==control['limit_minor']:
                    raise BankError('NO_CHANGE','卡片控制已是该状态，无需重复提交。',409)
                return {'account':dict(account),'card':dict(card),'control':dict(control),
                        'enabled':enabled,'limit_minor':limit}
            if card['type']!='credit':
                raise BankError('CREDIT_CARD_REQUIRED','额度调整只适用于信用卡。',422)
            requested=integer(payload['requested_limit_minor'],'申请信用额度（分）',1,100000000)
            if requested<=card['credit_limit_minor']:
                raise BankError('LIMIT_NOT_INCREASED','申请额度必须高于当前额度。',422)
            if requested<card['outstanding_minor']:
                raise BankError('LIMIT_BELOW_DEBT','申请额度不能低于当前未还金额。',422)
            return {'account':dict(account),'card':dict(card),'requested_limit_minor':requested}
        amount = integer(payload['amount_minor'], '操作金额（分）', 1)
        product = self._product(db, payload['product_id'])
        if product.get('execution_enabled') is False or product.get('historical_synthetic_position'):
            raise BankError('HISTORICAL_POSITION_READ_ONLY', '合成历史持仓仅供研究，缺少可交易份额和结算条款，不能申购或赎回。', 422)
        profile = self._profile(db, workspace)
        if action == 'purchase':
            reasons = self._eligibility(product, profile)
            if reasons:
                raise BankError('UNSUITABLE_PRODUCT', ' '.join(reasons), 422)
            if amount < product['min_purchase_minor']:
                raise BankError('BELOW_MINIMUM', '金额未达到模拟产品起购要求。', 422)
            if account['balance_minor']-amount < self._reserve_floor(db,workspace,account['id']):
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

    def prepare(self, workspace, payload, *, agent_origin=False, metric_required=False):
        if not isinstance(payload,dict) or not isinstance(payload.get('action'),str):
            raise BankError('INVALID_FIELDS','操作类型缺失或格式无效。')
        action = payload['action']
        supported=('purchase','redeem','close_mandate','card_control','report_card_lost',
                    'request_card_limit','apply_card','transfer','schedule_transfer',
                    'cancel_scheduled_payment','request_split','collect_split_payment',
                    'aa_create','aa_pay','aa_decline','aa_cancel',
                    'cancel_subscription','reserve_funds','release_funds','book_services',
                    'password_change','unblock_card')
        if action not in supported:
            raise BankError('ACTION_UNAVAILABLE', '该业务未接入模拟银行。', 422)
        fields={
            'purchase':{'action','account_id','idempotency_key','product_id','amount_minor'},
            'redeem':{'action','account_id','idempotency_key','product_id','amount_minor'},
            'close_mandate':{'action','account_id','idempotency_key','mandate_id'},
            'card_control':{'action','account_id','idempotency_key','card_id','control_type','enabled','limit_minor'},
            'report_card_lost':{'action','account_id','idempotency_key','card_id'},
            'unblock_card':{'action','account_id','idempotency_key','card_id'},
            'password_change':{'action','account_id','idempotency_key','replacement_id'},
            'request_card_limit':{'action','account_id','idempotency_key','card_id','requested_limit_minor'},
            'apply_card':{'action','account_id','idempotency_key','requested_type','requested_limit_minor','card_id','purpose'},
            'transfer':{'action','account_id','idempotency_key','beneficiary_id','amount_minor','reference','remittance'},
            'schedule_transfer':{'action','account_id','idempotency_key','beneficiary_id','amount_minor','frequency','next_due_on','end_on','remittance'},
            'cancel_scheduled_payment':{'action','account_id','idempotency_key','scheduled_payment_id'},
            'request_split':{'action','account_id','idempotency_key','split_id'},
            'collect_split_payment':{'action','account_id','idempotency_key','split_id','participant_id'},
            'aa_create':{'action','account_id','idempotency_key','aa_bill'},
            'aa_pay':{'action','account_id','idempotency_key','aa_share_id','card_id'},
            'aa_decline':{'action','account_id','idempotency_key','aa_share_id'},
            'aa_cancel':{'action','account_id','idempotency_key','aa_bill_id'},
            'cancel_subscription':{'action','account_id','idempotency_key','subscription_id'},
            'reserve_funds':{'action','account_id','idempotency_key','task_id','amount_minor','expires_on'},
            'release_funds':{'action','account_id','idempotency_key','hold_id'},
            'book_services':{'action','account_id','idempotency_key','task_id','hold_id','quote_ids'},
        }[action]
        required={
            'purchase':fields,
            'redeem':fields,
            'close_mandate':fields,
            'card_control':fields-({'limit_minor'} if payload.get('control_type')!='single_limit' else set()),
            'report_card_lost':fields,
            'unblock_card':fields,
            'password_change':fields,
            'request_card_limit':fields,
            'apply_card':fields-{'card_id','purpose'},
            'transfer':fields-{'reference','remittance'},
            'schedule_transfer':fields-{'end_on','remittance'},
            'cancel_scheduled_payment':fields,
            'request_split':fields,
            'collect_split_payment':fields,
            'aa_create':fields,
            'aa_pay':fields,
            'aa_decline':fields,
            'aa_cancel':fields,
            'cancel_subscription':fields,
            'reserve_funds':fields,
            'release_funds':fields,
            'book_services':fields,
        }[action]
        if action in ('transfer','schedule_transfer') and 'remittance' in payload:
            required=(required-{'beneficiary_id'})|{'remittance'}
        strict_fields(payload, fields, required)
        non_strings={'amount_minor','requested_limit_minor','enabled','limit_minor','quote_ids','remittance','aa_bill'}
        string_fields=(required-non_strings)-{'quote_ids'}
        if action=='apply_card' and 'card_id' in payload:string_fields.add('card_id')
        if any(not isinstance(payload[k], str) or not 1 <= len(payload[k]) <= 128 for k in string_fields):
            raise BankError('INVALID_FIELDS', '对象标识和幂等键必须为不超过128字符的字符串。')
        if action=='schedule_transfer' and payload.get('end_on') is not None and (
                not isinstance(payload['end_on'],str) or not 1<=len(payload['end_on'])<=128):
            raise BankError('INVALID_FIELDS','end_on必须为不超过128字符的日期字符串。')
        if action=='transfer' and 'reference' in payload and (
                not isinstance(payload['reference'],str) or len(payload['reference'])>128):
            raise BankError('INVALID_REFERENCE','转账备注不能超过128个字符。')
        if action=='schedule_transfer':
            if payload['frequency'] not in ('once','monthly'):
                raise BankError('INVALID_FREQUENCY','定时转账频率仅支持单次或每月。')
            for key in ('next_due_on','end_on'):
                if key in payload and payload[key] is not None:
                    try:
                        if date.fromisoformat(payload[key]).isoformat()!=payload[key]: raise ValueError()
                    except ValueError: raise BankError('INVALID_DATE',key+'必须为YYYY-MM-DD日期。')
        if action=='reserve_funds':
            try:
                if date.fromisoformat(payload['expires_on']).isoformat()!=payload['expires_on']: raise ValueError()
            except ValueError: raise BankError('INVALID_DATE','expires_on必须为YYYY-MM-DD日期。')
        key = payload['idempotency_key']
        with self.transaction() as db:
            ws = self._workspace(db, workspace)
            if action=='aa_create':
                from .aa_payments import canonical
                payload=canonical(self,db,workspace,payload)
            if action in ('transfer','schedule_transfer') and 'remittance' in payload:
                from .remittance import canonical_payload
                payload=canonical_payload(self,db,workspace,payload)
            payload_hash=digest(encoded(payload))
            existing = db.execute('SELECT * FROM operations WHERE workspace_id=? AND idempotency_key=?', (workspace,key)).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash:
                    raise BankError('IDEMPOTENCY_CONFLICT', '同一幂等键不能对应不同操作内容。', 409)
                if agent_origin and not db.execute('SELECT 1 FROM agent_draft_reviews WHERE operation_id=?',
                                                   (existing['id'],)).fetchone():
                    raise BankError('IDEMPOTENCY_CONFLICT', 'Agent 不能复用用户手工草案的幂等键。', 409)
                return self._public_operation(existing)
            details = self._validate_action(db, workspace, payload)
            operation, expires = uuid.uuid4().hex, self.clock()+300
            permission, confirmation, permission_record = self.permissions.prepare(
                db, workspace, operation, payload, details, expires, ws['version'])
            db.execute('''INSERT INTO operations(id,workspace_id,idempotency_key,payload,payload_hash,action,
                       permission,state,confirmation_digest,version,expires_at,created_at)
                       VALUES(?,?,?,?,?,?,?,'pending_confirmation',?,?,?,?)''',
                       (operation,workspace,key,encoded(payload),payload_hash,action,permission,confirmation,ws['version'],expires,self.clock()))
            self.permissions.bind(db, workspace, operation, confirmation, permission_record)
            if action=='aa_create':
                from .aa_evidence import bind_draft
                bind_draft(self,db,workspace,operation,payload['aa_bill'])
            if metric_required:
                register=getattr(self,'base_metrics_register',None)
                if not register:raise BankError('METRIC_NOT_CONFIGURED','金融结果守卫未配置，草案未保存。',503)
                register(db,workspace,operation)
            prepared_details={'action':action,'permission':permission,'version':ws['version']}
            if isinstance(details.get('transfer_terms'),dict):
                prepared_details['transfer_terms']={**details['transfer_terms'],'workspace_id':workspace,
                    'operation_id':operation,'operation_version':ws['version'],'payload_hash':payload_hash}
            self._audit(db, workspace, 'operation_prepared', prepared_details, operation)
            if agent_origin:
                register_agent_draft(db, self, workspace, operation)
                # 同一事务内替代旧卡，避免新草案准备与旧确认之间出现竞态窗口。
                older = db.execute("SELECT r.operation_id FROM agent_draft_reviews r JOIN operations o ON o.id=r.operation_id "
                    "WHERE r.workspace_id=? AND r.operation_id<>? AND o.state='pending_confirmation'", (workspace,operation)).fetchall()
                for old in older:
                    db.execute("UPDATE operations SET state='invalidated',verification_hash=NULL WHERE id=?", (old[0],))
                    db.execute("UPDATE agent_draft_reviews SET status='superseded' WHERE operation_id=?", (old[0],))
                    self._audit(db,workspace,'agent_draft_superseded',{'replacement_id':operation},old[0])
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
        return self.permissions.verify_sms(workspace, operation, payload)

    def start_sms_verification(self, workspace, operation):
        with ExitStack() as evidence_locks, self.transaction() as db:
            row = self._operation(db, workspace, operation)
            self.permissions.recheck(db, workspace, row)
            self._check_pending(db,workspace,row)
            require_approved(db, self, workspace, row)
            guard=getattr(self,'base_metrics_guard',None)
            if guard:guard(db,workspace,row)
            evidence_guard=getattr(self,'base_evidence_guard',None)
            if evidence_guard:evidence_locks.enter_context(evidence_guard(db,workspace,row))
        return self.permissions.start_sms(workspace, operation)

    def confirm(self, workspace, operation, payload):
        try:
            return self._confirm_authorized(workspace,operation,payload)
        except Exception as error:
            code=error.code if isinstance(error,BankError) else 'EXECUTION_FAILED'
            # 原事务失败/拒绝已经退出；只有明确未记认证失败的早期篡改才加锁计数。
            suspicious=(code in ('CONFIRMATION_MISMATCH','INVALID_CONFIRMATION','INVALID_FIELDS','NOT_FOUND')
                        and not (isinstance(error,BankError) and error.persist))
            self.permissions.failed_attempt(workspace,operation,code,suspicious=suspicious)
            raise

    def _confirm_authorized(self, workspace, operation, payload):
        strict_fields(payload,['confirmed','confirmation_digest','verification_token'],['confirmed','confirmation_digest'])
        if payload['confirmed'] is not True:
            raise BankError('CONFIRMATION_REQUIRED','必须明确确认具体操作。')
        if not isinstance(payload['confirmation_digest'],str):
            raise BankError('INVALID_CONFIRMATION','确认摘要格式错误。')
        with ExitStack() as evidence_locks, self.transaction() as db:
            row = self._operation(db,workspace,operation)
            if not hmac.compare_digest(row['confirmation_digest'],payload['confirmation_digest']):
                self._reject(db,workspace,row,'CONFIRMATION_MISMATCH','确认内容与操作不一致。')
            # 相同已授权操作重放仅返回保存的回执，绝不重复改余额。
            if row['state'] in ('succeeded','pending_settlement','settled') or (
                    row['state'] in ('scheduled','failed') and 'remittance' in json.loads(row['payload'])):
                return self._public_operation(row)
            self.permissions.recheck(db, workspace, row)
            self._check_pending(db,workspace,row)
            require_approved(db, self, workspace, row)
            guard=getattr(self,'base_metrics_guard',None)
            if guard:guard(db,workspace,row)
            evidence_guard=getattr(self,'base_evidence_guard',None)
            if evidence_guard:evidence_locks.enter_context(evidence_guard(db,workspace,row))
            original = json.loads(row['payload'])
            details = self._validate_action(db,workspace,original)
            if row['action']=='transfer' and 'remittance' not in original and self.ordinary_transfer_terms(db,workspace,row['id']) is None:
                self._reject(db,workspace,row,'STALE_CONFIRMATION','本机模拟手续费条款尚未核对，请重新准备并确认。','invalidated')
            proof = self.permissions.execution_proof(db, workspace, row, payload.get('verification_token'), details)
            # 证明消费与原金融执行、账本和回执共用事务；后续故障全部回滚。
            self.permissions.consume(db, workspace, row, proof)
            before = details['account']['balance_minor']
            if row['action'] in ('aa_create','aa_pay','aa_decline','aa_cancel'):
                from .aa_payments import execute
                result=execute(self,db,workspace,operation,original,details)
                state='succeeded'
            elif row['action']=='transfer':
                amount=details['amount_minor']; beneficiary=details['beneficiary']
                if 'remittance' in original:
                    from .remittance import beneficiary as create_beneficiary
                    beneficiary={**beneficiary,'id':create_beneficiary(self,db,workspace,original['remittance'])}
                as_of=date.fromisoformat(self._workspace(db,workspace)['as_of'])
                db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=? AND workspace_id=?',
                           (amount,original['account_id'],workspace))
                reference=(original['remittance']['purpose']+(' · '+original['remittance']['message'] if original['remittance']['message'] else '')) if 'remittance' in original else (original.get('reference') or '用户确认的模拟转账')
                transaction_id=self._ledger(db,workspace,original['account_id'],as_of,beneficiary['display_name'],amount,-amount,
                             'transfer',operation,category='汇款' if 'remittance' in original else '转账',note=reference)
                payment_id=self._record_payment(db,workspace,original['account_id'],beneficiary['id'],amount,
                                                as_of.isoformat(),'succeeded',reference)
                after=self._account(db,workspace,original['account_id'])['balance_minor']
                state='succeeded'
                result={'payment_id':payment_id,'beneficiary':beneficiary['display_name'],'amount_minor':amount,
                        'status':'succeeded','balance_before_minor':before,'balance_after_minor':after,
                        'notice':'仅在本机模拟账本记账；未向真实收款账户转账。'}
                if 'remittance' in original:
                    from .remittance import execution_time
                    execute_at,execute_on=execution_time(self,original['remittance']['mode'])
                    result.update(transaction_id=transaction_id,execute_on=execute_on,held_minor=0,
                        available_after_minor=max(0,after-self._active_holds(db,workspace,original['account_id'])),
                        mode=original['remittance']['mode'],execute_at=execute_at,processed_at=self.clock(),
                        business_on=as_of.isoformat(),environment='simulation')
            elif row['action']=='schedule_transfer':
                beneficiary_id=details['beneficiary']['id']
                if 'remittance' in original:
                    from .remittance import beneficiary as create_beneficiary
                    beneficiary_id=create_beneficiary(self,db,workspace,original['remittance'])
                schedule_id=self._insert_archive_row(db,'scheduled_payments',workspace,{
                    'account_id':original['account_id'],'beneficiary_id':beneficiary_id,
                    'amount_minor':details['amount_minor'],'frequency':original['frequency'],
                    'next_due_on':original['next_due_on'],'status':'active','end_on':original.get('end_on'),
                })
                state='succeeded'
                result={'scheduled_payment_id':schedule_id,'status':'active','frequency':original['frequency'],
                        'next_due_on':original['next_due_on'],'amount_minor':details['amount_minor'],
                        'notice':'模拟定时转账已启用；到期执行仅发生在本机模拟账本。'}
                if 'remittance' in original:
                    from .remittance import execution_time
                    execute_at,execute_on=execution_time(self,original['remittance']['mode'])
                    state='scheduled'
                    result.update(status='scheduled',execute_on=execute_on,execute_at=execute_at,
                        execute_business_on=original['next_due_on'],held_minor=details['amount_minor'],
                        balance_before_minor=before,balance_after_minor=before,transaction_id=None,payment_id=None,
                        available_after_minor=max(0,before-self._active_holds(db,workspace,original['account_id'])-details['amount_minor']),
                        mode=original['remittance']['mode'],environment='simulation',
                        notice='模拟汇款已受理并冻结资金；到期在本机处理，尚未向真实银行转账。')
            elif row['action']=='cancel_scheduled_payment':
                schedule=details['schedule']
                db.execute("UPDATE scheduled_payments SET status='cancelled' WHERE workspace_id=? AND id=?",
                           (workspace,schedule['id']))
                state='succeeded'
                result={'scheduled_payment_id':schedule['id'],'status':'cancelled',
                        'notice':'后续模拟转账已取消；不会影响其他账户或真实银行安排。'}
                remittance=db.execute("SELECT * FROM navren_remittances WHERE workspace_id=? AND scheduled_payment_id=? AND status='scheduled'",(workspace,schedule['id'])).fetchone()
                if remittance:
                    from .remittance import finish
                    finish(self,db,remittance,{'status':'cancelled','amount_minor':remittance['amount_minor'],
                        'scheduled_payment_id':schedule['id'],'execute_on':remittance['execute_on'],'held_minor':0,
                        'balance_before_minor':before,'balance_after_minor':before,'mode':remittance['mode'],
                        'available_after_minor':max(0,before-self._active_holds(db,workspace,original['account_id'])+remittance['amount_minor']),
                        'notice':'模拟汇款已取消，冻结资金已释放；未发生扣款。','environment':'simulation'})
            elif row['action']=='request_split':
                split=details['split']
                db.execute("UPDATE split_bills SET status='open' WHERE workspace_id=? AND id=?",
                           (workspace,split['id']))
                for person in details['participants']:
                    db.execute("UPDATE split_participants SET status='requested',request_ref=? WHERE workspace_id=? AND id=?",
                               ('simulation://split/'+split['id']+'/'+person['id'],workspace,person['id']))
                state='succeeded'
                result={'split_id':split['id'],'status':'open','participant_count':len(details['participants']),
                        'total_minor':split['total_minor'],'notice':'AA收款请求已写入模拟档案；未向真实联系人发送消息。'}
            elif row['action']=='collect_split_payment':
                person=details['participant']; split=details['split']; amount=details['amount_minor']
                db.execute("UPDATE split_participants SET paid_minor=share_minor,status='paid' WHERE workspace_id=? AND id=?",
                           (workspace,person['id']))
                remaining=db.execute('SELECT coalesce(sum(share_minor-paid_minor),0) FROM split_participants WHERE workspace_id=? AND split_id=?',
                                     (workspace,split['id'])).fetchone()[0]
                split_status='paid' if remaining==0 else 'partially_paid'
                db.execute('UPDATE split_bills SET status=? WHERE workspace_id=? AND id=?',(split_status,workspace,split['id']))
                as_of=date.fromisoformat(self._workspace(db,workspace)['as_of'])
                db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=? AND workspace_id=?',
                           (amount,original['account_id'],workspace))
                self._ledger(db,workspace,original['account_id'],as_of,person['display_name'],amount,amount,
                             'transfer',operation,category='AA分摊收款',note='演示者登记的模拟收款')
                after=self._account(db,workspace,original['account_id'])['balance_minor']
                state='succeeded'
                result={'split_id':split['id'],'participant_id':person['id'],'amount_minor':amount,
                        'split_status':split_status,'balance_before_minor':before,'balance_after_minor':after,
                        'notice':'仅登记为模拟入账；没有从真实联系人或银行收款。'}
            elif row['action']=='cancel_subscription':
                subscription=details['subscription']; as_of=self._workspace(db,workspace)['as_of']
                db.execute("UPDATE subscriptions SET merchant_status='cancelled' WHERE workspace_id=? AND id=?",
                           (workspace,subscription['id']))
                self._insert_archive_row(db,'subscription_events',workspace,{
                    'subscription_id':subscription['id'],'event_type':'cancelled','occurred_on':as_of,
                    'status':'recorded','amount_minor':subscription['amount_minor'],
                })
                state='succeeded'
                result={'subscription_id':subscription['id'],'merchant_status':'cancelled',
                        'mandate_status':'unchanged','notice':'仅将本机模拟商户订阅标记为已取消；银行代扣授权保持原状。'}
            elif row['action']=='reserve_funds':
                task=details['task']; as_of=self._workspace(db,workspace)['as_of']
                hold_id=self._insert_archive_row(db,'fund_holds',workspace,{
                    'account_id':original['account_id'],'task_id':task['id'],'amount_minor':details['amount_minor'],
                    'status':'held','created_on':as_of,'expires_on':details['expires_on'],'reason':'用户确认的模拟任务资金预留',
                })
                db.execute("UPDATE orchestration_tasks SET status='in_progress' WHERE workspace_id=? AND id=?",
                           (workspace,task['id']))
                db.execute("UPDATE task_steps SET status='succeeded' WHERE workspace_id=? AND task_id=? AND action='reserve_funds' AND status='planned'",
                           (workspace,task['id']))
                state='succeeded'
                result={'task_id':task['id'],'hold_id':hold_id,'status':'held','amount_minor':details['amount_minor'],
                        'balance_before_minor':before,'balance_after_minor':before,
                        'notice':'资金已在模拟预算中锁定，不会扣款；可继续执行该任务或取消预留。'}
            elif row['action']=='release_funds':
                hold=details['hold']
                db.execute("UPDATE fund_holds SET status='released' WHERE workspace_id=? AND id=?",
                           (workspace,hold['id']))
                db.execute("UPDATE orchestration_tasks SET status='cancelled' WHERE workspace_id=? AND id=? AND status='in_progress'",
                           (workspace,hold['task_id']))
                db.execute("UPDATE task_steps SET status='cancelled' WHERE workspace_id=? AND task_id=? AND status='planned'",
                           (workspace,hold['task_id']))
                state='succeeded'
                result={'hold_id':hold['id'],'status':'released','amount_minor':hold['amount_minor'],
                        'notice':'模拟资金预留已解除，没有产生扣款。'}
            elif row['action']=='book_services':
                task,hold,quotes=details['task'],details['hold'],details['quotes']
                amount=details['amount_minor']; as_of=date.fromisoformat(self._workspace(db,workspace)['as_of'])
                db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=? AND workspace_id=?',
                           (amount,original['account_id'],workspace))
                self._ledger(db,workspace,original['account_id'],as_of,'模拟服务预约',amount,-amount,
                             'expense',operation,category='服务预订',note='用户确认的跨场景模拟预约')
                reservation_ids=[]
                for quote in quotes:
                    reservation_ids.append(self._insert_archive_row(db,'service_reservations',workspace,{
                        'task_id':task['id'],'quote_id':quote['id'],'delivery_on':task['due_on'],
                        'status':'confirmed','amount_minor':quote['price_minor'],
                        'external_reference':'SIM-RESERVATION-'+uuid.uuid4().hex[:12].upper(),
                    }))
                    provider=db.execute('SELECT service_type FROM service_providers WHERE workspace_id=? AND id=?',
                                        (workspace,quote['provider_id'])).fetchone()
                    if provider:
                        db.execute("UPDATE task_steps SET status='succeeded' WHERE workspace_id=? AND task_id=? AND action=?",
                                   (workspace,task['id'],'book_'+provider['service_type']))
                db.execute("UPDATE fund_holds SET status='consumed' WHERE workspace_id=? AND id=?",
                           (workspace,hold['id']))
                db.execute("UPDATE orchestration_tasks SET status='completed' WHERE workspace_id=? AND id=?",
                           (workspace,task['id']))
                db.execute("UPDATE task_steps SET status='succeeded' WHERE workspace_id=? AND task_id=? AND action='check_budget'",
                           (workspace,task['id']))
                after=self._account(db,workspace,original['account_id'])['balance_minor']
                state='succeeded'
                result={'task_id':task['id'],'reservation_ids':reservation_ids,'hold_id':hold['id'],
                        'status':'completed','amount_minor':amount,'balance_before_minor':before,
                        'balance_after_minor':after,
                        'notice':'服务已在本机模拟数据中预约并记账；未连接或通知真实服务商。'}
            elif row['action']=='close_mandate':
                mandate = details['mandate']
                db.execute("UPDATE mandates SET status='closed' WHERE id=?", (mandate['id'],))
                state = 'succeeded'
                result = {'mandate_id':mandate['id'],'status':'closed','merchant_contract_status':'unchanged',
                          'notice':'仅关闭本模拟银行代扣协议，商户订阅合同未取消。'}
            elif row['action']=='card_control':
                card,control=details['card'],details['control']
                db.execute('UPDATE card_controls SET enabled=?,limit_minor=?,updated_on=? WHERE workspace_id=? AND card_id=? AND control_type=?',
                           (int(details['enabled']),details['limit_minor'],self._workspace(db,workspace)['as_of'],
                            workspace,card['id'],control['control_type']))
                state='succeeded'
                result={'card_id':card['id'],'control_type':control['control_type'],
                        'enabled':details['enabled'],'limit_minor':details['limit_minor'],
                        'notice':'模拟卡片交易控制已更新，不代表真实银行卡设置。'}
            elif row['action']=='report_card_lost':
                card=details['card']; as_of=self._workspace(db,workspace)['as_of']
                db.execute("UPDATE cards SET status='lost' WHERE workspace_id=? AND id=?",(workspace,card['id']))
                db.execute('UPDATE card_controls SET enabled=0,updated_on=? WHERE workspace_id=? AND card_id=?',
                           (as_of,workspace,card['id']))
                customer_event=uuid.uuid4().hex
                db.execute('INSERT INTO card_events(id,workspace_id,source_id,data_origin,details,card_id,event_type,occurred_on,status,reason) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (customer_event,workspace,'qingzhou','qingzhou_synthetic','{}',card['id'],'lost',as_of,'recorded','用户确认的模拟挂失'))
                state='succeeded'
                result={'card_id':card['id'],'status':'lost',
                        'notice':'模拟卡片已挂失，交易控制已全部关闭；不代表已通知真实银行。'}
            elif row['action']=='unblock_card':
                card=details['card'];as_of=self._workspace(db,workspace)['as_of']
                db.execute("UPDATE cards SET status='active' WHERE workspace_id=? AND id=?",(workspace,card['id']))
                self._insert_archive_row(db,'card_events',workspace,{'card_id':card['id'],'event_type':'unblocked',
                    'occurred_on':as_of,'status':'recorded','reason':'本人确认并完成短信证明的模拟解挂'})
                state='succeeded'
                result={'card_id':card['id'],'status':'active','controls_reenabled':False,
                        'notice':'已解除模拟挂失；交易开关仍保持关闭，需单独确认开启。'}
            elif row['action']=='password_change':
                from .security_credentials import execute as execute_password
                result=execute_password(self,db,workspace,operation,original['replacement_id'])
                state='succeeded'
            elif row['action']=='request_card_limit':
                card=details['card']; as_of=self._workspace(db,workspace)['as_of']
                request_id=uuid.uuid4().hex
                db.execute('INSERT INTO card_limit_requests(id,workspace_id,source_id,data_origin,details,card_id,requested_limit_minor,status,requested_on,reason) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (request_id,workspace,'qingzhou','qingzhou_synthetic','{}',card['id'],
                            details['requested_limit_minor'],'submitted',as_of,'用户确认的模拟额度申请，等待模拟审核'))
                state='succeeded'
                result={'card_id':card['id'],'request_id':request_id,'status':'submitted',
                        'requested_limit_minor':details['requested_limit_minor'],
                        'current_limit_minor':card['credit_limit_minor'],
                        'notice':'模拟额度申请已提交并等待审核；当前信用额度没有变化。'}
            elif row['action']=='apply_card':
                as_of=self._workspace(db,workspace)['as_of']; application_id=uuid.uuid4().hex
                virtual_card=None
                if details['requested_type']=='virtual':
                    virtual_card=self._insert_archive_row(db,'cards',workspace,{
                        'customer_id':details['customer_id'],'account_id':original['account_id'],'type':'virtual',
                        'network':'SIM','last_four':f'{secrets.randbelow(10000):04d}','status':'active',
                        'issued_on':as_of,'expires_on':(date.fromisoformat(as_of)+timedelta(days=365)).isoformat(),
                        'credit_limit_minor':0,'outstanding_minor':0})
                    for kind in ('online','overseas','contactless','single_limit'):
                        self._insert_archive_row(db,'card_controls',workspace,{'card_id':virtual_card,'control_type':kind,
                            'enabled':int(kind in ('online','single_limit')),'limit_minor':details['requested_limit_minor'] if kind=='single_limit' else 0,
                            'updated_on':as_of})
                    self._insert_archive_row(db,'card_events',workspace,{'card_id':virtual_card,'event_type':'issued',
                        'occurred_on':as_of,'status':'recorded','reason':'本人确认的本地模拟虚拟卡；无真实卡号和支付能力'})
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='connected_asset_migrations'").fetchone() and db.execute(
                            'SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone():
                        db.execute('INSERT INTO connected_card_links VALUES(?,?,?,?,?)',
                                   (workspace,virtual_card,'runtime:card:'+virtual_card,None,'payment'))
                db.execute('INSERT INTO card_applications(id,workspace_id,source_id,data_origin,details,customer_id,card_id,requested_type,requested_limit_minor,status,requested_on,decision_reason) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (application_id,workspace,'qingzhou','qingzhou_synthetic',encoded({'account_id':details['account']['id']}),details['customer_id'],
                            virtual_card or (details['replacement']['id'] if details['replacement'] else None),
                            details['requested_type'],details['requested_limit_minor'],'issued' if virtual_card else 'submitted',as_of,
                            '本地模拟虚拟卡已生成；无真实卡号、授信或对外支付能力。' if virtual_card else '仅受理模拟申请；未生成卡片或授信结果。'))
                state='succeeded'
                result={'application_id':application_id,'status':'submitted','requested_type':details['requested_type'],
                        'requested_limit_minor':details['requested_limit_minor'],
                        'replacement_card_id':details['replacement']['id'] if details['replacement'] else None,
                        'notice':'模拟补卡申请已提交；尚未审批，不代表已核发或授信。' if details['replacement'] else
                                '模拟卡片申请已提交；尚未审批，不代表已核发或授信。'}
                if virtual_card:
                    result.update(card_id=virtual_card,status='issued',fee_minor=0,validity_days=365,
                        single_transaction_limit_minor=details['requested_limit_minor'],credit_limit_minor=0,
                        notice='本地模拟虚拟卡已生成；无真实卡号、授信或对外支付能力。')
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
            if 'remittance' in original:
                from .remittance import save
                save(self,db,workspace,operation,original,result)
            db.execute('UPDATE operations SET state=?,result=?,verification_hash=NULL WHERE id=?', (state,encoded(result),operation))
            self._audit(db,workspace,'permission_executed',{'policy':self.permissions.status(db,workspace,operation)['policy'],
                'grade':row['permission'],'mfa_source':proof['source'] if proof else None,
                'real_SMS_acceptance':False},operation)
            self._bump(db,workspace)
            self._audit(db,workspace,'operation_executed',{'state':state,'result':result},operation)
            return self._public_operation(self._operation(db,workspace,operation))

    def _ledger(self, db, workspace, account, day, merchant, amount, delta, kind, operation,
                category='投资流转',note='模拟订单记账'):
        # 分账户接入后的新流水使用提供者时钟；封存旧样本仍保持原日期语义。
        connected = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_asset_migrations'").fetchone()
        connected = connected and db.execute('SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
        if connected:
            from datetime import timezone
            from zoneinfo import ZoneInfo
            current = datetime.fromtimestamp(self.clock(),timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
            occurred_at = current.isoformat(timespec='seconds') if day==current.date() else day.isoformat()+'T12:00:00+08:00'
        else:
            occurred_at = day.isoformat()+'T12:00:00+08:00'
        transaction_id=uuid.uuid4().hex
        db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)',
                   (transaction_id,workspace,account,occurred_at,merchant,amount,delta,kind,category,note,operation))
        return transaction_id

    def remittances(self,workspace,operation=None,*,process_due=True):
        from .remittance import preview
        if process_due:self.process_due_remittances(workspace)
        with closing(self.connect()) as db:
            ws=self._workspace(db,workspace)
            if operation:return preview(self,db,workspace,operation)
            rows=db.execute("SELECT id,payload FROM operations WHERE workspace_id=? AND action IN ('transfer','schedule_transfer') ORDER BY created_at DESC,id",(workspace,)).fetchall()
            return {'environment':'simulation','as_of':ws['as_of'],
                    'remittances':[preview(self,db,workspace,r['id']) for r in rows if 'remittance' in json.loads(r['payload'])]}

    def process_due_remittances(self,workspace):
        """读取时仅处理先前本人已授权的到期汇款，不推进其他金融业务。"""
        with self.transaction() as db:
            ws=self._workspace(db,workspace)
            due=[r['scheduled_payment_id'] for r in db.execute(
                "SELECT r.scheduled_payment_id FROM navren_remittances r JOIN scheduled_payments s "
                "ON s.workspace_id=r.workspace_id AND s.id=r.scheduled_payment_id "
                "WHERE r.workspace_id=? AND r.status='scheduled' AND s.status='active' "
                "AND r.execute_at<=?",(workspace,self.clock()))]
            if not due:return {'processed':0,'failed':0,'environment':'simulation'}
            self.permissions.write_allowed(db,workspace)
            completed,failed=self._process_scheduled_transfers(db,workspace,date.fromisoformat(ws['as_of']),schedule_ids=due)
            self._bump(db,workspace)
            return {'processed':completed,'failed':failed,'environment':'simulation'}

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

    def _process_scheduled_transfers(self, db, workspace, day, *, schedule_ids=None):
        completed=failed=0
        if schedule_ids is None:
            schedules=list(db.execute("SELECT * FROM scheduled_payments WHERE workspace_id=? AND status='active' AND next_due_on<=? ORDER BY next_due_on,id",
                                      (workspace,day.isoformat())))
        else:
            schedules=list(db.execute("SELECT * FROM scheduled_payments WHERE workspace_id=? AND status='active' AND id IN ("+
                ','.join('?' for _ in schedule_ids)+") ORDER BY next_due_on,id",(workspace,*schedule_ids)))
        for schedule in schedules:
            account=self._account(db,workspace,schedule['account_id'])
            beneficiary=self._archive_row(db,'beneficiaries',workspace,schedule['beneficiary_id'])
            remittance=db.execute("SELECT * FROM navren_remittances WHERE workspace_id=? AND scheduled_payment_id=? AND status='scheduled'",(workspace,schedule['id'])).fetchone()
            before=account['balance_minor']
            reason=None
            if remittance:
                from .remittance import validate
                original=json.loads(self._operation(db,workspace,remittance['operation_id'])['payload'])
                try:
                    if (original['amount_minor']!=schedule['amount_minor'] or remittance['amount_minor']!=schedule['amount_minor']
                            or original['account_id']!=schedule['account_id'] or remittance['account_id']!=schedule['account_id']):
                        raise BankError('REMITTANCE_CHANGED','已授权汇款的金额或账户关联已变化。',409)
                    validate(self,db,workspace,original,exclude_operation=remittance['operation_id'])
                except BankError as error:reason=error.message
            if beneficiary['status']!='active' or not beneficiary['verified']:
                reason='模拟收款人未通过核验或已被拦截。'
            else:
                try:
                    self._transfer_limits(db,workspace,account['id'],day.isoformat(),schedule['amount_minor'])
                except BankError as error:
                    reason=error.message
            held=self._active_holds(db,workspace,account['id'])
            profile=self._profile(db,workspace)
            # _known_due_minor also includes this scheduled payment. Subtract it
            # once while protecting all other near-term commitments from it.
            due=self._known_due_minor(db,workspace,account['id'],day.isoformat())
            other_due=max(0,due-schedule['amount_minor'])
            if not remittance and not reason and account['balance_minor']-schedule['amount_minor']-held-other_due<self._reserve_floor(db,workspace,account['id']):
                reason='模拟账户可用余额不足，未执行定时转账。'
            if reason:
                payment_id=self._record_payment(db,workspace,account['id'],beneficiary['id'],
                    schedule['amount_minor'],day.isoformat(),'failed','定时转账',reason)
                db.execute("UPDATE scheduled_payments SET status='failed' WHERE workspace_id=? AND id=?",
                           (workspace,schedule['id']))
                self._audit(db,workspace,'scheduled_transfer_failed',{'schedule_id':schedule['id'],
                    'payment_id':payment_id,'reason':reason,'date':day.isoformat()})
                if remittance:
                    from .remittance import finish
                    finish(self,db,remittance,{'status':'failed','payment_id':payment_id,'transaction_id':None,
                        'scheduled_payment_id':schedule['id'],'amount_minor':schedule['amount_minor'],
                        'balance_before_minor':before,'balance_after_minor':before,'held_minor':0,
                        'execute_on':remittance['execute_on'],'execute_at':remittance['execute_at'],
                        'processed_at':self.clock(),'business_on':day.isoformat(),'failure_reason':reason,
                        'available_after_minor':max(0,before-held+remittance['amount_minor']),
                        'mode':remittance['mode'],'environment':'simulation','notice':'模拟汇款未扣款，冻结资金已释放。'})
                failed+=1
                continue
            db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE workspace_id=? AND id=?',
                       (schedule['amount_minor'],workspace,account['id']))
            reference=(original['remittance']['purpose']+(' · '+original['remittance']['message'] if original['remittance']['message'] else '')) if remittance else '已确认的模拟定时转账'
            transaction_id=self._ledger(db,workspace,account['id'],day,beneficiary['display_name'],schedule['amount_minor'],
                         -schedule['amount_minor'],'transfer',remittance['operation_id'] if remittance else None,
                         category='汇款' if remittance else '定时转账',note=reference)
            payment_id=self._record_payment(db,workspace,account['id'],beneficiary['id'],
                schedule['amount_minor'],day.isoformat(),'succeeded',reference)
            next_due=add_month(date.fromisoformat(schedule['next_due_on'])) if schedule['frequency']=='monthly' else None
            complete=(schedule['frequency']=='once' or (schedule['end_on'] and next_due.isoformat()>schedule['end_on']))
            db.execute('UPDATE scheduled_payments SET status=?,next_due_on=? WHERE workspace_id=? AND id=?',
                       ('completed' if complete else 'active',schedule['next_due_on'] if complete else next_due.isoformat(),
                        workspace,schedule['id']))
            self._audit(db,workspace,'scheduled_transfer_executed',{'schedule_id':schedule['id'],
                'payment_id':payment_id,'amount_minor':schedule['amount_minor'],'date':day.isoformat()})
            if remittance:
                from .remittance import finish
                after=self._account(db,workspace,account['id'])['balance_minor']
                finish(self,db,remittance,{'status':'succeeded','payment_id':payment_id,'transaction_id':transaction_id,
                    'scheduled_payment_id':schedule['id'],'amount_minor':schedule['amount_minor'],
                    'balance_before_minor':before,'balance_after_minor':after,'held_minor':0,
                    'execute_on':remittance['execute_on'],'execute_at':remittance['execute_at'],
                    'processed_at':self.clock(),'business_on':day.isoformat(),
                    'available_after_minor':max(0,after-held+schedule['amount_minor']),
                    'mode':remittance['mode'],'environment':'simulation','notice':'模拟汇款已记账，未向真实银行发送资金。'})
            completed+=1
        return completed,failed

    def _process_card_reviews(self, db, workspace, day, *, limit=None):
        applications=limits=0
        if limit is not None:integer(limit,'审核队列批量',1,2000)
        limit_sql='' if limit is None else ' LIMIT '+str(limit)
        kyc=db.execute("SELECT * FROM kyc_reviews WHERE workspace_id=? AND status='passed' AND expires_on>=? AND sanctions_match=0 ORDER BY reviewed_on DESC LIMIT 1",
                       (workspace,day.isoformat())).fetchone()
        employment=db.execute('SELECT annual_income_minor FROM employment WHERE workspace_id=? ORDER BY id LIMIT 1',(workspace,)).fetchone()
        cap=max(0,(employment['annual_income_minor']//4) if employment else 0)
        for application in list(db.execute("SELECT * FROM card_applications WHERE workspace_id=? AND status='submitted' AND requested_on<? ORDER BY requested_on,id"+limit_sql,
                                           (workspace,day.isoformat()))):
            allowed=bool(kyc) and (application['requested_type']=='debit' or application['requested_limit_minor']<=cap)
            if not allowed:
                reason='模拟审核未通过：身份材料或演示额度条件不满足。'
                db.execute("UPDATE card_applications SET status='declined',decision_reason=? WHERE workspace_id=? AND id=?",
                           (reason,workspace,application['id']))
                self._audit(db,workspace,'card_application_declined',{'application_id':application['id'],'reason':reason})
                applications+=1
                continue
            card_id=uuid.uuid4().hex
            existing=db.execute('SELECT network FROM cards WHERE workspace_id=? ORDER BY id LIMIT 1',(workspace,)).fetchone()
            suffix=str(int(hashlib.sha256(application['id'].encode()).hexdigest()[:8],16)%10000).zfill(4)
            while db.execute('SELECT 1 FROM cards WHERE workspace_id=? AND last_four=?',(workspace,suffix)).fetchone():
                suffix=str((int(suffix)+1)%10000).zfill(4)
            if application['card_id']:
                issued_account=self._card(db,workspace,application['card_id'])['account_id']
            else:
                account_row=db.execute('SELECT account_id FROM account_details WHERE workspace_id=? AND customer_id=? ORDER BY id LIMIT 1',
                                       (workspace,application['customer_id'])).fetchone()
                if not account_row:
                    raise RuntimeError('模拟卡片申请没有关联账户')
                issued_account=account_row['account_id']
            connected = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_asset_migrations'").fetchone()
            connected = connected and db.execute('SELECT 1 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
            connected_facility = None
            connected_role = 'primary'
            connected_details = {}
            if connected:
                from .connected_assets import _id
                if not application['card_id']:
                    # 使用用户申请时已经校验的结算账户，不替换发行机构。
                    selected_account = json.loads(application['details']).get('account_id')
                    account_row = db.execute("SELECT l.account_id,l.connection_id FROM connected_account_links l "
                        "JOIN account_details d ON d.workspace_id=l.workspace_id AND d.account_id=l.account_id "
                        "WHERE l.workspace_id=? AND l.account_id=? AND d.customer_id=? AND l.account_type='bank_settlement'",
                        (workspace,selected_account,application['customer_id'])).fetchone()
                    if not account_row:
                        raise RuntimeError('模拟卡片申请没有已连接的银行结算账户')
                    issued_account = account_row['account_id']
                link = db.execute('SELECT * FROM connected_account_links WHERE workspace_id=? AND account_id=?',
                                  (workspace,issued_account)).fetchone()
                if not link:
                    raise RuntimeError('模拟发卡账户缺少银行连接')
                if application['requested_type']=='credit':
                    if application['card_id']:
                        old_link = db.execute('SELECT * FROM connected_card_links WHERE workspace_id=? AND card_id=?',
                                              (workspace,application['card_id'])).fetchone()
                        if not old_link or not old_link['facility_id'] or link['account_type']!='credit_card':
                            raise RuntimeError('模拟信用补卡缺少信用账户或授信关联')
                        connected_facility,connected_role = old_link['facility_id'],old_link['role']
                    else:
                        funding_account = issued_account
                        detail = db.execute('SELECT branch_id FROM account_details WHERE workspace_id=? AND account_id=?',
                                            (workspace,funding_account)).fetchone()
                        issued_account = _id(workspace,'credit-account',application['id'])
                        db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)',
                                   (issued_account,workspace,'新信用卡账务账户','CNY',0,0))
                        db.execute('INSERT INTO connected_account_links VALUES(?,?,?,?,?)',
                                   (workspace,issued_account,'runtime:credit:'+application['id'],link['connection_id'],'credit_card'))
                        self._insert_archive_row(db,'account_details',workspace,{'customer_id':application['customer_id'],
                            'account_id':issued_account,'branch_id':detail['branch_id'],'masked_number':'账户尾号 '+suffix,
                            'type':'current','status':'active','opened_on':day.isoformat(),'interest_bps':0,'overdraft_minor':0})
                        connected_facility = _id(workspace,'facility',application['id'])
                        connected_details['funding_account_id'] = funding_account
                elif link['account_type']=='credit_card':
                    raise RuntimeError('模拟借记卡不能挂信用账户')
                connected_details.update(source='navren_simulated_bank',origin='runtime_card_issuance',application_id=application['id'])
            db.execute('INSERT INTO cards(id,workspace_id,source_id,data_origin,details,customer_id,account_id,type,network,last_four,status,issued_on,expires_on,credit_limit_minor,outstanding_minor) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (card_id,workspace,'qingzhou','qingzhou_synthetic',encoded(connected_details),application['customer_id'],issued_account,
                        application['requested_type'],existing['network'] if existing else 'SIM-UnionPay',suffix,
                        'active',day.isoformat(),day.replace(year=day.year+5).isoformat(),
                        application['requested_limit_minor'] if application['requested_type']=='credit' else 0,0))
            if connected:
                db.execute('INSERT INTO connected_card_links VALUES(?,?,?,?,?)',
                           (workspace,card_id,'runtime:card:'+card_id,connected_facility,connected_role))
                if application['card_id']:
                    original_card = self._card(db,workspace,application['card_id'])
                    old_details = json.loads(original_card['details'])
                    old_details.update(lifecycle_state='replaced',replaced_by_card_id=card_id,replaced_on=day.isoformat())
                    db.execute("UPDATE cards SET status='expired',outstanding_minor=0,details=? WHERE workspace_id=? AND id=?",
                               (encoded(old_details),workspace,original_card['id']))
                    db.execute('UPDATE cards SET outstanding_minor=? WHERE workspace_id=? AND id=?',
                               (original_card['outstanding_minor'],workspace,card_id))
                    for statement in list(db.execute('SELECT id,details FROM credit_statements WHERE workspace_id=? AND card_id=?',
                                                     (workspace,original_card['id']))):
                        statement_details = json.loads(statement['details'])
                        statement_details.update(replaced_card_id=original_card['id'],replacement_card_id=card_id)
                        db.execute('UPDATE credit_statements SET card_id=?,details=? WHERE workspace_id=? AND id=?',
                                   (card_id,encoded(statement_details),workspace,statement['id']))
                    self._insert_archive_row(db,'card_events',workspace,{'card_id':original_card['id'],'event_type':'blocked',
                        'occurred_on':day.isoformat(),'status':'recorded','reason':'补卡核发，原卡已替换并停止使用',
                        'details':encoded({'replacement_card_id':card_id,'application_id':application['id']})})
            for kind,enabled,limit in (('online',1,0),('overseas',0,0),('contactless',1,0),('single_limit',1,min(200000,application['requested_limit_minor'] or 200000))):
                self._insert_archive_row(db,'card_controls',workspace,{'card_id':card_id,'control_type':kind,
                    'enabled':enabled,'limit_minor':limit,'updated_on':day.isoformat()})
            self._insert_archive_row(db,'card_events',workspace,{'card_id':card_id,'event_type':'issued',
                'occurred_on':day.isoformat(),'status':'recorded','reason':'演示规则自动审核并模拟发卡'})
            reason='模拟审核通过：KYC有效，申请符合本机演示规则；非真实发卡或授信。'
            db.execute("UPDATE card_applications SET status='issued',decision_reason=? WHERE workspace_id=? AND id=?",
                       (reason,workspace,application['id']))
            self._audit(db,workspace,'card_application_issued',{'application_id':application['id'],'card_id':card_id,'simulation_only':True})
            applications+=1
        for request in list(db.execute("SELECT r.*,c.account_id,c.credit_limit_minor FROM card_limit_requests r JOIN cards c ON c.workspace_id=r.workspace_id AND c.id=r.card_id WHERE r.workspace_id=? AND r.status='submitted' AND r.requested_on<? ORDER BY r.requested_on,r.id"+limit_sql,
                                       (workspace,day.isoformat()))):
            allowed=bool(kyc) and request['requested_limit_minor']<=cap and request['requested_limit_minor']>request['credit_limit_minor']
            if allowed:
                db.execute('UPDATE cards SET credit_limit_minor=? WHERE workspace_id=? AND id=?',
                           (request['requested_limit_minor'],workspace,request['card_id']))
                db.execute('UPDATE card_controls SET limit_minor=? WHERE workspace_id=? AND card_id=? AND control_type=\'single_limit\' AND limit_minor>?',
                           (request['requested_limit_minor'],workspace,request['card_id'],request['requested_limit_minor']))
                self._insert_archive_row(db,'card_events',workspace,{'card_id':request['card_id'],'event_type':'limit_updated',
                    'occurred_on':day.isoformat(),'status':'recorded','reason':'演示规则自动审核通过额度申请'})
                db.execute("UPDATE card_limit_requests SET status='approved',reason='模拟审核通过；非真实授信。' WHERE workspace_id=? AND id=?",
                           (workspace,request['id']))
                self._audit(db,workspace,'card_limit_approved',{'request_id':request['id'],'limit_minor':request['requested_limit_minor'],'simulation_only':True})
            else:
                db.execute("UPDATE card_limit_requests SET status='declined',reason='模拟审核未通过：身份材料或演示额度条件不满足。' WHERE workspace_id=? AND id=?",
                           (workspace,request['id']))
                self._audit(db,workspace,'card_limit_declined',{'request_id':request['id'],'simulation_only':True})
            limits+=1
        return applications,limits

    def _process_authorized_day(self, db, workspace, day, *, schedule_ids=None, limit=None):
        """调用方已持银行事务；只推进既有已授权任务，不生成新授权。"""
        settled=transfers=failed_transfers=applications=limits=expired_holds=0
        if limit is not None:integer(limit,'已授权队列批量',1,2000)
        limit_sql='' if limit is None else ' ORDER BY settlement_date,id LIMIT '+str(limit)
        orders=list(db.execute("SELECT * FROM orders WHERE workspace_id=? AND status='pending_settlement' AND settlement_date<=?"+limit_sql,
                               (workspace,day.isoformat())))
        for order in orders:
            amount=order['amount_minor']
            db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?',(amount,order['account_id']))
            self._ledger(db,workspace,order['account_id'],day,'模拟赎回结算',amount,amount,'investment_redemption',order['operation_id'])
            db.execute("UPDATE orders SET status='settled' WHERE id=?",(order['id'],))
            op=self._operation(db,workspace,order['operation_id'])
            result=json.loads(op['result'])
            result.update(status='settled',settled_on=day.isoformat(),balance_after_minor=self._account(db,workspace,order['account_id'])['balance_minor'])
            db.execute("UPDATE operations SET state='settled',result=? WHERE id=?",(encoded(result),op['id']))
            self._audit(db,workspace,'redemption_settled',{'order_id':order['id'],'amount_minor':amount},op['id'])
        settled+=len(orders)
        done,bad=self._process_scheduled_transfers(db,workspace,day,schedule_ids=schedule_ids)
        transfers+=done;failed_transfers+=bad
        reviewed_apps,reviewed_limits=self._process_card_reviews(db,workspace,day,limit=limit)
        applications+=reviewed_apps;limits+=reviewed_limits
        limit_sql='' if limit is None else ' ORDER BY expires_on,id LIMIT '+str(limit)
        expired=list(db.execute("SELECT * FROM fund_holds WHERE workspace_id=? AND status='held' AND expires_on<?"+limit_sql,
                                (workspace,day.isoformat())))
        for hold in expired:
            db.execute("UPDATE fund_holds SET status='expired' WHERE workspace_id=? AND id=?",(workspace,hold['id']))
            db.execute("UPDATE orchestration_tasks SET status='failed' WHERE workspace_id=? AND id=? AND status='in_progress'",
                       (workspace,hold['task_id']))
            db.execute("UPDATE task_steps SET status='failed' WHERE workspace_id=? AND task_id=? AND action='reserve_funds'",
                       (workspace,hold['task_id']))
            self._audit(db,workspace,'fund_hold_expired',{'hold_id':hold['id'],'amount_minor':hold['amount_minor']})
        expired_holds+=len(expired)
        return {'settled_orders':settled,'scheduled_transfers':transfers,'failed_transfers':failed_transfers,
                'reviewed_card_applications':applications,'reviewed_card_limits':limits,'expired_holds':expired_holds}

    def advance_days(self, workspace, days):
        """仅推进本机模拟日期，并处理已授权的模拟结算、定时转账及审核队列。"""
        integer(days,'推进天数',1,365)
        with self.transaction() as db:
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='registered_user_instances'").fetchone()
                    and db.execute('SELECT 1 FROM registered_user_instances WHERE workspace_id=?',(workspace,)).fetchone()):
                raise BankError('LIVING_CLOCK_MANAGED','注册实例由服务器时间推进，不能手动生成未来交易。',409)
            ws = self._workspace(db,workspace)
            start=date.fromisoformat(ws['as_of'])
            settled=transfers=failed_transfers=applications=limits=expired_holds=0
            for offset in range(1,days+1):
                day=start+timedelta(days=offset)
                db.execute('UPDATE workspaces SET as_of=? WHERE id=?',(day.isoformat(),workspace))
                outcome=self._process_authorized_day(db,workspace,day)
                settled+=outcome['settled_orders'];transfers+=outcome['scheduled_transfers']
                failed_transfers+=outcome['failed_transfers'];applications+=outcome['reviewed_card_applications']
                limits+=outcome['reviewed_card_limits'];expired_holds+=outcome['expired_holds']
            self._bump(db,workspace)
            final_day=start+timedelta(days=days)
            self._audit(db,workspace,'simulation_date_advanced',{'from':ws['as_of'],'to':final_day.isoformat(),
                'settled_orders':settled,'scheduled_transfers':transfers,'failed_transfers':failed_transfers,
                'reviewed_card_applications':applications,'reviewed_card_limits':limits,'expired_holds':expired_holds})
            return {'as_of':final_day.isoformat(),'settled_orders':settled,'scheduled_transfers':transfers,
                    'failed_transfers':failed_transfers,'reviewed_card_applications':applications,
                    'reviewed_card_limits':limits,'expired_holds':expired_holds,'environment':'simulation'}
