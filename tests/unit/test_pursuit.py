"""Goal Pursuit Loop tests."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.pursuit import (
    CriterionStatus,
    GoalPursuitLoop,
    GoalSpec,
    GoalStatus,
    IterationCheckpoint,
    PursuitBackgroundWait,
    PursuitConfig,
    PursuitEvidence,
    PursuitRun,
    PursuitRunStatus,
    SuccessCriterion,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore, format_run
from naumi_agent.orchestrator.subagent_manager import SubAgentManager
from naumi_agent.tools.base import ToolResult
from naumi_agent.tools.pursuit import (
    PursueTool,
    PursuitListTool,
    PursuitResumeTool,
    PursuitStatusTool,
)


def _make_engine() -> AgentEngine:
    return AgentEngine(AppConfig())


def _make_spec(
    goal: str = "test goal",
    criteria: list[SuccessCriterion] | None = None,
) -> GoalSpec:
    return GoalSpec(
        original_goal=goal,
        description=goal,
        success_criteria=criteria or [
            SuccessCriterion(
                id="c1",
                description="test criterion",
                verification_command="echo ok",
            ),
        ],
        constraints={},
    )


def _make_checkpoint(
    iteration: int = 1,
    convergence: float = 0.5,
    gaps: list[str] | None = None,
) -> IterationCheckpoint:
    return IterationCheckpoint(
        iteration=iteration,
        timestamp=time.time(),
        assessment="test assessment",
        gaps_found=gaps or ["gap1"],
        actions_planned=["action1"],
        actions_taken=["action1"],
        verification_results=[],
        criteria_status={"c1": "in_progress"},
        convergence_score=convergence,
    )


class TestDataStructures:
    def test_success_criterion_defaults(self) -> None:
        c = SuccessCriterion(
            id="c1",
            description="test",
            verification_command="echo test",
        )
        assert c.status == CriterionStatus.NOT_STARTED
        assert c.evidence == ""
        assert c.last_checked == 0.0

    def test_goal_spec_defaults(self) -> None:
        spec = _make_spec()
        assert spec.estimated_complexity == "M"
        assert len(spec.success_criteria) == 1

    def test_criterion_status_values(self) -> None:
        assert CriterionStatus.VERIFIED == "verified"
        assert CriterionStatus.FAILED == "failed"
        assert CriterionStatus.IN_PROGRESS == "in_progress"

    def test_goal_status_values(self) -> None:
        assert GoalStatus.ACHIEVED == "achieved"
        assert GoalStatus.STUCK == "stuck"
        assert GoalStatus.BUDGET_EXCEEDED == "budget_exceeded"

    def test_pursuit_config_defaults(self) -> None:
        config = PursuitConfig()
        assert config.max_iterations == 1000
        assert config.max_budget_usd == float("inf")
        assert config.max_time_seconds == float("inf")
        assert config.stagnation_threshold == 3

    def test_pursuit_evidence_defaults(self) -> None:
        evidence = PursuitEvidence(
            kind="criterion",
            source="c1",
            summary="Command output: ok",
            is_hard=True,
        )
        assert evidence.kind == "criterion"
        assert evidence.is_hard

    def test_pursuit_run_status_values(self) -> None:
        assert PursuitRunStatus.RUNNING == "running"
        assert PursuitRunStatus.BLOCKED == "blocked"
        assert PursuitRunStatus.COMPLETED == "completed"


class TestStagnationDetection:
    def test_no_history_not_stagnant(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        assert loop._is_stagnant() is False

    def test_few_iterations_not_stagnant(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._history = [_make_checkpoint(convergence=0.3)]
        assert loop._is_stagnant() is False

    def test_stagnant_same_convergence(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._history = [
            _make_checkpoint(iteration=i, convergence=0.3)
            for i in range(3)
        ]
        assert loop._is_stagnant() is True

    def test_not_stagnant_if_improving(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._history = [
            _make_checkpoint(iteration=1, convergence=0.3),
            _make_checkpoint(iteration=2, convergence=0.5),
            _make_checkpoint(iteration=3, convergence=0.7),
        ]
        assert loop._is_stagnant() is False

    def test_stagnant_if_decreasing(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._history = [
            _make_checkpoint(iteration=1, convergence=0.7),
            _make_checkpoint(iteration=2, convergence=0.5),
            _make_checkpoint(iteration=3, convergence=0.3),
        ]
        assert loop._is_stagnant() is True


class TestGoalParsing:
    @pytest.mark.asyncio
    async def test_parse_goal_with_criteria(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        # Mock LLM to return structured criteria
        mock_response = MagicMock()
        mock_response.content = (
            "### Description\nCreate a CSV export tool\n\n"
            "### Criteria\n"
            "CRITERION|c1|CSV export function exists|grep -r 'def export_csv' src/\n"
            "CRITERION|c2|Tests pass|pytest tests/\n"
            "CRITERION|c3|Ruff check clean|ruff check src/\n\n"
            "### Constraints\n"
            "- Must support UTF-8\n\n"
            "### Complexity\nL"
        )
        mock_response.usage = MagicMock(
            total_tokens=100, cost_usd=0.01,
        )

        loop._router = MagicMock()
        loop._router.call = AsyncMock(return_value=mock_response)

        spec = await loop._parse_goal("创建 CSV 导出工具")
        assert spec.original_goal == "创建 CSV 导出工具"
        assert len(spec.success_criteria) == 3
        assert spec.success_criteria[0].id == "c1"
        assert spec.success_criteria[1].verification_command == "pytest tests/"

    @pytest.mark.asyncio
    async def test_parse_goal_fallback(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        # Mock LLM to return unstructured content
        mock_response = MagicMock()
        mock_response.content = "Just some random text without criteria"
        mock_response.usage = MagicMock(
            total_tokens=50, cost_usd=0.005,
        )

        loop._router = MagicMock()
        loop._router.call = AsyncMock(return_value=mock_response)

        spec = await loop._parse_goal("模糊的目标")
        assert len(spec.success_criteria) == 1
        assert spec.success_criteria[0].id == "c1"


class TestVerification:
    @pytest.mark.asyncio
    async def test_verify_with_passing_command(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        spec = _make_spec()

        # Mock bash_run tool
        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(return_value="ok\nall passed")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        await loop._verify_criteria(spec)
        assert spec.success_criteria[0].status == CriterionStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_verify_uses_injected_tool_executor_for_bash_command(self) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-verify-c1",
                status="success",
                content="ok\n[exit code: 0]",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        spec = _make_spec()

        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(return_value="direct should not run")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        await loop._verify_criteria(spec)

        assert spec.success_criteria[0].status == CriterionStatus.VERIFIED
        mock_bash.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "bash_run"
        assert json.loads(tool_call.arguments) == {"command": "echo ok"}

    @pytest.mark.asyncio
    async def test_verify_uses_worktree_cwd_when_available(self) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-verify-c1",
                status="success",
                content="ok\n[exit code: 0]",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="修改 src/demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path="/tmp/pursue-demo",
        )
        spec = _make_spec()

        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(return_value="direct should not run")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        await loop._verify_criteria(spec)

        assert spec.success_criteria[0].status == CriterionStatus.VERIFIED
        mock_bash.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "bash_run"
        assert json.loads(tool_call.arguments) == {
            "command": "echo ok",
            "cwd": "/tmp/pursue-demo",
        }

    @pytest.mark.asyncio
    async def test_verify_with_failing_command(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        spec = _make_spec()

        # Mock bash_run tool that returns error
        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(
            return_value="Error: test failed\nFAIL AssertionError",
        )
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        await loop._verify_criteria(spec)
        assert spec.success_criteria[0].status != CriterionStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_verify_trusts_zero_exit_output_even_when_text_mentions_fail(
        self,
    ) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        spec = _make_spec()
        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(
            return_value="pytest collected marker named failover\n0 passed, 0 failed",
        )
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        await loop._verify_criteria(spec)

        assert spec.success_criteria[0].status == CriterionStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_verify_fails_on_nonzero_exit_code_even_without_error_words(
        self,
    ) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        spec = _make_spec()
        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(return_value="done\n[exit code: 1]")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        await loop._verify_criteria(spec)

        assert spec.success_criteria[0].status == CriterionStatus.FAILED

    @pytest.mark.asyncio
    async def test_final_verification_all_verified(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        spec = _make_spec()
        spec.success_criteria[0].status = CriterionStatus.VERIFIED

        # Mock bash to keep it verified
        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(return_value="ok")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        result = await loop._final_verification(spec)
        assert result is True

    @pytest.mark.asyncio
    async def test_completion_requires_hard_evidence(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        spec = _make_spec()
        spec.success_criteria[0].status = CriterionStatus.VERIFIED
        spec.success_criteria[0].evidence = "看起来已经完成"

        decision = await loop._completion_decision(spec)

        assert decision.status == PursuitRunStatus.RUNNING
        assert "强证据" in decision.reason

    @pytest.mark.asyncio
    async def test_final_verification_reruns_llm_verified_criterion(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )

        spec = _make_spec()
        spec.success_criteria[0].status = CriterionStatus.VERIFIED
        spec.success_criteria[0].evidence = "评估器声称完成"

        mock_bash = MagicMock()
        mock_bash.execute = AsyncMock(return_value="FAIL AssertionError")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=mock_bash)

        result = await loop._final_verification(spec)

        assert result is False
        assert spec.success_criteria[0].status == CriterionStatus.FAILED
        mock_bash.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pursue_records_blocked_when_no_actions(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            config=PursuitConfig(max_iterations=3),
        )
        spec = _make_spec()
        checkpoint = _make_checkpoint()

        loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
        loop._assess = AsyncMock(return_value={"checkpoint": checkpoint, "gaps": ["gap"]})  # type: ignore[method-assign]
        loop._plan = AsyncMock(return_value=[])  # type: ignore[method-assign]
        loop._generate_report = AsyncMock(return_value="报告")  # type: ignore[method-assign]

        result = await loop.pursue("需要明确阻塞的目标")

        assert result == "报告"
        assert loop._run is not None
        assert loop._run.status == PursuitRunStatus.BLOCKED
        assert "没有给出下一步" in loop._run.blocked_reason


class TestPursuitExecutionStrategy:
    @pytest.mark.asyncio
    async def test_code_goal_creates_worktree_when_available(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="修改 src/demo.py 并添加 tests/test_demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
        )
        spec = _make_spec(
            goal="修改 src/demo.py 并添加 tests/test_demo.py",
            criteria=[
                SuccessCriterion(
                    id="c1",
                    description="代码和测试已更新",
                    verification_command="pytest tests/test_demo.py",
                )
            ],
        )
        worktree = MagicMock()
        worktree.execute = AsyncMock(
            return_value=(
                "已创建隔离 worktree。\n\n"
                "### Worktree: pursue-demo\n"
                "- 路径：`/tmp/pursue-demo`\n"
            )
        )
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=worktree)

        await loop._ensure_worktree_for_code_goal(spec)

        assert loop._run.worktree_name.startswith("pursue-")
        assert loop._run.worktree_path == "/tmp/pursue-demo"
        worktree.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_code_goal_uses_injected_executor_for_worktree(self) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-worktree",
                status="success",
                content=(
                    "已创建隔离 worktree。\n\n"
                    "### Worktree: pursue-demo\n"
                    "- 路径：`/tmp/pursue-demo`\n"
                ),
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="修改 src/demo.py 并添加 tests/test_demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
        )
        spec = _make_spec(
            goal="修改 src/demo.py 并添加 tests/test_demo.py",
            criteria=[
                SuccessCriterion(
                    id="c1",
                    description="代码和测试已更新",
                    verification_command="pytest tests/test_demo.py",
                )
            ],
        )
        worktree = MagicMock()
        worktree.execute = AsyncMock(return_value="direct should not run")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=worktree)

        await loop._ensure_worktree_for_code_goal(spec)

        assert loop._run.worktree_name.startswith("pursue-")
        assert loop._run.worktree_path == "/tmp/pursue-demo"
        worktree.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "worktree_create"
        assert json.loads(tool_call.arguments) == {"name": loop._run.worktree_name}

    @pytest.mark.asyncio
    async def test_long_bash_action_runs_in_background_and_schedules_followup(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="运行完整测试",
            status=PursuitRunStatus.RUNNING,
            phase="execute",
            started_at=time.time(),
            updated_at=time.time(),
        )
        loop._llm_call = AsyncMock(return_value="python -m pytest tests/ -q")  # type: ignore[method-assign]

        bash = MagicMock()
        bash.execute = AsyncMock(return_value="should not run synchronously")
        background = MagicMock()
        background.execute = AsyncMock(return_value="后台任务已启动。\n\n- 任务 ID：`bg_0001`")
        schedule = MagicMock()
        schedule.execute = AsyncMock(return_value="调度任务已创建。")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(
            side_effect=lambda name: {
                "background_run": background,
                "schedule_create": schedule,
            }.get(name)
        )

        result = await loop._execute_via_bash(bash, "Run pytest tests/ slowly", "a1")

        assert result["status"] == "waiting"
        assert result["background_task_id"] == "bg_0001"
        assert loop._pending_background[0].task_id == "bg_0001"
        assert loop._run.waiting_on[0].task_id == "bg_0001"
        loop._record_action_evidence([result])
        assert loop._run.failure_count == 0
        bash.execute.assert_not_awaited()
        background.execute.assert_awaited_once()
        schedule.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_long_bash_action_uses_injected_executor_for_background(self) -> None:
        engine = _make_engine()

        async def execute_tool_call(call):
            if call.name == "background_run":
                return ToolResult(
                    call_id=call.id,
                    status="success",
                    content="后台任务已启动。\n\n- 任务 ID：`bg_0002`",
                )
            if call.name == "schedule_create":
                return ToolResult(
                    call_id=call.id,
                    status="success",
                    content="调度任务已创建。",
                )
            return ToolResult(
                call_id=call.id,
                status="error",
                content=f"unexpected tool: {call.name}",
            )

        executor = AsyncMock(side_effect=execute_tool_call)
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=executor,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="运行完整测试",
            status=PursuitRunStatus.RUNNING,
            phase="execute",
            started_at=time.time(),
            updated_at=time.time(),
        )
        loop._llm_call = AsyncMock(return_value="python -m pytest tests/ -q")  # type: ignore[method-assign]
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=None)
        bash = MagicMock()
        bash.execute = AsyncMock(return_value="direct should not run")

        result = await loop._execute_via_bash(bash, "Run pytest tests/ slowly", "a1")

        assert result["status"] == "waiting"
        assert result["background_task_id"] == "bg_0002"
        assert loop._pending_background[0].task_id == "bg_0002"
        bash.execute.assert_not_awaited()
        tool_calls = executor.await_args_list
        assert [call.args[0].name for call in tool_calls] == [
            "background_run",
            "schedule_create",
        ]
        background_args = json.loads(tool_calls[0].args[0].arguments)
        assert background_args == {
            "command": "python -m pytest tests/ -q",
            "cwd": "",
            "timeout_seconds": 1800,
        }

    @pytest.mark.asyncio
    async def test_bash_action_treats_multi_digit_exit_code_as_error(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._llm_call = AsyncMock(return_value="python -m pytest")  # type: ignore[method-assign]
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=None)
        bash = MagicMock()
        bash.execute = AsyncMock(return_value="done\n[exit code: 10]")

        result = await loop._execute_via_bash(bash, "Run pytest", "a1")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_bash_action_uses_injected_tool_executor(self) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="done\n[exit code: 0]",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._llm_call = AsyncMock(return_value="python -m pytest")  # type: ignore[method-assign]
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=None)
        bash = MagicMock()
        bash.execute = AsyncMock(return_value="direct should not run")

        result = await loop._execute_via_bash(bash, "Run pytest", "a1")

        assert result["status"] == "completed"
        bash.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.id == "pursuit-a1"
        assert tool_call.name == "bash_run"
        assert json.loads(tool_call.arguments) == {"command": "python -m pytest"}

    @pytest.mark.asyncio
    async def test_bash_action_uses_worktree_cwd_when_available(self) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="done\n[exit code: 0]",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="修改 src/demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path="/tmp/pursue-demo",
        )
        loop._llm_call = AsyncMock(return_value="python -m pytest")  # type: ignore[method-assign]
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=None)
        bash = MagicMock()
        bash.execute = AsyncMock(return_value="direct should not run")

        result = await loop._execute_via_bash(bash, "Run pytest", "a1")

        assert result["status"] == "completed"
        bash.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "bash_run"
        assert json.loads(tool_call.arguments) == {
            "command": "python -m pytest",
            "cwd": "/tmp/pursue-demo",
        }

    @pytest.mark.asyncio
    async def test_file_edit_uses_file_edit_tool_instead_of_direct_write(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        target = tmp_path / "demo.py"
        original = "x = 1\n"
        target.write_text(original, encoding="utf-8")
        loop._llm_call = AsyncMock(  # type: ignore[method-assign]
            return_value="[SEARCH]\nx = 1\n[REPLACE]\nx = 2\n[END]"
        )
        monkeypatch.setattr(loop, "_extract_target_path", lambda description: str(target))
        edit_tool = MagicMock()
        edit_tool.execute = AsyncMock(return_value="✅ 已编辑")

        result = await loop._execute_file_edit(
            edit_tool,
            f"修改 {target} 把 x 改成 2",
            "a1",
        )

        assert result["status"] == "completed"
        edit_tool.execute.assert_awaited_once_with(
            path=str(target),
            old_text="x = 1",
            new_text="x = 2",
        )
        assert target.read_text(encoding="utf-8") == original

    @pytest.mark.asyncio
    async def test_file_edit_uses_injected_tool_executor(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1-edit-1",
                status="success",
                content="✅ 已编辑",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        target = tmp_path / "demo.py"
        original = "x = 1\n"
        target.write_text(original, encoding="utf-8")
        loop._llm_call = AsyncMock(  # type: ignore[method-assign]
            return_value="[SEARCH]\nx = 1\n[REPLACE]\nx = 2\n[END]"
        )
        monkeypatch.setattr(loop, "_extract_target_path", lambda description: str(target))
        edit_tool = MagicMock()
        edit_tool.execute = AsyncMock(return_value="direct edit should not run")

        result = await loop._execute_file_edit(
            edit_tool,
            f"修改 {target} 把 x 改成 2",
            "a1",
        )

        assert result["status"] == "completed"
        edit_tool.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "file_edit"
        assert json.loads(tool_call.arguments) == {
            "path": str(target),
            "old_text": "x = 1",
            "new_text": "x = 2",
        }
        assert target.read_text(encoding="utf-8") == original

    @pytest.mark.asyncio
    async def test_file_edit_rebases_relative_path_to_worktree(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1-edit-1",
                status="success",
                content="✅ 已编辑",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        main_target = tmp_path / "demo.py"
        main_target.write_text("x = 100\n", encoding="utf-8")
        worktree_path = tmp_path / "pursue-demo"
        worktree_path.mkdir()
        worktree_target = worktree_path / "demo.py"
        worktree_target.write_text("x = 1\n", encoding="utf-8")

        seen_prompts: list[str] = []

        async def fake_llm_call(system: str, prompt: str) -> str:
            seen_prompts.append(prompt)
            return "[SEARCH]\nx = 1\n[REPLACE]\nx = 2\n[END]"

        loop._run = PursuitRun(
            id="pursuit_test",
            goal="修改 demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path=str(worktree_path),
        )
        loop._llm_call = AsyncMock(side_effect=fake_llm_call)  # type: ignore[method-assign]
        monkeypatch.setattr(loop, "_extract_target_path", lambda description: "demo.py")
        edit_tool = MagicMock()
        edit_tool.execute = AsyncMock(return_value="direct edit should not run")

        result = await loop._execute_file_edit(
            edit_tool,
            "修改 demo.py 把 x 改成 2",
            "a1",
        )

        assert result["status"] == "completed"
        assert seen_prompts
        assert "x = 1" in seen_prompts[0]
        assert "x = 100" not in seen_prompts[0]
        edit_tool.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "file_edit"
        assert json.loads(tool_call.arguments) == {
            "path": str(worktree_target),
            "old_text": "x = 1",
            "new_text": "x = 2",
        }
        assert main_target.read_text(encoding="utf-8") == "x = 100\n"
        assert worktree_target.read_text(encoding="utf-8") == "x = 1\n"

    @pytest.mark.asyncio
    async def test_file_write_uses_injected_tool_executor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="✅ 已写入",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._llm_call = AsyncMock(return_value="x = 2\n")  # type: ignore[method-assign]
        monkeypatch.setattr(loop, "_extract_target_path", lambda description: "demo.py")
        file_write = MagicMock()
        file_write.execute = AsyncMock(return_value="direct write should not run")

        result = await loop._execute_file_write(
            file_write,
            "创建 demo.py",
            "a1",
        )

        assert result["status"] == "completed"
        file_write.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "file_write"
        arguments = json.loads(tool_call.arguments)
        assert arguments == {"path": "demo.py", "content": "x = 2"}

    @pytest.mark.asyncio
    async def test_file_write_rebases_relative_path_to_worktree(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="✅ 已写入",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        worktree_path = tmp_path / "pursue-demo"
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="创建 demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path=str(worktree_path),
        )
        loop._llm_call = AsyncMock(return_value="x = 2\n")  # type: ignore[method-assign]
        monkeypatch.setattr(loop, "_extract_target_path", lambda description: "demo.py")
        file_write = MagicMock()
        file_write.execute = AsyncMock(return_value="direct write should not run")

        result = await loop._execute_file_write(
            file_write,
            "创建 demo.py",
            "a1",
        )

        assert result["status"] == "completed"
        file_write.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "file_write"
        assert json.loads(tool_call.arguments) == {
            "path": str(worktree_path / "demo.py"),
            "content": "x = 2",
        }

    @pytest.mark.asyncio
    async def test_file_write_rejects_worktree_path_escape(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="创建 ../escape.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path="/tmp/pursue-demo",
        )
        loop._llm_call = AsyncMock(return_value="x = 2\n")  # type: ignore[method-assign]
        monkeypatch.setattr(loop, "_extract_target_path", lambda description: "../escape.py")
        file_write = MagicMock()
        file_write.execute = AsyncMock(return_value="direct write should not run")

        result = await loop._execute_file_write(
            file_write,
            "创建 ../escape.py",
            "a1",
        )

        assert result["status"] == "error"
        assert "worktree" in result["output"]
        file_write.execute.assert_not_awaited()
        execute_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generic_tool_uses_injected_tool_executor(self) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="工具已执行",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._llm_call = AsyncMock(return_value='{"query": "abc"}')  # type: ignore[method-assign]
        tool = MagicMock()
        tool.parameters_schema = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询"}},
            "required": ["query"],
        }
        tool.execute = AsyncMock(return_value="direct should not run")

        result = await loop._execute_generic_tool(
            tool,
            "custom_search",
            "搜索 abc",
            "a1",
        )

        assert result["status"] == "completed"
        assert result["output"] == "工具已执行"
        tool.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.id == "pursuit-a1"
        assert tool_call.name == "custom_search"
        assert json.loads(tool_call.arguments) == {"query": "abc"}

    @pytest.mark.asyncio
    async def test_file_read_generic_rebases_relative_path_to_worktree(
        self,
        tmp_path,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="x = 1\n",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        worktree_path = tmp_path / "pursue-demo"
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="读取 demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path=str(worktree_path),
        )
        loop._llm_call = AsyncMock(return_value='{"path": "demo.py"}')  # type: ignore[method-assign]
        tool = MagicMock()
        tool.parameters_schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "路径"}},
            "required": ["path"],
        }
        tool.execute = AsyncMock(return_value="direct should not run")

        result = await loop._execute_generic_tool(
            tool,
            "file_read",
            "读取 demo.py",
            "a1",
        )

        assert result["status"] == "completed"
        tool.execute.assert_not_awaited()
        execute_tool_call.assert_awaited_once()
        tool_call = execute_tool_call.await_args.args[0]
        assert tool_call.name == "file_read"
        assert json.loads(tool_call.arguments) == {
            "path": str(worktree_path / "demo.py"),
        }

    @pytest.mark.asyncio
    async def test_file_read_generic_rejects_worktree_path_escape(
        self,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="读取 ../escape.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path="/tmp/pursue-demo",
        )
        loop._llm_call = AsyncMock(return_value='{"path": "../escape.py"}')  # type: ignore[method-assign]
        tool = MagicMock()
        tool.parameters_schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "路径"}},
            "required": ["path"],
        }
        tool.execute = AsyncMock(return_value="direct should not run")

        result = await loop._execute_generic_tool(
            tool,
            "file_read",
            "读取 ../escape.py",
            "a1",
        )

        assert result["status"] == "error"
        assert "worktree" in result["output"]
        tool.execute.assert_not_awaited()
        execute_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_read_generic_preserves_offset_and_limit(
        self,
        tmp_path,
    ) -> None:
        engine = _make_engine()
        execute_tool_call = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-a1",
                status="success",
                content="x = 1\n",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=execute_tool_call,
        )
        worktree_path = tmp_path / "pursue-demo"
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="读取 demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path=str(worktree_path),
        )
        loop._llm_call = AsyncMock(  # type: ignore[method-assign]
            return_value='{"path": "demo.py", "offset": 10, "limit": 20}'
        )
        tool = MagicMock()
        tool.parameters_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "路径"},
                "offset": {"type": "integer", "description": "偏移"},
                "limit": {"type": "integer", "description": "行数"},
            },
            "required": ["path"],
        }
        tool.execute = AsyncMock(return_value="direct should not run")

        await loop._execute_generic_tool(
            tool,
            "file_read",
            "读取 demo.py",
            "a1",
        )

        tool_call = execute_tool_call.await_args.args[0]
        assert json.loads(tool_call.arguments) == {
            "path": str(worktree_path / "demo.py"),
            "offset": 10,
            "limit": 20,
        }

    @pytest.mark.asyncio
    async def test_gather_state_evidence_uses_injected_bash_executor(self) -> None:
        engine = _make_engine()

        async def execute_tool_call(call):
            args = json.loads(call.arguments)
            command = args["command"]
            if command.startswith("git diff"):
                content = "src/demo.py | 2 ++"
            elif "cat src/demo.py" in command:
                content = "def demo():\n    return 1"
            elif "pytest" in command:
                content = "1 passed"
            elif "ruff" in command:
                content = "All checks passed!"
            else:
                content = f"unexpected command: {command}"
            return ToolResult(call_id=call.id, status="success", content=content)

        executor = AsyncMock(side_effect=execute_tool_call)
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=executor,
        )
        loop._start_time = time.time()
        bash = MagicMock()
        bash.execute = AsyncMock(return_value="direct should not run")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=bash)
        spec = _make_spec(
            goal="修改 src/demo.py",
            criteria=[
                SuccessCriterion(
                    id="c1",
                    description="测试通过",
                    verification_command="pytest tests/test_demo.py",
                )
            ],
        )

        evidence = await loop._gather_state_evidence(spec)

        assert "Git 变更" in evidence
        assert "文件 src/demo.py" in evidence
        assert "测试状态" in evidence
        assert "Lint 状态" in evidence
        bash.execute.assert_not_awaited()
        assert executor.await_count == 5
        assert all(call.args[0].name == "bash_run" for call in executor.await_args_list)

    @pytest.mark.asyncio
    async def test_gather_state_evidence_uses_worktree_cwd_when_available(self) -> None:
        engine = _make_engine()
        executor = AsyncMock(
            return_value=ToolResult(
                call_id="pursuit-evidence",
                status="success",
                content="ok",
            )
        )
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=executor,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="修改 src/demo.py",
            status=PursuitRunStatus.RUNNING,
            phase="test",
            started_at=time.time(),
            updated_at=time.time(),
            worktree_path="/tmp/pursue-demo",
        )
        loop._start_time = time.time()
        bash = MagicMock()
        bash.execute = AsyncMock(return_value="direct should not run")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(return_value=bash)
        spec = _make_spec(
            goal="修改 src/demo.py",
            criteria=[
                SuccessCriterion(
                    id="c1",
                    description="测试通过",
                    verification_command="pytest tests/test_demo.py",
                )
            ],
        )

        await loop._gather_state_evidence(spec)

        bash.execute.assert_not_awaited()
        assert executor.await_count == 5
        for call in executor.await_args_list:
            tool_call = call.args[0]
            assert tool_call.name == "bash_run"
            assert json.loads(tool_call.arguments)["cwd"] == "/tmp/pursue-demo"

    @pytest.mark.asyncio
    async def test_collect_background_results_records_hard_evidence(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="等待后台任务",
            status=PursuitRunStatus.WAITING,
            phase="waiting",
            started_at=time.time(),
            updated_at=time.time(),
        )
        loop._pending_background = [
            PursuitBackgroundWait(
                task_id="bg_0001",
                action_id="a1",
                command="python -m pytest tests/ -q",
                created_at=time.time(),
            )
        ]

        status = MagicMock()
        status.execute = AsyncMock(return_value="### 后台任务 bg_0001\n- 状态：已完成")
        output = MagicMock()
        output.execute = AsyncMock(return_value="1089 passed")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(
            side_effect=lambda name: {
                "background_status": status,
                "background_read_output": output,
            }.get(name)
        )

        await loop._collect_background_results()

        assert loop._pending_background == []
        assert loop._run.status == PursuitRunStatus.RUNNING
        assert any(
            evidence.kind == "background" and evidence.is_hard
            for evidence in loop._run.evidence
        )

    @pytest.mark.asyncio
    async def test_collect_background_results_uses_injected_tool_executor(self) -> None:
        engine = _make_engine()

        async def execute_tool_call(call):
            if call.name == "background_status":
                return ToolResult(
                    call_id=call.id,
                    status="success",
                    content="### 后台任务 bg_0001\n- 状态：已完成",
                )
            if call.name == "background_read_output":
                return ToolResult(
                    call_id=call.id,
                    status="success",
                    content="1089 passed",
                )
            return ToolResult(
                call_id=call.id,
                status="error",
                content=f"unexpected tool: {call.name}",
            )

        executor = AsyncMock(side_effect=execute_tool_call)
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            execute_tool_call=executor,
        )
        loop._run = PursuitRun(
            id="pursuit_test",
            goal="等待后台任务",
            status=PursuitRunStatus.WAITING,
            phase="waiting",
            started_at=time.time(),
            updated_at=time.time(),
        )
        loop._pending_background = [
            PursuitBackgroundWait(
                task_id="bg_0001",
                action_id="a1",
                command="python -m pytest tests/ -q",
                created_at=time.time(),
            )
        ]
        status = MagicMock()
        status.execute = AsyncMock(return_value="direct status should not run")
        output = MagicMock()
        output.execute = AsyncMock(return_value="direct output should not run")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(
            side_effect=lambda name: {
                "background_status": status,
                "background_read_output": output,
            }.get(name)
        )

        await loop._collect_background_results()

        assert loop._pending_background == []
        assert loop._run.status == PursuitRunStatus.RUNNING
        status.execute.assert_not_awaited()
        output.execute.assert_not_awaited()
        tool_calls = executor.await_args_list
        assert [call.args[0].name for call in tool_calls] == [
            "background_status",
            "background_read_output",
        ]
        assert json.loads(tool_calls[0].args[0].arguments) == {"task_id": "bg_0001"}
        assert json.loads(tool_calls[1].args[0].arguments) == {"task_id": "bg_0001"}


class TestPursuitPersistence:
    def test_store_constructor_does_not_touch_disk(self, tmp_path) -> None:
        base_dir = tmp_path / "pursuit"
        store = PursuitStore(base_dir)

        assert store.base_dir == base_dir.resolve()
        assert not base_dir.exists()

        assert store.list_runs() == []
        assert store.db_path.exists()

    def test_store_round_trips_run_evidence_and_waits(self, tmp_path) -> None:
        store = PursuitStore(tmp_path / "pursuit")
        now = time.time()
        run = PursuitRun(
            id="pursuit_1",
            goal="持久化目标",
            status=PursuitRunStatus.WAITING,
            phase="waiting",
            started_at=now,
            updated_at=now,
            iteration=2,
            criteria_total=3,
            criteria_verified=1,
            worktree_name="pursue-demo",
            worktree_path="/tmp/pursue-demo",
            waiting_on=[
                PursuitBackgroundWait(
                    task_id="bg_0001",
                    action_id="a1",
                    command="python -m pytest tests/ -q",
                    created_at=now,
                )
            ],
            evidence=[
                PursuitEvidence(
                    kind="background",
                    source="bg_0001",
                    summary="后台任务已启动",
                    is_hard=True,
                    timestamp=now,
                )
            ],
        )

        store.save_run(run)
        restored = store.get_run("pursuit_1")

        assert restored is not None
        assert restored.status == PursuitRunStatus.WAITING
        assert restored.waiting_on[0].task_id == "bg_0001"
        assert restored.evidence[0].summary == "后台任务已启动"
        assert "持久化目标" in format_run(restored)

    @pytest.mark.asyncio
    async def test_loop_persists_waiting_state_and_resume_collects_output(self, tmp_path) -> None:
        store = PursuitStore(tmp_path / "pursuit")
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
            store=store,
        )
        now = time.time()
        run = PursuitRun(
            id="pursuit_wait",
            goal="等待后台任务",
            status=PursuitRunStatus.WAITING,
            phase="waiting",
            started_at=now,
            updated_at=now,
            waiting_on=[
                PursuitBackgroundWait(
                    task_id="bg_0001",
                    action_id="a1",
                    command="echo done",
                    created_at=now,
                )
            ],
        )
        store.save_run(run)

        status = MagicMock()
        status.execute = AsyncMock(return_value="### 后台任务 bg_0001\n- 状态：已完成")
        output = MagicMock()
        output.execute = AsyncMock(return_value="done")
        loop._tools = MagicMock()
        loop._tools.get = MagicMock(
            side_effect=lambda name: {
                "background_status": status,
                "background_read_output": output,
            }.get(name)
        )

        result = await loop.resume_persisted("pursuit_wait")
        restored = store.get_run("pursuit_wait")

        assert "目标追踪状态已恢复" in result
        assert restored is not None
        assert restored.status == PursuitRunStatus.RUNNING
        assert restored.waiting_on == []
        assert any(item.kind == "background" and item.is_hard for item in restored.evidence)


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_sets_flag(self) -> None:
        engine = _make_engine()
        loop = GoalPursuitLoop(
            router=engine.router,
            tool_registry=engine.tool_registry,
            subagent_manager=SubAgentManager(engine),
        )
        assert loop._cancelled is False
        loop.cancel()
        assert loop._cancelled is True


class TestPursueToolRegistration:
    def test_tool_registered_in_engine(self) -> None:
        import naumi_agent.tools.pursuit as pursuit_mod

        engine = _make_engine()
        tool = engine.tool_registry.get("pursue_goal")
        assert tool is not None
        assert tool.name == "pursue_goal"
        assert engine.tool_registry.get("pursuit_list") is not None
        assert engine.tool_registry.get("pursuit_status") is not None
        assert engine.tool_registry.get("pursuit_resume") is not None
        assert hasattr(engine, "pursuit_store")
        assert pursuit_mod._global_pursuit_loop is not None
        assert pursuit_mod._global_pursuit_loop._execute_tool_call == engine.execute_tool

    def test_pursuit_tools_expose_permission_metadata(self) -> None:
        assert PursueTool().metadata.requires_confirmation is True
        assert PursueTool().metadata.destructive is True
        assert PursuitListTool().metadata.read_only is True
        assert PursuitListTool().metadata.concurrency_safe is True
        assert PursuitStatusTool().metadata.read_only is True
        assert PursuitResumeTool().metadata.requires_confirmation is True
        assert PursuitResumeTool().metadata.destructive is True

    @pytest.mark.asyncio
    async def test_tool_execute_without_init(self) -> None:
        # Reset global
        import naumi_agent.tools.pursuit as pursuit_mod
        pursuit_mod._global_pursuit_loop = None

        tool = PursueTool()
        result = await tool.execute(goal="test")
        assert "尚未初始化" in result

    @pytest.mark.asyncio
    async def test_pursue_tool_rejects_invalid_goal(self) -> None:
        result = await PursueTool().execute(goal="   ")
        assert "目标不能为空" in result

        result = await PursueTool().execute(goal="x" * 8001)
        assert "目标过长" in result

    @pytest.mark.asyncio
    async def test_pursue_tool_starts_background_run(self, tmp_path, monkeypatch) -> None:
        import naumi_agent.tools.pursuit as pursuit_mod

        store = PursuitStore(tmp_path / "pursuit")
        pursuit_mod._global_pursuit_loop = GoalPursuitLoop(
            router=MagicMock(),
            tool_registry=MagicMock(),
            subagent_manager=MagicMock(),
            store=store,
        )

        async def fake_pursue(self, goal: str) -> str:
            now = time.time()
            self._run = PursuitRun(
                id="pursuit_bg",
                goal=goal,
                status=PursuitRunStatus.RUNNING,
                phase="assess",
                started_at=now,
                updated_at=now,
            )
            self._persist_run()
            await asyncio.sleep(0)
            return "报告"

        monkeypatch.setattr(GoalPursuitLoop, "pursue", fake_pursue)

        result = await PursueTool().execute(goal="后台目标")

        assert "后台启动" in result
        assert "pursuit_bg" in result
        assert "1000 轮 / 无限 / 无限" in result
        assert store.get_run("pursuit_bg") is not None

    @pytest.mark.asyncio
    async def test_run_id_tools_reject_invalid_run_id(self) -> None:
        status = await PursuitStatusTool().execute(run_id="bad id")
        resume = await PursuitResumeTool().execute(run_id="bad id")

        assert "run_id 只能包含" in status
        assert "run_id 只能包含" in resume

    @pytest.mark.asyncio
    async def test_pursuit_status_tool_reads_store(self, tmp_path) -> None:
        import naumi_agent.tools.pursuit as pursuit_mod

        store = PursuitStore(tmp_path / "pursuit")
        now = time.time()
        store.save_run(PursuitRun(
            id="pursuit_status",
            goal="查询状态",
            status=PursuitRunStatus.RUNNING,
            phase="assess",
            started_at=now,
            updated_at=now,
        ))
        pursuit_mod._global_pursuit_loop = GoalPursuitLoop(
            router=MagicMock(),
            tool_registry=MagicMock(),
            subagent_manager=MagicMock(),
            store=store,
        )

        result = await PursuitStatusTool().execute(run_id="pursuit_status")

        assert "PursuitRun pursuit_status" in result
        assert "查询状态" in result
