"""模型路由单元测试."""

from __future__ import annotations

import json

import pytest

from naumi_agent.config.settings import ModelConfig, ModelMeta
from naumi_agent.model.catalog import parse_provider_catalog_json
from naumi_agent.model.router import ModelRouter, ModelTier


@pytest.fixture
def router() -> ModelRouter:
    config = ModelConfig(
        default_model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
        reasoning_model="claude-opus-4-7",
    )
    return ModelRouter(config)


@pytest.fixture
def router_with_custom() -> ModelRouter:
    config = ModelConfig(
        default_model="openai/my-model",
        fast_model="openai/my-model",
        reasoning_model="openai/my-model",
        model_info={
            "openai/my-model": ModelMeta(
                max_context=128000,
                max_output=4096,
                input_cost_per_million=1.0,
                output_cost_per_million=2.0,
            ),
        },
    )
    return ModelRouter(config)


class TestResolveTier:
    def test_resolve_tier(self, router: ModelRouter) -> None:
        assert router.resolve_model(ModelTier.FAST) == "claude-haiku-4-5"
        assert router.resolve_model(ModelTier.CAPABLE) == "claude-sonnet-4-6"
        assert router.resolve_model(ModelTier.REASONING) == "claude-opus-4-7"

    def test_resolve_tier_by_string(self, router: ModelRouter) -> None:
        assert router.resolve_model("fast") == "claude-haiku-4-5"
        assert router.resolve_model("capable") == "claude-sonnet-4-6"

    def test_invalid_tier_raises(self, router: ModelRouter) -> None:
        with pytest.raises(ValueError):
            router.resolve_model("nonexistent")

    def test_resolves_catalog_target_without_changing_tier_value(self) -> None:
        catalog = parse_provider_catalog_json(
            json.dumps(
                {
                    "provider": {
                        "nvidia": {
                            "models": {
                                "local-glm": {"upstreamId": "z-ai/glm4.7"},
                            }
                        }
                    }
                }
            )
        )
        config = ModelConfig(provider="nvidia", default_model="local-glm")
        router = ModelRouter(config, catalog=catalog)

        target = router.resolve_target("local-glm")

        assert target.canonical_model == "nvidia/local-glm"
        assert target.upstream_model == "z-ai/glm4.7"
        assert router.resolve_model(ModelTier.CAPABLE) == "local-glm"

    def test_runtime_identity_exposes_catalog_provider_protocol_and_upstream(self) -> None:
        catalog = parse_provider_catalog_json(
            json.dumps(
                {
                    "providers": {
                        "nvidia": {
                            "apiFormat": "openai_responses",
                            "baseURL": "https://integrate.api.nvidia.com/v1",
                            "models": {
                                "local-glm": {"upstreamId": "z-ai/glm4.7"},
                            },
                        }
                    }
                }
            )
        )
        router = ModelRouter(
            ModelConfig(provider="nvidia", default_model="local-glm"),
            catalog=catalog,
        )

        identity = router.get_runtime_identity("local-glm")

        assert identity.requested_model == "local-glm"
        assert identity.canonical_model == "nvidia/local-glm"
        assert identity.upstream_model == "z-ai/glm4.7"
        assert identity.provider == "nvidia"
        assert identity.api_format == "openai_responses"
        assert identity.source == "catalog"

    def test_runtime_identity_keeps_legacy_provider_without_guessing_protocol(self) -> None:
        router = ModelRouter(
            ModelConfig(provider="custom-gateway", default_model="vendor/model")
        )

        identity = router.get_runtime_identity("vendor/model")

        assert identity.provider == "custom-gateway"
        assert identity.api_format == "legacy"
        assert identity.upstream_model == "vendor/model"
        assert identity.source == "legacy"

    def test_runtime_identity_does_not_invent_missing_catalog_protocol(self) -> None:
        catalog = parse_provider_catalog_json(
            json.dumps(
                {
                    "provider": {
                        "custom": {
                            "models": {"model-a": {"upstreamId": "model-a-v2"}},
                        }
                    }
                }
            )
        )
        router = ModelRouter(
            ModelConfig(provider="custom", default_model="model-a"),
            catalog=catalog,
        )

        identity = router.get_runtime_identity("model-a")

        assert identity.provider == "custom"
        assert identity.api_format == ""
        assert identity.upstream_model == "model-a-v2"


