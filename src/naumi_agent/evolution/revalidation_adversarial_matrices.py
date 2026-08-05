"""Fail-closed Fresh Adversarial required-platform matrix authority."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionDecision,
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerHealthReport,
    WorkerIsolationContract,
    WorkerKind,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_adversarial_cohorts import (
    EvolutionRevalidationAdversarialCohortReceipt,
    EvolutionRevalidationAdversarialCohortStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)

EVOLUTION_REVALIDATION_ADVERSARIAL_MATRIX_POLICY = (
    "evolution-revalidation-adversarial-matrix-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
type Platform = Literal["linux", "macos", "windows"]
type LaneStatus = Literal["completed", "runnable", "pending"]

_CAPABILITIES = tuple(sorted((
    WorkerCapability.ARTIFACT_DIGEST,
    WorkerCapability.ENVIRONMENT_ALLOWLIST,
    WorkerCapability.NETWORK_POLICY,
    WorkerCapability.PROCESS_TREE_CANCEL,
    WorkerCapability.RESOURCE_LIMITS,
    WorkerCapability.SHELL_NON_PTY,
    WorkerCapability.WORKSPACE_EPHEMERAL,
), key=str))
_ISOLATION = WorkerIsolationContract(True, True, True, True, True, True)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationAdversarialMatrixLane(_StrictModel):
    platform: Platform
    status: LaneStatus
    cohort_receipt_id: str | None = Field(
        default=None, pattern=r"^evrevaladvcohort_[0-9a-f]{24}$"
    )
    cohort_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    worker_id: str | None = Field(default=None, min_length=1, max_length=128)
    worker_instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    worker_epoch: int | None = Field(default=None, ge=1)
    worker_contract_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        cohort = (self.cohort_receipt_id, self.cohort_receipt_sha256)
        worker = (
            self.worker_id,
            self.worker_instance_id,
            self.worker_epoch,
            self.worker_contract_sha256,
        )
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("Fresh Adversarial matrix reason codes 必须去重排序。")
        if self.status == "completed":
            if any(item is None for item in cohort) or any(item is not None for item in worker):
                raise ValueError("Completed lane 必须且只能绑定 cohort。")
            if self.reason_codes != ("cohort_complete",):
                raise ValueError("Completed lane reason code 无效。")
        elif self.status == "runnable":
            if any(item is not None for item in cohort) or any(item is None for item in worker):
                raise ValueError("Runnable lane 必须且只能绑定已准入 Worker。")
            if self.reason_codes != ("worker_admitted",):
                raise ValueError("Runnable lane reason code 无效。")
        elif any(item is not None for item in (*cohort, *worker)):
            raise ValueError("Pending lane 不能绑定执行权威。")
        return self


class EvolutionRevalidationAdversarialMatrixStatus(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-adversarial-matrix-v1"] = (
        EVOLUTION_REVALIDATION_ADVERSARIAL_MATRIX_POLICY
    )
    matrix_id: str = Field(pattern=r"^evrevaladvmatrix_[0-9a-f]{24}$")
    matrix_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    lanes: tuple[EvolutionRevalidationAdversarialMatrixLane, ...] = Field(
        min_length=1, max_length=3
    )
    completed_platforms: tuple[Platform, ...]
    runnable_platforms: tuple[Platform, ...]
    pending_platforms: tuple[Platform, ...]
    matrix_complete: bool
    dispatch_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    assessed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Adversarial matrix workspace 必须 canonical。")
        platforms = tuple(item.platform for item in self.lanes)
        if len(set(platforms)) != len(platforms):
            raise ValueError("Fresh Adversarial matrix platform lane 不能重复。")
        projections = {
            status: tuple(item.platform for item in self.lanes if item.status == status)
            for status in ("completed", "runnable", "pending")
        }
        if (
            self.completed_platforms != projections["completed"]
            or self.runnable_platforms != projections["runnable"]
            or self.pending_platforms != projections["pending"]
        ):
            raise ValueError("Fresh Adversarial matrix lane 投影不一致。")
        if self.matrix_complete is not all(item.status == "completed" for item in self.lanes):
            raise ValueError("Fresh Adversarial matrix_complete 投影不一致。")
        if datetime.fromisoformat(self.assessed_at).utcoffset() is None:
            raise ValueError("Fresh Adversarial matrix assessed_at 必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"matrix_id", "matrix_sha256"})
        )
        if not hmac.compare_digest(self.matrix_sha256, digest):
            raise ValueError("Fresh Adversarial matrix digest 不一致。")
        if self.matrix_id != f"evrevaladvmatrix_{digest[:24]}":
            raise ValueError("Fresh Adversarial matrix identity 不一致。")
        return self


class EvolutionRevalidationAdversarialMatrixError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationAdversarialMatrixStore:
    """Persist only complete matrices; incomplete status remains a live observation."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get(self, contract_id: str):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT status_json FROM evolution_revalidation_adversarial_matrices "
                "WHERE contract_id = ?", (contract_id,)
            )).fetchone()
        if row is None:
            return None
        return EvolutionRevalidationAdversarialMatrixStatus.model_validate_json(
            row["status_json"]
        )

    async def platform_completion_gate(self, contract_id: str, platform: Platform):
        if not self._db_path.exists():
            return False, None, None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            dispatch_table = await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                    "name = 'evolution_revalidation_platform_dispatches'"
                )
            ).fetchone()
            dispatch = None
            if dispatch_table is not None:
                dispatch = await (
                    await db.execute(
                        "SELECT 1 FROM evolution_revalidation_platform_dispatches "
                        "WHERE contract_id = ? AND platform = ?",
                        (contract_id, platform),
                    )
                ).fetchone()
            completion_table = await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                    "name = 'evolution_revalidation_platform_completions'"
                )
            ).fetchone()
            completion = None
            if completion_table is not None:
                completion = await (
                    await db.execute(
                        "SELECT cohort_receipt_id, cohort_receipt_sha256 FROM "
                        "evolution_revalidation_platform_completions "
                        "WHERE contract_id = ? AND platform = ?",
                        (contract_id, platform),
                    )
                ).fetchone()
        return (
            dispatch is not None,
            None if completion is None else str(completion["cohort_receipt_id"]),
            None if completion is None else str(completion["cohort_receipt_sha256"]),
        )

    async def record_complete(self, status: EvolutionRevalidationAdversarialMatrixStatus):
        item = EvolutionRevalidationAdversarialMatrixStatus.model_validate_json(
            status.model_dump_json()
        )
        if not item.matrix_complete:
            raise EvolutionRevalidationAdversarialMatrixError(
                "fresh_adversarial_matrix_incomplete",
                "不完整 Fresh Adversarial matrix 不能持久化为完成权威。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (await db.execute(
                "SELECT contract_sha256 FROM evolution_revalidation_runtime_contracts "
                "WHERE contract_id = ?", (item.contract_id,)
            )).fetchone()
            if dependency is None or dependency["contract_sha256"] != item.contract_sha256:
                await db.rollback()
                raise EvolutionRevalidationAdversarialMatrixError(
                    "fresh_adversarial_matrix_contract_mismatch",
                    "持久化 Runtime Contract 不存在或 digest 不一致。",
                )
            for lane in item.lanes:
                cohort = await (await db.execute(
                    "SELECT receipt_id, receipt_sha256 FROM "
                    "evolution_revalidation_adversarial_cohorts "
                    "WHERE contract_id = ? AND platform = ?",
                    (item.contract_id, lane.platform),
                )).fetchone()
                if cohort is None or (
                    cohort["receipt_id"] != lane.cohort_receipt_id
                    or cohort["receipt_sha256"] != lane.cohort_receipt_sha256
                ):
                    await db.rollback()
                    raise EvolutionRevalidationAdversarialMatrixError(
                        "fresh_adversarial_matrix_cohort_dependency_mismatch",
                        f"平台 {lane.platform} 的 cohort 持久化依赖不一致。",
                    )
                dispatch_table = await (
                    await db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                        "name = 'evolution_revalidation_platform_dispatches'"
                    )
                ).fetchone()
                dispatch = None
                if dispatch_table is not None:
                    dispatch = await (
                        await db.execute(
                            "SELECT 1 FROM evolution_revalidation_platform_dispatches "
                            "WHERE contract_id = ? AND platform = ?",
                            (item.contract_id, lane.platform),
                        )
                    ).fetchone()
                if dispatch is not None:
                    completion_table = await (
                        await db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                            "name = 'evolution_revalidation_platform_completions'"
                        )
                    ).fetchone()
                    completion = None
                    if completion_table is not None:
                        completion = await (
                            await db.execute(
                                "SELECT cohort_receipt_id, cohort_receipt_sha256 FROM "
                                "evolution_revalidation_platform_completions "
                                "WHERE contract_id = ? AND platform = ?",
                                (item.contract_id, lane.platform),
                            )
                        ).fetchone()
                    if completion is None or (
                        completion["cohort_receipt_id"] != lane.cohort_receipt_id
                        or completion["cohort_receipt_sha256"]
                        != lane.cohort_receipt_sha256
                    ):
                        await db.rollback()
                        raise EvolutionRevalidationAdversarialMatrixError(
                            "fresh_adversarial_matrix_platform_completion_missing",
                            f"平台 {lane.platform} 的 remote completion 尚未收口。",
                        )
            existing = await (await db.execute(
                "SELECT status_json FROM evolution_revalidation_adversarial_matrices "
                "WHERE contract_id = ?", (item.contract_id,)
            )).fetchone()
            if existing is not None:
                restored = EvolutionRevalidationAdversarialMatrixStatus.model_validate_json(
                    existing["status_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationAdversarialMatrixError(
                        "fresh_adversarial_matrix_conflict",
                        "Runtime Contract 已绑定不同 Fresh Adversarial matrix。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_adversarial_matrices "
                "(matrix_id, matrix_sha256, contract_id, status_json, assessed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.matrix_id, item.matrix_sha256, item.contract_id,
                 item.model_dump_json(), item.assessed_at),
            )
            await db.commit()
        return item


