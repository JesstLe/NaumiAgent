"""Durable, non-final evaluation receipts for one verified RED/GREEN lane."""

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

from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionReceipt,
    EvolutionFailureAttributionStore,
    FailureAttributionAction,
    FailureAttributionCategory,
)
from naumi_agent.harness.eval_models import EvalRunStatus
from naumi_agent.harness.eval_receipt import (
    EvalComparisonDecision,
    HarnessEvalComparisonReceipt,
)
from naumi_agent.harness.eval_statistics import EvalStatisticalVerdict
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoredEvalResult,
    HarnessStoreError,
)

EVALUATION_LANE_RECEIPT_POLICY = "evolution-evaluation-lane-receipt-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_ARTIFACT_KINDS = (
    "red_completion",
    "green_completion",
    "baseline_samples",
    "candidate_samples",
    "comparison",
    "failure_attribution",
)


class EvaluationLaneKind(StrEnum):
    SELF_REVIEW = "self_review"
    INTERVENTIONAL = "interventional"
    ADVERSARIAL = "adversarial"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionEvaluationArtifactRef(_StrictModel):
    order: int = Field(ge=1, le=6)
    kind: Literal[
        "red_completion",
        "green_completion",
        "baseline_samples",
        "candidate_samples",
        "comparison",
        "failure_attribution",
    ]
    artifact_id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=_SHA256_RE)


class EvolutionEvaluationCohortSummary(_StrictModel):
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    samples: int = Field(ge=1, le=10_000)
    samples_sha256: str = Field(pattern=_SHA256_RE)
    passed_samples: int = Field(ge=0, le=10_000)
    failed_samples: int = Field(ge=0, le=10_000)
    evaluation_error_samples: int = Field(ge=0, le=10_000)
    passed_cases: int = Field(ge=0, le=1_000_000)
    implementation_failures: int = Field(ge=0, le=1_000_000)
    evaluation_errors: int = Field(ge=0, le=1_000_000)
    skipped_cases: int = Field(ge=0, le=1_000_000)
    duration_ms: float = Field(ge=0)
    observed_tokens: float | None = Field(default=None, ge=0)
    token_samples: int = Field(ge=0, le=10_000)
    observed_cost_usd: float | None = Field(default=None, ge=0)
    cost_samples: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def _counts_are_complete(self) -> Self:
        if (
            self.passed_samples + self.failed_samples + self.evaluation_error_samples
            != self.samples
        ):
            raise ValueError("Evaluation cohort sample 状态计数不完整。")
        if (self.observed_tokens is None) != (self.token_samples == 0):
            raise ValueError("Evaluation cohort token evidence 状态不一致。")
        if (self.observed_cost_usd is None) != (self.cost_samples == 0):
            raise ValueError("Evaluation cohort cost evidence 状态不一致。")
        if self.token_samples > self.samples or self.cost_samples > self.samples:
            raise ValueError("Evaluation cohort resource coverage 越界。")
        return self


