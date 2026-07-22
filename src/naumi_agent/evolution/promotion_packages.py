"""Review-only Promotion Packages bound to an exact local target branch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputError,
    EvolutionPromotionPackageInputStore,
    _require_active_promotion_input,
)
from naumi_agent.evolution.promotion_package_inputs import (
    _ensure_schema as _ensure_input_schema,
)

EVOLUTION_PROMOTION_PACKAGE_POLICY = "evolution-promotion-package-v1"
EVOLUTION_PROMOTION_TARGET_POLICY = "evolution-promotion-target-snapshot-v1"
EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY = "evolution-promotion-approval-input-v1"
EVOLUTION_PROMOTION_SIGNATURE_DOMAIN = "naumi.evolution.promotion-package.review.v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_BRANCH_RE = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$"
_MAX_PACKAGE_BYTES = 512 * 1_024


class EvolutionPromotionTargetRelation(StrEnum):
    SAME = "same"
    ADVANCED = "advanced"
    DIVERGED = "diverged"


class EvolutionPromotionApprovalSignal(StrEnum):
    HIGH_RISK = "high_risk"
    CRITICAL_RISK = "critical_risk"
    PROTECTED_TARGET = "protected_target"
    PROTECTED_SCOPE = "protected_scope"
    MIGRATION_REVIEW = "migration_review"
    DATA_BACKUP = "data_backup"
    TARGET_ADVANCED = "target_advanced"
    TARGET_DIVERGED = "target_diverged"


class EvolutionPromotionProtectedScope(StrEnum):
    AUTHORIZATION = "authorization"
    CI_RELEASE = "ci_release"
    DEPENDENCY = "dependency"
    PERSISTENCE = "persistence"
    SECURITY = "security"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPromotionProtectedPath(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    scope: EvolutionPromotionProtectedScope

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(char in normalized for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Promotion protected path 必须是安全相对路径。")
        return normalized


class EvolutionPromotionTargetSnapshot(_StrictModel):
    policy_version: Literal["evolution-promotion-target-snapshot-v1"] = (
        EVOLUTION_PROMOTION_TARGET_POLICY
    )
    repository_root: str = Field(min_length=1, max_length=4_096)
    target_branch: str = Field(pattern=_BRANCH_RE, max_length=255)
    target_ref: str = Field(min_length=12, max_length=300)
    target_head: str = Field(pattern=_GIT_OBJECT_RE)
    target_tree: str = Field(pattern=_GIT_OBJECT_RE)
    baseline_commit: str = Field(pattern=_GIT_OBJECT_RE)
    relation_to_baseline: EvolutionPromotionTargetRelation
    rebase_required: bool
    revalidation_required: bool
    git_write_executed: Literal[False] = False
    snapshot_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _snapshot_is_exact(self) -> Self:
        if self.repository_root != str(Path(self.repository_root).expanduser().resolve()):
            raise ValueError("Promotion target repository_root 必须是 canonical 路径。")
        if self.target_ref != f"refs/heads/{self.target_branch}":
            raise ValueError("Promotion target ref/branch 不一致。")
        moved = self.relation_to_baseline is not EvolutionPromotionTargetRelation.SAME
        if self.rebase_required is not moved or self.revalidation_required is not moved:
            raise ValueError("Promotion target rebase/revalidation 投影不一致。")
        if (
            self.relation_to_baseline is EvolutionPromotionTargetRelation.SAME
            and self.target_head != self.baseline_commit
        ):
            raise ValueError("Promotion target same relation 与 commit 不一致。")
        expected = _sha256_payload(self.model_dump(mode="json", exclude={"snapshot_sha256"}))
        if not hmac.compare_digest(self.snapshot_sha256, expected):
            raise ValueError("Promotion target snapshot 摘要不一致。")
        return self


class EvolutionPromotionApprovalInput(_StrictModel):
    policy_version: Literal["evolution-promotion-approval-input-v1"] = (
        EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY
    )
    risk_level: Literal["low", "medium", "high", "critical"]
    target_branch: str = Field(pattern=_BRANCH_RE, max_length=255)
    target_branch_protected: bool
    target_relation: EvolutionPromotionTargetRelation
    changed_files: int = Field(ge=1, le=16)
    changed_lines: int = Field(ge=0, le=134_217_728)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    migration_review_required: bool
    data_backup_required: bool
    protected_paths: tuple[EvolutionPromotionProtectedPath, ...] = Field(max_length=16)
    signals: tuple[EvolutionPromotionApprovalSignal, ...] = Field(max_length=8)
    approval_review_required: Literal[True] = True
    approval_policy_evaluated: Literal[False] = False
    approval_decided: Literal[False] = False
    approval_input_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _approval_input_is_exact(self) -> Self:
        paths = tuple((item.path, item.scope.value) for item in self.protected_paths)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("Promotion protected paths 必须排序且不得重复。")
        if self.signals != tuple(sorted(set(self.signals), key=lambda item: item.value)):
            raise ValueError("Promotion approval signals 必须排序且不得重复。")
        expected_signals = _approval_signals(
            risk_level=self.risk_level,
            target_protected=self.target_branch_protected,
            relation=self.target_relation,
            protected_paths=self.protected_paths,
            migration_review_required=self.migration_review_required,
            data_backup_required=self.data_backup_required,
        )
        if self.signals != expected_signals:
            raise ValueError("Promotion approval signals 投影不一致。")
        expected = _sha256_payload(self.model_dump(mode="json", exclude={"approval_input_sha256"}))
        if not hmac.compare_digest(self.approval_input_sha256, expected):
            raise ValueError("Promotion approval input 摘要不一致。")
        return self


class EvolutionPromotionSignatureEnvelope(_StrictModel):
    domain: Literal["naumi.evolution.promotion-package.review.v1"] = (
        EVOLUTION_PROMOTION_SIGNATURE_DOMAIN
    )
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    target_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    patch_manifest_sha256: str = Field(pattern=_SHA256_RE)
    baseline_sha256: str = Field(pattern=_SHA256_RE)
    migration_assessment_sha256: str = Field(pattern=_SHA256_RE)
    rollback_plan_sha256: str = Field(pattern=_SHA256_RE)
    approval_input_sha256: str = Field(pattern=_SHA256_RE)
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    signature_policy_evaluated: Literal[False] = False
    signature_collected: Literal[False] = False

    @model_validator(mode="after")
    def _envelope_is_exact(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={
                    "signable_payload_sha256",
                    "signature_policy_evaluated",
                    "signature_collected",
                },
            )
        )
        if not hmac.compare_digest(self.signable_payload_sha256, expected):
            raise ValueError("Promotion signature payload 摘要不一致。")
        return self


class EvolutionPromotionPackage(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-promotion-package-v1"] = EVOLUTION_PROMOTION_PACKAGE_POLICY
    package_id: str = Field(pattern=r"^evpromopkg_[0-9a-f]{24}$")
    package_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    reflection_id: str = Field(pattern=r"^evreflection_[0-9a-f]{24}$")
    reflection_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    target: EvolutionPromotionTargetSnapshot
    approval_input: EvolutionPromotionApprovalInput
    signature_envelope: EvolutionPromotionSignatureEnvelope
    package_complete: Literal[True] = True
    package_review_only: Literal[True] = True
    package_input_embedded: Literal[False] = False
    approval_policy_evaluated: Literal[False] = False
    approval_decided: Literal[False] = False
    signature_collected: Literal[False] = False
    rebase_executed: Literal[False] = False
    revalidation_executed: Literal[False] = False
    promotion_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    contains_source_code: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    llm_generated: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _package_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Promotion Package workspace 必须是 canonical 路径。")
        if not (
            self.target.repository_root == self.workspace_root
            and self.approval_input.risk_level == self.risk_level
            and self.approval_input.target_branch == self.target.target_branch
            and self.approval_input.target_relation == self.target.relation_to_baseline
            and self.signature_envelope.promotion_input_id == self.promotion_input_id
            and self.signature_envelope.promotion_input_sha256 == self.promotion_input_sha256
            and self.signature_envelope.target_snapshot_sha256 == self.target.snapshot_sha256
            and self.signature_envelope.approval_input_sha256
            == self.approval_input.approval_input_sha256
        ):
            raise ValueError("Promotion Package authority 投影不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"package_id", "package_sha256"})
        )
        if not hmac.compare_digest(self.package_sha256, digest):
            raise ValueError("Promotion Package 摘要不一致。")
        if self.package_id != f"evpromopkg_{digest[:24]}":
            raise ValueError("Promotion Package identity 不一致。")
        return self


class EvolutionPromotionPackageView(_StrictModel):
    package: EvolutionPromotionPackage
    promotion_input_active: bool
    reflection_active: bool
    target_available: bool
    target_current: bool
    current_target_head: str | None = Field(default=None, pattern=_GIT_OBJECT_RE)
    package_review_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        if self.promotion_input_active is not self.reflection_active:
            raise ValueError("Promotion Package Input/Reflection 状态不一致。")
        if self.target_current and not self.target_available:
            raise ValueError("Promotion Package target current/available 状态不一致。")
        expected = bool(
            self.promotion_input_active
            and self.reflection_active
            and self.target_available
            and self.target_current
        )
        if self.package_review_eligible is not expected:
            raise ValueError("Promotion Package review eligibility 投影不一致。")
        return self


class EvolutionPromotionPackageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPromotionTargetProbe:
    """Read-only target-branch identity probe with no shell or Git writes."""

    def capture(
        self,
        *,
        workspace_root: str | Path,
        target_branch: str,
        baseline_commit: str,
    ) -> EvolutionPromotionTargetSnapshot:
        root = _workspace(workspace_root)
        branch = _branch(target_branch)
        try:
            top = Path(self._git(root, "rev-parse", "--show-toplevel")).resolve()
            if top != root:
                raise EvolutionPromotionPackageError(
                    "promotion_package_repository_mismatch",
                    "Promotion Package workspace 必须是 Git repository root。",
                )
            self._git(root, "check-ref-format", "--branch", branch)
            target_ref = f"refs/heads/{branch}"
            target_head = self._git(
                root,
                "rev-parse",
                "--verify",
                f"{target_ref}^{{commit}}",
            ).lower()
            resolved_baseline = self._git(
                root,
                "rev-parse",
                "--verify",
                f"{baseline_commit}^{{commit}}",
            ).lower()
            if resolved_baseline != baseline_commit.lower():
                raise EvolutionPromotionPackageError(
                    "promotion_package_baseline_mismatch",
                    "Promotion Input baseline commit 无法精确解析。",
                )
            target_tree = self._git(
                root,
                "rev-parse",
                "--verify",
                f"{target_head}^{{tree}}",
            ).lower()
            relation = self._relation(root, baseline_commit.lower(), target_head)
        except EvolutionPromotionPackageError:
            raise
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_target_probe_failed",
                "无法读取 Promotion target branch authority。",
            ) from exc
        payload = {
            "policy_version": EVOLUTION_PROMOTION_TARGET_POLICY,
            "repository_root": str(root),
            "target_branch": branch,
            "target_ref": target_ref,
            "target_head": target_head,
            "target_tree": target_tree,
            "baseline_commit": baseline_commit.lower(),
            "relation_to_baseline": relation.value,
            "rebase_required": relation is not EvolutionPromotionTargetRelation.SAME,
            "revalidation_required": (relation is not EvolutionPromotionTargetRelation.SAME),
            "git_write_executed": False,
        }
        return EvolutionPromotionTargetSnapshot.model_validate(
            {**payload, "snapshot_sha256": _sha256_payload(payload)}
        )

    def _relation(
        self,
        root: Path,
        baseline_commit: str,
        target_head: str,
    ) -> EvolutionPromotionTargetRelation:
        if baseline_commit == target_head:
            return EvolutionPromotionTargetRelation.SAME
        result = self._run_git(
            root,
            "merge-base",
            "--is-ancestor",
            baseline_commit,
            target_head,
            check=False,
        )
        if result.returncode == 0:
            return EvolutionPromotionTargetRelation.ADVANCED
        if result.returncode == 1:
            return EvolutionPromotionTargetRelation.DIVERGED
        raise EvolutionPromotionPackageError(
            "promotion_package_relation_failed",
            "无法判断 Promotion target 与 baseline 的关系。",
        )

    def _git(self, root: Path, *args: str) -> str:
        result = self._run_git(root, *args, check=True)
        value = result.stdout.decode("utf-8", errors="strict").strip()
        if not value:
            raise EvolutionPromotionPackageError(
                "promotion_package_git_output_empty",
                "Git 未返回 Promotion target authority。",
            )
        return value

    @staticmethod
    def _run_git(
        root: Path,
        *args: str,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        env = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if check and result.returncode != 0:
            raise EvolutionPromotionPackageError(
                "promotion_package_git_command_failed",
                "Git 无法解析 Promotion target branch。",
            )
        return result


class EvolutionPromotionPackageBuilder:
    def build(
        self,
        *,
        package_input: EvolutionPromotionPackageInput,
        target: EvolutionPromotionTargetSnapshot,
    ) -> EvolutionPromotionPackage:
        try:
            item = EvolutionPromotionPackageInput.model_validate_json(
                package_input.model_dump_json()
            )
            snapshot = EvolutionPromotionTargetSnapshot.model_validate_json(
                target.model_dump_json()
            )
            if not (
                item.workspace_root == snapshot.repository_root
                and item.baseline.baseline_commit == snapshot.baseline_commit
            ):
                raise EvolutionPromotionPackageError(
                    "promotion_package_target_mismatch",
                    "Promotion Input 与 target snapshot 不一致。",
                )
            approval = _approval_input(item, snapshot)
            envelope = _signature_envelope(item, snapshot, approval)
        except EvolutionPromotionPackageError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_authority_invalid",
                "Promotion Package authority 无效或已被篡改。",
            ) from exc
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_PROMOTION_PACKAGE_POLICY,
            "workspace_root": item.workspace_root,
            "candidate_id": item.candidate_id,
            "candidate_revision": item.candidate_revision,
            "risk_level": item.risk_level,
            "reflection_id": item.reflection_id,
            "reflection_sha256": item.reflection_sha256,
            "promotion_input_id": item.input_id,
            "promotion_input_sha256": item.input_sha256,
            "target": snapshot.model_dump(mode="json"),
            "approval_input": approval.model_dump(mode="json"),
            "signature_envelope": envelope.model_dump(mode="json"),
            "package_complete": True,
            "package_review_only": True,
            "package_input_embedded": False,
            "approval_policy_evaluated": False,
            "approval_decided": False,
            "signature_collected": False,
            "rebase_executed": False,
            "revalidation_executed": False,
            "promotion_executed": False,
            "git_write_executed": False,
            "merge_executed": False,
            "push_executed": False,
            "publish_executed": False,
            "contains_source_code": False,
            "contains_freeform_narrative": False,
            "llm_generated": False,
            "created_at": item.created_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionPromotionPackage.model_validate(
                {
                    **payload,
                    "package_id": f"evpromopkg_{digest[:24]}",
                    "package_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_artifact_invalid",
                "Promotion Package artifact 无法验证。",
            ) from exc


class EvolutionPromotionPackageStore:
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
        package: EvolutionPromotionPackage,
        *,
        package_input: EvolutionPromotionPackageInput,
    ) -> EvolutionPromotionPackage:
        try:
            item = EvolutionPromotionPackage.model_validate_json(package.model_dump_json())
            source = EvolutionPromotionPackageInput.model_validate_json(
                package_input.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_artifact_invalid",
                "Promotion Package 或 Input 无效。",
            ) from exc
        if not _package_matches_input(item, source):
            raise EvolutionPromotionPackageError(
                "promotion_package_input_mismatch",
                "Promotion Package 未绑定 exact Input。",
            )
        current_target = await asyncio.to_thread(
            self._target_probe.capture,
            workspace_root=item.workspace_root,
            target_branch=item.target.target_branch,
            baseline_commit=source.baseline.baseline_commit,
        )
        if current_target != item.target:
            raise EvolutionPromotionPackageError(
                "promotion_package_target_changed",
                "Promotion target 在 Package 落库前已变化，请重新生成。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_PACKAGE_BYTES:
            raise EvolutionPromotionPackageError(
                "promotion_package_oversized",
                "Promotion Package 超过 512 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_input_schema(db)
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await _require_active_promotion_input(db, source)
                except EvolutionPromotionPackageInputError as exc:
                    raise EvolutionPromotionPackageError(
                        "promotion_package_input_ineligible",
                        "Promotion Package Input 已失效。",
                    ) from exc
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_packages "
                        "WHERE promotion_input_id = ? AND target_branch = ? "
                        "AND target_head = ?",
                        (
                            item.promotion_input_id,
                            item.target.target_branch,
                            item.target.target_head,
                        ),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionPromotionPackageError(
                            "promotion_package_conflict",
                            "同一 Input/target snapshot 不可覆盖为不同 Package。",
                        )
                    await db.rollback()
                    return existing
                await db.execute(
                    "INSERT INTO evolution_promotion_packages "
                    "(package_id, package_sha256, promotion_input_id, "
                    "promotion_input_sha256, reflection_id, workspace_root, "
                    "candidate_id, candidate_revision, target_branch, target_head, "
                    "package_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.package_id,
                        item.package_sha256,
                        item.promotion_input_id,
                        item.promotion_input_sha256,
                        item.reflection_id,
                        item.workspace_root,
                        item.candidate_id,
                        item.candidate_revision,
                        item.target.target_branch,
                        item.target.target_head,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionPromotionPackageError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_store_error",
                "Promotion Package 无法持久化。",
            ) from exc
        restored = await self.get(item.package_id)
        assert restored is not None
        return restored

    async def get(self, package_id: str) -> EvolutionPromotionPackage | None:
        if (
            not isinstance(package_id, str)
            or re.fullmatch(r"evpromopkg_[0-9a-f]{24}", package_id) is None
        ):
            raise ValueError("promotion package id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_packages WHERE package_id = ?",
                        (package_id,),
                    )
                ).fetchone()
                return None if row is None else _from_row(row)
        except EvolutionPromotionPackageError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_store_corrupt",
                "Promotion Package 损坏或无法读取。",
            ) from exc


class EvolutionPromotionPackageExecutor:
    def __init__(
        self,
        *,
        input_store: EvolutionPromotionPackageInputStore,
        package_store: EvolutionPromotionPackageStore,
        builder: EvolutionPromotionPackageBuilder | None = None,
        target_probe: EvolutionPromotionTargetProbe | None = None,
    ) -> None:
        self._input_store = input_store
        self._package_store = package_store
        self._builder = builder or EvolutionPromotionPackageBuilder()
        self._target_probe = target_probe or EvolutionPromotionTargetProbe()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        promotion_input_id: str,
        target_branch: str = "main",
    ) -> EvolutionPromotionPackageView:
        workspace = _workspace(workspace_root)
        if (
            not isinstance(promotion_input_id, str)
            or re.fullmatch(r"evpromoin_[0-9a-f]{24}", promotion_input_id) is None
        ):
            raise EvolutionPromotionPackageError(
                "promotion_package_input_id_invalid",
                "Promotion Package Input ID 格式无效。",
            )
        try:
            source_view = await self._input_store.get(promotion_input_id)
        except (EvolutionPromotionPackageInputError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_input_read_failed",
                "无法读取 Promotion Package Input authority。",
            ) from exc
        if source_view is None:
            raise EvolutionPromotionPackageError(
                "promotion_package_input_missing",
                "Promotion Package Input 不存在。",
            )
        source = source_view.package_input
        if source.workspace_root != str(workspace):
            raise EvolutionPromotionPackageError(
                "promotion_package_workspace_mismatch",
                "Promotion Package Input 不属于当前工作区。",
            )
        if not source_view.promotion_review_eligible:
            raise EvolutionPromotionPackageError(
                "promotion_package_input_ineligible",
                "Promotion Package Input 已失效。",
            )
        target = await asyncio.to_thread(
            self._target_probe.capture,
            workspace_root=workspace,
            target_branch=target_branch,
            baseline_commit=source.baseline.baseline_commit,
        )
        package = self._builder.build(package_input=source, target=target)
        stored = await self._package_store.record(package, package_input=source)
        return await self.inspect(
            workspace_root=workspace,
            package_id=stored.package_id,
        )

    async def inspect(
        self,
        *,
        workspace_root: str | Path,
        package_id: str,
    ) -> EvolutionPromotionPackageView:
        workspace = _workspace(workspace_root)
        try:
            stored = await self._package_store.get(package_id)
        except (OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_read_failed",
                "无法读取 Promotion Package authority。",
            ) from exc
        if stored is None:
            raise EvolutionPromotionPackageError(
                "promotion_package_missing",
                "Promotion Package 不存在。",
            )
        if stored.workspace_root != str(workspace):
            raise EvolutionPromotionPackageError(
                "promotion_package_workspace_mismatch",
                "Promotion Package 不属于当前工作区。",
            )
        try:
            source_view = await self._input_store.get(stored.promotion_input_id)
        except (EvolutionPromotionPackageInputError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageError(
                "promotion_package_input_read_failed",
                "无法读取 Promotion Package Input authority。",
            ) from exc
        if source_view is None or not _package_matches_input(
            stored,
            source_view.package_input,
        ):
            raise EvolutionPromotionPackageError(
                "promotion_package_input_mismatch",
                "Promotion Package 的 Input authority 缺失或不一致。",
            )
        active = source_view.promotion_review_eligible
        try:
            current = await asyncio.to_thread(
                self._target_probe.capture,
                workspace_root=workspace,
                target_branch=stored.target.target_branch,
                baseline_commit=source_view.package_input.baseline.baseline_commit,
            )
        except EvolutionPromotionPackageError:
            return EvolutionPromotionPackageView(
                package=stored,
                promotion_input_active=active,
                reflection_active=active,
                target_available=False,
                target_current=False,
                current_target_head=None,
                package_review_eligible=False,
            )
        target_current = bool(
            current.target_head == stored.target.target_head
            and current.target_tree == stored.target.target_tree
        )
        return EvolutionPromotionPackageView(
            package=stored,
            promotion_input_active=active,
            reflection_active=active,
            target_available=True,
            target_current=target_current,
            current_target_head=current.target_head,
            package_review_eligible=active and target_current,
        )


def render_evolution_promotion_package(view: EvolutionPromotionPackageView) -> str:
    item = EvolutionPromotionPackageView.model_validate(view.model_dump(mode="python"))
    package = item.package
    signals = ", ".join(value.value for value in package.approval_input.signals) or "none"
    lines = [
        f"# Evolution Promotion Package `{package.package_id}`",
        "",
        "**完整审查 Package 已冻结；尚未审批、签名、rebase、合并、推送或发布。**",
        "",
        f"- Promotion Input：`{package.promotion_input_id}` · "
        f"{'active' if item.promotion_input_active else 'inactive'}",
        f"- Target：`{package.target.target_branch}` @ `{package.target.target_head[:12]}`",
        f"- Target relation：`{package.target.relation_to_baseline.value}`",
        f"- Target current：{'是' if item.target_current else '否'}",
        f"- Review eligible：{'是' if item.package_review_eligible else '否'}",
        f"- Risk：`{package.risk_level}`",
        f"- Approval signals：`{signals}`",
        f"- Protected paths：{len(package.approval_input.protected_paths)}",
        f"- Signable payload：`{package.signature_envelope.signable_payload_sha256}`",
        "- Approval policy evaluated：`false`",
        "- Signature collected：`false`",
        "- Git/Merge/Push/Publish：`false`",
        f"- Package SHA-256：`{package.package_sha256}`",
        "",
        "下一步：EVO-05.2 从仍 eligible 的 Package 评估审批与签名要求；"
        "任何 target 移动都必须重新生成或执行 EVO-05.3 rebase/revalidate。",
    ]
    return "\n".join(lines)


def _approval_input(
    item: EvolutionPromotionPackageInput,
    target: EvolutionPromotionTargetSnapshot,
) -> EvolutionPromotionApprovalInput:
    protected = _protected_paths(tuple(value.path for value in item.patch.files))
    target_protected = _target_is_protected(target.target_branch)
    signals = _approval_signals(
        risk_level=item.risk_level,
        target_protected=target_protected,
        relation=target.relation_to_baseline,
        protected_paths=protected,
        migration_review_required=item.migration.review_required,
        data_backup_required=item.migration.data_backup_required,
    )
    payload = {
        "policy_version": EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY,
        "risk_level": item.risk_level,
        "target_branch": target.target_branch,
        "target_branch_protected": target_protected,
        "target_relation": target.relation_to_baseline.value,
        "changed_files": len(item.patch.files),
        "changed_lines": item.patch.total_added_lines + item.patch.total_deleted_lines,
        "required_platforms": list(item.required_platforms),
        "migration_review_required": item.migration.review_required,
        "data_backup_required": item.migration.data_backup_required,
        "protected_paths": [value.model_dump(mode="json") for value in protected],
        "signals": [value.value for value in signals],
        "approval_review_required": True,
        "approval_policy_evaluated": False,
        "approval_decided": False,
    }
    return EvolutionPromotionApprovalInput.model_validate(
        {**payload, "approval_input_sha256": _sha256_payload(payload)}
    )


def _signature_envelope(
    item: EvolutionPromotionPackageInput,
    target: EvolutionPromotionTargetSnapshot,
    approval: EvolutionPromotionApprovalInput,
) -> EvolutionPromotionSignatureEnvelope:
    payload = {
        "domain": EVOLUTION_PROMOTION_SIGNATURE_DOMAIN,
        "promotion_input_id": item.input_id,
        "promotion_input_sha256": item.input_sha256,
        "target_snapshot_sha256": target.snapshot_sha256,
        "patch_manifest_sha256": item.patch.manifest_sha256,
        "baseline_sha256": item.baseline.baseline_sha256,
        "migration_assessment_sha256": item.migration.assessment_sha256,
        "rollback_plan_sha256": item.rollback.plan_sha256,
        "approval_input_sha256": approval.approval_input_sha256,
    }
    signable = _sha256_payload(payload)
    return EvolutionPromotionSignatureEnvelope.model_validate(
        {
            **payload,
            "signable_payload_sha256": signable,
            "signature_policy_evaluated": False,
            "signature_collected": False,
        }
    )


def _approval_signals(
    *,
    risk_level: str,
    target_protected: bool,
    relation: EvolutionPromotionTargetRelation,
    protected_paths: Sequence[EvolutionPromotionProtectedPath],
    migration_review_required: bool,
    data_backup_required: bool,
) -> tuple[EvolutionPromotionApprovalSignal, ...]:
    signals: set[EvolutionPromotionApprovalSignal] = set()
    if risk_level == "high":
        signals.add(EvolutionPromotionApprovalSignal.HIGH_RISK)
    elif risk_level == "critical":
        signals.add(EvolutionPromotionApprovalSignal.CRITICAL_RISK)
    if target_protected:
        signals.add(EvolutionPromotionApprovalSignal.PROTECTED_TARGET)
    if protected_paths:
        signals.add(EvolutionPromotionApprovalSignal.PROTECTED_SCOPE)
    if migration_review_required:
        signals.add(EvolutionPromotionApprovalSignal.MIGRATION_REVIEW)
    if data_backup_required:
        signals.add(EvolutionPromotionApprovalSignal.DATA_BACKUP)
    if relation is EvolutionPromotionTargetRelation.ADVANCED:
        signals.add(EvolutionPromotionApprovalSignal.TARGET_ADVANCED)
    elif relation is EvolutionPromotionTargetRelation.DIVERGED:
        signals.add(EvolutionPromotionApprovalSignal.TARGET_DIVERGED)
    return tuple(sorted(signals, key=lambda item: item.value))


def _protected_paths(paths: tuple[str, ...]) -> tuple[EvolutionPromotionProtectedPath, ...]:
    dependency_names = {
        "cargo.lock",
        "cargo.toml",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }
    found: list[EvolutionPromotionProtectedPath] = []
    for value in paths:
        folded = value.casefold()
        parts = tuple(part for part in folded.split("/") if part)
        scopes: set[EvolutionPromotionProtectedScope] = set()
        if any(part in {"auth", "authorization", "permissions", "safety"} for part in parts):
            scopes.add(EvolutionPromotionProtectedScope.AUTHORIZATION)
        if folded.startswith(".github/workflows/") or any(
            part in {"build", "installer", "release", "packaging"} for part in parts
        ):
            scopes.add(EvolutionPromotionProtectedScope.CI_RELEASE)
        if parts and parts[-1] in dependency_names:
            scopes.add(EvolutionPromotionProtectedScope.DEPENDENCY)
        if folded.endswith(".sql") or any(
            part in {"migrations", "persistence", "schema", "schemas", "state"} for part in parts
        ):
            scopes.add(EvolutionPromotionProtectedScope.PERSISTENCE)
        if any(part in {"crypto", "secrets", "security"} for part in parts):
            scopes.add(EvolutionPromotionProtectedScope.SECURITY)
        for scope in sorted(scopes, key=lambda item: item.value):
            found.append(EvolutionPromotionProtectedPath(path=value, scope=scope))
    return tuple(sorted(found, key=lambda item: (item.path, item.scope.value)))


def _target_is_protected(branch: str) -> bool:
    folded = branch.casefold()
    return bool(
        folded in {"main", "master", "stable"}
        or folded.startswith("release/")
        or re.fullmatch(r"v?\d+(?:\.\d+){1,2}", folded) is not None
    )


def _package_matches_input(
    package: EvolutionPromotionPackage,
    item: EvolutionPromotionPackageInput,
) -> bool:
    return bool(
        package.workspace_root == item.workspace_root
        and package.candidate_id == item.candidate_id
        and package.candidate_revision == item.candidate_revision
        and package.risk_level == item.risk_level
        and package.reflection_id == item.reflection_id
        and package.reflection_sha256 == item.reflection_sha256
        and package.promotion_input_id == item.input_id
        and package.promotion_input_sha256 == item.input_sha256
        and package.target.baseline_commit == item.baseline.baseline_commit
        and package.signature_envelope.patch_manifest_sha256 == item.patch.manifest_sha256
        and package.signature_envelope.baseline_sha256 == item.baseline.baseline_sha256
        and package.signature_envelope.migration_assessment_sha256
        == item.migration.assessment_sha256
        and package.signature_envelope.rollback_plan_sha256 == item.rollback.plan_sha256
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_promotion_packages (
            package_id TEXT PRIMARY KEY,
            package_sha256 TEXT NOT NULL,
            promotion_input_id TEXT NOT NULL,
            promotion_input_sha256 TEXT NOT NULL,
            reflection_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            candidate_revision INTEGER NOT NULL,
            target_branch TEXT NOT NULL,
            target_head TEXT NOT NULL,
            package_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(promotion_input_id, target_branch, target_head)
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionPromotionPackage:
    encoded = str(row["package_json"])
    if len(encoded.encode("utf-8")) > _MAX_PACKAGE_BYTES:
        raise ValueError("Promotion Package Store artifact 过大。")
    item = EvolutionPromotionPackage.model_validate_json(encoded)
    if not (
        row["package_id"] == item.package_id
        and row["package_sha256"] == item.package_sha256
        and row["promotion_input_id"] == item.promotion_input_id
        and row["promotion_input_sha256"] == item.promotion_input_sha256
        and row["reflection_id"] == item.reflection_id
        and row["workspace_root"] == item.workspace_root
        and row["candidate_id"] == item.candidate_id
        and row["candidate_revision"] == item.candidate_revision
        and row["target_branch"] == item.target.target_branch
        and row["target_head"] == item.target.target_head
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Promotion Package Store index 不一致。")
    return item


def _workspace(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError("workspace_root 类型无效。")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace_root 不存在。")
    return root


def _branch(value: str) -> str:
    if not isinstance(value, str):
        raise EvolutionPromotionPackageError(
            "promotion_package_target_branch_invalid",
            "Promotion target branch 类型无效。",
        )
    branch = value.strip()
    if (
        re.fullmatch(_BRANCH_RE, branch) is None
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", ".", ".lock"))
        or branch.startswith("-")
        or "@{" in branch
    ):
        raise EvolutionPromotionPackageError(
            "promotion_package_target_branch_invalid",
            "Promotion target branch 格式无效。",
        )
    return branch


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_PROMOTION_APPROVAL_INPUT_POLICY",
    "EVOLUTION_PROMOTION_PACKAGE_POLICY",
    "EVOLUTION_PROMOTION_SIGNATURE_DOMAIN",
    "EVOLUTION_PROMOTION_TARGET_POLICY",
    "EvolutionPromotionApprovalInput",
    "EvolutionPromotionApprovalSignal",
    "EvolutionPromotionPackage",
    "EvolutionPromotionPackageBuilder",
    "EvolutionPromotionPackageError",
    "EvolutionPromotionPackageExecutor",
    "EvolutionPromotionPackageStore",
    "EvolutionPromotionPackageView",
    "EvolutionPromotionProtectedPath",
    "EvolutionPromotionProtectedScope",
    "EvolutionPromotionSignatureEnvelope",
    "EvolutionPromotionTargetProbe",
    "EvolutionPromotionTargetRelation",
    "EvolutionPromotionTargetSnapshot",
    "render_evolution_promotion_package",
]
