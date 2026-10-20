from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import test_registered_instances as registered_fixture  # noqa: E402
from test_research_workspace import EvidenceFixture  # noqa: E402


class TransactionFaultInjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = registered_fixture.RegisteredInstancesTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gateway = self.fixture.g
        self.lease = self.fixture.select()
        self.workspace_id = self.lease["workspace_id"]
        self.service = self.gateway.research_workspace
        self.service._provider = EvidenceFixture()

    @contextmanager
    def scope(self):
        with self.gateway.demo_sessions.request(self.fixture.token(self.lease), self.workspace_id):
            yield self.workspace_id

    @staticmethod
    def task_value():
        return {
            "title": "PI-X06 synthetic recovery task",
            "kind": "research",
            "record_ids": [],
            "due_on": "2026-12-01",
            "status": "clarifying",
            "conditions": {},
            "notes": "Synthetic fault-injection acceptance fixture.",
            "reminder": {"enabled": True, "calendar": True, "message": True},
        }

    def _counts(self, task_id: str) -> dict[str, int]:
        calendar_id = hashlib.sha256((self.workspace_id + ":research-calendar:" + task_id).encode()).hexdigest()[:32]
        message_id = hashlib.sha256((self.workspace_id + ":research-message:" + task_id).encode()).hexdigest()[:32]
        with closing(self.gateway.bank.connect()) as db:
            bank = {
                "calendar": db.execute("SELECT COUNT(*) FROM calendar_series WHERE id=?", (calendar_id,)).fetchone()[0],
                "message": db.execute("SELECT COUNT(*) FROM navren_messages WHERE id=?", (message_id,)).fetchone()[0],
                "recovery_receipt": db.execute("SELECT COUNT(*) FROM research_workspace_receipts WHERE workspace_id=?", (self.workspace_id,)).fetchone()[0],
            }
        with closing(self.service.store.connect()) as db:
            private = {
                "task": db.execute("SELECT COUNT(*) FROM research_workspace_records WHERE id=? AND workspace_id=? AND deleted=0", (task_id, self.workspace_id)).fetchone()[0],
                "pending_outbox": db.execute("SELECT COUNT(*) FROM research_workspace_outbox WHERE workspace_id=? AND status='pending'", (self.workspace_id,)).fetchone()[0],
                "done_outbox": db.execute("SELECT COUNT(*) FROM research_workspace_outbox WHERE workspace_id=? AND status='done'", (self.workspace_id,)).fetchone()[0],
            }
        return {**private, **bank}

    def _recover_and_assert_exact_once(self, task_id: str) -> None:
        result_path = Path(self.fixture.temp.name) / "restarted-read.json"
        restarted = self._run_in_fresh_process("read", result_path=result_path)
        self.assertEqual(restarted.returncode, 0, restarted.stderr[-1000:])
        recovered = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(len(recovered["items"]), 1)
        self.assertEqual(recovered["items"][0]["id"], task_id)
        self.assertEqual(recovered["items"][0]["record_revision"], 1)
        self.assertEqual(recovered["items"][0]["links"]["calendar_id"], hashlib.sha256((self.workspace_id + ":research-calendar:" + task_id).encode()).hexdigest()[:32])
        self.assertEqual(recovered["items"][0]["links"]["message_id"], hashlib.sha256((self.workspace_id + ":research-message:" + task_id).encode()).hexdigest()[:32])
        after_first_read = self._counts(task_id)
        self.assertEqual(after_first_read, {"task": 1, "pending_outbox": 0, "done_outbox": 1, "calendar": 1, "message": 1, "recovery_receipt": 1})
        from research_workspace import ResearchWorkspace

        restarted_again = ResearchWorkspace(self.gateway, provider=self.service.provider)
        second = restarted_again.read(self.workspace_id, kind="task")
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(self._counts(task_id), after_first_read)

    def _run_in_fresh_process(self, mode: str, *, result_path: Path | None = None, task_value: dict | None = None) -> subprocess.CompletedProcess[str]:
        code = r'''import json, os
from pathlib import Path
from finance_gateway import FinanceGateway
import sys
sys.path.insert(0,"tests")
from test_research_workspace import EvidenceFixture
bank_path=os.environ["PI_BANK_DB"]
g=FinanceGateway(bank_path, financial_database=os.environ["PI_FINANCIAL_DB"], registered_mode=True)
g.bank.clock=lambda:float(os.environ["PI_TEST_NOW"])
w=os.environ["PI_WORKSPACE"]
token=os.environ["PI_TOKEN"]
with g.demo_sessions.request(token,w):
    service=g.research_workspace
    service._provider=EvidenceFixture()
    if os.environ["PI_MODE"]=="crash_after_private_commit":
        service._failpoint=lambda marker:os._exit(79) if marker=="after_private_commit" else None
        service.save(w,{"expected_revision":0,"value":json.loads(os.environ["PI_TASK_JSON"]),"confirmed":True},kind="task")
    else:
        result=service.read(w,kind="task")
        Path(os.environ["PI_RESULT_PATH"]).write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
'''
        env = os.environ.copy()
        env.update({
            "PI_BANK_DB": str(self.gateway.bank.database),
            "PI_FINANCIAL_DB": str(registered_fixture.FINANCIAL),
            "PI_TEST_NOW": str(self.fixture.now[0]),
            "PI_WORKSPACE": self.workspace_id,
            "PI_TOKEN": self.fixture.token(self.lease),
            "PI_MODE": mode,
            "PI_RESULT_PATH": str(result_path or Path(self.fixture.temp.name) / "unused-result.json"),
            "PI_TASK_JSON": json.dumps(task_value or self.task_value(), ensure_ascii=False),
            "NAVREN_MESSAGE_WORKER_ENABLED": "false",
            "NAVREN_PUBLIC_SYNC_ENABLED": "false",
        })
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)

    def test_commit_exception_replays_same_task_message_calendar_and_receipt_after_service_restart(self):
        original = self.gateway.bank.transaction
        injected = {"done": False}

        @contextmanager
        def fail_once_before_bank_commit():
            with original() as bank_db:
                yield bank_db
                if not injected["done"]:
                    injected["done"] = True
                    raise RuntimeError("injected_bank_commit_failure")

        with self.scope():
            self.gateway.bank.transaction = fail_once_before_bank_commit
            try:
                with self.assertRaisesRegex(RuntimeError, "injected_bank_commit_failure"):
                    self.service.save(self.workspace_id, {"expected_revision": 0, "value": self.task_value(), "confirmed": True}, kind="task")
            finally:
                self.gateway.bank.transaction = original

            with closing(self.service.store.connect()) as db:
                task_id = db.execute("SELECT id FROM research_workspace_records WHERE workspace_id=? AND kind='task' AND deleted=0", (self.workspace_id,)).fetchone()[0]
            before = self._counts(task_id)
            self.assertEqual(before, {"task": 1, "pending_outbox": 1, "done_outbox": 0, "calendar": 0, "message": 0, "recovery_receipt": 0})
            self._recover_and_assert_exact_once(task_id)


    def test_hard_process_exit_replays_pending_outbox_in_fresh_process(self):
        with self.scope():
            child = self._run_in_fresh_process("crash_after_private_commit")
            self.assertEqual(child.returncode, 79, child.stderr[-1000:])
            with closing(self.service.store.connect()) as db:
                task_id = db.execute("SELECT id FROM research_workspace_records WHERE workspace_id=? AND kind='task' AND deleted=0", (self.workspace_id,)).fetchone()[0]
            before = self._counts(task_id)
            self.assertEqual(before, {"task": 1, "pending_outbox": 1, "done_outbox": 0, "calendar": 0, "message": 0, "recovery_receipt": 0})
            self._recover_and_assert_exact_once(task_id)


if __name__ == "__main__":
    unittest.main()
