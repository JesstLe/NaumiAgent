"""Claim-bound start authorization for post-rollback remote evaluations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
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
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionReceipt,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantContract,
    RunDelegationGrantError,
    RunDelegationGrantRequest,
    verify_run_delegation_grant,
)
from naumi_agent.daemons.worker_contract import WorkerContract
from naumi_agent.evolution.post_rollback_remote_claims import (
    EvolutionPostRollbackRemoteClaimError,
    EvolutionPostRollbackRemoteClaimReceipt,
    EvolutionPostRollbackRemoteClaimService,
    EvolutionPostRollbackRemoteClaimStore,
)
from naumi_agent.evolution.post_rollback_remote_deliveries import (
    EvolutionPostRollbackRemoteDeliveryError,
    EvolutionPostRollbackRemoteDeliveryOffer,
    EvolutionPostRollbackRemoteDeliveryReceipt,
    EvolutionPostRollbackRemoteDeliveryService,
    EvolutionPostRollbackRemoteDeliveryStore,
)
from naumi_agent.evolution.post_rollback_remote_dispatches import (
    EvolutionPostRollbackRemoteDispatch,
    EvolutionPostRollbackRemoteDispatchError,
    EvolutionPostRollbackRemoteDispatchService,
    EvolutionPostRollbackRemoteDispatchStore,
)
from naumi_agent.harness.run_lease import (
    HarnessRunKind,
    HarnessRunLease,
    HarnessRunLeaseState,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.release.runtime_eval import (
    MAX_RUNTIME_EVAL_OUTPUT_BYTES,
    ReleaseRuntimeEvalRequest,
)

EVOLUTION_POST_ROLLBACK_REMOTE_EXECUTION_POLICY = (
    "evolution-post-rollback-remote-execution-authorization-v1"
)
EVOLUTION_POST_ROLLBACK_REMOTE_START_DOMAIN = (
    "naumi.evolution.post-rollback-remote-start.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
_MAX_CHALLENGE_BYTES = 768 * 1024
_MAX_AUTHORIZATION_BYTES = 896 * 1024
type ExecutionStatus = Literal["awaiting_start", "current", "expired", "stale"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRemoteStartPayload(_StrictModel):
    """Exact Worker-signable request; it does not itself authorize execution."""

    schema_version: Literal[1] = 1
    domain: Literal["naumi.evolution.post-rollback-remote-start.v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_START_DOMAIN
    )
    attempt_id: str = Field(pattern=r"^evpostattempt_[0-9a-f]{24}$")
    attempt_number: int = Field(ge=1, le=10_000)
    delivery_id: str = Field(pattern=r"^evpostdelivery_[0-9a-f]{24}$")
    delivery_receipt_id: str = Field(
        pattern=r"^evpostdeliveryreceipt_[0-9a-f]{24}$"
    )
    delivery_receipt_sha256: str = Field(pattern=_SHA256_RE)
    delivery_offer_sha256: str = Field(pattern=_SHA256_RE)
    delivery_expires_at: str = Field(min_length=1, max_length=100)
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evpostclaim_[0-9a-f]{24}$")
    claim_receipt_id: str = Field(pattern=r"^evpostclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_lease_epoch: int = Field(ge=1, le=1_000_000)
    claim_lease_expires_at: str = Field(min_length=1, max_length=100)
    reservation_expires_at: str = Field(min_length=1, max_length=100)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    archive_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    suite_sha256: str = Field(pattern=_SHA256_RE)
    repetitions: int = Field(ge=5, le=100)
    runtime_eval_request: ReleaseRuntimeEvalRequest
    case_execution_budget_ms: int = Field(ge=1, le=500_000)
    total_execution_budget_ms: int = Field(ge=5, le=50_000_000)
    max_memory_bytes: int = Field(ge=16 * 1024 * 1024, le=16 * 1024**4)
    max_cpu_seconds: int = Field(ge=1, le=7 * 24 * 60 * 60)
    max_wall_seconds: int = Field(ge=1, le=3_600)
    max_case_output_bytes: int = Field(ge=1_024, le=MAX_RUNTIME_EVAL_OUTPUT_BYTES)
    max_result_bytes: int = Field(ge=1_024, le=100 * MAX_RUNTIME_EVAL_OUTPUT_BYTES)
    allowed_tool_names: tuple[Literal["bash_run"], ...] = ("bash_run",)
    ephemeral_workspace: Literal[True] = True
    network_default_deny: Literal[True] = True
    environment_allowlist: Literal[True] = True
    resource_limits_enforced: Literal[True] = True
    process_tree_cancel: Literal[True] = True
    artifact_digest: Literal[True] = True
    parent_permission_receipt_id: str = Field(min_length=1, max_length=128)
    parent_permission_receipt_sha256: str = Field(pattern=_SHA256_RE)
    start_nonce_base64: str = Field(pattern=_B64_32_RE)
    issued_at: str = Field(min_length=1, max_length=100)
    start_deadline: str = Field(min_length=1, max_length=100)
    installation_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _decode_base64(self.start_nonce_base64, expected=32, field="Start nonce")
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Remote Start workspace 必须 canonical。")
        issued = _aware(self.issued_at)
        deadline = _aware(self.start_deadline)
        claim_deadline = _aware(self.claim_lease_expires_at)
        reservation_deadline = _aware(self.reservation_expires_at)
        delivery_deadline = _aware(self.delivery_expires_at)
        request = self.runtime_eval_request
        case_budget = sum(item.max_duration_ms for item in request.cases)
        expected_wall = max(1, math.ceil(self.total_execution_budget_ms / 1_000))
        if not (
            issued
            < deadline
            <= min(claim_deadline, reservation_deadline, delivery_deadline)
            and self.suite_id == request.suite_id
            and self.suite_sha256 == request.suite_sha256
            and self.case_execution_budget_ms == case_budget
            and self.total_execution_budget_ms == case_budget * self.repetitions
            and self.max_wall_seconds == expected_wall
            and self.max_cpu_seconds == expected_wall
            and self.max_case_output_bytes == MAX_RUNTIME_EVAL_OUTPUT_BYTES
            and self.max_result_bytes
            == MAX_RUNTIME_EVAL_OUTPUT_BYTES * self.repetitions
            and self.allowed_tool_names == ("bash_run",)
        ):
            raise ValueError("Remote Start suite/resource/time projection 不一致。")
        if self.attempt_id != _attempt_id(
            delivery_receipt_sha256=self.delivery_receipt_sha256,
            dispatch_sha256=self.dispatch_sha256,
            attempt_number=self.attempt_number,
            suite_sha256=self.suite_sha256,
        ):
            raise ValueError("Remote Start attempt identity 不一致。")
        if self.installation_authority or self.execution_authority or self.result_authority:
            raise ValueError("Remote Start challenge 不得提前授予执行权。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionPostRollbackRemoteStartChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-post-rollback-remote-execution-authorization-v1"
    ] = EVOLUTION_POST_ROLLBACK_REMOTE_EXECUTION_POLICY
    challenge_id: str = Field(pattern=r"^evpoststart_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=_SHA256_RE)
    attempt_id: str = Field(pattern=r"^evpostattempt_[0-9a-f]{24}$")
    payload: EvolutionPostRollbackRemoteStartPayload
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    state: Literal["awaiting_worker_signature"] = "awaiting_worker_signature"
    installation_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not (
            self.attempt_id == self.payload.attempt_id
            and hmac.compare_digest(
                self.signable_payload_sha256,
                hashlib.sha256(self.payload.canonical_bytes()).hexdigest(),
            )
        ):
            raise ValueError("Remote Start challenge payload binding 不一致。")
        core = self.model_dump(
            mode="json", exclude={"challenge_id", "challenge_sha256"}
        )
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.challenge_sha256, digest)
            and self.challenge_id == f"evpoststart_{digest[:24]}"
        ):
            raise ValueError("Remote Start challenge identity 不一致。")
        return self


class EvolutionPostRollbackRemoteRunGrantEnvelope(_StrictModel):
    schema_version: Literal[1] = 1
    grant_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    parent_receipt_id: str = Field(min_length=1, max_length=128)
    parent_receipt_sha256: str = Field(pattern=_SHA256_RE)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    workspace_sha256: str = Field(pattern=_SHA256_RE)
    run_kind: Literal["runtime"] = "runtime"
    lease_owner_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)
    delegated_tool_names: tuple[Literal["bash_run"], ...] = ("bash_run",)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    request_sha256: str = Field(pattern=_SHA256_RE)
    grant_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.delegated_tool_names != ("bash_run",):
            raise ValueError("Remote execution Run Grant 只能委托 bash_run。")
        if _aware(self.expires_at) <= _aware(self.issued_at):
            raise ValueError("Remote execution Run Grant expiry 无效。")
        if not verify_run_delegation_grant(self.as_contract()):
            raise ValueError("Remote execution Run Grant digest 无效。")
        return self

    def as_contract(self) -> RunDelegationGrantContract:
        return RunDelegationGrantContract(
            schema_version=self.schema_version,
            grant_id=self.grant_id,
            idempotency_key=self.idempotency_key,
            parent_receipt_id=self.parent_receipt_id,
            parent_receipt_sha256=self.parent_receipt_sha256,
            session_id=self.session_id,
            run_id=self.run_id,
            workspace_sha256=self.workspace_sha256,
            run_kind=HarnessRunKind(self.run_kind),
            lease_owner_id=self.lease_owner_id,
            lease_epoch=self.lease_epoch,
            delegated_tool_names=self.delegated_tool_names,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            request_sha256=self.request_sha256,
            grant_sha256=self.grant_sha256,
        )

    @classmethod
    def from_contract(cls, contract: RunDelegationGrantContract) -> Self:
        return cls.model_validate(
            {
                **asdict(contract),
                "run_kind": contract.run_kind.value,
                "delegated_tool_names": list(contract.delegated_tool_names),
            }
        )


class EvolutionPostRollbackRemoteExecutionAuthorization(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-post-rollback-remote-execution-authorization-v1"
    ] = EVOLUTION_POST_ROLLBACK_REMOTE_EXECUTION_POLICY
    authorization_id: str = Field(pattern=r"^evpostexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    attempt_id: str = Field(pattern=r"^evpostattempt_[0-9a-f]{24}$")
    challenge_id: str = Field(pattern=r"^evpoststart_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=_SHA256_RE)
    start_payload: EvolutionPostRollbackRemoteStartPayload
    worker_signature_base64: str = Field(pattern=_B64_64_RE, repr=False)
    worker_signature_sha256: str = Field(pattern=_SHA256_RE)
    worker_signature_verified: Literal[True] = True
    run_grant: EvolutionPostRollbackRemoteRunGrantEnvelope
    runtime_lease_owner_id: str = Field(min_length=1, max_length=128)
    runtime_lease_epoch: int = Field(ge=1)
    runtime_lease_expires_at: str = Field(min_length=1, max_length=100)
    authorized_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    state: Literal["authorized"] = "authorized"
    installation_authority: Literal[True] = True
    execution_authority: Literal[True] = True
    execution_started: Literal[False] = False
    result_authority: Literal[False] = False
    result_received: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_base64(
            self.worker_signature_base64,
            expected=64,
            field="Remote Start signature",
        )
        if not hmac.compare_digest(
            self.worker_signature_sha256,
            hashlib.sha256(signature).hexdigest(),
        ):
            raise ValueError("Remote execution Worker signature 摘要不一致。")
        payload = self.start_payload
        authorized = _aware(self.authorized_at)
        expected_expiry = min(
            authorized + timedelta(seconds=payload.max_wall_seconds),
            _aware(payload.claim_lease_expires_at),
            _aware(payload.reservation_expires_at),
            _aware(payload.delivery_expires_at),
            _aware(self.run_grant.expires_at),
            _aware(self.runtime_lease_expires_at),
        )
        if not (
            _aware(payload.issued_at)
            <= authorized
            < _aware(payload.start_deadline)
            and self.attempt_id == payload.attempt_id
            and self.run_grant.parent_receipt_id
            == payload.parent_permission_receipt_id
            and self.run_grant.parent_receipt_sha256
            == payload.parent_permission_receipt_sha256
            and self.run_grant.lease_owner_id == self.runtime_lease_owner_id
            and self.run_grant.lease_epoch == self.runtime_lease_epoch
            and self.run_grant.delegated_tool_names == payload.allowed_tool_names
            and authorized < _aware(self.expires_at) == expected_expiry
        ):
            raise ValueError("Remote execution Grant/lease/expiry projection 不一致。")
        core = self.model_dump(
            mode="json", exclude={"authorization_id", "authorization_sha256"}
        )
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.authorization_sha256, digest)
            and self.authorization_id == f"evpostexecauth_{digest[:24]}"
        ):
            raise ValueError("Remote execution authorization identity 不一致。")
        return self


class EvolutionPostRollbackRemoteExecutionAuthorizationView(_StrictModel):
    challenge: EvolutionPostRollbackRemoteStartChallenge
    authorization: EvolutionPostRollbackRemoteExecutionAuthorization | None = None
    status: ExecutionStatus
    delivery_authority: bool
    claim_authority: bool
    permission_authority: bool
    run_grant_authority: bool
    runtime_lease_authority: bool
    installation_authorized: bool
    execution_authorized: bool
    execution_started: Literal[False] = False
    result_authority: Literal[False] = False
    result_received: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = self.status == "current"
        if not (
            self.installation_authorized is current
            and self.execution_authorized is current
            and (not current or self.authorization is not None)
        ):
            raise ValueError("Remote execution authorization view 投影不一致。")
        if current and not all(
            (
                self.delivery_authority,
                self.claim_authority,
                self.permission_authority,
                self.run_grant_authority,
                self.runtime_lease_authority,
            )
        ):
            raise ValueError("Current Remote execution view 缺少 authority。")
        return self


class EvolutionPostRollbackRemoteExecutionAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRemoteExecutionAuthorizationStore:
    """Session SQLite boundary for start challenges and signed authorizations."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)

    async def get_challenge(
        self, challenge_id: str
    ) -> EvolutionPostRollbackRemoteStartChallenge | None:
        return await self._get_challenge("challenge_id", _challenge_id_value(challenge_id))

    async def get_latest_for_attempt(
        self, attempt_id: str
    ) -> EvolutionPostRollbackRemoteStartChallenge | None:
        return await self._get_challenge("attempt_id", _attempt_id_value(attempt_id))

    async def get_authorization_for_attempt(
        self, attempt_id: str
    ) -> EvolutionPostRollbackRemoteExecutionAuthorization | None:
        normalized = _attempt_id_value(attempt_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT authorization_json FROM "
                        "evolution_post_rollback_remote_execution_authorizations "
                        "WHERE attempt_id = ?",
                        (normalized,),
                    )
                ).fetchone()
            return None if row is None else _restore_authorization(row["authorization_json"])
        except EvolutionPostRollbackRemoteExecutionAuthorizationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_store_corrupt",
                "Remote execution authorization 损坏或无法读取。",
            ) from exc

    async def resolve_attempt_id(self, reference_id: str) -> str:
        value = str(reference_id or "").strip()
        if value.startswith("evpostattempt_"):
            return _attempt_id_value(value)
        if not self.db_path.is_file():
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_reference_missing",
                "Remote execution reference 不存在。",
            )
        if value.startswith("evpoststart_"):
            challenge = await self.get_challenge(value)
            if challenge is not None:
                return challenge.attempt_id
        if value.startswith("evpostexecauth_"):
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT attempt_id FROM "
                        "evolution_post_rollback_remote_execution_authorizations "
                        "WHERE authorization_id = ?",
                        (value,),
                    )
                ).fetchone()
            if row is not None:
                return _attempt_id_value(row["attempt_id"])
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_reference_missing",
            "Remote execution reference 不存在。",
        )

    async def record_challenge(
        self,
        challenge: EvolutionPostRollbackRemoteStartChallenge,
        *,
        assessed_at: str,
    ) -> EvolutionPostRollbackRemoteStartChallenge:
        item = _validate_challenge(challenge)
        now = _aware(assessed_at)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_CHALLENGE_BYTES:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_start_challenge_oversized",
                "Remote Start challenge 超过 768 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_atomic_start_authority(db, item.payload, now=now)
                existing_row = await (
                    await db.execute(
                        "SELECT challenge_json, state FROM "
                        "evolution_post_rollback_remote_start_challenges "
                        "WHERE attempt_id = ? ORDER BY issued_at DESC LIMIT 1",
                        (item.attempt_id,),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _restore_challenge(existing_row["challenge_json"])
                    state = str(existing_row["state"])
                    if state == "authorized":
                        await db.rollback()
                        return existing
                    if state == "pending" and _aware(existing.payload.start_deadline) > now:
                        await db.rollback()
                        if _same_start_scope(existing, item):
                            return existing
                        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                            "post_rollback_remote_start_challenge_conflict",
                            "同一 Remote attempt 已绑定不同 pending start scope。",
                        )
                    if state == "pending":
                        await db.execute(
                            "UPDATE evolution_post_rollback_remote_start_challenges "
                            "SET state = 'expired' WHERE challenge_id = ? AND state = 'pending'",
                            (existing.challenge_id,),
                        )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_start_challenges "
                    "(challenge_id, challenge_sha256, attempt_id, delivery_id, "
                    "claim_receipt_id, parent_permission_receipt_id, challenge_json, "
                    "state, issued_at, start_deadline, closed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '')",
                    (
                        item.challenge_id,
                        item.challenge_sha256,
                        item.attempt_id,
                        item.payload.delivery_id,
                        item.payload.claim_receipt_id,
                        item.payload.parent_permission_receipt_id,
                        encoded,
                        item.payload.issued_at,
                        item.payload.start_deadline,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackRemoteExecutionAuthorizationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_start_challenge_store_failed",
                "Remote Start challenge 无法持久化。",
            ) from exc

    async def record_authorization(
        self,
        authorization: EvolutionPostRollbackRemoteExecutionAuthorization,
        *,
        assessed_at: str,
    ) -> EvolutionPostRollbackRemoteExecutionAuthorization:
        item = _validate_authorization(authorization)
        now = _aware(assessed_at)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_AUTHORIZATION_BYTES:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_authorization_oversized",
                "Remote execution authorization 超过 896 KiB。",
            )
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT challenge_json, state, closed_by FROM "
                        "evolution_post_rollback_remote_start_challenges "
                        "WHERE challenge_id = ?",
                        (item.challenge_id,),
                    )
                ).fetchone()
                if row is None:
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_start_challenge_missing",
                        "Remote Start challenge 不存在。",
                    )
                challenge = _restore_challenge(row["challenge_json"])
                if str(row["state"]) == "authorized":
                    existing_row = await (
                        await db.execute(
                            "SELECT authorization_json FROM "
                            "evolution_post_rollback_remote_execution_authorizations "
                            "WHERE attempt_id = ?",
                            (challenge.attempt_id,),
                        )
                    ).fetchone()
                    await db.rollback()
                    if existing_row is None:
                        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                            "post_rollback_remote_execution_atomic_gap",
                            "Authorized start challenge 缺少 execution authorization。",
                        )
                    existing = _restore_authorization(existing_row["authorization_json"])
                    if hmac.compare_digest(
                        existing.worker_signature_base64,
                        item.worker_signature_base64,
                    ):
                        return existing
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_execution_signature_conflict",
                        "Remote attempt 已由不同 Worker start signature 关闭。",
                    )
                if str(row["state"]) != "pending":
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_start_challenge_closed",
                        "Remote Start challenge 已关闭。",
                    )
                if not _aware(challenge.payload.issued_at) <= now < _aware(
                    challenge.payload.start_deadline
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_start_window_invalid",
                        "Remote Start signature 不在 challenge 窗口内。",
                    )
                identity = await _require_atomic_start_authority(
                    db, challenge.payload, now=now
                )
                if not (
                    item.challenge_sha256 == challenge.challenge_sha256
                    and item.start_payload == challenge.payload
                    and item.attempt_id == challenge.attempt_id
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_execution_challenge_mismatch",
                        "Remote execution authorization 未绑定 exact challenge。",
                    )
                _verify_start_signature(
                    identity,
                    item.worker_signature_base64,
                    challenge.payload.canonical_bytes(),
                )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_execution_authorizations "
                    "(authorization_id, authorization_sha256, attempt_id, challenge_id, "
                    "authorization_json, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.authorization_id,
                        item.authorization_sha256,
                        item.attempt_id,
                        item.challenge_id,
                        encoded,
                        item.expires_at,
                    ),
                )
                cursor = await db.execute(
                    "UPDATE evolution_post_rollback_remote_start_challenges "
                    "SET state = 'authorized', closed_by = ? "
                    "WHERE challenge_id = ? AND state = 'pending'",
                    (item.authorization_id, item.challenge_id),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_execution_atomic_conflict",
                        "Remote Start challenge 原子关闭失败。",
                    )
                await db.commit()
            return item
        except EvolutionPostRollbackRemoteExecutionAuthorizationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_authorization_store_failed",
                "Remote execution authorization 无法持久化。",
            ) from exc

    async def _get_challenge(
        self, column: Literal["challenge_id", "attempt_id"], value: str
    ) -> EvolutionPostRollbackRemoteStartChallenge | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT challenge_json FROM "
                        "evolution_post_rollback_remote_start_challenges "
                        f"WHERE {column} = ? ORDER BY issued_at DESC LIMIT 1",
                        (value,),
                    )
                ).fetchone()
            return None if row is None else _restore_challenge(row["challenge_json"])
        except EvolutionPostRollbackRemoteExecutionAuthorizationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_store_corrupt",
                "Remote Start challenge 损坏或无法读取。",
            ) from exc


