"""Authorized tool invocation boundary consumed by the Agent runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from naumi_agent.runtime.ports.events import LegacyEventCallback


class ExecutableTool(Protocol):
    """Minimum behavior required from an already resolved tool."""

    @property
    def name(self) -> str: ...

    async def execute(self, **kwargs: object) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    """Successful authorized invocation result returned to the runtime."""

    content: str
    duration_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("ToolExecutionOutcome.content 必须是字符串")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise TypeError("ToolExecutionOutcome.duration_ms 必须是非负整数")
        if self.duration_ms < 0:
            raise ValueError("ToolExecutionOutcome.duration_ms 必须是非负整数")


@runtime_checkable
class ToolExecutionPort(Protocol):
    """Invoke a tool only after the runtime has authorized the call."""

    async def invoke(
        self,
        tool: ExecutableTool,
        arguments: Mapping[str, object],
        *,
        event_callback: LegacyEventCallback | None = None,
    ) -> ToolExecutionOutcome: ...


__all__ = [
    "ExecutableTool",
    "ToolExecutionOutcome",
    "ToolExecutionPort",
]
