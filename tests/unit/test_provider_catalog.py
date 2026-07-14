from __future__ import annotations

import json
import os
import socket

import keyring
import pytest

from naumi_agent.model.catalog import (
    APIFormat,
    AuthType,
    ProviderAuthSpec,
    ProviderCatalogError,
    SecretSource,
    load_provider_catalog,
    parse_provider_catalog_json,
)


def _native_catalog() -> dict[str, object]:
    return {
        "providers": {
            "NVIDIA": {
                "name": "NVIDIA NIM",
                "apiFormat": "openai_chat",
                "baseURL": "https://integrate.api.nvidia.com/v1/",
                "auth": {
                    "type": "bearer",
                    "credentialProvider": "NVIDIA",
                },
                "headers": {"User-Agent": "NaumiAgent/1"},
                "models": {
                    "glm-local": {
                        "name": "GLM 4.7",
                        "upstreamId": "z-ai/glm4.7",
                        "limit": {"context": 128000, "output": 8192},
                        "capabilities": {
                            "tools": True,
                            "reasoning": False,
                            "vision": False,
                        },
                    },
                    "hidden-model": {},
                },
                "discovery": {
                    "enabled": True,
                    "path": "/models",
                    "ttlSeconds": 3600,
                },
                "whitelist": ["glm-local"],
                "blacklist": ["hidden-model"],
            }
        }
    }


def test_native_catalog_normalizes_provider_model_auth_and_discovery() -> None:
    catalog = parse_provider_catalog_json(json.dumps(_native_catalog()))

    provider = catalog.providers["nvidia"]
    model = provider.models["glm-local"]
    assert provider.name == "NVIDIA NIM"
    assert provider.api_format is APIFormat.OPENAI_CHAT
    assert provider.base_url == "https://integrate.api.nvidia.com/v1"
    assert provider.auth.type is AuthType.BEARER
    assert provider.auth.secret_source is SecretSource.CREDENTIAL
    assert provider.auth.secret_ref == "nvidia"
    assert provider.headers == {"User-Agent": "NaumiAgent/1"}
    assert provider.discovery.enabled
    assert provider.discovery.path == "/models"
    assert provider.discovery.ttl_seconds == 3600
    assert model.upstream_id == "z-ai/glm4.7"
    assert model.max_context == 128000
    assert model.max_output == 8192
    assert model.supports_tools
    assert not model.supports_reasoning
    assert model.reasoning_efforts == ()
    assert model.default_reasoning_effort is None


def test_reasoning_capability_object_parses_ordered_efforts_and_default() -> None:
    payload = _native_catalog()
    model = payload["providers"]["NVIDIA"]["models"]["glm-local"]  # type: ignore[index]
    model["capabilities"]["reasoning"] = {  # type: ignore[index]
        "efforts": ["minimal", "low", "high"],
        "defaultEffort": "low",
    }

    parsed = parse_provider_catalog_json(json.dumps(payload)).providers["nvidia"].models[
        "glm-local"
    ]

    assert parsed.supports_reasoning is True
    assert tuple(value.value for value in parsed.reasoning_efforts) == (
        "minimal",
        "low",
        "high",
    )
    assert parsed.default_reasoning_effort is not None
    assert parsed.default_reasoning_effort.value == "low"


@pytest.mark.parametrize(
    ("reasoning", "message"),
    [
        ({}, "efforts"),
        ({"efforts": []}, "非空"),
        ({"efforts": "high"}, "数组"),
        ({"efforts": ["low", "low"]}, "重复"),
        ({"efforts": ["turbo"]}, "可选值"),
        ({"efforts": ["auto"]}, "可选值"),
        (
            {"efforts": ["low", "high"], "defaultEffort": "medium"},
            "必须出现在 efforts",
        ),
    ],
)
def test_reasoning_capability_object_rejects_invalid_shapes(
    reasoning: object,
    message: str,
) -> None:
    payload = _native_catalog()
    model = payload["providers"]["NVIDIA"]["models"]["glm-local"]  # type: ignore[index]
    model["capabilities"]["reasoning"] = reasoning  # type: ignore[index]

    with pytest.raises(ProviderCatalogError) as exc_info:
        parse_provider_catalog_json(json.dumps(payload))

    text = str(exc_info.value)
    assert "models.glm-local.capabilities.reasoning" in text
    assert message in text


def test_visible_models_applies_whitelist_then_blacklist_in_declaration_order() -> None:
    catalog = parse_provider_catalog_json(json.dumps(_native_catalog()))

    visible = catalog.providers["nvidia"].visible_models()

    assert [model.id for model in visible] == ["glm-local"]


