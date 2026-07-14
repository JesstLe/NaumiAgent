"""Project configuration path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config.paths import DEFAULT_CONFIG_PATH, resolve_config_path


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("log_level: INFO\n", encoding="utf-8")
    return path


def test_nearest_modern_config_wins(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "packages" / "agent"
    child.mkdir(parents=True)
    _write(tmp_path / ".naumi" / "config.yaml")
    nearest = _write(project / ".naumi" / "config.yaml")

    assert resolve_config_path(DEFAULT_CONFIG_PATH, cwd=child) == str(nearest)


def test_modern_parent_config_wins_over_nearer_legacy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "src" / "pkg"
    child.mkdir(parents=True)
    modern = _write(project / ".naumi" / "config.yaml")
    _write(child / "config.yaml")

    assert resolve_config_path(DEFAULT_CONFIG_PATH, cwd=child) == str(modern)


def test_nearest_legacy_config_is_compatibility_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "src" / "pkg"
    child.mkdir(parents=True)
    _write(tmp_path / "config.yaml")
    nearest = _write(project / "config.yaml")

    assert resolve_config_path(DEFAULT_CONFIG_PATH, cwd=child) == str(nearest)


def test_missing_default_targets_current_naumi_directory(tmp_path: Path) -> None:
    expected = tmp_path / ".naumi" / "config.yaml"

    assert resolve_config_path(DEFAULT_CONFIG_PATH, cwd=tmp_path) == str(expected)


def test_directory_is_not_treated_as_configuration(tmp_path: Path) -> None:
    child = tmp_path / "project" / "child"
    directory = child / ".naumi" / "config.yaml"
    directory.mkdir(parents=True)
    legacy = _write(tmp_path / "project" / "config.yaml")

    assert resolve_config_path(DEFAULT_CONFIG_PATH, cwd=child) == str(legacy)


def test_explicit_existing_relative_path_is_authoritative(tmp_path: Path) -> None:
    explicit = _write(tmp_path / "profiles" / "coding.yaml")
    _write(tmp_path / ".naumi" / "config.yaml")

    assert resolve_config_path("profiles/coding.yaml", cwd=tmp_path) == str(explicit)


def test_explicit_missing_relative_path_never_falls_back(tmp_path: Path) -> None:
    _write(tmp_path / ".naumi" / "config.yaml")
    expected = tmp_path / "profiles" / "missing.yaml"

    assert resolve_config_path("profiles/missing.yaml", cwd=tmp_path) == str(expected)


def test_explicit_absolute_path_is_preserved(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere" / "config.yaml"

    assert resolve_config_path(explicit, cwd=tmp_path / "ignored") == str(explicit)


def test_explicit_home_path_is_expanded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert resolve_config_path("~/.naumi/profile.yaml", cwd=tmp_path) == str(
        home / ".naumi" / "profile.yaml"
    )
