"""Command transport contract and real command/session persistence tests."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from naumi_agent import __version__
from naumi_agent.api.routes.commands import _CommandEngine, router, validate_command
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tools.base import ToolCall, ToolResult


@pytest.mark.parametrize(
    "command", ["/q", "/load id", "/version; /q", "/version && /q", "/unknown", "/goal pursue"]
)
def test_reject_unavailable_or_batched_command(command):
    with pytest.raises(HTTPException):
        validate_command(command)


def test_alias_and_quoted_separator():
    assert validate_command("/v") == "/version"
    assert validate_command('/read "a;b.txt"').startswith("/read ")


async def test_tool_ids_are_url_safe_and_failures_are_recorded():
    async def execute_tool(call, **kwargs):
        assert "/" not in call.id
        assert call.name == "file_write"
        assert callable(kwargs["on_event"])
        return ToolResult(call_id=call.id, status="error", content="拒绝执行")

    async def on_event(kind, data):
        pass

    adapter = _CommandEngine(SimpleNamespace(execute_tool=execute_tool), on_event)
    await adapter.execute_tool(
        ToolCall(id="slash-/write-original", name="file_write", arguments="{}")
    )
    assert adapter.failed


async def test_actual_version_handler_streams_and_persists(tmp_path):
    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="命令验收")
    engine = SimpleNamespace(session_store=sessions, runtime_mode="default")

    async def load_session(identifier):
        return await sessions.load(identifier)

    engine.load_session = load_session
    engine.set_runtime_mode = lambda mode: setattr(engine, "runtime_mode", mode)
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    app.state.chat_run_store = ChatRunStore(str(tmp_path / "runs.db"))
    app.state.engine_lock = asyncio.Lock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        index = (await client.get("/commands")).json()["commands"]
        assert any(item["command"] == "/version" for item in index)
        response = await client.post(
            f"/sessions/{session.id}/commands", json={"command": "/version"}
        )
        assert response.status_code == 200
        events = [json.loads(frame[6:]) for frame in response.text.strip().split("\n\n")]
        assert events[0]["type"] == "agent_start"
        assert __version__ in next(
            event["data"]["token"] for event in events if event["type"] == "token_delta"
        )
        assert events[-1]["data"]["status"] == "completed"
        saved = await sessions.load(session.id)
        assert saved.messages[-2]["content"] == "/version"
        assert __version__ in saved.messages[-1]["content"]
        runs = await app.state.chat_run_store.list_runs(session.id)
        assert runs[0].status == "completed"
        assert runs[0].steps[0].summary == "/version"
        assert engine.runtime_mode == "default"
        async with app.state.engine_lock:
            assert (
                await client.post(f"/sessions/{session.id}/commands", json={"command": "/version"})
            ).status_code == 409
