"""Static safety and contract tests for the Windows bootstrap script."""

from __future__ import annotations

import json
from pathlib import Path


def _script() -> str:
    return (
        Path(__file__).resolve().parents[2] / "scripts" / "windows" / "setup.ps1"
    ).read_text(encoding="utf-8")


def test_setup_script_checks_required_runtimes() -> None:
    script = _script()

    assert 'Require-Command "python"' in script
    assert 'Require-Command "uv"' in script
    assert 'Require-Command "node"' in script
    assert "Resolve-GitBash" in script
    assert "uv.Source sync --python 3.12 --extra dev" in script
    assert "uv.Source run playwright install chromium" in script
    assert (
        "uv.Source tool install --editable --force --python 3.12 $repoRoot"
        in script
    )
    assert 'Get-Command "naumiagent"' in script
    assert 'Write-Host "  TUI:  naumi"' in script
    assert "Windows compatibility alias:  naumiagent --tui" in script


def test_setup_script_creates_config_only_when_missing() -> None:
    script = _script()

    assert '$configDir = Join-Path $repoRoot ".naumi"' in script
    assert '$configPath = Join-Path $configDir "config.yaml"' in script
    assert '$legacyConfigPath = Join-Path $repoRoot "config.yaml"' in script
    assert "Test-Path -LiteralPath $configPath" in script
    assert "Test-Path -LiteralPath $legacyConfigPath" in script
    assert "New-Item -ItemType Directory -Path $configDir" in script
    assert 'workspace_root: "."' in script
    assert '    - "."' in script
    assert 'host: "127.0.0.1"' in script
    assert "enabled: false" in script
    assert "temperature: 1.0" in script
    assert "long_term_enabled: false" in script
    assert "max_budget_usd: null" in script
    assert "max_turns: 50" in script


def test_setup_script_verifies_the_shared_resolved_config() -> None:
    script = _script()

    assert "from naumi_agent.config.paths import DEFAULT_CONFIG_PATH, resolve_config_path" in script
    assert "AppConfig.from_yaml(resolve_config_path(DEFAULT_CONFIG_PATH))" in script


def test_setup_script_reads_user_key_without_printing_or_accepting_it() -> None:
    script = _script()

    assert '[Environment]::GetEnvironmentVariable("NAUMI_MODELS__API_KEY", "User")' in script
    assert "param()" in script
    assert "Read-Host" not in script
    assert "sk-" + "kimi-" not in script
    assert "api_key:" not in script


def test_setup_script_rejects_wsl_bash_launcher() -> None:
    script = _script()

    assert "Windows\\\\System32\\\\bash" in script
    assert "NAUMI_GIT_BASH" in script


def test_setup_script_has_utf8_bom_for_windows_powershell_51() -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "windows" / "setup.ps1"

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_setup_script_passes_multiline_python_as_one_argument() -> None:
    script = _script()

    assert "$verifyCode = @'" in script
    assert "run python -c $verifyCode" in script


def test_terminal_ui_npm_scripts_do_not_depend_on_shell_glob_expansion() -> None:
    package_path = (
        Path(__file__).resolve().parents[2] / "frontend" / "terminal-ui" / "package.json"
    )
    scripts = json.loads(package_path.read_text(encoding="utf-8"))["scripts"]

    assert "*" not in scripts["check"]
    assert "*" not in scripts["test"]
    assert scripts["check"] == "node scripts/check-syntax.js"
    assert scripts["test"] == "node scripts/run-tests.js"
