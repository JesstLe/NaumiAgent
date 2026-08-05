"""Fresh evaluation plan derived from a current validated Revalidation Outcome."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcome,
    EvolutionRevalidationOutcomeError,
    EvolutionRevalidationOutcomeService,
    EvolutionRevalidationOutcomeStore,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionRevalidationValidationStore,
)

EVOLUTION_REVALIDATION_EVALUATION_PLAN_POLICY = (
    "evolution-revalidation-evaluation-plan-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationEvaluationLaneKind(StrEnum):
    INTERVENTIONAL = "interventional"
    ADVERSARIAL = "adversarial"


class EvolutionRevalidationEvaluationLane(_StrictModel):
    order: int = Field(ge=1, le=4)
    kind: EvolutionRevalidationEvaluationLaneKind
    platform: Literal["", "linux", "macos", "windows"] = ""
    fresh_red_evidence_required: bool
    fresh_green_evidence_required: Literal[True] = True
    fresh_comparison_required: Literal[True] = True
    fresh_failure_attribution_required: Literal[True] = True
    fresh_lane_receipt_required: Literal[True] = True

    @model_validator(mode="after")
    def _lane_is_exact(self) -> Self:
        if self.kind is EvolutionRevalidationEvaluationLaneKind.INTERVENTIONAL:
            if self.platform or self.fresh_red_evidence_required:
                raise ValueError("Interventional lane 不使用 platform/red cohort。")
        elif not self.platform or not self.fresh_red_evidence_required:
            raise ValueError("Adversarial lane 必须重跑 platform RED/GREEN evidence。")
        return self


class EvolutionRevalidationEvaluationPlan(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-evaluation-plan-v1"] = (
        EVOLUTION_REVALIDATION_EVALUATION_PLAN_POLICY
    )
    plan_id: str = Field(pattern=r"^evrevalplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    outcome_id: str = Field(pattern=r"^evrevalout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    validation_receipt_id: str = Field(pattern=r"^evrevalidate_[0-9a-f]{24}$")
    validation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    overlay_source_sha256: str = Field(pattern=_SHA256_RE)
    old_final_evaluation_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    old_final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    lanes: tuple[EvolutionRevalidationEvaluationLane, ...] = Field(
        min_length=2,
        max_length=4,
    )
    fresh_evidence_after: str = Field(min_length=1, max_length=100)
    old_final_evaluation_invalidated: Literal[True] = True
    fresh_validation_plan_required: Literal[True] = True
    fresh_aggregation_contract_required: Literal[True] = True
    fresh_final_evaluation_required: Literal[True] = True
    approval_reaggregation_required: Literal[True] = True
    evaluation_started: Literal[False] = False
    final_evaluation_issued: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _plan_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Evaluation Plan workspace 必须是 canonical 路径。")
        if datetime.fromisoformat(self.fresh_evidence_after).utcoffset() is None:
            raise ValueError("Fresh evidence lower bound 必须包含 UTC offset。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Fresh Evaluation Plan created_at 必须包含 UTC offset。")
        if tuple(item.order for item in self.lanes) != tuple(
            range(1, len(self.lanes) + 1)
        ):
            raise ValueError("Fresh Evaluation lanes 顺序不连续。")
        if self.lanes[0].kind is not EvolutionRevalidationEvaluationLaneKind.INTERVENTIONAL:
            raise ValueError("Fresh Evaluation 第一条必须是 Interventional lane。")
        adversarial = self.lanes[1:]
        if tuple(item.platform for item in adversarial) != self.required_platforms:
            raise ValueError("Fresh Evaluation platform lanes 覆盖不完整。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
        )
        if not hmac.compare_digest(self.plan_sha256, digest):
            raise ValueError("Fresh Evaluation Plan 摘要不一致。")
        if self.plan_id != f"evrevalplan_{digest[:24]}":
            raise ValueError("Fresh Evaluation Plan identity 不一致。")
        return self


class EvolutionRevalidationEvaluationPlanView(_StrictModel):
    plan: EvolutionRevalidationEvaluationPlan
    source_current: bool
    current_status: Literal["ready", "stale"]
    execution_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        if self.current_status != ("ready" if self.source_current else "stale"):
            raise ValueError("Fresh Evaluation Plan current status 投影不一致。")
        if self.execution_eligible is not self.source_current:
            raise ValueError("Fresh Evaluation Plan execution eligibility 投影不一致。")
        return self


class EvolutionRevalidationEvaluationPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationEvaluationPlanStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self, item: EvolutionRevalidationEvaluationPlan
    ) -> EvolutionRevalidationEvaluationPlan:
        artifact = EvolutionRevalidationEvaluationPlan.model_validate_json(
            item.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency_tables = await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                    "('evolution_revalidation_outcomes', "
                    "'evolution_promotion_authority_invalidations')"
                )
            ).fetchall()
            if {row["name"] for row in dependency_tables} != {
                "evolution_revalidation_outcomes",
                "evolution_promotion_authority_invalidations",
            }:
                await db.rollback()
                raise EvolutionRevalidationEvaluationPlanError(
                    "revalidation_evaluation_authority_store_missing",
                    "Revalidation Outcome 或 invalidation authority store 不存在。",
                )
            outcome_row = await (
                await db.execute(
                    "SELECT outcome_sha256, outcome_json "
                    "FROM evolution_revalidation_outcomes WHERE outcome_id = ?",
                    (artifact.outcome_id,),
                )
            ).fetchone()
            if outcome_row is None:
                await db.rollback()
                raise EvolutionRevalidationEvaluationPlanError(
                    "revalidation_evaluation_outcome_missing",
                    "持久化 Revalidation Outcome 不存在。",
                )
            outcome = EvolutionRevalidationOutcome.model_validate_json(
                outcome_row["outcome_json"]
            )
            if not (
                outcome.outcome_sha256 == artifact.outcome_sha256
                and outcome_row["outcome_sha256"] == artifact.outcome_sha256
                and outcome.validation_receipt_id == artifact.validation_receipt_id
                and outcome.validation_receipt_sha256
                == artifact.validation_receipt_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationEvaluationPlanError(
                    "revalidation_evaluation_outcome_mismatch",
                    "Fresh Evaluation Plan 与持久化 Outcome 不一致。",
                )
            invalidation = await (
                await db.execute(
                    "SELECT authority_sha256 FROM "
                    "evolution_promotion_authority_invalidations "
                    "WHERE authority_kind = 'final_evaluation' AND authority_id = ? "
                    "AND outcome_id = ?",
                    (artifact.old_final_evaluation_id, artifact.outcome_id),
                )
            ).fetchone()
            if invalidation is None or invalidation["authority_sha256"] != (
                artifact.old_final_evaluation_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationEvaluationPlanError(
                    "revalidation_evaluation_invalidation_missing",
                    "持久化旧 Final Evaluation invalidation 不存在。",
                )
            row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_evaluation_plans "
                    "WHERE outcome_id = ?",
                    (artifact.outcome_id,),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionRevalidationEvaluationPlan.model_validate_json(
                    row["plan_json"]
                )
                await db.rollback()
                if restored != artifact:
                    raise EvolutionRevalidationEvaluationPlanError(
                        "revalidation_evaluation_plan_conflict",
                        "同一 Revalidation Outcome 已绑定不同 Fresh Evaluation Plan。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_evaluation_plans "
                "(plan_id, plan_sha256, outcome_id, plan_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    artifact.plan_id,
                    artifact.plan_sha256,
                    artifact.outcome_id,
                    artifact.model_dump_json(),
                    artifact.created_at,
                ),
            )
            await db.commit()
        return artifact

    async def get(self, plan_id: str) -> EvolutionRevalidationEvaluationPlan | None:
        if re.fullmatch(r"evrevalplan_[0-9a-f]{24}", str(plan_id)) is None:
            raise ValueError("Fresh Evaluation Plan ID 格式无效。")
        return await self._read("plan_id", plan_id)

    async def get_by_outcome(
        self, outcome_id: str
    ) -> EvolutionRevalidationEvaluationPlan | None:
        if re.fullmatch(r"evrevalout_[0-9a-f]{24}", str(outcome_id)) is None:
            raise ValueError("Revalidation Outcome ID 格式无效。")
        return await self._read("outcome_id", outcome_id)

    async def _read(
        self, field: str, value: str
    ) -> EvolutionRevalidationEvaluationPlan | None:
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_evaluation_plans "
                    f"WHERE {field} = ?",
                    (value,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationEvaluationPlan.model_validate_json(row["plan_json"])
        )


class EvolutionRevalidationEvaluationPlanService:
    def __init__(
        self,
        *,
        outcome_service: EvolutionRevalidationOutcomeService,
        outcome_store: EvolutionRevalidationOutcomeStore,
        validation_store: EvolutionRevalidationValidationStore,
        package_input_store: EvolutionPromotionPackageInputStore,
        store: EvolutionRevalidationEvaluationPlanStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._outcome_service = outcome_service
        self._outcome_store = outcome_store
        self._validation_store = validation_store
        self._package_input_store = package_input_store
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def issue(
        self, *, workspace_root: str | Path, outcome_id: str
    ) -> EvolutionRevalidationEvaluationPlanView:
        existing = await self._store.get_by_outcome(outcome_id)
        if existing is not None:
            return await self.inspect(workspace_root=workspace_root, plan_id=existing.plan_id)
        outcome_view, package, validation = await self._sources(
            workspace_root, outcome_id
        )
        if not outcome_view.rollout_candidate_eligible:
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_outcome_ineligible",
                "只有 current validated Revalidation Outcome 可以规划重新评估。",
            )
        outcome = outcome_view.outcome
        _require_old_final_invalidated(outcome, package)
        await self._require_invalidation_ledger(outcome, package)
        now = self._clock()
        if now.utcoffset() is None:
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_clock_invalid",
                "Fresh Evaluation Plan 时钟必须包含 UTC offset。",
            )
        input_ref = _authority(outcome, "promotion_input")
        lanes = (
            EvolutionRevalidationEvaluationLane(
                order=1,
                kind=EvolutionRevalidationEvaluationLaneKind.INTERVENTIONAL,
                fresh_red_evidence_required=False,
            ),
            *(
                EvolutionRevalidationEvaluationLane(
                    order=index + 2,
                    kind=EvolutionRevalidationEvaluationLaneKind.ADVERSARIAL,
                    platform=platform,
                    fresh_red_evidence_required=True,
                )
                for index, platform in enumerate(package.required_platforms)
            ),
        )
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_EVALUATION_PLAN_POLICY,
            "workspace_root": str(Path(workspace_root).expanduser().resolve(strict=True)),
            "outcome_id": outcome.outcome_id,
            "outcome_sha256": outcome.outcome_sha256,
            "validation_receipt_id": outcome.validation_receipt_id,
            "validation_receipt_sha256": outcome.validation_receipt_sha256,
            "promotion_input_id": input_ref.authority_id,
            "promotion_input_sha256": input_ref.authority_sha256,
            "candidate_id": package.candidate_id,
            "candidate_revision": package.candidate_revision,
            "target_head": outcome.target_head,
            "target_tree_sha256": outcome.target_tree_sha256,
            "overlay_source_sha256": validation.overlay_source_sha256,
            "old_final_evaluation_id": package.final_evaluation_receipt_id,
            "old_final_evaluation_sha256": package.final_evaluation_receipt_sha256,
            "required_platforms": list(package.required_platforms),
            "lanes": [item.model_dump(mode="json") for item in lanes],
            "fresh_evidence_after": outcome.created_at,
            "old_final_evaluation_invalidated": True,
            "fresh_validation_plan_required": True,
            "fresh_aggregation_contract_required": True,
            "fresh_final_evaluation_required": True,
            "approval_reaggregation_required": True,
            "evaluation_started": False,
            "final_evaluation_issued": False,
            "promotion_authority": False,
            "created_at": now.isoformat(),
        }
        digest = _sha256_payload(payload)
        plan = EvolutionRevalidationEvaluationPlan.model_validate(
            {**payload, "plan_id": f"evrevalplan_{digest[:24]}", "plan_sha256": digest}
        )
        await self._store.record(plan)
        return EvolutionRevalidationEvaluationPlanView(
            plan=plan,
            source_current=True,
            current_status="ready",
            execution_eligible=True,
        )

    async def inspect(
        self, *, workspace_root: str | Path, plan_id: str
    ) -> EvolutionRevalidationEvaluationPlanView:
        plan = await self._store.get(plan_id)
        if plan is None:
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_plan_not_found",
                "Fresh Evaluation Plan 不存在。",
            )
        try:
            outcome_view, package, validation = await self._sources(
                workspace_root, plan.outcome_id
            )
            input_ref = _authority(outcome_view.outcome, "promotion_input")
            source_current = bool(
                outcome_view.rollout_candidate_eligible
                and outcome_view.outcome.outcome_sha256 == plan.outcome_sha256
                and input_ref.authority_id == plan.promotion_input_id
                and input_ref.authority_sha256 == plan.promotion_input_sha256
                and package.final_evaluation_receipt_id
                == plan.old_final_evaluation_id
                and package.final_evaluation_receipt_sha256
                == plan.old_final_evaluation_sha256
                and validation.overlay_source_sha256 == plan.overlay_source_sha256
            )
            if source_current:
                await self._require_invalidation_ledger(outcome_view.outcome, package)
        except (
            OSError,
            TypeError,
            ValueError,
            EvolutionRevalidationOutcomeError,
            EvolutionRevalidationEvaluationPlanError,
        ):
            source_current = False
        return EvolutionRevalidationEvaluationPlanView(
            plan=plan,
            source_current=source_current,
            current_status="ready" if source_current else "stale",
            execution_eligible=source_current,
        )

    async def _sources(self, workspace_root: str | Path, outcome_id: str):
        outcome_view = await self._outcome_service.inspect(
            workspace_root=workspace_root,
            outcome_id=outcome_id,
        )
        validation = await self._validation_store.get_by_request(
            outcome_view.outcome.request_id
        )
        if validation is None or not (
            validation.receipt_id == outcome_view.outcome.validation_receipt_id
            and validation.receipt_sha256
            == outcome_view.outcome.validation_receipt_sha256
        ):
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_validation_mismatch",
                "当前 Harness Validation Receipt 与 Revalidation Outcome 不一致。",
            )
        input_ref = _authority(outcome_view.outcome, "promotion_input")
        package_view = await self._package_input_store.get(input_ref.authority_id)
        if package_view is None:
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_input_missing",
                "旧 Promotion Input 不存在，无法重建评估覆盖。",
            )
        package = package_view.package_input
        if package.input_sha256 != input_ref.authority_sha256:
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_input_mismatch",
                "旧 Promotion Input digest 与 Outcome 失效账本不一致。",
            )
        return outcome_view, package, validation

    async def _require_invalidation_ledger(self, outcome, package) -> None:
        ledger = await self._outcome_store.invalidation(
            kind="final_evaluation",
            authority_id=package.final_evaluation_receipt_id,
        )
        if ledger is None or ledger.authority_sha256 != (
            package.final_evaluation_receipt_sha256
        ):
            raise EvolutionRevalidationEvaluationPlanError(
                "revalidation_evaluation_invalidation_missing",
                "旧 Final Evaluation 缺少 durable authority invalidation。",
            )


def _authority(outcome: EvolutionRevalidationOutcome, kind: str):
    matches = tuple(item for item in outcome.invalidated_authorities if item.kind == kind)
    if len(matches) != 1:
        raise EvolutionRevalidationEvaluationPlanError(
            "revalidation_evaluation_authority_missing",
            f"Revalidation Outcome 缺少唯一 {kind} authority。",
        )
    return matches[0]


def _require_old_final_invalidated(
    outcome: EvolutionRevalidationOutcome,
    package: EvolutionPromotionPackageInput,
) -> None:
    final = _authority(outcome, "final_evaluation")
    if not (
        final.authority_id == package.final_evaluation_receipt_id
        and final.authority_sha256 == package.final_evaluation_receipt_sha256
    ):
        raise EvolutionRevalidationEvaluationPlanError(
            "revalidation_evaluation_final_authority_mismatch",
            "旧 Final Evaluation 未被当前 Outcome 精确失效。",
        )


def render_evolution_revalidation_evaluation_plan(
    view: EvolutionRevalidationEvaluationPlanView,
) -> str:
    item = view.plan
    return "\n".join(
        [
            f"# Evolution Fresh Evaluation Plan `{item.plan_id}`",
            "",
            f"- Current status：`{view.current_status}`",
            f"- Revalidation Outcome：`{item.outcome_id}`",
            f"- Old Final Evaluation：`{item.old_final_evaluation_id}` · invalidated",
            f"- Required lanes：{len(item.lanes)}",
            f"- Required platforms：`{', '.join(item.required_platforms)}`",
            f"- Fresh evidence after：`{item.fresh_evidence_after}`",
            "- Fresh plan / aggregation / final receipt：`required`",
            "- Approval reaggregation：`required`",
            "- Promotion authority：`false`",
        ]
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_evaluation_plans ("
        "plan_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
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
    "EVOLUTION_REVALIDATION_EVALUATION_PLAN_POLICY",
    "EvolutionRevalidationEvaluationLane",
    "EvolutionRevalidationEvaluationLaneKind",
    "EvolutionRevalidationEvaluationPlan",
    "EvolutionRevalidationEvaluationPlanError",
    "EvolutionRevalidationEvaluationPlanService",
    "EvolutionRevalidationEvaluationPlanStore",
    "EvolutionRevalidationEvaluationPlanView",
    "render_evolution_revalidation_evaluation_plan",
]
