"""Goal Pursuit Loop tests."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.agents.base import AgentResult
from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.pursuit import (
    CriterionStatus,
    GoalPursuitLoop,
    GoalSpec,
    GoalStatus,
    IterationCheckpoint,
    PursuitConfig,
    SuccessCriterion,
)
from naumi_agent.orchestrator.subagent_manager import SubAgentManager
from naumi_agent.tools.base import ToolRegistry


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
        assert config.max_iterations == 30
        assert config.max_budget_usd == 5.0
        assert config.stagnation_threshold == 3


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
        engine = _make_engine()
        tool = engine.tool_registry.get("pursue_goal")
        assert tool is not None
        assert tool.name == "pursue_goal"

    @pytest.mark.asyncio
    async def test_tool_execute_without_init(self) -> None:
        from naumi_agent.tools.pursuit import PursueTool

        # Reset global
        import naumi_agent.tools.pursuit as pursuit_mod
        pursuit_mod._global_pursuit_loop = None

        tool = PursueTool()
        result = await tool.execute(goal="test")
        assert "尚未初始化" in result