class EvolutionEvaluationLaneReceipt(_StrictModel):
    """One verified lane result that cannot masquerade as candidate completion."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-evaluation-lane-receipt-v1"] = EVALUATION_LANE_RECEIPT_POLICY
    receipt_id: str = Field(pattern=r"^evlane_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    lane_kind: EvaluationLaneKind
    workspace_root: str = Field(min_length=1, max_length=4_096)
    platform: Literal["linux", "macos", "windows", "unknown"]
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    red_completion_id: str = Field(pattern=r"^(?:evvred(?:run|cohort)|evadvcohort)_[0-9a-f]{24}$")
    red_completion_sha256: str = Field(pattern=_SHA256_RE)
    green_completion_id: str = Field(
        pattern=r"^(?:evvgreen(?:run|cohort)|evadvcohort)_[0-9a-f]{24}$"
    )
    green_completion_sha256: str = Field(pattern=_SHA256_RE)
    comparison_id: str = Field(pattern=_SHA256_RE)
    comparison_receipt_sha256: str = Field(pattern=_SHA256_RE)
    comparison_decision: EvalComparisonDecision
    statistical_verdict: EvalStatisticalVerdict
    statistical_code: str = Field(max_length=128)
    attribution_id: str = Field(pattern=r"^evattr_[0-9a-f]{24}$")
    attribution_sha256: str = Field(pattern=_SHA256_RE)
    failure_category: FailureAttributionCategory
    failure_reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    failure_action: FailureAttributionAction
    candidate_fault: bool
    retryable: bool
    requires_rerun: bool
    reflection_eligible: bool
    baseline: EvolutionEvaluationCohortSummary
    candidate: EvolutionEvaluationCohortSummary
    artifacts: tuple[EvolutionEvaluationArtifactRef, ...] = Field(
        min_length=6,
        max_length=6,
    )
    comparison_receipt: HarnessEvalComparisonReceipt
    failure_attribution_receipt: EvolutionFailureAttributionReceipt
    evidence_first_at: str
    evidence_last_at: str
    candidate_evaluation_complete: Literal[False] = False
    aggregation_required: Literal[True] = True
    created_at: str

    @field_validator("evidence_first_at", "evidence_last_at", "created_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Evaluation Lane Receipt 时间必须包含时区。")
        return parsed.isoformat()

    @model_validator(mode="after")
    def _tamper_evident_and_non_final(self) -> Self:
        if tuple(item.order for item in self.artifacts) != tuple(range(1, 7)):
            raise ValueError("Evaluation artifacts 必须按连续顺序排列。")
        if tuple(item.kind for item in self.artifacts) != _ARTIFACT_KINDS:
            raise ValueError("Evaluation artifacts 类型或顺序不完整。")
        expected_refs = (
            (
                self.artifacts[0],
                self.red_completion_id,
                self.red_completion_sha256,
            ),
            (
                self.artifacts[1],
                self.green_completion_id,
                self.green_completion_sha256,
            ),
            (self.artifacts[2], self.baseline.batch_id, self.baseline.samples_sha256),
            (self.artifacts[3], self.candidate.batch_id, self.candidate.samples_sha256),
            (self.artifacts[4], self.comparison_id, self.comparison_receipt_sha256),
            (self.artifacts[5], self.attribution_id, self.attribution_sha256),
        )
        if any(
            item.artifact_id != artifact_id or item.sha256 != digest
            for item, artifact_id, digest in expected_refs
        ):
            raise ValueError("Evaluation artifacts 与权威摘要不一致。")
        try:
            inferred_kind = _lane_kind(
                self.red_completion_id,
                self.green_completion_id,
            )
        except EvolutionEvaluationLaneReceiptError as exc:
            raise ValueError("Evaluation completion receipt lane 类型无效。") from exc
        if inferred_kind is not self.lane_kind:
            raise ValueError("Evaluation completion receipt 与 lane kind 不一致。")
        h5c = self.comparison_receipt
        failure = self.failure_attribution_receipt
        if not (
            h5c.workspace_root == self.workspace_root
            and h5c.suite_id == self.suite_id
            and h5c.id == self.comparison_id == failure.comparison_id
            and h5c.receipt_sha256
            == self.comparison_receipt_sha256
            == failure.comparison_receipt_sha256
            and h5c.decision is self.comparison_decision
            and h5c.statistical_verdict is self.statistical_verdict
            and h5c.statistical_code == self.statistical_code
            and h5c.baseline_batch_id == self.baseline.batch_id
            and h5c.current_batch_id == self.candidate.batch_id
            and h5c.baseline_identity_sha256 == self.baseline.identity_sha256
            and h5c.current_identity_sha256 == self.candidate.identity_sha256
            and h5c.baseline_samples == self.baseline.samples
            and h5c.current_samples == self.candidate.samples
            and h5c.baseline_samples_sha256 == self.baseline.samples_sha256
            and h5c.current_samples_sha256 == self.candidate.samples_sha256
            and failure.attribution_id == self.attribution_id
            and failure.attribution_sha256 == self.attribution_sha256
            and failure.validation_plan_id == self.validation_plan_id
            and failure.validation_plan_sha256 == self.validation_plan_sha256
            and failure.candidate_id == self.candidate_id
            and failure.candidate_revision == self.candidate_revision
            and failure.red_receipt_id == self.red_completion_id
            and failure.red_receipt_sha256 == self.red_completion_sha256
            and failure.green_receipt_id == self.green_completion_id
            and failure.green_receipt_sha256 == self.green_completion_sha256
            and failure.category is self.failure_category
            and failure.reason_code == self.failure_reason_code
            and failure.action is self.failure_action
            and failure.candidate_fault == self.candidate_fault
            and failure.retryable == self.retryable
            and failure.requires_rerun == self.requires_rerun
            and failure.reflection_eligible == self.reflection_eligible
        ):
            raise ValueError("Evaluation Lane 投影与 typed source receipts 不一致。")
        if datetime.fromisoformat(self.evidence_first_at) > datetime.fromisoformat(
            self.evidence_last_at
        ):
            raise ValueError("Evaluation evidence 时间范围倒置。")
        if self.created_at != self.evidence_last_at:
            raise ValueError("Evaluation Lane Receipt created_at 必须等于最新证据时间。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Evaluation Lane Receipt digest 不一致。")
        if self.receipt_id != f"evlane_{expected[:24]}":
            raise ValueError("Evaluation Lane Receipt identity 不一致。")
        return self


class EvolutionEvaluationLaneReceiptError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionEvaluationLaneReceiptBuilder:
    def build(
        self,
        *,
        comparison: HarnessStoredEvalComparisonReceipt,
        attribution: EvolutionFailureAttributionReceipt,
        baseline_records: tuple[HarnessStoredEvalResult, ...],
        candidate_records: tuple[HarnessStoredEvalResult, ...],
    ) -> EvolutionEvaluationLaneReceipt:
        try:
            stored = HarnessStoredEvalComparisonReceipt(
                **{field: getattr(comparison, field) for field in comparison.__dataclass_fields__}
            )
            h5c = HarnessEvalComparisonReceipt.model_validate(
                stored.receipt.model_dump(mode="json")
            )
            failure = EvolutionFailureAttributionReceipt.model_validate(
                attribution.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_authority_invalid",
                "Evaluation Lane authority 无效或已被篡改。",
            ) from exc
        if not (
            stored.id == h5c.id == failure.comparison_id
            and stored.receipt_sha256 == h5c.receipt_sha256 == failure.comparison_receipt_sha256
            and stored.workspace_root == h5c.workspace_root
            and stored.suite_id == h5c.suite_id
            and stored.baseline_id == h5c.baseline_id
            and stored.current_batch_id == h5c.current_batch_id
            and stored.decision == h5c.decision.value
        ):
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_authority_mismatch",
                "H5c 与 Failure Attribution authority 不一致。",
            )
        kind = _lane_kind(failure.red_receipt_id, failure.green_receipt_id)
        baseline = _cohort_summary(
            baseline_records,
            batch_id=h5c.baseline_batch_id,
            suite_id=h5c.suite_id,
            expected_samples=h5c.baseline_samples,
            expected_identity=h5c.baseline_identity_sha256,
            expected_samples_sha256=h5c.baseline_samples_sha256,
        )
        candidate = _cohort_summary(
            candidate_records,
            batch_id=h5c.current_batch_id,
            suite_id=h5c.suite_id,
            expected_samples=h5c.current_samples,
            expected_identity=h5c.current_identity_sha256,
            expected_samples_sha256=h5c.current_samples_sha256,
        )
        if tuple(item.result_sha256 for item in h5c.sample_evidence) != tuple(
            item.result_sha256 for item in candidate_records
        ):
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_candidate_evidence_mismatch",
                "Candidate H5a evidence 与 H5c 逐样本摘要不一致。",
            )
        platforms = {
            item.result.baseline_identity.platform.system
            for item in (*baseline_records, *candidate_records)
            if item.result.baseline_identity is not None
        }
        if len(platforms) != 1:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_platform_mismatch",
                "Evaluation Lane H5a platform 不唯一。",
            )
        platform = next(iter(platforms))
        artifacts = tuple(
            EvolutionEvaluationArtifactRef(order=index, kind=kind_name, artifact_id=aid, sha256=sha)
            for index, (kind_name, aid, sha) in enumerate(
                (
                    ("red_completion", failure.red_receipt_id, failure.red_receipt_sha256),
                    ("green_completion", failure.green_receipt_id, failure.green_receipt_sha256),
                    ("baseline_samples", baseline.batch_id, baseline.samples_sha256),
                    ("candidate_samples", candidate.batch_id, candidate.samples_sha256),
                    ("comparison", h5c.id, h5c.receipt_sha256),
                    ("failure_attribution", failure.attribution_id, failure.attribution_sha256),
                ),
                start=1,
            )
        )
        timestamps = tuple(item.created_at for item in (*baseline_records, *candidate_records)) + (
            h5c.created_at,
            failure.created_at,
        )
        parsed_timestamps = tuple(datetime.fromisoformat(item) for item in timestamps)
        payload = {
            "schema_version": 1,
            "policy_version": EVALUATION_LANE_RECEIPT_POLICY,
            "lane_kind": kind.value,
            "workspace_root": h5c.workspace_root,
            "platform": platform,
            "validation_plan_id": failure.validation_plan_id,
            "validation_plan_sha256": failure.validation_plan_sha256,
            "candidate_id": failure.candidate_id,
            "candidate_revision": failure.candidate_revision,
            "suite_id": h5c.suite_id,
            "red_completion_id": failure.red_receipt_id,
            "red_completion_sha256": failure.red_receipt_sha256,
            "green_completion_id": failure.green_receipt_id,
            "green_completion_sha256": failure.green_receipt_sha256,
            "comparison_id": h5c.id,
            "comparison_receipt_sha256": h5c.receipt_sha256,
            "comparison_decision": h5c.decision.value,
            "statistical_verdict": h5c.statistical_verdict.value,
            "statistical_code": h5c.statistical_code,
            "attribution_id": failure.attribution_id,
            "attribution_sha256": failure.attribution_sha256,
            "failure_category": failure.category.value,
            "failure_reason_code": failure.reason_code,
            "failure_action": failure.action.value,
            "candidate_fault": failure.candidate_fault,
            "retryable": failure.retryable,
            "requires_rerun": failure.requires_rerun,
            "reflection_eligible": failure.reflection_eligible,
            "baseline": baseline.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "comparison_receipt": h5c.model_dump(mode="json"),
            "failure_attribution_receipt": failure.model_dump(mode="json"),
            "evidence_first_at": min(parsed_timestamps).isoformat(),
            "evidence_last_at": max(parsed_timestamps).isoformat(),
            "candidate_evaluation_complete": False,
            "aggregation_required": True,
            "created_at": max(parsed_timestamps).isoformat(),
        }
        digest = _sha256_payload(payload)
        return EvolutionEvaluationLaneReceipt.model_validate(
            {
                **payload,
                "receipt_id": f"evlane_{digest[:24]}",
                "receipt_sha256": digest,
            }
        )


class EvolutionEvaluationLaneReceiptStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        receipt: EvolutionEvaluationLaneReceipt,
    ) -> EvolutionEvaluationLaneReceipt:
        artifact = EvolutionEvaluationLaneReceipt.model_validate(receipt.model_dump(mode="json"))
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_evaluation_lane_receipts WHERE comparison_id = ?",
                        (artifact.comparison_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_row(row)
                    if restored != artifact:
                        await db.rollback()
                        raise EvolutionEvaluationLaneReceiptError(
                            "evaluation_lane_store_conflict",
                            "同一 H5c 不可覆盖为不同 Evaluation Lane Receipt。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_evaluation_lane_receipts "
                    "(comparison_id, receipt_id, receipt_sha256, receipt_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        artifact.comparison_id,
                        artifact.receipt_id,
                        artifact.receipt_sha256,
                        _json_dumps(artifact.model_dump(mode="json")),
                        artifact.created_at,
                    ),
                )
                await db.commit()
        except EvolutionEvaluationLaneReceiptError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_store_error",
                "Evaluation Lane Receipt 无法持久化。",
            ) from exc
        restored = await self.get(artifact.comparison_id)
        assert restored is not None
        return restored

    async def get(self, comparison_id: str) -> EvolutionEvaluationLaneReceipt | None:
        if not isinstance(comparison_id, str) or re.fullmatch(_SHA256_RE, comparison_id) is None:
            raise ValueError("comparison_id 必须是 SHA-256。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_evaluation_lane_receipts WHERE comparison_id = ?",
                        (comparison_id,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_store_corrupt",
                "Evaluation Lane Receipt 损坏或无法读取。",
            ) from exc


class EvolutionEvaluationLaneReceiptExecutor:
    def __init__(
        self,
        *,
        harness_store: HarnessStore,
        attribution_store: EvolutionFailureAttributionStore,
        receipt_store: EvolutionEvaluationLaneReceiptStore,
        builder: EvolutionEvaluationLaneReceiptBuilder | None = None,
    ) -> None:
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Evaluation Lane executor 需要 HarnessStore。")
        if not isinstance(attribution_store, EvolutionFailureAttributionStore):
            raise TypeError("Evaluation Lane executor 需要 Attribution Store。")
        if not isinstance(receipt_store, EvolutionEvaluationLaneReceiptStore):
            raise TypeError("Evaluation Lane executor 需要 Receipt Store。")
        self._harness_store = harness_store
        self._attribution_store = attribution_store
        self._receipt_store = receipt_store
        self._builder = builder or EvolutionEvaluationLaneReceiptBuilder()

    async def execute(
        self,
        *,
        comparison: HarnessStoredEvalComparisonReceipt,
        attribution: EvolutionFailureAttributionReceipt,
    ) -> EvolutionEvaluationLaneReceipt:
        try:
            h5c = await self._harness_store.get_eval_comparison_receipt(
                comparison.workspace_root,
                comparison.suite_id,
                comparison.baseline_id,
                comparison.current_batch_id,
            )
            failure = await self._attribution_store.get(attribution.comparison_id)
            if h5c is None or h5c != comparison:
                raise EvolutionEvaluationLaneReceiptError(
                    "evaluation_lane_comparison_not_authoritative",
                    "传入的 H5c 不是 Harness Store 当前不可变事实。",
                )
            if failure is None or failure != attribution:
                raise EvolutionEvaluationLaneReceiptError(
                    "evaluation_lane_attribution_not_authoritative",
                    "传入的 Failure Attribution 不是当前不可变事实。",
                )
            baseline_records = await self._harness_store.list_eval_results(
                h5c.workspace_root,
                h5c.receipt.baseline_batch_id,
                h5c.suite_id,
                limit=h5c.receipt.baseline_samples + 1,
            )
            candidate_records = await self._harness_store.list_eval_results(
                h5c.workspace_root,
                h5c.current_batch_id,
                h5c.suite_id,
                limit=h5c.receipt.current_samples + 1,
            )
        except EvolutionEvaluationLaneReceiptError:
            raise
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_evidence_read_failed",
                "无法读取 Evaluation Lane authority。",
            ) from exc
        receipt = self._builder.build(
            comparison=h5c,
            attribution=failure,
            baseline_records=baseline_records,
            candidate_records=candidate_records,
        )
        return await self._receipt_store.record(receipt)

    async def execute_by_id(
        self,
        *,
        workspace_root: str | Path,
        comparison_id: str,
    ) -> EvolutionEvaluationLaneReceipt:
        if not isinstance(comparison_id, str) or re.fullmatch(_SHA256_RE, comparison_id) is None:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_comparison_id_invalid",
                "comparison_id 必须是 SHA-256。",
            )
        try:
            comparison = await self._harness_store.get_eval_comparison_receipt_by_id(
                workspace_root,
                comparison_id,
            )
            attribution = await self._attribution_store.get(comparison_id)
        except (HarnessStoreError, OSError, ValueError) as exc:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_evidence_read_failed",
                "无法按 ID 读取 Evaluation Lane authority。",
            ) from exc
        if comparison is None:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_comparison_missing",
                "当前工作区不存在该 H5c Comparison。",
            )
        if attribution is None:
            raise EvolutionEvaluationLaneReceiptError(
                "evaluation_lane_attribution_missing",
                "该 H5c 尚无可信 Failure Attribution。",
            )
        return await self.execute(
            comparison=comparison,
            attribution=attribution,
        )


def render_evaluation_lane_receipt(
    receipt: EvolutionEvaluationLaneReceipt,
) -> str:
    artifact = EvolutionEvaluationLaneReceipt.model_validate(receipt.model_dump(mode="json"))
    before = artifact.baseline
    after = artifact.candidate
    lines = [
        f"# Evaluation Lane `{artifact.receipt_id}`",
        "",
        "**这是单 lane 证据，不是候选最终 Evaluation Receipt。**",
        "",
        f"- 类型/平台：`{artifact.lane_kind.value}` / `{artifact.platform}`",
        f"- Candidate：`{artifact.candidate_id}` · revision {artifact.candidate_revision}",
        f"- Suite：`{artifact.suite_id}`",
        (
            f"- 比较：`{artifact.comparison_decision.value}` / "
            f"`{artifact.statistical_verdict.value}`"
        ),
        (
            f"- 归因：`{artifact.failure_category.value}` / "
            f"`{artifact.failure_reason_code}` / `{artifact.failure_action.value}`"
        ),
        f"- 可进入 Reflection：{'是' if artifact.reflection_eligible else '否'}",
        "",
        "## Before / After",
        "",
        _render_cohort("RED", before),
        _render_cohort("GREEN", after),
        "",
        "## 资源证据",
        "",
        _render_resources("RED", before),
        _render_resources("GREEN", after),
        "",
        "## Authority",
        "",
        f"- H5c：`{artifact.comparison_id}` / `{artifact.comparison_receipt_sha256}`",
        f"- Attribution：`{artifact.attribution_id}` / `{artifact.attribution_sha256}`",
        f"- Artifacts：{len(artifact.artifacts)} 个完整摘要引用",
        "",
        "下一步：聚合 Interventional 与全部必需平台的 Adversarial lane 后，才可签发候选最终回执。",
    ]
    return "\n".join(lines)


def _render_cohort(label: str, summary: EvolutionEvaluationCohortSummary) -> str:
    return (
        f"- {label} `{summary.batch_id}`：samples {summary.samples} · "
        f"passed/failed/eval-error "
        f"{summary.passed_samples}/{summary.failed_samples}/"
        f"{summary.evaluation_error_samples} · {summary.duration_ms:.0f}ms"
    )


def _render_resources(label: str, summary: EvolutionEvaluationCohortSummary) -> str:
    tokens = (
        f"{summary.observed_tokens:g}（{summary.token_samples}/{summary.samples} samples）"
        if summary.observed_tokens is not None
        else "未观测"
    )
    cost = (
        f"${summary.observed_cost_usd:g}（{summary.cost_samples}/{summary.samples} samples）"
        if summary.observed_cost_usd is not None
        else "未观测"
    )
    return f"- {label}：tokens {tokens} · cost {cost}"


def _lane_kind(red_id: str, green_id: str) -> EvaluationLaneKind:
    if re.fullmatch(r"evvredrun_[0-9a-f]{24}", red_id) and re.fullmatch(
        r"evvgreenrun_[0-9a-f]{24}", green_id
    ):
        return EvaluationLaneKind.SELF_REVIEW
    if re.fullmatch(r"evvredcohort_[0-9a-f]{24}", red_id) and re.fullmatch(
        r"evvgreencohort_[0-9a-f]{24}", green_id
    ):
        return EvaluationLaneKind.INTERVENTIONAL
    if (
        re.fullmatch(r"evadvcohort_[0-9a-f]{24}", red_id)
        and re.fullmatch(r"evadvcohort_[0-9a-f]{24}", green_id)
        and red_id != green_id
    ):
        return EvaluationLaneKind.ADVERSARIAL
    raise EvolutionEvaluationLaneReceiptError(
        "evaluation_lane_kind_invalid",
        "RED/GREEN completion receipt 类型无法形成同一 Evaluation Lane。",
    )


def _cohort_summary(
    records: tuple[HarnessStoredEvalResult, ...],
    *,
    batch_id: str,
    suite_id: str,
    expected_samples: int,
    expected_identity: str,
    expected_samples_sha256: str,
) -> EvolutionEvaluationCohortSummary:
    digests = tuple(item.result_sha256 for item in records)
    if not (
        len(records) == expected_samples
        and tuple(item.sample_index for item in records) == tuple(range(expected_samples))
        and all(item.batch_id == batch_id and item.suite_id == suite_id for item in records)
        and all(item.identity_sha256 == expected_identity for item in records)
        and _sample_set_sha256(digests) == expected_samples_sha256
    ):
        raise EvolutionEvaluationLaneReceiptError(
            "evaluation_lane_cohort_mismatch",
            "H5a cohort 与 H5c sample authority 不一致。",
        )
    tokens, token_samples = _resource_total(records, "tokens")
    cost, cost_samples = _resource_total(records, "usd")
    return EvolutionEvaluationCohortSummary(
        batch_id=batch_id,
        identity_sha256=expected_identity,
        samples=expected_samples,
        samples_sha256=expected_samples_sha256,
        passed_samples=sum(item.result.status is EvalRunStatus.PASSED for item in records),
        failed_samples=sum(item.result.status is EvalRunStatus.FAILED for item in records),
        evaluation_error_samples=sum(
            item.result.status is EvalRunStatus.EVALUATION_ERROR for item in records
        ),
        passed_cases=sum(item.result.passed for item in records),
        implementation_failures=sum(item.result.implementation_failures for item in records),
        evaluation_errors=sum(item.result.evaluation_errors for item in records),
        skipped_cases=sum(item.result.skipped for item in records),
        duration_ms=sum(item.result.duration_ms for item in records),
        observed_tokens=tokens,
        token_samples=token_samples,
        observed_cost_usd=cost,
        cost_samples=cost_samples,
    )


def _resource_total(
    records: tuple[HarnessStoredEvalResult, ...],
    unit: Literal["tokens", "usd"],
) -> tuple[float | None, int]:
    sample_values = []
    for record in records:
        values = [
            observation.value
            for case in record.result.cases
            for observation in case.metric_observations
            if observation.unit == unit
        ]
        if values:
            sample_values.append(sum(values))
    return (sum(sample_values), len(sample_values)) if sample_values else (None, 0)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_evaluation_lane_receipts (
            comparison_id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL UNIQUE,
            receipt_sha256 TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _from_row(row: aiosqlite.Row) -> EvolutionEvaluationLaneReceipt:
    receipt = EvolutionEvaluationLaneReceipt.model_validate_json(str(row["receipt_json"]))
    if not (
        str(row["comparison_id"]) == receipt.comparison_id
        and str(row["receipt_id"]) == receipt.receipt_id
        and str(row["receipt_sha256"]) == receipt.receipt_sha256
        and str(row["created_at"]) == receipt.created_at
    ):
        raise ValueError("Evaluation Lane Receipt row 与 receipt 不一致。")
    return receipt


def _sample_set_sha256(result_digests: tuple[str, ...]) -> str:
    return _sha256_payload(
        [
            {"sample_index": index, "result_sha256": digest}
            for index, digest in enumerate(result_digests)
        ]
    )


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


__all__ = [
    "EvaluationLaneKind",
    "EvolutionEvaluationArtifactRef",
    "EvolutionEvaluationCohortSummary",
    "EvolutionEvaluationLaneReceipt",
    "EvolutionEvaluationLaneReceiptBuilder",
    "EvolutionEvaluationLaneReceiptError",
    "EvolutionEvaluationLaneReceiptExecutor",
    "EvolutionEvaluationLaneReceiptStore",
    "render_evaluation_lane_receipt",
]
