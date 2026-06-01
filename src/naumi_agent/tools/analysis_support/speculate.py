"""Deterministic speculative decoding analysis helpers."""

from __future__ import annotations

import re
from pathlib import Path

BOILERPLATE_PATTERNS = [
    (r"def __init__\(self[^)]*\):\s*\n(?:\s+self\.\w+\s*=.*\n){3,}", "批量属性赋值"),
    (r"def (get_|set_|is_|has_)\w+\(self[^)]*\):\s*\n\s+return self\.\w+", "trivial getter/setter"),
    (r"(?:import|from)\s+\w+\s+import\s+\([^)]{50,}\)", "大批量导入"),
    (r"class\s+\w+\(.*Model.*\):\s*\n(?:\s+\w+:\s+\w+.*\n){5,}", "数据模型字段列表"),
    (r"@router\.(get|post|put|delete)\([^)]+\)\s*\n(?:async\s+)?def\s+\w+", "CRUD 端点"),
    (
        r"(?:try:\s*\n\s+.*\n\s+except\s+\w+.*:\s*\n"
        r"\s+raise\s+HTTPException){3,}",
        "重复 try/except 模式",
    ),
]

RISK_PATTERNS = [
    (r"malloc|calloc|realloc|free\s*\(", "内存管理操作", "CRITICAL"),
    (r"threading\.Lock|multiprocessing\.Lock|asyncio\.Lock", "并发锁", "HIGH"),
    (r"\.join\(timeout\s*=\s*None\)|\.wait\(\)", "无限等待/死锁风险", "HIGH"),
    (r"open\([^)]*,\s*['\"]w['\"]", "文件写入（无异常保护检查）", "MEDIUM"),
    (r"eval\s*\(|exec\s*\(", "动态代码执行", "CRITICAL"),
    (r"subprocess\.(call|run|Popen)\(", "子进程执行", "HIGH"),
    (r"os\.system\s*\(", "系统命令执行", "CRITICAL"),
    (r"pickle\.loads?\s*\(", "反序列化（安全风险）", "CRITICAL"),
    (r"cursor\.execute\s*\(\s*f['\"]", "SQL 字符串拼接（注入风险）", "CRITICAL"),
    (r"except\s*:\s*\n\s*pass", "静默吞异常", "HIGH"),
]


def scan_speculate(files: list[Path], source_text: str, target: str) -> str:
    """Identify boilerplate regions and high-risk review targets."""
    del target

    findings: list[str] = []

    boilerplate_items: list[str] = []
    for pattern, label in BOILERPLATE_PATTERNS:
        matches = re.findall(pattern, source_text, re.MULTILINE)
        if matches:
            boilerplate_items.append(f"{label}: {len(matches)} 处")

    if boilerplate_items:
        findings.append("- 样板代码模式（可快速起草后审查）:")
        for item in boilerplate_items:
            findings.append(f"  - {item}")
    else:
        findings.append("- 样板代码: 未检测到明显样板模式")

    risk_zones: list[tuple[str, str, str, int]] = []
    for line_number, line in enumerate(source_text.split("\n"), 1):
        for pattern, label, risk_level in RISK_PATTERNS:
            if re.search(pattern, line):
                risk_zones.append((label, risk_level, line.strip()[:80], line_number))

    if risk_zones:
        findings.append(f"- ⚠️ 高风险区域: {len(risk_zones)} 处（必须慢思考审查）")
        for risk_level in ("CRITICAL", "HIGH", "MEDIUM"):
            items = [risk for risk in risk_zones if risk[1] == risk_level]
            if items:
                findings.append(f"  - {risk_level} ({len(items)} 处):")
                for label, _, snippet, line_number in items[:5]:
                    findings.append(f"    - L{line_number}: [{label}] `{snippet}`")
                if len(items) > 5:
                    findings.append(f"    ... 还有 {len(items) - 5} 处")
    else:
        findings.append("- 高风险区域: 未检测到明显风险模式")

    file_complexity: dict[str, dict[str, int]] = {}
    for file in files:
        try:
            content = file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        branches = len(re.findall(r"\bif\s+|\belif\s+", content))
        loops = len(re.findall(r"\bfor\s+|\bwhile\s+", content))
        nesting = 0
        max_nesting = 0
        for char in content:
            if char in {"{", "("}:
                nesting += 1
                max_nesting = max(max_nesting, nesting)
            elif char in {"}", ")"}:
                nesting = max(0, nesting - 1)
        file_complexity[file.name] = {
            "branches": branches,
            "loops": loops,
            "max_nesting": min(max_nesting, 99),
            "lines": content.count("\n") + 1,
        }

    if file_complexity:
        findings.append("- 文件复杂度分布:")
        for file_name, metrics in sorted(
            file_complexity.items(),
            key=lambda x: x[1]["branches"],
            reverse=True,
        )[:6]:
            findings.append(
                f"  - {file_name}: "
                f"{metrics['branches']} 分支, "
                f"{metrics['loops']} 循环, "
                f"最大嵌套 {metrics['max_nesting']}, "
                f"{metrics['lines']} 行"
            )

    total_files = len(files)
    danger_files = sum(
        1
        for metrics in file_complexity.values()
        if metrics["branches"] > 15 or metrics["max_nesting"] > 6
    )
    safe_files = total_files - danger_files
    findings.append(
        f"- 区域划分: {safe_files} 个安全文件, "
        f"{danger_files} 个危险文件 (分支>15 或 嵌套>6)"
    )

    total_risks = len(risk_zones)
    critical_count = sum(1 for risk in risk_zones if risk[1] == "CRITICAL")
    if total_risks > 0:
        review_min = critical_count * 5 + (total_risks - critical_count) * 2
        findings.append(
            f"- 预估审查时间: ~{review_min} 分钟 "
            f"({critical_count} 个 CRITICAL 需要逐行审查)"
        )

    return "\n".join(findings)


