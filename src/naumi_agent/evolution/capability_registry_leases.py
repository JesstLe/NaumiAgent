"""Catalog-only, revocable Registry leases for verified Capability candidates."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionReceipt,
    permission_arguments_sha256,
)
from naumi_agent.evolution.capability_artifact import EvolutionCapabilityArtifactService
from naumi_agent.evolution.capability_sandbox_execution import (
    EvolutionCapabilitySandboxExecutionService,
)
from naumi_agent.tools.base import ToolRegistry, ToolRegistryConflictError

_POLICY_VERSION = "evolution-capability-registry-lease-v1"
RegistryLeaseState = Literal["active", "released", "revoked", "expired"]
RegistryLeaseReason = Literal[
    "acquired",
    "user_released",
    "lease_expired",
    "request_revoked",
    "artifact_revoked",
    "execution_receipt_revoked",
    "registry_reservation_lost",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionCapabilityRegistryLease(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-registry-lease-v1"] = (
        _POLICY_VERSION
    )
    lease_id: str = Field(pattern=r"^evcrl_[0-9a-f]{24}$")
    lease_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^evcsr_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_receipt_id: str = Field(pattern=r"^evcser_[0-9a-f]{24}$")
    execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^evcia_[0-9a-f]{24}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temporary_tool_name: str = Field(
        pattern=r"^evolution_sandbox:[0-9a-f]{12}:[a-z][a-z0-9_.:-]{0,127}$"
    )
    runtime_instance_id: str = Field(pattern=r"^evcruntime_[0-9a-f]{24}$")
    parent_permission_receipt_id: str = Field(min_length=1, max_length=128)
    parent_permission_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_seconds: int = Field(ge=30, le=900)
    issued_at: str = Field(min_length=20, max_length=64)
    expires_at: str = Field(min_length=20, max_length=64)
    catalog_only: Literal[True] = True
    registry_reserved: Literal[True] = True
    model_visible: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _lease_is_content_addressed(self) -> EvolutionCapabilityRegistryLease:
        issued = _aware(self.issued_at, field="issued_at")
        expires = _aware(self.expires_at, field="expires_at")
        if expires != issued + timedelta(seconds=self.duration_seconds):
            raise ValueError("Capability Registry lease 到期时间与 duration 不一致。")
        payload = self.model_dump(mode="json", exclude={"lease_id", "lease_sha256"})
        digest = _digest(payload)
        if not hmac.compare_digest(self.lease_sha256, digest):
            raise ValueError("Capability Registry lease 摘要不一致。")
        if self.lease_id != f"evcrl_{digest[:24]}":
            raise ValueError("Capability Registry lease identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class EvolutionCapabilityRegistryLeaseStateReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-registry-lease-v1"] = (
        _POLICY_VERSION
    )
    receipt_id: str = Field(pattern=r"^evcrlsr_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_id: str = Field(pattern=r"^evcrl_[0-9a-f]{24}$")
    lease_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    previous_receipt_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    state: RegistryLeaseState
    reason: RegistryLeaseReason
    observed_at: str = Field(min_length=20, max_length=64)
    request_current: bool
    artifact_current: bool
    execution_receipt_current: bool
    runtime_owner_current: bool
    local_registry_reservation_present: bool
    registry_reservation_release_confirmed: bool
    model_visible: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _state_receipt_is_exact(self) -> EvolutionCapabilityRegistryLeaseStateReceipt:
        _aware(self.observed_at, field="observed_at")
        if self.sequence == 1:
            if (
                self.previous_receipt_sha256
                or self.state != "active"
                or self.reason != "acquired"
            ):
                raise ValueError(
                    "Capability Registry 初始 receipt 必须是无前序的 active/acquired。"
                )
            if not all((
                self.request_current,
                self.artifact_current,
                self.execution_receipt_current,
                self.runtime_owner_current,
                self.local_registry_reservation_present,
            )):
                raise ValueError("Capability Registry 初始 receipt 的 authority 必须完整。")
            if self.registry_reservation_release_confirmed:
                raise ValueError("Capability Registry 初始 receipt 不得声称已释放 reservation。")
        elif not self.previous_receipt_sha256 or self.state == "active":
            raise ValueError("Capability Registry 后续 receipt 必须有前序且为终态。")
        if (
            self.registry_reservation_release_confirmed
            and self.local_registry_reservation_present
        ):
            raise ValueError("Capability Registry reservation 不能同时存在且已确认释放。")
        if self.reason == "user_released" and not (
            self.state == "released"
            and self.runtime_owner_current
            and self.registry_reservation_release_confirmed
        ):
            raise ValueError("用户释放回执必须证明 Runtime ownership 与 reservation 释放。")
        payload = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(payload)
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Capability Registry state receipt 摘要不一致。")
        if self.receipt_id != f"evcrlsr_{digest[:24]}":
            raise ValueError("Capability Registry state receipt identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class CapabilityRegistryLeaseView(_StrictModel):
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    lease: EvolutionCapabilityRegistryLease | None
    state_receipt: EvolutionCapabilityRegistryLeaseStateReceipt | None
    state: Literal["missing", "active", "detached", "released", "revoked", "expired"]
    locally_reserved: bool
    model_visible: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False


class CapabilityRegistryLeaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionCapabilityRegistryLeaseStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def latest(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> tuple[
        EvolutionCapabilityRegistryLease,
        EvolutionCapabilityRegistryLeaseStateReceipt,
    ] | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT current_state, lease_json, lease_payload_sha256, "
                        "state_json, state_payload_sha256 "
                        "FROM evolution_capability_registry_leases "
                        "WHERE workspace_root = ? AND candidate_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (workspace, candidate_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityRegistryLeaseError(
                "registry_lease_store_read_failed",
                "无法读取 Capability Registry lease。",
            ) from exc
        return None if row is None else _restore_pair(row)

    async def issue(
        self,
        workspace_root: str | Path,
        lease: EvolutionCapabilityRegistryLease,
        state: EvolutionCapabilityRegistryLeaseStateReceipt,
    ) -> tuple[EvolutionCapabilityRegistryLease, EvolutionCapabilityRegistryLeaseStateReceipt]:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        lease_json = lease.canonical_json()
        state_json = state.canonical_json()
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                active = await (
                    await db.execute(
                        "SELECT candidate_id, temporary_tool_name "
                        "FROM evolution_capability_registry_leases "
                        "WHERE workspace_root = ? AND current_state = 'active' "
                        "AND (candidate_id = ? OR temporary_tool_name = ?) LIMIT 1",
                        (workspace, lease.candidate_id, lease.temporary_tool_name),
                    )
                ).fetchone()
                if active is not None:
                    await db.rollback()
                    conflict = (
                        "Candidate 已存在 active Registry lease。"
                        if str(active[0]) == lease.candidate_id
                        else "临时工具名已被 active Registry lease 占用。"
                    )
                    raise CapabilityRegistryLeaseError(
                        "registry_lease_active_conflict",
                        conflict,
                    )
                await db.execute(
                    "INSERT INTO evolution_capability_registry_leases "
                    "(workspace_root, lease_id, candidate_id, temporary_tool_name, "
                    "runtime_instance_id, current_state, lease_json, lease_payload_sha256, "
                    "state_json, state_payload_sha256, issued_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        lease.lease_id,
                        lease.candidate_id,
                        lease.temporary_tool_name,
                        lease.runtime_instance_id,
                        lease_json,
                        hashlib.sha256(lease_json.encode()).hexdigest(),
                        state_json,
                        hashlib.sha256(state_json.encode()).hexdigest(),
                        lease.issued_at,
                        state.observed_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO evolution_capability_registry_lease_state_receipts "
                    "(workspace_root, lease_id, sequence, receipt_id, payload_json, "
                    "payload_sha256, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        lease.lease_id,
                        state.sequence,
                        state.receipt_id,
                        state_json,
                        hashlib.sha256(state_json.encode()).hexdigest(),
                        state.observed_at,
                    ),
                )
                await db.commit()
        except CapabilityRegistryLeaseError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityRegistryLeaseError(
                "registry_lease_store_write_failed",
                "无法保存 Capability Registry lease。",
            ) from exc
        return lease, state

    async def transition(
        self,
        workspace_root: str | Path,
        *,
        lease: EvolutionCapabilityRegistryLease,
        previous: EvolutionCapabilityRegistryLeaseStateReceipt,
        state: RegistryLeaseState,
        reason: RegistryLeaseReason,
        observed_at: str,
        request_current: bool,
        artifact_current: bool,
        execution_receipt_current: bool,
        runtime_owner_current: bool,
        registry_reservation_current: bool,
        registry_reservation_release_confirmed: bool,
    ) -> EvolutionCapabilityRegistryLeaseStateReceipt:
        if state == "active":
            raise ValueError("Capability Registry transition 只能进入终态。")
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        next_receipt = _state_receipt(
            lease=lease,
            sequence=previous.sequence + 1,
            previous_receipt_sha256=previous.receipt_sha256,
            state=state,
            reason=reason,
            observed_at=observed_at,
            request_current=request_current,
            artifact_current=artifact_current,
            execution_receipt_current=execution_receipt_current,
            runtime_owner_current=runtime_owner_current,
            registry_reservation_current=registry_reservation_current,
            registry_reservation_release_confirmed=(
                registry_reservation_release_confirmed
            ),
        )
        payload = next_receipt.canonical_json()
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE evolution_capability_registry_leases "
                    "SET current_state = ?, state_json = ?, state_payload_sha256 = ?, "
                    "updated_at = ? WHERE workspace_root = ? AND lease_id = ? "
                    "AND current_state = 'active' AND state_payload_sha256 = ?",
                    (
                        state,
                        payload,
                        hashlib.sha256(payload.encode()).hexdigest(),
                        observed_at,
                        workspace,
                        lease.lease_id,
                        hashlib.sha256(previous.canonical_json().encode()).hexdigest(),
                    ),
                )
                if cursor.rowcount != 1:
                    row = await (
                        await db.execute(
                            "SELECT current_state, lease_json, lease_payload_sha256, "
                            "state_json, state_payload_sha256 "
                            "FROM evolution_capability_registry_leases "
                            "WHERE workspace_root = ? AND lease_id = ?",
                            (workspace, lease.lease_id),
                        )
                    ).fetchone()
                    await db.rollback()
                    if row is None:
                        raise CapabilityRegistryLeaseError(
                            "registry_lease_missing",
                            "Capability Registry lease 不存在。",
                        )
                    _, current = _restore_pair(row)
                    return current
                await db.execute(
                    "INSERT INTO evolution_capability_registry_lease_state_receipts "
                    "(workspace_root, lease_id, sequence, receipt_id, payload_json, "
                    "payload_sha256, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        lease.lease_id,
                        next_receipt.sequence,
                        next_receipt.receipt_id,
                        payload,
                        hashlib.sha256(payload.encode()).hexdigest(),
                        observed_at,
                    ),
                )
                await db.commit()
        except CapabilityRegistryLeaseError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityRegistryLeaseError(
                "registry_lease_transition_failed",
                "无法更新 Capability Registry lease 状态。",
            ) from exc
        return next_receipt

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self._db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise CapabilityRegistryLeaseError(
                    "registry_lease_store_init_failed",
                    "无法初始化 Capability Registry lease Store。",
                ) from exc
            self._schema_ready = True


class EvolutionCapabilityRegistryLeaseService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        artifact_service: EvolutionCapabilityArtifactService,
        execution_service: EvolutionCapabilitySandboxExecutionService,
        store: EvolutionCapabilityRegistryLeaseStore,
        tool_registry: ToolRegistry,
        runtime_instance_id: str,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.artifact_service = artifact_service
        self.execution_service = execution_service
        self.store = store
        self.tool_registry = tool_registry
        runtime_suffix = runtime_instance_id.removeprefix("evcruntime_")
        if (
            len(runtime_suffix) != 24
            or any(character not in "0123456789abcdef" for character in runtime_suffix)
        ):
            raise ValueError("Capability Registry runtime instance ID 无效。")
        self.runtime_instance_id = runtime_instance_id
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._lock = asyncio.Lock()

    async def inspect(self, candidate_id: str) -> CapabilityRegistryLeaseView:
        async with self._lock:
            return await self._inspect_locked(candidate_id.strip())

    async def acquire(
        self,
        *,
        candidate_id: str,
        run_id: str,
        duration_seconds: int,
        parent_permission: PermissionDecisionReceipt,
    ) -> CapabilityRegistryLeaseView:
        if isinstance(duration_seconds, bool) or not 30 <= duration_seconds <= 900:
            raise self._error("duration_invalid", "Registry lease 时长必须为 30..900 秒。")
        candidate = candidate_id.strip()
        self._validate_parent(
            parent_permission,
            {
                "action": "acquire",
                "candidate_id": candidate,
                "duration_seconds": duration_seconds,
                "run_id": run_id,
            },
            run_id,
        )
        async with self._lock:
            existing = await self._inspect_locked(candidate)
            if existing.state in {"active", "detached"}:
                if (
                    existing.state == "active"
                    and existing.lease is not None
                    and existing.lease.runtime_instance_id == self.runtime_instance_id
                ):
                    return existing
                raise self._error(
                    "active_elsewhere",
                    "Candidate 的 Registry lease 当前由其他 Runtime 持有。",
                )
            facts = await self._current_facts(candidate)
            if not all(facts["authority"]):
                raise self._error(
                    "execution_receipt_not_eligible",
                    "Registry lease 只接受 current、passed 且权限观察完整的 Execution Receipt。",
                )
            request = facts["request"]
            receipt = facts["receipt"]
            artifact = facts["artifact"]
            issued_at = self._now()
            lease = _lease(
                candidate_id=candidate,
                request=request,
                receipt=receipt,
                artifact=artifact,
                runtime_instance_id=self.runtime_instance_id,
                parent_permission=parent_permission,
                duration_seconds=duration_seconds,
                issued_at=issued_at,
            )
            try:
                self.tool_registry.reserve_unique(
                    lease.temporary_tool_name,
                    lease.lease_id,
                )
            except ToolRegistryConflictError as exc:
                raise self._error("name_conflict", str(exc)) from exc
            initial = _state_receipt(
                lease=lease,
                sequence=1,
                previous_receipt_sha256="",
                state="active",
                reason="acquired",
                observed_at=issued_at,
                request_current=True,
                artifact_current=True,
                execution_receipt_current=True,
                runtime_owner_current=True,
                registry_reservation_current=True,
                registry_reservation_release_confirmed=False,
            )
            try:
                stored_lease, stored_state = await self.store.issue(
                    self.workspace_root,
                    lease,
                    initial,
                )
            except BaseException:
                self.tool_registry.release_reservation_if_owned(
                    lease.temporary_tool_name,
                    lease.lease_id,
                )
                raise
            return CapabilityRegistryLeaseView(
                candidate_id=candidate,
                lease=stored_lease,
                state_receipt=stored_state,
                state="active",
                locally_reserved=True,
            )

    async def release(
        self,
        *,
        candidate_id: str,
        run_id: str,
        parent_permission: PermissionDecisionReceipt,
    ) -> CapabilityRegistryLeaseView:
        candidate = candidate_id.strip()
        self._validate_parent(
            parent_permission,
            {
                "action": "release",
                "candidate_id": candidate,
                "duration_seconds": 0,
                "run_id": run_id,
            },
            run_id,
        )
        async with self._lock:
            current = await self._inspect_locked(candidate)
            if current.lease is None or current.state not in {"active", "detached"}:
                return current
            if current.lease.runtime_instance_id != self.runtime_instance_id:
                raise self._error(
                    "runtime_owner_mismatch",
                    "只能由持有该 catalog reservation 的 Runtime 释放 lease。",
                )
            assert current.state_receipt is not None
            facts = await self._lease_facts(current.lease)
            released = self.tool_registry.release_reservation_if_owned(
                current.lease.temporary_tool_name,
                current.lease.lease_id,
            )
            terminal = await self.store.transition(
                self.workspace_root,
                lease=current.lease,
                previous=current.state_receipt,
                state="released",
                reason="user_released",
                observed_at=self._now(),
                request_current=facts[0],
                artifact_current=facts[1],
                execution_receipt_current=facts[2],
                runtime_owner_current=True,
                registry_reservation_current=False,
                registry_reservation_release_confirmed=released,
            )
            return CapabilityRegistryLeaseView(
                candidate_id=candidate,
                lease=current.lease,
                state_receipt=terminal,
                state=terminal.state,
                locally_reserved=False,
            )

    async def _inspect_locked(self, candidate_id: str) -> CapabilityRegistryLeaseView:
        stored = await self.store.latest(self.workspace_root, candidate_id)
        if stored is None:
            return CapabilityRegistryLeaseView(
                candidate_id=candidate_id,
                lease=None,
                state_receipt=None,
                state="missing",
                locally_reserved=False,
            )
        lease, state = stored
        locally_owned = lease.runtime_instance_id == self.runtime_instance_id
        reserved = (
            self.tool_registry.reservation_owner(lease.temporary_tool_name)
            == lease.lease_id
        )
        if state.state != "active":
            if locally_owned and reserved:
                self.tool_registry.release_reservation_if_owned(
                    lease.temporary_tool_name,
                    lease.lease_id,
                )
            return CapabilityRegistryLeaseView(
                candidate_id=candidate_id,
                lease=lease,
                state_receipt=state,
                state=state.state,
                locally_reserved=False,
            )
        now = _aware(self._now(), field="now")
        if now >= _aware(lease.expires_at, field="expires_at"):
            request_current, artifact_current, receipt_current = (
                await self._lease_facts(lease)
            )
            if locally_owned and reserved:
                self.tool_registry.release_reservation_if_owned(
                    lease.temporary_tool_name,
                    lease.lease_id,
                )
            terminal = await self.store.transition(
                self.workspace_root,
                lease=lease,
                previous=state,
                state="expired",
                reason="lease_expired",
                observed_at=now.isoformat(),
                request_current=request_current,
                artifact_current=artifact_current,
                execution_receipt_current=receipt_current,
                runtime_owner_current=locally_owned,
                registry_reservation_current=False,
                registry_reservation_release_confirmed=(locally_owned and reserved),
            )
            return CapabilityRegistryLeaseView(
                candidate_id=candidate_id,
                lease=lease,
                state_receipt=terminal,
                state=terminal.state,
                locally_reserved=False,
            )
        request_current, artifact_current, receipt_current = await self._lease_facts(lease)
        reason: RegistryLeaseReason | None = None
        if not request_current:
            reason = "request_revoked"
        elif not artifact_current:
            reason = "artifact_revoked"
        elif not receipt_current:
            reason = "execution_receipt_revoked"
        elif locally_owned and not reserved:
            reason = "registry_reservation_lost"
        if reason is not None:
            if locally_owned and reserved:
                self.tool_registry.release_reservation_if_owned(
                    lease.temporary_tool_name,
                    lease.lease_id,
                )
            terminal = await self.store.transition(
                self.workspace_root,
                lease=lease,
                previous=state,
                state="revoked",
                reason=reason,
                observed_at=now.isoformat(),
                request_current=request_current,
                artifact_current=artifact_current,
                execution_receipt_current=receipt_current,
                runtime_owner_current=locally_owned,
                registry_reservation_current=False,
                registry_reservation_release_confirmed=(locally_owned and reserved),
            )
            return CapabilityRegistryLeaseView(
                candidate_id=candidate_id,
                lease=lease,
                state_receipt=terminal,
                state=terminal.state,
                locally_reserved=False,
            )
        return CapabilityRegistryLeaseView(
            candidate_id=candidate_id,
            lease=lease,
            state_receipt=state,
            state="active" if locally_owned else "detached",
            locally_reserved=locally_owned and reserved,
        )

    async def _current_facts(self, candidate_id: str) -> dict[str, Any]:
        execution = await self.execution_service.inspect(candidate_id)
        artifact_view = await self.artifact_service.inspect(
            self.workspace_root,
            candidate_id,
        )
        receipt = execution.receipt
        artifact = artifact_view.artifact
        request = (
            await self.execution_service.request_service.inspect(
                self.workspace_root,
                candidate_id,
            )
        ).request
        request_current = execution.request_current and request is not None
        artifact_current = bool(
            artifact is not None
            and artifact_view.state == "preview_ready"
            and request is not None
            and artifact.artifact_id == request.artifact_id
            and hmac.compare_digest(artifact.artifact_sha256, request.artifact_sha256)
        )
        receipt_current = bool(
            receipt is not None
            and execution.state == "passed"
            and receipt.status == "passed"
            and receipt.permission_observation_complete
            and request is not None
            and receipt.request_id == request.request_id
            and hmac.compare_digest(receipt.request_sha256, request.request_sha256)
        )
        return {
            "request": request,
            "receipt": receipt,
            "artifact": artifact,
            "authority": (
                request_current,
                artifact_current,
                receipt_current,
                receipt is not None and bool(receipt.all_scenarios_passed),
            ),
        }

    async def _lease_facts(
        self,
        lease: EvolutionCapabilityRegistryLease,
    ) -> tuple[bool, bool, bool]:
        facts = await self._current_facts(lease.candidate_id)
        request = facts["request"]
        artifact = facts["artifact"]
        receipt = facts["receipt"]
        return (
            bool(
                facts["authority"][0]
                and request.request_id == lease.request_id
                and hmac.compare_digest(request.request_sha256, lease.request_sha256)
            ),
            bool(
                facts["authority"][1]
                and artifact.artifact_id == lease.artifact_id
                and hmac.compare_digest(artifact.artifact_sha256, lease.artifact_sha256)
            ),
            bool(
                facts["authority"][2]
                and receipt.receipt_id == lease.execution_receipt_id
                and hmac.compare_digest(
                    receipt.receipt_sha256,
                    lease.execution_receipt_sha256,
                )
            ),
        )

    def _validate_parent(
        self,
        receipt: PermissionDecisionReceipt,
        arguments: dict[str, object],
        run_id: str,
    ) -> None:
        if (
            not isinstance(receipt, PermissionDecisionReceipt)
            or not receipt.authorizes_execution
            or not run_id
            or receipt.run_id != run_id
            or receipt.tool_name != "evolution_capability_registry_lease"
            or receipt.arguments_sha256 != permission_arguments_sha256(arguments)
        ):
            raise self._error(
                "parent_permission_invalid",
                "Capability Registry lease 缺少与操作精确匹配的父权限回执。",
            )

    def _now(self) -> str:
        return _aware(self.now(), field="clock").astimezone(UTC).isoformat()

    @staticmethod
    def _error(code: str, message: str) -> CapabilityRegistryLeaseError:
        return CapabilityRegistryLeaseError(f"capability_{code}", message)


def _lease(
    *,
    candidate_id: str,
    request: Any,
    receipt: Any,
    artifact: Any,
    runtime_instance_id: str,
    parent_permission: PermissionDecisionReceipt,
    duration_seconds: int,
    issued_at: str,
) -> EvolutionCapabilityRegistryLease:
    expires_at = (_aware(issued_at, field="issued_at") + timedelta(
        seconds=duration_seconds
    )).isoformat()
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "candidate_id": candidate_id,
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "execution_receipt_id": receipt.receipt_id,
        "execution_receipt_sha256": receipt.receipt_sha256,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "temporary_tool_name": artifact.temporary_tool_name,
        "runtime_instance_id": runtime_instance_id,
        "parent_permission_receipt_id": parent_permission.receipt_id,
        "parent_permission_receipt_sha256": parent_permission.receipt_sha256,
        "duration_seconds": duration_seconds,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "catalog_only": True,
        "registry_reserved": True,
        "model_visible": False,
        "shadow_authorized": False,
        "executable": False,
    }
    digest = _digest(payload)
    return EvolutionCapabilityRegistryLease.model_validate({
        **payload,
        "lease_id": f"evcrl_{digest[:24]}",
        "lease_sha256": digest,
    })


def _state_receipt(
    *,
    lease: EvolutionCapabilityRegistryLease,
    sequence: int,
    previous_receipt_sha256: str,
    state: RegistryLeaseState,
    reason: RegistryLeaseReason,
    observed_at: str,
    request_current: bool,
    artifact_current: bool,
    execution_receipt_current: bool,
    runtime_owner_current: bool,
    registry_reservation_current: bool,
    registry_reservation_release_confirmed: bool,
) -> EvolutionCapabilityRegistryLeaseStateReceipt:
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "lease_id": lease.lease_id,
        "lease_sha256": lease.lease_sha256,
        "sequence": sequence,
        "previous_receipt_sha256": previous_receipt_sha256,
        "state": state,
        "reason": reason,
        "observed_at": observed_at,
        "request_current": request_current,
        "artifact_current": artifact_current,
        "execution_receipt_current": execution_receipt_current,
        "runtime_owner_current": runtime_owner_current,
        "local_registry_reservation_present": registry_reservation_current,
        "registry_reservation_release_confirmed": (
            registry_reservation_release_confirmed
        ),
        "model_visible": False,
        "shadow_authorized": False,
        "executable": False,
    }
    digest = _digest(payload)
    return EvolutionCapabilityRegistryLeaseStateReceipt.model_validate({
        **payload,
        "receipt_id": f"evcrlsr_{digest[:24]}",
        "receipt_sha256": digest,
    })


def render_capability_registry_lease(view: CapabilityRegistryLeaseView) -> str:
    lines = ["# Capability Registry Lease", ""]
    if view.lease is None or view.state_receipt is None:
        lines.extend([
            "尚未形成临时 Registry lease。",
            "",
            "- Catalog：否 · 模型可见：否 · Shadow：否 · 可执行：否",
        ])
        return "\n".join(lines)
    lease = view.lease
    state = view.state_receipt
    lines.extend([
        f"- Lease：`{lease.lease_id}`",
        f"- 状态：`{view.state}` · 原因：`{state.reason}`",
        f"- 临时名称：`{lease.temporary_tool_name}`",
        f"- 到期：`{lease.expires_at}`",
        f"- 当前 Runtime 保留：{'是' if view.locally_reserved else '否'}",
        "- Catalog：是 · 模型可见：否 · Shadow：否 · 可执行：否",
    ])
    if view.state == "detached":
        lines.append("- 说明：lease 属于其他 Runtime；本进程未加载任何候选代码。")
    return "\n".join(lines)


def _restore_pair(row: Any) -> tuple[
    EvolutionCapabilityRegistryLease,
    EvolutionCapabilityRegistryLeaseStateReceipt,
]:
    current_state, lease_json, lease_digest, state_json, state_digest = map(str, row)
    if not hmac.compare_digest(
        hashlib.sha256(lease_json.encode()).hexdigest(),
        lease_digest,
    ) or not hmac.compare_digest(
        hashlib.sha256(state_json.encode()).hexdigest(),
        state_digest,
    ):
        raise CapabilityRegistryLeaseError(
            "registry_lease_store_tampered",
            "Capability Registry lease 持久摘要不一致。",
        )
    try:
        lease = EvolutionCapabilityRegistryLease.model_validate_json(lease_json)
        state = EvolutionCapabilityRegistryLeaseStateReceipt.model_validate_json(
            state_json
        )
        if current_state != state.state:
            raise ValueError("current_state 与 state receipt 不一致。")
        return lease, state
    except (TypeError, ValueError) as exc:
        raise CapabilityRegistryLeaseError(
            "registry_lease_store_invalid",
            "Capability Registry lease 持久内容损坏。",
        ) from exc


def _aware(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO-8601 时间。") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_registry_leases (
    workspace_root TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    temporary_tool_name TEXT NOT NULL,
    runtime_instance_id TEXT NOT NULL,
    current_state TEXT NOT NULL CHECK (
        current_state IN ('active', 'released', 'revoked', 'expired')
    ),
    lease_json TEXT NOT NULL,
    lease_payload_sha256 TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_payload_sha256 TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, lease_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS evolution_capability_registry_active_candidate
ON evolution_capability_registry_leases(workspace_root, candidate_id)
WHERE current_state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS evolution_capability_registry_active_name
ON evolution_capability_registry_leases(workspace_root, temporary_tool_name)
WHERE current_state = 'active';
CREATE TABLE IF NOT EXISTS evolution_capability_registry_lease_state_receipts (
    workspace_root TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    receipt_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, lease_id, sequence),
    UNIQUE (workspace_root, receipt_id),
    FOREIGN KEY (workspace_root, lease_id)
        REFERENCES evolution_capability_registry_leases(workspace_root, lease_id)
);
"""


__all__ = [
    "CapabilityRegistryLeaseError",
    "CapabilityRegistryLeaseView",
    "EvolutionCapabilityRegistryLease",
    "EvolutionCapabilityRegistryLeaseService",
    "EvolutionCapabilityRegistryLeaseStateReceipt",
    "EvolutionCapabilityRegistryLeaseStore",
    "render_capability_registry_lease",
]
