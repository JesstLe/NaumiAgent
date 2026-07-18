"""Typed external resources owned by the runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass, fields

from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.runs.store import ChatRunStore

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
}


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    """Current complete set of externally owned runtime resources."""

    chat_run_store: ChatRunStore
    evolution_candidate_store: EvolutionCandidateStore
    harness_store: HarnessStore
    harness_trust_store: HarnessTrustStore

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_resource(item.name, getattr(self, item.name))


@dataclass(frozen=True, slots=True)
class RuntimeResourceOverrides:
    """Optional resource instances; None alone requests a default resource."""

    chat_run_store: ChatRunStore | None = None
    evolution_candidate_store: EvolutionCandidateStore | None = None
    harness_store: HarnessStore | None = None
    harness_trust_store: HarnessTrustStore | None = None


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
