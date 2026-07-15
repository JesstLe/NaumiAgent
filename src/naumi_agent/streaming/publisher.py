"""Ordered run-scoped publication of immutable Runtime events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from naumi_agent.runtime.ports.events import (
    EventSink,
    LegacyEventCallback,
    RuntimeEvent,
    RuntimeEventType,
)


class RuntimeEventPublisher:
    """Assign identity and deliver events in strict per-run sequence order."""

    def __init__(
        self,
        sink: EventSink,
        *,
        session_id: str,
        run_id: str,
    ) -> None:
        if not isinstance(sink, EventSink):
            raise TypeError("RuntimeEventPublisher 需要完整的 EventSink 实现")
        self._sink = sink
        self._session_id = session_id
        self._run_id = run_id
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event_type: RuntimeEventType,
        data: Mapping[str, object],
        *,
        turn: int = 0,
    ) -> RuntimeEvent:
        """Build and synchronously deliver one ordered event."""
        async with self._lock:
            self._sequence += 1
            event = RuntimeEvent.create(
                event_type=event_type,
                data=data,
                session_id=self._session_id,
                run_id=self._run_id,
                turn=turn,
                sequence=self._sequence,
            )
            await self._sink.emit(event)
            return event

    def legacy_callback(self) -> LegacyEventCallback:
        """Expose a strict adapter for Tool APIs that still accept callbacks."""

        async def callback(name: str, data: dict[str, object]) -> None:
            try:
                event_type = RuntimeEventType(name)
            except ValueError as exc:
                raise ValueError(f"未知 Runtime 事件：{name}") from exc
            turn = data.get("turn", 0)
            if isinstance(turn, bool) or not isinstance(turn, int):
                raise TypeError("兼容事件 payload.turn 必须是非负整数")
            if turn < 0:
                raise ValueError("兼容事件 payload.turn 必须是非负整数")
            await self.publish(event_type, data, turn=turn)

        return callback


__all__ = ["RuntimeEventPublisher"]
