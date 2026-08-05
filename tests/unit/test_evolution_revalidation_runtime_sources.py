from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceStore,
)
from naumi_agent.evolution.revalidation_runtime_sources import (
    EvolutionRevalidationRuntimeSourceError,
    EvolutionRevalidationRuntimeSourceService,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanBuilder,
    EvolutionRevalidationValidationPlanView,
)
from tests.unit.test_evolution_revalidation_validation_plans import (
    _builder_scenario,
)


async def _runtime_scenario(tmp_path: Path, **builder_options):
    package, fresh, source, contract, harness, target_files, now = (
        await _builder_scenario(tmp_path, **builder_options)
    )
    plan = EvolutionRevalidationValidationPlanBuilder().build(
        workspace_root=tmp_path,
        fresh_plan=fresh,
        source=source,
        package=package,
        contract=contract,
        suite_id="core_revalidation",
        requested_samples=7,
        harness_plan=harness,
        target_files=target_files,
        now=now,
    )
    db_path = tmp_path / ".naumi" / "state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_evaluation_sources SET "
            "snapshot_id = ?, snapshot_sha256 = ?, snapshot_json = ?",
            (source.snapshot_id, source.snapshot_sha256, source.model_dump_json()),
        )
        await db.commit()
    source_store = EvolutionRevalidationEvaluationSourceStore(
        db_path,
        storage_dir=tmp_path / ".naumi" / "evolution" / "evaluation-sources",
    )
    state = SimpleNamespace(current=True)

    class _PlanService:
        async def inspect(self, **_kwargs):
            return EvolutionRevalidationValidationPlanView(
                plan=plan,
                source_current=state.current,
                current_status="ready" if state.current else "stale",
                execution_eligible=state.current,
            )

    service = EvolutionRevalidationRuntimeSourceService(
        validation_plan_service=_PlanService(),
        source_store=source_store,
    )
    return plan, source, source_store, state, service


@pytest.mark.asyncio
async def test_materialize_builds_same_target_red_and_immutable_green(
    tmp_path: Path,
) -> None:
    plan, source, _store, _state, service = await _runtime_scenario(tmp_path)

    pair = await service.materialize(
        workspace_root=tmp_path,
        validation_plan_id=plan.validation_plan_id,
    )

    assert pair.red.revision == pair.green.revision == plan.red_revision
    assert pair.red.revision_tree_sha256 == pair.green.revision_tree_sha256
    assert not pair.red.overlays
    assert pair.red.overlay_source_sha256 is None
    assert pair.green.overlays == pair.overlays
    assert pair.green.overlay_source_sha256 == source.overlay_source_sha256
    assert pair.green_identity.dirty
    assert not pair.red_identity.dirty
    assert pair.green_materialized_tree_sha256 != plan.green_tree_sha256
    assert await pair.red.source_is_current()
    assert await pair.green.source_is_current()


@pytest.mark.asyncio
async def test_runtime_pair_fails_closed_after_plan_or_blob_drift(tmp_path: Path) -> None:
    plan, source, _store, state, service = await _runtime_scenario(tmp_path)
    pair = await service.materialize(
        workspace_root=tmp_path,
        validation_plan_id=plan.validation_plan_id,
    )
    state.current = False

    assert not await pair.red.source_is_current()
    with pytest.raises(EvolutionRevalidationRuntimeSourceError) as stale:
        await service.materialize(
            workspace_root=tmp_path,
            validation_plan_id=plan.validation_plan_id,
        )
    assert stale.value.code == "revalidation_runtime_validation_plan_stale"

    state.current = True
    blob = (
        tmp_path
        / ".naumi"
        / "evolution"
        / "evaluation-sources"
        / source.blobs[0].storage_key
    )
    blob.chmod(0o600)
    blob.write_bytes(b"tampered")
    assert not await pair.green.source_is_current()
