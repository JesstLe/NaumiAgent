"""Static analysis mode fallback tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    ChaosAnalysisTool,
    ScaleAnalysisTool,
    StateAuditTool,
)


def _write_vulnerable_source(path: Path) -> None:
    path.write_text(
        """
import requests

cache = {}


def fetch(url):
    return requests.get(url)


def save_session(user):
    cache[user] = {"active": True}
""",
        encoding="utf-8",
    )


class TestStaticAnalysisFallbacks:
    @pytest.mark.asyncio
    async def test_chaos_returns_static_scan_without_router(self, tmp_path: Path) -> None:
        source = tmp_path / "service.py"
        _write_vulnerable_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await ChaosAnalysisTool().execute(target=str(source))

        assert "## Chaos 静态扫描" in output
        assert "无 timeout 的外部 HTTP 调用" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_scale_returns_static_scan_without_router(self, tmp_path: Path) -> None:
        source = tmp_path / "service.py"
        _write_vulnerable_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await ScaleAnalysisTool().execute(target=str(source), qps=5000)

        assert "## Scale 静态扫描（目标 QPS: 5,000）" in output
        assert "同步阻塞 I/O 调用" in output
        assert "目标 QPS: 5,000" in output
        assert "模型路由未初始化" in output

    @pytest.mark.asyncio
    async def test_state_returns_static_scan_without_router(self, tmp_path: Path) -> None:
        source = tmp_path / "service.py"
        _write_vulnerable_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await StateAuditTool().execute(target=str(source))

        assert "## State 静态扫描" in output
        assert "模块级可变容器" in output
        assert "云原生就绪评分" in output
        assert "模型路由未初始化" in output

    @pytest.mark.asyncio
    async def test_chaos_with_router_keeps_static_scan_and_adds_llm(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "service.py"
        _write_vulnerable_source(source)
        mock_response = ModelResponse(
            content="LLM 推演：外部依赖超时会放大故障。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await ChaosAnalysisTool().execute(target=str(source))

        assert "## Chaos 静态扫描" in output
        assert "## LLM 灾难推演" in output
        assert "LLM 推演" in output
