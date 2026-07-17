"""Evidence-first self-evolution primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.evolution.candidate import EvolutionCandidateDraft
    from naumi_agent.evolution.evidence import EvolutionEvidence

__all__ = [
    "EvolutionEvidence",
    "EvolutionCandidateDraft",
    "adapt_harness_failure_evidence",
    "adapt_self_review_static_evidence",
    "build_candidate_draft",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    candidate_exports = {"EvolutionCandidateDraft", "build_candidate_draft"}
    module_name = "candidate" if name in candidate_exports else "evidence"
    module = import_module(f"naumi_agent.evolution.{module_name}")
    return getattr(module, name)
