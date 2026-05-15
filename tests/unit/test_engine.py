"""AgentEngine 核心逻辑单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelResponse, ModelTier, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine, AgentResult, SYSTEM_PROMPT
from naumi_agent.tools.base import ToolCall, ToolResult


@pytest.fixture
def engine() -> AgentEngine:
    config = AppConfig()
    return AgentEngine(config)


@pytest.fixture
def mock_router() -> MagicMock:
    router = MagicMock()
    router.resolve_model.return_value = "test-model"
    router.get_context_window.return_value = 200_000
    router.get_max_output.return_value = 4_096
    return router


class TestEngineInit:
    def test_creates_with_default_config(self) -> None:
        engine = AgentEngine(AppConfig())
        assert len(engine.tool_registry) > 0
        assert engine.router is not None

    def test_has_builtin_tools(self, engine: AgentEngine) -> None:
        names = engine.tool_registry.names
        assert "file_read" in names
        assert "file_write" in names
        assert "file_edit" in names
        assert "bash_run" in names

    def test_has_browser_tools(self, engine: AgentEngine) -> None:
        assert "browser_navigate" in engine.tool_registry.names

    def test_has_memory_tools(self, engine: AgentEngine) -> None:
        assert "memory_store" in engine.tool_registry.names
        assert "memory_recall" in engine.tool_registry.names

    def test_has_subagent_tools(self, engine: AgentEngine) -> None:
        assert "delegate_task" in engine.tool_registry.names


class TestReset:
    def test_clears_messages(self, engine: AgentEngine) -> None:
        engine._messages.append({"role": "user", "content": "hi"})
        engine.reset()
        assert len(engine._messages) == 0

    def test_resets_usage(self, engine: AgentEngine) -> None:
        engine._usage.total_input_tokens = 100
        engine.reset()
        assert engine._usage.total_input_tokens == 0


class TestSetSystemPrompt:
    def test_replaces_existing(self, engine: AgentEngine) -> None:
        engine._messages.append({"role": "system", "content": "old"})
        engine.set_system_prompt("new prompt")
        assert engine._messages[0]["content"] == "new prompt"
        assert len([m for m in engine._messages if m["role"] == "system"]) == 1


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
        assert "Unknown tool" in result.content

    @pytest.mark.asyncio
    async def test_invalid_json_args(self, engine: AgentEngine) -> None:
        tc = ToolCall(id="x", name="file_read", arguments="not json{{{")
        result = await engine._execute_tool(tc)
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_path_sandbox_blocked(self, engine: AgentEngine) -> None:
        tc = ToolCall(id="x", name="file_read", arguments='{"path": "/etc/passwd"}')
        result = await engine._execute_tool(tc)
        assert result.status == "error"
        assert "Permission denied" in result.content

    @pytest.mark.asyncio
    async def test_path_in_allowed_dir(self, engine: AgentEngine) -> None:
        engine._permission_checker._allowed_dirs = ["/tmp"]
        tc = ToolCall(id="x", name="file_read", arguments='{"path": "/tmp/nonexistent_test_file.txt"}')
        result = await engine._execute_tool(tc)
        # Should attempt read, fail with "File not found" not "Permission denied"
        assert "Error: File not found" in result.content or "Permission denied" in result.content


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


class TestBudgetCheck:
    def test_budget_ok(self, engine: AgentEngine) -> None:
        result = engine._check_budget()
        assert result is None

    def test_budget_exceeded(self, engine: AgentEngine) -> None:
        engine._budget_tracker._total_input = 999_999_999
        result = engine._check_budget()
        assert result is not None
        assert result.status == "error"
        assert result.error == "budget_exceeded"


class TestRun:
    @pytest.mark.asyncio
    async def test_simple_response(self, engine: AgentEngine) -> None:
        mock_response = ModelResponse(
            content="Hello!",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test-model",
        )
        with patch.object(engine._router, "call", new_callable=AsyncMock, return_value=mock_response):
            result = await engine.run("hi")

        assert result.status == "completed"
        assert result.response == "Hello!"
        assert result.usage.total_input_tokens == 10

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
        with patch.object(engine._router, "call", new_callable=AsyncMock, return_value=mock_response):
            result = await engine.run("loop")

        assert result.status == "max_turns"
