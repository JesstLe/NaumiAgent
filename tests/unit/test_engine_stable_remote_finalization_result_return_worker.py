from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator import engine as engine_module
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_ports,
    build_runtime_resources,
    build_runtime_services,
)
from naumi_agent.runtime.services import RuntimeServiceOverrides


class _BoundResultTransport:
    async def submit(self, **_kwargs):
        raise AssertionError("empty target journal must not invoke Result transport")


@pytest.mark.asyncio
async def test_engine_composes_and_shuts_down_bound_result_return_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release-root"
    monkeypatch.setattr(engine_module, "default_release_root", lambda: release_root)
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )
    paths = build_runtime_paths(config)
    ports = build_runtime_ports(config, paths=paths)
    resources = build_runtime_resources(paths)
    transport = _BoundResultTransport()
    services = build_runtime_services(
        config,
        paths=paths,
        resources=resources,
        overrides=RuntimeServiceOverrides(
            stable_remote_finalization_result_transport=transport
        ),
    )
    assert services.stable_remote_finalization_result_transport is transport
    engine = AgentEngine(
        config,
        ports=ports,
        paths=paths,
        resources=resources,
        services=services,
    )
    worker = engine.evolution_stable_remote_finalization_result_return_worker
    assert worker is not None
    result = await engine.run_stable_remote_finalization_result_return_once()
    assert result.claimed == result.synchronized == 0
    assert worker.start()

    await engine.shutdown()

    assert engine.stable_remote_finalization_result_return_worker_snapshot().state == (
        "stopped"
    )
