"""Concurrent UI reads must not replace a live Agent turn's identity or lineage.

Only the model transport is gated. Engines, authenticated HTTP/WebSocket ASGI,
tools, private artifact bytes, session persistence and Run records remain real.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass

import httpx
import pytest

from naumi_agent.api.app import create_app
from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
_KEY = "session-race-fixture-key"
_TASK = "读取我的预算资料并保存一份有确切来源的报告。"
_ANSWER = "B_REPORT_DONE：已保存预算报告。"
_TIMEOUT = 15


@dataclass
class RaceContext:
    engine: object
    app: object
    client: httpx.AsyncClient
    actors: tuple[str, str]
    sessions: tuple[str, str]
    source: dict
    approval_id: str = ""
    approval_task_id: str = ""


@pytest.fixture
async def session_race(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))

    async def no_provider(self, **kwargs):
        raise AssertionError("The session-race fixture must not contact a model provider")

    async def no_stream(self, **kwargs):
        raise AssertionError("Install the controlled stream before starting a turn")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "public.txt").write_text("public fixture only", encoding="utf-8")
    (workspace / ".gitignore").write_text(".naumi/\n", encoding="utf-8")
    for arguments in (
        ["init", "-q"],
        ["add", "public.txt", ".gitignore"],
        [
            "-c", "user.name=Race Fixture", "-c", "user.email=race@example.invalid",
            "commit", "-qm", "Record session race fixture",
        ],
    ):
        subprocess.run(
            ["git", "-C", str(workspace), *arguments], check=True, capture_output=True,
        )
    config = AppConfig(
        workspace_root=str(workspace),
        api={"api_keys": [_KEY]},
        personal={"data_dir": str(tmp_path / "data/personal")},
        memory={
            "session_db_path": str(tmp_path / "data/sessions.db"),
            "vector_db_path": str(tmp_path / "data/vectors"),
            "long_term_enabled": False,
        },
        safety={"permission_mode": "bypass", "max_turns": 5},
    )
    engine = create_agent_engine(config)
    try:
        await engine.get_or_create_session()
        engine.personal_teams.start = lambda: None
        engine.personal_collaborations.start = lambda: None
        profiles = [
            engine.personal_agents.create(
                request_id=f"race-agent-{name}", name=name,
                instructions=f"只处理 {name} 的用户委托，保持私有来源。",
                tool_names=["personal_artifacts"], model_tier="fast",
            )
            for name in ("A", "B")
        ]
        sessions = tuple(
            [(await engine.personal_agents.open_session(p.id))["session_id"] for p in profiles]
        )
        source = await engine.personal_artifacts.publish(
            actor_id=profiles[1].id, request_id="race-source-b", title="B 私有预算",
            kind="material", filename="budget.csv", content=b"amount\n825\n",
        )
        app = create_app()
        app.state.engine, app.state.config = engine, config
        app.state.chat_run_store = engine.chat_run_store
        app.state.engine_lock = asyncio.Lock()
        app.state.active_chat_run_tasks = {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://session-race.test/api/v1",
            headers={"X-API-Key": _KEY},
        ) as client:
            yield RaceContext(
                engine, app, client, tuple(p.id for p in profiles), sessions, source,
            )
    finally:
        await engine.shutdown()


def _tool_chunk(operation, arguments, identity):
    return StreamChunk(
        tool_call={
            0: {
                "id": identity,
                "type": "function",
                "function": {
                    "name": "personal_artifacts",
                    "arguments": json.dumps(
                        {"operation": operation, **arguments}, ensure_ascii=False,
                    ),
                },
            },
        },
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=10, output_tokens=10, cost_usd=0),
    )


async def _read_websocket_snapshot(context, *, session_id=None, expect_missing=False):
    """Drive the real ASGI WebSocket stack using protocol frames, not a route mock."""
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    selected = session_id or context.sessions[0]
    path = f"/api/v1/workbench/sessions/{selected}/events/stream"
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"include_snapshot=true", "subprotocols": [],
        "headers": [(b"host", b"session-race.test"), (b"x-api-key", _KEY.encode())],
        "client": ("127.0.0.1", 12345), "server": ("session-race.test", 80),
    }
    await incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(context.app(scope, incoming.get, outgoing.put))
    frames = []
    try:
        async with asyncio.timeout(_TIMEOUT):
            first = await outgoing.get()
            assert first["type"] == "websocket.accept", first
            if expect_missing:
                frame = await outgoing.get()
                assert frame["type"] == "websocket.send", frame
                assert json.loads(frame["text"]) == {
                    "type": "error", "message": "Session not found",
                }
                return
            while not any(frame.get("type") == "workbench/snapshot" for frame in frames):
                frame = await outgoing.get()
                assert frame["type"] == "websocket.send", frame
                frames.append(json.loads(frame["text"]))
            snapshot = next(frame for frame in frames if frame["type"] == "workbench/snapshot")
            assert snapshot["payload"]["session_id"] == selected
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(task, _TIMEOUT)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def _interfere(context, action):
    engine, session_a = context.engine, context.sessions[0]
    if action == "snapshot":
        response = await context.client.get(f"/workbench/sessions/{session_a}/snapshot")
        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == session_a
    elif action == "bootstrap":
        # Saving updates A's registry timestamp without touching the live runtime.
        saved = await engine.session_store.load(session_a)
        await engine.session_store.save(saved)
        response = await context.client.get("/workbench/bootstrap")
        assert response.status_code == 200, response.text
        assert response.json()["selected_session_id"] == session_a
    elif action == "websocket":
        await _read_websocket_snapshot(context)
    elif action == "create":
        response = await context.client.post("/sessions", json={"title": "Concurrent C"})
        assert response.status_code == 201, response.text
        return response.json()["id"], ""
    elif action == "resolve-approval":
        assert context.app.state.engine_lock.locked()
        response = await asyncio.wait_for(
            context.client.post(
                f"/workbench/sessions/{session_a}/approvals/{context.approval_id}/resolve",
                json={"actor": "Human", "state": "approved", "decision_note": "同意 A 的请求"},
            ),
            timeout=5,
        )
        assert response.status_code == 200, response.text
        approval = response.json()
        assert approval["id"] == context.approval_id and approval["state"] == "approved"
        assert approval["session_id"] == session_a and approval["reviewer"] == "Human"
        assert approval["decision_note"] == "同意 A 的请求"
        assert approval["task"]["id"] == context.approval_task_id
        assert approval["task"]["session_id"] == session_a
        stored = await engine.workbench_store.get_approval(session_a, context.approval_id)
        assert stored is not None
        assert stored.state.value == "approved" and stored.reviewer == "Human"
        assert context.app.state.engine_lock.locked(), (
            "Approval resolution must bypass the run lock"
        )
    else:
        target = context.sessions[1] if action == "direct-same" else session_a
        try:
            await engine.load_session(target)
        except RuntimeError as error:
            return "", str(error)
        return "", ""
    return "", ""


@pytest.mark.parametrize(
    "action", [
        "snapshot", "bootstrap", "websocket", "create", "direct-other", "direct-same",
        "resolve-approval",
    ],
)
async def test_active_turn_keeps_session_actor_artifacts_refs_and_run_lineage(
    session_race, monkeypatch, action,
):
    context = session_race
    engine, service = context.engine, context.engine.personal_artifacts
    session_a, session_b = context.sessions
    actor_a, actor_b = context.actors
    gate_entered, release_gate = asyncio.Event(), asyncio.Event()
    call_count, tool_results = 0, []
    expected_ref = service.reference(context.source)
    if action == "resolve-approval":
        task = await engine.task_store.scoped(session_a).create_task(
            "审核 A 的预算请求", description="审批回执不得切换正在执行的 B 身份。",
        )
        approval = await engine.workbench_store.add_approval(
            session_id=session_a, task_id=task.id, mission_id="race-approval-mission",
            title="批准 A 的请求", detail="保持 B 的资料与运行归属", requester="A",
        )
        context.approval_id, context.approval_task_id = approval.id, task.id
    original_a = await engine.session_store.load(session_a)
    original_a_messages = list(original_a.messages)

    async def controlled_stream(self, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield _tool_chunk(
                "read", {"artifact_id": context.source["artifact_id"], "version": 1}, "race-read",
            )
        elif call_count == 2:
            messages = [message for message in kwargs["messages"] if message["role"] == "tool"]
            read = json.loads(messages[-1]["content"])
            assert read["artifact"]["owner_id"] == actor_b
            assert read["content"] == "amount\n825\n"
            gate_entered.set()
            await release_gate.wait()
            yield _tool_chunk(
                "publish",
                {
                    "request_id": "race-output-b", "title": "B 预算报告", "kind": "output",
                    "filename": "report.md", "content": "B_REPORT_DONE\n结余 825。",
                    "source_refs": [expected_ref],
                },
                "race-publish",
            )
        elif call_count == 3:
            messages = [message for message in kwargs["messages"] if message["role"] == "tool"]
            tool_results.append(messages[-1]["content"])
            yield StreamChunk(token=_ANSWER)
            yield StreamChunk(
                finish_reason="stop",
                usage=TokenUsage(input_tokens=10, output_tokens=10, cost_usd=0),
            )
        else:
            raise AssertionError(f"Unexpected model retry: {call_count}")

    monkeypatch.setattr(ModelRouter, "stream", controlled_stream)
    request = asyncio.create_task(
        context.client.post(
            f"/sessions/{session_b}/messages", json={"content": _TASK, "stream": True},
        )
    )
    created, direct_error, during = "", "", None
    try:
        await asyncio.wait_for(gate_entered.wait(), _TIMEOUT)
        assert engine._session.id == session_b
        assert service.engine_actor() == actor_b
        assert await service.session_refs(session_b, actor_b) == [expected_ref]
        async with asyncio.timeout(_TIMEOUT):
            created, direct_error = await _interfere(context, action)
        assert not release_gate.is_set(), "The concurrent operation must finish before B resumes"
        during = (engine._session.id, service.engine_actor())
    finally:
        release_gate.set()
        try:
            response = await asyncio.wait_for(request, _TIMEOUT)
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    assert during == (session_b, actor_b), (
        f"{action} replaced the running identity: expected B {(session_b, actor_b)}, got {during}"
    )
    if action.startswith("direct-"):
        assert direct_error, "load_session must reject all live-turn reloads, including the same ID"
    assert response.status_code == 200, response.text
    assert _ANSWER in response.text, response.text
    assert '"type": "agent_error"' not in response.text, response.text
    assert call_count == 3 and len(tool_results) == 1
    output = json.loads(tool_results[0])["artifact"]
    assert output["owner_id"] == actor_b
    assert output["source_refs"] == [expected_ref]
    assert output["evidence"]["session_id"] == session_b
    runs = await engine.chat_run_store.list_runs(session_b)
    assert len(runs) == 1 and runs[0].session_id == session_b
    run = runs[0]
    assert run.status == "completed" and run.receipt is not None, run
    assert run.receipt.run_id == run.id
    assert output["evidence"]["run_id"] == run.id
    saved_b = await engine.session_store.load(session_b)
    assert saved_b.personal_agent_id == actor_b
    user = next(message for message in saved_b.messages if message.get("content") == _TASK)
    answer = next(message for message in saved_b.messages if message.get("content") == _ANSWER)
    assert user["role"] == "user" and answer["role"] == "assistant"
    assert run.user_message_id and run.receipt.summary == _ANSWER
    assert any(step.stage == "request" and step.summary == _TASK for step in run.steps)
    assert answer["metadata"]["artifact_refs"] == [service.reference(output)]
    assert await service.session_refs(session_b, actor_b) == [expected_ref]
    sources_b = await engine.chat_run_store.list_sources(session_b)
    assert any(
        source.metadata.get("artifact_ref") == service.reference(output) for source in sources_b
    )
    outputs_b = await service.list(actor_id=actor_b, kind="output")
    assert len(outputs_b) == 1 and outputs_b[0]["artifact_id"] == output["artifact_id"]
    assert await service.list(actor_id=actor_a) == []
    assert await service.session_refs(session_a, actor_a) == []
    assert await engine.chat_run_store.list_sources(session_a) == []
    assert await engine.chat_run_store.list_runs(session_a) == []
    assert (await engine.session_store.load(session_a)).messages == original_a_messages
    if created:
        assert created not in context.sessions
        assert await engine.chat_run_store.list_runs(created) == []
        assert await engine.chat_run_store.list_sources(created) == []
        assert not any(
            message.get("role") in {"user", "assistant", "tool"}
            for message in (await engine.session_store.load(created)).messages
        )


@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_read_scope_rejects_foreign_workspace_without_rebinding(session_race, transport):
    context = session_race
    engine, actor_b = context.engine, context.actors[1]
    session_b = context.sessions[1]
    assert await engine.load_session(session_b)
    foreign = await engine.session_store.create_session(
        title="Foreign workspace", workspace_root=str(engine.workspace_root.parent / "other"),
    )
    if transport == "http":
        response = await context.client.get(f"/workbench/sessions/{foreign.id}/snapshot")
        assert response.status_code == 404, response.text
    else:
        await _read_websocket_snapshot(context, session_id=foreign.id, expect_missing=True)
    assert engine._session.id == session_b
    assert engine.personal_artifacts.engine_actor() == actor_b
    assert await engine.chat_run_store.list_runs(foreign.id) == []
    assert await engine.chat_run_store.list_sources(foreign.id) == []


async def test_idle_session_switch_remains_available_and_preserves_each_identity(session_race):
    context = session_race
    for index in (0, 1, 0):
        assert await context.engine.load_session(context.sessions[index])
        assert context.engine._session.id == context.sessions[index]
        assert context.engine.personal_artifacts.engine_actor() == context.actors[index]
    for session_id, actor_id in zip(context.sessions, context.actors):
        saved = await context.engine.session_store.load(session_id)
        assert saved.personal_agent_id == actor_id
        assert await context.engine.chat_run_store.list_runs(session_id) == []


class ObservedLock(asyncio.Lock):
    """Expose an actual contender reaching acquire, without scheduler sleeps."""

    def __init__(self):
        super().__init__()
        self.contender_waiting = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.contender_waiting.set()
        return await super().acquire()


async def test_workbench_create_waits_for_live_turn_before_loading_new_session(
    session_race, monkeypatch,
):
    context = session_race
    engine, session_b, actor_b = context.engine, context.sessions[1], context.actors[1]
    gate_entered, release_gate = asyncio.Event(), asyncio.Event()
    lock = ObservedLock()
    context.app.state.engine_lock = lock

    async def controlled_stream(self, **kwargs):
        gate_entered.set()
        await release_gate.wait()
        yield StreamChunk(token=_ANSWER)
        yield StreamChunk(
            finish_reason="stop", usage=TokenUsage(input_tokens=10, output_tokens=10, cost_usd=0),
        )

    monkeypatch.setattr(ModelRouter, "stream", controlled_stream)
    running = asyncio.create_task(
        context.client.post(
            f"/sessions/{session_b}/messages", json={"content": _TASK, "stream": True},
        )
    )
    mutation = None
    try:
        await asyncio.wait_for(gate_entered.wait(), _TIMEOUT)
        assert lock.locked()
        mutation = asyncio.create_task(
            context.client.post("/workbench/sessions", json={"title": "After B"})
        )
        await asyncio.wait_for(lock.contender_waiting.wait(), _TIMEOUT)
        assert not mutation.done(), "Workbench writes must wait for the whole live turn"
        assert engine._session.id == session_b
        assert engine.personal_artifacts.engine_actor() == actor_b
    finally:
        release_gate.set()
        tasks = [running, *([mutation] if mutation is not None else [])]
        try:
            responses = await asyncio.wait_for(asyncio.gather(*tasks), _TIMEOUT)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert len(responses) == 2
    chat, created = responses
    assert chat.status_code == 200 and _ANSWER in chat.text, chat.text
    assert created.status_code == 201, created.text
    session_c = created.json()["selected_session_id"]
    assert session_c not in context.sessions
    assert created.json()["snapshot"]["session_id"] == session_c
    assert engine._session.id == session_c
    assert engine.personal_artifacts.engine_actor() == "main"
    saved_b = await engine.session_store.load(session_b)
    assert any(message.get("content") == _TASK for message in saved_b.messages)
    assert any(message.get("content") == _ANSWER for message in saved_b.messages)
    runs = await engine.chat_run_store.list_runs(session_b)
    assert len(runs) == 1 and runs[0].status == "completed" and runs[0].receipt is not None
    assert runs[0].receipt.run_id == runs[0].id
    assert await engine.chat_run_store.list_runs(session_c) == []
