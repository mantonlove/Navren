"""服务发布互斥、入锁后到期及父事务提交的实际隔离反例。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager, ExitStack
from unittest.mock import patch

from cryptography.fernet import Fernet
from finance_gateway import FinanceGateway
from service_negotiation import ServiceNegotiationService
from agent_runtime.bank_tools import BankTools
from simulated_bank import BankError, SimulatedBank


class ServiceRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(),
                                     "NAVREN_KNOWLEDGE_DB": str(Path(self.temp.name) / "knowledge.sqlite3")})
        env.start(); self.addCleanup(env.stop)
        self.g = FinanceGateway(Path(self.temp.name) / "bank.sqlite3")
        self.w, _ = self.g.create_session()
        self.s = self.g.service_negotiations
        self.rules = copy.deepcopy(self.s.rules())
        self.s.rules_path = Path(self.temp.name) / "rules.json"
        self.s.rules_path.write_text(json.dumps(self.rules), encoding="utf-8")
        self.text = "我愿意迁入工资账户，希望协商服务费减免。"

    def snapshot(self):
        preview = self.s.preview(self.w, {"text": self.text, "share_memory": False})
        with closing(self.g.bank.connect()) as db:
            row = dict(db.execute("SELECT * FROM service_negotiation_drafts WHERE id=?", (preview["draft_id"],)).fetchone())
        return {"schema_version": 1, "workspace_id": self.w,
                "rules_sha256": BankTools._service_fingerprint(self.s.rules()), "applications_sha256": None,
                "preview": {"draft_id": row["id"], "sha256": BankTools._service_fingerprint(row)}}, row

    def counts(self):
        with closing(self.g.bank.connect()) as db:
            return [db.execute(sql, (self.w,)).fetchone()[0] for sql in (
                "SELECT count(*) FROM service_negotiation_applications WHERE workspace_id=?",
                "SELECT count(*) FROM audit WHERE workspace_id=? AND event='service_negotiation_application_submitted'",
                "SELECT count(*) FROM operations WHERE workspace_id=?",
                "SELECT sum(balance_minor) FROM accounts WHERE workspace_id=?")]

    def change(self):
        result = copy.deepcopy(self.rules)
        result["offers"][0]["conditions"] = ["修订的独立电子账单条件"]
        return result

    def test_metadiscussion_and_cross_clause_noncommitment_are_unknown(self):
        for text in ("我想了解愿意迁入工资后有哪些服务费优惠。",
                     "我可以迁入工资；只是问问，希望了解服务费优惠。",
                     "我愿意迁入工资，但尚未作出承诺，希望协商服务费优惠。",
                     "不是说我愿意迁入工资，希望协商服务费减免。",
                     "我既同意迁入工资又不同意迁入工资，希望协商服务费优惠。"):
            with self.subTest(text=text):
                view = self.s.preview(self.w, {"text": text, "share_memory": False})
                self.assertIs(view["conditions"]["salary_deposit_intent"], False)
                self.assertIs(view["conditions"]["salary_deposit_refused"], False)
                self.assertEqual([r["offer_id"] for r in view["offers"]], ["SYNTH-E-STATEMENT-FEE-90D"])
                self.assertIn("工资", view["message"])

    def test_rule_change_after_initial_check_before_public_lock_rejected_at_final_gate(self):
        snapshot, _ = self.snapshot(); before = self.counts(); published = []
        BankTools.assert_service_snapshot(self.g, self.w, snapshot)
        @contextmanager
        def public_lock():
            self.s.publish_rules(self.change(), expected_sha256=snapshot["rules_sha256"])
            yield
        with self.assertRaises(BankError) as raised:
            with ExitStack() as locks, self.g.bank.transaction() as db:
                locks.enter_context(public_lock())
                locks.enter_context(self.s.evidence_guard(self.w, snapshot, db=db))
                published.append("旧解释")
        self.assertEqual(raised.exception.code, "STALE_SERVICE_EVIDENCE")
        self.assertEqual(published, []); self.assertEqual(before, self.counts())

    def test_expiry_after_public_lock_and_at_each_send_is_rechecked(self):
        snapshot, row = self.snapshot(); before = self.counts(); now = [row["expires_at"] - 1]
        with patch.object(self.g.bank, "clock", side_effect=lambda: now[0]):
            BankTools.assert_service_snapshot(self.g, self.w, snapshot)
            now[0] = row["expires_at"]
            with self.assertRaises(BankError):
                with ExitStack() as locks, self.g.bank.transaction() as db:
                    locks.enter_context(self.s.evidence_guard(self.w, snapshot, db=db))
                    self.fail("过期预览不能进入发送段")
            now[0] = row["expires_at"] - 1
            with ExitStack() as locks, self.g.bank.transaction() as db:
                locks.enter_context(self.s.evidence_guard(self.w, snapshot, db=db))
                now[0] = row["expires_at"]
                with self.assertRaises(BankError): self.s.assert_evidence(self.w, snapshot, db=db)
        self.assertEqual(before, self.counts())

    def test_publisher_other_instance_blocks_and_reentrant_publication_is_rejected(self):
        snapshot, _ = self.snapshot(); started = threading.Event(); done = threading.Event(); errors = []
        other = ServiceNegotiationService.__new__(ServiceNegotiationService); other.rules_path = self.s.rules_path
        def publish():
            started.set()
            try: other.publish_rules(self.change(), expected_sha256=snapshot["rules_sha256"])
            except BaseException as error: errors.append(error)
            finally: done.set()
        with ExitStack() as locks, self.g.bank.transaction() as db:
            locks.enter_context(self.s.evidence_guard(self.w, snapshot, db=db))
            thread = threading.Thread(target=publish); thread.start()
            self.assertTrue(started.wait(2)); self.assertFalse(done.wait(.15))
            with self.assertRaises(BankError) as raised:
                self.s.publish_rules(self.change(), expected_sha256=snapshot["rules_sha256"])
            self.assertEqual(raised.exception.code, "SERVICE_RULES_BUSY")
        thread.join(3); self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(self.s.rules(), self.change())

    def test_publisher_other_process_uses_the_same_stable_flock(self):
        snapshot, _ = self.snapshot()
        payload = Path(self.temp.name) / "next.json"; payload.write_text(json.dumps(self.change()))
        started = Path(self.temp.name) / "started"; done = Path(self.temp.name) / "done"
        code = """import sys,json
