"""Read-only Workbench projection of governed Evolution outcomes."""

from __future__ import annotations

import asyncio
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    before_after_recorded: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    promoted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        if self.active_baseline and not self.authority_valid:
            raise ValueError("Proposal Outcome active baseline 超出 authority。")
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
    ) -> None:
        self.rollback_outcome_store = rollback_outcome_store
        self.rollback_outcome_service = rollback_outcome_service

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
        projections: dict[str, EvolutionProposalOutcomeProjection] = {}
        for (proposal_id, outcome), result in zip(
            by_proposal.items(), views, strict=True
        ):
            if isinstance(result, BaseException):
                raise EvolutionProposalOutcomeProjectionError(
                    "proposal_outcome_source_unavailable",
                    "Proposal Outcome source 暂不可用。",
                ) from result
            projections[proposal_id] = _project(outcome, result)
        return projections


def _project(
    outcome: EvolutionRevalidationRollbackOutcome,
    view: EvolutionRevalidationRollbackOutcomeView,
) -> EvolutionProposalOutcomeProjection:
    if outcome != view.outcome:
        raise EvolutionProposalOutcomeProjectionError(
            "proposal_outcome_source_mismatch",
            "Proposal Outcome 与 current view 不一致。",
        )
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
        before_after_recorded=False,
        long_term_metrics_recorded=False,
        promoted=False,
        learning_authority=False,
        promotion_authority=False,
    )


__all__ = [
    "EVOLUTION_PROPOSAL_OUTCOME_PROJECTION_POLICY",
    "EvolutionProposalOutcomeProjection",
    "EvolutionProposalOutcomeProjectionError",
    "EvolutionProposalOutcomeProjectionService",
]
