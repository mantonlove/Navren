"""注册主体的持久画像实例与同进程体验服务；没有客户端owner选择入口。"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from copy import deepcopy
import math
import hashlib
import json
from pathlib import Path
import re
import os
import shutil
import uuid

from demo_accounts import USER_IDS
from simulated_bank import BankError
from simulated_bank.service import digest
from user_memory import privacy_revision


INSTANCE_NAMESPACE = uuid.UUID('e2294f66-b9c4-43c0-a4d9-7543d071d348')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS registered_user_instances(
 registered_user_id TEXT NOT NULL,persona_id TEXT NOT NULL,
 workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(id),
 template_version TEXT NOT NULL,template_sha256 TEXT NOT NULL,template_end_on TEXT NOT NULL,
 initialization_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('initializing','ready')),created_at REAL NOT NULL,
 PRIMARY KEY(registered_user_id,persona_id));
CREATE TABLE IF NOT EXISTS registered_user_preferences(
 registered_user_id TEXT PRIMARY KEY,last_persona_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registered_instance_sessions(
 token_hash TEXT PRIMARY KEY,registered_user_id TEXT NOT NULL,auth_subject TEXT NOT NULL,
 persona_id TEXT NOT NULL,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 generation INTEGER NOT NULL,auth_expires_at REAL NOT NULL,expires_at REAL NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
CREATE INDEX IF NOT EXISTS registered_session_subject
 ON registered_instance_sessions(registered_user_id,auth_subject,revoked);
CREATE TABLE IF NOT EXISTS registered_auth_subjects(
 registered_user_id TEXT NOT NULL,auth_subject TEXT NOT NULL,expires_at REAL NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
 PRIMARY KEY(registered_user_id,auth_subject));
CREATE TABLE IF NOT EXISTS registered_demo_sources(
 user_id TEXT NOT NULL,workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
 display_name TEXT NOT NULL,as_of TEXT NOT NULL,source_version TEXT NOT NULL,
 source_hashes TEXT NOT NULL,history_path TEXT NOT NULL,history_sha256 TEXT NOT NULL,
 source_snapshot TEXT NOT NULL,account_mapping TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','ready')),imported_at REAL NOT NULL,import_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registered_template_sources(
 template_sha256 TEXT PRIMARY KEY,template_version TEXT NOT NULL,
 financial_file TEXT NOT NULL,financial_sha256 TEXT NOT NULL,financial_dataset_version TEXT NOT NULL);
'''


def registered_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) else None
        if parsed is None or parsed.version != 4 or parsed.hex != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise BankError('REGISTERED_ID_REQUIRED', '需要认证服务提供稳定注册用户身份。', 401) from None
    return parsed.hex


def persona_id(value):
    if value not in USER_IDS:
        raise BankError('DEMO_ACCOUNT_NOT_FOUND', '没有这个模拟画像。', 404)
    return value


def experience_identity(db, *, workspace=None, persona=None, owner_workspace=None):
    """已有账务服务的身份查询；画像查成员时必须带当前owner空间。"""
    present=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='registered_demo_sources'").fetchone())
    if present:
        if workspace is not None:
            row=db.execute('''SELECT s.user_id,s.workspace_id,s.display_name,i.registered_user_id
                FROM registered_demo_sources s JOIN registered_user_instances i ON i.workspace_id=s.workspace_id
                WHERE s.workspace_id=? AND s.status='ready' AND i.status='ready' ''',(workspace,)).fetchone()
            if row:return dict(row)
        if owner_workspace is not None:
            owner=db.execute("SELECT registered_user_id FROM registered_user_instances WHERE workspace_id=? AND status='ready'",
                             (owner_workspace,)).fetchone()
            if owner:
                row=db.execute('''SELECT s.user_id,s.workspace_id,s.display_name,i.registered_user_id
                    FROM registered_demo_sources s JOIN registered_user_instances i ON i.workspace_id=s.workspace_id
                    WHERE i.registered_user_id=? AND s.user_id=? AND s.status='ready' AND i.status='ready' ''',
                    (owner[0],persona)).fetchone()
                if row:return dict(row)
                raise BankError('AA_IDENTITY_REQUIRED','本人空间的目标画像尚未完成初始化。',409)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='navren_demo_accounts'").fetchone():
        raise BankError('AA_IDENTITY_REQUIRED','尚未完成体验账本接入。',409)
    key,value=('workspace_id',workspace) if workspace is not None else ('user_id',persona)
    row=db.execute('SELECT user_id,workspace_id,display_name FROM navren_demo_accounts WHERE '+key+"=? AND status='ready'",(value,)).fetchone()
    if row:return dict(row)
    raise BankError('AA_IDENTITY_REQUIRED','体验身份不存在或未就绪。',409)


def experience_members(db, workspace):
    me=experience_identity(db,workspace=workspace)
    if me.get('registered_user_id'):
        return [dict(r) for r in db.execute('''SELECT s.user_id,s.workspace_id,s.display_name
            FROM registered_demo_sources s JOIN registered_user_instances i ON i.workspace_id=s.workspace_id
            WHERE i.registered_user_id=? AND s.status='ready' AND i.status='ready' ORDER BY s.user_id''',
            (me['registered_user_id'],))]
    return [dict(r) for r in db.execute("SELECT user_id,workspace_id,display_name FROM navren_demo_accounts WHERE status='ready' ORDER BY user_id")]


@dataclass(frozen=True)
class SealedTemplate:
    persona_id: str
    version: str
    sha256: str
    end_on: str
    display_name: str


class InstanceRegistry:
    """开账回调只读封存模板；私库失败后保留pending，不重复开账或暴露实例。"""

    def __init__(self, bank, template_for_persona, initialize_bank, initialize_private):
        if not all(callable(f) for f in (template_for_persona, initialize_bank, initialize_private)):
            raise TypeError('trusted_template_and_initializers_required')
        self.bank = bank
        self.template_for_persona = template_for_persona
        self.initialize_bank = initialize_bank
        self.initialize_private = initialize_private
        with closing(bank.connect()) as db:
            db.executescript(SCHEMA)

    @staticmethod
    def workspace_id(owner, persona):
        return uuid.uuid5(INSTANCE_NAMESPACE, registered_id(owner) + ':' + persona_id(persona)).hex

    def _template(self, persona):
        template = self.template_for_persona(persona_id(persona))
        from datetime import date
        if (type(template) is not SealedTemplate or template.persona_id != persona
                or not template.version or not re.fullmatch('[0-9a-f]{64}', template.sha256)
                or date.fromisoformat(template.end_on).isoformat() != template.end_on):
            raise BankError('INSTANCE_TEMPLATE_INVALID', '画像模板尚未封存或来源不完整。', 503)
        return template

    @staticmethod
    def _record(db, owner, persona):
        row = db.execute('SELECT * FROM registered_user_instances WHERE registered_user_id=? AND persona_id=?',
                         (owner, persona)).fetchone()
        return dict(row) if row else None

    def ensure(self, owner, persona):
        owner, persona = registered_id(owner), persona_id(persona)
        with closing(self.bank.connect()) as db:
            existing = self._record(db, owner, persona)
        if existing and existing['status'] == 'ready':
            self.initialize_private(dict(existing))
            return existing  # 模板升级或来源不可用不重置已建实例。
        template = self._template(persona) if existing is None else None
        with self.bank.transaction() as db:
            record = self._record(db, owner, persona)
            if record is None:
                workspace = self.workspace_id(owner, persona)
                if db.execute('SELECT 1 FROM workspaces WHERE id=?', (workspace,)).fetchone():
                    raise BankError('INSTANCE_WORKSPACE_COLLISION', '工作区已有未证明归属的数据，禁止认领。', 409)
                self.initialize_bank(db, workspace, template)
                if not db.execute('SELECT 1 FROM workspaces WHERE id=?', (workspace,)).fetchone():
                    raise BankError('INSTANCE_OPENING_INCOMPLETE', '画像开账未产生受控工作区。', 503)
                for table in ('transactions', 'operations', 'orders'):
                    if db.execute('SELECT 1 FROM ' + table + ' WHERE workspace_id=? LIMIT 1', (workspace,)).fetchone():
                        raise BankError('INSTANCE_OPENING_NOT_CLEAN', '初始实例不能带入运行流水或用户操作。', 409)
                db.execute('INSERT INTO registered_user_instances VALUES(?,?,?,?,?,?,?,?,?)',
                    (owner, persona, workspace, template.version, template.sha256, template.end_on,
                     uuid.uuid4().hex, 'initializing', self.bank.clock()))
                record = self._record(db, owner, persona)
            if record['status'] == 'ready':
                return record
        # initialize_private必须幂等；没有私库成功回执就不能进入ready。
        self.initialize_private(dict(record))
        with self.bank.transaction() as db:
            current = self._record(db, owner, persona)
            if (current is None or current['workspace_id'] != record['workspace_id']
                    or current['initialization_id'] != record['initialization_id']):
                raise BankError('INSTANCE_CHANGED', '实例归属已变化。', 409)
            db.execute("UPDATE registered_user_instances SET status='ready' WHERE registered_user_id=? AND persona_id=? AND status='initializing'",
                       (owner, persona))
            return self._record(db, owner, persona)

    def owner_for_workspace(self, workspace, *, db=None):
        if db is None:
            with closing(self.bank.connect()) as connection:
                return self.owner_for_workspace(workspace, db=connection)
        row = db.execute("SELECT * FROM registered_user_instances WHERE workspace_id=? AND status='ready'", (workspace,)).fetchone()
        if row is None:
            raise BankError('REGISTERED_INSTANCE_REQUIRED', '当前工作区不属于已就绪的注册实例。', 403)
        return dict(row)

    def catalog(self, owner):
        owner = registered_id(owner)
        with closing(self.bank.connect()) as db:
            records = {r['persona_id']: dict(r) for r in db.execute(
                '''SELECT i.*,s.display_name FROM registered_user_instances i
                   LEFT JOIN registered_demo_sources s ON s.workspace_id=i.workspace_id
                   WHERE i.registered_user_id=?''', (owner,))}
        accounts = []
        for persona in USER_IDS:
            record = records.get(persona)
            template = self._template(persona) if record is None else None
            accounts.append({'account_id': persona, 'user_id': persona,
                'display_name': template.display_name if template else record['display_name'] or persona,
                'source_version': template.version if template else record['template_version'],
                'workspace_id': record['workspace_id'] if record and record['status'] == 'ready' else None,
                'status': record['status'] if record else 'not_initialized'})
        return {'registered_user_id': owner, 'accounts': accounts}

    def last_persona(self, owner):
        with closing(self.bank.connect()) as db:
            row = db.execute('SELECT last_persona_id FROM registered_user_preferences WHERE registered_user_id=?',
                             (registered_id(owner),)).fetchone()
        return row[0] if row else None

    def ensure_owner(self, owner):
        return [self.ensure(owner, persona) for persona in USER_IDS]

    def aa_members(self, workspace):
        # AA按当前实例查owner，不接受另一个owner作为选择参数。
        binding = self.owner_for_workspace(workspace)
        return self.catalog(binding['registered_user_id'])['accounts']


class RegisteredExperienceProvider:
    """只交给同进程已验证欢迎服务；金融select_current只从当前令牌解析owner。"""

    def __init__(self, bank, instances, demo_sessions, private_store):
        self.bank, self.instances = bank, instances
        self.demo_sessions, self.private_store = demo_sessions, private_store

    def _auth(self, owner, subject, expiry):
        owner = registered_id(owner)
        if not isinstance(subject, str) or not re.fullmatch('[A-Za-z0-9:_-]{16,192}', subject):
            raise BankError('AUTH_SUBJECT_REQUIRED', '需要当前欢迎会话授权。', 401)
        if (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= self.bank.clock()):
            raise BankError('WELCOME_SESSION_EXPIRED', '欢迎会话已过期，请重新登录。', 401)
        return owner, subject, float(expiry)

    @staticmethod
    def _revoke(db, owner, subject):
        count = db.execute('SELECT COUNT(*) FROM registered_instance_sessions WHERE registered_user_id=? AND auth_subject=? AND revoked=0',
                           (owner, subject)).fetchone()[0]
        db.execute('UPDATE demo_sessions SET revoked=1 WHERE token_hash IN (SELECT token_hash FROM registered_instance_sessions WHERE registered_user_id=? AND auth_subject=?)',
                   (owner, subject))
        db.execute('UPDATE registered_instance_sessions SET revoked=1 WHERE registered_user_id=? AND auth_subject=?', (owner, subject))
        return count

    def _subject(self, db, owner, subject, expiry, *, create=False):
        if create:
            db.execute('INSERT OR IGNORE INTO registered_auth_subjects VALUES(?,?,?,0,0)', (owner,subject,expiry))
        row=db.execute('SELECT * FROM registered_auth_subjects WHERE registered_user_id=? AND auth_subject=?',
                       (owner,subject)).fetchone()
        if not row or row['revoked'] or row['expires_at']!=expiry or row['expires_at']<=self.bank.clock():
            raise BankError('WELCOME_SESSION_REVOKED','欢迎授权已撤销或变化，请重新登录。',401)
        return row

    def _validate(self, db, token, *, workspace=None, owner=None, subject=None):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise BankError('UNAUTHENTICATED', '需要当前注册体验会话。', 401)
        row = db.execute('''SELECT s.*,d.revoked AS bank_revoked,d.expires_at AS bank_expires,
            d.workspace_id AS bank_workspace,i.registered_user_id AS instance_owner,i.persona_id AS instance_persona,
            i.status AS instance_status,a.revoked AS subject_revoked,a.generation AS subject_generation,
            a.expires_at AS subject_expires FROM registered_instance_sessions s
            JOIN demo_sessions d ON d.token_hash=s.token_hash
            JOIN registered_user_instances i ON i.workspace_id=s.workspace_id
            JOIN registered_auth_subjects a ON a.registered_user_id=s.registered_user_id AND a.auth_subject=s.auth_subject
            WHERE s.token_hash=?''', (digest(token),)).fetchone()
        if (not row or row['revoked'] or row['bank_revoked'] or row['expires_at'] <= self.bank.clock()
                or row['auth_expires_at'] <= self.bank.clock() or row['bank_expires'] <= self.bank.clock()
                or row['subject_revoked'] or row['subject_expires']!=row['auth_expires_at']
                or row['subject_generation']!=row['generation']
                or row['expires_at'] > row['auth_expires_at'] or row['bank_expires'] > row['auth_expires_at']
                or row['instance_status'] != 'ready' or row['workspace_id'] != row['bank_workspace']
                or row['registered_user_id'] != row['instance_owner'] or row['persona_id'] != row['instance_persona']):
            raise BankError('SESSION_CHANGED', '体验会话已失效，请重新登录。', 409)
        if ((workspace is not None and workspace != row['workspace_id'])
                or (owner is not None and owner != row['registered_user_id'])
                or (subject is not None and subject != row['auth_subject'])):
            raise BankError('WORKSPACE_CHANGED', '体验会话不属于当前用户或画像。', 409)
        if getattr(self,'materializer',None) is not None:
            self.materializer.assert_private_binding(self.instances.owner_for_workspace(row['workspace_id'],db=db))
        return dict(row)

    def _lease(self, db, record, token):
        return {'registered_user_id': record['registered_user_id'], 'account_id': record['persona_id'],
            'workspace_id': record['workspace_id'], 'auth_subject': record['auth_subject'],
            'auth_expires_at': float(record['auth_expires_at']), 'lease_expires_at': float(record['expires_at']),
            'session_generation': record['generation'], 'privacy_revision': privacy_revision(db, record['workspace_id']),
            'personal_revision': self.private_store.revision(record['workspace_id']),
            'selection_required': False, 'upstream_cookies': {'qz_bank_user': token}}

    def catalog(self, registered_user_id):
        return self.instances.catalog(registered_user_id)

    def resume(self, registered_user_id, auth_subject, *, auth_expires_at, bank_session_token=None):
        owner, subject, expiry = self._auth(registered_user_id, auth_subject, auth_expires_at)
        with self.bank.transaction() as db:self._subject(db,owner,subject,expiry,create=True)
        if bank_session_token is not None:
            with closing(self.bank.connect()) as db:
                record = self._validate(db, bank_session_token, owner=owner, subject=subject)
                if record['auth_expires_at'] != expiry:
                    raise BankError('SESSION_CHANGED', '欢迎授权时点已变化，请重新建立体验会话。', 409)
                return self._lease(db, record, bank_session_token)
        persona = self.instances.last_persona(owner)
        if persona is None:
            return {**self.catalog(owner), 'auth_subject': subject, 'auth_expires_at': expiry,
                    'selection_required': True, 'account_id': None, 'workspace_id': None, 'upstream_cookies': {}}
        return self.select_registered(owner, persona, subject, auth_expires_at=expiry, _restore=True)

    def select_registered(self, registered_user_id, persona, auth_subject, *, auth_expires_at,
                          expected_workspace_id=None, _restore=False):
        owner, subject, expiry = self._auth(registered_user_id, auth_subject, auth_expires_at)
        with self.bank.transaction() as db:self._subject(db,owner,subject,expiry,create=True)
        persona_id(persona)
        # 四个私有空间先独立开好，AA目录不会回退公共成员。
        self.instances.ensure_owner(owner)
        instance = self.instances.ensure(owner, persona)
        with self.bank.transaction() as db:
            active = db.execute('''SELECT workspace_id FROM registered_instance_sessions
                WHERE registered_user_id=? AND auth_subject=? AND revoked=0 AND expires_at>?
                ORDER BY generation DESC LIMIT 1''', (owner, subject, self.bank.clock())).fetchone()
            if not _restore and (active['workspace_id'] if active else None) != expected_workspace_id:
                raise BankError('WORKSPACE_CHANGED', '当前画像已变化，请重新核对后选择。', 409)
            return self._issue(db, owner, subject, expiry, instance)

    def _issue(self, db, owner, subject, expiry, instance):
        self._auth(owner, subject, expiry)  # 开账/私库初始化后重新核真实到期时间。
        authorization=self._subject(db,owner,subject,expiry)
        current = self.instances.owner_for_workspace(instance['workspace_id'], db=db)
        if current['registered_user_id'] != owner:
            raise BankError('WORKSPACE_CHANGED', '实例归属已变化。', 409)
        previous = db.execute('''SELECT token_hash FROM registered_instance_sessions
            WHERE registered_user_id=? AND auth_subject=? ORDER BY generation DESC LIMIT 1''', (owner, subject)).fetchone()
        generation = authorization['generation']+1
        db.execute('UPDATE registered_auth_subjects SET generation=? WHERE registered_user_id=? AND auth_subject=?',
                   (generation,owner,subject))
        self._revoke(db, owner, subject)
        workspace, token = self.demo_sessions._create(db, instance['persona_id'], instance['workspace_id'],
            generation, parent_token_hash=previous['token_hash'] if previous else None)
        original_expiry = db.execute('SELECT expires_at FROM demo_sessions WHERE token_hash=?', (digest(token),)).fetchone()[0]
        expires = min(expiry, original_expiry)
        db.execute('UPDATE demo_sessions SET expires_at=? WHERE token_hash=?', (expires, digest(token)))
        db.execute('INSERT INTO registered_instance_sessions VALUES(?,?,?,?,?,?,?,?,0)',
            (digest(token), owner, subject, instance['persona_id'], workspace, generation, expiry, expires))
        db.execute('''INSERT INTO registered_user_preferences VALUES(?,?) ON CONFLICT(registered_user_id)
            DO UPDATE SET last_persona_id=excluded.last_persona_id''', (owner, instance['persona_id']))
        return self._lease(db, self._validate(db, token, owner=owner, subject=subject), token)

    def select_current(self, bank_session_token, persona, expected_workspace_id, expected_privacy_revision):
        with closing(self.bank.connect()) as db:
            current = self._validate(db, bank_session_token, workspace=expected_workspace_id)
        instance = self.instances.ensure(current['registered_user_id'], persona)
        with self.bank.transaction() as db:
            current = self._validate(db, bank_session_token, workspace=expected_workspace_id)
            if type(expected_privacy_revision) is not int or privacy_revision(db, current['workspace_id']) != expected_privacy_revision:
                raise BankError('PRIVACY_REVISION_CHANGED', '资料权限已变化，请重新读取。', 409)
            return self._issue(db, current['registered_user_id'], current['auth_subject'], current['auth_expires_at'], instance)

    def recover_committed_selection(self, registered_user_id, auth_subject, *, auth_expires_at, bank_session_token):
        """仅恢复同一有效欢迎授权中已提交、相邻代次且丢失响应头的切号。"""
        owner, subject, expiry = self._auth(registered_user_id, auth_subject, auth_expires_at)
        def reject():
            raise BankError('SESSION_CHANGED', '旧会话不属于可恢复的已提交切换，请重新读取或登录。', 409)
        if not isinstance(bank_session_token, str) or not 20 <= len(bank_session_token) <= 200:
            reject()
        with self.bank.transaction() as db:
            authorization = self._subject(db, owner, subject, expiry)
            fields = """SELECT s.*,d.revoked AS bank_revoked,d.expires_at AS bank_expires,
                d.workspace_id AS bank_workspace,d.generation AS bank_generation,d.user_id AS bank_persona,
                i.registered_user_id AS instance_owner,i.persona_id AS instance_persona,i.status AS instance_status
                FROM registered_instance_sessions s JOIN demo_sessions d ON d.token_hash=s.token_hash
                JOIN registered_user_instances i ON i.workspace_id=s.workspace_id """
            old = db.execute(fields+'WHERE s.token_hash=?', (digest(bank_session_token),)).fetchone()
            if (not old or old['registered_user_id'] != owner or old['auth_subject'] != subject
                    or old['auth_expires_at'] != expiry or old['revoked'] != 1 or old['bank_revoked'] != 1
                    or old['generation']+1 != authorization['generation']):
                reject()
            active = db.execute(fields+"""WHERE s.registered_user_id=? AND s.auth_subject=?
                AND s.generation=? AND s.revoked=0 AND d.revoked=0""", (owner,subject,authorization['generation'])).fetchall()
            if len(active) != 1:
                reject()
            current = active[0]
            for record in (old, current):
                if (record['auth_expires_at'] != expiry or not self.bank.clock() < record['expires_at'] <= expiry
                        or record['bank_expires'] != record['expires_at']
                        or record['bank_generation'] != record['generation']
                        or record['instance_status'] != 'ready' or record['instance_owner'] != owner
                        or record['bank_workspace'] != record['workspace_id']
                        or record['instance_persona'] != record['persona_id'] or record['bank_persona'] != record['persona_id']):
                    reject()
            proof = db.execute("""SELECT COUNT(*) FROM audit WHERE workspace_id=? AND event='demo_session_created'
                AND json_extract(details,'$.token_hash')=? AND json_extract(details,'$.parent_token_hash')=?
                AND json_extract(details,'$.generation')=?""", (current['workspace_id'],current['token_hash'],old['token_hash'],current['generation'])).fetchone()[0]
            if proof != 1:
                reject()
            instance = self.instances.owner_for_workspace(current['workspace_id'], db=db)
            if instance['registered_user_id'] != owner or instance['persona_id'] != current['persona_id']:
                reject()
            if min(old['expires_at'],current['expires_at']) <= self.bank.clock():
                reject()
            return self._issue(db, owner, subject, expiry, instance)

    def validate(self, token, workspace=None):
        with closing(self.bank.connect()) as db:
            return self._validate(db, token, workspace=workspace)

    def guard_current_request(self, db):
        request = self.demo_sessions._request.get()
        if request is not None:
            self._validate(db, request[0], workspace=request[1])

    def revoke_subject(self, registered_user_id, auth_subject):
        owner = registered_id(registered_user_id)
        if not isinstance(auth_subject, str) or not re.fullmatch('[A-Za-z0-9:_-]{16,192}', auth_subject):
            raise BankError('AUTH_SUBJECT_REQUIRED', '需要当前欢迎会话授权。', 401)
        with self.bank.transaction() as db:
            count = self._revoke(db, owner, auth_subject)
            db.execute('''INSERT INTO registered_auth_subjects VALUES(?,?,?,0,1)
                ON CONFLICT(registered_user_id,auth_subject) DO UPDATE SET revoked=1''',
                (owner,auth_subject,self.bank.clock()))
        return {'revoked_session_count': count, 'persistent_instances_retained': True}

    def revoke_token_family(self, token):
        if not isinstance(token,str):raise BankError('UNAUTHENTICATED','体验会话无效。',401)
        with closing(self.bank.connect()) as db:
            row=db.execute('SELECT registered_user_id,auth_subject FROM registered_instance_sessions WHERE token_hash=?',
                           (digest(token),)).fetchone()
        if not row:raise BankError('UNAUTHENTICATED','体验会话无效。',401)
        return self.revoke_subject(row['registered_user_id'],row['auth_subject'])

    def owner_for_workspace(self, workspace_id):
        return self.instances.owner_for_workspace(workspace_id)['registered_user_id']


class RegisteredTemplateMaterializer:
    """从只读封存文件开账，复用原开账/规范化，不读取任何公共运行工作区。"""

    def __init__(self, gateway):
        from simulated_bank.connected_assets import _source
        from simulated_bank.service import encoded
        self.g, self.bank = gateway, gateway.bank
        if gateway.financial_data is None or gateway.financial_data.metadata.get('history_profile') != 'realistic_three_year':
            raise BankError('REGISTERED_TEMPLATE_REQUIRED', '注册体验需要已封存的三年画像模板。', 503)
        self.bundle = gateway.demo_accounts._bundle()
        gateway.demo_accounts._validate_bundle(self.bundle)
        source, self.subjects, self.snapshots, self.financial_hash = _source(gateway.financial_data.database_path)
        self.financial_path = self._cache_file(source.database_path, self.financial_hash)
        self.legacy_path = self._cache_file(self.bundle['database'], self.bundle['hashes']['financial_data.sqlite3'])
        from financial_data_service import FinancialDataService
        self.service = FinancialDataService(self.financial_path)
        self.template_hash = digest(encoded({'financial_sha256': self.financial_hash,
            'dataset_version': self.service.dataset_version, 'legacy_bundle': self.bundle['version']}))
        self.version = 'registered-v1:' + self.service.dataset_version
        self._services = {self.template_hash: self.service}
        # 原_seed_workspace只复用结构，开账金额必须来自当前三年封存模板。
        self.opening_bundle=deepcopy(self.bundle)
        for persona,current in self.opening_bundle['currents'].items():
            snapshot=self.snapshots[persona]
            for account in current['accounts']:
                values=[a for a in snapshot['accounts'] if a['source_pool_id']==account['account_id']]
                account['balance_minor']=sum(a['balance_minor'] for a in values)
            positions={p['asset_id']:p for p in snapshot['positions']}
            old_assets={a['account_id']:a for a in current['assets']}
            source_assets={a['asset_id']:a for a in snapshot['assets']}
            current['assets']=[]
            for asset_id,position in positions.items():
                if asset_id in old_assets:
                    current['assets'].append(old_assets[asset_id])
                elif asset_id in source_assets:
                    original=source_assets[asset_id]
                    current['assets'].append({'account_id':asset_id,'user_id':persona,
                        'code':'asset:'+asset_id.rsplit(':',1)[-1],'name':original['name'],
                        'account_class':'investment','scope':'personal','source':'navren_synthetic'})
                else:
                    raise BankError('REGISTERED_TEMPLATE_POSITION_MISMATCH','封存持仓缺少原资产身份。',409)
            for asset in current['assets']:
                position=positions[asset['account_id']]
                asset.update(cost_basis_minor=position['cost_basis_minor'],book_value_minor=position['book_value_minor'],
                             balance_minor=position['book_value_minor'],unrealized_pnl_minor=position['book_value_minor']-position['cost_basis_minor'])
            for loan in current['loans']:
                contract=next((c for c in snapshot['contracts'] if c['contract_type']=='loan'
                               and c['contract_id'].rsplit(':',1)[-1]==loan['account_id'].rsplit(':',1)[-1]),None)
                if contract is None:raise BankError('REGISTERED_TEMPLATE_LOAN_MISMATCH','封存贷款关联不完整。',409)
                loan.update(outstanding_principal_minor=contract['principal_minor'],balance_minor=-contract['principal_minor'])
            current.update(start_date=self.service.metadata['start_date'],end_date=self.service.metadata['end_date'])
            current['nonfinancial_assets']=[a for a in snapshot['assets'] if a['asset_id'] not in positions]

    def _cache_file(self, path, expected_hash):
        from demo_accounts import _hash_file
        path = Path(path)
        if Path(str(path) + '-wal').exists() and Path(str(path) + '-wal').stat().st_size:
            raise BankError('REGISTERED_TEMPLATE_BUSY', '模板尚未封存，不能开账。', 409)
        target = self.bank.database.with_name(self.bank.database.stem + '.uss-source-' + expected_hash + '.sqlite3')
        if target.is_symlink():
            raise BankError('REGISTERED_TEMPLATE_CHANGED', '模板副本不能是符号链接。', 409)
        if not target.exists():
            temporary = target.with_name(target.name + '.part-' + uuid.uuid4().hex)
            try:
                shutil.copyfile(path, temporary)
                if _hash_file(temporary) != expected_hash or _hash_file(path) != expected_hash:
                    raise BankError('REGISTERED_TEMPLATE_CHANGED', '封存模板复制校验失败。', 409)
                temporary.chmod(0o400)
                with temporary.open('rb') as stream:os.fsync(stream.fileno())
                try:os.link(temporary, target)
                except FileExistsError:pass
            finally:
                temporary.unlink(missing_ok=True)
        if _hash_file(target) != expected_hash:
            raise BankError('REGISTERED_TEMPLATE_CHANGED', '封存模板副本已变化。', 409)
        return target

    def template_for_persona(self, persona):
        persona_id(persona)
        return SealedTemplate(persona, self.version, self.template_hash, self.service.metadata['end_date'],
                              self.bundle['currents'][persona]['display_name'])

    def initialize_bank(self, db, workspace, template):
        from simulated_bank.connected_assets import ensure_registered_schema, _plan_workspace, _apply_workspace
        if template.sha256 != self.template_hash:
            raise BankError('REGISTERED_TEMPLATE_CHANGED', '开账模板绑定已变化。', 409)
        ensure_registered_schema(db)
        self.g.demo_accounts._seed_workspace(db, template.persona_id, self.opening_bundle,
            Path(self.legacy_path.name), self.bundle['hashes']['financial_data.sqlite3'],
            workspace=workspace, registry_table='registered_demo_sources')
        db.execute("UPDATE registered_demo_sources SET status='ready' WHERE workspace_id=?", (workspace,))
        subject = next(dict(s, workspace_id=workspace) for s in self.subjects if s['subject_id'] == template.persona_id)
        plan = _plan_workspace(db, subject, self.snapshots[template.persona_id], self.service,
                               self.financial_hash, registry_table='registered_demo_sources')
        _apply_workspace(db, plan, self.snapshots[template.persona_id], self.service,
                         self.financial_hash, self.bank.clock())
        # 注册实例在网关启动后才开账，不能依赖服务构造时的一次性扫描。
        from simulated_bank.account_connections import seed_defaults
        binding=db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
        seed_defaults(db,workspace,binding,self.bank.clock())
        db.execute('INSERT OR IGNORE INTO registered_template_sources VALUES(?,?,?,?,?)',
                   (self.template_hash, self.version, self.financial_path.name, self.financial_hash, self.service.dataset_version))
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise BankError('REGISTERED_TEMPLATE_INVARIANT', '实例开账关联校验失败。', 409)

    def initialize_private(self, binding):
        with self.g.personal_data.transaction(binding['workspace_id'], change=False) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS registered_private_instances(
                workspace_id TEXT PRIMARY KEY,registered_user_id TEXT NOT NULL,persona_id TEXT NOT NULL,
                template_sha256 TEXT NOT NULL,initialization_id TEXT NOT NULL)''')
            previous=db.execute('SELECT * FROM registered_private_instances WHERE workspace_id=?',
                                (binding['workspace_id'],)).fetchone()
            expected=tuple(binding[k] for k in ('workspace_id','registered_user_id','persona_id','template_sha256','initialization_id'))
            if previous:
                if tuple(previous)!=expected:raise BankError('INSTANCE_PRIVATE_BINDING_CONFLICT','私库实例凭据与本次开账不符，禁止认领。',409)
                return
            if binding['status']=='ready':
                raise BankError('INSTANCE_PRIVATE_BINDING_MISSING','既有实例私库凭据缺失，不能重新初始化。',409)
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                table=row[0]
                if table in ('registered_private_instances','personal_versions'):continue
                if any(c[1]=='workspace_id' for c in db.execute('PRAGMA table_info('+table+')')):
                    if db.execute('SELECT 1 FROM '+table+' WHERE workspace_id=? LIMIT 1',(binding['workspace_id'],)).fetchone():
                        raise BankError('INSTANCE_PRIVATE_ORPHANED','发现无本次开账凭据的私有记录，已保留且禁止认领。',409)
            revision=db.execute('SELECT revision,pending_invalidation FROM personal_versions WHERE workspace_id=?',
                                (binding['workspace_id'],)).fetchone()
            if revision and (revision[0]!=0 or revision[1]):
                raise BankError('INSTANCE_PRIVATE_ORPHANED','个人版本不属于本次干净开账。',409)
            db.execute('INSERT OR IGNORE INTO user_memory_settings VALUES(?,0,?)',
                       (binding['workspace_id'], self.bank.clock()))
            db.execute('INSERT INTO registered_private_instances VALUES(?,?,?,?,?)',expected)

    def assert_private_binding(self,binding):
        with closing(self.g.personal_data.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='registered_private_instances'").fetchone():
                raise BankError('INSTANCE_PRIVATE_BINDING_MISSING','私库实例凭据缺失。',409)
            row=db.execute('SELECT * FROM registered_private_instances WHERE workspace_id=?',(binding['workspace_id'],)).fetchone()
        expected=tuple(binding[k] for k in ('workspace_id','registered_user_id','persona_id','template_sha256','initialization_id'))
        if row is None or tuple(row)!=expected:
            raise BankError('INSTANCE_PRIVATE_BINDING_CONFLICT','私库实例凭据不属于当前开账。',409)

    def service_for(self, workspace):
        from demo_accounts import _hash_file
        from financial_data_service import FinancialDataService
        with closing(self.bank.connect()) as db:
            row = db.execute('''SELECT t.* FROM registered_template_sources t JOIN registered_user_instances i
                ON i.template_sha256=t.template_sha256 WHERE i.workspace_id=? AND i.status='ready' ''', (workspace,)).fetchone()
        if row is None:
            raise BankError('REGISTERED_TEMPLATE_MISSING', '实例的封存历史来源未登记。', 503)
        key = row['template_sha256']
        if key not in self._services:
            path = self.bank.database.parent / row['financial_file']
            if path.is_symlink() or not path.is_file() or _hash_file(path) != row['financial_sha256']:
                raise BankError('REGISTERED_TEMPLATE_CHANGED', '实例封存历史副本校验失败。', 503)
            self._services[key] = FinancialDataService(path)
        service = self._services[key]
        if service.dataset_version != row['financial_dataset_version']:
            raise BankError('REGISTERED_TEMPLATE_CHANGED', '实例封存历史版本不符。', 503)
        return service


def attach_registered_experiences(gateway):
    """仅由显式registered模式构造调用；不启动服务或触及其他数据库。"""
    # schema先建立，materializer只复制所给封存文件到本银行旁。
    with closing(gateway.bank.connect()) as db:db.executescript(SCHEMA)
    materializer = RegisteredTemplateMaterializer(gateway)
    registry = InstanceRegistry(gateway.bank, materializer.template_for_persona,
                                materializer.initialize_bank, materializer.initialize_private)
    provider = RegisteredExperienceProvider(gateway.bank, registry, gateway.demo_sessions, gateway.personal_data)
    provider.materializer = materializer
    gateway.experience_provider = provider
    from simulated_bank.living_simulation import LivingWorker, LivingScheduler
    gateway.living_simulation=LivingWorker(gateway.bank,template_service_for=materializer.service_for,funding_enabled=True)
    gateway.living_simulation.initialize()
    gateway.living_scheduler=LivingScheduler(gateway.living_simulation)
    original = gateway.bank.request_identity_guard
    def guard(db):
        original(db)
        provider.guard_current_request(db)
    gateway.bank.request_identity_guard = guard
    return provider
