"""Complete post-rollback behavioral matrix over local and remote lane evidence."""

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

from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageContract,
    EvolutionPostRollbackBehavioralCoverageError,
    EvolutionPostRollbackBehavioralCoverageLane,
    EvolutionPostRollbackBehavioralCoverageService,
    EvolutionPostRollbackBehavioralCoverageStore,
    EvolutionPostRollbackBehavioralCoverageView,
)
from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneError,
    EvolutionPostRollbackBehavioralLaneService,
    EvolutionPostRollbackBehavioralLaneStore,
    post_rollback_recovery_status,
)
from naumi_agent.evolution.post_rollback_remote_results import (
    EvolutionPostRollbackRemoteResultError,
    EvolutionPostRollbackRemoteResultService,
    EvolutionPostRollbackRemoteResultStore,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_POST_ROLLBACK_BEHAVIORAL_MATRIX_POLICY = (
    "evolution-post-rollback-behavioral-matrix-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 1024 * 1024
type RecoveryStatus = Literal["recovered", "changed", "inconclusive", "incompatible"]
type EvidenceKind = Literal["local_behavioral_lane", "remote_result_ingestion"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackBehavioralMatrixLane(_StrictModel):
    order: int = Field(ge=1, le=4)
    lane_kind: Literal["interventional", "adversarial"]
    platform: Literal["linux", "macos", "windows"]
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    original_comparison_id: str = Field(pattern=_SHA256_RE)
    original_comparison_sha256: str = Field(pattern=_SHA256_RE)
    evidence_kind: EvidenceKind
    evidence_id: str = Field(
        pattern=r"^(?:evpostbehavior|evpostresultreceipt)_[0-9a-f]{24}$"
    )
    evidence_sha256: str = Field(pattern=_SHA256_RE)
    fresh_comparison_id: str = Field(pattern=_SHA256_RE)
    fresh_comparison_sha256: str = Field(pattern=_SHA256_RE)
    recovery_status: RecoveryStatus
    evidence_authority_verified: Literal[True] = True
    evaluated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.evaluated_at)
        prefix = (
            "evpostbehavior_"
            if self.evidence_kind == "local_behavioral_lane"
            else "evpostresultreceipt_"
        )
        if not self.evidence_id.startswith(prefix):
            raise ValueError("Behavioral Matrix lane evidence kind/ID 不一致。")
        return self


class EvolutionPostRollbackBehavioralMatrix(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-behavioral-matrix-v1"] = (
        EVOLUTION_POST_ROLLBACK_BEHAVIORAL_MATRIX_POLICY
    )
    matrix_id: str = Field(pattern=r"^evpostmatrix_[0-9a-f]{24}$")
    matrix_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    coverage_contract_id: str = Field(pattern=r"^evpostcoverage_[0-9a-f]{24}$")
    coverage_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_verification_id: str = Field(pattern=r"^evpostrollback_[0-9a-f]{24}$")
    runtime_verification_sha256: str = Field(pattern=_SHA256_RE)
    before_after_evidence_id: str = Field(pattern=r"^evbeforeafter_[0-9a-f]{24}$")
    before_after_evidence_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    lane_count: int = Field(ge=2, le=4)
    local_lane_count: int = Field(ge=0, le=4)
    remote_lane_count: int = Field(ge=0, le=4)
    lanes: tuple[EvolutionPostRollbackBehavioralMatrixLane, ...] = Field(
        min_length=2,
        max_length=4,
    )
    recovery_verdict: RecoveryStatus
    behavioral_evaluation_recorded: Literal[True] = True
    long_term_metrics_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Behavioral Matrix workspace 必须 canonical。")
        _aware(self.recorded_at)
        if not (
            self.lane_count == len(self.lanes)
            and tuple(item.order for item in self.lanes)
            == tuple(range(1, self.lane_count + 1))
            and self.local_lane_count
            == sum(item.evidence_kind == "local_behavioral_lane" for item in self.lanes)
            and self.remote_lane_count == self.lane_count - self.local_lane_count
            and self.recovery_verdict
            == aggregate_post_rollback_recovery_status(
                tuple(item.recovery_status for item in self.lanes)
            )
        ):
            raise ValueError("Behavioral Matrix lane/verdict 聚合不一致。")
        if any(
            (
                self.long_term_metrics_recorded,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Behavioral Matrix 不得冒充长期、学习或推广 authority。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"matrix_id", "matrix_sha256"})
        )
        if not (
            hmac.compare_digest(self.matrix_sha256, digest)
            and self.matrix_id == f"evpostmatrix_{digest[:24]}"
        ):
            raise ValueError("Behavioral Matrix identity 不一致。")
        return self


class EvolutionPostRollbackBehavioralMatrixView(_StrictModel):
    matrix: EvolutionPostRollbackBehavioralMatrix
    status: Literal["recorded", "stale"]
    durable_matrix_valid: bool
    coverage_authority: bool
    complete_lane_authority: bool
    behavioral_evaluation_authority: bool
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_matrix_valid
            and self.coverage_authority
            and self.complete_lane_authority
        )
        if not (
            self.behavioral_evaluation_authority is expected
            and (self.status == "recorded") is expected
        ):
            raise ValueError("Behavioral Matrix authority projection 不一致。")
        return self


