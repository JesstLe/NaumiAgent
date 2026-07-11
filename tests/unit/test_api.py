"""API 路由单元测试."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import naumi_agent.api.routes.messages as message_routes
from naumi_agent import __version__
from naumi_agent.api.permission_broker import PermissionApprovalBroker
from naumi_agent.api.routes.messages import (
    _engine_event_to_stream_event,
    _stream_response,
    list_messages,
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


def _fake_request(engine: _FakeEngine):
    state = SimpleNamespace(engine=engine, engine_lock=asyncio.Lock())
    return SimpleNamespace(app=SimpleNamespace(state=state))


class TestMessageRoutes:
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

    def test_engine_event_to_stream_event_normalizes_token(self) -> None:
        event = _engine_event_to_stream_event(
            "token",
            {"content": "hi"},
            session_id="sess_1",
        )

        assert event.type == EventType.TOKEN_DELTA
        assert event.data == {"token": "hi"}

    def test_permission_bubble_becomes_sanitized_stream_request(self) -> None:
        event = _engine_event_to_stream_event(
            "permission_bubble",
            {
                "agent_name": "main",
                "tool_name": "bash_run",
                "call_id": "call-1",
                "status": "needs_confirmation",
                "reason": "命令执行需要确认。",
                "risk_level": "medium",
                "requires_confirmation": True,
                "arguments": {"command": "echo $API_KEY"},
            },
            session_id="sess_1",
        )

        assert event.type == EventType.PERMISSION_REQUEST
        assert event.data == {
            "agent_name": "main",
            "tool_name": "bash_run",
            "call_id": "call-1",
            "status": "needs_confirmation",
            "reason": "命令执行需要确认。",
            "risk_level": "medium",
            "requires_confirmation": True,
        }

    def test_thinking_delta_stream_omits_internal_content(self) -> None:
        event = _engine_event_to_stream_event(
            "thinking_delta",
            {"content": "内部推理不应离开引擎"},
            session_id="sess_1",
        )

        assert event.type == EventType.THINKING_DELTA
        assert event.data == {}
