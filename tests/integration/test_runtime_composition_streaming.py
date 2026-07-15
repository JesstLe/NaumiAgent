from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.config.settings import (
    AppConfig,
    MemoryConfig,
    ModelConfig,
    SafetyConfig,
)
from naumi_agent.memory.session import SessionStore
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.runtime.dependencies import RuntimePortOverrides
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.sinks import NullEventSink


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_root_composed_engine_runs_tool_persists_receipt_and_closes_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readable = tmp_path / "proof.txt"
    readable.write_text("composition-root-proof", encoding="utf-8")
    config = AppConfig(
        workspace_root=str(tmp_path),
        models=ModelConfig(),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
        safety=SafetyConfig(
            permission_mode="bypass",
            allowed_dirs=[str(tmp_path)],
        ),
    )
    model = ModelRouter(config.models)
    events = _RecordingSink()
    engine = create_agent_engine(
        config,
        port_overrides=RuntimePortOverrides(
            model_port=model,
            event_sink=events,
        ),
    )
    assert isinstance(engine.session_store, SessionStore)
    call_count = 0

    async def stream_response(**_: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "read-proof",
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": json.dumps({"path": str(readable)}),
                        },
                    }
                },
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(token="Composition Root 流式完成")
        yield StreamChunk(
            finish_reason="stop",
            usage=TokenUsage(
                input_tokens=5,
                output_tokens=6,
                total_tokens=11,
                cost_usd=0.001,
            ),
        )

    monkeypatch.setattr(model, "stream", stream_response)
    try:
        result = await engine.run_streaming(
            "读取 proof.txt 后确认结果",
            NullEventSink(),
        )

        assert result.status == "completed"
        assert result.response == "Composition Root 流式完成"
        assert result.receipt is not None
        assert call_count == 2
        assert engine._session is not None
        saved = await engine.session_store.load(engine._session.id)
        assert saved is not None
        assert any(
            message.get("role") == "tool"
            and "composition-root-proof" in str(message.get("content", ""))
            for message in saved.messages
        )
        assert [event.sequence for event in events.events] == list(
            range(1, len(events.events) + 1)
        )
        assert any(
            event.type is RuntimeEventType.TOOL_START
            for event in events.events
        )
        assert any(
            event.type is RuntimeEventType.TOOL_END
            for event in events.events
        )
        assert sum(
            event.type is RuntimeEventType.COMPLETION_RECEIPT
            for event in events.events
        ) == 1
        assert engine.session_store._db is not None
    finally:
        await engine.shutdown()

    assert engine.session_store._db is None
