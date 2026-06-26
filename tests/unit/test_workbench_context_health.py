from __future__ import annotations

from naumi_agent.workbench.context_health import ContextHealthInput, evaluate_context_health
from naumi_agent.workbench.models import ContextHealth


def test_missing_acceptance_criteria_is_missing() -> None:
    result = evaluate_context_health(
        ContextHealthInput(
            has_goal=True,
            has_acceptance_criteria=False,
            minutes_since_sync=2,
            token_load_ratio=0.2,
            policy_conflict=False,
        )
    )

    assert result.health == ContextHealth.MISSING
    assert "缺少验收标准" in result.reasons


def test_stale_sync_is_stale() -> None:
    result = evaluate_context_health(
        ContextHealthInput(
            has_goal=True,
            has_acceptance_criteria=True,
            minutes_since_sync=90,
            token_load_ratio=0.2,
            policy_conflict=False,
        )
    )

    assert result.health == ContextHealth.STALE


def test_policy_conflict_wins_over_token_load() -> None:
    result = evaluate_context_health(
        ContextHealthInput(
            has_goal=True,
            has_acceptance_criteria=True,
            minutes_since_sync=2,
            token_load_ratio=0.95,
            policy_conflict=True,
        )
    )

    assert result.health == ContextHealth.CONFLICTED
