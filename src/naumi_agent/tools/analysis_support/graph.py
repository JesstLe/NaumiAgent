"""Deterministic GraphRAG topology scanners."""

from __future__ import annotations

import ast
import collections
from pathlib import Path


def scan_graph(files: list[Path], source_text: str) -> str:
    """Build an entity-relation graph from Python source files."""
    del source_text

    findings: list[str] = []

    nodes: dict[str, set[str]] = collections.defaultdict(set)
    edges: list[tuple[str, str, str]] = []

    for file in files:
        module_name = file.stem
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                if isinstance(parent, ast.ClassDef):
                    setattr(child, "parent", parent.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                nodes["class"].add(f"{module_name}:{node.name}")
                for base in node.bases:
                    base_name = ""
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = ast.unparse(base)
                    if base_name:
                        edges.append(
                            (
                                f"{module_name}:{node.name}",
                                base_name,
                                "inherits",
                            )
                        )

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if hasattr(node, "parent"):
                    parent = getattr(node, "parent", None)
                    label = (
                        f"{module_name}:{parent}.{node.name}"
                        if parent
                        else f"{module_name}:{node.name}"
                    )
                else:
                    label = f"{module_name}:{node.name}"
                nodes["function"].add(label)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append((module_name, alias.name, "imports"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                edges.append((module_name, node.module, "imports"))

    adj: dict[str, set[str]] = collections.defaultdict(set)
    for src, dst, rel in edges:
        adj[src].add(dst)
        if rel == "imports":
            adj[dst]

    cycles: list[list[str]] = []
    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs_cycle(node: str, path: list[str]) -> None:
        visited.add(node)
        in_stack.add(node)
        path.append(node)
        for neighbor in adj.get(node, set()):
            if neighbor in in_stack:
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:] + [neighbor])
            elif neighbor not in visited:
                dfs_cycle(neighbor, path)
        path.pop()
        in_stack.discard(node)

    for node in list(adj.keys()):
        if node not in visited:
            dfs_cycle(node, [])

    degree: dict[str, int] = collections.defaultdict(int)
    for src, dst, _rel in edges:
        degree[src] += 1
        degree[dst] += 1

    component_map: dict[str, int] = {}
    comp_id = 0
    all_nodes_set: set[str] = set(adj.keys())
    for src, dst, _rel in edges:
        all_nodes_set.add(src)
        all_nodes_set.add(dst)

    unvisited = set(all_nodes_set)
    components: list[set[str]] = []
    while unvisited:
        comp_id += 1
        component: set[str] = set()
        queue = [unvisited.pop()]
        while queue:
            curr = queue.pop()
            component.add(curr)
            component_map[curr] = comp_id
            for neighbor in adj.get(curr, set()):
                if neighbor in unvisited:
                    unvisited.discard(neighbor)
                    queue.append(neighbor)
            for node in all_nodes_set:
                if node in unvisited and curr in adj.get(node, set()):
                    unvisited.discard(node)
                    queue.append(node)
        components.append(component)

    findings.append(f"- 实体节点: {sum(len(v) for v in nodes.values())} 个")
    for node_type, names in nodes.items():
        findings.append(f"  - {node_type}: {len(names)} 个")
        for name in sorted(names)[:6]:
            findings.append(f"    - {name}")
        if len(names) > 6:
            findings.append(f"    ... 还有 {len(names) - 6} 个")

    findings.append(f"- 关系边: {len(edges)} 条")
    edge_types: dict[str, int] = collections.defaultdict(int)
    for _src, _dst, rel in edges:
        edge_types[rel] += 1
    for rel, count in sorted(edge_types.items()):
        findings.append(f"  - {rel}: {count} 条")

    if cycles:
        findings.append(f"- ⚠️ 循环依赖: {len(cycles)} 个")
        for cycle in cycles[:5]:
            cycle_str = " → ".join(cycle[:6])
            if len(cycle) > 6:
                cycle_str += " → ..."
            findings.append(f"  - {cycle_str}")
    else:
        findings.append("- ✅ 无循环依赖")

    findings.append(f"- 连通分量: {len(components)} 个")
    for index, component in enumerate(components[:5]):
        if len(component) <= 4:
            findings.append(f"  - 分量 {index + 1}: {', '.join(sorted(component))}")
        else:
            findings.append(
                f"  - 分量 {index + 1}: {len(component)} 个节点 "
                f"({', '.join(sorted(component)[:3])}...)"
            )

    top_degree = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:5]
    if top_degree:
        findings.append("- 核心节点（度中心性 Top 5）:")
        for name, deg in top_degree:
            findings.append(f"  - {name}: degree={deg}")

    return "\n".join(findings)


def format_graph_report(scan_evidence: str, files: list[Path]) -> str:
    """Format deterministic GraphRAG scanner output."""
    return "\n".join(
        [
            "## GraphRAG 静态图谱",
            f"- 扫描文件数：{len(files)}",
            "",
            scan_evidence,
        ]
    )
