"""Fresh approval requirements over current revalidation authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRole,
)
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcome,
    EvolutionRevalidationOutcomeStatus,
    EvolutionRevalidationOutcomeStore,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInput,
    EvolutionRevalidationPromotionInputService,
)
from naumi_agent.evolution.revalidation_reapproval_authorities import (
    EvolutionRevalidationReapprovalAuthority,
    EvolutionRevalidationReapprovalAuthorityStore,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequest,
    EvolutionRevalidationRequestStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContract,
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlan,
    EvolutionRevalidationValidationPlanStore,
)

EVOLUTION_REVALIDATION_APPROVAL_REQUIREMENT_POLICY = (
    "evolution-revalidation-approval-requirement-v1"
)
EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_DOMAIN = (
    "naumi.evolution.revalidation-approval.review.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_REQUIREMENT_BYTES = 512 * 1024


class EvolutionRevalidationApprovalReason(StrEnum):
    FRESH_REAPPROVAL = "fresh_reapproval"
    PROMOTION_OWNER_CONSENT = "promotion_owner_consent"
    MEDIUM_RISK = "medium_risk"
    HIGH_RISK = "high_risk"
    CRITICAL_RISK = "critical_risk"
    PROTECTED_TARGET = "protected_target"
    MIGRATION_REVIEW = "migration_review"
    DATA_BACKUP = "data_backup"


class EvolutionRevalidationApprovalTechnicalGate(StrEnum):
    RUNTIME_CONTRACT_CURRENT = "runtime_contract_current"
    FRESH_FINAL_ELIGIBLE = "fresh_final_eligible"
    REAPPROVAL_AUTHORIZED = "reapproval_authorized"
    CURRENT_TARGET_BOUND = "current_target_bound"
    FRESH_PROMOTION_INPUT_COMPLETE = "fresh_promotion_input_complete"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationApprovalStep(_StrictModel):
    order: int = Field(ge=1, le=5)
    role: EvolutionPromotionApprovalRole
    reasons: tuple[EvolutionRevalidationApprovalReason, ...] = Field(
        min_length=1,
        max_length=6,
    )
    human_required: Literal[True] = True
    signature_required: bool
    fresh_interaction_required: Literal[True] = True
    prior_response_reusable: Literal[False] = False
    prior_signature_reusable: Literal[False] = False
    approved: Literal[False] = False
    signature_collected: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("Fresh approval reasons 必须排序且不得重复。")
        if self.signature_required is (self.role is EvolutionPromotionApprovalRole.USER):
            raise ValueError("Fresh approval 仅允许 user 免专业签名。")
        return self


class EvolutionRevalidationApprovalRequirement(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-approval-requirement-v1"] = (
        EVOLUTION_REVALIDATION_APPROVAL_REQUIREMENT_POLICY
    )
    requirement_id: str = Field(pattern=r"^evreapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evrevalfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    reapproval_authority_id: str = Field(pattern=r"^evreapproval_[0-9a-f]{24}$")
    reapproval_authority_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    outcome_id: str = Field(pattern=r"^evrevalout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrevalidation_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    target_branch: str = Field(min_length=1, max_length=255)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    patch_manifest_sha256: str = Field(pattern=_SHA256_RE)
    migration_assessment_sha256: str = Field(pattern=_SHA256_RE)
    rollback_plan_sha256: str = Field(pattern=_SHA256_RE)
    steps: tuple[EvolutionRevalidationApprovalStep, ...] = Field(
        min_length=4,
        max_length=5,
    )
    required_roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(
        min_length=4,
        max_length=5,
    )
    signature_required_roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(
        min_length=3,
        max_length=4,
    )
    minimum_approvals: int = Field(ge=4, le=5)
    minimum_signatures: int = Field(ge=3, le=4)
    technical_gates: tuple[EvolutionRevalidationApprovalTechnicalGate, ...] = Field(
        min_length=5,
        max_length=5,
    )
    blocking_gates: tuple[str, ...] = Field(max_length=0)
    approval_request_ready: Literal[True] = True
    validity_seconds: int = Field(ge=3_600, le=604_800)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    current_source_at_issue: Literal[True] = True
    fresh_final_eligible_at_issue: Literal[True] = True
    reapproval_authorized_at_issue: Literal[True] = True
    prior_decision_reusable: Literal[False] = False
    prior_approval_reusable: Literal[False] = False
    prior_response_reusable: Literal[False] = False
    prior_signature_reusable: Literal[False] = False
    interaction_created: Literal[False] = False
    approval_decided: Literal[False] = False
    signatures_collected: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    contains_user_custom_text: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Approval Requirement workspace 必须 canonical。")
        issued = _aware(self.issued_at)
        if _aware(self.expires_at) != issued + timedelta(seconds=self.validity_seconds):
            raise ValueError("Fresh Approval Requirement expiry 投影不一致。")
        if tuple(item.order for item in self.steps) != tuple(
            range(1, len(self.steps) + 1)
        ):
            raise ValueError("Fresh approval steps 顺序不连续。")
        roles = tuple(item.role for item in self.steps)
        signatures = tuple(item.role for item in self.steps if item.signature_required)
        if roles != self.required_roles or len(roles) != len(set(roles)):
            raise ValueError("Fresh approval roles/steps 投影不一致。")
        if signatures != self.signature_required_roles:
            raise ValueError("Fresh approval signature roles 投影不一致。")
        if not (
            self.minimum_approvals == len(roles)
            and self.minimum_signatures == len(signatures)
        ):
            raise ValueError("Fresh approval quorum 投影不一致。")
        expected_gates = tuple(EvolutionRevalidationApprovalTechnicalGate)
        if self.technical_gates != expected_gates or self.blocking_gates:
            raise ValueError("Fresh approval technical gates 投影不一致。")
        signable = _sha256_payload(_signable_projection(self))
        if not hmac.compare_digest(self.signable_payload_sha256, signable):
            raise ValueError("Fresh approval signable payload 摘要不一致。")
        digest = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"requirement_id", "requirement_sha256"},
            )
        )
        if not hmac.compare_digest(self.requirement_sha256, digest):
            raise ValueError("Fresh Approval Requirement digest 不一致。")
        if self.requirement_id != f"evreapprovalreq_{digest[:24]}":
            raise ValueError("Fresh Approval Requirement identity 不一致。")
        return self


class EvolutionRevalidationApprovalRequirementError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationApprovalRequirementStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        promotion_input_id: str,
    ) -> EvolutionRevalidationApprovalRequirement | None:
        if re.fullmatch(r"evrevalpromoin_[0-9a-f]{24}", str(promotion_input_id)) is None:
            raise ValueError("Fresh Promotion Input id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT requirement_json FROM "
                        "evolution_revalidation_approval_requirements "
                        "WHERE promotion_input_id = ?",
                        (promotion_input_id,),
                    )
                ).fetchone()
            return None if row is None else _from_json(row["requirement_json"])
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_store_corrupt",
                "Fresh Approval Requirement 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        requirement: EvolutionRevalidationApprovalRequirement,
    ) -> EvolutionRevalidationApprovalRequirement:
        try:
            item = EvolutionRevalidationApprovalRequirement.model_validate_json(
                requirement.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_artifact_invalid",
                "Fresh Approval Requirement artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_REQUIREMENT_BYTES:
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_oversized",
                "Fresh Approval Requirement 超过 512 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                row = await (
                    await db.execute(
                        "SELECT requirement_json FROM "
                        "evolution_revalidation_approval_requirements "
                        "WHERE promotion_input_id = ?",
                        (item.promotion_input_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_json(row["requirement_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationApprovalRequirementError(
                            "fresh_approval_requirement_conflict",
                            "同一 Fresh Promotion Input 已绑定不同审批要求。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_approval_requirements "
                    "(requirement_id, requirement_sha256, promotion_input_id, "
                    "contract_id, target_branch, target_head, requirement_json, "
                    "issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.requirement_id,
                        item.requirement_sha256,
                        item.promotion_input_id,
                        item.contract_id,
                        item.target_branch,
                        item.target_head,
                        encoded,
                        item.issued_at,
                        item.expires_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationApprovalRequirementError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_store_error",
                "Fresh Approval Requirement 无法持久化。",
            ) from exc
        return item


class EvolutionRevalidationApprovalRequirementService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        promotion_input_service: EvolutionRevalidationPromotionInputService,
        contract_service: EvolutionRevalidationRuntimeContractService,
        validation_plan_store: EvolutionRevalidationValidationPlanStore,
        outcome_store: EvolutionRevalidationOutcomeStore,
        request_store: EvolutionRevalidationRequestStore,
        reapproval_store: EvolutionRevalidationReapprovalAuthorityStore,
        store: EvolutionRevalidationApprovalRequirementStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.promotion_input_service = promotion_input_service
        self.contract_service = contract_service
        self.validation_plan_store = validation_plan_store
        self.outcome_store = outcome_store
        self.request_store = request_store
        self.reapproval_store = reapproval_store
        self.store = store

    async def issue(
        self,
        *,
        contract_id: str,
    ) -> EvolutionRevalidationApprovalRequirement:
        fresh = await self.promotion_input_service.issue(contract_id=contract_id)
        contract_view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
        )
        contract = contract_view.contract
        plan = await self.validation_plan_store.get(contract.validation_plan_id)
        authority = await self.reapproval_store.get(contract_id)
        if plan is None or authority is None:
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_authority_missing",
                "Fresh Approval Requirement 缺少 Plan 或 Reapproval authority。",
            )
        outcome = await self.outcome_store.get(plan.outcome_id)
        request = None if outcome is None else await self.request_store.get(outcome.request_id)
        if outcome is None or request is None:
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_target_source_missing",
                "Fresh Approval Requirement 缺少 current target 来源。",
            )
        _validate_sources(fresh, contract, plan, outcome, request, authority)
        roles = list(authority.required_roles)
        prior = fresh.prior_input
        if (
            prior.migration.review_required or prior.migration.data_backup_required
        ) and "data_owner" not in roles:
            roles.append("data_owner")
        canonical_roles = tuple(
            role
            for role in (
                EvolutionPromotionApprovalRole.USER,
                EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
                EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
                EvolutionPromotionApprovalRole.DATA_OWNER,
                EvolutionPromotionApprovalRole.RELEASE_MANAGER,
            )
            if role.value in roles
        )
        steps = _steps(fresh, request.target_branch, canonical_roles)
        issued = datetime.fromisoformat(fresh.created_at).astimezone(UTC)
        validity = _validity_seconds(fresh.risk_level)
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_APPROVAL_REQUIREMENT_POLICY,
            "workspace_root": fresh.workspace_root,
            "promotion_input_id": fresh.input_id,
            "promotion_input_sha256": fresh.input_sha256,
            "contract_id": fresh.contract_id,
            "contract_sha256": fresh.contract_sha256,
            "final_evaluation_id": fresh.final_evaluation_id,
            "final_evaluation_sha256": fresh.final_evaluation_sha256,
            "reapproval_authority_id": fresh.reapproval_authority_id,
            "reapproval_authority_sha256": fresh.reapproval_authority_sha256,
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "outcome_id": outcome.outcome_id,
            "outcome_sha256": outcome.outcome_sha256,
            "request_id": request.request_id,
            "request_sha256": request.request_sha256,
            "candidate_id": fresh.candidate_id,
            "candidate_revision": fresh.candidate_revision,
            "risk_level": fresh.risk_level,
            "target_branch": request.target_branch,
            "target_head": plan.red_revision,
            "target_tree_sha256": plan.red_tree_sha256,
            "patch_manifest_sha256": prior.patch.manifest_sha256,
            "migration_assessment_sha256": prior.migration.assessment_sha256,
            "rollback_plan_sha256": prior.rollback.plan_sha256,
            "steps": [item.model_dump(mode="json") for item in steps],
            "required_roles": [item.value for item in canonical_roles],
            "signature_required_roles": [
                item.role.value for item in steps if item.signature_required
            ],
            "minimum_approvals": len(steps),
            "minimum_signatures": sum(item.signature_required for item in steps),
            "technical_gates": [
                item.value for item in EvolutionRevalidationApprovalTechnicalGate
            ],
            "blocking_gates": [],
            "approval_request_ready": True,
            "validity_seconds": validity,
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(seconds=validity)).isoformat(),
            "current_source_at_issue": True,
            "fresh_final_eligible_at_issue": True,
            "reapproval_authorized_at_issue": True,
            "prior_decision_reusable": False,
            "prior_approval_reusable": False,
            "prior_response_reusable": False,
            "prior_signature_reusable": False,
            "interaction_created": False,
            "approval_decided": False,
            "signatures_collected": False,
            "promotion_authority": False,
            "git_write_executed": False,
            "merge_executed": False,
            "push_executed": False,
            "publish_executed": False,
            "contains_freeform_narrative": False,
            "contains_user_custom_text": False,
            "llm_generated": False,
        }
        signable = _sha256_payload(_signable_projection(core))
        payload = {**core, "signable_payload_sha256": signable}
        digest = _sha256_payload(payload)
        return await self.store.record(
            EvolutionRevalidationApprovalRequirement.model_validate({
                **payload,
                "requirement_id": f"evreapprovalreq_{digest[:24]}",
                "requirement_sha256": digest,
            })
        )


def _validate_sources(
    fresh: EvolutionRevalidationPromotionInput,
    contract: EvolutionRevalidationRuntimeContract,
    plan: EvolutionRevalidationValidationPlan,
    outcome: EvolutionRevalidationOutcome,
    request: EvolutionRevalidationRequest,
    authority: EvolutionRevalidationReapprovalAuthority,
) -> None:
    prior = fresh.prior_input
    if not (
        fresh.workspace_root == plan.workspace_root == request.workspace_root
        and fresh.contract_id == contract.contract_id
        and fresh.contract_sha256 == contract.contract_sha256
        and contract.validation_plan_id == plan.validation_plan_id
        and contract.validation_plan_sha256 == plan.validation_plan_sha256
        and fresh.contract_id == authority.contract_id
        and fresh.contract_sha256 == authority.contract_sha256
        and fresh.reapproval_authority_id == authority.authority_id
        and fresh.reapproval_authority_sha256 == authority.authority_sha256
    ):
        raise EvolutionRevalidationApprovalRequirementError(
            "fresh_approval_requirement_authority_mismatch",
            "Fresh Promotion Input、Plan 与 Reapproval authority 不一致。",
        )
    if not (
        plan.validation_plan_id == authority.validation_plan_id
        and plan.validation_plan_sha256 == authority.validation_plan_sha256
        and plan.outcome_id == outcome.outcome_id
        and plan.outcome_sha256 == outcome.outcome_sha256
        and outcome.request_id == request.request_id
        and outcome.request_sha256 == request.request_sha256
        and outcome.status is EvolutionRevalidationOutcomeStatus.VALIDATED
        and outcome.target_head == plan.red_revision
        and outcome.target_tree_sha256 == plan.red_tree_sha256
        and request.promotion_input_id == fresh.prior_input_id
        and request.promotion_input_sha256 == fresh.prior_input_sha256
        and request.candidate_id == fresh.candidate_id == plan.candidate_id
        and request.candidate_revision == fresh.candidate_revision == plan.candidate_revision
        and request.patch_manifest_sha256 == prior.patch.manifest_sha256
        and request.baseline_sha256 == prior.baseline.baseline_sha256
        and request.migration_assessment_sha256 == prior.migration.assessment_sha256
        and request.rollback_plan_sha256 == prior.rollback.plan_sha256
    ):
        raise EvolutionRevalidationApprovalRequirementError(
            "fresh_approval_requirement_target_mismatch",
            "Fresh target、Request 与 prior patch/rollback authority 不一致。",
        )


def _steps(
    fresh: EvolutionRevalidationPromotionInput,
    target_branch: str,
    roles: tuple[EvolutionPromotionApprovalRole, ...],
) -> tuple[EvolutionRevalidationApprovalStep, ...]:
    risk_reason = {
        "low": EvolutionRevalidationApprovalReason.FRESH_REAPPROVAL,
        "medium": EvolutionRevalidationApprovalReason.MEDIUM_RISK,
        "high": EvolutionRevalidationApprovalReason.HIGH_RISK,
        "critical": EvolutionRevalidationApprovalReason.CRITICAL_RISK,
    }[fresh.risk_level]
    result = []
    for role in roles:
        reasons = {EvolutionRevalidationApprovalReason.FRESH_REAPPROVAL}
        if role is EvolutionPromotionApprovalRole.USER:
            reasons = {EvolutionRevalidationApprovalReason.PROMOTION_OWNER_CONSENT}
        elif role is EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER:
            reasons.add(risk_reason)
        elif (
            role is EvolutionPromotionApprovalRole.RELEASE_MANAGER
            and _target_is_protected(target_branch)
        ):
            reasons.add(EvolutionRevalidationApprovalReason.PROTECTED_TARGET)
        elif role is EvolutionPromotionApprovalRole.DATA_OWNER:
            if fresh.prior_input.migration.review_required:
                reasons.add(EvolutionRevalidationApprovalReason.MIGRATION_REVIEW)
            if fresh.prior_input.migration.data_backup_required:
                reasons.add(EvolutionRevalidationApprovalReason.DATA_BACKUP)
        result.append(EvolutionRevalidationApprovalStep(
            order=len(result) + 1,
            role=role,
            reasons=tuple(sorted(reasons, key=lambda item: item.value)),
            signature_required=role is not EvolutionPromotionApprovalRole.USER,
        ))
    return tuple(result)


async def _require_dependencies(
    db: aiosqlite.Connection,
    item: EvolutionRevalidationApprovalRequirement,
) -> None:
    input_row = await (
        await db.execute(
            "SELECT input_sha256, input_json "
            "FROM evolution_revalidation_promotion_inputs "
            "WHERE input_id = ? AND contract_id = ?",
            (item.promotion_input_id, item.contract_id),
        )
    ).fetchone()
    try:
        fresh = (
            None
            if input_row is None
            else EvolutionRevalidationPromotionInput.model_validate_json(
                input_row["input_json"]
            )
        )
    except (TypeError, ValueError):
        fresh = None
    if not (
        fresh is not None
        and input_row is not None
        and input_row["input_sha256"] == item.promotion_input_sha256
        and fresh.input_id == item.promotion_input_id
        and fresh.input_sha256 == item.promotion_input_sha256
        and fresh.contract_id == item.contract_id
        and fresh.contract_sha256 == item.contract_sha256
        and fresh.final_evaluation_id == item.final_evaluation_id
        and fresh.final_evaluation_sha256 == item.final_evaluation_sha256
        and fresh.reapproval_authority_id == item.reapproval_authority_id
        and fresh.reapproval_authority_sha256 == item.reapproval_authority_sha256
        and fresh.candidate_id == item.candidate_id
        and fresh.candidate_revision == item.candidate_revision
    ):
        await db.rollback()
        raise EvolutionRevalidationApprovalRequirementError(
            "fresh_approval_requirement_dependency_mismatch",
            "Fresh Approval Requirement 的 Promotion Input 依赖不一致。",
        )
    checks = (
        (
            "SELECT authority_sha256 FROM evolution_revalidation_reapproval_authorities "
            "WHERE authority_id = ? AND contract_id = ?",
            (item.reapproval_authority_id, item.contract_id),
            "authority_sha256",
            item.reapproval_authority_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_revalidation_final_evaluations "
            "WHERE receipt_id = ? AND contract_id = ?",
            (item.final_evaluation_id, item.contract_id),
            "receipt_sha256",
            item.final_evaluation_sha256,
        ),
        (
            "SELECT validation_plan_sha256 FROM "
            "evolution_revalidation_validation_plans WHERE validation_plan_id = ?",
            (item.validation_plan_id,),
            "validation_plan_sha256",
            item.validation_plan_sha256,
        ),
        (
            "SELECT outcome_sha256 FROM evolution_revalidation_outcomes "
            "WHERE outcome_id = ? AND request_id = ?",
            (item.outcome_id, item.request_id),
            "outcome_sha256",
            item.outcome_sha256,
        ),
        (
            "SELECT request_sha256 FROM evolution_revalidation_requests "
            "WHERE request_id = ? AND target_branch = ?",
            (item.request_id, item.target_branch),
            "request_sha256",
            item.request_sha256,
        ),
    )
    for query, parameters, column, expected in checks:
        row = await (await db.execute(query, parameters)).fetchone()
        if row is None or row[column] != expected:
            await db.rollback()
            raise EvolutionRevalidationApprovalRequirementError(
                "fresh_approval_requirement_dependency_mismatch",
                "Fresh Approval Requirement 持久化依赖不一致。",
            )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_approval_requirements ("
        "requirement_id TEXT PRIMARY KEY, requirement_sha256 TEXT NOT NULL UNIQUE, "
        "promotion_input_id TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL UNIQUE, "
        "target_branch TEXT NOT NULL, target_head TEXT NOT NULL, "
        "requirement_json TEXT NOT NULL, issued_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL)"
    )
    await db.commit()


def _from_json(value: object) -> EvolutionRevalidationApprovalRequirement:
    encoded = str(value)
    if len(encoded.encode("utf-8")) > _MAX_REQUIREMENT_BYTES:
        raise ValueError("Fresh Approval Requirement artifact 过大。")
    return EvolutionRevalidationApprovalRequirement.model_validate_json(encoded)


def _signable_projection(
    item: EvolutionRevalidationApprovalRequirement | dict[str, object],
) -> dict[str, object]:
    if isinstance(item, EvolutionRevalidationApprovalRequirement):
        data = item.model_dump(mode="json")
    else:
        data = dict(item)
    return {
        "domain": EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_DOMAIN,
        "promotion_input_id": data["promotion_input_id"],
        "promotion_input_sha256": data["promotion_input_sha256"],
        "contract_id": data["contract_id"],
        "contract_sha256": data["contract_sha256"],
        "final_evaluation_id": data["final_evaluation_id"],
        "final_evaluation_sha256": data["final_evaluation_sha256"],
        "reapproval_authority_id": data["reapproval_authority_id"],
        "reapproval_authority_sha256": data["reapproval_authority_sha256"],
        "target_branch": data["target_branch"],
        "target_head": data["target_head"],
        "target_tree_sha256": data["target_tree_sha256"],
        "patch_manifest_sha256": data["patch_manifest_sha256"],
        "migration_assessment_sha256": data["migration_assessment_sha256"],
        "rollback_plan_sha256": data["rollback_plan_sha256"],
        "required_roles": data["required_roles"],
    }


def _validity_seconds(risk_level: str) -> int:
    return {
        "low": 7 * 24 * 60 * 60,
        "medium": 3 * 24 * 60 * 60,
        "high": 24 * 60 * 60,
        "critical": 6 * 60 * 60,
    }[risk_level]


def _target_is_protected(branch: str) -> bool:
    folded = branch.casefold()
    return bool(
        folded in {"main", "master", "stable"}
        or folded.startswith("release/")
        or re.fullmatch(r"v?\d+(?:\.\d+){1,2}", folded) is not None
    )


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_APPROVAL_REQUIREMENT_POLICY",
    "EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_DOMAIN",
    "EvolutionRevalidationApprovalReason",
    "EvolutionRevalidationApprovalRequirement",
    "EvolutionRevalidationApprovalRequirementError",
    "EvolutionRevalidationApprovalRequirementService",
    "EvolutionRevalidationApprovalRequirementStore",
    "EvolutionRevalidationApprovalStep",
    "EvolutionRevalidationApprovalTechnicalGate",
]
