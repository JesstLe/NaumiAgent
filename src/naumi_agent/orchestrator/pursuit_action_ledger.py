"""Strict durable identities and states for Pursuit shell actions."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ACTION_LEDGER_SCHEMA_VERSION = 1

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|token|password|passwd|secret|authorization|cookie)"
        r"(\s*[:=]\s*)([^\s,;]+)"
    ),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/-]{12,}=*)"),
)


class PursuitActionState(StrEnum):
    """Monotonic lifecycle states for one externally visible action."""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_ACTION_STATES = frozenset(
    {PursuitActionState.COMPLETED, PursuitActionState.FAILED}
)


class PursuitActionRecord(BaseModel):
    """Authenticated latest state reconstructed from an immutable event chain."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(default=ACTION_LEDGER_SCHEMA_VERSION, ge=1, le=1)
    action_key: str = Field(pattern=r"^pact_[0-9a-f]{24}$")
    run_id: str = Field(min_length=1, max_length=256)
    iteration: int = Field(ge=1)
    action_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments_size_bytes: int = Field(ge=0, le=10_000_000)
    argument_summary: str = Field(max_length=2_000)
    state: PursuitActionState
    sequence: int = Field(ge=1)
    dispatch_token: str = Field(min_length=1, max_length=128)
    background_task_id: str = Field(max_length=256)
    result_status: str = Field(max_length=64)
    result_summary: str = Field(max_length=2_000)
    result_sha256: str = Field(pattern=r"^$|^[0-9a-f]{64}$")
    prepared_at: float = Field(ge=0, allow_inf_nan=False)
    updated_at: float = Field(ge=0, allow_inf_nan=False)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ACTION_STATES


def canonical_action_arguments(arguments: dict[str, object]) -> tuple[str, str, int]:
    """Return canonical JSON, its digest, and byte size without mutating input."""
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded = payload.encode("utf-8")
    return payload, hashlib.sha256(encoded).hexdigest(), len(encoded)


def make_action_key(
    *,
    run_id: str,
    iteration: int,
    action_id: str,
    tool_name: str,
    arguments_sha256: str,
) -> str:
    """Build an identity stable for retries but distinct across iterations."""
    identity = json.dumps(
        [run_id, iteration, action_id, tool_name, arguments_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"pact_{digest[:24]}"


def action_safe_text(value: object, *, limit: int = 2_000) -> str:
    """Bound and redact text before it enters the durable action ledger."""
    text = str(value).replace("\x00", "�")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(r"\1\2[REDACTED]", text)
        elif pattern.groups == 2:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def digest_result(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
