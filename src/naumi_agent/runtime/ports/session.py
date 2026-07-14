"""Session persistence boundary consumed by the Agent runtime."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

SessionT = TypeVar("SessionT")


@runtime_checkable
class SessionPort(Protocol[SessionT]):
    """Persist sessions whose lifecycle is exclusively owned by one Engine."""

    async def create_session(
        self,
        title: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> SessionT:
        """Create and durably persist a new session."""
        ...

    async def save(self, session: SessionT) -> None:
        """Durably store the complete current session state."""
        ...

    async def load(self, session_id: str) -> SessionT | None:
        """Load one session, returning None when it does not exist."""
        ...

    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
    ) -> tuple[list[SessionT], int]:
        """Return one result page and the total matching session count."""
        ...

    async def delete(self, session_id: str) -> bool:
        """Delete one session and report whether a row was removed."""
        ...

    async def archive(self, session_id: str) -> bool:
        """Archive one session and report whether a row was updated."""
        ...

    async def close(self) -> None:
        """Release resources; repeated calls must remain safe."""
        ...


__all__ = ["SessionPort", "SessionT"]
