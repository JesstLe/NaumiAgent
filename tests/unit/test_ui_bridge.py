from __future__ import annotations

import asyncio
import inspect
import io
import json
import subprocess
import sys
import types
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from naumi_agent import __version__
from naumi_agent.agents.base import AgentResult as SubAgentResult
from naumi_agent.background.models import BackgroundStatus
from naumi_agent.config.paths import DEFAULT_CONFIG_PATH
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.eval_surface import (
    HarnessEvalBaselineStatus,
    HarnessEvalBaselineView,
    HarnessEvalBatchProgress,
    HarnessEvalBatchStatus,
    HarnessEvalPromotionStatus,
)
from naumi_agent.harness.explain import HarnessExplainLookup, HarnessRunExplanation
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.models import HarnessTaskKind
from naumi_agent.harness.replay_models import HarnessReplayLookup, HarnessReplayResult
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.model.reasoning import (
    ReasoningEffort,
    ReasoningEffortSetting,
    ReasoningEffortStatus,
)
from naumi_agent.model.router import StreamChunk
from naumi_agent.orchestrator.engine import AgentEngine, AgentResult, AgentRuntimeMode, AgentUsage
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.planner import Complexity, ExecutionMode, Plan, Step
from naumi_agent.orchestrator.pursuit import PursuitRun, PursuitRunStatus
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.orchestrator.subagent_manager import SubTask
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.runtime.ports.events import EventSink, RuntimeEventType
from naumi_agent.safety.permission_grants import PermissionGrant
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.publisher import RuntimeEventPublisher
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.base import ToolCall, ToolResult
from naumi_agent.ui import bridge as ui_bridge
from naumi_agent.ui.bridge import JsonlEngineBridge, resolve_config_path
from naumi_agent.ui.doctor import DoctorCheck
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
    ProtocolNegotiationError,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    negotiate_hello,
    normalize_client_record,
)
from naumi_agent.user_interaction import UserInteractionUnavailableError
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


@pytest.mark.asyncio
async def test_create_bridge_binds_engine_to_process_launch_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from naumi_agent.runtime import composition

    legacy = tmp_path / "legacy"
    launch = tmp_path / "launch"
    legacy.mkdir()
    launch.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "models:",
                "  provider: openai",
                "  default_model: test-model",
                f'workspace_root: "{legacy}"',
                "safety:",
                "  permission_mode: moderate",
                f'  allowed_dirs: ["{legacy}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, AppConfig] = {}

    def forbidden_default_root(_config: AppConfig) -> _FakeEngine:
        raise AssertionError("显式 factory 不得调用默认 root")

    monkeypatch.setattr(
        composition,
        "create_agent_engine",
        forbidden_default_root,
    )

    def engine_factory(config: AppConfig) -> _FakeEngine:
        captured["config"] = config
        engine = _FakeEngine()
        engine.workspace_root = config.resolve_workspace_root()
        return engine

    monkeypatch.chdir(launch)
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "test-secret")

    bridge = await ui_bridge.create_bridge(
        config_path=str(config_path),
        engine_factory=engine_factory,
    )
    bridge.bind_writer(io.StringIO())
    try:
        assert captured["config"].workspace_root == str(launch.resolve())
        assert bridge.engine.workspace_root == launch.resolve()
        assert bridge.status_payload()["workspace_root"] == str(launch.resolve())
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_create_bridge_uses_composition_root_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from naumi_agent.runtime import composition

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default_model: test-model\n",
        encoding="utf-8",
    )
    engine = _FakeEngine()
    captured: list[AppConfig] = []

    def create(config: AppConfig) -> _FakeEngine:
        captured.append(config)
        engine.workspace_root = config.resolve_workspace_root()
        return engine

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(composition, "create_agent_engine", create)
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "test-secret")
    bridge = await ui_bridge.create_bridge(config_path=str(config_path))
    bridge.bind_writer(io.StringIO())
    try:
        assert len(captured) == 1
        assert bridge.engine is engine
        engine.start_long_running_services.assert_awaited_once_with()
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_create_bridge_closes_engine_when_startup_recovery_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models:\n  default_model: test-model\n", encoding="utf-8")
    engine = _FakeEngine()
    engine.start_long_running_services = AsyncMock(
        side_effect=RuntimeError("recovery failed")
    )

    with pytest.raises(RuntimeError, match="recovery failed"):
        await ui_bridge.create_bridge(
            config_path=str(config_path),
            engine_factory=lambda _config: engine,
        )

    assert engine.shutdown_called is True


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
    def __init__(self) -> None:
        self.runtime_effort: ReasoningEffortSetting | None = None

    def resolve_model(self, tier: str) -> str:
        return f"fake-{tier}"

    def get_runtime_identity(self, model: str) -> SimpleNamespace:
        return SimpleNamespace(
            requested_model=model,
            canonical_model="nvidia/fake-capable",
            upstream_model="z-ai/glm4.7",
            provider="nvidia",
            api_format="openai_responses",
            source="catalog",
        )

    def get_model_capability_contract(self, model: str | None = None) -> SimpleNamespace:
        payload = {
            "requested_model": model or "fake-capable",
            "canonical_model": "nvidia/fake-capable",
            "upstream_model": "z-ai/glm4.7",
            "provider": "nvidia",
            "api_format": "openai_responses",
            "max_context": 128000,
            "max_output": 8192,
            "request_max_tokens": 4096,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 4.0,
            "supports_tools": True,
            "supports_streaming": True,
            "supports_parallel_tools": True,
            "supports_structured_output": True,
            "supports_reasoning": True,
            "supports_vision": False,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "field_sources": {"max_context": "catalog"},
            "status": "verified",
            "warnings": [],
            "errors": [],
        }
        return SimpleNamespace(to_dict=lambda: payload)

    def get_reasoning_effort_status(
        self,
        model: str | None = None,
    ) -> ReasoningEffortStatus:
        return ReasoningEffortStatus(
            model=model or "fake-capable",
            effective=self.runtime_effort or ReasoningEffortSetting.MEDIUM,
            source="runtime" if self.runtime_effort is not None else "model",
            supported=(ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH),
            default=ReasoningEffort.MEDIUM,
        )

    def set_reasoning_effort(
        self,
        value: str,
        *,
        model: str | None = None,
    ) -> ReasoningEffortStatus:
        self.runtime_effort = ReasoningEffortSetting(value)
        return self.get_reasoning_effort_status(model)

    def reset_reasoning_effort(
        self,
        *,
        model: str | None = None,
    ) -> ReasoningEffortStatus:
        self.runtime_effort = None
        return self.get_reasoning_effort_status(model)


def _fake_event_callback(
    event_sink: EventSink,
    *,
    run_id: str = "run-fake",
) -> Any:
    assert isinstance(event_sink, EventSink)
    return RuntimeEventPublisher(
        event_sink,
        session_id="session-fake",
        run_id=run_id,
    ).legacy_callback()


class _FakeEngine:
    def __init__(self) -> None:
        self.runtime_mode = AgentRuntimeMode.DEFAULT
        self.permission_mode = PermissionMode.MODERATE
        self.workspace_root = Path.cwd()
        self.usage = AgentUsage(total_input_tokens=12, total_output_tokens=3, turns=1)
        self.router = _FakeRouter()
        self.permission_confirmer = None
        self.user_interaction_handler = None
        self.shutdown_called = False
        self._session = None
        self._config = SimpleNamespace(ui=SimpleNamespace(show_reasoning=False))
        self.permission_grants: list[PermissionGrant] = []
        self.start_long_running_services = AsyncMock(return_value=())

    def session_retention_worker_status(self) -> dict[str, Any]:
        return {
            "configured_enabled": False,
            "state": "stopped",
            "lease_held": False,
            "pass_count": 0,
        }

    def set_permission_confirmer(self, confirmer: Any) -> None:
        self.permission_confirmer = confirmer

    def set_user_interaction_handler(self, handler: Any) -> None:
        self.user_interaction_handler = handler

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
        on_event = _fake_event_callback(on_event)
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


class _SlashToolTrap:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        raise AssertionError("New UI slash 命令不得直接调用 Tool.execute")


class _FacadeSlashEngine(_FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.tool = _SlashToolTrap()
        self.tool_registry = {"worktree_status": self.tool}
        self.executed: list[tuple[ToolCall, str | None]] = []

    async def execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        self.executed.append((tool_call, agent_name))
        return ToolResult(
            call_id=tool_call.id,
            status="success",
            content="Worktree 状态已通过公共 facade 获取。",
        )


class _SlowFakeEngine(_FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.release_run = asyncio.Event()
        self.run_tasks: list[str] = []

    async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
        self.run_tasks.append(task)
        on_event = _fake_event_callback(on_event, run_id=f"run-{len(self.run_tasks)}")
        await on_event("response_start", {})
        await on_event("token", {"content": f"处理中: {task}"})
        await self.release_run.wait()
        await on_event("response_end", {})
        return AgentResult(status="completed", response="完成", usage=self.usage)


class _BlockingShutdownEngine(_FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_started = asyncio.Event()
        self.release_shutdown = asyncio.Event()

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.shutdown_started.set()
        await self.release_shutdown.wait()


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


class _RevisionedWorkbenchService:
    def __init__(
        self,
        snapshots: list[dict[str, Any]] | None = None,
        review_evidence: dict[str, Any] | None = None,
    ) -> None:
        self.snapshots = list(snapshots or [])
        self.calls: list[str] = []
        self.review_evidence = review_evidence
        self.review_calls: list[tuple[str, str]] = []

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        self.calls.append(session_id)
        if not self.snapshots:
            raise RuntimeError("PRIVATE_WORKBENCH_FAILURE")
        return self.snapshots.pop(0)

    async def get_review_evidence(
        self, session_id: str, review_id: str
    ) -> dict[str, Any] | None:
        self.review_calls.append((session_id, review_id))
        return self.review_evidence


class _ProposalActionWorkbenchService:
    def __init__(self) -> None:
        self.governed: list[dict[str, Any]] = []
        self.revision = 1

    async def govern_proposal(self, session_id: str, proposal_id: str, **kwargs: Any):
        self.governed.append({
            "session_id": session_id,
            "proposal_id": proposal_id,
            **kwargs,
        })
        self.revision += 1
        return {
            "id": proposal_id,
            "session_id": session_id,
            "state": "approved" if kwargs["action"].value == "approve" else "rejected",
        }

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "stream_id": "proposal-stream",
            "revision": self.revision,
            "generated_at": "2026-07-18T12:00:00+00:00",
            "full": True,
            "session_id": session_id,
            "counts": {"tasks": 0, "worktrees": 0, "reviews": 0, "failures": 0},
            "active_selection": {},
            "missions": [],
            "tasks": [],
            "issues": [],
            "approvals": [],
            "proposals": [],
            "failures": [],
            "events": [],
        }


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
        on_event = _fake_event_callback(on_event, run_id="run-task")
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


@pytest.mark.asyncio
async def test_bridge_ping_emits_current_retention_worker_status() -> None:
    writer = io.StringIO()
    engine = _FakeEngine()
    engine.session_retention_worker_status = lambda: {  # type: ignore[method-assign]
        "configured_enabled": True,
        "state": "waiting",
        "lease_held": True,
        "pass_count": 2,
    }
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "ping-1", "type": ClientEventType.PING, "payload": {}}
    )

    records = _records(writer)
    assert [record["type"] for record in records] == ["pong", "runtime/status"]
    assert records[1]["payload"]["retention_worker"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_bridge_ping_suppresses_unchanged_retention_worker_status() -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)
    await bridge.emit_ready()
    writer.seek(0)
    writer.truncate(0)

    await bridge.handle_client_record(
        {"id": "ping-stable", "type": ClientEventType.PING, "payload": {}}
    )

    assert [record["type"] for record in _records(writer)] == ["pong"]


