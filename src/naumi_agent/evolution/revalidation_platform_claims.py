"""Authenticated remote Worker claims with renewable, fenced leases."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.worker_contract import WorkerContract
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservation,
    WorkerCapacityReservationState,
    WorkerRegistryStore,
)
from naumi_agent.evolution.revalidation_platform_dispatches import (
    EvolutionRevalidationPlatformDispatch,
    EvolutionRevalidationPlatformDispatchStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)

EVOLUTION_REVALIDATION_WORKER_IDENTITY_POLICY = (
    "evolution-revalidation-worker-identity-v1"
)
EVOLUTION_REVALIDATION_PLATFORM_CLAIM_POLICY = (
    "evolution-revalidation-platform-claim-v1"
)
EVOLUTION_REVALIDATION_PLATFORM_CLAIM_DOMAIN = (
    "naumi.evolution.revalidation-platform-claim.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
type ChallengeAction = Literal["claim", "renew"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationWorkerIdentity(_StrictModel):
    """Supervisor-attested public key for one exact Worker incarnation."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-worker-identity-v1"] = (
        EVOLUTION_REVALIDATION_WORKER_IDENTITY_POLICY
    )
    identity_id: str = Field(pattern=r"^evrevalworkerid_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    public_key_base64: str = Field(pattern=_B64_32_RE)
    public_key_sha256: str = Field(pattern=_SHA256_RE)
    enrolled_at: str = Field(min_length=1, max_length=100)
    attestation_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    supervisor_attestation_sha256: str = Field(pattern=_SHA256_RE)
    private_key_stored: Literal[False] = False
    claim_authority: Literal[True] = True
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        raw_key = _decode_b64(self.public_key_base64, expected=32, field="public key")
        if not hmac.compare_digest(
            self.public_key_sha256, hashlib.sha256(raw_key).hexdigest()
        ):
            raise ValueError("Worker Identity public key 摘要不一致。")
        _aware(self.enrolled_at)
        core = self.model_dump(
            mode="json",
            exclude={
                "identity_id",
                "identity_sha256",
                "supervisor_attestation_sha256",
            },
        )
        digest = _digest(core)
        if self.identity_sha256 != digest:
            raise ValueError("Worker Identity digest 不一致。")
        if self.identity_id != f"evrevalworkerid_{digest[:24]}":
            raise ValueError("Worker Identity id 不一致。")
        return self


class EvolutionRevalidationPlatformClaimPayload(_StrictModel):
    domain: Literal["naumi.evolution.revalidation-platform-claim.v1"] = (
        EVOLUTION_REVALIDATION_PLATFORM_CLAIM_DOMAIN
    )
    action: ChallengeAction
    challenge_id: str = Field(pattern=r"^evrevalclaimchallenge_[0-9a-f]{24}$")
    nonce_base64: str = Field(pattern=_B64_32_RE)
    dispatch_id: str = Field(pattern=r"^evrevalplatdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evrevalplatjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evrevalplatres_[0-9a-f]{24}$")
    identity_id: str = Field(pattern=r"^evrevalworkerid_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    claim_id: str = Field(pattern=r"^(?:|evrevalplatclaim_[0-9a-f]{24})$")
    sequence: int = Field(ge=1)
    previous_receipt_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    lease_seconds: int = Field(ge=5, le=300)

    @model_validator(mode="after")
    def _transition(self) -> Self:
        if _aware(self.expires_at) <= _aware(self.issued_at):
            raise ValueError("Platform Claim challenge expiry 无效。")
        if self.action == "claim":
            if self.claim_id or self.sequence != 1 or self.previous_receipt_sha256:
                raise ValueError("首次 Platform Claim payload 状态无效。")
        elif not self.claim_id or self.sequence < 2 or not self.previous_receipt_sha256:
            raise ValueError("Platform Claim renewal payload 状态无效。")
        expected = _challenge_id(
            action=self.action,
            nonce_base64=self.nonce_base64,
            dispatch_sha256=self.dispatch_sha256,
            identity_sha256=self.identity_sha256,
            claim_id=self.claim_id,
            sequence=self.sequence,
            previous_receipt_sha256=self.previous_receipt_sha256,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            lease_seconds=self.lease_seconds,
        )
        if self.challenge_id != expected:
            raise ValueError("Platform Claim challenge identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionRevalidationPlatformClaimChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-platform-claim-v1"] = (
        EVOLUTION_REVALIDATION_PLATFORM_CLAIM_POLICY
    )
    payload: EvolutionRevalidationPlatformClaimPayload
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    state: Literal["pending"] = "pending"
    signature_algorithm: Literal["ed25519"] = "ed25519"
    one_time: Literal[True] = True
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.signable_payload_sha256 != hashlib.sha256(
            self.payload.canonical_bytes()
        ).hexdigest():
            raise ValueError("Platform Claim signable payload digest 不一致。")
        return self


class EvolutionRevalidationPlatformClaimReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-platform-claim-v1"] = (
        EVOLUTION_REVALIDATION_PLATFORM_CLAIM_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrevalclaimreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evrevalplatclaim_[0-9a-f]{24}$")
    sequence: int = Field(ge=1)
    action: ChallengeAction
    challenge_id: str = Field(pattern=r"^evrevalclaimchallenge_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^evrevalplatdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evrevalplatjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evrevalplatres_[0-9a-f]{24}$")
    identity_id: str = Field(pattern=r"^evrevalworkerid_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    lease_epoch: int = Field(ge=1)
    previous_receipt_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    signature_base64: str = Field(pattern=_B64_64_RE)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    signature_verified: Literal[True] = True
    claimed_at: str = Field(min_length=1, max_length=100)
    lease_expires_at: str = Field(min_length=1, max_length=100)
    state: Literal["claimed"] = "claimed"
    capacity_reserved: Literal[True] = True
    worker_claimed: Literal[True] = True
    transport_delivered: Literal[True] = True
    execution_started: Literal[False] = False
    result_received: Literal[False] = False
    cohort_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_b64(self.signature_base64, expected=64, field="signature")
        if self.signature_sha256 != hashlib.sha256(signature).hexdigest():
            raise ValueError("Platform Claim signature digest 不一致。")
        if _aware(self.lease_expires_at) <= _aware(self.claimed_at):
            raise ValueError("Platform Claim lease expiry 无效。")
        if self.action == "claim":
            if self.sequence != 1 or self.lease_epoch != 1 or self.previous_receipt_sha256:
                raise ValueError("首次 Platform Claim receipt 状态无效。")
        elif (
            self.sequence < 2
            or self.lease_epoch != self.sequence
            or not self.previous_receipt_sha256
        ):
            raise ValueError("Platform Claim renewal receipt 状态无效。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest:
            raise ValueError("Platform Claim receipt digest 不一致。")
        if self.receipt_id != f"evrevalclaimreceipt_{digest[:24]}":
            raise ValueError("Platform Claim receipt id 不一致。")
        expected_claim_id = (
            "evrevalplatclaim_"
            f"{hashlib.sha256(self.dispatch_id.encode()).hexdigest()[:24]}"
        )
        if self.claim_id != expected_claim_id:
            raise ValueError("Platform Claim id 不一致。")
        return self


class EvolutionRevalidationPlatformClaimView(_StrictModel):
    receipt: EvolutionRevalidationPlatformClaimReceipt
    status: Literal["current", "expired", "stale"]
    lease_active: bool
    worker_claimed: bool
    transport_delivered: bool
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    cohort_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = self.status == "current"
        if self.lease_active is not current:
            raise ValueError("Platform Claim lease view 投影不一致。")
        if self.worker_claimed is not current or self.transport_delivered is not current:
            raise ValueError("Platform Claim authority view 投影不一致。")
        return self


class EvolutionRevalidationPlatformClaimError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def issue_evolution_revalidation_worker_identity(
    *,
    contract: WorkerContract,
    public_key_base64: str,
    enrolled_at: str,
    supervisor_key: bytes,
) -> EvolutionRevalidationWorkerIdentity:
    """Seal a public key using control-plane-only runtime key material."""
    if not isinstance(contract, WorkerContract):
        raise TypeError("contract 必须是 WorkerContract。")
    if not isinstance(supervisor_key, bytes) or len(supervisor_key) < 32:
        raise ValueError("Supervisor attestation key 至少需要 32 bytes。")
    normalized = _canonical_b64(public_key_base64, expected=32, field="public key")
    public_sha = hashlib.sha256(base64.b64decode(normalized)).hexdigest()
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_WORKER_IDENTITY_POLICY,
        "worker_id": contract.worker_id,
        "worker_instance_id": contract.instance_id,
        "worker_epoch": contract.epoch,
        "worker_contract_sha256": contract.contract_sha256,
        "signature_algorithm": "ed25519",
        "public_key_base64": normalized,
        "public_key_sha256": public_sha,
        "enrolled_at": _aware(enrolled_at).isoformat(),
        "attestation_algorithm": "hmac-sha256",
        "private_key_stored": False,
        "claim_authority": True,
        "execution_authority": False,
        "result_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    attestation = hmac.new(supervisor_key, _canonical(core), hashlib.sha256).hexdigest()
    return EvolutionRevalidationWorkerIdentity.model_validate(
        {
            **core,
            "identity_id": f"evrevalworkerid_{digest[:24]}",
            "identity_sha256": digest,
            "supervisor_attestation_sha256": attestation,
        }
    )


class EvolutionRevalidationPlatformClaimStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def record_identity(self, identity: EvolutionRevalidationWorkerIdentity):
        item = EvolutionRevalidationWorkerIdentity.model_validate_json(
            identity.model_dump_json()
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            existing = await _identity_for_incarnation(db, item)
            if existing is not None:
                restored = _identity_from_json(existing["identity_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformClaimError(
                        "platform_claim_identity_conflict",
                        "同一 Worker incarnation 已绑定不同认证密钥；换钥必须提升 Worker epoch。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_worker_identities "
                "(identity_id, identity_sha256, worker_id, worker_instance_id, worker_epoch, "
                "worker_contract_sha256, public_key_sha256, identity_json, enrolled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.identity_id,
                    item.identity_sha256,
                    item.worker_id,
                    item.worker_instance_id,
                    item.worker_epoch,
                    item.worker_contract_sha256,
                    item.public_key_sha256,
                    item.model_dump_json(),
                    item.enrolled_at,
                ),
            )
            await db.commit()
        return item

    async def get_identity(self, identity_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT identity_json FROM evolution_revalidation_worker_identities "
                    "WHERE identity_id = ?",
                    (identity_id,),
                )
            ).fetchone()
        return None if row is None else _identity_from_json(row["identity_json"])

    async def get_identity_for_dispatch(
        self, dispatch: EvolutionRevalidationPlatformDispatch
    ):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT identity_json FROM evolution_revalidation_worker_identities "
                    "WHERE worker_id = ? AND worker_instance_id = ? AND worker_epoch = ? "
                    "AND worker_contract_sha256 = ?",
                    (
                        dispatch.worker_id,
                        dispatch.worker_instance_id,
                        dispatch.worker_epoch,
                        dispatch.worker_contract_sha256,
                    ),
                )
            ).fetchone()
        return None if row is None else _identity_from_json(row["identity_json"])

    async def get_pending(
        self,
        *,
        dispatch_id: str,
        action: ChallengeAction,
        assessed_at: str,
    ):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE evolution_revalidation_platform_claim_challenges "
                "SET state = 'expired' WHERE dispatch_id = ? AND action = ? "
                "AND state = 'pending' AND expires_at <= ?",
                (dispatch_id, action, _aware(assessed_at).isoformat()),
            )
            row = await (
                await db.execute(
                    "SELECT challenge_json FROM evolution_revalidation_platform_claim_challenges "
                    "WHERE dispatch_id = ? AND action = ? AND state = 'pending'",
                    (dispatch_id, action),
                )
            ).fetchone()
            await db.commit()
        return None if row is None else _challenge_from_json(row["challenge_json"])

    async def record_challenge(
        self, challenge: EvolutionRevalidationPlatformClaimChallenge
    ):
        item = EvolutionRevalidationPlatformClaimChallenge.model_validate_json(
            challenge.model_dump_json()
        )
        payload = item.payload
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dispatch = await _dispatch_row(db, payload.dispatch_id)
            identity = await _identity_row(db, payload.identity_id)
            if not _rows_match_payload(dispatch, identity, payload):
                await db.rollback()
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_authority_mismatch",
                    "Platform Claim challenge 的 Dispatch 或 Worker Identity 已失效。",
                )
            pending = await (
                await db.execute(
                    "SELECT challenge_json FROM evolution_revalidation_platform_claim_challenges "
                    "WHERE dispatch_id = ? AND action = ? AND state = 'pending'",
                    (payload.dispatch_id, payload.action),
                )
            ).fetchone()
            if pending is not None:
                restored = _challenge_from_json(pending["challenge_json"])
                await db.rollback()
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_claim_challenges "
                "(challenge_id, dispatch_id, action, identity_id, state, challenge_json, "
                "issued_at, expires_at, closed_by_receipt_id) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, '')",
                (
                    payload.challenge_id,
                    payload.dispatch_id,
                    payload.action,
                    payload.identity_id,
                    item.model_dump_json(),
                    payload.issued_at,
                    payload.expires_at,
                ),
            )
            await db.commit()
        return item

    async def get_challenge(self, challenge_id: str):
        if not self.db_path.is_file():
            return None, "missing", ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT challenge_json, state, closed_by_receipt_id FROM "
                    "evolution_revalidation_platform_claim_challenges WHERE challenge_id = ?",
                    (challenge_id,),
                )
            ).fetchone()
        if row is None:
            return None, "missing", ""
        return (
            _challenge_from_json(row["challenge_json"]),
            str(row["state"]),
            str(row["closed_by_receipt_id"]),
        )

    async def get_receipt(self, receipt_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_claim_receipts "
                    "WHERE receipt_id = ?",
                    (receipt_id,),
                )
            ).fetchone()
        return None if row is None else _receipt_from_json(row["receipt_json"])

    async def get_latest(self, claim_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_claim_receipts "
                    "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
                    (claim_id,),
                )
            ).fetchone()
        return None if row is None else _receipt_from_json(row["receipt_json"])

    async def get_latest_for_dispatch(self, dispatch_id: str):
        claim_id = _claim_id(dispatch_id)
        return await self.get_latest(claim_id)

    async def record_receipt(
        self,
        receipt: EvolutionRevalidationPlatformClaimReceipt,
    ):
        item = EvolutionRevalidationPlatformClaimReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            challenge_row = await (
                await db.execute(
                    "SELECT challenge_json, state, closed_by_receipt_id FROM "
                    "evolution_revalidation_platform_claim_challenges WHERE challenge_id = ?",
                    (item.challenge_id,),
                )
            ).fetchone()
            dispatch = await _dispatch_row(db, item.dispatch_id)
            identity = await _identity_row(db, item.identity_id)
            if challenge_row is None:
                await db.rollback()
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_challenge_missing", "Platform Claim challenge 不存在。"
                )
            challenge = _challenge_from_json(challenge_row["challenge_json"])
            if not _rows_match_payload(dispatch, identity, challenge.payload):
                await db.rollback()
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_atomic_authority_mismatch",
                    "Platform Claim 持久化时 authority 已变化。",
                )
            if str(challenge_row["state"]) != "pending":
                existing_row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_platform_claim_receipts "
                        "WHERE receipt_id = ?",
                        (str(challenge_row["closed_by_receipt_id"]),),
                    )
                ).fetchone()
                existing = (
                    None
                    if existing_row is None
                    else _receipt_from_json(existing_row["receipt_json"])
                )
                await db.rollback()
                if existing == item:
                    return existing
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_challenge_closed", "Platform Claim challenge 已关闭。"
                )
            latest_row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_claim_receipts "
                    "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
                    (item.claim_id,),
                )
            ).fetchone()
            latest = None if latest_row is None else _receipt_from_json(latest_row["receipt_json"])
            identity_item = _identity_from_json(identity["identity_json"])
            dispatch_item = EvolutionRevalidationPlatformDispatch.model_validate_json(
                dispatch["dispatch_json"]
            )
            expected_lease_expiry = min(
                _aware(item.claimed_at)
                + timedelta(seconds=challenge.payload.lease_seconds),
                _aware(dispatch_item.reservation_expires_at),
            )
            if (
                not _receipt_matches_challenge(item, challenge)
                or _aware(item.lease_expires_at) != expected_lease_expiry
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_receipt_mismatch",
                    "Platform Claim receipt 未绑定 exact challenge。",
                )
            _verify_signature(
                identity_item,
                item.signature_base64,
                challenge.payload.canonical_bytes(),
            )
            if item.sequence == 1:
                valid_chain = latest is None and item.action == "claim"
            else:
                valid_chain = bool(
                    latest is not None
                    and item.action == "renew"
                    and item.sequence == latest.sequence + 1
                    and item.lease_epoch == latest.lease_epoch + 1
                    and item.previous_receipt_sha256 == latest.receipt_sha256
                    and _aware(item.claimed_at) < _aware(latest.lease_expires_at)
                )
            if not valid_chain:
                await db.rollback()
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_sequence_conflict",
                    "Platform Claim lease sequence 已被其他领取者推进。",
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_claim_receipts "
                "(receipt_id, receipt_sha256, claim_id, dispatch_id, sequence, lease_epoch, "
                "identity_id, challenge_id, receipt_json, claimed_at, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.claim_id,
                    item.dispatch_id,
                    item.sequence,
                    item.lease_epoch,
                    item.identity_id,
                    item.challenge_id,
                    item.model_dump_json(),
                    item.claimed_at,
                    item.lease_expires_at,
                ),
            )
            await db.execute(
                "UPDATE evolution_revalidation_platform_claim_challenges "
                "SET state = 'closed', closed_by_receipt_id = ? WHERE challenge_id = ?",
                (item.receipt_id, item.challenge_id),
            )
            await db.commit()
        return item


