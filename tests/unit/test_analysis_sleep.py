"""Sleep pruning analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    SleepPruningTool,
    _build_sleep_inventory_script,
    _build_sleep_report,
    _scan_sleep,
)


def _sleep_context() -> str:
    return """
目标：继续落地工具，更新 changelog 和版本。
已完成：page 工具通过 ruff 和 pytest 验证。
commit e52c0df version 0.1.29
下一步：处理 sleep 工具。
原始输出很长，已完成细节可以修剪。
""".strip()


def test_scan_sleep_reports_topics_and_context_size() -> None:
    scan = _scan_sleep([], _sleep_context(), _sleep_context())

    assert "对话主题分布" in scan
    assert "测试验证" in scan
    assert "会话上下文" in scan


def test_build_sleep_inventory_script_is_runnable(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.md"
    transcript.write_text(_sleep_context(), encoding="utf-8")
    script = tmp_path / "sleep_inventory.py"
    script.write_text(_build_sleep_inventory_script(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(transcript)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["estimated_tokens"] > 0
    assert payload["topics"]["交付/版本"] >= 1
    assert payload["sleep_contract"]["must_keep"]


def test_build_sleep_report_contains_evolution_patch() -> None:
    report = _build_sleep_report(
        _scan_sleep([], _sleep_context(), _sleep_context()),
        _sleep_context(),
        _sleep_context(),
    )

    assert "## Sleep 确定性突触修剪报告" in report
    assert "## Sleep Inventory Script" in report
    assert "## Evolution Patch" in report
    assert "sleep_contract" in report


class TestSleepPruningTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(self) -> None:
        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await SleepPruningTool().execute(
                session_context=_sleep_context(),
            )

        assert "## Sleep 确定性突触修剪报告" in output
        assert "Sleep Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(self) -> None:
        mock_response = ModelResponse(
            content="增强：把已完成工具压缩为 commit 摘要。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await SleepPruningTool().execute(
                session_context=_sleep_context(),
            )

        assert "## Sleep 确定性突触修剪报告" in output
        assert "## LLM Sleep 增强" in output
        assert "commit 摘要" in output