def test_bridge_resolve_config_path_uses_existing_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("log_level: DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_config_path("config.yaml") == str(config)


def test_bridge_resolve_config_path_targets_project_naumi_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = Path(resolve_config_path(DEFAULT_CONFIG_PATH))

    assert resolved == tmp_path / ".naumi" / "config.yaml"


async def test_bridge_cli_defaults_to_project_naumi_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    async def fake_create_bridge(*, config_path: str):
        requested.append(config_path)
        return object()

    async def fake_serve_stdio(_bridge: object) -> None:
        return None

    monkeypatch.setattr(ui_bridge, "create_bridge", fake_create_bridge)
    monkeypatch.setattr(ui_bridge, "serve_stdio", fake_serve_stdio)

    await ui_bridge._amain([])

    assert requested == [DEFAULT_CONFIG_PATH]


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
    assert contract["negotiation"] == {
        "minimum_version": 1,
        "maximum_version": 1,
        "capabilities": [
            "goal_snapshot",
            "heartbeat",
            "task_snapshot",
            "typed_ui_messages",
            "workbench_snapshot",
            "workbench_proposal_actions",
        ],
        "required_capabilities": ["typed_ui_messages"],
    }


def test_protocol_negotiates_highest_shared_version_and_capability_intersection() -> None:
    hello = normalize_client_record({
        "id": "hello-1",
        "type": "hello",
        "version": 1,
        "payload": {
            "client": " naumi-terminal-ui ",
            "minimum_version": 1,
            "maximum_version": 1,
            "capabilities": [
                "workbench_snapshot",
                "unknown_client_feature",
                "typed_ui_messages",
                "heartbeat",
                "heartbeat",
            ],
        },
    })

    assert hello["payload"] == {
        "client": "naumi-terminal-ui",
        "minimum_version": 1,
        "maximum_version": 1,
        "capabilities": [
            "heartbeat",
            "typed_ui_messages",
            "unknown_client_feature",
            "workbench_snapshot",
        ],
        "legacy": False,
    }
    assert negotiate_hello(hello["payload"]) == {
        "selected_version": 1,
        "server_minimum_version": 1,
        "server_maximum_version": 1,
        "capabilities": [
            "heartbeat",
            "typed_ui_messages",
            "workbench_snapshot",
        ],
    }


def test_protocol_keeps_one_release_legacy_hello_compatibility() -> None:
    hello = normalize_client_record({
        "type": "hello",
        "version": 1,
        "payload": {"client": "legacy-ui"},
    })

    assert hello["payload"]["legacy"] is True
    assert negotiate_hello(hello["payload"])["selected_version"] == 1


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"minimum_version": True, "maximum_version": 1}, "minimum_version"),
        ({"minimum_version": 2, "maximum_version": 1}, "不能大于"),
        ({"minimum_version": 1, "maximum_version": 1, "capabilities": "all"}, "数组"),
        ({"minimum_version": 1, "maximum_version": 1, "capabilities": ["Bad Flag"]}, "能力名称"),
    ],
)
def test_protocol_rejects_malformed_hello_negotiation(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_client_record({"type": "hello", "version": 1, "payload": payload})


def test_protocol_reports_version_and_required_capability_failures() -> None:
    with pytest.raises(ProtocolNegotiationError) as version_error:
        negotiate_hello({
            "client": "future-ui",
            "minimum_version": 2,
            "maximum_version": 3,
            "capabilities": ["typed_ui_messages"],
            "legacy": False,
        })
    assert version_error.value.code == "protocol_version_unsupported"
    assert "客户端支持 2-3" in str(version_error.value)

    with pytest.raises(ProtocolNegotiationError) as capability_error:
        negotiate_hello({
            "client": "limited-ui",
            "minimum_version": 1,
            "maximum_version": 1,
            "capabilities": ["heartbeat"],
            "legacy": False,
        })
    assert capability_error.value.code == "protocol_capability_missing"
    assert "typed_ui_messages" in str(capability_error.value)


@pytest.mark.asyncio
async def test_bridge_hello_ack_contains_negotiation_before_runtime_status() -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "hello-modern",
        "type": "hello",
        "version": 1,
        "payload": {
            "client": "naumi-terminal-ui",
            "minimum_version": 1,
            "maximum_version": 1,
            "capabilities": ["typed_ui_messages", "heartbeat"],
        },
    })

    records = _records(writer)
    assert [record["type"] for record in records] == ["ack", "runtime/status"]
    assert records[0]["request_id"] == "hello-modern"
    assert records[0]["payload"] == {
        "event": "hello",
        "negotiation": {
            "selected_version": 1,
            "server_minimum_version": 1,
            "server_maximum_version": 1,
            "capabilities": ["heartbeat", "typed_ui_messages"],
        },
    }


@pytest.mark.asyncio
async def test_bridge_rejects_incompatible_hello_without_status_or_runtime_mutation() -> None:
    writer = io.StringIO()
    engine = _FakeEngine()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "hello-future",
        "type": "hello",
        "version": 1,
        "payload": {
            "client": "future-ui",
            "minimum_version": 2,
            "maximum_version": 3,
            "capabilities": ["typed_ui_messages"],
        },
    })

    records = _records(writer)
    assert [record["type"] for record in records] == ["error"]
    assert records[0]["request_id"] == "hello-future"
    assert records[0]["payload"]["code"] == "protocol_version_unsupported"
    assert "请升级" in records[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_bridge_emits_typed_harness_receipt_without_duplicate_ui_message() -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)
    payload = {
        "run_id": "harness-run-1",
        "status": "completed_unverified",
        "task_kind": "change",
        "changed_files": ["src/app.py"],
        "checks": [{"id": "unit", "status": "failed"}],
        "criteria": [],
        "warnings": ["定向检查失败"],
        "tree_fingerprint": "a" * 64,
    }

    await bridge.handle_engine_event("harness_completion_receipt", payload)

    records = _records(writer)
    typed = next(record for record in records if record["type"] == "harness/receipt")
    assert typed["payload"] == {
        **payload,
        "schema_version": 1,
        "revision": 1,
    }
    assert not any(
        record["type"] == "ui/message"
        and record["payload"].get("title") == "Harness 完成回执"
        for record in records
    )


@pytest.mark.asyncio
async def test_bridge_resends_harness_explain_request_at_same_revision() -> None:
    class DetailService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def explain_run(self, run_id: str) -> HarnessExplainLookup:
            self.calls.append(run_id)
            return HarnessExplainLookup(
                status="ok",
                explanation=HarnessRunExplanation(
                    run_id=run_id,
                    status="completed_verified",
                    objective="验证类型化 Explain",
                    started_at="2026-07-15T10:00:00+00:00",
                    completed_at="2026-07-15T10:01:00+00:00",
                    verified=True,
                    running=False,
                    summary="验证完成，无已知失败。",
                    failure_classes=(),
                    findings=(),
                    checks=(),
                    evidence=(),
                ),
            )

    engine = _FakeEngine()
    service = DetailService()
    engine.harness_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    for request_id, known_revision in (("explain-first", 0), ("explain-resend", 1)):
        await bridge.handle_client_record(
            {
                "id": request_id,
                "type": ClientEventType.HARNESS_EXPLAIN_REQUEST,
                "payload": {
                    "run_id": "detail-run",
                    "known_revision": known_revision,
                },
            }
        )

    responses = [
        record for record in _records(writer) if record["type"] == "harness/explain"
    ]
    assert service.calls == ["detail-run", "detail-run"]
    assert [record["request_id"] for record in responses] == [
        "explain-first",
        "explain-resend",
    ]
    assert responses[0]["payload"] == responses[1]["payload"]
    assert responses[0]["payload"]["revision"] == 1
    assert responses[0]["payload"]["explanation"]["verified"] is True


@pytest.mark.asyncio
async def test_bridge_returns_typed_harness_eval_baseline_snapshot() -> None:
    class BaselineService:
        async def eval_baseline_status(self, suite_id: str) -> HarnessEvalBaselineStatus:
            return HarnessEvalBaselineStatus(
                status="ok",
                suite_id=suite_id,
                active=HarnessEvalBaselineView(
                    id="a" * 64,
                    version=1,
                    batch_id="baseline-1",
                    sample_count=5,
                    identity_sha256="b" * 64,
                    samples_sha256="c" * 64,
                    promoted_by="user",
                    promotion_reason="真实场景验证完成",
                    created_at="2026-07-18T10:00:00+00:00",
                ),
            )

    engine = _FakeEngine()
    engine.harness_service = BaselineService()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "baseline-status",
            "type": ClientEventType.HARNESS_EVAL_BASELINE_REQUEST,
            "payload": {"suite_id": "surface-protocol"},
        }
    )

    response = next(
        record for record in _records(writer) if record["type"] == "harness/eval-baseline"
    )
    assert response["request_id"] == "baseline-status"
    assert response["payload"]["active"]["version"] == 1
    assert response["payload"]["suite_id"] == "surface-protocol"
    assert len(response["payload"]["snapshot_sha256"]) == 64


@pytest.mark.asyncio
async def test_bridge_streams_non_blocking_harness_eval_batch_progress() -> None:
    class BatchService:
        async def eval_repetition_batch(
            self, suite: str, **kwargs: Any
        ) -> HarnessEvalBatchStatus:
            callback = kwargs["on_progress"]
            await callback(
                HarnessEvalBatchProgress(
                    stage="evaluating",
                    batch_id="candidate-1",
                    suite_id=suite,
                    requested=5,
                    completed=1,
                    persisted=0,
                )
            )
            return HarnessEvalBatchStatus(
                status="completed",
                batch_id="candidate-1",
                suite_id=suite,
                requested=5,
                completed=5,
                persisted=5,
                duration_ms=10,
            )

    engine = _FakeEngine()
    engine.harness_service = BatchService()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "batch-request",
            "type": ClientEventType.HARNESS_EVAL_BATCH_REQUEST,
            "payload": {
                "suite_id": "surface-protocol",
                "repetitions": 5,
                "batch_id": "candidate-1",
            },
        }
    )
    tasks = tuple(bridge._harness_eval_batch_tasks.values())
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    responses = [
        record for record in _records(writer) if record["type"] == "harness/eval-batch"
    ]
    assert [record["payload"]["stage"] for record in responses] == [
        "evaluating",
        "completed",
    ]
    assert all(record["request_id"] == "batch-request" for record in responses)
    assert responses[-1]["payload"]["terminal"] is True


