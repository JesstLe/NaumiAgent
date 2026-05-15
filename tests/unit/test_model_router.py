"""模型路由单元测试."""

from __future__ import annotations

import pytest

from naumi_agent.config.settings import ModelConfig, ModelMeta
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
