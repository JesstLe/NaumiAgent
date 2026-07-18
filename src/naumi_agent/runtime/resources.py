"""Typed external resources owned by the runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass, fields

from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.store import WorkbenchStore

_RESOURCE_CONTRACTS: dict[str, tuple[type[object], str]] = {
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
