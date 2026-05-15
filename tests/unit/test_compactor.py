"""上下文压缩器测试."""

import pytest

from naumi_agent.config.settings import MemoryConfig, ModelConfig
from naumi_agent.memory.compactor import ContextCompactor
from naumi_agent.model.router import ModelRouter


@pytest.fixture
def compactor() -> ContextCompactor:
    config = MemoryConfig()
    router = ModelRouter(ModelConfig())
    return ContextCompactor(config, router, threshold=0.75, max_messages=50)


class TestContextCompactor:
    def test_should_not_compact_small(self, compactor: ContextCompactor) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        assert not compactor.should_compact(messages, max_tokens=100000)

    def test_should_compact_many_messages(self, compactor: ContextCompactor) -> None:
        messages = [{"role": "user", "content": f"message {i}"} for i in range(60)]
        assert compactor.should_compact(messages, max_tokens=100000)

    def test_should_compact_high_tokens(self, compactor: ContextCompactor) -> None:
        # Use realistic words that tokenize less efficiently than single chars
        long_content = "hello world this is a test " * 2000  # ~50K chars, many tokens
        messages = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
        ]
        assert compactor.should_compact(messages, max_tokens=3000)

    def test_estimate_tokens(self, compactor: ContextCompactor) -> None:
        messages = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 200},
        ]
        estimated = compactor._estimate_tokens(messages)
        # litellm tokenizer returns actual token count (roughly chars/4 with overhead)
        assert estimated > 0
        assert estimated < 500  # sanity upper bound

    def test_messages_to_text(self, compactor: ContextCompactor) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        ]
        text = compactor._messages_to_text(messages)
        assert "[User]" in text
        assert "[Assistant]" in text
        assert "[Tool Result" in text

    async def test_compact_preserves_system(self, compactor: ContextCompactor) -> None:
        messages = [
            {"role": "system", "content": "system prompt"},
            *[{"role": "user", "content": f"msg {i}"} for i in range(10)],
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ]
        # 不需要实际 LLM 调用，测试数据不足以触发压缩
        result = await compactor.compact(messages, max_tokens=100000)
        # 应保持原样
        assert len(result) == len(messages)
