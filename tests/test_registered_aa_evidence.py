"""注册实例历史选源及原归属/路径/散列边界；实际隔离HTTP，无模型短信。"""
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import unittest
import uuid

import test_account_state_http as http_fixture
from simulated_bank import aa_evidence as evidence
from simulated_bank import BankError


class RegisteredAAEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.h = http_fixture.AccountStateHTTPTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.g = self.h.g; self.w = self.h.a['workspace_id']; self.f = self.h.f.f

    def rows(self, workspace=None, subject=None):
        workspace = workspace or self.w
        service = self.g.experience_provider.materializer.service_for(workspace)
        with closing(service._connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM transactions WHERE subject_id=? AND kind='expense' AND status='booked' AND signed_minor<0 AND amount_minor>10000 ORDER BY occurred_at DESC LIMIT 3", (subject or 'tycoon',))]

    def body(self, reference):
        with self.g.demo_sessions.request(self.f.token(self.h.a), self.w):
            card = next(c for c in self.g.overview(self.w)['cards'] if c['type'] == 'debit' and c['status'] == 'active')
        return {'account_id': card['account_id'], 'title': '隔离历史消费', 'total_minor': 2001,
                'split_mode': 'equal', 'participants': [{'user_id': 'tycoon'}, {'user_id': 'grain_farmer'}],
                'source_consumption_id': reference, 'idempotency_key': uuid.uuid4().hex}

    def test_actual_http_uses_instance_sealed_history_with_original_startup_path(self):
        service = self.g.experience_provider.materializer.service_for(self.w)
        self.assertNotEqual(self.g.financial_data.database_path, service.database_path)
        with closing(self.g.bank.connect()) as db:
            binding = dict(db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?', (self.w,)).fetchone())
        self.assertEqual(Path(binding['source_path']).resolve(), service.database_path)
        status, page, _ = self.h.request(path='/api/finance/aa/consumptions')
        self.assertEqual(status, 200, page)
        self.assertGreater(page['total'], 0)
        self.assertTrue(any(r['source'] == 'history' for r in page['consumptions']))
        row = self.rows()[0]
        status, draft, _ = self.h.request(self.body('history:'+row['transaction_id']), path='/api/finance/aa/bills/prepare')
        self.assertEqual(status, 200, draft)
        snapshot = draft['operation']['payload']['aa_bill']['consumption_snapshot']
        self.assertEqual(snapshot['paid_minor'], row['amount_minor'])

    def test_foreign_registered_runtime_and_unrelated_persona_history_rejected(self):
        other = self.f.select(owner=uuid.uuid4().hex, subject='welcome-'+uuid.uuid4().hex)
        with self.g.demo_sessions.request(self.f.token(other), other['workspace_id']), self.g.bank.transaction() as db:
            account = self.g.bank._accounts(db, other['workspace_id'])[0]
            ident = self.g.bank._ledger(db, other['workspace_id'], account['id'],
                date(2026,10,16), '另一注册用户私人消费', 10000, -10000, 'expense', None, category='餐饮')
        unrelated = self.rows(subject='retired_entrepreneur')[0]['transaction_id']
        for reference in ('runtime:'+ident, 'history:'+unrelated):
            status, result, _ = self.h.request(self.body(reference), path='/api/finance/aa/bills/prepare')
            self.assertEqual(status, 422, result)
            self.assertEqual(result['error']['code'], 'AA_CONSUMPTION_INVALID')
        status, _, _ = self.h.request(path='/api/finance/aa/consumptions', suffix='?workspace_id='+other['workspace_id'])
        self.assertEqual(status, 400)

    def test_registered_path_subject_and_hash_mismatch_remain_rejected(self):
        with closing(self.g.bank.connect()) as db:
            before = dict(db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?', (self.w,)).fetchone())
        for field, value in (('subject_id', 'grain_farmer'), ('source_path', str(self.g.financial_data.database_path)), ('source_sha256', '0'*64)):
            with self.g.demo_sessions.request(self.f.token(self.h.a), self.w), self.g.bank.transaction() as db:
                db.execute('UPDATE connected_asset_migrations SET '+field+'=? WHERE workspace_id=?', (value, self.w))
            status, result, _ = self.h.request(path='/api/finance/aa/consumptions')
            self.assertEqual(status, 422, result)
            self.assertEqual(result['error']['code'], 'AA_CONSUMPTION_INVALID')
            with self.g.demo_sessions.request(self.f.token(self.h.a), self.w), self.g.bank.transaction() as db:
                db.execute('UPDATE connected_asset_migrations SET '+field+'=? WHERE workspace_id=?', (before[field], self.w))
        self.assertEqual(self.h.request(path='/api/finance/aa/consumptions')[0], 200)
