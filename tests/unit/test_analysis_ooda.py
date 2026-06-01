"""OODA analysis tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import OODATool, _build_ooda_report, _scan_ooda


def _write_ooda_source(path: Path) -> None:
    path.write_text(
        """
from time import sleep


def automate(driver):
    sleep(5)
    button = driver.find_element("id", "submit")
    button.click()
""",
        encoding="utf-8",
    )


def test_scan_ooda_reports_fragility_and_coverage(tmp_path: Path) -> None:
    source = tmp_path / "bot.py"
    _write_ooda_source(source)
    text = source.read_text(encoding="utf-8")

    scan = _scan_ooda([source], text, "稳定提交按钮")

    assert "脆弱模式" in scan
    assert "硬编码等待时间" in scan
    assert "OODA 覆盖" in scan
    assert "脆弱性评分" in scan


def test_build_ooda_report_contains_loop_and_self_healing(tmp_path: Path) -> None:
    source = tmp_path / "bot.py"
    _write_ooda_source(source)
    scan = _scan_ooda([source], source.read_text(encoding="utf-8"), "稳定提交按钮")

    report = _build_ooda_report(scan, [source], task="稳定提交按钮")

    assert "## OODA 确定性任务指挥" in report
    assert "Observe" in report
    assert "Self-Healing Mechanisms" in report
    assert "Anti-Fragility Checklist" in report


class TestOODATool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_ooda_design(self, tmp_path: Path) -> None:
        source = tmp_path / "bot.py"
        _write_ooda_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await OODATool().execute(target=str(source), task="稳定提交按钮")

        assert "## OODA 确定性任务指挥" in output
        assert "Resilience Score" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_design_and_adds_enhancement(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "bot.py"
        _write_ooda_source(source)
        mock_response = ModelResponse(
            content="增强：用显式等待替代 sleep。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await OODATool().execute(target=str(source), task="稳定提交按钮")

        assert "## OODA 确定性任务指挥" in output
        assert "## LLM OODA 增强" in output
        assert "显式等待" in output
