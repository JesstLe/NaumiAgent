"""Immutable staged-rollout plans derived from a current approved Fresh Decision."""

from __future__ import annotations

import asyncio
import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_approval_decisions import (
    EvolutionRevalidationApprovalDecisionReceipt,
    EvolutionRevalidationApprovalDecisionService,
    EvolutionRevalidationApprovalDecisionStatus,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInput,
    EvolutionRevalidationPromotionInputService,
)

EVOLUTION_REVALIDATION_ROLLOUT_PLAN_POLICY = "evolution-revalidation-rollout-plan-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class EvolutionRevalidationRolloutStageName(StrEnum):
    LOCAL_CANARY = "local_canary"
    OPT_IN = "opt_in"
    PERCENTAGE = "percentage"
    STABLE = "stable"


class EvolutionRevalidationRolloutExposure(StrEnum):
    SYNTHETIC = "synthetic"
    OPT_IN = "opt_in"
    LIMITED = "limited"
    STABLE = "stable"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRolloutStage(_StrictModel):
    order: int = Field(ge=1, le=4)
    name: EvolutionRevalidationRolloutStageName
    predecessor: EvolutionRevalidationRolloutStageName | None = None
    exposure: EvolutionRevalidationRolloutExposure
    exposure_percent: int = Field(ge=0, le=100)
    minimum_observation_seconds: int = Field(ge=300, le=604_800)
    minimum_completed_runs: int = Field(ge=10, le=100_000)
    max_error_rate_basis_points: int = Field(ge=0, le=10_000)
    max_p95_latency_regression_basis_points: int = Field(ge=0, le=10_000)
    max_completion_rate_drop_basis_points: int = Field(ge=0, le=10_000)
    max_cost_regression_basis_points: int = Field(ge=0, le=10_000)
    manual_advance_required: bool
    data_backup_required_before_entry: bool
    automatic_pause_on_breach: Literal[True] = True
    automatic_rollback_on_breach: Literal[True] = True
    entry_authority: Literal[False] = False
    execution_started: Literal[False] = False
    completed: Literal[False] = False


