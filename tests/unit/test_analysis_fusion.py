"""Fusion analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    FusionTool,
    _build_fusion_inventory_script,
    _build_fusion_report,
    _scan_fusion,
)


def _write_fusion_source(path: Path) -> None:
    path.write_text(
        """
import json
import os

async def run(router):
    response = await router.call(messages=[], temperature=0.7)
    content = response.content
    data = json.loads(content)
    amount = int(content)
    os.system(content)
    return data, amount
""".strip(),
        encoding="utf-8",
    )


def test_scan_fusion_reads_target_path_and_detects_danger(tmp_path: Path) -> None:
    source = tmp_path / "fusion_case.py"
    _write_fusion_source(source)

    scan = _scan_fusion(str(source))

    assert "概率区" in scan
    assert "模型路由调用" in scan
    assert "危险融合" in scan
    assert "AI 输出直接反序列化" in scan


def test_build_fusion_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "fusion_case.py"
    _write_fusion_source(source)
    script = tmp_path / "fusion_inventory.py"
    script.write_text(_build_fusion_inventory_script(str(source)), encoding="utf-8")

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
    assert payload["summary"]["ai_hits"] >= 1
    assert payload["summary"]["danger_hits"] >= 1
    assert payload["files"][0]["validation_contracts"]


def test_build_fusion_report_contains_contract_and_script(tmp_path: Path) -> None:
    source = tmp_path / "fusion_case.py"
    _write_fusion_source(source)
    report = _build_fusion_report(str(source), _scan_fusion(str(source)))

    assert "## Fusion 确定性边界审计" in report
    assert "## Fusion Inventory Script" in report
    assert "## 验证层契约" in report
    assert "danger_hits" in report


class TestFusionTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "fusion_case.py"
        _write_fusion_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await FusionTool().execute(target=str(source))

        assert "## Fusion 确定性边界审计" in output
        assert "Fusion Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "fusion_case.py"
        _write_fusion_source(source)
        mock_response = ModelResponse(
            content="增强：为 content 增加 schema 校验。",
            usage=TokenUsage(input_tokens=18, output_tokens=6, total_tokens=24),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await FusionTool().execute(target=str(source))

        assert "## Fusion 确定性边界审计" in output
        assert "## LLM Fusion 增强" in output
        assert "schema 校验" in output

