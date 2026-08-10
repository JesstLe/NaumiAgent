"""Queued remote dispatch with durable health and atomic capacity authority."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionRequirements,
    WorkerContract,
    WorkerHealthReport,
    WorkerKind,
)
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityExhaustedError,
    WorkerCapacityReservationState,
    WorkerCapacitySnapshot,
    WorkerRegistryConflictError,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageError,
)
from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneError,
    load_post_rollback_runtime_eval_request,
)
from naumi_agent.evolution.post_rollback_remote_lane_placements import (
    EvolutionPostRollbackRemoteLanePlacement,
    EvolutionPostRollbackRemoteLanePlacementError,
)
from naumi_agent.evolution.post_rollback_target_baselines import (
    EvolutionPostRollbackTargetBaseline,
    EvolutionPostRollbackTargetBaselineError,
    EvolutionPostRollbackTargetBaselineService,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterEvidenceError,
    EvolutionProposalBeforeAfterEvidenceStore,
)
from naumi_agent.release.runtime_eval import (
    MAX_RUNTIME_EVAL_OUTPUT_BYTES,
    ReleaseRuntimeEvalRequest,
)

EVOLUTION_POST_ROLLBACK_REMOTE_DISPATCH_POLICY = (
    "evolution-post-rollback-remote-dispatch-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 768 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRemoteDispatch(_StrictModel):
    """Queued job authority; it is not a Worker claim or execution grant."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-dispatch-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_DISPATCH_POLICY
    )
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(pattern=r"^evpostjob_[0-9a-f]{24}$")
    reservation_id: str = Field(pattern=r"^evpostreservation_[0-9a-f]{24}$")
    workspace_root: str = Field(min_length=1, max_length=4_096)
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    coverage_contract_id: str = Field(pattern=r"^evpostcoverage_[0-9a-f]{24}$")
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    comparison_id: str = Field(pattern=_SHA256_RE)
    placement_id: str = Field(pattern=r"^evpostplacement_[0-9a-f]{24}$")
    placement_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    channel: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    suite_sha256: str = Field(pattern=_SHA256_RE)
    repetitions: int = Field(ge=5, le=100)
    runtime_eval_request: ReleaseRuntimeEvalRequest
    case_execution_budget_ms: int = Field(ge=1, le=500_000)
    total_execution_budget_ms: int = Field(ge=5, le=50_000_000)
    reservation_ttl_seconds: int = Field(ge=5, le=86_400)
    reservation_expires_at: str = Field(min_length=1, max_length=100)
    health_report_sha256: str = Field(pattern=_SHA256_RE)
    heartbeat_sequence: int = Field(ge=1)
    heartbeat_observed_at: str = Field(min_length=1, max_length=100)
    reported_active_jobs: int = Field(ge=0, le=10_000)
    reported_accepting_jobs: Literal[True] = True
    health_assessed_at: str = Field(min_length=1, max_length=100)
    capacity_maximum: int = Field(ge=1, le=10_000)
    state: Literal["queued"] = "queued"
    attempt: Literal[1] = 1
    baseline_resolved: Literal[True] = True
    health_verified: Literal[True] = True
    capacity_reserved: Literal[True] = True
    worker_claimed: Literal[False] = False
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    queued_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Remote Dispatch workspace 必须 canonical。")
        queued = _aware(self.queued_at)
        assessed = _aware(self.health_assessed_at)
        _aware(self.heartbeat_observed_at)
        if not (
            assessed == queued
            and _aware(self.reservation_expires_at)
            == queued + timedelta(seconds=self.reservation_ttl_seconds)
            and self.suite_id == self.runtime_eval_request.suite_id
            and self.suite_sha256 == self.runtime_eval_request.suite_sha256
            and self.case_execution_budget_ms
            == sum(item.max_duration_ms for item in self.runtime_eval_request.cases)
            and self.total_execution_budget_ms
            == self.case_execution_budget_ms * self.repetitions
            and self.reservation_ttl_seconds == _reservation_ttl(
                self.total_execution_budget_ms
            )
        ):
            raise ValueError("Remote Dispatch budget/health/capacity projection 不一致。")
        if any(
            (
                self.worker_claimed,
                self.transport_delivered,
                self.execution_authority,
                self.result_authority,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Queued Remote Dispatch 不得扩大权威。")
        core = self.model_dump(
            mode="json",
            exclude={"dispatch_id", "dispatch_sha256", "job_id", "reservation_id"},
        )
        digest = _digest(core)
        suffix = digest[:24]
        if not (
            hmac.compare_digest(self.dispatch_sha256, digest)
            and self.dispatch_id == f"evpostdispatch_{suffix}"
            and self.job_id == f"evpostjob_{suffix}"
            and self.reservation_id == f"evpostreservation_{suffix}"
        ):
            raise ValueError("Remote Dispatch identity 不一致。")
        return self


class EvolutionPostRollbackRemoteDispatchView(_StrictModel):
    dispatch: EvolutionPostRollbackRemoteDispatch
    durable_source_valid: bool
    baseline_authority: bool
    worker_registration_active: bool
    health_authority: bool
    capacity_authority: bool
    suite_authority: bool
    dispatch_authority: bool
    claim_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = bool(
            self.durable_source_valid
            and self.baseline_authority
            and self.worker_registration_active
            and self.health_authority
            and self.capacity_authority
            and self.suite_authority
        )
        if self.dispatch_authority is not current:
            raise ValueError("Remote Dispatch authority 投影不一致。")
        return self


class EvolutionPostRollbackRemoteDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRemoteDispatchStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        baseline_resolution_id: str,
    ) -> EvolutionPostRollbackRemoteDispatch | None:
        _baseline_id(baseline_resolution_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT dispatch_json FROM evolution_post_rollback_remote_dispatches "
                        "WHERE baseline_resolution_id = ?",
                        (baseline_resolution_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["dispatch_json"])
        except EvolutionPostRollbackRemoteDispatchError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_store_corrupt",
                "Remote Dispatch 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        dispatch: EvolutionPostRollbackRemoteDispatch,
    ) -> EvolutionPostRollbackRemoteDispatch:
        try:
            item = EvolutionPostRollbackRemoteDispatch.model_validate_json(
                dispatch.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_invalid",
                "Remote Dispatch artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_oversized",
                "Remote Dispatch 超过 768 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                dependency = await (
                    await db.execute(
                        "SELECT baseline_resolution_sha256 FROM "
                        "evolution_post_rollback_target_baselines "
                        "WHERE baseline_resolution_id = ?",
                        (item.baseline_resolution_id,),
                    )
                ).fetchone()
                if (
                    dependency is None
                    or dependency["baseline_resolution_sha256"]
                    != item.baseline_resolution_sha256
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_dependency_mismatch",
                        "Remote Dispatch 的 Target Baseline durable authority 不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT dispatch_json FROM evolution_post_rollback_remote_dispatches "
                        "WHERE baseline_resolution_id = ?",
                        (item.baseline_resolution_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["dispatch_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionPostRollbackRemoteDispatchError(
                            "post_rollback_remote_dispatch_conflict",
                            "同一 Target Baseline 已绑定不同 Remote Dispatch。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_dispatches "
                    "(dispatch_id, dispatch_sha256, baseline_resolution_id, "
                    "baseline_resolution_sha256, worker_id, worker_epoch, reservation_id, "
                    "dispatch_json, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.dispatch_id,
                        item.dispatch_sha256,
                        item.baseline_resolution_id,
                        item.baseline_resolution_sha256,
                        item.worker_id,
                        item.worker_epoch,
                        item.reservation_id,
                        encoded,
                        item.queued_at,
                    ),
                )
                await db.commit()
        except EvolutionPostRollbackRemoteDispatchError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_store_error",
                "Remote Dispatch 无法持久化。",
            ) from exc
        return item


class EvolutionPostRollbackRemoteDispatchService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        target_baseline_service: EvolutionPostRollbackTargetBaselineService,
        evidence_store: EvolutionProposalBeforeAfterEvidenceStore,
        worker_registry: WorkerRegistryStore,
        store: EvolutionPostRollbackRemoteDispatchStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.target_baseline_service = target_baseline_service
        self.evidence_store = evidence_store
        self.worker_registry = worker_registry
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def queue(
        self,
        *,
        request_id: str,
        comparison_id: str,
        channel: str,
        queued_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteDispatchView:
        request = _request_id(request_id)
        comparison = _comparison_id(comparison_id)
        lock = self._locks.setdefault(f"{request}:{comparison}", asyncio.Lock())
        async with lock:
            baseline_view = await self.target_baseline_service.resolve(
                request_id=request,
                comparison_id=comparison,
                channel=channel,
            )
            if not baseline_view.baseline_resolution_authority:
                raise EvolutionPostRollbackRemoteDispatchError(
                    "post_rollback_remote_dispatch_baseline_stale",
                    "Target Baseline Resolution authority 已失效。",
                )
            baseline = baseline_view.baseline
            existing = await self.store.get(baseline.baseline_resolution_id)
            if existing is not None:
                view = await self.inspect(dispatch=existing, assessed_at=queued_at)
                if not view.dispatch_authority:
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_stale",
                        "既有 Remote Dispatch authority 已失效。",
                    )
                return view
            now = _aware(queued_at or datetime.now(UTC).isoformat()).isoformat()
            placement = await self._placement(baseline)
            runtime_request, repetitions = await self._suite_inputs(baseline, placement)
            ttl = _reservation_ttl(
                sum(item.max_duration_ms for item in runtime_request.cases) * repetitions
            )
            registration = await self.worker_registry.get_active(baseline.worker_id)
            if registration is None or not _registration_matches(
                baseline,
                placement,
                registration.contract,
            ):
                raise EvolutionPostRollbackRemoteDispatchError(
                    "post_rollback_remote_dispatch_worker_stale",
                    "Placement Worker incarnation 已失效。",
                )
            requirements = _requirements(placement, ttl)
            report = await self.worker_registry.get_latest_health_report(baseline.worker_id)
            if report is None:
                raise EvolutionPostRollbackRemoteDispatchError(
                    "post_rollback_remote_dispatch_health_missing",
                    "目标 Worker 尚无 durable Health Report。",
                )
            admission = await self.worker_registry.assess_admission(
                worker_id=baseline.worker_id,
                report=report,
                requirements=requirements,
                now=now,
            )
            if not admission.admitted:
                raise EvolutionPostRollbackRemoteDispatchError(
                    "post_rollback_remote_dispatch_health_blocked",
                    "目标 Worker 当前健康、accepting 或 reported capacity 不满足要求。",
                )
            capacity = await self.worker_registry.capacity_snapshot(
                worker_id=baseline.worker_id,
                assessed_at=now,
            )
            if capacity is None or capacity.available < 1:
                raise EvolutionPostRollbackRemoteDispatchError(
                    "post_rollback_remote_dispatch_capacity_exhausted",
                    "目标 Worker 当前没有可预留 capacity。",
                )
            artifact = _build_dispatch(
                baseline=baseline,
                placement=placement,
                request=runtime_request,
                repetitions=repetitions,
                report=report,
                capacity=capacity,
                queued_at=now,
            )
            reserved = False
            try:
                reservation = await self.worker_registry.reserve_capacity(
                    reservation_id=artifact.reservation_id,
                    worker_id=artifact.worker_id,
                    instance_id=artifact.worker_instance_id,
                    epoch=artifact.worker_epoch,
                    job_id=artifact.job_id,
                    reserved_at=artifact.queued_at,
                    ttl_seconds=artifact.reservation_ttl_seconds,
                )
                reserved = True
                if reservation.expires_at != artifact.reservation_expires_at:
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_reservation_mismatch",
                        "Capacity Reservation expiry 与 Dispatch 不一致。",
                    )
                current_report = await self.worker_registry.get_latest_health_report(
                    artifact.worker_id
                )
                if current_report != report:
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_health_changed",
                        "Worker Health Report 在 capacity reserve 期间更新，请重试。",
                    )
                refreshed = await self.target_baseline_service.inspect(baseline=baseline)
                if not refreshed.baseline_resolution_authority:
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_baseline_changed",
                        "Target Baseline authority 在 capacity reserve 期间变化。",
                    )
                recorded = await self.store.record(artifact)
                view = await self.inspect(dispatch=recorded, assessed_at=now)
                if not view.dispatch_authority:
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_authority_changed",
                        "Remote Dispatch 持久化期间 authority 已变化。",
                    )
                return view
            except asyncio.CancelledError:
                if reserved:
                    await asyncio.shield(
                        self._release(artifact, reason_code="dispatch_cancelled")
                    )
                raise
            except (
                EvolutionPostRollbackBehavioralCoverageError,
                EvolutionPostRollbackBehavioralLaneError,
                EvolutionPostRollbackRemoteDispatchError,
                EvolutionPostRollbackRemoteLanePlacementError,
                EvolutionPostRollbackTargetBaselineError,
                EvolutionProposalBeforeAfterEvidenceError,
                WorkerCapacityExhaustedError,
                WorkerRegistryConflictError,
                WorkerRegistryStoreError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                if reserved:
                    try:
                        await self._release(
                            artifact,
                            reason_code="dispatch_record_failed",
                        )
                    except (WorkerRegistryConflictError, WorkerRegistryStoreError) as cleanup:
                        raise EvolutionPostRollbackRemoteDispatchError(
                            "post_rollback_remote_dispatch_cleanup_failed",
                            "Remote Dispatch 失败且 capacity compensation 未能完成。",
                        ) from cleanup
                if isinstance(exc, WorkerCapacityExhaustedError):
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_capacity_exhausted",
                        "目标 Worker capacity 在原子预留时已耗尽。",
                    ) from exc
                if isinstance(exc, (WorkerRegistryConflictError, WorkerRegistryStoreError)):
                    raise EvolutionPostRollbackRemoteDispatchError(
                        "post_rollback_remote_dispatch_capacity_failed",
                        "无法为 exact Worker incarnation 原子预留 capacity。",
                    ) from exc
                raise

    async def inspect(
        self,
        *,
        dispatch: EvolutionPostRollbackRemoteDispatch,
        assessed_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteDispatchView:
        item = EvolutionPostRollbackRemoteDispatch.model_validate_json(
            dispatch.model_dump_json()
        )
        now = _aware(assessed_at or datetime.now(UTC).isoformat()).isoformat()
        if _aware(now) < _aware(item.queued_at):
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_assessment_before_queue",
                "Remote Dispatch 不能在 queued_at 之前评估。",
            )
        flags = {
            "durable": False,
            "baseline": False,
            "worker": False,
            "health": False,
            "capacity": False,
            "suite": False,
        }
        try:
            durable = await self.store.get(item.baseline_resolution_id)
            baseline = await self.target_baseline_service.store.get(
                item.placement_id
            )
            if baseline is None:
                raise ValueError("Target baseline missing")
            _require_dispatch_baseline(item, baseline)
            baseline_view = await self.target_baseline_service.inspect(baseline=baseline)
            placement = await self._placement(baseline)
            registration = await self.worker_registry.get_active(item.worker_id)
            if registration is None:
                raise ValueError("Worker missing")
            requirements = _requirements(placement, item.reservation_ttl_seconds)
            admission = await self.worker_registry.assess_latest_admission(
                worker_id=item.worker_id,
                requirements=requirements,
                now=now,
            )
            reservation = await self.worker_registry.get_capacity_reservation(
                item.reservation_id,
                assessed_at=now,
            )
            request, repetitions = await self._suite_inputs(baseline, placement)
            flags["durable"] = durable == item and item.workspace_root == str(self.workspace_root)
            flags["baseline"] = baseline_view.baseline_resolution_authority
            flags["worker"] = _registration_matches(
                baseline,
                placement,
                registration.contract,
            )
            flags["health"] = admission.admitted
            flags["capacity"] = bool(
                reservation is not None
                and reservation.state is WorkerCapacityReservationState.ACTIVE
                and reservation.worker_id == item.worker_id
                and reservation.instance_id == item.worker_instance_id
                and reservation.epoch == item.worker_epoch
                and reservation.job_id == item.job_id
                and reservation.expires_at == item.reservation_expires_at
            )
            flags["suite"] = bool(
                request == item.runtime_eval_request
                and repetitions == item.repetitions
            )
        except (
            EvolutionPostRollbackBehavioralCoverageError,
            EvolutionPostRollbackBehavioralLaneError,
            EvolutionPostRollbackRemoteDispatchError,
            EvolutionPostRollbackRemoteLanePlacementError,
            EvolutionPostRollbackTargetBaselineError,
            EvolutionProposalBeforeAfterEvidenceError,
            WorkerRegistryStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            flags = {key: False for key in flags}
        return EvolutionPostRollbackRemoteDispatchView(
            dispatch=item,
            durable_source_valid=flags["durable"],
            baseline_authority=flags["baseline"],
            worker_registration_active=flags["worker"],
            health_authority=flags["health"],
            capacity_authority=flags["capacity"],
            suite_authority=flags["suite"],
            dispatch_authority=all(flags.values()),
        )

    async def _placement(
        self,
        baseline: EvolutionPostRollbackTargetBaseline,
    ) -> EvolutionPostRollbackRemoteLanePlacement:
        placement = await self.target_baseline_service.placement_store.get(
            baseline.coverage_contract_id,
            baseline.comparison_id,
        )
        if placement is None or not (
            placement.placement_id == baseline.placement_id
            and placement.placement_sha256 == baseline.placement_sha256
        ):
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_placement_stale",
                "Target Baseline 的 Placement durable authority 不一致。",
            )
        return placement

    async def _suite_inputs(
        self,
        baseline: EvolutionPostRollbackTargetBaseline,
        placement: EvolutionPostRollbackRemoteLanePlacement,
    ) -> tuple[ReleaseRuntimeEvalRequest, int]:
        coverage = await (
            self.target_baseline_service.placement_service.coverage_store.get_by_outcome(
                baseline.outcome_id
            )
        )
        if coverage is None or not (
            coverage.contract_id == baseline.coverage_contract_id
            and coverage.contract_sha256 == baseline.coverage_contract_sha256
            and coverage.workspace_root == baseline.workspace_root == str(self.workspace_root)
            and coverage.outcome_id == baseline.outcome_id
            and coverage.outcome_sha256 == baseline.outcome_sha256
            and coverage.request_id == baseline.request_id
        ):
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_coverage_stale",
                "Target Baseline 的 Coverage Contract authority 不一致。",
            )
        evidence = await self.evidence_store.get_by_outcome(baseline.outcome_id)
        if evidence is None or not (
            evidence.evidence_id == coverage.before_after_evidence_id
            and evidence.evidence_sha256 == coverage.before_after_evidence_sha256
            and evidence.workspace_root == coverage.workspace_root
            and evidence.outcome_id == coverage.outcome_id
            and evidence.outcome_sha256 == coverage.outcome_sha256
            and evidence.request_id == coverage.request_id
            and evidence.final_evaluation_id == coverage.final_evaluation_id
            and evidence.final_evaluation_sha256 == coverage.final_evaluation_sha256
        ):
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_evidence_stale",
                "原 Before/After Evidence authority 缺失。",
            )
        selected = tuple(
            lane for lane in evidence.lanes if lane.comparison_id == baseline.comparison_id
        )
        if len(selected) != 1:
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_lane_missing",
                "Target Baseline comparison 不属于原 Final Evaluation。",
            )
        lane = selected[0]
        if not (
            lane.suite_id == placement.lane.suite_id
            and lane.comparison_receipt_sha256
            == placement.lane.original_comparison_sha256
            and lane.baseline_id == placement.lane.original_baseline_id
            and lane.before.samples_sha256
            == placement.lane.original_baseline_samples_sha256
            and lane.before.samples == lane.after.samples
            and 5 <= lane.before.samples <= 100
        ):
            raise EvolutionPostRollbackRemoteDispatchError(
                "post_rollback_remote_dispatch_lane_invalid",
                "原 lane suite/repetitions 不适合 Remote Runtime Eval。",
            )
        request = load_post_rollback_runtime_eval_request(
            self.workspace_root,
            lane.suite_id,
        )
        return request, lane.before.samples

    async def _release(
        self,
        item: EvolutionPostRollbackRemoteDispatch,
        *,
        reason_code: str,
    ) -> None:
        try:
            await self.worker_registry.release_capacity(
                reservation_id=item.reservation_id,
                worker_id=item.worker_id,
                instance_id=item.worker_instance_id,
                epoch=item.worker_epoch,
                released_at=max(
                    datetime.now(UTC),
                    _aware(item.queued_at),
                ).isoformat(),
                reason_code=reason_code,
                accept_terminal=True,
            )
        except ValueError as exc:
            raise WorkerRegistryStoreError("Capacity compensation 参数无效。") from exc


