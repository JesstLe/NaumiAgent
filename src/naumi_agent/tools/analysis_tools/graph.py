"""GraphRAG topology analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.graph import format_graph_report, scan_graph
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]
CwdGetter = Callable[[], Path]

GRAPH_SYSTEM = """\
You are a GraphRAG (Graph-based Retrieval Augmented Generation) analyst.

You have REAL graph analysis data extracted from the codebase. Your task:

## Core Principle
Abandon linear text output. Model the problem as a **topology graph** \
with entity nodes and relationship edges. Think in terms of connectivity, \
centrality, and influence propagation.

## Your Tasks

### 1. Entity-Relationship Map
Based on the scan evidence, present the discovered topology:
- What are the core entities (classes, modules, functions)?
- What are the key relationships (imports, inheritance, calls)?
- Where are the hub nodes (high degree centrality)?

### 2. Structural Analysis
- **Bottleneck nodes**: Single points with many dependencies
- **Orphan nodes**: Entities with no connections (dead code risk)
- **Tight clusters**: Groups of highly interconnected entities (coupling risk)
- **Bridge nodes**: Entities that connect otherwise separate clusters

### 3. Risk Propagation Simulation
If node X fails, trace the blast radius through the graph:
- Which nodes are directly affected?
- Which clusters are disconnected?
- What is the cascading failure chain?

### 4. Optimization Recommendations
Based on the graph topology:
- Where to decouple (break cycles, reduce coupling)
- Where to consolidate (merge tightly coupled clusters)
- Where to add redundancy (single points of failure)

### 5. Visual Description
Describe the graph in a way that allows visualization:
- List the top 10 most important nodes with their connections
- Describe the overall shape (star, mesh, tree, layered)
- Identify the "backbone" path through the system

Be precise with graph theory terminology. Show adjacency, not just lists.
"""


class GraphRAGTool(Tool):
    """GraphRAG 升维图谱推演工具."""

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
        return "analysis_graph"

    @property
    def description(self) -> str:
        return (
            "GraphRAG 升维图谱推演：从源码中提取实体(类/函数/模块)作为节点、"
            "关系(导入/继承/调用)作为边，构建知识图谱，"
            "计算中心度/连通分量/循环依赖等图指标，"
            "推演风险传导路径并给出架构优化建议。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件路径或目录路径",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        if not target:
            target = str(self._cwd_getter())
        files = self._resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = self._read_sources(files)
        scan_evidence = scan_graph(files, source_text)
        deterministic = format_graph_report(scan_evidence, files)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回静态图谱扫描结果。"

        user_msg = f"## 图谱扫描证据\n{scan_evidence}\n\n## 源代码\n{source_text[:50000]}\n"

        enhanced = await self._run_analysis(router, GRAPH_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 图谱推演\n" + enhanced
