from __future__ import annotations

import asyncio
import io
import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from naumi_agent.background.models import BackgroundStatus
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.model.router import StreamChunk
from naumi_agent.orchestrator.engine import AgentEngine, AgentResult, AgentRuntimeMode, AgentUsage
from naumi_agent.orchestrator.planner import Complexity, ExecutionMode, Plan, Step
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.tools.base import ToolCall, ToolResult
from naumi_agent.ui.bridge import JsonlEngineBridge, resolve_config_path
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    PermissionBubbleMessage,
    RuntimeStatusMessage,
    TodoStatusMessage,
    ToolPrepareMessage,
    ToolResultMessage,
    ToolUseMessage,
)
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    normalize_client_record,
)


class _FakeRouter:
    def resolve_model(self, tier: str) -> str:
        return f"fake-{tier}"


class _FakeEngine:
    def __init__(self) -> None:
        self.runtime_mode = AgentRuntimeMode.DEFAULT
        self.permission_mode = PermissionMode.MODERATE
        self.workspace_root = Path.cwd()
        self.usage = AgentUsage(total_input_tokens=12, total_output_tokens=3, turns=1)
        self.router = _FakeRouter()
        self.permission_confirmer = None
        self.shutdown_called = False
        self._session = None
        self._config = SimpleNamespace(ui=SimpleNamespace(show_reasoning=False))

    def set_permission_confirmer(self, confirmer: Any) -> None:
        self.permission_confirmer = confirmer

    def reset(self) -> None:
        self._session = None

    def set_runtime_mode(self, mode: str) -> AgentRuntimeMode:
        self.runtime_mode = AgentRuntimeMode(mode)
        if self.runtime_mode == AgentRuntimeMode.PLAN:
            self.permission_mode = PermissionMode.STRICT
        elif self.runtime_mode == AgentRuntimeMode.BYPASS:
            self.permission_mode = PermissionMode.BYPASS
        else:
            self.permission_mode = PermissionMode.MODERATE
        return self.runtime_mode

    def cycle_runtime_mode(self) -> AgentRuntimeMode:
        next_mode = {
            AgentRuntimeMode.DEFAULT: AgentRuntimeMode.PLAN,
            AgentRuntimeMode.PLAN: AgentRuntimeMode.BYPASS,
            AgentRuntimeMode.BYPASS: AgentRuntimeMode.DEFAULT,
        }[self.runtime_mode]
        return self.set_runtime_mode(next_mode.value)

    def get_context_info(self) -> dict[str, Any]:
        return {"used": 12, "window": 256000, "percentage": 0.1}

    def get_budget_info(self) -> dict[str, Any]:
        return {"used_usd": 0.01, "max_usd": 5.0, "percentage": 0.2}

    def get_recent_permission_bubbles(self, limit: int = 8) -> list[dict[str, Any]]:
        return [
            {
                "request_id": "hist-1",
                "agent_name": "coder",
                "tool_name": "file_write",
                "status": "confirmed",
                "reason": "用户已允许。",
            }
        ][-limit:]

    async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
        await on_event("turn_start", {"turn": 1, "model": "fake-capable"})
        await on_event("response_start", {})
        await on_event("token", {"content": f"收到: {task}"})
        await on_event("response_end", {})
        await on_event(
            "tool_start",
            {
                "name": "file_read",
                "call_id": "call-1",
                "args": '{"file_path": "README.md"}',
            },
        )
        await on_event(
            "tool_end",
            {
                "name": "file_read",
                "call_id": "call-1",
                "status": "success",
                "duration_ms": 7,
                "content": "ok",
            },
        )
        return AgentResult(status="completed", response="完成", usage=self.usage)

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def list_sessions(self, page: int = 1, page_size: int = 20) -> tuple[list[Any], int]:
        session = SimpleNamespace(
            id="session-1",
            title="历史会话",
            messages=[
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
            ],
        )
        return [session], 1

    async def load_session(self, session_id: str) -> bool:
        if session_id != "session-1":
            return False
        self._session = SimpleNamespace(
            id="session-1",
            title="历史会话",
            messages=[
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
            ],
        )
        return True