class EvolutionPostRollbackRemoteExecutionAuthorizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        delivery_service: EvolutionPostRollbackRemoteDeliveryService,
        delivery_store: EvolutionPostRollbackRemoteDeliveryStore,
        claim_service: EvolutionPostRollbackRemoteClaimService,
        claim_store: EvolutionPostRollbackRemoteClaimStore,
        dispatch_service: EvolutionPostRollbackRemoteDispatchService,
        dispatch_store: EvolutionPostRollbackRemoteDispatchStore,
        identity_authority: AuthenticatedWorkerIdentityAuthority,
        permission_store: PermissionDecisionReceiptStore,
        harness_store: HarnessStore,
        run_grant_authority: RunDelegationGrantAuthority,
        store: EvolutionPostRollbackRemoteExecutionAuthorizationStore,
        random_bytes: Callable[[int], bytes] = os.urandom,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        session_paths = {
            delivery_store.db_path,
            claim_store.db_path,
            dispatch_store.db_path,
            identity_authority.store.db_path,
            store.db_path,
        }
        if len(session_paths) != 1:
            raise ValueError(
                "Remote execution 的 Delivery/Claim/Dispatch/Identity/Authorization "
                "store 必须共享 session SQLite authority。"
            )
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.delivery_service = delivery_service
        self.delivery_store = delivery_store
        self.claim_service = claim_service
        self.claim_store = claim_store
        self.dispatch_service = dispatch_service
        self.dispatch_store = dispatch_store
        self.identity_authority = identity_authority
        self.permission_store = permission_store
        self.harness_store = harness_store
        self.run_grant_authority = run_grant_authority
        self.store = store
        self.random_bytes = random_bytes
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare(
        self,
        *,
        delivery_id: str,
        parent_permission_receipt_id: str,
        issued_at: str | None = None,
        challenge_ttl_seconds: int = 5,
    ) -> EvolutionPostRollbackRemoteExecutionAuthorizationView:
        if isinstance(challenge_ttl_seconds, bool) or not isinstance(
            challenge_ttl_seconds, int
        ):
            raise TypeError("challenge_ttl_seconds 必须是整数。")
        if not 1 <= challenge_ttl_seconds <= 120:
            raise ValueError("challenge_ttl_seconds 必须在 1..120。")
        now = _aware(issued_at) if issued_at is not None else _aware_datetime(self.clock())
        lock = self._locks.setdefault(f"prepare:{delivery_id}", asyncio.Lock())
        async with lock:
            context = await self._context(
                delivery_id=delivery_id,
                parent_permission_receipt_id=parent_permission_receipt_id,
                now=now,
            )
            attempt_id = _attempt_id(
                delivery_receipt_sha256=context.delivery_receipt.receipt_sha256,
                dispatch_sha256=context.dispatch.dispatch_sha256,
                attempt_number=context.dispatch.attempt,
                suite_sha256=context.dispatch.suite_sha256,
            )
            existing = await self.store.get_latest_for_attempt(attempt_id)
            if existing is not None:
                authorization = await self.store.get_authorization_for_attempt(attempt_id)
                if authorization is not None:
                    return await self.inspect(
                        reference_id=authorization.authorization_id,
                        assessed_at=now.isoformat(),
                    )
                if _aware(existing.payload.start_deadline) > now:
                    if not _payload_matches_context(existing.payload, context):
                        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                            "post_rollback_remote_start_challenge_stale",
                            "Pending Remote Start challenge authority 已变化。",
                        )
                    return _view(
                        existing,
                        None,
                        status="awaiting_start",
                        flags=(True, True, True, False, False),
                    )
            deadline = min(
                now + timedelta(seconds=challenge_ttl_seconds),
                _aware(context.claim.lease_expires_at),
                _aware(context.dispatch.reservation_expires_at),
                _aware(context.delivery_offer.envelope.expires_at),
                _aware(context.parent_permission.decided_at)
                + timedelta(seconds=300),
            )
            if deadline <= now:
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_start_window_unavailable",
                    "Remote Claim/Delivery 剩余窗口不足以签发 Start challenge。",
                )
            challenge = _build_challenge(
                context=context,
                attempt_id=attempt_id,
                issued_at=now,
                start_deadline=deadline,
                start_nonce=self.random_bytes(32),
            )
            stored = await self.store.record_challenge(
                challenge, assessed_at=now.isoformat()
            )
            return await self.inspect(
                reference_id=stored.challenge_id,
                assessed_at=now.isoformat(),
            )

    async def submit(
        self,
        *,
        challenge_id: str,
        worker_signature_base64: str,
        authorized_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteExecutionAuthorizationView:
        explicit_time = authorized_at is not None
        now = _aware(authorized_at) if explicit_time else _aware_datetime(self.clock())
        lock = self._locks.setdefault(f"submit:{challenge_id}", asyncio.Lock())
        async with lock:
            challenge = await self.store.get_challenge(challenge_id)
            if challenge is None:
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_start_challenge_missing",
                    "Remote Start challenge 不存在。",
                )
            existing = await self.store.get_authorization_for_attempt(
                challenge.attempt_id
            )
            if existing is not None:
                if hmac.compare_digest(
                    existing.worker_signature_base64, worker_signature_base64
                ):
                    return await self.inspect(
                        reference_id=existing.authorization_id,
                        assessed_at=now.isoformat(),
                    )
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_execution_signature_conflict",
                    "Remote attempt 已由不同 Worker start signature 关闭。",
                )
            if not _aware(challenge.payload.issued_at) <= now < _aware(
                challenge.payload.start_deadline
            ):
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_start_window_invalid",
                    "Remote Start signature 不在 challenge 窗口内。",
                )
            context = await self._context(
                delivery_id=challenge.payload.delivery_id,
                parent_permission_receipt_id=(
                    challenge.payload.parent_permission_receipt_id
                ),
                now=now,
            )
            if not _payload_matches_context(challenge.payload, context):
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_start_challenge_stale",
                    "Remote Start challenge authority 已变化。",
                )
            normalized_signature = _canonical_base64(
                worker_signature_base64,
                expected=64,
                field="Remote Start signature",
            )
            _verify_start_signature(
                context.identity,
                normalized_signature,
                challenge.payload.canonical_bytes(),
            )
            duration = challenge.payload.max_wall_seconds
            execution_deadline = now + timedelta(seconds=duration)
            hard_deadline = min(
                _aware(context.claim.lease_expires_at),
                _aware(context.dispatch.reservation_expires_at),
                _aware(context.delivery_offer.envelope.expires_at),
            )
            if execution_deadline > hard_deadline:
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_execution_window_too_short",
                    "Current Claim/Delivery 窗口不足以覆盖完整 Eval 预算。",
                )
            owner = _lease_owner(challenge.attempt_id, challenge.challenge_sha256)
            lease = await self.harness_store.acquire_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=context.parent_permission.run_id,
                owner_id=owner,
                now=now.isoformat(),
                lease_seconds=duration,
            )
            if lease is None:
                raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                    "post_rollback_remote_execution_runtime_lease_unavailable",
                    "无法取得 Remote execution 所需 Runtime lease。",
                )
            grant = None
            try:
                grant = await self.run_grant_authority.issue(
                    RunDelegationGrantRequest(
                        idempotency_key=_grant_key(
                            challenge.attempt_id, challenge.challenge_sha256
                        ),
                        parent_receipt_id=context.parent_permission.receipt_id,
                        run_kind=HarnessRunKind.RUNTIME,
                        lease_owner_id=owner,
                        lease_epoch=lease.epoch,
                        delegated_tool_names=("bash_run",),
                    ),
                    now=now.isoformat(),
                    ttl_seconds=duration,
                )
                committed_at = now if explicit_time else _aware_datetime(self.clock())
                if not _aware(challenge.payload.issued_at) <= committed_at < _aware(
                    challenge.payload.start_deadline
                ):
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_start_window_invalid",
                        "Remote Start signature 提交期间越过 challenge deadline。",
                    )
                context = await self._context(
                    delivery_id=challenge.payload.delivery_id,
                    parent_permission_receipt_id=(
                        challenge.payload.parent_permission_receipt_id
                    ),
                    now=committed_at,
                )
                if not _payload_matches_context(challenge.payload, context):
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_execution_fenced_before_commit",
                        "Remote execution authority 在提交前已变化。",
                    )
                authorization = _build_authorization(
                    challenge=challenge,
                    worker_signature_base64=normalized_signature,
                    lease=lease,
                    grant=grant.contract,
                    authorized_at=committed_at,
                )
                stored = await self.store.record_authorization(
                    authorization, assessed_at=committed_at.isoformat()
                )
                view = await self.inspect(
                    reference_id=stored.authorization_id,
                    assessed_at=committed_at.isoformat(),
                )
                if view.status != "current":
                    raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                        "post_rollback_remote_execution_fenced_during_commit",
                        "Remote execution authorization 提交期间已被 fencing。",
                    )
                return view
            except BaseException:
                if grant is not None:
                    await self._best_effort_cleanup(
                        grant_id=grant.contract.grant_id,
                        run_id=context.parent_permission.run_id,
                        owner_id=owner,
                        lease_epoch=lease.epoch,
                        now=now,
                    )
                else:
                    await self._best_effort_release(
                        run_id=context.parent_permission.run_id,
                        owner_id=owner,
                        lease_epoch=lease.epoch,
                        now=now,
                    )
                raise

    async def inspect(
        self,
        *,
        reference_id: str,
        assessed_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteExecutionAuthorizationView:
        now = (
            _aware(assessed_at)
            if assessed_at is not None
            else _aware_datetime(self.clock())
        )
        attempt_id = await self.store.resolve_attempt_id(reference_id)
        challenge = await self.store.get_latest_for_attempt(attempt_id)
        if challenge is None:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_start_challenge_missing",
                "Remote Start challenge 不存在。",
            )
        authorization = await self.store.get_authorization_for_attempt(attempt_id)
        flags = [False, False, False, False, False]
        if authorization is None:
            if now >= _aware(challenge.payload.start_deadline):
                status: ExecutionStatus = "expired"
            else:
                try:
                    context = await self._context(
                        delivery_id=challenge.payload.delivery_id,
                        parent_permission_receipt_id=(
                            challenge.payload.parent_permission_receipt_id
                        ),
                        now=now,
                    )
                    if not _payload_matches_context(challenge.payload, context):
                        raise ValueError("start context changed")
                    flags[:3] = [True, True, True]
                    status = "awaiting_start"
                except _DYNAMIC_ERRORS:
                    status = "stale"
            return _view(challenge, None, status=status, flags=tuple(flags))
        if now >= _aware(authorization.expires_at):
            status = "expired"
        else:
            try:
                context = await self._context(
                    delivery_id=challenge.payload.delivery_id,
                    parent_permission_receipt_id=(
                        challenge.payload.parent_permission_receipt_id
                    ),
                    now=now,
                )
                grant = await self.run_grant_authority.validate(
                    grant_id=authorization.run_grant.grant_id,
                    now=now.isoformat(),
                )
                lease = await self.harness_store.get_run_lease(
                    workspace_root=self.workspace_root,
                    run_kind=HarnessRunKind.RUNTIME,
                    run_id=context.parent_permission.run_id,
                )
                if not (
                    _payload_matches_context(challenge.payload, context)
                    and grant.allowed
                    and grant.contract == authorization.run_grant.as_contract()
                    and lease is not None
                    and lease.state is HarnessRunLeaseState.ACTIVE
                    and lease.owner_id == authorization.runtime_lease_owner_id
                    and lease.epoch == authorization.runtime_lease_epoch
                    and _aware(lease.expires_at) > now
                ):
                    raise ValueError("execution authority changed")
                flags = [True, True, True, True, True]
                status = "current"
            except _DYNAMIC_ERRORS:
                status = "stale"
        return _view(challenge, authorization, status=status, flags=tuple(flags))

    async def _context(
        self,
        *,
        delivery_id: str,
        parent_permission_receipt_id: str,
        now: datetime,
    ) -> _ExecutionContext:
        delivery = await self.delivery_service.inspect(
            delivery_id=delivery_id,
            assessed_at=now.isoformat(),
        )
        if delivery.status != "delivered" or delivery.receipt is None:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_delivery_not_current",
                "Remote execution 需要 current Delivery receipt。",
            )
        offer = delivery.offer
        receipt = delivery.receipt
        claim_view = await self.claim_service.inspect(
            claim_id=receipt.claim_id,
            assessed_at=now.isoformat(),
        )
        if not (
            claim_view.status == "current"
            and claim_view.receipt.receipt_id == receipt.claim_receipt_id
            and claim_view.receipt.receipt_sha256 == receipt.claim_receipt_sha256
        ):
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_claim_not_current",
                "Remote execution Claim receipt 已失效或推进。",
            )
        dispatch = await self.dispatch_store.get_by_id(receipt.dispatch_id)
        if dispatch is None:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_dispatch_missing",
                "Remote execution Dispatch 不存在。",
            )
        dispatch_view = await self.dispatch_service.inspect(
            dispatch=dispatch,
            assessed_at=now.isoformat(),
        )
        if not dispatch_view.dispatch_authority:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_dispatch_stale",
                "Remote execution Dispatch authority 已失效。",
            )
        identity = await self.identity_authority.store.get(receipt.identity_id)
        if identity is None:
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_identity_missing",
                "Remote execution Worker Identity 不存在。",
            )
        identity_view = await self.identity_authority.inspect(identity)
        registration = await self.claim_service.worker_registry.get_active(
            dispatch.worker_id
        )
        parent = await self.permission_store.get(parent_permission_receipt_id)
        parent_fresh = bool(
            parent is not None
            and _aware(parent.decided_at) <= now
            and now - _aware(parent.decided_at) <= timedelta(seconds=300)
        )
        if not (
            identity_view.identity_authority
            and registration is not None
            and registration.contract.worker_id == receipt.worker_id
            and registration.contract.instance_id == receipt.worker_instance_id
            and registration.contract.epoch == receipt.worker_epoch
            and registration.contract.contract_sha256
            == dispatch.worker_contract_sha256
            and parent is not None
            and parent_fresh
            and parent.authorizes_execution
            and parent.run_id
            and parent.source
            in {
                PermissionDecisionSource.POLICY,
                PermissionDecisionSource.BYPASS,
                PermissionDecisionSource.USER_CONFIRMATION,
            }
            and "bash_run" in parent.delegated_tool_names
            and _execution_lineage_matches(receipt, offer, dispatch, claim_view.receipt, identity)
        ):
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "post_rollback_remote_execution_authority_stale",
                "Remote execution Worker/permission/lineage authority 已失效。",
            )
        return _ExecutionContext(
            delivery_offer=offer,
            delivery_receipt=receipt,
            claim=claim_view.receipt,
            dispatch=dispatch,
            identity=identity,
            worker_contract=registration.contract,
            parent_permission=parent,
        )

    async def _best_effort_cleanup(
        self,
        *,
        grant_id: str,
        run_id: str,
        owner_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> None:
        try:
            await self.run_grant_authority.revoke(
                grant_id=grant_id,
                reason="remote_execution_authorization_failed",
                revoked_at=now.isoformat(),
            )
        except (RunDelegationGrantError, OSError, TypeError, ValueError):
            pass
        await self._best_effort_release(
            run_id=run_id,
            owner_id=owner_id,
            lease_epoch=lease_epoch,
            now=now,
        )

    async def _best_effort_release(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> None:
        try:
            await self.harness_store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run_id,
                owner_id=owner_id,
                epoch=lease_epoch,
                now=now.isoformat(),
            )
        except (HarnessStoreError, OSError, TypeError, ValueError):
            pass


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    delivery_offer: EvolutionPostRollbackRemoteDeliveryOffer
    delivery_receipt: EvolutionPostRollbackRemoteDeliveryReceipt
    claim: EvolutionPostRollbackRemoteClaimReceipt
    dispatch: EvolutionPostRollbackRemoteDispatch
    identity: AuthenticatedWorkerIdentity
    worker_contract: WorkerContract
    parent_permission: PermissionDecisionReceipt


_DYNAMIC_ERRORS = (
    AuthenticatedWorkerIdentityError,
    EvolutionPostRollbackRemoteClaimError,
    EvolutionPostRollbackRemoteDeliveryError,
    EvolutionPostRollbackRemoteDispatchError,
    EvolutionPostRollbackRemoteExecutionAuthorizationError,
    HarnessStoreError,
    RunDelegationGrantError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


def _build_challenge(
    *,
    context: _ExecutionContext,
    attempt_id: str,
    issued_at: datetime,
    start_deadline: datetime,
    start_nonce: bytes,
) -> EvolutionPostRollbackRemoteStartChallenge:
    dispatch = context.dispatch
    receipt = context.delivery_receipt
    contract = context.worker_contract
    isolation = contract.isolation
    wall_seconds = max(1, math.ceil(dispatch.total_execution_budget_ms / 1_000))
    if wall_seconds > 3_600:
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_budget_too_large",
            "Remote Eval 单次执行预算超过 3600 秒 Run Grant 上限。",
        )
    if not (
        contract.resources.max_memory_bytes >= 16 * 1024 * 1024
        and contract.resources.max_cpu_seconds >= wall_seconds
        and contract.resources.max_wall_seconds >= wall_seconds
        and contract.resources.max_output_bytes >= MAX_RUNTIME_EVAL_OUTPUT_BYTES
        and isolation.ephemeral_workspace
        and isolation.network_default_deny
        and isolation.environment_allowlist
        and isolation.resource_limits_enforced
        and isolation.process_tree_cancel
        and isolation.artifact_digest
    ):
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_resource_unavailable",
            "Worker resource/isolation contract 不足以执行 exact Eval。",
        )
    payload = EvolutionPostRollbackRemoteStartPayload(
        attempt_id=attempt_id,
        attempt_number=dispatch.attempt,
        delivery_id=receipt.delivery_id,
        delivery_receipt_id=receipt.receipt_id,
        delivery_receipt_sha256=receipt.receipt_sha256,
        delivery_offer_sha256=receipt.offer_sha256,
        delivery_expires_at=context.delivery_offer.envelope.expires_at,
        dispatch_id=dispatch.dispatch_id,
        dispatch_sha256=dispatch.dispatch_sha256,
        claim_id=receipt.claim_id,
        claim_receipt_id=receipt.claim_receipt_id,
        claim_receipt_sha256=receipt.claim_receipt_sha256,
        claim_lease_epoch=receipt.claim_lease_epoch,
        claim_lease_expires_at=context.claim.lease_expires_at,
        reservation_expires_at=dispatch.reservation_expires_at,
        identity_id=receipt.identity_id,
        identity_sha256=receipt.identity_sha256,
        worker_id=receipt.worker_id,
        worker_instance_id=receipt.worker_instance_id,
        worker_epoch=receipt.worker_epoch,
        worker_contract_sha256=dispatch.worker_contract_sha256,
        workspace_root=dispatch.workspace_root,
        release_target=dispatch.release_target,
        baseline_resolution_sha256=receipt.baseline_resolution_sha256,
        archive_sha256=receipt.archive_sha256,
        manifest_sha256=receipt.manifest_sha256,
        suite_id=dispatch.suite_id,
        suite_sha256=dispatch.suite_sha256,
        repetitions=dispatch.repetitions,
        runtime_eval_request=dispatch.runtime_eval_request,
        case_execution_budget_ms=dispatch.case_execution_budget_ms,
        total_execution_budget_ms=dispatch.total_execution_budget_ms,
        max_memory_bytes=contract.resources.max_memory_bytes,
        max_cpu_seconds=wall_seconds,
        max_wall_seconds=wall_seconds,
        max_case_output_bytes=MAX_RUNTIME_EVAL_OUTPUT_BYTES,
        max_result_bytes=MAX_RUNTIME_EVAL_OUTPUT_BYTES * dispatch.repetitions,
        parent_permission_receipt_id=context.parent_permission.receipt_id,
        parent_permission_receipt_sha256=context.parent_permission.receipt_sha256,
        start_nonce_base64=base64.b64encode(start_nonce).decode("ascii"),
        issued_at=issued_at.isoformat(),
        start_deadline=start_deadline.isoformat(),
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_EXECUTION_POLICY,
        "attempt_id": attempt_id,
        "payload": payload.model_dump(mode="json"),
        "signable_payload_sha256": hashlib.sha256(payload.canonical_bytes()).hexdigest(),
        "state": "awaiting_worker_signature",
        "installation_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionPostRollbackRemoteStartChallenge.model_validate(
        {**core, "challenge_id": f"evpoststart_{digest[:24]}", "challenge_sha256": digest}
    )


