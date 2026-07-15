from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.composition import build_runtime_ports
from naumi_agent.streaming.sinks import NullEventSink


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


@pytest.mark.asyncio
async def test_engine_consumes_one_complete_port_bundle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ports = build_runtime_ports(config)
    engine = AgentEngine(config, ports=ports)
    try:
        assert engine.session_store is ports.session_port
        assert engine._permission_checker is ports.permission_port
        assert engine.router is ports.model_port
        assert engine.tool_executor is ports.tool_execution_port
        assert engine.event_sink is ports.event_sink
    finally:
        await engine.shutdown()


def test_engine_rejects_bundle_plus_legacy_override(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ports = build_runtime_ports(config)

    with pytest.raises(
        TypeError,
        match="ports 与单独 Port 参数不能同时提供",
    ):
        AgentEngine(config, ports=ports, event_sink=NullEventSink())


def test_engine_rejects_non_bundle_before_runtime_io(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(TypeError, match="ports 必须是完整的 RuntimePorts"):
        AgentEngine(config, ports=object())  # type: ignore[arg-type]

    assert not (tmp_path / ".naumi").exists()


@pytest.mark.asyncio
async def test_legacy_default_path_delegates_to_composition_builder(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ports = build_runtime_ports(config)
    with patch(
        "naumi_agent.runtime.composition.build_runtime_ports",
        return_value=ports,
    ) as build:
        engine = AgentEngine(config)
    try:
        build.assert_called_once()
        overrides = build.call_args.kwargs["overrides"]
        assert overrides.session_port is None
        assert overrides.permission_port is None
        assert overrides.model_port is None
        assert overrides.tool_execution_port is None
        assert overrides.event_sink is None
        assert engine.event_sink is ports.event_sink
    finally:
        await engine.shutdown()