def render_post_rollback_remote_dispatch(
    view: EvolutionPostRollbackRemoteDispatchView,
) -> str:
    item = view.dispatch
    return "\n".join(
        (
            "## 回滚后远端 Dispatch",
            "",
            f"- Dispatch：`{item.dispatch_id}`",
            f"- Job / Reservation：`{item.job_id}` / `{item.reservation_id}`",
            f"- Worker：`{item.worker_id}` · epoch {item.worker_epoch}",
            f"- Channel / target：`{item.channel}` / `{item.release_target}`",
            f"- Suite / repetitions：`{item.suite_id}` / {item.repetitions}",
            f"- 执行预算：{item.total_execution_budget_ms}ms · "
            f"reservation {item.reservation_ttl_seconds}s",
            f"- Health：seq {item.heartbeat_sequence} · active jobs {item.reported_active_jobs}",
            f"- Capacity：已原子预留 1/{item.capacity_maximum}",
            f"- Dispatch authority：{'有效' if view.dispatch_authority else '无效'}",
            "",
            "已原子预留 capacity；尚未被 Worker claim，未传输 baseline，"
            "也不授予执行、结果、学习或推广权限。",
        )
    )


def _build_dispatch(
    *,
    baseline: EvolutionPostRollbackTargetBaseline,
    placement: EvolutionPostRollbackRemoteLanePlacement,
    request: ReleaseRuntimeEvalRequest,
    repetitions: int,
    report: WorkerHealthReport,
    capacity: WorkerCapacitySnapshot,
    queued_at: str,
) -> EvolutionPostRollbackRemoteDispatch:
    case_budget = sum(item.max_duration_ms for item in request.cases)
    total_budget = case_budget * repetitions
    ttl = _reservation_ttl(total_budget)
    queued = _aware(queued_at)
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_DISPATCH_POLICY,
        "workspace_root": baseline.workspace_root,
        "baseline_resolution_id": baseline.baseline_resolution_id,
        "baseline_resolution_sha256": baseline.baseline_resolution_sha256,
        "coverage_contract_id": baseline.coverage_contract_id,
        "outcome_id": baseline.outcome_id,
        "request_id": baseline.request_id,
        "comparison_id": baseline.comparison_id,
        "placement_id": baseline.placement_id,
        "placement_sha256": baseline.placement_sha256,
        "worker_id": baseline.worker_id,
        "worker_instance_id": baseline.worker_instance_id,
        "worker_epoch": baseline.worker_epoch,
        "worker_contract_sha256": placement.worker_contract_sha256,
        "release_target": baseline.release_target,
        "channel": baseline.channel,
        "suite_id": request.suite_id,
        "suite_sha256": request.suite_sha256,
        "repetitions": repetitions,
        "runtime_eval_request": request.model_dump(mode="json"),
        "case_execution_budget_ms": case_budget,
        "total_execution_budget_ms": total_budget,
        "reservation_ttl_seconds": ttl,
        "reservation_expires_at": (queued + timedelta(seconds=ttl)).isoformat(),
        "health_report_sha256": report.report_sha256,
        "heartbeat_sequence": report.heartbeat.sequence,
        "heartbeat_observed_at": report.heartbeat.observed_at,
        "reported_active_jobs": report.active_jobs,
        "reported_accepting_jobs": True,
        "health_assessed_at": queued.isoformat(),
        "capacity_maximum": capacity.maximum,
        "state": "queued",
        "attempt": 1,
        "baseline_resolved": True,
        "health_verified": True,
        "capacity_reserved": True,
        "worker_claimed": False,
        "transport_delivered": False,
        "execution_authority": False,
        "result_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "queued_at": queued.isoformat(),
    }
    digest = _digest(core)
    suffix = digest[:24]
    return EvolutionPostRollbackRemoteDispatch.model_validate(
        {
            **core,
            "dispatch_id": f"evpostdispatch_{suffix}",
            "dispatch_sha256": digest,
            "job_id": f"evpostjob_{suffix}",
            "reservation_id": f"evpostreservation_{suffix}",
        }
    )


