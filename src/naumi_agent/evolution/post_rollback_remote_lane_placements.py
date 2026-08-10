"""Non-executing placement for one remote post-rollback behavioral lane."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerContract,
    WorkerIsolationContract,
    WorkerKind,
    verify_worker_contract,
)
from naumi_agent.daemons.worker_registry import (
    WorkerRegistrationState,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageContract,
    EvolutionPostRollbackBehavioralCoverageError,
    EvolutionPostRollbackBehavioralCoverageLane,
    EvolutionPostRollbackBehavioralCoverageService,
    EvolutionPostRollbackBehavioralCoverageStore,
)

EVOLUTION_POST_ROLLBACK_REMOTE_LANE_PLACEMENT_POLICY = (
    "evolution-post-rollback-remote-lane-placement-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 256 * 1024
_REQUIRED_CAPABILITIES = tuple(
    sorted(
        (
            WorkerCapability.ARTIFACT_DIGEST,
            WorkerCapability.ENVIRONMENT_ALLOWLIST,
            WorkerCapability.NETWORK_POLICY,
            WorkerCapability.PROCESS_TREE_CANCEL,
            WorkerCapability.RESOURCE_LIMITS,
            WorkerCapability.SHELL_NON_PTY,
            WorkerCapability.WORKSPACE_EPHEMERAL,
        ),
        key=str,
    )
)
_REQUIRED_ISOLATION = WorkerIsolationContract(True, True, True, True, True, True)
type Platform = Literal["linux", "macos", "windows"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRemoteLanePlacement(_StrictModel):
    """Exact worker target selection without health, capacity, or execution authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-lane-placement-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_LANE_PLACEMENT_POLICY
    )
    placement_id: str = Field(pattern=r"^evpostplacement_[0-9a-f]{24}$")
    placement_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    coverage_contract_id: str = Field(pattern=r"^evpostcoverage_[0-9a-f]{24}$")
    coverage_contract_sha256: str = Field(pattern=_SHA256_RE)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    workbench_proposal_id: str = Field(min_length=1, max_length=128)
    lane: EvolutionPostRollbackBehavioralCoverageLane
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    worker_kind: Literal["tool"] = "tool"
    worker_protocol_version: Literal[1] = 1
    worker_software_version: str = Field(min_length=1, max_length=64)
    worker_platform_system: Literal["darwin", "linux", "windows"]
    worker_platform_machine: str = Field(min_length=1, max_length=64)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    required_capabilities: tuple[WorkerCapability, ...] = _REQUIRED_CAPABILITIES
    required_isolation: WorkerIsolationContract = _REQUIRED_ISOLATION
    state: Literal["placed"] = "placed"
    worker_registration_verified: Literal[True] = True
    target_compatibility_verified: Literal[True] = True
    health_verified: Literal[False] = False
    capacity_reserved: Literal[False] = False
    baseline_resolved: Literal[False] = False
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    placed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Remote Lane Placement workspace 必须 canonical。")
        _aware(self.placed_at)
        platform = _system_platform(self.worker_platform_system)
        if not (
            self.lane.remote_target_required
            and not self.lane.local_execution_eligible
            and self.lane.platform == platform
            and self.release_target
            == _release_target(self.worker_platform_system, self.worker_platform_machine)
            and self.required_capabilities == _REQUIRED_CAPABILITIES
            and self.required_isolation == _REQUIRED_ISOLATION
        ):
            raise ValueError("Remote Lane Placement target/capability projection 不一致。")
        if any(
            (
                self.health_verified,
                self.capacity_reserved,
                self.baseline_resolved,
                self.transport_delivered,
                self.execution_authority,
                self.result_authority,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Remote Lane Placement 不得扩大权威。")
        digest = _digest(self.model_dump(mode="json", exclude={"placement_id", "placement_sha256"}))
        if not (
            hmac.compare_digest(self.placement_sha256, digest)
            and self.placement_id == f"evpostplacement_{digest[:24]}"
        ):
            raise ValueError("Remote Lane Placement identity 不一致。")
        return self


class EvolutionPostRollbackRemoteLanePlacementView(_StrictModel):
    placement: EvolutionPostRollbackRemoteLanePlacement
    durable_source_valid: bool
    coverage_authority: bool
    lane_still_missing: bool
    worker_registration_active: bool
    worker_contract_current: bool
    placement_authority: bool
    health_authority: Literal[False] = False
    capacity_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_source_valid
            and self.coverage_authority
            and self.lane_still_missing
            and self.worker_registration_active
            and self.worker_contract_current
        )
        if self.placement_authority is not expected:
            raise ValueError("Remote Lane Placement authority 投影不一致。")
        return self


class EvolutionPostRollbackRemoteLanePlacementError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRemoteLanePlacementStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        coverage_contract_id: str,
        comparison_id: str,
    ) -> EvolutionPostRollbackRemoteLanePlacement | None:
        _coverage_id(coverage_contract_id)
        _comparison_id(comparison_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT placement_json FROM "
                        "evolution_post_rollback_remote_lane_placements "
                        "WHERE coverage_contract_id = ? AND comparison_id = ?",
                        (coverage_contract_id, comparison_id),
                    )
                ).fetchone()
            return None if row is None else _restore(row["placement_json"])
        except EvolutionPostRollbackRemoteLanePlacementError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_store_corrupt",
                "Remote Lane Placement 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        placement: EvolutionPostRollbackRemoteLanePlacement,
    ) -> EvolutionPostRollbackRemoteLanePlacement:
        try:
            item = EvolutionPostRollbackRemoteLanePlacement.model_validate_json(
                placement.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_invalid",
                "Remote Lane Placement artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_oversized",
                "Remote Lane Placement 超过 256 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                dependency = await (
                    await db.execute(
                        "SELECT contract_sha256 FROM "
                        "evolution_post_rollback_behavioral_coverage "
                        "WHERE contract_id = ?",
                        (item.coverage_contract_id,),
                    )
                ).fetchone()
                if (
                    dependency is None
                    or dependency["contract_sha256"] != item.coverage_contract_sha256
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteLanePlacementError(
                        "post_rollback_placement_dependency_mismatch",
                        "Placement 的 Coverage Contract durable authority 不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT placement_json FROM "
                        "evolution_post_rollback_remote_lane_placements "
                        "WHERE coverage_contract_id = ? AND comparison_id = ?",
                        (item.coverage_contract_id, item.lane.original_comparison_id),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["placement_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionPostRollbackRemoteLanePlacementError(
                            "post_rollback_placement_conflict",
                            "同一 Coverage Lane 已绑定不同 Worker placement。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_lane_placements "
                    "(placement_id, placement_sha256, coverage_contract_id, "
                    "comparison_id, worker_id, worker_epoch, placement_json, placed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.placement_id,
                        item.placement_sha256,
                        item.coverage_contract_id,
                        item.lane.original_comparison_id,
                        item.worker_id,
                        item.worker_epoch,
                        encoded,
                        item.placed_at,
                    ),
                )
                await db.commit()
        except EvolutionPostRollbackRemoteLanePlacementError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_store_error",
                "Remote Lane Placement 无法持久化。",
            ) from exc
        return item


