from __future__ import annotations

import pytest

from naumi_agent.config.credentials import (
    CredentialStoreError,
    load_model_api_key,
    store_model_api_key,
)


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))


class _FailingBackend:
    def set_password(self, _service: str, _account: str, _value: str) -> None:
        raise RuntimeError("backend details")

    def get_password(self, _service: str, _account: str) -> str | None:
        raise RuntimeError("backend details")


def test_model_api_key_round_trips_through_backend() -> None:
    backend = _MemoryBackend()

    store_model_api_key("secret-value", backend=backend)

    assert load_model_api_key(backend=backend) == "secret-value"


def test_store_rejects_empty_model_api_key() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        store_model_api_key("  ", backend=_MemoryBackend())


def test_backend_errors_do_not_expose_secret() -> None:
    with pytest.raises(CredentialStoreError) as exc_info:
        store_model_api_key("secret-value", backend=_FailingBackend())

    assert "secret-value" not in str(exc_info.value)
