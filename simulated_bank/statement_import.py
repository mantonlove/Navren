"""隔离存储本机导入的CSV流水；不向模拟银行余额或订单账本记账。"""
import csv
import hashlib
import io
import json
import re
import secrets
import threading
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from .service import BankError, SimulatedBank, add_month, digest, encoded, strict_fields


MAX_BYTES = 1_572_864
MAX_ROWS = 5_000
PREVIEW_TTL = 600
REQUIRED_HEADERS = ('流水号', '日期', '商户', '金额', '类型')
OPTIONAL_HEADERS = ('分类', '币种', '备注')
KINDS = {'收入': 'income', '支出': 'expense', '退款': 'refund', '转账': 'transfer',
         'income': 'income', 'expense': 'expense', 'refund': 'refund', 'transfer': 'transfer'}
KIND_LABELS = {'income': '收入', 'expense': '支出', 'refund': '退款', 'transfer': '转账'}
BEIJING = timezone(timedelta(hours=8))


class StatementImportService:
    def __init__(self, bank: SimulatedBank, *, clock=time.time):
        self.bank, self.clock = bank, clock
        self.on_source_activated = None  # trusted host write-lifecycle callback
        self._previews = {}
        self._lock = threading.RLock()
        with closing(self.bank.connect()) as db, db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS statement_datasets(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(id),
 source_name TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency='CNY'),
 version INTEGER NOT NULL, transaction_count INTEGER NOT NULL,
 first_date TEXT, last_date TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS imported_statement_transactions(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), dataset_id TEXT NOT NULL REFERENCES statement_datasets(id),
 transaction_id TEXT NOT NULL, occurred_at TEXT NOT NULL, time_precision TEXT NOT NULL CHECK(time_precision IN ('date','datetime')),
 merchant TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 kind TEXT NOT NULL CHECK(kind IN ('income','expense','refund','transfer')),
 category TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency='CNY'), note TEXT NOT NULL,
 content_hash TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,transaction_id));
CREATE INDEX IF NOT EXISTS imported_tx_month ON imported_statement_transactions(workspace_id,occurred_at);
CREATE TABLE IF NOT EXISTS statement_import_batches(
 batch_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 dataset_id TEXT NOT NULL REFERENCES statement_datasets(id), content_hash TEXT NOT NULL,
 input_count INTEGER NOT NULL, inserted_count INTEGER NOT NULL, skipped_count INTEGER NOT NULL,
 result TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(workspace_id,batch_id));
CREATE TABLE IF NOT EXISTS finance_data_sources(
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
 source TEXT NOT NULL CHECK(source IN ('simulation','imported')), updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS imported_periodic_marks(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), candidate_id TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('confirmed','ignored')), details TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(workspace_id,candidate_id));
