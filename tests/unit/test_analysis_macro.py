"""Macro market analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    MacroTool,
    _build_macro_inventory_script,
    _build_macro_report,
    _scan_macro,
)
from naumi_agent.tools.analysis_tools.macro import MacroTool as SplitMacroTool


def _write_macro_source(path: Path) -> None:
    path.write_text(
        """
score = 0
token = 100
budget = 10

def coordinator_decide(task):
    data = fetch(task)
    cleaned = transform(data)
    if score < 0:
        kill(task)
    return cleaned
""".strip(),
        encoding="utf-8",
    )


def test_scan_macro_reads_path_and_detects_market_signals(tmp_path: Path) -> None:
    source = tmp_path / "macro_case.py"
    _write_macro_source(source)

    scan = _scan_macro(str(source))

    assert "中心化检测" in scan
    assert "数据市场潜力" in scan
    assert "激励机制" in scan
    assert "竞争与淘汰" in scan


def test_build_macro_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "macro_case.py"
    _write_macro_source(source)
    script = tmp_path / "macro_inventory.py"
    script.write_text(_build_macro_inventory_script(str(source)), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(source)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["summary"]["centralized"] >= 1
    assert payload["summary"]["data_market"] >= 1
    assert payload["summary"]["incentive"] >= 1
    assert payload["files"][0]["market_contract"]["minimum_market_roles"]


def test_build_macro_report_contains_market_contract(tmp_path: Path) -> None:
    source = tmp_path / "macro_case.py"
    _write_macro_source(source)
    report = _build_macro_report(str(source), _scan_macro(str(source)))

    assert "## Macro 确定性多智能体市场审计" in report
    assert "## Market Inventory Script" in report
    assert "## Market Contract" in report
    assert "market_contract" in report


class TestMacroTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "macro_case.py"
        _write_macro_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await MacroTool().execute(task=str(source))

        assert "## Macro 确定性多智能体市场审计" in output
        assert "Market Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "macro_case.py"
        _write_macro_source(source)
        mock_response = ModelResponse(
            content="增强：把 coordinator 拆成数据商和仲裁器。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await MacroTool().execute(task=str(source))

        assert "## Macro 确定性多智能体市场审计" in output
        assert "## LLM Macro 增强" in output
        assert "仲裁器" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "macro_case.py"
        _write_macro_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：把 coordinator 拆成数据商和仲裁器。"

        router = object()
        output = await SplitMacroTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(task=str(source))

        assert "## Macro 确定性多智能体市场审计" in output
        assert "## LLM Macro 增强" in output
        assert "仲裁器" in output
        assert calls
        assert calls[0][0] is router
        assert "多智能体经济系统架构师" in calls[0][1]
        assert str(source) in calls[0][2]