def _build_authorization(
    *,
    challenge: EvolutionPostRollbackRemoteStartChallenge,
    worker_signature_base64: str,
    lease: HarnessRunLease,
    grant: RunDelegationGrantContract,
    authorized_at: datetime,
) -> EvolutionPostRollbackRemoteExecutionAuthorization:
    run_grant = EvolutionPostRollbackRemoteRunGrantEnvelope.from_contract(grant)
    payload = challenge.payload
    expires = min(
        authorized_at + timedelta(seconds=payload.max_wall_seconds),
        _aware(payload.claim_lease_expires_at),
        _aware(payload.reservation_expires_at),
        _aware(payload.delivery_expires_at),
        _aware(run_grant.expires_at),
        _aware(lease.expires_at),
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_EXECUTION_POLICY,
        "attempt_id": challenge.attempt_id,
        "challenge_id": challenge.challenge_id,
        "challenge_sha256": challenge.challenge_sha256,
        "start_payload": payload.model_dump(mode="json"),
        "worker_signature_base64": worker_signature_base64,
        "worker_signature_sha256": hashlib.sha256(
            _decode_base64(worker_signature_base64, expected=64, field="Start signature")
        ).hexdigest(),
        "worker_signature_verified": True,
        "run_grant": run_grant.model_dump(mode="json"),
        "runtime_lease_owner_id": lease.owner_id,
        "runtime_lease_epoch": lease.epoch,
        "runtime_lease_expires_at": lease.expires_at,
        "authorized_at": authorized_at.isoformat(),
        "expires_at": expires.isoformat(),
        "state": "authorized",
        "installation_authority": True,
        "execution_authority": True,
        "execution_started": False,
        "result_authority": False,
        "result_received": False,
        "learning_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionPostRollbackRemoteExecutionAuthorization.model_validate(
        {
            **core,
            "authorization_id": f"evpostexecauth_{digest[:24]}",
            "authorization_sha256": digest,
        }
    )


