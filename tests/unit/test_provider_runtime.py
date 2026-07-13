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
    ProviderRuntimeError,
    build_openai_chat_transport,
)
from naumi_agent.model.targets import ResolvedModelTarget


def _target(
    *,
    api_format: APIFormat | None = APIFormat.OPENAI_CHAT,
    base_url: str | None = "https://provider.example/v1",
    auth: ProviderAuthSpec | None = None,
    headers: dict[str, str] | None = None,
    request_timeout_ms: int | None = 12_345,
    source: str = "catalog",
) -> ResolvedModelTarget:
    model = ProviderModelSpec(
        id="chat",
        upstream_id="vendor/model-v2",
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
        (APIFormat.OPENAI_RESPONSES, "适配器尚未实现"),
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
