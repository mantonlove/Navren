"""实际封存模板、独立临时银行/私库；不代表真实手机号注册或已部署。"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
from finance_gateway import FinanceGateway
from registered_instances import InstanceRegistry, registered_id, experience_identity
from simulated_bank import BankError
from test_sms_permissions import LocalSms,CODE

ROOT = Path(__file__).resolve().parents[1]
FINANCIAL = ROOT / 'outputs/realistic-history/navren-financial.sqlite3'


class RegisteredInstancesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-uss-instances-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bank.sqlite3'
        env = patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(Path(self.temp.name) / 'knowledge.sqlite3'),
            'NAVREN_MESSAGE_WORKER_ENABLED': 'false'})
        env.start(); self.addCleanup(env.stop)
        network = patch.object(socket, 'create_connection', side_effect=AssertionError('network_forbidden'))
        network.start(); self.addCleanup(network.stop)
        self.g = self.gateway()
        self.now = [datetime.fromisoformat('2026-10-16T12:00:00+08:00').timestamp()]
        self.g.bank.clock = lambda: self.now[0]
        self.owner = uuid.uuid4().hex
        self.subject = 'welcome-' + uuid.uuid4().hex
        self.expiry = self.now[0] + 900

    def gateway(self, registered=True):
        return FinanceGateway(self.path, financial_database=FINANCIAL if registered else
            ROOT / 'outputs/financial-data/navren-financial-v3.sqlite3', registered_mode=registered)

    def select(self, persona='tycoon', *, owner=None, subject=None, expected=None):
        return self.g.experience_provider.select_registered(owner or self.owner, persona, subject or self.subject,
            auth_expires_at=self.expiry, expected_workspace_id=expected)

    @staticmethod
    def token(lease):
        return lease['upstream_cookies']['qz_bank_user']

    def deny(self, code, callback):
        with self.assertRaises(BankError) as error: callback()
        self.assertEqual(error.exception.code, code)

    def test_canonical_uuid_and_registered_mode_reject_anonymous_bootstrap(self):
        self.assertEqual(registered_id(self.owner), self.owner)
        for value in (str(uuid.UUID(self.owner)), self.owner.upper(), uuid.uuid5(uuid.NAMESPACE_DNS, 'fixture').hex, 'phone-13800000000'):
            self.deny('REGISTERED_ID_REQUIRED', lambda: registered_id(value))
        self.deny('REGISTERED_LOGIN_REQUIRED', self.g.create_session)
        state = self.g.experience_provider.resume(self.owner, self.subject, auth_expires_at=self.expiry)
        self.assertTrue(state['selection_required']); self.assertIsNone(state['workspace_id'])
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0], 0)

    def test_existing_public_templates_and_two_private_tycoons_keep_products_separate(self):
        legacy = self.gateway(registered=False)
        legacy.demo_accounts.initialize_offline(allow_offline=True)
        with closing(legacy.bank.connect()) as db:
            public = [tuple(r) for r in db.execute("SELECT * FROM products WHERE id LIKE 'DEMO-%' ORDER BY id")]
            old_workspaces = [r[0] for r in db.execute('SELECT workspace_id FROM navren_demo_accounts ORDER BY user_id')]
        first = self.select()
        second = self.select(owner=uuid.uuid4().hex, subject='welcome-' + uuid.uuid4().hex)
        self.assertNotEqual(first['workspace_id'], second['workspace_id'])
        self.assertNotIn(first['workspace_id'], old_workspaces)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM products WHERE id LIKE 'DEMO-%' ORDER BY id")], public)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM registered_user_instances WHERE status='ready'").fetchone()[0], 8)
            products = [json.loads(r[0]) for r in db.execute("SELECT data FROM products WHERE id LIKE 'INSTANCE-%'")]
            self.assertTrue(products)
            self.assertTrue(all(p['workspace_id'] not in old_workspaces for p in products))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
        self.assertEqual(self.g.personal_data.read_history(first['workspace_id']), [])

    def test_same_owner_two_browsers_keep_persona_and_logout_scopes_independent(self):
        first = self.select('tycoon')
        other_subject = 'welcome-' + uuid.uuid4().hex
        second = self.select('working_landlady', subject=other_subject)
        restored = self.g.experience_provider.resume(self.owner, self.subject, auth_expires_at=self.expiry,
            bank_session_token=self.token(first))
        self.assertEqual(restored['workspace_id'], first['workspace_id'])
        self.g.experience_provider.revoke_subject(self.owner, self.subject)
        self.deny('SESSION_CHANGED', lambda: self.g.experience_provider.validate(self.token(first)))
        self.assertEqual(self.g.experience_provider.validate(self.token(second))['workspace_id'], second['workspace_id'])
        self.deny('WELCOME_SESSION_REVOKED', lambda: self.g.experience_provider.resume(self.owner, self.subject,
            auth_expires_at=self.expiry))

    def test_bank_lease_expires_with_real_welcome_expiry(self):
        lease = self.select()
        self.assertLessEqual(lease['lease_expires_at'], self.expiry)
        with closing(self.g.bank.connect()) as db:
            expiry = db.execute('SELECT expires_at FROM demo_sessions WHERE token_hash=?',
                                (hashlib.sha256(self.token(lease).encode()).hexdigest(),)).fetchone()[0]
        self.assertEqual(expiry, self.expiry)
        self.now[0] = self.expiry + 1
        self.deny('SESSION_CHANGED', lambda: self.g.experience_provider.validate(self.token(lease)))

    def test_cross_persona_generation_and_lost_header_revoke_full_auth_chain(self):
        first = self.select('tycoon')
        second = self.g.experience_provider.select_current(self.token(first), 'grain_farmer', first['workspace_id'], 0)
        third = self.g.experience_provider.select_current(self.token(second), 'tycoon', second['workspace_id'], 0)
        self.assertEqual([first['session_generation'], second['session_generation'], third['session_generation']], [1, 2, 3])
        other = self.select('retired_entrepreneur', subject='welcome-' + uuid.uuid4().hex)
        self.g.demo_sessions.revoke_family(self.token(second))  # t3响应头丢失，仅有t2也可撤整条subject。
        self.deny('SESSION_CHANGED', lambda: self.g.experience_provider.validate(self.token(third)))
        self.assertEqual(self.g.experience_provider.validate(self.token(other))['workspace_id'], other['workspace_id'])

    def test_revoked_subject_cannot_finish_paused_ensure_and_issue_lease(self):
        other = self.select(subject='welcome-' + uuid.uuid4().hex)
        provider = self.g.experience_provider
        entered, release = threading.Event(), threading.Event()
        original = provider.instances.ensure_owner
        errors = []
        def paused(owner):
            entered.set()
            if not release.wait(5): raise AssertionError('ensure_not_released')
            return original(owner)
        def run():
            try: self.select()
            except BaseException as error: errors.append(error)
        with patch.object(provider.instances, 'ensure_owner', side_effect=paused):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                provider.revoke_subject(self.owner, self.subject)
            finally:
                release.set(); thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BankError)
        self.assertEqual(errors[0].code, 'WELCOME_SESSION_REVOKED')
        self.assertEqual(provider.validate(self.token(other))['workspace_id'], other['workspace_id'])

    def test_private_commit_failure_retry_does_not_open_bank_twice(self):
        materializer = self.g.experience_provider.materializer
        original = materializer.initialize_private
        calls = []
        def fail_once(binding):
            original(binding)
            calls.append(binding['workspace_id'])
            if len(calls) == 1: raise RuntimeError('private_commit_fixture_failure')
        self.g.experience_provider.instances.initialize_private = fail_once
        with self.assertRaises(RuntimeError): self.select()
        with closing(self.g.bank.connect()) as db:
            pending = dict(db.execute('SELECT * FROM registered_user_instances').fetchone())
            accounts = [tuple(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY id', (pending['workspace_id'],))]
        lease = self.select()
        with closing(self.g.bank.connect()) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY id', (pending['workspace_id'],))], accounts)
        self.assertEqual(lease['workspace_id'], pending['workspace_id'])

    def test_orphan_private_data_is_not_adopted_or_erased(self):
        workspace = InstanceRegistry.workspace_id(self.owner, 'tycoon')
        with closing(self.g.personal_data.connect()) as db, db:
            db.execute('INSERT INTO agent_dialogues VALUES(?,?,?)',
                       (workspace, self.g.personal_data.protect('[{"role":"user","content":"孤立记录"}]'), 123.0))
        with closing(self.g.personal_data.connect()) as db:
            before = tuple(self.g.personal_data._raw_dialogue(db, workspace))
        self.deny('INSTANCE_PRIVATE_ORPHANED', self.select)
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual(tuple(self.g.personal_data._raw_dialogue(db, workspace)), before)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM registered_user_instances WHERE status='ready'").fetchone()[0], 0)

    def test_aa_members_and_registered_binding_follow_current_owner(self):
        first = self.select()
        other = self.select(owner=uuid.uuid4().hex, subject='welcome-' + uuid.uuid4().hex)
        members = self.g.experience_provider.instances.aa_members(first['workspace_id'])
        self.assertEqual(len(members), 4)
        self.assertNotIn(other['workspace_id'], [m['workspace_id'] for m in members])
        with self.g.demo_sessions.request(self.token(first), first['workspace_id']), self.g.bank.transaction() as db:
            identity = self.g.current_registered_binding(first['workspace_id'], db)
            self.assertEqual(identity['registered_user_id'], self.owner)
            peer = experience_identity(db, persona='working_landlady', owner_workspace=first['workspace_id'])
            self.assertEqual(self.g.experience_provider.instances.owner_for_workspace(peer['workspace_id'], db=db)['registered_user_id'], self.owner)
        self.deny('REGISTERED_CONTEXT_REQUIRED', lambda: self.g.current_registered_binding(first['workspace_id'], None))

    def test_aa_payment_uses_own_persona_peer_and_rejects_other_registered_owner(self):
        creator=self.select()
        payer=self.select('working_landlady',subject='welcome-'+uuid.uuid4().hex)
        outsider=self.select(owner=uuid.uuid4().hex,subject='welcome-'+uuid.uuid4().hex)
        self.g.bank.permissions.provider=LocalSms(self.g.bank.clock);self.g.bank.permissions.allow_test_provider=True
        def card(lease,suffix):
            with closing(self.g.bank.connect()) as db:
                return dict(db.execute('''SELECT c.id,c.account_id FROM cards c JOIN connected_account_links l
                    ON l.workspace_id=c.workspace_id AND l.account_id=c.account_id
                    WHERE c.workspace_id=? AND c.type='debit' AND c.status='active' AND l.source_account_id LIKE ?''',
                    (lease['workspace_id'],'%:'+suffix)).fetchone())
        receive,pay=card(creator,'rent'),card(payer,'reserve')
        def confirm(lease,op):
            workspace=lease['workspace_id']
            body={'confirmed':True,'confirmation_digest':op['confirmation_digest']}
            if op['permission']=='red':
                challenge=self.g.bank.start_sms_verification(workspace,op['id'])
                body['verification_token']=self.g.bank.verify(workspace,op['id'],{'challenge_id':challenge['challenge_id'],
                    'verification_code':CODE})['verification_token']
            return self.g.bank.confirm(workspace,op['id'],body)
        with self.g.demo_sessions.request(self.token(creator),creator['workspace_id']):
            view=self.g.prepare_aa(creator['workspace_id'],{'account_id':receive['account_id'],'title':'本人画像AA验证',
                'total_minor':2000,'split_mode':'equal','participants':[{'user_id':'tycoon'},{'user_id':'working_landlady'}],
                'idempotency_key':'private-aa-create'})
            done=confirm(creator,view['operation'])
        bill_id=done['result']['bill_id']
        self.deny('AA_NOT_FOUND',lambda:self.g.aa_bills(outsider['workspace_id'],bill_id))
        bill=self.g.aa_bills(payer['workspace_id'],bill_id)
        with closing(self.g.bank.connect()) as db:
            before={r['id']:r['balance_minor'] for r in db.execute('SELECT * FROM accounts WHERE id IN (?,?)',(receive['account_id'],pay['account_id']))}
        with self.g.demo_sessions.request(self.token(payer),payer['workspace_id']):
            op=self.g.prepare_aa(payer['workspace_id'],{'account_id':pay['account_id'],'card_id':pay['id'],
                'idempotency_key':'private-aa-pay'},kind='pay',object_id=bill['my_share']['id'])['operation']
            confirm(payer,op)
        self.assertEqual(self.g.aa_bills(payer['workspace_id'],bill_id)['my_share']['status'],'paid')
        with closing(self.g.bank.connect()) as db:
            after={r['id']:r['balance_minor'] for r in db.execute('SELECT * FROM accounts WHERE id IN (?,?)',(receive['account_id'],pay['account_id']))}
        self.assertEqual(after[receive['account_id']]-before[receive['account_id']],1000)
        self.assertEqual(after[pay['account_id']]-before[pay['account_id']],-1000)

    def test_directory_and_platform_connections_use_only_current_registered_instance(self):
        first=self.select('retired_entrepreneur')
        self.assertEqual(self.g.demo_accounts.catalog()['accounts'],[])
        with self.g.demo_sessions.request(self.token(first),first['workspace_id']):
            directory=self.g.demo_accounts.catalog()
            self.assertEqual(directory['registered_user_id'],self.owner)
            self.assertEqual(directory['initialized_count'],4)
            self.assertTrue(all(a['display_name']!=a['user_id'] for a in directory['accounts']))
        state=self.g.account_connections.read(first['workspace_id'],{'kind':['payment']})
        self.assertTrue(state['active_bindings'])
        session=self.g.session_data(first['workspace_id'],self.token(first))
        self.assertEqual(session['registered_user_id'],self.owner)
        self.assertEqual(session['persona_id'],'retired_entrepreneur')
        self.assertNotIn('upstream_cookies',session)
        def get(path,token=None):
            responses=[]
            handler=SimpleNamespace(path=path,command='GET',headers={'Host':'127.0.0.1:8765',
                **({'Cookie':'qz_bank_user='+token} if token else {})},
                server=SimpleNamespace(server_port=8765),client_address=('127.0.0.1',0),
                _finance_json=lambda *args:responses.append(args))
            self.assertTrue(self.g.dispatch(handler))
            return responses[-1]
        self.assertEqual(get('/api/finance/demo/accounts')[0],401)
        status,body=get('/api/finance/demo/accounts',self.token(first))
        self.assertEqual(status,200);self.assertEqual(body['registered_user_id'],self.owner)
        self.assertEqual(get('/api/finance/demo/accounts?workspace_id=other',self.token(first))[0],400)

    def test_bank_rollback_cannot_reclaim_existing_private_receipt(self):
        backup=Path(self.temp.name)/'before-opening.sqlite3'
        with closing(self.g.bank.connect()) as source,closing(sqlite3.connect(backup)) as target:source.backup(target)
        first=self.select()
        with closing(self.g.personal_data.connect()) as db:
            receipts=[tuple(r) for r in db.execute('SELECT * FROM registered_private_instances ORDER BY workspace_id')]
        with closing(sqlite3.connect(backup)) as source,closing(self.g.bank.connect()) as target:source.backup(target)
        self.deny('INSTANCE_PRIVATE_BINDING_CONFLICT',self.select)
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM registered_private_instances ORDER BY workspace_id')],receipts)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM registered_user_instances WHERE status='ready'").fetchone()[0],0)

    def test_bank_password_revokes_current_subject_and_no_raw_restore_bypass(self):
        from simulated_bank.security_credentials import prepare,restore
        first=self.select();workspace=first['workspace_id'];token=self.token(first)
        other_subject='welcome-'+uuid.uuid4().hex
        other=self.select('working_landlady',subject=other_subject)
        self.g.bank.permissions.provider=LocalSms(self.g.bank.clock)
        self.g.bank.permissions.allow_test_provider=True
        with self.g.demo_sessions.request(token,workspace):
            replacement=prepare(self.g.bank,workspace,{'new_password':'isolated-registered-password-52'})
            with closing(self.g.bank.connect()) as db:
                account=db.execute('SELECT id FROM accounts WHERE workspace_id=? ORDER BY id LIMIT 1',(workspace,)).fetchone()[0]
            op=self.g.bank.prepare(workspace,{'action':'password_change','account_id':account,
                'replacement_id':replacement['replacement_id'],'idempotency_key':'registered-password-fixture'})
            challenge=self.g.bank.start_sms_verification(workspace,op['id'])
            proof=self.g.bank.verify(workspace,op['id'],{'challenge_id':challenge['challenge_id'],'verification_code':CODE})
            self.g.bank.confirm(workspace,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
                'verification_token':proof['verification_token']})
        self.deny('SESSION_CHANGED',lambda:self.g.experience_provider.validate(token))
        self.deny('WELCOME_SESSION_REVOKED',lambda:self.g.experience_provider.resume(self.owner,self.subject,auth_expires_at=self.expiry))
        self.assertEqual(self.g.experience_provider.validate(self.token(other))['workspace_id'],other['workspace_id'])
        self.deny('REGISTERED_LOGIN_REQUIRED',lambda:restore(self.g.bank,{'workspace_id':workspace,'password':'isolated-registered-password-52'}))

    def test_confirmed_operation_reconnect_and_living_consumers_preserve_same_ledger(self):
        first=self.select();workspace=first['workspace_id'];token=self.token(first)
        self.g.bank.permissions.provider=LocalSms(self.g.bank.clock)
        self.g.bank.permissions.allow_test_provider=True
        with closing(self.g.bank.connect()) as db:
            account=db.execute("SELECT account_id FROM connected_account_links WHERE workspace_id=? AND source_account_id LIKE '%:broker'",(workspace,)).fetchone()[0]
            payee=db.execute("SELECT id FROM beneficiaries WHERE workspace_id=? AND verified=1 AND status='active' ORDER BY id LIMIT 1",(workspace,)).fetchone()[0]
        with self.g.demo_sessions.request(token,workspace):
            operation=self.g.bank.prepare(workspace,{'action':'transfer','account_id':account,
                'beneficiary_id':payee,'amount_minor':10000,'reference':'隔离接续验证',
                'idempotency_key':'registered-persistence-fixture'})
            confirmation={'confirmed':True,'confirmation_digest':operation['confirmation_digest']}
            if operation['permission']=='red':
                challenge=self.g.bank.start_sms_verification(workspace,operation['id'])
                grant=self.g.bank.verify(workspace,operation['id'],{'challenge_id':challenge['challenge_id'],'verification_code':CODE})
                confirmation.update(verification_token=grant['verification_token'])
            self.g.bank.confirm(workspace,operation['id'],confirmation)
        with closing(self.g.bank.connect()) as db:
            original=tuple(db.execute('SELECT * FROM operations WHERE id=?',(operation['id'],)).fetchone())
            order_rows=[tuple(r) for r in db.execute('SELECT * FROM orders WHERE workspace_id=? ORDER BY id',(workspace,))]
        result=self.g.living_simulation.run_batch(workspace,max_days=31,max_events=2000)
        self.assertTrue(result['caught_up']);self.assertGreater(result['posted'],0)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM operations WHERE id=?',(operation['id'],)).fetchone()),original)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM orders WHERE workspace_id=? ORDER BY id',(workspace,))],order_rows)
            runtime_ids={r[0] for r in db.execute("SELECT id FROM transactions WHERE workspace_id=? AND id LIKE 'living-%'",(workspace,))}
        report=self.g.living_simulation.read_snapshots(workspace,start_on='2026-10-04',end_on='2026-10-16')
        overview=self.g.bank.snapshot(workspace,month='2026-10')
        self.assertEqual(report['ledger_version'],overview['version'])
        self.assertEqual(report['snapshots'][-1]['cash_minor'],overview['total_cash_minor'])
        found=set();offset=0
        while True:
            flows=self.g.connected_assets.transactions(workspace,month='2026-10',limit=100,offset=offset)
            found.update(r.get('id') for r in flows['data']['items'])
            if not flows['data']['has_more']:break
            offset+=100
        self.assertTrue(runtime_ids.issubset(found))
        self.g.experience_provider.revoke_subject(self.owner,self.subject)
        restarted=self.gateway();restarted.bank.clock=lambda:self.now[0]
        restored=restarted.experience_provider.resume(self.owner,'welcome-'+uuid.uuid4().hex,auth_expires_at=self.expiry)
        self.assertEqual(restored['workspace_id'],workspace)
        self.assertEqual(restarted.bank.operation(workspace,operation['id'])['state'],'succeeded')
        self.assertEqual(restarted.bank.snapshot(workspace)['total_cash_minor'],overview['total_cash_minor'])


if __name__ == '__main__': unittest.main(verbosity=2)