class _SlowFakeEngine(_FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.release_run = asyncio.Event()

    async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
        await on_event("response_start", {})
        await on_event("token", {"content": f"处理中: {task}"})
        await self.release_run.wait()
        await on_event("response_end", {})
        return AgentResult(status="completed", response="完成", usage=self.usage)


class _FakeBackgroundRunner:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self._tasks = {
            "bg_1": SimpleNamespace(
                id="bg_1",
                status=BackgroundStatus.RUNNING,
                is_finished=False,
            )
        }

    def get(self, task_id: str) -> Any:
        return self._tasks.get(task_id)

    async def cancel(self, task_id: str) -> Any:
        self.cancelled.append(task_id)
        task = self._tasks[task_id]
        task.status = BackgroundStatus.CANCELLED
        task.is_finished = True
        return task

    def list_tasks(self) -> list[Any]:
        return list(self._tasks.values())


class _FakeBrowserTaskRunner:
    def __init__(self) -> None:
        self.aborted: list[tuple[str, str]] = []

    def abort_run(self, run_id: str, reason: str = "") -> dict[str, Any]:
        if run_id != "run_1":
            raise ValueError(f"Run not found: {run_id}")
        self.aborted.append((run_id, reason))
        return {"id": run_id, "status": "aborting"}

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{"id": "run_1", "status": "aborting"}][:limit]


def _records(writer: io.StringIO) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in writer.getvalue().splitlines()
        if line.strip()
    ]


def test_bridge_resolve_config_path_uses_existing_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("log_level: DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_config_path("config.yaml") == "config.yaml"


def test_bridge_resolve_config_path_falls_back_to_repo_config() -> None:
    resolved = Path(resolve_config_path("__missing_naumi_config__.yaml"))

    assert resolved.name == "config.yaml"
    assert resolved.exists()
    assert resolved.parent.name == "NaumiAgent"


def test_protocol_decodes_strict_jsonl() -> None:
    record = make_envelope(ServerEventType.READY, {"ok": True})
    line = encode_jsonl(record)
    decoded = decode_jsonl_line(line)
    assert decoded["type"] == "ready"
    assert decoded["payload"] == {"ok": True}

    with pytest.raises(ValueError, match="缺少 type"):
        decode_jsonl_line('{"payload":{}}\n')


def test_protocol_contract_matches_python_enums() -> None:
    contract_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "terminal-ui"
        / "protocol-contract.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert contract["version"] == 1
    assert contract["transport"] == "jsonl"
    assert contract["client_events"] == [str(event) for event in ClientEventType]
    assert contract["server_events"] == [str(event) for event in ServerEventType]


def test_protocol_contract_ui_message_fields_match_python_messages() -> None:
    contract_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "terminal-ui"
        / "protocol-contract.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    message_classes = {
        "assistant_stream": AssistantStreamMessage,
        "tool_prepare": ToolPrepareMessage,
        "tool_use": ToolUseMessage,
        "tool_result": ToolResultMessage,
        "todo_status": TodoStatusMessage,
        "permission_bubble": PermissionBubbleMessage,
        "runtime_status": RuntimeStatusMessage,
    }

    for message_type, cls in message_classes.items():
        assert message_type in contract["ui_messages"]
        python_fields = {field.name for field in fields(cls)}
        contract_fields = set(contract["ui_messages"][message_type]["fields"])
        assert contract_fields <= python_fields

    assert contract["ui_messages"]["tool_prepare"]["phases"] == [
        "start",
        "snapshot",
        "end",
    ]
    assert "prepare_end" in contract["ui_messages"]["tool_prepare"]["notes"]


