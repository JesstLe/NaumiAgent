"""安全系统测试."""

import pytest

from naumi_agent.safety.behavior import BehaviorMonitor
from naumi_agent.safety.budget import BudgetTracker, TokenBudget, TokenUsage
from naumi_agent.safety.guardrails import OutputGuardrail, SecurityError


class TestBudgetTracker:
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


class TestBehaviorMonitor:
    def test_no_anomaly(self) -> None:
        monitor = BehaviorMonitor(high_freq_window=5, high_freq_seconds=0.001)
        for i in range(3):
            monitor.record_tool_call(f"tool_{i % 3}")

        warnings = monitor.check_anomalous_behavior()
        assert len(warnings) == 0

    def test_loop_detection(self) -> None:
        monitor = BehaviorMonitor(loop_window=10)
        for _ in range(12):
            monitor.record_tool_call("same_tool")

        warnings = monitor.check_anomalous_behavior()
        assert any("循环" in w for w in warnings)

    def test_high_frequency_detection(self) -> None:
        monitor = BehaviorMonitor(high_freq_window=5, high_freq_seconds=5.0)
        # 快速连续调用
        for _ in range(6):
            monitor.record_tool_call("tool_a")

        warnings = monitor.check_anomalous_behavior()
        assert any("频率" in w for w in warnings)

    def test_error_rate_detection(self) -> None:
        monitor = BehaviorMonitor(error_rate_threshold=0.5)
        for i in range(10):
            monitor.record_tool_call("tool", is_error=(i < 6))

        warnings = monitor.check_anomalous_behavior()
        assert any("失败率" in w for w in warnings)

    def test_reset(self) -> None:
        monitor = BehaviorMonitor()
        monitor.record_tool_call("tool")
        monitor.reset()
        warnings = monitor.check_anomalous_behavior()
        assert len(warnings) == 0


class TestBehaviorMonitorTurnTracking:
    """Tests for turn-level tracking and begin_turn()."""

    def test_begin_turn_saves_previous_tools(self) -> None:
        monitor = BehaviorMonitor()
        monitor.begin_turn(0)
        monitor.record_tool_call("tool_a")
        monitor.record_tool_call("tool_b")
        monitor.begin_turn(1)
        assert len(monitor._turn_records) == 1
        assert monitor._turn_records[0].tools == ["tool_a", "tool_b"]

    def test_begin_turn_resets_current_tools(self) -> None:
        monitor = BehaviorMonitor()
        monitor.begin_turn(0)
        monitor.record_tool_call("tool_a")
        monitor.begin_turn(1)
        assert monitor._current_turn_tools == []

    def test_begin_turn_empty_turn_not_saved(self) -> None:
        monitor = BehaviorMonitor()
        monitor.begin_turn(0)
        # No tools recorded in turn 0
        monitor.begin_turn(1)
        assert len(monitor._turn_records) == 0

    def test_reset_clears_turn_state(self) -> None:
        monitor = BehaviorMonitor()
        monitor.begin_turn(0)
        monitor.record_tool_call("tool_a")
        monitor.begin_turn(1)
        assert len(monitor._turn_records) == 1
        monitor.reset()
        assert len(monitor._turn_records) == 0
        assert monitor._current_turn_tools == []
        assert monitor._intervention_count == 0


class TestBehaviorMonitorIntervention:
    """Tests for approach oscillation detection and intervention."""

    def test_no_intervention_few_turns(self) -> None:
        monitor = BehaviorMonitor()
        monitor.begin_turn(0)
        monitor.record_tool_call("tool_a")
        monitor.begin_turn(1)
        monitor.record_tool_call("tool_b")
        assert monitor.check_intervention() is None

    def test_no_intervention_consistent_tools(self) -> None:
        """Same tool every turn = no oscillation."""
        monitor = BehaviorMonitor()
        for i in range(5):
            monitor.begin_turn(i)
            monitor.record_tool_call("tool_a")
            if i < 4:
                monitor.begin_turn(i + 1)
        # Current turn tools are ["tool_a"], turn_records have 4 entries all ["tool_a"]
        assert monitor.check_intervention() is None

    def test_oscillation_detected_different_tools(self) -> None:
        """Completely different tools each turn = oscillation."""
        monitor = BehaviorMonitor(
            approach_overlap_threshold=0.3,
            approach_diversity_window=6,
        )
        tools = ["analysis_chaos", "analysis_scale", "analysis_state", "analysis_eval"]
        for i, tool in enumerate(tools):
            monitor.begin_turn(i)
            monitor.record_tool_call(tool)

        intervention = monitor.check_intervention()
        assert intervention is not None
        assert intervention.action in ("inject_message", "force_converge")

    def test_progressive_intervention(self) -> None:
        """First oscillation → inject_message, second → force_converge."""
        monitor = BehaviorMonitor(max_interventions=2)

        # Build 3 turns with different tools
        for i, tool in enumerate(["analysis_chaos", "analysis_scale", "analysis_state"]):
            monitor.begin_turn(i)
            monitor.record_tool_call(tool)

        i1 = monitor.check_intervention()
        assert i1 is not None
        assert i1.action == "inject_message"
        assert i1.severity == 2

        # Continue oscillating
        monitor.begin_turn(3)
        monitor.record_tool_call("analysis_eval")
        monitor.begin_turn(4)
        monitor.record_tool_call("analysis_graph")

        i2 = monitor.check_intervention()
        assert i2 is not None
        assert i2.action == "force_converge"
        assert i2.severity == 3

    def test_force_converge_immediate_with_low_max(self) -> None:
        """With max_interventions=1, first oscillation triggers force_converge."""
        monitor = BehaviorMonitor(max_interventions=1)
        for i, tool in enumerate(["tool_a", "tool_b", "tool_c", "tool_d"]):
            monitor.begin_turn(i)
            monitor.record_tool_call(tool)

        intervention = monitor.check_intervention()
        assert intervention is not None
        assert intervention.action == "force_converge"

    def test_partial_overlap_not_oscillation(self) -> None:
        """50% tool overlap between turns is stable, not oscillation."""
        monitor = BehaviorMonitor(approach_overlap_threshold=0.3)
        # Turn 0: tool_a, tool_b
        monitor.begin_turn(0)
        monitor.record_tool_call("tool_a")
        monitor.record_tool_call("tool_b")
        # Turn 1: tool_b, tool_c (50% overlap with turn 0)
        monitor.begin_turn(1)
        monitor.record_tool_call("tool_b")
        monitor.record_tool_call("tool_c")
        # Turn 2: tool_c, tool_d (50% overlap with turn 1)
        monitor.begin_turn(2)
        monitor.record_tool_call("tool_c")
        monitor.record_tool_call("tool_d")

        assert monitor.check_intervention() is None

    def test_reset_clears_intervention_history(self) -> None:
        monitor = BehaviorMonitor()
        for i, tool in enumerate(["tool_a", "tool_b", "tool_c"]):
            monitor.begin_turn(i)
            monitor.record_tool_call(tool)
        monitor.check_intervention()  # Triggers first intervention

        monitor.reset()
        # After reset, no turn data → no intervention
        assert monitor.check_intervention() is None


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
