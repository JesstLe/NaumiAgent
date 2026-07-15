"""流式事件系统单元测试."""

import json

import pytest

from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.event_bus import EventEmitter
from naumi_agent.streaming.events import EventType, StreamEvent, StreamEventSink

_RUNTIME_TRANSPORT_TYPES = {
    RuntimeEventType.COMPLETION_RECEIPT: EventType.COMPLETION_RECEIPT,
    RuntimeEventType.CONTEXT_COMPACTED: EventType.CONTEXT_COMPACTED,
    RuntimeEventType.ERROR: EventType.AGENT_ERROR,
    RuntimeEventType.PERMISSION_BUBBLE: EventType.PERMISSION_REQUEST,
    RuntimeEventType.RESPONSE_END: EventType.AGENT_END,
    RuntimeEventType.RESPONSE_START: EventType.AGENT_START,
    RuntimeEventType.THINKING_DELTA: EventType.THINKING_DELTA,
    RuntimeEventType.THINKING_END: EventType.THINKING_END,
    RuntimeEventType.THINKING_START: EventType.THINKING_START,
    RuntimeEventType.TOKEN: EventType.TOKEN_DELTA,
    RuntimeEventType.TOOL_END: EventType.TOOL_CALL_END,
    RuntimeEventType.TOOL_ERROR: EventType.TOOL_CALL_ERROR,
    RuntimeEventType.TOOL_START: EventType.TOOL_CALL_START,
    RuntimeEventType.TURN_START: EventType.TURN_START,
}


def _make_runtime_event(
    event_type: RuntimeEventType,
    *,
    data: dict | None = None,
) -> RuntimeEvent:
    payload = data
    if payload is None:
        payload = {
            RuntimeEventType.TOKEN: {"content": "你"},
            RuntimeEventType.TOOL_START: {
                "name": "bash_run",
                "call_id": "call-1",
                "args": '{"command":"echo secret"}',
            },
            RuntimeEventType.TOOL_END: {
                "name": "bash_run",
                "call_id": "call-1",
                "status": "success",
            },
            RuntimeEventType.TOOL_ERROR: {
                "name": "bash_run",
                "call_id": "call-1",
                "status": "error",
            },
            RuntimeEventType.PERMISSION_BUBBLE: {
                "tool_name": "bash_run",
                "call_id": "call-1",
                "status": "needs_confirmation",
                "reason": "命令执行需要确认。",
                "arguments": {"command": "echo secret"},
            },
        }.get(event_type, {"marker": event_type.value})
    return RuntimeEvent(
        id=f"event-{event_type.value}",
        type=event_type,
        data=payload,
        timestamp="2026-07-15T08:00:00+08:00",
        session_id="session-1",
        run_id="run-1",
        turn=2,
        sequence=7,
    )


@pytest.fixture
def emitter() -> EventEmitter:
    return EventEmitter(max_queue_size=10)


def _make_event(
    event_type: EventType = EventType.TOKEN_DELTA, data: dict | None = None
) -> StreamEvent:
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