class EvolutionPostRollbackBehavioralMatrixError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackBehavioralMatrixStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)

    async def get_by_outcome(
        self,
        outcome_id: str,
    ) -> EvolutionPostRollbackBehavioralMatrix | None:
        outcome = _outcome_id(outcome_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_behavioral_matrices "
                        "WHERE outcome_id = ?",
                        (outcome,),
                    )
                ).fetchone()
            if row is None:
                return None
            matrix = _restore(row["matrix_json"])
            if not (
                matrix.matrix_id == row["matrix_id"]
                and matrix.matrix_sha256 == row["matrix_sha256"]
                and matrix.request_id == row["request_id"]
                and matrix.coverage_contract_id == row["coverage_contract_id"]
            ):
                raise ValueError("matrix row mismatch")
            return matrix
        except EvolutionPostRollbackBehavioralMatrixError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_store_corrupt",
                "Post-Rollback Behavioral Matrix 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        matrix: EvolutionPostRollbackBehavioralMatrix,
    ) -> EvolutionPostRollbackBehavioralMatrix:
        item = _matrix(matrix)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_oversized",
                "Post-Rollback Behavioral Matrix 超过 1 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                coverage = await (
                    await db.execute(
                        "SELECT contract_sha256 FROM "
                        "evolution_post_rollback_behavioral_coverage "
                        "WHERE contract_id = ? AND outcome_id = ?",
                        (item.coverage_contract_id, item.outcome_id),
                    )
                ).fetchone()
                if (
                    coverage is None
                    or coverage["contract_sha256"] != item.coverage_contract_sha256
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackBehavioralMatrixError(
                        "post_rollback_behavioral_matrix_coverage_mismatch",
                        "Behavioral Matrix 缺少 exact durable Coverage Contract。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT matrix_json FROM "
                        "evolution_post_rollback_behavioral_matrices "
                        "WHERE outcome_id = ?",
                        (item.outcome_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["matrix_json"])
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionPostRollbackBehavioralMatrixError(
                        "post_rollback_behavioral_matrix_conflict",
                        "同一 Outcome 已绑定不同 Behavioral Matrix。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_behavioral_matrices "
                    "(matrix_id, matrix_sha256, outcome_id, request_id, "
                    "coverage_contract_id, matrix_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.matrix_id,
                        item.matrix_sha256,
                        item.outcome_id,
                        item.request_id,
                        item.coverage_contract_id,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackBehavioralMatrixError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_store_failed",
                "Post-Rollback Behavioral Matrix 无法持久化。",
            ) from exc


class EvolutionPostRollbackBehavioralMatrixService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        coverage_store: EvolutionPostRollbackBehavioralCoverageStore,
        coverage_service: EvolutionPostRollbackBehavioralCoverageService,
        lane_store: EvolutionPostRollbackBehavioralLaneStore,
        lane_service: EvolutionPostRollbackBehavioralLaneService,
        remote_result_store: EvolutionPostRollbackRemoteResultStore,
        remote_result_service: EvolutionPostRollbackRemoteResultService,
        harness_store: HarnessStore,
        store: EvolutionPostRollbackBehavioralMatrixStore,
    ) -> None:
        paths = {
            coverage_store.db_path,
            lane_store.db_path,
            remote_result_store.db_path,
            store.db_path,
        }
        if len(paths) != 1:
            raise ValueError("Behavioral Matrix authority stores 必须共享 session SQLite。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.coverage_store = coverage_store
        self.coverage_service = coverage_service
        self.lane_store = lane_store
        self.lane_service = lane_service
        self.remote_result_store = remote_result_store
        self.remote_result_service = remote_result_service
        self.harness_store = harness_store
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        request_id: str,
    ) -> EvolutionPostRollbackBehavioralMatrixView:
        request = _request_id(request_id)
        lock = self._locks.setdefault(request, asyncio.Lock())
        async with lock:
            coverage = await self.coverage_service.record(request_id=request)
            lanes = await self._resolve_lanes(coverage)
            artifact = _build_matrix(coverage.contract, lanes)
            existing = await self.store.get_by_outcome(coverage.contract.outcome_id)
            if existing is None:
                existing = await self.store.record(artifact)
            elif existing != artifact:
                raise EvolutionPostRollbackBehavioralMatrixError(
                    "post_rollback_behavioral_matrix_conflict",
                    "既有 Behavioral Matrix 与当前完整 lane 集不一致。",
                )
            view = await self.inspect(matrix=existing)
            if not view.behavioral_evaluation_authority:
                raise EvolutionPostRollbackBehavioralMatrixError(
                    "post_rollback_behavioral_matrix_authority_changed",
                    "Behavioral Matrix 持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        matrix: EvolutionPostRollbackBehavioralMatrix,
    ) -> EvolutionPostRollbackBehavioralMatrixView:
        item = _matrix(matrix)
        durable = False
        coverage_authority = False
        complete = False
        try:
            stored = await self.store.get_by_outcome(item.outcome_id)
            coverage_contract = await self.coverage_store.get_by_outcome(item.outcome_id)
            if coverage_contract is None:
                raise ValueError("coverage missing")
            coverage = await self.coverage_service.inspect(contract=coverage_contract)
            coverage_authority = _coverage_authoritative(coverage)
            lanes = await self._resolve_lanes(coverage)
            rebuilt = _build_matrix(coverage.contract, lanes)
            durable = stored == item
            complete = rebuilt == item
        except (
            EvolutionPostRollbackBehavioralCoverageError,
            EvolutionPostRollbackBehavioralLaneError,
            EvolutionPostRollbackRemoteResultError,
            EvolutionPostRollbackBehavioralMatrixError,
            HarnessStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            pass
        authority = bool(durable and coverage_authority and complete)
        return EvolutionPostRollbackBehavioralMatrixView(
            matrix=item,
            status="recorded" if authority else "stale",
            durable_matrix_valid=durable,
            coverage_authority=coverage_authority,
            complete_lane_authority=complete,
            behavioral_evaluation_authority=authority,
        )

    async def _resolve_lanes(
        self,
        coverage: EvolutionPostRollbackBehavioralCoverageView,
    ) -> tuple[EvolutionPostRollbackBehavioralMatrixLane, ...]:
        if not _coverage_authoritative(coverage):
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_coverage_stale",
                "Coverage Contract authority 已失效。",
            )
        resolved = []
        for expected in coverage.contract.lanes:
            if expected.execution_scope == "local_installed_baseline":
                resolved.append(await self._local_lane(coverage.contract, expected))
            else:
                resolved.append(await self._remote_lane(coverage.contract, expected))
        return tuple(resolved)

    async def _local_lane(
        self,
        contract: EvolutionPostRollbackBehavioralCoverageContract,
        expected: EvolutionPostRollbackBehavioralCoverageLane,
    ) -> EvolutionPostRollbackBehavioralMatrixLane:
        lane = await self.lane_store.get(contract.outcome_id, expected.original_comparison_id)
        if lane is None:
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_lane_missing",
                f"Behavioral Matrix 缺少本机 lane #{expected.order}。",
            )
        view = await self.lane_service.inspect(lane=lane)
        if not (
            view.lane_authority
            and lane.lane_order == expected.order
            and lane.lane_kind == expected.lane_kind
            and lane.platform == expected.platform
            and lane.suite_id == expected.suite_id
            and lane.original_comparison_id == expected.original_comparison_id
            and lane.original_comparison_sha256 == expected.original_comparison_sha256
            and lane.original_baseline_id == expected.original_baseline_id
            and lane.original_baseline_samples_sha256
            == expected.original_baseline_samples_sha256
        ):
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_lane_stale",
                f"Behavioral Matrix 本机 lane #{expected.order} authority 无效。",
            )
        return EvolutionPostRollbackBehavioralMatrixLane(
            order=expected.order,
            lane_kind=expected.lane_kind,
            platform=expected.platform,
            suite_id=expected.suite_id,
            original_comparison_id=expected.original_comparison_id,
            original_comparison_sha256=expected.original_comparison_sha256,
            evidence_kind="local_behavioral_lane",
            evidence_id=lane.lane_id,
            evidence_sha256=lane.lane_sha256,
            fresh_comparison_id=lane.fresh_comparison.id,
            fresh_comparison_sha256=lane.fresh_comparison.receipt_sha256,
            recovery_status=lane.recovery_status,
            evaluated_at=lane.evaluated_at,
        )

    async def _remote_lane(
        self,
        contract: EvolutionPostRollbackBehavioralCoverageContract,
        expected: EvolutionPostRollbackBehavioralCoverageLane,
    ) -> EvolutionPostRollbackBehavioralMatrixLane:
        manifests = await self.remote_result_store.list_by_lane(
            outcome_id=contract.outcome_id,
            comparison_id=expected.original_comparison_id,
            limit=2,
        )
        if len(manifests) != 1:
            code = (
                "post_rollback_behavioral_matrix_lane_missing"
                if not manifests
                else "post_rollback_behavioral_matrix_lane_conflict"
            )
            raise EvolutionPostRollbackBehavioralMatrixError(
                code,
                f"Behavioral Matrix 远端 lane #{expected.order} admission 数量无效。",
            )
        manifest = manifests[0]
        view = await self.remote_result_service.inspect(manifest_id=manifest.manifest_id)
        receipt = view.receipt
        payload = manifest.payload
        if receipt is None or not (
            view.status == "ingested"
            and view.lane_evaluation_authority
            and payload.outcome_id == contract.outcome_id
            and payload.request_id == contract.request_id
            and payload.comparison_id == expected.original_comparison_id
            and payload.suite_id == expected.suite_id
            and payload.release_target.split("-", 1)[0] == expected.platform
            and receipt.repetitions == payload.repetitions
        ):
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_lane_stale",
                f"Behavioral Matrix 远端 lane #{expected.order} authority 无效。",
            )
        stored = await self.harness_store.get_eval_comparison_receipt_by_id(
            self.workspace_root,
            receipt.h5c_comparison_id,
        )
        if stored is None or stored.receipt.receipt_sha256 != receipt.h5c_comparison_sha256:
            raise EvolutionPostRollbackBehavioralMatrixError(
                "post_rollback_behavioral_matrix_remote_h5c_stale",
                f"Behavioral Matrix 远端 lane #{expected.order} H5c 无效。",
            )
        return EvolutionPostRollbackBehavioralMatrixLane(
            order=expected.order,
            lane_kind=expected.lane_kind,
            platform=expected.platform,
            suite_id=expected.suite_id,
            original_comparison_id=expected.original_comparison_id,
            original_comparison_sha256=expected.original_comparison_sha256,
            evidence_kind="remote_result_ingestion",
            evidence_id=receipt.receipt_id,
            evidence_sha256=receipt.receipt_sha256,
            fresh_comparison_id=stored.receipt.id,
            fresh_comparison_sha256=stored.receipt.receipt_sha256,
            recovery_status=post_rollback_recovery_status(stored.receipt),
            evaluated_at=receipt.ingested_at,
        )


