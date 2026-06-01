"""Sleep pruning analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.sleep import (
    build_sleep_report,
    scan_sleep,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]

SLEEP_SYSTEM = """\
You are a Circadian Synaptic Pruning engine implementing biological \
sleep consolidation for AI systems.

## Tasks
1. Replay & Summarize (concepts, skills, decisions, corrections)
2. Synaptic Pruning (what to delete: dead-ends, understood basics, \
repetition, debugging chatter)
3. Knowledge Consolidation (what to hardcode: verified solutions, \
user preferences, project conventions, architectural decisions)
4. Evolution Patch (concise knowledge to append to system prompt)
5. Memory State After Sleep (size reduction, insights preserved, \
pruned items, readiness)
"""


class SleepPruningTool(Tool):
    """昼夜节律突触修剪工具."""

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
        return "analysis_sleep"

    @property
    def description(self) -> str:
        return (
            "昼夜节律突触修剪：对当前会话进行离线压缩，"
            "提取核心方法论和已固化概念，修剪冗余内容，"
            "生成可追加到 System Prompt 的进化补丁(Patch)。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_context": {
                    "type": "string",
                    "description": "当前会话的完整上下文",
                    "default": "",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码路径",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        session_context: str = "",
        target: str = "",
        **kwargs: Any,
    ) -> str:
        source_text = ""
        files: list[Path] = []
        if target:
            files = self._resolve_target(target)
            if files:
                source_text = self._read_sources(files)
        combined = source_text
        if session_context:
            combined = f"## 对话历史\n{session_context}\n\n## 源代码\n{source_text}"
        elif not source_text:
            combined = "（无会话上下文，将基于代码库进行分析）"
        scan_evidence = scan_sleep(files, combined, session_context)
        deterministic = build_sleep_report(scan_evidence, session_context, combined)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Sleep 突触修剪报告。"

        user_msg = (
            f"## 突触修剪扫描\n{scan_evidence}\n\n"
            f"## 确定性 Sleep 突触修剪报告\n{deterministic}\n\n"
            f"## 完整内容\n{combined[:60000]}\n"
        )
        enhanced = await self._run_analysis(router, SLEEP_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Sleep 增强\n" + enhanced
