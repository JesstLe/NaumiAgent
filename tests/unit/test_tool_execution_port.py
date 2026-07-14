"""Authorized tool execution port and local adapter contracts."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig, ModelConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.ports.tool_execution import (
    ToolEventCallback,
    ToolExecutionOutcome,
    ToolExecutionPort,
)
from naumi_agent.tools.base import Tool, ToolCall
from naumi_agent.tools.builtin import FileReadTool, FileWriteTool
from naumi_agent.tools.execution import LocalToolExecutor


class _IncompleteExecutor:
    pass


class _RecordingExecutor(LocalToolExecutor):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def invoke(
        self,
        tool: Any,
        arguments: Mapping[str, object],
        *,
        event_callback: ToolEventCallback | None = None,
    ) -> ToolExecutionOutcome:
        self.calls.append((tool.name, dict(arguments)))
        return await super().invoke(
            tool,
            arguments,
            event_callback=event_callback,
        )


class _FalseyExecutor(_RecordingExecutor):
    def __bool__(self) -> bool:
        return False


class _FailingExecutor:
    async def invoke(
        self,
        tool: Any,
        arguments: Mapping[str, object],
        *,
        event_callback: ToolEventCallback | None = None,
    ) -> ToolExecutionOutcome:
        del tool, arguments, event_callback
        raise RuntimeError("remote-worker-down")


class _BlockingExecutor:
    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered

    async def invoke(
        self,
        tool: Any,
        arguments: Mapping[str, object],
        *,
        event_callback: ToolEventCallback | None = None,
    ) -> ToolExecutionOutcome:
        del tool, arguments, event_callback
        self._entered.set()
        await asyncio.Event().wait()
        return ToolExecutionOutcome(content="unreachable", duration_ms=0)


class _CallbackTool(Tool):
    @property
    def name(self) -> str:
        return "callback_tool"

    @property
    def description(self) -> str:
        return "callback test"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(
        self,
        *,
        value: str,
        event_callback: ToolEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        if event_callback is not None:
            await event_callback("inner_progress", {"value": value})
        return f"callback:{value}"


class _InvalidResultTool(_CallbackTool):
    async def execute(  # type: ignore[override]
        self,
        *,
        value: str,
        event_callback: ToolEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        del value, event_callback, kwargs
        return 42  # type: ignore[return-value]


class _FailureTool(_CallbackTool):
    async def execute(
        self,
        *,
        value: str,
        event_callback: ToolEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        del value, event_callback, kwargs
        raise RuntimeError("adapter-boom")


class _CancellationTool(_CallbackTool):
    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered

    async def execute(
        self,
        *,
        value: str,
        event_callback: ToolEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        del value, event_callback, kwargs
        self._entered.set()
        await asyncio.Event().wait()
        return "unreachable"


def test_tool_execution_port_exposes_exact_invoke_surface() -> None:
    methods = {
        name
        for name, value in vars(ToolExecutionPort).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert methods == {"invoke"}


def test_local_executor_structurally_implements_port() -> None:
    assert isinstance(LocalToolExecutor(), ToolExecutionPort)
    assert not isinstance(_IncompleteExecutor(), ToolExecutionPort)


def test_tool_execution_outcome_rejects_invalid_values() -> None:
    outcome = ToolExecutionOutcome(content="ok", duration_ms=0)
    assert outcome.content == "ok"
    assert outcome.duration_ms == 0

    with pytest.raises(TypeError, match="content 必须是字符串"):
        ToolExecutionOutcome(content=object(), duration_ms=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="duration_ms 必须是非负整数"):
        ToolExecutionOutcome(content="ok", duration_ms=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duration_ms 必须是非负整数"):
        ToolExecutionOutcome(content="ok", duration_ms=-1)


def _engine_config(tmp_path: Path) -> AppConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        workspace_root=str(tmp_path),
        models=ModelConfig(),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


@pytest.mark.asyncio
async def test_engine_uses_explicit_tool_execution_port(tmp_path: Path) -> None:
    port = _RecordingExecutor()
    engine = AgentEngine(_engine_config(tmp_path), tool_execution_port=port)
    try:
        assert engine.tool_executor is port
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_does_not_replace_falsey_tool_execution_port(
    tmp_path: Path,
) -> None:
    port = _FalseyExecutor()
    engine = AgentEngine(_engine_config(tmp_path), tool_execution_port=port)
    try:
        assert engine.tool_executor is port
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_keeps_default_local_tool_executor(tmp_path: Path) -> None:
    engine = AgentEngine(_engine_config(tmp_path))
    try:
        assert isinstance(engine.tool_executor, LocalToolExecutor)
        assert isinstance(engine.tool_executor, ToolExecutionPort)
    finally:
        await engine.shutdown()


def test_engine_rejects_invalid_tool_execution_port_before_runtime_io(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        TypeError,
        match="tool_execution_port 必须实现完整的 ToolExecutionPort 契约",
    ):
        AgentEngine(
            _engine_config(tmp_path),
            tool_execution_port=_IncompleteExecutor(),  # type: ignore[arg-type]
        )
    assert not (tmp_path / ".naumi").exists()


@pytest.mark.asyncio
async def test_engine_rejections_never_reach_tool_execution_port(
    tmp_path: Path,
) -> None:
    port = _RecordingExecutor()
    engine = AgentEngine(_engine_config(tmp_path), tool_execution_port=port)
    try:
        unknown = await engine.execute_tool(
            ToolCall(id="unknown", name="missing_tool", arguments="{}")
        )
        invalid = await engine.execute_tool(
            ToolCall(id="invalid", name="file_read", arguments="not-json")
        )
        engine.set_runtime_mode("plan")
        plan_blocked = await engine.execute_tool(
            ToolCall(
                id="plan",
                name="file_write",
                arguments='{"path":"blocked.txt","content":"no"}',
            )
        )

        assert unknown.status == invalid.status == plan_blocked.status == "error"
        assert "未知工具" in unknown.content
        assert "Invalid JSON arguments" in invalid.content
        assert "Plan 模式" in plan_blocked.content
        assert port.calls == []
        assert not (tmp_path / "blocked.txt").exists()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_public_facade_invokes_port_after_bypass_authorization(
    tmp_path: Path,
) -> None:
    port = _RecordingExecutor()
    engine = AgentEngine(_engine_config(tmp_path), tool_execution_port=port)
    confirmations: list[dict[str, object]] = []

    async def confirmer(payload: dict[str, object]) -> str:
        confirmations.append(payload)
        return "deny"

    engine.set_permission_confirmer(confirmer)
    engine.set_runtime_mode("bypass")
    target = tmp_path / "authorized.txt"
    try:
        result = await engine.execute_tool(
            ToolCall(
                id="authorized",
                name="file_write",
                arguments=(
                    '{"path":"'
                    + str(target)
                    + '","content":"authorized through port"}'
                ),
            ),
            agent_name="contract-test",
        )

        assert result.call_id == "authorized"
        assert result.status == "success"
        assert result.duration_ms >= 0
        assert "已创建" in result.content
        assert target.read_text(encoding="utf-8") == "authorized through port"
        assert port.calls == [
            (
                "file_write",
                {"path": str(target), "content": "authorized through port"},
            )
        ]
        assert confirmations == []
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_normalizes_port_failure_but_propagates_cancellation(
    tmp_path: Path,
) -> None:
    failing = AgentEngine(
        _engine_config(tmp_path / "failing"),
        tool_execution_port=_FailingExecutor(),
    )
    failing.set_runtime_mode("bypass")
    try:
        failed = await failing.execute_tool(
            ToolCall(
                id="failed",
                name="file_read",
                arguments='{"path":"missing.txt"}',
            )
        )
        assert failed.status == "error"
        assert "RuntimeError: remote-worker-down" in failed.content
    finally:
        await failing.shutdown()

    entered = asyncio.Event()
    blocking = AgentEngine(
        _engine_config(tmp_path / "blocking"),
        tool_execution_port=_BlockingExecutor(entered),
    )
    blocking.set_runtime_mode("bypass")
    task = asyncio.create_task(
        blocking.execute_tool(
            ToolCall(
                id="cancelled",
                name="file_read",
                arguments='{"path":"missing.txt"}',
            )
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await blocking.shutdown()


@pytest.mark.asyncio
async def test_local_executor_runs_real_file_write_and_read_without_mutating_args(
    tmp_path: Path,
) -> None:
    executor = LocalToolExecutor()
    target = tmp_path / "nested" / "真实工具.txt"
    write_args: Mapping[str, object] = MappingProxyType({
        "path": str(target),
        "content": "工具端口真实闭环\n第二行",
    })

    written = await executor.invoke(FileWriteTool(tmp_path), write_args)
    read = await executor.invoke(
        FileReadTool(tmp_path),
        MappingProxyType({"path": str(target)}),
    )

    assert target.read_text(encoding="utf-8") == "工具端口真实闭环\n第二行"
    assert "已创建" in written.content
    assert "工具端口真实闭环" in read.content
    assert written.duration_ms >= 0
    assert read.duration_ms >= 0
    assert dict(write_args) == {
        "path": str(target),
        "content": "工具端口真实闭环\n第二行",
    }


@pytest.mark.asyncio
async def test_local_executor_only_injects_callback_when_explicitly_supported(
    tmp_path: Path,
) -> None:
    executor = LocalToolExecutor()
    events: list[tuple[str, dict[str, Any]]] = []

    async def callback(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    callback_result = await executor.invoke(
        _CallbackTool(),
        {"value": "ready"},
        event_callback=callback,
    )
    target = tmp_path / "plain.txt"
    target.write_text("plain", encoding="utf-8")
    plain_result = await executor.invoke(
        FileReadTool(tmp_path),
        {"path": str(target)},
        event_callback=callback,
    )

    assert callback_result.content == "callback:ready"
    assert events == [("inner_progress", {"value": "ready"})]
    assert "plain" in plain_result.content


@pytest.mark.asyncio
async def test_local_executor_propagates_contract_failure_and_tool_exception() -> None:
    executor = LocalToolExecutor()

    with pytest.raises(TypeError, match="必须返回字符串"):
        await executor.invoke(_InvalidResultTool(), {"value": "bad"})
    with pytest.raises(RuntimeError, match="adapter-boom"):
        await executor.invoke(_FailureTool(), {"value": "bad"})


@pytest.mark.asyncio
async def test_local_executor_does_not_swallow_cancellation() -> None:
    executor = LocalToolExecutor()
    entered = asyncio.Event()
    task = asyncio.create_task(
        executor.invoke(_CancellationTool(entered), {"value": "wait"})
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _accepts_callback_type(
    callback: ToolEventCallback,
) -> Awaitable[None] | None:
    """Keep the public callback alias import covered by static analysis."""
    del callback
    return None