def aggregate_post_rollback_recovery_status(
    statuses: tuple[RecoveryStatus, ...],
) -> RecoveryStatus:
    if not statuses:
        raise ValueError("Behavioral Matrix 至少需要一个 recovery status。")
    if "incompatible" in statuses:
        return "incompatible"
    if "inconclusive" in statuses:
        return "inconclusive"
    if set(statuses) == {"recovered"}:
        return "recovered"
    return "changed"


def render_post_rollback_behavioral_matrix(
    view: EvolutionPostRollbackBehavioralMatrixView,
) -> str:
    matrix = view.matrix
    lines = [
        f"# Post-Rollback Behavioral Matrix `{matrix.matrix_id}`",
        "",
        f"- 状态：`{view.status}`",
        f"- Rollback Outcome：`{matrix.outcome_id}`",
        f"- Coverage Contract：`{matrix.coverage_contract_id}`",
        f"- Lane：`{matrix.lane_count}`（本机 `{matrix.local_lane_count}` / "
        f"远端 `{matrix.remote_lane_count}`）",
        f"- 总体判定：`{matrix.recovery_verdict}`",
        f"- Behavioral evaluation authority：`{str(view.behavioral_evaluation_authority).lower()}`",
        "- 长期指标：`false`",
        "- Learning / Promotion authority：`false / false`",
        "",
        "## Lane",
    ]
    for lane in matrix.lanes:
        lines.append(
            f"- #{lane.order} `{lane.lane_kind}` / `{lane.platform}` / "
            f"`{lane.recovery_status}` / `{lane.evidence_kind}` / `{lane.evidence_id}`"
        )
    return "\n".join(lines)


