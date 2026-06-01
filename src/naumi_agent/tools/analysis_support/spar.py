"""Deterministic adversarial self-play helpers."""

from __future__ import annotations

import ast
import re

from naumi_agent.tools import analysis_common
from naumi_agent.tools.analysis_support import static_modes

REWARD_HACK_PATTERNS = [
    (
        r"if\s*\(\s*(?:len|size|length)\s*.*>\s*\d+\s*\)\s*(?:return|break|continue)",
        "大小检查直接返回 — 可能跳过处理而非修复根因",
    ),
    (
        r"except\s*(?:Exception|BaseException)\s*:\s*(?:return|pass)",
        "裸 except 静默吞掉异常 — 可能掩盖真实 Bug",
    ),
    (r"try:\s*\n[^#\n]*except:\s*\n\s*pass", "try/except pass — 无条件忽略所有错误"),
    (
        r"(?:TODO|FIXME|HACK|XXX).*(?:bypass|skip|ignore|workaround)",
        "绕行式临时注释 — 非正式修复",
    ),
    (r"if\s+False:", "死代码分支 — 可能是删除功能以满足测试"),
    (
        r"return\s+(?:None|True|False|\"\"|0)\s*#\s*(?:pass|bypass|skip)",
        "硬编码返回 + 绕过注释",
    ),
    (r"assert\s+False", "断言失败式短路 — 放弃而非修复"),
    (
        r"#\s*noqa|#\s*type:\s*ignore|#\s*pylint:\s*disable",
        "静默压制 Linter/类型检查 — 可能掩盖问题",
    ),
]

VULN_SURFACE_PATTERNS = [
    (r"(?:malloc|calloc|realloc|new)\s*\(", "堆内存分配"),
    (r"(?:\bfree\s*\(|\bdelete\s*(?:\(|\[|\s+\w+))", "堆内存释放"),
    (r"(?:strcpy|strcat|sprintf|gets)\s*\(", "不安全字符串操作"),
    (r"(?:memcpy|memmove)\s*\([^,]+,\s*[^,]+,\s*[^)]+\)", "内存拷贝"),
    (r"(?:fopen|fwrite|fread|open)\s*\(", "文件 I/O"),
    (r"(?:socket|connect|bind|accept|recv|send)\s*\(", "网络 I/O"),
    (r"(?:thread|Thread|spawn|fork|asyncio)\s*[\(\[]?", "并发/多线程"),
    (r"(?:subprocess|os\.system|os\.popen|exec|eval)\s*\(", "命令执行"),
    (r"(?:sql|cursor|execute)\s*\(", "数据库操作"),
    (r"(?:json\.loads|yaml\.load|pickle\.loads)\s*\(", "反序列化"),
    (r"\[\s*[^\]]*\s*\]\s*=|\.append|\.insert", "数组/列表写入"),
    (r"(?:int|float)\s*\([^)]*\)", "类型转换 — 可能溢出/精度丢失"),
]

ADVERSARIAL_INPUT_STRATEGIES = {
    "堆内存分配": [
        "超大输入 (>2GB) 测试内存耗尽",
        "零长度输入触发边界分配",
        "交错分配/释放制造碎片",
    ],
    "堆内存释放": [
        "重复释放 (double free) 同一指针",
        "释放后使用 (use-after-free)",
        "释放 NULL 指针",
    ],
    "不安全字符串操作": [
        "超长字符串 (100K+) 缓冲区溢出",
        "嵌入 NULL 字节截断",
        "Unicode/多字节混合编码",
    ],
    "内存拷贝": [
        "源/目标重叠区域拷贝",
        "拷贝长度 > 实际缓冲区",
        "空指针 + 非零长度",
    ],
    "文件 I/O": [
        "符号链接指向敏感文件",
        "并发读写同一文件",
        "文件名含路径遍历 (../../etc/passwd)",
    ],
    "网络 I/O": [
        "半开连接耗尽端口",
        "畸形 HTTP 请求头",
        "超时 + 重试风暴",
    ],
    "并发/多线程": [
        "竞态条件: 1000 线程同时写同一变量",
        "死锁: 按相反顺序获取锁",
        "活锁: 高优先级线程持续抢占",
    ],
    "命令执行": [
        "命令注入: ; rm -rf /",
        "环境变量劫持",
        "参数中嵌入反引号/管道符",
    ],
    "数据库操作": [
        "SQL 注入: ' OR 1=1 --",
        "超长查询字段",
        "并发事务死锁",
    ],
    "反序列化": [
        "恶意 pickle 字节码",
        "循环引用 JSON 对象",
        "深度嵌套 (>100层) 结构",
    ],
    "数组/列表写入": [
        "越界索引访问",
        "超大数组内存耗尽",
        "负索引边界",
    ],
    "类型转换": [
        "整数溢出: sys.maxsize + 1",
        "NaN / Inf 浮点输入",
        "非数字字符串转数值",
    ],
}

