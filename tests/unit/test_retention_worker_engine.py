"""Engine lifecycle wiring for HAR-06.5b2a retention worker core."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.retention_periodic import RetentionWorkerState
from naumi_agent.orchestrator.engine import AgentEngine


def _engine(tmp_path, *, enabled: bool) -> AgentEngine:
    return AgentEngine(
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
