from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig, RuntimeHeartbeatRetentionConfig
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.store import HarnessStore
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_resources,
    build_runtime_services,
    create_agent_engine,
)
from naumi_agent.runtime.services import RuntimeServiceOverrides, RuntimeServices
from naumi_agent.runtime.terminal_runtime import (
    TerminalRuntimeLifecycleFactory,
    TerminalRuntimeState,
)


def _config(tmp_path, *, retention_enabled: bool = False) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory={
            "session_db_path": str(tmp_path / ".naumi" / "sessions.db"),
            "vector_db_path": str(tmp_path / ".naumi" / "chroma"),
            "long_term_enabled": False,
        },
        harness={
            "runtime_heartbeat_retention": {
                "enabled": retention_enabled,
                "retention_days": 3,
                "interval_seconds": 60,
                "standby_retry_seconds": 1,
                "lease_seconds": 30,
                "scan_limit": 10,
                "catalog_limit": 10,
            }
        },
    )


def _factory(tmp_path, *, enabled: bool = False) -> TerminalRuntimeLifecycleFactory:
    ticks = iter(range(20))
    return TerminalRuntimeLifecycleFactory(
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        retention_config=RuntimeHeartbeatRetentionConfig(enabled=enabled),
        heartbeat_interval_seconds=1,
        heartbeat_timeout_seconds=3,
        now_provider=lambda: datetime(
            2026,
            7,
            20,
            0,
            0,
            next(ticks),
            tzinfo=UTC,
        ).isoformat(),
    )


def test_composition_builds_service_from_exact_resources_and_copies_policy(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    config = _config(tmp_path, retention_enabled=True)
    paths = build_runtime_paths(config)
    resources = build_runtime_resources(paths)

    services = build_runtime_services(
        config,
        paths=paths,
        resources=resources,
    )
    factory = services.terminal_runtime_lifecycle_factory
    config.harness.runtime_heartbeat_retention.enabled = False

    assert factory.store is resources.harness_store
    assert factory.workspace_root == paths.workspace_root
    assert factory.retention_config.enabled is True
    assert not paths.harness_db_path.exists()


def test_service_override_identity_and_invalid_bundle_fail_closed(tmp_path) -> None:
    config = _config(tmp_path)
    paths = build_runtime_paths(config)
    resources = build_runtime_resources(paths)
    factory = _factory(tmp_path)

    services = build_runtime_services(
        config,
        paths=paths,
        resources=resources,
        overrides=RuntimeServiceOverrides(
            terminal_runtime_lifecycle_factory=factory
        ),
    )
    assert services.terminal_runtime_lifecycle_factory is factory

    with pytest.raises(TypeError, match="TerminalRuntimeLifecycleFactory"):
        RuntimeServices(
            terminal_runtime_lifecycle_factory=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="RuntimeServiceOverrides"):
        build_runtime_services(
            config,
            paths=paths,
            resources=resources,
            overrides=object(),  # type: ignore[arg-type]
        )


def test_root_factory_preserves_service_override_in_engine(tmp_path) -> None:
    factory = _factory(tmp_path)
    engine = create_agent_engine(
        _config(tmp_path),
        service_overrides=RuntimeServiceOverrides(
            terminal_runtime_lifecycle_factory=factory
        ),
    )

    assert engine.terminal_runtime_lifecycle_factory is factory
    assert engine._services.terminal_runtime_lifecycle_factory is factory


@pytest.mark.asyncio
async def test_shared_terminal_lifecycle_persists_real_graceful_boundaries(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    lifecycle = factory.create(surface="tui", identity="tui-contract-test")

    assert await lifecycle.start() is True
    assert await lifecycle.start() is False
    assert lifecycle.snapshot().state is TerminalRuntimeState.RUNNING
    assert lifecycle.snapshot().heartbeat_phase == "running"
    assert await lifecycle.begin_draining() is True
    assert lifecycle.snapshot().state is TerminalRuntimeState.DRAINING
    assert await lifecycle.close() is True
    assert await lifecycle.close() is False
    assert lifecycle.snapshot().state is TerminalRuntimeState.STOPPED

    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="tui-contract-test",
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.STOPPED
    assert heartbeat.sequence == 4


@pytest.mark.asyncio
async def test_lifecycle_continues_draining_when_optional_retention_stop_fails(
    tmp_path,
) -> None:
    factory = _factory(tmp_path, enabled=True)
    lifecycle = factory.create(surface="new_ui", identity="new-ui-retention-failure")
    await lifecycle.start()
    assert lifecycle._retention is not None
    lifecycle._retention.stop = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("private path")
    )

    assert await lifecycle.begin_draining() is True
    snapshot = lifecycle.snapshot()
    assert snapshot.state is TerminalRuntimeState.DRAINING
    assert snapshot.last_error_code == "retention_stop_failed"
    assert "private" not in snapshot.last_error_code
    assert await lifecycle.close() is True


@pytest.mark.asyncio
async def test_retention_start_failure_rolls_back_started_heartbeat(tmp_path) -> None:
    factory = _factory(tmp_path, enabled=True)
    lifecycle = factory.create(surface="tui", identity="tui-start-rollback")
    assert lifecycle._retention is not None
    lifecycle._retention.start = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("private path")
    )

    with pytest.raises(RuntimeError, match="private path"):
        await lifecycle.start()

    snapshot = lifecycle.snapshot()
    assert snapshot.state is TerminalRuntimeState.FAILED
    assert snapshot.last_error_code == "retention_start_failed"
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="tui-start-rollback",
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.STOPPED


@pytest.mark.asyncio
async def test_draining_failure_still_allows_failed_terminal_commit(tmp_path) -> None:
    factory = _factory(tmp_path)
    lifecycle = factory.create(surface="new_ui", identity="new-ui-drain-failure")
    await lifecycle.start()
    lifecycle._producer.begin_draining = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("store unavailable")
    )

    with pytest.raises(OSError, match="store unavailable"):
        await lifecycle.begin_draining()
    assert lifecycle.snapshot().state is TerminalRuntimeState.FAILED
    assert await lifecycle.close(failed=True) is True
    assert await lifecycle.close(failed=True) is False
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="new-ui-drain-failure",
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.FAILED


def test_factory_rejects_unknown_surface_and_unsafe_identity(tmp_path) -> None:
    factory = _factory(tmp_path)
    with pytest.raises(ValueError, match="surface"):
        factory.create(surface="legacy")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identity"):
        factory.create(surface="tui", identity="bad identity")
