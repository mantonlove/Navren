"""原注册身份+原Aliyun适配器，只有SMS传输/SDK客户端是明确隔离替身。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import sqlite3
import unittest
import uuid

import test_sms_status_projection as fixture
import test_sms_auth as sms_fixture
import test_sms_aliyun as sdk_fixture
from base_runtime.security_recovery import PURPOSE, RecoveryVerified
from simulated_bank import BankError
from sms_aliyun import SmsFailure, AliyunSmsTransport


class ControlledRecoveryTests(unittest.TestCase):
    setUp = fixture.SmsStatusProjectionTests.setUp

    def ready(self):
        self.transport.supports_purpose = lambda purpose: purpose in {'register', 'login', 'bind', 'operation', 'financial_recovery'}
        self.r = self.g.security_events.recovery
        self.lock()

    def call(self, callback, *args, workspace=None, token=None):
        w = workspace or self.w
        with self.g.demo_sessions.request(token or self.token, w), self.g.security_events.request(w,
                request_id=uuid.uuid4().hex, event_id='human-recovery', phase='recovery'):
            return callback(*args)

    def lock(self):
        for _ in range(3):
            self.g.security_events.rejected(self.w, BankError('FORGED_AUTHORIZATION', '', 403))
        self.assertTrue(self.g.security_events.status(self.w)['financial_write_locked'])

    def deny(self, callback, *args):
        with self.assertRaises(BankError) as caught:
            self.call(callback, *args)
        self.assertTrue(self.g.security_events.status(self.w)['financial_write_locked'])
        return caught.exception.code

    def start(self):
        return self.call(self.r.start, self.w, {'purpose': PURPOSE, 'confirmed': True})

    def grant(self):
        c = self.start()
        p = self.call(self.r.verify, self.w, c['recovery_id'], {'purpose': PURPOSE,
            'challenge_id': c['challenge_id'], 'verification_code': self.transport.code(c['challenge_id'])})
        return c, {'recovery_id': c['recovery_id'], 'purpose': PURPOSE,
                   'recovery_token': p['recovery_token'], 'confirmed': True}

    def money(self):
        with closing(self.g.bank.connect()) as db:
            return {table: [tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('accounts', 'operations', 'transactions', 'orders', 'fund_holds')}

    def test_registered_purpose_proof_unlocks_once_with_exact_audit_and_no_financial_operation(self):
        self.ready(); before = self.money(); c, payload = self.grant()
        self.assertEqual(self.transport.sent[c['challenge_id']][1], 'financial_recovery')
        self.assertTrue(self.g.security_events.status(self.w)['financial_write_locked'])
        rid = uuid.uuid4().hex
        with self.g.demo_sessions.request(self.token, self.w), self.g.security_events.request(self.w,
                request_id=rid, event_id='consume', phase='recovery'):
            done = self.g.security_events.recover(self.w, payload)
        self.assertEqual(done['state'], 'recovered'); self.assertFalse(done['real_SMS_acceptance'])
        self.assertEqual(self.money(), before)
        with closing(self.g.bank.connect()) as db:
            proof = dict(db.execute('SELECT * FROM permission_recovery_proofs').fetchone())
            recovery = dict(db.execute('SELECT * FROM permission_recoveries').fetchone())
            audit = json.loads(db.execute("SELECT details FROM audit WHERE event='financial_lock_recovery' ORDER BY id DESC LIMIT 1").fetchone()[0])
            counts = tuple(db.execute('SELECT failures,locked FROM permission_security WHERE workspace_id=?', (self.w,)).fetchone())
        self.assertEqual(counts, (0, 0)); self.assertIsNotNone(proof['consumed_at'])
        self.assertEqual(audit['request_id'], rid); self.assertEqual(audit['recovery_id'], c['recovery_id'])
        self.assertEqual(audit['purpose'], PURPOSE); self.assertTrue(audit['before_state']['locked'])
        self.assertFalse(audit['after_state']['locked']); self.assertEqual(len(audit['authentication_ref']), 64)
        stored = json.dumps([proof, recovery, audit])
        for value in (sms_fixture.PHONE, self.transport.code(c['challenge_id']), payload['recovery_token'], self.token):
            self.assertNotIn(value, stored)
        self.lock(); self.deny(self.r.recover, self.w, payload)

    def test_workspace_only_login_wait_and_unapproved_channel_never_unlock(self):
        self.ready(); sent = len(self.transport.sent)
        with self.assertRaises(BankError): self.r.recover(self.w)
        self.f.now[0] += 121
        with self.assertRaises(BankError): self.r.start(self.w, {'purpose': PURPOSE, 'confirmed': True})
        self.assertEqual(len(self.transport.sent), sent)
        self.g.bank.permissions.allow_test_provider = False
        self.deny(self.g.security_events.recover, self.w)
        self.assertEqual(len(self.transport.sent), sent)

    def test_wrong_OTP_format_purpose_and_challenge_preserve_lock(self):
        self.ready()
        mutations = [lambda p: dict(p, verification_code='wrong'), lambda p: dict(p, purpose='operation'),
                     lambda p: dict(p, challenge_id='f'*32),
                     lambda p: dict(p, verification_code='999999' if p['verification_code'] != '999999' else '111111')]
        for mutate in mutations:
            c = self.start(); code = self.transport.code(c['challenge_id'])
            p = {'purpose': PURPOSE, 'challenge_id': c['challenge_id'], 'verification_code': code}
            self.deny(self.r.verify, self.w, c['recovery_id'], mutate(p))
            self.f.now[0] += 61
        self.assertGreater(self.g.security_events.status(self.w)['failures'], 3)

    def test_expired_tampered_wrong_object_and_wrong_use_proofs_never_unlock(self):
        self.ready()
        for change in ('token', 'object', 'purpose', 'expired'):
            c, p = self.grant()
            if change == 'token': p['recovery_token'] = 'a'*43
            elif change == 'object': p['recovery_id'] = 'f'*32
            elif change == 'purpose': p['purpose'] = 'operation'
            else: self.f.now[0] += 121
            self.deny(self.r.recover, self.w, p)
            self.f.now[0] += 61

    def test_same_owner_other_browser_and_other_subject_cannot_consume(self):
        self.ready(); c, p = self.grant()
        # 同一注册用户的新密码登录和独立租约，不能借用旧会话恢复证明。
        other_auth, other_session = self.auth.login(sms_fixture.PHONE, sms_fixture.PASSWORD, self.auth.new_session()[0])
        lease = self.g.experience_provider.select_registered(other_session['registered_user_id'], 'tycoon',
            self.auth.auth_subject(other_session), auth_expires_at=other_session['expires_at'])
        other_token = lease['upstream_cookies']['qz_bank_user']
        self.auth.update_upstream_cookies(other_session['user_id'], ['qz_bank_user='+other_token], session_token=other_auth)
        with self.assertRaises(BankError): self.call(self.r.recover, self.w, p, token=other_token)
        self.assertTrue(self.g.security_events.status(self.w)['financial_write_locked'])
        # 真正另一注册用户的独立工作区不拥有原恢复对象。
        anonymous = self.auth.new_session()[0]
        challenge = self.auth.send_sms(anonymous, 'register', sms_fixture.OTHER_PHONE)['challenge_id']
        auth2, user2 = self.auth.sms_register(sms_fixture.OTHER_PHONE, sms_fixture.PASSWORD, challenge,
            self.transport.code(challenge), anonymous, password_confirmation=sms_fixture.PASSWORD)
        session2 = self.auth.session(auth2)
        lease2 = self.g.experience_provider.select_registered(session2['registered_user_id'], 'tycoon',
            self.auth.auth_subject(session2), auth_expires_at=session2['expires_at'])
        w2, token2 = lease2['workspace_id'], lease2['upstream_cookies']['qz_bank_user']
        self.auth.update_upstream_cookies(user2['user_id'], ['qz_bank_user='+token2], session_token=auth2)
        for _ in range(3): self.g.security_events.rejected(w2, BankError('FORGED_AUTHORIZATION', '', 403))
        with self.assertRaises(BankError): self.call(self.r.recover, w2, p, workspace=w2, token=token2)
        self.assertTrue(self.g.security_events.status(w2)['financial_write_locked'])

    def test_revocation_and_phone_binding_change_after_auth_cannot_unlock(self):
        self.ready(); c, p = self.grant()
        with self.auth.connect() as db:
            db.execute('UPDATE sms_phone_bindings SET id=? WHERE user_id=?', ('f'*32, self.session['user_id']))
        self.deny(self.r.recover, self.w, p)
        self.f.now[0] += 61
        c = self.start()
        self.transport.before_verify = lambda: self.auth.logout(self.auth_token)
        self.deny(self.r.verify, self.w, c['recovery_id'], {'purpose': PURPOSE, 'challenge_id': c['challenge_id'],
            'verification_code': self.transport.code(c['challenge_id'])})
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM permission_recovery_proofs WHERE consumed_at IS NOT NULL').fetchone()[0], 0)

    def test_new_suspicious_event_invalidates_the_original_lock_snapshot(self):
        self.ready(); _, p = self.grant()
        self.g.security_events.rejected(self.w, BankError('FORGED_AUTHORIZATION', '', 403))
        self.deny(self.r.recover, self.w, p)

    def test_atomic_consumption_rolls_back_on_audit_failure_then_concurrency_consumes_once(self):
        self.ready(); _, p = self.grant()
        with closing(self.g.bank.connect()) as db:
            db.execute("CREATE TRIGGER recovery_audit_fail BEFORE INSERT ON audit WHEN NEW.event='financial_lock_recovery' BEGIN SELECT RAISE(ABORT,'controlled_fault'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.call(self.r.recover, self.w, p)
        self.assertTrue(self.g.security_events.status(self.w)['financial_write_locked'])
        with closing(self.g.bank.connect()) as db:
            self.assertIsNone(db.execute('SELECT consumed_at FROM permission_recovery_proofs').fetchone()[0])
            db.execute('DROP TRIGGER recovery_audit_fail')
        def consume(_):
            try: return self.call(self.r.recover, self.w, p)['state']
            except BankError: return 'rejected'
        with ThreadPoolExecutor(2) as pool: results = list(pool.map(consume, range(2)))
        self.assertEqual(sorted(results), ['recovered', 'rejected'])
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM permission_recoveries WHERE state='consumed'").fetchone()[0], 1)

    def test_provider_failure_does_not_increase_attack_count_and_fake_proof_fails_closed(self):
        self.ready(); before = self.g.security_events.status(self.w)['failures']
        with patch.object(self.provider, 'send_recovery', side_effect=RuntimeError('DO_NOT_LOG')):
            self.deny(self.r.start, self.w, {'purpose': PURPOSE, 'confirmed': True})
        self.assertEqual(self.g.security_events.status(self.w)['failures'], before)
        self.f.now[0] += 61; c = self.start()
        with patch.object(self.provider, 'verify_recovery', return_value=SimpleNamespace(human_authorized=True)):
            self.deny(self.r.verify, self.w, c['recovery_id'], {'purpose': PURPOSE, 'challenge_id': c['challenge_id'],
                'verification_code': self.transport.code(c['challenge_id'])})
        self.assertNotIn('DO_NOT_LOG', json.dumps(self.g.security_events.read(self.w)))

    def test_native_authenticated_CSRF_routes_and_safe_original_receipt_reads_while_locked(self):
        self.ready()
        def request(path, payload=None, csrf=True, authenticated=True):
            body = json.dumps(payload).encode() if payload is not None else b''
            h = SimpleNamespace(path=path, command='POST' if payload is not None else 'GET',
                client_address=('127.0.0.1', 1), server=SimpleNamespace(server_port=8222), rfile=BytesIO(body),
                headers={'Host': '127.0.0.1:8222', 'Origin': 'http://127.0.0.1:8222',
                    'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                    'X-QZ-CSRF': self.g.csrf(self.token) if csrf else '',
                    'Cookie': 'qz_bank_user='+self.token if authenticated else ''}, _navren_request_id=uuid.uuid4().hex)
            h._finance_json = lambda status, data, cookie=None: setattr(h, 'response', (status, data))
            self.g.dispatch(h)
            return h.response
        route = '/api/finance/security/recovery'
        self.assertEqual(request(route+'/sms', {'purpose': PURPOSE, 'confirmed': True}, csrf=False)[0], 403)
        self.assertEqual(request(route, authenticated=False)[0], 401)
        self.assertEqual(request(route+'?workspace=forged')[0], 400)
        self.assertTrue(request(route)[1]['recovery_configured'])
        self.assertEqual(request('/api/finance/operations/'+self.op['id'])[0], 200)
        status, c = request(route+'/sms', {'purpose': PURPOSE, 'confirmed': True}); self.assertEqual(status, 200)
        status, p = request(route+'/'+c['recovery_id']+'/verify', {'purpose': PURPOSE, 'challenge_id': c['challenge_id'],
            'verification_code': self.transport.code(c['challenge_id'])}); self.assertEqual(status, 200)
        status, done = request(route+'/confirm', {'purpose': PURPOSE, 'recovery_id': c['recovery_id'],
            'recovery_token': p['recovery_token'], 'confirmed': True})
        self.assertEqual(status, 200); self.assertEqual(done['state'], 'recovered')


class RecoveryTransportTests(unittest.TestCase):
    def test_explicit_recovery_template_and_provider_scheme_do_not_change_old_purposes(self):
        f = sdk_fixture.AliyunTransportTests(); f.setUp(); self.addCleanup(f.tearDown)
        self.assertTrue(f.transport.configured); self.assertFalse(f.transport.supports_purpose('financial_recovery'))
        with self.assertRaises(SmsFailure): f.send(purpose='financial_recovery')
        self.assertEqual(f.client.sent, [])
        configured = dict(f.config, templates=dict(f.config['templates'], financial_recovery='100006'))
        transport = AliyunSmsTransport(configured, client=f.client, clock=lambda: f.now)
        self.assertTrue(transport.supports_purpose('financial_recovery'))
        f.send(transport, purpose='operation'); f.send(transport, purpose='financial_recovery')
        operation, recovery = f.client.sent
        self.assertNotEqual(operation.scheme_name, recovery.scheme_name)
        self.assertEqual(operation.template_code, '100005'); self.assertEqual(recovery.template_code, '100006')
        self.assertIs(recovery.return_verify_code, False)
        self.assertTrue(transport.verify('13800000001', 'financial_recovery', 'a'*32, '123456', deadline=1010))


class RecoveryPurposeBoundaryTests(unittest.TestCase):
    """只验证原银行两类证明隔离；显式组件身份/渠道替身，不冒充注册身份验收。"""
    def setUp(self):
        import test_sms_permissions as native_fixture
        self.f = native_fixture.SmsPermissionTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.bank = self.f.bank; self.w = self.f.ws
        identity = {k: hashlib.sha256(k.encode()).hexdigest() for k in ('subject_ref','session_ref','phone_binding_hash')}
        self.f.provider.recovery_identity = lambda w: identity if w == self.w else None
        self.f.provider.send_recovery = lambda binding, **kw: True
        self.f.provider.verify_recovery = lambda binding, code, **kw: RecoveryVerified(
            binding, 'explicit-component-test', 'recovery-'+binding.challenge_id,
            self.f.clock(), binding.expires_at)
        self.r = self.bank.permissions.events.recovery

    def test_original_operation_and_recovery_tokens_cannot_authorize_each_other(self):
        import test_sms_permissions as native_fixture
        op = self.f.prepare(); original = self.f.grant(op)
        for _ in range(3): self.bank.permissions.events.rejected(self.w, BankError('FORGED_AUTHORIZATION','',403))
        def grant():
            c = self.r.start(self.w, {'purpose': PURPOSE, 'confirmed': True})
            proof = self.r.verify(self.w, c['recovery_id'], {'purpose': PURPOSE,
                'challenge_id': c['challenge_id'], 'verification_code': native_fixture.CODE})
            return {'recovery_id': c['recovery_id'], 'purpose': PURPOSE,
                'confirmed': True, 'recovery_token': proof['recovery_token']}
        payload = grant()
        with self.assertRaises(BankError):
            self.r.recover(self.w, dict(payload, recovery_token=original['verification_token']))
        self.assertTrue(self.bank.permissions.events.status(self.w)['financial_write_locked'])
        self.f.now[0] += 61; payload = grant(); self.r.recover(self.w, payload)
        before = self.bank.snapshot(self.w)['accounts']
        with self.assertRaises(BankError): self.f.confirm(op, payload['recovery_token'])
        self.assertEqual(self.bank.snapshot(self.w)['accounts'], before)
        self.assertEqual(self.bank.operation(self.w, op['id'])['state'], 'pending_confirmation')


if __name__ == '__main__':
    unittest.main()
