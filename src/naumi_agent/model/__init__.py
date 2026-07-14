"""NaumiAgent 模型层."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from naumi_agent.model.router import ModelRouter, ModelTier

__all__ = ["ModelRouter", "ModelTier"]


def __getattr__(name: str) -> Any:
    """Load router exports lazily so config can import model value types."""
    if name in __all__:
        from naumi_agent.model.router import ModelRouter, ModelTier

        return {"ModelRouter": ModelRouter, "ModelTier": ModelTier}[name]
    raise AttributeError(name)
