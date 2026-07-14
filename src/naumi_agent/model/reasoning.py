"""Shared model reasoning-effort vocabulary and immutable status values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ReasoningEffort(StrEnum):
    """Provider reasoning intensity values supported by NaumiAgent."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ReasoningEffortSetting(StrEnum):
    """Configured/runtime effort selection, including provider-default mode."""

    AUTO = "auto"
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @property
    def explicit(self) -> ReasoningEffort | None:
        if self is ReasoningEffortSetting.AUTO:
            return None
        return ReasoningEffort(self.value)


ReasoningEffortSource = Literal["runtime", "model", "global", "auto"]


@dataclass(frozen=True)
class ReasoningEffortStatus:
    """Authoritative effort resolution for one requested model."""

    model: str
    effective: ReasoningEffortSetting
    source: ReasoningEffortSource
    supported: tuple[ReasoningEffort, ...] = ()
    default: ReasoningEffort | None = None
    warning: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the bounded JSON-compatible status used by API/UI surfaces."""
        return {
            "model": self.model,
            "effective": self.effective.value,
            "source": self.source,
            "supported": [effort.value for effort in self.supported],
            "default": self.default.value if self.default is not None else None,
            "warning": self.warning,
        }


def reasoning_effort_values() -> str:
    """Return a stable Chinese-delimited list for validation errors."""
    return "、".join(effort.value for effort in ReasoningEffort)
