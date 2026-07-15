"""Deterministic adapters for the Runtime EventSink boundary."""

from __future__ import annotations

from collections.abc import Iterable

from naumi_agent.runtime.ports.events import (
    EventSink,
    LegacyEventCallback,
    RuntimeEvent,
    RuntimeEventType,
    thaw_event_data,
)

_LEGACY_RECEIPT_SEQUENCE_FIELDS = frozenset({
    "approvals",
    "changes",
    "evidence_refs",
    "next_actions",
    "risks",
    "unverified",
    "validations",
})


class NullEventSink:
    """Explicitly consume events when no external observer is configured."""

    async def emit(self, event: RuntimeEvent) -> None:
        del event


class CallbackEventSink:
    """Adapt one legacy ``(name, payload)`` callback to EventSink."""

    def __init__(
        self,
        callback: LegacyEventCallback,
        *,
        preserve_legacy_receipt: bool = False,
    ) -> None:
        if not callable(callback):
            raise TypeError("CallbackEventSink 需要可调用的异步事件回调")
        self._callback = callback
        self._preserve_legacy_receipt = preserve_legacy_receipt

    async def emit(self, event: RuntimeEvent) -> None:
        payload = thaw_event_data(event.data)
        if (
            self._preserve_legacy_receipt
            and event.type is RuntimeEventType.COMPLETION_RECEIPT
        ):
            for field_name in _LEGACY_RECEIPT_SEQUENCE_FIELDS:
                value = payload.get(field_name)
                if isinstance(value, list):
                    payload[field_name] = tuple(value)
        else:
            payload.update({
                "event_id": event.id,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "turn": event.turn,
                "sequence": event.sequence,
            })
        await self._callback(event.type.value, payload)


class CompositeEventSink:
    """Deliver one event sequentially to a stable set of typed Sinks."""

    def __init__(self, sinks: Iterable[EventSink]) -> None:
        resolved = tuple(sinks)
        if not resolved:
            raise ValueError("组合 EventSink 至少需要一个 Sink，不能为空")
        if any(not isinstance(sink, EventSink) for sink in resolved):
            raise TypeError("组合 EventSink 必须包含完整的 EventSink 实现")
        self._sinks = resolved

    async def emit(self, event: RuntimeEvent) -> None:
        for sink in self._sinks:
            await sink.emit(event)


def coerce_event_sink(candidate: EventSink | LegacyEventCallback) -> EventSink:
    """Normalize a typed Sink or a temporary legacy callback adapter."""
    if isinstance(candidate, EventSink):
        return candidate
    if callable(candidate):
        return CallbackEventSink(candidate, preserve_legacy_receipt=True)
    raise TypeError("事件消费者必须实现 EventSink 或提供异步事件回调")


__all__ = [
    "CallbackEventSink",
    "CompositeEventSink",
    "NullEventSink",
    "coerce_event_sink",
]
