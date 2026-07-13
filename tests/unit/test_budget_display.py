"""Shared runtime budget display tests."""

from naumi_agent.ui.budget import format_budget_detail


def test_budget_display_distinguishes_unlimited_and_finite() -> None:
    assert (
        format_budget_detail({"enabled": False, "used_usd": 0.0123})
        == "不限 · 已用 $0.0123"
    )
    assert "$0.0123/$2.00 (0.6%)" in format_budget_detail(
        {
            "enabled": True,
            "used_usd": 0.0123,
            "max_usd": 2.0,
            "cost_percentage": 0.6,
        }
    )


def test_budget_display_supports_token_only_limits() -> None:
    detail = format_budget_detail(
        {
            "enabled": True,
            "used_usd": 0,
            "max_usd": None,
            "input_tokens": 1_500,
            "max_input_tokens": 2_000,
            "output_tokens": 30,
            "max_output_tokens": 100,
        }
    )

    assert "不限费用 · 已用 $0.0000" in detail
    assert "输入 1,500/2,000" in detail
    assert "输出 30/100" in detail


def test_budget_display_never_emits_non_finite_numbers() -> None:
    detail = format_budget_detail(
        {
            "enabled": True,
            "used_usd": float("nan"),
            "max_usd": float("inf"),
            "cost_percentage": float("inf"),
        }
    )

    assert "nan" not in detail.lower()
    assert "inf" not in detail.lower()