def test_protocol_normalizes_known_client_event_payloads() -> None:
    task_record = normalize_client_record({
        "id": 42,
        "type": ClientEventType.TASK_PANEL,
        "version": "1",
        "payload": {
            "limit": 999,
            "source": "background",
            "status": "needs-input",
            "detail": "bg_0001",
            "pinned": "true",
            "refresh": 1,
        },
    })

    assert task_record["id"] == "42"
    assert task_record["version"] == 1
    assert task_record["payload"] == {
        "limit": 50,
        "source": "background",
        "status": "needs_input",
        "pinned": True,
        "refresh": True,
        "detail_id": "bg_0001",
    }

    permission_record = normalize_client_record({
        "type": ClientEventType.PERMISSION_RESPONSE,
        "payload": {"request_id": 123, "choice": "ALLOW"},
    })

    assert permission_record["payload"] == {
        "request_id": "123",
        "choice": "allow",
    }

    with pytest.raises(ValueError, match="协议 version 不兼容"):
        normalize_client_record({
            "type": ClientEventType.PING,
            "version": 999,
            "payload": {},
        })

    with pytest.raises(ValueError, match="权限选择无效"):
        normalize_client_record({
            "type": ClientEventType.PERMISSION_RESPONSE,
            "payload": {"request_id": "perm-1", "choice": "maybe"},
        })


@pytest.mark.asyncio
async def test_bridge_status_payload_includes_session_id() -> None:
    engine = _FakeEngine()
    engine._session = SimpleNamespace(id="session-abc")
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")

    assert bridge.status_payload()["session_id"] == "session-abc"


@pytest.mark.asyncio
async def test_bridge_status_payload_includes_compact_task_activity() -> None:
    engine = _FakeEngine()
    engine.background_runner = SimpleNamespace(
        list_tasks=lambda: [
            SimpleNamespace(status=BackgroundStatus.RUNNING),
            SimpleNamespace(status=BackgroundStatus.FAILED),
        ]
    )
    engine.subagent_manager = SimpleNamespace(
        list_agents=lambda: [
            {"state": "running"},
            {"state": "idle"},
        ]
    )
    engine.task_runner = SimpleNamespace(
        list_runs=lambda limit=20: [
            {"status": "running"},
            {"status": "completed"},
        ]
    )
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge._pending_permissions["perm-1"] = asyncio.get_running_loop().create_future()

    tasks = bridge.status_payload()["tasks"]

    assert tasks == {
        "background_running": 1,
        "background_attention": 1,
        "subagents_active": 1,
        "browser_active": 1,
        "permissions_pending": 1,
    }


@pytest.mark.asyncio
async def test_bridge_set_reasoning_updates_status_payload() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "reasoning-1",
            "type": ClientEventType.SET_REASONING,
            "payload": {"enabled": True},
        }
    )

    records = _records(writer)
    status = next(record["payload"] for record in records if record["type"] == "runtime/status")
    assert status["ui"]["show_reasoning"] is True


@pytest.mark.asyncio
async def test_bridge_slash_help_command_renders_system_notice() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "slash-help-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/help"},
    })

    records = _records(writer)
    message = next(
        record["payload"]
        for record in records
        if (
            record["type"] == "ui/message"
            and record["payload"].get("type") == "system_notice"
            and record["payload"].get("title") == "help"
        )
    )
    assert "/help" in message["content"]
    assert "/version" in message["content"]


@pytest.mark.asyncio
async def test_bridge_unknown_slash_command_emits_error() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "slash-unknown-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/not-found-cmd"},
    })

    records = _records(writer)
    error_records = [record for record in records if record["type"] == "error"]
    assert error_records, "预期收到错误事件"
    assert error_records[-1]["payload"]["code"] == "unknown_command"


@pytest.mark.asyncio
async def test_bridge_slash_reasoning_command_toggles_visibility() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "reasoning-on-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/reasoning on"},
    })

    assert _records(writer)[-1]["payload"]["ui"]["show_reasoning"] is True

    await bridge.handle_client_record({
        "id": "reasoning-off-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/reasoning off"},
    })

    assert _records(writer)[-1]["payload"]["ui"]["show_reasoning"] is False