''')

    def _workspace(self, workspace):
        with closing(self.bank.connect()) as db:
            self.bank._workspace(db, workspace)

    @staticmethod
    def _category(kind, merchant, supplied):
        if supplied:
            return supplied
        if kind == 'income':
            return '收入'
        if kind == 'refund':
            return '退款'
        if kind == 'transfer':
            return '转账'
        for needle, category in [('房租', '住房'), ('水电', '生活缴费'), ('电费', '生活缴费'),
                                 ('宽带', '生活缴费'), ('地铁', '交通'), ('公交', '交通'),
                                 ('超市', '购物'), ('餐', '餐饮'), ('咖啡', '餐饮'),
                                 ('会员', '订阅'), ('视频', '订阅'), ('理发', '生活服务')]:
            if needle in merchant:
                return category
        return '其他'

    @staticmethod
    def _safe_text(value, label, maximum, *, required=False):
        value = (value or '').strip()
        if required and not value:
            raise ValueError(f'{label}不能为空')
        if len(value) > maximum:
            raise ValueError(f'{label}不能超过{maximum}个字符')
        if any(ord(char) < 32 and char not in '\t' for char in value):
            raise ValueError(f'{label}包含控制字符')
        return value

    @staticmethod
    def _date(value):
        value = value.strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            parsed = date.fromisoformat(value)
            if not 1970 <= parsed.year <= 2100:
                raise ValueError('日期需在1970年至2100年之间')
            return parsed.isoformat(), 'date'
        try:
            parsed_dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('日期请用YYYY-MM-DD或含北京时间偏移的ISO格式')
        if parsed_dt.tzinfo is None or parsed_dt.utcoffset() != timedelta(hours=8):
            raise ValueError('精确时间必须明确带北京时间+08:00偏移')
        if not 1970 <= parsed_dt.year <= 2100:
            raise ValueError('日期需在1970年至2100年之间')
        return parsed_dt.isoformat(timespec='seconds'), 'datetime'

    @staticmethod
    def _amount(value):
        if not re.fullmatch(r'(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?', value.strip()):
            raise ValueError('金额须为正数，最多两位小数')
        try:
            minor = Decimal(value.strip()) * 100
        except InvalidOperation:
            raise ValueError('金额格式无效')
        if minor != minor.to_integral_value() or not 0 < minor <= 100_000_000:
            raise ValueError('金额须大于0且不超过1000000元')
        return int(minor)

    def _parse(self, workspace, text):
        if not isinstance(text, str):
            raise BankError('CSV_REQUIRED', '请上传UTF-8编码的CSV账单。', 415)
        raw = text.encode('utf-8')
        if not raw or len(raw) > MAX_BYTES:
            raise BankError('CSV_SIZE', 'CSV不能为空，且不能超过1.5MB。', 413)
        if b'\x00' in raw:
            raise BankError('CSV_ENCODING', 'CSV不能包含空字符。')
        file_hash = hashlib.sha256(raw).hexdigest()
        errors, rows, seen = [], [], {}
        record_count = in_file_duplicates = 0
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
            headers = reader.fieldnames
            if not headers:
                raise BankError('CSV_HEADER', 'CSV缺少表头。')
            headers = [h.strip() if h else '' for h in headers]
            if len(headers) != len(set(headers)):
                raise BankError('CSV_HEADER', 'CSV表头不能重复。')
            missing = [h for h in REQUIRED_HEADERS if h not in headers]
            unknown = [h for h in headers if h not in REQUIRED_HEADERS + OPTIONAL_HEADERS]
            if missing or unknown:
                details = []
                if missing: details.append('缺少必填列：'+'、'.join(missing))
                if unknown: details.append('不支持的列：'+'、'.join(unknown))
                raise BankError('CSV_HEADER', '；'.join(details)+'。')
            reader.fieldnames = headers
            for line, raw_row in enumerate(reader, start=2):
                record_count += 1
                if line > MAX_ROWS + 1:
                    errors.append({'row': line, 'message': f'最多导入{MAX_ROWS}条流水'})
                    break
                if None in raw_row or any(value is None for value in raw_row.values()):
                    errors.append({'row': line, 'message': '列数与表头不一致'})
                    continue
                try:
                    txid = self._safe_text(raw_row.get('流水号'), '流水号', 128, required=True)
                    if len(txid) > 128:
                        raise ValueError('流水号不能超过128个字符')
                    if any(ord(ch) < 32 for ch in txid):
                        raise ValueError('流水号包含控制字符')
                    occurred_at, precision = self._date(raw_row.get('日期', ''))
                    merchant = self._safe_text(raw_row.get('商户'), '商户', 120, required=True)
                    amount = self._amount(raw_row.get('金额', ''))
                    kind = KINDS.get((raw_row.get('类型') or '').strip())
                    if not kind:
                        raise ValueError('类型须为收入、支出、退款或转账')
                    currency = (raw_row.get('币种') or 'CNY').strip().upper() or 'CNY'
                    if currency != 'CNY':
                        raise ValueError('首版只支持人民币CNY；未做汇率换算')
                    category = self._safe_text(raw_row.get('分类'), '分类', 64) or self._category(kind, merchant, '')
                    note = self._safe_text(raw_row.get('备注'), '备注', 500)
                    normalized = {'transaction_id': txid, 'occurred_at': occurred_at, 'time_precision': precision,
                                  'merchant': merchant, 'amount_minor': amount, 'kind': kind,
                                  'category': category, 'currency': currency, 'note': note}
                    normalized['content_hash'] = digest(encoded({k:v for k,v in normalized.items() if k != 'content_hash'}))
                    prior = seen.get(txid)
                    if prior:
                        if prior['content_hash'] != normalized['content_hash']:
                            raise ValueError('同一文件中的流水号对应不同内容，已阻止整批导入')
                        in_file_duplicates += 1
                        continue
                    seen[txid] = normalized
                    rows.append(normalized)
                except ValueError as error:
                    errors.append({'row': line, 'message': str(error)})
                    if len(errors) >= 200:
                        errors.append({'row': line, 'message': '错误过多，仅显示前200条'})
                        break
        except csv.Error:
            raise BankError('CSV_PARSE', 'CSV引号或分隔符格式有误。')
        if record_count == 0:
            errors.append({'row': 2, 'message': 'CSV至少需要一笔流水记录。'})
        with closing(self.bank.connect()) as db:
            self.bank._workspace(db, workspace)
            for row in rows:
                prior = db.execute('SELECT content_hash FROM imported_statement_transactions WHERE workspace_id=? AND transaction_id=?',
                                   (workspace, row['transaction_id'])).fetchone()
                if prior and prior['content_hash'] != row['content_hash']:
                    errors.append({'row': '数据库', 'message': f'流水号{row["transaction_id"]}已存在，但内容不同；无法覆盖'})
                    if len(errors) >= 200: break
        return file_hash, rows, errors, record_count, in_file_duplicates

    def preview(self, workspace, text):
        file_hash, rows, errors, record_count, in_file_duplicates = self._parse(workspace, text)
        if errors:
            return {'valid': False, 'batch_id': None, 'expires_in_seconds': 0, 'input_count': record_count,
                    'duplicate_count': 0, 'date_range': self._range(rows), 'errors': errors,
                    'sample': [{k:v for k,v in row.items() if k != 'content_hash'} for row in rows[:5]],
                    'notice': '存在无效行或流水冲突，整批不能提交；原始CSV未保存。'}
        batch_id = secrets.token_hex(24)
        dataset_id = self._dataset(workspace)
        existing = set()
        if rows and dataset_id:
            with closing(self.bank.connect()) as db:
                ids = [row['transaction_id'] for row in rows]
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start+500]
                    q = ','.join('?' for _ in chunk)
                    existing.update(r[0] for r in db.execute(
                        f'SELECT transaction_id FROM imported_statement_transactions WHERE workspace_id=? AND transaction_id IN ({q})',
                        [workspace, *chunk]))
        duplicate_count = len(existing) + in_file_duplicates
        entry = {'workspace': workspace, 'expires_at': self.clock()+PREVIEW_TTL, 'file_hash': file_hash,
                 'rows': rows, 'input_count': record_count, 'duplicate_count': duplicate_count}
        with self._lock:
            self._purge_previews()
            if len(self._previews) >= 16:
                oldest = min(self._previews, key=lambda key: self._previews[key]['expires_at'])
                self._previews.pop(oldest, None)
            workspace_previews = sorted(
                ((key, value) for key, value in self._previews.items() if value['workspace']==workspace),
                key=lambda item: item[1]['expires_at'])
            while len(workspace_previews) >= 4:
                key, _ = workspace_previews.pop(0)
                self._previews.pop(key, None)
            self._previews[batch_id] = entry
        return {'valid': True, 'batch_id': batch_id, 'expires_in_seconds': PREVIEW_TTL,
                'input_count': entry['input_count'], 'new_count': len(rows)-len(existing),
                'duplicate_count': duplicate_count, 'date_range': self._range(rows), 'errors': [],
                'sample': [{k:v for k,v in row.items() if k != 'content_hash'} for row in rows[:5]],
                'notice': '预览不写入正式流水或银行余额；同内容重复流水会跳过，预览10分钟后失效。'}

    def _purge_previews(self):
        now = self.clock()
        for key in [k for k,v in self._previews.items() if v['expires_at'] <= now]:
            self._previews.pop(key, None)

    @staticmethod
    def _range(rows):
        days = [r['occurred_at'][:10] for r in rows]
        return [min(days), max(days)] if days else None

    def _dataset(self, workspace):
        with closing(self.bank.connect()) as db:
            row = db.execute('SELECT id FROM statement_datasets WHERE workspace_id=?', (workspace,)).fetchone()
            return row['id'] if row else None

    def cancel(self, workspace, payload):
        strict_fields(payload, ['batch_id'], ['batch_id'])
        batch_id = payload['batch_id']
        if not isinstance(batch_id, str) or not re.fullmatch(r'[a-f0-9]{48}', batch_id):
            raise BankError('INVALID_BATCH', '预览批次编号无效。')
        with self._lock:
            self._purge_previews()
            entry = self._previews.get(batch_id)
            if not entry or entry['workspace'] != workspace:
                raise BankError('PREVIEW_EXPIRED', '预览已取消、过期或不属于当前会话。', 409)
            self._previews.pop(batch_id, None)
        with self.bank.transaction() as db:
            self.bank._workspace(db, workspace)
            self.bank._audit(db, workspace, 'statement_import_cancelled', {'batch_id': batch_id,
                'record_count': entry['input_count'], 'content_hash': entry['file_hash']})
        return {'batch_id': batch_id, 'status': 'cancelled', 'imported': False}

    def commit(self, workspace, payload):
        strict_fields(payload, ['batch_id', 'confirmed'], ['batch_id', 'confirmed'])
        batch_id = payload['batch_id']
        if not isinstance(batch_id, str) or not re.fullmatch(r'[a-f0-9]{48}', batch_id):
            raise BankError('INVALID_BATCH', '预览批次编号无效。')
        if payload['confirmed'] is not True:
            raise BankError('CONFIRMATION_REQUIRED', '请明确确认后再导入账单。')
        prior = self._stored_batch(workspace, batch_id)
        if prior is not None:
            with self._lock:
                self._previews.pop(batch_id, None)
            return {**prior, 'idempotent_replay': True}
        with self._lock:
            self._purge_previews()
            entry = self._previews.get(batch_id)
            if entry and entry['workspace'] != workspace:
                entry = None
        if entry is None:
            # Another request may have committed and evicted this preview after our first read.
            prior = self._stored_batch(workspace, batch_id)
            if prior is not None:
                return {**prior, 'idempotent_replay': True}
            raise BankError('PREVIEW_EXPIRED', '账单预览已过期、取消或不属于当前会话，请重新上传。', 409)
        now = self.clock()
        with self.bank.transaction() as db:
            self.bank._workspace(db, workspace)
            result, replay = self._commit_in_transaction(db, workspace, batch_id, entry, now)
            if not replay and self.on_source_activated is not None:
                self.on_source_activated(workspace, db=db)
        with self._lock:
            self._previews.pop(batch_id, None)
        return {**result, 'idempotent_replay': replay}

    def _stored_batch(self, workspace, batch_id):
        with closing(self.bank.connect()) as db:
            self.bank._workspace(db, workspace)
            prior = db.execute('SELECT result FROM statement_import_batches WHERE workspace_id=? AND batch_id=?',
                               (workspace, batch_id)).fetchone()
            return json.loads(prior['result']) if prior else None

    def _commit_in_transaction(self, db, workspace, batch_id, entry, now):
        # Recheck while holding BEGIN IMMEDIATE so simultaneous confirmations replay the first receipt.
        prior = db.execute('SELECT result,content_hash FROM statement_import_batches WHERE workspace_id=? AND batch_id=?',
                           (workspace, batch_id)).fetchone()
        if prior:
            if prior['content_hash'] != entry['file_hash']:
                raise BankError('BATCH_CONFLICT', '预览批次编号已用于其他内容。', 409)
            return json.loads(prior['result']), True
        rows = entry['rows']
        conflict = None
        for row in rows:
            old = db.execute('SELECT content_hash FROM imported_statement_transactions WHERE workspace_id=? AND transaction_id=?',
                             (workspace, row['transaction_id'])).fetchone()
            if old and old['content_hash'] != row['content_hash']:
                conflict = row['transaction_id']; break
        if conflict:
            self.bank._audit(db, workspace, 'statement_import_conflict', {'batch_id': batch_id,
                'transaction_id': conflict, 'content_hash': entry['file_hash']})
            raise BankError('TRANSACTION_CONFLICT', f'流水号{conflict}已被其他导入占用；没有写入任何记录。', 409, persist=True)
        dataset = db.execute('SELECT * FROM statement_datasets WHERE workspace_id=?', (workspace,)).fetchone()
        if not dataset:
            dataset_id = secrets.token_hex(16)
            db.execute('''INSERT INTO statement_datasets(id,workspace_id,source_name,currency,version,transaction_count,
                       first_date,last_date,created_at,updated_at) VALUES(?,?,?, 'CNY',0,0,NULL,NULL,?,?)''',
                       (dataset_id, workspace, '本机CSV导入', now, now))
        else:
            dataset_id = dataset['id']
        inserted = 0
        for row in rows:
            exists = db.execute('SELECT content_hash FROM imported_statement_transactions WHERE workspace_id=? AND transaction_id=?',
                                 (workspace, row['transaction_id'])).fetchone()
            if exists:
                continue
            db.execute('''INSERT INTO imported_statement_transactions(workspace_id,dataset_id,transaction_id,occurred_at,
                       time_precision,merchant,amount_minor,kind,category,currency,note,content_hash,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (workspace, dataset_id, row['transaction_id'], row['occurred_at'], row['time_precision'],
                        row['merchant'], row['amount_minor'], row['kind'], row['category'], row['currency'],
                        row['note'], row['content_hash'], now))
            inserted += 1
        skipped = max(0,entry['input_count']-inserted)
        bounds = db.execute('SELECT min(substr(occurred_at,1,10)) lo,max(substr(occurred_at,1,10)) hi,count(*) n FROM imported_statement_transactions WHERE workspace_id=?',
                            (workspace,)).fetchone()
        db.execute('''UPDATE statement_datasets SET version=version+?,transaction_count=?,first_date=?,last_date=?,updated_at=?
                      WHERE id=?''', (int(inserted > 0), bounds['n'], bounds['lo'], bounds['hi'], now, dataset_id))
        result = {'batch_id': batch_id, 'dataset_id': dataset_id, 'status': 'committed',
                  'input_count': entry['input_count'], 'inserted_count': inserted, 'skipped_count': skipped,
                  'source': 'imported_local_csv', 'notice': '仅写入独立本地账单数据集；没有修改模拟银行余额、持仓或订单。'}
        db.execute('''INSERT INTO statement_import_batches(batch_id,workspace_id,dataset_id,content_hash,input_count,
                   inserted_count,skipped_count,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                   (batch_id, workspace, dataset_id, entry['file_hash'], entry['input_count'], inserted, skipped,
                    encoded(result), now))
        self.bank._audit(db, workspace, 'statement_import_committed', {'batch_id': batch_id,
            'dataset_id': dataset_id, 'record_count': inserted, 'skipped_count': skipped,
            'content_hash': entry['file_hash'], 'source': 'imported_local_csv'})
        return result, False

    def source(self, workspace):
        with closing(self.bank.connect()) as db:
            self.bank._workspace(db, workspace)
            selected = db.execute('SELECT source,updated_at FROM finance_data_sources WHERE workspace_id=?', (workspace,)).fetchone()
            dataset = db.execute('SELECT id,source_name,version,transaction_count,first_date,last_date,currency FROM statement_datasets WHERE workspace_id=?', (workspace,)).fetchone()
            return {'active_source': selected['source'] if selected else 'simulation',
                    'imported_available': bool(dataset), 'dataset': dict(dataset) if dataset else None,
                    'notice': '账单来源切换仅影响账单分析；资产、理财操作和银行代扣仍是独立模拟账户。'}

    def set_source(self, workspace, payload):
        strict_fields(payload, ['source'], ['source'])
        source = payload['source']
        if source not in ('simulation', 'imported'):
            raise BankError('INVALID_SOURCE', '账单来源只能选择模拟数据或本地导入数据。')
        with self.bank.transaction() as db:
            self.bank._workspace(db, workspace)
            dataset = db.execute('SELECT id FROM statement_datasets WHERE workspace_id=?', (workspace,)).fetchone()
            if source == 'imported' and not dataset:
                raise BankError('NO_IMPORTED_DATA', '当前会话还没有已导入的账单。', 409)
            old = db.execute('SELECT source FROM finance_data_sources WHERE workspace_id=?', (workspace,)).fetchone()
            previous = old['source'] if old else 'simulation'
            db.execute('''INSERT INTO finance_data_sources(workspace_id,source,updated_at) VALUES(?,?,?)
                          ON CONFLICT(workspace_id) DO UPDATE SET source=excluded.source,updated_at=excluded.updated_at''',
                       (workspace, source, self.clock()))
            if previous != source:
                self.bank._audit(db, workspace, 'finance_data_source_changed', {'from': previous, 'to': source,
                    'dataset_id': dataset['id'] if source == 'imported' and dataset else None,
                    'scope': 'statement_analysis_only'})
                if self.on_source_activated is not None:
                    self.on_source_activated(workspace, db=db)
        return self.source(workspace)

    def _imported_rows(self, workspace, month=None):
        if month is not None and (not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month)):
            raise BankError('INVALID_MONTH', '月份格式为YYYY-MM。')
        with closing(self.bank.connect()) as db:
            self.bank._workspace(db, workspace)
            dataset = db.execute('SELECT id FROM statement_datasets WHERE workspace_id=?', (workspace,)).fetchone()
            if not dataset:
                raise BankError('NO_IMPORTED_DATA', '当前会话没有本地导入账单。', 409)
            sql = 'SELECT * FROM imported_statement_transactions WHERE workspace_id=?'
            args = [workspace]
            if month:
                sql += ' AND substr(occurred_at,1,7)=?'; args.append(month)
            sql += ' ORDER BY occurred_at,transaction_id'
            rows = [dict(r) for r in db.execute(sql, args)]
        return [{**row, 'id': row['transaction_id'], 'account_id': 'imported-statement',
                 'delta_minor': row['amount_minor'] if row['kind'] in ('income','refund') else -row['amount_minor'] if row['kind']=='expense' else 0,
                 'source': 'imported_local_csv', 'mandate_id': None, 'operation_id': None}
                for row in rows]

    def transactions(self, workspace, month=None):
        return self._imported_rows(workspace, month)

    def analysis(self, workspace, month):
        if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
            raise BankError('INVALID_MONTH', '月报需要指定YYYY-MM格式的月份。')
        rows = self._imported_rows(workspace, month)
        totals = {'income': 0, 'expense': 0, 'refund': 0, 'transfer': 0,
                  'investment_purchase': 0, 'investment_redemption': 0}
        categories = defaultdict(int)
        category_ids = defaultdict(list)
        for row in rows:
            totals[row['kind']] += row['amount_minor']
            if row['kind'] == 'expense':
                categories[row['category']] += row['amount_minor']
                category_ids[row['category']].append(row['id'])
        source = self.source(workspace)['dataset']
        return {'environment': 'local_import', 'source': 'imported_local_csv', 'dataset_id': source['id'],
                'dataset_version': source['version'], 'month': month, 'transaction_count': len(rows),
                'recorded_range': [rows[0]['occurred_at'], rows[-1]['occurred_at']] if rows else None,
                'totals_minor': totals, 'category_expense_minor': dict(categories),
                'transaction_ids': [r['id'] for r in rows], 'category_transaction_ids': dict(category_ids),
                'income_expense_surplus_minor': totals['income']-totals['expense']+totals['refund'],
                'cash_delta_minor': None,
                'notice': '本地导入账单仅覆盖文件提供的流水；不能据此推算银行余额或断言完整月账单。转账单列，不计入收支结余。'}

    def annual_analysis(self, workspace, year):
        if type(year) is not int or not 1900 <= year <= 2100:
            raise BankError('INVALID_YEAR', '年度须为1900至2100之间的整数。')
        rows = [r for r in self._imported_rows(workspace) if r['occurred_at'][:4] == str(year)]
        months = sorted({r['occurred_at'][:7] for r in rows})
        totals = defaultdict(int); categories = defaultdict(int)
        for row in rows:
            totals[row['kind']] += row['amount_minor']
            if row['kind'] == 'expense': categories[row['category']] += row['amount_minor']
        return {'environment': 'local_import', 'source': 'imported_local_csv', 'dataset_id': self.source(workspace)['dataset']['id'],
                'year': year, 'covered_months': months, 'complete_calendar_year': len(months) == 12,
                'archived_count': 0, 'live_count': len(rows), 'transaction_count': len(rows),
                'totals_minor': dict(totals), 'category_expense_minor': dict(categories),
                'cash_delta_minor': None,
                'notice': '本地文件覆盖月份不等于全年完整。统计范围限于已导入记录，不推算银行余额。'}

    def anomalies(self, workspace):
        rows = self._imported_rows(workspace)
        by_key = defaultdict(list)
        for row in rows:
            if row['kind'] == 'expense' and row['time_precision'] == 'datetime':
                by_key[(row['merchant'], row['amount_minor'])].append(row)
        duplicates = []
        for group in by_key.values():
            index = 0
            while index < len(group):
                first = group[index]
                first_dt = datetime.fromisoformat(first['occurred_at'])
                related = [first]
                cursor = index+1
                while cursor < len(group):
                    other = group[cursor]
                    if (datetime.fromisoformat(other['occurred_at'])-first_dt).total_seconds() > 300:
                        break
                    related.append(other);cursor+=1
                if len(related) > 1:
                    ids = [r['id'] for r in related]
                    duplicates.append({'rule': 'duplicate_charge_5m', 'merchant': first['merchant'],
                                       'amount_minor': first['amount_minor'], 'transaction_ids': ids,
                                       'notice': '同商户、同额支出在5分钟内重复出现；仅待核实。'})
                    index=cursor
                else:
                    index+=1
        high = []
        for idx, row in enumerate(rows):
            if row['kind'] != 'expense' or row['amount_minor'] < 300_000:
                continue
            prior = [r['amount_minor'] for r in rows[:idx] if r['kind']=='expense' and r['category']==row['category']]
            if len(prior) >= 5:
                median = sorted(prior)[(len(prior)-1)//2]
                if row['amount_minor'] >= median*3:
                    high.append({'rule': 'high_amount', 'transaction_ids': [row['id']], 'amount_minor': row['amount_minor'],
                                 'baseline_count': len(prior), 'baseline_median_minor': median,
                                 'notice': '金额较此前同类消费显著偏高；仅待核实。'})
        return {'source': 'imported_local_csv', 'rule_version': 'local-import-anomaly-v1',
                'duplicates': duplicates, 'high_amounts': high,
                'notice': '规则只用于初筛；没有提示不代表交易安全，也不构成欺诈结论。'}

    def periodic_charges(self, workspace):
        rows = self._imported_rows(workspace)
        candidates = SimulatedBank._periodic_candidate_rows(rows)
        with closing(self.bank.connect()) as db:
            marks = {r['candidate_id']: dict(r) for r in db.execute(
                'SELECT candidate_id,decision,created_at,updated_at FROM imported_periodic_marks WHERE workspace_id=?', (workspace,))}
        for c in candidates:
            mark = marks.get(c['candidate_id'])
            c['user_decision'] = mark['decision'] if mark else None
            c['marked_at'] = mark['updated_at'] if mark else None
            c['status'] = '已确认是周期扣费（仅本地标记）' if mark and mark['decision']=='confirmed' else \
                          '已忽略（仅本地标记）' if mark and mark['decision']=='ignored' else '需用户核实'
            c['classification'] = '固定生活支出' if c['classification']=='固定生活支出' else '疑似周期扣费（本地导入）'
            c['source'] = 'imported_local_csv'
        return {'source': 'imported_local_csv', 'rule_version': 'sim-monthly-v1', 'candidates': candidates,
                'notice': '基于已导入流水的月度周期规则；需要用户核实，不代表已订阅或已扣款。'}

    def mark_periodic_candidate(self, workspace, payload):
        strict_fields(payload, ['candidate_id','decision'], ['candidate_id','decision'])
        candidate_id, decision = payload['candidate_id'], payload['decision']
        if not isinstance(candidate_id, str) or not re.fullmatch(r'[a-f0-9]{64}', candidate_id):
            raise BankError('INVALID_CANDIDATE', '周期扣费候选标识无效。')
        if decision not in ('confirmed','ignored'):
            raise BankError('INVALID_DECISION', '用户标记只能为确认或忽略。')
        now = self.clock()
        with self.bank.transaction() as db:
            self.bank._workspace(db, workspace)
            raw_rows = [dict(r) for r in db.execute(
                'SELECT * FROM imported_statement_transactions WHERE workspace_id=? ORDER BY occurred_at,transaction_id', (workspace,))]
            rows = [{**row,'id':row['transaction_id'],'account_id':'imported-statement','mandate_id':None}
                    for row in raw_rows]
            candidates = {c['candidate_id']: c for c in SimulatedBank._periodic_candidate_rows(rows)}
            candidate = candidates.get(candidate_id)
            if not candidate:
                raise BankError('CANDIDATE_CHANGED', '流水已变化，这条本地周期扣费候选已失效，请刷新后复核。', 409)
            old = db.execute('SELECT decision FROM imported_periodic_marks WHERE workspace_id=? AND candidate_id=?',
                             (workspace,candidate_id)).fetchone()
            if old and old['decision']==decision:
                return {'candidate_id':candidate_id,'decision':decision,
                        'status':'已确认是周期扣费（仅本地标记）' if decision=='confirmed' else '已忽略（仅本地标记）',
                        'merchant':candidate['merchant'],'transaction_ids':candidate['transaction_ids'],
                        'notice':'此操作只更新本地导入账单的分析标记；不会修改模拟银行代扣或商户订阅。'}
            db.execute('''INSERT INTO imported_periodic_marks(workspace_id,candidate_id,decision,details,created_at,updated_at)
                          VALUES(?,?,?,?,?,?) ON CONFLICT(workspace_id,candidate_id) DO UPDATE SET
                          decision=excluded.decision,details=excluded.details,updated_at=excluded.updated_at''',
                       (workspace,candidate_id,decision,encoded(candidate),now,now))
            if not old or old['decision'] != decision:
                self.bank._audit(db,workspace,'imported_periodic_candidate_marked',{
                    'candidate_id':candidate_id,'merchant':candidate['merchant'],'decision':decision,
                    'previous_decision':old['decision'] if old else None,'scope':'local_import_analysis_only',
                    'transaction_ids':candidate['transaction_ids'],'rule_version':'sim-monthly-v1'})
        return {'candidate_id':candidate_id,'decision':decision,
                'status':'已确认是周期扣费（仅本地标记）' if decision=='confirmed' else '已忽略（仅本地标记）',
                'merchant':candidate['merchant'],'transaction_ids':candidate['transaction_ids'],
                'notice':'此操作只更新本地导入账单的分析标记；不会修改模拟银行代扣或商户订阅。'}
