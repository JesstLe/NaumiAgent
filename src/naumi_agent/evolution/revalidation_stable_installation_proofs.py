"""Cryptographic installation possession proofs for stable deployment intents."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
)

EVOLUTION_REVALIDATION_STABLE_INSTALLATION_PROOF_DOMAIN = (
    "naumi.evolution.stable-installation-proof.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"

SignStableInstallationChallenge = Callable[[bytes], Awaitable[str]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableInstallationProofPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal[
        "naumi.evolution.stable-installation-proof.v1"
    ] = EVOLUTION_REVALIDATION_STABLE_INSTALLATION_PROOF_DOMAIN
    workspace_root: str = Field(min_length=1, max_length=4096)
    stage_advance_receipt_id: str = Field(pattern=r"^evrepercentadvance_[0-9a-f]{24}$")
    stage_advance_receipt_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=10_000)
    credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    credential_sha256: str = Field(pattern=_SHA256_RE)
    member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    archive_admission_id: str = Field(pattern=r"^relarchiveadmission_[0-9a-f]{24}$")
    archive_admission_sha256: str = Field(pattern=_SHA256_RE)
    candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    candidate_slot_sha256: str = Field(pattern=_SHA256_RE)
    channel: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    installation_target: str = Field(min_length=1, max_length=255)
    previous_pointer_sha256: str = Field(pattern=_SHA256_RE)
    previous_pointer_generation: int = Field(ge=1, le=1_000_000_000)
    stable_exposure_percent: Literal[100] = 100
    signed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Installation Proof workspace 必须 canonical。")
        _aware(self.signed_at)
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionRevalidationStableInstallationProof(_StrictModel):
    schema_version: Literal[1] = 1
    proof_id: str = Field(pattern=r"^evrestableproof_[0-9a-f]{24}$")
    proof_sha256: str = Field(pattern=_SHA256_RE)
    payload: EvolutionRevalidationStableInstallationProofPayload
    installation_credential: ReleaseManagedInstallationCredential
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    credential_binding_verified: Literal[True] = True
    population_membership_verified: Literal[False] = False
    proof_of_possession_verified: Literal[True] = True
    percentage_selection_required: Literal[False] = False
    private_key_persisted: Literal[False] = False
    deployment_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        credential = self.installation_credential
        payload = self.payload
        if not (
            payload.credential_id == credential.credential_id
            and payload.credential_sha256 == credential.credential_sha256
            and payload.member_id == credential.payload.member_id
            and payload.channel == credential.payload.channel
        ):
            raise ValueError("Stable Installation Proof Credential 投影不一致。")
        public_key = _decode_base64(
            credential.payload.installation_public_key_base64,
            expected_bytes=32,
            label="installation public key",
        )
        signature = _decode_base64(
            self.signature_base64,
            expected_bytes=64,
            label="stable installation signature",
        )
        if not (
            hmac.compare_digest(
                hashlib.sha256(public_key).hexdigest(),
                credential.payload.installation_public_key_sha256,
            )
            and hmac.compare_digest(
                hashlib.sha256(signature).hexdigest(), self.signature_sha256
            )
        ):
            raise ValueError("Stable Installation Proof key/signature identity 不一致。")
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature, payload.canonical_bytes()
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("Stable Installation proof-of-possession 无效。") from exc
        core = self.model_dump(mode="json", exclude={"proof_id", "proof_sha256"})
        digest = _digest(core)
        if self.proof_sha256 != digest or self.proof_id != (
            f"evrestableproof_{digest[:24]}"
        ):
            raise ValueError("Stable Installation Proof content identity 不一致。")
        return self


class EvolutionRevalidationStableInstallationProofError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def build_stable_installation_proof(
    *,
    payload: EvolutionRevalidationStableInstallationProofPayload,
    credential: ReleaseManagedInstallationCredential,
    sign_challenge: SignStableInstallationChallenge,
) -> EvolutionRevalidationStableInstallationProof:
    if not isinstance(payload, EvolutionRevalidationStableInstallationProofPayload):
        raise TypeError("Stable Installation Proof 需要 typed payload。")
    if not isinstance(credential, ReleaseManagedInstallationCredential):
        raise TypeError("Stable Installation Proof 需要 managed Credential。")
    if not callable(sign_challenge):
        raise TypeError("Stable Installation Proof 需要 installation signer port。")
    try:
        signature_base64 = await sign_challenge(payload.canonical_bytes())
        signature = _decode_base64(
            signature_base64,
            expected_bytes=64,
            label="stable installation signature",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableInstallationProofError(
            "stable_installation_signer_unavailable",
            "Installation proof signer 当前不可用或返回无效签名。",
        ) from exc
    core = {
        "schema_version": 1,
        "payload": payload.model_dump(mode="json"),
        "installation_credential": credential.model_dump(mode="json"),
        "signature_algorithm": "ed25519",
        "signature_base64": signature_base64,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "credential_binding_verified": True,
        "population_membership_verified": False,
        "proof_of_possession_verified": True,
        "percentage_selection_required": False,
        "private_key_persisted": False,
        "deployment_authority": False,
        "stable_rollout_authority": False,
    }
    digest = _digest(core)
    try:
        return EvolutionRevalidationStableInstallationProof.model_validate(
            {
                **core,
                "payload": payload,
                "installation_credential": credential,
                "proof_id": f"evrestableproof_{digest[:24]}",
                "proof_sha256": digest,
            }
        )
    except ValueError as exc:
        raise EvolutionRevalidationStableInstallationProofError(
            "stable_installation_proof_invalid",
            "Installation proof-of-possession 验证失败。",
        ) from exc


def _decode_base64(value: str, *, expected_bytes: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是 Base64 字符串。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} Base64 无效。") from exc
    if len(decoded) != expected_bytes or not hmac.compare_digest(
        base64.b64encode(decoded).decode("ascii"), value
    ):
        raise ValueError(f"{label} 长度或 canonical Base64 无效。")
    return decoded


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含 timezone。")
    return parsed.astimezone(UTC)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()
