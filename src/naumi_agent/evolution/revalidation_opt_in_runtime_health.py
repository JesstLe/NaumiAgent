"""Fenced opt-in runtime launch and terminal health observation receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_deployments import (
    EvolutionRevalidationOptInDeploymentError,
    EvolutionRevalidationOptInDeploymentReceipt,
    EvolutionRevalidationOptInDeploymentService,
    EvolutionRevalidationOptInDeploymentStore,
)
from naumi_agent.release.launcher import (
    ReleaseLaunchResolution,
    resolve_and_record_launch,
)
from naumi_agent.release.runtime_health import (
    ReleaseRuntimeHealthError,
    ReleaseRuntimeHealthReport,
    parse_runtime_health_report,
)
from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore
from naumi_agent.validation.executor import (
    CommandExecutionResult,
    CommandExecutionStatus,
    ValidationExecutor,
)

EVOLUTION_REVALIDATION_OPT_IN_RUNTIME_HEALTH_POLICY = (
    "evolution-revalidation-opt-in-runtime-health-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_LEASE_SECONDS = 60.0
_SAFE_ENVIRONMENT_NAMES = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInRuntimeHealthReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-runtime-health-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_RUNTIME_HEALTH_POLICY
    receipt_id: str = Field(pattern=r"^evreruntimehealth_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    install_root: str = Field(min_length=1, max_length=4096)
    deployment: EvolutionRevalidationOptInDeploymentReceipt
    launch_resolution: ReleaseLaunchResolution
    health_report: ReleaseRuntimeHealthReport | None = None
    outcome: Literal["healthy", "unhealthy"]
    failure_code: str = Field(pattern=r"^(?:|[a-z0-9_]{1,128})$")
    execution_status: CommandExecutionStatus
    exit_code: int | None = None
    captured_output_text_sha256: str = Field(pattern=_SHA256_RE)
    output_bytes: int = Field(ge=0)
    output_truncated: bool
    duration_ms: int = Field(ge=0)
    process_started: bool
    user_session_started: Literal[False] = False
    runtime_health_authority: bool
    opt_in_stage_completion_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    started_at: str = Field(min_length=1, max_length=100)
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Runtime Health Receipt workspace_root 必须 canonical。")
        if self.install_root != str(Path(self.install_root).expanduser().resolve()):
            raise ValueError("Runtime Health Receipt install_root 必须 canonical。")
        started = _aware(self.started_at)
        completed = _aware(self.completed_at)
        if completed < started or self.started_at != self.launch_resolution.resolved_at:
            raise ValueError("Runtime Health Receipt 执行时间窗口无效。")
        if not _launch_matches_deployment(self.launch_resolution, self.deployment):
            raise ValueError("Runtime Health Receipt launch/deployment binding 不一致。")
        healthy = bool(
            self.outcome == "healthy"
            and not self.failure_code
            and self.execution_status is CommandExecutionStatus.PASSED
            and self.exit_code == 0
            and not self.output_truncated
            and self.process_started
            and self.health_report is not None
            and _report_matches(
                self.health_report,
                self.launch_resolution,
                self.install_root,
            )
            and started <= _aware(self.health_report.checked_at) <= completed
        )
        if self.runtime_health_authority is not healthy:
            raise ValueError("Runtime Health Receipt authority projection 不一致。")
        if self.outcome == "healthy" and not healthy:
            raise ValueError("Healthy Runtime Health Receipt 缺少完整成功证据。")
        if self.outcome == "unhealthy" and not self.failure_code:
            raise ValueError("Unhealthy Runtime Health Receipt 必须提供 failure_code。")
        if self.health_report is not None and not (
            self.execution_status is CommandExecutionStatus.PASSED
            and self.exit_code == 0
            and not self.output_truncated
            and self.process_started
        ):
            raise ValueError("Runtime Health Report 只能绑定完整成功的进程输出。")
        if self.execution_status is CommandExecutionStatus.INFRASTRUCTURE_ERROR:
            if self.process_started:
                raise ValueError("基础设施启动失败不能声明 process_started。")
        elif not self.process_started:
            raise ValueError("已进入子进程执行路径必须声明 process_started。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evreruntimehealth_{digest[:24]}"
        ):
            raise ValueError("Runtime Health Receipt identity 不一致。")
        return self


class EvolutionRevalidationOptInRuntimeHealthView(_StrictModel):
    receipt: EvolutionRevalidationOptInRuntimeHealthReceipt
    deployment_source_current: bool
    active_deployment_authority: bool
    launch_fact_current: bool
    report_binding_current: bool
    runtime_health_authority: bool
    opt_in_stage_completion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.receipt.runtime_health_authority
            and self.deployment_source_current
            and self.active_deployment_authority
            and self.launch_fact_current
            and self.report_binding_current
        )
        if self.runtime_health_authority is not current:
            raise ValueError("Runtime Health View authority projection 不一致。")
        return self


class EvolutionRevalidationOptInRuntimeHealthError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _RuntimeHealthClaim(_StrictModel):
    deployment_receipt_id: str
    epoch: int = Field(ge=1)
    owner_token: str = Field(min_length=64, max_length=64)
    expires_at: str


class EvolutionRevalidationOptInRuntimeHealthStore:
    """Durable single-owner probe claim with epoch fencing and terminal receipt."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        deployment_store: EvolutionRevalidationOptInDeploymentStore,
        release_slot_store: ReleaseSlotStore,
    ) -> None:
        if not isinstance(deployment_store, EvolutionRevalidationOptInDeploymentStore):
            raise TypeError("Runtime Health Store 需要 Deployment Store。")
        if not isinstance(release_slot_store, ReleaseSlotStore):
            raise TypeError("Runtime Health Store 需要 ReleaseSlotStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        self.deployment_store = deployment_store
        self.release_slot_store = release_slot_store

    async def get_by_deployment(
        self, deployment_receipt_id: str
    ) -> EvolutionRevalidationOptInRuntimeHealthReceipt | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_opt_in_runtime_health_attempts "
                    "WHERE deployment_receipt_id = ? AND receipt_json != ''",
                    (deployment_receipt_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def claim(
        self,
        *,
        deployment: EvolutionRevalidationOptInDeploymentReceipt,
        now: datetime,
        lease_seconds: float,
    ) -> tuple[
        _RuntimeHealthClaim | None,
        EvolutionRevalidationOptInRuntimeHealthReceipt | None,
    ]:
        current = _aware(now)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Runtime Health lease_seconds 必须大于 0。")
        item = _validated_deployment(deployment)
        owner = secrets.token_hex(32)
        owner_sha = hashlib.sha256(owner.encode()).hexdigest()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_source_deployment(db, item)
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_opt_in_runtime_health_attempts "
                    "WHERE deployment_receipt_id = ?",
                    (item.receipt_id,),
                )
            ).fetchone()
            if row is not None and row["receipt_json"]:
                receipt = _restore_receipt(row["receipt_json"])
                await db.rollback()
                return None, receipt
            if row is not None and _aware(row["expires_at"]) > current:
                await db.rollback()
                raise EvolutionRevalidationOptInRuntimeHealthError(
                    "opt_in_runtime_health_claim_busy",
                    "同一 Opt-in Deployment 正由另一个健康观测执行者处理。",
                )
            epoch = 1 if row is None else int(row["epoch"]) + 1
            expires_at = (current + timedelta(seconds=lease_seconds)).isoformat()
            values = (
                item.receipt_sha256,
                epoch,
                owner_sha,
                expires_at,
                current.isoformat(),
                item.receipt_id,
            )
            if row is None:
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_runtime_health_attempts "
                    "(deployment_receipt_id, deployment_receipt_sha256, epoch, "
                    "owner_sha256, state, expires_at, receipt_json, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, '', ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        epoch,
                        owner_sha,
                        expires_at,
                        current.isoformat(),
                    ),
                )
            else:
                if row["deployment_receipt_sha256"] != item.receipt_sha256:
                    await db.rollback()
                    raise EvolutionRevalidationOptInRuntimeHealthError(
                        "opt_in_runtime_health_claim_conflict",
                        "Runtime Health claim 已绑定不同 Deployment Receipt。",
                    )
                await db.execute(
                    "UPDATE evolution_revalidation_opt_in_runtime_health_attempts "
                    "SET deployment_receipt_sha256 = ?, epoch = ?, owner_sha256 = ?, "
                    "state = 'active', expires_at = ?, updated_at = ? "
                    "WHERE deployment_receipt_id = ?",
                    values,
                )
            await db.commit()
        return (
            _RuntimeHealthClaim(
                deployment_receipt_id=item.receipt_id,
                epoch=epoch,
                owner_token=owner,
                expires_at=expires_at,
            ),
            None,
        )

    async def finish(
        self,
        *,
        claim: _RuntimeHealthClaim,
        receipt: EvolutionRevalidationOptInRuntimeHealthReceipt,
        now: datetime,
    ) -> EvolutionRevalidationOptInRuntimeHealthReceipt:
        item = _validated_receipt(receipt)
        if item.deployment.receipt_id != claim.deployment_receipt_id:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_claim_receipt_mismatch",
                "Runtime Health Receipt 未绑定 claim 的 Deployment。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_RECEIPT_BYTES:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_receipt_oversized",
                "Runtime Health Receipt 超过 4 MiB。",
            )
        try:
            launch = await asyncio.to_thread(
                self.release_slot_store.get_launch_resolution,
                item.launch_resolution.resolution_id,
            )
        except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_launch_fact_invalid",
                "无法验证 ARC-07 Launch Resolution 历史事实。",
            ) from exc
        if launch != item.launch_resolution:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_launch_fact_changed",
                "Runtime Health Receipt 未绑定 exact Launch Resolution。",
            )
        owner_sha = hashlib.sha256(claim.owner_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_source_deployment(db, item.deployment)
            cursor = await db.execute(
                "UPDATE evolution_revalidation_opt_in_runtime_health_attempts "
                "SET state = ?, receipt_json = ?, updated_at = ? "
                "WHERE deployment_receipt_id = ? AND epoch = ? AND owner_sha256 = ? "
                "AND state = 'active' AND receipt_json = '' AND expires_at > ?",
                (
                    item.outcome,
                    encoded,
                    _aware(now).isoformat(),
                    claim.deployment_receipt_id,
                    claim.epoch,
                    owner_sha,
                    _aware(now).isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise EvolutionRevalidationOptInRuntimeHealthError(
                    "opt_in_runtime_health_fenced",
                    "Runtime Health 执行者 epoch 已失效，禁止提交终态回执。",
                )
            await db.commit()
        return item

    async def abandon(self, *, claim: _RuntimeHealthClaim) -> None:
        owner_sha = hashlib.sha256(claim.owner_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM evolution_revalidation_opt_in_runtime_health_attempts "
                "WHERE deployment_receipt_id = ? AND epoch = ? AND owner_sha256 = ? "
                "AND state = 'active' AND receipt_json = ''",
                (
                    claim.deployment_receipt_id,
                    claim.epoch,
                    owner_sha,
                ),
            )
            await db.commit()


class EvolutionRevalidationOptInRuntimeHealthService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        deployment_service: EvolutionRevalidationOptInDeploymentService,
        store: EvolutionRevalidationOptInRuntimeHealthStore,
        executor: ValidationExecutor | None = None,
        clock: Callable[[], datetime] | None = None,
        environment_provider: Callable[[], Mapping[str, str]] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
    ) -> None:
        if not isinstance(
            deployment_service, EvolutionRevalidationOptInDeploymentService
        ):
            raise TypeError("Runtime Health Service 需要 Deployment Service。")
        if not isinstance(store, EvolutionRevalidationOptInRuntimeHealthStore):
            raise TypeError("Runtime Health Service 需要 Runtime Health Store。")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Runtime Health timeout_seconds 必须大于 0。")
        if not math.isfinite(lease_seconds) or lease_seconds <= timeout_seconds + 5:
            raise ValueError("Runtime Health lease 必须至少覆盖 timeout 加 5 秒提交窗口。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.deployment_service = deployment_service
        self.store = store
        self.release_slot_store = store.release_slot_store
        self.executor = executor or ValidationExecutor(output_limit_bytes=64 * 1024)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.environment_provider = environment_provider or (lambda: os.environ)
        self.timeout_seconds = timeout_seconds
        self.lease_seconds = lease_seconds

    async def observe(
        self,
        *,
        completion_id: str,
        cancel_event: asyncio.Event | None = None,
    ) -> EvolutionRevalidationOptInRuntimeHealthView:
        deployment_view = await self._deployment_view(completion_id)
        deployment = deployment_view.receipt
        existing = await self.store.get_by_deployment(deployment.receipt_id)
        if existing is not None:
            return await self._view(existing)
        if not deployment_view.active_deployment_authority:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_deployment_stale",
                "Opt-in Deployment 当前没有 runtime launch authority。",
            )
        now = self._now()
        try:
            claim, terminal = await self.store.claim(
                deployment=deployment,
                now=now,
                lease_seconds=self.lease_seconds,
            )
        except EvolutionRevalidationOptInRuntimeHealthError as exc:
            if exc.code != "opt_in_runtime_health_claim_busy":
                raise
            terminal = await self._await_terminal(deployment.receipt_id)
            if terminal is None:
                raise
            return await self._view(terminal)
        if terminal is not None:
            return await self._view(terminal)
        assert claim is not None
        try:
            resolution = await asyncio.to_thread(
                resolve_and_record_launch,
                self.release_slot_store,
                argument_count=1,
                process_start_requested=True,
                resolved_at=now.isoformat(),
            )
            if not _launch_matches_deployment(resolution, deployment):
                raise EvolutionRevalidationOptInRuntimeHealthError(
                    "opt_in_runtime_health_launch_fenced",
                    "Stable launcher 已解析到不同 active deployment。",
                )
        except (
            OSError,
            TypeError,
            ValueError,
            ReleaseSlotError,
            EvolutionRevalidationOptInRuntimeHealthError,
        ) as exc:
            await self.store.abandon(claim=claim)
            if isinstance(exc, EvolutionRevalidationOptInRuntimeHealthError):
                raise
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_launch_failed",
                "Stable launcher 无法形成 exact Runtime Health 启动事实。",
            ) from exc
        environment = _controlled_environment(
            self.environment_provider(),
            resolution=resolution,
            install_root=self.release_slot_store.release_root,
        )
        result = await self.executor.run(
            argv=(resolution.backend_path, "--runtime-health-check"),
            cwd=self.release_slot_store.release_root,
            timeout_seconds=self.timeout_seconds,
            cancel_event=cancel_event,
            env=environment,
        )
        completed = self._now()
        report, outcome, failure_code = _classify_result(
            result,
            resolution=resolution,
            install_root=self.release_slot_store.release_root,
            started_at=now,
            completed_at=completed,
        )
        receipt = _build_receipt(
            workspace_root=self.workspace_root,
            install_root=self.release_slot_store.release_root,
            deployment=deployment,
            resolution=resolution,
            result=result,
            report=report,
            outcome=outcome,
            failure_code=failure_code,
            completed_at=completed,
        )
        stored = await self.store.finish(claim=claim, receipt=receipt, now=completed)
        return await self._view(stored)

    async def reconcile(
        self,
        *,
        completion_id: str,
        cancel_event: asyncio.Event | None = None,
    ) -> EvolutionRevalidationOptInRuntimeHealthView:
        return await self.observe(
            completion_id=completion_id,
            cancel_event=cancel_event,
        )

    async def inspect(
        self, *, completion_id: str
    ) -> EvolutionRevalidationOptInRuntimeHealthView:
        deployment_view = await self._deployment_view(completion_id)
        receipt = await self.store.get_by_deployment(deployment_view.receipt.receipt_id)
        if receipt is None:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_receipt_missing",
                "尚未形成 Opt-in Runtime Health Receipt。",
            )
        return await self._view(receipt)

    async def _deployment_view(self, completion_id: str):
        try:
            return await self.deployment_service.inspect(completion_id=completion_id)
        except EvolutionRevalidationOptInDeploymentError as exc:
            raise EvolutionRevalidationOptInRuntimeHealthError(
                "opt_in_runtime_health_deployment_missing",
                "缺少可供健康观测的 Opt-in Deployment Receipt。",
            ) from exc

    async def _view(self, receipt):
        source = await self.store.deployment_store.get_by_completion(
            receipt.deployment.intent.admission.completion_id
        )
        deployment_source_current = source == receipt.deployment
        try:
            deployment_view = await self.deployment_service.inspect(
                completion_id=receipt.deployment.intent.admission.completion_id
            )
            active_deployment_authority = bool(
                deployment_view.receipt == receipt.deployment
                and deployment_view.active_deployment_authority
            )
        except EvolutionRevalidationOptInDeploymentError:
            active_deployment_authority = False
        try:
            launch = await asyncio.to_thread(
                self.release_slot_store.get_launch_resolution,
                receipt.launch_resolution.resolution_id,
            )
            launch_fact_current = launch == receipt.launch_resolution
        except (OSError, TypeError, ValueError, ReleaseSlotError):
            launch_fact_current = False
        report_binding_current = bool(
            receipt.health_report is not None
            and _report_matches(
                receipt.health_report,
                receipt.launch_resolution,
                receipt.install_root,
            )
        )
        current = bool(
            receipt.runtime_health_authority
            and deployment_source_current
            and active_deployment_authority
            and launch_fact_current
            and report_binding_current
        )
        return EvolutionRevalidationOptInRuntimeHealthView(
            receipt=receipt,
            deployment_source_current=deployment_source_current,
            active_deployment_authority=active_deployment_authority,
            launch_fact_current=launch_fact_current,
            report_binding_current=report_binding_current,
            runtime_health_authority=current,
        )

    def _now(self) -> datetime:
        return _aware(self.clock())

    async def _await_terminal(self, deployment_receipt_id: str):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds + 5
        while loop.time() < deadline:
            receipt = await self.store.get_by_deployment(deployment_receipt_id)
            if receipt is not None:
                return receipt
            await asyncio.sleep(0.02)
        return None


