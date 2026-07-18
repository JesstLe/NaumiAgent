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
from naumi_agent.harness.feedback import build_direct_user_feedback
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
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
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
    assert engine._paths.workspace_root == tmp_path.resolve()
    assert engine._paths.runtime_data_dir == (tmp_path / ".naumi").resolve()
    assert engine._paths.browser_data_dir == engine._paths.runtime_data_dir / "browser"
    assert engine._harness_store is engine._resources.harness_store
    assert engine.chat_run_store is engine._resources.chat_run_store
    assert engine.task_store is engine._resources.task_store
    assert engine.workbench_store is engine._resources.workbench_store
    assert engine.workbench_service._task_store is engine._resources.task_store
    assert (
        engine.workbench_service._workbench_store
        is engine._resources.workbench_store
    )
    assert (
        engine.evolution_candidate_store
        is engine._resources.evolution_candidate_store
    )
    assert engine.harness_service._trust_store is engine._resources.harness_trust_store
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
        assert engine._resources.chat_run_store.db_path.exists()
        await engine._resources.harness_store.record_profile(
            workspace_root=tmp_path,
            profile_digest="a" * 64,
            schema_version=1,
            loaded_at="2026-07-18T00:00:00+00:00",
            trusted_at="",
            trust_source="composition-test",
            status="untrusted",
        )
        assert engine._resources.harness_store.db_path.exists()
        feedback = build_direct_user_feedback(
            session_id=engine._session.id,
            category="defect",
            scope="runtime:composition",
            topic="resource_identity",
            summary="验证 Composition Root 共享 Evolution Store",
            provider="test",
            model="test/model",
            platform="test",
        )
        recorded = await engine.feedback_intake_service.ingest(tmp_path, feedback)
        assert recorded.status == "recorded"
        assert engine._resources.evolution_candidate_store.db_path.exists()
        task = await engine.task_store.create_task("验证共享任务数据库")
        mission = await engine.workbench_service.create_mission(
            session_id=engine._session.id,
            title="Composition Resource 验证",
            goal="证明 Task 与 Workbench 使用同一注入数据库",
        )
        assert task.session_id == engine._session.id
        assert mission.session_id == engine._session.id
        assert engine.task_store.db_path == engine.workbench_store.db_path
    finally:
        await engine.shutdown()

    assert engine.session_store._db is None
