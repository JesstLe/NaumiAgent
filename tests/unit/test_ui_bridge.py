from __future__ import annotations

import asyncio
import inspect
import io
import json
import subprocess
import sys
import types
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
from naumi_agent.safety.permission_grants import PermissionGrant
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.base import ToolCall, ToolResult
from naumi_agent.ui import bridge as ui_bridge
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
from naumi_agent.ui.permission_confirmation import PermissionChallengeStore
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    normalize_client_record,
)
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore


class _ReconfigurableStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_bridge_stdio_is_configured_as_utf8() -> None:
    stdin = _ReconfigurableStream()
    stdout = _ReconfigurableStream()
    stderr = _ReconfigurableStream()

    ui_bridge._configure_stdio_utf8(streams=(stdin, stdout, stderr))  # type: ignore[arg-type]

    assert stdin.calls == [{"encoding": "utf-8", "errors": "strict"}]
    assert stdout.calls == [{"encoding": "utf-8", "errors": "strict"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (RuntimeError("401 Unauthorized"), "model_auth_failed"),
        (RuntimeError("429 rate limit exceeded"), "model_rate_limited"),
        (TimeoutError("request timed out"), "model_timeout"),
        (RuntimeError("private provider payload"), "run_failed"),
    ],
)
def test_run_error_presentation_covers_provider_failure_classes(
    error: Exception,
    expected_code: str,
) -> None:
    message, code = ui_bridge._present_run_error(error)

    assert code == expected_code
    assert "private provider payload" not in message


@pytest.mark.asyncio
async def test_stdin_reader_does_not_use_asyncio_default_executor() -> None:
    loop = asyncio.get_running_loop()
    lines = ui_bridge._start_stdin_line_reader(io.StringIO("hello\n"), loop)

    assert await asyncio.wait_for(lines.get(), timeout=1) == "hello\n"
    assert await asyncio.wait_for(lines.get(), timeout=1) == ""


def test_git_snapshot_does_not_inherit_bridge_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_check_output(*_args: Any, **kwargs: Any) -> bytes:
        calls.append(kwargs)
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(ui_bridge.subprocess, "check_output", fake_check_output)

    assert ui_bridge._git_snapshot(tmp_path) == {"branch": "", "dirty": False}
    assert calls == [
        {
            "cwd": str(tmp_path),
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 2,
        }
    ]


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
        self.permission_grants: list[PermissionGrant] = []

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

    def list_permission_grants(self) -> tuple[PermissionGrant, ...]:
        return tuple(self.permission_grants)

    def revoke_permission_grant(self, grant_id: str) -> bool:
        for grant in self.permission_grants:
            if grant.grant_id == grant_id:
                self.permission_grants.remove(grant)
                return True
        return False

    def revoke_all_permission_grants(self) -> int:
        count = len(self.permission_grants)
        self.permission_grants.clear()
        return count

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


class _FailingFakeEngine(_FakeEngine):
    async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
        raise RuntimeError(
            'litellm.NotFoundError: AnthropicException - '
            '{"error":{"message":"The requested resource was not found"}}'
        )


class _FakeTaskStore:
    def __init__(self) -> None:
        self.session_id = ""
        self.updates: list[tuple[str, TaskStatus]] = []

    def set_session(self, session_id: str) -> None:
        self.session_id = session_id

    def scoped(self, session_id: str) -> _FakeTaskStore:
        self.session_id = session_id
        return self

    async def update_task(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        **_: Any,
    ) -> Any:
        if status is not None:
            self.updates.append((task_id, status))
        return SimpleNamespace(id=task_id, status=status)


class _FakeWorkbenchService:
    def __init__(self, missions: list[dict[str, Any]] | None = None) -> None:
        self.missions = list(missions or [])
        self.created_missions: list[dict[str, str]] = []
        self.created_issues: list[dict[str, Any]] = []

    async def list_missions(self, session_id: str, **_: Any) -> dict[str, Any]:
        return {"missions": self.missions, "session_id": session_id}

    async def create_mission(self, *, session_id: str, title: str, goal: str) -> Any:
        mission = {"id": "mission-auto", "session_id": session_id, "title": title, "goal": goal}
        self.missions.append(mission)
        self.created_missions.append(mission)
        return SimpleNamespace(**mission)

    async def create_issue(self, **kwargs: Any) -> dict[str, Any]:
        self.created_issues.append(kwargs)
        return {
            "task_id": "1",
            "mission_id": kwargs["mission_id"],
            "risk_level": str(kwargs["risk_level"]),
            "parallel_mode": str(kwargs["parallel_mode"]),
            "acceptance_criteria": kwargs["acceptance_criteria"],
            "task": {"id": "1", "subject": kwargs["title"], "status": "pending"},
        }

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        return {"version": 1, "session_id": session_id, "issues": [{"task_id": "1"}]}


