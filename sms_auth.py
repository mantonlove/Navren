"""服务端短信挑战、频控与一次性消费；不持久化验证码。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as day_time
import hashlib
import json
import re
import uuid
from zoneinfo import ZoneInfo

from sms_aliyun import SmsFailure


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def context_digest(context):
    return digest(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))


@dataclass(frozen=True, repr=False)
class VerifiedSms:
    challenge_id: str
    principal: str
    purpose: str
    phone: str
    context: str
    provider_reference: str
    verified_at: float
    expires_at: float


class SmsChallenges:
    def __init__(self, auth, transport=None, *, allow_test=False):
        self.auth, self.transport, self.allow_test = auth, transport, allow_test
        self.clock = auth.clock
        with auth.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sms_phone_bindings(
                  id TEXT PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES welcome_users(id),
                  phone TEXT UNIQUE NOT NULL, verified_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sms_challenges(
                  id TEXT PRIMARY KEY, principal TEXT NOT NULL, purpose TEXT NOT NULL,
                  phone TEXT NOT NULL, context_digest TEXT NOT NULL, state TEXT NOT NULL,
                  created_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  provider_reference TEXT, verified_at REAL, ip_hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sms_phone_created ON sms_challenges(phone,created_at);
                CREATE INDEX IF NOT EXISTS sms_created ON sms_challenges(created_at);
            ''')

    @property
    def configured(self):
        source = getattr(self.transport, 'source', None)
        return bool(self.transport and (source == 'provider' or source == 'test_double' and self.allow_test)
                    and self.transport.configured)

    def require_configured(self):
        if not self.configured:
            raise SmsFailure('SMS_NOT_CONFIGURED', '短信服务尚未配置完成。', 503)

    @staticmethod
    def principal(session):
        return 'welcome:' + session['token_hash']

    def send(self, principal, purpose, phone, context, *, client_ip='127.0.0.1',
             challenge_id=None, expires_at=None, deadline=None):
        self.require_configured()
        self.transport.permit_number(phone)
        if purpose not in ('register', 'login', 'bind', 'operation', 'financial_recovery'):
            raise SmsFailure('SMS_PURPOSE_INVALID', '短信用途无效。')
        now = self.clock()
        expires = min(now + 300, expires_at if expires_at is not None else now + 300)
        if expires <= now + 1:
            raise SmsFailure('SMS_EXPIRED', '当前操作已过期，请重新准备。')
        identifier = challenge_id or uuid.uuid4().hex
        if not isinstance(identifier, str) or re.fullmatch(r'[a-f0-9]{32}', identifier) is None:
            raise SmsFailure('SMS_CHALLENGE_INVALID', '验证请求无效。')
        ip_hash = digest(client_ip)
        china_now = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
        day_start = datetime.combine(china_now.date(), day_time(), china_now.tzinfo).timestamp()
        with self.auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            recent = db.execute('SELECT purpose,principal FROM sms_challenges '
                'WHERE phone=? AND created_at>?', (phone, now-60)).fetchall()
            # 同一浏览器的登录/注册各有独立冷却；切页不清除已发送用途的限额。
            # 其他浏览器、绑定及金融操作仍遵守原号码级60秒间隔。
            auth_switch = (purpose in ('login', 'register') and all(
                row['purpose'] in ('login', 'register') and row['principal'] == principal
                for row in recent))
            if recent and (not auth_switch or any(row['purpose'] == purpose for row in recent)):
                raise SmsFailure('SMS_RATE_LIMITED', '请间隔60秒后再发送验证码。', 429)
            total = db.execute('SELECT COUNT(*) FROM sms_challenges WHERE created_at>=?', (day_start,)).fetchone()[0]
            phone_total = db.execute('SELECT COUNT(*) FROM sms_challenges WHERE phone=? AND created_at>=?',
                                     (phone, day_start)).fetchone()[0]
            ip_total = db.execute('SELECT COUNT(*) FROM sms_challenges WHERE ip_hash=? AND created_at>=?',
                                  (ip_hash, now-3600)).fetchone()[0]
            if ((self.transport.daily_limit is not None and total >= self.transport.daily_limit)
                    or phone_total >= self.transport.phone_daily_limit
                    or ip_total >= self.transport.ip_hourly_limit):
                raise SmsFailure('SMS_QUOTA_REACHED', '短信发送次数已达当前限额，请稍后再试。', 429)
            # 同号码同用途只保留最新挑战；OutId只作回执引用，不承担验证码隔离。
            db.execute("UPDATE sms_challenges SET state='invalidated' WHERE phone=? AND purpose=? "
                       "AND state IN ('sending','sent','verifying','verified')", (phone, purpose))
            db.execute('INSERT INTO sms_challenges '
                '(id,principal,purpose,phone,context_digest,state,created_at,expires_at,ip_hash) '
                "VALUES(?,?,?,?,?,'sending',?,?,?)",
                (identifier, principal, purpose, phone, context_digest(context), now, expires, ip_hash))
        deadline = min(deadline if deadline is not None else now+10, now+10, expires)
        try:
            reference = self.transport.send(phone, purpose, identifier, expires_at=expires, deadline=deadline)
            if not reference or self.clock() >= deadline:
                raise SmsFailure('SMS_SEND_FAILED', '短信发送已超时，请稍后再试。', 503)
        except Exception as error:
            with self.auth.connect() as db:
                db.execute("UPDATE sms_challenges SET state='failed' WHERE id=? AND state='sending'", (identifier,))
            if isinstance(error, SmsFailure):
                raise error from None
            raise SmsFailure('SMS_SEND_FAILED', '短信未能成功提交，请稍后再试。', 503) from None
        with self.auth.connect() as db:
            changed = db.execute("UPDATE sms_challenges SET state='sent',provider_reference=? "
                "WHERE id=? AND state='sending' AND expires_at>?", (reference, identifier, self.clock())).rowcount
            if changed != 1:
                raise SmsFailure('SMS_CHALLENGE_INVALID', '验证码已失效，请重新发送。')
        return {'challenge_id': identifier, 'expires_at': expires, 'resend_after': 60,
                'channel': 'sms', 'source': self.transport.source}

    def verify(self, identifier, principal, purpose, phone, code, context, *, deadline=None):
        self.require_configured()
        now = self.clock()
        if (not isinstance(identifier, str) or re.fullmatch(r'[a-f0-9]{32}', identifier) is None
                or not isinstance(code, str) or re.fullmatch(r'[0-9]{6}', code) is None):
            raise SmsFailure('SMS_CODE_INVALID', '请输入六位短信验证码。')
        expected = context_digest(context)
        with self.auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sms_challenges WHERE id=?', (identifier,)).fetchone()
            if (not row or row['principal'] != principal or row['purpose'] != purpose or row['phone'] != phone
                    or row['context_digest'] != expected or row['state'] != 'sent'
                    or not row['created_at'] <= now < row['expires_at'] or row['attempts'] >= 5):
                raise SmsFailure('SMS_CHALLENGE_INVALID', '验证码已失效或不属于本次请求，请重新发送。')
            db.execute("UPDATE sms_challenges SET state='verifying',attempts=attempts+1 WHERE id=?", (identifier,))
            expires = row['expires_at']
        deadline = min(deadline if deadline is not None else now+10, now+10, expires)
        try:
            reference = self.transport.verify(phone, purpose, identifier, code, deadline=deadline)
        except Exception as error:
            with self.auth.connect() as db:
                db.execute("UPDATE sms_challenges SET state='failed' WHERE id=? AND state='verifying'", (identifier,))
            # Preserve only fixed safe reasons; provider text never crosses this boundary.
            safe = {
                'SMS_TIMEOUT': ('验证码核验已超时，请重新发送。', 504),
                'SMS_EXPIRED': ('验证已失效，请重新发送。', 400),
                'SMS_CODE_REJECTED': ('验证码核验失败，请重新发送。', 401),
                'SMS_CODE_INVALID': ('请输入六位短信验证码。', 400),
                'SMS_CHALLENGE_INVALID': ('验证码已失效，请重新发送。', 400),
            }
            if isinstance(error, SmsFailure) and isinstance(error.code, str) and error.code in safe:
                message, status = safe[error.code]
                raise SmsFailure(error.code, message, status) from None
            raise SmsFailure('SMS_VERIFY_UNAVAILABLE', '验证码核验暂不可用，请重新发送。', 503) from None
        current = self.clock()
        retry_allowed = False
        with self.auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sms_challenges WHERE id=?', (identifier,)).fetchone()
            if not row or row['state'] != 'verifying' or not now <= current < deadline:
                raise SmsFailure('SMS_EXPIRED', '验证已失效，请重新发送。')
            if not reference:
                db.execute("UPDATE sms_challenges SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'sent' END WHERE id=?", (identifier,))
                retry_allowed = reference is None and row['attempts'] < 5 and current < row['expires_at']
            else:
                db.execute("UPDATE sms_challenges SET state='verified',provider_reference=?,verified_at=? WHERE id=?",
                           (reference, current, identifier))
        if not reference:
            raise SmsFailure('SMS_CODE_REJECTED', '验证码不正确或已经失效。', 401,
                             same_challenge_retry=retry_allowed)
        return VerifiedSms(identifier, principal, purpose, phone, expected, reference, current, expires)

    def consume(self, db, proof):
        if not isinstance(proof, VerifiedSms):
            raise SmsFailure('SMS_PROOF_INVALID', '验证凭据无效。', 403)
        changed = db.execute("UPDATE sms_challenges SET state='consumed' WHERE id=? AND principal=? "
            "AND purpose=? AND phone=? AND context_digest=? AND provider_reference=? AND state='verified' "
            "AND expires_at>?", (proof.challenge_id, proof.principal, proof.purpose, proof.phone,
                proof.context, proof.provider_reference, self.clock())).rowcount
        if changed != 1:
            raise SmsFailure('SMS_PROOF_USED', '验证凭据已使用或过期，请重新发送。', 403)

    def binding_for_user(self, user_id, *, db=None):
        if db is None:
            with self.auth.connect() as connection:
                return self.binding_for_user(user_id, db=connection)
        row = db.execute('SELECT * FROM sms_phone_bindings WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def bind_phone(self, db, user_id, proof):
        existing = self.binding_for_user(user_id, db=db)
        if existing:
            if existing['phone'] != proof.phone:
                raise SmsFailure('SMS_PHONE_CHANGE_UNAVAILABLE', '当前仅支持首次验证手机号，更换手机号尚未开放。', 409)
            return existing['id']
        other = db.execute('SELECT user_id FROM sms_phone_bindings WHERE phone=?', (proof.phone,)).fetchone()
        if other:
            raise SmsFailure('SMS_PHONE_IN_USE', '这个手机号已绑定其他账户，请使用对应账户登录。', 409)
        reference = uuid.uuid4().hex
        db.execute('INSERT INTO sms_phone_bindings VALUES(?,?,?,?)',
                   (reference, user_id, proof.phone, self.clock()))
        return reference
