"""Static safety and contract tests for the Windows bootstrap script."""

from __future__ import annotations

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


def test_setup_script_creates_config_only_when_missing() -> None:
    script = _script()

    assert "Test-Path -LiteralPath $configPath" in script
    assert 'workspace_root: "."' in script
    assert '    - "."' in script
    assert 'host: "127.0.0.1"' in script
    assert "enabled: false" in script


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
