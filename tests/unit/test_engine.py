"""AgentEngine 核心逻辑单元测试."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.agents.base import AgentResult
from naumi_agent.agents.team_protocol import execute_team_signal
from naumi_agent.config.settings import AppConfig, MemoryConfig, SafetyConfig
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
    PermissionMode,
    PermissionReasonCode,
)
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tools.base import Tool, ToolCall, ToolResult


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
        engine._permission_checker = PermissionChecker(PermissionMode.BYPASS)
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
        engine._permission_checker._allowed_dirs = ["/tmp"]
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
        payloads: list[dict[str, object]] = []

        async def confirm(payload: dict[str, object]) -> str:
            payloads.append(payload)
            return "allow"

        engine.set_permission_confirmer(confirm)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo confirm_ok"}')
        result = await engine._execute_tool(tc)

        assert result.status == "success"
        assert "confirm_ok" in result.content
        assert payloads
        assert payloads[0]["tool_name"] == "bash_run"

    @pytest.mark.asyncio
    async def test_confirmation_callback_can_enable_bypass(self, engine: AgentEngine) -> None:
        async def confirm(payload: dict[str, object]) -> str:
            return "bypass"

        engine.set_permission_confirmer(confirm)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo bypass_now"}')
        result = await engine._execute_tool(tc)

        assert result.status == "success"
        assert "bypass_now" in result.content
        assert engine.permission_mode == PermissionMode.BYPASS
        assert engine.runtime_mode == AgentRuntimeMode.BYPASS

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
        async def confirm(payload: dict[str, object]) -> str:
            return "deny"

        engine.set_permission_confirmer(confirm)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo denied"}')
        result = await engine._execute_tool(tc)

        assert result.status == "error"
        assert "用户已拒绝" in result.content

    @pytest.mark.asyncio
    async def test_top_level_permission_bubble_emitted_for_confirmation(
        self,
        engine: AgentEngine,
    ) -> None:
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

    @pytest.mark.asyncio
    async def test_bypass_mode_runs_confirmation_tool(self, engine: AgentEngine) -> None:
        engine._permission_checker = PermissionChecker(PermissionMode.BYPASS)
        tc = ToolCall(id="x", name="bash_run", arguments='{"command": "echo bypass_ok"}')
        result = await engine._execute_tool(tc)
        assert result.status == "success"
        assert "bypass_ok" in result.content

    @pytest.mark.asyncio
    async def test_bypass_mode_runs_dangerous_shell_command(
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
            return_value=AgentResult(status="completed", response="检查完成"),
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
        engine._budget_tracker._total_input = 999_999_999
        result = engine._check_budget()
        assert result is not None
        assert result.status == "budget_exceeded"
        assert "预算已耗尽" in result.response
        assert "输入 token" in result.response

    def test_bypass_mode_tracks_budget_without_stopping(self) -> None:
        config = AppConfig(safety=SafetyConfig(permission_mode="bypass"))
        engine = AgentEngine(config)
        engine._budget_tracker._total_input = 999_999_999
        assert engine._check_budget() is None

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
    async def test_injects_relevant_memories(self, engine: AgentEngine) -> None:
        from naumi_agent.memory.long_term import MemoryEntry, MemorySearchResult

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
            engine.long_term_memory, "recall",
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
        with patch.object(
            engine.long_term_memory, "recall",
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
            engine.long_term_memory, "recall",
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
        with patch.object(
            engine.long_term_memory, "recall",
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
                engine.long_term_memory, "recall",
                new_callable=AsyncMock, return_value=[],
            ) as mock_recall,
        ):
            await engine.run("do something")

        mock_recall.assert_called_once()


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
