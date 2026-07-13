"""安全系统测试."""

import pytest

from naumi_agent.safety.budget import BudgetTracker, TokenBudget, TokenUsage
from naumi_agent.safety.guardrails import OutputGuardrail, SecurityError


class TestBudgetTracker:
    def test_unlimited_budget_never_exceeds_and_keeps_usage(self) -> None:
        tracker = BudgetTracker(TokenBudget())
        usage = TokenUsage(
            input_tokens=900_000,
            output_tokens=90_000,
            total_tokens=990_000,
            cost_usd=20.0,
        )

        tracker.track(usage, "test-model")

        assert tracker.budget.enabled is False
        assert tracker.is_exceeded() is False
        assert tracker.get_summary().remaining_usd is None
        assert tracker.total_input_tokens == 900_000
        assert tracker.total_output_tokens == 90_000
        assert tracker.total_cost_usd == 20.0

    @pytest.mark.parametrize(
        "budget",
        [
            TokenBudget(max_input_tokens=0),
            TokenBudget(max_output_tokens=0),
            TokenBudget(max_usd=0),
        ],
    )
    def test_zero_limit_is_already_exhausted(self, budget: TokenBudget) -> None:
        tracker = BudgetTracker(budget)

        assert tracker.is_exceeded() is True

    def test_track_usage(self) -> None:
        tracker = BudgetTracker(TokenBudget(max_usd=5.0))
        usage = TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500, cost_usd=0.01)
        tracker.track(usage, "claude-sonnet-4-6")

        assert tracker.total_input_tokens == 1000
        assert tracker.total_output_tokens == 500
        assert tracker.total_cost_usd > 0

    def test_budget_not_exceeded(self) -> None:
        tracker = BudgetTracker(TokenBudget(max_usd=5.0))
        usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, cost_usd=0.001)
        tracker.track(usage, "claude-sonnet-4-6")
        assert not tracker.is_exceeded()

    def test_budget_exceeded(self) -> None:
        tracker = BudgetTracker(TokenBudget(max_usd=0.001))
        usage = TokenUsage(
            input_tokens=100000, output_tokens=50000, total_tokens=150000, cost_usd=1.0
        )
        tracker.track(usage, "claude-sonnet-4-6")
        assert tracker.is_exceeded()

    def test_output_budget_exceeded(self) -> None:
        tracker = BudgetTracker(TokenBudget(max_output_tokens=100))
        usage = TokenUsage(input_tokens=10, output_tokens=101, total_tokens=111, cost_usd=0.0)
        tracker.track(usage, "claude-sonnet-4-6")
        assert tracker.is_exceeded()

    def test_get_summary(self) -> None:
        tracker = BudgetTracker(TokenBudget(max_usd=5.0))
        tracker.track(
            TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500, cost_usd=0.01),
            "claude-sonnet-4-6",
        )
        tracker.track(
            TokenUsage(input_tokens=500, output_tokens=200, total_tokens=700, cost_usd=0.005),
            "claude-haiku-4-5",
        )

        summary = tracker.get_summary()
        assert summary.total_input_tokens == 1500
        assert "claude-sonnet-4-6" in summary.model_breakdown
        assert "claude-haiku-4-5" in summary.model_breakdown


class TestOutputGuardrail:
    def test_redact_api_key(self) -> None:
        guardrail = OutputGuardrail()
        text = 'config api_key = "sk-abc123def456ghi789jkl012mno345"'
        result = guardrail.redact(text)
        assert "sk-abc123" not in result
        assert "[REDACTED]" in result

    def test_redact_github_token(self) -> None:
        guardrail = OutputGuardrail()
        text = "export GITHUB_TOKEN=ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvWwXxYy"
        result = guardrail.redact(text)
        assert "ghp_" not in result

    def test_pass_safe_content(self) -> None:
        guardrail = OutputGuardrail()
        text = "This is a normal response with no secrets."
        result = guardrail.validate(text)
        assert result == text

    def test_block_dangerous_command(self) -> None:
        guardrail = OutputGuardrail()
        with pytest.raises(SecurityError):
            guardrail.validate("Run this: rm -rf /")
