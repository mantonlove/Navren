"""原注册认证的金融锁恢复；独立用途，不创建金融operation，也不是模型工具。"""
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import re
import secrets
import uuid

from simulated_bank import BankError
from simulated_bank.permission_policy import canonical, sha

PURPOSE = 'financial_write_unlock'
SMS_PURPOSE = 'financial_recovery'
POLICY = 'navren.financial-recovery/1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS permission_recoveries(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 binding TEXT NOT NULL, binding_digest TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS permission_recovery_proofs(
 token_hash TEXT PRIMARY KEY, recovery_id TEXT UNIQUE NOT NULL REFERENCES permission_recoveries(id),
 workspace_id TEXT NOT NULL, purpose TEXT NOT NULL, binding_digest TEXT NOT NULL,
 provider_reference_hash TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
 issued_at REAL NOT NULL, expires_at REAL NOT NULL, consumed_at REAL);
'''


@dataclass(frozen=True)
class RecoveryBinding:
    challenge_id: str
    recovery_id: str
    subject: str
    purpose: str
    policy: str
    security_digest: str
    subject_ref: str
    session_ref: str
    phone_binding_hash: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True, repr=False)
class RecoveryVerified:
    binding: RecoveryBinding
    provider_id: str
    proof_reference: str
    verified_at: float
    expires_at: float


class FinancialLockRecovery:
    def __init__(self, bank):
        self.bank = bank
        self.permissions = bank.permissions
        self.events = self.permissions.events
        self.provider = self.permissions.provider

    def identity(self, workspace):
        source = getattr(self.provider, 'source', None)
        if source != 'provider' and not (source == 'test_double' and self.permissions.allow_test_provider):
            return None
        resolve = getattr(self.provider, 'recovery_identity', None)
        try:
            identity = resolve(workspace) if callable(resolve) else None
        except Exception:
            return None
        keys = {'subject_ref', 'session_ref', 'phone_binding_hash'}
        if (not isinstance(identity, dict) or set(identity) != keys or
                any(type(identity[k]) is not str or not re.fullmatch('[a-f0-9]{64}', identity[k]) for k in keys)):
            return None
        return identity

    def configured(self, workspace):
        return self.identity(workspace) is not None

    def _deny(self, db, workspace, reason, *, suspicious=False, fault=False):
        self.events.in_db(db, workspace, None, reason, phase='recovery',
                          outcome='fault' if fault else 'denied', suspicious=suspicious)
        raise BankError(reason, '金融锁恢复条件未满足，请核对本人会话及本次恢复验证。',
                        503 if fault else 403, persist=True)

    def _identity(self, workspace):
        identity = self.identity(workspace)
        if identity is None:
            with self.bank.transaction() as db:
                self._deny(db, workspace, 'SECURITY_RECOVERY_NOT_CONFIGURED')
        return identity

    def _current(self, db, workspace):
        identity = self.identity(workspace)
        if identity is None:
            self._deny(db, workspace, 'RECOVERY_SUBJECT_CHANGED', suspicious=True)
        return identity

    def _security(self, db, workspace):
        row = db.execute('SELECT failures,locked FROM permission_security WHERE workspace_id=?', (workspace,)).fetchone()
        count = db.execute('SELECT COUNT(*) FROM permission_security_event_counts WHERE workspace_id=?', (workspace,)).fetchone()[0]
        state = {'failures': row['failures'] if row else 0, 'locked': bool(row and row['locked'])}
        return state, sha([workspace, state, count])

    def _load(self, db, workspace, recovery_id, identity, states):
        self.bank._workspace(db, workspace)
        if type(recovery_id) is not str or not re.fullmatch('[a-f0-9]{32}', recovery_id):
            self._deny(db, workspace, 'RECOVERY_OBJECT_INVALID', suspicious=True)
        row = db.execute('SELECT * FROM permission_recoveries WHERE id=? AND workspace_id=?',
                         (recovery_id, workspace)).fetchone()
        if not row or row['state'] not in states:
            self._deny(db, workspace, 'RECOVERY_OBJECT_INVALID', suspicious=True)
        binding = RecoveryBinding(**json.loads(row['binding']))
        now = self.permissions.clock(db, workspace, floor=binding.issued_at)
        if not binding.issued_at <= now < binding.expires_at:
            self._deny(db, workspace, 'RECOVERY_EXPIRED')
        before, security = self._security(db, workspace)
        if (binding.subject != workspace or binding.recovery_id != recovery_id or binding.purpose != PURPOSE or
                binding.policy != POLICY or sha(asdict(binding)) != row['binding_digest'] or
                not before['locked'] or not hmac.compare_digest(binding.security_digest, security) or
                any(getattr(binding, k) != identity[k] for k in identity)):
            self._deny(db, workspace, 'RECOVERY_BINDING_INVALID', suspicious=True)
        return row, binding, now, before

    def _audit(self, db, workspace, binding, event, before, after, authentication_ref=None):
        value = self.events.in_db(db, workspace, None, event, outcome='allowed', phase='recovery',
                                 evidence_sha256=[sha(asdict(binding))])
        self.bank._audit(db, workspace, 'financial_lock_recovery', {
            'request_id': value['request_id'], 'security_event_id': value['event_id'],
            'recovery_id': binding.recovery_id, 'purpose': PURPOSE, 'policy': POLICY,
            'subject_ref': binding.subject_ref, 'session_ref': binding.session_ref,
            'binding_digest': sha(asdict(binding)), 'authentication_ref': authentication_ref,
            'before_state': before, 'after_state': after, 'reason': event})

    def start(self, workspace, payload):
        identity = self._identity(workspace)
        with self.bank.transaction() as db:
            if payload != {'purpose': PURPOSE, 'confirmed': True} or type(payload.get('confirmed')) is not bool:
                self._deny(db, workspace, 'RECOVERY_PURPOSE_INVALID', suspicious=True)
            now = self.permissions.clock(db, workspace)
            before, security = self._security(db, workspace)
            if not before['locked']:
                self._deny(db, workspace, 'RECOVERY_NOT_REQUIRED')
            latest = db.execute('SELECT MAX(created_at) FROM permission_recoveries WHERE workspace_id=?', (workspace,)).fetchone()[0]
            if latest is not None and now - latest < 60:
                self._deny(db, workspace, 'RECOVERY_RATE_LIMITED')
            binding = RecoveryBinding(uuid.uuid4().hex, uuid.uuid4().hex, workspace, PURPOSE, POLICY,
                                      security, **identity, issued_at=now, expires_at=now + 120)
            db.execute("UPDATE permission_recoveries SET state='invalidated' WHERE workspace_id=? AND state IN ('sending','sent','verified')", (workspace,))
            db.execute('INSERT INTO permission_recoveries VALUES(?,?,?,?,?,?,?)',
                       (binding.recovery_id, workspace, canonical(asdict(binding)), sha(asdict(binding)), 'sending', now, binding.expires_at))
        try:
            sent = self.provider.send_recovery(binding, deadline=now + 10) is True
        except Exception:
            sent = False
        with self.bank.transaction() as db:
            _, _, current, before = self._load(db, workspace, binding.recovery_id, self._current(db, workspace), ('sending',))
            if not sent or current >= now + 10:
                db.execute("UPDATE permission_recoveries SET state='failed' WHERE id=?", (binding.recovery_id,))
                self._deny(db, workspace, 'RECOVERY_PROVIDER_UNAVAILABLE', fault=True)
            db.execute("UPDATE permission_recoveries SET state='sent' WHERE id=?", (binding.recovery_id,))
            self._audit(db, workspace, binding, 'RECOVERY_SMS_SENT', before, before)
        return {'recovery_id': binding.recovery_id, 'challenge_id': binding.challenge_id, 'purpose': PURPOSE,
                'expires_at': binding.expires_at, 'source': self.provider.source, 'real_SMS_acceptance': False}

    def verify(self, workspace, recovery_id, payload):
        identity = self._identity(workspace)
        with self.bank.transaction() as db:
            if (not isinstance(payload, dict) or set(payload) != {'purpose', 'challenge_id', 'verification_code'} or
                    payload['purpose'] != PURPOSE or type(payload['verification_code']) is not str or
                    not re.fullmatch('[0-9]{6}', payload['verification_code'])):
                self._deny(db, workspace, 'RECOVERY_AUTH_FORMAT_INVALID', suspicious=True)
            _, binding, now, _ = self._load(db, workspace, recovery_id, identity, ('sent',))
            if payload['challenge_id'] != binding.challenge_id:
                self._deny(db, workspace, 'RECOVERY_OBJECT_INVALID', suspicious=True)
        try:
            proof = self.provider.verify_recovery(binding, payload['verification_code'], deadline=min(now + 10, binding.expires_at))
        except Exception as error:
            with self.bank.transaction() as db:
                if getattr(error, 'code', None) in {'SMS_CODE_REJECTED', 'SMS_CHALLENGE_INVALID', 'SMS_CODE_INVALID'}:
                    self._deny(db, workspace, 'RECOVERY_VERIFICATION_FAILED', suspicious=True)
                self._deny(db, workspace, 'RECOVERY_PROVIDER_UNAVAILABLE', fault=True)
        with self.bank.transaction() as db:
            _, _, current, before = self._load(db, workspace, recovery_id, self._current(db, workspace), ('sent',))
            if (not isinstance(proof, RecoveryVerified) or proof.binding != binding or
                    any(type(x) not in (int, float) or not math.isfinite(x) for x in (proof.verified_at, proof.expires_at)) or
                    not binding.issued_at <= proof.verified_at <= current < proof.expires_at <= binding.expires_at or
                    current >= min(now + 10, binding.expires_at) or
                    any(type(x) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}', x) for x in (proof.provider_id, proof.proof_reference))):
                self._deny(db, workspace, 'RECOVERY_VERIFICATION_FAILED', suspicious=True)
            reference = sha([proof.provider_id, proof.proof_reference])
            if (db.execute('SELECT 1 FROM permission_recovery_proofs WHERE provider_reference_hash=?', (reference,)).fetchone() or
                    db.execute('SELECT 1 FROM permission_sms_proofs WHERE provider_reference_hash=?', (reference,)).fetchone()):
                self._deny(db, workspace, 'RECOVERY_PROOF_REPLAY', suspicious=True)
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO permission_recovery_proofs VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                       (hashlib.sha256(token.encode()).hexdigest(), recovery_id, workspace, PURPOSE, sha(asdict(binding)),
                        reference, self.provider.source, proof.verified_at, proof.expires_at))
            db.execute("UPDATE permission_recoveries SET state='verified' WHERE id=?", (recovery_id,))
            self._audit(db, workspace, binding, 'RECOVERY_PROOF_ISSUED', before, before, reference)
        return {'recovery_id': recovery_id, 'purpose': PURPOSE, 'recovery_token': token,
                'expires_at': proof.expires_at, 'source': self.provider.source, 'real_SMS_acceptance': False}

    def recover(self, workspace, payload=None):
        identity = self._identity(workspace)
        with self.bank.transaction() as db:
            if (not isinstance(payload, dict) or set(payload) != {'recovery_id', 'purpose', 'recovery_token', 'confirmed'} or
                    payload['purpose'] != PURPOSE or payload['confirmed'] is not True or
                    type(payload['recovery_token']) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}', payload['recovery_token'])):
                self._deny(db, workspace, 'RECOVERY_AUTH_FORMAT_INVALID', suspicious=True)
            _, binding, now, before = self._load(db, workspace, payload['recovery_id'], identity, ('verified',))
            hashed = hashlib.sha256(payload['recovery_token'].encode()).hexdigest()
            proof = db.execute('SELECT * FROM permission_recovery_proofs WHERE token_hash=?', (hashed,)).fetchone()
            if (not proof or proof['workspace_id'] != workspace or proof['purpose'] != PURPOSE or
                    proof['recovery_id'] != binding.recovery_id or proof['binding_digest'] != sha(asdict(binding)) or
                    proof['consumed_at'] is not None or not binding.issued_at <= proof['issued_at'] <= now < proof['expires_at'] or
                    proof['source'] != self.provider.source):
                self._deny(db, workspace, 'RECOVERY_PROOF_INVALID', suspicious=True)
            db.execute('UPDATE permission_recovery_proofs SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL', (now, hashed))
            db.execute('UPDATE permission_security SET failures=0,locked=0 WHERE workspace_id=?', (workspace,))
            db.execute("UPDATE permission_recoveries SET state='consumed' WHERE id=?", (binding.recovery_id,))
            self._audit(db, workspace, binding, 'FINANCIAL_LOCK_RECOVERED', before,
                        {'failures': 0, 'locked': False}, proof['provider_reference_hash'])
            return {'recovery_id': binding.recovery_id, 'purpose': PURPOSE, 'state': 'recovered',
                    'financial_write_locked': False, 'failures': 0, 'source': proof['source'], 'real_SMS_acceptance': False}

    def dispatch(self, workspace, path, payload):
        if path == '/api/finance/security/recovery/sms':
            return self.start(workspace, payload)
        match = re.fullmatch('/api/finance/security/recovery/([a-f0-9]{32})/verify', path)
        if match:
            return self.verify(workspace, match[1], payload)
        if path == '/api/finance/security/recovery/confirm':
            return self.recover(workspace, payload)
        raise BankError('NOT_FOUND', '金融锁恢复接口不存在。', 404)
