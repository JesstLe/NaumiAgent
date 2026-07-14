"""Memory paging analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.page import build_page_report, scan_page
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

PAGE_SYSTEM = """\
You are an LLM OS memory manager implementing virtual memory paging.

## Current Context Analysis
The user has activated the memory paging protocol. Analyze the current \
conversation context and perform the following:

## Your Tasks

### 1. Register Snapshot (200 words max)
Summarize the CORE state of the current conversation:
- What is the main task/topic?
- What decisions have been made?
- What is the current progress?
- What are the pending items?

### 2. page_out() — Identify Evictable Content
List what can be safely removed from context to free up space:
- Already-completed subtasks
- Detailed exploration that led to a conclusion
- Repetitive or redundant exchanges
- Code that has already been applied

### 3. page_in() — Recommendations for Loading
Suggest what should be loaded next:
- Reference documentation needed
- Files that haven't been read yet
- Context from previous sessions that might be relevant

### 4. Memory Pressure Assessment
- Rate current memory pressure: LOW / MEDIUM / HIGH / CRITICAL
- Estimate how many more turns before context becomes a problem
- Recommend whether to compact, summarize, or start a fresh session

Be precise and actionable. The user needs to know EXACTLY what to keep \
and what to discard.
"""


class MemoryPageTool(Tool):
    """LLM OS 内存分页工具."""

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
        return "analysis_page"

    @property
    def description(self) -> str:
        return (
            "LLM OS 内存分页调度：分析当前对话的上下文使用情况，"
            "生成寄存器快照(核心状态摘要)、page_out(可换出内容)、"
            "page_in(需要换入的内容)，评估内存压力等级。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "context_window": {
                    "type": "integer",
                    "description": "模型上下文窗口大小（Token），默认 128000",
                    "default": 128000,
                },
                "session_context": {
                    "type": "string",
                    "description": "可选：当前会话 transcript 或摘要，用于真实计算分页压力",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self, *, context_window: int = 128000, session_context: str = "", **kwargs: Any
    ) -> str:
        window = context_window
        scan_evidence = scan_page(session_context)
        deterministic = build_page_report(scan_evidence, window, session_context)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Page 内存分页报告。"

        model = router.resolve_model("capable")
        real_window = router.get_context_window(model)
        window = min(context_window, real_window)
        deterministic = build_page_report(scan_evidence, window, session_context)

        user_msg = (
            f"## 系统信息\n"
            f"- 模型: {model}\n"
            f"- 上下文窗口: {window:,} tokens\n"
            f"\n## 确定性分页报告\n{deterministic}\n"
            f"- 请分析当前对话的内存使用情况并给出分页建议。\n"
        )

        enhanced = await self._run_analysis(router, PAGE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Page 增强\n" + enhanced
