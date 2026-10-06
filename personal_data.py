"""独立的、按服务端工作区隔离的个人资料库；银行库只保留失效标记。

敏感正文用 Fernet 保存。密钥来自环境或 macOS Keychain，不生成 keyfile。
跨库变更先持有银行写锁；私库提交后的未完成失效操作可以在重启时恢复。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from datetime import date
from pathlib import Path

from simulated_bank.service import BankError

LEGACY_TABLES = ('user_memory_settings', 'user_memories', 'user_memory_relations',
                 'profile_observations', 'agent_dialogues', 'agent_widget_history')
PRIVATE_TABLES = frozenset(LEGACY_TABLES + ('memory_candidates', 'conversation_sources'))
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS personal_versions(
 workspace_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
 pending_invalidation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_memory_settings(
 workspace_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS user_memories(
 id TEXT NOT NULL,workspace_id TEXT NOT NULL,category TEXT NOT NULL,value TEXT NOT NULL,
 origin TEXT NOT NULL,user_confirmed INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
 expires_on TEXT,source_type TEXT NOT NULL,source_ref TEXT,confidence REAL NOT NULL DEFAULT 1,
 confirmed_at REAL,record_revision INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(workspace_id,id));
CREATE TABLE IF NOT EXISTS user_memory_relations(
 id TEXT NOT NULL,workspace_id TEXT NOT NULL,source_node_id TEXT NOT NULL,target_node_id TEXT NOT NULL,
 relation TEXT NOT NULL,source_type TEXT NOT NULL DEFAULT 'user_confirmed',created_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,id),UNIQUE(workspace_id,source_node_id,target_node_id,relation),
 FOREIGN KEY(workspace_id,source_node_id) REFERENCES user_memories(workspace_id,id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,target_node_id) REFERENCES user_memories(workspace_id,id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS profile_observations(
 id TEXT NOT NULL,workspace_id TEXT NOT NULL,category TEXT NOT NULL,value TEXT NOT NULL,
 quote TEXT NOT NULL,source_ref TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,
 expires_on TEXT,confirmed_at REAL,expected_revision INTEGER NOT NULL DEFAULT 0,
 confirmation_token TEXT,source_verified INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(workspace_id,id));
CREATE TABLE IF NOT EXISTS conversation_sources(
 workspace_id TEXT NOT NULL,source_ref TEXT NOT NULL,text TEXT NOT NULL,
 text_hash TEXT NOT NULL,created_at REAL NOT NULL,expected_revision INTEGER NOT NULL,
 PRIMARY KEY(workspace_id,source_ref));
CREATE TABLE IF NOT EXISTS memory_candidates(
 workspace_id TEXT NOT NULL,proposal_id TEXT NOT NULL,category TEXT NOT NULL,value TEXT NOT NULL,
 source_ref TEXT NOT NULL,confirmation_token TEXT NOT NULL,expected_revision INTEGER NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL,expires_on TEXT,status TEXT NOT NULL DEFAULT 'candidate',
 PRIMARY KEY(workspace_id,proposal_id),
 FOREIGN KEY(workspace_id,source_ref) REFERENCES conversation_sources(workspace_id,source_ref) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS agent_dialogues(
 workspace_id TEXT PRIMARY KEY,messages TEXT NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_widget_history(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,widgets TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,response_id));
CREATE TABLE IF NOT EXISTS guarded_turn_saves(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,save_generation TEXT NOT NULL UNIQUE,
 personal_revision INTEGER NOT NULL,privacy_revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,
 history_hash TEXT NOT NULL,widget_hash TEXT,previous_messages TEXT,previous_updated_at REAL,
 previous_generation TEXT,status TEXT NOT NULL,saved_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,response_id));
CREATE TABLE IF NOT EXISTS personal_audit(
 id INTEGER PRIMARY KEY,workspace_id TEXT NOT NULL,event TEXT NOT NULL,object_id TEXT,
 revision INTEGER NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS private_crypto(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_rows(
 legacy_database TEXT NOT NULL,table_name TEXT NOT NULL,row_key TEXT NOT NULL,row_hash TEXT NOT NULL,
 PRIMARY KEY(legacy_database,table_name,row_key));
CREATE TABLE IF NOT EXISTS migration_state(
 legacy_database TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS memory_owner ON user_memories(workspace_id,category,updated_at);
CREATE INDEX IF NOT EXISTS profile_owner ON profile_observations(workspace_id,status,created_at);
'''


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _key_from_system():
    value = os.environ.get('NAVREN_PERSONAL_DATA_KEY')
    if value:
        return value.encode('ascii')
    # Keychain 输出只在内存使用，不进入命令日志、审计或数据库。
    account = os.environ.get('USER', '')
    lookup = ['security', 'find-generic-password', '-a', account,
              '-s', 'CC/NAVREN_PERSONAL_DATA_KEY', '-w']
    try:
        found = subprocess.run(lookup, capture_output=True, timeout=10, check=False)
        if found.returncode == 0:
            return found.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise BankError('PERSONAL_KEY_UNAVAILABLE', '个人资料加密密钥不可用，请配置环境变量或本机钥匙串。', 503)


def redact_sensitive(text):
    """过滤凭证与直接身份标识；金融金额和目标不做无依据替换。"""
    if not isinstance(text, str):
        return text
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[已隐藏手机号]', text)
    text = re.sub(r'(?<!\d)\d(?:[ -]?\d){11,18}[Xx]?(?!\d)', '[已隐藏号码]', text)
    return re.sub(r'(?i)((?:密码|验证码|口令|身份证号?|银行卡号?|手机号?|token|password|api[_ -]?key)\s*[:：=]?\s*)[^，。；\s,;]{1,100}', r'\1[已隐藏]', text)


def explicit_self_statement(text):
    """保守的本地准入：排除假设、第三人转述、疑问与推测。"""
    if not isinstance(text, str) or not text.strip():
        return False
    if re.search(r'假设|假如|如果|假定|举例|例如|比方|可能|也许|大概|猜测|听说|据说|设想|要是|[?？“”「」]|(?:朋友|同学|同事|老师|导师|父母|父亲|母亲|爸爸|妈妈|家人|配偶|妻子|丈夫|孩子|他|她|别人).{0,16}(?:说|想|准备|计划|打算|需要|喜欢|希望|目标|偏好)', text):
        return False
    return bool(re.match(r'\s*(?:(?:明年|今年|下个月|下月|未来\d{1,2}个月|\d{1,2}月)\s*)?(?:我(?:的|们)?|本人|以后请|今后请|请长期)', text))


class PersonalDataStore:
    def __init__(self, bank_or_path, *, encryption_key=None, clock=None):
        self.bank = bank_or_path if hasattr(bank_or_path, 'connect') else None
        self.database = (self.bank.database.with_name(self.bank.database.stem + '.personal.sqlite3')
                         if self.bank else Path(bank_or_path).expanduser().resolve())
        self.clock = clock or (self.bank.clock if self.bank else time.time)
        try:
            from cryptography.fernet import Fernet
            self._cipher = Fernet(encryption_key or _key_from_system())
        except ImportError:
            raise BankError('PERSONAL_ENCRYPTION_UNAVAILABLE', '个人资料加密组件不可用。', 503) from None
        except ValueError:
            raise BankError('PERSONAL_KEY_INVALID', '个人资料加密密钥格式无效。', 503) from None
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.database.chmod(0o600)
        with closing(self.connect()) as db:
            db.executescript(_SCHEMA)
            check = db.execute("SELECT value FROM private_crypto WHERE key='key_check'").fetchone()
            if check is None:
                db.execute("INSERT INTO private_crypto VALUES('key_check',?)", (self.protect('navren-personal-v1'),))
                db.commit()
            elif check[0] != 'navren-personal-v1':
                raise BankError('PERSONAL_DATA_UNREADABLE', '个人资料密钥与已有数据库不符。', 503)
        self.database.chmod(0o600)
        if self.bank:
            self.bank.personal_store = self
            with self.bank.transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS personal_data_binding(id INTEGER PRIMARY KEY CHECK(id=1),database_path TEXT NOT NULL)')
                db.execute('INSERT INTO personal_data_binding VALUES(1,?) ON CONFLICT(id) DO UPDATE SET database_path=excluded.database_path', (str(self.database),))
            self.migrate_legacy()
            self.recover_invalidations()

    def protect(self, value):
        if value is None:
            return None
        return 'pd1:' + self._cipher.encrypt(str(value).encode()).decode()

    def _decode(self, value):
        if isinstance(value, str) and value.startswith('pd1:'):
            try:
                return self._cipher.decrypt(value[4:].encode()).decode()
            except Exception:
                raise BankError('PERSONAL_DATA_UNREADABLE', '个人资料无法用当前密钥读取，已停止读取以避免丢失数据。', 503) from None
        return value

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        private_fields = {'value', 'quote', 'text', 'messages', 'widgets'}
        db.row_factory = lambda cursor, row: sqlite3.Row(cursor, tuple(
            self._decode(x) if cursor.description[i][0] in private_fields else x
            for i, x in enumerate(row)))
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('PRAGMA secure_delete=ON')
        return db

    def validate_owner(self, owner):
        if not isinstance(owner, str) or not owner or len(owner) > 160:
            raise BankError('INVALID_OWNER', '个人资料需要服务端绑定的用户身份。')
        if self.bank:
            with closing(self.bank.connect()) as db:
                if not db.execute('SELECT 1 FROM workspaces WHERE id=?', (owner,)).fetchone():
                    raise BankError('NOT_FOUND', '当前工作区不存在。', 404)
        return owner

    @contextmanager
    def connection(self, owner):
        self.validate_owner(owner)
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            yield db

    def revision(self, owner):
        self.validate_owner(owner)
        with closing(self.connect()) as db:
            row = db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?', (owner,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def discard_unused_source(db, owner, source_ref):
        """撤回最后一条引用时一并清除原话和候选；不删除其他条目的依据。"""
        if not source_ref:
            return
        for table in ('user_memories', 'profile_observations'):
            if db.execute('SELECT 1 FROM '+table+' WHERE workspace_id=? AND source_ref=? LIMIT 1',
                          (owner, source_ref)).fetchone():
                return
        db.execute('DELETE FROM conversation_sources WHERE workspace_id=? AND source_ref=?',
                   (owner, source_ref))

    @staticmethod
    def check_revision(db, owner, expected):
        row = db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?', (owner,)).fetchone()
        actual = row[0] if row else 0
        if expected is not None and (type(expected) is not int or expected != actual):
            raise BankError('PERSONAL_VERSION_CONFLICT', '个人资料已变化，请重新读取后再确认。', 409)
        return actual

    @contextmanager
    def transaction(self, owner, *, expected_revision=None, expected_privacy=None,
                    change=True, event='personal_changed', object_id=None):
        self.validate_owner(owner)
        # 银行确认、图谱计算及个人变更使用同一写锁序列，避免跨库权限竞态。
        with (self.bank.transaction() if self.bank else nullcontext(None)) as bank_db:
            if expected_privacy is not None and bank_db is not None:
                from user_memory import privacy_revision
                if type(expected_privacy) is not int or privacy_revision(bank_db, owner) != expected_privacy:
                    raise BankError('PERSONAL_PRIVACY_CONFLICT', '资料权限已变化，旧对话不能保存。', 409)
            db = self.connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                revision = self.check_revision(db, owner, expected_revision)
                db.execute('INSERT OR IGNORE INTO personal_versions(workspace_id) VALUES(?)', (owner,))
                yield db
                if change:
                    revision += 1
                    db.execute('UPDATE personal_versions SET revision=?,pending_invalidation=1 WHERE workspace_id=?', (revision, owner))
                    # 完整聊天和旧组件不继续作为推断依据；审计仅留事件和对象 ID。
                    for table in ('agent_dialogues', 'agent_widget_history'):
                        db.execute('DELETE FROM '+table+' WHERE workspace_id=?', (owner,))
                    # 撤回用的旧历史同样属于正文；用户修改或删除后不能留下恢复副本。
                    db.execute('DELETE FROM guarded_turn_saves WHERE workspace_id=?', (owner,))
                    db.execute("UPDATE memory_candidates SET status='stale' WHERE workspace_id=? AND status='candidate'", (owner,))
                    db.execute('INSERT INTO personal_audit(workspace_id,event,object_id,revision,created_at) VALUES(?,?,?,?,?)',
                               (owner, event, object_id, revision, self.clock()))
                db.commit()
                if change and bank_db is not None:
                    self._invalidate_bank(bank_db, owner, revision)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        if change:
            # 最后标记完成；任意崩溃都留下可重放的私库 outbox。
            with closing(self.connect()) as db, db:
                db.execute('UPDATE personal_versions SET pending_invalidation=0 WHERE workspace_id=? AND revision=?', (owner, revision))
        self._permissions()

    def _permissions(self):
        for path in (self.database, Path(str(self.database)+'-journal'), Path(str(self.database)+'-wal'), Path(str(self.database)+'-shm')):
            if path.exists():
                path.chmod(0o600)

    @staticmethod
    def _invalidate_bank(db, owner, revision):
        from user_memory import invalidate_graph
        invalidate_graph(db, owner)
        db.execute('CREATE TABLE IF NOT EXISTS personal_data_versions(workspace_id TEXT PRIMARY KEY,revision INTEGER NOT NULL)')
        db.execute('INSERT INTO personal_data_versions VALUES(?,?) ON CONFLICT(workspace_id) DO UPDATE SET revision=excluded.revision', (owner, revision))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_draft_reviews'").fetchone():
            db.execute("UPDATE agent_draft_reviews SET status='superseded' WHERE workspace_id=? AND status IN ('pending','approved')", (owner,))

    def recover_invalidations(self):
        if not self.bank:
            return
        with closing(self.connect()) as db:
            pending = db.execute('SELECT workspace_id,revision FROM personal_versions WHERE pending_invalidation=1').fetchall()
        for row in pending:
            with self.bank.transaction() as db:
                if db.execute('SELECT 1 FROM workspaces WHERE id=?', (row[0],)).fetchone():
                    self._invalidate_bank(db, row[0], row[1])
            with closing(self.connect()) as db, db:
                db.execute('UPDATE personal_versions SET pending_invalidation=0 WHERE workspace_id=? AND revision=?', (row[0], row[1]))

    def migrate_legacy(self, *, fail_after_copy=False):
        """先幂等复制并记录逐行指纹，再删除源正文；重启可重复清理。

        持有银行写锁，迁移期间旧路径不能新增记录。未知旧对话候选不晋升。
        """
        if not self.bank:
            return
        legacy = str(self.bank.database)
        with self.bank.transaction() as old, closing(self.connect()) as new:
            new.execute('BEGIN IMMEDIATE')
            try:
                tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in LEGACY_TABLES:
                    if table not in tables:
                        continue
                    for row in old.execute('SELECT * FROM '+table).fetchall():
                        data = dict(row)
                        owner = data.get('workspace_id')
                        if not owner:
                            raise BankError('PERSONAL_MIGRATION_INVALID', '旧个人资料缺少用户身份，迁移已停止。', 503)
                        row_key = str(data.get('id', data.get('response_id', owner)))
                        fingerprint = hashlib.sha256(_json(data).encode()).hexdigest()
                        copied = new.execute('SELECT row_hash FROM migration_rows WHERE legacy_database=? AND table_name=? AND row_key=?',
                                             (legacy, table, owner+':'+row_key)).fetchone()
                        if copied:
                            if copied[0] != fingerprint:
                                raise BankError('PERSONAL_MIGRATION_CONFLICT', '待清理的旧个人资料已变化，停止迁移以避免覆盖。', 503)
                            continue
                        target_cols = {r[1] for r in new.execute('PRAGMA table_info('+table+')')}
                        data = {k:v for k,v in data.items() if k in target_cols}
                        if table == 'user_memories':
                            # 无法证明旧对话来源时仍保留内容，但不供 Agent 当作确认事实。
                            if data.get('source_type') == 'conversation_user_statement':
                                data['user_confirmed'] = 0
                                data['confirmed_at'] = None
                            data.setdefault('source_type', 'user_entered')
                            from user_memory import UserMemoryStore
                            try:
                                UserMemoryStore._value(data['value'])
                            except BankError:
                                data['user_confirmed'] = 0
                                data['confirmed_at'] = None
                        if table == 'profile_observations':
                            data['status'] = 'candidate'
                            data['source_verified'] = 0
                        for field in ('value', 'quote', 'text', 'messages', 'widgets'):
                            if field in data:
                                data[field] = self.protect(data[field])
                        columns = list(data)
                        new.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', tuple(data.values()))
                        new.execute('INSERT INTO migration_rows VALUES(?,?,?,?)', (legacy, table, owner+':'+row_key, fingerprint))
                        new.execute('INSERT OR IGNORE INTO personal_versions(workspace_id,pending_invalidation) VALUES(?,1)', (owner,))
                new.execute("INSERT INTO migration_state VALUES(?,'copied',?) ON CONFLICT(legacy_database) DO UPDATE SET status='copied',updated_at=excluded.updated_at", (legacy, self.clock()))
                new.commit()
            except Exception:
                new.rollback()
                raise
            if fail_after_copy:
                raise RuntimeError('模拟迁移在私库复制完成后中断')
            # 只清理已复制的原表。保留空 schema 供旧业务升级兼容，但不保留正文镜像。
            old.execute('PRAGMA secure_delete=ON')
            for table in ('user_memory_relations', 'user_memories', 'user_memory_settings', 'profile_observations', 'agent_widget_history', 'agent_dialogues'):
                if table in tables:
                    old.execute('DELETE FROM '+table)
        with closing(self.connect()) as db, db:
            db.execute("UPDATE migration_state SET status='complete',updated_at=? WHERE legacy_database=?", (self.clock(), legacy))
        self._permissions()

    def register_conversation_source(self, owner, source_ref, text, *, expected_revision=None):
        if not isinstance(source_ref, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', source_ref):
            raise BankError('INVALID_MEMORY_SOURCE', '对话来源标识无效。')
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise BankError('INVALID_MEMORY_SOURCE', '对话原话无效。')
        with self.transaction(owner, expected_revision=expected_revision, change=False) as db:
            setting = db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?', (owner,)).fetchone()
            if not setting or not setting[0]:
                return {'saved':False, 'source_ref':source_ref, 'personal_revision':self.check_revision(db, owner, None)}
            revision = self.check_revision(db, owner, None)
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            existing = db.execute('SELECT text_hash FROM conversation_sources WHERE workspace_id=? AND source_ref=?', (owner, source_ref)).fetchone()
            if existing and existing[0] != text_hash:
                raise BankError('SOURCE_REF_CONFLICT', '同一对话来源不能绑定不同原话。', 409)
            db.execute('INSERT OR IGNORE INTO conversation_sources VALUES(?,?,?,?,?,?)',
                       (owner, source_ref, self.protect(text), text_hash, self.clock(), revision))
        return {'saved':True, 'source_ref':source_ref, 'personal_revision':revision}

    def source_statement(self, owner, source_ref):
        with self.connection(owner) as db:
            row = db.execute('SELECT * FROM conversation_sources WHERE workspace_id=? AND source_ref=?', (owner, source_ref)).fetchone()
        if not row:
            raise BankError('INVALID_MEMORY_SOURCE', '对话原话未在当前用户的服务器资料中登记。', 409)
        return dict(row)

    def propose_candidates(self, owner, text, source_ref, *, expected_revision=None):
        registered = self.register_conversation_source(owner, source_ref, text, expected_revision=expected_revision)
        if not registered['saved']:
            return []
        from user_memory import propose_conversation_memories
        proposals = propose_conversation_memories(text, source_ref)
        out = []
        with self.transaction(owner, expected_revision=expected_revision, change=False) as db:
            revision = self.check_revision(db, owner, None)
            source = db.execute('SELECT expected_revision FROM conversation_sources WHERE workspace_id=? AND source_ref=?', (owner, source_ref)).fetchone()
            if not source or source[0] != revision:
                raise BankError('PERSONAL_VERSION_CONFLICT', '对话来源所依据的个人资料版本已变化。', 409)
            for p in proposals:
                node_id = hashlib.sha256((owner+':'+p['proposal_id']).encode()).hexdigest()[:32]
                row = db.execute('SELECT * FROM memory_candidates WHERE workspace_id=? AND proposal_id=?', (owner, node_id)).fetchone()
                if row is None:
                    db.execute('INSERT INTO memory_candidates(workspace_id,proposal_id,category,value,source_ref,confirmation_token,expected_revision,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
                               (owner,node_id,p['category'],self.protect(p['value']),source_ref,uuid.uuid4().hex,revision,self.clock(),self.clock()+86400))
                    row = db.execute('SELECT * FROM memory_candidates WHERE workspace_id=? AND proposal_id=?', (owner,node_id)).fetchone()
                if row['status'] != 'candidate' or row['expected_revision'] != revision:
                    continue
                out.append({'proposal_id':node_id,'category':row['category'],'value':row['value'],
                            'source_type':'conversation_user_statement','source_ref':source_ref,
                            'confirmation_token':row['confirmation_token'],'expected_revision':revision,
                            'notice':'候选来自本人原话，确认后才生效。'})
        return out

    def read_history(self, owner):
        with self.connection(owner) as db:
            row = db.execute('SELECT messages FROM agent_dialogues WHERE workspace_id=?', (owner,)).fetchone()
        return json.loads(row[0]) if row else []

    def save_history(self, owner, messages, *, expected_revision=None):
        clean = self._clean_history(messages)
        try:
            with self.transaction(owner, expected_revision=expected_revision, change=False) as db:
                db.execute('INSERT INTO agent_dialogues VALUES(?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET messages=excluded.messages,updated_at=excluded.updated_at',
                           (owner,self.protect(_json(clean)),self.clock()))
        except BankError as error:
            if error.code == 'PERSONAL_VERSION_CONFLICT':
                return False
            raise
        return True

    @staticmethod
    def _clean_history(messages):
        if not isinstance(messages, list) or len(messages) > 100:
            raise BankError('INVALID_HISTORY', '聊天历史格式无效。')
        # 不接受模型/客户端提供的任意角色结构；身份凭证先过滤再加密。
        clean = []
        for message in messages:
            if not isinstance(message, dict) or message.get('role') not in ('user','assistant') or not isinstance(message.get('content'), str):
                raise BankError('INVALID_HISTORY', '聊天历史格式无效。')
            clean.append({'role':message['role'],'content':redact_sensitive(message['content'])})
        return clean

    def save_turn(self, owner, messages, response_id, widgets, *, expected_revision=None, expected_privacy=None):
        """一次银行锁和私库事务核对版本并保存整轮，拒绝迟到回复复活历史。"""
        clean = self._clean_history(messages)
        if not isinstance(response_id, str) or not isinstance(widgets, list):
            raise BankError('INVALID_HISTORY', '对话组件格式无效。')
        try:
            with self.transaction(owner, expected_revision=expected_revision, expected_privacy=expected_privacy, change=False) as db:
                db.execute('INSERT INTO agent_dialogues VALUES(?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET messages=excluded.messages,updated_at=excluded.updated_at',
                           (owner,self.protect(_json(clean)),self.clock()))
                if widgets:
                    db.execute('INSERT INTO agent_widget_history VALUES(?,?,?,?) ON CONFLICT(workspace_id,response_id) DO UPDATE SET widgets=excluded.widgets,created_at=excluded.created_at',
                               (owner,response_id,self.protect(_json(widgets)),self.clock()))
                # 限量保留可见组件，避免私库无界增长。
                db.execute('DELETE FROM agent_widget_history WHERE workspace_id=? AND response_id NOT IN (SELECT response_id FROM agent_widget_history WHERE workspace_id=? ORDER BY created_at DESC LIMIT 32)', (owner,owner))
        except BankError as error:
            if error.code in {'PERSONAL_VERSION_CONFLICT','PERSONAL_PRIVACY_CONFLICT'}:
                return False
            raise
        return True

    def _guarded_bank_owner(self, owner, bank_db):
        """仅查传入连接；BEGIN IMMEDIATE 写锁由受信调用者持有，不自行重入银行。"""
        if (self.bank is None or not isinstance(bank_db, sqlite3.Connection)
                or not isinstance(owner, str) or not 1 <= len(owner) <= 160):
            return False
        try:
            if not bank_db.in_transaction:
                return False
            main = next((row[2] for row in bank_db.execute('PRAGMA database_list') if row[1] == 'main'), None)
            if not main or Path(main).resolve() != self.bank.database.resolve():
                return False
            binding = bank_db.execute('SELECT database_path FROM personal_data_binding WHERE id=1').fetchone()
            return bool(binding and Path(binding[0]).resolve() == self.database.resolve()
                        and bank_db.execute('SELECT 1 FROM workspaces WHERE id=?', (owner,)).fetchone())
        except (sqlite3.Error, ValueError, OSError):
            return False

    @staticmethod
    def _guarded_row_hash(row, body, stamp):
        return hashlib.sha256(_json({body: row[body], stamp: row[stamp]}).encode()).hexdigest() if row else None

    @staticmethod
    def _raw_dialogue(db, owner):
        # 别名绕过 connect 的正文解密，CAS 比对实际密文而非可碰撞的重保存明文。
        return db.execute('SELECT messages AS messages_cipher,updated_at FROM agent_dialogues WHERE workspace_id=?', (owner,)).fetchone()

    @staticmethod
    def _raw_widget(db, owner, response_id):
        return db.execute('SELECT widgets AS widgets_cipher,created_at FROM agent_widget_history WHERE workspace_id=? AND response_id=?', (owner,response_id)).fetchone()

    def _guarded_versions(self, db, owner, bank_db, expected_revision, expected_privacy):
        from user_memory import privacy_revision
        if (type(expected_revision) is not int or expected_revision < 0
                or type(expected_privacy) is not int or expected_privacy < 0
                or privacy_revision(bank_db, owner) != expected_privacy):
            return False
        row = db.execute('SELECT revision,pending_invalidation FROM personal_versions WHERE workspace_id=?', (owner,)).fetchone()
        if (row[0] if row else 0) != expected_revision or (row and row[1]):
            return False
        return True

    def save_turn_guarded(self, owner, messages, response_id, widgets, *, bank_db,
                          expected_revision, expected_privacy):
        """持既有银行发布锁保存会话；长期记忆开关不控制会话历史。"""
        clean = self._clean_history(messages)
        if (not isinstance(response_id, str) or not 1 <= len(response_id) <= 160
                or not isinstance(widgets, list)):
            raise BankError('INVALID_HISTORY', '对话组件格式无效。')
        if not self._guarded_bank_owner(owner, bank_db):
            return False
        try:
            payload_hash = hashlib.sha256(_json({'messages':clean,'widgets':widgets}).encode()).hexdigest()
        except (TypeError, ValueError):
            raise BankError('INVALID_HISTORY', '对话组件格式无效。') from None
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not self._guarded_versions(db,owner,bank_db,expected_revision,expected_privacy):
                    db.rollback();return False
                dialogue = self._raw_dialogue(db,owner)
                widget = self._raw_widget(db,owner,response_id)
                history_hash = self._guarded_row_hash(dialogue,'messages_cipher','updated_at')
                widget_hash = self._guarded_row_hash(widget,'widgets_cipher','created_at')
                existing = db.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?', (owner,response_id)).fetchone()
                if existing:
                    # bool 接口无外部代次凭据：禁止同 ID 另存一代，旧清理不能误伤新轮。
                    valid = (existing['status']=='saved' and existing['personal_revision']==expected_revision
                             and existing['privacy_revision']==expected_privacy and existing['payload_hash']==payload_hash
                             and existing['history_hash']==history_hash
                             and existing['widget_hash']==widget_hash)
                    db.rollback();return valid
                if widget is not None:
                    # 老入口已占用该响应 ID；不能把它的组件改成当前待发布轮。
                    db.rollback();return False
                previous = db.execute("SELECT save_generation FROM guarded_turn_saves WHERE workspace_id=? AND history_hash=? AND status='saved' ORDER BY rowid DESC LIMIT 1", (owner,history_hash)).fetchone() if dialogue else None
                now = self.clock()
                db.execute('INSERT INTO agent_dialogues VALUES(?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET messages=excluded.messages,updated_at=excluded.updated_at',
                           (owner,self.protect(_json(clean)),now))
                if widgets:
                    db.execute('INSERT INTO agent_widget_history VALUES(?,?,?,?) ON CONFLICT(workspace_id,response_id) DO UPDATE SET widgets=excluded.widgets,created_at=excluded.created_at',
                               (owner,response_id,self.protect(_json(widgets)),now))
                saved_history_hash = self._guarded_row_hash(self._raw_dialogue(db,owner),'messages_cipher','updated_at')
                saved_widget_hash = self._guarded_row_hash(self._raw_widget(db,owner,response_id),'widgets_cipher','created_at') if widgets else None
                db.execute('INSERT INTO guarded_turn_saves VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (owner,response_id,uuid.uuid4().hex,expected_revision,expected_privacy,payload_hash,
                            saved_history_hash,saved_widget_hash,dialogue['messages_cipher'] if dialogue else None,
                            dialogue['updated_at'] if dialogue else None,previous[0] if previous else None,'saved',now))
                # 失败轮不得顺带淘汰其他成功轮的组件或撤回依据。
                db.commit()
            except Exception:
                db.rollback();raise
        self._permissions()
        return True

    def _previous_guarded_history(self, db, owner, saved):
        """跨已撤回的前轮回到其原件；缺失或异常链条时拒绝恢复。"""
        seen = set()
        while saved['previous_generation']:
            generation = saved['previous_generation']
            if generation in seen:
                return None
            seen.add(generation)
            previous = db.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND save_generation=?', (owner,generation)).fetchone()
            snapshot = {'messages_cipher':saved['previous_messages'],'updated_at':saved['previous_updated_at']}
            if (not previous or previous['personal_revision']!=saved['personal_revision']
                    or previous['privacy_revision']!=saved['privacy_revision']
                    or previous['history_hash']!=self._guarded_row_hash(snapshot,'messages_cipher','updated_at')):
                return None
            if previous['status']=='saved':
                break
            if previous['status'] not in ('discarded','discarded_partial'):
                return None
            saved = previous
        return {'messages_cipher':saved['previous_messages'],'updated_at':saved['previous_updated_at']}

    def discard_turn_guarded(self, owner, response_id, *, bank_db,
                             expected_revision, expected_privacy):
        """只补偿匹配的本轮原件；后续历史、组件和用户修改不会被覆盖。"""
        if (not isinstance(response_id,str) or not 1 <= len(response_id) <= 160
                or not self._guarded_bank_owner(owner,bank_db)):
            return False
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not self._guarded_versions(db,owner,bank_db,expected_revision,expected_privacy):
                    db.rollback();return False
                saved = db.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?', (owner,response_id)).fetchone()
                if (not saved or saved['personal_revision']!=expected_revision or saved['privacy_revision']!=expected_privacy):
                    db.rollback();return False
                if saved['status'] in ('discarded','discarded_partial'):
                    db.rollback();return saved['status']=='discarded'
                if saved['status']!='saved':
                    db.rollback();return False
                dialogue = self._raw_dialogue(db,owner)
                history_matches = self._guarded_row_hash(dialogue,'messages_cipher','updated_at')==saved['history_hash']
                previous = self._previous_guarded_history(db,owner,saved) if history_matches else None
                restored = history_matches and previous is not None
                if restored:
                    if previous['messages_cipher'] is None:
                        db.execute('DELETE FROM agent_dialogues WHERE workspace_id=? AND messages=? AND updated_at=?',
                                   (owner,dialogue['messages_cipher'],dialogue['updated_at']))
                    else:
                        db.execute('UPDATE agent_dialogues SET messages=?,updated_at=? WHERE workspace_id=? AND messages=? AND updated_at=?',
                                   (previous['messages_cipher'],previous['updated_at'],owner,dialogue['messages_cipher'],dialogue['updated_at']))
                widget = self._raw_widget(db,owner,response_id)
                widgets_match = (saved['widget_hash'] is None or widget is None
                                 or self._guarded_row_hash(widget,'widgets_cipher','created_at')==saved['widget_hash'])
                if saved['widget_hash'] is not None and widget is not None and widgets_match:
                    db.execute('DELETE FROM agent_widget_history WHERE workspace_id=? AND response_id=? AND widgets=? AND created_at=?',
                               (owner,response_id,widget['widgets_cipher'],widget['created_at']))
                complete = bool(restored and widgets_match)
                db.execute("UPDATE guarded_turn_saves SET status=? WHERE workspace_id=? AND response_id=? AND save_generation=? AND status='saved'",
                           ('discarded' if complete else 'discarded_partial',owner,response_id,saved['save_generation']))
                db.commit()
            except Exception:
                db.rollback();raise
        self._permissions()
        return bool(restored and widgets_match)

    def read_widgets(self, owner, limit=3):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BankError('INVALID_HISTORY', '历史组件数量无效。')
        with self.connection(owner) as db:
            rows = db.execute('SELECT response_id,widgets,created_at FROM agent_widget_history WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?', (owner,limit)).fetchall()
        return [{'response_id':r['response_id'],'widgets':json.loads(r['widgets']),'created_at':r['created_at']} for r in rows]

    def save_widgets(self, owner, response_id, widgets, *, expected_revision=None):
        if not isinstance(response_id, str) or not isinstance(widgets,list):
            raise BankError('INVALID_HISTORY', '历史组件格式无效。')
        try:
            with self.transaction(owner, expected_revision=expected_revision, change=False) as db:
                db.execute('INSERT INTO agent_widget_history VALUES(?,?,?,?) ON CONFLICT(workspace_id,response_id) DO UPDATE SET widgets=excluded.widgets,created_at=excluded.created_at',
                           (owner,response_id,self.protect(_json(widgets)),self.clock()))
        except BankError as error:
            if error.code == 'PERSONAL_VERSION_CONFLICT':
                return False
            raise
        return True

    def delete_history(self, owner, *, expected_revision=None):
        with self.transaction(owner, expected_revision=expected_revision, event='history_deleted') as db:
            # 删除原话也撤回以它为依据的候选；已确认资料保留其非正文来源 ID。
            db.execute('DELETE FROM conversation_sources WHERE workspace_id=?', (owner,))
            db.execute("DELETE FROM profile_observations WHERE workspace_id=? AND status!='confirmed'", (owner,))
        return {'deleted':True,'personal_revision':self.revision(owner)}

    def rows(self, owner, table):
        if table not in PRIVATE_TABLES:
            raise BankError('INVALID_PERSONAL_TABLE', '个人资料类型无效。')
        with self.connection(owner) as db:
            return [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE workspace_id=?', (owner,))]


def personal_store(bank):
    store = getattr(bank, 'personal_store', None)
    return store if store is not None else PersonalDataStore(bank)
