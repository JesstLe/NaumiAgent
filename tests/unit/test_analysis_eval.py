"""Eval-driven tool tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import EvalDrivenTool, _build_eval_baseline


def _write_eval_target(path: Path) -> None:
    path.write_text(
        """
def add(left: int, right: int = 0) -> int:
    return left + right


class Calculator:
    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def multiply(self, value: int) -> int:
        return self.seed * value
""",
        encoding="utf-8",
    )


def test_build_eval_baseline_generates_runnable_pytest(tmp_path: Path) -> None:
    target = tmp_path / "sample_module.py"
    _write_eval_target(target)
    baseline = _build_eval_baseline([target])
    test_file = tmp_path / "test_generated_baseline.py"
    test_file.write_text(baseline.test_code, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert baseline.function_count == 3
    assert baseline.class_count == 1
    assert result.returncode == 0, result.stdout + result.stderr


def test_build_eval_baseline_imports_module_without_public_targets(tmp_path: Path) -> None:
    target = tmp_path / "private_module.py"
    target.write_text("_VALUE = 1\n", encoding="utf-8")

    baseline = _build_eval_baseline([target])

    assert f"TARGET_FILES = ['{target.resolve()}']" in baseline.test_code
    assert baseline.function_count == 0
    assert baseline.class_count == 0


class TestEvalDrivenTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_baseline_pytest(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "sample_module.py"
        _write_eval_target(target)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await EvalDrivenTool().execute(target=str(target))

        assert "## Eval 静态扫描" in output
        assert "## EDD Baseline Pytest" in output
        assert "FUNCTION_TARGETS" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_baseline_and_adds_enhancement(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "sample_module.py"
        _write_eval_target(target)
        mock_response = ModelResponse(
            content="增强测试：补充 None 和负数输入。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await EvalDrivenTool().execute(target=str(target))

        assert "## EDD Baseline Pytest" in output
        assert "## LLM 边界测试增强" in output
        assert "补充 None" in output
