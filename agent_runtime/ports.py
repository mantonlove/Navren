"""模型适配端口。具体供应商 SDK/API 不进入编排核心。"""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    AdviceDraft,
    AdviceRequest,
    AgentDescriptor,
    AgentResult,
    AgentTask,
    MemoryGraphContext,
    MemoryPermissions,
    MemoryProposal,
    PolicyResult,
    ReviewDecision,
    RoutePlan,
)


class SupervisorAgent(Protocol):
    model_id: str

    def route(
        self,
        request: AdviceRequest,
        agent_catalog: tuple[AgentDescriptor, ...],
    ) -> RoutePlan:
        """将请求拆分为受限的业务任务；不得发起银行操作。"""

    def synthesize(
        self,
        request: AdviceRequest,
        outputs: tuple[AgentResult, ...],
    ) -> AdviceDraft:
        """基于子任务结果形成最终草案；不得补造证据或政策结论。"""


class DomainAgent(Protocol):
    agent_id: str
    capabilities: tuple[str, ...]
    model_id: str

    def run(
        self,
        request: AdviceRequest,
        task: AgentTask,
        dependency_outputs: tuple[AgentResult, ...],
    ) -> AdviceDraft:
        """完成本领域分析或准备草案；工具权限由适配器按领域收窄。"""

    def revise(
        self,
        request: AdviceRequest,
        task: AgentTask,
        draft: AdviceDraft,
        dependency_outputs: tuple[AgentResult, ...],
        feedback: tuple[str, ...],
    ) -> AdviceDraft:
        """仅按审核意见修订本领域草案，不执行操作。"""


class CollaborativeReviewerAgent(Protocol):
    model_id: str

    def review(
        self,
        request: AdviceRequest,
        outputs: tuple[AgentResult, ...],
        final_draft: AdviceDraft,
    ) -> ReviewDecision:
        """审核各业务 Agent 的证据、规则与边界；没有写入或交易工具。"""


class MemoryCuratorAgent(Protocol):
    model_id: str

    def propose_updates(
        self,
        request: AdviceRequest,
        outputs: tuple[AgentResult, ...],
        final_draft: AdviceDraft,
    ) -> tuple[MemoryProposal, ...]:
        """只提出记忆变更候选，不直接写入用户记忆库。"""


class UserMemoryGraphPort(Protocol):
    def read_relevant(
        self,
        subject_id: str,
        query: str,
        allowed_categories: tuple[str, ...],
    ) -> MemoryGraphContext:
        """按用户隔离读取获准用于当前任务的相关图谱片段。"""

    def commit(self, subject_id: str, proposals: tuple[MemoryProposal, ...]) -> tuple[str, ...]:
        """只提交已过同意和确定性记忆策略门批准的候选。"""

    def list_all(self, subject_id: str) -> MemoryGraphContext:
        """支持用户查看已保存记忆。"""

    def correct(self, subject_id: str, node_id: str, value: str) -> None:
        """支持用户纠正记忆内容。"""

    def delete(self, subject_id: str, node_id: str) -> None:
        """支持用户删除单条记忆。"""

    def disable(self, subject_id: str) -> None:
        """支持用户关闭记忆功能并停止后续读取与持久化。"""


class MemoryWriteGate(Protocol):
    def authorize(
        self,
        subject_id: str,
        proposals: tuple[MemoryProposal, ...],
        permissions: MemoryPermissions,
    ) -> tuple[MemoryProposal, ...]:
        """依据授权、类别与来源规则筛选可写入候选。"""


class PolicyGate(Protocol):
    def check(self, request: AdviceRequest, draft: AdviceDraft) -> PolicyResult:
        """执行确定性政策与合同校验，不依赖模型自我判断。"""
