"""Strict contracts for deterministic Harness evaluation assets and results."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class HarnessEvalSuite(_StrictModel):
    schema_version: Literal[1]
    id: str
    title: str = Field(min_length=1, max_length=200)
    cases: tuple[HarnessEvalCase, ...] = Field(min_length=1, max_length=500)
    budget: HarnessEvalSuiteBudget = Field(default_factory=HarnessEvalSuiteBudget)

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


class HarnessEvalCaseResult(_StrictModel):
    case_id: str
    runner: str
    status: EvalCaseStatus
    expected: HarnessProtocolExpected | None = None
    actual: HarnessProtocolActual | None = None
    code: str = ""
    message: str = ""
    duration_ms: float = Field(default=0, ge=0)


class HarnessEvalSuiteResult(_StrictModel):
    suite_id: str
    title: str
    suite_path: str
    status: EvalRunStatus
    cases: tuple[HarnessEvalCaseResult, ...] = ()
    code: str = ""
    message: str = ""
    duration_ms: float = Field(default=0, ge=0)

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
            "status": str(self.status),
            "code": self.code,
            "message": self.message,
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
