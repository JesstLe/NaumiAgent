from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
from datetime import timedelta
from pathlib import Path

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.evolution.revalidation_percentage_cohort_assignments import (
    EvolutionRevalidationPercentageCohortAssignmentError,
    EvolutionRevalidationPercentageCohortAssignmentService,
    EvolutionRevalidationPercentageCohortAssignmentStore,
    _selection,
)
from naumi_agent.release.population_registry import (
    ReleasePopulationRegistrySigner,
    ReleasePopulationSnapshotStore,
    ReleaseTrustedPopulationRegistryKey,
    create_release_population_trust_policy,
)
from tests.unit.test_evolution_revalidation_opt_in_stage_advances import _fixture


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_percentage_assignment_is_deterministic_proven_and_revocable(
    tmp_path: Path,
) -> None:
    passing, _declined, plan, _control_store, build_advance, now = await _fixture(
        tmp_path
    )
    advance_service = build_advance("advance", [])
    advance_view = await advance_service.authorize(
        evidence_id=passing.evidence_id,
        subject_id=passing.subject_id,
    )
    assert advance_view.percentage_stage_entry_authority

    registry = ReleasePopulationRegistrySigner.from_private_keys_base64(
        registry_id="naumi-production-registry",
        key_id="population-2026-01",
        key_generation=1,
        signing_private_key_base64=_b64(bytes(range(32))),
        pseudonym_key_base64=_b64(bytes(reversed(range(32)))),
    )
    installation_keys: dict[str, Ed25519PrivateKey] = {}
    credentials = []
    for index in range(128):
        seed = hashlib.sha256(f"managed-installation-{index}".encode()).digest()
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        public_key = private_key.public_key().public_bytes_raw()
        credential = registry.issue_credential(
            installation_public_key_base64=_b64(public_key),
            channel="stable",
            registered_at=(now - timedelta(days=1)).isoformat(),
            expires_at=(now + timedelta(days=30)).isoformat(),
        )
        installation_keys[credential.payload.member_id] = private_key
        credentials.append(credential)
    snapshot = registry.issue_snapshot(
        channel="stable",
        credentials=tuple(credentials),
        previous=None,
        generated_at=now.isoformat(),
        valid_from=(now + timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
    )
    trust_policy = create_release_population_trust_policy(
        (
            ReleaseTrustedPopulationRegistryKey(
                identity=registry.identity,
                state="active",
                valid_from=(now - timedelta(days=2)).isoformat(),
            ),
        )
    )
    population_store = ReleasePopulationSnapshotStore(
        tmp_path / ".naumi" / "population.db",
        trust_policy_provider=lambda: trust_policy,
        clock=lambda: now + timedelta(seconds=2),
    )
    await population_store.record(snapshot)

    selections = {
        member_id: _selection(plan, snapshot, member_id)
        for member_id in installation_keys
    }
    selected_member = next(
        member_id for member_id, value in selections.items() if value["member_selected"]
    )
    unselected_members = [
        member_id
        for member_id, value in selections.items()
        if not value["member_selected"]
    ]
    unselected_member, invalid_proof_member = unselected_members[:2]

    def _service_for(member_id: str, *, wrong_key: bool = False):
        private_key = (
            Ed25519PrivateKey.from_private_bytes(b"x" * 32)
            if wrong_key
            else installation_keys[member_id]
        )

        async def sign(payload: bytes) -> str:
            return _b64(private_key.sign(payload))

        store = EvolutionRevalidationPercentageCohortAssignmentStore(
            advance_service.store.db_path,
            advance_service=advance_service,
            population_store=population_store,
            plan_service=advance_service.plan_service,
        )
        return EvolutionRevalidationPercentageCohortAssignmentService(
            workspace_root=tmp_path,
            channel="stable",
            advance_service=advance_service,
            population_store=population_store,
            plan_service=advance_service.plan_service,
            store=store,
            sign_assignment_challenge=sign,
            clock=lambda: now + timedelta(seconds=3),
        )

    services = tuple(_service_for(selected_member) for _ in range(4))
    selected_views = await asyncio.gather(
        *(
            service.assign(
                stage_completion_evidence_id=passing.evidence_id,
                member_id=selected_member,
            )
            for service in services
        )
    )
    selected = selected_views[0]
    assert all(item == selected for item in selected_views)
    assert selected.population_assignment_enforced
    assert selected.percentage_cohort_membership_authority
    assert selected.assignment.proof_of_possession_verified
    assert selected.assignment.population_denominator == 128
    assert selected.assignment.target_member_count == max(
        1, math.ceil(128 * plan.stages[2].exposure_percent / 100)
    )
    assert len(selected.assignment.selected_member_ids) == (
        selected.assignment.target_member_count
    )
    assert not selected.percentage_rollout_authority
    assert not selected.deployment_authority
    assert not selected.stable_rollout_authority
    assert not selected.promotion_authority

    unselected_service = _service_for(unselected_member)
    unselected = await unselected_service.assign(
        stage_completion_evidence_id=passing.evidence_id,
        member_id=unselected_member,
    )
    assert unselected.population_assignment_enforced
    assert not unselected.percentage_cohort_membership_authority

    with pytest.raises(EvolutionRevalidationPercentageCohortAssignmentError) as proof:
        await _service_for(invalid_proof_member, wrong_key=True).assign(
            stage_completion_evidence_id=passing.evidence_id,
            member_id=invalid_proof_member,
        )
    assert proof.value.code == "percentage_assignment_proof_invalid"
    assert (
        await _service_for(invalid_proof_member).store.get_by_source(
            advance_receipt_id=advance_view.receipt.receipt_id,
            snapshot_id=snapshot.snapshot_id,
            member_id=invalid_proof_member,
        )
        is None
    )
    with pytest.raises(EvolutionRevalidationPercentageCohortAssignmentError) as missing:
        await services[0].assign(
            stage_completion_evidence_id=passing.evidence_id,
            member_id="relpopmember_" + "f" * 24,
        )
    assert missing.value.code == "percentage_assignment_member_missing"

    next_snapshot = registry.issue_snapshot(
        channel="stable",
        credentials=tuple(credentials[:-1]),
        previous=snapshot,
        generated_at=(now + timedelta(seconds=3)).isoformat(),
        valid_from=(now + timedelta(seconds=4)).isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
    )
    population_store.clock = lambda: now + timedelta(seconds=5)
    await population_store.record(next_snapshot)
    historical = await services[0].inspect(
        advance_receipt_id=selected.assignment.stage_advance.receipt_id,
        snapshot_id=snapshot.snapshot_id,
        member_id=selected_member,
    )
    assert not historical.population_snapshot_current
    assert not historical.population_assignment_enforced
    assert not historical.percentage_cohort_membership_authority

    async with aiosqlite.connect(advance_service.store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_percentage_cohort_assignments "
            "SET assignment_json = replace(assignment_json, ?, ?) "
            "WHERE assignment_id = ?",
            (
                selected.assignment.assignment_sha256,
                "0" * 64,
                selected.assignment.assignment_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationPercentageCohortAssignmentError) as corrupt:
        await services[0].inspect(
            advance_receipt_id=selected.assignment.stage_advance.receipt_id,
            snapshot_id=snapshot.snapshot_id,
            member_id=selected_member,
        )
    assert corrupt.value.code == "percentage_assignment_source_invalid"
