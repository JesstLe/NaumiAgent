"""COOE DAG scheduling analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.cooe import build_cooe_report, scan_cooe
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]

COOE_SYSTEM = """\
You are a Cognitive Out-of-Order Execution (COOE) engine architect, \
directly applying CPU pipeline design to AI agent workflows.

## Core Principle
**NEVER think linearly about complex multi-step tasks.** Instead, model \
the task as a Directed Acyclic Graph (DAG) and execute like a modern \
CPU's out-of-order execution pipeline.

## The 3-Stage Pipeline

### Stage 1: Instruction Decode & DAG Generation
Break the task into atomic sub-tasks and build the dependency graph:
- Each node is an atomic operation (fetch data, compute, transform, etc.)
- Each edge is a DATA dependency (Task B needs Task A's output)
- Identify all independent branches (can run in parallel)

Output a formal DAG:
```
Task A (fetch财报) ──┐
                     ├──→ Task D (汇总分析) ──→ Task E (写报告)
Task B (拉K线)   ──┤
                     ├──→ Task D
Task C (搜政策)   ──┘
```

### Stage 2: Reservation Stations & Parallel Issue
For each independent task group:
- Assign to a "reservation station" (worker agent/slot)
- Mark estimated execution time (I/O bound vs CPU bound)
- Mark resource requirements (API calls, memory, etc.)
- Issue all independent tasks SIMULTANEOUSLY

### Stage 3: Reorder Buffer (ROB) & Commit
- All results enter the ROB in completion order
- Results are held until all predecessors in the DAG are complete
- Commit stage assembles results in the correct logical order
- Only THEN produce the final output

## Your Output Format

### 1. Task Decomposition
List every atomic sub-task with:
- Name, estimated time, I/O vs CPU bound, dependencies

### 2. DAG Visualization
Show the complete dependency graph with ASCII art or structured text

### 3. Execution Timeline
Compare sequential vs parallel timelines:
```
Sequential:  [A: 10s] → [B: 5s] → [C: 3s] → [D: 2s] = 20s
COOE:        [A: 10s]
             [B: 5s]  ──→ [D: 2s]  = 12s
             [C: 3s]  ──↗
```

### 4. Scheduler Design
- How many worker slots (reservation stations)?
- What's the dispatch strategy (FIFO, priority-based)?
- How to handle failures (one task fails, what happens)?

### 5. ROB Configuration
- Buffer size and ordering policy
- Commit trigger conditions
- Backpressure handling (what if ROB is full?)

### 6. Speedup Analysis
- Theoretical maximum speedup (critical path)
- Practical speedup accounting for overhead
- Bottleneck analysis (which task limits parallelism?)

Be architectural. Think in terms of CPU pipeline stages, not prompts.
"""


class COOETool(Tool):
    """COOE 认知乱序执行调度工具."""

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
        return "analysis_cooe"

    @property
    def description(self) -> str:
        return (
            "认知乱序执行引擎(COOE)：将复杂任务拆解为 DAG（有向无环图），"
            "识别数据依赖和可并行步骤，设计调度器+保留站+"
            "重排序缓冲(ROB)的 CPU 级流水线架构，"
            "实现时间复杂度的极致压缩。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要分析的多步骤任务描述",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码路径（可选）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        source_text = ""
        files: list[Path] = []
        if target:
            files = self._resolve_target(target)
            if files:
                source_text = self._read_sources(files)

        scan_evidence = scan_cooe(files, source_text, task)
        deterministic = build_cooe_report(task, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 COOE 调度计划。"

        user_msg = f"## 任务描述\n{task}\n"
        user_msg += f"\n## COOE 扫描证据\n{scan_evidence}\n"
        user_msg += f"\n## 确定性 COOE 计划\n{deterministic}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:40000]}\n"

        enhanced = await self._run_analysis(router, COOE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM COOE 架构增强\n" + enhanced
