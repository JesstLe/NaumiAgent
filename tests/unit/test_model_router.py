"""模型路由单元测试."""

import pytest

from naumi_agent.config.settings import ModelConfig
from naumi_agent.model.router import ModelRouter, ModelTier


@pytest.fixture
def router() -> ModelRouter:
    config = ModelConfig(
        default_model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
        reasoning_model="claude-opus-4-7",
    )
    return ModelRouter(config)


class TestModelRouter:
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
