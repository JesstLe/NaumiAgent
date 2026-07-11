"""In-memory broker for API-hosted tool permission confirmations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

_ALLOWED_DECISIONS = frozenset({"allow", "deny", "bypass"})


@dataclass(frozen=True)
class _PendingPermission:
    session_id: str
    call_id: str
    future: asyncio.Future[str]


class PermissionApprovalBroker:
    """Wait for a user decision without exposing tool arguments to the API."""

    def __init__(self, *, timeout_seconds: float = 180.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._pending: dict[tuple[str, str], _PendingPermission] = {}
        self._lock = asyncio.Lock()

    async def confirm(self, payload: dict[str, Any]) -> str:
        """Wait for the matching authenticated API client to resolve a request."""
        session_id = str(payload.get("session_id", "")).strip()
        call_id = str(payload.get("call_id", "")).strip()
        if not session_id or not call_id:
            return "deny"

        key = (session_id, call_id)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = _PendingPermission(
            session_id=session_id,
            call_id=call_id,
            future=future,
        )
        async with self._lock:
            if key in self._pending:
                return "deny"
            self._pending[key] = pending

        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return "deny"
        finally:
            async with self._lock:
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)

    async def resolve(self, session_id: str, call_id: str, decision: str) -> bool:
        """Resolve only a matching pending request with an allowed decision."""
        normalized_decision = decision.strip().lower()
        if normalized_decision not in _ALLOWED_DECISIONS:
            return False

        key = (session_id.strip(), call_id.strip())
        async with self._lock:
            pending = self._pending.get(key)
            if pending is None or pending.future.done():
                return False
            pending.future.set_result(normalized_decision)
            return True

    async def close(self) -> None:
        """Deny unresolved requests before the daemon shuts down."""
        async with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_result("deny")
