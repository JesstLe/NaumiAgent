"""Semantic pointer architecture analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.pointer import (
    build_pointer_report,
    scan_pointer,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]

POINTER_SYSTEM = """\
You are a Semantic Pointer Architecture (SPA) analyst implementing the \
C++ pointer concept in AI systems.

## Core Principle
**Separate "reasoning space" (fuzzy AI thinking) from "physical space" \
(precise data computation).** The AI should NEVER directly generate or \
manipulate precise data. Instead:

1. **Reasoning Space (AI's job)**: Strategy, logic, orchestration, \
natural language understanding, user interaction
2. **Physical Space (Hardcoded modules)**: Numerical computation, \
data retrieval, type-safe operations, precision-critical calculations
3. **Pointers (The bridge)**: API calls, DB queries, function references \
that let AI "dereference" precise data without touching it

## Your Tasks

### 1. Hallucination Risk Assessment
Based on scan evidence, identify where the current system risks AI \
hallucination on precise data:
- Which modules handle financial/medical/safety-critical data?
- Where does AI output flow directly into data computations?
- What hardcoded values should be externalized?

### 2. SPA Architecture Design
Redesign the system into two spaces:

**Reasoning Space (AI-managed):**
- List what the AI SHOULD do (strategy, routing, NL generation)
- Define the "pointer interface" — what APIs/calls the AI can make
- Specify the contract: input format, expected return type

**Physical Space (Code-managed):**
- List what must be in precise modules (calculations, DB queries)
- Define the "dereference modules" — functions that fetch real data
- Specify type contracts: Decimal, not float; validated, not raw

### 3. Pointer Protocol
For each data boundary:
- Define the pointer token format (API endpoint, function name, query)
- Define the dereference contract (input type → output type)
- Define the error handling (what if pointer returns null/error?)
- Define the validation layer (how to verify dereferenced data)

### 4. Migration Plan
Phase-by-phase refactoring:
- Phase 1: Identify and isolate the highest-risk boundary
- Phase 2: Build the dereference module for that boundary
- Phase 3: Replace AI direct data handling with pointer calls
- Phase 4: Add validation layer and monitoring
- Phase 5: Repeat for remaining boundaries

### 5. Example Pointer Table
Provide a concrete table:

| Pointer | Dereference Module | Input | Output | Risk Level |
|---------|-------------------|-------|--------|------------|
| ...     | ...               | ...   | ...    | ...        |

Be architectural. Think in terms of memory management, not prompts.
"""


class PointerTool(Tool):
    """语义指针架构分析工具."""

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
        return "analysis_pointer"

    @property
    def description(self) -> str:
        return (
            "语义指针架构(SPA)：检测代码中 AI 直接处理精密数据"
            "的幻觉风险点，设计推理态(AI逻辑)与物理态(精确计算)"
            "分离方案，定义指针协议（API/DB引用）替代直接数据操作。"
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
                "context": {
                    "type": "string",
                    "description": "补充上下文（业务领域、精度要求等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self,
        *,
        target: str,
        context: str = "",
        **kwargs: Any,
    ) -> str:
        files = self._resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = self._read_sources(files)
        scan_evidence = scan_pointer(files, source_text, target)
        deterministic = build_pointer_report(scan_evidence, files, context)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 SPA 指针方案。"

        user_msg = (
            f"## SPA 扫描证据\n{scan_evidence}\n\n"
            f"## 确定性 SPA 方案\n{deterministic}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        enhanced = await self._run_analysis(router, POINTER_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM SPA 架构增强\n" + enhanced
