"""Deterministic, non-executable Proposal previews for review-ready Candidates."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.aggregation import aggregate_candidate
from naumi_agent.evolution.eligibility import assess_candidate_eligibility
from naumi_agent.evolution.store import EvolutionStoredCandidate

ProposalKind = Literal["knowledge", "profile", "prompt", "tool", "test", "code"]

_GENERATOR_VERSION = "evolution-proposal-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID_RE = re.compile(r"^evc_[0-9a-f]{24}$")
_PROPOSAL_ID_RE = re.compile(r"^evp_[0-9a-f]{24}$")
_FINDING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ABSOLUTE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_PATH_PREFIXES = ("src/", "tests/", "docs/", "config/", "frontend/", ".github/")

_SCOPE_PREFIX_KIND: dict[str, ProposalKind] = {
    "knowledge": "knowledge",
    "profile": "profile",
    "provider": "profile",
    "model": "profile",
    "prompt": "prompt",
    "instruction": "prompt",
    "system_prompt": "prompt",
    "tool": "tool",
    "mcp": "tool",
    "browser": "tool",
    "test": "test",
    "eval": "test",
    "verification": "test",
}
_FINDING_KIND: dict[str, ProposalKind] = {
    "knowledge_gap": "knowledge",
    "outdated_knowledge": "knowledge",
    "missing_knowledge": "knowledge",
    "environment_error": "profile",
    "model_capability_mismatch": "profile",
    "provider_contract_error": "profile",
    "agent_premature_finish": "prompt",
    "agent_repetition": "prompt",
    "user_correction": "prompt",
    "tool_contract_error": "tool",
    "permission_block": "tool",
    "verification_failure": "test",
    "flaky_test": "test",
    "missing_test": "test",
}
_KIND_LABELS: dict[ProposalKind, str] = {
    "knowledge": "知识",
    "profile": "模型配置",
    "prompt": "指令",
    "tool": "工具",
    "test": "测试",
    "code": "代码",
}
_VERIFIER_PROCEDURES = {
    "harness_replay": "使用同一 Harness 输入安全回放，并比较失败分类与验收条件。",
    "self_review_static": "对同一 scope 重新运行 Self-Review 静态扫描并比较 finding 数量。",
    "feedback_recurrence": "在后续观察窗口比较同根用户反馈复发率，不把沉默视为自动通过。",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ProposalSourceSnapshot(_StrictModel):
    candidate_id: str
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str
    occurrence_count: int = Field(ge=1, le=10_000)
    last_observed_at: str
    aggregation_policy: Literal["candidate-aggregation-v1"]
    trend: Literal["new", "increasing", "stable", "decreasing", "insufficient"]

    @field_validator("candidate_sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("candidate_sha256 必须是 SHA-256。")
        return value

    @field_validator("candidate_id")
    @classmethod
    def _valid_candidate_id(cls, value: str) -> str:
        if not _CANDIDATE_ID_RE.fullmatch(value):
            raise ValueError("candidate_id 格式无效。")
        return value


class ProposalValidationStep(_StrictModel):
    metric_name: str = Field(min_length=1, max_length=128)
    direction: Literal["decrease", "increase"]
    target: float
    verifier: Literal[
        "harness_replay",
        "self_review_static",
        "feedback_recurrence",
    ]
    procedure: str = Field(min_length=1, max_length=1_000)


class EvolutionProposalPreview(_StrictModel):
    schema_version: Literal[1] = 1
    proposal_id: str
    generator_version: Literal["evolution-proposal-v1"] = _GENERATOR_VERSION
    proposal_kind: ProposalKind
    classification_reason: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2_000)
    impact_scope: str = Field(min_length=1, max_length=1_024)
    intended_files: tuple[str, ...] = Field(max_length=16)
    validation_plan: tuple[ProposalValidationStep, ...] = Field(
        min_length=1,
        max_length=8,
    )
    risk_level: Literal["low", "medium", "high", "critical"]
    review_notes: tuple[str, ...] = Field(min_length=1, max_length=8)
    source: ProposalSourceSnapshot
    requires_human_review: Literal[True] = True
    executable: Literal[False] = False
    experiment_eligible: Literal[False] = False
    state: Literal["preview"] = "preview"

    @model_validator(mode="after")
    def _identity_is_stable(self) -> EvolutionProposalPreview:
        if not _PROPOSAL_ID_RE.fullmatch(self.proposal_id):
            raise ValueError("proposal_id 格式无效。")
        expected = _proposal_id(
            candidate_id=self.source.candidate_id,
            revision=self.source.candidate_revision,
            candidate_sha256=self.source.candidate_sha256,
            proposal_kind=self.proposal_kind,
        )
        if self.proposal_id != expected:
            raise ValueError("proposal_id 与 Candidate source snapshot 不一致。")
        if len({step.metric_name for step in self.validation_plan}) != len(
            self.validation_plan
        ):
            raise ValueError("Proposal validation metric 不得重复。")
        return self

    @field_validator("intended_files")
    @classmethod
    def _safe_relative_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().replace("\\", "/") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("intended_files 不得重复。")
        for value in normalized:
            parts = value.split("/")
            if (
                not value
                or len(value) > 1_024
                or _ABSOLUTE_PATH_RE.match(value)
                or ".." in parts
                or any(char in value for char in ("\x00", "\r", "\n"))
            ):
                raise ValueError("intended_files 必须是安全的相对路径。")
        return normalized


def generate_proposal_preview(
    stored: EvolutionStoredCandidate,
) -> EvolutionProposalPreview | None:
    """Build a review preview, or ``None`` when policy disallows proposal entry."""
    if not isinstance(stored, EvolutionStoredCandidate):
        raise TypeError("Proposal generator 只能处理 EvolutionStoredCandidate。")
    candidate = stored.draft
    candidate_sha256 = _candidate_sha256(candidate.model_dump(mode="json"))
    if stored.revision < 1 or stored.draft_sha256 != candidate_sha256:
        raise ValueError("Stored Candidate revision 或摘要不可信。")
    assessment = assess_candidate_eligibility(candidate)
    if not assessment.review_ready:
        return None
    aggregation = aggregate_candidate(candidate)
    proposal_kind, classification_reason = classify_proposal_kind(
        candidate.finding_code,
        candidate.scope,
    )
    source = ProposalSourceSnapshot(
        candidate_id=candidate.candidate_id,
        candidate_revision=stored.revision,
        candidate_sha256=stored.draft_sha256,
        occurrence_count=candidate.occurrence_count,
        last_observed_at=candidate.last_observed_at,
        aggregation_policy=aggregation.policy_version,
        trend=aggregation.trend,
    )
    return EvolutionProposalPreview(
        proposal_id=_proposal_id(
            candidate_id=candidate.candidate_id,
            revision=stored.revision,
            candidate_sha256=stored.draft_sha256,
            proposal_kind=proposal_kind,
        ),
        proposal_kind=proposal_kind,
        classification_reason=classification_reason,
        title=f"{_KIND_LABELS[proposal_kind]}改进建议：{candidate.finding_code}",
        summary=candidate.hypothesis.text,
        impact_scope=candidate.scope,
        intended_files=_intended_files(candidate.scope),
        validation_plan=tuple(
            ProposalValidationStep(
                metric_name=metric.name,
                direction=metric.direction,
                target=metric.target,
                verifier=metric.verifier,
                procedure=_VERIFIER_PROCEDURES[metric.verifier],
            )
            for metric in candidate.expected_metrics
        ),
        risk_level=candidate.risk.level,
        review_notes=(
            f"eligibility:{assessment.decision}",
            f"evidence_count:{candidate.occurrence_count}",
            f"trend:{aggregation.trend}",
            "尚未进入 Workbench Review Queue",
        ),
        source=source,
    )


def classify_proposal_kind(finding_code: str, scope: str) -> tuple[ProposalKind, str]:
    """Classify a Candidate into exactly one of HAR-09.4's six proposal types."""
    normalized_finding = str(finding_code).strip().casefold()
    normalized_scope = str(scope).strip().replace("\\", "/").casefold()
    if not normalized_finding or not normalized_scope:
        raise ValueError("finding_code 与 scope 不能为空。")
    if not _FINDING_RE.fullmatch(normalized_finding):
        raise ValueError("finding_code 格式无效。")
    scope_parts = re.split(r"[/:]", normalized_scope)
    if (
        _ABSOLUTE_PATH_RE.match(normalized_scope)
        or ".." in scope_parts
        or any(char in normalized_scope for char in ("\x00", "\r", "\n"))
    ):
        raise ValueError("scope 必须是安全的相对范围。")
    scope_prefix = normalized_scope.split(":", 1)[0]
    if scope_prefix in _SCOPE_PREFIX_KIND:
        return _SCOPE_PREFIX_KIND[scope_prefix], f"scope_prefix:{scope_prefix}"
    path_rules: tuple[tuple[str, ProposalKind], ...] = (
        ("/knowledge/", "knowledge"),
        ("/profiles/", "profile"),
        ("/providers/", "profile"),
        ("/prompts/", "prompt"),
        ("/tools/", "tool"),
        ("/mcp/", "tool"),
        ("/browser/", "tool"),
    )
    if normalized_scope.startswith("tests/"):
        return "test", "scope_path:tests"
    for marker, proposal_kind in path_rules:
        if marker in f"/{normalized_scope}":
            return proposal_kind, f"scope_path:{marker.strip('/')}"
    if normalized_finding in _FINDING_KIND:
        return _FINDING_KIND[normalized_finding], f"finding:{normalized_finding}"
    return "code", "fallback:code"


def _intended_files(scope: str) -> tuple[str, ...]:
    normalized = scope.strip().replace("\\", "/")
    candidate = normalized.split(":", 1)[0]
    if candidate.startswith(_PATH_PREFIXES) and not candidate.endswith("/"):
        return (candidate,)
    return ()


def _candidate_sha256(value: dict[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _proposal_id(
    *,
    candidate_id: str,
    revision: int,
    candidate_sha256: str,
    proposal_kind: ProposalKind,
) -> str:
    payload = json.dumps(
        {
            "candidate_id": candidate_id,
            "candidate_revision": revision,
            "candidate_sha256": candidate_sha256,
            "generator_version": _GENERATOR_VERSION,
            "proposal_kind": proposal_kind,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"evp_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


__all__ = [
    "EvolutionProposalPreview",
    "ProposalKind",
    "ProposalSourceSnapshot",
    "ProposalValidationStep",
    "classify_proposal_kind",
    "generate_proposal_preview",
]
