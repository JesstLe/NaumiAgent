"""Claim-bound execution authority for remote revalidation workers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantContract,
    RunDelegationGrantError,
    RunDelegationGrantRequest,
    verify_run_delegation_grant,
)
from naumi_agent.evolution.revalidation_platform_claims import (
    EvolutionRevalidationPlatformClaimReceipt,
    EvolutionRevalidationPlatformClaimService,
)
from naumi_agent.evolution.revalidation_platform_dispatches import (
    EvolutionRevalidationPlatformDispatch,
    EvolutionRevalidationPlatformDispatchStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_REVALIDATION_PLATFORM_EXECUTION_AUTHORIZATION_POLICY = (
    "evolution-revalidation-platform-execution-authorization-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
type AuthorizationStatus = Literal["current", "expired", "revoked", "stale"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationPlatformRunGrantEnvelope(_StrictModel):
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
            raise ValueError("Platform execution Run Grant 只能委托 bash_run。")
        if _aware(self.expires_at) <= _aware(self.issued_at):
            raise ValueError("Platform execution Run Grant expiry 无效。")
        if not verify_run_delegation_grant(self.as_contract()):
            raise ValueError("Platform execution Run Grant digest 无效。")
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
    def from_contract(cls, contract: RunDelegationGrantContract):
        return cls.model_validate(
            {
                "schema_version": contract.schema_version,
                "grant_id": contract.grant_id,
                "idempotency_key": contract.idempotency_key,
                "parent_receipt_id": contract.parent_receipt_id,
                "parent_receipt_sha256": contract.parent_receipt_sha256,
                "session_id": contract.session_id,
                "run_id": contract.run_id,
                "workspace_sha256": contract.workspace_sha256,
                "run_kind": contract.run_kind.value,
                "lease_owner_id": contract.lease_owner_id,
                "lease_epoch": contract.lease_epoch,
                "delegated_tool_names": list(contract.delegated_tool_names),
                "issued_at": contract.issued_at,
                "expires_at": contract.expires_at,
                "request_sha256": contract.request_sha256,
                "grant_sha256": contract.grant_sha256,
            }
        )


class EvolutionRevalidationPlatformExecutionAuthorization(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-platform-execution-authorization-v1"
    ] = EVOLUTION_REVALIDATION_PLATFORM_EXECUTION_AUTHORIZATION_POLICY
    authorization_id: str = Field(pattern=r"^evrevalexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    authorization_sequence: int = Field(ge=1, le=10_000)
    previous_authorization_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    dispatch_id: str = Field(pattern=r"^evrevalplatdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evrevalplatjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evrevalplatres_[0-9a-f]{24}$")
    claim_id: str = Field(pattern=r"^evrevalplatclaim_[0-9a-f]{24}$")
    claim_receipt_id: str = Field(pattern=r"^evrevalclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_lease_epoch: int = Field(ge=1)
    claim_lease_expires_at: str = Field(min_length=1, max_length=100)
    identity_id: str = Field(pattern=r"^evrevalworkerid_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    platform: Literal["linux", "macos", "windows"]
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    requested_samples: int = Field(ge=5, le=100)
    sample_indices: tuple[int, ...] = Field(min_length=5, max_length=100)
    phases: tuple[Literal["red", "green"], ...] = ("red", "green")
    seed: int = Field(ge=0)
    probe_registry_sha256: str = Field(pattern=_SHA256_RE)
    probe_check_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    allowed_tool_names: tuple[Literal["bash_run"], ...] = ("bash_run",)
    max_case_output_bytes: int = Field(ge=1024, le=1024**4)
    max_result_bytes: int = Field(ge=1024, le=1024**3)
    parent_permission_receipt_id: str = Field(min_length=1, max_length=128)
    parent_permission_receipt_sha256: str = Field(pattern=_SHA256_RE)
    run_grant: EvolutionRevalidationPlatformRunGrantEnvelope
    runtime_lease_owner_id: str = Field(min_length=1, max_length=128)
    runtime_lease_epoch: int = Field(ge=1)
    runtime_lease_expires_at: str = Field(min_length=1, max_length=100)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    control_plane_attestation_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    control_plane_attestation_sha256: str = Field(pattern=_SHA256_RE)
    state: Literal["authorized"] = "authorized"
    execution_started: Literal[False] = False
    result_received: Literal[False] = False
    cohort_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Platform execution workspace 必须 canonical。")
        if (self.authorization_sequence == 1) is not (
            not self.previous_authorization_sha256
        ):
            raise ValueError("Platform execution authorization chain 无效。")
        if self.sample_indices != tuple(range(self.requested_samples)):
            raise ValueError("Platform execution sample indices 必须是完整连续范围。")
        if self.phases != ("red", "green") or self.allowed_tool_names != ("bash_run",):
            raise ValueError("Platform execution phase/tool scope 无效。")
        if self.probe_check_sha256 != tuple(sorted(set(self.probe_check_sha256))):
            raise ValueError("Platform execution probe check digests 必须排序且唯一。")
        expected_result_limit = min(
            self.max_case_output_bytes * 2 * self.requested_samples,
            1024**3,
        )
        if self.max_result_bytes != expected_result_limit:
            raise ValueError("Platform execution aggregate result limit 投影不一致。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        boundaries = (
            _aware(self.claim_lease_expires_at),
            _aware(self.run_grant.expires_at),
            _aware(self.runtime_lease_expires_at),
        )
        if expires <= issued or expires != min(boundaries):
            raise ValueError("Platform execution expiry 未绑定 grant/lease 最短边界。")
        if not (
            self.run_grant.parent_receipt_id == self.parent_permission_receipt_id
            and self.run_grant.parent_receipt_sha256
            == self.parent_permission_receipt_sha256
            and self.run_grant.lease_owner_id == self.runtime_lease_owner_id
            and self.run_grant.lease_epoch == self.runtime_lease_epoch
        ):
            raise ValueError("Platform execution Run Grant authority 投影不一致。")
        core = self.model_dump(
            mode="json",
            exclude={
                "authorization_id",
                "authorization_sha256",
                "control_plane_attestation_sha256",
            },
        )
        digest = _digest(core)
        if self.authorization_sha256 != digest:
            raise ValueError("Platform execution authorization digest 不一致。")
        if self.authorization_id != f"evrevalexecauth_{digest[:24]}":
            raise ValueError("Platform execution authorization id 不一致。")
        return self


class EvolutionRevalidationPlatformExecutionRevocation(_StrictModel):
    schema_version: Literal[1] = 1
    revocation_id: str = Field(pattern=r"^evrevalexecrevoke_[0-9a-f]{24}$")
    revocation_sha256: str = Field(pattern=_SHA256_RE)
    authorization_id: str = Field(pattern=r"^evrevalexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    revoked_at: str = Field(min_length=1, max_length=100)
    run_grant_revoked: Literal[True] = True
    runtime_lease_released: bool
    result_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.revoked_at)
        core = self.model_dump(mode="json", exclude={"revocation_id", "revocation_sha256"})
        digest = _digest(core)
        if self.revocation_sha256 != digest:
            raise ValueError("Platform execution revocation digest 不一致。")
        if self.revocation_id != f"evrevalexecrevoke_{digest[:24]}":
            raise ValueError("Platform execution revocation id 不一致。")
        return self


class EvolutionRevalidationPlatformExecutionAuthorizationView(_StrictModel):
    authorization: EvolutionRevalidationPlatformExecutionAuthorization
    revocation: EvolutionRevalidationPlatformExecutionRevocation | None = None
    status: AuthorizationStatus
    execution_authorized: bool
    result_submission_authorized: bool
    execution_started: Literal[False] = False
    result_received: Literal[False] = False
    cohort_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = self.status == "current"
        if self.execution_authorized is not current:
            raise ValueError("Platform execution authorization view 投影不一致。")
        if self.result_submission_authorized is not current:
            raise ValueError("Platform result submission view 投影不一致。")
        if (self.revocation is not None) is not (self.status == "revoked"):
            raise ValueError("Platform execution revocation view 投影不一致。")
        return self


class EvolutionRevalidationPlatformExecutionAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPlatformExecutionAuthorizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(self, authorization_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT authorization_json FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE authorization_id = ?",
                    (authorization_id,),
                )
            ).fetchone()
        return None if row is None else _authorization(row["authorization_json"])

    async def get_by_claim(self, claim_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT authorization_json FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE claim_id = ? ORDER BY authorization_sequence DESC LIMIT 1",
                    (claim_id,),
                )
            ).fetchone()
        return None if row is None else _authorization(row["authorization_json"])

    async def get_by_claim_receipt(self, claim_receipt_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT authorization_json FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE claim_receipt_id = ? ORDER BY authorization_sequence DESC LIMIT 1",
                    (claim_receipt_id,),
                )
            ).fetchone()
        return None if row is None else _authorization(row["authorization_json"])

    async def get_revocation(self, authorization_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT revocation_json FROM "
                    "evolution_revalidation_platform_execution_revocations "
                    "WHERE authorization_id = ?",
                    (authorization_id,),
                )
            ).fetchone()
        return None if row is None else _revocation(row["revocation_json"])

    async def record(self, authorization):
        item = EvolutionRevalidationPlatformExecutionAuthorization.model_validate_json(
            authorization.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                "platform_execution_authorization_oversized",
                "Platform execution authorization 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            contract = await (
                await db.execute(
                    "SELECT contract_sha256 FROM evolution_revalidation_runtime_contracts "
                    "WHERE contract_id = ?",
                    (item.contract_id,),
                )
            ).fetchone()
            dispatch = await (
                await db.execute(
                    "SELECT dispatch_sha256 FROM evolution_revalidation_platform_dispatches "
                    "WHERE dispatch_id = ?",
                    (item.dispatch_id,),
                )
            ).fetchone()
            claim = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_claim_receipts "
                    "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
                    (item.claim_id,),
                )
            ).fetchone()
            if not (
                contract is not None
                and contract["contract_sha256"] == item.contract_sha256
                and dispatch is not None
                and dispatch["dispatch_sha256"] == item.dispatch_sha256
                and claim is not None
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_atomic_authority_mismatch",
                    "Platform execution authority 在持久化时已变化。",
                )
            claim_item = EvolutionRevalidationPlatformClaimReceipt.model_validate_json(
                str(claim["receipt_json"])
            )
            if not (
                claim_item.receipt_id == item.claim_receipt_id
                and claim_item.receipt_sha256 == item.claim_receipt_sha256
                and claim_item.lease_epoch == item.claim_lease_epoch
                and claim_item.lease_expires_at == item.claim_lease_expires_at
                and claim_item.identity_id == item.identity_id
                and claim_item.identity_sha256 == item.identity_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_claim_advanced",
                    "Platform Claim lease 已在 authorization 持久化前推进。",
                )
            existing = await (
                await db.execute(
                    "SELECT authorization_json FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE claim_receipt_id = ? AND authorization_sequence = ?",
                    (item.claim_receipt_id, item.authorization_sequence),
                )
            ).fetchone()
            if existing is not None:
                restored = _authorization(existing["authorization_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                        "platform_execution_authorization_conflict",
                        "同一 Platform Claim 已绑定不同 execution authorization。",
                    )
                return restored
            latest_row = await (
                await db.execute(
                    "SELECT authorization_json FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE claim_id = ? ORDER BY authorization_sequence DESC LIMIT 1",
                    (item.claim_id,),
                )
            ).fetchone()
            latest = (
                None
                if latest_row is None
                else _authorization(latest_row["authorization_json"])
            )
            if latest is None:
                chain_valid = (
                    item.authorization_sequence == 1
                    and not item.previous_authorization_sha256
                )
            else:
                revoked = await (
                    await db.execute(
                        "SELECT 1 FROM "
                        "evolution_revalidation_platform_execution_revocations "
                        "WHERE authorization_id = ?",
                        (latest.authorization_id,),
                    )
                ).fetchone()
                chain_valid = bool(
                    revoked is not None
                    and item.authorization_sequence
                    == latest.authorization_sequence + 1
                    and item.previous_authorization_sha256
                    == latest.authorization_sha256
                )
            if not chain_valid:
                await db.rollback()
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_authorization_sequence_conflict",
                    "Platform execution authorization generation 不连续。",
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_execution_authorizations "
                "(authorization_id, authorization_sha256, authorization_sequence, "
                "contract_id, dispatch_id, claim_id, claim_receipt_id, run_grant_id, "
                "runtime_lease_owner_id, runtime_lease_epoch, "
                "authorization_json, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.authorization_id,
                    item.authorization_sha256,
                    item.authorization_sequence,
                    item.contract_id,
                    item.dispatch_id,
                    item.claim_id,
                    item.claim_receipt_id,
                    item.run_grant.grant_id,
                    item.runtime_lease_owner_id,
                    item.runtime_lease_epoch,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return item

    async def record_revocation(self, revocation):
        item = EvolutionRevalidationPlatformExecutionRevocation.model_validate_json(
            revocation.model_dump_json()
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            authority = await (
                await db.execute(
                    "SELECT authorization_sha256 FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE authorization_id = ?",
                    (item.authorization_id,),
                )
            ).fetchone()
            if authority is None or authority["authorization_sha256"] != (
                item.authorization_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_revocation_authority_mismatch",
                    "Platform execution revocation 未绑定 exact authorization。",
                )
            existing = await (
                await db.execute(
                    "SELECT revocation_json FROM "
                    "evolution_revalidation_platform_execution_revocations "
                    "WHERE authorization_id = ?",
                    (item.authorization_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _revocation(existing["revocation_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                        "platform_execution_revocation_conflict",
                        "Platform execution authorization 已由不同事实撤销。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_execution_revocations "
                "(revocation_id, revocation_sha256, authorization_id, reason_code, "
                "revocation_json, revoked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.revocation_id,
                    item.revocation_sha256,
                    item.authorization_id,
                    item.reason_code,
                    item.model_dump_json(),
                    item.revoked_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationPlatformExecutionAuthorizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_service: EvolutionRevalidationRuntimeContractService,
        dispatch_store: EvolutionRevalidationPlatformDispatchStore,
        claim_service: EvolutionRevalidationPlatformClaimService,
        permission_store: PermissionDecisionReceiptStore,
        harness_store: HarnessStore,
        run_grant_authority: RunDelegationGrantAuthority,
        store: EvolutionRevalidationPlatformExecutionAuthorizationStore,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service = contract_service
        self.dispatch_store = dispatch_store
        self.claim_service = claim_service
        self.permission_store = permission_store
        self.harness_store = harness_store
        self.run_grant_authority = run_grant_authority
        self.store = store
        self._control_plane_key_provider = control_plane_key_provider
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue(
        self,
        *,
        claim_id: str,
        parent_permission_receipt_id: str,
        issued_at: str | None = None,
        ttl_seconds: int = 300,
    ) -> EvolutionRevalidationPlatformExecutionAuthorizationView:
        lock = self._locks.setdefault(f"issue:{claim_id}", asyncio.Lock())
        async with lock:
            now = _aware(issued_at or datetime.now(UTC).isoformat())
            claim = await self.claim_service.inspect(
                claim_id=claim_id, assessed_at=now.isoformat()
            )
            if claim.status != "current":
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_claim_not_current",
                    "Platform Claim 当前不可用于派生 execution authority。",
                )
            existing = await self.store.get_by_claim(claim_id)
            if existing is not None:
                view = await self.inspect(
                    authorization_id=existing.authorization_id,
                    assessed_at=now.isoformat(),
                )
                if (
                    view.status == "current"
                    and existing.claim_receipt_id == claim.receipt.receipt_id
                ):
                    return view
                if view.revocation is not None and view.revocation.reason_code not in {
                    "authorization_superseded",
                    "claim_lease_superseded",
                }:
                    raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                        "platform_execution_reauthorization_blocked",
                        "显式撤销的 execution authorization 不能自动重新签发。",
                    )
                if view.revocation is None:
                    await self._supersede(
                        existing,
                        reason_code=(
                            "claim_lease_superseded"
                            if existing.claim_receipt_id != claim.receipt.receipt_id
                            else "authorization_superseded"
                        ),
                        superseded_at=now,
                    )
            dispatch = await self._dispatch(claim.receipt.dispatch_id)
            contract_view = await self.contract_service.inspect(
                workspace_root=self.workspace_root,
                contract_id=dispatch.contract_id,
            )
            if not (
                contract_view.execution_eligible
                and contract_view.contract.contract_sha256 == dispatch.contract_sha256
            ):
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_contract_not_ready",
                    "Runtime Contract 当前不可执行。",
                )
            parent = await self.permission_store.get(parent_permission_receipt_id)
            if not (
                parent is not None
                and parent.authorizes_execution
                and parent.run_id
                and "bash_run" in parent.delegated_tool_names
            ):
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_parent_permission_invalid",
                    "远端执行缺少可委托 bash_run 的父权限回执。",
                )
            remaining = math.floor(
                (_aware(claim.receipt.lease_expires_at) - now).total_seconds()
            )
            if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
                raise TypeError("ttl_seconds 必须是整数。")
            if not 1 <= ttl_seconds <= 3_600:
                raise ValueError("ttl_seconds 必须在 1..3600。")
            duration = min(
                ttl_seconds,
                remaining,
                contract_view.contract.max_total_duration_seconds,
            )
            if duration < 1:
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_window_too_short",
                    "Platform Claim 剩余 lease 不足以签发执行权限。",
                )
            sequence = 1 if existing is None else existing.authorization_sequence + 1
            owner = _lease_owner(
                dispatch.dispatch_id,
                claim.receipt.receipt_sha256,
                sequence,
            )
            lease = await self.harness_store.acquire_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=parent.run_id,
                owner_id=owner,
                now=now.isoformat(),
                lease_seconds=duration,
            )
            if lease is None:
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_runtime_lease_unavailable",
                    "无法取得远端执行所需 Runtime lease。",
                )
            grant = None
            try:
                grant = await self.run_grant_authority.issue(
                    RunDelegationGrantRequest(
                        idempotency_key=_grant_key(
                            dispatch.dispatch_id,
                            claim.receipt.receipt_sha256,
                            sequence,
                        ),
                        parent_receipt_id=parent.receipt_id,
                        run_kind=HarnessRunKind.RUNTIME,
                        lease_owner_id=owner,
                        lease_epoch=lease.epoch,
                        delegated_tool_names=("bash_run",),
                    ),
                    now=now.isoformat(),
                    ttl_seconds=duration,
                )
                registration = await self.claim_service.worker_registry.get_active(
                    dispatch.worker_id
                )
                if not (
                    registration is not None
                    and registration.contract.instance_id
                    == dispatch.worker_instance_id
                    and registration.contract.epoch == dispatch.worker_epoch
                    and registration.contract.contract_sha256
                    == dispatch.worker_contract_sha256
                ):
                    raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                        "platform_execution_worker_fenced",
                        "Worker incarnation 在 execution authorization 签发前已被 fencing。",
                    )
                item = self._build(
                    dispatch=dispatch,
                    contract=contract_view.contract,
                    claim=claim.receipt,
                    parent=parent,
                    lease=lease,
                    grant=grant.contract,
                    max_result_bytes=min(
                        registration.contract.resources.max_output_bytes
                        * 2
                        * contract_view.contract.requested_samples,
                        1024**3,
                    ),
                    max_case_output_bytes=(
                        registration.contract.resources.max_output_bytes
                    ),
                    authorization_sequence=sequence,
                    previous_authorization_sha256=(
                        "" if existing is None else existing.authorization_sha256
                    ),
                    issued_at=now,
                )
                stored = await self.store.record(item)
                view = await self.inspect(
                    authorization_id=stored.authorization_id,
                    assessed_at=now.isoformat(),
                )
                if view.status != "current":
                    raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                        "platform_execution_fenced_during_commit",
                        "Execution authority 提交期间已被 fencing。",
                    )
                return view
            except BaseException:
                if grant is not None:
                    await self._best_effort_cleanup(
                        grant_id=grant.contract.grant_id,
                        run_id=parent.run_id,
                        owner_id=owner,
                        lease_epoch=lease.epoch,
                        now=now,
                    )
                else:
                    await self._best_effort_release(
                        run_id=parent.run_id,
                        owner_id=owner,
                        lease_epoch=lease.epoch,
                        now=now,
                    )
                raise

    async def inspect(
        self, *, authorization_id: str, assessed_at: str | None = None
    ) -> EvolutionRevalidationPlatformExecutionAuthorizationView:
        now = _aware(assessed_at or datetime.now(UTC).isoformat())
        item = await self.store.get(authorization_id)
        if item is None:
            raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                "platform_execution_authorization_missing",
                "Platform execution authorization 不存在。",
            )
        revocation = await self.store.get_revocation(item.authorization_id)
        if revocation is not None:
            status: AuthorizationStatus = "revoked"
        elif now >= _aware(item.expires_at):
            status = "expired"
        else:
            status = await self._dynamic_status(item, now=now)
        current = status == "current"
        return EvolutionRevalidationPlatformExecutionAuthorizationView(
            authorization=item,
            revocation=revocation,
            status=status,
            execution_authorized=current,
            result_submission_authorized=current,
        )

    async def revoke(
        self,
        *,
        authorization_id: str,
        reason_code: str,
        revoked_at: str | None = None,
    ) -> EvolutionRevalidationPlatformExecutionAuthorizationView:
        lock = self._locks.setdefault(f"revoke:{authorization_id}", asyncio.Lock())
        async with lock:
            now = _aware(revoked_at or datetime.now(UTC).isoformat())
            item = await self.store.get(authorization_id)
            if item is None:
                raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                    "platform_execution_authorization_missing",
                    "Platform execution authorization 不存在。",
                )
            existing = await self.store.get_revocation(authorization_id)
            if existing is not None:
                if existing.reason_code != reason_code:
                    raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                        "platform_execution_revocation_conflict",
                        "Platform execution authorization 已由不同原因撤销。",
                    )
                return await self.inspect(
                    authorization_id=authorization_id, assessed_at=now.isoformat()
                )
            released = await self._revoke_dependencies(
                item, reason_code=reason_code, revoked_at=now
            )
            revocation = _build_revocation(
                item,
                reason_code=reason_code,
                revoked_at=now,
                runtime_lease_released=released,
            )
            await self.store.record_revocation(revocation)
            return await self.inspect(
                authorization_id=authorization_id, assessed_at=now.isoformat()
            )

    async def _dynamic_status(self, item, *, now):
        try:
            self._verify_attestation(item)
            claim = await self.claim_service.inspect(
                claim_id=item.claim_id, assessed_at=now.isoformat()
            )
            contract = await self.contract_service.inspect(
                workspace_root=self.workspace_root, contract_id=item.contract_id
            )
            grant = await self.run_grant_authority.validate(
                grant_id=item.run_grant.grant_id, now=now.isoformat()
            )
            parent = await self.permission_store.get(item.parent_permission_receipt_id)
            if not (
                claim.status == "current"
                and claim.receipt.receipt_id == item.claim_receipt_id
                and claim.receipt.receipt_sha256 == item.claim_receipt_sha256
                and claim.receipt.lease_epoch == item.claim_lease_epoch
                and contract.execution_eligible
                and contract.contract.contract_sha256 == item.contract_sha256
                and grant.allowed
                and grant.contract == item.run_grant.as_contract()
                and parent is not None
                and parent.authorizes_execution
                and parent.receipt_sha256 == item.parent_permission_receipt_sha256
            ):
                return "stale"
        except (
            EvolutionRevalidationPlatformExecutionAuthorizationError,
            RunDelegationGrantError,
            HarnessStoreError,
            OSError,
            TypeError,
            ValueError,
        ):
            return "stale"
        return "current"

    def _build(
        self,
        *,
        dispatch,
        contract,
        claim,
        parent,
        lease,
        grant,
        max_case_output_bytes,
        max_result_bytes,
        authorization_sequence,
        previous_authorization_sha256,
        issued_at,
    ):
        key = self._control_plane_key()
        probe_digests = tuple(
            sorted(_digest(item.model_dump(mode="json")) for item in contract.probe_checks)
        )
        run_grant = EvolutionRevalidationPlatformRunGrantEnvelope.from_contract(grant)
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_PLATFORM_EXECUTION_AUTHORIZATION_POLICY,
            "authorization_sequence": authorization_sequence,
            "previous_authorization_sha256": previous_authorization_sha256,
            "workspace_root": str(self.workspace_root),
            "contract_id": contract.contract_id,
            "contract_sha256": contract.contract_sha256,
            "validation_plan_id": contract.validation_plan_id,
            "validation_plan_sha256": contract.validation_plan_sha256,
            "source_snapshot_id": contract.source_snapshot_id,
            "source_snapshot_sha256": contract.source_snapshot_sha256,
            "dispatch_id": dispatch.dispatch_id,
            "dispatch_sha256": dispatch.dispatch_sha256,
            "job_id": dispatch.job_id,
            "reservation_id": dispatch.reservation_id,
            "claim_id": claim.claim_id,
            "claim_receipt_id": claim.receipt_id,
            "claim_receipt_sha256": claim.receipt_sha256,
            "claim_lease_epoch": claim.lease_epoch,
            "claim_lease_expires_at": claim.lease_expires_at,
            "identity_id": claim.identity_id,
            "identity_sha256": claim.identity_sha256,
            "worker_id": dispatch.worker_id,
            "worker_instance_id": dispatch.worker_instance_id,
            "worker_epoch": dispatch.worker_epoch,
            "worker_contract_sha256": dispatch.worker_contract_sha256,
            "platform": dispatch.platform,
            "suite_id": contract.suite_id,
            "requested_samples": contract.requested_samples,
            "sample_indices": list(range(contract.requested_samples)),
            "phases": ["red", "green"],
            "seed": contract.seed,
            "probe_registry_sha256": contract.probe_registry_sha256,
            "probe_check_sha256": list(probe_digests),
            "allowed_tool_names": ["bash_run"],
            "max_case_output_bytes": max_case_output_bytes,
            "max_result_bytes": max_result_bytes,
            "parent_permission_receipt_id": parent.receipt_id,
            "parent_permission_receipt_sha256": parent.receipt_sha256,
            "run_grant": run_grant.model_dump(mode="json"),
            "runtime_lease_owner_id": lease.owner_id,
            "runtime_lease_epoch": lease.epoch,
            "runtime_lease_expires_at": lease.expires_at,
            "issued_at": issued_at.isoformat(),
            "expires_at": min(
                _aware(claim.lease_expires_at),
                _aware(run_grant.expires_at),
                _aware(lease.expires_at),
            ).isoformat(),
            "control_plane_attestation_algorithm": "hmac-sha256",
            "state": "authorized",
            "execution_started": False,
            "result_received": False,
            "cohort_authority": False,
            "comparison_authority": False,
            "promotion_authority": False,
        }
        digest = _digest(core)
        attestation = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
        return EvolutionRevalidationPlatformExecutionAuthorization.model_validate(
            {
                **core,
                "authorization_id": f"evrevalexecauth_{digest[:24]}",
                "authorization_sha256": digest,
                "control_plane_attestation_sha256": attestation,
            }
        )

    def _verify_attestation(self, item):
        core = item.model_dump(
            mode="json",
            exclude={
                "authorization_id",
                "authorization_sha256",
                "control_plane_attestation_sha256",
            },
        )
        expected = hmac.new(
            self._control_plane_key(), _canonical(core), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, item.control_plane_attestation_sha256):
            raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                "platform_execution_attestation_invalid",
                "Platform execution control-plane attestation 无效。",
            )

    def _control_plane_key(self):
        key = self._control_plane_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                "platform_execution_control_plane_key_invalid",
                "Platform execution control-plane key 不可用。",
            )
        return key

    async def _dispatch(self, dispatch_id):
        if not self.dispatch_store.db_path.is_file():
            raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                "platform_execution_dispatch_missing", "Platform Dispatch 不存在。"
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
            raise EvolutionRevalidationPlatformExecutionAuthorizationError(
                "platform_execution_dispatch_missing", "Platform Dispatch 不存在。"
            )
        return EvolutionRevalidationPlatformDispatch.model_validate_json(
            row["dispatch_json"]
        )

    async def _revoke_dependencies(self, item, *, reason_code, revoked_at):
        await self.run_grant_authority.revoke(
            grant_id=item.run_grant.grant_id,
            reason=reason_code,
            revoked_at=revoked_at.isoformat(),
        )
        return await self._best_effort_release(
            run_id=item.run_grant.run_id,
            owner_id=item.runtime_lease_owner_id,
            lease_epoch=item.runtime_lease_epoch,
            now=revoked_at,
        )

    async def _supersede(self, item, *, reason_code, superseded_at):
        released = await self._revoke_dependencies(
            item,
            reason_code=reason_code,
            revoked_at=superseded_at,
        )
        await self.store.record_revocation(
            _build_revocation(
                item,
                reason_code=reason_code,
                revoked_at=superseded_at,
                runtime_lease_released=released,
            )
        )

    async def _best_effort_cleanup(self, *, grant_id, run_id, owner_id, lease_epoch, now):
        try:
            await self.run_grant_authority.revoke(
                grant_id=grant_id,
                reason="platform_execution_issue_failed",
                revoked_at=now.isoformat(),
            )
        except (RunDelegationGrantError, OSError, TypeError, ValueError):
            pass
        finally:
            await self._best_effort_release(
                run_id=run_id,
                owner_id=owner_id,
                lease_epoch=lease_epoch,
                now=now,
            )

    async def _best_effort_release(self, *, run_id, owner_id, lease_epoch, now):
        try:
            await self.harness_store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run_id,
                owner_id=owner_id,
                epoch=lease_epoch,
                now=now.isoformat(),
            )
            authoritative = await self.harness_store.get_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run_id,
            )
            return bool(
                authoritative is not None
                and authoritative.epoch == lease_epoch
                and authoritative.owner_id == owner_id
                and authoritative.state is HarnessRunLeaseState.RELEASED
            )
        except (HarnessStoreError, OSError, TypeError, ValueError):
            return False


def _build_revocation(item, *, reason_code, revoked_at, runtime_lease_released):
    core = {
        "schema_version": 1,
        "authorization_id": item.authorization_id,
        "authorization_sha256": item.authorization_sha256,
        "reason_code": reason_code,
        "revoked_at": revoked_at.isoformat(),
        "run_grant_revoked": True,
        "runtime_lease_released": runtime_lease_released,
        "result_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationPlatformExecutionRevocation.model_validate(
        {
            **core,
            "revocation_id": f"evrevalexecrevoke_{digest[:24]}",
            "revocation_sha256": digest,
        }
    )


def _lease_owner(dispatch_id, claim_receipt_sha256, sequence):
    digest = hashlib.sha256(
        f"{dispatch_id}:{claim_receipt_sha256}:{sequence}".encode()
    ).hexdigest()
    return f"evreval-exec-{digest[:24]}"


def _grant_key(dispatch_id, claim_receipt_sha256, sequence):
    digest = hashlib.sha256(
        f"grant:{dispatch_id}:{claim_receipt_sha256}:{sequence}".encode()
    ).hexdigest()
    return f"evreval-exec-grant-{digest[:24]}"


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Platform execution 时间必须包含 offset。")
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


def _authorization(value):
    return EvolutionRevalidationPlatformExecutionAuthorization.model_validate_json(value)


def _revocation(value):
    return EvolutionRevalidationPlatformExecutionRevocation.model_validate_json(value)


async def _ensure_schema(db):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_execution_authorizations (
            authorization_id TEXT PRIMARY KEY,
            authorization_sha256 TEXT NOT NULL UNIQUE,
            authorization_sequence INTEGER NOT NULL,
            contract_id TEXT NOT NULL,
            dispatch_id TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            claim_receipt_id TEXT NOT NULL,
            run_grant_id TEXT NOT NULL UNIQUE,
            runtime_lease_owner_id TEXT NOT NULL,
            runtime_lease_epoch INTEGER NOT NULL,
            authorization_json TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(claim_id, authorization_sequence)
        );
        CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_execution_revocations (
            revocation_id TEXT PRIMARY KEY,
            revocation_sha256 TEXT NOT NULL UNIQUE,
            authorization_id TEXT NOT NULL UNIQUE,
            reason_code TEXT NOT NULL,
            revocation_json TEXT NOT NULL,
            revoked_at TEXT NOT NULL
        );
        """
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PLATFORM_EXECUTION_AUTHORIZATION_POLICY",
    "EvolutionRevalidationPlatformExecutionAuthorization",
    "EvolutionRevalidationPlatformExecutionAuthorizationError",
    "EvolutionRevalidationPlatformExecutionAuthorizationService",
    "EvolutionRevalidationPlatformExecutionAuthorizationStore",
    "EvolutionRevalidationPlatformExecutionAuthorizationView",
    "EvolutionRevalidationPlatformExecutionRevocation",
    "EvolutionRevalidationPlatformRunGrantEnvelope",
]
