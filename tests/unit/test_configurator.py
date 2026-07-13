from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from naumi_agent.config.configurator import ConfigurationError, configure_project


def test_configure_kimi_updates_models_and_preserves_other_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default_model": "claude-sonnet-4-6"},
                "safety": {"permission_mode": "strict"},
                "custom_feature": {"enabled": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    stored: list[str] = []

    result = configure_project(
        config_path,
        provider="kimi",
        api_key="secret-value",
        store_credential=stored.append,
    )

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result.provider == "kimi"
    assert stored == ["secret-value"]
    assert persisted["models"] == {
        "provider": "kimi",
        "default_model": "openai/kimi-for-coding",
        "fast_model": "openai/kimi-for-coding",
        "reasoning_model": "openai/kimi-for-coding",
        "api_base": "https://api.kimi.com/coding/v1",
        "temperature": 1.0,
    }
    assert persisted["safety"] == {"permission_mode": "strict"}
    assert persisted["custom_feature"] == {"enabled": True}
    assert "api_key" not in persisted["models"]


def test_configure_custom_requires_model_and_api_base(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="自定义 provider"):
        configure_project(tmp_path / "config.yaml", provider="custom")


def test_configure_does_not_rewrite_file_when_credential_store_fails(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    original = "models:\n  default_model: old-model\n"
    config_path.write_text(original, encoding="utf-8")

    def fail_store(_value: str) -> None:
        raise RuntimeError("credential backend failed")

    with pytest.raises(ConfigurationError, match="系统凭据库"):
        configure_project(
            config_path,
            provider="kimi",
            api_key="secret-value",
            store_credential=fail_store,
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_configure_migrates_legacy_plaintext_key_before_removing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default_model: old-model\n  api_key: legacy-secret\n",
        encoding="utf-8",
    )
    stored: list[str] = []

    result = configure_project(
        config_path,
        provider="openai",
        store_credential=stored.append,
    )

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result.credential_updated is True
    assert stored == ["legacy-secret"]
    assert "api_key" not in persisted["models"]


def test_configure_validates_workspace_before_storing_credential(tmp_path: Path) -> None:
    stored: list[str] = []

    with pytest.raises(ConfigurationError, match="工作区不存在"):
        configure_project(
            tmp_path / "config.yaml",
            provider="kimi",
            api_key="secret-value",
            workspace=tmp_path / "missing",
            store_credential=stored.append,
        )

    assert stored == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"default_model": "claude-sonnet-4-6"}, "模型与 kimi provider 不匹配"),
        ({"api_base": "https://api.openai.com/v1"}, "API Base 与 kimi provider 不匹配"),
    ],
)
def test_known_provider_rejects_incompatible_overrides(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        configure_project(tmp_path / "config.yaml", provider="kimi", **overrides)
