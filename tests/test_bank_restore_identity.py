"""原四账户离线夹具的恢复与卡片投影；非生产身份或真实短信验收。"""
from contextlib import closing
import json
import unittest

import test_connected_assets as connected_fixture
from test_sms_permissions import LocalSms,CODE
from simulated_bank import BankError
from simulated_bank.security_credentials import prepare,restore


class BankRestoreIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connected_fixture.ConnectedAssetsTests.setUpClass()
        cls.folder=connected_fixture.ConnectedAssetsTests.folder;cls.seed=connected_fixture.ConnectedAssetsTests.seed

    @classmethod
    def tearDownClass(cls):connected_fixture.ConnectedAssetsTests.tearDownClass()

    def setUp(self):
        connected_fixture.ConnectedAssetsTests.setUp(self)
        self.uid='retired_entrepreneur'
        self.g.bank.permissions.provider=LocalSms(self.g.bank.clock)
        self.g.bank.permissions.allow_test_provider=True

    def change_password(self):
        workspace,token=self.g.demo_sessions.create(self.uid)
        self.assertEqual(workspace,self.workspace)
        account=max(self.g.bank.snapshot(workspace)['accounts'],key=lambda a:a['balance_minor'])
        password='isolated-bank-restore-password-56'
        replacement=prepare(self.g.bank,workspace,{'new_password':password})
        view=self.g.prepare(workspace,{'action':'password_change','account_id':account['id'],
            'replacement_id':replacement['replacement_id'],'idempotency_key':'identity-password'})
        op=view['operation'];challenge=self.g.bank.start_sms_verification(workspace,op['id'])
        result=self.g.confirm(workspace,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
            'challenge_id':challenge['challenge_id'],'verification_code':CODE})
        self.assertTrue(result['sessions_revoked'])
        return token,password

    def test_restored_session_preserves_canonical_demo_alias_and_same_workspace_without_new_account(self):
        old,password=self.change_password()
        with closing(self.g.bank.connect()) as db:before=db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0]
        restored=restore(self.g.bank,{'workspace_id':self.workspace,'password':password})
        self.assertEqual(restored['account_id'],self.uid)
        identity=self.g.demo_sessions.describe(restored['session_token'])
        self.assertEqual(identity['user_id'],self.uid);self.assertGreater(identity['generation'],1)
        session=self.g.session_data(self.workspace,restored['session_token'])
        self.assertEqual(session['account_id'],self.uid);self.assertEqual(session['workspace_id'],self.workspace)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0],before)
            self.assertEqual(db.execute('SELECT revoked FROM demo_sessions WHERE token_hash=?',
                             (__import__('hashlib').sha256(old.encode()).hexdigest(),)).fetchone()[0],1)

    def test_restore_does_not_unlock_financial_writes_or_resurrect_old_session(self):
        old,password=self.change_password()
        with closing(self.g.bank.connect()) as db,db:
            db.execute('INSERT OR REPLACE INTO permission_security VALUES(?,3,1)',(self.workspace,))
        restored=restore(self.g.bank,{'workspace_id':self.workspace,'password':password})
        self.assertEqual(self.g.bank.session(restored['session_token']),(self.workspace,'human'))
        with self.assertRaises(BankError):self.g.bank.session(old)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT locked FROM permission_security WHERE workspace_id=?',(self.workspace,)).fetchone()[0],1)

    def test_unready_registry_rejects_restore_without_legacy_fallback_or_new_account(self):
        _,password=self.change_password()
        with closing(self.g.bank.connect()) as db,db:
            db.execute("UPDATE navren_demo_accounts SET status='pending' WHERE workspace_id=?",(self.workspace,))
            before=[tuple(r) for r in db.execute('SELECT token_hash,workspace_id,user_id,generation,revoked FROM demo_sessions ORDER BY token_hash')]
        with self.assertRaises(BankError) as error:restore(self.g.bank,{'workspace_id':self.workspace,'password':password})
        self.assertEqual(error.exception.code,'BANK_RESTORE_IDENTITY_UNAVAILABLE')
        with closing(self.g.bank.connect()) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT token_hash,workspace_id,user_id,generation,revoked FROM demo_sessions ORDER BY token_hash')],before)

    def test_client_alias_override_is_rejected(self):
        _,password=self.change_password()
        with self.assertRaises(BankError) as error:
            restore(self.g.bank,{'workspace_id':self.workspace,'password':password,'user_id':'tycoon'})
        self.assertEqual(error.exception.code,'INVALID_FIELDS')

    def test_actual_runtime_virtual_card_network_survives_connected_asset_projection(self):
        connected_fixture.ConnectedAssetsTests.migrate(self,apply=True)
        data=self.g.connected_assets.overview(self.workspace)
        account=next(a for a in data['data']['accounts'] if a['account_type']=='bank_settlement')
        op=self.g.prepare(self.workspace,{'action':'apply_card','account_id':account['account_id'],
            'requested_type':'virtual','requested_limit_minor':100000,'idempotency_key':'network-virtual'})['operation']
        result=self.g.confirm(self.workspace,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest']})
        card_id=result['operation']['result']['card_id']
        projected=next(c for c in self.g.connected_assets.overview(self.workspace)['data']['cards'] if c['card_id']==card_id)
        self.assertEqual(projected['network'],'SIM');self.assertEqual(projected['card_type'],'virtual')
        self.assertFalse(projected.get('registration_only',False));self.assertIsNone(projected['facility_id'])
        self.assertRegex(projected['masked_number'],r'^\*{4} \*{4} \*{4} [0-9]{4}$')


if __name__=='__main__':unittest.main(verbosity=2)
