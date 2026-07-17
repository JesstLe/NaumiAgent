"""Bounded single-pass executor for planned Session retention."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    ReconciliationCoordinatorResult,
)
from naumi_agent.harness.retention_planner import SessionRetentionPreview


class RetentionPassStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    DEADLINE_REACHED = "deadline_reached"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionRetentionPassResult:
    status: RetentionPassStatus
    planned_count: int
    attempted_count: int
    completed_count: int
    retry_scheduled_count: int
    retry_exhausted_count: int
    policy_blocked_count: int
    not_found_count: int
    error_count: int
    remaining_count: int
    planned_bytes: int
    duration_seconds: float
    results: tuple[ReconciliationCoordinatorResult, ...]
    message: str


DeleteSession = Callable[[str], Awaitable[ReconciliationCoordinatorResult]]


class SessionRetentionExecutor:
    """Execute one immutable plan through the durable deletion coordinator."""

    def __init__(
        self,
        delete_session: DeleteSession,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delete_session = delete_session
        self._monotonic = monotonic

    async def execute(
        self,
        preview: SessionRetentionPreview,
        *,
        max_runtime_seconds: float,
        cancel_event: asyncio.Event | None = None,
    ) -> SessionRetentionPassResult:
        if max_runtime_seconds <= 0:
            raise ValueError("retention 单轮时间预算必须大于 0 秒。")
        started_at = self._monotonic()
        outcomes: list[ReconciliationCoordinatorResult] = []
        attempted = 0
        error_count = 0
        terminal_count = 0
        stop_status: RetentionPassStatus | None = None

        for candidate in preview.selected:
            if cancel_event is not None and cancel_event.is_set():
                stop_status = RetentionPassStatus.CANCELLED
                break
            elapsed = self._monotonic() - started_at
            remaining_seconds = max_runtime_seconds - elapsed
            if remaining_seconds <= 0:
                stop_status = RetentionPassStatus.DEADLINE_REACHED
                break

            attempted += 1
            try:
                outcome, stopped = await self._execute_one(
                    candidate.session_id,
                    timeout=remaining_seconds,
                    cancel_event=cancel_event,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                error_count += 1
                stop_status = RetentionPassStatus.FAILED
                break
            if stopped is not None:
                stop_status = stopped
                break
            assert outcome is not None
            outcomes.append(outcome)
            terminal_count += 1

        counts = {outcome: 0 for outcome in ReconciliationCoordinatorOutcome}
        for item in outcomes:
            counts[item.outcome] += 1

        if stop_status is None:
            non_success = terminal_count - counts[ReconciliationCoordinatorOutcome.COMPLETED]
            stop_status = (
                RetentionPassStatus.COMPLETED
                if non_success == 0
                else RetentionPassStatus.PARTIAL
            )
        duration = max(0.0, self._monotonic() - started_at)
        remaining_count = max(0, len(preview.selected) - terminal_count)
        return SessionRetentionPassResult(
            status=stop_status,
            planned_count=len(preview.selected),
            attempted_count=attempted,
            completed_count=counts[ReconciliationCoordinatorOutcome.COMPLETED],
            retry_scheduled_count=counts[
                ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
            ],
            retry_exhausted_count=counts[
                ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED
            ],
            policy_blocked_count=counts[
                ReconciliationCoordinatorOutcome.POLICY_BLOCKED
            ],
            not_found_count=counts[ReconciliationCoordinatorOutcome.NOT_FOUND],
            error_count=error_count,
            remaining_count=remaining_count,
            planned_bytes=preview.selected_bytes,
            duration_seconds=duration,
            results=tuple(outcomes),
            message=_pass_message(stop_status),
        )

    async def _execute_one(
        self,
        session_id: str,
        *,
        timeout: float,
        cancel_event: asyncio.Event | None,
    ) -> tuple[
        ReconciliationCoordinatorResult | None,
        RetentionPassStatus | None,
    ]:
        deletion = asyncio.create_task(self._delete_session(session_id))
        cancellation = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        waiting = {deletion}
        if cancellation is not None:
            waiting.add(cancellation)
        try:
            done, _ = await asyncio.wait(
                waiting,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if deletion in done:
                return deletion.result(), None
            await _cancel_and_drain(deletion)
            if cancellation is not None and cancellation in done:
                return None, RetentionPassStatus.CANCELLED
            return None, RetentionPassStatus.DEADLINE_REACHED
        except asyncio.CancelledError:
            await _cancel_and_drain(deletion)
            raise
        finally:
            if cancellation is not None:
                await _cancel_and_drain(cancellation)


async def _cancel_and_drain(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _pass_message(status: RetentionPassStatus) -> str:
    return {
        RetentionPassStatus.COMPLETED: "本轮 Session 保留清理已完成。",
        RetentionPassStatus.PARTIAL: "本轮部分完成；未完成项已阻止或进入安全重试。",
        RetentionPassStatus.DEADLINE_REACHED: "已达到单轮时间预算，剩余候选未执行。",
        RetentionPassStatus.CANCELLED: "本轮已取消；进行中的协调已安全停止或进入恢复队列。",
        RetentionPassStatus.FAILED: "协调器发生未预期错误，本轮已失败关闭。",
    }[status]
