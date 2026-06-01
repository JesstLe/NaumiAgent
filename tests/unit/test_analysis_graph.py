"""GraphRAG analysis tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import GraphRAGTool, _scan_graph
from naumi_agent.tools.analysis_tools.graph import GraphRAGTool as SplitGraphRAGTool


def _write_graph_project(root: Path) -> None:
    (root / "alpha.py").write_text(
        """
from beta import Beta


class Alpha(Beta):
    def run(self):
        return Beta()
""",
        encoding="utf-8",
    )
    (root / "beta.py").write_text(
        """
import alpha


class Beta:
    def value(self):
        return 1
""",
        encoding="utf-8",
    )


def test_scan_graph_extracts_methods_and_cycles(tmp_path: Path) -> None:
    _write_graph_project(tmp_path)
    files = sorted(tmp_path.glob("*.py"))

    scan = _scan_graph(files, "")

    assert "alpha:Alpha" in scan
    assert "alpha:Alpha.run" in scan
    assert "beta:Beta.value" in scan
    assert "循环依赖" in scan
    assert "核心节点" in scan


class TestGraphRAGTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_static_graph(self, tmp_path: Path) -> None:
        _write_graph_project(tmp_path)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await GraphRAGTool().execute(target=str(tmp_path))

        assert "## GraphRAG 静态图谱" in output
        assert "实体节点" in output
        assert "关系边" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_graph_and_adds_enhancement(
        self,
        tmp_path: Path,
    ) -> None:
        _write_graph_project(tmp_path)
        mock_response = ModelResponse(
            content="拓扑建议：打破 alpha 与 beta 的循环依赖。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await GraphRAGTool().execute(target=str(tmp_path))

        assert "## GraphRAG 静态图谱" in output
        assert "## LLM 图谱推演" in output
        assert "循环依赖" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_file_and_router_dependencies(
        self,
        tmp_path: Path,
    ) -> None:
        _write_graph_project(tmp_path)

        async def run_analysis(router, system_prompt: str, user_msg: str) -> str:
            assert router == "router"
            assert "GraphRAG" in system_prompt
            assert "循环依赖" in user_msg
            assert "class Alpha" in user_msg
            return "注入图谱推演"

        tool = SplitGraphRAGTool(
            router_getter=lambda: "router",
            run_analysis=run_analysis,
            resolve_target=lambda raw: sorted(Path(raw).glob("*.py")),
            read_sources=lambda files: "\n".join(
                file.read_text(encoding="utf-8") for file in files
            ),
            cwd_getter=lambda: tmp_path,
        )

        output = await tool.execute(target=str(tmp_path))

        assert "## GraphRAG 静态图谱" in output
        assert "## LLM 图谱推演" in output
        assert "注入图谱推演" in output
