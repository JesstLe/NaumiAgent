"""Strict data contracts for repository Harness profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessProfileStatus(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    INVALID = "invalid"


class HarnessTaskKind(StrEnum):
    ANSWER = "answer"
    ANALYSIS = "analysis"
    CHANGE = "change"
    MONITOR = "monitor"


class HarnessProfileError(_StrictModel):
    code: str
    message: str
    hint: str


class HarnessKnowledgeSpec(_StrictModel):
    entrypoints: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_turn_tokens: int = Field(default=8_000, ge=1, le=12_000)
    max_file_bytes: int = Field(default=131_072, ge=1, le=1_048_576)


class HarnessCompletionSpec(_StrictModel):
    require_todo_reconciliation: bool = True
    require_change_evidence: bool = True
    correction_attempts: int = Field(default=1, ge=0, le=1)
    unverified_status: Literal["completed_unverified", "blocked"] = (
        "completed_unverified"
    )


class HarnessAcceptanceCriterion(_StrictModel):
    id: str
    description: str

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _CHECK_ID_RE.fullmatch(normalized):
            raise ValueError("验收标准 id 格式无效")
        return normalized

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("验收标准 description 不能为空")
        return normalized


class HarnessCompletionContract(_StrictModel):
    run_id: str = Field(max_length=128)
    session_id: str = Field(max_length=256)
    task_id: str | None = None
    issue_id: str | None = None
    profile_digest: str | None = Field(default=None, max_length=128)
    task_kind: HarnessTaskKind
    objective: str = Field(max_length=16_000)
    acceptance_criteria: tuple[HarnessAcceptanceCriterion, ...] = Field(
        default=(),
        max_length=128,
    )
    allowed_scope: tuple[str, ...] = Field(default=(), max_length=512)
    prohibited_scope: tuple[str, ...] = Field(default=(), max_length=512)
    required_checks: tuple[str, ...] = Field(default=(), max_length=128)
    required_evidence: tuple[str, ...] = Field(default=(), max_length=128)
    correction_attempts: int = Field(default=1, ge=0, le=1)
    unverified_status: Literal["completed_unverified", "blocked"] = (
        "completed_unverified"
    )
    source_refs: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _RUN_ID_RE.fullmatch(normalized):
            raise ValueError("run_id 格式无效")
        return normalized

    @field_validator("session_id", "task_id", "issue_id", "profile_digest")
    @classmethod
    def _validate_optional_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("身份字段不能为空")
        return normalized

    @field_validator("objective")
    @classmethod
    def _validate_objective(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("objective 不能为空")
        return normalized

    @field_validator("allowed_scope", "prohibited_scope")
    @classmethod
    def _validate_scope_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_unique_strings(values, field="scope", paths=True)

    @field_validator("required_checks")
    @classmethod
    def _validate_required_checks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _normalize_unique_strings(values, field="required_checks")
        if any(not _CHECK_ID_RE.fullmatch(value) for value in normalized):
            raise ValueError("required_checks 含无效 check id")
        return normalized

    @field_validator("required_evidence", "source_refs")
    @classmethod
    def _validate_string_lists(
        cls,
        values: tuple[str, ...],
        info: object,
    ) -> tuple[str, ...]:
        field_name = getattr(info, "field_name", "field")
        return _normalize_unique_strings(values, field=field_name)

    @model_validator(mode="after")
    def _criteria_ids_are_unique(self) -> HarnessCompletionContract:
        ids = [criterion.id for criterion in self.acceptance_criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance_criteria 中存在重复 id")
        return self

    def effective_task_kind(self, *, mutating_tool_used: bool) -> HarnessTaskKind:
        if mutating_tool_used:
            return HarnessTaskKind.CHANGE
        return self.task_kind


class HarnessCheckSpec(_StrictModel):
    id: str
    label: str | None = None
    argv: tuple[str, ...]
    timeout_seconds: int = Field(default=180, ge=1, le=3_600)
    when_changed: tuple[str, ...] = ()
    required_for: tuple[Literal["answer", "analysis", "change", "monitor"], ...] = ()

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _CHECK_ID_RE.fullmatch(normalized):
            raise ValueError(
                "check id 必须以小写字母开头，且只能包含小写字母、数字、_ 或 -"
            )
        return normalized

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("check label 不能为空")
        return normalized

    @field_validator("argv", mode="before")
    @classmethod
    def _validate_argv_shape(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("check argv 必须是非空字符串数组")
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError("check argv 不能包含空值或非字符串")
        return tuple(item.strip() for item in value)


class HarnessEvalSpec(_StrictModel):
    suites: tuple[str, ...] = ()
    live_default: bool = False
    max_cost_usd: float = Field(default=1.0, ge=0)
    max_duration_seconds: int = Field(default=1_800, ge=1, le=86_400)


class HarnessProfile(_StrictModel):
    schema_version: Literal[1]
    knowledge: HarnessKnowledgeSpec = Field(default_factory=HarnessKnowledgeSpec)
    completion: HarnessCompletionSpec = Field(default_factory=HarnessCompletionSpec)
    checks: tuple[HarnessCheckSpec, ...] = ()
    evals: HarnessEvalSpec = Field(default_factory=HarnessEvalSpec)

    @model_validator(mode="after")
    def _check_ids_are_unique(self) -> HarnessProfile:
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("checks 中存在重复 id")
        return self


@dataclass(frozen=True)
class HarnessProfileSnapshot:
    workspace_root: Path
    profile_path: Path
    status: HarnessProfileStatus
    digest: str | None = None
    profile: HarnessProfile | None = None
    errors: tuple[HarnessProfileError, ...] = ()


def _normalize_unique_strings(
    values: tuple[str, ...],
    *,
    field: str,
    paths: bool = False,
) -> tuple[str, ...]:
    normalized = tuple(value.strip().replace("\\", "/") for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"{field} 不能包含空值")
    if any(len(value) > 1_024 for value in normalized):
        raise ValueError(f"{field} 单项长度不能超过 1024")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} 中存在重复值")
    if paths:
        for value in normalized:
            parts = value.split("/")
            if value.startswith("/") or ".." in parts:
                raise ValueError(f"{field} 必须是工作区内的相对 pattern")
    return normalized
