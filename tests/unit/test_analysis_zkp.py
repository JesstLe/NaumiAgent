"""ZKP analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    ZKPTool,
    _build_zkp_report,
    _build_zkp_trace_script,
    _scan_zkp,
)


def _write_zkp_source(path: Path) -> None:
    path.write_text(
        """
async def summarize(router):
    result = await router.call("summarize")
    summary = result.content
    return summary

def traced():
    source = "doc.md"
    line_no = 42
    confidence = 0.9
    return source, line_no, confidence
""".strip(),
        encoding="utf-8",
    )


def test_scan_zkp_reads_path_and_detects_unverified_output(tmp_path: Path) -> None:
    source = tmp_path / "zkp_case.py"
    _write_zkp_source(source)

    scan = _scan_zkp(str(source))

    assert "未验证输出检测" in scan
    assert "AI 输出赋值无验证层" in scan
    assert "引用基础设施" in scan
    assert "置信度评分" in scan


def test_build_zkp_trace_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "zkp_case.py"
    _write_zkp_source(source)
    script = tmp_path / "zkp_trace.py"
    script.write_text(_build_zkp_trace_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["unverified_outputs"] >= 1
    assert payload["summary"]["citations"] >= 1
    assert payload["files"][0]["trace_contract"]["requires_trace_tree"] is True


def test_build_zkp_report_contains_trace_contract(tmp_path: Path) -> None:
    source = tmp_path / "zkp_case.py"
    _write_zkp_source(source)
    report = _build_zkp_report(str(source), _scan_zkp(str(source)))

    assert "## ZKP 确定性轨迹校验方案" in report
    assert "## Trace Verifier Script" in report
    assert "## Trace Contract" in report
    assert "unverified_outputs" in report


class TestZKPTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "zkp_case.py"
        _write_zkp_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await ZKPTool().execute(target=str(source))

        assert "## ZKP 确定性轨迹校验方案" in output
        assert "Trace Verifier Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "zkp_case.py"
        _write_zkp_source(source)
        mock_response = ModelResponse(
            content="增强：每个 summary claim 都要绑定 source_path。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await ZKPTool().execute(target=str(source))

        assert "## ZKP 确定性轨迹校验方案" in output
        assert "## LLM ZKP 增强" in output
        assert "source_path" in output

