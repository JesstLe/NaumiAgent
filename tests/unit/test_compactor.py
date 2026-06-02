"""上下文压缩器测试."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.config.settings import MemoryConfig, ModelConfig
from naumi_agent.memory.compactor import ContextCompactor, _parse_extraction_response
from naumi_agent.model.router import ModelResponse, ModelRouter, TokenUsage


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

    def test_offload_large_tool_result_writes_artifact(self, tmp_path) -> None:
        config = MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        router = ModelRouter(ModelConfig())
        compactor = ContextCompactor(config, router)
        large_content = "line\n" * 4000
        messages = [
            {"role": "user", "content": "run command"},
            {"role": "tool", "tool_call_id": "call/1", "content": large_content},
        ]

        updated, archived = compactor.offload_large_tool_results(
            messages,
            min_chars=1_000,
        )

        assert len(archived) == 1
        artifact_path = archived[0].path
        assert "compaction-artifacts" in artifact_path
        assert "call_1" in artifact_path
        assert archived[0].chars == len(large_content)
        assert Path(artifact_path).read_text(encoding="utf-8") == large_content
        placeholder = updated[1]["content"]
        assert "[大型工具结果已归档]" in placeholder
        assert "artifact:" in placeholder
        assert "预览：" in placeholder
        assert len(placeholder) < len(large_content)

    def test_offload_large_tool_result_is_idempotent(self, tmp_path) -> None:
        config = MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        router = ModelRouter(ModelConfig())
        compactor = ContextCompactor(config, router)
        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "[大型工具结果已归档]\nartifact: /tmp/x",
            },
        ]

        updated, archived = compactor.offload_large_tool_results(messages, min_chars=1)

        assert archived == []
        assert updated == messages

    def test_sanitize_visual_payloads_replaces_inline_image_content(
        self,
        compactor: ContextCompactor,
    ) -> None:
        data_url = "data:image/png;base64," + ("a" * 2048)
        messages = [
            {"role": "user", "content": f"看这个截图：{data_url}"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请分析"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

        updated, count = compactor.sanitize_visual_payloads(messages)

        assert count == 2
        assert "base64_chars=2048" in updated[0]["content"]
        assert "a" * 512 not in str(updated)
        assert updated[1]["content"][1] == {
            "type": "text",
            "text": "[图片内容已省略: data:image/png;base64, base64_chars=2048]",
        }

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

    async def test_compact_appends_runtime_snapshot(
        self,
        compactor: ContextCompactor,
    ) -> None:
        messages = [
            {"role": "system", "content": "system prompt"},
            *[{"role": "user", "content": f"msg {i} " * 50} for i in range(8)],
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ]
        summary_response = ModelResponse(
            content="## 任务目标\n继续优化项目",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch.object(
            compactor._router,
            "call",
            new_callable=AsyncMock,
            return_value=summary_response,
        ):
            result = await compactor.compact(
                messages,
                max_tokens=1000,
                runtime_snapshot="### Todo 状态\n- #1 [blocked] 等待用户确认",
            )

        summary_messages = [
            msg for msg in result
            if "压缩时保留的运行时状态" in str(msg.get("content", ""))
        ]
        assert summary_messages
        assert "等待用户确认" in summary_messages[0]["content"]


class TestParseExtractionResponse:
    def test_valid_json_array(self):
        text = '[{"content": "Uses FastAPI", "category": "fact"}]'
        result = _parse_extraction_response(text)
        assert len(result) == 1
        assert result[0]["content"] == "Uses FastAPI"
        assert result[0]["category"] == "fact"

    def test_json_in_code_block(self):
        text = '```json\n[{"content": "Prefers dark theme", "category": "preference"}]\n```'
        result = _parse_extraction_response(text)
        assert len(result) == 1
        assert result[0]["category"] == "preference"

    def test_empty_array(self):
        text = "[]"
        result = _parse_extraction_response(text)
        assert result == []

    def test_invalid_json(self):
        text = "Not JSON at all"
        result = _parse_extraction_response(text)
        assert result == []

    def test_filters_items_without_content(self):
        text = '[{"content": "valid"}, {"category": "no content"}]'
        result = _parse_extraction_response(text)
        assert len(result) == 1

    def test_multiple_items(self):
        text = """```json
[
  {"content": "Project uses Python 3.12", "category": "fact"},
  {"content": "User prefers type hints", "category": "preference"},
  {"content": "Chose Redis over Memcached", "category": "decision"}
]
```"""
        result = _parse_extraction_response(text)
        assert len(result) == 3


class TestMemoryExtraction:
    @pytest.mark.asyncio
    async def test_extract_stores_to_memory(self, tmp_path):
        """Compaction with memory backend stores extracted facts."""
        config = MemoryConfig(vector_db_path=str(tmp_path / "chroma"))
        router = ModelRouter(ModelConfig())
        memory = MagicMock()
        memory.store = AsyncMock(return_value="mem123")

        compactor = ContextCompactor(
            config, router, threshold=0.75, max_messages=5,
            long_term_memory=memory,
        )

        # Mock the LLM to return extraction results + summary
        extraction_response = ModelResponse(
            content='[{"content": "Uses FastAPI", "category": "fact"}]',
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test",
        )
        summary_response = ModelResponse(
            content="## 任务目标\nBuild an API",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test",
        )

        messages = [
            {"role": "system", "content": "system"},
            *[{"role": "user", "content": f"msg {i} " * 50} for i in range(8)],
            {"role": "user", "content": "latest"},
            {"role": "assistant", "content": "answer"},
        ]

        with patch.object(
            router, "call", new_callable=AsyncMock,
            side_effect=[extraction_response, summary_response],
        ):
            await compactor.compact(messages, max_tokens=1000)

        assert memory.store.call_count == 1

    @pytest.mark.asyncio
    async def test_no_extraction_without_memory(self):
        """Compaction without memory backend does not attempt extraction."""
        config = MemoryConfig()
        router = ModelRouter(ModelConfig())
        compactor = ContextCompactor(
            config, router, threshold=0.75, max_messages=5,
            long_term_memory=None,
        )

        summary_response = ModelResponse(
            content="## 任务目标\nBuild an API",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001),
            model="test",
        )

        messages = [
            {"role": "system", "content": "system"},
            *[{"role": "user", "content": f"msg {i} " * 50} for i in range(8)],
            {"role": "user", "content": "latest"},
            {"role": "assistant", "content": "answer"},
        ]

        with patch.object(
            router, "call", new_callable=AsyncMock,
            return_value=summary_response,
        ) as mock_call:
            await compactor.compact(messages, max_tokens=1000)

        # Only 1 LLM call (summary), no extraction call
        assert mock_call.call_count == 1
