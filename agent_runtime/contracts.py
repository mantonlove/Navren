"""共享数据合同。适配器只传输完成任务所需的最少客户信息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


@dataclass(frozen=True)
class EvidenceItem:
    """来自客户档案、模拟银行或产品库的可追溯证据。"""

    evidence_id: str
    source: str
    text: str
    version: str | None = None


@dataclass(frozen=True)
class Claim:
    """建议中的可核验陈述及其证据引用。"""

    text: str
    evidence_ids: tuple[str, ...] = ()


class MemoryOrigin(str, Enum):
    USER_DECLARED = "user_declared"
    EXPLICIT_FEEDBACK = "explicit_feedback"
    AGGREGATED_BEHAVIOR = "aggregated_behavior"
    INFERRED = "inferred"


@dataclass(frozen=True)
class MemoryNode:
    """可审计的用户记忆图节点；不替代正式风险测评结果。"""

    node_id: str
    category: str
    value: str
    origin: MemoryOrigin
    evidence_ids: tuple[str, ...] = ()
    confidence: float = 1.0
    observed_at: str | None = None
    expires_at: str | None = None
    user_confirmed: bool = False
    source_type: str = "user_entered"
    source_ref: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory confidence 必须在 0 到 1 之间。")


@dataclass(frozen=True)
class MemoryRelation:
    source_node_id: str
    relation: str
    target_node_id: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryGraphContext:
    nodes: tuple[MemoryNode, ...] = ()
    relations: tuple[MemoryRelation, ...] = ()


@dataclass(frozen=True)
class MemoryPermissions:
    """本轮由产品设置/用户选择得到的记忆读取与写入授权快照。"""

    read_categories: tuple[str, ...] = ()
    write_categories: tuple[str, ...] = ()
    persistent_storage_enabled: bool = False


@dataclass(frozen=True)
class MemoryProposal:
    operation: str
    category: str
    value: str
    origin: MemoryOrigin
    evidence_ids: tuple[str, ...]
    confidence: float = 1.0
    expires_at: str | None = None
    user_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.operation not in {"add", "update", "expire"}:
            raise ValueError("memory operation 必须是 add、update 或 expire。")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory confidence 必须在 0 到 1 之间。")


@dataclass(frozen=True)
class AdviceRequest:
    """顾问与审核智能体共用的最小请求上下文。"""

    request_id: str
    question: str
    customer_facts: Mapping[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    subject_id: str | None = None
    memory_context: MemoryGraphContext = field(default_factory=MemoryGraphContext)
    memory_permissions: MemoryPermissions = field(default_factory=MemoryPermissions)


@dataclass(frozen=True)
class AgentDescriptor:
    """供主管选择的 Agent 能力与授权目录，不绑定固定 Agent 数量。"""

    agent_id: str
    capabilities: tuple[str, ...]
    data_scopes: tuple[str, ...] = ()
    tool_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTask:
    task_id: str
    agent_id: str
    capability: str
    objective: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutePlan:
    tasks: tuple[AgentTask, ...] = ()
    clarification: str | None = None


@dataclass(frozen=True)
class AgentResult:
    task_id: str
    agent_id: str
    capability: str
    draft: "AdviceDraft"


@dataclass(frozen=True)
class AdviceDraft:
    """主顾问输出；事实性陈述应拆为 claims 并绑定证据。"""

    text: str
    claims: tuple[Claim, ...] = ()
    proposed_actions: tuple[str, ...] = ()


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def blocks_release(self) -> bool:
        return self in (Severity.HIGH, Severity.CRITICAL)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str
    evidence_ids: tuple[str, ...] = ()


class ReviewVerdict(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class ReviewDecision:
    verdict: ReviewVerdict
    findings: tuple[Finding, ...] = ()
    revision_instructions: tuple[str, ...] = ()
    revision_task_ids: tuple[str, ...] = ()


class RunStatus(str, Enum):
    APPROVED = "approved"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class TraceEvent:
    stage: str
    outcome: str
    model_id: str | None = None
    duration_ms: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    answer: str
    draft: AdviceDraft | None
    attempts: int
    findings: tuple[Finding, ...] = ()
    trace: tuple[TraceEvent, ...] = ()
    agent_results: tuple[AgentResult, ...] = ()
    route_plan: RoutePlan | None = None
    memory_updated_node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    allowed: bool
    findings: tuple[Finding, ...] = ()
