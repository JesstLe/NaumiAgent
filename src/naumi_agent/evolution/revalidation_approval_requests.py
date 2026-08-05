"""Fresh role-scoped approval interactions and response receipts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalIdentityAssurance,
    EvolutionPromotionApprovalResponse,
    EvolutionPromotionSignatureReceiptEntry,
)
from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRole,
)
from naumi_agent.evolution.revalidation_approval_requirements import (
    EvolutionRevalidationApprovalReason,
    EvolutionRevalidationApprovalRequirement,
    EvolutionRevalidationApprovalRequirementError,
    EvolutionRevalidationApprovalRequirementService,
    EvolutionRevalidationApprovalStep,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_REVALIDATION_APPROVAL_REQUEST_POLICY = (
    "evolution-revalidation-approval-request-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 256 * 1024
_INTERACTION_RE = re.compile(
    r"^ask-evreapproval-([0-9a-f]{24})-"
    r"(user|independent_reviewer|security_reviewer|data_owner|release_manager)-"
    r"([1-9][0-9]{0,3})$"
)

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationApprovalResponseReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-approval-request-v1"] = (
        EVOLUTION_REVALIDATION_APPROVAL_REQUEST_POLICY
    )
    receipt_id: str = Field(pattern=r"^evreapprovalresp_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    requirement_id: str = Field(pattern=r"^evreapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    approval_request_id: str = Field(pattern=r"^evreapprovalrequest_[0-9a-f]{24}$")
    role: EvolutionPromotionApprovalRole
    step_order: int = Field(ge=1, le=5)
    reasons: tuple[EvolutionRevalidationApprovalReason, ...] = Field(
        min_length=1,
        max_length=6,
    )
    response: EvolutionPromotionApprovalResponse
    interaction: HarnessInteractionRecord
    interaction_request_sha256: str = Field(pattern=_SHA256_RE)
    identity_assurance: EvolutionPromotionApprovalIdentityAssurance
    role_binding_verified: bool
    role_response_recorded: Literal[True] = True
    counts_toward_role_quorum: bool
    eligible_for_fresh_signature: bool
    signature_entry: EvolutionPromotionSignatureReceiptEntry
    requirement_expires_at: str = Field(min_length=1, max_length=100)
    recorded_at: str = Field(min_length=1, max_length=100)
    prior_response_reused: Literal[False] = False
    prior_signature_reused: Literal[False] = False
    overall_approval_decided: Literal[False] = False
    final_quorum_reached: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    contains_user_custom_text: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Approval Response workspace 必须 canonical。")
        interaction = self.interaction
        if not (
            interaction.state == "answered"
            and interaction.answer_kind == "option"
            and interaction.answer_value == self.response.value
            and interaction.answered_by == "user"
            and interaction.subject_kind == "tool"
            and interaction.subject_id == self.requirement_id
        ):
            raise ValueError("Fresh Approval Response 未绑定结构化用户答案。")
        match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
        if not (
            match is not None
            and match.group(1)
            == self.requirement_id.removeprefix("evreapprovalreq_")
            and match.group(2) == self.role.value
        ):
            raise ValueError("Fresh Approval Response interaction identity 无效。")
        actual_request_digest = _sha256_payload(
            _static_request_payload(interaction.request())
        )
        if not hmac.compare_digest(
            self.interaction_request_sha256,
            actual_request_digest,
        ):
            raise ValueError("Fresh Approval Request payload 摘要不一致。")
        expected_request_id = _approval_request_id(
            requirement_id=self.requirement_id,
            role=self.role,
            interaction_request_sha256=self.interaction_request_sha256,
        )
        if self.approval_request_id != expected_request_id:
            raise ValueError("Fresh Approval Request identity 不一致。")
        local_user = bool(
            self.role is EvolutionPromotionApprovalRole.USER
            and interaction.session_id
        )
        assurance = (
            EvolutionPromotionApprovalIdentityAssurance.LOCAL_SESSION_USER
            if local_user
            else EvolutionPromotionApprovalIdentityAssurance.UNVERIFIED_ROLE_CLAIM
        )
        if self.identity_assurance is not assurance:
            raise ValueError("Fresh Approval identity assurance 投影不一致。")
        if self.role_binding_verified is not local_user:
            raise ValueError("Fresh Approval role binding 投影不一致。")
        approved = self.response is EvolutionPromotionApprovalResponse.APPROVE
        if self.counts_toward_role_quorum is not bool(approved and local_user):
            raise ValueError("Fresh Approval response quorum 投影不一致。")
        if self.eligible_for_fresh_signature is not bool(
            approved and self.signature_entry.required
        ):
            raise ValueError("Fresh Approval signature eligibility 投影不一致。")
        if not (
            self.signature_entry.role is self.role
            and self.signature_entry.signable_payload_sha256
            and self.signature_entry.required
            is (self.role is not EvolutionPromotionApprovalRole.USER)
        ):
            raise ValueError("Fresh Approval signature entry 投影不一致。")
        if _aware(self.recorded_at) != _aware(interaction.answered_at):
            raise ValueError("Fresh Approval recorded_at 必须来自 interaction。")
        if _aware(self.recorded_at) >= _aware(self.requirement_expires_at):
            raise ValueError("到期后的答案不能形成 Fresh Approval Response。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Fresh Approval Response digest 不一致。")
        if self.receipt_id != f"evreapprovalresp_{digest[:24]}":
            raise ValueError("Fresh Approval Response identity 不一致。")
        return self


class EvolutionRevalidationApprovalResponseView(_StrictModel):
    receipt: EvolutionRevalidationApprovalResponseReceipt
    requirement_current: bool
    requirement_expired: bool
    eligible_for_fresh_signature: bool
    eligible_for_future_aggregation: bool

    @model_validator(mode="after")
    def _exact(self) -> Self:
        current = self.requirement_current and not self.requirement_expired
        if self.eligible_for_fresh_signature is not bool(
            current and self.receipt.eligible_for_fresh_signature
        ):
            raise ValueError("Fresh signature eligibility view 投影不一致。")
        if self.eligible_for_future_aggregation is not bool(
            current
            and self.receipt.response is EvolutionPromotionApprovalResponse.APPROVE
            and self.receipt.counts_toward_role_quorum
        ):
            raise ValueError("Fresh response aggregation eligibility 投影不一致。")
        return self


class EvolutionRevalidationApprovalRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationApprovalResponseStore:
    def __init__(self, db_path: str | Path, *, interaction_store: HarnessStore) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Fresh Approval Response Store 需要 Harness Store。")
        self._interaction_store = interaction_store

    async def get_by_requirement_role(
        self,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole | str,
    ) -> EvolutionRevalidationApprovalResponseReceipt | None:
        role_value = EvolutionPromotionApprovalRole(role)
        if re.fullmatch(r"evreapprovalreq_[0-9a-f]{24}", str(requirement_id)) is None:
            raise ValueError("Fresh Approval Requirement id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_revalidation_approval_responses "
                        "WHERE requirement_id = ? AND role = ?",
                        (requirement_id, role_value.value),
                    )
                ).fetchone()
            if row is None:
                return None
            receipt = _receipt_from_json(row["receipt_json"])
            authoritative = await self._interaction_store.get_interaction(
                workspace_root=receipt.workspace_root,
                interaction_id=receipt.interaction.interaction_id,
            )
            if authoritative != receipt.interaction:
                raise ValueError("Fresh Approval Response interaction authority 不一致。")
            return receipt
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_store_corrupt",
                "Fresh Approval Response 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        receipt: EvolutionRevalidationApprovalResponseReceipt,
        *,
        requirement: EvolutionRevalidationApprovalRequirement,
    ) -> EvolutionRevalidationApprovalResponseReceipt:
        try:
            item = EvolutionRevalidationApprovalResponseReceipt.model_validate_json(
                receipt.model_dump_json()
            )
            source = EvolutionRevalidationApprovalRequirement.model_validate_json(
                requirement.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_artifact_invalid",
                "Fresh Approval Response 或 Requirement 无效。",
            ) from exc
        if not _receipt_matches_requirement(item, source):
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_requirement_mismatch",
                "Fresh Approval Response 未绑定 exact Requirement。",
            )
        try:
            authoritative = await self._interaction_store.get_interaction(
                workspace_root=source.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_interaction_read_failed",
                "无法重读 Fresh Approval interaction authority。",
            ) from exc
        if authoritative != item.interaction:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_interaction_mismatch",
                "Fresh Approval Response 未绑定 Harness interaction authority。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_oversized",
                "Fresh Approval Response 超过 256 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT requirement_sha256 FROM "
                        "evolution_revalidation_approval_requirements "
                        "WHERE requirement_id = ? AND promotion_input_id = ?",
                        (item.requirement_id, item.promotion_input_id),
                    )
                ).fetchone()
                if row is None or row["requirement_sha256"] != item.requirement_sha256:
                    await db.rollback()
                    raise EvolutionRevalidationApprovalRequestError(
                        "fresh_approval_response_requirement_dependency_mismatch",
                        "Fresh Approval Requirement 持久化依赖不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_revalidation_approval_responses "
                        "WHERE requirement_id = ? AND role = ?",
                        (item.requirement_id, item.role.value),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _receipt_from_json(existing["receipt_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationApprovalRequestError(
                            "fresh_approval_response_conflict",
                            "同一 Fresh Requirement/Role 已绑定不同 Response。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_approval_responses "
                    "(receipt_id, receipt_sha256, requirement_id, requirement_sha256, "
                    "promotion_input_id, role, interaction_id, interaction_digest, "
                    "response, receipt_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.requirement_id,
                        item.requirement_sha256,
                        item.promotion_input_id,
                        item.role.value,
                        item.interaction.interaction_id,
                        item.interaction.digest(),
                        item.response.value,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationApprovalRequestError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_response_store_error",
                "Fresh Approval Response 无法持久化。",
            ) from exc
        return item


class EvolutionRevalidationApprovalRequestService:
    def __init__(
        self,
        *,
        requirement_service: EvolutionRevalidationApprovalRequirementService,
        interaction_store: HarnessStore,
        response_store: EvolutionRevalidationApprovalResponseStore,
        request_user_input: RequestUserInputCallback,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Fresh Approval Request service 需要 Harness Store。")
        if not callable(request_user_input):
            raise TypeError("Fresh Approval Request service 需要用户交互 callback。")
        self.requirement_service = requirement_service
        self.interaction_store = interaction_store
        self.response_store = response_store
        self.request_user_input = request_user_input
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        *,
        contract_id: str,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole | str,
    ) -> EvolutionRevalidationApprovalResponseView:
        try:
            role_value = EvolutionPromotionApprovalRole(role)
        except (TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_role_invalid",
                "Fresh Approval Request role 无效。",
            ) from exc
        key = f"{contract_id}:{requirement_id}:{role_value.value}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._execute_locked(
                contract_id=contract_id,
                requirement_id=requirement_id,
                role=role_value,
            )

    async def _execute_locked(
        self,
        *,
        contract_id: str,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole,
    ) -> EvolutionRevalidationApprovalResponseView:
        requirement = await self._current_requirement(contract_id, requirement_id)
        step = next((item for item in requirement.steps if item.role is role), None)
        if step is None:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_role_not_required",
                f"当前 Fresh Requirement 不需要 {role.value} 角色。",
            )
        expired = self._now() >= _aware(requirement.expires_at)
        existing = await self.response_store.get_by_requirement_role(
            requirement.requirement_id,
            role,
        )
        if existing is not None:
            return _response_view(existing, requirement_current=True, expired=expired)
        if expired:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_requirement_expired",
                "Fresh Approval Requirement 已过期。",
            )
        history = await self._interaction_history(requirement, step)
        answered = tuple(item for item in history if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_answer_ambiguous",
                "同一 Fresh Requirement/Role 存在多个已回答交互。",
            )
        if answered:
            return await self._record(requirement, step, answered[0])
        pending = next((item for item in history if item.state == "pending"), None)
        if pending is not None:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_interaction_pending",
                f"角色交互 {pending.interaction_id} 仍待回答。",
            )
        remaining = int((_aware(requirement.expires_at) - self._now()).total_seconds())
        if remaining < 3:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_requirement_expired",
                "Fresh Approval Requirement 已无足够时间创建交互。",
            )
        request = _approval_interaction_request(
            requirement,
            step,
            timeout_seconds=min(remaining, 604_800),
        )
        interaction_id = _next_interaction_id(requirement, step, history)
        payload = {
            **request.to_public_dict(),
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "tool",
            "_durable_subject_id": requirement.requirement_id,
        }
        try:
            await self.request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_interaction_unavailable",
                "当前界面无法创建持久 Fresh Approval 交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_interaction_invalid",
                "Fresh Approval 交互未通过运行时协议校验。",
            ) from exc
        try:
            interaction = await self.interaction_store.get_interaction(
                workspace_root=requirement.workspace_root,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_interaction_read_failed",
                "无法重读 Fresh Approval interaction authority。",
            ) from exc
        if interaction is None or interaction.state != "answered":
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_answer_not_committed",
                "用户答案尚未提交到 Harness authority。",
            )
        refreshed = await self._current_requirement(contract_id, requirement_id)
        if self._now() >= _aware(refreshed.expires_at):
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_answer_expired",
                "用户答案提交时 Fresh Requirement 已过期。",
            )
        return await self._record(requirement, step, interaction)

    async def _current_requirement(
        self,
        contract_id: str,
        requirement_id: str,
    ) -> EvolutionRevalidationApprovalRequirement:
        try:
            current = await self.requirement_service.issue(contract_id=contract_id)
        except EvolutionRevalidationApprovalRequirementError as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_requirement_read_failed",
                "无法读取 current Fresh Approval Requirement。",
            ) from exc
        if current.requirement_id != requirement_id:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_requirement_stale",
                "Fresh Approval Requirement 已被新的 authority 取代。",
            )
        return current

    async def _interaction_history(
        self,
        requirement: EvolutionRevalidationApprovalRequirement,
        step: EvolutionRevalidationApprovalStep,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            records = await self.interaction_store.list_interactions(
                workspace_root=requirement.workspace_root,
                subject_kind="tool",
                subject_ids=(requirement.requirement_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_interaction_history_failed",
                "无法读取 Fresh Approval 持久交互历史。",
            ) from exc
        static = _approval_interaction_request(requirement, step, timeout_seconds=None)
        return tuple(
            item
            for item in records
            if _interaction_matches(item, requirement, step, static)
        )

    async def _record(
        self,
        requirement: EvolutionRevalidationApprovalRequirement,
        step: EvolutionRevalidationApprovalStep,
        interaction: HarnessInteractionRecord,
    ) -> EvolutionRevalidationApprovalResponseView:
        receipt = _build_response(requirement, step, interaction)
        stored = await self.response_store.record(receipt, requirement=requirement)
        return _response_view(stored, requirement_current=True, expired=False)

    def _now(self) -> datetime:
        value = self.clock()
        if value.utcoffset() is None:
            raise EvolutionRevalidationApprovalRequestError(
                "fresh_approval_request_clock_invalid",
                "Fresh Approval Request clock 必须包含时区。",
            )
        return value.astimezone(UTC)


def _build_response(requirement, step, interaction):
    request = _approval_interaction_request(requirement, step, timeout_seconds=None)
    if not _interaction_matches(interaction, requirement, step, request):
        raise EvolutionRevalidationApprovalRequestError(
            "fresh_approval_response_interaction_mismatch",
            "已回答 interaction 未绑定 exact Fresh Approval Request。",
        )
    response = EvolutionPromotionApprovalResponse(interaction.answer_value)
    request_digest = _sha256_payload(_static_request_payload(request))
    local_user = bool(
        step.role is EvolutionPromotionApprovalRole.USER
        and interaction.session_id
    )
    signature_entry = EvolutionPromotionSignatureReceiptEntry(
        role=step.role,
        required=step.signature_required,
        signable_payload_sha256=requirement.signable_payload_sha256,
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_APPROVAL_REQUEST_POLICY,
        "workspace_root": requirement.workspace_root,
        "requirement_id": requirement.requirement_id,
        "requirement_sha256": requirement.requirement_sha256,
        "promotion_input_id": requirement.promotion_input_id,
        "promotion_input_sha256": requirement.promotion_input_sha256,
        "approval_request_id": _approval_request_id(
            requirement_id=requirement.requirement_id,
            role=step.role,
            interaction_request_sha256=request_digest,
        ),
        "role": step.role.value,
        "step_order": step.order,
        "reasons": [item.value for item in step.reasons],
        "response": response.value,
        "interaction": interaction.model_dump(mode="json"),
        "interaction_request_sha256": request_digest,
        "identity_assurance": (
            EvolutionPromotionApprovalIdentityAssurance.LOCAL_SESSION_USER.value
            if local_user
            else EvolutionPromotionApprovalIdentityAssurance.UNVERIFIED_ROLE_CLAIM.value
        ),
        "role_binding_verified": local_user,
        "role_response_recorded": True,
        "counts_toward_role_quorum": bool(
            local_user and response is EvolutionPromotionApprovalResponse.APPROVE
        ),
        "eligible_for_fresh_signature": bool(
            step.signature_required
            and response is EvolutionPromotionApprovalResponse.APPROVE
        ),
        "signature_entry": signature_entry.model_dump(mode="json"),
        "requirement_expires_at": requirement.expires_at,
        "recorded_at": interaction.answered_at,
        "prior_response_reused": False,
        "prior_signature_reused": False,
        "overall_approval_decided": False,
        "final_quorum_reached": False,
        "promotion_authority": False,
        "git_write_executed": False,
        "merge_executed": False,
        "push_executed": False,
        "publish_executed": False,
        "contains_user_custom_text": False,
        "llm_generated": False,
    }
    digest = _sha256_payload(core)
    try:
        return EvolutionRevalidationApprovalResponseReceipt.model_validate_json(
            json.dumps({
                **core,
                "receipt_id": f"evreapprovalresp_{digest[:24]}",
                "receipt_sha256": digest,
            }, ensure_ascii=False)
        )
    except ValueError as exc:
        raise EvolutionRevalidationApprovalRequestError(
            "fresh_approval_response_artifact_invalid",
            "Fresh Approval Response artifact 无法验证。",
        ) from exc


def _approval_interaction_request(requirement, step, *, timeout_seconds):
    reasons = "、".join(item.value for item in step.reasons)
    header_role = {
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER: "独立审查人",
    }.get(step.role, step.role.value)
    return normalize_interaction_request({
        "header": f"Fresh Evolution 审批 · {header_role}",
        "question": (
            f"请以 {step.role.value} 角色审查 Fresh Requirement "
            f"{requirement.requirement_id}（target "
            f"{requirement.target_branch}@{requirement.target_head[:12]}，"
            f"原因：{reasons}）。本选择只形成新的角色回答，不会合并、推送或发布。"
        ),
        "options": [
            {
                "value": "approve",
                "label": "同意本角色意见",
                "description": "记录新意见；专业角色仍需新的密码学签名。",
            },
            {
                "value": "request_changes",
                "label": "要求修改后重审",
                "description": "记录修改要求；当前候选不得继续 Promotion。",
            },
            {
                "value": "reject",
                "label": "拒绝本次 Promotion",
                "description": "记录拒绝意见；不执行 Git 或发布操作。",
            },
        ],
        "allow_custom": False,
        "custom_label": "不允许自定义审批文本",
        "timeout_seconds": timeout_seconds,
        "priority": "critical",
    })


def _interaction_matches(interaction, requirement, step, request) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and interaction.subject_kind == "tool"
        and interaction.subject_id == requirement.requirement_id
        and match.group(1)
        == requirement.requirement_id.removeprefix("evreapprovalreq_")
        and match.group(2) == step.role.value
        and _static_request_payload(interaction.request())
        == _static_request_payload(request)
        and interaction.request().timeout_seconds is not None
        and interaction.request().timeout_seconds <= requirement.validity_seconds
        and _aware(interaction.created_at) < _aware(requirement.expires_at)
    )


def _next_interaction_id(requirement, step, history) -> str:
    attempts = []
    for item in history:
        match = _INTERACTION_RE.fullmatch(item.interaction_id)
        if match is not None:
            attempts.append(int(match.group(3)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionRevalidationApprovalRequestError(
            "fresh_approval_request_attempts_exhausted",
            "Fresh Approval 交互尝试次数已达上限。",
        )
    suffix = requirement.requirement_id.removeprefix("evreapprovalreq_")
    return f"ask-evreapproval-{suffix}-{step.role.value}-{attempt}"


def _approval_request_id(*, requirement_id, role, interaction_request_sha256):
    digest = _sha256_payload({
        "policy_version": EVOLUTION_REVALIDATION_APPROVAL_REQUEST_POLICY,
        "requirement_id": requirement_id,
        "role": role.value,
        "interaction_request_sha256": interaction_request_sha256,
    })
    return f"evreapprovalrequest_{digest[:24]}"


def _receipt_matches_requirement(receipt, requirement) -> bool:
    step = next((item for item in requirement.steps if item.role is receipt.role), None)
    return bool(
        step is not None
        and receipt.workspace_root == requirement.workspace_root
        and receipt.requirement_id == requirement.requirement_id
        and receipt.requirement_sha256 == requirement.requirement_sha256
        and receipt.promotion_input_id == requirement.promotion_input_id
        and receipt.promotion_input_sha256 == requirement.promotion_input_sha256
        and receipt.step_order == step.order
        and receipt.reasons == step.reasons
        and receipt.signature_entry.required == step.signature_required
        and receipt.signature_entry.signable_payload_sha256
        == requirement.signable_payload_sha256
        and receipt.requirement_expires_at == requirement.expires_at
        and _interaction_matches(
            receipt.interaction,
            requirement,
            step,
            _approval_interaction_request(requirement, step, timeout_seconds=None),
        )
    )


def _response_view(receipt, *, requirement_current, expired):
    current = requirement_current and not expired
    return EvolutionRevalidationApprovalResponseView(
        receipt=receipt,
        requirement_current=requirement_current,
        requirement_expired=expired,
        eligible_for_fresh_signature=current and receipt.eligible_for_fresh_signature,
        eligible_for_future_aggregation=(
            current
            and receipt.response is EvolutionPromotionApprovalResponse.APPROVE
            and receipt.counts_toward_role_quorum
        ),
    )


def _static_request_payload(request: UserInteractionRequest) -> dict[str, object]:
    return {**request.to_public_dict(), "timeout_seconds": None}


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_approval_responses ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "requirement_id TEXT NOT NULL, requirement_sha256 TEXT NOT NULL, "
        "promotion_input_id TEXT NOT NULL, role TEXT NOT NULL, "
        "interaction_id TEXT NOT NULL UNIQUE, interaction_digest TEXT NOT NULL, "
        "response TEXT NOT NULL, receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL, "
        "UNIQUE(requirement_id, role))"
    )
    await db.commit()


def _receipt_from_json(value: object) -> EvolutionRevalidationApprovalResponseReceipt:
    encoded = str(value)
    if len(encoded.encode("utf-8")) > _MAX_RECEIPT_BYTES:
        raise ValueError("Fresh Approval Response artifact 过大。")
    return EvolutionRevalidationApprovalResponseReceipt.model_validate_json(encoded)


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
    "EVOLUTION_REVALIDATION_APPROVAL_REQUEST_POLICY",
    "EvolutionRevalidationApprovalRequestError",
    "EvolutionRevalidationApprovalRequestService",
    "EvolutionRevalidationApprovalResponseReceipt",
    "EvolutionRevalidationApprovalResponseStore",
    "EvolutionRevalidationApprovalResponseView",
]
