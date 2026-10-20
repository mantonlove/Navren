"""隔离认证库验证短信绑定、跨场景拒绝及并发消费；没有真实短信调用。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from sms_aliyun import SmsFailure
from welcome_auth import AuthError, WelcomeAuth, create_welcome_server


PHONE = '13800000001'
OTHER_PHONE = '13900000002'
PASSWORD = 'private-test-password'


class Clock:
    def __init__(self): self.now = 1792036800.0
    def __call__(self): return self.now
    def advance(self, value=61): self.now += value


class TestTransport:
    source = 'test_double'
    configured = True
    daily_limit = 20
    phone_daily_limit = 10
    ip_hourly_limit = 20
    def __init__(self):
        self.sent = {}
        self.verify_calls = 0
        self.before_verify = None
    def permit_number(self, phone):
        if phone not in (PHONE, OTHER_PHONE): raise SmsFailure('TEST_ONLY', '仅测试号码。', 403)
    def code(self, reference):
        return str(int(hashlib.sha256(reference.encode()).hexdigest()[:10],16)%1000000).zfill(6)
    def send(self, phone, purpose, reference, **kwargs):
        self.sent[reference] = (phone,purpose)
        return 'send-' + reference
    def verify(self, phone, purpose, reference, code, **kwargs):
        self.verify_calls += 1
        if self.before_verify: self.before_verify()
        return 'proof-' + reference if self.sent.get(reference)==(phone,purpose) and code==self.code(reference) else None


class SmsAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock, self.transport = Clock(), TestTransport()
        self.path = Path(self.tmp.name)/'auth.sqlite3'
        self.auth = WelcomeAuth(self.path, self.clock, sms_transport=self.transport, allow_test_sms=True,
                                allow_unverified_registration=True)
    def tearDown(self): self.tmp.cleanup()
    def anonymous(self): return self.auth.new_session()[0]
    def send(self, token, purpose='register', phone=PHONE, **kw):
        return self.auth.send_sms(token,purpose,phone,**kw)['challenge_id']
    def register(self, phone=PHONE):
        token = self.anonymous(); challenge = self.send(token,phone=phone)
        return self.auth.sms_register(phone,PASSWORD,challenge,self.transport.code(challenge),token,password_confirmation=PASSWORD)

    def test_registration_verifies_phone_rotates_session_and_never_stores_code(self):
        token = self.anonymous(); challenge = self.send(token)
        result, session = self.auth.sms_register(PHONE,PASSWORD,challenge,self.transport.code(challenge),token,password_confirmation=PASSWORD)
        self.assertIsNone(self.auth.session(token))
        self.assertTrue(session['phone_verified'])
        self.assertEqual(self.auth.public_session(session)['sms']['masked_phone'],'138****0001')
        with self.auth.connect() as db:
            row = dict(db.execute('SELECT * FROM sms_challenges').fetchone())
            self.assertEqual(row['state'],'consumed')
            self.assertNotIn('verification_code',row)
            self.assertNotIn(self.transport.code(challenge),json.dumps(row))
        self.assertTrue(self.auth.session(result))

    def test_register_code_cannot_be_used_for_login_or_a_different_browser(self):
        token = self.anonymous(); challenge = self.send(token)
        with self.assertRaises(SmsFailure):
            self.auth.sms_login(PHONE,challenge,self.transport.code(challenge),token)
        other = self.anonymous()
        with self.assertRaises(SmsFailure):
            self.auth.sms_register(PHONE,PASSWORD,challenge,self.transport.code(challenge),other,password_confirmation=PASSWORD)
        self.assertEqual(self.transport.verify_calls,0)

    def test_sms_login_and_replay(self):
        registered, user = self.register()
        self.auth.logout(registered)
        self.clock.advance()
        token = self.anonymous(); challenge = self.send(token,'login')
        logged, session = self.auth.sms_login(PHONE,challenge,self.transport.code(challenge),token)
        self.assertEqual(session['user_id'],user['user_id'])
        with self.assertRaises((SmsFailure,AuthError)):
            self.auth.sms_login(PHONE,challenge,self.transport.code(challenge),token)
        self.assertTrue(self.auth.session(logged))

    def test_expiry_and_five_wrong_attempts(self):
        token = self.anonymous(); challenge = self.send(token)
        wrong = '999999' if self.transport.code(challenge)!='999999' else '111111'
        for _ in range(5):
            with self.assertRaises(SmsFailure): self.auth.sms_register(PHONE,PASSWORD,challenge,wrong,token,password_confirmation=PASSWORD)
        with self.assertRaises(SmsFailure):
            self.auth.sms_register(PHONE,PASSWORD,challenge,self.transport.code(challenge),token,password_confirmation=PASSWORD)
        self.assertEqual(self.transport.verify_calls,5)
        self.clock.advance()
        fresh = self.send(token)
        self.clock.advance(301)
        with self.assertRaises(SmsFailure):
            self.auth.sms_register(PHONE,PASSWORD,fresh,self.transport.code(fresh),token,password_confirmation=PASSWORD)

    def test_old_local_phone_account_needs_password_before_binding(self):
        anonymous = self.anonymous()
        token, session = self.auth.register(PHONE,PASSWORD,anonymous)
        with self.assertRaises(AuthError):
            self.auth.send_sms(token,'bind',PHONE,password='incorrect-password')
        self.assertEqual(len(self.transport.sent),0)
        challenge = self.send(token,'bind',password=PASSWORD)
        with self.assertRaises(AuthError):
            self.auth.sms_bind(PHONE,'incorrect-password',challenge,self.transport.code(challenge),token)
        logged, fresh = self.auth.sms_bind(PHONE,PASSWORD,challenge,self.transport.code(challenge),token)
        self.assertEqual(fresh['user_id'],session['user_id'])
        self.assertTrue(fresh['phone_verified'])
        self.assertIsNone(self.auth.session(token))

    def test_two_concurrent_registrations_only_create_one_identity(self):
        token = self.anonymous(); challenge = self.send(token)
        barrier = threading.Barrier(2)
        def request():
            barrier.wait()
            try:
                return self.auth.sms_register(PHONE,PASSWORD,challenge,self.transport.code(challenge),token,password_confirmation=PASSWORD)[1]['user_id']
            except (SmsFailure,AuthError): return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _:request(),range(2)))
        self.assertEqual(sum(x is not None for x in results),1)
        with self.auth.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM welcome_users').fetchone()[0],1)

    def test_revoked_browser_during_provider_call_cannot_finish_registration(self):
        token = self.anonymous(); challenge = self.send(token)
        self.transport.before_verify = lambda:self.auth.logout(token)
        with self.assertRaises(AuthError):
            self.auth.sms_register(PHONE,PASSWORD,challenge,self.transport.code(challenge),token,password_confirmation=PASSWORD)
        with self.auth.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sms_phone_bindings').fetchone()[0],0)

    def test_frequency_and_quotas_survive_new_auth_instance(self):
        self.transport.phone_daily_limit = 2
        token = self.anonymous(); self.send(token)
        with self.assertRaises(SmsFailure): self.send(token,'login')
        self.clock.advance(); self.send(token,'login')
        self.clock.advance()
        again = WelcomeAuth(self.path,self.clock,sms_transport=self.transport,allow_test_sms=True)
        with self.assertRaises(SmsFailure): again.send_sms(token,'login',PHONE)

    def test_test_transport_is_never_implicitly_enabled(self):
        normal = WelcomeAuth(Path(self.tmp.name)/'normal.sqlite3',self.clock,sms_transport=self.transport)
        self.assertFalse(normal.sms.configured)

    def test_provider_current_code_ignores_out_id_and_replaces_old_code(self):
        current = {}
        def send(phone,purpose,reference,**kwargs):
            current[phone,purpose] = self.transport.code(reference)
            return 'send-' + reference
        def verify(phone,purpose,reference,code,**kwargs):
            return 'proof-' + reference if current.get((phone,purpose)) == code else None
        self.transport.send, self.transport.verify = send, verify
        first = self.anonymous(); old = self.send(first)
        self.clock.advance()
        second = self.anonymous(); new = self.send(second)
        with self.assertRaises(SmsFailure):
            self.auth.sms_register(PHONE,PASSWORD,old,self.transport.code(new),first,password_confirmation=PASSWORD)
        with self.assertRaises(SmsFailure):
            self.auth.sms_register(PHONE,PASSWORD,new,self.transport.code(old),second,password_confirmation=PASSWORD)
        _, user = self.auth.sms_register(PHONE,PASSWORD,new,self.transport.code(new),second,password_confirmation=PASSWORD)
        self.assertTrue(user['phone_verified'])


class SmsHttpTests(unittest.TestCase):
    def test_missing_or_disabled_sms_never_downgrades_registration(self):
        from http.client import HTTPConnection
        for transport in (None, TestTransport()):
            with self.subTest(transport=type(transport).__name__), tempfile.TemporaryDirectory() as tmp:
                if transport: transport.configured = False
                instance = create_welcome_server(0,Path(tmp)/'auth.sqlite3',sms_transport=transport,allow_test_sms=True)
                worker = threading.Thread(target=instance.serve_forever,daemon=True); worker.start()
                try:
                    connection = HTTPConnection('127.0.0.1',instance.server_port,timeout=5)
                    connection.request('GET','/api/welcome/auth/session')
                    response = connection.getresponse(); cookie = response.getheader('Set-Cookie').split(';')[0]
                    session = json.loads(response.read()); connection.close()
                    self.assertTrue(session['sms']['registration_required'])
                    headers = {'Host':f'127.0.0.1:{instance.server_port}',
                        'Origin':f'http://127.0.0.1:{instance.server_port}', 'Cookie':cookie,
                        'Content-Type':'application/json','X-Welcome-CSRF':session['csrf']}
                    connection = HTTPConnection('127.0.0.1',instance.server_port,timeout=5)
                    connection.request('POST','/api/welcome/auth/register',json.dumps({'phone':PHONE,'password':PASSWORD}),headers)
                    response = connection.getresponse(); data = json.loads(response.read()); connection.close()
                    self.assertEqual(response.status,503)
                    self.assertEqual(data['code'],'SMS_NOT_CONFIGURED')
                    with instance.auth.connect() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM welcome_users').fetchone()[0],0)
                    with self.assertRaises(AuthError): instance.auth.register(PHONE,PASSWORD,None)
                finally:
                    instance.shutdown(); instance.server_close(); worker.join(3)

    def test_csrf_required_registration_bypass_rejected_and_no_code_in_response(self):
        from http.client import HTTPConnection
        with tempfile.TemporaryDirectory() as tmp:
            transport=TestTransport()
            instance=create_welcome_server(0,Path(tmp)/'auth.sqlite3',sms_transport=transport,allow_test_sms=True)
            worker=threading.Thread(target=instance.serve_forever,daemon=True); worker.start()
            port=instance.server_port
            connection=HTTPConnection('127.0.0.1',port,timeout=5)
            try:
                connection.request('GET','/api/welcome/auth/session')
                response=connection.getresponse(); cookie=response.getheader('Set-Cookie').split(';')[0]
                session=json.loads(response.read())
                def post(path,payload,csrf=True):
                    headers={'Host':f'127.0.0.1:{port}','Origin':f'http://127.0.0.1:{port}',
                             'Cookie':cookie,'Content-Type':'application/json'}
                    if csrf: headers['X-Welcome-CSRF']=session['csrf']
                    client=HTTPConnection('127.0.0.1',port,timeout=5)
                    try:
                        client.request('POST','/api/welcome/auth/'+path,json.dumps(payload),headers)
                        response=client.getresponse(); return response.status,json.loads(response.read())
                    finally: client.close()
                self.assertEqual(post('sms/send',{'phone':PHONE,'purpose':'register'},False)[0],403)
                self.assertEqual(len(transport.sent),0)
                self.assertEqual(post('register',{'username':PHONE,'password':PASSWORD})[0],400)
                status,data=post('sms/send',{'phone':PHONE,'purpose':'register'})
                self.assertEqual(status,200)
                self.assertNotIn('verification_code',data)
                challenge=data['challenge_id']
                status,data=post('register',{'phone':PHONE,'password':PASSWORD,'challenge_id':challenge,
                                            'verification_code':transport.code(challenge),'password_confirmation':PASSWORD})
                self.assertEqual(status,201)
                self.assertTrue(data['sms']['phone_verified'])
            finally:
                connection.close(); instance.shutdown(); instance.server_close(); worker.join(3)


if __name__=='__main__': unittest.main()
