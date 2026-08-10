"""Aggregate current stable execution outcomes into stage evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineError,
    EvolutionRevalidationRolloutBaselineService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
    EvolutionRevalidationRolloutStageName,
)
from naumi_agent.evolution.revalidation_stable_execution_outcome_ledger import (
    EvolutionRevalidationStableExecutionOutcomeLedgerService,
)
from naumi_agent.evolution.revalidation_stable_execution_outcomes import (
    EvolutionRevalidationStableExecutionOutcomeError,
)
from naumi_agent.evolution.revalidation_stable_observation_window_assessments import (
    EvolutionRevalidationStableObservationAssessmentError,
    EvolutionRevalidationStableObservationWindowService,
)
from naumi_agent.evolution.revalidation_stage_completion_metrics import (
    EvolutionRevalidationStageCompletionMetrics,
    EvolutionRevalidationStageCompletionStatus,
    calculate_stage_completion_metrics,
)

EVOLUTION_REVALIDATION_STABLE_STAGE_COMPLETION_POLICY = (
    "evolution-revalidation-stable-stage-completion-v1"
)
_MAX_OUTCOMES = 100
_MAX_ARTIFACT_BYTES = 512 * 1024
_INTENT_RE = re.compile(r"^evrestableintent_[0-9a-f]{24}$")
_EVIDENCE_RE = re.compile(r"^evrestablecomplete_[0-9a-f]{24}$")
_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableStageCompletion(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-stable-stage-completion-v1"] = (
        EVOLUTION_REVALIDATION_STABLE_STAGE_COMPLETION_POLICY
    )
    evidence_id: str = Field(pattern=r"^evrestablecomplete_[0-9a-f]{24}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    subject_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,95}$")
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_id: str = Field(pattern=r"^evrerolloutbaseline_[0-9a-f]{24}$")
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    liveness_window_id: str = Field(pattern=r"^evrestablewindow_[0-9a-f]{24}$")
    liveness_window_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=10_000)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    exposure_percent: Literal[100] = 100
    stage_order: Literal[4] = 4
    completed_stage: Literal["stable"] = "stable"
    outcome_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    outcome_sha256: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    run_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    authoritative_outcome_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    successful_outcome_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    duration_micros: tuple[int, ...] = Field(max_length=_MAX_OUTCOMES)
    reported_cost_microusd: tuple[int, ...] = Field(max_length=_MAX_OUTCOMES)
    metrics: EvolutionRevalidationStageCompletionMetrics
    assessed_at: str = Field(min_length=1, max_length=100)
    liveness_verified: Literal[True] = True
    outcomes_revalidated: Literal[True] = True
    reported_cost_source_authority: Literal[True] = True
    billing_authority: Literal[False] = False
    stable_stage_completion_authority: bool
    pause_input_authority: bool
    rollback_input_authority: bool
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Stage Completion workspace 必须 canonical。")
        authoritative = set(self.authoritative_outcome_ids)
        successful = set(self.successful_outcome_ids)
        if not (
            len(self.outcome_ids) == len(self.outcome_sha256) == len(self.run_ids)
            and len(set(self.outcome_ids)) == len(self.outcome_ids)
            and len(set(self.run_ids)) == len(self.run_ids)
            and len(authoritative) == len(self.authoritative_outcome_ids)
            and len(successful) == len(self.successful_outcome_ids)
            and authoritative.issubset(self.outcome_ids)
            and successful.issubset(authoritative)
            and len(self.duration_micros) == len(authoritative)
            and len(self.reported_cost_microusd) == len(authoritative)
            and self.metrics.observed_runs == len(authoritative)
            and self.metrics.successful_runs == len(successful)
        ):
            raise ValueError("Stable Stage Completion outcome projection 不一致。")
        expected_metrics = calculate_stage_completion_metrics(
            durations_micros=self.duration_micros,
            reported_cost_microusd=self.reported_cost_microusd,
            successful_runs=len(successful),
            minimum_completed_runs=self.metrics.minimum_completed_runs,
            max_error_rate_basis_points=self.metrics.max_error_rate_basis_points,
            max_completion_rate_drop_basis_points=(
                self.metrics.max_completion_rate_drop_basis_points
            ),
            max_p95_latency_regression_basis_points=(
                self.metrics.max_p95_latency_regression_basis_points
            ),
            max_cost_regression_basis_points=(self.metrics.max_cost_regression_basis_points),
            baseline_completion_rate_basis_points=(
                self.metrics.baseline_completion_rate_basis_points
            ),
            baseline_p95_duration_micros=(self.metrics.baseline_p95_duration_micros),
            baseline_mean_cost_microusd=self.metrics.baseline_mean_cost_microusd,
            baseline_cost_source=self.metrics.baseline_cost_source,
        )
        passing = self.metrics.status is EvolutionRevalidationStageCompletionStatus.PASSING
        breached = self.metrics.status is EvolutionRevalidationStageCompletionStatus.BREACHED
        if not (
            self.metrics == expected_metrics
            and self.stable_stage_completion_authority is passing
            and self.pause_input_authority is breached
            and self.rollback_input_authority is breached
            and _aware(self.assessed_at)
        ):
            raise ValueError("Stable Stage Completion authority projection 不一致。")
        expected_source = _source_set_digest(self)
        if self.source_set_sha256 != expected_source:
            raise ValueError("Stable Stage Completion source-set digest 不一致。")
        core = self.model_dump(mode="json", exclude={"evidence_id", "evidence_sha256"})
        digest = _digest(core)
        if self.evidence_sha256 != digest or self.evidence_id != (
            f"evrestablecomplete_{digest[:24]}"
        ):
            raise ValueError("Stable Stage Completion identity 不一致。")
        return self


class EvolutionRevalidationStableStageCompletionView(_StrictModel):
    receipt: EvolutionRevalidationStableStageCompletion
    evidence_source_current: bool
    latest_assessment: bool
    plan_source_current: bool
    baseline_source_current: bool
    liveness_source_current: bool
    outcome_set_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    stable_stage_completion_authority: bool
    pause_input_authority: bool
    rollback_input_authority: bool
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.evidence_source_current
            and self.latest_assessment
            and self.plan_source_current
            and self.baseline_source_current
            and self.liveness_source_current
            and self.outcome_set_current
        )
        passing = self.receipt.metrics.status is (
            EvolutionRevalidationStageCompletionStatus.PASSING
        )
        breached = self.receipt.metrics.status is (
            EvolutionRevalidationStageCompletionStatus.BREACHED
        )
        if not (
            self.stable_stage_completion_authority is (current and passing)
            and self.pause_input_authority is (current and breached)
            and self.rollback_input_authority is self.pause_input_authority
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Stage Completion View projection 不一致。")
        return self


class EvolutionRevalidationStableStageCompletionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationStableStageCompletionStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        outcome_service: EvolutionRevalidationStableExecutionOutcomeLedgerService,
        window_service: EvolutionRevalidationStableObservationWindowService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        baseline_service: EvolutionRevalidationRolloutBaselineService,
    ) -> None:
        if not isinstance(
            outcome_service,
            EvolutionRevalidationStableExecutionOutcomeLedgerService,
        ):
            raise TypeError("Stable Stage Completion Store 需要 Outcome Service。")
        if not isinstance(
            window_service,
            EvolutionRevalidationStableObservationWindowService,
        ):
            raise TypeError("Stable Stage Completion Store 需要 Window Service。")
        if not isinstance(plan_service, EvolutionRevalidationRolloutPlanService):
            raise TypeError("Stable Stage Completion Store 需要 Plan Service。")
        if not isinstance(baseline_service, EvolutionRevalidationRolloutBaselineService):
            raise TypeError("Stable Stage Completion Store 需要 Baseline Service。")
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            self.db_path
            == outcome_service.store.db_path
            == window_service.store.db_path
            == plan_service.store.db_path
            == baseline_service.store.db_path
        ):
            raise ValueError("Stable Stage Completion 必须共用同一证据数据库。")
        self.outcome_service = outcome_service
        self.outcome_store = outcome_service.store
        self.window_service = window_service
        self.plan_service = plan_service
        self.baseline_service = baseline_service

    async def get(
        self,
        evidence_id: str,
    ) -> EvolutionRevalidationStableStageCompletion | None:
        return await self._read(
            "WHERE evidence_id = ?",
            (_evidence_id(evidence_id),),
        )

    async def latest(
        self,
        intent_id: str,
    ) -> EvolutionRevalidationStableStageCompletion | None:
        return await self._read(
            "WHERE intent_id = ? ORDER BY rowid DESC LIMIT 1",
            (_intent(intent_id),),
        )

    async def _read(self, where: str, parameters: tuple[object, ...]):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT evidence_json FROM "
                        "evolution_revalidation_stable_stage_completions " + where,
                        parameters,
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["evidence_json"]))
        except EvolutionRevalidationStableStageCompletionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_source_unavailable",
                "Stable Stage Completion durable source 当前不可读取。",
            ) from exc

    async def record(
        self,
        receipt: EvolutionRevalidationStableStageCompletion,
    ) -> EvolutionRevalidationStableStageCompletion:
        item = _validated(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_oversized",
                "Stable Stage Completion 超过 512 KiB。",
            )
        await self._require_live_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await self._require_durable_sources(db, item)
                same_source = await (
                    await db.execute(
                        "SELECT evidence_json FROM "
                        "evolution_revalidation_stable_stage_completions "
                        "WHERE intent_id = ? AND source_set_sha256 = ?",
                        (item.intent_id, item.source_set_sha256),
                    )
                ).fetchone()
                if same_source is not None:
                    restored = _restore(str(same_source["evidence_json"]))
                    await db.rollback()
                    return restored
                existing = await (
                    await db.execute(
                        "SELECT evidence_json FROM "
                        "evolution_revalidation_stable_stage_completions "
                        "WHERE evidence_id = ?",
                        (item.evidence_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["evidence_json"]))
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationStableStageCompletionError(
                            "stable_stage_completion_identity_conflict",
                            "同一 Stable Stage Completion ID 已绑定不同内容。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_stable_stage_completions "
                    "(evidence_id, evidence_sha256, intent_id, status, "
                    "source_set_sha256, evidence_json, assessed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.evidence_id,
                        item.evidence_sha256,
                        item.intent_id,
                        item.metrics.status.value,
                        item.source_set_sha256,
                        encoded,
                        item.assessed_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationStableStageCompletionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_store_error",
                "Stable Stage Completion 无法持久化。",
            ) from exc
        return item

    async def _require_live_sources(
        self,
        item: EvolutionRevalidationStableStageCompletion,
    ) -> None:
        snapshot = await _load_snapshot(
            workspace_root=Path(item.workspace_root),
            intent_id=item.intent_id,
            subject_id=item.subject_id,
            outcome_service=self.outcome_service,
            window_service=self.window_service,
            plan_service=self.plan_service,
            baseline_service=self.baseline_service,
            outcome_store=self.outcome_store,
            db_path=self.db_path,
        )
        if snapshot != _snapshot_signature(item):
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_live_source_changed",
                "Stable Stage Completion 的 current source 已变化。",
            )

    async def _require_durable_sources(
        self,
        db: aiosqlite.Connection,
        item: EvolutionRevalidationStableStageCompletion,
    ) -> None:
        plan = await (
            await db.execute(
                "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans WHERE plan_id = ?",
                (item.plan_id,),
            )
        ).fetchone()
        baseline = await (
            await db.execute(
                "SELECT baseline_sha256 FROM evolution_revalidation_rollout_baselines "
                "WHERE baseline_id = ?",
                (item.baseline_id,),
            )
        ).fetchone()
        window = await (
            await db.execute(
                "SELECT window_sha256 FROM "
                "evolution_revalidation_stable_observation_windows "
                "WHERE window_id = ?",
                (item.liveness_window_id,),
            )
        ).fetchone()
        outcomes = await (
            await db.execute(
                "SELECT outcome_id, outcome_sha256 FROM "
                "evolution_revalidation_stable_execution_outcomes "
                "WHERE intent_id = ? "
                "ORDER BY execution_completed_at, outcome_id LIMIT ?",
                (item.intent_id, _MAX_OUTCOMES + 1),
            )
        ).fetchall()
        projection = (
            tuple(str(row["outcome_id"]) for row in outcomes),
            tuple(str(row["outcome_sha256"]) for row in outcomes),
        )
        if not (
            plan is not None
            and plan["plan_sha256"] == item.plan_sha256
            and baseline is not None
            and baseline["baseline_sha256"] == item.baseline_sha256
            and window is not None
            and window["window_sha256"] == item.liveness_window_sha256
            and len(outcomes) <= _MAX_OUTCOMES
            and projection == (item.outcome_ids, item.outcome_sha256)
        ):
            await db.rollback()
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_dependency_changed",
                "Stable Stage Completion 的 durable dependency 已变化。",
            )


class EvolutionRevalidationStableStageCompletionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationStableExecutionOutcomeLedgerService,
        window_service: EvolutionRevalidationStableObservationWindowService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        baseline_service: EvolutionRevalidationRolloutBaselineService,
        store: EvolutionRevalidationStableStageCompletionStore,
        clock=None,
    ) -> None:
        if not isinstance(store, EvolutionRevalidationStableStageCompletionStore):
            raise TypeError("Stable Stage Completion 需要 Stage Completion Store。")
        if not (
            outcome_service is store.outcome_service
            and outcome_service.window_service is window_service
            and store.window_service is window_service
            and store.plan_service is plan_service
            and store.baseline_service is baseline_service
        ):
            raise ValueError("Stable Stage Completion durable source 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not (
            self.workspace_root
            == outcome_service.workspace_root
            == window_service.workspace_root
            == plan_service.workspace_root
            == baseline_service.workspace_root
        ):
            raise ValueError("Stable Stage Completion workspace 必须一致。")
        self.outcome_service = outcome_service
        self.window_service = window_service
        self.plan_service = plan_service
        self.baseline_service = baseline_service
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def assess(
        self,
        *,
        intent_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableStageCompletionView:
        intent = _intent(intent_id)
        subject = _subject(subject_id)
        lock = self._locks.setdefault(intent, asyncio.Lock())
        async with lock:
            snapshot = await self._snapshot(intent, subject)
            latest = await self.store.latest(intent)
            if latest is not None and _snapshot_signature(latest) == snapshot:
                return await self._view(latest, subject_id=subject)
            receipt = _build_receipt(snapshot, _aware(self.clock()).isoformat())
            stored = await self.store.record(receipt)
            return await self._view(stored, subject_id=subject)

    async def inspect(
        self,
        *,
        evidence_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableStageCompletionView:
        receipt = await self.store.get(evidence_id)
        if receipt is None:
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_missing",
                "指定的 Stable Stage Completion 不存在。",
            )
        return await self._view(receipt, subject_id=_subject(subject_id))

    async def _snapshot(self, intent_id: str, subject_id: str) -> dict:
        return await _load_snapshot(
            workspace_root=self.workspace_root,
            intent_id=intent_id,
            subject_id=subject_id,
            outcome_service=self.outcome_service,
            window_service=self.window_service,
            plan_service=self.plan_service,
            baseline_service=self.baseline_service,
            outcome_store=self.store.outcome_store,
            db_path=self.store.db_path,
        )

    async def _view(self, receipt, *, subject_id: str):
        reasons: list[str] = []
        try:
            evidence_source_current = await self.store.get(receipt.evidence_id) == receipt
        except EvolutionRevalidationStableStageCompletionError:
            evidence_source_current = False
        if not evidence_source_current:
            reasons.append("evidence_source_changed")
        try:
            latest_assessment = await self.store.latest(receipt.intent_id) == receipt
        except EvolutionRevalidationStableStageCompletionError:
            latest_assessment = False
        if not latest_assessment:
            reasons.append("newer_assessment_exists")
        plan_source_current = baseline_source_current = False
        try:
            plan_view = await self.plan_service.inspect(plan_id=receipt.plan_id)
            plan_source_current = bool(
                plan_view.current_rollout_eligible
                and plan_view.plan.plan_sha256 == receipt.plan_sha256
            )
        except (EvolutionRevalidationRolloutPlanError, OSError, TypeError, ValueError):
            pass
        if not plan_source_current:
            reasons.append("plan_source_changed")
        try:
            baseline_view = await self.baseline_service.inspect(plan_id=receipt.plan_id)
            baseline_source_current = bool(
                baseline_view.monitor_input_ready
                and baseline_view.baseline.baseline_id == receipt.baseline_id
                and baseline_view.baseline.baseline_sha256 == receipt.baseline_sha256
            )
        except (
            EvolutionRevalidationRolloutBaselineError,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        if not baseline_source_current:
            reasons.append("baseline_source_changed")
        liveness_source_current = False
        try:
            window_view = await self.window_service.inspect(
                intent_id=receipt.intent_id,
                subject_id=subject_id,
            )
            liveness_source_current = bool(
                window_view.stable_runtime_window_authority
                and window_view.receipt.window_id == receipt.liveness_window_id
                and window_view.receipt.window_sha256 == receipt.liveness_window_sha256
            )
        except (
            EvolutionRevalidationStableObservationAssessmentError,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        if not liveness_source_current:
            reasons.append("liveness_source_changed")
        try:
            outcome_set_current = await self._snapshot(
                receipt.intent_id, subject_id
            ) == _snapshot_signature(receipt)
        except (
            EvolutionRevalidationStableStageCompletionError,
            EvolutionRevalidationStableObservationAssessmentError,
            EvolutionRevalidationRolloutBaselineError,
            EvolutionRevalidationRolloutPlanError,
            OSError,
            TypeError,
            ValueError,
        ):
            outcome_set_current = False
        if not outcome_set_current:
            reasons.append("outcome_set_changed")
        current = bool(
            evidence_source_current
            and latest_assessment
            and plan_source_current
            and baseline_source_current
            and liveness_source_current
            and outcome_set_current
        )
        passing = receipt.metrics.status is (EvolutionRevalidationStageCompletionStatus.PASSING)
        breached = receipt.metrics.status is (EvolutionRevalidationStageCompletionStatus.BREACHED)
        return EvolutionRevalidationStableStageCompletionView(
            receipt=receipt,
            evidence_source_current=evidence_source_current,
            latest_assessment=latest_assessment,
            plan_source_current=plan_source_current,
            baseline_source_current=baseline_source_current,
            liveness_source_current=liveness_source_current,
            outcome_set_current=outcome_set_current,
            invalidation_reasons=tuple(sorted(set(reasons))),
            stable_stage_completion_authority=current and passing,
            pause_input_authority=current and breached,
            rollback_input_authority=current and breached,
        )


async def _load_snapshot(
    *,
    workspace_root: Path,
    intent_id: str,
    subject_id: str,
    outcome_service,
    window_service,
    plan_service,
    baseline_service,
    outcome_store,
    db_path: Path,
) -> dict:
    try:
        window_view = await window_service.inspect(
            intent_id=intent_id,
            subject_id=subject_id,
        )
    except EvolutionRevalidationStableObservationAssessmentError as exc:
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_liveness_unavailable",
            "缺少当前可用的 Stable runtime window。",
        ) from exc
    if not window_view.stable_runtime_window_authority:
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_liveness_not_authoritative",
            "当前 Stable runtime window 不具备 Stage Completion 准入权限。",
        )
    window = window_view.receipt
    intent = window.exposure.deployment.preparation.intent
    plan_view = await plan_service.inspect(plan_id=intent.plan.plan_id)
    stage = plan_view.plan.stages[3]
    if not (
        plan_view.current_rollout_eligible
        and plan_view.plan.plan_sha256 == intent.plan.plan_sha256
        and stage == intent.plan.stages[3]
        and stage.name is EvolutionRevalidationRolloutStageName.STABLE
        and stage.exposure_percent == 100
        and intent.intent_id == intent_id
        and window.exposure.binding.subject_id == subject_id
        and window.population_snapshot_id == intent.population_snapshot_id
        and window.population_snapshot_sha256 == intent.population_snapshot_sha256
        and window.population_snapshot_sequence == intent.population_snapshot_sequence
        and window.population_denominator == intent.population_denominator
        and window.installation_member_id == intent.proof.payload.member_id
    ):
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_plan_stale",
            "Stable Rollout Plan、Intent 或冻结 Stage 已失效。",
        )
    baseline_view = await baseline_service.inspect(plan_id=plan_view.plan.plan_id)
    baseline = baseline_view.baseline
    if not (
        baseline_view.monitor_input_ready
        and baseline.plan_sha256 == plan_view.plan.plan_sha256
        and baseline.workspace_root == str(workspace_root)
    ):
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_baseline_stale",
            "Stable Stage Completion 的 GREEN Baseline 已失效。",
        )
    outcomes = await outcome_store.list_for_intent(
        intent_id,
        limit=_MAX_OUTCOMES,
    )
    if (
        len(outcomes) == _MAX_OUTCOMES
        and await _outcome_count(
            db_path,
            intent_id,
        )
        > _MAX_OUTCOMES
    ):
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_outcome_limit_exceeded",
            "Stable Stage Completion outcome 超过 100 条证据上限。",
        )
    authoritative = []
    successful_ids = []
    for outcome in outcomes:
        source = outcome.liveness_source
        if not (
            source.intent_id == intent_id
            and outcome.workspace_root == str(workspace_root)
            and source.binding_id == window.exposure.binding.binding_id
            and source.binding_sha256 == window.exposure.binding.binding_sha256
            and source.candidate_version == intent.candidate_version
            and source.candidate_target == intent.installation_target
            and source.population_snapshot_id == window.population_snapshot_id
            and source.population_snapshot_sha256 == window.population_snapshot_sha256
            and source.population_snapshot_sequence == window.population_snapshot_sequence
            and source.population_denominator == window.population_denominator
            and source.installation_member_id == window.installation_member_id
            and source.exposure_percent == 100
        ):
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_outcome_scope_mismatch",
                "Execution Outcome 与当前 stable intent/release 不一致。",
            )
        try:
            view = await outcome_service.inspect(outcome_id=outcome.outcome_id)
        except EvolutionRevalidationStableExecutionOutcomeError as exc:
            raise EvolutionRevalidationStableStageCompletionError(
                "stable_stage_completion_outcome_source_unavailable",
                "Stable Stage Completion 的 Outcome source 当前不可用。",
            ) from exc
        if view.execution_outcome_authority:
            authoritative.append(outcome)
            if view.successful_completed_run_authority:
                successful_ids.append(outcome.outcome_id)
    durations = tuple(item.duration_ms * 1_000 for item in authoritative)
    costs = tuple(_reported_microusd(item.usage.reported_cost_usd) for item in authoritative)
    metrics = calculate_stage_completion_metrics(
        durations_micros=durations,
        reported_cost_microusd=costs,
        successful_runs=len(successful_ids),
        minimum_completed_runs=stage.minimum_completed_runs,
        max_error_rate_basis_points=stage.max_error_rate_basis_points,
        max_completion_rate_drop_basis_points=(stage.max_completion_rate_drop_basis_points),
        max_p95_latency_regression_basis_points=(stage.max_p95_latency_regression_basis_points),
        max_cost_regression_basis_points=stage.max_cost_regression_basis_points,
        baseline_completion_rate_basis_points=baseline.completion_rate_basis_points,
        baseline_p95_duration_micros=baseline.p95_duration_micros,
        baseline_mean_cost_microusd=baseline.mean_cost_microusd,
        baseline_cost_source=baseline.cost_source,
    )
    return {
        "workspace_root": str(workspace_root),
        "intent_id": intent_id,
        "subject_id": subject_id,
        "plan_id": plan_view.plan.plan_id,
        "plan_sha256": plan_view.plan.plan_sha256,
        "baseline_id": baseline.baseline_id,
        "baseline_sha256": baseline.baseline_sha256,
        "liveness_window_id": window.window_id,
        "liveness_window_sha256": window.window_sha256,
        "binding_id": window.exposure.binding.binding_id,
        "binding_sha256": window.exposure.binding.binding_sha256,
        "candidate_version": intent.candidate_version,
        "candidate_target": intent.installation_target,
        "population_snapshot_id": window.population_snapshot_id,
        "population_snapshot_sha256": window.population_snapshot_sha256,
        "population_snapshot_sequence": window.population_snapshot_sequence,
        "population_denominator": window.population_denominator,
        "installation_member_id": window.installation_member_id,
        "exposure_percent": 100,
        "outcome_ids": tuple(item.outcome_id for item in outcomes),
        "outcome_sha256": tuple(item.outcome_sha256 for item in outcomes),
        "run_ids": tuple(item.run_id for item in outcomes),
        "authoritative_outcome_ids": tuple(item.outcome_id for item in authoritative),
        "successful_outcome_ids": tuple(successful_ids),
        "duration_micros": durations,
        "reported_cost_microusd": costs,
        "metrics": metrics,
    }


def _build_receipt(snapshot: dict, assessed_at: str):
    source_set_sha256 = _digest(
        {
            key: _jsonable(snapshot[key])
            for key in (
                "plan_id",
                "plan_sha256",
                "baseline_id",
                "baseline_sha256",
                "liveness_window_id",
                "liveness_window_sha256",
                "outcome_ids",
                "outcome_sha256",
                "authoritative_outcome_ids",
                "successful_outcome_ids",
            )
        }
    )
    status = snapshot["metrics"].status
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_STABLE_STAGE_COMPLETION_POLICY,
        "source_set_sha256": source_set_sha256,
        **snapshot,
        "stage_order": 4,
        "completed_stage": "stable",
        "assessed_at": _aware(assessed_at).isoformat(),
        "liveness_verified": True,
        "outcomes_revalidated": True,
        "reported_cost_source_authority": True,
        "billing_authority": False,
        "stable_stage_completion_authority": (
            status is EvolutionRevalidationStageCompletionStatus.PASSING
        ),
        "pause_input_authority": (status is EvolutionRevalidationStageCompletionStatus.BREACHED),
        "rollback_input_authority": (status is EvolutionRevalidationStageCompletionStatus.BREACHED),
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    normalized = {key: _jsonable(value) for key, value in core.items()}
    digest = _digest(normalized)
    return EvolutionRevalidationStableStageCompletion.model_validate(
        {
            **normalized,
            "evidence_id": f"evrestablecomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )


def _snapshot_signature(receipt) -> dict:
    snapshot = receipt.model_dump(
        mode="python",
        include={
            "workspace_root",
            "intent_id",
            "subject_id",
            "plan_id",
            "plan_sha256",
            "baseline_id",
            "baseline_sha256",
            "liveness_window_id",
            "liveness_window_sha256",
            "binding_id",
            "binding_sha256",
            "candidate_version",
            "candidate_target",
            "population_snapshot_id",
            "population_snapshot_sha256",
            "population_snapshot_sequence",
            "population_denominator",
            "installation_member_id",
            "exposure_percent",
            "outcome_ids",
            "outcome_sha256",
            "run_ids",
            "authoritative_outcome_ids",
            "successful_outcome_ids",
            "duration_micros",
            "reported_cost_microusd",
            "metrics",
        },
    )
    snapshot["metrics"] = receipt.metrics
    return snapshot


def _source_set_digest(receipt) -> str:
    return _digest(
        {
            key: _jsonable(getattr(receipt, key))
            for key in (
                "plan_id",
                "plan_sha256",
                "baseline_id",
                "baseline_sha256",
                "liveness_window_id",
                "liveness_window_sha256",
                "outcome_ids",
                "outcome_sha256",
                "authoritative_outcome_ids",
                "successful_outcome_ids",
            )
        }
    )


def _reported_microusd(value: Decimal) -> int:
    return int((value * 1_000_000).to_integral_value())


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stable Stage Completion timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _intent(value: str) -> str:
    if not isinstance(value, str) or _INTENT_RE.fullmatch(value) is None:
        raise ValueError("intent_id 必须是稳定的 Stable Intent 标识。")
    return value


def _evidence_id(value: str) -> str:
    if not isinstance(value, str) or _EVIDENCE_RE.fullmatch(value) is None:
        raise ValueError("evidence_id 必须是稳定的 Stable Completion 标识。")
    return value


def _subject(value: str) -> str:
    if not isinstance(value, str) or _SUBJECT_RE.fullmatch(value) is None:
        raise ValueError("subject_id 必须是稳定的 runtime 标识。")
    return value


def _validated(value):
    try:
        return EvolutionRevalidationStableStageCompletion.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_invalid",
            "Stable Stage Completion artifact 无效。",
        ) from exc


def _restore(value: str):
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_source_oversized",
            "Stable Stage Completion durable source 超过上限。",
        )
    try:
        return EvolutionRevalidationStableStageCompletion.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableStageCompletionError(
            "stable_stage_completion_source_invalid",
            "Stable Stage Completion durable source 无效。",
        ) from exc


async def _outcome_count(db_path: Path, intent_id: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM "
                "evolution_revalidation_stable_execution_outcomes "
                "WHERE intent_id = ?",
                (intent_id,),
            )
        ).fetchone()
    return 0 if row is None else int(row[0])


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_stable_stage_completions (
            evidence_id TEXT PRIMARY KEY,
            evidence_sha256 TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            status TEXT NOT NULL,
            source_set_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            assessed_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_evolution_revalidation_stable_stage_completions_intent "
        "ON evolution_revalidation_stable_stage_completions "
        "(intent_id, assessed_at, evidence_id)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_evolution_revalidation_stable_stage_completions_source "
        "ON evolution_revalidation_stable_stage_completions "
        "(intent_id, source_set_sha256)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_STABLE_STAGE_COMPLETION_POLICY",
    "EvolutionRevalidationStableStageCompletion",
    "EvolutionRevalidationStableStageCompletionError",
    "EvolutionRevalidationStableStageCompletionService",
    "EvolutionRevalidationStableStageCompletionStore",
    "EvolutionRevalidationStableStageCompletionView",
]
