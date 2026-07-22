"""Deterministic counterfactual evidence over an immutable Evolution review chain."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.decision_inputs import (
    EvolutionDecisionInput,
    EvolutionDecisionInputError,
    EvolutionDecisionInputStore,
)
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractAuthority,
    EvolutionExperimentContractStore,
    EvolutionExperimentContractStoreError,
)
from naumi_agent.evolution.independent_reviews import (
    EvolutionIndependentReview,
    EvolutionIndependentReviewError,
    EvolutionIndependentReviewStore,
    IndependentReviewStatus,
)
from naumi_agent.evolution.mechanical_gates import (
    EvolutionMechanicalGate,
    EvolutionMechanicalGateError,
    EvolutionMechanicalGateStore,
)
from naumi_agent.evolution.mutation_receipts import (
    EvolutionMutationReceipt,
    EvolutionMutationReceiptError,
    EvolutionMutationReceiptStore,
    MutationReceiptFile,
)

COUNTERFACTUAL_EVIDENCE_POLICY = "evolution-counterfactual-evidence-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 16 * 1_024 * 1_024
_MAX_SOURCE_BYTES = 2 * 1_024 * 1_024
_MAX_GIT_OUTPUT = 2 * 1_024 * 1_024
_MAX_DIFF_LINES = 20_000
_MAX_REPRESENTATIVE_FINDINGS_PER_CODE = 16


class CounterfactualFindingCode(StrEnum):
    NON_SEMANTIC_SCOPE = "non_semantic_scope"
    TEST_DELETION = "test_deletion"
    METRIC_MUTATION = "metric_mutation"
    THRESHOLD_RELAXATION = "threshold_relaxation"
    SKIP_ADDED = "skip_added"
    MOCK_ADDED = "mock_added"
    EVALUATION_LEAKAGE = "evaluation_leakage"


class CounterfactualSeverity(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"


class CounterfactualRule(StrEnum):
    AUTHORITY_CHAIN_BOUND = "authority_chain_bound"
    WORKTREE_BINDING_EXACT = "worktree_binding_exact"
    CANDIDATE_BYTES_MATCH_RECEIPT = "candidate_bytes_match_receipt"
    UNIFIED_DIFF_MATCH_RECEIPT = "unified_diff_match_receipt"
    NO_REDUCIBLE_SCOPE = "no_reducible_scope"
    NO_TEST_DELETION = "no_test_deletion"
    NO_METRIC_MUTATION = "no_metric_mutation"
    NO_THRESHOLD_RELAXATION = "no_threshold_relaxation"
    NO_SKIP_ADDED = "no_skip_added"
    NO_MOCK_ADDED = "no_mock_added"
    NO_EVALUATION_LEAKAGE = "no_evaluation_leakage"


class CounterfactualRequiredAction(StrEnum):
    CONTINUE_TO_REWARD_HACKING_REVIEW = "continue_to_reward_hacking_review"
    INVESTIGATE_ALTERNATIVE_EXPLANATION = "investigate_alternative_explanation"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class CounterfactualLeaseBinding(_StrictModel):
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    worktree_name: str = Field(pattern=r"^experiment-[0-9a-f]{16}$")
    branch_sha256: str = Field(pattern=_SHA256_RE)
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    observed_state: ExperimentLeaseState
    binding_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _binding_is_exact(self) -> Self:
        expected_lease = hashlib.sha256(
            f"{self.contract_id}:{self.contract_manifest_sha256}".encode()
        ).hexdigest()
        if self.lease_id != f"evl_{expected_lease[:24]}":
            raise ValueError("Counterfactual Lease identity 不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"binding_sha256"})
        )
        if not hmac.compare_digest(self.binding_sha256, digest):
            raise ValueError("Counterfactual Lease binding 摘要不一致。")
        return self


class CounterfactualFinding(_StrictModel):
    code: CounterfactualFindingCode
    severity: CounterfactualSeverity
    path: str = Field(min_length=1, max_length=1_024)
    direction: Literal["added", "deleted", "replacement", "file"]
    baseline_line: int | None = Field(default=None, ge=1, le=4_194_304)
    candidate_line: int | None = Field(default=None, ge=1, le=4_194_304)
    evidence_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def _location_matches_direction(self) -> Self:
        if self.direction == "added" and self.candidate_line is None:
            raise ValueError("Counterfactual added finding 缺少 candidate line。")
        if self.direction == "deleted" and self.baseline_line is None:
            raise ValueError("Counterfactual deleted finding 缺少 baseline line。")
        if self.direction == "replacement" and (
            self.baseline_line is None or self.candidate_line is None
        ):
            raise ValueError("Counterfactual replacement finding 缺少行号。")
        if self.direction == "file" and (
            self.baseline_line is not None or self.candidate_line is not None
        ):
            raise ValueError("Counterfactual file finding 不得包含行号。")
        return self


class CounterfactualFileEvidence(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    unified_diff_sha256: str = Field(pattern=_SHA256_RE)
    added_lines: int = Field(ge=0, le=4_194_304)
    deleted_lines: int = Field(ge=0, le=4_194_304)
    semantic_added_lines: int = Field(ge=0, le=4_194_304)
    semantic_deleted_lines: int = Field(ge=0, le=4_194_304)
    changed_line_evidence_sha256: str = Field(pattern=_SHA256_RE)
    finding_codes: tuple[CounterfactualFindingCode, ...] = Field(max_length=7)
    smaller_scope_plausible: bool
    evidence_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def _file_evidence_is_consistent(self) -> Self:
        if self.operation == "modify" and self.before_sha256 is None:
            raise ValueError("Counterfactual modify evidence 缺少 before digest。")
        if self.operation == "create" and self.before_sha256 is not None:
            raise ValueError("Counterfactual create evidence 不得包含 before digest。")
        if self.semantic_added_lines > self.added_lines or (
            self.semantic_deleted_lines > self.deleted_lines
        ):
            raise ValueError("Counterfactual semantic line count 超出 diff。")
        expected_codes = tuple(sorted(set(self.finding_codes), key=lambda item: item.value))
        if self.finding_codes != expected_codes:
            raise ValueError("Counterfactual file finding codes 必须排序且唯一。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"evidence_sha256"})
        )
        if not hmac.compare_digest(self.evidence_sha256, digest):
            raise ValueError("Counterfactual file evidence 摘要不一致。")
        return self


class CounterfactualCheck(_StrictModel):
    order: int = Field(ge=1, le=16)
    rule: CounterfactualRule
    passed: bool
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _refs_are_stable(self) -> Self:
        if self.evidence_refs != tuple(dict.fromkeys(self.evidence_refs)):
            raise ValueError("Counterfactual evidence refs 不得重复。")
        if any(
            not value
            or len(value) > 256
            or any(char in value for char in ("\x00", "\r", "\n"))
            for value in self.evidence_refs
        ):
            raise ValueError("Counterfactual evidence ref 无效。")
        return self


class EvolutionCounterfactualEvidence(_StrictModel):
    """Static alternative-explanation evidence; never an acceptance decision."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-counterfactual-evidence-v1"] = (
        COUNTERFACTUAL_EVIDENCE_POLICY
    )
    evidence_id: str = Field(pattern=r"^evcounter_[0-9a-f]{24}$")
    evidence_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    review_id: str = Field(pattern=r"^evreview_[0-9a-f]{24}$")
    review_sha256: str = Field(pattern=_SHA256_RE)
    gate_id: str = Field(pattern=r"^evgate_[0-9a-f]{24}$")
    gate_sha256: str = Field(pattern=_SHA256_RE)
    decision_input_id: str = Field(pattern=r"^evdin_[0-9a-f]{24}$")
    decision_input_sha256: str = Field(pattern=_SHA256_RE)
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    lease_binding: CounterfactualLeaseBinding
    files: tuple[CounterfactualFileEvidence, ...] = Field(min_length=1, max_length=16)
    findings: tuple[CounterfactualFinding, ...] = Field(max_length=2_048)
    checks: tuple[CounterfactualCheck, ...] = Field(min_length=11, max_length=11)
    outcome: Literal["clear", "concern"]
    required_actions: tuple[CounterfactualRequiredAction, ...] = Field(
        min_length=1,
        max_length=2,
    )
    smaller_scope_found: bool
    alternative_explanation_found: bool
    mechanical_gate_outcome_preserved: Literal[True] = True
    reviewer_advisory_only: Literal[True] = True
    llm_used: Literal[False] = False
    candidate_acceptance_decided: Literal[False] = False
    reward_hacking_review_ready: Literal[True] = True
    promotion_ready: Literal[False] = False
    review: EvolutionIndependentReview
    created_at: str = Field(min_length=1, max_length=100)

    @field_validator("workspace_root")
    @classmethod
    def _canonical_workspace(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Counterfactual workspace 必须是安全绝对路径。")
        return str(path.resolve())

    @model_validator(mode="after")
    def _artifact_is_exact_and_tamper_evident(self) -> Self:
        review = self.review
        gate = review.gate
        decision = gate.decision_input
        mutation = decision.mutation
        experiment = decision.experiment
        _require_authority_chain(review, gate, decision, mutation, experiment)
        if review.status is not IndependentReviewStatus.COMPLETED or (
            not review.counterfactual_review_ready
        ):
            raise ValueError("只有 completed Independent Review 可形成 Counterfactual。")
        if not (
            self.workspace_root == review.workspace_root
            and self.review_id == review.review_id
            and self.review_sha256 == review.review_sha256
            and self.gate_id == gate.gate_id
            and self.gate_sha256 == gate.gate_sha256
            and self.decision_input_id == decision.decision_input_id
            and self.decision_input_sha256 == decision.decision_input_sha256
            and self.mutation_receipt_id == mutation.mutation_receipt_id
            and self.mutation_receipt_sha256 == mutation.receipt_sha256
            and self.experiment_contract_id == experiment.contract_id
            and self.experiment_contract_sha256 == experiment.contract_manifest_sha256
            and self.candidate_id == decision.candidate_id
            and self.candidate_revision == decision.candidate_revision
            and self.lease_binding.contract_id == experiment.contract_id
            and self.lease_binding.contract_manifest_sha256
            == experiment.contract_manifest_sha256
            and self.lease_binding.baseline_commit == experiment.contract.baseline.commit
        ):
            raise ValueError("Counterfactual authority 投影不一致。")
        receipt_files = mutation.files
        if len(self.files) != len(receipt_files):
            raise ValueError("Counterfactual file evidence 数量不一致。")
        for evidence, receipt in zip(self.files, receipt_files, strict=True):
            if not _file_matches_receipt(evidence, receipt):
                raise ValueError("Counterfactual file evidence 与 Mutation Receipt 不一致。")
        expected_findings = tuple(
            sorted(
                self.findings,
                key=lambda item: (
                    item.path,
                    item.code.value,
                    item.direction,
                    item.baseline_line or 0,
                    item.candidate_line or 0,
                    item.evidence_sha256,
                ),
            )
        )
        if self.findings != expected_findings or len({
            item.evidence_sha256 for item in self.findings
        }) != len(self.findings):
            raise ValueError("Counterfactual findings 必须稳定排序且唯一。")
        for file_evidence in self.files:
            expected_codes = tuple(sorted(
                {item.code for item in self.findings if item.path == file_evidence.path},
                key=lambda item: item.value,
            ))
            if file_evidence.finding_codes != expected_codes:
                raise ValueError("Counterfactual file/global findings 投影不一致。")
        expected_smaller = any(item.smaller_scope_plausible for item in self.files)
        expected_alternative = bool(self.findings)
        expected_checks = _build_checks(
            review=review,
            lease_binding=self.lease_binding,
            files=self.files,
            findings=self.findings,
        )
        expected_outcome = "concern" if expected_alternative else "clear"
        expected_actions = (
            (
                CounterfactualRequiredAction.INVESTIGATE_ALTERNATIVE_EXPLANATION,
                CounterfactualRequiredAction.CONTINUE_TO_REWARD_HACKING_REVIEW,
            )
            if expected_alternative
            else (CounterfactualRequiredAction.CONTINUE_TO_REWARD_HACKING_REVIEW,)
        )
        if not (
            self.smaller_scope_found == expected_smaller
            and self.alternative_explanation_found == expected_alternative
            and self.checks == expected_checks
            and self.outcome == expected_outcome
            and self.required_actions == expected_actions
            and self.created_at == review.reviewed_at
        ):
            raise ValueError("Counterfactual outcome 或机械 checks 不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"evidence_id", "evidence_sha256"})
        )
        if not hmac.compare_digest(self.evidence_sha256, digest):
            raise ValueError("Counterfactual Evidence 摘要不一致。")
        if self.evidence_id != f"evcounter_{digest[:24]}":
            raise ValueError("Counterfactual Evidence identity 不一致。")
        return self


class EvolutionCounterfactualEvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionCounterfactualEvidenceBuilder:
    def build(
        self,
        *,
        review: EvolutionIndependentReview,
        lease_binding: CounterfactualLeaseBinding,
        files: tuple[CounterfactualFileEvidence, ...],
        findings: tuple[CounterfactualFinding, ...],
    ) -> EvolutionCounterfactualEvidence:
        try:
            review = EvolutionIndependentReview.model_validate(
                review.model_dump(mode="json")
            )
            if review.status is not IndependentReviewStatus.COMPLETED or (
                not review.counterfactual_review_ready
            ):
                raise ValueError("review not ready")
            lease_binding = CounterfactualLeaseBinding.model_validate(
                lease_binding.model_dump(mode="json")
            )
            files = tuple(
                CounterfactualFileEvidence.model_validate(item.model_dump(mode="json"))
                for item in files
            )
            findings = tuple(
                sorted(
                    (
                        CounterfactualFinding.model_validate(item.model_dump(mode="json"))
                        for item in findings
                    ),
                    key=lambda item: (
                        item.path,
                        item.code.value,
                        item.direction,
                        item.baseline_line or 0,
                        item.candidate_line or 0,
                        item.evidence_sha256,
                    ),
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_input_invalid",
                "Counterfactual 输入 authority 或扫描证据无效。",
            ) from exc
        gate = review.gate
        decision = gate.decision_input
        mutation = decision.mutation
        experiment = decision.experiment
        checks = _build_checks(
            review=review,
            lease_binding=lease_binding,
            files=files,
            findings=findings,
        )
        concern = bool(findings)
        payload = {
            "schema_version": 1,
            "policy_version": COUNTERFACTUAL_EVIDENCE_POLICY,
            "workspace_root": review.workspace_root,
            "review_id": review.review_id,
            "review_sha256": review.review_sha256,
            "gate_id": gate.gate_id,
            "gate_sha256": gate.gate_sha256,
            "decision_input_id": decision.decision_input_id,
            "decision_input_sha256": decision.decision_input_sha256,
            "mutation_receipt_id": mutation.mutation_receipt_id,
            "mutation_receipt_sha256": mutation.receipt_sha256,
            "experiment_contract_id": experiment.contract_id,
            "experiment_contract_sha256": experiment.contract_manifest_sha256,
            "candidate_id": decision.candidate_id,
            "candidate_revision": decision.candidate_revision,
            "lease_binding": lease_binding.model_dump(mode="json"),
            "files": [item.model_dump(mode="json") for item in files],
            "findings": [item.model_dump(mode="json") for item in findings],
            "checks": [item.model_dump(mode="json") for item in checks],
            "outcome": "concern" if concern else "clear",
            "required_actions": (
                [
                    CounterfactualRequiredAction.INVESTIGATE_ALTERNATIVE_EXPLANATION.value,
                    CounterfactualRequiredAction.CONTINUE_TO_REWARD_HACKING_REVIEW.value,
                ]
                if concern
                else [
                    CounterfactualRequiredAction.CONTINUE_TO_REWARD_HACKING_REVIEW.value
                ]
            ),
            "smaller_scope_found": any(item.smaller_scope_plausible for item in files),
            "alternative_explanation_found": concern,
            "mechanical_gate_outcome_preserved": True,
            "reviewer_advisory_only": True,
            "llm_used": False,
            "candidate_acceptance_decided": False,
            "reward_hacking_review_ready": True,
            "promotion_ready": False,
            "review": review.model_dump(mode="json"),
            "created_at": review.reviewed_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionCounterfactualEvidence.model_validate({
                **payload,
                "evidence_id": f"evcounter_{digest[:24]}",
                "evidence_sha256": digest,
            })
        except ValueError as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_artifact_invalid",
                "Counterfactual Evidence artifact 无法验证。",
            ) from exc


class EvolutionCounterfactualEvidenceStore:
    """Immutable one-artifact-per-Independent-Review storage."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        artifact: EvolutionCounterfactualEvidence,
    ) -> EvolutionCounterfactualEvidence:
        try:
            item = EvolutionCounterfactualEvidence.model_validate(
                artifact.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_artifact_invalid",
                "Counterfactual Evidence 无效或已被篡改。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_artifact_oversized",
                "Counterfactual Evidence 超过 16 MiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_counterfactual_evidence "
                        "WHERE review_id = ?",
                        (item.review_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionCounterfactualEvidenceError(
                            "counterfactual_artifact_conflict",
                            "同一 Independent Review 不可覆盖为不同 Counterfactual Evidence。",
                        )
                    await db.rollback()
                    return existing
                await db.execute(
                    "INSERT INTO evolution_counterfactual_evidence "
                    "(evidence_id, evidence_sha256, review_id, review_sha256, "
                    "workspace_root, outcome, evidence_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.evidence_id,
                        item.evidence_sha256,
                        item.review_id,
                        item.review_sha256,
                        item.workspace_root,
                        item.outcome,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionCounterfactualEvidenceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_store_error",
                "Counterfactual Evidence 无法持久化。",
            ) from exc
        restored = await self.get(item.evidence_id)
        assert restored is not None
        return restored

    async def get(self, evidence_id: str) -> EvolutionCounterfactualEvidence | None:
        if not isinstance(evidence_id, str) or re.fullmatch(
            r"evcounter_[0-9a-f]{24}", evidence_id
        ) is None:
            raise ValueError("evidence_id 格式无效。")
        return await self._read("evidence_id", evidence_id)

    async def get_by_review(
        self,
        review_id: str,
    ) -> EvolutionCounterfactualEvidence | None:
        if not isinstance(review_id, str) or re.fullmatch(
            r"evreview_[0-9a-f]{24}", review_id
        ) is None:
            raise ValueError("review_id 格式无效。")
        return await self._read("review_id", review_id)

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionCounterfactualEvidence | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_counterfactual_evidence "
                        f"WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_store_corrupt",
                "Counterfactual Evidence 损坏或无法读取。",
            ) from exc


class EvolutionCounterfactualEvidenceExecutor:
    def __init__(
        self,
        *,
        review_store: EvolutionIndependentReviewStore,
        gate_store: EvolutionMechanicalGateStore,
        decision_store: EvolutionDecisionInputStore,
        mutation_store: EvolutionMutationReceiptStore,
        experiment_store: EvolutionExperimentContractStore,
        lease_store: EvolutionExperimentLeaseStore,
        evidence_store: EvolutionCounterfactualEvidenceStore,
        worktree_storage_dir: str | Path,
        builder: EvolutionCounterfactualEvidenceBuilder | None = None,
    ) -> None:
        if not isinstance(review_store, EvolutionIndependentReviewStore):
            raise TypeError("Counterfactual 需要 Independent Review Store。")
        if not isinstance(gate_store, EvolutionMechanicalGateStore):
            raise TypeError("Counterfactual 需要 Mechanical Gate Store。")
        if not isinstance(decision_store, EvolutionDecisionInputStore):
            raise TypeError("Counterfactual 需要 Decision Input Store。")
        if not isinstance(mutation_store, EvolutionMutationReceiptStore):
            raise TypeError("Counterfactual 需要 Mutation Receipt Store。")
        if not isinstance(experiment_store, EvolutionExperimentContractStore):
            raise TypeError("Counterfactual 需要 Experiment Contract Store。")
        if not isinstance(lease_store, EvolutionExperimentLeaseStore):
            raise TypeError("Counterfactual 需要 Experiment Lease Store。")
        if not isinstance(evidence_store, EvolutionCounterfactualEvidenceStore):
            raise TypeError("Counterfactual 需要 Counterfactual Evidence Store。")
        if builder is not None and not isinstance(
            builder, EvolutionCounterfactualEvidenceBuilder
        ):
            raise TypeError("Counterfactual 需要 Counterfactual Evidence Builder。")
        self._review_store = review_store
        self._gate_store = gate_store
        self._decision_store = decision_store
        self._mutation_store = mutation_store
        self._experiment_store = experiment_store
        self._lease_store = lease_store
        self._evidence_store = evidence_store
        self._storage_dir = Path(worktree_storage_dir).expanduser().resolve()
        self._builder = builder or EvolutionCounterfactualEvidenceBuilder()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        review_id: str,
    ) -> EvolutionCounterfactualEvidence:
        workspace = _workspace(workspace_root)
        review = await self._load_review(review_id)
        if review.workspace_root != str(workspace):
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_workspace_mismatch",
                "Independent Review 不属于当前工作区。",
            )
        if review.status is not IndependentReviewStatus.COMPLETED or (
            not review.counterfactual_review_ready
        ):
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_review_not_ready",
                "Independent Review 未完成或被 Mechanical veto 阻断。",
            )
        gate = await self._load_gate(review.gate_id)
        decision = await self._load_decision(gate.decision_input_id)
        mutation = await self._load_mutation(decision.mutation_receipt_id)
        experiment = await self._load_experiment(workspace, decision.experiment_contract_id)
        try:
            _require_authority_chain(review, gate, decision, mutation, experiment)
        except ValueError as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_authority_mismatch",
                "Counterfactual 上游 authority 引用漂移。",
            ) from exc
        existing = await self._evidence_store.get_by_review(review.review_id)
        if existing is not None:
            _require_existing(existing, review)
            return existing
        lease = await self._load_lease(experiment.contract_id)
        try:
            lease_binding = _lease_binding(lease, experiment)
            files, findings = await asyncio.to_thread(
                _scan_worktree,
                lease,
                experiment,
                mutation,
                self._storage_dir,
            )
        except EvolutionCounterfactualEvidenceError:
            raise
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_scan_failed",
                "Counterfactual 无法读取或验证受管 worktree 证据。",
            ) from exc
        artifact = self._builder.build(
            review=review,
            lease_binding=lease_binding,
            files=files,
            findings=findings,
        )
        return await self._evidence_store.record(artifact)

    async def _load_review(self, review_id: str) -> EvolutionIndependentReview:
        try:
            item = await self._review_store.get(review_id.strip())
        except (EvolutionIndependentReviewError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_review_read_failed",
                "无法读取 Independent Review。",
            ) from exc
        if item is None:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_review_missing", "Independent Review 不存在。"
            )
        return item

    async def _load_gate(self, gate_id: str) -> EvolutionMechanicalGate:
        try:
            item = await self._gate_store.get(gate_id)
        except (EvolutionMechanicalGateError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_gate_read_failed", "无法读取 Mechanical Gate。"
            ) from exc
        if item is None:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_gate_missing", "Mechanical Gate 不存在。"
            )
        return item

    async def _load_decision(self, decision_id: str) -> EvolutionDecisionInput:
        try:
            item = await self._decision_store.get(decision_id)
        except (EvolutionDecisionInputError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_decision_read_failed", "无法读取 Decision Input。"
            ) from exc
        if item is None:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_decision_missing", "Decision Input 不存在。"
            )
        return item

    async def _load_mutation(self, receipt_id: str) -> EvolutionMutationReceipt:
        try:
            item = await asyncio.to_thread(self._mutation_store.get, receipt_id)
        except (EvolutionMutationReceiptError, OSError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_mutation_read_failed", "无法读取 Mutation Receipt。"
            ) from exc
        if item is None:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_mutation_missing", "Mutation Receipt 不存在。"
            )
        return item

    async def _load_experiment(
        self,
        workspace: Path,
        contract_id: str,
    ) -> EvolutionExperimentContractAuthority:
        try:
            item = await self._experiment_store.get(workspace, contract_id)
        except (EvolutionExperimentContractStoreError, TypeError, ValueError) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_experiment_read_failed",
                "无法读取 Experiment Contract Authority。",
            ) from exc
        if item is None:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_experiment_missing",
                "Experiment Contract Authority 不存在。",
            )
        return item

    async def _load_lease(self, contract_id: str) -> ExperimentWorktreeLease:
        try:
            item = await self._lease_store.get(contract_id)
        except (OSError, TypeError, ValueError, aiosqlite.Error) as exc:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_lease_read_failed", "无法读取 Experiment Lease。"
            ) from exc
        if item is None:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_lease_missing", "Experiment Lease 不存在。"
            )
        return item


def render_counterfactual_evidence(
    artifact: EvolutionCounterfactualEvidence,
) -> str:
    item = EvolutionCounterfactualEvidence.model_validate(
        artifact.model_dump(mode="json")
    )
    title = "未发现直接替代解释" if item.outcome == "clear" else "发现替代解释风险"
    passed = sum(check.passed for check in item.checks)
    lines = [
        f"# Evolution Counterfactual Evidence `{item.evidence_id}`",
        "",
        f"**{title}；这是确定性证据扫描，不接受 Candidate，也不批准发布。**",
        "",
        f"- Independent Review：`{item.review_id}`",
        f"- Mechanical Gate：`{item.gate_id}` · `pass`（保持不变）",
        f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
        f"- Checks：{passed}/{len(item.checks)} 通过",
        f"- Files：{len(item.files)}",
        f"- 代表性 Findings：{len(item.findings)}",
        f"- 更小 scope：`{'plausible' if item.smaller_scope_found else 'not_found'}`",
        "- LLM used：`false`",
    ]
    if item.findings:
        grouped: dict[CounterfactualFindingCode, int] = {}
        for finding in item.findings:
            grouped[finding.code] = grouped.get(finding.code, 0) + 1
        lines.append(
            "- 风险：" + ", ".join(
                f"`{code.value}` × {count}"
                for code, count in sorted(grouped.items(), key=lambda pair: pair[0].value)
            )
        )
        affected = tuple(dict.fromkeys(finding.path for finding in item.findings))
        lines.append("- 影响文件：" + ", ".join(f"`{path}`" for path in affected))
    lines.extend((
        "- Required actions：" + ", ".join(
            f"`{action.value}`" for action in item.required_actions
        ),
        f"- Evidence SHA-256：`{item.evidence_sha256}`",
        "",
        (
            "下一步：EVO-04.5 reward-hacking detector；本结果不能覆盖 Mechanical Gate，"
            "也不能直接形成 accept/reject。"
        ),
    ))
    return "\n".join(lines)


def _scan_worktree(
    lease: ExperimentWorktreeLease,
    experiment: EvolutionExperimentContractAuthority,
    mutation: EvolutionMutationReceipt,
    storage_dir: Path,
) -> tuple[tuple[CounterfactualFileEvidence, ...], tuple[CounterfactualFinding, ...]]:
    _require_lease_binding(lease, experiment)
    root = Path(lease.worktree_path).resolve(strict=True)
    if not root.is_dir() or root.parent != storage_dir or root.name != lease.worktree_name:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_worktree_unmanaged",
            "Experiment worktree 不属于受管存储目录。",
        )
    top = _git_text(root, "rev-parse", "--show-toplevel")
    head = _git_text(root, "rev-parse", "HEAD").lower()
    if Path(top).resolve() != root or head != experiment.contract.baseline.commit:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_worktree_drift",
            "Experiment worktree Git binding 已漂移。",
        )
    files: list[CounterfactualFileEvidence] = []
    findings: list[CounterfactualFinding] = []
    required_metrics = mutation.required_metrics
    for receipt_file in mutation.files:
        before = _read_baseline(root, head, receipt_file)
        after = _read_candidate(root, receipt_file)
        evidence, file_findings = _scan_file(
            receipt_file,
            before=before,
            after=after,
            required_metrics=required_metrics,
        )
        files.append(evidence)
        findings.extend(file_findings)
    return tuple(files), tuple(sorted(
        findings,
        key=lambda item: (
            item.path,
            item.code.value,
            item.direction,
            item.baseline_line or 0,
            item.candidate_line or 0,
            item.evidence_sha256,
        ),
    ))


def _scan_file(
    receipt: MutationReceiptFile,
    *,
    before: bytes | None,
    after: bytes,
    required_metrics: tuple[str, ...],
) -> tuple[CounterfactualFileEvidence, tuple[CounterfactualFinding, ...]]:
    before_sha = hashlib.sha256(before).hexdigest() if before is not None else None
    after_sha = hashlib.sha256(after).hexdigest()
    if before_sha != receipt.before_sha256 or not hmac.compare_digest(
        after_sha, receipt.after_sha256
    ):
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_file_digest_mismatch",
            f"Counterfactual 文件 `{receipt.path}` 与 Mutation Receipt 不一致。",
        )
    before_lines = _decode_lines(before, receipt.path)
    after_lines = _decode_lines(after, receipt.path)
    unified = tuple(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{receipt.path}",
        tofile=f"b/{receipt.path}",
        lineterm="",
    ))
    if len(unified) > _MAX_DIFF_LINES:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_diff_oversized",
            f"Counterfactual diff `{receipt.path}` 超过 20000 行。",
        )
    unified_sha = _sha256_payload(unified)
    if not hmac.compare_digest(unified_sha, receipt.unified_diff_sha256):
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_diff_digest_mismatch",
            f"Counterfactual diff `{receipt.path}` 与 Mutation Receipt 不一致。",
        )
    changes = _changed_lines(before_lines, after_lines)
    added = sum(item.direction == "added" for item in changes)
    deleted = sum(item.direction == "deleted" for item in changes)
    if (added, deleted) != (receipt.added_lines, receipt.deleted_lines):
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_diff_count_mismatch",
            f"Counterfactual diff `{receipt.path}` 行数与 Mutation Receipt 不一致。",
        )
    findings = _detect_findings(
        receipt.path,
        changes,
        required_metrics=required_metrics,
    )
    semantic = tuple(item for item in changes if item.semantic)
    suspicious_line_refs = {
        (finding.direction, finding.baseline_line, finding.candidate_line)
        for finding in findings
        if finding.direction != "file"
    }
    semantic_refs = {
        (
            item.direction,
            item.baseline_line if item.direction == "deleted" else None,
            item.candidate_line if item.direction == "added" else None,
        )
        for item in semantic
    }
    fully_suspicious = bool(semantic_refs) and (
        semantic_refs.issubset(suspicious_line_refs)
        or all(
            _line_is_direct_alternative(
                receipt.path,
                item,
                required_metrics=required_metrics,
            )
            for item in semantic
        )
    )
    smaller_scope = not semantic or fully_suspicious
    if not semantic:
        findings = (*findings, _finding(
            CounterfactualFindingCode.NON_SEMANTIC_SCOPE,
            receipt.path,
            direction="file",
            severity=CounterfactualSeverity.MEDIUM,
        ))
    codes = tuple(sorted({item.code for item in findings}, key=lambda item: item.value))
    line_digest = _sha256_payload([
        {
            "direction": item.direction,
            "baseline_line": item.baseline_line,
            "candidate_line": item.candidate_line,
            "content_sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
            "semantic": item.semantic,
        }
        for item in changes
    ])
    payload = {
        "path": receipt.path,
        "operation": receipt.operation,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "unified_diff_sha256": unified_sha,
        "added_lines": added,
        "deleted_lines": deleted,
        "semantic_added_lines": sum(
            item.semantic and item.direction == "added" for item in changes
        ),
        "semantic_deleted_lines": sum(
            item.semantic and item.direction == "deleted" for item in changes
        ),
        "changed_line_evidence_sha256": line_digest,
        "finding_codes": [item.value for item in codes],
        "smaller_scope_plausible": smaller_scope,
    }
    digest = _sha256_payload(payload)
    return (
        CounterfactualFileEvidence.model_validate({
            **payload,
            "evidence_sha256": digest,
        }),
        tuple(sorted(
            findings,
            key=lambda item: (
                item.code.value,
                item.direction,
                item.baseline_line or 0,
                item.candidate_line or 0,
                item.evidence_sha256,
            ),
        )),
    )


class _ChangedLine(_StrictModel):
    direction: Literal["added", "deleted"]
    baseline_line: int | None = None
    candidate_line: int | None = None
    text: str
    semantic: bool
    group: int = Field(ge=0)


def _changed_lines(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[_ChangedLine, ...]:
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    result: list[_ChangedLine] = []
    for group, (tag, left_start, left_end, right_start, right_end) in enumerate(
        matcher.get_opcodes()
    ):
        if tag == "equal":
            continue
        whitespace_only_left: set[int] = set()
        whitespace_only_right: set[int] = set()
        if tag == "replace":
            unmatched_right = list(range(right_start, right_end))
            for left_index in range(left_start, left_end):
                for offset, right_index in enumerate(unmatched_right):
                    if before[left_index].strip() == after[right_index].strip():
                        whitespace_only_left.add(left_index)
                        whitespace_only_right.add(right_index)
                        unmatched_right.pop(offset)
                        break
        for index in range(left_start, left_end):
            result.append(_ChangedLine(
                direction="deleted",
                baseline_line=index + 1,
                text=before[index],
                semantic=(
                    _semantic_line(before[index]) and index not in whitespace_only_left
                ),
                group=group,
            ))
        for index in range(right_start, right_end):
            result.append(_ChangedLine(
                direction="added",
                candidate_line=index + 1,
                text=after[index],
                semantic=(
                    _semantic_line(after[index]) and index not in whitespace_only_right
                ),
                group=group,
            ))
    return tuple(result)


def _detect_findings(
    path: str,
    changes: tuple[_ChangedLine, ...],
    *,
    required_metrics: tuple[str, ...],
) -> tuple[CounterfactualFinding, ...]:
    findings: list[CounterfactualFinding] = []
    test_surface = _is_test_path(path)
    metric_surface = _is_metric_path(path)
    for item in changes:
        folded = item.text.casefold()
        if not item.semantic:
            continue
        if item.direction == "deleted" and test_surface:
            findings.append(_finding(
                CounterfactualFindingCode.TEST_DELETION,
                path,
                direction="deleted",
                severity=CounterfactualSeverity.HIGH,
                baseline_line=item.baseline_line,
                text=item.text,
            ))
        if metric_surface or any(metric.casefold() in folded for metric in required_metrics):
            findings.append(_finding(
                CounterfactualFindingCode.METRIC_MUTATION,
                path,
                direction=item.direction,
                severity=CounterfactualSeverity.HIGH,
                baseline_line=item.baseline_line,
                candidate_line=item.candidate_line,
                text=item.text,
            ))
        if item.direction == "added" and _SKIP_RE.search(item.text):
            findings.append(_finding(
                CounterfactualFindingCode.SKIP_ADDED,
                path,
                direction="added",
                severity=CounterfactualSeverity.HIGH,
                candidate_line=item.candidate_line,
                text=item.text,
            ))
        if item.direction == "added" and _MOCK_RE.search(item.text):
            findings.append(_finding(
                CounterfactualFindingCode.MOCK_ADDED,
                path,
                direction="added",
                severity=CounterfactualSeverity.HIGH,
                candidate_line=item.candidate_line,
                text=item.text,
            ))
        if item.direction == "added" and _LEAKAGE_RE.search(item.text):
            findings.append(_finding(
                CounterfactualFindingCode.EVALUATION_LEAKAGE,
                path,
                direction="added",
                severity=CounterfactualSeverity.HIGH,
                candidate_line=item.candidate_line,
                text=item.text,
            ))
    groups = sorted({item.group for item in changes})
    for group in groups:
        deleted = [item for item in changes if item.group == group and item.direction == "deleted"]
        added = [item for item in changes if item.group == group and item.direction == "added"]
        for old, new in _pair_threshold_lines(deleted, added):
            if _threshold_relaxed(old.text, new.text):
                findings.append(_finding(
                    CounterfactualFindingCode.THRESHOLD_RELAXATION,
                    path,
                    direction="replacement",
                    severity=CounterfactualSeverity.HIGH,
                    baseline_line=old.baseline_line,
                    candidate_line=new.candidate_line,
                    text=f"{old.text}\n{new.text}",
                ))
    unique = sorted(
        {item.evidence_sha256: item for item in findings}.values(),
        key=lambda item: (
            item.code.value,
            item.direction,
            item.baseline_line or 0,
            item.candidate_line or 0,
            item.evidence_sha256,
        ),
    )
    counts: dict[CounterfactualFindingCode, int] = {}
    bounded: list[CounterfactualFinding] = []
    for item in unique:
        count = counts.get(item.code, 0)
        if count >= _MAX_REPRESENTATIVE_FINDINGS_PER_CODE:
            continue
        counts[item.code] = count + 1
        bounded.append(item)
    return tuple(bounded)


def _line_is_direct_alternative(
    path: str,
    item: _ChangedLine,
    *,
    required_metrics: tuple[str, ...],
) -> bool:
    folded = item.text.casefold()
    return (
        _is_metric_path(path)
        or any(metric.casefold() in folded for metric in required_metrics)
        or (item.direction == "deleted" and _is_test_path(path))
        or (
            item.direction == "added"
            and bool(
                _SKIP_RE.search(item.text)
                or _MOCK_RE.search(item.text)
                or _LEAKAGE_RE.search(item.text)
            )
        )
    )


def _pair_threshold_lines(
    deleted: list[_ChangedLine],
    added: list[_ChangedLine],
) -> Iterable[tuple[_ChangedLine, _ChangedLine]]:
    remaining = list(added)
    for old in deleted:
        old_shape = _numeric_shape(old.text)
        if old_shape is None:
            continue
        for index, new in enumerate(remaining):
            if _numeric_shape(new.text) == old_shape:
                yield old, new
                remaining.pop(index)
                break


def _threshold_relaxed(before: str, after: str) -> bool:
    left = _COMPARISON_RE.search(before)
    right = _COMPARISON_RE.search(after)
    if left and right and left.group("op") == right.group("op"):
        old = float(left.group("value"))
        new = float(right.group("value"))
        if left.group("op") in {">", ">="}:
            return new < old
        return new > old
    left_key = _KEY_THRESHOLD_RE.search(before)
    right_key = _KEY_THRESHOLD_RE.search(after)
    if not left_key or not right_key or left_key.group("key").casefold() != (
        right_key.group("key").casefold()
    ):
        return False
    key = left_key.group("key").casefold()
    old = float(left_key.group("value"))
    new = float(right_key.group("value"))
    if any(token in key for token in ("min", "pass", "score", "rate")):
        return new < old
    if any(token in key for token in ("max", "error", "fail")):
        return new > old
    return False


def _numeric_shape(value: str) -> str | None:
    if not (_COMPARISON_RE.search(value) or _KEY_THRESHOLD_RE.search(value)):
        return None
    return re.sub(r"-?\d+(?:\.\d+)?", "#", value).strip().casefold()


def _finding(
    code: CounterfactualFindingCode,
    path: str,
    *,
    direction: Literal["added", "deleted", "replacement", "file"],
    severity: CounterfactualSeverity,
    baseline_line: int | None = None,
    candidate_line: int | None = None,
    text: str = "",
) -> CounterfactualFinding:
    payload = {
        "code": code.value,
        "severity": severity.value,
        "path": path,
        "direction": direction,
        "baseline_line": baseline_line,
        "candidate_line": candidate_line,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return CounterfactualFinding(
        code=code,
        severity=severity,
        path=path,
        direction=direction,
        baseline_line=baseline_line,
        candidate_line=candidate_line,
        evidence_sha256=_sha256_payload(payload),
    )


def _build_checks(
    *,
    review: EvolutionIndependentReview,
    lease_binding: CounterfactualLeaseBinding,
    files: tuple[CounterfactualFileEvidence, ...],
    findings: tuple[CounterfactualFinding, ...],
) -> tuple[CounterfactualCheck, ...]:
    codes = {item.code for item in findings}
    file_refs = tuple(item.evidence_sha256 for item in files)
    facts: tuple[tuple[CounterfactualRule, bool, tuple[str, ...]], ...] = (
        (CounterfactualRule.AUTHORITY_CHAIN_BOUND, True, (review.review_id, review.gate_id)),
        (CounterfactualRule.WORKTREE_BINDING_EXACT, True, (lease_binding.binding_sha256,)),
        (CounterfactualRule.CANDIDATE_BYTES_MATCH_RECEIPT, True, file_refs),
        (CounterfactualRule.UNIFIED_DIFF_MATCH_RECEIPT, True, tuple(
            item.unified_diff_sha256 for item in files
        )),
        (CounterfactualRule.NO_REDUCIBLE_SCOPE, not any(
            item.smaller_scope_plausible for item in files
        ), file_refs),
        (CounterfactualRule.NO_TEST_DELETION,
         CounterfactualFindingCode.TEST_DELETION not in codes,
         _finding_refs(findings, CounterfactualFindingCode.TEST_DELETION, review.review_id)),
        (CounterfactualRule.NO_METRIC_MUTATION,
         CounterfactualFindingCode.METRIC_MUTATION not in codes,
         _finding_refs(findings, CounterfactualFindingCode.METRIC_MUTATION, review.review_id)),
        (CounterfactualRule.NO_THRESHOLD_RELAXATION,
         CounterfactualFindingCode.THRESHOLD_RELAXATION not in codes,
         _finding_refs(findings, CounterfactualFindingCode.THRESHOLD_RELAXATION, review.review_id)),
        (CounterfactualRule.NO_SKIP_ADDED,
         CounterfactualFindingCode.SKIP_ADDED not in codes,
         _finding_refs(findings, CounterfactualFindingCode.SKIP_ADDED, review.review_id)),
        (CounterfactualRule.NO_MOCK_ADDED,
         CounterfactualFindingCode.MOCK_ADDED not in codes,
         _finding_refs(findings, CounterfactualFindingCode.MOCK_ADDED, review.review_id)),
        (CounterfactualRule.NO_EVALUATION_LEAKAGE,
         CounterfactualFindingCode.EVALUATION_LEAKAGE not in codes,
         _finding_refs(findings, CounterfactualFindingCode.EVALUATION_LEAKAGE, review.review_id)),
    )
    return tuple(
        CounterfactualCheck(
            order=index,
            rule=rule,
            passed=passed,
            evidence_refs=refs,
        )
        for index, (rule, passed, refs) in enumerate(facts, start=1)
    )


def _finding_refs(
    findings: tuple[CounterfactualFinding, ...],
    code: CounterfactualFindingCode,
    fallback: str,
) -> tuple[str, ...]:
    refs = tuple(item.evidence_sha256 for item in findings if item.code is code)
    if len(refs) > 32:
        return (_sha256_payload(refs),)
    return refs or (fallback,)


def _file_matches_receipt(
    evidence: CounterfactualFileEvidence,
    receipt: MutationReceiptFile,
) -> bool:
    return (
        evidence.path == receipt.path
        and evidence.operation == receipt.operation
        and evidence.before_sha256 == receipt.before_sha256
        and evidence.after_sha256 == receipt.after_sha256
        and evidence.unified_diff_sha256 == receipt.unified_diff_sha256
        and evidence.added_lines == receipt.added_lines
        and evidence.deleted_lines == receipt.deleted_lines
    )


def _require_authority_chain(
    review: EvolutionIndependentReview,
    gate: EvolutionMechanicalGate,
    decision: EvolutionDecisionInput,
    mutation: EvolutionMutationReceipt,
    experiment: EvolutionExperimentContractAuthority,
) -> None:
    if not (
        review.gate == gate
        and review.gate_id == gate.gate_id
        and review.gate_sha256 == gate.gate_sha256
        and gate.outcome == "pass"
        and gate.decision_input == decision
        and gate.decision_input_id == decision.decision_input_id
        and gate.decision_input_sha256 == decision.decision_input_sha256
        and decision.mutation == mutation
        and decision.mutation_receipt_id == mutation.mutation_receipt_id
        and decision.mutation_receipt_sha256 == mutation.receipt_sha256
        and decision.experiment == experiment
        and decision.experiment_contract_id == experiment.contract_id
        and decision.experiment_contract_sha256 == experiment.contract_manifest_sha256
    ):
        raise ValueError("Counterfactual authority chain mismatch")


def _require_lease_binding(
    lease: ExperimentWorktreeLease,
    experiment: EvolutionExperimentContractAuthority,
) -> None:
    contract = experiment.contract
    expected_lease = hashlib.sha256(
        f"{contract.contract_id}:{contract.manifest_sha256}".encode()
    ).hexdigest()
    if not (
        lease.lease_id == f"evl_{expected_lease[:24]}"
        and lease.contract_id == contract.contract_id
        and lease.manifest_sha256 == contract.manifest_sha256
        and lease.session_id == contract.source.session_id
        and lease.mission_id == contract.source.mission_id
        and lease.task_id == contract.source.task_id
        and lease.baseline_commit == contract.baseline.commit
    ):
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_lease_mismatch",
            "Experiment Lease 与 Contract authority 不一致。",
        )


def _lease_binding(
    lease: ExperimentWorktreeLease,
    experiment: EvolutionExperimentContractAuthority,
) -> CounterfactualLeaseBinding:
    _require_lease_binding(lease, experiment)
    payload = {
        "lease_id": lease.lease_id,
        "contract_id": lease.contract_id,
        "contract_manifest_sha256": lease.manifest_sha256,
        "worktree_name": lease.worktree_name,
        "branch_sha256": hashlib.sha256(lease.branch.encode("utf-8")).hexdigest(),
        "baseline_commit": lease.baseline_commit,
        "observed_state": lease.state.value,
    }
    return CounterfactualLeaseBinding.model_validate({
        **payload,
        "binding_sha256": _sha256_payload(payload),
    })


def _require_existing(
    existing: EvolutionCounterfactualEvidence,
    review: EvolutionIndependentReview,
) -> None:
    if not (
        existing.review == review
        and existing.review_id == review.review_id
        and existing.review_sha256 == review.review_sha256
    ):
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_existing_mismatch",
            "已存在 Counterfactual Evidence 与 Independent Review 不一致。",
        )


def _read_baseline(
    root: Path,
    commit: str,
    receipt: MutationReceiptFile,
) -> bytes | None:
    if receipt.operation == "create":
        completed = _git(root, "cat-file", "-e", f"{commit}:{receipt.path}")
        if completed.returncode == 0:
            raise EvolutionCounterfactualEvidenceError(
                "counterfactual_baseline_mismatch",
                f"新文件 `{receipt.path}` 在 baseline 中已存在。",
            )
        return None
    completed = _git(root, "cat-file", "blob", f"{commit}:{receipt.path}")
    if completed.returncode != 0:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_baseline_missing",
            f"无法读取 `{receipt.path}` 的 Git baseline。",
        )
    if len(completed.stdout) > _MAX_SOURCE_BYTES:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_source_oversized",
            f"Counterfactual baseline `{receipt.path}` 超过 2 MiB。",
        )
    return completed.stdout


def _read_candidate(root: Path, receipt: MutationReceiptFile) -> bytes:
    target = root / receipt.path
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
        if resolved != target.absolute():
            raise OSError("symlinked candidate path")
        before = target.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_candidate_missing",
            f"Counterfactual candidate `{receipt.path}` 不存在。",
        ) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SOURCE_BYTES:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_candidate_invalid",
            f"Counterfactual candidate `{receipt.path}` 必须是小于等于 2 MiB 的普通文件。",
        )
    try:
        data = target.read_bytes()
        after = target.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_candidate_read_failed",
            f"Counterfactual candidate `{receipt.path}` 无法读取。",
        ) from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(data) != after.st_size:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_candidate_changed",
            f"Counterfactual candidate `{receipt.path}` 在读取期间发生变化。",
        )
    return data


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_git_failed", "无法读取 Counterfactual Git 证据。"
        ) from exc
    if len(completed.stdout) > _MAX_GIT_OUTPUT or len(completed.stderr) > 64 * 1_024:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_git_oversized", "Counterfactual Git 输出超过安全上限。"
        )
    return completed


def _git_text(root: Path, *args: str) -> str:
    completed = _git(root, *args)
    if completed.returncode != 0:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_git_failed", "Counterfactual Git identity 不可验证。"
        )
    try:
        return completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_git_invalid", "Counterfactual Git identity 不是 ASCII。"
        ) from exc


def _decode_lines(content: bytes | None, path: str) -> tuple[str, ...]:
    if content is None:
        return ()
    try:
        return tuple(content.decode("utf-8").splitlines())
    except UnicodeDecodeError as exc:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_invalid_encoding",
            f"Counterfactual 文件 `{path}` 不是 UTF-8。",
        ) from exc


def _semantic_line(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and not stripped.startswith(("#", "//", "/*", "*", "--"))


def _is_test_path(path: str) -> bool:
    folded = path.casefold()
    name = Path(folded).name
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in Path(folded).parts)
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    )


def _is_metric_path(path: str) -> bool:
    folded = path.casefold()
    return any(token in folded for token in (
        "metric", "evaluation", "evaluator", "benchmark", "scoring", "scorecard"
    ))


def _relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(char in normalized for char in ("\x00", "\r", "\n"))
    ):
        raise ValueError("Counterfactual path 必须是安全相对路径。")
    return normalized


def _workspace(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_workspace_missing", "Counterfactual 工作区不存在。"
        ) from exc
    if not path.is_dir():
        raise EvolutionCounterfactualEvidenceError(
            "counterfactual_workspace_missing", "Counterfactual 工作区不存在。"
        )
    return path


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_counterfactual_evidence (
            evidence_id TEXT PRIMARY KEY,
            evidence_sha256 TEXT NOT NULL,
            review_id TEXT NOT NULL UNIQUE,
            review_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('clear', 'concern')),
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionCounterfactualEvidence:
    encoded = str(row["evidence_json"])
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Counterfactual Evidence Store artifact 过大。")
    item = EvolutionCounterfactualEvidence.model_validate_json(encoded)
    if not (
        row["evidence_id"] == item.evidence_id
        and row["evidence_sha256"] == item.evidence_sha256
        and row["review_id"] == item.review_id
        and row["review_sha256"] == item.review_sha256
        and row["workspace_root"] == item.workspace_root
        and row["outcome"] == item.outcome
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Counterfactual Evidence Store index 不一致。")
    return item


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_SKIP_RE = re.compile(
    r"(?:pytest\.mark\.(?:skip|skipif|xfail)|unittest\.skip|@disabled\b|"
    r"\b(?:describe|context|it|test)\.skip\s*\(|\b(?:xdescribe|xit|xtest)\s*\()",
    re.IGNORECASE,
)
_MOCK_RE = re.compile(
    r"(?:unittest\.mock|\b(?:magicmock|mock)\s*\(|\bmonkeypatch\b|"
    r"\b(?:jest|vi)\.mock\s*\(|\bsinon\.stub\s*\()",
    re.IGNORECASE,
)
_LEAKAGE_RE = re.compile(
    r"\b(?:ground_truth|golden_answers?|answer_key|expected_outputs?|"
    r"test_labels?|evaluation_results?)\b",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"(?P<op>>=|<=|>|<)\s*(?P<value>-?\d{1,18}(?:\.\d{1,18})?)"
)
_KEY_THRESHOLD_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*(?:threshold|min|max|rate|score|fail|error)"
    r"[A-Za-z0-9_.-]*)\s*[:=]\s*(?P<value>-?\d{1,18}(?:\.\d{1,18})?)",
    re.IGNORECASE,
)


__all__ = [
    "COUNTERFACTUAL_EVIDENCE_POLICY",
    "CounterfactualCheck",
    "CounterfactualFileEvidence",
    "CounterfactualFinding",
    "CounterfactualFindingCode",
    "CounterfactualLeaseBinding",
    "CounterfactualRequiredAction",
    "CounterfactualRule",
    "CounterfactualSeverity",
    "EvolutionCounterfactualEvidence",
    "EvolutionCounterfactualEvidenceBuilder",
    "EvolutionCounterfactualEvidenceError",
    "EvolutionCounterfactualEvidenceExecutor",
    "EvolutionCounterfactualEvidenceStore",
    "render_counterfactual_evidence",
]