class EvolutionPostRollbackRemoteLanePlacementService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        coverage_store: EvolutionPostRollbackBehavioralCoverageStore,
        coverage_service: EvolutionPostRollbackBehavioralCoverageService,
        worker_registry: WorkerRegistryStore,
        store: EvolutionPostRollbackRemoteLanePlacementStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.coverage_store = coverage_store
        self.coverage_service = coverage_service
        self.worker_registry = worker_registry
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def place(
        self,
        *,
        request_id: str,
        comparison_id: str,
        worker_id: str,
        placed_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteLanePlacementView:
        request = _request_id(request_id)
        comparison = _comparison_id(comparison_id)
        worker = _worker_id(worker_id)
        lock = self._locks.setdefault(f"{request}:{comparison}", asyncio.Lock())
        async with lock:
            coverage = await self.coverage_service.record(request_id=request)
            expected, lane_view = _remote_missing_lane(coverage, comparison)
            existing = await self.store.get(
                coverage.contract.contract_id,
                comparison,
            )
            if existing is not None:
                if existing.worker_id != worker:
                    raise EvolutionPostRollbackRemoteLanePlacementError(
                        "post_rollback_placement_conflict",
                        "该远端 lane 已绑定其他 Worker。",
                    )
                view = await self.inspect(placement=existing)
                if not view.placement_authority:
                    raise EvolutionPostRollbackRemoteLanePlacementError(
                        "post_rollback_placement_stale",
                        "既有 Remote Lane Placement authority 已失效。",
                    )
                return view
            registration = await self._active_worker(worker, expected.platform)
            artifact = _build_placement(
                coverage=coverage.contract,
                lane=expected,
                worker=registration.contract,
                placed_at=placed_at or datetime.now(UTC).isoformat(),
            )
            recorded = await self.store.record(artifact)
            view = await self.inspect(placement=recorded)
            if not view.placement_authority or not lane_view.dispatch_required:
                raise EvolutionPostRollbackRemoteLanePlacementError(
                    "post_rollback_placement_authority_changed",
                    "Remote Lane Placement 持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        placement: EvolutionPostRollbackRemoteLanePlacement,
    ) -> EvolutionPostRollbackRemoteLanePlacementView:
        item = EvolutionPostRollbackRemoteLanePlacement.model_validate_json(
            placement.model_dump_json()
        )
        flags = {
            "durable": False,
            "coverage": False,
            "missing": False,
            "active": False,
            "worker": False,
        }
        try:
            durable = await self.store.get(
                item.coverage_contract_id,
                item.lane.original_comparison_id,
            )
            contract = await self.coverage_store.get_by_outcome(item.outcome_id)
            if contract is None:
                raise ValueError("Coverage identity mismatch")
            _require_coverage_lineage(item, contract)
            coverage = await self.coverage_service.inspect(contract=contract)
            selected = tuple(
                lane
                for lane in coverage.lanes
                if lane.expected.original_comparison_id == item.lane.original_comparison_id
            )
            if len(selected) != 1:
                raise ValueError("Coverage lane missing")
            registration = await self.worker_registry.get_active(item.worker_id)
            flags["durable"] = durable == item
            flags["coverage"] = bool(
                coverage.durable_dependencies_valid
                and coverage.outcome_authority
                and coverage.runtime_verification_authority
                and coverage.before_after_authority
                and coverage.active_baseline_authority
            )
            flags["missing"] = bool(
                selected[0].status == "missing" and selected[0].dispatch_required
            )
            flags["active"] = bool(
                registration is not None and registration.state is WorkerRegistrationState.ACTIVE
            )
            flags["worker"] = bool(
                registration is not None and _worker_matches(item, registration.contract)
            )
        except (
            EvolutionPostRollbackBehavioralCoverageError,
            EvolutionPostRollbackRemoteLanePlacementError,
            WorkerRegistryStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            flags = {key: False for key in flags}
        authority = all(flags.values())
        return EvolutionPostRollbackRemoteLanePlacementView(
            placement=item,
            durable_source_valid=flags["durable"],
            coverage_authority=flags["coverage"],
            lane_still_missing=flags["missing"],
            worker_registration_active=flags["active"],
            worker_contract_current=flags["worker"],
            placement_authority=authority,
        )

    async def _active_worker(self, worker_id: str, platform: Platform):
        try:
            registration = await self.worker_registry.get_active(worker_id)
        except WorkerRegistryStoreError as exc:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_worker_unavailable",
                "无法读取 Worker registration authority。",
            ) from exc
        if registration is None or registration.state is not WorkerRegistrationState.ACTIVE:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_worker_inactive",
                "目标 Worker 当前没有 active incarnation。",
            )
        try:
            _validate_worker(registration.contract, platform)
        except (TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteLanePlacementError(
                "post_rollback_placement_worker_incompatible",
                "目标 Worker 的平台、协议、能力或隔离合同不满足 Placement。",
            ) from exc
        return registration


def render_post_rollback_remote_lane_placement(
    view: EvolutionPostRollbackRemoteLanePlacementView,
) -> str:
    item = view.placement
    return "\n".join(
        (
            "## 回滚后远端 Lane Placement",
            "",
            f"- Placement：`{item.placement_id}`",
            f"- Coverage：`{item.coverage_contract_id}`",
            f"- Lane：L{item.lane.order} `{item.lane.platform}` / `{item.lane.suite_id}`",
            f"- Worker：`{item.worker_id}` · epoch {item.worker_epoch}",
            f"- Instance：`{item.worker_instance_id}`",
            f"- Release target：`{item.release_target}`",
            f"- Placement authority：{'有效' if view.placement_authority else '无效'}",
            "- Health / capacity：尚未验证、尚未预留",
            "",
            "该 Placement 不下载 baseline、不下发任务，也不授予执行、结果、学习或推广权限。",
        )
    )


def _build_placement(
    *,
    coverage: EvolutionPostRollbackBehavioralCoverageContract,
    lane: EvolutionPostRollbackBehavioralCoverageLane,
    worker: WorkerContract,
    placed_at: str,
) -> EvolutionPostRollbackRemoteLanePlacement:
    _validate_worker(worker, lane.platform)
    timestamp = _aware(placed_at).isoformat()
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_LANE_PLACEMENT_POLICY,
        "workspace_root": coverage.workspace_root,
        "coverage_contract_id": coverage.contract_id,
        "coverage_contract_sha256": coverage.contract_sha256,
        "outcome_id": coverage.outcome_id,
        "outcome_sha256": coverage.outcome_sha256,
        "request_id": coverage.request_id,
        "workbench_proposal_id": coverage.workbench_proposal_id,
        "lane": lane.model_dump(mode="json"),
        "worker_id": worker.worker_id,
        "worker_instance_id": worker.instance_id,
        "worker_epoch": worker.epoch,
        "worker_contract_sha256": worker.contract_sha256,
        "worker_kind": "tool",
        "worker_protocol_version": 1,
        "worker_software_version": worker.software_version,
        "worker_platform_system": worker.platform.system,
        "worker_platform_machine": worker.platform.machine,
        "release_target": _release_target(worker.platform.system, worker.platform.machine),
        "required_capabilities": [item.value for item in _REQUIRED_CAPABILITIES],
        "required_isolation": {
            "ephemeral_workspace": True,
            "network_default_deny": True,
            "environment_allowlist": True,
            "resource_limits_enforced": True,
            "process_tree_cancel": True,
            "artifact_digest": True,
        },
        "state": "placed",
        "worker_registration_verified": True,
        "target_compatibility_verified": True,
        "health_verified": False,
        "capacity_reserved": False,
        "baseline_resolved": False,
        "transport_delivered": False,
        "execution_authority": False,
        "result_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "placed_at": timestamp,
    }
    digest = _digest(payload)
    return EvolutionPostRollbackRemoteLanePlacement.model_validate(
        {
            **payload,
            "placement_id": f"evpostplacement_{digest[:24]}",
            "placement_sha256": digest,
        }
    )


