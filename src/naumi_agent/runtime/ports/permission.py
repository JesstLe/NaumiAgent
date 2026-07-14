"""Permission policy boundary consumed by the Agent runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from naumi_agent.safety.permissions import (
    PermissionAwareTool,
    PermissionDecision,
    PermissionMode,
)


@runtime_checkable
class PermissionPort(Protocol):
    """Evaluate tool policy and own the current session permission mode."""

    @property
    def mode(self) -> PermissionMode:
        """Return the mode used by subsequent policy checks."""
        ...

    def set_mode(self, mode: PermissionMode) -> None:
        """Switch the active policy mode."""
        ...

    def check(
        self,
        tool_name: str,
        args: Mapping[str, object],
        tool: PermissionAwareTool | None = None,
    ) -> PermissionDecision:
        """Return a complete decision without mutating arguments."""
        ...

    def reset_counts(self) -> None:
        """Clear session call counts without changing policy mode."""
        ...


__all__ = ["PermissionPort"]
