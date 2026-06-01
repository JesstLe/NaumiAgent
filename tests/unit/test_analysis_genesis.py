"""Genesis analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    GenesisTool,
    _build_genesis_inventory_script,
    _build_genesis_report,
    _scan_genesis,
)
from naumi_agent.tools.analysis_tools.genesis import GenesisTool as SplitGenesisTool


def _write_genesis_source(path: Path) -> None:
    path.write_text(
        """
import importlib
import inspect

MAX_RETRIES = 3
registry = {}

def load_plugin(name):
    module = importlib.import_module(name)
    registry[name] = module
    return getattr(module, "Plugin")

def describe(obj):
    return inspect.getsource(obj)
""".strip(),
        encoding="utf-8",
    )


def test_scan_genesis_reads_path_and_detects_evolution_signals(tmp_path: Path) -> None:
    source = tmp_path / "genesis_case.py"
    _write_genesis_source(source)

    scan = _scan_genesis(str(source))

    assert "刚性检测" in scan
    assert "编译时固定参数" in scan
    assert "动态导入机制" in scan
    assert "代码内省模块" in scan


def test_build_genesis_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "genesis_case.py"
    _write_genesis_source(source)
    script = tmp_path / "genesis_inventory.py"
    script.write_text(_build_genesis_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["rigidity"] >= 1
    assert payload["summary"]["evolution"] >= 1
    assert payload["files"][0]["evolution_contract"]["externalize_config_required"] is True


def test_build_genesis_report_contains_contract_and_script(tmp_path: Path) -> None:
    source = tmp_path / "genesis_case.py"
    _write_genesis_source(source)
    report = _build_genesis_report(str(source), _scan_genesis(str(source)))

    assert "## Genesis 确定性自演化审计" in report
    assert "## Genesis Inventory Script" in report
    assert "## Evolution Contract" in report
    assert "evolution_contract" in report


class TestGenesisTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "genesis_case.py"
        _write_genesis_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await GenesisTool().execute(target=str(source))

        assert "## Genesis 确定性自演化审计" in output
        assert "Genesis Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "genesis_case.py"
        _write_genesis_source(source)
        mock_response = ModelResponse(
            content="增强：为 registry 增加沙盒验证和回滚点。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await GenesisTool().execute(target=str(source))

        assert "## Genesis 确定性自演化审计" in output
        assert "## LLM Genesis 增强" in output
        assert "回滚点" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "genesis_case.py"
        _write_genesis_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：为 registry 增加沙盒验证和回滚点。"

        router = object()
        output = await SplitGenesisTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(target=str(source))

        assert "## Genesis 确定性自演化审计" in output
        assert "## LLM Genesis 增强" in output
        assert "回滚点" in output
        assert calls
        assert calls[0][0] is router
        assert "系统自演化架构师" in calls[0][1]
        assert str(source) in calls[0][2]
