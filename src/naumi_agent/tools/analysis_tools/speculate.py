"""Speculative decoding analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.speculate import (
    build_speculate_report,
    scan_speculate,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]

SPECULATE_SYSTEM = """\
You are a Speculative Decoding engine using the "Intern Draft + Architect \
Review" dual-mode paradigm.

## Core Principle
Split the work into TWO passes:
1. **Intern Pass (Fast Draft)**: Rapidly generate the outline, boilerplate, \
and straightforward sections. Don't overthink — just get it written.
2. **Architect Pass (Slow Review)**: Carefully review ONLY the zones flagged \
as high-risk. This is where you spend your "slow thinking" budget.

## Your Tasks

### Phase 1: Intern Draft (Fast)
Generate the initial draft at high speed:
- Produce the full solution outline
- Write boilerplate sections (imports, setup, data models, config)
- Implement the straightforward logic paths
- For each section, mark: ✅ (confident) or ⚠️ (needs review)

### Phase 2: Architect Review (Slow)
For EVERY ⚠️ section, perform deep analysis:
- **Memory safety**: Any leaks, double-frees, buffer overflows?
- **Concurrency**: Deadlocks, race conditions, priority inversion?
- **Error handling**: Are all failure paths covered? Silent catches?
- **Security**: Injection, traversal, deserialization risks?
- **Edge cases**: Empty inputs, None, negative numbers, concurrent access?

For each reviewed section:
- Show the original draft code
- Show the reviewed/fixed code with changes highlighted
- Explain WHY each change was needed

### Phase 3: Diff Summary
Produce a final summary:
- Total lines drafted: N
- Lines reviewed and modified: N
- CRITICAL fixes applied: N
- Remaining concerns: (list any unresolved issues)
- Confidence in the final output: X/10

Be decisive in the intern phase, surgical in the architect phase.
"""


class SpeculateTool(Tool):
    """推测解码双阶段审查工具."""

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
        return "analysis_speculate"

    @property
    def description(self) -> str:
        return (
            '推测解码(Speculative Decoding)：先用"实习生"模式极速生成初稿'
            '（样板代码、大纲、常规逻辑），再用"架构师"模式'
            "对高风险区域（内存、并发、安全、边界情况）进行逐行审查与重构。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要生成/审查的文件或目录路径",
                },
                "task": {
                    "type": "string",
                    "description": "要生成的代码功能描述",
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
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = self._read_sources(files)
        scan_evidence = scan_speculate(files, source_text, target)
        deterministic = build_speculate_report(scan_evidence, files, task)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性推测解码计划。"

        user_msg = (
            f"## 风险扫描证据\n{scan_evidence}\n\n"
            f"## 确定性双阶段计划\n{deterministic}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if task:
            user_msg += f"\n## 生成任务\n{task}\n"

        enhanced = await self._run_analysis(router, SPECULATE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 推测解码增强\n" + enhanced
