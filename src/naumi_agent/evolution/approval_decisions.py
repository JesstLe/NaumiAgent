"""Deterministic, append-only aggregation of promotion approval authority."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalState,
)
from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalRequestError,
    EvolutionPromotionApprovalResponse,
    EvolutionPromotionApprovalResponseReceipt,
    EvolutionPromotionApprovalResponseStore,
)
from naumi_agent.evolution.approval_requests import _ensure_schema as _ensure_response_schema
from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRequirement,
    EvolutionPromotionApprovalRequirementError,
    EvolutionPromotionApprovalRequirementExecutor,
    EvolutionPromotionApprovalRequirementView,
    EvolutionPromotionApprovalRole,
    EvolutionPromotionApprovalStep,
    EvolutionPromotionTechnicalGate,
)
from naumi_agent.evolution.approval_requirements import (
    _ensure_schema as _ensure_requirement_schema,
)
from naumi_agent.evolution.approval_signatures import (
    EvolutionApprovalSignatureError,
    EvolutionApprovalSignatureReceiptView,
    EvolutionApprovalSignatureService,
    EvolutionApprovalSignatureStore,
)
from naumi_agent.evolution.approval_signatures import (
    _ensure_schema as _ensure_signature_schema,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionPackageError,
    EvolutionPromotionPackageExecutor,
    EvolutionPromotionPackageView,
)
from naumi_agent.evolution.promotion_packages import _ensure_schema as _ensure_package_schema

EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY = "evolution-promotion-approval-decision-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_DECISION_BYTES = 512 * 1_024


class EvolutionPromotionApprovalDecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    PENDING = "pending"
    STALE = "stale"


class EvolutionPromotionApprovalRoleOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    RESPONSE_MISSING = "response_missing"
    IDENTITY_UNVERIFIED = "identity_unverified"
    SIGNATURE_MISSING = "signature_missing"
    SIGNATURE_STALE = "signature_stale"


class EvolutionPromotionTechnicalGateState(StrEnum):
    SATISFIED = "satisfied"
    BLOCKING = "blocking"
    STALE = "stale"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPromotionApprovalRoleDecision(_StrictModel):
    order: int = Field(ge=1, le=5)
    role: EvolutionPromotionApprovalRole
    signature_required: bool
    response_present: bool
    response_receipt_id: str = Field(
        default="",
        pattern=r"^(?:|evapprovalresp_[0-9a-f]{24})$",
    )
    response_receipt_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    response: Literal["", "approve", "reject", "request_changes"] = ""
    response_current: bool
    identity_verified: bool
    signature_present: bool
    signature_receipt_id: str = Field(
        default="",
        pattern=r"^(?:|evsigreceipt_[0-9a-f]{24})$",
    )
    signature_receipt_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    signature_verified: bool
    signature_current: bool
    counts_toward_quorum: bool
    outcome: EvolutionPromotionApprovalRoleOutcome

    @model_validator(mode="after")
    def _role_projection_is_exact(self) -> Self:
        response_ids = bool(self.response_receipt_id and self.response_receipt_sha256)
        signature_ids = bool(self.signature_receipt_id and self.signature_receipt_sha256)
        if self.response_present is not response_ids or self.signature_present is not signature_ids:
            raise ValueError("Approval role source presence 投影不一致。")
        if not self.response_present:
            if (
                any(
                    (
                        self.response,
                        self.response_current,
                        self.identity_verified,
                        self.signature_present,
                        self.signature_verified,
                        self.signature_current,
                        self.counts_toward_quorum,
                    )
                )
                or self.outcome is not EvolutionPromotionApprovalRoleOutcome.RESPONSE_MISSING
            ):
                raise ValueError("Missing Approval Response 的 role outcome 无效。")
            return self
        if not self.response or not self.response_current:
            raise ValueError("存在的 Approval Response 必须绑定 current exact authority。")
        if self.response == EvolutionPromotionApprovalResponse.REJECT.value:
            expected = EvolutionPromotionApprovalRoleOutcome.REJECTED
        elif self.response == EvolutionPromotionApprovalResponse.REQUEST_CHANGES.value:
            expected = EvolutionPromotionApprovalRoleOutcome.CHANGES_REQUESTED
        elif self.signature_required:
            if not self.signature_present:
                expected = EvolutionPromotionApprovalRoleOutcome.SIGNATURE_MISSING
            elif not self.signature_verified or not self.identity_verified:
                raise ValueError("Signature Receipt 存在但密码学或身份验证未通过。")
            elif not self.signature_current:
                expected = EvolutionPromotionApprovalRoleOutcome.SIGNATURE_STALE
            else:
                expected = EvolutionPromotionApprovalRoleOutcome.APPROVED
        elif self.identity_verified:
            expected = EvolutionPromotionApprovalRoleOutcome.APPROVED
        else:
            expected = EvolutionPromotionApprovalRoleOutcome.IDENTITY_UNVERIFIED
        expected_counts = expected is EvolutionPromotionApprovalRoleOutcome.APPROVED
        if self.outcome is not expected or self.counts_toward_quorum is not expected_counts:
            raise ValueError("Approval role outcome/quorum 投影不一致。")
        if not self.signature_present and any((self.signature_verified, self.signature_current)):
            raise ValueError("缺失 Signature Receipt 不能声明 signature verified/current。")
        return self


class EvolutionPromotionTechnicalGateDecision(_StrictModel):
    gate: EvolutionPromotionTechnicalGate
    state: EvolutionPromotionTechnicalGateState
    evidence_role: Literal["", "data_owner"] = ""

    @model_validator(mode="after")
    def _gate_projection_is_exact(self) -> Self:
        evidence_gates = {
            EvolutionPromotionTechnicalGate.MIGRATION_REVIEW_REQUIRED,
            EvolutionPromotionTechnicalGate.DATA_BACKUP_REQUIRED,
        }
        if (self.gate in evidence_gates) is not bool(self.evidence_role):
            raise ValueError("Approval technical gate evidence role 投影不一致。")
        return self


class EvolutionPromotionApprovalDecisionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-promotion-approval-decision-v1"] = (
        EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY
    )
    decision_id: str = Field(pattern=r"^evapprovaldecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=_SHA256_RE)
    source_set_sha256: str = Field(pattern=_SHA256_RE)
    sequence: int = Field(ge=1, le=10_000)
    previous_decision_id: str = Field(
        default="",
        pattern=r"^(?:|evapprovaldecision_[0-9a-f]{24})$",
    )
    previous_decision_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    workspace_root: str = Field(min_length=1, max_length=4_096)
    requirement_id: str = Field(pattern=r"^evapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    requirement_policy_version: str = Field(min_length=1, max_length=100)
    package_id: str = Field(pattern=r"^evpromopkg_[0-9a-f]{24}$")
    package_sha256: str = Field(pattern=_SHA256_RE)
    target_branch: str = Field(min_length=1, max_length=255)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    package_current: bool
    input_active: bool
    reflection_active: bool
    target_current: bool
    requirement_expired: bool
    role_decisions: tuple[EvolutionPromotionApprovalRoleDecision, ...] = Field(
        min_length=1,
        max_length=5,
    )
    technical_gate_decisions: tuple[EvolutionPromotionTechnicalGateDecision, ...] = Field(
        min_length=4,
        max_length=8,
    )
    required_approvals: int = Field(ge=1, le=5)
    approvals_collected: int = Field(ge=0, le=5)
    required_signatures: int = Field(ge=0, le=5)
    signatures_collected: int = Field(ge=0, le=5)
    missing_roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(max_length=5)
    blocking_gates: tuple[EvolutionPromotionTechnicalGate, ...] = Field(max_length=8)
    status: EvolutionPromotionApprovalDecisionStatus
    approval_decided: bool
    final_quorum_reached: bool
    rebase_revalidation_eligible: bool
    decided_at: str = Field(min_length=1, max_length=100)
    promotion_authority: Literal[False] = False
    promotion_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _decision_projection_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Approval Decision workspace 必须是 canonical 路径。")
        if (_aware(self.decided_at).utcoffset() is None) or (
            bool(self.previous_decision_id) is not bool(self.previous_decision_sha256)
        ):
            raise ValueError("Approval Decision clock 或 previous link 无效。")
        if (self.sequence == 1) is bool(self.previous_decision_id):
            raise ValueError("Approval Decision sequence/previous link 不一致。")
        roles = tuple(item.role for item in self.role_decisions)
        orders = tuple(item.order for item in self.role_decisions)
        if len(roles) != len(set(roles)) or orders != tuple(range(1, len(self.role_decisions) + 1)):
            raise ValueError("Approval Decision role 顺序或唯一性无效。")
        gates = tuple(item.gate for item in self.technical_gate_decisions)
        if len(gates) != len(set(gates)):
            raise ValueError("Approval Decision technical gate 重复。")
        data_owner_approved = any(
            item.role is EvolutionPromotionApprovalRole.DATA_OWNER
            and item.outcome is EvolutionPromotionApprovalRoleOutcome.APPROVED
            for item in self.role_decisions
        )
        for gate in self.technical_gate_decisions:
            expected_state, expected_evidence = _technical_gate_projection(
                gate.gate,
                package_current=self.package_current,
                input_active=self.input_active,
                reflection_active=self.reflection_active,
                target_current=self.target_current,
                data_owner_approved=data_owner_approved,
            )
            if gate.state is not expected_state or gate.evidence_role != expected_evidence:
                raise ValueError("Approval Decision technical gate 投影不一致。")
        approvals = sum(item.counts_toward_quorum for item in self.role_decisions)
        signatures = sum(
            item.signature_required
            and item.outcome is EvolutionPromotionApprovalRoleOutcome.APPROVED
            for item in self.role_decisions
        )
        missing = tuple(
            item.role
            for item in self.role_decisions
            if item.outcome
            in {
                EvolutionPromotionApprovalRoleOutcome.RESPONSE_MISSING,
                EvolutionPromotionApprovalRoleOutcome.IDENTITY_UNVERIFIED,
                EvolutionPromotionApprovalRoleOutcome.SIGNATURE_MISSING,
            }
        )
        blocking = tuple(
            item.gate
            for item in self.technical_gate_decisions
            if item.state is not EvolutionPromotionTechnicalGateState.SATISFIED
        )
        expected_status = _decision_status(
            package_current=self.package_current,
            input_active=self.input_active,
            reflection_active=self.reflection_active,
            target_current=self.target_current,
            requirement_expired=self.requirement_expired,
            role_decisions=self.role_decisions,
            gate_decisions=self.technical_gate_decisions,
        )
        if not (
            self.required_approvals == len(self.role_decisions)
            and self.approvals_collected == approvals
            and self.required_signatures
            == sum(item.signature_required for item in self.role_decisions)
            and self.signatures_collected == signatures
            and self.missing_roles == missing
            and self.blocking_gates == blocking
            and self.status is expected_status
            and self.approval_decided
            is (
                expected_status
                in {
                    EvolutionPromotionApprovalDecisionStatus.APPROVED,
                    EvolutionPromotionApprovalDecisionStatus.REJECTED,
                    EvolutionPromotionApprovalDecisionStatus.CHANGES_REQUESTED,
                }
            )
            and self.final_quorum_reached
            is (expected_status is EvolutionPromotionApprovalDecisionStatus.APPROVED)
            and self.rebase_revalidation_eligible
            is (expected_status is EvolutionPromotionApprovalDecisionStatus.APPROVED)
        ):
            raise ValueError("Approval Decision aggregate projection 不一致。")
        source_digest = _sha256_payload(_decision_source_projection(self))
        if not hmac.compare_digest(self.source_set_sha256, source_digest):
            raise ValueError("Approval Decision source-set 摘要不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"decision_id", "decision_sha256"})
        )
        if not hmac.compare_digest(self.decision_sha256, digest):
            raise ValueError("Approval Decision artifact 摘要不一致。")
        if self.decision_id != f"evapprovaldecision_{digest[:24]}":
            raise ValueError("Approval Decision identity 不一致。")
        return self


class EvolutionPromotionApprovalDecisionView(_StrictModel):
    receipt: EvolutionPromotionApprovalDecisionReceipt
    source_current: bool
    current_status: EvolutionPromotionApprovalDecisionStatus
    target_only_stale: bool = False
    current_rebase_revalidation_eligible: bool

    @model_validator(mode="after")
    def _view_projection_is_exact(self) -> Self:
        if self.source_current and self.current_status is not self.receipt.status:
            raise ValueError("Current Approval Decision 与 source-current Receipt 不一致。")
        if self.target_only_stale and (
            self.source_current
            or self.current_status is not EvolutionPromotionApprovalDecisionStatus.STALE
        ):
            raise ValueError("Approval Decision target-only stale 投影不一致。")
        if self.current_rebase_revalidation_eligible is not (
            self.current_status is EvolutionPromotionApprovalDecisionStatus.APPROVED
            or self.target_only_stale
        ):
            raise ValueError("Approval Decision current eligibility 投影不一致。")
        return self


class EvolutionPromotionApprovalDecisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPromotionApprovalDecisionBuilder:
    def build(
        self,
        *,
        requirement_view: EvolutionPromotionApprovalRequirementView,
        package_view: EvolutionPromotionPackageView,
        responses: tuple[EvolutionPromotionApprovalResponseReceipt, ...],
        signatures: tuple[EvolutionApprovalSignatureReceiptView, ...],
        sequence: int,
        previous_decision_id: str = "",
        previous_decision_sha256: str = "",
        decided_at: datetime,
    ) -> EvolutionPromotionApprovalDecisionReceipt:
        try:
            requirement_view = EvolutionPromotionApprovalRequirementView.model_validate_json(
                requirement_view.model_dump_json()
            )
            package_view = EvolutionPromotionPackageView.model_validate_json(
                package_view.model_dump_json()
            )
            responses = tuple(
                EvolutionPromotionApprovalResponseReceipt.model_validate_json(
                    item.model_dump_json()
                )
                for item in responses
            )
            signatures = tuple(
                EvolutionApprovalSignatureReceiptView.model_validate_json(item.model_dump_json())
                for item in signatures
            )
            timestamp = _aware_datetime(decided_at, "Approval Decision clock")
            _require_requirement_package(requirement_view, package_view)
            response_by_role = _responses_by_role(requirement_view.requirement, responses)
            signature_by_response = _signatures_by_response(signatures)
            _require_signature_set_scoped(
                requirement_view.requirement,
                response_by_role,
                signature_by_response,
            )
            role_decisions = tuple(
                _role_decision(
                    requirement_view.requirement,
                    step,
                    response_by_role.get(step.role),
                    signature_by_response,
                )
                for step in requirement_view.requirement.steps
            )
            gate_decisions = _technical_gate_decisions(
                requirement_view,
                package_view,
                role_decisions,
            )
        except EvolutionPromotionApprovalDecisionError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_input_invalid",
                "Approval Decision 输入 authority 无效。",
            ) from exc
        status = _decision_status(
            package_current=True,
            input_active=package_view.promotion_input_active,
            reflection_active=package_view.reflection_active,
            target_current=package_view.target_current,
            requirement_expired=requirement_view.expired,
            role_decisions=role_decisions,
            gate_decisions=gate_decisions,
        )
        missing = tuple(
            item.role
            for item in role_decisions
            if item.outcome
            in {
                EvolutionPromotionApprovalRoleOutcome.RESPONSE_MISSING,
                EvolutionPromotionApprovalRoleOutcome.IDENTITY_UNVERIFIED,
                EvolutionPromotionApprovalRoleOutcome.SIGNATURE_MISSING,
            }
        )
        blocking = tuple(
            item.gate
            for item in gate_decisions
            if item.state is not EvolutionPromotionTechnicalGateState.SATISFIED
        )
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY,
            "sequence": sequence,
            "previous_decision_id": previous_decision_id,
            "previous_decision_sha256": previous_decision_sha256,
            "workspace_root": requirement_view.requirement.workspace_root,
            "requirement_id": requirement_view.requirement.requirement_id,
            "requirement_sha256": requirement_view.requirement.requirement_sha256,
            "requirement_policy_version": requirement_view.requirement.policy_version,
            "package_id": requirement_view.requirement.package_id,
            "package_sha256": requirement_view.requirement.package_sha256,
            "target_branch": requirement_view.requirement.target_branch,
            "target_head": requirement_view.requirement.target_head,
            "target_tree": requirement_view.requirement.target_tree,
            "package_current": True,
            "input_active": package_view.promotion_input_active,
            "reflection_active": package_view.reflection_active,
            "target_current": package_view.target_current,
            "requirement_expired": requirement_view.expired,
            "role_decisions": [item.model_dump(mode="json") for item in role_decisions],
            "technical_gate_decisions": [item.model_dump(mode="json") for item in gate_decisions],
            "required_approvals": len(role_decisions),
            "approvals_collected": sum(item.counts_toward_quorum for item in role_decisions),
            "required_signatures": sum(item.signature_required for item in role_decisions),
            "signatures_collected": sum(
                item.signature_required
                and item.outcome is EvolutionPromotionApprovalRoleOutcome.APPROVED
                for item in role_decisions
            ),
            "missing_roles": [item.value for item in missing],
            "blocking_gates": [item.value for item in blocking],
            "status": status.value,
            "approval_decided": status
            in {
                EvolutionPromotionApprovalDecisionStatus.APPROVED,
                EvolutionPromotionApprovalDecisionStatus.REJECTED,
                EvolutionPromotionApprovalDecisionStatus.CHANGES_REQUESTED,
            },
            "final_quorum_reached": status is EvolutionPromotionApprovalDecisionStatus.APPROVED,
            "rebase_revalidation_eligible": status
            is EvolutionPromotionApprovalDecisionStatus.APPROVED,
            "decided_at": timestamp.isoformat(),
            "promotion_authority": False,
            "promotion_executed": False,
            "git_write_executed": False,
            "merge_executed": False,
            "push_executed": False,
            "publish_executed": False,
            "contains_freeform_narrative": False,
            "llm_generated": False,
        }
        source_digest = _sha256_payload(_decision_source_projection(core))
        payload = {**core, "source_set_sha256": source_digest}
        digest = _sha256_payload(payload)
        try:
            return EvolutionPromotionApprovalDecisionReceipt.model_validate(
                {
                    **payload,
                    "decision_id": f"evapprovaldecision_{digest[:24]}",
                    "decision_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_artifact_invalid",
                "Approval Decision Receipt 无法验证。",
            ) from exc


class EvolutionPromotionApprovalDecisionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        receipt: EvolutionPromotionApprovalDecisionReceipt,
        *,
        requirement_view: EvolutionPromotionApprovalRequirementView,
        package_view: EvolutionPromotionPackageView,
        responses: tuple[EvolutionPromotionApprovalResponseReceipt, ...],
        signatures: tuple[EvolutionApprovalSignatureReceiptView, ...],
    ) -> EvolutionPromotionApprovalDecisionReceipt:
        item = _validated_receipt(receipt)
        expected = EvolutionPromotionApprovalDecisionBuilder().build(
            requirement_view=requirement_view,
            package_view=package_view,
            responses=responses,
            signatures=signatures,
            sequence=item.sequence,
            previous_decision_id=item.previous_decision_id,
            previous_decision_sha256=item.previous_decision_sha256,
            decided_at=_aware(item.decided_at),
        )
        if expected != item:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_source_mismatch",
                "Approval Decision 未由 exact current authority 确定性生成。",
            )
        encoded = item.model_dump_json()
        _require_bounded(encoded)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_upstream_schemas(db)
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _assert_source_indexes(
                    db,
                    item=item,
                    requirement=requirement_view.requirement,
                )
                same_source = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_decisions "
                        "WHERE requirement_id = ? AND source_set_sha256 = ?",
                        (item.requirement_id, item.source_set_sha256),
                    )
                ).fetchone()
                if same_source is not None:
                    await db.rollback()
                    return _from_row(same_source)
                latest_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_decisions "
                        "WHERE requirement_id = ? ORDER BY sequence DESC LIMIT 1",
                        (item.requirement_id,),
                    )
                ).fetchone()
                if latest_row is None:
                    chain_ok = bool(
                        item.sequence == 1
                        and not item.previous_decision_id
                        and not item.previous_decision_sha256
                    )
                else:
                    latest = _from_row(latest_row)
                    chain_ok = bool(
                        item.sequence == latest.sequence + 1
                        and item.previous_decision_id == latest.decision_id
                        and item.previous_decision_sha256 == latest.decision_sha256
                    )
                if not chain_ok:
                    await db.rollback()
                    raise EvolutionPromotionApprovalDecisionError(
                        "approval_decision_chain_race",
                        "Approval Decision append chain 已变化，请重试。",
                    )
                collision = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_decisions "
                        "WHERE decision_id = ?",
                        (item.decision_id,),
                    )
                ).fetchone()
                if collision is not None:
                    restored = _from_row(collision)
                    if restored != item:
                        await db.rollback()
                        raise EvolutionPromotionApprovalDecisionError(
                            "approval_decision_id_conflict",
                            "Approval Decision ID 已绑定不同内容。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_promotion_approval_decisions "
                    "(decision_id, decision_sha256, source_set_sha256, requirement_id, "
                    "requirement_sha256, package_id, package_sha256, workspace_root, "
                    "sequence, previous_decision_id, status, decision_json, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.decision_id,
                        item.decision_sha256,
                        item.source_set_sha256,
                        item.requirement_id,
                        item.requirement_sha256,
                        item.package_id,
                        item.package_sha256,
                        item.workspace_root,
                        item.sequence,
                        item.previous_decision_id,
                        item.status.value,
                        encoded,
                        item.decided_at,
                    ),
                )
                await db.commit()
        except EvolutionPromotionApprovalDecisionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_store_error",
                "Approval Decision Receipt 无法持久化。",
            ) from exc
        restored = await self.get(item.decision_id)
        assert restored is not None
        return restored

    async def get(
        self,
        decision_id: str,
    ) -> EvolutionPromotionApprovalDecisionReceipt | None:
        if re.fullmatch(r"evapprovaldecision_[0-9a-f]{24}", str(decision_id)) is None:
            raise ValueError("approval decision id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_decisions "
                        "WHERE decision_id = ?",
                        (decision_id,),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionApprovalDecisionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_store_corrupt",
                "Approval Decision Receipt 损坏或无法读取。",
            ) from exc

    async def latest(
        self,
        requirement_id: str,
    ) -> EvolutionPromotionApprovalDecisionReceipt | None:
        if re.fullmatch(r"evapprovalreq_[0-9a-f]{24}", str(requirement_id)) is None:
            raise ValueError("approval requirement id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_decisions "
                        "WHERE requirement_id = ? ORDER BY sequence DESC LIMIT 1",
                        (requirement_id,),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionApprovalDecisionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_store_corrupt",
                "Approval Decision append chain 损坏或无法读取。",
            ) from exc


class EvolutionPromotionApprovalDecisionService:
    def __init__(
        self,
        *,
        requirement_executor: EvolutionPromotionApprovalRequirementExecutor,
        package_executor: EvolutionPromotionPackageExecutor,
        response_store: EvolutionPromotionApprovalResponseStore,
        signature_store: EvolutionApprovalSignatureStore,
        signature_service: EvolutionApprovalSignatureService,
        decision_store: EvolutionPromotionApprovalDecisionStore,
        builder: EvolutionPromotionApprovalDecisionBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            requirement_executor,
            EvolutionPromotionApprovalRequirementExecutor,
        ):
            raise TypeError("Approval Decision service 需要 Requirement Executor。")
        if not isinstance(package_executor, EvolutionPromotionPackageExecutor):
            raise TypeError("Approval Decision service 需要 Package Executor。")
        if not isinstance(response_store, EvolutionPromotionApprovalResponseStore):
            raise TypeError("Approval Decision service 需要 Response Store。")
        if not isinstance(signature_store, EvolutionApprovalSignatureStore):
            raise TypeError("Approval Decision service 需要 Signature Store。")
        if not isinstance(signature_service, EvolutionApprovalSignatureService):
            raise TypeError("Approval Decision service 需要 Signature Service。")
        if not isinstance(decision_store, EvolutionPromotionApprovalDecisionStore):
            raise TypeError("Approval Decision service 需要 Decision Store。")
        if builder is not None and not isinstance(
            builder,
            EvolutionPromotionApprovalDecisionBuilder,
        ):
            raise TypeError("Approval Decision service 需要 Decision Builder。")
        self._requirement_executor = requirement_executor
        self._package_executor = package_executor
        self._response_store = response_store
        self._signature_store = signature_store
        self._signature_service = signature_service
        self._decision_store = decision_store
        self._builder = builder or EvolutionPromotionApprovalDecisionBuilder()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        requirement_id: str,
    ) -> EvolutionPromotionApprovalDecisionView:
        key = f"{Path(workspace_root).expanduser()}::{requirement_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            for _attempt in range(3):
                sources = await self._collect_sources(workspace_root, requirement_id)
                latest = await self._decision_store.latest(requirement_id)
                sequence = 1 if latest is None else latest.sequence + 1
                decided_at = self._now()
                proposed = self._builder.build(
                    **sources,
                    sequence=sequence,
                    previous_decision_id="" if latest is None else latest.decision_id,
                    previous_decision_sha256="" if latest is None else latest.decision_sha256,
                    decided_at=decided_at,
                )
                refreshed = await self._collect_sources(workspace_root, requirement_id)
                expected = self._builder.build(
                    **refreshed,
                    sequence=sequence,
                    previous_decision_id="" if latest is None else latest.decision_id,
                    previous_decision_sha256="" if latest is None else latest.decision_sha256,
                    decided_at=decided_at,
                )
                if proposed.source_set_sha256 != expected.source_set_sha256:
                    continue
                try:
                    stored = await self._decision_store.record(
                        expected,
                        **refreshed,
                    )
                except EvolutionPromotionApprovalDecisionError as exc:
                    if exc.code == "approval_decision_chain_race":
                        continue
                    raise
                current_sources = await self._collect_sources(
                    workspace_root,
                    requirement_id,
                )
                current = self._builder.build(
                    **current_sources,
                    sequence=stored.sequence,
                    previous_decision_id=stored.previous_decision_id,
                    previous_decision_sha256=stored.previous_decision_sha256,
                    decided_at=_aware(stored.decided_at),
                )
                source_current = current.source_set_sha256 == stored.source_set_sha256
                target_only_stale = _target_only_stale(
                    approved=stored,
                    current=current,
                    sources=current_sources,
                )
                return EvolutionPromotionApprovalDecisionView(
                    receipt=stored,
                    source_current=source_current,
                    current_status=current.status,
                    target_only_stale=target_only_stale,
                    current_rebase_revalidation_eligible=(
                        current.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
                        or target_only_stale
                    ),
                )
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_authority_unstable",
                "审批 authority 在聚合期间持续变化，请稍后重试。",
            )

    async def inspect(
        self,
        *,
        workspace_root: str | Path,
        decision_id: str,
    ) -> EvolutionPromotionApprovalDecisionView:
        receipt = await self._decision_store.get(decision_id)
        if receipt is None:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_missing",
                "Approval Decision Receipt 不存在。",
            )
        sources = await self._collect_sources(workspace_root, receipt.requirement_id)
        current = self._builder.build(
            **sources,
            sequence=receipt.sequence,
            previous_decision_id=receipt.previous_decision_id,
            previous_decision_sha256=receipt.previous_decision_sha256,
            decided_at=_aware(receipt.decided_at),
        )
        source_current = current.source_set_sha256 == receipt.source_set_sha256
        target_only_stale = _target_only_stale(
            approved=receipt,
            current=current,
            sources=sources,
        )
        return EvolutionPromotionApprovalDecisionView(
            receipt=receipt,
            source_current=source_current,
            current_status=current.status,
            target_only_stale=target_only_stale,
            current_rebase_revalidation_eligible=(
                current.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
                or target_only_stale
            ),
        )

    async def _collect_sources(
        self,
        workspace_root: str | Path,
        requirement_id: str,
    ) -> dict[str, object]:
        try:
            requirement_view = await self._requirement_executor.inspect(
                workspace_root=workspace_root,
                requirement_id=requirement_id,
            )
            package_view = await self._package_executor.inspect(
                workspace_root=workspace_root,
                package_id=requirement_view.requirement.package_id,
            )
        except (
            EvolutionPromotionApprovalRequirementError,
            EvolutionPromotionPackageError,
        ) as exc:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_source_read_failed",
                "无法读取 Approval Requirement 或 Promotion Package authority。",
            ) from exc
        responses: list[EvolutionPromotionApprovalResponseReceipt] = []
        signatures: list[EvolutionApprovalSignatureReceiptView] = []
        for step in requirement_view.requirement.steps:
            try:
                response = await self._response_store.get_by_requirement_role(
                    requirement_id,
                    step.role,
                )
            except EvolutionPromotionApprovalRequestError as exc:
                raise EvolutionPromotionApprovalDecisionError(
                    "approval_decision_response_read_failed",
                    f"无法读取 {step.role.value} Approval Response authority。",
                ) from exc
            if response is None:
                continue
            responses.append(response)
            if (
                response.response is EvolutionPromotionApprovalResponse.APPROVE
                and step.signature_required
            ):
                try:
                    signature = await self._signature_store.get_receipt_by_response(
                        response.receipt_id
                    )
                    if signature is not None:
                        signatures.append(
                            await self._signature_service.inspect(
                                workspace_root=workspace_root,
                                receipt_id=signature.receipt_id,
                            )
                        )
                except EvolutionApprovalSignatureError as exc:
                    raise EvolutionPromotionApprovalDecisionError(
                        "approval_decision_signature_read_failed",
                        f"无法读取 {step.role.value} Signature Receipt authority。",
                    ) from exc
        return {
            "requirement_view": requirement_view,
            "package_view": package_view,
            "responses": tuple(responses),
            "signatures": tuple(signatures),
        }

    def _now(self) -> datetime:
        return _aware_datetime(self._clock(), "Approval Decision clock")


def render_evolution_promotion_approval_decision(
    view: EvolutionPromotionApprovalDecisionView,
) -> str:
    view = EvolutionPromotionApprovalDecisionView.model_validate_json(view.model_dump_json())
    item = view.receipt
    lines = [
        f"# Approval Decision `{item.decision_id}`",
        "",
        "**该回执只聚合审批 authority；不会执行 rebase、Git、merge、push、publish 或 Promotion。**",
        "",
        f"- Status：`{item.status.value}` · current `{view.current_status.value}`",
        f"- Source current：{'是' if view.source_current else '否'}",
        f"- Target-only stale：{'是' if view.target_only_stale else '否'}",
        f"- Sequence：{item.sequence}",
        f"- Requirement：`{item.requirement_id}`",
        f"- Package：`{item.package_id}`",
        f"- Approvals：{item.approvals_collected}/{item.required_approvals}",
        f"- Signatures：{item.signatures_collected}/{item.required_signatures}",
        "- Missing roles：" + (", ".join(role.value for role in item.missing_roles) or "none"),
        "- Blocking gates：" + (", ".join(gate.value for gate in item.blocking_gates) or "none"),
        "- Rebase/revalidate eligible："
        + ("是" if view.current_rebase_revalidation_eligible else "否"),
        "- Git/Merge/Push/Publish/Promotion：`false`",
        "",
        "## Roles",
    ]
    lines.extend(
        f"- `{role.role.value}`：`{role.outcome.value}`"
        + (f" · response `{role.response_receipt_id}`" if role.response_receipt_id else "")
        for role in item.role_decisions
    )
    return "\n".join(lines)


def _role_decision(
    requirement: EvolutionPromotionApprovalRequirement,
    step: EvolutionPromotionApprovalStep,
    response: EvolutionPromotionApprovalResponseReceipt | None,
    signatures: Mapping[str, EvolutionApprovalSignatureReceiptView],
) -> EvolutionPromotionApprovalRoleDecision:
    if response is None:
        return EvolutionPromotionApprovalRoleDecision(
            order=step.order,
            role=step.role,
            signature_required=step.signature_required,
            response_present=False,
            response_current=False,
            identity_verified=False,
            signature_present=False,
            signature_verified=False,
            signature_current=False,
            counts_toward_quorum=False,
            outcome=EvolutionPromotionApprovalRoleOutcome.RESPONSE_MISSING,
        )
    _require_response_matches(requirement, step, response)
    signature = signatures.get(response.receipt_id)
    if response.response is EvolutionPromotionApprovalResponse.REJECT:
        outcome = EvolutionPromotionApprovalRoleOutcome.REJECTED
    elif response.response is EvolutionPromotionApprovalResponse.REQUEST_CHANGES:
        outcome = EvolutionPromotionApprovalRoleOutcome.CHANGES_REQUESTED
    elif step.signature_required:
        if signature is None:
            outcome = EvolutionPromotionApprovalRoleOutcome.SIGNATURE_MISSING
        elif not signature.eligible_for_future_aggregation:
            outcome = EvolutionPromotionApprovalRoleOutcome.SIGNATURE_STALE
        else:
            outcome = EvolutionPromotionApprovalRoleOutcome.APPROVED
    elif response.counts_toward_role_quorum and response.role_binding_verified:
        outcome = EvolutionPromotionApprovalRoleOutcome.APPROVED
    else:
        outcome = EvolutionPromotionApprovalRoleOutcome.IDENTITY_UNVERIFIED
    if signature is not None:
        _require_signature_matches(response, signature)
    verified_signature = bool(signature is not None and signature.receipt.signature_verified)
    identity_verified = bool(
        (not step.signature_required and response.role_binding_verified)
        or (signature is not None and signature.receipt.identity_binding_verified)
    )
    return EvolutionPromotionApprovalRoleDecision(
        order=step.order,
        role=step.role,
        signature_required=step.signature_required,
        response_present=True,
        response_receipt_id=response.receipt_id,
        response_receipt_sha256=response.receipt_sha256,
        response=response.response.value,
        response_current=True,
        identity_verified=identity_verified,
        signature_present=signature is not None,
        signature_receipt_id="" if signature is None else signature.receipt.receipt_id,
        signature_receipt_sha256="" if signature is None else signature.receipt.receipt_sha256,
        signature_verified=verified_signature,
        signature_current=bool(signature is not None and signature.eligible_for_future_aggregation),
        counts_toward_quorum=(outcome is EvolutionPromotionApprovalRoleOutcome.APPROVED),
        outcome=outcome,
    )


def _technical_gate_decisions(
    requirement_view: EvolutionPromotionApprovalRequirementView,
    package_view: EvolutionPromotionPackageView,
    roles: tuple[EvolutionPromotionApprovalRoleDecision, ...],
) -> tuple[EvolutionPromotionTechnicalGateDecision, ...]:
    data_owner_approved = any(
        item.role is EvolutionPromotionApprovalRole.DATA_OWNER
        and item.outcome is EvolutionPromotionApprovalRoleOutcome.APPROVED
        for item in roles
    )
    values: list[EvolutionPromotionTechnicalGateDecision] = []
    for gate in requirement_view.requirement.technical_gates:
        state, evidence_role = _technical_gate_projection(
            gate,
            package_current=True,
            input_active=package_view.promotion_input_active,
            reflection_active=package_view.reflection_active,
            target_current=package_view.target_current,
            data_owner_approved=data_owner_approved,
        )
        values.append(
            EvolutionPromotionTechnicalGateDecision(
                gate=gate,
                state=state,
                evidence_role=evidence_role,
            )
        )
    return tuple(values)


def _technical_gate_projection(
    gate: EvolutionPromotionTechnicalGate,
    *,
    package_current: bool,
    input_active: bool,
    reflection_active: bool,
    target_current: bool,
    data_owner_approved: bool,
) -> tuple[EvolutionPromotionTechnicalGateState, Literal["", "data_owner"]]:
    dynamic_states = {
        EvolutionPromotionTechnicalGate.PACKAGE_CURRENT: package_current,
        EvolutionPromotionTechnicalGate.INPUT_ACTIVE: input_active,
        EvolutionPromotionTechnicalGate.REFLECTION_ACTIVE: reflection_active,
        EvolutionPromotionTechnicalGate.TARGET_CURRENT: target_current,
    }
    if gate in dynamic_states:
        return (
            EvolutionPromotionTechnicalGateState.SATISFIED
            if dynamic_states[gate]
            else EvolutionPromotionTechnicalGateState.STALE,
            "",
        )
    if gate in {
        EvolutionPromotionTechnicalGate.REBASE_REQUIRED,
        EvolutionPromotionTechnicalGate.REVALIDATION_REQUIRED,
    }:
        return EvolutionPromotionTechnicalGateState.BLOCKING, ""
    if gate in {
        EvolutionPromotionTechnicalGate.MIGRATION_REVIEW_REQUIRED,
        EvolutionPromotionTechnicalGate.DATA_BACKUP_REQUIRED,
    }:
        return (
            EvolutionPromotionTechnicalGateState.SATISFIED
            if data_owner_approved
            else EvolutionPromotionTechnicalGateState.BLOCKING,
            "data_owner",
        )
    raise ValueError(f"未知 Approval technical gate：{gate.value}")


def _decision_status(
    *,
    package_current: bool,
    input_active: bool,
    reflection_active: bool,
    target_current: bool,
    requirement_expired: bool,
    role_decisions: tuple[EvolutionPromotionApprovalRoleDecision, ...],
    gate_decisions: tuple[EvolutionPromotionTechnicalGateDecision, ...],
) -> EvolutionPromotionApprovalDecisionStatus:
    if (
        not all((package_current, input_active, reflection_active, target_current))
        or requirement_expired
    ):
        return EvolutionPromotionApprovalDecisionStatus.STALE
    if any(
        item.outcome is EvolutionPromotionApprovalRoleOutcome.REJECTED for item in role_decisions
    ):
        return EvolutionPromotionApprovalDecisionStatus.REJECTED
    if any(
        item.outcome is EvolutionPromotionApprovalRoleOutcome.CHANGES_REQUESTED
        for item in role_decisions
    ):
        return EvolutionPromotionApprovalDecisionStatus.CHANGES_REQUESTED
    if any(
        item.outcome is EvolutionPromotionApprovalRoleOutcome.SIGNATURE_STALE
        for item in role_decisions
    ):
        return EvolutionPromotionApprovalDecisionStatus.STALE
    if any(
        item.outcome is not EvolutionPromotionApprovalRoleOutcome.APPROVED
        for item in role_decisions
    ) or any(
        item.state is not EvolutionPromotionTechnicalGateState.SATISFIED for item in gate_decisions
    ):
        return EvolutionPromotionApprovalDecisionStatus.PENDING
    return EvolutionPromotionApprovalDecisionStatus.APPROVED


def _target_only_stale(
    *,
    approved: EvolutionPromotionApprovalDecisionReceipt,
    current: EvolutionPromotionApprovalDecisionReceipt,
    sources: Mapping[str, object],
) -> bool:
    """Return whether only target movement invalidated an approved authority set."""
    if not (
        approved.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
        and current.status is EvolutionPromotionApprovalDecisionStatus.STALE
        and current.package_current
        and current.input_active
        and current.reflection_active
        and not current.target_current
        and not current.requirement_expired
    ):
        return False
    current_roles = {item.role: item for item in current.role_decisions}
    for original in approved.role_decisions:
        observed = current_roles.get(original.role)
        if original.outcome is not EvolutionPromotionApprovalRoleOutcome.APPROVED:
            return False
        allowed = {EvolutionPromotionApprovalRoleOutcome.APPROVED}
        if original.signature_required:
            allowed.add(EvolutionPromotionApprovalRoleOutcome.SIGNATURE_STALE)
        if observed is None or observed.outcome not in allowed:
            return False
    signatures = tuple(sources.get("signatures", ()))
    if any(
        not isinstance(item, EvolutionApprovalSignatureReceiptView)
        or not (
            item.requirement_current
            and not item.target_current
            and not item.requirement_expired
            and item.response_current
            and item.principal_active
            and item.principal_key_current
            and item.role_binding_current
        )
        for item in signatures
    ):
        return False
    if len(signatures) != approved.required_signatures:
        return False
    target_gate_seen = False
    for gate in current.technical_gate_decisions:
        if gate.gate is EvolutionPromotionTechnicalGate.TARGET_CURRENT:
            target_gate_seen = True
            if gate.state is not EvolutionPromotionTechnicalGateState.STALE:
                return False
        elif gate.state is not EvolutionPromotionTechnicalGateState.SATISFIED:
            return False
    return target_gate_seen


def _responses_by_role(
    requirement: EvolutionPromotionApprovalRequirement,
    responses: tuple[EvolutionPromotionApprovalResponseReceipt, ...],
) -> dict[EvolutionPromotionApprovalRole, EvolutionPromotionApprovalResponseReceipt]:
    values: dict[EvolutionPromotionApprovalRole, EvolutionPromotionApprovalResponseReceipt] = {}
    steps = {item.role: item for item in requirement.steps}
    for response in responses:
        step = steps.get(response.role)
        if step is None or response.role in values:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_response_set_invalid",
                "Approval Response set 含额外或重复角色。",
            )
        _require_response_matches(requirement, step, response)
        values[response.role] = response
    return values


def _signatures_by_response(
    signatures: tuple[EvolutionApprovalSignatureReceiptView, ...],
) -> dict[str, EvolutionApprovalSignatureReceiptView]:
    values: dict[str, EvolutionApprovalSignatureReceiptView] = {}
    for signature in signatures:
        response_id = signature.receipt.challenge.payload.approval_response_id
        if response_id in values:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_signature_set_invalid",
                "Signature Receipt set 含重复 Response。",
            )
        values[response_id] = signature
    return values


def _require_signature_set_scoped(
    requirement: EvolutionPromotionApprovalRequirement,
    responses: Mapping[
        EvolutionPromotionApprovalRole,
        EvolutionPromotionApprovalResponseReceipt,
    ],
    signatures: Mapping[str, EvolutionApprovalSignatureReceiptView],
) -> None:
    steps = {item.role: item for item in requirement.steps}
    allowed_response_ids = {
        response.receipt_id
        for role, response in responses.items()
        if steps[role].signature_required
        and response.response is EvolutionPromotionApprovalResponse.APPROVE
    }
    extras = tuple(sorted(set(signatures) - allowed_response_ids))
    if extras:
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_signature_set_invalid",
            "Signature Receipt set 含不属于当前可签名 Approval Response 的证据。",
        )


def _require_requirement_package(
    requirement_view: EvolutionPromotionApprovalRequirementView,
    package_view: EvolutionPromotionPackageView,
) -> None:
    requirement = requirement_view.requirement
    package = package_view.package
    if not (
        requirement.workspace_root == package.workspace_root
        and requirement.package_id == package.package_id
        and requirement.package_sha256 == package.package_sha256
        and requirement.promotion_input_id == package.promotion_input_id
        and requirement.promotion_input_sha256 == package.promotion_input_sha256
        and requirement.reflection_id == package.reflection_id
        and requirement.target_branch == package.target.target_branch
        and requirement.target_head == package.target.target_head
        and requirement.target_tree == package.target.target_tree
        and requirement.signable_payload_sha256
        == package.signature_envelope.signable_payload_sha256
    ):
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_requirement_package_mismatch",
            "Approval Requirement 未绑定 exact Promotion Package。",
        )


def _require_response_matches(
    requirement: EvolutionPromotionApprovalRequirement,
    step: EvolutionPromotionApprovalStep,
    response: EvolutionPromotionApprovalResponseReceipt,
) -> None:
    if not (
        response.workspace_root == requirement.workspace_root
        and response.requirement_id == requirement.requirement_id
        and response.requirement_sha256 == requirement.requirement_sha256
        and response.package_id == requirement.package_id
        and response.package_sha256 == requirement.package_sha256
        and response.role is step.role
        and response.step_order == step.order
        and response.reasons == step.reasons
        and response.signature_entry.role is step.role
        and response.signature_entry.required is step.signature_required
        and response.signature_entry.signable_payload_sha256 == requirement.signable_payload_sha256
        and response.requirement_expires_at == requirement.expires_at
    ):
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_response_mismatch",
            f"{step.role.value} Approval Response 未绑定 exact Requirement step。",
        )


def _require_signature_matches(
    response: EvolutionPromotionApprovalResponseReceipt,
    signature: EvolutionApprovalSignatureReceiptView,
) -> None:
    payload = signature.receipt.challenge.payload
    if not (
        payload.approval_response_id == response.receipt_id
        and payload.approval_response_sha256 == response.receipt_sha256
        and payload.requirement_id == response.requirement_id
        and payload.requirement_sha256 == response.requirement_sha256
        and payload.package_id == response.package_id
        and payload.package_sha256 == response.package_sha256
        and payload.role is response.role
        and payload.response == EvolutionPromotionApprovalResponse.APPROVE.value
        and signature.receipt.signature_verified
        and signature.receipt.identity_binding_verified
        and signature.receipt.role_binding_verified
    ):
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_signature_mismatch",
            f"{response.role.value} Signature Receipt 未绑定 exact Approval Response。",
        )


def _decision_source_projection(
    value: EvolutionPromotionApprovalDecisionReceipt | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(value, EvolutionPromotionApprovalDecisionReceipt):
        payload = value.model_dump(mode="json")
    else:
        payload = dict(value)
    keys = (
        "workspace_root",
        "requirement_id",
        "requirement_sha256",
        "requirement_policy_version",
        "package_id",
        "package_sha256",
        "target_branch",
        "target_head",
        "target_tree",
        "package_current",
        "input_active",
        "reflection_active",
        "target_current",
        "requirement_expired",
        "role_decisions",
        "technical_gate_decisions",
        "required_approvals",
        "approvals_collected",
        "required_signatures",
        "signatures_collected",
        "missing_roles",
        "blocking_gates",
        "status",
    )
    return {key: payload[key] for key in keys}


async def _assert_source_indexes(
    db: aiosqlite.Connection,
    *,
    item: EvolutionPromotionApprovalDecisionReceipt,
    requirement: EvolutionPromotionApprovalRequirement,
) -> None:
    requirement_row = await (
        await db.execute(
            "SELECT requirement_sha256, package_sha256 FROM "
            "evolution_promotion_approval_requirements WHERE requirement_id = ?",
            (item.requirement_id,),
        )
    ).fetchone()
    package_row = await (
        await db.execute(
            "SELECT package_sha256 FROM evolution_promotion_packages WHERE package_id = ?",
            (item.package_id,),
        )
    ).fetchone()
    if not (
        requirement_row is not None
        and requirement_row["requirement_sha256"] == item.requirement_sha256
        and requirement_row["package_sha256"] == item.package_sha256
        and package_row is not None
        and package_row["package_sha256"] == item.package_sha256
    ):
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_source_index_changed",
            "Requirement 或 Package authority index 已变化。",
        )
    for role in item.role_decisions:
        response_row = await (
            await db.execute(
                "SELECT receipt_id, receipt_sha256 FROM "
                "evolution_promotion_approval_responses "
                "WHERE requirement_id = ? AND role = ?",
                (item.requirement_id, role.role.value),
            )
        ).fetchone()
        if role.response_present:
            if not (
                response_row is not None
                and response_row["receipt_id"] == role.response_receipt_id
                and response_row["receipt_sha256"] == role.response_receipt_sha256
            ):
                raise EvolutionPromotionApprovalDecisionError(
                    "approval_decision_response_index_changed",
                    f"{role.role.value} Response authority 已变化。",
                )
        elif response_row is not None:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_response_index_changed",
                f"{role.role.value} Response 在聚合期间出现。",
            )
        if not role.response_present:
            continue
        signature_row = await (
            await db.execute(
                "SELECT receipt_id, receipt_sha256 FROM "
                "evolution_approval_signature_receipts WHERE approval_response_id = ?",
                (role.response_receipt_id,),
            )
        ).fetchone()
        if role.signature_present:
            if not (
                signature_row is not None
                and signature_row["receipt_id"] == role.signature_receipt_id
                and signature_row["receipt_sha256"] == role.signature_receipt_sha256
            ):
                raise EvolutionPromotionApprovalDecisionError(
                    "approval_decision_signature_index_changed",
                    f"{role.role.value} Signature authority 已变化。",
                )
            if role.counts_toward_quorum:
                principal_row = await (
                    await db.execute(
                        "SELECT latest_event_sha256, state, key_id, key_generation, roles_json "
                        "FROM evolution_approval_principals WHERE workspace_root = ? "
                        "AND principal_id = (SELECT principal_id FROM "
                        "evolution_approval_signature_receipts WHERE receipt_id = ?)",
                        (item.workspace_root, role.signature_receipt_id),
                    )
                ).fetchone()
                if principal_row is None:
                    raise EvolutionPromotionApprovalDecisionError(
                        "approval_decision_principal_changed",
                        f"{role.role.value} Principal authority 已缺失。",
                    )
                roles = json.loads(str(principal_row["roles_json"]))
                signature_json = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_approval_signature_receipts "
                        "WHERE receipt_id = ?",
                        (role.signature_receipt_id,),
                    )
                ).fetchone()
                payload = json.loads(str(signature_json["receipt_json"]))["challenge"]["payload"]
                if not (
                    principal_row["latest_event_sha256"] == payload["principal_event_sha256"]
                    and principal_row["state"] == EvolutionApprovalPrincipalState.ACTIVE.value
                    and principal_row["key_id"] == payload["key_id"]
                    and int(principal_row["key_generation"]) == payload["key_generation"]
                    and role.role.value in roles
                ):
                    raise EvolutionPromotionApprovalDecisionError(
                        "approval_decision_principal_changed",
                        f"{role.role.value} Principal/key/role authority 已变化。",
                    )
        elif signature_row is not None:
            raise EvolutionPromotionApprovalDecisionError(
                "approval_decision_signature_index_changed",
                f"{role.role.value} Signature 在聚合期间出现。",
            )


async def _ensure_upstream_schemas(db: aiosqlite.Connection) -> None:
    await _ensure_package_schema(db)
    await _ensure_requirement_schema(db)
    await _ensure_response_schema(db)
    await _ensure_signature_schema(db)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_promotion_approval_decisions (
            decision_id TEXT PRIMARY KEY,
            decision_sha256 TEXT NOT NULL,
            source_set_sha256 TEXT NOT NULL,
            requirement_id TEXT NOT NULL,
            requirement_sha256 TEXT NOT NULL,
            package_id TEXT NOT NULL,
            package_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            previous_decision_id TEXT NOT NULL,
            status TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            UNIQUE(requirement_id, sequence),
            UNIQUE(requirement_id, source_set_sha256)
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionPromotionApprovalDecisionReceipt:
    encoded = str(row["decision_json"])
    _require_bounded(encoded)
    item = EvolutionPromotionApprovalDecisionReceipt.model_validate_json(encoded)
    if not (
        row["decision_id"] == item.decision_id
        and row["decision_sha256"] == item.decision_sha256
        and row["source_set_sha256"] == item.source_set_sha256
        and row["requirement_id"] == item.requirement_id
        and row["requirement_sha256"] == item.requirement_sha256
        and row["package_id"] == item.package_id
        and row["package_sha256"] == item.package_sha256
        and row["workspace_root"] == item.workspace_root
        and int(row["sequence"]) == item.sequence
        and row["previous_decision_id"] == item.previous_decision_id
        and row["status"] == item.status.value
        and row["decided_at"] == item.decided_at
    ):
        raise ValueError("Approval Decision Store index 不一致。")
    return item


def _validated_receipt(
    value: EvolutionPromotionApprovalDecisionReceipt,
) -> EvolutionPromotionApprovalDecisionReceipt:
    try:
        return EvolutionPromotionApprovalDecisionReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_artifact_invalid",
            "Approval Decision Receipt 无效。",
        ) from exc


def _require_bounded(encoded: str) -> None:
    if len(encoded.encode("utf-8")) > _MAX_DECISION_BYTES:
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_oversized",
            "Approval Decision Receipt 超过 512 KiB 上限。",
        )


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _aware_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EvolutionPromotionApprovalDecisionError(
            "approval_decision_clock_invalid",
            f"{label} 必须包含时区。",
        )
    return value.astimezone(UTC)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_PROMOTION_APPROVAL_DECISION_POLICY",
    "EvolutionPromotionApprovalDecisionBuilder",
    "EvolutionPromotionApprovalDecisionError",
    "EvolutionPromotionApprovalDecisionReceipt",
    "EvolutionPromotionApprovalDecisionService",
    "EvolutionPromotionApprovalDecisionStatus",
    "EvolutionPromotionApprovalDecisionStore",
    "EvolutionPromotionApprovalDecisionView",
    "EvolutionPromotionApprovalRoleDecision",
    "EvolutionPromotionApprovalRoleOutcome",
    "EvolutionPromotionTechnicalGateDecision",
    "EvolutionPromotionTechnicalGateState",
    "render_evolution_promotion_approval_decision",
]
