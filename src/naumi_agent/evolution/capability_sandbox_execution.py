"""ARC-04 execution and immutable receipts for sealed Capability requests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import platform
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionReceipt,
    PermissionDecisionReceiptStore,
    permission_arguments_sha256,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.evolution.capability_sandbox_request import (
    EvolutionCapabilitySandboxExecutionRequest,
    EvolutionCapabilitySandboxRequestService,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckStatus,
    HarnessSandboxSourceOverlay,
)
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
    HarnessSandboxEvalSource,
)
from naumi_agent.harness.store import HarnessStore

_POLICY_VERSION = "evolution-capability-sandbox-execution-v1"
_UNSUPPORTED_PERMISSION_FAMILIES = frozenset({"network", "browser", "secrets"})
CapabilityScenarioExecutionStatus = Literal[
    "passed",
    "oracle_mismatch",
    "declared_error_mismatch",
    "timed_out",
    "permission_violation",
    "resource_limit",
    "cancelled",
    "source_stale",
    "infrastructure_failure",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class CapabilitySandboxPermissionObservation(_StrictModel):
    family: Literal[
        "workspace_read",
        "workspace_write",
        "process",
        "network",
        "observation",
    ]
    scope: str = Field(min_length=1, max_length=512)
    decision: Literal["allow", "deny"]


class CapabilitySandboxScenarioExecutionReceipt(_StrictModel):
    order: int = Field(ge=1, le=8)
    check_id: str = Field(pattern=r"^capability_scenario_0[1-8]$")
    expectation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CapabilityScenarioExecutionStatus
    actual_kind: Literal[
        "result",
        "error",
        "timeout",
        "permission_violation",
        "resource_limit",
        "cancelled",
        "stale",
        "infrastructure_error",
    ]
    actual_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permission_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permission_observation_complete: bool
    permission_observations: tuple[CapabilitySandboxPermissionObservation, ...] = Field(
        max_length=256,
        repr=False,
    )
    job_id: str = Field(min_length=1, max_length=128)
    lifecycle_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _scenario_receipt_is_exact(self) -> CapabilitySandboxScenarioExecutionReceipt:
        if self.check_id != f"capability_scenario_{self.order:02d}":
            raise ValueError("Capability execution check id 与顺序不一致。")
        observed = [item.model_dump(mode="json") for item in self.permission_observations]
        if not hmac.compare_digest(self.permission_observation_sha256, _digest(observed)):
            raise ValueError("Capability permission observation 摘要不一致。")
        return self


class EvolutionCapabilitySandboxExecutionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-sandbox-execution-v1"] = (
        _POLICY_VERSION
    )
    receipt_id: str = Field(pattern=r"^evcser_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(pattern=r"^evcsr_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    binding_id: str = Field(pattern=r"^evcsb_[0-9a-f]{24}$")
    source_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overlay_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_permission_receipt_id: str = Field(min_length=1, max_length=128)
    parent_permission_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenarios: tuple[CapabilitySandboxScenarioExecutionReceipt, ...] = Field(
        min_length=1,
        max_length=8,
    )
    status: Literal["passed", "failed", "stale"]
    all_scenarios_executed: Literal[True] = True
    all_scenarios_passed: bool
    permission_observation_complete: bool
    exact_revision_materialized: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    run_grant_revoked: Literal[True] = True
    runtime_lease_released: Literal[True] = True
    sandbox_execution_completed: Literal[True] = True
    registry_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False
    completed_at: str = Field(min_length=20, max_length=64)

    @model_validator(mode="after")
    def _receipt_is_content_addressed(self) -> EvolutionCapabilitySandboxExecutionReceipt:
        if tuple(item.order for item in self.scenarios) != tuple(
            range(1, len(self.scenarios) + 1)
        ):
            raise ValueError("Capability execution receipts 必须连续排序。")
        all_passed = all(item.status == "passed" for item in self.scenarios)
        any_stale = any(item.status == "source_stale" for item in self.scenarios)
        expected_status = "stale" if any_stale else ("passed" if all_passed else "failed")
        if self.all_scenarios_passed != all_passed or self.status != expected_status:
            raise ValueError("Capability execution 汇总状态与场景不一致。")
        if self.permission_observation_complete != all(
            item.permission_observation_complete for item in self.scenarios
        ):
            raise ValueError("Capability permission observation 汇总不一致。")
        payload = self.model_dump(
            mode="json",
            exclude={"receipt_id", "receipt_sha256"},
        )
        digest = _digest(payload)
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Capability Sandbox Execution Receipt 摘要不一致。")
        if self.receipt_id != f"evcser_{digest[:24]}":
            raise ValueError("Capability Sandbox Execution Receipt identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class CapabilitySandboxExecutionView(_StrictModel):
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    receipt: EvolutionCapabilitySandboxExecutionReceipt | None
    state: Literal["missing", "passed", "failed", "revoked"]
    request_current: bool
    registry_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False


class CapabilitySandboxExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CapabilitySandboxExecutionClaim(_StrictModel):
    request_id: str = Field(pattern=r"^evcsr_[0-9a-f]{24}$")
    owner_id: str = Field(pattern=r"^evcsexec_[0-9a-f]{24}$")
    epoch: int = Field(ge=1)
    acquired_at: str = Field(min_length=20, max_length=64)
    expires_at: str = Field(min_length=20, max_length=64)
    state: Literal["active", "terminal"]

    @model_validator(mode="after")
    def _claim_interval_is_valid(self) -> CapabilitySandboxExecutionClaim:
        acquired = _aware_time(self.acquired_at, field="acquired_at")
        expires = _aware_time(self.expires_at, field="expires_at")
        if expires <= acquired:
            raise ValueError("Capability Sandbox execution claim 到期时间无效。")
        return self


class EvolutionCapabilitySandboxExecutionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def get(
        self,
        workspace_root: str | Path,
        request_id: str,
    ) -> EvolutionCapabilitySandboxExecutionReceipt | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_sandbox_execution_receipts "
                        "WHERE workspace_root = ? AND request_id = ?",
                        (workspace, request_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilitySandboxExecutionError(
                "sandbox_execution_store_read_failed",
                "无法读取 Capability Sandbox Execution Receipt。",
            ) from exc
        return None if row is None else _restore_receipt(row[0], row[1])

    async def claim(
        self,
        workspace_root: str | Path,
        *,
        request_id: str,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> CapabilitySandboxExecutionClaim | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        timestamp = _aware_time(now, field="now")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3_600:
            raise ValueError("Capability Sandbox claim lease 必须在 1..3600 秒。")
        expires_at = (timestamp + timedelta(seconds=lease_seconds)).isoformat()
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                receipt_row = await (
                    await db.execute(
                        "SELECT 1 FROM evolution_capability_sandbox_execution_receipts "
                        "WHERE workspace_root = ? AND request_id = ?",
                        (workspace, request_id),
                    )
                ).fetchone()
                if receipt_row is not None:
                    await db.rollback()
                    return None
                await db.execute(
                    """
                    INSERT INTO evolution_capability_sandbox_execution_claims (
                        workspace_root, request_id, owner_id, epoch, state,
                        acquired_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, 1, 'active', ?, ?, ?)
                    ON CONFLICT(workspace_root, request_id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        epoch = evolution_capability_sandbox_execution_claims.epoch + 1,
                        state = 'active',
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    WHERE evolution_capability_sandbox_execution_claims.state = 'active'
                      AND evolution_capability_sandbox_execution_claims.expires_at
                          <= excluded.updated_at
                    """,
                    (
                        workspace,
                        request_id,
                        owner_id,
                        timestamp.isoformat(),
                        expires_at,
                        timestamp.isoformat(),
                    ),
                )
                row = await (
                    await db.execute(
                        "SELECT request_id, owner_id, epoch, acquired_at, expires_at, state "
                        "FROM evolution_capability_sandbox_execution_claims "
                        "WHERE workspace_root = ? AND request_id = ?",
                        (workspace, request_id),
                    )
                ).fetchone()
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilitySandboxExecutionError(
                "sandbox_execution_claim_failed",
                "无法取得 Capability Sandbox 持久执行 claim。",
            ) from exc
        claim = None if row is None else _claim_from_row(row)
        if claim is None or claim.owner_id != owner_id or claim.state != "active":
            return None
        return claim

    async def record(
        self,
        workspace_root: str | Path,
        receipt: EvolutionCapabilitySandboxExecutionReceipt,
        *,
        claim: CapabilitySandboxExecutionClaim,
    ) -> EvolutionCapabilitySandboxExecutionReceipt:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        payload = receipt.canonical_json()
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                claim_row = await (
                    await db.execute(
                        "SELECT request_id, owner_id, epoch, acquired_at, expires_at, state "
                        "FROM evolution_capability_sandbox_execution_claims "
                        "WHERE workspace_root = ? AND request_id = ?",
                        (workspace, receipt.request_id),
                    )
                ).fetchone()
                stored_claim = None if claim_row is None else _claim_from_row(claim_row)
                completed_at = _aware_time(receipt.completed_at, field="completed_at")
                if (
                    stored_claim is None
                    or stored_claim != claim
                    or stored_claim.state != "active"
                    or _aware_time(stored_claim.expires_at, field="expires_at")
                    <= completed_at
                ):
                    await db.rollback()
                    raise CapabilitySandboxExecutionError(
                        "sandbox_execution_claim_stale",
                        "Capability Sandbox 执行 claim 已漂移或过期，拒绝提交 Receipt。",
                    )
                await db.execute(
                    "INSERT OR IGNORE INTO "
                    "evolution_capability_sandbox_execution_receipts "
                    "(workspace_root, request_id, receipt_id, payload_json, "
                    "payload_sha256, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        receipt.request_id,
                        receipt.receipt_id,
                        payload,
                        payload_sha256,
                        receipt.completed_at,
                    ),
                )
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_sandbox_execution_receipts "
                        "WHERE workspace_root = ? AND request_id = ?",
                        (workspace, receipt.request_id),
                    )
                ).fetchone()
                cursor = await db.execute(
                    "UPDATE evolution_capability_sandbox_execution_claims "
                    "SET state = 'terminal', updated_at = ? "
                    "WHERE workspace_root = ? AND request_id = ? "
                    "AND owner_id = ? AND epoch = ? AND state = 'active'",
                    (
                        receipt.completed_at,
                        workspace,
                        receipt.request_id,
                        claim.owner_id,
                        claim.epoch,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise CapabilitySandboxExecutionError(
                        "sandbox_execution_claim_stale",
                        "Capability Sandbox 执行 claim 未能原子收口。",
                    )
                await db.commit()
        except CapabilitySandboxExecutionError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilitySandboxExecutionError(
                "sandbox_execution_store_write_failed",
                "无法保存 Capability Sandbox Execution Receipt。",
            ) from exc
        if row is None:
            raise CapabilitySandboxExecutionError(
                "sandbox_execution_store_missing",
                "Capability Sandbox Execution Receipt 未形成持久记录。",
            )
        return _restore_receipt(row[0], row[1])

    async def abandon(
        self,
        workspace_root: str | Path,
        claim: CapabilitySandboxExecutionClaim,
    ) -> bool:
        """Remove an exact claim only before candidate execution has started."""
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "DELETE FROM evolution_capability_sandbox_execution_claims "
                    "WHERE workspace_root = ? AND request_id = ? AND owner_id = ? "
                    "AND epoch = ? AND state = 'active'",
                    (workspace, claim.request_id, claim.owner_id, claim.epoch),
                )
                await db.commit()
                return cursor.rowcount == 1
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilitySandboxExecutionError(
                "sandbox_execution_claim_abandon_failed",
                "无法释放尚未开始的 Capability Sandbox 执行 claim。",
            ) from exc

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
                raise CapabilitySandboxExecutionError(
                    "sandbox_execution_store_init_failed",
                    "无法初始化 Capability Sandbox Execution Store。",
                ) from exc
            self._schema_ready = True


class EvolutionCapabilitySandboxExecutionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        request_service: EvolutionCapabilitySandboxRequestService,
        store: EvolutionCapabilitySandboxExecutionStore,
        harness_store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        execution_kernel: HarnessSandboxEvalExecutionKernel,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.request_service = request_service
        self.store = store
        self.harness_store = harness_store
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.execution_kernel = execution_kernel
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        if execution_kernel.workspace_root != self.workspace_root:
            raise ValueError("Capability Sandbox kernel 与 workspace 不一致。")
        if execution_kernel.permission_store is not permission_store:
            raise ValueError("Capability Sandbox kernel 与 Permission Store 不一致。")
        if execution_kernel.run_grant_authority is not run_grant_authority:
            raise ValueError("Capability Sandbox kernel 与 Run Grant authority 不一致。")
        self._execute_lock = asyncio.Lock()

    async def inspect(self, candidate_id: str) -> CapabilitySandboxExecutionView:
        request_view = await self.request_service.inspect(
            self.workspace_root,
            candidate_id,
        )
        request = request_view.request
        if request is None:
            return CapabilitySandboxExecutionView(
                candidate_id=candidate_id,
                receipt=None,
                state="missing",
                request_current=False,
            )
        receipt = await self.store.get(self.workspace_root, request.request_id)
        current = request_view.state == "ready"
        state = (
            "missing"
            if receipt is None
            else (
                "revoked"
                if not current
                else ("passed" if receipt.status == "passed" else "failed")
            )
        )
        return CapabilitySandboxExecutionView(
            candidate_id=request.candidate_id,
            receipt=receipt,
            state=state,
            request_current=current,
        )

    async def execute(
        self,
        *,
        candidate_id: str,
        run_id: str,
        parent_permission: PermissionDecisionReceipt,
    ) -> CapabilitySandboxExecutionView:
        async with self._execute_lock:
            return await self._execute_once(
                candidate_id=candidate_id,
                run_id=run_id,
                parent_permission=parent_permission,
            )

    async def _execute_once(
        self,
        *,
        candidate_id: str,
        run_id: str,
        parent_permission: PermissionDecisionReceipt,
    ) -> CapabilitySandboxExecutionView:
        request_view = await self.request_service.inspect(
            self.workspace_root,
            candidate_id,
        )
        request = request_view.request
        if request is None or request_view.state != "ready":
            raise self._error(
                "request_not_current",
                "Capability Sandbox Execution Request 不存在或已撤权。",
            )
        self._validate_parent(parent_permission, candidate_id, run_id)
        existing = await self.store.get(self.workspace_root, request.request_id)
        if existing is not None:
            return await self.inspect(candidate_id)
        unsupported = sorted(
            item.family
            for item in request.permissions
            if item.family in _UNSUPPORTED_PERMISSION_FAMILIES
        )
        if unsupported:
            raise self._error(
                "permission_adapter_unavailable",
                "当前 Sandbox 尚无可信隔离适配器：" + "、".join(unsupported) + "。",
            )
        runtime_identity = _runtime_identity(request)
        checks = tuple(
            HarnessCheckSpec(
                id=item.check_id,
                label=item.scenario_name,
                argv=item.argv,
                # The sealed driver enforces timeout_ms around Tool.execute().
                # ARC-04 needs bounded startup/teardown headroom that must not
                # silently shorten the semantic scenario timeout.
                timeout_seconds=min(3_600, item.timeout_seconds + 15),
                required_for=("change",),
                provides=("contract",),
                adversarial_probes=("boundary", "security"),
            )
            for item in request.checks
        )
        overlays = tuple(
            HarnessSandboxSourceOverlay(
                path=item.path,
                content=item.content_utf8.encode("utf-8"),
                sha256=item.sha256,
                executable=item.executable,
            )
            for item in request.overlays
        )

        async def request_is_current() -> bool:
            refreshed = await self.request_service.inspect(
                self.workspace_root,
                candidate_id,
            )
            return (
                refreshed.state == "ready"
                and refreshed.request is not None
                and refreshed.request.request_id == request.request_id
                and hmac.compare_digest(
                    refreshed.request.request_sha256,
                    request.request_sha256,
                )
            )

        owner_id = f"evcsexec_{uuid4().hex[:24]}"
        lease_seconds = min(
            3_600,
            max(60, sum(item.timeout_seconds + 15 for item in request.checks) + 30),
        )
        claim = await self.store.claim(
            self.workspace_root,
            request_id=request.request_id,
            owner_id=owner_id,
            now=self._now(),
            lease_seconds=lease_seconds,
        )
        if claim is None:
            existing = await self.store.get(self.workspace_root, request.request_id)
            if existing is not None:
                return await self.inspect(candidate_id)
            raise self._error(
                "execution_in_progress",
                "同一 Capability Sandbox Request 已有其他执行者持有 claim。",
            )
        lease = await self.harness_store.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=parent_permission.run_id,
            owner_id=owner_id,
            now=self._now(),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            await self.store.abandon(self.workspace_root, claim)
            raise self._error(
                "runtime_lease_unavailable",
                "Capability Sandbox 无法取得独占 Runtime lease。",
            )
        grant_id: str | None = None
        cleanup_errors: list[BaseException] = []
        try:
            grant = await self.run_grant_authority.issue(
                RunDelegationGrantRequest(
                    idempotency_key=(
                        f"ev-cap-sandbox-{request.request_sha256[:20]}-{lease.epoch}"
                    ),
                    parent_receipt_id=parent_permission.receipt_id,
                    run_kind=HarnessRunKind.RUNTIME,
                    lease_owner_id=owner_id,
                    lease_epoch=lease.epoch,
                    delegated_tool_names=("bash_run",),
                ),
                now=self._now(),
                ttl_seconds=lease_seconds,
            )
            grant_id = grant.contract.grant_id
            results = await self.execution_kernel.execute(
                lane="sandbox",
                authority_key=request.request_sha256,
                parent_receipt_id=parent_permission.receipt_id,
                sample_index=0,
                checks=checks,
                profile_digest=request.request_sha256,
                profile_is_current=request_is_current,
                source=HarnessSandboxEvalSource(
                    revision=request.source_revision,
                    revision_tree_sha256=request.source_tree_sha256,
                    overlays=overlays,
                    overlay_source_sha256=request.overlay_source_sha256,
                    source_is_current=request_is_current,
                ),
                run_authority=HarnessSandboxEvalRunAuthority(
                    parent_receipt_id=parent_permission.receipt_id,
                    run_id=parent_permission.run_id,
                    grant_id=grant.contract.grant_id,
                    grant_sha256=grant.contract.grant_sha256,
                ),
            )
            scenario_receipts = _scenario_receipts(request, results)
            if not await request_is_current():
                scenario_receipts = tuple(
                    item.model_copy(
                        update={
                            "status": "source_stale",
                            "actual_kind": "stale",
                            "actual_sha256": _digest({"kind": "stale"}),
                        }
                    )
                    for item in scenario_receipts
                )
            run_grant_sha256 = grant.contract.grant_sha256
        finally:
            cleanup_at = self._now()
            if grant_id is not None:
                try:
                    await self.run_grant_authority.revoke(
                        grant_id=grant_id,
                        reason="capability_sandbox_execution_finished",
                        revoked_at=cleanup_at,
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                released = await self.harness_store.release_run_lease(
                    workspace_root=self.workspace_root,
                    run_kind=HarnessRunKind.RUNTIME,
                    run_id=parent_permission.run_id,
                    owner_id=owner_id,
                    epoch=lease.epoch,
                    now=cleanup_at,
                )
                if released is None:
                    cleanup_errors.append(RuntimeError("Runtime lease 未释放。"))
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise self._error(
                "authority_cleanup_failed",
                "Capability Sandbox Run Grant 或 Runtime lease 清理不完整。",
            )
        receipt = _execution_receipt(
            request=request,
            parent_permission=parent_permission,
            run_grant_sha256=run_grant_sha256,
            runtime_identity_sha256=runtime_identity,
            scenarios=scenario_receipts,
            completed_at=self._now(),
        )
        stored = await self.store.record(
            self.workspace_root,
            receipt,
            claim=claim,
        )
        return CapabilitySandboxExecutionView(
            candidate_id=request.candidate_id,
            receipt=stored,
            state="passed" if stored.status == "passed" else "failed",
            request_current=True,
        )

    def _validate_parent(
        self,
        receipt: PermissionDecisionReceipt,
        candidate_id: str,
        run_id: str,
    ) -> None:
        expected_arguments = {"candidate_id": candidate_id, "run_id": run_id}
        if (
            not isinstance(receipt, PermissionDecisionReceipt)
            or not receipt.authorizes_execution
            or not receipt.run_id
            or receipt.run_id != run_id
            or receipt.tool_name != "evolution_capability_sandbox_execute"
            or "bash_run" not in receipt.delegated_tool_names
            or receipt.arguments_sha256 != permission_arguments_sha256(expected_arguments)
        ):
            raise self._error(
                "parent_permission_invalid",
                "Capability Sandbox 缺少与 Candidate 精确匹配的父权限回执。",
            )

    def _now(self) -> str:
        value = self.now()
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise self._error(
                "clock_invalid",
                "Capability Sandbox 运行时钟无效。",
            ) from exc
        if parsed.utcoffset() is None:
            raise self._error(
                "clock_invalid",
                "Capability Sandbox 运行时钟必须包含时区。",
            )
        return parsed.astimezone(UTC).isoformat()

    @staticmethod
    def _error(code: str, message: str) -> CapabilitySandboxExecutionError:
        return CapabilitySandboxExecutionError(f"capability_sandbox_{code}", message)


def _runtime_identity(request: EvolutionCapabilitySandboxExecutionRequest) -> str:
    executable = Path(request.python_executable).resolve(strict=True)
    current = Path(sys.executable).resolve(strict=True)
    if executable != current:
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_runtime_identity_drifted",
            "Capability Sandbox Python runtime 已偏离 Request。",
        )
    return _digest({
        "python_executable": str(executable),
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "driver_policy_version": request.driver_policy_version,
    })


def _scenario_receipts(
    request: EvolutionCapabilitySandboxExecutionRequest,
    results: tuple[HarnessSandboxCheckResult, ...],
) -> tuple[CapabilitySandboxScenarioExecutionReceipt, ...]:
    if len(results) != len(request.checks):
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_result_count_invalid",
            "Capability Sandbox Worker 结果数量与 Request 不一致。",
        )
    receipts: list[CapabilitySandboxScenarioExecutionReceipt] = []
    for check, result in zip(request.checks, results, strict=True):
        if result.check_id != check.check_id:
            raise CapabilitySandboxExecutionError(
                "capability_sandbox_result_identity_invalid",
                "Capability Sandbox Worker 结果顺序或 identity 无效。",
            )
        status, kind, actual, observations, observation_complete = _interpret_result(
            check.expectation_sha256,
            result,
        )
        if not result.job_id or not result.lifecycle_receipt_sha256:
            raise CapabilitySandboxExecutionError(
                "capability_sandbox_worker_evidence_missing",
                "Capability Sandbox 缺少 ARC-04 Worker lifecycle evidence。",
            )
        observation_models = tuple(
            CapabilitySandboxPermissionObservation.model_validate(item)
            for item in observations
        )
        receipts.append(CapabilitySandboxScenarioExecutionReceipt(
            order=check.order,
            check_id=check.check_id,
            expectation_sha256=check.expectation_sha256,
            status=status,
            actual_kind=kind,
            actual_sha256=_digest(actual),
            permission_observation_sha256=_digest(observations),
            permission_observation_complete=observation_complete,
            permission_observations=observation_models,
            job_id=result.job_id,
            lifecycle_receipt_sha256=result.lifecycle_receipt_sha256,
            snapshot_manifest_sha256=result.snapshot_manifest_sha256,
            duration_ms=result.duration_ms,
        ))
    return tuple(receipts)


def _interpret_result(
    expectation_sha256: str,
    result: HarnessSandboxCheckResult,
) -> tuple[CapabilityScenarioExecutionStatus, str, dict[str, Any], list[dict[str, str]], bool]:
    mapped = {
        HarnessSandboxCheckStatus.TIMED_OUT: ("timed_out", "timeout"),
        HarnessSandboxCheckStatus.RESOURCE_LIMIT: ("resource_limit", "resource_limit"),
        HarnessSandboxCheckStatus.CANCELLED: ("cancelled", "cancelled"),
        HarnessSandboxCheckStatus.STALE: ("source_stale", "stale"),
        HarnessSandboxCheckStatus.BLOCKED: (
            "infrastructure_failure",
            "infrastructure_error",
        ),
        HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR: (
            "infrastructure_failure",
            "infrastructure_error",
        ),
        HarnessSandboxCheckStatus.FAILED: (
            "infrastructure_failure",
            "infrastructure_error",
        ),
    }
    if result.status in mapped:
        status, kind = mapped[result.status]
        return status, kind, {"kind": kind}, [], False
    try:
        payload = json.loads(result.output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_driver_output_invalid",
            "Capability Sandbox driver 输出不是单一 JSON envelope。",
        ) from exc
    if not isinstance(payload, dict):
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_driver_output_invalid",
            "Capability Sandbox driver 输出结构无效。",
        )
    observation = payload.pop("permission_observation", None)
    if (
        observation is None
        and payload.get("kind") == "infrastructure_error"
        and set(payload) == {"kind", "error_code"}
        and isinstance(payload.get("error_code"), str)
        and payload["error_code"]
    ):
        actual = {
            "kind": "infrastructure_error",
            "error_code": payload["error_code"],
        }
        return "infrastructure_failure", "infrastructure_error", actual, [], False
    if not isinstance(observation, dict):
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_permission_observation_missing",
            "Capability Sandbox driver 未返回权限观察证据。",
        )
    events = observation.get("events")
    violations = observation.get("violations")
    complete = observation.get("complete")
    if (
        not isinstance(events, list)
        or not isinstance(violations, list)
        or not isinstance(complete, bool)
        or len(events) + len(violations) > 256
    ):
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_permission_observation_invalid",
            "Capability Sandbox 权限观察证据无效或超限。",
        )
    observations = [*events, *violations]
    for item in observations:
        CapabilitySandboxPermissionObservation.model_validate(item)
    kind = payload.get("kind")
    if kind == "result" and set(payload) == {"kind", "value"}:
        actual = {"kind": "result", "value": payload["value"], "error_code": ""}
        status: CapabilityScenarioExecutionStatus = (
            "passed" if _digest(actual) == expectation_sha256 else "oracle_mismatch"
        )
    elif kind == "error" and set(payload) == {"kind", "error_code", "retryable"}:
        actual = {"kind": "error", "value": None, "error_code": payload["error_code"]}
        status = (
            "passed"
            if _digest(actual) == expectation_sha256
            else "declared_error_mismatch"
        )
    elif kind == "timeout" and set(payload) == {"kind"}:
        actual = {"kind": "timeout"}
        status = "timed_out"
    elif kind == "permission_violation" and set(payload) == {"kind"}:
        actual = {"kind": "permission_violation"}
        status = "permission_violation"
    elif kind == "infrastructure_error" and set(payload) == {"kind", "error_code"}:
        actual = {
            "kind": "infrastructure_error",
            "error_code": payload["error_code"],
        }
        status = "infrastructure_failure"
    else:
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_driver_output_invalid",
            "Capability Sandbox driver envelope 含未知或多余字段。",
        )
    if violations and status not in {"permission_violation", "infrastructure_failure"}:
        status = "permission_violation"
        actual = {"kind": "permission_violation"}
        kind = "permission_violation"
    return status, str(kind), actual, observations, complete


def _execution_receipt(
    *,
    request: EvolutionCapabilitySandboxExecutionRequest,
    parent_permission: PermissionDecisionReceipt,
    run_grant_sha256: str,
    runtime_identity_sha256: str,
    scenarios: tuple[CapabilitySandboxScenarioExecutionReceipt, ...],
    completed_at: str,
) -> EvolutionCapabilitySandboxExecutionReceipt:
    all_passed = all(item.status == "passed" for item in scenarios)
    any_stale = any(item.status == "source_stale" for item in scenarios)
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "candidate_id": request.candidate_id,
        "binding_id": request.binding_id,
        "source_revision": request.source_revision,
        "source_tree_sha256": request.source_tree_sha256,
        "overlay_source_sha256": request.overlay_source_sha256,
        "parent_permission_receipt_id": parent_permission.receipt_id,
        "parent_permission_receipt_sha256": parent_permission.receipt_sha256,
        "run_grant_sha256": run_grant_sha256,
        "runtime_identity_sha256": runtime_identity_sha256,
        "scenarios": [item.model_dump(mode="json") for item in scenarios],
        "status": "stale" if any_stale else ("passed" if all_passed else "failed"),
        "all_scenarios_executed": True,
        "all_scenarios_passed": all_passed,
        "permission_observation_complete": all(
            item.permission_observation_complete for item in scenarios
        ),
        "exact_revision_materialized": True,
        "arc04_worker_used": True,
        "run_grant_revoked": True,
        "runtime_lease_released": True,
        "sandbox_execution_completed": True,
        "registry_authorized": False,
        "shadow_authorized": False,
        "executable": False,
        "completed_at": completed_at,
    }
    digest = _digest(payload)
    return EvolutionCapabilitySandboxExecutionReceipt.model_validate({
        **payload,
        "receipt_id": f"evcser_{digest[:24]}",
        "receipt_sha256": digest,
    })


def render_capability_sandbox_execution(view: CapabilitySandboxExecutionView) -> str:
    lines = ["# Capability Sandbox Execution", ""]
    if view.receipt is None:
        lines.extend([
            "尚未形成隔离执行回执。",
            "",
            "- Registry：否 · Shadow：否 · 可执行：否",
        ])
        return "\n".join(lines)
    receipt = view.receipt
    passed = sum(item.status == "passed" for item in receipt.scenarios)
    lines.extend([
        f"- Receipt：`{receipt.receipt_id}`",
        f"- 状态：`{view.state}`",
        f"- 场景：{len(receipt.scenarios)} · 通过：{passed}",
        f"- Request 当前：{'是' if view.request_current else '否'}",
        f"- 权限观察完整：{'是' if receipt.permission_observation_complete else '否'}",
        "- exact revision：是 · ARC-04 Worker：是 · Run Grant/Runtime lease：已清理",
        "- Registry：否 · Shadow：否 · 可执行：否",
    ])
    for item in receipt.scenarios:
        lines.append(f"- `{item.check_id}` · `{item.status}` · {item.duration_ms}ms")
    return "\n".join(lines)


def _restore_receipt(
    payload: str,
    payload_sha256: str,
) -> EvolutionCapabilitySandboxExecutionReceipt:
    if not hmac.compare_digest(hashlib.sha256(payload.encode()).hexdigest(), payload_sha256):
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_execution_store_tampered",
            "Capability Sandbox Execution Receipt 持久摘要不一致。",
        )
    try:
        return EvolutionCapabilitySandboxExecutionReceipt.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise CapabilitySandboxExecutionError(
            "capability_sandbox_execution_store_invalid",
            "Capability Sandbox Execution Receipt JSON 损坏。",
        ) from exc


def _claim_from_row(row: Any) -> CapabilitySandboxExecutionClaim:
    try:
        return CapabilitySandboxExecutionClaim(
            request_id=str(row[0]),
            owner_id=str(row[1]),
            epoch=int(row[2]),
            acquired_at=str(row[3]),
            expires_at=str(row[4]),
            state=str(row[5]),
        )
    except (TypeError, ValueError) as exc:
        raise CapabilitySandboxExecutionError(
            "sandbox_execution_claim_corrupt",
            "Capability Sandbox 持久执行 claim 已损坏。",
        ) from exc


def _aware_time(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO-8601 时间。") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed.astimezone(UTC)


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
CREATE TABLE IF NOT EXISTS evolution_capability_sandbox_execution_claims (
    workspace_root TEXT NOT NULL,
    request_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    state TEXT NOT NULL CHECK (state IN ('active', 'terminal')),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, request_id)
);

CREATE TABLE IF NOT EXISTS evolution_capability_sandbox_execution_receipts (
    workspace_root TEXT NOT NULL,
    request_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, request_id),
    UNIQUE (workspace_root, receipt_id)
);
"""


__all__ = [
    "CapabilitySandboxExecutionClaim",
    "CapabilitySandboxExecutionError",
    "CapabilitySandboxExecutionView",
    "CapabilitySandboxPermissionObservation",
    "CapabilitySandboxScenarioExecutionReceipt",
    "EvolutionCapabilitySandboxExecutionReceipt",
    "EvolutionCapabilitySandboxExecutionService",
    "EvolutionCapabilitySandboxExecutionStore",
    "render_capability_sandbox_execution",
]
