from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutCostSource,
)
from naumi_agent.evolution.revalidation_stable_stage_completions import (
    _build_receipt,
)
from naumi_agent.evolution.revalidation_stage_completion_metrics import (
    calculate_stage_completion_metrics,
)
from naumi_agent.evolution.stable_population_candidate_previews import (
    EvolutionStablePopulationCandidatePreview,
    EvolutionStablePopulationCandidatePreviewError,
    EvolutionStablePopulationCandidatePreviewService,
    EvolutionStablePopulationCandidateStatus,
    render_stable_population_candidate_preview,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePopulationCandidatePreviewTool,
)
from tests.unit.test_evolution_revalidation_stable_execution_outcome_ledger import (
    _sources,
    _terminal_run,
)
from tests.unit.test_evolution_revalidation_stable_stage_completions import (
    _services,
)


def _passing_receipt(tmp_path: Path, index: int, *, member: int | None = None):
    member_index = index if member is None else member
    durations = (1_000,) * 10
    costs = (1_000,) * 10
    metrics = calculate_stage_completion_metrics(
        durations_micros=durations,
        reported_cost_microusd=costs,
        successful_runs=10,
        minimum_completed_runs=10,
        max_error_rate_basis_points=100,
        max_completion_rate_drop_basis_points=100,
        max_p95_latency_regression_basis_points=100,
        max_cost_regression_basis_points=100,
        baseline_completion_rate_basis_points=10_000,
        baseline_p95_duration_micros=1_000,
        baseline_mean_cost_microusd=1_000,
        baseline_cost_source=EvolutionRevalidationRolloutCostSource.LIVE_EVIDENCE,
    )
    hex_id = f"{index:024x}"
    member_hex = f"{member_index:024x}"
    outcome_ids = tuple(f"stable-outcome-{index}-{item}" for item in range(10))
    return _build_receipt(
        {
            "workspace_root": str(tmp_path.resolve()),
            "intent_id": f"evrestableintent_{hex_id}",
            "subject_id": f"stable-member-{index}",
            "plan_id": "evrerolloutplan_" + "1" * 24,
            "plan_sha256": "1" * 64,
            "baseline_id": "evrerolloutbaseline_" + "2" * 24,
            "baseline_sha256": "2" * 64,
            "liveness_window_id": f"evrestablewindow_{hex_id}",
            "liveness_window_sha256": f"{index + 3:x}" * 64,
            "binding_id": f"hrreleasebinding_{hex_id}",
            "binding_sha256": f"{index + 5:x}" * 64,
            "candidate_version": "1.2.3",
            "candidate_target": "darwin-arm64",
            "population_snapshot_id": "relpopsnapshot_" + "3" * 24,
            "population_snapshot_sha256": "3" * 64,
            "population_snapshot_sequence": 7,
            "population_denominator": 2,
            "installation_member_id": f"relpopmember_{member_hex}",
            "exposure_percent": 100,
            "outcome_ids": outcome_ids,
            "outcome_sha256": tuple(f"{item + index + 1:x}"[-1] * 64 for item in range(10)),
            "run_ids": tuple(f"run-{index}-{item}" for item in range(10)),
            "authoritative_outcome_ids": outcome_ids,
            "successful_outcome_ids": outcome_ids,
            "duration_micros": durations,
            "reported_cost_microusd": costs,
            "metrics": metrics,
        },
        datetime(2026, 8, 11, 8, index, tzinfo=UTC).isoformat(),
    )


