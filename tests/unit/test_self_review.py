"""Self-review tool tests."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.tools.analysis import (
    SelfReviewTool,
    _find_agent_source_dir,
    _scan_self_review,
)


class TestFindAgentSourceDir:
    def test_locates_source_dir(self):
        path = _find_agent_source_dir()
        assert Path(path).is_dir()
        assert Path(path).name == "naumi_agent"
        assert (Path(path) / "__init__.py").exists()

    def test_contains_key_modules(self):
        path = _find_agent_source_dir()
        assert (Path(path) / "orchestrator").is_dir()
        assert (Path(path) / "tools").is_dir()
        assert (Path(path) / "memory").is_dir()


class TestScanSelfReview:
    def test_reports_file_count(self):
        files = [Path("/fake/file.py")]
        source = "def hello(): pass"
        result = _scan_self_review(files, source)
        assert "源文件: 1 个" in result

    def test_detects_bare_except(self):
        files = [Path("/fake/file.py")]
        source = "try:\n    pass\nexcept:\n    pass"
        result = _scan_self_review(files, source)
        assert "裸 except" in result

    def test_no_bare_except(self):
        files = [Path("/fake/file.py")]
        source = "try:\n    pass\nexcept ValueError:\n    pass"
        result = _scan_self_review(files, source)
        assert "无裸 except" in result

    def test_detects_hardcoded_secrets(self):
        files = [Path("/fake/file.py")]
        source = 'api_key = "sk-1234567890abcdef"'
        result = _scan_self_review(files, source)
        assert "硬编码密钥" in result

    def test_no_hardcoded_secrets(self):
        files = [Path("/fake/file.py")]
        source = 'api_key = os.environ["API_KEY"]'
        result = _scan_self_review(files, source)
        assert "无硬编码密钥" in result

    def test_counts_tool_registrations(self):
        files = [Path("/fake/file.py")]
        source = "registry.register(tool_a)\nregistry.register(tool_b)"
        result = _scan_self_review(files, source)
        assert "工具注册调用: 2 处" in result

    def test_counts_todo_markers(self):
        files = [Path("/fake/file.py")]
        source = "# TODO: fix this\n# FIXME: that"
        result = _scan_self_review(files, source)
        assert "TODO/FIXME/HACK" in result

    def test_reports_async_ratio(self):
        files = [Path("/fake/file.py")]
        source = "async def a(): pass\ndef b(): pass"
        result = _scan_self_review(files, source)
        assert "async/sync 函数比" in result

    def test_reports_logging(self):
        files = [Path("/fake/file.py")]
        source = "logger.info('hi')\nprint('debug')"
        result = _scan_self_review(files, source)
        assert "logger 调用: 1" in result
        assert "print 调用: 1" in result


class TestSelfReviewTool:
    def test_tool_name(self):
        assert SelfReviewTool().name == "self_review"

    def test_tool_description(self):
        desc = SelfReviewTool().description
        assert "审查" in desc or "源代码" in desc

    def test_tool_schema(self):
        schema = SelfReviewTool().parameters_schema
        assert "focus" in schema["properties"]
        assert "module" in schema["properties"]
        assert len(schema["required"]) == 0

    @pytest.mark.asyncio
    async def test_execute_scans_agent_source(self):
        from naumi_agent.model.router import ModelResponse, TokenUsage

        tool = SelfReviewTool()
        mock_response = ModelResponse(
            content="## 自我审查报告\n整体评分: B",
            usage=TokenUsage(
                input_tokens=100, output_tokens=50,
                total_tokens=150, cost_usd=0.01,
            ),
            model="test",
        )

        with patch(
            "naumi_agent.tools.analysis._global_router",
        ) as mock_router:
            mock_router.call = AsyncMock(return_value=mock_response)
            result = await tool.execute(focus="quality")

        assert "自我审查" in result or "评分" in result

    @pytest.mark.asyncio
    async def test_execute_with_module(self):
        from naumi_agent.model.router import ModelResponse, TokenUsage

        tool = SelfReviewTool()
        mock_response = ModelResponse(
            content="Report for memory module",
            usage=TokenUsage(
                input_tokens=100, output_tokens=50,
                total_tokens=150, cost_usd=0.01,
            ),
            model="test",
        )

        with patch(
            "naumi_agent.tools.analysis._global_router",
        ) as mock_router:
            mock_router.call = AsyncMock(return_value=mock_response)
            result = await tool.execute(module="memory")

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_execute_no_router(self):

        tool = SelfReviewTool()
        with patch("naumi_agent.tools.analysis._global_router", None):
            result = await tool.execute()

        assert "未初始化" in result or "Router" in result
