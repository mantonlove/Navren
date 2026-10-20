"""注册主体的通用资料；现有加密私库、可信会话、全账号 CAS。"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime
import json
import re

from simulated_bank import BankError
from personal_profile import FIELDS, TZ, validate_profile
from user_memory import privacy_revision

CONTRACT = 'navren.account-state/1'
PRESETS = ('default', 'stone', 'wave', 'orbit', 'leaf')
MODES = ('classic', 'senior', 'youth')
TOPICS = ('health', 'property', 'agriculture', 'equipment', 'liability', 'pension', 'accident')
DEFAULTS = {
    'profile': {**{f: '' for f in FIELDS}, 'nickname': '我的账户', 'avatar_preset': 'default'},
    'settings': {'theme': 'light', 'language': 'zh-CN', 'mode': 'classic', 'font_size': 'medium',
                 'mode_font_sizes': {}, 'voice_enabled': False, 'voice_preset': 'female_sweet'},
    'preferences': {'recommendByRisk': True, 'recommendByHorizon': True, 'marketHotPush': False,
                    'marketHotVisible': True, 'weeklyReport': True},
    'insurance': {'topics': []}, 'favorites': [], 'reading': [],
    'markers': {'onboarded': False, 'privacy_read': False, 'voice_guide_read': False,
                'tips': {k: False for k in ('chat', 'recommend', 'account', 'mine')}},
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS registered_account_state(
 registered_user_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=0),
 value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS registered_account_state_audit(
 id INTEGER PRIMARY KEY,registered_user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
 revision INTEGER NOT NULL,event TEXT NOT NULL,section TEXT NOT NULL,value TEXT NOT NULL,created_at REAL NOT NULL);
'''


def invalid(message='账号资料格式无效。', code='ACCOUNT_STATE_INVALID', status=422):
    raise BankError(code, message, status)


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys): invalid()


def font(value):
    return ((type(value) is str and value in ('small', 'medium', 'large')) or
            (type(value) is int and 14 <= value <= 24) or
            (type(value) is str and re.fullmatch(r'\d{2}', value) and 14 <= int(value) <= 24))


def bounded(value, depth=0):
    if depth > 4: invalid()
    if value is None or type(value) is bool: return
    if type(value) is int and abs(value) <= 2**53-1: return
    if type(value) is str and len(value) <= 4000 and not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', value): return
    if type(value) is dict and len(value) <= 20:
        for key, item in value.items():
            if type(key) is not str or len(key) > 64 or key in ('__proto__', 'constructor', 'prototype'): invalid()
            bounded(item, depth+1)
        return
    invalid()


class AccountState:
    def __init__(self, gateway):
        self.g, self.store = gateway, gateway.personal_data
        with closing(self.store.connect()) as db:
            db.executescript(SCHEMA); db.commit()
        self.store._permissions()

    def binding(self, w, db):
        return self.g.current_registered_binding(w, db)

    def _unpack(self, row):
        if row is None:
            return {'sections': {}, 'migration': {'legacy_profile': 'none', 'conflicts': []}}
        try:
            value = json.loads(row['value'])
            if type(value) is not dict or set(value) != {'sections', 'migration'}: raise ValueError
            if type(value['sections']) is not dict or set(value['sections']) - set(DEFAULTS): raise ValueError
            return value
        except (ValueError, TypeError):
            invalid('账号资料无法解密读取。', 'ACCOUNT_STATE_UNREADABLE', 503)

    def validate(self, section, value, w):
        if section not in DEFAULTS: invalid()
        if section == 'profile':
            exact(value, DEFAULTS['profile'])
            if type(value['avatar_preset']) is not str or value['avatar_preset'] not in PRESETS: invalid()
            clean = validate_profile({f: value[f] for f in FIELDS}, datetime.fromtimestamp(self.store.clock(), TZ).date())
            return {**clean, 'avatar_preset': value['avatar_preset']}
        if section == 'settings':
            exact(value, DEFAULTS['settings'])
            if (value['theme'] not in ('light', 'dark') or value['language'] not in ('zh-CN', 'en')
                    or value['mode'] not in MODES or not font(value['font_size'])
                    or type(value['voice_enabled']) is not bool
                    or value['voice_preset'] not in ('female_sweet', 'male_warm', 'male_podcast')
                    or type(value['mode_font_sizes']) is not dict or set(value['mode_font_sizes']) - set(MODES)
                    or not all(font(v) for v in value['mode_font_sizes'].values())): invalid()
        elif section == 'preferences':
            exact(value, DEFAULTS[section])
            if not all(type(v) is bool for v in value.values()): invalid()
        elif section == 'insurance':
            exact(value, ('topics',))
            keys = value['topics']
            if type(keys) is not list or len(keys) > 3 or any(type(k) is not str or k not in TOPICS for k in keys) or len(set(keys)) != len(keys): invalid()
        elif section == 'markers':
            exact(value, DEFAULTS[section]); exact(value['tips'], DEFAULTS[section]['tips'])
            if any(type(v) is not bool for k, v in value.items() if k != 'tips') or any(type(v) is not bool for v in value['tips'].values()): invalid()
        else:
            allowed = (set('category kind object_kind object_id id text source url published_at version archive timestamp time_view source_scope workspace_id privacy_revision request_id message_id account_id personal_revision auth_subject registered_user_id'.split()) if section == 'favorites' else
                       set('kind id version source_scope product_source archive label updated_at time_view object_kind object_id'.split()))
            if type(value) is not list or len(value) > (500 if section == 'favorites' else 200): invalid()
            for item in value:
                if type(item) is not dict or not item or set(item) - allowed: invalid()
                bounded(item)
                if 'archive' in item:
                    archive = item['archive']
                    if type(archive) is not dict or set(archive) - {'edition_date', 'revision', 'event_version'}: invalid()
                if section == 'favorites':
                    category = item.get('category', 'chat_message')
                    if category not in ('chat_message', 'recommendation', 'market_news', 'market_weekly'): invalid()
                    public = category in ('market_news', 'market_weekly')
                    if public:
                        if type(item.get('object_id')) is not str or not item['object_id']: invalid()
                    elif (item.get('workspace_id') != w or type(item.get('privacy_revision')) is not int or item['privacy_revision'] < 0):
                        invalid('私人收藏必须属于当前体验工作区。', 'ACCOUNT_STATE_FOREIGN_FAVORITE', 403)
                    if 'url' in item and item['url'] is not None and (type(item['url']) is not str or not re.match(r'^https?://', item['url'])): invalid()
                elif (item.get('source_scope') not in ('public', 'market', 'public_market')
                      or type(item.get('id', item.get('object_id'))) is not str or 'version' not in item): invalid()
        return deepcopy(value)

    def _legacy_profile(self, db, bank_db, owner):
        # 只能枚举注册表已证明属于同一主体的实例；不读取公共画像、无主记录或浏览器全局值。
        workspaces = [r[0] for r in bank_db.execute('SELECT workspace_id FROM registered_user_instances WHERE registered_user_id=? AND status=\'ready\'', (owner,))]
        candidates, displays, refs = [], [], []
        has_profiles = db.execute("SELECT 1 FROM sqlite_master WHERE name='personal_profiles'").fetchone()
        for w in workspaces:
            if has_profiles:
                row = db.execute('SELECT value,revision FROM personal_profiles WHERE workspace_id=?', (w,)).fetchone()
                if row:
                    try:
                        value = json.loads(row['value'])
                        clean = self.validate('profile', {**value, 'avatar_preset': 'default'}, w)
                        candidates.append(clean); refs.append({'workspace_id': w, 'store': 'personal_profiles', 'revision': row['revision']})
                    except (BankError, ValueError, TypeError):
                        return None, {'legacy_profile': 'invalid', 'conflicts': [{'workspace_id': w, 'store': 'personal_profiles'}]}
            display = db.execute("SELECT value,record_revision,source_type,user_confirmed FROM user_memories WHERE workspace_id=? AND id='account-display'", (w,)).fetchone()
            if display and display['source_type'] == 'account_display_edit' and display['user_confirmed']:
                try:
                    item = json.loads(display['value']); name = item['display_name']; preset = item['avatar']
                    if type(name) is not str or preset not in PRESETS: raise ValueError
                    candidate = {**DEFAULTS['profile'], 'nickname': name, 'avatar_preset': preset}
                    displays.append(self.validate('profile', candidate, w)); refs.append({'workspace_id': w, 'store': 'account-display', 'revision': display['record_revision']})
                except (BankError, ValueError, TypeError, KeyError):
                    return None, {'legacy_profile': 'invalid', 'conflicts': [{'workspace_id': w, 'store': 'account-display'}]}
        # 已保存的空值也可能代表本人清除；不同完整档案不做字段级擅自拼接。
        if not candidates and not displays: return None, {'legacy_profile': 'none', 'conflicts': []}
        if ((candidates and any(c != candidates[0] for c in candidates))
                or (displays and any(c != displays[0] for c in displays))):
            return None, {'legacy_profile': 'conflict', 'conflicts': refs}
        merged = deepcopy(candidates[0] if candidates else displays[0])
        if candidates and displays:
            old = displays[0]
            if (merged['nickname'] != old['nickname'] or merged['avatar'] and old['avatar_preset'] != 'default'):
                return None, {'legacy_profile': 'conflict', 'conflicts': refs}
            merged['avatar_preset'] = old['avatar_preset']
        return merged, {'legacy_profile': 'imported', 'conflicts': []}

    def _load(self, db, bank_db, binding, *, migrate=False):
        owner, w = binding['registered_user_id'], binding['workspace_id']
        row = db.execute('SELECT * FROM registered_account_state WHERE registered_user_id=?', (owner,)).fetchone()
        value = self._unpack(row)
        if migrate and row is None:
            profile, migration = self._legacy_profile(db, bank_db, owner)
            value['migration'] = migration
            if profile is not None: value['sections']['profile'] = profile
            revision = 1 if profile is not None else 0
            self._write(db, owner, revision, value)
            self._audit(db, binding, revision, 'legacy_profile_checked', 'profile', migration)
            row = db.execute('SELECT * FROM registered_account_state WHERE registered_user_id=?', (owner,)).fetchone()
        revision = row['revision'] if row else 0
        sections = {**deepcopy(DEFAULTS), **deepcopy(value['sections'])}
        sections['favorites'] = [x for x in sections['favorites'] if x.get('category') in ('market_news', 'market_weekly') or x.get('workspace_id') == w]
        result = {'contract': CONTRACT, **binding, 'workspace_binding': w, 'revision': revision,
                  'updated_at': datetime.fromtimestamp(row['updated_at'], TZ).isoformat(timespec='seconds') if row else None,
                  'initialized_sections': sorted(value['sections']),
                  'sections': sections, 'migration': value['migration']}
        return result, value

    def _write(self, db, owner, revision, value):
        encrypted = self.store.protect(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
        db.execute('INSERT INTO registered_account_state VALUES(?,?,?,?) ON CONFLICT(registered_user_id) DO UPDATE SET revision=excluded.revision,value=excluded.value,updated_at=excluded.updated_at',
                   (owner, revision, encrypted, self.store.clock()))

    def _audit(self, db, binding, revision, event, section, metadata):
        db.execute('INSERT INTO registered_account_state_audit(registered_user_id,workspace_id,revision,event,section,value,created_at) VALUES(?,?,?,?,?,?,?)',
                   (binding['registered_user_id'], binding['workspace_id'], revision, event, section,
                    self.store.protect(json.dumps(metadata, ensure_ascii=False)), self.store.clock()))

    def read(self, w, *, bank_db=None, private_db=None):
        if bank_db is None:
            with self.g.bank.transaction() as bank:
                return self.read(w, bank_db=bank)
        binding = self.binding(w, bank_db)
        if private_db is not None:
            return self._load(private_db, bank_db, binding)[0]
        with closing(self.store.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._load(db, bank_db, binding, migrate=True)[0]
            db.commit()
        self.store._permissions()
        return result

    def _save(self, w, payload, bank_db, db):
        if type(payload) is not dict or set(payload) - {'expected_revision', 'section', 'value', 'operation', 'source'} or not {'expected_revision', 'section', 'value'} <= set(payload): invalid()
        expected, section, operation = payload['expected_revision'], payload['section'], payload.get('operation', 'replace')
        if type(expected) is not int or not 0 <= expected <= 2**53-2 or type(section) is not str or operation not in ('replace', 'migrate'): invalid()
        binding = self.binding(w, bank_db)
        state, raw = self._load(db, bank_db, binding, migrate=True)
        if state['revision'] != expected: invalid('账号资料已更新，请重新读取后再保存。', 'ACCOUNT_STATE_VERSION_CONFLICT', 409)
        if operation == 'migrate':
            source = payload.get('source'); exact(source, ('kind', 'workspace_id', 'auth_subject', 'privacy_revision'))
            request = self.g.demo_sessions._request.get()
            lease = self.g.experience_provider._validate(bank_db, request[0], workspace=w)
            if (source['kind'] != 'owned_browser_cache' or source['workspace_id'] != w or source['auth_subject'] != lease['auth_subject']
                    or type(source['privacy_revision']) is not int or source['privacy_revision'] != privacy_revision(bank_db, w)):
                invalid('待迁移缓存的会话归属已变化。', 'ACCOUNT_STATE_MIGRATION_BINDING_CHANGED', 409)
            if section in raw['sections']: invalid('服务器已有本人资料，本地缓存未覆盖。', 'ACCOUNT_STATE_ALREADY_INITIALIZED', 409)
        elif 'source' in payload: invalid()
        clean = self.validate(section, payload['value'], w)
        if section == 'favorites':
            if any('registered_user_id' in x and x['registered_user_id'] != binding['registered_user_id'] for x in clean):
                invalid('收藏的账号主体与当前登录不一致。', 'ACCOUNT_STATE_FOREIGN_FAVORITE', 403)
            others = [x for x in raw['sections'].get('favorites', []) if x.get('category') not in ('market_news', 'market_weekly') and x.get('workspace_id') != w]
            if len(others) + len(clean) > 500: invalid('收藏总数不能超过500条。')
            clean = others + clean
        raw['sections'][section] = clean
        self._write(db, binding['registered_user_id'], expected+1, raw)
        self._audit(db, binding, expected+1, operation, section, {'source': payload.get('source'), 'previous_revision': expected})
        return self._load(db, bank_db, binding)[0]

    def save(self, w, payload, *, bank_db=None, private_db=None):
        if bank_db is None:
            with self.g.bank.transaction() as bank:
                return self.save(w, payload, bank_db=bank)
        if private_db is not None: return self._save(w, payload, bank_db, private_db)
        with closing(self.store.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._save(w, payload, bank_db, db)
            db.commit()
        self.store._permissions()
        return result

    @contextmanager
    def send_guard(self, w, expected):
        # 发布前再验证当前会话/主体/资料版本；写入同用原银行锁，迟到读不能恢复旧状态。
        with self.g.bank.transaction() as bank, closing(self.store.connect()) as db:
            binding = self.binding(w, bank)
            current = self._load(db, bank, binding)[0]
            if current['revision'] != expected: invalid('账号资料已变化，请重新读取。', 'ACCOUNT_STATE_VERSION_CONFLICT', 409)
            yield bank