class TestModelInfo:
    def test_litellm_known_model(self, router: ModelRouter) -> None:
        info = router.get_model_info("claude-sonnet-4-6")
        assert "max_input_tokens" in info
        assert info["max_input_tokens"] > 0

    def test_context_window(self, router: ModelRouter) -> None:
        window = router.get_context_window("claude-sonnet-4-6")
        assert window > 0

    def test_max_output(self, router: ModelRouter) -> None:
        output = router.get_max_output("claude-sonnet-4-6")
        assert output > 0

    def test_cost_rates(self, router: ModelRouter) -> None:
        rates = router.get_cost_rates("claude-sonnet-4-6")
        assert "input" in rates
        assert "output" in rates
        assert rates["input"] > 0
        assert rates["output"] > 0

    def test_unknown_model_fallback(self, router: ModelRouter) -> None:
        window = router.get_context_window("totally-unknown-model-xyz")
        assert window == 128_000  # fallback

    def test_unknown_model_max_output_fallback(self, router: ModelRouter) -> None:
        output = router.get_max_output("totally-unknown-model-xyz")
        assert output == 4_096  # fallback

    def test_custom_model_override(self, router_with_custom: ModelRouter) -> None:
        info = router_with_custom.get_model_info("openai/my-model")
        assert info["max_input_tokens"] == 128_000
        assert info["max_output_tokens"] == 4_096

    def test_custom_model_cost_rates(self, router_with_custom: ModelRouter) -> None:
        rates = router_with_custom.get_cost_rates("openai/my-model")
        assert rates["input"] == 1.0
        assert rates["output"] == 2.0

    def test_info_cached(self, router: ModelRouter) -> None:
        info1 = router.get_model_info("claude-sonnet-4-6")
        info2 = router.get_model_info("claude-sonnet-4-6")
        assert info1 is info2  # same object

    def test_merges_metadata_per_field_and_caches_requested_aliases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = parse_provider_catalog_json(
            json.dumps(
                {
                    "provider": {
                        "nvidia": {
                            "models": {
                                "local-glm": {
                                    "upstreamId": "z-ai/glm4.7",
                                    "limit": {"context": 32_000, "output": 2_000},
                                },
                                "local-glm-copy": {
                                    "upstreamId": "z-ai/glm4.7",
                                    "limit": {"context": 48_000},
                                },
                            }
                        }
                    }
                }
            )
        )
        config = ModelConfig(
            provider="nvidia",
            model_info={
                "nvidia/local-glm": ModelMeta(
                    max_context=64_000,
                    max_output=8_000,
                    input_cost_per_million=3.0,
                ),
                "local-glm": ModelMeta(
                    max_context=96_000,
                    input_cost_per_million=5.0,
                ),
            },
        )
        requested_upstreams: list[str] = []

        def fake_get_model_info(model: str) -> dict[str, float | int]:
            requested_upstreams.append(model)
            return {
                "max_input_tokens": 8_000,
                "max_output_tokens": 1_000,
                "input_cost_per_token": 1.0 / 1_000_000,
                "output_cost_per_token": 2.0 / 1_000_000,
            }

        monkeypatch.setattr(
            "naumi_agent.model.router.litellm.get_model_info", fake_get_model_info
        )
        router = ModelRouter(config, catalog=catalog)

        primary = router.get_model_info("local-glm")
        copy = router.get_model_info("local-glm-copy")

        assert primary["max_input_tokens"] == 96_000
        assert primary["max_output_tokens"] == 8_000
        assert router.get_cost_rates("local-glm") == {"input": 5.0, "output": 2.0}
        assert copy["max_input_tokens"] == 48_000
        assert copy["max_output_tokens"] == 1_000
        assert primary is not copy
        assert requested_upstreams == ["z-ai/glm4.7", "z-ai/glm4.7"]


class TestMaxTokens:
    def test_requested_overrides_config(self, router: ModelRouter) -> None:
        result = router._resolve_max_tokens("claude-sonnet-4-6", 100)
        assert result == 100

    def test_config_value_when_no_request(self, router: ModelRouter) -> None:
        result = router._resolve_max_tokens("claude-sonnet-4-6", None)
        assert result == 4096  # default config value


