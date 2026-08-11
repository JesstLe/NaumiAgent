"""Deterministic reinjection of verified Outcomes into Evolution Candidates."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.evolution.candidate import (
    EvolutionCandidateDraft,
    build_candidate_draft,
)
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
    EvolutionRevalidationRollbackOutcomeService,
)
from naumi_agent.evolution.store import EvolutionCandidateStore

EVOLUTION_OUTCOME_OPPORTUNITY_POLICY = "evolution-outcome-opportunity-v1"
_OUTCOME_ID_RE = re.compile(r"^evrerollbackout_[0-9a-f]{24}$")
_OUTCOME_URI_RE = re.compile(
    r"^evolution-outcome://rollback/(evrerollbackout_[0-9a-f]{24})$"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionOutcomeOpportunityResult(_StrictModel):
    """Public receipt for one idempotent Outcome-to-Candidate reinjection."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-outcome-opportunity-v1"] = (
        EVOLUTION_OUTCOME_OPPORTUNITY_POLICY
    )
    status: Literal["recorded"] = "recorded"
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    occurrence_count: int = Field(ge=1, le=10_000)
    evidence_id: str = Field(pattern=r"^eve_[0-9a-f]{24}$")
    source_authority_valid: Literal[True] = True
    experiment_eligible: Literal[False] = False
    promotion_authority: Literal[False] = False


class EvolutionOutcomeOpportunityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionOutcomeOpportunityService:
    """Turn current, verified rollback facts into non-executable opportunities."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationRollbackOutcomeService,
        candidate_store: EvolutionCandidateStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.outcome_service = outcome_service
        self.candidate_store = candidate_store

    async def discover(self, *, outcome_id: str) -> EvolutionOutcomeOpportunityResult:
        normalized = str(outcome_id or "").strip()
        if _OUTCOME_ID_RE.fullmatch(normalized) is None:
            raise EvolutionOutcomeOpportunityError(
                "outcome_opportunity_id_invalid",
                "Rollback Outcome ID 格式无效。",
            )
        try:
            view = await self.outcome_service.inspect_outcome(outcome_id=normalized)
        except EvolutionRevalidationRollbackOutcomeError as exc:
            raise EvolutionOutcomeOpportunityError(
                "outcome_opportunity_source_unavailable",
                "Rollback Outcome 不存在、损坏或无法重验。",
            ) from exc
        if not view.outcome_authority:
            raise EvolutionOutcomeOpportunityError(
                "outcome_opportunity_source_stale",
                "Rollback Outcome authority 已失效，未生成下一轮机会。",
            )
        if Path(view.outcome.workspace_root).resolve() != self.workspace_root:
            raise EvolutionOutcomeOpportunityError(
                "outcome_opportunity_workspace_mismatch",
                "Rollback Outcome 不属于当前工作区。",
            )
        evidence = adapt_rollback_outcome_evidence(view.outcome)
        stored = await self.candidate_store.upsert_candidate(
            self.workspace_root,
            build_candidate_draft((evidence,)),
        )
        return EvolutionOutcomeOpportunityResult(
            outcome_id=view.outcome.outcome_id,
            outcome_sha256=view.outcome.outcome_sha256,
            candidate_id=stored.draft.candidate_id,
            candidate_revision=stored.revision,
            occurrence_count=stored.draft.occurrence_count,
            evidence_id=evidence.evidence_id,
        )

    async def validate_candidate_sources(
        self,
        candidate: EvolutionCandidateDraft,
    ) -> bool:
        """Revalidate every rollback source before review or queue projection."""
        for evidence in candidate.evidence:
            if evidence.source_kind != "rollback_outcome":
                continue
            match = _OUTCOME_URI_RE.fullmatch(evidence.source_uri)
            if match is None or len(evidence.refs) != 1:
                return False
            try:
                view = await self.outcome_service.inspect_outcome(
                    outcome_id=match.group(1)
                )
            except (EvolutionRevalidationRollbackOutcomeError, OSError, ValueError):
                return False
            if not (
                view.outcome_authority
                and view.outcome.workspace_root == str(self.workspace_root)
                and view.outcome.outcome_id == match.group(1)
                and view.outcome.outcome_sha256 == evidence.refs[0].sha256
            ):
                return False
        return True


def adapt_rollback_outcome_evidence(
    outcome: EvolutionRevalidationRollbackOutcome,
) -> EvolutionEvidence:
    """Build privacy-bounded evidence without copying patch or source payloads."""
    if not isinstance(outcome, EvolutionRevalidationRollbackOutcome):
        raise TypeError("Outcome opportunity 只能消费 Rollback Outcome。")
    finding_code = "rollback_guardrail_breach"
    scope = f"evolution:rollout:{outcome.proposal_kind}"
    root_fingerprint = _digest({
        "breach_reasons": list(outcome.breach_reasons),
        "finding_code": finding_code,
        "proposal_kind": outcome.proposal_kind,
        "scope": scope,
    })
    evidence_sha = _digest({
        "outcome_id": outcome.outcome_id,
        "outcome_sha256": outcome.outcome_sha256,
        "root_fingerprint": root_fingerprint,
    })
    ref = EvolutionEvidenceRef(
        uri=f"evolution-outcome://rollback/{outcome.outcome_id}",
        sha256=outcome.outcome_sha256,
    )
    return EvolutionEvidence(
        evidence_id=f"eve_{evidence_sha[:24]}",
        source_kind="rollback_outcome",
        source_uri=ref.uri,
        observed_at=outcome.recorded_at,
        finding_code=finding_code,
        scope=scope,
        root_fingerprint=root_fingerprint,
        refs=(ref,),
    )


def render_outcome_opportunity(result: EvolutionOutcomeOpportunityResult) -> str:
    return "\n".join([
        "# Outcome 已回注下一轮机会发现",
        "",
        f"- Outcome：`{result.outcome_id}`",
        f"- Candidate：`{result.candidate_id}` · revision {result.candidate_revision}",
        f"- 唯一证据：{result.occurrence_count}",
        f"- Evidence：`{result.evidence_id}`",
        "- 来源 authority：已实时重验",
        "- 状态：不可执行；未授予实验或推广权限",
        "",
        f"下一步：`/evolution detail {result.candidate_id}`",
    ])


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "EVOLUTION_OUTCOME_OPPORTUNITY_POLICY",
    "EvolutionOutcomeOpportunityError",
    "EvolutionOutcomeOpportunityResult",
    "EvolutionOutcomeOpportunityService",
    "adapt_rollback_outcome_evidence",
    "render_outcome_opportunity",
]
