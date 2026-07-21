"""Engine lifecycle wiring for HAR-06.5b2a retention worker core."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.retention_periodic import RetentionWorkerState
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.composition import create_agent_engine


def _engine(tmp_path, *, enabled: bool) -> AgentEngine:
    return create_agent_engine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                long_term_enabled=False,
                session_retention={"periodic_enabled": enabled},
            ),
        )
    )


@pytest.mark.asyncio
async def test_worker_is_default_off_and_engine_refuses_implicit_start(tmp_path) -> None:
    engine = _engine(tmp_path, enabled=False)
    engine._retention_periodic_service.start = MagicMock(return_value=True)
    try:
        assert engine.start_session_retention_worker() is False
        engine._retention_periodic_service.start.assert_not_called()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_worker_status_is_json_safe_and_reports_config_gate(tmp_path) -> None:
    engine = _engine(tmp_path, enabled=False)
    try:
        status = engine.session_retention_worker_status()
    finally:
        await engine.shutdown()

    assert status["configured_enabled"] is False
    assert status["state"] == "stopped"
    assert status["lease_held"] is False
    assert status["pass_count"] == 0


@pytest.mark.asyncio
async def test_enabled_worker_delegates_start_wake_status_and_shutdown(tmp_path) -> None:
    engine = _engine(tmp_path, enabled=True)
    snapshot = SimpleNamespace(state=RetentionWorkerState.WAITING)
    engine._retention_periodic_service.start = MagicMock(return_value=True)
    engine._retention_periodic_service.wake = MagicMock(return_value=True)
    engine._retention_periodic_service.snapshot = MagicMock(return_value=snapshot)
    engine._retention_periodic_service.stop = AsyncMock(return_value=True)

    assert engine.start_session_retention_worker() is True
    assert engine.wake_session_retention_worker() is True
    assert engine.session_retention_worker_snapshot() is snapshot
    await engine.shutdown()

    engine._retention_periodic_service.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_long_running_startup_recovers_before_starting_worker(tmp_path) -> None:
    engine = _engine(tmp_path, enabled=True)
    order: list[str] = []

    async def recover_patches():
        order.append("patch")
        return ()

    async def recover_patch_sets():
        order.append("patch_set")
        return ()

    async def recover():
        order.append("recover")
        return ()

    def start() -> bool:
        order.append("start")
        return True

    engine.evolution_patch_recovery.recover_pending = recover_patches  # type: ignore[method-assign]
    engine.evolution_patch_set_recovery.recover_pending = recover_patch_sets  # type: ignore[method-assign]
    engine.recover_session_reconciliations = recover  # type: ignore[method-assign]
    engine.start_session_retention_worker = start  # type: ignore[method-assign]
    try:
        recovered = await engine.start_long_running_services()
    finally:
        await engine.shutdown()

    assert recovered == ()
    assert order == ["patch_set", "patch", "recover", "start"]


@pytest.mark.asyncio
async def test_long_running_startup_does_not_start_worker_after_recovery_failure(
    tmp_path,
) -> None:
    engine = _engine(tmp_path, enabled=True)
    engine.recover_session_reconciliations = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("broken recovery")
    )
    engine.start_session_retention_worker = MagicMock(  # type: ignore[method-assign]
        return_value=True
    )
    try:
        with pytest.raises(RuntimeError, match="broken recovery"):
            await engine.start_long_running_services()
        engine.start_session_retention_worker.assert_not_called()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_patch_recovery_status_aggregates_single_and_multi_transactions(
    tmp_path,
) -> None:
    engine = _engine(tmp_path, enabled=False)
    engine._last_evolution_patch_recovery = (
        SimpleNamespace(
            status="already_baseline",
            failure_code="",
            filesystem_changed=False,
            recovery_complete=True,
        ),
    )
    engine._last_evolution_patch_set_recovery = (
        SimpleNamespace(
            status="rolled_back",
            failure_code="recovered_after_replace",
            filesystem_changed=True,
            recovery_complete=True,
        ),
        SimpleNamespace(
            status="failed",
            failure_code="target_digest_unknown",
            filesystem_changed=False,
            recovery_complete=False,
        ),
    )
    try:
        status = engine.evolution_patch_recovery_status()
    finally:
        await engine.shutdown()

    assert status == {
        "total": 3,
        "single_file_total": 1,
        "multi_file_total": 2,
        "completed": 2,
        "rolled_back": 1,
        "already_baseline": 1,
        "orphan_lock_removed": 0,
        "deferred": 0,
        "failed": 1,
        "filesystem_changed": 1,
        "failure_codes": ["recovered_after_replace", "target_digest_unknown"],
    }
