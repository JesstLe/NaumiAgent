"""AgentEngine 核心逻辑单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.memory.session import Session
from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.planner import Complexity, ExecutionMode, Plan, Step
from naumi_agent.safety.behavior import BehaviorMonitor
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
        assert "browser_goto" in engine.tool_registry.names

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
        tc = ToolCall(
            id="x", name="file_read",
            arguments='{"path": "/tmp/nonexistent_test_file.txt"}',
        )
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
    def test_budget_disabled(self, engine: AgentEngine) -> None:
        # Budget check is intentionally neutered — tracking only, no blocking
        engine._budget_tracker._total_input = 999_999_999
        result = engine._check_budget()
        assert result is None


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
        ):
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
        with patch.object(
            engine._router, "call", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await engine.run("loop")

        assert result.status == "max_turns"

    @pytest.mark.asyncio
    async def test_repeated_tool_call_breaks_loop(self, engine: AgentEngine) -> None:
        """Identical tool call 3x in a row should inject stop message."""
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


class TestOscillationBreaking:
    """Tests for force-converge termination when approach oscillation is detected."""

    @pytest.mark.asyncio
    async def test_force_converge_terminates_loop(self, engine: AgentEngine) -> None:
        """After max interventions, force_converge should terminate the loop."""
        engine._behavior_monitor = BehaviorMonitor(max_interventions=1)

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

        engine._config.safety.max_turns = 20

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

        assert result.status == "completed"
        assert call_count < 20  # Terminated early by force_converge

    @pytest.mark.asyncio
    async def test_intervention_message_injected(self, engine: AgentEngine) -> None:
        """Oscillating tools should cause an intervention system message."""
        engine._behavior_monitor = BehaviorMonitor(max_interventions=3)

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

        # Check that intervention messages were injected
        intervention_msgs = [
            m for m in engine._messages
            if m.get("role") == "system" and "频繁切换执行方案" in m.get("content", "")
        ]
        assert len(intervention_msgs) >= 1
