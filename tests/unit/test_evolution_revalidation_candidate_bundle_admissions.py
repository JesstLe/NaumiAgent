from __future__ import annotations

import base64
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_candidate_bundle_admissions import (
    EvolutionRevalidationCandidateBundleAdmissionError,
    EvolutionRevalidationCandidateBundleAdmissionService,
    EvolutionRevalidationCandidateBundleAdmissionStore,
)
from naumi_agent.release.build_attestations import (
    ReleaseBuildContext,
    ReleaseBuildSigner,
    ReleaseTrustedBuilderKey,
    create_release_build_trust_policy,
)
from naumi_agent.release.slots import ReleaseSlotStore, host_release_target
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_rollout_stage_advances import _service
from tests.unit.test_release_slots import _bundle


async def _admission_service(
    root: Path,
    *,
    candidate_source_matches=True,
    candidate_backend_content: bytes | None = None,
):
    advance_service, completion, _control, _observed = await _service(root)
    await advance_service.authorize(completion_id=completion.completion_id)
    plan_service = advance_service.completion_service.plan_service
    plan = (await plan_service.inspect(plan_id=completion.plan_id)).plan
    promotion_input = await plan_service.promotion_input_service.issue(contract_id=plan.contract_id)
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
    signer = ReleaseBuildSigner.from_private_key_base64(
        builder_id="naumi-test-release",
        key_id="test-2026-q3",
        key_generation=1,
        private_key_base64=base64.b64encode(b"q" * 32).decode("ascii"),
    )
    build_context = ReleaseBuildContext(
        repository="JesstLe/NaumiAgent",
        workflow_ref="JesstLe/NaumiAgent/.github/workflows/release-binaries.yml@refs/heads/main",
        run_id="candidate-fixture-1",
        run_attempt=1,
        built_at=(T0 + timedelta(hours=9)).isoformat(),
    )
    trusted_key = ReleaseTrustedBuilderKey(
        identity=signer.identity,
        state="active",
        valid_from="2026-07-01T00:00:00+00:00",
        valid_until="2026-08-01T00:00:00+00:00",
    )
    policy_box = [create_release_build_trust_policy((trusted_key,))]
    candidate_bundle = _bundle(
        root,
        version="1.0.0",
        output_name="candidate-release",
        source_commit=(plan.target_head if candidate_source_matches else "f" * 40),
        source_tree_sha256=(plan.target_tree_sha256 if candidate_source_matches else "e" * 64),
        build_signer=signer,
        build_context=build_context,
        backend_content=candidate_backend_content,
    )
    target = host_release_target()
    archive_suffix = "zip" if target.startswith("windows-") else "tar.gz"
    candidate_attestation = (
        root / "candidate-release" / f"naumi-1.0.0-{target}.{archive_suffix}.attestation.json"
    )
    service = EvolutionRevalidationCandidateBundleAdmissionService(
        workspace_root=root,
        stage_advance_service=advance_service,
        plan_service=plan_service,
        release_slot_store=slots,
        trust_policy_provider=lambda: policy_box[0],
        store=EvolutionRevalidationCandidateBundleAdmissionStore(advance_service.store.db_path),
        now=lambda: (T0 + timedelta(hours=9, minutes=10)).isoformat(),
    )
    return (
        service,
        completion,
        candidate_bundle,
        candidate_attestation,
        slots,
        original_pointer,
        plan,
        policy_box,
        signer,
    )


