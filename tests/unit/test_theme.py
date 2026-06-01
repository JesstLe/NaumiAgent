"""Tests for shared theme and output style configuration."""

import pytest

from naumi_agent.ui.theme import (
    ThemeConfigError,
    build_ui_style_config,
    render_style_help,
)


def test_default_style_uses_dark_detailed_policy() -> None:
    style = build_ui_style_config()

    assert style.theme.name.value == "dark"
    assert style.output_style.name.value == "detailed"
    assert style.output_style.diff_max_files == 20
    assert style.ansi("diff_add", "+ok") == "\033[32m+ok\033[0m"


def test_high_contrast_theme_changes_semantic_tokens() -> None:
    style = build_ui_style_config(theme="high-contrast", output_style="debug")

    assert style.theme.name.value == "high_contrast"
    assert style.output_style.show_debug_metadata is True
    assert style.ansi("diff_add", "+ok") == "\033[92;1m+ok\033[0m"
    assert "#00ff00" in style.tui_css()


def test_minimal_theme_keeps_color_semantics_quiet() -> None:
    style = build_ui_style_config(theme="minimal", output_style="compact")

    assert style.theme.label == "极简"
    assert style.output_style.diff_max_files == 8
    assert style.ansi("danger", "错误") == "\033[37m错误\033[0m"


def test_unknown_theme_and_output_style_report_available_values() -> None:
    with pytest.raises(ThemeConfigError, match="未知主题"):
        build_ui_style_config(theme="neon")

    with pytest.raises(ThemeConfigError, match="未知输出风格"):
        build_ui_style_config(output_style="verbose")


def test_style_help_lists_current_and_available_values() -> None:
    style = build_ui_style_config(theme="high_contrast", output_style="silent_tools")
    help_text = render_style_help(style)

    assert "## 界面样式" in help_text
    assert "`high_contrast`" in help_text
    assert "`silent_tools`" in help_text
    assert "config.yaml" in help_text