class _TaskSubmitFakeEngine(_FakeEngine):
    def __init__(self, missions: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.task_store = _FakeTaskStore()
        self.workbench_service = _FakeWorkbenchService(missions)
        self.turn_contexts: list[str] = []

    async def get_or_create_session(self, title: str | None = None) -> Any:
        self._session = SimpleNamespace(id="session-task", title=title or "任务会话")
        return self._session

    async def run_streaming(
        self,
        task: str,
        on_event: Any,
        turn_context: str = "",
    ) -> AgentResult:
        self.turn_contexts.append(turn_context)
        await on_event("response_start", {})
        await on_event("token", {"content": f"执行: {task}"})
        await on_event("response_end", {})
        return AgentResult(status="completed", response="完成", usage=self.usage)


class _FailingTaskSubmitEngine(_TaskSubmitFakeEngine):
    async def run_streaming(
        self,
        task: str,
        on_event: Any,
        turn_context: str = "",
    ) -> AgentResult:
        self.turn_contexts.append(turn_context)
        raise RuntimeError("private task failure")


class _SlowTaskSubmitEngine(_TaskSubmitFakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run_streaming(
        self,
        task: str,
        on_event: Any,
        turn_context: str = "",
    ) -> AgentResult:
        self.turn_contexts.append(turn_context)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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

    assert resolved.name in {"config.yaml", "config.yaml.example"}
    assert resolved.exists()
    assert resolved.parent == Path(__file__).resolve().parents[2]


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
    assert contract["client_events"] == [
        str(event)
        for event in ClientEventType
        if event != ClientEventType.PERMISSION_REVOKE
    ]
    assert contract["server_events"] == [
        str(event)
        for event in ServerEventType
        if event
        not in {
            ServerEventType.PERMISSION_CONFIRMATION_REQUIRED,
            ServerEventType.PERMISSION_GRANTS_CHANGED,
        }
    ]


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
    run_cancel = normalize_client_record({
        "id": "cancel-1",
        "type": "run_cancel",
        "payload": {"reason": " 用户请求停止 ", "ignored": "value"},
    })
    assert run_cancel["payload"] == {"reason": "用户请求停止"}

    task_submit = normalize_client_record({
        "id": "task-submit-1",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {
            "text": "实现登录流程",
            "mission_id": " mission-1 ",
            "title": " 登录任务 ",
            "acceptance_criteria": ["测试通过", "", 42],
            "blocked_by": ["1", "", 2],
            "parallel_mode": "COOPERATIVE",
            "risk_level": "HIGH",
        },
    })
    assert task_submit["payload"] == {
        "text": "实现登录流程",
        "mission_id": "mission-1",
        "title": "登录任务",
        "acceptance_criteria": ["测试通过", "42"],
        "blocked_by": ["1", "2"],
        "parallel_mode": "cooperative",
        "risk_level": "high",
    }

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
            "history": False,
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

    with pytest.raises(ValueError, match="任务内容不能为空"):
        normalize_client_record({
            "type": ClientEventType.TASK_SUBMIT,
            "payload": {"text": "  "},
        })

    with pytest.raises(ValueError, match="并行模式无效"):
        normalize_client_record({
            "type": ClientEventType.TASK_SUBMIT,
            "payload": {"text": "任务", "parallel_mode": "invalid"},
        })

    with pytest.raises(ValueError, match="取消原因不能超过 500 个字符"):
        normalize_client_record({
            "type": "run_cancel",
            "payload": {"reason": "x" * 501},
        })


@pytest.mark.asyncio
async def test_bridge_status_payload_includes_session_id() -> None:
    engine = _FakeEngine()
    engine._session = SimpleNamespace(id="session-abc")
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")

    assert bridge.status_payload()["session_id"] == "session-abc"


def test_bridge_status_payload_includes_slash_command_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ui_bridge,
        "_load_cli_slash_commands",
        lambda: [{"command": "/help", "description": "显示帮助"}],
    )
    engine = _FakeEngine()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    slash_commands = bridge.status_payload().get("slash_commands")

    assert isinstance(slash_commands, list)
    assert slash_commands == [{"command": "/help", "aliases": ["/h"], "description": "显示帮助"}]


def test_bridge_status_payload_exposes_runtime_slash_commands() -> None:
    engine = _FakeEngine()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")

    slash_commands = bridge.status_payload().get("slash_commands")
    command_names = {item["command"] for item in slash_commands}

    assert "/browse" in command_names
    assert "/tasks" in command_names
    assert "/scan-full" in command_names
    assert "/btemplate-list" in command_names


def test_bridge_status_payload_can_omit_static_slash_commands() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")

    payload = bridge.status_payload(include_slash_commands=False)

    assert "slash_commands" not in payload


@pytest.mark.asyncio
async def test_bridge_status_payload_includes_compact_task_activity() -> None:
    engine = _FakeEngine()
    engine.background_runner = SimpleNamespace(
        list_tasks=lambda: [
            SimpleNamespace(status=BackgroundStatus.RUNNING),
            SimpleNamespace(status=BackgroundStatus.FAILED, notified=False),
            SimpleNamespace(status=BackgroundStatus.FAILED, notified=True),
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
    bridge.bind_writer(io.StringIO())
    pending_task = asyncio.create_task(
        bridge.confirm_permission(
            {
                "call_id": "perm-1",
                "tool_name": "bash_run",
                "choices": ["allow_once", "deny"],
            }
        )
    )
    await asyncio.sleep(0)

    tasks = bridge.status_payload()["tasks"]

    assert tasks == {
        "background_running": 1,
        "background_attention": 1,
        "subagents_active": 1,
        "browser_active": 1,
        "permissions_pending": 1,
    }
    await bridge.resolve_permission(
        {"request_id": "perm-1", "choice": "deny"}, request_id="response-1"
    )
    assert await pending_task == "deny"


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
async def test_bridge_quit_slash_command_shuts_down_bridge() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "slash-quit-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/q"},
    })

    records = _records(writer)
    assert engine.shutdown_called
    assert records[-1]["type"] == "shutdown"
    assert records[-1]["payload"] == {"ok": True}
    assert not [
        record
        for record in records
        if (
            record["type"] == "ui/message"
            and record["payload"].get("type") == "system_notice"
        )
    ]


