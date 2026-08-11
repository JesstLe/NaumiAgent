from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.eligibility import (
    CandidateGovernanceContext,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.goal_need_opportunities import (
    EvolutionGoalNeedOpportunityError,
    EvolutionGoalNeedOpportunityService,
    adapt_goal_need_evidence,
    render_goal_need_opportunity,
)
from naumi_agent.evolution.proposal import generate_proposal_preview
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.evolution.validation_cohorts import BaselineCohortMetricCase
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerRegistry,
)
from naumi_agent.orchestrator.goal_store import GoalStatus, GoalStore
from naumi_agent.tools.evolution_review import EvolutionGoalNeedOpportunityTool


def _service(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    goals = GoalStore(tmp_path / "state" / "goals")
    candidates = EvolutionCandidateStore(tmp_path / "state" / "evolution.db")
    service = EvolutionGoalNeedOpportunityService(
        workspace_root=workspace,
        goal_store=goals,
        candidate_store=candidates,
    )
    return workspace, goals, candidates, service


@pytest.mark.asyncio
async def test_durable_goal_discovers_one_private_idempotent_need_candidate(
    tmp_path: Path,
) -> None:
    workspace, goals, candidates, service = _service(tmp_path)
    objective = "增加一个能够审计浏览器并发任务的能力，内部代号 SECRET-NEED-42"
    goal = goals.create(objective, session_id="session-private")

    results = await asyncio.gather(
        *(service.discover(goal_id=goal.id) for _ in range(8))
    )

    assert all(item == results[0] for item in results)
    result = results[0]
    assert result.goal_status == "active"
    assert result.candidate_revision == 1
    assert result.occurrence_count == 1
    stored = await candidates.get_candidate(workspace, result.candidate_id)
    assert stored is not None
    assert stored.draft.kind == "capability"
    assert stored.draft.finding_code == "user_explicit_need"
    assert stored.draft.source_kinds == ("goal_need",)
    assert stored.draft.expected_metrics[0].name == (
        "goal.user_explicit_need.completion"
    )
    assert stored.draft.expected_metrics[0].verifier == "goal_completion"
    assert await service.validate_candidate_sources(stored.draft)
    encoded = stored.draft.model_dump_json()
    assert objective not in encoded
    assert "SECRET-NEED-42" not in encoded
    assert "session-private" not in encoded
    assert str(workspace) not in encoded
    rendered = render_goal_need_opportunity(result)
    assert objective not in rendered
    assert "未保存 Goal objective" in rendered

    governance = CandidateGovernanceContext(
        allowed=True,
        reason="no_active_cooldown",
    )
    assessment = assess_candidate_eligibility(
        stored.draft,
        governance=governance,
    )
    assert assessment.review_ready
    assert not assessment.experiment_eligible
    preview = generate_proposal_preview(stored, governance=governance)
    assert preview is not None
    assert preview.proposal_kind == "tool"
    assert preview.validation_plan[0].verifier == "goal_completion"
    assert not preview.executable


@pytest.mark.asyncio
async def test_paused_and_blocked_goals_remain_needs_but_terminal_goals_revoke(
    tmp_path: Path,
) -> None:
    workspace, goals, candidates, service = _service(tmp_path)
    paused = goals.create("需要暂停后仍可审阅的能力")
    goals.update(paused.id, GoalStatus.PAUSED, note="等待输入")

    paused_result = await service.discover(goal_id=paused.id)
    paused_candidate = await candidates.get_candidate(
        workspace,
        paused_result.candidate_id,
    )
    assert paused_result.goal_status == "paused"
    assert paused_candidate is not None
    assert await service.validate_candidate_sources(paused_candidate.draft)

    goals.update(paused.id, GoalStatus.BLOCKED, note="外部依赖")
    blocked_retry = await service.discover(goal_id=paused.id)
    assert blocked_retry == paused_result.model_copy(update={"goal_status": "blocked"})
    assert await service.validate_candidate_sources(paused_candidate.draft)

    goals.update(paused.id, GoalStatus.CANCELLED, note="用户撤回")
    assert not await service.validate_candidate_sources(paused_candidate.draft)
    with pytest.raises(EvolutionGoalNeedOpportunityError) as withdrawn:
        await service.discover(goal_id=paused.id)
    assert withdrawn.value.code == "goal_need_withdrawn"

    completed = goals.create("需要完成后退出机会池的能力")
    completed_result = await service.discover(goal_id=completed.id)
    goals.update(completed.id, GoalStatus.COMPLETED, note="用户验收")
    completed_candidate = await candidates.get_candidate(
        workspace,
        completed_result.candidate_id,
    )
    assert completed_candidate is not None
    assert not await service.validate_candidate_sources(completed_candidate.draft)
    with pytest.raises(EvolutionGoalNeedOpportunityError) as satisfied:
        await service.discover(goal_id=completed.id)
    assert satisfied.value.code == "goal_need_satisfied"


@pytest.mark.asyncio
async def test_goal_identity_tamper_invalidates_existing_candidate(tmp_path: Path) -> None:
    workspace, goals, candidates, service = _service(tmp_path)
    goal = goals.create("需要可防篡改重验的能力")
    result = await service.discover(goal_id=goal.id)
    candidate = await candidates.get_candidate(workspace, result.candidate_id)
    assert candidate is not None

    with sqlite3.connect(goals.db_path) as db:
        db.execute(
            "UPDATE goals SET objective = ? WHERE id = ?",
            ("已被伪造的新目标", goal.id),
        )
        db.commit()

    assert not await service.validate_candidate_sources(candidate.draft)


@pytest.mark.asyncio
async def test_recreated_identical_goal_is_a_new_need_candidate(tmp_path: Path) -> None:
    _, goals, _, service = _service(tmp_path)
    first = goals.create("同一明确需求")
    first_result = await service.discover(goal_id=first.id)
    goals.update(first.id, GoalStatus.COMPLETED)

    second = goals.create("同一明确需求")
    second_result = await service.discover(goal_id=second.id)

    assert second.id != first.id
    assert second_result.candidate_id != first_result.candidate_id
    assert second_result.evidence_id != first_result.evidence_id


@pytest.mark.asyncio
async def test_invalid_or_missing_goal_fails_closed(tmp_path: Path) -> None:
    _, _, _, service = _service(tmp_path)

    with pytest.raises(EvolutionGoalNeedOpportunityError) as invalid:
        await service.discover(goal_id="goal_not-hex")
    assert invalid.value.code == "goal_need_id_invalid"

    with pytest.raises(EvolutionGoalNeedOpportunityError) as missing:
        await service.discover(goal_id="goal_0123456789ab")
    assert missing.value.code == "goal_need_source_unavailable"


def test_adapter_rejects_non_goal_and_terminal_goal(tmp_path: Path) -> None:
    _, goals, _, _ = _service(tmp_path)
    with pytest.raises(TypeError, match="durable Goal"):
        adapt_goal_need_evidence(object())  # type: ignore[arg-type]
    goal = goals.create("终态不能适配")
    terminal = goals.update(goal.id, GoalStatus.CANCELLED)
    with pytest.raises(ValueError, match="未终结"):
        adapt_goal_need_evidence(terminal)


def test_goal_completion_verifier_is_explicitly_blocked_without_runner() -> None:
    resolution = EvolutionMetricRunnerRegistry().resolve(
        BaselineCohortMetricCase(
            order=1,
            metric_name="goal.user_explicit_need.completion",
            direction="increase",
            target=1,
            verifier="goal_completion",
            procedure_sha256="a" * 64,
        ),
        validation_paths=("src/naumi_agent/example.py",),
    )

    assert resolution.status == "blocked"
    assert resolution.fixture_kind == "goal_status_authority"
    assert resolution.blocking_code == "goal_completion_runner_unavailable"
    assert resolution.runner_version is None


@pytest.mark.asyncio
async def test_agent_tool_and_slash_share_goal_need_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, goals, _, service = _service(tmp_path)
    goal = goals.create("需要 Tool 与 Slash 共用服务")
    tool = EvolutionGoalNeedOpportunityTool(SimpleNamespace(
        evolution_goal_need_opportunity_service=service,
    ))

    rendered = await tool.execute(goal.id)

    assert tool.parameters_schema["properties"]["goal_id"]["pattern"] == (
        "^goal_[0-9a-f]{12}$"
    )
    assert "Goal 明确需求已进入机会发现" in rendered

    from naumi_agent import main

    calls: list[dict[str, object]] = []

    async def fake_run_tool(engine, **kwargs):
        calls.append({"engine": engine, **kwargs})

    monkeypatch.setattr(main, "_run_tool_slash_command", fake_run_tool)
    engine = SimpleNamespace()
    await main._run_evolution_review(engine, f"discover-goal {goal.id}")

    assert calls[0]["engine"] is engine
    assert calls[0]["tool_name"] == "evolution_discover_goal_need_opportunity"
    assert calls[0]["parse_args"]("") == {"goal_id": goal.id}
