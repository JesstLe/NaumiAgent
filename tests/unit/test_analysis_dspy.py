"""DSPy analysis tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import DSPyTool
from naumi_agent.tools.analysis import _scan_dspy as analysis_scan_dspy
from naumi_agent.tools.analysis_support.dspy import build_dspy_baseline_metric, scan_dspy


def _write_prompt_source(path: Path) -> None:
    path.write_text(
        '''
SYSTEM_PROMPT = """You are a helpful assistant.

Example:
Input: hello
Output: 你好
"""


def evaluate_output(result):
    return "你好" in result
''',
        encoding="utf-8",
    )


def test_scan_dspy_reports_prompt_examples_and_metric(tmp_path: Path) -> None:
    source = tmp_path / "prompts.py"
    _write_prompt_source(source)

    scan = scan_dspy([source], source.read_text(encoding="utf-8"), "翻译问候语")

    assert "发现 Prompt 模板" in scan
    assert "Few-shot 示例" in scan
    assert "评估函数/Metric" in scan
    assert "DSPy 工程成熟度" in scan
    assert analysis_scan_dspy([source], source.read_text(encoding="utf-8"), "翻译问候语") == scan


def test_build_dspy_baseline_metric_is_executable() -> None:
    namespace: dict[str, object] = {}
    exec(build_dspy_baseline_metric("翻译问候语"), namespace)

    result = namespace["score_output"]("hello", "1. 你好")

    assert isinstance(result, dict)
    assert result["score"] > 0
    assert result["has_content"] is True


class TestDSPyTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_scan_and_metric(self, tmp_path: Path) -> None:
        source = tmp_path / "prompts.py"
        _write_prompt_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await DSPyTool().execute(
                target=str(source),
                prompt_target="翻译问候语",
            )

        assert "## DSPy 静态成熟度扫描" in output
        assert "## Baseline Metric" in output
        assert "score_output" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_scan_and_adds_compiler_advice(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "prompts.py"
        _write_prompt_source(source)
        mock_response = ModelResponse(
            content="建议：用真实标注集替换启发式 metric。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await DSPyTool().execute(target=str(source))

        assert "## DSPy 静态成熟度扫描" in output
        assert "## LLM Prompt 编译建议" in output
        assert "真实标注集" in output
