"""World model analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    WorldModelTool,
    _build_world_inventory_script,
    _build_world_report,
    _scan_spar,
    _scan_world,
)
from naumi_agent.tools.analysis_tools.world import WorldModelTool as SplitWorldModelTool


def _write_stateful_source(path: Path) -> None:
    path.write_text(
        """
class Cart:
    def __init__(self):
        self.items = []
        self.orphan = 1

    def add(self, item):
        if item:
            self.items.append(item)
            self.status = "dirty"
        return len(self.items)

    def save(self, db):
        db.commit()
        self.status = "saved"
""".strip(),
        encoding="utf-8",
    )


def test_scan_world_detects_state_transitions_and_lost_state(tmp_path: Path) -> None:
    source = tmp_path / "cart.py"
    _write_stateful_source(source)

    scan = _scan_world(str(source))

    assert "状态清单" in scan
    assert "状态转移函数" in scan
    assert "add" in scan
    assert "客体永久性" in scan
    assert "orphan" in scan


def test_ast_safe_source_reader_also_fixes_spar_empty_function_detection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty.py"
    source.write_text("def empty():\n    pass\n", encoding="utf-8")

    scan = _scan_spar(str(source))

    assert "发现 **1** 个空函数体" in scan


def test_build_world_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "cart.py"
    _write_stateful_source(source)
    script = tmp_path / "world_inventory.py"
    script.write_text(_build_world_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["state_writes"] >= 1
    assert payload["summary"]["transitions"] >= 1
    assert payload["files"][0]["lost_state"]


def test_build_world_report_contains_inventory_and_plan(tmp_path: Path) -> None:
    source = tmp_path / "cart.py"
    _write_stateful_source(source)
    report = _build_world_report(str(source), _scan_world(str(source)))

    assert "## World 确定性世界模型审计" in report
    assert "## World Inventory Script" in report
    assert "## 反事实补强计划" in report
    assert "lost_state" in report


class TestWorldModelTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "cart.py"
        _write_stateful_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await WorldModelTool().execute(target=str(source))

        assert "## World 确定性世界模型审计" in output
        assert "World Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "cart.py"
        _write_stateful_source(source)
        mock_response = ModelResponse(
            content="增强：为 Cart.add 增加反事实失败路径。",
            usage=TokenUsage(input_tokens=18, output_tokens=6, total_tokens=24),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await WorldModelTool().execute(target=str(source))

        assert "## World 确定性世界模型审计" in output
        assert "## LLM World 增强" in output
        assert "Cart.add" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "cart.py"
        _write_stateful_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：为 Cart.add 增加反事实失败路径。"

        router = object()
        output = await SplitWorldModelTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(target=str(source))

        assert "## World 确定性世界模型审计" in output
        assert "## LLM World 增强" in output
        assert "反事实失败路径" in output
        assert calls
        assert calls[0][0] is router
        assert "世界模型架构师" in calls[0][1]
        assert str(source) in calls[0][2]
