"""正式模拟银行单账本权限；渠道为显式测试替身，零短信与付费调用。"""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import http.client
import os
from cryptography.fernet import Fernet

from simulated_bank import SimulatedBank, BankError
from simulated_bank.permission_policy import POLICY_VERSION
from simulated_bank.sms_mfa import SmsVerified

CODE = '975319'  # 隔离夹具，绝非真实短信验证码。


class LocalSms:
    source = 'test_double'
    def __init__(self, clock):
        self.clock = clock
        self.sends = 0
        self.verifications = 0
        self.after_verify = lambda: None
        self.transform = lambda proof: proof

    def send(self, binding, *, deadline):
        self.sends += 1
        return self.clock() < deadline

    def verify(self, binding, code, *, deadline):
        self.verifications += 1
        if code != CODE:
            return None
        proof = SmsVerified('local-test', 'proof-' + binding.challenge_id,
                            'fixture-registered-phone', binding, self.clock(), binding.expires_at)
        self.after_verify()
        return self.transform(proof)


class SmsPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-sms-permission-')
        self.addCleanup(self.temp.cleanup)
        self.now = [1791018000.0]
        self.clock = lambda: self.now[0]
        self.provider = LocalSms(self.clock)
        self.path = Path(self.temp.name) / 'bank.sqlite3'
        self.bank = SimulatedBank(self.path, clock=self.clock, sms_provider=self.provider,
                                  allow_test_sms_provider=True)
        credentials = self.bank.create_workspace()
        self.credentials = credentials
        self.ws = credentials['workspace_id']
        self.demo_code = credentials['human_verification_code']
        self.accounts = self.bank.snapshot(self.ws)['accounts']
        self.account = self.accounts[0]['id']
        self.beneficiary = next(r['id'] for r in self.bank.customer_archive(self.ws)['records']['beneficiaries']
                                if r['status'] == 'active' and r['verified'])
        self.serial = 0

    def prepare(self, amount=100001, *, account=None, workspace=None, beneficiary=None):
        self.serial += 1
        return self.bank.prepare(workspace or self.ws, {'action': 'transfer',
            'account_id': account or self.account, 'beneficiary_id': beneficiary or self.beneficiary,
            'amount_minor': amount, 'idempotency_key': 'sms-' + str(self.serial)})

    def deny(self, code, fn):
        with self.assertRaises(BankError) as error:
            fn()
        self.assertEqual(error.exception.code, code)

    def grant(self, operation):
        challenge = self.bank.start_sms_verification(self.ws, operation['id'])
        return self.bank.verify(self.ws, operation['id'], {'verification_code': CODE,
                                                        'challenge_id': challenge['challenge_id']})

    def confirm(self, operation, token=None):
        body = {'confirmed': True, 'confirmation_digest': operation['confirmation_digest']}
        if token is not None:
            body['verification_token'] = token
        return self.bank.confirm(self.ws, operation['id'], body)

    def financial_state(self):
        with closing(self.bank.connect()) as db:
            return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                    for table in ['accounts', 'transactions', 'orders', 'permission_sms_proofs']}

    def test_default_unconfigured_never_uses_demo_pin(self):
        self.bank = SimulatedBank(self.path, clock=self.clock)
        op = self.prepare()
        before = self.financial_state()
        self.deny('SMS_MFA_NOT_CONFIGURED', lambda: self.bank.start_sms_verification(self.ws, op['id']))
        self.deny('SMS_MFA_NOT_CONFIGURED', lambda: self.bank.verify(self.ws, op['id'],
                  {'verification_code': self.demo_code, 'challenge_id': 'made-up'}))
        self.deny('SMS_MFA_NOT_CONFIGURED', lambda: self.confirm(op))
        self.assertEqual(before, self.financial_state())

        self.assertEqual((self.provider.sends, self.provider.verifications), (0, 0))

    def test_test_provider_rejected_unless_server_constructor_enables_it(self):
        self.bank = SimulatedBank(self.path, clock=self.clock, sms_provider=self.provider)
        op = self.prepare()
        self.deny('SMS_MFA_NOT_CONFIGURED', lambda: self.bank.start_sms_verification(self.ws, op['id']))
        self.assertEqual(self.provider.sends, 0)

    def test_yellow_exact_threshold_executes_once_without_sms(self):
        op = self.prepare(100000)
        self.assertEqual(op['permission'], 'yellow')
        result = self.confirm(op)
        after = self.financial_state()
        self.assertEqual(result, self.confirm(op))
        self.assertEqual(after, self.financial_state())
        self.assertEqual(self.provider.sends, 0)

    def test_cross_accounts_daily_cumulative_and_other_subject_isolation(self):
        self.confirm(self.prepare(60000))
        self.assertEqual(self.prepare(40000, account=self.accounts[1]['id'])['permission'], 'yellow')
        self.assertEqual(self.prepare(40001, account=self.accounts[1]['id'])['permission'], 'red')
        other = self.bank.create_workspace()['workspace_id']
        account = self.bank.snapshot(other)['accounts'][0]['id']
        beneficiary = next(r['id'] for r in self.bank.customer_archive(other)['records']['beneficiaries']
                           if r['status'] == 'active' and r['verified'])
        self.assertEqual(self.prepare(100000, workspace=other, account=account,
                                     beneficiary=beneficiary)['permission'], 'yellow')

    def test_old_yellow_over_threshold_requires_new_grade_no_silent_upgrade(self):
        old = self.prepare(60000)
        self.confirm(self.prepare(50000))
        self.deny('REPREPARE_WITH_CURRENT_GRADE', lambda: self.confirm(old))
        self.assertEqual(self.bank.operation(self.ws, old['id'])['state'], 'invalidated')
        self.assertEqual(self.prepare(60000)['permission'], 'red')

    def test_local_bound_proof_one_transaction_and_original_receipt(self):
        op = self.prepare()
        grant = self.grant(op)
        self.assertEqual(grant['source'], 'test_double')
        self.assertFalse(grant['real_SMS_acceptance'])
        result = self.confirm(op, grant['verification_token'])
        self.assertEqual(result['state'], 'succeeded')
        before = self.financial_state()
        self.assertEqual(result, self.confirm(op, grant['verification_token']))
        self.assertEqual(before, self.financial_state())
        with closing(self.bank.connect()) as db:
            p = db.execute('SELECT * FROM permission_sms_proofs').fetchone()
            self.assertIsNotNone(p['consumed_at'])
            events = [r[0] for r in db.execute('SELECT event FROM audit WHERE operation_id=?', (op['id'],))]
        self.assertIn('sms_proof_consumed', events)
        self.assertIn('permission_executed', events)

    def test_verification_returned_after_expiry_cannot_issue_proof(self):
        op = self.prepare()
        self.provider.after_verify = lambda: self.now.__setitem__(0, self.now[0] + 121)
        before = self.financial_state()
        self.deny('SMS_VERIFICATION_FAILED', lambda: self.grant(op))
        self.assertEqual(before, self.financial_state())

    def test_business_day_changes_during_provider_verification(self):
        op = self.prepare()
        def change_day():
            with closing(self.bank.connect()) as db, db:
                db.execute("UPDATE workspaces SET as_of='2026-10-04' WHERE id=?", (self.ws,))
        self.provider.after_verify = change_day
        before = self.financial_state()
        self.deny('BUSINESS_DAY_CHANGED', lambda: self.grant(op))
        self.assertEqual(before, self.financial_state())

    def test_changed_amount_or_payee_binding_and_forged_boolean_rejected(self):
        op = self.prepare()
        self.provider.transform = lambda p: replace(p, binding=replace(p.binding, summary_sha256='0' * 64))
        self.deny('SMS_VERIFICATION_FAILED', lambda: self.grant(op))
        self.deny('INVALID_FIELDS', lambda: self.bank.confirm(self.ws, op['id'], {
            'confirmed': True, 'confirmation_digest': op['confirmation_digest'], 'mfa_verified': True}))
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM permission_sms_proofs').fetchone()[0], 0)

    def test_three_bad_formats_persist_lock_across_reopen_and_elapsed_time(self):
        op = self.prepare()
        for _ in range(3):
            self.deny('SMS_CODE_INVALID', lambda: self.bank.verify(self.ws, op['id'],
                       {'verification_code': 'wrong', 'challenge_id': 'wrong'}))
        self.now[0] += 3600
        self.bank = SimulatedBank(self.path, clock=self.clock)
        self.deny('FINANCIAL_WRITE_LOCKED', lambda: self.prepare(1))
        self.bank.snapshot(self.ws)  # 授权查询保持可用。

    def test_original_receipt_recovery_allowed_during_persistent_lock(self):
        op = self.prepare(100)
        result = self.confirm(op)
        with closing(self.bank.connect()) as db, db:
            db.execute('INSERT INTO permission_security VALUES(?,3,1)', (self.ws,))
        self.assertEqual(result, self.confirm(op))

    def test_proof_not_reusable_for_other_operation_or_subject(self):
        original = self.prepare()
        grant = self.grant(original)
        other = self.prepare()
        self.deny('SMS_PROOF_REQUIRED', lambda: self.confirm(other, grant['verification_token']))
        other_ws = self.bank.create_workspace()['workspace_id']
        self.deny('NOT_FOUND', lambda: self.bank.confirm(other_ws, original['id'], {
            'confirmed': True, 'confirmation_digest': original['confirmation_digest'],
            'verification_token': grant['verification_token']}))

    def test_expired_proof_and_clock_rollback_rejected(self):
        op = self.prepare()
        token = self.grant(op)['verification_token']
        self.now[0] += 120
        self.deny('SMS_PROOF_REQUIRED', lambda: self.confirm(op, token))
        self.now[0] -= 200
        self.deny('TRUSTED_CLOCK_INVALID', lambda: self.confirm(op, token))

    def test_execution_failure_rolls_back_funds_receipt_and_proof_consumption(self):
        op = self.prepare()
        token = self.grant(op)['verification_token']
        before = self.financial_state()
        with patch.object(self.bank, '_ledger', side_effect=RuntimeError('fixture ledger fault')):
            with self.assertRaises(RuntimeError):
                self.confirm(op, token)
        self.assertEqual(before, self.financial_state())
        self.assertEqual(self.bank.operation(self.ws, op['id'])['state'], 'pending_confirmation')
        with closing(self.bank.connect()) as db:
            failures=[json.loads(r[0]) for r in db.execute("SELECT details FROM audit WHERE operation_id=? AND event='permission_attempt_failed'",(op['id'],))]
        self.assertEqual(failures[-1]['code'],'EXECUTION_FAILED')
        self.assertEqual(failures[-1]['original_operation_state'],'pending_confirmation')
        self.assertNotIn('fixture ledger fault',json.dumps(failures))
        self.assertEqual(self.confirm(op, token)['state'], 'succeeded')

    def test_concurrent_confirm_consumes_once(self):
        op = self.prepare()
        token = self.grant(op)['verification_token']
        results = []
        errors = []
        def execute():
            try: results.append(self.confirm(op, token))
            except Exception as e: errors.append(type(e).__name__)
        threads = [threading.Thread(target=execute) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM transactions WHERE operation_id=?', (op['id'],)).fetchone()[0], 1)

    def test_otp_provider_references_and_phone_not_stored_or_audited(self):
        op = self.prepare()
        token = self.grant(op)['verification_token']
        self.confirm(op, token)
        with closing(self.bank.connect()) as db:
            data = json.dumps({t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in
                               ['permission_bindings', 'permission_sms_challenges', 'permission_sms_proofs', 'audit']})
        self.assertNotIn(CODE, data)
        self.assertNotIn(token, data)
        self.assertNotIn('fixture-registered-phone', data)
        self.assertNotIn('proof-', data)

    def test_provider_return_past_request_deadline_refused_even_before_challenge_expiry(self):
        op = self.prepare()
        self.provider.after_verify = lambda: self.now.__setitem__(0, self.now[0] + 11)
        before = self.financial_state()
        self.deny('SMS_VERIFICATION_FAILED', lambda: self.grant(op))
        self.assertEqual(before, self.financial_state())

    def test_provider_proof_reference_replay_between_two_challenges_refused(self):
        self.provider.transform = lambda p: replace(p, proof_reference='same-proof')
        self.grant(self.prepare())
        self.now[0] += 60
        self.deny('SMS_PROOF_REPLAY', lambda: self.grant(self.prepare()))

    def test_actual_stored_payload_tamper_cannot_execute_with_old_proof(self):
        op = self.prepare()
        token = self.grant(op)['verification_token']
        with closing(self.bank.connect()) as db, db:
            payload = json.loads(db.execute('SELECT payload FROM operations WHERE id=?', (op['id'],)).fetchone()[0])
            payload['amount_minor'] += 1
            db.execute('UPDATE operations SET payload=? WHERE id=?', (json.dumps(payload), op['id']))
        before = self.financial_state()
        self.deny('PERMISSION_BINDING_INVALID', lambda: self.confirm(op, token))
        self.assertEqual(before, self.financial_state())


    def test_virtual_card_yellow_actual_issue_and_idempotent_receipt(self):
        before = self.bank.snapshot(self.ws)['total_cash_minor']
        op = self.bank.prepare(self.ws, {'action':'apply_card','account_id':self.account,
            'requested_type':'virtual','requested_limit_minor':200000,'idempotency_key':'virtual'})
        self.assertEqual(op['permission'],'yellow')
        result = self.confirm(op)
        self.assertEqual(result['result']['status'],'issued')
        card = next(c for c in self.bank.customer_archive(self.ws)['records']['cards'] if c['id']==result['result']['card_id'])
        self.assertEqual(card['type'],'virtual'); self.assertEqual(card['credit_limit_minor'],0)
        self.assertEqual(before,self.bank.snapshot(self.ws)['total_cash_minor'])
        self.assertEqual(result,self.confirm(op))

    def test_unblock_uses_red_proof_same_card_and_does_not_reenable_controls(self):
        card = next(c for c in self.bank.customer_archive(self.ws)['records']['cards']
                    if c['status']=='active')
        self.account=card['account_id']
        lost = self.bank.prepare(self.ws, {'action':'report_card_lost','account_id':self.account,
            'card_id':card['id'],'idempotency_key':'lost'})
        self.confirm(lost,self.grant(lost)['verification_token'])
        self.now[0] += 60
        op = self.bank.prepare(self.ws, {'action':'unblock_card','account_id':self.account,
            'card_id':card['id'],'idempotency_key':'unblock'})
        self.assertEqual(op['permission'],'red')
        result = self.confirm(op,self.grant(op)['verification_token'])
        self.assertFalse(result['result']['controls_reenabled'])
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT status FROM cards WHERE id=?',(card['id'],)).fetchone()[0],'active')
            self.assertEqual(db.execute('SELECT SUM(enabled) FROM card_controls WHERE card_id=?',(card['id'],)).fetchone()[0],0)

    def test_password_changes_hash_only_revoke_old_bank_sessions_and_pending_drafts(self):
        from simulated_bank.security_credentials import prepare, restore
        old = self.prepare()
        password = 'isolated-fixture-password-94'
        replacement = prepare(self.bank,self.ws,{'new_password':password})
        self.assertNotIn(password,json.dumps(replacement))
        op = self.bank.prepare(self.ws, {'action':'password_change','account_id':self.account,
            'replacement_id':replacement['replacement_id'],'idempotency_key':'change-password'})
        self.assertEqual(op['permission'],'red')
        result = self.confirm(op,self.grant(op)['verification_token'])
        self.assertTrue(result['result']['sessions_revoked'])
        self.deny('UNAUTHENTICATED',lambda:self.bank.session(self.credentials['session_token']))
        self.deny('UNAUTHENTICATED',lambda:self.bank.session(self.credentials['agent_token']))
        self.assertEqual(self.bank.operation(self.ws,old['id'])['state'],'invalidated')
        restored=restore(self.bank,{'workspace_id':self.ws,'password':password})
        self.assertEqual(self.bank.session(restored['session_token']),(self.ws,'human'))
        with closing(self.bank.connect()) as db:
            raw=json.dumps({t:[tuple(r) for r in db.execute('SELECT * FROM '+t)]
                            for t in ['operations','audit','bank_password_drafts','bank_access_credentials']},default=lambda v:v.hex())
        self.assertNotIn(password,raw)

    def test_password_plaintext_in_operation_payload_is_rejected(self):
        self.deny('INVALID_FIELDS',lambda:self.bank.prepare(self.ws,{'action':'password_change',
            'account_id':self.account,'idempotency_key':'unsafe-password','new_password':'never-in-a-draft'}))

    def test_subscription_cancellation_is_yellow_and_original_business_receipt(self):
        subscription=next(s for s in self.bank.customer_archive(self.ws)['records']['subscriptions'] if s['merchant_status']=='active')
        op=self.bank.prepare(self.ws,{'action':'cancel_subscription','account_id':subscription['account_id'],
            'subscription_id':subscription['id'],'idempotency_key':'yellow-subscription'})
        self.assertEqual(op['permission'],'yellow')
        result=self.confirm(op)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(self.provider.sends,0)
        self.assertEqual(result,self.confirm(op))


class SmsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import create_server, ProxyHandler
        cls.temp = tempfile.TemporaryDirectory(prefix='navren-sms-http-')
        cls.addClassCleanup(cls.temp.cleanup)
        environment = patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_PUBLIC_DATA_DIR': cls.temp.name + '/public', 'NAVREN_PERSONAL_DATA_DB': cls.temp.name + '/personal.sqlite3',
            'NAVREN_KNOWLEDGE_DB': cls.temp.name + '/knowledge.sqlite3'})
        environment.start(); cls.addClassCleanup(environment.stop)
        quiet = patch.object(ProxyHandler, 'log_message', lambda *args: None)
        quiet.start(); cls.addClassCleanup(quiet.stop)
        class NoModel:
            def process(self, *args, **kwargs):
                raise AssertionError('SMS must not enter a model')
        cls.server = create_server(0, Path(cls.temp.name) / 'bank.sqlite3',
                                  conversation_agent=NoModel(), public_sync_enabled=False)
        cls.g = cls.server.finance
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join, 3)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def request(self, method, path, payload=None, *, csrf=True, cookie=None):
        headers = {'Content-Type': 'application/json'}
        if getattr(self, 'cookie', None): headers['Cookie'] = self.cookie if cookie is None else cookie
        if csrf and getattr(self, 'csrf', None): headers['X-QZ-CSRF'] = self.csrf
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request(method, path, None if payload is None else json.dumps(payload), headers)
            response = conn.getresponse(); data = json.loads(response.read())
            if response.getheader('Set-Cookie'): self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            return response.status, data
        finally: conn.close()

    def setUp(self):
        self.cookie = self.csrf = None
        status, session = self.request('POST', '/api/finance/session', {})
        self.assertEqual(status, 200)
        self.ws = session['workspace_id']; self.csrf = session['csrf']
        state = self.g.overview(self.ws)
        self.account = state['accounts'][0]['id']
        self.beneficiary = next(r['id'] for r in state['beneficiaries'] if r['status'] == 'active' and r['verified'])

    def prepare(self, amount):
        status, view = self.request('POST', '/api/finance/operations/prepare', {
            'action': 'transfer', 'account_id': self.account, 'beneficiary_id': self.beneficiary,
            'amount_minor': amount, 'idempotency_key': 'http-sms'})
        self.assertEqual(status, 200, view)
        return view

    def test_default_formal_http_red_without_provider_is_explicitly_denied(self):
        view = self.prepare(100001); op = view['operation']
        self.assertFalse(view['strong_verification']['channel_configured'])
        before = self.g.bank.snapshot(self.ws)['total_cash_minor']
        status, result = self.request('POST', '/api/finance/operations/' + op['id'] + '/sms', {})
        self.assertEqual(status, 403, result)
        self.assertEqual(result['error']['code'], 'SMS_MFA_NOT_CONFIGURED')
        status, result = self.request('POST', '/api/finance/operations/' + op['id'] + '/confirm',
            {'confirmed': True, 'confirmation_digest': op['confirmation_digest']})
        self.assertEqual(status, 403, result)
        self.assertEqual(result['error']['code'], 'SMS_MFA_NOT_CONFIGURED')
        self.assertEqual(before, self.g.bank.snapshot(self.ws)['total_cash_minor'])

    def test_default_http_yellow_explicit_confirm_and_same_receipt_retry(self):
        op = self.prepare(100000)['operation']
        path = '/api/finance/operations/' + op['id'] + '/confirm'
        body = {'confirmed': True, 'confirmation_digest': op['confirmation_digest']}
        status, result = self.request('POST', path, body)
        self.assertEqual(status, 200, result)
        before = self.g.bank.snapshot(self.ws)['total_cash_minor']
        status, repeat = self.request('POST', path, body)
        self.assertEqual(status, 200, repeat)
        self.assertEqual(result['operation'], repeat['operation'])
        self.assertEqual(before, self.g.bank.snapshot(self.ws)['total_cash_minor'])

    def test_sms_http_retains_csrf_and_rejects_client_destination_or_verified_flag(self):
        op = self.prepare(100001)['operation']; path = '/api/finance/operations/' + op['id'] + '/sms'
        status, _ = self.request('POST', path, {}, csrf=False)
        self.assertEqual(status, 403)
        status, result = self.request('POST', path, {'phone': 'client-selected', 'verified': True})
        self.assertEqual(status, 400, result)
        self.assertEqual(result['error']['code'], 'INVALID_FIELDS')

    def test_runtime_binding_same_request_same_transaction_and_forgery_refused(self):
        from base_runtime.runtime_guards import RequestBinding
        token=self.cookie.partition('=')[2]
        with self.assertRaises(BankError):self.g.capture_request_binding(self.ws)
        with self.g.demo_sessions.request(token,self.ws):
            binding=self.g.capture_request_binding(self.ws)
            with self.g.bank.transaction() as db:
                result=self.g.revalidate_request_binding(binding,bank_db=db)
                self.assertTrue(result['same_bank_transaction'])
                with self.assertRaises(BankError):self.g.revalidate_request_binding(RequestBinding('unissued'),bank_db=db)
                self.assertTrue(db.in_transaction)
        with self.g.bank.transaction() as db:
            with self.assertRaises(BankError):self.g.revalidate_request_binding(binding,bank_db=db)

    def test_runtime_binding_privacy_drift_same_thread_is_rejected(self):
        token=self.cookie.partition('=')[2]
        with self.g.demo_sessions.request(token,self.ws):
            binding=self.g.capture_request_binding(self.ws)
            with self.g.bank.transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS conversation_privacy_versions('
                           'workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),revision INTEGER NOT NULL)')
                db.execute('INSERT INTO conversation_privacy_versions VALUES(?,1) '
                           'ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1',(self.ws,))
                with self.assertRaises(BankError):self.g.revalidate_request_binding(binding,bank_db=db)

    def test_history_buffer_cleared_before_guarded_write_and_clean_error_status(self):
        from server import ProxyHandler
        handler=object.__new__(ProxyHandler);handler._headers_buffer=[b'HTTP/1.1 200 OK\r\n']
        class Writer:
            def write(inner,body):
                self.assertEqual(handler._headers_buffer,[])
                raise BankError('STALE_MARKET_EVIDENCE','fixture source revoked',409)
        handler.wfile=Writer();handler.connection=type('Connection',(),{'published':False})()
        with self.assertRaises(BankError) as caught:handler.flush_headers()
        replies=[];handler._finance_json=lambda status,body:replies.append((status,body))
        handler._history_failure(caught.exception)
        self.assertEqual(len(replies),1);self.assertEqual(replies[0][0],409)
        self.assertEqual(handler._headers_buffer,[])

    def test_history_already_published_headers_close_without_second_response(self):
        from server import ProxyHandler
        handler=object.__new__(ProxyHandler);handler._headers_buffer=[b'pending second headers']
        calls=[]
        handler.connection=type('Connection',(),{'published':True,'shutdown':lambda inner,mode:calls.append(mode)})()
        handler._finance_json=lambda *args:self.fail('must never append a second HTTP status')
        handler._history_failure(BankError('STALE_MARKET_EVIDENCE','fixture',409))
        self.assertTrue(handler.close_connection);self.assertEqual(len(calls),1)
        self.assertEqual(handler._headers_buffer,[])

    def test_raw_history_marker_denied_before_any_private_read(self):
        with (patch.object(self.g.wealth,'provision',side_effect=AssertionError('private wealth must not be read')),
              patch.object(self.g.financial_metrics,'snapshot',side_effect=AssertionError('private snapshot must not be read'))):
            status,result=self.request('POST','/api/agent',{'text':'复核这篇旧文章',
                'market_context':{'kind':'article','id':'untrusted','parameters':{'reading_mode':'historical_audit'}}})
        self.assertEqual(status,403,result)
        self.assertEqual(result['error']['code'],'HISTORY_CONTEXT_REQUIRED')