class TestStreamEventSink:
    async def _convert(self, event: RuntimeEvent) -> StreamEvent:
        received: list[StreamEvent] = []

        async def collect(stream_event: StreamEvent) -> None:
            received.append(stream_event)

        await StreamEventSink(collect).emit(event)
        assert len(received) == 1
        return received[0]

    @pytest.mark.parametrize("runtime_type", tuple(RuntimeEventType))
    async def test_runtime_mapping_is_exhaustive_and_never_defaults_to_turn_end(
        self,
        runtime_type: RuntimeEventType,
    ) -> None:
        received: list[StreamEvent] = []

        async def collect(event: StreamEvent) -> None:
            received.append(event)

        runtime_event = _make_runtime_event(runtime_type)
        await StreamEventSink(collect).emit(runtime_event)

        assert len(tuple(RuntimeEventType)) == 31
        assert len(received) == 1
        transport_event = received[0]
        assert transport_event.type is not EventType.TURN_END
        assert transport_event.id == runtime_event.id
        assert transport_event.event_id == runtime_event.id
        assert transport_event.source_event == runtime_type.value
        assert transport_event.sequence == runtime_event.sequence
        assert transport_event.session_id == runtime_event.session_id
        assert transport_event.turn == runtime_event.turn
        expected_type = _RUNTIME_TRANSPORT_TYPES.get(
            runtime_type,
            EventType.RUNTIME_EVENT,
        )
        assert transport_event.type is expected_type
        if expected_type is EventType.RUNTIME_EVENT:
            assert set(transport_event.data) == {"event", "data"}
            assert transport_event.data["event"] == runtime_type.value

    async def test_token_mapping_renames_content_without_losing_identity(self) -> None:
        runtime_event = _make_runtime_event(
            RuntimeEventType.TOKEN,
            data={"content": "hi", "finish_reason": None},
        )

        event = await self._convert(runtime_event)

        assert event.type is EventType.TOKEN_DELTA
        assert event.data["token"] == "hi"
        assert event.data["finish_reason"] is None
        assert "content" not in event.data
        assert event.event_id == runtime_event.id

    async def test_permission_mapping_omits_private_arguments(self) -> None:
        runtime_event = _make_runtime_event(
            RuntimeEventType.PERMISSION_BUBBLE,
            data={
                "agent_name": "main",
                "tool_name": "bash_run",
                "call_id": "call-1",
                "status": "needs_confirmation",
                "reason": "命令执行需要确认。",
                "risk_level": "medium",
                "requires_confirmation": True,
                "arguments": {"command": "echo $API_KEY"},
            },
        )

        event = await self._convert(runtime_event)

        assert event.type is EventType.PERMISSION_REQUEST
        assert event.data == {
            "agent_name": "main",
            "tool_name": "bash_run",
            "call_id": "call-1",
            "status": "needs_confirmation",
            "reason": "命令执行需要确认。",
            "risk_level": "medium",
            "requires_confirmation": True,
        }

    async def test_tool_start_mapping_omits_raw_arguments(self) -> None:
        runtime_event = _make_runtime_event(
            RuntimeEventType.TOOL_START,
            data={
                "name": "bash_run",
                "tool_call_id": "call-1",
                "args": '{"command": "echo $API_KEY"}',
                "argument_chars": 26,
            },
        )

        event = await self._convert(runtime_event)

        assert event.type is EventType.TOOL_CALL_START
        assert event.data == {"name": "bash_run", "call_id": "call-1"}

    async def test_thinking_delta_mapping_omits_internal_content(self) -> None:
        runtime_event = _make_runtime_event(
            RuntimeEventType.THINKING_DELTA,
            data={"content": "内部推理不应离开引擎"},
        )

        event = await self._convert(runtime_event)

        assert event.type is EventType.THINKING_DELTA
        assert event.data == {}

    async def test_sse_and_websocket_preserve_one_runtime_event_identity(self) -> None:
        runtime_event = _make_runtime_event(
            RuntimeEventType.RUNTIME_NOTIFICATION,
            data={"message": "后台任务完成", "nested": {"items": [1, 2]}},
        )
        sse_frames: list[str] = []
        websocket_frames: list[str] = []

        async def send_sse(event: StreamEvent) -> None:
            sse_frames.append(event.to_sse())

        async def send_websocket(event: StreamEvent) -> None:
            websocket_frames.append(event.to_ws())

        await StreamEventSink(send_sse).emit(runtime_event)
        await StreamEventSink(send_websocket).emit(runtime_event)

        sse_payload = json.loads(sse_frames[0].removeprefix("data: "))
        websocket_payload = json.loads(websocket_frames[0])
        assert sse_payload == websocket_payload
        assert sse_payload["id"] == runtime_event.id
        assert sse_payload["event_id"] == runtime_event.id
        assert sse_payload["source_event"] == "runtime_notification"
        assert sse_payload["sequence"] == 7
        assert sse_payload["data"]["event"] == "runtime_notification"
        assert sse_payload["data"]["data"]["nested"] == {"items": [1, 2]}

    def test_version_one_stream_event_serialization_stays_compact(self) -> None:
        legacy_event = StreamEvent(
            type=EventType.TOKEN_DELTA,
            data={"token": "旧客户端"},
            session_id="session-legacy",
        )

        payload = legacy_event.to_dict()

        assert "source_event" not in payload
        assert "event_id" not in payload
        assert "sequence" not in payload
