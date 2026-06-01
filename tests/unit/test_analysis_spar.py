"""SPAR analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    SparTool,
    _build_spar_harness_script,
    _build_spar_report,
    _scan_spar,
)
from naumi_agent.tools.analysis_tools.spar import SparTool as SplitSparTool


def _write_risky_source(path: Path) -> None:
    path.write_text(
        """
import os

def run(cmd):
    try:
        return os.system(cmd)  # noqa
    except Exception:
        return True

def empty():
    pass
""".strip(),
        encoding="utf-8",
    )


def test_scan_spar_detects_attack_surface_and_reward_hacking(tmp_path: Path) -> None:
    source = tmp_path / "risky.py"
    _write_risky_source(source)

    scan = _scan_spar(str(source))

    assert "攻击面扫描" in scan
    assert "命令执行" in scan
    assert "奖励作弊" in scan
    assert "空函数体" in scan


def test_scan_spar_does_not_flag_free_inside_words(tmp_path: Path) -> None:
    source = tmp_path / "safe_text.py"
    source.write_text(
        '"""dependency-free helper."""\n\n'
        "def ok(value):\n"
        "    return str(value)\n",
        encoding="utf-8",
    )

    scan = _scan_spar(str(source))

    assert "堆内存释放" not in scan


def test_build_spar_harness_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "risky.py"
    _write_risky_source(source)
    script = tmp_path / "spar_harness.py"
    script.write_text(_build_spar_harness_script("审查 risky.py"), encoding="utf-8")

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
    assert payload["summary"]["vulnerability_hits"] >= 1
    assert payload["summary"]["reward_hack_hits"] >= 1
    assert payload["files"][0]["empty_functions"] == 1
    assert payload["files"][0]["red_team_tests"]


def test_build_spar_report_contains_harness_and_gates(tmp_path: Path) -> None:
    source = tmp_path / "risky.py"
    _write_risky_source(source)
    report = _build_spar_report(str(source), _scan_spar(str(source)))

    assert "## SPAR 确定性对抗自博弈基线" in report
    assert "## Static Adversarial Harness" in report
    assert "## 收敛门槛" in report
    assert "reward_hack_hits" in report


class TestSparTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "risky.py"
        _write_risky_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await SparTool().execute(task=str(source))

        assert "## SPAR 确定性对抗自博弈基线" in output
        assert "Static Adversarial Harness" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "risky.py"
        _write_risky_source(source)
        mock_response = ModelResponse(
            content="增强：把命令执行入口纳入红队边界测试。",
            usage=TokenUsage(input_tokens=20, output_tokens=8, total_tokens=28),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await SparTool().execute(task=str(source))

        assert "## SPAR 确定性对抗自博弈基线" in output
        assert "## LLM SPAR 增强" in output
        assert "红队边界测试" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "risky.py"
        _write_risky_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：把命令执行入口纳入红队边界测试。"

        router = object()
        output = await SplitSparTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(task=str(source))

        assert "## SPAR 确定性对抗自博弈基线" in output
        assert "## LLM SPAR 增强" in output
        assert "红队边界测试" in output
        assert calls
        assert calls[0][0] is router
        assert "对抗性自博弈架构师" in calls[0][1]
        assert str(source) in calls[0][2]
