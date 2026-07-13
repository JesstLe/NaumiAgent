"""Secure storage for model credentials."""

from __future__ import annotations

from typing import Protocol

_SERVICE_NAME = "NaumiAgent"
_MODEL_API_KEY_ACCOUNT = "models.api_key"


class CredentialBackend(Protocol):
    def set_password(self, service: str, account: str, value: str) -> None: ...

    def get_password(self, service: str, account: str) -> str | None: ...


class CredentialStoreError(RuntimeError):
    """Raised when the operating-system credential store is unavailable."""


def store_model_api_key(
    value: str,
    *,
    backend: CredentialBackend | None = None,
) -> None:
    """Persist the active model API key without writing it to project files."""
    if not value.strip():
        raise ValueError("模型 API Key 不能为空。")
    active_backend = backend or _default_backend()
    try:
        active_backend.set_password(_SERVICE_NAME, _MODEL_API_KEY_ACCOUNT, value)
    except Exception as exc:
        raise CredentialStoreError(
            "无法写入系统凭据库，请检查系统凭据服务是否可用。"
        ) from exc


def load_model_api_key(*, backend: CredentialBackend | None = None) -> str | None:
    """Load the active model API key from the operating-system credential store."""
    active_backend = backend or _default_backend()
    try:
        value = active_backend.get_password(_SERVICE_NAME, _MODEL_API_KEY_ACCOUNT)
    except Exception as exc:
        raise CredentialStoreError(
            "无法读取系统凭据库，请检查系统凭据服务是否可用。"
        ) from exc
    return value if value and value.strip() else None


def _default_backend() -> CredentialBackend:
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreError(
            "系统凭据组件未安装，请重新安装 NaumiAgent。"
        ) from exc
    return keyring