def _view(
    challenge: EvolutionPostRollbackRemoteStartChallenge,
    authorization: EvolutionPostRollbackRemoteExecutionAuthorization | None,
    *,
    status: ExecutionStatus,
    flags: tuple[bool, bool, bool, bool, bool],
) -> EvolutionPostRollbackRemoteExecutionAuthorizationView:
    current = status == "current"
    return EvolutionPostRollbackRemoteExecutionAuthorizationView(
        challenge=challenge,
        authorization=authorization,
        status=status,
        delivery_authority=flags[0],
        claim_authority=flags[1],
        permission_authority=flags[2],
        run_grant_authority=flags[3],
        runtime_lease_authority=flags[4],
        installation_authorized=current,
        execution_authorized=current,
    )


def _payload_matches_context(
    payload: EvolutionPostRollbackRemoteStartPayload,
    context: _ExecutionContext,
) -> bool:
    receipt = context.delivery_receipt
    dispatch = context.dispatch
    claim = context.claim
    identity = context.identity
    contract = context.worker_contract
    parent = context.parent_permission
    return bool(
        payload.delivery_id == receipt.delivery_id
        and payload.delivery_receipt_id == receipt.receipt_id
        and payload.delivery_receipt_sha256 == receipt.receipt_sha256
        and payload.delivery_offer_sha256 == receipt.offer_sha256
        and payload.delivery_expires_at == context.delivery_offer.envelope.expires_at
        and payload.dispatch_id == dispatch.dispatch_id
        and payload.dispatch_sha256 == dispatch.dispatch_sha256
        and payload.claim_id == claim.claim_id == receipt.claim_id
        and payload.claim_receipt_id == claim.receipt_id == receipt.claim_receipt_id
        and payload.claim_receipt_sha256
        == claim.receipt_sha256
        == receipt.claim_receipt_sha256
        and payload.claim_lease_epoch == claim.lease_epoch
        and payload.claim_lease_expires_at == claim.lease_expires_at
        and payload.reservation_expires_at == dispatch.reservation_expires_at
        and payload.identity_id == identity.identity_id == receipt.identity_id
        and payload.identity_sha256 == identity.identity_sha256 == receipt.identity_sha256
        and payload.worker_id == contract.worker_id == receipt.worker_id
        and payload.worker_instance_id == contract.instance_id == receipt.worker_instance_id
        and payload.worker_epoch == contract.epoch == receipt.worker_epoch
        and payload.worker_contract_sha256 == contract.contract_sha256
        and payload.workspace_root == dispatch.workspace_root
        and payload.release_target == dispatch.release_target
        and payload.baseline_resolution_sha256 == receipt.baseline_resolution_sha256
        and payload.archive_sha256 == receipt.archive_sha256
        and payload.manifest_sha256 == receipt.manifest_sha256
        and payload.suite_id == dispatch.suite_id
        and payload.suite_sha256 == dispatch.suite_sha256
        and payload.repetitions == dispatch.repetitions
        and payload.runtime_eval_request == dispatch.runtime_eval_request
        and payload.case_execution_budget_ms == dispatch.case_execution_budget_ms
        and payload.total_execution_budget_ms == dispatch.total_execution_budget_ms
        and payload.max_memory_bytes == contract.resources.max_memory_bytes
        and payload.parent_permission_receipt_id == parent.receipt_id
        and payload.parent_permission_receipt_sha256 == parent.receipt_sha256
    )


