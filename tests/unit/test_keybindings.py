"""Tests for configurable CLI/TUI keybindings."""

from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.ui.keybindings import (
    KeybindingAction,
    KeybindingConfigError,
    build_keybindings,
    display_key,
    normalize_key_name,
    render_keybinding_help,
    to_textual_key,
)


def test_default_keybindings_include_cli_permission_and_mode_scopes() -> None:
    bindings = build_keybindings()

    assert bindings.keys_for(KeybindingAction.MODE_CYCLE, interface="cli") == ("s-tab",)
    assert bindings.keys_for(KeybindingAction.PERMISSION_BYPASS, interface="cli") == (
        "s-tab",
    )
    assert "Shift+Tab" in bindings.display_keys_for(
        KeybindingAction.MODE_CYCLE,
        interface="cli",
    )


def test_key_name_normalization_accepts_user_friendly_aliases() -> None:
    assert normalize_key_name("Ctrl+Y") == "c-y"
    assert normalize_key_name("control+x") == "c-x"
    assert normalize_key_name("Shift+Tab") == "s-tab"
    assert normalize_key_name("pgdn") == "pagedown"
    assert display_key("c-y") == "Ctrl+Y"
    assert to_textual_key("c-y") == "ctrl+y"
    assert to_textual_key("s-tab") == "shift+tab"


def test_config_overrides_replace_defaults() -> None:
    bindings = build_keybindings(
        {
            "copy_transcript": "Ctrl+X",
            "mode_cycle": ["f2"],
        }
    )

    assert bindings.keys_for(KeybindingAction.COPY_TRANSCRIPT, interface="cli") == (
        "c-x",
    )
    assert bindings.keys_for(KeybindingAction.MODE_CYCLE, interface="tui") == ("f2",)
    assert "Ctrl+Y" not in render_keybinding_help(bindings, interface="cli")


def test_conflict_detection_is_scoped_by_interface_and_permission_mode() -> None:
    build_keybindings({"permission_bypass": "Shift+Tab"})

    with pytest.raises(KeybindingConfigError, match="快捷键冲突"):
        build_keybindings({"copy_transcript": "Shift+Tab"})


def test_unknown_action_reports_available_actions() -> None:
    with pytest.raises(KeybindingConfigError, match="未知快捷键动作"):
        build_keybindings({"copy_all_the_things": "Ctrl+X"})


def test_app_config_loads_keybinding_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "models:",
                "  api_key: test-key",
                "keybindings:",
                "  copy_transcript: Ctrl+X",
                "  mode_cycle:",
                "    - F2",
            ]
        ),
        encoding="utf-8",
    )

    config = AppConfig.from_yaml(config_path)

    assert config.keybindings == {
        "copy_transcript": "Ctrl+X",
        "mode_cycle": ["F2"],
    }
    assert build_keybindings(config.keybindings).keys_for(
        KeybindingAction.COPY_TRANSCRIPT,
        interface="cli",
    ) == ("c-x",)


def test_help_renders_current_bindings_in_chinese() -> None:
    bindings = build_keybindings({"copy_transcript": "Ctrl+X"})
    help_text = render_keybinding_help(bindings, interface="cli")

    assert "## CLI 快捷键" in help_text
    assert "`Ctrl+X`" in help_text
    assert "复制/导出记录" in help_text
    assert "config.yaml" in help_text
