"""Supervisor tree analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    SupervisorTool,
    _build_supervisor_inventory_script,
    _build_supervisor_report,
    _scan_supervisor,
)
from naumi_agent.tools.analysis_tools.supervisor import (
    SupervisorTool as SplitSupervisorTool,
)


def _write_supervisor_source(path: Path) -> None:
    path.write_text(
        """
import multiprocessing

restart_policy = "permanent"
child_spec = {"name": "coder"}
max_retries = 3

def worker_agent():
    fetch()
    subprocess.run(["pytest"], timeout=10)

def supervisor_guardian():
    process = multiprocessing.Process(target=worker_agent)
    process.start()
    try:
        process.join(timeout=5)
    except Exception:
        rollback()
""".strip(),
        encoding="utf-8",
    )


def test_scan_supervisor_reads_path_and_detects_guardian_signals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "supervisor_case.py"
    _write_supervisor_source(source)

    scan = _scan_supervisor(str(source))

    assert "单体风险检测" in scan
    assert "进化节点候选" in scan
    assert "守护基础设施" in scan
    assert "错误隔离质量" in scan


def test_build_supervisor_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "supervisor_case.py"
    _write_supervisor_source(source)
    script = tmp_path / "supervisor_inventory.py"
    script.write_text(_build_supervisor_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["worker"] >= 1
    assert payload["summary"]["supervisor"] >= 1
    assert payload["summary"]["isolation"] >= 1
    assert payload["files"][0]["restart_contract"]["minimum_restart_chain"]


def test_build_supervisor_report_contains_restart_contract(tmp_path: Path) -> None:
    source = tmp_path / "supervisor_case.py"
    _write_supervisor_source(source)
    report = _build_supervisor_report(str(source), _scan_supervisor(str(source)))

    assert "## Supervisor 确定性守护者树审计" in report
    assert "## Supervisor Inventory Script" in report
    assert "## Restart Contract" in report
    assert "restart_contract" in report


class TestSupervisorTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "supervisor_case.py"
        _write_supervisor_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await SupervisorTool().execute(target=str(source))

        assert "## Supervisor 确定性守护者树审计" in output
        assert "Supervisor Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "supervisor_case.py"
        _write_supervisor_source(source)
        mock_response = ModelResponse(
            content="增强：为 worker 配置 max restart intensity。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with (
            patch("naumi_agent.tools.analysis._global_router") as router,
            patch(
                "naumi_agent.tools.analysis._get_analysis_subagent_manager",
                return_value=None,
            ),
        ):
            router.call = AsyncMock(return_value=mock_response)
            output = await SupervisorTool().execute(target=str(source))

        assert "## Supervisor 确定性守护者树审计" in output
        assert "## LLM Supervisor 增强" in output
        assert "max restart intensity" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(self, tmp_path: Path) -> None:
        source = tmp_path / "supervisor_case.py"
        _write_supervisor_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：为 worker 配置 max restart intensity。"

        router = object()
        output = await SplitSupervisorTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
            subagent_manager_getter=lambda _router: None,
        ).execute(target=str(source))

        assert "## Supervisor 确定性守护者树审计" in output
        assert "## LLM Supervisor 增强" in output
        assert "max restart intensity" in output
        assert calls
        assert calls[0][0] is router
        assert "Erlang/OTP 守护者架构师" in calls[0][1]
        assert str(source) in calls[0][2]