def test_opencode_shape_infers_adapter_and_preserves_file_reference() -> None:
    payload = {
        "model": "nvidia/z-ai/glm4.7",
        "provider": {
            "nvidia": {
                "name": "NVIDIA NIM",
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "apiKey": "{file:secrets/nvidia_api_key}",
                    "baseURL": "https://integrate.api.nvidia.com/v1",
                },
                "models": {
                    "z-ai/glm4.7": {
                        "limit": {"context": 128000, "output": 8192},
                        "modalities": {"input": ["text"], "output": ["text"]},
                    }
                },
            }
        },
        "share": "disabled",
    }

    catalog = parse_provider_catalog_json(json.dumps(payload))

    provider = catalog.providers["nvidia"]
    assert provider.api_format is APIFormat.OPENAI_CHAT
    assert provider.auth.secret_source is SecretSource.FILE
    assert provider.auth.secret_ref == "secrets/nvidia_api_key"
    assert provider.models["z-ai/glm4.7"].input_modalities == ("text",)


def test_opencode_environment_reference_is_normalized() -> None:
    payload = {
        "provider": {
            "openai-proxy": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "apiKey": "{env:PROXY_API_KEY}",
                    "baseURL": "https://proxy.example/v1",
                },
                "models": {"model-a": {}},
            }
        }
    }

    provider = parse_provider_catalog_json(json.dumps(payload)).providers["openai-proxy"]

    assert provider.auth.secret_source is SecretSource.ENV
    assert provider.auth.secret_ref == "PROXY_API_KEY"


def test_opencode_anthropic_api_key_uses_x_api_key_auth() -> None:
    payload = {
        "provider": {
            "anthropic-proxy": {
                "npm": "@ai-sdk/anthropic",
                "options": {
                    "apiKey": "{env:ANTHROPIC_PROXY_KEY}",
                    "baseURL": "https://anthropic-proxy.example",
                },
                "models": {"claude": {}},
            }
        }
    }

    provider = parse_provider_catalog_json(json.dumps(payload)).providers[
        "anthropic-proxy"
    ]

    assert provider.api_format is APIFormat.ANTHROPIC_MESSAGES
    assert provider.auth.type is AuthType.API_KEY_HEADER
    assert provider.auth.header == "X-API-Key"
    assert provider.auth.secret_source is SecretSource.ENV
    assert provider.auth.secret_ref == "ANTHROPIC_PROXY_KEY"


def test_opencode_google_provider_uses_x_goog_api_key() -> None:
    catalog = parse_provider_catalog_json(
        json.dumps(
            {
                "provider": {
                    "google": {
                        "npm": "@ai-sdk/google",
                        "options": {
                            "baseURL": (
                                "https://generativelanguage.googleapis.com/v1beta"
                            ),
                            "apiKey": "{env:GEMINI_SELECTED_KEY}",
                        },
                        "models": {
                            "flash": {"upstreamId": "gemini-3.5-flash"},
                        },
                    },
                },
            }
        )
    )

    provider = catalog.providers["google"]
    assert provider.api_format is APIFormat.GOOGLE_GENAI
    assert provider.auth == ProviderAuthSpec(
        type=AuthType.API_KEY_HEADER,
        secret_source=SecretSource.ENV,
        secret_ref="GEMINI_SELECTED_KEY",
        header="X-Goog-Api-Key",
    )


@pytest.mark.parametrize("field", ["apiKey", "api_key"])
def test_inline_api_keys_are_rejected_without_echoing_secret(field: str) -> None:
    secret = "do-not-echo-this-secret"
    payload = {
        "provider": {
            "unsafe": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    field: secret,
                    "baseURL": "https://example.test/v1",
                },
                "models": {"model-a": {}},
            }
        }
    }

    with pytest.raises(ProviderCatalogError) as exc_info:
        parse_provider_catalog_json(json.dumps(payload))

    assert "明文" in str(exc_info.value)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "X-API-Key",
        "X-Goog-Api-Key",
        "Anthropic-Api-Key",
        "Ocp-Apim-Subscription-Key",
    ],
)
def test_sensitive_static_auth_header_is_rejected(header: str) -> None:
    payload = _native_catalog()
    provider = payload["providers"]["NVIDIA"]  # type: ignore[index]
    provider["headers"] = {header: "secret"}  # type: ignore[index]

    with pytest.raises(ProviderCatalogError, match="敏感认证头"):
        parse_provider_catalog_json(json.dumps(payload))