def _execution_lineage_matches(
    receipt: EvolutionPostRollbackRemoteDeliveryReceipt,
    offer: EvolutionPostRollbackRemoteDeliveryOffer,
    dispatch: EvolutionPostRollbackRemoteDispatch,
    claim: EvolutionPostRollbackRemoteClaimReceipt,
    identity: AuthenticatedWorkerIdentity,
) -> bool:
    return bool(
        receipt.offer_id == offer.offer_id
        and receipt.offer_sha256 == offer.offer_sha256
        and receipt.delivery_id == offer.delivery_id
        and receipt.dispatch_id == dispatch.dispatch_id == claim.dispatch_id
        and receipt.dispatch_sha256 == dispatch.dispatch_sha256 == claim.dispatch_sha256
        and receipt.claim_id == claim.claim_id == offer.claim_id
        and receipt.claim_receipt_id == claim.receipt_id == offer.claim_receipt_id
        and receipt.claim_receipt_sha256
        == claim.receipt_sha256
        == offer.claim_receipt_sha256
        and receipt.identity_id == identity.identity_id == offer.identity_id
        and receipt.identity_sha256 == identity.identity_sha256 == offer.identity_sha256
        and receipt.worker_id == dispatch.worker_id == claim.worker_id
        and receipt.worker_instance_id
        == dispatch.worker_instance_id
        == claim.worker_instance_id
        and receipt.worker_epoch == dispatch.worker_epoch == claim.worker_epoch
    )


