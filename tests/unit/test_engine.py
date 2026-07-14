"""AgentEngine 核心逻辑单元测试."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.agents.base import AgentResult
from naumi_agent.agents.team_protocol import execute_team_signal
from naumi_agent.config.settings import AppConfig, MemoryConfig, ModelConfig, SafetyConfig
from naumi_agent.hooks import HookContext, HookPoint
from naumi_agent.memory.session import Session
from naumi_agent.model.router import ModelResponse, ModelTier, StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import (
    AgentEngine,
    AgentRuntimeMode,
    _summarize_tool_prepare_snapshot,
)
from naumi_agent.orchestrator.planner import Complexity, ExecutionMode, Plan, Step
from naumi_agent.orchestrator.subagent_manager import SubTask
from naumi_agent.safety.budget import TokenBudget
from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionDecision,
    PermissionMode,
    PermissionOutcome,
    PermissionReasonCode,
)
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tools.base import Tool, ToolCall, ToolMetadata, ToolResult
from naumi_agent.tools.builtin import BashRunTool


class FakeTool(Tool):
    @property
    def name(self) -> str:
        return "fake_tool"

    @property
    def description(self) -> str:
        return "测试工具"

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: object) -> str:
        return "ok"


class CoordinatedSafeTool(Tool):
    def __init__(
        self,
        name: str,
        entered: set[str],
        both_entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._name = name
        self._entered = entered
        self._both_entered = both_entered
        self._release = release

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(read_only=True, concurrency_safe=True)

    async def execute(self, **kwargs: object) -> str:
        self._entered.add(self._name)
        if len(self._entered) == 2:
            self._both_entered.set()
        await self._release.wait()
        return self._name


def _usage() -> TokenUsage:
    return TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)


def test_agent_engine_loads_model_catalog(tmp_path: Path) -> None:
    catalog_path = tmp_path / "providers.json"
    catalog_path.write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "apiFormat": "openai_chat",
                        "baseURL": "http://127.0.0.1:8000/v1",
                        "models": {
                            "chat": {
                                "upstreamId": "upstream-chat",
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    engine = AgentEngine(
        AppConfig(
            models=ModelConfig(provider="local", catalog_path=str(catalog_path)),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
                long_term_enabled=False,
            ),
        )
    )

    target = engine.router.resolve_target("chat")

    assert target.canonical_model == "local/chat"
    assert target.upstream_model == "upstream-chat"


def test_agent_engine_without_model_catalog_keeps_legacy_resolution(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
                long_term_enabled=False,
            )
        )
    )

    target = engine.router.resolve_target("legacy")

    assert target.canonical_model == "legacy"
    assert target.upstream_model == "legacy"


def _two_safe_tool_response() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=[
            {
                "id": "safe-a-call",
                "function": {"name": "safe_a", "arguments": "{}"},
            },
            {
                "id": "safe-b-call",
                "function": {"name": "safe_b", "arguments": "{}"},
            },
        ],
        usage=_usage(),
        model="test-model",
    )


@pytest.mark.asyncio
async def test_shutdown_continues_after_browser_cleanup_failure(tmp_path: Path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )
    engine._browser_session.stop = AsyncMock(
        side_effect=RuntimeError("browser cleanup failed")
    )
    mcp_manager = MagicMock()
    mcp_manager.disconnect_all = AsyncMock()
    engine._mcp_manager = mcp_manager
    engine.session_store.close = AsyncMock()

    await engine.shutdown()

    mcp_manager.disconnect_all.assert_awaited_once()
    engine.session_store.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_react_loop_executes_safe_tool_calls_concurrently(tmp_path) -> None:
    engine = AgentEngine(AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        safety=SafetyConfig(max_parallel_tools=2),
    ))
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()
    engine.tool_registry.register(
        CoordinatedSafeTool("safe_a", entered, both_entered, release)
    )
    engine.tool_registry.register(
        CoordinatedSafeTool("safe_b", entered, both_entered, release)
    )
    responses = [
        _two_safe_tool_response(),
        ModelResponse(content="完成", usage=_usage(), model="test-model"),
    ]
    run_task: asyncio.Task[object] | None = None

    try:
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            run_task = asyncio.create_task(
                engine._react_loop(engine.tool_registry.get_openai_tools())
            )
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            release.set()
            result = await run_task

        assert result.response == "完成"
        tool_messages = [
            message for message in engine._messages if message.get("role") == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "safe-a-call",
            "safe-b-call",
        ]
    finally:
        release.set()
        if run_task is not None and not run_task.done():
            await run_task
        await engine.shutdown()


@pytest.mark.asyncio
async def test_streaming_parallel_tools_emit_batch_metadata(tmp_path) -> None:
    engine = AgentEngine(AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        safety=SafetyConfig(max_parallel_tools=2),
    ))
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()
    engine.tool_registry.register(
        CoordinatedSafeTool("safe_a", entered, both_entered, release)
    )
    engine.tool_registry.register(
        CoordinatedSafeTool("safe_b", entered, both_entered, release)
    )
    events: list[tuple[str, dict[str, object]]] = []
    call_count = 0

    async def stream_response(**_: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamChunk(
                tool_call={
                    index: call
                    for index, call in enumerate(_two_safe_tool_response().tool_calls)
                },
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(token="完成")
        yield StreamChunk(finish_reason="stop")

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    run_task: asyncio.Task[object] | None = None
    try:
        with patch.object(engine._router, "stream", new=stream_response):
            run_task = asyncio.create_task(
                engine._react_loop_streaming(
                    engine.tool_registry.get_openai_tools(),
                    on_event,
                )
            )
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            release.set()
            await run_task

        tool_events = [
            data for event, data in events if event in {"tool_start", "tool_end"}
        ]
        assert tool_events
        assert all(data["batch_size"] == 2 for data in tool_events)
        assert all(data["parallel"] is True for data in tool_events)
        assert {data["batch_id"] for data in tool_events} == {"turn-1-batch-1"}
    finally:
        release.set()
        if run_task is not None and not run_task.done():
            await run_task
        await engine.shutdown()


@pytest.mark.asyncio
async def test_react_loop_requires_todo_reconciliation_before_final(tmp_path) -> None:
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
    )
    engine = AgentEngine(config)
    engine.task_store.set_session("todo-reconcile")
    task = await engine.task_store.create_task("实现后端")
    await engine.task_store.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    responses = [
        ModelResponse(content="过早结束", usage=_usage(), model="test-model"),
        ModelResponse(
            content="",
            tool_calls=[{
                "id": "update-1",
                "function": {
                    "name": "task_update",
                    "arguments": json.dumps({"task_id": task.id, "status": "completed"}),
                },
            }],
            usage=_usage(),
            model="test-model",
        ),
        ModelResponse(content="状态已对账", usage=_usage(), model="test-model"),
    ]

    try:
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        stored = await engine.task_store.get_task(task.id)
        assert result.response == "状态已对账"
        assert stored is not None
        assert stored.status == TaskStatus.COMPLETED
        assert not any(message.get("content") == "过早结束" for message in engine._messages)
        assert any(
            "最终回答前必须对账 Todo" in str(message.get("content", ""))
            for message in engine._messages
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_streaming_reconciliation_hides_premature_final_text(tmp_path) -> None:
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
    )
    engine = AgentEngine(config)
    engine.task_store.set_session("todo-stream-reconcile")
    task = await engine.task_store.create_task("实现流式对账")
    await engine.task_store.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    events: list[tuple[str, dict[str, object]]] = []
    call_count = 0

    async def stream_response(**_: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamChunk(token="这是一段绝对不能展示给用户的过早最终回答。")
            yield StreamChunk(finish_reason="stop")
            return
        yield StreamChunk(token="已将未对账任务标记为阻塞。")
        yield StreamChunk(finish_reason="stop")

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    try:
        with patch.object(engine._router, "stream", new=stream_response):
            result = await engine._react_loop_streaming(
                engine.tool_registry.get_openai_tools(),
                on_event,
            )

        token_text = "".join(
            str(data.get("content", ""))
            for event, data in events
            if event == "token"
        )
        stored = await engine.task_store.get_task(task.id)
        assert result.response == "已将未对账任务标记为阻塞。"
        assert "过早最终回答" not in token_text
        assert token_text == "已将未对账任务标记为阻塞。"
        assert stored is not None
        assert stored.status == TaskStatus.BLOCKED
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_todo_reconciliation_blocks_active_task_when_turns_exhausted(tmp_path) -> None:
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        safety=SafetyConfig(max_turns=1),
    )
    engine = AgentEngine(config)
    engine.task_store.set_session("todo-max-turns")
    task = await engine.task_store.create_task("完成收尾")
    await engine.task_store.update_task(task.id, status=TaskStatus.IN_PROGRESS)

    try:
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=ModelResponse(
                content="未对账的最终回答",
                usage=_usage(),
                model="test-model",
            ),
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        stored = await engine.task_store.get_task(task.id)
        assert result.status == "max_turns"
        assert stored is not None
        assert stored.status == TaskStatus.BLOCKED
        assert stored.active_form == "Agent 结束前未完成状态对账"
    finally:
        await engine.shutdown()


@pytest.fixture
def engine(request: pytest.FixtureRequest) -> AgentEngine:
    config = AppConfig()
    instance = AgentEngine(config)

    def cleanup() -> None:
        asyncio.run(instance.shutdown())

    request.addfinalizer(cleanup)
    return instance


@pytest.fixture
def mock_router() -> MagicMock:
    router = MagicMock()
    router.resolve_model.return_value = "test-model"
    router.get_context_window.return_value = 200_000
    router.get_max_output.return_value = 4_096
    return router


@pytest.mark.asyncio
async def test_engine_registers_safe_workbench_tools(tmp_path) -> None:
    from naumi_agent.config.settings import AppConfig, MemoryConfig
    from naumi_agent.orchestrator.engine import AgentEngine

    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )
    try:
        names = set(engine.tool_registry.names)
        assert {
            "workbench_snapshot",
            "workbench_propose_issue",
        }.issubset(names)
    finally:
        await engine.shutdown()


class TestEngineInit:
    def test_creates_with_default_config(self) -> None:
        engine = AgentEngine(AppConfig())
        assert len(engine.tool_registry) > 0
        assert engine.router is not None

    def test_has_builtin_tools(self, engine: AgentEngine) -> None:
        names = engine.tool_registry.names
        assert "glob" in names
        assert "grep" in names
        assert "read" in names
        assert "file_read" in names
        assert "file_write" in names
        assert "file_edit" in names
        assert "bash_run" in names

    @pytest.mark.asyncio
    async def test_bash_output_dir_stays_readable_inside_workspace(self, tmp_path) -> None:
        state_dir = tmp_path / "runtime-data"
        engine = AgentEngine(
            AppConfig(
                workspace_root=str(tmp_path),
                memory=MemoryConfig(session_db_path=str(state_dir / "sessions.db")),
            )
        )
        try:
            tool = engine.tool_registry.get("bash_run")

            assert isinstance(tool, BashRunTool)
            assert tool.output_dir == (tmp_path / ".naumi" / "shell-output").resolve()
        finally:
            await engine.shutdown()

    def test_has_browser_tools(self, engine: AgentEngine) -> None:
        assert "browser_goto" in engine.tool_registry.names

    def test_has_memory_tools(self, engine: AgentEngine) -> None:
        assert "memory_store" in engine.tool_registry.names
        assert "memory_recall" in engine.tool_registry.names

    def test_has_subagent_tools(self, engine: AgentEngine) -> None:
        assert "delegate_task" in engine.tool_registry.names

    @pytest.mark.asyncio
    async def test_registered_tools_have_permission_rules(self) -> None:
        engine = AgentEngine(AppConfig())
        checker = PermissionChecker(PermissionMode.MODERATE, allowed_dirs=["/workspace"])

        unknown = []
        for name in engine.tool_registry.names:
            decision = checker.check(name, {})
            if not decision.allowed and decision.code == PermissionReasonCode.UNKNOWN_TOOL:
                unknown.append(name)

        assert unknown == []
        await engine.shutdown()

    def test_registered_tools_have_openai_compatible_schemas(self) -> None:
        engine = AgentEngine(AppConfig())
        name_re = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
        errors: list[str] = []
        seen: set[str] = set()

        for tool in engine.tool_registry.all():
            if tool.name in seen:
                errors.append(f"{tool.name}: duplicate tool name")
            seen.add(tool.name)

            if not name_re.match(tool.name):
                errors.append(f"{tool.name}: invalid function name")

            schema = tool.to_openai_tool()
            json.dumps(schema, ensure_ascii=False)
            parameters = schema.get("function", {}).get("parameters", {})
            properties = parameters.get("properties", {})
            required = parameters.get("required", [])

            if parameters.get("type") != "object":
                errors.append(f"{tool.name}: parameters.type must be object")
            if not isinstance(properties, dict):
                errors.append(f"{tool.name}: properties must be object")
            if not isinstance(required, list):
                errors.append(f"{tool.name}: required must be list")
            else:
                for field in required:
                    if field not in properties:
                        errors.append(
                            f"{tool.name}: required field missing from properties: {field}"
                        )

        assert errors == []


class TestReset:
    def test_clears_messages(self, engine: AgentEngine) -> None:
        engine._messages.append({"role": "user", "content": "hi"})
        engine.reset()
        assert len(engine._messages) == 0

    def test_resets_usage(self, engine: AgentEngine) -> None:
        engine._usage.total_input_tokens = 100
        engine.reset()
        assert engine._usage.total_input_tokens == 0

    @pytest.mark.asyncio
    async def test_reset_clears_permission_grants_for_the_active_session(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        grant = engine._permission_grant_store.create(
            session.id,
            "shell",
            "reset-call",
        )

        engine.reset()

        assert engine.list_permission_grants() == ()
        assert engine._permission_grant_store.list_session(session.id) == ()
        revocations = [
            record
            for record in engine.get_recent_permission_bubbles(limit=50)
            if record["status"] == "grant_revoked"
        ]
        assert len(revocations) == 1
        assert revocations[0]["grant_id"] == grant.grant_id
        assert revocations[0]["tool_family"] == "shell"
        assert revocations[0]["session_id"] == session.id
        assert isinstance(revocations[0]["timestamp"], float)
        assert revocations[0]["reason"]
        assert revocations[0]["source"] == "reset"


class TestPermissionGrantRevocationAudit:
    @staticmethod
    def _assert_revocation(
        record: dict[str, object],
        *,
        grant_id: str,
        tool_family: str,
        session_id: str,
        source: str,
    ) -> None:
        assert record["status"] == "grant_revoked"
        assert record["grant_id"] == grant_id
        assert record["tool_family"] == tool_family
        assert record["session_id"] == session_id
        assert isinstance(record["timestamp"], float)
        assert record["reason"]
        assert record["source"] == source

    @pytest.mark.asyncio
    async def test_explicit_revoke_records_the_removed_grant_with_bounded_history(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        grant = engine._permission_grant_store.create(
            session.id,
            "shell",
            "explicit-call",
        )
        engine._permission_bubble_history = [
            {"status": "existing", "timestamp": float(index)}
            for index in range(100)
        ]

        assert engine.revoke_permission_grant(grant.grant_id) is True

        history = engine.get_recent_permission_bubbles(limit=50)
        assert len(engine._permission_bubble_history) == 100
        self._assert_revocation(
            history[-1],
            grant_id=grant.grant_id,
            tool_family="shell",
            session_id=session.id,
            source="explicit_revoke",
        )

    @pytest.mark.asyncio
    async def test_revoke_all_records_each_removed_grant(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        shell = engine._permission_grant_store.create(session.id, "shell", "shell-call")
        background = engine._permission_grant_store.create(
            session.id,
            "background_process",
            "background-call",
        )

        assert engine.revoke_all_permission_grants() == 2

        revocations = [
            record
            for record in engine.get_recent_permission_bubbles(limit=50)
            if record["status"] == "grant_revoked"
        ]
        assert len(revocations) == 2
        self._assert_revocation(
            revocations[0],
            grant_id=shell.grant_id,
            tool_family="shell",
            session_id=session.id,
            source="revoke_all",
        )
        self._assert_revocation(
            revocations[1],
            grant_id=background.grant_id,
            tool_family="background_process",
            session_id=session.id,
            source="revoke_all",
        )

    @pytest.mark.asyncio
    async def test_no_revocation_audit_is_created_when_no_grant_is_removed(
        self,
        engine: AgentEngine,
    ) -> None:
        await engine.get_or_create_session()

        assert engine.revoke_permission_grant("missing-grant") is False
        assert engine.revoke_all_permission_grants() == 0

        assert engine.get_recent_permission_bubbles(limit=50) == []


class TestContextVisualPayloads:
    @pytest.mark.asyncio
    async def test_model_call_sanitizes_inline_image_payload(self, engine: AgentEngine) -> None:
        data_url = "data:image/png;base64," + ("a" * 4096)
        messages = [{"role": "user", "content": f"请看截图：{data_url}"}]
        response = ModelResponse(content="ok", model="test-model")
        engine._router.call = AsyncMock(return_value=response)  # type: ignore[method-assign]

        await engine._call_model_with_recovery(
            messages=messages,
            tier=ModelTier.CAPABLE,
            tools=None,
        )

        sent_messages = engine._router.call.call_args.kwargs["messages"]
        assert "base64_chars=4096" in sent_messages[0]["content"]
        assert "a" * 512 not in str(sent_messages)

    def test_context_info_estimates_sanitized_current_messages(
        self,
        engine: AgentEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_url = "data:image/png;base64," + ("a" * 12000)
        engine._messages = [{"role": "user", "content": f"截图：{data_url}"}]
        engine._usage.total_input_tokens = 12000
        engine._router.resolve_model = MagicMock(return_value="test-model")
        engine._router.get_context_window = MagicMock(return_value=200000)

        def fallback_estimate(messages: list[dict[str, Any]]) -> int:
            assert "a" * 512 not in str(messages)
            return sum(len(str(message.get("content", ""))) for message in messages) // 4

        monkeypatch.setattr(engine._compactor, "_estimate_tokens", fallback_estimate)

        info = engine.get_context_info()

        assert info["used"] < 200
        assert info["percentage"] < 1


class TestContextBudget:
    def test_compute_context_budget_uses_model_window_when_runtime_budget_is_unlimited(
        self,
        engine: AgentEngine,
        mock_router: MagicMock,
    ) -> None:
        engine._router = mock_router
        engine._config.memory.compaction_reserved_tokens = 20_000
        mock_router.get_context_window.return_value = 120_000
        mock_router.get_max_output.return_value = 8_000

        budget, reserve = engine._compute_context_budget("model-x")

        assert budget == 112_000
        assert reserve == 8_000

    def test_compute_context_budget_reserves_output_tokens(
        self,
        engine: AgentEngine,
        mock_router: MagicMock,
    ) -> None:
        engine._router = mock_router
        engine._config.memory.compaction_reserved_tokens = 20_000
        mock_router.get_context_window.return_value = 120_000
        mock_router.get_max_output.return_value = 8_000

        budget, reserve = engine._compute_context_budget("model-x")

        assert budget == 112_000
        assert reserve == 8_000

    def test_compute_context_budget_falls_back_when_reserve_exceeds_window(
        self,
        engine: AgentEngine,
        mock_router: MagicMock,
    ) -> None:
        engine._router = mock_router
        engine._config.memory.compaction_reserved_tokens = 200_000
        mock_router.get_context_window.return_value = 120_000
        mock_router.get_max_output.return_value = 300_000

        budget, reserve = engine._compute_context_budget("model-x")

        # 当预留超过窗口时，回退为原始窗口，避免 unusable 的可用预算。
        assert budget == 120_000
        assert reserve == 0


class TestSetSystemPrompt:
    def test_ensure_system_prompt_uses_section_builder(self, engine: AgentEngine) -> None:
        engine._ensure_system_prompt()

        system_messages = [m for m in engine._messages if m["role"] == "system"]
        assert len(system_messages) == 1
        content = system_messages[0]["content"]
        assert '<naumi_system_prompt version="sections-v1">' in content
        assert "## Runtime Defaults" in content
        assert str(engine.workspace_root) in content
        assert "Registered tools:" in content

    def test_ensure_system_prompt_refreshes_generated_prompt(
        self,
        engine: AgentEngine,
    ) -> None:
        engine._ensure_system_prompt()
        before = engine._messages[0]["content"]
        before_count = int(re.search(r"Registered tools: (\d+)", before).group(1))
        engine._tool_registry.register(FakeTool())

        engine._ensure_system_prompt()

        system_messages = [m for m in engine._messages if m["role"] == "system"]
        assert len(system_messages) == 1
        after = system_messages[0]["content"]
        after_count = int(re.search(r"Registered tools: (\d+)", after).group(1))
        assert after_count == before_count + 1
        assert len(engine._full_history) == 1

    def test_custom_system_prompt_is_not_overwritten(self, engine: AgentEngine) -> None:
        engine.set_system_prompt("custom prompt")

        engine._ensure_system_prompt()

        assert engine._messages[0]["content"] == "custom prompt"

    def test_replaces_existing(self, engine: AgentEngine) -> None:
        engine._messages.append({"role": "system", "content": "old"})
        engine.set_system_prompt("new prompt")
        assert engine._messages[0]["content"] == "new prompt"
        assert len([m for m in engine._messages if m["role"] == "system"]) == 1

    def test_updates_full_history(self, engine: AgentEngine) -> None:
        engine._messages = [
            {"role": "system", "content": "old active"},
            {"role": "user", "content": "hi"},
        ]
        engine._full_history = [
            {"role": "system", "content": "old persisted"},
            {"role": "user", "content": "hi"},
        ]

        engine.set_system_prompt("new prompt")

        assert engine._messages[0] == {"role": "system", "content": "new prompt"}
        assert engine._full_history[0] == {"role": "system", "content": "new prompt"}
        assert len([m for m in engine._full_history if m["role"] == "system"]) == 1


class TestAutoMemoryExtraction:
    @pytest.mark.asyncio
    async def test_disabled_long_term_memory_skips_extraction(
        self,
        engine: AgentEngine,
    ) -> None:
        engine._config.memory.long_term_enabled = False
        engine.long_term_memory.store = AsyncMock(return_value="mem1")  # type: ignore[method-assign]

        await engine._auto_extract_memories(
            "remember this preference",
            AgentResult(status="completed", response="ok"),
        )

        engine.long_term_memory.store.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_completed_turn_stores_high_confidence_memory(
        self,
        engine: AgentEngine,
    ) -> None:
        engine._session = Session(title="auto memory")
        engine.long_term_memory.store = AsyncMock(return_value="mem1")  # type: ignore[method-assign]
        result = AgentResult(status="completed", response="收到。")

        await engine._auto_extract_memories(
            "以后请优先用中文回复。",
            result,
        )

        engine.long_term_memory.store.assert_awaited_once()  # type: ignore[attr-defined]
        entry = engine.long_term_memory.store.await_args.args[0]  # type: ignore[attr-defined]
        assert entry.category == "preference"
        assert "用户偏好" in entry.content
        assert entry.metadata["source"] == "auto_extract"
        assert entry.metadata["session_id"] == engine._session.id
        assert entry.metadata["scope"] == "global"

    @pytest.mark.asyncio
    async def test_non_completed_turn_does_not_store_memory(
        self,
        engine: AgentEngine,
    ) -> None:
        engine.long_term_memory.store = AsyncMock(return_value="mem1")  # type: ignore[method-assign]

        await engine._auto_extract_memories(
            "以后请优先用中文回复。",
            AgentResult(status="error", error="boom"),
        )

        engine.long_term_memory.store.assert_not_awaited()  # type: ignore[attr-defined]


class TestSessionLoading:
    def test_git_branch_probe_does_not_inherit_interactive_stdin(
        self,
        engine: AgentEngine,
    ) -> None:
        with patch("subprocess.check_output", return_value="main\n") as check_output:
            assert engine._current_git_branch() == "main"

        assert check_output.call_args.kwargs == {
            "cwd": str(engine.workspace_root),
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "timeout": 2,
        }

    @pytest.mark.asyncio
    async def test_load_session_keeps_sanitized_full_history(self, tmp_path) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        session = Session(title="缺失工具结果")
        session.messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "读文件"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
            },
        ]
        await engine.session_store.save(session)

        loaded = await engine.load_session(session.id)

        assert loaded is True
        assert engine._messages == engine._full_history
        assert engine._messages[-1]["role"] == "tool"
        assert engine._messages[-1]["tool_call_id"] == "call_1"
        await engine.shutdown()

    @pytest.mark.asyncio
    async def test_load_session_revokes_previous_session_permission_grants(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        try:
            previous = await engine.get_or_create_session()
            previous_grant = engine._permission_grant_store.create(
                previous.id,
                "shell",
                "previous-call",
            )
            replacement = await engine.session_store.create_session(title="replacement")

            assert await engine.load_session(replacement.id) is True
            assert engine.list_permission_grants() == ()
            assert engine._permission_grant_store.list_session(previous.id) == ()
            revocations = [
                record
                for record in engine.get_recent_permission_bubbles(limit=50)
                if record["status"] == "grant_revoked"
            ]
            assert len(revocations) == 1
            TestPermissionGrantRevocationAudit._assert_revocation(
                revocations[0],
                grant_id=previous_grant.grant_id,
                tool_family="shell",
                session_id=previous.id,
                source="session_load",
            )
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_delete_transition_blocks_session_grant_while_persistence_is_pending(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        delete_task: asyncio.Task[bool] | None = None
        marker = tmp_path / "delete-transition-tool-ran"
        try:
            active = await engine.get_or_create_session()
            engine._permission_grant_store.create(active.id, "shell", "active-grant")
            original_delete = engine.session_store.delete

            async def delayed_delete(session_id: str) -> bool:
                entered.set()
                await release.wait()
                return await original_delete(session_id)

            engine.session_store.delete = delayed_delete  # type: ignore[method-assign]
            confirmations: list[dict[str, object]] = []

            async def confirm(payload: dict[str, object]) -> str:
                confirmations.append(payload)
                return "allow_once"

            engine.set_permission_confirmer(confirm)
            delete_task = asyncio.create_task(engine.delete_session(active.id))
            await entered.wait()

            assert engine._permission_grant_store.allows(active.id, "shell") is True
            result = await engine._execute_tool(
                ToolCall(
                    id="delete-transition-tool",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "error"
            assert "会话正在切换" in result.content
            assert confirmations == []
            assert not marker.exists()

            release.set()
            assert await delete_task is True
        finally:
            release.set()
            if delete_task is not None and not delete_task.done():
                await delete_task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_load_transition_blocks_session_grant_while_persistence_is_pending(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        load_task: asyncio.Task[bool] | None = None
        marker = tmp_path / "load-transition-tool-ran"
        try:
            active = await engine.get_or_create_session()
            engine._permission_grant_store.create(active.id, "shell", "active-grant")
            replacement = await engine.session_store.create_session(title="replacement")
            original_load = engine.session_store.load

            async def delayed_load(session_id: str) -> Session | None:
                entered.set()
                await release.wait()
                return await original_load(session_id)

            engine.session_store.load = delayed_load  # type: ignore[method-assign]
            confirmations: list[dict[str, object]] = []

            async def confirm(payload: dict[str, object]) -> str:
                confirmations.append(payload)
                return "allow_once"

            engine.set_permission_confirmer(confirm)
            load_task = asyncio.create_task(engine.load_session(replacement.id))
            await entered.wait()

            assert engine._permission_grant_store.allows(active.id, "shell") is True
            result = await engine._execute_tool(
                ToolCall(
                    id="load-transition-tool",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "error"
            assert "会话正在切换" in result.content
            assert confirmations == []
            assert not marker.exists()

            release.set()
            assert await load_task is True
            assert engine._session is not None
            assert engine._session.id == replacement.id
            assert engine._permission_grant_store.list_session(active.id) == ()
        finally:
            release.set()
            if load_task is not None and not load_task.done():
                await load_task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_failed_delete_clears_transition_and_preserves_active_grant(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        delete_task: asyncio.Task[bool] | None = None
        marker = tmp_path / "failed-delete-tool-ran"
        try:
            active = await engine.get_or_create_session()
            grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "active-grant",
            )

            async def failed_delete(_: str) -> bool:
                entered.set()
                await release.wait()
                return False

            engine.session_store.delete = failed_delete  # type: ignore[method-assign]
            confirmations: list[dict[str, object]] = []

            async def confirm(payload: dict[str, object]) -> str:
                confirmations.append(payload)
                return "allow_once"

            engine.set_permission_confirmer(confirm)
            delete_task = asyncio.create_task(engine.delete_session(active.id))
            await entered.wait()

            release.set()
            assert await delete_task is False
            assert engine._session is active
            assert engine._permission_grant_store.list_session(active.id) == (grant,)
            assert engine._session_transition_epochs == {}

            result = await engine._execute_tool(
                ToolCall(
                    id="failed-delete-tool",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "success"
            assert marker.exists()
            assert confirmations == []
        finally:
            release.set()
            if delete_task is not None and not delete_task.done():
                await delete_task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_failed_load_clears_transition_and_preserves_active_grant(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        load_task: asyncio.Task[bool] | None = None
        marker = tmp_path / "failed-load-tool-ran"
        try:
            active = await engine.get_or_create_session()
            grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "active-grant",
            )

            async def failed_load(_: str) -> Session | None:
                entered.set()
                await release.wait()
                return None

            engine.session_store.load = failed_load  # type: ignore[method-assign]
            confirmations: list[dict[str, object]] = []

            async def confirm(payload: dict[str, object]) -> str:
                confirmations.append(payload)
                return "allow_once"

            engine.set_permission_confirmer(confirm)
            load_task = asyncio.create_task(engine.load_session("missing-session"))
            await entered.wait()

            release.set()
            assert await load_task is False
            assert engine._session is active
            assert engine._permission_grant_store.list_session(active.id) == (grant,)
            assert engine._session_transition_epochs == {}

            result = await engine._execute_tool(
                ToolCall(
                    id="failed-load-tool",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "success"
            assert marker.exists()
            assert confirmations == []
        finally:
            release.set()
            if load_task is not None and not load_task.done():
                await load_task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_cancelled_delete_clears_transition_and_preserves_active_grant(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        delete_task: asyncio.Task[bool] | None = None
        try:
            active = await engine.get_or_create_session()
            grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "active-grant",
            )

            async def delayed_delete(_: str) -> bool:
                entered.set()
                await release.wait()
                return True

            engine.session_store.delete = delayed_delete  # type: ignore[method-assign]
            delete_task = asyncio.create_task(engine.delete_session(active.id))
            await entered.wait()
            delete_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await delete_task

            assert engine._session is active
            assert engine._permission_grant_store.list_session(active.id) == (grant,)
            assert engine._session_transition_epochs == {}
        finally:
            release.set()
            if delete_task is not None and not delete_task.done():
                await delete_task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_cancelled_delete_after_commit_reconciles_active_session_authority(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        committed = asyncio.Event()
        release_delete_result = asyncio.Event()
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()
        delete_task: asyncio.Task[bool] | None = None
        pending_tool_task: asyncio.Task[ToolResult] | None = None
        marker = tmp_path / "post-commit-pending-confirmation-ran"
        try:
            active = await engine.get_or_create_session()
            initial_generation = engine._session_authorization_generation
            original_delete = engine.session_store.delete

            async def committed_delete(session_id: str) -> bool:
                deleted = await original_delete(session_id)
                committed.set()
                await release_delete_result.wait()
                return deleted

            async def confirm(payload: dict[str, object]) -> str:
                assert payload["session_id"] == active.id
                confirmation_started.set()
                await release_confirmation.wait()
                return "allow_once"

            engine.session_store.delete = committed_delete  # type: ignore[method-assign]
            engine.set_permission_confirmer(confirm)
            pending_tool_task = asyncio.create_task(
                engine._execute_tool(
                    ToolCall(
                        id="post-commit-pending-confirmation",
                        name="bash_run",
                        arguments=json.dumps(
                            {"command": f"printf ran > {shlex.quote(str(marker))}"}
                        ),
                    )
                )
            )
            await confirmation_started.wait()
            grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "pre-delete-grant",
            )

            delete_task = asyncio.create_task(engine.delete_session(active.id))
            await committed.wait()
            delete_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await delete_task

            assert await engine.session_store.load(active.id) is None
            assert engine._session is None
            assert engine._session_authorization_generation == initial_generation + 1
            assert engine._permission_grant_store.list_session(active.id) == ()
            revocations = [
                record
                for record in engine.get_recent_permission_bubbles(limit=50)
                if record["status"] == "grant_revoked"
            ]
            TestPermissionGrantRevocationAudit._assert_revocation(
                revocations[-1],
                grant_id=grant.grant_id,
                tool_family="shell",
                session_id=active.id,
                source="session_deletion",
            )

            release_confirmation.set()
            pending_result = await pending_tool_task

            assert pending_result.status == "error"
            assert "会话已切换" in pending_result.content
            assert not marker.exists()
        finally:
            release_delete_result.set()
            release_confirmation.set()
            for task in (delete_task, pending_tool_task):
                if task is not None and not task.done():
                    await task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_repeatedly_cancelled_delete_after_commit_finishes_reconciliation(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        committed = asyncio.Event()
        release_delete_result = asyncio.Event()
        reconciliation_started = asyncio.Event()
        release_reconciliation = asyncio.Event()
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()
        delete_task: asyncio.Task[bool] | None = None
        pending_tool_task: asyncio.Task[ToolResult] | None = None
        marker = tmp_path / "repeated-cancel-pending-confirmation-ran"
        try:
            active = await engine.get_or_create_session()
            initial_generation = engine._session_authorization_generation
            original_delete = engine.session_store.delete
            original_load = engine.session_store.load

            async def committed_delete(session_id: str) -> bool:
                deleted = await original_delete(session_id)
                committed.set()
                await release_delete_result.wait()
                return deleted

            async def paused_reconciliation_load(session_id: str) -> Session | None:
                reconciliation_started.set()
                await release_reconciliation.wait()
                return await original_load(session_id)

            async def confirm(payload: dict[str, object]) -> str:
                assert payload["session_id"] == active.id
                confirmation_started.set()
                await release_confirmation.wait()
                return "allow_once"

            engine.session_store.delete = committed_delete  # type: ignore[method-assign]
            engine.session_store.load = (  # type: ignore[method-assign]
                paused_reconciliation_load
            )
            engine.set_permission_confirmer(confirm)
            pending_tool_task = asyncio.create_task(
                engine._execute_tool(
                    ToolCall(
                        id="repeated-cancel-pending-confirmation",
                        name="bash_run",
                        arguments=json.dumps(
                            {"command": f"printf ran > {shlex.quote(str(marker))}"}
                        ),
                    )
                )
            )
            await asyncio.wait_for(confirmation_started.wait(), timeout=1.0)
            grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "repeated-cancel-grant",
            )

            delete_task = asyncio.create_task(engine.delete_session(active.id))
            await asyncio.wait_for(committed.wait(), timeout=1.0)
            delete_task.cancel()
            await asyncio.wait_for(reconciliation_started.wait(), timeout=1.0)
            delete_task.cancel()
            await asyncio.sleep(0)

            assert not delete_task.done()
            release_reconciliation.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(delete_task, timeout=1.0)

            assert await original_load(active.id) is None
            assert engine._session is None
            assert engine._session_authorization_generation == initial_generation + 1
            assert engine._permission_grant_store.list_session(active.id) == ()
            assert engine._session_transition_epochs == {}
            assert engine._session_transition_tokens == {}
            revocations = [
                record
                for record in engine.get_recent_permission_bubbles(limit=50)
                if record["status"] == "grant_revoked"
            ]
            TestPermissionGrantRevocationAudit._assert_revocation(
                revocations[-1],
                grant_id=grant.grant_id,
                tool_family="shell",
                session_id=active.id,
                source="session_deletion",
            )

            release_confirmation.set()
            pending_result = await asyncio.wait_for(pending_tool_task, timeout=1.0)

            assert pending_result.status == "error"
            assert "会话已切换" in pending_result.content
            assert not marker.exists()
        finally:
            release_delete_result.set()
            release_reconciliation.set()
            release_confirmation.set()
            for task in (delete_task, pending_tool_task):
                if task is not None and not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_same_session_load_does_not_begin_transition_or_revoke_grant(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            active = await engine.get_or_create_session()
            grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "active-grant",
            )
            original_load = engine.session_store.load

            async def same_session_load(session_id: str) -> Session | None:
                assert engine._session_transition_epochs == {}
                return await original_load(session_id)

            engine.session_store.load = same_session_load  # type: ignore[method-assign]

            assert await engine.load_session(active.id) is True
            assert engine._session is not None
            assert engine._session.id == active.id
            assert engine._permission_grant_store.list_session(active.id) == (grant,)
            assert engine._session_transition_epochs == {}
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_clears_all_permission_grants(self, tmp_path) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        session = await engine.get_or_create_session()
        grant = engine._permission_grant_store.create(
            session.id,
            "shell",
            "shutdown-call",
        )

        await engine.shutdown()

        assert engine._permission_grant_store.list_session(session.id) == ()
        revocations = [
            record
            for record in engine.get_recent_permission_bubbles(limit=50)
            if record["status"] == "grant_revoked"
        ]
        assert len(revocations) == 1
        TestPermissionGrantRevocationAudit._assert_revocation(
            revocations[0],
            grant_id=grant.grant_id,
            tool_family="shell",
            session_id=session.id,
            source="shutdown",
        )

    @pytest.mark.asyncio
    async def test_delete_active_session_revokes_its_grants_and_invalidates_runtime_state(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            active = await engine.get_or_create_session()
            survivor = await engine.session_store.create_session(title="survivor")
            active_grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "active-grant",
            )
            survivor_grant = engine._permission_grant_store.create(
                survivor.id,
                "shell",
                "survivor-grant",
            )
            engine._messages.append({"role": "user", "content": "active context"})
            engine._full_history.append({"role": "user", "content": "active history"})
            engine._usage.total_input_tokens = 9
            engine._permission_checker.check("bash_run", {"command": "printf counted"})
            engine.task_store.set_session(active.id)

            assert await engine.delete_session(active.id) is True

            assert await engine.session_store.load(active.id) is None
            assert engine._permission_grant_store.list_session(active.id) == ()
            assert engine._permission_grant_store.list_session(survivor.id) == (survivor_grant,)
            assert engine._session is None
            assert engine._messages == []
            assert engine._full_history == []
            assert engine._usage.total_input_tokens == 0
            assert engine.task_store.session_id == ""
            assert engine._permission_checker.get_call_counts() == {}
            revocations = [
                record
                for record in engine.get_recent_permission_bubbles(limit=50)
                if record["status"] == "grant_revoked"
            ]
            assert len(revocations) == 1
            TestPermissionGrantRevocationAudit._assert_revocation(
                revocations[0],
                grant_id=active_grant.grant_id,
                tool_family="shell",
                session_id=active.id,
                source="session_deletion",
            )

            confirmations: list[dict[str, object]] = []

            async def confirm(payload: dict[str, object]) -> str:
                confirmations.append(payload)
                return "deny"

            engine.set_permission_confirmer(confirm)
            fresh = await engine.get_or_create_session()
            result = await engine._execute_tool(
                ToolCall(
                    id="after-deletion",
                    name="bash_run",
                    arguments='{"command": "printf should_not_run"}',
                )
            )

            assert result.status == "error"
            assert len(confirmations) == 1
            assert confirmations[0]["session_id"] == fresh.id
            assert engine.list_permission_grants() == ()
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_delete_non_active_or_missing_session_preserves_current_runtime_state(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            active = await engine.get_or_create_session()
            other = await engine.session_store.create_session(title="other")
            active_grant = engine._permission_grant_store.create(
                active.id,
                "shell",
                "active-grant",
            )
            engine._permission_grant_store.create(other.id, "shell", "other-grant")
            engine._messages.append({"role": "user", "content": "keep context"})
            engine._full_history.append({"role": "user", "content": "keep history"})
            engine.task_store.set_session(active.id)

            assert await engine.delete_session(other.id) is True
            assert await engine.delete_session("missing-session") is False

            assert engine._session is not None
            assert engine._session.id == active.id
            assert engine._messages == [{"role": "user", "content": "keep context"}]
            assert engine._full_history == [{"role": "user", "content": "keep history"}]
            assert engine.task_store.session_id == active.id
            assert engine._permission_grant_store.list_session(active.id) == (active_grant,)
            assert engine._permission_grant_store.list_session(other.id) == ()
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_delete_active_session_makes_pending_confirmation_stale(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        marker = tmp_path / "deleted-session-tool-ran"
        try:
            active = await engine.get_or_create_session()

            async def confirm(payload: dict[str, object]) -> str:
                assert payload["session_id"] == active.id
                assert await engine.delete_session(active.id) is True
                return "allow_once"

            engine.set_permission_confirmer(confirm)
            result = await engine._execute_tool(
                ToolCall(
                    id="delete-during-confirmation",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "error"
            assert "会话已切换" in result.content
            assert not marker.exists()
            assert engine._session is None
        finally:
            await engine.shutdown()


class TestSessionAuthorizationGeneration:
    @pytest.mark.asyncio
    async def test_allow_once_confirmation_does_not_revive_after_session_aba(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        marker = tmp_path / "allow-once-aba-ran"
        try:
            original = await engine.get_or_create_session()
            replacement = await engine.session_store.create_session(title="replacement")

            async def confirm(payload: dict[str, object]) -> str:
                assert payload["session_id"] == original.id
                assert await engine.load_session(replacement.id) is True
                assert await engine.load_session(original.id) is True
                return "allow_once"

            engine.set_permission_confirmer(confirm)
            result = await engine._execute_tool(
                ToolCall(
                    id="allow-once-aba",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "error"
            assert "会话已切换" in result.content
            assert not marker.exists()
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_session_grant_does_not_revive_after_session_aba(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        marker = tmp_path / "grant-aba-ran"
        try:
            original = await engine.get_or_create_session()
            replacement = await engine.session_store.create_session(title="replacement")

            async def confirm(payload: dict[str, object]) -> str:
                assert payload["session_id"] == original.id
                assert await engine.load_session(replacement.id) is True
                assert await engine.load_session(original.id) is True
                return "grant_session"

            engine.set_permission_confirmer(confirm)
            result = await engine._execute_tool(
                ToolCall(
                    id="grant-aba",
                    name="bash_run",
                    arguments=json.dumps(
                        {"command": f"printf ran > {shlex.quote(str(marker))}"}
                    ),
                )
            )

            assert result.status == "error"
            assert "会话已切换" in result.content
            assert not marker.exists()
            assert engine._permission_grant_store.list_session(original.id) == ()
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_serialized_concurrent_loads_keep_newer_transition_fence(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        first_entered = asyncio.Event()
        first_release = asyncio.Event()
        second_entered = asyncio.Event()
        second_release = asyncio.Event()
        first_load: asyncio.Task[bool] | None = None
        second_load: asyncio.Task[bool] | None = None
        try:
            await engine.get_or_create_session()
            intermediate = await engine.session_store.create_session(title="intermediate")
            final = await engine.session_store.create_session(title="final")
            real_load = engine.session_store.load

            async def gated_load(session_id: str) -> Session | None:
                if session_id == intermediate.id:
                    first_entered.set()
                    await first_release.wait()
                elif session_id == final.id:
                    second_entered.set()
                    await second_release.wait()
                return await real_load(session_id)

            engine.session_store.load = gated_load  # type: ignore[method-assign]
            first_load = asyncio.create_task(engine.load_session(intermediate.id))
            await first_entered.wait()
            second_load = asyncio.create_task(engine.load_session(final.id))
            await asyncio.sleep(0)

            first_release.set()
            await second_entered.wait()
            assert await first_load is True
            assert engine._session is not None
            assert engine._session.id == intermediate.id

            intermediate_grant = engine._permission_grant_store.create(
                intermediate.id,
                "shell",
                "intermediate-grant",
            )
            blocked = await engine._execute_tool(
                ToolCall(
                    id="blocked-while-newer-load-pending",
                    name="bash_run",
                    arguments='{"command": "printf must_not_run"}',
                )
            )
            assert blocked.status == "error"
            assert "会话正在切换" in blocked.content

            second_release.set()
            assert await second_load is True
            assert engine._session is not None
            assert engine._session.id == final.id
            assert engine._permission_grant_store.list_session(intermediate.id) == ()
            assert intermediate_grant.grant_id
        finally:
            first_release.set()
            second_release.set()
            for task in (first_load, second_load):
                if task is not None and not task.done():
                    await task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_cancelling_queued_load_does_not_clear_running_load_fence(
        self,
        tmp_path: Path,
    ) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        running_load: asyncio.Task[bool] | None = None
        queued_load: asyncio.Task[bool] | None = None
        try:
            active = await engine.get_or_create_session()
            first_target = await engine.session_store.create_session(title="first")
            second_target = await engine.session_store.create_session(title="second")
            real_load = engine.session_store.load

            async def gated_load(session_id: str) -> Session | None:
                if session_id == first_target.id:
                    entered.set()
                    await release.wait()
                return await real_load(session_id)

            engine.session_store.load = gated_load  # type: ignore[method-assign]
            running_load = asyncio.create_task(engine.load_session(first_target.id))
            await entered.wait()
            queued_load = asyncio.create_task(engine.load_session(second_target.id))
            await asyncio.sleep(0)
            queued_load.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued_load

            result = await engine._execute_tool(
                ToolCall(
                    id="blocked-after-queued-cancel",
                    name="bash_run",
                    arguments='{"command": "printf must_not_run"}',
                )
            )
            assert result.status == "error"
            assert "会话正在切换" in result.content
            assert engine._session is active

            release.set()
            assert await running_load is True
        finally:
            release.set()
            for task in (running_load, queued_load):
                if task is not None and not task.done():
                    await task
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_engine_uses_permission_outcome_when_compatibility_booleans_disagree(
        self,
        engine: AgentEngine,
    ) -> None:
        await engine.get_or_create_session()
        allow = PermissionDecision(
            allowed=False,
            requires_confirmation=True,
            outcome=PermissionOutcome.ALLOW,
        )
        block = PermissionDecision(
            allowed=True,
            reason="outcome block",
            requires_confirmation=False,
            outcome=PermissionOutcome.BLOCK,
        )
        with patch.object(
            engine._permission_checker,
            "check",
            side_effect=[allow, block],
        ):
            allowed_result = await engine._execute_tool(
                ToolCall(
                    id="outcome-allow",
                    name="bash_run",
                    arguments='{"command": "printf outcome_allow"}',
                )
            )
            blocked_result = await engine._execute_tool(
                ToolCall(
                    id="outcome-block",
                    name="bash_run",
                    arguments='{"command": "printf outcome_block"}',
                )
            )

        assert allowed_result.status == "success"
        assert "outcome_allow" in allowed_result.content
        assert blocked_result.status == "error"
        assert "outcome block" in blocked_result.content


class TestToolCallParsing:
    def test_valid_tool_call(self, engine: AgentEngine) -> None:
        raw = {
            "id": "call_123",
            "function": {"name": "file_read", "arguments": '{"path": "/tmp/x"}'},
        }
        tc = engine._parse_tool_call(raw)
        assert tc is not None
        assert tc.name == "file_read"
        assert tc.arguments == '{"path": "/tmp/x"}'

    def test_missing_function(self, engine: AgentEngine) -> None:
        raw = {"id": "call_123"}
        tc = engine._parse_tool_call(raw)
        assert tc is not None
        assert tc.name == ""

    def test_empty_dict(self, engine: AgentEngine) -> None:
        tc = engine._parse_tool_call({})
        assert tc is not None


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, engine: AgentEngine) -> None:
        tc = ToolCall(id="x", name="nonexistent_tool", arguments="{}")
        result = await engine._execute_tool(tc)
        assert result.status == "error"
        assert "未知工具" in result.content

    @pytest.mark.asyncio
    async def test_invalid_json_args(self, engine: AgentEngine) -> None:
        tc = ToolCall(id="x", name="file_read", arguments="not json{{{")
        result = await engine._execute_tool(tc)
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_decoded_dict_args_execute_normally(self, engine: AgentEngine) -> None:
        engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
        tc = ToolCall(
            id="x",
            name="file_read",
            arguments={"path": "pyproject.toml"},  # type: ignore[arg-type]
        )

        result = await engine._execute_tool(tc)

        assert result.status == "success"
        assert "pyproject.toml" in result.content

    @pytest.mark.asyncio
    async def test_non_string_invalid_args_do_not_crash(self, engine: AgentEngine) -> None:
        tc = ToolCall(
            id="x",
            name="file_read",
            arguments=None,  # type: ignore[arg-type]
        )

        result = await engine._execute_tool(tc)

        assert result.status == "error"
        assert "Invalid JSON arguments" in result.content

    @pytest.mark.asyncio
    async def test_path_sandbox_blocked(self, engine: AgentEngine) -> None:
        tc = ToolCall(id="x", name="file_read", arguments='{"path": "/etc/passwd"}')
        result = await engine._execute_tool(tc)
        assert result.status == "error"
        assert "权限拒绝" in result.content

    @pytest.mark.asyncio
    async def test_metadata_path_arg_sandbox_blocks_yaml_validate(
        self,
        engine: AgentEngine,
    ) -> None:
        tc = ToolCall(
            id="x",
            name="yaml_validate",
            arguments='{"file_path": "/etc/passwd"}',
        )

        result = await engine._execute_tool(tc)

        assert result.status == "error"
        assert "权限拒绝" in result.content
        assert "不在允许目录内" in result.content

    @pytest.mark.asyncio
    async def test_path_in_allowed_dir(self, engine: AgentEngine) -> None:
        engine._permission_checker._allowed_dirs = [str(Path("/tmp").resolve())]
        tc = ToolCall(
            id="x", name="file_read",
            arguments='{"path": "/tmp/nonexistent_test_file.txt"}',
        )
        result = await engine._execute_tool(tc)
        # Should attempt read, fail with "File not found" not a permission denial.
        assert "Error: File not found" in result.content or "权限拒绝" in result.content

    @pytest.mark.asyncio
    async def test_managed_worktree_storage_is_permission_allowed(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state_dir = tmp_path / "state"
        worktree_file = state_dir / "worktrees" / "pursue-demo" / "demo.py"
        worktree_file.parent.mkdir(parents=True)
        worktree_file.write_text("x = 1\n", encoding="utf-8")

        config = AppConfig()
        config.workspace_root = str(workspace)
        config.memory.session_db_path = str(state_dir / "sessions.db")
        config.safety.allowed_dirs = []
        engine = AgentEngine(config)
        try:
            result = await engine._execute_tool(
                ToolCall(
                    id="x",
                    name="file_read",
                    arguments=json.dumps({"path": str(worktree_file)}),
                )
            )
        finally:
            await engine.shutdown()

        assert result.status == "success"
        assert "x = 1" in result.content

    @pytest.mark.asyncio
    async def test_confirmation_required_tool_blocked(self, engine: AgentEngine) -> None:
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo should_not_run"}')
        result = await engine._execute_tool(tc)
        assert result.status == "error"
        assert "需要用户确认" in result.content

    @pytest.mark.asyncio
    async def test_confirmation_callback_allows_tool_once(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session()
        payloads: list[dict[str, object]] = []
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            payloads.append(payload)
            return "allow"

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo confirm_ok"}')
        result = await engine._execute_tool(tc, on_event=on_event)

        assert result.status == "success"
        assert "confirm_ok" in result.content
        assert payloads
        assert payloads[0]["tool_name"] == "bash_run"
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", session.id),
            ("confirmed", session.id),
        ]

    @pytest.mark.asyncio
    async def test_bypass_confirmation_switches_the_runtime_mode_globally(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        payloads: list[dict[str, object]] = []
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            payloads.append(payload)
            return "bypass"

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        first = await engine._execute_tool(
            ToolCall(
                id="shell-1",
                name="bash_run",
                arguments='{"command": "printf first"}',
            ),
            on_event=on_event,
        )
        second = await engine._execute_tool(
            ToolCall(
                id="shell-2",
                name="bash_run",
                arguments='{"command": "printf second"}',
            )
        )

        assert first.status == "success"
        assert second.status == "success"
        assert len(payloads) == 1
        assert payloads[0]["session_id"] == session.id
        assert payloads[0]["tool_family"] == "shell"
        assert payloads[0]["choices"] == ["allow_once", "deny", "grant_session"]
        assert payloads[0]["scope"] == "session"
        assert payloads[0]["expires_at"] is None
        assert payloads[0]["requires_double_confirm"] is False
        assert engine.runtime_mode == AgentRuntimeMode.BYPASS
        assert engine.permission_mode == PermissionMode.BYPASS
        assert engine.list_permission_grants() == ()
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", session.id),
            ("bypass_enabled", session.id),
        ]

    @pytest.mark.asyncio
    async def test_explicitly_revoked_shell_grant_requires_confirmation_again(
        self,
        engine: AgentEngine,
    ) -> None:
        await engine.get_or_create_session()
        confirmations: list[dict[str, object]] = []
        responses = iter(["grant_session", "allow_once"])

        async def confirm(payload: dict[str, object]) -> str:
            confirmations.append(payload)
            return next(responses)

        engine.set_permission_confirmer(confirm)
        first = await engine._execute_tool(
            ToolCall(
                id="first-shell",
                name="bash_run",
                arguments='{"command": "printf first"}',
            )
        )
        grants = engine.list_permission_grants()

        assert first.status == "success"
        assert len(grants) == 1
        assert engine.revoke_permission_grant(grants[0].grant_id) is True

        second = await engine._execute_tool(
            ToolCall(
                id="second-shell",
                name="bash_run",
                arguments='{"command": "printf second"}',
            )
        )

        assert second.status == "success"
        assert len(confirmations) == 2
        assert all(payload["tool_family"] == "shell" for payload in confirmations)

    @pytest.mark.asyncio
    async def test_permission_grant_does_not_match_another_tool_family(
        self,
        engine: AgentEngine,
    ) -> None:
        await engine.get_or_create_session()
        payloads: list[dict[str, object]] = []
        responses = iter(["grant_session", "deny"])

        async def confirm(payload: dict[str, object]) -> str:
            payloads.append(payload)
            return next(responses)

        engine.set_permission_confirmer(confirm)
        shell = await engine._execute_tool(
            ToolCall(
                id="shell-1",
                name="bash_run",
                arguments='{"command": "printf shell"}',
            )
        )
        background = await engine._execute_tool(
            ToolCall(
                id="background-1",
                name="background_run",
                arguments='{"command": "printf background"}',
            )
        )

        assert shell.status == "success"
        assert background.status == "error"
        assert len(payloads) == 2
        assert payloads[1]["tool_family"] == "background_process"
        assert engine.list_permission_grants()[0].tool_family == "shell"

    @pytest.mark.asyncio
    async def test_grant_session_without_an_active_session_is_denied(
        self,
        engine: AgentEngine,
    ) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            assert payload["session_id"] == ""
            assert payload["choices"] == ["allow_once", "deny"]
            assert payload["scope"] == "call"
            return "grant_session"

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        result = await engine._execute_tool(
            ToolCall(
                id="no-session",
                name="bash_run",
                arguments='{"command": "printf should_not_run"}',
            ),
            on_event=on_event,
        )

        assert result.status == "error"
        assert "没有活动会话" in result.content
        assert engine.list_permission_grants() == ()
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", ""),
            ("grant_rejected", ""),
        ]

    @pytest.mark.asyncio
    async def test_grant_session_is_rejected_when_the_session_changes_during_confirmation(
        self,
        engine: AgentEngine,
        tmp_path: Path,
    ) -> None:
        original = await engine.get_or_create_session()
        replacement = await engine.session_store.create_session(title="replacement")
        marker = tmp_path / "grant-session-ran"
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            assert payload["session_id"] == original.id
            assert await engine.load_session(replacement.id) is True
            return "grant_session"

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        result = await engine._execute_tool(
            ToolCall(
                id="session-changed",
                name="bash_run",
                arguments=json.dumps({"command": f"printf ran > {shlex.quote(str(marker))}"}),
            ),
            on_event=on_event,
        )

        assert result.status == "error"
        assert "会话已切换" in result.content
        assert not marker.exists()
        assert engine._permission_grant_store.list_session(original.id) == ()
        assert engine._permission_grant_store.list_session(replacement.id) == ()
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", original.id),
            ("grant_rejected", original.id),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("response", ["allow", "allow_once"])
    async def test_stale_allow_confirmation_is_rejected_before_bash_execution(
        self,
        engine: AgentEngine,
        tmp_path: Path,
        response: str,
    ) -> None:
        original = await engine.get_or_create_session()
        replacement = await engine.session_store.create_session(title="replacement")
        marker = tmp_path / f"stale-{response}-ran"
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            assert payload["session_id"] == original.id
            assert await engine.load_session(replacement.id) is True
            return response

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        result = await engine._execute_tool(
            ToolCall(
                id=f"stale-{response}",
                name="bash_run",
                arguments=json.dumps({"command": f"printf ran > {shlex.quote(str(marker))}"}),
            ),
            on_event=on_event,
        )

        assert result.status == "error"
        assert "会话已切换" in result.content
        assert not marker.exists()
        assert engine._permission_grant_store.list_session(original.id) == ()
        assert engine._permission_grant_store.list_session(replacement.id) == ()
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", original.id),
            ("confirmed", original.id),
            ("stale_confirmation_rejected", original.id),
        ]

    @pytest.mark.asyncio
    async def test_reset_during_allow_once_confirmation_stops_bash_execution(
        self,
        engine: AgentEngine,
        tmp_path: Path,
    ) -> None:
        original = await engine.get_or_create_session()
        marker = tmp_path / "reset-confirmation-ran"
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            assert payload["session_id"] == original.id
            engine.reset()
            return "allow_once"

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        result = await engine._execute_tool(
            ToolCall(
                id="reset-confirmation",
                name="bash_run",
                arguments=json.dumps({"command": f"printf ran > {shlex.quote(str(marker))}"}),
            ),
            on_event=on_event,
        )

        assert result.status == "error"
        assert "会话已切换" in result.content
        assert not marker.exists()
        assert engine._permission_grant_store.list_session(original.id) == ()
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", original.id),
            ("confirmed", original.id),
            ("stale_confirmation_rejected", original.id),
        ]

    @pytest.mark.asyncio
    async def test_high_risk_tool_in_bypass_skips_confirmation(
        self,
        engine: AgentEngine,
    ) -> None:
        await engine.get_or_create_session()
        payloads: list[dict[str, object]] = []

        async def confirm(payload: dict[str, object]) -> str:
            payloads.append(payload)
            return "bypass"

        engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
        engine.set_permission_confirmer(confirm)
        result = await engine._execute_tool(
            ToolCall(
                id="high-risk",
                name="session_delete",
                arguments='{"session_id": "missing-session"}',
            )
        )

        assert result.status == "success"
        assert payloads == []
        assert engine.list_permission_grants() == ()
        assert engine.runtime_mode == AgentRuntimeMode.BYPASS
        assert engine.permission_mode == PermissionMode.BYPASS

    @pytest.mark.asyncio
    async def test_current_session_permission_grant_apis_cannot_touch_other_sessions(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        current = engine._permission_grant_store.create(
            session.id,
            "shell",
            "current-call",
        )
        other = engine._permission_grant_store.create(
            "another-session",
            "shell",
            "other-call",
        )

        assert engine.list_permission_grants() == (current,)
        assert engine.revoke_permission_grant(other.grant_id) is False
        assert engine.revoke_permission_grant(current.grant_id) is True
        assert engine.list_permission_grants() == ()

        engine._permission_grant_store.create(session.id, "shell", "shell-call")
        engine._permission_grant_store.create(
            session.id,
            "background_process",
            "background-call",
        )
        assert engine.revoke_all_permission_grants() == 2
        assert engine.list_permission_grants() == ()
        assert engine._permission_grant_store.list_session("another-session") == (other,)

    @pytest.mark.asyncio
    async def test_plan_runtime_mode_blocks_write_tools(
        self,
        engine: AgentEngine,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "blocked.txt"
        engine.set_runtime_mode(AgentRuntimeMode.PLAN)
        tc = ToolCall(
            id="x",
            name="file_write",
            arguments=json.dumps({"path": str(target), "content": "nope"}),
        )

        result = await engine._execute_tool(tc)

        assert result.status == "error"
        assert "Plan 模式只允许只读工具" in result.content
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_plan_runtime_mode_allows_read_only_tools(self, engine: AgentEngine) -> None:
        engine.set_runtime_mode("plan")
        tc = ToolCall(id="x", name="file_read", arguments='{"path": "pyproject.toml"}')

        result = await engine._execute_tool(tc)

        assert result.status == "success"
        assert "pyproject.toml" in result.content

    def test_plan_runtime_mode_read_only_name_allowlist_matches_registered_tools(
        self,
        engine: AgentEngine,
    ) -> None:
        tool = FakeTool()

        for tool_name in [
            "background_status",
            "background_list",
            "background_read_output",
            "schedule_list",
            "worktree_status",
        ]:
            assert engine._tool_allowed_in_plan_mode(tool_name, tool)

        for tool_name in [
            "background_run",
            "schedule_create",
            "worktree_create",
        ]:
            assert not engine._tool_allowed_in_plan_mode(tool_name, tool)

    def test_plan_runtime_mode_allows_self_review(self, engine: AgentEngine) -> None:
        tool = engine.tool_registry.get("self_review")
        assert tool is not None

        assert engine._tool_allowed_in_plan_mode("self_review", tool)

    def test_runtime_mode_cycle_updates_permission_mode(self, engine: AgentEngine) -> None:
        assert engine.runtime_mode == AgentRuntimeMode.DEFAULT

        assert engine.cycle_runtime_mode() == AgentRuntimeMode.PLAN
        assert engine.permission_mode == PermissionMode.STRICT

        assert engine.cycle_runtime_mode() == AgentRuntimeMode.BYPASS
        assert engine.permission_mode == PermissionMode.BYPASS

        assert engine.cycle_runtime_mode() == AgentRuntimeMode.DEFAULT
        assert engine.permission_mode == PermissionMode.MODERATE

    @pytest.mark.asyncio
    async def test_confirmation_callback_denies_tool(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session()
        events: list[tuple[str, dict[str, object]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            return "deny"

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo denied"}')
        result = await engine._execute_tool(tc, on_event=on_event)

        assert result.status == "error"
        assert "用户已拒绝" in result.content
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert [(bubble["status"], bubble["session_id"]) for bubble in bubbles] == [
            ("needs_confirmation", session.id),
            ("denied", session.id),
        ]

    @pytest.mark.asyncio
    async def test_top_level_permission_bubble_emitted_for_confirmation(
        self,
        engine: AgentEngine,
    ) -> None:
        from naumi_agent.memory.session import Session

        engine._session = Session(title="permission bubble")
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo blocked"}')
        result = await engine._execute_tool(tc, on_event=on_event)

        assert result.status == "error"
        bubbles = [data for event, data in events if event == "permission_bubble"]
        assert bubbles
        assert bubbles[0]["agent_name"] == "main"
        assert bubbles[0]["tool_name"] == "bash_run"
        assert bubbles[0]["status"] == "needs_confirmation"
        assert bubbles[0]["call_id"] == "x"
        assert bubbles[0]["session_id"] == engine._session.id
        assert bubbles[0]["risk_level"] == "medium"

    @pytest.mark.asyncio
    async def test_bypass_mode_runs_confirmation_tool(self, engine: AgentEngine) -> None:
        engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo bypass_ok"}')
        result = await engine._execute_tool(tc)
        assert result.status == "success"
        assert "bypass_ok" in result.content

    @pytest.mark.asyncio
    async def test_bypass_mode_runs_dangerous_shell_command_without_confirmation(
        self,
        engine: AgentEngine,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "showcase-page"
        target.mkdir()
        (target / "index.html").write_text("<h1>demo</h1>", encoding="utf-8")

        engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
        command = f"rm -rf {shlex.quote(str(target))} && echo removed"
        tc = ToolCall(
            id="x",
            name="bash_run",
            arguments=json.dumps({"command": command}),
        )

        result = await engine._execute_tool(tc)

        assert result.status == "success"
        assert "removed" in result.content
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_task_create_tool_passes_permission_layer(self, tmp_path) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)

        result = await engine._execute_tool(
            ToolCall(
                id="x",
                name="task_create",
                arguments='{"subject": "创建文件"}',
            )
        )

        assert result.status == "success"
        assert "已创建任务" in result.content
        await engine.shutdown()


class TestUsageAccumulation:
    def test_accumulate(self, engine: AgentEngine) -> None:
        usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, cost_usd=0.01)
        engine._accumulate_usage(usage)
        assert engine._usage.total_input_tokens == 100
        assert engine._usage.total_output_tokens == 50
        assert engine._usage.total_cost_usd == 0.01

    def test_accumulate_multiple(self, engine: AgentEngine) -> None:
        u1 = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, cost_usd=0.01)
        u2 = TokenUsage(input_tokens=200, output_tokens=100, total_tokens=300, cost_usd=0.02)
        engine._accumulate_usage(u1)
        engine._accumulate_usage(u2)
        assert engine._usage.total_input_tokens == 300
        assert engine._usage.total_output_tokens == 150


class TestHookIntegration:
    @pytest.mark.asyncio
    async def test_user_prompt_hook_can_rewrite_prompt(self, engine: AgentEngine) -> None:
        @engine.hooks.on(HookPoint.USER_PROMPT_SUBMIT)
        def rewrite(ctx: HookContext) -> None:
            ctx.data["prompt"] = "rewritten prompt"

        mock_response = ModelResponse(
            content="ok",
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            model="test-model",
        )

        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine.run("original prompt")

        assert result.status == "completed"
        assert {"role": "user", "content": "rewritten prompt"} in engine._messages
        assert engine.hooks.get_trace()[0].point == "user_prompt_submit"

    @pytest.mark.asyncio
    async def test_context_assembly_hook_appends_visible_section(
        self,
        engine: AgentEngine,
    ) -> None:
        @engine.hooks.on(HookPoint.CONTEXT_ASSEMBLE_END)
        def append_section(ctx: HookContext) -> None:
            ctx.data["extra_sections"] = ["### Hook 注入\n- 已触发"]

        await engine._inject_harness_context_snapshot()

        assert "### Hook 注入" in engine._messages[-1]["content"]
        assert any(
            entry.point == "context_assemble_end"
            for entry in engine.hooks.get_trace()
        )

    @pytest.mark.asyncio
    async def test_tool_permission_hook_can_block_tool(self, engine: AgentEngine) -> None:
        @engine.hooks.on(HookPoint.TOOL_PERMISSION_CHECK)
        def block_after(ctx: HookContext) -> None:
            if ctx.data.get("phase") == "after":
                ctx.data["abort"] = True
                ctx.data["abort_reason"] = "测试策略拒绝"

        result = await engine._execute_tool(ToolCall(
            id="x",
            name="file_read",
            arguments='{"path": "pyproject.toml"}',
        ))

        assert result.status == "error"
        assert "测试策略拒绝" in result.content
        assert any(
            entry.point == "tool_permission_check" and entry.aborted
            for entry in engine.hooks.get_trace()
        )

    @pytest.mark.asyncio
    async def test_streaming_emits_hook_trace_events(self, engine: AgentEngine) -> None:
        @engine.hooks.on(HookPoint.USER_PROMPT_SUBMIT)
        def mark_prompt(ctx: HookContext) -> None:
            ctx.data["prompt"] = ctx.data["prompt"] + " hooked"

        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        mock_response = ModelResponse(
            content="ok",
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            model="test-model",
        )

        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine.run_streaming("hello", on_event)

        assert result.status == "completed"
        assert events[0][0] == "run_started"
        hook_events = [data for event, data in events if event == "hook_trace"]
        assert hook_events
        assert hook_events[0]["point"] == "user_prompt_submit"

    @pytest.mark.asyncio
    async def test_streaming_persists_authoritative_completion_receipt(
        self,
        engine: AgentEngine,
    ) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        mock_response = ModelResponse(
            content="已完成真实检查。",
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            model="test-model",
        )

        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine.run_streaming("检查当前状态", on_event)

        assert result.receipt is not None
        receipt_events = [data for event, data in events if event == "completion_receipt"]
        assert receipt_events == [result.receipt.to_dict()]
        assert events[0][0] == "run_started"
        assert events[0][1]["run_id"] == result.receipt.run_id
        assert engine._session is not None
        restored = await engine.chat_run_store.get_run(
            engine._session.id,
            result.receipt.run_id,
        )
        assert restored is not None
        assert restored.receipt == result.receipt
        assert restored.status == "completed"

    @pytest.mark.asyncio
    async def test_streaming_cancellation_persists_receipt_before_propagating(
        self,
        engine: AgentEngine,
    ) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        with (
            patch.object(
                engine,
                "_run_streaming_core",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await engine.run_streaming("执行后取消", on_event)

        assert engine._session is not None
        runs = await engine.chat_run_store.list_runs(engine._session.id)
        assert len(runs) == 1
        assert runs[0].status == "cancelled"
        assert runs[0].receipt is not None
        assert runs[0].receipt.outcome == "cancelled"
        receipt_events = [data for event, data in events if event == "completion_receipt"]
        assert receipt_events == [runs[0].receipt.to_dict()]

    @pytest.mark.asyncio
    async def test_hook_trace_event_emits_when_trace_is_capped(
        self,
        engine: AgentEngine,
    ) -> None:
        engine.hooks._max_trace_entries = 1

        @engine.hooks.on(HookPoint.USER_PROMPT_SUBMIT)
        def mark_prompt(ctx: HookContext) -> None:
            ctx.data["seen"] = True

        await engine._fire_hook(HookContext(point=HookPoint.USER_PROMPT_SUBMIT))
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        await engine._fire_hook(
            HookContext(point=HookPoint.USER_PROMPT_SUBMIT),
            on_event,
        )

        assert len(events) == 1
        assert events[0][0] == "hook_trace"
        assert events[0][1]["point"] == "user_prompt_submit"
        assert str(events[0][1]["callback"]).endswith("mark_prompt")
        assert events[0][1]["aborted"] is False
        assert events[0][1]["error"] == ""

    @pytest.mark.asyncio
    async def test_agent_stop_hook_fires_on_final_response(
        self,
        engine: AgentEngine,
    ) -> None:
        stops: list[str] = []

        @engine.hooks.on(HookPoint.AGENT_STOP)
        def on_stop(ctx: HookContext) -> None:
            stops.append(str(ctx.data["status"]))

        mock_response = ModelResponse(
            content="done",
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            model="test-model",
        )

        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        assert result.status == "completed"
        assert stops == ["completed"]


class TestTaskVisualization:
    def test_todo_write_prepare_snapshot_includes_progress_preview(self) -> None:
        snapshot = {
            0: {
                "id": "call-1",
                "function": {
                    "name": "todo_write",
                    "arguments": json.dumps(
                        {
                            "todos": [
                                {"content": "读取实现", "status": "completed"},
                                {"content": "补测试", "status": "in_progress"},
                                {"content": "验证", "status": "pending"},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            }
        }

        result = _summarize_tool_prepare_snapshot(
            snapshot,
            started_at=1.0,
            now=1.25,
        )

        assert result["todo_total"] == 3
        assert result["todo_completed"] == 1
        assert result["todo_open"] == 2
        assert result["todo_items"][0]["subject"] == "补测试"

    def test_todo_write_prepare_snapshot_parses_partial_streamed_arguments(self) -> None:
        snapshot = {
            0: {
                "id": "call-1",
                "function": {
                    "name": "todo_write",
                    "arguments": (
                        '{"todos":['
                        '{"content":"创建文件","status":"in_progress"},'
                        '{"content":"验证页面","status":"pending"}'
                    ),
                },
            }
        }

        result = _summarize_tool_prepare_snapshot(
            snapshot,
            started_at=1.0,
            now=1.25,
        )

        assert result["todo_total"] == 2
        assert result["todo_completed"] == 0
        assert result["todo_open"] == 2
        assert result["todo_items"][0]["subject"] == "创建文件"

    def test_append_message_sanitizes_visual_payloads_before_context(self, tmp_path) -> None:
        engine = AgentEngine(AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        ))
        try:
            engine._append_message({
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这个页面"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + ("A" * 4096),
                        },
                    },
                ],
            })

            stored = str(engine._messages[-1])
            assert "A" * 128 not in stored
            assert "图片内容已省略" in stored
        finally:
            asyncio.run(engine.shutdown())

    @pytest.mark.asyncio
    async def test_task_tool_emits_snapshot_event(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        result = await engine._execute_tool(ToolCall(
            id="todo-1",
            name="todo_write",
            arguments=json.dumps({
                "todos": [
                    {"content": "读取实现", "status": "completed"},
                    {"content": "补测试", "status": "pending", "blocked_by": ["1"]},
                ],
            }, ensure_ascii=False),
        ), on_event=on_event)

        assert result.status == "success"
        snapshots = [data for event, data in events if event == "task_snapshot"]
        assert snapshots
        assert snapshots[-1]["source"] == "todo_write"
        assert snapshots[-1]["open_count"] == 1
        assert snapshots[-1]["completed_count"] == 1
        assert snapshots[-1]["items"] == [
            {"id": "2", "status": "pending", "subject": "补测试"}
        ]
        assert "补测试" in str(snapshots[-1]["summary"])

    @pytest.mark.asyncio
    async def test_todo_snapshot_does_not_duplicate_merge_items(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        await engine._execute_tool(ToolCall(
            id="todo-1",
            name="todo_write",
            arguments=json.dumps({
                "todos": [
                    {"content": "创建项目目录结构", "status": "pending"},
                    {"content": "编写 HTML 文件", "status": "pending"},
                ],
            }, ensure_ascii=False),
        ), on_event=on_event)
        result = await engine._execute_tool(ToolCall(
            id="todo-2",
            name="todo_write",
            arguments=json.dumps({
                "todos": [
                    {"content": "创建项目目录结构", "status": "completed"},
                ],
            }, ensure_ascii=False),
        ), on_event=on_event)

        assert result.status == "success"
        snapshots = [data for event, data in events if event == "task_snapshot"]
        assert snapshots[-1]["count"] == 2
        assert snapshots[-1]["open_count"] == 1
        assert snapshots[-1]["completed_count"] == 1
        assert snapshots[-1]["items"] == [
            {"id": "2", "status": "pending", "subject": "编写 HTML 文件"}
        ]


class TestSubagentVisualization:
    @pytest.mark.asyncio
    async def test_delegate_task_emits_events_and_completes_linked_todo(
        self,
        tmp_path,
    ) -> None:
        engine = AgentEngine(AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        ))
        try:
            await self._assert_delegate_task_emits_events_and_completes_linked_todo(engine)
        finally:
            await engine.shutdown()

    async def _assert_delegate_task_emits_events_and_completes_linked_todo(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        task = await engine.task_store.create_task(subject="让 coder 检查实现")
        coder = engine.subagent_manager.get_agent("coder")
        assert coder is not None
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        with patch.object(
            coder,
            "execute",
            new_callable=AsyncMock,
            return_value=AgentResult(
                status="completed",
                response="检查完成",
                total_tokens=256,
                total_cost_usd=0.0123,
            ),
        ):
            result = await engine._execute_tool(ToolCall(
                id="delegate-1",
                name="delegate_task",
                arguments=json.dumps({
                    "task": "检查实现是否完整",
                    "agent": "coder",
                    "task_id": task.id,
                    "success_criteria": "给出明确结论",
                }, ensure_ascii=False),
            ), on_event=on_event)

        assert result.status == "success"
        refreshed = await engine.task_store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status.value == "completed"
        subagent_events = [data for event, data in events if event == "subagent_event"]
        assert [event["status"] for event in subagent_events] == ["started", "completed"]
        assert all("检查实现是否完整" in str(event["description"]) for event in subagent_events)
        assert all("给出明确结论" in str(event["description"]) for event in subagent_events)
        assert subagent_events[-1]["tokens"] == 256
        assert subagent_events[-1]["cost"] == 0.0123
        assert all(float(event["timestamp"]) > 0 for event in subagent_events)
        assert any(event == "task_snapshot" for event, _ in events)

    @pytest.mark.asyncio
    async def test_delegate_task_blocks_linked_todo_on_failure(
        self,
        tmp_path,
    ) -> None:
        engine = AgentEngine(AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        ))
        try:
            await self._assert_delegate_task_blocks_linked_todo_on_failure(engine)
        finally:
            await engine.shutdown()

    async def _assert_delegate_task_blocks_linked_todo_on_failure(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        task = await engine.task_store.create_task(subject="让 coder 处理失败用例")
        coder = engine.subagent_manager.get_agent("coder")
        assert coder is not None

        with patch.object(
            coder,
            "execute",
            new_callable=AsyncMock,
            return_value=AgentResult(status="error", error="缺少输入"),
        ):
            result = await engine._execute_tool(ToolCall(
                id="delegate-2",
                name="delegate_task",
                arguments=json.dumps({
                    "task": "处理失败用例",
                    "agent": "coder",
                    "task_id": task.id,
                }, ensure_ascii=False),
            ))

        assert result.status == "success"
        refreshed = await engine.task_store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status.value == "blocked"
        assert refreshed.active_form is not None
        assert "缺少输入" in refreshed.active_form

    @pytest.mark.asyncio
    async def test_delegate_task_blocks_linked_todo_on_agent_exception(
        self,
        tmp_path,
    ) -> None:
        engine = AgentEngine(AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        ))
        try:
            await self._assert_delegate_task_blocks_linked_todo_on_agent_exception(engine)
        finally:
            await engine.shutdown()

    async def _assert_delegate_task_blocks_linked_todo_on_agent_exception(
        self,
        engine: AgentEngine,
    ) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        task = await engine.task_store.create_task(subject="让 coder 处理异常")
        coder = engine.subagent_manager.get_agent("coder")
        assert coder is not None
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        with patch.object(
            coder,
            "execute",
            new_callable=AsyncMock,
            side_effect=RuntimeError("执行器崩溃"),
        ):
            result = await engine._execute_tool(ToolCall(
                id="delegate-3",
                name="delegate_task",
                arguments=json.dumps({
                    "task": "处理异常",
                    "agent": "coder",
                    "task_id": task.id,
                }, ensure_ascii=False),
            ), on_event=on_event)

        assert result.status == "success"
        refreshed = await engine.task_store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status.value == "blocked"
        assert refreshed.active_form is not None
        assert "执行器崩溃" in refreshed.active_form
        subagent_events = [data for event, data in events if event == "subagent_event"]
        assert [event["status"] for event in subagent_events] == ["started", "error"]


class TestBudgetCheck:
    def test_budget_exceeded_returns_stop_result(self, engine: AgentEngine) -> None:
        engine._budget_tracker.budget = TokenBudget(max_input_tokens=500_000)
        engine._budget_tracker._total_input = 999_999_999
        result = engine._check_budget()
        assert result is not None
        assert result.status == "budget_exceeded"
        assert "预算已耗尽" in result.response
        assert "输入 token" in result.response

    def test_explicit_budget_applies_in_bypass_mode(self) -> None:
        config = AppConfig(
            safety=SafetyConfig(permission_mode="bypass", max_budget_usd=0)
        )
        engine = AgentEngine(config)
        result = engine._check_budget()

        assert result is not None
        assert result.status == "budget_exceeded"

    def test_default_engine_budget_is_unlimited(self, engine: AgentEngine) -> None:
        info = engine.get_budget_info()

        assert info == {
            "enabled": False,
            "used_usd": 0.0,
            "max_usd": None,
            "remaining_usd": None,
            "cost_percentage": None,
            "input_tokens": 0,
            "max_input_tokens": None,
            "input_percentage": None,
            "output_tokens": 0,
            "max_output_tokens": None,
            "output_percentage": None,
            "percentage": None,
        }
        assert engine._check_budget() is None
        json.dumps(info, allow_nan=False)

    def test_budget_status_uses_highest_active_percentage(
        self,
        engine: AgentEngine,
    ) -> None:
        engine._budget_tracker.budget = TokenBudget(
            max_input_tokens=1_000,
            max_output_tokens=100,
            max_usd=2.0,
        )
        engine._budget_tracker._total_input = 500
        engine._budget_tracker._total_output = 80
        engine._budget_tracker._total_cost = 0.5

        info = engine.get_budget_info()

        assert info["cost_percentage"] == 25.0
        assert info["input_percentage"] == 50.0
        assert info["output_percentage"] == 80.0
        assert info["percentage"] == 80.0
        json.dumps(info, allow_nan=False)

    @pytest.mark.asyncio
    async def test_react_loop_stops_after_budget_exceeded(self, engine: AgentEngine) -> None:
        engine._budget_tracker.budget = TokenBudget(max_usd=0.001)
        mock_response = ModelResponse(
            content="This response should not be returned as completed.",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=1.0),
            model="test-model",
        )
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        assert result.status == "budget_exceeded"
        assert "费用" in result.response


class TestOrchestratedExecution:
    @pytest.mark.asyncio
    async def test_orchestrated_run_reports_failed_step_and_tracks_subagent_usage(
        self,
        engine: AgentEngine,
    ) -> None:
        plan = Plan(
            understanding="需要多 Agent 协作",
            approach="并行编排",
            steps=[
                Step(
                    id="ok",
                    description="完成可行步骤",
                    tool=None,
                    depends_on=[],
                    parallelizable=True,
                    complexity=Complexity.SIMPLE,
                ),
                Step(
                    id="bad",
                    description="暴露失败步骤",
                    tool=None,
                    depends_on=[],
                    parallelizable=True,
                    complexity=Complexity.SIMPLE,
                ),
            ],
            mode=ExecutionMode.ORCHESTRATOR,
        )
        engine.subagent_manager = MagicMock()
        engine.subagent_manager.stop_reaper = AsyncMock()
        engine.subagent_manager.execute_dag = AsyncMock(
            return_value={
                "ok": AgentResult(
                    status="completed",
                    response="完成了",
                    total_tokens=9,
                    total_cost_usd=0.25,
                ),
                "bad": AgentResult(
                    status="error",
                    error="子任务失败",
                    total_tokens=7,
                    total_cost_usd=0.2,
                ),
            }
        )

        result = await engine._run_orchestrated(plan, tools=None)
        budget = engine._budget_tracker.get_summary()

        assert result.status == "error"
        assert "暴露失败步骤" in result.response
        assert "子任务失败" in (result.error or "")
        assert budget.total_output_tokens == 16
        assert budget.total_cost_usd == 0.45


class TestContextCompactionPreservation:
    @pytest.mark.asyncio
    async def test_maybe_compact_archives_large_tool_results(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        try:
            large_content = "tool-output\n" * 2000
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "生成大输出"},
                {"role": "tool", "tool_call_id": "call_big", "content": large_content},
            ]

            await engine._maybe_compact(on_event)

            tool_message = next(
                msg for msg in engine._messages
                if msg.get("role") == "tool"
            )
            content = str(tool_message.get("content", ""))
            assert "[大型工具结果已归档]" in content
            assert "artifact:" in content
            artifact_line = next(
                line for line in content.splitlines()
                if line.startswith("artifact: ")
            )
            artifact_path = artifact_line.removeprefix("artifact: ")
            assert Path(artifact_path).read_text(encoding="utf-8") == large_content

            compacted_events = [
                data for event, data in events
                if event == "context_compacted"
            ]
            assert compacted_events
            assert compacted_events[-1]["archived_tool_results"] == 1
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_maybe_compact_preserves_runtime_state(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        try:
            session = await engine.get_or_create_session()
            engine.task_store.set_session(session.id)
            task = await engine.task_store.create_task(subject="等待 team protocol 复核")
            await engine.task_store.update_task(
                task.id,
                status=TaskStatus.BLOCKED,
                active_form="阻塞：等待用户确认 API 范围",
            )
            await execute_team_signal(
                engine.subagent_manager,
                event_type="handoff",
                sender="main_agent",
                recipient="coder",
                content="接手压缩保真验证。",
                priority="high",
            )
            await engine.subagent_manager.delegate(
                SubTask(id="sub-no-agent", description="没有明确关键词的任务"),
            )
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                *[
                    {"role": "user", "content": f"历史消息 {i} " * 60}
                    for i in range(56)
                ],
                {"role": "user", "content": "先不要全量测试，目前比较耽误时间，可以最后做"},
                {"role": "assistant", "content": "收到"},
            ]

            summary = ModelResponse(
                content="## 任务目标\n继续优化运行时压缩",
                usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                model="test",
            )
            with (
                patch.object(engine._compactor, "_extract_memories", new_callable=AsyncMock),
                patch.object(engine._router, "call", new_callable=AsyncMock, return_value=summary),
            ):
                await engine._maybe_compact(on_event)

            compacted_events = [
                data for event, data in events
                if event == "context_compacted"
            ]
            assert compacted_events
            assert "todo" in compacted_events[-1]["preserved_sections"]
            assert "team_protocol" in compacted_events[-1]["preserved_sections"]
            assert "subagent_events" in compacted_events[-1]["preserved_sections"]
            assert compacted_events[-1]["warnings"]

            summary_text = "\n".join(
                str(msg.get("content", "")) for msg in engine._messages
                if msg.get("role") == "system"
            )
            assert "压缩时保留的运行时状态" in summary_text
            assert "等待 team protocol 复核" in summary_text
            assert "接手压缩保真验证" in summary_text
            assert "没有找到合适的子 Agent" in summary_text
            assert "先不要全量测试" in summary_text
        finally:
            await engine.shutdown()


class TestErrorRecovery:
    @pytest.mark.asyncio
    async def test_length_finish_reason_continues_final_response(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        try:
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "写一个完整答案"},
            ]
            first = ModelResponse(
                content="第一段",
                finish_reason="length",
                usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                model="test-model",
            )
            continuation = ModelResponse(
                content="第二段",
                finish_reason="stop",
                usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
                model="test-model",
            )

            with patch.object(
                engine._router,
                "call",
                new_callable=AsyncMock,
                side_effect=[first, continuation],
            ) as mock_call:
                result = await engine._react_loop(tools=None)

            assert result.status == "completed"
            assert result.response == "第一段第二段"
            assert mock_call.call_count == 2
            continuation_messages = mock_call.call_args_list[1].kwargs["messages"]
            assert continuation_messages[-2] == {
                "role": "assistant",
                "content": "第一段",
            }
            assert "从截断处直接继续" in continuation_messages[-1]["content"]
            assistant_messages = [
                msg for msg in engine._messages
                if msg.get("role") == "assistant"
            ]
            assert assistant_messages[-1]["content"] == "第一段第二段"
            assert not any(
                msg.get("role") == "user" and "从截断处直接继续" in msg.get("content", "")
                for msg in engine._messages
            )
            assert engine.usage.total_input_tokens == 7
            assert engine.usage.total_output_tokens == 4
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_length_finish_reason_emits_continuation(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            yield StreamChunk(token="上半")
            yield StreamChunk(finish_reason="length")
            yield StreamChunk(
                usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                finish_reason="stop",
            )

        try:
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "写一个完整答案"},
            ]
            continuation = ModelResponse(
                content="下半",
                finish_reason="stop",
                usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
                model="test-model",
            )

            with (
                patch.object(engine._router, "stream", new=stream_response),
                patch.object(
                    engine._router,
                    "call",
                    new_callable=AsyncMock,
                    return_value=continuation,
                ),
            ):
                result = await engine._react_loop_streaming(
                    tools=None,
                    on_event=on_event,
                )

            assert result.status == "completed"
            assert result.response == "上半下半"
            token_text = "".join(
                str(data.get("content", ""))
                for event, data in events
                if event == "token"
            )
            assert token_text == "上半下半"
            recovery_events = [
                data for event, data in events
                if event == "recovery_event"
                and data.get("reason") == "output_truncated"
            ]
            assert [event["phase"] for event in recovery_events] == [
                "started",
                "completed",
            ]
            assert recovery_events[-1]["unit"] == "chars"
            assert engine._messages[-1]["content"] == "上半下半"
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_tool_call_suppresses_buffered_text_fragments(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []
        call_count = 0

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamChunk(token=")")
                yield StreamChunk(token="y")
                yield StreamChunk(tool_call_started=True)
                yield StreamChunk(
                    tool_call={
                        0: {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "fake_tool",
                                "arguments": "{}",
                            },
                        }
                    },
                    finish_reason="tool_calls",
                )
                return

            yield StreamChunk(token="工具完成")
            yield StreamChunk(finish_reason="stop")

        try:
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "调用工具"},
            ]

            with (
                patch.object(engine._router, "stream", new=stream_response),
                patch.object(
                    engine,
                    "_execute_tool",
                    new_callable=AsyncMock,
                    return_value=ToolResult(
                        call_id="call_1",
                        status="success",
                        content="ok",
                        duration_ms=1,
                    ),
                ),
            ):
                result = await engine._react_loop_streaming(
                    tools=[FakeTool().to_openai_tool()],
                    on_event=on_event,
                )

            assert result.status == "completed"
            assert result.response == "工具完成"
            token_text = "".join(
                str(data.get("content", ""))
                for event, data in events
                if event == "token"
            )
            assert token_text == "工具完成"
            tool_assistant_messages = [
                msg for msg in engine._messages
                if msg.get("role") == "assistant" and msg.get("tool_calls")
            ]
            assert tool_assistant_messages[-1]["content"] is None
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_tool_call_emits_prepare_progress(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []
        call_count = 0
        large_content = "\n".join(f"line_{idx}" for idx in range(600))
        final_args = json.dumps(
            {
                "file_path": "/tmp/showcase.html",
                "content": large_content,
            }
        )

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamChunk(
                    tool_call_started=True,
                    tool_call_snapshot={
                        0: {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "file_write",
                                "arguments": (
                                    '{"file_path": "/tmp/showcase.html", '
                                    '"content": "line_1'
                                ),
                            },
                        }
                    },
                )
                yield StreamChunk(
                    tool_call_snapshot={
                        0: {
                            "id": "call_1",
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
                            "id": "call_1",
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

            yield StreamChunk(token="创建完成")
            yield StreamChunk(finish_reason="stop")

        try:
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "创建网页"},
            ]

            with (
                patch.object(engine._router, "stream", new=stream_response),
                patch.object(
                    engine,
                    "_execute_tool",
                    new_callable=AsyncMock,
                    return_value=ToolResult(
                        call_id="call_1",
                        status="success",
                        content="ok",
                        duration_ms=1,
                    ),
                ),
            ):
                result = await engine._react_loop_streaming(
                    tools=[FakeTool().to_openai_tool()],
                    on_event=on_event,
                )

            assert result.status == "completed"
            prepare_events = [
                (event, data)
                for event, data in events
                if event.startswith("tool_prepare_")
            ]
            assert [event for event, _ in prepare_events] == [
                "tool_prepare_start",
                "tool_prepare_snapshot",
                "tool_prepare_end",
            ]
            assert prepare_events[0][1]["name"] == "file_write"
            assert prepare_events[0][1]["path"] == "/tmp/showcase.html"
            assert prepare_events[1][1]["content_lines"] == 600
            assert prepare_events[1][1]["argument_chars"] < len(final_args) + 1
            assert prepare_events[-1][1]["content_lines"] == 600
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_final_answer_flushes_after_tool_guard_window(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            yield StreamChunk(token="abcdefghijklmnopqrstuvwxyz普通回答")
            assert any(event == "token" for event, _ in events)
            yield StreamChunk(token="，继续实时输出")
            yield StreamChunk(finish_reason="stop")

        try:
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "直接回答"},
            ]

            with patch.object(engine._router, "stream", new=stream_response):
                result = await engine._react_loop_streaming(
                    tools=[FakeTool().to_openai_tool()],
                    on_event=on_event,
                )

            assert result.status == "completed"
            assert result.response == "abcdefghijklmnopqrstuvwxyz普通回答，继续实时输出"
            token_text = "".join(
                str(data.get("content", ""))
                for event, data in events
                if event == "token"
            )
            assert token_text == result.response
            perf_phases = [
                str(data.get("phase", ""))
                for event, data in events
                if event == "perf_phase"
            ]
            assert "context_prepare" in perf_phases
            assert "llm_first_chunk" in perf_phases
            assert "llm_stream" in perf_phases
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_prompt_too_long_reactive_compacts_and_retries(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        try:
            engine._messages = [
                {"role": "system", "content": "system prompt"},
                *[
                    {"role": "user", "content": f"long history {i}"}
                    for i in range(20)
                ],
            ]
            recovered_response = ModelResponse(
                content="恢复成功",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test",
            )
            compacted_messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "system", "content": "compact summary"},
                {"role": "user", "content": "latest"},
            ]
            with (
                patch.object(
                    engine._router,
                    "call",
                    new_callable=AsyncMock,
                    side_effect=[
                        RuntimeError("prompt_too_long: context length exceeded"),
                        recovered_response,
                    ],
                ) as mock_call,
                patch.object(
                    engine._compactor,
                    "compact",
                    new_callable=AsyncMock,
                    return_value=compacted_messages,
                ),
            ):
                result = await engine._call_model_with_recovery(
                    messages=engine._messages,
                    tier=ModelTier.CAPABLE,
                    tools=None,
                    on_event=on_event,
                )

            assert result.content == "恢复成功"
            assert mock_call.call_count == 2
            recovery_events = [data for event, data in events if event == "recovery_event"]
            assert [event["phase"] for event in recovery_events] == ["started", "completed"]
            assert recovery_events[-1]["action"] == "reactive_compact_retry"
            assert len(engine._messages) < 22
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_prompt_too_long_uses_deterministic_fallback_if_compactor_stalls(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        try:
            original_messages = [
                {"role": "system", "content": "system prompt"},
                *[
                    {"role": "user", "content": f"long history {i}"}
                    for i in range(20)
                ],
            ]
            engine._messages = list(original_messages)
            recovered_response = ModelResponse(
                content="fallback ok",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test",
            )
            with (
                patch.object(
                    engine._router,
                    "call",
                    new_callable=AsyncMock,
                    side_effect=[
                        RuntimeError("context_length exceeded"),
                        recovered_response,
                    ],
                ),
                patch.object(
                    engine._compactor,
                    "compact",
                    new_callable=AsyncMock,
                    return_value=original_messages,
                ),
            ):
                result = await engine._call_model_with_recovery(
                    messages=engine._messages,
                    tier=ModelTier.CAPABLE,
                    tools=None,
                )

            assert result.content == "fallback ok"
            summary_text = "\n".join(str(msg.get("content", "")) for msg in engine._messages)
            assert "Reactive compact fallback" in summary_text
            assert len(engine._messages) < len(original_messages)
        finally:
            await engine.shutdown()


class TestRun:
    @pytest.mark.asyncio
    async def test_simple_response(self, engine: AgentEngine) -> None:
        mock_response = ModelResponse(
            content="Hello!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_call:
            result = await engine.run("hi")

        assert result.status == "completed"
        assert result.response == "Hello!"
        assert result.usage.total_input_tokens == 10
        assert mock_call.await_count == 1
        assert engine.subagent_manager._reaper_task is None

    @pytest.mark.asyncio
    async def test_run_persists_system_prompt_in_full_history(self, tmp_path) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        mock_response = ModelResponse(
            content="Hello!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )

        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine.run("hi")

        saved = await engine.session_store.load(engine._session.id)

        assert result.status == "completed"
        assert saved is not None
        assert saved.messages[0]["role"] == "system"
        assert saved.messages[1] == {"role": "user", "content": "hi"}
        await engine.shutdown()

    @pytest.mark.asyncio
    async def test_max_turns(self, engine: AgentEngine) -> None:
        # Tool call loop that never ends
        mock_response = ModelResponse(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "file_read", "arguments": '{"path": "/tmp/x"}'},
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        engine._config.safety.max_turns = 2
        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine.run("loop")

        assert result.status == "max_turns"

    @pytest.mark.asyncio
    async def test_repeated_tool_call_breaks_loop(self, engine: AgentEngine) -> None:
        """Identical tool call 3x in a row should inject stop message."""
        class FakeBrowserGotoTool(Tool):
            @property
            def name(self) -> str:
                return "browser_goto"

            @property
            def description(self) -> str:
                return "Fake browser navigation for unit tests."

            @property
            def parameters_schema(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                }

            async def execute(self, **kwargs: object) -> str:
                return f"navigated: {kwargs.get('url', '')}"

        engine.tool_registry.register(FakeBrowserGotoTool())
        call_count = 0
        tool_response = ModelResponse(
            content="好的我来打开",
            tool_calls=[{
                "id": "call_1",
                "function": {
                    "name": "browser_goto",
                    "arguments": '{"url": "https://www.baidu.com"}',
                },
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        final_response = ModelResponse(
            content="已为你打开百度",
            usage=TokenUsage(input_tokens=5, output_tokens=3, total_tokens=8, cost_usd=0.001),
            model="test-model",
        )

        async def mock_call(**kwargs: object) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                return tool_response
            return final_response

        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            side_effect=mock_call,
        ):
            result = await engine.run("打开百度")

        # Should break after the 3rd repeat + 1 final response
        assert call_count <= 5
        assert "已为你打开" in result.response or result.status == "completed"

    @pytest.mark.asyncio
    async def test_repeated_multi_tool_call_keeps_protocol_complete(
        self,
        engine: AgentEngine,
    ) -> None:
        """Every assistant tool_call must receive a tool message, even when skipping repeats."""
        repeated = {
            "function": {
                "name": "file_read",
                "arguments": '{"path": "pyproject.toml"}',
            },
        }
        responses = [
            ModelResponse(
                content="",
                tool_calls=[{"id": "call_1", **repeated}],
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            ),
            ModelResponse(
                content="",
                tool_calls=[{"id": "call_2", **repeated}],
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            ),
            ModelResponse(
                content="",
                tool_calls=[
                    {"id": "call_3a", **repeated},
                    {
                        "id": "call_3b",
                        "function": {
                            "name": "file_read",
                            "arguments": '{"path": "README.md"}',
                        },
                    },
                ],
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            ),
            ModelResponse(
                content="done",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            ),
        ]

        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        assert result.status == "completed"
        tool_result_ids = {
            m.get("tool_call_id")
            for m in engine._messages
            if m.get("role") == "tool"
        }
        assert {"call_3a", "call_3b"}.issubset(tool_result_ids)
        repeated_messages = [
            str(m.get("content", ""))
            for m in engine._messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "call_3a"
        ]
        assert any("连续重复" in content for content in repeated_messages)
        assert not any("completed successfully" in content for content in repeated_messages)
        assert not any("final response" in content for content in repeated_messages)


class TestMemoryInjection:
    @pytest.mark.asyncio
    async def test_disabled_long_term_memory_skips_recall(
        self,
        engine: AgentEngine,
    ) -> None:
        engine._config.memory.long_term_enabled = False
        engine.long_term_memory.recall_for_session = AsyncMock(return_value=[])  # type: ignore[method-assign]

        await engine._inject_relevant_memories("hello")

        engine.long_term_memory.recall_for_session.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_injection_uses_current_session_scope(self, engine: AgentEngine) -> None:
        from naumi_agent.memory.session import Session

        engine._session = Session(title="scoped memory")
        with patch.object(
            engine.long_term_memory,
            "recall_for_session",
            new_callable=AsyncMock,
            return_value=[],
        ) as recall_for_session:
            await engine._inject_relevant_memories("检查当前项目")

        recall_for_session.assert_awaited_once_with(
            "检查当前项目",
            session_id=engine._session.id,
            top_k=3,
            min_relevance=0.4,
        )

    @pytest.mark.asyncio
    async def test_injects_relevant_memories(self, engine: AgentEngine) -> None:
        from naumi_agent.memory.long_term import MemoryEntry, MemorySearchResult
        from naumi_agent.memory.session import Session

        engine._session = Session(title="relevant memories")
        fake_results = [
            MemorySearchResult(
                entry=MemoryEntry(
                    id="m1", content="用户喜欢 Python", category="preference",
                    created_at="2026-01-01", updated_at="2026-01-01",
                ),
                relevance=0.85,
            ),
        ]

        with patch.object(
            engine.long_term_memory, "recall_for_session",
            new_callable=AsyncMock, return_value=fake_results,
        ):
            await engine._inject_relevant_memories("写一个 Python 脚本")

        # Should have added a system message with memories
        memory_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "## 相关记忆" in m.get("content", "")
        ]
        assert len(memory_msgs) == 1
        assert "用户喜欢 Python" in memory_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_no_injection_when_empty(self, engine: AgentEngine) -> None:
        from naumi_agent.memory.session import Session

        engine._session = Session(title="empty memories")
        with patch.object(
            engine.long_term_memory, "recall_for_session",
            new_callable=AsyncMock, return_value=[],
        ):
            await engine._inject_relevant_memories("hello")

        memory_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "## 相关记忆" in m.get("content", "")
        ]
        assert len(memory_msgs) == 0

    @pytest.mark.asyncio
    async def test_injection_replaces_previous(self, engine: AgentEngine) -> None:
        from naumi_agent.memory.long_term import MemoryEntry, MemorySearchResult
        from naumi_agent.memory.session import Session

        engine._session = Session(title="replacement memories")
        engine._messages.append({"role": "system", "content": "## 相关记忆\n- old"})

        fake_results = [
            MemorySearchResult(
                entry=MemoryEntry(
                    id="m1", content="new memory", category="fact",
                    created_at="2026-01-01", updated_at="2026-01-01",
                ),
                relevance=0.9,
            ),
        ]

        with patch.object(
            engine.long_term_memory, "recall_for_session",
            new_callable=AsyncMock, return_value=fake_results,
        ):
            await engine._inject_relevant_memories("query")

        memory_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "## 相关记忆" in m.get("content", "")
        ]
        assert len(memory_msgs) == 1
        assert "new memory" in memory_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_injection_failure_does_not_crash(self, engine: AgentEngine) -> None:
        from naumi_agent.memory.session import Session

        engine._session = Session(title="failed memories")
        with patch.object(
            engine.long_term_memory, "recall_for_session",
            new_callable=AsyncMock, side_effect=RuntimeError("db down"),
        ):
            await engine._inject_relevant_memories("query")  # Should not raise

    @pytest.mark.asyncio
    async def test_run_triggers_injection(self, engine: AgentEngine) -> None:
        mock_response = ModelResponse(
            content="Done!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        with (
            patch.object(
                engine._router, "call", new_callable=AsyncMock,
                return_value=mock_response,
            ),
            patch.object(
                engine.long_term_memory, "recall_for_session",
                new_callable=AsyncMock, return_value=[],
            ) as mock_recall_for_session,
        ):
            await engine.run("do something")

        mock_recall_for_session.assert_awaited_once()


class TestPlanInjection:
    """Tests for plan guidance injection into the ReAct loop."""

    @pytest.mark.asyncio
    async def test_multi_step_plan_injected(self, engine: AgentEngine) -> None:
        plan = Plan(
            understanding="analyze code",
            approach="multi-step analysis",
            steps=[
                Step(
                    id="s1", description="read files", tool="file_read",
                    depends_on=[], parallelizable=False, complexity=Complexity.SIMPLE,
                ),
                Step(
                    id="s2", description="write report", tool="file_write",
                    depends_on=["s1"], parallelizable=False, complexity=Complexity.SIMPLE,
                ),
            ],
            mode=ExecutionMode.PROMPT_CHAIN,
        )

        mock_response = ModelResponse(
            content="Done!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine._react_loop(
                engine.tool_registry.get_openai_tools(), plan=plan,
            )

        assert result.status == "completed"
        system_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "执行计划指导" in m.get("content", "")
        ]
        assert len(system_msgs) == 1
        assert "multi-step analysis" in system_msgs[0]["content"]
        assert "不要中途切换到其他方案" not in system_msgs[0]["content"]
        assert "可以调整下一步" in system_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_simple_plan_not_injected(self, engine: AgentEngine) -> None:
        plan = Plan(
            understanding="simple task",
            approach="直接执行",
            steps=[
                Step(
                    id="s1", description="do it", tool=None,
                    depends_on=[], parallelizable=False, complexity=Complexity.SIMPLE,
                ),
            ],
            mode=ExecutionMode.SINGLE_TURN,
        )

        mock_response = ModelResponse(
            content="Done!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine._react_loop(
                engine.tool_registry.get_openai_tools(), plan=plan,
            )

        assert result.status == "completed"
        system_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "执行计划指导" in m.get("content", "")
        ]
        assert len(system_msgs) == 0

    @pytest.mark.asyncio
    async def test_no_plan_no_injection(self, engine: AgentEngine) -> None:
        mock_response = ModelResponse(
            content="Done!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine._react_loop(
                engine.tool_registry.get_openai_tools(), plan=None,
            )

        assert result.status == "completed"
        system_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "执行计划指导" in m.get("content", "")
        ]
        assert len(system_msgs) == 0


class TestStreamingStartupLatency:
    @pytest.mark.asyncio
    async def test_streaming_skips_preplanning_for_normal_turn(self, tmp_path) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            yield StreamChunk(token="ok")
            yield StreamChunk(finish_reason="stop")

        try:
            with (
                patch.object(engine._router, "stream", new=stream_response),
                patch.object(
                    engine._planner,
                    "plan",
                    new_callable=AsyncMock,
                ) as mock_plan,
                patch.object(
                    engine.long_term_memory,
                    "recall",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
            ):
                result = await engine.run_streaming("直接回答这个问题", on_event)

            assert result.status == "completed"
            assert result.response == "ok"
            mock_plan.assert_not_awaited()
            planning = [
                data for event, data in events
                if event == "perf_phase" and data.get("phase") == "planning"
            ]
            assert planning[-1]["mode"] == "skipped_for_streaming"
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_emits_end_to_end_latency_metrics(self, tmp_path) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            yield StreamChunk(token="ok")
            yield StreamChunk(finish_reason="stop")

        try:
            with (
                patch.object(engine._router, "stream", new=stream_response),
                patch.object(
                    engine.long_term_memory,
                    "recall",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
            ):
                result = await engine.run_streaming("直接回答这个问题", on_event)

            assert result.status == "completed"
            metrics = [
                data for event, data in events
                if event == "latency_metric"
            ]
            assert [data["metric"] for data in metrics] == [
                "first_progress",
                "first_model_chunk",
                "first_token",
            ]
            assert metrics[0]["label"] == "首反馈"
            assert metrics[1]["label"] == "模型首包"
            assert metrics[2]["label"] == "端到端首字"
            assert all(isinstance(data["duration_ms"], int) for data in metrics)
            assert all(data["duration_ms"] >= 0 for data in metrics)
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_streaming_preplans_when_user_explicitly_requests_orchestration(
        self,
        tmp_path,
    ) -> None:
        config = AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        )
        engine = AgentEngine(config)
        events: list[tuple[str, dict[str, object]]] = []
        plan = Plan(
            understanding="orchestrate",
            approach="direct",
            steps=[
                Step(
                    id="s1",
                    description="do it",
                    tool=None,
                    depends_on=[],
                    parallelizable=False,
                    complexity=Complexity.SIMPLE,
                ),
            ],
            mode=ExecutionMode.SINGLE_TURN,
        )

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        async def stream_response(**_: object):
            yield StreamChunk(token="ok")
            yield StreamChunk(finish_reason="stop")

        try:
            with (
                patch.object(engine._router, "stream", new=stream_response),
                patch.object(
                    engine._planner,
                    "plan",
                    new_callable=AsyncMock,
                    return_value=plan,
                ) as mock_plan,
                patch.object(
                    engine.long_term_memory,
                    "recall",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
            ):
                result = await engine.run_streaming("请先规划并任务分解这个改造", on_event)

            assert result.status == "completed"
            mock_plan.assert_awaited_once()
            planning = [
                data for event, data in events
                if event == "perf_phase" and data.get("phase") == "planning"
            ]
            assert planning[-1]["mode"] == str(ExecutionMode.SINGLE_TURN)
        finally:
            await engine.shutdown()

    def test_openai_tool_schemas_are_cached_until_registry_changes(
        self,
        engine: AgentEngine,
    ) -> None:
        first = engine._get_openai_tools_cached()
        second = engine._get_openai_tools_cached()

        assert first is second

        engine.tool_registry.register(FakeTool())
        third = engine._get_openai_tools_cached()

        assert third is not first
        assert third is not None
        assert any(tool["function"]["name"] == "fake_tool" for tool in third)


class TestConvergenceMessagesDisabled:
    """Engine should not inject convergence-control messages."""

    @pytest.mark.asyncio
    async def test_tool_diversity_does_not_force_converge(self, engine: AgentEngine) -> None:
        call_count = 0
        tool_names = [
            "analysis_chaos", "analysis_scale", "analysis_state",
            "analysis_eval", "analysis_graph", "analysis_mcts",
        ]

        async def mock_call(**kwargs: object) -> ModelResponse:
            nonlocal call_count
            name = tool_names[call_count % len(tool_names)]
            call_count += 1
            return ModelResponse(
                content="",
                tool_calls=[{
                    "id": f"c{call_count}",
                    "function": {"name": name, "arguments": "{}"},
                }],
                usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
                model="test-model",
            )

        async def mock_execute(tc: ToolCall) -> ToolResult:
            return ToolResult(
                call_id=tc.id, status="success", content="mocked",
            )

        engine._config.safety.max_turns = 4

        with (
            patch.object(
                engine._router, "call", new_callable=AsyncMock,
                side_effect=mock_call,
            ),
            patch.object(
                engine, "_execute_tool", new_callable=AsyncMock,
                side_effect=mock_execute,
            ),
        ):
            result = await engine.run("展示一项能力")

        assert result.status == "max_turns"
        assert call_count >= 4
        assert not any(
            m.get("role") == "system"
            and (
                "频繁切换执行方案" in m.get("content", "")
                or "不要再调用任何工具" in m.get("content", "")
            )
            for m in engine._messages
        )

    @pytest.mark.asyncio
    async def test_tool_diversity_can_still_reach_final_response(
        self,
        engine: AgentEngine,
    ) -> None:
        call_count = 0
        tool_names = ["analysis_chaos", "analysis_scale", "analysis_state"]

        final_response = ModelResponse(
            content="最终结果",
            usage=TokenUsage(input_tokens=5, output_tokens=3, total_tokens=8, cost_usd=0.001),
            model="test-model",
        )

        async def mock_call(**kwargs: object) -> ModelResponse:
            nonlocal call_count
            if call_count >= 4:
                return final_response
            name = tool_names[call_count % len(tool_names)]
            call_count += 1
            return ModelResponse(
                content="",
                tool_calls=[{
                    "id": f"c{call_count}",
                    "function": {"name": name, "arguments": "{}"},
                }],
                usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
                model="test-model",
            )

        async def mock_execute(tc: ToolCall) -> ToolResult:
            return ToolResult(call_id=tc.id, status="success", content="mocked")

        with (
            patch.object(
                engine._router, "call", new_callable=AsyncMock,
                side_effect=mock_call,
            ),
            patch.object(
                engine, "_execute_tool", new_callable=AsyncMock,
                side_effect=mock_execute,
            ),
        ):
            result = await engine.run("test oscillation")

        assert result.status == "completed"
        assert result.response == "最终结果"
        assert not any(
            m.get("role") == "system"
            and (
                "频繁切换执行方案" in m.get("content", "")
                or "不要再调用任何工具" in m.get("content", "")
            )
            for m in engine._messages
        )

    @pytest.mark.asyncio
    async def test_subagent_workflow_has_no_convergence_message(
        self,
        engine: AgentEngine,
    ) -> None:
        responses = [
            ModelResponse(
                content="",
                tool_calls=[{
                    "id": "c1",
                    "function": {
                        "name": "spawn_agent",
                        "arguments": '{"name":"news_summarizer","domain":"news"}',
                    },
                }],
                usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                model="test-model",
            ),
            ModelResponse(
                content="",
                tool_calls=[{
                    "id": "c2",
                    "function": {
                        "name": "delegate_task",
                        "arguments": (
                            '{"agent":"news_summarizer",'
                            '"task":"搜索并总结热门新闻"}'
                        ),
                    },
                }],
                usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                model="test-model",
            ),
            ModelResponse(
                content="新闻总结完成",
                usage=TokenUsage(input_tokens=5, output_tokens=3, total_tokens=8),
                model="test-model",
            ),
        ]

        async def mock_execute(tc: ToolCall) -> ToolResult:
            return ToolResult(call_id=tc.id, status="success", content=f"{tc.name} ok")

        with (
            patch.object(
                engine._router,
                "call",
                new_callable=AsyncMock,
                side_effect=responses,
            ),
            patch.object(
                engine,
                "_execute_tool",
                new_callable=AsyncMock,
                side_effect=mock_execute,
            ),
        ):
            result = await engine.run("启动子 agent 对新闻进行总结")

        assert result.status == "completed"
        assert result.response == "新闻总结完成"
        assert not any(
            m.get("role") == "system" and "不要再调用任何工具" in m.get("content", "")
            for m in engine._messages
        )
