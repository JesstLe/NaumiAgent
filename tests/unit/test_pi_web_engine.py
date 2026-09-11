"""Slice 2a tests: pi web engine facade, event translation, and API dispatch.

Event shapes mirror the live captures against pi 0.85.1 driving GLM-4.7.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from naumi_agent.api.routes import engines as engines_route
from naumi_agent.api.routes import messages as messages_route
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.pi_engine.rpc import PiEventType
from naumi_agent.pi_engine.web_events import PiWebEventTranslator
from naumi_agent.pi_engine.web_facade import PiWebEngine, PiWebEngineError
from naumi_agent.runtime.ports.events import RuntimeEventType

# ---------------------------------------------------------------------------
# event translation


def test_translator_maps_full_tool_roundtrip() -> None:
    translator = PiWebEventTranslator(session_id="s1")
    events: list = []
    for raw in [
        {"type": PiEventType.AGENT_START},
        {"type": PiEventType.TURN_START},
        {
            "type": PiEventType.MESSAGE_UPDATE,
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "想一想"},
        },
        {
            "type": PiEventType.MESSAGE_UPDATE,
            "assistantMessageEvent": {"type": "text_start", "contentIndex": 0},
        },
        {
            "type": PiEventType.MESSAGE_UPDATE,
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "好的"},
        },
        {
            "type": PiEventType.TOOL_EXECUTION_START,
            "toolCallId": "call_1",
            "toolName": "bash",
            "args": {"command": "echo hi"},
        },
        {
            "type": PiEventType.TOOL_EXECUTION_END,
            "toolCallId": "call_1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "hi\n"}]},
            "isError": False,
        },
        {
            "type": PiEventType.MESSAGE_END,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "好的"}],
                "stopReason": "stop",
                "usage": {"totalTokens": 42, "cost": {"total": 0.001}},
            },
        },
        {"type": PiEventType.AGENT_SETTLED},
    ]:
        events.extend(translator.feed(raw))

    types = [e.type for e in events]
    assert RuntimeEventType.RESPONSE_START in types
    assert RuntimeEventType.TURN_START in types
    assert RuntimeEventType.THINKING_DELTA in types
    token_events = [e for e in events if e.type is RuntimeEventType.TOKEN]
    assert token_events[0].data["content"] == "好的"
    tool_start = next(e for e in events if e.type is RuntimeEventType.TOOL_START)
    assert tool_start.data["name"] == "bash"
    assert json.loads(tool_start.data["arguments"]) == {"command": "echo hi"}
    assert "echo hi" in tool_start.data["activity_summary"]
    tool_end = next(e for e in events if e.type is RuntimeEventType.TOOL_END)
    assert tool_end.data["status"] == "success"
    assert tool_end.data["result"] == "hi\n"
    assert translator.final_text() == "好的"
    assert translator.turn == 1
    assert translator.total_tokens == 42
    assert translator.total_cost_usd == pytest.approx(0.001)
    assert translator.error == ""


def test_translator_maps_model_error() -> None:
    translator = PiWebEventTranslator()
    events = translator.feed(
        {
            "type": PiEventType.MESSAGE_END,
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "error": "401",
            },
        }
    )
    assert events[0].type is RuntimeEventType.ERROR
    assert "401" in events[0].data["message"]
    assert translator.error == "401"


def test_translator_tool_error_event() -> None:
    translator = PiWebEventTranslator()
    events = translator.feed(
        {
            "type": PiEventType.TOOL_EXECUTION_END,
            "toolCallId": "x",
            "toolName": "edit",
            "result": {"content": [{"type": "text", "text": "boom"}]},
            "isError": True,
        }
    )
    assert events[0].type is RuntimeEventType.TOOL_ERROR
    assert events[0].data["status"] == "error"


# ---------------------------------------------------------------------------
# facade with a fake rpc


class FakeWebRpc:
    def __init__(self) -> None:
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.prompts: list[str] = []
        self.switched: list[str] = []
        self.dialogs_cancelled: list[str] = []
        self.state = {
            "sessionId": "pi-1",
            "sessionFile": "/tmp/pi/sessions/a.jsonl",
            "model": {"id": "glm-4.7", "provider": "zai-coding-cn"},
        }

    async def prompt(self, message: str, *, images=None) -> None:
        self.prompts.append(message)

    async def abort(self) -> None:
        return None

    async def new_session(self) -> None:
        self.state = {
            "sessionId": "pi-2",
            "sessionFile": "/tmp/pi/sessions/b.jsonl",
            "model": {"id": "glm-4.7"},
        }

    async def get_state(self) -> dict:
        return dict(self.state)

    async def request(self, command_type: str, **fields):
        if command_type == "switch_session":
            self.switched.append(str(fields.get("sessionPath")))
            self.state = {**self.state, "sessionFile": str(fields.get("sessionPath"))}
            return {}
        raise AssertionError(f"unexpected command {command_type}")

    async def send_extension_ui_response(self, request_id: str, fields: dict) -> None:
        self.dialogs_cancelled.append((request_id, fields))

    async def stop(self) -> None:
        return None

    def emit(self, event: dict) -> None:
        self.event_queue.put_nowait(event)

    def emit_simple_run(self, text: str = "done") -> None:
        self.emit({"type": PiEventType.AGENT_START})
        self.emit({"type": PiEventType.TURN_START})
        self.emit(
            {
                "type": PiEventType.MESSAGE_UPDATE,
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": text,
                },
            }
        )
        self.emit(
            {
                "type": PiEventType.MESSAGE_END,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "stopReason": "stop",
                    "usage": {"totalTokens": 10, "cost": {"total": 0.01}},
                },
            }
        )
        self.emit({"type": PiEventType.AGENT_SETTLED})


class FakeSessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    async def create_session(
        self,
        title: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        engine: str | None = None,
        **kwargs,
    ) -> Session:
        session = Session(title=title or "新会话", engine=engine or "naumi")
        if system_prompt:
            session.add_message("system", system_prompt)
        self.sessions[session.id] = session
        return session

    async def load(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def save(self, session: Session) -> None:
        self.sessions[session.id] = session


@dataclass
class _FakeConfig:
    workspace_root: str = "/tmp/ws"
    api: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(api_keys=[])
    )
    engine: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(
            provider="pi",
            pi=SimpleNamespace(
                binary="pi", provider="zai-coding-cn", model="glm-4.7",
                extra_args=[], env={},
            ),
        )
    )

    def resolve_workspace_root(self) -> Path:
        return Path(self.workspace_root)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


async def _facade_with_fake(rpc: FakeWebRpc) -> tuple[PiWebEngine, FakeSessionStore]:
    store = FakeSessionStore()
    session = Session(id="web-1", engine="pi")
    store.sessions["web-1"] = session
    facade = PiWebEngine(_FakeConfig(), store)
    facade._rpc = rpc  # inject the fake transport
    facade._events = rpc.event_queue
    return facade, store


async def test_facade_streams_and_persists_history() -> None:
    rpc = FakeWebRpc()
    facade, store = await _facade_with_fake(rpc)
    assert await facade.load_session("web-1") is True

    sink = RecordingSink()

    async def drive() -> None:
        await asyncio.sleep(0.05)
        rpc.emit_simple_run("回复内容")

    driver = asyncio.get_event_loop().create_task(drive())
    result = await facade.run_streaming("你好", sink)
    await driver

    assert result.status == "completed"
    assert result.response == "回复内容"
    assert result.usage.turns == 1
    assert result.usage.total_cost_usd == pytest.approx(0.01)
    assert rpc.prompts == ["你好"]
    types = [e.type for e in sink.events]
    assert RuntimeEventType.RESPONSE_START in types
    assert RuntimeEventType.TOKEN in types
    assert RuntimeEventType.RESPONSE_END not in types

    session = store.sessions["web-1"]
    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant"]
    assert session.messages[-1]["content"] == "回复内容"
    assert session.total_tokens == 10
    assert facade._pi_session_files["web-1"] == "/tmp/pi/sessions/a.jsonl"


async def test_facade_switches_pi_session_per_web_session() -> None:
    rpc = FakeWebRpc()
    facade, store = await _facade_with_fake(rpc)
    store.sessions["web-2"] = Session(id="web-2", engine="pi")

    async def run_one(session_id: str) -> None:
        assert await facade.load_session(session_id)
        sink = RecordingSink()

        async def drive() -> None:
            await asyncio.sleep(0.03)
            rpc.emit_simple_run("ok")

        driver = asyncio.get_event_loop().create_task(drive())
        await facade.run_streaming("hi", sink)
        await driver

    await run_one("web-1")
    await run_one("web-2")
    await run_one("web-1")  # back to the first pi conversation
    # web-2 opened a NEW pi session; returning to web-1 switches back.
    assert rpc.switched == ["/tmp/pi/sessions/a.jsonl"]
    assert facade._pi_session_files == {
        "web-1": "/tmp/pi/sessions/a.jsonl",
        "web-2": "/tmp/pi/sessions/b.jsonl",
    }


async def test_facade_cancels_extension_dialogs() -> None:
    rpc = FakeWebRpc()
    facade, store = await _facade_with_fake(rpc)
    await facade.load_session("web-1")
    sink = RecordingSink()

    async def drive() -> None:
        await asyncio.sleep(0.03)
        rpc.emit(
            {
                "type": PiEventType.EXTENSION_UI_REQUEST,
                "id": "d1",
                "method": "confirm",
                "title": "允许？",
                "message": "run something",
            }
        )
        rpc.emit_simple_run("ok")

    driver = asyncio.get_event_loop().create_task(drive())
    result = await facade.run_streaming("hi", sink)
    await driver
    assert result.status == "completed"
    assert rpc.dialogs_cancelled == [("d1", {"cancelled": True})]


async def test_facade_serializes_runs() -> None:
    rpc = FakeWebRpc()
    facade, store = await _facade_with_fake(rpc)
    await facade.load_session("web-1")
    release = asyncio.Event()

    async def hold_prompt(message: str, *, images=None) -> None:
        rpc.prompts.append(message)
        await release.wait()

    rpc.prompt = hold_prompt  # type: ignore[method-assign]

    first = asyncio.get_event_loop().create_task(
        facade.run_streaming("一", RecordingSink())
    )
    await asyncio.sleep(0.05)
    with pytest.raises(PiWebEngineError, match="一次只能执行一个"):
        await facade.run_streaming("二", RecordingSink())
    rpc.emit_simple_run("ok")
    release.set()
    result = await first
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# session store engine column


async def test_session_store_persists_engine_column(tmp_path: Path) -> None:
    from naumi_agent.config.settings import MemoryConfig

    store = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "s.db")))
    await store.create_session(title="pi 会话", engine="pi")
    await store.create_session(title="普通")
    sessions, _total = await store.list_sessions(page=1, page_size=10)
    by_title = {s.title: s for s in sessions}
    assert by_title["pi 会话"].engine == "pi"
    assert by_title["普通"].engine == "naumi"

    loaded = await store.load(by_title["pi 会话"].id)
    assert loaded is not None and loaded.engine == "pi"


# ---------------------------------------------------------------------------
# API routes: engines + dispatch


class _RouteEngine:
    def __init__(self, store) -> None:
        self.session_store = store

    async def create_session(self, *, title=None, model=None, system_prompt=None,
                             engine=None, **kwargs):
        return await self.session_store.create_session(
            title=title, model=model, system_prompt=system_prompt, engine=engine,
        )


def _api_app(config=None, engine=None) -> FastAPI:
    app = FastAPI()
    app.state.engine = engine or _RouteEngine(FakeSessionStore())
    app.state.config = config or _FakeConfig()
    app.state.chat_run_store = None
    app.include_router(engines_route.router)
    app.include_router(messages_route.router)
    return app


def test_engines_endpoint_lists_both_engines() -> None:
    app = _api_app(_FakeConfig())
    client = TestClient(app)
    response = client.get("/engines")
    assert response.status_code == 200
    payload = response.json()
    assert payload["default"] == "pi"
    by_id = {e["id"]: e for e in payload["engines"]}
    assert by_id["naumi"]["available"] is True
    assert by_id["naumi"]["default"] is False
    assert isinstance(by_id["pi"]["available"], bool)
    assert "编码代理" in by_id["pi"]["description"]


def test_create_session_accepts_engine_field() -> None:
    app = _api_app()
    client = TestClient(app)
    response = client.post("/sessions", json={"engine": "pi", "title": "pi 对话"})
    assert response.status_code == 201
    assert response.json()["engine"] == "pi"

    response = client.post("/sessions", json={"title": "默认"})
    assert response.json()["engine"] == "pi"  # config default applies

    response = client.post("/sessions", json={"engine": "cuda"})
    assert response.status_code == 422


def test_create_session_rejects_unavailable_pi() -> None:
    config = _FakeConfig()
    config.engine.pi.binary = "definitely-not-a-real-pi-binary"
    app = _api_app(config)
    client = TestClient(app)
    response = client.post("/sessions", json={"engine": "pi"})
    assert response.status_code == 400
    assert "不可用" in response.json()["detail"]


def test_send_message_dispatches_to_pi_facade(monkeypatch) -> None:
    store = FakeSessionStore()
    store.sessions["web-1"] = Session(id="web-1", engine="pi")
    app = _api_app(engine=_RouteEngine(store))

    captured: dict = {}

    class FakeFacade:
        runtime_mode = "default"

        def set_runtime_mode(self, mode: str) -> None:
            captured["mode"] = mode

        async def load_session(self, session_id: str) -> bool:
            captured["loaded"] = session_id
            return True

        async def run_streaming(self, content, sink, turn_context=""):
            captured["content"] = content

            async def drive() -> None:
                from naumi_agent.streaming.events import EventType, StreamEvent

                await asyncio.sleep(0.02)
                await sink._consumer(  # noqa: SLF001 - inject through the route's sink
                    StreamEvent(
                        type=EventType.TOKEN_DELTA,
                        data={"token": "pi 回复"},
                        session_id="web-1",
                    )
                )

            asyncio.get_event_loop().create_task(drive())
            return SimpleNamespace(
                status="completed",
                response="pi 回复",
                error="",
                usage=SimpleNamespace(turns=1, total_cost_usd=0.0),
            )

    monkeypatch.setattr(
        messages_route, "_pi_web_engine", lambda request: FakeFacade()
    )
    client = TestClient(app)
    with client.stream(
        "POST", "/sessions/web-1/messages", json={"content": "你好", "stream": True}
    ) as response:
        assert response.status_code == 200
        body = "".join(chunk for chunk in response.iter_text())
    assert "token_delta" in body
    assert "pi 回复" in body
    assert body.count('"type": "agent_end"') == 1
    assert captured["content"] == "你好"
    assert captured["loaded"] == "web-1"


def test_send_message_rejects_pi_unsupported_extras() -> None:
    store = FakeSessionStore()
    store.sessions["web-1"] = Session(id="web-1", engine="pi")
    app = _api_app(engine=_RouteEngine(store))
    client = TestClient(app)
    response = client.post(
        "/sessions/web-1/messages",
        json={"content": "hi", "source_ids": ["src-1"]},
    )
    assert response.status_code == 400
    assert "资料源" in response.json()["detail"]


def test_send_message_rejects_pi_history_edit() -> None:
    store = FakeSessionStore()
    session = Session(id="web-1", engine="pi")
    session.add_message("user", "旧问题")
    store.sessions["web-1"] = session
    app = _api_app(engine=_RouteEngine(store))
    client = TestClient(app)

    response = client.post(
        "/sessions/web-1/messages",
        json={
            "content": "修改后的问题",
            "edit_message_id": "msg-1",
        },
    )

    assert response.status_code == 400
    assert "pi 引擎会话暂不支持编辑历史消息" in response.json()["detail"]
