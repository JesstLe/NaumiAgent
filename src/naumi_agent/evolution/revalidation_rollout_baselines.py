"""Freeze exact GREEN evaluation metrics as staged-rollout monitor baselines."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_final_evaluations import (
    EvolutionRevalidationFinalEvaluationStore,
)
from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortStore,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanService,
)
from naumi_agent.harness.eval_models import EvalRunStatus
from naumi_agent.harness.store import HarnessStore

EVOLUTION_REVALIDATION_ROLLOUT_BASELINE_POLICY = (
    "evolution-revalidation-rollout-baseline-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class EvolutionRevalidationRolloutCostSource(StrEnum):
    NO_MODEL_EXECUTION = "no_model_execution"
    LIVE_EVIDENCE = "live_evidence"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRolloutBaseline(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollout-baseline-v1"] = (
        EVOLUTION_REVALIDATION_ROLLOUT_BASELINE_POLICY
    )
    baseline_id: str = Field(pattern=r"^evrerolloutbaseline_[0-9a-f]{24}$")
    baseline_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evrevalfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    cohort_receipt_id: str = Field(pattern=r"^evrevalcohort_[0-9a-f]{24}$")
    cohort_receipt_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    green_batch_id: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=5, le=100)
    green_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    duration_micros: tuple[int, ...] = Field(min_length=5, max_length=100)
    p95_duration_micros: int = Field(ge=0)
    completed_runs: int = Field(ge=0, le=100)
    failed_runs: int = Field(ge=0, le=100)
    completion_rate_basis_points: int = Field(ge=0, le=10_000)
    error_rate_basis_points: int = Field(ge=0, le=10_000)
    cost_microusd: tuple[int, ...] = Field(min_length=5, max_length=100)
    mean_cost_microusd: int = Field(ge=0)
    cost_source: EvolutionRevalidationRolloutCostSource
    raw_h5a_verified: Literal[True] = True
    immutable_baseline: Literal[True] = True
    monitor_input_authority: Literal[True] = True
    stage_advance_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollout Baseline workspace 必须 canonical。")
        lengths = {
            self.sample_count,
            len(self.green_result_sha256),
            len(self.duration_micros),
            len(self.cost_microusd),
            self.completed_runs + self.failed_runs,
        }
        if len(lengths) != 1 or any(
            len(item) != 64 for item in self.green_result_sha256
        ):
            raise ValueError("Rollout Baseline 样本投影不完整。")
        if self.p95_duration_micros != _p95(self.duration_micros):
            raise ValueError("Rollout Baseline p95 投影不一致。")
        if self.completion_rate_basis_points != _basis_points(
            self.completed_runs, self.sample_count
        ) or self.error_rate_basis_points != _basis_points(
            self.failed_runs, self.sample_count
        ):
            raise ValueError("Rollout Baseline rate 投影不一致。")
        if self.mean_cost_microusd != _rounded_div(
            sum(self.cost_microusd), self.sample_count
        ):
            raise ValueError("Rollout Baseline cost 投影不一致。")
        if self.cost_source is EvolutionRevalidationRolloutCostSource.NO_MODEL_EXECUTION and any(
            self.cost_microusd
        ):
            raise ValueError("无模型执行的 Rollout Baseline 成本必须为零。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Rollout Baseline created_at 必须包含 offset。")
        core = self.model_dump(mode="json", exclude={"baseline_id", "baseline_sha256"})
        digest = _digest(core)
        if self.baseline_sha256 != digest or self.baseline_id != (
            f"evrerolloutbaseline_{digest[:24]}"
        ):
            raise ValueError("Rollout Baseline identity 不一致。")
        return self


class EvolutionRevalidationRolloutBaselineView(_StrictModel):
    baseline: EvolutionRevalidationRolloutBaseline
    source_current: bool
    monitor_input_ready: bool
    stage_advance_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        if self.monitor_input_ready is not self.source_current:
            raise ValueError("Rollout Baseline current projection 不一致。")
        return self


class EvolutionRevalidationRolloutBaselineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRolloutBaselineStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(self, plan_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT baseline_json FROM evolution_revalidation_rollout_baselines "
                "WHERE plan_id = ?", (plan_id,)
            )).fetchone()
        return None if row is None else EvolutionRevalidationRolloutBaseline.model_validate_json(
            row["baseline_json"]
        )

    async def record(self, baseline):
        item = EvolutionRevalidationRolloutBaseline.model_validate_json(
            baseline.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_oversized", "Rollout Baseline 超过 512 KiB。"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            plan = await (await db.execute(
                "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans "
                "WHERE plan_id = ?", (item.plan_id,)
            )).fetchone()
            final = await (await db.execute(
                "SELECT receipt_sha256 FROM evolution_revalidation_final_evaluations "
                "WHERE receipt_id = ?", (item.final_evaluation_id,)
            )).fetchone()
            cohort = await (await db.execute(
                "SELECT receipt_sha256 FROM evolution_revalidation_interventional_cohorts "
                "WHERE receipt_id = ?", (item.cohort_receipt_id,)
            )).fetchone()
            if not (
                plan is not None and plan["plan_sha256"] == item.plan_sha256
                and final is not None
                and final["receipt_sha256"] == item.final_evaluation_sha256
                and cohort is not None
                and cohort["receipt_sha256"] == item.cohort_receipt_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationRolloutBaselineError(
                    "rollout_baseline_dependency_mismatch",
                    "Rollout Baseline 的 Plan、Final 或 cohort 依赖不一致。",
                )
            existing = await (await db.execute(
                "SELECT baseline_json FROM evolution_revalidation_rollout_baselines "
                "WHERE plan_id = ?", (item.plan_id,)
            )).fetchone()
            if existing is not None:
                restored = EvolutionRevalidationRolloutBaseline.model_validate_json(
                    existing["baseline_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationRolloutBaselineError(
                        "rollout_baseline_conflict",
                        "同一 Rollout Plan 已绑定不同 Baseline。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_rollout_baselines "
                "(baseline_id, baseline_sha256, plan_id, contract_id, baseline_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (item.baseline_id, item.baseline_sha256, item.plan_id, item.contract_id,
                 encoded, item.created_at),
            )
            await db.commit()
        return item


class EvolutionRevalidationRolloutBaselineService:
    def __init__(self, *, workspace_root, plan_service, final_store, cohort_store,
                 harness_store, store) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.plan_service: EvolutionRevalidationRolloutPlanService = plan_service
        self.final_store: EvolutionRevalidationFinalEvaluationStore = final_store
        self.cohort_store: EvolutionRevalidationInterventionalCohortStore = cohort_store
        self.harness_store: HarnessStore = harness_store
        self.store: EvolutionRevalidationRolloutBaselineStore = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue(self, *, plan_id: str):
        lock = self._locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get(plan_id)
            if existing is not None:
                return await self.inspect(plan_id=plan_id)
            plan_view = await self.plan_service.inspect(plan_id=plan_id)
            if not plan_view.current_rollout_eligible:
                raise EvolutionRevalidationRolloutBaselineError(
                    "rollout_baseline_plan_stale", "Rollout Plan 当前不可用于 Baseline。"
                )
            proposed = await self._derive(plan_view.plan)
            refreshed = await self.plan_service.inspect(plan_id=plan_id)
            if not refreshed.current_rollout_eligible:
                raise EvolutionRevalidationRolloutBaselineError(
                    "rollout_baseline_plan_changed", "Rollout Plan 在持久化前失效。"
                )
            stored = await self.store.record(proposed)
            return EvolutionRevalidationRolloutBaselineView(
                baseline=stored, source_current=True, monitor_input_ready=True
            )

    async def inspect(self, *, plan_id: str):
        baseline = await self.store.get(plan_id)
        if baseline is None:
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_missing", "Rollout Baseline 不存在。"
            )
        try:
            plan_view = await self.plan_service.inspect(plan_id=plan_id)
            current = plan_view.current_rollout_eligible and (
                await self._derive(plan_view.plan)
            ) == baseline
        except (OSError, TypeError, ValueError, RuntimeError):
            current = False
        return EvolutionRevalidationRolloutBaselineView(
            baseline=baseline,
            source_current=bool(current),
            monitor_input_ready=bool(current),
        )

    async def _derive(self, plan):
        final = await self.final_store.get(plan.contract_id)
        cohort = await self.cohort_store.get_by_contract(plan.contract_id)
        if not (
            final is not None
            and final.receipt_id == plan.final_evaluation_id
            and final.receipt_sha256 == plan.final_evaluation_sha256
            and final.contract.contract_sha256 == plan.contract_sha256
            and cohort is not None
            and cohort.receipt_id == final.interventional.cohort_receipt_id
            and cohort.receipt_sha256 == final.interventional.cohort_receipt_sha256
            and cohort.contract_sha256 == plan.contract_sha256
        ):
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_evidence_mismatch",
                "Rollout Baseline 缺少 exact Final/cohort evidence。",
            )
        records = await self.harness_store.list_eval_results(
            self.workspace_root,
            cohort.green_batch_id,
            cohort.suite_id,
            limit=cohort.requested_samples,
        )
        if len(records) != cohort.requested_samples or tuple(
            item.result_sha256 for item in records
        ) != cohort.green_result_sha256 or tuple(
            item.sample_index for item in records
        ) != tuple(range(cohort.requested_samples)):
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_h5a_incomplete",
                "Rollout Baseline 引用的 GREEN H5a 不完整或已漂移。",
            )
        durations = tuple(_micros(item.result.duration_ms) for item in records)
        completed = sum(item.result.status is EvalRunStatus.PASSED for item in records)
        costs, cost_source = _costs(records)
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_BASELINE_POLICY,
            "workspace_root": plan.workspace_root,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "contract_id": plan.contract_id,
            "contract_sha256": plan.contract_sha256,
            "final_evaluation_id": final.receipt_id,
            "final_evaluation_sha256": final.receipt_sha256,
            "cohort_receipt_id": cohort.receipt_id,
            "cohort_receipt_sha256": cohort.receipt_sha256,
            "suite_id": cohort.suite_id,
            "green_batch_id": cohort.green_batch_id,
            "sample_count": len(records),
            "green_result_sha256": list(cohort.green_result_sha256),
            "duration_micros": list(durations),
            "p95_duration_micros": _p95(durations),
            "completed_runs": completed,
            "failed_runs": len(records) - completed,
            "completion_rate_basis_points": _basis_points(completed, len(records)),
            "error_rate_basis_points": _basis_points(len(records) - completed, len(records)),
            "cost_microusd": list(costs),
            "mean_cost_microusd": _rounded_div(sum(costs), len(records)),
            "cost_source": cost_source.value,
            "raw_h5a_verified": True,
            "immutable_baseline": True,
            "monitor_input_authority": True,
            "stage_advance_authority": False,
            "rollback_authority": False,
            "promotion_authority": False,
            "created_at": final.created_at,
        }
        digest = _digest(core)
        return EvolutionRevalidationRolloutBaseline.model_validate({
            **core,
            "baseline_id": f"evrerolloutbaseline_{digest[:24]}",
            "baseline_sha256": digest,
        })


def _costs(records):
    values = []
    any_live = False
    for record in records:
        identity = record.result.baseline_identity
        if identity is None:
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_identity_missing",
                "GREEN H5a 缺少 exact Baseline Identity。",
            )
        lives = tuple(
            case.live_evidence for case in record.result.cases
            if case.live_evidence is not None
        )
        live_run = identity.configuration.live
        if live_run is not bool(lives):
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_live_evidence_mismatch",
                "GREEN H5a live 配置与成本证据不一致。",
            )
        any_live = any_live or live_run
        if any(item.cost_source == "unavailable" for item in lives):
            raise EvolutionRevalidationRolloutBaselineError(
                "rollout_baseline_cost_unavailable",
                "GREEN H5a 包含 live case，但成本来源不可用。",
            )
        values.append(sum(_microusd(item.cost_usd) for item in lives))
    source = (
        EvolutionRevalidationRolloutCostSource.LIVE_EVIDENCE
        if any_live else EvolutionRevalidationRolloutCostSource.NO_MODEL_EXECUTION
    )
    return tuple(values), source


def _micros(milliseconds):
    value = float(milliseconds)
    if not math.isfinite(value) or value < 0:
        raise EvolutionRevalidationRolloutBaselineError(
            "rollout_baseline_duration_invalid", "GREEN H5a duration 无效。"
        )
    return int(round(value * 1_000))


def _microusd(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise EvolutionRevalidationRolloutBaselineError(
            "rollout_baseline_cost_invalid", "GREEN H5a cost 无效。"
        )
    return int(round(number * 1_000_000))


def _p95(values):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _basis_points(numerator, denominator):
    return _rounded_div(numerator * 10_000, denominator)


def _rounded_div(numerator, denominator):
    return (numerator + denominator // 2) // denominator


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollout_baselines ("
        "baseline_id TEXT PRIMARY KEY, baseline_sha256 TEXT NOT NULL UNIQUE, "
        "plan_id TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL, "
        "baseline_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLOUT_BASELINE_POLICY",
    "EvolutionRevalidationRolloutBaseline",
    "EvolutionRevalidationRolloutBaselineError",
    "EvolutionRevalidationRolloutBaselineService",
    "EvolutionRevalidationRolloutBaselineStore",
    "EvolutionRevalidationRolloutBaselineView",
    "EvolutionRevalidationRolloutCostSource",
]
