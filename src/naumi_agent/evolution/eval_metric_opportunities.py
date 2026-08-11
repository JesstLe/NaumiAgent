"""Discover review-only Evolution Candidates from authoritative H5c regressions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.evolution.candidate import EvolutionCandidateDraft, build_candidate_draft
from naumi_agent.evolution.evidence import (
    EvolutionEvidence,
    EvolutionEvidenceRef,
    EvolutionQuantitativeMetric,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    HarnessEvalComparisonReceipt,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.eval_statistics import (
    EvalMeanDifference,
    EvalStatisticalComparison,
    EvalStatisticalVerdict,
    compare_eval_repetitions,
)
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoredEvalResult,
    HarnessStoreError,
)

EVOLUTION_EVAL_METRIC_OPPORTUNITY_POLICY = "evolution-eval-metric-opportunity-v1"
_COMPARISON_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_URI_RE = re.compile(
    r"^harness://eval-comparison/([0-9a-f]{64})/metric/([0-9a-f]{24})$"
)
_MAX_REGRESSIONS = 128


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionEvalMetricCandidateResult(_StrictModel):
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    occurrence_count: int = Field(ge=1, le=10_000)
    evidence_id: str = Field(pattern=r"^eve_[0-9a-f]{24}$")
    finding_code: Literal[
        "eval_latency_regression",
        "eval_cost_regression",
        "eval_token_regression",
        "eval_metric_regression",
    ]
    metric: EvolutionQuantitativeMetric


class EvolutionEvalMetricOpportunityResult(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-eval-metric-opportunity-v1"] = (
        EVOLUTION_EVAL_METRIC_OPPORTUNITY_POLICY
    )
    status: Literal["recorded"] = "recorded"
    comparison_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparison_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[EvolutionEvalMetricCandidateResult, ...] = Field(
        min_length=1,
        max_length=_MAX_REGRESSIONS,
    )
    source_authority_valid: Literal[True] = True
    experiment_eligible: Literal[False] = False
    promotion_authority: Literal[False] = False


class EvolutionEvalMetricOpportunityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _AuthoritativeComparison:
    stored: HarnessStoredEvalComparisonReceipt
    receipt: HarnessEvalComparisonReceipt
    statistics: EvalStatisticalComparison


class EvolutionEvalMetricOpportunityService:
    """Recompute H5c and persist one Candidate for every primary metric regression."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        candidate_store: EvolutionCandidateStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.candidate_store = candidate_store

    async def discover(
        self,
        *,
        comparison_id: str,
    ) -> EvolutionEvalMetricOpportunityResult:
        authoritative = await self._load_authoritative(comparison_id)
        evidence = _adapt_regressions(authoritative)
        if not evidence:
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_regression_absent",
                "H5c Comparison 没有经 95% 置信区间确认的主定量指标回归。",
            )
        results: list[EvolutionEvalMetricCandidateResult] = []
        for item in evidence:
            stored = await self.candidate_store.upsert_candidate(
                self.workspace_root,
                build_candidate_draft((item,)),
            )
            assert item.quantitative_metric is not None
            results.append(EvolutionEvalMetricCandidateResult(
                candidate_id=stored.draft.candidate_id,
                candidate_revision=stored.revision,
                occurrence_count=stored.draft.occurrence_count,
                evidence_id=item.evidence_id,
                finding_code=item.finding_code,
                metric=item.quantitative_metric,
            ))
        return EvolutionEvalMetricOpportunityResult(
            comparison_id=authoritative.receipt.id,
            comparison_sha256=authoritative.receipt.receipt_sha256,
            candidates=tuple(results),
        )

    async def validate_candidate_sources(
        self,
        candidate: EvolutionCandidateDraft,
    ) -> bool:
        """Reload and exactly reproduce every H5c-backed Evidence observation."""
        selected = tuple(
            item
            for item in candidate.evidence
            if item.source_kind == "eval_metric_regression"
        )
        if not selected:
            return True
        by_comparison: dict[str, list[EvolutionEvidence]] = {}
        for item in selected:
            match = _SOURCE_URI_RE.fullmatch(item.source_uri)
            if match is None:
                return False
            by_comparison.setdefault(match.group(1), []).append(item)
        for comparison_id, expected in by_comparison.items():
            try:
                authoritative = await self._load_authoritative(comparison_id)
                actual = {item.evidence_id: item for item in _adapt_regressions(authoritative)}
            except (
                EvolutionEvalMetricOpportunityError,
                HarnessStoreError,
                OSError,
                TypeError,
                ValueError,
            ):
                return False
            if any(actual.get(item.evidence_id) != item for item in expected):
                return False
        return True

    async def _load_authoritative(
        self,
        comparison_id: str,
    ) -> _AuthoritativeComparison:
        normalized = str(comparison_id or "").strip()
        if _COMPARISON_ID_RE.fullmatch(normalized) is None:
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_comparison_id_invalid",
                "H5c Comparison ID 必须是完整 SHA-256。",
            )
        stored = await self.harness_store.get_eval_comparison_receipt_by_id(
            self.workspace_root,
            normalized,
        )
        if stored is None:
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_comparison_unavailable",
                "当前工作区不存在该 H5c Comparison，或其存储不可读。",
            )
        receipt = stored.receipt
        baseline_record = await self.harness_store.get_eval_baseline_by_batch(
            self.workspace_root,
            receipt.suite_id,
            receipt.baseline_batch_id,
        )
        if (
            baseline_record is None
            or baseline_record.id != receipt.baseline_id
            or baseline_record.samples_sha256 != receipt.baseline_samples_sha256
            or baseline_record.sample_count != receipt.baseline_samples
        ):
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_baseline_authority_invalid",
                "H5c Baseline authority 不存在或已与回执分离。",
            )
        baseline_rows = await self.harness_store.list_eval_results(
            self.workspace_root,
            receipt.baseline_batch_id,
            receipt.suite_id,
            limit=10_000,
        )
        current_rows = await self.harness_store.list_eval_results(
            self.workspace_root,
            receipt.current_batch_id,
            receipt.suite_id,
            limit=10_000,
        )
        if (
            len(baseline_rows) != receipt.baseline_samples
            or len(current_rows) != receipt.current_samples
        ):
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_cohort_authority_invalid",
                "H5c 的 Baseline 或 Current H5a cohort 不完整。",
            )
        baseline = tuple(_receipt_sample(item) for item in baseline_rows)
        current = tuple(_receipt_sample(item) for item in current_rows)
        try:
            rebuilt = build_eval_comparison_receipt(
                workspace_root=self.workspace_root,
                suite_id=receipt.suite_id,
                baseline_id=receipt.baseline_id,
                baseline_batch_id=receipt.baseline_batch_id,
                baseline_samples_sha256=receipt.baseline_samples_sha256,
                baseline_samples=baseline,
                current_batch_id=receipt.current_batch_id,
                current_samples=current,
                created_at=receipt.created_at,
            )
        except (TypeError, ValueError) as exc:
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_receipt_rebuild_failed",
                "H5c Comparison 无法从当前 H5a authority 重建。",
            ) from exc
        if rebuilt != receipt or stored.receipt_sha256 != receipt.receipt_sha256:
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_receipt_authority_invalid",
                "H5c Comparison 与重建后的 H5a 证据不一致。",
            )
        statistics = compare_eval_repetitions(
            tuple(item.result for item in baseline),
            tuple(item.result for item in current),
        )
        if statistics.verdict is not EvalStatisticalVerdict.REGRESSED:
            raise EvolutionEvalMetricOpportunityError(
                "eval_metric_comparison_not_regressed",
                "H5c Comparison 的统计结论不是 regressed。",
            )
        return _AuthoritativeComparison(
            stored=stored,
            receipt=receipt,
            statistics=statistics,
        )


