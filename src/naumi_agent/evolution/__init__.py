"""Evidence-first self-evolution primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.evolution.candidate import EvolutionCandidateDraft
    from naumi_agent.evolution.evidence import EvolutionEvidence
    from naumi_agent.evolution.experiments import (
        EvolutionExperimentContract,
        EvolutionExperimentContractIssuer,
        ExperimentBudget,
    )
    from naumi_agent.evolution.store import (
        EvolutionCandidateEvent,
        EvolutionCandidateStore,
        EvolutionStoreConflictError,
        EvolutionStoreCorruptionError,
        EvolutionStoredCandidate,
        EvolutionStoreError,
    )

__all__ = [
    "EvolutionEvidence",
    "EvolutionCandidateDraft",
    "EvolutionCandidateEvent",
    "EvolutionCandidateStore",
    "EvolutionProposalPreview",
    "adapt_harness_failure_evidence",
    "adapt_self_review_static_evidence",
    "build_candidate_draft",
    "classify_proposal_kind",
    "generate_proposal_preview",
    "EvolutionStoredCandidate",
    "EvolutionExperimentContract",
    "EvolutionExperimentContractIssuer",
    "ExperimentBudget",
    "EvolutionStoreConflictError",
    "EvolutionStoreCorruptionError",
    "EvolutionStoreError",
    "resolve_evolution_db_path",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    candidate_exports = {"EvolutionCandidateDraft", "build_candidate_draft"}
    evidence_exports = {
        "EvolutionEvidence",
        "adapt_harness_failure_evidence",
        "adapt_self_review_static_evidence",
    }
    proposal_exports = {
        "EvolutionProposalPreview",
        "classify_proposal_kind",
        "generate_proposal_preview",
    }
    experiment_exports = {
        "EvolutionExperimentContract",
        "EvolutionExperimentContractIssuer",
        "ExperimentBudget",
    }
    if name in candidate_exports:
        module_name = "candidate"
    elif name in evidence_exports:
        module_name = "evidence"
    elif name in proposal_exports:
        module_name = "proposal"
    elif name in experiment_exports:
        module_name = "experiments"
    else:
        module_name = "store"
    module = import_module(f"naumi_agent.evolution.{module_name}")
    return getattr(module, name)
