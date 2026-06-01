"""Self-heal tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import SelfHealTool, _build_heal_report

TRACEBACK = """Traceback (most recent call last):
  File "/tmp/app.py", line 10, in handle
    return user["name"]
KeyError: 'name'
"""


def _write_heal_target(path: Path) -> None:
    path.write_text(
        """
def handle(user):
    return user["name"]
""",
        encoding="utf-8",
    )


def test_build_heal_report_extracts_root_frame_and_guidance() -> None:
    report = _build_heal_report(TRACEBACK)

    assert "错误类型：KeyError" in report
    assert "疑似根因位置：/tmp/app.py:10 in handle()" in report
    assert "显式存在性校验" in report
    assert "回归验证建议" in report


class TestSelfHealTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_diagnosis(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "app.py"
        _write_heal_target(target)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await SelfHealTool().execute(error_log=TRACEBACK, target=str(target))

        assert "## Heal 确定性诊断" in output
        assert "错误类型：KeyError" in output
        assert "Heal 静态扫描" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_diagnosis_and_adds_hotfix(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "app.py"
        _write_heal_target(target)
        mock_response = ModelResponse(
            content="热修复：使用 user.get('name') 并返回中文错误。",
            usage=TokenUsage(input_tokens=10, output_tokens=6, total_tokens=16),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await SelfHealTool().execute(error_log=TRACEBACK, target=str(target))

        assert "## Heal 确定性诊断" in output
        assert "## LLM 热修复增强" in output
        assert "user.get" in output
