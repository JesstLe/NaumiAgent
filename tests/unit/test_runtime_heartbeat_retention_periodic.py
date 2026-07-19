from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.heartbeat_retention_periodic import (
    RuntimeHeartbeatRetentionPolicy,
    RuntimeHeartbeatRetentionService,
    RuntimeHeartbeatRetentionState,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore

NOW = datetime(2026, 7, 20, tzinfo=UTC)


async def _record(
    store,
    workspace,
    subject_id,
    observed_at,
    *,
    phase=HarnessHeartbeatPhase.RUNNING,
):
    await store.record_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=subject_id,
        instance_id=f"instance-{subject_id}",
        epoch=1,
        sequence=1,
        phase=phase,
        observed_at=observed_at,
        timeout_seconds=30,
        detail_code="runtime_alive",
    )


@pytest.mark.asyncio
async def test_cycle_composes_catalog_lease_and_bounded_prune(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _record(store, workspace, "old-runtime", "2026-07-10T00:00:00+00:00")
    await _record(
        store,
        workspace,
        "protected-terminal",
        "2026-07-10T00:00:00+00:00",
        phase=HarnessHeartbeatPhase.STOPPED,
    )
    await _record(store, workspace, "current-runtime", "2026-07-20T00:00:00+00:00")
    service = RuntimeHeartbeatRetentionService(
        port=store,
        workspace_root=workspace,
        owner_id="retention-a",
        protected_subject_ids=lambda: ("protected-terminal",),
        now=lambda: NOW,
        policy=RuntimeHeartbeatRetentionPolicy(
            retention_seconds=259_200,
            interval_seconds=60,
            standby_retry_seconds=5,
            lease_seconds=30,
            scan_limit=10,
        ),
    )

    receipt = await service.run_cycle()
    assert receipt is not None
    assert receipt.deleted_subject_ids == ("old-runtime",)
    assert (
        await store.get_heartbeat(
            workspace_root=workspace,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id="protected-terminal",
        )
        is not None
    )
    assert (
        await store.get_heartbeat(
            workspace_root=workspace,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id="current-runtime",
        )
        is not None
    )
    snapshot = service.snapshot()
    assert snapshot.state is RuntimeHeartbeatRetentionState.WAITING
    assert snapshot.cycle_count == 1
    assert snapshot.deleted_count == 1


@pytest.mark.asyncio
async def test_cycle_never_prunes_after_lease_loss(tmp_path) -> None:
    port = AsyncMock()
    port.acquire_run_lease.return_value = SimpleNamespace(epoch=2)
    port.list_runtime_heartbeats.return_value = SimpleNamespace(items=())
    port.renew_run_lease.return_value = None
    service = RuntimeHeartbeatRetentionService(
        port=port,
        workspace_root=tmp_path,
        owner_id="retention-a",
        now=lambda: NOW,
    )

    assert await service.run_cycle() is None
    port.prune_runtime_heartbeats.assert_not_awaited()
    assert service.snapshot().last_error_code == "lease_lost"
    port.release_run_lease.assert_awaited_once()


@pytest.mark.asyncio
async def test_standby_and_failure_snapshots_do_not_expose_raw_errors(tmp_path) -> None:
    standby = AsyncMock()
    standby.acquire_run_lease.return_value = None
    service = RuntimeHeartbeatRetentionService(
        port=standby,
        workspace_root=tmp_path,
        owner_id="standby",
        now=lambda: NOW,
    )
    assert await service.run_cycle() is None
    assert service.snapshot().state is RuntimeHeartbeatRetentionState.STANDBY

    failing = AsyncMock()
    failing.acquire_run_lease.side_effect = RuntimeError("secret database path")
    failed = RuntimeHeartbeatRetentionService(
        port=failing,
        workspace_root=tmp_path,
        owner_id="failed",
        now=lambda: NOW,
    )
    assert await failed.run_cycle() is None
    assert failed.snapshot().last_error_code == "lease_acquire_failed"
    assert "secret" not in failed.snapshot().last_error_code


def test_policy_rejects_retention_shorter_than_offline_safety_window() -> None:
    with pytest.raises(ValueError, match="3 天"):
        RuntimeHeartbeatRetentionPolicy(retention_seconds=259_199)
    with pytest.raises(ValueError, match="scan limit"):
        RuntimeHeartbeatRetentionPolicy(scan_limit=True)
    with pytest.raises(ValueError, match="owner_id"):
        RuntimeHeartbeatRetentionService(
            port=AsyncMock(),
            workspace_root=".",
            owner_id="bad owner",
        )


@pytest.mark.asyncio
async def test_background_lifecycle_is_idempotent_and_interrupts_wait(tmp_path) -> None:
    port = AsyncMock()
    port.acquire_run_lease.return_value = None
    service = RuntimeHeartbeatRetentionService(
        port=port,
        workspace_root=tmp_path,
        owner_id="lifecycle",
        now=lambda: NOW,
        policy=RuntimeHeartbeatRetentionPolicy(
            interval_seconds=60,
            standby_retry_seconds=60,
        ),
    )

    assert service.start() is True
    assert service.start() is False
    await asyncio.wait_for(port.acquire_run_lease.wait(), timeout=1)
    assert await asyncio.wait_for(service.stop(), timeout=1) is True
    assert await service.stop() is False
    assert service.snapshot().state is RuntimeHeartbeatRetentionState.STOPPED


@pytest.mark.asyncio
async def test_invalid_clock_becomes_sanitized_failure_instead_of_crashing(tmp_path) -> None:
    service = RuntimeHeartbeatRetentionService(
        port=AsyncMock(),
        workspace_root=tmp_path,
        owner_id="bad-clock",
        now=lambda: datetime(2026, 7, 20),
    )

    assert await service.run_cycle() is None
    snapshot = service.snapshot()
    assert snapshot.state is RuntimeHeartbeatRetentionState.FAILED
    assert snapshot.last_error_code == "lease_acquire_failed"
