"""Deterministic world-model audit helpers."""

from __future__ import annotations

import ast
import re

from naumi_agent.tools import analysis_common
from naumi_agent.tools.analysis_support import static_modes

STATE_MUTATION_PATTERNS = [
    (r"(?:self\.\w+)\s*=\s*", "实例属性赋值"),
    (r"\w+\[.+\]\s*=\s*", "字典/列表索引赋值"),
    (r"\w+\.(?:append|extend|insert|pop|remove|clear|update)\s*\(", "集合修改方法"),
    (r"(?:await\s+)?(?:db|database|session|cursor)\.\w+\(", "数据库操作"),
    (r"\w+\.(?:save|commit|write|flush|persist)\s*\(", "持久化写入"),
    (r"(?:global|nonlocal)\s+\w+", "全局/闭包变量修改"),
    (r"\w+\.(?:state|status|phase)\s*=\s*", "状态字段直接赋值"),
]

STATE_READ_PATTERNS = [
    (r"\w+\.(?:get|read|fetch|query|select|find|search|load)\s*\(", "读取操作"),
    (r"\w+\.(?:property|@property)", "属性访问"),
    (r"len\s*\(\s*\w+\s*\)", "长度查询"),
    (r"\w+\.(?:count|index|contains|exists)\s*\(", "存在性检查"),
]

CAUSAL_PATTERNS = [
    (r"(?:if|when|on|after|before|handle|trigger|notify|emit|dispatch)\s*", "条件触发"),
    (r"\w+\.(?:on_|handle_|process_|before_|after_)\w+\s*\(", "事件处理器"),
    (r"(?:raise|throw|except|catch|error|fail)", "异常传播"),
    (r"(?:publish|subscribe|emit|listen|broadcast)\s*\(", "消息传递"),
    (r"(?:callback|handler|listener|observer)\s*=", "回调注册"),
]

COUNTERFACTUAL_GAP_PATTERNS = [
    (r"\.\w+\([^)]*\)\s*(?!\s*(?:try|except|if|await))", "无保护的方法调用"),
    (r"\w+\[\s*\w+\s*\](?!\s*=\s*)(?!\s*if)(?!\s*try)", "无边界检查的索引访问"),
    (r"(?:open|connect|request)\s*\(.*\)(?!\s*(?:with|try))", "无上下文管理的资源获取"),
    (r"int\s*\([^)]*\)(?!\s*(?:if|try|or|and))", "无异常处理的类型转换"),
    (r"\.\w+\s*\([^)]*\)\s*$", "链式调用终点无错误处理"),
]


