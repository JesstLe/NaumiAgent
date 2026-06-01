"""Semantic pointer analysis tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import PointerTool, _build_pointer_report, _scan_pointer


def _write_pointer_source(path: Path) -> None:
    path.write_text(
        """
from decimal import Decimal
import requests


def explain_price(symbol: str) -> str:
    price = Decimal("12.34")
    response = requests.get("https://example.com/price")
    return f"price={price} token=abc123 data={response.text}"
""",
        encoding="utf-8",
    )


def test_scan_pointer_detects_precision_and_boundary(tmp_path: Path) -> None:
    source = tmp_path / "finance.py"
    _write_pointer_source(source)
    text = source.read_text(encoding="utf-8")

    scan = _scan_pointer([source], text, str(source))

    assert "精密数据类型" in scan
    assert "可指针化的数据源" in scan
    assert "幻觉风险评分" in scan


def test_build_pointer_report_includes_pointer_table(tmp_path: Path) -> None:
    source = tmp_path / "finance.py"
    _write_pointer_source(source)
    scan = _scan_pointer([source], source.read_text(encoding="utf-8"), str(source))

    report = _build_pointer_report(scan, [source], context="金融报价")

    assert "## SPA 确定性指针架构" in report
    assert "Reasoning Space" in report
    assert "Physical Space" in report
    assert "finance.price_ref" in report
    assert "api.response_ref" in report


class TestPointerTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_spa_report(self, tmp_path: Path) -> None:
        source = tmp_path / "finance.py"
        _write_pointer_source(source)

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await PointerTool().execute(target=str(source), context="金融报价")

        assert "## SPA 确定性指针架构" in output
        assert "Pointer Table" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "finance.py"
        _write_pointer_source(source)
        mock_response = ModelResponse(
            content="增强：所有金额必须通过 Decimal dereference 模块返回。",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await PointerTool().execute(target=str(source))

        assert "## SPA 确定性指针架构" in output
        assert "## LLM SPA 架构增强" in output
        assert "Decimal dereference" in output
