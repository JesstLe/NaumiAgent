"""Mechanical, durable failure attribution for EVO-03 comparisons."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.self_review_green_cohort import (
    EvolutionSelfReviewGreenCohortReceipt,
)
from naumi_agent.evolution.self_review_red_baseline import (
    EvolutionSelfReviewRedCohortReceipt,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_receipt import (
    EvalComparisonDecision,
    HarnessEvalComparisonReceipt,
)
from naumi_agent.harness.eval_statistics import EvalStatisticalVerdict
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoreError,
)

FAILURE_ATTRIBUTION_POLICY = "evolution-failure-attribution-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_EVIDENCE_INCOMPLETE_CODES = frozenset({
    "sample_count_insufficient",
    "confidence_interval_overlaps_zero",
})


class FailureAttributionCategory(StrEnum):
    NONE = "none"
    OBJECTIVE_NOT_IMPROVED = "objective_not_improved"
    CANDIDATE_DEFECT = "candidate_defect"
    EVALUATION_INFRASTRUCTURE = "evaluation_infrastructure"
    ENVIRONMENT_INCOMPATIBLE = "environment_incompatible"
    FLAKY_EVIDENCE = "flaky_evidence"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"


class FailureAttributionAction(StrEnum):
    CONTINUE_TO_REFLECTION = "continue_to_reflection"
    REVISE_CANDIDATE = "revise_candidate"
    RERUN_EVALUATION = "rerun_evaluation"
    REBUILD_ENVIRONMENT = "rebuild_environment"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionFailureAttributionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-failure-attribution-v1"] = (
        FAILURE_ATTRIBUTION_POLICY
    )
    attribution_id: str = Field(pattern=r"^evattr_[0-9a-f]{24}$")
    attribution_sha256: str = Field(pattern=_SHA256_RE)
    comparison_id: str = Field(pattern=_SHA256_RE)
    comparison_receipt_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(
        pattern=r"^evvred(?:run|cohort)_[0-9a-f]{24}$"
    )
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    green_receipt_id: str = Field(
        pattern=r"^evvgreen(?:run|cohort)_[0-9a-f]{24}$"
    )
    green_receipt_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    category: FailureAttributionCategory
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    evidence_codes: tuple[str, ...] = Field(max_length=128)
    action: FailureAttributionAction
    candidate_fault: bool
    retryable: bool
    requires_rerun: bool
    reflection_eligible: bool
    created_at: str

    @field_validator("evidence_codes")
    @classmethod
    def _ordered_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("Failure attribution evidence codes 必须排序且不得重复。")
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("Failure attribution evidence code 无效。")
        return values

    @field_validator("created_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Failure attribution created_at 必须包含时区。")
        return parsed.isoformat()

    @model_validator(mode="after")
    def _tamper_evident(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"attribution_id", "attribution_sha256"},
            )
        )
        if not hmac.compare_digest(self.attribution_sha256, expected):
            raise ValueError("Failure attribution digest 不一致。")
        if self.attribution_id != f"evattr_{expected[:24]}":
            raise ValueError("Failure attribution identity 不一致。")
        _require_semantics(self)
        return self


class EvolutionFailureAttributionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionFailureAttributionAuthority(_StrictModel):
    """Lane-neutral, fully resolved authority consumed by the mapping kernel."""

    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(
        pattern=r"^evvred(?:run|cohort)_[0-9a-f]{24}$"
    )
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    green_receipt_id: str = Field(
        pattern=r"^evvgreen(?:run|cohort)_[0-9a-f]{24}$"
    )
    green_receipt_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    red_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    green_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    red_samples: int = Field(ge=1, le=100)
    green_samples: int = Field(ge=1, le=100)
    red_result_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    green_result_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("red_result_sha256", "green_result_sha256")
    @classmethod
    def _valid_result_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_RE, value) is None for value in values):
            raise ValueError("Failure Attribution sample digest 格式无效。")
        return values

    @model_validator(mode="after")
    def _complete_cohorts(self) -> Self:
        if not (
            self.red_samples == len(self.red_result_sha256)
            and self.green_samples == len(self.green_result_sha256)
        ):
            raise ValueError("Failure Attribution cohort authority 不完整。")
        return self


class EvolutionFailureAttributionKernel:
    """Apply the single mechanical attribution policy to lane-neutral facts."""

    def build(
        self,
        *,
        authority: EvolutionFailureAttributionAuthority,
        comparison: HarnessStoredEvalComparisonReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        try:
            facts = EvolutionFailureAttributionAuthority.model_validate(
                authority.model_dump(mode="json")
            )
            receipt = HarnessEvalComparisonReceipt.model_validate(
                comparison.receipt.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "attribution_authority_invalid",
                "Failure Attribution authority 无效或已被篡改。",
            ) from exc
        if not (
            comparison.id == receipt.id
            and comparison.receipt_sha256 == receipt.receipt_sha256
            and comparison.workspace_root == receipt.workspace_root
            and comparison.suite_id == receipt.suite_id
            and comparison.baseline_id == receipt.baseline_id
            and comparison.current_batch_id == receipt.current_batch_id
            and comparison.decision == receipt.decision.value
            and receipt.suite_id == facts.suite_id
            and receipt.baseline_batch_id == facts.red_batch_id
            and receipt.current_batch_id == facts.green_batch_id
            and receipt.baseline_samples == facts.red_samples
            and receipt.current_samples == facts.green_samples
            and receipt.baseline_samples_sha256
            == _sample_set_sha256(facts.red_result_sha256)
            and receipt.current_samples_sha256
            == _sample_set_sha256(facts.green_result_sha256)
            and tuple(item.result_sha256 for item in receipt.sample_evidence)
            == facts.green_result_sha256
        ):
            raise EvolutionFailureAttributionError(
                "attribution_authority_mismatch",
                "RED、GREEN 与 H5c Comparison authority 不一致。",
            )
        classification = _classify(receipt)
        payload = {
            "schema_version": 1,
            "policy_version": FAILURE_ATTRIBUTION_POLICY,
            "comparison_id": receipt.id,
            "comparison_receipt_sha256": receipt.receipt_sha256,
            "validation_plan_id": facts.validation_plan_id,
            "validation_plan_sha256": facts.validation_plan_sha256,
            "red_receipt_id": facts.red_receipt_id,
            "red_receipt_sha256": facts.red_receipt_sha256,
            "green_receipt_id": facts.green_receipt_id,
            "green_receipt_sha256": facts.green_receipt_sha256,
            "candidate_id": facts.candidate_id,
            "candidate_revision": facts.candidate_revision,
            **classification,
            "created_at": receipt.created_at,
        }
        digest = _sha256_payload(payload)
        return EvolutionFailureAttributionReceipt.model_validate({
            **payload,
            "attribution_id": f"evattr_{digest[:24]}",
            "attribution_sha256": digest,
        })


class EvolutionFailureAttributionBuilder:
    def __init__(self, kernel: EvolutionFailureAttributionKernel | None = None) -> None:
        self._kernel = kernel or EvolutionFailureAttributionKernel()

    def build(
        self,
        *,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionSelfReviewRedCohortReceipt,
        green_receipt: EvolutionSelfReviewGreenCohortReceipt,
        comparison: HarnessStoredEvalComparisonReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        try:
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            red = EvolutionSelfReviewRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            green = EvolutionSelfReviewGreenCohortReceipt.model_validate(
                green_receipt.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "attribution_authority_invalid",
                "Failure Attribution authority 无效或已被篡改。",
            ) from exc
        if not (
            red.validation_plan_id == plan.validation_plan_id
            and red.validation_plan_sha256 == plan.validation_plan_sha256
            and green.validation_plan_id == plan.validation_plan_id
            and green.validation_plan_sha256 == plan.validation_plan_sha256
            and green.red_receipt_id == red.receipt_id
            and green.red_receipt_sha256 == red.receipt_sha256
            and green.candidate_id == plan.candidate_id
            and green.candidate_revision == plan.candidate_revision
            and red.suite_id == green.suite_id
        ):
            raise EvolutionFailureAttributionError(
                "attribution_authority_mismatch",
                "Plan、RED 与 GREEN completion authority 不一致。",
            )
        authority = EvolutionFailureAttributionAuthority(
            validation_plan_id=plan.validation_plan_id,
            validation_plan_sha256=plan.validation_plan_sha256,
            red_receipt_id=red.receipt_id,
            red_receipt_sha256=red.receipt_sha256,
            green_receipt_id=green.receipt_id,
            green_receipt_sha256=green.receipt_sha256,
            candidate_id=plan.candidate_id,
            candidate_revision=plan.candidate_revision,
            suite_id=red.suite_id,
            red_batch_id=red.batch_id,
            green_batch_id=green.batch_id,
            red_samples=red.persisted_samples,
            green_samples=green.persisted_samples,
            red_result_sha256=red.sample_result_sha256,
            green_result_sha256=green.sample_result_sha256,
        )
        return self._kernel.build(authority=authority, comparison=comparison)


class EvolutionFailureAttributionExecutor:
    def __init__(
        self,
        *,
        harness_store: HarnessStore,
        attribution_store: EvolutionFailureAttributionStore,
        builder: EvolutionFailureAttributionBuilder | None = None,
    ) -> None:
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Failure Attribution executor 需要 HarnessStore。")
        if not isinstance(attribution_store, EvolutionFailureAttributionStore):
            raise TypeError("Failure Attribution executor 需要 Attribution Store。")
        self._harness_store = harness_store
        self._attribution_store = attribution_store
        self._builder = builder or EvolutionFailureAttributionBuilder()

    async def execute(
        self,
        *,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionSelfReviewRedCohortReceipt,
        green_receipt: EvolutionSelfReviewGreenCohortReceipt,
        comparison: HarnessStoredEvalComparisonReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        try:
            authoritative = await self._harness_store.get_eval_comparison_receipt(
                comparison.workspace_root,
                comparison.suite_id,
                comparison.baseline_id,
                comparison.current_batch_id,
            )
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "attribution_comparison_read_failed",
                "无法从 Harness Store 读取 H5c Comparison authority。",
            ) from exc
        if authoritative is None or authoritative != comparison:
            raise EvolutionFailureAttributionError(
                "attribution_comparison_not_authoritative",
                "传入的 H5c Comparison 不是 Harness Store 当前不可变事实。",
            )
        receipt = self._builder.build(
            validation_plan=validation_plan,
            red_receipt=red_receipt,
            green_receipt=green_receipt,
            comparison=authoritative,
        )
        return await self._attribution_store.record(receipt)


class EvolutionFailureAttributionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        receipt: EvolutionFailureAttributionReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        artifact = EvolutionFailureAttributionReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM evolution_failure_attributions "
                    "WHERE comparison_id = ?",
                    (artifact.comparison_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    restored = _from_row(existing)
                    if restored != artifact:
                        await db.rollback()
                        raise EvolutionFailureAttributionError(
                            "attribution_store_conflict",
                            "同一 H5c Comparison 不可覆盖为不同 Failure Attribution。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    """
                    INSERT INTO evolution_failure_attributions (
                        comparison_id, attribution_id, attribution_sha256,
                        receipt_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.comparison_id,
                        artifact.attribution_id,
                        artifact.attribution_sha256,
                        _json_dumps(artifact.model_dump(mode="json")),
                        artifact.created_at,
                    ),
                )
                await db.commit()
        except EvolutionFailureAttributionError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "attribution_store_error",
                "Failure Attribution 无法持久化。",
            ) from exc
        restored = await self.get(artifact.comparison_id)
        assert restored is not None
        return restored

    async def get(
        self,
        comparison_id: str,
    ) -> EvolutionFailureAttributionReceipt | None:
        if (
            not isinstance(comparison_id, str)
            or re.fullmatch(_SHA256_RE, comparison_id) is None
        ):
            raise ValueError("comparison_id 必须是 SHA-256。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                cursor = await db.execute(
                    "SELECT * FROM evolution_failure_attributions "
                    "WHERE comparison_id = ?",
                    (comparison_id,),
                )
                row = await cursor.fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "attribution_store_corrupt",
                "Failure Attribution 损坏或无法读取。",
            ) from exc


