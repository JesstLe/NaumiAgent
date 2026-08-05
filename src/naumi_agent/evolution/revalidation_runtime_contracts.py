"""Fresh metric-runner and adversarial-probe authority after revalidation."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.adversarial_probe_contracts import (
    AdversarialProbeBlocker,
    AdversarialProbeCheckBinding,
    AdversarialProbeCoverage,
    AdversarialProbeRequirement,
    EvolutionAdversarialProbeRegistry,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlan,
    EvolutionRevalidationValidationPlanService,
)
from naumi_agent.evolution.validation_cohorts import BaselineCohortMetricCase
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerRegistry,
    MetricRunnerBindingEntry,
)
from naumi_agent.harness.checks import select_required_check_ids
from naumi_agent.harness.models import HarnessCheckSpec

EVOLUTION_REVALIDATION_RUNTIME_CONTRACT_POLICY = (
    "evolution-revalidation-runtime-contract-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationRuntimeContract(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-runtime-contract-v1"] = (
        EVOLUTION_REVALIDATION_RUNTIME_CONTRACT_POLICY
    )
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    requested_samples: int = Field(ge=5, le=100)
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    metric_entries: tuple[MetricRunnerBindingEntry, ...] = Field(
        min_length=1,
        max_length=8,
    )
    probe_registry_sha256: str = Field(pattern=_SHA256_RE)
    probe_requirements: tuple[AdversarialProbeRequirement, ...] = Field(
        min_length=1,
        max_length=96,
    )
    probe_checks: tuple[AdversarialProbeCheckBinding, ...] = Field(max_length=80)
    probe_coverage: tuple[AdversarialProbeCoverage, ...] = Field(max_length=96)
    probe_blockers: tuple[AdversarialProbeBlocker, ...] = Field(max_length=96)
    profile_timeout_seconds_per_sample: int = Field(ge=1, le=288_000)
    metric_timeout_seconds_per_sample: int = Field(ge=0, le=28_800)
    required_duration_seconds: int = Field(ge=1, le=2_883_600)
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    blocking_codes: tuple[str, ...] = Field(max_length=104)
    binding_status: Literal["ready", "blocked"]
    metric_binding_complete: bool
    adversarial_coverage_complete: bool
    identical_red_green_environment_required: Literal[True] = True
    profile_trust_must_be_revalidated: Literal[True] = True
    execution_started: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _contract_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Revalidation Runtime Contract workspace 必须 canonical。")
        if tuple(item.order for item in self.metric_entries) != tuple(
            range(1, len(self.metric_entries) + 1)
        ):
            raise ValueError("Revalidation metric entries 顺序无效。")
        requirements = tuple(
            (item.path, item.kind, item.probe_id) for item in self.probe_requirements
        )
        coverage = tuple(
            (item.path, item.kind, item.probe_id) for item in self.probe_coverage
        )
        blockers = tuple(
            (item.path, item.kind, item.probe_id) for item in self.probe_blockers
        )
        if requirements != tuple(sorted(set(requirements))):
            raise ValueError("Revalidation probe requirements 顺序无效。")
        if set(coverage) | set(blockers) != set(requirements) or (
            set(coverage) & set(blockers)
        ):
            raise ValueError("Revalidation probe coverage/blocker 投影不完整。")
        metric_codes = {
            item.resolution.blocking_code
            for item in self.metric_entries
            if item.resolution.blocking_code is not None
        }
        probe_codes = {item.code for item in self.probe_blockers}
        budget_codes = (
            {"runtime_duration_budget_exceeded"}
            if self.required_duration_seconds > self.max_total_duration_seconds
            else set()
        )
        expected_codes = tuple(sorted(metric_codes | probe_codes | budget_codes))
        if self.blocking_codes != expected_codes:
            raise ValueError("Revalidation runtime blocking codes 投影不一致。")
        if self.metric_binding_complete is not (not metric_codes and not budget_codes):
            raise ValueError("Revalidation metric binding 状态不一致。")
        if self.adversarial_coverage_complete is not (not probe_codes):
            raise ValueError("Revalidation adversarial coverage 状态不一致。")
        expected_status = "ready" if not expected_codes else "blocked"
        if self.binding_status != expected_status:
            raise ValueError("Revalidation runtime binding status 不一致。")
        expected_duration = self.requested_samples * (
            self.profile_timeout_seconds_per_sample
            + self.metric_timeout_seconds_per_sample
        )
        if self.required_duration_seconds != expected_duration:
            raise ValueError("Revalidation runtime duration 投影不一致。")
        created = datetime.fromisoformat(self.created_at)
        if created.utcoffset() is None:
            raise ValueError("Revalidation Runtime Contract 时间必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"contract_id", "contract_sha256"})
        )
        if not hmac.compare_digest(self.contract_sha256, digest):
            raise ValueError("Revalidation Runtime Contract 摘要不一致。")
        if self.contract_id != f"evrevalruntime_{digest[:24]}":
            raise ValueError("Revalidation Runtime Contract identity 不一致。")
        return self


class EvolutionRevalidationRuntimeContractView(_StrictModel):
    contract: EvolutionRevalidationRuntimeContract
    source_current: bool
    current_status: Literal["ready", "blocked", "stale"]
    execution_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected = (
            "stale"
            if not self.source_current
            else self.contract.binding_status
        )
        if self.current_status != expected:
            raise ValueError("Revalidation Runtime Contract view 状态不一致。")
        if self.execution_eligible is not (
            self.source_current and self.contract.binding_status == "ready"
        ):
            raise ValueError("Revalidation Runtime Contract eligibility 不一致。")
        return self


class EvolutionRevalidationRuntimeContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRuntimeContractBuilder:
    def __init__(
        self,
        *,
        metric_registry: EvolutionMetricRunnerRegistry | None = None,
        probe_registry: EvolutionAdversarialProbeRegistry | None = None,
    ) -> None:
        self._metric_registry = metric_registry or EvolutionMetricRunnerRegistry()
        self._probe_registry = probe_registry or EvolutionAdversarialProbeRegistry()

    def build(
        self,
        *,
        plan: EvolutionRevalidationValidationPlan,
        now: datetime,
    ) -> EvolutionRevalidationRuntimeContract:
        if now.utcoffset() is None:
            raise EvolutionRevalidationRuntimeContractError(
                "revalidation_runtime_contract_clock_invalid",
                "Revalidation Runtime Contract 时钟必须包含 offset。",
            )
        paths = tuple(item.path for item in plan.files)
        metric_entries = tuple(
            MetricRunnerBindingEntry(
                order=item.order,
                metric_name=item.metric_name,
                direction=item.direction,
                target=item.target,
                procedure_sha256=hashlib.sha256(
                    item.procedure.encode("utf-8")
                ).hexdigest(),
                resolution=self._metric_registry.resolve(
                    BaselineCohortMetricCase(
                        order=item.order,
                        metric_name=item.metric_name,
                        direction=item.direction,
                        target=item.target,
                        verifier=item.verifier,
                        procedure_sha256=hashlib.sha256(
                            item.procedure.encode("utf-8")
                        ).hexdigest(),
                    ),
                    validation_paths=paths,
                ),
            )
            for item in plan.metrics
        )
        requirements = tuple(sorted(
            (
                requirement
                for file in plan.files
                for requirement in self._probe_registry.requirements_for(file.path)
            ),
            key=lambda item: (item.path, item.kind, item.probe_id),
        ))
        coverage, blockers, used_checks = _bind_probes(
            requirements,
            plan.checks,
        )
        profile_timeout = sum(item.timeout_seconds for item in plan.checks)
        metric_timeout = sum(
            item.resolution.timeout_seconds_per_sample or 0
            for item in metric_entries
        )
        required_duration = plan.requested_samples * (
            profile_timeout + metric_timeout
        )
        metric_codes = {
            item.resolution.blocking_code
            for item in metric_entries
            if item.resolution.blocking_code is not None
        }
        probe_codes = {item.code for item in blockers}
        budget_codes = (
            {"runtime_duration_budget_exceeded"}
            if required_duration > plan.budget.max_duration_seconds
            else set()
        )
        blocking_codes = tuple(sorted(metric_codes | probe_codes | budget_codes))
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_RUNTIME_CONTRACT_POLICY,
            "workspace_root": plan.workspace_root,
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "source_snapshot_id": plan.source_snapshot_id,
            "source_snapshot_sha256": plan.source_snapshot_sha256,
            "candidate_id": plan.candidate_id,
            "candidate_revision": plan.candidate_revision,
            "suite_id": plan.suite_id,
            "requested_samples": plan.requested_samples,
            "seed": plan.seed,
            "required_platforms": list(plan.required_platforms),
            "metric_entries": [item.model_dump(mode="json") for item in metric_entries],
            "probe_registry_sha256": self._probe_registry.sha256,
            "probe_requirements": [item.model_dump(mode="json") for item in requirements],
            "probe_checks": [
                _probe_check_binding(item).model_dump(mode="json")
                for item in sorted(used_checks.values(), key=lambda item: item.id)
            ],
            "probe_coverage": [item.model_dump(mode="json") for item in coverage],
            "probe_blockers": [item.model_dump(mode="json") for item in blockers],
            "profile_timeout_seconds_per_sample": profile_timeout,
            "metric_timeout_seconds_per_sample": metric_timeout,
            "required_duration_seconds": required_duration,
            "max_total_duration_seconds": plan.budget.max_duration_seconds,
            "blocking_codes": list(blocking_codes),
            "binding_status": "blocked" if blocking_codes else "ready",
            "metric_binding_complete": not metric_codes and not budget_codes,
            "adversarial_coverage_complete": not probe_codes,
            "identical_red_green_environment_required": True,
            "profile_trust_must_be_revalidated": True,
            "execution_started": False,
            "promotion_authority": False,
            "created_at": now.isoformat(),
        }
        digest = _sha256_payload(payload)
        return EvolutionRevalidationRuntimeContract.model_validate({
            **payload,
            "contract_id": f"evrevalruntime_{digest[:24]}",
            "contract_sha256": digest,
        })


class EvolutionRevalidationRuntimeContractStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        item: EvolutionRevalidationRuntimeContract,
    ) -> EvolutionRevalidationRuntimeContract:
        artifact = EvolutionRevalidationRuntimeContract.model_validate_json(
            item.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (
                await db.execute(
                    "SELECT validation_plan_sha256 FROM "
                    "evolution_revalidation_validation_plans "
                    "WHERE validation_plan_id = ?",
                    (artifact.validation_plan_id,),
                )
            ).fetchone()
            if dependency is None or (
                dependency["validation_plan_sha256"]
                != artifact.validation_plan_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationRuntimeContractError(
                    "revalidation_runtime_contract_plan_mismatch",
                    "持久化 Validation Plan 不存在或 digest 不一致。",
                )
            row = await (
                await db.execute(
                    "SELECT contract_json FROM evolution_revalidation_runtime_contracts "
                    "WHERE validation_plan_id = ?",
                    (artifact.validation_plan_id,),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionRevalidationRuntimeContract.model_validate_json(
                    row["contract_json"]
                )
                await db.rollback()
                if restored != artifact:
                    raise EvolutionRevalidationRuntimeContractError(
                        "revalidation_runtime_contract_conflict",
                        "同一 Validation Plan 已绑定不同 Runtime Contract。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_runtime_contracts "
                "(contract_id, contract_sha256, validation_plan_id, contract_json, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    artifact.contract_id,
                    artifact.contract_sha256,
                    artifact.validation_plan_id,
                    artifact.model_dump_json(),
                    artifact.created_at,
                ),
            )
            await db.commit()
        return artifact

    async def get(self, contract_id: str):
        return await self._read("contract_id", contract_id)

    async def get_by_plan(self, validation_plan_id: str):
        return await self._read("validation_plan_id", validation_plan_id)

    async def _read(self, field: str, value: str):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT contract_json FROM evolution_revalidation_runtime_contracts "
                    f"WHERE {field} = ?",
                    (value,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationRuntimeContract.model_validate_json(
                row["contract_json"]
            )
        )


class EvolutionRevalidationRuntimeContractService:
    def __init__(
        self,
        *,
        validation_plan_service: EvolutionRevalidationValidationPlanService,
        store: EvolutionRevalidationRuntimeContractStore,
        builder: EvolutionRevalidationRuntimeContractBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._plan_service = validation_plan_service
        self._store = store
        self._builder = builder or EvolutionRevalidationRuntimeContractBuilder()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def issue(self, *, workspace_root: str | Path, validation_plan_id: str):
        existing = await self._store.get_by_plan(validation_plan_id)
        if existing is not None:
            return await self.inspect(
                workspace_root=workspace_root,
                contract_id=existing.contract_id,
            )
        view = await self._plan_service.inspect(
            workspace_root=workspace_root,
            validation_plan_id=validation_plan_id,
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationRuntimeContractError(
                "revalidation_runtime_contract_plan_stale",
                "Revalidation Validation Plan 已 stale。",
            )
        contract = self._builder.build(plan=view.plan, now=self._clock())
        stored = await self._store.record(contract)
        return _view(stored, source_current=True)

    async def inspect(self, *, workspace_root: str | Path, contract_id: str):
        stored = await self._store.get(contract_id)
        if stored is None:
            raise EvolutionRevalidationRuntimeContractError(
                "revalidation_runtime_contract_not_found",
                "Revalidation Runtime Contract 不存在。",
            )
        try:
            plan_view = await self._plan_service.inspect(
                workspace_root=workspace_root,
                validation_plan_id=stored.validation_plan_id,
            )
            rebuilt = self._builder.build(
                plan=plan_view.plan,
                now=datetime.fromisoformat(stored.created_at),
            )
            current = plan_view.execution_eligible and rebuilt == stored
        except (OSError, TypeError, ValueError, RuntimeError):
            current = False
        return _view(stored, source_current=current)


def _bind_probes(requirements, checks: tuple[HarnessCheckSpec, ...]):
    coverage = []
    blockers = []
    used = {}
    for requirement in requirements:
        selected = set(select_required_check_ids(
            checks,
            task_kind="change",
            changed_paths=(requirement.path,),
        ))
        candidates = tuple(
            item
            for item in checks
            if item.id in selected and requirement.kind in item.adversarial_probes
        )
        if len(candidates) == 1:
            used[candidates[0].id] = candidates[0]
            coverage.append(AdversarialProbeCoverage(
                path=requirement.path,
                probe_id=requirement.probe_id,
                kind=requirement.kind,
                check_id=candidates[0].id,
            ))
        else:
            blockers.append(AdversarialProbeBlocker(
                code="probe_check_missing" if not candidates else "probe_check_ambiguous",
                path=requirement.path,
                probe_id=requirement.probe_id,
                kind=requirement.kind,
                candidate_check_ids=tuple(sorted(item.id for item in candidates)),
            ))
    def key(item):
        return item.path, item.kind, item.probe_id

    return tuple(sorted(coverage, key=key)), tuple(sorted(blockers, key=key)), used


def _probe_check_binding(check: HarnessCheckSpec) -> AdversarialProbeCheckBinding:
    return AdversarialProbeCheckBinding(
        check_id=check.id,
        spec_sha256=_sha256_payload(check.model_dump(mode="json")),
        argv_sha256=_sha256_payload(list(check.argv)),
        timeout_seconds=check.timeout_seconds,
        probes=check.adversarial_probes,
    )


def _view(contract, *, source_current: bool):
    return EvolutionRevalidationRuntimeContractView(
        contract=contract,
        source_current=source_current,
        current_status=(
            contract.binding_status if source_current else "stale"
        ),
        execution_eligible=source_current and contract.binding_status == "ready",
    )


def render_evolution_revalidation_runtime_contract(
    view: EvolutionRevalidationRuntimeContractView,
) -> str:
    item = view.contract
    return "\n".join([
        f"# Evolution Fresh Runtime Contract `{item.contract_id}`",
        "",
        f"- Current status：`{view.current_status}`",
        f"- Metric runners：{len(item.metric_entries)}",
        "- Adversarial probes / covered："
        f"{len(item.probe_requirements)} / {len(item.probe_coverage)}",
        f"- Samples / platforms：{item.requested_samples} / {len(item.required_platforms)}",
        "- Required / budget seconds："
        f"{item.required_duration_seconds} / {item.max_total_duration_seconds}",
        f"- Blockers：{', '.join(item.blocking_codes) if item.blocking_codes else 'none'}",
        "- Execution / Promotion authority：`false` / `false`",
    ])


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_runtime_contracts ("
        "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL UNIQUE, "
        "validation_plan_id TEXT NOT NULL UNIQUE, contract_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_RUNTIME_CONTRACT_POLICY",
    "EvolutionRevalidationRuntimeContract",
    "EvolutionRevalidationRuntimeContractBuilder",
    "EvolutionRevalidationRuntimeContractError",
    "EvolutionRevalidationRuntimeContractService",
    "EvolutionRevalidationRuntimeContractStore",
    "EvolutionRevalidationRuntimeContractView",
    "render_evolution_revalidation_runtime_contract",
]
