"""Focused behavior tests for deterministic Runtime event Sink adapters."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from naumi_agent.runtime.ports.events import EventSink, RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.sinks import (
    CallbackEventSink,
    CompositeEventSink,
    NullEventSink,
    coerce_event_sink,
)


def _event() -> RuntimeEvent:
    return RuntimeEvent(
        id="event-1",
        type=RuntimeEventType.TOKEN,
        data={"content": "hello", "nested": {"items": [1]}},
        timestamp=datetime.now(UTC).isoformat(),
        session_id="session-1",
        run_id="run-1",
        turn=2,
        sequence=7,
    )


class _RecordingSink:
    def __init__(self, name: str, calls: list[tuple[str, RuntimeEvent]]) -> None:
        self.name = name
        self.calls = calls

    async def emit(self, event: RuntimeEvent) -> None:
        self.calls.append((self.name, event))


class _FalseySink(_RecordingSink):
    def __bool__(self) -> bool:
        return False


def test_builtin_adapters_structurally_implement_event_sink() -> None:
    async def callback(_: str, __: dict[str, object]) -> None:
        return None

    assert isinstance(NullEventSink(), EventSink)
    assert isinstance(CallbackEventSink(callback), EventSink)
    assert isinstance(CompositeEventSink((NullEventSink(),)), EventSink)


@pytest.mark.asyncio
async def test_composite_delivers_same_event_in_stable_order() -> None:
    calls: list[tuple[str, RuntimeEvent]] = []
    event = _event()
    sink = CompositeEventSink((
        _RecordingSink("first", calls),
        _RecordingSink("second", calls),
    ))

    await sink.emit(event)

    assert [name for name, _ in calls] == ["first", "second"]
    assert calls[0][1] is event
    assert calls[1][1] is event


@pytest.mark.asyncio
async def test_callback_receives_metadata_and_an_independent_payload() -> None:
    received: list[dict[str, object]] = []

    async def callback(name: str, payload: dict[str, object]) -> None:
        assert name == "token"
        received.append(payload)
        payload["content"] = "mutated"
        payload["nested"]["items"].append(2)

    event = _event()
    await CallbackEventSink(callback).emit(event)

    assert received[0]["event_id"] == "event-1"
    assert received[0]["session_id"] == "session-1"
    assert received[0]["run_id"] == "run-1"
    assert received[0]["turn"] == 2
    assert received[0]["sequence"] == 7
    assert event.data["content"] == "hello"
    assert event.data["nested"]["items"] == (1,)


def test_composite_rejects_empty_or_incomplete_sinks() -> None:
    with pytest.raises(ValueError, match="至少.*一个"):
        CompositeEventSink(())
    with pytest.raises(TypeError, match="EventSink"):
        CompositeEventSink((object(),))  # type: ignore[arg-type]


def test_coerce_preserves_explicit_falsey_sink() -> None:
    sink = _FalseySink("falsey", [])
    assert coerce_event_sink(sink) is sink


@pytest.mark.asyncio
async def test_composite_stops_after_ordinary_sink_failure() -> None:
    calls: list[tuple[str, RuntimeEvent]] = []

    class FailingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            calls.append(("failed", event))
            raise RuntimeError("delivery failed")

    sink = CompositeEventSink((FailingSink(), _RecordingSink("late", calls)))
    with pytest.raises(RuntimeError, match="delivery failed"):
        await sink.emit(_event())
    assert [name for name, _ in calls] == ["failed"]


@pytest.mark.asyncio
async def test_composite_propagates_cancellation() -> None:
    class CancellingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            del event
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await CompositeEventSink((CancellingSink(),)).emit(_event())


@pytest.mark.asyncio
async def test_callback_sink_applies_awaited_backpressure() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def callback(_: str, __: dict[str, object]) -> None:
        entered.set()
        await release.wait()

    delivery = asyncio.create_task(CallbackEventSink(callback).emit(_event()))
    await entered.wait()
    assert not delivery.done()
    release.set()
    await delivery


def test_coerce_rejects_non_sink_non_callback() -> None:
    with pytest.raises(TypeError, match="EventSink"):
        coerce_event_sink(object())  # type: ignore[arg-type]
