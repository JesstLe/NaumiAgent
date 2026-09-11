"""Engine selection tests: config parsing and terminal UI bridge wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.main import (
    TerminalUiLaunchError,
    _build_terminal_ui_command,
    _resolve_terminal_engine_provider,
)


def test_default_engine_is_naumi() -> None:
    config = AppConfig()
    assert config.engine.provider == "naumi"
    assert config.engine.pi.binary == "pi"


def test_engine_section_parses_from_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
engine:
  provider: pi
  pi:
    provider: zai-coding-cn
    model: glm-4.7
    extra_args: ["--verbose"]
""",
        encoding="utf-8",
    )
    config = AppConfig.from_yaml(config_file)
    assert config.engine.provider == "pi"
    assert config.engine.pi.provider == "zai-coding-cn"
    assert config.engine.pi.model == "glm-4.7"
    assert config.engine.pi.extra_args == ["--verbose"]


def test_invalid_engine_provider_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("engine:\n  provider: cuda\n", encoding="utf-8")
    with pytest.raises(Exception):
        AppConfig.from_yaml(config_file)


def test_bridge_command_switches_module_for_pi(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("", encoding="utf-8")

    cmd_naumi = _build_terminal_ui_command(str(config_file), engine_provider="naumi")
    bridge_naumi = json.loads(cmd_naumi[-1])
    assert "naumi_agent.ui.bridge" in bridge_naumi

    cmd_pi = _build_terminal_ui_command(str(config_file), engine_provider="pi")
    bridge_pi = json.loads(cmd_pi[-1])
    assert "naumi_agent.pi_engine" in bridge_pi
    assert bridge_pi[-2] == "--config"


def test_packaged_ui_rejects_pi_engine(tmp_path: Path) -> None:
    packaged = tmp_path / "terminal-ui-bin"
    packaged.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    packaged.chmod(0o755)
    with pytest.raises(TerminalUiLaunchError, match="pi"):
        _build_terminal_ui_command(
            "config.yaml",
            engine_provider="pi",
            terminal_ui_executable=packaged,
        )


def test_resolver_prefers_override(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("engine:\n  provider: pi\n", encoding="utf-8")
    assert _resolve_terminal_engine_provider(str(config_file), None) == "pi"
    assert _resolve_terminal_engine_provider(str(config_file), "naumi") == "naumi"
    assert _resolve_terminal_engine_provider(str(config_file), "PI") == "pi"


def test_pi_env_refs_expand_from_environment(tmp_path: Path, monkeypatch) -> None:
    from naumi_agent.pi_engine.env import resolve_env_refs

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    resolved = resolve_env_refs(
        {
            "ZAI_CODING_CN_API_KEY": "{env:OPENAI_API_KEY}",
            "PI_CUSTOM": "plain-value",
            "DROP_ME": "{env:NOT_SET_ANYWHERE}",
        }
    )
    assert resolved == {
        "ZAI_CODING_CN_API_KEY": "sk-test-value",
        "PI_CUSTOM": "plain-value",
    }
