"""Goal Pursuit Loop tests."""

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


class TestPursuitPersistence:
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
        engine = _make_engine()
        tool = engine.tool_registry.get("pursue_goal")
        assert tool is not None
        assert tool.name == "pursue_goal"
        assert engine.tool_registry.get("pursuit_list") is not None
        assert engine.tool_registry.get("pursuit_status") is not None
        assert engine.tool_registry.get("pursuit_resume") is not None
        assert hasattr(engine, "pursuit_store")

    @pytest.mark.asyncio
    async def test_tool_execute_without_init(self) -> None:
        # Reset global
        import naumi_agent.tools.pursuit as pursuit_mod
        from naumi_agent.tools.pursuit import PursueTool
        pursuit_mod._global_pursuit_loop = None

        tool = PursueTool()
        result = await tool.execute(goal="test")
        assert "尚未初始化" in result

    @pytest.mark.asyncio
    async def test_pursuit_status_tool_reads_store(self, tmp_path) -> None:
        import naumi_agent.tools.pursuit as pursuit_mod
        from naumi_agent.tools.pursuit import PursuitStatusTool

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
