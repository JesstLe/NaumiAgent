"""Deterministic governance and cooldown policy for Workbench proposals."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from naumi_agent.workbench.models import (
    ProposalSourceKind,
    ProposalState,
    RiskLevel,
    WorkbenchProposal,
)

GOVERNANCE_POLICY_VERSION = "proposal-governance-v1"
REJECT_COOLDOWN = timedelta(days=30)
MIN_DEFER = timedelta(hours=1)
MAX_DEFER = timedelta(days=90)


class ProposalAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    DEFER = "defer"
    MERGE = "merge"
    REOPEN = "reopen"


class ProposalGovernanceConflictError(ValueError):
    """The proposal was already decided through another terminal transition."""


@dataclass(frozen=True, slots=True)
class ProposalTransitionPlan:
    action: ProposalAction
    target_state: ProposalState
    decision_at: str
    cooldown_until: str = ""
    merged_into_id: str = ""
    policy_version: str = GOVERNANCE_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class ProposalCooldownDecision:
    allowed: bool
    reason: str
    cooldown_until: str = ""
    significant_new_evidence: bool = False
    policy_version: str = GOVERNANCE_POLICY_VERSION


def plan_proposal_transition(
    proposal: WorkbenchProposal,
    *,
    action: ProposalAction,
    now: datetime,
    decision_note: str = "",
    defer_until: str = "",
    merge_into_id: str = "",
) -> ProposalTransitionPlan:
    """Validate one state transition without persistence side effects."""
    current = proposal.state
    instant = _aware_utc(now)
    note = decision_note.strip()
    if action is ProposalAction.REOPEN:
        if current not in {ProposalState.REJECTED, ProposalState.DEFERRED}:
            raise ValueError("只有 rejected/deferred Proposal 可以重新入队。")
        return ProposalTransitionPlan(
            action=action,
            target_state=ProposalState.OPEN,
            decision_at=_iso(instant),
        )
    if current is not ProposalState.OPEN:
        raise ProposalGovernanceConflictError(
            f"Proposal 已处于 {current.value}，不能执行 {action.value}。"
        )
    if action is ProposalAction.APPROVE:
        return ProposalTransitionPlan(action, ProposalState.APPROVED, _iso(instant))
    if action is ProposalAction.REJECT:
        if proposal.source_kind is ProposalSourceKind.EVOLUTION_CANDIDATE and not note:
            raise ValueError("拒绝 Evolution Proposal 时必须填写原因。")
        return ProposalTransitionPlan(
            action,
            ProposalState.REJECTED,
            _iso(instant),
            cooldown_until=_iso(instant + REJECT_COOLDOWN),
        )
    if action is ProposalAction.DEFER:
        if not note:
            raise ValueError("延后 Proposal 时必须填写原因。")
        target = _parse_timestamp(defer_until, "defer_until")
        delay = target - instant
        if delay < MIN_DEFER or delay > MAX_DEFER:
            raise ValueError("defer_until 必须位于当前时间后 1 小时至 90 天。")
        return ProposalTransitionPlan(
            action,
            ProposalState.DEFERRED,
            _iso(instant),
            cooldown_until=_iso(target),
        )
    if action is ProposalAction.MERGE:
        target_id = merge_into_id.strip()
        if not target_id or target_id == proposal.id:
            raise ValueError("merge 必须指定另一个 Proposal。")
        return ProposalTransitionPlan(
            action,
            ProposalState.MERGED,
            _iso(instant),
            merged_into_id=target_id,
        )
    raise ValueError("Proposal action 未注册。")


def evaluate_proposal_cooldown(
    previous: WorkbenchProposal | None,
    *,
    candidate_revision: int,
    occurrence_count: int,
    risk_level: RiskLevel,
    now: datetime,
) -> ProposalCooldownDecision:
    """Decide whether a Candidate revision may enter review after reject/defer."""
    if previous is None or previous.state not in {
        ProposalState.REJECTED,
        ProposalState.DEFERRED,
    }:
        return ProposalCooldownDecision(True, "no_active_cooldown")
    instant = _aware_utc(now)
    if not previous.cooldown_until.strip():
        return ProposalCooldownDecision(False, "cooldown_record_missing")
    until = _parse_timestamp(previous.cooldown_until, "cooldown_until")
    if instant >= until:
        return ProposalCooldownDecision(True, "cooldown_expired", _iso(until))
    significant = _has_significant_new_evidence(
        previous,
        candidate_revision=candidate_revision,
        occurrence_count=occurrence_count,
        risk_level=risk_level,
    )
    if significant:
        return ProposalCooldownDecision(
            True,
            "significant_new_evidence",
            _iso(until),
            significant_new_evidence=True,
        )
    return ProposalCooldownDecision(False, "cooldown_active", _iso(until))


def validate_merge_target(source: WorkbenchProposal, target: WorkbenchProposal) -> None:
    """Ensure merge consolidates revisions of the same Evolution Candidate."""
    valid = (
        source.id != target.id
        and source.session_id == target.session_id
        and source.source_kind is ProposalSourceKind.EVOLUTION_CANDIDATE
        and target.source_kind is ProposalSourceKind.EVOLUTION_CANDIDATE
        and source.source_id == target.source_id
        and target.source_revision > source.source_revision
        and target.state is ProposalState.OPEN
    )
    if not valid:
        raise ValueError("merge 目标必须是同一 Candidate 的较新 revision open Proposal。")


def _has_significant_new_evidence(
    previous: WorkbenchProposal,
    *,
    candidate_revision: int,
    occurrence_count: int,
    risk_level: RiskLevel,
) -> bool:
    if candidate_revision <= previous.source_revision:
        return False
    risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
    if risk_order[risk_level] > risk_order[previous.risk_level]:
        return True
    baseline = previous.source_occurrence_count
    if baseline < 1:
        return False
    required_growth = max(2, math.ceil(baseline * 0.5))
    return occurrence_count >= baseline + required_growth


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("治理时间必须包含 UTC offset。")
    return value.astimezone(UTC)


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是带 UTC offset 的 ISO 时间。") from exc
    return _aware_utc(parsed)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


__all__ = [
    "GOVERNANCE_POLICY_VERSION",
    "ProposalAction",
    "ProposalCooldownDecision",
    "ProposalGovernanceConflictError",
    "ProposalTransitionPlan",
    "evaluate_proposal_cooldown",
    "plan_proposal_transition",
    "validate_merge_target",
]