@pytest.mark.asyncio
async def test_bridge_renders_permission_panel_as_system_notice() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    bridge._pending_permission_payloads["perm-1"] = {
        "tool_name": "bash_run",
        "reason": "需要启动本地服务。",
        "status": "needs_confirmation",
    }

    await bridge.handle_client_record(
        {"id": "perm-panel-1", "type": ClientEventType.PERMISSIONS_PANEL, "payload": {"limit": 5}}
    )

    records = _records(writer)
    message = next(
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    )
    assert message["title"] == "permissions"
    assert "权限面板" in message["content"]
    assert "perm-1 main -> bash_run [needs_confirmation]" in message["content"]
    assert "来源:TOOL_PERMISSIONS:bash_run" in message["content"]
    assert "确认:需要确认" in message["content"]
    assert "hist-1 coder -> file_write [confirmed]" in message["content"]


@pytest.mark.asyncio
async def test_bridge_streams_engine_events_as_ui_messages() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("你好", request_id="submit-1")
    assert bridge._run_task is not None
    await bridge._run_task

    records = _records(writer)
    event_types = [record["type"] for record in records]
    assert "user/message" in event_types
    assert "run/started" in event_types
    assert "run/completed" in event_types
    assert event_types.count("ui/message") >= 4
    assert any(
        record["type"] == "ui/message"
        and record["payload"].get("type") == "tool_use"
        and record["payload"].get("tool_name") == "file_read"
        and record["payload"].get("tool_call_id") == "call-1"
        for record in records
    )
    assert any(record["type"] == "runtime/status" for record in records)


@pytest.mark.asyncio
async def test_bridge_streams_real_engine_tool_lifecycle_without_external_api(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    final_args = json.dumps(
        {
            "file_path": "demo.txt",
            "content": "\n".join(f"line {index}" for index in range(900)),
        }
    )
    call_count = 0

    async def stream_response(**_: Any):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamChunk(
                tool_call_started=True,
                tool_call_snapshot={
                    0: {
                        "id": "call-real-1",
                        "type": "function",
                        "function": {
                            "name": "file_write",
                            "arguments": '{"file_path": "demo.txt", "content": "line 0',
                        },
                    }
                },
            )
            yield StreamChunk(
                tool_call_snapshot={
                    0: {
                        "id": "call-real-1",
                        "type": "function",
                        "function": {
                            "name": "file_write",
                            "arguments": final_args,
                        },
                    }
                }
            )
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "call-real-1",
                        "type": "function",
                        "function": {
                            "name": "file_write",
                            "arguments": final_args,
                        },
                    }
                },
                finish_reason="tool_calls",
            )
            return

        yield StreamChunk(token="文件已写入。")
        yield StreamChunk(finish_reason="stop")

    async def execute_tool(tc: ToolCall, **_: Any) -> ToolResult:
        return ToolResult(
            call_id=tc.id,
            status="success",
            content="写入成功",
            duration_ms=3,
        )

    engine._planner.plan = AsyncMock(
        return_value=Plan(
            understanding="写入演示文件",
            approach="直接执行",
            steps=[
                Step(
                    id="step-1",
                    description="写入文件",
                    tool="file_write",
                    depends_on=[],
                    parallelizable=False,
                    complexity=Complexity.SIMPLE,
                )
            ],
            mode=ExecutionMode.SINGLE_TURN,
        )
    )
    engine._router.stream = stream_response  # type: ignore[method-assign]
    engine._execute_tool = execute_tool  # type: ignore[method-assign]

    try:
        await bridge.submit("写入 demo 文件", request_id="submit-real-engine")
        assert bridge._run_task is not None
        await bridge._run_task
    finally:
        await engine.shutdown()

    records = _records(writer)
    ui_messages = [
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
    ]
    prepare_messages = [
        message for message in ui_messages
        if message.get("type") == "tool_prepare"
    ]
    assert [message["phase"] for message in prepare_messages] == [
        "start",
        "snapshot",
        "end",
    ]
    assert {message["tool_call_id"] for message in prepare_messages} == {"call-real-1"}
    assert prepare_messages[-1]["content_lines"] == 900
    assert any(
        message.get("type") == "tool_use"
        and message.get("tool_name") == "file_write"
        and message.get("tool_call_id") == "call-real-1"
        and message.get("file_path") == "demo.txt"
        for message in ui_messages
    )
    assert any(
        message.get("type") == "tool_result"
        and message.get("status") == "success"
        and message.get("tool_call_id") == "call-real-1"
        for message in ui_messages
    )
    assert any(record["type"] == "run/completed" for record in records)
    assert any(
        message.get("type") == "assistant_stream"
        and message.get("phase") == "token"
        and "文件已写入" in message.get("content", "")
        for message in ui_messages
    )


