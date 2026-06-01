"""Watchdog analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    WatchdogTool,
    _build_watchdog_inventory_script,
    _build_watchdog_report,
    _scan_watchdog,
)


def _write_watchdog_source(path: Path) -> None:
    path.write_text(
        """
import importlib

heartbeat = "alive"
snapshot = "pre-change"
sandbox = True

def mutate():
    importlib.reload(plugin)
    backup = snapshot
    if heartbeat:
        return backup
""".strip(),
        encoding="utf-8",
    )


def test_scan_watchdog_reads_path_and_detects_recovery_signals(tmp_path: Path) -> None:
    source = tmp_path / "watchdog_case.py"
    _write_watchdog_source(source)

    scan = _scan_watchdog(str(source))

    assert "原地修改风险" in scan
    assert "运行时重载模块" in scan
    assert "心跳与健康检查" in scan
    assert "回滚基础设施" in scan


def test_build_watchdog_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "watchdog_case.py"
    _write_watchdog_source(source)
    script = tmp_path / "watchdog_inventory.py"
    script.write_text(_build_watchdog_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["inplace"] >= 1
    assert payload["summary"]["health"] >= 1
    assert payload["summary"]["rollback"] >= 1
    assert payload["files"][0]["phoenix_contract"]["minimum_recovery_chain"]


def test_build_watchdog_report_contains_phoenix_contract(tmp_path: Path) -> None:
    source = tmp_path / "watchdog_case.py"
    _write_watchdog_source(source)
    report = _build_watchdog_report(str(source), _scan_watchdog(str(source)))

    assert "## Watchdog 确定性灾难隔离审计" in report
    assert "## Watchdog Inventory Script" in report
    assert "## Phoenix Contract" in report
    assert "phoenix_contract" in report


class TestWatchdogTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "watchdog_case.py"
        _write_watchdog_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await WatchdogTool().execute(target=str(source))

        assert "## Watchdog 确定性灾难隔离审计" in output
        assert "Watchdog Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "watchdog_case.py"
        _write_watchdog_source(source)
        mock_response = ModelResponse(
            content="增强：heartbeat timeout 后自动 restore snapshot。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await WatchdogTool().execute(target=str(source))

        assert "## Watchdog 确定性灾难隔离审计" in output
        assert "## LLM Watchdog 增强" in output
        assert "restore snapshot" in output