@pytest.mark.asyncio
async def test_bridge_cli_backed_slash_commands_are_dispatched_via_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ui_bridge,
        "_load_cli_slash_commands_with_alias",
        lambda: [
            "/help",
            "/pursue",
            "/diff",
            "/chaos",
            "/c",
            "/h",
            "/r",
            "/l",
            "/task",
            "/m",
            "/u",
            "/v",
        ],
    )
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    async def fake_handle_command(_: Any, cmd: str) -> None:
        captured.append(cmd)

    captured: list[str] = []

    async def fake_capture_async(func: Any) -> str:
        result = func()
        if inspect.isawaitable(result):
            await result
        return f"handled {captured[-1] if captured else ''}"

    fake_main_module = types.ModuleType("naumi_agent.main")
    fake_main_module._handle_command = fake_handle_command
    fake_main_module._capture_async = fake_capture_async
    fake_main_module.__dict__["__file__"] = __file__
    monkeypatch.setitem(sys.modules, "naumi_agent.main", fake_main_module)

    await bridge.handle_client_record({
        "id": "pursue-forward-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/pursue 生成 HTML"},
    })

    records = _records(writer)
    notice_records = [
        record
        for record in records
        if (
            record["type"] == "ui/message"
            and record["payload"].get("type") == "system_notice"
            and record["payload"].get("title") == "command"
        )
    ]
    assert notice_records, "预期 /pursue 走 CLI 并回显"
    assert "handled /pursue 生成 HTML" in notice_records[-1]["payload"]["content"]


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
    pending_task = asyncio.create_task(
        bridge.confirm_permission(
            {
                "call_id": "perm-1",
                "tool_name": "bash_run",
                "reason": "需要启动本地服务。",
                "choices": ["allow_once", "deny"],
            }
        )
    )
    await asyncio.sleep(0)

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
    await bridge.resolve_permission(
        {"request_id": "perm-1", "choice": "deny"}, request_id="response-1"
    )
    assert await pending_task == "deny"


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
    for record in records:
        if record["type"] in {"user/message", "run/started", "run/completed"}:
            assert record["request_id"] == "submit-1"
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
async def test_bridge_task_submit_creates_issue_and_executes_with_task_context() -> None:
    engine = _TaskSubmitFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-1",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {
            "text": "实现登录流程并补测试",
            "acceptance_criteria": ["定向测试通过"],
            "parallel_mode": "cooperative",
            "risk_level": "high",
        },
    })
    assert bridge._run_task is not None
    await bridge._run_task

    assert engine.task_store.session_id == "session-task"
    assert len(engine.workbench_service.created_missions) == 1
    assert engine.workbench_service.created_issues[0]["mission_id"] == "mission-auto"
    assert engine.task_store.updates == [
        ("1", TaskStatus.IN_PROGRESS),
        ("1", TaskStatus.COMPLETED),
    ]
    assert "task_id: 1" in engine.turn_contexts[0]
    records = _records(writer)
    task_created = next(record for record in records if record["type"] == "task/created")
    assert task_created["request_id"] == "task-submit-1"
    assert task_created["payload"]["task"]["id"] == "1"
    assert task_created["payload"]["task"]["status"] == "in_progress"
    assert task_created["payload"]["mission"]["id"] == "mission-auto"
    assert next(
        record for record in records if record["type"] == "run/started"
    )["payload"] == {
        "task": "实现登录流程并补测试",
        "task_id": "1",
        "mission_id": "mission-auto",
        "intent": "task",
    }
    completed = next(record for record in records if record["type"] == "run/completed")
    assert completed["request_id"] == "task-submit-1"
    assert completed["payload"]["task_id"] == "1"
    assert completed["payload"]["status"] == "completed"
    assert len([
        record for record in records if record["type"] == "workbench/snapshot"
    ]) == 2


