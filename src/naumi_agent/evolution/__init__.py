"""Evidence-first self-evolution primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.evolution.candidate import EvolutionCandidateDraft
    from naumi_agent.evolution.candidate_snapshots import (
        EvolutionCandidateSnapshotError,
        EvolutionCandidateSourceBlob,
        EvolutionCandidateWorktreeSnapshot,
        capture_candidate_worktree_snapshot,
        revalidate_candidate_worktree_snapshot,
    )
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
    from naumi_agent.evolution.failure_attribution import (
        EvolutionFailureAttributionBuilder,
        EvolutionFailureAttributionError,
        EvolutionFailureAttributionExecutor,
        EvolutionFailureAttributionReceipt,
        EvolutionFailureAttributionStore,
        FailureAttributionAction,
        FailureAttributionCategory,
    )
    from naumi_agent.evolution.interventional_green_request import (
        EvolutionInterventionalGreenCohortRequest,
        EvolutionInterventionalGreenCohortRequestBuilder,
        EvolutionInterventionalGreenRequestError,
    )
    from naumi_agent.evolution.interventional_red_cohort import (
        EvolutionInterventionalRedCohortError,
        EvolutionInterventionalRedCohortExecutor,
        EvolutionInterventionalRedCohortReceipt,
        InterventionalRedCheckSummary,
        InterventionalRedMetricSummary,
    )
    from naumi_agent.evolution.interventional_red_sample import (
        EvolutionInterventionalRedCheckSampleError,
        EvolutionInterventionalRedCheckSampleExecutor,
        EvolutionInterventionalRedCheckSampleReceipt,
        EvolutionInterventionalRedRunAuthority,
        EvolutionInterventionalRedSampleError,
        EvolutionInterventionalRedSampleExecutor,
        EvolutionInterventionalRedSampleReceipt,
        validate_interventional_red_authority,
    )
    from naumi_agent.evolution.interventional_sample_kernel import (
        EvolutionInterventionalRunAuthority,
        EvolutionInterventionalSampleKernel,
        EvolutionInterventionalSampleKernelError,
        EvolutionInterventionalSampleSource,
    )
    from naumi_agent.evolution.mutation_generation import (
        EvolutionMutationGenerationError,
        EvolutionMutationGenerationResult,
        EvolutionMutationGenerationService,
        EvolutionMutationGenerationSession,
        EvolutionMutationGenerationTrace,
        EvolutionMutationGenerationTraceStore,
        MutationGenerationCallFact,
        MutationGenerationFileFact,
    )
    from naumi_agent.evolution.mutation_plans import (
        EvolutionMutationPlan,
        EvolutionMutationPlanner,
        MutationFileFact,
        MutationObjective,
        MutationPlanStage,
    )
    from naumi_agent.evolution.mutation_receipts import (
        EvolutionMutationReceipt,
        EvolutionMutationReceiptConflictError,
        EvolutionMutationReceiptError,
        EvolutionMutationReceiptService,
        EvolutionMutationReceiptStore,
        MutationReceiptFile,
        MutationToolEvidence,
    )
    from naumi_agent.evolution.mutation_turns import (
        EvolutionMutationTurnError,
        EvolutionMutationTurnResult,
        EvolutionMutationTurnRunner,
        MutationTurnBudget,
        MutationTurnEventPublisher,
    )
    from naumi_agent.evolution.patch_journals import (
        EvolutionPatchJournal,
        EvolutionPatchJournalStore,
        PatchJournalState,
    )
    from naumi_agent.evolution.patch_recovery import (
        EvolutionPatchRecoveryCoordinator,
        EvolutionPatchRecoveryResult,
        EvolutionPatchSetRecoveryCoordinator,
        EvolutionPatchSetRecoveryResult,
    )
    from naumi_agent.evolution.patch_set_writers import (
        EvolutionPatchSetWriter,
        EvolutionPatchSetWriteReceipt,
    )
    from naumi_agent.evolution.patch_sets import (
        EvolutionPatchSetFileFact,
        EvolutionPatchSetStore,
        EvolutionPatchSetTransaction,
        PatchSetFilePhase,
        PatchSetScanFailure,
        PatchSetState,
    )
    from naumi_agent.evolution.patch_writers import (
        EvolutionPatchWriteError,
        EvolutionPatchWriter,
        EvolutionPatchWriteReceipt,
    )
    from naumi_agent.evolution.postflight_guards import (
        EvolutionPostflightGuard,
        EvolutionPostflightGuardError,
        EvolutionPostflightGuardReceipt,
        PostflightDiffFact,
    )
    from naumi_agent.evolution.self_review_comparison import (
        EvolutionSelfReviewComparisonError,
        EvolutionSelfReviewComparisonExecutor,
    )
    from naumi_agent.evolution.self_review_green_cohort import (
        EvolutionSelfReviewGreenCohortError,
        EvolutionSelfReviewGreenCohortExecutor,
        EvolutionSelfReviewGreenCohortReceipt,
        EvolutionSelfReviewGreenCohortRequest,
        EvolutionSelfReviewGreenCohortRequestBuilder,
        SelfReviewGreenMetricSummary,
    )
    from naumi_agent.evolution.self_review_red_baseline import (
        EvolutionSelfReviewRedBaselineError,
        EvolutionSelfReviewRedBaselineExecutor,
        EvolutionSelfReviewRedCohortReceipt,
        SelfReviewRedMetricSummary,
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
    from naumi_agent.evolution.validation_cohorts import (
        BaselineCohortCheckCase,
        BaselineCohortMetricCase,
        EvolutionBaselineCohortRequest,
        EvolutionBaselineCohortRequestBuilder,
        EvolutionCohortRequestError,
    )
    from naumi_agent.evolution.validation_metric_bindings import (
        EvolutionMetricBindingError,
        EvolutionMetricRunnerBinding,
        EvolutionMetricRunnerBindingBuilder,
        EvolutionMetricRunnerRegistry,
        MetricRunnerBindingEntry,
        MetricRunnerResolution,
    )
    from naumi_agent.evolution.validation_plans import (
        EvolutionValidationBindingError,
        EvolutionValidationPlan,
        EvolutionValidationPlanner,
        EvolutionValidationProfileBinder,
        EvolutionValidationProfileBinding,
        ValidationCheckCoverage,
        ValidationFileRequirement,
        ValidationMetricPair,
        ValidationProfileCheckBinding,
        validation_requirements_for_path,
    )

__all__ = [
    "EvolutionEvidence",
    "EvolutionFailureAttributionBuilder",
    "EvolutionFailureAttributionError",
    "EvolutionFailureAttributionExecutor",
    "EvolutionFailureAttributionReceipt",
    "EvolutionFailureAttributionStore",
    "FailureAttributionAction",
    "FailureAttributionCategory",
    "EvolutionCandidateDraft",
    "EvolutionCandidateSnapshotError",
    "EvolutionCandidateSourceBlob",
    "EvolutionCandidateWorktreeSnapshot",
    "capture_candidate_worktree_snapshot",
    "revalidate_candidate_worktree_snapshot",
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
    "EvolutionMutationGenerationError",
    "EvolutionMutationGenerationResult",
    "EvolutionMutationGenerationService",
    "EvolutionMutationGenerationSession",
    "EvolutionMutationGenerationTrace",
    "EvolutionMutationGenerationTraceStore",
    "EvolutionMutationReceipt",
    "EvolutionMutationReceiptConflictError",
    "EvolutionMutationReceiptError",
    "EvolutionMutationReceiptService",
    "EvolutionMutationReceiptStore",
    "EvolutionMutationTurnError",
    "EvolutionMutationTurnResult",
    "EvolutionMutationTurnRunner",
    "EvolutionPatchJournal",
    "EvolutionPatchJournalStore",
    "EvolutionPostflightGuard",
    "EvolutionPostflightGuardError",
    "EvolutionPostflightGuardReceipt",
    "EvolutionPatchSetFileFact",
    "EvolutionPatchSetStore",
    "EvolutionPatchSetTransaction",
    "EvolutionPatchSetWriteReceipt",
    "EvolutionPatchSetWriter",
    "EvolutionPatchRecoveryCoordinator",
    "EvolutionPatchRecoveryResult",
    "EvolutionPatchSetRecoveryCoordinator",
    "EvolutionPatchSetRecoveryResult",
    "EvolutionPatchWriteError",
    "EvolutionPatchWriteReceipt",
    "EvolutionPatchWriter",
    "EvolutionStaticGuard",
    "EvolutionStaticGuardPolicy",
    "EvolutionStaticGuardReceipt",
    "EvolutionValidationPlan",
    "EvolutionValidationPlanner",
    "EvolutionValidationBindingError",
    "EvolutionValidationProfileBinder",
    "EvolutionValidationProfileBinding",
    "EvolutionBaselineCohortRequest",
    "EvolutionBaselineCohortRequestBuilder",
    "EvolutionCohortRequestError",
    "EvolutionMetricBindingError",
    "EvolutionMetricRunnerBinding",
    "EvolutionMetricRunnerBindingBuilder",
    "EvolutionMetricRunnerRegistry",
    "ExperimentBudget",
    "ExperimentLeaseConflictError",
    "ExperimentLeaseState",
    "ExperimentWorktreeLease",
    "ExperimentToolIdentity",
    "MutationFileFact",
    "MutationGenerationCallFact",
    "MutationGenerationFileFact",
    "MutationObjective",
    "MutationPlanStage",
    "MutationReceiptFile",
    "MutationToolEvidence",
    "MutationTurnBudget",
    "MutationTurnEventPublisher",
    "PatchJournalState",
    "PostflightDiffFact",
    "PatchSetFilePhase",
    "PatchSetScanFailure",
    "PatchSetState",
    "StaticGuardChangeFact",
    "StaticGuardViolation",
    "ValidationFileRequirement",
    "ValidationMetricPair",
    "ValidationCheckCoverage",
    "ValidationProfileCheckBinding",
    "BaselineCohortCheckCase",
    "BaselineCohortMetricCase",
    "MetricRunnerBindingEntry",
    "MetricRunnerResolution",
    "EvolutionSelfReviewRedBaselineError",
    "EvolutionSelfReviewRedBaselineExecutor",
    "EvolutionSelfReviewRedCohortReceipt",
    "EvolutionInterventionalRedCheckSampleError",
    "EvolutionInterventionalRedCheckSampleExecutor",
    "EvolutionInterventionalRedCheckSampleReceipt",
    "EvolutionInterventionalRedRunAuthority",
    "EvolutionInterventionalRedSampleError",
    "EvolutionInterventionalRedSampleExecutor",
    "EvolutionInterventionalRedSampleReceipt",
    "validate_interventional_red_authority",
    "EvolutionInterventionalRedCohortError",
    "EvolutionInterventionalRedCohortExecutor",
    "EvolutionInterventionalRedCohortReceipt",
    "InterventionalRedCheckSummary",
    "InterventionalRedMetricSummary",
    "EvolutionInterventionalGreenCohortRequest",
    "EvolutionInterventionalGreenCohortRequestBuilder",
    "EvolutionInterventionalGreenRequestError",
    "EvolutionInterventionalRunAuthority",
    "EvolutionInterventionalSampleKernel",
    "EvolutionInterventionalSampleKernelError",
    "EvolutionInterventionalSampleSource",
    "SelfReviewRedMetricSummary",
    "EvolutionSelfReviewGreenCohortError",
    "EvolutionSelfReviewGreenCohortExecutor",
    "EvolutionSelfReviewGreenCohortReceipt",
    "EvolutionSelfReviewGreenCohortRequest",
    "EvolutionSelfReviewGreenCohortRequestBuilder",
    "SelfReviewGreenMetricSummary",
    "EvolutionSelfReviewComparisonError",
    "EvolutionSelfReviewComparisonExecutor",
    "validation_requirements_for_path",
    "EvolutionStoreConflictError",
    "EvolutionStoreCorruptionError",
    "EvolutionStoreError",
    "resolve_evolution_db_path",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    candidate_exports = {"EvolutionCandidateDraft", "build_candidate_draft"}
    candidate_snapshot_exports = {
        "EvolutionCandidateSnapshotError",
        "EvolutionCandidateSourceBlob",
        "EvolutionCandidateWorktreeSnapshot",
        "capture_candidate_worktree_snapshot",
        "revalidate_candidate_worktree_snapshot",
    }
    evidence_exports = {
        "EvolutionEvidence",
        "adapt_harness_failure_evidence",
        "adapt_self_review_static_evidence",
    }
    failure_attribution_exports = {
        "EvolutionFailureAttributionBuilder",
        "EvolutionFailureAttributionError",
        "EvolutionFailureAttributionExecutor",
        "EvolutionFailureAttributionReceipt",
        "EvolutionFailureAttributionStore",
        "FailureAttributionAction",
        "FailureAttributionCategory",
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
    mutation_generation_exports = {
        "EvolutionMutationGenerationError",
        "EvolutionMutationGenerationResult",
        "EvolutionMutationGenerationService",
        "EvolutionMutationGenerationSession",
        "EvolutionMutationGenerationTrace",
        "EvolutionMutationGenerationTraceStore",
        "MutationGenerationCallFact",
        "MutationGenerationFileFact",
    }
    mutation_receipt_exports = {
        "EvolutionMutationReceipt",
        "EvolutionMutationReceiptConflictError",
        "EvolutionMutationReceiptError",
        "EvolutionMutationReceiptService",
        "EvolutionMutationReceiptStore",
        "MutationReceiptFile",
        "MutationToolEvidence",
    }
    mutation_turn_exports = {
        "EvolutionMutationTurnError",
        "EvolutionMutationTurnResult",
        "EvolutionMutationTurnRunner",
        "MutationTurnBudget",
        "MutationTurnEventPublisher",
    }
    patch_journal_exports = {
        "EvolutionPatchJournal",
        "EvolutionPatchJournalStore",
        "PatchJournalState",
    }
    postflight_guard_exports = {
        "EvolutionPostflightGuard",
        "EvolutionPostflightGuardError",
        "EvolutionPostflightGuardReceipt",
        "PostflightDiffFact",
    }
    patch_set_exports = {
        "EvolutionPatchSetFileFact",
        "EvolutionPatchSetStore",
        "EvolutionPatchSetTransaction",
        "PatchSetFilePhase",
        "PatchSetScanFailure",
        "PatchSetState",
    }
    patch_set_writer_exports = {
        "EvolutionPatchSetWriteReceipt",
        "EvolutionPatchSetWriter",
    }
    patch_recovery_exports = {
        "EvolutionPatchRecoveryCoordinator",
        "EvolutionPatchRecoveryResult",
        "EvolutionPatchSetRecoveryCoordinator",
        "EvolutionPatchSetRecoveryResult",
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
    validation_plan_exports = {
        "EvolutionValidationBindingError",
        "EvolutionValidationPlan",
        "EvolutionValidationPlanner",
        "EvolutionValidationProfileBinder",
        "EvolutionValidationProfileBinding",
        "ValidationCheckCoverage",
        "ValidationFileRequirement",
        "ValidationMetricPair",
        "ValidationProfileCheckBinding",
        "validation_requirements_for_path",
    }
    validation_cohort_exports = {
        "BaselineCohortCheckCase",
        "BaselineCohortMetricCase",
        "EvolutionBaselineCohortRequest",
        "EvolutionBaselineCohortRequestBuilder",
        "EvolutionCohortRequestError",
    }
    validation_metric_binding_exports = {
        "EvolutionMetricBindingError",
        "EvolutionMetricRunnerBinding",
        "EvolutionMetricRunnerBindingBuilder",
        "EvolutionMetricRunnerRegistry",
        "MetricRunnerBindingEntry",
        "MetricRunnerResolution",
    }
    self_review_red_baseline_exports = {
        "EvolutionSelfReviewRedBaselineError",
        "EvolutionSelfReviewRedBaselineExecutor",
        "EvolutionSelfReviewRedCohortReceipt",
        "SelfReviewRedMetricSummary",
    }
    interventional_red_sample_exports = {
        "EvolutionInterventionalRedCheckSampleError",
        "EvolutionInterventionalRedCheckSampleExecutor",
        "EvolutionInterventionalRedCheckSampleReceipt",
        "EvolutionInterventionalRedRunAuthority",
        "EvolutionInterventionalRedSampleError",
        "EvolutionInterventionalRedSampleExecutor",
        "EvolutionInterventionalRedSampleReceipt",
        "validate_interventional_red_authority",
    }
    interventional_red_cohort_exports = {
        "EvolutionInterventionalRedCohortError",
        "EvolutionInterventionalRedCohortExecutor",
        "EvolutionInterventionalRedCohortReceipt",
        "InterventionalRedCheckSummary",
        "InterventionalRedMetricSummary",
    }
    interventional_green_request_exports = {
        "EvolutionInterventionalGreenCohortRequest",
        "EvolutionInterventionalGreenCohortRequestBuilder",
        "EvolutionInterventionalGreenRequestError",
    }
    interventional_sample_kernel_exports = {
        "EvolutionInterventionalRunAuthority",
        "EvolutionInterventionalSampleKernel",
        "EvolutionInterventionalSampleKernelError",
        "EvolutionInterventionalSampleSource",
    }
    self_review_green_cohort_exports = {
        "EvolutionSelfReviewGreenCohortError",
        "EvolutionSelfReviewGreenCohortExecutor",
        "EvolutionSelfReviewGreenCohortReceipt",
        "EvolutionSelfReviewGreenCohortRequest",
        "EvolutionSelfReviewGreenCohortRequestBuilder",
        "SelfReviewGreenMetricSummary",
    }
    self_review_comparison_exports = {
        "EvolutionSelfReviewComparisonError",
        "EvolutionSelfReviewComparisonExecutor",
    }
    if name in candidate_exports:
        module_name = "candidate"
    elif name in candidate_snapshot_exports:
        module_name = "candidate_snapshots"
    elif name in evidence_exports:
        module_name = "evidence"
    elif name in failure_attribution_exports:
        module_name = "failure_attribution"
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
    elif name in mutation_generation_exports:
        module_name = "mutation_generation"
    elif name in mutation_receipt_exports:
        module_name = "mutation_receipts"
    elif name in mutation_turn_exports:
        module_name = "mutation_turns"
    elif name in patch_journal_exports:
        module_name = "patch_journals"
    elif name in postflight_guard_exports:
        module_name = "postflight_guards"
    elif name in patch_set_exports:
        module_name = "patch_sets"
    elif name in patch_set_writer_exports:
        module_name = "patch_set_writers"
    elif name in patch_recovery_exports:
        module_name = "patch_recovery"
    elif name in patch_writer_exports:
        module_name = "patch_writers"
    elif name in static_guard_exports:
        module_name = "static_guards"
    elif name in validation_plan_exports:
        module_name = "validation_plans"
    elif name in validation_cohort_exports:
        module_name = "validation_cohorts"
    elif name in validation_metric_binding_exports:
        module_name = "validation_metric_bindings"
    elif name in self_review_red_baseline_exports:
        module_name = "self_review_red_baseline"
    elif name in interventional_red_cohort_exports:
        module_name = "interventional_red_cohort"
    elif name in interventional_green_request_exports:
        module_name = "interventional_green_request"
    elif name in interventional_sample_kernel_exports:
        module_name = "interventional_sample_kernel"
    elif name in interventional_red_sample_exports:
        module_name = "interventional_red_sample"
    elif name in self_review_green_cohort_exports:
        module_name = "self_review_green_cohort"
    elif name in self_review_comparison_exports:
        module_name = "self_review_comparison"
    else:
        module_name = "store"
    module = import_module(f"naumi_agent.evolution.{module_name}")
    return getattr(module, name)
