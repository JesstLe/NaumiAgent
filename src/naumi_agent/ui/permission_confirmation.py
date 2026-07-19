"""Public permission-confirmation helpers for terminal UI transports."""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Mapping
from itertools import islice
from typing import Any

_REDACTED = "[已隐藏]"
_TRUNCATED = "[已截断]"
_DEPTH_TRUNCATED = "[已达深度上限]"
_CIRCULAR_REFERENCE = "[循环引用]"
_OVERSIZED_INTEGER = "[整数过大]"
_NON_FINITE_FLOAT = "[非有限浮点数]"
_SUMMARY_UNAVAILABLE = "[无法生成摘要]"
_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "authorization", "cookie")
_STRING_LIMIT = 160
_COLLECTION_LIMIT = 50
_JSON_LIMIT = 1200
_DEPTH_LIMIT = 12
_INTEGER_BIT_LIMIT = 512
_CONTAINER_TYPES = (dict, list, tuple, set, frozenset)
_SUPPORTED_BACKEND_CHOICES = frozenset(
    {"allow_once", "deny", "grant_session"}
)


def normalize_backend_permission_choices(
    raw_choices: Any,
) -> tuple[str, ...] | None:
    """Normalize one backend-owned choice list without inventing authority."""
    if (
        not isinstance(raw_choices, Collection)
        or isinstance(raw_choices, (str, bytes, bytearray, Mapping))
    ):
        return None
    values = (
        sorted(raw_choices, key=lambda choice: str(choice))
        if isinstance(raw_choices, (set, frozenset))
        else raw_choices
    )
    choices: list[str] = []
    for value in values:
        if not isinstance(value, str):
            return None
        choice = value.strip().lower()
        if not choice or choice not in _SUPPORTED_BACKEND_CHOICES:
            return None
        if choice not in choices:
            choices.append(choice)
    return tuple(choices)


def public_permission_request_payload(
    payload: Mapping[str, Any],
    *,
    request_id: str,
    choices: tuple[str, ...],
) -> dict[str, Any]:
    """Build the exact redacted request shown by every terminal frontend."""
    normalized_choices = normalize_backend_permission_choices(choices)
    if normalized_choices is None or not {"allow_once", "deny"}.issubset(
        normalized_choices
    ):
        raise ValueError("权限选择必须同时包含 allow_once 与 deny。")
    public_choices = [*normalized_choices, "bypass"]
    return {
        "request_id": request_id,
        "call_id": str(payload.get("call_id") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "agent_name": str(
            payload.get("agent_name") or payload.get("agent") or "main"
        ),
        "tool_name": str(
            payload.get("tool_name") or payload.get("tool") or "tool"
        ),
        "tool_family": str(payload.get("tool_family") or ""),
        "arguments_summary": summarize_arguments(payload.get("arguments", {})),
        "reason": str(payload.get("reason") or "等待用户确认。"),
        "risk_level": str(payload.get("risk_level") or "medium"),
        "choices": public_choices,
        "scope": "session" if "grant_session" in normalized_choices else "call",
        "expires_at": payload.get("expires_at"),
        "requires_double_confirm": False,
        "status": "needs_confirmation",
    }


def summarize_arguments(arguments: Any) -> dict[str, Any]:
    """Return a bounded, JSON-safe public summary without sensitive values."""
    try:
        normalized = _summarize(
            arguments,
            depth_remaining=_DEPTH_LIMIT,
            active_container_ids=set(),
        )
        if not isinstance(normalized, dict):
            normalized = {"value": normalized}
        return _fit_json(normalized)
    except Exception:
        return {"value": _SUMMARY_UNAVAILABLE}


def _summarize(
    value: Any,
    *,
    depth_remaining: int,
    active_container_ids: set[int],
) -> Any:
    if depth_remaining <= 0:
        return _DEPTH_TRUNCATED

    value_type = type(value)
    if value_type in _CONTAINER_TYPES:
        container_id = id(value)
        if container_id in active_container_ids:
            return _CIRCULAR_REFERENCE
        active_container_ids.add(container_id)
        try:
            if value_type is dict:
                return _summarize_dict(
                    value,
                    depth_remaining=depth_remaining,
                    active_container_ids=active_container_ids,
                )
            return [
                _summarize(
                    item,
                    depth_remaining=depth_remaining - 1,
                    active_container_ids=active_container_ids,
                )
                for item in islice(value, _COLLECTION_LIMIT)
            ]
        finally:
            active_container_ids.remove(container_id)
    if value_type is str:
        return _truncate(value)
    if value is None or value_type is bool:
        return value
    if value_type is int:
        return value if value.bit_length() <= _INTEGER_BIT_LIMIT else _OVERSIZED_INTEGER
    if value_type is float:
        return value if math.isfinite(value) else _NON_FINITE_FLOAT
    if value_type is bytes:
        return "<bytes>"
    return _type_placeholder(value)


def _summarize_dict(
    value: dict[Any, Any],
    *,
    depth_remaining: int,
    active_container_ids: set[int],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for raw_key, raw_value in islice(value.items(), _COLLECTION_LIMIT):
        key, sensitive = _summarize_key(raw_key)
        summary[key] = (
            _REDACTED
            if sensitive
            else _summarize(
                raw_value,
                depth_remaining=depth_remaining - 1,
                active_container_ids=active_container_ids,
            )
        )
    return summary


def _summarize_key(value: Any) -> tuple[str, bool]:
    value_type = type(value)
    if value_type is str:
        return _truncate(value), _is_sensitive(value)
    if value is None:
        return "None", False
    if value_type is bool:
        return str(value), False
    if value_type is int:
        if value.bit_length() > _INTEGER_BIT_LIMIT:
            return _OVERSIZED_INTEGER, False
        return str(value), False
    if value_type is float:
        if not math.isfinite(value):
            return _NON_FINITE_FLOAT, False
        return str(value), False
    return _type_placeholder(value), False


def _type_placeholder(value: Any) -> str:
    return _truncate(f"<{type(value).__name__}>")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _truncate(value: str) -> str:
    return value[:_STRING_LIMIT]


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _fit_json(value: dict[str, Any]) -> dict[str, Any]:
    if _json_size(value) <= _JSON_LIMIT:
        return value

    fitted: dict[str, Any] = {}
    for key, item in value.items():
        candidate = {**fitted, key: item}
        if _json_size(candidate) <= _JSON_LIMIT:
            fitted[key] = item
            continue
        placeholder = {**fitted, key: _TRUNCATED}
        if _json_size(placeholder) <= _JSON_LIMIT:
            fitted[key] = _TRUNCATED
        break
    return fitted
