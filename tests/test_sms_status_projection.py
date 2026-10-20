"""真实注册实例+正式短信适配器的就绪投影；仅显式隔离transport。"""
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch

import test_registered_instances as registered_fixture
from test_sms_auth import TestTransport, PHONE, PASSWORD
from simulated_bank.aliyun_sms import AliyunOperationSmsProvider
from welcome_auth import WelcomeAuth
from simulated_bank import BankError


class SmsStatusProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = registered_fixture.RegisteredInstancesTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.g = self.f.g; self.transport = TestTransport()
        self.auth = WelcomeAuth(Path(self.f.temp.name)/'registered-auth.sqlite3', self.g.bank.clock,
            sms_transport=self.transport, allow_test_sms=True, allow_legacy_experience=False)
        anonymous = self.auth.new_session()[0]
        challenge = self.auth.send_sms(anonymous, 'register', PHONE)['challenge_id']
        self.auth_token, user = self.auth.sms_register(PHONE, PASSWORD, challenge,
            self.transport.code(challenge), anonymous, password_confirmation=PASSWORD)
        self.session = self.auth.session(self.auth_token)
        self.lease = self.g.experience_provider.select_registered(self.session['registered_user_id'], 'tycoon',
            self.auth.auth_subject(self.session), auth_expires_at=self.session['expires_at'])
        self.w = self.lease['workspace_id']; self.token = self.lease['upstream_cookies']['qz_bank_user']
        self.auth.update_upstream_cookies(user['user_id'], ['qz_bank_user='+self.token], session_token=self.auth_token)
        # 保留真实短信服务的发送间隔；注册短信后推进隔离时钟，不跳过限流。
        self.f.now[0] += 61
        self.provider = AliyunOperationSmsProvider(self.auth, self.g.bank, experience_provider=self.g.experience_provider)
        self.g.bank.permissions.provider = self.provider; self.g.bank.permissions.allow_test_provider = True
        with self.g.demo_sessions.request(self.token, self.w):
            card = next(c for c in self.g.overview(self.w)['cards'] if c['type']=='debit' and c['status']=='active')
            self.view = self.g.prepare_aa(self.w, {'account_id': card['account_id'], 'title': '隔离渠道投影验证',
                'total_minor': 2000, 'split_mode': 'equal', 'participants': [{'user_id': 'tycoon'}, {'user_id': 'grain_farmer'}],
                'idempotency_key': 'sms-ready-aa-fixture'})
        self.op = self.view['operation']

    def status(self):
        with self.g.demo_sessions.request(self.token, self.w), closing(self.g.bank.connect()) as db:
            return self.g.bank.permissions.status(db, self.w, self.op['id'])

    def test_real_registered_adapter_projection_and_sms_confirmation_agree(self):
        self.assertEqual(self.provider.source, 'test_double')
        with self.g.demo_sessions.request(self.token, self.w):
            self.assertTrue(self.provider.available(self.w))
            before = len(self.transport.sent)
            view = self.g.operation_view(self.w, self.op['id'])
            status = view['strong_verification']
            self.assertEqual(status['grade'], 'red'); self.assertEqual(self.op['permission'], 'red')
            self.assertTrue(status['channel_configured'], status)
            self.assertFalse(status['real_SMS_acceptance'])
            self.assertEqual(len(self.transport.sent), before, '只读投影不能发送短信')
            challenge = self.g.bank.start_sms_verification(self.w, self.op['id'])
            done = self.g.confirm(self.w, self.op['id'], {'confirmed': True,
                'confirmation_digest': self.op['confirmation_digest'], 'challenge_id': challenge['challenge_id'],
                'verification_code': self.transport.code(challenge['challenge_id'])})
            self.assertEqual(done['operation']['state'], 'succeeded')

    def test_unapproved_test_provider_missing_channel_and_unavailable_are_not_ready(self):
        self.g.bank.permissions.allow_test_provider = False
        self.assertFalse(self.status()['channel_configured'])
        with self.g.demo_sessions.request(self.token, self.w):
            with self.assertRaises(BankError) as caught: self.g.bank.start_sms_verification(self.w, self.op['id'])
        self.assertEqual(caught.exception.code, 'SMS_MFA_NOT_CONFIGURED')
        self.g.bank.permissions.allow_test_provider = True
        for outcome in (False, None):
            with patch.object(self.provider, 'available', return_value=outcome):
                self.assertFalse(self.status()['channel_configured'])
        with patch.object(self.provider, 'available', side_effect=RuntimeError('fixture')):
            self.assertFalse(self.status()['channel_configured'])
        with patch.object(self.g.bank.permissions, 'provider', None):
            self.assertFalse(self.status()['channel_configured'])

    def test_current_request_role_and_revoked_welcome_identity_are_not_ready(self):
        other = self.g.experience_provider.select_registered(self.session['registered_user_id'], 'grain_farmer',
            self.auth.auth_subject(self.session), auth_expires_at=self.session['expires_at'], expected_workspace_id=self.w)
        other_token = other['upstream_cookies']['qz_bank_user']
        with self.g.demo_sessions.request(other_token, other['workspace_id']), closing(self.g.bank.connect()) as db:
            self.assertFalse(self.g.bank.permissions.status(db, self.w, self.op['id'])['channel_configured'])
            with self.assertRaises(BankError): self.g.bank.permissions.status(db, other['workspace_id'], self.op['id'])
        # welcome注销后旧手机号不能被其他或旧工作区重新认领为已接通。
        self.auth.logout(self.auth_token)
        self.assertFalse(self.status()['channel_configured'])