@pytest.mark.asyncio
async def test_bridge_task_submit_rejects_ambiguous_missions_without_issue() -> None:
    engine = _TaskSubmitFakeEngine([
        {"id": "mission-1", "title": "前端", "status": "planning"},
        {"id": "mission-2", "title": "后端", "status": "active"},
    ])
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-ambiguous",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "实现登录流程"},
    })

    assert bridge._run_task is None
    assert engine.workbench_service.created_issues == []
    error = next(record for record in _records(writer) if record["type"] == "error")
    assert error["request_id"] == "task-submit-ambiguous"
    assert error["payload"]["code"] == "mission_required"
    assert "mission-1" in error["payload"]["message"]
    assert "mission-2" in error["payload"]["message"]


@pytest.mark.asyncio
async def test_bridge_task_submit_ignores_terminal_missions_when_auto_resolving() -> None:
    engine = _TaskSubmitFakeEngine([
        {"id": "mission-closed", "title": "旧任务", "status": "completed"},
        {"id": "mission-open", "title": "当前任务", "status": "active"},
    ])
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-open-mission",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "继续当前目标"},
    })
    assert bridge._run_task is not None
    await bridge._run_task

    assert engine.workbench_service.created_issues[0]["mission_id"] == "mission-open"


@pytest.mark.asyncio
async def test_bridge_task_submit_rejects_explicit_terminal_mission() -> None:
    engine = _TaskSubmitFakeEngine([
        {"id": "mission-closed", "title": "旧任务", "status": "cancelled"},
    ])
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-closed-mission",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {
            "text": "错误挂载",
            "mission_id": "mission-closed",
        },
    })

    assert bridge._run_task is None
    assert engine.workbench_service.created_issues == []
    error = next(record for record in _records(writer) if record["type"] == "error")
    assert error["payload"]["code"] == "mission_closed"
    assert "已结束" in error["payload"]["message"]


@pytest.mark.asyncio
async def test_bridge_task_submit_uses_explicit_owned_mission() -> None:
    engine = _TaskSubmitFakeEngine([
        {"id": "mission-1", "title": "前端", "status": "planning"},
        {"id": "mission-2", "title": "后端", "status": "active"},
    ])
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-explicit",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "实现 API", "mission_id": "mission-2"},
    })
    assert bridge._run_task is not None
    await bridge._run_task

    assert engine.workbench_service.created_missions == []
    assert engine.workbench_service.created_issues[0]["mission_id"] == "mission-2"


@pytest.mark.asyncio
async def test_bridge_task_submit_failure_blocks_backing_task() -> None:
    engine = _FailingTaskSubmitEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-failed",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "失败任务"},
    })
    assert bridge._run_task is not None
    await bridge._run_task

    assert engine.task_store.updates == [
        ("1", TaskStatus.IN_PROGRESS),
        ("1", TaskStatus.BLOCKED),
    ]
    error = next(record for record in _records(writer) if record["type"] == "error")
    assert error["request_id"] == "task-submit-failed"
    assert error["payload"]["code"] == "run_failed"
    assert error["payload"]["task_id"] == "1"
    assert error["payload"]["mission_id"] == "mission-auto"
    assert error["payload"]["intent"] == "task"
    assert error["payload"]["task_status"] == "blocked"
    assert len([
        record for record in _records(writer) if record["type"] == "workbench/snapshot"
    ]) == 2


@pytest.mark.asyncio
async def test_bridge_shutdown_blocks_active_workbench_task() -> None:
    engine = _SlowTaskSubmitEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-cancel",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "长任务"},
    })
    await engine.started.wait()
    await bridge.shutdown()

    assert engine.task_store.updates == [
        ("1", TaskStatus.IN_PROGRESS),
        ("1", TaskStatus.BLOCKED),
    ]
    assert bridge._run_task is not None
    assert bridge._run_task.done()
    assert len([
        record for record in _records(writer) if record["type"] == "workbench/snapshot"
    ]) == 2


@pytest.mark.asyncio
async def test_bridge_task_submit_persists_real_workbench_graph(tmp_path: Path) -> None:
    database = tmp_path / "task-submit.db"
    engine = _TaskSubmitFakeEngine()
    engine.task_store = TaskStore(database)
    engine.workbench_store = WorkbenchStore(database)
    engine.workbench_service = WorkbenchService(
        task_store=engine.task_store,
        workbench_store=engine.workbench_store,
        workspace_root=str(tmp_path),
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-real-store",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "真实数据库任务", "acceptance_criteria": ["记录可追溯"]},
    })
    assert bridge._run_task is not None
    await bridge._run_task

    missions = (await engine.workbench_service.list_missions("session-task"))["missions"]
    assert len(missions) == 1
    task = await engine.task_store.scoped("session-task").get_task("1")
    assert task is not None
    assert task.status == TaskStatus.COMPLETED
    issue = await engine.workbench_store.get_issue("session-task", "1")
    assert issue is not None
    assert issue.mission_id == missions[0]["id"]
    assert issue.acceptance_criteria == ["记录可追溯"]
    events = await engine.workbench_store.list_events("session-task")
    assert {event.type for event in events} >= {"mission.created", "issue.created"}


