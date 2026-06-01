"""Hook analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    HookTool,
    _build_hook_inventory_script,
    _build_hook_report,
    _scan_hook,
)
from naumi_agent.tools.analysis_support.hook import classify_target_types
from naumi_agent.tools.analysis_tools.hook import HookTool as SplitHookTool


def test_scan_hook_detects_platform_and_protection() -> None:
    scan = _scan_hook("分析 Unity il2cpp 游戏，存在 anti-cheat 和 integrity check")

    assert "dotnet" in scan
    assert "保护/合规风险" in scan
    assert "默认不提供绕过或规避步骤" in scan


def test_classify_target_types_detects_wasm_and_android() -> None:
    matches = classify_target_types("检查 android apk 和 wasm 插件")
    types = {item[0] for item in matches}

    assert {"java", "wasm"} <= types


def test_build_hook_inventory_script_is_runnable_and_readonly(tmp_path: Path) -> None:
    sample = tmp_path / "module.wasm"
    sample.write_bytes(b"\x00asm" + b"\x01\x00\x00\x00")
    script = tmp_path / "hook_inventory.py"
    script.write_text(_build_hook_inventory_script("检查 wasm 模块导出表"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(sample)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["files"][0]["format"] == "wasm"
    assert payload["files"][0]["sha256_first_1m"]


def test_build_hook_report_contains_inventory_and_boundaries() -> None:
    report = _build_hook_report(
        "分析 Android APK 的只读参数观测",
        "apk",
        _scan_hook("分析 Android APK 的只读参数观测 apk"),
    )

    assert "## Hook 确定性合规侦测方案" in report
    assert "## Read-only Target Inventory Script" in report
    assert "默认不注入、不 patch、不绕过保护" in report
    assert "UNKNOWN ABI" in report


class TestHookTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(self) -> None:
        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await HookTool().execute(task="分析 wasm 模块导出表", target_type="wasm")

        assert "## Hook 确定性合规侦测方案" in output
        assert "Read-only Target Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(self) -> None:
        mock_response = ModelResponse(
            content="增强：先对 hash 固定的样本读取导出表。",
            usage=TokenUsage(input_tokens=12, output_tokens=6, total_tokens=18),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await HookTool().execute(task="分析 wasm 模块导出表", target_type="wasm")

        assert "## Hook 确定性合规侦测方案" in output
        assert "## LLM Hook 增强" in output
        assert "读取导出表" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(self) -> None:
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：只读导出表与 hash 校验。"

        router = object()
        output = await SplitHookTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(task="分析 wasm 模块导出表", target_type="wasm")

        assert "## Hook 确定性合规侦测方案" in output
        assert "## LLM Hook 增强" in output
        assert "只读导出表" in output
        assert calls
        assert calls[0][0] is router
        assert "Do not provide bypass" in calls[0][1]
        assert "分析 wasm 模块导出表" in calls[0][2]