async def _require_atomic_start_authority(
    db: aiosqlite.Connection,
    payload: EvolutionPostRollbackRemoteStartPayload,
    *,
    now: datetime,
) -> AuthenticatedWorkerIdentity:
    offer_row = await (
        await db.execute(
            "SELECT offer_json, state FROM evolution_post_rollback_remote_delivery_offers "
            "WHERE delivery_id = ?",
            (payload.delivery_id,),
        )
    ).fetchone()
    receipt_row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_post_rollback_remote_delivery_receipts "
            "WHERE receipt_id = ?",
            (payload.delivery_receipt_id,),
        )
    ).fetchone()
    claim_row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_post_rollback_remote_claim_receipts "
            "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
            (payload.claim_id,),
        )
    ).fetchone()
    dispatch_row = await (
        await db.execute(
            "SELECT dispatch_json FROM evolution_post_rollback_remote_dispatches "
            "WHERE dispatch_id = ?",
            (payload.dispatch_id,),
        )
    ).fetchone()
    identity_row = await (
        await db.execute(
            "SELECT identity_json FROM authenticated_worker_identities "
            "WHERE identity_id = ?",
            (payload.identity_id,),
        )
    ).fetchone()
    if any(row is None for row in (offer_row, receipt_row, claim_row, dispatch_row, identity_row)):
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_atomic_authority_missing",
            "Remote execution atomic authority dependency 缺失。",
        )
    try:
        offer = EvolutionPostRollbackRemoteDeliveryOffer.model_validate_json(
            offer_row["offer_json"]
        )
        receipt = EvolutionPostRollbackRemoteDeliveryReceipt.model_validate_json(
            receipt_row["receipt_json"]
        )
        claim = EvolutionPostRollbackRemoteClaimReceipt.model_validate_json(
            claim_row["receipt_json"]
        )
        dispatch = EvolutionPostRollbackRemoteDispatch.model_validate_json(
            dispatch_row["dispatch_json"]
        )
        identity = AuthenticatedWorkerIdentity.model_validate_json(
            identity_row["identity_json"]
        )
    except ValueError as exc:
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_atomic_authority_corrupt",
            "Remote execution atomic authority dependency 损坏。",
        ) from exc
    if not (
        str(offer_row["state"]) == "delivered"
        and _execution_lineage_matches(receipt, offer, dispatch, claim, identity)
        and payload.delivery_receipt_sha256 == receipt.receipt_sha256
        and payload.dispatch_sha256 == dispatch.dispatch_sha256
        and payload.claim_receipt_sha256 == claim.receipt_sha256
        and payload.identity_sha256 == identity.identity_sha256
        and payload.suite_sha256 == dispatch.suite_sha256
        and payload.runtime_eval_request == dispatch.runtime_eval_request
        and payload.reservation_expires_at == dispatch.reservation_expires_at
        and now < _aware(claim.lease_expires_at)
        and now < _aware(dispatch.reservation_expires_at)
        and now < _aware(offer.envelope.expires_at)
    ):
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_atomic_authority_mismatch",
            "Remote execution atomic authority dependency 已变化。",
        )
    return identity


