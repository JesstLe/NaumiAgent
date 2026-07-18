"""Typed external resources owned by the runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass, fields

from naumi_agent.daemons.execution_grants import ExecutionGrantStore
from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.tool_jobs import ToolJobStore
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.store import WorkbenchStore

_RESOURCE_CONTRACTS: dict[str, tuple[type[object], str]] = {
    "worker_registry_store": (
        WorkerRegistryStore,
        "worker_registry_store 必须是 WorkerRegistryStore 实例。",
    ),
    "execution_grant_store": (
        ExecutionGrantStore,
        "execution_grant_store 必须是 ExecutionGrantStore 实例。",
    ),
    "permission_decision_store": (
        PermissionDecisionReceiptStore,
        "permission_decision_store 必须是 PermissionDecisionReceiptStore 实例。",
    ),
    "tool_job_store": (
        ToolJobStore,
        "tool_job_store 必须是 ToolJobStore 实例。",
    ),
    "chat_run_store": (
        ChatRunStore,
        "chat_run_store 必须是 ChatRunStore 实例。",
    ),
    "evolution_candidate_store": (
        EvolutionCandidateStore,
        "evolution_candidate_store 必须是 EvolutionCandidateStore 实例。",
    ),
    "harness_store": (
        HarnessStore,
        "harness_store 必须是 HarnessStore 实例。",
    ),
    "harness_trust_store": (
        HarnessTrustStore,
        "harness_trust_store 必须是 HarnessTrustStore 实例。",
    ),
    "goal_store": (
        GoalStore,
        "goal_store 必须是 GoalStore 实例。",
    ),
    "pursuit_store": (
        PursuitStore,
        "pursuit_store 必须是 PursuitStore 实例。",
    ),
    "task_store": (
        TaskStore,
        "task_store 必须是 TaskStore 实例。",
    ),
    "workbench_store": (
        WorkbenchStore,
        "workbench_store 必须是 WorkbenchStore 实例。",
    ),
}


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    """Current complete set of externally owned runtime resources."""

    chat_run_store: ChatRunStore
    worker_registry_store: WorkerRegistryStore
    execution_grant_store: ExecutionGrantStore
    permission_decision_store: PermissionDecisionReceiptStore
    tool_job_store: ToolJobStore
    evolution_candidate_store: EvolutionCandidateStore
    harness_store: HarnessStore
    harness_trust_store: HarnessTrustStore
    goal_store: GoalStore
    pursuit_store: PursuitStore
    task_store: TaskStore
    workbench_store: WorkbenchStore

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_resource(item.name, getattr(self, item.name))
        if self.task_store.db_path != self.workbench_store.db_path:
            raise ValueError(
                "task_store 与 workbench_store 必须共享同一个 SQLite 数据库。"
            )


@dataclass(frozen=True, slots=True)
class RuntimeResourceOverrides:
    """Optional resource instances; None alone requests a default resource."""

    chat_run_store: ChatRunStore | None = None
    worker_registry_store: WorkerRegistryStore | None = None
    execution_grant_store: ExecutionGrantStore | None = None
    permission_decision_store: PermissionDecisionReceiptStore | None = None
    tool_job_store: ToolJobStore | None = None
    evolution_candidate_store: EvolutionCandidateStore | None = None
    harness_store: HarnessStore | None = None
    harness_trust_store: HarnessTrustStore | None = None
    goal_store: GoalStore | None = None
    pursuit_store: PursuitStore | None = None
    task_store: TaskStore | None = None
    workbench_store: WorkbenchStore | None = None


def validate_runtime_resource_overrides(overrides: RuntimeResourceOverrides) -> None:
    """Reject invalid explicit resources before constructing any defaults."""
    for item in fields(overrides):
        value = getattr(overrides, item.name)
        if value is not None:
            _require_resource(item.name, value)


def _require_resource(name: str, value: object) -> None:
    resource_type, message = _RESOURCE_CONTRACTS[name]
    if not isinstance(value, resource_type):
        raise TypeError(message)


__all__ = [
    "RuntimeResourceOverrides",
    "RuntimeResources",
    "validate_runtime_resource_overrides",
]
