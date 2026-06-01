"""Speculative decoding analysis tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    SpeculateTool,
    _build_speculate_report,
    _scan_speculate,
)


def _write_speculate_source(path: Path) -> None:
    path.write_text(
        """
import subprocess


class UserModel:
    name: str
    email: str
    role: str
    tenant: str
    active: bool


def dangerous(command: str):
    return subprocess.run(command, shell=True)
""",
        encoding="utf-8",
    )


def test_scan_speculate_detects_risk_and_complexity(tmp_path: Path) -> None:
    source = tmp_path / "danger.py"
    _write_speculate_source(source)
    text = source.read_text(encoding="utf-8")

    scan = _scan_speculate([source], text, str(source))

    assert "高风险区域" in scan
    assert "子进程执行" in scan
    assert "文件复杂度分布" in scan


def test_build_speculate_report_marks_architect_review(tmp_path: Path) -> None:
    source = tmp_path / "danger.py"
    _write_speculate_source(source)
    scan = _scan_speculate([source], source.read_text(encoding="utf-8"), str(source))

    report = _build_speculate_report(scan, [source], task="增加命令执行功能")

    assert "Phase 1: Intern Draft" in report
    assert "Phase 2: Architect Review" in report
    assert "danger.py" in report
    assert "Diff Summary Contract" in report


class TestSpeculateTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_two_phase_plan(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "danger.py"
        _write_speculate_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await SpeculateTool().execute(target=str(source), task="审查命令执行")

        assert "## Speculate 确定性双阶段计划" in output
        assert "Architect Review" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_plan_and_adds_enhancement(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "danger.py"
        _write_speculate_source(source)
        mock_response = ModelResponse(
            content="增强：shell=True 必须替换为参数数组。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await SpeculateTool().execute(target=str(source), task="审查命令执行")

        assert "## Speculate 确定性双阶段计划" in output
        assert "## LLM 推测解码增强" in output
        assert "shell=True" in output
