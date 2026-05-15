"""预算追踪器."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from naumi_agent.model.router import TokenUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenBudget:
    max_input_tokens: int = 500_000
    max_output_tokens: int = 50_000
    max_usd: float = 5.0


@dataclass(frozen=True)
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: str


@dataclass
class BudgetSummary:
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    remaining_usd: float
    model_breakdown: dict[str, dict[str, float]]


class BudgetTracker:
    """Token 预算追踪."""

    def __init__(
        self,
        budget: TokenBudget,
        cost_fn: Callable[[str], dict[str, float]] | None = None,
    ) -> None:
        self.budget = budget
        self._records: list[UsageRecord] = []
        self._cost_fn = cost_fn

    def track(self, usage: TokenUsage, model: str) -> None:
        cost = usage.cost_usd
        self._records.append(UsageRecord(
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            timestamp=datetime.now().isoformat(),
        ))
        logger.debug(
            "Tracked %s: %d in + %d out = $%.4f",
            model, usage.input_tokens, usage.output_tokens, cost,
        )

    def is_exceeded(self) -> bool:
        return (
            self.total_input_tokens > self.budget.max_input_tokens
            or self.total_cost_usd > self.budget.max_usd
        )

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self._records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self._records)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self._records)

    @property
    def remaining_usd(self) -> float:
        return max(0, self.budget.max_usd - self.total_cost_usd)

    def get_summary(self) -> BudgetSummary:
        breakdown: dict[str, dict[str, float]] = {}
        for r in self._records:
            if r.model not in breakdown:
                breakdown[r.model] = {"input": 0.0, "output": 0.0, "cost": 0.0}
            breakdown[r.model]["input"] += r.input_tokens
            breakdown[r.model]["output"] += r.output_tokens
            breakdown[r.model]["cost"] += r.cost_usd

        return BudgetSummary(
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            total_cost_usd=round(self.total_cost_usd, 6),
            remaining_usd=round(self.remaining_usd, 4),
            model_breakdown=breakdown,
        )
