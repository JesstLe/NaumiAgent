"""Evidence-first self-evolution primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.evolution.adversarial_batch_requests import (
        AdversarialBatchCheckCase,
        AdversarialBatchLane,
        AdversarialBatchProbeCase,
        EvolutionAdversarialBatchRequest,
        EvolutionAdversarialBatchRequestBuilder,
        EvolutionAdversarialBatchRequestError,
    )
    from naumi_agent.evolution.adversarial_cohort import (
        AdversarialCohortCheckSummary,
        EvolutionAdversarialCohortError,
        EvolutionAdversarialCohortExecutor,
        EvolutionAdversarialCohortReceipt,
    )
    from naumi_agent.evolution.adversarial_comparison import (
        EvolutionAdversarialComparisonError,
        EvolutionAdversarialComparisonExecutor,
    )
    from naumi_agent.evolution.adversarial_failure_attribution import (
        EvolutionAdversarialFailureAttributionBuilder,
        EvolutionAdversarialFailureAttributionExecutor,
    )
    from naumi_agent.evolution.adversarial_probe_contracts import (
        AdversarialProbeBlocker,
        AdversarialProbeCheckBinding,
        AdversarialProbeCoverage,
        AdversarialProbeDefinition,
        AdversarialProbeRequirement,
        EvolutionAdversarialProbeContract,
        EvolutionAdversarialProbeContractBuilder,
        EvolutionAdversarialProbeContractError,
        EvolutionAdversarialProbeRegistry,
    )
    from naumi_agent.evolution.adversarial_samples import (
        EvolutionAdversarialSampleError,
        EvolutionAdversarialSampleExecutor,
        EvolutionAdversarialSampleReceipt,
        adversarial_lane_authority_key,
    )
    from naumi_agent.evolution.approval_decisions import (
        EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY,
        EvolutionPromotionApprovalDecisionBuilder,
        EvolutionPromotionApprovalDecisionError,
        EvolutionPromotionApprovalDecisionReceipt,
        EvolutionPromotionApprovalDecisionService,
        EvolutionPromotionApprovalDecisionStatus,
        EvolutionPromotionApprovalDecisionStore,
        EvolutionPromotionApprovalDecisionView,
        EvolutionPromotionApprovalRoleDecision,
        EvolutionPromotionApprovalRoleOutcome,
        EvolutionPromotionTechnicalGateDecision,
        EvolutionPromotionTechnicalGateState,
        render_evolution_promotion_approval_decision,
    )
    from naumi_agent.evolution.approval_principals import (
        EVOLUTION_APPROVAL_PRINCIPAL_POLICY,
        EvolutionApprovalPrincipalAction,
        EvolutionApprovalPrincipalError,
        EvolutionApprovalPrincipalEvent,
        EvolutionApprovalPrincipalEventBuilder,
        EvolutionApprovalPrincipalGovernanceResult,
        EvolutionApprovalPrincipalService,
        EvolutionApprovalPrincipalState,
        EvolutionApprovalPrincipalStore,
        EvolutionApprovalPrincipalView,
        parse_approval_roles,
        render_evolution_approval_principal,
    )
    from naumi_agent.evolution.approval_requests import (
        EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY,
        EvolutionPromotionApprovalIdentityAssurance,
        EvolutionPromotionApprovalRequestError,
        EvolutionPromotionApprovalRequestService,
        EvolutionPromotionApprovalResponse,
        EvolutionPromotionApprovalResponseBuilder,
        EvolutionPromotionApprovalResponseReceipt,
        EvolutionPromotionApprovalResponseStore,
        EvolutionPromotionApprovalResponseView,
        EvolutionPromotionSignatureReceiptEntry,
        render_evolution_promotion_approval_response,
    )
    from naumi_agent.evolution.approval_requirements import (
        EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY,
        EvolutionPromotionApprovalReason,
        EvolutionPromotionApprovalRequirement,
        EvolutionPromotionApprovalRequirementBuilder,
        EvolutionPromotionApprovalRequirementError,
        EvolutionPromotionApprovalRequirementExecutor,
        EvolutionPromotionApprovalRequirementStore,
        EvolutionPromotionApprovalRequirementView,
        EvolutionPromotionApprovalRole,
        EvolutionPromotionApprovalStep,
        EvolutionPromotionTechnicalGate,
        render_evolution_promotion_approval_requirement,
    )
    from naumi_agent.evolution.approval_signatures import (
        EVOLUTION_APPROVAL_SIGNATURE_DOMAIN,
        EVOLUTION_APPROVAL_SIGNATURE_POLICY,
        EvolutionApprovalSignatureBuilder,
        EvolutionApprovalSignatureChallenge,
        EvolutionApprovalSignatureChallengeStatus,
        EvolutionApprovalSignatureChallengeView,
        EvolutionApprovalSignatureError,
        EvolutionApprovalSignaturePayload,
        EvolutionApprovalSignatureReceipt,
        EvolutionApprovalSignatureReceiptView,
        EvolutionApprovalSignatureService,
        EvolutionApprovalSignatureStore,
        render_evolution_approval_signature,
    )
    from naumi_agent.evolution.candidate import EvolutionCandidateDraft
    from naumi_agent.evolution.candidate_snapshots import (
        EvolutionCandidateSnapshotError,
        EvolutionCandidateSourceBlob,
        EvolutionCandidateWorktreeSnapshot,
        capture_candidate_worktree_snapshot,
        revalidate_candidate_worktree_snapshot,
    )
    from naumi_agent.evolution.comparison_kernel import (
        EvolutionComparisonKernel,
        EvolutionComparisonKernelError,
    )
    from naumi_agent.evolution.counterfactual_evidence import (
        CounterfactualCheck,
        CounterfactualFileEvidence,
        CounterfactualFinding,
        CounterfactualFindingCode,
        CounterfactualLeaseBinding,
        CounterfactualRequiredAction,
        CounterfactualRule,
        CounterfactualSeverity,
        EvolutionCounterfactualEvidence,
        EvolutionCounterfactualEvidenceBuilder,
        EvolutionCounterfactualEvidenceError,
        EvolutionCounterfactualEvidenceExecutor,
        EvolutionCounterfactualEvidenceStore,
        render_counterfactual_evidence,
    )
    from naumi_agent.evolution.decision_resolutions import (
        EvolutionDecisionResolution,
        EvolutionDecisionResolutionAction,
        EvolutionDecisionResolutionBuilder,
        EvolutionDecisionResolutionError,
        EvolutionDecisionResolutionOutcome,
        EvolutionDecisionResolutionService,
        EvolutionDecisionResolutionStore,
        render_evolution_decision_resolution,
        resolve_escalation_answer,
    )
    from naumi_agent.evolution.decision_states import (
        EvolutionDecisionCheck,
        EvolutionDecisionCheckStatus,
        EvolutionDecisionEscalationOption,
        EvolutionDecisionEscalationRequest,
        EvolutionDecisionReason,
        EvolutionDecisionRule,
        EvolutionDecisionState,
        EvolutionDecisionStateBuilder,
        EvolutionDecisionStateError,
        EvolutionDecisionStateExecutor,
        EvolutionDecisionStateStore,
        EvolutionDecisionStateValue,
        render_evolution_decision_state,
        resolve_evolution_decision_state,
    )
    from naumi_agent.evolution.evaluation_lane_receipts import (
        EvaluationLaneKind,
        EvolutionEvaluationArtifactRef,
        EvolutionEvaluationCohortSummary,
        EvolutionEvaluationLaneReceipt,
        EvolutionEvaluationLaneReceiptBuilder,
        EvolutionEvaluationLaneReceiptError,
        EvolutionEvaluationLaneReceiptExecutor,
        EvolutionEvaluationLaneReceiptStore,
        render_evaluation_lane_receipt,
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
        EvolutionExperimentContractAuthority,
        EvolutionExperimentContractIssuer,
        EvolutionExperimentContractStore,
        EvolutionExperimentContractStoreError,
        ExperimentBudget,
        build_experiment_contract_authority,
        render_experiment_contract_authority,
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
    from naumi_agent.evolution.independent_reviews import (
        EvolutionIndependentReview,
        EvolutionIndependentReviewBuilder,
        EvolutionIndependentReviewError,
        EvolutionIndependentReviewExecutor,
        EvolutionIndependentReviewStore,
        IndependentReviewConfidence,
        IndependentReviewerBudget,
        IndependentReviewerIdentity,
        IndependentReviewOpinion,
        IndependentReviewRecommendation,
        IndependentReviewStatus,
        render_independent_review,
    )
    from naumi_agent.evolution.interventional_cohort_kernel import (
        EvolutionInterventionalCohortKernel,
        EvolutionInterventionalCohortKernelError,
    )
    from naumi_agent.evolution.interventional_comparison import (
        EvolutionInterventionalComparisonError,
        EvolutionInterventionalComparisonExecutor,
    )
    from naumi_agent.evolution.interventional_failure_attribution import (
        EvolutionInterventionalFailureAttributionBuilder,
        EvolutionInterventionalFailureAttributionExecutor,
    )
    from naumi_agent.evolution.interventional_green_cohort import (
        EvolutionInterventionalGreenCohortError,
        EvolutionInterventionalGreenCohortExecutor,
        EvolutionInterventionalGreenCohortReceipt,
        InterventionalGreenCheckSummary,
        InterventionalGreenMetricSummary,
    )
    from naumi_agent.evolution.interventional_green_request import (
        EvolutionInterventionalGreenCohortRequest,
        EvolutionInterventionalGreenCohortRequestBuilder,
        EvolutionInterventionalGreenRequestError,
    )
    from naumi_agent.evolution.interventional_green_sample import (
        EvolutionInterventionalGreenSampleError,
        EvolutionInterventionalGreenSampleExecutor,
        EvolutionInterventionalGreenSampleReceipt,
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
    from naumi_agent.evolution.mutation_author_receipts import (
        EvolutionMutationAuthorReceipt,
        EvolutionMutationAuthorReceiptBuilder,
        EvolutionMutationAuthorReceiptError,
        EvolutionMutationAuthorReceiptStore,
        MutationAuthorModelCallFact,
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
    from naumi_agent.evolution.promotion_package_inputs import (
        EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY,
        EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY,
        EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY,
        EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
        EvolutionPromotionBaseline,
        EvolutionPromotionEvidenceKind,
        EvolutionPromotionEvidenceRef,
        EvolutionPromotionMigrationAssessment,
        EvolutionPromotionMigrationSignal,
        EvolutionPromotionPackageInput,
        EvolutionPromotionPackageInputBuilder,
        EvolutionPromotionPackageInputError,
        EvolutionPromotionPackageInputExecutor,
        EvolutionPromotionPackageInputStore,
        EvolutionPromotionPackageInputView,
        EvolutionPromotionPatchFile,
        EvolutionPromotionPatchManifest,
        EvolutionPromotionRollbackOperation,
        EvolutionPromotionRollbackPlan,
        EvolutionPromotionRollbackStep,
        render_evolution_promotion_package_input,
    )
    from naumi_agent.evolution.promotion_packages import (
        EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY,
        EVOLUTION_PROMOTION_PACKAGE_POLICY,
        EVOLUTION_PROMOTION_SIGNATURE_DOMAIN,
        EVOLUTION_PROMOTION_TARGET_POLICY,
        EvolutionPromotionApprovalInput,
        EvolutionPromotionApprovalSignal,
        EvolutionPromotionPackage,
        EvolutionPromotionPackageBuilder,
        EvolutionPromotionPackageError,
        EvolutionPromotionPackageExecutor,
        EvolutionPromotionPackageStore,
        EvolutionPromotionPackageView,
        EvolutionPromotionProtectedPath,
        EvolutionPromotionProtectedScope,
        EvolutionPromotionSignatureEnvelope,
        EvolutionPromotionTargetProbe,
        EvolutionPromotionTargetRelation,
        EvolutionPromotionTargetSnapshot,
        render_evolution_promotion_package,
    )
    from naumi_agent.evolution.reflection_memories import (
        EvolutionReflectionAction,
        EvolutionReflectionEvidenceKind,
        EvolutionReflectionEvidenceRef,
        EvolutionReflectionLessonKind,
        EvolutionReflectionMemory,
        EvolutionReflectionMemoryBuilder,
        EvolutionReflectionMemoryError,
        EvolutionReflectionMemoryExecutor,
        EvolutionReflectionMemoryRevocation,
        EvolutionReflectionMemoryRevoker,
        EvolutionReflectionMemoryStore,
        EvolutionReflectionMemoryView,
        EvolutionReflectionRevocationReason,
        EvolutionReflectionSignal,
        render_evolution_reflection_memory,
    )
    from naumi_agent.evolution.revalidation_requests import (
        EVOLUTION_REVALIDATION_REQUEST_POLICY,
        EvolutionRevalidationRequest,
        EvolutionRevalidationRequestBuilder,
        EvolutionRevalidationRequestError,
        EvolutionRevalidationRequestService,
        EvolutionRevalidationRequestStore,
        EvolutionRevalidationRequestView,
        render_evolution_revalidation_request,
    )
    from naumi_agent.evolution.reward_hacking_evidence import (
        EvolutionRewardHackingEvidence,
        EvolutionRewardHackingEvidenceBuilder,
        EvolutionRewardHackingEvidenceError,
        EvolutionRewardHackingEvidenceExecutor,
        EvolutionRewardHackingEvidenceStore,
        RewardHackingCheck,
        RewardHackingCheckStatus,
        RewardHackingCoverage,
        RewardHackingFinding,
        RewardHackingFindingCode,
        RewardHackingLaneDirection,
        RewardHackingLaneObservation,
        RewardHackingRequiredAction,
        RewardHackingResourceKind,
        RewardHackingResourceObservation,
        RewardHackingRule,
        RewardHackingSeverity,
        render_reward_hacking_evidence,
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
    "EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY",
    "EvolutionPromotionApprovalDecisionBuilder",
    "EvolutionPromotionApprovalDecisionError",
    "EvolutionPromotionApprovalDecisionReceipt",
    "EvolutionPromotionApprovalDecisionService",
    "EvolutionPromotionApprovalDecisionStatus",
    "EvolutionPromotionApprovalDecisionStore",
    "EvolutionPromotionApprovalDecisionView",
    "EvolutionPromotionApprovalRoleDecision",
    "EvolutionPromotionApprovalRoleOutcome",
    "EvolutionPromotionTechnicalGateDecision",
    "EvolutionPromotionTechnicalGateState",
    "render_evolution_promotion_approval_decision",
    "EVOLUTION_REVALIDATION_REQUEST_POLICY",
    "EvolutionRevalidationRequest",
    "EvolutionRevalidationRequestBuilder",
    "EvolutionRevalidationRequestError",
    "EvolutionRevalidationRequestService",
    "EvolutionRevalidationRequestStore",
    "EvolutionRevalidationRequestView",
    "render_evolution_revalidation_request",
    "EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY",
    "EvolutionPromotionApprovalReason",
    "EvolutionPromotionApprovalRequirement",
    "EvolutionPromotionApprovalRequirementBuilder",
    "EvolutionPromotionApprovalRequirementError",
    "EvolutionPromotionApprovalRequirementExecutor",
    "EvolutionPromotionApprovalRequirementStore",
    "EvolutionPromotionApprovalRequirementView",
    "EvolutionPromotionApprovalRole",
    "EvolutionPromotionApprovalStep",
    "EvolutionPromotionTechnicalGate",
    "render_evolution_promotion_approval_requirement",
    "EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY",
    "EvolutionPromotionApprovalIdentityAssurance",
    "EvolutionPromotionApprovalRequestError",
    "EvolutionPromotionApprovalRequestService",
    "EvolutionPromotionApprovalResponse",
    "EvolutionPromotionApprovalResponseBuilder",
    "EvolutionPromotionApprovalResponseReceipt",
    "EvolutionPromotionApprovalResponseStore",
    "EvolutionPromotionApprovalResponseView",
    "EvolutionPromotionSignatureReceiptEntry",
    "render_evolution_promotion_approval_response",
    "EVOLUTION_APPROVAL_SIGNATURE_DOMAIN",
    "EVOLUTION_APPROVAL_SIGNATURE_POLICY",
    "EvolutionApprovalSignatureBuilder",
    "EvolutionApprovalSignatureChallenge",
    "EvolutionApprovalSignatureChallengeStatus",
    "EvolutionApprovalSignatureChallengeView",
    "EvolutionApprovalSignatureError",
    "EvolutionApprovalSignaturePayload",
    "EvolutionApprovalSignatureReceipt",
    "EvolutionApprovalSignatureReceiptView",
    "EvolutionApprovalSignatureService",
    "EvolutionApprovalSignatureStore",
    "render_evolution_approval_signature",
    "EVOLUTION_APPROVAL_PRINCIPAL_POLICY",
    "EvolutionApprovalPrincipalAction",
    "EvolutionApprovalPrincipalError",
    "EvolutionApprovalPrincipalEvent",
    "EvolutionApprovalPrincipalEventBuilder",
    "EvolutionApprovalPrincipalGovernanceResult",
    "EvolutionApprovalPrincipalService",
    "EvolutionApprovalPrincipalState",
    "EvolutionApprovalPrincipalStore",
    "EvolutionApprovalPrincipalView",
    "parse_approval_roles",
    "render_evolution_approval_principal",
    "AdversarialBatchCheckCase",
    "AdversarialBatchLane",
    "AdversarialBatchProbeCase",
    "AdversarialProbeBlocker",
    "AdversarialProbeCheckBinding",
    "AdversarialProbeCoverage",
    "AdversarialProbeDefinition",
    "AdversarialProbeRequirement",
    "EvolutionAdversarialProbeContract",
    "EvolutionAdversarialProbeContractBuilder",
    "EvolutionAdversarialProbeContractError",
    "EvolutionAdversarialProbeRegistry",
    "EvolutionAdversarialBatchRequest",
    "EvolutionAdversarialBatchRequestBuilder",
    "EvolutionAdversarialBatchRequestError",
    "AdversarialCohortCheckSummary",
    "EvolutionAdversarialCohortError",
    "EvolutionAdversarialCohortExecutor",
    "EvolutionAdversarialCohortReceipt",
    "EvolutionAdversarialComparisonError",
    "EvolutionAdversarialComparisonExecutor",
    "EvolutionAdversarialFailureAttributionBuilder",
    "EvolutionAdversarialFailureAttributionExecutor",
    "EvolutionAdversarialSampleError",
    "EvolutionAdversarialSampleExecutor",
    "EvolutionAdversarialSampleReceipt",
    "adversarial_lane_authority_key",
    "EvolutionEvidence",
    "EvaluationLaneKind",
    "EvolutionEvaluationArtifactRef",
    "EvolutionEvaluationCohortSummary",
    "EvolutionEvaluationLaneReceipt",
    "EvolutionEvaluationLaneReceiptBuilder",
    "EvolutionEvaluationLaneReceiptError",
    "EvolutionEvaluationLaneReceiptExecutor",
    "EvolutionEvaluationLaneReceiptStore",
    "render_evaluation_lane_receipt",
    "EvolutionFailureAttributionBuilder",
    "EvolutionFailureAttributionError",
    "EvolutionFailureAttributionExecutor",
    "EvolutionFailureAttributionReceipt",
    "EvolutionFailureAttributionStore",
    "FailureAttributionAction",
    "FailureAttributionCategory",
    "EvolutionIndependentReview",
    "EvolutionIndependentReviewBuilder",
    "EvolutionIndependentReviewError",
    "EvolutionIndependentReviewExecutor",
    "EvolutionIndependentReviewStore",
    "IndependentReviewConfidence",
    "IndependentReviewOpinion",
    "IndependentReviewRecommendation",
    "IndependentReviewStatus",
    "IndependentReviewerBudget",
    "IndependentReviewerIdentity",
    "render_independent_review",
    "CounterfactualCheck",
    "CounterfactualFileEvidence",
    "CounterfactualFinding",
    "CounterfactualFindingCode",
    "CounterfactualLeaseBinding",
    "CounterfactualRequiredAction",
    "CounterfactualRule",
    "CounterfactualSeverity",
    "EvolutionCounterfactualEvidence",
    "EvolutionCounterfactualEvidenceBuilder",
    "EvolutionCounterfactualEvidenceError",
    "EvolutionCounterfactualEvidenceExecutor",
    "EvolutionCounterfactualEvidenceStore",
    "render_counterfactual_evidence",
    "EvolutionDecisionCheck",
    "EvolutionDecisionCheckStatus",
    "EvolutionDecisionEscalationOption",
    "EvolutionDecisionEscalationRequest",
    "EvolutionDecisionReason",
    "EvolutionDecisionRule",
    "EvolutionDecisionState",
    "EvolutionDecisionStateBuilder",
    "EvolutionDecisionStateError",
    "EvolutionDecisionStateExecutor",
    "EvolutionDecisionStateStore",
    "EvolutionDecisionStateValue",
    "render_evolution_decision_state",
    "resolve_evolution_decision_state",
    "EvolutionDecisionResolution",
    "EvolutionDecisionResolutionAction",
    "EvolutionDecisionResolutionBuilder",
    "EvolutionDecisionResolutionError",
    "EvolutionDecisionResolutionOutcome",
    "EvolutionDecisionResolutionService",
    "EvolutionDecisionResolutionStore",
    "render_evolution_decision_resolution",
    "resolve_escalation_answer",
    "EvolutionRewardHackingEvidence",
    "EvolutionRewardHackingEvidenceBuilder",
    "EvolutionRewardHackingEvidenceError",
    "EvolutionRewardHackingEvidenceExecutor",
    "EvolutionRewardHackingEvidenceStore",
    "RewardHackingCheck",
    "RewardHackingCheckStatus",
    "RewardHackingCoverage",
    "RewardHackingFinding",
    "RewardHackingFindingCode",
    "RewardHackingLaneDirection",
    "RewardHackingLaneObservation",
    "RewardHackingRequiredAction",
    "RewardHackingResourceKind",
    "RewardHackingResourceObservation",
    "RewardHackingRule",
    "RewardHackingSeverity",
    "render_reward_hacking_evidence",
    "EvolutionReflectionAction",
    "EvolutionReflectionEvidenceKind",
    "EvolutionReflectionEvidenceRef",
    "EvolutionReflectionLessonKind",
    "EvolutionReflectionMemory",
    "EvolutionReflectionMemoryBuilder",
    "EvolutionReflectionMemoryError",
    "EvolutionReflectionMemoryExecutor",
    "EvolutionReflectionMemoryRevocation",
    "EvolutionReflectionMemoryRevoker",
    "EvolutionReflectionMemoryStore",
    "EvolutionReflectionMemoryView",
    "EvolutionReflectionRevocationReason",
    "EvolutionReflectionSignal",
    "render_evolution_reflection_memory",
    "EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY",
    "EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY",
    "EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY",
    "EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY",
    "EvolutionPromotionBaseline",
    "EvolutionPromotionEvidenceKind",
    "EvolutionPromotionEvidenceRef",
    "EvolutionPromotionMigrationAssessment",
    "EvolutionPromotionMigrationSignal",
    "EvolutionPromotionPackageInput",
    "EvolutionPromotionPackageInputBuilder",
    "EvolutionPromotionPackageInputError",
    "EvolutionPromotionPackageInputExecutor",
    "EvolutionPromotionPackageInputStore",
    "EvolutionPromotionPackageInputView",
    "EvolutionPromotionPatchFile",
    "EvolutionPromotionPatchManifest",
    "EvolutionPromotionRollbackOperation",
    "EvolutionPromotionRollbackPlan",
    "EvolutionPromotionRollbackStep",
    "render_evolution_promotion_package_input",
    "EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY",
    "EVOLUTION_PROMOTION_PACKAGE_POLICY",
    "EVOLUTION_PROMOTION_SIGNATURE_DOMAIN",
    "EVOLUTION_PROMOTION_TARGET_POLICY",
    "EvolutionPromotionApprovalInput",
    "EvolutionPromotionApprovalSignal",
    "EvolutionPromotionPackage",
    "EvolutionPromotionPackageBuilder",
    "EvolutionPromotionPackageError",
    "EvolutionPromotionPackageExecutor",
    "EvolutionPromotionPackageStore",
    "EvolutionPromotionPackageView",
    "EvolutionPromotionProtectedPath",
    "EvolutionPromotionProtectedScope",
    "EvolutionPromotionSignatureEnvelope",
    "EvolutionPromotionTargetProbe",
    "EvolutionPromotionTargetRelation",
    "EvolutionPromotionTargetSnapshot",
    "render_evolution_promotion_package",
    "EvolutionCandidateDraft",
    "EvolutionCandidateSnapshotError",
    "EvolutionCandidateSourceBlob",
    "EvolutionCandidateWorktreeSnapshot",
    "EvolutionComparisonKernel",
    "EvolutionComparisonKernelError",
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
    "EvolutionExperimentContractAuthority",
    "EvolutionExperimentContractIssuer",
    "EvolutionExperimentContractStore",
    "EvolutionExperimentContractStoreError",
    "build_experiment_contract_authority",
    "render_experiment_contract_authority",
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
    "EvolutionMutationAuthorReceipt",
    "EvolutionMutationAuthorReceiptBuilder",
    "EvolutionMutationAuthorReceiptError",
    "EvolutionMutationAuthorReceiptStore",
    "MutationAuthorModelCallFact",
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
    "EvolutionInterventionalGreenCohortError",
    "EvolutionInterventionalGreenCohortExecutor",
    "EvolutionInterventionalGreenCohortReceipt",
    "InterventionalGreenCheckSummary",
    "InterventionalGreenMetricSummary",
    "EvolutionInterventionalGreenSampleError",
    "EvolutionInterventionalGreenSampleExecutor",
    "EvolutionInterventionalGreenSampleReceipt",
    "EvolutionInterventionalComparisonError",
    "EvolutionInterventionalComparisonExecutor",
    "EvolutionInterventionalFailureAttributionBuilder",
    "EvolutionInterventionalFailureAttributionExecutor",
    "EvolutionInterventionalCohortKernel",
    "EvolutionInterventionalCohortKernelError",
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
    approval_decision_exports = {
        "EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY",
        "EvolutionPromotionApprovalDecisionBuilder",
        "EvolutionPromotionApprovalDecisionError",
        "EvolutionPromotionApprovalDecisionReceipt",
        "EvolutionPromotionApprovalDecisionService",
        "EvolutionPromotionApprovalDecisionStatus",
        "EvolutionPromotionApprovalDecisionStore",
        "EvolutionPromotionApprovalDecisionView",
        "EvolutionPromotionApprovalRoleDecision",
        "EvolutionPromotionApprovalRoleOutcome",
        "EvolutionPromotionTechnicalGateDecision",
        "EvolutionPromotionTechnicalGateState",
        "render_evolution_promotion_approval_decision",
    }
    approval_requirement_exports = {
        "EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY",
        "EvolutionPromotionApprovalReason",
        "EvolutionPromotionApprovalRequirement",
        "EvolutionPromotionApprovalRequirementBuilder",
        "EvolutionPromotionApprovalRequirementError",
        "EvolutionPromotionApprovalRequirementExecutor",
        "EvolutionPromotionApprovalRequirementStore",
        "EvolutionPromotionApprovalRequirementView",
        "EvolutionPromotionApprovalRole",
        "EvolutionPromotionApprovalStep",
        "EvolutionPromotionTechnicalGate",
        "render_evolution_promotion_approval_requirement",
    }
    revalidation_request_exports = {
        "EVOLUTION_REVALIDATION_REQUEST_POLICY",
        "EvolutionRevalidationRequest",
        "EvolutionRevalidationRequestBuilder",
        "EvolutionRevalidationRequestError",
        "EvolutionRevalidationRequestService",
        "EvolutionRevalidationRequestStore",
        "EvolutionRevalidationRequestView",
        "render_evolution_revalidation_request",
    }
    approval_request_exports = {
        "EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY",
        "EvolutionPromotionApprovalIdentityAssurance",
        "EvolutionPromotionApprovalRequestError",
        "EvolutionPromotionApprovalRequestService",
        "EvolutionPromotionApprovalResponse",
        "EvolutionPromotionApprovalResponseBuilder",
        "EvolutionPromotionApprovalResponseReceipt",
        "EvolutionPromotionApprovalResponseStore",
        "EvolutionPromotionApprovalResponseView",
        "EvolutionPromotionSignatureReceiptEntry",
        "render_evolution_promotion_approval_response",
    }
    approval_signature_exports = {
        "EVOLUTION_APPROVAL_SIGNATURE_DOMAIN",
        "EVOLUTION_APPROVAL_SIGNATURE_POLICY",
        "EvolutionApprovalSignatureBuilder",
        "EvolutionApprovalSignatureChallenge",
        "EvolutionApprovalSignatureChallengeStatus",
        "EvolutionApprovalSignatureChallengeView",
        "EvolutionApprovalSignatureError",
        "EvolutionApprovalSignaturePayload",
        "EvolutionApprovalSignatureReceipt",
        "EvolutionApprovalSignatureReceiptView",
        "EvolutionApprovalSignatureService",
        "EvolutionApprovalSignatureStore",
        "render_evolution_approval_signature",
    }
    approval_principal_exports = {
        "EVOLUTION_APPROVAL_PRINCIPAL_POLICY",
        "EvolutionApprovalPrincipalAction",
        "EvolutionApprovalPrincipalError",
        "EvolutionApprovalPrincipalEvent",
        "EvolutionApprovalPrincipalEventBuilder",
        "EvolutionApprovalPrincipalGovernanceResult",
        "EvolutionApprovalPrincipalService",
        "EvolutionApprovalPrincipalState",
        "EvolutionApprovalPrincipalStore",
        "EvolutionApprovalPrincipalView",
        "parse_approval_roles",
        "render_evolution_approval_principal",
    }
    candidate_exports = {"EvolutionCandidateDraft", "build_candidate_draft"}
    adversarial_batch_request_exports = {
        "AdversarialBatchCheckCase",
        "AdversarialBatchLane",
        "AdversarialBatchProbeCase",
        "EvolutionAdversarialBatchRequest",
        "EvolutionAdversarialBatchRequestBuilder",
        "EvolutionAdversarialBatchRequestError",
    }
    adversarial_cohort_exports = {
        "AdversarialCohortCheckSummary",
        "EvolutionAdversarialCohortError",
        "EvolutionAdversarialCohortExecutor",
        "EvolutionAdversarialCohortReceipt",
    }
    adversarial_comparison_exports = {
        "EvolutionAdversarialComparisonError",
        "EvolutionAdversarialComparisonExecutor",
    }
    adversarial_failure_attribution_exports = {
        "EvolutionAdversarialFailureAttributionBuilder",
        "EvolutionAdversarialFailureAttributionExecutor",
    }
    adversarial_probe_contract_exports = {
        "AdversarialProbeBlocker",
        "AdversarialProbeCheckBinding",
        "AdversarialProbeCoverage",
        "AdversarialProbeDefinition",
        "AdversarialProbeRequirement",
        "EvolutionAdversarialProbeContract",
        "EvolutionAdversarialProbeContractBuilder",
        "EvolutionAdversarialProbeContractError",
        "EvolutionAdversarialProbeRegistry",
    }
    adversarial_sample_exports = {
        "EvolutionAdversarialSampleError",
        "EvolutionAdversarialSampleExecutor",
        "EvolutionAdversarialSampleReceipt",
        "adversarial_lane_authority_key",
    }
    candidate_snapshot_exports = {
        "EvolutionCandidateSnapshotError",
        "EvolutionCandidateSourceBlob",
        "EvolutionCandidateWorktreeSnapshot",
        "capture_candidate_worktree_snapshot",
        "revalidate_candidate_worktree_snapshot",
    }
    comparison_kernel_exports = {
        "EvolutionComparisonKernel",
        "EvolutionComparisonKernelError",
    }
    evidence_exports = {
        "EvolutionEvidence",
        "adapt_harness_failure_evidence",
        "adapt_self_review_static_evidence",
    }
    evaluation_lane_receipt_exports = {
        "EvaluationLaneKind",
        "EvolutionEvaluationArtifactRef",
        "EvolutionEvaluationCohortSummary",
        "EvolutionEvaluationLaneReceipt",
        "EvolutionEvaluationLaneReceiptBuilder",
        "EvolutionEvaluationLaneReceiptError",
        "EvolutionEvaluationLaneReceiptExecutor",
        "EvolutionEvaluationLaneReceiptStore",
        "render_evaluation_lane_receipt",
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
    independent_review_exports = {
        "EvolutionIndependentReview",
        "EvolutionIndependentReviewBuilder",
        "EvolutionIndependentReviewError",
        "EvolutionIndependentReviewExecutor",
        "EvolutionIndependentReviewStore",
        "IndependentReviewConfidence",
        "IndependentReviewOpinion",
        "IndependentReviewRecommendation",
        "IndependentReviewStatus",
        "IndependentReviewerBudget",
        "IndependentReviewerIdentity",
        "render_independent_review",
    }
    counterfactual_evidence_exports = {
        "CounterfactualCheck",
        "CounterfactualFileEvidence",
        "CounterfactualFinding",
        "CounterfactualFindingCode",
        "CounterfactualLeaseBinding",
        "CounterfactualRequiredAction",
        "CounterfactualRule",
        "CounterfactualSeverity",
        "EvolutionCounterfactualEvidence",
        "EvolutionCounterfactualEvidenceBuilder",
        "EvolutionCounterfactualEvidenceError",
        "EvolutionCounterfactualEvidenceExecutor",
        "EvolutionCounterfactualEvidenceStore",
        "render_counterfactual_evidence",
    }
    decision_state_exports = {
        "EvolutionDecisionCheck",
        "EvolutionDecisionCheckStatus",
        "EvolutionDecisionEscalationOption",
        "EvolutionDecisionEscalationRequest",
        "EvolutionDecisionReason",
        "EvolutionDecisionRule",
        "EvolutionDecisionState",
        "EvolutionDecisionStateBuilder",
        "EvolutionDecisionStateError",
        "EvolutionDecisionStateExecutor",
        "EvolutionDecisionStateStore",
        "EvolutionDecisionStateValue",
        "render_evolution_decision_state",
        "resolve_evolution_decision_state",
    }
    decision_resolution_exports = {
        "EvolutionDecisionResolution",
        "EvolutionDecisionResolutionAction",
        "EvolutionDecisionResolutionBuilder",
        "EvolutionDecisionResolutionError",
        "EvolutionDecisionResolutionOutcome",
        "EvolutionDecisionResolutionService",
        "EvolutionDecisionResolutionStore",
        "render_evolution_decision_resolution",
        "resolve_escalation_answer",
    }
    reward_hacking_evidence_exports = {
        "EvolutionRewardHackingEvidence",
        "EvolutionRewardHackingEvidenceBuilder",
        "EvolutionRewardHackingEvidenceError",
        "EvolutionRewardHackingEvidenceExecutor",
        "EvolutionRewardHackingEvidenceStore",
        "RewardHackingCheck",
        "RewardHackingCheckStatus",
        "RewardHackingCoverage",
        "RewardHackingFinding",
        "RewardHackingFindingCode",
        "RewardHackingLaneDirection",
        "RewardHackingLaneObservation",
        "RewardHackingRequiredAction",
        "RewardHackingResourceKind",
        "RewardHackingResourceObservation",
        "RewardHackingRule",
        "RewardHackingSeverity",
        "render_reward_hacking_evidence",
    }
    reflection_memory_exports = {
        "EvolutionReflectionAction",
        "EvolutionReflectionEvidenceKind",
        "EvolutionReflectionEvidenceRef",
        "EvolutionReflectionLessonKind",
        "EvolutionReflectionMemory",
        "EvolutionReflectionMemoryBuilder",
        "EvolutionReflectionMemoryError",
        "EvolutionReflectionMemoryExecutor",
        "EvolutionReflectionMemoryRevocation",
        "EvolutionReflectionMemoryRevoker",
        "EvolutionReflectionMemoryStore",
        "EvolutionReflectionMemoryView",
        "EvolutionReflectionRevocationReason",
        "EvolutionReflectionSignal",
        "render_evolution_reflection_memory",
    }
    promotion_package_input_exports = {
        "EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY",
        "EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY",
        "EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY",
        "EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY",
        "EvolutionPromotionBaseline",
        "EvolutionPromotionEvidenceKind",
        "EvolutionPromotionEvidenceRef",
        "EvolutionPromotionMigrationAssessment",
        "EvolutionPromotionMigrationSignal",
        "EvolutionPromotionPackageInput",
        "EvolutionPromotionPackageInputBuilder",
        "EvolutionPromotionPackageInputError",
        "EvolutionPromotionPackageInputExecutor",
        "EvolutionPromotionPackageInputStore",
        "EvolutionPromotionPackageInputView",
        "EvolutionPromotionPatchFile",
        "EvolutionPromotionPatchManifest",
        "EvolutionPromotionRollbackOperation",
        "EvolutionPromotionRollbackPlan",
        "EvolutionPromotionRollbackStep",
        "render_evolution_promotion_package_input",
    }
    promotion_package_exports = {
        "EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY",
        "EVOLUTION_PROMOTION_PACKAGE_POLICY",
        "EVOLUTION_PROMOTION_SIGNATURE_DOMAIN",
        "EVOLUTION_PROMOTION_TARGET_POLICY",
        "EvolutionPromotionApprovalInput",
        "EvolutionPromotionApprovalSignal",
        "EvolutionPromotionPackage",
        "EvolutionPromotionPackageBuilder",
        "EvolutionPromotionPackageError",
        "EvolutionPromotionPackageExecutor",
        "EvolutionPromotionPackageStore",
        "EvolutionPromotionPackageView",
        "EvolutionPromotionProtectedPath",
        "EvolutionPromotionProtectedScope",
        "EvolutionPromotionSignatureEnvelope",
        "EvolutionPromotionTargetProbe",
        "EvolutionPromotionTargetRelation",
        "EvolutionPromotionTargetSnapshot",
        "render_evolution_promotion_package",
    }
    proposal_exports = {
        "EvolutionProposalPreview",
        "classify_proposal_kind",
        "generate_proposal_preview",
        "parse_proposal_scope_files",
    }
    experiment_exports = {
        "EvolutionExperimentContract",
        "EvolutionExperimentContractAuthority",
        "EvolutionExperimentContractIssuer",
        "EvolutionExperimentContractStore",
        "EvolutionExperimentContractStoreError",
        "ExperimentBudget",
        "build_experiment_contract_authority",
        "render_experiment_contract_authority",
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
    mutation_author_receipt_exports = {
        "EvolutionMutationAuthorReceipt",
        "EvolutionMutationAuthorReceiptBuilder",
        "EvolutionMutationAuthorReceiptError",
        "EvolutionMutationAuthorReceiptStore",
        "MutationAuthorModelCallFact",
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
    interventional_green_cohort_exports = {
        "EvolutionInterventionalGreenCohortError",
        "EvolutionInterventionalGreenCohortExecutor",
        "EvolutionInterventionalGreenCohortReceipt",
        "InterventionalGreenCheckSummary",
        "InterventionalGreenMetricSummary",
    }
    interventional_green_sample_exports = {
        "EvolutionInterventionalGreenSampleError",
        "EvolutionInterventionalGreenSampleExecutor",
        "EvolutionInterventionalGreenSampleReceipt",
    }
    interventional_comparison_exports = {
        "EvolutionInterventionalComparisonError",
        "EvolutionInterventionalComparisonExecutor",
    }
    interventional_failure_attribution_exports = {
        "EvolutionInterventionalFailureAttributionBuilder",
        "EvolutionInterventionalFailureAttributionExecutor",
    }
    interventional_cohort_kernel_exports = {
        "EvolutionInterventionalCohortKernel",
        "EvolutionInterventionalCohortKernelError",
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
    if name in approval_decision_exports:
        module_name = "approval_decisions"
    elif name in revalidation_request_exports:
        module_name = "revalidation_requests"
    elif name in approval_requirement_exports:
        module_name = "approval_requirements"
    elif name in approval_request_exports:
        module_name = "approval_requests"
    elif name in approval_signature_exports:
        module_name = "approval_signatures"
    elif name in approval_principal_exports:
        module_name = "approval_principals"
    elif name in adversarial_comparison_exports:
        module_name = "adversarial_comparison"
    elif name in adversarial_failure_attribution_exports:
        module_name = "adversarial_failure_attribution"
    elif name in adversarial_cohort_exports:
        module_name = "adversarial_cohort"
    elif name in adversarial_sample_exports:
        module_name = "adversarial_samples"
    elif name in adversarial_batch_request_exports:
        module_name = "adversarial_batch_requests"
    elif name in adversarial_probe_contract_exports:
        module_name = "adversarial_probe_contracts"
    elif name in candidate_exports:
        module_name = "candidate"
    elif name in candidate_snapshot_exports:
        module_name = "candidate_snapshots"
    elif name in comparison_kernel_exports:
        module_name = "comparison_kernel"
    elif name in evidence_exports:
        module_name = "evidence"
    elif name in evaluation_lane_receipt_exports:
        module_name = "evaluation_lane_receipts"
    elif name in failure_attribution_exports:
        module_name = "failure_attribution"
    elif name in independent_review_exports:
        module_name = "independent_reviews"
    elif name in counterfactual_evidence_exports:
        module_name = "counterfactual_evidence"
    elif name in decision_resolution_exports:
        module_name = "decision_resolutions"
    elif name in decision_state_exports:
        module_name = "decision_states"
    elif name in reward_hacking_evidence_exports:
        module_name = "reward_hacking_evidence"
    elif name in reflection_memory_exports:
        module_name = "reflection_memories"
    elif name in promotion_package_input_exports:
        module_name = "promotion_package_inputs"
    elif name in promotion_package_exports:
        module_name = "promotion_packages"
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
    elif name in mutation_author_receipt_exports:
        module_name = "mutation_author_receipts"
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
    elif name in interventional_green_cohort_exports:
        module_name = "interventional_green_cohort"
    elif name in interventional_green_sample_exports:
        module_name = "interventional_green_sample"
    elif name in interventional_comparison_exports:
        module_name = "interventional_comparison"
    elif name in interventional_failure_attribution_exports:
        module_name = "interventional_failure_attribution"
    elif name in interventional_cohort_kernel_exports:
        module_name = "interventional_cohort_kernel"
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
