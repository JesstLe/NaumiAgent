"""Deterministic, non-executable Evolution Candidate drafts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.evidence import EvolutionEvidence

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID_RE = re.compile(r"^evc_[0-9a-f]{24}$")
_FINDING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_METRIC_RE = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_ABSOLUTE_SCOPE_RE = re.compile(r"(?:^|:)(?:/|[A-Za-z]:[\\/])")
_SENSITIVE_HYPOTHESIS_RE = re.compile(
    r"(?:\b(?:api[_-]?key|password|secret|token|authorization|cookie)\b\s*[:=]\s*\S+)"
    r"|(?:\bbearer\s+\S+)"
    r"|(?:\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_ABSOLUTE_TEXT_PATH_RE = re.compile(
    r"(?:^|[\s`(])(?:/(?:Users|home|tmp|var)/\S+|[A-Za-z]:[\\/]\S+)",
)

CandidateKind = Literal["correctness", "maintainability", "reliability", "safety"]
CandidateRiskLevel = Literal["low", "medium", "high", "critical"]

_MAINTAINABILITY_FINDINGS = frozenset(
    {"broad_except", "long_function", "mutable_global", "untyped_public_return"}
)
_RELIABILITY_FINDINGS = frozenset(
    {"agent_premature_finish", "agent_repetition", "environment_error"}
)
_SAFETY_FINDINGS = frozenset(
    {"hardcoded_secret", "permission_block", "scope_invalid", "scope_prohibited"}
)
_HIGH_RISK_FINDINGS = frozenset({"hardcoded_secret", "scope_invalid", "scope_prohibited"})
_LOW_RISK_FINDINGS = frozenset(
    {"broad_except", "long_function", "mutable_global", "untyped_public_return"}
)
_FINDING_LABELS = MappingProxyType({
    "agent_premature_finish": "过早结束",
    "agent_repetition": "重复无进展执行",
    "bare_except": "裸异常捕获",
    "broad_except": "宽泛异常捕获",
    "environment_error": "环境失败",
    "hardcoded_secret": "硬编码敏感信息",
    "long_function": "超长函数",
    "mutable_global": "模块级可变状态",
    "permission_block": "权限阻断",
    "syntax_error": "语法错误",
    "tool_contract_error": "工具契约错误",
    "untyped_public_return": "公开函数返回类型缺失",
    "verification_failure": "机械验证失败",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CandidateHypothesis(_StrictModel):
    origin: Literal["deterministic_template", "llm"]
    text: str = Field(min_length=1, max_length=2_000)

    @field_validator("text")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        normalized = value.strip()
        if "\x00" in normalized:
            raise ValueError("hypothesis 含非法控制字符。")
        if _SENSITIVE_HYPOTHESIS_RE.search(normalized):
            raise ValueError("hypothesis 疑似包含未脱敏凭据。")
        if _ABSOLUTE_TEXT_PATH_RE.search(normalized):
            raise ValueError("hypothesis 不得保存本机绝对路径。")
        return normalized


class CandidateRisk(_StrictModel):
    level: CandidateRiskLevel
    reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    policy_version: Literal["candidate-draft-v1"] = "candidate-draft-v1"

    @field_validator("reasons")
    @classmethod
    def _unique_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("risk reasons 必须非空且唯一。")
        return normalized


class CandidateExpectedMetric(_StrictModel):
    name: str
    direction: Literal["decrease", "increase"]
    target: float
    verifier: Literal["harness_replay", "self_review_static"]

    @model_validator(mode="after")
    def _metric_is_mechanical(self) -> CandidateExpectedMetric:
        if not _METRIC_RE.fullmatch(self.name):
            raise ValueError("expected metric 名称无效。")
        if not math.isfinite(self.target):
            raise ValueError("expected metric target 必须是有限值。")
        return self


class EvolutionCandidateDraft(_StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str
    fingerprint: str
    finding_code: str
    kind: CandidateKind
    scope: str = Field(min_length=1, max_length=1_024)
    hypothesis: CandidateHypothesis
    risk: CandidateRisk
    expected_metrics: tuple[CandidateExpectedMetric, ...] = Field(
        min_length=1,
        max_length=8,
    )
    evidence: tuple[EvolutionEvidence, ...] = Field(min_length=1, max_length=10_000)
    occurrence_count: int = Field(ge=1, le=10_000)
    first_observed_at: str
    last_observed_at: str
    source_kinds: tuple[str, ...] = Field(min_length=1, max_length=16)
    status: Literal["draft"] = "draft"
    experiment_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _candidate_is_consistent(self) -> EvolutionCandidateDraft:
        _validate_candidate_identity(self)
        _validate_candidate_evidence(self)
        _validate_candidate_policy(self)
        _validate_candidate_window(self)
        return self


def _validate_candidate_identity(candidate: EvolutionCandidateDraft) -> None:
    if not _CANDIDATE_ID_RE.fullmatch(candidate.candidate_id):
        raise ValueError("candidate_id 格式无效。")
    if not _SHA256_RE.fullmatch(candidate.fingerprint):
        raise ValueError("candidate fingerprint 必须是 SHA-256。")
    if candidate.candidate_id != f"evc_{candidate.fingerprint[:24]}":
        raise ValueError("candidate_id 必须由 fingerprint 确定性生成。")
    if not _FINDING_RE.fullmatch(candidate.finding_code):
        raise ValueError("candidate finding_code 格式无效。")
    scope_parts = re.split(r"[\\/:]", candidate.scope)
    invalid_scope = _ABSOLUTE_SCOPE_RE.search(candidate.scope) or ".." in scope_parts
    has_control = any(
        character in candidate.scope for character in ("\n", "\r", "\x00")
    )
    if invalid_scope or has_control:
        raise ValueError("candidate scope 必须是无控制字符的相对 scope。")


def _validate_candidate_evidence(candidate: EvolutionCandidateDraft) -> None:
    if candidate.occurrence_count != len(candidate.evidence):
        raise ValueError("occurrence_count 必须等于唯一 Evidence 数量。")
    if len({item.evidence_id for item in candidate.evidence}) != len(
        candidate.evidence
    ):
        raise ValueError("Candidate Evidence 不得重复。")
    ordered = tuple(
        sorted(
            candidate.evidence,
            key=lambda item: (_parse_timestamp(item.observed_at), item.evidence_id),
        )
    )
    if candidate.evidence != ordered:
        raise ValueError("Candidate Evidence 必须按观察时间稳定排序。")
    if not any(item.hard_evidence for item in candidate.evidence):
        raise ValueError("Candidate 至少需要一个 hard evidence。")
    roots = {item.root_fingerprint for item in candidate.evidence}
    findings = {item.finding_code for item in candidate.evidence}
    scopes = {item.scope for item in candidate.evidence}
    if len(roots) != 1 or findings != {candidate.finding_code} or scopes != {candidate.scope}:
        raise ValueError("Candidate Evidence 必须与候选根因、finding 和 scope 一致。")
    expected = _digest(
        {
            "finding_code": candidate.finding_code,
            "root_fingerprint": next(iter(roots)),
            "scope": candidate.scope,
        }
    )
    if candidate.fingerprint != expected:
        raise ValueError("candidate fingerprint 与 Evidence 不一致。")


def _validate_candidate_policy(candidate: EvolutionCandidateDraft) -> None:
    expected_sources = tuple(
        sorted({item.source_kind for item in candidate.evidence})
    )
    if candidate.source_kinds != expected_sources:
        raise ValueError("source_kinds 必须匹配 Evidence 且稳定排序。")
    if candidate.kind != _candidate_kind(candidate.finding_code):
        raise ValueError("candidate kind 与 finding policy 不一致。")
    if candidate.risk != _risk(candidate.finding_code):
        raise ValueError("candidate risk 与 draft policy 不一致。")
    if len({metric.name for metric in candidate.expected_metrics}) != len(
        candidate.expected_metrics
    ):
        raise ValueError("expected metrics 不得重复。")
    required_metrics = set(_expected_metrics(candidate.evidence))
    if not required_metrics.issubset(set(candidate.expected_metrics)):
        raise ValueError("Candidate 缺少来源对应的机械指标。")


def _validate_candidate_window(candidate: EvolutionCandidateDraft) -> None:
    first = _parse_timestamp(candidate.first_observed_at)
    last = _parse_timestamp(candidate.last_observed_at)
    if first > last:
        raise ValueError("Candidate observation 时间窗口无效。")
    observed = tuple(_parse_timestamp(item.observed_at) for item in candidate.evidence)
    if first != min(observed) or last != max(observed):
        raise ValueError("Candidate observation 时间窗口必须来自 Evidence。")


def build_candidate_draft(
    evidence: Iterable[EvolutionEvidence],
) -> EvolutionCandidateDraft:
    """Build one stable draft from observations of exactly one mechanical root."""
    unique = _unique_evidence(evidence)
    if not unique:
        raise ValueError("Candidate 至少需要一个 Evidence。")
    if len(unique) > 10_000:
        raise ValueError("单个 Candidate 最多包含 10000 个 Evidence。")
    roots = {item.root_fingerprint for item in unique}
    finding_codes = {item.finding_code for item in unique}
    scopes = {item.scope for item in unique}
    if len(roots) != 1 or len(finding_codes) != 1 or len(scopes) != 1:
        raise ValueError("Candidate 只能聚合同根、同 finding、同 scope 的 Evidence。")
    ordered = tuple(
        sorted(
            unique,
            key=lambda item: (_parse_timestamp(item.observed_at), item.evidence_id),
        )
    )
    root = next(iter(roots))
    finding_code = next(iter(finding_codes))
    scope = next(iter(scopes))
    fingerprint = _digest(
        {"finding_code": finding_code, "root_fingerprint": root, "scope": scope}
    )
    source_kinds = tuple(sorted({item.source_kind for item in ordered}))
    kind = _candidate_kind(finding_code)
    return EvolutionCandidateDraft(
        candidate_id=f"evc_{fingerprint[:24]}",
        fingerprint=fingerprint,
        finding_code=finding_code,
        kind=kind,
        scope=scope,
        hypothesis=CandidateHypothesis(
            origin="deterministic_template",
            text=_hypothesis(kind, finding_code, scope),
        ),
        risk=_risk(finding_code),
        expected_metrics=_expected_metrics(ordered),
        evidence=ordered,
        occurrence_count=len(ordered),
        first_observed_at=ordered[0].observed_at,
        last_observed_at=ordered[-1].observed_at,
        source_kinds=source_kinds,
    )


def _unique_evidence(evidence: Iterable[EvolutionEvidence]) -> tuple[EvolutionEvidence, ...]:
    unique: dict[str, EvolutionEvidence] = {}
    for position, item in enumerate(evidence, start=1):
        if position > 20_000:
            raise ValueError("Candidate 输入最多接受 20000 次 Evidence 投递。")
        if not isinstance(item, EvolutionEvidence):
            raise TypeError("Candidate evidence 必须是 EvolutionEvidence。")
        existing = unique.get(item.evidence_id)
        if existing is not None and existing != item:
            raise ValueError(f"Evidence id 存在冲突内容：{item.evidence_id}")
        unique.setdefault(item.evidence_id, item)
        if len(unique) > 10_000:
            raise ValueError("单个 Candidate 最多包含 10000 个唯一 Evidence。")
    return tuple(unique.values())


def _candidate_kind(finding_code: str) -> CandidateKind:
    if finding_code in _SAFETY_FINDINGS:
        return "safety"
    if finding_code in _RELIABILITY_FINDINGS:
        return "reliability"
    if finding_code in _MAINTAINABILITY_FINDINGS:
        return "maintainability"
    return "correctness"


def _risk(finding_code: str) -> CandidateRisk:
    if finding_code in _HIGH_RISK_FINDINGS:
        level: CandidateRiskLevel = "high"
    elif finding_code in _LOW_RISK_FINDINGS:
        level = "low"
    else:
        level = "medium"
    return CandidateRisk(
        level=level,
        reasons=(f"finding:{finding_code}", "未经过 eligibility 与 protected-scope gate"),
    )


def _expected_metrics(
    evidence: Iterable[EvolutionEvidence],
) -> tuple[CandidateExpectedMetric, ...]:
    by_source: dict[str, CandidateExpectedMetric] = {}
    for item in evidence:
        if item.source_kind == "self_review_static":
            metric = CandidateExpectedMetric(
                name=f"self_review.{item.finding_code}.count",
                direction="decrease",
                target=0,
                verifier="self_review_static",
            )
        else:
            metric = CandidateExpectedMetric(
                name=f"harness.{item.finding_code}.rate",
                direction="decrease",
                target=0,
                verifier="harness_replay",
            )
        by_source[item.source_kind] = metric
    return tuple(by_source[source] for source in sorted(by_source))


def _hypothesis(kind: CandidateKind, finding_code: str, scope: str) -> str:
    label = _FINDING_LABELS.get(finding_code, finding_code)
    return f"在相对 scope `{scope}` 中消除{label}，并用声明的机械指标复核；候选类型为 {kind}。"


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Candidate Evidence 时间必须是 ISO-8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Candidate Evidence 时间必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "CandidateExpectedMetric",
    "CandidateHypothesis",
    "CandidateRisk",
    "EvolutionCandidateDraft",
    "build_candidate_draft",
]