def _require_coverage_lineage(
    item: EvolutionPostRollbackRemoteLanePlacement,
    contract: EvolutionPostRollbackBehavioralCoverageContract,
) -> None:
    if not (
        contract.contract_id == item.coverage_contract_id
        and contract.contract_sha256 == item.coverage_contract_sha256
        and contract.outcome_id == item.outcome_id
        and contract.outcome_sha256 == item.outcome_sha256
        and contract.request_id == item.request_id
        and contract.workbench_proposal_id == item.workbench_proposal_id
        and item.lane in contract.lanes
    ):
        raise ValueError("Placement/Coverage lineage 不一致。")


def _remote_missing_lane(coverage, comparison_id: str):
    selected = tuple(
        lane for lane in coverage.lanes if lane.expected.original_comparison_id == comparison_id
    )
    if len(selected) != 1:
        raise EvolutionPostRollbackRemoteLanePlacementError(
            "post_rollback_placement_lane_missing",
            "Comparison 不属于 Coverage Contract。",
        )
    view = selected[0]
    if not (
        view.expected.remote_target_required and view.status == "missing" and view.dispatch_required
    ):
        raise EvolutionPostRollbackRemoteLanePlacementError(
            "post_rollback_placement_lane_not_remote_missing",
            "只有尚未记录的远端 lane 可以建立 Placement。",
        )
    return view.expected, view