async def _record(db_path: Path, *receipts) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS "
            "evolution_revalidation_stable_stage_completions ("
            "evidence_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL, "
            "intent_id TEXT NOT NULL, status TEXT NOT NULL, "
            "source_set_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL, "
            "assessed_at TEXT NOT NULL)"
        )
        for receipt in receipts:
            await db.execute(
                "INSERT INTO evolution_revalidation_stable_stage_completions "
                "(evidence_id, evidence_sha256, intent_id, status, source_set_sha256, "
                "evidence_json, assessed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.evidence_id,
                    receipt.evidence_sha256,
                    receipt.intent_id,
                    receipt.metrics.status.value,
                    receipt.source_set_sha256,
                    receipt.model_dump_json(),
                    receipt.assessed_at,
                ),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_candidate_preview_groups_members_without_granting_authority(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "evolution.db"
    first = _passing_receipt(tmp_path, 1)
    second = _passing_receipt(tmp_path, 2)
    await _record(db_path, first, second)
    service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=db_path,
        clock=lambda: datetime(2026, 8, 11, 9, tzinfo=UTC),
    )

    before = db_path.read_bytes()
    preview = await service.preview(limit=1)
    assert db_path.read_bytes() == before
    assert preview.status is EvolutionStablePopulationCandidateStatus.CANDIDATE_COMPLETE
    assert preview.candidate_complete
    assert preview.population_denominator == 2
    assert preview.observed_members == preview.passing_members == 2
    assert preview.hidden_items == 1
    assert not preview.dynamic_revalidation_authority
    assert not preview.stable_rollout_authority
    assert not preview.promotion_authority
    assert EvolutionStablePopulationCandidatePreview.model_validate_json(
        preview.model_dump_json()
    ) == preview
    rendered = render_stable_population_candidate_preview(preview)
    assert "候选已覆盖" in rendered
    assert "Promotion authority：`false`" in rendered

    tool = EvolutionStablePopulationCandidatePreviewTool(
        SimpleNamespace(evolution_stable_population_candidate_preview_service=service)
    )
    assert tool.metadata.read_only and tool.metadata.concurrency_safe
    assert not tool.metadata.requires_confirmation
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        def __init__(self) -> None:
            self.tool_registry = registry
            self.calls: list[tuple[ToolCall, str | None]] = []

        async def execute_tool(
            self,
            call: ToolCall,
            *,
            agent_name: str | None = None,
        ) -> ToolResult:
            self.calls.append((call, agent_name))
            registered = self.tool_registry.get(call.name)
            assert registered is not None
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    slash_engine = _SlashEngine()
    slash = await execute_slash_command(
        slash_engine,
        f"/evolution stable-population-preview {preview.population_snapshot_id} 1",
    )
    assert "候选已覆盖" in slash
    assert len(slash_engine.calls) == 1
    assert slash_engine.calls[0][0].name == (
        "evolution_stable_population_candidate_preview"
    )
    assert slash_engine.calls[0][1] == "cli"

    duplicate = _passing_receipt(tmp_path, 3, member=1)
    await _record(db_path, duplicate)
    conflicted = await service.preview(snapshot_id=preview.population_snapshot_id)
    assert conflicted.status is EvolutionStablePopulationCandidateStatus.CONFLICTED
    assert conflicted.conflicting_members == 1
    assert "duplicate_member_intents" in conflicted.integrity_conflicts
    assert not conflicted.candidate_complete

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_stable_stage_completions "
            "SET evidence_json = replace(evidence_json, ?, ?) WHERE evidence_id = ?",
            (duplicate.evidence_sha256, "0" * 64, duplicate.evidence_id),
        )
        await db.commit()
    with pytest.raises(EvolutionStablePopulationCandidatePreviewError) as corrupt:
        await service.preview(snapshot_id=preview.population_snapshot_id)
    assert corrupt.value.code == "stable_population_candidate_receipt_invalid"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_candidate_preview_reads_real_stable_completion_without_mutating_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, lifecycle, exposure, chat_store, _outcome_store, outcome_service = await _sources(
        tmp_path, monkeypatch
    )
    intent_id = data["intent_id"]
    subject_id = exposure.binding.subject_id
    monkeypatch.setattr(
        "naumi_agent.runs.store._now_iso",
        lambda: (data["runtime_now"][0] - timedelta(seconds=1)).isoformat(),
    )
    plan_service, completion_store, completion_service = _services(
        tmp_path,
        outcome_service,
        lambda: data["runtime_now"][0],
    )
    plan = (
        await plan_service.inspect(
            plan_id=exposure.deployment.preparation.intent.plan.plan_id
        )
    ).plan
    for index in range(plan.stages[3].minimum_completed_runs):
        run = await _terminal_run(
            store=chat_store,
            binding=exposure.binding,
            outcome="completed",
            index=index,
        )
        await outcome_service.record(
            intent_id=intent_id,
            subject_id=subject_id,
            session_id=run.session_id,
            run_id=run.id,
        )
    completion = await completion_service.assess(
        intent_id=intent_id,
        subject_id=subject_id,
    )
    assert completion.receipt.metrics.status.value == "insufficient"
    before = completion_store.db_path.read_bytes()
    service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=completion_store.db_path,
        clock=lambda: data["runtime_now"][0] + timedelta(seconds=1),
    )
    preview = await service.preview(
        snapshot_id=completion.receipt.population_snapshot_id,
    )
    assert completion_store.db_path.read_bytes() == before
    assert preview.status is EvolutionStablePopulationCandidateStatus.PARTIAL
    assert preview.observed_members == 1
    assert preview.insufficient_members == 1
    assert preview.items[0].evidence_id == completion.receipt.evidence_id
    assert not preview.candidate_complete
    assert await lifecycle.close()


@pytest.mark.asyncio
async def test_candidate_preview_empty_and_input_bounds(tmp_path: Path) -> None:
    service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=tmp_path / "missing.db",
    )
    empty = await service.preview()
    assert empty.status is EvolutionStablePopulationCandidateStatus.EMPTY
    assert empty.items == ()
    with pytest.raises(ValueError, match="snapshot_id"):
        await service.preview(snapshot_id="' OR 1=1 --")
    with pytest.raises(ValueError, match="limit"):
        await service.preview(limit=0)


@pytest.mark.asyncio
async def test_engine_composes_candidate_preview_service_and_tool(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "sessions.db"
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(session_db_path=str(db_path)),
        )
    )
    try:
        service = engine.evolution_stable_population_candidate_preview_service
        assert service.workspace_root == tmp_path.resolve()
        assert service.db_path == db_path.resolve()
        assert "evolution_stable_population_candidate_preview" in (
            engine.tool_registry.names
        )
        result = await engine.execute_tool(
            ToolCall(
                id="stable-population-preview-engine",
                name="evolution_stable_population_candidate_preview",
                arguments={"limit": 1},
            ),
            agent_name="engine-integration-test",
        )
        assert result.status == "success"
        assert "状态：**暂无候选**" in result.content
        assert "Promotion authority：`false`" in result.content
    finally:
        await engine.shutdown()
