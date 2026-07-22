"""Deterministic, non-executable approval requirements for Promotion Packages."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.promotion_package_inputs import (
    _ensure_schema as _ensure_input_schema,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionPackage,
    EvolutionPromotionPackageError,
    EvolutionPromotionPackageExecutor,
    EvolutionPromotionProtectedScope,
    EvolutionPromotionTargetProbe,
    _require_active_promotion_package,
)
from naumi_agent.evolution.promotion_packages import (
    _ensure_schema as _ensure_package_schema,
)

EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY_V1 = (
    "evolution-promotion-approval-requirement-v1"
)
EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY = (
    "evolution-promotion-approval-requirement-v2"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_REQUIREMENT_BYTES = 256 * 1_024


class EvolutionPromotionApprovalRole(StrEnum):
    USER = "user"
    INDEPENDENT_REVIEWER = "independent_reviewer"
    SECURITY_REVIEWER = "security_reviewer"
    DATA_OWNER = "data_owner"
    RELEASE_MANAGER = "release_manager"


class EvolutionPromotionApprovalReason(StrEnum):
    PROMOTION_OWNER_CONSENT = "promotion_owner_consent"
    MEDIUM_RISK = "medium_risk"
    HIGH_RISK = "high_risk"
    CRITICAL_RISK = "critical_risk"
    PROTECTED_TARGET = "protected_target"
    PROTECTED_SCOPE = "protected_scope"
    AUTHORIZATION_OR_SECURITY_SCOPE = "authorization_or_security_scope"
    PERSISTENCE_SCOPE = "persistence_scope"
    CI_RELEASE_OR_DEPENDENCY_SCOPE = "ci_release_or_dependency_scope"
    MIGRATION_REVIEW = "migration_review"
    DATA_BACKUP = "data_backup"


class EvolutionPromotionTechnicalGate(StrEnum):
    PACKAGE_CURRENT = "package_current"
    INPUT_ACTIVE = "input_active"
    REFLECTION_ACTIVE = "reflection_active"
    TARGET_CURRENT = "target_current"
    REBASE_REQUIRED = "rebase_required"
    REVALIDATION_REQUIRED = "revalidation_required"
    MIGRATION_REVIEW_REQUIRED = "migration_review_required"
    DATA_BACKUP_REQUIRED = "data_backup_required"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPromotionApprovalStep(_StrictModel):
    order: int = Field(ge=1, le=5)
    role: EvolutionPromotionApprovalRole
    reasons: tuple[EvolutionPromotionApprovalReason, ...] = Field(
        min_length=1,
        max_length=8,
    )
    human_required: Literal[True] = True
    signature_required: bool
    durable_interaction_required: Literal[True] = True
    approved: Literal[False] = False
    signature_collected: Literal[False] = False

    @model_validator(mode="after")
    def _step_is_exact(self) -> Self:
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("Approval reasons 必须排序且不得重复。")
        return self


class EvolutionPromotionApprovalRequirement(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-promotion-approval-requirement-v1",
        "evolution-promotion-approval-requirement-v2",
    ] = (
        EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY
    )
    requirement_id: str = Field(pattern=r"^evapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    policy_projection_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    package_id: str = Field(pattern=r"^evpromopkg_[0-9a-f]{24}$")
    package_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    reflection_id: str = Field(pattern=r"^evreflection_[0-9a-f]{24}$")
    target_branch: str = Field(min_length=1, max_length=255)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    risk_level: Literal["low", "medium", "high", "critical"]
    steps: tuple[EvolutionPromotionApprovalStep, ...] = Field(
        min_length=1,
        max_length=5,
    )
    required_roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(
        min_length=1,
        max_length=5,
    )
    signature_required_roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(max_length=5)
    minimum_approvals: int = Field(ge=1, le=5)
    minimum_signatures: int = Field(ge=0, le=5)
    protected_scope_human_gate: bool
    automatic_approval_allowed: Literal[False] = False
    technical_gates: tuple[EvolutionPromotionTechnicalGate, ...] = Field(
        min_length=4,
        max_length=8,
    )
    blocking_gates: tuple[EvolutionPromotionTechnicalGate, ...] = Field(max_length=2)
    approval_request_ready: bool
    validity_seconds: int = Field(ge=3_600, le=604_800)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    package_current_at_issue: Literal[True] = True
    input_active_at_issue: Literal[True] = True
    reflection_active_at_issue: Literal[True] = True
    target_current_at_issue: Literal[True] = True
    approval_decided: Literal[False] = False
    signatures_collected: Literal[False] = False
    interaction_created: Literal[False] = False
    promotion_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    contains_user_custom_text: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _requirement_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Approval Requirement workspace 必须是 canonical 路径。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        if expires != issued + timedelta(seconds=self.validity_seconds):
            raise ValueError("Approval Requirement expiry 投影不一致。")
        if tuple(item.order for item in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("Approval steps 顺序不连续。")
        roles = tuple(item.role for item in self.steps)
        if roles != self.required_roles or len(roles) != len(set(roles)):
            raise ValueError("Approval roles/steps 投影不一致。")
        signatures = tuple(item.role for item in self.steps if item.signature_required)
        if signatures != self.signature_required_roles:
            raise ValueError("Approval signature roles 投影不一致。")
        if self.policy_version == EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY:
            expected_signatures = tuple(
                role
                for role in self.required_roles
                if role is not EvolutionPromotionApprovalRole.USER
            )
            if signatures != expected_signatures:
                raise ValueError("Approval Requirement v2 必须验证每个专业角色签名。")
        if not (
            self.minimum_approvals == len(self.steps) and self.minimum_signatures == len(signatures)
        ):
            raise ValueError("Approval quorum 投影不一致。")
        expected_gates, blocking = _technical_gates_from_values(
            rebase_required=(
                EvolutionPromotionTechnicalGate.REBASE_REQUIRED in self.technical_gates
            ),
            revalidation_required=(
                EvolutionPromotionTechnicalGate.REVALIDATION_REQUIRED in self.technical_gates
            ),
            migration_review_required=(
                EvolutionPromotionTechnicalGate.MIGRATION_REVIEW_REQUIRED in self.technical_gates
            ),
            data_backup_required=(
                EvolutionPromotionTechnicalGate.DATA_BACKUP_REQUIRED in self.technical_gates
            ),
        )
        if self.technical_gates != expected_gates or self.blocking_gates != blocking:
            raise ValueError("Approval technical gates 投影不一致。")
        if self.approval_request_ready != (len(blocking) == 0):
            raise ValueError("Approval request readiness 投影不一致。")
        policy_digest = _sha256_payload(_policy_projection(self))
        if not hmac.compare_digest(self.policy_projection_sha256, policy_digest):
            raise ValueError("Approval Requirement policy projection 摘要不一致。")
        digest = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"requirement_id", "requirement_sha256"},
            )
        )
        if not hmac.compare_digest(self.requirement_sha256, digest):
            raise ValueError("Approval Requirement 摘要不一致。")
        if self.requirement_id != f"evapprovalreq_{digest[:24]}":
            raise ValueError("Approval Requirement identity 不一致。")
        return self


class EvolutionPromotionApprovalRequirementView(_StrictModel):
    requirement: EvolutionPromotionApprovalRequirement
    package_active: bool
    target_current: bool
    expired: bool
    approval_request_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected = bool(
            self.package_active
            and self.target_current
            and not self.expired
            and self.requirement.approval_request_ready
        )
        if self.approval_request_eligible is not expected:
            raise ValueError("Approval Requirement eligibility 投影不一致。")
        return self


class EvolutionPromotionApprovalRequirementError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPromotionApprovalRequirementBuilder:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        policy_version: Literal[
            "evolution-promotion-approval-requirement-v1",
            "evolution-promotion-approval-requirement-v2",
        ] = EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._policy_version = policy_version

    def build(
        self,
        *,
        package: EvolutionPromotionPackage,
    ) -> EvolutionPromotionApprovalRequirement:
        try:
            item = EvolutionPromotionPackage.model_validate_json(package.model_dump_json())
            steps = _approval_steps(item, policy_version=self._policy_version)
            gates, blocking = _technical_gates(item)
            validity = _validity_seconds(item)
            issued = self._clock()
            if issued.utcoffset() is None:
                raise ValueError("Approval Requirement clock 必须包含时区。")
            issued = issued.astimezone(UTC)
            expires = issued + timedelta(seconds=validity)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_package_invalid",
                "Promotion Package 无效或无法计算审批要求。",
            ) from exc
        core = {
            "schema_version": 1,
            "policy_version": self._policy_version,
            "workspace_root": item.workspace_root,
            "package_id": item.package_id,
            "package_sha256": item.package_sha256,
            "promotion_input_id": item.promotion_input_id,
            "promotion_input_sha256": item.promotion_input_sha256,
            "reflection_id": item.reflection_id,
            "target_branch": item.target.target_branch,
            "target_head": item.target.target_head,
            "target_tree": item.target.target_tree,
            "signable_payload_sha256": (item.signature_envelope.signable_payload_sha256),
            "risk_level": item.risk_level,
            "steps": [step.model_dump(mode="json") for step in steps],
            "required_roles": [step.role.value for step in steps],
            "signature_required_roles": [
                step.role.value for step in steps if step.signature_required
            ],
            "minimum_approvals": len(steps),
            "minimum_signatures": sum(step.signature_required for step in steps),
            "protected_scope_human_gate": bool(item.approval_input.protected_paths),
            "automatic_approval_allowed": False,
            "technical_gates": [gate.value for gate in gates],
            "blocking_gates": [gate.value for gate in blocking],
            "approval_request_ready": not blocking,
            "validity_seconds": validity,
            "package_current_at_issue": True,
            "input_active_at_issue": True,
            "reflection_active_at_issue": True,
            "target_current_at_issue": True,
            "approval_decided": False,
            "signatures_collected": False,
            "interaction_created": False,
            "promotion_executed": False,
            "git_write_executed": False,
            "contains_freeform_narrative": False,
            "contains_user_custom_text": False,
            "llm_generated": False,
        }
        policy_digest = _sha256_payload(core)
        payload = {
            **core,
            "policy_projection_sha256": policy_digest,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionPromotionApprovalRequirement.model_validate(
                {
                    **payload,
                    "requirement_id": f"evapprovalreq_{digest[:24]}",
                    "requirement_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_artifact_invalid",
                "Approval Requirement artifact 无法验证。",
            ) from exc


class EvolutionPromotionApprovalRequirementStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        target_probe: EvolutionPromotionTargetProbe | None = None,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._target_probe = target_probe or EvolutionPromotionTargetProbe()

    async def record(
        self,
        requirement: EvolutionPromotionApprovalRequirement,
        *,
        package: EvolutionPromotionPackage,
    ) -> EvolutionPromotionApprovalRequirement:
        try:
            item = EvolutionPromotionApprovalRequirement.model_validate_json(
                requirement.model_dump_json()
            )
            source = EvolutionPromotionPackage.model_validate_json(package.model_dump_json())
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_artifact_invalid",
                "Approval Requirement 或 Package 无效。",
            ) from exc
        if not _requirement_matches_package(item, source):
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_package_mismatch",
                "Approval Requirement 未绑定 exact Package。",
            )
        expected = EvolutionPromotionApprovalRequirementBuilder(
            clock=lambda: _aware(item.issued_at),
            policy_version=item.policy_version,
        ).build(package=source)
        if expected != item:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_policy_mismatch",
                "Approval Requirement 未遵循当前确定性审批策略。",
            )
        current = await asyncio.to_thread(
            self._target_probe.capture,
            workspace_root=source.workspace_root,
            target_branch=source.target.target_branch,
            baseline_commit=source.target.baseline_commit,
        )
        if current != source.target:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_target_changed",
                "Promotion target 已变化，不能写入审批要求。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_REQUIREMENT_BYTES:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_oversized",
                "Approval Requirement 超过 256 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_input_schema(db)
                await _ensure_package_schema(db)
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await _require_active_promotion_package(db, source)
                except EvolutionPromotionPackageError as exc:
                    raise EvolutionPromotionApprovalRequirementError(
                        "approval_requirement_package_ineligible",
                        "Promotion Package 已失效。",
                    ) from exc
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_requirements "
                        "WHERE package_id = ? AND policy_version = ?",
                        (item.package_id, item.policy_version),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing.policy_projection_sha256 != item.policy_projection_sha256:
                        await db.rollback()
                        raise EvolutionPromotionApprovalRequirementError(
                            "approval_requirement_conflict",
                            "同一 Package/Policy 不可覆盖为不同审批要求。",
                        )
                    await db.rollback()
                    return existing
                await db.execute(
                    "INSERT INTO evolution_promotion_approval_requirements "
                    "(requirement_id, requirement_sha256, policy_projection_sha256, "
                    "policy_version, package_id, package_sha256, promotion_input_id, "
                    "workspace_root, target_branch, target_head, requirement_json, "
                    "issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.requirement_id,
                        item.requirement_sha256,
                        item.policy_projection_sha256,
                        item.policy_version,
                        item.package_id,
                        item.package_sha256,
                        item.promotion_input_id,
                        item.workspace_root,
                        item.target_branch,
                        item.target_head,
                        encoded,
                        item.issued_at,
                        item.expires_at,
                    ),
                )
                await db.commit()
        except EvolutionPromotionApprovalRequirementError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_store_error",
                "Approval Requirement 无法持久化。",
            ) from exc
        restored = await self.get(item.requirement_id)
        assert restored is not None
        return restored

    async def get(
        self,
        requirement_id: str,
    ) -> EvolutionPromotionApprovalRequirement | None:
        if (
            not isinstance(requirement_id, str)
            or re.fullmatch(r"evapprovalreq_[0-9a-f]{24}", requirement_id) is None
        ):
            raise ValueError("approval requirement id 格式无效。")
        return await self._read("requirement_id", requirement_id)

    async def get_by_package(
        self,
        package_id: str,
        *,
        policy_version: str = EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY,
    ) -> EvolutionPromotionApprovalRequirement | None:
        if (
            not isinstance(package_id, str)
            or re.fullmatch(r"evpromopkg_[0-9a-f]{24}", package_id) is None
        ):
            raise ValueError("promotion package id 格式无效。")
        if policy_version not in {
            EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY_V1,
            EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY,
        }:
            raise ValueError("approval requirement policy version 无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_approval_requirements "
                        "WHERE package_id = ? AND policy_version = ?",
                        (package_id, policy_version),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionApprovalRequirementError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_store_corrupt",
                "Approval Requirement 损坏或无法读取。",
            ) from exc

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionPromotionApprovalRequirement | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_promotion_approval_requirements "
                        f"WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionApprovalRequirementError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_store_corrupt",
                "Approval Requirement 损坏或无法读取。",
            ) from exc


class EvolutionPromotionApprovalRequirementExecutor:
    def __init__(
        self,
        *,
        package_executor: EvolutionPromotionPackageExecutor,
        requirement_store: EvolutionPromotionApprovalRequirementStore,
        builder: EvolutionPromotionApprovalRequirementBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._package_executor = package_executor
        self._requirement_store = requirement_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._builder = builder or EvolutionPromotionApprovalRequirementBuilder(clock=self._clock)

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        package_id: str,
    ) -> EvolutionPromotionApprovalRequirementView:
        package_view = await self._inspect_package(workspace_root, package_id)
        if not package_view.package_review_eligible:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_package_ineligible",
                "只有 still-current 的 active Package 可生成审批要求。",
            )
        existing = await self._requirement_store.get_by_package(
            package_id,
            policy_version=EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY,
        )
        if existing is None:
            proposed = self._builder.build(package=package_view.package)
            if (
                proposed.policy_version
                != EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY
            ):
                raise EvolutionPromotionApprovalRequirementError(
                    "approval_requirement_policy_outdated",
                    "新 Approval Requirement 必须使用 current v2 身份签名策略。",
                )
            existing = await self._requirement_store.record(
                proposed,
                package=package_view.package,
            )
        return await self.inspect(
            workspace_root=workspace_root,
            requirement_id=existing.requirement_id,
        )

    async def inspect(
        self,
        *,
        workspace_root: str | Path,
        requirement_id: str,
    ) -> EvolutionPromotionApprovalRequirementView:
        try:
            requirement = await self._requirement_store.get(requirement_id)
        except (OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_read_failed",
                "无法读取 Approval Requirement authority。",
            ) from exc
        if requirement is None:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_missing",
                "Approval Requirement 不存在。",
            )
        package_view = await self._inspect_package(
            workspace_root,
            requirement.package_id,
        )
        if not _requirement_matches_package(requirement, package_view.package):
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_package_mismatch",
                "Approval Requirement 对应 Package authority 不一致。",
            )
        current = self._clock()
        if current.utcoffset() is None:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_clock_invalid",
                "Approval Requirement clock 必须包含时区。",
            )
        expired = current.astimezone(UTC) >= _aware(requirement.expires_at)
        active = package_view.promotion_input_active
        return EvolutionPromotionApprovalRequirementView(
            requirement=requirement,
            package_active=active,
            target_current=package_view.target_current,
            expired=expired,
            approval_request_eligible=(
                active
                and package_view.target_current
                and not expired
                and requirement.approval_request_ready
            ),
        )

    async def _inspect_package(self, workspace_root: str | Path, package_id: str):
        try:
            return await self._package_executor.inspect(
                workspace_root=workspace_root,
                package_id=package_id,
            )
        except EvolutionPromotionPackageError as exc:
            raise EvolutionPromotionApprovalRequirementError(
                "approval_requirement_package_read_failed",
                "无法读取 still-current Promotion Package authority。",
            ) from exc


def render_evolution_promotion_approval_requirement(
    view: EvolutionPromotionApprovalRequirementView,
) -> str:
    item = EvolutionPromotionApprovalRequirementView.model_validate(view.model_dump(mode="python"))
    requirement = item.requirement
    lines = [
        f"# Evolution Approval Requirement `{requirement.requirement_id}`",
        "",
        "**审批要求已冻结；尚未创建交互、作出审批、收集签名或执行 Promotion。**",
        "",
        f"- Package：`{requirement.package_id}` · "
        f"{'active' if item.package_active else 'inactive'}",
        f"- Target current：{'是' if item.target_current else '否'}",
        f"- Expired：{'是' if item.expired else '否'}",
        f"- Approval request eligible：{'是' if item.approval_request_eligible else '否'}",
        f"- Required approvals：{requirement.minimum_approvals}",
        f"- Required signatures：{requirement.minimum_signatures}",
        "- Roles：" + ", ".join(f"`{role.value}`" for role in requirement.required_roles),
        "- Blocking gates："
        + (
            ", ".join(f"`{gate.value}`" for gate in requirement.blocking_gates)
            if requirement.blocking_gates
            else "none"
        ),
        f"- Validity：{requirement.validity_seconds}s · `{requirement.expires_at}`",
        "- Protected-scope human gate："
        + ("是" if requirement.protected_scope_human_gate else "否"),
        "- Approval/Signature/Interaction：`false`",
        "- Git/Promotion：`false`",
        f"- Requirement SHA-256：`{requirement.requirement_sha256}`",
        "",
        "下一步：为每个 required role 收集 fenced Response；专业角色还必须提交真实 Ed25519 "
        "Signature Receipt，之后由 EVO-05.2d 聚合。本命令本身不能批准 Package。",
    ]
    return "\n".join(lines)


def _approval_steps(
    package: EvolutionPromotionPackage,
    *,
    policy_version: str = EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY,
) -> tuple[EvolutionPromotionApprovalStep, ...]:
    reasons: dict[EvolutionPromotionApprovalRole, set[EvolutionPromotionApprovalReason]] = {
        EvolutionPromotionApprovalRole.USER: {
            EvolutionPromotionApprovalReason.PROMOTION_OWNER_CONSENT
        }
    }
    risk = package.risk_level
    if risk in {"medium", "high", "critical"}:
        reasons.setdefault(EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER, set()).add(
            {
                "medium": EvolutionPromotionApprovalReason.MEDIUM_RISK,
                "high": EvolutionPromotionApprovalReason.HIGH_RISK,
                "critical": EvolutionPromotionApprovalReason.CRITICAL_RISK,
            }[risk]
        )
    if risk in {"high", "critical"}:
        reasons.setdefault(EvolutionPromotionApprovalRole.RELEASE_MANAGER, set()).add(
            EvolutionPromotionApprovalReason.HIGH_RISK
            if risk == "high"
            else EvolutionPromotionApprovalReason.CRITICAL_RISK
        )
    if risk == "critical":
        reasons.setdefault(EvolutionPromotionApprovalRole.SECURITY_REVIEWER, set()).add(
            EvolutionPromotionApprovalReason.CRITICAL_RISK
        )
    if package.approval_input.target_branch_protected:
        reasons.setdefault(EvolutionPromotionApprovalRole.RELEASE_MANAGER, set()).add(
            EvolutionPromotionApprovalReason.PROTECTED_TARGET
        )
    scopes = {item.scope for item in package.approval_input.protected_paths}
    if scopes:
        reasons.setdefault(
            EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
            set(),
        ).add(EvolutionPromotionApprovalReason.PROTECTED_SCOPE)
    if scopes & {
        EvolutionPromotionProtectedScope.AUTHORIZATION,
        EvolutionPromotionProtectedScope.SECURITY,
    }:
        reasons.setdefault(EvolutionPromotionApprovalRole.SECURITY_REVIEWER, set()).add(
            EvolutionPromotionApprovalReason.AUTHORIZATION_OR_SECURITY_SCOPE
        )
    if EvolutionPromotionProtectedScope.PERSISTENCE in scopes:
        reasons.setdefault(EvolutionPromotionApprovalRole.DATA_OWNER, set()).add(
            EvolutionPromotionApprovalReason.PERSISTENCE_SCOPE
        )
    if scopes & {
        EvolutionPromotionProtectedScope.CI_RELEASE,
        EvolutionPromotionProtectedScope.DEPENDENCY,
    }:
        reasons.setdefault(EvolutionPromotionApprovalRole.RELEASE_MANAGER, set()).add(
            EvolutionPromotionApprovalReason.CI_RELEASE_OR_DEPENDENCY_SCOPE
        )
    if package.approval_input.migration_review_required:
        reasons.setdefault(EvolutionPromotionApprovalRole.DATA_OWNER, set()).add(
            EvolutionPromotionApprovalReason.MIGRATION_REVIEW
        )
    if package.approval_input.data_backup_required:
        reasons.setdefault(EvolutionPromotionApprovalRole.DATA_OWNER, set()).add(
            EvolutionPromotionApprovalReason.DATA_BACKUP
        )
    order = (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.DATA_OWNER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    steps: list[EvolutionPromotionApprovalStep] = []
    for role in order:
        role_reasons = reasons.get(role)
        if not role_reasons:
            continue
        if policy_version == EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY:
            signature_required = role is not EvolutionPromotionApprovalRole.USER
        elif policy_version == EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY_V1:
            signature_required = bool(
                role
                in {
                    EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
                    EvolutionPromotionApprovalRole.DATA_OWNER,
                    EvolutionPromotionApprovalRole.RELEASE_MANAGER,
                }
                or (
                    role is EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER
                    and risk in {"high", "critical"}
                )
            )
        else:
            raise ValueError("approval requirement policy version 无效。")
        steps.append(
            EvolutionPromotionApprovalStep(
                order=len(steps) + 1,
                role=role,
                reasons=tuple(sorted(role_reasons, key=lambda item: item.value)),
                signature_required=signature_required,
            )
        )
    return tuple(steps)


def _technical_gates(
    package: EvolutionPromotionPackage,
) -> tuple[
    tuple[EvolutionPromotionTechnicalGate, ...],
    tuple[EvolutionPromotionTechnicalGate, ...],
]:
    return _technical_gates_from_values(
        rebase_required=package.target.rebase_required,
        revalidation_required=package.target.revalidation_required,
        migration_review_required=package.approval_input.migration_review_required,
        data_backup_required=package.approval_input.data_backup_required,
    )


def _technical_gates_from_values(
    *,
    rebase_required: bool,
    revalidation_required: bool,
    migration_review_required: bool,
    data_backup_required: bool,
) -> tuple[
    tuple[EvolutionPromotionTechnicalGate, ...],
    tuple[EvolutionPromotionTechnicalGate, ...],
]:
    gates = [
        EvolutionPromotionTechnicalGate.PACKAGE_CURRENT,
        EvolutionPromotionTechnicalGate.INPUT_ACTIVE,
        EvolutionPromotionTechnicalGate.REFLECTION_ACTIVE,
        EvolutionPromotionTechnicalGate.TARGET_CURRENT,
    ]
    blocking: list[EvolutionPromotionTechnicalGate] = []
    if rebase_required:
        gates.append(EvolutionPromotionTechnicalGate.REBASE_REQUIRED)
        blocking.append(EvolutionPromotionTechnicalGate.REBASE_REQUIRED)
    if revalidation_required:
        gates.append(EvolutionPromotionTechnicalGate.REVALIDATION_REQUIRED)
        blocking.append(EvolutionPromotionTechnicalGate.REVALIDATION_REQUIRED)
    if migration_review_required:
        gates.append(EvolutionPromotionTechnicalGate.MIGRATION_REVIEW_REQUIRED)
    if data_backup_required:
        gates.append(EvolutionPromotionTechnicalGate.DATA_BACKUP_REQUIRED)
    return tuple(gates), tuple(blocking)


def _validity_seconds(package: EvolutionPromotionPackage) -> int:
    value = {
        "low": 7 * 24 * 60 * 60,
        "medium": 3 * 24 * 60 * 60,
        "high": 24 * 60 * 60,
        "critical": 6 * 60 * 60,
    }[package.risk_level]
    if package.approval_input.migration_review_required:
        value = min(value, 24 * 60 * 60)
    if package.target.rebase_required:
        value = min(value, 6 * 60 * 60)
    return value


def _policy_projection(item: EvolutionPromotionApprovalRequirement) -> dict[str, object]:
    return item.model_dump(
        mode="json",
        exclude={
            "requirement_id",
            "requirement_sha256",
            "policy_projection_sha256",
            "issued_at",
            "expires_at",
        },
    )


def _requirement_matches_package(
    requirement: EvolutionPromotionApprovalRequirement,
    package: EvolutionPromotionPackage,
) -> bool:
    return bool(
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
        and requirement.risk_level == package.risk_level
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_promotion_approval_requirements (
            requirement_id TEXT PRIMARY KEY,
            requirement_sha256 TEXT NOT NULL,
            policy_projection_sha256 TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            package_id TEXT NOT NULL,
            package_sha256 TEXT NOT NULL,
            promotion_input_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            target_head TEXT NOT NULL,
            requirement_json TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(package_id, policy_version)
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionPromotionApprovalRequirement:
    encoded = str(row["requirement_json"])
    if len(encoded.encode("utf-8")) > _MAX_REQUIREMENT_BYTES:
        raise ValueError("Approval Requirement Store artifact 过大。")
    item = EvolutionPromotionApprovalRequirement.model_validate_json(encoded)
    if not (
        row["requirement_id"] == item.requirement_id
        and row["requirement_sha256"] == item.requirement_sha256
        and row["policy_projection_sha256"] == item.policy_projection_sha256
        and row["policy_version"] == item.policy_version
        and row["package_id"] == item.package_id
        and row["package_sha256"] == item.package_sha256
        and row["promotion_input_id"] == item.promotion_input_id
        and row["workspace_root"] == item.workspace_root
        and row["target_branch"] == item.target_branch
        and row["target_head"] == item.target_head
        and row["issued_at"] == item.issued_at
        and row["expires_at"] == item.expires_at
    ):
        raise ValueError("Approval Requirement Store index 不一致。")
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
    "EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY",
    "EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY_V1",
    "EvolutionPromotionApprovalReason",
    "EvolutionPromotionApprovalRequirement",
    "EvolutionPromotionApprovalRequirementBuilder",
    "EvolutionPromotionApprovalRequirementError",
    "EvolutionPromotionApprovalRequirementExecutor",
    "EvolutionPromotionApprovalRequirementStore",
    "EvolutionPromotionApprovalRequirementView",
    "EvolutionPromotionApprovalRole",
    "EvolutionPromotionApprovalStep",
    "EvolutionPromotionTechnicalGate",
    "render_evolution_promotion_approval_requirement",
]
