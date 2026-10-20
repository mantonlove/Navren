"""四画像的受控离线导入；历史原件和运行账本各自保留，不重放交易。"""
from __future__ import annotations

import calendar
from contextlib import closing
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid

from simulated_bank.service import BankError, digest, encoded
from simulated_bank.customer_archive import verify_invariants
from personal_data import personal_store


SOURCE = 'navren_synthetic'
USER_IDS = ('tycoon', 'grain_farmer', 'working_landlady', 'retired_entrepreneur')
NAMESPACE = uuid.UUID('baee3c7d-91d5-5b79-939b-26cc0e86481f')
REGISTRY = 'navren_demo_accounts'
IMPORT_VERSION = 'four-profiles-v1'
START_DATE, END_DATE = '2016-10-04', '2026-10-03'
HISTORY_TABLES = {
    'users': ('user_id', None),
    'ledger_accounts': ('account_id', None),
    'journal_events': ('occurred_at,event_id', 'substr(occurred_at,1,10)'),
    'journal_lines': ('line_id', 'substr(e.occurred_at,1,10)'),
    'cash_transactions': ('occurred_at,transaction_id', 'substr(occurred_at,1,10)'),
    'daily_balances': ('date', 'date'),
    'asset_values': ('date,asset_id', 'date'),
    'loan_balances': ('date,loan_id', 'date'),
    'monthly_summaries': ('period_start', 'summary'),
    'annual_summaries': ('period_start', 'summary'),
    'recurring_obligations': ('next_due_on,obligation_id', 'next_due_on'),
    'financial_targets': ('as_of,target_id', 'as_of'),
}


def _hash_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA foreign_keys=ON')
    return db


def _minor(value, name):
    # 离线存量可超过交易单笔上限，但必须是 SQLite 可保存的整数分。
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise BankError('DEMO_SOURCE_INVALID', f'{name}必须为非负整数分。', 422)
    return value


def _period(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}(?:-\d{2})?(?:-\d{2})?', value):
            raise ValueError
        parts = [int(p) for p in value.split('-')]
        if len(parts) == 1:
            return date(parts[0], 1, 1).isoformat(), date(parts[0], 12, 31).isoformat()
        if len(parts) == 2:
            return (date(*parts, 1).isoformat(),
                    date(*parts, calendar.monthrange(*parts)[1]).isoformat())
        day = date(*parts).isoformat()
        return day, day
    except (ValueError, TypeError):
        raise BankError('INVALID_PERIOD', '期间必须是有效的 YYYY、YYYY-MM 或 YYYY-MM-DD。', 400) from None


