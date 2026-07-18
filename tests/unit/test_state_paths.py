"""Cross-platform contracts for user-owned Naumi state paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config import state_paths


def test_explicit_state_home_has_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "custom-state"
    monkeypatch.setenv("NAUMI_STATE_HOME", str(explicit))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored"))

    assert state_paths.resolve_naumi_state_home() == explicit.resolve()
    assert not explicit.exists()


@pytest.mark.parametrize(
    ("platform", "environment", "suffix"),
    [
        ("darwin", {}, ("Library", "Application Support", "NaumiAgent")),
        ("win32", {"LOCALAPPDATA": "local"}, ("local", "NaumiAgent")),
        ("linux", {"XDG_STATE_HOME": "xdg"}, ("xdg", "naumi-agent")),
    ],
)
def test_platform_native_state_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    environment: dict[str, str],
    suffix: tuple[str, ...],
) -> None:
    monkeypatch.delenv("NAUMI_STATE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(state_paths.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for key, value in environment.items():
        monkeypatch.setenv(key, str(tmp_path / value))

    expected = tmp_path.joinpath(*suffix).resolve()
    assert state_paths.resolve_naumi_state_home() == expected
    assert not expected.exists()


@pytest.mark.parametrize(
    ("platform", "suffix"),
    [
        ("win32", ("AppData", "Local", "NaumiAgent")),
        ("linux", (".local", "state", "naumi-agent")),
    ],
)
def test_platform_fallback_without_optional_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    suffix: tuple[str, ...],
) -> None:
    monkeypatch.delenv("NAUMI_STATE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(state_paths.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert state_paths.resolve_naumi_state_home() == tmp_path.joinpath(*suffix).resolve()