def _controlled_environment(
    source: Mapping[str, str],
    *,
    resolution: ReleaseLaunchResolution,
    install_root: str | Path,
) -> dict[str, str]:
    environment = {
        name: str(source[name])
        for name in _SAFE_ENVIRONMENT_NAMES
        if name in source and str(source[name])
    }
    environment.update(
        {
            "NAUMI_ACTIVE_SLOT_ID": resolution.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(resolution.pointer_generation),
            "NAUMI_INSTALL_ROOT": str(Path(install_root).expanduser().resolve()),
        }
    )
    return environment


def _classify_result(
    result: CommandExecutionResult,
    *,
    resolution: ReleaseLaunchResolution,
    install_root: str | Path,
    started_at: datetime,
    completed_at: datetime,
):
    report = None
    if result.status is CommandExecutionStatus.INFRASTRUCTURE_ERROR:
        return report, "unhealthy", "runtime_health_process_start_failed"
    if result.status is CommandExecutionStatus.TIMED_OUT:
        return report, "unhealthy", "runtime_health_timed_out"
    if result.status is CommandExecutionStatus.CANCELLED:
        return report, "unhealthy", "runtime_health_cancelled"
    if result.status is CommandExecutionStatus.FAILED or result.exit_code != 0:
        return report, "unhealthy", "runtime_health_nonzero_exit"
    if result.output_truncated:
        return report, "unhealthy", "runtime_health_output_truncated"
    try:
        report = parse_runtime_health_report(result.output)
    except ReleaseRuntimeHealthError:
        return None, "unhealthy", "runtime_health_report_invalid"
    if not (
        _report_matches(report, resolution, install_root)
        and _aware(started_at) <= _aware(report.checked_at) <= _aware(completed_at)
    ):
        return report, "unhealthy", "runtime_health_report_mismatch"
    return report, "healthy", ""


