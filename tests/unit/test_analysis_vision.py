"""Vision analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    VisionTool,
    _build_vision_inventory_script,
    _build_vision_report,
    _scan_vision,
)
from naumi_agent.tools.analysis_support.vision import detect_data_types


def _minimal_png(width: int = 2, height: int = 3) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def test_scan_vision_detects_obstacles_and_data_types() -> None:
    scan = _scan_vision("登录后页面有 Cloudflare，提取表格和价格走势图")

    assert "反爬虫/访问障碍" in scan
    assert "CDN/WAF 防护" in scan
    assert "table" in scan
    assert "chart" in scan
    assert "number" in scan


def test_detect_data_types() -> None:
    matches = detect_data_types("从 K线 图表 和 榜单 表格 提取价格")
    types = {item[0] for item in matches}

    assert {"chart", "table", "number"} <= types


def test_build_vision_inventory_script_reads_png_dimensions(tmp_path: Path) -> None:
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(_minimal_png(7, 9))
    script = tmp_path / "vision_inventory.py"
    script.write_text(_build_vision_inventory_script("提取表格价格"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(screenshot)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["images"][0]["width"] == 7
    assert payload["images"][0]["height"] == 9
    assert payload["images"][0]["sha256"]
    assert payload["roi_contract"][0]["fields"][0]["data_type"] == "table"


def test_build_vision_report_contains_contract_and_script() -> None:
    report = _build_vision_report(
        "提取登录页面中的表格和价格",
        _scan_vision("提取登录页面中的表格和价格"),
    )

    assert "## Vision 确定性视觉提取方案" in report
    assert "## Screenshot Inventory Script" in report
    assert "## 提取契约" in report
    assert "禁止直接编造屏幕数据" in report


class TestVisionTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(self) -> None:
        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await VisionTool().execute(task="提取验证码后页面中的表格价格")

        assert "## Vision 确定性视觉提取方案" in output
        assert "Screenshot Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(self) -> None:
        mock_response = ModelResponse(
            content="增强：先固定截图 hash，再标注 ROI。",
            usage=TokenUsage(input_tokens=14, output_tokens=7, total_tokens=21),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await VisionTool().execute(task="提取价格走势图")

        assert "## Vision 确定性视觉提取方案" in output
        assert "## LLM Vision 增强" in output
        assert "截图 hash" in output

