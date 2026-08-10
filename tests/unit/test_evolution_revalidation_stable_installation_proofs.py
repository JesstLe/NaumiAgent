from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.evolution.revalidation_stable_installation_proofs import (
    EvolutionRevalidationStableInstallationProof,
    EvolutionRevalidationStableInstallationProofError,
    EvolutionRevalidationStableInstallationProofPayload,
    build_stable_installation_proof,
)
from tests.unit.test_release_population_registry import T0, _signer


def _payload(workspace: Path, credential):
    return EvolutionRevalidationStableInstallationProofPayload(
        workspace_root=str(workspace.resolve()),
        stage_advance_receipt_id="evrepercentadvance_" + "1" * 24,
        stage_advance_receipt_sha256="1" * 64,
        population_snapshot_id="relpopsnapshot_" + "2" * 24,
        population_snapshot_sha256="2" * 64,
        population_snapshot_sequence=7,
        population_denominator=128,
        credential_id=credential.credential_id,
        credential_sha256=credential.credential_sha256,
        member_id=credential.payload.member_id,
        plan_id="evrerolloutplan_" + "3" * 24,
        plan_sha256="3" * 64,
        candidate_id="evc_" + "4" * 24,
        candidate_revision=2,
        archive_admission_id="relarchiveadmission_" + "5" * 24,
        archive_admission_sha256="5" * 64,
        candidate_slot_id="relslot_" + "6" * 24,
        candidate_slot_sha256="6" * 64,
        channel="stable",
        installation_target="macos-arm64",
        previous_pointer_sha256="7" * 64,
        previous_pointer_generation=9,
        signed_at=(T0 + timedelta(seconds=2)).isoformat(),
    )


@pytest.mark.asyncio
async def test_stable_installation_proof_verifies_real_key_without_selection(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"stable-installation").digest()
    )
    public = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    credential = _signer().issue_credential(
        installation_public_key_base64=public,
        channel="stable",
        registered_at=(T0 - timedelta(days=1)).isoformat(),
        expires_at=(T0 + timedelta(days=30)).isoformat(),
    )

    async def sign(challenge: bytes) -> str:
        return base64.b64encode(private_key.sign(challenge)).decode()

    proof = await build_stable_installation_proof(
        payload=_payload(tmp_path, credential),
        credential=credential,
        sign_challenge=sign,
    )
    assert proof.proof_of_possession_verified
    assert proof.credential_binding_verified
    assert not proof.population_membership_verified
    assert not proof.percentage_selection_required
    assert not proof.deployment_authority
    assert "private_key_base64" not in proof.model_dump_json().casefold()
    tampered = proof.model_dump(mode="json")
    tampered["payload"]["installation_target"] = "linux-x64"
    with pytest.raises(ValueError, match="proof-of-possession"):
        EvolutionRevalidationStableInstallationProof.model_validate(tampered)


@pytest.mark.asyncio
async def test_stable_installation_proof_rejects_wrong_key_and_credential(
    tmp_path: Path,
) -> None:
    owner = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    public = base64.b64encode(owner.public_key().public_bytes_raw()).decode()
    signer = _signer()
    credential = signer.issue_credential(
        installation_public_key_base64=public,
        channel="stable",
        registered_at=(T0 - timedelta(days=1)).isoformat(),
        expires_at=(T0 + timedelta(days=30)).isoformat(),
    )

    async def wrong_key(challenge: bytes) -> str:
        return base64.b64encode(attacker.sign(challenge)).decode()

    with pytest.raises(EvolutionRevalidationStableInstallationProofError) as error:
        await build_stable_installation_proof(
            payload=_payload(tmp_path, credential),
            credential=credential,
            sign_challenge=wrong_key,
        )
    assert error.value.code == "stable_installation_proof_invalid"

    other = signer.issue_credential(
        installation_public_key_base64=base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        ).decode(),
        channel="stable",
        registered_at=(T0 - timedelta(days=1)).isoformat(),
        expires_at=(T0 + timedelta(days=30)).isoformat(),
    )
    with pytest.raises(EvolutionRevalidationStableInstallationProofError):
        await build_stable_installation_proof(
            payload=_payload(tmp_path, credential),
            credential=other,
            sign_challenge=wrong_key,
        )
