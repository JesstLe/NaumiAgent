"""Real filesystem proof for project-local Naumi configuration."""

from __future__ import annotations

import json
from pathlib import Path

from naumi_agent.config.paths import DEFAULT_CONFIG_PATH, resolve_config_path
from naumi_agent.config.settings import AppConfig
from naumi_agent.model.catalog import load_provider_catalog


def test_nested_launch_loads_parent_naumi_config_and_catalog(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src" / "package"
    naumi_dir = project / ".naumi"
    nested.mkdir(parents=True)
    naumi_dir.mkdir()
    (naumi_dir / "config.yaml").write_text(
        "models:\n"
        "  provider: loopback\n"
        "  catalog_path: providers.json\n"
        "  default_model: local\n",
        encoding="utf-8",
    )
    (naumi_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "loopback": {
                        "apiFormat": "ollama",
                        "baseURL": "http://127.0.0.1:11434",
                        "auth": {"type": "none"},
                        "models": {"local": {"upstreamId": "qwen:latest"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    selected = resolve_config_path(DEFAULT_CONFIG_PATH, cwd=nested)
    config = AppConfig.from_yaml(selected)
    catalog = load_provider_catalog(config.models.catalog_path or "")

    assert Path(selected) == naumi_dir / "config.yaml"
    assert config.memory.session_db_path == str(naumi_dir / "data" / "sessions.db")
    assert config.memory.vector_db_path == str(naumi_dir / "data" / "chroma")
    assert config.models.catalog_path == str(naumi_dir / "providers.json")
    assert catalog.providers["loopback"].models["local"].upstream_id == "qwen:latest"
