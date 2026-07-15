"""API 路由单元测试."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import naumi_agent.api.routes.messages as message_routes
from naumi_agent import __version__
from naumi_agent.api.chat_runs import ChatRunStore
from naumi_agent.api.permission_broker import PermissionApprovalBroker
from naumi_agent.api.routes.messages import (
    _stream_response,
    add_chat_source,
    cancel_chat_run,
    delete_session,
    get_chat_environment,
    list_chat_runs,
    list_messages,
    send_message,
)
from naumi_agent.api.routes.ws import _run_streaming_to_websocket
from naumi_agent.api.schemas import HealthResponse, MessageCreate, SessionCreate
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.ports.events import EventSink, RuntimeEvent, RuntimeEventType


class TestSchemas:
    def test_session_create(self) -> None:
        s = SessionCreate(title="test")
        assert s.title == "test"

    def test_session_create_defaults(self) -> None:
        s = SessionCreate()
        assert s.title is None

    def test_health_response(self) -> None:
        h = HealthResponse(
            status="healthy",
            version=__version__,
            uptime_seconds=0.0,
            active_sessions=0,
        )
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
        assert data["version"] == __version__


class _FakeSessionStore:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def load(self, session_id: str):
        return SimpleNamespace(id=session_id, messages=self.messages)


class _FakeEngine:
    def __init__(self) -> None:
        self.session_store = _FakeSessionStore()
        self.loaded: list[str] = []
        self.ran: list[str] = []
        self.workbench_service = SimpleNamespace(
            create_issue=self._create_issue,
            dashboard_snapshot=self._dashboard_snapshot,
        )
        self.created_issues: list[dict] = []
        self.turn_contexts: list[str] = []
        self.workspace_root = "."
        self._runtime_mode = "default"
        self.runtime_mode_transitions: list[str] = []
        self.background_runner = SimpleNamespace(
            store=SimpleNamespace(list_tasks=lambda: [])
        )
        self.deleted: list[str] = []
        self.delete_result = True

    @property
    def runtime_mode(self):
        return SimpleNamespace(value=self._runtime_mode)

    def set_runtime_mode(self, mode):
        value = getattr(mode, "value", mode)
        self._runtime_mode = str(value)
        self.runtime_mode_transitions.append(self._runtime_mode)
        return SimpleNamespace(value=self._runtime_mode)

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        return True

    async def delete_session(self, session_id: str) -> bool:
        self.deleted.append(session_id)
        return self.delete_result

    async def run(self, content: str, turn_context: str = ""):
        self.ran.append(content)
        self.turn_contexts.append(turn_context)
        usage = SimpleNamespace(turns=1, total_cost_usd=0.01)
        return SimpleNamespace(status="completed", response="ok", usage=usage)

    async def run_streaming(self, content: str, on_event, turn_context: str = ""):
        self.ran.append(content)
        self.turn_contexts.append(turn_context)
        await on_event("token", {"content": "你"})
        usage = SimpleNamespace(turns=1, total_cost_usd=0.01)
        return SimpleNamespace(status="completed", response="你", usage=usage)

    async def _create_issue(self, **kwargs):
        self.created_issues.append(
            {
                **kwargs,
                "parallel_mode": kwargs["parallel_mode"].value,
                "risk_level": kwargs["risk_level"].value,
            }
        )
        return {
            "session_id": kwargs["session_id"],
            "mission_id": kwargs["mission_id"],
            "task_id": "task-chat-1",
            "parallel_mode": kwargs["parallel_mode"].value,
            "risk_level": kwargs["risk_level"].value,
            "requires_human_approval": True,
            "acceptance_criteria": kwargs["acceptance_criteria"],
            "expected_artifacts": [],
            "related_branch": "",
            "related_worktree": "",
            "related_pr": "",
            "created_at": "2026-07-02T08:00:00",
            "updated_at": "2026-07-02T08:00:00",
        }

    async def _dashboard_snapshot(self, session_id: str):
        return {
            "session_id": session_id,
            "summary": {"current_mission_title": "日常对话联动"},
            "missions": [],
            "agent_profiles": [],
            "tasks": [],
            "issues": [],
            "leases": [],
            "failures": [],
            "events": [],
        }


def _fake_request(engine: _FakeEngine, chat_run_store=None):
    state = SimpleNamespace(
        engine=engine,
        engine_lock=asyncio.Lock(),
        chat_run_store=chat_run_store,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _async_value(value):
    return value


class TestMessageRoutes:
    @pytest.mark.asyncio
    async def test_delete_session_route_delegates_to_engine_lifecycle(self) -> None:
        engine = _FakeEngine()

        response = await delete_session("sess_1", _fake_request(engine), auth="test")

        assert response is None
        assert engine.deleted == ["sess_1"]
        route = next(
            route
            for route in message_routes.router.routes
            if getattr(route, "path", "") == "/sessions/{session_id}"
            and "DELETE" in getattr(route, "methods", set())
        )
        assert route.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_session_route_returns_404_when_engine_cannot_delete(self) -> None:
        engine = _FakeEngine()
        engine.delete_result = False

        with pytest.raises(Exception) as exc:
            await delete_session("missing", _fake_request(engine), auth="test")

        assert exc.value.status_code == 404
        assert exc.value.detail == "Session not found"

    @pytest.mark.asyncio
    async def test_delete_session_route_cleans_active_engine_grants(self, tmp_path) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            session = await engine.get_or_create_session()
            engine._permission_grant_store.create(session.id, "shell", "active-grant")

            response = await delete_session(session.id, _fake_request(engine), auth="test")

            assert response is None
            assert engine._session is None
            assert engine._permission_grant_store.list_session(session.id) == ()
            assert await engine.session_store.load(session.id) is None
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_permission_resolution_route_unblocks_matching_request(self) -> None:
        broker = PermissionApprovalBroker(timeout_seconds=1)
        waiting = asyncio.create_task(
            broker.confirm({"session_id": "sess_1", "call_id": "call_1"})
        )
        await asyncio.sleep(0)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(permission_broker=broker))
        )
        route = getattr(message_routes, "resolve_permission", None)

        assert route is not None
        response = await route(
            "sess_1",
            "call_1",
            SimpleNamespace(decision="allow"),
            request,
            auth="test",
        )

        assert response.status == "resolved"
        assert await waiting == "allow"

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
    async def test_send_message_scopes_runtime_mode_to_one_turn(self) -> None:
        engine = _FakeEngine()
        request = _fake_request(engine)

        await send_message(
            "sess_1",
            MessageCreate(content="inspect only", stream=False, runtime_mode="plan"),
            request,
            auth="test",
        )

        assert engine.runtime_mode_transitions == ["plan", "default"]
        assert engine.runtime_mode.value == "default"

    @pytest.mark.asyncio
    async def test_send_message_can_create_workbench_issue_from_chat(self) -> None:
        engine = _FakeEngine()
        request = _fake_request(engine)

        response = await send_message(
            "sess_1",
            MessageCreate(
                content="把登录失败问题记录成任务",
                workbench_issue={
                    "mission_id": "mission-1",
                    "title": "修复登录失败",
                    "description": "用户在输入正确密码后仍然失败。",
                    "acceptance_criteria": ["正确密码可以登录"],
                    "parallel_mode": "exclusive",
                    "risk_level": "high",
                },
            ),
            request,
            auth="test",
        )

        assert engine.loaded == ["sess_1"]
        assert engine.ran == ["把登录失败问题记录成任务"]
        assert engine.created_issues == [
            {
                "session_id": "sess_1",
                "mission_id": "mission-1",
                "title": "修复登录失败",
                "description": "用户在输入正确密码后仍然失败。",
                "blocked_by": [],
                "acceptance_criteria": ["正确密码可以登录"],
                "parallel_mode": "exclusive",
                "risk_level": "high",
            }
        ]
        assert response.metadata["workbench_issue"]["task_id"] == "task-chat-1"
        assert response.metadata["workbench_snapshot"]["session_id"] == "sess_1"

    @pytest.mark.asyncio
    async def test_explicit_streaming_message_rejects_synchronous_issue_creation(
        self,
    ) -> None:
        engine = _FakeEngine()
        request = _fake_request(engine)

        with pytest.raises(Exception) as exc:
            await send_message(
                "sess_1",
                MessageCreate(
                    content="把这句变成任务",
                    stream=True,
                    workbench_issue={
                        "mission_id": "mission-1",
                        "title": "补齐聊天联动",
                    },
                ),
                request,
                auth="test",
            )

        assert exc.value.status_code == 400
        assert exc.value.detail == "流式对话暂不支持同步创建 Issue"
        assert engine.ran == []
        assert engine.created_issues == []

    @pytest.mark.asyncio
    async def test_implicit_stream_with_issue_falls_back_to_non_streaming(self) -> None:
        # When the client does not explicitly opt into streaming but the default
        # `stream=True` is set alongside a workbench issue, the route silently
        # downgrades to non-streaming so the issue can be created synchronously.
        engine = _FakeEngine()
        request = _fake_request(engine)

        body = MessageCreate(
            content="把这句变成任务",
            workbench_issue={
                "mission_id": "mission-1",
                "title": "隐式降级",
            },
        )
        # The model default for `stream` is True; we did not set it explicitly.
        assert body.stream is True
        assert "stream" not in body.model_fields_set

        response = await send_message("sess_1", body, request, auth="test")

        # Non-streaming path ran and the linked issue metadata is present.
        assert engine.ran == ["把这句变成任务"]
        assert response.metadata["workbench_issue"]["task_id"] == "task-chat-1"

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

    @pytest.mark.asyncio
    async def test_sse_and_websocket_routes_pass_typed_sink_and_preserve_identity(
        self,
    ) -> None:
        runtime_event = RuntimeEvent(
            id="event-route-parity",
            type=RuntimeEventType.RUNTIME_NOTIFICATION,
            data={"message": "后台任务完成", "nested": {"items": [1, 2]}},
            timestamp="2026-07-15T08:00:00+08:00",
            session_id="sess_1",
            run_id="run-route-parity",
            turn=3,
            sequence=11,
        )

        class IdentityEngine(_FakeEngine):
            async def run_streaming(
                self,
                content: str,
                event_sink: EventSink,
                turn_context: str = "",
            ):
                assert isinstance(event_sink, EventSink)
                self.ran.append(content)
                self.turn_contexts.append(turn_context)
                await event_sink.emit(runtime_event)
                usage = SimpleNamespace(turns=1, total_cost_usd=0.01)
                return SimpleNamespace(status="completed", response="ok", usage=usage)

        class RecordingWebSocket:
            def __init__(self, engine) -> None:
                self.app = SimpleNamespace(
                    state=SimpleNamespace(engine=engine, engine_lock=asyncio.Lock())
                )
                self.frames: list[str] = []

            async def send_text(self, frame: str) -> None:
                self.frames.append(frame)

        sse_engine = IdentityEngine()
        sse_records = [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in _stream_response(
                sse_engine,
                "sess_1",
                "hello",
                _fake_request(sse_engine),
            )
        ]
        websocket_engine = IdentityEngine()
        websocket = RecordingWebSocket(websocket_engine)
        await _run_streaming_to_websocket(
            websocket,
            websocket_engine,
            "sess_1",
            "hello",
        )
        websocket_record = json.loads(websocket.frames[0])

        assert sse_records[0] == websocket_record
        assert sse_records[0]["id"] == runtime_event.id
        assert sse_records[0]["event_id"] == runtime_event.id
        assert sse_records[0]["source_event"] == "runtime_notification"
        assert sse_records[0]["sequence"] == 11
        assert sse_records[0]["data"]["data"]["nested"] == {"items": [1, 2]}

    @pytest.mark.asyncio
    async def test_stream_response_persists_run_and_list_endpoint(self, tmp_path) -> None:
        engine = _FakeEngine()
        store = ChatRunStore(tmp_path / "chat-runs.db")
        request = _fake_request(engine, store)

        async for _ in _stream_response(engine, "sess_1", "hello", request):
            pass

        response = await list_chat_runs("sess_1", request, limit=50, auth="test")

        assert len(response.runs) == 1
        assert response.runs[0].status == "completed"
        assert response.runs[0].steps[-1].stage == "response"

    @pytest.mark.asyncio
    async def test_stream_response_reuses_engine_managed_run_and_exposes_receipt(
        self,
        tmp_path,
    ) -> None:
        from naumi_agent.runs.models import CompletionReceipt

        store = ChatRunStore(tmp_path / "chat-runs.db")

        class ManagedRunEngine(_FakeEngine):
            def __init__(self) -> None:
                super().__init__()
                self.chat_run_store = store

            async def run_streaming(
                self,
                content: str,
                on_event,
                turn_context: str = "",
            ):
                run = await store.start_run(
                    session_id="sess_1",
                    user_message_id="msg-engine",
                    run_id="run-engine",
                )
                await on_event("run_started", {"task": content, "run_id": run.id})
                receipt = CompletionReceipt.from_dict(
                    {
                        "schema_version": 1,
                        "receipt_id": "receipt-engine",
                        "run_id": run.id,
                        "outcome": "completed",
                        "summary": "已完成。",
                        "git_state": {"available": False, "dirty": False},
                    }
                )
                await store.finish_run(run.id, status="completed", receipt=receipt)
                await on_event("completion_receipt", receipt.to_dict())
                usage = SimpleNamespace(turns=1, total_cost_usd=0.01)
                return SimpleNamespace(
                    status="completed",
                    response="已完成。",
                    usage=usage,
                    receipt=receipt,
                )

        engine = ManagedRunEngine()
        request = _fake_request(engine, store)
        events = [
            json.loads(chunk.removeprefix("data: "))
            async for chunk in _stream_response(engine, "sess_1", "hello", request)
        ]

        runs = await store.list_runs("sess_1")
        assert len(runs) == 1
        assert runs[0].id == "run-engine"
        receipt_events = [event for event in events if event["type"] == "completion_receipt"]
        assert receipt_events[0]["data"]["receipt_id"] == "receipt-engine"
        response = await list_chat_runs("sess_1", request, limit=50, auth="test")
        assert response.runs[0].receipt["receipt_id"] == "receipt-engine"

    @pytest.mark.asyncio
    async def test_chat_environment_requires_existing_session(self, tmp_path) -> None:
        engine = _FakeEngine()
        engine.session_store.load = lambda _session_id: _async_value(None)
        request = _fake_request(engine, ChatRunStore(tmp_path / "chat-runs.db"))

        with pytest.raises(Exception) as exc:
            await get_chat_environment("missing", request, auth="test")

        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cancel_chat_run_stops_agent_and_persists_cancelled_state(
        self, tmp_path
    ) -> None:
        class SlowEngine(_FakeEngine):
            async def run_streaming(
                self, content: str, on_event, turn_context: str = ""
            ):
                await on_event("turn_start", {})
                await asyncio.Event().wait()

        engine = SlowEngine()
        store = ChatRunStore(tmp_path / "chat-runs.db")
        request = _fake_request(engine, store)
        stream = _stream_response(engine, "sess_1", "hello", request)
        first = json.loads((await anext(stream)).removeprefix("data: "))
        run_id = first["data"]["run_id"]

        response = await cancel_chat_run(
            "sess_1", run_id, request, auth="test"
        )
        remaining = [chunk async for chunk in stream]

        assert response.status == "cancellation_requested"
        assert remaining == []
        run = await store.get_run("sess_1", run_id)
        assert run is not None
        assert run.status == "cancelled"

    @pytest.mark.asyncio
    async def test_add_chat_source_accepts_only_existing_workspace_file(
        self, tmp_path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        source = workspace / "spec.md"
        source.write_text("spec", encoding="utf-8")
        engine = _FakeEngine()
        engine.workspace_root = workspace
        request = _fake_request(engine, ChatRunStore(tmp_path / "chat-runs.db"))

        response = await add_chat_source(
            "sess_1",
            SimpleNamespace(path=str(source), kind="file", title="Product spec"),
            request,
            auth="test",
        )

        assert response.path == "spec.md"
        assert response.title == "Product spec"

        with pytest.raises(Exception) as exc:
            await add_chat_source(
                "sess_1",
                SimpleNamespace(
                    path=str(tmp_path / "outside.md"),
                    kind="file",
                    title="outside",
                ),
                request,
                auth="test",
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_message_sources_are_injected_without_rewriting_user_text(
        self, tmp_path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        source_path = workspace / "spec.md"
        source_path.write_text("Acceptance: chat keeps three columns.", encoding="utf-8")
        engine = _FakeEngine()
        engine.workspace_root = workspace
        store = ChatRunStore(tmp_path / "chat-runs.db")
        source = await store.add_source(
            session_id="sess_1",
            kind="file",
            title="spec.md",
            path="spec.md",
        )
        request = _fake_request(engine, store)

        await send_message(
            "sess_1",
            MessageCreate(
                content="Check the design",
                stream=False,
                source_ids=[source.id],
            ),
            request,
            auth="test",
        )

        assert engine.ran == ["Check the design"]
        assert "Acceptance: chat keeps three columns." in engine.turn_contexts[0]

    @pytest.mark.asyncio
    async def test_existing_issue_is_linked_into_turn_context(self, tmp_path) -> None:
        engine = _FakeEngine()
        engine.workbench_store = SimpleNamespace(
            get_issue=lambda _session_id, _task_id: _async_value(
                SimpleNamespace(
                    task_id="task-1",
                    mission_id="mission-1",
                    risk_level="high",
                    acceptance_criteria=["tests pass"],
                )
            )
        )
        request = _fake_request(engine, ChatRunStore(tmp_path / "chat-runs.db"))

        response = await send_message(
            "sess_1",
            MessageCreate(
                content="Continue this issue",
                stream=False,
                linked_issue_id="task-1",
            ),
            request,
            auth="test",
        )

        assert "task_id: task-1" in engine.turn_contexts[0]
        assert response.metadata["linked_issue"] == {"task_id": "task-1"}

    @pytest.mark.asyncio
    async def test_list_messages_preserves_metadata_for_chat_history(self) -> None:
        engine = _FakeEngine()
        engine.session_store.messages = [
            {
                "role": "user",
                "content": "把登录失败记录成任务",
                "timestamp": "2026-07-02T08:00:00",
                "metadata": {"source": "chat"},
            },
            {
                "role": "assistant",
                "content": "已创建关联任务。",
                "timestamp": "2026-07-02T08:00:03",
                "metadata": {"workbench_issue": {"task_id": "task-chat-1"}},
            },
        ]
        request = _fake_request(engine)

        response = await list_messages("sess_1", request, page=1, page_size=50, auth="test")

        assert response.total == 2
        assert [message.id for message in response.messages] == ["msg-1", "msg-2"]
        assert response.messages[0].metadata == {"source": "chat"}
        assert response.messages[1].metadata == {
            "workbench_issue": {"task_id": "task-chat-1"}
        }

    @pytest.mark.asyncio
    async def test_list_messages_normalizes_empty_tool_call_content(self) -> None:
        engine = _FakeEngine()
        engine.session_store.messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-1"}],
            }
        ]
        request = _fake_request(engine)

        response = await list_messages("sess_1", request, page=1, page_size=50, auth="test")

        assert response.total == 1
        assert response.messages[0].role == "assistant"
        assert response.messages[0].content == ""