@pytest.mark.asyncio
async def test_bridge_runs_guided_harness_eval_promotion_through_interaction_protocol() -> None:
    class PromotionService:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def promote_eval_baseline(
            self,
            suite_id: str,
            batch_id: str,
            *,
            actor: str,
            reason: str,
        ) -> HarnessEvalPromotionStatus:
            self.calls.append(
                {
                    "suite_id": suite_id,
                    "batch_id": batch_id,
                    "actor": actor,
                    "reason": reason,
                }
            )
            return HarnessEvalPromotionStatus(
                status="promoted",
                suite_id=suite_id,
                batch_id=batch_id,
                baseline_id="a" * 64,
                active_baseline_id="a" * 64,
                version=1,
                sample_count=5,
                promoted_by="user",
                promotion_reason=reason,
                created_at="2026-07-18T10:00:00+00:00",
            )

    engine = _FakeEngine()
    service = PromotionService()
    engine.harness_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "promotion-request",
            "type": ClientEventType.HARNESS_EVAL_PROMOTION_REQUEST,
            "payload": {
                "suite_id": "surface-protocol",
                "batch_id": "candidate-1",
                "reason": "",
            },
        }
    )
    task = bridge._harness_eval_promotion_tasks["promotion-request"]
    await asyncio.sleep(0)
    first = next(
        record
        for record in _records(writer)
        if record["type"] == "interaction/request"
    )
    await bridge.handle_client_record(
        {
            "id": "promotion-reason",
            "type": ClientEventType.INTERACTION_RESPONSE,
            "payload": {
                "request_id": first["payload"]["request_id"],
                "kind": "option",
                "value": "recommended",
            },
        }
    )
    await asyncio.sleep(0)
    interactions = [
        record
        for record in _records(writer)
        if record["type"] == "interaction/request"
    ]
    assert len(interactions) == 2
    await bridge.handle_client_record(
        {
            "id": "promotion-confirm",
            "type": ClientEventType.INTERACTION_RESPONSE,
            "payload": {
                "request_id": interactions[1]["payload"]["request_id"],
                "kind": "option",
                "value": "confirm",
            },
        }
    )
    await task

    promotion_events = [
        record
        for record in _records(writer)
        if record["type"] == "harness/eval-promotion"
    ]
    assert [record["payload"]["stage"] for record in promotion_events] == [
        "awaiting_reason",
        "awaiting_confirmation",
        "promoted",
    ]
    assert all(record["request_id"] == "promotion-request" for record in promotion_events)
    assert promotion_events[-1]["payload"]["terminal"] is True
    assert service.calls[0]["actor"] == "user"
    assert "审阅完整 Eval Batch" in service.calls[0]["reason"]


@pytest.mark.asyncio
async def test_bridge_rejects_duplicate_and_fifth_pending_harness_promotions() -> None:
    engine = _FakeEngine()
    engine.harness_service = object()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    payload = {
        "suite_id": "surface-protocol",
        "batch_id": "candidate-1",
        "reason": "",
    }

    await bridge.start_harness_eval_promotion(payload, request_id="promotion-0")
    await asyncio.sleep(0)
    await bridge.start_harness_eval_promotion(payload, request_id="promotion-0")
    for index in range(1, 4):
        await bridge.start_harness_eval_promotion(
            {**payload, "batch_id": f"candidate-{index + 1}"},
            request_id=f"promotion-{index}",
        )
    await asyncio.sleep(0)
    await bridge.start_harness_eval_promotion(
        {**payload, "batch_id": "candidate-5"},
        request_id="promotion-4",
    )

    codes = [
        record["payload"]["code"]
        for record in _records(writer)
        if record["type"] == "error"
    ]
    assert "harness_eval_promotion_duplicate" in codes
    assert "harness_eval_promotion_limit" in codes
    tasks = tuple(bridge._harness_eval_promotion_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert bridge._harness_eval_promotion_tasks == {}


@pytest.mark.asyncio
async def test_bridge_keeps_heartbeat_live_and_bounds_parallel_eval_batches() -> None:
    gate = asyncio.Event()

    class BlockingBatchService:
        async def eval_repetition_batch(
            self, suite: str, **kwargs: Any
        ) -> HarnessEvalBatchStatus:
            batch_id = kwargs["batch_id"]
            await kwargs["on_progress"](
                HarnessEvalBatchProgress(
                    stage="preparing",
                    batch_id=batch_id,
                    suite_id=suite,
                    requested=5,
                    completed=0,
                    persisted=0,
                )
            )
            await gate.wait()
            return HarnessEvalBatchStatus(
                status="completed",
                batch_id=batch_id,
                suite_id=suite,
                requested=5,
                completed=5,
                persisted=5,
                duration_ms=10,
            )

    engine = _FakeEngine()
    engine.harness_service = BlockingBatchService()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    for index in range(4):
        await bridge.handle_client_record(
            {
                "id": f"batch-{index}",
                "type": ClientEventType.HARNESS_EVAL_BATCH_REQUEST,
                "payload": {
                    "suite_id": "surface-protocol",
                    "repetitions": 5,
                    "batch_id": f"candidate-{index}",
                },
            }
        )
    await asyncio.sleep(0)
    await bridge.handle_client_record(
        {"id": "ping-during-batch", "type": ClientEventType.PING, "payload": {}}
    )
    await bridge.handle_client_record(
        {
            "id": "batch-over-limit",
            "type": ClientEventType.HARNESS_EVAL_BATCH_REQUEST,
            "payload": {
                "suite_id": "surface-protocol",
                "repetitions": 5,
                "batch_id": "candidate-over-limit",
            },
        }
    )

    records = _records(writer)
    assert any(
        record["type"] == "pong" and record["request_id"] == "ping-during-batch"
        for record in records
    )
    assert any(
        record["type"] == "error"
        and record["request_id"] == "batch-over-limit"
        and record["payload"]["code"] == "harness_eval_batch_limit"
        for record in records
    )

    tasks = tuple(bridge._harness_eval_batch_tasks.values())
    gate.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_bridge_handles_harness_replay_request() -> None:
    class DetailService:
        async def replay_run(self, run_id: str) -> HarnessReplayLookup:
            return HarnessReplayLookup(
                status="ok",
                result=HarnessReplayResult(
                    run_id=run_id,
                    status="reproduced",
                    baseline_manifest_sha256="a" * 64,
                    current_manifest_sha256="a" * 64,
                    baseline_rule_version="1",
                    current_rule_version="1",
                    baseline_explanation_sha256="b" * 64,
                    current_explanation_sha256="b" * 64,
                    timeline=(),
                    artifacts=(),
                    anomalies=(),
                    differences=(),
                ),
            )

    engine = _FakeEngine()
    engine.harness_service = DetailService()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "replay-request",
            "type": ClientEventType.HARNESS_REPLAY_REQUEST,
            "payload": {"run_id": "detail-run", "known_revision": 0},
        }
    )

    response = next(
        record for record in _records(writer) if record["type"] == "harness/replay"
    )
    assert response["request_id"] == "replay-request"
    assert response["payload"]["run_id"] == "detail-run"
    assert response["payload"]["result"]["status"] == "reproduced"


@pytest.mark.asyncio
async def test_bridge_returns_typed_harness_detail_unavailable_without_leaking_error() -> None:
    class FailingService:
        async def explain_run(self, _run_id: str) -> HarnessExplainLookup:
            raise RuntimeError("private path: /Users/example/secret")

    class MalformedService:
        async def explain_run(self, _run_id: str) -> HarnessExplainLookup:
            return HarnessExplainLookup(status="ok", explanation=None)

    class RunningService:
        async def explain_run(self, run_id: str) -> HarnessExplainLookup:
            return HarnessExplainLookup(
                status="ok",
                explanation=HarnessRunExplanation(
                    run_id=run_id,
                    status="running",
                    objective="仍在运行",
                    started_at="2026-07-15T10:00:00+00:00",
                    completed_at="",
                    verified=False,
                    running=True,
                    summary="仍在运行",
                    failure_classes=(),
                    findings=(),
                    checks=(),
                    evidence=(),
                ),
            )

    for service in (None, FailingService(), MalformedService(), RunningService()):
        engine = _FakeEngine()
        if service is not None:
            engine.harness_service = service
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record(
            {
                "id": "unavailable-request",
                "type": ClientEventType.HARNESS_EXPLAIN_REQUEST,
                "payload": {"run_id": "detail-run", "known_revision": 1},
            }
        )

        response = next(
            record for record in _records(writer) if record["type"] == "harness/explain"
        )
        assert response["request_id"] == "unavailable-request"
        assert response["payload"]["lookup_status"] == "unavailable"
        assert "explanation" not in response["payload"]
        assert "private path" not in json.dumps(response, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bridge_returns_typed_unavailable_for_mutable_harness_replay() -> None:
    class RunningReplayService:
        async def replay_run(self, run_id: str) -> HarnessReplayLookup:
            return HarnessReplayLookup(
                status="ok",
                result=HarnessReplayResult(
                    run_id=run_id,
                    status="partial",
                    baseline_manifest_sha256="a" * 64,
                    current_manifest_sha256="a" * 64,
                    baseline_rule_version="1",
                    current_rule_version="1",
                    baseline_explanation_sha256="b" * 64,
                    current_explanation_sha256="b" * 64,
                    timeline=(),
                    artifacts=(),
                    anomalies=("run_not_finished",),
                    differences=(),
                ),
            )

    engine = _FakeEngine()
    engine.harness_service = RunningReplayService()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "running-replay",
            "type": ClientEventType.HARNESS_REPLAY_REQUEST,
            "payload": {"run_id": "running-run", "known_revision": 0},
        }
    )

    response = next(
        record for record in _records(writer) if record["type"] == "harness/replay"
    )
    assert response["request_id"] == "running-replay"
    assert response["payload"]["lookup_status"] == "unavailable"
    assert "result" not in response["payload"]


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


