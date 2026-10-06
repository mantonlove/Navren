import tempfile
import copy
import json
import unittest
from contextlib import closing
from pathlib import Path

from finance_gateway import FinanceGateway
from simulated_bank import BankError


class ServiceNegotiationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = FinanceGateway(Path(self.temp.name) / "bank.sqlite3")
        self.workspace, _ = self.gateway.create_session()
        self.service = self.gateway.service_negotiations
        self.request = {
            "text": "我愿意迁入工资账户，但不接受购买理财作为优惠条件；希望协商服务费减免。",
            "share_memory": False,
        }

    def test_rules_return_only_preapproved_offers_and_preserve_user_constraints(self):
        preview = self.service.preview(self.workspace, self.request)

        self.assertEqual(preview["status"], "previewed")
        self.assertEqual({item["offer_id"] for item in preview["offers"]}, {
            "SYNTH-E-STATEMENT-FEE-90D", "SYNTH-PAYROLL-FEE-180D",
        })
        self.assertTrue(preview["conditions"]["salary_deposit_intent"])
        self.assertTrue(preview["conditions"]["investment_purchase_refused"])
        self.assertTrue(all(not item["requires_investment_purchase"] for item in preview["offers"]))
        self.assertTrue(all(item["review_status"] == "rules_checked" for item in preview["offers"]))
        self.assertIn("合成数据", preview["notice"])

    def test_accept_is_explicit_idempotent_and_does_not_change_account_balance(self):
        preview = self.service.preview(self.workspace, self.request)
        offer_id = "SYNTH-PAYROLL-FEE-180D"
        initial = self.gateway.overview(self.workspace)["total_cash_minor"]
        with self.assertRaises(BankError) as unconfirmed:
            self.service.accept(self.workspace, preview["draft_id"],
                                {"offer_id": offer_id, "confirmed": False})
        self.assertEqual(unconfirmed.exception.code, "CONFIRMATION_REQUIRED")

        accepted = self.service.accept(self.workspace, preview["draft_id"], {
            "offer_id": offer_id, "confirmed": True,
        })
        replay = self.service.accept(self.workspace, preview["draft_id"], {
            "offer_id": offer_id, "confirmed": True,
        })
        self.assertEqual(accepted["status"], "submitted_simulation_pending_eligibility")
        self.assertFalse(accepted["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.gateway.overview(self.workspace)["total_cash_minor"], initial)
        self.assertEqual(len(self.service.applications(self.workspace)["applications"]), 1)

    def test_cannot_accept_offer_not_in_current_preview_or_superseded_preview(self):
        first = self.service.preview(self.workspace, self.request)
        second = self.service.preview(self.workspace, {
            "text": "请协商服务费减免，不接受购买理财。", "share_memory": False,
        })
        with self.assertRaises(BankError) as stale:
            self.service.accept(self.workspace, first["draft_id"], {
                "offer_id": "SYNTH-E-STATEMENT-FEE-90D", "confirmed": True,
            })
        self.assertEqual(stale.exception.code, "SERVICE_DRAFT_STALE")
        with self.assertRaises(BankError) as excluded:
            self.service.accept(self.workspace, second["draft_id"], {
                "offer_id": "SYNTH-PAYROLL-FEE-180D", "confirmed": True,
            })
        self.assertEqual(excluded.exception.code, "SERVICE_OFFER_NOT_ELIGIBLE")

    def test_changes_to_shared_memory_invalidate_unexecuted_offer(self):
        item = self.gateway.memories.create(self.workspace, {
            "category": "goal", "value": "毕业前优先积累应急金",
        })["items"][0]
        self.gateway.memories.set_enabled(self.workspace, {"enabled": True})
        preview = self.service.preview(self.workspace, {**self.request, "share_memory": True})
        self.assertEqual([row["node_id"] for row in preview["shared_memory"]], [item["node_id"]])
        self.gateway.memories.correct(self.workspace, item["node_id"], {"value": "已改变的财务目标"})

        with self.assertRaises(BankError) as stale:
            self.service.accept(self.workspace, preview["draft_id"], {
                "offer_id": "SYNTH-E-STATEMENT-FEE-90D", "confirmed": True,
            })
        self.assertEqual(stale.exception.code, "SERVICE_DRAFT_STALE")
        with closing(self.gateway.bank.connect()) as db:
            draft = db.execute("SELECT status FROM service_negotiation_drafts WHERE id=?",
                               (preview["draft_id"],)).fetchone()
        self.assertEqual(draft["status"], "superseded")

    def test_rule_cost_and_rule_version_are_rechecked(self):
        original_path = self.service.rules_path
        self.service.rules_path = Path(self.temp.name) / "bad-rules.json"
        self.service.rules_path.write_text('{"schema_version":"wrong","environment":"synthetic","offers":[]}', encoding="utf-8")
        with self.assertRaises(BankError) as invalid:
            self.service.preview(self.workspace, self.request)
        self.assertEqual(invalid.exception.code, "SERVICE_RULES_INVALID")
        self.service.rules_path = original_path

    def test_refusal_overrides_salary_word_cooccurrence_and_other_consent(self):
        for text in (
            "我不愿意迁入工资，但愿意开通电子账单服务费减免；拒绝购买理财。",
            "我不同意转入工资，但同意电子账单服务费减免；拒绝购买理财。",
            "我拒绝工资迁入，只同意电子账单服务费减免；拒绝购买理财。",
            "我只同意电子账单，不愿迁入工资；请减免服务费，拒绝购买理财。",
            "工资转入怎么办？我想查看服务费优惠。",
            "我可以接受电子账单服务费优惠但不转入工资。",
            "我愿意开通电子账单且不迁入工资，希望服务费减免。",
            "工资转入可以带来什么服务费优惠吗？",
            "我同意电子账单服务费优惠，工资转入的条件是什么？",
        ):
            with self.subTest(text=text):
                preview=self.service.preview(self.workspace,{"text":text,"share_memory":False})
                self.assertFalse(preview["conditions"]["salary_deposit_intent"])
                self.assertEqual([r["offer_id"] for r in preview["offers"]],["SYNTH-E-STATEMENT-FEE-90D"])

    def test_changed_terms_cost_eligibility_dates_and_unbound_draft_cannot_be_accepted(self):
        original=copy.deepcopy(self.service.rules())
        path=Path(self.temp.name)/"current-rules.json"
        self.service.rules_path=path
        for fault in ("terms","cost","eligibility","date","unbound"):
            with self.subTest(fault=fault):
                path.write_text(json.dumps(original),encoding="utf-8")
                preview=self.service.preview(self.workspace,self.request)
                selected="SYNTH-PAYROLL-FEE-180D" if fault=="eligibility" else "SYNTH-E-STATEMENT-FEE-90D"
                changed=copy.deepcopy(original)
                rule=next(r for r in changed["offers"] if r["offer_id"]==selected)
                if fault=="terms":rule.update(monthly_value_minor=250,duration_months=2,max_cost_minor=500)
                elif fault=="cost":rule["max_cost_minor"]+=1
                elif fault=="eligibility":rule["minimum_monthly_salary_minor"]+=300000
                elif fault=="date":rule["valid_through"]="2026-10-01"
                else:
                    with self.gateway.bank.transaction() as db:
                        db.execute("DELETE FROM base_service_negotiation_terms WHERE draft_id=?",(preview["draft_id"],))
                path.write_text(json.dumps(changed),encoding="utf-8")
                with self.assertRaises(BankError) as stale:
                    self.service.accept(self.workspace,preview["draft_id"],{"offer_id":selected,"confirmed":True})
                self.assertEqual(stale.exception.code,"SERVICE_DRAFT_STALE")
                self.assertEqual(stale.exception.status,409)
        self.assertEqual(self.service.applications(self.workspace)["applications"],[])
        with closing(self.gateway.bank.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE event='service_negotiation_application_submitted'").fetchone()[0],0)

    def test_existing_application_replay_preserves_original_terms_after_rule_change(self):
        preview=self.service.preview(self.workspace,self.request)
        payload={"offer_id":"SYNTH-E-STATEMENT-FEE-90D","confirmed":True}
        accepted=self.service.accept(self.workspace,preview["draft_id"],payload)
        changed=copy.deepcopy(self.service.rules())
        changed["offers"]=[]
        path=Path(self.temp.name)/"withdrawn-rules.json"
        path.write_text(json.dumps(changed),encoding="utf-8")
        self.service.rules_path=path
        with self.gateway.bank.transaction() as db:
            db.execute("UPDATE service_negotiation_drafts SET expires_at=0 WHERE id=?",(preview["draft_id"],))
        replay=self.service.accept(self.workspace,preview["draft_id"],payload)
        self.assertEqual(replay["application_id"],accepted["application_id"])
        self.assertEqual(replay["terms"],accepted["terms"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.service.applications(self.workspace)["applications"]),1)


if __name__ == "__main__":
    unittest.main()
