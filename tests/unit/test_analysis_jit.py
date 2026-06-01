"""JIT analysis tool tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import JITTool
from naumi_agent.tools.analysis import _build_jit_baseline as analysis_build_jit_baseline
from naumi_agent.tools.analysis_support.jit import build_jit_baseline


def test_build_jit_baseline_verifies_arithmetic_with_runnable_script(tmp_path: Path) -> None:
    baseline = build_jit_baseline("计算 2 + 3 * 4")
    script = tmp_path / "jit_script.py"
    script.write_text(baseline.script, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert baseline.verified
    assert "RESULT=14" in baseline.execution_output
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=verified" in result.stdout
    assert analysis_build_jit_baseline("计算 2 + 3 * 4") == baseline


def test_build_jit_baseline_generates_runnable_triage_scaffold(tmp_path: Path) -> None:
    baseline = build_jit_baseline("解析一段 CSV 并按城市聚合")
    script = tmp_path / "jit_triage.py"
    script.write_text(baseline.script, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert not baseline.verified
    assert "needs_manual_verification" in baseline.execution_output
    assert result.returncode == 0, result.stdout + result.stderr
    assert "classification=data" in result.stdout


def test_build_jit_baseline_downgrades_invalid_arithmetic(tmp_path: Path) -> None:
    baseline = build_jit_baseline("计算 1 / 0")
    script = tmp_path / "jit_invalid.py"
    script.write_text(baseline.script, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert not baseline.verified
    assert "needs_manual_verification" in baseline.execution_output
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=needs_manual_contract" in result.stdout


class TestJITTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_script_and_execution_result(self) -> None:
        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await JITTool().execute(task="计算 2 + 3 * 4")

        assert "## JIT 确定性脚本" in output
        assert "RESULT=14" in output
        assert "status=verified" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_script_and_adds_enhancement(self) -> None:
        mock_response = ModelResponse(
            content="增强：补充边界测试 0 和负数。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await JITTool().execute(task="计算 8 / 2")

        assert "## JIT 确定性脚本" in output
        assert "RESULT=4.0" in output
        assert "## LLM JIT 增强" in output
        assert "边界测试" in output
