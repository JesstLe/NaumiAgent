"""Durable proposal-bound Outcome for a verified revalidation rollback."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractAuthority,
    EvolutionExperimentContractStore,
    EvolutionExperimentContractStoreError,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInput,
    EvolutionRevalidationPromotionInputError,
    EvolutionRevalidationPromotionInputStore,
)
from naumi_agent.evolution.revalidation_rollback_executions import (
    EvolutionRevalidationRollbackExecutionError,
    EvolutionRevalidationRollbackExecutionReceipt,
    EvolutionRevalidationRollbackExecutionService,
)
from naumi_agent.evolution.revalidation_rollback_requests import (
    EvolutionRevalidationRollbackRequest,
    EvolutionRevalidationRollbackRequestStore,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanStore,
)

EVOLUTION_REVALIDATION_ROLLBACK_OUTCOME_POLICY = (
    "evolution-revalidation-rollback-outcome-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRollbackOutcome(_StrictModel):
    """Immutable historical fact; it is not promotion or learning authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollback-outcome-v1"] = (
        EVOLUTION_REVALIDATION_ROLLBACK_OUTCOME_POLICY
    )
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    status: Literal["rolled_back"] = "rolled_back"
    rollback_receipt_id: str = Field(pattern=r"^evrerollbackexec_[0-9a-f]{24}$")
    rollback_receipt_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    runtime_contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    runtime_contract_sha256: str = Field(pattern=_SHA256_RE)
    prior_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    prior_input_sha256: str = Field(pattern=_SHA256_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    experiment_authority_id: str = Field(pattern=r"^evxauth_[0-9a-f]{24}$")
    experiment_authority_sha256: str = Field(pattern=_SHA256_RE)
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
    proposal_id: str = Field(pattern=r"^evp_[0-9a-f]{24}$")
    proposal_kind: Literal["knowledge", "profile", "prompt", "tool", "test", "code"]
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    candidate_slot_id: str = Field(min_length=1, max_length=160)
    candidate_slot_sha256: str = Field(pattern=_SHA256_RE)
    baseline_slot_id: str = Field(min_length=1, max_length=160)
    baseline_slot_sha256: str = Field(pattern=_SHA256_RE)
    breach_reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    rollback_fact_verified: Literal[True] = True
    proposal_binding_verified: Literal[True] = True
    outcome_recorded: Literal[True] = True
    promoted: Literal[False] = False
    superseded: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollback Outcome workspace 必须 canonical。")
        if self.breach_reasons != tuple(sorted(set(self.breach_reasons))):
            raise ValueError("Rollback Outcome breach reasons 必须有序且唯一。")
        _aware(self.recorded_at)
        core = self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"})
        digest = _digest(core)
        if not hmac.compare_digest(self.outcome_sha256, digest) or self.outcome_id != (
            f"evrerollbackout_{digest[:24]}"
        ):
            raise ValueError("Rollback Outcome identity 不一致。")
        return self


class EvolutionRevalidationRollbackOutcomeView(_StrictModel):
    outcome: EvolutionRevalidationRollbackOutcome
    durable_dependencies_valid: bool
    rollback_fact_authority: bool
    proposal_binding_valid: bool
    active_baseline_authority: bool
    outcome_authority: bool
    long_term_metrics_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_dependencies_valid
            and self.rollback_fact_authority
            and self.proposal_binding_valid
        )
        if self.outcome_authority is not expected:
            raise ValueError("Rollback Outcome authority 投影不一致。")
        if self.active_baseline_authority and not self.rollback_fact_authority:
            raise ValueError("Rollback Outcome active baseline 超出 rollback fact。")
        return self


class EvolutionRevalidationRollbackOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRollbackOutcomeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_request(
        self, request_id: str
    ) -> EvolutionRevalidationRollbackOutcome | None:
        if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", str(request_id)) is None:
            raise ValueError("Rollback Request ID 格式无效。")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT outcome_json FROM evolution_revalidation_rollback_outcomes "
                        "WHERE request_id = ?",
                        (request_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_outcome(row["outcome_json"])
        except EvolutionRevalidationRollbackOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_store_corrupt",
                "Rollback Outcome 损坏或无法读取。",
            ) from exc

    async def record(
        self, outcome: EvolutionRevalidationRollbackOutcome
    ) -> EvolutionRevalidationRollbackOutcome:
        try:
            item = EvolutionRevalidationRollbackOutcome.model_validate_json(
                outcome.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_artifact_invalid", "Rollback Outcome artifact 无效。"
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_artifact_oversized",
                "Rollback Outcome 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                existing = await (
                    await db.execute(
                        "SELECT outcome_json FROM evolution_revalidation_rollback_outcomes "
                        "WHERE request_id = ?",
                        (item.request_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_outcome(existing["outcome_json"])
                    await db.rollback()
                    if not _same_binding(restored, item):
                        raise EvolutionRevalidationRollbackOutcomeError(
                            "rollback_outcome_conflict",
                            "同一 Rollback Request 已绑定不同 Outcome。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_rollback_outcomes "
                    "(outcome_id, outcome_sha256, request_id, rollback_receipt_id, "
                    "workbench_session_id, workbench_proposal_id, status, outcome_json, "
                    "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.outcome_id,
                        item.outcome_sha256,
                        item.request_id,
                        item.rollback_receipt_id,
                        item.workbench_session_id,
                        item.workbench_proposal_id,
                        item.status,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationRollbackOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_store_error", "Rollback Outcome 无法持久化。"
            ) from exc
        restored = await self.get_by_request(item.request_id)
        assert restored is not None
        return restored


class EvolutionRevalidationRollbackOutcomeService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        execution_service: EvolutionRevalidationRollbackExecutionService,
        request_store: EvolutionRevalidationRollbackRequestStore,
        plan_store: EvolutionRevalidationRolloutPlanStore,
        promotion_input_store: EvolutionRevalidationPromotionInputStore,
        experiment_store: EvolutionExperimentContractStore,
        store: EvolutionRevalidationRollbackOutcomeStore,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.execution_service = execution_service
        self.request_store = request_store
        self.plan_store = plan_store
        self.promotion_input_store = promotion_input_store
        self.experiment_store = experiment_store
        self.store = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def record(self, *, request_id: str) -> EvolutionRevalidationRollbackOutcomeView:
        normalized = str(request_id or "").strip()
        if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", normalized) is None:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_request_id_invalid", "Rollback Request ID 格式无效。"
            )
        existing = await self.store.get_by_request(normalized)
        if existing is not None:
            view = await self._view(existing)
            if not view.outcome_authority:
                raise EvolutionRevalidationRollbackOutcomeError(
                    "rollback_outcome_stale",
                    "既有 Rollback Outcome 的 source 已变化，authority 当前无效。",
                )
            return view
        execution, request, plan, promotion_input, experiment = await self._sources(
            normalized
        )
        source = experiment.contract.source
        prior = promotion_input.prior_input
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_OUTCOME_POLICY,
            "workspace_root": str(self.workspace_root),
            "status": "rolled_back",
            "rollback_receipt_id": execution.receipt_id,
            "rollback_receipt_sha256": execution.receipt_sha256,
            "request_id": request.request_id,
            "request_sha256": request.request_sha256,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "promotion_input_id": promotion_input.input_id,
            "promotion_input_sha256": promotion_input.input_sha256,
            "runtime_contract_id": promotion_input.contract_id,
            "runtime_contract_sha256": promotion_input.contract_sha256,
            "prior_input_id": prior.input_id,
            "prior_input_sha256": prior.input_sha256,
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
            "candidate_slot_id": execution.candidate_slot.slot_id,
            "candidate_slot_sha256": execution.candidate_slot.slot_sha256,
            "baseline_slot_id": execution.baseline_slot.slot_id,
            "baseline_slot_sha256": execution.baseline_slot.slot_sha256,
            "breach_reasons": list(request.breach_reasons),
            "rollback_fact_verified": True,
            "proposal_binding_verified": True,
            "outcome_recorded": True,
            "promoted": False,
            "superseded": False,
            "long_term_metrics_recorded": False,
            "learning_authority": False,
            "promotion_authority": False,
            "recorded_at": _aware(self.now()).isoformat(),
        }
        digest = _digest(core)
        outcome = EvolutionRevalidationRollbackOutcome.model_validate(
            {
                **core,
                "outcome_id": f"evrerollbackout_{digest[:24]}",
                "outcome_sha256": digest,
            }
        )
        recorded = await self.store.record(outcome)
        view = await self._view(recorded)
        if not view.outcome_authority:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_post_commit_stale",
                "Rollback Outcome 落盘后 source 已变化，未签发 Outcome authority。",
            )
        return view

    async def inspect(
        self, *, request_id: str
    ) -> EvolutionRevalidationRollbackOutcomeView:
        item = await self.store.get_by_request(str(request_id or "").strip())
        if item is None:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_not_found", "Rollback Outcome 不存在。"
            )
        return await self._view(item)

    async def _sources(self, request_id: str):
        try:
            execution_view = await self.execution_service.inspect(request_id=request_id)
            request = await self.request_store.get(request_id)
            if request is None:
                raise ValueError("request missing")
            plan = await self.plan_store.get(request.plan_id)
            if plan is None:
                raise ValueError("plan missing")
            promotion_input = await self.promotion_input_store.get(plan.contract_id)
            if promotion_input is None:
                raise ValueError("promotion input missing")
            experiment = await self.experiment_store.get(
                self.workspace_root, promotion_input.prior_input.experiment_contract_id
            )
        except (
            EvolutionExperimentContractStoreError,
            EvolutionRevalidationPromotionInputError,
            EvolutionRevalidationRollbackExecutionError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_source_invalid",
                "Rollback Outcome 的 durable source 不完整或已损坏。",
            ) from exc
        if experiment is None or not self._matches(
            execution_view.receipt, request, plan, promotion_input, experiment
        ):
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_source_mismatch",
                "Rollback、Promotion Input 与 Proposal/Contract 绑定不一致。",
            )
        if not execution_view.rollback_fact_authority:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_fact_stale", "Rollback fact 当前无法验证。"
            )
        return execution_view.receipt, request, plan, promotion_input, experiment

    def _matches(
        self,
        execution: EvolutionRevalidationRollbackExecutionReceipt,
        request: EvolutionRevalidationRollbackRequest,
        plan: EvolutionRevalidationRolloutPlan,
        promotion_input: EvolutionRevalidationPromotionInput,
        experiment: EvolutionExperimentContractAuthority,
    ) -> bool:
        prior = promotion_input.prior_input
        source = experiment.contract.source
        return bool(
            execution.workspace_root == str(self.workspace_root)
            and request.workspace_root == str(self.workspace_root)
            and plan.workspace_root == str(self.workspace_root)
            and execution.request_id == request.request_id
            and execution.request_sha256 == request.request_sha256
            and execution.plan_id == plan.plan_id
            and execution.plan_sha256 == plan.plan_sha256
            and request.promotion_input_id == promotion_input.input_id
            and request.promotion_input_sha256 == promotion_input.input_sha256
            and plan.contract_id == promotion_input.contract_id
            and plan.contract_sha256 == promotion_input.contract_sha256
            and prior.input_id == promotion_input.prior_input_id
            and prior.input_sha256 == promotion_input.prior_input_sha256
            and prior.rollback.plan_sha256 == request.rollback_plan_sha256
            and prior.experiment_contract_id == experiment.contract_id
            and prior.experiment_contract_sha256 == experiment.contract_manifest_sha256
            and plan.candidate_id == prior.candidate_id == source.candidate_id
            and plan.candidate_revision == prior.candidate_revision == source.candidate_revision
            and prior.candidate_sha256 == source.candidate_sha256
        )

    async def _view(
        self, item: EvolutionRevalidationRollbackOutcome
    ) -> EvolutionRevalidationRollbackOutcomeView:
        durable = rollback = proposal = active = False
        try:
            execution, request, plan, promotion_input, experiment = await self._sources(
                item.request_id
            )
            source = experiment.contract.source
            durable = bool(
                item.rollback_receipt_id == execution.receipt_id
                and item.rollback_receipt_sha256 == execution.receipt_sha256
                and item.request_sha256 == request.request_sha256
                and item.plan_sha256 == plan.plan_sha256
                and item.promotion_input_id == promotion_input.input_id
                and item.promotion_input_sha256 == promotion_input.input_sha256
                and item.prior_input_id == promotion_input.prior_input.input_id
                and item.prior_input_sha256 == promotion_input.prior_input.input_sha256
            )
            rollback = durable
            proposal = bool(
                durable
                and item.experiment_contract_id == experiment.contract_id
                and item.experiment_contract_sha256
                == experiment.contract_manifest_sha256
                and item.experiment_authority_id == experiment.authority_id
                and item.experiment_authority_sha256 == experiment.authority_sha256
                and item.workbench_session_id == source.session_id
                and item.workbench_proposal_id == source.workbench_proposal_id
                and item.proposal_id == source.proposal_id
                and item.proposal_kind == source.proposal_kind
                and item.candidate_id == source.candidate_id
                and item.candidate_revision == source.candidate_revision
                and item.candidate_sha256 == source.candidate_sha256
            )
            execution_view = await self.execution_service.inspect(request_id=item.request_id)
            active = execution_view.active_baseline_authority
        except EvolutionRevalidationRollbackOutcomeError:
            pass
        authority = bool(durable and rollback and proposal)
        return EvolutionRevalidationRollbackOutcomeView(
            outcome=item,
            durable_dependencies_valid=durable,
            rollback_fact_authority=rollback,
            proposal_binding_valid=proposal,
            active_baseline_authority=active,
            outcome_authority=authority,
            long_term_metrics_authority=False,
            promotion_authority=False,
        )