from pathlib import Path
from service_negotiation import ServiceNegotiationService as S
s=S.__new__(S);s.rules_path=Path(sys.argv[1]);Path(sys.argv[3]).write_text('start')
s.publish_rules(json.loads(Path(sys.argv[2]).read_text()),expected_sha256=sys.argv[5])
Path(sys.argv[4]).write_text('done')
"""
        with ExitStack() as locks, self.g.bank.transaction() as db:
            locks.enter_context(self.s.evidence_guard(self.w, snapshot, db=db))
            alternate_tmp = Path(self.temp.name) / "alternate-tmp"; alternate_tmp.mkdir()
            process = subprocess.Popen([sys.executable, "-c", code, str(self.s.rules_path), str(payload),
                str(started), str(done), snapshot["rules_sha256"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "TMPDIR": str(alternate_tmp)})
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            import time
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(started.exists()); self.assertFalse(done.exists()); self.assertIsNone(process.poll())
        stdout, stderr = process.communicate(timeout=4)
        self.assertEqual(process.returncode, 0, stderr.decode()); self.assertTrue(done.exists())

    def test_compare_and_swap_rejects_stale_publication(self):
        old = self.s._rules_digest(self.rules)
        self.s.publish_rules(self.change(), expected_sha256=old)
        with self.assertRaises(BankError) as raised: self.s.publish_rules(self.rules, expected_sha256=old)
        self.assertEqual(raised.exception.code, "SERVICE_RULES_VERSION_CONFLICT")
        self.assertEqual(self.s.rules(), self.change())

    def test_accept_holds_rules_until_bank_commit_then_publisher_can_proceed(self):
        snapshot, row = self.snapshot(); started = threading.Event(); done = threading.Event(); failures = []
        original = self.g.bank.transaction
        other = ServiceNegotiationService.__new__(ServiceNegotiationService); other.rules_path = self.s.rules_path
        def publish():
            started.set()
            try: other.publish_rules(self.change(), expected_sha256=snapshot["rules_sha256"])
            except BaseException as error: failures.append(error)
            finally: done.set()
        @contextmanager
        def paused_commit():
            with original() as db:
                yield db
                thread.start(); self.assertTrue(started.wait(2)); self.assertFalse(done.wait(.15))
        thread = threading.Thread(target=publish)
        with patch.object(self.g.bank, "transaction", side_effect=paused_commit):
            receipt = self.s.accept(self.w, row["id"], {"offer_id": "SYNTH-E-STATEMENT-FEE-90D", "confirmed": True})
        thread.join(3); self.assertFalse(thread.is_alive()); self.assertEqual(failures, [])
        self.assertEqual(self.counts()[:3], [1, 1, 0])
        self.assertEqual(receipt["terms"]["conditions"], self.rules["offers"][0]["conditions"])

    def test_accept_samples_clock_after_waiting_for_rule_lock_and_rejects_boundary(self):
        snapshot, row = self.snapshot(); before = self.counts(); now = [row["expires_at"] - 1]
        original = self.s.rules_guard
        @contextmanager
        def delayed_guard(**args):
            with original(**args) as local:
                now[0] = row["expires_at"]
                yield local
        with patch.object(self.g.bank, "clock", side_effect=lambda: now[0]), patch.object(self.s, "rules_guard", side_effect=delayed_guard):
            with self.assertRaises(BankError) as raised:
                self.s.accept(self.w, row["id"], {"offer_id": "SYNTH-E-STATEMENT-FEE-90D", "confirmed": True})
        self.assertEqual(raised.exception.code, "SERVICE_DRAFT_STALE"); self.assertEqual(before, self.counts())

    def test_old_condition_policy_requires_new_preview_but_successful_replay_survives(self):
        _, row = self.snapshot(); before = self.counts()
        with self.g.bank.transaction() as db: db.execute("DELETE FROM base_service_condition_policies WHERE draft_id=?", (row["id"],))
        with self.assertRaises(BankError): self.s.accept(self.w, row["id"], {"offer_id": "SYNTH-E-STATEMENT-FEE-90D", "confirmed": True})
        self.assertEqual(before, self.counts())
        _, fresh = self.snapshot(); body = {"offer_id": "SYNTH-E-STATEMENT-FEE-90D", "confirmed": True}
        accepted = self.s.accept(self.w, fresh["id"], body)
        with self.g.bank.transaction() as db: db.execute("DELETE FROM base_service_condition_policies WHERE draft_id=?", (fresh["id"],))
        self.s.publish_rules(self.change(), expected_sha256=self.s._rules_digest(self.rules))
        replay = self.s.accept(self.w, fresh["id"], body)
        self.assertEqual(replay["application_id"], accepted["application_id"])
        self.assertEqual(replay["terms"], accepted["terms"]); self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.counts()[:3], [1, 1, 0])


class BankCommitLockTests(unittest.TestCase):
    def test_verify_and_confirm_release_public_lock_after_real_sqlite_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            bank = SimulatedBank(Path(temp) / "bank.sqlite3")
            credentials = bank.create_workspace(); workspace = credentials["workspace_id"]
            account = next(r["id"] for r in bank.snapshot(workspace)["accounts"] if r["name"] == "模拟日常账户")
            operation = bank.prepare(workspace, {"action": "purchase", "account_id": account,
                "product_id": "SIM-CASH-01", "amount_minor": 500000, "idempotency_key": "commit-lock"})
            releases = []
            @contextmanager
            def guard(db, owner, row):
                yield
                with closing(bank.connect()) as observed:
                    releases.append({"state": observed.execute("SELECT state FROM operations WHERE id=?", (row["id"],)).fetchone()[0],
                        "verified": observed.execute("SELECT count(*) FROM audit WHERE operation_id=? AND event='verification_passed'", (row["id"],)).fetchone()[0]})
            bank.base_evidence_guard = guard
            grant = bank.verify(workspace, operation["id"], {"verification_code": credentials["human_verification_code"]})
            self.assertEqual(releases[0]["verified"], 1)
            bank.confirm(workspace, operation["id"], {"confirmed": True, "confirmation_digest": operation["confirmation_digest"],
                "verification_token": grant["verification_token"]})
            self.assertEqual(releases[1]["state"], "succeeded")


if __name__ == "__main__": unittest.main()