class DemoAccountService:
    def __init__(self, gateway, source_dir=None):
        self.gateway = gateway
        self.bank = gateway.bank
        self.source_dir = Path(source_dir or Path(__file__).parent / 'outputs/contest-finances').expanduser().resolve()
        self._verified_snapshots = {}

    @staticmethod
    def stable_workspace(user_id):
        if user_id not in USER_IDS:
            raise BankError('DEMO_ACCOUNT_NOT_FOUND', '没有这个演示画像。', 404)
        return uuid.uuid5(NAMESPACE, 'navren:contest:' + user_id).hex

    @staticmethod
    def _has_registry(db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (REGISTRY,)).fetchone())

    def _record(self, workspace, *, include_pending=False):
        with closing(self.bank.connect()) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='registered_demo_sources'").fetchone():
                private = db.execute('''SELECT s.*,i.registered_user_id FROM registered_demo_sources s
                    JOIN registered_user_instances i ON i.workspace_id=s.workspace_id
                    WHERE s.workspace_id=? AND i.status='ready' ''', (workspace,)).fetchone()
                if private is not None:
                    return dict(private)
            row = (db.execute('SELECT * FROM ' + REGISTRY + ' WHERE workspace_id=?', (workspace,)).fetchone()
                   if self._has_registry(db) else None)
        if row is None or (not include_pending and row['status'] != 'ready'):
            raise BankError('DEMO_ACCOUNT_NOT_FOUND', '这个演示账号尚未完成受控离线初始化。', 404)
        return dict(row)

    def catalog(self):
        provider = getattr(self.gateway, 'experience_provider', None)
        request = self.gateway.demo_sessions._request.get() if provider is not None else None
        if request is not None:
            owner = provider.owner_for_workspace(request[1])
            value = provider.catalog(owner)
            return {**value,'initialized_count':sum(a['status']=='ready' for a in value['accounts']),
                    'source':SOURCE,'is_real_bank_data':False}
        if provider is not None:
            # 内部无当前认证上下文时只报告模板规模，不列旧公共或他人实例。
            return {'environment':'simulation','source':SOURCE,'accounts':[],
                    'initialized_count':0,'template_count':len(USER_IDS),
                    'import_version':IMPORT_VERSION,'is_real_bank_data':False}
        with closing(self.bank.connect()) as db:
            rows = db.execute('SELECT * FROM ' + REGISTRY + " WHERE status='ready'").fetchall() if self._has_registry(db) else []
        indexed = {r['user_id']: r for r in rows}
        accounts = [{'user_id': uid, 'display_name': indexed[uid]['display_name'],
                     'workspace_id': indexed[uid]['workspace_id'], 'as_of': indexed[uid]['as_of'],
                     'source': SOURCE, 'source_version': indexed[uid]['source_version'],
                     'coverage_start': START_DATE, 'coverage_end': END_DATE}
                    for uid in USER_IDS if uid in indexed]
        for account in accounts:
            history = self.active_history(account['workspace_id'])
            if history:
                account.update(source_version=history.dataset_version,
                               coverage_start=history.metadata['start_date'],
                               coverage_end=history.metadata['end_date'])
        return {'environment': 'simulation', 'source': SOURCE, 'accounts': accounts,
                'initialized_count': len(accounts), 'import_version': IMPORT_VERSION,
                'is_real_bank_data': False}

    def workspace_for(self, user_id):
        workspace = self.stable_workspace(user_id)
        self._record(workspace)
        return workspace

    def subject_for(self, workspace):
        """只从已登记的服务端工作区取主体，不消费客户端subject_id。"""
        record = self._record(workspace)
        uid = record['user_id']
        expected = self.stable_workspace(uid)
        if record.get('registered_user_id'):
            from registered_instances import InstanceRegistry
            expected = InstanceRegistry.workspace_id(record['registered_user_id'],uid)
        if uid not in USER_IDS or record['workspace_id'] != workspace or expected != workspace:
            raise BankError('FINANCIAL_SUBJECT_MISMATCH', '工作区与财务主体映射不一致。', 409)
        return uid

    def is_demo(self, workspace):
        try:
            self._record(workspace)
            return True
        except BankError as error:
            if error.code == 'DEMO_ACCOUNT_NOT_FOUND':
                return False
            raise

    def _bundle(self):
        paths = [self.source_dir / name for name in
                 ('financial_data.sqlite3', 'contest_profiles.json', 'summary.json', 'data_contract.json', 'manifest.json')]
        paths += [self.source_dir / uid / 'current_finances.json' for uid in USER_IDS]
        if any(not p.is_file() for p in paths):
            raise BankError('DEMO_SOURCE_MISSING', '四画像来源文件不完整，未进行初始化。', 422)
        if Path(str(paths[0]) + '-wal').exists() and Path(str(paths[0]) + '-wal').stat().st_size:
            raise BankError('DEMO_SOURCE_BUSY', '来源数据库尚有未封存的 WAL，先完成来源封存。', 409)
        hashes = {str(p.relative_to(self.source_dir)): _hash_file(p) for p in paths}
        version = digest(encoded({'import_version': IMPORT_VERSION, 'files': hashes}))
        try:
            profiles = json.loads(paths[1].read_text(encoding='utf-8'))
            currents = {uid: json.loads((self.source_dir / uid / 'current_finances.json').read_text(encoding='utf-8')) for uid in USER_IDS}
        except (ValueError, OSError):
            raise BankError('DEMO_SOURCE_INVALID', '来源 JSON 无法读取。', 422) from None
        if (not isinstance(profiles, dict) or not isinstance(profiles.get('profiles'), list) or
                any(not isinstance(p, dict) or not isinstance(p.get('id'), str) for p in profiles['profiles'])):
            raise BankError('DEMO_SOURCE_INVALID', '画像配置结构无效。', 422)
        return {'database': paths[0], 'hashes': hashes, 'version': version,
                'profiles': profiles, 'currents': currents}

    def _validate_bundle(self, bundle):
        from scripts.audit_base_dataset import audit_dataset
        audit = audit_dataset(self.source_dir)
        if not audit['passed']:
            failed = [c['id'] for c in audit['checks'] if not c['passed']]
            raise BankError('DEMO_SOURCE_AUDIT_FAILED', '来源独立审计失败：' + '、'.join(failed), 422)
        profiles = bundle['profiles']
        profile_map = {p['id']: p for p in profiles['profiles']}
        if set(profile_map) != set(USER_IDS) or len(profiles['profiles']) != 4:
            raise BankError('DEMO_SOURCE_INVALID', '来源必须恰好包含约定的四个画像。', 422)
        with closing(_readonly(bundle['database'])) as db:
            metadata = dict(db.execute('SELECT key,value FROM metadata'))
            if (metadata.get('source') != SOURCE or metadata.get('start_date') != START_DATE or
                    metadata.get('end_date') != END_DATE or
                    metadata.get('config_sha256') != bundle['hashes']['contest_profiles.json']):
                raise BankError('DEMO_SOURCE_INVALID', '来源标识、时间范围或画像配置哈希不一致。', 422)
            for uid in USER_IDS:
                current, profile = bundle['currents'][uid], profile_map[uid]
                user = dict(db.execute('SELECT * FROM users WHERE user_id=?', (uid,)).fetchone())
                if any(current.get(key) != value for key, value in user.items()):
                    raise BankError('DEMO_SOURCE_INVALID', '期末画像与原始用户表不一致。', 422)
                if profile.get('source') != SOURCE or any(profile.get(k) != user[k] for k in ('display_name', 'risk_level', 'horizon_days', 'reserve_minor')):
                    raise BankError('DEMO_SOURCE_INVALID', '画像规则配置与原始用户表不一致。', 422)
                latest = dict(db.execute('SELECT * FROM daily_balances WHERE user_id=? AND date=?', (uid, END_DATE)).fetchone())
                if latest != current['latest_balances']:
                    raise BankError('DEMO_SOURCE_INVALID', '期末汇总与原始日余额不一致。', 422)
                source_accounts = [dict(r) for r in db.execute('SELECT * FROM ledger_accounts WHERE user_id=?', (uid,))]
                imported = current['accounts'] + current['assets'] + current['loans']
                by_id = {r['account_id']: r for r in imported}
                expected = [r for r in source_accounts if r['account_class'] in ('cash', 'investment', 'real_asset', 'liability')]
                if len(imported) != len(by_id) or set(by_id) != {r['account_id'] for r in expected}:
                    raise BankError('DEMO_SOURCE_INVALID', '期末资产账户缺失、重复或多出。', 422)
                for account in expected:
                    item = by_id[account['account_id']]
                    if any(item.get(k) != v for k, v in account.items()):
                        raise BankError('DEMO_SOURCE_INVALID', '期末账户关联与来源不一致。', 422)
                    balance = db.execute('SELECT coalesce(sum(debit_minor-credit_minor),0) FROM journal_lines WHERE user_id=? AND account_id=?', (uid, account['account_id'])).fetchone()[0]
                    if item['balance_minor'] != balance:
                        raise BankError('DEMO_SOURCE_INVALID', '期末账户余额与分录不一致。', 422)
                    if account['account_class'] in ('investment', 'real_asset'):
                        valuation = db.execute('SELECT * FROM asset_values WHERE user_id=? AND asset_id=? AND date=?', (uid, account['account_id'], END_DATE)).fetchone()
                        if any(item.get(k) != valuation[k] for k in ('book_value_minor', 'cost_basis_minor', 'unrealized_pnl_minor')):
                            raise BankError('DEMO_SOURCE_INVALID', '期末资产估值与来源不一致。', 422)
                    elif account['account_class'] == 'liability':
                        principal = db.execute('SELECT principal_minor FROM loan_balances WHERE user_id=? AND loan_id=? AND date=?', (uid, account['account_id'], END_DATE)).fetchone()[0]
                        if item.get('outstanding_principal_minor') != principal:
                            raise BankError('DEMO_SOURCE_INVALID', '期末贷款本金与来源不一致。', 422)
                for table in ('recurring_obligations', 'financial_targets'):
                    actual = [dict(r) for r in db.execute('SELECT * FROM ' + table + ' WHERE user_id=?', (uid,))]
                    if sorted(actual, key=encoded) != sorted(current[table], key=encoded):
                        raise BankError('DEMO_SOURCE_INVALID', '预测或目标与来源不一致。', 422)
                    if table == 'recurring_obligations' and any(r['status'] != 'forecast_not_paid' or r['source'] != SOURCE for r in actual):
                        raise BankError('DEMO_SOURCE_INVALID', '未记账预测必须保留候选来源。', 422)
                for item in current['accounts']:
                    _minor(item['balance_minor'], '期末现金')
                for item in current['assets']:
                    _minor(item['cost_basis_minor'], '期末资产成本')
                    _minor(item['book_value_minor'], '期末资产账面值')
                for item in current['loans']:
                    _minor(item['outstanding_principal_minor'], '期末贷款本金')
        bundle['metadata'] = metadata
        bundle['profile_map'] = profile_map
        bundle['audit'] = {'passed': True, 'audit_version': 'independent-base-dataset-v1',
                           'row_counts': audit['counts'], 'check_count': len(audit['checks']),
                           'uncovered_categories': [c['id'] for c in audit['uncovered']]}

    def _copy_history(self, bundle):
        target = self.bank.database.with_name(self.bank.database.stem + '.demo-source-' + bundle['version'] + '.sqlite3')
        if target.exists():
            # 仅允许注册过的副本复用；遗留同名文件不能冒充成功导入。
            with closing(self.bank.connect()) as db:
                row = db.execute('SELECT history_sha256 FROM ' + REGISTRY + ' WHERE history_path=? LIMIT 1', (str(target),)).fetchone() if self._has_registry(db) else None
            if row is None or _hash_file(target) != row['history_sha256']:
                raise BankError('DEMO_HISTORY_CONFLICT', '历史副本已存在但未登记或校验失败。', 409)
            return target, row['history_sha256'], False
        temporary = target.with_name(target.name + '.part-' + uuid.uuid4().hex)
        try:
            with closing(_readonly(bundle['database'])) as src, closing(sqlite3.connect(temporary)) as dest:
                src.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or dest.execute('PRAGMA foreign_key_check').fetchone():
                    raise BankError('DEMO_HISTORY_INVALID', '历史副本完整性校验失败。', 422)
            if _hash_file(bundle['database']) != bundle['hashes']['financial_data.sqlite3']:
                raise BankError('DEMO_SOURCE_CHANGED', '复制期间来源发生变化，导入已停止。', 409)
            if any(_hash_file(self.source_dir / name) != expected for name, expected in bundle['hashes'].items()):
                raise BankError('DEMO_SOURCE_CHANGED', '导入期间来源文件发生变化，导入已停止。', 409)
            temporary.chmod(0o400)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            # 不覆盖已有文件；并发初始化由 SQLite 注册事务决定最终归属。
            os.link(temporary, target)
            temporary.unlink()
            return target, _hash_file(target), True
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _cash_mapping(workspace, current):
        mapping = {}
        for item in current['accounts']:
            suffix = {'cash:daily': 'main', 'cash:operating': 'operating', 'cash:reserve': 'reserve'}.get(item['code'])
            if suffix is None:
                raise BankError('DEMO_SOURCE_INVALID', '来源现金账户类型未约定。', 422)
            mapping[item['account_id']] = 'acct-' + workspace + '-' + suffix
        return mapping

    def _insert_entity(self, db, table, workspace, key, fields, *, details=None):
        row = {'id': 'demo-' + workspace + '-' + key, 'workspace_id': workspace,
               'source_id': 'qingzhou', 'data_origin': 'qingzhou_synthetic',
               'details': encoded({'source': SOURCE, 'import_version': IMPORT_VERSION, **(details or {})}), **fields}
        db.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        return row['id']

    def _seed_workspace(self, db, uid, bundle, history_path, history_hash, *, workspace=None, registry_table=REGISTRY):
        if registry_table not in (REGISTRY,'registered_demo_sources'):
            raise ValueError('unsupported_template_registry')
        workspace = workspace or self.stable_workspace(uid)
        current, config = bundle['currents'][uid], bundle['profile_map'][uid]
        mapping = self._cash_mapping(workspace, current)
        main = mapping[uid + '::cash:daily']
        # 初始化不发出凭据； HTTP 选择器另行签发受确认权限约束的会话。
        db.execute('INSERT INTO workspaces(id,token_hash,agent_token_hash,pin_hash,as_of,version,created_at) VALUES(?,?,?,?,?,1,?)',
                   (workspace, digest(secrets.token_urlsafe(32)), digest(secrets.token_urlsafe(32)),
                    digest(workspace + ':' + secrets.token_urlsafe(32)), END_DATE, self.bank.clock()))
        db.execute('INSERT INTO profiles VALUES(?,?,?,?)', (workspace, current['risk_level'], current['horizon_days'], current['reserve_minor']))
        for item in current['accounts']:
            cash = item['balance_minor']
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)', (mapping[item['account_id']], workspace, item['name'], 'CNY', cash, cash))
        branch = self._insert_entity(db, 'bank_branches', workspace, 'branch',
                                    {'name': 'Navren 合成演示机构', 'bank_name': 'Navren 合成银行', 'bank_code': 'SIMULATED', 'city': '未提供', 'channel': 'simulation'})
        customer = self._insert_entity(db, 'customers', workspace, 'customer',
                                      {'branch_id': branch, 'display_name': current['display_name'], 'birth_date': None,
                                       'nationality': '未提供', 'status': 'active', 'opened_on': END_DATE, 'segment': 'synthetic_demo'},
                                      details={'opened_on_semantics': '运行账本初始化日期；不代表历史开户', 'identity_not_provided': True})
        for item in current['accounts']:
            account = mapping[item['account_id']]
            self._insert_entity(db, 'account_details', workspace, item['code'].replace(':', '-') + '-details',
                                {'customer_id': customer, 'account_id': account, 'branch_id': branch,
                                 'masked_number': 'SIM-' + item['code'].split(':')[1], 'type': 'current', 'status': 'active',
                                 'opened_on': END_DATE, 'interest_bps': 0, 'overdraft_minor': 0},
                                details={'source_account_id': item['account_id'], 'scope': item['scope'],
                                         'opened_on_semantics': '演示运行账户创建日期', 'interest_bps_semantics': '运行演示无计息，不代表来源利率'})
            for kind, amount in (('transfer_daily', 5000000), ('transfer_single', 2000000), ('cash_daily', 1000000)):
                self._insert_entity(db, 'account_limits', workspace, item['code'].replace(':', '-') + '-' + kind,
                                    {'account_id': account, 'kind': kind, 'limit_minor': amount, 'used_minor': 0, 'effective_on': END_DATE},
                                    details={'configuration_source': SOURCE, 'limit_scope': 'simulation_rule'})
        for index in (1, 2):
            self._insert_entity(db, 'beneficiaries', workspace, 'beneficiary-' + str(index),
                                {'display_name': '演示收款账户 ' + str(index), 'alias': '合成转账测试',
                                 'masked_mobile': '未提供', 'masked_account': 'SIM-RECIPIENT-' + str(index),
                                 'bank_name': 'Navren 合成银行', 'currency': 'CNY', 'verified': 1, 'status': 'active'},
                                details={'verification_scope': 'synthetic_rule_fixture', 'not_real_identity_verification': True})
        self._insert_entity(db, 'risk_assessments', workspace, 'risk-config',
                            {'customer_id': customer, 'assessed_on': END_DATE, 'expires_on': '2027-10-03',
                             'risk_level': current['risk_level'], 'horizon_days': current['horizon_days'],
                             'reserve_minor': current['reserve_minor'], 'status': 'current', 'answers_json': '{}'},
                            details={'configuration_source': SOURCE, 'not_real_risk_assessment': True,
                                     'expiry_scope': 'simulation_rule'})
        for item in current['financial_targets']:
            self._insert_entity(db, 'wealth_goals', workspace, item['target_id'],
                                {'customer_id': customer, 'name': item['category'], 'target_minor': item['target_minor'],
                                 'saved_minor': item['allocated_minor'], 'due_on': None, 'priority': 1,
                                 'status': 'completed' if item['allocated_minor'] >= item['target_minor'] else 'active'},
                                details={'source_target_id': item['target_id'], 'as_of': item['as_of'], 'due_on_status': 'unknown'})
        loan_config = {p['id']: p for p in config['loans']}
        for item in current['loans']:
            original = loan_config[item['code'].split(':', 1)[1]]
            self._insert_entity(db, 'loans', workspace, item['account_id'],
                                {'customer_id': customer, 'repayment_account_id': main, 'loan_type': item['name'],
                                 'original_principal_minor': None, 'outstanding_minor': item['outstanding_principal_minor'],
                                 'annual_rate_bps': original['annual_rate_bps'], 'started_on': None, 'matures_on': None,
                                 'status': 'active' if item['outstanding_principal_minor'] else 'closed', 'overdue_minor': 0},
                                details={'source_loan_id': item['account_id'], 'repayment_schedule_status': 'unknown',
                                         'overdue_minor_semantics': '运行演示无逾期事件，原始逾期状态未提供',
                                         'repayment_account_semantics': '演示规则路由；非原始合同',
                                         'source_opening_principal_minor': original['opening_principal_minor'],
                                         'source_monthly_principal_minor': original['monthly_principal_minor'],
                                         'source_scope': item['scope']})
        for item in current['assets']:
            if item['account_class'] != 'investment':
                continue
            product_id = ('DEMO-' + uid + '-' + item['code'].split(':', 1)[1] if registry_table == REGISTRY
                          else 'INSTANCE-' + workspace + '-' + item['code'].split(':', 1)[1])
            product = {'id': product_id, 'name': item['name'], 'source': SOURCE, 'workspace_id': workspace,
                       'source_user_id': uid, 'source_asset_id': item['account_id'],
                       'historical_synthetic_position': True, 'execution_enabled': False,
                       'risk_level': None, 'term_days': None, 'min_purchase_minor': None,
                       'redemption_delay_days': None, 'annual_yield_bps': None,
                       'quantity': None, 'valuation_mode': 'synthetic_book_value',
                       'locked_until_unknown': True, 'not_contractual_lock_date': True,
                       'notice': '合成历史头寸，条款和份额未知，不能申购或赎回。'}
            db.execute('INSERT INTO products VALUES(?,?)', (product_id, encoded(product)))
            principal = item['cost_basis_minor']
            db.execute('INSERT INTO holdings VALUES(?,?,?,?,?)', (workspace, main, product_id, principal, '9999-12-31'))
            if principal:
                db.execute('INSERT INTO investment_lots VALUES(?,?,?,?,?,?,?,?,?,?)',
                           ('demo-lot-' + workspace + '-' + product_id, workspace, main, product_id, None, None,
                            '9999-12-31', principal, principal, 'fixture'))
        db.execute('INSERT INTO workspace_datasets VALUES(?,?,?,?,?)',
                   (workspace, IMPORT_VERSION, bundle['version'], self.bank.seed_hash, self.bank.clock()))
        stored = {**current, 'source_metadata': bundle['metadata'], 'source_audit': bundle['audit']}
        db.execute('INSERT INTO ' + registry_table + ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (uid, workspace, current['display_name'], END_DATE, bundle['version'], encoded(bundle['hashes']),
                    str(history_path), history_hash, encoded(stored), encoded(mapping), 'pending',
                    self.bank.clock(), IMPORT_VERSION))
        self.bank._audit(db, workspace, 'demo_offline_imported', {'source': SOURCE, 'source_version': bundle['version'],
                                                               'history_replayed': False, 'user_id': uid})
        invariants = verify_invariants(db, workspace)
        if not invariants['ok']:
            raise BankError('DEMO_IMPORT_INVARIANT_FAILED', '导入账本校验失败：' + '、'.join(invariants['problems']), 422)

    def initialize_offline(self, *, allow_offline=False):
        if allow_offline is not True:
            raise BankError('DEMO_OFFLINE_PERMISSION_REQUIRED', '四画像初始化只允许显式开启的受控离线操作。', 403)
        bundle = self._bundle()
        with closing(self.bank.connect()) as db:
            rows = db.execute('SELECT * FROM ' + REGISTRY).fetchall() if self._has_registry(db) else []
            if rows and (len(rows) != 4 or {r['user_id'] for r in rows} != set(USER_IDS) or
                         any(r['source_version'] != bundle['version'] or r['import_version'] != IMPORT_VERSION for r in rows)):
                raise BankError('DEMO_SOURCE_VERSION_CONFLICT', '已有导入与新来源版本不同，必须显式迁移，不能重置运行账本。', 409)
            if not rows and any(db.execute('SELECT 1 FROM workspaces WHERE id=?', (self.stable_workspace(uid),)).fetchone() for uid in USER_IDS):
                raise BankError('DEMO_WORKSPACE_CONFLICT', '固定工作区 ID 已存在但未登记，不能覆盖。', 409)
        if not rows:
            self._validate_bundle(bundle)
            history_path, history_hash, created = self._copy_history(bundle)
            try:
                with self.bank.transaction() as db:
                    db.execute('''CREATE TABLE IF NOT EXISTS navren_demo_accounts(
                        user_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(id),
                        display_name TEXT NOT NULL,as_of TEXT NOT NULL,source_version TEXT NOT NULL,
                        source_hashes TEXT NOT NULL,history_path TEXT NOT NULL,history_sha256 TEXT NOT NULL,
                        source_snapshot TEXT NOT NULL,account_mapping TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','ready')),imported_at REAL NOT NULL,
                        import_version TEXT NOT NULL)''')
                    for uid in USER_IDS:
                        self._seed_workspace(db, uid, bundle, history_path, history_hash)
                    if db.execute('PRAGMA foreign_key_check').fetchone():
                        raise BankError('DEMO_IMPORT_INVARIANT_FAILED', '导入关联完整性校验失败。', 422)
            except Exception:
                if created:
                    history_path.unlink(missing_ok=True)
                raise
        else:
            for row in rows:
                self._history_connection(dict(row)).close()
        # 两个数据库的提交无法伪装成一个事务：bank 先 pending，私库成功后才 ready。
        # 私库故障时选择器不可见；重试仅补全设置，不再次写余额或覆盖已有个人记忆。
        store = getattr(getattr(self.gateway, 'memories', None), 'store', None) or personal_store(self.bank)
        for uid in USER_IDS:
            workspace = self.stable_workspace(uid)
            with store.transaction(workspace, change=False) as private_db:
                private_db.execute('INSERT OR IGNORE INTO user_memory_settings VALUES(?,0,?)', (workspace, self.bank.clock()))
        with self.bank.transaction() as db:
            db.execute('UPDATE ' + REGISTRY + " SET status='ready' WHERE source_version=?", (bundle['version'],))
        return {**self.catalog(), 'source_version': bundle['version'], 'already_initialized': bool(rows),
                'history_replayed': False}

    def _history_connection(self, record):
        path = Path(record['history_path'])
        if not path.is_absolute():path=self.bank.database.parent/path
        if not path.is_file():
            raise BankError('DEMO_HISTORY_MISSING', '导入历史副本丢失，不能用其他来源代替。', 503)
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino, record['history_sha256'])
        if self._verified_snapshots.get(str(path)) != signature:
            if _hash_file(path) != record['history_sha256']:
                raise BankError('DEMO_HISTORY_INVALID', '导入历史副本校验失败，已停止读取。', 503)
            self._verified_snapshots[str(path)] = signature
        return _readonly(path)

    def history(self, workspace, table, period=None, limit=100, offset=0):
        if table not in HISTORY_TABLES:
            raise BankError('INVALID_HISTORY_TABLE', '没有允许读取的历史表。', 400)
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise BankError('INVALID_PAGINATION', '分页条数为 1 至 500，偏移必须为非负整数。', 400)
        bounds = _period(period)
        record = self._record(workspace)
        order, dated = HISTORY_TABLES[table]
        select = 't.*' if table == 'journal_lines' else '*'
        from_clause = table + ' t JOIN journal_events e ON e.user_id=t.user_id AND e.event_id=t.event_id' if table == 'journal_lines' else table
        where = ['t.user_id=?' if table == 'journal_lines' else 'user_id=?']
        values = [record['user_id']]
        if bounds and dated:
            where.append('period_end>=? AND period_start<=?' if dated == 'summary' else dated + ' BETWEEN ? AND ?')
            values += list(bounds)
        base = ' FROM ' + from_clause + ' WHERE ' + ' AND '.join(where)
        with closing(self._history_connection(record)) as db:
            total = db.execute('SELECT count(*)' + base, values).fetchone()[0]
            rows = [dict(r) for r in db.execute('SELECT ' + select + base + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', [*values, limit, offset])]
        return {'environment': 'simulation', 'source': SOURCE, 'source_version': record['source_version'],
                'workspace_id': workspace, 'user_id': record['user_id'], 'table': table, 'period': period,
                'rows': rows, 'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + len(rows) < total, 'coverage_start': START_DATE, 'coverage_end': END_DATE,
                'coverage_partial_period': bool(bounds and (bounds[0] < START_DATE or bounds[1] > END_DATE)),
                'forecast_not_paid': table == 'recurring_obligations'}

    def map_account_id(self, workspace, source_id):
        mapping = json.loads(self._record(workspace)['account_mapping'])
        if source_id not in mapping:
            raise BankError('DEMO_ACCOUNT_MAPPING_NOT_FOUND', '这个来源账户没有对应的运行现金账户。', 404)
        return mapping[source_id]

    def current(self, workspace):
        record = self._record(workspace)
        snapshot = json.loads(record['source_snapshot'])
        mapping = json.loads(record['account_mapping'])
        inverse = {value: key for key, value in mapping.items()}
        with closing(self.bank.connect()) as db:
            ws = self.bank._workspace(db, workspace)
            accounts = [{**dict(r), 'runtime_account_id': r['id'], 'source_account_id': inverse.get(r['id'])}
                        for r in db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY id', (workspace,))]
            holdings = []
            for r in db.execute('SELECT h.*,p.data FROM holdings h JOIN products p ON p.id=h.product_id WHERE h.workspace_id=?', (workspace,)):
                row = dict(r)
                product = json.loads(row.pop('data'))
                row.update(source_asset_id=product.get('source_asset_id'), quantity=None,
                           source=product.get('source', 'qingzhou_synthetic'),
                           historical_synthetic_position=bool(product.get('historical_synthetic_position')),
                           execution_enabled=product.get('execution_enabled', True))
                if row['historical_synthetic_position']:
                    row['technical_locked_until'] = row.pop('locked_until')
                    row['locked_until'] = None
                holdings.append(row)
            loans = [dict(r, details=json.loads(r['details'])) for r in db.execute('SELECT * FROM loans WHERE workspace_id=? ORDER BY id', (workspace,))]
            bank_state = {'as_of': ws['as_of'], 'version': ws['version'], 'accounts': accounts,
                          'account_mapping': mapping, 'holdings': holdings, 'loans': loans,
                          'cash_minor': sum(a['balance_minor'] for a in accounts),
                          'holding_principal_minor': sum(a['amount_minor'] for a in holdings),
                          'liabilities_minor': sum(a['outstanding_minor'] for a in loans),
                          'valuation_scope': '运行本金账本；不包含历史估值涨跌与非金融资产'}
        history = self.active_history(workspace)
        history_fields = ({'start_date': history.metadata['start_date'],
                           'end_date': history.metadata['end_date']} if history else {})
        return {**snapshot, **history_fields, 'environment': 'simulation', 'workspace_id': workspace, 'source': SOURCE,
                'source_version': history.dataset_version if history else record['source_version'], 'import_version': IMPORT_VERSION,
                'as_of': record['as_of'], 'source_snapshot': snapshot, 'bank_state': bank_state,
                'forecasts': snapshot['recurring_obligations'], 'is_real_bank_data': False,
                'quantity_status': 'unknown', 'notice': '历史估值按期末记录展示；运行本金状态单独列示。',
                'independent_financial_data': self.independent_reading_status()}

    def active_history(self, workspace):
        """仅明确采用的三年库接入原查账链路，绑定失配时停止读取。"""
        provider=getattr(self.gateway,'experience_provider',None)
        record=self._record(workspace) if provider else None
        service=(provider.materializer.service_for(workspace) if provider and record.get('registered_user_id')
                 else getattr(self.gateway, 'financial_data', None))
        if service is None or service.metadata.get('history_profile') != 'realistic_three_year':
            return None
        subject = self.subject_for(workspace)
        with closing(self.bank.connect()) as db:
            binding = db.execute('SELECT subject_id,dataset_version,source_path FROM connected_asset_migrations '
                                 'WHERE workspace_id=?', (workspace,)).fetchone()
        if (binding is None or binding['subject_id'] != subject
                or binding['dataset_version'] != service.dataset_version
                or Path(binding['source_path']).resolve() != service.database_path):
            raise BankError('CONNECTED_HISTORY_UNAVAILABLE', '账户历史绑定与当前读取版本不一致。', 503)
        return service

    def independent_reading_status(self):
        service = getattr(self.gateway, 'financial_data', None)
        return {'configured': service is not None,
                'schema_version': 'navren.financial.v1',
                'dataset_version': service.dataset_version if service is not None else None,
                'endpoint': '/api/finance/data/', 'runtime_operations_included': False,
                'legacy_runtime_state_replaced': False,
                'notice': '独立财务事实源通过专用只读入口读取；现行账本、记忆、会话及执行回执保留，不重复汇总。'}
