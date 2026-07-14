"""Local adapter for authorized tool invocations."""

from __future__ import annotations

from collections.abc import Mapping
from inspect import signature
from time import perf_counter

from naumi_agent.runtime.ports.tool_execution import (
    ExecutableTool,
    ToolEventCallback,
    ToolExecutionOutcome,
)


class LocalToolExecutor:
    """Execute an authorized tool in the current process."""

    async def invoke(
        self,
        tool: ExecutableTool,
        arguments: Mapping[str, object],
        *,
        event_callback: ToolEventCallback | None = None,
    ) -> ToolExecutionOutcome:
        invocation_arguments = dict(arguments)
        if (
            event_callback is not None
            and "event_callback" in signature(tool.execute).parameters
        ):
            invocation_arguments["event_callback"] = event_callback

        started_at = perf_counter()
        content = await tool.execute(**invocation_arguments)
        duration_ms = max(0, int((perf_counter() - started_at) * 1000))
        if not isinstance(content, str):
            raise TypeError(f"工具 {tool.name} 必须返回字符串")
        return ToolExecutionOutcome(content=content, duration_ms=duration_ms)


__all__ = ["LocalToolExecutor"]