class EvolutionRevalidationPlatformClaimService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_service: EvolutionRevalidationRuntimeContractService,
        dispatch_store: EvolutionRevalidationPlatformDispatchStore,
        worker_registry: WorkerRegistryStore,
        store: EvolutionRevalidationPlatformClaimStore,
        supervisor_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service = contract_service
        self.dispatch_store = dispatch_store
        self.worker_registry = worker_registry
        self.store = store
        self._supervisor_key_provider = supervisor_key_provider
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def enroll_identity(
        self, identity: EvolutionRevalidationWorkerIdentity
    ) -> EvolutionRevalidationWorkerIdentity:
        item = EvolutionRevalidationWorkerIdentity.model_validate_json(
            identity.model_dump_json()
        )
        self._verify_attestation(item)
        registration = await self.worker_registry.get_active(item.worker_id)
        if not _registration_matches_identity(registration, item):
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_identity_worker_stale",
                "Worker Identity 未绑定 current active Worker incarnation。",
            )
        assert registration is not None
        if _aware(item.enrolled_at) < _aware(registration.registered_at):
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_identity_time_invalid",
                "Worker Identity enrolled_at 早于 Worker registration。",
            )
        return await self.store.record_identity(item)

    async def prepare_claim(
        self,
        *,
        contract_id: str,
        platform: Literal["linux", "macos", "windows"],
        issued_at: str | None = None,
        challenge_ttl_seconds: int = 120,
        lease_seconds: int = 30,
    ) -> EvolutionRevalidationPlatformClaimChallenge:
        dispatch = await self.dispatch_store.get(contract_id, platform)
        if dispatch is None:
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_dispatch_missing", "Required-platform Dispatch 不存在。"
            )
        lock = self._locks.setdefault(f"prepare:{dispatch.dispatch_id}", asyncio.Lock())
        async with lock:
            now = _aware(issued_at or datetime.now(UTC).isoformat())
            pending = await self.store.get_pending(
                dispatch_id=dispatch.dispatch_id,
                action="claim",
                assessed_at=now.isoformat(),
            )
            if pending is not None and _aware(pending.payload.expires_at) > now:
                return pending
            if await self.store.get_latest_for_dispatch(dispatch.dispatch_id) is not None:
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_already_claimed",
                    "Platform Dispatch 已领取；请续租现有 claim。",
                )
            identity, reservation = await self._validate_authority(dispatch, now=now)
            challenge = _build_challenge(
                action="claim",
                dispatch=dispatch,
                identity=identity,
                current=None,
                issued_at=now,
                challenge_ttl_seconds=challenge_ttl_seconds,
                lease_seconds=lease_seconds,
            )
            if _aware(challenge.payload.expires_at) > _aware(reservation.expires_at):
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_window_exceeds_reservation",
                    "Claim challenge 窗口超过 capacity reservation。",
                )
            return await self.store.record_challenge(challenge)

    async def prepare_renewal(
        self,
        *,
        claim_id: str,
        issued_at: str | None = None,
        challenge_ttl_seconds: int = 120,
        lease_seconds: int = 30,
    ) -> EvolutionRevalidationPlatformClaimChallenge:
        lock = self._locks.setdefault(f"prepare-renew:{claim_id}", asyncio.Lock())
        async with lock:
            now = _aware(issued_at or datetime.now(UTC).isoformat())
            view = await self.inspect(claim_id=claim_id, assessed_at=now.isoformat())
            if view.status != "current":
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_not_renewable", "Platform Claim 当前不可续租。"
                )
            current = view.receipt
            pending = await self.store.get_pending(
                dispatch_id=current.dispatch_id,
                action="renew",
                assessed_at=now.isoformat(),
            )
            if pending is not None and _aware(pending.payload.expires_at) > now:
                if pending.payload.previous_receipt_sha256 == current.receipt_sha256:
                    return pending
            dispatch = await self._dispatch_for_receipt(current)
            identity, reservation = await self._validate_authority(dispatch, now=now)
            challenge = _build_challenge(
                action="renew",
                dispatch=dispatch,
                identity=identity,
                current=current,
                issued_at=now,
                challenge_ttl_seconds=challenge_ttl_seconds,
                lease_seconds=lease_seconds,
            )
            if _aware(challenge.payload.expires_at) > _aware(reservation.expires_at):
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_window_exceeds_reservation",
                    "Renew challenge 窗口超过 capacity reservation。",
                )
            return await self.store.record_challenge(challenge)

    async def submit(
        self,
        *,
        challenge_id: str,
        signature_base64: str,
        claimed_at: str | None = None,
    ) -> EvolutionRevalidationPlatformClaimView:
        lock = self._locks.setdefault(f"submit:{challenge_id}", asyncio.Lock())
        async with lock:
            now = _aware(claimed_at or datetime.now(UTC).isoformat())
            challenge, state, closed_by = await self.store.get_challenge(challenge_id)
            if challenge is None:
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_challenge_missing", "Platform Claim challenge 不存在。"
                )
            try:
                normalized_signature = _canonical_b64(
                    signature_base64, expected=64, field="signature"
                )
            except (TypeError, ValueError) as exc:
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_signature_encoding_invalid",
                    "Platform Claim signature 必须是 canonical 64-byte Base64。",
                ) from exc
            if state == "closed":
                existing = await self.store.get_receipt(closed_by)
                if existing is not None and hmac.compare_digest(
                    existing.signature_base64, normalized_signature
                ):
                    return await self.inspect(
                        claim_id=existing.claim_id, assessed_at=now.isoformat()
                    )
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_challenge_closed", "Platform Claim challenge 已关闭。"
                )
            if now < _aware(challenge.payload.issued_at) or now >= _aware(
                challenge.payload.expires_at
            ):
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_challenge_expired", "Platform Claim challenge 已过期。"
                )
            dispatch = await self._dispatch_for_payload(challenge.payload)
            identity, reservation = await self._validate_authority(dispatch, now=now)
            if identity.identity_id != challenge.payload.identity_id:
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_identity_changed", "Worker Identity 已变化。"
                )
            if challenge.payload.action == "renew":
                current = await self.store.get_latest(challenge.payload.claim_id)
                if not (
                    current is not None
                    and current.receipt_sha256
                    == challenge.payload.previous_receipt_sha256
                    and now < _aware(current.lease_expires_at)
                ):
                    raise EvolutionRevalidationPlatformClaimError(
                        "platform_claim_lease_expired",
                        "旧 Platform Claim lease 已过期或被推进，不能复活。",
                    )
            _verify_signature(identity, normalized_signature, challenge.payload.canonical_bytes())
            lease_expires = min(
                now + timedelta(seconds=challenge.payload.lease_seconds),
                _aware(reservation.expires_at),
            )
            if lease_expires <= now:
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_reservation_expired", "Capacity reservation 已过期。"
                )
            receipt = _build_receipt(
                challenge=challenge,
                signature_base64=normalized_signature,
                claimed_at=now,
                lease_expires_at=lease_expires,
            )
            stored = await self.store.record_receipt(receipt)
            view = await self.inspect(claim_id=stored.claim_id, assessed_at=now.isoformat())
            if view.status != "current":
                raise EvolutionRevalidationPlatformClaimError(
                    "platform_claim_fenced_during_commit",
                    "Worker 在 Claim 提交期间被 fencing。",
                )
            return view

    async def inspect(
        self, *, claim_id: str, assessed_at: str | None = None
    ) -> EvolutionRevalidationPlatformClaimView:
        now = _aware(assessed_at or datetime.now(UTC).isoformat())
        receipt = await self.store.get_latest(claim_id)
        if receipt is None:
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_missing", "Platform Claim 不存在。"
            )
        if now >= _aware(receipt.lease_expires_at):
            status: Literal["current", "expired", "stale"] = "expired"
        else:
            try:
                dispatch = await self._dispatch_for_receipt(receipt)
                identity, _reservation = await self._validate_authority(dispatch, now=now)
                status = (
                    "current"
                    if identity.identity_id == receipt.identity_id
                    and identity.identity_sha256 == receipt.identity_sha256
                    else "stale"
                )
            except EvolutionRevalidationPlatformClaimError:
                status = "stale"
        current = status == "current"
        return EvolutionRevalidationPlatformClaimView(
            receipt=receipt,
            status=status,
            lease_active=current,
            worker_claimed=current,
            transport_delivered=current,
        )

    async def _dispatch_for_payload(self, payload):
        dispatch = await self._dispatch_by_id(payload.dispatch_id)
        if not _dispatch_matches_payload(dispatch, payload):
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_dispatch_mismatch", "Platform Claim 未绑定 exact Dispatch。"
            )
        return dispatch

    async def _dispatch_for_receipt(self, receipt):
        dispatch = await self._dispatch_by_id(receipt.dispatch_id)
        if not (
            dispatch.dispatch_sha256 == receipt.dispatch_sha256
            and dispatch.job_id == receipt.job_id
            and dispatch.reservation_id == receipt.reservation_id
        ):
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_dispatch_mismatch", "Platform Claim receipt 的 Dispatch 已变化。"
            )
        return dispatch

    async def _dispatch_by_id(self, dispatch_id: str):
        if not self.dispatch_store.db_path.is_file():
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_dispatch_missing", "Platform Dispatch 不存在。"
            )
        async with aiosqlite.connect(self.dispatch_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT dispatch_json FROM evolution_revalidation_platform_dispatches "
                    "WHERE dispatch_id = ?",
                    (dispatch_id,),
                )
            ).fetchone()
        if row is None:
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_dispatch_missing", "Platform Dispatch 不存在。"
            )
        return EvolutionRevalidationPlatformDispatch.model_validate_json(
            row["dispatch_json"]
        )

    async def _validate_authority(self, dispatch, *, now: datetime):
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=dispatch.contract_id
        )
        registration = await self.worker_registry.get_active(dispatch.worker_id)
        reservation = await self.worker_registry.get_capacity_reservation(
            dispatch.reservation_id, assessed_at=now.isoformat()
        )
        identity = await self.store.get_identity_for_dispatch(dispatch)
        if not (
            view.execution_eligible
            and view.contract.contract_sha256 == dispatch.contract_sha256
            and _registration_matches_dispatch(registration, dispatch)
            and _reservation_matches_dispatch(reservation, dispatch)
            and identity is not None
        ):
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_authority_stale",
                "Platform Claim 的 Contract、Worker、Identity 或 reservation 已失效。",
            )
        self._verify_attestation(identity)
        return identity, reservation

    def _verify_attestation(self, identity):
        key = self._supervisor_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_supervisor_key_invalid",
                "Worker Identity supervisor key 不可用。",
            )
        core = identity.model_dump(
            mode="json",
            exclude={
                "identity_id",
                "identity_sha256",
                "supervisor_attestation_sha256",
            },
        )
        expected = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, identity.supervisor_attestation_sha256):
            raise EvolutionRevalidationPlatformClaimError(
                "platform_claim_identity_attestation_invalid",
                "Worker Identity supervisor attestation 无效。",
            )