@pytest.mark.asyncio
async def test_bridge_pong_reports_control_plane_runtime_facts() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("active", request_id="submit-active")
    await asyncio.sleep(0)
    await bridge.submit("queued", request_id="submit-queued")
    await bridge.handle_client_record({
        "id": "heartbeat-7",
        "type": ClientEventType.PING,
        "payload": {},
    })

    pong = next(record for record in _records(writer) if record["type"] == "pong")
    assert pong["request_id"] == "heartbeat-7"
    assert pong["payload"] == {
        "ok": True,
        "active_run": True,
        "queued_conversations": 1,
    }

    await bridge.shutdown()


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

    inspector_record = normalize_client_record({
        "type": "inspector/request",
        "payload": {
            "open": "true",
            "known_revision": "7",
            "session_id": " session-1 ",
            "ignored": "value",
        },
    })
    assert inspector_record["payload"] == {
        "open": True,
        "known_revision": 7,
        "session_id": "session-1",
    }

    agents_record = normalize_client_record({
        "type": "agents/request",
        "payload": {
            "open": "true",
            "known_revision": "3",
            "session_id": " session-1 ",
        },
    })
    assert agents_record["payload"] == {
        "open": True,
        "known_revision": 3,
        "session_id": "session-1",
    }
    stop_record = normalize_client_record({
        "type": "agents/stop",
        "payload": {
            "task_id": " execution-1 ",
            "session_id": " session-1 ",
            "reason": " 用户停止。 ",
        },
    })
    assert stop_record["payload"] == {
        "task_id": "execution-1",
        "session_id": "session-1",
        "reason": "用户停止。",
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

    with pytest.raises(ValueError, match="known_revision"):
        normalize_client_record({
            "type": "inspector/request",
            "payload": {"known_revision": -1},
        })

    with pytest.raises(ValueError, match="task_id"):
        normalize_client_record({"type": "agents/stop", "payload": {}})


@pytest.mark.asyncio
async def test_bridge_agent_snapshot_updates_and_session_isolation(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    try:
        session = await engine.get_or_create_session(title="Agent Bridge")
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "agents-open",
            "type": "agents/request",
            "payload": {"open": True, "known_revision": 0},
        })
        snapshot = next(
            record for record in reversed(_records(writer))
            if record["type"] == "agents/snapshot"
        )
        assert snapshot["request_id"] == "agents-open"
        assert snapshot["payload"]["session_id"] == session.id
        first_revision = snapshot["payload"]["revision"]

        await engine.subagent_manager.message_bus.blackboard_set(
            "team/review",
            "ready",
            "coder",
        )
        await bridge.handle_engine_event("team_event", {
            "event_type": "decision",
            "sender": "coder",
            "recipient": "reviewer",
        })
        update = next(
            record for record in reversed(_records(writer))
            if record["type"] == "agents/update"
        )
        assert update["payload"]["revision"] == first_revision + 1
        assert set(update["payload"]["changed_sections"]) == {"blackboard"}

        await bridge.handle_client_record({
            "id": "agents-wrong-session",
            "type": "agents/request",
            "payload": {"open": True, "session_id": "other-session"},
        })
        rejected = _records(writer)[-1]
        assert rejected["type"] == "error"
        assert rejected["payload"]["code"] == "agents_session_mismatch"

        await bridge.handle_client_record({
            "id": "agents-close",
            "type": "agents/request",
            "payload": {"open": False},
        })
        close_index = len(_records(writer))
        await engine.subagent_manager.message_bus.blackboard_set(
            "team/review",
            "closed",
            "coder",
        )
        await bridge.handle_engine_event("team_event", {"event_type": "decision"})
        assert not any(
            record["type"].startswith("agents/")
            for record in _records(writer)[close_index:]
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_agent_initial_snapshot_failure_is_user_visible(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    try:
        await engine.get_or_create_session(title="Agent Snapshot Failure")
        engine.agent_control.snapshot = AsyncMock(side_effect=RuntimeError("source down"))
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "agents-open-failed",
            "type": "agents/request",
            "payload": {"open": True},
        })

        failure = _records(writer)[-1]
        assert failure["type"] == "error"
        assert failure["request_id"] == "agents-open-failed"
        assert failure["payload"] == {
            "message": "Agent 页面暂时无法加载，请稍后重试。",
            "code": "agents_snapshot_failed",
        }
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_agent_revision_ack_gap_fallback_and_refresh_retention(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    try:
        await engine.get_or_create_session(title="Agent Revision")
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)
        await bridge.handle_client_record({
            "id": "agents-first",
            "type": "agents/request",
            "payload": {"open": True},
        })
        first = bridge._agents_snapshot
        assert first is not None

        await bridge.handle_client_record({
            "id": "agents-current",
            "type": "agents/request",
            "payload": {"open": True, "known_revision": first.revision},
        })
        current = _records(writer)[-1]
        assert current["type"] == "ack"
        assert current["payload"] == {
            "event": "agents/request",
            "open": True,
            "revision": first.revision,
        }

        gap = replace(
            first,
            revision=first.revision + 2,
            warnings=("检测到数据源延迟。",),
        )
        engine.agent_control.snapshot = AsyncMock(return_value=gap)
        await bridge.handle_engine_event("team_event", {"event_type": "status_update"})
        fallback = _records(writer)[-1]
        assert fallback["type"] == "agents/snapshot"
        assert fallback["payload"]["revision"] == gap.revision
        assert bridge._agents_snapshot is gap

        engine.agent_control.snapshot = AsyncMock(side_effect=RuntimeError("source down"))
        await bridge.handle_engine_event("team_event", {"event_type": "status_update"})
        failure = _records(writer)[-1]
        assert failure["type"] == "error"
        assert failure["payload"]["code"] == "agents_refresh_failed"
        assert bridge._agents_snapshot is gap
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_agent_stop_unknown_task_returns_stable_action(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    try:
        session = await engine.get_or_create_session(title="Unknown Agent Stop")
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "agents-stop-missing",
            "type": "agents/stop",
            "payload": {"session_id": session.id, "task_id": "missing-task"},
        })

        action = _records(writer)[-1]
        assert action["type"] == "agents/action"
        assert action["request_id"] == "agents-stop-missing"
        assert action["payload"]["accepted"] is False
        assert action["payload"]["code"] == "not_found"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_agent_stop_returns_action_and_authoritative_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    delegated: asyncio.Task[AgentResult] | None = None
    try:
        session = await engine.get_or_create_session(title="Agent Stop")
        agent = engine.subagent_manager.get_agent("coder")
        assert agent is not None
        started = asyncio.Event()

        async def blocking_execute(**kwargs: object) -> AgentResult:
            started.set()
            await asyncio.Event().wait()
            return AgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", blocking_execute)
        delegated = asyncio.create_task(engine.subagent_manager.delegate(
            SubTask("stop-me", "等待停止", "coder")
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)
        await bridge.handle_client_record({
            "id": "agents-open-stop",
            "type": "agents/request",
            "payload": {"open": True},
        })

        await bridge.handle_client_record({
            "id": "agents-stop",
            "type": "agents/stop",
            "payload": {
                "session_id": session.id,
                "task_id": "stop-me",
                "reason": "用户确认停止。",
            },
        })
        action = next(
            record for record in reversed(_records(writer))
            if record["type"] == "agents/action"
        )
        assert action["request_id"] == "agents-stop"
        assert action["payload"] == {
            "task_id": "stop-me",
            "accepted": True,
            "code": "accepted",
            "message": "已请求停止 Agent 执行 stop-me。",
        }
        assert (await asyncio.wait_for(delegated, timeout=1)).status == "cancelled"

        await bridge.handle_engine_event("subagent_event", {
            "task_id": "stop-me",
            "agent_name": "coder",
            "status": "cancelled",
        })
        terminal = next(
            record for record in reversed(_records(writer))
            if record["type"] in {"agents/update", "agents/snapshot"}
        )
        payload = terminal["payload"]
        executions = (
            payload["changed_sections"]["executions"]
            if terminal["type"] == "agents/update"
            else payload["executions"]
        )
        assert executions[0]["status"] == "cancelled"
        assert executions[0]["stop_supported"] is False

        await bridge.handle_client_record({
            "id": "agents-stop-again",
            "type": "agents/stop",
            "payload": {"session_id": session.id, "task_id": "stop-me"},
        })
        repeated = next(
            record for record in reversed(_records(writer))
            if record["type"] == "agents/action"
            and record.get("request_id") == "agents-stop-again"
        )
        assert repeated["type"] == "agents/action"
        assert repeated["payload"]["code"] == "already_finished"
    finally:
        if delegated is not None and not delegated.done():
            delegated.cancel()
            await asyncio.gather(delegated, return_exceptions=True)
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_agent_stop_rejects_execution_from_another_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    delegated: asyncio.Task[AgentResult] | None = None
    try:
        await engine.get_or_create_session(title="old")
        agent = engine.subagent_manager.get_agent("coder")
        assert agent is not None
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_execute(**kwargs: object) -> SubAgentResult:
            started.set()
            await release.wait()
            return SubAgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", blocking_execute)
        delegated = asyncio.create_task(engine.subagent_manager.delegate(
            SubTask("old-session-task", "等待", "coder")
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        engine._session = await engine.session_store.create_session(title="new")
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "cross-session-stop",
            "type": "agents/stop",
            "payload": {
                "session_id": engine._session.id,
                "task_id": "old-session-task",
            },
        })

        rejected = _records(writer)[-1]
        assert rejected["type"] == "error"
        assert rejected["payload"]["code"] == "agents_session_mismatch"
        assert not delegated.done()
        release.set()
        assert (await delegated).status == "completed"
    finally:
        if delegated is not None and not delegated.done():
            delegated.cancel()
            await asyncio.gather(delegated, return_exceptions=True)
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_inspector_snapshot_updates_and_session_isolation(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "vectors"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        session = await engine.get_or_create_session(title="Inspector Bridge")
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "inspector-open",
            "type": "inspector/request",
            "payload": {"open": True, "known_revision": 0},
        })
        snapshot = [
            record
            for record in _records(writer)
            if record["type"] == "inspector/snapshot"
        ][-1]
        assert snapshot["request_id"] == "inspector-open"
        assert snapshot["payload"]["session_id"] == session.id
        first_revision = snapshot["payload"]["revision"]

        event = {
            "event_id": "inspector-tool-1",
            "session_id": session.id,
            "run_id": "run-inspector",
            "name": "file_read",
            "call_id": "read-1",
            "args": json.dumps({"path": "README.md"}),
        }
        engine.runtime_inspector.observe("tool_start", event)
        await bridge.handle_engine_event("tool_start", event)

        update = [
            record
            for record in _records(writer)
            if record["type"] == "inspector/update"
        ][-1]
        assert update["payload"]["revision"] > first_revision
        assert set(update["payload"]["changed_tabs"]) == {"tools"}
        assert update["payload"]["changed_tabs"]["tools"]["items"][0]["call_id"] == "read-1"

        before_rejection = len(_records(writer))
        await bridge.handle_client_record({
            "id": "inspector-wrong-session",
            "type": "inspector/request",
            "payload": {
                "open": True,
                "known_revision": update["payload"]["revision"],
                "session_id": "another-session",
            },
        })
        rejected = _records(writer)[before_rejection:]
        assert rejected[-1]["type"] == "error"
        assert rejected[-1]["payload"]["code"] == "inspector_session_mismatch"

        await bridge.handle_client_record({
            "id": "inspector-close",
            "type": "inspector/request",
            "payload": {"open": False},
        })
        close_index = len(_records(writer))
        second_event = {
            "event_id": "inspector-tool-2",
            "session_id": session.id,
            "run_id": "run-inspector",
            "name": "file_read",
            "call_id": "read-2",
        }
        engine.runtime_inspector.observe("tool_start", second_event)
        await bridge.handle_engine_event("tool_start", second_event)
        after_close = _records(writer)[close_index:]
        assert not any(record["type"].startswith("inspector/") for record in after_close)
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_inspector_known_revision_gap_gets_full_snapshot(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "vectors"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        await engine.get_or_create_session(title="Inspector Gap")
        await engine.runtime_inspector.snapshot()
        engine.runtime_inspector.observe(
            "tool_start",
            {"event_id": "gap-tool", "name": "file_read", "call_id": "gap-1"},
        )
        await engine.runtime_inspector.snapshot()
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "inspector-gap",
            "type": "inspector/request",
            "payload": {"open": True, "known_revision": 0},
        })

        records = _records(writer)
        assert records[-1]["type"] == "inspector/snapshot"
        assert records[-1]["payload"]["revision"] >= 2
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_inspector_top_level_only_revision_uses_full_snapshot(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "vectors"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        session = await engine.get_or_create_session(title="Inspector Top Level")
        initial = RuntimeInspectorSnapshot.empty(session_id=session.id).with_revision(
            1,
            "2026-07-13T00:00:00+00:00",
        )
        changed = replace(
            initial,
            revision=2,
            generated_at="2026-07-13T00:00:01+00:00",
            active_run_id="run-new",
        )
        snapshots = iter((initial, changed))
        engine.runtime_inspector.snapshot = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda: next(snapshots)
        )
        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)

        await bridge.handle_client_record({
            "id": "inspector-top-open",
            "type": "inspector/request",
            "payload": {"open": True},
        })
        await bridge.handle_engine_event("run_started", {"run_id": "run-new"})

        inspector_records = [
            record
            for record in _records(writer)
            if record["type"].startswith("inspector/")
        ]
        assert [record["type"] for record in inspector_records] == [
            "inspector/snapshot",
            "inspector/snapshot",
        ]
        assert inspector_records[-1]["payload"]["active_run_id"] == "run-new"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_bridge_status_payload_includes_session_id() -> None:
    engine = _FakeEngine()
    engine._session = SimpleNamespace(id="session-abc")
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")

    assert bridge.status_payload()["session_id"] == "session-abc"


def test_bridge_status_payload_exposes_authoritative_product_identity() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")

    payload = bridge.status_payload()

    assert payload["version"] == __version__
    assert payload["model"] == "fake-capable"
    assert payload["provider"] == "nvidia"
    assert payload["api_format"] == "openai_responses"
    assert payload["upstream_model"] == "z-ai/glm4.7"
    assert payload["workspace_root"]
    assert payload["mode"] == "default"
    assert payload["permission_mode"] == "moderate"
    assert payload["reasoning_effort"] == {
        "model": "fake-capable",
        "effective": "medium",
        "source": "model",
        "supported": ["low", "medium", "high"],
        "default": "medium",
        "warning": None,
    }
    assert payload["model_contract"]["status"] == "verified"
    assert payload["model_contract"]["max_context"] == 128000
    assert payload["model_contract"]["supports_parallel_tools"] is True
    assert payload["protocol_registry"]["contract_version"] == 1
    assert payload["protocol_registry"]["client_event_count"] == len(ClientEventType)
    assert payload["protocol_registry"]["server_event_count"] == len(ServerEventType)
    assert len(payload["protocol_registry"]["registry_sha256"]) == 64
    assert payload["evolution_patch_recovery"]["total"] == 0


