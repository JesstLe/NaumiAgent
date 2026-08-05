"""Durable queued dispatch authority for required-platform evaluation workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.worker_contract import WorkerHealthReport
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityExhaustedError,
    WorkerRegistryConflictError,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)
from naumi_agent.evolution.revalidation_adversarial_matrices import (
    EvolutionRevalidationAdversarialMatrixError,
    EvolutionRevalidationAdversarialMatrixService,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)

EVOLUTION_REVALIDATION_PLATFORM_DISPATCH_POLICY = "evolution-revalidation-platform-dispatch-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
type Platform = Literal["linux", "macos", "windows"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationPlatformDispatch(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-platform-dispatch-v1"] = (
        EVOLUTION_REVALIDATION_PLATFORM_DISPATCH_POLICY
    )
    dispatch_id: str = Field(pattern=r"^evrevalplatdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evrevalplatjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evrevalplatres_[0-9a-f]{24}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    platform: Platform
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    requested_samples: int = Field(ge=5, le=100)
    seed: int = Field(ge=0)
    probe_registry_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    reservation_expires_at: str = Field(min_length=1, max_length=100)
    state: Literal["queued"] = "queued"
    attempt: Literal[1] = 1
    capacity_reserved: Literal[True] = True
    worker_claimed: Literal[False] = False
    transport_delivered: Literal[False] = False
    execution_started: Literal[False] = False
    result_received: Literal[False] = False
    cohort_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    queued_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Platform Dispatch workspace 必须 canonical。")
        queued = _aware(self.queued_at)
        if _aware(self.reservation_expires_at) <= queued:
            raise ValueError("Platform Dispatch reservation expiry 无效。")
        core = self.model_dump(
            mode="json", exclude={"dispatch_id", "dispatch_sha256", "job_id", "reservation_id"}
        )
        digest = _digest(core)
        if self.dispatch_sha256 != digest:
            raise ValueError("Platform Dispatch digest 不一致。")
        suffix = digest[:24]
        if not (
            self.dispatch_id == f"evrevalplatdispatch_{suffix}"
            and self.job_id == f"evrevalplatjob_{suffix}"
            and self.reservation_id == f"evrevalplatres_{suffix}"
        ):
            raise ValueError("Platform Dispatch identities 不一致。")
        return self


class EvolutionRevalidationPlatformDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPlatformDispatchStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(self, contract_id: str, platform: Platform):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT dispatch_json FROM evolution_revalidation_platform_dispatches "
                    "WHERE contract_id = ? AND platform = ?",
                    (contract_id, platform),
                )
            ).fetchone()
        return None if row is None else _from_json(row["dispatch_json"])

    async def record(self, dispatch: EvolutionRevalidationPlatformDispatch):
        item = EvolutionRevalidationPlatformDispatch.model_validate_json(dispatch.model_dump_json())
        encoded = item.model_dump_json()
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
            if contract is None or contract["contract_sha256"] != item.contract_sha256:
                await db.rollback()
                raise EvolutionRevalidationPlatformDispatchError(
                    "platform_dispatch_contract_mismatch",
                    "Platform Dispatch Runtime Contract authority 不一致。",
                )
            existing = await (
                await db.execute(
                    "SELECT dispatch_json FROM evolution_revalidation_platform_dispatches "
                    "WHERE contract_id = ? AND platform = ?",
                    (item.contract_id, item.platform),
                )
            ).fetchone()
            if existing is not None:
                restored = _from_json(existing["dispatch_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformDispatchError(
                        "platform_dispatch_conflict",
                        "同一 Contract/Platform 已绑定不同 Dispatch。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_dispatches "
                "(dispatch_id, dispatch_sha256, contract_id, platform, worker_id, "
                "worker_instance_id, worker_epoch, reservation_id, state, "
                "dispatch_json, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.dispatch_id,
                    item.dispatch_sha256,
                    item.contract_id,
                    item.platform,
                    item.worker_id,
                    item.worker_instance_id,
                    item.worker_epoch,
                    item.reservation_id,
                    item.state,
                    encoded,
                    item.queued_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationPlatformDispatchService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_service: EvolutionRevalidationRuntimeContractService,
        matrix_service: EvolutionRevalidationAdversarialMatrixService,
        worker_registry: WorkerRegistryStore,
        store: EvolutionRevalidationPlatformDispatchStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service = contract_service
        self.matrix_service = matrix_service
        self.worker_registry = worker_registry
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def queue(
        self,
        *,
        contract_id: str,
        platform: Platform,
        worker_health_reports: tuple[WorkerHealthReport, ...],
        queued_at: str | None = None,
    ) -> EvolutionRevalidationPlatformDispatch:
        lock = self._locks.setdefault(f"{contract_id}:{platform}", asyncio.Lock())
        async with lock:
            now = queued_at or datetime.now(UTC).isoformat()
            _aware(now)
            existing = await self.store.get(contract_id, platform)
            if existing is not None:
                return await self._validate_existing(existing, assessed_at=now)
            try:
                matrix = await self.matrix_service.inspect(
                    contract_id=contract_id,
                    worker_health_reports=worker_health_reports,
                    assessed_at=now,
                )
            except EvolutionRevalidationAdversarialMatrixError as exc:
                raise EvolutionRevalidationPlatformDispatchError(
                    "platform_dispatch_matrix_failed",
                    "无法取得 current required-platform matrix。",
                ) from exc
            lane = next((item for item in matrix.lanes if item.platform == platform), None)
            if lane is None or lane.status != "runnable":
                state = "missing" if lane is None else lane.status
                raise EvolutionRevalidationPlatformDispatchError(
                    "platform_dispatch_lane_not_runnable",
                    f"平台 {platform} lane 当前为 {state}，不能排队。",
                )
            view = await self.contract_service.inspect(
                workspace_root=self.workspace_root,
                contract_id=contract_id,
            )
            if not view.execution_eligible:
                raise EvolutionRevalidationPlatformDispatchError(
                    "platform_dispatch_contract_not_ready",
                    "Runtime Contract 不再具备执行资格。",
                )
            contract = view.contract
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_REVALIDATION_PLATFORM_DISPATCH_POLICY,
                "workspace_root": contract.workspace_root,
                "contract_id": contract.contract_id,
                "contract_sha256": contract.contract_sha256,
                "validation_plan_id": contract.validation_plan_id,
                "validation_plan_sha256": contract.validation_plan_sha256,
                "source_snapshot_id": contract.source_snapshot_id,
                "source_snapshot_sha256": contract.source_snapshot_sha256,
                "platform": platform,
                "suite_id": contract.suite_id,
                "requested_samples": contract.requested_samples,
                "seed": contract.seed,
                "probe_registry_sha256": contract.probe_registry_sha256,
                "worker_id": lane.worker_id,
                "worker_instance_id": lane.worker_instance_id,
                "worker_epoch": lane.worker_epoch,
                "worker_contract_sha256": lane.worker_contract_sha256,
                "reservation_expires_at": (
                    _aware(now) + _duration(contract.max_total_duration_seconds)
                ).isoformat(),
                "state": "queued",
                "attempt": 1,
                "capacity_reserved": True,
                "worker_claimed": False,
                "transport_delivered": False,
                "execution_started": False,
                "result_received": False,
                "cohort_authority": False,
                "comparison_authority": False,
                "promotion_authority": False,
                "queued_at": _aware(now).isoformat(),
            }
            digest = _digest(core)
            suffix = digest[:24]
            item = EvolutionRevalidationPlatformDispatch.model_validate(
                {
                    **core,
                    "dispatch_id": f"evrevalplatdispatch_{suffix}",
                    "dispatch_sha256": digest,
                    "job_id": f"evrevalplatjob_{suffix}",
                    "reservation_id": f"evrevalplatres_{suffix}",
                }
            )
            try:
                reservation = await self.worker_registry.reserve_capacity(
                    reservation_id=item.reservation_id,
                    worker_id=item.worker_id,
                    instance_id=item.worker_instance_id,
                    epoch=item.worker_epoch,
                    job_id=item.job_id,
                    reserved_at=item.queued_at,
                    ttl_seconds=contract.max_total_duration_seconds,
                )
                if reservation.expires_at != item.reservation_expires_at:
                    raise EvolutionRevalidationPlatformDispatchError(
                        "platform_dispatch_reservation_mismatch",
                        "Worker reservation expiry 与 Dispatch 不一致。",
                    )
                return await self.store.record(item)
            except (
                WorkerCapacityExhaustedError,
                WorkerRegistryConflictError,
                WorkerRegistryStoreError,
            ) as exc:
                raise EvolutionRevalidationPlatformDispatchError(
                    "platform_dispatch_capacity_failed",
                    "无法为 exact Worker incarnation 预留容量。",
                ) from exc
            except (
                EvolutionRevalidationPlatformDispatchError,
                aiosqlite.Error,
                OSError,
                TypeError,
                ValueError,
            ):
                await self._release(item)
                raise

    async def _validate_existing(self, item, *, assessed_at: str):
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=item.contract_id,
        )
        reservation = await self.worker_registry.get_capacity_reservation(
            item.reservation_id,
            assessed_at=assessed_at,
        )
        if not (
            view.execution_eligible
            and view.contract.contract_sha256 == item.contract_sha256
            and reservation is not None
            and reservation.worker_id == item.worker_id
            and reservation.instance_id == item.worker_instance_id
            and reservation.epoch == item.worker_epoch
            and reservation.job_id == item.job_id
            and reservation.state.value == "active"
        ):
            raise EvolutionRevalidationPlatformDispatchError(
                "platform_dispatch_stale",
                "Queued Platform Dispatch 的 Contract、Worker 或 reservation 已失效。",
            )
        return item

    async def _release(self, item):
        try:
            await self.worker_registry.release_capacity(
                reservation_id=item.reservation_id,
                worker_id=item.worker_id,
                instance_id=item.worker_instance_id,
                epoch=item.worker_epoch,
                released_at=datetime.now(UTC).isoformat(),
                reason_code="dispatch_record_failed",
            )
        except (WorkerRegistryStoreError, ValueError):
            pass


def _duration(seconds: int):
    return timedelta(seconds=seconds)


def _aware(value: str):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Platform Dispatch 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _from_json(value):
    return EvolutionRevalidationPlatformDispatch.model_validate_json(value)


async def _ensure_schema(db):
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_dispatches (
        dispatch_id TEXT PRIMARY KEY,
        dispatch_sha256 TEXT NOT NULL UNIQUE,
        contract_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        worker_instance_id TEXT NOT NULL,
        worker_epoch INTEGER NOT NULL,
        reservation_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL,
        dispatch_json TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        UNIQUE(contract_id, platform))"""
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PLATFORM_DISPATCH_POLICY",
    "EvolutionRevalidationPlatformDispatch",
    "EvolutionRevalidationPlatformDispatchError",
    "EvolutionRevalidationPlatformDispatchService",
    "EvolutionRevalidationPlatformDispatchStore",
]
