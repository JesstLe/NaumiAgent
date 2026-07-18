"""Trusted, privacy-bounded feedback intake for HAR-09 candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.store import EvolutionCandidateStore

FeedbackCategory = Literal["correction", "defect", "preference", "cancel", "praise"]
FeedbackOrigin = Literal["direct_user", "agent_interpretation"]
_ACTIONABLE_CATEGORIES = frozenset({"correction", "defect"})
_TOPIC_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ABSOLUTE_SCOPE_RE = re.compile(r"(?:^|:)(?:/|[A-Za-z]:[\\/])")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedbackSourceEnvelope(_StrictModel):
    """Runtime-minted reference to one durable user turn."""

    run_id: str = Field(min_length=1, max_length=256)
    user_message_id: str = Field(min_length=1, max_length=256)
    content_sha256: str
    observed_at: str

    @field_validator("content_sha256")
    @classmethod
    def _full_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("user turn digest 必须是完整 SHA-256。")
        return value

    @field_validator("observed_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        _parse_aware_time(value)
        return value


class FeedbackObservation(_StrictModel):
    """Transient input; summary is hashed and never persisted."""

    category: FeedbackCategory
    scope: str = Field(min_length=1, max_length=1_024)
    topic: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2_000)
    origin: FeedbackOrigin
    source_id: str = Field(min_length=1, max_length=256)
    source_uri: str = Field(min_length=1, max_length=2_048)
    source_sha256: str
    observed_at: str
    provider: str = Field(default="", max_length=128)
    model: str = Field(default="", max_length=256)
    platform: str = Field(default="", max_length=64)

    @field_validator("scope")
    @classmethod
    def _relative_scope(cls, value: str) -> str:
        normalized = value.strip()
        parts = re.split(r"[\\/:]", normalized)
        if _ABSOLUTE_SCOPE_RE.search(normalized) or ".." in parts:
            raise ValueError("feedback scope 必须是相对 scope。")
        if any(character in normalized for character in ("\n", "\r", "\x00")):
            raise ValueError("feedback scope 含非法控制字符。")
        if normalized.casefold().startswith("files:"):
            items = normalized[6:].replace("\\", "/").split(",")
            if not 2 <= len(items) <= 16:
                raise ValueError("多文件 feedback scope 必须包含 2..16 个文件。")
            clean_items = tuple(item.strip() for item in items)
            if len(set(clean_items)) != len(clean_items):
                raise ValueError("多文件 feedback scope 不得重复。")
            for item in clean_items:
                item_parts = item.split("/")
                if (
                    not item
                    or item.endswith("/")
                    or _ABSOLUTE_SCOPE_RE.search(item)
                    or ".." in item_parts
                    or ":" in item
                ):
                    raise ValueError("多文件 feedback scope 包含不安全路径。")
        return normalized

    @field_validator("topic")
    @classmethod
    def _stable_topic(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _TOPIC_RE.fullmatch(normalized):
            raise ValueError("feedback topic 必须是稳定的小写标识符。")
        return normalized

    @field_validator("summary")
    @classmethod
    def _transient_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("feedback summary 不能为空或包含 NUL。")
        return normalized

    @field_validator("source_sha256")
    @classmethod
    def _source_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("feedback source digest 必须是完整 SHA-256。")
        return value

    @field_validator("observed_at")
    @classmethod
    def _observation_time(cls, value: str) -> str:
        _parse_aware_time(value)
        return value

    @model_validator(mode="after")
    def _source_kind_matches_uri(self) -> FeedbackObservation:
        if self.origin == "direct_user" and not self.source_uri.startswith(
            "artifact://feedback/"
        ):
            raise ValueError("direct feedback 必须使用内部 feedback URI。")
        if self.origin == "agent_interpretation" and not self.source_uri.startswith(
            "chat-run://runs/"
        ):
            raise ValueError("Agent feedback 必须引用 durable Chat Run。")
        return self


@dataclass(frozen=True, slots=True)
class FeedbackIntakeResult:
    status: Literal["recorded", "ignored"]
    reason_code: str
    candidate_id: str = ""
    occurrence_count: int = 0
    source_kind: str = ""


class FeedbackIntakeService:
    """Convert trusted feedback observations into non-executable Candidates."""

    def __init__(self, store: EvolutionCandidateStore) -> None:
        if not isinstance(store, EvolutionCandidateStore):
            raise TypeError("store 必须是 EvolutionCandidateStore 实例。")
        self._store = store

    async def ingest(
        self,
        workspace_root: str | Path,
        observation: FeedbackObservation,
    ) -> FeedbackIntakeResult:
        if observation.category not in _ACTIONABLE_CATEGORIES:
            return FeedbackIntakeResult(
                status="ignored",
                reason_code=f"non_defect_{observation.category}",
            )
        evidence = adapt_feedback_evidence(observation)
        stored = await self._store.upsert_candidate(
            workspace_root,
            build_candidate_draft((evidence,)),
        )
        return FeedbackIntakeResult(
            status="recorded",
            reason_code="candidate_draft_updated",
            candidate_id=stored.draft.candidate_id,
            occurrence_count=stored.draft.occurrence_count,
            source_kind=evidence.source_kind,
        )


def build_direct_user_feedback(
    *,
    session_id: str,
    category: FeedbackCategory,
    scope: str,
    topic: str,
    summary: str,
    provider: str = "",
    model: str = "",
    platform: str = "",
    now: datetime | None = None,
) -> FeedbackObservation:
    """Build an idempotent direct-user observation for one minute bucket."""
    observed = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    source_payload = {
        "category": category,
        "scope": scope.strip(),
        "session_id": session_id.strip(),
        "summary": summary.strip(),
        "topic": topic.strip().lower(),
        "window": observed.isoformat(),
    }
    source_sha256 = _digest(source_payload)
    source_id = f"feedback-{source_sha256[:24]}"
    return FeedbackObservation(
        category=category,
        scope=scope,
        topic=topic,
        summary=summary,
        origin="direct_user",
        source_id=source_id,
        source_uri=f"artifact://feedback/{quote(source_id, safe='')}",
        source_sha256=source_sha256,
        observed_at=observed.isoformat(),
        provider=provider,
        model=model,
        platform=platform,
    )


def build_agent_interpreted_feedback(
    envelope: FeedbackSourceEnvelope,
    *,
    category: FeedbackCategory,
    scope: str,
    topic: str,
    summary: str,
    provider: str = "",
    model: str = "",
    platform: str = "",
) -> FeedbackObservation:
    """Bind an Agent interpretation to a runtime-minted durable turn."""
    source_id = f"{envelope.run_id}:{envelope.user_message_id}"
    return FeedbackObservation(
        category=category,
        scope=scope,
        topic=topic,
        summary=summary,
        origin="agent_interpretation",
        source_id=source_id,
        source_uri=(
            f"chat-run://runs/{quote(envelope.run_id, safe='')}/messages/"
            f"{quote(envelope.user_message_id, safe='')}"
        ),
        source_sha256=envelope.content_sha256,
        observed_at=envelope.observed_at,
        provider=provider,
        model=model,
        platform=platform,
    )


def adapt_feedback_evidence(observation: FeedbackObservation) -> EvolutionEvidence:
    if observation.category not in _ACTIONABLE_CATEGORIES:
        raise ValueError("非缺陷反馈不得转换为 Evolution Evidence。")
    finding_code = (
        "user_correction"
        if observation.category == "correction"
        else "user_reported_defect"
    )
    root_fingerprint = _digest({
        "finding_code": finding_code,
        "scope": observation.scope,
        "topic": observation.topic,
    })
    evidence_id = f"eve_{_digest({
        'observed_at': observation.observed_at,
        'origin': observation.origin,
        'root_fingerprint': root_fingerprint,
        'source_id': observation.source_id,
    })[:24]}"
    ref = EvolutionEvidenceRef(
        uri=observation.source_uri,
        sha256=observation.source_sha256,
    )
    source_kind = (
        "user_feedback"
        if observation.origin == "direct_user"
        else "agent_interpreted_feedback"
    )
    return EvolutionEvidence(
        evidence_id=evidence_id,
        source_kind=source_kind,
        source_uri=ref.uri,
        observed_at=observation.observed_at,
        finding_code=finding_code,
        scope=observation.scope,
        root_fingerprint=root_fingerprint,
        refs=(ref,),
        feedback_category=observation.category,
        feedback_topic=observation.topic,
        provider=observation.provider,
        model=observation.model,
        platform=observation.platform,
    )


def render_feedback_result(result: FeedbackIntakeResult) -> str:
    if result.status == "ignored":
        return "该反馈已识别为偏好、取消或正向评价，不会计入 Agent 缺陷候选。"
    return (
        "反馈证据已安全记录为不可执行 Candidate Draft。\n"
        f"- Candidate：`{result.candidate_id}`\n"
        f"- 唯一证据：{result.occurrence_count}\n"
        f"- 来源：`{result.source_kind}`\n"
        "- 状态：尚未通过 Eligibility，不会自动修改代码或配置。"
    )


def _parse_aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("feedback 时间必须是 ISO-8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("feedback 时间必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "FeedbackCategory",
    "FeedbackIntakeResult",
    "FeedbackIntakeService",
    "FeedbackObservation",
    "FeedbackSourceEnvelope",
    "adapt_feedback_evidence",
    "build_agent_interpreted_feedback",
    "build_direct_user_feedback",
    "render_feedback_result",
]