def test_bridge_status_payload_exposes_patch_recovery_without_source_content() -> None:
    engine = _FakeEngine()
    engine.evolution_patch_recovery_status = lambda: {  # type: ignore[attr-defined]
            "total": 2,
            "single_file_total": 1,
            "multi_file_total": 1,
            "completed": 1,
            "rolled_back": 1,
            "already_baseline": 0,
            "orphan_lock_removed": 0,
            "deferred": 0,
            "failed": 1,
            "filesystem_changed": 1,
            "failure_codes": ["journal_corrupt"],
        }

    payload = JsonlEngineBridge(engine, config_path="config.yaml").status_payload()

    assert payload["evolution_patch_recovery"] == {
        "total": 2,
        "single_file_total": 1,
        "multi_file_total": 1,
        "completed": 1,
        "rolled_back": 1,
        "already_baseline": 0,
        "orphan_lock_removed": 0,
        "deferred": 0,
        "failed": 1,
        "filesystem_changed": 1,
        "failure_codes": ["journal_corrupt"],
    }
    assert "backup" not in str(payload["evolution_patch_recovery"])


@pytest.mark.asyncio
async def test_bridge_effort_slash_emits_refreshed_authoritative_status() -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "effort-1",
            "type": ClientEventType.SUBMIT,
            "payload": {"text": "/effort high"},
        }
    )

    records = _records(writer)
    status = [
        record["payload"]
        for record in records
        if record["type"] == ServerEventType.STATUS
    ][-1]
    assert status["reasoning_effort"]["effective"] == "high"
    assert status["reasoning_effort"]["source"] == "runtime"


def test_bridge_status_payload_keeps_model_when_runtime_identity_fails() -> None:
    engine = _FakeEngine()

    def fail_identity(_model: str) -> None:
        raise ValueError("invalid catalog")

    engine.router.get_runtime_identity = fail_identity  # type: ignore[method-assign]
    payload = JsonlEngineBridge(engine, config_path="config.yaml").status_payload()

    assert payload["model"] == "fake-capable"
    assert payload["provider"] == ""
    assert payload["api_format"] == ""
    assert payload["upstream_model"] == ""


@pytest.mark.asyncio
async def test_bridge_ready_event_carries_authoritative_product_identity() -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.emit_ready()

    ready = _records(writer)[0]
    assert ready["type"] == "ready"
    assert ready["payload"]["version"] == __version__
    assert ready["payload"]["model"] == "fake-capable"
    assert ready["payload"]["workspace_root"]
    assert ready["payload"]["permission_mode"] == "moderate"


@pytest.mark.asyncio
async def test_bridge_ready_event_carries_nullable_budget(tmp_path: Path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    try:
        await bridge.emit_ready()
        ready = _records(writer)[0]
        budget = ready["payload"]["budget"]

        assert budget["enabled"] is False
        assert budget["max_usd"] is None
        json.dumps(ready, allow_nan=False)
    finally:
        await engine.shutdown()


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
    assert "/models" in command_names
    assert "/harness" in command_names
    assert "/goal" in command_names
    harness = next(item for item in slash_commands if item["command"] == "/harness")
    assert "知识" in harness["description"]
    assert "解释" in harness["description"]
    assert "评测" in harness["description"]


def test_bridge_fallback_slash_registry_keeps_goal_available() -> None:
    commands = {
        item["command"]: item
        for item in ui_bridge._fallback_slash_command_registry()
    }

    assert "/goal" in commands
    assert "持久目标" in commands["/goal"]["description"]


def test_bridge_status_payload_can_omit_static_slash_commands() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")

    payload = bridge.status_payload(include_slash_commands=False)

    assert "slash_commands" not in payload


@pytest.mark.asyncio
async def test_bridge_status_payload_includes_compact_task_activity() -> None:
    engine = _FakeEngine()
    engine.background_runner = SimpleNamespace(
        list_tasks=lambda: [
            SimpleNamespace(status=BackgroundStatus.PREPARING),
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
        "background_running": 2,
        "background_attention": 1,
        "subagents_active": 1,
        "browser_active": 1,
        "permissions_pending": 1,
        "interactions_pending": 0,
        "queued_conversations": 0,
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
async def test_bridge_worktree_slash_reaches_public_engine_tool_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ui_bridge,
        "_load_cli_slash_commands_with_alias",
        lambda: ["/worktree"],
    )
    engine = _FacadeSlashEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record({
        "id": "worktree-facade-1",
        "type": ClientEventType.SUBMIT,
        "payload": {"text": "/worktree status demo"},
    })

    assert engine.tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "worktree_status"
    assert json.loads(tool_call.arguments) == {"name": "demo"}
    notices = [
        record["payload"]
        for record in _records(writer)
        if record["type"] == "ui/message"
        and record["payload"].get("type") == "system_notice"
    ]
    assert notices
    assert "Worktree 状态已通过公共 facade 获取" in notices[-1]["content"]


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
async def test_bridge_emits_typed_permission_snapshot() -> None:
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
        if record["type"] == "permissions/snapshot"
    )
    assert message["schema_version"] == 1
    assert message["pending"][0]["request_id"] == "perm-1"
    assert message["pending"][0]["tool_name"] == "bash_run"
    assert message["pending"][0]["policy"]["source"] == "TOOL_PERMISSIONS:bash_run"
    assert message["history"][0]["request_id"] == "hist-1"
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
async def test_bridge_passes_explicit_event_sink_to_engine() -> None:
    class SinkRequiredEngine(_FakeEngine):
        async def run_streaming(
            self,
            task: str,
            event_sink: EventSink,
        ) -> AgentResult:
            assert isinstance(event_sink, EventSink)
            publisher = RuntimeEventPublisher(
                event_sink,
                session_id="session-ui",
                run_id="run-ui",
            )
            await publisher.publish(RuntimeEventType.RESPONSE_START, {})
            await publisher.publish(RuntimeEventType.TOKEN, {"content": task})
            await publisher.publish(RuntimeEventType.RESPONSE_END, {})
            return AgentResult(status="completed", response=task, usage=self.usage)

    engine = SinkRequiredEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("类型化 UI", request_id="sink-ui")
    assert bridge._run_task is not None
    await bridge._run_task

    messages = [
        record["payload"]
        for record in _records(writer)
        if record["type"] == "ui/message"
    ]
    assert any(message.get("type") == "assistant_stream" for message in messages)


@pytest.mark.asyncio
async def test_bridge_emits_completion_receipt_before_correlated_run_completion() -> None:
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "receipt-bridge",
            "run_id": "run-bridge",
            "outcome": "partial",
            "summary": "验证失败，已保留改动证据。",
            "validations": [
                {
                    "command": "pytest -q",
                    "scope": "tests",
                    "status": "failed",
                    "exit_code": 1,
                    "failed": 1,
                }
            ],
            "risks": [
                {
                    "code": "validation_failed",
                    "level": "high",
                    "message": "1 项验证失败。",
                }
            ],
            "git_state": {"available": True, "branch": "main", "dirty": True},
        }
    )

    class ReceiptEngine(_FakeEngine):
        async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
            on_event = _fake_event_callback(on_event, run_id=receipt.run_id)
            await on_event("run_started", {"task": task, "run_id": receipt.run_id})
            await on_event("completion_receipt", receipt.to_dict())
            return AgentResult(
                status="completed",
                response="完成",
                usage=self.usage,
                receipt=receipt,
            )

    writer = io.StringIO()
    bridge = JsonlEngineBridge(ReceiptEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("执行验证", request_id="submit-receipt")
    assert bridge._run_task is not None
    await bridge._run_task

    records = _records(writer)
    receipt_record = next(
        record for record in records if record["type"] == "completion/receipt"
    )
    completed_record = next(
        record for record in records if record["type"] == "run/completed"
    )
    assert records.index(receipt_record) < records.index(completed_record)
    assert receipt_record["payload"] == json.loads(json.dumps(receipt.to_dict()))
    assert completed_record["payload"]["receipt_id"] == receipt.receipt_id
    assert completed_record["payload"]["run_id"] == receipt.run_id


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
async def test_bridge_workbench_request_returns_current_read_only_snapshot() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    service = _RevisionedWorkbenchService([
        {
            "schema_version": 1,
            "stream_id": "stream-a",
            "revision": 4,
            "generated_at": "2026-07-17T12:00:00+08:00",
            "full": True,
            "session_id": "session-task",
            "counts": {"tasks": 2, "worktrees": 1, "reviews": 1},
            "active_selection": {"task_id": "2"},
            "missions": [],
            "tasks": [],
            "issues": [],
            "failures": [],
            "events": [],
        }
    ])
    engine.workbench_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "workbench-read",
            "type": ClientEventType.WORKBENCH_REQUEST,
            "payload": {
                "session_id": "session-task",
                "known_stream_id": "stream-a",
                "known_revision": 3,
            },
        }
    )

    records = _records(writer)
    snapshot = next(record for record in records if record["type"] == "workbench/snapshot")
    assert snapshot["request_id"] == "workbench-read"
    assert snapshot["payload"]["revision"] == 4
    assert snapshot["payload"]["counts"]["worktrees"] == 1
    assert service.calls == ["session-task"]
    assert bridge._run_task is None


@pytest.mark.asyncio
async def test_bridge_workbench_review_returns_current_read_only_evidence() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    evidence = {
        "approval": {"id": "approval-1", "state": "waiting"},
        "issue": None,
        "worktree": {"name": "wt", "path": "/tmp/wt", "status": "present"},
        "validation_runs": [],
        "changed_files": [{"path": "README.md", "status": "modified"}],
        "diff_hunks": [{"path": "README.md", "patch": "-old\n+new"}],
        "agent_notes": [],
        "events": [],
    }
    service = _RevisionedWorkbenchService(review_evidence=evidence)
    engine.workbench_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "workbench-review",
            "type": ClientEventType.WORKBENCH_REVIEW_REQUEST,
            "payload": {"session_id": "session-task", "review_id": "approval-1"},
        }
    )

    record = next(
        item for item in _records(writer) if item["type"] == "workbench/review"
    )
    assert record["request_id"] == "workbench-review"
    assert record["payload"]["status"] == "ready"
    assert record["payload"]["evidence"]["diff_hunks"][0]["patch"] == "-old\n+new"
    assert service.review_calls == [("session-task", "approval-1")]
    assert bridge._run_task is None


@pytest.mark.asyncio
async def test_bridge_workbench_review_reports_missing_without_error_details() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    engine.workbench_service = _RevisionedWorkbenchService(review_evidence=None)
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "workbench-review-missing",
            "type": ClientEventType.WORKBENCH_REVIEW_REQUEST,
            "payload": {"review_id": "missing"},
        }
    )

    record = next(
        item for item in _records(writer) if item["type"] == "workbench/review"
    )
    assert record["payload"] == {
        "schema_version": 1,
        "session_id": "session-task",
        "review_id": "missing",
        "status": "unavailable",
        "code": "review_not_found",
    }


