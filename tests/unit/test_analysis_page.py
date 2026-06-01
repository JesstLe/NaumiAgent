"""Memory page analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    MemoryPageTool,
    _build_page_inventory_script,
    _build_page_report,
    _scan_page,
)


def _page_context() -> str:
    return """
{"role": "system", "content": "规则"}
{"role": "user", "content": "继续落地工具"}
{"role": "assistant", "content": "已提交 watchdog"}
{"role": "tool", "content": "ruff check passed"}
已完成任务
已完成任务
""".strip()


def test_scan_page_reports_tokens_and_roles() -> None:
    scan = _scan_page(_page_context())

    assert "当前对话估算 Token 数" in scan
    assert "user=1" in scan
    assert "assistant=1" in scan
    assert "tool=1" in scan


def test_build_page_inventory_script_is_runnable(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(_page_context(), encoding="utf-8")
    script = tmp_path / "page_inventory.py"
    script.write_text(_build_page_inventory_script(200), encoding="utf-8")

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
    assert payload["roles"]["user"] == 1
    assert payload["page_contract"]["page_out_candidates"]


def test_build_page_report_contains_page_contract() -> None:
    report = _build_page_report(_scan_page(_page_context()), 200, _page_context())

    assert "## Page 确定性内存分页报告" in report
    assert "## Page Inventory Script" in report
    assert "## page_out()" in report
    assert "page_contract" in report


class TestMemoryPageTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(self) -> None:
        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await MemoryPageTool().execute(
                context_window=200,
                session_context=_page_context(),
            )

        assert "## Page 确定性内存分页报告" in output
        assert "Page Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(self) -> None:
        mock_response = ModelResponse(
            content="增强：优先换出已提交任务细节。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )
        router = Mock()
        router.resolve_model.return_value = "test-model"
        router.get_context_window.return_value = 256
        router.call = AsyncMock(return_value=mock_response)

        with patch("naumi_agent.tools.analysis._global_router", router):
            output = await MemoryPageTool().execute(
                context_window=512,
                session_context=_page_context(),
            )

        assert "## Page 确定性内存分页报告" in output
        assert "## LLM Page 增强" in output
        assert "已提交任务" in output
