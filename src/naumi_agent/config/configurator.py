"""Validated, atomic project configuration updates."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import yaml

from naumi_agent.config.credentials import store_model_api_key


@dataclass(frozen=True)
class ProviderProfile:
    default_model: str
    fast_model: str
    reasoning_model: str
    api_base: str
    temperature: float


@dataclass(frozen=True)
class ConfigurationResult:
    config_path: Path
    provider: str
    default_model: str
    api_base: str
    credential_updated: bool


class ConfigurationError(ValueError):
    """Raised when a requested configuration cannot be applied safely."""


PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "kimi": ProviderProfile(
        default_model="openai/kimi-for-coding",
        fast_model="openai/kimi-for-coding",
        reasoning_model="openai/kimi-for-coding",
        api_base="https://api.kimi.com/coding/v1",
        temperature=1.0,
    ),
    "openai": ProviderProfile(
        default_model="gpt-4o",
        fast_model="gpt-4o-mini",
        reasoning_model="o3-mini",
        api_base="https://api.openai.com/v1",
        temperature=1.0,
    ),
    "anthropic": ProviderProfile(
        default_model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
        reasoning_model="claude-opus-4-7",
        api_base="https://api.anthropic.com/v1",
        temperature=1.0,
    ),
}
_PROVIDER_HOSTS = {
    "kimi": "api.kimi.com",
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
}


def configure_project(
    config_path: str | Path,
    *,
    provider: str,
    api_key: str | None = None,
    default_model: str | None = None,
    fast_model: str | None = None,
    reasoning_model: str | None = None,
    api_base: str | None = None,
    workspace: str | Path | None = None,
    permission_mode: str | None = None,
    store_credential: Callable[[str], None] | None = None,
) -> ConfigurationResult:
    """Validate and atomically update a project configuration."""
    normalized_provider = provider.strip().lower()
    profile = _resolve_profile(
        normalized_provider,
        default_model=default_model,
        fast_model=fast_model,
        reasoning_model=reasoning_model,
        api_base=api_base,
    )
    path = Path(config_path).expanduser().resolve()
    data = _read_config(path)
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        raise ConfigurationError("models 配置必须是映射结构。")

    resolved_workspace: Path | None = None
    if workspace is not None:
        resolved_workspace = Path(workspace).expanduser().resolve()
        if not resolved_workspace.is_dir():
            raise ConfigurationError(f"工作区不存在或不是目录：{resolved_workspace}")
    if permission_mode is not None and permission_mode not in {
        "strict",
        "moderate",
        "relaxed",
        "bypass",
    }:
        raise ConfigurationError("权限模式必须是 strict、moderate、relaxed 或 bypass。")
    if resolved_workspace is not None or permission_mode is not None:
        safety = data.setdefault("safety", {})
        if not isinstance(safety, dict):
            raise ConfigurationError("safety 配置必须是映射结构。")

    legacy_key = models.get("api_key")
    credential = api_key or (legacy_key if isinstance(legacy_key, str) else None)
    if credential is not None and not credential.strip():
        raise ConfigurationError("模型 API Key 不能为空。")
    if credential:
        try:
            (store_credential or store_model_api_key)(credential)
        except Exception as exc:
            raise ConfigurationError("无法写入系统凭据库，配置未发生变化。") from exc
        os.environ["NAUMI_MODELS__API_KEY"] = credential

    models.update(
        {
            "provider": normalized_provider,
            "default_model": profile.default_model,
            "fast_model": profile.fast_model,
            "reasoning_model": profile.reasoning_model,
            "api_base": profile.api_base,
            "temperature": profile.temperature,
        }
    )
    models.pop("api_key", None)

    if resolved_workspace is not None:
        data["workspace_root"] = str(resolved_workspace)
        safety["allowed_dirs"] = [str(resolved_workspace)]

    if permission_mode is not None:
        safety["permission_mode"] = permission_mode

    _atomic_write_yaml(path, data)
    return ConfigurationResult(
        config_path=path,
        provider=normalized_provider,
        default_model=profile.default_model,
        api_base=profile.api_base,
        credential_updated=bool(credential),
    )


def _resolve_profile(
    provider: str,
    *,
    default_model: str | None,
    fast_model: str | None,
    reasoning_model: str | None,
    api_base: str | None,
) -> ProviderProfile:
    if provider == "custom":
        if not default_model or not api_base:
            raise ConfigurationError("自定义 provider 必须提供默认模型和 API Base。")
        profile = ProviderProfile(
            default_model=default_model,
            fast_model=fast_model or default_model,
            reasoning_model=reasoning_model or default_model,
            api_base=api_base,
            temperature=1.0,
        )
    else:
        preset = PROVIDER_PROFILES.get(provider)
        if preset is None:
            choices = "、".join([*PROVIDER_PROFILES, "custom"])
            raise ConfigurationError(f"不支持的 provider；可选值：{choices}。")
        profile = ProviderProfile(
            default_model=default_model or preset.default_model,
            fast_model=fast_model or preset.fast_model,
            reasoning_model=reasoning_model or preset.reasoning_model,
            api_base=api_base or preset.api_base,
            temperature=preset.temperature,
        )
    _validate_api_base(profile.api_base)
    if not all(
        value.strip()
        for value in (profile.default_model, profile.fast_model, profile.reasoning_model)
    ):
        raise ConfigurationError("模型名称不能为空。")
    if provider != "custom":
        _validate_known_provider(provider, profile)
    return ProviderProfile(
        default_model=profile.default_model.strip(),
        fast_model=profile.fast_model.strip(),
        reasoning_model=profile.reasoning_model.strip(),
        api_base=profile.api_base.rstrip("/"),
        temperature=profile.temperature,
    )


def _validate_api_base(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("API Base 必须是有效的 HTTP 或 HTTPS 地址。")


def _validate_known_provider(provider: str, profile: ProviderProfile) -> None:
    hostname = (urlparse(profile.api_base).hostname or "").lower()
    if hostname != _PROVIDER_HOSTS[provider]:
        raise ConfigurationError(f"API Base 与 {provider} provider 不匹配；代理地址请使用 custom。")

    if not all(
        _model_matches_provider(provider, model)
        for model in (profile.default_model, profile.fast_model, profile.reasoning_model)
    ):
        raise ConfigurationError(f"模型与 {provider} provider 不匹配；代理模型请使用 custom。")


def validate_provider_configuration(
    *,
    provider: str | None,
    default_model: str,
    fast_model: str,
    reasoning_model: str,
    api_base: str | None,
    temperature: float,
) -> tuple[str | None, str | None]:
    """Return the resolved provider and a safe inconsistency description."""
    resolved = provider.strip().lower() if provider else _infer_provider(default_model, api_base)
    if resolved == "custom":
        return resolved, None
    if resolved not in PROVIDER_PROFILES:
        return resolved, "无法识别 provider，请运行 `naumi configure` 明确选择。"
    if api_base:
        hostname = (urlparse(api_base).hostname or "").lower()
        if hostname != _PROVIDER_HOSTS[resolved]:
            return resolved, f"API Base 与 {resolved} provider 不匹配。"
    if not all(
        _model_matches_provider(resolved, model)
        for model in (default_model, fast_model, reasoning_model)
    ):
        return resolved, f"模型与 {resolved} provider 不匹配。"
    if resolved == "kimi" and abs(temperature - 1.0) > 1e-9:
        return resolved, "Kimi Coding API 的 temperature 必须为 1.0。"
    return resolved, None


def _infer_provider(default_model: str, api_base: str | None) -> str | None:
    if api_base:
        hostname = (urlparse(api_base).hostname or "").lower()
        for provider, expected in _PROVIDER_HOSTS.items():
            if hostname == expected:
                return provider
    for provider in PROVIDER_PROFILES:
        if _model_matches_provider(provider, default_model):
            return provider
    return None


def _model_matches_provider(provider: str, model: str) -> bool:
    normalized = model.lower()
    if provider == "kimi":
        return normalized.startswith("openai/") and any(
            name in normalized for name in ("kimi", "moonshot")
        )
    if provider == "openai":
        return normalized.startswith(("gpt-", "o1", "o3", "o4", "openai/"))
    return normalized.startswith(("claude-", "anthropic/"))


def _read_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"无法读取配置文件：{path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError("配置文件根节点必须是映射结构。")
    return loaded


def _atomic_write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, sort_keys=False, allow_unicode=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigurationError(f"无法原子写入配置文件：{path}") from exc
    finally:
        temporary.unlink(missing_ok=True)
