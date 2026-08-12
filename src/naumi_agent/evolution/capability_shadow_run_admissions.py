"""Durable admission authority for bounded Capability shadow observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionReceipt,
    PermissionDecisionReceiptError,
    PermissionDecisionReceiptStore,
    permission_arguments_sha256,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantError,
    RunDelegationGrantRequest,
)
from naumi_agent.evolution.capability_shadow_observation_contracts import (
    CapabilityShadowObservationContractView,
    EvolutionCapabilityShadowObservationContract,
    EvolutionCapabilityShadowObservationContractService,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

_POLICY_VERSION = "evolution-capability-shadow-run-admission-v1"
_RUNNER_TOOL = "evolution_capability_shadow_observation_run"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
type AdmissionTerminalState = Literal["released", "expired", "revoked"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionCapabilityShadowRunAdmission(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-capability-shadow-run-admission-v1"
    ] = _POLICY_VERSION
    admission_id: str = Field(pattern=r"^evcsra_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    observation_contract_id: str = Field(pattern=r"^evcsoc_[0-9a-f]{24}$")
    observation_contract_sha256: str = Field(pattern=_SHA256_RE)
    observation_binding_sha256: str = Field(pattern=_SHA256_RE)
    model_contract_sha256: str = Field(pattern=_SHA256_RE)
    sampling_policy_sha256: str = Field(pattern=_SHA256_RE)
    parent_permission_receipt_id: str = Field(min_length=1, max_length=128)
    parent_permission_receipt_sha256: str = Field(pattern=_SHA256_RE)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    runtime_instance_id: str = Field(pattern=r"^evcsrart_[0-9a-f]{24}$")
    lease_owner_id: str = Field(pattern=r"^evcsraown_[0-9a-f]{24}$")
    lease_epoch: int = Field(ge=1)
    lease_expires_at: str = Field(min_length=20, max_length=64)
    run_grant_id: str = Field(min_length=1, max_length=128)
    run_grant_sha256: str = Field(pattern=_SHA256_RE)
    run_grant_expires_at: str = Field(min_length=20, max_length=64)
    delegated_tool_name: Literal[
        "evolution_capability_shadow_observation_run"
    ] = _RUNNER_TOOL
    max_model_calls: int = Field(ge=2, le=12)
    max_total_input_tokens: int = Field(ge=2_048)
    max_total_output_tokens: int = Field(ge=256)
    max_cost_microusd: int = Field(ge=0)
    max_wall_clock_seconds: int = Field(ge=120, le=720)
    admitted_at: str = Field(min_length=20, max_length=64)
    expires_at: str = Field(min_length=20, max_length=64)
    provider_call_scope_reserved: Literal[True] = True
    provider_call_completed: Literal[False] = False
    observation_recorded: Literal[False] = False
    candidate_execution_authorized: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _identity_is_exact(self) -> EvolutionCapabilityShadowRunAdmission:
        admitted = _aware(self.admitted_at, field="admitted_at")
        expires = _aware(self.expires_at, field="expires_at")
        lease_expires = _aware(self.lease_expires_at, field="lease_expires_at")
        grant_expires = _aware(self.run_grant_expires_at, field="run_grant_expires_at")
        if not admitted < expires <= min(lease_expires, grant_expires):
            raise ValueError("Shadow Run Admission 截止时间未被 lease/grant 截断。")
        if int((expires - admitted).total_seconds()) > self.max_wall_clock_seconds + 60:
            raise ValueError("Shadow Run Admission 生命周期超过预算清理窗口。")
        for value in (
            self.parent_permission_receipt_id,
            self.session_id,
            self.run_id,
            self.run_grant_id,
        ):
            if _SAFE_ID_RE.fullmatch(value) is None:
                raise ValueError("Shadow Run Admission authority identity 无效。")
        core = self.model_dump(mode="json", exclude={"admission_id", "admission_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.admission_sha256, digest)
            and self.admission_id == f"evcsra_{digest[:24]}"
        ):
            raise ValueError("Shadow Run Admission content identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class StoredCapabilityShadowRunAdmission(_StrictModel):
    admission: EvolutionCapabilityShadowRunAdmission
    state: Literal["active", "released", "expired", "revoked"]
    terminal_at: str | None = Field(default=None, max_length=64)
    terminal_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _state_is_exact(self) -> StoredCapabilityShadowRunAdmission:
        if self.state == "active":
            if self.terminal_at is not None or self.terminal_reason is not None:
                raise ValueError("Active Admission 不得包含终态事实。")
        else:
            if self.terminal_at is None or self.terminal_reason is None:
                raise ValueError("终态 Admission 必须包含时间与原因。")
            terminal = _aware(self.terminal_at, field="terminal_at")
            if terminal < _aware(self.admission.admitted_at, field="admitted_at"):
                raise ValueError("Admission 终态时间早于签发时间。")
            if _SAFE_ID_RE.fullmatch(self.terminal_reason) is None:
                raise ValueError("Admission 终态原因格式无效。")
        return self


class CapabilityShadowRunAdmissionView(_StrictModel):
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    admission: EvolutionCapabilityShadowRunAdmission | None
    state: Literal[
        "missing",
        "ready",
        "released",
        "expired",
        "revoked",
        "contract_revoked",
        "authority_revoked",
        "runtime_detached",
    ]
    observation_contract_current: bool
    runtime_current: bool
    run_grant_current: bool
    run_lease_current: bool
    runner_input_eligible: bool
    provider_call_authorized: bool
    provider_call_completed: Literal[False] = False
    observation_recorded: Literal[False] = False
    candidate_execution_authorized: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _projection_is_exact(self) -> CapabilityShadowRunAdmissionView:
        ready = self.state == "ready"
        if self.runner_input_eligible != ready or self.provider_call_authorized != ready:
            raise ValueError("Shadow Run Admission eligibility 与状态不一致。")
        if self.admission is None and self.state != "missing":
            raise ValueError("无 Admission 时状态只能是 missing。")
        return self


class CapabilityShadowRunAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionCapabilityShadowRunAdmissionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def latest(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> StoredCapabilityShadowRunAdmission | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self.db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT admission_id, candidate_id, observation_contract_id, "
                        "run_id, run_grant_id, payload_json, payload_sha256, state, "
                        "terminal_at, terminal_reason FROM "
                        "evolution_capability_shadow_run_admissions "
                        "WHERE workspace_root = ? AND candidate_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (workspace, candidate_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise self._error("store_read_failed", "无法读取 Shadow Run Admission。") from exc
        return None if row is None else _restore(row, expected_candidate_id=candidate_id)

    async def record(
        self,
        workspace_root: str | Path,
        admission: EvolutionCapabilityShadowRunAdmission,
    ) -> StoredCapabilityShadowRunAdmission:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        payload = admission.canonical_json()
        if len(payload.encode()) > 128 * 1024:
            raise self._error("oversized", "Shadow Run Admission 超过 128 KiB。")
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO evolution_capability_shadow_run_admissions "
                    "(workspace_root, admission_id, candidate_id, observation_contract_id, "
                    "run_id, run_grant_id, payload_json, payload_sha256, state, "
                    "terminal_at, terminal_reason, admitted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?)",
                    (
                        workspace,
                        admission.admission_id,
                        admission.candidate_id,
                        admission.observation_contract_id,
                        admission.run_id,
                        admission.run_grant_id,
                        payload,
                        hashlib.sha256(payload.encode()).hexdigest(),
                        admission.admitted_at,
                    ),
                )
                await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise self._error(
                "active_conflict",
                "Candidate 已存在 active Shadow Run Admission。",
            ) from exc
        except (aiosqlite.Error, OSError) as exc:
            raise self._error("store_write_failed", "无法保存 Shadow Run Admission。") from exc
        return StoredCapabilityShadowRunAdmission(admission=admission, state="active")

    async def transition(
        self,
        workspace_root: str | Path,
        *,
        admission_id: str,
        state: AdmissionTerminalState,
        terminal_at: str,
        reason: str,
    ) -> StoredCapabilityShadowRunAdmission:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        timestamp = _aware(terminal_at, field="terminal_at").astimezone(UTC).isoformat()
        if _SAFE_ID_RE.fullmatch(reason) is None:
            raise ValueError("Admission 终态原因格式无效。")
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE evolution_capability_shadow_run_admissions "
                    "SET state = ?, terminal_at = ?, terminal_reason = ? "
                    "WHERE workspace_root = ? AND admission_id = ? AND state = 'active'",
                    (state, timestamp, reason, workspace, admission_id),
                )
                if cursor.rowcount <= 0:
                    row = await (
                        await db.execute(
                            "SELECT admission_id, candidate_id, observation_contract_id, "
                            "run_id, run_grant_id, payload_json, payload_sha256, state, "
                            "terminal_at, terminal_reason FROM "
                            "evolution_capability_shadow_run_admissions "
                            "WHERE workspace_root = ? AND admission_id = ?",
                            (workspace, admission_id),
                        )
                    ).fetchone()
                    if row is None:
                        raise self._error("missing", "Shadow Run Admission 不存在。")
                    restored = _restore(row)
                    if (
                        restored.state != state
                        or restored.terminal_at != timestamp
                        or restored.terminal_reason != reason
                    ):
                        raise self._error("terminal_conflict", "Admission 已按其他事实终止。")
                    await db.rollback()
                    return restored
                row = await (
                    await db.execute(
                        "SELECT admission_id, candidate_id, observation_contract_id, "
                        "run_id, run_grant_id, payload_json, payload_sha256, state, "
                        "terminal_at, terminal_reason FROM "
                        "evolution_capability_shadow_run_admissions "
                        "WHERE workspace_root = ? AND admission_id = ?",
                        (workspace, admission_id),
                    )
                ).fetchone()
                await db.commit()
        except CapabilityShadowRunAdmissionError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise self._error("store_transition_failed", "无法终止 Shadow Run Admission。") from exc
        assert row is not None
        return _restore(row)

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self.db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise self._error(
                    "store_init_failed",
                    "无法初始化 Shadow Run Admission Store。",
                ) from exc
            self._schema_ready = True

    @staticmethod
    def _error(code: str, message: str) -> CapabilityShadowRunAdmissionError:
        return CapabilityShadowRunAdmissionError(f"capability_shadow_run_{code}", message)


class EvolutionCapabilityShadowRunAdmissionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        observation_contract_service: EvolutionCapabilityShadowObservationContractService,
        store: EvolutionCapabilityShadowRunAdmissionStore,
        harness_store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        runtime_instance_id: str,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.observation_contract_service = observation_contract_service
        self.store = store
        self.harness_store = harness_store
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        if re.fullmatch(r"evcsrart_[0-9a-f]{24}", runtime_instance_id) is None:
            raise ValueError("Shadow Run Admission runtime identity 无效。")
        self.runtime_instance_id = runtime_instance_id
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._lock = asyncio.Lock()

    async def inspect(self, candidate_id: str) -> CapabilityShadowRunAdmissionView:
        candidate = candidate_id.strip()
        async with self._lock:
            return await self._inspect_locked(candidate)

    async def issue(
        self,
        *,
        candidate_id: str,
        run_id: str,
        parent_permission: PermissionDecisionReceipt,
    ) -> CapabilityShadowRunAdmissionView:
        candidate = candidate_id.strip()
        run = run_id.strip()
        await self._validate_parent(parent_permission, "issue", candidate, run)
        async with self._lock:
            current = await self._inspect_locked(candidate)
            if current.state == "ready":
                if (
                    current.admission is not None
                    and current.admission.run_id == run
                    and current.admission.runtime_instance_id == self.runtime_instance_id
                ):
                    return current
                raise self._error(
                    "active_conflict",
                    "Candidate 的 active Admission 已绑定其他 run 或 Runtime。",
                )
            if current.admission is not None and current.state not in {
                "released",
                "revoked",
            }:
                await self._terminate_locked(
                    current.admission,
                    state="expired" if current.state == "expired" else "revoked",
                    reason=f"reconciled_{current.state}",
                    require_release=False,
                )
            observation = await self.observation_contract_service.inspect(candidate)
            contract = observation.contract
            if (
                observation.state != "ready"
                or contract is None
                or not observation.observation_input_eligible
            ):
                raise self._error(
                    "contract_not_ready",
                    "Shadow Run Admission 需要 current 4b Observation Contract。",
                )
            admitted_at = self._now()
            ttl_seconds = min(780, contract.sampling.max_wall_clock_seconds + 60)
            owner_id = f"evcsraown_{uuid4().hex[:24]}"
            lease = await self.harness_store.acquire_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run,
                owner_id=owner_id,
                now=admitted_at,
                lease_seconds=ttl_seconds,
            )
            if lease is None:
                raise self._error("lease_unavailable", "无法取得独占 Shadow Runtime lease。")
            grant = None
            try:
                grant = await self.run_grant_authority.issue(
                    RunDelegationGrantRequest(
                        idempotency_key=(
                            f"ev-shadow-{contract.contract_sha256[:20]}-{lease.epoch}"
                        ),
                        parent_receipt_id=parent_permission.receipt_id,
                        run_kind=HarnessRunKind.RUNTIME,
                        lease_owner_id=owner_id,
                        lease_epoch=lease.epoch,
                        delegated_tool_names=(_RUNNER_TOOL,),
                    ),
                    now=admitted_at,
                    ttl_seconds=ttl_seconds,
                )
                admission = _admission(
                    contract=contract,
                    parent=parent_permission,
                    runtime_instance_id=self.runtime_instance_id,
                    lease=lease,
                    grant=grant.contract,
                    admitted_at=admitted_at,
                )
                await self.store.record(self.workspace_root, admission)
            except asyncio.CancelledError:
                await self._cleanup_failed_issue(lease, grant, admitted_at)
                raise
            except CapabilityShadowRunAdmissionError:
                cleanup_errors = await self._cleanup_failed_issue(
                    lease,
                    grant,
                    admitted_at,
                )
                if cleanup_errors:
                    raise self._error(
                        "authority_cleanup_failed",
                        "Shadow Run Admission 签发失败且 authority 清理不完整。",
                    )
                raise
            except (
                HarnessStoreError,
                RunDelegationGrantError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                cleanup_errors = await self._cleanup_failed_issue(
                    lease,
                    grant,
                    admitted_at,
                )
                if cleanup_errors:
                    raise self._error(
                        "authority_cleanup_failed",
                        "Shadow Run Admission 签发失败且 authority 清理不完整。",
                    ) from exc
                raise self._error(
                    "authority_issue_failed",
                    "Shadow Run Lease 或 Run Grant 签发失败。",
                ) from exc
            return await self._inspect_locked(candidate)

    async def revoke(
        self,
        *,
        candidate_id: str,
        run_id: str,
        parent_permission: PermissionDecisionReceipt,
    ) -> CapabilityShadowRunAdmissionView:
        candidate = candidate_id.strip()
        run = run_id.strip()
        await self._validate_parent(parent_permission, "revoke", candidate, run)
        async with self._lock:
            current = await self._inspect_locked(candidate)
            if current.admission is None:
                return current
            if current.admission.run_id != run:
                raise self._error("run_mismatch", "Admission 与当前 run_id 不一致。")
            if current.state in {"released", "revoked"}:
                return current
            if current.admission.runtime_instance_id != self.runtime_instance_id:
                raise self._error(
                    "runtime_owner_mismatch",
                    "只能由签发 Admission 的 Runtime 撤销。",
                )
            await self._terminate_locked(
                current.admission,
                state="revoked",
                reason="user_revoked",
                require_release=current.state != "expired",
            )
            return await self._inspect_locked(candidate)

    async def _inspect_locked(self, candidate_id: str) -> CapabilityShadowRunAdmissionView:
        stored = await self.store.latest(self.workspace_root, candidate_id)
        if stored is None:
            return _view(candidate_id, None, state="missing")
        admission = stored.admission
        if stored.state != "active":
            return _view(candidate_id, admission, state=stored.state)
        now = _aware(self._now(), field="now")
        if now >= _aware(admission.expires_at, field="expires_at"):
            return _view(candidate_id, admission, state="expired")
        observation = await self.observation_contract_service.inspect(candidate_id)
        observation_current = _observation_matches(observation, admission)
        runtime_current = admission.runtime_instance_id == self.runtime_instance_id
        try:
            grant = await self.run_grant_authority.validate(
                grant_id=admission.run_grant_id,
                now=now.isoformat(),
            )
        except (RunDelegationGrantError, OSError, TypeError, ValueError):
            grant = None
        run_grant_current = bool(
            grant is not None
            and grant.allowed
            and grant.contract is not None
            and hmac.compare_digest(
                grant.contract.grant_sha256,
                admission.run_grant_sha256,
            )
            and grant.contract.delegated_tool_names == (_RUNNER_TOOL,)
        )
        lease = await self.harness_store.get_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=admission.run_id,
        )
        run_lease_current = bool(
            lease is not None
            and lease.state is HarnessRunLeaseState.ACTIVE
            and lease.owner_id == admission.lease_owner_id
            and lease.epoch == admission.lease_epoch
            and lease.expires_at == admission.lease_expires_at
            and now < _aware(lease.expires_at, field="lease_expires_at")
        )
        if not observation_current:
            state = "contract_revoked"
        elif not runtime_current:
            state = "runtime_detached"
        elif not run_grant_current or not run_lease_current:
            state = "authority_revoked"
        else:
            state = "ready"
        return _view(
            candidate_id,
            admission,
            state=state,
            observation_contract_current=observation_current,
            runtime_current=runtime_current,
            run_grant_current=run_grant_current,
            run_lease_current=run_lease_current,
        )

    async def _terminate_locked(
        self,
        admission: EvolutionCapabilityShadowRunAdmission,
        *,
        state: AdmissionTerminalState,
        reason: str,
        require_release: bool,
    ) -> None:
        terminal_at = self._now()
        cleanup_errors: list[BaseException] = []
        try:
            await self.run_grant_authority.revoke(
                grant_id=admission.run_grant_id,
                reason=reason,
                revoked_at=terminal_at,
            )
        except BaseException as exc:
            try:
                validation = await self.run_grant_authority.validate(
                    grant_id=admission.run_grant_id,
                    now=terminal_at,
                )
            except BaseException:
                cleanup_errors.append(exc)
            else:
                if (
                    validation.allowed
                    or validation.contract is None
                    or not hmac.compare_digest(
                        validation.contract.grant_sha256,
                        admission.run_grant_sha256,
                    )
                ):
                    cleanup_errors.append(exc)
        try:
            released = await self.harness_store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=admission.run_id,
                owner_id=admission.lease_owner_id,
                epoch=admission.lease_epoch,
                now=terminal_at,
            )
            if require_release and released is None:
                cleanup_errors.append(RuntimeError("Shadow Runtime lease 未释放。"))
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise self._error(
                "authority_cleanup_failed",
                "Shadow Run Grant 或 Runtime lease 清理不完整。",
            )
        await self.store.transition(
            self.workspace_root,
            admission_id=admission.admission_id,
            state=state,
            terminal_at=terminal_at,
            reason=reason,
        )

    async def _cleanup_failed_issue(
        self,
        lease: Any,
        grant: Any,
        now: str,
    ) -> tuple[BaseException, ...]:
        errors: list[BaseException] = []
        if grant is not None:
            try:
                await self.run_grant_authority.revoke(
                    grant_id=grant.contract.grant_id,
                    reason="admission_issue_failed",
                    revoked_at=now,
                )
            except BaseException as exc:
                errors.append(exc)
        try:
            released = await self.harness_store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                epoch=lease.epoch,
                now=now,
            )
            if released is None:
                errors.append(RuntimeError("Shadow Runtime lease 未释放。"))
        except BaseException as exc:
            errors.append(exc)
        return tuple(errors)

    async def _validate_parent(
        self,
        receipt: PermissionDecisionReceipt,
        action: str,
        candidate_id: str,
        run_id: str,
    ) -> None:
        arguments = {"action": action, "candidate_id": candidate_id, "run_id": run_id}
        if (
            not isinstance(receipt, PermissionDecisionReceipt)
            or not receipt.authorizes_execution
            or not receipt.run_id
            or receipt.run_id != run_id
            or receipt.tool_name != "evolution_capability_shadow_run_admission"
            or _RUNNER_TOOL not in receipt.delegated_tool_names
            or receipt.arguments_sha256 != permission_arguments_sha256(arguments)
        ):
            raise self._error(
                "parent_permission_invalid",
                "Shadow Run Admission 缺少与操作精确匹配的父权限回执。",
            )
        try:
            stored = await self.permission_store.get(receipt.receipt_id)
        except (PermissionDecisionReceiptError, OSError, TypeError, ValueError) as exc:
            raise self._error(
                "parent_permission_unavailable",
                "Shadow Run Admission 无法重读父权限回执。",
            ) from exc
        if stored != receipt:
            raise self._error(
                "parent_permission_not_current",
                "Shadow Run Admission 父权限回执不存在或与持久 authority 不一致。",
            )

    def _now(self) -> str:
        return _aware(self.now(), field="clock").astimezone(UTC).isoformat()

    @staticmethod
    def _error(code: str, message: str) -> CapabilityShadowRunAdmissionError:
        return CapabilityShadowRunAdmissionError(f"capability_shadow_run_{code}", message)


def _admission(
    *,
    contract: EvolutionCapabilityShadowObservationContract,
    parent: PermissionDecisionReceipt,
    runtime_instance_id: str,
    lease: Any,
    grant: Any,
    admitted_at: str,
) -> EvolutionCapabilityShadowRunAdmission:
    expires = min(
        _aware(lease.expires_at, field="lease_expires_at"),
        _aware(grant.expires_at, field="run_grant_expires_at"),
        _aware(admitted_at, field="admitted_at")
        + timedelta(seconds=contract.sampling.max_wall_clock_seconds + 60),
    ).isoformat()
    core = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "candidate_id": contract.candidate_id,
        "observation_contract_id": contract.contract_id,
        "observation_contract_sha256": contract.contract_sha256,
        "observation_binding_sha256": contract.binding_sha256,
        "model_contract_sha256": contract.model.contract_sha256,
        "sampling_policy_sha256": _digest(contract.sampling.model_dump(mode="json")),
        "parent_permission_receipt_id": parent.receipt_id,
        "parent_permission_receipt_sha256": parent.receipt_sha256,
        "session_id": parent.session_id,
        "run_id": parent.run_id,
        "runtime_instance_id": runtime_instance_id,
        "lease_owner_id": lease.owner_id,
        "lease_epoch": lease.epoch,
        "lease_expires_at": lease.expires_at,
        "run_grant_id": grant.grant_id,
        "run_grant_sha256": grant.grant_sha256,
        "run_grant_expires_at": grant.expires_at,
        "delegated_tool_name": _RUNNER_TOOL,
        "max_model_calls": contract.sampling.max_model_calls,
        "max_total_input_tokens": contract.sampling.max_total_input_tokens,
        "max_total_output_tokens": contract.sampling.max_total_output_tokens,
        "max_cost_microusd": contract.sampling.max_cost_microusd,
        "max_wall_clock_seconds": contract.sampling.max_wall_clock_seconds,
        "admitted_at": _aware(admitted_at, field="admitted_at").astimezone(UTC).isoformat(),
        "expires_at": expires,
        "provider_call_scope_reserved": True,
        "provider_call_completed": False,
        "observation_recorded": False,
        "candidate_execution_authorized": False,
        "side_effects_allowed": False,
        "activation_authorized": False,
    }
    digest = _digest(core)
    return EvolutionCapabilityShadowRunAdmission.model_validate({
        **core,
        "admission_id": f"evcsra_{digest[:24]}",
        "admission_sha256": digest,
    })


def _observation_matches(
    view: CapabilityShadowObservationContractView,
    admission: EvolutionCapabilityShadowRunAdmission,
) -> bool:
    contract = view.contract
    return bool(
        view.state == "ready"
        and view.observation_input_eligible
        and contract is not None
        and contract.contract_id == admission.observation_contract_id
        and hmac.compare_digest(
            contract.contract_sha256,
            admission.observation_contract_sha256,
        )
        and hmac.compare_digest(contract.binding_sha256, admission.observation_binding_sha256)
        and hmac.compare_digest(
            contract.model.contract_sha256,
            admission.model_contract_sha256,
        )
        and hmac.compare_digest(
            _digest(contract.sampling.model_dump(mode="json")),
            admission.sampling_policy_sha256,
        )
    )


def _view(
    candidate_id: str,
    admission: EvolutionCapabilityShadowRunAdmission | None,
    *,
    state: str,
    observation_contract_current: bool = False,
    runtime_current: bool = False,
    run_grant_current: bool = False,
    run_lease_current: bool = False,
) -> CapabilityShadowRunAdmissionView:
    return CapabilityShadowRunAdmissionView(
        candidate_id=candidate_id,
        admission=admission,
        state=state,
        observation_contract_current=observation_contract_current,
        runtime_current=runtime_current,
        run_grant_current=run_grant_current,
        run_lease_current=run_lease_current,
        runner_input_eligible=state == "ready",
        provider_call_authorized=state == "ready",
    )


def render_capability_shadow_run_admission(view: CapabilityShadowRunAdmissionView) -> str:
    lines = ["# Capability Shadow 运行准入", ""]
    if view.admission is None:
        return "\n".join([
            *lines,
            "尚未签发 Shadow Run Admission。",
            "",
            "- Provider 调用授权：否 · Candidate 执行：否 · 激活：否",
        ])
    item = view.admission
    lines.extend([
        f"- Admission：`{item.admission_id}`",
        f"- 状态：`{view.state}` · Contract：`{item.observation_contract_id}`",
        f"- Run / Lease epoch：`{item.run_id}` / `{item.lease_epoch}`",
        f"- Grant：`{item.run_grant_id}` · 截止：`{item.expires_at}`",
        (
            f"- 预算：{item.max_model_calls} calls · "
            f"{item.max_total_input_tokens} input tokens · "
            f"{item.max_total_output_tokens} output tokens · "
            f"{item.max_cost_microusd} µUSD · {item.max_wall_clock_seconds}s"
        ),
        f"- Runner 输入资格：{'是' if view.runner_input_eligible else '否'}",
        f"- Provider 调用授权：{'是' if view.provider_call_authorized else '否'}",
        "- Observation：未记录 · Candidate 执行：否 · 副作用：否 · 激活：否",
        "- 说明：本阶段只保留一次有界 Runner scope；尚未发送任何 Provider 请求。",
    ])
    return "\n".join(lines)


def _restore(
    row: Any,
    *,
    expected_candidate_id: str | None = None,
) -> StoredCapabilityShadowRunAdmission:
    (
        stored_admission_id,
        stored_candidate_id,
        stored_contract_id,
        stored_run_id,
        stored_grant_id,
        payload,
        stored_digest,
        state,
        terminal_at,
        terminal_reason,
    ) = row
    payload = str(payload)
    if not hmac.compare_digest(
        hashlib.sha256(payload.encode()).hexdigest(),
        str(stored_digest),
    ):
        raise CapabilityShadowRunAdmissionError(
            "capability_shadow_run_store_tampered",
            "Shadow Run Admission 持久摘要不一致。",
        )
    try:
        admission = EvolutionCapabilityShadowRunAdmission.model_validate_json(payload)
        restored = StoredCapabilityShadowRunAdmission(
            admission=admission,
            state=str(state),
            terminal_at=None if terminal_at is None else str(terminal_at),
            terminal_reason=None if terminal_reason is None else str(terminal_reason),
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityShadowRunAdmissionError(
            "capability_shadow_run_store_invalid",
            "Shadow Run Admission 持久内容损坏。",
        ) from exc
    if (
        str(stored_admission_id) != admission.admission_id
        or str(stored_candidate_id) != admission.candidate_id
        or str(stored_contract_id) != admission.observation_contract_id
        or str(stored_run_id) != admission.run_id
        or str(stored_grant_id) != admission.run_grant_id
        or (
            expected_candidate_id is not None
            and str(stored_candidate_id) != expected_candidate_id
        )
    ):
        raise CapabilityShadowRunAdmissionError(
            "capability_shadow_run_store_tampered",
            "Shadow Run Admission 持久裁决字段不一致。",
        )
    return restored


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
CREATE TABLE IF NOT EXISTS evolution_capability_shadow_run_admissions (
    workspace_root TEXT NOT NULL,
    admission_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    observation_contract_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    run_grant_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'released', 'expired', 'revoked')),
    terminal_at TEXT,
    terminal_reason TEXT,
    admitted_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, admission_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS evolution_capability_shadow_run_one_active
ON evolution_capability_shadow_run_admissions(workspace_root, candidate_id)
WHERE state = 'active';
CREATE INDEX IF NOT EXISTS evolution_capability_shadow_run_candidate
ON evolution_capability_shadow_run_admissions(workspace_root, candidate_id, admitted_at);
"""


__all__ = [
    "CapabilityShadowRunAdmissionError",
    "CapabilityShadowRunAdmissionView",
    "EvolutionCapabilityShadowRunAdmission",
    "EvolutionCapabilityShadowRunAdmissionService",
    "EvolutionCapabilityShadowRunAdmissionStore",
    "StoredCapabilityShadowRunAdmission",
    "render_capability_shadow_run_admission",
]
