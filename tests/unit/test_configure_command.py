from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

import naumi_agent.config.configurator as configurator
from naumi_agent.main import app

runner = CliRunner()


def test_configure_command_reads_key_from_stdin_without_echo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    stored: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        configurator,
        "store_model_api_key",
        lambda value, *, provider=None: stored.append((provider, value)),
    )

    result = runner.invoke(
        app,
        [
            "configure",
            "--provider",
            "kimi",
            "--config",
            str(config_path),
            "--api-key-stdin",
        ],
        input="secret-value\n",
    )

    assert result.exit_code == 0
    assert stored == [("kimi", "secret-value")]
    assert "secret-value" not in result.output
    assert "openai/kimi-for-coding" in result.output
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "api_key" not in persisted["models"]


def test_configure_non_interactive_requires_provider(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["configure", "--non-interactive", "--config", str(tmp_path / "config.yaml")],
    )

    assert result.exit_code == 2
    assert "非交互模式必须指定 --provider" in result.output


def test_configure_default_updates_active_legacy_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    legacy = tmp_path / "config.yaml"
    legacy.write_text(
        "models:\n  provider: kimi\n  default_model: old-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["configure", "--non-interactive", "--provider", "openai"],
    )

    assert result.exit_code == 0
    persisted = yaml.safe_load(legacy.read_text(encoding="utf-8"))
    assert persisted["models"]["provider"] == "openai"
    assert not (tmp_path / ".naumi" / "config.yaml").exists()


def test_configure_default_creates_project_naumi_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["configure", "--non-interactive", "--provider", "kimi"],
    )

    modern = tmp_path / ".naumi" / "config.yaml"
    assert result.exit_code == 0
    assert modern.is_file()
    assert not (tmp_path / "config.yaml").exists()
    assert "api_key" not in yaml.safe_load(modern.read_text(encoding="utf-8"))[
        "models"
    ]


def test_configure_help_does_not_offer_plaintext_key_argument() -> None:
    result = runner.invoke(app, ["configure", "--help"], terminal_width=120)

    assert result.exit_code == 0
    assert "--api-key-stdin" in result.output
    assert "--api-key " not in result.output
