"""Focused AgentEngine EventSink injection contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.ports.events import RuntimeEvent
from naumi_agent.streaming.event_bus import EventEmitter
from naumi_agent.streaming.sinks import NullEventSink


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


class _FalseySink(_RecordingSink):
    def __bool__(self) -> bool:
        return False


class _IncompleteSink:
    pass


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
async def test_engine_exposes_injected_event_sink_identity(tmp_path: Path) -> None:
    sink = _RecordingSink()
    engine = AgentEngine(_config(tmp_path), event_sink=sink)
    try:
        assert engine.event_sink is sink
        assert engine._event_sink is sink
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_preserves_explicit_falsey_event_sink(tmp_path: Path) -> None:
    sink = _FalseySink()
    engine = AgentEngine(_config(tmp_path), event_sink=sink)
    try:
        assert engine.event_sink is sink
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_uses_null_sink_by_default_and_keeps_legacy_emitter(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    try:
        assert isinstance(engine.event_sink, NullEventSink)
        assert isinstance(engine.emitter, EventEmitter)
    finally:
        await engine.shutdown()


def test_engine_rejects_incomplete_sink_before_runtime_io(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="event_sink 必须实现完整的 EventSink 契约"):
        AgentEngine(
            _config(tmp_path),
            event_sink=_IncompleteSink(),  # type: ignore[arg-type]
        )

    assert not (tmp_path / ".naumi").exists()
