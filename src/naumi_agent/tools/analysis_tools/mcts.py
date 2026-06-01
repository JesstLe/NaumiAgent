"""MCTS decision analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.mcts import (
    build_mcts_decision_report,
    scan_mcts,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]

MCTS_SYSTEM = """\
You are a Monte Carlo Tree Search (MCTS) decision engine implementing \
Test-Time Compute scaling (System 2 "slow thinking").

You have REAL complexity analysis data. Your task:

## Core Principle
**DO NOT immediately output the first answer that comes to mind.** \
Instead, explicitly explore multiple solution paths, evaluate each, \
and only output the verified best path.

## Mandatory Output Structure

### Path A: [Descriptive Name]
- **Approach**: How this path solves the problem
- **Estimated effort**: Lines of code / time / complexity
- **Pros**: What makes this path attractive
- **Cons**: What could go wrong
- **Disaster simulation**: What happens if this path fails?
  - Which edge cases would break it?
  - What are the failure modes?
  - Score: X/10 confidence

### Path B: [Descriptive Name]
(same structure as Path A)

### Path C: [Descriptive Name] (if applicable)
(same structure)

### Pruning Decision
- Path A score: X/10 → KEEP / PRUNE (reason)
- Path B score: X/10 → KEEP / PRUNE (reason)
- Path C score: X/10 → KEEP / PRUNE (reason)

### Winning Path: [Selected Path Name]
- **Why this path wins**: Clear justification
- **Implementation plan**: Step-by-step
- **Validation**: How to verify correctness
- **Backtracking trigger**: Under what conditions to abandon this path

### Regression Guard
- What test would catch if this solution breaks in the future?
- What monitoring would detect degradation?

## Rules
1. You MUST generate at least 2 distinct paths (3 recommended)
2. Each path must be genuinely different (not just renaming variables)
3. Disaster simulation must identify at least 2 real failure modes
4. The winning path must have explicit backtracking criteria
5. If all paths score below 5/10, say so and explain why the problem \
needs human intervention
"""


class MCTSTool(Tool):
    """MCTS 蒙特卡洛树搜索决策工具."""

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
        return "analysis_mcts"

    @property
    def description(self) -> str:
        return (
            "蒙特卡洛树搜索(MCTS)慢思考机制：对待解决问题进行多路径探索，"
            "生成至少3条截然不同的解决方案，对每条路径进行灾难推演（自我博弈），"
            "主动剪掉错误树枝，只输出经过验证的最佳路径。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "problem": {
                    "type": "string",
                    "description": "待解决的问题描述（算法题、架构决策、策略选择等）",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码的文件路径或目录路径（可选）",
                    "default": "",
                },
            },
            "required": ["problem"],
        }

    async def execute(
        self,
        *,
        problem: str,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        scan_evidence = ""
        source_text = ""
        if target:
            files = self._resolve_target(target)
            if files:
                source_text = self._read_sources(files)
                scan_evidence = scan_mcts(files, source_text, problem)
        deterministic = build_mcts_decision_report(problem, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 MCTS 决策骨架。"

        user_msg = f"## 待解决问题\n{problem}\n"
        if scan_evidence:
            user_msg += f"\n## 复杂度扫描证据\n{scan_evidence}\n"
        user_msg += f"\n## 确定性 MCTS 骨架\n{deterministic}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:40000]}\n"

        enhanced = await self._run_analysis(router, MCTS_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM MCTS 深化\n" + enhanced
