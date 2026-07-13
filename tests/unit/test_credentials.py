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
        self.get_calls: list[tuple[str, str]] = []

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        self.get_calls.append((service, account))
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


def test_provider_keys_are_stored_independently() -> None:
    backend = _MemoryBackend()

    store_model_api_key("kimi-secret", provider="kimi", backend=backend)
    store_model_api_key("openai-secret", provider="openai", backend=backend)
    store_model_api_key("anthropic-secret", provider="anthropic", backend=backend)

    assert load_model_api_key(provider="kimi", backend=backend) == "kimi-secret"
    assert load_model_api_key(provider="openai", backend=backend) == "openai-secret"
    assert load_model_api_key(provider="anthropic", backend=backend) == "anthropic-secret"
    assert backend.values[("NaumiAgent", "models.providers.kimi.api_key")] == "kimi-secret"


def test_provider_id_is_normalized_before_account_lookup() -> None:
    backend = _MemoryBackend()

    store_model_api_key("secret-value", provider="  OpenAI  ", backend=backend)

    assert load_model_api_key(provider="openai", backend=backend) == "secret-value"


@pytest.mark.parametrize(
    "provider",
    ["", "../openai", "open ai", "custom:local", "a" * 65],
)
def test_invalid_provider_id_is_rejected_before_backend_access(provider: str) -> None:
    backend = _MemoryBackend()

    with pytest.raises(ValueError, match="provider ID"):
        store_model_api_key("secret-value", provider=provider, backend=backend)

    assert backend.values == {}


def test_provider_load_falls_back_to_legacy_global_key() -> None:
    backend = _MemoryBackend()
    store_model_api_key("legacy-secret", backend=backend)

    assert load_model_api_key(provider="kimi", backend=backend) == "legacy-secret"


def test_provider_load_can_disable_legacy_fallback() -> None:
    backend = _MemoryBackend()
    store_model_api_key("legacy-secret", backend=backend)

    assert (
        load_model_api_key(
            provider="kimi",
            backend=backend,
            fallback_to_legacy=False,
        )
        is None
    )


def test_repeated_provider_load_reads_backend_only_once_per_process() -> None:
    backend = _MemoryBackend()
    account = ("NaumiAgent", "models.providers.kimi.api_key")
    backend.values[account] = "kimi-secret"

    first = load_model_api_key(provider="kimi", backend=backend)
    second = load_model_api_key(provider="kimi", backend=backend)

    assert first == second == "kimi-secret"
    assert backend.get_calls == [account]


def test_store_rejects_empty_model_api_key() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        store_model_api_key("  ", backend=_MemoryBackend())


def test_backend_errors_do_not_expose_secret() -> None:
    with pytest.raises(CredentialStoreError) as exc_info:
        store_model_api_key("secret-value", backend=_FailingBackend())

    assert "secret-value" not in str(exc_info.value)