def build_speculate_report(scan_evidence: str, files: list[Path], task: str = "") -> str:
    """Build the deterministic intern-draft and architect-review plan."""
    risky_files = speculate_risky_files(files)
    risky_names = {name for name, _ in risky_files}
    safe_files = [file.name for file in files if file.name not in risky_names]
    lines = [
        "## Speculate 确定性双阶段计划",
        f"- 任务：{task or '审查现有代码'}",
        f"- 文件数：{len(files)}",
        "",
        "## 风险扫描",
        scan_evidence,
        "",
        "## Phase 1: Intern Draft",
        "- 快速处理低风险样板区域：导入、配置、数据模型字段、重复 getter/setter。",
        "- 对每个改动保留最小 diff，不跨越模块边界。",
    ]
    if safe_files:
        lines.append(f"- Safe files: {', '.join(safe_files[:8])}")
    lines.extend(
        [
            "",
            "## Phase 2: Architect Review",
        ]
    )
    if risky_files:
        for file_name, reason in risky_files[:8]:
            lines.append(f"- ⚠️ {file_name}: {reason}")
    else:
        lines.append("- 未发现高风险文件；仍需覆盖空输入、错误输入和正常路径。")
    lines.extend(
        [
            "",
            "## Diff Summary Contract",
            "- Total lines drafted: 由实际 diff 统计，禁止口头估算后跳过验证。",
            f"- Files requiring slow review: {len(risky_files)}",
            "- CRITICAL fixes applied: 必须逐条绑定测试或静态证据。",
            "- Remaining concerns: 未覆盖的风险必须显式列出。",
            "- Confidence: 只有 targeted tests 和真实场景都通过后才能 >8/10。",
        ]
    )
    return "\n".join(lines)


def speculate_risky_files(files: list[Path]) -> list[tuple[str, str]]:
    """Return files requiring slow architect review with concise reasons."""
    risky: list[tuple[str, str]] = []
    for file in files:
        try:
            content = file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        reasons: list[str] = []
        for pattern, label, risk_level in RISK_PATTERNS:
            if re.search(pattern, content):
                reasons.append(f"{risk_level}:{label}")
        branches = len(re.findall(r"\bif\s+|\belif\s+", content))
        if branches > 15:
            reasons.append(f"HIGH:分支过多({branches})")
        if reasons:
            risky.append((file.name, ", ".join(reasons[:4])))
    return risky