@pytest.mark.asyncio
async def test_bridge_proposal_action_requires_confirmation_then_audits_decision() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    engine._permission_checker = PermissionChecker(
        PermissionMode.MODERATE,
        workspace_root=str(Path.cwd()),
    )
    service = _ProposalActionWorkbenchService()
    engine.workbench_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    base = {
        "session_id": "session-task",
        "proposal_id": "proposal-1",
        "action": "reject",
        "decision_note": "证据不足",
    }
    await bridge.handle_client_record(
        {
            "id": "proposal-reject-preview",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {**base, "confirmed": False},
        }
    )
    assert service.governed == []
    preview = [
        item
        for item in _records(writer)
        if item["type"] == "workbench/proposal/action_result"
    ][-1]
    assert preview["payload"]["status"] == "needs_confirmation"

    await bridge.handle_client_record(
        {
            "id": "proposal-reject-confirm",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {**base, "confirmed": True},
        }
    )
    completed = [
        item
        for item in _records(writer)
        if item["type"] == "workbench/proposal/action_result"
    ][-1]
    assert completed["payload"]["status"] == "completed"
    assert completed["payload"]["proposal"]["state"] == "rejected"
    assert service.governed[0]["reviewer"] == "Human"
    assert service.governed[0]["decision_note"] == "证据不足"
    assert service.governed[0]["action"].value == "reject"


@pytest.mark.asyncio
async def test_bridge_bypass_executes_proposal_action_without_second_confirmation() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    engine.permission_mode = PermissionMode.BYPASS
    engine._permission_checker = PermissionChecker(
        PermissionMode.BYPASS,
        workspace_root=str(Path.cwd()),
    )
    service = _ProposalActionWorkbenchService()
    engine.workbench_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "proposal-approve-bypass",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {
                "session_id": "session-task",
                "proposal_id": "proposal-1",
                "action": "approve",
                "decision_note": "",
                "confirmed": False,
            },
        }
    )

    result = next(
        item
        for item in _records(writer)
        if item["type"] == "workbench/proposal/action_result"
    )
    assert result["payload"]["status"] == "completed"
    assert len(service.governed) == 1


@pytest.mark.asyncio
async def test_bridge_proposal_action_rejects_cross_session_write() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    service = _ProposalActionWorkbenchService()
    engine.workbench_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "proposal-cross-session",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {
                "session_id": "another-session",
                "proposal_id": "proposal-1",
                "action": "approve",
                "decision_note": "",
                "confirmed": True,
            },
        }
    )

    error = next(item for item in _records(writer) if item["type"] == "error")
    assert error["payload"]["code"] == "workbench_session_mismatch"
    assert service.governed == []


@pytest.mark.asyncio
async def test_real_sqlite_bridge_proposal_action_persists_state_and_audit(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
            )
        )
    )
    engine.set_runtime_mode("bypass")
    session = await engine.get_or_create_session("Proposal action real chain")
    mission = await engine.workbench_service.create_mission(
        session_id=session.id,
        title="治理 Proposal",
        goal="验证 Bridge 到 SQLite 的真实写入链路",
    )
    issue = await engine.workbench_service.create_issue(
        session_id=session.id,
        mission_id=mission.id,
        title="审查 Harness 策略",
        description="只改变 Proposal 状态",
    )
    proposal = await engine.workbench_service.create_proposal(
        session_id=session.id,
        mission_id=mission.id,
        task_id=str(issue["task_id"]),
        agent_id="Harness-Agent",
        title="收紧无证据通过规则",
        impact_scope="Harness judge policy",
        intended_files=["src/naumi_agent/harness/judge.py"],
        validation_plan=["运行 Harness judge 模块测试"],
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "proposal-real-approve",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {
                "session_id": session.id,
                "proposal_id": proposal["id"],
                "action": "approve",
                "decision_note": "",
                "confirmed": False,
            },
        }
    )

    records = _records(writer)
    result = next(
        item
        for item in records
        if item["type"] == "workbench/proposal/action_result"
    )
    persisted = await engine.workbench_service.get_proposal(session.id, proposal["id"])
    events = await engine.workbench_service.list_events(session.id)

    assert result["payload"]["status"] == "completed"
    assert result["payload"]["workbench_snapshot"]["counts"]["reviews"] == 0
    assert persisted is not None
    assert persisted["state"] == "approved"
    assert persisted["reviewer"] == "Human"
    assert any(
        event["type"] == "proposal.approved" and event["subject_id"] == proposal["id"]
        for event in events["events"]
    )


@pytest.mark.asyncio
async def test_bridge_workbench_request_rejects_cross_session_and_redacts_failures() -> None:
    engine = _TaskSubmitFakeEngine()
    engine._session = SimpleNamespace(id="session-task")
    service = _RevisionedWorkbenchService()
    engine.workbench_service = service
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "workbench-cross-session",
            "type": ClientEventType.WORKBENCH_REQUEST,
            "payload": {"session_id": "other-session"},
        }
    )
    await bridge.handle_client_record(
        {
            "id": "workbench-failure",
            "type": ClientEventType.WORKBENCH_REQUEST,
            "payload": {"session_id": "session-task"},
        }
    )

    errors = [record for record in _records(writer) if record["type"] == "error"]
    assert [record["payload"]["code"] for record in errors] == [
        "workbench_session_mismatch",
        "workbench_snapshot_failed",
    ]
    assert service.calls == ["session-task"]
    assert "PRIVATE_WORKBENCH_FAILURE" not in json.dumps(errors, ensure_ascii=False)


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
    completed = next(
        record for record in _records(writer) if record["type"] == "run/completed"
    )
    assert completed["request_id"] == "submit-failed"
    assert completed["payload"] == {
        "status": "failed",
        "response": "",
        "error": error["payload"]["message"],
    }


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
    engine.execute_tool = execute_tool  # type: ignore[method-assign]

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


@pytest.mark.asyncio
async def test_bridge_bypass_response_switches_mode_and_allows_current_request() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    permission_task = asyncio.create_task(
        bridge.confirm_permission(_permission_payload("call-bypass"))
    )
    await asyncio.sleep(0)

    await bridge.handle_client_record({
        "id": "response-bypass",
        "type": ClientEventType.PERMISSION_RESPONSE,
        "payload": {"request_id": "call-bypass", "choice": "bypass"},
    })

    assert await asyncio.wait_for(permission_task, timeout=1) == "allow_once"
    assert engine.runtime_mode is AgentRuntimeMode.BYPASS
    assert engine.permission_mode is PermissionMode.BYPASS
    records = _records(writer)
    assert any(record["type"] == "mode/changed" for record in records)
    resolved = next(record for record in records if record["type"] == "permission/resolved")
    assert resolved["payload"] == {
        "request_id": "call-bypass",
        "choice": "bypass",
        "status": "bypass_enabled",
    }


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
async def test_bridge_resolves_high_risk_permission_with_one_confirmation() -> None:
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

    assert await task == "allow_once"
    assert engine.runtime_mode == AgentRuntimeMode.DEFAULT
    assert any(record["type"] == "permission/resolved" for record in _records(writer))


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
async def test_bridge_legacy_high_risk_flag_still_requires_complete_choices(
    choices: list[str],
) -> None:
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
    assert records[-1]["payload"]["code"] == "permission_choices_medium_risk_unusable"
    assert "后端权限选择" in records[-1]["payload"]["message"]
    assert not any(record["type"] == "permission/request" for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choices", "published", "resolution"),
    [
        (["allow_once"], False, "deny"),
        (["deny"], False, "deny"),
        (["grant_session"], False, "deny"),
        (["allow_once", "deny"], True, "allow_once"),
        (["allow_once", "grant_session"], False, "deny"),
        (["deny", "grant_session"], False, "deny"),
        (["allow_once", "deny", "grant_session"], True, "deny"),
    ],
)
async def test_bridge_only_publishes_semantically_complete_medium_backend_choices(
    choices: list[str],
    published: bool,
    resolution: str,
) -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    task = asyncio.create_task(
        bridge.confirm_permission(
            _permission_payload("call-medium-policy", choices=choices)
        )
    )
    await asyncio.sleep(0)

    records = _records(writer)
    requests = [record for record in records if record["type"] == "permission/request"]
    if not published:
        assert task.done()
        assert await task == "deny"
        assert bridge._pending_permissions == {}
        assert not requests
        assert records[-1]["payload"]["code"] == "permission_choices_medium_risk_unusable"
        assert "后端权限选择" in records[-1]["payload"]["message"]
        return

    assert len(requests) == 1
    assert {"allow_once", "deny"}.issubset(requests[0]["payload"]["choices"])
    await bridge.resolve_permission(
        {"request_id": "call-medium-policy", "choice": resolution}, request_id="response"
    )
    assert await task == resolution


@pytest.mark.asyncio
async def test_bridge_legacy_high_risk_flag_does_not_add_a_second_confirmation() -> None:
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
    assert request["payload"]["choices"] == ["grant_session", "allow_once", "deny"]
    assert request["payload"]["requires_double_confirm"] is False

    await bridge.resolve_permission(
        {"request_id": "call-high-valid", "choice": "allow_once"}, request_id="response"
    )
    assert await task == "allow_once"


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
async def test_bridge_shutdown_denies_each_pending_permission() -> None:
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
    await bridge.shutdown()

    assert await high_risk == "deny"
    assert await medium_risk == "deny"
    assert bridge._pending_permissions == {}


@pytest.mark.asyncio
async def test_bridge_denies_permission_requested_after_shutdown_without_publication() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    await bridge.shutdown()

    late_permission = asyncio.create_task(
        bridge.confirm_permission(_permission_payload("late-after-shutdown"))
    )
    await asyncio.sleep(0)
    try:
        assert late_permission.done()
        assert await late_permission == "deny"
        assert bridge._pending_permissions == {}
        assert not any(
            record["type"] == "permission/request" for record in _records(writer)
        )
    finally:
        if not late_permission.done():
            late_permission.cancel()
            await asyncio.gather(late_permission, return_exceptions=True)


@pytest.mark.asyncio
async def test_bridge_denies_permission_arriving_after_shutdown_cleanup() -> None:
    engine = _BlockingShutdownEngine()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    writer = io.StringIO()
    bridge.bind_writer(writer)
    shutdown_task = asyncio.create_task(bridge.shutdown())
    await engine.shutdown_started.wait()

    late_permission = asyncio.create_task(
        bridge.confirm_permission(_permission_payload("late-during-shutdown"))
    )
    await asyncio.sleep(0)
    try:
        assert bridge._closed is True
        assert late_permission.done()
        assert await late_permission == "deny"
        assert bridge._pending_permissions == {}
        assert not any(
            record["type"] == "permission/request" for record in _records(writer)
        )
    finally:
        if not late_permission.done():
            late_permission.cancel()
            await asyncio.gather(late_permission, return_exceptions=True)
        engine.release_shutdown.set()
        await shutdown_task


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
async def test_bridge_resume_replays_durable_completion_receipts(tmp_path: Path) -> None:
    engine = _FakeEngine()
    engine.chat_run_store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await engine.chat_run_store.start_run(
        session_id="session-1",
        user_message_id="msg-old",
        run_id="run-old",
    )
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "receipt-old",
            "run_id": run.id,
            "outcome": "completed",
            "summary": "历史运行已完成。",
            "git_state": {"available": False, "dirty": False},
        }
    )
    await engine.chat_run_store.finish_run(
        run.id,
        status="completed",
        receipt=receipt,
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "resume-receipt", "type": ClientEventType.RESUME, "payload": {}}
    )

    records = _records(writer)
    replayed = [
        record
        for record in records
        if record["type"] == "completion/receipt"
    ]
    assert [record["payload"] for record in replayed] == [
        json.loads(json.dumps(receipt.to_dict()))
    ]
    assert replayed[0]["request_id"] == "resume-receipt"


