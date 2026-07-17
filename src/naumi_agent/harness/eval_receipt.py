"""Immutable, evidence-bound receipts for repeated Harness Eval comparisons."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.eval_models import HarnessEvalSuiteResult
from naumi_agent.harness.eval_policy import EvalPolicyVerdict, evaluate_eval_policy
from naumi_agent.harness.eval_statistics import (
    EvalStatisticalVerdict,
    compare_eval_repetitions,
)
from naumi_agent.harness.eval_suite_compare import EvalMechanicalVerdict

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalComparisonDecision(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    FLAKY = "flaky"
    INCONCLUSIVE = "inconclusive"
    INCOMPATIBLE = "incompatible"


class EvalReceiptSample(_StrictModel):
    """One ordered stored sample and the digest of its exact persisted result."""

    sample_index: int = Field(ge=0, le=9_999)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: HarnessEvalSuiteResult

    @model_validator(mode="after")
    def _digest_matches_result(self) -> EvalReceiptSample:
        if self.result_sha256 != eval_result_sha256(self.result):
            raise ValueError("Eval sample digest 与 Result 内容不一致。")
        return self


class EvalSampleComparisonEvidence(_StrictModel):
    sample_index: int = Field(ge=0, le=9_999)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mechanical_verdict: EvalMechanicalVerdict
    mechanical_code: str = ""
    policy_verdict: EvalPolicyVerdict
    policy_code: str = ""
    violation_codes: tuple[str, ...] = ()


class HarnessEvalComparisonReceipt(_StrictModel):
    """Tamper-evident authority consumed by later Harness and evolution gates."""

    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4_096)
    suite_id: str = Field(min_length=1, max_length=64)
    baseline_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_batch_id: str = Field(min_length=1, max_length=128)
    current_batch_id: str = Field(min_length=1, max_length=128)
    baseline_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_samples: int = Field(ge=1, le=10_000)
    current_samples: int = Field(ge=1, le=10_000)
    baseline_samples_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_samples_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistical_verdict: EvalStatisticalVerdict
    statistical_code: str = ""
    sample_evidence: tuple[EvalSampleComparisonEvidence, ...]
    decision: EvalComparisonDecision
    created_at: str
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("baseline_batch_id", "current_batch_id")
    @classmethod
    def _valid_batch_id(cls, value: str) -> str:
        if not _BATCH_ID_RE.fullmatch(value):
            raise ValueError("Comparison receipt batch_id 格式无效。")
        return value

    @field_validator("created_at")
    @classmethod
    def _valid_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("created_at 必须是 ISO 8601 时间。") from exc
        if parsed.tzinfo is None:
            raise ValueError("created_at 必须包含时区。")
        return value

    @model_validator(mode="after")
    def _validate_immutable_envelope(self) -> HarnessEvalComparisonReceipt:
        expected_id = eval_comparison_receipt_id(
            self.workspace_root,
            self.suite_id,
            self.baseline_id,
            self.current_batch_id,
        )
        if self.id != expected_id:
            raise ValueError("Comparison receipt immutable key 与内容不一致。")
        if len(self.sample_evidence) != self.current_samples:
            raise ValueError("Comparison receipt evidence 数量与样本数不一致。")
        indexes = [item.sample_index for item in self.sample_evidence]
        if indexes != list(range(self.current_samples)):
            raise ValueError("Comparison receipt evidence 必须按连续 sample_index 排序。")
        if self.decision is not _aggregate_decision(
            self.statistical_verdict,
            self.sample_evidence,
        ):
            raise ValueError("Comparison receipt decision 与证据聚合结果不一致。")
        if self.receipt_sha256 != _receipt_digest(self):
            raise ValueError("Comparison receipt digest 与内容不一致。")
        return self


def build_eval_comparison_receipt(
    *,
    workspace_root: str | Path,
    suite_id: str,
    baseline_id: str,
    baseline_batch_id: str,
    baseline_samples_sha256: str,
    baseline_samples: Sequence[EvalReceiptSample],
    current_batch_id: str,
    current_samples: Sequence[EvalReceiptSample],
    created_at: str,
) -> HarnessEvalComparisonReceipt:
    """Build one authoritative receipt from two exact, ordered stored cohorts."""
    workspace = str(Path(workspace_root).expanduser().resolve())
    normalized_suite = suite_id.strip()
    if not normalized_suite or len(normalized_suite) > 64:
        raise ValueError("suite_id 不能为空且不能超过 64 字符。")
    if not _SHA256_RE.fullmatch(baseline_id):
        raise ValueError("baseline_id 必须是 SHA-256。")
    if not _BATCH_ID_RE.fullmatch(baseline_batch_id):
        raise ValueError("baseline_batch_id 格式无效。")
    if not _BATCH_ID_RE.fullmatch(current_batch_id):
        raise ValueError("current_batch_id 格式无效。")
    baseline = _validate_cohort(baseline_samples, name="Baseline")
    current = _validate_cohort(current_samples, name="Candidate")
    actual_baseline_digest = eval_sample_set_sha256(baseline)
    if baseline_samples_sha256 != actual_baseline_digest:
        raise ValueError("Baseline samples digest 与已晋升版本不一致。")
    if baseline[0].result.suite_id != normalized_suite:
        raise ValueError("Baseline suite_id 与 receipt 不一致。")
    if current[0].result.suite_id != normalized_suite:
        raise ValueError("Candidate suite_id 与 receipt 不一致。")

    statistical = compare_eval_repetitions(
        tuple(item.result for item in baseline),
        tuple(item.result for item in current),
    )
    evidence = tuple(
        _sample_evidence(baseline[0].result, item)
        for item in current
    )
    decision = _aggregate_decision(statistical.verdict, evidence)
    current_digest = eval_sample_set_sha256(current)
    receipt_id = eval_comparison_receipt_id(
        workspace,
        normalized_suite,
        baseline_id,
        current_batch_id,
    )
    payload = {
        "schema_version": 1,
        "id": receipt_id,
        "workspace_root": workspace,
        "suite_id": normalized_suite,
        "baseline_id": baseline_id,
        "baseline_batch_id": baseline_batch_id,
        "current_batch_id": current_batch_id,
        "baseline_identity_sha256": _cohort_identity(baseline),
        "current_identity_sha256": _cohort_identity(current),
        "baseline_samples": len(baseline),
        "current_samples": len(current),
        "baseline_samples_sha256": actual_baseline_digest,
        "current_samples_sha256": current_digest,
        "statistical_verdict": statistical.verdict.value,
        "statistical_code": statistical.code,
        "sample_evidence": [item.model_dump(mode="json") for item in evidence],
        "decision": decision.value,
        "created_at": created_at,
    }
    return HarnessEvalComparisonReceipt(
        **payload,
        receipt_sha256=_sha256(_json_dumps(payload)),
    )


def eval_result_sha256(result: HarnessEvalSuiteResult) -> str:
    return _sha256(_json_dumps(result.model_dump(mode="json")))


def eval_sample_set_sha256(samples: Sequence[EvalReceiptSample]) -> str:
    return _sha256(
        _json_dumps(
            [
                {
                    "sample_index": item.sample_index,
                    "result_sha256": item.result_sha256,
                }
                for item in samples
            ]
        )
    )


def eval_comparison_receipt_id(
    workspace_root: str,
    suite_id: str,
    baseline_id: str,
    current_batch_id: str,
) -> str:
    return _sha256("\x00".join((workspace_root, suite_id, baseline_id, current_batch_id)))


def _validate_cohort(
    samples: Sequence[EvalReceiptSample],
    *,
    name: str,
) -> tuple[EvalReceiptSample, ...]:
    cohort = tuple(samples)
    if not cohort:
        raise ValueError(f"{name} cohort 不能为空。")
    if [item.sample_index for item in cohort] != list(range(len(cohort))):
        raise ValueError(f"{name} sample_index 必须从 0 连续递增。")
    identities = {
        item.result.baseline_identity.identity_sha256
        if item.result.baseline_identity is not None
        else ""
        for item in cohort
    }
    if "" in identities or len(identities) != 1:
        raise ValueError(f"{name} cohort 必须具有统一 Identity。")
    return cohort


def _cohort_identity(samples: tuple[EvalReceiptSample, ...]) -> str:
    identity = samples[0].result.baseline_identity
    assert identity is not None
    return identity.identity_sha256


def _sample_evidence(
    baseline: HarnessEvalSuiteResult,
    sample: EvalReceiptSample,
) -> EvalSampleComparisonEvidence:
    policy = evaluate_eval_policy(baseline, sample.result)
    return EvalSampleComparisonEvidence(
        sample_index=sample.sample_index,
        result_sha256=sample.result_sha256,
        mechanical_verdict=policy.mechanical.verdict.value,
        mechanical_code=policy.mechanical.code,
        policy_verdict=policy.verdict.value,
        policy_code=policy.code,
        violation_codes=tuple(sorted(item.code for item in policy.violations)),
    )


def _aggregate_decision(
    statistical: EvalStatisticalVerdict,
    evidence: tuple[EvalSampleComparisonEvidence, ...],
) -> EvalComparisonDecision:
    policy_verdicts = {item.policy_verdict for item in evidence}
    if (
        statistical is EvalStatisticalVerdict.INCOMPATIBLE
        or EvalPolicyVerdict.INCOMPATIBLE in policy_verdicts
    ):
        return EvalComparisonDecision.INCOMPATIBLE
    if (
        statistical is EvalStatisticalVerdict.INCONCLUSIVE
        or EvalPolicyVerdict.INCONCLUSIVE in policy_verdicts
    ):
        return EvalComparisonDecision.INCONCLUSIVE
    if EvalPolicyVerdict.FAILED in policy_verdicts:
        return EvalComparisonDecision.FAILED
    if statistical is EvalStatisticalVerdict.FLAKY:
        return EvalComparisonDecision.FLAKY
    return EvalComparisonDecision.PASSED


def _receipt_digest(receipt: HarnessEvalComparisonReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_sha256"})
    return _sha256(_json_dumps(payload))


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
