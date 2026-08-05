"""Validation authority rebinding RED to current target and GREEN to immutable source."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiments import (
    EvolutionExperimentContract,
    ExperimentBudget,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionEvidenceKind,
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.revalidation_evaluation_plans import (
    EvolutionRevalidationEvaluationPlan,
    EvolutionRevalidationEvaluationPlanService,
)
from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceSnapshot,
    EvolutionRevalidationEvaluationSourceStore,
)
from naumi_agent.evolution.validation_plans import (
    ValidationCheckKind,
    ValidationMetricPair,
    validation_requirements_for_path,
)
from naumi_agent.harness.checks import select_required_check_ids
from naumi_agent.harness.evolution_revalidation import (
    HarnessEvolutionRevalidationPlan,
)
from naumi_agent.harness.models import HarnessCheckSpec

EVOLUTION_REVALIDATION_VALIDATION_PLAN_POLICY = (
    "evolution-revalidation-validation-plan-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_TARGET_BLOB_BYTES = 2 * 1_024 * 1_024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationValidationFile(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    red_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    green_sha256: str = Field(pattern=_SHA256_RE)
    executable: bool
    required_checks: tuple[ValidationCheckKind, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _file_is_exact(self) -> Self:
        if (self.operation == "create") is (self.red_sha256 is not None):
            raise ValueError("Revalidation Validation file operation/digest 不一致。")
        if self.required_checks != tuple(dict.fromkeys(self.required_checks)):
            raise ValueError("Revalidation Validation file checks 不得重复。")
        return self


class EvolutionRevalidationCheckCoverage(_StrictModel):
    order: int = Field(ge=1, le=80)
    path: str = Field(min_length=1, max_length=1_024)
    check_kind: ValidationCheckKind
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class EvolutionRevalidationValidationPlan(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-validation-plan-v1"] = (
        EVOLUTION_REVALIDATION_VALIDATION_PLAN_POLICY
    )
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    fresh_evaluation_plan_id: str = Field(pattern=r"^evrevalplan_[0-9a-f]{24}$")
    fresh_evaluation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    outcome_id: str = Field(pattern=r"^evrevalout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    old_validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    old_validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    old_final_evaluation_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    old_final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    requested_samples: int = Field(ge=5, le=100)
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    budget: ExperimentBudget
    metrics: tuple[ValidationMetricPair, ...] = Field(min_length=1, max_length=8)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    red_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    red_tree_sha256: str = Field(pattern=_SHA256_RE)
    green_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    green_tree_sha256: str = Field(pattern=_SHA256_RE)
    green_overlay_source_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    harness_plan_sha256: str = Field(pattern=_SHA256_RE)
    files: tuple[EvolutionRevalidationValidationFile, ...] = Field(
        min_length=1,
        max_length=16,
    )
    required_check_kinds: tuple[ValidationCheckKind, ...] = Field(
        min_length=1,
        max_length=5,
    )
    checks: tuple[HarnessCheckSpec, ...] = Field(min_length=1, max_length=80)
    coverage: tuple[EvolutionRevalidationCheckCoverage, ...] = Field(
        min_length=1,
        max_length=80,
    )
    fresh_evidence_after: str = Field(min_length=1, max_length=100)
    baseline_first: Literal[True] = True
    identical_environment_required: Literal[True] = True
    immutable_green_source_required: Literal[True] = True
    evaluation_execution_started: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _plan_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Revalidation Validation Plan workspace 必须 canonical。")
        if self.red_revision != self.green_revision or (
            self.red_tree_sha256 != self.green_tree_sha256
        ):
            raise ValueError("RED/GREEN 必须共享 current-target baseline。")
        if tuple(item.order for item in self.files) != tuple(
            range(1, len(self.files) + 1)
        ) or tuple(item.path for item in self.files) != tuple(
            sorted(item.path for item in self.files)
        ):
            raise ValueError("Revalidation Validation files 顺序无效。")
        derived_kinds = tuple(
            sorted({kind for item in self.files for kind in item.required_checks})
        )
        if self.required_check_kinds != derived_kinds:
            raise ValueError("Revalidation required check kinds 投影不一致。")
        if tuple(item.order for item in self.metrics) != tuple(
            range(1, len(self.metrics) + 1)
        ):
            raise ValueError("Revalidation metrics 顺序不连续。")
        if tuple(item.order for item in self.coverage) != tuple(
            range(1, len(self.coverage) + 1)
        ):
            raise ValueError("Revalidation check coverage 顺序不连续。")
        expected_coverage = {
            (item.path, kind) for item in self.files for kind in item.required_checks
        }
        if {(item.path, item.check_kind) for item in self.coverage} != expected_coverage:
            raise ValueError("Revalidation check coverage 不完整。")
        by_check = {item.id: item for item in self.checks}
        if len(by_check) != len(self.checks) or any(
            item.check_id not in by_check
            or item.check_kind not in by_check[item.check_id].provides
            for item in self.coverage
        ):
            raise ValueError("Revalidation check coverage 与 checks 不一致。")
        fresh_after = datetime.fromisoformat(self.fresh_evidence_after)
        created_at = datetime.fromisoformat(self.created_at)
        if fresh_after.utcoffset() is None or created_at.utcoffset() is None:
            raise ValueError("Revalidation Validation Plan 时间必须包含 offset。")
        if created_at < fresh_after:
            raise ValueError("Revalidation Validation Plan 早于 fresh evidence 下界。")
        digest = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"validation_plan_id", "validation_plan_sha256"},
            )
        )
        if not hmac.compare_digest(self.validation_plan_sha256, digest):
            raise ValueError("Revalidation Validation Plan 摘要不一致。")
        if self.validation_plan_id != f"evrevalvplan_{digest[:24]}":
            raise ValueError("Revalidation Validation Plan identity 不一致。")
        return self


class EvolutionRevalidationValidationPlanView(_StrictModel):
    plan: EvolutionRevalidationValidationPlan
    source_current: bool
    current_status: Literal["ready", "stale"]
    execution_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        if self.current_status != ("ready" if self.source_current else "stale"):
            raise ValueError("Revalidation Validation Plan status 投影不一致。")
        if self.execution_eligible is not self.source_current:
            raise ValueError("Revalidation Validation Plan eligibility 投影不一致。")
        return self


class EvolutionRevalidationValidationPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionExperimentAuthorityReader(Protocol):
    async def get(self, workspace_root: str | Path, contract_id: str): ...


class EvolutionFinalEvaluationReader(Protocol):
    async def get_by_receipt_id(self, receipt_id: str): ...


class EvolutionRevalidationHarnessPlanner(Protocol):
    async def prepare_evolution_revalidation(
        self, *, changed_paths: tuple[str, ...]
    ) -> HarnessEvolutionRevalidationPlan: ...


class EvolutionRevalidationValidationPlanBuilder:
    def build(
        self,
        *,
        workspace_root: str | Path,
        fresh_plan: EvolutionRevalidationEvaluationPlan,
        source: EvolutionRevalidationEvaluationSourceSnapshot,
        package: EvolutionPromotionPackageInput,
        contract: EvolutionExperimentContract,
        suite_id: str,
        requested_samples: int,
        harness_plan: HarnessEvolutionRevalidationPlan,
        target_files: dict[str, tuple[bytes | None, bool]],
        now: datetime,
    ) -> EvolutionRevalidationValidationPlan:
        _require_sources(fresh_plan, source, package, contract, harness_plan)
        canonical_workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if canonical_workspace != fresh_plan.workspace_root:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_workspace_mismatch",
                "调用工作区与 Fresh Evaluation authority 不一致。",
            )
        expected_paths = tuple(item.path for item in package.patch.files)
        if tuple(sorted(target_files)) != expected_paths:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_target_scope_mismatch",
                "Current target blobs 未精确覆盖 Promotion patch scope。",
            )
        if now.utcoffset() is None:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_clock_invalid",
                "Revalidation Validation Plan 时钟必须包含 offset。",
            )
        overlays = {item.path: item for item in source.blobs}
        files = []
        for order, patch in enumerate(sorted(package.patch.files, key=lambda x: x.path), 1):
            baseline, baseline_executable = target_files[patch.path]
            green = overlays.get(patch.path)
            if green is None:
                raise EvolutionRevalidationValidationPlanError(
                    "revalidation_validation_plan_green_file_missing",
                    f"Immutable GREEN source 缺少文件：{patch.path}",
                )
            operation = "create" if baseline is None else "modify"
            if operation != patch.operation:
                raise EvolutionRevalidationValidationPlanError(
                    "revalidation_validation_plan_target_operation_drift",
                    f"Current target 已改变 {patch.path} 的 {patch.operation} 语义。",
                )
            requirement = validation_requirements_for_path(
                patch.path,
                operation=operation,
                baseline_sha256=(
                    None if baseline is None else hashlib.sha256(baseline).hexdigest()
                ),
                candidate_sha256=green.sha256,
            )
            if baseline is not None and baseline_executable != green.executable:
                raise EvolutionRevalidationValidationPlanError(
                    "revalidation_validation_plan_mode_change",
                    f"Revalidation evaluation 不允许 executable mode 漂移：{patch.path}",
                )
            files.append(
                EvolutionRevalidationValidationFile(
                    order=order,
                    path=patch.path,
                    operation=operation,
                    red_sha256=requirement.baseline_sha256,
                    green_sha256=requirement.candidate_sha256,
                    executable=green.executable,
                    required_checks=requirement.required_checks,
                )
            )
        checks, coverage = _coverage(tuple(files), harness_plan)
        old_plan = _evidence(package, EvolutionPromotionEvidenceKind.VALIDATION_PLAN)
        metrics = tuple(
            ValidationMetricPair(
                order=index,
                metric_name=item.metric_name,
                direction=item.direction,
                target=item.target,
                verifier=item.verifier,
                procedure=item.procedure,
            )
            for index, item in enumerate(contract.allowed_checks, 1)
        )
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_VALIDATION_PLAN_POLICY,
            "workspace_root": canonical_workspace,
            "fresh_evaluation_plan_id": fresh_plan.plan_id,
            "fresh_evaluation_plan_sha256": fresh_plan.plan_sha256,
            "source_snapshot_id": source.snapshot_id,
            "source_snapshot_sha256": source.snapshot_sha256,
            "outcome_id": fresh_plan.outcome_id,
            "outcome_sha256": fresh_plan.outcome_sha256,
            "experiment_contract_id": contract.contract_id,
            "experiment_contract_sha256": contract.manifest_sha256,
            "promotion_input_id": package.input_id,
            "promotion_input_sha256": package.input_sha256,
            "old_validation_plan_id": old_plan.authority_id,
            "old_validation_plan_sha256": old_plan.authority_sha256,
            "old_final_evaluation_id": package.final_evaluation_receipt_id,
            "old_final_evaluation_sha256": package.final_evaluation_receipt_sha256,
            "candidate_id": package.candidate_id,
            "candidate_revision": package.candidate_revision,
            "suite_id": suite_id,
            "requested_samples": requested_samples,
            "seed": contract.seed,
            "budget": contract.budget.model_dump(mode="json"),
            "metrics": [item.model_dump(mode="json") for item in metrics],
            "required_platforms": list(fresh_plan.required_platforms),
            "red_revision": source.target_head,
            "red_tree_sha256": source.target_tree_sha256,
            "green_revision": source.target_head,
            "green_tree_sha256": source.target_tree_sha256,
            "green_overlay_source_sha256": source.overlay_source_sha256,
            "profile_sha256": harness_plan.profile_sha256,
            "harness_plan_sha256": harness_plan.plan_sha256,
            "files": [item.model_dump(mode="json") for item in files],
            "required_check_kinds": sorted(
                {kind for item in files for kind in item.required_checks}
            ),
            "checks": [item.model_dump(mode="json") for item in checks],
            "coverage": [item.model_dump(mode="json") for item in coverage],
            "fresh_evidence_after": fresh_plan.fresh_evidence_after,
            "baseline_first": True,
            "identical_environment_required": True,
            "immutable_green_source_required": True,
            "evaluation_execution_started": False,
            "promotion_authority": False,
            "created_at": now.isoformat(),
        }
        digest = _sha256_payload(payload)
        return EvolutionRevalidationValidationPlan.model_validate(
            {
                **payload,
                "validation_plan_id": f"evrevalvplan_{digest[:24]}",
                "validation_plan_sha256": digest,
            }
        )


class EvolutionRevalidationValidationPlanStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self, item: EvolutionRevalidationValidationPlan
    ) -> EvolutionRevalidationValidationPlan:
        artifact = EvolutionRevalidationValidationPlan.model_validate_json(
            item.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            source = await (
                await db.execute(
                    "SELECT snapshot_sha256 FROM evolution_revalidation_evaluation_sources "
                    "WHERE snapshot_id = ?",
                    (artifact.source_snapshot_id,),
                )
            ).fetchone()
            if source is None or source["snapshot_sha256"] != artifact.source_snapshot_sha256:
                await db.rollback()
                raise EvolutionRevalidationValidationPlanError(
                    "revalidation_validation_plan_source_mismatch",
                    "Immutable Evaluation Source 未持久化或 digest 不一致。",
                )
            row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_validation_plans "
                    "WHERE source_snapshot_id = ?",
                    (artifact.source_snapshot_id,),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionRevalidationValidationPlan.model_validate_json(
                    row["plan_json"]
                )
                await db.rollback()
                if restored != artifact:
                    raise EvolutionRevalidationValidationPlanError(
                        "revalidation_validation_plan_conflict",
                        "同一 immutable source 已绑定不同 Validation Plan。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_validation_plans "
                "(validation_plan_id, validation_plan_sha256, source_snapshot_id, "
                "plan_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    artifact.validation_plan_id,
                    artifact.validation_plan_sha256,
                    artifact.source_snapshot_id,
                    artifact.model_dump_json(),
                    artifact.created_at,
                ),
            )
            await db.commit()
        return artifact

    async def get(self, plan_id: str) -> EvolutionRevalidationValidationPlan | None:
        if re.fullmatch(r"evrevalvplan_[0-9a-f]{24}", str(plan_id)) is None:
            raise ValueError("Revalidation Validation Plan ID 格式无效。")
        return await self._read("validation_plan_id", plan_id)

    async def get_by_source(
        self, snapshot_id: str
    ) -> EvolutionRevalidationValidationPlan | None:
        return await self._read("source_snapshot_id", snapshot_id)

    async def _read(self, field: str, value: str):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_validation_plans "
                    f"WHERE {field} = ?",
                    (value,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationValidationPlan.model_validate_json(row["plan_json"])
        )


class EvolutionRevalidationValidationPlanService:
    def __init__(
        self,
        *,
        fresh_plan_service: EvolutionRevalidationEvaluationPlanService,
        source_store: EvolutionRevalidationEvaluationSourceStore,
        package_store: EvolutionPromotionPackageInputStore,
        experiment_store: EvolutionExperimentAuthorityReader,
        final_evaluation_store: EvolutionFinalEvaluationReader,
        harness: EvolutionRevalidationHarnessPlanner,
        store: EvolutionRevalidationValidationPlanStore,
        builder: EvolutionRevalidationValidationPlanBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fresh_plan_service = fresh_plan_service
        self._source_store = source_store
        self._package_store = package_store
        self._experiment_store = experiment_store
        self._final_store = final_evaluation_store
        self._harness = harness
        self._store = store
        self._builder = builder or EvolutionRevalidationValidationPlanBuilder()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def issue(
        self, *, workspace_root: str | Path, source_snapshot_id: str
    ) -> EvolutionRevalidationValidationPlanView:
        existing = await self._store.get_by_source(source_snapshot_id)
        if existing is not None:
            return await self.inspect(
                workspace_root=workspace_root,
                validation_plan_id=existing.validation_plan_id,
            )
        plan = await self._build_current(workspace_root, source_snapshot_id)
        stored = await self._store.record(plan)
        return EvolutionRevalidationValidationPlanView(
            plan=stored,
            source_current=True,
            current_status="ready",
            execution_eligible=True,
        )

    async def inspect(
        self, *, workspace_root: str | Path, validation_plan_id: str
    ) -> EvolutionRevalidationValidationPlanView:
        stored = await self._store.get(validation_plan_id)
        if stored is None:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_not_found",
                "Revalidation Validation Plan 不存在。",
            )
        try:
            current = await self._build_current(
                workspace_root,
                stored.source_snapshot_id,
                now=datetime.fromisoformat(stored.created_at),
            )
            source_current = current == stored
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, RuntimeError):
            source_current = False
        return EvolutionRevalidationValidationPlanView(
            plan=stored,
            source_current=source_current,
            current_status="ready" if source_current else "stale",
            execution_eligible=source_current,
        )

    async def _build_current(self, workspace_root, source_snapshot_id, *, now=None):
        source = await self._source_store.get(source_snapshot_id)
        if source is None:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_source_missing",
                "Immutable Evaluation Source 不存在。",
            )
        await self._source_store.verify_blobs(source)
        fresh_view = await self._fresh_plan_service.inspect(
            workspace_root=workspace_root,
            plan_id=source.plan_id,
        )
        if not fresh_view.execution_eligible:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_fresh_plan_stale",
                "Fresh Evaluation Plan 已 stale。",
            )
        fresh = fresh_view.plan
        package_view = await self._package_store.get(fresh.promotion_input_id)
        if package_view is None:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_input_missing",
                "Promotion Input 不存在。",
            )
        package = package_view.package_input
        authority = await self._experiment_store.get(
            workspace_root, package.experiment_contract_id
        )
        if authority is None:
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_contract_missing",
                "Experiment Contract authority 不存在。",
            )
        contract = EvolutionExperimentContract.model_validate(
            authority.contract.model_dump(mode="json")
        )
        final = await self._final_store.get_by_receipt_id(
            package.final_evaluation_receipt_id
        )
        if final is None or not (
            final.receipt_id == package.final_evaluation_receipt_id
            and final.receipt_sha256 == package.final_evaluation_receipt_sha256
        ):
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_final_missing",
                "旧 Final Evaluation authority 不存在或 digest 不一致。",
            )
        old_plan = _evidence(package, EvolutionPromotionEvidenceKind.VALIDATION_PLAN)
        if not (
            final.workspace_root
            == str(Path(workspace_root).expanduser().resolve(strict=True))
            and final.candidate_id == package.candidate_id
            and final.candidate_revision == package.candidate_revision
            and tuple(final.required_platforms) == tuple(fresh.required_platforms)
            and final.validation_plan_id == old_plan.authority_id
            and final.validation_plan_sha256 == old_plan.authority_sha256
        ):
            raise EvolutionRevalidationValidationPlanError(
                "revalidation_validation_plan_final_mismatch",
                "旧 Final Evaluation 的 Candidate 或平台集合与当前 authority 不一致。",
            )
        aggregation = final.aggregation_contract
        harness_plan = await self._harness.prepare_evolution_revalidation(
            changed_paths=tuple(item.path for item in package.patch.files)
        )
        target_files = {
            item.path: _target_file(Path(workspace_root), source.target_head, item.path)
            for item in package.patch.files
        }
        return self._builder.build(
            workspace_root=workspace_root,
            fresh_plan=fresh,
            source=source,
            package=package,
            contract=contract,
            suite_id=aggregation.suite_id,
            requested_samples=aggregation.requested_samples,
            harness_plan=harness_plan,
            target_files=target_files,
            now=now or self._clock(),
        )


def _require_sources(fresh, source, package, contract, harness_plan) -> None:
    workspace = str(Path(fresh.workspace_root).expanduser().resolve())
    if not (
        package.workspace_root == workspace
        and source.plan_id == fresh.plan_id
        and source.plan_sha256 == fresh.plan_sha256
        and source.outcome_id == fresh.outcome_id
        and source.outcome_sha256 == fresh.outcome_sha256
        and source.target_head == fresh.target_head
        and source.target_tree_sha256 == fresh.target_tree_sha256
        and source.overlay_source_sha256 == fresh.overlay_source_sha256
        and package.input_id == fresh.promotion_input_id
        and package.input_sha256 == fresh.promotion_input_sha256
        and contract.contract_id == package.experiment_contract_id
        and contract.manifest_sha256 == package.experiment_contract_sha256
        and contract.source.candidate_id == package.candidate_id
        and contract.source.candidate_revision == package.candidate_revision
        and tuple(contract.scope.allowed_files)
        == tuple(item.path for item in package.patch.files)
        and tuple(item.path for item in source.blobs)
        == tuple(item.path for item in package.patch.files)
        and all(
            blob.sha256 == patch.after_sha256
            for blob, patch in zip(source.blobs, package.patch.files, strict=True)
        )
        and harness_plan.profile_sha256 == source.harness_profile_sha256
        and harness_plan.plan_sha256 == source.harness_plan_sha256
    ):
        raise EvolutionRevalidationValidationPlanError(
            "revalidation_validation_plan_authority_mismatch",
            "Fresh Plan、Source、Promotion Input、Contract 或 Harness authority 不一致。",
        )


def _coverage(files, harness_plan):
    used = set()
    coverage = []
    for file in files:
        selected_ids = set(
            select_required_check_ids(
                harness_plan.checks,
                task_kind="change",
                changed_paths=(file.path,),
            )
        )
        for kind in file.required_checks:
            matches = tuple(
                check
                for check in harness_plan.checks
                if check.id in selected_ids and kind in check.provides
            )
            if len(matches) != 1:
                raise EvolutionRevalidationValidationPlanError(
                    "revalidation_validation_plan_check_coverage_invalid",
                    f"{file.path} 的 {kind} 必须由唯一 current Profile check 覆盖。",
                )
            used.add(matches[0].id)
            coverage.append((file.path, kind, matches[0].id))
    checks = tuple(item for item in harness_plan.checks if item.id in used)
    return checks, tuple(
        EvolutionRevalidationCheckCoverage(
            order=index,
            path=path,
            check_kind=kind,
            check_id=check_id,
        )
        for index, (path, kind, check_id) in enumerate(sorted(coverage), 1)
    )


def _evidence(package, kind):
    matches = tuple(item for item in package.evidence_refs if item.kind is kind)
    if len(matches) != 1:
        raise EvolutionRevalidationValidationPlanError(
            "revalidation_validation_plan_evidence_missing",
            f"Promotion Input 缺少唯一 {kind.value} evidence。",
        )
    return matches[0]


def _target_file(workspace: Path, revision: str, path: str) -> tuple[bytes | None, bool]:
    spec = f"{revision}:{path}"
    kind = _git(workspace, "cat-file", "-t", spec, allow_missing=True)
    if kind is None:
        return None, False
    if kind.strip() != b"blob":
        raise EvolutionRevalidationValidationPlanError(
            "revalidation_validation_plan_target_type_unsafe",
            f"Current target path 不是 blob：{path}",
        )
    content = _git(workspace, "cat-file", "-p", spec)
    assert content is not None
    if len(content) > _MAX_TARGET_BLOB_BYTES:
        raise EvolutionRevalidationValidationPlanError(
            "revalidation_validation_plan_target_oversized",
            f"Current target file 超过 2 MiB：{path}",
        )
    listing = _git(workspace, "ls-tree", revision, "--", path)
    assert listing is not None
    mode = listing.split(None, 1)[0]
    if mode not in {b"100644", b"100755"}:
        raise EvolutionRevalidationValidationPlanError(
            "revalidation_validation_plan_target_mode_unsafe",
            f"Current target mode 不安全：{path}",
        )
    return content, mode == b"100755"


def _git(workspace: Path, *args: str, allow_missing: bool = False) -> bytes | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=False,
        capture_output=True,
        timeout=20,
    )
    if completed.returncode != 0:
        if allow_missing and completed.returncode in {1, 128}:
            return None
        raise EvolutionRevalidationValidationPlanError(
            "revalidation_validation_plan_git_failed",
            "无法读取 current target Git authority。",
        )
    return completed.stdout


def render_evolution_revalidation_validation_plan(
    view: EvolutionRevalidationValidationPlanView,
) -> str:
    item = view.plan
    return "\n".join(
        [
            f"# Evolution Revalidation Validation Plan `{item.validation_plan_id}`",
            "",
            f"- Current status：`{view.current_status}`",
            f"- RED baseline：`{item.red_revision}`",
            f"- GREEN overlay：`{item.green_overlay_source_sha256}`",
            f"- Metrics / checks：{len(item.metrics)} / {len(item.checks)}",
            f"- Samples / platforms：{item.requested_samples} / {len(item.required_platforms)}",
            "- Same environment / baseline first：`true` / `true`",
            "- Execution / Promotion authority：`false` / `false`",
        ]
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_validation_plans ("
        "validation_plan_id TEXT PRIMARY KEY, validation_plan_sha256 TEXT NOT NULL UNIQUE, "
        "source_snapshot_id TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
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
    "EVOLUTION_REVALIDATION_VALIDATION_PLAN_POLICY",
    "EvolutionRevalidationCheckCoverage",
    "EvolutionRevalidationValidationFile",
    "EvolutionRevalidationValidationPlan",
    "EvolutionRevalidationValidationPlanBuilder",
    "EvolutionRevalidationValidationPlanError",
    "EvolutionRevalidationValidationPlanService",
    "EvolutionRevalidationValidationPlanStore",
    "EvolutionRevalidationValidationPlanView",
    "render_evolution_revalidation_validation_plan",
]
