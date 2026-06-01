"""PID analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    PIDTool,
    _build_pid_inventory_script,
    _build_pid_report,
    _scan_pid,
)
from naumi_agent.tools.analysis_tools.pid import PIDTool as SplitPIDTool


def _write_pid_source(path: Path) -> None:
    path.write_text(
        """
pipeline = [fetch, transform, save]

def run(items):
    total = 0
    buffer = []
    for item in items:
        total += item.value
        buffer.append(item)
    while True:
        process(buffer)
    return total
""".strip(),
        encoding="utf-8",
    )


def test_scan_pid_reads_path_and_detects_open_loop(tmp_path: Path) -> None:
    source = tmp_path / "pid_case.py"
    _write_pid_source(source)

    scan = _scan_pid(str(source))

    assert "开环检测" in scan
    assert "线性流水线定义" in scan
    assert "误差累积风险" in scan
    assert "无限循环" in scan


def test_build_pid_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "pid_case.py"
    _write_pid_source(source)
    script = tmp_path / "pid_inventory.py"
    script.write_text(_build_pid_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["open_loop"] >= 1
    assert payload["summary"]["accumulation"] >= 1
    assert payload["files"][0]["pid_contract"]["i_required"] is True


def test_build_pid_report_contains_contract_and_script(tmp_path: Path) -> None:
    source = tmp_path / "pid_case.py"
    _write_pid_source(source)
    report = _build_pid_report(str(source), _scan_pid(str(source)))

    assert "## PID 确定性闭环审计" in report
    assert "## PID Inventory Script" in report
    assert "## PID 改造契约" in report
    assert "pid_contract" in report


class TestPIDTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "pid_case.py"
        _write_pid_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await PIDTool().execute(target=str(source))

        assert "## PID 确定性闭环审计" in output
        assert "PID Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "pid_case.py"
        _write_pid_source(source)
        mock_response = ModelResponse(
            content="增强：为 while True 增加 deadline 和熔断。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await PIDTool().execute(target=str(source))

        assert "## PID 确定性闭环审计" in output
        assert "## LLM PID 增强" in output
        assert "deadline" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "pid_case.py"
        _write_pid_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：为 while True 增加 deadline 和熔断。"

        router = object()
        output = await SplitPIDTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(target=str(source))

        assert "## PID 确定性闭环审计" in output
        assert "## LLM PID 增强" in output
        assert "deadline" in output
        assert calls
        assert calls[0][0] is router
        assert "自动化控制论架构师" in calls[0][1]
        assert str(source) in calls[0][2]
