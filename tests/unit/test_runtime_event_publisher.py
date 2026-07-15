"""Focused tests for ordered run-scoped Runtime event publication."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.publisher import RuntimeEventPublisher


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_publisher_assigns_run_identity_and_strict_sequence() -> None:
    sink = _RecordingSink()
    publisher = RuntimeEventPublisher(
        sink,
        session_id="session-1",
        run_id="run-1",
    )

    first = await publisher.publish(RuntimeEventType.RUN_STARTED, {"task": "demo"})
    second = await publisher.publish(
        RuntimeEventType.TURN_START,
        {"model": "test"},
        turn=1,
    )

    assert sink.events == [first, second]
    assert [event.sequence for event in sink.events] == [1, 2]
    assert [event.turn for event in sink.events] == [0, 1]
    assert all(event.session_id == "session-1" for event in sink.events)
    assert all(event.run_id == "run-1" for event in sink.events)
    assert len({event.id for event in sink.events}) == 2
    assert all(
        datetime.fromisoformat(event.timestamp).utcoffset() is not None
        for event in sink.events
    )


@pytest.mark.asyncio
async def test_separate_publishers_start_their_own_sequences() -> None:
    first_sink = _RecordingSink()
    second_sink = _RecordingSink()
    first = RuntimeEventPublisher(first_sink, session_id="s", run_id="run-a")
    second = RuntimeEventPublisher(second_sink, session_id="s", run_id="run-b")

    await first.publish(RuntimeEventType.TOKEN, {"content": "a"})
    await second.publish(RuntimeEventType.TOKEN, {"content": "b"})

    assert first_sink.events[0].sequence == 1
    assert second_sink.events[0].sequence == 1


@pytest.mark.asyncio
async def test_concurrent_publication_is_delivered_in_contiguous_sequence_order() -> None:
    sink = _RecordingSink()
    publisher = RuntimeEventPublisher(sink, session_id="s", run_id="run")

    results = await asyncio.gather(*(
        publisher.publish(RuntimeEventType.TOKEN, {"content": str(index)})
        for index in range(50)
    ))

    assert [event.sequence for event in sink.events] == list(range(1, 51))
    assert {event.id for event in results} == {event.id for event in sink.events}


@pytest.mark.asyncio
async def test_publisher_awaits_slow_sink_without_background_delivery() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowSink:
        async def emit(self, event: RuntimeEvent) -> None:
            del event
            entered.set()
            await release.wait()

    publisher = RuntimeEventPublisher(SlowSink(), session_id="s", run_id="run")
    delivery = asyncio.create_task(
        publisher.publish(RuntimeEventType.TOKEN, {"content": "wait"})
    )
    await entered.wait()
    assert not delivery.done()
    release.set()
    await delivery


@pytest.mark.asyncio
async def test_publisher_propagates_sink_failure_and_cancellation() -> None:
    class FailingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            del event
            raise RuntimeError("sink failed")

    class CancellingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            del event
            raise asyncio.CancelledError

    with pytest.raises(RuntimeError, match="sink failed"):
        await RuntimeEventPublisher(FailingSink(), session_id="s", run_id="r").publish(
            RuntimeEventType.ERROR,
            {"message": "failed"},
        )
    with pytest.raises(asyncio.CancelledError):
        await RuntimeEventPublisher(CancellingSink(), session_id="s", run_id="r").publish(
            RuntimeEventType.ERROR,
            {"message": "cancelled"},
        )


@pytest.mark.asyncio
async def test_legacy_callback_validates_name_before_sink_delivery() -> None:
    sink = _RecordingSink()
    callback = RuntimeEventPublisher(sink, session_id="s", run_id="r").legacy_callback()

    with pytest.raises(ValueError, match="unknown_event"):
        await callback("unknown_event", {})
    assert sink.events == []

    await callback("turn_start", {"turn": 3, "model": "test"})
    assert sink.events[0].type is RuntimeEventType.TURN_START
    assert sink.events[0].turn == 3
    assert sink.events[0].data["model"] == "test"


def test_publisher_rejects_incomplete_sink() -> None:
    with pytest.raises(TypeError, match="EventSink"):
        RuntimeEventPublisher(object(), session_id="s", run_id="r")  # type: ignore[arg-type]
