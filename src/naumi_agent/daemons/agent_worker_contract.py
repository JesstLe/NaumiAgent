"""Tamper-evident request and terminal contracts for delegated Agent work.

The contracts intentionally contain digests and bounded execution policy, not
raw prompts, context, responses, errors, credentials, or workspace paths.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PERMISSION_MODES = {"bypass", "permissive", "moderate", "strict", "lockdown"}
_MODEL_TIERS = {"fast", "capable", "reasoning"}
_MAX_TASK_BYTES = 2 * 1024**2
_MAX_CONTEXT_BYTES = 16 * 1024**2
_MAX_RESPONSE_BYTES = 16 * 1024**2
_MAX_ERROR_BYTES = 1024**2


class AgentWorkerResultStatus(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    MAX_TURNS = "max_turns"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentWorkerRequest:
    """Immutable execution boundary issued before an Agent model call."""

    schema_version: int
    request_id: str
    task_id_sha256: str
    session_id_sha256: str
    agent_name: str
    task_sha256: str
    task_bytes: int
    context_sha256: str
    context_bytes: int
    tool_scope: tuple[str, ...]
    permission_mode: str
    model_tier: str
    max_turns: int
    max_cost_microusd: int | None
    timeout_milliseconds: int
    message_topic_sha256: str
    issued_at: str
    request_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Agent Worker request schema_version 必须为 1。")
        _require_identifier(self.request_id, field="request_id")
        _require_sha256(self.task_id_sha256, field="task_id_sha256")
        _require_sha256(self.session_id_sha256, field="session_id_sha256")
        _require_identifier(self.agent_name, field="agent_name")
        _require_sha256(self.task_sha256, field="task_sha256")
        _require_range(
            self.task_bytes,
            field="task_bytes",
            minimum=1,
            maximum=_MAX_TASK_BYTES,
        )
        _require_sha256(self.context_sha256, field="context_sha256")
        _require_range(
            self.context_bytes,
            field="context_bytes",
            minimum=0,
            maximum=_MAX_CONTEXT_BYTES,
        )
        if len(self.tool_scope) > 256:
            raise ValueError("Agent Worker tool_scope 最多允许 256 项。")
        if tuple(sorted(set(self.tool_scope))) != self.tool_scope:
            raise ValueError("Agent Worker tool_scope 必须唯一且排序。")
        if any(not _TOOL_RE.fullmatch(item) for item in self.tool_scope):
            raise ValueError("Agent Worker tool_scope 包含无效工具名。")
        if self.permission_mode not in _PERMISSION_MODES:
            raise ValueError("Agent Worker permission_mode 无效。")
        if self.model_tier not in _MODEL_TIERS:
            raise ValueError("Agent Worker model_tier 无效。")
        _require_range(self.max_turns, field="max_turns", minimum=1, maximum=1_000)
        if self.max_cost_microusd is not None:
            _require_range(
                self.max_cost_microusd,
                field="max_cost_microusd",
                minimum=0,
                maximum=10**15,
            )
        _require_range(
            self.timeout_milliseconds,
            field="timeout_milliseconds",
            minimum=1,
            maximum=7 * 24 * 60 * 60 * 1_000,
        )
        _require_sha256(self.message_topic_sha256, field="message_topic_sha256")
        _require_aware_timestamp(self.issued_at, field="issued_at")
        _require_sha256(self.request_sha256, field="request_sha256")
        if self.request_sha256 != _digest(_request_payload(self)):
            raise ValueError("Agent Worker request 摘要校验失败。")


@dataclass(frozen=True, slots=True)
class AgentWorkerResult:
    """Low-sensitivity terminal receipt bound to one exact request."""

    schema_version: int
    request_sha256: str
    status: AgentWorkerResultStatus
    response_sha256: str
    response_bytes: int
    error_sha256: str
    total_tokens: int
    total_cost_microusd: int
    turns: int
    reason_code: str
    completed_at: str
    result_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Agent Worker result schema_version 必须为 1。")
        _require_sha256(self.request_sha256, field="request_sha256")
        if not isinstance(self.status, AgentWorkerResultStatus):
            raise TypeError("Agent Worker result status 类型无效。")
        _require_sha256(self.response_sha256, field="response_sha256")
        _require_range(
            self.response_bytes,
            field="response_bytes",
            minimum=0,
            maximum=_MAX_RESPONSE_BYTES,
        )
        _require_sha256(self.error_sha256, field="error_sha256")
        _require_range(
            self.total_tokens,
            field="total_tokens",
            minimum=0,
            maximum=2**63 - 1,
        )
        _require_range(
            self.total_cost_microusd,
            field="total_cost_microusd",
            minimum=0,
            maximum=10**18,
        )
        _require_range(self.turns, field="turns", minimum=0, maximum=1_000)
        _require_identifier(self.reason_code, field="reason_code")
        expected_reason = {
            AgentWorkerResultStatus.COMPLETED: "agent_completed",
            AgentWorkerResultStatus.ERROR: "agent_failed",
            AgentWorkerResultStatus.TIMEOUT: "agent_timeout",
            AgentWorkerResultStatus.MAX_TURNS: "agent_max_turns",
            AgentWorkerResultStatus.CANCELLED: "agent_cancelled",
        }[self.status]
        if self.reason_code != expected_reason:
            raise ValueError("Agent Worker result reason_code 与状态不一致。")
        _require_aware_timestamp(self.completed_at, field="completed_at")
        _require_sha256(self.result_sha256, field="result_sha256")
        if self.result_sha256 != _digest(_result_payload(self)):
            raise ValueError("Agent Worker result 摘要校验失败。")


def issue_agent_worker_request(
    *,
    task_id: str,
    session_id: str,
    agent_name: str,
    task: str,
    context: str,
    tool_scope: tuple[str, ...] | list[str],
    permission_mode: str,
    model_tier: str,
    max_turns: int,
    max_budget_usd: float | None,
    timeout_seconds: float,
    message_topic: str,
    issued_at: str,
) -> AgentWorkerRequest:
    """Issue a request from the exact policy and content about to execute."""
    normalized_task_id = _require_text(task_id, field="task_id", maximum=512)
    normalized_session = _require_text(
        session_id,
        field="session_id",
        maximum=512,
        allow_empty=True,
    )
    normalized_agent = _require_text(agent_name, field="agent_name", maximum=128)
    normalized_task = _require_text(task, field="task", maximum=_MAX_TASK_BYTES)
    normalized_context = _require_text(
        context,
        field="context",
        maximum=_MAX_CONTEXT_BYTES,
        allow_empty=True,
    )
    normalized_topic = _require_text(message_topic, field="message_topic", maximum=768)
    tools = tuple(sorted(set(tool_scope)))
    max_cost = _usd_to_microusd(max_budget_usd)
    timeout_ms = _seconds_to_milliseconds(timeout_seconds)
    timestamp = _normalize_timestamp(issued_at, field="issued_at")
    identity = "\x00".join(
        (normalized_session, normalized_task_id, normalized_agent)
    ).encode("utf-8")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "request_id": f"agent-request-{hashlib.sha256(identity).hexdigest()}",
        "task_id_sha256": _text_digest(normalized_task_id),
        "session_id_sha256": _text_digest(normalized_session),
        "agent_name": normalized_agent,
        "task_sha256": _text_digest(normalized_task),
        "task_bytes": len(normalized_task.encode("utf-8")),
        "context_sha256": _text_digest(normalized_context),
        "context_bytes": len(normalized_context.encode("utf-8")),
        "tool_scope": tools,
        "permission_mode": str(permission_mode),
        "model_tier": str(model_tier),
        "max_turns": max_turns,
        "max_cost_microusd": max_cost,
        "timeout_milliseconds": timeout_ms,
        "message_topic_sha256": _text_digest(normalized_topic),
        "issued_at": timestamp,
    }
    return AgentWorkerRequest(
        **payload,
        request_sha256=_digest(payload),
    )


def issue_agent_worker_result(
    *,
    request: AgentWorkerRequest,
    status: str,
    response: str,
    error: str | None,
    total_tokens: int,
    total_cost_usd: float,
    turns: int,
    completed_at: str,
) -> AgentWorkerResult:
    """Issue a terminal receipt without retaining raw model output or errors."""
    if not isinstance(request, AgentWorkerRequest):
        raise TypeError("request 必须是 AgentWorkerRequest。")
    raw_status = str(status).strip().lower()
    if raw_status == "failed":
        raw_status = AgentWorkerResultStatus.ERROR.value
    try:
        normalized_status = AgentWorkerResultStatus(raw_status)
    except ValueError as exc:
        raise ValueError("Agent Worker result status 无效。") from exc
    normalized_response = _require_text(
        response,
        field="response",
        maximum=_MAX_RESPONSE_BYTES,
        allow_empty=True,
    )
    normalized_error = _require_text(
        error or "",
        field="error",
        maximum=_MAX_ERROR_BYTES,
        allow_empty=True,
    )
    reason = {
        AgentWorkerResultStatus.COMPLETED: "agent_completed",
        AgentWorkerResultStatus.ERROR: "agent_failed",
        AgentWorkerResultStatus.TIMEOUT: "agent_timeout",
        AgentWorkerResultStatus.MAX_TURNS: "agent_max_turns",
        AgentWorkerResultStatus.CANCELLED: "agent_cancelled",
    }[normalized_status]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "request_sha256": request.request_sha256,
        "status": normalized_status,
        "response_sha256": _text_digest(normalized_response),
        "response_bytes": len(normalized_response.encode("utf-8")),
        "error_sha256": _text_digest(normalized_error),
        "total_tokens": total_tokens,
        "total_cost_microusd": _usd_to_microusd(total_cost_usd) or 0,
        "turns": turns,
        "reason_code": reason,
        "completed_at": _normalize_timestamp(completed_at, field="completed_at"),
    }
    return AgentWorkerResult(
        **payload,
        result_sha256=_digest(payload),
    )


def _request_payload(request: AgentWorkerRequest) -> dict[str, Any]:
    return {
        "schema_version": request.schema_version,
        "request_id": request.request_id,
        "task_id_sha256": request.task_id_sha256,
        "session_id_sha256": request.session_id_sha256,
        "agent_name": request.agent_name,
        "task_sha256": request.task_sha256,
        "task_bytes": request.task_bytes,
        "context_sha256": request.context_sha256,
        "context_bytes": request.context_bytes,
        "tool_scope": request.tool_scope,
        "permission_mode": request.permission_mode,
        "model_tier": request.model_tier,
        "max_turns": request.max_turns,
        "max_cost_microusd": request.max_cost_microusd,
        "timeout_milliseconds": request.timeout_milliseconds,
        "message_topic_sha256": request.message_topic_sha256,
        "issued_at": request.issued_at,
    }


def _result_payload(result: AgentWorkerResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "request_sha256": result.request_sha256,
        "status": result.status,
        "response_sha256": result.response_sha256,
        "response_bytes": result.response_bytes,
        "error_sha256": result.error_sha256,
        "total_tokens": result.total_tokens,
        "total_cost_microusd": result.total_cost_microusd,
        "turns": result.turns,
        "reason_code": result.reason_code,
        "completed_at": result.completed_at,
    }


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(
    value: str,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串。")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field} 不能为空。")
    if "\x00" in value:
        raise ValueError(f"{field} 不能包含 NUL。")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} 超过 {maximum} 字节上限。")
    return value


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} 标识格式无效。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _require_range(value: int, *, field: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} 必须是整数。")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间。")


def _usd_to_microusd(value: float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Agent Worker 预算必须是数字或 None。")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("Agent Worker 预算必须是非负有限数字。")
    micros = round(amount * 1_000_000)
    if micros > 10**18:
        raise ValueError("Agent Worker 预算超过上限。")
    return micros


def _seconds_to_milliseconds(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Agent Worker timeout 必须是数字。")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Agent Worker timeout 必须是正有限数字。")
    return max(1, round(seconds * 1_000))


def _normalize_timestamp(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是 ISO 时间字符串。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效 ISO 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed.isoformat()


def _require_aware_timestamp(value: str, *, field: str) -> None:
    _normalize_timestamp(value, field=field)


__all__ = [
    "AgentWorkerRequest",
    "AgentWorkerResult",
    "AgentWorkerResultStatus",
    "issue_agent_worker_request",
    "issue_agent_worker_result",
]
