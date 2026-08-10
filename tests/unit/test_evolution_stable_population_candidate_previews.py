from __future__ import annotations

import asyncio
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
    EvolutionRevalidationStableStageCompletionView,
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
from naumi_agent.evolution.stable_read_graph import (
    EvolutionLazyStableReadGraphInspector,
    EvolutionStableReadGraphInspector,
    build_evolution_stable_read_graph_inspector,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.release.population_registry import ReleasePopulationSnapshotStore
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
from tests.unit.test_release_population_registry import (
    T0,
    _credentials,
    _policy,
    _signer,
)


def _passing_receipt(
    tmp_path: Path,
    index: int,
    *,
    member: int | None = None,
    member_id: str | None = None,
    snapshot_id: str = "relpopsnapshot_" + "3" * 24,
    snapshot_sha256: str = "3" * 64,
    snapshot_sequence: int = 7,
    population_denominator: int = 2,
):
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
            "population_snapshot_id": snapshot_id,
            "population_snapshot_sha256": snapshot_sha256,
            "population_snapshot_sequence": snapshot_sequence,
            "population_denominator": population_denominator,
            "installation_member_id": member_id or f"relpopmember_{member_hex}",
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


def _current_view(receipt) -> EvolutionRevalidationStableStageCompletionView:
    return EvolutionRevalidationStableStageCompletionView(
        receipt=receipt,
        evidence_source_current=True,
        latest_assessment=True,
        plan_source_current=True,
        baseline_source_current=True,
        liveness_source_current=True,
        outcome_set_current=True,
        invalidation_reasons=(),
        stable_stage_completion_authority=True,
        pause_input_authority=False,
        rollback_input_authority=False,
    )


class _StageCompletionInspector:
    def __init__(self, views) -> None:
        self.views = {item.receipt.evidence_id: item for item in views}
        self.calls: list[tuple[str, str]] = []

    async def inspect(self, *, evidence_id: str, subject_id: str):
        self.calls.append((evidence_id, subject_id))
        return self.views[evidence_id]


@pytest.mark.asyncio
async def test_dynamic_inspection_is_bounded_and_rejects_identity_mismatch(
    tmp_path: Path,
) -> None:
    receipt = _passing_receipt(tmp_path, 1, population_denominator=1)
    view = _current_view(receipt)

    class _BoundedInspector:
        active = 0
        maximum = 0
        calls = 0

        async def inspect(self, *, evidence_id: str, subject_id: str):
            assert evidence_id == receipt.evidence_id
            assert subject_id == receipt.subject_id
            self.calls += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return view

    bounded = _BoundedInspector()
    service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=tmp_path / "bounded.db",
        stage_completion_inspector=bounded,
    )
    results = await service._inspect_stage_completions((receipt,) * 17)
    assert bounded.calls == 17
    assert bounded.maximum == 16
    assert results[receipt.evidence_id] == (True, True, ())

    other = _passing_receipt(tmp_path, 2, population_denominator=1)
    mismatch = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=tmp_path / "mismatch.db",
        stage_completion_inspector=_StageCompletionInspector((_current_view(other),)),
    )
    mismatch.stage_completion_inspector.views[receipt.evidence_id] = _current_view(other)
    mismatch_results = await mismatch._inspect_stage_completions((receipt,))
    assert mismatch_results[receipt.evidence_id] == (
        False,
        False,
        ("dynamic_inspection_identity_mismatch",),
    )

    class _FailingInspector:
        async def inspect(self, *, evidence_id: str, subject_id: str):
            raise RuntimeError(f"private path for {evidence_id} and {subject_id}")

    failing = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=tmp_path / "failing.db",
        stage_completion_inspector=_FailingInspector(),
    )
    failed_results = await failing._inspect_stage_completions((receipt,))
    assert failed_results[receipt.evidence_id] == (
        False,
        False,
        ("dynamic_inspection_failed",),
    )


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
    assert not preview.dynamic_inspector_configured
    assert preview.dynamically_revalidated_members == 0
    assert preview.dynamic_authoritative_members == 0
    assert preview.dynamic_non_authoritative_members == 0
    assert not preview.dynamic_revalidation_authority
    assert preview.items[0].dynamic_invalidation_reasons == (
        "dynamic_inspector_not_configured",
    )
    assert not preview.population_snapshot_authority
    assert "population_store_not_configured" in (
        preview.population_invalidation_reasons
    )
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
async def test_candidate_preview_reconciles_current_signed_population(
    tmp_path: Path,
) -> None:
    signer = _signer()
    credentials = _credentials(signer, count=2)
    snapshot = signer.issue_snapshot(
        channel="stable",
        credentials=credentials,
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    current_policy = [_policy(signer)]
    population_store = ReleasePopulationSnapshotStore(
        tmp_path / "release-population.db",
        trust_policy_provider=lambda: current_policy[0],
        clock=lambda: T0 + timedelta(seconds=2),
    )
    await population_store.record(snapshot)
    empty_candidate = await EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=tmp_path / "empty-evolution.db",
        population_store=population_store,
        clock=lambda: T0 + timedelta(seconds=3),
    ).preview(snapshot_id=snapshot.snapshot_id)
    assert empty_candidate.status is EvolutionStablePopulationCandidateStatus.EMPTY
    assert empty_candidate.population_denominator == 2
    assert empty_candidate.missing_members == 2
    assert empty_candidate.population_snapshot_authority
    assert not empty_candidate.candidate_complete

    db_path = tmp_path / "evolution.db"
    receipts = tuple(
        _passing_receipt(
            tmp_path,
            index,
            member_id=credential.payload.member_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.snapshot_sha256,
            snapshot_sequence=snapshot.payload.sequence,
            population_denominator=snapshot.payload.population_denominator,
        )
        for index, credential in enumerate(credentials, start=1)
    )
    await _record(db_path, *receipts)
    inspector = _StageCompletionInspector(tuple(_current_view(item) for item in receipts))
    service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=db_path,
        population_store=population_store,
        stage_completion_inspector=inspector,
        clock=lambda: T0 + timedelta(seconds=3),
    )
    evolution_before = db_path.read_bytes()
    population_before = population_store.db_path.read_bytes()
    current = await service.preview(snapshot_id=snapshot.snapshot_id)
    assert db_path.read_bytes() == evolution_before
    assert population_store.db_path.read_bytes() == population_before
    assert current.candidate_complete
    assert current.population_source_configured
    assert current.population_source_current
    assert current.population_latest_for_channel
    assert current.population_trust_current
    assert not current.population_not_yet_valid
    assert current.population_membership_consistent
    assert current.population_snapshot_authority
    assert current.population_invalidation_reasons == ()
    assert current.dynamic_inspector_configured
    assert current.dynamically_revalidated_members == 2
    assert current.dynamic_authoritative_members == 2
    assert current.dynamic_non_authoritative_members == 0
    assert current.dynamic_revalidation_authority
    assert {item[0] for item in inspector.calls} == {
        receipt.evidence_id for receipt in receipts
    }
    assert not current.stable_rollout_authority
    assert not current.promotion_authority

    next_snapshot = signer.issue_snapshot(
        channel="stable",
        credentials=credentials,
        previous=snapshot,
        generated_at=(T0 + timedelta(days=1)).isoformat(),
        valid_from=(T0 + timedelta(days=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=8)).isoformat(),
    )
    await population_store.record(next_snapshot)
    stale = await service.preview(snapshot_id=snapshot.snapshot_id)
    assert not stale.population_latest_for_channel
    assert not stale.population_snapshot_authority
    assert "newer_snapshot_exists" in stale.population_invalidation_reasons

    current_policy[0] = _policy(signer, state="revoked")
    revoked = await service.preview(snapshot_id=snapshot.snapshot_id)
    assert not revoked.population_trust_current
    assert not revoked.population_snapshot_authority
    assert "registry_trust_changed" in revoked.population_invalidation_reasons

    mismatched_db = tmp_path / "mismatched-evolution.db"
    nonmember = _passing_receipt(
        tmp_path,
        3,
        member_id="relpopmember_" + "f" * 24,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        snapshot_sequence=snapshot.payload.sequence,
        population_denominator=snapshot.payload.population_denominator,
    )
    await _record(mismatched_db, receipts[0], nonmember)
    current_policy[0] = _policy(signer)
    mismatched = await EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=mismatched_db,
        population_store=population_store,
        clock=lambda: T0 + timedelta(seconds=3),
    ).preview(snapshot_id=snapshot.snapshot_id)
    assert not mismatched.population_membership_consistent
    assert not mismatched.population_snapshot_authority
    assert "population_lineage_or_membership_mismatch" in (
        mismatched.population_invalidation_reasons
    )


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
    stable_intent_service = (
        completion_service.window_service.exposure_service.deployment_service.intent_service
    )
    percentage_advance_service = stable_intent_service.store.stage_advance_service
    percentage_intent_service = (
        percentage_advance_service.completion_service.window_service.exposure_service
        .deployment_service.intent_service
    )
    opt_in_advance_service = percentage_intent_service.assignment_service.advance_service
    opt_in_runtime_health_service = (
        opt_in_advance_service.completion_service.window_service.runtime_health_service
    )
    archive_service = stable_intent_service.store.archive_service
    read_graph = build_evolution_stable_read_graph_inspector(
        workspace_root=tmp_path,
        evolution_db_path=completion_store.db_path,
        release_staging_root=archive_service.staging_root,
        harness_store=completion_service.window_service.harness_store,
        chat_run_store=completion_service.outcome_service.chat_run_store,
        opt_in_runtime_health_service=opt_in_runtime_health_service,
        plan_service=completion_service.plan_service,
        baseline_service=completion_service.baseline_service,
        control_store=percentage_advance_service.control_store,
        population_store=stable_intent_service.store.population_store,
        artifact_fetch_service=archive_service.fetch_service,
        release_slot_store=archive_service.slot_store,
    )
    assert isinstance(read_graph, EvolutionStableReadGraphInspector)
    assert not hasattr(read_graph, "assess")
    factory_calls = 0

    def read_graph_factory():
        nonlocal factory_calls
        factory_calls += 1
        return read_graph

    lazy_read_graph = EvolutionLazyStableReadGraphInspector(read_graph_factory)
    assert not lazy_read_graph.initialized
    before = completion_store.db_path.read_bytes()
    service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=completion_store.db_path,
        stage_completion_inspector=lazy_read_graph,
        clock=lambda: data["runtime_now"][0] + timedelta(seconds=1),
    )
    preview = await service.preview(
        snapshot_id=completion.receipt.population_snapshot_id,
    )
    assert completion_store.db_path.read_bytes() == before
    assert lazy_read_graph.initialized
    assert factory_calls == 1
    assert preview.status is EvolutionStablePopulationCandidateStatus.PARTIAL
    assert preview.observed_members == 1
    assert preview.insufficient_members == 1
    assert preview.items[0].evidence_id == completion.receipt.evidence_id
    assert preview.dynamic_inspector_configured
    assert preview.dynamically_revalidated_members == 1
    assert preview.dynamic_authoritative_members == 0
    assert preview.dynamic_non_authoritative_members == 1
    assert preview.items[0].dynamically_revalidated
    assert not preview.items[0].dynamic_stage_completion_authority
    assert not preview.dynamic_revalidation_authority
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
async def test_engine_composes_candidate_preview_service_and_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_trust_read(_path):
        raise AssertionError("空候选启动不应读取 Population trust artifact")

    monkeypatch.setattr(
        "naumi_agent.orchestrator.engine.load_release_population_trust_policy",
        unexpected_trust_read,
    )
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
        assert service.population_store is (
            engine.evolution_release_population_snapshot_store
        )
        assert service.stage_completion_inspector is (
            engine.evolution_stable_stage_completion_inspector
        )
        assert service.stage_completion_inspector is not None
        assert isinstance(
            service.stage_completion_inspector,
            EvolutionLazyStableReadGraphInspector,
        )
        assert not hasattr(service.stage_completion_inspector, "assess")
        assert not service.stage_completion_inspector.initialized
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
        assert "端口 `configured`" in result.content
        assert "Promotion authority：`false`" in result.content
        assert not service.stage_completion_inspector.initialized
    finally:
        await engine.shutdown()
