from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console

import naumi_agent.cli.onboarding as onboarding
import naumi_agent.main as main_module
from naumi_agent.cli.onboarding import _build_config
from naumi_agent.config.credentials import CredentialStoreError


def _preset() -> dict[str, str]:
    return {
        "default_model": "openai/test-model",
        "fast_model": "openai/test-model",
        "reasoning_model": "openai/test-model",
        "api_base": "https://example.test/v1",
    }


def test_build_config_never_serializes_model_api_key(tmp_path: Path) -> None:
    config = _build_config(
        provider="custom",
        preset=_preset(),
        workspace=str(tmp_path),
        permission_mode="moderate",
    )

    assert "api_key" not in config["models"]
    assert config["models"]["provider"] == "custom"


def test_build_config_limits_default_permissions_to_workspace(tmp_path: Path) -> None:
    config = _build_config(
        provider="custom",
        preset=_preset(),
        workspace=str(tmp_path),
        permission_mode="moderate",
    )

    assert config["safety"]["allowed_dirs"] == [str(tmp_path)]


def test_build_config_keeps_runtime_budgets_unlimited_by_default(
    tmp_path: Path,
) -> None:
    config = _build_config(
        provider="custom",
        preset=_preset(),
        workspace=str(tmp_path),
        permission_mode="bypass",
    )

    safety = config["safety"]
    assert safety["max_turns"] == 50
    assert "max_budget_usd" not in safety
    assert "max_input_tokens" not in safety
    assert "max_output_tokens" not in safety


def test_run_onboarding_stores_key_outside_yaml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    stored: list[tuple[str | None, str]] = []
    answers = iter([str(tmp_path), "moderate"])

    monkeypatch.setattr(onboarding, "_choose_provider", lambda: "kimi")
    monkeypatch.setattr(onboarding, "_prompt_api_key", lambda _name: "secret-value")
    monkeypatch.setattr(onboarding.Prompt, "ask", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(onboarding.Confirm, "ask", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(onboarding, "_check_node_ui", lambda _root: None)
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "")
    monkeypatch.setattr(
        onboarding,
        "store_model_api_key",
        lambda value, *, provider=None: stored.append((provider, value)),
        raising=False,
    )

    assert onboarding.run_onboarding(config_path, project_root=tmp_path) is True

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert stored == [("kimi", "secret-value")]
    assert "api_key" not in persisted["models"]


def test_run_onboarding_reuses_environment_key_without_keyring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    answers = iter([str(tmp_path), "moderate"])
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "environment-secret")
    monkeypatch.setattr(onboarding, "_choose_provider", lambda: "kimi")
    monkeypatch.setattr(
        onboarding,
        "_prompt_api_key",
        lambda _name: "environment-secret",
    )
    monkeypatch.setattr(onboarding.Prompt, "ask", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(onboarding, "_check_node_ui", lambda _root: None)
    monkeypatch.setattr(
        onboarding,
        "store_model_api_key",
        lambda _value, **_kwargs: pytest.fail(
            "environment credentials must not require keyring"
        ),
    )

    assert onboarding.run_onboarding(config_path, project_root=tmp_path) is True


def test_skipped_key_recommends_secure_configuration_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(onboarding, "console", Console(file=output, force_terminal=False))
    monkeypatch.setattr(onboarding, "_choose_provider", lambda: "kimi")
    monkeypatch.setattr(onboarding, "_prompt_api_key", lambda _name: "")

    assert onboarding.run_onboarding(
        tmp_path / ".naumi" / "config.yaml",
        project_root=tmp_path,
    ) is False

    text = output.getvalue()
    assert "naumi configure" in text
    assert "NAUMI_MODELS__API_KEY" in text
    assert "config.yaml 设置" not in text


def test_migrate_legacy_key_moves_secret_before_rewriting_yaml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  provider: openai\n  default_model: test-model\n  api_key: legacy-secret\n",
        encoding="utf-8",
    )
    stored: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        onboarding,
        "store_model_api_key",
        lambda value, *, provider=None: stored.append((provider, value)),
    )
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "")

    assert onboarding.migrate_legacy_model_api_key(config_path) is True

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert stored == [("openai", "legacy-secret")]
    assert "api_key" not in persisted["models"]
    assert onboarding.os.environ["NAUMI_MODELS__API_KEY"] == "legacy-secret"


def test_migrate_legacy_key_keeps_file_when_secure_store_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    original = "models:\n  api_key: legacy-secret\n"
    config_path.write_text(original, encoding="utf-8")

    def fail_store(_value: str, **_kwargs) -> None:
        raise CredentialStoreError("secure store unavailable")

    monkeypatch.setattr(onboarding, "store_model_api_key", fail_store)

    with pytest.raises(CredentialStoreError, match="secure store unavailable"):
        onboarding.migrate_legacy_model_api_key(config_path)

    assert config_path.read_text(encoding="utf-8") == original


def test_needs_onboarding_loads_only_configured_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  provider: anthropic\n  default_model: claude-test\n",
        encoding="utf-8",
    )
    requested: list[str | None] = []
    monkeypatch.delenv("NAUMI_MODELS__API_KEY", raising=False)
    monkeypatch.setattr(
        onboarding,
        "load_model_api_key",
        lambda *, provider=None: requested.append(provider) or "anthropic-secret",
    )

    assert onboarding.needs_onboarding(config_path) is False
    assert requested == ["anthropic"]


def test_main_prepares_legacy_credentials_before_onboarding_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "_resolve_config_path", lambda _path: str(config_path))
    monkeypatch.setattr(
        onboarding,
        "migrate_legacy_model_api_key",
        lambda _path: calls.append("migrate") or False,
    )
    monkeypatch.setattr(
        onboarding,
        "needs_onboarding",
        lambda _path: calls.append("check") or False,
    )

    main_module._ensure_onboarding_ready("config.yaml")

    assert calls == ["migrate", "check"]


def test_node_check_recommends_only_explicit_legacy_fallbacks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(onboarding, "console", Console(file=output, force_terminal=False))
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: None)

    onboarding._check_node_ui(tmp_path)

    text = output.getvalue()
    assert "naumi chat --classic" in text
    assert "naumi ui --legacy" in text
    assert "全屏 CLI（naumi）" not in text


def test_missing_key_message_does_not_recommend_plaintext_yaml(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(main_module, "console", Console(file=output, force_terminal=False))

    main_module._check_api_key(SimpleNamespace(models=SimpleNamespace(api_key=None)))

    text = output.getvalue()
    assert "系统凭据库" in text
    assert "config.yaml 中配置 api_key" not in text


def test_onboarding_reports_keyless_search_as_ready(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(onboarding, "console", Console(file=output, force_terminal=False))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    onboarding._report_search_readiness()

    text = output.getvalue()
    assert "网络搜索" in text
    assert "零配置" in text
    assert "无需搜索 API Key" in text


def test_onboarding_masks_enhanced_search_credentials(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(onboarding, "console", Console(file=output, force_terminal=False))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "search-secret-value")

    onboarding._report_search_readiness()

    text = output.getvalue()
    assert "已增强" in text
    assert "search-secret-value" not in text
