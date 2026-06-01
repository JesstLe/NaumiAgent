"""COOE analysis tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import COOETool, _build_cooe_report, _scan_cooe
from naumi_agent.tools.analysis_tools.cooe import COOETool as SplitCOOETool


def _write_cooe_source(path: Path) -> None:
    path.write_text(
        """
import asyncio


async def pipeline(client):
    a = await client.fetch_a()
    b = await client.fetch_b()
    return await process(a, b)


async def process(a, b):
    return a + b
""",
        encoding="utf-8",
    )


def test_scan_cooe_detects_io_and_call_graph(tmp_path: Path) -> None:
    source = tmp_path / "pipeline.py"
    _write_cooe_source(source)
    text = source.read_text(encoding="utf-8")

    scan = _scan_cooe([source], text, "fetch api, read file, 汇总报告")

    assert "I/O 阻塞操作" in scan
    assert "调用图" in scan
    assert "ROB 基础设施" in scan


def test_build_cooe_report_contains_dag_and_rob() -> None:
    report = _build_cooe_report(
        "fetch api, read file, 汇总报告",
        "- I/O 阻塞操作: 2 处",
    )

    assert "## COOE 确定性 DAG 调度" in report
    assert "Task Decomposition" in report
    assert "DAG Visualization" in report
    assert "ROB Configuration" in report


class TestCOOETool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_schedule(self, tmp_path: Path) -> None:
        source = tmp_path / "pipeline.py"
        _write_cooe_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await COOETool().execute(
                task="fetch api, read file, 汇总报告",
                target=str(source),
            )

        assert "## COOE 确定性 DAG 调度" in output
        assert "Reservation Stations" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_schedule_and_adds_enhancement(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "pipeline.py"
        _write_cooe_source(source)
        mock_response = ModelResponse(
            content="增强：将 fetch_a 和 fetch_b 放入 asyncio.gather。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await COOETool().execute(task="fetch api, read file", target=str(source))

        assert "## COOE 确定性 DAG 调度" in output
        assert "## LLM COOE 架构增强" in output
        assert "asyncio.gather" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_file_and_router_dependencies(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "pipeline.py"
        _write_cooe_source(source)

        async def run_analysis(router, system_prompt: str, user_msg: str) -> str:
            assert router == "router"
            assert "Out-of-Order Execution" in system_prompt
            assert "fetch api" in user_msg
            assert "I/O 阻塞操作" in user_msg
            return "注入 COOE 增强"

        tool = SplitCOOETool(
            router_getter=lambda: "router",
            run_analysis=run_analysis,
            resolve_target=lambda raw: [Path(raw)],
            read_sources=lambda files: "\n".join(
                file.read_text(encoding="utf-8") for file in files
            ),
        )

        output = await tool.execute(task="fetch api, read file", target=str(source))

        assert "## COOE 确定性 DAG 调度" in output
        assert "## LLM COOE 架构增强" in output
        assert "注入 COOE 增强" in output