class TestBaseKwargs:
    def test_no_api_base(self, router: ModelRouter) -> None:
        kw = router._base_kwargs()
        assert "api_base" not in kw
        assert "api_key" not in kw

    def test_with_api_base(self) -> None:
        config = ModelConfig(
            api_base="https://api.kimi.com/v1",
            api_key="sk-test",
        )
        r = ModelRouter(config)
        kw = r._base_kwargs()
        assert kw["api_base"] == "https://api.kimi.com/v1"
        assert kw["api_key"] == "sk-test"

    def test_kimi_user_agent(self) -> None:
        config = ModelConfig(
            api_base="https://api.kimi.com/coding/v1",
            api_key="sk-test",
        )
        r = ModelRouter(config)
        kw = r._base_kwargs()
        assert kw["extra_headers"]["User-Agent"] == "Kilo-Code/1.0"


class TestKimiThinkingModel:
    def test_kimi_k2_detected(self) -> None:
        config = ModelConfig(default_model="openai/kimi-k2.6")
        r = ModelRouter(config)
        assert r._is_kimi_thinking_model("openai/kimi-k2.6")

    def test_kimi_k25_detected(self) -> None:
        config = ModelConfig(default_model="openai/kimi-k2.5")
        r = ModelRouter(config)
        assert r._is_kimi_thinking_model("openai/kimi-k2.5")

    def test_kimi_latest_detected(self) -> None:
        config = ModelConfig(default_model="openai/kimi-latest")
        r = ModelRouter(config)
        assert r._is_kimi_thinking_model("openai/kimi-latest")

    def test_kimi_for_coding_not_detected(self) -> None:
        config = ModelConfig(default_model="openai/kimi-for-coding")
        r = ModelRouter(config)
        assert not r._is_kimi_thinking_model("openai/kimi-for-coding")

    def test_non_kimi_not_detected(self) -> None:
        config = ModelConfig(default_model="claude-sonnet-4-6")
        r = ModelRouter(config)
        assert not r._is_kimi_thinking_model("claude-sonnet-4-6")


class TestSanitizeMessages:
    def test_strips_reasoning_content_by_default(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "需要读文件",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ]

        sanitized = ModelRouter._sanitize_messages(messages)

        assert "reasoning_content" not in sanitized[0]

    def test_preserves_reasoning_content_for_thinking_tool_history(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "需要读文件",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ]

        sanitized = ModelRouter._sanitize_messages(
            messages,
            preserve_reasoning_content=True,
        )

        assert sanitized[0]["reasoning_content"] == "需要读文件"

    def test_adds_blank_reasoning_content_for_thinking_tool_history(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ]

        sanitized = ModelRouter._sanitize_messages(
            messages,
            preserve_reasoning_content=True,
        )

        assert sanitized[0]["reasoning_content"] == ""

    def test_kimi_thinking_preserves_reasoning_content(self) -> None:
        config = ModelConfig(default_model="openai/kimi-k2.6")
        router = ModelRouter(config)

        assert router._should_preserve_reasoning_content(
            "openai/kimi-k2.6", None,
        )
        assert not router._should_preserve_reasoning_content(
            "openai/kimi-k2.6", {"type": "disabled"},
        )

    def test_kimi_for_coding_preserves_reasoning_content(self) -> None:
        config = ModelConfig(
            default_model="openai/kimi-for-coding",
            api_base="https://api.kimi.com/coding/v1",
        )
        router = ModelRouter(config)

        assert router._should_preserve_reasoning_content(
            "openai/kimi-for-coding", None,
        )

    def test_keeps_complete_trailing_tool_results(self) -> None:
        messages = [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ]

        sanitized = ModelRouter._sanitize_messages(messages)

        assert sanitized == messages

    def test_drops_incomplete_trailing_tool_sequence(self) -> None:
        messages = [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ]

        sanitized = ModelRouter._sanitize_messages(messages)

        assert sanitized == [{"role": "user", "content": "read file"}]

    def test_fills_missing_middle_tool_result_before_next_user_message(self) -> None:
        messages = [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
            {"role": "user", "content": "发生了什么"},
        ]

        sanitized = ModelRouter._sanitize_messages(messages)

        assert sanitized[2]["tool_call_id"] == "call_1"
        assert sanitized[3]["role"] == "tool"
        assert sanitized[3]["tool_call_id"] == "call_2"
        assert "工具调用结果缺失" in sanitized[3]["content"]
        assert sanitized[4] == {"role": "user", "content": "发生了什么"}
