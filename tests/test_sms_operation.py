"""认证库与原银行账本之间的操作级接线；全部使用显式隔离替身。"""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from simulated_bank import BankError, SimulatedBank
from simulated_bank.aliyun_sms import AliyunOperationSmsProvider
from welcome_auth import WelcomeAuth
from test_sms_auth import Clock, TestTransport, PHONE, OTHER_PHONE, PASSWORD


class OperationSmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.transport = Clock(), TestTransport()
        root = Path(self.tmp.name)
        self.auth = WelcomeAuth(root/'auth.sqlite3',self.clock,
            sms_transport=self.transport,allow_test_sms=True,allow_legacy_experience=True)
        self.provider = AliyunOperationSmsProvider()
        self.bank = SimulatedBank(root/'bank.sqlite3',clock=self.clock,
            sms_provider=self.provider,allow_test_sms_provider=True)
        self.provider.attach(self.auth,self.bank)
        self.credentials = self.bank.create_workspace()
        self.ws = self.credentials['workspace_id']
        self.token, self.user = self.register(PHONE)
        self.auth.update_upstream_cookies(self.user['user_id'],
            ['qz_bank_user='+self.credentials['session_token']],session_token=self.token)
        self.clock.advance()
        self.account = self.bank.snapshot(self.ws)['accounts'][0]['id']
        self.beneficiary = next(row['id'] for row in
            self.bank.customer_archive(self.ws)['records']['beneficiaries']
            if row['status']=='active' and row['verified'])

    def register(self,phone):
        anonymous = self.auth.new_session()[0]
        challenge = self.auth.send_sms(anonymous,'register',phone)['challenge_id']
        return self.auth.sms_register(phone,PASSWORD,challenge,self.transport.code(challenge),anonymous,password_confirmation=PASSWORD)

    def prepare(self):
        return self.bank.prepare(self.ws,{'action':'transfer','account_id':self.account,
            'beneficiary_id':self.beneficiary,'amount_minor':100001,'idempotency_key':'sms-operation-test'})

    def grant(self,operation):
        challenge = self.bank.start_sms_verification(self.ws,operation['id'])
        return self.bank.verify(self.ws,operation['id'],{'challenge_id':challenge['challenge_id'],
            'verification_code':self.transport.code(challenge['challenge_id'])})

    def confirm(self,operation,proof):
        return self.bank.confirm(self.ws,operation['id'],{'confirmed':True,
            'confirmation_digest':operation['confirmation_digest'],'verification_token':proof['verification_token']})

    def test_bound_operation_executes_once_and_proof_has_no_phone_or_otp(self):
        self.assertTrue(self.provider.available(self.ws))
        operation = self.prepare(); proof = self.grant(operation)
        self.assertEqual(proof['source'],'test_double')
        self.assertFalse(proof['real_SMS_acceptance'])
        result = self.confirm(operation,proof)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result,self.confirm(operation,proof))
        with closing(self.bank.connect()) as db:
            row = dict(db.execute('SELECT * FROM permission_sms_proofs').fetchone())
            self.assertIsNotNone(row['consumed_at'])
            self.assertNotIn(PHONE,str(row))
            self.assertNotIn(self.transport.code(row['challenge_id']),str(row))

    def test_agent_token_and_shared_workspace_cannot_receive_sms(self):
        self.auth.update_upstream_cookies(self.user['user_id'],
            ['qz_bank_user='+self.credentials['agent_token']],session_token=self.token)
        self.assertFalse(self.provider.available(self.ws))
        operation = self.prepare()
        with self.assertRaises(BankError): self.bank.start_sms_verification(self.ws,operation['id'])
        self.auth.update_upstream_cookies(self.user['user_id'],
            ['qz_bank_user='+self.credentials['session_token']],session_token=self.token)
        other_token, other_user = self.register(OTHER_PHONE)
        self.auth.update_upstream_cookies(other_user['user_id'],
            ['qz_bank_user='+self.credentials['session_token']],session_token=other_token)
        self.assertFalse(self.provider.available(self.ws))

    def test_session_revoked_during_provider_verification_issues_no_bank_proof(self):
        operation = self.prepare()
        challenge = self.bank.start_sms_verification(self.ws,operation['id'])
        self.transport.before_verify = lambda:self.auth.logout(self.token)
        with self.assertRaises(BankError):
            self.bank.verify(self.ws,operation['id'],{'challenge_id':challenge['challenge_id'],
                'verification_code':self.transport.code(challenge['challenge_id'])})
        with closing(self.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM permission_sms_proofs').fetchone()[0],0)

    def test_binding_changed_after_verification_cannot_confirm(self):
        operation = self.prepare(); proof = self.grant(operation)
        with self.auth.connect() as db:
            db.execute('UPDATE sms_phone_bindings SET id=? WHERE user_id=?',('f'*32,self.user['user_id']))
        with self.assertRaises(BankError) as raised: self.confirm(operation,proof)
        self.assertEqual(raised.exception.code,'SMS_PHONE_BINDING_CHANGED')
        with closing(self.bank.connect()) as db:
            self.assertIsNone(db.execute('SELECT consumed_at FROM permission_sms_proofs').fetchone()[0])


if __name__=='__main__': unittest.main()
