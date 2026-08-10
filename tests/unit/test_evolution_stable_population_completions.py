from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.revalidation_stable_stage_completions import (
    EvolutionRevalidationStableStageCompletionView,
)
from naumi_agent.evolution.stable_population_candidate_previews import (
    EvolutionStablePopulationCandidatePreviewService,
)
from naumi_agent.evolution.stable_population_completions import (
    EvolutionStablePopulationCompletionError,
    EvolutionStablePopulationCompletionReceipt,
    EvolutionStablePopulationCompletionService,
    EvolutionStablePopulationCompletionStore,
    render_stable_population_completion,
)
from naumi_agent.evolution.stable_population_completions import (
    _build_receipt as _build_completion_receipt,
)
from naumi_agent.release.population_registry import ReleasePopulationSnapshotStore
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStablePopulationCompletionTool
from tests.unit.test_evolution_stable_population_candidate_previews import (
    _current_view,
    _passing_receipt,
    _record,
    _StageCompletionInspector,
)
from tests.unit.test_release_population_registry import (
    T0,
    _credentials,
    _policy,
    _signer,
)


async def _authority_services(tmp_path: Path):
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
    policy = [_policy(signer)]
    population_store = ReleasePopulationSnapshotStore(
        tmp_path / "release-population.db",
        trust_policy_provider=lambda: policy[0],
        clock=lambda: T0 + timedelta(seconds=2),
    )
    await population_store.record(snapshot)
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
    db_path = tmp_path / "evolution.db"
    await _record(db_path, *receipts)
    inspector = _StageCompletionInspector(tuple(_current_view(item) for item in receipts))
    preview_service = EvolutionStablePopulationCandidatePreviewService(
        workspace_root=tmp_path,
        db_path=db_path,
        population_store=population_store,
        stage_completion_inspector=inspector,
        clock=lambda: T0 + timedelta(seconds=3),
    )
    store = EvolutionStablePopulationCompletionStore(db_path)
    service = EvolutionStablePopulationCompletionService(
        workspace_root=tmp_path,
        preview_service=preview_service,
        store=store,
    )
    return {
        "signer": signer,
        "credentials": credentials,
        "snapshot": snapshot,
        "policy": policy,
        "population_store": population_store,
        "receipts": receipts,
        "inspector": inspector,
        "preview_service": preview_service,
        "store": store,
        "service": service,
        "db_path": db_path,
    }


@pytest.mark.asyncio
async def test_completion_is_exact_idempotent_and_shared_by_tool_and_slash(
    tmp_path: Path,
) -> None:
    data = await _authority_services(tmp_path)
    views = await asyncio.gather(
        *(
            data["service"].complete(snapshot_id=data["snapshot"].snapshot_id)
            for _ in range(4)
        )
    )
    assert len({view.receipt.receipt_id for view in views}) == 1
    current = views[0]
    assert current.stable_population_completion_authority
    assert current.receipt.population_denominator == 2
    assert current.receipt.installation_member_ids == tuple(
        sorted(item.payload.member_id for item in data["credentials"])
    )
    assert not current.stable_rollout_authority
    assert not current.promotion_authority
    assert EvolutionStablePopulationCompletionReceipt.model_validate_json(
        current.receipt.model_dump_json()
    ) == current.receipt
    async with aiosqlite.connect(data["db_path"]) as db:
        count = await (
            await db.execute(
                "SELECT COUNT(*) FROM evolution_stable_population_completions"
            )
        ).fetchone()
    assert count == (1,)
    rendered = render_stable_population_completion(current)
    assert "状态：**有效**" in rendered
    assert "Stable rollout authority：`false`" in rendered
    assert "Promotion authority：`false`" in rendered

    tool = EvolutionStablePopulationCompletionTool(
        SimpleNamespace(evolution_stable_population_completion_service=data["service"])
    )
    assert not tool.metadata.read_only
    assert not tool.metadata.destructive
    assert tool.metadata.concurrency_safe
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
        "/evolution stable-population-completion inspect "
        + current.receipt.receipt_id,
    )
    assert strip_ansi(slash) == rendered
    assert slash_engine.calls[0][0].name == "evolution_stable_population_completion"
    assert slash_engine.calls[0][1] == "cli"