@pytest.mark.asyncio
async def test_bridge_presents_model_404_without_raw_provider_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FailingFakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("你好", request_id="submit-failed")
    assert bridge._run_task is not None
    await bridge._run_task

    error = next(record for record in _records(writer) if record["type"] == "error")
    assert error["request_id"] == "submit-failed"
    assert error["payload"] == {
        "message": (
            "模型或 API Base 不匹配，服务端未找到请求资源。"
            "请运行 `naumi doctor --live` 检查配置。"
        ),
        "code": "model_not_found",
    }
    assert "AnthropicException" not in writer.getvalue()
    assert not [record for record in caplog.records if record.levelno >= 40]


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
                "choices": ["allow_once", "deny"],
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
    assert await permission_task == "allow_once"
    assert any(record["type"] == "permission/resolved" for record in _records(writer))


def _permission_payload(
    call_id: str,
    *,
    choices: Any = None,
    requires_double_confirm: bool = False,
) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "session_id": "session-1",
        "tool_name": "bash_run",
        "tool_family": "shell",
        "arguments": {"authorization": "Bearer private", "command": "echo hello"},
        "choices": choices if choices is not None else ["allow_once", "deny", "grant_session"],
        "requires_double_confirm": requires_double_confirm,
    }


@pytest.mark.asyncio
async def test_bridge_resolves_two_permission_requests_in_reverse_order() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    first = asyncio.create_task(bridge.confirm_permission(_permission_payload("call-1")))
    second = asyncio.create_task(bridge.confirm_permission(_permission_payload("call-2")))
    await asyncio.sleep(0)

    requests = [record for record in _records(writer) if record["type"] == "permission/request"]
    assert {record["request_id"] for record in requests} == {"call-1", "call-2"}
    assert all("arguments" not in record["payload"] for record in requests)
    assert requests[0]["payload"]["arguments_summary"]["authorization"] == "[已隐藏]"

    await bridge.resolve_permission(
        {"request_id": "call-2", "choice": "deny"}, request_id="response-2"
    )
    await bridge.resolve_permission(
        {"request_id": "call-1", "choice": "allow_once"}, request_id="response-1"
    )

    assert await second == "deny"
    assert await first == "allow_once"


@pytest.mark.asyncio
async def test_bridge_generates_unique_request_ids_for_blank_call_ids() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(io.StringIO())
    first = asyncio.create_task(bridge.confirm_permission(_permission_payload("")))
    second = asyncio.create_task(bridge.confirm_permission(_permission_payload("")))
    await asyncio.sleep(0)

    request_ids = tuple(bridge._pending_permissions)
    assert len(request_ids) == 2
    assert len(set(request_ids)) == 2
    assert all(request_id for request_id in request_ids)

    await bridge.shutdown()
    assert await first == "deny"
    assert await second == "deny"


@pytest.mark.asyncio
async def test_bridge_keeps_equal_call_id_requests_independently_addressable() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    first = asyncio.create_task(bridge.confirm_permission(_permission_payload("same-call")))
    second = asyncio.create_task(bridge.confirm_permission(_permission_payload("same-call")))
    await asyncio.sleep(0)

    requests = [record for record in _records(writer) if record["type"] == "permission/request"]
    request_ids = [record["request_id"] for record in requests]
    assert request_ids[0] == "same-call"
    assert len(request_ids) == len(set(request_ids)) == 2
    assert all(record["payload"]["call_id"] == "same-call" for record in requests)
    assert all(pending.call_id == "same-call" for pending in bridge._pending_permissions.values())

    await bridge.resolve_permission(
        {"request_id": request_ids[1], "choice": "deny"}, request_id="response-second"
    )
    await bridge.resolve_permission(
        {"request_id": request_ids[0], "choice": "allow_once"}, request_id="response-first"
    )

    assert await second == "deny"
    assert await first == "allow_once"
    assert bridge._pending_permissions == {}

    shutdown_first = asyncio.create_task(
        bridge.confirm_permission(_permission_payload("same-call"))
    )
    shutdown_second = asyncio.create_task(
        bridge.confirm_permission(_permission_payload("same-call"))
    )
    await asyncio.sleep(0)
    assert len(bridge._pending_permissions) == 2

    await bridge.shutdown()

    assert await shutdown_first == "deny"
    assert await shutdown_second == "deny"
    assert bridge._pending_permissions == {}
    assert bridge._permission_challenges.count == 0


