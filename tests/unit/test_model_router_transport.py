"""ModelRouter transport integration tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.config.settings import ModelConfig
from naumi_agent.model.catalog import parse_provider_catalog_json
from naumi_agent.model.provider_runtime import ProviderRuntimeError
from naumi_agent.model.router import ModelRouter


def _chat_catalog(*, source: str = "/tmp/providers.json"):
    return parse_provider_catalog_json(
        json.dumps(
            {
                "provider": {
                    "vendor": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "https://chat.vendor.example/v1",
                            "apiKey": "{env:VENDOR_CHAT_KEY}",
                            "headers": {"X-Tenant": "tenant-a"},
                            "timeout": 12_500,
                        },
                        "models": {
                            "chat": {
                                "upstreamId": "vendor/model-v2",
                                "limit": {"output": 2_048},
                            }
                        },
                    }
                }
            }
        ),
        source=source,
    )


def _responses_catalog():
    return parse_provider_catalog_json(
        json.dumps(
            {
                "providers": {
                    "vendor": {
                        "apiFormat": "openai_responses",
                        "baseURL": "https://responses.vendor.example/v1",
                        "auth": {"type": "none"},
                        "models": {"chat": {"upstreamId": "vendor/model-v2"}},
                    }
                }
            }
        )
    )


def _kimi_chat_catalog():
    return parse_provider_catalog_json(
        json.dumps(
            {
                "provider": {
                    "kimi": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "https://api.kimi.com/coding/v1",
                            "apiKey": "{env:KIMI_CHAT_KEY}",
                        },
                        "models": {
                            "coding": {"upstreamId": "kimi-k2.6"},
                        },
                    }
                }
            }
        )
    )


def _anthropic_catalog():
    return parse_provider_catalog_json(
        json.dumps(
            {
                "providers": {
                    "vendor": {
                        "apiFormat": "anthropic_messages",
                        "baseURL": "https://anthropic.vendor.example/v1",
                        "auth": {"type": "none"},
                        "models": {"chat": {"upstreamId": "vendor/model-v2"}},
                    }
                }
            }
        )
    )


def _completion_response(content: str = "adapter-ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=2,
            prompt_tokens_details=None,
        ),
    )


async def _completion_stream() -> AsyncIterator[SimpleNamespace]:
    yield SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content="stream-ok",
                    reasoning_content=None,
                    thinking_blocks=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
    )


@pytest.mark.asyncio
async def test_call_uses_catalog_openai_chat_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VENDOR_CHAT_KEY", "selected-provider-secret")
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _completion_response()

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    router = ModelRouter(
        ModelConfig(provider="vendor", default_model="chat"),
        catalog=_chat_catalog(),
    )

    response = await router.call([{"role": "user", "content": "hello"}])

    assert captured["model"] == "openai/vendor/model-v2"
    assert captured["api_base"] == "https://chat.vendor.example/v1"
    assert captured["api_key"] == "selected-provider-secret"
    assert dict(captured["extra_headers"]) == {"X-Tenant": "tenant-a"}
    assert captured["timeout"] == 12.5
    assert response.content == "adapter-ok"
    assert response.model == "chat"


@pytest.mark.asyncio
async def test_stream_uses_the_same_catalog_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VENDOR_CHAT_KEY", "selected-provider-secret")
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _completion_stream()

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.register_model",
        lambda _model_cost: pytest.fail(
            "Chat transport must not register Responses streaming"
        ),
    )
    router = ModelRouter(
        ModelConfig(provider="vendor", default_model="chat"),
        catalog=_chat_catalog(),
    )

    chunks = [
        chunk
        async for chunk in router.stream(
            [{"role": "user", "content": "hello"}]
        )
    ]
    assert captured["model"] == "openai/vendor/model-v2"
    assert captured["api_base"] == "https://chat.vendor.example/v1"
    assert captured["api_key"] == "selected-provider-secret"
    assert dict(captured["extra_headers"]) == {"X-Tenant": "tenant-a"}
    assert captured["timeout"] == 12.5
    assert [chunk.token for chunk in chunks] == ["stream-ok"]


@pytest.mark.asyncio
async def test_legacy_call_keeps_existing_model_and_base_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _completion_response("legacy-ok")

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    router = ModelRouter(
        ModelConfig(
            default_model="openai/legacy-model",
            api_base="https://legacy.example/v1",
            api_key="legacy-secret",
        )
    )

    response = await router.call([{"role": "user", "content": "hello"}])

    assert captured["model"] == "openai/legacy-model"
    assert captured["api_base"] == "https://legacy.example/v1"
    assert captured["api_key"] == "legacy-secret"
    assert response.content == "legacy-ok"


@pytest.mark.asyncio
async def test_call_uses_catalog_openai_responses_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _completion_response("responses-ok")

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    router = ModelRouter(
        ModelConfig(provider="vendor", default_model="chat"),
        catalog=_responses_catalog(),
    )

    response = await router.call([{"role": "user", "content": "hello"}])

    assert captured["model"] == "openai/responses/vendor/model-v2"
    assert captured["api_base"] == "https://responses.vendor.example/v1"
    assert response.content == "responses-ok"
    assert response.model == "chat"


@pytest.mark.asyncio
async def test_stream_uses_catalog_openai_responses_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    registrations: list[dict[str, dict[str, Any]]] = []

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _completion_stream()

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.register_model",
        lambda model_cost: registrations.append(model_cost),
    )
    router = ModelRouter(
        ModelConfig(provider="vendor", default_model="chat"),
        catalog=_responses_catalog(),
    )

    async def collect(content: str):
        return [
            chunk
            async for chunk in router.stream(
                [{"role": "user", "content": content}]
            )
        ]

    chunks, second_chunks = await asyncio.gather(
        collect("hello"),
        collect("hello again"),
    )

    assert captured["model"] == "openai/responses/vendor/model-v2"
    assert captured["api_base"] == "https://responses.vendor.example/v1"
    assert [chunk.token for chunk in chunks] == ["stream-ok"]
    assert [chunk.token for chunk in second_chunks] == ["stream-ok"]
    assert registrations == [
        {
            "openai/vendor/model-v2": {
                "litellm_provider": "openai",
                "mode": "responses",
                "supports_native_streaming": True,
            }
        }
    ]


@pytest.mark.asyncio
async def test_responses_stream_registration_failure_is_sanitized_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    leaked = "registry-internal-secret"

    async def fake_acompletion(**_kwargs: Any):
        nonlocal called
        called = True
        return _completion_stream()

    def fail_registration(_model_cost: dict[str, Any]) -> None:
        raise RuntimeError(leaked)

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.register_model",
        fail_registration,
    )
    router = ModelRouter(
        ModelConfig(provider="vendor", default_model="chat"),
        catalog=_responses_catalog(),
    )

    with pytest.raises(ProviderRuntimeError, match="无法启用 Responses 原生流式") as error:
        _ = [
            chunk
            async for chunk in router.stream(
                [{"role": "user", "content": "hello"}]
            )
        ]

    assert leaked not in str(error.value)
    assert called is False


@pytest.mark.asyncio
async def test_unsupported_catalog_format_fails_before_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
        nonlocal called
        called = True
        return _completion_response()

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    router = ModelRouter(
        ModelConfig(provider="vendor", default_model="chat"),
        catalog=_anthropic_catalog(),
    )

    with pytest.raises(ProviderRuntimeError, match="anthropic_messages.*尚未实现"):
        await router.call([{"role": "user", "content": "hello"}])

    assert called is False


@pytest.mark.asyncio
async def test_catalog_alias_preserves_kimi_thinking_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIMI_CHAT_KEY", "kimi-selected-secret")
    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.get_model_info",
        lambda _model: {},
    )
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _completion_response()

    monkeypatch.setattr(
        "naumi_agent.model.router.litellm.acompletion",
        fake_acompletion,
    )
    router = ModelRouter(
        ModelConfig(provider="kimi", default_model="coding"),
        catalog=_kimi_chat_catalog(),
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "prior-reasoning",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "done"},
    ]

    await router.call(messages)

    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["messages"][0]["reasoning_content"] == "prior-reasoning"
