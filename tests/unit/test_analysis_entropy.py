"""Entropy valve tool tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    EntropyValveTool,
)
from naumi_agent.tools.analysis import _build_entropy_anchor as analysis_entropy_anchor
from naumi_agent.tools.analysis import _scan_entropy as analysis_scan_entropy
from naumi_agent.tools.analysis_support.entropy import build_entropy_anchor, scan_entropy
from naumi_agent.tools.analysis_tools.entropy import (
    EntropyValveTool as SplitEntropyTool,
)


class TestEntropyAnchor:
    def test_scan_entropy_reports_critical_for_repeated_context(self) -> None:
        repeated = "目标是修复工具。目标是修复工具。目标是修复工具。"

        scan = scan_entropy("", repeated)

        assert "语义重复率" in scan
        assert "CRITICAL" in scan
        assert analysis_scan_entropy("", repeated) == scan

    def test_build_entropy_anchor_uses_goal_and_verified_facts(self) -> None:
        context = (
            "我们已经提交 tool_search，测试通过。"
            "下一步需要把 prompt 包装工具落地。"
        )

        anchor = build_entropy_anchor(context, goal="对齐 Claude Code 成熟能力")

        assert "## 熵减锚点" in anchor
        assert "核心任务：对齐 Claude Code 成熟能力" in anchor
        assert "测试通过" in anchor
        assert "下一步需要" in anchor
        assert "重启协议" in anchor
        assert analysis_entropy_anchor(context, goal="对齐 Claude Code 成熟能力") == anchor

    def test_build_entropy_anchor_falls_back_without_relevant_keywords(self) -> None:
        anchor = build_entropy_anchor("这是一段很长但是没有明确执行语义的背景材料" * 12)

        assert "当前目标需要继续推进" in anchor
        assert "当前没有可确认的验证事实" in anchor
        assert "下一步应选择最小可验证动作" in anchor


class TestEntropyValveTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_anchor(self) -> None:
        tool = EntropyValveTool()

        with patch("naumi_agent.tools.analysis._global_router", None):
            output = await tool.execute(
                context="目标是修复工具。已经完成 /vibe scaffold，测试通过。下一步继续拆分模块。",
                goal="保证工具真实可用",
            )

        assert "## 熵值扫描" in output
        assert "## 熵减锚点" in output
        assert "保证工具真实可用" in output
        assert "模型路由未初始化" in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_anchor_and_adds_enhancement(self) -> None:
        tool = EntropyValveTool()
        mock_response = ModelResponse(
            content="增强锚点：只保留已验证事实。",
            usage=TokenUsage(input_tokens=20, output_tokens=8, total_tokens=28),
            model="test",
        )

        with patch("naumi_agent.tools.analysis._global_router") as router:
            router.call = AsyncMock(return_value=mock_response)
            output = await tool.execute(
                context="目标是继续落地工具。已经通过 targeted tests。下一步拆分 analysis。",
            )

        assert "## 熵减锚点" in output
        assert "## LLM 增强熵减" in output
        assert "增强锚点" in output

    @pytest.mark.asyncio
    async def test_split_tool_accepts_injected_router_runner(self) -> None:
        async def run_analysis(router, system_prompt: str, user_msg: str) -> str:
            assert router == "router"
            assert "Mandatory Protocol" in system_prompt
            assert "## 当前上下文" in user_msg
            return "注入增强结果"

        tool = SplitEntropyTool(
            router_getter=lambda: "router",
            run_analysis=run_analysis,
        )

        output = await tool.execute(
            context="目标是拆分 analysis。已经迁移 entropy。下一步跑测试。",
            goal="降低 analysis.py 维护成本",
        )

        assert "## 熵减锚点" in output
        assert "## LLM 增强熵减" in output
        assert "注入增强结果" in output
