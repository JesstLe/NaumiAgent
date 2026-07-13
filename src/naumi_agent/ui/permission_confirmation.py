"""Public permission-confirmation helpers for terminal UI transports."""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

_REDACTED = "[已隐藏]"
_TRUNCATED = "[已截断]"
_NON_FINITE_FLOAT = "[非有限浮点数]"
_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "authorization", "cookie")
_STRING_LIMIT = 160
_COLLECTION_LIMIT = 50
_JSON_LIMIT = 1200

ChallengeStatus = Literal["valid", "unknown", "mismatch", "expired", "consumed"]


def summarize_arguments(arguments: Any) -> dict[str, Any]:
    """Return a bounded, JSON-safe public summary without sensitive values."""
    normalized = _summarize(arguments)
    if not isinstance(normalized, dict):
        normalized = {"value": normalized}
    return _fit_json(normalized)


def _summarize(value: Any) -> Any:
    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:_COLLECTION_LIMIT]:
            original_key = str(raw_key)
            key = _truncate(original_key)
            summary[key] = _REDACTED if _is_sensitive(original_key) else _summarize(raw_value)
        return summary
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_summarize(item) for item in list(value)[:_COLLECTION_LIMIT]]
    if isinstance(value, str):
        return _truncate(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _NON_FINITE_FLOAT
    if isinstance(value, bytes):
        return "<bytes>"
    return _truncate(f"<{type(value).__name__}>")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _truncate(value: str) -> str:
    return value[:_STRING_LIMIT]


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


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
