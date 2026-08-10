"""Authenticated, one-time Worker claims for post-rollback remote dispatches."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentity,
    AuthenticatedWorkerIdentityAuthority,
    AuthenticatedWorkerIdentityError,
)
from naumi_agent.daemons.worker_contract import WorkerContract
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservation,
    WorkerCapacityReservationState,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)
from naumi_agent.evolution.post_rollback_remote_dispatches import (
    EvolutionPostRollbackRemoteDispatch,
    EvolutionPostRollbackRemoteDispatchError,
    EvolutionPostRollbackRemoteDispatchService,
    EvolutionPostRollbackRemoteDispatchStore,
)

EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_POLICY = (
    "evolution-post-rollback-remote-claim-v1"
)
EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_DOMAIN = (
    "naumi.evolution.post-rollback-remote-claim.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
_MAX_ARTIFACT_BYTES = 256 * 1024
type ClaimAction = Literal["claim", "renew"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRemoteClaimPayload(_StrictModel):
    domain: Literal["naumi.evolution.post-rollback-remote-claim.v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_DOMAIN
    )
    action: ClaimAction
    challenge_id: str = Field(pattern=r"^evpostclaimchallenge_[0-9a-f]{24}$")
    nonce_base64: str = Field(pattern=_B64_32_RE)
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evpostjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evpostreservation_[0-9a-f]{24}$")
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    claim_id: str = Field(pattern=r"^(?:|evpostclaim_[0-9a-f]{24})$")
    sequence: int = Field(ge=1, le=1_000_000)
    previous_receipt_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    lease_seconds: int = Field(ge=1, le=300)

    @model_validator(mode="after")
    def _transition(self) -> Self:
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        if expires <= issued:
            raise ValueError("Remote Claim challenge expiry 无效。")
        if self.action == "claim":
            if self.claim_id or self.sequence != 1 or self.previous_receipt_sha256:
                raise ValueError("首次 Remote Claim payload 状态无效。")
        elif not self.claim_id or self.sequence < 2 or not self.previous_receipt_sha256:
            raise ValueError("Remote Claim renewal payload 状态无效。")
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
            raise ValueError("Remote Claim challenge identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionPostRollbackRemoteClaimChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-claim-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_POLICY
    )
    payload: EvolutionPostRollbackRemoteClaimPayload
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    state: Literal["pending"] = "pending"
    signature_algorithm: Literal["ed25519"] = "ed25519"
    one_time: Literal[True] = True
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        expected = hashlib.sha256(self.payload.canonical_bytes()).hexdigest()
        if not hmac.compare_digest(self.signable_payload_sha256, expected):
            raise ValueError("Remote Claim signable payload 摘要不一致。")
        return self


class EvolutionPostRollbackRemoteClaimReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-claim-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_POLICY
    )
    receipt_id: str = Field(pattern=r"^evpostclaimreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evpostclaim_[0-9a-f]{24}$")
    sequence: int = Field(ge=1, le=1_000_000)
    action: ClaimAction
    challenge_id: str = Field(pattern=r"^evpostclaimchallenge_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evpostjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evpostreservation_[0-9a-f]{24}$")
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    lease_epoch: int = Field(ge=1, le=1_000_000)
    previous_receipt_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    signature_base64: str = Field(pattern=_B64_64_RE)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    signature_verified: Literal[True] = True
    claimed_at: str = Field(min_length=1, max_length=100)
    lease_expires_at: str = Field(min_length=1, max_length=100)
    reservation_deadline: str = Field(min_length=1, max_length=100)
    state: Literal["claimed"] = "claimed"
    capacity_reserved: Literal[True] = True
    worker_claimed: Literal[True] = True
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_canonical_base64(
            self.signature_base64,
            expected_bytes=64,
            field="Remote Claim signature",
        )
        if not hmac.compare_digest(
            self.signature_sha256,
            hashlib.sha256(signature).hexdigest(),
        ):
            raise ValueError("Remote Claim signature 摘要不一致。")
        claimed = _aware(self.claimed_at)
        lease_expires = _aware(self.lease_expires_at)
        reservation_deadline = _aware(self.reservation_deadline)
        if not claimed < lease_expires <= reservation_deadline:
            raise ValueError("Remote Claim lease 未被 reservation deadline 截断。")
        if self.action == "claim":
            if self.sequence != 1 or self.lease_epoch != 1 or self.previous_receipt_sha256:
                raise ValueError("首次 Remote Claim receipt 状态无效。")
        elif (
            self.sequence < 2
            or self.lease_epoch != self.sequence
            or not self.previous_receipt_sha256
        ):
            raise ValueError("Remote Claim renewal receipt 状态无效。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.receipt_sha256, digest)
            and self.receipt_id == f"evpostclaimreceipt_{digest[:24]}"
            and self.claim_id == _claim_id(self.dispatch_id)
        ):
            raise ValueError("Remote Claim receipt identity/digest 不一致。")
        if any(
            (
                self.transport_delivered,
                self.execution_authority,
                self.result_authority,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Remote Claim 不得扩大 transport/execution authority。")
        return self


class EvolutionPostRollbackRemoteClaimView(_StrictModel):
    receipt: EvolutionPostRollbackRemoteClaimReceipt
    status: Literal["current", "expired", "stale"]
    lease_active: bool
    worker_claimed: bool
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = self.status == "current"
        if self.lease_active is not current or self.worker_claimed is not current:
            raise ValueError("Remote Claim view 投影不一致。")
        return self


class EvolutionPostRollbackRemoteClaimError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRemoteClaimStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_pending(
        self,
        *,
        dispatch_id: str,
        action: ClaimAction,
        assessed_at: str,
    ) -> EvolutionPostRollbackRemoteClaimChallenge | None:
        if not self.db_path.is_file():
            return None
        now = _aware(assessed_at).isoformat()
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "UPDATE evolution_post_rollback_remote_claim_challenges "
                    "SET state = 'expired' WHERE dispatch_id = ? AND action = ? "
                    "AND state = 'pending' AND expires_at <= ?",
                    (dispatch_id, action, now),
                )
                row = await (
                    await db.execute(
                        "SELECT challenge_json FROM "
                        "evolution_post_rollback_remote_claim_challenges "
                        "WHERE dispatch_id = ? AND action = ? AND state = 'pending'",
                        (dispatch_id, action),
                    )
                ).fetchone()
                await db.commit()
            return None if row is None else _restore_challenge(row["challenge_json"])
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_store_corrupt",
                "Remote Claim challenge 无法读取。",
            ) from exc

    async def record_challenge(
        self,
        challenge: EvolutionPostRollbackRemoteClaimChallenge,
    ) -> EvolutionPostRollbackRemoteClaimChallenge:
        item = _validate_challenge(challenge)
        payload = item.payload
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                dispatch = await _dispatch_row(db, payload.dispatch_id)
                identity = await _identity_row(db, payload.identity_id)
                if not _rows_match_payload(dispatch, identity, payload):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_authority_mismatch",
                        "Remote Claim challenge 的 Dispatch 或 Worker Identity 已变化。",
                    )
                pending = await (
                    await db.execute(
                        "SELECT challenge_json FROM "
                        "evolution_post_rollback_remote_claim_challenges "
                        "WHERE dispatch_id = ? AND action = ? AND state = 'pending'",
                        (payload.dispatch_id, payload.action),
                    )
                ).fetchone()
                if pending is not None:
                    restored = _restore_challenge(pending["challenge_json"])
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_claim_challenges "
                    "(challenge_id, dispatch_id, action, identity_id, state, "
                    "challenge_json, issued_at, expires_at, closed_by_receipt_id) "
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
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_store_error",
                "Remote Claim challenge 无法持久化。",
            ) from exc
        return item

    async def get_challenge(
        self,
        challenge_id: str,
    ) -> tuple[EvolutionPostRollbackRemoteClaimChallenge | None, str, str]:
        challenge_identity = _challenge_id_value(challenge_id)
        if not self.db_path.is_file():
            return None, "missing", ""
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT challenge_json, state, closed_by_receipt_id FROM "
                        "evolution_post_rollback_remote_claim_challenges "
                        "WHERE challenge_id = ?",
                        (challenge_identity,),
                    )
                ).fetchone()
            if row is None:
                return None, "missing", ""
            return (
                _restore_challenge(row["challenge_json"]),
                str(row["state"]),
                str(row["closed_by_receipt_id"]),
            )
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_store_corrupt",
                "Remote Claim challenge 损坏或无法读取。",
            ) from exc

    async def get_receipt(
        self,
        receipt_id: str,
    ) -> EvolutionPostRollbackRemoteClaimReceipt | None:
        receipt_identity = _receipt_id_value(receipt_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_post_rollback_remote_claim_receipts "
                        "WHERE receipt_id = ?",
                        (receipt_identity,),
                    )
                ).fetchone()
            return None if row is None else _restore_receipt(row["receipt_json"])
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_store_corrupt",
                "Remote Claim receipt 损坏或无法读取。",
            ) from exc

    async def get_latest(
        self,
        claim_id: str,
    ) -> EvolutionPostRollbackRemoteClaimReceipt | None:
        claim_identity = _claim_id_value(claim_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_post_rollback_remote_claim_receipts "
                        "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
                        (claim_identity,),
                    )
                ).fetchone()
            return None if row is None else _restore_receipt(row["receipt_json"])
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_store_corrupt",
                "Remote Claim receipt 损坏或无法读取。",
            ) from exc

    async def get_latest_for_dispatch(
        self,
        dispatch_id: str,
    ) -> EvolutionPostRollbackRemoteClaimReceipt | None:
        return await self.get_latest(_claim_id(dispatch_id))

    async def record_receipt(
        self,
        receipt: EvolutionPostRollbackRemoteClaimReceipt,
    ) -> EvolutionPostRollbackRemoteClaimReceipt:
        item = _validate_receipt(receipt)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                challenge_row = await (
                    await db.execute(
                        "SELECT challenge_json, state, closed_by_receipt_id FROM "
                        "evolution_post_rollback_remote_claim_challenges "
                        "WHERE challenge_id = ?",
                        (item.challenge_id,),
                    )
                ).fetchone()
                dispatch_row = await _dispatch_row(db, item.dispatch_id)
                identity_row = await _identity_row(db, item.identity_id)
                if challenge_row is None:
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_challenge_missing",
                        "Remote Claim challenge 不存在。",
                    )
                challenge = _restore_challenge(challenge_row["challenge_json"])
                if not _rows_match_payload(
                    dispatch_row,
                    identity_row,
                    challenge.payload,
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_atomic_authority_mismatch",
                        "Remote Claim 提交时 Dispatch 或 Identity 已变化。",
                    )
                if str(challenge_row["state"]) != "pending":
                    existing = await self._closed_receipt(
                        db,
                        str(challenge_row["closed_by_receipt_id"]),
                    )
                    await db.rollback()
                    if existing == item:
                        return item
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_challenge_closed",
                        "Remote Claim challenge 已关闭。",
                    )
                latest_row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_post_rollback_remote_claim_receipts "
                        "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
                        (item.claim_id,),
                    )
                ).fetchone()
                latest = (
                    None
                    if latest_row is None
                    else _restore_receipt(latest_row["receipt_json"])
                )
                assert dispatch_row is not None and identity_row is not None
                dispatch = _restore_dispatch(dispatch_row["dispatch_json"])
                identity = _restore_identity(identity_row["identity_json"])
                expected_expiry = min(
                    _aware(item.claimed_at)
                    + timedelta(seconds=challenge.payload.lease_seconds),
                    _aware(dispatch.reservation_expires_at),
                )
                if not (
                    _receipt_matches_challenge(item, challenge)
                    and _aware(item.lease_expires_at) == expected_expiry
                    and item.reservation_deadline == dispatch.reservation_expires_at
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_receipt_mismatch",
                        "Remote Claim receipt 未绑定 exact challenge/deadline。",
                    )
                _verify_signature(
                    identity,
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
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_sequence_conflict",
                        "Remote Claim lease sequence 已被其他请求推进。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_claim_receipts "
                    "(receipt_id, receipt_sha256, claim_id, dispatch_id, sequence, "
                    "lease_epoch, identity_id, challenge_id, receipt_json, claimed_at, "
                    "lease_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    "UPDATE evolution_post_rollback_remote_claim_challenges "
                    "SET state = 'closed', closed_by_receipt_id = ? "
                    "WHERE challenge_id = ? AND state = 'pending'",
                    (item.receipt_id, item.challenge_id),
                )
                await db.commit()
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_store_error",
                "Remote Claim receipt 无法持久化。",
            ) from exc
        return item

    async def _closed_receipt(
        self,
        db: aiosqlite.Connection,
        receipt_id: str,
    ) -> EvolutionPostRollbackRemoteClaimReceipt | None:
        row = await (
            await db.execute(
                "SELECT receipt_json FROM evolution_post_rollback_remote_claim_receipts "
                "WHERE receipt_id = ?",
                (receipt_id,),
            )
        ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])


class EvolutionPostRollbackRemoteClaimService:
    def __init__(
        self,
        *,
        dispatch_service: EvolutionPostRollbackRemoteDispatchService,
        dispatch_store: EvolutionPostRollbackRemoteDispatchStore,
        worker_registry: WorkerRegistryStore,
        identity_authority: AuthenticatedWorkerIdentityAuthority,
        store: EvolutionPostRollbackRemoteClaimStore,
    ) -> None:
        if not (
            dispatch_store.db_path == store.db_path
            and identity_authority.store.db_path == store.db_path
        ):
            raise ValueError(
                "Remote Claim 的 Dispatch、Identity 与 Claim store "
                "必须共享同一 SQLite authority。"
            )
        self.dispatch_service = dispatch_service
        self.dispatch_store = dispatch_store
        self.worker_registry = worker_registry
        self.identity_authority = identity_authority
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare_claim(
        self,
        *,
        dispatch_id: str,
        issued_at: str | None = None,
        challenge_ttl_seconds: int = 5,
        lease_seconds: int = 5,
    ) -> EvolutionPostRollbackRemoteClaimChallenge:
        dispatch = await self._dispatch_by_id(dispatch_id)
        lock = self._locks.setdefault(f"prepare:{dispatch.dispatch_id}", asyncio.Lock())
        async with lock:
            now = _aware(issued_at or datetime.now(UTC).isoformat())
            pending = await self.store.get_pending(
                dispatch_id=dispatch.dispatch_id,
                action="claim",
                assessed_at=now.isoformat(),
            )
            if pending is not None and _aware(pending.payload.expires_at) > now:
                identity, reservation = await self._validate_authority(
                    dispatch,
                    now=now,
                )
                if not (
                    pending.payload.identity_id == identity.identity_id
                    and pending.payload.identity_sha256 == identity.identity_sha256
                    and _aware(pending.payload.expires_at)
                    <= _aware(reservation.expires_at)
                ):
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_pending_stale",
                        "Pending Remote Claim challenge authority 已变化。",
                    )
                return pending
            if await self.store.get_latest_for_dispatch(dispatch.dispatch_id) is not None:
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_already_claimed",
                    "Remote Dispatch 已领取；请续租现有 claim。",
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
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_window_exceeds_reservation",
                    "Remote Claim challenge 窗口超过 capacity reservation。",
                )
            return await self.store.record_challenge(challenge)

    async def prepare_renewal(
        self,
        *,
        claim_id: str,
        issued_at: str | None = None,
        challenge_ttl_seconds: int = 2,
        lease_seconds: int = 5,
    ) -> EvolutionPostRollbackRemoteClaimChallenge:
        lock = self._locks.setdefault(f"renew:{claim_id}", asyncio.Lock())
        async with lock:
            now = _aware(issued_at or datetime.now(UTC).isoformat())
            view = await self.inspect(claim_id=claim_id, assessed_at=now.isoformat())
            if view.status != "current":
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_not_renewable",
                    "Remote Claim 当前不可续租。",
                )
            current = view.receipt
            pending = await self.store.get_pending(
                dispatch_id=current.dispatch_id,
                action="renew",
                assessed_at=now.isoformat(),
            )
            if pending is not None and (
                _aware(pending.payload.expires_at) > now
                and pending.payload.previous_receipt_sha256 == current.receipt_sha256
            ):
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
            deadline = min(
                _aware(current.lease_expires_at),
                _aware(reservation.expires_at),
            )
            if _aware(challenge.payload.expires_at) > deadline:
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_window_exceeds_lease",
                    "Renew challenge 窗口超过 current claim/reservation deadline。",
                )
            return await self.store.record_challenge(challenge)

    async def submit(
        self,
        *,
        challenge_id: str,
        signature_base64: str,
        claimed_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteClaimView:
        lock = self._locks.setdefault(f"submit:{challenge_id}", asyncio.Lock())
        async with lock:
            now = _aware(claimed_at or datetime.now(UTC).isoformat())
            challenge, state, closed_by = await self.store.get_challenge(challenge_id)
            if challenge is None:
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_challenge_missing",
                    "Remote Claim challenge 不存在。",
                )
            try:
                normalized_signature = _canonical_base64(
                    signature_base64,
                    expected_bytes=64,
                    field="Remote Claim signature",
                )
            except (TypeError, ValueError) as exc:
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_signature_encoding_invalid",
                    "Remote Claim signature 必须是 canonical 64-byte Base64。",
                ) from exc
            if state == "closed":
                existing = await self.store.get_receipt(closed_by)
                if existing is not None and hmac.compare_digest(
                    existing.signature_base64,
                    normalized_signature,
                ):
                    return await self.inspect(
                        claim_id=existing.claim_id,
                        assessed_at=now.isoformat(),
                    )
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_challenge_closed",
                    "Remote Claim challenge 已关闭。",
                )
            if not _aware(challenge.payload.issued_at) <= now < _aware(
                challenge.payload.expires_at
            ):
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_challenge_expired",
                    "Remote Claim challenge 尚未生效或已经过期。",
                )
            dispatch = await self._dispatch_for_payload(challenge.payload)
            identity, reservation = await self._validate_authority(dispatch, now=now)
            if not (
                identity.identity_id == challenge.payload.identity_id
                and identity.identity_sha256 == challenge.payload.identity_sha256
            ):
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_identity_changed",
                    "Worker Identity 在 challenge 期间变化。",
                )
            if challenge.payload.action == "renew":
                current = await self.store.get_latest(challenge.payload.claim_id)
                if not (
                    current is not None
                    and current.receipt_sha256
                    == challenge.payload.previous_receipt_sha256
                    and now < _aware(current.lease_expires_at)
                ):
                    raise EvolutionPostRollbackRemoteClaimError(
                        "post_rollback_remote_claim_lease_expired",
                        "旧 Remote Claim lease 已过期或被推进，不能复活。",
                    )
            _verify_signature(
                identity,
                normalized_signature,
                challenge.payload.canonical_bytes(),
            )
            lease_expires = min(
                now + timedelta(seconds=challenge.payload.lease_seconds),
                _aware(reservation.expires_at),
            )
            if lease_expires <= now:
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_reservation_expired",
                    "Capacity reservation 已过期。",
                )
            receipt = _build_receipt(
                challenge=challenge,
                signature_base64=normalized_signature,
                claimed_at=now,
                lease_expires_at=lease_expires,
                reservation_deadline=dispatch.reservation_expires_at,
            )
            stored = await self.store.record_receipt(receipt)
            view = await self.inspect(
                claim_id=stored.claim_id,
                assessed_at=now.isoformat(),
            )
            if view.status != "current":
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_fenced_during_commit",
                    "Worker 在 Remote Claim 提交期间被 fencing。",
                )
            return view

    async def inspect(
        self,
        *,
        claim_id: str,
        assessed_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteClaimView:
        now = _aware(assessed_at or datetime.now(UTC).isoformat())
        receipt = await self.store.get_latest(claim_id)
        if receipt is None:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_missing",
                "Remote Claim 不存在。",
            )
        if now >= _aware(receipt.lease_expires_at):
            status: Literal["current", "expired", "stale"] = "expired"
        else:
            try:
                dispatch = await self._dispatch_for_receipt(receipt)
                identity, _ = await self._validate_authority(dispatch, now=now)
                status = (
                    "current"
                    if identity.identity_id == receipt.identity_id
                    and identity.identity_sha256 == receipt.identity_sha256
                    else "stale"
                )
            except (
                AuthenticatedWorkerIdentityError,
                EvolutionPostRollbackRemoteClaimError,
                EvolutionPostRollbackRemoteDispatchError,
                WorkerRegistryStoreError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                status = "stale"
        current = status == "current"
        return EvolutionPostRollbackRemoteClaimView(
            receipt=receipt,
            status=status,
            lease_active=current,
            worker_claimed=current,
        )

    async def _dispatch_by_id(
        self,
        dispatch_id: str,
    ) -> EvolutionPostRollbackRemoteDispatch:
        dispatch_identity = _dispatch_id_value(dispatch_id)
        if not self.dispatch_store.db_path.is_file():
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_dispatch_missing",
                "Remote Dispatch 不存在。",
            )
        try:
            async with aiosqlite.connect(self.dispatch_store.db_path) as db:
                db.row_factory = aiosqlite.Row
                row = await (
                    await db.execute(
                        "SELECT dispatch_json FROM evolution_post_rollback_remote_dispatches "
                        "WHERE dispatch_id = ?",
                        (dispatch_identity,),
                    )
                ).fetchone()
            if row is None:
                raise EvolutionPostRollbackRemoteClaimError(
                    "post_rollback_remote_claim_dispatch_missing",
                    "Remote Dispatch 不存在。",
                )
            return _restore_dispatch(row["dispatch_json"])
        except EvolutionPostRollbackRemoteClaimError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_dispatch_corrupt",
                "Remote Dispatch 损坏或无法读取。",
            ) from exc

    async def _dispatch_for_payload(
        self,
        payload: EvolutionPostRollbackRemoteClaimPayload,
    ) -> EvolutionPostRollbackRemoteDispatch:
        dispatch = await self._dispatch_by_id(payload.dispatch_id)
        if not _dispatch_matches_payload(dispatch, payload):
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_dispatch_mismatch",
                "Remote Claim 未绑定 exact Dispatch。",
            )
        return dispatch

    async def _dispatch_for_receipt(
        self,
        receipt: EvolutionPostRollbackRemoteClaimReceipt,
    ) -> EvolutionPostRollbackRemoteDispatch:
        dispatch = await self._dispatch_by_id(receipt.dispatch_id)
        if not (
            dispatch.dispatch_sha256 == receipt.dispatch_sha256
            and dispatch.job_id == receipt.job_id
            and dispatch.reservation_id == receipt.reservation_id
            and dispatch.reservation_expires_at == receipt.reservation_deadline
        ):
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_dispatch_mismatch",
                "Remote Claim receipt 的 Dispatch 已变化。",
            )
        return dispatch

    async def _validate_authority(
        self,
        dispatch: EvolutionPostRollbackRemoteDispatch,
        *,
        now: datetime,
    ) -> tuple[AuthenticatedWorkerIdentity, WorkerCapacityReservation]:
        dispatch_view = await self.dispatch_service.inspect(
            dispatch=dispatch,
            assessed_at=now.isoformat(),
        )
        registration = await self.worker_registry.get_active(dispatch.worker_id)
        reservation = await self.worker_registry.get_capacity_reservation(
            dispatch.reservation_id,
            assessed_at=now.isoformat(),
        )
        identity_view = (
            None
            if registration is None
            else await self.identity_authority.resolve_for_contract(
                registration.contract
            )
        )
        if not (
            dispatch_view.dispatch_authority
            and registration is not None
            and _registration_matches_dispatch(registration.contract, dispatch)
            and _reservation_matches_dispatch(reservation, dispatch)
            and identity_view is not None
            and identity_view.identity_authority
        ):
            raise EvolutionPostRollbackRemoteClaimError(
                "post_rollback_remote_claim_authority_stale",
                "Remote Claim 的 Dispatch、Worker、Identity 或 reservation 已失效。",
            )
        assert reservation is not None
        return identity_view.identity, reservation


def render_post_rollback_remote_claim(
    view: EvolutionPostRollbackRemoteClaimView,
) -> str:
    item = view.receipt
    return "\n".join(
        (
            "## 回滚后远端 Claim",
            "",
            f"- Claim：`{item.claim_id}` · lease epoch {item.lease_epoch}",
            f"- Receipt：`{item.receipt_id}`",
            f"- Dispatch / Job：`{item.dispatch_id}` / `{item.job_id}`",
            f"- Worker：`{item.worker_id}` · epoch {item.worker_epoch}",
            f"- Identity：`{item.identity_id}`",
            f"- Lease expiry：`{item.lease_expires_at}`",
            f"- 状态：{view.status}",
            "",
            "Worker 已通过 one-time Ed25519 challenge 领取短 lease；尚未传输 baseline，"
            "也不授予执行、结果、学习或推广权限。",
        )
    )


def render_post_rollback_remote_claim_challenge(
    challenge: EvolutionPostRollbackRemoteClaimChallenge,
) -> str:
    item = challenge.payload
    return "\n".join(
        (
            "## 回滚后远端 Claim Challenge",
            "",
            f"- Challenge：`{item.challenge_id}` · {item.action}",
            f"- Dispatch / Job：`{item.dispatch_id}` / `{item.job_id}`",
            f"- Worker：`{item.worker_id}` · epoch {item.worker_epoch}",
            f"- Identity：`{item.identity_id}`",
            f"- Sequence：{item.sequence}",
            f"- Challenge expiry：`{item.expires_at}`",
            f"- Requested lease：{item.lease_seconds}s",
            f"- Signable SHA-256：`{challenge.signable_payload_sha256}`",
            "",
            "Worker 必须用 exact incarnation 私钥签署 canonical payload；challenge 一次性，"
            "尚未取得 claim、传输或执行权。",
            "",
            "```json",
            item.model_dump_json(),
            "```",
        )
    )


def _build_challenge(
    *,
    action: ClaimAction,
    dispatch: EvolutionPostRollbackRemoteDispatch,
    identity: AuthenticatedWorkerIdentity,
    current: EvolutionPostRollbackRemoteClaimReceipt | None,
    issued_at: datetime,
    challenge_ttl_seconds: int,
    lease_seconds: int,
) -> EvolutionPostRollbackRemoteClaimChallenge:
    if isinstance(challenge_ttl_seconds, bool) or not isinstance(
        challenge_ttl_seconds, int
    ):
        raise TypeError("challenge_ttl_seconds 必须是整数。")
    if not 1 <= challenge_ttl_seconds <= 120:
        raise ValueError("challenge_ttl_seconds 必须在 1..120。")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
        raise TypeError("lease_seconds 必须是整数。")
    if not 1 <= lease_seconds <= 300:
        raise ValueError("lease_seconds 必须在 1..300。")
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
    payload = EvolutionPostRollbackRemoteClaimPayload(
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
    return EvolutionPostRollbackRemoteClaimChallenge(
        payload=payload,
        signable_payload_sha256=hashlib.sha256(payload.canonical_bytes()).hexdigest(),
    )


def _build_receipt(
    *,
    challenge: EvolutionPostRollbackRemoteClaimChallenge,
    signature_base64: str,
    claimed_at: datetime,
    lease_expires_at: datetime,
    reservation_deadline: str,
) -> EvolutionPostRollbackRemoteClaimReceipt:
    payload = challenge.payload
    signature = base64.b64decode(signature_base64)
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_POLICY,
        "claim_id": payload.claim_id or _claim_id(payload.dispatch_id),
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
        "reservation_deadline": reservation_deadline,
        "state": "claimed",
        "capacity_reserved": True,
        "worker_claimed": True,
        "transport_delivered": False,
        "execution_authority": False,
        "result_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionPostRollbackRemoteClaimReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evpostclaimreceipt_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _verify_signature(
    identity: AuthenticatedWorkerIdentity,
    signature_base64: str,
    payload: bytes,
) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(
            base64.b64decode(identity.public_key_base64)
        ).verify(base64.b64decode(signature_base64), payload)
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_signature_invalid",
            "Ed25519 signature 无效或未绑定 exact Remote Claim challenge。",
        ) from exc


def _registration_matches_dispatch(
    contract: WorkerContract,
    dispatch: EvolutionPostRollbackRemoteDispatch,
) -> bool:
    return bool(
        contract.worker_id == dispatch.worker_id
        and contract.instance_id == dispatch.worker_instance_id
        and contract.epoch == dispatch.worker_epoch
        and contract.contract_sha256 == dispatch.worker_contract_sha256
    )


def _reservation_matches_dispatch(
    reservation: WorkerCapacityReservation | None,
    dispatch: EvolutionPostRollbackRemoteDispatch,
) -> bool:
    return bool(
        reservation is not None
        and reservation.state is WorkerCapacityReservationState.ACTIVE
        and reservation.worker_id == dispatch.worker_id
        and reservation.instance_id == dispatch.worker_instance_id
        and reservation.epoch == dispatch.worker_epoch
        and reservation.job_id == dispatch.job_id
        and reservation.reservation_id == dispatch.reservation_id
        and _aware(reservation.expires_at) >= _aware(dispatch.reservation_expires_at)
    )


def _rows_match_payload(dispatch_row, identity_row, payload) -> bool:
    if dispatch_row is None or identity_row is None:
        return False
    dispatch = _restore_dispatch(dispatch_row["dispatch_json"])
    identity = _restore_identity(identity_row["identity_json"])
    return _dispatch_matches_payload(dispatch, payload) and bool(
        identity.identity_id == payload.identity_id
        and identity.identity_sha256 == payload.identity_sha256
        and identity.worker_id == payload.worker_id
        and identity.worker_instance_id == payload.worker_instance_id
        and identity.worker_epoch == payload.worker_epoch
        and identity.worker_contract_sha256 == dispatch.worker_contract_sha256
    )


def _dispatch_matches_payload(dispatch, payload) -> bool:
    return bool(
        dispatch.dispatch_id == payload.dispatch_id
        and dispatch.dispatch_sha256 == payload.dispatch_sha256
        and dispatch.job_id == payload.job_id
        and dispatch.reservation_id == payload.reservation_id
        and dispatch.worker_id == payload.worker_id
        and dispatch.worker_instance_id == payload.worker_instance_id
        and dispatch.worker_epoch == payload.worker_epoch
    )


def _receipt_matches_challenge(receipt, challenge) -> bool:
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


async def _dispatch_row(db, dispatch_id):
    return await (
        await db.execute(
            "SELECT dispatch_json FROM evolution_post_rollback_remote_dispatches "
            "WHERE dispatch_id = ?",
            (dispatch_id,),
        )
    ).fetchone()


async def _identity_row(db, identity_id):
    return await (
        await db.execute(
            "SELECT identity_json FROM authenticated_worker_identities "
            "WHERE identity_id = ?",
            (identity_id,),
        )
    ).fetchone()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_claim_challenges (
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
        CREATE UNIQUE INDEX IF NOT EXISTS evolution_post_rollback_pending_claim
        ON evolution_post_rollback_remote_claim_challenges (dispatch_id, action)
        WHERE state = 'pending';
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_claim_receipts (
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


def _validate_challenge(
    value: EvolutionPostRollbackRemoteClaimChallenge,
) -> EvolutionPostRollbackRemoteClaimChallenge:
    try:
        item = EvolutionPostRollbackRemoteClaimChallenge.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_challenge_invalid",
            "Remote Claim challenge 无效。",
        ) from exc
    if len(item.model_dump_json().encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_challenge_oversized",
            "Remote Claim challenge 超过 256 KiB。",
        )
    return item


def _validate_receipt(
    value: EvolutionPostRollbackRemoteClaimReceipt,
) -> EvolutionPostRollbackRemoteClaimReceipt:
    try:
        item = EvolutionPostRollbackRemoteClaimReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_receipt_invalid",
            "Remote Claim receipt 无效。",
        ) from exc
    if len(item.model_dump_json().encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_receipt_oversized",
            "Remote Claim receipt 超过 256 KiB。",
        )
    return item


def _restore_dispatch(encoded: str) -> EvolutionPostRollbackRemoteDispatch:
    return EvolutionPostRollbackRemoteDispatch.model_validate_json(encoded)


def _restore_identity(encoded: str) -> AuthenticatedWorkerIdentity:
    return AuthenticatedWorkerIdentity.model_validate_json(encoded)


def _restore_challenge(encoded: str) -> EvolutionPostRollbackRemoteClaimChallenge:
    try:
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise ValueError("oversized")
        return EvolutionPostRollbackRemoteClaimChallenge.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_store_corrupt",
            "Remote Claim challenge 无法验证。",
        ) from exc


def _restore_receipt(encoded: str) -> EvolutionPostRollbackRemoteClaimReceipt:
    try:
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise ValueError("oversized")
        return EvolutionPostRollbackRemoteClaimReceipt.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_store_corrupt",
            "Remote Claim receipt 无法验证。",
        ) from exc


def _challenge_id(**payload) -> str:
    return f"evpostclaimchallenge_{_digest(payload)[:24]}"


def _claim_id(dispatch_id: str) -> str:
    return f"evpostclaim_{hashlib.sha256(dispatch_id.encode()).hexdigest()[:24]}"


def _dispatch_id_value(value: str) -> str:
    return _bounded_id(
        value,
        pattern=r"evpostdispatch_[0-9a-f]{24}",
        field="Remote Dispatch ID",
    )


def _challenge_id_value(value: str) -> str:
    return _bounded_id(
        value,
        pattern=r"evpostclaimchallenge_[0-9a-f]{24}",
        field="Remote Claim Challenge ID",
    )


def _claim_id_value(value: str) -> str:
    return _bounded_id(
        value,
        pattern=r"evpostclaim_[0-9a-f]{24}",
        field="Remote Claim ID",
    )


def _receipt_id_value(value: str) -> str:
    return _bounded_id(
        value,
        pattern=r"evpostclaimreceipt_[0-9a-f]{24}",
        field="Remote Claim Receipt ID",
    )


def _bounded_id(value: str, *, pattern: str, field: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(pattern, normalized) is None:
        raise EvolutionPostRollbackRemoteClaimError(
            "post_rollback_remote_claim_id_invalid",
            f"{field} 格式无效。",
        )
    return normalized


def _canonical_base64(value: str, *, expected_bytes: int, field: str) -> str:
    raw = _decode_canonical_base64(
        value,
        expected_bytes=expected_bytes,
        field=field,
    )
    return base64.b64encode(raw).decode("ascii")


def _decode_canonical_base64(
    value: str,
    *,
    expected_bytes: int,
    field: str,
) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} Base64 无效。") from exc
    if len(raw) != expected_bytes:
        raise ValueError(f"{field} 长度无效。")
    if not hmac.compare_digest(value, base64.b64encode(raw).decode("ascii")):
        raise ValueError(f"{field} 必须使用 canonical Base64。")
    return raw


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Remote Claim 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_DOMAIN",
    "EVOLUTION_POST_ROLLBACK_REMOTE_CLAIM_POLICY",
    "EvolutionPostRollbackRemoteClaimChallenge",
    "EvolutionPostRollbackRemoteClaimError",
    "EvolutionPostRollbackRemoteClaimPayload",
    "EvolutionPostRollbackRemoteClaimReceipt",
    "EvolutionPostRollbackRemoteClaimService",
    "EvolutionPostRollbackRemoteClaimStore",
    "EvolutionPostRollbackRemoteClaimView",
    "render_post_rollback_remote_claim",
    "render_post_rollback_remote_claim_challenge",
]