class SimBankSMSHTTPTests(unittest.TestCase):
    def setUp(self):
        from simulated_bank.http_api import create_server
        self.temp=tempfile.TemporaryDirectory(prefix='navren-sim-sms-http-');self.addCleanup(self.temp.cleanup)
        self.provider=LocalSms(lambda:1791018000.0)
        self.bank=SimulatedBank(Path(self.temp.name)/'bank.sqlite3',clock=lambda:1791018000.0,
                                sms_provider=self.provider,allow_test_sms_provider=True)
        self.server=create_server(self.bank,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.thread.join,3);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        status,self.credentials=self.request('/api/sim-bank/workspaces',{})
        self.assertEqual(status,201)
        self.ws=self.credentials['workspace_id']; self.token=self.credentials['session_token']

    def request(self,path,payload,*,token=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        headers={'Content-Type':'application/json'}
        if token or getattr(self,'token',None):headers['Authorization']='Bearer '+(token or self.token)
        try:
            conn.request('POST',path,json.dumps(payload),headers);response=conn.getresponse()
            return response.status,json.loads(response.read())
        finally:conn.close()

    def test_actual_sim_bank_challenge_verify_confirm_routes_and_agent_denial(self):
        account=self.bank.snapshot(self.ws)['accounts'][0]['id']
        payee=next(b['id'] for b in self.bank.customer_archive(self.ws)['records']['beneficiaries'] if b['status']=='active' and b['verified'])
        status,op=self.request('/api/sim-bank/operations/prepare',{'action':'transfer','account_id':account,
            'beneficiary_id':payee,'amount_minor':100001,'idempotency_key':'sim-sms'})
        self.assertEqual(status,200,op)
        path='/api/sim-bank/operations/'+op['id']
        status,result=self.request(path+'/sms',{},token=self.credentials['agent_token'])
        self.assertEqual(status,403,result);self.assertEqual(self.provider.sends,0)
        status,challenge=self.request(path+'/sms',{});self.assertEqual(status,200,challenge)
        status,proof=self.request(path+'/verify',{'verification_code':CODE,'challenge_id':challenge['challenge_id']})
        self.assertEqual(status,200,proof)
        status,result=self.request(path+'/confirm',{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
                                                  'verification_token':proof['verification_token']})
        self.assertEqual(status,200,result);self.assertEqual(result['state'],'succeeded')


class SmsConfiguredHTTPTests(unittest.TestCase):
    request=SmsHTTPTests.request
    setUp=SmsHTTPTests.setUp

    @classmethod
    def setUpClass(cls):
        import time
        from server import create_server, ProxyHandler
        from base_runtime.service_assembly import AssembledGateway
        cls.temp=tempfile.TemporaryDirectory(prefix='navren-sms-configured-http-');cls.addClassCleanup(cls.temp.cleanup)
        environment=patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB':cls.temp.name+'/knowledge.sqlite3','NAVREN_PUBLIC_DATA_DIR':cls.temp.name+'/public',
            'NAVREN_MESSAGE_WORKER_ENABLED':'false'})
        environment.start();cls.addClassCleanup(environment.stop)
        quiet=patch.object(ProxyHandler,'log_message',lambda *args:None);quiet.start();cls.addClassCleanup(quiet.stop)
        cls.provider=LocalSms(time.time)
        class NoModel:
            def process(self,*args,**kwargs):raise AssertionError('password form must not enter model')
        cls.server=create_server(0,Path(cls.temp.name)/'bank.sqlite3',conversation_agent=NoModel(),public_sync_enabled=False,
            finance_factory=lambda p,**kwargs:AssembledGateway(p,sms_provider=cls.provider,allow_test_sms_provider=True,**kwargs))
        cls.g=cls.server.finance
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.addClassCleanup(cls.thread.join,3);cls.addClassCleanup(cls.server.server_close);cls.addClassCleanup(cls.server.shutdown)

    def test_actual_gateway_password_dedicated_form_sms_confirm_revoke_and_restore(self):
        password='isolated-form-password-48'
        status,replacement=self.request('POST','/api/finance/security/password-drafts',{'new_password':password})
        self.assertEqual(status,200,replacement);self.assertNotIn(password,json.dumps(replacement))
        status,view=self.request('POST','/api/finance/operations/prepare',{'action':'password_change',
            'account_id':self.account,'replacement_id':replacement['replacement_id'],'idempotency_key':'password-form'})
        self.assertEqual(status,200,view);op=view['operation'];path='/api/finance/operations/'+op['id']
        status,challenge=self.request('POST',path+'/sms',{});self.assertEqual(status,200,challenge)
        old_cookie=self.cookie
        status,result=self.request('POST',path+'/confirm',{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
            'verification_code':CODE,'challenge_id':challenge['challenge_id']})
        self.assertEqual(status,200,result);self.assertTrue(result['sessions_revoked'])
        status,_=self.request('GET','/api/finance/session',cookie=old_cookie)
        self.assertNotEqual(status,200)
        status,restored=self.request('POST','/api/finance/security/password-session',{'workspace_id':self.ws,'password':password})
        self.assertEqual(status,200,restored);self.assertEqual(restored['workspace_id'],self.ws)
        self.assertNotIn(password,json.dumps(restored));self.assertNotIn('session_token',restored)
        status,current=self.request('GET','/api/finance/session')
        self.assertEqual(status,200,current);self.assertEqual(current['workspace_id'],self.ws)



if __name__ == '__main__':
    unittest.main(verbosity=2)
