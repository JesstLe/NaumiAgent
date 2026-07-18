"""Strict contracts for deterministic Harness evaluation assets and results."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.eval_identity import HarnessEvalBaselineIdentity

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalCaseStatus(StrEnum):
    PASSED = "passed"
    IMPLEMENTATION_FAILURE = "implementation_failure"
    EVALUATION_ERROR = "evaluation_error"
    SKIPPED = "skipped"


class EvalRunStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    EVALUATION_ERROR = "evaluation_error"


class EvalGuardrailStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class HarnessEvalInput(_StrictModel):
    transport: Literal["jsonl"] = "jsonl"


class HarnessEvalFixture(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".json":
            raise ValueError("fixture.path 必须是 Suite 目录内的相对 JSON 路径")
        return normalized


class HarnessProtocolExpected(_StrictModel):
    outcome: Literal["accepted", "rejected"]
    error_code: str | None = Field(default=None, max_length=128)
    selected_version: int | None = Field(default=None, ge=1)
    capabilities: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("error_code")
    @classmethod
    def _validate_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _CAPABILITY_RE.fullmatch(normalized):
            raise ValueError("expected.error_code 格式无效")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CAPABILITY_RE.fullmatch(value) for value in values):
            raise ValueError("expected.capabilities 含无效能力名称")
        if len(values) != len(set(values)):
            raise ValueError("expected.capabilities 不能重复")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def _validate_outcome_fields(self) -> HarnessProtocolExpected:
        if self.outcome == "accepted":
            if self.selected_version is None or self.error_code is not None:
                raise ValueError(
                    "accepted expected 必须提供 selected_version 且不能提供 error_code"
                )
        elif not self.error_code or self.selected_version is not None or self.capabilities:
            raise ValueError("rejected expected 必须只提供 error_code")
        return self


class HarnessEvalMetrics(_StrictModel):
    primary: Literal["protocol_outcome_match"]
    guardrails: tuple[Literal["no_model", "no_side_effect"], ...] = ()

    @field_validator("guardrails")
    @classmethod
    def _unique_guardrails(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("metrics.guardrails 不能重复")
        return tuple(sorted(values))


class HarnessEvalCaseBudget(_StrictModel):
    max_duration_ms: int = Field(default=100, ge=1, le=5_000)


class HarnessEvalSuiteBudget(_StrictModel):
    max_duration_ms: int = Field(default=30_000, ge=1, le=60_000)


class HarnessEvalComparisonPolicy(_StrictModel):
    """Mechanical absolute and relative gates bound into the Suite identity."""

    min_pass_rate: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    max_regressions: int = Field(default=0, ge=0, le=500)
    max_implementation_failures: int = Field(default=0, ge=0, le=500)
    max_pass_rate_drop: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)

    def canonical_payload(self) -> dict[str, int | float]:
        return self.model_dump(mode="json")

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.canonical_payload())


class HarnessEvalCase(_StrictModel):
    id: str
    runner: Literal["protocol_hello"]
    input: HarnessEvalInput
    fixture: HarnessEvalFixture
    expected: HarnessProtocolExpected
    metrics: HarnessEvalMetrics
    budget: HarnessEvalCaseBudget = Field(default_factory=HarnessEvalCaseBudget)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("case id 格式无效")
        return normalized

    @model_validator(mode="after")
    def _runner_guardrails_are_complete(self) -> HarnessEvalCase:
        if self.runner == "protocol_hello" and set(self.metrics.guardrails) != {
            "no_model",
            "no_side_effect",
        }:
            raise ValueError(
                "protocol_hello 必须声明 no_model 与 no_side_effect guardrail"
            )
        return self


class HarnessEvalSuite(_StrictModel):
    schema_version: Literal[1]
    id: str
    title: str = Field(min_length=1, max_length=200)
    cases: tuple[HarnessEvalCase, ...] = Field(min_length=1, max_length=500)
    budget: HarnessEvalSuiteBudget = Field(default_factory=HarnessEvalSuiteBudget)
    comparison_policy: HarnessEvalComparisonPolicy = Field(
        default_factory=HarnessEvalComparisonPolicy
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("suite id 格式无效")
        return normalized

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("suite title 不能为空")
        return normalized

    @model_validator(mode="after")
    def _unique_case_ids(self) -> HarnessEvalSuite:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Eval Suite 中存在重复 case id")
        return self


class HarnessProtocolActual(_StrictModel):
    outcome: Literal["accepted", "rejected"]
    error_code: str | None = None
    selected_version: int | None = None
    capabilities: tuple[str, ...] = ()


class HarnessEvalGuardrailResult(_StrictModel):
    guardrail: Literal["no_model", "no_side_effect"]
    status: EvalGuardrailStatus
    code: str = Field(default="", max_length=128)


class HarnessEvalMetricObservation(_StrictModel):
    """One finite, unit-bound mechanical observation produced by a runner."""

    metric: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    value: float = Field(ge=-1_000_000_000_000_000, le=1_000_000_000_000_000)
    unit: Literal["count", "ratio", "milliseconds", "tokens", "usd", "scalar"]
    direction: Literal["decrease", "increase"]
    target: float = Field(ge=-1_000_000_000_000_000, le=1_000_000_000_000_000)
    primary: bool = False

    @field_validator("value", "target", mode="before")
    @classmethod
    def _reject_boolean_number(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("数值指标不得使用布尔值。")
        return value

    @model_validator(mode="after")
    def _value_matches_unit(self) -> HarnessEvalMetricObservation:
        if self.unit in {"count", "milliseconds", "tokens", "usd"} and (
            self.value < 0 or self.target < 0
        ):
            raise ValueError("非负指标的 value 与 target 不能小于 0。")
        if self.unit in {"count", "tokens"} and (
            not self.value.is_integer() or not self.target.is_integer()
        ):
            raise ValueError("count/tokens 指标必须是整数值。")
        if self.unit == "ratio" and not (
            0 <= self.value <= 1 and 0 <= self.target <= 1
        ):
            raise ValueError("ratio 指标必须位于 0..1。")
        return self

    @property
    def target_met(self) -> bool:
        equal = math.isclose(
            self.value,
            self.target,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        if self.direction == "decrease":
            return self.value < self.target or equal
        return self.value > self.target or equal


class HarnessEvalCaseResult(_StrictModel):
    case_id: str
    runner: str
    status: EvalCaseStatus
    expected: HarnessProtocolExpected | None = None
    actual: HarnessProtocolActual | None = None
    primary_metric: str = Field(default="", max_length=128)
    metric_observations: tuple[HarnessEvalMetricObservation, ...] = Field(
        default=(),
        max_length=32,
    )
    guardrails: tuple[HarnessEvalGuardrailResult, ...] = Field(
        default=(),
        max_length=16,
    )
    code: str = ""
    message: str = ""
    duration_ms: float = Field(default=0, ge=0)

    @field_validator("primary_metric")
    @classmethod
    def _valid_primary_metric(cls, value: str) -> str:
        if value and not re.fullmatch(r"^[a-z][a-z0-9_.-]{0,127}$", value):
            raise ValueError("primary_metric 格式无效。")
        return value

    @field_validator("guardrails")
    @classmethod
    def _unique_guardrail_results(
        cls,
        values: tuple[HarnessEvalGuardrailResult, ...],
    ) -> tuple[HarnessEvalGuardrailResult, ...]:
        names = [item.guardrail for item in values]
        if len(names) != len(set(names)):
            raise ValueError("guardrail result 不能重复。")
        return tuple(sorted(values, key=lambda item: item.guardrail))

    @model_validator(mode="after")
    def _metric_observations_are_authoritative(self) -> HarnessEvalCaseResult:
        observations = self.metric_observations
        names = tuple(item.metric for item in observations)
        if names != tuple(sorted(set(names))):
            raise ValueError("metric observations 必须排序且不得重复。")
        if not observations:
            return self
        if self.status in {EvalCaseStatus.EVALUATION_ERROR, EvalCaseStatus.SKIPPED}:
            raise ValueError("评测错误或跳过的 case 不得携带数值指标。")
        primaries = tuple(item for item in observations if item.primary)
        if len(primaries) != 1 or self.primary_metric != primaries[0].metric:
            raise ValueError("metric observations 必须唯一绑定 primary_metric。")
        expected_status = (
            EvalCaseStatus.PASSED
            if primaries[0].target_met
            else EvalCaseStatus.IMPLEMENTATION_FAILURE
        )
        if self.status is not expected_status:
            raise ValueError("case status 与 primary metric target 判定不一致。")
        return self


class HarnessEvalSuiteResult(_StrictModel):
    suite_id: str
    title: str
    suite_path: str
    suite_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    status: EvalRunStatus
    cases: tuple[HarnessEvalCaseResult, ...] = ()
    code: str = ""
    message: str = ""
    comparison_policy: HarnessEvalComparisonPolicy = Field(
        default_factory=HarnessEvalComparisonPolicy
    )
    policy_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    baseline_identity: HarnessEvalBaselineIdentity | None = None
    baseline_identity_code: str = Field(default="", max_length=128)
    duration_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _policy_digest_matches(self) -> HarnessEvalSuiteResult:
        expected = self.comparison_policy.sha256
        if not self.policy_sha256:
            object.__setattr__(self, "policy_sha256", expected)
        elif self.policy_sha256 != expected:
            raise ValueError("policy_sha256 与 comparison_policy 不匹配。")
        return self

    @property
    def passed(self) -> int:
        return sum(case.status is EvalCaseStatus.PASSED for case in self.cases)

    @property
    def implementation_failures(self) -> int:
        return sum(
            case.status is EvalCaseStatus.IMPLEMENTATION_FAILURE for case in self.cases
        )

    @property
    def evaluation_errors(self) -> int:
        return sum(case.status is EvalCaseStatus.EVALUATION_ERROR for case in self.cases)

    @property
    def skipped(self) -> int:
        return sum(case.status is EvalCaseStatus.SKIPPED for case in self.cases)

    def canonical_payload(self) -> dict[str, object]:
        """Return deterministic comparison data with volatile timings removed."""
        return {
            "suite_id": self.suite_id,
            "title": self.title,
            "suite_path": self.suite_path,
            "suite_sha256": self.suite_sha256,
            "status": str(self.status),
            "code": self.code,
            "message": self.message,
            "comparison_policy": self.comparison_policy.model_dump(mode="json"),
            "policy_sha256": self.policy_sha256,
            "baseline_identity": (
                self.baseline_identity.model_dump(mode="json")
                if self.baseline_identity is not None
                else None
            ),
            "baseline_identity_code": self.baseline_identity_code,
            "cases": [
                {
                    key: value
                    for key, value in case.model_dump(mode="json").items()
                    if key != "duration_ms"
                }
                for case in self.cases
            ],
        }


class HarnessEvalReport(_StrictModel):
    requested: str = "all"
    status: EvalRunStatus
    suites: tuple[HarnessEvalSuiteResult, ...] = ()
    code: str = ""
    message: str = ""
    duration_ms: float = Field(default=0, ge=0)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "status": str(self.status),
            "code": self.code,
            "message": self.message,
            "suites": [suite.canonical_payload() for suite in self.suites],
        }


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
