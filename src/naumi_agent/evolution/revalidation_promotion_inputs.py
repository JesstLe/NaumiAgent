"""Versioned Promotion Input built only from Fresh reapproval authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.revalidation_final_evaluations import (
    EvolutionRevalidationFinalEvaluationStore,
)
from naumi_agent.evolution.revalidation_reapproval_authorities import (
    EvolutionRevalidationReapprovalAuthorityError,
    EvolutionRevalidationReapprovalAuthorityService,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanStore,
)

EVOLUTION_REVALIDATION_PROMOTION_INPUT_POLICY = (
    "evolution-revalidation-promotion-input-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationPromotionInput(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-promotion-input-v1"] = (
        EVOLUTION_REVALIDATION_PROMOTION_INPUT_POLICY
    )
    input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    input_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evrevalfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    reapproval_authority_id: str = Field(pattern=r"^evreapproval_[0-9a-f]{24}$")
    reapproval_authority_sha256: str = Field(pattern=_SHA256_RE)
    prior_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    prior_input_sha256: str = Field(pattern=_SHA256_RE)
    prior_input: EvolutionPromotionPackageInput
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1, max_length=3
    )
    prior_final_invalidated: Literal[True] = True
    prior_decision_reusable: Literal[False] = False
    prior_approval_reusable: Literal[False] = False
    prior_signature_reusable: Literal[False] = False
    patch_and_rollback_carried_forward: Literal[True] = True
    source_current_at_issue: Literal[True] = True
    input_complete: Literal[True] = True
    approval_requirement_ready: Literal[True] = True
    approval_decided: Literal[False] = False
    promotion_authority: Literal[False] = False
    contains_source_code: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    llm_generated: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        prior = self.prior_input
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Promotion Input workspace 必须 canonical。")
        if self.required_platforms != tuple(dict.fromkeys(self.required_platforms)):
            raise ValueError("Fresh Promotion Input required platforms 不得重复。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Fresh Promotion Input created_at 必须包含 offset。")
        if not (
            self.workspace_root == prior.workspace_root
            and self.candidate_id == prior.candidate_id
            and self.candidate_revision == prior.candidate_revision
            and self.risk_level == prior.risk_level
            and self.prior_input_id == prior.input_id
            and self.prior_input_sha256 == prior.input_sha256
            and self.required_platforms == prior.required_platforms
        ):
            raise ValueError("Fresh Promotion Input 与 prior input 投影不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"input_id", "input_sha256"})
        )
        if not hmac.compare_digest(self.input_sha256, digest):
            raise ValueError("Fresh Promotion Input digest 不一致。")
        if self.input_id != f"evrevalpromoin_{digest[:24]}":
            raise ValueError("Fresh Promotion Input identity 不一致。")
        return self


class EvolutionRevalidationPromotionInputError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPromotionInputStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get(
        self, contract_id: str
    ) -> EvolutionRevalidationPromotionInput | None:
        if (
            not isinstance(contract_id, str)
            or re.fullmatch(r"evrevalruntime_[0-9a-f]{24}", contract_id) is None
        ):
            raise ValueError("Fresh Runtime Contract id 格式无效。")
        if not self._db_path.exists():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT input_json FROM evolution_revalidation_promotion_inputs "
                        "WHERE contract_id = ?",
                        (contract_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            encoded = str(row["input_json"])
            if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
                raise ValueError("Fresh Promotion Input artifact 过大。")
            return EvolutionRevalidationPromotionInput.model_validate_json(encoded)
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_store_corrupt",
                "Fresh Promotion Input 损坏或无法读取。",
            ) from exc

    async def record(
        self, artifact: EvolutionRevalidationPromotionInput
    ) -> EvolutionRevalidationPromotionInput:
        try:
            item = EvolutionRevalidationPromotionInput.model_validate_json(
                artifact.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_artifact_invalid",
                "Fresh Promotion Input artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_oversized",
                "Fresh Promotion Input 超过 2 MiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                existing = await (
                    await db.execute(
                        "SELECT input_json FROM "
                        "evolution_revalidation_promotion_inputs "
                        "WHERE contract_id = ?",
                        (item.contract_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = EvolutionRevalidationPromotionInput.model_validate_json(
                        existing["input_json"]
                    )
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationPromotionInputError(
                            "fresh_promotion_input_conflict",
                            "同一 Runtime Contract 已绑定不同 Fresh Promotion Input。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_promotion_inputs "
                    "(input_id, input_sha256, contract_id, input_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item.input_id,
                        item.input_sha256,
                        item.contract_id,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationPromotionInputError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_store_error",
                "Fresh Promotion Input 无法持久化。",
            ) from exc
        return item


class EvolutionRevalidationPromotionInputService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_service: EvolutionRevalidationRuntimeContractService,
        validation_plan_store: EvolutionRevalidationValidationPlanStore,
        final_store: EvolutionRevalidationFinalEvaluationStore,
        reapproval_service: EvolutionRevalidationReapprovalAuthorityService,
        prior_input_store: EvolutionPromotionPackageInputStore,
        store: EvolutionRevalidationPromotionInputStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service: EvolutionRevalidationRuntimeContractService = contract_service
        self.validation_plan_store: EvolutionRevalidationValidationPlanStore = (
            validation_plan_store
        )
        self.final_store: EvolutionRevalidationFinalEvaluationStore = final_store
        self.reapproval_service: EvolutionRevalidationReapprovalAuthorityService = (
            reapproval_service
        )
        self.prior_input_store: EvolutionPromotionPackageInputStore = prior_input_store
        self.store: EvolutionRevalidationPromotionInputStore = store

    async def issue(
        self, *, contract_id: str
    ) -> EvolutionRevalidationPromotionInput:
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        contract = view.contract
        plan = await self.validation_plan_store.get(contract.validation_plan_id)
        final = await self.final_store.get(contract_id)
        if plan is None or final is None:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_authority_missing",
                "Fresh Promotion Input 缺少 Validation Plan 或 Final Evaluation。",
            )
        try:
            authority = await self.reapproval_service.issue(contract_id=contract_id)
        except EvolutionRevalidationReapprovalAuthorityError as exc:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_reapproval_blocked",
                "Fresh Promotion Input 未获得重新审批准入。",
            ) from exc
        prior_view = await self.prior_input_store.get(plan.promotion_input_id)
        if prior_view is None:
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_prior_missing",
                "原 Promotion Input 不存在。",
            )
        prior = prior_view.package_input
        if not (
            plan.validation_plan_id == contract.validation_plan_id
            and plan.validation_plan_sha256 == contract.validation_plan_sha256
            and plan.promotion_input_id == prior.input_id
            and plan.promotion_input_sha256 == prior.input_sha256
            and final.contract == contract
            and authority.contract_id == contract.contract_id
            and authority.contract_sha256 == contract.contract_sha256
            and authority.validation_plan_id == plan.validation_plan_id
            and authority.validation_plan_sha256 == plan.validation_plan_sha256
            and authority.candidate_id == contract.candidate_id
            and authority.candidate_revision == contract.candidate_revision
            and authority.final_evaluation_id == final.receipt_id
            and authority.final_evaluation_sha256 == final.receipt_sha256
            and authority.required_platforms == contract.required_platforms
            and prior.candidate_id == contract.candidate_id
            and prior.candidate_revision == contract.candidate_revision
            and prior.required_platforms == contract.required_platforms
        ):
            raise EvolutionRevalidationPromotionInputError(
                "fresh_promotion_input_authority_mismatch",
                "Prior Input、Fresh Plan、Final 与 Reapproval authority 不一致。",
            )
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_PROMOTION_INPUT_POLICY,
            "workspace_root": contract.workspace_root,
            "candidate_id": contract.candidate_id,
            "candidate_revision": contract.candidate_revision,
            "risk_level": prior.risk_level,
            "contract_id": contract.contract_id,
            "contract_sha256": contract.contract_sha256,
            "final_evaluation_id": final.receipt_id,
            "final_evaluation_sha256": final.receipt_sha256,
            "reapproval_authority_id": authority.authority_id,
            "reapproval_authority_sha256": authority.authority_sha256,
            "prior_input_id": prior.input_id,
            "prior_input_sha256": prior.input_sha256,
            "prior_input": prior.model_dump(mode="json"),
            "required_platforms": list(contract.required_platforms),
            "prior_final_invalidated": True,
            "prior_decision_reusable": False,
            "prior_approval_reusable": False,
            "prior_signature_reusable": False,
            "patch_and_rollback_carried_forward": True,
            "source_current_at_issue": True,
            "input_complete": True,
            "approval_requirement_ready": True,
            "approval_decided": False,
            "promotion_authority": False,
            "contains_source_code": False,
            "contains_freeform_narrative": False,
            "llm_generated": False,
            "created_at": authority.issued_at,
        }
        digest = _sha256_payload(payload)
        return await self.store.record(
            EvolutionRevalidationPromotionInput.model_validate({
                **payload,
                "input_id": f"evrevalpromoin_{digest[:24]}",
                "input_sha256": digest,
            })
        )


async def _require_dependencies(
    db: aiosqlite.Connection,
    item: EvolutionRevalidationPromotionInput,
) -> None:
    prior = await (
        await db.execute(
            "SELECT input_sha256, candidate_id, candidate_revision "
            "FROM evolution_promotion_package_inputs WHERE input_id = ?",
            (item.prior_input_id,),
        )
    ).fetchone()
    final = await (
        await db.execute(
            "SELECT receipt_sha256, contract_id "
            "FROM evolution_revalidation_final_evaluations WHERE receipt_id = ?",
            (item.final_evaluation_id,),
        )
    ).fetchone()
    authority = await (
        await db.execute(
            "SELECT authority_sha256, contract_id "
            "FROM evolution_revalidation_reapproval_authorities WHERE authority_id = ?",
            (item.reapproval_authority_id,),
        )
    ).fetchone()
    if not (
        prior is not None
        and prior["input_sha256"] == item.prior_input_sha256
        and prior["candidate_id"] == item.candidate_id
        and prior["candidate_revision"] == item.candidate_revision
        and final is not None
        and final["receipt_sha256"] == item.final_evaluation_sha256
        and final["contract_id"] == item.contract_id
        and authority is not None
        and authority["authority_sha256"] == item.reapproval_authority_sha256
        and authority["contract_id"] == item.contract_id
    ):
        await db.rollback()
        raise EvolutionRevalidationPromotionInputError(
            "fresh_promotion_input_dependency_mismatch",
            "Fresh Promotion Input 持久化依赖不一致。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_promotion_inputs ("
        "input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL UNIQUE, input_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_PROMOTION_INPUT_POLICY",
    "EvolutionRevalidationPromotionInput",
    "EvolutionRevalidationPromotionInputError",
    "EvolutionRevalidationPromotionInputService",
    "EvolutionRevalidationPromotionInputStore",
]
