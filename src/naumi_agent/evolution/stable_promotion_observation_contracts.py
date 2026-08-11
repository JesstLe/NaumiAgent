"""Frozen policy contract for observing a successfully finalized stable rollout."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractAuthority,
    EvolutionExperimentContractStore,
    EvolutionExperimentContractStoreError,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInput,
    EvolutionRevalidationPromotionInputError,
    EvolutionRevalidationPromotionInputService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
)
from naumi_agent.evolution.stable_population_completions import (
    EvolutionStablePopulationCompletionError,
    EvolutionStablePopulationCompletionReceipt,
    EvolutionStablePopulationCompletionService,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationError,
    EvolutionStableRemotePopulationFinalizationReceipt,
    EvolutionStableRemotePopulationFinalizationService,
)

EVOLUTION_STABLE_PROMOTION_OBSERVATION_CONTRACT_POLICY = (
    "evolution-stable-promotion-observation-contract-v1"
)
_FINALIZATION_RE = re.compile(r"^evstableremotepopfinal_[0-9a-f]{24}$")
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionObservationContract(_StrictModel):
    """Exact successful-rollout lineage and future observation rules."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-observation-contract-v1"
    ] = EVOLUTION_STABLE_PROMOTION_OBSERVATION_CONTRACT_POLICY
    contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    population_finalization_receipt_id: str = Field(
        pattern=r"^evstableremotepopfinal_[0-9a-f]{24}$"
    )
    population_finalization_receipt_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=10_000)
    population_completion_receipt_id: str = Field(
        pattern=r"^evstablepopcomplete_[0-9a-f]{24}$"
    )
    population_completion_receipt_sha256: str = Field(pattern=_SHA256_RE)
    population_completion_source_set_sha256: str = Field(pattern=_SHA256_RE)
    rollout_plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    rollout_plan_sha256: str = Field(pattern=_SHA256_RE)
    approval_decision_id: str = Field(
        pattern=r"^evreapprovaldecision_[0-9a-f]{24}$"
    )
    approval_decision_sha256: str = Field(pattern=_SHA256_RE)
    approval_source_set_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    experiment_authority_id: str = Field(pattern=r"^evxauth_[0-9a-f]{24}$")
    experiment_authority_sha256: str = Field(pattern=_SHA256_RE)
    workbench_session_id: str = Field(pattern=r"^[^\x00\r\n]{1,128}$")
    workbench_proposal_id: str = Field(pattern=r"^[^\x00\r\n]{1,128}$")
    proposal_id: str = Field(pattern=r"^evp_[0-9a-f]{24}$")
    proposal_kind: Literal["knowledge", "profile", "prompt", "tool", "test", "code"]
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    eligible_surfaces: tuple[Literal["new_ui", "tui"], ...] = ("new_ui", "tui")
    required_chain_origin_kind: Literal["startup"] = "startup"
    required_chain_origin_sequence: Literal[1] = 1
    operational_phases: tuple[Literal["running", "waiting"], ...] = (
        "running",
        "waiting",
    )
    breach_phases: tuple[Literal["failed"], ...] = ("failed",)
    censor_phases: tuple[Literal["draining", "stopped"], ...] = (
        "draining",
        "stopped",
    )
    minimum_observation_seconds: Literal[3600] = 3600
    minimum_operational_samples: Literal[12] = 12
    maximum_sample_count: Literal[5000] = 5000
    gap_limit_rule: Literal["sample_timeout_seconds"] = "sample_timeout_seconds"
    latest_age_limit_rule: Literal["sample_timeout_seconds"] = (
        "sample_timeout_seconds"
    )
    require_constant_runtime_binding: Literal[True] = True
    require_contiguous_sequence_and_hash: Literal[True] = True
    approval_current_at_issue: Literal[True] = True
    active_stable_runtime_at_issue: Literal[True] = True
    proposal_binding_verified: Literal[True] = True
    observation_contract_recorded: Literal[True] = True
    long_term_metrics_recorded: Literal[False] = False
    observation_window_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    window_not_before_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("稳定推广观察契约 workspace 必须 canonical。")
        if any(char in self.workspace_root for char in ("\x00", "\r", "\n")):
            raise ValueError("稳定推广观察契约 workspace 包含不安全字符。")
        if self.eligible_surfaces != ("new_ui", "tui"):
            raise ValueError("稳定推广观察契约必须覆盖 New UI 与 TUI。")
        if self.operational_phases != ("running", "waiting") or not (
            self.breach_phases == ("failed",)
            and self.censor_phases == ("draining", "stopped")
        ):
            raise ValueError("稳定推广观察契约 phase 规则不完整。")
        if any(
            (
                self.long_term_metrics_recorded,
                self.observation_window_authority,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("稳定推广观察契约不得冒充长期效果或推广 authority。")
        _aware(self.window_not_before_at)
        digest = _digest(
            self.model_dump(mode="json", exclude={"contract_id", "contract_sha256"})
        )
        if not (
            hmac.compare_digest(self.contract_sha256, digest)
            and self.contract_id == f"evstablepromobserve_{digest[:24]}"
        ):
            raise ValueError("稳定推广观察契约 content identity 不一致。")
        return self


class EvolutionStablePromotionObservationContractView(_StrictModel):
    contract: EvolutionStablePromotionObservationContract
    status: Literal["recorded", "stale"]
    durable_contract_valid: bool
    approval_authority: bool
    active_stable_runtime_authority: bool
    proposal_binding_authority: bool
    observation_contract_authority: bool
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_contract_valid
            and self.approval_authority
            and self.active_stable_runtime_authority
            and self.proposal_binding_authority
        )
        if not (
            self.observation_contract_authority is expected
            and (self.status == "recorded") is expected
        ):
            raise ValueError("稳定推广观察契约 authority projection 不一致。")
        return self


class EvolutionStablePromotionObservationContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionObservationContractStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_finalization(
        self, receipt_id: str
    ) -> EvolutionStablePromotionObservationContract | None:
        normalized = _finalization_id(receipt_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_observation_contracts "
                        "WHERE population_finalization_receipt_id = ?",
                        (normalized,),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(row["contract_json"])
            if not (
                item.contract_id == row["contract_id"]
                and item.contract_sha256 == row["contract_sha256"]
                and item.population_finalization_receipt_id
                == row["population_finalization_receipt_id"]
                and item.population_completion_receipt_id
                == row["population_completion_receipt_id"]
                and item.rollout_plan_id == row["rollout_plan_id"]
                and item.experiment_contract_id == row["experiment_contract_id"]
            ):
                raise ValueError("stable promotion observation row mismatch")
            if not await _dependencies_current(self.db_path, item):
                raise EvolutionStablePromotionObservationContractError(
                    "stable_promotion_observation_contract_dependency_changed",
                    "稳定推广观察契约的 durable dependency 已变化。",
                )
            return item
        except EvolutionStablePromotionObservationContractError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_contract_store_corrupt",
                "稳定推广长期观察契约损坏或无法读取。",
            ) from exc

    async def record(
        self, contract: EvolutionStablePromotionObservationContract
    ) -> EvolutionStablePromotionObservationContract:
        item = _contract(contract)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_contract_oversized",
                "稳定推广长期观察契约超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                existing = await (
                    await db.execute(
                        "SELECT contract_json FROM "
                        "evolution_stable_promotion_observation_contracts "
                        "WHERE population_finalization_receipt_id = ?",
                        (item.population_finalization_receipt_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["contract_json"])
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionStablePromotionObservationContractError(
                        "stable_promotion_observation_contract_conflict",
                        "同一 Population Finalization 已绑定不同观察契约。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_observation_contracts "
                    "(contract_id, contract_sha256, population_finalization_receipt_id, "
                    "population_completion_receipt_id, rollout_plan_id, "
                    "experiment_contract_id, contract_json, window_not_before_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.contract_id,
                        item.contract_sha256,
                        item.population_finalization_receipt_id,
                        item.population_completion_receipt_id,
                        item.rollout_plan_id,
                        item.experiment_contract_id,
                        encoded,
                        item.window_not_before_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionObservationContractError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_contract_store_failed",
                "稳定推广长期观察契约无法持久化。",
            ) from exc


class EvolutionStablePromotionObservationContractService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        finalization_service: EvolutionStableRemotePopulationFinalizationService,
        completion_service: EvolutionStablePopulationCompletionService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        promotion_input_service: EvolutionRevalidationPromotionInputService,
        experiment_store: EvolutionExperimentContractStore,
        store: EvolutionStablePromotionObservationContractStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.finalization_service = finalization_service
        self.completion_service = completion_service
        self.plan_service = plan_service
        self.promotion_input_service = promotion_input_service
        self.experiment_store = experiment_store
        self.store = store
        paths = {
            finalization_service.store.db_path,
            completion_service.store.db_path,
            plan_service.store.db_path,
            promotion_input_service.store.db_path,
            experiment_store.db_path,
            store.db_path,
        }
        if len(paths) != 1:
            raise ValueError("稳定推广观察 authority stores 必须共享 session SQLite。")
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self, *, finalization_receipt_id: str
    ) -> EvolutionStablePromotionObservationContractView:
        receipt_id = _finalization_id(finalization_receipt_id)
        lock = self._locks.setdefault(receipt_id, asyncio.Lock())
        async with lock:
            sources = await self._sources(receipt_id)
            finalization_view, completion_view, plan_view, _input, _experiment = sources
            if not (
                finalization_view.stable_population_finalization_authority
                and completion_view.stable_population_completion_authority
                and plan_view.current_rollout_eligible
            ):
                raise EvolutionStablePromotionObservationContractError(
                    "stable_promotion_observation_lineage_stale",
                    "稳定 Population 或 Approval 当前不具备观察契约 authority。",
                )
            proposed = _build_contract(*sources)
            existing = await self.store.get_by_finalization(receipt_id)
            if existing is None:
                existing = await self.store.record(proposed)
            elif existing != proposed:
                raise EvolutionStablePromotionObservationContractError(
                    "stable_promotion_observation_contract_conflict",
                    "既有观察契约与当前 exact lineage 不一致。",
                )
            view = await self.inspect(contract=existing)
            if not view.observation_contract_authority:
                raise EvolutionStablePromotionObservationContractError(
                    "stable_promotion_observation_contract_authority_changed",
                    "稳定推广观察契约持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self, *, contract: EvolutionStablePromotionObservationContract
    ) -> EvolutionStablePromotionObservationContractView:
        item = _contract(contract)
        durable = approval = active = proposal = False
        try:
            sources = await self._sources(item.population_finalization_receipt_id)
            rebuilt = _build_contract(*sources)
            finalization_view, completion_view, plan_view, _input, _experiment = sources
            approval = bool(plan_view.decision_current)
            active = bool(
                finalization_view.stable_population_finalization_authority
                and completion_view.stable_population_completion_authority
            )
            proposal = True
            stored = await self.store.get_by_finalization(
                item.population_finalization_receipt_id
            )
            durable = stored == item and rebuilt == item
        except (
            EvolutionStablePromotionObservationContractError,
            EvolutionStableRemotePopulationFinalizationError,
            EvolutionStablePopulationCompletionError,
            EvolutionRevalidationRolloutPlanError,
            EvolutionRevalidationPromotionInputError,
            EvolutionExperimentContractStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            pass
        authority = bool(durable and approval and active and proposal)
        return EvolutionStablePromotionObservationContractView(
            contract=item,
            status="recorded" if authority else "stale",
            durable_contract_valid=durable,
            approval_authority=approval,
            active_stable_runtime_authority=active,
            proposal_binding_authority=proposal,
            observation_contract_authority=authority,
        )

    async def _sources(self, receipt_id: str):
        finalization_view = await self.finalization_service.inspect(receipt_id=receipt_id)
        finalization = finalization_view.receipt
        completion_view = await self.completion_service.inspect(
            receipt_id=finalization.population_completion_receipt_id
        )
        completion = completion_view.receipt
        plan_view = await self.plan_service.inspect(plan_id=completion.plan_id)
        plan = plan_view.plan
        promotion_input = await self.promotion_input_service.store.get(plan.contract_id)
        if promotion_input is None:
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_input_missing",
                "稳定推广观察契约缺少 Fresh Promotion Input。",
            )
        experiment = await self.experiment_store.get(
            self.workspace_root,
            promotion_input.prior_input.experiment_contract_id,
        )
        if experiment is None:
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_experiment_missing",
                "稳定推广观察契约缺少 Experiment/Proposal authority。",
            )
        if not _lineage_matches(
            self.workspace_root,
            finalization,
            completion,
            plan,
            promotion_input,
            experiment,
        ):
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_lineage_mismatch",
                "稳定 Population、Approval 与 Proposal lineage 不一致。",
            )
        return finalization_view, completion_view, plan_view, promotion_input, experiment


