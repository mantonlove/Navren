"""资金时间轴的本地确定性 Agent 适配器。"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .contracts import (
    AdviceDraft,
    AdviceRequest,
    AgentTask,
    Claim,
    EvidenceItem,
    Finding,
    MemoryGraphContext,
    MemoryNode,
    MemoryPermissions,
    PolicyResult,
    ReviewDecision,
    ReviewVerdict,
    RoutePlan,
    RunStatus,
    Severity,
)
from .multi_agent import MultiAgentOrchestrator


MODEL_ID = "navren-local-deterministic-v1"


def _money(minor: int) -> str:
    return f"¥{Decimal(minor) / 100:,.2f}"


def _account_text(account: dict) -> str:
    name = account["account_name"]
    currency = account["currency"]
    if currency != "CNY":
        return f"{name}（{currency}）：金额保留在该账户原币种，不与人民币合计。"
    return (
        f"{name}：当前可用 {_money(account['current_available_minor'])}；"
        f"已确认承诺后的最低可用 {_money(account['minimum_available_after_confirmed_minor'])}，"
        f"期末可用 {_money(account['projected_available_after_confirmed_minor'])}；"
        f"若周期扣费候选发生，最低可用 {_money(account['minimum_available_if_candidates_occur_minor'])}。"
    )


class CashflowSupervisor:
    model_id = MODEL_ID

    def route(self, request: AdviceRequest, agent_catalog) -> RoutePlan:
        if request.customer_facts.get("workflow") != "cashflow_timeline":
            return RoutePlan(clarification="当前本地工作流只处理资金时间轴查询。")
        if "cashflow_projection" not in {capability for agent in agent_catalog for capability in agent.capabilities}:
            return RoutePlan(clarification="资金时间轴分析能力当前不可用。")
        return RoutePlan(tasks=(
            AgentTask("account-snapshot", "navren-cashflow", "account_snapshot",
                      "整理各账户当前余额、币种与可用资金"),
            AgentTask("cashflow-projection", "navren-cashflow", "cashflow_projection",
                      "依据账户快照核验未来承诺、候选扣费与期末可用资金",
                      depends_on=("account-snapshot",)),
        ))

    def synthesize(self, request: AdviceRequest, outputs) -> AdviceDraft:
        facts = request.customer_facts
        cashflow = facts["cashflow"]
        account_lines = "\n".join(item.draft.text for item in outputs)
        text = (
            f"未来 {cashflow['horizon_days']} 天资金时间轴（{cashflow['as_of']} 至 "
            f"{cashflow['horizon_end']}，模拟账户，按日终汇总）：\n{account_lines}\n"
            f"{cashflow['notice']}"
        )
        window_claim = Claim(
            text=(f"资金时间轴覆盖 {cashflow['as_of']} 至 {cashflow['horizon_end']}，共 "
                  f"{cashflow['horizon_days']} 天。{cashflow['notice']}"),
            evidence_ids=("cashflow:window",),
        )
        claims = (window_claim, *(claim for item in outputs for claim in item.draft.claims))
        if cashflow["reserve_unallocated_minor"]:
            reserve_claim = Claim(
                text=(f"应急保留金额 {_money(cashflow['reserve_unallocated_minor'])} 未能对应到唯一人民币主账户，"
                      "因此没有分配到单个账户。"),
                evidence_ids=("reserve:unallocated",),
            )
            text += "\n" + reserve_claim.text
            claims += (reserve_claim,)
        memories = request.memory_context.nodes
        if memories:
            memory_claim = Claim(
                text="本次参考了用户自行保存的财务记忆作为背景；这些内容不改变账户余额和现金流计算。",
                evidence_ids=tuple(node.node_id for node in memories),
            )
            text += "\n" + memory_claim.text
            claims += (memory_claim,)
        return AdviceDraft(text=text, claims=claims)


class CashflowDomainAgent:
    agent_id = "navren-cashflow"
    capabilities = ("account_snapshot", "cashflow_projection")
    data_scopes = ("simulated_bank.account_summary", "simulated_bank.cashflow_timeline")
    tool_scopes = ("simulated_bank.account_summary.read", "simulated_bank.cashflow_timeline.read")
    model_id = MODEL_ID

    def run(self, request: AdviceRequest, task: AgentTask, dependency_outputs) -> AdviceDraft:
        accounts = request.customer_facts["cashflow"]["accounts"]
        if task.capability == "account_snapshot":
            claims = tuple(
                Claim(
                    text=(f"{account['account_name']}当前账面余额 {_money(account['opening_balance_minor'])}。"
                          if account["currency"] == "CNY" else
                          f"{account['account_name']}以{account['currency']}记账；原始金额单位保留在账户快照中。"),
                    evidence_ids=(f"account:{account['account_id']}",),
                )
                for account in accounts
            )
            return AdviceDraft("\n".join(claim.text for claim in claims), claims=claims)

        if task.capability != "cashflow_projection" or len(dependency_outputs) != 1:
            raise ValueError("资金推演任务缺少有效的账户快照依赖。")
        claims = tuple(
            Claim(
                text=_account_text(account),
                evidence_ids=(f"account:{account['account_id']}",
                              f"projection:{account['account_id']}",
                              *account.get("event_evidence_ids", ())),
            )
            for account in accounts
        )
        return AdviceDraft("\n".join(claim.text for claim in claims), claims=claims)

    def revise(self, request, task, draft, dependency_outputs, feedback):
        # 确定性适配器不改写审核意见；不确定时交由运行时安全回退。
        raise RuntimeError("本地资金推演不支持自由文本修订。")


class CashflowPolicyGate:
    def check(self, request: AdviceRequest, draft: AdviceDraft) -> PolicyResult:
        facts = request.customer_facts.get("cashflow", {})
        horizon = facts.get("horizon_days")
        accounts = facts.get("accounts", ())
        findings = []
        if type(horizon) is not int or not 1 <= horizon <= 365:
            findings.append(Finding("INVALID_CASHFLOW_HORIZON", Severity.CRITICAL,
                                    "资金时间轴范围必须为 1 至 365 天。"))
        else:
            try:
                as_of = date.fromisoformat(facts["as_of"])
                horizon_end = date.fromisoformat(facts["horizon_end"])
                valid_window = (as_of.isoformat() == facts["as_of"]
                                and horizon_end.isoformat() == facts["horizon_end"]
                                and horizon_end == as_of + timedelta(days=horizon))
            except (KeyError, TypeError, ValueError):
                valid_window = False
            if not valid_window:
                findings.append(Finding("INVALID_CASHFLOW_WINDOW", Severity.CRITICAL,
                                        "资金时间轴起止日期与范围不一致。"))
        if not accounts:
            findings.append(Finding("MISSING_CASHFLOW_ACCOUNTS", Severity.CRITICAL,
                                    "缺少可核验的模拟账户。"))
        if draft.proposed_actions:
            findings.append(Finding("CASHFLOW_ACTION_FORBIDDEN", Severity.CRITICAL,
                                    "只读资金时间轴不得产生银行操作。"))
        currencies = {item.get("currency") for item in accounts}
        if len(currencies) > 1 and any(token in draft.text for token in ("现金合计", "资金合计", "总可用")):
            findings.append(Finding("CROSS_CURRENCY_TOTAL_FORBIDDEN", Severity.CRITICAL,
                                    "不同币种账户不得合并计算。"))
        allowed_minor = set()
        for item in request.evidence:
            for match in re.finditer(r'"[A-Za-z0-9_]*_minor"\s*:\s*(-?\d+)', item.text):
                allowed_minor.add(int(match.group(1)))
        cited_amounts = set()
        for text in (draft.text, *(claim.text for claim in draft.claims)):
            for match in re.finditer(
                    r'(?:[¥￥]\s*(-?\d[\d,]*(?:\.\d{1,2})?)|(-?\d[\d,]*(?:\.\d{1,2})?)\s*元)',
                    text):
                raw = (match.group(1) or match.group(2)).replace(',', '')
                try:
                    minor = Decimal(raw) * 100
                except Exception:
                    findings.append(Finding("INVALID_CASHFLOW_AMOUNT", Severity.CRITICAL,
                                            "答复中的金额格式无法核验。"))
                    continue
                if minor != minor.to_integral_value():
                    findings.append(Finding("INVALID_CASHFLOW_AMOUNT", Severity.CRITICAL,
                                            "答复中的金额精度无法核验。"))
                    continue
                cited_amounts.add(int(minor))
        unsupported_amounts = cited_amounts - allowed_minor
        if unsupported_amounts:
            findings.append(Finding("UNSUPPORTED_CASHFLOW_AMOUNT", Severity.CRITICAL,
                                    "答复出现了未能从模拟银行证据重新计算的金额。"))
        return PolicyResult(not findings, tuple(findings))


class CashflowReviewer:
    model_id = MODEL_ID

    def review(self, request: AdviceRequest, outputs, final_draft: AdviceDraft) -> ReviewDecision:
        tasks = request.customer_facts.get("expected_task_graph", ())
        actual = tuple((item.task_id, item.capability) for item in outputs)
        if actual != tasks:
            return ReviewDecision(ReviewVerdict.REJECT, (Finding(
                "CASHFLOW_TASK_GRAPH_MISMATCH", Severity.CRITICAL,
                "实际执行的任务图与只读资金时间轴要求不符。"),))
        if final_draft.proposed_actions:
            return ReviewDecision(ReviewVerdict.REJECT, (Finding(
                "CASHFLOW_ACTION_FORBIDDEN", Severity.CRITICAL,
                "资金时间轴答复包含了不允许的操作草案。"),))
        if not final_draft.claims or any(not claim.evidence_ids for claim in final_draft.claims):
            return ReviewDecision(ReviewVerdict.INSUFFICIENT_EVIDENCE, (Finding(
                "CASHFLOW_EVIDENCE_INCOMPLETE", Severity.HIGH,
                "资金时间轴结论缺少来源引用。"),))
        return ReviewDecision(ReviewVerdict.APPROVE)


def run_cashflow_workflow(
    question: str,
    state: dict,
    timeline: dict,
    memory_nodes: tuple[MemoryNode, ...] = (),
    *,
    supervisor=None,
    reviewer=None,
) -> dict:
    """把已读取的最小账户快照与推演结果交给只读任务图并返回可审计记录。"""
    if timeline.get("environment") != "simulation" or timeline.get("source") != "simulated_bank_archive":
        raise ValueError("资金时间轴来源不符合模拟账本合同。")
    if timeline.get("workspace_id") != state.get("workspace_id"):
        raise ValueError("账户快照与资金时间轴工作区不一致。")
    state_accounts = {item["id"]: item for item in state["accounts"]}
    timeline_accounts = timeline["accounts"]
    timeline_account_ids = [item.get("account_id") for item in timeline_accounts]
    if (not timeline_accounts or len(set(timeline_account_ids)) != len(timeline_account_ids)
            or any(account_id not in state_accounts for account_id in timeline_account_ids)):
        raise ValueError("资金时间轴包含未知或缺失的账户。")

    evidence = []
    accounts = []
    evidence_sources = {}
    window_evidence = EvidenceItem(
        "cashflow:window",
        "simulated_bank.cashflow_timeline:metadata",
        json.dumps({key: timeline[key] for key in (
            "as_of", "horizon_end", "horizon_days", "notice", "formula",
        ) if key in timeline}, ensure_ascii=False, sort_keys=True),
        str(timeline.get("version", "")),
    )
    evidence.append(window_evidence)
    evidence_sources[window_evidence.evidence_id] = window_evidence.source
    for projection in timeline_accounts:
        account = state_accounts[projection["account_id"]]
        account_id = projection["account_id"]
        if projection.get("currency") != account.get("currency"):
            raise ValueError("账户快照与资金时间轴币种不一致。")
        snapshot_evidence_id = f"account:{account_id}"
        projection_evidence_id = f"projection:{account_id}"
        snapshot_text = json.dumps({
            "account_id": account_id,
            "account_name": account["name"],
            "currency": account["currency"],
            "balance_minor": account["balance_minor"],
        }, ensure_ascii=False, sort_keys=True)
        projection_text = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        evidence.extend((
            EvidenceItem(snapshot_evidence_id,
                         f"simulated_bank.customer_archive:accounts/{account_id}",
                         snapshot_text, str(state.get("version", ""))),
            EvidenceItem(projection_evidence_id,
                         f"simulated_bank.cashflow_timeline:accounts/{account_id}",
                         projection_text, str(timeline.get("version", ""))),
        ))
        evidence_sources[snapshot_evidence_id] = evidence[-2].source
        evidence_sources[projection_evidence_id] = evidence[-1].source
        accounts.append({**projection, "opening_balance_minor": account["balance_minor"]})

    event_ids_by_account = defaultdict(list)
    for day in timeline["timeline"]:
        for event in day["events"]:
            event_id = f"event:{event['id']}"
            evidence.append(EvidenceItem(
                event_id, event["evidence_ref"],
                json.dumps({key: event[key] for key in (
                    "date", "scheduled_date", "account_id", "currency", "amount_minor",
                    "cash_delta_minor", "reserved_delta_minor", "kind", "certainty", "title",
                    "date_status",
                )}, ensure_ascii=False, sort_keys=True),
                str(timeline.get("version", "")),
            ))
            evidence_sources[event_id] = event["evidence_ref"]
            event_ids_by_account[event["account_id"]].append(event_id)

    for account in accounts:
        account["event_evidence_ids"] = tuple(event_ids_by_account[account["account_id"]])

    reserve_unallocated_minor = timeline.get("reserve_unallocated_minor", 0)
    if type(reserve_unallocated_minor) is not int or reserve_unallocated_minor < 0:
        raise ValueError("未分配的应急保留金额无效。")
    if reserve_unallocated_minor:
        reserve_evidence = EvidenceItem(
            "reserve:unallocated",
            "simulated_bank.customer_archive:profiles/reserve_minor",
            json.dumps({"reserve_unallocated_minor": reserve_unallocated_minor}, sort_keys=True),
            str(timeline.get("version", "")),
        )
        evidence.append(reserve_evidence)
        evidence_sources[reserve_evidence.evidence_id] = reserve_evidence.source

    expected_tasks = (("account-snapshot", "account_snapshot"),
                      ("cashflow-projection", "cashflow_projection"))
    request = AdviceRequest(
        request_id=f"cashflow-{uuid.uuid4().hex}",
        question=question,
        customer_facts={
            "workflow": "cashflow_timeline",
            "expected_task_graph": expected_tasks,
            "cashflow": {
                "as_of": timeline["as_of"],
                "horizon_end": timeline["horizon_end"],
                "horizon_days": timeline["horizon_days"],
                "accounts": accounts,
                "notice": timeline["notice"],
                "reserve_unallocated_minor": reserve_unallocated_minor,
            },
        },
        constraints=(
            "只读分析；禁止提出或执行银行操作。",
            "按账户与币种分别展示，不合并不同币种。",
            "周期续费和代扣仅作候选情景，不得写成确定扣款。",
            "不预支未来收入，不推测未记录生活支出。",
        ),
        evidence=tuple(evidence),
        subject_id=timeline["workspace_id"],
        memory_context=MemoryGraphContext(nodes=tuple(memory_nodes)),
        memory_permissions=MemoryPermissions(
            read_categories=tuple(sorted({node.category for node in memory_nodes})),
            persistent_storage_enabled=bool(memory_nodes),
        ),
    )
    agent = CashflowDomainAgent()
    orchestrator = MultiAgentOrchestrator(
        supervisor or CashflowSupervisor(), {agent.agent_id: agent},
        reviewer or CashflowReviewer(), CashflowPolicyGate(),
    )
    result = orchestrator.run(request)
    evidence_output = list(evidence)
    evidence_sources_output = dict(evidence_sources)
    for node in memory_nodes:
        source = f"user_memory:{node.source_type}"
        evidence_output.append(EvidenceItem(node.node_id, source, node.value, node.observed_at))
        evidence_sources_output[node.node_id] = source
    task_graph = [
        {"task_id": task.task_id, "agent_id": task.agent_id, "capability": task.capability,
         "depends_on": list(task.depends_on)}
        for task in (result.route_plan.tasks if result.route_plan else ())
    ]
    return {
        "status": result.status.value,
        "answer": result.answer,
        "task_graph": task_graph,
        "trace": [
            {"stage": item.stage, "outcome": item.outcome, "model_id": item.model_id,
             "duration_ms": item.duration_ms, "reason": item.reason}
            for item in result.trace
        ],
        "findings": [
            {"code": item.code, "severity": item.severity.value, "message": item.message,
             "evidence_ids": list(item.evidence_ids)}
            for item in result.findings
        ],
        "evidence": [{"evidence_id": item.evidence_id, "source": item.source}
                     for item in evidence_output],
        "claims": ([{"text": item.text, "evidence_ids": list(item.evidence_ids)}
                   for item in result.draft.claims] if result.draft else []),
        "evidence_sources": evidence_sources_output,
        "attempts": result.attempts,
        "fallback_reason": next((item.reason for item in reversed(result.trace)
                                  if item.reason), None),
        "bank_actions": [],
        "memory_context": ([{"node_id": node.node_id, "category": node.category,
                             "value": node.value, "origin": node.origin.value,
                             "source_type": node.source_type, "source_ref": node.source_ref,
                             "observed_at": node.observed_at, "expires_at": node.expires_at,
                             "user_confirmed": node.user_confirmed}
                            for node in memory_nodes]
                           if result.status == RunStatus.APPROVED and memory_nodes else []),
    }


def live_cashflow_agents():
    """生产环境的主模型与独立审核模型。"""
    from .model_adapters import AIPingCashflowReviewer, AIPingCashflowSupervisor
    return AIPingCashflowSupervisor(), DeterministicThenModelReviewer(AIPingCashflowReviewer())


def deterministic_cashflow_agents():
    """测试专用的确定性组合；线上服务器不默认使用此组合。"""
    return CashflowSupervisor(), CashflowReviewer()


class DeterministicThenModelReviewer:
    """先执行代码级结构审核，再调用独立模型核验事实支持与条件遗漏。"""
    model_id = "GLM-5.3-Flash"

    def __init__(self, model_reviewer):
        self.model_reviewer = model_reviewer

    def review(self, request, outputs, final_draft):
        structural = CashflowReviewer().review(request, outputs, final_draft)
        if structural.verdict is not ReviewVerdict.APPROVE:
            return structural
        return self.model_reviewer.review(request, outputs, final_draft)