def _same_start_scope(
    left: EvolutionPostRollbackRemoteStartChallenge,
    right: EvolutionPostRollbackRemoteStartChallenge,
) -> bool:
    return bool(
        left.attempt_id == right.attempt_id
        and left.payload.delivery_receipt_sha256
        == right.payload.delivery_receipt_sha256
        and left.payload.claim_receipt_sha256 == right.payload.claim_receipt_sha256
        and left.payload.parent_permission_receipt_sha256
        == right.payload.parent_permission_receipt_sha256
        and left.payload.runtime_eval_request == right.payload.runtime_eval_request
        and left.payload.total_execution_budget_ms
        == right.payload.total_execution_budget_ms
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_start_challenges (
            challenge_id TEXT PRIMARY KEY,
            challenge_sha256 TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL,
            claim_receipt_id TEXT NOT NULL,
            parent_permission_receipt_id TEXT NOT NULL,
            challenge_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'authorized', 'expired')),
            issued_at TEXT NOT NULL,
            start_deadline TEXT NOT NULL,
            closed_by TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_evpost_remote_start_one_pending_attempt "
        "ON evolution_post_rollback_remote_start_challenges(attempt_id) "
        "WHERE state = 'pending'"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_evpost_remote_start_attempt "
        "ON evolution_post_rollback_remote_start_challenges(attempt_id, issued_at DESC)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_execution_authorizations (
            authorization_id TEXT PRIMARY KEY,
            authorization_sha256 TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL UNIQUE,
            challenge_id TEXT NOT NULL UNIQUE,
            authorization_json TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )


