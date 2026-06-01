"""Autopsy trace analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    AutopsyTool,
    _build_autopsy_inventory_script,
    _build_autopsy_report,
    _scan_autopsy,
)


def _write_autopsy_source(path: Path) -> None:
    path.write_text(
        """
import sys

hypothesis = "upstream data is dirty"
caller = "api"
impact_radius = 2

def target(value):
    assert hypothesis
    return normalize(value)

def normalize(value):
    sys.settrace(lambda *args: None)
    return value.strip()

def test_target():
    assert target(" x ") == "x"
""".strip(),
        encoding="utf-8",
    )


def test_scan_autopsy_reads_path_and_detects_dts_signals(tmp_path: Path) -> None:
    source = tmp_path / "autopsy_case.py"
    _write_autopsy_source(source)

    scan = _scan_autopsy(str(source))

    assert "盲目读取风险" in scan
    assert "执行迹基础设施" in scan
    assert "假设验证能力" in scan
    assert "爆炸半径隔离" in scan


def test_build_autopsy_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "autopsy_case.py"
    _write_autopsy_source(source)
    script = tmp_path / "autopsy_inventory.py"
    script.write_text(_build_autopsy_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["trace"] >= 1
    assert payload["summary"]["hypothesis"] >= 1
    assert payload["summary"]["blast"] >= 1
    assert payload["summary"]["call_edges"] >= 1
    assert payload["files"][0]["autopsy_contract"]["minimum_dts_che_chain"]


def test_build_autopsy_report_contains_contract(tmp_path: Path) -> None:
    source = tmp_path / "autopsy_case.py"
    _write_autopsy_source(source)
    report = _build_autopsy_report(str(source), _scan_autopsy(str(source)))

    assert "## Autopsy 确定性执行迹切片审计" in report
    assert "## Autopsy Inventory Script" in report
    assert "## Autopsy Contract" in report
    assert "autopsy_contract" in report


class TestAutopsyTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "autopsy_case.py"
        _write_autopsy_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await AutopsyTool().execute(target=str(source))

        assert "## Autopsy 确定性执行迹切片审计" in output
        assert "Autopsy Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "autopsy_case.py"
        _write_autopsy_source(source)
        mock_response = ModelResponse(
            content="增强：用 probe 证伪 upstream data 假设。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await AutopsyTool().execute(target=str(source))

        assert "## Autopsy 确定性执行迹切片审计" in output
        assert "## LLM Autopsy 增强" in output
        assert "probe" in output
