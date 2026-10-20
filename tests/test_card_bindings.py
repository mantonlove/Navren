"""本人银行卡登记边界：仅隔离合成库，禁止读取或写入活跃用户账本。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
import os
import threading
import unittest
from unittest.mock import patch

import test_aa_payments as fixture
from finance_gateway import FinanceGateway, COOKIE
from simulated_bank import BankError


NUMBER = '6222000000019876'


class CardBindingsTests(unittest.TestCase):
    setUpClass = fixture.AAPaymentsTests.__dict__['setUpClass']
    tearDownClass = fixture.AAPaymentsTests.__dict__['tearDownClass']
    setUp = fixture.AAPaymentsTests.setUp
    error = fixture.AAPaymentsTests.error

    def form(self,u=None,**changes):
        u = u or self.a
        overview = self.g.connected_assets.overview(u['workspace_id'])
        account = next(row for row in overview['data']['accounts']
                       if row['account_type'] == 'bank_settlement' and row['status'] == 'active')
        return {'account_id':account['account_id'],'card_number':NUMBER,'confirmed':True,
                'expected_workspace_id':u['workspace_id'],'expected_subject_id':u['user_id'],
                'expected_privacy_revision':overview['privacy_revision'],'expected_version':overview['version'],
                'idempotency_key':'registration',**changes}

    def register(self,body,u=None,g=None):
        u = u or self.a
        gateway = g or self.g
        with gateway.demo_sessions.request(self.tokens[u['user_id']],u['workspace_id']):
            return gateway.card_bindings.register(u['workspace_id'],body)

    def rows(self,tables):
        with closing(self.g.bank.connect()) as db:
            return {table:hashlib.sha256(json.dumps(sorted(tuple(row) for row in db.execute('SELECT * FROM '+table)),
                                                   ensure_ascii=False,default=str).encode()).hexdigest()
                    for table in tables}

    def test_registration_preserves_every_financial_table_full_directory_and_unknown_dates(self):
        w = self.a['workspace_id']
        before = self.g.connected_assets.overview(w)
        untouched = ['accounts','account_details','connected_account_links','holdings','investment_lots','transactions',
                     'orders','loans','credit_statements','connected_asset_migrations','card_applications']
        hashes = self.rows(untouched)
        old_dates = {row['card_id']:(row['issued_on'],row['expires_on']) for row in before['data']['cards']}
        result = self.register(self.form(card_number='6222 0000 0001 9876'))
        self.assertTrue(result['changed']);self.assertFalse(result['replayed']);self.assertFalse(result['real_connection'])
        self.assertEqual(result['version'],before['version']+1)
        self.assertEqual((result['workspace_id'],result['subject_id'],result['privacy_revision']),
                         (w,self.a['user_id'],before['privacy_revision']))
        self.assertEqual(self.rows(untouched),hashes)
        after = self.g.connected_assets.overview(w)
        self.assertEqual(after['data']['summary'],before['data']['summary'])
        self.assertEqual([{key:value for key,value in row.items() if key!='card_ids'} for row in after['data']['accounts']],
                         [{key:value for key,value in row.items() if key!='card_ids'} for row in before['data']['accounts']])
        selected_before = next(row for row in before['data']['accounts'] if row['account_id']==result['card']['account_id'])
        selected_after = next(row for row in after['data']['accounts'] if row['account_id']==result['card']['account_id'])
        self.assertEqual(set(selected_after['card_ids']),set(selected_before['card_ids'])|{result['card']['card_id']})
        self.assertEqual(after['data']['positions'],before['data']['positions'])
        self.assertEqual(after['data']['credit_facilities'],before['data']['credit_facilities'])
        self.assertEqual(len(after['data']['cards']),len(before['data']['cards'])+1)
        card = next(row for row in after['data']['cards'] if row['card_id'] == result['card']['card_id'])
        self.assertEqual(card['account_id'],result['card']['account_id'])
        self.assertEqual(card['masked_number'],'**** **** **** 9876')
        self.assertTrue(card['registration_only']);self.assertEqual(card['registered_on'],'2026-10-10')
        self.assertIsNone(card['issued_on']);self.assertIsNone(card['expires_on'])
        self.assertNotIn('details',card);self.assertNotIn('card_fingerprint',card)
        for row in after['data']['cards']:
            if row['card_id'] in old_dates:
                self.assertEqual((row['issued_on'],row['expires_on']),old_dates[row['card_id']])
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
            raw = dict(db.execute('SELECT * FROM cards WHERE workspace_id=? AND id=?',(w,card['card_id'])).fetchone())
            self.assertEqual((raw['credit_limit_minor'],raw['outstanding_minor']),(0,0))
            self.assertTrue(json.loads(raw['details'])['issued_on_unknown'])
        self.assertNotIn(NUMBER.encode(),self.path.read_bytes())
        self.assertNotIn(NUMBER,json.dumps(result))

    def test_confirmation_number_types_lengths_and_strict_fields_reject_without_writes(self):
        body = self.form()
        tables = ['cards','connected_card_links','navren_card_registrations','navren_card_registration_requests','workspaces','audit']
        before = self.rows(tables)
        for value in (False,1,'true',None):
            self.error('CARD_BINDING_CONFIRMATION_REQUIRED',lambda:self.register({**body,'confirmed':value}))
        for value in (1234567890123456,'123456789012345','12345678901234567890','１２３４５６７８９０１２３４５６',
                      '6222-0000-0001-9876','6222\n000000019876',None,'x'*81):
            self.error('CARD_NUMBER_INVALID',lambda:self.register({**body,'card_number':value}))
        for key,value in [('card_type','credit'),('bank_name','伪造银行'),('balance_minor',123),('source','real_bank')]:
            self.error('INVALID_FIELDS',lambda:self.register({**body,key:value}))
        for key,value in [('expected_version',True),('expected_version','1'),('expected_privacy_revision',1.0),('idempotency_key','bad\nkey')]:
            self.error('INVALID_FIELDS',lambda:self.register({**body,key:value}))
        missing = dict(body);missing.pop('expected_subject_id')
        self.error('INVALID_FIELDS',lambda:self.register(missing))
        self.assertEqual(self.rows(tables),before)

    def test_idempotency_concurrency_original_version_recovery_conflict_and_persistence(self):
        body = self.form()
        initial = body['expected_version']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:self.register(body),range(2)))
        self.assertEqual(sum(row['changed'] for row in results),1)
        self.assertEqual(sum(row['replayed'] for row in results),1)
        ids = {row['card']['card_id'] for row in results};self.assertEqual(len(ids),1)
        replay = self.register({**body,'card_number':'6222 0000 0001 9876'})
        self.assertTrue(replay['replayed']);self.assertFalse(replay['changed']);self.assertEqual(replay['version'],initial+1)
        self.error('IDEMPOTENCY_CONFLICT',lambda:self.register({**body,'card_number':'6222000000019877'}))
        same_card_new_key = self.register(self.form(idempotency_key='same-card-new-key'))
        self.assertFalse(same_card_new_key['changed']);self.assertEqual(same_card_new_key['version'],initial+1)
        reopened = FinanceGateway(self.path,financial_database=self.g.financial_data.database_path)
        reopened.bank.clock = lambda:self.now[0]
        self.assertEqual(self.register(body,g=reopened)['card']['card_id'],replay['card']['card_id'])
        self.assertEqual(self.g.connected_assets.overview(self.a['workspace_id'])['version'],initial+1)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM navren_card_registrations').fetchone()[0],1)

    def test_same_card_cross_account_and_subject_rejected_without_duplicate(self):
        body = self.form();result = self.register(body)
        data = self.g.connected_assets.overview(self.a['workspace_id'])['data']
        other = next(row for row in data['accounts'] if row['account_type']=='bank_settlement' and row['account_id']!=body['account_id'])
        before = self.rows(['cards','connected_card_links','navren_card_registrations','workspaces'])
        self.error('CARD_ALREADY_REGISTERED',lambda:self.register(self.form(account_id=other['account_id'],idempotency_key='other-account')))
        self.error('CARD_ALREADY_REGISTERED',lambda:self.register(self.form(self.b,idempotency_key='other-user'),self.b))
        self.assertEqual(self.rows(['cards','connected_card_links','navren_card_registrations','workspaces']),before)
        self.assertNotIn(result['card']['card_id'],{row['card_id'] for row in self.g.connected_assets.overview(self.b['workspace_id'])['data']['cards']})

    def test_owner_version_privacy_and_switched_session_fail_closed(self):
        body = self.form()
        self.error('CARD_BINDING_IDENTITY_CHANGED',lambda:self.register({**body,'expected_workspace_id':self.b['workspace_id']}))
        self.error('CARD_BINDING_IDENTITY_CHANGED',lambda:self.register({**body,'expected_subject_id':self.b['user_id']}))
        self.error('CARD_BINDING_ACCOUNT_UNAVAILABLE',lambda:self.register({**body,'account_id':self.form(self.b)['account_id']}))
        self.error('CARD_BINDING_VERSION_CHANGED',lambda:self.register({**body,'expected_version':body['expected_version']-1}))
        self.error('PRIVACY_REVISION_CHANGED',lambda:self.register({**body,'expected_privacy_revision':body['expected_privacy_revision']+1}))
        self.register(body)
        with self.g.bank.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS conversation_privacy_versions(workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), revision INTEGER NOT NULL)')
            db.execute('INSERT INTO conversation_privacy_versions VALUES(?,1) ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1',
                       (self.a['workspace_id'],))
        self.error('PRIVACY_REVISION_CHANGED',lambda:self.register(body))
        before = self.rows(['cards','connected_card_links','navren_card_registrations'])
        self.g.demo_sessions.select(self.tokens[self.a['user_id']],self.b['user_id'],self.a['workspace_id'])
        self.error('SESSION_CHANGED',lambda:self.register(body))
        self.assertEqual(self.rows(['cards','connected_card_links','navren_card_registrations']),before)

    def test_wallet_credit_frozen_account_and_tail_collision_rejected(self):
        body = self.form();data = self.g.connected_assets.overview(self.a['workspace_id'])['data']
        for typ in ('credit_card','payment_wallet','securities_cash'):
            account = next(row for row in data['accounts'] if row['account_type']==typ)
            self.error('CARD_BINDING_ACCOUNT_UNAVAILABLE',lambda:self.register({**body,'account_id':account['account_id']}))
        existing = next(row for row in data['cards'] if row['card_type']=='debit' and row['account_id']==body['account_id'])
        self.error('CARD_NUMBER_AMBIGUOUS',lambda:self.register({**body,'card_number':'622200000001'+existing['masked_number'][-4:]}))
        with self.g.bank.transaction() as db:
            db.execute("UPDATE account_details SET status='frozen' WHERE workspace_id=? AND account_id=?",
                       (self.a['workspace_id'],body['account_id']))
        self.error('CARD_BINDING_ACCOUNT_UNAVAILABLE',lambda:self.register(body))

    def test_valid_19_digit_card_and_inactive_registration_cannot_be_reenabled(self):
        result = self.register(self.form(card_number='6222000000000009876'))
        self.assertEqual(result['card']['masked_number'],'**** **** **** 9876')
        with self.g.bank.transaction() as db:
            db.execute("UPDATE cards SET status='lost' WHERE workspace_id=? AND id=?",
                       (self.a['workspace_id'],result['card']['card_id']))
        self.error('CARD_BINDING_INACTIVE',lambda:self.register(self.form(card_number='6222000000000009876',idempotency_key='again')))

    def test_http_csrf_query_rejection_and_valid_receipt(self):
        from server import create_server, ProxyHandler
        from webank_catalog import PublicProductCatalog
        from test_bank_integration import Browser
        body = self.form()
        with patch.dict(os.environ,{'NAVREN_FINANCIAL_DB':str(self.g.financial_data.database_path),
                                   'NAVREN_MESSAGE_WORKER_ENABLED':'false'}),patch.object(ProxyHandler,'log_message',lambda *args:None):
            server = create_server(0,self.path,public_catalog=PublicProductCatalog(lambda *args,**kwargs:io.BytesIO()),public_sync_enabled=False)
            server.finance.bank.clock = lambda:self.now[0]
            thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def call(path,csrf=True,method='POST'):
                    browser = Browser('http://127.0.0.1:'+str(server.server_port))
                    token = self.tokens[self.a['user_id']]
                    browser.csrf = self.g.csrf(token) if csrf else None
                    return browser.call(path,method,body if method=='POST' else None,headers={'Cookie':COOKIE+'='+token})
                path = '/api/finance/card-bindings'
                self.assertEqual(call(path,csrf=False)[0],403)
                for suffix in ('?x=1','?x=','?workspace_id=other','?expected_version=1'):
                    self.assertEqual(call(path+suffix)[0],400)
                self.assertEqual(call(path,method='GET')[0],404)
                status,result,headers = call(path)
                self.assertEqual(status,200);self.assertTrue(result['changed']);self.assertIn('no-store',headers['Cache-Control'])
                status,replay,_ = call(path)
                self.assertEqual(status,200);self.assertTrue(replay['replayed']);self.assertFalse(replay['changed'])
                self.assertEqual(result['card']['card_id'],replay['card']['card_id'])
                self.assertNotIn(NUMBER,json.dumps(result))
            finally:
                server.shutdown();server.server_close();thread.join(2)


if __name__ == '__main__':
    unittest.main()