def _build_challenge(
    *, action, dispatch, identity, current, issued_at, challenge_ttl_seconds, lease_seconds
):
    if not 5 <= challenge_ttl_seconds <= 300:
        raise ValueError("challenge_ttl_seconds 必须在 5..300。")
    if not 5 <= lease_seconds <= 300:
        raise ValueError("lease_seconds 必须在 5..300。")
    nonce = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    expires = issued_at + timedelta(seconds=challenge_ttl_seconds)
    sequence = 1 if current is None else current.sequence + 1
    claim_id = "" if current is None else current.claim_id
    previous = "" if current is None else current.receipt_sha256
    challenge_id = _challenge_id(
        action=action,
        nonce_base64=nonce,
        dispatch_sha256=dispatch.dispatch_sha256,
        identity_sha256=identity.identity_sha256,
        claim_id=claim_id,
        sequence=sequence,
        previous_receipt_sha256=previous,
        issued_at=issued_at.isoformat(),
        expires_at=expires.isoformat(),
        lease_seconds=lease_seconds,
    )
    payload = EvolutionRevalidationPlatformClaimPayload(
        action=action,
        challenge_id=challenge_id,
        nonce_base64=nonce,
        dispatch_id=dispatch.dispatch_id,
        dispatch_sha256=dispatch.dispatch_sha256,
        job_id=dispatch.job_id,
        reservation_id=dispatch.reservation_id,
        identity_id=identity.identity_id,
        identity_sha256=identity.identity_sha256,
        worker_id=dispatch.worker_id,
        worker_instance_id=dispatch.worker_instance_id,
        worker_epoch=dispatch.worker_epoch,
        claim_id=claim_id,
        sequence=sequence,
        previous_receipt_sha256=previous,
        issued_at=issued_at.isoformat(),
        expires_at=expires.isoformat(),
        lease_seconds=lease_seconds,
    )
    return EvolutionRevalidationPlatformClaimChallenge(
        payload=payload,
        signable_payload_sha256=hashlib.sha256(payload.canonical_bytes()).hexdigest(),
    )


