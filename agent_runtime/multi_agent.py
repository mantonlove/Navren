"""能力目录 + 动态任务图 + 确定性政策门 + 独立审核。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from time import perf_counter
from typing import Callable, Mapping

from .contracts import (
    AdviceDraft, AdviceRequest, AgentDescriptor, AgentResult, AgentTask,
    EvidenceItem, Finding, MemoryProposal, PolicyResult,
    ReviewVerdict, RoutePlan, RunResult, RunStatus, Severity, TraceEvent,
)
from .ports import (
    CollaborativeReviewerAgent,
    DomainAgent,
    MemoryCuratorAgent,
    MemoryWriteGate,
    PolicyGate,
    SupervisorAgent,
    UserMemoryGraphPort,
)


@dataclass(frozen=True)
class MultiAgentConfig:
    max_tasks_per_request: int = 8
    max_review_revisions: int = 1
    safe_fallback: str = (
        "这项处理暂时无法完成可靠核验。为避免给您造成误导，我先不提供具体建议或操作，"
        "请补充信息或联系人工顾问。"
    )

    def __post_init__(self) -> None:
        if not 1 <= self.max_tasks_per_request <= 24:
            raise ValueError("max_tasks_per_request 必须在 1 到 24 之间。")
        if not 0 <= self.max_review_revisions <= 1:
            raise ValueError("max_review_revisions 必须为 0 或 1。")


class MultiAgentOrchestrator:
    """主管按能力目录规划任务；Agent 数量和组合由请求决定。

    六项银行功能是可注册的业务能力，不等于六个固定 Agent。一个 Agent 可
    覆盖多个能力，一个请求也可由同一 Agent 执行多个任务。子任务只接收
    明确依赖的上游结果；任何 Agent 均不持有交易确认或银行写入能力。
    """

    _FORBIDDEN_TOOL_SCOPES = frozenset({
        "transaction.execute", "user.confirm", "verification.code", "customer_profile.write",
    })
    _NON_MEMORY_PROFILE_CATEGORIES = frozenset({
        "risk_level", "risk_profile", "suitability", "creditworthiness", "identity",
    })

    def __init__(
        self,
        supervisor: SupervisorAgent,
        specialists: Mapping[str, DomainAgent],
        reviewer: CollaborativeReviewerAgent,
        policy_gate: PolicyGate,
        *,
        user_memory_graph: UserMemoryGraphPort | None = None,
        memory_curator: MemoryCuratorAgent | None = None,
        memory_write_gate: MemoryWriteGate | None = None,
        config: MultiAgentConfig | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.supervisor = supervisor
        self.specialists = dict(specialists)
        self.reviewer = reviewer
        self.policy_gate = policy_gate
        self.user_memory_graph = user_memory_graph
        self.memory_curator = memory_curator
        self.memory_write_gate = memory_write_gate
        self.config = config or MultiAgentConfig()
        self.clock = clock

    def run(self, request: AdviceRequest) -> RunResult:
        trace: list[TraceEvent] = []
        findings: list[Finding] = []
        request = self._load_memory_context(request, trace)
        catalog = self._catalog()
        try:
            started = self.clock()
            plan = self.supervisor.route(request, catalog)
            trace.append(self._event("supervisor.route", "completed", self.supervisor.model_id, started))
        except Exception as error:
            trace.append(self._event("supervisor.route", "failed", self._model_id(self.supervisor), reason=self._safe_failure_reason(error)))
            return self._result(RunStatus.FALLBACK, None, [], findings, trace)

        if plan.clarification and not plan.tasks:
            trace.append(TraceEvent("supervisor.route", "needs_clarification"))
            return self._result(RunStatus.HANDOFF, None, [], findings, trace, plan,
                                answer=plan.clarification)
        if not self._valid_plan(plan):
            trace.append(TraceEvent("supervisor.route", "invalid_plan"))
            return self._result(RunStatus.HANDOFF, None, [], findings, trace, plan)

        result_by_task: dict[str, AgentResult] = {}
        evidence_ids = {item.evidence_id for item in request.evidence}
        evidence_ids.update(node.node_id for node in request.memory_context.nodes)
        for task in plan.tasks:
            dependencies = tuple(result_by_task[item] for item in task.depends_on)
            result = self._run_task(request, task, dependencies, evidence_ids, findings, trace)
            if isinstance(result, RunResult):
                return self._with_plan(result, plan)
            result_by_task[task.task_id] = result

        outputs = [result_by_task[task.task_id] for task in plan.tasks]
        return self._synthesize_review_and_finish(request, plan, outputs, findings, trace, evidence_ids)

    def _run_task(self, request, task, dependencies, evidence_ids, findings, trace):
        agent = self.specialists[task.agent_id]
        try:
            started = self.clock()
            draft = agent.run(request, task, dependencies)
            trace.append(self._event(f"agent.{task.agent_id}.{task.task_id}", "completed", agent.model_id, started))
        except Exception as error:
            trace.append(self._event(f"agent.{task.agent_id}.{task.task_id}", "failed", self._model_id(agent), reason=self._safe_failure_reason(error)))
            return self._result(RunStatus.FALLBACK, None, [], findings, trace)
        return self._validate_and_wrap(request, task, draft, evidence_ids, findings, trace)

    def _validate_and_wrap(self, request, task, draft, evidence_ids, findings, trace):
        bad_claims = self._unsupported_claims(draft, evidence_ids)
        if bad_claims:
            findings.extend(bad_claims)
            trace.append(TraceEvent(f"evidence.{task.task_id}", "blocked"))
            return self._result(RunStatus.BLOCKED, None, [], findings, trace)
        try:
            started = self.clock()
            policy: PolicyResult = self.policy_gate.check(request, draft)
            blocked = (not policy.allowed
                       or any(item.severity.blocks_release for item in policy.findings))
            trace.append(self._event(f"policy.{task.task_id}",
                                     "blocked" if blocked else "passed", None, started))
        except Exception:
            trace.append(TraceEvent(f"policy.{task.task_id}", "failed_closed"))
            return self._result(RunStatus.FALLBACK, None, [], findings, trace)
        findings.extend(policy.findings)
        if blocked:
            return self._result(RunStatus.BLOCKED, None, [], findings, trace)
        return AgentResult(task.task_id, task.agent_id, task.capability, draft)

    def _synthesize_review_and_finish(self, request, plan, outputs, findings, trace, evidence_ids):
        task_by_id = {task.task_id: task for task in plan.tasks}
        synthesis_override = None
        for review_round in range(self.config.max_review_revisions + 1):
            if synthesis_override is not None:
                final_draft, synthesis_override = synthesis_override, None
            else:
                try:
                    started = self.clock()
                    final_draft = self.supervisor.synthesize(request, tuple(outputs))
                    trace.append(self._event("supervisor.synthesize", "completed", self.supervisor.model_id, started))
                except Exception as error:
                    trace.append(self._event("supervisor.synthesize", "failed", self._model_id(self.supervisor), reason=self._safe_failure_reason(error)))
                    return self._result(RunStatus.FALLBACK, None, outputs, findings, trace, plan)

            if not final_draft.text.strip():
                findings.append(Finding("EMPTY_SYNTHESIS", Severity.CRITICAL, "主管未形成可展示的综合答复。"))
                return self._result(RunStatus.BLOCKED, final_draft, outputs, findings, trace, plan)
            bad_claims = self._unsupported_claims(final_draft, evidence_ids)
            if bad_claims:
                findings.extend(bad_claims)
                trace.append(TraceEvent("evidence.supervisor_synthesis", "blocked"))
                return self._result(RunStatus.BLOCKED, final_draft, outputs, findings, trace, plan)
            try:
                started = self.clock()
                policy = self.policy_gate.check(request, final_draft)
                blocked = (not policy.allowed
                           or any(item.severity.blocks_release for item in policy.findings))
                trace.append(self._event("policy.final", "blocked" if blocked else "passed", None, started))
            except Exception:
                trace.append(TraceEvent("policy.final", "failed_closed"))
                return self._result(RunStatus.FALLBACK, final_draft, outputs, findings, trace, plan)
            findings.extend(policy.findings)
            if blocked:
                return self._result(RunStatus.BLOCKED, final_draft, outputs, findings, trace, plan)
            try:
                started = self.clock()
                decision = self.reviewer.review(request, tuple(outputs), final_draft)
                trace.append(self._event("reviewer.review", decision.verdict.value,
                                         self.reviewer.model_id, started))
            except Exception as error:
                trace.append(self._event("reviewer.review", "failed_closed", self._model_id(self.reviewer), reason=self._safe_failure_reason(error)))
                return self._result(RunStatus.FALLBACK, final_draft, outputs, findings, trace, plan)

            findings.extend(decision.findings)
            blocking = any(item.severity.blocks_release for item in decision.findings)
            if decision.verdict is ReviewVerdict.APPROVE and not blocking:
                return self._approved(request, final_draft, plan, outputs, findings, trace, evidence_ids)
            if decision.verdict is ReviewVerdict.REJECT or blocking:
                return self._result(RunStatus.BLOCKED, final_draft, outputs, findings, trace, plan)
            if decision.verdict is ReviewVerdict.INSUFFICIENT_EVIDENCE:
                return self._result(RunStatus.HANDOFF, final_draft, outputs, findings, trace, plan)
            if decision.verdict is not ReviewVerdict.REVISE:
                return self._result(RunStatus.HANDOFF, final_draft, outputs, findings, trace, plan)
            if review_round >= self.config.max_review_revisions:
                return self._result(RunStatus.HANDOFF, final_draft, outputs, findings, trace, plan)

            targets, feedback = decision.revision_task_ids, decision.revision_instructions
            if not targets or not feedback or len(set(targets)) != len(targets):
                return self._result(RunStatus.HANDOFF, final_draft, outputs, findings, trace, plan)
            if targets == ("supervisor.synthesis",):
                revise_synthesis = getattr(self.supervisor, "revise_synthesis", None)
                if not callable(revise_synthesis):
                    return self._result(RunStatus.HANDOFF, final_draft, outputs, findings, trace, plan)
                try:
                    started = self.clock()
                    revised = revise_synthesis(request, tuple(outputs), final_draft, feedback)
                    if not isinstance(revised, AdviceDraft):
                        raise ValueError("主管修订合同无效。")
                    trace.append(self._event("supervisor.synthesis.revise", "completed",
                                             self._model_id(self.supervisor), started))
                except Exception as error:
                    trace.append(self._event("supervisor.synthesis.revise", "failed",
                                             self._model_id(self.supervisor),
                                             reason=self._safe_failure_reason(error)))
                    return self._result(RunStatus.FALLBACK, final_draft, outputs, findings, trace, plan)
                synthesis_override = revised
                continue
            if any(task_id not in task_by_id for task_id in targets):
                return self._result(RunStatus.HANDOFF, final_draft, outputs, findings, trace, plan)
            output_by_task = {item.task_id: item for item in outputs}
            for task in plan.tasks:
                if task.task_id not in targets:
                    continue
                agent = self.specialists[task.agent_id]
                dependencies = tuple(output_by_task[item] for item in task.depends_on)
                try:
                    started = self.clock()
                    revised = agent.revise(request, task, output_by_task[task.task_id].draft,
                                           dependencies, feedback)
                    trace.append(self._event(f"agent.{task.agent_id}.{task.task_id}.revise",
                                             "completed", agent.model_id, started))
                except Exception as error:
                    trace.append(self._event(f"agent.{task.agent_id}.{task.task_id}.revise",
                                             "failed", self._model_id(agent), reason=self._safe_failure_reason(error)))
                    return self._result(RunStatus.FALLBACK, final_draft, outputs, findings, trace, plan)
                checked = self._validate_and_wrap(request, task, revised, evidence_ids, findings, trace)
                if isinstance(checked, RunResult):
                    return self._with_plan(checked, plan)
                output_by_task[task.task_id] = checked
            outputs = [output_by_task[task.task_id] for task in plan.tasks]
        return self._result(RunStatus.HANDOFF, None, outputs, findings, trace, plan)

    def _catalog(self):
        return tuple(AgentDescriptor(
            agent_id=agent_id,
            capabilities=tuple(getattr(agent, "capabilities", ())),
            data_scopes=tuple(getattr(agent, "data_scopes", ())),
            tool_scopes=tuple(getattr(agent, "tool_scopes", ())),
        ) for agent_id, agent in sorted(self.specialists.items()))

    def _load_memory_context(self, request, trace):
        permissions = request.memory_permissions
        allowed = set(permissions.read_categories)
        if not allowed or not permissions.persistent_storage_enabled or not request.subject_id:
            if request.memory_context.nodes or request.memory_context.relations:
                trace.append(TraceEvent("memory.retrieve", "not_authorized"))
            return replace(request, memory_context=type(request.memory_context)())

        context = request.memory_context
        if self.user_memory_graph is not None:
            try:
                loaded = self.user_memory_graph.read_relevant(
                    request.subject_id, request.question, tuple(sorted(allowed)),
                )
                nodes_by_id = {node.node_id: node for node in context.nodes if node.category in allowed}
                nodes_by_id.update({node.node_id: node for node in loaded.nodes if node.category in allowed})
                node_ids = set(nodes_by_id)
                relations_by_key = {
                    (item.source_node_id, item.relation, item.target_node_id): item
                    for item in (*context.relations, *loaded.relations)
                    if item.source_node_id in node_ids and item.target_node_id in node_ids
                }
                context = type(context)(tuple(nodes_by_id.values()), tuple(relations_by_key.values()))
                trace.append(TraceEvent("memory.retrieve", "completed"))
            except Exception:
                # Personalization is optional: a memory-store outage must not make
                # a general answer appear to be memory-grounded.
                context = type(context)()
                trace.append(TraceEvent("memory.retrieve", "unavailable_optional"))
        else:
            nodes = tuple(node for node in context.nodes if node.category in allowed)
            node_ids = {node.node_id for node in nodes}
            context = type(context)(
                nodes,
                tuple(item for item in context.relations
                      if item.source_node_id in node_ids and item.target_node_id in node_ids),
            )

        evidence_by_id = {item.evidence_id: item for item in request.evidence}
        for node in context.nodes:
            evidence_by_id[node.node_id] = EvidenceItem(
                evidence_id=node.node_id,
                source=f"user_memory:{node.source_type}",
                text=node.value,
                version=node.observed_at,
            )
        return replace(
            request,
            memory_context=context,
            evidence=tuple(evidence_by_id.values()),
        )

    def _valid_plan(self, plan):
        if plan.clarification or not plan.tasks or len(plan.tasks) > self.config.max_tasks_per_request:
            return False
        seen: set[str] = set()
        for task in plan.tasks:
            agent = self.specialists.get(task.agent_id)
            if (not task.task_id or task.task_id in seen or not task.objective.strip()
                    or not task.capability.strip() or agent is None
                    or getattr(agent, "agent_id", None) != task.agent_id
                    or task.capability not in getattr(agent, "capabilities", ())):
                return False
            if set(getattr(agent, "tool_scopes", ())) & self._FORBIDDEN_TOOL_SCOPES:
                return False
            if len(set(task.depends_on)) != len(task.depends_on) or any(dep not in seen for dep in task.depends_on):
                return False
            seen.add(task.task_id)
        return True

    @staticmethod
    def _unsupported_claims(draft, evidence_ids):
        return [Finding(
            code="UNSUPPORTED_CLAIM",
            severity=Severity.CRITICAL,
            message=f"第 {index} 项事实陈述缺少有效证据引用。",
            evidence_ids=tuple(item for item in claim.evidence_ids if item in evidence_ids),
        ) for index, claim in enumerate(draft.claims, start=1)
            if not claim.evidence_ids or any(item not in evidence_ids for item in claim.evidence_ids)]

    def _approved(self, request, draft, plan, outputs, findings, trace, evidence_ids):
        memory_updates = self._persist_memory(request, outputs, draft, findings, trace, evidence_ids)
        return RunResult(
            status=RunStatus.APPROVED,
            answer=draft.text,
            draft=draft,
            attempts=sum(event.stage.startswith("agent.") for event in trace),
            findings=tuple(findings),
            trace=tuple(trace),
            agent_results=tuple(outputs),
            route_plan=plan,
            memory_updated_node_ids=memory_updates,
        )

    def _persist_memory(self, request, outputs, final_draft, findings, trace, evidence_ids):
        permissions = request.memory_permissions
        if (not permissions.persistent_storage_enabled or not permissions.write_categories
                or not request.subject_id):
            return ()
        if (self.user_memory_graph is None or self.memory_curator is None
                or self.memory_write_gate is None):
            trace.append(TraceEvent("memory.write", "not_configured"))
            return ()
        try:
            started = self.clock()
            proposals = self.memory_curator.propose_updates(request, tuple(outputs), final_draft)
            trace.append(self._event("memory.curate", "completed", self.memory_curator.model_id, started))
            allowed_categories = set(permissions.write_categories)
            valid_proposals = tuple(
                item for item in proposals
                if item.category in allowed_categories
                and item.category not in self._NON_MEMORY_PROFILE_CATEGORIES
                and item.evidence_ids
                and all(evidence_id in evidence_ids for evidence_id in item.evidence_ids)
                and (item.origin.value not in {"aggregated_behavior", "inferred"}
                     or item.user_confirmed)
            )
            started = self.clock()
            authorized = self.memory_write_gate.authorize(
                request.subject_id, valid_proposals, permissions,
            )
            trace.append(self._event("memory.authorize", "completed", None, started))
            authorized = tuple(item for item in authorized if item in valid_proposals)
            if not authorized:
                trace.append(TraceEvent("memory.write", "no_authorized_updates"))
                return ()
            started = self.clock()
            node_ids = self.user_memory_graph.commit(request.subject_id, authorized)
            trace.append(self._event("memory.write", "committed", None, started))
            return tuple(node_ids)
        except Exception:
            # A failed memory write never changes the already reviewed answer.
            trace.append(TraceEvent("memory.write", "failed_optional"))
            return ()

    def _result(self, status, draft, outputs, findings, trace, plan=None, *, answer=None):
        return RunResult(
            status=status,
            answer=answer if answer is not None else self.config.safe_fallback,
            draft=draft,
            attempts=sum(event.stage.startswith("agent.") for event in trace),
            findings=tuple(findings),
            trace=tuple(trace),
            agent_results=tuple(outputs),
            route_plan=plan,
        )

    @staticmethod
    def _with_plan(result, plan):
        return RunResult(
            status=result.status, answer=result.answer, draft=result.draft,
            attempts=result.attempts, findings=result.findings, trace=result.trace,
            agent_results=result.agent_results, route_plan=plan,
            memory_updated_node_ids=result.memory_updated_node_ids,
        )

    def _event(self, stage, outcome, model_id, started=None, reason=None):
        duration = None if started is None else max(0, round((self.clock() - started) * 1000))
        return TraceEvent(stage, outcome, model_id, duration, reason)

    @staticmethod
    def _safe_failure_reason(error):
        code = getattr(error, "code", None)
        allowed = {"missing_api_key", "provider_http_error", "provider_timeout",
                   "provider_unavailable", "invalid_model_contract", "response_truncated",
                   "provider_error"}
        if code in allowed:
            return code
        return "contract_invalid" if isinstance(error, (TypeError, ValueError, KeyError)) else "stage_failed"

    @staticmethod
    def _model_id(agent):
        value = getattr(agent, "model_id", None)
        return str(value) if value is not None else None