@pytest.mark.asyncio
async def test_bridge_mode_and_permission_round_trip() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "mode-plan",
            "type": ClientEventType.SET_MODE,
            "payload": {"mode": "plan"},
        }
    )
    assert engine.runtime_mode == AgentRuntimeMode.PLAN
    assert engine.permission_mode == PermissionMode.STRICT
    records = _records(writer)
    assert records[-1]["type"] == "runtime/status"
    assert records[-1]["payload"]["mode"] == "plan"
    assert records[-1]["payload"]["permission_mode"] == "strict"

    await bridge.handle_client_record(
        {
            "id": "mode-1",
            "type": ClientEventType.SET_MODE,
            "payload": {"mode": "bypass"},
        }
    )
    assert engine.runtime_mode == AgentRuntimeMode.BYPASS
    assert engine.permission_mode == PermissionMode.BYPASS

    permission_task = asyncio.create_task(
        bridge.confirm_permission(
            {
                "call_id": "call-1",
                "tool_name": "bash_run",
                "arguments": {"command": "rm -rf tmp"},
            }
        )
    )
    await asyncio.sleep(0)
    assert any(record["type"] == "permission/request" for record in _records(writer))

    await bridge.handle_client_record(
        {
            "id": "perm-1",
            "type": ClientEventType.PERMISSION_RESPONSE,
            "payload": {"request_id": "call-1", "choice": "allow"},
        }
    )
    assert await permission_task == "allow"
    assert any(record["type"] == "permission/resolved" for record in _records(writer))


@pytest.mark.asyncio
async def test_bridge_rejects_invalid_client_record_before_dispatch() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "bad-1",
        "type": ClientEventType.PERMISSION_RESPONSE,
        "version": 1,
        "payload": {"request_id": "call-1", "choice": "maybe"},
    })

    records = _records(writer)
    assert records[-1]["type"] == "error"
    assert records[-1]["request_id"] == "bad-1"
    assert records[-1]["payload"]["code"] == "bad_request"
    assert "权限选择无效" in records[-1]["payload"]["message"]


@pytest.mark.asyncio
async def test_bridge_resume_replays_session_messages() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "resume-1", "type": ClientEventType.RESUME, "payload": {}}
    )

    records = _records(writer)
    assert any(record["type"] == "session/replayed" for record in records)
    replayed = [
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
    ]
    assert any(
        message.get("type") == "user" and message.get("content") == "旧问题"
        for message in replayed
    )
    assert any(
        message.get("type") == "assistant_stream" and message.get("content") == "旧回答"
        for message in replayed
    )


@pytest.mark.asyncio
async def test_bridge_rejects_resume_while_run_is_active() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "submit-1", "type": ClientEventType.SUBMIT, "payload": {"text": "长任务"}}
    )
    assert bridge._run_task is not None
    await asyncio.sleep(0)

    await bridge.handle_client_record(
        {"id": "resume-1", "type": ClientEventType.RESUME, "payload": {}}
    )

    records = _records(writer)
    assert any(
        record["type"] == "error"
        and record["payload"].get("code") == "run_in_progress"
        and "恢复会话" in record["payload"].get("message", "")
        for record in records
    )
    assert not any(record["type"] == "session/replayed" for record in records)

    engine.release_run.set()
    await bridge._run_task