def _build_matrix(
    contract: EvolutionPostRollbackBehavioralCoverageContract,
    lanes: tuple[EvolutionPostRollbackBehavioralMatrixLane, ...],
) -> EvolutionPostRollbackBehavioralMatrix:
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_BEHAVIORAL_MATRIX_POLICY,
        "workspace_root": contract.workspace_root,
        "outcome_id": contract.outcome_id,
        "outcome_sha256": contract.outcome_sha256,
        "request_id": contract.request_id,
        "coverage_contract_id": contract.contract_id,
        "coverage_contract_sha256": contract.contract_sha256,
        "runtime_verification_id": contract.runtime_verification_id,
        "runtime_verification_sha256": contract.runtime_verification_sha256,
        "before_after_evidence_id": contract.before_after_evidence_id,
        "before_after_evidence_sha256": contract.before_after_evidence_sha256,
        "final_evaluation_id": contract.final_evaluation_id,
        "final_evaluation_sha256": contract.final_evaluation_sha256,
        "lane_count": len(lanes),
        "local_lane_count": sum(
            item.evidence_kind == "local_behavioral_lane" for item in lanes
        ),
        "remote_lane_count": sum(
            item.evidence_kind == "remote_result_ingestion" for item in lanes
        ),
        "lanes": [item.model_dump(mode="json") for item in lanes],
        "recovery_verdict": aggregate_post_rollback_recovery_status(
            tuple(item.recovery_status for item in lanes)
        ),
        "behavioral_evaluation_recorded": True,
        "long_term_metrics_recorded": False,
        "learning_authority": False,
        "promotion_authority": False,
        "recorded_at": max(_aware(item.evaluated_at) for item in lanes).isoformat(),
    }
    digest = _digest(core)
    return EvolutionPostRollbackBehavioralMatrix.model_validate(
        {
            **core,
            "matrix_id": f"evpostmatrix_{digest[:24]}",
            "matrix_sha256": digest,
        }
    )


