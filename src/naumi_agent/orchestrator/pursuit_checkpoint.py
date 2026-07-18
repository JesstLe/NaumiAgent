"""Strict, bounded checkpoint contract for durable Pursuit recovery."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINT_HISTORY = 20
MAX_CHECKPOINT_ACTIONS = 20

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b"),
)


class PursuitCheckpointPersistenceError(RuntimeError):
    """Raised when a safe-boundary checkpoint cannot be persisted."""


class _CheckpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CheckpointCriterion(_CheckpointModel):
    id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2_000)
    verification_command: str = Field(max_length=4_000)
    status: Literal["not_started", "in_progress", "verified", "failed"]
    evidence: str = Field(max_length=4_000)
    last_checked: float = Field(ge=0, allow_inf_nan=False)


class CheckpointGoal(_CheckpointModel):
    original_goal: str = Field(min_length=1, max_length=8_000)
    description: str = Field(min_length=1, max_length=8_000)
    criteria: tuple[CheckpointCriterion, ...] = Field(min_length=1, max_length=50)
    constraints: tuple[str, ...] = Field(max_length=50)
    estimated_complexity: Literal["S", "M", "L", "XL"]


class CheckpointBudget(_CheckpointModel):
    tokens_used: int = Field(ge=0)
    cost_usd: float = Field(ge=0, allow_inf_nan=False)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_iterations: int = Field(ge=1)
    max_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_time_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    stagnation_threshold: int = Field(default=3, ge=1, le=100)
    verify_interval: int = Field(default=1, ge=1, le=10_000)
    plan_depth: int = Field(default=3, ge=1, le=100)
    replan_on_stagnation: bool = True


class CheckpointWait(_CheckpointModel):
    task_id: str = Field(min_length=1, max_length=256)
    action_id: str = Field(max_length=256)
    command: str = Field(max_length=4_000)
    created_at: float = Field(ge=0, allow_inf_nan=False)


class CheckpointInteraction(_CheckpointModel):
    interaction_id: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=4_000)
    options: tuple[str, ...] = Field(max_length=20)
    allow_custom_input: bool
    created_at: float = Field(ge=0, allow_inf_nan=False)
    timeout_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class CheckpointInteractionRef(_CheckpointModel):
    """Stable reference to the Harness interaction authority."""

    authority: Literal["harness"] = "harness"
    interaction_id: str = Field(
        min_length=5,
        max_length=132,
        pattern=r"^ask-[A-Za-z0-9._:-]{1,128}$",
    )


class CheckpointIteration(_CheckpointModel):
    iteration: int = Field(ge=1)
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    assessment: str = Field(max_length=4_000)
    gaps_found: tuple[str, ...] = Field(max_length=50)
    actions_planned: tuple[str, ...] = Field(max_length=MAX_CHECKPOINT_ACTIONS)
    actions_taken: tuple[str, ...] = Field(max_length=MAX_CHECKPOINT_ACTIONS)
    criteria_status: dict[str, str] = Field(max_length=50)
    convergence_score: float = Field(ge=0, le=1, allow_inf_nan=False)


class PursuitCheckpoint(_CheckpointModel):
    """Self-contained latest recovery snapshot for one Pursuit run."""

    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(ge=1)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    status: Literal[
        "running", "waiting", "blocked", "completed", "failed",
        "cancelled", "budget_exceeded",
    ]
    phase: str = Field(min_length=1, max_length=128)
    iteration: int = Field(ge=0)
    goal: CheckpointGoal
    pending_actions: tuple[str, ...] = Field(max_length=MAX_CHECKPOINT_ACTIONS)
    next_action: str = Field(max_length=2_000)
    budget: CheckpointBudget
    evidence_cursor: int = Field(ge=0)
    waiting_on: tuple[CheckpointWait, ...] = Field(max_length=100)
    pending_interaction: CheckpointInteractionRef | CheckpointInteraction | None = None
    recent_history: tuple[CheckpointIteration, ...] = Field(
        max_length=MAX_CHECKPOINT_HISTORY,
    )
    worktree_name: str = Field(max_length=256)
    worktree_path: str = Field(max_length=4_000)

    def canonical_json(self) -> str:
        """Return deterministic JSON used for storage and integrity checks."""
        payload = self.model_dump(mode="json")
        # HAR-10.4b added operational loop settings without invalidating v1
        # payloads already written by HAR-10.4a. Missing optional fields remain
        # absent when authenticating those historical bytes.
        budget_payload = payload["budget"]
        for field_name in (
            "stagnation_threshold",
            "verify_interval",
            "plan_depth",
            "replan_on_stagnation",
        ):
            if field_name not in self.budget.model_fields_set:
                budget_payload.pop(field_name, None)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def checkpoint_id(self) -> str:
        return f"pchk_{self.digest()[:24]}"


def checkpoint_safe_text(value: object, *, limit: int) -> str:
    """Bound and redact text before it enters a durable checkpoint."""
    text = str(value).replace("\x00", "�")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(r"\1\2[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text[:limit]
