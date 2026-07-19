"""Typed long-running services owned by the runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass

from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Services migrated out of AgentEngine so far."""

    terminal_runtime_lifecycle_factory: TerminalRuntimeLifecycleFactory

    def __post_init__(self) -> None:
        if not isinstance(
            self.terminal_runtime_lifecycle_factory,
            TerminalRuntimeLifecycleFactory,
        ):
            raise TypeError(
                "terminal_runtime_lifecycle_factory 必须是 "
                "TerminalRuntimeLifecycleFactory。"
            )


@dataclass(frozen=True, slots=True)
class RuntimeServiceOverrides:
    """Optional service instances; None alone selects the production default."""

    terminal_runtime_lifecycle_factory: TerminalRuntimeLifecycleFactory | None = None


__all__ = ["RuntimeServiceOverrides", "RuntimeServices"]
