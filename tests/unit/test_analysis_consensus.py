"""Consensus analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    ConsensusTool,
    _build_consensus_inventory_script,
    _build_consensus_report,
    _scan_consensus,
)
from naumi_agent.tools.analysis_tools.consensus import (
    ConsensusTool as SplitConsensusTool,
)


def _write_consensus_source(path: Path) -> None:
    path.write_text(
        """
async def decide_and_delete(model, repo):
    decision = await model.call("should delete?")
    if decision:
        repo.delete("prod")
    return decision

def deploy_release(service):
    service.deploy("prod")
""".strip(),
        encoding="utf-8",
    )


def test_scan_consensus_reads_path_and_detects_risks(tmp_path: Path) -> None:
    source = tmp_path / "decision.py"
    _write_consensus_source(source)

    scan = _scan_consensus(str(source))

    assert "高风险决策点" in scan
    assert "数据删除操作" in scan
    assert "生产环境发布" in scan
    assert "单模型直接决策" in scan


def test_build_consensus_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "decision.py"
    _write_consensus_source(source)
    script = tmp_path / "consensus_inventory.py"
    script.write_text(_build_consensus_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["high_risk"] >= 1
    assert payload["summary"]["single_points"] >= 1
    assert payload["files"][0]["quorum_contracts"][0]["required_voters"] == 3


def test_build_consensus_report_contains_quorum_contract(tmp_path: Path) -> None:
    source = tmp_path / "decision.py"
    _write_consensus_source(source)
    report = _build_consensus_report(str(source), _scan_consensus(str(source)))

    assert "## Consensus 确定性共识审计" in report
    assert "## Consensus Inventory Script" in report
    assert "## Quorum 契约" in report
    assert "quorum_contracts" in report


class TestConsensusTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "decision.py"
        _write_consensus_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await ConsensusTool().execute(target=str(source))

        assert "## Consensus 确定性共识审计" in output
        assert "Consensus Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "decision.py"
        _write_consensus_source(source)
        mock_response = ModelResponse(
            content="增强：删除前需要 2/3 quorum 和 dry-run。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await ConsensusTool().execute(target=str(source))

        assert "## Consensus 确定性共识审计" in output
        assert "## LLM Consensus 增强" in output
        assert "2/3 quorum" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "decision.py"
        _write_consensus_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：删除前需要 2/3 quorum 和 dry-run。"

        router = object()
        output = await SplitConsensusTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
        ).execute(target=str(source))

        assert "## Consensus 确定性共识审计" in output
        assert "## LLM Consensus 增强" in output
        assert "2/3 quorum" in output
        assert calls
        assert calls[0][0] is router
        assert "拜占庭容错架构师" in calls[0][1]
        assert str(source) in calls[0][2]
