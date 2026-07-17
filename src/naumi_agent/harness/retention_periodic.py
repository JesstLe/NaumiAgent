"""Lease-owned periodic service for bounded Session retention passes."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from naumi_agent.harness.retention_executor import SessionRetentionPassResult


class RetentionLeasePort(Protocol):
    async def acquire_retention_worker_lease(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> bool: ...

    async def renew_retention_worker_lease(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> bool: ...

    async def release_retention_worker_lease(self, *, owner_id: str) -> bool: ...


class RetentionWorkerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    STANDBY = "standby"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class RetentionPeriodicPolicy:
    interval_seconds: float = 300.0
    max_empty_backoff_seconds: float = 1800.0
    lease_seconds: int = 60
    standby_retry_seconds: float = 15.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("retention 周期间隔必须大于 0 秒。")
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError("空轮最大退避不能小于基础周期间隔。")
        if not 1 <= self.lease_seconds <= 86_400:
            raise ValueError("retention 租约必须在 1 到 86400 秒之间。")
        if self.standby_retry_seconds <= 0:
            raise ValueError("standby 重试间隔必须大于 0 秒。")
        if not 0 <= self.jitter_ratio <= 0.5:
            raise ValueError("retention 抖动比例必须在 0 到 0.5 之间。")


@dataclass(frozen=True, slots=True)
class RetentionWorkerSnapshot:
    owner_id: str
    state: RetentionWorkerState
    lease_held: bool
    pass_count: int
    completed_session_count: int
    retry_scheduled_count: int
    failure_count: int
    consecutive_empty_passes: int
    next_delay_seconds: float
    last_pass_status: str
    last_error_code: str
    started_at: str
    last_pass_at: str


RunRetentionPass = Callable[
    [asyncio.Event],
    Awaitable[SessionRetentionPassResult],
]


class SessionRetentionPeriodicService:
    """Run one retention pass at a time while holding a durable lease."""

    def __init__(
        self,
        *,
        lease_port: RetentionLeasePort,
        run_pass: RunRetentionPass,
        policy: RetentionPeriodicPolicy,
        owner_id: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._owner_id = (owner_id or f"retention-{uuid.uuid4().hex}").strip()
        if not self._owner_id or len(self._owner_id) > 128:
            raise ValueError("retention owner_id 必须为 1 到 128 个字符。")
        self._lease_port = lease_port
        self._run_pass = run_pass
        self._policy = policy
        self._now = now
        self._monotonic = monotonic
        self._random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = RetentionWorkerState.STOPPED
        self._lease_held = False
        self._pass_count = 0
        self._completed_session_count = 0
        self._retry_scheduled_count = 0
        self._failure_count = 0
        self._consecutive_empty_passes = 0
        self._next_delay_seconds = policy.interval_seconds
        self._last_pass_status = ""
        self._last_error_code = ""
        self._started_at = ""
        self._last_pass_at = ""

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    def start(self) -> bool:
        """Start exactly one local loop; cross-process authority uses the lease."""
        if self._task is not None and not self._task.done():
            return False
        self._stop_event.clear()
        self._wake_event.clear()
        self._state = RetentionWorkerState.STARTING
        self._started_at = self._timestamp()
        self._task = asyncio.create_task(
            self._run_loop(),
            name=f"naumi-session-retention:{self._owner_id}",
        )
        return True

    async def stop(self) -> bool:
        """Cooperatively cancel a pass, drain it, and release owned lease."""
        task = self._task
        if task is None or task.done():
            if self._lease_held:
                await self._release_lease()
                self._state = RetentionWorkerState.STOPPED
                return True
            return False
        self._state = RetentionWorkerState.STOPPING
        self._stop_event.set()
        self._wake_event.set()
        try:
            await task
        finally:
            self._task = None
        return True

    def wake(self) -> bool:
        if self._task is None or self._task.done() or self._stop_event.is_set():
            return False
        self._wake_event.set()
        return True

    async def run_cycle(self) -> SessionRetentionPassResult | None:
        """Run one leased cycle; useful for both the loop and deterministic tests."""
        if self._stop_event.is_set():
            self._state = RetentionWorkerState.STOPPING
            return None
        if not self._lease_held:
            try:
                self._lease_held = await self._lease_port.acquire_retention_worker_lease(
                    owner_id=self._owner_id,
                    now=self._timestamp(),
                    lease_seconds=self._policy.lease_seconds,
                )
            except Exception:
                self._lease_held = False
                self._failure_count += 1
                self._last_error_code = "lease_acquire_failed"
            if not self._lease_held:
                self._state = RetentionWorkerState.STANDBY
                self._next_delay_seconds = self._policy.standby_retry_seconds
                return None

        self._state = RetentionWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        try:
            result = await self._run_pass(self._stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failure_count += 1
            self._last_error_code = "pass_failed"
            self._consecutive_empty_passes += 1
            self._next_delay_seconds = self._jittered_delay(
                self._empty_backoff_delay()
            )
            self._state = RetentionWorkerState.WAITING
            return None

        self._pass_count += 1
        self._completed_session_count += result.completed_count
        self._retry_scheduled_count += result.retry_scheduled_count
        self._last_pass_status = result.status.value
        self._last_error_code = ""
        if result.planned_count == 0:
            self._consecutive_empty_passes += 1
            delay = self._empty_backoff_delay()
        else:
            self._consecutive_empty_passes = 0
            delay = self._policy.interval_seconds
        self._next_delay_seconds = self._jittered_delay(delay)
        self._state = RetentionWorkerState.WAITING
        return result

    def snapshot(self) -> RetentionWorkerSnapshot:
        return RetentionWorkerSnapshot(
            owner_id=self._owner_id,
            state=self._state,
            lease_held=self._lease_held,
            pass_count=self._pass_count,
            completed_session_count=self._completed_session_count,
            retry_scheduled_count=self._retry_scheduled_count,
            failure_count=self._failure_count,
            consecutive_empty_passes=self._consecutive_empty_passes,
            next_delay_seconds=self._next_delay_seconds,
            last_pass_status=self._last_pass_status,
            last_error_code=self._last_error_code,
            started_at=self._started_at,
            last_pass_at=self._last_pass_at,
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self.run_cycle()
                if self._stop_event.is_set():
                    break
                await self._wait_and_renew(self._next_delay_seconds)
        finally:
            await self._release_lease()
            self._state = RetentionWorkerState.STOPPED

    async def _wait_and_renew(self, delay: float) -> None:
        deadline = self._monotonic() + max(0.0, delay)
        heartbeat = max(0.1, self._policy.lease_seconds / 3)
        while not self._stop_event.is_set():
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            signal = await self._wait_for_signal(min(remaining, heartbeat))
            if signal == "stop":
                return
            if signal == "wake":
                self._wake_event.clear()
                return
            if self._lease_held:
                try:
                    renewed = await self._lease_port.renew_retention_worker_lease(
                        owner_id=self._owner_id,
                        now=self._timestamp(),
                        lease_seconds=self._policy.lease_seconds,
                    )
                except Exception:
                    renewed = False
                    self._failure_count += 1
                    self._last_error_code = "lease_renew_failed"
                if not renewed:
                    self._lease_held = False
                    self._state = RetentionWorkerState.STANDBY
                    self._next_delay_seconds = self._policy.standby_retry_seconds
                    deadline = self._monotonic() + self._next_delay_seconds

    async def _wait_for_signal(self, timeout: float) -> str:
        stop = asyncio.create_task(self._stop_event.wait())
        wake = asyncio.create_task(self._wake_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop, wake},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop in done and stop.result():
                return "stop"
            if wake in done and wake.result():
                return "wake"
            return "timeout"
        finally:
            for task in (stop, wake):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _release_lease(self) -> None:
        if not self._lease_held:
            return
        try:
            await self._lease_port.release_retention_worker_lease(
                owner_id=self._owner_id
            )
        except Exception:
            self._failure_count += 1
            self._last_error_code = "lease_release_failed"
        finally:
            self._lease_held = False

    def _empty_backoff_delay(self) -> float:
        return min(
            self._policy.max_empty_backoff_seconds,
            self._policy.interval_seconds * (2**self._consecutive_empty_passes),
        )

    def _jittered_delay(self, delay: float) -> float:
        spread = self._policy.jitter_ratio
        factor = 1 + spread * (2 * self._random_value() - 1)
        return max(0.0, delay * factor)

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retention worker 时钟必须包含时区。")
        return value.astimezone(UTC).isoformat()
