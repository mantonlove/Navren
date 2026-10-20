"""固定时钟注册实例后台到期；真实模拟确认和隔离SMS替身，无页面触发补执行。"""
from contextlib import closing
from datetime import datetime
import unittest
import uuid

import test_registered_instances as registered_fixture
import test_remittance_simulation as remittance_fixture
from test_sms_permissions import LocalSms, CODE
from base_runtime.service_assembly import AssembledGateway


class RegisteredRemittanceDueTests(unittest.TestCase):
    def setUp(self):
        self.f = registered_fixture.RegisteredInstancesTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.g = self.f.g
        self.f.now[0] = datetime.fromisoformat('2026-10-16T10:00:00+08:00').timestamp()
        self.f.expiry = self.f.now[0]+900
        self.lease = self.f.select(); self.w = self.lease['workspace_id']; self.token = self.f.token(self.lease)
        self.g.bank.permissions.provider = LocalSms(self.g.bank.clock); self.g.bank.permissions.allow_test_provider = True
        self.worker = self.g.living_simulation
        # 本题只测既有授权队列；空生活计划避免把新模拟消费混入金额/到期断言。
        self.worker.planner = lambda *args, **kwargs: []
        self.worker.run_batch(self.w, max_days=31)
        with closing(self.g.bank.connect()) as db:
            self.card = dict(db.execute("SELECT c.id,c.account_id FROM cards c JOIN accounts a ON a.workspace_id=c.workspace_id AND a.id=c.account_id WHERE c.workspace_id=? AND c.type='debit' AND c.status='active' AND a.currency='CNY' AND a.balance_minor>100000 ORDER BY c.id LIMIT 1", (self.w,)).fetchone())

    def raw(self, op):
        with closing(self.g.bank.connect()) as db:
            row = dict(db.execute('SELECT * FROM navren_remittances WHERE workspace_id=? AND operation_id=?', (self.w, op)).fetchone())
            row['cash'] = db.execute('SELECT balance_minor FROM accounts WHERE workspace_id=? AND id=?', (self.w, self.card['account_id'])).fetchone()[0]
            row['posts'] = db.execute('SELECT count(*) FROM transactions WHERE workspace_id=? AND operation_id=?', (self.w, op)).fetchone()[0]
            row['payments'] = db.execute('SELECT count(*) FROM payment_instructions WHERE workspace_id=? AND id=?', (self.w, row['payment_id'])).fetchone()[0]
            row['total_payments'] = db.execute('SELECT count(*) FROM payment_instructions WHERE workspace_id=?', (self.w,)).fetchone()[0]
            row['hold'] = self.g.bank._active_holds(db, self.w, self.card['account_id'])
            row['next_due_on'] = db.execute('SELECT next_due_on FROM scheduled_payments WHERE workspace_id=? AND id=?', (self.w, row['scheduled_payment_id'])).fetchone()[0]
            return row

    def confirm(self, mode='ordinary'):
        with self.g.demo_sessions.request(self.token, self.w):
            op = self.g.prepare_remittance(self.w, remittance_fixture.sample_form(self.card, mode=mode))['operation']
            body = {'confirmed': True, 'confirmation_digest': op['confirmation_digest']}
            if op['permission'] == 'red':
                challenge = self.g.bank.start_sms_verification(self.w, op['id'])
                body.update(challenge_id=challenge['challenge_id'], verification_code=CODE)
            result = self.g.confirm(self.w, op['id'], body)['operation']
        self.assertEqual(result['state'], 'scheduled')
        self.g.experience_provider.revoke_subject(self.f.owner, self.f.subject)
        return op['id']

    def tick(self, stamp):
        self.f.now[0] = stamp
        return self.worker.run_batch(self.w, max_days=31)

    def test_ordinary_exact_two_hours_worker_without_page_and_new_session_restart(self):
        op = self.confirm(); initial = self.raw(op); due = initial['execute_at']
        self.assertEqual(due, datetime.fromisoformat('2026-10-16T12:00:00+08:00').timestamp())
        self.assertEqual(initial['next_due_on'], '2026-10-17')
        self.tick(due-1); self.assertEqual(self.raw(op)['status'], 'scheduled')
        self.assertEqual(self.raw(op)['posts'], 0)
        self.tick(due)
        # 先查数据库；禁止overview/remittances等GET在断言前偷偷补执行。
        finished = self.raw(op)
        self.assertEqual(finished['status'], 'succeeded', finished)
        self.assertEqual(finished['cash'], initial['cash']-10000)
        self.assertEqual(finished['posts'], 1); self.assertEqual(finished['payments'], 1); self.assertEqual(finished['hold'], 0)
        self.tick(due+1); self.assertEqual(self.raw(op), finished)
        restarted = AssembledGateway(self.f.path, financial_database=registered_fixture.FINANCIAL, registered_mode=True)
        restarted.bank.clock = self.g.bank.clock
        lease = restarted.experience_provider.resume(self.f.owner, 'welcome-'+uuid.uuid4().hex, auth_expires_at=due+900)
        with restarted.demo_sessions.request(lease['upstream_cookies']['qz_bank_user'], self.w):
            receipt = restarted.bank.remittances(self.w, op)
        self.assertEqual(receipt['status'], 'succeeded'); self.assertEqual(receipt['transaction_id'], finished['transaction_id'])
        self.assertEqual(self.raw(op)['posts'], 1)

    def test_next_day_not_early_and_exact_execute_time_only_once(self):
        op = self.confirm('next_day'); initial = self.raw(op); due = initial['execute_at']
        self.assertEqual(due, datetime.fromisoformat('2026-10-17T09:00:00+08:00').timestamp())
        self.tick(due-1); self.assertEqual(self.raw(op)['status'], 'scheduled'); self.assertEqual(self.raw(op)['posts'], 0)
        self.tick(due); self.assertEqual(self.raw(op)['status'], 'succeeded'); self.assertEqual(self.raw(op)['posts'], 1)
        self.tick(due+1); self.assertEqual(self.raw(op)['posts'], 1)

    def test_due_card_validation_still_fails_without_debit_or_repeated_payment(self):
        op = self.confirm(); initial = self.raw(op)
        with self.g.bank.transaction() as db:
            db.execute("UPDATE cards SET status='lost' WHERE workspace_id=? AND id=?", (self.w, self.card['id']))
        self.tick(initial['execute_at']); failed = self.raw(op)
        self.assertEqual(failed['status'], 'failed'); self.assertEqual(failed['cash'], initial['cash'])
        self.assertEqual(failed['posts'], 0); self.assertEqual(failed['payments'], 1); self.assertEqual(failed['hold'], 0)
        self.tick(initial['execute_at']+1); self.assertEqual(self.raw(op), failed)
