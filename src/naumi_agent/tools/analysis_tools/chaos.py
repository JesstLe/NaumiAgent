"""Chaos engineering static analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools import analysis_common
from naumi_agent.tools.analysis_support.static_modes import (
    format_static_scan_result,
    scan_chaos,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

CHAOS_SYSTEM = """\
You are a ruthless chaos engineering architect reviewing REAL static analysis evidence.

Below is auto-generated scan evidence from the target codebase, followed by the \
actual source code. Your job:

1. **SPOF Analysis (Top 3)**: Based on the evidence, identify the 3 most fragile \
single points of failure. Cite specific file paths and line patterns from the evidence. \
For each: describe the blast radius and failure probability.

2. **Catastrophic Scenario Simulation**: Assume a memory leak + database deadlock + \
critical dependency outage happen SIMULTANEOUSLY. Walk through the death spiral with \
simulated timestamps.

3. **Upgrade Roadmap**: Three-tier plan from emergency patches to enterprise resilience. \
Each fix must reference a specific finding from the evidence.

Be harsh, specific, and evidence-based. No generic advice.
"""


class ChaosAnalysisTool(Tool):
    """Chaos engineering audit with deterministic static evidence."""

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
        return "analysis_chaos"

    @property
    def description(self) -> str:
        return (
            "对代码进行灾难演练分析。"
            "先静态扫描代码找脆弱点(裸except、硬编码、无重试、无超时等)，"
            "再由 LLM 综合推理出 SPOF、推演灾难场景、给出改造路线图。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件路径或目录路径",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（系统架构、技术栈等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, context: str = "", **kwargs: Any) -> str:
        files = analysis_common.resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = analysis_common.read_sources(files)
        scan_evidence = scan_chaos(files, source_text)
        deterministic = format_static_scan_result("Chaos 静态扫描", scan_evidence, files)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回静态扫描结果。"

        user_msg = f"## 静态扫描证据\n{scan_evidence}\n\n## 源代码\n{source_text[:50000]}\n"
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        enhanced = await self._run_analysis(router, CHAOS_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 灾难推演\n" + enhanced
