"""End-to-end contracts for AgentEngine's durable Runtime event pipeline."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import (
    AppConfig,
    MemoryConfig,
    RuntimeHeartbeatRetentionConfig,
)
from naumi_agent.orchestrator.engine import AgentEngine, AgentResult
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.runtime.terminal_runtime import (
    TerminalRuntimeLifecycleFactory,
    terminal_run_release_context,
)
from naumi_agent.streaming.publisher import RuntimeEventPublisher
from tests.unit.test_harness_runtime_release_binding import _managed_identity


class _RecordingSink:
    def __init__(self, name: str, trace: list[tuple[str, RuntimeEvent]]) -> None:
        self.name = name
        self.trace = trace

    async def emit(self, event: RuntimeEvent) -> None:
        self.trace.append((self.name, event))


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
async def test_run_streaming_delivers_one_identity_to_base_and_caller_sinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[tuple[str, RuntimeEvent]] = []
    engine = AgentEngine(_config(tmp_path), event_sink=_RecordingSink("base", trace))

    async def fake_core(
        task: str,
        events: RuntimeEventPublisher,
        **_: object,
    ) -> AgentResult:
        assert task == "执行读取"
        await events.publish(RuntimeEventType.RUN_STARTED, {"task": task})
        await events.publish(
            RuntimeEventType.TOOL_START,
            {"name": "file_read", "call_id": "read-1", "args": {"path": "a.txt"}},
        )
        return AgentResult(status="completed", response="读取完成")

    monkeypatch.setattr(engine, "_run_streaming_core", fake_core)
    caller = _RecordingSink("caller", trace)
    try:
        result = await engine.run_streaming("执行读取", caller)
        session = await engine.get_or_create_session()
        assert result.receipt is not None
        restored = await engine.chat_run_store.get_run(session.id, result.receipt.run_id)
        assert restored is not None
        assert restored.receipt == result.receipt
        assert restored.release_provenance is None
        assert restored.steps[-1].event_id == next(
            event.id for name, event in trace
            if name == "caller" and event.type is RuntimeEventType.TOOL_START
        )

        base_events = [event for name, event in trace if name == "base"]
        caller_events = [event for name, event in trace if name == "caller"]
        assert base_events == caller_events
        assert [event.sequence for event in caller_events] == [1, 2, 3]
        assert [event.type for event in caller_events] == [
            RuntimeEventType.RUN_STARTED,
            RuntimeEventType.TOOL_START,
            RuntimeEventType.COMPLETION_RECEIPT,
        ]
        assert [name for name, _ in trace] == [
            "base", "caller", "base", "caller", "base", "caller",
        ]
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_run_streaming_persists_only_current_terminal_release_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    identity_verified_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    factory = TerminalRuntimeLifecycleFactory(
        store=engine.terminal_runtime_lifecycle_factory.store,
        workspace_root=tmp_path,
        retention_config=RuntimeHeartbeatRetentionConfig(enabled=False),
        runtime_identity_provider=lambda: _managed_identity(
            tmp_path,
            verified_at=identity_verified_at,
        ),
    )
    lifecycle = factory.create(
        surface="new_ui",
        identity="new-ui-engine-run",
    )

    async def fake_core(*_: object, **__: object) -> AgentResult:
        engine._usage.total_input_tokens += 31
        engine._usage.total_output_tokens += 7
        engine._usage.cache_tokens += 4
        engine._usage.total_cost_usd += 0.0125
        engine._usage.turns += 1
        return AgentResult(status="completed", response="绑定运行完成")

    monkeypatch.setattr(engine, "_run_streaming_core", fake_core)
    await lifecycle.start()
    try:
        with terminal_run_release_context(lifecycle):
            result = await engine.run_streaming(
                "运行 candidate",
                _RecordingSink("caller", []),
            )
        session = await engine.get_or_create_session()
        assert result.receipt is not None
        restored = await engine.chat_run_store.get_run(
            session.id,
            result.receipt.run_id,
        )
        assert restored is not None and restored.release_provenance is not None
        provenance = restored.release_provenance
        assert provenance.binding == lifecycle.release_binding()
        assert provenance.run_id == result.receipt.run_id
        assert not provenance.execution_outcome_authority
        assert restored.usage is not None
        assert restored.usage.input_tokens == 31
        assert restored.usage.output_tokens == 7
        assert restored.usage.cache_tokens == 4
        assert restored.usage.turns == 1
        assert str(restored.usage.reported_cost_usd) == "0.012500000000"
    finally:
        await lifecycle.close()
        await engine.shutdown()


@pytest.mark.asyncio
async def test_run_streaming_keeps_terminal_receipt_when_usage_delta_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    engine._usage.total_input_tokens = 10

    async def fake_core(*_: object, **__: object) -> AgentResult:
        engine._usage.total_input_tokens = 5
        return AgentResult(status="completed", response="运行仍应完成")

    monkeypatch.setattr(engine, "_run_streaming_core", fake_core)
    try:
        result = await engine.run_streaming(
            "用量来源异常时仍保存回执",
            _RecordingSink("caller", []),
        )
        session = await engine.get_or_create_session()
        assert result.receipt is not None
        restored = await engine.chat_run_store.get_run(
            session.id,
            result.receipt.run_id,
        )
        assert restored is not None
        assert restored.receipt == result.receipt
        assert restored.usage is None
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_run_streaming_persists_one_terminal_receipt_for_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    trace: list[tuple[str, RuntimeEvent]] = []
    engine = AgentEngine(_config(tmp_path))

    async def failing_core(*_: object, **__: object) -> AgentResult:
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("core failed")

    monkeypatch.setattr(engine, "_run_streaming_core", failing_core)
    try:
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected):
            await engine.run_streaming("失败任务", _RecordingSink("caller", trace))

        session = await engine.get_or_create_session()
        runs = await engine.chat_run_store.list_runs(session.id)
        assert len(runs) == 1
        assert runs[0].receipt is not None
        assert runs[0].status == ("cancelled" if failure == "cancel" else "failed")
        receipts = [
            event for _, event in trace
            if event.type is RuntimeEventType.COMPLETION_RECEIPT
        ]
        assert len(receipts) == 1
        assert receipts[0].run_id == runs[0].id
        assert receipts[0].session_id == session.id
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_terminal_sink_failure_cannot_erase_durable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    receipt_was_durable = False

    async def fake_core(*_: object, **__: object) -> AgentResult:
        return AgentResult(status="completed", response="完成")

    class ReceiptFailingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            nonlocal receipt_was_durable
            if event.type is not RuntimeEventType.COMPLETION_RECEIPT:
                return
            restored = await engine.chat_run_store.get_run(event.session_id, event.run_id)
            receipt_was_durable = restored is not None and restored.receipt is not None
            raise RuntimeError("terminal disconnected")

    monkeypatch.setattr(engine, "_run_streaming_core", fake_core)
    try:
        with pytest.raises(RuntimeError, match="terminal disconnected"):
            await engine.run_streaming("完成任务", ReceiptFailingSink())
        assert receipt_was_durable is True
        session = await engine.get_or_create_session()
        runs = await engine.chat_run_store.list_runs(session.id)
        assert len(runs) == 1
        assert runs[0].receipt is not None
    finally:
        await engine.shutdown()
