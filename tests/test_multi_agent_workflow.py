"""验证动态 Agent 任务图、记忆隔离、政策门和独立审核。"""

import unittest

from agent_runtime import (
    AdviceDraft,
    AdviceRequest,
    AgentDescriptor,
    AgentResult,
    AgentTask,
    Claim,
    EvidenceItem,
    Finding,
    MemoryGraphContext,
    MemoryNode,
    MemoryOrigin,
    MemoryPermissions,
    MemoryProposal,
    MultiAgentConfig,
    MultiAgentOrchestrator,
    PolicyResult,
    ReviewDecision,
    ReviewVerdict,
    RoutePlan,
    RunStatus,
    Severity,
)


class FakeSupervisor:
    model_id = "fake-supervisor"

    def __init__(self, plan):
        self.plan = plan
        self.catalog = ()
        self.synthesis_inputs = []
        self.synthesis_revision_feedback = []

    def route(self, request, agent_catalog):
        self.catalog = agent_catalog
        return self.plan

    def synthesize(self, request, outputs):
        self.synthesis_inputs.append(tuple(item.task_id for item in outputs))
        text = "\n".join(item.draft.text for item in outputs)
        claims = tuple(claim for item in outputs for claim in item.draft.claims)
        return AdviceDraft(text=text, claims=claims)

    def revise_synthesis(self, request, outputs, draft, feedback):
        self.synthesis_revision_feedback.append(tuple(feedback))
        return AdviceDraft(text=draft.text + " 已补充审核要求。", claims=draft.claims)


class FakeAgent:
    def __init__(self, agent_id, capabilities, tool_scopes=()):
        self.agent_id = agent_id
        self.capabilities = tuple(capabilities)
        self.tool_scopes = tuple(tool_scopes)
        self.data_scopes = ("simulated_customer_archive",)
        self.model_id = f"fake-{agent_id}"
        self.calls = []
        self.revision_feedback = []
        self.request_contexts = []

    def run(self, request, task, dependencies):
        self.calls.append((task.task_id, tuple(item.task_id for item in dependencies)))
        self.request_contexts.append(request)
        return AdviceDraft(
            text=f"{task.capability}已分析。",
            claims=(Claim(f"{task.capability}来源结论。", ("e-1",)),),
        )

    def revise(self, request, task, draft, dependencies, feedback):
        self.revision_feedback.append((task.task_id, feedback))
        return AdviceDraft(
            text=draft.text + "已补充期限依据。",
            claims=draft.claims,
        )


class FakeReviewer:
    model_id = "fake-reviewer"

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.seen = []

    def review(self, request, outputs, final_draft):
        self.seen.append((tuple(item.task_id for item in outputs), final_draft.text))
        return self.decisions.pop(0)


class FakePolicy:
    def __init__(self, results=None):
        self.results = list(results or [PolicyResult(True)])
        self.checked = []

    def check(self, request, draft):
        self.checked.append(draft.text)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeMemoryGraph:
    def __init__(self, context):
        self.context = context
        self.read_args = []
        self.committed = []

    def read_relevant(self, subject_id, query, allowed_categories):
        self.read_args.append((subject_id, query, allowed_categories))
        return self.context

    def commit(self, subject_id, proposals):
        self.committed.append((subject_id, proposals))
        return tuple(f"mem-{i}" for i, _ in enumerate(proposals, start=1))

    def list_all(self, subject_id):
        return self.context

    def correct(self, subject_id, node_id, value):
        return None

    def delete(self, subject_id, node_id):
        return None

    def disable(self, subject_id):
        return None


class FakeMemoryCurator:
    model_id = "fake-memory-curator"

    def __init__(self, proposals):
        self.proposals = proposals

    def propose_updates(self, request, outputs, final_draft):
        return self.proposals


class FakeMemoryGate:
    def __init__(self):
        self.calls = []

    def authorize(self, subject_id, proposals, permissions):
        self.calls.append((subject_id, proposals, permissions))
        return proposals


def make_request(**kwargs):
    values = dict(
        request_id="multi-1",
        question="分析账单后给我适合的模拟理财建议",
        customer_facts={"risk_level": "R2", "horizon_days": 180},
        constraints=("不能超风险推荐", "交易需要用户确认"),
        evidence=(EvidenceItem("e-1", "simulated_bank", "风险等级 R2；产品 SIM-1 风险等级 R2。"),),
    )
    values.update(kwargs)
    return AdviceRequest(**values)


