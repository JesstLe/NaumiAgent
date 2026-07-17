"""HAR-06.5b2a periodic retention service tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from naumi_agent.config.settings import SessionRetentionConfig
from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_periodic import (
    RetentionPeriodicPolicy,
    RetentionWorkerState,
    SessionRetentionPeriodicService,
)


def _pass_result(*, planned: int = 0, completed: int = 0) -> SessionRetentionPassResult:
    return SessionRetentionPassResult(
        status=RetentionPassStatus.COMPLETED,
        planned_count=planned,
        attempted_count=completed,
        completed_count=completed,
        retry_scheduled_count=0,
        retry_exhausted_count=0,
        policy_blocked_count=0,
        not_found_count=0,
        error_count=0,
        remaining_count=max(0, planned - completed),
        planned_bytes=planned * 100,
        duration_seconds=0.01,
        results=(),
        message="完成",
    )


def test_periodic_config_is_default_off_and_rejects_unsafe_lease() -> None:
    assert SessionRetentionConfig().periodic_enabled is False
    with pytest.raises(ValueError, match="worker_lease_seconds"):
        SessionRetentionConfig(
            max_runtime_seconds=60,
            worker_lease_seconds=60,
        )


class _LeasePort:
    def __init__(self, *, acquire: bool = True, renew: bool = True) -> None:
        self.acquire_allowed = acquire
        self.renew_allowed = renew
        self.acquire_calls = 0
        self.renew_calls = 0
        self.release_calls = 0

    async def acquire_retention_worker_lease(self, **_: object) -> bool:
        self.acquire_calls += 1
        return self.acquire_allowed

    async def renew_retention_worker_lease(self, **_: object) -> bool:
        self.renew_calls += 1
        return self.renew_allowed

    async def release_retention_worker_lease(self, **_: object) -> bool:
        self.release_calls += 1
        return True


@pytest.mark.asyncio
async def test_run_cycle_holds_lease_and_records_bounded_metrics() -> None:
    lease = _LeasePort()
    run_pass = AsyncMock(return_value=_pass_result(planned=2, completed=2))
    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(
            interval_seconds=1,
            max_empty_backoff_seconds=4,
            lease_seconds=2,
            standby_retry_seconds=1,
            jitter_ratio=0,
        ),
    )

    result = await service.run_cycle()
    snapshot = service.snapshot()

    assert result is not None
    assert snapshot.state is RetentionWorkerState.WAITING
    assert snapshot.lease_held is True
    assert snapshot.pass_count == 1
    assert snapshot.completed_session_count == 2
    assert snapshot.failure_count == 0
    assert snapshot.next_delay_seconds == 1
    run_pass.assert_awaited_once_with(service.stop_event)


@pytest.mark.asyncio
async def test_empty_cycles_back_off_and_nonempty_cycle_resets_delay() -> None:
    lease = _LeasePort()
    run_pass = AsyncMock(
        side_effect=[
            _pass_result(),
            _pass_result(),
            _pass_result(planned=1, completed=1),
        ]
    )
    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(
            interval_seconds=2,
            max_empty_backoff_seconds=8,
            lease_seconds=3,
            standby_retry_seconds=1,
            jitter_ratio=0,
        ),
    )

    await service.run_cycle()
    assert service.snapshot().next_delay_seconds == 4
    await service.run_cycle()
    assert service.snapshot().next_delay_seconds == 8
    await service.run_cycle()
    assert service.snapshot().next_delay_seconds == 2


@pytest.mark.asyncio
async def test_standby_worker_never_runs_pass_without_lease() -> None:
    lease = _LeasePort(acquire=False)
    run_pass = AsyncMock()
    service = SessionRetentionPeriodicService(
        owner_id="standby",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(jitter_ratio=0),
    )

    result = await service.run_cycle()

    assert result is None
    assert service.snapshot().state is RetentionWorkerState.STANDBY
    assert service.snapshot().lease_held is False
    run_pass.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_pass_error_isolated_and_raw_text_not_exposed() -> None:
    lease = _LeasePort()
    run_pass = AsyncMock(side_effect=RuntimeError("raw secret"))
    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(jitter_ratio=0),
    )

    assert await service.run_cycle() is None
    snapshot = service.snapshot()

    assert snapshot.state is RetentionWorkerState.WAITING
    assert snapshot.failure_count == 1
    assert snapshot.last_error_code == "pass_failed"
    assert "raw secret" not in snapshot.last_error_code


@pytest.mark.asyncio
async def test_start_stop_are_idempotent_and_stop_cancels_inflight_pass() -> None:
    lease = _LeasePort()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def run_pass(cancel_event: asyncio.Event) -> SessionRetentionPassResult:
        started.set()
        await cancel_event.wait()
        stopped.set()
        return _pass_result()

    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(jitter_ratio=0),
    )

    assert service.start() is True
    assert service.start() is False
    await asyncio.wait_for(started.wait(), timeout=1)
    assert await service.stop() is True
    assert await service.stop() is False

    assert stopped.is_set()
    assert lease.release_calls == 1
    assert service.snapshot().state is RetentionWorkerState.STOPPED


@pytest.mark.asyncio
async def test_wake_interrupts_long_wait_and_runs_next_cycle() -> None:
    lease = _LeasePort()
    second_pass = asyncio.Event()
    calls = 0

    async def run_pass(_: asyncio.Event) -> SessionRetentionPassResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_pass.set()
        return _pass_result(planned=1, completed=1)

    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(
            interval_seconds=30,
            max_empty_backoff_seconds=30,
            lease_seconds=2,
            standby_retry_seconds=1,
            jitter_ratio=0,
        ),
    )

    assert service.start()
    while service.snapshot().pass_count < 1:
        await asyncio.sleep(0)
    assert service.wake()
    await asyncio.wait_for(second_pass.wait(), timeout=1)
    await service.stop()

    assert calls == 2


@pytest.mark.asyncio
async def test_lease_loss_during_wait_moves_worker_to_standby() -> None:
    lease = _LeasePort(renew=False)
    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=AsyncMock(return_value=_pass_result(planned=1, completed=1)),
        policy=RetentionPeriodicPolicy(
            interval_seconds=1,
            max_empty_backoff_seconds=1,
            lease_seconds=1,
            standby_retry_seconds=0.1,
            jitter_ratio=0,
        ),
    )
    await service.run_cycle()

    await service._wait_and_renew(0.4)

    assert lease.renew_calls == 1
    assert service.snapshot().state is RetentionWorkerState.STANDBY
    assert service.snapshot().lease_held is False


@pytest.mark.asyncio
async def test_lease_loss_waits_standby_interval_before_reacquiring() -> None:
    lease = _LeasePort(renew=False)
    first_pass = asyncio.Event()
    second_acquire = asyncio.Event()

    async def run_pass(_: asyncio.Event) -> SessionRetentionPassResult:
        first_pass.set()
        return _pass_result(planned=1, completed=1)

    original_acquire = lease.acquire_retention_worker_lease

    async def acquire(**kwargs: object) -> bool:
        acquired = await original_acquire(**kwargs)
        if lease.acquire_calls == 2:
            second_acquire.set()
        return acquired

    lease.acquire_retention_worker_lease = acquire  # type: ignore[method-assign]
    service = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=lease,
        run_pass=run_pass,
        policy=RetentionPeriodicPolicy(
            interval_seconds=0.6,
            max_empty_backoff_seconds=0.6,
            lease_seconds=1,
            standby_retry_seconds=0.25,
            jitter_ratio=0,
        ),
    )

    assert service.start()
    await asyncio.wait_for(first_pass.wait(), timeout=1)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_acquire.wait(), timeout=0.45)
    await asyncio.wait_for(second_acquire.wait(), timeout=0.3)
    await service.stop()
