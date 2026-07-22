"""Non-executable, tamper-evident inputs for controlled Evolution promotion."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.decision_states import (
    EvolutionDecisionState,
    EvolutionDecisionStateError,
    EvolutionDecisionStateStore,
    EvolutionDecisionStateValue,
)
from naumi_agent.evolution.reflection_memories import (
    EvolutionReflectionAction,
    EvolutionReflectionLessonKind,
    EvolutionReflectionMemory,
    EvolutionReflectionMemoryError,
    EvolutionReflectionMemoryStore,
    EvolutionReflectionMemoryView,
)

EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY = "evolution-promotion-package-input-v1"
EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY = "evolution-promotion-patch-manifest-v1"
EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY = (
    "evolution-promotion-migration-assessment-v1"
)
EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY = "evolution-promotion-rollback-plan-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_MAX_ARTIFACT_BYTES = 2 * 1_024 * 1_024


class EvolutionPromotionEvidenceKind(StrEnum):
    EXPERIMENT_CONTRACT = "experiment_contract"
    SOURCE_SNAPSHOT = "source_snapshot"
    MUTATION_RECEIPT = "mutation_receipt"
    VALIDATION_PLAN = "validation_plan"
    EVALUATION_AGGREGATION = "evaluation_aggregation"
    FINAL_EVALUATION = "final_evaluation"
    EVALUATION_LANE = "evaluation_lane"
    RED_COMPLETION = "red_completion"
    GREEN_COMPLETION = "green_completion"
    COMPARISON_RECEIPT = "comparison_receipt"
    FAILURE_ATTRIBUTION = "failure_attribution"
    MECHANICAL_GATE = "mechanical_gate"
    INDEPENDENT_REVIEW = "independent_review"
    COUNTERFACTUAL = "counterfactual"
    REWARD_HACKING = "reward_hacking"
    DECISION_STATE = "decision_state"
    REFLECTION_MEMORY = "reflection_memory"


class EvolutionPromotionMigrationSignal(StrEnum):
    DATABASE_MIGRATION_PATH = "database_migration_path"
    STATE_SCHEMA_PATH = "state_schema_path"
    DEPENDENCY_MANIFEST = "dependency_manifest"
    ADDITIVE_API_CHANGE = "additive_api_change"


class EvolutionPromotionRollbackOperation(StrEnum):
    RESTORE_BASELINE_BLOB = "restore_baseline_blob"
    REMOVE_CREATED_FILE = "remove_created_file"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPromotionEvidenceRef(_StrictModel):
    order: int = Field(ge=1, le=40)
    kind: EvolutionPromotionEvidenceKind
    authority_id: str = Field(min_length=1, max_length=128)
    authority_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _identity_matches_kind(self) -> Self:
        patterns = {
            EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT: r"^evx_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.SOURCE_SNAPSHOT: r"^evs_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.MUTATION_RECEIPT: r"^evmr_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.VALIDATION_PLAN: r"^evvplan_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.EVALUATION_AGGREGATION: r"^evagg_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.FINAL_EVALUATION: r"^evfinal_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.EVALUATION_LANE: r"^evlane_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.RED_COMPLETION: (
                r"^(?:evvred(?:run|cohort)|evadvcohort)_[0-9a-f]{24}$"
            ),
            EvolutionPromotionEvidenceKind.GREEN_COMPLETION: (
                r"^(?:evvgreen(?:run|cohort)|evadvcohort)_[0-9a-f]{24}$"
            ),
            EvolutionPromotionEvidenceKind.COMPARISON_RECEIPT: _SHA256_RE,
            EvolutionPromotionEvidenceKind.FAILURE_ATTRIBUTION: r"^evattr_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.MECHANICAL_GATE: r"^evgate_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.INDEPENDENT_REVIEW: r"^evreview_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.COUNTERFACTUAL: r"^evcounter_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.REWARD_HACKING: r"^evreward_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.DECISION_STATE: r"^evdecision_[0-9a-f]{24}$",
            EvolutionPromotionEvidenceKind.REFLECTION_MEMORY: r"^evreflection_[0-9a-f]{24}$",
        }
        if re.fullmatch(patterns[self.kind], self.authority_id) is None:
            raise ValueError("Promotion evidence authority ID 与 kind 不一致。")
        return self


class EvolutionPromotionPatchFile(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    unified_diff_sha256: str = Field(pattern=_SHA256_RE)
    added_lines: int = Field(ge=0, le=4_194_304)
    deleted_lines: int = Field(ge=0, le=4_194_304)
    api_change: Literal["not_applicable", "unchanged", "additive"]
    mutation_fact_sha256: str = Field(pattern=_SHA256_RE)

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
            raise ValueError("Promotion patch path 必须是安全相对路径。")
        return normalized

    @model_validator(mode="after")
    def _file_fact_is_exact(self) -> Self:
        if (self.operation == "modify") != (self.before_sha256 is not None):
            raise ValueError("Promotion patch operation/before digest 不一致。")
        original_payload = {
            "path": self.path,
            "operation": self.operation,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "unified_diff_sha256": self.unified_diff_sha256,
            "added_lines": self.added_lines,
            "deleted_lines": self.deleted_lines,
            "api_change": self.api_change,
        }
        if not hmac.compare_digest(
            self.mutation_fact_sha256,
            _sha256_payload(original_payload),
        ):
            raise ValueError("Promotion patch file 与 Mutation fact 不一致。")
        return self


class EvolutionPromotionPatchManifest(_StrictModel):
    policy_version: Literal["evolution-promotion-patch-manifest-v1"] = (
        EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY
    )
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    files_sha256: str = Field(pattern=_SHA256_RE)
    files: tuple[EvolutionPromotionPatchFile, ...] = Field(min_length=1, max_length=16)
    total_added_lines: int = Field(ge=0, le=67_108_864)
    total_deleted_lines: int = Field(ge=0, le=67_108_864)
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _manifest_is_exact(self) -> Self:
        if tuple(item.order for item in self.files) != tuple(
            range(1, len(self.files) + 1)
        ):
            raise ValueError("Promotion patch files 顺序不连续。")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Promotion patch files 必须排序且不得重复。")
        source_facts = [
            {
                "path": item.path,
                "operation": item.operation,
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
                "unified_diff_sha256": item.unified_diff_sha256,
                "added_lines": item.added_lines,
                "deleted_lines": item.deleted_lines,
                "api_change": item.api_change,
                "fact_sha256": item.mutation_fact_sha256,
            }
            for item in self.files
        ]
        if not (
            self.files_sha256 == _sha256_payload(source_facts)
            and self.total_added_lines == sum(item.added_lines for item in self.files)
            and self.total_deleted_lines == sum(item.deleted_lines for item in self.files)
        ):
            raise ValueError("Promotion patch manifest 与文件事实不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if not hmac.compare_digest(self.manifest_sha256, expected):
            raise ValueError("Promotion patch manifest 摘要不一致。")
        return self


class EvolutionPromotionBaseline(_StrictModel):
    baseline_commit: str = Field(pattern=_GIT_OBJECT_RE)
    workspace_dirty_at_issue: bool
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    experiment_config_sha256: str = Field(pattern=_SHA256_RE)
    toolset_sha256: str = Field(pattern=_SHA256_RE)
    rebase_validation_required: Literal[True] = True
    baseline_update_executed: Literal[False] = False
    baseline_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _baseline_is_exact(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"baseline_sha256"})
        )
        if not hmac.compare_digest(self.baseline_sha256, expected):
            raise ValueError("Promotion baseline 摘要不一致。")
        return self


class EvolutionPromotionMigrationAssessment(_StrictModel):
    policy_version: Literal["evolution-promotion-migration-assessment-v1"] = (
        EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY
    )
    signals: tuple[EvolutionPromotionMigrationSignal, ...] = Field(max_length=4)
    evidence_paths: tuple[str, ...] = Field(max_length=16)
    review_required: bool
    data_backup_required: bool
    irreversible_change_blocked: Literal[True] = True
    migration_execution_allowed: Literal[False] = False
    assessment_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _assessment_is_exact(self) -> Self:
        if self.signals != tuple(sorted(set(self.signals), key=lambda item: item.value)):
            raise ValueError("Migration signals 必须排序且不得重复。")
        if self.evidence_paths != tuple(sorted(set(self.evidence_paths))):
            raise ValueError("Migration evidence paths 必须排序且不得重复。")
        if self.review_required is not bool(self.signals):
            raise ValueError("Migration review_required 投影不一致。")
        backup_signals = {
            EvolutionPromotionMigrationSignal.DATABASE_MIGRATION_PATH,
            EvolutionPromotionMigrationSignal.STATE_SCHEMA_PATH,
        }
        if self.data_backup_required is not bool(set(self.signals) & backup_signals):
            raise ValueError("Migration data_backup_required 投影不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"assessment_sha256"})
        )
        if not hmac.compare_digest(self.assessment_sha256, expected):
            raise ValueError("Migration assessment 摘要不一致。")
        return self


class EvolutionPromotionRollbackStep(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1_024)
    operation: EvolutionPromotionRollbackOperation
    baseline_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    candidate_sha256: str = Field(pattern=_SHA256_RE)


class EvolutionPromotionRollbackPlan(_StrictModel):
    policy_version: Literal["evolution-promotion-rollback-plan-v1"] = (
        EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY
    )
    baseline_commit: str = Field(pattern=_GIT_OBJECT_RE)
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    steps: tuple[EvolutionPromotionRollbackStep, ...] = Field(min_length=1, max_length=16)
    data_restore_required: bool
    rollback_review_required: Literal[True] = True
    rollback_authority: Literal[False] = False
    rollback_executed: Literal[False] = False
    plan_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _plan_is_exact(self) -> Self:
        if tuple(item.order for item in self.steps) != tuple(
            range(1, len(self.steps) + 1)
        ):
            raise ValueError("Rollback steps 顺序不连续。")
        paths = tuple(item.path for item in self.steps)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Rollback steps 必须排序且不得重复。")
        for step in self.steps:
            expected = (
                EvolutionPromotionRollbackOperation.RESTORE_BASELINE_BLOB
                if step.baseline_sha256 is not None
                else EvolutionPromotionRollbackOperation.REMOVE_CREATED_FILE
            )
            if step.operation is not expected:
                raise ValueError("Rollback operation 与 baseline digest 不一致。")
        expected_digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"plan_sha256"})
        )
        if not hmac.compare_digest(self.plan_sha256, expected_digest):
            raise ValueError("Rollback plan 摘要不一致。")
        return self


class EvolutionPromotionPackageInput(_StrictModel):
    """Frozen review input; never an approval or promotion authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-promotion-package-input-v1"] = (
        EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY
    )
    input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    input_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    risk_level: Literal["low", "medium", "high", "critical"]
    reflection_id: str = Field(pattern=r"^evreflection_[0-9a-f]{24}$")
    reflection_sha256: str = Field(pattern=_SHA256_RE)
    decision_state_id: str = Field(pattern=r"^evdecision_[0-9a-f]{24}$")
    decision_state_sha256: str = Field(pattern=_SHA256_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_receipt_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    final_evaluation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    evaluation_lane_count: int = Field(ge=2, le=4)
    patch: EvolutionPromotionPatchManifest
    baseline: EvolutionPromotionBaseline
    migration: EvolutionPromotionMigrationAssessment
    rollback: EvolutionPromotionRollbackPlan
    evidence_refs: tuple[EvolutionPromotionEvidenceRef, ...] = Field(
        min_length=22,
        max_length=32,
    )
    source_reflection_active_at_issue: Literal[True] = True
    stale_on_reflection_revocation: Literal[True] = True
    package_input_complete: Literal[True] = True
    promotion_package_ready: Literal[False] = False
    approval_decided: Literal[False] = False
    promotion_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    contains_source_code: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    contains_user_custom_text: Literal[False] = False
    llm_generated: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _input_is_exact_and_tamper_evident(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Promotion Input workspace 必须是 canonical 绝对路径。")
        if any(char in self.workspace_root for char in ("\x00", "\r", "\n")):
            raise ValueError("Promotion Input workspace 包含不安全字符。")
        _aware(self.created_at)
        if not (
            self.patch.mutation_receipt_id == self.mutation_receipt_id
            and self.patch.mutation_receipt_sha256 == self.mutation_receipt_sha256
            and self.rollback.baseline_commit == self.baseline.baseline_commit
            and self.rollback.baseline_tree_sha256 == self.baseline.baseline_tree_sha256
            and self.rollback.source_snapshot_id == self.baseline.source_snapshot_id
            and self.rollback.source_snapshot_sha256
            == self.baseline.source_snapshot_sha256
            and self.rollback.data_restore_required
            is self.migration.data_backup_required
        ):
            raise ValueError("Promotion Input patch/baseline/rollback 投影不一致。")
        _validate_evidence_refs(self)
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"input_id", "input_sha256"})
        )
        if not hmac.compare_digest(self.input_sha256, digest):
            raise ValueError("Promotion Package Input 摘要不一致。")
        if self.input_id != f"evpromoin_{digest[:24]}":
            raise ValueError("Promotion Package Input identity 不一致。")
        return self