def _build_receipt(
    *,
    workspace_root: str | Path,
    install_root: str | Path,
    deployment: EvolutionRevalidationOptInDeploymentReceipt,
    resolution: ReleaseLaunchResolution,
    result: CommandExecutionResult,
    report: ReleaseRuntimeHealthReport | None,
    outcome: Literal["healthy", "unhealthy"],
    failure_code: str,
    completed_at: datetime,
) -> EvolutionRevalidationOptInRuntimeHealthReceipt:
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_RUNTIME_HEALTH_POLICY,
        "workspace_root": str(Path(workspace_root).expanduser().resolve()),
        "install_root": str(Path(install_root).expanduser().resolve()),
        "deployment": deployment.model_dump(mode="json"),
        "launch_resolution": resolution.model_dump(mode="json"),
        "health_report": None if report is None else report.model_dump(mode="json"),
        "outcome": outcome,
        "failure_code": failure_code,
        "execution_status": result.status.value,
        "exit_code": result.exit_code,
        "captured_output_text_sha256": hashlib.sha256(
            result.output.encode("utf-8")
        ).hexdigest(),
        "output_bytes": result.output_bytes,
        "output_truncated": result.output_truncated,
        "duration_ms": result.duration_ms,
        "process_started": result.status is not CommandExecutionStatus.INFRASTRUCTURE_ERROR,
        "user_session_started": False,
        "runtime_health_authority": outcome == "healthy",
        "opt_in_stage_completion_authority": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
        "started_at": resolution.resolved_at,
        "completed_at": _aware(completed_at).isoformat(),
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInRuntimeHealthReceipt.model_validate(
        {
            **core,
            "deployment": deployment,
            "launch_resolution": resolution,
            "health_report": report,
            "receipt_id": f"evreruntimehealth_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _launch_matches_deployment(
    resolution: ReleaseLaunchResolution,
    deployment: EvolutionRevalidationOptInDeploymentReceipt,
) -> bool:
    pointer = deployment.activated_pointer
    admission = deployment.intent.admission
    return bool(
        resolution.process_start_requested
        and resolution.process_start_authority
        and not resolution.process_started
        and resolution.argument_count == 1
        and resolution.pointer_id == pointer.pointer_id
        and resolution.pointer_sha256 == pointer.pointer_sha256
        and resolution.pointer_generation == pointer.generation
        and resolution.slot_id == admission.candidate_slot.slot_id
        and resolution.slot_sha256 == admission.candidate_slot.slot_sha256
        and resolution.boot_receipt_id == admission.boot_receipt.receipt_id
        and resolution.boot_receipt_sha256 == admission.boot_receipt.receipt_sha256
        and resolution.binary_sha256 == admission.boot_receipt.binary_sha256
    )


def _report_matches(
    report: ReleaseRuntimeHealthReport,
    resolution: ReleaseLaunchResolution,
    install_root: str | Path,
) -> bool:
    return bool(
        report.pointer_id == resolution.pointer_id
        and report.pointer_sha256 == resolution.pointer_sha256
        and report.pointer_generation == resolution.pointer_generation
        and report.slot_id == resolution.slot_id
        and report.slot_sha256 == resolution.slot_sha256
        and report.version == resolution.version
        and report.target == resolution.target
        and report.boot_receipt_id == resolution.boot_receipt_id
        and report.boot_receipt_sha256 == resolution.boot_receipt_sha256
        and report.binary_sha256 == resolution.binary_sha256
        and os.path.normcase(report.runtime_path)
        == os.path.normcase(resolution.backend_path)
        and os.path.normcase(report.install_root)
        == os.path.normcase(str(Path(install_root).expanduser().resolve()))
        and report.process_started
        and not report.user_session_started
    )


def _validated_deployment(value):
    try:
        return EvolutionRevalidationOptInDeploymentReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInRuntimeHealthError(
            "opt_in_runtime_health_deployment_invalid",
            "Runtime Health source Deployment Receipt 无效。",
        ) from exc


def _validated_receipt(value):
    try:
        return EvolutionRevalidationOptInRuntimeHealthReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInRuntimeHealthError(
            "opt_in_runtime_health_receipt_invalid",
            "Opt-in Runtime Health Receipt artifact 无效。",
        ) from exc


def _restore_receipt(encoded: str):
    try:
        return EvolutionRevalidationOptInRuntimeHealthReceipt.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInRuntimeHealthError(
            "opt_in_runtime_health_store_corrupt",
            "Stored Runtime Health Receipt 无法验证。",
        ) from exc


async def _require_source_deployment(db, item) -> None:
    row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_revalidation_opt_in_deployments "
            "WHERE receipt_id = ?",
            (item.receipt_id,),
        )
    ).fetchone()
    try:
        source = (
            None
            if row is None
            else EvolutionRevalidationOptInDeploymentReceipt.model_validate_json(
                row["receipt_json"]
            )
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInRuntimeHealthError(
            "opt_in_runtime_health_deployment_corrupt",
            "Stored Deployment Receipt 无法验证。",
        ) from exc
    if source != item:
        raise EvolutionRevalidationOptInRuntimeHealthError(
            "opt_in_runtime_health_deployment_changed",
            "Runtime Health source Deployment Receipt 已变化或不存在。",
        )


async def _ensure_schema(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_runtime_health_attempts (
            deployment_receipt_id TEXT PRIMARY KEY,
            deployment_receipt_sha256 TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            owner_sha256 TEXT NOT NULL,
            state TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Runtime Health 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_RUNTIME_HEALTH_POLICY",
    "EvolutionRevalidationOptInRuntimeHealthError",
    "EvolutionRevalidationOptInRuntimeHealthReceipt",
    "EvolutionRevalidationOptInRuntimeHealthService",
    "EvolutionRevalidationOptInRuntimeHealthStore",
    "EvolutionRevalidationOptInRuntimeHealthView",
]