@pytest.mark.asyncio
async def test_bridge_rejects_grant_session_absent_from_backend_choices() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(_permission_payload("call-high", choices=["allow_once", "deny"]))
    )
    await asyncio.sleep(0)

    await bridge.resolve_permission(
        {"request_id": "call-high", "choice": "grant_session"}, request_id="response-1"
    )

    assert not task.done()
    assert _records(writer)[-1]["payload"]["code"] == "permission_choice_unavailable"
    await bridge.resolve_permission(
        {"request_id": "call-high", "choice": "deny"}, request_id="response-2"
    )
    assert await task == "deny"


@pytest.mark.asyncio
async def test_bridge_requires_second_confirmation_for_high_risk_permission() -> None:
    engine = _FakeEngine()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-danger",
                choices=["allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    await asyncio.sleep(0)

    await bridge.resolve_permission(
        {"request_id": "call-danger", "choice": "allow_once"}, request_id="response-1"
    )

    confirmation = next(
        record
        for record in _records(writer)
        if record["type"] == "permission/confirmation_required"
    )
    token = confirmation["payload"]["confirmation_token"]
    assert not task.done()
    assert engine.runtime_mode == AgentRuntimeMode.DEFAULT

    await bridge.resolve_permission(
        {
            "request_id": "call-danger",
            "choice": "confirm",
            "confirmation_token": token,
        },
        request_id="response-2",
    )

    assert await task == "allow_once"
    assert any(record["type"] == "permission/resolved" for record in _records(writer))


@pytest.mark.asyncio
async def test_bridge_replaces_high_risk_challenge_and_rejects_superseded_token() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-danger",
                choices=["allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    await asyncio.sleep(0)

    for response_id in ("first-stage-1", "first-stage-2"):
        await bridge.resolve_permission(
            {"request_id": "call-danger", "choice": "allow_once"}, request_id=response_id
        )
    tokens = [
        record["payload"]["confirmation_token"]
        for record in _records(writer)
        if record["type"] == "permission/confirmation_required"
    ]
    superseded, current = tokens

    assert superseded != current
    assert bridge._permission_challenges.count == 1
    await bridge.resolve_permission(
        {
            "request_id": "call-danger",
            "choice": "confirm",
            "confirmation_token": superseded,
        },
        request_id="superseded-token",
    )

    assert not task.done()
    assert _records(writer)[-1]["payload"]["code"] == "permission_challenge_mismatch"
    await bridge.resolve_permission(
        {
            "request_id": "call-danger",
            "choice": "confirm",
            "confirmation_token": current,
        },
        request_id="current-token",
    )

    assert await task == "allow_once"


@pytest.mark.asyncio
async def test_bridge_rejects_replay_after_valid_confirmation() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-danger",
                choices=["allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    await asyncio.sleep(0)
    await bridge.resolve_permission(
        {"request_id": "call-danger", "choice": "allow_once"}, request_id="first-stage"
    )
    token = next(
        record["payload"]["confirmation_token"]
        for record in _records(writer)
        if record["type"] == "permission/confirmation_required"
    )

    await bridge.resolve_permission(
        {"request_id": "call-danger", "choice": "confirm", "confirmation_token": token},
        request_id="valid-confirmation",
    )
    assert await task == "allow_once"

    await bridge.resolve_permission(
        {"request_id": "call-danger", "choice": "confirm", "confirmation_token": token},
        request_id="replayed-confirmation",
    )

    resolved = [record for record in _records(writer) if record["type"] == "permission/resolved"]
    assert len(resolved) == 1
    assert _records(writer)[-1]["payload"]["code"] == "unknown_permission_request"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "missing"])
async def test_bridge_fails_closed_when_backend_choices_are_not_usable(kind: str) -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    payload = _permission_payload("call-no-choices", choices=[])
    if kind == "missing":
        del payload["choices"]

    task = asyncio.create_task(bridge.confirm_permission(payload))
    try:
        await asyncio.sleep(0)

        assert task.done()
        assert await task == "deny"
        assert bridge._pending_permissions == {}
        records = _records(writer)
        assert records[-1]["type"] == "error"
        assert records[-1]["payload"]["code"] == f"permission_choices_{kind}"
        assert "后端权限选择" in records[-1]["payload"]["message"]
        assert not any(record["type"] == "permission/request" for record in records)
        assert not any("grant_session" in str(record["payload"]) for record in records)
    finally:
        if not task.done():
            await bridge.shutdown()
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choices",
    [
        "allow_once",
        {"allow_once": True},
        ["   "],
        ["allow_once", "unknown_choice"],
        ["allow_once", 1],
    ],
)
async def test_bridge_fails_closed_for_malformed_backend_choices(choices: Any) -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    payload = _permission_payload("call-invalid-choices")
    payload["choices"] = choices

    task = asyncio.create_task(bridge.confirm_permission(payload))
    await asyncio.sleep(0)

    assert task.done()
    assert await task == "deny"
    assert bridge._pending_permissions == {}
    records = _records(writer)
    assert records[-1]["type"] == "error"
    assert records[-1]["payload"]["code"] == "permission_choices_invalid"
    assert "后端权限选择" in records[-1]["payload"]["message"]
    assert not any(record["type"] == "permission/request" for record in records)


