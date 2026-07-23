"""Secure storage for model credentials."""

from __future__ import annotations

import base64
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from typing import Protocol

_SERVICE_NAME = "NaumiAgent"
_MODEL_API_KEY_ACCOUNT = "models.api_key"
_RUNTIME_PAYLOAD_KEY_ACCOUNT = "runtime.payload_encryption_key.v1"
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CREDENTIAL_CACHE: dict[tuple[int, str], tuple[object, str | None]] = {}
_CREDENTIAL_CACHE_LOCK = threading.Lock()
_RUNTIME_PAYLOAD_PROVISION_LOCK = threading.Lock()


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


def load_runtime_payload_key(
    *,
    backend: CredentialBackend | None = None,
) -> bytes | None:
    """Load the 256-bit Runtime payload key without creating one implicitly."""
    active_backend = backend or _default_backend()
    try:
        encoded = _load_cached_credential(
            active_backend,
            _RUNTIME_PAYLOAD_KEY_ACCOUNT,
        )
    except Exception as exc:
        raise CredentialStoreError(
            "无法读取 Runtime payload 系统密钥。"
        ) from exc
    if encoded is None:
        return None
    return decode_runtime_payload_key(encoded)


def resolve_runtime_payload_key(
    *,
    environment: Mapping[str, str] | None = None,
    backend: CredentialBackend | None = None,
) -> bytes:
    """Resolve an explicit environment key or the provisioned system key."""
    environ = os.environ if environment is None else environment
    injected = environ.get("NAUMI_RUNTIME_PAYLOAD_KEY", "").strip()
    if injected:
        return decode_runtime_payload_key(injected)
    stored = load_runtime_payload_key(backend=backend)
    if stored is None:
        raise CredentialStoreError(
            "Runtime payload 密钥尚未配置；请先执行显式密钥初始化。"
        )
    return stored


def provision_runtime_payload_key(
    *,
    backend: CredentialBackend | None = None,
    key_factory: Callable[[int], bytes] = secrets.token_bytes,
) -> bytes:
    """Create the Runtime payload key once through an explicit provision step."""
    active_backend = backend or _default_backend()
    with _RUNTIME_PAYLOAD_PROVISION_LOCK:
        existing = load_runtime_payload_key(backend=active_backend)
        if existing is not None:
            return existing
        key = key_factory(32)
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("Runtime payload key factory 必须返回 32 bytes。")
        encoded = base64.b64encode(key).decode("ascii")
        try:
            active_backend.set_password(
                _SERVICE_NAME,
                _RUNTIME_PAYLOAD_KEY_ACCOUNT,
                encoded,
            )
        except Exception as exc:
            raise CredentialStoreError(
                "无法写入 Runtime payload 系统密钥。"
            ) from exc
        _cache_credential(
            active_backend,
            _RUNTIME_PAYLOAD_KEY_ACCOUNT,
            encoded,
        )
        return key


def _model_api_key_account(provider: str | None) -> str:
    if provider is None:
        return _MODEL_API_KEY_ACCOUNT
    normalized = provider.strip().lower()
    if not _PROVIDER_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "provider ID 必须由字母、数字、点、下划线或短横线组成，长度为 1-64。"
        )
    return f"models.providers.{normalized}.api_key"


def decode_runtime_payload_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialStoreError(
            "Runtime payload 系统密钥格式无效。"
        ) from exc
    if len(decoded) != 32:
        raise CredentialStoreError("Runtime payload 系统密钥长度无效。")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise CredentialStoreError("Runtime payload 系统密钥不是 canonical Base64。")
    return decoded


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
