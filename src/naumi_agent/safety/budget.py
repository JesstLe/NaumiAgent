"""预算追踪器."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from naumi_agent.model.router import TokenUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenBudget:
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_usd: float | None = None

    @property
    def enabled(self) -> bool:
        """Return whether at least one cumulative budget limit is active."""
        return any(
            limit is not None
            for limit in (self.max_input_tokens, self.max_output_tokens, self.max_usd)
        )


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
    remaining_usd: float | None
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
        self._total_input: int = 0
        self._total_output: int = 0
        self._total_cost: float = 0.0

    def track(self, usage: TokenUsage, model: str) -> None:
        cost = usage.cost_usd
        self._records.append(
            UsageRecord(
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost,
                timestamp=datetime.now().isoformat(),
            )
        )
        self._total_input += usage.input_tokens
        self._total_output += usage.output_tokens
        self._total_cost += cost
        logger.debug(
            "Tracked %s: %d in + %d out = $%.4f",
            model,
            usage.input_tokens,
            usage.output_tokens,
            cost,
        )

    def is_exceeded(self) -> bool:
        return (
            self._is_limit_exceeded(self._total_input, self.budget.max_input_tokens)
            or self._is_limit_exceeded(self._total_output, self.budget.max_output_tokens)
            or self._is_limit_exceeded(self._total_cost, self.budget.max_usd)
        )

    @staticmethod
    def _is_limit_exceeded(total: int | float, limit: int | float | None) -> bool:
        if limit is None:
            return False
        if limit == 0:
            return True
        return total > limit

    @property
    def total_input_tokens(self) -> int:
        return self._total_input

    @property
    def total_output_tokens(self) -> int:
        return self._total_output

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost

    @property
    def remaining_usd(self) -> float | None:
        if self.budget.max_usd is None:
            return None
        return max(0, self.budget.max_usd - self._total_cost)

    def reset(self) -> None:
        """Reset all tracked usage (for new session)."""
        self._records.clear()
        self._total_input = 0
        self._total_output = 0
        self._total_cost = 0.0

    def get_summary(self) -> BudgetSummary:
        breakdown: dict[str, dict[str, float]] = {}
        for r in self._records:
            if r.model not in breakdown:
                breakdown[r.model] = {"input": 0.0, "output": 0.0, "cost": 0.0}
            breakdown[r.model]["input"] += r.input_tokens
            breakdown[r.model]["output"] += r.output_tokens
            breakdown[r.model]["cost"] += r.cost_usd

        remaining_usd = self.remaining_usd
        return BudgetSummary(
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            total_cost_usd=round(self.total_cost_usd, 6),
            remaining_usd=round(remaining_usd, 4) if remaining_usd is not None else None,
            model_breakdown=breakdown,
        )
