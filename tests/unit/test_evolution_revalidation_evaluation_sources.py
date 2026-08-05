from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.revalidation_evaluation_plans import (
    EvolutionRevalidationEvaluationPlanService,
    EvolutionRevalidationEvaluationPlanStore,
)
from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceError,
    EvolutionRevalidationEvaluationSourceService,
    EvolutionRevalidationEvaluationSourceStore,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionRevalidationValidationStore,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckStatus
from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionRiskLevel
from naumi_agent.tools.evolution_review import (
    EvolutionRevalidationEvaluationSourceTool,
)
from tests.unit.test_evolution_revalidation_outcomes import _scenario
from tests.unit.test_evolution_revalidation_validations import _StaticStore


async def _source_scenario(tmp_path: Path):
    (
        package,
        request_view,
        validation_receipt,
        harness,
        validation_service,
        outcome_store,
        outcome_service,
    ) = await _scenario(tmp_path, status=HarnessSandboxCheckStatus.PASSED)
    outcome = await outcome_service.issue(
        workspace_root=tmp_path,
        request_id=request_view.request.request_id,
    )
    db_path = tmp_path / ".naumi" / "state.db"
    plan_service = EvolutionRevalidationEvaluationPlanService(
        outcome_service=outcome_service,
        outcome_store=outcome_store,
        validation_store=EvolutionRevalidationValidationStore(db_path),
        package_input_store=_StaticStore(
            type("PackageView", (), {"package_input": package})()
        ),
        store=EvolutionRevalidationEvaluationPlanStore(db_path),
        clock=lambda: datetime.fromisoformat(validation_receipt.created_at),
    )
    plan = await plan_service.issue(
        workspace_root=tmp_path,
        outcome_id=outcome.outcome.outcome_id,
    )
    store = EvolutionRevalidationEvaluationSourceStore(
        db_path,
        storage_dir=tmp_path / ".naumi" / "evolution" / "evaluation-sources",
    )
    service = EvolutionRevalidationEvaluationSourceService(
        plan_service=plan_service,
        outcome_store=outcome_store,
        source_provider=validation_service,
        store=store,
        clock=lambda: datetime.fromisoformat(validation_receipt.created_at),
    )
    return package, harness, plan, store, service


@pytest.mark.asyncio
async def test_capture_persists_exact_revalidation_overlay_as_immutable_blobs(
    tmp_path: Path,
) -> None:
    package, harness, plan, store, service = await _source_scenario(tmp_path)
    original = {
        item.path: (tmp_path / item.path).read_bytes() for item in package.patch.files
    }

    snapshot = await service.capture(
        workspace_root=tmp_path,
        plan_id=plan.plan.plan_id,
    )
    overlays = await store.load_overlays(snapshot.snapshot_id)

    assert snapshot.plan_sha256 == plan.plan.plan_sha256
    assert snapshot.overlay_source_sha256 == plan.plan.overlay_source_sha256
    assert snapshot.immutable_source_complete
    assert not snapshot.evaluation_execution_started
    assert not snapshot.promotion_authority
    assert harness.calls == 1
    assert tuple(item.path for item in overlays) == tuple(
        sorted(item.path for item in package.patch.files)
    )
    for overlay in overlays:
        assert overlay.content != original[overlay.path]
        assert (tmp_path / overlay.path).read_bytes() == original[overlay.path]


@pytest.mark.asyncio
async def test_snapshot_remains_loadable_after_candidate_worktree_is_removed(
    tmp_path: Path,
) -> None:
    _package, _harness, plan, store, service = await _source_scenario(tmp_path)
    snapshot = await service.capture(
        workspace_root=tmp_path,
        plan_id=plan.plan.plan_id,
    )
    shutil.rmtree(tmp_path / ".naumi" / "worktrees")

    repeated = await service.capture(
        workspace_root=tmp_path,
        plan_id=plan.plan.plan_id,
    )
    overlays = await store.load_overlays(snapshot.snapshot_id)

    assert repeated == snapshot
    assert overlays


@pytest.mark.asyncio
async def test_corrupt_or_symlinked_blob_is_rejected(tmp_path: Path) -> None:
    _package, _harness, plan, store, service = await _source_scenario(tmp_path)
    snapshot = await service.capture(
        workspace_root=tmp_path,
        plan_id=plan.plan.plan_id,
    )
    blob_path = (
        tmp_path
        / ".naumi"
        / "evolution"
        / "evaluation-sources"
        / snapshot.blobs[0].storage_key
    )
    blob_path.chmod(0o600)
    blob_path.write_bytes(b"tampered")

    with pytest.raises(EvolutionRevalidationEvaluationSourceError) as corrupt:
        await store.load_overlays(snapshot.snapshot_id)
    assert corrupt.value.code == "revalidation_evaluation_source_blob_corrupt"

    blob_path.unlink()
    blob_path.symlink_to(tmp_path / "README.md")
    with pytest.raises(EvolutionRevalidationEvaluationSourceError) as unsafe:
        await store.load_overlays(snapshot.snapshot_id)
    assert unsafe.value.code == "revalidation_evaluation_source_blob_unreadable"


@pytest.mark.asyncio
async def test_stale_plan_cannot_capture_first_source_snapshot(tmp_path: Path) -> None:
    _package, harness, plan, _store, service = await _source_scenario(tmp_path)
    harness.check = harness.check.model_copy(update={"timeout_seconds": 31})

    with pytest.raises(EvolutionRevalidationEvaluationSourceError) as exc:
        await service.capture(
            workspace_root=tmp_path,
            plan_id=plan.plan.plan_id,
        )

    assert exc.value.code == "revalidation_evaluation_source_plan_stale"


@pytest.mark.asyncio
async def test_evaluation_source_has_agent_and_slash_entrypoints(tmp_path: Path) -> None:
    _package, _harness, plan, _store, service = await _source_scenario(tmp_path)
    engine = type(
        "Engine",
        (),
        {
            "workspace_root": tmp_path,
            "evolution_revalidation_evaluation_source_service": service,
        },
    )()

    tool_output = await EvolutionRevalidationEvaluationSourceTool(engine).execute(
        plan.plan.plan_id
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution revalidation-evaluation-source {plan.plan.plan_id}",
    )

    assert "Content addressed / complete：`true` / `true`" in tool_output
    assert "Content addressed / complete" in slash_output
    rule = TOOL_PERMISSIONS["evolution_revalidation_evaluation_source"]
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert not rule.requires_confirmation
