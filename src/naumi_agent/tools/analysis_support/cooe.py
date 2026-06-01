"""Deterministic COOE DAG scheduling analysis helpers."""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

IO_PATTERNS = [
    (r"await\s+(?:client\.|session\.|httpx|aiohttp|requests)", "异步网络 I/O"),
    (r"(?:fetch|download|scrape|crawl|request)\s*\(", "数据抓取"),
    (r"(?:read_text|read_csv|read_json|read_file|open\()", "文件 I/O"),
    (r"(?:cursor\.execute|session\.query|\.query\()", "数据库查询"),
    (r"(?:redis\.\w+|cache\.\w+|memcached)", "缓存 I/O"),
    (r"(?:LLM|model|chat|complete|generate)\s*\(", "LLM API 调用"),
]

PARALLEL_PATTERNS = [
    (r"asyncio\.gather\s*\(", "已使用 asyncio.gather 并行"),
    (r"asyncio\.create_task\s*\(", "已使用 create_task 并行"),
    (r"concurrent\.futures", "已使用线程池并行"),
    (r"multiprocessing", "已使用多进程"),
    (r"threading\.Thread", "已使用多线程"),
    (r"async\s+for\s+", "异步迭代器"),
]

SEQUENTIAL_PATTERNS = [
    (r"result\s*=\s*await\s+\w+.*\n\s*\w+\s*=\s*await", "串行 await 链"),
    (
        r"(?:response|data|result)\s*=\s*await.*\n\s*(?:process|parse|extract)",
        "I/O → 处理串行依赖",
    ),
    (r"for\s+\w+\s+in\s+(?:range|list|items)", "串行循环（可并行化）"),
]


