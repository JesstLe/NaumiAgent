"""Strict data contracts for repository Harness profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessProfileStatus(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    INVALID = "invalid"


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
