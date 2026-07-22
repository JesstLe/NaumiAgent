"""Role-scoped durable approval requests for Promotion Packages."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalReason,
    EvolutionPromotionApprovalRequirement,
    EvolutionPromotionApprovalRequirementError,
    EvolutionPromotionApprovalRequirementExecutor,
    EvolutionPromotionApprovalRequirementView,
    EvolutionPromotionApprovalRole,
    EvolutionPromotionApprovalStep,
)
from naumi_agent.evolution.approval_requirements import (
    _ensure_schema as _ensure_requirement_schema,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY = "evolution-promotion-approval-request-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 256 * 1_024
_INTERACTION_RE = re.compile(
    r"^ask-evapproval-([0-9a-f]{24})-"
    r"(user|independent_reviewer|security_reviewer|data_owner|release_manager)-"
    r"([1-9][0-9]{0,3})$"
)

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class EvolutionPromotionApprovalResponse(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class EvolutionPromotionApprovalIdentityAssurance(StrEnum):
    LOCAL_SESSION_USER = "local_session_user"
    UNVERIFIED_ROLE_CLAIM = "unverified_role_claim"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPromotionSignatureReceiptEntry(_StrictModel):
    """Non-secret entry contract for a later verified signature receipt."""

    schema_version: Literal[1] = 1
    role: EvolutionPromotionApprovalRole
    required: bool
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    identity_binding_required: Literal[True] = True
    signature_collected: Literal[False] = False
    signer_identity: Literal[""] = ""
    signature_algorithm: Literal[""] = ""
    signature_value: Literal[""] = ""


class EvolutionPromotionApprovalResponseReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-promotion-approval-request-v1"] = (
        EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY
    )
    receipt_id: str = Field(pattern=r"^evapprovalresp_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    requirement_id: str = Field(pattern=r"^evapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    package_id: str = Field(pattern=r"^evpromopkg_[0-9a-f]{24}$")
    package_sha256: str = Field(pattern=_SHA256_RE)
    approval_request_id: str = Field(pattern=r"^evapprovalrequest_[0-9a-f]{24}$")
    role: EvolutionPromotionApprovalRole
    step_order: int = Field(ge=1, le=5)
    reasons: tuple[EvolutionPromotionApprovalReason, ...] = Field(
        min_length=1,
        max_length=8,
    )
    response: EvolutionPromotionApprovalResponse
    interaction: HarnessInteractionRecord
    interaction_request_sha256: str = Field(pattern=_SHA256_RE)
    identity_assurance: EvolutionPromotionApprovalIdentityAssurance
    role_binding_verified: bool
    role_response_recorded: Literal[True] = True
    counts_toward_role_quorum: bool
    signature_entry: EvolutionPromotionSignatureReceiptEntry
    requirement_expires_at: str = Field(min_length=1, max_length=100)
    recorded_at: str = Field(min_length=1, max_length=100)
    overall_approval_decided: Literal[False] = False
    final_quorum_reached: Literal[False] = False
    promotion_authority: Literal[False] = False
    promotion_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    contains_user_custom_text: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Approval Response workspace 必须是 canonical 路径。")
        if self.interaction.state != "answered" or self.interaction.answer_kind != "option":
            raise ValueError("Approval Response 必须来自已提交的结构化选项答案。")
        if self.interaction.answer_value != self.response.value:
            raise ValueError("Approval Response 与 interaction answer 不一致。")
        if self.interaction.answered_by != "user":
            raise ValueError("Approval Response 必须来自真实用户答案。")
        if self.interaction.subject_kind != "tool":
            raise ValueError("Approval Response interaction subject kind 无效。")
        if self.interaction.subject_id != self.requirement_id:
            raise ValueError("Approval Response interaction 未绑定 Requirement。")
        match = _INTERACTION_RE.fullmatch(self.interaction.interaction_id)
        if (
            match is None
            or match.group(1) != self.requirement_id.removeprefix("evapprovalreq_")
            or match.group(2) != self.role.value
        ):
            raise ValueError("Approval Response interaction identity 无效。")
        expected_request_id = _approval_request_id(
            requirement_id=self.requirement_id,
            role=self.role,
            interaction_request_sha256=self.interaction_request_sha256,
        )
        if self.approval_request_id != expected_request_id:
            raise ValueError("Approval Request identity 不一致。")
        actual_request_digest = _sha256_payload(_static_request_payload(self.interaction.request()))
        if not hmac.compare_digest(
            self.interaction_request_sha256,
            actual_request_digest,
        ):
            raise ValueError("Approval Request payload 摘要不一致。")
        local_user_bound = bool(
            self.role is EvolutionPromotionApprovalRole.USER
            and self.interaction.session_id
            and self.interaction.answered_by == "user"
        )
        expected_assurance = (
            EvolutionPromotionApprovalIdentityAssurance.LOCAL_SESSION_USER
            if local_user_bound
            else EvolutionPromotionApprovalIdentityAssurance.UNVERIFIED_ROLE_CLAIM
        )
        if self.identity_assurance is not expected_assurance:
            raise ValueError("Approval Response identity assurance 投影不一致。")
        expected_binding = local_user_bound
        if self.role_binding_verified is not expected_binding:
            raise ValueError("Approval Response role binding 投影不一致。")
        expected_counts = bool(
            self.response is EvolutionPromotionApprovalResponse.APPROVE
            and expected_binding
            and not self.signature_entry.required
        )
        if self.counts_toward_role_quorum is not expected_counts:
            raise ValueError("Approval Response quorum 投影不一致。")
        if (
            self.signature_entry.role is not self.role
            or self.signature_entry.identity_binding_required is not True
        ):
            raise ValueError("Signature Receipt entry 未绑定相同角色。")
        if _aware(self.recorded_at) != _aware(self.interaction.answered_at):
            raise ValueError("Approval Response recorded_at 必须来自 interaction。")
        if _aware(self.recorded_at) >= _aware(self.requirement_expires_at):
            raise ValueError("到期后的答案不能形成 Approval Response Receipt。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Approval Response Receipt 摘要不一致。")
        if self.receipt_id != f"evapprovalresp_{digest[:24]}":
            raise ValueError("Approval Response Receipt identity 不一致。")
        return self


class EvolutionPromotionApprovalResponseView(_StrictModel):
    receipt: EvolutionPromotionApprovalResponseReceipt
    requirement_current: bool
    target_current: bool
    requirement_expired: bool
    eligible_for_future_aggregation: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected = bool(
            self.requirement_current
            and self.target_current
            and not self.requirement_expired
            and self.receipt.response is EvolutionPromotionApprovalResponse.APPROVE
            and self.receipt.counts_toward_role_quorum
        )
        if self.eligible_for_future_aggregation is not expected:
            raise ValueError("Approval Response aggregation eligibility 投影不一致。")
        return self


class EvolutionPromotionApprovalRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPromotionApprovalResponseBuilder:
    def build(
        self,
        *,
        requirement: EvolutionPromotionApprovalRequirement,
        step: EvolutionPromotionApprovalStep,
        interaction: HarnessInteractionRecord,
    ) -> EvolutionPromotionApprovalResponseReceipt:
        try:
            source = EvolutionPromotionApprovalRequirement.model_validate_json(
                requirement.model_dump_json()
            )
            role_step = EvolutionPromotionApprovalStep.model_validate_json(step.model_dump_json())
            answered = HarnessInteractionRecord.model_validate_json(interaction.model_dump_json())
            request = _approval_interaction_request(source, role_step, timeout_seconds=None)
            _require_matching_interaction(answered, source, role_step, request)
            response = EvolutionPromotionApprovalResponse(answered.answer_value)
        except EvolutionPromotionApprovalRequestError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_input_invalid",
                "Approval Response 输入无效或未绑定 exact interaction。",
            ) from exc
        request_digest = _sha256_payload(_static_request_payload(request))
        signature_entry = EvolutionPromotionSignatureReceiptEntry(
            role=role_step.role,
            required=role_step.signature_required,
            signable_payload_sha256=source.signable_payload_sha256,
        )
        local_user_bound = bool(
            role_step.role is EvolutionPromotionApprovalRole.USER
            and answered.session_id
            and answered.answered_by == "user"
        )
        assurance = (
            EvolutionPromotionApprovalIdentityAssurance.LOCAL_SESSION_USER
            if local_user_bound
            else EvolutionPromotionApprovalIdentityAssurance.UNVERIFIED_ROLE_CLAIM
        )
        role_binding_verified = local_user_bound
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY,
            "workspace_root": source.workspace_root,
            "requirement_id": source.requirement_id,
            "requirement_sha256": source.requirement_sha256,
            "package_id": source.package_id,
            "package_sha256": source.package_sha256,
            "approval_request_id": _approval_request_id(
                requirement_id=source.requirement_id,
                role=role_step.role,
                interaction_request_sha256=request_digest,
            ),
            "role": role_step.role.value,
            "step_order": role_step.order,
            "reasons": [item.value for item in role_step.reasons],
            "response": response.value,
            "interaction": answered.model_dump(mode="json"),
            "interaction_request_sha256": request_digest,
            "identity_assurance": assurance.value,
            "role_binding_verified": role_binding_verified,
            "role_response_recorded": True,
            "counts_toward_role_quorum": bool(
                response is EvolutionPromotionApprovalResponse.APPROVE
                and role_binding_verified
                and not role_step.signature_required
            ),
            "signature_entry": signature_entry.model_dump(mode="json"),
            "requirement_expires_at": source.expires_at,
            "recorded_at": answered.answered_at,
            "overall_approval_decided": False,
            "final_quorum_reached": False,
            "promotion_authority": False,
            "promotion_executed": False,
            "git_write_executed": False,
            "contains_user_custom_text": False,
            "llm_generated": False,
        }
        digest = _sha256_payload(core)
        try:
            return EvolutionPromotionApprovalResponseReceipt.model_validate_json(
                json.dumps(
                    {
                        **core,
                        "receipt_id": f"evapprovalresp_{digest[:24]}",
                        "receipt_sha256": digest,
                    },
                    ensure_ascii=False,
                )
            )
        except ValueError as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_artifact_invalid",
                "Approval Response Receipt 无法验证。",
            ) from exc


class EvolutionPromotionApprovalResponseStore:
    def __init__(self, db_path: str | Path, *, interaction_store: HarnessStore) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Approval Response Store 需要 Harness Store。")
        self._interaction_store = interaction_store

    async def record(
        self,
        receipt: EvolutionPromotionApprovalResponseReceipt,
        *,
        requirement: EvolutionPromotionApprovalRequirement,
    ) -> EvolutionPromotionApprovalResponseReceipt:
        try:
            item = EvolutionPromotionApprovalResponseReceipt.model_validate_json(
                receipt.model_dump_json()
            )
            source = EvolutionPromotionApprovalRequirement.model_validate_json(
                requirement.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_artifact_invalid",
                "Approval Response Receipt 或 Requirement 无效。",
            ) from exc
        if not _receipt_matches_requirement(item, source):
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_requirement_mismatch",
                "Approval Response Receipt 未绑定 exact Requirement。",
            )
        try:
            authoritative = await self._interaction_store.get_interaction(
                workspace_root=source.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_interaction_read_failed",
                "无法重读 Harness interaction authority。",
            ) from exc
        if authoritative is None or authoritative != item.interaction:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_interaction_mismatch",
                "Approval Response Receipt 未绑定 Harness interaction authority。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_oversized",
                "Approval Response Receipt 超过 256 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_requirement_schema(db)
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                requirement_row = await (
                    await db.execute(
                        "SELECT requirement_sha256 FROM "
                        "evolution_promotion_approval_requirements "
                        "WHERE requirement_id = ?",
                        (source.requirement_id,),
                    )
                ).fetchone()
                if requirement_row is None or not hmac.compare_digest(
                    str(requirement_row["requirement_sha256"]),
                    source.requirement_sha256,
                ):
                    await db.rollback()
                    raise EvolutionPromotionApprovalRequestError(
                        "approval_response_requirement_stale",
                        "Approval Requirement authority 已缺失或变化。",
                    )
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_responses "
                        "WHERE requirement_id = ? AND role = ?",
                        (item.requirement_id, item.role.value),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing == item:
                        await db.rollback()
                        return existing
                    await db.rollback()
                    raise EvolutionPromotionApprovalRequestError(
                        "approval_response_conflict",
                        "同一 Requirement/Role 已存在不同回答，拒绝覆盖。",
                    )
                await db.execute(
                    "INSERT INTO evolution_promotion_approval_responses "
                    "(receipt_id, receipt_sha256, requirement_id, requirement_sha256, "
                    "package_id, workspace_root, role, interaction_id, interaction_digest, "
                    "response, receipt_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.requirement_id,
                        item.requirement_sha256,
                        item.package_id,
                        item.workspace_root,
                        item.role.value,
                        item.interaction.interaction_id,
                        item.interaction.digest(),
                        item.response.value,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
        except EvolutionPromotionApprovalRequestError:
            raise
        except (aiosqlite.Error, HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_store_error",
                "Approval Response Receipt 无法持久化。",
            ) from exc
        restored = await self.get_by_requirement_role(item.requirement_id, item.role)
        assert restored is not None
        return restored

    async def get_by_requirement_role(
        self,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole | str,
    ) -> EvolutionPromotionApprovalResponseReceipt | None:
        if re.fullmatch(r"evapprovalreq_[0-9a-f]{24}", str(requirement_id)) is None:
            raise ValueError("approval requirement id 格式无效。")
        role_value = EvolutionPromotionApprovalRole(role).value
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_responses "
                        "WHERE requirement_id = ? AND role = ?",
                        (requirement_id, role_value),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionApprovalRequestError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_store_corrupt",
                "Approval Response Receipt 损坏或无法读取。",
            ) from exc

    async def get(
        self,
        receipt_id: str,
    ) -> EvolutionPromotionApprovalResponseReceipt | None:
        if re.fullmatch(r"evapprovalresp_[0-9a-f]{24}", str(receipt_id)) is None:
            raise ValueError("approval response receipt id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_responses "
                        "WHERE receipt_id = ?",
                        (receipt_id,),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionApprovalRequestError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_response_store_corrupt",
                "Approval Response Receipt 损坏或无法读取。",
            ) from exc


class EvolutionPromotionApprovalRequestService:
    """Ask one exact approval role through HAR-10.6 and freeze its response."""

    def __init__(
        self,
        *,
        requirement_executor: EvolutionPromotionApprovalRequirementExecutor,
        interaction_store: HarnessStore,
        response_store: EvolutionPromotionApprovalResponseStore,
        request_user_input: RequestUserInputCallback,
        builder: EvolutionPromotionApprovalResponseBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            requirement_executor,
            EvolutionPromotionApprovalRequirementExecutor,
        ):
            raise TypeError("Approval Request service 需要 Requirement Executor。")
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Approval Request service 需要 Harness Store。")
        if not isinstance(response_store, EvolutionPromotionApprovalResponseStore):
            raise TypeError("Approval Request service 需要 Response Store。")
        if not callable(request_user_input):
            raise TypeError("Approval Request service 需要用户交互 callback。")
        self._requirement_executor = requirement_executor
        self._interaction_store = interaction_store
        self._response_store = response_store
        self._request_user_input = request_user_input
        self._builder = builder or EvolutionPromotionApprovalResponseBuilder()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole | str,
    ) -> EvolutionPromotionApprovalResponseView:
        try:
            role_value = EvolutionPromotionApprovalRole(role)
        except (TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_role_invalid",
                "Approval Request role 无效。",
            ) from exc
        lock_key = f"{Path(workspace_root).expanduser()}::{requirement_id}::{role_value.value}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._execute_locked(
                workspace_root=workspace_root,
                requirement_id=requirement_id,
                role=role_value,
            )

    async def _execute_locked(
        self,
        *,
        workspace_root: str | Path,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole,
    ) -> EvolutionPromotionApprovalResponseView:
        view = await self._inspect_requirement(workspace_root, requirement_id)
        requirement = view.requirement
        step = next((item for item in requirement.steps if item.role is role), None)
        if step is None:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_role_not_required",
                f"当前 Requirement 不需要 {role.value} 角色。",
            )
        existing = await self._response_store.get_by_requirement_role(
            requirement.requirement_id,
            role,
        )
        if existing is not None:
            return _response_view(existing, view)
        if not view.approval_request_eligible:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_requirement_ineligible",
                "Approval Requirement 已过期、失效或仍有技术阻塞门。",
            )
        history = await self._interaction_history(requirement, step)
        answered = tuple(item for item in history if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_answer_ambiguous",
                "同一 Requirement/Role 存在多个已回答交互，拒绝猜测审批意图。",
            )
        if answered:
            return await self._record(requirement, step, answered[0], view)
        pending = next((item for item in history if item.state == "pending"), None)
        if pending is not None:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_interaction_pending",
                f"角色交互 {pending.interaction_id} 仍待回答；请先在界面完成选择。",
            )
        now = self._now()
        remaining = int((_aware(requirement.expires_at) - now).total_seconds())
        if remaining < 3:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_requirement_expired",
                "Approval Requirement 已无足够有效时间创建交互。",
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
            await self._request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_interaction_unavailable",
                "当前界面无法创建持久 Evolution 审批交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_interaction_invalid",
                "Evolution 审批交互未通过运行时协议校验。",
            ) from exc
        try:
            interaction = await self._interaction_store.get_interaction(
                workspace_root=requirement.workspace_root,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_interaction_read_failed",
                "无法重读 Evolution 审批 interaction authority。",
            ) from exc
        if interaction is None or interaction.state != "answered":
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_answer_not_committed",
                "用户答案尚未提交到 Harness authority，不能形成审批回执。",
            )
        refreshed = await self._inspect_requirement(workspace_root, requirement_id)
        if refreshed.expired:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_answer_expired",
                "用户答案提交时 Approval Requirement 已过期，拒绝形成回执。",
            )
        return await self._record(requirement, step, interaction, refreshed)

    async def _inspect_requirement(
        self,
        workspace_root: str | Path,
        requirement_id: str,
    ) -> EvolutionPromotionApprovalRequirementView:
        try:
            return await self._requirement_executor.inspect(
                workspace_root=workspace_root,
                requirement_id=requirement_id,
            )
        except EvolutionPromotionApprovalRequirementError as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_requirement_read_failed",
                "无法读取 still-current Approval Requirement authority。",
            ) from exc

    async def _interaction_history(
        self,
        requirement: EvolutionPromotionApprovalRequirement,
        step: EvolutionPromotionApprovalStep,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            records = await self._interaction_store.list_interactions(
                workspace_root=requirement.workspace_root,
                subject_kind="tool",
                subject_ids=(requirement.requirement_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_interaction_history_failed",
                "无法读取 Approval Request 持久交互历史。",
            ) from exc
        static_request = _approval_interaction_request(
            requirement,
            step,
            timeout_seconds=None,
        )
        return tuple(
            item
            for item in records
            if _interaction_matches(item, requirement, step, static_request)
        )

    async def _record(
        self,
        requirement: EvolutionPromotionApprovalRequirement,
        step: EvolutionPromotionApprovalStep,
        interaction: HarnessInteractionRecord,
        requirement_view: EvolutionPromotionApprovalRequirementView,
    ) -> EvolutionPromotionApprovalResponseView:
        receipt = self._builder.build(
            requirement=requirement,
            step=step,
            interaction=interaction,
        )
        stored = await self._response_store.record(receipt, requirement=requirement)
        return _response_view(stored, requirement_view)

    def _now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise EvolutionPromotionApprovalRequestError(
                "approval_request_clock_invalid",
                "Approval Request clock 必须包含时区。",
            )
        return value.astimezone(UTC)


def render_evolution_promotion_approval_response(
    view: EvolutionPromotionApprovalResponseView,
) -> str:
    item = EvolutionPromotionApprovalResponseView.model_validate(view.model_dump(mode="python"))
    receipt = item.receipt
    identity = (
        "本地会话用户已绑定" if receipt.role_binding_verified else "角色身份未验证，不能计入 quorum"
    )
    signature = (
        "需要独立签名回执，当前未收集"
        if receipt.signature_entry.required
        else "本角色无需密码学签名"
    )
    return "\n".join(
        [
            f"# Evolution Approval Response `{receipt.receipt_id}`",
            "",
            "**角色回答已通过 HAR-10.6 fencing 持久化；这不是最终 Promotion 审批。**",
            "",
            f"- Requirement：`{receipt.requirement_id}`",
            f"- Package：`{receipt.package_id}`",
            f"- Role：`{receipt.role.value}` · step {receipt.step_order}",
            f"- Response：`{receipt.response.value}`",
            f"- Interaction：`{receipt.interaction.interaction_id}` · "
            f"sequence {receipt.interaction.sequence}",
            f"- Identity assurance：`{receipt.identity_assurance.value}` · {identity}",
            f"- Signature：{signature}",
            "- Counts toward role quorum：" + ("是" if receipt.counts_toward_role_quorum else "否"),
            "- Eligible for future aggregation："
            + ("是" if item.eligible_for_future_aggregation else "否"),
            "- Overall approval/final quorum：`false`",
            "- Git/Promotion：`false`",
            f"- Receipt SHA-256：`{receipt.receipt_sha256}`",
            "",
            "下一步：为需要身份或签名的角色提交独立可验证回执；"
            "最终聚合仍必须重新检查 Requirement、target 与全部技术门。",
        ]
    )


def _approval_interaction_request(
    requirement: EvolutionPromotionApprovalRequirement,
    step: EvolutionPromotionApprovalStep,
    *,
    timeout_seconds: int | None,
) -> UserInteractionRequest:
    reasons = "、".join(item.value for item in step.reasons)
    payload = {
        "header": f"Evolution 审批 · {step.role.value}",
        "question": (
            f"请以 {step.role.value} 角色审查 Package {requirement.package_id} "
            f"（target {requirement.target_branch}@{requirement.target_head[:12]}，"
            f"原因：{reasons}）。本选择只形成角色回答回执，不会合并、推送或发布。"
        ),
        "options": [
            {
                "value": EvolutionPromotionApprovalResponse.APPROVE.value,
                "label": "同意本角色意见",
                "description": "记录同意意见；身份、签名和最终 quorum 仍需独立验证。",
            },
            {
                "value": EvolutionPromotionApprovalResponse.REQUEST_CHANGES.value,
                "label": "要求修改后重审",
                "description": "记录需要修改；当前 Package 不得继续 Promotion。",
            },
            {
                "value": EvolutionPromotionApprovalResponse.REJECT.value,
                "label": "拒绝本次 Promotion",
                "description": "记录拒绝意见；不执行 Git 或发布操作。",
            },
        ],
        "allow_custom": False,
        "custom_label": "不允许自定义审批文本",
        "timeout_seconds": timeout_seconds,
    }
    return normalize_interaction_request(payload)


def _static_request_payload(request: UserInteractionRequest) -> dict[str, object]:
    return {
        **request.to_public_dict(),
        "timeout_seconds": None,
    }


def _interaction_matches(
    interaction: HarnessInteractionRecord,
    requirement: EvolutionPromotionApprovalRequirement,
    step: EvolutionPromotionApprovalStep,
    request: UserInteractionRequest,
) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    if match is None:
        return False
    return bool(
        interaction.subject_kind == "tool"
        and interaction.subject_id == requirement.requirement_id
        and match.group(1) == requirement.requirement_id.removeprefix("evapprovalreq_")
        and match.group(2) == step.role.value
        and _static_request_payload(interaction.request()) == _static_request_payload(request)
        and interaction.request().timeout_seconds is not None
        and interaction.request().timeout_seconds <= requirement.validity_seconds
        and _aware(interaction.created_at) < _aware(requirement.expires_at)
    )


def _require_matching_interaction(
    interaction: HarnessInteractionRecord,
    requirement: EvolutionPromotionApprovalRequirement,
    step: EvolutionPromotionApprovalStep,
    request: UserInteractionRequest,
) -> None:
    if not _interaction_matches(interaction, requirement, step, request):
        raise EvolutionPromotionApprovalRequestError(
            "approval_response_interaction_mismatch",
            "已回答 interaction 未绑定 exact Approval Request。",
        )


def _next_interaction_id(
    requirement: EvolutionPromotionApprovalRequirement,
    step: EvolutionPromotionApprovalStep,
    history: tuple[HarnessInteractionRecord, ...],
) -> str:
    attempts = []
    for item in history:
        match = _INTERACTION_RE.fullmatch(item.interaction_id)
        if match is not None:
            attempts.append(int(match.group(3)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionPromotionApprovalRequestError(
            "approval_request_attempts_exhausted",
            "Approval Request 交互尝试次数已达上限。",
        )
    suffix = requirement.requirement_id.removeprefix("evapprovalreq_")
    return f"ask-evapproval-{suffix}-{step.role.value}-{attempt}"


def _approval_request_id(
    *,
    requirement_id: str,
    role: EvolutionPromotionApprovalRole,
    interaction_request_sha256: str,
) -> str:
    digest = _sha256_payload(
        {
            "policy_version": EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY,
            "requirement_id": requirement_id,
            "role": role.value,
            "interaction_request_sha256": interaction_request_sha256,
        }
    )
    return f"evapprovalrequest_{digest[:24]}"


def _receipt_matches_requirement(
    receipt: EvolutionPromotionApprovalResponseReceipt,
    requirement: EvolutionPromotionApprovalRequirement,
) -> bool:
    step = next((item for item in requirement.steps if item.role is receipt.role), None)
    expected_request = (
        _approval_interaction_request(requirement, step, timeout_seconds=None)
        if step is not None
        else None
    )
    return bool(
        step is not None
        and receipt.workspace_root == requirement.workspace_root
        and receipt.requirement_id == requirement.requirement_id
        and receipt.requirement_sha256 == requirement.requirement_sha256
        and receipt.package_id == requirement.package_id
        and receipt.package_sha256 == requirement.package_sha256
        and receipt.step_order == step.order
        and receipt.reasons == step.reasons
        and receipt.signature_entry.required == step.signature_required
        and receipt.signature_entry.signable_payload_sha256 == requirement.signable_payload_sha256
        and receipt.requirement_expires_at == requirement.expires_at
        and expected_request is not None
        and _interaction_matches(
            receipt.interaction,
            requirement,
            step,
            expected_request,
        )
    )


def _response_view(
    receipt: EvolutionPromotionApprovalResponseReceipt,
    view: EvolutionPromotionApprovalRequirementView,
) -> EvolutionPromotionApprovalResponseView:
    return EvolutionPromotionApprovalResponseView(
        receipt=receipt,
        requirement_current=view.package_active,
        target_current=view.target_current,
        requirement_expired=view.expired,
        eligible_for_future_aggregation=(
            view.package_active
            and view.target_current
            and not view.expired
            and receipt.response is EvolutionPromotionApprovalResponse.APPROVE
            and receipt.counts_toward_role_quorum
        ),
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_promotion_approval_responses (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL,
            requirement_id TEXT NOT NULL,
            requirement_sha256 TEXT NOT NULL,
            package_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            role TEXT NOT NULL,
            interaction_id TEXT NOT NULL UNIQUE,
            interaction_digest TEXT NOT NULL,
            response TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(requirement_id, role)
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionPromotionApprovalResponseReceipt:
    encoded = str(row["receipt_json"])
    if len(encoded.encode("utf-8")) > _MAX_RECEIPT_BYTES:
        raise ValueError("Approval Response Store artifact 过大。")
    item = EvolutionPromotionApprovalResponseReceipt.model_validate_json(encoded)
    if not (
        row["receipt_id"] == item.receipt_id
        and row["receipt_sha256"] == item.receipt_sha256
        and row["requirement_id"] == item.requirement_id
        and row["requirement_sha256"] == item.requirement_sha256
        and row["package_id"] == item.package_id
        and row["workspace_root"] == item.workspace_root
        and row["role"] == item.role.value
        and row["interaction_id"] == item.interaction.interaction_id
        and row["interaction_digest"] == item.interaction.digest()
        and row["response"] == item.response.value
        and row["recorded_at"] == item.recorded_at
    ):
        raise ValueError("Approval Response Store index 不一致。")
    return item


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_PROMOTION_APPROVAL_REQUEST_POLICY",
    "EvolutionPromotionApprovalIdentityAssurance",
    "EvolutionPromotionApprovalRequestError",
    "EvolutionPromotionApprovalRequestService",
    "EvolutionPromotionApprovalResponse",
    "EvolutionPromotionApprovalResponseBuilder",
    "EvolutionPromotionApprovalResponseReceipt",
    "EvolutionPromotionApprovalResponseStore",
    "EvolutionPromotionApprovalResponseView",
    "EvolutionPromotionSignatureReceiptEntry",
    "render_evolution_promotion_approval_response",
]