def _receipt_sample(row: HarnessStoredEvalResult) -> EvalReceiptSample:
    return EvalReceiptSample(
        sample_index=row.sample_index,
        result_sha256=row.result_sha256,
        result=row.result,
    )


def _adapt_regressions(
    authoritative: _AuthoritativeComparison,
) -> tuple[EvolutionEvidence, ...]:
    differences = tuple(
        item
        for item in authoritative.statistics.differences
        if item.primary and item.case_id is not None and _is_regression(item)
    )
    if len(differences) > _MAX_REGRESSIONS:
        raise EvolutionEvalMetricOpportunityError(
            "eval_metric_regression_limit_exceeded",
            "单个 H5c Comparison 的主指标回归超过 128 个，拒绝部分摄取。",
        )
    return tuple(
        _adapt_metric(authoritative.receipt, item)
        for item in sorted(differences, key=lambda value: value.metric)
    )


def _is_regression(item: EvalMeanDifference) -> bool:
    return (
        item.confidence_low > 0
        if item.direction == "decrease"
        else item.confidence_high < 0
    )


def _adapt_metric(
    receipt: HarnessEvalComparisonReceipt,
    difference: EvalMeanDifference,
) -> EvolutionEvidence:
    assert difference.case_id is not None
    if difference.target is None:
        raise EvolutionEvalMetricOpportunityError(
            "eval_metric_target_missing",
            "H5c 主定量指标缺少机械 target。",
        )
    metric_name = difference.metric.removeprefix(f"case:{difference.case_id}:")
    finding_code = _finding_code(difference.unit)
    metric_contract = {
        "case_id": difference.case_id,
        "direction": difference.direction,
        "metric": metric_name,
        "target": difference.target,
        "unit": difference.unit,
    }
    metric_fingerprint = _digest(metric_contract)
    suite_fingerprint = _digest({"suite_id": receipt.suite_id})
    case_fingerprint = _digest({"case_id": difference.case_id})
    scope = (
        f"harness:eval:{suite_fingerprint[:16]}:"
        f"{case_fingerprint[:16]}:{metric_fingerprint[:16]}"
    )
    root_fingerprint = _digest({
        "baseline_id": receipt.baseline_id,
        "finding_code": finding_code,
        "metric_contract": metric_contract,
        "scope": scope,
        "suite_fingerprint": suite_fingerprint,
    })
    metric = EvolutionQuantitativeMetric(
        name=metric_name,
        case_fingerprint=case_fingerprint,
        unit=difference.unit,
        direction=difference.direction,
        target=difference.target,
        baseline_mean=difference.baseline_mean,
        current_mean=difference.current_mean,
        delta=difference.delta,
        confidence_low=difference.confidence_low,
        confidence_high=difference.confidence_high,
    )
    source_uri = (
        f"harness://eval-comparison/{receipt.id}/metric/{metric_fingerprint[:24]}"
    )
    refs = (
        EvolutionEvidenceRef(uri=source_uri, sha256=receipt.receipt_sha256),
        EvolutionEvidenceRef(
            uri=f"harness://eval-cohort/baseline/{receipt.baseline_samples_sha256}",
            sha256=receipt.baseline_samples_sha256,
        ),
        EvolutionEvidenceRef(
            uri=f"harness://eval-cohort/current/{receipt.current_samples_sha256}",
            sha256=receipt.current_samples_sha256,
        ),
    )
    evidence_sha = _digest({
        "comparison_id": receipt.id,
        "comparison_sha256": receipt.receipt_sha256,
        "metric": metric.model_dump(mode="json"),
        "root_fingerprint": root_fingerprint,
    })
    return EvolutionEvidence(
        schema_version=2,
        evidence_id=f"eve_{evidence_sha[:24]}",
        source_kind="eval_metric_regression",
        source_uri=source_uri,
        observed_at=receipt.created_at,
        finding_code=finding_code,
        scope=scope,
        root_fingerprint=root_fingerprint,
        refs=refs,
        quantitative_metric=metric,
    )