@pytest.mark.asyncio
async def test_completion_dynamically_revokes_without_rewriting_receipt(
    tmp_path: Path,
) -> None:
    data = await _authority_services(tmp_path)
    issued = await data["service"].complete(snapshot_id=data["snapshot"].snapshot_id)
    before = issued.receipt.model_dump_json()
    member = data["receipts"][0]
    data["inspector"].views[member.evidence_id] = (
        EvolutionRevalidationStableStageCompletionView(
            receipt=member,
            evidence_source_current=False,
            latest_assessment=True,
            plan_source_current=True,
            baseline_source_current=True,
            liveness_source_current=True,
            outcome_set_current=True,
            invalidation_reasons=("evidence_source_changed",),
            stable_stage_completion_authority=False,
            pause_input_authority=False,
            rollback_input_authority=False,
        )
    )
    revoked = await data["service"].inspect(receipt_id=issued.receipt.receipt_id)
    assert not revoked.stable_population_completion_authority
    assert not revoked.dynamic_revalidation_authority
    assert "dynamic_revalidation_revoked" in revoked.invalidation_reasons
    assert await data["store"].get(issued.receipt.receipt_id) == issued.receipt
    assert issued.receipt.model_dump_json() == before


@pytest.mark.asyncio
async def test_completion_revokes_on_new_population_and_fails_closed_on_corruption(
    tmp_path: Path,
) -> None:
    data = await _authority_services(tmp_path)
    issued = await data["service"].complete(snapshot_id=data["snapshot"].snapshot_id)
    data["policy"][0] = _policy(data["signer"], state="revoked")
    trust_revoked = await data["service"].inspect(
        receipt_id=issued.receipt.receipt_id
    )
    assert not trust_revoked.population_snapshot_authority
    assert "registry_trust_changed" in trust_revoked.invalidation_reasons
    assert not trust_revoked.stable_population_completion_authority
    data["policy"][0] = _policy(data["signer"])
    next_snapshot = data["signer"].issue_snapshot(
        channel="stable",
        credentials=data["credentials"],
        previous=data["snapshot"],
        generated_at=(T0 + timedelta(days=1)).isoformat(),
        valid_from=(T0 + timedelta(days=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=8)).isoformat(),
    )
    await data["population_store"].record(next_snapshot)
    stale = await data["service"].inspect(receipt_id=issued.receipt.receipt_id)
    assert not stale.population_snapshot_authority
    assert "newer_snapshot_exists" in stale.invalidation_reasons
    assert not stale.stable_population_completion_authority

    target = data["receipts"][0]
    async with aiosqlite.connect(data["db_path"]) as db:
        await db.execute(
            "UPDATE evolution_revalidation_stable_stage_completions "
            "SET evidence_json = replace(evidence_json, ?, ?) WHERE evidence_id = ?",
            (target.evidence_sha256, "f" * 64, target.evidence_id),
        )
        await db.commit()
    corrupt = await data["service"].inspect(receipt_id=issued.receipt.receipt_id)
    assert corrupt.receipt_source_current
    assert not corrupt.stable_population_completion_authority
    assert "dynamic_revalidation_revoked" in corrupt.invalidation_reasons


@pytest.mark.asyncio
async def test_store_rejects_tampered_5f5r_dependency(tmp_path: Path) -> None:
    data = await _authority_services(tmp_path)
    material = await data["preview_service"].inspect_authority_material(
        snapshot_id=data["snapshot"].snapshot_id,
        limit=1,
    )
    receipt = _build_completion_receipt(material)
    target = data["receipts"][0]
    async with aiosqlite.connect(data["db_path"]) as db:
        await db.execute(
            "UPDATE evolution_revalidation_stable_stage_completions "
            "SET evidence_sha256 = ? WHERE evidence_id = ?",
            ("0" * 64, target.evidence_id),
        )
        await db.commit()
    with pytest.raises(EvolutionStablePopulationCompletionError) as error:
        await data["store"].record(receipt)
    assert error.value.code == "stable_population_completion_dependency_corrupt"


@pytest.mark.asyncio
async def test_store_fences_preview_to_current_source_set(tmp_path: Path) -> None:
    data = await _authority_services(tmp_path)
    material = await data["preview_service"].inspect_authority_material(
        snapshot_id=data["snapshot"].snapshot_id,
        limit=1,
    )
    receipt = _build_completion_receipt(material)
    replacement = _passing_receipt(
        tmp_path,
        3,
        member_id=data["credentials"][0].payload.member_id,
        snapshot_id=data["snapshot"].snapshot_id,
        snapshot_sha256=data["snapshot"].snapshot_sha256,
        snapshot_sequence=data["snapshot"].payload.sequence,
        population_denominator=data["snapshot"].payload.population_denominator,
    )
    await _record(data["db_path"], replacement)
    with pytest.raises(EvolutionStablePopulationCompletionError) as error:
        await data["store"].record(receipt)
    assert error.value.code == "stable_population_completion_dependency_mismatch"
