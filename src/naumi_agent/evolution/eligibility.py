"""Deterministic, non-executing Candidate eligibility policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from naumi_agent.evolution.candidate import EvolutionCandidateDraft

EligibilityDecision = Literal["blocked", "needs_evidence", "review_ready"]

_MECHANICAL_SOURCES = frozenset({"harness_failure", "self_review_static"})
_FEEDBACK_SOURCES = frozenset({"user_feedback", "agent_interpreted_feedback"})
_PROTECTED_PREFIXES = (
    "src/naumi_agent/safety/",
    "src/naumi_agent/config/credentials",
    "src/naumi_agent/persistence/migrations",
    "src/naumi_agent/update/",
    "safety:",
    "permissions:",
    "secret_storage:",
    "migrations:",
    "updater:",
)


@dataclass(frozen=True, slots=True)
class EligibilityCheck:
    code: str
    passed: bool
    hard_block: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CandidateEligibilityAssessment:
    policy_version: str
    decision: EligibilityDecision
    review_ready: bool
    experiment_eligible: bool
    human_review_required: bool
    checks: tuple[EligibilityCheck, ...]


def assess_candidate_eligibility(
    candidate: EvolutionCandidateDraft,
) -> CandidateEligibilityAssessment:
    """Assess proposal readiness without granting experiment authority."""
    if not isinstance(candidate, EvolutionCandidateDraft):
        raise TypeError("eligibility 只能评估 EvolutionCandidateDraft。")

    protected = _is_protected_scope(candidate.scope)
    mechanical = bool(set(candidate.source_kinds) & _MECHANICAL_SOURCES)
    direct_feedback = "user_feedback" in candidate.source_kinds
    feedback_only = set(candidate.source_kinds).issubset(_FEEDBACK_SOURCES)
    repeated_feedback = feedback_only and direct_feedback and candidate.occurrence_count >= 2
    evidence_ready = mechanical or repeated_feedback
    verifier_ready = bool(candidate.expected_metrics) and all(
        metric.verifier in {
            "harness_replay",
            "self_review_static",
            "feedback_recurrence",
        }
        for metric in candidate.expected_metrics
    )

    checks = (
        EligibilityCheck(
            code="protected_scope",
            passed=not protected,
            hard_block=True,
            detail=(
                "scope 命中受保护模块，必须由人工治理且不可自动实验。"
                if protected
                else "scope 未命中 v1 受保护模块清单。"
            ),
        ),
        EligibilityCheck(
            code="evidence_strength",
            passed=evidence_ready,
            hard_block=False,
            detail=_evidence_detail(
                mechanical=mechanical,
                direct_feedback=direct_feedback,
                occurrence_count=candidate.occurrence_count,
            ),
        ),
        EligibilityCheck(
            code="mechanical_verifier",
            passed=verifier_ready,
            hard_block=True,
            detail=(
                "所有预期指标均有受支持的机械 verifier。"
                if verifier_ready
                else "缺少受支持的机械 verifier。"
            ),
        ),
        EligibilityCheck(
            code="cooldown_gate",
            passed=False,
            hard_block=False,
            detail="reject/defer 冷却记录尚未实现，因此不能授予实验资格。",
        ),
        EligibilityCheck(
            code="experiment_contract",
            passed=False,
            hard_block=False,
            detail="隔离 worktree、预算和允许工具契约尚未签发。",
        ),
    )
    if protected or not verifier_ready:
        decision: EligibilityDecision = "blocked"
    elif not evidence_ready:
        decision = "needs_evidence"
    else:
        decision = "review_ready"
    return CandidateEligibilityAssessment(
        policy_version="candidate-eligibility-v1",
        decision=decision,
        review_ready=decision == "review_ready",
        experiment_eligible=False,
        human_review_required=protected or candidate.risk.level in {"high", "critical"},
        checks=checks,
    )


def _is_protected_scope(scope: str) -> bool:
    normalized = scope.strip().replace("\\", "/").casefold()
    return any(normalized.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def _evidence_detail(
    *,
    mechanical: bool,
    direct_feedback: bool,
    occurrence_count: int,
) -> str:
    if mechanical:
        return "包含 Harness 或静态扫描机械证据。"
    if direct_feedback and occurrence_count >= 2:
        return "直接用户反馈已至少出现 2 次，可进入人工审阅。"
    if direct_feedback:
        return "直接用户反馈仅出现 1 次；需要复现或机械证据。"
    return "仅有 Agent 解释反馈；需要直接用户反馈或机械证据。"


__all__ = [
    "CandidateEligibilityAssessment",
    "EligibilityCheck",
    "assess_candidate_eligibility",
]
