"""Authorized tool execution port and local adapter contracts."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from naumi_agent.runtime.ports.tool_execution import (
    ToolEventCallback,
    ToolExecutionOutcome,
    ToolExecutionPort,
)
from naumi_agent.tools.base import Tool
from naumi_agent.tools.builtin import FileReadTool, FileWriteTool
from naumi_agent.tools.execution import LocalToolExecutor


class _IncompleteExecutor:
    pass


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
