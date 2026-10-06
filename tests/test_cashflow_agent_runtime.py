"""现金流功能经任务图、证据门、规则门与独立审核的集成测试。"""

import unittest

from agent_runtime.cashflow_finance import (
    CashflowPolicyGate,
    CashflowReviewer,
    run_cashflow_workflow,
)
from agent_runtime.contracts import (
    AdviceDraft,
    AdviceRequest,
    AgentResult,
    ReviewVerdict,
    Severity,
)


def fixture_data(multiple_currencies=False):
    accounts = [
        {
            "id": "acct-main",
            "name": "日常账户",
            "currency": "CNY",
            "balance_minor": 100_000,
        }
    ]
    projected_accounts = [
        {
            "account_id": "acct-main",
            "account_name": "日常账户",
            "currency": "CNY",
            "opening_balance_minor": 100_000,
            "current_available_minor": 90_000,
            "projected_available_after_confirmed_minor": 70_000,
            "minimum_available_after_confirmed_minor": 70_000,
            "minimum_available_if_candidates_occur_minor": 65_000,
            "projected_balance_after_confirmed_minor": 80_000,
            "projected_balance_if_candidates_occur_minor": 75_000,
            "reserve_floor_minor": 5_000,
            "held_minor": 5_000,
        }
    ]
    if multiple_currencies:
        accounts.append({
            "id": "acct-usd",
            "name": "留学账户",
            "currency": "USD",
            "balance_minor": 123_456,
        })
        projected_accounts.append({
            "account_id": "acct-usd",
            "account_name": "留学账户",
            "currency": "USD",
            "opening_balance_minor": 123_456,
            "current_available_minor": 123_456,
            "projected_available_after_confirmed_minor": 120_000,
            "minimum_available_after_confirmed_minor": 120_000,
            "minimum_available_if_candidates_occur_minor": 120_000,
            "projected_balance_after_confirmed_minor": 120_000,
            "projected_balance_if_candidates_occur_minor": 120_000,
            "reserve_floor_minor": 0,
            "held_minor": 0,
        })

    state = {
        "workspace_id": "workspace-1",
        "version": 3,
        "accounts": accounts,
    }
    timeline = {
        "environment": "simulation",
        "source": "simulated_bank_archive",
        "workspace_id": "workspace-1",
        "version": 3,
        "as_of": "2026-10-03",
        "horizon_end": "2026-11-02",
        "horizon_days": 30,
        "notice": "这是模拟推演，不预支未来收入。",
        "reserve_unallocated_minor": 0,
        "accounts": projected_accounts,
        "timeline": [{
            "date": "2026-10-09",
            "events": [{
                "id": "subscription:sub-1:2026-10-09",
                "date": "2026-10-09",
                "scheduled_date": "2026-10-09",
                "account_id": "acct-main",
                "currency": "CNY",
                "amount_minor": 5_000,
                "cash_delta_minor": -5_000,
                "reserved_delta_minor": 0,
                "kind": "subscription_renewal",
                "certainty": "candidate",
                "title": "云存储续费候选",
                "date_status": "candidate",
                "evidence_ref": "simulated_bank.customer_archive:subscriptions/sub-1",
            }],
        }],
    }
    return state, timeline


