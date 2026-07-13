"""In-memory session-scoped permission grants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(frozen=True)
class PermissionGrant:
    """An immutable grant bound to one session and canonical tool family."""

    grant_id: str
    session_id: str
    tool_family: str
    created_at: str
    expires_at: str | None
    source_request_id: str


class PermissionGrantStore:
    """Manage non-persistent grants for the lifetime of an active session."""

    def __init__(self) -> None:
        self._grants_by_id: dict[str, PermissionGrant] = {}
        self._grants_by_scope: dict[tuple[str, str], PermissionGrant] = {}

    def create(
        self,
        session_id: str,
        tool_family: str,
        source_request_id: str,
    ) -> PermissionGrant:
        """Create or return the active grant for a session and tool family."""
        self._validate_scope(session_id, tool_family)
        scope = (session_id, tool_family)
        existing = self._grants_by_scope.get(scope)
        if existing is not None:
            return existing

        grant = PermissionGrant(
            grant_id=uuid4().hex,
            session_id=session_id,
            tool_family=tool_family,
            created_at=datetime.now(UTC).isoformat(),
            expires_at=None,
            source_request_id=source_request_id,
        )
        self._grants_by_id[grant.grant_id] = grant
        self._grants_by_scope[scope] = grant
        return grant

    def allows(self, session_id: str, tool_family: str) -> bool:
        """Return whether this exact session and family have an active grant."""
        return (session_id, tool_family) in self._grants_by_scope

    def list_session(self, session_id: str) -> tuple[PermissionGrant, ...]:
        """Return immutable records for the requested session."""
        return tuple(
            grant
            for grant in self._grants_by_id.values()
            if grant.session_id == session_id
        )

    def list_all(self) -> tuple[PermissionGrant, ...]:
        """Return an immutable snapshot of every active grant in creation order."""
        return tuple(self._grants_by_id.values())

    def revoke(self, grant_id: str, session_id: str) -> bool:
        """Revoke a matching grant without allowing cross-session removal."""
        grant = self._grants_by_id.get(grant_id)
        if grant is None or grant.session_id != session_id:
            return False

        del self._grants_by_id[grant_id]
        del self._grants_by_scope[(grant.session_id, grant.tool_family)]
        return True

    def revoke_session(self, session_id: str) -> int:
        """Revoke every grant for one session and return the removed count."""
        grants = self.list_session(session_id)
        for grant in grants:
            self.revoke(grant.grant_id, session_id)
        return len(grants)

    def clear(self) -> None:
        """Remove all in-memory grants."""
        self._grants_by_id.clear()
        self._grants_by_scope.clear()

    @staticmethod
    def _validate_scope(session_id: str, tool_family: str) -> None:
        if not session_id or not session_id.strip():
            raise ValueError("session_id must not be blank")
        if not tool_family or not tool_family.strip():
            raise ValueError("tool_family must not be blank")
