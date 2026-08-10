"""Read-only Workbench projection of governed Evolution outcomes."""

from __future__ import annotations

import asyncio
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.post_rollback_behavioral_matrix import (
    EvolutionPostRollbackBehavioralMatrix,
    EvolutionPostRollbackBehavioralMatrixError,
    EvolutionPostRollbackBehavioralMatrixService,
    EvolutionPostRollbackBehavioralMatrixStore,
    EvolutionPostRollbackBehavioralMatrixView,
)
from naumi_agent.evolution.post_rollback_runtime_verifications import (
    EvolutionPostRollbackRuntimeVerification,
    EvolutionPostRollbackRuntimeVerificationError,
    EvolutionPostRollbackRuntimeVerificationService,
    EvolutionPostRollbackRuntimeVerificationStore,
    EvolutionPostRollbackRuntimeVerificationView,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterEvidence,
    EvolutionProposalBeforeAfterEvidenceError,
    EvolutionProposalBeforeAfterEvidenceService,
    EvolutionProposalBeforeAfterEvidenceStore,
    EvolutionProposalBeforeAfterEvidenceView,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
    EvolutionRevalidationRollbackOutcomeService,
    EvolutionRevalidationRollbackOutcomeStore,
    EvolutionRevalidationRollbackOutcomeView,
)

