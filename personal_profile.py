"""用户自填账户资料；独立于银行客户身份、风险资料与财务记忆。"""
from __future__ import annotations

import base64
import binascii
from contextlib import closing
from datetime import date, datetime
import io
import json
import re
import warnings
from zoneinfo import ZoneInfo

from simulated_bank.service import BankError, strict_fields


FIELDS = {
    'nickname': 40, 'full_name': 80, 'phone': 32, 'email': 254,
    'city': 80, 'occupation': 80, 'company': 120, 'birthday': 10,
    'gender': 12, 'bio': 500, 'avatar': 400 * 1024,
}
GENDERS = frozenset(('', 'female', 'male', 'undisclosed'))
TZ = ZoneInfo('Asia/Shanghai')
SCHEMA = '''CREATE TABLE IF NOT EXISTS personal_profiles(
 workspace_id TEXT PRIMARY KEY, value TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), updated_at REAL NOT NULL)'''


def _invalid(field, message):
    raise BankError('PERSONAL_PROFILE_INVALID', message, 400)


def _avatar(value):
    if not value:
        return value
    match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})', value)
    if not match:
        _invalid('avatar', '头像只支持 PNG、JPEG 或 WebP 图片。')
    try:
        raw = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        _invalid('avatar', '头像图片编码无效。')
    if not raw or len(raw) > 400 * 1024:
        _invalid('avatar', '头像图片不能超过 400KB。')
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if (image.format != {'png': 'PNG', 'jpeg': 'JPEG', 'webp': 'WEBP'}[match[1]]
                        or getattr(image, 'n_frames', 1) != 1
                        or not 1 <= image.width * image.height <= 4000000):
                    _invalid('avatar', '头像需要是有效的静态图片，最多 400 万像素。')
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
    except ImportError:
        raise BankError('PERSONAL_PROFILE_IMAGE_UNAVAILABLE', '头像图片校验组件不可用。', 503) from None
    except BankError:
        raise
    except Exception:
        _invalid('avatar', '头像图片内容损坏或格式无效。')
    return value


def validate_profile(profile, today):
    strict_fields(profile, FIELDS, FIELDS)
    clean = {}
    for field, limit in FIELDS.items():
        value = profile[field]
        # 图片的上限按解码后字节计；先限制字符串，避免无界解码。
        maximum = 4 * ((limit + 2) // 3) + 40 if field == 'avatar' else limit
        if not isinstance(value, str) or len(value) > maximum:
            _invalid(field, '账户资料字段格式或长度无效。')
        value = value.strip()
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', value):
            _invalid(field, '账户资料不能包含控制字符。')
        if field != 'bio' and ('\n' in value or '\r' in value or '\t' in value):
            _invalid(field, '此账户资料字段只能填写一行。')
        clean[field] = value
    if not clean['nickname']:
        _invalid('nickname', '请填写昵称。')
    if clean['phone']:
        digits = re.sub(r'[^0-9]', '', clean['phone'])
        if (not re.fullmatch(r'\+?[0-9 ()-]+', clean['phone'])
                or not 6 <= len(digits) <= 20):
            _invalid('phone', '请填写有效的联系电话。')
    if clean['email']:
        parts = clean['email'].split('@')
        if (len(parts) != 2 or not 1 <= len(parts[0]) <= 64
                or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", parts[0])
                or parts[0].startswith('.') or parts[0].endswith('.') or '..' in parts[0]
                or len(parts[1]) > 253 or '.' not in parts[1]
                or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                       for label in parts[1].split('.'))):
            _invalid('email', '请填写有效的电子邮箱。')
    if clean['birthday']:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', clean['birthday']):
                raise ValueError
            if date.fromisoformat(clean['birthday']) > today:
                raise ValueError
        except ValueError:
            _invalid('birthday', '出生日期必须是有效日期，且不能晚于今天。')
    if clean['gender'] not in GENDERS:
        _invalid('gender', '性别选项无效。')
    clean['avatar'] = _avatar(clean['avatar'])
    return clean