class EvolutionPromotionPackageInputView(_StrictModel):
    package_input: EvolutionPromotionPackageInput
    reflection_active: bool
    promotion_review_eligible: bool

    @model_validator(mode="after")
    def _eligibility_is_exact(self) -> Self:
        if self.promotion_review_eligible is not self.reflection_active:
            raise ValueError("Promotion Input eligibility 与 Reflection 状态不一致。")
        return self


class EvolutionPromotionPackageInputError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPromotionPackageInputBuilder:
    def build(
        self,
        *,
        reflection: EvolutionReflectionMemory,
        decision: EvolutionDecisionState,
    ) -> EvolutionPromotionPackageInput:
        try:
            memory = EvolutionReflectionMemory.model_validate_json(
                reflection.model_dump_json()
            )
            state = EvolutionDecisionState.model_validate(decision.model_dump(mode="json"))
            _validate_authority(memory, state)
            decision_input = state.gate.decision_input
            mutation = decision_input.mutation
            contract = decision_input.experiment.contract
            final = decision_input.final_evaluation
            request = final.aggregation_contract.batch_request
            patch = _patch_manifest(mutation)
            baseline = _baseline(contract.baseline.workspace_dirty_at_issue, request)
            migration = _migration_assessment(patch.files)
            rollback = _rollback_plan(patch.files, baseline, migration)
            refs = _evidence_refs(memory, state)
        except EvolutionPromotionPackageInputError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_authority_invalid",
                "Promotion Package Input authority 无效、不完整或已被篡改。",
            ) from exc
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY,
            "workspace_root": state.workspace_root,
            "candidate_id": state.candidate_id,
            "candidate_revision": state.candidate_revision,
            "candidate_sha256": decision_input.candidate_sha256,
            "risk_level": state.risk_level,
            "reflection_id": memory.reflection_id,
            "reflection_sha256": memory.reflection_sha256,
            "decision_state_id": state.decision_id,
            "decision_state_sha256": state.decision_sha256,
            "experiment_contract_id": decision_input.experiment_contract_id,
            "experiment_contract_sha256": decision_input.experiment_contract_sha256,
            "mutation_receipt_id": decision_input.mutation_receipt_id,
            "mutation_receipt_sha256": decision_input.mutation_receipt_sha256,
            "final_evaluation_receipt_id": decision_input.final_evaluation_receipt_id,
            "final_evaluation_receipt_sha256": (
                decision_input.final_evaluation_receipt_sha256
            ),
            "required_platforms": list(final.required_platforms),
            "evaluation_lane_count": final.lane_count,
            "patch": patch.model_dump(mode="json"),
            "baseline": baseline.model_dump(mode="json"),
            "migration": migration.model_dump(mode="json"),
            "rollback": rollback.model_dump(mode="json"),
            "evidence_refs": [item.model_dump(mode="json") for item in refs],
            "source_reflection_active_at_issue": True,
            "stale_on_reflection_revocation": True,
            "package_input_complete": True,
            "promotion_package_ready": False,
            "approval_decided": False,
            "promotion_executed": False,
            "git_write_executed": False,
            "merge_executed": False,
            "push_executed": False,
            "publish_executed": False,
            "contains_source_code": False,
            "contains_freeform_narrative": False,
            "contains_user_custom_text": False,
            "llm_generated": False,
            "created_at": memory.created_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionPromotionPackageInput.model_validate(
                {
                    **payload,
                    "input_id": f"evpromoin_{digest[:24]}",
                    "input_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_artifact_invalid",
                "Promotion Package Input artifact 无法验证。",
            ) from exc


class EvolutionPromotionPackageInputStore:
    """Immutable package inputs with atomic Reflection-active admission."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        artifact: EvolutionPromotionPackageInput,
        *,
        reflection: EvolutionReflectionMemory,
    ) -> EvolutionPromotionPackageInputView:
        try:
            item = EvolutionPromotionPackageInput.model_validate_json(
                artifact.model_dump_json()
            )
            memory = EvolutionReflectionMemory.model_validate_json(
                reflection.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_artifact_invalid",
                "Promotion Package Input 或 Reflection 无效。",
            ) from exc
        if not (
            item.reflection_id == memory.reflection_id
            and item.reflection_sha256 == memory.reflection_sha256
            and item.workspace_root == memory.workspace_root
        ):
            raise EvolutionPromotionPackageInputError(
                "promotion_input_reflection_mismatch",
                "Promotion Package Input 未绑定 exact Reflection。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_oversized",
                "Promotion Package Input 超过 2 MiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_active_reflection(db, memory)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_promotion_package_inputs "
                        "WHERE reflection_id = ?",
                        (item.reflection_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionPromotionPackageInputError(
                            "promotion_input_conflict",
                            "同一 Reflection 不可覆盖为不同 Promotion Input。",
                        )
                    await db.rollback()
                    return EvolutionPromotionPackageInputView(
                        package_input=existing,
                        reflection_active=True,
                        promotion_review_eligible=True,
                    )
                await db.execute(
                    "INSERT INTO evolution_promotion_package_inputs "
                    "(input_id, input_sha256, reflection_id, reflection_sha256, "
                    "decision_state_id, workspace_root, candidate_id, candidate_revision, "
                    "input_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.input_id,
                        item.input_sha256,
                        item.reflection_id,
                        item.reflection_sha256,
                        item.decision_state_id,
                        item.workspace_root,
                        item.candidate_id,
                        item.candidate_revision,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionPromotionPackageInputError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_store_error",
                "Promotion Package Input 无法持久化。",
            ) from exc
        restored = await self.get(item.input_id)
        assert restored is not None and restored.promotion_review_eligible
        return restored

    async def get(self, input_id: str) -> EvolutionPromotionPackageInputView | None:
        if (
            not isinstance(input_id, str)
            or re.fullmatch(r"evpromoin_[0-9a-f]{24}", input_id) is None
        ):
            raise ValueError("promotion input id 格式无效。")
        return await self._read("input_id", input_id)

    async def get_by_reflection(
        self,
        reflection_id: str,
    ) -> EvolutionPromotionPackageInputView | None:
        if (
            not isinstance(reflection_id, str)
            or re.fullmatch(r"evreflection_[0-9a-f]{24}", reflection_id) is None
        ):
            raise ValueError("reflection id 格式无效。")
        return await self._read("reflection_id", reflection_id)

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionPromotionPackageInputView | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_promotion_package_inputs WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                if row is None:
                    return None
                item = _from_row(row)
                active = await _reflection_active_for_input(db, item)
                return EvolutionPromotionPackageInputView(
                    package_input=item,
                    reflection_active=active,
                    promotion_review_eligible=active,
                )
        except EvolutionPromotionPackageInputError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_store_corrupt",
                "Promotion Package Input 损坏或无法读取。",
            ) from exc


class EvolutionPromotionPackageInputExecutor:
    def __init__(
        self,
        *,
        reflection_store: EvolutionReflectionMemoryStore,
        decision_store: EvolutionDecisionStateStore,
        package_store: EvolutionPromotionPackageInputStore,
        builder: EvolutionPromotionPackageInputBuilder | None = None,
    ) -> None:
        self._reflection_store = reflection_store
        self._decision_store = decision_store
        self._package_store = package_store
        self._builder = builder or EvolutionPromotionPackageInputBuilder()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        reflection_id: str,
    ) -> EvolutionPromotionPackageInputView:
        workspace = _workspace(workspace_root)
        if (
            not isinstance(reflection_id, str)
            or re.fullmatch(r"evreflection_[0-9a-f]{24}", reflection_id) is None
        ):
            raise EvolutionPromotionPackageInputError(
                "promotion_input_reflection_id_invalid",
                "Reflection Memory ID 格式无效。",
            )
        try:
            view = await self._reflection_store.get(reflection_id)
        except (EvolutionReflectionMemoryError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_reflection_read_failed",
                "无法读取 Promotion Input Reflection authority。",
            ) from exc
        if view is None:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_reflection_missing",
                "Reflection Memory 不存在。",
            )
        if view.memory.workspace_root != str(workspace):
            raise EvolutionPromotionPackageInputError(
                "promotion_input_workspace_mismatch",
                "Reflection Memory 不属于当前工作区。",
            )
        if not view.active:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_reflection_revoked",
                "Reflection Memory 已撤销，不能进入 promotion review。",
            )
        if not _reflection_is_eligible(view):
            raise EvolutionPromotionPackageInputError(
                "promotion_input_not_eligible",
                "只有 accepted_experiment 的 active Reflection 可创建 Promotion Input。",
            )
        try:
            decision = await self._decision_store.get(view.memory.decision_state_id)
        except (EvolutionDecisionStateError, OSError, TypeError, ValueError) as exc:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_decision_read_failed",
                "无法读取 Promotion Input Decision authority。",
            ) from exc
        if decision is None:
            raise EvolutionPromotionPackageInputError(
                "promotion_input_decision_missing",
                "Reflection 对应的 Decision State 不存在。",
            )
        artifact = self._builder.build(reflection=view.memory, decision=decision)
        return await self._package_store.record(artifact, reflection=view.memory)


def render_evolution_promotion_package_input(
    view: EvolutionPromotionPackageInputView,
) -> str:
    item = EvolutionPromotionPackageInputView.model_validate(
        view.model_dump(mode="python")
    )
    package = item.package_input
    lines = [
        f"# Evolution Promotion Package Input `{package.input_id}`",
        "",
        "**已冻结 promotion review 输入；尚未审批、合并、推送或发布。**",
        "",
        f"- Reflection：`{package.reflection_id}` · "
        f"{'active' if item.reflection_active else 'revoked'}",
        f"- Review eligible：{'是' if item.promotion_review_eligible else '否'}",
        f"- Candidate：`{package.candidate_id}` · revision {package.candidate_revision}",
        f"- Risk：`{package.risk_level}`",
        f"- Patch：{len(package.patch.files)} files · "
        f"+{package.patch.total_added_lines}/-{package.patch.total_deleted_lines}",
        f"- Evaluation lanes：{package.evaluation_lane_count}",
        "- Required platforms："
        + ", ".join(f"`{value}`" for value in package.required_platforms),
        "- Migration review："
        + ("需要" if package.migration.review_required else "未检测到迁移信号"),
        f"- Rollback steps：{len(package.rollback.steps)}",
        "- Promotion package ready：`false`",
        "- Approval decided：`false`",
        "- Git/Merge/Push/Publish：`false`",
        f"- Input SHA-256：`{package.input_sha256}`",
        "",
        "下一步：EVO-05.1b 只能从仍 active 的本 Input 创建完整 Promotion Package；"
        "随后还需 approval、rebase/revalidate 和 staged rollout。",
    ]
    return "\n".join(lines)


def _validate_authority(
    memory: EvolutionReflectionMemory,
    decision: EvolutionDecisionState,
) -> None:
    if not (
        memory.workspace_root == decision.workspace_root
        and memory.decision_input_id == decision.decision_input_id
        and memory.decision_state_id == decision.decision_id
        and memory.decision_state_sha256 == decision.decision_sha256
        and memory.candidate_id == decision.candidate_id
        and memory.candidate_revision == decision.candidate_revision
        and memory.risk_level == decision.risk_level
        and memory.decision_state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
        and decision.state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
        and memory.resolution_id is None
        and memory.resolution_sha256 is None
        and memory.resolution_outcome is None
        and memory.lesson_kind is EvolutionReflectionLessonKind.VALIDATED_EXPERIMENT
        and memory.required_action is EvolutionReflectionAction.REVIEW_FOR_PROMOTION
        and memory.candidate_acceptance_decided
        and memory.candidate_accepted
        and memory.promotion_review_ready
        and not memory.promotion_executed
        and not memory.promotion_authority
        and decision.candidate_acceptance_decided
        and decision.candidate_accepted
        and decision.experiment_accepted
        and decision.promotion_review_ready
        and not decision.promotion_executed
    ):
        raise EvolutionPromotionPackageInputError(
            "promotion_input_not_eligible",
            "Decision/Reflection 尚未满足 accepted_experiment promotion review 条件。",
        )


def _reflection_is_eligible(view: EvolutionReflectionMemoryView) -> bool:
    memory = view.memory
    return bool(
        view.active
        and memory.decision_state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
        and memory.lesson_kind is EvolutionReflectionLessonKind.VALIDATED_EXPERIMENT
        and memory.required_action is EvolutionReflectionAction.REVIEW_FOR_PROMOTION
        and memory.candidate_accepted
        and memory.promotion_review_ready
        and not memory.promotion_executed
        and not memory.promotion_authority
    )


def _patch_manifest(mutation) -> EvolutionPromotionPatchManifest:
    files = tuple(
        EvolutionPromotionPatchFile(
            order=index,
            path=item.path,
            operation=item.operation,
            before_sha256=item.before_sha256,
            after_sha256=item.after_sha256,
            unified_diff_sha256=item.unified_diff_sha256,
            added_lines=item.added_lines,
            deleted_lines=item.deleted_lines,
            api_change=item.api_change,
            mutation_fact_sha256=item.fact_sha256,
        )
        for index, item in enumerate(mutation.files, start=1)
    )
    payload = {
        "policy_version": EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY,
        "mutation_receipt_id": mutation.mutation_receipt_id,
        "mutation_receipt_sha256": mutation.receipt_sha256,
        "files_sha256": mutation.files_sha256,
        "files": [item.model_dump(mode="json") for item in files],
        "total_added_lines": mutation.total_added_lines,
        "total_deleted_lines": mutation.total_deleted_lines,
    }
    return EvolutionPromotionPatchManifest.model_validate(
        {**payload, "manifest_sha256": _sha256_payload(payload)}
    )


def _baseline(workspace_dirty_at_issue: bool, request) -> EvolutionPromotionBaseline:
    payload = {
        "baseline_commit": request.baseline_commit,
        "workspace_dirty_at_issue": workspace_dirty_at_issue,
        "source_snapshot_id": request.source_snapshot_id,
        "source_snapshot_sha256": request.source_snapshot_sha256,
        "baseline_tree_sha256": request.baseline_tree_sha256,
        "profile_sha256": request.profile_sha256,
        "experiment_config_sha256": request.experiment_config_sha256,
        "toolset_sha256": request.toolset_sha256,
        "rebase_validation_required": True,
        "baseline_update_executed": False,
    }
    return EvolutionPromotionBaseline.model_validate(
        {**payload, "baseline_sha256": _sha256_payload(payload)}
    )


def _migration_assessment(
    files: tuple[EvolutionPromotionPatchFile, ...],
) -> EvolutionPromotionMigrationAssessment:
    signals: set[EvolutionPromotionMigrationSignal] = set()
    evidence_paths: set[str] = set()
    dependency_names = {
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "cargo.toml",
        "cargo.lock",
    }
    for item in files:
        folded = item.path.casefold()
        parts = tuple(part for part in folded.split("/") if part)
        local_signals: set[EvolutionPromotionMigrationSignal] = set()
        if folded.endswith(".sql") or "migrations" in parts:
            local_signals.add(
                EvolutionPromotionMigrationSignal.DATABASE_MIGRATION_PATH
            )
        if any(part in {"schema", "schemas", "persistence", "state"} for part in parts):
            local_signals.add(EvolutionPromotionMigrationSignal.STATE_SCHEMA_PATH)
        if parts and parts[-1] in dependency_names:
            local_signals.add(EvolutionPromotionMigrationSignal.DEPENDENCY_MANIFEST)
        if item.api_change == "additive":
            local_signals.add(EvolutionPromotionMigrationSignal.ADDITIVE_API_CHANGE)
        if local_signals:
            signals.update(local_signals)
            evidence_paths.add(item.path)
    ordered_signals = tuple(sorted(signals, key=lambda item: item.value))
    ordered_paths = tuple(sorted(evidence_paths))
    backup = bool(
        signals
        & {
            EvolutionPromotionMigrationSignal.DATABASE_MIGRATION_PATH,
            EvolutionPromotionMigrationSignal.STATE_SCHEMA_PATH,
        }
    )
    payload = {
        "policy_version": EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY,
        "signals": [item.value for item in ordered_signals],
        "evidence_paths": list(ordered_paths),
        "review_required": bool(ordered_signals),
        "data_backup_required": backup,
        "irreversible_change_blocked": True,
        "migration_execution_allowed": False,
    }
    return EvolutionPromotionMigrationAssessment.model_validate(
        {**payload, "assessment_sha256": _sha256_payload(payload)}
    )


def _rollback_plan(
    files: tuple[EvolutionPromotionPatchFile, ...],
    baseline: EvolutionPromotionBaseline,
    migration: EvolutionPromotionMigrationAssessment,
) -> EvolutionPromotionRollbackPlan:
    steps = tuple(
        EvolutionPromotionRollbackStep(
            order=index,
            path=item.path,
            operation=(
                EvolutionPromotionRollbackOperation.RESTORE_BASELINE_BLOB
                if item.operation == "modify"
                else EvolutionPromotionRollbackOperation.REMOVE_CREATED_FILE
            ),
            baseline_sha256=item.before_sha256,
            candidate_sha256=item.after_sha256,
        )
        for index, item in enumerate(files, start=1)
    )
    payload = {
        "policy_version": EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
        "baseline_commit": baseline.baseline_commit,
        "baseline_tree_sha256": baseline.baseline_tree_sha256,
        "source_snapshot_id": baseline.source_snapshot_id,
        "source_snapshot_sha256": baseline.source_snapshot_sha256,
        "steps": [item.model_dump(mode="json") for item in steps],
        "data_restore_required": migration.data_backup_required,
        "rollback_review_required": True,
        "rollback_authority": False,
        "rollback_executed": False,
    }
    return EvolutionPromotionRollbackPlan.model_validate(
        {**payload, "plan_sha256": _sha256_payload(payload)}
    )


def _evidence_refs(
    memory: EvolutionReflectionMemory,
    decision: EvolutionDecisionState,
) -> tuple[EvolutionPromotionEvidenceRef, ...]:
    decision_input = decision.gate.decision_input
    final = decision_input.final_evaluation
    request = final.aggregation_contract.batch_request
    raw: list[tuple[EvolutionPromotionEvidenceKind, str, str]] = [
        (
            EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT,
            decision_input.experiment_contract_id,
            decision_input.experiment_contract_sha256,
        ),
        (
            EvolutionPromotionEvidenceKind.SOURCE_SNAPSHOT,
            request.source_snapshot_id,
            request.source_snapshot_sha256,
        ),
        (
            EvolutionPromotionEvidenceKind.MUTATION_RECEIPT,
            decision_input.mutation_receipt_id,
            decision_input.mutation_receipt_sha256,
        ),
        (
            EvolutionPromotionEvidenceKind.VALIDATION_PLAN,
            request.validation_plan_id,
            request.validation_plan_sha256,
        ),
        (
            EvolutionPromotionEvidenceKind.EVALUATION_AGGREGATION,
            final.aggregation_contract_id,
            final.aggregation_contract_sha256,
        ),
        (
            EvolutionPromotionEvidenceKind.FINAL_EVALUATION,
            final.receipt_id,
            final.receipt_sha256,
        ),
    ]
    lanes = (final.interventional_lane,) + tuple(
        item.lane_receipt for item in final.adversarial_lanes
    )
    for lane in lanes:
        raw.extend(
            (
                (
                    EvolutionPromotionEvidenceKind.EVALUATION_LANE,
                    lane.receipt_id,
                    lane.receipt_sha256,
                ),
                (
                    EvolutionPromotionEvidenceKind.RED_COMPLETION,
                    lane.red_completion_id,
                    lane.red_completion_sha256,
                ),
                (
                    EvolutionPromotionEvidenceKind.GREEN_COMPLETION,
                    lane.green_completion_id,
                    lane.green_completion_sha256,
                ),
                (
                    EvolutionPromotionEvidenceKind.COMPARISON_RECEIPT,
                    lane.comparison_id,
                    lane.comparison_receipt_sha256,
                ),
                (
                    EvolutionPromotionEvidenceKind.FAILURE_ATTRIBUTION,
                    lane.attribution_id,
                    lane.attribution_sha256,
                ),
            )
        )
    assert decision.counterfactual is not None and decision.reward_hacking is not None
    raw.extend(
        (
            (
                EvolutionPromotionEvidenceKind.MECHANICAL_GATE,
                decision.mechanical_gate_id,
                decision.mechanical_gate_sha256,
            ),
            (
                EvolutionPromotionEvidenceKind.INDEPENDENT_REVIEW,
                decision.independent_review_id,
                decision.independent_review_sha256,
            ),
            (
                EvolutionPromotionEvidenceKind.COUNTERFACTUAL,
                decision.counterfactual.evidence_id,
                decision.counterfactual.evidence_sha256,
            ),
            (
                EvolutionPromotionEvidenceKind.REWARD_HACKING,
                decision.reward_hacking.evidence_id,
                decision.reward_hacking.evidence_sha256,
            ),
            (
                EvolutionPromotionEvidenceKind.DECISION_STATE,
                decision.decision_id,
                decision.decision_sha256,
            ),
            (
                EvolutionPromotionEvidenceKind.REFLECTION_MEMORY,
                memory.reflection_id,
                memory.reflection_sha256,
            ),
        )
    )
    return tuple(
        EvolutionPromotionEvidenceRef(
            order=index,
            kind=kind,
            authority_id=authority_id,
            authority_sha256=digest,
        )
        for index, (kind, authority_id, digest) in enumerate(raw, start=1)
    )


def _validate_evidence_refs(item: EvolutionPromotionPackageInput) -> None:
    refs = item.evidence_refs
    if tuple(ref.order for ref in refs) != tuple(range(1, len(refs) + 1)):
        raise ValueError("Promotion evidence refs 顺序不连续。")
    identities = tuple((ref.kind, ref.authority_id) for ref in refs)
    if len(identities) != len(set(identities)):
        raise ValueError("Promotion evidence refs 不得重复。")
    counts = Counter(ref.kind for ref in refs)
    singleton = {
        EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT,
        EvolutionPromotionEvidenceKind.SOURCE_SNAPSHOT,
        EvolutionPromotionEvidenceKind.MUTATION_RECEIPT,
        EvolutionPromotionEvidenceKind.VALIDATION_PLAN,
        EvolutionPromotionEvidenceKind.EVALUATION_AGGREGATION,
        EvolutionPromotionEvidenceKind.FINAL_EVALUATION,
        EvolutionPromotionEvidenceKind.MECHANICAL_GATE,
        EvolutionPromotionEvidenceKind.INDEPENDENT_REVIEW,
        EvolutionPromotionEvidenceKind.COUNTERFACTUAL,
        EvolutionPromotionEvidenceKind.REWARD_HACKING,
        EvolutionPromotionEvidenceKind.DECISION_STATE,
        EvolutionPromotionEvidenceKind.REFLECTION_MEMORY,
    }
    lane_kinds = {
        EvolutionPromotionEvidenceKind.EVALUATION_LANE,
        EvolutionPromotionEvidenceKind.RED_COMPLETION,
        EvolutionPromotionEvidenceKind.GREEN_COMPLETION,
        EvolutionPromotionEvidenceKind.COMPARISON_RECEIPT,
        EvolutionPromotionEvidenceKind.FAILURE_ATTRIBUTION,
    }
    if any(counts[kind] != 1 for kind in singleton) or any(
        counts[kind] != item.evaluation_lane_count for kind in lane_kinds
    ):
        raise ValueError("Promotion evidence refs 覆盖不完整。")
    expected_kinds = [
        EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT,
        EvolutionPromotionEvidenceKind.SOURCE_SNAPSHOT,
        EvolutionPromotionEvidenceKind.MUTATION_RECEIPT,
        EvolutionPromotionEvidenceKind.VALIDATION_PLAN,
        EvolutionPromotionEvidenceKind.EVALUATION_AGGREGATION,
        EvolutionPromotionEvidenceKind.FINAL_EVALUATION,
    ]
    for _ in range(item.evaluation_lane_count):
        expected_kinds.extend(
            (
                EvolutionPromotionEvidenceKind.EVALUATION_LANE,
                EvolutionPromotionEvidenceKind.RED_COMPLETION,
                EvolutionPromotionEvidenceKind.GREEN_COMPLETION,
                EvolutionPromotionEvidenceKind.COMPARISON_RECEIPT,
                EvolutionPromotionEvidenceKind.FAILURE_ATTRIBUTION,
            )
        )
    expected_kinds.extend(
        (
            EvolutionPromotionEvidenceKind.MECHANICAL_GATE,
            EvolutionPromotionEvidenceKind.INDEPENDENT_REVIEW,
            EvolutionPromotionEvidenceKind.COUNTERFACTUAL,
            EvolutionPromotionEvidenceKind.REWARD_HACKING,
            EvolutionPromotionEvidenceKind.DECISION_STATE,
            EvolutionPromotionEvidenceKind.REFLECTION_MEMORY,
        )
    )
    if tuple(ref.kind for ref in refs) != tuple(expected_kinds):
        raise ValueError("Promotion evidence refs 类型顺序不一致。")
    major = {
        ref.kind: ref
        for ref in refs
        if ref.kind
        in {
            EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT,
            EvolutionPromotionEvidenceKind.MUTATION_RECEIPT,
            EvolutionPromotionEvidenceKind.FINAL_EVALUATION,
            EvolutionPromotionEvidenceKind.DECISION_STATE,
            EvolutionPromotionEvidenceKind.REFLECTION_MEMORY,
        }
    }
    expected_major = {
        EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT: (
            item.experiment_contract_id,
            item.experiment_contract_sha256,
        ),
        EvolutionPromotionEvidenceKind.MUTATION_RECEIPT: (
            item.mutation_receipt_id,
            item.mutation_receipt_sha256,
        ),
        EvolutionPromotionEvidenceKind.FINAL_EVALUATION: (
            item.final_evaluation_receipt_id,
            item.final_evaluation_receipt_sha256,
        ),
        EvolutionPromotionEvidenceKind.DECISION_STATE: (
            item.decision_state_id,
            item.decision_state_sha256,
        ),
        EvolutionPromotionEvidenceKind.REFLECTION_MEMORY: (
            item.reflection_id,
            item.reflection_sha256,
        ),
    }
    if any(
        (major[kind].authority_id, major[kind].authority_sha256) != expected
        for kind, expected in expected_major.items()
    ):
        raise ValueError("Promotion major evidence ref 与顶层 authority 不一致。")


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_promotion_package_inputs (
            input_id TEXT PRIMARY KEY,
            input_sha256 TEXT NOT NULL,
            reflection_id TEXT NOT NULL UNIQUE,
            reflection_sha256 TEXT NOT NULL,
            decision_state_id TEXT NOT NULL UNIQUE,
            workspace_root TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            candidate_revision INTEGER NOT NULL,
            input_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )


async def _require_active_reflection(
    db: aiosqlite.Connection,
    memory: EvolutionReflectionMemory,
) -> None:
    row = await (
        await db.execute(
            "SELECT * FROM evolution_reflection_memories WHERE reflection_id = ?",
            (memory.reflection_id,),
        )
    ).fetchone()
    if row is None:
        raise EvolutionPromotionPackageInputError(
            "promotion_input_reflection_missing",
            "Reflection authority 不存在于 Promotion Store。",
        )
    try:
        stored = EvolutionReflectionMemory.model_validate_json(str(row["memory_json"]))
    except (TypeError, ValueError) as exc:
        raise EvolutionPromotionPackageInputError(
            "promotion_input_reflection_corrupt",
            "Reflection authority 损坏。",
        ) from exc
    if not (
        stored == memory
        and row["reflection_id"] == memory.reflection_id
        and row["reflection_sha256"] == memory.reflection_sha256
        and row["decision_state_id"] == memory.decision_state_id
        and row["decision_input_id"] == memory.decision_input_id
        and row["workspace_root"] == memory.workspace_root
        and row["lesson_kind"] == memory.lesson_kind.value
        and row["created_at"] == memory.created_at
        and _reflection_is_eligible(
            EvolutionReflectionMemoryView(
                memory=memory,
                revocation=None,
                active=True,
            )
        )
    ):
        raise EvolutionPromotionPackageInputError(
            "promotion_input_reflection_mismatch",
            "Promotion Store 与 Reflection authority 不一致。",
        )
    revoked = await (
        await db.execute(
            "SELECT 1 FROM evolution_reflection_revocations WHERE reflection_id = ?",
            (memory.reflection_id,),
        )
    ).fetchone()
    if revoked is not None:
        raise EvolutionPromotionPackageInputError(
            "promotion_input_reflection_revoked",
            "Reflection 已撤销，不能写入 Promotion Input。",
        )


async def _reflection_active_for_input(
    db: aiosqlite.Connection,
    item: EvolutionPromotionPackageInput,
) -> bool:
    row = await (
        await db.execute(
            "SELECT * FROM evolution_reflection_memories WHERE reflection_id = ?",
            (item.reflection_id,),
        )
    ).fetchone()
    if row is None:
        raise ValueError("Promotion Input 对应 Reflection authority 缺失。")
    memory = EvolutionReflectionMemory.model_validate_json(str(row["memory_json"]))
    if not (
        memory.reflection_id == item.reflection_id == row["reflection_id"]
        and memory.reflection_sha256 == item.reflection_sha256 == row["reflection_sha256"]
        and memory.decision_state_id == item.decision_state_id == row["decision_state_id"]
        and memory.workspace_root == item.workspace_root == row["workspace_root"]
        and row["decision_input_id"] == memory.decision_input_id
        and row["lesson_kind"] == memory.lesson_kind.value
        and row["created_at"] == memory.created_at
        and _reflection_is_eligible(
            EvolutionReflectionMemoryView(
                memory=memory,
                revocation=None,
                active=True,
            )
        )
    ):
        raise ValueError("Promotion Input 对应 Reflection authority 不一致。")
    revoked = await (
        await db.execute(
            "SELECT 1 FROM evolution_reflection_revocations WHERE reflection_id = ?",
            (item.reflection_id,),
        )
    ).fetchone()
    return revoked is None


def _from_row(row: aiosqlite.Row) -> EvolutionPromotionPackageInput:
    encoded = str(row["input_json"])
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Promotion Input Store artifact 过大。")
    item = EvolutionPromotionPackageInput.model_validate_json(encoded)
    if not (
        row["input_id"] == item.input_id
        and row["input_sha256"] == item.input_sha256
        and row["reflection_id"] == item.reflection_id
        and row["reflection_sha256"] == item.reflection_sha256
        and row["decision_state_id"] == item.decision_state_id
        and row["workspace_root"] == item.workspace_root
        and row["candidate_id"] == item.candidate_id
        and row["candidate_revision"] == item.candidate_revision
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Promotion Input Store index 不一致。")
    return item


def _workspace(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvolutionPromotionPackageInputError(
            "promotion_input_workspace_missing",
            "Promotion Input 工作区不存在。",
        ) from exc
    if not path.is_dir():
        raise EvolutionPromotionPackageInputError(
            "promotion_input_workspace_missing",
            "Promotion Input 工作区不存在。",
        )
    return path


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Promotion Input timestamp 必须包含时区。")
    return parsed


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "EVOLUTION_PROMOTION_MIGRATION_ASSESSMENT_POLICY",
    "EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY",
    "EVOLUTION_PROMOTION_PATCH_MANIFEST_POLICY",
    "EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY",
    "EvolutionPromotionBaseline",
    "EvolutionPromotionEvidenceKind",
    "EvolutionPromotionEvidenceRef",
    "EvolutionPromotionMigrationAssessment",
    "EvolutionPromotionMigrationSignal",
    "EvolutionPromotionPackageInput",
    "EvolutionPromotionPackageInputBuilder",
    "EvolutionPromotionPackageInputError",
    "EvolutionPromotionPackageInputExecutor",
    "EvolutionPromotionPackageInputStore",
    "EvolutionPromotionPackageInputView",
    "EvolutionPromotionPatchFile",
    "EvolutionPromotionPatchManifest",
    "EvolutionPromotionRollbackOperation",
    "EvolutionPromotionRollbackPlan",
    "EvolutionPromotionRollbackStep",
    "render_evolution_promotion_package_input",
]
