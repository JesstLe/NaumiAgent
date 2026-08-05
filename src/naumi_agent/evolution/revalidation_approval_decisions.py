"""Deterministic aggregation of fresh approval responses and signatures."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_requests import EvolutionPromotionApprovalResponse
from naumi_agent.evolution.approval_requirements import EvolutionPromotionApprovalRole
from naumi_agent.evolution.revalidation_approval_requests import (
    EvolutionRevalidationApprovalRequestError,
    EvolutionRevalidationApprovalResponseStore,
)
from naumi_agent.evolution.revalidation_approval_requirements import (
    EvolutionRevalidationApprovalRequirementError,
    EvolutionRevalidationApprovalRequirementService,
    EvolutionRevalidationApprovalTechnicalGate,
)
from naumi_agent.evolution.revalidation_approval_signatures import (
    EvolutionRevalidationApprovalSignatureError,
    EvolutionRevalidationApprovalSignatureReceipt,
    EvolutionRevalidationApprovalSignatureService,
    EvolutionRevalidationApprovalSignatureStore,
)

EVOLUTION_REVALIDATION_APPROVAL_DECISION_POLICY = "evolution-revalidation-approval-decision-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationApprovalDecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    STALE = "stale"


class EvolutionRevalidationApprovalRoleOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    RESPONSE_MISSING = "response_missing"
    IDENTITY_UNVERIFIED = "identity_unverified"
    SIGNATURE_MISSING = "signature_missing"
    SIGNATURE_STALE = "signature_stale"


class EvolutionRevalidationApprovalRoleDecision(_StrictModel):
    order: int = Field(ge=1, le=5)
    role: EvolutionPromotionApprovalRole
    response_id: str = Field(default="", pattern=r"^(?:|evreapprovalresp_[0-9a-f]{24})$")
    response_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    response: str = Field(default="", pattern=r"^(?:|approve|request_changes|reject)$")
    signature_required: bool
    signature_receipt_id: str = Field(default="", pattern=r"^(?:|evreapprovalsig_[0-9a-f]{24})$")
    signature_receipt_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    identity_verified: bool
    signature_current: bool
    outcome: EvolutionRevalidationApprovalRoleOutcome
    counts_toward_quorum: bool

    @model_validator(mode="after")
    def _exact(self) -> Self:
        has_response = bool(self.response_id)
        if has_response is not bool(self.response_sha256 and self.response):
            raise ValueError("Fresh Decision response binding 不完整。")
        has_signature = bool(self.signature_receipt_id)
        if has_signature is not bool(self.signature_receipt_sha256):
            raise ValueError("Fresh Decision signature binding 不完整。")
        if self.signature_required is (self.role is EvolutionPromotionApprovalRole.USER):
            raise ValueError("Fresh Decision 仅允许 user 免签名。")
        if self.counts_toward_quorum is not (
            self.outcome is EvolutionRevalidationApprovalRoleOutcome.APPROVED
        ):
            raise ValueError("Fresh Decision role quorum 投影不一致。")
        if self.outcome is EvolutionRevalidationApprovalRoleOutcome.APPROVED:
            if not self.identity_verified or (
                self.signature_required and not self.signature_current
            ):
                raise ValueError("Fresh Decision approved role 缺少 current identity/signature。")
        return self


class EvolutionRevalidationApprovalGateDecision(_StrictModel):
    gate: EvolutionRevalidationApprovalTechnicalGate
    satisfied: bool
    evidence_requirement_sha256: str = Field(pattern=_SHA256_RE)


class EvolutionRevalidationApprovalDecisionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-approval-decision-v1"] = (
        EVOLUTION_REVALIDATION_APPROVAL_DECISION_POLICY
    )
    decision_id: str = Field(pattern=r"^evreapprovaldecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=_SHA256_RE)
    source_set_sha256: str = Field(pattern=_SHA256_RE)
    sequence: int = Field(ge=1, le=10_000)
    previous_decision_id: str = Field(
        default="", pattern=r"^(?:|evreapprovaldecision_[0-9a-f]{24})$"
    )
    previous_decision_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    requirement_id: str = Field(pattern=r"^evreapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    target_branch: str = Field(min_length=1, max_length=255)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    requirement_expired: bool
    role_decisions: tuple[EvolutionRevalidationApprovalRoleDecision, ...] = Field(
        min_length=4, max_length=5
    )
    gate_decisions: tuple[EvolutionRevalidationApprovalGateDecision, ...] = Field(
        min_length=5, max_length=5
    )
    required_approvals: int = Field(ge=4, le=5)
    approvals_collected: int = Field(ge=0, le=5)
    required_signatures: int = Field(ge=3, le=4)
    signatures_collected: int = Field(ge=0, le=4)
    missing_roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(max_length=5)
    blocking_gates: tuple[EvolutionRevalidationApprovalTechnicalGate, ...] = Field(max_length=5)
    status: EvolutionRevalidationApprovalDecisionStatus
    approval_decided: bool
    final_quorum_reached: bool
    staged_rollout_eligible: bool
    decided_at: str = Field(min_length=1, max_length=100)
    prior_decision_reused: Literal[False] = False
    promotion_authority: Literal[False] = False
    rollout_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Decision workspace 必须 canonical。")
        _aware(self.decided_at)
        if (self.sequence == 1) is bool(self.previous_decision_id):
            raise ValueError("Fresh Decision chain link 不一致。")
        if bool(self.previous_decision_id) is not bool(self.previous_decision_sha256):
            raise ValueError("Fresh Decision previous digest 不一致。")
        if tuple(item.order for item in self.role_decisions) != tuple(
            range(1, len(self.role_decisions) + 1)
        ):
            raise ValueError("Fresh Decision role order 不连续。")
        roles = tuple(item.role for item in self.role_decisions)
        if len(roles) != len(set(roles)):
            raise ValueError("Fresh Decision role 重复。")
        gates = tuple(item.gate for item in self.gate_decisions)
        if gates != tuple(EvolutionRevalidationApprovalTechnicalGate):
            raise ValueError("Fresh Decision technical gates 不完整。")
        approvals = sum(item.counts_toward_quorum for item in self.role_decisions)
        signatures = sum(
            bool(item.signature_receipt_id and item.signature_current)
            for item in self.role_decisions
        )
        missing = tuple(
            item.role
            for item in self.role_decisions
            if item.outcome
            in {
                EvolutionRevalidationApprovalRoleOutcome.RESPONSE_MISSING,
                EvolutionRevalidationApprovalRoleOutcome.IDENTITY_UNVERIFIED,
                EvolutionRevalidationApprovalRoleOutcome.SIGNATURE_MISSING,
            }
        )
        blocking = tuple(item.gate for item in self.gate_decisions if not item.satisfied)
        status = _status(self.requirement_expired, self.role_decisions, self.gate_decisions)
        if not (
            self.required_approvals == len(self.role_decisions)
            and self.approvals_collected == approvals
            and self.required_signatures
            == sum(item.signature_required for item in self.role_decisions)
            and self.signatures_collected == signatures
            and self.missing_roles == missing
            and self.blocking_gates == blocking
            and self.status is status
            and self.approval_decided
            is (
                status
                in {
                    EvolutionRevalidationApprovalDecisionStatus.APPROVED,
                    EvolutionRevalidationApprovalDecisionStatus.REJECTED,
                    EvolutionRevalidationApprovalDecisionStatus.CHANGES_REQUESTED,
                }
            )
            and self.final_quorum_reached
            is (status is EvolutionRevalidationApprovalDecisionStatus.APPROVED)
            and self.staged_rollout_eligible
            is (status is EvolutionRevalidationApprovalDecisionStatus.APPROVED)
        ):
            raise ValueError("Fresh Decision aggregate projection 不一致。")
        if self.source_set_sha256 != _source_digest(self):
            raise ValueError("Fresh Decision source-set digest 不一致。")
        digest = _digest(self.model_dump(mode="json", exclude={"decision_id", "decision_sha256"}))
        if (
            self.decision_sha256 != digest
            or self.decision_id != f"evreapprovaldecision_{digest[:24]}"
        ):
            raise ValueError("Fresh Decision identity 不一致。")
        return self


class EvolutionRevalidationApprovalDecisionView(_StrictModel):
    receipt: EvolutionRevalidationApprovalDecisionReceipt
    source_current: bool
    current_status: EvolutionRevalidationApprovalDecisionStatus
    current_staged_rollout_eligible: bool

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.source_current and self.current_status is not self.receipt.status:
            raise ValueError("Fresh Decision current status 不一致。")
        if self.current_staged_rollout_eligible is not (
            self.source_current
            and self.current_status is EvolutionRevalidationApprovalDecisionStatus.APPROVED
        ):
            raise ValueError("Fresh Decision rollout eligibility 不一致。")
        return self


class EvolutionRevalidationApprovalDecisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationApprovalDecisionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(self, decision_id: str) -> EvolutionRevalidationApprovalDecisionReceipt | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT decision_json FROM "
                    "evolution_revalidation_approval_decisions "
                    "WHERE decision_id = ?",
                    (decision_id,),
                )
            ).fetchone()
        return None if row is None else _receipt(row["decision_json"])

    async def latest(
        self, requirement_id: str
    ) -> EvolutionRevalidationApprovalDecisionReceipt | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT decision_json FROM evolution_revalidation_approval_decisions "
                    "WHERE requirement_id = ? ORDER BY sequence DESC LIMIT 1",
                    (requirement_id,),
                )
            ).fetchone()
        return None if row is None else _receipt(row["decision_json"])

    async def record(
        self, receipt: EvolutionRevalidationApprovalDecisionReceipt
    ) -> EvolutionRevalidationApprovalDecisionReceipt:
        item = EvolutionRevalidationApprovalDecisionReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_oversized", "Fresh Decision 超过 512 KiB。"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_dependencies(db, item)
            same = await (
                await db.execute(
                    "SELECT decision_json FROM "
                    "evolution_revalidation_approval_decisions "
                    "WHERE requirement_id = ? AND source_set_sha256 = ?",
                    (item.requirement_id, item.source_set_sha256),
                )
            ).fetchone()
            if same is not None:
                await db.rollback()
                return _receipt(same["decision_json"])
            latest = await (
                await db.execute(
                    "SELECT decision_id, decision_sha256, sequence FROM "
                    "evolution_revalidation_approval_decisions "
                    "WHERE requirement_id = ? ORDER BY sequence DESC LIMIT 1",
                    (item.requirement_id,),
                )
            ).fetchone()
            expected = (
                (1, "", "")
                if latest is None
                else (int(latest["sequence"]) + 1, latest["decision_id"], latest["decision_sha256"])
            )
            if (
                item.sequence,
                item.previous_decision_id,
                item.previous_decision_sha256,
            ) != expected:
                await db.rollback()
                raise EvolutionRevalidationApprovalDecisionError(
                    "fresh_decision_chain_race", "Fresh Decision chain 已变化。"
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_approval_decisions "
                "(decision_id, decision_sha256, source_set_sha256, "
                "requirement_id, sequence, decision_json, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.decision_id,
                    item.decision_sha256,
                    item.source_set_sha256,
                    item.requirement_id,
                    item.sequence,
                    encoded,
                    item.decided_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationApprovalDecisionService:
    def __init__(
        self,
        *,
        requirement_service: EvolutionRevalidationApprovalRequirementService,
        response_store: EvolutionRevalidationApprovalResponseStore,
        signature_store: EvolutionRevalidationApprovalSignatureStore,
        signature_service: EvolutionRevalidationApprovalSignatureService,
        decision_store: EvolutionRevalidationApprovalDecisionStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            requirement_service,
            EvolutionRevalidationApprovalRequirementService,
        ):
            raise TypeError("Fresh Decision service 需要 Requirement Service。")
        if not isinstance(response_store, EvolutionRevalidationApprovalResponseStore):
            raise TypeError("Fresh Decision service 需要 Response Store。")
        if not isinstance(
            signature_store,
            EvolutionRevalidationApprovalSignatureStore,
        ):
            raise TypeError("Fresh Decision service 需要 Signature Store。")
        if not isinstance(
            signature_service,
            EvolutionRevalidationApprovalSignatureService,
        ):
            raise TypeError("Fresh Decision service 需要 Signature Service。")
        if not isinstance(decision_store, EvolutionRevalidationApprovalDecisionStore):
            raise TypeError("Fresh Decision service 需要 Decision Store。")
        if clock is not None and not callable(clock):
            raise TypeError("Fresh Decision clock 必须可调用。")
        self.requirement_service = requirement_service
        self.response_store = response_store
        self.signature_store = signature_store
        self.signature_service = signature_service
        self.decision_store = decision_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def execute(
        self, *, workspace_root: str | Path, contract_id: str, requirement_id: str
    ) -> EvolutionRevalidationApprovalDecisionView:
        lock = self._locks.setdefault(requirement_id, asyncio.Lock())
        async with lock:
            for _attempt in range(3):
                requirement, roles, gates = await self._collect(
                    workspace_root, contract_id, requirement_id
                )
                latest = await self.decision_store.latest(requirement_id)
                proposed = _build(requirement, roles, gates, latest, self._now())
                refreshed_requirement, refreshed_roles, refreshed_gates = await self._collect(
                    workspace_root, contract_id, requirement_id
                )
                refreshed = _build(
                    refreshed_requirement, refreshed_roles, refreshed_gates, latest, self._now()
                )
                if proposed.source_set_sha256 != refreshed.source_set_sha256:
                    continue
                try:
                    stored = await self.decision_store.record(refreshed)
                except EvolutionRevalidationApprovalDecisionError as exc:
                    if exc.code == "fresh_decision_chain_race":
                        continue
                    raise
                return await self.inspect(
                    workspace_root=workspace_root,
                    contract_id=contract_id,
                    decision_id=stored.decision_id,
                )
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_authority_unstable", "Fresh Approval authority 持续变化。"
            )

    async def inspect(
        self, *, workspace_root: str | Path, contract_id: str, decision_id: str
    ) -> EvolutionRevalidationApprovalDecisionView:
        stored = await self.decision_store.get(decision_id)
        if stored is None:
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_missing", "Fresh Decision 不存在。"
            )
        try:
            requirement, roles, gates = await self._collect(
                workspace_root, contract_id, stored.requirement_id
            )
        except EvolutionRevalidationApprovalDecisionError as exc:
            if exc.code == "fresh_decision_requirement_stale":
                return EvolutionRevalidationApprovalDecisionView(
                    receipt=stored,
                    source_current=False,
                    current_status=EvolutionRevalidationApprovalDecisionStatus.STALE,
                    current_staged_rollout_eligible=False,
                )
            raise
        current = _build(requirement, roles, gates, None, _aware(stored.decided_at))
        source_current = current.source_set_sha256 == stored.source_set_sha256
        return EvolutionRevalidationApprovalDecisionView(
            receipt=stored,
            source_current=source_current,
            current_status=current.status,
            current_staged_rollout_eligible=source_current
            and current.status is EvolutionRevalidationApprovalDecisionStatus.APPROVED,
        )

    async def _collect(self, workspace_root, contract_id, requirement_id):
        try:
            requirement = await self.requirement_service.issue(contract_id=contract_id)
        except EvolutionRevalidationApprovalRequirementError as exc:
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_requirement_failed", "无法读取 Fresh Requirement。"
            ) from exc
        if requirement.requirement_id != requirement_id:
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_requirement_stale", "Fresh Requirement 已被替换。"
            )
        expired = self._now() >= _aware(requirement.expires_at)
        role_decisions = []
        for step in requirement.steps:
            try:
                response = await self.response_store.get_by_requirement_role(
                    requirement_id, step.role
                )
            except EvolutionRevalidationApprovalRequestError as exc:
                raise EvolutionRevalidationApprovalDecisionError(
                    "fresh_decision_response_failed", f"无法读取 {step.role.value} Fresh Response。"
                ) from exc
            role_decisions.append(await self._role(workspace_root, contract_id, step, response))
        gates = tuple(
            EvolutionRevalidationApprovalGateDecision(
                gate=gate,
                satisfied=not expired,
                evidence_requirement_sha256=requirement.requirement_sha256,
            )
            for gate in EvolutionRevalidationApprovalTechnicalGate
        )
        return requirement, tuple(role_decisions), gates

    async def _role(self, workspace_root, contract_id, step, response):
        if response is None:
            return _role_decision(
                step, None, None, EvolutionRevalidationApprovalRoleOutcome.RESPONSE_MISSING
            )
        if response.response is EvolutionPromotionApprovalResponse.REJECT:
            return _role_decision(
                step, response, None, EvolutionRevalidationApprovalRoleOutcome.REJECTED
            )
        if response.response is EvolutionPromotionApprovalResponse.REQUEST_CHANGES:
            return _role_decision(
                step, response, None, EvolutionRevalidationApprovalRoleOutcome.CHANGES_REQUESTED
            )
        if not step.signature_required:
            outcome = (
                EvolutionRevalidationApprovalRoleOutcome.APPROVED
                if response.counts_toward_role_quorum
                else EvolutionRevalidationApprovalRoleOutcome.IDENTITY_UNVERIFIED
            )
            return _role_decision(step, response, None, outcome)
        signature = await self.signature_store.get_receipt_by_response(response.receipt_id)
        if signature is None:
            return _role_decision(
                step, response, None, EvolutionRevalidationApprovalRoleOutcome.SIGNATURE_MISSING
            )
        try:
            view = await self.signature_service.inspect(
                workspace_root=workspace_root,
                contract_id=contract_id,
                receipt_id=signature.receipt_id,
            )
        except EvolutionRevalidationApprovalSignatureError:
            return _role_decision(
                step, response, signature, EvolutionRevalidationApprovalRoleOutcome.SIGNATURE_STALE
            )
        outcome = (
            EvolutionRevalidationApprovalRoleOutcome.APPROVED
            if view.eligible_for_decision_aggregation
            else EvolutionRevalidationApprovalRoleOutcome.SIGNATURE_STALE
        )
        return _role_decision(step, response, signature, outcome)

    def _now(self):
        value = self.clock()
        if value.utcoffset() is None:
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_clock_invalid", "Fresh Decision clock 必须包含时区。"
            )
        return value.astimezone(UTC)


def _role_decision(step, response, signature, outcome):
    return EvolutionRevalidationApprovalRoleDecision(
        order=step.order,
        role=step.role,
        response_id="" if response is None else response.receipt_id,
        response_sha256="" if response is None else response.receipt_sha256,
        response="" if response is None else response.response.value,
        signature_required=step.signature_required,
        signature_receipt_id="" if signature is None else signature.receipt_id,
        signature_receipt_sha256="" if signature is None else signature.receipt_sha256,
        identity_verified=outcome is EvolutionRevalidationApprovalRoleOutcome.APPROVED,
        signature_current=bool(
            signature is not None and outcome is EvolutionRevalidationApprovalRoleOutcome.APPROVED
        ),
        outcome=outcome,
        counts_toward_quorum=outcome is EvolutionRevalidationApprovalRoleOutcome.APPROVED,
    )


def _status(expired, roles, gates):
    if expired or any(
        item.outcome is EvolutionRevalidationApprovalRoleOutcome.SIGNATURE_STALE for item in roles
    ):
        return EvolutionRevalidationApprovalDecisionStatus.STALE
    if any(item.outcome is EvolutionRevalidationApprovalRoleOutcome.REJECTED for item in roles):
        return EvolutionRevalidationApprovalDecisionStatus.REJECTED
    if any(
        item.outcome is EvolutionRevalidationApprovalRoleOutcome.CHANGES_REQUESTED for item in roles
    ):
        return EvolutionRevalidationApprovalDecisionStatus.CHANGES_REQUESTED
    if any(not item.counts_toward_quorum for item in roles) or any(
        not item.satisfied for item in gates
    ):
        return EvolutionRevalidationApprovalDecisionStatus.PENDING
    return EvolutionRevalidationApprovalDecisionStatus.APPROVED


def _build(requirement, roles, gates, latest, now):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_APPROVAL_DECISION_POLICY,
        "sequence": 1 if latest is None else latest.sequence + 1,
        "previous_decision_id": "" if latest is None else latest.decision_id,
        "previous_decision_sha256": "" if latest is None else latest.decision_sha256,
        "workspace_root": requirement.workspace_root,
        "contract_id": requirement.contract_id,
        "requirement_id": requirement.requirement_id,
        "requirement_sha256": requirement.requirement_sha256,
        "promotion_input_id": requirement.promotion_input_id,
        "promotion_input_sha256": requirement.promotion_input_sha256,
        "target_branch": requirement.target_branch,
        "target_head": requirement.target_head,
        "target_tree_sha256": requirement.target_tree_sha256,
        "requirement_expired": now >= _aware(requirement.expires_at),
        "role_decisions": roles,
        "gate_decisions": gates,
        "required_approvals": len(roles),
        "approvals_collected": sum(item.counts_toward_quorum for item in roles),
        "required_signatures": sum(item.signature_required for item in roles),
        "signatures_collected": sum(
            bool(item.signature_receipt_id and item.signature_current) for item in roles
        ),
        "missing_roles": tuple(
            item.role
            for item in roles
            if item.outcome
            in {
                EvolutionRevalidationApprovalRoleOutcome.RESPONSE_MISSING,
                EvolutionRevalidationApprovalRoleOutcome.IDENTITY_UNVERIFIED,
                EvolutionRevalidationApprovalRoleOutcome.SIGNATURE_MISSING,
            }
        ),
        "blocking_gates": tuple(item.gate for item in gates if not item.satisfied),
        "status": _status(now >= _aware(requirement.expires_at), roles, gates),
        "approval_decided": _status(now >= _aware(requirement.expires_at), roles, gates)
        in {
            EvolutionRevalidationApprovalDecisionStatus.APPROVED,
            EvolutionRevalidationApprovalDecisionStatus.REJECTED,
            EvolutionRevalidationApprovalDecisionStatus.CHANGES_REQUESTED,
        },
        "final_quorum_reached": _status(now >= _aware(requirement.expires_at), roles, gates)
        is EvolutionRevalidationApprovalDecisionStatus.APPROVED,
        "staged_rollout_eligible": _status(now >= _aware(requirement.expires_at), roles, gates)
        is EvolutionRevalidationApprovalDecisionStatus.APPROVED,
        "decided_at": now.isoformat(),
        "prior_decision_reused": False,
        "promotion_authority": False,
        "rollout_executed": False,
        "git_write_executed": False,
        "merge_executed": False,
        "push_executed": False,
        "publish_executed": False,
        "llm_generated": False,
    }
    normalized = {
        **core,
        "role_decisions": [item.model_dump(mode="json") for item in roles],
        "gate_decisions": [item.model_dump(mode="json") for item in gates],
    }
    source = _source_digest(normalized)
    payload = {**core, "source_set_sha256": source}
    digest = _digest({**normalized, "source_set_sha256": source})
    return EvolutionRevalidationApprovalDecisionReceipt.model_validate(
        {**payload, "decision_id": f"evreapprovaldecision_{digest[:24]}", "decision_sha256": digest}
    )


def _source_digest(item):
    data = (
        item.model_dump(mode="json")
        if isinstance(item, EvolutionRevalidationApprovalDecisionReceipt)
        else dict(item)
    )
    return _digest(
        {
            key: data[key]
            for key in (
                "workspace_root",
                "contract_id",
                "requirement_id",
                "requirement_sha256",
                "promotion_input_id",
                "promotion_input_sha256",
                "target_branch",
                "target_head",
                "target_tree_sha256",
                "requirement_expired",
                "role_decisions",
                "gate_decisions",
            )
        }
    )


async def _require_dependencies(db, item):
    row = await (
        await db.execute(
            "SELECT requirement_sha256 FROM "
            "evolution_revalidation_approval_requirements "
            "WHERE requirement_id = ?",
            (item.requirement_id,),
        )
    ).fetchone()
    if row is None or row["requirement_sha256"] != item.requirement_sha256:
        await db.rollback()
        raise EvolutionRevalidationApprovalDecisionError(
            "fresh_decision_requirement_mismatch", "Fresh Decision Requirement authority 不一致。"
        )
    for role in item.role_decisions:
        if not role.response_id:
            continue
        response = await (
            await db.execute(
                "SELECT receipt_sha256 FROM "
                "evolution_revalidation_approval_responses "
                "WHERE receipt_id = ? AND requirement_id = ? AND role = ?",
                (role.response_id, item.requirement_id, role.role.value),
            )
        ).fetchone()
        if response is None or response["receipt_sha256"] != role.response_sha256:
            await db.rollback()
            raise EvolutionRevalidationApprovalDecisionError(
                "fresh_decision_response_mismatch", "Fresh Decision Response authority 不一致。"
            )
        if role.signature_receipt_id:
            signature = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_approval_signature_receipts "
                    "WHERE receipt_id = ? AND approval_response_id = ?",
                    (role.signature_receipt_id, role.response_id),
                )
            ).fetchone()
            signed = None if signature is None else _receipt_signature(signature["receipt_json"])
            principal = (
                None
                if signed is None
                else await (
                    await db.execute(
                        "SELECT state, latest_event_id, latest_event_sha256, "
                        "key_id, key_generation, public_key_sha256, roles_json "
                        "FROM evolution_approval_principals "
                        "WHERE workspace_root = ? AND principal_id = ?",
                        (
                            item.workspace_root,
                            signed.principal.principal_id,
                        ),
                    )
                ).fetchone()
            )
            try:
                principal_roles = (
                    () if principal is None else tuple(json.loads(principal["roles_json"]))
                )
            except (TypeError, ValueError):
                principal_roles = ()
            payload = None if signed is None else signed.challenge.payload
            if not (
                signed is not None
                and signed.receipt_sha256 == role.signature_receipt_sha256
                and payload is not None
                and principal is not None
                and principal["state"] == "active"
                and principal["latest_event_id"] == payload.principal_event_id
                and principal["latest_event_sha256"] == payload.principal_event_sha256
                and principal["key_id"] == payload.key_id
                and principal["key_generation"] == payload.key_generation
                and principal["public_key_sha256"] == payload.public_key_sha256
                and role.role.value in principal_roles
            ):
                await db.rollback()
                raise EvolutionRevalidationApprovalDecisionError(
                    "fresh_decision_signature_mismatch",
                    "Fresh Decision Signature authority 不一致。",
                )


async def _ensure_schema(db):
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_revalidation_approval_decisions (
        decision_id TEXT PRIMARY KEY,
        decision_sha256 TEXT NOT NULL UNIQUE,
        source_set_sha256 TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        decision_json TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        UNIQUE(requirement_id, sequence),
        UNIQUE(requirement_id, source_set_sha256))"""
    )
    await db.commit()


def _receipt(value):
    return EvolutionRevalidationApprovalDecisionReceipt.model_validate_json(value)


def _receipt_signature(value):
    return EvolutionRevalidationApprovalSignatureReceipt.model_validate_json(value)


def _aware(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_APPROVAL_DECISION_POLICY",
    "EvolutionRevalidationApprovalDecisionError",
    "EvolutionRevalidationApprovalDecisionReceipt",
    "EvolutionRevalidationApprovalDecisionService",
    "EvolutionRevalidationApprovalDecisionStatus",
    "EvolutionRevalidationApprovalDecisionStore",
    "EvolutionRevalidationApprovalDecisionView",
    "EvolutionRevalidationApprovalGateDecision",
    "EvolutionRevalidationApprovalRoleDecision",
    "EvolutionRevalidationApprovalRoleOutcome",
]