def _classify(receipt: HarnessEvalComparisonReceipt) -> dict[str, object]:
    evidence_codes = tuple(sorted({
        code
        for code in (
            receipt.statistical_code,
            *(item.mechanical_code for item in receipt.sample_evidence),
            *(item.policy_code for item in receipt.sample_evidence),
            *(
                code
                for item in receipt.sample_evidence
                for code in item.violation_codes
            ),
        )
        if code
    }))
    statistical = receipt.statistical_verdict
    decision = receipt.decision
    if decision is EvalComparisonDecision.INCOMPATIBLE:
        values = (
            FailureAttributionCategory.ENVIRONMENT_INCOMPATIBLE,
            "comparison_identity_incompatible",
            FailureAttributionAction.REBUILD_ENVIRONMENT,
            False,
            True,
            True,
            False,
        )
    elif decision is EvalComparisonDecision.INCONCLUSIVE:
        incomplete = receipt.statistical_code in _EVIDENCE_INCOMPLETE_CODES
        values = (
            FailureAttributionCategory.EVIDENCE_INCOMPLETE
            if incomplete
            else FailureAttributionCategory.EVALUATION_INFRASTRUCTURE,
            receipt.statistical_code or "evaluation_inconclusive",
            FailureAttributionAction.RERUN_EVALUATION,
            False,
            True,
            True,
            False,
        )
    elif decision is EvalComparisonDecision.FLAKY:
        values = (
            FailureAttributionCategory.FLAKY_EVIDENCE,
            receipt.statistical_code or "case_status_flaky",
            FailureAttributionAction.RERUN_EVALUATION,
            False,
            True,
            True,
            False,
        )
    elif decision is EvalComparisonDecision.FAILED or (
        decision is EvalComparisonDecision.PASSED
        and statistical is EvalStatisticalVerdict.REGRESSED
    ):
        values = (
            FailureAttributionCategory.CANDIDATE_DEFECT,
            "candidate_policy_failed"
            if decision is EvalComparisonDecision.FAILED
            else "statistical_regression_within_policy",
            FailureAttributionAction.REVISE_CANDIDATE,
            True,
            False,
            False,
            False,
        )
    elif statistical is EvalStatisticalVerdict.IMPROVED:
        values = (
            FailureAttributionCategory.NONE,
            "verified_improvement",
            FailureAttributionAction.CONTINUE_TO_REFLECTION,
            False,
            False,
            False,
            True,
        )
    else:
        values = (
            FailureAttributionCategory.OBJECTIVE_NOT_IMPROVED,
            "objective_metric_unchanged",
            FailureAttributionAction.REVISE_CANDIDATE,
            False,
            False,
            False,
            False,
        )
    category, reason, action, fault, retryable, rerun, promotable = values
    return {
        "category": category.value,
        "reason_code": reason,
        "evidence_codes": evidence_codes,
        "action": action.value,
        "candidate_fault": fault,
        "retryable": retryable,
        "requires_rerun": rerun,
        "reflection_eligible": promotable,
    }