def render_post_rollback_remote_start_challenge(
    view: EvolutionPostRollbackRemoteExecutionAuthorizationView,
) -> str:
    item = view.challenge
    payload = item.payload
    return "\n".join(
        (
            "# 回滚后远端执行 Start Challenge",
            "",
            f"- 状态：`{view.status}`",
            f"- Attempt：`{item.attempt_id}` / {payload.attempt_number}",
            f"- Challenge：`{item.challenge_id}`",
            f"- Delivery receipt：`{payload.delivery_receipt_id}`",
            f"- Claim receipt / epoch：`{payload.claim_receipt_id}` / {payload.claim_lease_epoch}",
            (
                f"- Worker：`{payload.worker_id}` / "
                f"`{payload.worker_instance_id}` / epoch {payload.worker_epoch}"
            ),
            f"- Suite / repetitions：`{payload.suite_id}` / {payload.repetitions}",
            (
                f"- Budget：case {payload.case_execution_budget_ms}ms / "
                f"total {payload.total_execution_budget_ms}ms / "
                f"output {payload.max_result_bytes} bytes"
            ),
            f"- Start deadline：`{payload.start_deadline}`",
            f"- Signable SHA-256：`{item.signable_payload_sha256}`",
            "- 权威：未安装、未执行、未接收结果",
            "",
            "Worker canonical challenge JSON：",
            item.model_dump_json(),
        )
    )


def render_post_rollback_remote_execution_authorization(
    view: EvolutionPostRollbackRemoteExecutionAuthorizationView,
) -> str:
    if view.authorization is None:
        return render_post_rollback_remote_start_challenge(view)
    item = view.authorization
    payload = item.start_payload
    return "\n".join(
        (
            "# 回滚后远端执行授权",
            "",
            f"- 状态：`{view.status}`",
            f"- Authorization：`{item.authorization_id}`",
            f"- Attempt / Challenge：`{item.attempt_id}` / `{item.challenge_id}`",
            f"- Worker signature：verified / `{item.worker_signature_sha256}`",
            f"- Suite / repetitions：`{payload.suite_id}` / {payload.repetitions}",
            f"- Runtime lease：`{item.runtime_lease_owner_id}` / epoch {item.runtime_lease_epoch}",
            f"- Run Grant：`{item.run_grant.grant_id}` / `bash_run` only",
            f"- Execution expires：`{item.expires_at}`",
            f"- 安装 / 执行权：{view.installation_authorized} / {view.execution_authorized}",
            "- 执行状态：尚未由结果通道证明启动；无 result/learning/promotion authority",
        )
    )


def _verify_start_signature(
    identity: AuthenticatedWorkerIdentity,
    signature_base64: str,
    payload: bytes,
) -> None:
    signature = _decode_base64(
        signature_base64, expected=64, field="Remote Start signature"
    )
    public_key = Ed25519PublicKey.from_public_bytes(
        _decode_base64(
            identity.public_key_base64,
            expected=32,
            field="Worker Identity public key",
        )
    )
    try:
        public_key.verify(signature, payload)
    except InvalidSignature as exc:
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_start_signature_invalid",
            "Remote Start Worker signature 无效。",
        ) from exc


def _attempt_id(
    *,
    delivery_receipt_sha256: str,
    dispatch_sha256: str,
    attempt_number: int,
    suite_sha256: str,
) -> str:
    digest = _digest(
        {
            "domain": EVOLUTION_POST_ROLLBACK_REMOTE_START_DOMAIN,
            "delivery_receipt_sha256": delivery_receipt_sha256,
            "dispatch_sha256": dispatch_sha256,
            "attempt_number": attempt_number,
            "suite_sha256": suite_sha256,
        }
    )
    return f"evpostattempt_{digest[:24]}"


def _lease_owner(attempt_id: str, challenge_sha256: str) -> str:
    digest = hashlib.sha256(f"{attempt_id}:{challenge_sha256}".encode()).hexdigest()
    return f"evpostexec:{digest[:24]}"


def _grant_key(attempt_id: str, challenge_sha256: str) -> str:
    digest = hashlib.sha256(f"{attempt_id}:{challenge_sha256}".encode()).hexdigest()
    return f"evpostgrant:{digest[:24]}"


def _validate_challenge(
    value: EvolutionPostRollbackRemoteStartChallenge,
) -> EvolutionPostRollbackRemoteStartChallenge:
    return EvolutionPostRollbackRemoteStartChallenge.model_validate(
        value.model_dump(mode="json")
    )


def _validate_authorization(
    value: EvolutionPostRollbackRemoteExecutionAuthorization,
) -> EvolutionPostRollbackRemoteExecutionAuthorization:
    return EvolutionPostRollbackRemoteExecutionAuthorization.model_validate(
        value.model_dump(mode="json")
    )


def _restore_challenge(encoded: str) -> EvolutionPostRollbackRemoteStartChallenge:
    if len(encoded.encode("utf-8")) > _MAX_CHALLENGE_BYTES:
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_start_challenge_oversized",
            "Remote Start challenge 超过 768 KiB。",
        )
    return EvolutionPostRollbackRemoteStartChallenge.model_validate_json(encoded)


def _restore_authorization(
    encoded: str,
) -> EvolutionPostRollbackRemoteExecutionAuthorization:
    if len(encoded.encode("utf-8")) > _MAX_AUTHORIZATION_BYTES:
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_authorization_oversized",
            "Remote execution authorization 超过 896 KiB。",
        )
    return EvolutionPostRollbackRemoteExecutionAuthorization.model_validate_json(encoded)


def _challenge_id_value(value: str) -> str:
    normalized = str(value or "").strip()
    if not (
        normalized.startswith("evpoststart_")
        and len(normalized) == len("evpoststart_") + 24
        and all(ch in "0123456789abcdef" for ch in normalized.removeprefix("evpoststart_"))
    ):
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_start_challenge_id_invalid",
            "Remote Start challenge ID 无效。",
        )
    return normalized


def _attempt_id_value(value: str) -> str:
    normalized = str(value or "").strip()
    if not (
        normalized.startswith("evpostattempt_")
        and len(normalized) == len("evpostattempt_") + 24
        and all(ch in "0123456789abcdef" for ch in normalized.removeprefix("evpostattempt_"))
    ):
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_attempt_id_invalid",
            "Remote execution Attempt ID 无效。",
        )
    return normalized


def _canonical_base64(value: str, *, expected: int, field: str) -> str:
    try:
        decoded = _decode_base64(value, expected=expected, field=field)
    except (TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_base64_invalid",
            f"{field} 必须是 canonical Base64。",
        ) from exc
    normalized = base64.b64encode(decoded).decode("ascii")
    if not hmac.compare_digest(normalized, value):
        raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
            "post_rollback_remote_execution_base64_noncanonical",
            f"{field} 必须是 canonical Base64。",
        )
    return normalized


def _decode_base64(value: str, *, expected: int, field: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 canonical Base64。") from exc
    if len(decoded) != expected:
        raise ValueError(f"{field} 长度无效。")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field} 必须是 canonical Base64。")
    return decoded


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Runtime clock 必须返回 aware datetime。")
    return value.astimezone(UTC)


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
    "EVOLUTION_POST_ROLLBACK_REMOTE_EXECUTION_POLICY",
    "EVOLUTION_POST_ROLLBACK_REMOTE_START_DOMAIN",
    "EvolutionPostRollbackRemoteExecutionAuthorization",
    "EvolutionPostRollbackRemoteExecutionAuthorizationError",
    "EvolutionPostRollbackRemoteExecutionAuthorizationService",
    "EvolutionPostRollbackRemoteExecutionAuthorizationStore",
    "EvolutionPostRollbackRemoteExecutionAuthorizationView",
    "EvolutionPostRollbackRemoteRunGrantEnvelope",
    "EvolutionPostRollbackRemoteStartChallenge",
    "EvolutionPostRollbackRemoteStartPayload",
    "render_post_rollback_remote_execution_authorization",
    "render_post_rollback_remote_start_challenge",
]
