"""Focused tests for the periodic Agent publication recovery worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from naumi_agent.agents.base import AgentResult
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.daemons.agent_jobs import AgentJobError, AgentJobPublicationState
from naumi_agent.orchestrator.agent_publication_worker import (
    AgentPublicationRecoveryWorker,
    AgentPublicationWorkerPolicy,
    AgentPublicationWorkerState,
)
from naumi_agent.orchestrator.subagent_manager import (
    AgentPublicationRecoverySummary,
    SubTask,
)
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


def _manager(tmp_path):
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                long_term_enabled=False,
            ),
            harness={
                "agent_publication_recovery": {"enabled": False},
                "pursuit_terminal_outbox": {"enabled": False},
            },
        )
    )
    return engine, engine.subagent_manager


def test_policy_rejects_unbounded_or_inconsistent_values() -> None:
    with pytest.raises(ValueError, match="scan limit"):
        AgentPublicationWorkerPolicy(scan_limit=0)
    with pytest.raises(ValueError, match="空轮退避"):
        AgentPublicationWorkerPolicy(
            interval_seconds=10,
            max_empty_backoff_seconds=5,
        )
    with pytest.raises(ValueError, match="失败退避"):
        AgentPublicationWorkerPolicy(
            interval_seconds=10,
            max_failure_backoff_seconds=5,
        )
    with pytest.raises(ValueError, match="jitter"):
        AgentPublicationWorkerPolicy(jitter_ratio=0.6)


@pytest.mark.asyncio
async def test_run_once_serializes_passes_and_applies_bounded_backoff(
    tmp_path,
) -> None:
    engine, manager = _manager(tmp_path)
    release = asyncio.Event()
    first_started = asyncio.Event()
    active = 0
    peak = 0
    results = iter(
        (
            AgentPublicationRecoverySummary(),
            AgentPublicationRecoverySummary(
                scanned=1,
                failed=1,
                failure_codes=("delivery_failed",),
            ),
        )
    )

    async def recover(*, limit: int) -> AgentPublicationRecoverySummary:
        nonlocal active, peak
        assert limit == 7
        active += 1
        peak = max(peak, active)
        first_started.set()
        await release.wait()
        active -= 1
        return next(results)

    manager.recover_pending_publications = recover  # type: ignore[method-assign]
    worker = AgentPublicationRecoveryWorker(
        manager=manager,
        policy=AgentPublicationWorkerPolicy(
            interval_seconds=10,
            max_empty_backoff_seconds=40,
            max_failure_backoff_seconds=80,
            scan_limit=7,
            jitter_ratio=0,
        ),
        now=lambda: datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )
    try:
        first = asyncio.create_task(worker.run_once())
        await first_started.wait()
        second = asyncio.create_task(worker.run_once())
        await asyncio.sleep(0)
        assert peak == 1
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
    finally:
        await engine.shutdown()

    assert first_result.scanned == 0
    assert second_result.failed == 1
    assert peak == 1
    snapshot = worker.snapshot()
    assert snapshot.pass_count == 2
    assert snapshot.scanned_count == 1
    assert snapshot.failure_count == 1
    assert snapshot.consecutive_empty_passes == 0
    assert snapshot.consecutive_failure_passes == 1
    assert snapshot.next_delay_seconds == 10
    assert snapshot.last_failure_codes == ("delivery_failed",)


@pytest.mark.asyncio
async def test_start_waits_until_wake_and_stop_drains_worker(tmp_path) -> None:
    engine, manager = _manager(tmp_path)
    called = asyncio.Event()

    async def recover(*, limit: int) -> AgentPublicationRecoverySummary:
        assert limit == 3
        called.set()
        return AgentPublicationRecoverySummary(scanned=1, delivered=1)

    manager.recover_pending_publications = recover  # type: ignore[method-assign]
    worker = AgentPublicationRecoveryWorker(
        manager=manager,
        policy=AgentPublicationWorkerPolicy(
            interval_seconds=3600,
            max_empty_backoff_seconds=3600,
            max_failure_backoff_seconds=3600,
            scan_limit=3,
            jitter_ratio=0,
        ),
    )
    try:
        assert worker.start() is True
        assert worker.start() is False
        assert worker.snapshot().state is AgentPublicationWorkerState.WAITING
        assert called.is_set() is False
        assert worker.wake() is True
        await asyncio.wait_for(called.wait(), timeout=2)
        assert worker.snapshot().state is AgentPublicationWorkerState.WAITING
        assert await worker.stop() is True
        assert worker.snapshot().state is AgentPublicationWorkerState.STOPPED
        assert await worker.stop() is False
        assert worker.wake() is False
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_uncaught_pass_failure_is_sanitized_and_backed_off(tmp_path) -> None:
    engine, manager = _manager(tmp_path)
    manager.recover_pending_publications = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("raw secret must not escape")
    )
    worker = AgentPublicationRecoveryWorker(
        manager=manager,
        policy=AgentPublicationWorkerPolicy(
            interval_seconds=5,
            max_empty_backoff_seconds=20,
            max_failure_backoff_seconds=20,
            jitter_ratio=0,
        ),
    )
    try:
        first = await worker.run_once()
        second = await worker.run_once()
    finally:
        await engine.shutdown()

    assert first.failure_codes == ("agent_publication_worker_pass_failed",)
    assert second.failure_codes == ("agent_publication_worker_pass_failed",)
    snapshot = worker.snapshot()
    assert snapshot.failure_count == 2
    assert snapshot.consecutive_failure_passes == 2
    assert snapshot.next_delay_seconds == 10
    assert "secret" not in repr(snapshot)


@pytest.mark.asyncio
async def test_real_worker_pass_recovers_post_commit_publication_gap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_engine, first = _manager(tmp_path)
    agent = first.get_agent("coder")
    assert agent is not None

    async def execute(**_: object) -> AgentResult:
        return AgentResult(
            status="completed",
            response="periodic-recovery-result",
            total_tokens=9,
            total_cost_usd=0.001,
            turns=1,
        )

    async def fail_live_delivery(_: object) -> object:
        raise AgentJobError("injected post-commit publication gap")

    monkeypatch.setattr(agent, "execute", execute)
    monkeypatch.setattr(first, "_deliver_execution_publication", fail_live_delivery)
    result = await first.delegate(SubTask("periodic-publication", "work", "coder"))
    record = next(
        item for item in first.list_executions()
        if item.task_id == "periodic-publication"
    )
    pending = await first._agent_job_store.get_job_publication(  # noqa: SLF001
        record.worker_job_id
    )
    assert result.status == "completed"
    assert pending is not None
    assert pending.state is AgentJobPublicationState.PENDING
    await first_engine.shutdown()

    second_engine, second = _manager(tmp_path)
    worker = AgentPublicationRecoveryWorker(
        manager=second,
        policy=AgentPublicationWorkerPolicy(jitter_ratio=0),
    )
    try:
        recovered = await worker.run_once()
        delivered = await second._agent_job_store.get_publication(  # noqa: SLF001
            pending.publication_id
        )
        inbox = await second._agent_job_store.list_result_inbox("")  # noqa: SLF001
    finally:
        await second_engine.shutdown()

    assert recovered.scanned == 1
    assert recovered.delivered == 1
    assert recovered.failed == 0
    assert delivered is not None
    assert delivered.state is AgentJobPublicationState.PUBLISHED
    assert len(inbox) == 1