async def _require_dependencies(
    db: aiosqlite.Connection, item: EvolutionRevalidationRollbackOutcome
) -> None:
    checks = (
        (
            "SELECT receipt_sha256 FROM evolution_revalidation_rollback_executions "
            "WHERE receipt_id = ?",
            item.rollback_receipt_id,
            "receipt_sha256",
            item.rollback_receipt_sha256,
        ),
        (
            "SELECT request_sha256 FROM evolution_revalidation_rollback_requests "
            "WHERE request_id = ?",
            item.request_id,
            "request_sha256",
            item.request_sha256,
        ),
        (
            "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans WHERE plan_id = ?",
            item.plan_id,
            "plan_sha256",
            item.plan_sha256,
        ),
    )
    for query, identity, column, expected in checks:
        row = await (await db.execute(query, (identity,))).fetchone()
        if row is None or row[column] != expected:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_dependency_mismatch",
                "Rollback Outcome dependency 不存在或摘要不一致。",
            )
    promotion_row = await (
        await db.execute(
            "SELECT input_sha256, input_json FROM evolution_revalidation_promotion_inputs "
            "WHERE input_id = ?",
            (item.promotion_input_id,),
        )
    ).fetchone()
    prior_row = await (
        await db.execute(
            "SELECT input_sha256, input_json FROM evolution_promotion_package_inputs "
            "WHERE input_id = ?",
            (item.prior_input_id,),
        )
    ).fetchone()
    try:
        promotion = EvolutionRevalidationPromotionInput.model_validate_json(
            promotion_row["input_json"] if promotion_row is not None else ""
        )
        prior = EvolutionPromotionPackageInput.model_validate_json(
            prior_row["input_json"] if prior_row is not None else ""
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationRollbackOutcomeError(
            "rollback_outcome_promotion_dependency_corrupt",
            "Rollback Outcome 的 Promotion Input dependency 损坏。",
        ) from exc
    if not (
        promotion_row is not None
        and prior_row is not None
        and promotion_row["input_sha256"] == item.promotion_input_sha256
        and prior_row["input_sha256"] == item.prior_input_sha256
        and promotion.input_id == item.promotion_input_id
        and promotion.input_sha256 == item.promotion_input_sha256
        and promotion.prior_input_id == prior.input_id == item.prior_input_id
        and promotion.prior_input_sha256 == prior.input_sha256 == item.prior_input_sha256
        and promotion.prior_input == prior
    ):
        raise EvolutionRevalidationRollbackOutcomeError(
            "rollback_outcome_promotion_dependency_mismatch",
            "Rollback Outcome 的 Promotion Input dependency 不一致。",
        )
    contract = await (
        await db.execute(
            "SELECT manifest_sha256, authority_id, authority_sha256, "
            "source_session_id, workbench_proposal_id, authority_json "
            "FROM evolution_experiment_contracts "
            "WHERE workspace_root = ? AND contract_id = ?",
            (item.workspace_root, item.experiment_contract_id),
        )
    ).fetchone()
    try:
        authority = EvolutionExperimentContractAuthority.model_validate_json(
            contract["authority_json"] if contract is not None else ""
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationRollbackOutcomeError(
            "rollback_outcome_proposal_dependency_corrupt",
            "Rollback Outcome 的 Proposal/Contract dependency 损坏。",
        ) from exc
    source = authority.contract.source
    if contract is None or not (
        contract["manifest_sha256"] == authority.contract_manifest_sha256
        == item.experiment_contract_sha256
        and contract["authority_id"] == authority.authority_id
        == item.experiment_authority_id
        and contract["authority_sha256"] == authority.authority_sha256
        == item.experiment_authority_sha256
        and contract["source_session_id"] == source.session_id
        == item.workbench_session_id
        and contract["workbench_proposal_id"] == source.workbench_proposal_id
        == item.workbench_proposal_id
        and source.proposal_id == item.proposal_id
        and source.proposal_kind == item.proposal_kind
        and source.candidate_id == item.candidate_id
        and source.candidate_revision == item.candidate_revision
        and source.candidate_sha256 == item.candidate_sha256
    ):
        raise EvolutionRevalidationRollbackOutcomeError(
            "rollback_outcome_proposal_dependency_mismatch",
            "Rollback Outcome 的 Proposal/Contract dependency 不一致。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollback_outcomes ("
        "outcome_id TEXT PRIMARY KEY, outcome_sha256 TEXT NOT NULL UNIQUE, "
        "request_id TEXT NOT NULL UNIQUE, rollback_receipt_id TEXT NOT NULL UNIQUE, "
        "workbench_session_id TEXT NOT NULL, workbench_proposal_id TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK(status = 'rolled_back'), outcome_json TEXT NOT NULL, "
        "recorded_at TEXT NOT NULL)"
    )


def render_revalidation_rollback_outcome(
    view: EvolutionRevalidationRollbackOutcomeView,
) -> str:
    item = view.outcome
    state = "可验证" if view.outcome_authority else "证据已失效"
    active = "Baseline 当前生效" if view.active_baseline_authority else "历史回滚事实"
    return "\n".join(
        (
            "## Evolution 回滚 Outcome",
            "",
            f"- 状态：`{item.status}` · `{state}` · {active}",
            f"- Outcome：`{item.outcome_id}`",
            f"- Rollback Receipt：`{item.rollback_receipt_id}`",
            f"- Workbench Proposal：`{item.workbench_proposal_id}`",
            f"- Experiment Contract：`{item.experiment_contract_id}`",
            f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
            f"- Breach：{', '.join(item.breach_reasons)}",
            "- 长期指标：尚未记录；不得用于 policy learning",
            "- 权限：不授予 promotion authority",
        )
    )


def _restore_outcome(encoded: str) -> EvolutionRevalidationRollbackOutcome:
    if not isinstance(encoded, str) or len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationRollbackOutcomeError(
            "rollback_outcome_artifact_corrupt", "Rollback Outcome artifact 损坏。"
        )
    try:
        return EvolutionRevalidationRollbackOutcome.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationRollbackOutcomeError(
            "rollback_outcome_artifact_corrupt", "Rollback Outcome artifact 损坏。"
        ) from exc


def _same_binding(
    left: EvolutionRevalidationRollbackOutcome,
    right: EvolutionRevalidationRollbackOutcome,
) -> bool:
    excluded = {"outcome_id", "outcome_sha256", "recorded_at"}
    return left.model_dump(mode="json", exclude=excluded) == right.model_dump(
        mode="json", exclude=excluded
    )


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Rollback Outcome 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
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
    "EVOLUTION_REVALIDATION_ROLLBACK_OUTCOME_POLICY",
    "EvolutionRevalidationRollbackOutcome",
    "EvolutionRevalidationRollbackOutcomeError",
    "EvolutionRevalidationRollbackOutcomeService",
    "EvolutionRevalidationRollbackOutcomeStore",
    "EvolutionRevalidationRollbackOutcomeView",
    "render_revalidation_rollback_outcome",
]