def _requirements(
    placement: EvolutionPostRollbackRemoteLanePlacement,
    ttl_seconds: int,
) -> WorkerAdmissionRequirements:
    return WorkerAdmissionRequirements(
        kind=WorkerKind.TOOL,
        protocol_version=placement.worker_protocol_version,
        capabilities=placement.required_capabilities,
        allowed_platforms=(placement.worker_platform_system,),
        min_wall_seconds=ttl_seconds,
        min_output_bytes=MAX_RUNTIME_EVAL_OUTPUT_BYTES,
        isolation=placement.required_isolation,
    )


def _registration_matches(
    baseline: EvolutionPostRollbackTargetBaseline,
    placement: EvolutionPostRollbackRemoteLanePlacement,
    contract: WorkerContract,
) -> bool:
    return bool(
        contract.worker_id == baseline.worker_id == placement.worker_id
        and contract.instance_id == baseline.worker_instance_id == placement.worker_instance_id
        and contract.epoch == baseline.worker_epoch == placement.worker_epoch
        and contract.contract_sha256 == placement.worker_contract_sha256
        and placement.release_target == baseline.release_target
    )


def _require_dispatch_baseline(
    item: EvolutionPostRollbackRemoteDispatch,
    baseline: EvolutionPostRollbackTargetBaseline,
) -> None:
    if not (
        item.baseline_resolution_id == baseline.baseline_resolution_id
        and item.baseline_resolution_sha256 == baseline.baseline_resolution_sha256
        and item.coverage_contract_id == baseline.coverage_contract_id
        and item.outcome_id == baseline.outcome_id
        and item.request_id == baseline.request_id
        and item.comparison_id == baseline.comparison_id
        and item.placement_id == baseline.placement_id
        and item.placement_sha256 == baseline.placement_sha256
        and item.worker_id == baseline.worker_id
        and item.worker_instance_id == baseline.worker_instance_id
        and item.worker_epoch == baseline.worker_epoch
        and item.release_target == baseline.release_target
        and item.channel == baseline.channel
    ):
        raise ValueError("Remote Dispatch/Target Baseline lineage 不一致。")