def render_stable_promotion_observation_contract(
    view: EvolutionStablePromotionObservationContractView,
) -> str:
    item = view.contract
    return "\n".join(
        [
            f"# Stable Promotion Observation Contract `{item.contract_id}`",
            "",
            f"- 状态：`{view.status}`",
            f"- Population Finalization：`{item.population_finalization_receipt_id}`",
            f"- Population Completion：`{item.population_completion_receipt_id}`",
            f"- Rollout Plan / Approval：`{item.rollout_plan_id}` / `{item.approval_decision_id}`",
            f"- Proposal：`{item.workbench_proposal_id}` · `{item.proposal_kind}`",
            f"- Candidate：`{item.candidate_version}` · `{item.candidate_target}`",
            f"- Surface：`{' / '.join(item.eligible_surfaces)}`",
            f"- 最短观察：`{item.minimum_observation_seconds}s`",
            f"- 最少运行样本：`{item.minimum_operational_samples}`",
            "- 最大间隙 / 最新样本年龄：不得超过每条 heartbeat 的 timeout",
            "- Censoring：`draining / stopped`；Breach：`failed`",
            f"- Approval authority：`{str(view.approval_authority).lower()}`",
            "- Active stable runtime / Proposal binding authority："
            f"`{str(view.active_stable_runtime_authority).lower()} / "
            f"{str(view.proposal_binding_authority).lower()}`",
            "- 长期指标 / Window / Promoted Outcome authority：`false / false / false`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _build_contract(finalization_view, completion_view, plan_view, promotion_input, experiment):
    finalization = finalization_view.receipt
    completion = completion_view.receipt
    plan = plan_view.plan
    source = experiment.contract.source
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OBSERVATION_CONTRACT_POLICY,
        "workspace_root": completion.workspace_root,
        "population_finalization_receipt_id": finalization.receipt_id,
        "population_finalization_receipt_sha256": finalization.receipt_sha256,
        "population_snapshot_id": finalization.population_snapshot_id,
        "population_snapshot_sha256": finalization.population_snapshot_sha256,
        "population_snapshot_sequence": finalization.population_sequence,
        "population_denominator": finalization.population_denominator,
        "population_completion_receipt_id": completion.receipt_id,
        "population_completion_receipt_sha256": completion.receipt_sha256,
        "population_completion_source_set_sha256": completion.source_set_sha256,
        "rollout_plan_id": plan.plan_id,
        "rollout_plan_sha256": plan.plan_sha256,
        "approval_decision_id": plan.decision_id,
        "approval_decision_sha256": plan.decision_sha256,
        "approval_source_set_sha256": plan.decision_source_set_sha256,
        "promotion_input_id": promotion_input.input_id,
        "promotion_input_sha256": promotion_input.input_sha256,
        "experiment_contract_id": experiment.contract_id,
        "experiment_contract_sha256": experiment.contract_manifest_sha256,
        "experiment_authority_id": experiment.authority_id,
        "experiment_authority_sha256": experiment.authority_sha256,
        "workbench_session_id": source.session_id,
        "workbench_proposal_id": source.workbench_proposal_id,
        "proposal_id": source.proposal_id,
        "proposal_kind": source.proposal_kind,
        "candidate_id": source.candidate_id,
        "candidate_revision": source.candidate_revision,
        "candidate_sha256": source.candidate_sha256,
        "candidate_version": completion.candidate_version,
        "candidate_target": completion.candidate_target,
        "eligible_surfaces": ["new_ui", "tui"],
        "required_chain_origin_kind": "startup",
        "required_chain_origin_sequence": 1,
        "operational_phases": ["running", "waiting"],
        "breach_phases": ["failed"],
        "censor_phases": ["draining", "stopped"],
        "minimum_observation_seconds": 3600,
        "minimum_operational_samples": 12,
        "maximum_sample_count": 5000,
        "gap_limit_rule": "sample_timeout_seconds",
        "latest_age_limit_rule": "sample_timeout_seconds",
        "require_constant_runtime_binding": True,
        "require_contiguous_sequence_and_hash": True,
        "approval_current_at_issue": True,
        "active_stable_runtime_at_issue": True,
        "proposal_binding_verified": True,
        "observation_contract_recorded": True,
        "long_term_metrics_recorded": False,
        "observation_window_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "window_not_before_at": finalization.finalized_at,
    }
    digest = _digest(core)
    return EvolutionStablePromotionObservationContract.model_validate(
        {
            **core,
            "contract_id": f"evstablepromobserve_{digest[:24]}",
            "contract_sha256": digest,
        }
    )


def _lineage_matches(
    workspace_root: Path,
    finalization: EvolutionStableRemotePopulationFinalizationReceipt,
    completion: EvolutionStablePopulationCompletionReceipt,
    plan: EvolutionRevalidationRolloutPlan,
    promotion_input: EvolutionRevalidationPromotionInput,
    experiment: EvolutionExperimentContractAuthority,
) -> bool:
    prior = promotion_input.prior_input
    source = experiment.contract.source
    return bool(
        completion.workspace_root == str(workspace_root)
        and finalization.population_completion_receipt_id == completion.receipt_id
        and finalization.population_completion_receipt_sha256 == completion.receipt_sha256
        and finalization.population_snapshot_id == completion.population_snapshot_id
        and finalization.population_snapshot_sha256 == completion.population_snapshot_sha256
        and finalization.population_sequence == completion.population_snapshot_sequence
        and finalization.population_denominator == completion.population_denominator
        and finalization.candidate_version == completion.candidate_version
        and completion.plan_id == plan.plan_id
        and completion.plan_sha256 == plan.plan_sha256
        and plan.workspace_root == str(workspace_root)
        and plan.promotion_input_id == promotion_input.input_id
        and plan.promotion_input_sha256 == promotion_input.input_sha256
        and plan.contract_id == promotion_input.contract_id
        and plan.contract_sha256 == promotion_input.contract_sha256
        and prior.experiment_contract_id == experiment.contract_id
        and prior.experiment_contract_sha256 == experiment.contract_manifest_sha256
        and experiment.workspace_root == str(workspace_root)
        and plan.candidate_id == promotion_input.candidate_id == source.candidate_id
        and plan.candidate_revision
        == promotion_input.candidate_revision
        == source.candidate_revision
        and prior.candidate_sha256 == source.candidate_sha256
    )


async def _require_dependencies(
    db: aiosqlite.Connection, item: EvolutionStablePromotionObservationContract
) -> None:
    checks = (
        (
            "SELECT receipt_sha256 FROM evolution_stable_remote_population_finalizations "
            "WHERE receipt_id = ?",
            item.population_finalization_receipt_id,
            item.population_finalization_receipt_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_stable_population_completions "
            "WHERE receipt_id = ?",
            item.population_completion_receipt_id,
            item.population_completion_receipt_sha256,
        ),
        (
            "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans WHERE plan_id = ?",
            item.rollout_plan_id,
            item.rollout_plan_sha256,
        ),
        (
            "SELECT input_sha256 FROM evolution_revalidation_promotion_inputs WHERE input_id = ?",
            item.promotion_input_id,
            item.promotion_input_sha256,
        ),
        (
            "SELECT authority_sha256 FROM evolution_experiment_contracts "
            "WHERE workspace_root = ? AND contract_id = ?",
            (item.workspace_root, item.experiment_contract_id),
            item.experiment_authority_sha256,
        ),
    )
    for query, parameter, expected in checks:
        params = parameter if isinstance(parameter, tuple) else (parameter,)
        row = await (await db.execute(query, params)).fetchone()
        if row is None or row[0] != expected:
            await db.rollback()
            raise EvolutionStablePromotionObservationContractError(
                "stable_promotion_observation_contract_dependency_mismatch",
                "稳定推广观察契约缺少 exact durable dependency。",
            )


async def _dependencies_current(
    db_path: Path, item: EvolutionStablePromotionObservationContract
) -> bool:
    checks = (
        (
            "SELECT receipt_sha256 FROM evolution_stable_remote_population_finalizations "
            "WHERE receipt_id = ?",
            (item.population_finalization_receipt_id,),
            item.population_finalization_receipt_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_stable_population_completions "
            "WHERE receipt_id = ?",
            (item.population_completion_receipt_id,),
            item.population_completion_receipt_sha256,
        ),
        (
            "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans WHERE plan_id = ?",
            (item.rollout_plan_id,),
            item.rollout_plan_sha256,
        ),
        (
            "SELECT input_sha256 FROM evolution_revalidation_promotion_inputs WHERE input_id = ?",
            (item.promotion_input_id,),
            item.promotion_input_sha256,
        ),
        (
            "SELECT authority_sha256 FROM evolution_experiment_contracts "
            "WHERE workspace_root = ? AND contract_id = ?",
            (item.workspace_root, item.experiment_contract_id),
            item.experiment_authority_sha256,
        ),
    )
    try:
        async with aiosqlite.connect(db_path, timeout=5.0) as db:
            for query, params, expected in checks:
                row = await (await db.execute(query, params)).fetchone()
                if row is None or row[0] != expected:
                    return False
    except (aiosqlite.Error, OSError, TypeError, ValueError):
        return False
    return True


def _contract(value) -> EvolutionStablePromotionObservationContract:
    try:
        return EvolutionStablePromotionObservationContract.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationContractError(
            "stable_promotion_observation_contract_invalid",
            "稳定推广长期观察契约无效。",
        ) from exc


def _restore(raw: str) -> EvolutionStablePromotionObservationContract:
    if len(str(raw).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("stable promotion observation contract oversized")
    return EvolutionStablePromotionObservationContract.model_validate_json(raw)


def _finalization_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _FINALIZATION_RE.fullmatch(normalized) is None:
        raise EvolutionStablePromotionObservationContractError(
            "stable_promotion_observation_finalization_id_invalid",
            "Population Finalization Receipt ID 格式无效。",
        )
    return normalized


def _aware(value: str):
    from datetime import datetime

    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("稳定推广观察契约时间必须包含 offset。")
    return parsed


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
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_observation_contracts ("
        "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL UNIQUE, "
        "population_finalization_receipt_id TEXT NOT NULL UNIQUE, "
        "population_completion_receipt_id TEXT NOT NULL UNIQUE, "
        "rollout_plan_id TEXT NOT NULL, experiment_contract_id TEXT NOT NULL, "
        "contract_json TEXT NOT NULL, window_not_before_at TEXT NOT NULL)"
    )


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_OBSERVATION_CONTRACT_POLICY",
    "EvolutionStablePromotionObservationContract",
    "EvolutionStablePromotionObservationContractError",
    "EvolutionStablePromotionObservationContractService",
    "EvolutionStablePromotionObservationContractStore",
    "EvolutionStablePromotionObservationContractView",
    "render_stable_promotion_observation_contract",
]
