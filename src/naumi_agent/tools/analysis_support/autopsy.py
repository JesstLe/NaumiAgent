"""Deterministic execution-trace autopsy helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

BLIND_READ_PATTERNS = [
    (r"(?:grep|rg|ag|find)\s+[^|]+\s*\|\s*\w+", "管道式盲目搜索 (信息过载风险)"),
    (r"(?:read_file|open)\s*\([^)]*(?:\*|\.\*)\s*", "通配符批量读取 (上下文爆炸)"),
    (
        r"(?:search|query|retrieve)\s*\([^)]*\).*top_k\s*=\s*\d{2,}",
        "大范围 RAG 检索 (k>10，噪声过高)",
    ),
    (r"for\s+\w+\s+in\s+(?:glob|os\.walk)", "遍历式文件扫描 (效率极低)"),
]

TRACE_PATTERNS = [
    (r"(?:sys\.settrace|sys\.setprofile|trace)\s*\(", "Python 调用追踪"),
    (r"(?:cProfile|profile|line_profiler)\s*", "性能剖析工具"),
    (r"(?:pdb|ipdb|breakpoint|debugger)\s*", "交互式调试器"),
    (r"(?:strace|ltrace|dtrace|perf)\s*", "系统级调用追踪"),
    (r"(?:logging|logger)\.\w+\s*\([^)]*(?:trace|debug|verbose)", "详细日志追踪"),
    (r"(?:pytest|--tb|traceback|stack.?trace)\s*", "测试堆栈追踪"),
    (r"(?:coverage|branch)\s*", "覆盖率追踪"),
]

SINGLE_HYPOTHESIS_PATTERNS = [
    (
        r"(?:fix|patch|hotfix)\s*\([^)]*\)\s*:\s*\n\s*(?:self\.\w+)\s*=\s*",
        "直接赋值修复 (无假设验证)",
    ),
    (r"#\s*fix\s*:\s*\w+\s*", "注释式修复标记 (未经证伪)"),
    (r"return\s+(?:True|False|None|0)\s*#\s*(?:fix|workaround)", "返回值绕过 (非真正修复)"),
]

HYPOTHESIS_PATTERNS = [
    (r"(?:hypothesis|assume|conjecture|guess)\s*[:=]", "假设定义"),
    (r"(?:assert|verify|check|confirm)\s*\([^)]*(?:hypothesis|assume)", "假设验证断言"),
    (r"(?:probe|inject|instrument)\s*\(", "探测脚本注入"),
    (r"(?:control|variable|experiment)\s*", "控制变量实验"),
    (r"(?:reproduce|minimal|repro)\s*", "最小复现脚本"),
    (r"(?:bisect|binary.?search|narrow)\s*", "二分定位法"),
]

BLAST_RADIUS_PATTERNS = [
    (r"(?:caller|callee|dependency|dependents)\s*", "调用者/依赖者分析"),
    (r"(?:ast|parse|syntax.?tree)\s*", "AST 解析"),
    (r"(?:refactor|impact|risk|radius)\s*", "影响范围评估"),
    (r"(?:backward.?compat|breaking.?change|migration)\s*", "向后兼容性检查"),
    (r"(?:grep|find)\s+[^)]*(?:caller|usage|import|reference)", "引用搜索 (爆炸半径计算)"),
    (r"(?:test|spec).*(?:run|execute|suite)\s*", "回归测试执行"),
]


def _collect_label_hits(
    lines: list[str],
    patterns: list[tuple[str, str]],
) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for pattern, label in patterns:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                hits.setdefault(label, []).append(index)
    return hits


def _collect_line_hits(
    lines: list[str],
    patterns: list[tuple[str, str]],
    *,
    ignore_case: bool = False,
) -> list[tuple[str, int]]:
    flags = re.IGNORECASE if ignore_case else 0
    hits: list[tuple[str, int]] = []
    for pattern, desc in patterns:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, flags):
                hits.append((desc, index))
    return hits


def scan_autopsy(target: str) -> str:
    """Scan DTS-CHE readiness across trace, hypothesis, and blast-radius controls."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 盲目读取风险 (Blind Code Reading)")
    blind_hits = _collect_line_hits(lines, BLIND_READ_PATTERNS)
    if blind_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(blind_hits)}** 处盲目读取模式 — "
            f"上下文可能被无关代码撑爆：",
        )
        for desc, line_no in blind_hits[:6]:
            findings.append(f"  - L{line_no}: {desc}")
        findings.append("- 💡 应改为: 只读取执行迹涉及的关键函数，压缩 99% 无效信息")
    else:
        findings.append("- ✅ 代码读取模式较为精准")
    findings.append("")

    findings.append("## 2. 执行迹基础设施 (Trace Infrastructure)")
    trace_hits = _collect_label_hits(lines, TRACE_PATTERNS)
    if trace_hits:
        total_trace = sum(len(line_nos) for line_nos in trace_hits.values())
        findings.append(
            f"- 检测到 **{total_trace}** 处执行迹工具，"
            f"**{len(trace_hits)}** 类：",
        )
        for label, line_nos in sorted(
            trace_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无执行迹工具 — 无法获取'死亡瞬间的解剖图'")
    findings.append("")

    findings.append("## 3. 假设验证能力 (Hypothesis Verification)")
    hyp_hits = _collect_label_hits(lines, HYPOTHESIS_PATTERNS)
    single_hits = _collect_line_hits(
        lines,
        SINGLE_HYPOTHESIS_PATTERNS,
        ignore_case=True,
    )

    if hyp_hits:
        total_hyp = sum(len(line_nos) for line_nos in hyp_hits.values())
        findings.append(
            f"- 检测到 **{total_hyp}** 处科学验证机制，"
            f"**{len(hyp_hits)}** 类：",
        )
        for label, line_nos in hyp_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无假设验证机制 — Bug 修复可能基于幻觉")

    if single_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(single_hits)}** 处单假设直接修复 "
            f"— 未经证伪，可能改错地方",
        )
    findings.append("")

    findings.append("## 4. 爆炸半径隔离 (Blast-Radius Containment)")
    blast_hits = _collect_label_hits(lines, BLAST_RADIUS_PATTERNS)
    if blast_hits:
        total_blast = sum(len(line_nos) for line_nos in blast_hits.values())
        findings.append(
            f"- 检测到 **{total_blast}** 处爆炸半径控制机制，"
            f"**{len(blast_hits)}** 类：",
        )
        for label, line_nos in blast_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无爆炸半径控制 — 修复可能引发连锁崩溃")
    findings.append("")

    trace_score = min(len(trace_hits) / 4.0, 1.0)
    hyp_score = min(len(hyp_hits) / 3.0, 1.0)
    blast_score = min(len(blast_hits) / 4.0, 1.0)
    blind_penalty = min(len(blind_hits) * 0.1, 0.3)
    single_penalty = min(len(single_hits) * 0.1, 0.3)

    dts_score = (
        trace_score * 0.35
        + hyp_score * 0.30
        + blast_score * 0.25
        - blind_penalty
        - single_penalty
        + 0.10
    )
    dts_score = max(0.0, min(1.0, dts_score))

    findings.append("## 5. DTS-CHE 就绪度评分")
    findings.append(f"- **综合评分: {dts_score:.0%}**")
    findings.append(f"- 执行迹能力: {trace_score:.0%}")
    findings.append(f"- 假设验证能力: {hyp_score:.0%}")
    findings.append(f"- 爆炸半径控制: {blast_score:.0%}")
    findings.append(f"- 盲目读取惩罚: -{blind_penalty:.0%}")
    findings.append(f"- 单假设惩罚: -{single_penalty:.0%}")

    if dts_score >= 0.7:
        findings.append("- ✅ 系统具备 DTS-CHE 架构，可高效定位复杂 Bug")
    elif dts_score >= 0.4:
        findings.append("- ⚠️ 部分具备定位能力，需补强执行迹和假设验证")
    else:
        findings.append("- ❌ Bug 定位方式原始，建议引入 DTS-CHE 三刀锋架构")

    return "\n".join(findings)