def _build_receipt(*, challenge, signature_base64, claimed_at, lease_expires_at):
    payload = challenge.payload
    claim_id = payload.claim_id or _claim_id(payload.dispatch_id)
    signature = base64.b64decode(signature_base64)
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PLATFORM_CLAIM_POLICY,
        "claim_id": claim_id,
        "sequence": payload.sequence,
        "action": payload.action,
        "challenge_id": payload.challenge_id,
        "dispatch_id": payload.dispatch_id,
        "dispatch_sha256": payload.dispatch_sha256,
        "job_id": payload.job_id,
        "reservation_id": payload.reservation_id,
        "identity_id": payload.identity_id,
        "identity_sha256": payload.identity_sha256,
        "worker_id": payload.worker_id,
        "worker_instance_id": payload.worker_instance_id,
        "worker_epoch": payload.worker_epoch,
        "lease_epoch": payload.sequence,
        "previous_receipt_sha256": payload.previous_receipt_sha256,
        "signature_base64": signature_base64,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "signature_verified": True,
        "claimed_at": claimed_at.isoformat(),
        "lease_expires_at": lease_expires_at.isoformat(),
        "state": "claimed",
        "capacity_reserved": True,
        "worker_claimed": True,
        "transport_delivered": True,
        "execution_started": False,
        "result_received": False,
        "cohort_authority": False,
        "comparison_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationPlatformClaimReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evrevalclaimreceipt_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _verify_signature(identity, signature_base64, payload):
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(identity.public_key_base64)).verify(
            base64.b64decode(signature_base64), payload
        )
    except (InvalidSignature, ValueError) as exc:
        raise EvolutionRevalidationPlatformClaimError(
            "platform_claim_signature_invalid",
            "Ed25519 signature 无效或未绑定 exact Platform Claim challenge。",
        ) from exc


