"""Coverage-complete Final Evaluation authority for Fresh revalidation."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionReceipt,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContract,
)

EVOLUTION_REVALIDATION_FINAL_EVALUATION_POLICY = (
    "evolution-revalidation-final-evaluation-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationFinalInterventionalEvidence(_StrictModel):
    cohort_receipt_id: str = Field(pattern=r"^evrevalcohort_[0-9a-f]{24}$")
    cohort_receipt_sha256: str = Field(pattern=_SHA256_RE)
    comparison_id: str = Field(pattern=_SHA256_RE)
    comparison_receipt_sha256: str = Field(pattern=_SHA256_RE)
    attribution: EvolutionFailureAttributionReceipt


class EvolutionRevalidationFinalAdversarialEvidence(_StrictModel):
    order: int = Field(ge=1, le=3)
    platform: Literal["linux", "macos", "windows"]
    cohort_receipt_id: str = Field(pattern=r"^evrevaladvcohort_[0-9a-f]{24}$")
    cohort_receipt_sha256: str = Field(pattern=_SHA256_RE)
    comparison_id: str = Field(pattern=_SHA256_RE)
    comparison_receipt_sha256: str = Field(pattern=_SHA256_RE)
    attribution: EvolutionFailureAttributionReceipt


class EvolutionRevalidationFinalEvaluationReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-final-evaluation-v1"] = (
        EVOLUTION_REVALIDATION_FINAL_EVALUATION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrevalfinal_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract: EvolutionRevalidationRuntimeContract
    matrix_id: str = Field(pattern=r"^evrevaladvmatrix_[0-9a-f]{24}$")
    matrix_sha256: str = Field(pattern=_SHA256_RE)
    interventional: EvolutionRevalidationFinalInterventionalEvidence
    adversarial: tuple[EvolutionRevalidationFinalAdversarialEvidence, ...] = Field(
        min_length=1, max_length=3
    )
    comparison_ids: tuple[str, ...] = Field(min_length=2, max_length=4)
    attribution_ids: tuple[str, ...] = Field(min_length=2, max_length=4)
    any_candidate_fault: bool
    any_requires_rerun: bool
    all_reflection_eligible: bool
    source_current_at_issue: Literal[True] = True
    evaluation_complete: Literal[True] = True
    reapproval_eligible: bool
    candidate_acceptance_decided: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != self.contract.workspace_root:
            raise ValueError("Fresh Final Evaluation workspace 与 Contract 不一致。")
        if tuple(item.order for item in self.adversarial) != tuple(
            range(1, len(self.adversarial) + 1)
        ) or tuple(item.platform for item in self.adversarial) != (
            self.contract.required_platforms
        ):
            raise ValueError("Fresh Final Evaluation platform 覆盖不完整。")
        evidence = (self.interventional,) + self.adversarial
        attributions = tuple(item.attribution for item in evidence)
        if self.comparison_ids != tuple(item.comparison_id for item in evidence):
            raise ValueError("Fresh Final Evaluation comparison 投影不一致。")
        if self.attribution_ids != tuple(
            item.attribution_id for item in attributions
        ):
            raise ValueError("Fresh Final Evaluation attribution 投影不一致。")
        if any(
            item.comparison_id != item.attribution.comparison_id
            or item.comparison_receipt_sha256
            != item.attribution.comparison_receipt_sha256
            for item in evidence
        ):
            raise ValueError("Fresh Final Evaluation H5c 与 attribution 不一致。")
        if self.any_candidate_fault is not any(
            item.candidate_fault for item in attributions
        ) or self.any_requires_rerun is not any(
            item.requires_rerun for item in attributions
        ) or self.all_reflection_eligible is not all(
            item.reflection_eligible for item in attributions
        ):
            raise ValueError("Fresh Final Evaluation attribution 聚合不一致。")
        expected_eligible = (
            self.all_reflection_eligible
            and not self.any_candidate_fault
            and not self.any_requires_rerun
        )
        if self.reapproval_eligible is not expected_eligible:
            raise ValueError("Fresh Final Evaluation reapproval eligibility 不一致。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Fresh Final Evaluation created_at 必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Fresh Final Evaluation digest 不一致。")
        if self.receipt_id != f"evrevalfinal_{digest[:24]}":
            raise ValueError("Fresh Final Evaluation identity 不一致。")
        return self


class EvolutionRevalidationFinalEvaluationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationFinalEvaluationStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get(self, contract_id: str):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT receipt_json FROM evolution_revalidation_final_evaluations "
                "WHERE contract_id = ?", (contract_id,)
            )).fetchone()
        return None if row is None else (
            EvolutionRevalidationFinalEvaluationReceipt.model_validate_json(
                row["receipt_json"]
            )
        )

    async def record(self, receipt):
        item = EvolutionRevalidationFinalEvaluationReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_dependencies(db, item)
            existing = await (await db.execute(
                "SELECT receipt_json FROM evolution_revalidation_final_evaluations "
                "WHERE contract_id = ?", (item.contract.contract_id,)
            )).fetchone()
            if existing is not None:
                restored = EvolutionRevalidationFinalEvaluationReceipt.model_validate_json(
                    existing["receipt_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationFinalEvaluationError(
                        "fresh_final_evaluation_conflict",
                        "同一 Runtime Contract 已绑定不同 Fresh Final Evaluation。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_final_evaluations "
                "(receipt_id, receipt_sha256, contract_id, receipt_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.receipt_id, item.receipt_sha256, item.contract.contract_id,
                 item.model_dump_json(), item.created_at),
            )
            await db.commit()
        return item


async def _require_dependencies(db, item):
    contract = await (await db.execute(
        "SELECT contract_sha256 FROM evolution_revalidation_runtime_contracts "
        "WHERE contract_id = ?", (item.contract.contract_id,)
    )).fetchone()
    matrix = await (await db.execute(
        "SELECT matrix_sha256 FROM evolution_revalidation_adversarial_matrices "
        "WHERE contract_id = ?", (item.contract.contract_id,)
    )).fetchone()
    interventional = await (await db.execute(
        "SELECT receipt_sha256 FROM evolution_revalidation_interventional_cohorts "
        "WHERE contract_id = ?", (item.contract.contract_id,)
    )).fetchone()
    if (
        contract is None
        or contract["contract_sha256"] != item.contract.contract_sha256
        or matrix is None
        or matrix["matrix_sha256"] != item.matrix_sha256
        or interventional is None
        or interventional["receipt_sha256"]
        != item.interventional.cohort_receipt_sha256
    ):
        await db.rollback()
        raise EvolutionRevalidationFinalEvaluationError(
            "fresh_final_evaluation_dependency_mismatch",
            "Fresh Final Evaluation 的 Contract、matrix 或 Interventional 依赖不一致。",
        )
    for evidence in (item.interventional,) + item.adversarial:
        attribution = await (await db.execute(
            "SELECT attribution_sha256 FROM evolution_failure_attributions "
            "WHERE comparison_id = ?", (evidence.comparison_id,)
        )).fetchone()
        if attribution is None or (
            attribution["attribution_sha256"]
            != evidence.attribution.attribution_sha256
        ):
            await db.rollback()
            raise EvolutionRevalidationFinalEvaluationError(
                "fresh_final_evaluation_attribution_dependency_mismatch",
                "Fresh Final Evaluation attribution 持久化依赖不一致。",
            )
    for evidence in item.adversarial:
        cohort = await (await db.execute(
            "SELECT receipt_sha256 FROM evolution_revalidation_adversarial_cohorts "
            "WHERE contract_id = ? AND platform = ?",
            (item.contract.contract_id, evidence.platform),
        )).fetchone()
        if cohort is None or cohort["receipt_sha256"] != evidence.cohort_receipt_sha256:
            await db.rollback()
            raise EvolutionRevalidationFinalEvaluationError(
                "fresh_final_evaluation_adversarial_dependency_mismatch",
                f"平台 {evidence.platform} 的 cohort 持久化依赖不一致。",
            )


class EvolutionRevalidationFinalEvaluationExecutor:
    def __init__(self, *, workspace_root, contract_service, matrix_service,
                 interventional_cohort_store, adversarial_cohort_store,
                 interventional_comparison_executor, adversarial_comparison_executor,
                 interventional_attribution_executor, adversarial_attribution_executor,
                 receipt_store):
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service = contract_service
        self.matrix_service = matrix_service
        self.interventional_cohort_store = interventional_cohort_store
        self.adversarial_cohort_store = adversarial_cohort_store
        self.interventional_comparison_executor = interventional_comparison_executor
        self.adversarial_comparison_executor = adversarial_comparison_executor
        self.interventional_attribution_executor = interventional_attribution_executor
        self.adversarial_attribution_executor = adversarial_attribution_executor
        self.receipt_store = receipt_store

    async def execute(self, *, contract_id: str):
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationFinalEvaluationError(
                "fresh_final_evaluation_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        contract = view.contract
        matrix = await self.matrix_service.inspect(contract_id=contract_id)
        if not matrix.matrix_complete:
            raise EvolutionRevalidationFinalEvaluationError(
                "fresh_final_evaluation_matrix_incomplete",
                "Fresh Adversarial required-platform matrix 尚未完成。",
            )
        interventional_cohort = await self.interventional_cohort_store.get_by_contract(
            contract_id
        )
        if interventional_cohort is None:
            raise EvolutionRevalidationFinalEvaluationError(
                "fresh_final_evaluation_interventional_missing",
                "Fresh Interventional cohort 尚未完成。",
            )
        interventional_comparison = await self.interventional_comparison_executor.execute(
            contract_id=contract_id
        )
        interventional_attribution = (
            await self.interventional_attribution_executor.execute(
                contract_id=contract_id
            )
        )
        adversarial_comparisons = await self.adversarial_comparison_executor.execute(
            contract_id=contract_id
        )
        adversarial_attributions = await self.adversarial_attribution_executor.execute(
            contract_id=contract_id
        )
        if not (
            len(matrix.lanes)
            == len(adversarial_comparisons)
            == len(adversarial_attributions)
        ):
            raise EvolutionRevalidationFinalEvaluationError(
                "fresh_final_evaluation_platform_coverage_incomplete",
                "Fresh Adversarial evidence 未覆盖全部 required platforms。",
            )
        adversarial = []
        for order, (lane, comparison, attribution) in enumerate(zip(
            matrix.lanes, adversarial_comparisons, adversarial_attributions, strict=True
        ), start=1):
            cohort = await self.adversarial_cohort_store.get(contract_id, lane.platform)
            if cohort is None or (
                lane.cohort_receipt_id != cohort.receipt_id
                or lane.cohort_receipt_sha256 != cohort.receipt_sha256
            ):
                raise EvolutionRevalidationFinalEvaluationError(
                    "fresh_final_evaluation_adversarial_mismatch",
                    f"平台 {lane.platform} 的 matrix/cohort evidence 不一致。",
                )
            adversarial.append(EvolutionRevalidationFinalAdversarialEvidence(
                order=order,
                platform=lane.platform,
                cohort_receipt_id=cohort.receipt_id,
                cohort_receipt_sha256=cohort.receipt_sha256,
                comparison_id=comparison.id,
                comparison_receipt_sha256=comparison.receipt_sha256,
                attribution=attribution,
            ))
        evidence = EvolutionRevalidationFinalInterventionalEvidence(
            cohort_receipt_id=interventional_cohort.receipt_id,
            cohort_receipt_sha256=interventional_cohort.receipt_sha256,
            comparison_id=interventional_comparison.id,
            comparison_receipt_sha256=interventional_comparison.receipt_sha256,
            attribution=interventional_attribution,
        )
        attributions = (interventional_attribution,) + adversarial_attributions
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_FINAL_EVALUATION_POLICY,
            "workspace_root": contract.workspace_root,
            "contract": contract.model_dump(mode="json"),
            "matrix_id": matrix.matrix_id,
            "matrix_sha256": matrix.matrix_sha256,
            "interventional": evidence.model_dump(mode="json"),
            "adversarial": [item.model_dump(mode="json") for item in adversarial],
            "comparison_ids": [evidence.comparison_id] + [
                item.comparison_id for item in adversarial
            ],
            "attribution_ids": [item.attribution_id for item in attributions],
            "any_candidate_fault": any(item.candidate_fault for item in attributions),
            "any_requires_rerun": any(item.requires_rerun for item in attributions),
            "all_reflection_eligible": all(
                item.reflection_eligible for item in attributions
            ),
            "source_current_at_issue": True,
            "evaluation_complete": True,
            "reapproval_eligible": all(
                item.reflection_eligible for item in attributions
            ) and not any(item.candidate_fault or item.requires_rerun for item in attributions),
            "candidate_acceptance_decided": False,
            "promotion_authority": False,
            "created_at": max(item.created_at for item in attributions),
        }
        digest = _sha256_payload(payload)
        receipt = EvolutionRevalidationFinalEvaluationReceipt.model_validate({
            **payload,
            "receipt_id": f"evrevalfinal_{digest[:24]}",
            "receipt_sha256": digest,
        })
        return await self.receipt_store.record(receipt)


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_final_evaluations ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_FINAL_EVALUATION_POLICY",
    "EvolutionRevalidationFinalAdversarialEvidence",
    "EvolutionRevalidationFinalEvaluationError",
    "EvolutionRevalidationFinalEvaluationExecutor",
    "EvolutionRevalidationFinalEvaluationReceipt",
    "EvolutionRevalidationFinalEvaluationStore",
    "EvolutionRevalidationFinalInterventionalEvidence",
]
