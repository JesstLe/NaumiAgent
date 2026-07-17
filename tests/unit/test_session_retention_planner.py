"""HAR-06.5a deterministic Session retention planning tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.harness.retention_planner import (
    SessionRetentionCandidate,
    SessionRetentionPolicy,
    SessionRetentionReason,
    plan_session_retention,
)

NOW = datetime(2026, 7, 17, 12, 0, 0)


def test_retention_config_is_safe_by_default_and_rejects_unbounded_scan() -> None:
    assert MemoryConfig().session_retention.max_archived_session_bytes == 0
    with pytest.raises(ValueError, match="less than or equal to 10000"):
        MemoryConfig(session_retention={"scan_limit": 10_001})


def _candidate(
    session_id: str,
    *,
    days_since_access: int,
    payload_bytes: int = 100,
    status: str = "archived",
    archived_days_ago: int | None = None,
) -> SessionRetentionCandidate:
    archived_days = (
        days_since_access if archived_days_ago is None else archived_days_ago
    )
    return SessionRetentionCandidate(
        session_id=session_id,
        title=f"会话 {session_id}",
        status=status,
        last_accessed_at=NOW - timedelta(days=days_since_access),
        archived_at=NOW - timedelta(days=archived_days),
        payload_bytes=payload_bytes,
    )


def _policy(**overrides: int) -> SessionRetentionPolicy:
    values = {
        "delete_archived_after_days": 30,
        "max_archived_session_bytes": 0,
        "max_sessions_per_pass": 20,
        "max_bytes_per_pass": 1024,
        "scan_limit": 10_000,
    }
    values.update(overrides)
    return SessionRetentionPolicy(**values)


def test_age_expiry_uses_recent_access_not_old_archive_time() -> None:
    candidates = [
        _candidate("expired", days_since_access=31),
        _candidate("recent", days_since_access=2, archived_days_ago=90),
        _candidate("active", days_since_access=90, status="active"),
    ]

    preview = plan_session_retention(
        candidates,
        total_archived_count=2,
        total_archived_bytes=300,
        policy=_policy(),
        now=NOW,
        current_session_id="expired-current-does-not-match",
    )

    assert [item.session_id for item in preview.selected] == ["expired"]
    assert preview.selected[0].reason is SessionRetentionReason.AGE_EXPIRED
    assert preview.selected[0].effective_last_accessed_at == NOW - timedelta(days=31)


def test_current_session_is_never_selected_even_if_archived_and_expired() -> None:
    preview = plan_session_retention(
        [_candidate("current", days_since_access=90)],
        total_archived_count=1,
        total_archived_bytes=100,
        policy=_policy(),
        now=NOW,
        current_session_id="current",
    )

    assert preview.selected == ()
    assert preview.eligible_count == 0


def test_storage_pressure_selects_oldest_until_excess_is_reclaimed() -> None:
    candidates = [
        _candidate("new", days_since_access=1, payload_bytes=300),
        _candidate("old", days_since_access=10, payload_bytes=250),
        _candidate("older", days_since_access=20, payload_bytes=200),
    ]

    preview = plan_session_retention(
        candidates,
        total_archived_count=3,
        total_archived_bytes=750,
        policy=_policy(max_archived_session_bytes=400),
        now=NOW,
    )

    assert [item.session_id for item in preview.selected] == ["older", "old"]
    assert all(
        item.reason is SessionRetentionReason.STORAGE_PRESSURE
        for item in preview.selected
    )
    assert preview.storage_excess_bytes == 350
    assert preview.selected_bytes == 450


def test_reason_is_both_when_age_and_storage_rules_select_same_session() -> None:
    preview = plan_session_retention(
        [_candidate("old", days_since_access=60, payload_bytes=200)],
        total_archived_count=1,
        total_archived_bytes=200,
        policy=_policy(max_archived_session_bytes=100),
        now=NOW,
    )

    assert preview.selected[0].reason is SessionRetentionReason.AGE_AND_STORAGE


def test_hard_pass_budgets_defer_eligible_sessions_without_overshoot() -> None:
    candidates = [
        _candidate("too-large", days_since_access=90, payload_bytes=900),
        _candidate("fits-1", days_since_access=80, payload_bytes=200),
        _candidate("fits-2", days_since_access=70, payload_bytes=200),
        _candidate("count-deferred", days_since_access=60, payload_bytes=50),
    ]

    preview = plan_session_retention(
        candidates,
        total_archived_count=4,
        total_archived_bytes=1350,
        policy=_policy(max_sessions_per_pass=2, max_bytes_per_pass=400),
        now=NOW,
    )

    assert [item.session_id for item in preview.selected] == ["fits-1", "fits-2"]
    assert preview.selected_bytes == 400
    assert preview.deferred_eligible_count == 2
    assert preview.budget_exhausted is True


def test_order_is_deterministic_and_scan_truncation_is_visible_at_10k() -> None:
    candidates = [
        _candidate(f"s-{index:05d}", days_since_access=45, payload_bytes=1)
        for index in reversed(range(10_000))
    ]

    preview = plan_session_retention(
        candidates,
        total_archived_count=10_001,
        total_archived_bytes=10_001,
        policy=_policy(max_sessions_per_pass=3, max_bytes_per_pass=3),
        now=NOW,
    )

    assert [item.session_id for item in preview.selected] == [
        "s-00000",
        "s-00001",
        "s-00002",
    ]
    assert preview.scanned_count == 10_000
    assert preview.scan_truncated is True