def _registration_matches_identity(registration, identity):
    return bool(
        registration is not None
        and registration.state.value == "active"
        and registration.contract.worker_id == identity.worker_id
        and registration.contract.instance_id == identity.worker_instance_id
        and registration.contract.epoch == identity.worker_epoch
        and registration.contract.contract_sha256 == identity.worker_contract_sha256
    )


def _registration_matches_dispatch(registration, dispatch):
    return bool(
        registration is not None
        and registration.state.value == "active"
        and registration.contract.instance_id == dispatch.worker_instance_id
        and registration.contract.epoch == dispatch.worker_epoch
        and registration.contract.contract_sha256 == dispatch.worker_contract_sha256
    )


def _reservation_matches_dispatch(reservation, dispatch):
    return bool(
        isinstance(reservation, WorkerCapacityReservation)
        and reservation.state is WorkerCapacityReservationState.ACTIVE
        and reservation.worker_id == dispatch.worker_id
        and reservation.instance_id == dispatch.worker_instance_id
        and reservation.epoch == dispatch.worker_epoch
        and reservation.job_id == dispatch.job_id
    )


def _rows_match_payload(dispatch_row, identity_row, payload):
    if dispatch_row is None or identity_row is None:
        return False
    dispatch = EvolutionRevalidationPlatformDispatch.model_validate_json(
        dispatch_row["dispatch_json"]
    )
    identity = _identity_from_json(identity_row["identity_json"])
    return _dispatch_matches_payload(dispatch, payload) and bool(
        identity.identity_id == payload.identity_id
        and identity.identity_sha256 == payload.identity_sha256
        and identity.worker_id == payload.worker_id
        and identity.worker_instance_id == payload.worker_instance_id
        and identity.worker_epoch == payload.worker_epoch
    )


