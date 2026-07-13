from __future__ import annotations

import asyncio

import pytest

from naumi_agent.orchestrator.tool_batches import (
    ScheduledToolCall,
    build_tool_batches,
    execute_tool_batch,
)
from naumi_agent.tools.base import Tool, ToolCall, ToolMetadata, ToolRegistry, ToolResult


class BatchTool(Tool):
    def __init__(self, name: str, *, safe: bool) -> None:
        self._name = name
        self._safe = safe

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=self._safe,
            concurrency_safe=self._safe,
            user_facing_name=self._name,
        )

    async def execute(self, **kwargs: object) -> str:
        return self._name


def _call(index: int, name: str) -> ScheduledToolCall:
    return ScheduledToolCall(
        index=index,
        call=ToolCall(id=f"call-{index}", name=name, arguments="{}"),
    )


def test_build_tool_batches_respects_unsafe_barriers() -> None:
    registry = ToolRegistry()
    registry.register(BatchTool("safe-a", safe=True))
    registry.register(BatchTool("safe-b", safe=True))
    registry.register(BatchTool("unsafe", safe=False))
    registry.register(BatchTool("safe-c", safe=True))

    batches = build_tool_batches(
        (
            _call(0, "safe-a"),
            _call(1, "safe-b"),
            _call(2, "unsafe"),
            _call(3, "safe-c"),
        ),
        registry,
        max_parallel_tools=4,
    )

    assert [[item.call.name for item in batch.calls] for batch in batches] == [
        ["safe-a", "safe-b"],
        ["unsafe"],
        ["safe-c"],
    ]
    assert [batch.parallel for batch in batches] == [True, False, False]


def test_build_tool_batches_honors_parallel_limit_and_serial_mode() -> None:
    registry = ToolRegistry()
    for name in ("a", "b", "c"):
        registry.register(BatchTool(name, safe=True))
    calls = tuple(_call(index, name) for index, name in enumerate(("a", "b", "c")))

    limited = build_tool_batches(calls, registry, max_parallel_tools=2)
    serial = build_tool_batches(calls, registry, max_parallel_tools=1)

    assert [len(batch.calls) for batch in limited] == [2, 1]
    assert [batch.parallel for batch in limited] == [True, False]
    assert [len(batch.calls) for batch in serial] == [1, 1, 1]
    assert not any(batch.parallel for batch in serial)


@pytest.mark.asyncio
async def test_execute_parallel_batch_enters_all_calls_before_release() -> None:
    registry = ToolRegistry()
    registry.register(BatchTool("a", safe=True))
    registry.register(BatchTool("b", safe=True))
    batch = build_tool_batches(
        (_call(0, "a"), _call(1, "b")),
        registry,
        max_parallel_tools=2,
    )[0]
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(call: ToolCall) -> ToolResult:
        entered.add(call.name)
        if len(entered) == 2:
            both_entered.set()
        await release.wait()
        return ToolResult(call_id=call.id, status="success", content=call.name)

    task = asyncio.create_task(execute_tool_batch(batch, execute))
    await asyncio.wait_for(both_entered.wait(), timeout=1)
    release.set()
    results = await task

    assert entered == {"a", "b"}
    assert [item.result.content for item in results if item.result] == ["a", "b"]


@pytest.mark.asyncio
async def test_parallel_batch_captures_failure_without_cancelling_sibling() -> None:
    registry = ToolRegistry()
    registry.register(BatchTool("fail", safe=True))
    registry.register(BatchTool("ok", safe=True))
    batch = build_tool_batches(
        (_call(0, "fail"), _call(1, "ok")),
        registry,
        max_parallel_tools=2,
    )[0]

    async def execute(call: ToolCall) -> ToolResult:
        if call.name == "fail":
            raise RuntimeError("boom")
        await asyncio.sleep(0)
        return ToolResult(call_id=call.id, status="success", content="ok")

    results = await execute_tool_batch(batch, execute)

    assert isinstance(results[0].exception, RuntimeError)
    assert results[1].result is not None
    assert results[1].result.content == "ok"
