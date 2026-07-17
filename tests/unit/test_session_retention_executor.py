"""HAR-06.5b1 bounded Session retention pass tests."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    ReconciliationCoordinatorResult,
)
from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionExecutor,
)
from naumi_agent.harness.retention_planner import (
    SessionRetentionPolicy,
    SessionRetentionPreview,
    SessionRetentionReason,
    SessionRetentionSelection,
)


def _preview(ids: tuple[str, ...]) -> SessionRetentionPreview:
    selected = tuple(
        SessionRetentionSelection(
            session_id=session_id,
            title=session_id,
            effective_last_accessed_at=datetime(2026, 1, 1),
            payload_bytes=100,
            reason=SessionRetentionReason.AGE_EXPIRED,
        )
        for session_id in ids
    )
    return SessionRetentionPreview(
        selected=selected,
        total_archived_count=len(ids),
        total_archived_bytes=len(ids) * 100,
        scanned_count=len(ids),
        eligible_count=len(ids),
        deferred_eligible_count=0,
        selected_bytes=len(ids) * 100,
        storage_excess_bytes=0,
        scan_truncated=False,
        budget_exhausted=False,
        policy=SessionRetentionPolicy(),
    )


def _result(
    session_id: str,
    outcome: ReconciliationCoordinatorOutcome,
) -> ReconciliationCoordinatorResult:
    return ReconciliationCoordinatorResult(
        session_id=session_id,
        request_id=f"request-{session_id}",
        outcome=outcome,
        reconciliation_state=None,
        tombstone_status=None,
        message=outcome.value,
    )


@pytest.mark.asyncio
async def test_executor_processes_oldest_first_and_counts_all_outcomes() -> None:
    delete = AsyncMock(
        side_effect=[
            _result("a", ReconciliationCoordinatorOutcome.COMPLETED),
            _result("b", ReconciliationCoordinatorOutcome.RETRY_SCHEDULED),
            _result("c", ReconciliationCoordinatorOutcome.POLICY_BLOCKED),
        ]
    )

    result = await SessionRetentionExecutor(delete).execute(
        _preview(("a", "b", "c")),
        max_runtime_seconds=10,
    )

    assert [call.args[0] for call in delete.await_args_list] == ["a", "b", "c"]
    assert result.status is RetentionPassStatus.PARTIAL
    assert result.attempted_count == 3
    assert result.completed_count == 1
    assert result.retry_scheduled_count == 1
    assert result.policy_blocked_count == 1
    assert result.remaining_count == 0


@pytest.mark.asyncio
async def test_executor_stops_before_next_item_when_time_budget_is_spent() -> None:
    ticks = iter((0.0, 0.2, 1.1, 1.1))
    delete = AsyncMock(
        return_value=_result("a", ReconciliationCoordinatorOutcome.COMPLETED)
    )
    executor = SessionRetentionExecutor(delete, monotonic=lambda: next(ticks))

    result = await executor.execute(
        _preview(("a", "b")),
        max_runtime_seconds=1.0,
    )

    delete.assert_awaited_once_with("a")
    assert result.status is RetentionPassStatus.DEADLINE_REACHED
    assert result.attempted_count == 1
    assert result.remaining_count == 1


@pytest.mark.asyncio
async def test_explicit_cancel_event_cancels_inflight_coordinator_safely() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def delete(_: str) -> ReconciliationCoordinatorResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        SessionRetentionExecutor(delete).execute(
            _preview(("a", "b")),
            max_runtime_seconds=10,
            cancel_event=cancel_event,
        )
    )
    await started.wait()
    cancel_event.set()

    result = await asyncio.wait_for(task, timeout=1)

    assert cancelled.is_set()
    assert result.status is RetentionPassStatus.CANCELLED
    assert result.attempted_count == 1
    assert result.remaining_count == 2


@pytest.mark.asyncio
async def test_unexpected_coordinator_error_fails_closed_and_stops_batch() -> None:
    delete = AsyncMock(side_effect=RuntimeError("raw secret should not escape"))

    result = await SessionRetentionExecutor(delete).execute(
        _preview(("a", "b")),
        max_runtime_seconds=10,
    )

    assert result.status is RetentionPassStatus.FAILED
    assert result.error_count == 1
    assert result.remaining_count == 2
    assert "raw secret" not in result.message