def _dispatch_matches_payload(dispatch, payload):
    return bool(
        dispatch.dispatch_id == payload.dispatch_id
        and dispatch.dispatch_sha256 == payload.dispatch_sha256
        and dispatch.job_id == payload.job_id
        and dispatch.reservation_id == payload.reservation_id
        and dispatch.worker_id == payload.worker_id
        and dispatch.worker_instance_id == payload.worker_instance_id
        and dispatch.worker_epoch == payload.worker_epoch
    )


def _receipt_matches_challenge(receipt, challenge):
    payload = challenge.payload
    return bool(
        receipt.action == payload.action
        and receipt.challenge_id == payload.challenge_id
        and receipt.dispatch_id == payload.dispatch_id
        and receipt.dispatch_sha256 == payload.dispatch_sha256
        and receipt.job_id == payload.job_id
        and receipt.reservation_id == payload.reservation_id
        and receipt.identity_id == payload.identity_id
        and receipt.identity_sha256 == payload.identity_sha256
        and receipt.worker_id == payload.worker_id
        and receipt.worker_instance_id == payload.worker_instance_id
        and receipt.worker_epoch == payload.worker_epoch
        and receipt.sequence == payload.sequence
        and receipt.lease_epoch == payload.sequence
        and receipt.previous_receipt_sha256 == payload.previous_receipt_sha256
        and receipt.claim_id == (payload.claim_id or _claim_id(payload.dispatch_id))
        and _aware(payload.issued_at) <= _aware(receipt.claimed_at)
        and _aware(receipt.claimed_at) < _aware(payload.expires_at)
    )


