from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_candidate_bundle_admissions import (
    EvolutionRevalidationCandidateBundleAdmissionError,
    EvolutionRevalidationCandidateBundleAdmissionService,
    EvolutionRevalidationCandidateBundleAdmissionStore,
)
from naumi_agent.release.slots import ReleaseSlotStore
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_rollout_stage_advances import _service
from tests.unit.test_release_slots import _bundle


async def _admission_service(root: Path, *, candidate_source_matches=True):
    advance_service, completion, _control, _observed = await _service(root)
    await advance_service.authorize(completion_id=completion.completion_id)
    plan_service = advance_service.completion_service.plan_service
    plan = (await plan_service.inspect(plan_id=completion.plan_id)).plan
    promotion_input = await plan_service.promotion_input_service.issue(
        contract_id=plan.contract_id
    )
    rollback = promotion_input.prior_input.rollback
    slots = ReleaseSlotStore(root / "installed")
    baseline_bundle = _bundle(
        root,
        version="0.9.0",
        output_name="baseline-release",
        source_commit=rollback.baseline_commit,
        source_tree_sha256=rollback.baseline_tree_sha256,
    )
    baseline = slots.install(baseline_bundle)
    slots.verify_bootable(baseline.slot_id)
    original_pointer = slots.activate(baseline.slot_id)
    candidate_bundle = _bundle(
        root,
        version="1.0.0",
        output_name="candidate-release",
        source_commit=(plan.target_head if candidate_source_matches else "f" * 40),
        source_tree_sha256=(
            plan.target_tree_sha256 if candidate_source_matches else "e" * 64
        ),
    )
    service = EvolutionRevalidationCandidateBundleAdmissionService(
        workspace_root=root,
        stage_advance_service=advance_service,
        plan_service=plan_service,
        release_slot_store=slots,
        store=EvolutionRevalidationCandidateBundleAdmissionStore(
            advance_service.store.db_path
        ),
        now=lambda: (T0 + timedelta(hours=9, minutes=10)).isoformat(),
    )
    return service, completion, candidate_bundle, slots, original_pointer, plan


@pytest.mark.asyncio
async def test_exact_candidate_bundle_is_installed_and_booted_without_activation(
    tmp_path: Path,
) -> None:
    service, completion, bundle, slots, original_pointer, plan = (
        await _admission_service(tmp_path)
    )

    first = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
    )
    second = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
    )

    assert first == second
    assert first.activation_input_authority
    assert first.admission.candidate_id == plan.candidate_id
    assert first.admission.candidate_slot.source_commit == plan.target_head
    assert first.admission.boot_receipt.bootable
    assert first.admission.rollback_slot_verified
    assert slots.active() == original_pointer
    assert not first.admission.deployment_authority
    assert not first.admission.active_pointer_switched


@pytest.mark.asyncio
async def test_candidate_bundle_with_wrong_source_is_rejected_after_safe_install(
    tmp_path: Path,
) -> None:
    service, completion, bundle, slots, original_pointer, _plan = (
        await _admission_service(tmp_path, candidate_source_matches=False)
    )

    with pytest.raises(EvolutionRevalidationCandidateBundleAdmissionError) as blocked:
        await service.admit(
            completion_id=completion.completion_id,
            bundle_dir=bundle,
        )

    assert blocked.value.code == "candidate_bundle_source_mismatch"
    assert slots.active() == original_pointer
    assert await service.store.get_by_stage_advance(
        (await service.stage_advance_service.inspect(
            completion_id=completion.completion_id
        )).receipt.receipt_id
    ) is None


@pytest.mark.asyncio
async def test_active_pointer_change_revokes_admission_authority(tmp_path: Path) -> None:
    service, completion, bundle, slots, _original_pointer, _plan = (
        await _admission_service(tmp_path)
    )
    admitted = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
    )
    slots.activate(admitted.admission.candidate_slot.slot_id)

    stale = await service.inspect(completion_id=completion.completion_id)

    assert not stale.active_pointer_current
    assert not stale.activation_input_authority


@pytest.mark.asyncio
async def test_candidate_slot_tamper_revokes_admission_authority(tmp_path: Path) -> None:
    service, completion, bundle, _slots, _original_pointer, _plan = (
        await _admission_service(tmp_path)
    )
    admitted = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
    )
    binary = (
        Path(admitted.admission.candidate_slot.bundle_dir)
        / admitted.admission.candidate_slot.backend_path
    )
    binary.chmod(0o755)
    binary.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")

    stale = await service.inspect(completion_id=completion.completion_id)

    assert not stale.slot_current
    assert not stale.boot_receipt_current
    assert not stale.activation_input_authority
