"""Real cross-instance SQLite lease tests for HAR-06.5b2a."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_periodic import (
    RetentionPeriodicPolicy,
    SessionRetentionPeriodicService,
)
from naumi_agent.harness.store import HarnessStore


@pytest.mark.asyncio
async def test_only_one_store_instance_holds_retention_lease_and_expiry_takes_over(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    first = HarnessStore(db_path)
    second = HarnessStore(db_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    acquired = await asyncio.gather(
        first.acquire_retention_worker_lease(
            owner_id="worker-a",
            now=now.isoformat(),
            lease_seconds=30,
        ),
        second.acquire_retention_worker_lease(
            owner_id="worker-b",
            now=now.isoformat(),
            lease_seconds=30,
        ),
    )

    assert sum(acquired) == 1
    winner, loser = ("worker-a", "worker-b") if acquired[0] else ("worker-b", "worker-a")
    winner_store = first if acquired[0] else second
    loser_store = second if acquired[0] else first
    assert await loser_store.renew_retention_worker_lease(
        owner_id=loser,
        now=(now + timedelta(seconds=1)).isoformat(),
        lease_seconds=30,
    ) is False
    assert await winner_store.renew_retention_worker_lease(
        owner_id=winner,
        now=(now + timedelta(seconds=1)).isoformat(),
        lease_seconds=30,
    ) is True
    assert await loser_store.acquire_retention_worker_lease(
        owner_id=loser,
        now=(now + timedelta(seconds=32)).isoformat(),
        lease_seconds=30,
    ) is True
    assert await winner_store.release_retention_worker_lease(
        owner_id=winner,
    ) is False
    assert await loser_store.release_retention_worker_lease(owner_id=loser) is True


@pytest.mark.asyncio
async def test_two_periodic_services_execute_only_one_cycle_under_real_lease(
    tmp_path: Path,
) -> None:
    store_a = HarnessStore(tmp_path / "harness.db")
    store_b = HarnessStore(tmp_path / "harness.db")
    calls: list[str] = []

    def result() -> SessionRetentionPassResult:
        return SessionRetentionPassResult(
            status=RetentionPassStatus.COMPLETED,
            planned_count=0,
            attempted_count=0,
            completed_count=0,
            retry_scheduled_count=0,
            retry_exhausted_count=0,
            policy_blocked_count=0,
            not_found_count=0,
            error_count=0,
            remaining_count=0,
            planned_bytes=0,
            duration_seconds=0,
            results=(),
            message="空轮",
        )

    async def run_a(_: asyncio.Event) -> SessionRetentionPassResult:
        calls.append("a")
        return result()

    async def run_b(_: asyncio.Event) -> SessionRetentionPassResult:
        calls.append("b")
        return result()

    policy = RetentionPeriodicPolicy(jitter_ratio=0)
    first = SessionRetentionPeriodicService(
        owner_id="worker-a",
        lease_port=store_a,
        run_pass=run_a,
        policy=policy,
    )
    second = SessionRetentionPeriodicService(
        owner_id="worker-b",
        lease_port=store_b,
        run_pass=run_b,
        policy=policy,
    )

    results = await asyncio.gather(first.run_cycle(), second.run_cycle())

    assert sum(item is not None for item in results) == 1
    assert len(calls) == 1
    await first.stop()
    await second.stop()
