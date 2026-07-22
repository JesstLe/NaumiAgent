from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.store import HarnessStore
from naumi_agent.runtime.browser_heartbeat import (
    BrowserExecutionHeartbeatFactory,
    BrowserExecutionHeartbeatState,
)


def _factory(tmp_path, *, store: HarnessStore | None = None):
    current = datetime(2026, 7, 23, tzinfo=UTC)

    def now() -> str:
        nonlocal current
        value = current.isoformat()
        current += timedelta(seconds=1)
        return value

    return BrowserExecutionHeartbeatFactory(
        store=store or HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        interval_seconds=1,
        timeout_seconds=3,
        now_provider=now,
        auto_pulse=False,
    )


@pytest.mark.asyncio
async def test_browser_lifecycle_keeps_waiting_live_and_resumes(tmp_path) -> None:
    factory = _factory(tmp_path)
    lifecycle = await factory.create(run_id="run-1")

    assert await lifecycle.start() is True
    assert await lifecycle.enter_waiting(mode="instruction") is True
    waiting = await lifecycle._producer.pulse_now()
    assert waiting.phase is HarnessHeartbeatPhase.WAITING
    assert waiting.detail_code == "browser_waiting_alive"

    assert await lifecycle.enter_waiting(mode="manual_control") is True
    manual = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=lifecycle.snapshot().subject_id,
    )
    assert manual is not None
    assert manual.detail_code == "browser_manual_control"

    assert await lifecycle.resume() is True
    assert lifecycle.snapshot().state is BrowserExecutionHeartbeatState.RUNNING
    assert lifecycle.snapshot().phase == "running"
    assert await lifecycle.finish("completed") is True
    assert await lifecycle.finish("completed") is False

    terminal = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=lifecycle.snapshot().subject_id,
    )
    assert terminal is not None
    assert terminal.phase is HarnessHeartbeatPhase.STOPPED
    assert terminal.detail_code == "browser_completed"
    assert terminal.sequence == 8


@pytest.mark.asyncio
async def test_factory_isolates_runs_and_advances_epoch_for_recovery(tmp_path) -> None:
    factory = _factory(tmp_path)
    first = await factory.create(run_id="run-stable")
    other = await factory.create(run_id="run-other")

    assert first.snapshot().subject_id != other.snapshot().subject_id
    assert "run-stable" not in first.snapshot().subject_id
    assert first.snapshot().epoch == 1
    await first.start()
    await first.finish("failed")

    retry = await factory.create(run_id="run-stable")
    assert retry.snapshot().subject_id == first.snapshot().subject_id
    assert retry.snapshot().epoch == 2
    await retry.start()
    await retry.finish("aborted")

    interrupted = await factory.record_interrupted(run_id="run-stable")
    assert interrupted.epoch == 3
    assert interrupted.state is BrowserExecutionHeartbeatState.FAILED
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=interrupted.subject_id,
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.FAILED
    assert heartbeat.detail_code == "browser_runtime_interrupted"


@pytest.mark.asyncio
async def test_start_failure_is_sanitized_without_leaking_store_error(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    factory = _factory(tmp_path, store=store)
    lifecycle = await factory.create(run_id="run-private")
    store.record_heartbeat = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("/private/secret/browser-profile")
    )

    with pytest.raises(OSError, match="private/secret"):
        await lifecycle.start()

    snapshot = lifecycle.snapshot()
    assert snapshot.state is BrowserExecutionHeartbeatState.FAILED
    assert snapshot.failure_code == "browser_heartbeat_start_failed"
    assert "private" not in snapshot.failure_code


def test_factory_rejects_invalid_cadence_store_and_run_id(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    with pytest.raises(TypeError, match="HarnessStore"):
        BrowserExecutionHeartbeatFactory(  # type: ignore[arg-type]
            store=object(),
            workspace_root=tmp_path,
        )
    with pytest.raises(ValueError, match="interval"):
        BrowserExecutionHeartbeatFactory(
            store=store,
            workspace_root=tmp_path,
            interval_seconds=3,
            timeout_seconds=3,
        )


@pytest.mark.asyncio
async def test_factory_rejects_empty_run_id(tmp_path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        await _factory(tmp_path).create(run_id="  ")
