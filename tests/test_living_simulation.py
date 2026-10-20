"""实际 SimulatedBank + 本目录临时库的有限验证；不启动网关/短信/模型。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

from simulated_bank.living_simulation import BankAdapter, LivingError, LivingWorker, calibrate_template
from registered_instances import SCHEMA as REGISTERED_SCHEMA
from simulated_bank.service import SimulatedBank
from simulated_bank.connected_assets import DDL as CONNECTED_DDL


def stamp(day, hour=23):
    return datetime.fromisoformat(f"{day}T{hour:02}:59:59+08:00")


def small_plan(persona, day, *, seed_namespace, observations=None):
    return [{"rule_id": rule, "scheduled_at": day + clock, "kind": kind,
             "category": category, "merchant": merchant, "amount_minor": amount,
             "account_role": "personal_daily", "occurrence_index": 0}
            for rule, clock, kind, category, merchant, amount in (
                ("income.salary", "T09:00:00+08:00", "income", "工资收入", "测试工资", 1000),
                ("life.drink", "T15:00:00+08:00", "expense", "饮品", "测试饮品", 200))]


class Template:
    def __init__(self, path):
        self.path = path
        self.metadata = {"start_date": "2023-10-04", "end_date": "2026-10-03"}
        self.dataset_version = "isolated-template-v1"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE transactions(subject_id TEXT,transaction_id TEXT,category TEXT,kind TEXT,amount_minor INTEGER,booked_on TEXT)")
            for year, month in [(2025, n) for n in (10, 11, 12)] + [(2026, n) for n in range(1, 10)]:
                db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?)", (
                    "working_landlady", f"salary-{year}-{month}", "工资收入", "income", 1900000,
                    f"{year}-{month:02}-25"))
                for day in (1, 5, 10, 15, 20):
                    db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?)", (
                        "working_landlady", f"drink-{year}-{month}-{day}", "饮品", "expense", 2200,
                        f"{year}-{month:02}-{day:02}"))
                for category, kind, amount in (("自营餐饮回款", "income", 3800000), ("餐饮原料", "expense", 1900000)):
                    db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?)", (
                        "retired_entrepreneur", f"restaurant-{kind}-{year}-{month}", category, kind, amount,
                        f"{year}-{month:02}-05"))
            for on, amount in (("2025-10-20", 205000000), ("2026-06-20", 210000000)):
                db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?)", (
                    "grain_farmer", "grain-" + on, "粮食销售回款", "income", amount, on))

    def _connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db


class WorkerTests(unittest.TestCase):
    def test_snapshot_refresh_and_send_guard_reject_changed_ledger(self):
        from simulated_bank import BankError
        self.worker.run_batch(self.workspace,max_days=31)
        original=self.worker.read_snapshots(self.workspace)
        with self.bank.transaction() as db:
            db.execute('UPDATE accounts SET balance_minor=balance_minor+10 WHERE id=?',(self.account,))
            self.bank._ledger(db,self.workspace,self.account,date(2026,10,6),'隔离入账',10,10,'income',None)
            self.bank._bump(db,self.workspace)
        with self.assertRaises(BankError) as error:
            with self.worker.snapshot_send_guard(self.workspace,original['ledger_version']):pass
        self.assertEqual(error.exception.code,'LIVING_LEDGER_CHANGED')
        current=self.worker.read_snapshots(self.workspace)
        self.assertGreater(current['ledger_version'],original['ledger_version'])
        self.assertEqual(current['snapshots'][-1]['cash_minor'],sum(a['balance_minor'] for a in self.rows('accounts')))
        with self.worker.snapshot_send_guard(self.workspace,current['ledger_version']):pass
        with self.assertRaises(BankError) as error:self.bank.advance_days(self.workspace,1)
        self.assertEqual(error.exception.code,'LIVING_CLOCK_MANAGED')

    def test_real_authorized_hold_expires_in_same_snapshot_transaction(self):
        from test_sms_permissions import LocalSms,CODE
        self.bank.permissions.provider=LocalSms(self.bank.clock)
        self.bank.permissions.allow_test_provider=True
        records=self.bank.customer_archive(self.workspace)['records']
        task=next(t for t in records['orchestration_tasks'] if t['status']=='draft')
        amount=sum(q['price_minor'] for q in records['service_quotes'] if q['status']=='illustrative')
        op=self.bank.prepare(self.workspace,{'action':'reserve_funds','account_id':self.account,'task_id':task['id'],
            'amount_minor':amount,'expires_on':'2026-10-07','idempotency_key':'living-real-hold'})
        challenge=self.bank.start_sms_verification(self.workspace,op['id'])
        grant=self.bank.verify(self.workspace,op['id'],{'challenge_id':challenge['challenge_id'],'verification_code':CODE})
        confirmed=self.bank.confirm(self.workspace,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
            'verification_token':grant['verification_token']})
        hold=confirmed['result']['hold_id']
        self.worker.run_batch(self.workspace,now=stamp('2026-10-06'),max_days=31)
        self.assertEqual(next(r for r in self.rows('fund_holds') if r['id']==hold)['status'],'held')
        self.bank.clock=lambda:stamp('2026-10-08').timestamp()
        self.worker.run_batch(self.workspace,max_days=31)
        self.assertEqual(next(r for r in self.rows('fund_holds') if r['id']==hold)['status'],'expired')
        self.assertEqual(self.bank.operation(self.workspace,op['id'])['result'],confirmed['result'])
        page=self.worker.read_snapshots(self.workspace)
        self.assertEqual(page['snapshots'][-1]['cash_minor'],sum(a['balance_minor'] for a in self.rows('accounts')))
        self.assertTrue(all(r['ledger_version']==page['ledger_version'] for r in page['snapshots']))

    def test_special_income_routes_have_no_daily_or_balance_fallback(self):
        with self.bank.transaction() as db:
            for rule,suffix in (('income.business_distribution','broker'),('income.machinery_services','machinery'),('income.restaurant_sales','shop_wallet')):
                account=uuid.uuid4().hex
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)',(account,self.workspace,suffix,'CNY',0,0))
                db.execute('INSERT INTO connected_account_links VALUES(?,?,?,?,?)',
                    (self.workspace,account,'fixture::account:'+suffix,'fixture-'+suffix,'bank_settlement'))
                event={'rule_id':rule,'account_role':'personal_daily'}
                self.assertEqual(self.worker.adapter.resolve_account(db,self.workspace,event)['account_id'],account)
                db.execute('DELETE FROM connected_account_links WHERE account_id=?',(account,))
                self.assertIsNone(self.worker.adapter.resolve_account(db,self.workspace,event))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="navren-living-fixture-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bank = SimulatedBank(root / "isolated-bank.sqlite3", clock=lambda: stamp("2026-10-06").timestamp())
        self.template = Template(root / "isolated-template.sqlite3")
        with closing(self.bank.connect()) as db:
            db.executescript(REGISTERED_SCHEMA)
            db.execute(CONNECTED_DDL[1])
            db.commit()
        self.workspace, self.account = self.new_instance()
        self.worker = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=small_plan)
        self.worker.initialize()

    def new_instance(self):
        result = self.bank.create_workspace()
        workspace = result["workspace_id"]
        with self.bank.transaction() as db:
            account = db.execute("SELECT id FROM accounts WHERE workspace_id=? AND id LIKE '%-main'", (workspace,)).fetchone()[0]
            db.execute("INSERT INTO registered_user_instances VALUES(?,?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, "working_landlady", workspace, "fixture-template", "a" * 64,
                "2026-10-03", uuid.uuid4().hex, "ready", self.bank.clock()))
            db.execute("INSERT INTO connected_account_links VALUES(?,?,?,?,?)", (
                workspace, account, "working_landlady::account:daily", "fixture-connection", "bank_settlement"))
        return workspace, account

    def rows(self, table, workspace=None):
        with closing(self.bank.connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM " + table + " WHERE workspace_id=?", (workspace or self.workspace,))]

    def cash(self, account=None):
        with closing(self.bank.connect()) as db:
            return db.execute("SELECT balance_minor FROM accounts WHERE id=?", (account or self.account,)).fetchone()[0]

    def living_transactions(self, workspace=None):
        return [r for r in self.rows("transactions", workspace) if r["id"].startswith("living-")]

    def test_today_can_run_again_future_events_not_posted_and_no_duplicate(self):
        opening = self.cash()
        first = self.worker.run_batch(self.workspace, now=stamp("2026-10-04", 10))
        self.assertEqual((first["posted"], self.cash()), (1, opening + 1000))
        self.assertEqual(first["closed_through"], "2026-10-03")
        self.assertEqual(CounterStatus(self.rows("living_events")), {"posted": 1, "planned": 1})
        replay = self.worker.run_batch(self.workspace, now=stamp("2026-10-04", 10))
        self.assertEqual(replay["posted"], 0)
        later = self.worker.run_batch(self.workspace, now=stamp("2026-10-04", 16))
        self.assertEqual((later["posted"], self.cash()), (1, opening + 800))
        self.assertEqual(later["closed_through"], "2026-10-03")
        self.assertTrue(later["caught_up"])
        self.assertEqual(len(self.living_transactions()), 2)
        self.assertTrue(all(datetime.fromisoformat(r["occurred_at"]) <= stamp("2026-10-04", 16)
                            for r in self.living_transactions()))

    def test_bounded_restart_catchup_preserves_existing_receipts_and_uses_actual_posting_time(self):
        original = self.rows("transactions")
        baseline = self.cash()
        # 模拟已确认用户操作：回执原件不得因后续补记被重写。
        with self.bank.transaction() as db:
            db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)", (
                "user-confirmed-001", self.workspace, self.account, "2026-10-04T12:00:00+08:00", "本人操作", 500, -500,
                "expense", "购物", "不可改写的原始回执"))
            db.execute("UPDATE accounts SET balance_minor=balance_minor-500 WHERE id=?", (self.account,))
        original = self.rows("transactions")
        receipt_fingerprint = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
        run = self.worker.run_batch(self.workspace, now=stamp("2026-10-06"), max_days=1)
        self.assertEqual(run["closed_through"], "2026-10-04")
        self.assertFalse(run["caught_up"])
        self.assertEqual(self.cash(), baseline - 500 + 800)
        # 旧日快照按实际入账时间重放，补记不能倒插到原回执之前。
        historical = next(r for r in self.rows("living_snapshots") if r["day"] == "2026-10-04")
        self.assertEqual(json.loads(historical["account_balances"])[self.account], baseline - 500)
        restarted = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=small_plan)
        finished = restarted.run_batch(self.workspace, now=stamp("2026-10-06"), max_days=7)
        self.assertEqual(finished["closed_through"], "2026-10-05")
        self.assertEqual(self.cash(), baseline - 500 + 2400)
        self.assertEqual({r["occurred_at"][:10] for r in self.living_transactions()}, {"2026-10-06"})
        after = [r for r in self.rows("transactions") if not r["id"].startswith("living-")]
        self.assertEqual(hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(), receipt_fingerprint)
        self.assertEqual(len({(r["rule_id"], r["scheduled_on"], r["occurrence_index"]) for r in self.rows("living_events")}), 6)

    def test_max_events_resumes_unfinished_past_day(self):
        first = self.worker.run_batch(self.workspace, now=stamp("2026-10-06"), max_events=1)
        self.assertEqual(first["events_processed"], 1)
        self.assertEqual(first["closed_through"], "2026-10-03")
        second = self.worker.run_batch(self.workspace, now=stamp("2026-10-06"), max_events=1)
        self.assertEqual(second["events_processed"], 1)
        self.assertEqual(second["closed_through"], "2026-10-04")
        self.assertEqual(len(self.living_transactions()), 2)

    def test_continuous_and_catchup_have_same_scheduled_event_set(self):
        other, other_account = self.new_instance()
        baseline_a, baseline_b = self.cash(), self.cash(other_account)
        for day in ("2026-10-04", "2026-10-05", "2026-10-06"):
            self.worker.run_batch(self.workspace, now=stamp(day))
        self.worker.run_batch(other, now=stamp("2026-10-06"))
        def scheduled(w):
            return {(r["rule_id"], r["scheduled_on"], r["occurrence_index"], r["status"]) for r in self.rows("living_events", w)}
        self.assertEqual(scheduled(self.workspace), scheduled(other))
        self.assertEqual(self.cash() - baseline_a, self.cash(other_account) - baseline_b)

    def test_failpoint_rolls_back_event_money_snapshot_and_cursor_then_retry(self):
        baseline = self.cash()
        def fail(point, db):
            if point == "after_postings_before_snapshots":
                raise RuntimeError("injected crash")
        broken = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=small_plan, failpoint=fail)
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            broken.run_batch(self.workspace, now=stamp("2026-10-06"))
        self.assertEqual(self.cash(), baseline)
        self.assertEqual(self.rows("living_events"), [])
        self.assertEqual(self.rows("living_snapshots"), [])
        self.assertEqual(self.rows("living_states")[0]["closed_through"], "2026-10-03")
        good = self.worker.run_batch(self.workspace, now=stamp("2026-10-06"))
        self.assertEqual(good["posted"], 6)
        self.assertEqual(self.cash(), baseline + 2400)

    def test_parallel_runs_post_each_occurrence_once(self):
        baseline = self.cash()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.worker.run_batch(self.workspace, now=stamp("2026-10-06")), range(2)))
        self.assertEqual(sum(r["posted"] for r in results), 6)
        self.assertEqual(len(self.living_transactions()), 6)
        self.assertEqual(self.cash(), baseline + 2400)
        snapshot = next(r for r in self.rows("living_snapshots") if r["day"] == "2026-10-06")
        self.assertEqual(json.loads(snapshot["account_balances"])[self.account], self.cash())

    def test_frozen_disabled_closed_and_insufficient_do_not_deduct_or_make_up_income(self):
        for mode, expected in (("disabled", "user_disabled_rule"), ("closed", "user_closed_merchant_mandate"),
                               ("subscription", "user_cancelled_merchant_subscription"),
                               ("frozen", "account_not_active"), ("held", "insufficient_available_funds"),
                               ("locked", "financial_write_locked")):
            with self.subTest(mode=mode):
                workspace, account = self.new_instance()
                baseline = self.cash(account)
                with self.bank.transaction() as db:
                    if mode == "disabled":
                        db.execute("INSERT INTO living_rule_controls VALUES(?,?,0,?)", (workspace, "life.drink", stamp("2026-10-04").isoformat()))
                    elif mode == "closed":
                        db.execute("INSERT INTO mandates VALUES(?,?,?,?,?,'closed',?,'unchanged')", (
                            uuid.uuid4().hex, workspace, account, "测试饮品", 200, "2026-10-04"))
                    elif mode == "frozen":
                        db.execute("UPDATE account_details SET status='frozen' WHERE workspace_id=? AND account_id=?", (workspace, account))
                    elif mode == "subscription":
                        merchant = self.bank._insert_archive_row(db, "merchants", workspace, {
                            "name": "测试饮品", "mcc": "0000", "category": "饮品", "channel": "simulation"})
                        mandate = uuid.uuid4().hex
                        db.execute("INSERT INTO mandates VALUES(?,?,?,?,?,'active',?,'unchanged')", (
                            mandate, workspace, account, "测试饮品", 200, "2026-10-04"))
                        self.bank._insert_archive_row(db, "subscriptions", workspace, {
                            "account_id": account, "mandate_id": mandate, "merchant_id": merchant,
                            "plan_name": "已取消情境", "amount_minor": 200, "cycle": "monthly",
                            "next_renewal_on": "2026-10-04", "merchant_status": "cancelled", "cancellation_route": "simulation"})
                    elif mode == "locked":
                        db.execute("INSERT INTO permission_security(workspace_id,locked) VALUES(?,1) ON CONFLICT(workspace_id) DO UPDATE SET locked=1", (workspace,))
                class HeldAdapter(BankAdapter):
                    def held_minor(self, db, w, a):
                        return baseline + 1000 if mode == "held" else super().held_minor(db, w, a)
                worker = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=small_plan, adapter=HeldAdapter())
                worker.run_batch(workspace, now=stamp("2026-10-04"))
                events = self.rows("living_events", workspace)
                drink = next(e for e in events if e["rule_id"] == "life.drink")
                self.assertEqual(drink["reason"], expected)
                self.assertIsNone(drink["transaction_id"])
                self.assertIsNone(drink["posted_at"])
                self.assertEqual(self.cash(account), baseline if mode in ("frozen", "locked") else baseline + 1000)
                self.assertEqual(len(self.living_transactions(workspace)), 0 if mode in ("frozen", "locked") else 1)

    def test_binding_and_rule_upgrade_fail_closed_old_records_kept(self):
        self.worker.run_batch(self.workspace, now=stamp("2026-10-04"))
        baseline = self.cash()
        changed_version = LivingWorker(self.bank, template_service_for=lambda w: self.template,
                                       planner=small_plan, rules_version="unapproved-new-version")
        with self.assertRaisesRegex(LivingError, "UPGRADE"):
            changed_version.run_batch(self.workspace, now=stamp("2026-10-05"))
        with self.bank.transaction() as db:
            db.execute("UPDATE registered_user_instances SET registered_user_id=? WHERE workspace_id=?", (uuid.uuid4().hex, self.workspace))
        with self.assertRaisesRegex(LivingError, "BINDING_CHANGED"):
            self.worker.run_batch(self.workspace, now=stamp("2026-10-05"))
        self.assertEqual(self.cash(), baseline)
        self.assertEqual(len(self.living_transactions()), 2)

    def test_template_calibration_complete_months_and_default_rule_end_to_end(self):
        observations, receipt = calibrate_template(self.template, "working_landlady", "2026-10-03")
        self.assertEqual((receipt["start_on"], receipt["end_on"]), ("2025-10-01", "2026-09-30"))
        self.assertEqual(observations["rules"]["income.salary"], {"amount_minor": 1900000, "day_of_month": 25})
        self.assertEqual(observations["rules"]["life.drink"]["monthly_occurrences"], 5)
        farming, _ = calibrate_template(self.template, "grain_farmer", "2026-10-03")
        self.assertEqual(farming["rules"]["income.grain_sales"], {
            "amount_range_minor": [205000000, 210000000], "months": [6, 10], "day_of_month": 20})
        restaurant, _ = calibrate_template(self.template, "retired_entrepreneur", "2026-10-03")
        self.assertNotIn("monthly_occurrences", restaurant["rules"]["income.restaurant_sales"])
        self.assertTrue(all(130000 <= v <= 160000 for v in restaurant["rules"]["income.restaurant_sales"]["amount_range_minor"]))
        real_planner = LivingWorker(self.bank, template_service_for=lambda w: self.template)
        result = real_planner.run_batch(self.workspace, now=stamp("2026-10-05"))
        self.assertGreater(result["events_processed"], 0)
        self.assertTrue(all(r["status"] in ("posted", "failed", "cancelled") for r in self.rows("living_events")))
        with closing(self.bank.connect()) as db:
            self.assertTrue(BankAdapter().balances(db, self.workspace))

    def test_worker_round_robin_progresses_beyond_first_limited_instances(self):
        other, _ = self.new_instance()
        first = self.worker.run_ready_instances(now=stamp("2026-10-04"), limit=1)
        second = self.worker.run_ready_instances(now=stamp("2026-10-04"), limit=1)
        self.assertNotEqual(first[0]["workspace_id"], second[0]["workspace_id"])
        self.assertEqual({first[0]["workspace_id"], second[0]["workspace_id"]}, {self.workspace, other})

    def test_empty_days_have_continuous_snapshots_and_invalid_plan_rolls_back(self):
        empty = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=lambda *a, **k: [])
        baseline = self.cash()
        outcome = empty.run_batch(self.workspace, now=stamp("2026-10-06"))
        self.assertEqual(outcome["closed_through"], "2026-10-05")
        self.assertEqual(outcome["posted"], 0)
        self.assertEqual(self.cash(), baseline)
        snapshots = {r["day"]: r for r in self.rows("living_snapshots")}
        self.assertEqual(set(snapshots), {"2026-10-04", "2026-10-05", "2026-10-06"})
        self.assertEqual(snapshots["2026-10-04"]["timing"], "end_of_day")
        self.assertEqual(snapshots["2026-10-06"]["timing"], "intraday")
        other, account = self.new_instance()
        def bad_plan(*args, **kwargs):
            events = small_plan(*args, **kwargs)
            events[0]["amount_minor"] = -1
            return events
        invalid = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=bad_plan)
        before = self.cash(account)
        with self.assertRaisesRegex(LivingError, "PLAN_CONTRACT"):
            invalid.run_batch(other, now=stamp("2026-10-06"))
        self.assertEqual(self.cash(account), before)
        self.assertEqual(self.rows("living_events", other), [])

    def test_clock_rollback_rejected_without_changing_cash_cursor_or_snapshots(self):
        self.worker.run_batch(self.workspace, now=stamp("2026-10-04", 16))
        tables = ("living_states", "living_events", "living_snapshots", "accounts", "transactions", "audit")
        before = {table: self.rows(table) for table in tables}
        self.assertEqual(before["living_states"][0]["last_checked_at"], stamp("2026-10-04", 16).isoformat())
        restarted = LivingWorker(self.bank, template_service_for=lambda w: self.template, planner=small_plan)
        with self.assertRaisesRegex(LivingError, "TRUSTED_CLOCK_ROLLBACK"):
            restarted.run_batch(self.workspace, now=stamp("2026-10-04", 10))
        self.assertEqual({table: self.rows(table) for table in tables}, before)
        # 同一时间边界也检查用户已有入账，而非只信worker自己的时间字段。
        with self.bank.transaction() as db:
            db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)", (
                "user-later-than-worker-clock", self.workspace, self.account, "2026-10-04T17:00:00+08:00",
                "本人后续入账", 100, 100, "income", "其他收入", "已有用户入账不能被回溯快照掩盖"))
            db.execute("UPDATE accounts SET balance_minor=balance_minor+100 WHERE id=?", (self.account,))
        before = {table: self.rows(table) for table in tables}
        with self.assertRaisesRegex(LivingError, "LEDGER_AFTER_TRUSTED_TIME"):
            restarted.run_batch(self.workspace, now=stamp("2026-10-04", 16))
        self.assertEqual({table: self.rows(table) for table in tables}, before)

    def test_bad_instance_error_is_reported_and_does_not_starve_other_instances(self):
        bad, _ = self.new_instance()
        with self.bank.transaction() as db:
            db.execute("UPDATE registered_user_instances SET template_end_on='1900-01-01' WHERE workspace_id=?", (bad,))
        first = self.worker.run_ready_instances(now=stamp("2026-10-04"), limit=1)
        self.assertEqual(first[0]["workspace_id"], bad)
        self.assertEqual(first[0]["status"], "error")
        self.assertIsNone(first[0]["result"])
        self.assertEqual(first[0]["error"]["code"], "LIVING_TEMPLATE_END_MISMATCH")
        self.assertEqual(self.rows("living_states"), [])
        self.assertEqual(self.rows("living_states", bad), [])
        second = self.worker.run_ready_instances(now=stamp("2026-10-04"), limit=1)
        self.assertEqual(second[0]["workspace_id"], self.workspace)
        self.assertEqual(second[0]["status"], "ok")
        self.assertIsNone(second[0]["error"])
        self.assertEqual(second[0]["result"]["posted"], 2)
        mixed = self.worker.run_ready_instances(now=stamp("2026-10-04"), limit=2)
        self.assertEqual(len(mixed), 2)
        self.assertEqual({r["workspace_id"]: r["status"] for r in mixed}, {bad: "error", self.workspace: "ok"})
        self.assertEqual(len(self.living_transactions()), 2)
        self.assertEqual(self.living_transactions(bad), [])
        self.assertEqual(self.rows("living_schedule", bad)[0]["status"], "error")


def CounterStatus(events):
    result = {}
    for event in events:
        result[event["status"]] = result.get(event["status"], 0) + 1
    return result


if __name__ == "__main__":
    unittest.main(verbosity=2)
