"""Durable, non-executing authority for approved rebase/revalidation work."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_decisions import (
    EvolutionPromotionApprovalDecisionError,
    EvolutionPromotionApprovalDecisionReceipt,
    EvolutionPromotionApprovalDecisionService,
    EvolutionPromotionApprovalDecisionStatus,
    EvolutionPromotionApprovalDecisionView,
)
from naumi_agent.evolution.approval_decisions import (
    _ensure_schema as _ensure_decision_schema,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionPackage,
    EvolutionPromotionPackageError,
    EvolutionPromotionPackageExecutor,
    EvolutionPromotionPackageView,
    EvolutionPromotionTargetProbe,
    EvolutionPromotionTargetRelation,
)
from naumi_agent.evolution.promotion_packages import (
    _ensure_schema as _ensure_package_schema,
)

EVOLUTION_REVALIDATION_REQUEST_POLICY = "evolution-revalidation-request-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_REQUEST_ID_RE = r"^evrevalidation_[0-9a-f]{24}$"
_MAX_REQUEST_BYTES = 128 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationRequest(_StrictModel):
    """Exact authority input for a future isolated rebase/revalidation executor."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-request-v1"] = (
        EVOLUTION_REVALIDATION_REQUEST_POLICY
    )
    request_id: str = Field(pattern=_REQUEST_ID_RE)
    request_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    decision_id: str = Field(pattern=r"^evapprovaldecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=_SHA256_RE)
    decision_source_set_sha256: str = Field(pattern=_SHA256_RE)
    requirement_id: str = Field(pattern=r"^evapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    package_id: str = Field(pattern=r"^evpromopkg_[0-9a-f]{24}$")
    package_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    reflection_id: str = Field(pattern=r"^evreflection_[0-9a-f]{24}$")
    reflection_sha256: str = Field(pattern=_SHA256_RE)
    target_branch: str = Field(min_length=1, max_length=255)
    target_head: str = Field(pattern=_GIT_OBJECT_RE)
    target_tree: str = Field(pattern=_GIT_OBJECT_RE)
    baseline_commit: str = Field(pattern=_GIT_OBJECT_RE)
    target_relation: EvolutionPromotionTargetRelation
    operation: Literal[
        "validate_exact_tree",
        "rebase_then_validate",
        "block_for_reconciliation",
    ]
    manual_reconciliation_required: bool
    patch_manifest_sha256: str = Field(pattern=_SHA256_RE)
    baseline_sha256: str = Field(pattern=_SHA256_RE)
    migration_assessment_sha256: str = Field(pattern=_SHA256_RE)
    rollback_plan_sha256: str = Field(pattern=_SHA256_RE)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    sandbox_required: Literal[True] = True
    exact_target_required: Literal[True] = True
    current_approval_required: Literal[True] = True
    validation_receipts_must_be_reissued: Literal[True] = True
    network_allowed: Literal[False] = False
    dependency_install_allowed: Literal[False] = False
    main_worktree_write_allowed: Literal[False] = False
    target_branch_write_allowed: Literal[False] = False
    execution_started: Literal[False] = False
    rebase_executed: Literal[False] = False
    validation_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    contains_source_code: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Revalidation Request workspace 必须是 canonical 路径。")
        target_moved = self.target_head != self.baseline_commit
        if target_moved is (self.target_relation is EvolutionPromotionTargetRelation.SAME):
            raise ValueError("Revalidation Request target relation 与 commit 不一致。")
        expected_operation = {
            EvolutionPromotionTargetRelation.SAME: "validate_exact_tree",
            EvolutionPromotionTargetRelation.ADVANCED: "rebase_then_validate",
            EvolutionPromotionTargetRelation.DIVERGED: "block_for_reconciliation",
        }[self.target_relation]
        if self.operation != expected_operation:
            raise ValueError("Revalidation Request operation 与 target relation 不一致。")
        if self.manual_reconciliation_required is not (
            self.target_relation is EvolutionPromotionTargetRelation.DIVERGED
        ):
            raise ValueError("Revalidation Request reconciliation 投影不一致。")
        if self.required_platforms != tuple(sorted(set(self.required_platforms))):
            raise ValueError("Revalidation Request platforms 必须排序且不得重复。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, digest):
            raise ValueError("Revalidation Request 摘要不一致。")
        if self.request_id != f"evrevalidation_{digest[:24]}":
            raise ValueError("Revalidation Request identity 不一致。")
        return self


class EvolutionRevalidationRequestView(_StrictModel):
    request: EvolutionRevalidationRequest
    source_readable: bool
    decision_current: bool
    decision_rebase_eligible: bool = False
    package_current: bool
    target_current: bool
    current_target_head: str | None = Field(default=None, pattern=_GIT_OBJECT_RE)
    current_target_relation: Literal["same", "advanced", "diverged", "unavailable"] = "unavailable"
    current_status: Literal["ready", "stale", "ineligible"]
    execution_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        exact_eligible = bool(
            self.source_readable
            and self.decision_current
            and self.package_current
            and self.target_current
            and self.current_target_relation == "same"
        )
        rebase_eligible = bool(
            self.source_readable
            and not self.decision_current
            and self.decision_rebase_eligible
            and self.package_current
            and not self.target_current
            and self.current_target_relation == "advanced"
        )
        if self.target_current is not (self.current_target_relation == "same"):
            raise ValueError("Revalidation Request target relation 投影不一致。")
        if (self.current_target_head is None) is (self.current_target_relation != "unavailable"):
            raise ValueError("Revalidation Request current target head 投影不一致。")
        expected_status: Literal["ready", "stale", "ineligible"]
        if not self.source_readable:
            expected_status = "ineligible"
        elif not self.package_current or not self.target_current:
            expected_status = "stale"
        elif not self.decision_current:
            expected_status = "ineligible"
        else:
            expected_status = "ready"
        if self.current_status != expected_status:
            raise ValueError("Revalidation Request current status 投影不一致。")
        if self.execution_eligible is not (exact_eligible or rebase_eligible):
            raise ValueError("Revalidation Request eligibility 投影不一致。")
        return self


class EvolutionRevalidationRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRequestBuilder:
    def build(
        self,
        *,
        decision_view: EvolutionPromotionApprovalDecisionView,
        package_view: EvolutionPromotionPackageView,
    ) -> EvolutionRevalidationRequest:
        try:
            decision_view = EvolutionPromotionApprovalDecisionView.model_validate_json(
                decision_view.model_dump_json()
            )
            package_view = EvolutionPromotionPackageView.model_validate_json(
                package_view.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_source_invalid",
                "Revalidation Request 上游 authority 无法验证。",
            ) from exc
        decision = decision_view.receipt
        package = package_view.package
        if not decision_view.source_current:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_decision_stale",
                "Approval Decision 已失效，不能创建 Revalidation Request。",
            )
        if not (
            decision_view.current_status is EvolutionPromotionApprovalDecisionStatus.APPROVED
            and decision_view.current_rebase_revalidation_eligible
            and decision.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
            and decision.rebase_revalidation_eligible
        ):
            raise EvolutionRevalidationRequestError(
                "revalidation_request_decision_ineligible",
                "Approval Decision 尚未获得当前 rebase/revalidate 资格。",
            )
        if not package_view.package_review_eligible:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_package_stale",
                "Promotion Package 或 target 已失效。",
            )
        if not (
            decision.workspace_root == package.workspace_root
            and decision.package_id == package.package_id
            and decision.package_sha256 == package.package_sha256
            and decision.target_branch == package.target.target_branch
            and decision.target_head == package.target.target_head
            and decision.target_tree == package.target.target_tree
        ):
            raise EvolutionRevalidationRequestError(
                "revalidation_request_source_mismatch",
                "Approval Decision 与 Promotion Package 不是 exact authority 组合。",
            )
        envelope = package.signature_envelope
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_REQUEST_POLICY,
            "workspace_root": package.workspace_root,
            "decision_id": decision.decision_id,
            "decision_sha256": decision.decision_sha256,
            "decision_source_set_sha256": decision.source_set_sha256,
            "requirement_id": decision.requirement_id,
            "requirement_sha256": decision.requirement_sha256,
            "package_id": package.package_id,
            "package_sha256": package.package_sha256,
            "promotion_input_id": package.promotion_input_id,
            "promotion_input_sha256": package.promotion_input_sha256,
            "candidate_id": package.candidate_id,
            "candidate_revision": package.candidate_revision,
            "reflection_id": package.reflection_id,
            "reflection_sha256": package.reflection_sha256,
            "target_branch": package.target.target_branch,
            "target_head": package.target.target_head,
            "target_tree": package.target.target_tree,
            "baseline_commit": package.target.baseline_commit,
            "target_relation": package.target.relation_to_baseline.value,
            "operation": {
                EvolutionPromotionTargetRelation.SAME: "validate_exact_tree",
                EvolutionPromotionTargetRelation.ADVANCED: "rebase_then_validate",
                EvolutionPromotionTargetRelation.DIVERGED: "block_for_reconciliation",
            }[package.target.relation_to_baseline],
            "manual_reconciliation_required": (
                package.target.relation_to_baseline is EvolutionPromotionTargetRelation.DIVERGED
            ),
            "patch_manifest_sha256": envelope.patch_manifest_sha256,
            "baseline_sha256": envelope.baseline_sha256,
            "migration_assessment_sha256": envelope.migration_assessment_sha256,
            "rollback_plan_sha256": envelope.rollback_plan_sha256,
            "required_platforms": tuple(sorted(package.approval_input.required_platforms)),
            "sandbox_required": True,
            "exact_target_required": True,
            "current_approval_required": True,
            "validation_receipts_must_be_reissued": True,
            "network_allowed": False,
            "dependency_install_allowed": False,
            "main_worktree_write_allowed": False,
            "target_branch_write_allowed": False,
            "execution_started": False,
            "rebase_executed": False,
            "validation_executed": False,
            "promotion_authority": False,
            "merge_executed": False,
            "push_executed": False,
            "publish_executed": False,
            "contains_source_code": False,
            "contains_freeform_narrative": False,
            "llm_generated": False,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionRevalidationRequest.model_validate(
                {
                    **payload,
                    "request_id": f"evrevalidation_{digest[:24]}",
                    "request_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_artifact_invalid",
                "Revalidation Request artifact 无法验证。",
            ) from exc


class EvolutionRevalidationRequestStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        request: EvolutionRevalidationRequest,
        *,
        decision: EvolutionPromotionApprovalDecisionReceipt,
        package: EvolutionPromotionPackage,
    ) -> EvolutionRevalidationRequest:
        item = _validated_request(request)
        if not _request_matches_sources(item, decision, package):
            raise EvolutionRevalidationRequestError(
                "revalidation_request_source_mismatch",
                "Revalidation Request 未绑定 exact Decision/Package。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_oversized",
                "Revalidation Request 超过 128 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_package_schema(db)
                await _ensure_decision_schema(db)
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                decision_row = await (
                    await db.execute(
                        "SELECT decision_sha256, package_id, package_sha256, status, "
                        "decision_json "
                        "FROM evolution_promotion_approval_decisions WHERE decision_id = ?",
                        (item.decision_id,),
                    )
                ).fetchone()
                package_row = await (
                    await db.execute(
                        "SELECT package_sha256, promotion_input_id, promotion_input_sha256, "
                        "target_head, package_json FROM evolution_promotion_packages "
                        "WHERE package_id = ?",
                        (item.package_id,),
                    )
                ).fetchone()
                stored_decision = (
                    None
                    if decision_row is None
                    else _decision_from_json(decision_row["decision_json"])
                )
                stored_package = (
                    None if package_row is None else _package_from_json(package_row["package_json"])
                )
                if (
                    stored_decision != decision
                    or stored_package != package
                    or decision_row is None
                    or package_row is None
                    or not (
                        decision_row["decision_sha256"] == item.decision_sha256
                        and decision_row["package_id"] == item.package_id
                        and decision_row["package_sha256"] == item.package_sha256
                        and decision_row["status"] == "approved"
                        and package_row["package_sha256"] == item.package_sha256
                        and package_row["promotion_input_id"] == item.promotion_input_id
                        and package_row["promotion_input_sha256"] == item.promotion_input_sha256
                        and package_row["target_head"] == item.target_head
                    )
                ):
                    await db.rollback()
                    raise EvolutionRevalidationRequestError(
                        "revalidation_request_source_index_mismatch",
                        "Revalidation Request 上游索引已变化。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_revalidation_requests WHERE decision_id = ?",
                        (item.decision_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _from_row(existing)
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationRequestError(
                            "revalidation_request_conflict",
                            "同一 Approval Decision 不可绑定不同 Revalidation Request。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_requests "
                    "(request_id, request_sha256, decision_id, decision_sha256, "
                    "package_id, package_sha256, workspace_root, target_branch, "
                    "target_head, request_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.request_id,
                        item.request_sha256,
                        item.decision_id,
                        item.decision_sha256,
                        item.package_id,
                        item.package_sha256,
                        item.workspace_root,
                        item.target_branch,
                        item.target_head,
                        encoded,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationRequestError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_store_error",
                "Revalidation Request 无法持久化。",
            ) from exc
        restored = await self.get(item.request_id)
        assert restored is not None
        return restored

    async def get(self, request_id: str) -> EvolutionRevalidationRequest | None:
        if re.fullmatch(_REQUEST_ID_RE, str(request_id)) is None:
            raise ValueError("revalidation request id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_revalidation_requests WHERE request_id = ?",
                        (request_id,),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionRevalidationRequestError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_store_corrupt",
                "Revalidation Request 损坏或无法读取。",
            ) from exc


class EvolutionRevalidationRequestService:
    def __init__(
        self,
        *,
        decision_service: EvolutionPromotionApprovalDecisionService,
        package_executor: EvolutionPromotionPackageExecutor,
        request_store: EvolutionRevalidationRequestStore,
        builder: EvolutionRevalidationRequestBuilder | None = None,
        target_probe: EvolutionPromotionTargetProbe | None = None,
    ) -> None:
        if not isinstance(decision_service, EvolutionPromotionApprovalDecisionService):
            raise TypeError("Revalidation Request service 需要 Decision Service。")
        if not isinstance(package_executor, EvolutionPromotionPackageExecutor):
            raise TypeError("Revalidation Request service 需要 Package Executor。")
        if not isinstance(request_store, EvolutionRevalidationRequestStore):
            raise TypeError("Revalidation Request service 需要 Request Store。")
        self._decision_service = decision_service
        self._package_executor = package_executor
        self._request_store = request_store
        self._builder = builder or EvolutionRevalidationRequestBuilder()
        self._target_probe = target_probe or EvolutionPromotionTargetProbe()

    async def issue(
        self,
        *,
        workspace_root: str | Path,
        decision_id: str,
    ) -> EvolutionRevalidationRequestView:
        decision_view, package_view = await self._sources(workspace_root, decision_id)
        request = self._builder.build(
            decision_view=decision_view,
            package_view=package_view,
        )
        stored = await self._request_store.record(
            request,
            decision=decision_view.receipt,
            package=package_view.package,
        )
        return await self._current_view(stored)

    async def inspect(
        self,
        *,
        workspace_root: str | Path,
        request_id: str,
    ) -> EvolutionRevalidationRequestView:
        stored = await self._request_store.get(request_id)
        if stored is None:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_missing",
                "Revalidation Request 不存在。",
            )
        if stored.workspace_root != str(Path(workspace_root).expanduser().resolve()):
            raise EvolutionRevalidationRequestError(
                "revalidation_request_workspace_mismatch",
                "Revalidation Request 不属于当前工作区。",
            )
        return await self._current_view(stored)

    async def _sources(
        self,
        workspace_root: str | Path,
        decision_id: str,
    ) -> tuple[EvolutionPromotionApprovalDecisionView, EvolutionPromotionPackageView]:
        try:
            decision_view = await self._decision_service.inspect(
                workspace_root=workspace_root,
                decision_id=decision_id,
            )
            package_view = await self._package_executor.inspect(
                workspace_root=workspace_root,
                package_id=decision_view.receipt.package_id,
            )
        except (
            EvolutionPromotionApprovalDecisionError,
            EvolutionPromotionPackageError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationRequestError(
                "revalidation_request_source_read_failed",
                "无法读取 Approval Decision/Promotion Package authority。",
            ) from exc
        return decision_view, package_view

    async def _current_view(
        self,
        request: EvolutionRevalidationRequest,
    ) -> EvolutionRevalidationRequestView:
        try:
            decision_view, package_view = await self._sources(
                request.workspace_root,
                request.decision_id,
            )
        except EvolutionRevalidationRequestError:
            return EvolutionRevalidationRequestView(
                request=request,
                source_readable=False,
                decision_current=False,
                decision_rebase_eligible=False,
                package_current=False,
                target_current=False,
                current_target_head=None,
                current_target_relation="unavailable",
                current_status="ineligible",
                execution_eligible=False,
            )
        decision_current = bool(
            decision_view.source_current
            and decision_view.current_status is EvolutionPromotionApprovalDecisionStatus.APPROVED
            and decision_view.current_rebase_revalidation_eligible
            and decision_view.receipt.decision_sha256 == request.decision_sha256
            and decision_view.receipt.source_set_sha256 == request.decision_source_set_sha256
        )
        decision_rebase_eligible = bool(
            decision_view.target_only_stale
            and decision_view.current_rebase_revalidation_eligible
            and decision_view.receipt.decision_sha256 == request.decision_sha256
            and decision_view.receipt.source_set_sha256 == request.decision_source_set_sha256
        )
        package_current = bool(
            package_view.promotion_input_active
            and package_view.reflection_active
            and package_view.package.package_sha256 == request.package_sha256
        )
        try:
            target = self._target_probe.capture(
                workspace_root=request.workspace_root,
                target_branch=request.target_branch,
                baseline_commit=request.target_head,
            )
            current_target_head = target.target_head
            current_target_relation = target.relation_to_baseline.value
        except (EvolutionPromotionPackageError, OSError, TypeError, ValueError):
            current_target_head = None
            current_target_relation = "unavailable"
        target_current = current_target_relation == "same"
        status: Literal["ready", "stale", "ineligible"]
        if not package_current or not target_current:
            status = "stale"
        elif not decision_current:
            status = "ineligible"
        else:
            status = "ready"
        return EvolutionRevalidationRequestView(
            request=request,
            source_readable=True,
            decision_current=decision_current,
            decision_rebase_eligible=decision_rebase_eligible,
            package_current=package_current,
            target_current=target_current,
            current_target_head=current_target_head,
            current_target_relation=current_target_relation,
            current_status=status,
            execution_eligible=(
                status == "ready"
                or (
                    status == "stale"
                    and decision_rebase_eligible
                    and package_current
                    and current_target_relation == "advanced"
                )
            ),
        )


def render_evolution_revalidation_request(
    view: EvolutionRevalidationRequestView,
) -> str:
    view = EvolutionRevalidationRequestView.model_validate_json(view.model_dump_json())
    item = view.request
    return "\n".join(
        (
            f"# Revalidation Request `{item.request_id}`",
            "",
            f"- 当前状态：`{view.current_status}`",
            f"- 执行资格：`{str(view.execution_eligible).lower()}`",
            f"- 当前目标关系：`{view.current_target_relation}`",
            "- 隔离 rebase 资格：" + ("是" if view.decision_rebase_eligible else "否"),
            f"- Approval Decision：`{item.decision_id}`",
            f"- Promotion Package：`{item.package_id}`",
            f"- Candidate：`{item.candidate_id}` revision {item.candidate_revision}",
            f"- Target：`{item.target_branch}` @ `{item.target_head}`",
            f"- 操作：`{item.operation}`",
            "- 人工 reconciliation："
            + ("需要" if item.manual_reconciliation_required else "不需要"),
            f"- 平台：{', '.join(item.required_platforms)}",
            "",
            "**本回执只授权未来隔离 rebase/revalidation 输入；当前没有执行 Git write、",
            "merge、push、publish 或 Promotion。**",
        )
    )


def _request_matches_sources(
    request: EvolutionRevalidationRequest,
    decision: EvolutionPromotionApprovalDecisionReceipt,
    package: EvolutionPromotionPackage,
) -> bool:
    return bool(
        request.workspace_root == decision.workspace_root == package.workspace_root
        and request.decision_id == decision.decision_id
        and request.decision_sha256 == decision.decision_sha256
        and request.decision_source_set_sha256 == decision.source_set_sha256
        and request.requirement_id == decision.requirement_id
        and request.requirement_sha256 == decision.requirement_sha256
        and request.package_id == decision.package_id == package.package_id
        and request.package_sha256 == decision.package_sha256 == package.package_sha256
        and request.promotion_input_id == package.promotion_input_id
        and request.promotion_input_sha256 == package.promotion_input_sha256
        and request.candidate_id == package.candidate_id
        and request.candidate_revision == package.candidate_revision
        and request.reflection_id == package.reflection_id
        and request.reflection_sha256 == package.reflection_sha256
        and request.target_branch == decision.target_branch == package.target.target_branch
        and request.target_head == decision.target_head == package.target.target_head
        and request.target_tree == decision.target_tree == package.target.target_tree
        and request.baseline_commit == package.target.baseline_commit
        and request.target_relation == package.target.relation_to_baseline
        and request.patch_manifest_sha256 == package.signature_envelope.patch_manifest_sha256
        and request.baseline_sha256 == package.signature_envelope.baseline_sha256
        and request.migration_assessment_sha256
        == package.signature_envelope.migration_assessment_sha256
        and request.rollback_plan_sha256 == package.signature_envelope.rollback_plan_sha256
        and request.required_platforms == tuple(sorted(package.approval_input.required_platforms))
    )


def _validated_request(value: object) -> EvolutionRevalidationRequest:
    try:
        if not isinstance(value, EvolutionRevalidationRequest):
            raise TypeError("request type invalid")
        return EvolutionRevalidationRequest.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationRequestError(
            "revalidation_request_artifact_invalid",
            "Revalidation Request artifact 无法验证。",
        ) from exc


def _from_row(row: aiosqlite.Row) -> EvolutionRevalidationRequest:
    try:
        item = EvolutionRevalidationRequest.model_validate_json(row["request_json"])
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationRequestError(
            "revalidation_request_store_corrupt",
            "Revalidation Request artifact 损坏。",
        ) from exc
    if not (
        row["request_id"] == item.request_id
        and row["request_sha256"] == item.request_sha256
        and row["decision_id"] == item.decision_id
        and row["decision_sha256"] == item.decision_sha256
        and row["package_id"] == item.package_id
        and row["package_sha256"] == item.package_sha256
        and row["workspace_root"] == item.workspace_root
        and row["target_branch"] == item.target_branch
        and row["target_head"] == item.target_head
    ):
        raise EvolutionRevalidationRequestError(
            "revalidation_request_store_corrupt",
            "Revalidation Request Store 索引与 artifact 不一致。",
        )
    return item


def _decision_from_json(value: str) -> EvolutionPromotionApprovalDecisionReceipt:
    try:
        return EvolutionPromotionApprovalDecisionReceipt.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationRequestError(
            "revalidation_request_source_store_corrupt",
            "Approval Decision authority artifact 损坏。",
        ) from exc


def _package_from_json(value: str) -> EvolutionPromotionPackage:
    try:
        return EvolutionPromotionPackage.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationRequestError(
            "revalidation_request_source_store_corrupt",
            "Promotion Package authority artifact 损坏。",
        ) from exc


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_requests (
            request_id TEXT PRIMARY KEY,
            request_sha256 TEXT NOT NULL,
            decision_id TEXT NOT NULL UNIQUE,
            decision_sha256 TEXT NOT NULL,
            package_id TEXT NOT NULL,
            package_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            target_head TEXT NOT NULL,
            request_json TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_evolution_revalidation_package "
        "ON evolution_revalidation_requests(package_id, target_head)"
    )


def _sha256_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_REQUEST_POLICY",
    "EvolutionRevalidationRequest",
    "EvolutionRevalidationRequestBuilder",
    "EvolutionRevalidationRequestError",
    "EvolutionRevalidationRequestService",
    "EvolutionRevalidationRequestStore",
    "EvolutionRevalidationRequestView",
    "render_evolution_revalidation_request",
]