@pytest.mark.asyncio
async def test_exact_candidate_bundle_is_installed_and_booted_without_activation(
    tmp_path: Path,
) -> None:
    (
        service,
        completion,
        bundle,
        attestation,
        slots,
        original_pointer,
        plan,
        _policy,
        _signer,
    ) = await _admission_service(tmp_path)
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "CREATE TABLE evolution_revalidation_candidate_bundle_admissions ("
            "admission_id TEXT PRIMARY KEY, admission_sha256 TEXT NOT NULL UNIQUE, "
            "stage_advance_receipt_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, "
            "slot_id TEXT NOT NULL, admission_json TEXT NOT NULL, admitted_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_revalidation_candidate_bundle_admissions "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-v1",
                "1" * 64,
                "legacy-stage-advance",
                "legacy-plan",
                "legacy-slot",
                '{"schema_version":1}',
                "2026-07-19T09:00:00+00:00",
            ),
        )

    first = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
        build_attestation_path=attestation,
    )
    second = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
        build_attestation_path=attestation,
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
    with sqlite3.connect(service.store.db_path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM evolution_revalidation_candidate_bundle_admissions"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM evolution_revalidation_candidate_bundle_admissions_v2"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_candidate_bundle_with_wrong_signed_source_is_rejected_before_install(
    tmp_path: Path,
) -> None:
    (
        service,
        completion,
        bundle,
        attestation,
        slots,
        original_pointer,
        _plan,
        _policy,
        _signer,
    ) = await _admission_service(tmp_path, candidate_source_matches=False)

    with pytest.raises(EvolutionRevalidationCandidateBundleAdmissionError) as blocked:
        await service.admit(
            completion_id=completion.completion_id,
            bundle_dir=bundle,
            build_attestation_path=attestation,
        )

    assert blocked.value.code == "candidate_build_attestation_source_mismatch"
    assert slots.active() == original_pointer
    assert (
        await service.store.get_by_stage_advance(
            (
                await service.stage_advance_service.inspect(completion_id=completion.completion_id)
            ).receipt.receipt_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_active_pointer_change_revokes_admission_authority(tmp_path: Path) -> None:
    (
        service,
        completion,
        bundle,
        attestation,
        slots,
        _original_pointer,
        _plan,
        _policy,
        _signer,
    ) = await _admission_service(tmp_path)
    admitted = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
        build_attestation_path=attestation,
    )
    slots.activate(admitted.admission.candidate_slot.slot_id)

    stale = await service.inspect(completion_id=completion.completion_id)

    assert not stale.active_pointer_current
    assert not stale.activation_input_authority


@pytest.mark.asyncio
async def test_candidate_slot_tamper_revokes_admission_authority(tmp_path: Path) -> None:
    (
        service,
        completion,
        bundle,
        attestation,
        _slots,
        _original_pointer,
        _plan,
        _policy,
        _signer,
    ) = await _admission_service(tmp_path)
    admitted = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
        build_attestation_path=attestation,
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


@pytest.mark.asyncio
async def test_missing_or_untrusted_build_attestation_never_installs_candidate(
    tmp_path: Path,
) -> None:
    (
        service,
        completion,
        bundle,
        attestation,
        slots,
        original_pointer,
        _plan,
        policy_box,
        _signer,
    ) = await _admission_service(tmp_path)
    attestation_bytes = attestation.read_bytes()
    attestation.unlink()

    with pytest.raises(EvolutionRevalidationCandidateBundleAdmissionError) as missing:
        await service.admit(
            completion_id=completion.completion_id,
            bundle_dir=bundle,
            build_attestation_path=attestation,
        )
    assert missing.value.code == "candidate_build_attestation_invalid"
    assert slots.active() == original_pointer
    assert len(tuple(slots.slots_dir.glob("relslot_*"))) == 1

    other_signer = ReleaseBuildSigner.from_private_key_base64(
        builder_id="unrelated-builder",
        key_id="unrelated-key",
        key_generation=1,
        private_key_base64=base64.b64encode(b"u" * 32).decode("ascii"),
    )
    policy_box[0] = create_release_build_trust_policy(
        (
            ReleaseTrustedBuilderKey(
                identity=other_signer.identity,
                state="active",
                valid_from="2026-07-01T00:00:00+00:00",
            ),
        )
    )
    attestation.write_bytes(attestation_bytes)
    with pytest.raises(EvolutionRevalidationCandidateBundleAdmissionError) as untrusted:
        await service.admit(
            completion_id=completion.completion_id,
            bundle_dir=bundle,
            build_attestation_path=attestation,
        )
    assert untrusted.value.code == "candidate_build_attestation_invalid"


@pytest.mark.asyncio
async def test_trust_policy_rotation_or_revocation_fences_existing_admission(
    tmp_path: Path,
) -> None:
    (
        service,
        completion,
        bundle,
        attestation,
        _slots,
        _pointer,
        _plan,
        policy_box,
        signer,
    ) = await _admission_service(tmp_path)
    admitted = await service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
        build_attestation_path=attestation,
    )
    original_key = policy_box[0].keys[0]
    additional_signer = ReleaseBuildSigner.from_private_key_base64(
        builder_id="naumi-test-release",
        key_id="test-2026-q4",
        key_generation=2,
        private_key_base64=base64.b64encode(b"r" * 32).decode("ascii"),
    )
    additional_key = ReleaseTrustedBuilderKey(
        identity=additional_signer.identity,
        state="active",
        valid_from="2026-07-01T00:00:00+00:00",
    )
    policy_box[0] = create_release_build_trust_policy((original_key, additional_key))

    rotated = await service.inspect(completion_id=completion.completion_id)

    assert rotated.build_attestation_current
    assert not rotated.trust_policy_current
    assert not rotated.activation_input_authority

    policy_box[0] = create_release_build_trust_policy(
        (
            ReleaseTrustedBuilderKey(
                identity=signer.identity,
                state="revoked",
                valid_from="2026-07-01T00:00:00+00:00",
                revoked_at="2026-07-20T00:00:00+00:00",
            ),
        )
    )
    revoked = await service.inspect(completion_id=completion.completion_id)

    assert admitted.activation_input_authority
    assert not revoked.build_attestation_current
    assert not revoked.trust_policy_current
    assert not revoked.activation_input_authority