def scan_cooe(files: list[Path], source_text: str, task: str) -> str:
    """Analyze I/O stalls, parallelization opportunities, and call DAG shape."""
    findings: list[str] = []

    io_ops: list[tuple[str, int]] = []
    for pattern, label in IO_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            io_ops.append((label, count))

    if io_ops:
        total_io = sum(count for _, count in io_ops)
        findings.append(f"- I/O 阻塞操作: {total_io} 处（潜在串行等待瓶颈）")
        for label, count in io_ops:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- I/O 阻塞操作: 未检测到")

    parallel_ops: list[tuple[str, int]] = []
    for pattern, label in PARALLEL_PATTERNS:
        count = len(re.findall(pattern, source_text))
        if count:
            parallel_ops.append((label, count))

    if parallel_ops:
        findings.append("- 已有并行化机制:")
        for label, count in parallel_ops:
            findings.append(f"  - ✅ {label}: {count} 处")
    else:
        findings.append("- 已有并行化机制: 无（全部串行执行）")

    seq_ops: list[tuple[str, int]] = []
    for pattern, label in SEQUENTIAL_PATTERNS:
        count = len(re.findall(pattern, source_text, re.MULTILINE))
        if count:
            seq_ops.append((label, count))

    if seq_ops:
        findings.append(f"- 串行瓶颈: {sum(count for _, count in seq_ops)} 处")
        for label, count in seq_ops:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- 串行瓶颈: 未检测到明显瓶颈")

    call_graph: dict[str, set[str]] = collections.defaultdict(set)
    for file in files:
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                func_name = node.name
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            call_graph[func_name].add(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            call_graph[func_name].add(child.func.attr)

    top_level = set(call_graph.keys())
    for callees in call_graph.values():
        top_level -= callees

    if call_graph:
        findings.append(f"- 调用图: {len(call_graph)} 个函数, {len(top_level)} 个顶层入口")
        independent_groups: list[list[str]] = []
        used: set[str] = set()
        for func in top_level:
            if func in used:
                continue
            group = [func]
            used.add(func)
            for callee in call_graph.get(func, set()):
                if callee not in used:
                    group.append(callee)
                    used.add(callee)
            independent_groups.append(group)

        if len(independent_groups) > 1:
            findings.append(f"- 可并行子图: {len(independent_groups)} 组")
            for index, group in enumerate(independent_groups[:5]):
                findings.append(f"  - 组 {index + 1}: {', '.join(group[:4])}")
        else:
            findings.append("- 可并行子图: 仅 1 组（强依赖，难以并行）")

    io_count = sum(count for _, count in io_ops)
    parallel_count = sum(count for _, count in parallel_ops)
    if io_count > 0 and parallel_count == 0:
        est_speedup = f"{min(io_count, 10)}x"
        findings.append(f"- 预估加速比: ~{est_speedup} （全部 I/O 串行，改为并行可获得显著提升）")
    elif io_count > parallel_count:
        findings.append("- 预估加速比: 2-5x（部分已并行，仍有优化空间）")
    elif parallel_count > 0:
        findings.append("- 预估加速比: ~1x（已有并行化机制）")

    has_queue = bool(re.findall(r"(?:Queue|deque|PriorityQueue|asyncio\.Queue)", source_text))
    has_barrier = bool(re.findall(r"(?:Barrier|Event|Semaphore|gather|wait)", source_text))
    rob_features = []
    if has_queue:
        rob_features.append("队列机制")
    if has_barrier:
        rob_features.append("同步屏障")
    if rob_features:
        findings.append(f"- ROB 基础设施: {' + '.join(rob_features)}")
    else:
        findings.append("- ROB 基础设施: 无（需要构建调度器+ROB）")

    if task:
        findings.append(f"- 目标任务: {task[:200]}")

    return "\n".join(findings)


def build_cooe_report(task: str, scan_evidence: str) -> str:
    """Build a deterministic DAG schedule and reorder-buffer plan."""
    subtasks = cooe_subtasks(task)
    worker_count = min(max(2, len(subtasks)), 5)
    sequential = sum(duration for _name, duration, _kind in subtasks)
    critical = max((duration for _name, duration, _kind in subtasks[:-1]), default=1)
    commit = subtasks[-1][1] if subtasks else 1
    parallel = critical + commit
    speedup = sequential / max(parallel, 1)
    lines = [
        "## COOE 确定性 DAG 调度",
        f"- 任务：{task}",
        f"- Reservation Stations：{worker_count}",
        f"- Sequential Estimate：{sequential}s",
        f"- COOE Estimate：{parallel}s",
        f"- Speedup：{speedup:.1f}x",
        "",
        "## 扫描证据",
        scan_evidence,
        "",
        "## Task Decomposition",
    ]
    for idx, (name, duration, kind) in enumerate(subtasks, 1):
        deps = "none" if idx < len(subtasks) else ", ".join(f"T{i}" for i in range(1, idx))
        lines.append(f"- T{idx} {name}: {duration}s, {kind}, deps={deps}")
    lines.extend(
        [
            "",
            "## DAG Visualization",
            *cooe_dag_lines(subtasks),
            "",
            "## Scheduler Design",
            f"- Worker slots: {worker_count}",
            "- Dispatch: ready-queue priority by I/O latency first, CPU work second.",
            "- Failure: failed node blocks dependents; ROB commits completed predecessors only.",
            "",
            "## ROB Configuration",
            f"- Buffer size: {max(4, len(subtasks) * 2)} entries",
            "- Ordering policy: completion order in buffer, logical order on commit.",
            "- Commit trigger: all dependencies for final aggregation are available.",
            "- Backpressure: pause new dispatch when ROB usage exceeds 80%.",
        ]
    )
    return "\n".join(lines)


def cooe_subtasks(task: str) -> list[tuple[str, int, str]]:
    """Split a task into deterministic COOE nodes."""
    chunks = [
        chunk.strip()
        for chunk in re.split(r"[，,;；、]|然后|并且|再|and then| then ", task)
        if len(chunk.strip()) > 2
    ]
    if not chunks:
        chunks = [task.strip() or "执行任务"]
    subtasks: list[tuple[str, int, str]] = []
    for chunk in chunks[:5]:
        lowered = chunk.lower()
        io_bound = any(
            token in lowered
            for token in ("fetch", "api", "http", "读取", "查询", "下载")
        )
        duration = 8 if io_bound else 3
        kind = "I/O-bound" if io_bound else "CPU-bound"
        subtasks.append((chunk[:40], duration, kind))
    if len(subtasks) > 1:
        subtasks.append(("汇总并按依赖顺序提交结果", 2, "commit"))
    return subtasks


def cooe_dag_lines(subtasks: list[tuple[str, int, str]]) -> list[str]:
    """Render a small deterministic DAG visualization."""
    if len(subtasks) <= 1:
        return ["T1 ──→ Commit"]
    commit_id = f"T{len(subtasks)}"
    return [f"T{idx} ──┐" for idx in range(1, len(subtasks))] + [
        f"      ├──→ {commit_id} (ROB commit)"
    ]