@pytest.mark.asyncio
async def test_bridge_renders_task_panel_as_system_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    async def fake_render_task_panel(
        target_engine: Any,
        *,
        limit: int = 12,
        source: str = "all",
        status: str = "all",
        detail_id: str = "",
    ) -> str:
        assert target_engine is engine
        assert limit == 5
        assert source == "background"
        assert status == "running"
        assert detail_id == "bg_1"
        return (
            "任务面板\n"
            "filter: source=background status=running detail=bg_1\n"
            "Detail\n"
            "  类型: Background\n"
            "Background\n"
            "  - bg_1 正在验证\n"
        )

    monkeypatch.setattr(
        "naumi_agent.ui.task_panel.render_task_panel",
        fake_render_task_panel,
    )

    await bridge.handle_client_record(
        {
            "id": "tasks-1",
            "type": ClientEventType.TASK_PANEL,
            "payload": {
                "limit": 5,
                "source": "background",
                "status": "running",
                "detail_id": "bg_1",
            },
        }
    )

    records = _records(writer)
    message = next(
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    )
    assert message["title"] == "tasks"
    assert "filter: source=background status=running detail=bg_1" in message["content"]
    assert "类型: Background" in message["content"]
    assert "正在验证" in message["content"]
    assert any(record["type"] == "runtime/status" for record in records)


@pytest.mark.asyncio
async def test_bridge_cancels_background_task_through_runner() -> None:
    engine = _FakeEngine()
    engine.background_runner = _FakeBackgroundRunner()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "cancel-bg-1",
            "type": ClientEventType.TASK_CANCEL,
            "payload": {
                "task_id": "bg_1",
                "source": "background",
                "reason": "用户从任务面板取消。",
            },
        }
    )

    records = _records(writer)
    assert engine.background_runner.cancelled == ["bg_1"]
    message = next(
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    )
    assert message["title"] == "tasks"
    assert "已请求取消后台任务 bg_1" in message["content"]
    assert any(record["type"] == "runtime/status" for record in records)


@pytest.mark.asyncio
async def test_bridge_aborts_browser_task_through_runner() -> None:
    engine = _FakeEngine()
    engine.task_runner = _FakeBrowserTaskRunner()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "cancel-browser-1",
            "type": ClientEventType.TASK_CANCEL,
            "payload": {
                "task_id": "run_1",
                "source": "browser",
                "reason": "用户从任务面板取消。",
            },
        }
    )

    records = _records(writer)
    assert engine.task_runner.aborted == [("run_1", "用户从任务面板取消。")]
    message = next(
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    )
    assert message["title"] == "tasks"
    assert "已请求取消浏览器任务 run_1" in message["content"]


@pytest.mark.asyncio
async def test_bridge_rejects_unsupported_task_cancel_without_mutating() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "cancel-todo-1",
            "type": ClientEventType.TASK_CANCEL,
            "payload": {"task_id": "todo_1", "source": "todo"},
        }
    )

    records = _records(writer)
    message = next(
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    )
    assert message["title"] == "tasks"
    assert message["level"] == "warning"
    assert "支持来源: background / browser" in message["content"]


@pytest.mark.asyncio
async def test_bridge_renders_doctor_report_as_system_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    class FakeReport:
        status = "warn"

    async def fake_run_doctor(config: Any, *, workspace_root: Path, mcp_manager: Any) -> Any:
        assert workspace_root == engine.workspace_root
        assert mcp_manager is None
        return FakeReport()

    def fake_render_doctor_report(report: Any) -> str:
        assert isinstance(report, FakeReport)
        return "## 环境诊断存在提醒\n\n- **WARN browser daemon**：未启动"

    monkeypatch.setattr("naumi_agent.ui.doctor.run_doctor", fake_run_doctor)
    monkeypatch.setattr(
        "naumi_agent.ui.doctor.render_doctor_report",
        fake_render_doctor_report,
    )

    await bridge.handle_client_record(
        {"id": "doctor-1", "type": ClientEventType.DOCTOR, "payload": {}}
    )

    records = _records(writer)
    message = next(
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    )
    assert message["title"] == "doctor"
    assert message["level"] == "warn"
    assert "browser daemon" in message["content"]
    assert any(record["type"] == "runtime/status" for record in records)
