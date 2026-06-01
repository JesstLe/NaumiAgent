"""MCTS analysis tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import MCTSTool, _build_mcts_decision_report, _scan_mcts


def _write_branchy_source(path: Path) -> None:
    path.write_text(
        """
def decide(value):
    if value is None:
        raise ValueError("missing")
    if value > 10:
        return "large"
    elif value > 0:
        return "small"
    return "zero"
""",
        encoding="utf-8",
    )


def test_scan_mcts_handles_files_without_imports(tmp_path: Path) -> None:
    source = tmp_path / "decision.py"
    _write_branchy_source(source)

    scan = _scan_mcts([source], source.read_text(encoding="utf-8"), "修复决策逻辑")

    assert "决策分支点" in scan
    assert "异常路径" in scan
    assert "决策复杂度" in scan


def test_build_mcts_decision_report_contains_pruning_and_winner() -> None:
    report = _build_mcts_decision_report(
        "修复决策逻辑",
        "- 决策复杂度: 64 (HIGH) — 需要 MCTS 多路径探索",
    )

    assert "Path A" in report
    assert "Path B" in report
    assert "Path C" in report
    assert "Pruning Decision" in report
    assert "Winning Path: Path B" in report


class TestMCTSTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_decision_skeleton(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "decision.py"
        _write_branchy_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await MCTSTool().execute(problem="修复决策逻辑", target=str(source))

        assert "## MCTS 确定性多路径探索" in output
        assert "Pruning Decision" in output
        assert "Backtracking trigger" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_skeleton_and_adds_deepening(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "decision.py"
        _write_branchy_source(source)
        mock_response = ModelResponse(
            content="深化：先保留 Path B，并补异常路径测试。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await MCTSTool().execute(problem="修复决策逻辑", target=str(source))

        assert "## MCTS 确定性多路径探索" in output
        assert "## LLM MCTS 深化" in output
        assert "异常路径测试" in output
