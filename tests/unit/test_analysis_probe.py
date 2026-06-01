"""Probe analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    ProbeTool,
    _build_probe_report,
    _build_probe_script,
    _scan_probe,
)
from naumi_agent.tools.analysis_support.probe import select_probe_modes
from naumi_agent.tools.analysis_tools.probe import ProbeTool as SplitProbeTool


def test_scan_probe_flags_unknown_system() -> None:
    scan = _scan_probe("给某个闭源游戏写内部 API 调用，需要内存 offset", "")

    assert "未知系统特征" in scan
    assert "幻觉风险评分" in scan
    assert "必须先探测真实接口" in scan
    assert "memory" in scan


def test_select_probe_modes_from_task_and_context() -> None:
    modes = select_probe_modes(
        "探测私有 API endpoint 和本地配置",
        "https://internal.example.test/v1",
    )

    assert "network" in modes
    assert "file" in modes


def test_build_probe_script_is_runnable_and_readonly(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"ok": true}', encoding="utf-8")
    script = tmp_path / "probe.py"
    script.write_text(
        _build_probe_script("探测 python 模块和配置文件", "https://example.test/api"),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), "json", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert "reflection" in payload["probe_modes"]
    assert "file" in payload["probe_modes"]
    assert payload["reflection"][0]["module"] == "json"
    assert any("config.json" in item["path"] for item in payload["files"])


def test_build_probe_report_contains_script_and_template() -> None:
    report = _build_probe_report(
        "探测私有 API，没有文档",
        "https://internal.example.test/v1",
        _scan_probe("探测私有 API，没有文档", "https://internal.example.test/v1"),
    )

    assert "## Probe 确定性反幻觉协议" in report
    assert "## Read-only Probe Script" in report
    assert "## 信息回填模板" in report
    assert "禁止把猜测 offset 当事实" in report


class TestProbeTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_probe_protocol(self) -> None:
        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await ProbeTool().execute(task="给某个闭源游戏写内部 API 调用")

        assert "## Probe 确定性反幻觉协议" in output
        assert "Read-only Probe Script" in output
        assert "信息回填模板" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_protocol_and_adds_enhancement(self) -> None:
        mock_response = ModelResponse(
            content="增强：先探测模块版本，再映射真实 API。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await ProbeTool().execute(task="探测 python inspect API")

        assert "## Probe 确定性反幻觉协议" in output
        assert "## LLM 探测增强" in output
        assert "模块版本" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_router_runner(self) -> None:
        async def run_analysis(router, system_prompt: str, user_msg: str) -> str:
            assert router == "router"
            assert "Black-Box Probe" in system_prompt
            assert "探测 python inspect API" in user_msg
            assert "Read-only Probe Script" in user_msg
            return "注入探测增强"

        tool = SplitProbeTool(
            router_getter=lambda: "router",
            run_analysis=run_analysis,
        )

        output = await tool.execute(
            task="探测 python inspect API",
            context="inspect module",
        )

        assert "## Probe 确定性反幻觉协议" in output
        assert "## LLM 探测增强" in output
        assert "注入探测增强" in output