@pytest.mark.asyncio
async def test_bridge_resume_replays_harness_receipt_before_generic_receipt(
    tmp_path: Path,
) -> None:
    engine = _FakeEngine()
    engine.chat_run_store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await engine.chat_run_store.start_run(
        session_id="session-1",
        user_message_id="msg-harness-resume",
        run_id="run-harness-resume",
    )
    generic_receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "receipt-harness-resume",
            "run_id": run.id,
            "outcome": "partial",
            "summary": "历史运行完成但未完全验证。",
            "git_state": {"available": False, "dirty": False},
        }
    )
    await engine.chat_run_store.finish_run(
        run.id,
        status="completed_unverified",
        receipt=generic_receipt,
    )
    harness_receipt = HarnessCompletionReceipt(
        run_id=run.id,
        status="completed_unverified",
        task_kind=HarnessTaskKind.CHANGE,
        changed_files=("src/app.py",),
        checks=(),
        criteria=(),
        warnings=("尚未运行受信检查",),
        tree_fingerprint="a" * 64,
    )

    class ResumeHarnessStore:
        def __init__(self) -> None:
            self.calls: list[tuple[Path, str, int]] = []

        async def list_session_runs(
            self,
            workspace_root: Path,
            session_id: str,
            *,
            limit: int,
        ) -> tuple[SimpleNamespace, ...]:
            self.calls.append((workspace_root, session_id, limit))
            return (
                SimpleNamespace(id="run-incomplete", receipt=None),
                SimpleNamespace(id=run.id, receipt=harness_receipt),
            )

    harness_store = ResumeHarnessStore()
    engine.harness_service = SimpleNamespace(store=harness_store)
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.resume_session({}, request_id="resume-harness")

    records = _records(writer)
    receipt_records = [
        record
        for record in records
        if record["type"] in {"harness/receipt", "completion/receipt"}
    ]
    assert [record["type"] for record in receipt_records] == [
        "harness/receipt",
        "completion/receipt",
    ]
    assert receipt_records[0]["request_id"] == "resume-harness"
    assert receipt_records[0]["payload"] == {
        **harness_receipt.model_dump(mode="json"),
        "schema_version": 1,
        "revision": 1,
    }
    assert harness_store.calls == [(engine.workspace_root, "session-1", 200)]


@pytest.mark.asyncio
async def test_bridge_resume_preserves_generic_receipts_when_harness_recovery_fails(
    tmp_path: Path,
) -> None:
    engine = _FakeEngine()
    engine.chat_run_store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await engine.chat_run_store.start_run(
        session_id="session-1",
        user_message_id="msg-harness-failure",
        run_id="run-harness-failure",
    )
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "receipt-harness-failure",
            "run_id": run.id,
            "outcome": "completed",
            "summary": "通用回执仍应恢复。",
            "git_state": {"available": False, "dirty": False},
        }
    )
    await engine.chat_run_store.finish_run(run.id, status="completed", receipt=receipt)

    class BrokenHarnessStore:
        async def list_session_runs(self, *_: Any, **__: Any) -> tuple[Any, ...]:
            raise RuntimeError("PRIVATE_DATABASE_DETAIL")

    engine.harness_service = SimpleNamespace(store=BrokenHarnessStore())
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.resume_session({}, request_id="resume-harness-failure")

    records = _records(writer)
    assert any(record["type"] == "completion/receipt" for record in records)
    failure = next(
        record
        for record in records
        if record["type"] == "error"
        and record["payload"].get("code") == "harness_receipt_recovery_failed"
    )
    assert failure["request_id"] == "resume-harness-failure"
    assert "Harness 回执恢复失败" in failure["payload"]["message"]
    assert "PRIVATE_DATABASE_DETAIL" not in json.dumps(failure, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bridge_resends_requested_completion_receipt(tmp_path: Path) -> None:
    engine = _FakeEngine()
    engine.chat_run_store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await engine.chat_run_store.start_run(
        session_id="session-1",
        user_message_id="msg-resend",
        run_id="run-resend",
    )
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "receipt-resend",
            "run_id": run.id,
            "outcome": "completed",
            "summary": "补发成功。",
            "git_state": {"available": False, "dirty": False},
        }
    )
    await engine.chat_run_store.finish_run(
        run.id,
        status="completed",
        receipt=receipt,
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "request-receipt",
            "type": ClientEventType.RECEIPT_REQUEST,
            "payload": {
                "session_id": "session-1",
                "receipt_id": receipt.receipt_id,
                "run_id": run.id,
            },
        }
    )

    records = _records(writer)
    resent = next(record for record in records if record["type"] == "completion/receipt")
    assert resent["request_id"] == "request-receipt"
    assert resent["payload"]["receipt_id"] == receipt.receipt_id

    writer.seek(0)
    writer.truncate(0)
    await bridge.handle_client_record(
        {
            "id": "request-cross-session",
            "type": ClientEventType.RECEIPT_REQUEST,
            "payload": {
                "session_id": "session-other",
                "receipt_id": receipt.receipt_id,
            },
        }
    )
    rejected = _records(writer)
    assert not any(record["type"] == "completion/receipt" for record in rejected)
    assert rejected[-1]["payload"]["code"] == "receipt_not_found"


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
async def test_bridge_queues_second_submit_and_runs_it_after_active_turn() -> None:
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
    queued = next(
        record
        for record in records
        if record["type"] == "run/queued"
        and record.get("request_id") == "submit-second"
    )
    assert queued["payload"] == {"task": "第二条", "position": 1, "queued": 1}
    assert any(
        record["type"] == "user/message"
        and record.get("request_id") == "submit-second"
        for record in records
    )
    assert bridge.status_payload()["tasks"]["queued_conversations"] == 1
    assert engine.run_tasks == ["长任务"]

    engine.release_run.set()
    await bridge._run_task
    await asyncio.sleep(0)

    assert engine.run_tasks == ["长任务", "第二条"]
    assert bridge.status_payload()["tasks"]["queued_conversations"] == 0
    assert any(
        record["type"] == "run/started"
        and record.get("request_id") == "submit-second"
        for record in _records(writer)
    )