class EvolutionRevalidationRolloutPlan(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollout-plan-v1"] = (
        EVOLUTION_REVALIDATION_ROLLOUT_PLAN_POLICY
    )
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    decision_id: str = Field(pattern=r"^evreapprovaldecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=_SHA256_RE)
    decision_source_set_sha256: str = Field(pattern=_SHA256_RE)
    requirement_id: str = Field(pattern=r"^evreapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evrevalfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1, max_length=3
    )
    target_branch: str = Field(min_length=1, max_length=255)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    patch_manifest_sha256: str = Field(pattern=_SHA256_RE)
    migration_assessment_sha256: str = Field(pattern=_SHA256_RE)
    rollback_plan_sha256: str = Field(pattern=_SHA256_RE)
    data_backup_required: bool
    stages: tuple[EvolutionRevalidationRolloutStage, ...] = Field(
        min_length=4, max_length=4
    )
    current_decision_at_issue: Literal[True] = True
    immutable_plan: Literal[True] = True
    monitor_required: Literal[True] = True
    rollback_required: Literal[True] = True
    rollout_plan_authority: Literal[True] = True
    stage_entry_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    planned_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollout Plan workspace 必须 canonical。")
        if self.required_platforms != tuple(dict.fromkeys(self.required_platforms)):
            raise ValueError("Rollout Plan required platforms 不得重复。")
        if tuple(item.order for item in self.stages) != (1, 2, 3, 4):
            raise ValueError("Rollout stages 顺序不连续。")
        if self.stages != _stages(
            self.risk_level, data_backup_required=self.data_backup_required
        ):
            raise ValueError("Rollout stages 与风险/迁移策略不一致。")
        _aware(self.planned_at)
        core = self.model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
        digest = _digest(core)
        if self.plan_sha256 != digest or self.plan_id != f"evrerolloutplan_{digest[:24]}":
            raise ValueError("Rollout Plan identity 不一致。")
        return self


class EvolutionRevalidationRolloutPlanView(_StrictModel):
    plan: EvolutionRevalidationRolloutPlan
    decision_current: bool
    current_rollout_eligible: bool
    next_stage: EvolutionRevalidationRolloutStageName | None
    execution_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        if self.current_rollout_eligible is not self.decision_current:
            raise ValueError("Rollout Plan current eligibility 投影不一致。")
        expected = (
            EvolutionRevalidationRolloutStageName.LOCAL_CANARY
            if self.current_rollout_eligible
            else None
        )
        if self.next_stage is not expected:
            raise ValueError("Rollout Plan next stage 投影不一致。")
        return self


class EvolutionRevalidationRolloutPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRolloutPlanStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(self, plan_id: str) -> EvolutionRevalidationRolloutPlan | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                    "WHERE plan_id = ?",
                    (plan_id,),
                )
            ).fetchone()
        return None if row is None else _plan(row["plan_json"])

    async def get_by_decision(
        self, decision_id: str
    ) -> EvolutionRevalidationRolloutPlan | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                    "WHERE decision_id = ?",
                    (decision_id,),
                )
            ).fetchone()
        return None if row is None else _plan(row["plan_json"])

    async def record(
        self, plan: EvolutionRevalidationRolloutPlan
    ) -> EvolutionRevalidationRolloutPlan:
        item = EvolutionRevalidationRolloutPlan.model_validate_json(plan.model_dump_json())
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRolloutPlanError(
                "rollout_plan_oversized", "Rollout Plan 超过 512 KiB。"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            decision_row = await (
                await db.execute(
                    "SELECT decision_json FROM evolution_revalidation_approval_decisions "
                    "WHERE decision_id = ?",
                    (item.decision_id,),
                )
            ).fetchone()
            input_row = await (
                await db.execute(
                    "SELECT input_json FROM evolution_revalidation_promotion_inputs "
                    "WHERE input_id = ?",
                    (item.promotion_input_id,),
                )
            ).fetchone()
            if decision_row is None or input_row is None:
                await db.rollback()
                raise EvolutionRevalidationRolloutPlanError(
                    "rollout_plan_dependency_missing",
                    "Rollout Plan 缺少 Fresh Decision 或 Promotion Input。",
                )
            decision = EvolutionRevalidationApprovalDecisionReceipt.model_validate_json(
                decision_row["decision_json"]
            )
            promotion_input = EvolutionRevalidationPromotionInput.model_validate_json(
                input_row["input_json"]
            )
            if not _dependencies_match(item, decision, promotion_input):
                await db.rollback()
                raise EvolutionRevalidationRolloutPlanError(
                    "rollout_plan_dependency_mismatch",
                    "Rollout Plan 未绑定 exact approved Decision/Input。",
                )
            existing = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                    "WHERE decision_id = ?",
                    (item.decision_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _plan(existing["plan_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationRolloutPlanError(
                        "rollout_plan_conflict",
                        "同一 Fresh Decision 已绑定不同 Rollout Plan。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_rollout_plans "
                "(plan_id, plan_sha256, decision_id, contract_id, plan_json, planned_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.plan_id,
                    item.plan_sha256,
                    item.decision_id,
                    item.contract_id,
                    encoded,
                    item.planned_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationRolloutPlanService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        decision_service: EvolutionRevalidationApprovalDecisionService,
        promotion_input_service: EvolutionRevalidationPromotionInputService,
        store: EvolutionRevalidationRolloutPlanStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.decision_service = decision_service
        self.promotion_input_service = promotion_input_service
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue(
        self, *, contract_id: str, decision_id: str
    ) -> EvolutionRevalidationRolloutPlanView:
        lock = self._locks.setdefault(decision_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_decision(decision_id)
            if existing is not None:
                return await self.inspect(plan_id=existing.plan_id)
            decision = await self._decision(contract_id, decision_id)
            promotion_input = await self.promotion_input_service.issue(
                contract_id=contract_id
            )
            if not (
                decision.receipt.promotion_input_id == promotion_input.input_id
                and decision.receipt.promotion_input_sha256 == promotion_input.input_sha256
            ):
                raise EvolutionRevalidationRolloutPlanError(
                    "rollout_plan_input_mismatch",
                    "Fresh Decision 与 current Promotion Input 不一致。",
                )
            proposed = _build(decision.receipt, promotion_input)
            refreshed = await self._decision(contract_id, decision_id)
            if not refreshed.current_staged_rollout_eligible:
                raise EvolutionRevalidationRolloutPlanError(
                    "rollout_plan_decision_changed",
                    "Fresh Decision 在 Rollout Plan 持久化前失效。",
                )
            stored = await self.store.record(proposed)
            return EvolutionRevalidationRolloutPlanView(
                plan=stored,
                decision_current=True,
                current_rollout_eligible=True,
                next_stage=EvolutionRevalidationRolloutStageName.LOCAL_CANARY,
            )

    async def inspect(self, *, plan_id: str) -> EvolutionRevalidationRolloutPlanView:
        plan = await self.store.get(plan_id)
        if plan is None:
            raise EvolutionRevalidationRolloutPlanError(
                "rollout_plan_missing", "Rollout Plan 不存在。"
            )
        decision = await self.decision_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=plan.contract_id,
            decision_id=plan.decision_id,
        )
        current = bool(
            decision.current_staged_rollout_eligible
            and decision.receipt.decision_sha256 == plan.decision_sha256
            and decision.receipt.source_set_sha256 == plan.decision_source_set_sha256
        )
        return EvolutionRevalidationRolloutPlanView(
            plan=plan,
            decision_current=current,
            current_rollout_eligible=current,
            next_stage=(
                EvolutionRevalidationRolloutStageName.LOCAL_CANARY if current else None
            ),
        )

    async def _decision(self, contract_id, decision_id):
        view = await self.decision_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
            decision_id=decision_id,
        )
        if not (
            view.source_current
            and view.current_status is EvolutionRevalidationApprovalDecisionStatus.APPROVED
            and view.current_staged_rollout_eligible
        ):
            raise EvolutionRevalidationRolloutPlanError(
                "rollout_plan_decision_not_eligible",
                "只有动态 current 的 approved Fresh Decision 才能生成 Rollout Plan。",
            )
        return view


def _build(decision, promotion_input):
    prior = promotion_input.prior_input
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_PLAN_POLICY,
        "workspace_root": decision.workspace_root,
        "contract_id": decision.contract_id,
        "contract_sha256": promotion_input.contract_sha256,
        "decision_id": decision.decision_id,
        "decision_sha256": decision.decision_sha256,
        "decision_source_set_sha256": decision.source_set_sha256,
        "requirement_id": decision.requirement_id,
        "requirement_sha256": decision.requirement_sha256,
        "promotion_input_id": promotion_input.input_id,
        "promotion_input_sha256": promotion_input.input_sha256,
        "final_evaluation_id": promotion_input.final_evaluation_id,
        "final_evaluation_sha256": promotion_input.final_evaluation_sha256,
        "candidate_id": promotion_input.candidate_id,
        "candidate_revision": promotion_input.candidate_revision,
        "risk_level": promotion_input.risk_level,
        "required_platforms": list(promotion_input.required_platforms),
        "target_branch": decision.target_branch,
        "target_head": decision.target_head,
        "target_tree_sha256": decision.target_tree_sha256,
        "patch_manifest_sha256": prior.patch.manifest_sha256,
        "migration_assessment_sha256": prior.migration.assessment_sha256,
        "rollback_plan_sha256": prior.rollback.plan_sha256,
        "data_backup_required": prior.migration.data_backup_required,
        "stages": [
            item.model_dump(mode="json")
            for item in _stages(
                promotion_input.risk_level,
                data_backup_required=prior.migration.data_backup_required,
            )
        ],
        "current_decision_at_issue": True,
        "immutable_plan": True,
        "monitor_required": True,
        "rollback_required": True,
        "rollout_plan_authority": True,
        "stage_entry_authority": False,
        "execution_authority": False,
        "git_write_executed": False,
        "merge_executed": False,
        "push_executed": False,
        "publish_executed": False,
        "planned_at": decision.decided_at,
    }
    digest = _digest(core)
    return EvolutionRevalidationRolloutPlan.model_validate({
        **core,
        "plan_id": f"evrerolloutplan_{digest[:24]}",
        "plan_sha256": digest,
    })


def _dependencies_match(plan, decision, promotion_input):
    prior = promotion_input.prior_input
    return bool(
        decision.status is EvolutionRevalidationApprovalDecisionStatus.APPROVED
        and decision.staged_rollout_eligible
        and decision.decision_sha256 == plan.decision_sha256
        and decision.source_set_sha256 == plan.decision_source_set_sha256
        and decision.requirement_id == plan.requirement_id
        and decision.requirement_sha256 == plan.requirement_sha256
        and decision.workspace_root == plan.workspace_root
        and decision.promotion_input_id == plan.promotion_input_id
        and decision.promotion_input_sha256 == plan.promotion_input_sha256
        and decision.contract_id == plan.contract_id
        and decision.target_branch == plan.target_branch
        and decision.target_head == plan.target_head
        and decision.target_tree_sha256 == plan.target_tree_sha256
        and promotion_input.input_sha256 == plan.promotion_input_sha256
        and promotion_input.contract_id == plan.contract_id
        and promotion_input.contract_sha256 == plan.contract_sha256
        and promotion_input.final_evaluation_id == plan.final_evaluation_id
        and promotion_input.final_evaluation_sha256 == plan.final_evaluation_sha256
        and promotion_input.candidate_id == plan.candidate_id
        and promotion_input.candidate_revision == plan.candidate_revision
        and promotion_input.risk_level == plan.risk_level
        and promotion_input.required_platforms == plan.required_platforms
        and prior.patch.manifest_sha256 == plan.patch_manifest_sha256
        and prior.migration.assessment_sha256 == plan.migration_assessment_sha256
        and prior.rollback.plan_sha256 == plan.rollback_plan_sha256
        and prior.migration.data_backup_required is plan.data_backup_required
    )


def _stages(risk_level, *, data_backup_required):
    profiles = {
        "low": (25, 900, 20, 200, 2_000, 500, 2_000),
        "medium": (10, 1_800, 40, 100, 1_500, 300, 1_500),
        "high": (5, 3_600, 80, 50, 1_000, 200, 1_000),
        "critical": (1, 7_200, 160, 25, 500, 100, 500),
    }
    percentage, base_seconds, base_runs, error, latency, completion, cost = profiles[
        risk_level
    ]
    manual = risk_level in {"high", "critical"} or data_backup_required
    specifications = (
        ("local_canary", None, "synthetic", 0, 1, 1),
        ("opt_in", "local_canary", "opt_in", 1, 2, 2),
        ("percentage", "opt_in", "limited", percentage, 4, 5),
        ("stable", "percentage", "stable", 100, 8, 10),
    )
    return tuple(
        EvolutionRevalidationRolloutStage(
            order=order,
            name=name,
            predecessor=predecessor,
            exposure=exposure,
            exposure_percent=exposure_percent,
            minimum_observation_seconds=base_seconds * seconds_multiplier,
            minimum_completed_runs=base_runs * runs_multiplier,
            max_error_rate_basis_points=error,
            max_p95_latency_regression_basis_points=latency,
            max_completion_rate_drop_basis_points=completion,
            max_cost_regression_basis_points=cost,
            manual_advance_required=manual or name == "stable",
            data_backup_required_before_entry=data_backup_required and order == 1,
        )
        for order, (
            name,
            predecessor,
            exposure,
            exposure_percent,
            seconds_multiplier,
            runs_multiplier,
        ) in enumerate(specifications, start=1)
    )


def _aware(value):
    from datetime import datetime

    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Rollout Plan 时间必须包含 offset。")
    return parsed


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _plan(value):
    return EvolutionRevalidationRolloutPlan.model_validate_json(value)


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollout_plans ("
        "plan_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL UNIQUE, "
        "decision_id TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL, "
        "plan_json TEXT NOT NULL, planned_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLOUT_PLAN_POLICY",
    "EvolutionRevalidationRolloutExposure",
    "EvolutionRevalidationRolloutPlan",
    "EvolutionRevalidationRolloutPlanError",
    "EvolutionRevalidationRolloutPlanService",
    "EvolutionRevalidationRolloutPlanStore",
    "EvolutionRevalidationRolloutPlanView",
    "EvolutionRevalidationRolloutStage",
    "EvolutionRevalidationRolloutStageName",
]
