"""DSPy-inspired prompt compiler analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.dspy import (
    build_dspy_baseline_metric,
    format_dspy_report,
    scan_dspy,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]
CwdGetter = Callable[[], Path]

DSPY_SYSTEM = """\
You are a Prompt Compiler implementing the DSPy (Declaration-based \
Self-evolving Programming) paradigm.

You have REAL static analysis evidence about prompt engineering maturity \
in the codebase. Your task:

## Core Principle
**STOP manually tweaking prompts.** Prompt optimization must be driven by:
1. **Metric** — A measurable evaluation function (not "feels better")
2. **Data** — Ground-truth input/output examples (few-shot samples)
3. **Compiler** — An automated optimizer that searches the prompt space

## Your Tasks

### 1. Current State Assessment
Based on the scan evidence, assess the prompt engineering maturity:
- How many prompts exist? Are they hardcoded or configurable?
- Are there few-shot examples? If not, what examples should be added?
- Are there evaluation metrics? If not, what metrics should be defined?

### 2. Metric Definition
For the target prompt/task, define a concrete evaluation function:
- Input validation: does the output have correct format?
- Quality score: semantic accuracy, relevance, completeness
- Edge case detection: does it handle empty/malformed inputs?
- Provide actual Python code for the metric function

### 3. Few-shot Example Design
Provide 3-5 high-quality input/output pairs that:
- Cover the main use case
- Cover edge cases (empty, ambiguous, adversarial)
- Are unambiguous (a human annotator would agree on the expected output)

### 4. Optimization Plan
Describe the DSPy compilation loop:
- What prompt variants to test (instruction, prefix, suffix)
- What scoring strategy to use (majority vote, weighted, best-of-N)
- How many iterations to run
- When to stop (convergence criteria)

### 5. Anti-pattern Warnings
Flag any of these prompt anti-patterns found in the code:
- "You are a world-class expert" (flattery — brittle across models)
- "Think step by step" (hack — should be structural, not linguistic)
- Overly long system prompts (>2000 chars — context pollution)
- No error handling in output parsing
- Prompt mixing concerns (one prompt doing 3 unrelated things)

Output actionable, compilable recommendations. No hand-waving.
"""


class DSPyTool(Tool):
    """DSPy 声明式 Prompt 编译优化工具."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
        resolve_target: ResolveTarget | None = None,
        read_sources: ReadSources | None = None,
        cwd_getter: CwdGetter | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis
        self._resolve_target = resolve_target or (lambda _target: [])
        self._read_sources = read_sources or (lambda _files: "")
        self._cwd_getter = cwd_getter or Path.cwd

    @property
    def name(self) -> str:
        return "analysis_dspy"

    @property
    def description(self) -> str:
        return (
            "DSPy 声明式 Prompt 编译优化：扫描代码中的 Prompt 模板、"
            "Few-shot 示例、评估函数，计算 Prompt 工程成熟度评分，"
            "并生成可执行的评价函数和优化方案。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要优化的 Prompt 所在的文件或目录路径",
                    "default": "",
                },
                "prompt_target": {
                    "type": "string",
                    "description": "具体想优化的 Prompt 功能描述",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        target: str = "",
        prompt_target: str = "",
        **kwargs: Any,
    ) -> str:
        if not target:
            target = str(self._cwd_getter())
        files = self._resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = self._read_sources(files)
        scan_evidence = scan_dspy(files, source_text, prompt_target)
        deterministic = format_dspy_report(scan_evidence, prompt_target, files)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 DSPy 扫描结果。"

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## Baseline Metric\n{build_dspy_baseline_metric(prompt_target)}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if prompt_target:
            user_msg += f"\n## 优化目标\n用户想要优化这个 Prompt 的效果: {prompt_target}\n"

        enhanced = await self._run_analysis(router, DSPY_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Prompt 编译建议\n" + enhanced