def build_autopsy_inventory_script(target: str) -> str:
    """Build a dependency-free trace and blast-radius scanner."""
    return f'''\
"""Autopsy trace inventory script generated by analysis_autopsy."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx", ".sh"}}
BLIND_PATTERNS = [
    (r"(?:grep|rg|ag|find)\\s+[^|]+\\s*\\|\\s*\\w+", "管道式盲目搜索"),
    (r"(?:read_file|open)\\s*\\([^)]*(?:\\*|\\.\\*)", "通配符批量读取"),
    (r"(?:search|query|retrieve)\\s*\\([^)]*top_k\\s*=\\s*\\d{{2,}}", "大范围检索"),
]
TRACE_PATTERNS = [
    (r"(?:sys\\.settrace|sys\\.setprofile|trace)\\s*\\(", "Python 调用追踪"),
    (r"(?:cProfile|profile|line_profiler)", "性能剖析"),
    (r"(?:logging|logger)\\.\\w+\\s*\\([^)]*(?:trace|debug)", "详细日志追踪"),
    (r"(?:pytest|traceback|stack.?trace|coverage)", "测试/堆栈追踪"),
]
HYPOTHESIS_PATTERNS = [
    (r"(?:hypothesis|assume|conjecture|guess)\\s*[:=]", "假设定义"),
    (r"(?:assert|verify|check|confirm)\\s*\\([^)]*(?:hypothesis|assume)", "假设验证"),
    (r"(?:probe|inject|instrument)\\s*\\(", "探测脚本"),
    (r"(?:reproduce|minimal|repro|bisect)", "复现/二分"),
]
BLAST_PATTERNS = [
    (r"(?:caller|callee|dependency|dependents)", "调用者/依赖者"),
    (r"(?:ast|parse|syntax.?tree)", "AST 解析"),
    (r"(?:impact|risk|radius|breaking.?change|migration)", "影响范围"),
    (r"(?:test|spec).*(?:run|execute|suite)", "回归测试"),
]


def collect_sources(raw: str) -> list[Path]:
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        return []
    if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
        return [path]
    if path.is_dir():
        return [
            child for child in list(path.rglob("*"))[:500]
            if child.is_file() and child.suffix.lower() in SOURCE_SUFFIXES
        ][:80]
    return []


def find_hits(source: str, patterns: list[tuple[str, str]]) -> list[dict[str, object]]:
    hits = []
    for line_no, line in enumerate(source.splitlines(), 1):
        for pattern, label in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                hits.append({{"line": line_no, "label": label, "sample": line.strip()[:120]}})
    return hits


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = call_name(node.value)
        return f"{{base}}.{{node.attr}}" if base else node.attr
    return None


def inspect_python_ast(path: Path, source: str) -> dict[str, object]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {{"parse_error": str(exc), "functions": [], "call_edges": []}}
    functions = []
    call_edges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({{"name": node.name, "line": node.lineno}})
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    name = call_name(child.func)
                    if name:
                        call_edges.append({{
                            "caller": node.name,
                            "callee": name,
                            "line": getattr(child, "lineno", node.lineno),
                        }})
    caller_index: dict[str, list[str]] = {{}}
    for edge in call_edges:
        caller_index.setdefault(str(edge["callee"]).split(".")[-1], []).append(
            str(edge["caller"])
        )
    return {{
        "path": str(path),
        "functions": functions[:120],
        "call_edges": call_edges[:200],
        "caller_index": {{k: sorted(set(v)) for k, v in caller_index.items()}},
    }}


def build_autopsy_contract(
    trace: list[dict[str, object]],
    hypothesis: list[dict[str, object]],
    blast: list[dict[str, object]],
    ast_inventory: dict[str, object],
) -> dict[str, object]:
    return {{
        "trace_required": not bool(trace),
        "hypothesis_gate_present": bool(hypothesis),
        "blast_radius_present": bool(blast) or bool(ast_inventory.get("call_edges")),
        "minimum_dts_che_chain": [
            "reproduce",
            "dynamic_trace",
            "three_hypotheses",
            "probe_each_hypothesis",
            "ast_blast_radius",
            "targeted_regression",
        ],
    }}


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    blind = find_hits(source, BLIND_PATTERNS)
    trace = find_hits(source, TRACE_PATTERNS)
    hypothesis = find_hits(source, HYPOTHESIS_PATTERNS)
    blast = find_hits(source, BLAST_PATTERNS)
    ast_inventory = (
        inspect_python_ast(path, source) if path.suffix.lower() == ".py" else {{}}
    )
    return {{
        "path": str(path),
        "found": True,
        "blind": blind[:80],
        "trace": trace[:80],
        "hypothesis": hypothesis[:80],
        "blast": blast[:80],
        "ast": ast_inventory,
        "autopsy_contract": build_autopsy_contract(
            trace, hypothesis, blast, ast_inventory,
        ),
    }}


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    return {{
        "status": "ok" if files else "no_source_files",
        "target": TARGET,
        "files": files,
        "summary": {{
            "files": len(files),
            "blind": sum(len(item.get("blind", [])) for item in files),
            "trace": sum(len(item.get("trace", [])) for item in files),
            "hypothesis": sum(len(item.get("hypothesis", [])) for item in files),
            "blast": sum(len(item.get("blast", [])) for item in files),
            "call_edges": sum(
                len(item.get("ast", {{}}).get("call_edges", [])) for item in files
            ),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_autopsy_report(target: str, scan_evidence: str) -> str:
    """Build deterministic DTS-CHE autopsy output."""
    script = build_autopsy_inventory_script(target)
    return (
        "## Autopsy 确定性执行迹切片审计\n"
        "- 执行锚点: 盲读风险/执行迹/假设验证/爆炸半径 inventory + AST 调用图。\n"
        "- 审计目标: 让 Bug 修复先有复现、执行迹、证伪和影响面证据，再允许改代码。\n\n"
        f"## 静态 DTS-CHE 扫描\n{scan_evidence}\n\n"
        "## Autopsy Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python autopsy_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `blind`、`trace`、`hypothesis`、`blast`、`ast.call_edges` 和 `autopsy_contract`。\n"
        "- 脚本只读源码，只解析 AST，不运行目标代码。\n\n"
        "## Autopsy Contract\n"
        "- 修复前必须有 reproduce case 和 dynamic trace。\n"
        "- 每个根因必须拆成至少 3 个互斥 hypothesis，并用 probe 证伪。\n"
        "- 修复点必须计算 AST blast radius，列出 callers/callees。\n"
        "- 只运行与爆炸半径匹配的 targeted regression，避免无意义全量测试。\n\n"
        "## 改造计划\n"
        "1. 用 `ast.call_edges` 定位潜在修改函数的调用者。\n"
        "2. 为每个失败路径补 trace/probe/reproduce 钩子。\n"
        "3. 将单假设修复改为 hypothesis matrix。\n"
        "4. 根据 blast radius 自动选择最小回归测试集合。\n"
    )