class PersonalProfile:
    def __init__(self, gateway):
        self.g = gateway
        self.store = gateway.personal_data
        with closing(self.store.connect()) as db:
            db.execute(SCHEMA)
            db.commit()

    @staticmethod
    def _default(identity):
        fields = {field: '' for field in FIELDS}
        identity = identity if isinstance(identity, dict) else {}
        nickname = identity.get('display_name') or identity.get('name')
        fields['nickname'] = (nickname.strip()[:40]
                              if isinstance(nickname, str) and nickname.strip() else '我的账户')
        return {**fields, 'revision': 0, 'updated_at': None}

    def _profile(self, db, workspace, identity=None):
        # value 使用个人库的标准加密列名，由既有连接的 row_factory 解密。
        row = db.execute('SELECT value,revision,updated_at FROM personal_profiles WHERE workspace_id=?',
                         (workspace,)).fetchone()
        if row is None:
            return self._default(identity)
        try:
            profile = json.loads(row['value'])
            if not isinstance(profile, dict) or set(profile) != set(FIELDS):
                raise ValueError
        except (ValueError, TypeError):
            raise BankError('PERSONAL_PROFILE_UNREADABLE', '账户资料无法读取，请稍后重试。', 503) from None
        return {**profile, 'revision': row['revision'],
                'updated_at': datetime.fromtimestamp(row['updated_at'], TZ).isoformat(timespec='seconds')}

    def read(self, workspace, *, identity=None):
        if getattr(self.g, 'registered_mode', False):
            state = self.g.account_state.read(workspace)
            return self._account_projection(state)
        self.store.validate_owner(workspace)
        with closing(self.store.connect()) as db:
            profile = self._profile(db, workspace, identity)
        return {'profile': profile, 'workspace_binding': workspace}

    def save(self, workspace, payload, *, identity=None, expected_privacy=None):
        strict_fields(payload, ('expected_revision', 'profile'), ('expected_revision', 'profile'))
        expected = payload['expected_revision']
        if type(expected) is not int or not 0 <= expected <= 2**63 - 2:
            raise BankError('PERSONAL_PROFILE_VERSION_REQUIRED', '请先读取账户资料，再提交当前版本。', 409)
        clean = validate_profile(payload['profile'], datetime.fromtimestamp(self.store.clock(), TZ).date())
        if getattr(self.g, 'registered_mode', False):
            with self.g.bank.transaction() as bank_db:
                if expected_privacy is not None:
                    from user_memory import privacy_revision
                    if privacy_revision(bank_db, workspace) != expected_privacy:
                        raise BankError('PRIVACY_REVISION_CHANGED', '资料授权已变化，请重新读取账户资料。', 409)
                state = self.g.account_state.read(workspace, bank_db=bank_db)
                state = self.g.account_state.save(workspace, {'expected_revision': expected,
                    'section': 'profile', 'value': {**clean, 'avatar_preset': state['sections']['profile']['avatar_preset']}}, bank_db=bank_db)
                return self._account_projection(state)
        self.store.validate_owner(workspace)
        # 银行事务只取得既有会话 guard 与互斥锁，绝不改银行、风险或记忆数据。
        with self.g.bank.transaction() as bank_db:
            if expected_privacy is not None:
                from user_memory import privacy_revision
                if privacy_revision(bank_db, workspace) != expected_privacy:
                    raise BankError('PRIVACY_REVISION_CHANGED', '资料授权已变化，请重新读取账户资料。', 409)
            with closing(self.store.connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT revision FROM personal_profiles WHERE workspace_id=?',
                                     (workspace,)).fetchone()
                revision = current['revision'] if current else 0
                if revision != expected:
                    raise BankError('PERSONAL_PROFILE_VERSION_CONFLICT', '账户资料已更新，请重新读取后再保存。', 409)
                now = self.store.clock()
                protected = self.store.protect(json.dumps(clean, ensure_ascii=False, separators=(',', ':')))
                db.execute('''INSERT INTO personal_profiles VALUES(?,?,?,?)
                    ON CONFLICT(workspace_id) DO UPDATE SET
                    value=excluded.value,revision=excluded.revision,updated_at=excluded.updated_at''',
                    (workspace, protected, revision + 1, now))
                result = {'profile': self._profile(db, workspace, identity), 'workspace_binding': workspace}
                db.commit()
        return result

    @staticmethod
    def _account_projection(state):
        return {'profile': {**{f: state['sections']['profile'][f] for f in FIELDS},
                            'revision': state['revision'], 'updated_at': state['updated_at']},
                'workspace_binding': state['workspace_binding'],
                'registered_user_id': state['registered_user_id'], 'account_revision': state['revision']}
