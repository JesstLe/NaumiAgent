"""Deterministic scheduling for safe parallel tool-call batches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult


@dataclass(frozen=True)
class ScheduledToolCall:
    index: int
    call: ToolCall


@dataclass(frozen=True)
class ToolBatch:
    calls: tuple[ScheduledToolCall, ...]
    parallel: bool


@dataclass(frozen=True)
class ScheduledToolResult:
    index: int
    call: ToolCall
    result: ToolResult | None = None
    exception: Exception | None = None


def build_tool_batches(
    calls: Sequence[ScheduledToolCall],
    registry: ToolRegistry,
    *,
    max_parallel_tools: int,
) -> tuple[ToolBatch, ...]:
    """Partition calls into safe parallel runs separated by serial barriers."""
    if max_parallel_tools < 1:
        raise ValueError("max_parallel_tools 必须大于 0")

    batches: list[ToolBatch] = []
    safe_calls: list[ScheduledToolCall] = []

    def flush_safe_calls() -> None:
        while safe_calls:
            chunk = tuple(safe_calls[:max_parallel_tools])
            del safe_calls[:max_parallel_tools]
            batches.append(ToolBatch(calls=chunk, parallel=len(chunk) > 1))

    for scheduled in calls:
        tool = registry.get(scheduled.call.name)
        is_safe = (
            max_parallel_tools > 1
            and tool is not None
            and tool.is_concurrency_safe
        )
        if is_safe:
            safe_calls.append(scheduled)
            if len(safe_calls) >= max_parallel_tools:
                flush_safe_calls()
            continue
        flush_safe_calls()
        batches.append(ToolBatch(calls=(scheduled,), parallel=False))

    flush_safe_calls()
    return tuple(batches)


async def execute_tool_batch(
    batch: ToolBatch,
    execute: Callable[[ToolCall], Awaitable[ToolResult]],
) -> tuple[ScheduledToolResult, ...]:
    """Execute one batch while isolating ordinary sibling failures."""

    async def run(scheduled: ScheduledToolCall) -> ScheduledToolResult:
        try:
            result = await execute(scheduled.call)
        except Exception as exc:
            return ScheduledToolResult(
                index=scheduled.index,
                call=scheduled.call,
                exception=exc,
            )
        return ScheduledToolResult(
            index=scheduled.index,
            call=scheduled.call,
            result=result,
        )

    if not batch.parallel:
        results = [await run(scheduled) for scheduled in batch.calls]
    else:
        tasks: list[asyncio.Task[ScheduledToolResult]] = []
        async with asyncio.TaskGroup() as group:
            for scheduled in batch.calls:
                tasks.append(group.create_task(run(scheduled)))
        results = [task.result() for task in tasks]
    return tuple(sorted(results, key=lambda item: item.index))