NIHILISM_SIGNALS = [
    "删除所有功能代码以满足安全要求",
    "空函数体 (只有 return/pass)",
    "核心逻辑被条件编译排除",
    "所有 public 方法改为 private 且无调用者",
    "测试中全用 assert True",
]


def scan_spar(target: str) -> str:
    """Scan adversarial self-play readiness from real source code evidence."""
    findings: list[str] = []
    source = static_modes.read_sources_for_ast(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    findings.append("## 1. 攻击面扫描 (Vulnerability Surface)")
    vuln_hits: dict[str, list[int]] = {}
    for pattern, label in VULN_SURFACE_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                vuln_hits.setdefault(label, []).append(index)

    if vuln_hits:
        total_vuln_points = sum(len(line_nos) for line_nos in vuln_hits.values())
        findings.append(
            f"- 共检测到 **{total_vuln_points}** 处潜在攻击点，"
            f"覆盖 **{len(vuln_hits)}** 个类别："
        )
        for label, line_nos in sorted(vuln_hits.items(), key=lambda item: -len(item[1])):
            samples = line_nos[:5]
            loc_str = ", ".join(str(line_no) for line_no in samples)
            if len(line_nos) > 5:
                loc_str += f" 等 {len(line_nos)} 处"
            findings.append(f"  - **{label}**: {loc_str}")
    else:
        findings.append("- 未检测到明显的底层操作，攻击面较低")
    findings.append("")

    findings.append("## 2. 对抗输入策略推荐")
    recommended = 0
    for label in vuln_hits:
        strategies = ADVERSARIAL_INPUT_STRATEGIES.get(label, [])
        if strategies:
            recommended += 1
            findings.append(f"  **[{label}]**")
            for strategy in strategies:
                findings.append(f"    - {strategy}")
    if recommended == 0:
        findings.append("- 代码较为安全，建议使用通用模糊测试")
    findings.append("")

    findings.append("## 3. 奖励作弊检测 (Reward Hacking Risk)")
    hack_hits: list[tuple[str, int, str]] = []
    for pattern, desc in REWARD_HACK_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                hack_hits.append((desc, index, line.strip()))

    if hack_hits:
        findings.append(f"- ⚠️ 发现 **{len(hack_hits)}** 处疑似奖励作弊模式：")
        for desc, line_no, line_text in hack_hits[:8]:
            short = line_text[:80] + ("..." if len(line_text) > 80 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        hack_score = min(len(hack_hits) / max(total_lines / 50, 1), 1.0)
        findings.append(
            f"- 作弊风险评分: **{hack_score:.0%}** "
            f"(基于 {len(hack_hits)} 处 / {total_lines} 行)"
        )
    else:
        findings.append("- ✅ 未检测到明显的奖励作弊模式")
    findings.append("")

    findings.append("## 4. 虚无主义检测 (Nihilism Risk)")
    empty_funcs = 0
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                body = node.body
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    empty_funcs += 1
                elif (
                    len(body) == 1
                    and isinstance(body[0], ast.Return)
                    and (
                        body[0].value is None
                        or (
                            isinstance(body[0].value, ast.Constant)
                            and body[0].value.value in (None, True, False, 0, "")
                        )
                    )
                ):
                    empty_funcs += 1
    except SyntaxError:
        empty_funcs = -1

    if empty_funcs > 0:
        findings.append(f"- ⚠️ 发现 **{empty_funcs}** 个空函数体 — 可能是删除功能后的残留")
    elif empty_funcs == 0:
        findings.append("- ✅ 未发现空函数体")

    for signal in NIHILISM_SIGNALS:
        for line in lines:
            if signal in line:
                findings.append(f"  - 虚无信号: `{signal}`")
                break
    findings.append("")

    findings.append("## 5. 代码复杂度")
    import_count = sum(
        1 for line in lines if re.match(r"\s*(?:import|from)\s+", line)
    )
    func_count = 0
    class_count = 0
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                func_count += 1
            elif isinstance(node, ast.ClassDef):
                class_count += 1
    except SyntaxError:
        pass

    findings.append(
        f"- 文件: {total_lines} 行 | {import_count} imports | "
        f"{class_count} classes | {func_count} functions"
    )
    findings.append("")

    vuln_score = min(len(vuln_hits) / 6.0, 1.0)
    hack_risk = min(len(hack_hits) / max(total_lines / 100, 1), 1.0)
    nihilism_risk = (
        min(empty_funcs / max(func_count, 1), 1.0) if func_count > 0 else 0.0
    )

    readiness = (
        (1.0 - nihilism_risk) * 0.4
        + vuln_score * 0.35
        + (1.0 - hack_risk) * 0.25
    )
    readiness = max(0.0, min(1.0, readiness))

    findings.append("## 6. 自博弈就绪度评分")
    findings.append(f"- **综合评分: {readiness:.0%}**")
    findings.append(f"  - 攻击面丰富度: {vuln_score:.0%}")
    findings.append(f"  - 作弊免疫力: {(1.0 - hack_risk):.0%}")
    findings.append(f"  - 虚无主义免疫力: {(1.0 - nihilism_risk):.0%}")
    findings.append(
        "- "
        + (
            "✅ 适合启动对抗性自博弈"
            if readiness >= 0.6
            else "⚠️ 建议先清理代码再启动自博弈"
        )
    )

    return "\n".join(findings)


def build_spar_harness_script(task: str) -> str:
    """Build a dependency-free static adversarial harness."""
    return f'''\
"""Static adversarial self-play harness generated by analysis_spar."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

TASK = {task!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".cpp", ".h", ".hpp"}}
VULN_PATTERNS = [
    (r"(?:subprocess|os\\.system|os\\.popen|exec|eval)\\s*\\(", "命令执行"),
    (r"(?:json\\.loads|yaml\\.load|pickle\\.loads)\\s*\\(", "反序列化"),
    (r"(?:socket|connect|bind|accept|recv|send)\\s*\\(", "网络 I/O"),
    (r"(?:open|fopen|fwrite|fread)\\s*\\(", "文件 I/O"),
    (r"(?:sql|cursor|execute)\\s*\\(", "数据库操作"),
]
REWARD_HACK_PATTERNS = [
    (r"except\\s*(?:Exception|BaseException)?\\s*:\\s*(?:return|pass)", "异常吞噬"),
    (r"assert\\s+True", "空断言"),
    (r"#\\s*(?:noqa|type:\\s*ignore|pylint:\\s*disable)", "静默压制检查"),
    (r"TODO|FIXME|HACK|XXX", "临时绕行标记"),
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


def inspect_python_empty_functions(source: str) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return -1
    empty = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                empty += 1
            elif (
                len(node.body) == 1
                and isinstance(node.body[0], ast.Return)
                and (
                    node.body[0].value is None
                    or (
                        isinstance(node.body[0].value, ast.Constant)
                        and node.body[0].value.value in (None, True, False, 0, "")
                    )
                )
            ):
                empty += 1
    return empty


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    vuln_hits = []
    reward_hits = []
    for line_no, line in enumerate(source.splitlines(), 1):
        for pattern, label in VULN_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                vuln_hits.append({{"line": line_no, "label": label, "sample": line.strip()[:120]}})
        for pattern, label in REWARD_HACK_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                reward_hits.append({{
                    "line": line_no,
                    "label": label,
                    "sample": line.strip()[:120],
                }})
    empty_funcs = inspect_python_empty_functions(source) if path.suffix == ".py" else None
    return {{
        "path": str(path),
        "found": True,
        "lines": len(source.splitlines()),
        "vulnerability_hits": vuln_hits[:50],
        "reward_hack_hits": reward_hits[:50],
        "empty_functions": empty_funcs,
        "red_team_tests": build_red_team_tests(vuln_hits, reward_hits, empty_funcs),
    }}


def build_red_team_tests(
    vuln_hits: list[dict[str, object]],
    reward_hits: list[dict[str, object]],
    empty_functions: int | None,
) -> list[str]:
    tests = []
    labels = {{str(item["label"]) for item in vuln_hits}}
    if "命令执行" in labels:
        tests.append("命令参数注入、环境变量劫持、shell=False 断言")
    if "反序列化" in labels:
        tests.append("深度嵌套、循环引用、非可信 pickle/yaml 输入")
    if "文件 I/O" in labels:
        tests.append("路径穿越、符号链接、并发读写、权限错误")
    if "网络 I/O" in labels:
        tests.append("超时、半开连接、重试风暴、畸形响应")
    if reward_hits:
        tests.append("功能完整性断言：不能通过吞异常、空断言或 noqa 绕过")
    if empty_functions and empty_functions > 0:
        tests.append("功能保留率断言：空函数必须补真实行为或移除死接口")
    return tests or ["通用边界：空输入、超大输入、非法类型、重复执行幂等性"]


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    total_vuln = sum(len(item.get("vulnerability_hits", [])) for item in files)
    total_reward = sum(len(item.get("reward_hack_hits", [])) for item in files)
    return {{
        "status": "ok" if files else "no_source_files",
        "task": TASK,
        "files": files,
        "summary": {{
            "files": len(files),
            "vulnerability_hits": total_vuln,
            "reward_hack_hits": total_reward,
        }},
        "convergence_gate": [
            "目标功能验收测试通过",
            "红队边界测试通过",
            "reward_hack_hits 为 0 或有明确豁免",
            "empty_functions 为 0 或有明确接口设计说明",
        ],
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_spar_report(task: str, scan_evidence: str) -> str:
    """Build deterministic SPAR output."""
    script = build_spar_harness_script(task)
    return (
        "## SPAR 确定性对抗自博弈基线\n"
        "- 执行锚点: 静态攻击面扫描 + 可运行 harness JSON 输出。\n"
        "- 收敛原则: 功能完整性、红队边界、奖励作弊、虚无主义同时验收。\n\n"
        f"## 静态自博弈扫描\n{scan_evidence}\n\n"
        "## Static Adversarial Harness\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python spar_harness.py <source_file_or_directory>`。\n"
        "- 输出 `vulnerability_hits`、`reward_hack_hits`、`empty_functions` 和建议红队测试。\n"
        "- 该脚本只读源码，不写文件、不执行被测代码。\n\n"
        "## 蓝军建设约束\n"
        "- 修复必须保留核心功能，不能用空返回、吞异常、跳过输入来换取测试通过。\n"
        "- 每个高风险入口必须有正常路径、错误路径、极端输入和重复执行测试。\n\n"
        "## 红军攻击策略\n"
        "- 根据 harness 输出逐项生成边界输入，不凭空制造与代码无关的攻击。\n"
        "- 对命令执行、文件 I/O、反序列化、网络 I/O 优先设计可复现实验。\n\n"
        "## 收敛门槛\n"
        "1. 功能验收测试通过。\n"
        "2. 红队边界测试通过。\n"
        "3. `reward_hack_hits` 清零或有明确豁免。\n"
        "4. `empty_functions` 清零或有明确接口设计说明。\n"
    )
