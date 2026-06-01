"""Entropy valve analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.entropy import (
    build_entropy_anchor,
    scan_entropy,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

ENTROPY_SYSTEM = """\
You are a Dissipative Structure Valve implementing thermodynamic \
entropy reduction for AI reasoning chains.

## Mandatory Protocol
1. HALT current reasoning
2. Condense context into 3 sentences: core task, verified facts, \
remaining work
3. Purge all dead-ends and repetition
4. Restart from the 3-sentence anchor + original goal
5. Anti-drift: check every 3 paragraphs for relevance
"""


class EntropyValveTool(Tool):
    """耗散结构热力学重置工具."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis

    @property
    def name(self) -> str:
        return "analysis_entropy"

    @property
    def description(self) -> str:
        return (
            "耗散结构热力学重置：当推理链过长或逻辑发散时，"
            "强制执行熵减 — 用3句话总结正确状态（锚点），"
            "丢弃上下文包袱，从锚点重新启动推理。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "当前对话上下文或需要熵减的长文本",
                },
                "goal": {
                    "type": "string",
                    "description": "原始目标/任务",
                    "default": "",
                },
            },
            "required": ["context"],
        }

    async def execute(
        self,
        *,
        context: str,
        goal: str = "",
        **kwargs: Any,
    ) -> str:
        scan_evidence = scan_entropy("", context)
        deterministic = f"## 熵值扫描\n{scan_evidence}\n\n{build_entropy_anchor(context, goal)}"
        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性熵减锚点。"

        user_msg = (
            f"## 熵值扫描\n{scan_evidence}\n\n"
            f"## 确定性锚点\n{deterministic}\n\n"
            f"## 当前上下文\n{context[:60000]}\n"
        )
        if goal:
            user_msg += f"\n## 原始目标\n{goal}\n"
        enhanced = await self._run_analysis(router, ENTROPY_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 增强熵减\n" + enhanced