def _validate_worker(worker: WorkerContract, platform: Platform) -> None:
    if not verify_worker_contract(worker):
        raise ValueError("Worker Contract digest 无效。")
    if not (
        worker.kind is WorkerKind.TOOL
        and worker.protocol_min <= 1 <= worker.protocol_max
        and _system_platform(worker.platform.system) == platform
        and set(_REQUIRED_CAPABILITIES).issubset(worker.capabilities)
        and _isolation_satisfies(worker.isolation)
    ):
        raise ValueError("Worker Contract 不满足远端 Lane Placement。")
    _release_target(worker.platform.system, worker.platform.machine)


def _worker_matches(
    item: EvolutionPostRollbackRemoteLanePlacement,
    worker: WorkerContract,
) -> bool:
    try:
        _validate_worker(worker, item.lane.platform)
    except ValueError:
        return False
    return bool(
        worker.worker_id == item.worker_id
        and worker.instance_id == item.worker_instance_id
        and worker.epoch == item.worker_epoch
        and worker.contract_sha256 == item.worker_contract_sha256
        and worker.software_version == item.worker_software_version
        and worker.platform.system == item.worker_platform_system
        and worker.platform.machine == item.worker_platform_machine
        and _release_target(worker.platform.system, worker.platform.machine) == item.release_target
    )