def _coverage_authoritative(view: EvolutionPostRollbackBehavioralCoverageView) -> bool:
    return bool(
        view.durable_dependencies_valid
        and view.outcome_authority
        and view.runtime_verification_authority
        and view.before_after_authority
        and view.active_baseline_authority
        and len(view.contract.lanes) == view.contract.lane_count
    )


def _matrix(value) -> EvolutionPostRollbackBehavioralMatrix:
    try:
        return EvolutionPostRollbackBehavioralMatrix.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackBehavioralMatrixError(
            "post_rollback_behavioral_matrix_invalid",
            "Post-Rollback Behavioral Matrix 无效。",
        ) from exc


def _restore(raw: str) -> EvolutionPostRollbackBehavioralMatrix:
    if len(str(raw).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("matrix oversized")
    return EvolutionPostRollbackBehavioralMatrix.model_validate_json(raw)


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackreq_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackBehavioralMatrixError(
            "post_rollback_behavioral_matrix_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _outcome_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackout_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackBehavioralMatrixError(
            "post_rollback_behavioral_matrix_outcome_id_invalid",
            "Rollback Outcome ID 格式无效。",
        )
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Behavioral Matrix 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_behavioral_matrices ("
        "matrix_id TEXT PRIMARY KEY, matrix_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, "
        "coverage_contract_id TEXT NOT NULL UNIQUE, matrix_json TEXT NOT NULL, "
        "recorded_at TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_behavioral_matrix_request "
        "ON evolution_post_rollback_behavioral_matrices(request_id, recorded_at);"
    )


__all__ = [
    "EVOLUTION_POST_ROLLBACK_BEHAVIORAL_MATRIX_POLICY",
    "EvolutionPostRollbackBehavioralMatrix",
    "EvolutionPostRollbackBehavioralMatrixError",
    "EvolutionPostRollbackBehavioralMatrixLane",
    "EvolutionPostRollbackBehavioralMatrixService",
    "EvolutionPostRollbackBehavioralMatrixStore",
    "EvolutionPostRollbackBehavioralMatrixView",
    "aggregate_post_rollback_recovery_status",
    "render_post_rollback_behavioral_matrix",
]