def build_workflow(plan, agents, decisions=None, policy=None, **kwargs):
    reviewer = FakeReviewer(decisions or [ReviewDecision(ReviewVerdict.APPROVE)])
    supervisor = FakeSupervisor(plan)
    policy = policy or FakePolicy()
    workflow = MultiAgentOrchestrator(supervisor, agents, reviewer, policy, **kwargs)
    return workflow, supervisor, reviewer, policy


class MultiAgentWorkflowTests(unittest.TestCase):
    def test_one_agent_can_cover_multiple_capabilities_and_request_uses_one_agent(self):
        agent = FakeAgent("finance-generalist", ("statement_analysis", "product_comparison"))
        task = AgentTask("t1", "finance-generalist", "statement_analysis", "analyze statement")
        workflow, supervisor, reviewer, _ = build_workflow(RoutePlan((task,)), {agent.agent_id: agent})

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(len(supervisor.catalog), 1)
        self.assertEqual(supervisor.catalog[0].capabilities,
                         ("statement_analysis", "product_comparison"))
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(len(reviewer.seen), 1)

    def test_dynamic_task_graph_passes_only_declared_dependencies(self):
        ledger = FakeAgent("ledger", ("statement_analysis",))
        adviser = FakeAgent("adviser", ("product_comparison",))
        tasks = (
            AgentTask("read-ledger", "ledger", "statement_analysis", "summarize cash flow"),
            AgentTask("compare-products", "adviser", "product_comparison", "compare eligible products",
                      depends_on=("read-ledger",)),
        )
        workflow, supervisor, reviewer, _ = build_workflow(
            RoutePlan(tasks), {ledger.agent_id: ledger, adviser.agent_id: adviser})

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(adviser.calls, [("compare-products", ("read-ledger",))])
        self.assertEqual(reviewer.seen[0][0], ("read-ledger", "compare-products"))
        self.assertEqual(supervisor.synthesis_inputs, [("read-ledger", "compare-products")])

    def test_same_agent_can_be_reused_for_multiple_tasks(self):
        agent = FakeAgent("finance", ("bill", "budget"))
        tasks = (
            AgentTask("t-bill", "finance", "bill", "summarize bills"),
            AgentTask("t-budget", "finance", "budget", "estimate free cash", depends_on=("t-bill",)),
        )
        result = MultiAgentOrchestrator(
            FakeSupervisor(RoutePlan(tasks)), {"finance": agent},
            FakeReviewer([ReviewDecision(ReviewVerdict.APPROVE)]), FakePolicy(),
        ).run(make_request())

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(agent.calls, [("t-bill", ()), ("t-budget", ("t-bill",))])

    def test_review_targets_task_for_one_bounded_revision(self):
        agent = FakeAgent("adviser", ("product_comparison",))
        task = AgentTask("compare", "adviser", "product_comparison", "compare products")
        decisions = [
            ReviewDecision(ReviewVerdict.REVISE, revision_instructions=("说明期限证据",),
                           revision_task_ids=("compare",)),
            ReviewDecision(ReviewVerdict.APPROVE),
        ]
        workflow, _, reviewer, _ = build_workflow(RoutePlan((task,)), {"adviser": agent}, decisions)

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(agent.revision_feedback, [("compare", ("说明期限证据",))])
        self.assertEqual(len(reviewer.seen), 2)

    def test_review_can_request_one_targeted_supervisor_synthesis_revision(self):
        agent = FakeAgent("adviser", ("product_comparison",))
        task = AgentTask("compare", "adviser", "product_comparison", "compare products")
        decisions = [
            ReviewDecision(ReviewVerdict.REVISE, revision_instructions=("明确结论适用范围",),
                           revision_task_ids=("supervisor.synthesis",)),
            ReviewDecision(ReviewVerdict.APPROVE),
        ]
        workflow, supervisor, reviewer, policy = build_workflow(
            RoutePlan((task,)), {agent.agent_id: agent}, decisions)

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(supervisor.synthesis_revision_feedback, [("明确结论适用范围",)])
        self.assertIn("已补充审核要求", reviewer.seen[1][1])
        self.assertEqual(len(policy.checked), 3)
        self.assertTrue(any(item.stage == "supervisor.synthesis.revise" and item.outcome == "completed"
                            for item in result.trace))

    def test_evidence_references_are_validated_before_review(self):
        class UnsupportedAgent(FakeAgent):
            def run(self, request, task, dependencies):
                return AdviceDraft("无来源建议", claims=(Claim("无依据事实", ("missing",)),))

        agent = UnsupportedAgent("adviser", ("product_comparison",))
        task = AgentTask("compare", "adviser", "product_comparison", "compare")
        workflow, _, reviewer, _ = build_workflow(RoutePlan((task,)), {"adviser": agent})

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(reviewer.seen, [])
        self.assertEqual(result.findings[0].code, "UNSUPPORTED_CLAIM")

    def test_policy_gate_blocks_before_reviewer(self):
        agent = FakeAgent("transfer", ("transfer_draft",))
        task = AgentTask("transfer", "transfer", "transfer_draft", "prepare transfer draft")
        critical = Finding("CONFIRM_REQUIRED", Severity.CRITICAL, "必须由用户单独确认。")
        workflow, _, reviewer, _ = build_workflow(
            RoutePlan((task,)), {"transfer": agent},
            policy=FakePolicy([PolicyResult(False, (critical,))]),
        )

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(reviewer.seen, [])

    def test_critical_policy_finding_blocks_even_if_gate_reports_allowed(self):
        agent = FakeAgent("transfer", ("transfer_draft",))
        task = AgentTask("transfer", "transfer", "transfer_draft", "prepare transfer draft")
        critical = Finding("CONFIRM_REQUIRED", Severity.CRITICAL, "必须由用户单独确认。")
        workflow, _, reviewer, _ = build_workflow(
            RoutePlan((task,)), {"transfer": agent},
            policy=FakePolicy([PolicyResult(True, (critical,))]),
        )

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(reviewer.seen, [])

    def test_clarification_returns_without_dispatch(self):
        agent = FakeAgent("cards", ("card_query",))
        workflow, _, reviewer, _ = build_workflow(
            RoutePlan(clarification="你想查询哪张卡？"), {"cards": agent})

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.HANDOFF)
        self.assertEqual(result.answer, "你想查询哪张卡？")
        self.assertEqual(agent.calls, [])
        self.assertEqual(reviewer.seen, [])

    def test_critical_review_finding_blocks_even_with_approve_verdict(self):
        agent = FakeAgent("planner", ("allocation",))
        task = AgentTask("allocation", "planner", "allocation", "review allocation")
        reviewer_decision = ReviewDecision(
            ReviewVerdict.APPROVE,
            findings=(Finding("RISK_MISMATCH", Severity.CRITICAL, "风险错配。"),),
        )
        workflow, _, _, _ = build_workflow(
            RoutePlan((task,)), {"planner": agent}, [reviewer_decision])

        result = workflow.run(make_request())
        self.assertEqual(result.status, RunStatus.BLOCKED)

    def test_invalid_or_oversized_graph_is_not_dispatched(self):
        agent = FakeAgent("finance", ("one", "two", "three"))
        tasks = tuple(AgentTask(f"t{i}", "finance", cap, cap) for i, cap in enumerate(agent.capabilities))
        workflow, _, _, _ = build_workflow(
            RoutePlan(tasks), {"finance": agent}, config=MultiAgentConfig(max_tasks_per_request=2))

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.HANDOFF)
        self.assertEqual(agent.calls, [])

    def test_forbidden_tool_scope_invalidates_agent_plan(self):
        agent = FakeAgent("unsafe", ("transfer_draft",), tool_scopes=("transaction.execute",))
        task = AgentTask("t", "unsafe", "transfer_draft", "transfer")
        workflow, _, _, _ = build_workflow(RoutePlan((task,)), {"unsafe": agent})

        result = workflow.run(make_request())

        self.assertEqual(result.status, RunStatus.HANDOFF)
        self.assertEqual(agent.calls, [])

    def test_memory_is_scoped_to_subject_and_only_authorized_categories(self):
        node = MemoryNode("memory-interest", "product_interest", "用户近期关注低波动理财。",
                          MemoryOrigin.USER_DECLARED, confidence=1.0, user_confirmed=True)
        private_node = MemoryNode("memory-risk", "risk_profile", "不得作为记忆读取的风险结论。",
                                  MemoryOrigin.INFERRED)
        graph = FakeMemoryGraph(MemoryGraphContext(nodes=(node, private_node)))
        agent = FakeAgent("adviser", ("product_comparison",))
        task = AgentTask("compare", "adviser", "product_comparison", "compare")
        permissions = MemoryPermissions(read_categories=("product_interest",),
                                        persistent_storage_enabled=True)
        workflow, _, _, _ = build_workflow(
            RoutePlan((task,)), {"adviser": agent}, user_memory_graph=graph)

        result = workflow.run(make_request(subject_id="opaque-user-7", memory_permissions=permissions))

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(graph.read_args[0][0], "opaque-user-7")
        self.assertEqual(graph.read_args[0][2], ("product_interest",))
        seen_request = agent.request_contexts[0]
        self.assertEqual(tuple(item.node_id for item in seen_request.memory_context.nodes),
                         ("memory-interest",))
        self.assertIn("memory-interest", {item.evidence_id for item in seen_request.evidence})
        self.assertNotIn("memory-risk", {item.evidence_id for item in seen_request.evidence})

    def test_memory_writes_require_permissions_curator_gate_and_valid_sources(self):
        agent = FakeAgent("adviser", ("product_comparison",))
        task = AgentTask("compare", "adviser", "product_comparison", "compare")
        graph = FakeMemoryGraph(MemoryGraphContext())
        curator = FakeMemoryCurator((
            MemoryProposal("add", "product_interest", "对短债产品感兴趣。",
                           MemoryOrigin.EXPLICIT_FEEDBACK, ("e-1",)),
            MemoryProposal("add", "risk_profile", "推断用户风险承受能力较高。",
                           MemoryOrigin.INFERRED, ("e-1",)),
            MemoryProposal("add", "spending_summary", "用户经常购买某类商品。",
                           MemoryOrigin.AGGREGATED_BEHAVIOR, ("e-1",)),
            MemoryProposal("add", "product_interest", "无证据来源。",
                           MemoryOrigin.INFERRED, ("missing",)),
            MemoryProposal("add", "product_interest", "用户确认关注短债产品。",
                           MemoryOrigin.INFERRED, ("e-1",), user_confirmed=True),
        ))
        gate = FakeMemoryGate()
        permissions = MemoryPermissions(
            read_categories=("product_interest",),
            write_categories=("product_interest", "risk_profile"),
            persistent_storage_enabled=True,
        )
        workflow, _, _, _ = build_workflow(
            RoutePlan((task,)), {"adviser": agent},
            user_memory_graph=graph, memory_curator=curator, memory_write_gate=gate,
        )

        result = workflow.run(make_request(subject_id="opaque-user-7", memory_permissions=permissions))

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(len(gate.calls[0][1]), 2)
        self.assertEqual({item.category for item in gate.calls[0][1]},
                         {"product_interest"})
        self.assertEqual(graph.committed[0][0], "opaque-user-7")
        self.assertEqual(result.memory_updated_node_ids, ("mem-1", "mem-2"))

    def test_memory_is_not_read_or_written_without_opt_in(self):
        node = MemoryNode("memory-interest", "product_interest", "兴趣记忆。", MemoryOrigin.INFERRED)
        graph = FakeMemoryGraph(MemoryGraphContext(nodes=(node,)))
        agent = FakeAgent("adviser", ("product_comparison",))
        task = AgentTask("compare", "adviser", "product_comparison", "compare")
        curator = FakeMemoryCurator((MemoryProposal(
            "add", "product_interest", "兴趣", MemoryOrigin.INFERRED, ("e-1",)),))
        gate = FakeMemoryGate()
        workflow, _, _, _ = build_workflow(
            RoutePlan((task,)), {"adviser": agent}, user_memory_graph=graph,
            memory_curator=curator, memory_write_gate=gate,
        )

        result = workflow.run(make_request(subject_id="opaque-user-7"))

        self.assertEqual(result.status, RunStatus.APPROVED)
        self.assertEqual(graph.read_args, [])
        self.assertEqual(graph.committed, [])
        self.assertEqual(gate.calls, [])

    def test_review_timeout_fails_closed(self):
        class BrokenReviewer:
            model_id = "broken-reviewer"

            def review(self, request, outputs, final_draft):
                raise TimeoutError("timeout")

        agent = FakeAgent("cards", ("card_query",))
        task = AgentTask("cards", "cards", "card_query", "query cards")
        result = MultiAgentOrchestrator(
            FakeSupervisor(RoutePlan((task,))), {"cards": agent}, BrokenReviewer(), FakePolicy(),
        ).run(make_request())

        self.assertEqual(result.status, RunStatus.FALLBACK)
        self.assertIn("无法完成可靠核验", result.answer)


if __name__ == "__main__":
    unittest.main()