class EvolutionRevalidationAdversarialMatrixService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_service: EvolutionRevalidationRuntimeContractService,
        cohort_store: EvolutionRevalidationAdversarialCohortStore,
        matrix_store: EvolutionRevalidationAdversarialMatrixStore,
        worker_registry: WorkerRegistryStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service = contract_service
        self.cohort_store = cohort_store
        self.matrix_store = matrix_store
        self.worker_registry = worker_registry

    async def inspect(
        self,
        *,
        contract_id: str,
        worker_health_reports: tuple[WorkerHealthReport, ...] = (),
        assessed_at: str | None = None,
    ) -> EvolutionRevalidationAdversarialMatrixStatus:
        now = assessed_at or datetime.now(UTC).isoformat()
        if datetime.fromisoformat(now).utcoffset() is None:
            raise EvolutionRevalidationAdversarialMatrixError(
                "fresh_adversarial_matrix_clock_invalid", "矩阵评估时钟必须包含 offset。"
            )
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationAdversarialMatrixError(
                "fresh_adversarial_matrix_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        contract = view.contract
        existing = await self.matrix_store.get(contract_id)
        if existing is not None:
            await self._validate_complete(existing, contract)
            return existing

        admitted, rejected = await self._admitted_workers(
            contract, worker_health_reports, now
        )
        lanes = []
        for platform in contract.required_platforms:
            cohort = await self.cohort_store.get(contract.contract_id, platform)
            if cohort is not None:
                _validate_cohort(cohort, contract, platform)
                required, completion_id, completion_sha256 = (
                    await self.matrix_store.platform_completion_gate(
                        contract.contract_id,
                        platform,
                    )
                )
                if required and completion_id is None:
                    lanes.append(EvolutionRevalidationAdversarialMatrixLane(
                        platform=platform,
                        status="pending",
                        reason_codes=("platform_completion_pending",),
                    ))
                    continue
                if required and (
                    completion_id != cohort.receipt_id
                    or completion_sha256 != cohort.receipt_sha256
                ):
                    raise EvolutionRevalidationAdversarialMatrixError(
                        "fresh_adversarial_matrix_platform_completion_changed",
                        f"平台 {platform} completion 与 cohort 不一致。",
                    )
                lanes.append(EvolutionRevalidationAdversarialMatrixLane(
                    platform=platform,
                    status="completed",
                    cohort_receipt_id=cohort.receipt_id,
                    cohort_receipt_sha256=cohort.receipt_sha256,
                    reason_codes=("cohort_complete",),
                ))
                continue
            workers = admitted.get(platform, ())
            if workers:
                registration = workers[0]
                worker = registration.contract
                lanes.append(EvolutionRevalidationAdversarialMatrixLane(
                    platform=platform,
                    status="runnable",
                    worker_id=worker.worker_id,
                    worker_instance_id=worker.instance_id,
                    worker_epoch=worker.epoch,
                    worker_contract_sha256=worker.contract_sha256,
                    reason_codes=("worker_admitted",),
                ))
            else:
                reasons = rejected.get(platform) or (
                    "healthy_platform_worker_missing",
                )
                lanes.append(EvolutionRevalidationAdversarialMatrixLane(
                    platform=platform,
                    status="pending",
                    reason_codes=reasons,
                ))
        status = _build_status(contract, tuple(lanes), now)
        return await self.matrix_store.record_complete(status) if status.matrix_complete else status

    async def _admitted_workers(self, contract, reports, now):
        selected: dict[str, list] = {}
        rejected: dict[str, set[str]] = {}
        seen = set()
        for report in reports:
            worker_id = report.heartbeat.subject_id
            if worker_id in seen:
                raise EvolutionRevalidationAdversarialMatrixError(
                    "fresh_adversarial_matrix_duplicate_worker_report",
                    f"Worker {worker_id} 提交了重复健康报告。",
                )
            seen.add(worker_id)
            registration = await self.worker_registry.get_active(worker_id)
            if registration is None:
                continue
            system = registration.contract.platform.system
            platform = "macos" if system == "darwin" else system
            if platform not in contract.required_platforms:
                continue
            result = await self.worker_registry.assess_admission(
                worker_id=worker_id,
                report=report,
                requirements=_requirements(platform, contract.max_total_duration_seconds),
                now=now,
            )
            if result.decision is WorkerAdmissionDecision.ADMITTED:
                selected.setdefault(platform, []).append(registration)
            else:
                rejected.setdefault(platform, set()).update(
                    f"worker_{reason.value}" for reason in result.reasons
                )
        admitted = {
            platform: tuple(sorted(items, key=lambda item: (
                item.contract.worker_id, -item.contract.epoch
            )))
            for platform, items in selected.items()
        }
        rejection_codes = {
            platform: tuple(sorted(reasons))
            for platform, reasons in rejected.items()
        }
        return admitted, rejection_codes

    async def _validate_complete(self, status, contract):
        if (
            not status.matrix_complete
            or status.workspace_root != contract.workspace_root
            or status.contract_sha256 != contract.contract_sha256
            or tuple(item.platform for item in status.lanes) != contract.required_platforms
        ):
            raise EvolutionRevalidationAdversarialMatrixError(
                "fresh_adversarial_matrix_stale", "持久化 matrix 与当前 Runtime Contract 不一致。"
            )
        for lane in status.lanes:
            cohort = await self.cohort_store.get(contract.contract_id, lane.platform)
            if cohort is None:
                raise EvolutionRevalidationAdversarialMatrixError(
                    "fresh_adversarial_matrix_cohort_missing", "完成 matrix 的 cohort 已缺失。"
                )
            _validate_cohort(cohort, contract, lane.platform)
            if (
                lane.cohort_receipt_id != cohort.receipt_id
                or lane.cohort_receipt_sha256 != cohort.receipt_sha256
            ):
                raise EvolutionRevalidationAdversarialMatrixError(
                    "fresh_adversarial_matrix_cohort_changed",
                    "完成 matrix 的 cohort identity 已变化。",
                )
            required, completion_id, completion_sha256 = (
                await self.matrix_store.platform_completion_gate(
                    contract.contract_id,
                    lane.platform,
                )
            )
            if required and (
                completion_id != cohort.receipt_id
                or completion_sha256 != cohort.receipt_sha256
            ):
                raise EvolutionRevalidationAdversarialMatrixError(
                    "fresh_adversarial_matrix_platform_completion_missing",
                    "完成 matrix 的 remote platform completion 已缺失。",
                )


def _requirements(platform: Platform, wall_seconds: int):
    system = "darwin" if platform == "macos" else platform
    return WorkerAdmissionRequirements(
        kind=WorkerKind.TOOL,
        protocol_version=1,
        capabilities=_CAPABILITIES,
        allowed_platforms=(system,),
        min_wall_seconds=wall_seconds,
        isolation=_ISOLATION,
    )


def _validate_cohort(receipt, contract, platform):
    if not isinstance(receipt, EvolutionRevalidationAdversarialCohortReceipt) or (
        receipt.workspace_root != contract.workspace_root
        or receipt.contract_id != contract.contract_id
        or receipt.contract_sha256 != contract.contract_sha256
        or receipt.validation_plan_id != contract.validation_plan_id
        or receipt.validation_plan_sha256 != contract.validation_plan_sha256
        or receipt.source_snapshot_id != contract.source_snapshot_id
        or receipt.source_snapshot_sha256 != contract.source_snapshot_sha256
        or receipt.platform != platform
        or receipt.suite_id != contract.suite_id
        or receipt.requested_samples != contract.requested_samples
    ):
        raise EvolutionRevalidationAdversarialMatrixError(
            "fresh_adversarial_matrix_cohort_mismatch",
            f"平台 {platform} 的 Fresh Adversarial cohort 与 Runtime Contract 不一致。",
        )


def _build_status(contract, lanes, assessed_at):
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ADVERSARIAL_MATRIX_POLICY,
        "workspace_root": contract.workspace_root,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "validation_plan_id": contract.validation_plan_id,
        "validation_plan_sha256": contract.validation_plan_sha256,
        "source_snapshot_id": contract.source_snapshot_id,
        "source_snapshot_sha256": contract.source_snapshot_sha256,
        "lanes": [item.model_dump(mode="json") for item in lanes],
        "completed_platforms": [item.platform for item in lanes if item.status == "completed"],
        "runnable_platforms": [item.platform for item in lanes if item.status == "runnable"],
        "pending_platforms": [item.platform for item in lanes if item.status == "pending"],
        "matrix_complete": all(item.status == "completed" for item in lanes),
        "dispatch_authority": False,
        "comparison_authority": False,
        "promotion_authority": False,
        "assessed_at": assessed_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionRevalidationAdversarialMatrixStatus.model_validate({
        **payload,
        "matrix_id": f"evrevaladvmatrix_{digest[:24]}",
        "matrix_sha256": digest,
    })


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_adversarial_matrices ("
        "matrix_id TEXT PRIMARY KEY, matrix_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL UNIQUE, status_json TEXT NOT NULL, assessed_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_ADVERSARIAL_MATRIX_POLICY",
    "EvolutionRevalidationAdversarialMatrixError",
    "EvolutionRevalidationAdversarialMatrixLane",
    "EvolutionRevalidationAdversarialMatrixService",
    "EvolutionRevalidationAdversarialMatrixStatus",
    "EvolutionRevalidationAdversarialMatrixStore",
]
