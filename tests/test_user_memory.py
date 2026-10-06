"""本地财务记忆默认关闭、工作区隔离且由用户控制。"""

import tempfile
import sqlite3
import unittest
from pathlib import Path

from simulated_bank import BankError, SimulatedBank
from user_memory import UserMemoryStore, propose_conversation_memories


class UserMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bank = SimulatedBank(Path(self.temp.name) / "bank.sqlite3")
        first = self.bank.create_workspace()
        second = self.bank.create_workspace()
        self.workspace = first["workspace_id"]
        self.other_workspace = second["workspace_id"]
        self.memories = UserMemoryStore(self.bank)

    def test_default_off_manual_crud_and_explicit_scoped_use(self):
        self.assertFalse(self.memories.state(self.workspace)["enabled"])
        saved = self.memories.create(self.workspace, {
            "category": "planned_commitment", "value": "两个月后预留学费 20,000 元",
        })
        item = saved["items"][0]
        self.assertEqual(item["origin"], "user_declared")
        self.assertTrue(item["user_confirmed"])
        self.assertEqual(self.memories.read_relevant(self.workspace, "未来30天资金时间轴", workflow="cashflow").nodes, ())

        enabled = self.memories.set_enabled(self.workspace, {"enabled": True})
        self.assertTrue(enabled["enabled"])
        context = self.memories.read_relevant(self.workspace, "未来30天资金时间轴", workflow="cashflow")
        self.assertEqual([(node.category, node.value) for node in context.nodes],
                         [("planned_commitment", "两个月后预留学费 20,000 元")])
        self.assertEqual(self.memories.read_relevant(self.workspace, "分析账单", workflow="other").nodes, ())

        corrected = self.memories.correct(self.workspace, item["node_id"], {"value": "两个月后预留学费 22,000 元"})
        self.assertEqual(corrected["items"][0]["value"], "两个月后预留学费 22,000 元")
        self.assertEqual(self.memories.state(self.other_workspace)["items"], [])

        disabled = self.memories.set_enabled(self.workspace, {"enabled": False})
        self.assertFalse(disabled["enabled"])
        self.assertEqual(len(disabled["items"]), 1)
        self.assertEqual(self.memories.read_relevant(self.workspace, "未来30天资金时间轴", workflow="cashflow").nodes, ())

        removed = self.memories.delete(self.workspace, item["node_id"], {})
        self.assertEqual(removed["items"], [])

    def test_rejects_unapproved_categories_credentials_and_oversize_values(self):
        for payload in (
            {"category": "risk_profile", "value": "我能承受较高风险"},
            {"category": "goal", "value": "银行卡号 6222000000000000000"},
            {"category": "goal", "value": "x" * 241},
        ):
            with self.assertRaises(BankError):
                self.memories.create(self.workspace, payload)

    def test_update_and_delete_cannot_cross_workspace(self):
        item = self.memories.create(self.workspace, {"category": "goal", "value": "一年后支付学费"})["items"][0]
        with self.assertRaises(BankError) as corrected:
            self.memories.correct(self.other_workspace, item["node_id"], {"value": "修改后的内容"})
        self.assertEqual(corrected.exception.status, 404)
        with self.assertRaises(BankError) as deleted:
            self.memories.delete(self.other_workspace, item["node_id"], {})
        self.assertEqual(deleted.exception.status, 404)
        self.assertEqual(len(self.memories.state(self.workspace)["items"]), 1)

    def test_conversation_memory_is_only_saved_after_confirmation_and_keeps_provenance(self):
        turn_id = "a" * 32
        proposals = propose_conversation_memories("我的长期目标是毕业前存下 30,000 元", turn_id)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(self.memories.state(self.workspace)["items"], [])
        self.memories.set_enabled(self.workspace, {"enabled": True})
        proposals = self.memories.store.propose_candidates(
            self.workspace, "我的长期目标是毕业前存下 30,000 元", turn_id,
            expected_revision=self.memories.state(self.workspace)["personal_revision"],
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(self.memories.state(self.workspace)["items"], [])
        saved = self.memories.create(self.workspace, {
            key: proposals[0][key] for key in (
                "proposal_id", "source_ref", "confirmation_token", "expected_revision",
            )
        })["items"][0]
        self.assertEqual(saved["source_type"], "conversation_user_statement")
        self.assertEqual(saved["source_ref"], turn_id)
        self.assertTrue(saved["user_confirmed"])
        self.assertIsNotNone(saved["confirmed_at"])

    def test_memory_relations_are_scoped_audited_and_return_with_enabled_context(self):
        first = self.memories.create(self.workspace, {"category": "goal", "value": "一年后支付学费"})["items"][0]
        second = self.memories.create(self.workspace, {
            "category": "planned_commitment", "value": "预留学费 20,000 元",
        })["items"][0]
        before = self.memories.state(self.workspace)["revision"]
        linked = self.memories.add_relation(self.workspace, first["node_id"], {
            "target_node_id": second["node_id"], "relation": "supports",
        })
        self.assertNotEqual(linked["revision"], before)
        self.memories.set_enabled(self.workspace, {"enabled": True})
        context = self.memories.read_relevant(self.workspace, "未来30天资金时间轴", workflow="cashflow")
        self.assertEqual(len(context.relations), 1)
        self.assertEqual(context.relations[0].relation, "supports")
        self.assertEqual(len(linked["items"][0]["relations"]), 1)

        with self.assertRaises(BankError) as error:
            self.memories.add_relation(self.other_workspace, first["node_id"], {
                "target_node_id": second["node_id"], "relation": "supports",
            })
        self.assertEqual(error.exception.status, 404)
        relation_id = linked["items"][0]["relations"][0]["relation_id"]
        deleted = self.memories.delete_relation(self.workspace, relation_id)
        self.assertEqual(deleted["items"][0]["relations"], [])

    def test_expired_memory_remains_visible_but_is_excluded_from_analysis(self):
        saved = self.memories.create(self.workspace, {
            "category": "planned_commitment", "value": "演示用的已确认支出",
            "expires_on": "2026-10-03",
        })
        item = saved["items"][0]
        self.memories.set_enabled(self.workspace, {"enabled": True})
        self.assertEqual(len(self.memories.read_relevant(
            self.workspace, "未来30天资金时间轴", workflow="cashflow").nodes), 1)

        self.bank.advance_days(self.workspace, 1)

        state = self.memories.state(self.workspace)
        self.assertTrue(state["items"][0]["expired"])
        self.assertEqual(state["items"][0]["node_id"], item["node_id"])
        self.assertEqual(self.memories.read_relevant(
            self.workspace, "未来30天资金时间轴", workflow="cashflow").nodes, ())
        with self.assertRaises(BankError) as error:
            self.memories.create(self.workspace, {
                "category": "goal", "value": "过期目标", "expires_on": "2026-10-03",
            })
        self.assertEqual(error.exception.code, "MEMORY_EXPIRY_IN_PAST")

    def test_existing_memory_table_is_upgraded_without_losing_rows(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE user_memories("
                "id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,category TEXT NOT NULL,value TEXT NOT NULL,"
                "origin TEXT NOT NULL,user_confirmed INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO user_memories VALUES(?,?,?,?,?,?,?,?)",
                ("legacy-memory", "legacy-workspace", "goal", "旧目标", "user_declared", 1, 1.0, 1.0),
            )

        bank = SimulatedBank(path)
        with bank.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(user_memories)")}
            row = db.execute("SELECT id,value,expires_on FROM user_memories WHERE id='legacy-memory'").fetchone()
        self.assertIn("expires_on", columns)
        self.assertIn("source_type", columns)
        self.assertIn("confirmed_at", columns)
        self.assertEqual(dict(row), {"id": "legacy-memory", "value": "旧目标", "expires_on": None})


if __name__ == "__main__":
    unittest.main()