@pytest.mark.asyncio
async def test_bridge_deduplicates_valid_backend_choices_in_backend_order() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-repeated-choices",
                choices=["grant_session", "allow_once", "deny", "allow_once", "deny"],
            )
        )
    )
    await asyncio.sleep(0)

    request = next(record for record in _records(writer) if record["type"] == "permission/request")
    assert request["payload"]["choices"] == ["grant_session", "allow_once", "deny"]

    await bridge.resolve_permission(
        {"request_id": "call-repeated-choices", "choice": "deny"}, request_id="response"
    )
    assert await task == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choices",
    [
        ["grant_session"],
        ["allow_once", "grant_session"],
        ["deny", "grant_session"],
    ],
)
async def test_bridge_fails_closed_for_unusable_high_risk_choices(choices: list[str]) -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-high-unusable",
                choices=choices,
                requires_double_confirm=True,
            )
        )
    )
    await asyncio.sleep(0)

    assert task.done()
    assert await task == "deny"
    assert bridge._pending_permissions == {}
    records = _records(writer)
    assert records[-1]["payload"]["code"] == "permission_choices_high_risk_unusable"
    assert "后端权限选择" in records[-1]["payload"]["message"]
    assert not any(record["type"] == "permission/request" for record in records)


@pytest.mark.asyncio
async def test_bridge_allows_valid_high_risk_handshake_choices_after_filtering() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-high-valid",
                choices=["grant_session", "allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    await asyncio.sleep(0)

    request = next(record for record in _records(writer) if record["type"] == "permission/request")
    assert request["payload"]["choices"] == ["allow_once", "deny"]
    assert "grant_session" not in request["payload"]["choices"]

    await bridge.resolve_permission(
        {"request_id": "call-high-valid", "choice": "deny"}, request_id="response"
    )
    assert await task == "deny"


@pytest.mark.asyncio
async def test_bridge_fails_closed_for_bad_cross_expired_and_reused_challenges() -> None:
    clock = [100.0]
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge._permission_challenges = PermissionChallengeStore(clock=lambda: clock[0])
    writer = io.StringIO()
    bridge.bind_writer(writer)
    first = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-1",
                choices=["allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    second = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-2",
                choices=["allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    await asyncio.sleep(0)
    for call_id in ("call-1", "call-2"):
        await bridge.resolve_permission(
            {"request_id": call_id, "choice": "allow_once"}, request_id=f"start-{call_id}"
        )
    confirmations = {
        record["payload"]["request_id"]: record["payload"]["confirmation_token"]
        for record in _records(writer)
        if record["type"] == "permission/confirmation_required"
    }

    await bridge.resolve_permission(
        {"request_id": "call-1", "choice": "confirm", "confirmation_token": "unknown"},
        request_id="bad-token",
    )
    await bridge.resolve_permission(
        {
            "request_id": "call-2",
            "choice": "confirm",
            "confirmation_token": confirmations["call-1"],
        },
        request_id="cross-token",
    )
    clock[0] += 31
    await bridge.resolve_permission(
        {
            "request_id": "call-1",
            "choice": "confirm",
            "confirmation_token": confirmations["call-1"],
        },
        request_id="expired-token",
    )
    assert not first.done()
    assert not second.done()

    await bridge.resolve_permission(
        {"request_id": "call-1", "choice": "deny"}, request_id="deny-1"
    )
    assert await first == "deny"
    await bridge.resolve_permission(
        {"request_id": "call-2", "choice": "deny"}, request_id="deny-2"
    )
    assert await second == "deny"


@pytest.mark.asyncio
async def test_bridge_revokes_one_or_all_grants_and_emits_changes() -> None:
    engine = _FakeEngine()
    engine.permission_grants = [
        PermissionGrant("grant-1", "session-1", "shell", "now", None, "perm-1"),
        PermissionGrant("grant-2", "session-1", "code_execution", "now", None, "perm-2"),
    ]
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "revoke-one", "type": "permission_revoke", "payload": {"grant_id": "grant-1"}}
    )
    await bridge.handle_client_record(
        {"id": "revoke-all", "type": "permission_revoke", "payload": {"scope": "all"}}
    )

    changes = [
        record
        for record in _records(writer)
        if record["type"] == "permission/grants_changed"
    ]
    assert changes[0]["payload"]["revoked"] == 1
    assert [grant["grant_id"] for grant in changes[0]["payload"]["grants"]] == ["grant-2"]
    assert changes[1]["payload"]["revoked"] == 1
    assert changes[1]["payload"]["grants"] == []


@pytest.mark.asyncio
async def test_bridge_shutdown_denies_each_pending_permission_and_clears_challenges() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(io.StringIO())
    high_risk = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload(
                "call-danger",
                choices=["allow_once", "deny"],
                requires_double_confirm=True,
            )
        )
    )
    medium_risk = asyncio.create_task(bridge.confirm_permission(_permission_payload("call-medium")))
    await asyncio.sleep(0)
    await bridge.resolve_permission(
        {"request_id": "call-danger", "choice": "allow_once"}, request_id="first-stage"
    )

    await bridge.shutdown()

    assert await high_risk == "deny"
    assert await medium_risk == "deny"
    assert bridge._pending_permissions == {}
    assert bridge._permission_challenges.count == 0


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
async def test_bridge_rejects_second_submit_with_correlated_error_and_no_echo() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "submit-first", "type": ClientEventType.SUBMIT, "payload": {"text": "长任务"}}
    )
    assert bridge._run_task is not None
    await asyncio.sleep(0)

    await bridge.handle_client_record(
        {"id": "submit-second", "type": ClientEventType.SUBMIT, "payload": {"text": "第二条"}}
    )

    records = _records(writer)
    rejection = next(
        record
        for record in records
        if record["type"] == "error"
        and record.get("request_id") == "submit-second"
    )
    assert rejection["payload"]["code"] == "run_in_progress"
    assert not any(
        record["type"] == "user/message"
        and record.get("request_id") == "submit-second"
        for record in records
    )

    engine.release_run.set()
    await bridge._run_task