class CashflowAgentRuntimeTests(unittest.TestCase):
    def test_cashflow_uses_snapshot_then_projection_and_is_reviewed(self):
        state, timeline = fixture_data()

        result = run_cashflow_workflow("展示30天资金时间轴", state, timeline)

        self.assertEqual(result["status"], "approved")
        self.assertEqual(
            [(task["task_id"], task["capability"], task["depends_on"])
             for task in result["task_graph"]],
            [
                ("account-snapshot", "account_snapshot", []),
                ("cashflow-projection", "cashflow_projection", ["account-snapshot"]),
            ],
        )
        outcomes = {(item["stage"], item["outcome"]) for item in result["trace"]}
        self.assertIn(("policy.account-snapshot", "passed"), outcomes)
        self.assertIn(("policy.cashflow-projection", "passed"), outcomes)
        self.assertIn(("policy.final", "passed"), outcomes)
        self.assertIn(("reviewer.review", "approve"), outcomes)
        self.assertIn("未来 30 天资金时间轴", result["answer"])
        self.assertIn("最低可用 ¥700.00", result["answer"])
        self.assertIn("若周期扣费候选发生", " ".join(item["text"] for item in result["claims"]))
        evidence_ids = {item["evidence_id"] for item in result["evidence"]}
        self.assertTrue(result["claims"])
        self.assertTrue(all(set(item["evidence_ids"]) <= evidence_ids for item in result["claims"]))
        self.assertIn("event:subscription:sub-1:2026-10-09",
                      result["claims"][-1]["evidence_ids"])
        self.assertEqual(result["bank_actions"], [])

    def test_different_currencies_are_kept_separate(self):
        state, timeline = fixture_data(multiple_currencies=True)

        result = run_cashflow_workflow("展示30天资金时间轴", state, timeline)

        self.assertEqual(result["status"], "approved")
        self.assertIn("留学账户（USD）", result["answer"])
        self.assertIn("不与人民币合计", result["answer"])
        self.assertNotIn("现金合计", result["answer"])
        self.assertNotIn("资金合计", result["answer"])

    def test_unallocated_reserve_is_in_reviewed_answer_with_its_own_evidence(self):
        state, timeline = fixture_data()
        timeline["reserve_unallocated_minor"] = 12_345

        result = run_cashflow_workflow("展示30天资金时间轴", state, timeline)

        self.assertEqual(result["status"], "approved")
        self.assertIn("应急保留金额 ¥123.45 未能对应到唯一人民币主账户", result["answer"])
        reserve_claim = next(claim for claim in result["claims"]
                             if "应急保留金额" in claim["text"])
        self.assertEqual(reserve_claim["evidence_ids"], ["reserve:unallocated"])
        self.assertIn("reserve:unallocated", {item["evidence_id"] for item in result["evidence"]})

    def test_workspace_mismatch_fails_before_any_summary_is_returned(self):
        state, timeline = fixture_data()
        timeline["workspace_id"] = "another-workspace"

        with self.assertRaisesRegex(ValueError, "工作区不一致"):
            run_cashflow_workflow("展示30天资金时间轴", state, timeline)

    def test_policy_blocks_any_bank_action_draft(self):
        request = AdviceRequest("r1", "展示资金时间轴", customer_facts={
            "cashflow": {"horizon_days": 30, "as_of": "2026-10-03",
                         "horizon_end": "2026-11-02", "accounts": [{"currency": "CNY"}]}
        })
        result = CashflowPolicyGate().check(
            request, AdviceDraft("资金建议", proposed_actions=("transfer",)),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.findings[0].code, "CASHFLOW_ACTION_FORBIDDEN")

    def test_policy_blocks_inconsistent_cashflow_window(self):
        request = AdviceRequest("r1", "展示资金时间轴", customer_facts={
            "cashflow": {"horizon_days": 30, "as_of": "2026-10-03",
                         "horizon_end": "2026-11-01", "accounts": [{"currency": "CNY"}]}
        })

        result = CashflowPolicyGate().check(request, AdviceDraft("未来 30 天资金时间轴"))

        self.assertFalse(result.allowed)
        self.assertEqual(result.findings[0].code, "INVALID_CASHFLOW_WINDOW")

    def test_policy_rejects_amounts_not_present_in_recalculable_evidence(self):
        from agent_runtime import Claim, EvidenceItem

        request = AdviceRequest("amount-1", "展示资金时间轴", customer_facts={
            "cashflow": {"horizon_days": 30, "as_of": "2026-10-03",
                         "horizon_end": "2026-11-02", "accounts": [{"currency": "CNY"}]}
        }, evidence=(EvidenceItem("e-account", "synthetic_archive",
                                  '{"current_available_minor":12345}'),))
        gate = CashflowPolicyGate()

        approved = gate.check(request, AdviceDraft("可用 ¥123.45。", claims=(
            Claim("可用 ¥123.45。", ("e-account",)),
        )))
        rejected = gate.check(request, AdviceDraft("可用 ¥999.99。", claims=(
            Claim("可用 ¥999.99。", ("e-account",)),
        )))

        self.assertTrue(approved.allowed)
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.findings[0].code, "UNSUPPORTED_CASHFLOW_AMOUNT")

    def test_reviewer_rejects_a_graph_without_snapshot_dependency(self):
        request = AdviceRequest("r1", "展示资金时间轴", customer_facts={
            "expected_task_graph": (("account-snapshot", "account_snapshot"),
                                    ("cashflow-projection", "cashflow_projection"))
        })
        outputs = (AgentResult("cashflow-projection", "navren-cashflow", "cashflow_projection",
                              AdviceDraft("推演", claims=())),)

        result = CashflowReviewer().review(request, outputs, AdviceDraft("推演"))

        self.assertEqual(result.verdict, ReviewVerdict.REJECT)
        self.assertEqual(result.findings[0].severity, Severity.CRITICAL)


if __name__ == "__main__":
    unittest.main()
