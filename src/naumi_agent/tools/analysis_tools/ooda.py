"""OODA resilience analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.ooda import build_ooda_report, scan_ooda
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]

OODA_SYSTEM = """\
You are a Mission Command architect implementing the OODA \
(Observe-Orient-Decide-Act) loop for resilient AI agent design.

## Output Format
1. Commander's Intent (one sentence goal)
2. OODA Loop Design (each stage: implementation, failure modes, \
recovery)
3. Self-Healing Mechanisms (failure detection, auto-retry, fallback, \
self-repair)
4. Anti-Fragility Checklist (no hardcoded URLs/selectors, no fixed \
waits, no single-path, no silent failures)
5. Resilience Score (1-10: adaptability, self-correction, isolation, \
degradation, recovery)
"""


class OODATool(Tool):
    """OODA resilient mission-command analysis tool."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
        resolve_target: ResolveTarget | None = None,
        read_sources: ReadSources | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis
        self._resolve_target = resolve_target or (lambda _target: [])
        self._read_sources = read_sources or (lambda _files: "")

    @property
    def name(self) -> str:
        return "analysis_ooda"

    @property
    def description(self) -> str:
        return (
            "战场任务式指挥(OODA)：分析代码脆弱性，"
            "设计意图驱动的 OODA 循环架构，"
            "包含环境感知、异常自纠错和自我修复。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件或目录路径",
                },
                "task": {
                    "type": "string",
                    "description": "任务目标描述",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self,
        *,
        target: str,
        task: str = "",
        **kwargs: Any,
    ) -> str:
        files = self._resolve_target(target)
        if not files:
            return f"无法解析目标: {target}"
        source_text = self._read_sources(files)
        scan_evidence = scan_ooda(files, source_text, task)
        deterministic = build_ooda_report(scan_evidence, files, task)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 OODA 指挥方案。"

        user_msg = (
            f"## 脆弱性扫描\n{scan_evidence}\n\n"
            f"## 确定性 OODA 方案\n{deterministic}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if task:
            user_msg += f"\n## 任务目标\n{task}\n"
        enhanced = await self._run_analysis(router, OODA_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM OODA 增强\n" + enhanced