EVOLUTION_PROPOSAL_OUTCOME_PROJECTION_POLICY = "evolution-proposal-outcome-projection-v1"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionProposalOutcomeProjection(_StrictModel):
    """Dynamic display projection; never a replacement for Proposal governance state."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-proposal-outcome-projection-v1"] = (
        EVOLUTION_PROPOSAL_OUTCOME_PROJECTION_POLICY
    )
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
    governance_state_unchanged: Literal[True] = True
    status: Literal["rolled_back"] = "rolled_back"
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    rollback_receipt_id: str = Field(pattern=r"^evrerollbackexec_[0-9a-f]{24}$")
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    breach_reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    recorded_at: str = Field(min_length=1, max_length=100)
    authority_valid: bool
    active_baseline: bool
    contract_issue_allowed: Literal[False] = False
    before_after_evidence: EvolutionProposalBeforeAfterEvidence | None = None
    before_after_recorded: bool = False
    post_rollback_verification: EvolutionPostRollbackRuntimeVerification | None = None
    post_rollback_verification_recorded: bool = False
    post_rollback_evaluation_recorded: bool = False
    post_rollback_behavioral_matrix: EvolutionPostRollbackBehavioralMatrix | None = None
    post_rollback_behavioral_evaluation_recorded: bool = False
    long_term_metrics_recorded: Literal[False] = False
    promoted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        if self.active_baseline and not self.authority_valid:
            raise ValueError("Proposal Outcome active baseline 超出 authority。")
        if self.before_after_recorded is not (self.before_after_evidence is not None):
            raise ValueError("Proposal Outcome before/after 状态与 evidence 不一致。")
        evidence = self.before_after_evidence
        if evidence is not None and not (
            evidence.outcome_id == self.outcome_id
            and evidence.outcome_sha256 == self.outcome_sha256
            and evidence.workbench_session_id == self.workbench_session_id
            and evidence.workbench_proposal_id == self.workbench_proposal_id
            and evidence.experiment_contract_id == self.experiment_contract_id
            and evidence.candidate_id == self.candidate_id
            and evidence.candidate_revision == self.candidate_revision
            and evidence.before_after_recorded
            and not evidence.post_rollback_evaluation_recorded
            and not evidence.long_term_metrics_recorded
            and not evidence.promoted
            and not evidence.learning_authority
            and not evidence.promotion_authority
        ):
            raise ValueError("Proposal Outcome before/after evidence 绑定无效。")
        verification = self.post_rollback_verification
        recorded = verification is not None
        if not (
            self.post_rollback_verification_recorded is recorded
            and self.post_rollback_evaluation_recorded is recorded
        ):
            raise ValueError("Proposal Outcome post-rollback 状态与 evidence 不一致。")
        if verification is not None and not (
            verification.outcome_id == self.outcome_id
            and verification.outcome_sha256 == self.outcome_sha256
            and verification.workbench_session_id == self.workbench_session_id
            and verification.workbench_proposal_id == self.workbench_proposal_id
            and verification.experiment_contract_id == self.experiment_contract_id
            and verification.candidate_id == self.candidate_id
            and verification.candidate_revision == self.candidate_revision
            and verification.post_rollback_verification_recorded
            and verification.post_rollback_evaluation_recorded
            and not verification.behavioral_evaluation_recorded
            and not verification.long_term_metrics_recorded
            and not verification.learning_authority
            and not verification.promotion_authority
        ):
            raise ValueError("Proposal Outcome post-rollback evidence 绑定无效。")
        matrix = self.post_rollback_behavioral_matrix
        matrix_recorded = matrix is not None
        if self.post_rollback_behavioral_evaluation_recorded is not matrix_recorded:
            raise ValueError("Proposal Outcome Behavioral Matrix 状态不一致。")
        if matrix is not None and not (
            evidence is not None
            and verification is not None
            and matrix.outcome_id == self.outcome_id
            and matrix.outcome_sha256 == self.outcome_sha256
            and matrix.before_after_evidence_id == evidence.evidence_id
            and matrix.before_after_evidence_sha256 == evidence.evidence_sha256
            and matrix.final_evaluation_id == evidence.final_evaluation_id
            and matrix.final_evaluation_sha256 == evidence.final_evaluation_sha256
            and matrix.runtime_verification_id == verification.verification_id
            and matrix.runtime_verification_sha256
            == verification.verification_sha256
            and matrix.behavioral_evaluation_recorded
            and not matrix.long_term_metrics_recorded
            and not matrix.learning_authority
            and not matrix.promotion_authority
        ):
            raise ValueError("Proposal Outcome Behavioral Matrix 绑定无效。")
        return self


class EvolutionProposalOutcomeProjectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionProposalOutcomeProjectionService:
    def __init__(
        self,
        *,
        rollback_outcome_store: EvolutionRevalidationRollbackOutcomeStore,
        rollback_outcome_service: EvolutionRevalidationRollbackOutcomeService,
        before_after_store: EvolutionProposalBeforeAfterEvidenceStore | None = None,
        before_after_service: EvolutionProposalBeforeAfterEvidenceService | None = None,
        post_rollback_store: EvolutionPostRollbackRuntimeVerificationStore | None = None,
        post_rollback_service: EvolutionPostRollbackRuntimeVerificationService
        | None = None,
        behavioral_matrix_store: EvolutionPostRollbackBehavioralMatrixStore
        | None = None,
        behavioral_matrix_service: EvolutionPostRollbackBehavioralMatrixService
        | None = None,
    ) -> None:
        self.rollback_outcome_store = rollback_outcome_store
        self.rollback_outcome_service = rollback_outcome_service
        if (before_after_store is None) != (before_after_service is None):
            raise ValueError("Before/After Store 与 Service 必须同时绑定。")
        self.before_after_store = before_after_store
        self.before_after_service = before_after_service
        if (post_rollback_store is None) != (post_rollback_service is None):
            raise ValueError("Post-Rollback Store 与 Service 必须同时绑定。")
        self.post_rollback_store = post_rollback_store
        self.post_rollback_service = post_rollback_service
        if (behavioral_matrix_store is None) != (behavioral_matrix_service is None):
            raise ValueError("Behavioral Matrix Store 与 Service 必须同时绑定。")
        self.behavioral_matrix_store = behavioral_matrix_store
        self.behavioral_matrix_service = behavioral_matrix_service

    async def project_session(
        self,
        session_id: str,
    ) -> dict[str, EvolutionProposalOutcomeProjection]:
        normalized = str(session_id or "").strip()
        if re.fullmatch(_SAFE_BINDING_RE, normalized) is None:
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_session_id_invalid",
                "Workbench Session ID 格式无效。",
            )
        try:
            outcomes = await self.rollback_outcome_store.list_by_session(normalized)
        except (EvolutionRevalidationRollbackOutcomeError, TypeError, ValueError) as exc:
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_source_unavailable",
                "Proposal Outcome source 暂不可用。",
            ) from exc
        by_proposal: dict[str, EvolutionRevalidationRollbackOutcome] = {}
        for outcome in outcomes:
            if outcome.workbench_proposal_id in by_proposal:
                raise EvolutionProposalOutcomeProjectionError(
                    "proposal_outcome_ambiguous",
                    "同一 Workbench Proposal 存在多个未 supersede Outcome。",
                )
            by_proposal[outcome.workbench_proposal_id] = outcome
        if not by_proposal:
            return {}
        views = await asyncio.gather(
            *(
                self.rollback_outcome_service.inspect(request_id=outcome.request_id)
                for outcome in by_proposal.values()
            ),
            return_exceptions=True,
        )
        evidence_views = await self._before_after_views(tuple(by_proposal.values()))
        post_rollback_views = await self._post_rollback_views(
            tuple(by_proposal.values())
        )
        matrix_views = await self._behavioral_matrix_views(
            tuple(by_proposal.values())
        )
        projections: dict[str, EvolutionProposalOutcomeProjection] = {}
        for (
            (proposal_id, outcome),
            result,
            evidence_view,
            verification_view,
            matrix_view,
        ) in zip(
            by_proposal.items(),
            views,
            evidence_views,
            post_rollback_views,
            matrix_views,
            strict=True,
        ):
            if isinstance(result, BaseException):
                raise EvolutionProposalOutcomeProjectionError(
                    "proposal_outcome_source_unavailable",
                    "Proposal Outcome source 暂不可用。",
                ) from result
            projections[proposal_id] = _project(
                outcome,
                result,
                evidence_view,
                verification_view,
                matrix_view,
            )
        return projections

    async def _before_after_views(
        self,
        outcomes: tuple[EvolutionRevalidationRollbackOutcome, ...],
    ) -> tuple[EvolutionProposalBeforeAfterEvidenceView | None, ...]:
        if self.before_after_store is None or self.before_after_service is None:
            return tuple(None for _ in outcomes)
        try:
            evidence = await asyncio.gather(*(
                self.before_after_store.get_by_outcome(item.outcome_id)
                for item in outcomes
            ))
            inspected = await asyncio.gather(*(
                self.before_after_service.inspect(evidence=item)
                if item is not None
                else _none()
                for item in evidence
            ))
        except (
            EvolutionProposalBeforeAfterEvidenceError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_before_after_unavailable",
                "Proposal Before/After Evidence source 暂不可用。",
            ) from exc
        if any(
            item is not None and not item.before_after_authority
            for item in inspected
        ):
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_before_after_stale",
                "Proposal Before/After Evidence authority 当前无效。",
            )
        return tuple(inspected)

    async def _post_rollback_views(
        self,
        outcomes: tuple[EvolutionRevalidationRollbackOutcome, ...],
    ) -> tuple[EvolutionPostRollbackRuntimeVerificationView | None, ...]:
        if self.post_rollback_store is None or self.post_rollback_service is None:
            return tuple(None for _ in outcomes)
        try:
            evidence = await asyncio.gather(*(
                self.post_rollback_store.get_by_outcome(item.outcome_id)
                for item in outcomes
            ))
            inspected = await asyncio.gather(*(
                self.post_rollback_service.inspect(verification=item)
                if item is not None
                else _none()
                for item in evidence
            ))
        except (
            EvolutionPostRollbackRuntimeVerificationError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_post_rollback_unavailable",
                "Post-Rollback Runtime Verification source 暂不可用。",
            ) from exc
        if any(
            item is not None and not item.verification_authority
            for item in inspected
        ):
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_post_rollback_stale",
                "Post-Rollback Runtime Verification authority 当前无效。",
            )
        return tuple(inspected)

    async def _behavioral_matrix_views(
        self,
        outcomes: tuple[EvolutionRevalidationRollbackOutcome, ...],
    ) -> tuple[EvolutionPostRollbackBehavioralMatrixView | None, ...]:
        if self.behavioral_matrix_store is None or self.behavioral_matrix_service is None:
            return tuple(None for _ in outcomes)
        try:
            matrices = await asyncio.gather(*(
                self.behavioral_matrix_store.get_by_outcome(item.outcome_id)
                for item in outcomes
            ))
            inspected = await asyncio.gather(*(
                self.behavioral_matrix_service.inspect(matrix=item)
                if item is not None
                else _none()
                for item in matrices
            ))
        except (
            EvolutionPostRollbackBehavioralMatrixError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_behavioral_matrix_unavailable",
                "Post-Rollback Behavioral Matrix source 暂不可用。",
            ) from exc
        if any(
            item is not None and not item.behavioral_evaluation_authority
            for item in inspected
        ):
            raise EvolutionProposalOutcomeProjectionError(
                "proposal_outcome_behavioral_matrix_stale",
                "Post-Rollback Behavioral Matrix authority 当前无效。",
            )
        return tuple(inspected)


def _project(
    outcome: EvolutionRevalidationRollbackOutcome,
    view: EvolutionRevalidationRollbackOutcomeView,
    evidence_view: EvolutionProposalBeforeAfterEvidenceView | None = None,
    verification_view: EvolutionPostRollbackRuntimeVerificationView | None = None,
    matrix_view: EvolutionPostRollbackBehavioralMatrixView | None = None,
) -> EvolutionProposalOutcomeProjection:
    if outcome != view.outcome:
        raise EvolutionProposalOutcomeProjectionError(
            "proposal_outcome_source_mismatch",
            "Proposal Outcome 与 current view 不一致。",
        )
    evidence = evidence_view.evidence if evidence_view is not None else None
    verification = (
        verification_view.verification if verification_view is not None else None
    )
    matrix = matrix_view.matrix if matrix_view is not None else None
    return EvolutionProposalOutcomeProjection(
        workbench_session_id=outcome.workbench_session_id,
        workbench_proposal_id=outcome.workbench_proposal_id,
        governance_state_unchanged=True,
        status="rolled_back",
        outcome_id=outcome.outcome_id,
        outcome_sha256=outcome.outcome_sha256,
        rollback_receipt_id=outcome.rollback_receipt_id,
        experiment_contract_id=outcome.experiment_contract_id,
        candidate_id=outcome.candidate_id,
        candidate_revision=outcome.candidate_revision,
        breach_reasons=outcome.breach_reasons,
        recorded_at=outcome.recorded_at,
        authority_valid=view.outcome_authority,
        active_baseline=view.active_baseline_authority,
        contract_issue_allowed=False,
        before_after_evidence=evidence,
        before_after_recorded=evidence is not None,
        post_rollback_verification=verification,
        post_rollback_verification_recorded=verification is not None,
        post_rollback_evaluation_recorded=verification is not None,
        post_rollback_behavioral_matrix=matrix,
        post_rollback_behavioral_evaluation_recorded=matrix is not None,
        long_term_metrics_recorded=False,
        promoted=False,
        learning_authority=False,
        promotion_authority=False,
    )


async def _none() -> None:
    return None


__all__ = [
    "EVOLUTION_PROPOSAL_OUTCOME_PROJECTION_POLICY",
    "EvolutionProposalOutcomeProjection",
    "EvolutionProposalOutcomeProjectionError",
    "EvolutionProposalOutcomeProjectionService",
]