def scan_world(target: str) -> str:
    """Audit source code as a stateful world model with causal transitions."""
    findings: list[str] = []
    source = static_modes.read_sources_for_ast(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    tree = None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        pass

    findings.append("## 1. 状态清单 (State Inventory)")
    state_vars: dict[str, list[int]] = {}
    for pattern, label in STATE_MUTATION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                state_vars.setdefault(label, []).append(index)

    total_mutations = sum(len(line_nos) for line_nos in state_vars.values())
    if state_vars:
        findings.append(
            f"- 检测到 **{total_mutations}** 处状态变更操作，"
            f"覆盖 **{len(state_vars)}** 类："
        )
        for label, line_nos in sorted(state_vars.items(), key=lambda item: -len(item[1])):
            findings.append(
                f"  - {label}: {len(line_nos)} 处 "
                f"(L{line_nos[0]}"
                f"{', L' + str(line_nos[1]) if len(line_nos) > 1 else ''}"
                f"{'...' if len(line_nos) > 2 else ''})"
            )
    else:
        findings.append("- 未检测到明显的状态变更操作")
    findings.append("")

    findings.append("## 2. 状态转移映射 (State Transition Map)")
    transitions: list[tuple[str, str, int]] = []
    if tree:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                func_name = node.name
                reads_in_func: list[str] = []
                writes_in_func: list[str] = []
                func_source = "\n".join(
                    lines[node.lineno - 1 : node.end_lineno or node.lineno]
                )
                for pattern, label in STATE_READ_PATTERNS:
                    if re.search(pattern, func_source):
                        reads_in_func.append(label)
                for pattern, label in STATE_MUTATION_PATTERNS:
                    if re.search(pattern, func_source):
                        writes_in_func.append(label)
                if writes_in_func:
                    read_str = (
                        ", ".join(set(reads_in_func)) if reads_in_func else "外部输入"
                    )
                    write_str = ", ".join(set(writes_in_func))
                    transitions.append(
                        (func_name, f"{read_str} → [{write_str}]", node.lineno)
                    )

    if transitions:
        findings.append(f"- 发现 **{len(transitions)}** 个状态转移函数：")
        for func_name, desc, line_no in transitions[:12]:
            findings.append(f"  - `{func_name}` (L{line_no}): {desc}")
        if len(transitions) > 12:
            findings.append(f"  - ... 还有 {len(transitions) - 12} 个")
    else:
        findings.append("- 未检测到明确的状态转移函数")
    findings.append("")

    findings.append("## 3. 因果链分析 (Causal Chain Analysis)")
    causal_events: dict[str, list[int]] = {}
    for pattern, label in CAUSAL_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                causal_events.setdefault(label, []).append(index)

    if causal_events:
        total_causal = sum(len(line_nos) for line_nos in causal_events.values())
        findings.append(
            f"- 检测到 **{total_causal}** 处因果链节点，"
            f"**{len(causal_events)}** 类："
        )
        for label, line_nos in sorted(
            causal_events.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 因果链稀疏 — 系统可能是无副作用的纯函数式设计")
    findings.append("")

    findings.append("## 4. 客体永久性审计 (Object Permanence)")
    lost_state_count = 0
    potential_lost: list[str] = []
    if tree:
        assigned_attrs: dict[str, int] = {}
        read_attrs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                attr_key = f"{node.value.id}.{node.attr}"
                if isinstance(node.ctx, ast.Store):
                    assigned_attrs[attr_key] = getattr(node, "lineno", 0)
                elif isinstance(node.ctx, ast.Load):
                    read_attrs.add(attr_key)
        for attr, line_no in assigned_attrs.items():
            if attr not in read_attrs:
                lost_state_count += 1
                potential_lost.append(f"`{attr}` (L{line_no})")

    if potential_lost:
        findings.append(
            f"- ⚠️ 发现 **{lost_state_count}** 个属性被写入但从未被读取"
            f" — 可能是'消失的客体'："
        )
        for attr in potential_lost[:6]:
            findings.append(f"  - {attr}")
        if len(potential_lost) > 6:
            findings.append(f"  - ... 还有 {len(potential_lost) - 6} 个")
    else:
        findings.append("- ✅ 所有写入的属性均有读取方，客体永久性良好")
    findings.append("")

    findings.append("## 5. 反事实推演缺口 (Counterfactual Gaps)")
    gap_count = 0
    gap_examples: list[tuple[str, int]] = []
    for pattern, desc in COUNTERFACTUAL_GAP_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line) and not line.strip().startswith("#"):
                gap_count += 1
                if len(gap_examples) < 8:
                    gap_examples.append((desc, index))

    if gap_examples:
        findings.append(f"- ⚠️ 发现 **{gap_count}** 处可能缺少'如果出错了怎么办'的处理：")
        for desc, line_no in gap_examples:
            short_line = lines[line_no - 1].strip()[:70]
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short_line}`")
    else:
        findings.append("- ✅ 关键操作均有保护措施")
    findings.append("")

    state_richness = min(len(state_vars) / 5.0, 1.0)
    transition_richness = min(len(transitions) / 8.0, 1.0)
    causal_density = min(
        sum(len(line_nos) for line_nos in causal_events.values())
        / max(total_lines / 20, 1),
        1.0,
    )
    permanence_score = (
        1.0 - min(lost_state_count / max(total_mutations, 1), 0.5)
        if total_mutations > 0
        else 1.0
    )
    counterfactual_coverage = 1.0 - min(gap_count / max(total_lines / 30, 1), 1.0)

    completeness = (
        state_richness * 0.20
        + transition_richness * 0.25
        + causal_density * 0.20
        + permanence_score * 0.15
        + counterfactual_coverage * 0.20
    )
    completeness = max(0.0, min(1.0, completeness))

    findings.append("## 6. 世界模型完整度评分")
    findings.append(f"- **综合评分: {completeness:.0%}**")
    findings.append(f"  - 状态丰富度: {state_richness:.0%}")
    findings.append(f"  - 转移完备度: {transition_richness:.0%}")
    findings.append(f"  - 因果链密度: {causal_density:.0%}")
    findings.append(f"  - 客体永久性: {permanence_score:.0%}")
    findings.append(f"  - 反事实覆盖: {counterfactual_coverage:.0%}")

    if completeness >= 0.75:
        findings.append("- ✅ 系统具备较完整的世界模型，能感知状态演化")
    elif completeness >= 0.5:
        findings.append("- ⚠️ 世界模型部分建立，存在盲区需补强")
    else:
        findings.append("- ❌ 世界模型严重缺失 — 系统更接近无状态的函数式管道")

    return "\n".join(findings)


def build_world_inventory_script(target: str) -> str:
    """Build a dependency-free state-transition inventory script."""
    return f'''\
"""World-model inventory script generated by analysis_world."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py"}}
STATE_WRITE_PATTERNS = [
    (r"self\\.\\w+\\s*=", "实例属性赋值"),
    (r"\\w+\\[(?:[^\\]]+)\\]\\s*=", "索引赋值"),
    (r"\\.(?:append|extend|insert|pop|remove|clear|update)\\s*\\(", "集合修改"),
    (r"\\.(?:save|commit|write|flush|persist)\\s*\\(", "持久化写入"),
]
CAUSE_PATTERNS = [
    (r"\\bif\\b|\\bexcept\\b|\\braise\\b", "分支/异常因果"),
    (r"\\.(?:emit|dispatch|publish|send|notify)\\s*\\(", "事件发布"),
    (r"\\.(?:handle|process|on_)\\w*\\s*\\(", "事件处理"),
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


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}

    lines = source.splitlines()
    writes = []
    causes = []
    for line_no, line in enumerate(lines, 1):
        for pattern, label in STATE_WRITE_PATTERNS:
            if re.search(pattern, line):
                writes.append({{"line": line_no, "label": label, "sample": line.strip()[:120]}})
        for pattern, label in CAUSE_PATTERNS:
            if re.search(pattern, line):
                causes.append({{"line": line_no, "label": label, "sample": line.strip()[:120]}})

    transitions = []
    assigned_attrs: dict[str, int] = {{}}
    read_attrs: set[str] = set()
    syntax_error = None
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                segment = ast.get_source_segment(source, node) or ""
                if any(re.search(pattern, segment) for pattern, _ in STATE_WRITE_PATTERNS):
                    transitions.append({{
                        "function": node.name,
                        "line": node.lineno,
                        "writes_state": True,
                    }})
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                key = f"{{node.value.id}}.{{node.attr}}"
                if isinstance(node.ctx, ast.Store):
                    assigned_attrs[key] = getattr(node, "lineno", 0)
                elif isinstance(node.ctx, ast.Load):
                    read_attrs.add(key)
    except SyntaxError as exc:
        syntax_error = str(exc)

    lost_state = [
        {{"name": name, "line": line}}
        for name, line in assigned_attrs.items()
        if name not in read_attrs
    ]
    return {{
        "path": str(path),
        "found": True,
        "lines": len(lines),
        "state_writes": writes[:80],
        "causal_events": causes[:80],
        "transitions": transitions[:80],
        "lost_state": lost_state[:80],
        "syntax_error": syntax_error,
    }}


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    total_writes = sum(len(item.get("state_writes", [])) for item in files)
    total_transitions = sum(len(item.get("transitions", [])) for item in files)
    total_lost = sum(len(item.get("lost_state", [])) for item in files)
    return {{
        "status": "ok" if files else "no_python_sources",
        "target": TARGET,
        "files": files,
        "summary": {{
            "files": len(files),
            "state_writes": total_writes,
            "transitions": total_transitions,
            "lost_state": total_lost,
        }},
        "counterfactual_gate": [
            "每个状态转移函数有失败路径",
            "每个持久化写入有回滚或幂等策略",
            "每个 lost_state 有读取方、生命周期说明或删除决策",
        ],
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_world_report(target: str, scan_evidence: str) -> str:
    """Build deterministic world-model audit output."""
    script = build_world_inventory_script(target)
    return (
        "## World 确定性世界模型审计\n"
        "- 执行锚点: AST/正则状态 inventory + 可运行 JSON 审计脚本。\n"
        "- 审计目标: 状态实体、状态转移、因果链、客体永久性、反事实缺口。\n\n"
        f"## 静态世界模型扫描\n{scan_evidence}\n\n"
        "## World Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python world_inventory.py <python_file_or_directory>`。\n"
        "- 输出 `state_writes`、`transitions`、`causal_events`、`lost_state`。\n"
        "- 脚本只读 Python 源码，不执行目标代码。\n\n"
        "## 状态宇宙图谱\n"
        "- `state_writes` 是候选状态实体和变更点。\n"
        "- `transitions` 是会改变世界状态的函数，应成为回归测试入口。\n"
        "- `lost_state` 是被写入但缺少读取方的实体，需要生命周期解释。\n\n"
        "## 反事实补强计划\n"
        "1. 为每个状态转移补失败路径、幂等性和回滚检查。\n"
        "2. 为每个持久化写入补提交失败、重试、重复执行的测试。\n"
        "3. 为每个 lost_state 选择读取方、生命周期文档或删除策略。\n"
        "4. 将 inventory JSON 固化为回归样本，防止状态模型退化。\n"
    )
