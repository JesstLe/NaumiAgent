"""Secure storage for model credentials."""

from __future__ import annotations

import re
import threading
from typing import Protocol

_SERVICE_NAME = "NaumiAgent"
_MODEL_API_KEY_ACCOUNT = "models.api_key"
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CREDENTIAL_CACHE: dict[tuple[int, str], tuple[object, str | None]] = {}
_CREDENTIAL_CACHE_LOCK = threading.Lock()


class CredentialBackend(Protocol):
    def set_password(self, service: str, account: str, value: str) -> None: ...

    def get_password(self, service: str, account: str) -> str | None: ...


class CredentialStoreError(RuntimeError):
    """Raised when the operating-system credential store is unavailable."""


def store_model_api_key(
    value: str,
    *,
    provider: str | None = None,
    backend: CredentialBackend | None = None,
) -> None:
    """Persist the active model API key without writing it to project files."""
    if not value.strip():
        raise ValueError("模型 API Key 不能为空。")
    account = _model_api_key_account(provider)
    active_backend = backend or _default_backend()
    try:
        active_backend.set_password(_SERVICE_NAME, account, value)
    except Exception as exc:
        raise CredentialStoreError(
            "无法写入系统凭据库，请检查系统凭据服务是否可用。"
        ) from exc
    _cache_credential(active_backend, account, value)


def load_model_api_key(
    *,
    provider: str | None = None,
    backend: CredentialBackend | None = None,
    fallback_to_legacy: bool = True,
) -> str | None:
    """Load the active model API key from the operating-system credential store."""
    account = _model_api_key_account(provider)
    accounts = [account]
    if provider is not None and fallback_to_legacy:
        accounts.append(_MODEL_API_KEY_ACCOUNT)
    active_backend = backend or _default_backend()
    try:
        for candidate in accounts:
            value = _load_cached_credential(active_backend, candidate)
            if value and value.strip():
                return value
    except Exception as exc:
        raise CredentialStoreError(
            "无法读取系统凭据库，请检查系统凭据服务是否可用。"
        ) from exc
    return None


def _model_api_key_account(provider: str | None) -> str:
    if provider is None:
        return _MODEL_API_KEY_ACCOUNT
    normalized = provider.strip().lower()
    if not _PROVIDER_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "provider ID 必须由字母、数字、点、下划线或短横线组成，长度为 1-64。"
        )
    return f"models.providers.{normalized}.api_key"


def _load_cached_credential(
    backend: CredentialBackend,
    account: str,
) -> str | None:
    cache_key = (id(backend), account)
    with _CREDENTIAL_CACHE_LOCK:
        cached = _CREDENTIAL_CACHE.get(cache_key)
        if cached is not None and cached[0] is backend:
            return cached[1]
        value = backend.get_password(_SERVICE_NAME, account)
        _CREDENTIAL_CACHE[cache_key] = (backend, value)
        return value


def _cache_credential(
    backend: CredentialBackend,
    account: str,
    value: str | None,
) -> None:
    with _CREDENTIAL_CACHE_LOCK:
        _CREDENTIAL_CACHE[(id(backend), account)] = (backend, value)


def _default_backend() -> CredentialBackend:
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreError(
            "系统凭据组件未安装，请重新安装 NaumiAgent。"
        ) from exc
    return keyring
