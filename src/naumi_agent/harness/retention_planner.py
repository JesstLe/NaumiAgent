"""Deterministic, side-effect-free planning for Session retention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from naumi_agent.memory.lifecycle import SessionRetentionCandidate


class SessionRetentionReason(StrEnum):
    """Closed reasons explaining why one archived Session is eligible."""

    AGE_EXPIRED = "age_expired"
    STORAGE_PRESSURE = "storage_pressure"
    AGE_AND_STORAGE = "age_and_storage"


@dataclass(frozen=True, slots=True)
class SessionRetentionPolicy:
    """Validated limits consumed by the pure planner."""

    delete_archived_after_days: int = 30
    max_archived_session_bytes: int = 0
    max_sessions_per_pass: int = 20
    max_bytes_per_pass: int = 256 * 1024 * 1024
    scan_limit: int = 10_000

    def __post_init__(self) -> None:
        if self.delete_archived_after_days < 1:
            raise ValueError("归档保留天数必须至少为 1 天。")
        if self.max_archived_session_bytes < 0:
            raise ValueError("归档会话空间上限不能为负数。")
        if self.max_sessions_per_pass < 1:
            raise ValueError("单轮最多清理会话数必须至少为 1。")
        if self.max_bytes_per_pass < 1:
            raise ValueError("单轮最多清理字节数必须至少为 1。")
        if not 1 <= self.scan_limit <= 10_000:
            raise ValueError("单轮扫描上限必须在 1 到 10000 之间。")


@dataclass(frozen=True, slots=True)
class SessionRetentionSelection:
    session_id: str
    title: str
    effective_last_accessed_at: datetime
    payload_bytes: int
    reason: SessionRetentionReason


@dataclass(frozen=True, slots=True)
class SessionRetentionPreview:
    """Explainable dry-run result; it never authorizes deletion by itself."""

    selected: tuple[SessionRetentionSelection, ...]
    total_archived_count: int
    total_archived_bytes: int
    scanned_count: int
    eligible_count: int
    deferred_eligible_count: int
    selected_bytes: int
    storage_excess_bytes: int
    scan_truncated: bool
    budget_exhausted: bool
    policy: SessionRetentionPolicy


def plan_session_retention(
    candidates: list[SessionRetentionCandidate]
    | tuple[SessionRetentionCandidate, ...],
    *,
    total_archived_count: int,
    total_archived_bytes: int,
    policy: SessionRetentionPolicy,
    now: datetime,
    current_session_id: str = "",
) -> SessionRetentionPreview:
    """Select an oldest-first bounded dry-run without mutating persistence."""
    if total_archived_count < 0 or total_archived_bytes < 0:
        raise ValueError("归档会话统计不能为负数。")

    cutoff = now - timedelta(days=policy.delete_archived_after_days)
    ordered = sorted(
        (
            item
            for item in candidates
            if item.status == "archived"
            and item.session_id
            and item.session_id != current_session_id
            and item.payload_bytes >= 0
        ),
        key=lambda item: (item.effective_last_accessed_at, item.session_id),
    )

    storage_excess = (
        max(0, total_archived_bytes - policy.max_archived_session_bytes)
        if policy.max_archived_session_bytes > 0
        else 0
    )
    pressure_ids: set[str] = set()
    reclaimed = 0
    if storage_excess:
        for item in ordered:
            pressure_ids.add(item.session_id)
            reclaimed += item.payload_bytes
            if reclaimed >= storage_excess:
                break

    eligible: list[SessionRetentionSelection] = []
    for item in ordered:
        age_expired = item.effective_last_accessed_at <= cutoff
        under_pressure = item.session_id in pressure_ids
        if not age_expired and not under_pressure:
            continue
        reason = (
            SessionRetentionReason.AGE_AND_STORAGE
            if age_expired and under_pressure
            else SessionRetentionReason.AGE_EXPIRED
            if age_expired
            else SessionRetentionReason.STORAGE_PRESSURE
        )
        eligible.append(
            SessionRetentionSelection(
                session_id=item.session_id,
                title=item.title,
                effective_last_accessed_at=item.effective_last_accessed_at,
                payload_bytes=item.payload_bytes,
                reason=reason,
            )
        )

    selected: list[SessionRetentionSelection] = []
    selected_bytes = 0
    for item in eligible:
        if len(selected) >= policy.max_sessions_per_pass:
            continue
        if selected_bytes + item.payload_bytes > policy.max_bytes_per_pass:
            continue
        selected.append(item)
        selected_bytes += item.payload_bytes

    deferred = len(eligible) - len(selected)
    return SessionRetentionPreview(
        selected=tuple(selected),
        total_archived_count=total_archived_count,
        total_archived_bytes=total_archived_bytes,
        scanned_count=len(candidates),
        eligible_count=len(eligible),
        deferred_eligible_count=deferred,
        selected_bytes=selected_bytes,
        storage_excess_bytes=storage_excess,
        scan_truncated=total_archived_count > len(candidates),
        budget_exhausted=deferred > 0,
        policy=policy,
    )
