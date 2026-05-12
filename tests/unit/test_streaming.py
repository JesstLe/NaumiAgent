"""流式事件系统单元测试."""

import asyncio

import pytest

from naumi_agent.streaming.events import EventType, StreamEvent
from naumi_agent.streaming.event_bus import EventEmitter


@pytest.fixture
def emitter() -> EventEmitter:
    return EventEmitter(max_queue_size=10)


def _make_event(event_type: EventType = EventType.TOKEN_DELTA, data: dict | None = None) -> StreamEvent:
    return StreamEvent(
        type=event_type,
        data=data or {"token": "test"},
        session_id="test_session",
    )


class TestEventEmitter:
    async def test_subscribe_and_emit(self, emitter: EventEmitter) -> None:
        queue = emitter.subscribe("sub1")
        event = _make_event()
        await emitter.emit(event)

        received = queue.get_nowait()
        assert received.type == EventType.TOKEN_DELTA
        assert received.data["token"] == "test"

    async def test_filter_events(self, emitter: EventEmitter) -> None:
        queue = emitter.subscribe(
            "sub1",
            event_types={EventType.TOKEN_DELTA},
        )
        await emitter.emit(_make_event(EventType.TOKEN_DELTA))
        await emitter.emit(_make_event(EventType.TOOL_CALL_START))

        assert not queue.empty()
        event = queue.get_nowait()
        assert event.type == EventType.TOKEN_DELTA
        assert queue.empty()

    async def test_backpressure(self, emitter: EventEmitter) -> None:
        queue = emitter.subscribe("sub1")

        for i in range(15):
            await emitter.emit(_make_event(data={"token": str(i)}))

        # 应该丢弃最旧的，保留最新的 10 个
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        assert len(events) == 10
        # 最旧的被丢弃
        assert events[0].data["token"] == "5"

    async def test_unsubscribe(self, emitter: EventEmitter) -> None:
        emitter.subscribe("sub1")
        emitter.unsubscribe("sub1")
        assert emitter.subscriber_count == 0

    async def test_history(self, emitter: EventEmitter) -> None:
        for i in range(5):
            await emitter.emit(_make_event(data={"token": str(i)}))

        history = emitter.get_history(limit=3)
        assert len(history) == 3
        assert history[0].data["token"] == "2"

    async def test_history_with_filter(self, emitter: EventEmitter) -> None:
        await emitter.emit(_make_event(EventType.TOKEN_DELTA))
        await emitter.emit(_make_event(EventType.TOOL_CALL_START))
        await emitter.emit(_make_event(EventType.TOKEN_DELTA))

        history = emitter.get_history(event_types={EventType.TOKEN_DELTA})
        assert len(history) == 2

    async def test_event_serialization(self) -> None:
        event = StreamEvent(
            type=EventType.TOKEN_DELTA,
            data={"token": "你好"},
            session_id="sess_123",
        )
        d = event.to_dict()
        assert d["type"] == "token_delta"
        assert d["data"]["token"] == "你好"

        sse = event.to_sse()
        assert sse.startswith("data: ")
        assert "\n\n" in sse

        ws = event.to_ws()
        assert '"token_delta"' in ws
