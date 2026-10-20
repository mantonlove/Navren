"""现有银行单账本的集中权限与短信证明；没有第二套余额或执行器。

复用 permission-candidate prototype/2 的分级、时钟重验、持久锁及
一次消费合同，改为原 SimulatedBank 的同一事务和交易表。
"""
import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
from contextlib import closing
from .sms_mfa import SmsBinding, SmsVerified

POLICY_VERSION = 'navren.permission.sms/1'
THRESHOLD_MINOR = 100000
LOCK_THRESHOLD = 3
YELLOW_ACTIONS = frozenset({'close_mandate', 'cancel_subscription', 'card_control',
                          'aa_decline', 'aa_cancel'})
SCHEMA = '''
CREATE TABLE IF NOT EXISTS permission_bindings(
 operation_id TEXT PRIMARY KEY REFERENCES operations(id), workspace_id TEXT NOT NULL,
 policy TEXT NOT NULL, grade TEXT NOT NULL, business_day TEXT NOT NULL,
 summary_hash TEXT NOT NULL, details_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS permission_security(
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), failures INTEGER NOT NULL DEFAULT 0,
 locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)));
CREATE TABLE IF NOT EXISTS permission_sms_challenges(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(id),
 binding TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS permission_sms_proofs(
 token_hash TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(id),
 summary_hash TEXT NOT NULL, challenge_id TEXT NOT NULL, provider_reference_hash TEXT NOT NULL UNIQUE,
 phone_binding_hash TEXT NOT NULL, source TEXT NOT NULL, issued_at REAL NOT NULL,
 expires_at REAL NOT NULL, consumed_at REAL);
'''

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