def _challenge_id(**payload):
    return f"evrevalclaimchallenge_{_digest(payload)[:24]}"


def _claim_id(dispatch_id):
    return f"evrevalplatclaim_{hashlib.sha256(dispatch_id.encode()).hexdigest()[:24]}"


def _canonical_b64(value, *, expected, field):
    raw = _decode_b64(value, expected=expected, field=field)
    normalized = base64.b64encode(raw).decode("ascii")
    if not hmac.compare_digest(value, normalized):
        raise ValueError(f"{field} 必须使用 canonical Base64。")
    return normalized


def _decode_b64(value, *, expected, field):
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} Base64 无效。") from exc
    if len(raw) != expected:
        raise ValueError(f"{field} 长度无效。")
    return raw


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Platform Claim 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _identity_from_json(value):
    return EvolutionRevalidationWorkerIdentity.model_validate_json(value)


def _challenge_from_json(value):
    return EvolutionRevalidationPlatformClaimChallenge.model_validate_json(value)


def _receipt_from_json(value):
    return EvolutionRevalidationPlatformClaimReceipt.model_validate_json(value)


async def _identity_for_incarnation(db, identity):
    return await (
        await db.execute(
            "SELECT identity_json FROM evolution_revalidation_worker_identities "
            "WHERE worker_id = ? AND worker_instance_id = ? AND worker_epoch = ?",
            (identity.worker_id, identity.worker_instance_id, identity.worker_epoch),
        )
    ).fetchone()


