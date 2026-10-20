"""隔离四画像库：显式迁移、同账本读写、归属、确定性估值与历史分页。"""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from demo_accounts import DemoAccountService
from finance_gateway import FinanceGateway, COOKIE
from simulated_bank import BankError
from simulated_bank.connected_assets import migrate_connected_assets, _hash_file, TRANSACTION_KINDS
from simulated_bank.customer_archive import verify_invariants

ROOT = Path(__file__).resolve().parents[1]
FINANCIAL = ROOT/'outputs/financial-data/navren-financial-v3.sqlite3'


class ConnectedAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='navren-connected-assets-test-')
        cls.env = patch.dict(os.environ,{'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode(),
                                         'NAVREN_KNOWLEDGE_DB':str(Path(cls.folder.name)/'knowledge.sqlite3')})
        cls.env.start()
        cls.network = patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden'))
        cls.network.start()
        cls.seed = Path(cls.folder.name)/'seed.sqlite3'
        cls.g_seed = FinanceGateway(cls.seed,financial_database=FINANCIAL)
        cls.g_seed.demo_accounts.initialize_offline(allow_offline=True)

    @classmethod
    def tearDownClass(cls):
        cls.network.stop();cls.env.stop();cls.folder.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.folder.name)
        self.addCleanup(self.case.cleanup)
        self.path = Path(self.case.name)/'bank.sqlite3'
        with closing(sqlite3.connect(self.seed)) as source, closing(sqlite3.connect(self.path)) as target:
            source.backup(target)
        self.g = FinanceGateway(self.path,financial_database=FINANCIAL)
        self.now = [datetime.fromisoformat('2026-10-07T21:40:41+08:00').timestamp()]
        self.g.bank.clock = lambda:self.now[0]
        self.workspace = DemoAccountService.stable_workspace('retired_entrepreneur')

    def migrate(self, **options):
        return migrate_connected_assets(self.path,FINANCIAL,clock=lambda:self.now[0],**options)

    def error(self, code, call):
        with self.assertRaises(BankError) as caught:
            call()
        self.assertEqual(caught.exception.code,code)

    def read(self, uid='retired_entrepreneur'):
        return self.g.connected_assets.overview(DemoAccountService.stable_workspace(uid))

    def test_dry_run_does_not_write_or_replace_identity(self):
        before, source_hash = _hash_file(self.path),_hash_file(FINANCIAL)
        result = self.migrate()
        self.assertFalse(result['applied']);self.assertFalse(result['changed'])
        self.assertEqual(len(result['workspaces']),4)
        self.assertEqual(_hash_file(self.path),before)
        self.assertEqual(_hash_file(FINANCIAL),source_hash)
        with closing(self.g.bank.connect()) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='connected_asset_migrations'").fetchone())

    def test_opening_credit_reclassification_preserves_net_cash_and_original_ids(self):
        with closing(self.g.bank.connect()) as db:
            old_accounts = {r['id'] for r in db.execute('SELECT id FROM accounts WHERE workspace_id=?',(self.workspace,))}
            before = dict(db.execute('SELECT * FROM workspaces WHERE id=?',(self.workspace,)).fetchone())
            profile = dict(db.execute('SELECT * FROM profiles WHERE workspace_id=?',(self.workspace,)).fetchone())
        self.migrate(apply=True)
        result = self.read();data = result['data'];summary = data['summary']
        self.assertEqual(summary['cash_minor'],73655636)
        self.assertEqual(summary['credit_card_outstanding_minor'],22312)
        self.assertEqual(summary['cash_minor']-summary['credit_card_outstanding_minor'],73633324)
        self.assertTrue(old_accounts.issubset({r['account_id'] for r in data['accounts']}))
        self.assertEqual(summary['net_worth_minor'],summary['total_assets_minor']-summary['liabilities_minor'])
        self.assertEqual(result['version'],before['version']+1)
        self.assertEqual(result['as_of'],'2026-10-07')
        self.assertEqual(result['observed_at'],'2026-10-07T13:40:41+00:00')
        self.assertTrue(result['runtime_ledger_included'])
        with closing(self.g.bank.connect()) as db:
            after = dict(db.execute('SELECT * FROM workspaces WHERE id=?',(self.workspace,)).fetchone())
            self.assertEqual(after['as_of'],'2026-10-07')
            for key in ('token_hash','agent_token_hash','pin_hash','created_at'):
                self.assertEqual(after[key],before[key])
            self.assertEqual(dict(db.execute('SELECT * FROM profiles WHERE workspace_id=?',(self.workspace,)).fetchone()),profile)
            self.assertTrue(verify_invariants(db,self.workspace)['ok'])
        for uid in ('tycoon','grain_farmer','working_landlady','retired_entrepreneur'):
            data = self.read(uid)['data']
            self.assertEqual(sum(a['balance_minor'] for a in data['accounts'] if a['account_type']!='credit_card'),data['summary']['cash_minor'])
            self.assertEqual(sum(f['outstanding_minor'] for f in data['credit_facilities']),data['summary']['credit_card_outstanding_minor'])
            account_ids = {a['account_id'] for a in data['accounts']}
            self.assertTrue(all(card['account_id'] in account_ids for card in data['cards']))
            self.assertTrue(all(p['account_id'] in account_ids for p in data['positions']))
            self.assertTrue(all('::' not in ident for ident in account_ids))

    def test_unmigrated_current_api_fails_without_history_fallback(self):
        self.error('CONNECTED_ASSETS_NOT_MIGRATED',lambda:self.read())

    def test_existing_financial_operation_blocks_whole_migration(self):
        account = next(a for a in self.g.bank.snapshot(self.workspace)['accounts'] if a['id'].endswith('-reserve'))
        self.g.bank.prepare(self.workspace,{'action':'purchase','account_id':account['id'],'product_id':'SIM-CASH-01',
                                           'amount_minor':10000,'idempotency_key':'pre-existing'})
        self.error('CONNECTED_HAS_FINANCIAL_OPERATIONS',lambda:self.migrate(apply=True))
        with closing(self.g.bank.connect()) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='connected_asset_migrations'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(self.workspace,)).fetchone()[0],1)

    def test_expected_version_is_checked_under_migration_lock(self):
        plan = self.migrate()
        versions = {row['workspace_id']:row['version'] for row in plan['workspaces']}
        with self.g.bank.transaction() as db:
            db.execute('UPDATE workspaces SET version=version+1 WHERE id=?',(self.workspace,))
        self.error('CONNECTED_VERSION_CHANGED',lambda:self.migrate(apply=True,expected_versions=versions))

    def execute_transfer(self, value=10000, key='isolated-transfer'):
        _,token = self.g.demo_sessions.create('retired_entrepreneur')
        account = next(a for a in self.read()['data']['accounts'] if a['source_account_id'].endswith(':operating'))
        with closing(self.g.bank.connect()) as db:
            beneficiary = db.execute('SELECT id FROM beneficiaries WHERE workspace_id=? LIMIT 1',(self.workspace,)).fetchone()[0]
        operation = self.g.bank.prepare(self.workspace,{'action':'transfer','account_id':account['account_id'],
                        'beneficiary_id':beneficiary,'amount_minor':value,'idempotency_key':key})
        with self.g.demo_sessions.request(token,self.workspace):
            verification = self.g.bank.verify(self.workspace,operation['id'],{'verification_code':self.g.demo_code(token)})
            receipt = self.g.bank.confirm(self.workspace,operation['id'],{'confirmed':True,
                'confirmation_digest':operation['confirmation_digest'],'verification_token':verification['verification_token']})
        return account,operation,receipt

    def execute(self, payload, key):
        _,token = self.g.demo_sessions.create('retired_entrepreneur')
        operation = self.g.bank.prepare(self.workspace,{**payload,'idempotency_key':key})
        with self.g.demo_sessions.request(token,self.workspace):
            verification = self.g.bank.verify(self.workspace,operation['id'],{'verification_code':self.g.demo_code(token)})
            return self.g.bank.confirm(self.workspace,operation['id'],{'confirmed':True,
                'confirmation_digest':operation['confirmation_digest'],'verification_token':verification['verification_token']})

    def test_confirmed_transfer_card_total_and_runtime_transaction_share_same_balance(self):
        self.migrate(apply=True)
        before = self.read()
        account,operation,receipt = self.execute_transfer()
        after = self.read()
        updated = next(a for a in after['data']['accounts'] if a['account_id']==account['account_id'])
        self.assertEqual(updated['balance_minor'],account['balance_minor']-10000)
        self.assertEqual(after['data']['summary']['cash_minor'],before['data']['summary']['cash_minor']-10000)
        self.assertEqual(after['data']['summary']['net_worth_minor'],before['data']['summary']['net_worth_minor']-10000)
        self.assertEqual(after['version'],before['version']+1)
        card = next(c for c in after['data']['cards'] if c['account_id']==account['account_id'])
        self.assertEqual(card['status'],'active')
        rows = self.g.connected_assets.transactions(self.workspace,month='2026-10',account_id=account['account_id'])
        live = next(r for r in rows['data']['items'] if not r['historical_import'])
        self.assertEqual(live['signed_minor'],-10000)
        self.assertEqual(live['occurred_at'],'2026-10-07T21:40:41+08:00')
        self.assertEqual(live['account_id'],card['account_id'])
        self.assertEqual(live['operation_id'],operation['id'])
        repeat = self.g.bank.confirm(self.workspace,operation['id'],{'confirmed':True,'confirmation_digest':operation['confirmation_digest']})
        self.assertEqual(repeat['id'],receipt['id'])
        self.assertEqual(self.read()['data']['summary']['cash_minor'],after['data']['summary']['cash_minor'])
        result = self.migrate(apply=True)
        self.assertFalse(result['changed'])
        self.assertEqual(self.read()['data']['summary']['cash_minor'],after['data']['summary']['cash_minor'])

    def test_mock_nav_is_deterministic_and_read_has_no_ledger_side_effect(self):
        self.migrate(apply=True)
        first = self.read();second = self.read()
        self.assertEqual(first,second)
        self.now[0] += 60
        later = self.read()
        self.assertEqual(later['version'],first['version'])
        self.assertNotEqual(later['valuation_version'],first['valuation_version'])
        self.assertNotEqual(later['data']['summary']['investment_minor'],first['data']['summary']['investment_minor'])
        for position in first['data']['positions']:
            self.assertEqual(position['quantity_origin'],'versioned_simulated_opening')
            self.assertEqual(position['valuation_source'],'navren_simulated_bank')
        property_asset = next(a for a in first['data']['assets'] if not a['asset_type'].startswith('fund_'))
        self.assertEqual(property_asset['valuation_date'],'2026-10-03')
        self.assertEqual(first['evidence']['historical_dataset_version'],self.g.financial_data.dataset_version)

    def test_supported_product_full_redemption_keeps_valid_current_contract_associations(self):
        self.migrate(apply=True)
        before = self.read()
        account = next(a for a in before['data']['accounts'] if a['source_account_id'].endswith(':reserve'))
        payload = {'account_id':account['account_id'],'product_id':'SIM-CASH-01','amount_minor':10000}
        self.execute({**payload,'action':'purchase'},'purchase-before-full-redemption')
        purchased = self.read()
        position = next(p for p in purchased['data']['positions'] if p['product_id']=='SIM-CASH-01')
        self.assertEqual(position['book_value_minor'],10000)
        self.assertEqual(position['unit_nav'],'1')
        self.execute({**payload,'action':'redeem'},'full-redemption')
        after = self.read()
        assets = {r['asset_id'] for r in after['data']['assets']}
        self.assertTrue(all(c['asset_id'] is None or c['asset_id'] in assets for c in after['data']['contracts']))
        self.assertEqual(after['data']['summary']['cash_minor'],before['data']['summary']['cash_minor'])
        self.assertEqual(after['data']['summary']['net_worth_minor'],before['data']['summary']['net_worth_minor'])

    def seed_current_card_review_materials(self):
        with self.g.bank.transaction() as db:
            customer = db.execute('SELECT id FROM customers WHERE workspace_id=?',(self.workspace,)).fetchone()[0]
            self.g.bank._insert_archive_row(db,'kyc_reviews',self.workspace,{'customer_id':customer,'reviewed_on':'2026-10-07',
                'expires_on':'2027-10-07','risk_band':'low','status':'passed','pep':0,'sanctions_match':0})
            self.g.bank._insert_archive_row(db,'employment',self.workspace,{'customer_id':customer,'occupation':'隔离测试',
                'employer':'隔离测试','annual_income_minor':100000000,'income_source':'test_fixture','verified_on':'2026-10-07'})

    def test_new_card_owned_nonbank_accounts_fail_before_application(self):
        self.migrate(apply=True)
        data = self.read()['data']
        excluded = [a for a in data['accounts'] if a['account_type']!='bank_settlement']
        self.assertTrue(any(a['account_type']=='payment_wallet' for a in excluded))
        for account in excluded:
            self.error('INVALID_CARD_APPLICATION_ACCOUNT',lambda account=account:self.g.bank.prepare(self.workspace,
                {'action':'apply_card','account_id':account['account_id'],'requested_type':'debit',
                 'requested_limit_minor':0,'idempotency_key':'invalid-card-'+account['account_id']}))
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM card_applications WHERE workspace_id=?',(self.workspace,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(self.workspace,)).fetchone()[0],0)
        self.g.bank.advance_days(self.workspace,1)
        self.assertEqual(self.read()['data']['summary']['cash_minor'],data['summary']['cash_minor'])

    def test_lost_credit_card_replacement_preserves_account_and_shared_facility(self):
        self.migrate(apply=True)
        self.seed_current_card_review_materials()
        before = self.read();data = before['data']
        card = next(c for c in data['cards'] if c['card_type']=='credit' and c['status']=='active')
        facility = next(f for f in data['credit_facilities'] if f['facility_id']==card['facility_id'])
        self.execute({'action':'report_card_lost','account_id':card['account_id'],'card_id':card['card_id']},'credit-lost')
        self.execute({'action':'apply_card','account_id':card['account_id'],'card_id':card['card_id'],
            'requested_type':'credit','requested_limit_minor':facility['limit_minor']},'credit-replacement')
        duplicate = {'action':'apply_card','account_id':card['account_id'],'card_id':card['card_id'],
            'requested_type':'credit','requested_limit_minor':facility['limit_minor'],'idempotency_key':'duplicate-replacement'}
        self.error('CARD_REPLACEMENT_ALREADY_REQUESTED',lambda:self.g.bank.prepare(self.workspace,duplicate))
        self.g.bank.advance_days(self.workspace,1)
        after = self.read()['data']
        replacement = next(c for c in after['cards'] if c['source_card_id'] is None and c['card_type']=='credit')
        self.assertEqual(replacement['account_id'],card['account_id'])
        self.assertEqual(replacement['facility_id'],card['facility_id'])
        self.assertEqual(next(c for c in after['cards'] if c['card_id']==card['card_id'])['status'],'expired')
        self.error('CARD_REPLACEMENT_ALREADY_REQUESTED',lambda:self.g.bank.prepare(self.workspace,
            {**duplicate,'idempotency_key':'duplicate-after-issued'}))
        self.assertEqual(after['summary']['cash_minor'],data['summary']['cash_minor'])
        self.assertEqual(after['summary']['credit_card_outstanding_minor'],data['summary']['credit_card_outstanding_minor'])
        self.assertEqual(len(after['credit_facilities']),len(data['credit_facilities']))
        statements_before = {s['id'] for s in data['credit_statements'] if s['card_id']==card['card_id']}
        self.assertTrue(statements_before)
        statements_after = [s for s in after['credit_statements'] if s['id'] in statements_before]
        self.assertTrue(all(s['card_id']==replacement['card_id'] for s in statements_after))
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT outstanding_minor FROM cards WHERE workspace_id=? AND id=?',
                                       (self.workspace,card['card_id'])).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT outstanding_minor FROM cards WHERE workspace_id=? AND id=?',
                                       (self.workspace,replacement['card_id'])).fetchone()[0],facility['outstanding_minor'])
            self.assertTrue(verify_invariants(db,self.workspace)['ok'])

    def test_connected_business_day_rolls_at_midnight_without_get_writes(self):
        self.migrate(apply=True)
        self.now[0] = datetime.fromisoformat('2026-10-07T23:59:59+08:00').timestamp()
        account = next(a for a in self.read()['data']['accounts'] if a['source_account_id'].endswith(':operating'))
        with self.g.bank.transaction() as db:
            db.execute("UPDATE account_limits SET limit_minor=10000 WHERE workspace_id=? AND account_id=? AND kind='transfer_daily'",
                       (self.workspace,account['account_id']))
        self.execute_transfer(key='before-midnight')
        self.error('TRANSFER_DAILY_LIMIT',lambda:self.execute_transfer(key='same-day-rejected'))
        self.now[0] += 2
        file_hash = _hash_file(self.path)
        current = self.read()
        self.assertEqual(current['as_of'],'2026-10-08')
        self.assertEqual(_hash_file(self.path),file_hash)
        self.execute_transfer(key='after-midnight')
        rows = self.g.connected_assets.transactions(self.workspace,month='2026-10',account_id=account['account_id'])
        transfers = [r for r in rows['data']['items'] if not r['historical_import'] and r['kind']=='transfer']
        self.assertEqual(sorted(r['occurred_at'] for r in transfers),
            ['2026-10-07T23:59:59+08:00','2026-10-08T00:00:01+08:00'])
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT as_of FROM workspaces WHERE id=?',(self.workspace,)).fetchone()[0],'2026-10-07')

    def test_unmigrated_fixture_business_day_does_not_follow_provider_clock(self):
        self.now[0] = datetime.fromisoformat('2026-10-08T00:00:01+08:00').timestamp()
        self.assertEqual(self.g.bank.snapshot(self.workspace)['as_of'],'2026-10-03')

    def test_new_debit_and_credit_card_reviews_keep_current_owned_relationships(self):
        self.migrate(apply=True)
        before = self.read()
        self.seed_current_card_review_materials()
        account = next(a for a in before['data']['accounts'] if a['source_account_id'].endswith(':reserve'))
        for card_type,limit in (('debit',0),('credit',100000)):
            self.execute({'action':'apply_card','account_id':account['account_id'],
                          'requested_type':card_type,'requested_limit_minor':limit},'new-card-'+card_type)
        self.g.bank.advance_days(self.workspace,1)
        after = self.read();data = after['data']
        old_ids = {c['card_id'] for c in before['data']['cards']}
        new_cards = [c for c in data['cards'] if c['card_id'] not in old_ids]
        self.assertEqual(len(new_cards),2)
        accounts = {a['account_id']:a for a in data['accounts']}
        facilities = {f['facility_id']:f for f in data['credit_facilities']}
        debit = next(c for c in new_cards if c['card_type']=='debit')
        credit = next(c for c in new_cards if c['card_type']=='credit')
        self.assertEqual(accounts[debit['account_id']]['account_type'],'bank_settlement')
        self.assertEqual(debit['account_id'],account['account_id'])
        self.assertEqual(accounts[debit['account_id']]['connection_id'],account['connection_id'])
        self.assertEqual(accounts[credit['account_id']]['connection_id'],account['connection_id'])
        connection = next(c for c in data['connections'] if c['connection_id']==account['connection_id'])
        self.assertEqual(connection['institution_name'],'中国邮政储蓄银行')
        self.assertEqual(accounts[credit['account_id']]['account_type'],'credit_card')
        self.assertEqual(facilities[credit['facility_id']]['account_id'],credit['account_id'])
        self.assertEqual(facilities[credit['facility_id']]['outstanding_minor'],0)
        self.assertIsNone(credit['source_card_id'])
        self.assertEqual(data['summary']['cash_minor'],before['data']['summary']['cash_minor'])
        self.assertEqual(data['summary']['credit_card_outstanding_minor'],before['data']['summary']['credit_card_outstanding_minor'])

    def test_advanced_settlement_uses_future_business_day_not_wall_clock_day(self):
        self.migrate(apply=True)
        profile = self.g.bank.profile(self.workspace)
        self.g.bank.profile(self.workspace,{'risk_level':3,'horizon_days':365,
            'reserve_minor':profile['reserve_minor'],'confirmed':True})
        account = next(a for a in self.read()['data']['accounts'] if a['source_account_id'].endswith(':reserve'))
        payload = {'account_id':account['account_id'],'product_id':'SIM-FLEX-03','amount_minor':100000}
        self.execute({**payload,'action':'purchase'},'delayed-purchase')
        self.execute({**payload,'action':'redeem'},'delayed-redemption')
        pending = self.read()
        self.assertEqual(pending['data']['summary']['pending_redemption_minor'],100000)
        self.g.bank.advance_days(self.workspace,2)
        settled = self.read()
        self.assertEqual(settled['as_of'],'2026-10-09')
        self.assertEqual(settled['data']['summary']['pending_redemption_minor'],0)
        rows = self.g.connected_assets.transactions(self.workspace,month='2026-10',account_id=account['account_id'])
        settlement = next(r for r in rows['data']['items'] if r['counterparty']=='模拟赎回结算')
        self.assertEqual(settlement['occurred_at'],'2026-10-09T12:00:00+08:00')

    def test_transactions_pagination_owned_account_and_no_import_replay(self):
        self.migrate(apply=True)
        data = self.read()['data']
        account = next(a for a in data['accounts'] if a['source_account_id'].endswith(':operating'))
        result = self.g.connected_assets.transactions(self.workspace,month='2026-09',account_id=account['account_id'],limit=3,offset=1)
        with closing(self.g.financial_data._connect()) as db:
            expected = db.execute('SELECT count(*) FROM transactions WHERE subject_id=? AND account_id=? AND substr(booked_on,1,7)=?',
                                  ('retired_entrepreneur',account['source_account_id'],'2026-09')).fetchone()[0]
        self.assertEqual(result['data']['total'],expected)
        self.assertEqual(result['data']['offset'],1)
        self.assertTrue(all(r['account_id']==account['account_id'] for r in result['data']['items']))
        self.assertTrue(all(r['historical_import'] for r in result['data']['items']))
        self.assertEqual(len({r['transaction_id'] for r in result['data']['items']}),len(result['data']['items']))
        foreign = self.read('tycoon')['data']['accounts'][0]['account_id']
        self.error('CONNECTED_ACCOUNT_NOT_ACCESSIBLE',lambda:self.g.connected_assets.transactions(
            self.workspace,month='2026-09',account_id=foreign))
        for options in ({'month':'2026-13'},{'month':'2026-09','limit':101},{'month':'2026-09','offset':-1}):
            self.error('INVALID_MONTH' if options['month']=='2026-13' else 'INVALID_PAGINATION',
                       lambda options=options:self.g.connected_assets.transactions(self.workspace,**options))
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM transactions WHERE workspace_id=?',(self.workspace,)).fetchone()[0],0)

    def fixture_transaction(self, ident, account, kind, stamp, workspace=None):
        # 只在每例隔离库构造读取样本；不触及正式账本或业务执行路径。
        with self.g.bank.transaction() as db:
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident,workspace or self.workspace,account,stamp,'隔离筛选样本',100,-100,
                        kind,'隔离测试','',None,None))

    def test_historical_kind_filter_counts_before_pagination_and_keeps_available_kinds(self):
        self.migrate(apply=True)
        plain = self.g.connected_assets.transactions(self.workspace,month='2026-09',limit=100)
        bank_hash,history_hash = _hash_file(self.path),_hash_file(FINANCIAL)
        with closing(self.g.financial_data._connect()) as db:
            available = sorted(r[0] for r in db.execute(
                "SELECT DISTINCT kind FROM transactions WHERE subject_id=? AND substr(booked_on,1,7)=?",
                ('retired_entrepreneur','2026-09')))
            for kind in TRANSACTION_KINDS:
                with self.subTest(kind=kind):
                    expected = [r[0] for r in db.execute(
                        'SELECT transaction_id FROM transactions WHERE subject_id=? AND substr(booked_on,1,7)=? '
                        'AND kind=? ORDER BY occurred_at DESC,transaction_id DESC',
                        ('retired_entrepreneur','2026-09',kind))]
                    result = self.g.connected_assets.transactions(self.workspace,month='2026-09',kind=kind,limit=3,offset=1)
                    self.assertEqual(result['data']['total'],len(expected))
                    self.assertEqual([r['transaction_id'] for r in result['data']['items']],
                                     ['history:'+ident for ident in expected[1:4]])
                    self.assertEqual(result['data']['has_more'],4<len(expected))
                    self.assertEqual(result['data']['kind'],kind)
                    self.assertEqual(result['data']['available_kinds'],available)
                    for key in ('workspace_binding','subject_id','current_account_id','privacy_revision','version','dataset_version'):
                        self.assertEqual(result[key],plain[key])
        self.assertIsNone(plain['data']['kind'])
        self.assertEqual(plain['data']['available_kinds'],available)
        self.assertEqual((_hash_file(self.path),_hash_file(FINANCIAL)),(bank_hash,history_hash))

    def test_kind_filter_merges_history_runtime_before_offset_and_isolates_account_and_month(self):
        self.migrate(apply=True)
        accounts = self.read()['data']['accounts']
        account = next(a for a in accounts if a['source_account_id'].endswith(':operating'))
        other = next(a for a in accounts if a['source_account_id'].endswith(':daily'))
        expected = []
        with closing(self.g.financial_data._connect()) as db:
            for row in db.execute("SELECT transaction_id,occurred_at FROM transactions WHERE subject_id=? AND account_id=? AND substr(booked_on,1,7)=? AND kind='expense'",
                                  ('retired_entrepreneur',account['source_account_id'],'2026-09')):
                expected.append((row['occurred_at'],'history:'+row['transaction_id']))
            historical_kinds = {r[0] for r in db.execute(
                'SELECT DISTINCT kind FROM transactions WHERE subject_id=? AND account_id=? AND substr(booked_on,1,7)=?',
                ('retired_entrepreneur',account['source_account_id'],'2026-09'))}
        for index,day in enumerate((1,12,23,28)):
            stamp = '2026-09-'+str(day).zfill(2)+'T12:00:00+08:00'
            ident = 'matching-'+str(index)
            self.fixture_transaction(ident,account['account_id'],'expense',stamp)
            expected.append((stamp,'runtime:'+ident))
        for index in range(8):
            self.fixture_transaction('nonmatching-'+str(index),account['account_id'],'transfer',
                                     '2026-09-30T22:00:'+str(index).zfill(2)+'+08:00')
        self.fixture_transaction('other-account',other['account_id'],'expense','2026-09-30T23:00:00+08:00')
        self.fixture_transaction('other-month',account['account_id'],'expense','2026-10-01T23:00:00+08:00')
        foreign = self.read('tycoon')['data']['accounts'][0]['account_id']
        self.fixture_transaction('other-workspace',foreign,'expense','2026-09-30T23:01:00+08:00',
                                 DemoAccountService.stable_workspace('tycoon'))
        expected = [ident for _,ident in sorted(expected,reverse=True)]
        collected = []
        before = _hash_file(self.path)
        for offset in range(0,len(expected)+2,2):
            result = self.g.connected_assets.transactions(self.workspace,month='2026-09',account_id=account['account_id'],
                                                         kind='expense',limit=2,offset=offset)['data']
            page = [r['transaction_id'] for r in result['items']]
            self.assertEqual(page,expected[offset:offset+2])
            self.assertEqual(result['total'],len(expected))
            self.assertEqual(result['has_more'],offset+2<len(expected))
            self.assertEqual(result['available_kinds'],sorted(historical_kinds|{'transfer'}))
            self.assertTrue(all(r['account_id']==account['account_id'] for r in result['items']))
            collected.extend(page)
        self.assertEqual(collected,expected)
        self.assertTrue(any(ident.startswith('history:') for ident in collected))
        self.assertTrue(any(ident.startswith('runtime:') for ident in collected))
        self.assertEqual(_hash_file(self.path),before)
        self.error('CONNECTED_ACCOUNT_NOT_ACCESSIBLE',lambda:self.g.connected_assets.transactions(
            self.workspace,month='2026-09',kind='expense',account_id=foreign))

    def test_transfer_filter_groups_both_source_kinds_and_keeps_internal_transfer_separate(self):
        history_path = Path(self.case.name)/'history.sqlite3'
        with closing(sqlite3.connect(FINANCIAL)) as source, closing(sqlite3.connect(history_path)) as target:
            source.backup(target)
            samples = target.execute("SELECT transaction_id FROM transactions WHERE subject_id=? AND account_id=? AND substr(booked_on,1,7)=? AND kind='expense' ORDER BY transaction_id LIMIT 2",
                                     ('retired_entrepreneur','retired_entrepreneur::account:operating','2026-09')).fetchall()
            self.assertEqual(len(samples),2)
            for row,kind in zip(samples,('transfer','external_transfer')):
                target.execute('UPDATE transactions SET kind=? WHERE subject_id=? AND transaction_id=?',
                               (kind,'retired_entrepreneur',row[0]))
            target.commit()
        self.g = FinanceGateway(self.path,financial_database=history_path)
        self.g.bank.clock = lambda:self.now[0]
        migrate_connected_assets(self.path,history_path,apply=True,clock=lambda:self.now[0])
        account = next(a for a in self.read()['data']['accounts'] if a['source_account_id'].endswith(':operating'))
        for kind in ('transfer','external_transfer','internal_transfer'):
            self.fixture_transaction('alias-'+kind,account['account_id'],kind,'2026-09-29T12:00:00+08:00')
        result = self.g.connected_assets.transactions(self.workspace,month='2026-09',account_id=account['account_id'],kind='transfer',limit=100)['data']
        self.assertEqual(result['total'],4)
        self.assertEqual({r['kind'] for r in result['items']},{'transfer','external_transfer'})
        self.assertEqual(sum(r['historical_import'] for r in result['items']),2)
        self.assertEqual(result['kind'],'transfer')
        self.assertEqual(result['available_kinds'],['expense','internal_transfer','transfer'])
        internal = self.g.connected_assets.transactions(self.workspace,month='2026-09',account_id=account['account_id'],kind='internal_transfer',limit=100)['data']
        self.assertEqual(internal['total'],2)
        self.assertTrue(all(r['kind']=='internal_transfer' for r in internal['items']))
        self.assertEqual(internal['available_kinds'],result['available_kinds'])

    def test_transaction_kind_query_is_routed_and_rejects_invalid_empty_or_repeated_values(self):
        self.migrate(apply=True)
        _,token = self.g.demo_sessions.create('retired_entrepreneur')
        for query,status,code in (
            ('month=2026-09&kind=expense',200,None),
            ('month=2026-09',200,None),
            ('month=2026-09&kind=',400,'INVALID_TRANSACTION_KIND'),
            ('month=2026-09&kind=unknown',400,'INVALID_TRANSACTION_KIND'),
            ('month=2026-09&kind=external_transfer',400,'INVALID_TRANSACTION_KIND'),
            ('month=2026-09&kind=expense&kind=income',400,'INVALID_QUERY'),
            ('month=2026-09&kind=&kind=expense',400,'INVALID_QUERY'),
            ('month=2026-09&kind=expense&month=',400,'INVALID_QUERY'),
        ):
            with self.subTest(query=query):
                responses = []
                handler = SimpleNamespace(path='/api/finance/connected-assets/transactions?'+query,command='GET',
                    server=SimpleNamespace(server_port=60291),client_address=('127.0.0.1',12345),
                    headers={'Host':'127.0.0.1:60291','Cookie':COOKIE+'='+token},
                    _finance_json=lambda status,payload,cookie=None:responses.append((status,payload)))
                self.assertTrue(self.g.dispatch(handler))
                self.assertEqual(responses[0][0],status)
                if code:self.assertEqual(responses[0][1]['error']['code'],code)
                else:
                    self.assertEqual(responses[0][1]['data']['kind'],'expense' if 'kind=' in query else None)
                    if 'kind=' in query:
                        self.assertTrue(all(r['kind']=='expense' for r in responses[0][1]['data']['items']))
        for kind in ('', 'unknown', 'external_transfer', [], 1, True):
            self.error('INVALID_TRANSACTION_KIND',lambda kind=kind:self.g.connected_assets.transactions(
                self.workspace,month='2026-09',kind=kind))


if __name__=='__main__':
    unittest.main()