def test_unknown_opencode_npm_adapter_requires_explicit_api_format() -> None:
    payload = {
        "provider": {
            "unknown": {
                "npm": "@vendor/unknown-runtime",
                "options": {"baseURL": "https://example.test/v1"},
                "models": {"model-a": {}},
            }
        }
    }

    with pytest.raises(ProviderCatalogError, match="apiFormat"):
        parse_provider_catalog_json(json.dumps(payload))


def test_duplicate_json_keys_are_rejected() -> None:
    raw = (
        '{"providers":{"openai":{"apiFormat":"openai_chat",'
        '"baseURL":"https://api.example/v1","models":{"a":{},"a":{}}}}}'
    )

    with pytest.raises(ProviderCatalogError, match="重复字段"):
        parse_provider_catalog_json(raw)


def test_bad_base_url_does_not_expose_embedded_password() -> None:
    password = "private-password"
    payload = _native_catalog()
    provider = payload["providers"]["NVIDIA"]  # type: ignore[index]
    provider["baseURL"] = f"https://user:{password}@example.test/v1"  # type: ignore[index]

    with pytest.raises(ProviderCatalogError) as exc_info:
        parse_provider_catalog_json(json.dumps(payload))

    assert "用户名或密码" in str(exc_info.value)
    assert password not in str(exc_info.value)


@pytest.mark.parametrize("parameter", ["api_key", "key", "token", "access_token"])
def test_base_url_rejects_secret_query_parameters_without_echoing_value(
    parameter: str,
) -> None:
    secret = "private-query-secret"
    payload = _native_catalog()
    provider = payload["providers"]["NVIDIA"]  # type: ignore[index]
    provider["baseURL"] = f"https://example.test/v1?{parameter}={secret}"  # type: ignore[index]

    with pytest.raises(ProviderCatalogError) as exc_info:
        parse_provider_catalog_json(json.dumps(payload))

    assert "认证参数" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_filter_overlap_is_rejected() -> None:
    payload = _native_catalog()
    provider = payload["providers"]["NVIDIA"]  # type: ignore[index]
    provider["blacklist"] = ["glm-local"]  # type: ignore[index]

    with pytest.raises(ProviderCatalogError, match="同时出现在"):
        parse_provider_catalog_json(json.dumps(payload))


def test_provider_requires_static_models_or_discovery() -> None:
    payload = {
        "providers": {
            "empty": {
                "apiFormat": "ollama",
                "baseURL": "http://127.0.0.1:11434",
                "models": {},
            }
        }
    }

    with pytest.raises(ProviderCatalogError, match="至少一个静态模型"):
        parse_provider_catalog_json(json.dumps(payload))


def test_discovery_only_provider_is_valid() -> None:
    payload = {
        "providers": {
            "ollama": {
                "apiFormat": "ollama",
                "baseURL": "http://127.0.0.1:11434",
                "auth": {"type": "none"},
                "models": {},
                "discovery": {"enabled": True, "path": "/api/tags"},
            }
        }
    }

    provider = parse_provider_catalog_json(json.dumps(payload)).providers["ollama"]

    assert provider.discovery.enabled
    assert provider.models == {}


def test_file_loader_rejects_oversized_json_before_parsing(tmp_path) -> None:
    path = tmp_path / "providers.json"
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))

    with pytest.raises(ProviderCatalogError, match="2 MiB"):
        load_provider_catalog(path)


def test_file_loader_reports_malformed_json_without_raw_content(tmp_path) -> None:
    path = tmp_path / "providers.json"
    marker = "private-malformed-marker"
    path.write_text(f'{{"providers": {marker}', encoding="utf-8")

    with pytest.raises(ProviderCatalogError) as exc_info:
        load_provider_catalog(path)

    assert "JSON" in str(exc_info.value)
    assert marker not in str(exc_info.value)


def test_file_loader_has_no_secret_or_network_side_effects(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "credential-backed": {
                        "apiFormat": "openai_responses",
                        "baseURL": "https://api.example/v1",
                        "auth": {"credentialProvider": "credential-backed"},
                        "models": {"model-a": {}},
                    },
                    "env-backed": {
                        "apiFormat": "anthropic_messages",
                        "baseURL": "https://anthropic.example/v1",
                        "auth": {"env": "ANTHROPIC_API_KEY"},
                        "models": {"model-b": {}},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("catalog loading must remain side-effect free")

    monkeypatch.setattr(keyring, "get_password", unexpected_call)
    monkeypatch.setattr(os, "getenv", unexpected_call)
    monkeypatch.setattr(socket, "socket", unexpected_call)

    catalog = load_provider_catalog(path)

    assert tuple(catalog.providers) == ("credential-backed", "env-backed")
