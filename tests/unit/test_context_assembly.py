"""Harness context assembly tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequestBuilder,
)
from naumi_agent.evolution.adversarial_probe_contracts import (
    EvolutionAdversarialProbeContractBuilder,
)
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseManager,
    EvolutionExperimentLeaseStore,
)
from naumi_agent.evolution.experiment_snapshots import (
    EvolutionExperimentSourceSnapshotBuilder,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContractIssuer
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionBuilder,
    EvolutionFailureAttributionExecutor,
    EvolutionFailureAttributionStore,
)
from naumi_agent.evolution.mutation_generation import (
    EvolutionMutationGenerationService,
    EvolutionMutationGenerationTraceStore,
)
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlanner
from naumi_agent.evolution.mutation_receipts import (
    EvolutionMutationReceiptService,
    EvolutionMutationReceiptStore,
)
from naumi_agent.evolution.mutation_turns import EvolutionMutationTurnRunner
from naumi_agent.evolution.patch_journals import EvolutionPatchJournalStore
from naumi_agent.evolution.patch_recovery import (
    EvolutionPatchRecoveryCoordinator,
    EvolutionPatchSetRecoveryCoordinator,
)
from naumi_agent.evolution.patch_set_writers import EvolutionPatchSetWriter
from naumi_agent.evolution.patch_sets import EvolutionPatchSetStore
from naumi_agent.evolution.patch_writers import EvolutionPatchWriter
from naumi_agent.evolution.self_review_comparison import (
    EvolutionSelfReviewComparisonExecutor,
)
from naumi_agent.evolution.self_review_green_cohort import (
    EvolutionSelfReviewGreenCohortExecutor,
    EvolutionSelfReviewGreenCohortRequestBuilder,
)
from naumi_agent.evolution.self_review_red_baseline import (
    EvolutionSelfReviewRedBaselineExecutor,
)
from naumi_agent.evolution.static_guards import EvolutionStaticGuard
from naumi_agent.evolution.validation_cohorts import (
    EvolutionBaselineCohortRequestBuilder,
)
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBindingBuilder,
)
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlanner,
    EvolutionValidationProfileBinder,
)
from naumi_agent.orchestrator.context_assembly import (
    HARNESS_CONTEXT_MARKER,
    HarnessContextAssembler,
    is_harness_context_message,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.pursuit import PursuitRun, PursuitRunStatus


def test_background_context_reports_preparing_reservation() -> None:
    runner = type("Runner", (), {
        "list_tasks": lambda self: [BackgroundTask(
            id="bg_preparing",
            command="echo preparing",
            cwd="/tmp",
            status=BackgroundStatus.PREPARING,
            output_path="/tmp/bg-preparing.log",
        )]
    })()

    section = HarnessContextAssembler()._background_section(runner)

    assert "1 准备中" in section
    assert "bg_preparing [preparing]" in section


@pytest.fixture
async def engine(tmp_path) -> AgentEngine:
    config = AppConfig(
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "chroma"),
        ),
        workspace_root=str(tmp_path),
    )
    agent = AgentEngine(config)
    try:
        session = await agent.get_or_create_session()
        agent.task_store.set_session(session.id)
        yield agent
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_harness_context_snapshot_includes_live_state(engine: AgentEngine) -> None:
    await engine.task_store.create_task("整理 hooks 优化方案")
    engine.scheduler_runner.create(
        kind="once",
        expression="2999-01-01T00:00:00+00:00",
        prompt="复查长期任务",
    )
    now = time.time()
    engine.pursuit_store.save_run(PursuitRun(
        id="pursuit_ctx",
        goal="完成上下文快照",
        status=PursuitRunStatus.RUNNING,
        phase="assess",
        started_at=now,
        updated_at=now,
        criteria_total=2,
        criteria_verified=1,
    ))
    engine.goal_store.create("持续完善持久目标能力", session_id="session-context")

    await engine._inject_harness_context_snapshot()
    snapshot = engine._messages[-1]

    assert is_harness_context_message(snapshot)
    content = snapshot["content"]
    assert HARNESS_CONTEXT_MARKER in content
    assert "## Harness 状态快照" in content
    assert "### 工具池" in content
    assert "整理 hooks 优化方案" in content
    assert "复查长期任务" in content
    assert "pursuit_ctx" in content
    assert "完成上下文快照" in content
    assert "### 当前目标" in content
    assert "持续完善持久目标能力" in content
    assert "session-context" in content
    assert "预算：不限 · 已用 $0.0000" in content


def test_engine_composes_experiment_contract_and_worktree_lease_services(
    engine: AgentEngine,
) -> None:
    assert isinstance(
        engine.evolution_experiment_contract_issuer,
        EvolutionExperimentContractIssuer,
    )
    assert isinstance(
        engine.evolution_experiment_lease_store,
        EvolutionExperimentLeaseStore,
    )
    assert isinstance(
        engine.evolution_experiment_lease_manager,
        EvolutionExperimentLeaseManager,
    )
    assert isinstance(
        engine.evolution_experiment_source_snapshot_builder,
        EvolutionExperimentSourceSnapshotBuilder,
    )
    assert isinstance(engine.evolution_mutation_planner, EvolutionMutationPlanner)
    assert isinstance(engine.evolution_static_guard, EvolutionStaticGuard)
    assert isinstance(engine.evolution_patch_journal_store, EvolutionPatchJournalStore)
    assert isinstance(engine.evolution_patch_set_store, EvolutionPatchSetStore)
    assert isinstance(
        engine.evolution_mutation_receipt_store,
        EvolutionMutationReceiptStore,
    )
    assert isinstance(
        engine.evolution_mutation_generation_trace_store,
        EvolutionMutationGenerationTraceStore,
    )
    assert isinstance(
        engine.evolution_mutation_generation_service,
        EvolutionMutationGenerationService,
    )
    assert isinstance(
        engine.evolution_mutation_turn_runner,
        EvolutionMutationTurnRunner,
    )
    assert isinstance(
        engine.evolution_validation_planner,
        EvolutionValidationPlanner,
    )
    assert isinstance(
        engine.evolution_validation_profile_binder,
        EvolutionValidationProfileBinder,
    )
    assert isinstance(
        engine.evolution_adversarial_probe_contract_builder,
        EvolutionAdversarialProbeContractBuilder,
    )
    assert isinstance(
        engine.evolution_adversarial_batch_request_builder,
        EvolutionAdversarialBatchRequestBuilder,
    )
    assert isinstance(
        engine.evolution_baseline_cohort_request_builder,
        EvolutionBaselineCohortRequestBuilder,
    )
    assert isinstance(
        engine.evolution_metric_runner_binding_builder,
        EvolutionMetricRunnerBindingBuilder,
    )
    assert isinstance(
        engine.evolution_self_review_red_baseline_executor,
        EvolutionSelfReviewRedBaselineExecutor,
    )
    assert isinstance(
        engine.evolution_self_review_green_cohort_request_builder,
        EvolutionSelfReviewGreenCohortRequestBuilder,
    )
    assert isinstance(
        engine.evolution_self_review_green_cohort_executor,
        EvolutionSelfReviewGreenCohortExecutor,
    )
    assert isinstance(
        engine.evolution_self_review_comparison_executor,
        EvolutionSelfReviewComparisonExecutor,
    )
    assert isinstance(
        engine.evolution_failure_attribution_builder,
        EvolutionFailureAttributionBuilder,
    )
    assert isinstance(
        engine.evolution_failure_attribution_store,
        EvolutionFailureAttributionStore,
    )
    assert isinstance(
        engine.evolution_failure_attribution_executor,
        EvolutionFailureAttributionExecutor,
    )
    assert isinstance(
        engine.evolution_mutation_receipt_service,
        EvolutionMutationReceiptService,
    )
    assert isinstance(engine.evolution_patch_set_writer, EvolutionPatchSetWriter)
    assert isinstance(engine.evolution_patch_recovery, EvolutionPatchRecoveryCoordinator)
    assert isinstance(
        engine.evolution_patch_set_recovery,
        EvolutionPatchSetRecoveryCoordinator,
    )
    assert isinstance(engine.evolution_patch_writer, EvolutionPatchWriter)
    assert (
        engine.evolution_experiment_lease_manager._worktree_manager
        is engine.worktree_manager
    )


@pytest.mark.asyncio
async def test_harness_context_snapshot_reports_no_unfinished_goal(
    engine: AgentEngine,
) -> None:
    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    assert "### 当前目标" in content
    assert "当前没有未完成目标" in content


@pytest.mark.asyncio
async def test_engine_registers_goal_tools(engine: AgentEngine) -> None:
    assert {
        "goal_create",
        "goal_status",
        "goal_list",
        "goal_update",
        "goal_pursue",
    }.issubset(engine.tool_registry.names)


@pytest.mark.asyncio
async def test_harness_context_snapshot_replaces_previous_without_persisting(
    engine: AgentEngine,
) -> None:
    engine._messages = [
        {"role": "system", "content": "base"},
        {"role": "system", "content": f"{HARNESS_CONTEXT_MARKER}\nold"},
        {"role": "user", "content": "hello"},
    ]
    engine._full_history = list(engine._messages)

    await engine._inject_harness_context_snapshot()
    await engine._inject_harness_context_snapshot()

    active_snapshots = [
        item for item in engine._messages
        if is_harness_context_message(item)
    ]
    persisted_snapshots = [
        item for item in engine._full_history
        if is_harness_context_message(item)
    ]

    assert len(active_snapshots) == 1
    assert active_snapshots[0]["content"] != f"{HARNESS_CONTEXT_MARKER}\nold"
    assert len(persisted_snapshots) == 1
    assert persisted_snapshots[0]["content"] == f"{HARNESS_CONTEXT_MARKER}\nold"


@pytest.mark.asyncio
async def test_harness_context_snapshot_includes_trusted_local_time(
    engine: AgentEngine,
) -> None:
    fixed = datetime(
        2026, 7, 12, 3, 22, 36,
        tzinfo=timezone(timedelta(hours=8), name="Asia/Shanghai"),
    )
    engine._harness_context = HarnessContextAssembler(clock=lambda: fixed)

    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    assert "### 当前环境" in content
    assert "当前本地时间：2026-07-12T03:22:36+08:00" in content
    assert "时区：Asia/Shanghai (UTC+08:00)" in content
    assert "可直接回答，无需调用工具或公网 API" in content


@pytest.mark.asyncio
async def test_harness_context_clock_refreshes_each_snapshot(
    engine: AgentEngine,
) -> None:
    times = iter((
        datetime(2026, 7, 12, 3, 22, tzinfo=UTC),
        datetime(2026, 7, 12, 3, 23, tzinfo=UTC),
    ))
    engine._harness_context = HarnessContextAssembler(clock=lambda: next(times))

    await engine._inject_harness_context_snapshot()
    first = engine._messages[-1]["content"]
    await engine._inject_harness_context_snapshot()
    second = engine._messages[-1]["content"]

    assert "2026-07-12T03:22:00+00:00" in first
    assert "2026-07-12T03:23:00+00:00" in second
    assert "2026-07-12T03:22:00+00:00" not in second


@pytest.mark.asyncio
async def test_harness_context_normalizes_naive_clock_to_local_timezone(
    engine: AgentEngine,
) -> None:
    engine._harness_context = HarnessContextAssembler(
        clock=lambda: datetime(2026, 7, 12, 3, 22, 36),
    )

    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    time_line = next(
        line for line in content.splitlines()
        if line.startswith("- 当前本地时间：")
    )
    parsed = datetime.fromisoformat(time_line.split("：", 1)[1])

    assert parsed.utcoffset() is not None
    assert "- 时区：" in content
    assert "(UTC+" in content or "(UTC-" in content


@pytest.mark.asyncio
async def test_harness_context_default_clock_tracks_local_time(
    engine: AgentEngine,
) -> None:
    before = datetime.now().astimezone()

    await engine._inject_harness_context_snapshot()

    after = datetime.now().astimezone()
    content = engine._messages[-1]["content"]
    time_line = next(
        line for line in content.splitlines()
        if line.startswith("- 当前本地时间：")
    )
    parsed = datetime.fromisoformat(time_line.split("：", 1)[1])

    assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)
    assert parsed.utcoffset() == parsed.astimezone().utcoffset()
