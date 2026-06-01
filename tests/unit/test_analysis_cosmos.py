"""Cosmos world-engine analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    CosmosTool,
    _build_cosmos_inventory_script,
    _build_cosmos_report,
    _scan_cosmos,
)


def _write_cosmos_source(path: Path) -> None:
    path.write_text(
        """
position = (0, 0)
velocity = 1
health = 100
relationship = {}
memory = []
time = 0

def generate(seed):
    return procedural(seed)

def agent_decide(goal):
    observe(goal)
    interact(goal)
    return goal

def on_click(event):
    update(event)
""".strip(),
        encoding="utf-8",
    )


def test_scan_cosmos_reads_path_and_detects_world_signals(tmp_path: Path) -> None:
    source = tmp_path / "cosmos_case.py"
    _write_cosmos_source(source)

    scan = _scan_cosmos(str(source))

    assert "状态维度丰富度" in scan
    assert "生成能力" in scan
    assert "社会模拟就绪度" in scan
    assert "观测者效应" in scan


def test_build_cosmos_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "cosmos_case.py"
    _write_cosmos_source(source)
    script = tmp_path / "cosmos_inventory.py"
    script.write_text(_build_cosmos_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["state"] >= 4
    assert payload["summary"]["generation"] >= 1
    assert payload["summary"]["social"] >= 2
    assert payload["summary"]["observer"] >= 1
    assert payload["files"][0]["genesis_contract"]["required_planes"]


def test_build_cosmos_report_contains_genesis_contract(tmp_path: Path) -> None:
    source = tmp_path / "cosmos_case.py"
    _write_cosmos_source(source)
    report = _build_cosmos_report(str(source), _scan_cosmos(str(source)))

    assert "## Cosmos 确定性创世引擎审计" in report
    assert "## Cosmos Inventory Script" in report
    assert "## Genesis Contract" in report
    assert "genesis_contract" in report


class TestCosmosTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "cosmos_case.py"
        _write_cosmos_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await CosmosTool().execute(target=str(source))

        assert "## Cosmos 确定性创世引擎审计" in output
        assert "Cosmos Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "cosmos_case.py"
        _write_cosmos_source(source)
        mock_response = ModelResponse(
            content="增强：把 WorldState schema 接入 deterministic replay。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await CosmosTool().execute(target=str(source))

        assert "## Cosmos 确定性创世引擎审计" in output
        assert "## LLM Cosmos 增强" in output
        assert "deterministic replay" in output