@pytest.mark.asyncio
async def test_bridge_rejects_submit_when_chat_queue_is_full() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("active", request_id="submit-active")
    await asyncio.sleep(0)
    for index in range(20):
        await bridge.submit(f"queued-{index}", request_id=f"submit-{index}")

    await bridge.submit("overflow", request_id="submit-overflow")

    records = _records(writer)
    rejection = next(
        record
        for record in records
        if record["type"] == "error"
        and record.get("request_id") == "submit-overflow"
    )
    assert rejection["payload"]["code"] == "queue_full"
    assert "20" in rejection["payload"]["message"]
    assert not any(
        record["type"] == "user/message"
        and record.get("request_id") == "submit-overflow"
        for record in records
    )

    await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_refreshes_remaining_chat_queue_positions_after_advancing() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("active", request_id="submit-active")
    await asyncio.sleep(0)
    first_run = bridge._run_task
    await bridge.submit("next", request_id="submit-next")
    await bridge.submit("later", request_id="submit-later")
    engine.release_run.set()
    assert first_run is not None
    await first_run

    later_positions = [
        record["payload"]["position"]
        for record in _records(writer)
        if record["type"] == "run/queued"
        and record.get("request_id") == "submit-later"
    ]
    assert later_positions == [2, 1]

    await asyncio.sleep(0)
    await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_advances_queued_chat_after_active_run_failure() -> None:
    class _FailFirstEngine(_FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.run_tasks: list[str] = []

        async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
            self.run_tasks.append(task)
            if len(self.run_tasks) == 1:
                self.started.set()
                await self.release_first.wait()
                raise RuntimeError("private provider payload")
            return await super().run_streaming(task, on_event)

    engine = _FailFirstEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("first", request_id="submit-first")
    await engine.started.wait()
    first_run = bridge._run_task
    await bridge.submit("second", request_id="submit-second")
    engine.release_first.set()
    assert first_run is not None
    await first_run
    await asyncio.sleep(0)
    assert bridge._run_task is not None
    await bridge._run_task

    assert engine.run_tasks == ["first", "second"]
    assert any(
        record["type"] == "run/completed"
        and record.get("request_id") == "submit-second"
        and record["payload"]["status"] == "completed"
        for record in _records(writer)
    )


@pytest.mark.asyncio
async def test_bridge_cancel_active_run_then_starts_queued_chat() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("active", request_id="submit-active")
    await asyncio.sleep(0)
    await bridge.submit("queued", request_id="submit-queued")
    await bridge.cancel_run({"reason": "切换到下一条"}, request_id="cancel-active")
    await asyncio.sleep(0)

    assert engine.run_tasks == ["active", "queued"]
    assert bridge._run_task is not None
    engine.release_run.set()
    await bridge._run_task
    records = _records(writer)
    cancelled_index = next(
        index for index, record in enumerate(records)
        if record["type"] == "run/cancelled"
    )
    queued_start_index = next(
        index for index, record in enumerate(records)
        if record["type"] == "run/started"
        and record.get("request_id") == "submit-queued"
    )
    assert cancelled_index < queued_start_index


@pytest.mark.asyncio
async def test_bridge_shutdown_cancels_chat_queue_without_starting_it() -> None:
    engine = _SlowFakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("active", request_id="submit-active")
    await asyncio.sleep(0)
    await bridge.submit("queued", request_id="submit-queued")

    await bridge.shutdown()

    assert engine.run_tasks == ["active"]
    assert bridge.status_payload()["tasks"]["queued_conversations"] == 0
    cancelled = next(
        record for record in _records(writer)
        if record["type"] == "run/cancelled"
        and record.get("request_id") == "submit-queued"
    )
    assert cancelled["payload"]["target_request_id"] == "submit-queued"
    assert "界面已关闭" in cancelled["payload"]["reason"]


@pytest.mark.asyncio
async def test_bridge_starts_queued_chat_after_workbench_task_completes() -> None:
    class _ReleasableTaskEngine(_TaskSubmitFakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.chat_tasks: list[str] = []

        async def run_streaming(
            self,
            task: str,
            on_event: Any,
            turn_context: str = "",
        ) -> AgentResult:
            if turn_context:
                self.started.set()
                await self.release.wait()
                return AgentResult(status="completed", response="任务完成", usage=self.usage)
            self.chat_tasks.append(task)
            return await _FakeEngine.run_streaming(self, task, on_event)

    engine = _ReleasableTaskEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit_task({"text": "workbench"}, request_id="task-active")
    await engine.started.wait()
    task_run = bridge._run_task
    await bridge.submit("chat-after-task", request_id="submit-chat")
    engine.release.set()
    assert task_run is not None
    await task_run
    await asyncio.sleep(0)
    assert bridge._run_task is not None
    await bridge._run_task

    assert engine.chat_tasks == ["chat-after-task"]


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
async def test_bridge_emits_typed_task_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    async def fake_build_task_panel_snapshot(
        target_engine: Any,
        *,
        limit: int = 12,
        source: str = "all",
        status: str = "all",
        detail_id: str = "",
        history: bool = False,
    ) -> Any:
        assert target_engine is engine
        assert limit == 5
        assert source == "background"
        assert status == "running"
        assert detail_id == "bg_1"
        assert history is False
        return SimpleNamespace(to_protocol_dict=lambda: {
            "schema_version": 1,
            "generated_at": "2026-07-18T00:00:00+00:00",
            "full": True,
            "filters": {
                "source": "background",
                "status": "running",
                "detail_id": "bg_1",
                "history": False,
            },
            "items": [],
            "timeline": [],
            "warnings": [],
        })

    monkeypatch.setattr(
        "naumi_agent.ui.task_panel.build_task_panel_snapshot",
        fake_build_task_panel_snapshot,
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
    snapshot = next(
        record["payload"] for record in records if record["type"] == "tasks/snapshot"
    )
    assert snapshot["schema_version"] == 1
    assert snapshot["filters"] == {
        "source": "background",
        "status": "running",
        "detail_id": "bg_1",
        "history": False,
    }

    writer.seek(0)
    writer.truncate(0)
    bridge._client_capabilities = {"typed_ui_messages"}
    monkeypatch.setattr(
        "naumi_agent.ui.task_panel.render_task_panel_snapshot",
        lambda target: "任务面板\nTodo\n  暂无任务\n",
    )
    await bridge.handle_client_record(
        {
                "id": "tasks-legacy",
                "type": ClientEventType.TASK_PANEL,
                "payload": {
                    "limit": 5,
                    "source": "background",
                    "status": "running",
                    "detail_id": "bg_1",
                },
        }
    )
    fallback = next(
        record["payload"]
        for record in _records(writer)
        if record["type"] == "ui/message"
    )
    assert fallback["title"] == "tasks"
    assert "暂无任务" in fallback["content"]
    assert any(record["type"] == "runtime/status" for record in records)


@pytest.mark.asyncio
async def test_bridge_emits_typed_goal_snapshot_and_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()
    engine.goal_store = object()
    engine.pursuit_store = object()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    bridge._client_capabilities = {"goal_snapshot", "typed_ui_messages"}
    snapshot = SimpleNamespace(
        to_protocol_dict=lambda: {
            "schema_version": 1,
            "generated_at": "2026-07-18T00:00:00+00:00",
            "full": True,
            "current_goal_id": "goal_1",
            "goals": [],
            "warnings": [],
            "truncated": False,
            "include_finished": True,
        }
    )

    build_calls: list[dict[str, Any]] = []

    async def fake_build(
        goal_store: Any,
        pursuit_store: Any,
        authority: Any,
        **kwargs: Any,
    ) -> Any:
        assert goal_store is engine.goal_store
        assert pursuit_store is engine.pursuit_store
        assert authority is None
        build_calls.append(kwargs)
        return snapshot

    monkeypatch.setattr(
        "naumi_agent.ui.goal_panel.build_goal_pursuit_snapshot_with_recovery",
        fake_build,
    )
    await bridge.handle_client_record({
        "id": "goal-open",
        "type": ClientEventType.GOAL_PANEL,
        "payload": {"limit": 7, "include_finished": False},
    })

    records = _records(writer)
    typed = next(record for record in records if record["type"] == "goals/snapshot")
    assert typed["request_id"] == "goal-open"
    assert typed["payload"]["current_goal_id"] == "goal_1"
    assert build_calls == [{
        "workspace_root": engine.workspace_root,
        "limit": 7,
        "include_finished": False,
    }]

    writer.seek(0)
    writer.truncate(0)
    bridge._client_capabilities = {"typed_ui_messages"}
    monkeypatch.setattr(
        "naumi_agent.ui.goal_panel.render_goal_pursuit_snapshot",
        lambda _: "### 持久目标\n\n当前没有未完成目标。",
    )
    await bridge.handle_client_record({
        "id": "goal-legacy",
        "type": ClientEventType.GOAL_PANEL,
        "payload": {},
    })

    fallback = next(
        record for record in _records(writer) if record["type"] == "ui/message"
    )
    assert fallback["payload"]["title"] == "goal"
    assert "当前没有未完成目标" in fallback["payload"]["content"]
    assert build_calls[-1] == {
        "workspace_root": engine.workspace_root,
        "limit": 20,
        "include_finished": True,
    }


@pytest.mark.asyncio
async def test_bridge_goal_snapshot_contains_real_recovery_authorities(tmp_path) -> None:
    engine = _FakeEngine()
    engine.workspace_root = tmp_path
    engine.goal_store = GoalStore(tmp_path / "goals")
    engine.pursuit_store = PursuitStore(tmp_path / "pursuit")
    harness_store = HarnessStore(tmp_path / "harness.db")
    engine.harness_service = SimpleNamespace(store=harness_store)
    goal = engine.goal_store.create("显示真实恢复健康")
    run = PursuitRun(
        id="pursuit-bridge-recovery",
        goal=goal.objective,
        status=PursuitRunStatus.RUNNING,
        phase="assess",
        started_at=1.0,
        updated_at=2.0,
    )
    engine.pursuit_store.save_run(run)
    engine.goal_store.attach_pursuit(goal.id, run.id)
    now = datetime.now(UTC).isoformat()
    lease = await harness_store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id=run.id,
        owner_id="worker-a",
        now=now,
        lease_seconds=86_400,
    )
    assert lease is not None
    await harness_store.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id=run.id,
        instance_id=lease.owner_id,
        epoch=lease.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=now,
        timeout_seconds=86_400,
        detail_code="lease_active",
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    bridge._client_capabilities = {"goal_snapshot", "typed_ui_messages"}

    await bridge.handle_client_record({
        "id": "goal-recovery",
        "type": ClientEventType.GOAL_PANEL,
        "payload": {"limit": 1, "include_finished": False},
    })

    record = next(
        item for item in _records(writer) if item["type"] == "goals/snapshot"
    )
    recovery = record["payload"]["goals"][0]["pursuit"]["recovery"]
    assert recovery["run_id"] == run.id
    assert recovery["recovery_state"] == "active"
    assert recovery["lease"]["owner_id"] == "worker-a"
    assert recovery["heartbeat"]["health"] == "healthy"
    assert recovery["heartbeat"]["instance_id"] == "worker-a"


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
    tmp_path: Path,
) -> None:
    engine = _FakeEngine()
    engine.workspace_root = tmp_path
    engine.goal_store = GoalStore(tmp_path / "goals")
    engine.pursuit_store = PursuitStore(tmp_path / "pursuit")
    engine.harness_service = SimpleNamespace(store=HarnessStore(tmp_path / "harness.db"))
    goal = engine.goal_store.create("诊断追踪恢复")
    run = PursuitRun(
        id="pursuit-doctor-recovery",
        goal=goal.objective,
        status=PursuitRunStatus.WAITING,
        phase="waiting",
        started_at=1.0,
        updated_at=2.0,
    )
    engine.pursuit_store.save_run(run)
    engine.goal_store.attach_pursuit(goal.id, run.id)
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    class FakeReport:
        status = "warn"
        checks = (DoctorCheck("browser daemon", "warn", "未启动"),)

    async def fake_run_doctor(
        config: Any,
        *,
        workspace_root: Path,
        mcp_manager: Any,
        model_router: Any,
    ) -> Any:
        assert workspace_root == engine.workspace_root
        assert mcp_manager is None
        assert model_router is engine.router
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
    health = next(record for record in records if record["type"] == "doctor/health")
    assert health["request_id"] == "doctor-1"
    assert health["payload"]["status"] == "degraded"
    assert health["payload"]["items"][0]["domain"] == "browser"
    recovery_item = next(
        item
        for item in health["payload"]["items"]
        if item["id"] == "runtime-pursuit-recovery"
    )
    assert recovery_item["severity"] == "ok"
    assert "安全等待" in recovery_item["detail"]
    assert any(record["type"] == "runtime/status" for record in records)


@pytest.mark.asyncio
async def test_bridge_doctor_failure_returns_typed_product_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    async def fail_doctor(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private doctor failure")

    monkeypatch.setattr("naumi_agent.ui.doctor.run_doctor", fail_doctor)
    await bridge.handle_client_record(
        {"id": "doctor-failure", "type": ClientEventType.DOCTOR, "payload": {}}
    )

    health = next(
        record for record in _records(writer) if record["type"] == "doctor/health"
    )
    item = health["payload"]["items"][0]
    assert health["payload"]["status"] == "error"
    assert item["responsibility"] == "product_runtime"
    assert "诊断流程自身失败" in item["detail"]
    assert "private doctor failure" not in json.dumps(health, ensure_ascii=False)


def _interaction_payload() -> dict[str, Any]:
    return {
        "header": "实现策略",
        "question": "请选择持久化范围",
        "options": [
            {"value": "workspace", "label": "工作区", "description": "仓库共享"},
            {"value": "session", "label": "当前会话", "description": "仅本会话"},
        ],
        "allow_custom": True,
        "custom_label": "其他方案",
    }


@pytest.mark.asyncio
async def test_bridge_suspends_interaction_until_matching_response() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    pending = asyncio.create_task(engine.user_interaction_handler(_interaction_payload()))
    await asyncio.sleep(0)
    request = next(record for record in _records(writer) if record["type"] == "interaction/request")
    assert request["payload"]["options"][0]["value"] == "workspace"
    assert pending.done() is False

    await bridge.handle_client_record(
        {
            "id": "interaction-answer-1",
            "type": "interaction_response",
            "payload": {
                "request_id": request["payload"]["request_id"],
                "kind": "option",
                "value": "session",
            },
        }
    )

    assert await pending == {
        "kind": "option",
        "value": "session",
        "label": "当前会话",
        "custom_text": "",
    }
    resolved = next(
        record for record in _records(writer)
        if record["type"] == "interaction/resolved"
    )
    assert resolved["payload"]["value"] == "session"


@pytest.mark.asyncio
async def test_bridge_rejects_invalid_interaction_without_resolving_pending() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    pending = asyncio.create_task(engine.user_interaction_handler(_interaction_payload()))
    await asyncio.sleep(0)
    request = next(record for record in _records(writer) if record["type"] == "interaction/request")

    await bridge.handle_client_record(
        {
            "id": "interaction-answer-bad",
            "type": "interaction_response",
            "payload": {
                "request_id": request["payload"]["request_id"],
                "kind": "option",
                "value": "unknown",
            },
        }
    )

    assert pending.done() is False
    error = next(record for record in _records(writer) if record["type"] == "error")
    assert error["payload"]["code"] == "interaction_response_invalid"

    await bridge.handle_client_record(
        {
            "id": "interaction-answer-good",
            "type": "interaction_response",
            "payload": {
                "request_id": request["payload"]["request_id"],
                "kind": "custom",
                "custom_text": "由配置决定",
            },
        }
    )
    assert (await pending)["custom_text"] == "由配置决定"


@pytest.mark.asyncio
async def test_bridge_keeps_parallel_interaction_responses_isolated() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    first = asyncio.create_task(engine.user_interaction_handler(_interaction_payload()))
    second = asyncio.create_task(engine.user_interaction_handler({
        **_interaction_payload(),
        "question": "第二个问题",
    }))
    await asyncio.sleep(0)
    requests = [record for record in _records(writer) if record["type"] == "interaction/request"]
    assert len(requests) == 2
    first_request = next(
        record for record in requests if record["payload"]["question"] == "请选择持久化范围"
    )
    second_request = next(
        record for record in requests if record["payload"]["question"] == "第二个问题"
    )

    await bridge.handle_client_record({
        "id": "answer-second",
        "type": "interaction_response",
        "payload": {
            "request_id": second_request["payload"]["request_id"],
            "kind": "option",
            "value": "session",
        },
    })
    assert (await asyncio.wait_for(second, timeout=2))["value"] == "session"
    assert first.done() is False

    await bridge.handle_client_record({
        "id": "answer-first",
        "type": "interaction_response",
        "payload": {
            "request_id": first_request["payload"]["request_id"],
            "kind": "option",
            "value": "workspace",
        },
    })
    assert (await asyncio.wait_for(first, timeout=2))["value"] == "workspace"


@pytest.mark.asyncio
async def test_bridge_shutdown_releases_pending_interaction() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    pending = asyncio.create_task(engine.user_interaction_handler(_interaction_payload()))
    await asyncio.sleep(0)
    await bridge.shutdown()

    with pytest.raises(UserInteractionUnavailableError, match="界面已关闭"):
        await pending