def _reservation_ttl(total_budget_ms: int) -> int:
    execution_seconds = math.ceil(total_budget_ms / 1_000)
    overhead = max(10, math.ceil(execution_seconds * 0.2))
    ttl = execution_seconds + overhead
    if not 5 <= ttl <= 86_400:
        raise ValueError("Remote Dispatch reservation TTL 超出支持范围。")
    return ttl


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_dispatches ("
        "dispatch_id TEXT PRIMARY KEY, dispatch_sha256 TEXT NOT NULL UNIQUE, "
        "baseline_resolution_id TEXT NOT NULL UNIQUE, "
        "baseline_resolution_sha256 TEXT NOT NULL, worker_id TEXT NOT NULL, "
        "worker_epoch INTEGER NOT NULL, reservation_id TEXT NOT NULL UNIQUE, "
        "dispatch_json TEXT NOT NULL, queued_at TEXT NOT NULL)"
    )
    await db.commit()


def _restore(encoded: str) -> EvolutionPostRollbackRemoteDispatch:
    try:
        return EvolutionPostRollbackRemoteDispatch.model_validate_json(encoded)
    except ValueError as exc:
        raise EvolutionPostRollbackRemoteDispatchError(
            "post_rollback_remote_dispatch_store_corrupt",
            "Remote Dispatch 无法验证。",
        ) from exc


def _request_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", text) is None:
        raise EvolutionPostRollbackRemoteDispatchError(
            "post_rollback_remote_dispatch_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return text


def _comparison_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(_SHA256_RE, text) is None:
        raise EvolutionPostRollbackRemoteDispatchError(
            "post_rollback_remote_dispatch_comparison_id_invalid",
            "Comparison ID 格式无效。",
        )
    return text


def _baseline_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evpostbaseline_[0-9a-f]{24}", text) is None:
        raise EvolutionPostRollbackRemoteDispatchError(
            "post_rollback_remote_dispatch_baseline_id_invalid",
            "Target Baseline Resolution ID 格式无效。",
        )
    return text


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Remote Dispatch 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_REMOTE_DISPATCH_POLICY",
    "EvolutionPostRollbackRemoteDispatch",
    "EvolutionPostRollbackRemoteDispatchError",
    "EvolutionPostRollbackRemoteDispatchService",
    "EvolutionPostRollbackRemoteDispatchStore",
    "EvolutionPostRollbackRemoteDispatchView",
    "render_post_rollback_remote_dispatch",
]
