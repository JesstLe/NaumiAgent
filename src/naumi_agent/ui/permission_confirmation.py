"""Public permission-confirmation helpers for terminal UI transports."""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from time import monotonic
from typing import Any, Literal

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

ChallengeStatus = Literal["valid", "unknown", "mismatch", "expired", "consumed"]


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


@dataclass
class _PermissionChallenge:
    request_id: str
    session_id: str
    call_id: str
    expires_at: float
    consumed: bool = False


class PermissionChallengeStore:
    """Issue short-lived, request-bound confirmation tokens exactly once."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._challenges: dict[str, _PermissionChallenge] = {}

    @property
    def count(self) -> int:
        """Return the number of tracked challenges."""
        return len(self._challenges)

    def issue(self, request_id: str, session_id: str, call_id: str) -> str:
        """Replace any live request challenge with one unguessable token."""
        self.discard_request(request_id)
        token = secrets.token_urlsafe()
        while token in self._challenges:
            token = secrets.token_urlsafe()
        self._challenges[token] = _PermissionChallenge(
            request_id=request_id,
            session_id=session_id,
            call_id=call_id,
            expires_at=self._clock() + self._ttl_seconds,
        )
        return token

    def consume(
        self,
        token: str,
        request_id: str,
        session_id: str,
        call_id: str,
    ) -> ChallengeStatus:
        """Validate and consume a challenge, never allowing replay."""
        challenge = self._challenges.get(token)
        if challenge is None:
            return "unknown"
        if challenge.consumed:
            return "consumed"
        if self._clock() >= challenge.expires_at:
            return "expired"
        if (
            challenge.request_id != request_id
            or challenge.session_id != session_id
            or challenge.call_id != call_id
        ):
            return "mismatch"
        challenge.consumed = True
        return "valid"

    def discard_request(self, request_id: str) -> None:
        """Remove every token associated with a completed request."""
        for token, challenge in list(self._challenges.items()):
            if challenge.request_id == request_id:
                del self._challenges[token]

    def clear(self) -> None:
        """Forget all outstanding challenges when the transport closes."""
        self._challenges.clear()