def _require_semantics(receipt: EvolutionFailureAttributionReceipt) -> None:
    expected = {
        FailureAttributionCategory.NONE: (
            FailureAttributionAction.CONTINUE_TO_REFLECTION,
            False,
            False,
            False,
            True,
        ),
        FailureAttributionCategory.OBJECTIVE_NOT_IMPROVED: (
            FailureAttributionAction.REVISE_CANDIDATE,
            False,
            False,
            False,
            False,
        ),
        FailureAttributionCategory.CANDIDATE_DEFECT: (
            FailureAttributionAction.REVISE_CANDIDATE,
            True,
            False,
            False,
            False,
        ),
        FailureAttributionCategory.EVALUATION_INFRASTRUCTURE: (
            FailureAttributionAction.RERUN_EVALUATION,
            False,
            True,
            True,
            False,
        ),
        FailureAttributionCategory.ENVIRONMENT_INCOMPATIBLE: (
            FailureAttributionAction.REBUILD_ENVIRONMENT,
            False,
            True,
            True,
            False,
        ),
        FailureAttributionCategory.FLAKY_EVIDENCE: (
            FailureAttributionAction.RERUN_EVALUATION,
            False,
            True,
            True,
            False,
        ),
        FailureAttributionCategory.EVIDENCE_INCOMPLETE: (
            FailureAttributionAction.RERUN_EVALUATION,
            False,
            True,
            True,
            False,
        ),
    }[receipt.category]
    actual = (
        receipt.action,
        receipt.candidate_fault,
        receipt.retryable,
        receipt.requires_rerun,
        receipt.reflection_eligible,
    )
    if actual != expected:
        raise ValueError("Failure attribution category 与 action flags 不一致。")


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_failure_attributions (
            comparison_id TEXT PRIMARY KEY,
            attribution_id TEXT NOT NULL UNIQUE,
            attribution_sha256 TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _from_row(row: aiosqlite.Row) -> EvolutionFailureAttributionReceipt:
    receipt = EvolutionFailureAttributionReceipt.model_validate_json(
        str(row["receipt_json"])
    )
    if not (
        str(row["comparison_id"]) == receipt.comparison_id
        and str(row["attribution_id"]) == receipt.attribution_id
        and str(row["attribution_sha256"]) == receipt.attribution_sha256
        and str(row["created_at"]) == receipt.created_at
    ):
        raise ValueError("Failure Attribution row 与 receipt 不一致。")
    return receipt


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_payload(value: object) -> str:
    return hashlib.sha256(_json_dumps(value).encode()).hexdigest()


def _sample_set_sha256(result_digests: tuple[str, ...]) -> str:
    return _sha256_payload([
        {"sample_index": index, "result_sha256": digest}
        for index, digest in enumerate(result_digests)
    ])


__all__ = [
    "EvolutionFailureAttributionBuilder",
    "EvolutionFailureAttributionError",
    "EvolutionFailureAttributionExecutor",
    "EvolutionFailureAttributionReceipt",
    "EvolutionFailureAttributionStore",
    "FailureAttributionAction",
    "FailureAttributionCategory",
]
