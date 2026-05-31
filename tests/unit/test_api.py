"""API 路由单元测试."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from naumi_agent.api.routes.messages import (
    _engine_event_to_stream_event,
    _stream_response,
    send_message,
)
from naumi_agent.api.schemas import HealthResponse, MessageCreate, SessionCreate
from naumi_agent.streaming.events import EventType


class TestSchemas:
    def test_session_create(self) -> None:
        s = SessionCreate(title="test")
        assert s.title == "test"

    def test_session_create_defaults(self) -> None:
        s = SessionCreate()
        assert s.title is None

    def test_health_response(self) -> None:
        h = HealthResponse(status="healthy", version="0.1.0", uptime_seconds=0.0, active_sessions=0)
        assert h.status == "healthy"


class TestHealthEndpoint:
    def test_health_check(self) -> None:
        from naumi_agent.api.app import create_app

        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.1.0"


class _FakeSessionStore:
    async def load(self, session_id: str):
        return SimpleNamespace(id=session_id)


class _FakeEngine:
    def __init__(self) -> None:
        self.session_store = _FakeSessionStore()
        self.loaded: list[str] = []
        self.ran: list[str] = []

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        return True

    async def run(self, content: str):
        self.ran.append(content)
        usage = SimpleNamespace(turns=1, total_cost_usd=0.01)
        return SimpleNamespace(status="completed", response="ok", usage=usage)

    async def run_streaming(self, content: str, on_event):
        self.ran.append(content)
        await on_event("token", {"content": "你"})
        usage = SimpleNamespace(turns=1, total_cost_usd=0.01)
        return SimpleNamespace(status="completed", response="你", usage=usage)


def _fake_request(engine: _FakeEngine):
    state = SimpleNamespace(engine=engine, engine_lock=asyncio.Lock())
    return SimpleNamespace(app=SimpleNamespace(state=state))


class TestMessageRoutes:
    @pytest.mark.asyncio
    async def test_send_message_loads_requested_session(self) -> None:
        engine = _FakeEngine()
        request = _fake_request(engine)

        response = await send_message(
            "sess_1",
            MessageCreate(content="hello", stream=False),
            request,
            auth="test",
        )

        assert engine.loaded == ["sess_1"]
        assert engine.ran == ["hello"]
        assert response.content == "ok"

    @pytest.mark.asyncio
    async def test_stream_response_uses_run_streaming_events(self) -> None:
        engine = _FakeEngine()
        request = _fake_request(engine)

        events = []
        async for chunk in _stream_response(engine, "sess_1", "hello", request):
            events.append(json.loads(chunk.removeprefix("data: ")))

        assert engine.loaded == ["sess_1"]
        assert engine.ran == ["hello"]
        assert events[0]["type"] == "token_delta"
        assert events[0]["data"]["token"] == "你"
        assert events[-1]["type"] == "agent_end"

    def test_engine_event_to_stream_event_normalizes_token(self) -> None:
        event = _engine_event_to_stream_event(
            "token",
            {"content": "hi"},
            session_id="sess_1",
        )

        assert event.type == EventType.TOKEN_DELTA
        assert event.data == {"token": "hi"}
