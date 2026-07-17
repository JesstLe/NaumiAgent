"""Evidence-first self-evolution primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.evolution.evidence import EvolutionEvidence

__all__ = [
    "EvolutionEvidence",
    "adapt_harness_failure_evidence",
    "adapt_self_review_static_evidence",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    evidence = import_module("naumi_agent.evolution.evidence")
    return getattr(evidence, name)