async def _identity_row(db, identity_id):
    return await (
        await db.execute(
            "SELECT identity_json FROM evolution_revalidation_worker_identities "
            "WHERE identity_id = ?",
            (identity_id,),
        )
    ).fetchone()


async def _dispatch_row(db, dispatch_id):
    return await (
        await db.execute(
            "SELECT dispatch_json FROM evolution_revalidation_platform_dispatches "
            "WHERE dispatch_id = ?",
            (dispatch_id,),
        )
    ).fetchone()


async def _ensure_schema(db):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_worker_identities (
            identity_id TEXT PRIMARY KEY,
            identity_sha256 TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            worker_instance_id TEXT NOT NULL,
            worker_epoch INTEGER NOT NULL,
            worker_contract_sha256 TEXT NOT NULL,
            public_key_sha256 TEXT NOT NULL,
            identity_json TEXT NOT NULL,
            enrolled_at TEXT NOT NULL,
            UNIQUE(worker_id, worker_instance_id, worker_epoch)
        );
        CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_claim_challenges (
            challenge_id TEXT PRIMARY KEY,
            dispatch_id TEXT NOT NULL,
            action TEXT NOT NULL,
            identity_id TEXT NOT NULL,
            state TEXT NOT NULL,
            challenge_json TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            closed_by_receipt_id TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS evolution_revalidation_pending_claim_challenge
        ON evolution_revalidation_platform_claim_challenges (dispatch_id, action)
        WHERE state = 'pending';
        CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_claim_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL UNIQUE,
            claim_id TEXT NOT NULL,
            dispatch_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            lease_epoch INTEGER NOT NULL,
            identity_id TEXT NOT NULL,
            challenge_id TEXT NOT NULL UNIQUE,
            receipt_json TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            UNIQUE(claim_id, sequence),
            UNIQUE(dispatch_id, sequence)
        );
        """
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PLATFORM_CLAIM_DOMAIN",
    "EVOLUTION_REVALIDATION_PLATFORM_CLAIM_POLICY",
    "EVOLUTION_REVALIDATION_WORKER_IDENTITY_POLICY",
    "EvolutionRevalidationPlatformClaimChallenge",
    "EvolutionRevalidationPlatformClaimError",
    "EvolutionRevalidationPlatformClaimPayload",
    "EvolutionRevalidationPlatformClaimReceipt",
    "EvolutionRevalidationPlatformClaimService",
    "EvolutionRevalidationPlatformClaimStore",
    "EvolutionRevalidationPlatformClaimView",
    "EvolutionRevalidationWorkerIdentity",
    "issue_evolution_revalidation_worker_identity",
]
