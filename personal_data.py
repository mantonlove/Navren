"""独立的、按服务端工作区隔离的个人资料库；银行库只保留失效标记。

敏感正文用 Fernet 保存。密钥来自环境或 macOS Keychain，不生成 keyfile。
跨库变更先持有银行写锁；私库提交后的未完成失效操作可以在重启时恢复。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from simulated_bank.service import BankError


@dataclass(frozen=True)
class PersonalCommitReceipt:
    """仅在本次私库及银行失效事务都成功提交后交给内部调用方。"""

    store: object
    owner: str
    personal_before: int
    personal_after: int
    privacy_before: int
    privacy_after: int


@dataclass
class _PersonalResponseScope:
    owner: str
    expected_privacy: int
    receipts: list
    active: bool = True


LEGACY_TABLES = ('user_memory_settings', 'user_memories', 'user_memory_relations',
                 'profile_observations', 'agent_dialogues', 'agent_widget_history')
PRIVATE_TABLES = frozenset(LEGACY_TABLES + ('memory_candidates', 'conversation_sources'))

# 宿主从已核验 resolver 构造只读快照；不接受浏览器标签、票据或原 payload。
ARCHIVE_USER_CONTRACT = 'navren.archived-user-turn/2'
ARCHIVE_REFERENCE_LABELS = {
    'event': '市场事件', 'article': '研究文章', 'weekly': '市场周报', 'graph': '财务关系图',
    'calendar_item': '日历事项', 'calendar': '财务日历', 'product': '金融产品',
    'goal': '财务目标', 'research_plan': '研究计划', 'operation': '历史操作', 'report': '账单报告',
    'account': '账户', 'card': '银行卡', 'holding': '持仓', 'loan': '贷款', 'deposit': '存款',
    'subscription': '订阅', 'mandate': '扣款授权', 'task': '任务', 'plan': '业务计划',
    'support': '服务工单', 'permission': '权限记录', 'scheduled_payment': '计划付款',
    'split_bill': '分账账单',
}
ARCHIVE_REFERENCE_PARAMETERS = {kind: frozenset() for kind in ARCHIVE_REFERENCE_LABELS}
ARCHIVE_REFERENCE_PARAMETERS.update({
    'event': frozenset({'edition_date', 'revision', 'event_version'}),
    'article': frozenset({'reading_mode'}), 'weekly': frozenset({'reading_mode'}),
    'graph': frozenset({'node_id', 'edge_id', 'insight_id', 'view', 'from', 'to', 'source',
                        'portfolio', 'accounting_basis', 'expected_version'}),
    'calendar': frozenset({'from', 'to', 'view', 'source', 'portfolio', 'accounting_basis',
                           'kinds', 'include_life_events', 'simulation_overlay', 'period'}),
    'report': frozenset({'month', 'year'}),
})
_ARCHIVE_RETAIN_EVENTS = frozenset({'memory_created', 'memory_candidate_confirmed',
                                   'profile_confirm', 'memory_enabled', 'memory_relation_created',
                                   'memory_corrected', 'memory_deleted', 'memory_disabled',
                                   'memory_relation_deleted'})
_ARCHIVE_REFERENCE_FIELDS = frozenset({'contract', 'kind', 'id', 'source_scope', 'version', 'label',
                                       'parameters', 'historical_readonly', 'current_fact_eligible',
                                       'execution_authority'})
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
CREATE TABLE IF NOT EXISTS dialogue_catalog_source_registry(
 source_id TEXT PRIMARY KEY,record_cipher TEXT NOT NULL,state_cipher TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
 withdrawn_generation INTEGER NOT NULL CHECK(withdrawn_generation>=0),
 CHECK(substr(record_cipher,1,4)='pd1:'),CHECK(substr(state_cipher,1,4)='pd1:'));
CREATE TABLE IF NOT EXISTS agent_dialogue_attestations(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,attestation_cipher TEXT NOT NULL,
 created_at REAL NOT NULL,PRIMARY KEY(workspace_id,response_id),
 CHECK(substr(attestation_cipher,1,4)='pd1:'));
CREATE TABLE IF NOT EXISTS dialogue_archive_epochs(
 workspace_id TEXT PRIMARY KEY,archive_epoch INTEGER NOT NULL DEFAULT 0 CHECK(archive_epoch>=0));
CREATE TABLE IF NOT EXISTS agent_user_turn_archive(
 workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,archive_sequence INTEGER NOT NULL,
 archive_epoch INTEGER NOT NULL CHECK(archive_epoch>=0),
 user_text_cipher TEXT NOT NULL,personal_revision INTEGER NOT NULL,privacy_revision INTEGER NOT NULL,
 received_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,request_id),UNIQUE(workspace_id,archive_sequence),
 CHECK(archive_sequence>=1),CHECK(personal_revision>=0),CHECK(privacy_revision>=0),
 CHECK(substr(user_text_cipher,1,4)='pd1:'));
CREATE TABLE IF NOT EXISTS agent_conversation_membership(
 workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
 archive_sequence INTEGER NOT NULL,archive_epoch INTEGER NOT NULL,binding_cipher TEXT NOT NULL,
 PRIMARY KEY(workspace_id,request_id),
 FOREIGN KEY(workspace_id,request_id) REFERENCES agent_user_turn_archive(workspace_id,request_id) ON DELETE CASCADE,
 CHECK(archive_sequence>=1),CHECK(archive_epoch>=0),CHECK(length(conversation_id)=32),
 CHECK(substr(binding_cipher,1,4)='pd1:'));
CREATE INDEX IF NOT EXISTS conversation_membership_lookup
 ON agent_conversation_membership(workspace_id,conversation_id,archive_sequence);
CREATE TABLE IF NOT EXISTS agent_widget_history(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,widgets TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,response_id));
CREATE TABLE IF NOT EXISTS guarded_turn_saves(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,save_generation TEXT NOT NULL UNIQUE,
 personal_revision INTEGER NOT NULL,privacy_revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,
 history_hash TEXT NOT NULL,widget_hash TEXT,previous_messages TEXT,previous_updated_at REAL,
 previous_generation TEXT,status TEXT NOT NULL,saved_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,response_id));
CREATE TABLE IF NOT EXISTS guarded_turn_publications(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,save_generation TEXT NOT NULL,
 widget_hash TEXT NOT NULL,publication_widget TEXT NOT NULL,
 PRIMARY KEY(workspace_id,response_id),
 FOREIGN KEY(workspace_id,response_id) REFERENCES guarded_turn_saves(workspace_id,response_id) ON DELETE CASCADE);
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
    return re.sub(r'(?i)((?:密码|验证码|口令|身份证号?|银行卡(?:号|(?=\s*[:：=])|(?=\s*[0-9*＊xX]))|手机号?|token|password|api[_ -]?key)\s*[:：=]?\s*)[^，。；\s,;]{1,100}', r'\1[已隐藏]', text)


def explicit_self_statement(text):
    """保守的本地准入：排除假设、第三人转述、疑问与推测。"""
    if not isinstance(text, str) or not text.strip():
        return False
    if re.search(r'假设|假如|如果|假定|举例|例如|比方|可能|也许|大概|猜测|听说|据说|设想|要是|[?？“”「」]|(?:朋友|同学|同事|老师|导师|父母|父亲|母亲|爸爸|妈妈|家人|配偶|妻子|丈夫|孩子|他|她|别人).{0,16}(?:说|想|准备|计划|打算|需要|喜欢|希望|目标|偏好)', text):
        return False
    return bool(re.match(r'\s*(?:(?:明年|今年|下个月|下月|未来\d{1,2}个月|\d{1,2}月)\s*)?(?:我(?:的|们)?|本人|以后请|今后请|请长期)', text))


_MESSAGE_STATE_FIELDS = frozenset('''schema_version message_id request_id workspace_binding language
 approved_content content_amount_claims surface_bindings projection_hash view_refs source_refs followups personal_revision privacy_revision answer_context'''.split())
_CONTENT_CLAIM_FIELDS = frozenset('''metric_id subject_id metric_name currency unit value_minor as_of
 period_start period_end data_scope calculation_status basis formula_id source_versions text claim_kind'''.split())
_MESSAGE_HTML = re.compile(r'<(?:/?[A-Za-z][A-Za-z0-9:-]*(?=[\s/>])|!--|![A-Za-z]|\?)')


def _message_state_error():
    raise BankError('MESSAGE_STATE_INVALID', '消息展示状态格式或绑定无效。', 422)


def _message_state_json(value):
    """只复制有限 JSON 原件；普通比较表达式的小于号不作为 HTML。"""
    def check(item, depth=0):
        if depth > 8:
            _message_state_error()
        if type(item) is str:
            if len(item) > 32000 or _MESSAGE_HTML.search(item):
                _message_state_error()
        elif type(item) is dict:
            if len(item) > 128:
                _message_state_error()
            for key, child in item.items():
                if type(key) is not str or not key or len(key) > 256:
                    _message_state_error()
                check(key, depth + 1);check(child, depth + 1)
        elif type(item) is list:
            if len(item) > 128:
                _message_state_error()
            for child in item:check(child, depth + 1)
        elif item is not None and type(item) not in (int, bool):
            _message_state_error()
    check(value)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(body.encode()) > 262144:
        _message_state_error()
    return body


def _history_message_state(owner, response_id, state, content, revision, privacy):
    """仅核对历史结构；来源的新鲜度由受信发行/刷新接口分别核验。"""
    if (type(state) is not dict
            or set(state) not in (_MESSAGE_STATE_FIELDS, _MESSAGE_STATE_FIELDS - {'surface_bindings'})):
        _message_state_error()
    state = json.loads(_message_state_json(state))
    # 已保存的旧原件没有附加显示位置，兼容为空；其它未知字段仍封口。
    state.setdefault('surface_bindings', [])
    def string(value, limit=160):
        return type(value) is str and bool(value.strip()) and len(value) <= limit
    def digest(value):
        return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None
    if (type(state['schema_version']) is not int or state['schema_version'] != 1
            or state['message_id'] != response_id or state['request_id'] != response_id
            or not string(state['message_id']) or not string(state['request_id'])
            or state['workspace_binding'] != owner or not string(state['workspace_binding'])
            or state['language'] not in ('zh', 'en')
            or type(state['approved_content']) is not str or state['approved_content'] != content
            or not digest(state['projection_hash']) or not digest(state['answer_context'])
            or type(state['personal_revision']) is not int or state['personal_revision'] < 0
            or state['personal_revision'] != revision
            or type(state['privacy_revision']) is not int or state['privacy_revision'] < 0
            or state['privacy_revision'] != privacy):
        _message_state_error()
    views, sources, followups = state['view_refs'], state['source_refs'], state['followups']
    if (type(views) is not list or len(views) != 1 or type(sources) is not list or len(sources) != 1
            or type(followups) is not list or len(followups) > 2):
        _message_state_error()
    claims = state['content_amount_claims']
    if type(claims) is not list or len(claims) > 24:
        _message_state_error()
    for claim in claims:
        if (type(claim) is not dict or not _CONTENT_CLAIM_FIELDS <= set(claim)
                or set(claim) - (_CONTENT_CLAIM_FIELDS | {'occurrence_start'})):
            _message_state_error()
        # 完整字段/安全 JSON 在本层封口；金额、每个出现位置和类型由可信回调回查原 registry。
    surfaces = state['surface_bindings']
    if type(surfaces) is not list or len(surfaces) > 24:
        _message_state_error()
    surface_paths = set()
    for surface in surfaces:
        if (type(surface) is not dict or set(surface) != {'path','metric_id'}
                or not string(surface['path'],512) or not string(surface['metric_id'])
                or surface['path'] in surface_paths):
            _message_state_error()
        # 每个显示位置独立保留；同额/同指标不去重，也不接收调用者提供的金额。
        surface_paths.add(surface['path'])
    view_ids = set()
    for view in views:
        if (type(view) is not dict or set(view) != {'view_id','projection_hash','metric_snapshot_id'}
                or not string(view['view_id']) or not digest(view['projection_hash'])
                or not string(view['metric_snapshot_id'])):
            _message_state_error()
        view_ids.add(view['view_id'])
    source_ids = set()
    for source in sources:
        if (type(source) is not dict or set(source) != {'view_id','source_scope','source_versions','range',
                                                     'currency','portfolio','accounting_basis'}
                or not string(source['view_id']) or source['view_id'] not in view_ids
                or type(source['source_scope']) is not dict or not source['source_scope']
                or type(source['source_versions']) is not dict or not source['source_versions']
                or any(not string(k,256) or not string(v,256) for k,v in source['source_versions'].items())
                or type(source['range']) is not dict or set(source['range']) != {'from','to'}
                or type(source['currency']) is not str or re.fullmatch(r'[A-Z]{3}',source['currency']) is None
                or not string(source['portfolio'])
                or not string(source['accounting_basis'],256)):
            _message_state_error()
        try:
            start, end = (date.fromisoformat(source['range'][key]) for key in ('from','to'))
            if (start.isoformat() != source['range']['from'] or end.isoformat() != source['range']['to']
                    or start > end):
                _message_state_error()
        except (TypeError, ValueError):
            _message_state_error()
        source_ids.add(source['view_id'])
    if source_ids != view_ids:
        _message_state_error()
    seen = set()
    for item in followups:
        if (type(item) is not dict or set(item) != {'schema_version','id','label','text','type','intent',
                                                   'context_ref','availability'}
                or type(item['schema_version']) is not int or item['schema_version'] != 1
                or not string(item['id']) or item['id'] in seen or not string(item['label'],80)
                or not string(item['text'],6000)
                or (item['type'],item['intent']) not in (('send_message','ask'),('open_view','open_view'))
                or type(item['context_ref']) is not dict or set(item['context_ref']) != {'view_id'}
                or not string(item['context_ref']['view_id']) or item['context_ref']['view_id'] not in view_ids
                or item['availability'] != 'available'):
            _message_state_error()
        seen.add(item['id'])
    return state


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
    return re.sub(r'(?i)((?:密码|验证码|口令|身份证号?|银行卡(?:号|(?=\s*[:：=])|(?=\s*[0-9*＊xX]))|手机号?|token|password|api[_ -]?key)\s*[:：=]?\s*)[^，。；\s,;]{1,100}', r'\1[已隐藏]', text)


def explicit_self_statement(text):
    """保守的本地准入：排除假设、第三人转述、疑问与推测。"""
    if not isinstance(text, str) or not text.strip():
        return False
    if re.search(r'假设|假如|如果|假定|举例|例如|比方|可能|也许|大概|猜测|听说|据说|设想|要是|[?？“”「」]|(?:朋友|同学|同事|老师|导师|父母|父亲|母亲|爸爸|妈妈|家人|配偶|妻子|丈夫|孩子|他|她|别人).{0,16}(?:说|想|准备|计划|打算|需要|喜欢|希望|目标|偏好)', text):
        return False
    return bool(re.match(r'\s*(?:(?:明年|今年|下个月|下月|未来\d{1,2}个月|\d{1,2}月)\s*)?(?:我(?:的|们)?|本人|以后请|今后请|请长期)', text))


class PersonalDataStore:
    def __init__(self, bank_or_path, *, encryption_key=None, clock=None):
        self._response_commit_scope = ContextVar('navren_personal_response_commit', default=None)
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

    def committed_response(self, owner, expected_privacy, mutation, *args, **kwargs):
        """对既有个人变更使用一次请求内的真实提交回执，不重读最新隐私来放行。"""
        if self._response_commit_scope.get() is not None:
            raise RuntimeError('personal_response_mutation_must_not_nest')
        if type(expected_privacy) is not int or expected_privacy < 0:
            raise BankError('PERSONAL_PRIVACY_CONFLICT', '资料授权版本无效，请重新读取。', 409)
        scope = _PersonalResponseScope(owner, expected_privacy, [])
        binding = self._response_commit_scope.set(scope)
        try:
            result = mutation(*args, **kwargs)
        finally:
            scope.active = False
            self._response_commit_scope.reset(binding)
        receipt = scope.receipts[0] if len(scope.receipts) == 1 else None
        if (type(receipt) is not PersonalCommitReceipt or receipt.store is not self
                or receipt.owner != owner or receipt.privacy_before != expected_privacy
                or receipt.privacy_after != expected_privacy + 1
                or receipt.personal_after != receipt.personal_before + 1
                or not isinstance(result, dict)
                or result.get('personal_revision') != receipt.personal_after):
            raise BankError('PRIVACY_REVISION_CHANGED', '个人变更回执与本次提交版本不一致，请重新核对。', 409)
        return result, receipt

    @contextmanager
    def transaction(self, owner, *, expected_revision=None, expected_privacy=None,
                    change=True, event='personal_changed', object_id=None, commit_receipts=None):
        scope = self._response_commit_scope.get()
        if scope is not None:
            if not scope.active or scope.owner != owner:
                raise BankError('WORKSPACE_CHANGED', '个人变更不属于本次账户请求。', 409)
            if expected_privacy is not None and expected_privacy != scope.expected_privacy:
                raise BankError('PERSONAL_PRIVACY_CONFLICT', '资料授权已变化，请重新读取。', 409)
            expected_privacy = scope.expected_privacy
            if change:
                if scope.receipts:
                    raise BankError('PRIVACY_REVISION_CHANGED', '本次个人变更已提交，不能再采用另一笔变更回执。', 409)
                if commit_receipts is not None and commit_receipts is not scope.receipts:
                    raise RuntimeError('personal_response_receipt_collector_mismatch')
                commit_receipts = scope.receipts
        if commit_receipts is not None and (type(commit_receipts) is not list or commit_receipts
                                           or not change or self.bank is None):
            raise ValueError('commit_receipts requires an empty internal collector and a bank-backed mutation')
        receipt = None
        self.validate_owner(owner)
        # 银行确认、图谱计算及个人变更使用同一写锁序列，避免跨库权限竞态。
        with (self.bank.transaction() if self.bank else nullcontext(None)) as bank_db:
            if commit_receipts is not None:
                from user_memory import privacy_revision
                privacy_before = privacy_revision(bank_db, owner)
            if expected_privacy is not None and bank_db is not None:
                from user_memory import privacy_revision
                if type(expected_privacy) is not int or privacy_revision(bank_db, owner) != expected_privacy:
                    raise BankError('PERSONAL_PRIVACY_CONFLICT', '资料权限已变化，旧对话不能保存。', 409)
            db = self.connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                revision = self.check_revision(db, owner, expected_revision)
                personal_before = revision
                db.execute('INSERT OR IGNORE INTO personal_versions(workspace_id) VALUES(?)', (owner,))
                yield db
                if change:
                    revision += 1
                    db.execute('UPDATE personal_versions SET revision=?,pending_invalidation=1 WHERE workspace_id=?', (revision, owner))
                    # 所有资料变更继续使模型近期上下文失效；历史展示有独立撤回代次。
                    db.execute('DELETE FROM agent_dialogues WHERE workspace_id=?', (owner,))
                    registered_archive = db.execute('SELECT 1 FROM dialogue_archive_epochs WHERE workspace_id=?', (owner,)).fetchone()
                    if event not in _ARCHIVE_RETAIN_EVENTS or not registered_archive:
                        # 显式删除聊天、整份个人数据撤回及未知变更才撤回原件；
                        # 单条记忆更正/删除/关闭读取仅撤销近期上下文及依赖授权。
                        for table in ('agent_widget_history', 'agent_user_turn_archive', 'guarded_turn_saves',
                                      'agent_dialogue_attestations'):
                            db.execute('DELETE FROM '+table+' WHERE workspace_id=?', (owner,))
                        # 原件与归档撤回处于同一私库事务；普通资料新增走保留分支。
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_media_objects'").fetchone():
                            db.execute('DELETE FROM chat_media_objects WHERE workspace_id=?',(owner,))
                        if registered_archive:
                            db.execute('UPDATE dialogue_archive_epochs SET archive_epoch=archive_epoch+1 WHERE workspace_id=?', (owner,))
                    db.execute("UPDATE memory_candidates SET status='stale' WHERE workspace_id=? AND status='candidate'", (owner,))
                    db.execute('INSERT INTO personal_audit(workspace_id,event,object_id,revision,created_at) VALUES(?,?,?,?,?)',
                               (owner, event, object_id, revision, self.clock()))
                db.commit()
                if change and bank_db is not None:
                    self._invalidate_bank(bank_db, owner, revision)
                    if commit_receipts is not None:
                        receipt = PersonalCommitReceipt(self, owner, personal_before, revision,
                                                        privacy_before, privacy_revision(bank_db, owner))
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
        if receipt is not None:
            commit_receipts.append(receipt)

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

    def _guarded_catalog_bank(self, bank_db):
        """全局公开目录registry仍只接受当前绑定银行的既有事务连接。"""
        if self.bank is None or not isinstance(bank_db, sqlite3.Connection) or not bank_db.in_transaction:
            return False
        try:
            main = next((row[2] for row in bank_db.execute('PRAGMA database_list') if row[1] == 'main'), None)
            binding = bank_db.execute('SELECT database_path FROM personal_data_binding WHERE id=1').fetchone()
            return bool(main and Path(main).resolve() == self.bank.database.resolve() and binding
                        and Path(binding[0]).resolve() == self.database.resolve())
        except (sqlite3.Error, ValueError, OSError):
            return False

    @staticmethod
    def _catalog_archive_record(source_id, record):
        from local_knowledge.catalog_information import validate_source, digest
        required = {'contract', 'source_id', 'source_kind', 'original_source', 'original_as_of',
                    'full_package_sha256', 'original_package'}
        if (type(source_id) is not str or re.fullmatch(r'[0-9a-f]{64}', source_id) is None
                or type(record) is not dict or set(record) != required
                or record['contract'] != 'navren.archived-catalog-source/1'
                or record['source_id'] != source_id
                or record['source_kind'] not in ('historical', 'synthetic')
                or type(record['full_package_sha256']) is not str
                or re.fullmatch(r'[0-9a-f]{64}', record['full_package_sha256']) is None):
            raise BankError('CATALOG_ARCHIVE_INVALID', '目录归档来源结构或标识无效。', 409)
        original = validate_source(record['original_package'], record['source_kind'])
        identity = {'contract': 'navren.archived-catalog-source-id/1',
                    'source_kind': record['source_kind'], 'original_source': record['original_source'],
                    'original_as_of': record['original_as_of'], 'full_package_sha256': record['full_package_sha256']}
        if (type(record['original_source']) is not str or type(record['original_as_of']) is not str
                or record['original_source'] != original['source'] or record['original_as_of'] != original['as_of']
                or digest(original) != record['full_package_sha256'] or digest(identity) != source_id):
            raise BankError('CATALOG_ARCHIVE_INVALID', '目录归档原包与来源摘要不一致。', 409)
        serialized = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(serialized.encode()) > 65536:
            raise BankError('CATALOG_ARCHIVE_INVALID', '目录归档来源超出允许大小。', 409)
        return json.loads(serialized)

    def _catalog_registry_entry(self, db, source_id):
        row = db.execute('SELECT * FROM dialogue_catalog_source_registry WHERE source_id=?', (source_id,)).fetchone()
        if row is None:
            return None
        try:
            if any(type(row[k]) is not str or not row[k].startswith('pd1:') for k in ('record_cipher', 'state_cipher')):
                raise ValueError('unsealed_registry')
            record = self._catalog_archive_record(source_id, json.loads(self._decode(row['record_cipher'])))
            state = json.loads(self._decode(row['state_cipher']))
            expected = {'contract': 'navren.archived-catalog-source-state/1', 'source_id': source_id,
                        'full_package_sha256': record['full_package_sha256'], 'status': row['status'],
                        'withdrawn_generation': row['withdrawn_generation']}
            if (type(state) is not dict or set(state) != set(expected) or _json(state) != _json(expected)
                    or type(row['withdrawn_generation']) is not int
                    or (row['status'], row['withdrawn_generation']) not in (('active', 0), ('withdrawn', 1))):
                raise ValueError('registry_state_changed')
        except (BankError, TypeError, ValueError):
            raise BankError('CATALOG_ARCHIVE_CHANGED', '目录归档原件或撤回状态已变化。', 409) from None
        return {'source_id': source_id, 'record': record, 'status': row['status'],
                'withdrawn_generation': row['withdrawn_generation']}

    def register_catalog_source_guarded(self, source_id, record, *, bank_db):
        if not self._guarded_catalog_bank(bank_db):
            raise BankError('CATALOG_ARCHIVE_BINDING_REQUIRED', '目录归档需要当前银行事务绑定。', 403)
        record = self._catalog_archive_record(source_id, record)
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                existing = self._catalog_registry_entry(db, source_id)
                if existing is not None:
                    if _json(existing['record']) != _json(record):
                        raise BankError('CATALOG_ARCHIVE_CONFLICT', '同一目录来源不能覆盖原始归档。', 409)
                    db.rollback();return existing  # withdrawn仍返回撤回状态，禁止重新发行。
                state = {'contract': 'navren.archived-catalog-source-state/1', 'source_id': source_id,
                         'full_package_sha256': record['full_package_sha256'], 'status': 'active',
                         'withdrawn_generation': 0}
                db.execute('INSERT INTO dialogue_catalog_source_registry VALUES(?,?,?,?,?)',
                           (source_id, self.protect(_json(record)), self.protect(_json(state)), 'active', 0))
                db.commit()
            except Exception:
                db.rollback();raise
        self._permissions()
        return {'source_id': source_id, 'record': record, 'status': 'active', 'withdrawn_generation': 0}

    def read_catalog_source_guarded(self, source_id, *, bank_db):
        if not self._guarded_catalog_bank(bank_db):
            raise BankError('CATALOG_ARCHIVE_BINDING_REQUIRED', '目录归档需要当前银行事务绑定。', 403)
        if type(source_id) is not str or re.fullmatch(r'[0-9a-f]{64}', source_id) is None:
            raise BankError('CATALOG_ARCHIVE_INVALID', '目录归档来源标识无效。', 422)
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            return self._catalog_registry_entry(db, source_id)

    def withdraw_catalog_source_guarded(self, source_id, *, bank_db):
        if not self._guarded_catalog_bank(bank_db):
            raise BankError('CATALOG_ARCHIVE_BINDING_REQUIRED', '目录归档需要当前银行事务绑定。', 403)
        if type(source_id) is not str or re.fullmatch(r'[0-9a-f]{64}', source_id) is None:
            raise BankError('CATALOG_ARCHIVE_INVALID', '目录归档来源标识无效。', 422)
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                existing = self._catalog_registry_entry(db, source_id)
                if existing is None or existing['status'] == 'withdrawn':
                    db.rollback();return existing
                state = {'contract': 'navren.archived-catalog-source-state/1', 'source_id': source_id,
                         'full_package_sha256': existing['record']['full_package_sha256'],
                         'status': 'withdrawn', 'withdrawn_generation': 1}
                changed = db.execute("UPDATE dialogue_catalog_source_registry SET status='withdrawn',withdrawn_generation=1,state_cipher=? "
                                     "WHERE source_id=? AND status='active' AND withdrawn_generation=0",
                                     (self.protect(_json(state)), source_id)).rowcount
                if changed != 1:
                    raise BankError('CATALOG_ARCHIVE_CHANGED', '目录撤回状态已变化。', 409)
                db.commit()
            except Exception:
                db.rollback();raise
        self._permissions()
        return {**existing, 'status': 'withdrawn', 'withdrawn_generation': 1}

    @staticmethod
    def archive_epoch_from_db(db, owner):
        """只读既有私连接；调用者先持银行锁并验证真实工作区归属。"""
        row = db.execute('SELECT archive_epoch FROM dialogue_archive_epochs WHERE workspace_id=?', (owner,)).fetchone()
        epoch = row[0] if row else 0
        if type(epoch) is not int or epoch < 0:
            raise BankError('DIALOGUE_ARCHIVE_EPOCH_INVALID', '聊天归档撤回代次无效。', 409)
        return epoch

    def archive_epoch(self, owner):
        with self.connection(owner) as db:
            return self.archive_epoch_from_db(db, owner)

    @staticmethod
    def _archive_reference_metadata(references, attachments):
        """只复制有限、只读、已由宿主核验的引用快照；不接受票据或附件正文。"""
        def invalid():
            raise BankError('INVALID_ARCHIVE_REFERENCE', '聊天归档只接受受信的只读资料快照。', 422)
        def text(value, limit):
            return (type(value) is str and bool(value.strip()) and len(value) <= limit
                    and not _MESSAGE_HTML.search(value)
                    and not any(ord(c) < 32 for c in value)
                    and redact_sensitive(value) == value)
        def version(value):
            return (value is None or type(value) is int and 0 <= value <= 9223372036854775807
                    or text(value, 256))
        references = [] if references is None else references
        attachments = [] if attachments is None else attachments
        if type(references) is not list or len(references) > 3:
            invalid()
        clean = []
        for ref in references:
            if type(ref) is not dict or set(ref) != _ARCHIVE_REFERENCE_FIELDS:
                invalid()
            kind, ident, params = ref['kind'], ref['id'], ref['parameters']
            if (type(kind) is not str or kind not in ARCHIVE_REFERENCE_LABELS
                    or not text(ident, 128) or not text(ref['source_scope'], 128)
                    or re.fullmatch(r'[A-Za-z0-9_.:-]+', ref['source_scope']) is None
                    or not version(ref['version'])
                    or ref['contract'] != 'navren.archived-reference/1'
                    or ref['label'] != ARCHIVE_REFERENCE_LABELS[kind] + ' · ' + ident
                    or ref['historical_readonly'] is not True
                    or ref['current_fact_eligible'] is not False or ref['execution_authority'] is not False
                    or type(params) is not dict or set(params) - ARCHIVE_REFERENCE_PARAMETERS[kind]):
                invalid()
            for key, value in params.items():
                if key in ('include_life_events', 'simulation_overlay'):
                    if type(value) is not bool: invalid()
                elif key == 'year':
                    if type(value) is not int or not 2025 <= value <= 2100: invalid()
                elif key == 'revision':
                    if type(value) is not int or not 1 <= value <= 999999999: invalid()
                elif key in ('event_version', 'expected_version'):
                    if value is None or not version(value): invalid()
                elif key == 'kinds':
                    if type(value) is not list or len(value) > 16 or any(not text(v, 64) for v in value): invalid()
                elif key == 'reading_mode':
                    if value != 'historical_audit': invalid()
                elif not text(value, 500 if key in ('node_id', 'edge_id', 'insight_id') else 160):
                    invalid()
            # JSON 复制与长度封口；无 HTML、token、return_view、rawpayload 或任意嵌套。
            serialized = json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(serialized.encode()) > 4096: invalid()
            clean.append(json.loads(serialized))
        if type(attachments) is not list or len(attachments)>8:invalid()
        clean_attachments=[]
        from base_runtime.chat_media import validate_descriptor
        for attachment in attachments:
            if attachment=={'kind':'image','storage_status':'not_archived'}:
                clean_attachments.append(dict(attachment))
            else:
                value=validate_descriptor(attachment)
                if value['storage_status']!='bound':invalid()
                clean_attachments.append(value)
        return clean,clean_attachments

    def archive_user_turn_guarded(self, owner, request_id, text, registered_user_id, *, bank_db,
                                  expected_revision, expected_privacy, references=None, attachments=None,media_ids=None,
                                  conversation_scope_validator=None):
        """只存受理原话/只读引用；普通资料更新保留原件，撤回按独立epoch失效。"""
        if not self._guarded_bank_owner(owner, bank_db):
            return False
        if (type(request_id) is not str or not request_id.strip() or len(request_id) > 160
                or type(registered_user_id) is not str
                or re.fullmatch(r'[0-9a-f]{32}', registered_user_id) is None):
            raise BankError('INVALID_HISTORY', '注册聊天归档标识无效。', 422)
        registered = uuid.UUID(hex=registered_user_id)
        if (registered.hex != registered_user_id or registered.version != 4
                or registered.variant != uuid.RFC_4122):
            raise BankError('INVALID_HISTORY', '注册聊天归档身份无效。', 422)
        refs, attachment_metadata = self._archive_reference_metadata(references, attachments)
        # 描述符只能由当前私库媒体行物化；客户端自报描述符不能取得绑定资格。
        if any(a.get('contract')=='navren.chat-media/1' for a in attachment_metadata):
            raise BankError('CHAT_MEDIA_IDS_REQUIRED','媒体须传已上传ID，不能自报描述符。',422)
        if media_ids is not None:
            from base_runtime.chat_media import ident,MAX_PER_TURN
            if type(media_ids) is not list or len(media_ids)>MAX_PER_TURN or any(not ident(x) for x in media_ids) or len(set(media_ids))!=len(media_ids):
                raise BankError('CHAT_MEDIA_IDS_INVALID','媒体ID列表无效。',422)
        if type(text) is not str or len(text) > 6000 or not text.strip() and not refs and not attachment_metadata and not media_ids:
            raise BankError('INVALID_HISTORY', '归档须有用户原话或有效资料引用，文字不超过6000字。', 422)
        clean = redact_sensitive(text)
        if len(clean) > 6000:
            raise BankError('INVALID_HISTORY', '脱敏后的用户原话超过归档范围。', 422)
        required = {'contract', 'registered_user_id', 'workspace_id', 'request_id', 'archive_sequence',
                    'content', 'personal_revision', 'privacy_revision', 'received_at',
                    'archive_epoch', 'references', 'attachments'}
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not self._guarded_versions(db, owner, bank_db, expected_revision, expected_privacy):
                    db.rollback();return False
                db.execute('INSERT OR IGNORE INTO dialogue_archive_epochs(workspace_id,archive_epoch) VALUES(?,0)', (owner,))
                epoch = self.archive_epoch_from_db(db, owner)
                conversation_id = None
                if conversation_scope_validator is not None:
                    if not callable(conversation_scope_validator):
                        raise BankError('CONVERSATION_SCOPE_INVALID','对话范围无法核验。',422)
                    conversation_id = conversation_scope_validator(db)
                    if type(conversation_id) is not str or re.fullmatch(r'[0-9a-f]{32}',conversation_id) is None:
                        raise BankError('CONVERSATION_SCOPE_INVALID','对话范围无法核验。',422)
                if media_ids:
                    service=getattr(self,'chat_media',None)
                    if service is None:raise BankError('CHAT_MEDIA_UNAVAILABLE','媒体存储尚未接线。',503)
                    attachment_metadata+=service.bind_user_in_db(db,owner,request_id,media_ids,bank_db=bank_db,
                        registered_user_id=registered_user_id)
                    if len(attachment_metadata)>8:raise BankError('CHAT_MEDIA_IDS_INVALID','每轮媒体超过上限。',422)
                existing = db.execute('SELECT * FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',
                                      (owner, request_id)).fetchone()
                if existing is not None:
                    # 只认领同epoch内完整原包；不重封印、不修补或覆盖异常行。
                    cipher = existing['user_text_cipher']
                    if type(cipher) is not str or not cipher.startswith('pd1:'):
                        db.rollback();return False
                    try:
                        packet = json.loads(self._decode(cipher))
                    except (BankError, TypeError, ValueError):
                        db.rollback();return False
                    valid = (type(packet) is dict and set(packet) == required
                             and packet['contract'] == ARCHIVE_USER_CONTRACT
                             and type(packet['registered_user_id']) is str
                             and packet['registered_user_id'] == registered_user_id
                             and type(packet['workspace_id']) is str and packet['workspace_id'] == owner
                             and type(packet['request_id']) is str and packet['request_id'] == request_id
                             and type(packet['content']) is str and packet['content'] == clean
                             and type(packet['archive_sequence']) is int and packet['archive_sequence'] >= 1
                             and type(packet['archive_epoch']) is int and packet['archive_epoch'] == epoch
                             and type(packet['personal_revision']) is int
                             and packet['personal_revision'] == expected_revision
                             and type(packet['privacy_revision']) is int
                             and packet['privacy_revision'] == expected_privacy
                             and type(packet['received_at']) is float and math.isfinite(packet['received_at'])
                             and _json(packet['references']) == _json(refs)
                             and _json(packet['attachments']) == _json(attachment_metadata)
                             and all(type(packet[key]) is type(existing[key]) and packet[key] == existing[key]
                                     for key in ('workspace_id', 'request_id', 'archive_sequence', 'archive_epoch',
                                                 'personal_revision', 'privacy_revision', 'received_at')))
                    if not valid:
                        db.rollback();return False
                else:
                    sequence = db.execute('SELECT COALESCE(MAX(archive_sequence),0)+1 FROM agent_user_turn_archive WHERE workspace_id=?',
                                          (owner,)).fetchone()[0]
                    if type(sequence) is not int or not 1 <= sequence <= 9223372036854775807:
                        raise BankError('INVALID_HISTORY', '聊天归档序号无效。', 409)
                    stamp = self.clock()
                    if type(stamp) not in (int, float) or not math.isfinite(stamp):
                        raise BankError('INVALID_HISTORY', '聊天归档时间无效。', 503)
                    packet = {'contract': ARCHIVE_USER_CONTRACT,
                              'registered_user_id': registered_user_id, 'workspace_id': owner,
                              'request_id': request_id, 'archive_sequence': sequence, 'content': clean,
                              'personal_revision': expected_revision, 'privacy_revision': expected_privacy,
                              'received_at': float(stamp), 'archive_epoch': epoch,
                              'references': refs, 'attachments': attachment_metadata}
                    sealed = self.protect(json.dumps(packet, ensure_ascii=False, sort_keys=True,
                                                     separators=(',', ':'), allow_nan=False))
                    db.execute('INSERT INTO agent_user_turn_archive(workspace_id,request_id,archive_sequence,archive_epoch,user_text_cipher,personal_revision,privacy_revision,received_at) VALUES(?,?,?,?,?,?,?,?)',
                               (owner, request_id, sequence, epoch, sealed, expected_revision, expected_privacy,
                                packet['received_at']))
                member = db.execute('SELECT * FROM agent_conversation_membership WHERE workspace_id=? AND request_id=?',
                                    (owner,request_id)).fetchone()
                if conversation_id is None:
                    if member is not None:
                        raise BankError('CONVERSATION_SCOPE_CONFLICT','当前请求已绑定另一对话。',409)
                else:
                    binding = {'contract':'navren.conversation-membership/1','workspace_id':owner,
                        'registered_user_id':registered_user_id,'request_id':request_id,
                        'conversation_id':conversation_id,'archive_sequence':packet['archive_sequence'],
                        'archive_epoch':epoch,'privacy_revision':expected_privacy}
                    if member is None:
                        if existing is not None:
                            raise BankError('CONVERSATION_SCOPE_CONFLICT','历史原件不能自动绑定新对话。',409)
                        sealed_binding=self.protect(json.dumps(binding,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))
                        db.execute('INSERT INTO agent_conversation_membership(workspace_id,request_id,conversation_id,archive_sequence,archive_epoch,binding_cipher) VALUES(?,?,?,?,?,?)',
                            (owner,request_id,conversation_id,packet['archive_sequence'],epoch,sealed_binding))
                    else:
                        try:verified=json.loads(self._decode(member['binding_cipher']))
                        except (BankError,TypeError,ValueError):
                            raise BankError('CONVERSATION_SCOPE_CONFLICT','对话归属无法核验。',409) from None
                        if (type(verified) is not dict or _json(verified) != _json(binding) or member['conversation_id'] != conversation_id
                                or member['archive_sequence'] != packet['archive_sequence'] or member['archive_epoch'] != epoch):
                            raise BankError('CONVERSATION_SCOPE_CONFLICT','当前请求已绑定另一对话。',409)
                db.commit()
            except Exception:
                db.rollback();raise
        self._permissions()
        return {'archived': True, 'request_id': request_id, 'archive_sequence': packet['archive_sequence'],
                'personal_revision': expected_revision, 'privacy_revision': expected_privacy,
                'received_at': packet['received_at'], 'archive_epoch': epoch,'attachments':packet['attachments']}

    def save_turn_guarded(self, owner, messages, response_id, widgets, *, bank_db,
                          expected_revision, expected_privacy, message_state=None, validate_message_state=None):
        """持既有银行发布锁保存会话；长期记忆开关不控制会话历史。"""
        clean = self._clean_history(messages)
        if (not isinstance(response_id, str) or not 1 <= len(response_id) <= 160
                or not isinstance(widgets, list)):
            raise BankError('INVALID_HISTORY', '对话组件格式无效。')
        if not self._guarded_bank_owner(owner, bank_db):
            return False
        supplied = None
        if message_state is not None:
            if not callable(validate_message_state):
                raise BankError('MESSAGE_STATE_VALIDATOR_REQUIRED', '消息状态需要受信服务核验。', 422)
            if not clean or clean[-1]['role'] != 'assistant':_message_state_error()
            supplied = _history_message_state(owner,response_id,message_state,clean[-1]['content'],
                                             expected_revision,expected_privacy)
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not self._guarded_versions(db,owner,bank_db,expected_revision,expected_privacy):
                    db.rollback();return False
                dialogue = self._raw_dialogue(db,owner)
                widget = self._raw_widget(db,owner,response_id)
                existing = db.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?', (owner,response_id)).fetchone()
                self._inherit_message_states(db,owner,clean,dialogue,response_id,same_response=existing is not None)
                if supplied is not None:
                    canonical = validate_message_state(owner,deepcopy(supplied),bank_db=bank_db)
                    canonical = _history_message_state(owner,response_id,canonical,clean[-1]['content'],
                                                       expected_revision,expected_privacy)
                    if _message_state_json(canonical) != _message_state_json(supplied):
                        _message_state_error()
                    clean[-1]['message_state'] = canonical
                try:
                    payload_hash = hashlib.sha256(_json({'messages':clean,'widgets':widgets}).encode()).hexdigest()
                except (TypeError, ValueError):
                    raise BankError('INVALID_HISTORY', '对话组件格式无效。') from None
                history_hash = self._guarded_row_hash(dialogue,'messages_cipher','updated_at')
                widget_hash = self._guarded_row_hash(widget,'widgets_cipher','created_at')
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
                # A server-completed publication must never be compensated as an
                # unpublished turn, even if the later archive attestation fails.
                # Keep the legacy inline marker under the same conservative gate.
                publication=db.execute('SELECT 1 FROM guarded_turn_publications WHERE workspace_id=? AND response_id=?',
                                       (owner,response_id)).fetchone()
                widget=self._raw_widget(db,owner,response_id)
                if publication is not None or (widget is not None and any(
                        type(item) is dict and item.get('contract')=='navren.response-publication/1'
                        for item in json.loads(self._decode(widget['widgets_cipher'])))):
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
        """模型/旧组件入口只看当前版本的真实guarded原件；归档reader另行只读核验。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BankError('INVALID_HISTORY', '历史组件数量无效。')
        self.validate_owner(owner)
        if self.bank is None:
            return []  # 无银行隐私版本，不能证明旧组件仍有本轮上下文资格。
        from user_memory import privacy_revision
        with closing(self.bank.connect()) as bank_db:
            privacy = privacy_revision(bank_db, owner)
        with self.connection(owner) as db:
            revision = self.check_revision(db, owner, None)
            rows = db.execute('SELECT w.response_id,w.widgets AS widgets_cipher,w.created_at,s.widget_hash '
                              'FROM agent_widget_history w JOIN guarded_turn_saves s '
                              'ON s.workspace_id=w.workspace_id AND s.response_id=w.response_id '
                              "WHERE w.workspace_id=? AND s.status='saved' AND s.personal_revision=? AND s.privacy_revision=? "
                              'ORDER BY w.created_at DESC LIMIT ?', (owner, revision, privacy, limit)).fetchall()
        private_contracts = {'navren.approved-response/1', 'navren.response-publication/1',
                             'navren.local-rule-response/1'}
        result = []
        for row in rows:
            if row['widget_hash'] != self._guarded_row_hash(row, 'widgets_cipher', 'created_at'):
                continue
            widgets = json.loads(self._decode(row['widgets_cipher']))
            if type(widgets) is not list:
                continue
            visible = [x for x in widgets if type(x) is dict and x.get('contract') not in private_contracts]
            if visible:
                result.append({'response_id': row['response_id'], 'widgets': visible, 'created_at': row['created_at']})
        return result

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
            clause = " AND category!='account_display'" if table == 'user_memories' else ''
            return [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE workspace_id=?'+clause, (owner,))]


    def _inherit_message_states(self, db, owner, clean, dialogue, response_id, *, same_response=False):
        """匹配旧密文原件的连续历史；调用者的附带 metadata 不能获得来源资格。"""
        if dialogue is None:return
        original = json.loads(self._decode(dialogue['messages_cipher']))
        previous = self._clean_history(original)
        # 新轮末尾不是旧历史，即使正文逐字相同；同 ID 幂等重试才匹配完整原件。
        historical_limit = len(clean)
        if not same_response and clean and clean[-1]['role'] == 'assistant':
            historical_limit -= 2 if len(clean) > 1 and clean[-2]['role'] == 'user' else 1
        overlap = next((size for size in range(min(len(previous),historical_limit),0,-1)
                        if previous[-size:] == clean[:size]),0)
        if not overlap:return
        saved = {row['response_id']:(row['personal_revision'],row['privacy_revision']) for row in db.execute(
            "SELECT response_id,personal_revision,privacy_revision FROM guarded_turn_saves WHERE workspace_id=? AND status='saved'",(owner,))}
        for old, current in zip(original[-overlap:],clean[:overlap]):
            state = old.get('message_state')
            if (old['role'] != 'assistant' or type(state) is not dict
                    or type(state.get('message_id')) is not str
                    or state['message_id'] == response_id or state['message_id'] not in saved):
                continue
            revision, privacy = saved[state['message_id']]
            try:
                current['message_state'] = _history_message_state(owner,state['message_id'],state,
                                                                 current['content'],revision,privacy)
            except BankError:
                # 异常旧 metadata 不能阻止新轮普通历史，也不能作为可操作状态恢复。
                continue

    def read_message_states(self, owner):
        """读取受信历史原件；状态显示不等于当前版本可操作，刷新必须另行复验。"""
        with self.connection(owner) as db:
            row = db.execute('SELECT messages FROM agent_dialogues WHERE workspace_id=?',(owner,)).fetchone()
            messages = json.loads(row['messages']) if row else []
            saved = {item['response_id']:(item['personal_revision'],item['privacy_revision']) for item in db.execute(
                "SELECT response_id,personal_revision,privacy_revision FROM guarded_turn_saves WHERE workspace_id=? AND status='saved'",(owner,))}
        result = []
        for message in messages:
            if type(message) is not dict:continue
            state = message.get('message_state')
            if (message.get('role') != 'assistant' or type(state) is not dict
                    or type(state.get('message_id')) is not str or state['message_id'] not in saved):
                continue
            revision, privacy = saved[state['message_id']]
            try:
                result.append(_history_message_state(owner,state['message_id'],state,message['content'],revision,privacy))
            except (BankError, KeyError):
                continue
        return result

    def append_turn_widget_guarded(self, owner, response_id, widget, *, bank_db,
                                   expected_revision, expected_privacy):
        """同原银行发布锁记录服务完成回执，保持组件原件及保存指纹。"""
        if type(widget) is not dict or not self._guarded_bank_owner(owner, bank_db):
            return False
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not self._guarded_versions(db,owner,bank_db,expected_revision,expected_privacy):
                    db.rollback();return False
                saved=db.execute("SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=? AND status='saved'",
                                 (owner,response_id)).fetchone()
                row=self._raw_widget(db,owner,response_id);dialogue=self._raw_dialogue(db,owner)
                if (not saved or not row or saved['personal_revision']!=expected_revision or saved['privacy_revision']!=expected_privacy
                    or saved['widget_hash']!=self._guarded_row_hash(row,'widgets_cipher','created_at')
                    or saved['history_hash']!=self._guarded_row_hash(dialogue,'messages_cipher','updated_at')):
                    db.rollback();return False
                widgets=json.loads(self._decode(row['widgets_cipher']))
                existing=[x for x in widgets if isinstance(x,dict) and x.get('contract')==widget.get('contract')]
                if existing:
                    db.rollback();return len(existing)==1 and existing[0]==widget
                publication=db.execute('SELECT * FROM guarded_turn_publications WHERE workspace_id=? AND response_id=?',
                                       (owner,response_id)).fetchone()
                if publication:
                    valid=(publication['save_generation']==saved['save_generation']
                           and publication['widget_hash']==saved['widget_hash']
                           and json.loads(self._decode(publication['publication_widget']))==widget)
                    db.rollback();return valid
                # 发布凭据只新增本轮绑定记录，不能重加密已经发送成功的组件原件。
                db.execute('INSERT INTO guarded_turn_publications VALUES(?,?,?,?,?)',
                           (owner,response_id,saved['save_generation'],saved['widget_hash'],self.protect(_json(widget))))
                db.commit()
            except Exception:
                db.rollback();raise
        self._permissions()
        return True

    def published_turn_widgets(self, db, owner, response_id):
        """受信原件reader组合发布凭据；原组件密文、时点及CAS摘要保持不变。"""
        row=self._raw_widget(db,owner,response_id)
        if row is None:
            return None
        widgets=json.loads(self._decode(row['widgets_cipher']))
        publication=db.execute('SELECT * FROM guarded_turn_publications WHERE workspace_id=? AND response_id=?',
                               (owner,response_id)).fetchone()
        if publication is None:
            return widgets  # 兼容此前内联的发布标记；原reader仍核验封印。
        saved=db.execute("SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=? AND status='saved'",
                         (owner,response_id)).fetchone()
        if (not saved or publication['save_generation']!=saved['save_generation']
                or publication['widget_hash']!=saved['widget_hash']
                or saved['widget_hash']!=self._guarded_row_hash(row,'widgets_cipher','created_at')):
            raise BankError('HISTORY_PUBLICATION_CHANGED', '发布凭据与对话组件原件不符。', 409)
        widgets.append(json.loads(self._decode(publication['publication_widget'])))
        return widgets


def personal_store(bank):
    store = getattr(bank, 'personal_store', None)
    return store if store is not None else PersonalDataStore(bank)
