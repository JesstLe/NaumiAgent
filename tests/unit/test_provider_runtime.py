"""Provider catalog runtime mapping tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from naumi_agent.config.credentials import CredentialStoreError
from naumi_agent.model.catalog import (
    APIFormat,
    AuthType,
    ModelDiscoverySpec,
    ProviderAuthSpec,
    ProviderModelSpec,
    ProviderSpec,
    SecretSource,
)
from naumi_agent.model.provider_runtime import (
    NO_GLOBAL_API_KEY,
    ProviderHTTPConfig,
    ProviderRuntimeError,
    build_anthropic_messages_transport,
    build_google_genai_transport,
    build_openai_chat_transport,
    build_openai_responses_transport,
    build_provider_http_config,
    build_provider_transport,
)
from naumi_agent.model.targets import ResolvedModelTarget


def _target(
    *,
    api_format: APIFormat | None = APIFormat.OPENAI_CHAT,
    upstream_model: str = "vendor/model-v2",
    base_url: str | None = "https://provider.example/v1",
    auth: ProviderAuthSpec | None = None,
    headers: dict[str, str] | None = None,
    request_timeout_ms: int | None = 12_345,
    source: str = "catalog",
) -> ResolvedModelTarget:
    model = ProviderModelSpec(
        id="chat",
        upstream_id=upstream_model,
        name="Chat",
    )
    provider = ProviderSpec(
        id="vendor",
        name="Vendor",
        api_format=api_format,
        base_url=base_url,
        auth=auth or ProviderAuthSpec(type=AuthType.NONE),
        headers=MappingProxyType(headers or {}),
        models=MappingProxyType({"chat": model}),
        discovery=ModelDiscoverySpec(),
        request_timeout_ms=request_timeout_ms,
    )
    return ResolvedModelTarget(
        requested_model="vendor/chat",
        canonical_model="vendor/chat",
        upstream_model=model.upstream_id,
        provider=provider,
        model=model,
        source=source,  # type: ignore[arg-type]
    )


def _auth(
    auth_type: AuthType,
    source: SecretSource | None,
    ref: str | None,
    *,
    header: str | None = None,
    scheme: str | None = None,
) -> ProviderAuthSpec:
    return ProviderAuthSpec(
        type=auth_type,
        secret_source=source,
        secret_ref=ref,
        header=header,
        scheme=scheme,
    )


def _provider(**kwargs) -> ProviderSpec:
    target = _target(**kwargs)
    assert target.provider is not None
    return target.provider


def test_http_config_maps_standard_bearer_to_authorization(monkeypatch) -> None:
    monkeypatch.setenv("DISCOVERY_TOKEN", "selected-secret")

    config = build_provider_http_config(
        _provider(
            auth=_auth(
                AuthType.BEARER,
                SecretSource.ENV,
                "DISCOVERY_TOKEN",
            )
        ),
        catalog_source="/tmp/providers.json",
    )

    assert isinstance(config, ProviderHTTPConfig)
    assert config.base_url == "https://provider.example/v1"
    assert config.headers == {"Authorization": "Bearer selected-secret"}
    assert config.timeout_seconds == 12.345
    assert "selected-secret" not in repr(config)


def test_http_config_merges_custom_auth_and_static_headers(monkeypatch) -> None:
    monkeypatch.setenv("DISCOVERY_KEY", "custom-secret")

    config = build_provider_http_config(
        _provider(
            auth=_auth(
                AuthType.API_KEY_HEADER,
                SecretSource.ENV,
                "DISCOVERY_KEY",
                header="X-Provider-Key",
            ),
            headers={"X-Tenant": "tenant-a"},
        ),
        catalog_source="/tmp/providers.json",
    )

    assert config.headers == {
        "X-Tenant": "tenant-a",
        "X-Provider-Key": "custom-secret",
    }


def test_http_config_none_auth_ignores_global_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used-either")

    config = build_provider_http_config(
        _provider(
            auth=_auth(AuthType.NONE, None, None),
            request_timeout_ms=None,
        ),
        catalog_source="/tmp/providers.json",
    )

    assert config.headers == {}
    assert config.timeout_seconds == 10.0
    assert "must-not-be-used" not in repr(config)


def test_http_config_rejects_missing_base_url() -> None:
    with pytest.raises(ProviderRuntimeError, match="缺少 baseURL"):
        build_provider_http_config(
            _provider(base_url=None),
            catalog_source="/tmp/providers.json",
        )


def test_http_config_rejects_header_conflict_before_secret_lookup(monkeypatch) -> None:
    calls = 0

    def fail_if_called(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("credential lookup must not happen")

    monkeypatch.setattr(
        "naumi_agent.model.provider_runtime.load_model_api_key",
        fail_if_called,
    )
    provider = _provider(
        auth=_auth(
            AuthType.BEARER,
            SecretSource.CREDENTIAL,
            "discovery-provider",
        ),
        headers={"authorization": "static-value"},
    )

    with pytest.raises(ProviderRuntimeError, match="静态 header 与认证头冲突"):
        build_provider_http_config(
            provider,
            catalog_source="/tmp/providers.json",
        )

    assert calls == 0


def test_maps_model_base_headers_timeout_and_copies_headers() -> None:
    static_headers = {"X-Tenant": "tenant-a"}
    target = _target(headers=static_headers)

    transport = build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert transport.model == "openai/vendor/model-v2"
    assert transport.kwargs == {
        "api_base": "https://provider.example/v1",
        "api_key": NO_GLOBAL_API_KEY,
        "extra_headers": {"X-Tenant": "tenant-a"},
        "timeout": 12.345,
    }
    assert transport.kwargs["extra_headers"] is not target.provider.headers


def test_omits_timeout_when_provider_does_not_define_one() -> None:
    transport = build_openai_chat_transport(
        _target(request_timeout_ms=None),
        catalog_source="/tmp/catalog.json",
    )

    assert "timeout" not in transport.kwargs


def test_maps_openai_responses_model_and_shared_provider_kwargs() -> None:
    target = _target(
        api_format=APIFormat.OPENAI_RESPONSES,
        headers={"X-Tenant": "tenant-a"},
    )

    transport = build_openai_responses_transport(
        target,
        catalog_source="/tmp/catalog.json",
    )

    assert transport.model == "openai/responses/vendor/model-v2"
    assert transport.kwargs == {
        "api_base": "https://provider.example/v1",
        "api_key": NO_GLOBAL_API_KEY,
        "extra_headers": {"X-Tenant": "tenant-a"},
        "timeout": 12.345,
    }


def test_maps_anthropic_messages_model_and_shared_provider_kwargs() -> None:
    target = _target(
        api_format=APIFormat.ANTHROPIC_MESSAGES,
        headers={"X-Tenant": "tenant-a"},
    )

    transport = build_anthropic_messages_transport(
        target,
        catalog_source="/tmp/catalog.json",
    )

    assert transport.model == "anthropic/vendor/model-v2"
    assert transport.kwargs == {
        "api_base": "https://provider.example/v1",
        "api_key": NO_GLOBAL_API_KEY,
        "extra_headers": {"X-Tenant": "tenant-a"},
        "timeout": 12.345,
    }


@pytest.mark.parametrize(
    ("api_format", "expected_model"),
    [
        (APIFormat.OPENAI_CHAT, "openai/vendor/model-v2"),
        (APIFormat.OPENAI_RESPONSES, "openai/responses/vendor/model-v2"),
        (APIFormat.ANTHROPIC_MESSAGES, "anthropic/vendor/model-v2"),
        (APIFormat.GOOGLE_GENAI, "gemini/gemini-model-v2"),
    ],
)
def test_dispatches_supported_provider_api_formats(
    api_format: APIFormat,
    expected_model: str,
) -> None:
    transport = build_provider_transport(
        _target(
            api_format=api_format,
            upstream_model=(
                "gemini-model-v2"
                if api_format is APIFormat.GOOGLE_GENAI
                else "vendor/model-v2"
            ),
        ),
        catalog_source="/tmp/catalog.json",
    )

    assert transport.model == expected_model


def test_dispatcher_rejects_an_unimplemented_provider_format() -> None:
    with pytest.raises(ProviderRuntimeError, match="azure_openai.*尚未实现"):
        build_provider_transport(
            _target(api_format=APIFormat.AZURE_OPENAI),
            catalog_source="/tmp/catalog.json",
        )


def test_rejects_legacy_target() -> None:
    with pytest.raises(ProviderRuntimeError, match="只接受 provider catalog"):
        build_openai_chat_transport(
            _target(source="legacy"),
            catalog_source="/tmp/catalog.json",
        )


@pytest.mark.parametrize(
    ("api_format", "message"),
    [
        (None, "缺少 apiFormat"),
        (APIFormat.OPENAI_RESPONSES, "不能由 openai_chat 适配器处理"),
    ],
)
def test_rejects_missing_or_unsupported_api_format(
    api_format: APIFormat | None,
    message: str,
) -> None:
    with pytest.raises(ProviderRuntimeError, match=message):
        build_openai_chat_transport(
            _target(api_format=api_format),
            catalog_source="/tmp/catalog.json",
        )


def test_responses_builder_rejects_a_chat_target_with_an_accurate_error() -> None:
    with pytest.raises(
        ProviderRuntimeError,
        match="不能由 openai_responses 适配器处理",
    ):
        build_openai_responses_transport(
            _target(api_format=APIFormat.OPENAI_CHAT),
            catalog_source="/tmp/catalog.json",
        )


def test_anthropic_builder_rejects_a_chat_target_with_an_accurate_error() -> None:
    with pytest.raises(
        ProviderRuntimeError,
        match="不能由 anthropic_messages 适配器处理",
    ):
        build_anthropic_messages_transport(
            _target(api_format=APIFormat.OPENAI_CHAT),
            catalog_source="/tmp/catalog.json",
        )


def test_anthropic_standard_api_key_is_passed_to_litellm(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_SELECTED_KEY", "anthropic-selected-secret")
    target = _target(
        api_format=APIFormat.ANTHROPIC_MESSAGES,
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.ENV,
            "ANTHROPIC_SELECTED_KEY",
            header="X-API-Key",
        ),
    )

    transport = build_anthropic_messages_transport(
        target,
        catalog_source="/tmp/catalog.json",
    )

    assert transport.kwargs["api_key"] == "anthropic-selected-secret"
    assert transport.kwargs["extra_headers"] == {}
    assert "anthropic-selected-secret" not in repr(transport)


def test_anthropic_bearer_stays_in_authorization_header(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_SELECTED_TOKEN", "anthropic-selected-token")
    target = _target(
        api_format=APIFormat.ANTHROPIC_MESSAGES,
        auth=_auth(
            AuthType.BEARER,
            SecretSource.ENV,
            "ANTHROPIC_SELECTED_TOKEN",
        ),
    )

    transport = build_anthropic_messages_transport(
        target,
        catalog_source="/tmp/catalog.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == {
        "Authorization": "Bearer anthropic-selected-token"
    }


def test_anthropic_none_auth_does_not_read_global_anthropic_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")

    transport = build_anthropic_messages_transport(
        _target(
            api_format=APIFormat.ANTHROPIC_MESSAGES,
            auth=_auth(AuthType.NONE, None, None),
        ),
        catalog_source="/tmp/catalog.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert "must-not-be-used" not in repr(transport)


def test_anthropic_custom_api_key_header_is_preserved(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_KEY", "anthropic-custom-secret")
    target = _target(
        api_format=APIFormat.ANTHROPIC_MESSAGES,
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.ENV,
            "ANTHROPIC_CUSTOM_KEY",
            header="X-Provider-Key",
        ),
    )

    transport = build_anthropic_messages_transport(
        target,
        catalog_source="/tmp/catalog.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == {
        "X-Provider-Key": "anthropic-custom-secret"
    }


def test_google_genai_maps_standard_key_model_base_headers_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_SELECTED_KEY", "google-selected-secret")
    target = _target(
        api_format=APIFormat.GOOGLE_GENAI,
        upstream_model="gemini-model-v2",
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.ENV,
            "GEMINI_SELECTED_KEY",
            header="x-goog-api-key",
        ),
        headers={"X-Tenant": "tenant-a"},
    )

    transport = build_google_genai_transport(
        target,
        catalog_source="/tmp/providers.json",
    )

    assert transport.model == "gemini/gemini-model-v2"
    assert transport.kwargs == {
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "google-selected-secret",
        "extra_headers": {"X-Tenant": "tenant-a"},
        "timeout": 12.345,
    }
    assert "google-selected-secret" not in repr(transport)


def test_google_genai_strips_one_official_models_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_SELECTED_KEY", "secret")
    target = replace(
        _target(
            api_format=APIFormat.GOOGLE_GENAI,
            auth=_auth(
                AuthType.API_KEY_HEADER,
                SecretSource.ENV,
                "GEMINI_SELECTED_KEY",
                header="X-Goog-Api-Key",
            ),
        ),
        upstream_model="models/gemini-3.5-flash",
    )

    transport = build_google_genai_transport(
        target,
        catalog_source="/tmp/providers.json",
    )

    assert transport.model == "gemini/gemini-3.5-flash"


@pytest.mark.parametrize(
    ("auth", "expected_headers"),
    [
        (
            _auth(AuthType.BEARER, SecretSource.ENV, "GOOGLE_CUSTOM_SECRET"),
            {"Authorization": "Bearer custom-secret"},
        ),
        (
            _auth(
                AuthType.API_KEY_HEADER,
                SecretSource.ENV,
                "GOOGLE_CUSTOM_SECRET",
                header="X-Proxy-Key",
            ),
            {"X-Proxy-Key": "custom-secret"},
        ),
    ],
)
def test_google_custom_auth_uses_header_and_nonsecret_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    auth: ProviderAuthSpec,
    expected_headers: dict[str, str],
) -> None:
    monkeypatch.setenv("GOOGLE_CUSTOM_SECRET", "custom-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-must-not-win")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-must-not-win")

    transport = build_google_genai_transport(
        _target(
            api_format=APIFormat.GOOGLE_GENAI,
            upstream_model="gemini-model-v2",
            auth=auth,
        ),
        catalog_source="/tmp/providers.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == expected_headers


def test_google_none_auth_never_reads_ambient_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-must-not-win")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-must-not-win")

    transport = build_google_genai_transport(
        _target(
            api_format=APIFormat.GOOGLE_GENAI,
            upstream_model="gemini-model-v2",
            auth=_auth(AuthType.NONE, None, None),
        ),
        catalog_source="/tmp/providers.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == {}


@pytest.mark.parametrize(
    "upstream_model",
    [
        "",
        "models/",
        "models/a/b",
        "a:generateContent",
        "a?key=x",
        "a%2Fescaped",
        "a\\backslash",
        "a with-space",
        "a\nunsafe",
        "x" * 257,
    ],
)
def test_google_genai_rejects_invalid_model_before_secret_lookup(
    monkeypatch: pytest.MonkeyPatch,
    upstream_model: str,
) -> None:
    calls = 0

    def fail_if_called(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("credential lookup must not happen")

    monkeypatch.setattr(
        "naumi_agent.model.provider_runtime.load_model_api_key",
        fail_if_called,
    )
    target = replace(
        _target(
            api_format=APIFormat.GOOGLE_GENAI,
            auth=_auth(
                AuthType.API_KEY_HEADER,
                SecretSource.CREDENTIAL,
                "google",
                header="X-Goog-Api-Key",
            ),
        ),
        upstream_model=upstream_model,
    )

    with pytest.raises(
        ProviderRuntimeError,
        match="Google GenAI upstream model ID 无效",
    ) as error:
        build_google_genai_transport(target, catalog_source="/tmp/providers.json")

    assert calls == 0
    if upstream_model:
        assert upstream_model not in str(error.value)


def test_google_genai_rejects_non_positive_timeout() -> None:
    with pytest.raises(ProviderRuntimeError, match="request timeout 必须大于 0"):
        build_google_genai_transport(
            _target(
                api_format=APIFormat.GOOGLE_GENAI,
                upstream_model="gemini-model-v2",
                request_timeout_ms=0,
            ),
            catalog_source="/tmp/providers.json",
        )


def test_google_genai_rejects_auth_header_conflict_before_secret_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail_if_called(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("credential lookup must not happen")

    monkeypatch.setattr(
        "naumi_agent.model.provider_runtime.load_model_api_key",
        fail_if_called,
    )
    target = _target(
        api_format=APIFormat.GOOGLE_GENAI,
        upstream_model="gemini-model-v2",
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.CREDENTIAL,
            "google",
            header="X-Goog-Api-Key",
        ),
        headers={"x-goog-api-key": "static"},
    )

    with pytest.raises(ProviderRuntimeError, match="认证头冲突"):
        build_google_genai_transport(target, catalog_source="/tmp/providers.json")

    assert calls == 0


def test_rejects_missing_base_url() -> None:
    with pytest.raises(ProviderRuntimeError, match="缺少 baseURL"):
        build_openai_chat_transport(
            _target(base_url=None),
            catalog_source="/tmp/catalog.json",
        )


def test_standard_bearer_uses_api_key_and_disables_legacy_fallback(monkeypatch) -> None:
    calls: list[tuple[str | None, bool]] = []

    def fake_load(*, provider=None, fallback_to_legacy=True):
        calls.append((provider, fallback_to_legacy))
        return "  credential-secret  "

    monkeypatch.setattr(
        "naumi_agent.model.provider_runtime.load_model_api_key",
        fake_load,
    )
    target = _target(
        auth=_auth(
            AuthType.BEARER,
            SecretSource.CREDENTIAL,
            "credential-vendor",
            header="Authorization",
            scheme="Bearer",
        )
    )

    transport = build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert calls == [("credential-vendor", False)]
    assert transport.kwargs["api_key"] == "credential-secret"
    assert transport.kwargs["extra_headers"] == {}


def test_custom_bearer_header_uses_only_extra_headers(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_PROVIDER_TOKEN", "custom-secret")
    target = _target(
        auth=_auth(
            AuthType.BEARER,
            SecretSource.ENV,
            "CUSTOM_PROVIDER_TOKEN",
            header="X-Provider-Authorization",
            scheme="Token",
        )
    )

    transport = build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == {
        "X-Provider-Authorization": "Token custom-secret"
    }
    assert "Authorization" not in transport.kwargs["extra_headers"]


def test_api_key_header_uses_only_extra_headers(monkeypatch) -> None:
    monkeypatch.setenv("VENDOR_API_KEY", "header-secret")
    target = _target(
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.ENV,
            "VENDOR_API_KEY",
            header="X-API-Key",
        )
    )

    transport = build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == {"X-API-Key": "header-secret"}


def test_none_auth_uses_fixed_placeholder_and_does_not_read_global_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    transport = build_openai_chat_transport(
        _target(auth=_auth(AuthType.NONE, None, None)),
        catalog_source="/tmp/catalog.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert "must-not-be-used" not in repr(transport)


def test_reads_only_the_exact_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-secret")
    monkeypatch.setenv("SELECTED_VENDOR_KEY", "right-secret")
    target = _target(
        auth=_auth(
            AuthType.BEARER,
            SecretSource.ENV,
            "SELECTED_VENDOR_KEY",
            header="Authorization",
            scheme="Bearer",
        )
    )

    transport = build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert transport.kwargs["api_key"] == "right-secret"


@pytest.mark.parametrize("value", [None, "", " \n\t "])
def test_rejects_missing_or_empty_environment_secret(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("MISSING_VENDOR_KEY", raising=False)
    else:
        monkeypatch.setenv("MISSING_VENDOR_KEY", value)
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.ENV, "MISSING_VENDOR_KEY")
    )

    with pytest.raises(ProviderRuntimeError) as error:
        build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert "vendor" in str(error.value)
    assert "env" in str(error.value)
    if value and value.strip():
        assert value.strip() not in str(error.value)


def test_credential_store_error_is_sanitized(monkeypatch) -> None:
    leaked = "do-not-leak-this-secret"

    def fail_load(**_kwargs):
        raise CredentialStoreError(f"backend failure: {leaked}")

    monkeypatch.setattr(
        "naumi_agent.model.provider_runtime.load_model_api_key",
        fail_load,
    )
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.CREDENTIAL, "credential-vendor")
    )

    with pytest.raises(ProviderRuntimeError) as error:
        build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert "credential" in str(error.value)
    assert leaked not in str(error.value)


@pytest.mark.parametrize("secret", ["first\nsecond", "first\rsecond", "first\x00second"])
def test_rejects_secret_values_with_control_characters(
    monkeypatch,
    secret: str,
) -> None:
    monkeypatch.setattr(
        "naumi_agent.model.provider_runtime.load_model_api_key",
        lambda **_kwargs: secret,
    )
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.CREDENTIAL, "vendor")
    )

    with pytest.raises(ProviderRuntimeError, match="包含控制字符") as error:
        build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")

    assert secret not in str(error.value)


def test_reads_absolute_utf8_secret_file_and_strips_outer_whitespace(tmp_path: Path) -> None:
    secret_file = tmp_path / "provider.key"
    secret_file.write_text(" \n file-secret \t", encoding="utf-8")
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.FILE, str(secret_file))
    )

    transport = build_openai_chat_transport(target, catalog_source="<memory>")

    assert transport.kwargs["api_key"] == "file-secret"


def test_reads_relative_secret_from_catalog_directory(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "catalog"
    secret_dir = catalog_dir / "secrets"
    secret_dir.mkdir(parents=True)
    (secret_dir / "provider.key").write_text("relative-secret", encoding="utf-8")
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.FILE, "secrets/provider.key")
    )

    transport = build_openai_chat_transport(
        target,
        catalog_source=str(catalog_dir / "providers.json"),
    )

    assert transport.kwargs["api_key"] == "relative-secret"


def test_rejects_relative_secret_for_memory_catalog() -> None:
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.FILE, "secrets/provider.key")
    )

    with pytest.raises(ProviderRuntimeError, match="内存 catalog.*相对"):
        build_openai_chat_transport(target, catalog_source="<memory>")


@pytest.mark.parametrize("reference", ["../outside.key", "secrets/../../outside.key"])
def test_rejects_relative_secret_path_escape(tmp_path: Path, reference: str) -> None:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (tmp_path / "outside.key").write_text("outside-secret", encoding="utf-8")
    target = _target(auth=_auth(AuthType.BEARER, SecretSource.FILE, reference))

    with pytest.raises(ProviderRuntimeError) as error:
        build_openai_chat_transport(
            target,
            catalog_source=str(catalog_dir / "providers.json"),
        )

    assert "超出 catalog 目录" in str(error.value)
    assert "outside-secret" not in str(error.value)


def test_rejects_symlink_that_escapes_catalog_directory(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    outside = tmp_path / "outside.key"
    outside.write_text("outside-secret", encoding="utf-8")
    (catalog_dir / "linked.key").symlink_to(outside)
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.FILE, "linked.key")
    )

    with pytest.raises(ProviderRuntimeError, match="超出 catalog 目录"):
        build_openai_chat_transport(
            target,
            catalog_source=str(catalog_dir / "providers.json"),
        )


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid_utf8", "empty", "large"])
def test_rejects_invalid_secret_files(tmp_path: Path, kind: str) -> None:
    secret_path = tmp_path / "secret"
    if kind == "directory":
        secret_path.mkdir()
    elif kind == "invalid_utf8":
        secret_path.write_bytes(b"\xff\xfe")
    elif kind == "empty":
        secret_path.write_text(" \n\t ", encoding="utf-8")
    elif kind == "large":
        secret_path.write_bytes(b"x" * (64 * 1024 + 1))
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.FILE, str(secret_path))
    )

    with pytest.raises(ProviderRuntimeError) as error:
        build_openai_chat_transport(target, catalog_source="<memory>")

    message = str(error.value)
    assert "vendor" in message
    assert "file" in message
    if kind == "invalid_utf8":
        assert "\\xff" not in message


@pytest.mark.parametrize(
    "auth",
    [
        _auth(AuthType.BEARER, None, None),
        _auth(AuthType.API_KEY_HEADER, SecretSource.ENV, None),
        _auth(AuthType.NONE, SecretSource.ENV, "SHOULD_NOT_EXIST"),
    ],
)
def test_rejects_internally_inconsistent_auth(auth: ProviderAuthSpec) -> None:
    with pytest.raises(ProviderRuntimeError, match="认证配置无效"):
        build_openai_chat_transport(
            _target(auth=auth),
            catalog_source="/tmp/catalog.json",
        )


def test_rejects_case_insensitive_static_and_auth_header_conflict(monkeypatch) -> None:
    monkeypatch.setenv("VENDOR_KEY", "secret")
    target = _target(
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.ENV,
            "VENDOR_KEY",
            header="X-Custom-Key",
        ),
        headers={"x-custom-key": "static"},
    )

    with pytest.raises(ProviderRuntimeError, match="认证头冲突"):
        build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")


def test_transport_result_is_immutable() -> None:
    transport = build_openai_chat_transport(
        _target(),
        catalog_source="/tmp/catalog.json",
    )

    with pytest.raises(TypeError):
        transport.kwargs["api_base"] = "https://changed.example"  # type: ignore[index]


def test_transport_repr_does_not_expose_resolved_secret(monkeypatch) -> None:
    monkeypatch.setenv("VENDOR_API_KEY", "repr-must-not-leak")
    target = _target(
        auth=_auth(AuthType.BEARER, SecretSource.ENV, "VENDOR_API_KEY")
    )

    transport = build_openai_chat_transport(
        target,
        catalog_source="/tmp/catalog.json",
    )

    assert "repr-must-not-leak" not in repr(transport)


def test_rejects_catalog_target_without_provider() -> None:
    target = replace(_target(), provider=None)

    with pytest.raises(ProviderRuntimeError, match="缺少 provider"):
        build_openai_chat_transport(target, catalog_source="/tmp/catalog.json")
