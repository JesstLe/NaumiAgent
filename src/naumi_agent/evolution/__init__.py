"""Evidence-first self-evolution primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.evolution.candidate import EvolutionCandidateDraft
    from naumi_agent.evolution.evidence import EvolutionEvidence
    from naumi_agent.evolution.experiment_leases import (
        EvolutionExperimentLeaseManager,
        EvolutionExperimentLeaseStore,
        ExperimentLeaseConflictError,
        ExperimentLeaseState,
        ExperimentWorktreeLease,
    )
    from naumi_agent.evolution.experiment_snapshots import (
        EvolutionExperimentSourceSnapshot,
        EvolutionExperimentSourceSnapshotBuilder,
        ExperimentToolIdentity,
    )
    from naumi_agent.evolution.experiments import (
        EvolutionExperimentContract,
        EvolutionExperimentContractIssuer,
        ExperimentBudget,
    )
    from naumi_agent.evolution.mutation_plans import (
        EvolutionMutationPlan,
        EvolutionMutationPlanner,
        MutationFileFact,
        MutationObjective,
        MutationPlanStage,
    )
    from naumi_agent.evolution.patch_journals import (
        EvolutionPatchJournal,
        EvolutionPatchJournalStore,
        PatchJournalState,
    )
    from naumi_agent.evolution.patch_recovery import (
        EvolutionPatchRecoveryCoordinator,
        EvolutionPatchRecoveryResult,
    )
    from naumi_agent.evolution.patch_writers import (
        EvolutionPatchWriteError,
        EvolutionPatchWriter,
        EvolutionPatchWriteReceipt,
    )
    from naumi_agent.evolution.static_guards import (
        EvolutionStaticGuard,
        EvolutionStaticGuardPolicy,
        EvolutionStaticGuardReceipt,
        StaticGuardChangeFact,
        StaticGuardViolation,
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
    "parse_proposal_scope_files",
    "EvolutionStoredCandidate",
    "EvolutionExperimentContract",
    "EvolutionExperimentContractIssuer",
    "EvolutionExperimentLeaseManager",
    "EvolutionExperimentLeaseStore",
    "EvolutionExperimentSourceSnapshot",
    "EvolutionExperimentSourceSnapshotBuilder",
    "EvolutionMutationPlan",
    "EvolutionMutationPlanner",
    "EvolutionPatchJournal",
    "EvolutionPatchJournalStore",
    "EvolutionPatchRecoveryCoordinator",
    "EvolutionPatchRecoveryResult",
    "EvolutionPatchWriteError",
    "EvolutionPatchWriteReceipt",
    "EvolutionPatchWriter",
    "EvolutionStaticGuard",
    "EvolutionStaticGuardPolicy",
    "EvolutionStaticGuardReceipt",
    "ExperimentBudget",
    "ExperimentLeaseConflictError",
    "ExperimentLeaseState",
    "ExperimentWorktreeLease",
    "ExperimentToolIdentity",
    "MutationFileFact",
    "MutationObjective",
    "MutationPlanStage",
    "PatchJournalState",
    "StaticGuardChangeFact",
    "StaticGuardViolation",
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
        "parse_proposal_scope_files",
    }
    experiment_exports = {
        "EvolutionExperimentContract",
        "EvolutionExperimentContractIssuer",
        "ExperimentBudget",
    }
    experiment_lease_exports = {
        "EvolutionExperimentLeaseManager",
        "EvolutionExperimentLeaseStore",
        "ExperimentLeaseConflictError",
        "ExperimentLeaseState",
        "ExperimentWorktreeLease",
    }
    experiment_snapshot_exports = {
        "EvolutionExperimentSourceSnapshot",
        "EvolutionExperimentSourceSnapshotBuilder",
        "ExperimentToolIdentity",
    }
    mutation_plan_exports = {
        "EvolutionMutationPlan",
        "EvolutionMutationPlanner",
        "MutationFileFact",
        "MutationObjective",
        "MutationPlanStage",
    }
    patch_journal_exports = {
        "EvolutionPatchJournal",
        "EvolutionPatchJournalStore",
        "PatchJournalState",
    }
    patch_recovery_exports = {
        "EvolutionPatchRecoveryCoordinator",
        "EvolutionPatchRecoveryResult",
    }
    patch_writer_exports = {
        "EvolutionPatchWriteError",
        "EvolutionPatchWriteReceipt",
        "EvolutionPatchWriter",
    }
    static_guard_exports = {
        "EvolutionStaticGuard",
        "EvolutionStaticGuardPolicy",
        "EvolutionStaticGuardReceipt",
        "StaticGuardChangeFact",
        "StaticGuardViolation",
    }
    if name in candidate_exports:
        module_name = "candidate"
    elif name in evidence_exports:
        module_name = "evidence"
    elif name in proposal_exports:
        module_name = "proposal"
    elif name in experiment_exports:
        module_name = "experiments"
    elif name in experiment_lease_exports:
        module_name = "experiment_leases"
    elif name in experiment_snapshot_exports:
        module_name = "experiment_snapshots"
    elif name in mutation_plan_exports:
        module_name = "mutation_plans"
    elif name in patch_journal_exports:
        module_name = "patch_journals"
    elif name in patch_recovery_exports:
        module_name = "patch_recovery"
    elif name in patch_writer_exports:
        module_name = "patch_writers"
    elif name in static_guard_exports:
        module_name = "static_guards"
    else:
        module_name = "store"
    module = import_module(f"naumi_agent.evolution.{module_name}")
    return getattr(module, name)