def _finding_code(unit: str) -> Literal[
    "eval_latency_regression",
    "eval_cost_regression",
    "eval_token_regression",
    "eval_metric_regression",
]:
    if unit == "milliseconds":
        return "eval_latency_regression"
    if unit == "usd":
        return "eval_cost_regression"
    if unit == "tokens":
        return "eval_token_regression"
    return "eval_metric_regression"


def render_eval_metric_opportunity(
    result: EvolutionEvalMetricOpportunityResult,
) -> str:
    lines = [
        "# H5c 定量回归已进入机会发现",
        "",
        f"- Comparison：`{result.comparison_id}`",
        f"- 已记录 Candidate：{len(result.candidates)}",
        "- 来源 authority：已从 H5a cohorts 完整重建并实时重验",
        "- 状态：不可执行；未授予实验或推广权限",
        "",
    ]
    for item in result.candidates:
        metric = item.metric
        lines.extend([
            f"## `{metric.name}`",
            f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
            f"- 均值：{metric.baseline_mean:g} → {metric.current_mean:g} {metric.unit}",
            (
                f"- 差值 95% CI：{metric.delta:g} "
                f"[{metric.confidence_low:g}, {metric.confidence_high:g}]"
            ),
            f"- 期望：{metric.direction} 至 {metric.target:g}",
            f"- 下一步：`/evolution detail {item.candidate_id}`",
            "",
        ])
    return "\n".join(lines).rstrip()


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "EVOLUTION_EVAL_METRIC_OPPORTUNITY_POLICY",
    "EvolutionEvalMetricCandidateResult",
    "EvolutionEvalMetricOpportunityError",
    "EvolutionEvalMetricOpportunityResult",
    "EvolutionEvalMetricOpportunityService",
    "render_eval_metric_opportunity",
]