def _isolation_satisfies(actual: WorkerIsolationContract) -> bool:
    return all(
        not getattr(_REQUIRED_ISOLATION, field) or getattr(actual, field)
        for field in (
            "ephemeral_workspace",
            "network_default_deny",
            "environment_allowlist",
            "resource_limits_enforced",
            "process_tree_cancel",
            "artifact_digest",
        )
    )


def _system_platform(system: str) -> Platform:
    mapping: dict[str, Platform] = {
        "darwin": "macos",
        "linux": "linux",
        "windows": "windows",
    }
    try:
        return mapping[system]
    except KeyError as exc:
        raise ValueError("Worker platform system 不受支持。") from exc


def _release_target(system: str, machine: str) -> str:
    platform = _system_platform(system)
    normalized = machine.casefold()
    if normalized in {"arm64", "aarch64"}:
        arch = "arm64"
    elif normalized in {"x86_64", "amd64", "x64"}:
        arch = "x64"
    else:
        raise ValueError("Worker CPU 架构不支持 release target。")
    return f"{platform}-{arch}"


def _request_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", text) is None:
        raise EvolutionPostRollbackRemoteLanePlacementError(
            "post_rollback_placement_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return text


def _coverage_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evpostcoverage_[0-9a-f]{24}", text) is None:
        raise ValueError("Coverage Contract ID 格式无效。")
    return text


def _comparison_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(_SHA256_RE, text) is None:
        raise EvolutionPostRollbackRemoteLanePlacementError(
            "post_rollback_placement_comparison_id_invalid",
            "Comparison ID 格式无效。",
        )
    return text


def _worker_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", text) is None:
        raise EvolutionPostRollbackRemoteLanePlacementError(
            "post_rollback_placement_worker_id_invalid",
            "Worker ID 格式无效。",
        )
    return text


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_lane_placements ("
        "placement_id TEXT PRIMARY KEY, placement_sha256 TEXT NOT NULL UNIQUE, "
        "coverage_contract_id TEXT NOT NULL, comparison_id TEXT NOT NULL, "
        "worker_id TEXT NOT NULL, worker_epoch INTEGER NOT NULL, "
        "placement_json TEXT NOT NULL, placed_at TEXT NOT NULL, "
        "UNIQUE(coverage_contract_id, comparison_id))"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_placement_worker "
        "ON evolution_post_rollback_remote_lane_placements(worker_id, worker_epoch)"
    )
    await db.commit()


def _restore(encoded: str) -> EvolutionPostRollbackRemoteLanePlacement:
    try:
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise ValueError("Placement payload 必须是对象。")
        return EvolutionPostRollbackRemoteLanePlacement.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteLanePlacementError(
            "post_rollback_placement_store_corrupt",
            "Remote Lane Placement 无法验证。",
        ) from exc


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_REMOTE_LANE_PLACEMENT_POLICY",
    "EvolutionPostRollbackRemoteLanePlacement",
    "EvolutionPostRollbackRemoteLanePlacementError",
    "EvolutionPostRollbackRemoteLanePlacementService",
    "EvolutionPostRollbackRemoteLanePlacementStore",
    "EvolutionPostRollbackRemoteLanePlacementView",
    "render_post_rollback_remote_lane_placement",
]