@pytest.mark.asyncio
async def test_bridge_rejects_run_cancel_without_active_run() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "cancel-idle",
        "type": ClientEventType.RUN_CANCEL,
        "payload": {"reason": "用户按下 Ctrl+C"},
    })

    records = _records(writer)
    assert records[-1]["type"] == "error"
    assert records[-1]["request_id"] == "cancel-idle"
    assert records[-1]["payload"]["code"] == "no_active_run"
    assert engine.shutdown_called is False


@pytest.mark.asyncio
async def test_bridge_cancels_active_run_and_remains_usable() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "submit-long",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "长任务"},
    })
    await asyncio.sleep(0)

    await bridge.handle_client_record({
        "id": "cancel-long",
        "type": ClientEventType.RUN_CANCEL,
        "payload": {"reason": "用户按下 Ctrl+C"},
    })

    records = _records(writer)
    accepted = next(
        record for record in records
        if record["type"] == "ack" and record.get("request_id") == "cancel-long"
    )
    assert accepted["payload"] == {
        "event": "run_cancel",
        "status": "accepted",
        "target_request_id": "submit-long",
    }
    cancelled = next(record for record in records if record["type"] == "run/cancelled")
    assert cancelled["payload"] == {
        "status": "cancelled",
        "target_request_id": "submit-long",
        "intent": "chat",
        "reason": "用户按下 Ctrl+C",
    }
    assert bridge._run_task is not None
    assert bridge._run_task.done()
    assert engine.shutdown_called is False

    engine.release_run.set()
    await bridge.handle_client_record({
        "id": "submit-after-cancel",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "继续使用"},
    })
    assert bridge._run_task is not None
    await bridge._run_task
    assert any(
        record["type"] == "run/completed"
        and record.get("request_id") == "submit-after-cancel"
        for record in _records(writer)
    )


@pytest.mark.asyncio
async def test_bridge_cancel_blocks_active_workbench_task_and_returns_identity() -> None:
    engine = _SlowTaskSubmitEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "task-submit-cancelled",
        "type": ClientEventType.TASK_SUBMIT,
        "payload": {"text": "可取消任务"},
    })
    await engine.started.wait()

    await bridge.handle_client_record({
        "id": "cancel-task",
        "type": ClientEventType.RUN_CANCEL,
        "payload": {},
    })

    assert engine.task_store.updates == [
        ("1", TaskStatus.IN_PROGRESS),
        ("1", TaskStatus.BLOCKED),
    ]
    cancelled = next(
        record for record in _records(writer) if record["type"] == "run/cancelled"
    )
    assert cancelled["payload"] == {
        "status": "cancelled",
        "target_request_id": "task-submit-cancelled",
        "intent": "task",
        "task_id": "1",
        "mission_id": "mission-auto",
        "task_status": "blocked",
        "reason": "用户取消了当前运行。",
    }
    assert len([
        record for record in _records(writer) if record["type"] == "workbench/snapshot"
    ]) == 2


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
        history: bool = False,
    ) -> str:
        assert target_engine is engine
        assert limit == 5
        assert source == "background"
        assert status == "running"
        assert detail_id == "bg_1"
        assert history is False
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