class BankPermissions:
    def __init__(self, bank, *, sms_provider=None, allow_test_sms_provider=False):
        self.bank = bank
        self.provider = sms_provider
        self.allow_test_provider = allow_test_sms_provider is True
        from base_runtime.security_events import SecurityEvents
        self.events=SecurityEvents(bank)

    def initialize(self, db):
        db.executescript(SCHEMA)
        from base_runtime.security_events import SCHEMA as EVENT_SCHEMA
        db.executescript(EVENT_SCHEMA)
        from base_runtime.security_recovery import SCHEMA as RECOVERY_SCHEMA
        db.executescript(RECOVERY_SCHEMA)

    def deny(self, db, workspace, operation, code, *, suspicious=False):
        from .service import BankError
        event=self.events.in_db(db,workspace,operation,code,suspicious=suspicious,phase='native')
        self.bank._audit(db, workspace, 'permission_rejected', {'code': code, 'policy': POLICY_VERSION,
                         'request_id':event['request_id'],'security_event_id':event['event_id'],
                         'security_failure_recorded': event['security_failure_recorded']}, operation)
        raise BankError(code, '操作权限或短信验证条件未满足；请核对具体操作。', 403, persist=True)

    def failed_attempt(self, workspace, operation, code, *, suspicious=False):
        """原执行事务退出后，只写脱敏失败审计；不会重放金融副作用。"""
        with closing(self.bank.connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM workspaces WHERE id=?', (workspace,)).fetchone():
                    db.rollback();return
                event=self.events.in_db(db,workspace,operation,code,suspicious=suspicious,phase='native')
                original=db.execute('SELECT state FROM operations WHERE id=? AND workspace_id=?',
                                    (operation,workspace)).fetchone()
                self.bank._audit(db, workspace, 'permission_attempt_failed', {
                    'request_id':event['request_id'],'security_event_id':event['event_id'],'code':code,'policy':POLICY_VERSION,
                    'original_operation_state':original['state'] if original else None,
                    'requires_original_receipt_reconciliation':True,
                    'security_failure_recorded':event['security_failure_recorded']},operation)
                db.commit()
                return event
            except Exception:
                db.rollback()
                raise

    def write_allowed(self, db, workspace, operation=None):
        row = db.execute('SELECT locked FROM permission_security WHERE workspace_id=?', (workspace,)).fetchone()
        if row and row['locked']:
            self.deny(db, workspace, operation, 'FINANCIAL_WRITE_LOCKED')

    def clock(self, db, workspace, operation=None, *, floor=None):
        now = self.bank.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or (floor is not None and now < floor):
            self.deny(db, workspace, operation, 'TRUSTED_CLOCK_INVALID')
        return now

    def grade(self, db, workspace, payload):
        if payload['action'] == 'transfer':
            day = self.bank._workspace(db, workspace)['as_of']
            # 原实际成功转账流水，跨同一主体的所有付款账户累计；不计投资本金。
            previous = sum(r[0] for r in db.execute(
                "SELECT amount_minor FROM transactions WHERE workspace_id=? AND kind='transfer' "
                "AND delta_minor<0 AND substr(occurred_at,1,10)=?", (workspace, day)))
            total = previous + payload['amount_minor']
            return ('yellow' if total <= THRESHOLD_MINOR else 'red'), total
        if payload['action'] == 'apply_card' and payload.get('requested_type') == 'virtual':
            return 'yellow', None
        return ('yellow' if payload['action'] in YELLOW_ACTIONS else 'red'), None

    def prepare(self, db, workspace, operation, payload, details, expires_at, version):
        self.write_allowed(db, workspace, operation)
        now = self.clock(db, workspace, operation)
        grade, total = self.grade(db, workspace, payload)
        day = self.bank._workspace(db, workspace)['as_of']
        summary = {'workspace': workspace, 'operation': operation, 'payload': payload,
                   'version': version, 'expires_at': expires_at, 'details': details,
                   'policy': POLICY_VERSION, 'grade': grade, 'business_day': day}
        return grade, sha(summary), {'grade': grade, 'day': day, 'details_hash': sha(details),
                                    'created_at': now, 'total': total}

    def bind(self, db, workspace, operation, summary_hash, record):
        db.execute('INSERT INTO permission_bindings VALUES(?,?,?,?,?,?,?,?)',
                   (operation, workspace, POLICY_VERSION, record['grade'], record['day'],
                    summary_hash, record['details_hash'], record['created_at']))
        event=self.events.in_db(db,workspace,operation,'PERMISSION_PREPARED',outcome='allowed',phase='native')
        self.bank._audit(db, workspace, 'permission_prepared', {'policy': POLICY_VERSION,
            'request_id':event['request_id'],'security_event_id':event['event_id'],
            'grade': record['grade'], 'business_day': record['day'], 'summary_hash': summary_hash,
            'transfer_cumulative_including_current_minor': record['total']}, operation)

    def recheck(self, db, workspace, row, *, details=None):
        self.write_allowed(db, workspace, row['id'])
        binding = db.execute('SELECT * FROM permission_bindings WHERE operation_id=? AND workspace_id=?',
                             (row['id'], workspace)).fetchone()
        if not binding or binding['policy'] != POLICY_VERSION:
            self.deny(db, workspace, row['id'], 'PERMISSION_REPREPARE_REQUIRED')
        now = self.clock(db, workspace, row['id'], floor=binding['created_at'])
        if now >= row['expires_at']:
            self.deny(db, workspace, row['id'], 'CONFIRMATION_EXPIRED')
        if binding['business_day'] != self.bank._workspace(db, workspace)['as_of']:
            self.deny(db, workspace, row['id'], 'BUSINESS_DAY_CHANGED')
        payload = json.loads(row['payload'])
        # 在收费条款、草案状态等派生检查前拒绝存储载荷篡改，保持原操作绑定与安全计数。
        if not hmac.compare_digest(sha(payload),row['payload_hash']):
            self.deny(db,workspace,row['id'],'PERMISSION_BINDING_INVALID',suspicious=True)
        grade, total = self.grade(db, workspace, payload)
        if grade != binding['grade'] or grade != row['permission']:
            db.execute("UPDATE operations SET state='invalidated',verification_hash=NULL WHERE id=?", (row['id'],))
            self.deny(db, workspace, row['id'], 'REPREPARE_WITH_CURRENT_GRADE')
        if not hmac.compare_digest(binding['summary_hash'], row['confirmation_digest']):
            self.deny(db, workspace, row['id'], 'PERMISSION_BINDING_INVALID', suspicious=True)
        if details is not None:
            expected = sha({'workspace': workspace, 'operation': row['id'], 'payload': payload,
                'version': row['version'], 'expires_at': row['expires_at'], 'details': details,
                'policy': POLICY_VERSION, 'grade': grade, 'business_day': binding['business_day']})
            if sha(details) != binding['details_hash'] or expected != binding['summary_hash']:
                self.deny(db, workspace, row['id'], 'PERMISSION_BINDING_INVALID', suspicious=True)
        return binding, now

    def provider_source(self, db, workspace, operation):
        source = getattr(self.provider, 'source', None)
        if source != 'provider' and not (source == 'test_double' and self.allow_test_provider):
            self.deny(db, workspace, operation, 'SMS_MFA_NOT_CONFIGURED')
        return source

    def status(self, db, workspace, operation):
        row = self.bank._operation(db, workspace, operation)
        locked = db.execute('SELECT locked FROM permission_security WHERE workspace_id=?', (workspace,)).fetchone()
        source = getattr(self.provider, 'source', None)
        configured = source == 'provider' or (source == 'test_double' and self.allow_test_provider)
        available = getattr(self.provider, 'available', None)
        if configured and callable(available):
            try:
                configured = available(workspace) is True
            except Exception:
                configured = False
        return {'operation_id': operation, 'policy': POLICY_VERSION, 'grade': row['permission'],
                'channel': 'sms', 'channel_configured': configured,
                'real_SMS_acceptance': False, 'financial_write_locked': bool(locked and locked['locked']),
                'notice': ('隔离短信测试渠道已接通；不代表真实短信验收。' if configured and source == 'test_double' else
                           '短信渠道已配置；操作级整体验收尚未完成。' if configured else
                           '请先登录并验证手机号；共享体验账户不开放真实短信强验证。')}

    def start_sms(self, workspace, operation):
        from .service import BankError
        with self.bank.transaction() as db:
            row = self.bank._operation(db, workspace, operation)
            binding, now = self.recheck(db, workspace, row)
            self.bank._check_pending(db, workspace, row)
            if row['permission'] != 'red':
                self.deny(db, workspace, operation, 'SMS_VERIFICATION_UNNECESSARY')
            source = self.provider_source(db, workspace, operation)
            latest = db.execute('SELECT created_at FROM permission_sms_challenges WHERE workspace_id=? '
                                'ORDER BY created_at DESC LIMIT 1', (workspace,)).fetchone()
            if latest and now - latest['created_at'] < 60:
                self.deny(db, workspace, operation, 'SMS_SEND_RATE_LIMITED')
            challenge = SmsBinding(uuid.uuid4().hex, workspace, operation, binding['summary_hash'],
                                   binding['business_day'], POLICY_VERSION, now, min(now + 120, row['expires_at']))
            db.execute('INSERT INTO permission_sms_challenges VALUES(?,?,?,?,?,?,?)',
                (challenge.challenge_id, workspace, operation, canonical(challenge.__dict__), 'sending', now, challenge.expires_at))
        # 不把数据库或写事务交给渠道；发送后再核对同一草案和受信时间。
        try:
            sent = self.provider.send(challenge, deadline=min(now + 10, challenge.expires_at)) is True
        except Exception:
            sent = False
        with self.bank.transaction() as db:
            row = self.bank._operation(db, workspace, operation)
            self.recheck(db, workspace, row)
            self.bank._check_pending(db, workspace, row)
            current = self.clock(db, workspace, operation, floor=now)
            if not sent or current >= min(now + 10, challenge.expires_at):
                db.execute("UPDATE permission_sms_challenges SET state='failed' WHERE id=?", (challenge.challenge_id,))
                self.deny(db, workspace, operation, 'SMS_SEND_FAILED')
            db.execute("UPDATE permission_sms_challenges SET state='sent' WHERE id=?", (challenge.challenge_id,))
            self.bank._audit(db, workspace, 'sms_challenge_sent', {'challenge_id': challenge.challenge_id,
                             'summary_hash': binding['summary_hash'], 'source': source}, operation)
        return {'challenge_id': challenge.challenge_id, 'operation_id': operation,
                'expires_at': challenge.expires_at, 'channel': 'sms', 'source': source,
                'real_SMS_acceptance': False}

    def verify_sms(self, workspace, operation, payload):
        with self.bank.transaction() as db:
            row = self.bank._operation(db, workspace, operation)
            binding, now = self.recheck(db, workspace, row)
            self.bank._check_pending(db, workspace, row)
            source = self.provider_source(db, workspace, operation)
            if (not isinstance(payload, dict) or set(payload) != {'verification_code', 'challenge_id'}
                    or not isinstance(payload['verification_code'], str)
                    or re.fullmatch(r'[0-9]{6}', payload['verification_code']) is None
                    or not identifier(payload['challenge_id'])):
                self.deny(db, workspace, operation, 'SMS_CODE_INVALID', suspicious=True)
            c = db.execute('SELECT * FROM permission_sms_challenges WHERE id=? AND workspace_id=? AND operation_id=?',
                           (payload['challenge_id'], workspace, operation)).fetchone()
            if not c or c['state'] != 'sent' or now >= c['expires_at']:
                self.deny(db, workspace, operation, 'SMS_CHALLENGE_INVALID', suspicious=True)
            challenge = SmsBinding(**json.loads(c['binding']))
            if challenge.summary_sha256 != binding['summary_hash']:
                self.deny(db, workspace, operation, 'SMS_CHALLENGE_INVALID', suspicious=True)
        try:
            proof = self.provider.verify(challenge, payload['verification_code'], deadline=min(now + 10, challenge.expires_at))
        except Exception:
            proof = None
        with self.bank.transaction() as db:
            row = self.bank._operation(db, workspace, operation)
            binding, current = self.recheck(db, workspace, row)
            self.bank._check_pending(db, workspace, row)
            c = db.execute('SELECT state FROM permission_sms_challenges WHERE id=?', (challenge.challenge_id,)).fetchone()
            valid = (isinstance(proof, SmsVerified) and proof.binding == challenge
                and identifier(proof.provider_id) and identifier(proof.proof_reference)
                and identifier(proof.phone_binding_reference)
                and type(proof.verified_at) in (int, float) and math.isfinite(proof.verified_at)
                and type(proof.expires_at) in (int, float) and math.isfinite(proof.expires_at)
                and challenge.issued_at <= proof.verified_at <= current < proof.expires_at <= challenge.expires_at)
            if not valid or current < now or current >= min(now + 10, challenge.expires_at) or not c or c['state'] != 'sent':
                self.deny(db, workspace, operation, 'SMS_VERIFICATION_FAILED', suspicious=True)
            reference_hash = sha([proof.provider_id, proof.proof_reference])
            if db.execute('SELECT 1 FROM permission_sms_proofs WHERE provider_reference_hash=?', (reference_hash,)).fetchone():
                self.deny(db, workspace, operation, 'SMS_PROOF_REPLAY', suspicious=True)
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            db.execute('INSERT INTO permission_sms_proofs VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                (token_hash, workspace, operation, binding['summary_hash'], challenge.challenge_id,
                 reference_hash, sha(proof.phone_binding_reference), source, proof.verified_at, proof.expires_at))
            db.execute("UPDATE permission_sms_challenges SET state='verified' WHERE id=?", (challenge.challenge_id,))
            db.execute('UPDATE operations SET verification_hash=?,verification_expires_at=? WHERE id=?',
                       (token_hash, proof.expires_at, operation))
            self.bank._audit(db, workspace, 'sms_proof_verified', {'challenge_id': challenge.challenge_id,
                'provider_reference_hash': reference_hash, 'source': source, 'expires_at': proof.expires_at}, operation)
            return {'verification_token': token, 'operation_id': operation, 'expires_at': proof.expires_at,
                    'source': source, 'real_SMS_acceptance': False}

    def execution_proof(self, db, workspace, row, token, details):
        binding, now = self.recheck(db, workspace, row, details=details)
        if row['permission'] != 'red':
            return None
        self.provider_source(db, workspace, row['id'])
        token_hash = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else ''
        p = db.execute('SELECT * FROM permission_sms_proofs WHERE token_hash=? AND workspace_id=? AND operation_id=?',
                       (token_hash, workspace, row['id'])).fetchone()
        if (not p or p['consumed_at'] is not None or not row['verification_hash']
                or not hmac.compare_digest(row['verification_hash'], token_hash)
                or p['summary_hash'] != binding['summary_hash'] or not p['issued_at'] <= now < p['expires_at']):
            self.deny(db, workspace, row['id'], 'SMS_PROOF_REQUIRED', suspicious=True)
        current = getattr(self.provider, 'binding_is_current', None)
        if callable(current) and current(workspace, p['phone_binding_hash']) is not True:
            self.deny(db, workspace, row['id'], 'SMS_PHONE_BINDING_CHANGED', suspicious=True)
        return p

    def consume(self, db, workspace, row, proof):
        self.recheck(db, workspace, row)
        self.bank._check_pending(db, workspace, row)
        if proof is None:
            return
        current = getattr(self.provider, 'binding_is_current', None)
        if callable(current) and current(workspace, proof['phone_binding_hash']) is not True:
            self.deny(db, workspace, row['id'], 'SMS_PHONE_BINDING_CHANGED', suspicious=True)
        now = self.clock(db, workspace, row['id'], floor=proof['issued_at'])
        if now >= proof['expires_at']:
            self.deny(db, workspace, row['id'], 'SMS_PROOF_EXPIRED')
        changed = db.execute('UPDATE permission_sms_proofs SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL',
                             (now, proof['token_hash'])).rowcount
        if changed != 1:
            self.deny(db, workspace, row['id'], 'SMS_PROOF_REPLAY', suspicious=True)
        self.bank._audit(db, workspace, 'sms_proof_consumed', {'provider_reference_hash': proof['provider_reference_hash'],
                         'source': proof['source'], 'policy': POLICY_VERSION}, row['id'])
