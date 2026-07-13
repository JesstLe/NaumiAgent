"""Deployment bootstrap tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from naumi_agent.config.settings import AppConfig
from naumi_agent.deploy import validate_deployment


def _write_config(path: Path, root: Path, *, include_key: bool = False) -> None:
    models = {"default_model": "openai/kimi-for-coding"}
    if include_key:
        models["api_key"] = "test-key"
    data = {
        "models": models,
        "memory": {
            "session_db_path": str(root / "data" / "sessions.db"),
            "vector_db_path": str(root / "data" / "chroma"),
        },
        "workspace_root": str(root / "workspace"),
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_validate_deployment_creates_required_dirs(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, tmp_path, include_key=True)

    report = validate_deployment(config_path, create_dirs=True, require_api_key=True)

    assert report.ok
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "workspace").is_dir()


def test_validate_deployment_requires_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NAUMI_MODELS__API_KEY", raising=False)
    monkeypatch.setitem(AppConfig.model_config, "env_file", None)
    monkeypatch.setattr("naumi_agent.config.settings.load_model_api_key", lambda: None)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, tmp_path)
    content = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    content["models"]["api_key"] = ""
    config_path.write_text(
        yaml.safe_dump(content, sort_keys=False),
        encoding="utf-8",
    )

    report = validate_deployment(config_path, create_dirs=True, require_api_key=True)

    assert not report.ok
    assert any("API Key" in error for error in report.errors)


def test_container_config_allows_env_secret_override(monkeypatch) -> None:
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "env-key")
    monkeypatch.setenv("NAUMI_MODELS__API_BASE", "https://example.test/v1")
    monkeypatch.setenv("NAUMI_API__API_KEYS", '["api-key"]')

    config = AppConfig.from_yaml("deploy/config.container.yaml")

    assert config.models.api_key == "env-key"
    assert config.models.api_base == "https://example.test/v1"
    assert config.api.api_keys == ["api-key"]


def test_api_config_path_uses_env(monkeypatch) -> None:
    from naumi_agent.api.app import resolve_config_path

    monkeypatch.setenv("NAUMI_CONFIG", "/tmp/naumi-config.yaml")

    assert resolve_config_path() == "/tmp/naumi-config.yaml"
